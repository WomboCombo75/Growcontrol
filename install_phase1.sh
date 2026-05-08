#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/pi/Growcontrol}"
SERVICE_TEMPLATE="$PROJECT_DIR/deploy/growcontrol-collector.service"
SERVICE_DEST="/etc/systemd/system/growcontrol-collector.service"
WEBAPI_TEMPLATE="$PROJECT_DIR/deploy/growcontrol-webapi.service"
WEBAPI_DEST="/etc/systemd/system/growcontrol-webapi.service"
UPDATE_CHECK_SERVICE_TEMPLATE="$PROJECT_DIR/deploy/growcontrol-update-check.service"
UPDATE_CHECK_TIMER_TEMPLATE="$PROJECT_DIR/deploy/growcontrol-update-check.timer"
UPDATE_CHECK_SERVICE_DEST="/etc/systemd/system/growcontrol-update-check.service"
UPDATE_CHECK_TIMER_DEST="/etc/systemd/system/growcontrol-update-check.timer"
NGINX_API_TEMPLATE="$PROJECT_DIR/deploy/nginx-growcontrol-api.conf"
NGINX_API_SNIPPET="/etc/nginx/snippets/growcontrol-api.conf"
NGINX_DEFAULT_SITE="/etc/nginx/sites-available/default"
WEB_SOURCE_DIR="$PROJECT_DIR/web"
WEB_TARGET_DIR="/var/www/html/growcontrol"
RUN_USER="${RUN_USER:-$(id -un)}"
RUN_GROUP="${RUN_GROUP:-$(id -gn)}"
# If the script was started as root via sudo, run services as the real user (BLE needs a normal login user).
if [[ $(id -u) -eq 0 ]] && [[ -n "${SUDO_USER:-}" ]]; then
  RUN_USER="$SUDO_USER"
  RUN_GROUP="$(id -gn "$RUN_USER")"
fi

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "Error: PROJECT_DIR does not exist: $PROJECT_DIR"
  exit 1
fi

if [[ ! -f "$PROJECT_DIR/requirements.txt" ]]; then
  echo "Error: requirements.txt not found in $PROJECT_DIR"
  exit 1
fi

echo "[1/7] Installing system packages"
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv bluetooth bluez bluez-tools nginx rsync curl ca-certificates git psmisc
if ! id -nG "$RUN_USER" 2>/dev/null | tr ' ' '\n' | grep -qx bluetooth; then
  sudo usermod -aG bluetooth "$RUN_USER" 2>/dev/null || true
  echo "Added user '$RUN_USER' to group 'bluetooth'. Log out and back in (or reboot) if BLE access is denied."
fi

echo "[2/7] Creating virtual environment"
python3 -m venv "$PROJECT_DIR/.venv"

echo "[3/7] Installing Python dependencies"
"$PROJECT_DIR/.venv/bin/pip" install --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

echo "[4/7] Ensuring runtime files"
mkdir -p "$PROJECT_DIR/logs"
chmod +x "$PROJECT_DIR/growcontrol"
chmod +x "$PROJECT_DIR/deploy_web.sh" || true
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  echo "Created $PROJECT_DIR/.env (please set OPENWEATHER_API_KEY)"
fi

if [[ ! -f "$PROJECT_DIR/config/sensors.json" ]]; then
  cp "$PROJECT_DIR/config/sensors.example.json" "$PROJECT_DIR/config/sensors.json"
  echo "Created config/sensors.json from sensors.example.json (add sensors via Options or CLI)."
fi
if [[ ! -f "$PROJECT_DIR/config/settings.json" ]]; then
  cp "$PROJECT_DIR/config/settings.example.json" "$PROJECT_DIR/config/settings.json"
  echo "Created config/settings.json from settings.example.json (set weather if desired)."
fi

if [[ -f "$PROJECT_DIR/growcontrol" ]]; then
  sudo ln -sf "$PROJECT_DIR/growcontrol" /usr/local/bin/growcontrol
fi

echo "[5/7] Deploying web files"
if [[ -d "$WEB_SOURCE_DIR" ]]; then
  sudo mkdir -p "$WEB_TARGET_DIR"
  sudo chown -R "$RUN_USER":"$RUN_GROUP" "$WEB_TARGET_DIR"
  sudo rsync -a --exclude 'Sensor_*' --exclude 'weatherdata' --exclude 'status.json' "$WEB_SOURCE_DIR/" "$WEB_TARGET_DIR/"
  sudo find "$WEB_TARGET_DIR" -type d -exec chmod 775 {} \;
  sudo find "$WEB_TARGET_DIR" -type f -exec chmod 664 {} \;
  echo "Web UI deployed to $WEB_TARGET_DIR"
else
  echo "Warning: $WEB_SOURCE_DIR not found, skipping web deployment"
fi

echo "[6/7] Installing systemd services"
sed \
  -e "s|/usr/bin/python3|$PROJECT_DIR/.venv/bin/python|g" \
  -e "s|/home/pi/Growcontrol|$PROJECT_DIR|g" \
  -e "s|^User=.*|User=$RUN_USER|g" \
  "$SERVICE_TEMPLATE" | sudo tee "$SERVICE_DEST" >/dev/null

sed \
  -e "s|/usr/bin/python3|$PROJECT_DIR/.venv/bin/python|g" \
  -e "s|/home/pi/Growcontrol|$PROJECT_DIR|g" \
  -e "s|^User=.*|User=$RUN_USER|g" \
  "$WEBAPI_TEMPLATE" | sudo tee "$WEBAPI_DEST" >/dev/null

# Optional daily git update check (writes update_status.json for the UI footer)
if [[ -f "$UPDATE_CHECK_SERVICE_TEMPLATE" ]] && [[ -f "$UPDATE_CHECK_TIMER_TEMPLATE" ]]; then
  sed -e "s|/home/pi/Growcontrol|$PROJECT_DIR|g" -e "s|^User=.*|User=$RUN_USER|g" \
    "$UPDATE_CHECK_SERVICE_TEMPLATE" | sudo tee "$UPDATE_CHECK_SERVICE_DEST" >/dev/null
  sudo cp "$UPDATE_CHECK_TIMER_TEMPLATE" "$UPDATE_CHECK_TIMER_DEST"
fi

if [[ -f "$NGINX_API_TEMPLATE" ]]; then
  sudo cp "$NGINX_API_TEMPLATE" "$NGINX_API_SNIPPET"
  if ! sudo grep -q "include /etc/nginx/snippets/growcontrol-api.conf;" "$NGINX_DEFAULT_SITE"; then
    sudo sed -i "/server_name _;/a\\
    include /etc/nginx/snippets/growcontrol-api.conf;" "$NGINX_DEFAULT_SITE"
  fi
fi

sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable growcontrol-collector.service
sudo systemctl enable growcontrol-webapi.service
sudo systemctl enable nginx
if [[ -f "$UPDATE_CHECK_TIMER_DEST" ]]; then
  sudo systemctl enable growcontrol-update-check.timer
fi

echo "[7/7] Starting services and running health checks"
sudo systemctl restart nginx
sudo systemctl restart growcontrol-collector.service
sudo systemctl restart growcontrol-webapi.service
if [[ -f "$UPDATE_CHECK_TIMER_DEST" ]]; then
  sudo systemctl start growcontrol-update-check.timer || true
fi

if command -v growcontrol >/dev/null 2>&1; then
  growcontrol doctor --strict || true
fi

echo "Installation complete."
echo "Open: http://$(hostname -I | awk '{print $1}')/growcontrol/Dashboard.html"
echo "Troubleshooting:"
echo "  sudo systemctl status nginx growcontrol-collector.service growcontrol-webapi.service"
echo "  journalctl -u growcontrol-collector.service -f"
echo "  journalctl -u growcontrol-webapi.service -f"
