#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${OASIS_ENV_FILE:-$ROOT/.env}"
ENV_EXAMPLE="$ROOT/.env.example"
MODE="${OASIS_MODE:-local}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}PASS${NC} $*"; }
warn() { echo -e "${YELLOW}WARN${NC} $*"; }
fail() { echo -e "${RED}FAIL${NC} $*"; exit 1; }

case "$MODE" in
  local|production) ;;
  *) fail "OASIS_MODE must be local or production" ;;
esac

command -v docker >/dev/null 2>&1 || fail "docker not found"
command -v openssl >/dev/null 2>&1 || fail "openssl not found"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"
pass "Docker and Compose prerequisites available"

if [ ! -f "$ENV_FILE" ]; then
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  replace_var() {
    local key="$1" value="$2"
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  }
  replace_var POSTGRES_PASSWORD "$(openssl rand -hex 32)"
  replace_var REDIS_PASSWORD "$(openssl rand -hex 32)"
  replace_var AUTHENTIK_SECRET_KEY "$(openssl rand -hex 32)"
  pass "Created .env with generated secrets"
else
  chmod 600 "$ENV_FILE"
  pass "Using existing .env without replacing secrets"
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

if grep -Eq 'replace-with-|^POSTGRES_PASSWORD=$|^REDIS_PASSWORD=$|^AUTHENTIK_SECRET_KEY=$' "$ENV_FILE"; then
  fail "Replace all sample secrets in $ENV_FILE before starting Oasis"
fi

mkdir -p "$ROOT/certs" "$ROOT/backups"
if [ "$MODE" = production ]; then
  [ -n "${ACME_EMAIL:-}" ] || fail "ACME_EMAIL is required in production mode"
  pass "Production mode selected; run scripts/init-certificates.sh after startup"
else
  pass "Local mode selected; HTTP endpoint will be available on localhost:8080"
fi

cd "$ROOT"
docker compose --profile "$MODE" config >/dev/null
# Do not auto-start production before DNS/certificate prerequisites are checked.
if [ "${OASIS_START:-true}" = true ]; then
  docker compose --profile "$MODE" up -d
  pass "Oasis services started with profile: $MODE"
fi

echo
echo "Next steps:"
if [ "$MODE" = production ]; then
  echo "  scripts/init-certificates.sh"
  echo "  Open https://${AUTH_DOMAIN:-auth.oasis.innotel.us}/if/initial-setup/"
else
  echo "  Open http://localhost:8080/if/initial-setup/"
fi
