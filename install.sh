#!/usr/bin/env bash
#
# Growcontrol one-line installer (Pi-hole style).
# Fetches this project from GitHub and runs the full system installer.
#
# Usage (recommended: run as your normal user, e.g. pi — not a root login shell):
#   curl -sSL https://raw.githubusercontent.com/WomboCombo75/Growcontrol/main/install.sh | bash
#
# Optional environment variables:
#   GROWCONTROL_REPO   Git clone URL (default: official repo HTTPS URL below)
#   GROWCONTROL_BRANCH Branch to install (default: main)
#   GROWCONTROL_DIR    Install location (default: $HOME/Growcontrol)
#   GROWCONTROL_SKIP_SYSTEM  Set to 1 to only clone/update and skip apt/systemd (for developers)
#
set -euo pipefail

GROWCONTROL_REPO="${GROWCONTROL_REPO:-https://github.com/WomboCombo75/Growcontrol.git}"
GROWCONTROL_BRANCH="${GROWCONTROL_BRANCH:-main}"
GROWCONTROL_DIR="${GROWCONTROL_DIR:-$HOME/Growcontrol}"
GROWCONTROL_SKIP_SYSTEM="${GROWCONTROL_SKIP_SYSTEM:-0}"

RED='\033[0;31m'
GRN='\033[0;32m'
BLU='\033[0;34m'
NC='\033[0m'

log() { echo -e "${BLU}[*]${NC} $*"; }
ok() { echo -e "${GRN}[✓]${NC} $*"; }
err() { echo -e "${RED}[!]${NC} $*" >&2; }

need_cmd() {
  local c="$1"
  command -v "$c" >/dev/null 2>&1 || return 1
  return 0
}

prompt_yes_no() {
  local question="$1"
  local default_answer="${2:-N}"
  local prompt="[y/N]"
  [[ "$default_answer" =~ ^[Yy]$ ]] && prompt="[Y/n]"
  local reply=""
  while true; do
    read -r -p "$question $prompt " reply || true
    reply="${reply:-$default_answer}"
    case "$reply" in
      [Yy]|[Yy][Ee][Ss]) return 0 ;;
      [Nn]|[Nn][Oo]) return 1 ;;
      *) echo "Please answer yes or no." ;;
    esac
  done
}

ensure_git_curl() {
  if need_cmd git && need_cmd curl; then
    return 0
  fi
  if ! need_cmd sudo; then
    err "Need 'git' and 'curl'. Install them, or install 'sudo' and re-run."
    exit 1
  fi
  log "Installing git and curl…"
  sudo apt-get update -qq
  sudo apt-get install -y -qq git curl ca-certificates
  ok "git and curl are available."
}

if [[ "$(uname -s)" != "Linux" ]]; then
  err "Growcontrol is intended for Linux (Raspberry Pi OS / Debian)."
  exit 1
fi

if ! need_cmd apt-get; then
  err "This installer expects apt-get (Debian / Raspberry Pi OS)."
  exit 1
fi

log "Growcontrol installer"
log "Repository: $GROWCONTROL_REPO (branch: $GROWCONTROL_BRANCH)"
log "Install directory: $GROWCONTROL_DIR"

if [[ "$GROWCONTROL_SKIP_SYSTEM" != "1" ]] && [[ -t 0 ]]; then
  if prompt_yes_no "Are you installing in WSL?" "N"; then
    GROWCONTROL_SKIP_SYSTEM="1"
    log "WSL mode enabled: will clone/update project and skip system service install."
  fi
fi

ensure_git_curl

if [[ "$GROWCONTROL_DIR" =~ [[:space:]] ]]; then
  err "GROWCONTROL_DIR must not contain spaces."
  exit 1
fi

if [[ -e "$GROWCONTROL_DIR" ]] && [[ ! -d "$GROWCONTROL_DIR/.git" ]]; then
  err "Path already exists and is not a git clone: $GROWCONTROL_DIR"
  err "Remove it or choose another path: GROWCONTROL_DIR=/path/to/Growcontrol curl … | bash"
  exit 1
fi

if [[ -d "$GROWCONTROL_DIR/.git" ]]; then
  log "Updating existing clone in $GROWCONTROL_DIR …"
  git -C "$GROWCONTROL_DIR" fetch --depth 1 origin "$GROWCONTROL_BRANCH"
  git -C "$GROWCONTROL_DIR" reset --hard "origin/$GROWCONTROL_BRANCH"
  ok "Repository updated."
else
  log "Cloning into $GROWCONTROL_DIR …"
  parent="$(dirname "$GROWCONTROL_DIR")"
  mkdir -p "$parent"
  git clone --depth 1 --branch "$GROWCONTROL_BRANCH" "$GROWCONTROL_REPO" "$GROWCONTROL_DIR"
  ok "Clone complete."
fi

export PROJECT_DIR="$GROWCONTROL_DIR"

if [[ "$GROWCONTROL_SKIP_SYSTEM" == "1" ]]; then
  ok "GROWCONTROL_SKIP_SYSTEM=1 — skipping install_phase1.sh (system packages / systemd)."
  echo "Project is at: $PROJECT_DIR"
  exit 0
fi

if [[ ! -f "$PROJECT_DIR/install_phase1.sh" ]]; then
  err "Missing install_phase1.sh in $PROJECT_DIR"
  exit 1
fi

log "Running full installer (packages, venv, nginx, systemd)…"
bash "$PROJECT_DIR/install_phase1.sh"
ok "All done."
