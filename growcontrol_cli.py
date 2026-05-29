#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


DEFAULT_SENSORS_PATH = Path("config/sensors.json")
DEFAULT_SETTINGS_PATH = Path("config/settings.json")
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
MAC_SEARCH_RE = re.compile(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
FLORA_NAME_KEYWORDS = ("flower care", "miflora", "flora", "hhcc")
XIAOMI_SERVICE_HINTS = ("0000fe95", "fe95")
LIKELY_FLORA_PREFIXES = ("5C:85:7E",)


def repair_sensors(data: Dict[str, List[Dict[str, object]]]) -> Tuple[Dict[str, List[Dict[str, object]]], bool]:
    """Assign missing ids/names so sensor actions remain addressable."""
    sensors = data.setdefault("sensors", [])
    used_ids = {str(sensor.get("id", "")).strip() for sensor in sensors if str(sensor.get("id", "")).strip()}
    changed = False

    for index, sensor in enumerate(sensors):
        sensor_id = str(sensor.get("id", "")).strip()
        if not sensor_id:
            mac = str(sensor.get("mac", "")).replace(":", "").upper()
            base = f"sensor_{mac[-6:]}" if len(mac) >= 6 else f"sensor_{index + 1}"
            candidate = base
            suffix = 2
            while candidate in used_ids:
                candidate = f"{base}_{suffix}"
                suffix += 1
            sensor["id"] = candidate
            used_ids.add(candidate)
            changed = True
            sensor_id = candidate

        if not str(sensor.get("name", "")).strip():
            sensor["name"] = sensor_id
            changed = True

    return data, changed


def load_sensors(path: Path) -> Dict[str, List[Dict[str, object]]]:
    if not path.exists():
        return {"sensors": []}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    data, changed = repair_sensors(data)
    if changed:
        save_sensors(path, data)
    return data


def load_settings(path: Path) -> Dict[str, object]:
    if not path.exists():
        raise ValueError(f"Settings file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_sensors(path: Path, data: Dict[str, List[Dict[str, object]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def _clean_scan_line(line: str) -> str:
    # Remove ANSI/control characters emitted by bluetoothctl.
    line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
    line = re.sub(r"[\x00-\x1f]", "", line)
    return line.strip()


def _device_priority(device: Dict[str, Any]) -> Tuple[int, str]:
    # Lower tuple means higher position.
    if device.get("is_likely_flora"):
        return (0, str(device.get("name", "")).lower())
    name = str(device.get("name", "")).lower()
    if name != "(unknown)":
        return (1, name)
    return (2, str(device.get("mac", "")))


def _is_likely_flora(mac: str, name: str, xiaomi_hint: bool) -> bool:
    lowered = name.lower()
    if any(keyword in lowered for keyword in FLORA_NAME_KEYWORDS):
        return True
    if any(mac.startswith(prefix) for prefix in LIKELY_FLORA_PREFIXES):
        return True
    if xiaomi_hint:
        return True
    return False


def _parse_scan_output(output: str) -> List[Dict[str, Any]]:
    devices: Dict[str, Dict[str, Any]] = {}
    noisy_suffixes = (
        "RSSI:",
        "TxPower:",
        "ManufacturerData",
        "ServiceData",
        "Class:",
        "Icon:",
        "Modalias:",
        "UUIDs:",
        "Name:",
        "Alias:",
        "Discovering:",
    )
    for raw in output.splitlines():
        line = _clean_scan_line(raw)
        if not line:
            continue
        match = MAC_SEARCH_RE.search(line)
        if not match:
            continue
        mac = match.group(0).upper()
        tail = line[match.end() :].strip()
        lowered_line = line.lower()
        xiaomi_hint = any(hint in lowered_line for hint in XIAOMI_SERVICE_HINTS)

        if any(tail.startswith(prefix) for prefix in noisy_suffixes):
            tail = ""

        entry = devices.get(
            mac,
            {
                "mac": mac,
                "name": "(unknown)",
                "is_likely_flora": False,
                "source_hint": "",
            },
        )
        if tail and (entry["name"] == "(unknown)" or entry["name"] == ""):
            entry["name"] = tail
        elif tail and entry["name"] == "(unknown)":
            entry["name"] = tail

        if xiaomi_hint:
            entry["source_hint"] = "xiaomi-service"
        entry["is_likely_flora"] = _is_likely_flora(
            mac=mac,
            name=str(entry["name"]),
            xiaomi_hint=xiaomi_hint or bool(entry.get("source_hint") == "xiaomi-service"),
        )
        devices[mac] = entry

    return sorted(devices.values(), key=_device_priority)


def _scan_hcitool(timeout_seconds: int) -> Tuple[List[Dict[str, Any]], str]:
    cmd = ["timeout", str(timeout_seconds), "hcitool", "lescan", "--duplicates"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    out = (result.stdout or "") + "\n" + (result.stderr or "")
    return _parse_scan_output(out), out


def _scan_bluetoothctl(timeout_seconds: int) -> Tuple[List[Dict[str, Any]], str]:
    cmd = ["bluetoothctl", "--timeout", str(timeout_seconds), "scan", "on"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    out = (result.stdout or "") + "\n" + (result.stderr or "")
    return _parse_scan_output(out), out


def discover_ble_devices_detailed(timeout_seconds: int) -> List[Dict[str, Any]]:
    errors: List[str] = []

    try:
        hci_devices, hci_out = _scan_hcitool(timeout_seconds)
        if hci_devices:
            return hci_devices
        if "Operation not permitted" in hci_out or "Permission denied" in hci_out:
            errors.append("hcitool: permission denied")
        elif "Set scan parameters failed" in hci_out:
            errors.append("hcitool: adapter scan-parameter error")
    except FileNotFoundError:
        errors.append("hcitool/timeout not found")

    try:
        bt_devices, bt_out = _scan_bluetoothctl(timeout_seconds)
        if bt_devices:
            return bt_devices
        if "Operation not permitted" in bt_out or "Permission denied" in bt_out:
            errors.append("bluetoothctl: permission denied")
    except FileNotFoundError:
        errors.append("bluetoothctl not found")

    if errors and all("permission denied" in e for e in errors):
        raise RuntimeError("BLE scan permission denied. Try: sudo growcontrol add-sensor --scan")

    return []


def discover_ble_devices(timeout_seconds: int) -> List[Tuple[str, str]]:
    devices = discover_ble_devices_detailed(timeout_seconds)
    return [(str(device.get("mac")), str(device.get("name"))) for device in devices]


def next_output_file(sensors: List[Dict[str, object]]) -> str:
    used = set()
    for sensor in sensors:
        output_file = str(sensor.get("output_file", "")).strip()
        if output_file.startswith("Sensor_"):
            suffix = output_file.split("_", 1)[1]
            if suffix.isdigit():
                used.add(int(suffix))
    idx = 1
    while idx in used:
        idx += 1
    return f"Sensor_{idx}"


def validate_mac(mac: str) -> str:
    normalized = mac.strip().upper()
    if not MAC_RE.match(normalized):
        raise ValueError(f"Invalid MAC format: {mac}")
    return normalized


def add_sensor(
    sensors_path: Path,
    sensor_id: str,
    name: str,
    mac: str,
    output_file: str | None,
    enabled: bool,
) -> None:
    sensor_id = sensor_id.strip()
    name = name.strip()
    if not sensor_id:
        raise ValueError("Sensor id is required")
    if not name:
        raise ValueError("Sensor name is required")

    data = load_sensors(sensors_path)
    sensors = data.setdefault("sensors", [])

    if any(str(entry.get("id")) == sensor_id for entry in sensors):
        raise ValueError(f"Sensor id already exists: {sensor_id}")
    if any(str(entry.get("mac", "")).upper() == mac for entry in sensors):
        raise ValueError(f"Sensor MAC already exists: {mac}")

    if not output_file:
        output_file = next_output_file(sensors)

    sensors.append(
        {
            "id": sensor_id,
            "name": name,
            "mac": mac,
            "output_file": output_file,
            "enabled": enabled,
        }
    )
    save_sensors(sensors_path, data)


def find_sensor_index(sensors: List[Dict[str, object]], sensor_id: str) -> int:
    for idx, sensor in enumerate(sensors):
        if str(sensor.get("id")) == sensor_id:
            return idx
    return -1


def edit_sensor(
    sensors_path: Path,
    sensor_id: str,
    new_id: str | None,
    name: str | None,
    mac: str | None,
    output_file: str | None,
    enabled: bool | None,
) -> Dict[str, object]:
    data = load_sensors(sensors_path)
    sensors = data.setdefault("sensors", [])
    idx = find_sensor_index(sensors, sensor_id)
    if idx < 0:
        raise ValueError(f"Sensor not found: {sensor_id}")

    sensor = dict(sensors[idx])
    effective_id = new_id if new_id else str(sensor.get("id"))
    effective_mac = mac if mac else str(sensor.get("mac", "")).upper()

    for i, entry in enumerate(sensors):
        if i == idx:
            continue
        if str(entry.get("id")) == effective_id:
            raise ValueError(f"Another sensor already uses id: {effective_id}")
        if str(entry.get("mac", "")).upper() == effective_mac:
            raise ValueError(f"Another sensor already uses MAC: {effective_mac}")

    if new_id:
        sensor["id"] = new_id
    if name:
        sensor["name"] = name
    if mac:
        sensor["mac"] = validate_mac(mac)
    if output_file:
        sensor["output_file"] = output_file
    if enabled is not None:
        sensor["enabled"] = enabled

    sensors[idx] = sensor
    save_sensors(sensors_path, data)
    return sensor


def remove_sensor(sensors_path: Path, sensor_id: str) -> Dict[str, object]:
    data = load_sensors(sensors_path)
    sensors = data.setdefault("sensors", [])
    idx = find_sensor_index(sensors, sensor_id)
    if idx < 0:
        raise ValueError(f"Sensor not found: {sensor_id}")

    removed = sensors.pop(idx)
    save_sensors(sensors_path, data)
    return removed


def restart_service(service_name: str) -> None:
    cmd = ["sudo", "systemctl", "restart", service_name]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to restart {service_name}. "
            f"stdout: {result.stdout.strip()} stderr: {result.stderr.strip()}"
        )


def cmd_add_sensor(args: argparse.Namespace) -> int:
    sensors_path = Path(args.sensors_file)
    if args.scan:
        devices = discover_ble_devices_detailed(args.scan_timeout)
        if not devices:
            print("No BLE devices discovered. Move sensor closer, wait 5-10 seconds, then scan again.")
            return 0
        print("Discovered BLE devices:")
        for device in devices:
            mac = str(device.get("mac"))
            name = str(device.get("name"))
            marker = " [flora?]" if bool(device.get("is_likely_flora")) else ""
            print(f"- {mac}  {name}{marker}")
        return 0

    if not args.id or not args.name or not args.mac:
        raise ValueError("For adding a sensor you must provide --id, --name and --mac.")

    mac = validate_mac(args.mac)
    add_sensor(
        sensors_path=sensors_path,
        sensor_id=args.id,
        name=args.name,
        mac=mac,
        output_file=args.output_file,
        enabled=not args.disabled,
    )
    print(f"Added sensor '{args.id}' ({mac}) to {sensors_path}")

    if args.restart_service:
        restart_service(args.service_name)
        print(f"Restarted service: {args.service_name}")
    else:
        print("Tip: restart collector to apply immediately:")
        print(f"  sudo systemctl restart {args.service_name}")
    return 0


def cmd_list_sensors(args: argparse.Namespace) -> int:
    sensors_path = Path(args.sensors_file)
    data = load_sensors(sensors_path)
    sensors = data.get("sensors", [])
    if not sensors:
        print("No sensors configured.")
        return 0
    print(f"Sensors in {sensors_path}:")
    for sensor in sensors:
        state = "enabled" if sensor.get("enabled", True) else "disabled"
        print(
            f"- {sensor.get('id')} | {sensor.get('name')} | {sensor.get('mac')} | "
            f"{sensor.get('output_file')} | {state}"
        )
    return 0


def cmd_edit_sensor(args: argparse.Namespace) -> int:
    sensors_path = Path(args.sensors_file)
    if not args.id:
        raise ValueError("Missing required argument: --id")

    enabled = None
    if args.enabled:
        enabled = True
    if args.disabled:
        enabled = False

    sensor = edit_sensor(
        sensors_path=sensors_path,
        sensor_id=args.id,
        new_id=args.new_id,
        name=args.name,
        mac=validate_mac(args.mac) if args.mac else None,
        output_file=args.output_file,
        enabled=enabled,
    )
    print(f"Updated sensor '{args.id}' in {sensors_path}")
    print(
        f"- id={sensor.get('id')} name={sensor.get('name')} "
        f"mac={sensor.get('mac')} output_file={sensor.get('output_file')} "
        f"enabled={sensor.get('enabled')}"
    )

    if args.restart_service:
        restart_service(args.service_name)
        print(f"Restarted service: {args.service_name}")
    else:
        print("Tip: restart collector to apply immediately:")
        print(f"  sudo systemctl restart {args.service_name}")
    return 0


def cmd_remove_sensor(args: argparse.Namespace) -> int:
    sensors_path = Path(args.sensors_file)
    if not args.id:
        raise ValueError("Missing required argument: --id")

    removed = remove_sensor(sensors_path=sensors_path, sensor_id=args.id)
    print(f"Removed sensor '{args.id}' from {sensors_path}")
    print(
        f"- id={removed.get('id')} name={removed.get('name')} "
        f"mac={removed.get('mac')} output_file={removed.get('output_file')} "
        f"enabled={removed.get('enabled')}"
    )

    if args.restart_service:
        restart_service(args.service_name)
        print(f"Restarted service: {args.service_name}")
    else:
        print("Tip: restart collector to apply immediately:")
        print(f"  sudo systemctl restart {args.service_name}")
    return 0


def run_check(name: str, ok: bool, detail: str) -> bool:
    state = "OK" if ok else "FAIL"
    print(f"[{state}] {name}: {detail}")
    return ok


def command_exists(command: str) -> bool:
    result = subprocess.run(["bash", "-lc", f"command -v {command}"], capture_output=True, text=True, check=False)
    return result.returncode == 0


def service_is_active(service_name: str) -> Tuple[bool, str]:
    result = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True, check=False)
    out = (result.stdout or result.stderr).strip()
    return result.returncode == 0 and out == "active", out or "unknown"


def bluetooth_adapter_ready() -> Tuple[bool, str]:
    if not command_exists("rfkill"):
        return True, "rfkill not available (skipped)"
    result = subprocess.run(["rfkill", "list", "bluetooth"], capture_output=True, text=True, check=False)
    output = result.stdout.strip()
    if not output:
        return True, "no Bluetooth rfkill entry (skipped)"
    if "Soft blocked: yes" in output:
        return False, "adapter soft-blocked; run: sudo systemctl restart growcontrol-bluetooth.service"
    if command_exists("bluetoothctl"):
        ctl = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True, check=False)
        ctl_out = ctl.stdout
        if "Powered: no" in ctl_out or "off-blocked" in ctl_out:
            return False, "adapter powered off; run: sudo systemctl restart growcontrol-bluetooth.service"
    return True, "unblocked and powered on"


def cmd_doctor(args: argparse.Namespace) -> int:
    ok_all = True
    sensors_path = Path(args.sensors_file)
    settings_path = Path(args.settings_file)

    ok_all &= run_check("Python", True, sys.executable)
    ok_all &= run_check("hcitool", command_exists("hcitool"), "required for BLE scan")
    ok_all &= run_check("timeout", command_exists("timeout"), "required for BLE scan time limit")
    bt_ok, bt_detail = bluetooth_adapter_ready()
    ok_all &= run_check("bluetooth adapter", bt_ok, bt_detail)
    if not bt_ok and args.strict:
        ok_all = False
    ok_all &= run_check("systemctl", command_exists("systemctl"), "required for service management")
    ok_all &= run_check("nginx", command_exists("nginx"), "required for web dashboard")

    settings: Dict[str, object] = {}
    sensors_data: Dict[str, List[Dict[str, object]]] = {"sensors": []}

    try:
        settings = load_settings(settings_path)
        ok_all &= run_check("settings.json", True, str(settings_path))
    except Exception as exc:  # noqa: BLE001
        ok_all &= run_check("settings.json", False, str(exc))

    try:
        sensors_data = load_sensors(sensors_path)
        ok_all &= run_check("sensors.json", True, str(sensors_path))
    except Exception as exc:  # noqa: BLE001
        ok_all &= run_check("sensors.json", False, str(exc))

    if settings:
        output_dir = Path(str(settings.get("output_dir", "")))
        if output_dir:
            exists = output_dir.exists()
            writable = os.access(output_dir, os.W_OK) if exists else False
            ok_all &= run_check("output_dir exists", exists, str(output_dir))
            ok_all &= run_check("output_dir writable", writable, str(output_dir))

        weather_enabled = bool(settings.get("weather_enabled", True))
        if weather_enabled:
            has_key = bool(os.getenv("OPENWEATHER_API_KEY", "").strip())
            ok_all &= run_check("weather API key", has_key, "OPENWEATHER_API_KEY set")
        else:
            run_check("weather", True, "disabled in settings")

    sensors = sensors_data.get("sensors", [])
    run_check("configured sensors", True, str(len(sensors)))
    macs = set()
    ids = set()
    for sensor in sensors:
        sensor_id = str(sensor.get("id", ""))
        mac = str(sensor.get("mac", "")).upper()
        valid_id = bool(sensor_id)
        valid_mac = bool(MAC_RE.match(mac))
        unique_id = sensor_id not in ids
        unique_mac = mac not in macs
        if valid_id:
            ids.add(sensor_id)
        if valid_mac:
            macs.add(mac)

        ok_all &= run_check(f"sensor {sensor_id or '(missing id)'} id", valid_id and unique_id, "present and unique")
        ok_all &= run_check(f"sensor {sensor_id or '(missing id)'} mac", valid_mac and unique_mac, mac or "(missing)")

    service_ok, service_state = service_is_active(args.service_name)
    run_check("collector service", service_ok, f"{args.service_name} is {service_state}")
    if not service_ok and args.strict:
        ok_all = False

    webapi_ok, webapi_state = service_is_active(args.webapi_service_name)
    run_check("webapi service", webapi_ok, f"{args.webapi_service_name} is {webapi_state}")
    if not webapi_ok and args.strict:
        ok_all = False

    bt_service_ok, bt_service_state = service_is_active("growcontrol-bluetooth.service")
    run_check("bluetooth boot service", bt_service_ok, f"growcontrol-bluetooth.service is {bt_service_state}")
    if not bt_service_ok and args.strict:
        ok_all = False

    nginx_ok, nginx_state = service_is_active(args.nginx_service_name)
    run_check("nginx service", nginx_ok, f"{args.nginx_service_name} is {nginx_state}")
    if not nginx_ok and args.strict:
        ok_all = False

    if ok_all:
        print("Doctor finished: system looks healthy.")
        return 0

    print("Doctor finished: issues detected.")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="growcontrol", description="Growcontrol helper CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    add_sensor_parser = sub.add_parser("add-sensor", help="Scan BLE or add a sensor to config")
    add_sensor_parser.add_argument("--scan", action="store_true", help="Scan nearby BLE devices")
    add_sensor_parser.add_argument("--scan-timeout", type=int, default=20, help="Seconds for BLE scan")
    add_sensor_parser.add_argument("--id", help="Unique sensor id (e.g. plant_1)")
    add_sensor_parser.add_argument("--name", help="Display name")
    add_sensor_parser.add_argument("--mac", help="Sensor MAC address")
    add_sensor_parser.add_argument("--output-file", help="Output file name (default auto Sensor_N)")
    add_sensor_parser.add_argument("--disabled", action="store_true", help="Add sensor as disabled")
    add_sensor_parser.add_argument("--restart-service", action="store_true", help="Restart collector automatically")
    add_sensor_parser.add_argument("--service-name", default="growcontrol-collector.service")
    add_sensor_parser.add_argument("--sensors-file", default=str(DEFAULT_SENSORS_PATH))
    add_sensor_parser.set_defaults(func=cmd_add_sensor)

    list_parser = sub.add_parser("list-sensors", help="List configured sensors")
    list_parser.add_argument("--sensors-file", default=str(DEFAULT_SENSORS_PATH))
    list_parser.set_defaults(func=cmd_list_sensors)

    edit_parser = sub.add_parser("edit-sensor", help="Edit an existing sensor")
    edit_parser.add_argument("--id", required=True, help="Existing sensor id")
    edit_parser.add_argument("--new-id", help="Set a new sensor id")
    edit_parser.add_argument("--name", help="Set display name")
    edit_parser.add_argument("--mac", help="Set sensor MAC address")
    edit_parser.add_argument("--output-file", help="Set output file name")
    state_group = edit_parser.add_mutually_exclusive_group()
    state_group.add_argument("--enabled", action="store_true", help="Set sensor enabled")
    state_group.add_argument("--disabled", action="store_true", help="Set sensor disabled")
    edit_parser.add_argument("--restart-service", action="store_true", help="Restart collector automatically")
    edit_parser.add_argument("--service-name", default="growcontrol-collector.service")
    edit_parser.add_argument("--sensors-file", default=str(DEFAULT_SENSORS_PATH))
    edit_parser.set_defaults(func=cmd_edit_sensor)

    remove_parser = sub.add_parser("remove-sensor", help="Remove a sensor by id")
    remove_parser.add_argument("--id", required=True, help="Sensor id to remove")
    remove_parser.add_argument("--restart-service", action="store_true", help="Restart collector automatically")
    remove_parser.add_argument("--service-name", default="growcontrol-collector.service")
    remove_parser.add_argument("--sensors-file", default=str(DEFAULT_SENSORS_PATH))
    remove_parser.set_defaults(func=cmd_remove_sensor)

    doctor_parser = sub.add_parser("doctor", help="Run health checks for Growcontrol setup")
    doctor_parser.add_argument("--service-name", default="growcontrol-collector.service")
    doctor_parser.add_argument("--webapi-service-name", default="growcontrol-webapi.service")
    doctor_parser.add_argument("--nginx-service-name", default="nginx.service")
    doctor_parser.add_argument("--sensors-file", default=str(DEFAULT_SENSORS_PATH))
    doctor_parser.add_argument("--settings-file", default=str(DEFAULT_SETTINGS_PATH))
    doctor_parser.add_argument("--strict", action="store_true", help="Fail if service is not active")
    doctor_parser.set_defaults(func=cmd_doctor)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
