#!/usr/bin/env bash
set -euo pipefail

: "${WEB_HOST:=127.0.0.1}"
: "${WEB_PORT:=3000}"
: "${GLADOS_DATA_DIR:=/var/lib/glados-checkin}"
: "${GLADOS_BROWSER_PROFILE_DIR:=$GLADOS_DATA_DIR/profiles}"
export WEB_HOST WEB_PORT GLADOS_DATA_DIR GLADOS_BROWSER_PROFILE_DIR
mkdir -p "$GLADOS_BROWSER_PROFILE_DIR"
cd /opt/glados-checkin
command=(/opt/glados-checkin/.venv/bin/python -m uvicorn web_service:app --host "$WEB_HOST" --port "$WEB_PORT")

# Keep a private virtual display available for the UI's headed-xvfb diagnostics
# while Chromium still runs headless by default. No VNC or display port is opened.
exec /usr/bin/xvfb-run --auto-servernum --server-args='-screen 0 1366x900x24 -nolisten tcp' "${command[@]}"
