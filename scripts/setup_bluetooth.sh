#!/usr/bin/env bash
#
# Enable the Pi/Debian Bluetooth adapter for headless Growcontrol installs.
# - Installs pi-bluetooth on Raspberry Pi OS when available
# - Clears systemd-rfkill persisted soft-block state
# - Powers the adapter on immediately and installs a boot-time systemd unit
#
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/pi/Growcontrol}"
BT_SERVICE_TEMPLATE="$PROJECT_DIR/deploy/growcontrol-bluetooth.service"
BT_SERVICE_DEST="/etc/systemd/system/growcontrol-bluetooth.service"
RFKILL_DIR="/var/lib/systemd/rfkill"

install_pi_bluetooth_package() {
  if ! command -v apt-cache >/dev/null 2>&1; then
    return 0
  fi
  if ! apt-cache show pi-bluetooth >/dev/null 2>&1; then
    return 0
  fi
  if dpkg-query -W -f='${Status}' pi-bluetooth 2>/dev/null | grep -q "install ok installed"; then
    return 0
  fi
  echo "Installing pi-bluetooth (Raspberry Pi Bluetooth firmware helper)…"
  sudo apt-get install -y pi-bluetooth
}

clear_bluetooth_rfkill_persist() {
  [[ -d "$RFKILL_DIR" ]] || return 0
  local path
  for path in "$RFKILL_DIR"/platform-*:bluetooth; do
    [[ -e "$path" ]] || continue
    echo 0 | sudo tee "$path" >/dev/null
  done
}

enable_bluetooth_adapter_now() {
  sudo systemctl enable bluetooth.service
  sudo systemctl start bluetooth.service
  if command -v rfkill >/dev/null 2>&1; then
    sudo rfkill unblock bluetooth 2>/dev/null || true
  fi
  clear_bluetooth_rfkill_persist
  if command -v bluetoothctl >/dev/null 2>&1; then
    sudo bluetoothctl power on 2>/dev/null || true
  fi
}

install_boot_service() {
  if [[ ! -f "$BT_SERVICE_TEMPLATE" ]]; then
    echo "Error: missing $BT_SERVICE_TEMPLATE" >&2
    exit 1
  fi
  sudo cp "$BT_SERVICE_TEMPLATE" "$BT_SERVICE_DEST"
  sudo systemctl daemon-reload
  sudo systemctl enable growcontrol-bluetooth.service
  sudo systemctl start growcontrol-bluetooth.service
}

install_pi_bluetooth_package
enable_bluetooth_adapter_now
install_boot_service

if command -v rfkill >/dev/null 2>&1; then
  state="$(rfkill list bluetooth 2>/dev/null || true)"
  if [[ "$state" == *"Soft blocked: yes"* ]]; then
    echo "Warning: Bluetooth adapter is still soft-blocked after setup." >&2
    echo "$state" >&2
    exit 1
  fi
fi

echo "Bluetooth adapter enabled (growcontrol-bluetooth.service installed)."
