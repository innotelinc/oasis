#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${OASIS_INSTALL_ROOT:-/opt/oasis}"

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
[ "${CONFIRM_UNINSTALL:-}" = yes ] || {
  echo "Set CONFIRM_UNINSTALL=yes to remove Oasis services and files." >&2
  echo "Data volumes and $INSTALL_ROOT/backups are preserved unless PURGE_DATA=yes." >&2
  exit 1
}

systemctl disable --now oasis-backup.timer oasis.service 2>/dev/null || true
rm -f /etc/systemd/system/oasis.service \
      /etc/systemd/system/oasis-backup.service \
      /etc/systemd/system/oasis-backup.timer
systemctl daemon-reload

if [ -d "$INSTALL_ROOT" ]; then
  if [ "${PURGE_DATA:-no}" = yes ]; then
    (cd "$INSTALL_ROOT" && docker compose down --volumes --remove-orphans 2>/dev/null || true)
    rm -rf "$INSTALL_ROOT"
  else
    (cd "$INSTALL_ROOT" && docker compose down --remove-orphans 2>/dev/null || true)
    echo "Preserved $INSTALL_ROOT, including .env and backups."
  fi
fi

echo "Oasis systemd services removed."
