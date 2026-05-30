#!/usr/bin/env python3
"""
Unified collector for Xiaomi MiFlora sensors and weather data.

Phase 1 goals:
- Replace multiple cron scripts with one service process.
- Keep compatibility with existing flat files used by Growcontrol.html.
- Add explicit status and error reporting instead of writing None metrics.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from growcontrol_env import migrate_openweather_key_from_settings, openweather_api_key_from_env
from btlewrap.bluepy import BluepyBackend
from miflora.miflora_poller import (
    MI_BATTERY,
    MI_CONDUCTIVITY,
    MI_LIGHT,
    MI_MOISTURE,
    MI_TEMPERATURE,
    MiFloraPoller,
)
from growcontrol_storage import GrowcontrolStorage


DEFAULT_SETTINGS_PATH = Path("config/settings.json")
DEFAULT_SENSORS_PATH = Path("config/sensors.json")


@dataclass
class SensorConfig:
    sensor_id: str
    name: str
    mac: str
    output_file: str
    enabled: bool = True


def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("growcontrol")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_settings(path: Path) -> Dict[str, Any]:
    settings = load_json(path)
    required = [
        "output_dir",
        "status_file",
        "weather_file",
        "sensor_history_limit",
        "sensor_poll_minutes",
        "sensor_parallelism",
        "weather_poll_minutes",
        "weather_lat",
        "weather_lon",
        "weather_units",
        "request_timeout_seconds",
        "loop_sleep_seconds",
    ]
    missing = [key for key in required if key not in settings]
    if missing:
        raise ValueError(f"Missing settings keys in {path}: {', '.join(missing)}")
    settings.setdefault("weather_enabled", True)
    settings.setdefault("database_path", "data/growcontrol.db")
    settings, migrated = migrate_openweather_key_from_settings(settings)
    if migrated:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return settings


def load_sensors(path: Path) -> List[SensorConfig]:
    sensors_raw = load_json(path)
    sensors: List[SensorConfig] = []
    for entry in sensors_raw.get("sensors", []):
        sensors.append(
            SensorConfig(
                sensor_id=entry["id"],
                name=entry["name"],
                mac=entry["mac"],
                output_file=entry["output_file"],
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return sensors


def timestamp_display(dt: Optional[datetime] = None) -> str:
    d = dt or datetime.now()
    return d.strftime("%d.%m %H:%M")


def timestamp_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_existing_blocks(output_file: Path) -> List[List[str]]:
    if not output_file.exists():
        return []

    lines = output_file.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks: List[List[str]] = []
    current: List[str] = []

    for line in lines:
        if line.startswith("Date/Time: ") and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)

    if current:
        blocks.append(current)

    return blocks


def trim_blocks(blocks: List[List[str]], history_limit: int) -> List[List[str]]:
    if history_limit <= 0:
        return blocks
    return blocks[-history_limit:]


def append_sensor_block(
    output_file: Path,
    history_limit: int,
    sensor_name: str,
    firmware: Optional[str],
    metrics: Optional[Dict[str, Any]],
    status: str,
    error: Optional[str] = None,
    display_time: Optional[str] = None,
) -> None:
    blocks = read_existing_blocks(output_file)

    new_block: List[str] = [
        f"Date/Time: {display_time or timestamp_display()}\n",
        f"Status: {status}\n",
        f"Configured Name: {sensor_name}\n",
    ]

    if firmware is not None:
        new_block.append(f"Firmware Version: {firmware}\n")

    if metrics:
        new_block.extend(
            [
                f"Temperature: {metrics['temperature']}\n",
                f"Moisture: {metrics['moisture']}\n",
                f"Light: {metrics['light']}\n",
                f"Conductivity: {metrics['conductivity']}\n",
                f"Battery: {metrics['battery']}\n",
            ]
        )
    elif error:
        new_block.append(f"Error: {error}\n")

    new_block.append("\n")

    blocks.append(new_block)
    blocks = trim_blocks(blocks, history_limit)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8") as handle:
        for block in blocks:
            handle.writelines(block)


def collect_sensor(mac: str, timeout_seconds: int) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    try:
        poller = MiFloraPoller(mac, BluepyBackend, cache_timeout=timeout_seconds)
        firmware = poller.firmware_version()
        metrics = {
            "temperature": poller.parameter_value(MI_TEMPERATURE),
            "moisture": poller.parameter_value(MI_MOISTURE),
            "light": poller.parameter_value(MI_LIGHT),
            "conductivity": poller.parameter_value(MI_CONDUCTIVITY),
            "battery": poller.parameter_value(MI_BATTERY),
        }
        return firmware, metrics, None
    except Exception as exc:  # noqa: BLE001 - hardware stack raises varying exceptions
        return None, None, str(exc)


def run_sensor_cycle(
    sensors: List[SensorConfig],
    settings: Dict[str, Any],
    logger: logging.Logger,
    status: Dict[str, Any],
    storage: GrowcontrolStorage,
    cycle_ts: str,
) -> None:
    output_dir = Path(settings["output_dir"])
    history_limit = int(settings["sensor_history_limit"])
    timeout_seconds = int(settings["request_timeout_seconds"])

    sensors_state: Dict[str, Any] = status.setdefault("sensors", {})
    enabled_sensors = [sensor for sensor in sensors if sensor.enabled]
    status["configured_sensor_count"] = len(sensors)
    status["enabled_sensor_count"] = len(enabled_sensors)

    if not enabled_sensors:
        logger.info("No enabled sensors configured; skipping BLE polling")
        return

    try:
        cycle_dt_local = datetime.fromisoformat(cycle_ts).astimezone()
    except Exception:  # noqa: BLE001
        cycle_dt_local = datetime.now().astimezone()
    display_time = timestamp_display(cycle_dt_local)

    # Poll sensors in parallel (bounded) so they share the same cycle timestamp.
    futures = {}
    try:
        configured_parallelism = int(settings.get("sensor_parallelism", 2))
    except Exception:  # noqa: BLE001
        configured_parallelism = 2
    max_workers = max(1, min(6, configured_parallelism, len(enabled_sensors)))
    status["sensor_parallelism"] = configured_parallelism

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for sensor in enabled_sensors:
            futures[pool.submit(collect_sensor, sensor.mac, timeout_seconds)] = sensor

        for fut in as_completed(futures):
            sensor = futures[fut]
            logger.info("Collecting sensor %s (%s)", sensor.sensor_id, sensor.mac)
            firmware, metrics, err = fut.result()
            output_file = output_dir / sensor.output_file

            sensor_state: Dict[str, Any] = sensors_state.setdefault(sensor.sensor_id, {})
            sensor_state["name"] = sensor.name
            sensor_state["mac"] = sensor.mac
            sensor_state["output_file"] = sensor.output_file
            sensor_state["last_attempt"] = cycle_ts

            if err:
                logger.warning("Sensor %s failed: %s", sensor.sensor_id, err)
                sensor_state["status"] = "error"
                sensor_state["last_error"] = err
                sensor_state["enabled"] = sensor.enabled
                append_sensor_block(
                    output_file=output_file,
                    history_limit=history_limit,
                    sensor_name=sensor.name,
                    firmware=None,
                    metrics=None,
                    status="error",
                    error=err,
                    display_time=display_time,
                )
                storage.insert_sensor_reading(
                    ts=cycle_ts,
                    sensor_id=sensor.sensor_id,
                    status="error",
                    metrics=None,
                    error=err,
                )
                continue

            logger.info("Sensor %s OK", sensor.sensor_id)
            sensor_state["status"] = "ok"
            sensor_state["last_error"] = None
            sensor_state["last_success"] = cycle_ts
            sensor_state["last_metrics"] = metrics
            sensor_state["enabled"] = sensor.enabled

            append_sensor_block(
                output_file=output_file,
                history_limit=history_limit,
                sensor_name=sensor.name,
                firmware=firmware,
                metrics=metrics,
                status="ok",
                error=None,
                display_time=display_time,
            )
            storage.insert_sensor_reading(
                ts=cycle_ts,
                sensor_id=sensor.sensor_id,
                status="ok",
                metrics=metrics,
                error=None,
            )


def run_weather_cycle(
    settings: Dict[str, Any],
    logger: logging.Logger,
    status: Dict[str, Any],
    storage: GrowcontrolStorage,
) -> None:
    weather_state = status.setdefault("weather", {})
    if not bool(settings.get("weather_enabled", True)):
        weather_state["status"] = "disabled"
        weather_state["last_error"] = None
        logger.info("Weather collection disabled in settings")
        storage.insert_weather_reading(ts=timestamp_iso(), status="disabled", data=None, error=None)
        return

    # Prefer settings.json key (editable from UI), fallback to EnvironmentFile (.env)
    api_key = openweather_api_key_from_env()
    if not api_key:
        weather_state["status"] = "error"
        weather_state["last_error"] = "OpenWeather API key is missing"
        logger.warning("Weather collection enabled but OPENWEATHER_API_KEY is missing")
        storage.insert_weather_reading(
            ts=timestamp_iso(),
            status="error",
            data=None,
            error=weather_state["last_error"],
        )
        return

    lat = settings["weather_lat"]
    lon = settings["weather_lon"]
    units = settings["weather_units"]
    timeout_seconds = int(settings["request_timeout_seconds"])

    weather_url = "https://api.openweathermap.org/data/2.5/weather"
    params = {"lat": lat, "lon": lon, "appid": api_key, "units": units}

    logger.info("Collecting weather data")
    weather_state["last_attempt"] = timestamp_iso()

    try:
        response = requests.get(weather_url, params=params, timeout=timeout_seconds)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - network stack raises varying exceptions
        err = str(exc)
        logger.warning("Weather fetch failed: %s", err)
        weather_state["status"] = "error"
        weather_state["last_error"] = err
        storage.insert_weather_reading(ts=timestamp_iso(), status="error", data=None, error=err)
        return

    output_dir = Path(settings["output_dir"])
    weather_file = output_dir / settings["weather_file"]
    weather_file.parent.mkdir(parents=True, exist_ok=True)
    weather_file.write_text(json.dumps(data), encoding="utf-8")

    weather_state["status"] = "ok"
    weather_state["last_error"] = None
    weather_state["last_success"] = timestamp_iso()
    storage.insert_weather_reading(ts=timestamp_iso(), status="ok", data=data, error=None)
    logger.info("Weather data OK")


def persist_status(settings: Dict[str, Any], status: Dict[str, Any]) -> None:
    output_dir = Path(settings["output_dir"])
    status_path = output_dir / settings["status_file"]
    output_dir.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")


def load_previous_status(settings: Dict[str, Any]) -> Dict[str, Any]:
    output_dir = Path(settings["output_dir"])
    status_path = output_dir / settings["status_file"]
    if not status_path.exists():
        return {}
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def run_once(
    sensors: List[SensorConfig],
    settings: Dict[str, Any],
    logger: logging.Logger,
    collect_sensors: bool,
    collect_weather: bool,
    storage: GrowcontrolStorage,
) -> int:
    status: Dict[str, Any] = {"updated_at": timestamp_iso(), "service_status": "ok"}
    try:
        cycle_ts = timestamp_iso()
        if collect_sensors:
            run_sensor_cycle(sensors, settings, logger, status, storage, cycle_ts=cycle_ts)
        if collect_weather:
            run_weather_cycle(settings, logger, status, storage)
        status["retention_key"] = storage.get_retention_key()
        storage.prune_old_data(storage.retention_days())
        status["updated_at"] = timestamp_iso()
        persist_status(settings, status)
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level guard for service mode
        logger.exception("Run failed: %s", exc)
        status["updated_at"] = timestamp_iso()
        status["service_status"] = "error"
        status["fatal_error"] = str(exc)
        persist_status(settings, status)
        return 1


def run_loop(
    sensors_path: Path,
    settings_path: Path,
    settings: Dict[str, Any],
    logger: logging.Logger,
    storage: GrowcontrolStorage,
) -> int:
    sensor_every = int(settings["sensor_poll_minutes"]) * 60
    weather_every = int(settings["weather_poll_minutes"]) * 60
    sleep_seconds = max(1, int(settings["loop_sleep_seconds"]))
    last_settings_mtime: Optional[float] = None
    try:
        last_settings_mtime = settings_path.stat().st_mtime
    except Exception:  # noqa: BLE001
        last_settings_mtime = None

    def next_aligned(now: float, every: int) -> float:
        if every <= 0:
            return now
        return (math.floor(now / every) + 1) * every

    # Write an initial status snapshot immediately on startup, so the dashboard
    # is never "blank" until the next aligned boundary.
    try:
        init_ts = timestamp_iso()
        init_status: Dict[str, Any] = {"updated_at": init_ts, "service_status": "ok"}
        sensors = load_sensors(sensors_path)
        run_sensor_cycle(sensors, settings, logger, init_status, storage, cycle_ts=init_ts)
        run_weather_cycle(settings, logger, init_status, storage)
        init_status["retention_key"] = storage.get_retention_key()
        init_status["sensor_poll_minutes"] = int(settings.get("sensor_poll_minutes", 60))
        init_status["weather_poll_minutes"] = int(settings.get("weather_poll_minutes", 15))
        storage.prune_old_data(storage.retention_days())
        init_status["updated_at"] = timestamp_iso()
        persist_status(settings, init_status)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Initial status write failed: %s", exc)

    next_sensor = next_aligned(time.time(), sensor_every)
    next_weather = next_aligned(time.time(), weather_every)

    logger.info("Starting collector loop (sensor=%ss, weather=%ss)", sensor_every, weather_every)

    while True:
        now = time.time()

        # Reload settings if settings.json changed, so polling intervals can be tuned without restarting.
        try:
            mtime = settings_path.stat().st_mtime
        except Exception:  # noqa: BLE001
            mtime = None
        if mtime and mtime != last_settings_mtime:
            try:
                settings = load_settings(settings_path)
                sensor_every = int(settings["sensor_poll_minutes"]) * 60
                weather_every = int(settings["weather_poll_minutes"]) * 60
                sleep_seconds = max(1, int(settings["loop_sleep_seconds"]))
                last_settings_mtime = mtime
                next_sensor = next_aligned(time.time(), sensor_every)
                next_weather = next_aligned(time.time(), weather_every)
                logger.info(
                    "Reloaded settings (sensor=%ss, weather=%ss, loop_sleep=%ss)",
                    sensor_every,
                    weather_every,
                    sleep_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to reload settings.json, keeping previous: %s", exc)

        do_sensor = now >= next_sensor
        do_weather = now >= next_weather

        if do_sensor or do_weather:
            status: Dict[str, Any] = load_previous_status(settings)
            status["updated_at"] = timestamp_iso()
            status["service_status"] = "ok"
            try:
                if do_sensor:
                    sensors = load_sensors(sensors_path)
                    cycle_ts = timestamp_iso()
                    run_sensor_cycle(sensors, settings, logger, status, storage, cycle_ts=cycle_ts)
                    next_sensor = next_aligned(now, sensor_every)
                if do_weather:
                    run_weather_cycle(settings, logger, status, storage)
                    next_weather = next_aligned(now, weather_every)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Cycle failed: %s", exc)
                status["service_status"] = "error"
                status["fatal_error"] = str(exc)

            status["retention_key"] = storage.get_retention_key()
            status["sensor_poll_minutes"] = int(settings.get("sensor_poll_minutes", 60))
            status["weather_poll_minutes"] = int(settings.get("weather_poll_minutes", 15))
            storage.prune_old_data(storage.retention_days())
            status["updated_at"] = timestamp_iso()
            persist_status(settings, status)

        time.sleep(sleep_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Growcontrol unified collector")
    parser.add_argument("--settings", default=str(DEFAULT_SETTINGS_PATH), help="Path to settings.json")
    parser.add_argument("--sensors", default=str(DEFAULT_SENSORS_PATH), help="Path to sensors.json")
    parser.add_argument("--once", action="store_true", help="Run one collection cycle and exit")
    parser.add_argument("--sensors-only", action="store_true", help="Collect sensors only (one-shot mode)")
    parser.add_argument("--weather-only", action="store_true", help="Collect weather only (one-shot mode)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings_path = Path(args.settings)
    sensors_path = Path(args.sensors)

    settings = load_settings(settings_path)
    sensors = load_sensors(sensors_path)
    logger = setup_logging(Path(settings["log_file"]))
    storage = GrowcontrolStorage(Path(settings["database_path"]))

    if args.once:
        collect_sensors = not args.weather_only
        collect_weather = not args.sensors_only
        return run_once(sensors, settings, logger, collect_sensors, collect_weather, storage)

    if args.sensors_only or args.weather_only:
        logger.warning("--sensors-only / --weather-only are only used with --once")

    return run_loop(sensors_path, settings_path, settings, logger, storage)


if __name__ == "__main__":
    raise SystemExit(main())
