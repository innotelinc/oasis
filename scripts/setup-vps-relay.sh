#!/usr/bin/env bash
set -euo pipefail

# ┌──────────────────────────────────────────────────────────┐
# │  EmailRelay — VPS Outbound Relay Setup                   │
# │                                                          │
# │  Run THIS script ON your VPS (root). It installs         │
# │  emailrelay, listening on port 587 with TLS + auth, and  │
# │  delivers relayed mail to the internet via DNS MX (port  │
# │  25), which your home ISP blocks.                        │
# │                                                          │
# │  Usage:                                                  │
# │    ./setup-vps-relay.sh                        # auto     │
# │    ./setup-vps-relay.sh --port 2525 --dry-run # preview  │
# └──────────────────────────────────────────────────────────┘

# ── Config (env or args) ───────────────────────────────────
VPS_RELAY_PORT="${VPS_RELAY_PORT:-587}"
VPS_RELAY_USER="${VPS_RELAY_USER:-mailrelay}"
VPS_RELAY_PASSWORD="${VPS_RELAY_PASSWORD:-}"
DRY_RUN="${DRY_RUN:-false}"

# track whether credentials were explicitly supplied (env or args)
PASS_EXPLICIT=false
[ -n "${VPS_RELAY_PASSWORD}" ] && PASS_EXPLICIT=true
USER_EXPLICIT=false

while [ $# -gt 0 ]; do
    case "$1" in
        --port)    VPS_RELAY_PORT="$2"; shift 2 ;;
        --user)    VPS_RELAY_USER="$2"; USER_EXPLICIT=true; shift 2 ;;
        --password) VPS_RELAY_PASSWORD="$2"; PASS_EXPLICIT=true; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        --help|-h)
            echo "Usage: $0 [--port PORT] [--user USER] [--password PASS] [--dry-run]"
            echo "Installs emailrelay on this VPS as an authenticated outbound relay."
            echo "Port default: 587. A random password is generated if not given."
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

dry() { if [ "${DRY_RUN}" = "true" ]; then echo "[DRY-RUN] $*"; else "$@"; fi; }

# Generate a random password if none was provided
if [ -z "${VPS_RELAY_PASSWORD}" ]; then
    VPS_RELAY_PASSWORD=$(openssl rand -base64 18 2>/dev/null | tr -d '=+/')
    echo "[i] Generated random relay password: ${VPS_RELAY_PASSWORD}"
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "[x] This script must be run as root (on the VPS)" >&2
    exit 1
fi

# ── 1. Build dependencies + source ─────────────────────────
if [ ! -d /usr/src/emailrelay ]; then
    echo "[+] Installing build dependencies..."
    if command -v apt-get >/dev/null 2>&1; then
        dry apt-get update
        dry apt-get -y install build-essential autoconf automake libtool \
            libssl-dev make git openssl
    elif command -v dnf >/dev/null 2>&1; then
        dry dnf -y install gcc gcc-c++ make autoconf automake libtool \
            openssl-devel git openssl
    fi
    echo "[+] Cloning emailrelay..."
    dry git clone https://github.com/innotelinc/emailrelay.git /usr/src/emailrelay
fi

# ── 2. Build & install emailrelay ──────────────────────────
cd /usr/src/emailrelay
if [ ! -x /usr/sbin/emailrelay ] && [ ! -x /usr/bin/emailrelay ] && [ "${DRY_RUN}" != "true" ]; then
    echo "[+] Building emailrelay (a few minutes)..."
    ./configure --prefix=/usr --with-openssl=/usr/bin/openssl
    make
    make install
fi

# ── 3. TLS certificate (self-signed, used only for relay auth) ──
CERT=/etc/ssl/certs/emailrelay.pem
if [ ! -f "${CERT}" ]; then
    echo "[+] Generating TLS certificate..."
    dry openssl req -newkey rsa:2048 -nodes -keyout /tmp/emailrelay.key \
        -x509 -days 365 -out /tmp/emailrelay.crt \
        -subj "/CN=$(hostname -f 2>/dev/null || hostname)" 2>/dev/null
    dry cat /tmp/emailrelay.key /tmp/emailrelay.crt > "${CERT}"
    dry chmod 600 "${CERT}"
    rm -f /tmp/emailrelay.key /tmp/emailrelay.crt
fi

# ── 4. Auth file (what Zimbra will authenticate with) ──────
AUTH=/etc/emailrelay.auth
if [ ! -f "${AUTH}" ]; then
    if [ -z "${VPS_RELAY_PASSWORD}" ]; then
        VPS_RELAY_PASSWORD=$(openssl rand -base64 18 2>/dev/null | tr -d '=+/')
        echo "[i] Generated random relay password: ${VPS_RELAY_PASSWORD}"
    fi
    echo "[+] Writing auth file..."
    echo "server plain ${VPS_RELAY_USER} ${VPS_RELAY_PASSWORD}" > /tmp/emailrelay.auth
    dry install -m 600 /tmp/emailrelay.auth "${AUTH}"
    rm -f /tmp/emailrelay.auth
else
    # Reuse existing credentials so re-runs stay consistent, unless the user
    # explicitly passed new ones (env or --user/--password) — in that case the
    # auth file is refreshed so the new credentials take effect.
    # auth line format: server plain <user> <password>
    if ! ${PASS_EXPLICIT}; then
        VPS_RELAY_PASSWORD=$(awk '{print $NF}' "${AUTH}" 2>/dev/null || true)
    fi
    if ! ${USER_EXPLICIT}; then
        VPS_RELAY_USER=$(awk '{print $3}' "${AUTH}" 2>/dev/null || echo "${VPS_RELAY_USER}")
    fi
    if ${PASS_EXPLICIT} || ${USER_EXPLICIT}; then
        echo "server plain ${VPS_RELAY_USER} ${VPS_RELAY_PASSWORD}" > /tmp/emailrelay.auth
        dry install -m 600 /tmp/emailrelay.auth "${AUTH}"
        rm -f /tmp/emailrelay.auth
    fi
fi

# ── 5. Config: listen :PORT with STARTTLS+auth, deliver via MX ──
#    (no 'as-proxy', no smarthost: emailrelay delivers to remote MX on port
#     25 via DNS — the one thing your home ISP blocks.)
#    server-tls + server-tls-required = STARTTLS mandatory before AUTH,
#    which is what Zimbra's Postfix uses on port 587 (not implicit TLS).
echo "[+] Writing emailrelay config..."
dry mkdir -p /usr/var/spool/emailrelay
dry tee /usr/etc/emailrelay.conf <<EOF
port ${VPS_RELAY_PORT}
remote-clients
server-tls
server-tls-required
server-auth /etc/emailrelay.auth
server-tls-certificate /etc/ssl/certs/emailrelay.pem
spool-dir /usr/var/spool/emailrelay
poll 30
EOF

# ── 6. systemd service ─────────────────────────────────────
SVC=/usr/src/emailrelay/etc/emailrelay.service
if [ -f "${SVC}" ]; then
    dry cp "${SVC}" /etc/systemd/system/emailrelay.service
    dry systemctl daemon-reload
    dry systemctl enable emailrelay
    # restart (not start) so config changes apply on re-runs
    if ! dry systemctl restart emailrelay; then
        echo "[!] Could not start emailrelay via systemd — check: systemctl status emailrelay"
    fi
else
    echo "[!] emailrelay.service not found in repo — start manually:"
    echo "    emailrelay --as-server --config-file /usr/etc/emailrelay.conf"
fi

# ── 7. Firewall ────────────────────────────────────────────
echo "[+] Opening port ${VPS_RELAY_PORT}/tcp..."
dry ufw allow "${VPS_RELAY_PORT}/tcp" 2>/dev/null || true
dry firewall-cmd --permanent --add-port="${VPS_RELAY_PORT}/tcp" 2>/dev/null && dry firewall-cmd --reload 2>/dev/null || true

# ── 8. Port-25 check (many VPS providers block outbound 25 by default) ──
echo "[+] Checking outbound port 25 from this VPS..."
if timeout 10 bash -c 'exec 3<>/dev/tcp/gmail-smtp-in.l.google.com/25' 2>/dev/null; then
    echo "[+] Port 25 reachable — emailrelay can deliver to the internet."
else
    echo "[!] WARNING: outbound port 25 looks BLOCKED from this VPS!"
    echo "    DigitalOcean, AWS EC2 and GCP block port 25 by default — open a"
    echo "    support ticket to unblock it, or mail will spool without delivering."
fi

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  VPS relay ready on port ${VPS_RELAY_PORT}"
echo "═══════════════════════════════════════════════════════"
echo "  Put these values in scripts/install-config.env on the"
echo "  mail server (or your .env):"
echo ""
echo "  RELAY_ENABLED=true"
echo "  RELAY_HOST=\"[<THIS-VPS-IP-OR-HOSTNAME>]:${VPS_RELAY_PORT}\""
echo "  RELAY_USER=\"${VPS_RELAY_USER}\""
echo "  RELAY_PASSWORD=\"${VPS_RELAY_PASSWORD}\""
echo "  RELAY_TLS_LEVEL=\"encrypt\""
echo ""
echo "  Then install Zimbra with ./build.sh deploy, and test"
echo "  outbound mail from a mailbox."
echo "═══════════════════════════════════════════════════════"
