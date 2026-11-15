#!/usr/bin/env bash
# Native Debian 13+ deployment for the Instabox standard template.
# Run from a clone of the feat/instabox-debian-native-chromium branch.
set -euo pipefail

APP_DIR=/opt/glados-checkin
APP_PARENT=/opt
DATA_DIR=/var/lib/glados-checkin
SERVICE_USER=glados
SERVICE_GROUP=glados
ENV_DIR=/etc/glados-checkin
ENV_FILE="$ENV_DIR/glados-checkin.env"
UNIT_NAME=glados-checkin.service
SOURCE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
STAGE_DIR=""
PREVIOUS_DIR=""
RELEASE_SWITCHED=false
RECOVERING=false

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo 'Run with sudo: sudo bash deploy/debian/setup.sh' >&2
  exit 1
fi

if ! command -v apt-get >/dev/null; then
  echo 'This installer requires Debian/apt.' >&2
  exit 1
fi

if [[ -e "$APP_DIR" && ! -x "$APP_DIR/deploy/debian/run-service.sh" ]] \
  && [[ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]]; then
  echo "Refusing to replace unexpected non-empty application directory: $APP_DIR" >&2
  exit 1
fi

cleanup_stage() {
  if [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]]; then
    rm -rf "$STAGE_DIR"
  fi
}
trap cleanup_stage EXIT

recover_unexpected_failure() {
  local status=$1
  if [[ "$RELEASE_SWITCHED" == true && "$RECOVERING" == false ]] \
    && [[ -n "$PREVIOUS_DIR" && -d "$PREVIOUS_DIR" ]]; then
    RECOVERING=true
    echo 'Unexpected deployment failure after release switch; restoring previous release.' >&2
    systemctl stop "$UNIT_NAME" || true
    rm -rf "$APP_DIR"
    mv "$PREVIOUS_DIR" "$APP_DIR" || true
    PREVIOUS_DIR=""
    systemctl daemon-reload || true
    systemctl start "$UNIT_NAME" || true
  fi
  exit "$status"
}
trap 'recover_unexpected_failure $?' ERR

service_port() {
  /usr/bin/python3 - "$ENV_FILE" <<'PY'
import sys
from pathlib import Path

values = {}
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip()

host = values.get("WEB_HOST", "127.0.0.1")
port = values.get("WEB_PORT", "3000")
if host not in {"127.0.0.1", "::1"}:
    raise SystemExit("WEB_HOST must remain a loopback address for this deployment branch")
if not port.isdigit() or not 1 <= int(port) <= 65535:
    raise SystemExit("WEB_PORT must be an integer between 1 and 65535")
print(port)
PY
}

rollback_release() {
  local failure_message=$1
  echo "$failure_message" >&2
  systemctl stop "$UNIT_NAME" || true
  if [[ -n "$PREVIOUS_DIR" && -d "$PREVIOUS_DIR" ]]; then
    echo "Restoring previous application release from $PREVIOUS_DIR" >&2
    rm -rf "$APP_DIR"
    mv "$PREVIOUS_DIR" "$APP_DIR"
    PREVIOUS_DIR=""
    RELEASE_SWITCHED=false
    systemctl daemon-reload
    systemctl start "$UNIT_NAME" || true
  fi
  systemctl --no-pager --full status "$UNIT_NAME" || true
  echo "Inspect: journalctl -u $UNIT_NAME -n 100 --no-pager" >&2
  exit 1
}

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl git python3 python3-venv chromium xvfb xauth fonts-liberation fonts-noto-color-emoji

if ! getent group "$SERVICE_GROUP" >/dev/null; then
  groupadd --system "$SERVICE_GROUP"
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SERVICE_GROUP" --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -o root -g root -m 0755 "$APP_PARENT" "$ENV_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 \
  "$DATA_DIR" "$DATA_DIR/profiles" "$DATA_DIR/cache" "$DATA_DIR/config"

# Build and validate a root-owned replacement before stopping the live service.
STAGE_DIR=$(mktemp -d "$APP_PARENT/.glados-checkin-stage.XXXXXX")
tar \
  --exclude=.git --exclude=.claude --exclude=secret --exclude=data --exclude=.env --exclude='.env.*' \
  --exclude=.venv --exclude=venv --exclude=__pycache__ --exclude='*.pyc' --exclude='*.log' \
  -C "$SOURCE_DIR" -cf - . | tar -C "$STAGE_DIR" -xf -
chown -R root:root "$STAGE_DIR"
find "$STAGE_DIR" -type d -exec chmod 0755 {} +
find "$STAGE_DIR" -type f -exec chmod 0644 {} +
chmod 0755 "$STAGE_DIR/deploy/debian/setup.sh" "$STAGE_DIR/deploy/debian/run-service.sh"

python3 -m venv "$STAGE_DIR/.venv"
"$STAGE_DIR/.venv/bin/pip" install --upgrade pip
"$STAGE_DIR/.venv/bin/pip" install -r "$STAGE_DIR/requirements-service.txt"

if [[ ! -f "$ENV_FILE" ]]; then
  install -o root -g "$SERVICE_GROUP" -m 0640 \
    "$STAGE_DIR/deploy/debian/glados-checkin.env.example" "$ENV_FILE"
fi
WEB_PORT=$(service_port)

if [[ -d "$APP_DIR" && -x "$APP_DIR/deploy/debian/run-service.sh" ]]; then
  PREVIOUS_DIR="$APP_PARENT/.glados-checkin-previous-$(date +%Y%m%d%H%M%S)"
  systemctl stop "$UNIT_NAME" || true
  mv "$APP_DIR" "$PREVIOUS_DIR"
  RELEASE_SWITCHED=true
fi
mv "$STAGE_DIR" "$APP_DIR"
STAGE_DIR=""
RELEASE_SWITCHED=true

install -o root -g root -m 0644 \
  "$APP_DIR/deploy/systemd/glados-checkin.service" "/etc/systemd/system/$UNIT_NAME"
systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"

for attempt in {1..15}; do
  if curl -fsS --max-time 3 "http://127.0.0.1:$WEB_PORT/api/health" >/dev/null; then
    break
  fi
  sleep 1
  if [[ $attempt -eq 15 ]]; then
    rollback_release "Service failed its loopback readiness check."
  fi
done

if ! curl -fsS --max-time 30 "http://127.0.0.1:$WEB_PORT/api/health?deep=true" >/dev/null; then
  rollback_release "Native Chromium smoke check failed under the service unit. Do not add --no-sandbox; inspect the journal before evaluating systemd sandbox compatibility."
fi

echo "GLaDOS native Chromium service is healthy on 127.0.0.1:$WEB_PORT."
if [[ -n "$PREVIOUS_DIR" ]]; then
  echo "Previous application release retained at $PREVIOUS_DIR until you remove it manually."
fi
