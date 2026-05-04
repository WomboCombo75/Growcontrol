# Growcontrol

**Growcontrol** runs on a Raspberry Pi (or similar Debian-based Linux box with Bluetooth), talks to **Xiaomi MiFlora / HHCC** plant sensors over **BLE**, and serves a **local web dashboard** for live readings, history charts, optional **OpenWeather** context, and **browser-based onboarding** (scan, verify, add sensors).

No cloud account is required for core operation: data stays on your machine and LAN.

---

## What you get

| Feature | Description |
|--------|-------------|
| **Collector** | `systemd` service polls sensors on a schedule, writes SQLite history and live JSON for the UI. |
| **Web UI** | Dashboard (overview, charts, weather) and Options (sensors, retention, polling, weather, API key). |
| **REST API** | Local HTTP API behind nginx (`/growcontrol-api/`) for the UI and CLI. |
| **CLI** | `growcontrol` command for scans, add/remove/edit sensors, and `doctor` diagnostics. |
| **Storage** | SQLite time-series (`data/growcontrol.db`) with configurable retention; short rolling text blocks per sensor under the web docroot. |

---

## Requirements

- **OS:** Raspberry Pi OS or Debian-based Linux with `apt-get`.
- **Hardware:** Bluetooth adapter (built-in Pi BT is fine) in range of MiFlora-style sensors.
- **User:** Run the installer as the same Linux user that should own the services (usually **`pi`**). That user is added to the **`bluetooth`** group; **log out and back in** (or reboot) once if BLE permission errors appear.
- **Network:** Optional; only needed for `git clone` / `curl` during install and for OpenWeather if you enable it.

---

## Install (Pi-hole style, one command)

Run as your normal login user (example **`pi`**), **not** a root-only SSH session:

```bash
curl -sSL https://raw.githubusercontent.com/WomboCombo75/Growcontrol/main/install.sh | bash
```

This script:

1. Installs **`git`** and **`curl`** if needed (via `sudo`).
2. **Clones** (or updates) the repo into **`$HOME/Growcontrol`** by default.
3. Runs **`install_phase1.sh`**, which installs Python/nginx/BlueZ dependencies, creates **`.venv`**, deploys the **web UI** to **`/var/www/html/growcontrol`**, installs **systemd** units, configures **nginx** for **`/growcontrol-api/`**, enables services, and starts them.

When it finishes, open:

**`http://<your-pi-ip>/growcontrol/Dashboard.html`**

(Use `hostname -I` on the Pi to see addresses.)

### Optional environment variables

| Variable | Meaning |
|----------|---------|
| `GROWCONTROL_DIR` | Install path (default: `$HOME/Growcontrol`). |
| `GROWCONTROL_BRANCH` | Git branch (default: `main`). |
| `GROWCONTROL_REPO` | Git URL (default: this project’s GitHub HTTPS URL). |
| `GROWCONTROL_SKIP_SYSTEM` | Set to `1` to only clone/update and **not** run apt/systemd (for developers). |

Example: install from a **fork**:

```bash
export GROWCONTROL_REPO="https://github.com/<you>/Growcontrol.git"
curl -sSL https://raw.githubusercontent.com/<you>/Growcontrol/main/install.sh | bash
```

Until **`install.sh`** exists on the branch you reference, the `raw.githubusercontent.com` one-liner will fail—use the **manual** path below, or from a clone directory run **`./install.sh`** (same logic as `curl | bash`).

Until your changes are **pushed to GitHub**, the public `curl` URL will not serve your latest `install.sh`; use **manual install** or **`./install.sh`** from a local clone.

### Manual install (clone + installer)

```bash
git clone https://github.com/WomboCombo75/Growcontrol.git
cd Growcontrol
./install_phase1.sh
```

Same end state as the one-liner.

---

## How it works

1. **`growcontrol-collector.service`** runs `growcontrol_collector.py` in a loop: aligned sensor cycles, bounded parallel BLE reads, optional weather fetch, writes **`status.json`**, per-sensor **`Sensor_*`** files, **`weatherdata`**, and appends rows to **SQLite**.
2. **`growcontrol-webapi.service`** runs `growcontrol_webapi.py` on **127.0.0.1:8788** (by default): sensors CRUD, BLE scan/verify, history API, settings.
3. **nginx** serves static files from **`/var/www/html/growcontrol/`** and reverse-proxies **`/growcontrol-api/`** to the web API.
4. The **browser** loads HTML/JS from nginx and calls the API with optional **`X-API-Key`** when you configure one.

```text
[ MiFlora BLE ] → collector → SQLite (history) + status.json / Sensor_* (live)
                                    ↓
[ Browser ] ← nginx (static + /growcontrol-api/) ← web API
```

---

## Project layout (after clone)

| Path | Role |
|------|------|
| `growcontrol_collector.py` | BLE + weather loop, writes DB and docroot files. |
| `growcontrol_webapi.py` | HTTP API for UI and CLI. |
| `growcontrol_cli.py` / `growcontrol` | Command-line helper. |
| `growcontrol_storage.py` | SQLite schema, inserts, retention. |
| `config/settings.json` | Polling, paths, weather coordinates, etc. |
| `config/sensors.json` | Sensor registry (id, name, MAC, `output_file`). |
| `web/*.html` | Dashboard, Options, redirect stub. |
| `deploy/*.service`, `deploy/nginx-*.conf` | Templates copied/adapted by `install_phase1.sh`. |
| `data/growcontrol.db` | Created at runtime: historical sensor + weather readings. |
| `logs/growcontrol.log` | Collector log file (path from settings). |
| `legacy/` | Old PHP rotation helpers and optional **mjpg-streamer** tree (not used by the current stack). |

---

## Where data is stored

| Data | Location |
|------|----------|
| **History** (charts, retention) | `database_path` in `config/settings.json` — default **`data/growcontrol.db`** under the project directory (e.g. `~/Growcontrol/data/`). |
| **Live status** for the UI | `output_dir` + `status_file` — default **`/var/www/html/growcontrol/status.json`**. |
| **Per-sensor text blocks** (short rolling log) | `output_dir` + each sensor’s **`output_file`** (e.g. **`Sensor_1`**). |
| **Weather JSON snapshot** | `output_dir` + **`weatherdata`**. |

---

## Configuration highlights

- **Templates in git:** **`config/settings.example.json`** and **`config/sensors.example.json`**. On first install, **`install_phase1.sh`** copies them to **`config/settings.json`** and **`config/sensors.json`** if those files do not exist yet. The real JSON files are **gitignored** so your MAC addresses, API keys, and coordinates are never pushed.
- **`config/settings.json`** (local) — `sensor_poll_minutes`, `sensor_parallelism`, `output_dir`, `database_path`, OpenWeather fields, etc.
- **`.env`** — optional `OPENWEATHER_API_KEY`, `GROWCONTROL_API_KEY`, overrides. Created from **`.env.example`** on first install if missing (also gitignored once created if you add `.env` — already ignored).
- **Optional write lock** — if `GROWCONTROL_API_KEY` is set, write operations from the API require the same value in the **`X-API-Key`** header; the Options page stores a copy in **browser `localStorage`** for convenience.

Weather: enable in **`config/settings.json`**, set coordinates and units, and provide an API key in **`.env`** or settings (see Options UI).

---

## Updating

- **HTML/CSS/JS only:** from the project directory run **`./deploy_web.sh`** (safe rsync: excludes `status.json`, `weatherdata`, `Sensor_*` on the server).
- **Full code update:** run the **curl installer again**; it resets the clone to **`origin/main`** (or your `GROWCONTROL_BRANCH`). Local uncommitted changes in the install directory can be overwritten—use a manual `git pull` workflow if you develop in-place.

---

## CLI quick reference

```bash
growcontrol doctor [--strict]
growcontrol add-sensor --scan
growcontrol add-sensor --id plant_1 --name "My Plant" --mac AA:BB:CC:DD:EE:FF [--restart-service]
growcontrol list-sensors
growcontrol edit-sensor --id plant_1 --name "Tomato" --enabled [--restart-service]
growcontrol remove-sensor --id plant_1 [--restart-service]
```

Manual MAC discovery (if needed): `sudo hcitool lescan`

---

## Web onboarding

1. Open **`http://<pi-ip>/growcontrol/Dashboard.html`**
2. Go to **Options**
3. **Scan BLE Devices** → pick a device → **Verify Sensor** (recommended)
4. Enter **Sensor ID** and **Name** → **Add Sensor**

You can also tune retention, polling, parallelism, and weather from Options.

**Dashboard display → Chart time zone** uses **IANA** zones (stored in the browser) so chart labels match your local time.

---

## Troubleshooting

```bash
sudo systemctl status nginx growcontrol-collector.service growcontrol-webapi.service
journalctl -u growcontrol-collector.service -f
journalctl -u growcontrol-webapi.service -f
sudo nginx -t
```

- **BLE permission denied:** ensure the service user is in group **`bluetooth`** and re-login after `install_phase1.sh`.
- **404 on Dashboard:** confirm **`/var/www/html/growcontrol/`** exists and nginx default site serves **`/var/www/html/`**.
- **API 401:** set the API key in Options / `.env` to match `GROWCONTROL_API_KEY`.

---

## Upgrading from older `/weed/` docroot

If you previously used **`/var/www/html/weed`**, set **`output_dir`** to **`/var/www/html/growcontrol`**, run **`./deploy_web.sh`**, move any **`status.json` / `weatherdata` / `Sensor_*`** you care about into the new folder, then remove the old **`weed`** directory. Use URLs under **`/growcontrol/`**.

---

## Security note

Treat the Pi and your LAN as the trust boundary. The default API is bound to **localhost** and reached through nginx; restrict access at the network layer if exposing the Pi beyond your home.

---

## License

Add your preferred **LICENSE** file in the repository root if you distribute this project publicly.
