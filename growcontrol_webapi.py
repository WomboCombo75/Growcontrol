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
from growcontrol_env import (
    migrate_openweather_key_from_settings,
    openweather_api_key_configured,
    set_openweather_api_key,
)
from growcontrol_webcam import (
    normalize_stream_source,
    parse_webcam_stream_url,
    probe_external_webcam_url,
    validate_webcam_stream_entry,
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
DEFAULT_FEATURE_CONTAINERS: Dict[str, Dict[str, bool]] = {
    "webcam": {"installed": True},
    "notifications": {"installed": False},
}


def normalize_feature_containers(raw: Any) -> Dict[str, Dict[str, bool]]:
    src = raw if isinstance(raw, dict) else {}
    out: Dict[str, Dict[str, bool]] = {}
    for name, defaults in DEFAULT_FEATURE_CONTAINERS.items():
        entry = src.get(name)
        if isinstance(entry, dict):
            installed = bool(entry.get("installed", defaults["installed"]))
        else:
            installed = bool(defaults["installed"])
        out[name] = {"installed": installed}
    return out


def ensure_feature_containers_in_settings(settings: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
    current = settings.get("feature_containers")
    normalized = normalize_feature_containers(current)
    changed = normalized != current
    settings["feature_containers"] = normalized
    return settings, changed


def container_is_installed(settings: Dict[str, Any], container_name: str) -> bool:
    containers = normalize_feature_containers(settings.get("feature_containers"))
    entry = containers.get(container_name) or {}
    return bool(entry.get("installed", False))

def load_settings_file() -> Dict[str, Any]:
    with SETTINGS_FILE.open("r", encoding="utf-8") as handle:
        settings = json.load(handle)
    settings, feature_changed = ensure_feature_containers_in_settings(settings)
    settings, migrated = migrate_openweather_key_from_settings(settings)
    if feature_changed or migrated:
        write_settings_file(settings)
    return settings


def write_settings_file(settings: Dict[str, Any]) -> None:
    settings = dict(settings)
    settings, _ = ensure_feature_containers_in_settings(settings)
    migrate_openweather_key_from_settings(settings)
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
        source = normalize_stream_source(item.get("source"))
        if url or source == "external":
            try:
                normalized_item, entry_warnings = validate_webcam_stream_entry(
                    {"id": sid, "label": label, "stream_url": url, "source": source, "sensor_ids": item.get("sensor_ids", [])},
                    index=i,
                    probe_external=True,
                )
                url = str(normalized_item.get("stream_url", "")).strip()
                source = str(normalized_item.get("source", "builtin"))
                warnings.extend(entry_warnings)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
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
        out.append({"id": sid, "label": label, "stream_url": url, "source": source, "sensor_ids": clean_sids})
    if len(out) > 24:
        raise ValueError("too many webcam streams (max 24)")
    return out, warnings


def _cors_allow_origin(handler: BaseHTTPRequestHandler) -> str:
    """
    Reflect Origin only for same-host browser requests (Dashboard/Options via nginx).
    Omit CORS for cross-site origins so LAN clients cannot be read by arbitrary websites.
    """
    origin = (handler.headers.get("Origin") or "").strip()
    if not origin:
        return ""
    host_header = (handler.headers.get("Host") or "").strip()
    if not host_header:
        return ""
    try:
        o = urlparse(origin)
        origin_host = (o.hostname or "").lower()
        req_host = host_header.split(":")[0].lower()
    except Exception:  # noqa: BLE001
        return ""
    if not origin_host or not req_host:
        return ""
    loopback = frozenset({"localhost", "127.0.0.1", "::1"})
    if origin_host == req_host or (origin_host in loopback and req_host in loopback):
        return origin
    return ""


def apply_cors_headers(handler: BaseHTTPRequestHandler) -> None:
    allowed = _cors_allow_origin(handler)
    if not allowed:
        return
    handler.send_header("Access-Control-Allow-Origin", allowed)
    handler.send_header("Vary", "Origin")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    apply_cors_headers(handler)
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
    cmd = ["systemctl", "restart", COLLECTOR_SERVICE]
    attempts: List[List[str]] = [["sudo", "-n", *cmd], cmd]
    last_err = ""
    for argv in attempts:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return
        last_err = f"{result.stdout.strip()} {result.stderr.strip()}".strip()
    raise RuntimeError(
        f"Failed to restart {COLLECTOR_SERVICE}: {last_err}. "
        "Re-run install_phase1.sh to install /etc/sudoers.d/growcontrol-collector-restart."
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


DEFAULT_MJPEG_STREAM_AUTOFILL_PATH = "/?action=stream"
DEFAULT_MJPEG_HTTP_PORT = 8080
DEFAULT_MJPG_CAMERA: Dict[str, Any] = {
    "device": "/dev/video0",
    "resolution": "1280x720",
    "fps": 15,
    "quality": 85,
    "auto_exposure": "manual",
    "exposure_time": "800",
    "gain": "40",
    "white_balance_automatic": "1",
    "brightness": "",
    "contrast": "",
    "sharpness": "",
    "saturation": "",
}
MJPG_NAMED_RESOLUTIONS = frozenset(
    {"QSIF", "QCIF", "CGA", "QVGA", "CIF", "VGA", "SVGA", "XGA", "SXGA"}
)
MJPG_CAMERA_TUNING_KEYS = ("brightness", "contrast", "sharpness", "saturation")
MJPG_CAMERA_CLAMP_KEYS = MJPG_CAMERA_TUNING_KEYS + ("gain", "exposure_time")
V4L2_INT_CTRL_RE = re.compile(
    r"^\s*(brightness|contrast|saturation|sharpness|gain|exposure_time_absolute)\b.*?min=(-?\d+)\s+max=(-?\d+)",
    re.IGNORECASE,
)
V4L2_AUTO_EXPOSURE_ALIASES = {
    "manual": "1",
    "1": "1",
    "aperture-priority": "3",
    "aperture_priority": "3",
    "aperature-priority": "3",
    "3": "3",
    "shutter-priority": "2",
    "shutter_priority": "2",
    "2": "2",
    "auto": "0",
    "0": "0",
}


def query_v4l2_controls(device: str) -> Dict[str, Dict[str, int]]:
    """Return min/max for supported UVC controls (requires v4l2-ctl from v4l-utils)."""
    dev = str(device or "").strip()
    if not re.fullmatch(r"/dev/video[\w]+", dev):
        return {}
    if not shutil.which("v4l2-ctl"):
        return {}
    try:
        proc = subprocess.run(
            ["v4l2-ctl", "-d", dev, "--list-ctrls"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    out: Dict[str, Dict[str, int]] = {}
    for line in proc.stdout.splitlines():
        match = V4L2_INT_CTRL_RE.search(line)
        if not match:
            continue
        name = match.group(1).lower()
        name = match.group(1).lower()
        out[name] = {"min": int(match.group(2)), "max": int(match.group(3))}
    if "exposure_time_absolute" in out:
        out["exposure_time"] = dict(out["exposure_time_absolute"])
    return out


def read_v4l2_control_values(device: str, names: List[str]) -> Dict[str, str]:
    dev = str(device or "").strip()
    if not re.fullmatch(r"/dev/video[\w]+", dev) or not names:
        return {}
    if not shutil.which("v4l2-ctl"):
        return {}
    try:
        proc = subprocess.run(
            ["v4l2-ctl", "-d", dev, "-C", ",".join(names)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    out: Dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        if key == "exposure_time_absolute":
            out["exposure_time"] = val.split()[0]
        out[key] = val.split()[0] if val else ""
    return out


def _normalize_auto_exposure_value(raw: Any) -> str:
    s = str(raw or "manual").strip().lower()
    if not s:
        return "manual"
    if s in V4L2_AUTO_EXPOSURE_ALIASES:
        mapped = V4L2_AUTO_EXPOSURE_ALIASES[s]
        if mapped == "1":
            return "manual"
        if mapped == "3":
            return "aperture-priority"
        if mapped == "2":
            return "shutter-priority"
        if mapped == "0":
            return "auto"
    raise ValueError("camera.auto_exposure must be manual, aperture-priority, shutter-priority, or auto")


def _auto_exposure_v4l2_value(camera: Dict[str, Any]) -> Optional[str]:
    ae = _normalize_auto_exposure_value(camera.get("auto_exposure", "manual"))
    return V4L2_AUTO_EXPOSURE_ALIASES.get(ae)


def apply_v4l2_clamps(camera: Dict[str, Any], controls: Dict[str, Dict[str, int]]) -> Tuple[Dict[str, Any], List[str]]:
    if not controls:
        return camera, []
    out = dict(camera)
    notes: List[str] = []
    for key in MJPG_CAMERA_CLAMP_KEYS:
        val = str(out.get(key, "") or "").strip()
        if not val or val.lower() == "auto":
            continue
        spec = controls.get(key)
        if not spec:
            continue
        try:
            n = int(val)
        except ValueError:
            continue
        lo, hi = int(spec["min"]), int(spec["max"])
        clamped = max(lo, min(hi, n))
        if clamped != n:
            notes.append(f"{key} adjusted from {n} to {clamped} (camera allows {lo}..{hi})")
            out[key] = str(clamped)
    return out, notes


def apply_v4l2_camera_live(device: str, camera: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Push tuning to the camera via v4l2-ctl while the stream is running."""
    dev = str(device or "").strip()
    if not re.fullmatch(r"/dev/video[\w]+", dev):
        return [], ["invalid device path"]
    if not shutil.which("v4l2-ctl"):
        return [], ["v4l2-ctl not installed (try: sudo apt install v4l-utils)"]

    controls = query_v4l2_controls(dev)
    camera, _ = apply_v4l2_clamps(dict(camera), controls)
    applied: List[str] = []
    errors: List[str] = []

    def set_ctrl(v4l_name: str, raw_val: str) -> None:
        val = str(raw_val).strip()
        if not val:
            return
        try:
            proc = subprocess.run(
                ["v4l2-ctl", "-d", dev, f"--set-ctrl={v4l_name}={val}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{v4l_name}: {exc}")
            return
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            errors.append(f"{v4l_name}: {detail or 'set failed'}")
            return
        applied.append(f"{v4l_name}={val}")

    wb = str(camera.get("white_balance_automatic", "") or "").strip()
    if wb in ("0", "1"):
        set_ctrl("white_balance_automatic", wb)

    ae_val = _auto_exposure_v4l2_value(camera)
    if ae_val is not None:
        set_ctrl("auto_exposure", ae_val)

    ae_mode = _normalize_auto_exposure_value(camera.get("auto_exposure", "manual"))
    exp = str(camera.get("exposure_time", "") or "").strip()
    if exp and ae_mode in ("manual",):
        set_ctrl("exposure_time_absolute", exp)

    gain = str(camera.get("gain", "") or "").strip()
    if gain and gain.lower() != "auto":
        set_ctrl("gain", gain)

    for key in MJPG_CAMERA_TUNING_KEYS:
        val = str(camera.get(key, "") or "").strip()
        if not val:
            continue
        if key == "brightness" and val.lower() == "auto":
            set_ctrl("auto_brightness", "1")
            continue
        set_ctrl(key, val)

    return applied, errors


def merge_camera_into_settings(settings: Dict[str, Any], camera_raw: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    block = dict(_mjpg_settings_block(settings))
    camera = normalize_mjpg_camera(camera_raw)
    controls = query_v4l2_controls(camera["device"])
    camera, notes = apply_v4l2_clamps(camera, controls)
    block["camera"] = camera
    settings["mjpg_streamer"] = block
    return camera, notes


def effective_mjpg_camera(settings: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    block = _mjpg_settings_block(settings)
    try:
        camera = normalize_mjpg_camera(block.get("camera"))
    except ValueError:
        camera = dict(DEFAULT_MJPG_CAMERA)
    controls = query_v4l2_controls(camera["device"])
    return apply_v4l2_clamps(camera, controls)

    block = _mjpg_settings_block(settings)
    try:
        camera = normalize_mjpg_camera(block.get("camera"))
    except ValueError:
        camera = dict(DEFAULT_MJPG_CAMERA)
    controls = query_v4l2_controls(camera["device"])
    return apply_v4l2_clamps(camera, controls)


def persist_mjpg_start_options(settings: Dict[str, Any], body: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Merge camera + optional webcam stream URL into settings before starting mjpg_streamer."""
    notes: List[str] = []
    if "camera" in body and isinstance(body.get("camera"), dict):
        _, clamp_notes = merge_camera_into_settings(settings, body["camera"])
        notes.extend(clamp_notes)
    stream_id = str(body.get("webcam_stream_id", "") or "").strip()
    stream_url = str(body.get("stream_url", "") or "").strip()
    if stream_id and stream_url:
        parse_webcam_stream_url(stream_url)
        streams = settings.get("webcam_streams")
        if not isinstance(streams, list):
            streams = []
        matched = False
        for item in streams:
            if isinstance(item, dict) and str(item.get("id", "")).strip() == stream_id:
                item["stream_url"] = stream_url
                matched = True
                break
        if matched:
            settings["webcam_streams"] = streams
        else:
            notes.append(f"webcam stream {stream_id!r} not found; stream URL not saved")
    return settings, notes


def normalize_mjpg_camera(raw: Any) -> Dict[str, Any]:
    """Validate mjpg_streamer.camera settings used to build input_uvc.so arguments."""
    src = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_MJPG_CAMERA)
    device = str(src.get("device", out["device"]) or "/dev/video0").strip()
    if not re.fullmatch(r"/dev/video[\w]+", device):
        raise ValueError("camera.device must look like /dev/video0")
    out["device"] = device

    resolution = str(src.get("resolution", out["resolution"]) or "").strip()
    if not resolution:
        raise ValueError("camera.resolution is required")
    res_upper = resolution.upper()
    if res_upper not in MJPG_NAMED_RESOLUTIONS and not re.fullmatch(r"\d{2,5}x\d{2,5}", resolution):
        raise ValueError("camera.resolution must be WIDTHxHEIGHT (e.g. 1920x1080) or a named preset")
    out["resolution"] = res_upper if res_upper in MJPG_NAMED_RESOLUTIONS else resolution

    fps_raw = src.get("fps", out["fps"])
    if fps_raw is None or fps_raw == "":
        out["fps"] = ""
    else:
        try:
            fps = int(fps_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("camera.fps must be an integer 1..120 or empty") from exc
        if fps < 1 or fps > 120:
            raise ValueError("camera.fps must be between 1 and 120")
        out["fps"] = fps

    try:
        quality = int(src.get("quality", out["quality"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("camera.quality must be an integer 1..100") from exc
    if quality < 1 or quality > 100:
        raise ValueError("camera.quality must be between 1 and 100")
    out["quality"] = quality

    out["auto_exposure"] = _normalize_auto_exposure_value(src.get("auto_exposure", out["auto_exposure"]))

    exp_raw = src.get("exposure_time", out.get("exposure_time", ""))
    if exp_raw is None or str(exp_raw).strip() == "":
        out["exposure_time"] = ""
    else:
        try:
            exp = int(exp_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("camera.exposure_time must be an integer or empty") from exc
        if exp < 1 or exp > 100000:
            raise ValueError("camera.exposure_time is out of range")
        out["exposure_time"] = str(exp)

    gain_raw = src.get("gain", out.get("gain", ""))
    if gain_raw is None or str(gain_raw).strip() == "":
        out["gain"] = ""
    else:
        gs = str(gain_raw).strip()
        if gs.lower() == "auto":
            out["gain"] = "auto"
        else:
            try:
                gain = int(gs)
            except ValueError as exc:
                raise ValueError("camera.gain must be empty, auto, or an integer") from exc
            if gain < 0 or gain > 10000:
                raise ValueError("camera.gain is out of range")
            out["gain"] = str(gain)

    wb_raw = src.get("white_balance_automatic", out.get("white_balance_automatic", ""))
    if wb_raw is None or str(wb_raw).strip() == "":
        out["white_balance_automatic"] = ""
    else:
        wb = str(wb_raw).strip()
        if wb not in ("0", "1"):
            raise ValueError("camera.white_balance_automatic must be 0, 1, or empty")
        out["white_balance_automatic"] = wb

    for key in MJPG_CAMERA_TUNING_KEYS:
        val = src.get(key, out.get(key, ""))
        if val is None:
            out[key] = ""
            continue
        s = str(val).strip()
        if not s:
            out[key] = ""
            continue
        if s.lower() == "auto":
            out[key] = "auto"
            continue
        try:
            n = int(s)
        except ValueError as exc:
            raise ValueError(f"camera.{key} must be empty, auto, or an integer") from exc
        if n < -10000 or n > 10000:
            raise ValueError(f"camera.{key} is out of range")
        out[key] = str(n)
    return out


def build_input_uvc_plugin_args(camera: Dict[str, Any]) -> str:
    parts = ["input_uvc.so", f"-d {camera['device']}", f"-r {camera['resolution']}"]
    fps = camera.get("fps")
    if fps not in ("", None):
        parts.append(f"-f {int(fps)}")
    parts.append(f"-q {int(camera['quality'])}")
    flag_map = {"brightness": "br", "contrast": "co", "sharpness": "sh", "saturation": "sa"}
    for key, flag in flag_map.items():
        val = str(camera.get(key, "") or "").strip()
        if val:
            parts.append(f"-{flag} {val}")
    ae_mode = _normalize_auto_exposure_value(camera.get("auto_exposure", "manual"))
    exp = str(camera.get("exposure_time", "") or "").strip()
    if ae_mode == "aperture-priority":
        parts.append("-ex aperature-priority")
    elif ae_mode == "shutter-priority":
        parts.append("-ex shutter-priority")
    elif ae_mode == "auto":
        parts.append("-ex auto")
    elif exp:
        parts.append(f"-ex {exp}")
    gain = str(camera.get("gain", "") or "").strip()
    if gain:
        parts.append(f"-gain {gain}")
    return " ".join(parts)


def list_video_devices() -> List[Dict[str, str]]:
    devices: List[Dict[str, str]] = []
    base = Path("/sys/class/video4linux")
    if not base.is_dir():
        return devices
    for node in sorted(base.iterdir()):
        if not node.name.startswith("video"):
            continue
        label = node.name
        try:
            label = (node / "name").read_text(encoding="utf-8").strip() or label
        except OSError:
            pass
        devices.append({"path": f"/dev/{node.name}", "name": label})
    return devices


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
    v = block.get("mjpg_streamer_root")
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
    if raw_block.get("use_custom_args") and isinstance(raw_block.get("args"), list) and raw_block["args"]:
        template = [str(x) for x in raw_block["args"]]
    else:
        camera, _ = effective_mjpg_camera(settings)
        input_part = build_input_uvc_plugin_args(camera)
        template = ["-i", input_part, "-o", "output_http.so -w www -p {port}"]
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


def execute_mjpg_start(port: int, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Start mjpg_streamer; optionally merge camera/stream URL from body into settings first."""
    try:
        settings = load_settings_file()
    except Exception as exc:  # noqa: BLE001
        return 500, {"error": f"failed to load settings: {exc}"}

    notes: List[str] = []
    try:
        settings, notes = persist_mjpg_start_options(settings, body)
        write_settings_file(settings)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return 500, {"error": f"failed to save settings: {exc}"}

    ok, msg, pid = start_mjpg_streamer_subprocess(port, settings)
    if not ok:
        return 500, {"success": False, "error": msg, "port": port}

    camera_out, _ = effective_mjpg_camera(settings)
    live_applied, live_errors = apply_v4l2_camera_live(camera_out["device"], camera_out)
    if live_errors:
        notes.extend(live_errors)
    return 200, {
        "success": True,
        "message": msg,
        "port": port,
        "pid": pid,
        "camera": camera_out,
        "built_input_args": build_input_uvc_plugin_args(camera_out),
        "live_applied": live_applied,
        "warnings": notes,
        "default_stream_url_path": mjpeg_autofill_path_from_settings(settings),
        "default_http_port": mjpeg_default_http_port_from_settings(settings),
    }


def execute_mjpg_stop(port: int) -> Tuple[int, Dict[str, Any]]:
    ok, msg = stop_mjpeg_on_port(port)
    if ok:
        return 200, {"success": True, "message": msg, "port": port}
    return 500, {"success": False, "error": msg, "port": port}


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

        if parsed.path == "/api/mjpg/devices":
            json_response(self, 200, {"devices": list_video_devices()})
            return

        if parsed.path == "/api/mjpg/controls":
            query = parse_qs(parsed.query, keep_blank_values=False)
            device = str(query.get("device", ["/dev/video0"])[0]).strip() or "/dev/video0"
            if not re.fullmatch(r"/dev/video[\w]+", device):
                json_response(self, 400, {"error": "device must look like /dev/video0"})
                return
            controls = query_v4l2_controls(device)
            value_names = [
                "brightness",
                "contrast",
                "saturation",
                "sharpness",
                "gain",
                "exposure_time_absolute",
                "auto_exposure",
                "white_balance_automatic",
            ]
            values = read_v4l2_control_values(device, value_names)
            json_response(
                self,
                200,
                {
                    "device": device,
                    "controls": controls,
                    "values": values,
                    "v4l2_ctl_available": bool(shutil.which("v4l2-ctl")),
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
                        "openweather_api_key_set": openweather_api_key_configured(),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/settings/containers":
            try:
                settings = load_settings_file()
                containers = normalize_feature_containers(settings.get("feature_containers"))
                json_response(self, 200, {"feature_containers": containers})
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/settings/webcams":
            try:
                settings = load_settings_file()
                installed = container_is_installed(settings, "webcam")
                if not installed:
                    json_response(self, 200, {"container_installed": False, "webcam_streams": []})
                    return
                streams = settings.get("webcam_streams") or []
                if not isinstance(streams, list):
                    streams = []
                json_response(self, 200, {"container_installed": True, "webcam_streams": streams})
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/settings/mjpg-streamer":
            try:
                settings = load_settings_file()
                installed = container_is_installed(settings, "webcam")
                if not installed:
                    json_response(
                        self,
                        200,
                        {
                            "container_installed": False,
                            "mjpg_streamer_root_saved": "",
                            "mjpg_streamer_root_from_env": False,
                            "resolved_install_dir": "",
                            "mjpg_streamer_found": False,
                            "args": None,
                            "use_custom_args": False,
                            "camera": dict(DEFAULT_MJPG_CAMERA),
                            "built_input_args": "",
                            "v4l2_controls": {},
                            "default_stream_url_path": DEFAULT_MJPEG_STREAM_AUTOFILL_PATH,
                            "default_http_port": DEFAULT_MJPEG_HTTP_PORT,
                        },
                    )
                    return
                install_dir = resolved_mjpg_install_dir(settings)
                exe = mjpg_streamer_executable_path(settings)
                saved = mjpg_streamer_root_from_settings_only(settings)
                from_env = bool(os.getenv("GROWCONTROL_MJPG_STREAMER_ROOT", "").strip())
                block = _mjpg_settings_block(settings)
                args_out = block.get("args")
                if not isinstance(args_out, list):
                    args_out = None
                try:
                    camera_out, _ = effective_mjpg_camera(settings)
                except ValueError:
                    camera_out = dict(DEFAULT_MJPG_CAMERA)
                json_response(
                    self,
                    200,
                    {
                        "mjpg_streamer_root_saved": saved,
                        "mjpg_streamer_root_from_env": from_env,
                        "resolved_install_dir": str(install_dir),
                        "mjpg_streamer_found": exe.is_file(),
                        "args": args_out,
                        "use_custom_args": bool(block.get("use_custom_args")),
                        "camera": camera_out,
                        "built_input_args": build_input_uvc_plugin_args(camera_out),
                        "v4l2_controls": query_v4l2_controls(camera_out.get("device", "/dev/video0")),
                        "default_stream_url_path": mjpeg_autofill_path_from_settings(settings),
                        "default_http_port": mjpeg_default_http_port_from_settings(settings),
                        "container_installed": True,
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

            try:
                if api_key:
                    set_openweather_api_key(api_key)
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
                        "openweather_api_key_set": openweather_api_key_configured(),
                        "note": "Weather API key is stored in .env (OPENWEATHER_API_KEY). Collector reloads settings.json when it changes.",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if path == "/api/settings/containers":
            try:
                settings = load_settings_file()
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": f"failed to load settings: {exc}"})
                return
            container = str(body.get("container", "")).strip().lower()
            if container not in DEFAULT_FEATURE_CONTAINERS:
                json_response(self, 400, {"error": "invalid container", "choices": list(DEFAULT_FEATURE_CONTAINERS.keys())})
                return
            if "installed" not in body:
                json_response(self, 400, {"error": "missing field: installed"})
                return
            installed = bool(body.get("installed"))
            containers = normalize_feature_containers(settings.get("feature_containers"))
            containers[container]["installed"] = installed
            settings["feature_containers"] = containers
            try:
                write_settings_file(settings)
                json_response(self, 200, {"success": True, "feature_containers": containers})
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
            return

        if path == "/api/webcam/validate-url":
            try:
                settings = load_settings_file()
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": f"failed to load settings: {exc}"})
                return
            if not container_is_installed(settings, "webcam"):
                json_response(self, 403, {"success": False, "error": "webcam container is not installed"})
                return
            stream_url = str(body.get("stream_url", "")).strip()
            try:
                result = probe_external_webcam_url(stream_url)
                json_response(self, 200, {"success": True, **result})
            except ValueError as exc:
                json_response(self, 400, {"success": False, "error": str(exc)})
            return

        if path == "/api/settings/webcams":
            try:
                settings = load_settings_file()
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": f"failed to load settings: {exc}"})
                return
            if not container_is_installed(settings, "webcam"):
                json_response(self, 403, {"error": "webcam container is not installed"})
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
                    "note": "Dashboard loads streams in the browser. Built-in streams use mjpg-streamer (Start/Stop). External streams must pass URL safety checks (http/https image or MJPEG/HTML viewer only — no downloads).",
                },
            )
            return

        if path == "/api/settings/mjpg-streamer":
            try:
                settings = load_settings_file()
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": f"failed to load settings: {exc}"})
                return
            if not container_is_installed(settings, "webcam"):
                json_response(self, 403, {"error": "webcam container is not installed"})
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
            if "camera" in body:
                try:
                    camera = normalize_mjpg_camera(body.get("camera"))
                    controls = query_v4l2_controls(camera["device"])
                    camera, clamp_notes = apply_v4l2_clamps(camera, controls)
                    block["camera"] = camera
                except ValueError as exc:
                    json_response(self, 400, {"error": str(exc)})
                    return
            if "use_custom_args" in body:
                block["use_custom_args"] = bool(body.get("use_custom_args"))
            settings["mjpg_streamer"] = block
            try:
                write_settings_file(settings)
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
                return
            install_dir = resolved_mjpg_install_dir(settings)
            exe = install_dir / "mjpg_streamer"
            camera_out, clamp_notes = effective_mjpg_camera(settings)
            json_response(
                self,
                200,
                {
                    "success": True,
                    "mjpg_streamer": block,
                    "mjpg_streamer_root_saved": block.get("mjpg_streamer_root", ""),
                    "resolved_install_dir": str(install_dir),
                    "mjpg_streamer_found": exe.is_file(),
                    "camera": camera_out,
                    "built_input_args": build_input_uvc_plugin_args(camera_out),
                    "v4l2_controls": query_v4l2_controls(camera_out.get("device", "/dev/video0")),
                    "warnings": clamp_notes,
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
        if path == "/api/mjpg/controls/apply":
            camera_raw = body.get("camera")
            if not isinstance(camera_raw, dict):
                json_response(self, 400, {"error": "camera object is required"})
                return
            save = bool(body.get("save", True))
            try:
                settings = load_settings_file()
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": f"failed to load settings: {exc}"})
                return
            if not container_is_installed(settings, "webcam"):
                json_response(self, 403, {"error": "webcam container is not installed"})
                return
            try:
                camera, notes = merge_camera_into_settings(settings, camera_raw)
                applied, errors = apply_v4l2_camera_live(camera["device"], camera)
                if save:
                    write_settings_file(settings)
            except ValueError as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
                return
            json_response(
                self,
                200,
                {
                    "success": not errors,
                    "camera": camera,
                    "applied": applied,
                    "errors": errors,
                    "warnings": notes,
                    "built_input_args": build_input_uvc_plugin_args(camera),
                },
            )
            return

        if path == "/api/mjpg":
            try:
                settings = load_settings_file()
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": f"failed to load settings: {exc}"})
                return
            if not container_is_installed(settings, "webcam"):
                json_response(self, 403, {"error": "webcam container is not installed"})
                return
            try:
                port = int(body.get("port"))
            except (TypeError, ValueError):
                json_response(self, 400, {"error": "missing or invalid port (integer 1..65535)"})
                return
            run = str(body.get("run", "")).strip().lower()
            if run == "start":
                status, payload = execute_mjpg_start(port, body)
                json_response(self, status, payload)
                return
            if run == "stop":
                status, payload = execute_mjpg_stop(port)
                json_response(self, status, payload)
                return
            json_response(self, 400, {"error": 'invalid run; use "start" or "stop"'})
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
