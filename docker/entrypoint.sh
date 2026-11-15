#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${GLADOS_DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR"
chown -R app:app "$DATA_DIR"

export HOME=/home/app
export XDG_CACHE_HOME=${XDG_CACHE_HOME:-/opt/camoufox-cache}
export DISPLAY=${DISPLAY:-:99}

echo "[docker] GLaDOS browser check-in console starting on ${WEB_HOST:-0.0.0.0}:${WEB_PORT:-8791}"
echo "[docker] Camoufox runs headed in Xvfb; every account receives a disposable profile."

exec gosu app xvfb-run \
  --auto-servernum \
  --server-args="-screen 0 ${XVFB_SCREEN:-1920x1080x24} -nolisten tcp" \
  "$@"
