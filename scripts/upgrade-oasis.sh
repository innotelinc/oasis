#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="${OASIS_INSTALL_ROOT:-/opt/oasis}"
PROFILE="${OASIS_MODE:-local}"

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
[ -d "$INSTALL_ROOT" ] || { echo "Oasis is not installed at $INSTALL_ROOT." >&2; exit 1; }

if [ "${CONFIRM_UPGRADE:-}" != yes ]; then
  echo "This upgrades the Compose deployment at $INSTALL_ROOT and restarts services."
  echo "Set CONFIRM_UPGRADE=yes to continue." >&2
  exit 1
fi

cd "$INSTALL_ROOT"
set -a
# shellcheck disable=SC1091
. ./.env
set +a
BACKUP_DIR="${BACKUP_DIR:-$INSTALL_ROOT/backups}" \
  POSTGRES_PASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing}" \
  REDIS_PASSWORD="${REDIS_PASSWORD:?REDIS_PASSWORD missing}" \
  ./scripts/backup-oasis.sh

rsync -a --delete --exclude '.env' --exclude 'backups/' "$ROOT/" "$INSTALL_ROOT/"
docker compose --profile "$PROFILE" pull
docker compose --profile "$PROFILE" up -d --remove-orphans
systemctl daemon-reload
systemctl restart oasis.service

echo "Oasis upgrade complete. Run scripts/smoke-oasis.sh $PROFILE to verify."
