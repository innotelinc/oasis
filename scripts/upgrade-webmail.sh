#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# upgrade-webmail.sh — move the Loomix Mail client to a newer release.
#
# The webmail is built by CI in innotelinc/webmail: a green push to `main`
# publishes ghcr.io/innotelinc/webmail-release:latest plus an immutable
# :sha-<commit>. On the mail host, `bulwark-update.timer` (hourly) resolves
# `latest` to that commit tag and recreates the container, rolling back if the
# new build does not go healthy. So an update normally needs no action here.
#
# This script is for the cases the timer should not guess at:
#
#   upgrade-webmail.sh                 # what the timer would do, then do it
#   upgrade-webmail.sh --check         # report only, change nothing
#   upgrade-webmail.sh --pin <ref>     # deploy one explicit tag (manual/rollback)
#   upgrade-webmail.sh --force         # recreate even when already current
#
# Run it on the mail host (the updater reads /opt/bulwark/docker-compose.yml
# and talks to the local Docker daemon).
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

UPDATER="${OASIS_UPDATER:-/opt/oasis/scripts/bulwark-update.py}"
COMPOSE="${OASIS_WEBMAIL_COMPOSE:-/opt/bulwark/docker-compose.yml}"
PIN_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/webmail.version"

die() { echo "upgrade-webmail: $*" >&2; exit 2; }

command -v python3 >/dev/null || die "python3 not found"
[ -f "$UPDATER" ] || die "updater not found at $UPDATER (is this the mail host?)"
[ -f "$COMPOSE" ] || die "compose not found at $COMPOSE"

MODE="apply"
PIN=""
FORCE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE="check" ;;
    --pin)   shift; [ $# -gt 0 ] || die "--pin needs a tag"; PIN="$1" ;;
    --force) FORCE="--force" ;;
    -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

args=()
[ -n "$PIN" ] && args+=(--pin "$PIN")
[ -n "$FORCE" ] && args+=(--force)

if [ "$MODE" = "check" ]; then
  python3 "$UPDATER" --check
  exit $?
fi

# The timer and this script must not both be deploying at once.
python3 "$UPDATER" "${args[@]}"
rc=$?

if [ "$rc" -eq 0 ]; then
  rev="$(docker inspect bulwark --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || echo unknown)"
  echo "upgrade-webmail: bulwark is running revision $rev"
  echo "upgrade-webmail: pinned in $PIN_FILE — update it if this deploy is the new baseline"
else
  echo "upgrade-webmail: updater exited $rc (1 means it deployed and rolled back)" >&2
fi
exit "$rc"
