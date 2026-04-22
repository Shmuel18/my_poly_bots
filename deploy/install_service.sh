#!/usr/bin/env bash
# Install the polybot systemd service so it auto-starts on reboot.
# Run with sudo: sudo bash deploy/install_service.sh

set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "Please run with sudo: sudo bash deploy/install_service.sh" >&2
  exit 1
fi

TARGET_USER="${SUDO_USER:-$USER}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_SRC="$SCRIPT_DIR/polybot.service"
SERVICE_DST="/etc/systemd/system/polybot.service"

if [ ! -f "$SERVICE_SRC" ]; then
  echo "Missing $SERVICE_SRC" >&2
  exit 2
fi

echo "[install] Writing service for user=$TARGET_USER home=$TARGET_HOME"
sed -e "s|%USER%|$TARGET_USER|g" -e "s|%HOME%|$TARGET_HOME|g" \
    "$SERVICE_SRC" > "$SERVICE_DST"

mkdir -p "$TARGET_HOME/my_poly_bots/logs"
chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/my_poly_bots/logs"

systemctl daemon-reload
systemctl enable polybot.service
systemctl restart polybot.service

sleep 2
echo
echo "[install] Status:"
systemctl --no-pager --full status polybot.service || true

cat <<EOF

Useful commands:
  journalctl -u polybot -f              # tail logs live
  sudo systemctl stop polybot           # stop
  sudo systemctl start polybot          # start
  sudo systemctl restart polybot        # restart after code pull
  sudo systemctl disable polybot        # don't autostart on reboot
EOF
