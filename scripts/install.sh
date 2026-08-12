#!/usr/bin/env bash
set -euo pipefail

# ┌──────────────────────────────────────────────────────────┐
# │  Zimbra FOSS — Universal Install Script                  │
# │  Installs and configures Zimbra on Ubuntu / RHEL         │
# │                                                          │
# │  Usage:                                                  │
# │    ./install.sh /path/to/zcs-*.tgz                       │
# │    ./install.sh --url https://example.com/zcs-*.tgz      │
# │    ./install.sh --config install-config.env ...          │
# └──────────────────────────────────────────────────────────┘

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colors ─────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*" >&2; }
info() { echo -e "${BLUE}[i]${NC} $*"; }
header() { echo -e "\n${BOLD}${BLUE}═══ $* ═══${NC}\n"; }
dry()  { if [ "${DRY_RUN:-false}" = "true" ]; then info "[DRY-RUN] $*"; else "$@"; fi; }

# ── Defaults ───────────────────────────────────────────────
HOSTNAME="${HOSTNAME:-mail.example.com}"
DOMAIN="${DOMAIN:-example.com}"
PUBLIC_IP="${PUBLIC_IP:-}"
TIMEZONE="${TIMEZONE:-America/New_York}"
ZIMBRA_ADMIN_PASSWORD="${ZIMBRA_ADMIN_PASSWORD:-}"
LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-admin@example.com}"
# Use ${SSL_DOMAINS:+x} rather than "${SSL_DOMAINS[@]:-}" or ${#SSL_DOMAINS[@]}
# so this is safe under `set -u` even when the array was never declared
# (older bash reports "SSL_DOMAINS: unbound variable" otherwise).
if [ -z "${SSL_DOMAINS:+x}" ]; then SSL_DOMAINS=(mail.example.com); fi
CREATE_SWAP="${CREATE_SWAP:-true}"
SWAP_SIZE_MB="${SWAP_SIZE_MB:-1024}"
DISABLE_IPV6="${DISABLE_IPV6:-true}"
WEBMIN_ENABLED="${WEBMIN_ENABLED:-true}"
DRY_RUN="${DRY_RUN:-false}"
ZIMBRA_TGZ_PATH="${ZIMBRA_TGZ_PATH:-}"
ZIMBRA_TGZ_URL="${ZIMBRA_TGZ_URL:-}"
RELAY_ENABLED="${RELAY_ENABLED:-false}"
RELAY_HOST="${RELAY_HOST:-}"
RELAY_USER="${RELAY_USER:-}"
RELAY_PASSWORD="${RELAY_PASSWORD:-}"
RELAY_TLS_LEVEL="${RELAY_TLS_LEVEL:-may}"
EMAILRELAY_ENABLED="${EMAILRELAY_ENABLED:-false}"
EMAILRELAY_AUTH_USER="${EMAILRELAY_AUTH_USER:-}"
EMAILRELAY_AUTH_PASSWORD="${EMAILRELAY_AUTH_PASSWORD:-}"
ZEXTRAS_THEME_ENABLED="${ZEXTRAS_THEME_ENABLED:-false}"
DISABLE_MODERN_UI="${DISABLE_MODERN_UI:-false}"
SMTP_ALT_PORT="${SMTP_ALT_PORT:-}"
SKIP_DEPS="${SKIP_DEPS:-false}"
SKIP_DNS="${SKIP_DNS:-false}"
SKIP_SSL="${SKIP_SSL:-false}"
SKIP_THEME="${SKIP_THEME:-false}"
SKIP_RELAY="${SKIP_RELAY:-false}"
if [ -z "${DNS_SERVERS:+x}" ]; then DNS_SERVERS=("8.8.8.8" "8.8.4.4"); fi

# ── Detect OS ──────────────────────────────────────────────
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS_ID="${ID}"
        OS_VERSION_ID="${VERSION_ID}"
        OS_CODENAME="${VERSION_CODENAME:-}"
        OS_NAME="${PRETTY_NAME:-${ID} ${VERSION_ID}}"
    elif [ -f /etc/redhat-release ]; then
        OS_ID="rhel"
        OS_VERSION_ID=$(rpm -q --qf "%{VERSION}" "$(rpm -q --whatprovides redhat-release)" 2>/dev/null || echo "8")
        OS_NAME="$(cat /etc/redhat-release)"
    else
        err "Cannot detect OS"
        exit 1
    fi
    
    # Normalize OS family
    case "${OS_ID}" in
        ubuntu|debian) PKG_MGR="apt"; OS_FAMILY="debian" ;;
        rocky|almalinux|rhel|centos|ol|oracle|fedora) PKG_MGR="dnf"; OS_FAMILY="rhel" ;;
        *) err "Unsupported OS: ${OS_ID}"; exit 1 ;;
    esac
    
    log "Detected: ${OS_NAME} (${OS_FAMILY}, ${PKG_MGR})"
}

# ── Check root ─────────────────────────────────────────────
check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        err "This script must be run as root"
        exit 1
    fi
    if [ "${DRY_RUN}" = "true" ]; then
        warn "DRY RUN MODE — no changes will be made"
    fi
}

# ── Idempotency check ─────────────────────────────────────
check_already_installed() {
    if [ -f /opt/zimbra/bin/zmcontrol ] || [ -f /opt/zimbra/.install_done ]; then
        warn "Zimbra appears to be already installed"
        ZIMBRA_ALREADY_INSTALLED=true
        if [ "${FORCE_REINSTALL:-false}" = "true" ]; then
            warn "FORCE_REINSTALL=true — re-installing"
            ZIMBRA_ALREADY_INSTALLED=false
        fi
    fi
}

mark_installed() {
    dry touch /opt/zimbra/.install_done
}

# ── Load config file ───────────────────────────────────────
load_config() {
    local cfg="${1:-}"
    if [ -n "${cfg}" ] && [ -f "${cfg}" ]; then
        log "Loading config: ${cfg}"
        set -a; source "${cfg}"; set +a
    fi
}

# ═══════════════════════════════════════════════════════════
#  SECTION 1: System Preparation
# ═══════════════════════════════════════════════════════════

install_deps() {
    header "Installing Dependencies"
    
    if [ "${SKIP_DEPS}" = "true" ]; then
        warn "Skipping dependency installation"
        return 0
    fi
    
    if [ "${PKG_MGR}" = "apt" ]; then
        dry apt update
        dry apt -y install gcc make g++ openssl libxml2-dev perl net-tools \
            gnupg2 locate git software-properties-common openjdk-8-jdk \
            ant ant-optional ruby maven build-essential rsyslog debhelper \
            dnsmasq python3 python3-dev python3-venv libaugeas0 libaugeas-dev
    elif [ "${PKG_MGR}" = "dnf" ]; then
        dry dnf -y update
        dry dnf -y install gcc gcc-c++ make openssl libxml2-devel perl \
            net-tools gnupg2 mlocate git java-1.8.0-openjdk-devel ant \
            ruby ruby-devel maven rsyslog dnsmasq python3 python3-devel \
            augeas augeas-devel
    fi
    log "Dependencies installed"
}

remove_postfix() {
    header "Removing Postfix"
    
    if systemctl is-active --quiet postfix 2>/dev/null; then
        dry systemctl stop postfix
    fi
    
    if [ "${PKG_MGR}" = "apt" ]; then
        dry apt -y purge postfix 2>/dev/null || true
    elif [ "${PKG_MGR}" = "dnf" ]; then
        dry dnf -y remove postfix 2>/dev/null || true
    fi
    log "Postfix removed"
}

install_webmin() {
    if [ "${WEBMIN_ENABLED}" != "true" ]; then return 0; fi
    
    header "Installing Webmin"
    
    if command -v webmin >/dev/null 2>&1; then
        info "Webmin already installed"
        return 0
    fi
    
    cd /usr/src
    dry wget -q https://www.webmin.com/download/deb/webmin-current.deb 2>/dev/null || {
        warn "Could not download Webmin .deb, trying rpm..."
        dry wget -q https://www.webmin.com/download/rpm/webmin-current.rpm 2>/dev/null || {
            warn "Webmin download failed, skipping"
            return 0
        }
        dry rpm -i webmin-current.rpm 2>/dev/null || true
        return 0
    }
    dry dpkg -i webmin-current.deb 2>/dev/null || true
    dry apt -fy install 2>/dev/null || true
    log "Webmin installed (https://${HOSTNAME}:10000)"
}

set_hostname() {
    header "Setting Hostname"
    
    local current
    current=$(hostname)
    if [ "${current}" != "${HOSTNAME}" ]; then
        dry hostnamectl set-hostname "${HOSTNAME}" --static
    fi
    log "Hostname: ${HOSTNAME}"
}

configure_hosts() {
    header "Configuring /etc/hosts"
    
    if ! grep -q "${HOSTNAME}" /etc/hosts 2>/dev/null; then
        local entry="127.0.0.1 localhost.localdomain localhost\n::1 localhost.localdomain localhost ip6-localhost ip6-loopback"
        if [ -n "${PUBLIC_IP}" ]; then
            entry="${entry}\n${PUBLIC_IP} ${HOSTNAME} $(echo ${HOSTNAME} | cut -d. -f1)"
        fi
        dry bash -c "echo -e '${entry}' > /etc/hosts"
    fi
    log "Hosts file configured"
}

disable_ipv6() {
    if [ "${DISABLE_IPV6}" != "true" ]; then return 0; fi
    
    header "Disabling IPv6"
    
    if ! grep -q "net.ipv6.conf.all.disable_ipv6" /etc/sysctl.conf 2>/dev/null; then
        dry tee -a /etc/sysctl.conf <<'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
        dry sysctl -p 2>/dev/null || warn "Could not apply sysctl (container?)"
    fi
    log "IPv6 disabled"
}

disable_firewall() {
    header "Disabling Firewalls"
    
    for svc in ufw iptables ip6tables firewalld; do
        if systemctl is-active --quiet "${svc}" 2>/dev/null; then
            dry systemctl stop "${svc}" 2>/dev/null || true
            dry systemctl disable "${svc}" 2>/dev/null || true
        fi
    done
    dry iptables -F 2>/dev/null || true
    log "Firewalls disabled"
}

# ═══════════════════════════════════════════════════════════
#  SECTION 2: DNS Setup
# ═══════════════════════════════════════════════════════════

configure_dns() {
    if [ "${SKIP_DNS}" = "true" ]; then return 0; fi
    
    header "Configuring DNS (dnsmasq)"
    
    # Disable systemd-resolved stub
    if systemctl is-active --quiet systemd-resolved 2>/dev/null; then
        dry systemctl stop systemd-resolved
        dry systemctl disable systemd-resolved
        dry systemctl mask systemd-resolved 2>/dev/null || true
    fi
    
    # Set upstream DNS temporarily
    dry rm -f /etc/resolv.conf
    dry tee /etc/resolv.conf <<< "nameserver ${DNS_SERVERS[0]}"
    
    # Configure dnsmasq
    dry unlink /etc/dnsmasq.conf 2>/dev/null || true
    
    local dns_servers=""
    for ns in "${DNS_SERVERS[@]}"; do
        dns_servers="${dns_servers}server=${ns}\n"
    done
    
    dry tee /etc/dnsmasq.conf <<EOF
${dns_servers}
trust-anchor=.,20326,8,2,E06D44B80B8F1D39A95C0B0D7C65D08458E880409BBC683457104237C7F8EC8D
dnssec
dnssec-check-unsigned
no-resolv
listen-address=127.0.0.1
bind-interfaces
bogus-priv
domain-needed
stop-dns-rebind
rebind-localhost-ok
domain=${DOMAIN}
mx-host=${DOMAIN},${HOSTNAME},0
address=/${HOSTNAME}/${PUBLIC_IP}
cache-size=2000
EOF
    
    dry tee /etc/resolv.conf <<< "nameserver 127.0.0.1"
    dry systemctl restart dnsmasq 2>/dev/null || dry service dnsmasq restart
    log "DNS configured"
}

# ═══════════════════════════════════════════════════════════
#  SECTION 3: Zimbra Installation
# ═══════════════════════════════════════════════════════════

install_zimbra() {
    header "Installing Zimbra"
    
    if [ "${ZIMBRA_ALREADY_INSTALLED:-false}" = "true" ]; then
        info "Zimbra already installed, skipping installation."
        return 0
    fi
    
    local tgz_path="${1:-${ZIMBRA_TGZ_PATH}}"
    local tgz_url="${ZIMBRA_TGZ_URL}"
    
    # Resolve the .tgz
    if [ -z "${tgz_path}" ] && [ -n "${tgz_url}" ]; then
        log "Downloading Zimbra from ${tgz_url}..."
        cd /usr/src
        dry wget -q "${tgz_url}" -O zimbra-install.tgz
        tgz_path="/usr/src/zimbra-install.tgz"
    fi
    
    if [ -z "${tgz_path}" ] || [ ! -f "${tgz_path}" ]; then
        err "No Zimbra installer found. Provide a .tgz file or --url"
        err "Usage: $0 /path/to/zcs-*.tgz"
        exit 1
    fi
    
    log "Using installer: ${tgz_path}"
    
    # Extract
    cd /usr/src
    local zdir
    zdir=$(tar tzf "${tgz_path}" 2>/dev/null | head -1 | cut -d/ -f1)
    
    if [ -d "/usr/src/${zdir}" ]; then
        dry rm -rf "/usr/src/${zdir}"
    fi
    
    dry tar zxf "${tgz_path}"
    cd "/usr/src/${zdir}"
    
    if [ "${DRY_RUN}" = "true" ]; then
        info "[DRY-RUN] Would run: ./install.sh"
        return 0
    fi
    
    # Non-interactive install using config file piped to stdin
    log "Running Zimbra installer (non-interactive)..."
    log "This will take 10-30 minutes..."
    
    if [ -n "${ZIMBRA_ADMIN_PASSWORD}" ]; then
        # Create auto-install config
        local cfg="/tmp/zimbra_install_config"
        cat > "${cfg}" <<EOF
AVDOMAIN="${DOMAIN}"
CREATEADMIN="admin@${DOMAIN}"
CREATEADMINPASS="${ZIMBRA_ADMIN_PASSWORD}"
CREATEDOMAIN="${DOMAIN}"
DOTIMESTAMP="yes"
HOSTNAME="${HOSTNAME}"
INSTALL_PACKAGES="zimbra-core zimbra-ldap zimbra-logger zimbra-mta zimbra-dnscache zimbra-snmp zimbra-store zimbra-apache zimbra-spell zimbra-memcached zimbra-proxy zimbra-drive zimbra-imapd zimbra-patch"
LDAPHOST="${HOSTNAME}"
LDAPPORT=389
LDAPROOTPW="${ZIMBRA_LDAP_PASSWORD:-${ZIMBRA_ADMIN_PASSWORD}}"
INSTALL_WEBAPPS="service zimlet zimbra zimbraAdmin"
REMOVE="no"
RUNAV="yes"
RUNSA="yes"
SMTPDEST="admin@${DOMAIN}"
SMTPHOST="${HOSTNAME}"
SMTPNOTIFY="yes"
SMTPSOURCE="admin@${DOMAIN}"
SNMPNOTIFY="yes"
SNMPTRAPHOST="${HOSTNAME}"
STARTSERVERS="yes"
SYSTEMMEMORY="auto"
TRAINSA="yes"
UPGRADE="yes"
USESPELL="yes"
VERSIONUPDATECHECKS="TRUE"
VIRUSQUARANTINE="admin@${DOMAIN}"
ZIMBRA_REQ_SECURITY="yes"
EOF
        # Run installer with config piped via stdin
        ./install.sh < "${cfg}"
        rm -f "${cfg}"
    else
        ./install.sh
    fi
    
    log "Zimbra installed"
}

# ═══════════════════════════════════════════════════════════
#  SECTION 4: Post-Install Configuration
# ═══════════════════════════════════════════════════════════

configure_zimbra() {
    header "Configuring Zimbra"
    
    dry su - zimbra -c "zmlocalconfig -e zimbra_zmprov_default_to_ldap=true"
    dry su - zimbra -c "zmprov ms \$(zmhostname) zimbraReverseProxyMailMode redirect"
    dry su - zimbra -c "zmprov ms \$(zmhostname) zimbraMtaLmtpHostLookup native"
    dry su - zimbra -c "zmprov mcf zimbraMtaLmtpHostLookup native"
    dry su - zimbra -c "zmmtactl restart"
    dry su - zimbra -c "zmproxyctl restart"
    
    # Disable modern UI if requested
    if [ "${DISABLE_MODERN_UI}" = "true" ]; then
        disable_modern_ui
    fi
    
    log "Zimbra configured"
}

disable_modern_ui() {
    log "Disabling Modern UI..."
    
    local msg_file="/opt/zimbra/jetty_base/webapps/zimbra/WEB-INF/classes/messages/ZmMsg.properties"
    local jsp_file="/opt/zimbra/jetty_base/webapps/zimbra/public/login.jsp"
    
    if [ -f "${msg_file}" ]; then
        dry sed -i 's/^clientAdvanced = .*/clientAdvanced = Default/' "${msg_file}"
        dry sed -i 's/^clientPreferred = .*/clientPreferred =/' "${msg_file}"
        dry sed -i 's/^clientModern = .*/clientModern =/' "${msg_file}"
    fi
    
    if [ -f "${jsp_file}" ]; then
        dry sed -i '/<option value="preferred"/,+3d' "${jsp_file}"
        dry sed -i '/<option value="modern"/,+3d' "${jsp_file}"
    fi
    
    dry su - zimbra -c 'zmmailboxdctl restart'
    log "Modern UI disabled"
}

configure_amavis() {
    header "Configuring Amavis"
    
    local amavis_conf="/opt/zimbra/conf/amavisd.conf.in"
    if [ -f "${amavis_conf}" ]; then
        if ! grep -q '10024,10026' "${amavis_conf}" 2>/dev/null; then
            dry sed -i 's/\$inet_socket_port = \[10024\];/\$inet_socket_port = [10024,10026];/' "${amavis_conf}"
        fi
        if ! grep -q "${HOSTNAME}" "${amavis_conf}" 2>/dev/null; then
            dry sed -i "s/\$myhostname = .*/\$myhostname = '${HOSTNAME}';/" "${amavis_conf}"
        fi
    fi
    log "Amavis configured"
}

configure_spamassassin() {
    header "Updating SpamAssassin"
    
    cd /usr/src
    dry wget -q https://spamassassin.apache.org/updates/GPG.KEY -O /tmp/sa-gpg.key 2>/dev/null || {
        warn "Could not download SpamAssassin GPG key"
        return 0
    }
    dry su - zimbra -c "sa-update --import /tmp/sa-gpg.key" 2>/dev/null || true
    dry su - zimbra -c "/opt/zimbra/common/bin/sa-update -D" 2>/dev/null || true
    log "SpamAssassin updated"
}

configure_smtp_port() {
    if [ -z "${SMTP_ALT_PORT}" ]; then return 0; fi
    
    header "Configuring Alt SMTP Port (${SMTP_ALT_PORT})"
    
    local master_cf="/opt/zimbra/common/conf/master.cf.in"
    if [ -f "${master_cf}" ] && ! grep -q "^${SMTP_ALT_PORT}" "${master_cf}" 2>/dev/null; then
        dry sed -i "/^smtp.*inet.*postscreen/a ${SMTP_ALT_PORT}      inet  n       -       y       -       -       smtpd" "${master_cf}"
    fi
    
    local my_networks="127.0.0.0/8 [::1]/128 ${MY_NETWORKS}"
    dry su - zimbra -c "zmprov ms ${HOSTNAME} zimbraMtaMyNetworks '${my_networks}'"
    dry su - zimbra -c "zmprov mcf zimbraSmtpPort ${SMTP_ALT_PORT}"
    dry su - zimbra -c "zmprov ms \$(zmhostname) zimbraSmtpPort ${SMTP_ALT_PORT}"
    dry su - zimbra -c 'postfix reload'
    dry su - zimbra -c 'zmcontrol restart'
    log "SMTP port set to ${SMTP_ALT_PORT}"
}

# ═══════════════════════════════════════════════════════════
#  SECTION 5: SSL / Let's Encrypt
# ═══════════════════════════════════════════════════════════

setup_letsencrypt() {
    if [ "${SKIP_SSL}" = "true" ]; then return 0; fi
    
    header "Setting up Let's Encrypt SSL"
    
    # Install certbot via python venv (most portable)
    cd /usr/src
    if ! command -v certbot >/dev/null 2>&1; then
        log "Installing certbot..."
        dry apt update 2>/dev/null || true
        dry apt -y install python3 python3-dev python3-venv libaugeas0 libaugeas-dev 2>/dev/null || true
        
        dry python3 -m venv /opt/certbot/
        dry /opt/certbot/bin/pip install --upgrade pip 2>/dev/null || true
        dry /opt/certbot/bin/pip install certbot 2>/dev/null || true
        dry ln -sf /opt/certbot/bin/certbot /usr/bin/certbot
    fi
    
    # Stop Zimbra services that use port 80/443
    dry su - zimbra -c 'zmproxyctl stop' 2>/dev/null || true
    dry su - zimbra -c 'zmmailboxdctl stop' 2>/dev/null || true
    
    # Build domain list
    local domain_args=""
    for d in "${SSL_DOMAINS[@]}"; do
        domain_args="${domain_args} -d ${d}"
    done
    
    # Request certificate
    log "Requesting certificate for: ${SSL_DOMAINS[*]}"
    dry certbot certonly --agree-tos -m "${LETSENCRYPT_EMAIL}" \
        --key-type rsa --preferred-chain "ISRG Root X1" \
        --standalone ${domain_args} -n 2>/dev/null || {
        warn "certbot failed — check DNS points to this server"
        return 1
    }
    
    # Add ISRG Root
    local cert_dir="/etc/letsencrypt/live/${SSL_DOMAINS[0]}"
    dry wget -q --no-check-certificate -O /tmp/ISRG-X1.pem \
        https://letsencrypt.org/certs/isrgrootx1.pem.txt 2>/dev/null
    dry cat /tmp/ISRG-X1.pem >> "${cert_dir}/fullchain.pem" 2>/dev/null || true
    
    # Copy to Zimbra
    dry mkdir -p /opt/zimbra/ssl/letsencrypt
    dry cp "${cert_dir}/"* /opt/zimbra/ssl/letsencrypt/
    dry chown zimbra:zimbra /opt/zimbra/ssl/letsencrypt/*
    
    log "SSL certificate obtained"
}

deploy_ssl() {
    if [ "${SKIP_SSL}" = "true" ]; then return 0; fi
    
    header "Deploying SSL Certificate"
    
    cd /opt/zimbra/ssl/letsencrypt
    
    # Verify
    dry su - zimbra -c "cd /opt/zimbra/ssl/letsencrypt && /opt/zimbra/bin/zmcertmgr verifycrt comm privkey.pem cert.pem fullchain.pem"
    
    # Backup existing
    dry cp -a /opt/zimbra/ssl/zimbra "/opt/zimbra/ssl/zimbra.$(date +%Y%m%d)" 2>/dev/null || true
    
    # Create commercial key
    dry cp /opt/zimbra/ssl/letsencrypt/privkey.pem /opt/zimbra/ssl/zimbra/commercial/commercial.key 2>/dev/null || true
    
    # Deploy
    dry su - zimbra -c "cd /opt/zimbra/ssl/letsencrypt && /opt/zimbra/bin/zmcertmgr deploycrt comm cert.pem fullchain.pem"
    
    # Restart services
    dry su - zimbra -c 'zmproxyctl start'
    dry su - zimbra -c 'zmmailboxdctl start'
    
    # Renewal cron
    if ! grep -q "certbot renew" /etc/crontab 2>/dev/null; then
        echo "0 0,12 * * * root python3 -c 'import random; import time; time.sleep(random.random() * 3600)' && certbot renew --force-renewal --preferred-chain \"ISRG Root X1\"" | dry tee -a /etc/crontab > /dev/null
    fi
    
    log "SSL deployed"
}

# ═══════════════════════════════════════════════════════════
#  SECTION 6: Outbound Relay
# ═══════════════════════════════════════════════════════════

configure_relay() {
    if [ "${RELAY_ENABLED}" != "true" ] || [ "${SKIP_RELAY}" = "true" ]; then return 0; fi
    
    header "Configuring Outbound Relay"
    
    # If the ISP blocks outbound port 25, ALL outbound mail must go through an
    # upstream smarthost on an open port (587 submission, 465 SMTPS, or a custom
    # port to your own VPS relay). Zimbra's Postfix relays everything there and
    # the smarthost delivers to the world on port 25.
    #   RELAY_HOST="[smtp.gmail.com]:587"   # brackets = no MX lookup, exact host:port
    #   RELAY_HOST="[vps.example.com]:587"  # your own VPS relay (see README)
    if [ -z "${RELAY_HOST}" ]; then
        warn "RELAY_HOST not set, skipping relay config"
        return 0
    fi
    if [[ "${RELAY_HOST}" != *:* ]]; then
        warn "RELAY_HOST '${RELAY_HOST}' has no port — Postfix would use port 25, which is what your ISP blocks. Use the [host]:587 style."
    fi
    if [ -z "${RELAY_USER}" ] || [ -z "${RELAY_PASSWORD}" ]; then
        warn "RELAY_USER or RELAY_PASSWORD not set, skipping relay config"
        return 0
    fi
    
    # Credentials map (Postfix smtp_sasl_password_maps). Postfix strips the
    # [brackets] when looking up the key, so store it WITHOUT them.
    local relay_key="${RELAY_HOST//[\[\]]/}"
    echo "${relay_key} ${RELAY_USER}:${RELAY_PASSWORD}" | \
        dry tee /opt/zimbra/conf/relay_password > /dev/null
    dry chown zimbra:zimbra /opt/zimbra/conf/relay_password
    dry chmod 600 /opt/zimbra/conf/relay_password
    dry su - zimbra -c "postmap /opt/zimbra/conf/relay_password"
    
    dry su - zimbra -c "zmprov ms \$(zmhostname) zimbraMtaRelayHost '${RELAY_HOST}'"
    dry su - zimbra -c "zmprov ms \$(zmhostname) zimbraMtaSmtpSaslAuthEnable yes"
    dry su - zimbra -c "zmprov ms \$(zmhostname) zimbraMtaSmtpSaslSecurityOptions noanonymous"
    dry su - zimbra -c "zmprov ms \$(zmhostname) zimbraMtaSmtpTlsSecurityLevel ${RELAY_TLS_LEVEL:-may}"
    dry su - zimbra -c "zmprov ms \$(zmhostname) zimbraMtaSmtpSaslPasswordMaps lmdb:/opt/zimbra/conf/relay_password"
    # Must be 'no' for CNAME'd smarthosts (e.g. smtp.gmail.com): otherwise Postfix
    # looks up credentials under the canonical CNAME and auth silently fails.
    dry su - zimbra -c "zmprov ms \$(zmhostname) zimbraMtaSmtpCnameOverridesServername no"
    dry su - zimbra -c 'zmmtactl restart'
    
    log "Outbound relay configured: ${RELAY_HOST}"
    log "NOTE: all outbound mail now goes through ${RELAY_HOST} — verify it can deliver (test send from a mailbox)."
}

# ═══════════════════════════════════════════════════════════
#  SECTION 7: Zextras Theme
# ═══════════════════════════════════════════════════════════

install_theme() {
    if [ "${ZEXTRAS_THEME_ENABLED}" != "true" ] || [ "${SKIP_THEME}" = "true" ]; then return 0; fi
    
    header "Installing Zextras Theme"
    
    cd /usr/src
    dry wget -q "${ZEXTRAS_THEME_URL}" -O zextras-theme.tgz 2>/dev/null || {
        warn "Could not download Zextras theme"
        return 0
    }
    dry tar xf zextras-theme.tgz
    cd zextras-theme-installer 2>/dev/null || cd zextras* 2>/dev/null || return 0
    
    dry dpkg -i packages/zextras-theme_*_amd64.deb 2>/dev/null || true
    dry su - zimbra -c 'zmskindeploy /opt/zimbra/jetty/webapps/zimbra/skins/zextras/' 2>/dev/null || true
    dry su - zimbra -c 'zmmailboxdctl restart'
    
    log "Zextras theme installed"
}

# ═══════════════════════════════════════════════════════════
#  SECTION 8: Email Relay (for ISP multi-server)
# ═══════════════════════════════════════════════════════════

install_emailrelay() {
    if [ "${EMAILRELAY_ENABLED}" != "true" ]; then return 0; fi
    
    header "Installing Email Relay"
    
    cd /usr/src
    if [ ! -d emailrelay ]; then
        dry git clone https://github.com/innotelinc/emailrelay.git
    fi
    
    cd emailrelay
    dry ./configure --prefix=/usr --with-openssl=/usr/bin/openssl && dry make && dry make install
    dry systemctl enable /usr/src/emailrelay/etc/emailrelay.service 2>/dev/null || true
    
    # Generate TLS cert
    if [ ! -f /etc/ssl/certs/emailrelay.pem ]; then
        dry openssl req -newkey rsa:2048 -nodes -keyout emailrelay.key \
            -x509 -days 365 -out emailrelay.crt \
            -subj "/CN=${HOSTNAME}" 2>/dev/null
        dry cat emailrelay.key emailrelay.crt > emailrelay.pem
        dry chmod 600 emailrelay.pem
        dry mv emailrelay.pem /etc/ssl/certs/
    fi
    
    # Config — STARTTLS (server-tls + server-tls-required) is the standard mode
    # for port 587. (server-tls-connection would mean implicit TLS/SMTPS, which
    # is unusual on 587.)
    dry tee /usr/etc/emailrelay.conf <<EOF
as-proxy ${HOSTNAME}:${SMTP_ALT_PORT:-25}
spool-dir /usr/var/spool/emailrelay
remote-clients
port 587
server-tls
server-tls-required
server-auth /etc/emailrelay.auth
server-tls-certificate /etc/ssl/certs/emailrelay.pem
EOF
    
    if [ -n "${EMAILRELAY_AUTH_USER}" ]; then
        echo "server plain ${EMAILRELAY_AUTH_USER} ${EMAILRELAY_AUTH_PASSWORD}" | \
            dry tee /etc/emailrelay.auth > /dev/null
        dry chmod 600 /etc/emailrelay.auth
    fi
    
    dry systemctl start emailrelay
    log "Email relay installed"
}

# ═══════════════════════════════════════════════════════════
#  SECTION 9: Swap
# ═══════════════════════════════════════════════════════════

create_swap() {
    if [ "${CREATE_SWAP}" != "true" ]; then return 0; fi
    
    header "Creating Swap (${SWAP_SIZE_MB}MB)"
    
    if [ -f /opt/zimbra/swap ]; then
        info "Swap already exists"
        return 0
    fi
    
    dry dd if=/dev/zero of=/opt/zimbra/swap bs=1M count="${SWAP_SIZE_MB}" 2>/dev/null
    dry chmod 600 /opt/zimbra/swap
    dry mkswap /opt/zimbra/swap
    dry swapon /opt/zimbra/swap
    
    log "Swap created"
}

# ═══════════════════════════════════════════════════════════
#  SECTION 10: Snap Loop Fix
# ═══════════════════════════════════════════════════════════

fix_snap_loops() {
    header "Fixing Snap Loop Reports"
    
    local loop_devices
    loop_devices=$(df -Th 2>/dev/null | grep '/dev/loop' | awk '{print ":"$1}' | tr -d '\n' | sed 's/^://')
    
    if [ -n "${loop_devices}" ]; then
        dry su - zimbra -c "zmlocalconfig -e zmstat_df_excludes='${loop_devices}'"
        dry su - zimbra -c 'zmstatctl restart'
        log "Snap loops excluded"
    fi
}

# ═══════════════════════════════════════════════════════════
#  SECTION 11: Final Cleanup & Restart
# ═══════════════════════════════════════════════════════════

finalize() {
    header "Finalizing"
    
    # Clean stale PIDs
    dry rm -f /opt/zimbra/log/*.pid 2>/dev/null || true
    
    # Mark installed
    mark_installed
    
    # Autoremove
    if [ "${PKG_MGR}" = "apt" ]; then
        dry apt -y autoremove 2>/dev/null || true
    elif [ "${PKG_MGR}" = "dnf" ]; then
        dry dnf -y autoremove 2>/dev/null || true
    fi
    
    # Restart Zimbra
    dry su - zimbra -c 'zmcontrol restart'
    
    log "Installation complete!"
    log "============================================"
    log "  Admin URL:  https://${HOSTNAME}:7071"
    log "  Webmail:    https://${HOSTNAME}"
    if [ "${WEBMIN_ENABLED}" = "true" ]; then
        log "  Webmin:     https://${HOSTNAME}:10000"
    fi
    log "============================================"
}

# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

usage() {
    echo "Usage: $0 [OPTIONS] [ZIMBRA_TGZ_PATH]"
    echo ""
    echo "Options:"
    echo "  --config FILE      Load configuration from FILE"
    echo "  --url URL          Download Zimbra installer from URL"
    echo "  --dry-run          Show what would be done, don't do it"
    echo "  --skip-deps        Skip dependency installation"
    echo "  --skip-dns         Skip DNS configuration"
    echo "  --skip-ssl         Skip SSL certificate setup"
    echo "  --skip-theme       Skip theme installation"
    echo "  --skip-relay       Skip relay configuration"
    echo "  --hostname NAME    Set server hostname"
    echo "  --domain NAME      Set email domain"
    echo "  --admin-pass PASS  Set admin password"
    echo ""
    echo "Examples:"
    echo "  $0 zcs-10.1.16_GA_*.tgz"
    echo "  $0 --config install-config.env zcs-*.tgz"
    echo "  $0 --url https://example.com/zcs-*.tgz --dry-run"
}

main() {
    local tgz_path=""
    local config_file=""
    
    # Parse arguments
    while [ $# -gt 0 ]; do
        case "$1" in
            --config)      config_file="$2"; shift 2 ;;
            --url)         ZIMBRA_TGZ_URL="$2"; shift 2 ;;
            --dry-run)     DRY_RUN=true; shift ;;
            --skip-deps)   SKIP_DEPS=true; shift ;;
            --skip-dns)    SKIP_DNS=true; shift ;;
            --skip-ssl)    SKIP_SSL=true; shift ;;
            --skip-theme)  SKIP_THEME=true; shift ;;
            --skip-relay)  SKIP_RELAY=true; shift ;;
            --hostname)    HOSTNAME="$2"; shift 2 ;;
            --domain)      DOMAIN="$2"; shift 2 ;;
            --admin-pass)  ZIMBRA_ADMIN_PASSWORD="$2"; shift 2 ;;
            --help|-h)     usage; exit 0 ;;
            -*)
                if echo "$1" | grep -q '\.tgz$'; then
                    tgz_path="$1"
                elif [ -f "$1" ]; then
                    tgz_path="$1"
                else
                    err "Unknown option: $1"; usage; exit 1
                fi
                shift
                ;;
            *)
                if [ -f "$1" ]; then
                    tgz_path="$1"
                else
                    err "Unknown argument: $1"; usage; exit 1
                fi
                shift
                ;;
        esac
    done
    
    ZIMBRA_TGZ_PATH="${tgz_path:-${ZIMBRA_TGZ_PATH}}"
    
    # Load config
    load_config "${config_file}"
    
    # Banner
    echo -e "\n${BLUE}${BOLD}  ╔══════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}${BOLD}  ║${NC}     ${BOLD}Zimbra FOSS — Universal Installer${NC}       ${BLUE}${BOLD}║${NC}"
    echo -e "${BLUE}${BOLD}  ╚══════════════════════════════════════════╝${NC}\n"
    
    # Detect OS
    detect_os
    check_root
    check_already_installed
    
    # Run all sections
    install_deps
    remove_postfix
    install_webmin
    set_hostname
    configure_hosts
    disable_ipv6
    disable_firewall
    configure_dns
    install_zimbra "${ZIMBRA_TGZ_PATH}"
    configure_zimbra
    configure_amavis
    configure_spamassassin
    configure_smtp_port
    setup_letsencrypt
    deploy_ssl
    configure_relay
    install_theme
    install_emailrelay
    create_swap
    fix_snap_loops
    finalize
}

main "$@"
