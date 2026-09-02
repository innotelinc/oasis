#!/usr/bin/env bash
set -euo pipefail

ARCHIVE="${1:-}"
COMPOSE="${COMPOSE:-docker compose}"
WORKDIR=""

if [ -z "${ARCHIVE}" ] || [ ! -f "${ARCHIVE}" ]; then
  echo "Usage: $0 /path/to/oasis-YYYYmmddTHHMMSSZ.tar.gz" >&2
  exit 1
fi

if [ "${CONFIRM_RESTORE:-}" != "yes" ]; then
  echo "Restore replaces the current PostgreSQL database, Redis data, and Authentik media." >&2
  echo "Create a fresh backup, stop dependent applications, review the archive, then rerun with CONFIRM_RESTORE=yes." >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

tar -xzf "${ARCHIVE}" -C "${WORKDIR}"
ROOT="$(find "${WORKDIR}" -mindepth 1 -maxdepth 1 -type d -print -quit)"
[ -n "${ROOT}" ] || { echo "Invalid backup archive" >&2; exit 1; }

if [ -f "${ROOT}/SHA256SUMS" ] && command -v sha256sum >/dev/null 2>&1; then
  (cd "${ROOT}" && sha256sum -c SHA256SUMS)
fi

printf 'Stopping Authentik services...\n'
${COMPOSE} stop authentik-server authentik-worker

printf 'Restoring PostgreSQL...\n'
${COMPOSE} exec -T postgres dropdb --if-exists -U "${POSTGRES_USER:-authentik}" "${POSTGRES_DB:-authentik}"
${COMPOSE} exec -T postgres createdb -U "${POSTGRES_USER:-authentik}" "${POSTGRES_DB:-authentik}"
cat "${ROOT}/postgres.dump" | ${COMPOSE} exec -T postgres pg_restore \
  --clean --if-exists --no-owner --no-privileges \
  -U "${POSTGRES_USER:-authentik}" -d "${POSTGRES_DB:-authentik}"

printf 'Restoring Redis...\n'
${COMPOSE} stop redis
${COMPOSE} cp "${ROOT}/redis.rdb" redis:/data/dump.rdb
${COMPOSE} start redis

printf 'Restoring Authentik media and templates...\n'
${COMPOSE} cp "${ROOT}/media/." authentik-server:/media/
${COMPOSE} cp "${ROOT}/templates/." authentik-server:/templates/

${COMPOSE} start authentik-server authentik-worker
printf 'Restore complete. Verify Authentik login and application integrations before reopening traffic.\n'
