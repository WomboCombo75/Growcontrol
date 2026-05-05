#!/bin/sh
#
# Start mjpg-streamer (UVC + HTTP by default; override with -i / -o or MJPG_* env vars).
# Works from any current working directory.
#
# Install root = directory that contains: mjpg_streamer, *.so plugins, www/
#
# Resolution order for install root:
#   1) MJPG_STREAMER_ROOT  (use when this script lives in another project/repo)
#   2) directory of this script (typical: script stays next to mjpg_streamer after "make")
#
# Example from elsewhere:
#   MJPG_STREAMER_ROOT=/home/pi/legacy/mjpg-streamer/mjpg-streamer-experimental /path/to/start.sh
#
# Optional env (passed through to mjpg_streamer): MJPG_DEVICE MJPG_RESOLUTION MJPG_FPS
# MJPG_WWW MJPG_PORT MJPG_INPUT MJPG_INPUT_PLUGIN
#
# SPDX-License-Identifier: GPL-2.0-only
# (mjpg-streamer is GPL-2; this launcher is a thin wrapper.)

# Directory containing this script (absolute).
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Streamer install / build directory.
if [ -n "${MJPG_STREAMER_ROOT}" ]; then
  STREAMER_ROOT="${MJPG_STREAMER_ROOT}"
else
  STREAMER_ROOT="${SCRIPT_DIR}"
fi

# Normalize to absolute path when MJPG_STREAMER_ROOT was relative.
case "${STREAMER_ROOT}" in
  /*) ;;
  *) STREAMER_ROOT=$(CDPATH= cd -- "${STREAMER_ROOT}" && pwd) ;;
esac

if [ ! -x "${STREAMER_ROOT}/mjpg_streamer" ]; then
  printf '%s\n' "start.sh: no executable ${STREAMER_ROOT}/mjpg_streamer" >&2
  printf '%s\n' "Set MJPG_STREAMER_ROOT to the folder built by 'make' (contains mjpg_streamer, *.so, www/)." >&2
  exit 1
fi

cd "${STREAMER_ROOT}" || exit 1

# Plugins are loaded from cwd-relative paths by default; also help dlopen().
export LD_LIBRARY_PATH="${STREAMER_ROOT}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

exec "${STREAMER_ROOT}/mjpg_streamer" "$@"
