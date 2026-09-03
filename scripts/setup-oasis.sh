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

# Nginx Proxy Manager edge integration. When NPM credentials are configured,
# provision the Oasis hostnames (app, api, auth, mail, files, admin) and the
# Let's Encrypt certificate through the NPM API. Skipped in local mode where
# no public DNS or NPM instance exists. Never fails setup: NPM is an external
# dependency that may legitimately be provisioned later.
if [ "$MODE" = production ] && [ "${OASIS_NPM_SYNC:-auto}" != false ]; then
  if [ -n "${NPM_API_TOKEN:-}${NPM_ADMIN_EMAIL:-}${NPM_ADMIN_PASSWORD:-}" ]; then
    warn "Synchronizing Oasis hostnames and certificates with Nginx Proxy Manager..."
    if python3 "$ROOT/scripts/npm-proxy-hosts.py" --check; then
      python3 "$ROOT/scripts/npm-proxy-hosts.py" \
        || warn "NPM provisioning reported errors; rerun scripts/npm-proxy-hosts.py after fixing them"
    else
      warn "NPM drift check failed (is NPM reachable at ${NPM_API_URL:-http://127.0.0.1:81}?); skipping host provisioning"
    fi
  else
    warn "NPM credentials not configured (NPM_API_TOKEN or NPM_ADMIN_EMAIL/NPM_ADMIN_PASSWORD); skipping host provisioning"
  fi
elif [ "$MODE" = local ]; then
  warn "Local mode: Nginx Proxy Manager host provisioning is only run in production mode"
fi

echo
echo "Next steps:"
if [ "$MODE" = production ]; then
  echo "  scripts/init-certificates.sh"
  echo "  scripts/oasis-health.sh --report   # domain/MX/SPF/DKIM/DMARC/TLS health and compliance report"
  echo "  Open https://${AUTH_DOMAIN:-auth.oasis.innotel.us}/if/initial-setup/"
else
  echo "  Open http://localhost:8080/if/initial-setup/"
fi

# ── Infisical (SecretOps) — opt-in secret provisioning ──────────────
# Secrets for the Innotel Platform Stack live in Infisical. Enable by
# setting INFISICAL_ADMIN_EMAIL / INFISICAL_ADMIN_PASSWORD and the
# INFISICAL_* keys in .env, then re-run setup (idempotent).
if grep -qE '^INFISICAL_ADMIN_EMAIL=.+' .env 2>/dev/null && \
   grep -qE '^INFISICAL_ADMIN_PASSWORD=.+' .env 2>/dev/null; then
  __root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
  case "$__root" in
    */scripts) __root="$(dirname "$__root")" ;;
  esac
  if [ -f "$__root/scripts/infisical-setup.sh" ]; then
    echo ">> provisioning secrets into Infisical (SecretOps)..."
    bash "$__root/scripts/infisical-setup.sh" \
      || echo "!! infisical setup failed (see above); .env values remain valid" >&2
  fi
  unset __root
fi
