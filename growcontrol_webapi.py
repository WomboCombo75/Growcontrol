#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from btlewrap.bluepy import BluepyBackend
from miflora.miflora_poller import (
    MI_BATTERY,
    MI_CONDUCTIVITY,
    MI_LIGHT,
    MI_MOISTURE,
    MI_TEMPERATURE,
    MiFloraPoller,
)

from growcontrol_cli import (
    add_sensor,
    discover_ble_devices_detailed,
    edit_sensor,
    load_sensors,
    remove_sensor,
    validate_mac,
)
from growcontrol_storage import GrowcontrolStorage, RETENTION_CHOICES


SENSORS_FILE = Path(os.getenv("GROWCONTROL_SENSORS_FILE", "config/sensors.json"))
SETTINGS_FILE = Path(os.getenv("GROWCONTROL_SETTINGS_FILE", "config/settings.json"))
DB_FILE = Path(os.getenv("GROWCONTROL_DB_FILE", "data/growcontrol.db"))
COLLECTOR_SERVICE = os.getenv("GROWCONTROL_COLLECTOR_SERVICE", "growcontrol-collector.service")
BIND_HOST = os.getenv("GROWCONTROL_WEBAPI_HOST", "127.0.0.1")
BIND_PORT = int(os.getenv("GROWCONTROL_WEBAPI_PORT", "8788"))
API_KEY = os.getenv("GROWCONTROL_API_KEY", "").strip()
STORAGE = GrowcontrolStorage(DB_FILE)

def load_settings_file() -> Dict[str, Any]:
    with SETTINGS_FILE.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_settings_file(settings: Dict[str, Any]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


def parse_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    if not raw.strip():
        return {}
    return json.loads(raw)


def restart_collector() -> None:
    result = subprocess.run(
        ["systemctl", "restart", COLLECTOR_SERVICE],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to restart {COLLECTOR_SERVICE}: "
            f"{result.stdout.strip()} {result.stderr.strip()}"
        )


def service_state(service_name: str) -> str:
    result = subprocess.run(
        ["systemctl", "is-active", service_name],
        capture_output=True,
        text=True,
        check=False,
    )
    out = (result.stdout or result.stderr).strip()
    return out or "unknown"


def probe_sensor(mac: str, timeout_seconds: int) -> Dict[str, Any]:
    poller = MiFloraPoller(mac, BluepyBackend, cache_timeout=timeout_seconds)
    return {
        "firmware": poller.firmware_version(),
        "device_name": poller.name(),
        "temperature": poller.parameter_value(MI_TEMPERATURE),
        "moisture": poller.parameter_value(MI_MOISTURE),
        "light": poller.parameter_value(MI_LIGHT),
        "conductivity": poller.parameter_value(MI_CONDUCTIVITY),
        "battery": poller.parameter_value(MI_BATTERY),
    }


def timestamp_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_status_file() -> Dict[str, Any]:
    settings = load_settings_file()
    output_dir = Path(str(settings.get("output_dir", "/var/www/html/growcontrol")))
    status_file = str(settings.get("status_file", "status.json"))
    path = output_dir / status_file
    if not path.exists():
        return {"updated_at": timestamp_iso(), "service_status": "ok", "sensors": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"updated_at": timestamp_iso(), "service_status": "ok", "sensors": {}}


def write_status_file(status: Dict[str, Any]) -> None:
    settings = load_settings_file()
    output_dir = Path(str(settings.get("output_dir", "/var/www/html/growcontrol")))
    status_file = str(settings.get("status_file", "status.json"))
    path = output_dir / status_file
    output_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")


def authorized_write(handler: BaseHTTPRequestHandler) -> bool:
    """
    Write operations require an API key if configured.
    Read operations are intentionally open because the dashboard is local-first
    and nginx already scopes access to LAN by default setups.
    """
    if not API_KEY:
        return True
    return handler.headers.get("X-API-Key", "").strip() == API_KEY


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:  # noqa: N802
        json_response(self, 204, {})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/api/health":
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "collector_service": COLLECTOR_SERVICE,
                    "collector_state": service_state(COLLECTOR_SERVICE),
                    "requires_api_key": bool(API_KEY),
                },
            )
            return

        if parsed.path == "/api/sensors":
            try:
                data = load_sensors(SENSORS_FILE)
                json_response(self, 200, data)
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            sensor_id = str(query.get("sensor_id", [""])[0]).strip()
            limit = int(query.get("limit", ["1000"])[0])
            range_key = str(query.get("range", [""])[0]).strip()
            if not sensor_id:
                json_response(self, 400, {"error": "missing query param: sensor_id"})
                return
            try:
                since_days = None
                since_hours = None
                if range_key == "24h":
                    since_hours = 24
                elif range_key == "7d":
                    since_days = 7
                elif range_key == "30d":
                    since_days = 30
                elif range_key == "1y":
                    since_days = 365
                elif range_key in ("all", ""):
                    pass
                else:
                    # Custom ranges:
                    # - h12 => last 12 hours
                    # - d14 => last 14 days
                    m = re.fullmatch(r"([hd])(\d{1,4})", range_key)
                    if m:
                        unit = m.group(1)
                        value = int(m.group(2))
                        if unit == "h":
                            if value < 1 or value > 24 * 365:
                                json_response(self, 400, {"error": "invalid range hours (h1..h8760)"})
                                return
                            since_hours = value
                        else:
                            if value < 1 or value > 3650:
                                json_response(self, 400, {"error": "invalid range days (d1..d3650)"})
                                return
                            since_days = value
                    else:
                        json_response(
                            self,
                            400,
                            {
                                "error": "invalid range",
                                "choices": ["24h", "7d", "30d", "1y", "all", "h12", "d14"],
                            },
                        )
                        return

                data = STORAGE.get_sensor_history(
                    sensor_id=sensor_id,
                    limit=limit,
                    since_days=since_days,
                    since_hours=since_hours,
                )
                json_response(self, 200, {"sensor_id": sensor_id, "range": range_key or "all", "readings": data})
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/settings/retention":
            json_response(
                self,
                200,
                {
                    "retention_key": STORAGE.get_retention_key(),
                    "choices": list(RETENTION_CHOICES.keys()),
                },
            )
            return

        if parsed.path == "/api/settings/polling":
            try:
                settings = load_settings_file()
                json_response(
                    self,
                    200,
                    {
                        "sensor_poll_minutes": int(settings.get("sensor_poll_minutes", 60)),
                        "sensor_parallelism": int(settings.get("sensor_parallelism", 2)),
                        "weather_poll_minutes": int(settings.get("weather_poll_minutes", 15)),
                        "loop_sleep_seconds": int(settings.get("loop_sleep_seconds", 5)),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/settings/weather":
            try:
                settings = load_settings_file()
                json_response(
                    self,
                    200,
                    {
                        "weather_enabled": bool(settings.get("weather_enabled", False)),
                        "weather_lat": float(settings.get("weather_lat", 0.0)),
                        "weather_lon": float(settings.get("weather_lon", 0.0)),
                        "weather_units": str(settings.get("weather_units", "metric")),
                        "openweather_api_key_set": bool(str(settings.get("openweather_api_key", "")).strip()),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        json_response(self, 404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not authorized_write(self):
            json_response(self, 401, {"error": "unauthorized"})
            return

        try:
            body = parse_body(self)
        except Exception as exc:  # noqa: BLE001
            json_response(self, 400, {"error": f"invalid json: {exc}"})
            return

        if self.path == "/api/scan":
            timeout = int(body.get("timeout", 20))
            timeout = max(3, min(timeout, 30))
            try:
                devices = discover_ble_devices_detailed(timeout)
                json_response(
                    self,
                    200,
                    {
                        "devices": [
                            {
                                "mac": str(device.get("mac")),
                                "name": str(device.get("name")),
                                "is_likely_flora": bool(device.get("is_likely_flora", False)),
                                "source_hint": str(device.get("source_hint", "")),
                            }
                            for device in devices
                        ]
                    },
                )
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if self.path == "/api/verify":
            try:
                mac = validate_mac(str(body["mac"]))
                timeout = int(body.get("timeout", 20))
                timeout = max(5, min(timeout, 45))
            except KeyError as exc:
                json_response(self, 400, {"error": f"missing field: {exc}"})
                return
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"error": str(exc)})
                return

            try:
                data = probe_sensor(mac, timeout)
                json_response(self, 200, {"success": True, "mac": mac, "probe": data})
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if self.path == "/api/settings/retention":
            retention_key = str(body.get("retention_key", "")).strip()
            if retention_key not in RETENTION_CHOICES:
                json_response(
                    self,
                    400,
                    {
                        "error": "invalid retention_key",
                        "choices": list(RETENTION_CHOICES.keys()),
                    },
                )
                return
            try:
                STORAGE.set_retention_key(retention_key)
                STORAGE.prune_old_data(RETENTION_CHOICES[retention_key])
                json_response(self, 200, {"success": True, "retention_key": retention_key})
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if self.path == "/api/settings/polling":
            try:
                settings = load_settings_file()
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": f"failed to load settings: {exc}"})
                return

            def clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
                try:
                    n = int(value)
                except Exception:  # noqa: BLE001
                    n = default
                return max(lo, min(hi, n))

            sensor_poll_minutes = clamp_int(body.get("sensor_poll_minutes", settings.get("sensor_poll_minutes", 60)), 60, 5, 24 * 60)
            sensor_parallelism = clamp_int(body.get("sensor_parallelism", settings.get("sensor_parallelism", 2)), 2, 1, 6)
            # Keep weather polling at 15 minutes (battery/traffic friendly default).
            # This is intentionally not exposed in the UI to reduce confusion.
            weather_poll_minutes = 15

            settings["sensor_poll_minutes"] = sensor_poll_minutes
            settings["sensor_parallelism"] = sensor_parallelism
            settings["weather_poll_minutes"] = weather_poll_minutes
            try:
                write_settings_file(settings)
                json_response(
                    self,
                    200,
                    {
                        "success": True,
                        "sensor_poll_minutes": sensor_poll_minutes,
                        "sensor_parallelism": sensor_parallelism,
                        "weather_poll_minutes": weather_poll_minutes,
                        "note": "Collector auto-reloads settings.json when it changes.",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if self.path == "/api/settings/weather":
            try:
                settings = load_settings_file()
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": f"failed to load settings: {exc}"})
                return

            def clamp_float(value: Any, default: float, lo: float, hi: float) -> float:
                try:
                    n = float(value)
                except Exception:  # noqa: BLE001
                    n = default
                return max(lo, min(hi, n))

            weather_enabled = bool(body.get("weather_enabled", settings.get("weather_enabled", False)))
            units = str(body.get("weather_units", settings.get("weather_units", "metric"))).strip().lower()
            if units not in ("metric", "imperial", "standard"):
                json_response(self, 400, {"error": "invalid weather_units", "choices": ["metric", "imperial", "standard"]})
                return

            lat = clamp_float(body.get("weather_lat", settings.get("weather_lat", 0.0)), 0.0, -90.0, 90.0)
            lon = clamp_float(body.get("weather_lon", settings.get("weather_lon", 0.0)), 0.0, -180.0, 180.0)
            api_key = str(body.get("openweather_api_key", "")).strip()

            settings["weather_enabled"] = weather_enabled
            settings["weather_lat"] = lat
            settings["weather_lon"] = lon
            settings["weather_units"] = units
            if api_key:
                settings["openweather_api_key"] = api_key

            try:
                write_settings_file(settings)
                json_response(
                    self,
                    200,
                    {
                        "success": True,
                        "weather_enabled": weather_enabled,
                        "weather_lat": lat,
                        "weather_lon": lon,
                        "weather_units": units,
                        "openweather_api_key_set": bool(str(settings.get("openweather_api_key", "")).strip()),
                        "note": "Collector auto-reloads settings.json when it changes.",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if self.path == "/api/data/clear":
            confirm = str(body.get("confirm_text", "")).strip()
            second_confirm = bool(body.get("are_you_sure", False))
            if not second_confirm or confirm != "DELETE_ALL_DATA":
                json_response(
                    self,
                    400,
                    {"error": "confirmation failed (require are_you_sure=true and confirm_text=DELETE_ALL_DATA)"},
                )
                return
            try:
                STORAGE.clear_all_data()
                json_response(self, 200, {"success": True})
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if self.path == "/api/sensors":
            try:
                sensor_id = str(body["id"]).strip()
                name = str(body["name"]).strip()
                mac = validate_mac(str(body["mac"]))
                output_file = body.get("output_file")
                enabled = bool(body.get("enabled", True))
                restart = bool(body.get("restart", False))
            except KeyError as exc:
                json_response(self, 400, {"error": f"missing field: {exc}"})
                return
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"error": str(exc)})
                return

            try:
                add_sensor(
                    sensors_path=SENSORS_FILE,
                    sensor_id=sensor_id,
                    name=name,
                    mac=mac,
                    output_file=output_file,
                    enabled=enabled,
                )
                restart_error = None
                if restart:
                    try:
                        restart_collector()
                    except Exception as exc:  # noqa: BLE001
                        restart_error = str(exc)
                json_response(self, 200, {"success": True, "restart_error": restart_error})
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/sensors/") and parsed.path.endswith("/refresh"):
            sensor_id = parsed.path.split("/api/sensors/", 1)[1].rsplit("/refresh", 1)[0]
            if not sensor_id:
                json_response(self, 400, {"error": "missing sensor id"})
                return

            timeout = int(body.get("timeout", 20))
            timeout = max(5, min(timeout, 45))
            try:
                sensors_doc = load_sensors(SENSORS_FILE)
                sensor = next((s for s in sensors_doc.get("sensors", []) if str(s.get("id")) == sensor_id), None)
                if not sensor:
                    json_response(self, 404, {"error": "sensor not found"})
                    return
                mac = validate_mac(str(sensor.get("mac", "")))
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"error": str(exc)})
                return

            status = load_status_file()
            sensors_state = status.setdefault("sensors", {})
            sensor_state = sensors_state.setdefault(sensor_id, {})
            sensor_state["last_attempt"] = timestamp_iso()
            sensor_state["mac"] = mac
            sensor_state["name"] = str(sensor.get("name", sensor_id))
            sensor_state["enabled"] = bool(sensor.get("enabled", True))

            try:
                probe = probe_sensor(mac, timeout)
                metrics = {
                    "temperature": probe.get("temperature"),
                    "moisture": probe.get("moisture"),
                    "light": probe.get("light"),
                    "conductivity": probe.get("conductivity"),
                    "battery": probe.get("battery"),
                }
                sensor_state["status"] = "ok"
                sensor_state["last_error"] = None
                sensor_state["last_success"] = timestamp_iso()
                sensor_state["last_metrics"] = metrics
                status["updated_at"] = timestamp_iso()
                write_status_file(status)

                STORAGE.insert_sensor_reading(
                    ts=timestamp_iso(),
                    sensor_id=sensor_id,
                    status="ok",
                    metrics=metrics,
                    error=None,
                )
                json_response(self, 200, {"success": True, "sensor_id": sensor_id, "probe": probe})
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                sensor_state["status"] = "error"
                sensor_state["last_error"] = err
                status["updated_at"] = timestamp_iso()
                write_status_file(status)
                STORAGE.insert_sensor_reading(
                    ts=timestamp_iso(),
                    sensor_id=sensor_id,
                    status="error",
                    metrics=None,
                    error=err,
                )
                json_response(self, 500, {"error": err})
            return

        if self.path == "/api/restart":
            try:
                restart_collector()
                json_response(self, 200, {"success": True})
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        json_response(self, 404, {"error": "not found"})

    def do_PATCH(self) -> None:  # noqa: N802
        if not authorized_write(self):
            json_response(self, 401, {"error": "unauthorized"})
            return

        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/sensors/"):
            json_response(self, 404, {"error": "not found"})
            return

        sensor_id = parsed.path.split("/api/sensors/", 1)[1]
        if not sensor_id:
            json_response(self, 400, {"error": "missing sensor id"})
            return

        try:
            body = parse_body(self)
            sensor = edit_sensor(
                sensors_path=SENSORS_FILE,
                sensor_id=sensor_id,
                new_id=body.get("new_id"),
                name=body.get("name"),
                mac=validate_mac(str(body["mac"])) if body.get("mac") else None,
                output_file=body.get("output_file"),
                enabled=body.get("enabled"),
            )
            restart_error = None
            if bool(body.get("restart", False)):
                try:
                    restart_collector()
                except Exception as exc:  # noqa: BLE001
                    restart_error = str(exc)
            json_response(self, 200, {"success": True, "sensor": sensor, "restart_error": restart_error})
        except Exception as exc:  # noqa: BLE001
            json_response(self, 500, {"error": str(exc)})

    def do_DELETE(self) -> None:  # noqa: N802
        if not authorized_write(self):
            json_response(self, 401, {"error": "unauthorized"})
            return

        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/sensors/"):
            json_response(self, 404, {"error": "not found"})
            return

        sensor_id = parsed.path.split("/api/sensors/", 1)[1]
        if not sensor_id:
            json_response(self, 400, {"error": "missing sensor id"})
            return

        query = parse_qs(parsed.query)
        restart = query.get("restart", ["0"])[0] != "0"
        try:
            removed = remove_sensor(SENSORS_FILE, sensor_id)
            restart_error = None
            if restart:
                try:
                    restart_collector()
                except Exception as exc:  # noqa: BLE001
                    restart_error = str(exc)
            json_response(self, 200, {"success": True, "removed": removed, "restart_error": restart_error})
        except Exception as exc:  # noqa: BLE001
            json_response(self, 500, {"error": str(exc)})

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep API quiet in stdout logs.
        return


def main() -> int:
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    print(f"Growcontrol web API listening on {BIND_HOST}:{BIND_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
