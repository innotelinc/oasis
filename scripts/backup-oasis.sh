#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
COMPOSE="${COMPOSE:-docker compose}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_DIR}/${STAMP}"

mkdir -p "${DEST}"
umask 077

cleanup() { rm -rf "${DEST}"; }
trap cleanup ERR

info() { printf '[backup] %s\n' "$*"; }

info "Checking service readiness"
${COMPOSE} exec -T postgres pg_isready >/dev/null
${COMPOSE} exec -T redis redis-cli --no-auth-warning -a "${REDIS_PASSWORD:?Set REDIS_PASSWORD}" ping | grep -q PONG

info "Backing up PostgreSQL"
${COMPOSE} exec -T postgres pg_dump \
  --format=custom \
  --no-owner \
  --no-privileges \
  -U "${POSTGRES_USER:-authentik}" \
  -d "${POSTGRES_DB:-authentik}" > "${DEST}/postgres.dump"

info "Backing up Redis"
${COMPOSE} exec -T redis redis-cli --no-auth-warning -a "${REDIS_PASSWORD}" BGSAVE >/dev/null
sleep "${REDIS_SAVE_WAIT_SECONDS:-2}"
${COMPOSE} cp redis:/data/dump.rdb "${DEST}/redis.rdb"

info "Backing up Authentik media and templates"
${COMPOSE} cp authentik-server:/media "${DEST}/media"
${COMPOSE} cp authentik-server:/templates "${DEST}/templates"

cat > "${DEST}/manifest.txt" <<EOF
created_at=${STAMP}
postgres_database=${POSTGRES_DB:-authentik}
postgres_user=${POSTGRES_USER:-authentik}
compose_project=${COMPOSE_PROJECT_NAME:-oasis}
EOF

if command -v sha256sum >/dev/null 2>&1; then
  (cd "${DEST}" && sha256sum postgres.dump redis.rdb manifest.txt > SHA256SUMS)
fi

ARCHIVE="${BACKUP_DIR}/oasis-${STAMP}.tar.gz"
tar -czf "${ARCHIVE}" -C "${BACKUP_DIR}" "${STAMP}"
rm -rf "${DEST}"
find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'oasis-*.tar.gz' -mtime +"${RETENTION_DAYS}" -delete

info "Backup written to ${ARCHIVE}"
info "Encrypt or upload this archive using your organization's protected backup storage."
