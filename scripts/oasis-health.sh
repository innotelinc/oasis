#!/usr/bin/env bash
# Oasis domain health monitoring and compliance reporting
#
# Checks the DNS, mail, and TLS posture of an Oasis deployment and produces a
# PASS/WARN/FAIL compliance report. Safe to run from any user, any time.
# Exit status: 0 = healthy, 1 = warnings, 2 = failures.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${OASIS_ENV_FILE:-$ROOT/.env}"

DOMAIN=""
HOST_LABELS="app api auth mail files admin"
RUN_DNS=true
REPORT=false
JSON=false
QUIET=false
RESOLVER=""
HOST_LIMIT=5

usage() {
  cat <<'EOF'
Usage: oasis-health.sh [options]

  --domain DOMAIN        base domain to check (default: OASIS_BASE_DOMAIN or
                         derived from AUTH_DOMAIN in .env)
  --hosts "a b c"        hostname labels to probe (default: app api auth mail
                         files admin)
  --dns-resolver IP      use this resolver for dig lookups
  --report               write a dated Markdown report under reports/
  --json                 print a machine-readable summary
  --quiet                only print non-PASS results
  --no-dns               skip DNS/MX/SPF/DKIM/DMARC checks
  --help                 show this help

Examples:
  scripts/oasis-health.sh --report
  scripts/oasis-health.sh --domain example.com --hosts "mail app" --json
  scripts/oasis-health.sh --quiet
EOF
}

env_val() {
  local key="$1" line value
  [ -f "$ENV_FILE" ] || return 1
  line="$(grep -E "^${key}=" "$ENV_FILE" | head -n 1 || true)"
  [ -n "$line" ] || return 1
  value="${line#*=}"
  [ -n "$value" ] || return 1
  printf '%s\n' "$value"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --hosts) HOST_LABELS="$2"; shift 2 ;;
    --dns-resolver) RESOLVER="$2"; shift 2 ;;
    --report) REPORT=true; shift ;;
    --json) JSON=true; shift ;;
    --quiet) QUIET=true; shift ;;
    --no-dns) RUN_DNS=false; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -z "$DOMAIN" ]; then
  DOMAIN="$(env_val OASIS_BASE_DOMAIN || true)"
fi
if [ -z "$DOMAIN" ]; then
  AUTH="$(env_val AUTH_DOMAIN || true)"
  [ -n "$AUTH" ] && DOMAIN="${AUTH#*.}"
fi
if [ -z "$DOMAIN" ]; then
  echo "ERROR: no domain; use --domain DOMAIN or set OASIS_BASE_DOMAIN/AUTH_DOMAIN in .env" >&2
  exit 2
fi

read -r -a LABELS <<< "$HOST_LABELS"
[ "${#LABELS[@]}" -gt 0 ] || { echo "ERROR: --hosts is empty" >&2; exit 2; }

PASS=0
WARNINGS=0
FAILURES=0
REPORT_ROWS=()

record() {
  local status="$1" label="$2" detail="$3"
  REPORT_ROWS+=("| ${label} | ${status} | ${detail} |")
  case "$status" in
    PASS)
      PASS=$((PASS + 1))
      [ "$QUIET" = false ] && printf 'PASS %-34s %s\n' "$label" "$detail"
      ;;
    WARN)
      WARNINGS=$((WARNINGS + 1))
      printf 'WARN %-34s %s\n' "$label" "$detail"
      ;;
    FAIL)
      FAILURES=$((FAILURES + 1))
      printf 'FAIL %-34s %s\n' "$label" "$detail"
      ;;
  esac
}

dig_query() {
  local type="$1" name="$2"
  if [ -n "$RESOLVER" ]; then
    dig +short "@${RESOLVER}" "$type" "$name" 2>/dev/null || true
  else
    dig +short "$type" "$name" 2>/dev/null || true
  fi
}

port_open() {
  local host="$1" port="$2"
  timeout 5 bash -c "</dev/tcp/${host}/${port}" >/dev/null 2>&1
}

tls_cert() {
  local host="$1" output expiry_text expiry_secs now remaining days
  output="$(timeout 12 openssl s_client -connect "${host}:443" -servername "${host}" </dev/null 2>/dev/null || true)"
  if ! printf '%s' "$output" | grep -q 'BEGIN CERTIFICATE'; then
    record FAIL "tls ${host}" "no TLS certificate on ${host}:443"
    return
  fi
  expiry_text="$(printf '%s' "$output" | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2- || true)"
  if printf '%s' "$output" | grep -q 'Verify return code: 0'; then
    chain="valid chain"
  else
    chain="unverified chain"
  fi
  if [ -n "$expiry_text" ]; then
    expiry_secs="$(date -d "$expiry_text" +%s 2>/dev/null || true)"
    now="$(date +%s)"
    if [ -n "$expiry_secs" ]; then
      remaining=$((expiry_secs - now))
      days=$((remaining / 86400))
      if [ "$remaining" -lt 0 ]; then
        record FAIL "tls ${host}" "certificate expired ${expiry_text} (${chain})"
      elif [ "$remaining" -lt 604800 ]; then
        record FAIL "tls ${host}" "certificate expires in ${days} days (${expiry_text}, ${chain})"
      elif [ "$remaining" -lt 2592000 ]; then
        record WARN "tls ${host}" "certificate expires in ${days} days (${expiry_text}, ${chain})"
      else
        record PASS "tls ${host}" "certificate valid until ${expiry_text} (${days} days, ${chain})"
      fi
    else
      record WARN "tls ${host}" "could not parse expiry date (${chain})"
    fi
  else
    record WARN "tls ${host}" "certificate present but expiry date unreadable (${chain})"
  fi
}

http_status() {
  local host="$1" code
  command -v curl >/dev/null 2>&1 || { record WARN "http ${host}" "curl not installed; HTTPS port probed instead"; return; }
  code="$(curl -ksS -o /dev/null -w '%{http_code}' --max-time "${HOST_LIMIT}" "https://${host}/" 2>/dev/null || true)"
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    record PASS "http ${host}" "HTTPS reachable, HTTP ${code}"
  else
    record FAIL "http ${host}" "no HTTPS response from ${host}"
  fi
}

echo "Oasis health check for ${DOMAIN}"
[ "$RUN_DNS" = true ] && echo "Mail and DNS checks enabled (--no-dns to skip)"

# ── Per-hostname checks ────────────────────────────────────
for label in "${LABELS[@]}"; do
  host="${label}.${DOMAIN}"
  if [ -n "$(dig_query A "$host")" ]; then
    record PASS "dns ${host}" "A record resolves"
  else
    record FAIL "dns ${host}" "A record missing"
  fi
  tls_cert "$host"
  http_status "$host"
done

# ── Base-domain DNS, mail, and compliance checks ───────────
if [ "$RUN_DNS" = true ]; then
  if [ -n "$(dig_query MX "$DOMAIN")" ]; then
    record PASS "mx ${DOMAIN}" "MX records present"
  else
    record FAIL "mx ${DOMAIN}" "no MX records; mail will not be delivered"
  fi

  spf="$(dig_query TXT "$DOMAIN" | grep -E 'v=spf1' | head -n 1 || true)"
  if [ -n "$spf" ]; then
    case "$spf" in
      *"-all") record PASS "spf ${DOMAIN}" "SPF published with hard fail (-all)" ;;
      *"~all") record WARN "spf ${DOMAIN}" "SPF published with soft fail (~all) instead of -all" ;;
      *) record PASS "spf ${DOMAIN}" "SPF published" ;;
    esac
  else
    record FAIL "spf ${DOMAIN}" "no SPF record; outbound mail may be flagged as spam"
  fi

  dkim_found=""
  for selector in zimbra default mail oasisdkim k1; do
    if [ -n "$(dig_query TXT "${selector}._domainkey.${DOMAIN}")" ]; then
      dkim_found="$selector"
      break
    fi
  done
  if [ -n "$dkim_found" ]; then
    record PASS "dkim ${DOMAIN}" "selector '${dkim_found}' published"
  else
    record FAIL "dkim ${DOMAIN}" "no DKIM selector found among: zimbra default mail oasisdkim k1"
  fi

  dmarc="$(dig_query TXT "_dmarc.${DOMAIN}" | head -n 1 || true)"
  if [ -n "$dmarc" ]; then
    case "$dmarc" in
      *"p=reject"*) record PASS "dmarc ${DOMAIN}" "policy=reject (${dmarc:0:60}...)" ;;
      *"p=quarantine"*) record PASS "dmarc ${DOMAIN}" "policy=quarantine (${dmarc:0:60}...)" ;;
      *"p=none"*) record WARN "dmarc ${DOMAIN}" "policy=none; tighten to quarantine or reject" ;;
      *) record PASS "dmarc ${DOMAIN}" "DMARC record present" ;;
    esac
  else
    record FAIL "dmarc ${DOMAIN}" "no DMARC record; recipients cannot evaluate spoofed mail"
  fi

  for port in 25 465 587; do
    if port_open "$DOMAIN" "$port"; then
      record PASS "port ${port} ${DOMAIN}" "reachable"
    else
      record WARN "port ${port} ${DOMAIN}" "not reachable (may be blocked or firewalled)"
    fi
  done
  for port in 993 995; do
    if port_open "$DOMAIN" "$port"; then
      record PASS "port ${port} ${DOMAIN}" "reachable"
    else
      record WARN "port ${port} ${DOMAIN}" "not reachable"
    fi
  done
fi

# ── Write the report ───────────────────────────────────────
if [ "$REPORT" = true ]; then
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  outdir="$ROOT/reports"
  mkdir -p "$outdir"
  outfile="$outdir/oasis-compliance-${stamp}.md"
  {
    printf '# Oasis compliance report\n\n'
    printf '**Domain:** %s  \n' "$DOMAIN"
    printf '**Generated:** %s (UTC)  \n' "$stamp"
    printf '**Result:** %d pass, %d warn, %d fail\n\n' "$PASS" "$WARNINGS" "$FAILURES"
    printf '| check | status | detail |\n|---|---|---|\n'
    printf '%s\n' "${REPORT_ROWS[@]}"
    printf '\n## Module gateway mapping\n\n'
    printf '| hostname | service |\n|---|---|\n'
    printf '| app.%s | Oasis web application |\n' "$DOMAIN"
    printf '| api.%s | Oasis API |\n' "$DOMAIN"
    printf '| auth.%s | Authentik identity provider |\n' "$DOMAIN"
    printf '| mail.%s | Oasis webmail |\n' "$DOMAIN"
    printf '| files.%s | Oasis file service |\n' "$DOMAIN"
    printf '| admin.%s | Oasis administration |\n' "$DOMAIN"
    printf '\n_Generated by scripts/oasis-health.sh — rerun after DNS or TLS changes._\n'
  } > "$outfile"
  echo
  echo "Compliance report written to $outfile"
fi

if [ "$JSON" = true ]; then
  printf '{"domain":"%s","pass":%d,"warn":%d,"fail":%d}\n' "$DOMAIN" "$PASS" "$WARNINGS" "$FAILURES"
fi

echo "Summary: ${PASS} pass, ${WARNINGS} warn, ${FAILURES} fail"
if [ "$FAILURES" -gt 0 ]; then
  exit 2
elif [ "$WARNINGS" -gt 0 ]; then
  exit 1
fi
exit 0