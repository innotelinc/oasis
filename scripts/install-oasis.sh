#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="${OASIS_INSTALL_ROOT:-/opt/oasis}"
SYSTEMD_DIR="/etc/systemd/system"

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
command -v systemctl >/dev/null 2>&1 || { echo "systemd is required." >&2; exit 1; }

install -d -m 0750 "$INSTALL_ROOT" "$INSTALL_ROOT/backups"
rsync -a --delete --exclude '.env' --exclude 'backups/' "$ROOT/" "$INSTALL_ROOT/"
[ -f "$INSTALL_ROOT/.env" ] || install -m 0600 /dev/null "$INSTALL_ROOT/.env"

sed "s#__OASIS_ROOT__#$INSTALL_ROOT#g" "$ROOT/systemd/oasis.service" > "$SYSTEMD_DIR/oasis.service"
sed "s#__OASIS_ROOT__#$INSTALL_ROOT#g" "$ROOT/systemd/oasis-backup.service" > "$SYSTEMD_DIR/oasis-backup.service"
install -m 0644 "$ROOT/systemd/oasis-backup.timer" "$SYSTEMD_DIR/oasis-backup.timer"
systemctl daemon-reload
systemctl enable oasis.service oasis-backup.timer
systemctl start oasis.service oasis-backup.timer

echo "Oasis installed at $INSTALL_ROOT. Copy production .env into that directory before exposing the host."
