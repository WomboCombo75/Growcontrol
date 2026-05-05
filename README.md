# Growcontrol

**Local plant monitoring dashboard for Raspberry Pi + Bluetooth plant sensors — track multiple values over time with charts, no cloud required.**

## What it does

Growcontrol turns a Raspberry Pi into a simple, local “plant monitor”: it polls BLE plant sensors, stores history, and shows an easy dashboard from your phone or PC. Everything stays on your LAN—built to monitor multiple plant values without cloud accounts.

## Key Features

- **See trends, not just numbers**: moisture, temperature, light and EC history charts
- **Know what needs attention**: quickly spot stale or erroring sensors
- **Fast onboarding in the browser**: scan → verify → add sensors
- **Optional weather context**: temperature / humidity / condition from OpenWeather
- **Optional MJPEG webcams**: attach one or more streams to sensors for live viewing
- **Built-in version + updater check**: the Dashboard footer shows version and whether updates are available (check-only; no auto-update)

## Screenshots

- **Dashboard overview**

![Dashboard overview](docs/images/dashboard-overview.png)

- **Analytics view**

![Analytics view](docs/images/dashboard-analytics.png)


## Quick Start

### Prerequisites

- Raspberry Pi OS / Debian-based Linux with `apt-get`
- Bluetooth enabled on the Pi (built-in Pi BT is fine)
- Supported BLE plant sensors (e.g. Xiaomi MiFlora / HHCC)
- Network access (for installation + optional weather)

### Install (one command)

Run as your normal login user (usually `pi`):

```bash
curl -sSL https://raw.githubusercontent.com/WomboCombo75/Growcontrol/main/install.sh | bash
```

When it finishes, open:

**`http://<your-pi-ip>/growcontrol/Dashboard.html`**

Tip: run `hostname -I` on the Pi to see its LAN IP.

### Start / restart

```bash
sudo systemctl restart growcontrol-collector.service growcontrol-webapi.service
```

### MJPEG webcam recommendation

For the best MJPEG experience, use the updated mjpg-streamer fork:
[`WomboCombo75/new-mjpg-streamer`](https://github.com/WomboCombo75/new-mjpg-streamer)

Build the `mjpg-streamer-experimental` folder, then set the install directory in **Options → Webcam**.

## Example Use Case

1. You place a Raspberry Pi in your grow tent and add a MiFlora sensor to each pot.
2. Growcontrol logs moisture + temperature over time and shows trends.
3. Temperature rises unusually fast → you notice it immediately on the dashboard trend.
4. You turn on a fan (or trigger your own automation) → temperature stabilizes.
5. Humidity drops after ventilation → you confirm the effect in the weather/metrics view.
6. You open the attached MJPEG stream to visually verify plant posture and substrate.

*(Roadmap placeholder: add actual notifications/actuation integrations.)*

## Who is this for?

- **Hobby growers** who want to monitor multiple plant values locally (no cloud)
- **Raspberry Pi users** who prefer simple, local-first dashboards
- **IoT / DIY enthusiasts** who want a focused tool for plant monitoring

## Why this instead of Home Assistant?

Home Assistant is great for a whole smart home, but it can be heavy if you only want plant monitoring.

- **Growcontrol**: focused, quick to install, purpose-built UI for plant sensors + charts
- **Home Assistant**: broader ecosystem + integrations, more setup/maintenance overhead

If you already run Home Assistant, Growcontrol can still be useful as a dedicated “plant dashboard.”

## Project Status

- **Status**: actively developed (works for real setups; still evolving)
- **Roadmap (placeholder)**:
  - Notifications (Telegram/email) for “too dry / stale / error”
  - Actuation hooks (fan/humidifier) via GPIO / MQTT
  - More sensor types and calibration tools

## Updating

- **UI only:** `./deploy_web.sh`
- **Full update:** run the installer again (or `git pull` if you manage updates manually)

## Troubleshooting (quick)

```bash
sudo systemctl status nginx growcontrol-collector.service growcontrol-webapi.service
journalctl -u growcontrol-collector.service -f
journalctl -u growcontrol-webapi.service -f
sudo nginx -t
```

## Suggested GitHub topics

`grow-automation` `raspberry-pi` `iot` `home-automation` `environment-monitoring`
