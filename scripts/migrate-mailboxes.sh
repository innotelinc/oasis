#!/usr/bin/env bash
# Oasis mailbox migration utility
#
# Migrates mailboxes between IMAP servers (Exchange, Google Workspace, Zimbra,
# or any IMAP4 provider) onto an Oasis-deployed mail server. Uses imapsync when
# available, otherwise the official imapsync Docker image. Passwords are read
# from passfiles or environment variables and are never placed on the command
# line.
set -euo pipefail

IMAPSYNC_IMAGE="${IMAPSYNC_IMAGE:-gilleslamiral/imapsync}"

usage() {
  cat <<'EOF'
Usage: migrate-mailboxes.sh [options]

Required:
  --source PROVIDER          exchange | google | zimbra | generic
  --source-host HOST         IMAP host of the old server
  --source-user USER         mailbox on the old server
  --target-host HOST         Oasis mail server IMAP host
  --target-user USER         mailbox on the Oasis server

Authentication (pick one per side):
  --source-passfile FILE     file containing the source mailbox password
  --target-passfile FILE     file containing the target mailbox password
  IMAP_SOURCE_PASSWORD=...   environment variable for the source mailbox
  IMAP_TARGET_PASSWORD=...   environment variable for the target mailbox

Common options:
  --source-port PORT         IMAP port (preset: 993 except generic 143)
  --target-port PORT         IMAP port (default: 143, or 993 in production)
  --source-tls ssl|starttls|none   TLS mode for the source (preset-dependent)
  --target-tls ssl|starttls|none   TLS mode for the target (default starttls)
  --include 'FOLDER|REGEX'   folders to migrate (imapsync --include filter)
  --exclude 'FOLDER|REGEX'   folders to skip (default: Trash and Spam)
  --dry-run                  do everything except copy (imapsync --dry)
  --delete                   DELETE mail on the target that is gone on the
                             source (destructive; off by default)
  --backend imapsync|docker  force the backend instead of auto-detecting
  --help                     show this help

Examples:
  # Google Workspace -> Oasis (sam@example.com on both sides)
  IMAP_SOURCE_PASSWORD='app-password' IMAP_TARGET_PASSWORD='target-pass' \\
  migrate-mailboxes.sh --source google --source-user sam@example.com \\
    --target-host mail.example.com --target-user sam@example.com

  # Old Zimbra server -> Oasis, excluding Trash/Spam, without copying
  migrate-mailboxes.sh --source zimbra \\
    --source-host old-zimbra.example.com --source-user sam@example.com \\
    --target-host mail.example.com --target-user sam@example.com \\
    --source-passfile /root/old.pass --target-passfile /root/new.pass --dry-run

Full provider guidance: docs/MIGRATION.md
EOF
}

SOURCE_PROVIDER=""
SOURCE_HOST=""
SOURCE_USER=""
SOURCE_PORT=""
SOURCE_TLS="starttls"
TARGET_HOST=""
TARGET_USER=""
TARGET_PORT="143"
TARGET_TLS="starttls"
SOURCE_PASSFILE=""
TARGET_PASSFILE=""
INCLUDE=""
EXCLUDE="INBOX.Trash|INBOX.Spam|Trash|Spam|Junk|Deleted Items|Deleted|Sent items"
DRY_RUN=false
DELETE=false
BACKEND=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source) SOURCE_PROVIDER="$2"; shift 2 ;;
    --source-host) SOURCE_HOST="$2"; shift 2 ;;
    --source-user) SOURCE_USER="$2"; shift 2 ;;
    --source-port) SOURCE_PORT="$2"; shift 2 ;;
    --source-tls) SOURCE_TLS="$2"; shift 2 ;;
    --source-passfile) SOURCE_PASSFILE="$2"; shift 2 ;;
    --target-host) TARGET_HOST="$2"; shift 2 ;;
    --target-user) TARGET_USER="$2"; shift 2 ;;
    --target-port) TARGET_PORT="$2"; shift 2 ;;
    --target-tls) TARGET_TLS="$2"; shift 2 ;;
    --target-passfile) TARGET_PASSFILE="$2"; shift 2 ;;
    --include) INCLUDE="$2"; shift 2 ;;
    --exclude) EXCLUDE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --delete) DELETE=true; shift ;;
    --backend) BACKEND="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

fail() { echo "ERROR: $*" >&2; exit 1; }

[ -n "$SOURCE_PROVIDER" ] || fail "--source PROVIDER is required (exchange|google|zimbra|generic)"

# Provider presets (applied before validation so defaults like the Google
# host and per-provider ports are available to the checks below).
case "$SOURCE_PROVIDER" in
  exchange)
    SOURCE_PORT="${SOURCE_PORT:-993}"
    [ "$SOURCE_TLS" = "starttls" ] && SOURCE_TLS=ssl
    ;;
  google)
    SOURCE_HOST="${SOURCE_HOST:-imap.gmail.com}"
    SOURCE_PORT="${SOURCE_PORT:-993}"
    [ "$SOURCE_TLS" = "starttls" ] && SOURCE_TLS=ssl
    ;;
  zimbra)
    SOURCE_PORT="${SOURCE_PORT:-993}"
    [ "$SOURCE_TLS" = "starttls" ] && SOURCE_TLS=ssl
    ;;
  generic) ;;
  *) fail "--source must be one of: exchange google zimbra generic" ;;
esac

[ -n "$SOURCE_HOST" ] || fail "--source-host is required"
[ -n "$SOURCE_USER" ] || fail "--source-user is required"
[ -n "$TARGET_HOST" ] || fail "--target-host is required"
[ -n "$TARGET_USER" ] || fail "--target-user is required"
[ -n "$SOURCE_PORT" ] || fail "--source-port is required for the generic provider"
case "$SOURCE_TLS" in ssl|starttls|none) ;; *) fail "--source-tls must be ssl, starttls, or none" ;; esac
case "$TARGET_TLS" in ssl|starttls|none) ;; *) fail "--target-tls must be ssl, starttls, or none" ;; esac

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# Resolve passwords into 0600 passfiles so they never appear on the command
# line or in process listings.
if [ -z "$SOURCE_PASSFILE" ] || [ ! -f "$SOURCE_PASSFILE" ]; then
  if [ -z "${IMAP_SOURCE_PASSWORD:-}" ]; then
    fail "source password needed: --source-passfile FILE or IMAP_SOURCE_PASSWORD"
  fi
fi
if [ -z "$TARGET_PASSFILE" ] || [ ! -f "$TARGET_PASSFILE" ]; then
  if [ -z "${IMAP_TARGET_PASSWORD:-}" ]; then
    fail "target password needed: --target-passfile FILE or IMAP_TARGET_PASSWORD"
  fi
fi

umask 077
SOURCE_PW_FILE="$WORKDIR/source.pass"
TARGET_PW_FILE="$WORKDIR/target.pass"
if [ -n "$SOURCE_PASSFILE" ]; then
  install -m 0600 "$SOURCE_PASSFILE" "$SOURCE_PW_FILE"
else
  printf '%s\n' "$IMAP_SOURCE_PASSWORD" > "$SOURCE_PW_FILE"
fi
if [ -n "$TARGET_PASSFILE" ]; then
  install -m 0600 "$TARGET_PASSFILE" "$TARGET_PW_FILE"
else
  printf '%s\n' "$IMAP_TARGET_PASSWORD" > "$TARGET_PW_FILE"
fi
chmod 0600 "$SOURCE_PW_FILE" "$TARGET_PW_FILE"

# Select the imapsync backend.
if [ -z "$BACKEND" ]; then
  if command -v imapsync >/dev/null 2>&1; then
    BACKEND=imapsync
  elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    BACKEND=docker
  else
    fail "no imapsync binary found and Docker is unavailable; install imapsync or start Docker"
  fi
fi
case "$BACKEND" in
  imapsync) command -v imapsync >/dev/null 2>&1 || fail "--backend imapsync requested but imapsync is not installed" ;;
  docker) command -v docker >/dev/null 2>&1 || fail "--backend docker requested but docker is not installed" ;;
  *) fail "--backend must be imapsync or docker" ;;
esac

# Build the imapsync argument list.
IMAPSYNC_ARGS=(
  --host1 "$SOURCE_HOST" --user1 "$SOURCE_USER" --passfile1 "$SOURCE_PW_FILE"
  --port1 "$SOURCE_PORT"
  --host2 "$TARGET_HOST" --user2 "$TARGET_USER" --passfile2 "$TARGET_PW_FILE"
  --port2 "$TARGET_PORT"
  --automap --syncinternaldates
  --nolog
)
case "$SOURCE_TLS" in
  ssl) IMAPSYNC_ARGS+=(--ssl1) ;;
  none) IMAPSYNC_ARGS+=(--notls1) ;;
esac
case "$TARGET_TLS" in
  ssl) IMAPSYNC_ARGS+=(--ssl2) ;;
  none) IMAPSYNC_ARGS+=(--notls2) ;;
esac
[ -n "$INCLUDE" ] && IMAPSYNC_ARGS+=(--include "$INCLUDE")
[ -n "$EXCLUDE" ] && IMAPSYNC_ARGS+=(--exclude "$EXCLUDE")
[ "$DELETE" = true ] && IMAPSYNC_ARGS+=(--delete2)
[ "$DRY_RUN" = true ] && IMAPSYNC_ARGS+=(--dry)

info() { printf '[migrate] %s\n' "$*"; }
info "Source: ${SOURCE_USER}@${SOURCE_HOST}:${SOURCE_PORT} (${SOURCE_TLS}) via ${SOURCE_PROVIDER}"
info "Target: ${TARGET_USER}@${TARGET_HOST}:${TARGET_PORT} (${TARGET_TLS})"
info "Backend: ${BACKEND}"
[ "$DRY_RUN" = true ] && info "DRY RUN — no mail will be copied"
[ "$DELETE" = true ] && info "WARNING: --delete is enabled; mail deleted on the source will be deleted on the target"

if [ "$BACKEND" = docker ]; then
  docker run --rm -i -v "$WORKDIR:/data:ro" "$IMAPSYNC_IMAGE" \
    "${IMAPSYNC_ARGS[@]//"$WORKDIR"/\/data}"
else
  imapsync "${IMAPSYNC_ARGS[@]}"
fi

echo
echo "Migration finished. Next steps:"
echo "  1. Verify folders and mail counts on $TARGET_HOST (webmail or IMAP client)."
echo "  2. Run again without --dry-run after a final sync to capture recent mail."
echo "  3. Migrate calendar and contacts separately (exports), then switch MX."
echo "  Full guidance: docs/MIGRATION.md"