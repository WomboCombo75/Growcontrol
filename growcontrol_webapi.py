#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

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


REPO_ROOT = Path(__file__).resolve().parent
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


def _configured_sensor_ids() -> set[str]:
    try:
        data = load_sensors(SENSORS_FILE)
    except Exception:  # noqa: BLE001
        return set()
    out: set[str] = set()
    for entry in data.get("sensors") or []:
        sid = str(entry.get("id", "")).strip()
        if sid:
            out.add(sid)
    return out


def normalize_webcam_streams(raw: Any) -> tuple[List[Dict[str, Any]], List[str]]:
    """
    Returns (clean_streams, warnings) where warnings describe dropped unknown sensor_ids.
    """
    warnings: List[str] = []
    if raw is None:
        return [], warnings
    if not isinstance(raw, list):
        raise ValueError("webcam_streams must be a JSON array")
    known = _configured_sensor_ids()
    out: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"webcam_streams[{i}] must be an object")
        sid = str(item.get("id", "")).strip() or f"stream_{i + 1}"
        sid = re.sub(r"[^a-zA-Z0-9_-]", "_", sid)[:64]
        base = sid
        n = 0
        while sid in seen_ids:
            n += 1
            sid = f"{base}_{n}"[:64]
        seen_ids.add(sid)
        label = str(item.get("label", "")).strip() or sid
        url = str(item.get("stream_url", "")).strip()
        if url:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                raise ValueError(f"webcam_streams[{i}].stream_url must use http or https")
            if len(url) > 2048:
                raise ValueError("webcam stream URL is too long (max 2048 characters)")
        raw_sensors = item.get("sensor_ids", [])
        if not isinstance(raw_sensors, list):
            raw_sensors = []
        clean_sids: List[str] = []
        dropped: List[str] = []
        for x in raw_sensors:
            xs = str(x).strip()
            if not xs or xs in clean_sids:
                continue
            if known and xs not in known:
                dropped.append(xs)
                continue
            clean_sids.append(xs)
        if dropped:
            warnings.append(f"stream {sid!r}: dropped unknown sensor_ids {dropped}")
        out.append({"id": sid, "label": label, "stream_url": url, "sensor_ids": clean_sids})
    if len(out) > 24:
        raise ValueError("too many webcam streams (max 24)")
    return out, warnings


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


def normalize_api_path(handler: BaseHTTPRequestHandler) -> str:
    """Path only (no query); strip a trailing slash so routing matches reliably."""
    raw = urlparse(handler.path).path or "/"
    if len(raw) > 1 and raw.endswith("/"):
        return raw[:-1]
    return raw


def parse_sensor_id_from_path(path: str) -> str:
    """Decode sensor id from /api/sensors/{id} paths (handles %-encoded UTF-8)."""
    if not path.startswith("/api/sensors/"):
        return ""
    raw = path.split("/api/sensors/", 1)[1]
    if raw.endswith("/refresh"):
        raw = raw.rsplit("/refresh", 1)[0]
    return unquote(raw, encoding="utf-8", errors="strict").strip()


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


def read_version() -> str:
    p = REPO_ROOT / "VERSION"
    try:
        v = p.read_text(encoding="utf-8").strip()
        return v or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def update_status_path() -> Path:
    settings = {}
    try:
        settings = load_settings_file()
    except Exception:  # noqa: BLE001
        settings = {}
    output_dir = Path(str(settings.get("output_dir", "/var/www/html/growcontrol")))
    return output_dir / "update_status.json"


def read_update_status() -> Dict[str, Any]:
    p = update_status_path()
    if not p.exists():
        return {"checked_at": None, "update_available": None, "behind_count": None, "branch": None, "error": None}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"checked_at": None, "update_available": None, "behind_count": None, "branch": None, "error": "invalid update_status.json"}
        return data
    except Exception as exc:  # noqa: BLE001
        return {"checked_at": None, "update_available": None, "behind_count": None, "branch": None, "error": str(exc)}


DEFAULT_MJPG_STREAMER_ARGS = ["-i", "input_uvc.so", "-o", "output_http.so -w www -p {port}"]
DEFAULT_MJPEG_STREAM_AUTOFILL_PATH = "/?action=stream"
DEFAULT_MJPEG_HTTP_PORT = 8080


def normalize_default_http_port(raw: Any) -> int:
    if raw is None or raw == "":
        return DEFAULT_MJPEG_HTTP_PORT
    try:
        p = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("default_http_port must be an integer 1..65535") from exc
    if p < 1 or p > 65535:
        raise ValueError("default_http_port must be between 1 and 65535")
    return p


def normalize_default_stream_url_path(raw: Any) -> str:
    """
    Relative path/query used when the UI builds Stream URL after MJPEG Start.
    Must start with '/'. Typical values: '/?action=stream', '/mjpeg/stream_simple.html'
    """
    s = "" if raw is None else str(raw).strip()
    if not s:
        return DEFAULT_MJPEG_STREAM_AUTOFILL_PATH
    if not s.startswith("/"):
        raise ValueError("default_stream_url_path must start with '/'")
    if len(s) > 512:
        raise ValueError("default_stream_url_path is too long (max 512 characters)")
    if "\n" in s or "\r" in s:
        raise ValueError("default_stream_url_path must be a single line")
    return s


def _mjpg_settings_block(settings: Dict[str, Any]) -> Dict[str, Any]:
    raw = settings.get("mjpg_streamer")
    return raw if isinstance(raw, dict) else {}


def mjpeg_autofill_path_from_settings(settings: Dict[str, Any]) -> str:
    block = _mjpg_settings_block(settings)
    raw = block.get("default_stream_url_path")
    try:
        return normalize_default_stream_url_path(raw)
    except ValueError:
        return DEFAULT_MJPEG_STREAM_AUTOFILL_PATH


def mjpeg_default_http_port_from_settings(settings: Dict[str, Any]) -> int:
    block = _mjpg_settings_block(settings)
    raw = block.get("default_http_port")
    try:
        return normalize_default_http_port(raw)
    except ValueError:
        return DEFAULT_MJPEG_HTTP_PORT


def mjpg_streamer_root_from_settings_only(settings: Dict[str, Any]) -> str:
    block = _mjpg_settings_block(settings)
    for key in ("mjpg_streamer_root", "streamer_root"):
        v = block.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def resolved_mjpg_install_dir(settings: Dict[str, Any]) -> Path:
    """Directory that must contain the mjpg_streamer binary (and plugins, www/)."""
    text = mjpg_streamer_root_from_settings_only(settings)
    if not text:
        text = os.getenv("GROWCONTROL_MJPG_STREAMER_ROOT", "").strip()
    if not text:
        return REPO_ROOT
    p = Path(text).expanduser()
    if not p.is_absolute():
        p = (REPO_ROOT / p).resolve()
    else:
        p = p.resolve()
    return p


def mjpg_streamer_executable_path(settings: Dict[str, Any]) -> Path:
    return resolved_mjpg_install_dir(settings) / "mjpg_streamer"


def infer_mjpeg_http_port(url: str) -> Optional[int]:
    """Return TCP port for mjpg-streamer HTTP from a stream URL, or None if unusable."""
    try:
        u = urlparse((url or "").strip())
        if u.scheme not in ("http", "https"):
            return None
        if not (u.hostname or "").strip():
            return None
        if u.port is not None:
            p = int(u.port)
            return p if 1 <= p <= 65535 else None
        if u.scheme == "https":
            return 443
        return 8080
    except Exception:  # noqa: BLE001
        return None


def stream_url_tcp_listening(url: str, timeout: float = 0.5) -> Tuple[bool, str]:
    """
    Best-effort: TCP connect to the stream URL's host:port from this machine.
    Used so the Dashboard can show Stopped vs Live when nothing accepts that port.
    """
    raw = (url or "").strip()
    if not raw:
        return False, "empty url"
    try:
        u = urlparse(raw)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if u.scheme not in ("http", "https"):
        return False, "url must be http or https"
    host = (u.hostname or "").strip()
    if not host:
        return False, "missing host"
    if host in ("localhost", "::1"):
        host = "127.0.0.1"
    port = u.port
    if port is None:
        port = 443 if u.scheme == "https" else 8080
    if port < 1 or port > 65535:
        return False, "invalid port"
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
        return True, ""
    except OSError as exc:
        return False, str(exc)


def mjpg_streamer_argv(port: int, settings: Dict[str, Any]) -> List[str]:
    raw_block = settings.get("mjpg_streamer")
    raw_block = raw_block if isinstance(raw_block, dict) else {}
    args_in = raw_block.get("args")
    if not isinstance(args_in, list) or not args_in:
        template = DEFAULT_MJPG_STREAMER_ARGS
    else:
        template = [str(x) for x in args_in]
    pstr = str(int(port))
    return [s.replace("{port}", pstr) for s in template]


def start_mjpg_streamer_subprocess(port: int, settings: Dict[str, Any]) -> Tuple[bool, str, Optional[int]]:
    """
    Run project-root start.sh in the background with args from settings (or defaults).
    Logs append to logs/mjpg_streamer.log.
    """
    port_i = int(port)
    if port_i < 1 or port_i > 65535:
        return False, "invalid port (use 1..65535)", None
    script = REPO_ROOT / "start.sh"
    if not script.is_file():
        return False, f"start.sh not found at {script}", None
    argv_tail = mjpg_streamer_argv(port_i, settings)
    install_dir = resolved_mjpg_install_dir(settings)
    exe = install_dir / "mjpg_streamer"
    if not exe.is_file():
        return (
            False,
            (
                f"No mjpg_streamer executable at {exe}. "
                "In Options → Webcam, set “mjpg-streamer install directory” to the folder produced by "
                "`make` (it must contain mjpg_streamer, .so plugins, and www/), or add "
                "`mjpg_streamer.mjpg_streamer_root` to config/settings.json. "
                "You can also set environment variable GROWCONTROL_MJPG_STREAMER_ROOT for the growcontrol-webapi service."
            ),
            None,
        )
    env = os.environ.copy()
    env["MJPG_STREAMER_ROOT"] = str(install_dir)
    log_path = REPO_ROOT / "logs" / "mjpg_streamer.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cmd = ["/bin/sh", str(script)] + argv_tail
    log_f = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    try:
        log_f.write(f"\n[{ts}] spawn port={port_i} cmd={cmd!r}\n")
        log_f.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=False,
        )
    except OSError as exc:
        log_f.write(f"[{ts}] Popen failed: {exc}\n")
        log_f.close()
        return False, str(exc), None
    time.sleep(0.35)
    code = proc.poll()
    if code is not None:
        log_f.write(f"[{ts}] exited immediately rc={code}\n")
        log_f.flush()
        log_f.close()
        tail = ""
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-1200:]
        except Exception:  # noqa: BLE001
            pass
        msg = f"mjpg_streamer exited immediately (code {code})."
        if tail.strip():
            msg += f" Log tail: {tail.strip()[-600:]}"
        return False, msg, None
    log_f.close()
    return True, f"started (pid {proc.pid}); logs: logs/mjpg_streamer.log", proc.pid


def stop_mjpeg_on_port(port: int) -> Tuple[bool, str]:
    """
    Stop whatever is listening on this TCP port (typically mjpg_streamer's HTTP server).
    Uses fuser from the psmisc package (SIGTERM).
    """
    port_i = int(port)
    if port_i < 1 or port_i > 65535:
        return False, "invalid port (use 1..65535)"
    fuser = shutil.which("fuser")
    if not fuser:
        return (
            False,
            "fuser not found on PATH (try: sudo apt install psmisc). Stop mjpg_streamer manually on the Pi.",
        )
    log_path = REPO_ROOT / "logs" / "mjpg_streamer.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(log_path, "a", encoding="utf-8") as log_handle:
            log_handle.write(f"\n[{ts}] fuser stop TCP port {port_i}\n")
    except OSError:
        pass
    try:
        result = subprocess.run(
            [fuser, "-k", "-TERM", f"{port_i}/tcp"],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"stop timed out while signaling port {port_i}"
    combined = f"{result.stdout or ''}{result.stderr or ''}".strip()
    # fuser: 0 = signaled at least one process; 1 = no processes matched
    if result.returncode == 0:
        msg = f"Sent SIGTERM to process(es) on TCP port {port_i}."
        if combined:
            msg += f" ({combined})"
        return True, msg
    if result.returncode == 1:
        return True, f"No process was using TCP port {port_i}."
    err = combined or f"exit code {result.returncode}"
    return False, f"Could not stop listeners on port {port_i}: {err}"


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

        if parsed.path == "/api/version":
            json_response(
                self,
                200,
                {
                    "version": read_version(),
                    "update": read_update_status(),
                },
            )
            return

        if parsed.path in ("/api/mjpg/listening", "/api/stream/listening"):
            query = parse_qs(parsed.query, keep_blank_values=False)
            surl = str(query.get("stream_url", [""])[0]).strip()
            if not surl:
                json_response(self, 400, {"error": "missing query param: stream_url", "listening": False})
                return
            ok, err = stream_url_tcp_listening(surl)
            json_response(
                self,
                200,
                {
                    "stream_url": surl,
                    "listening": ok,
                    "detail": err or None,
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

        if parsed.path == "/api/settings/webcams":
            try:
                settings = load_settings_file()
                streams = settings.get("webcam_streams") or []
                if not isinstance(streams, list):
                    streams = []
                json_response(self, 200, {"webcam_streams": streams})
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/settings/mjpg-streamer":
            try:
                settings = load_settings_file()
                install_dir = resolved_mjpg_install_dir(settings)
                exe = mjpg_streamer_executable_path(settings)
                saved = mjpg_streamer_root_from_settings_only(settings)
                from_env = bool(os.getenv("GROWCONTROL_MJPG_STREAMER_ROOT", "").strip())
                block = _mjpg_settings_block(settings)
                args_out = block.get("args")
                if not isinstance(args_out, list):
                    args_out = None
                json_response(
                    self,
                    200,
                    {
                        "mjpg_streamer_root_saved": saved,
                        "mjpg_streamer_root_from_env": from_env,
                        "resolved_install_dir": str(install_dir),
                        "mjpg_streamer_found": exe.is_file(),
                        "args": args_out,
                        "default_stream_url_path": mjpeg_autofill_path_from_settings(settings),
                        "default_http_port": mjpeg_default_http_port_from_settings(settings),
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

        path = normalize_api_path(self)

        if path == "/api/scan":
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

        if path == "/api/verify":
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

        if path == "/api/settings/retention":
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

        if path == "/api/settings/polling":
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

        if path == "/api/settings/weather":
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

        if path == "/api/settings/webcams":
            try:
                settings = load_settings_file()
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": f"failed to load settings: {exc}"})
                return
            raw_streams = body.get("webcam_streams")
            try:
                clean, warnings = normalize_webcam_streams(raw_streams)
            except ValueError as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            settings["webcam_streams"] = clean
            try:
                write_settings_file(settings)
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
                return
            json_response(
                self,
                200,
                {
                    "success": True,
                    "webcam_streams": clean,
                    "warnings": warnings,
                    "note": "Dashboard loads streams in the browser; use http(s) reachable from your viewer device (often http://<pi-ip>:PORT/… not localhost elsewhere). *.html URLs show in iframe; MJPEG URLs go in <img>.",
                },
            )
            return

        if path == "/api/settings/mjpg-streamer":
            try:
                settings = load_settings_file()
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": f"failed to load settings: {exc}"})
                return
            block = dict(_mjpg_settings_block(settings))
            if "mjpg_streamer_root" in body:
                r = body.get("mjpg_streamer_root")
                block["mjpg_streamer_root"] = str(r).strip() if r is not None else ""
            if "args" in body:
                a = body.get("args")
                if a is None:
                    block.pop("args", None)
                elif isinstance(a, list) and all(isinstance(x, (str, int, float)) for x in a):
                    block["args"] = [str(x) for x in a][:48]
                else:
                    json_response(self, 400, {"error": "args must be a JSON array of strings or null"})
                    return
            if "default_stream_url_path" in body:
                try:
                    block["default_stream_url_path"] = normalize_default_stream_url_path(body.get("default_stream_url_path"))
                except ValueError as exc:
                    json_response(self, 400, {"error": str(exc)})
                    return
            if "default_http_port" in body:
                try:
                    block["default_http_port"] = normalize_default_http_port(body.get("default_http_port"))
                except ValueError as exc:
                    json_response(self, 400, {"error": str(exc)})
                    return
            settings["mjpg_streamer"] = block
            try:
                write_settings_file(settings)
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
                return
            install_dir = resolved_mjpg_install_dir(settings)
            exe = install_dir / "mjpg_streamer"
            json_response(
                self,
                200,
                {
                    "success": True,
                    "mjpg_streamer": block,
                    "mjpg_streamer_root_saved": block.get("mjpg_streamer_root", ""),
                    "resolved_install_dir": str(install_dir),
                    "mjpg_streamer_found": exe.is_file(),
                    "default_stream_url_path": mjpeg_autofill_path_from_settings(settings),
                    "default_http_port": mjpeg_default_http_port_from_settings(settings),
                },
            )
            return

        if path == "/api/data/clear":
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

        if path == "/api/sensors":
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

            if not sensor_id:
                json_response(self, 400, {"error": "sensor id is required"})
                return
            if not name:
                json_response(self, 400, {"error": "sensor name is required"})
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

        if path.startswith("/api/sensors/") and path.endswith("/refresh"):
            sensor_id = parse_sensor_id_from_path(path)
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

        # Prefer POST /api/mjpg + JSON {"run":"start"|"stop","port":N} — avoids proxies/WAFs that block "/stop" in the path.
        if path == "/api/mjpg":
            try:
                port = int(body.get("port"))
            except (TypeError, ValueError):
                json_response(self, 400, {"error": "missing or invalid port (integer 1..65535)"})
                return
            run = str(body.get("run", "")).strip().lower()
            if run == "start":
                try:
                    settings = load_settings_file()
                except Exception as exc:  # noqa: BLE001
                    json_response(self, 500, {"error": f"failed to load settings: {exc}"})
                    return
                ok, msg, pid = start_mjpg_streamer_subprocess(port, settings)
                if ok:
                    json_response(
                        self,
                        200,
                        {
                            "success": True,
                            "message": msg,
                            "port": port,
                            "pid": pid,
                            "default_stream_url_path": mjpeg_autofill_path_from_settings(settings),
                            "default_http_port": mjpeg_default_http_port_from_settings(settings),
                        },
                    )
                else:
                    json_response(self, 500, {"success": False, "error": msg, "port": port})
                return
            if run == "stop":
                ok, msg = stop_mjpeg_on_port(port)
                if ok:
                    json_response(self, 200, {"success": True, "message": msg, "port": port})
                else:
                    json_response(self, 500, {"success": False, "error": msg, "port": port})
                return
            json_response(self, 400, {"error": 'invalid run; use "start" or "stop"'})
            return

        if path in ("/api/mjpg-streamer/start", "/api/mjpg/start"):
            try:
                settings = load_settings_file()
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": f"failed to load settings: {exc}"})
                return
            try:
                port = int(body.get("port"))
            except (TypeError, ValueError):
                json_response(self, 400, {"error": "missing or invalid port (integer 1..65535)"})
                return
            ok, msg, pid = start_mjpg_streamer_subprocess(port, settings)
            if ok:
                json_response(
                    self,
                    200,
                    {
                        "success": True,
                        "message": msg,
                        "port": port,
                        "pid": pid,
                        "default_stream_url_path": mjpeg_autofill_path_from_settings(settings),
                        "default_http_port": mjpeg_default_http_port_from_settings(settings),
                    },
                )
            else:
                json_response(self, 500, {"success": False, "error": msg, "port": port})
            return

        if path in ("/api/mjpg-streamer/stop", "/api/mjpg/stop"):
            try:
                port = int(body.get("port"))
            except (TypeError, ValueError):
                json_response(self, 400, {"error": "missing or invalid port (integer 1..65535)"})
                return
            ok, msg = stop_mjpeg_on_port(port)
            if ok:
                json_response(self, 200, {"success": True, "message": msg, "port": port})
            else:
                json_response(self, 500, {"success": False, "error": msg, "port": port})
            return

        if path == "/api/restart":
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

        sensor_id = parse_sensor_id_from_path(parsed.path)
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

        sensor_id = parse_sensor_id_from_path(parsed.path)
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
