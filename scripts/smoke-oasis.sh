#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${1:-local}"
BASE_URL="${OASIS_BASE_URL:-}"

case "$PROFILE" in
  local) BASE_URL="${BASE_URL:-http://localhost:8080}" ;;
  production) BASE_URL="${BASE_URL:-https://${AUTH_DOMAIN:-auth.oasis.innotel.us}}" ;;
  *) echo "Usage: $0 [local|production]" >&2; exit 2 ;;
esac

cd "$ROOT"

docker compose ps --status running >/dev/null
for service in postgres redis authentik-server authentik-worker; do
  state="$(docker compose ps --format '{{.Service}} {{.Health}}' "$service" 2>/dev/null || true)"
  case "$state" in
    *healthy*|*running*) printf 'PASS %s (%s)\n' "$service" "$state" ;;
    *) printf 'FAIL %s (%s)\n' "$service" "${state:-not running}" >&2; exit 1 ;;
  esac
done

code="$(curl -ksS -o /dev/null -w '%{http_code}' --max-time 10 "$BASE_URL/-/health/ready/" || true)"
case "$code" in
  2??|3??) printf 'PASS Authentik readiness (%s)\n' "$code" ;;
  *) printf 'FAIL Authentik readiness (%s)\n' "${code:-no response}" >&2; exit 1 ;;
esac

printf 'Oasis smoke test passed (%s)\n' "$PROFILE"
