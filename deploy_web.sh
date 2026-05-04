#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/pi/Growcontrol}"
WEB_SOURCE_DIR="${WEB_SOURCE_DIR:-$PROJECT_DIR/web}"
WEB_TARGET_DIR="${WEB_TARGET_DIR:-/var/www/html/growcontrol}"

if [[ ! -d "$WEB_SOURCE_DIR" ]]; then
  echo "Error: WEB_SOURCE_DIR not found: $WEB_SOURCE_DIR" >&2
  exit 1
fi

echo "Deploying web UI (runtime-safe) to $WEB_TARGET_DIR"
sudo mkdir -p "$WEB_TARGET_DIR"

# IMPORTANT: Do not delete runtime files produced by the collector.
# - status.json
# - weatherdata
# - Sensor_*
sudo rsync -av \
  --delete \
  --exclude 'Sensor_*' \
  --exclude 'weatherdata' \
  --exclude 'status.json' \
  "$WEB_SOURCE_DIR/" "$WEB_TARGET_DIR/"

sudo systemctl reload nginx >/dev/null 2>&1 || true
echo "Done."

