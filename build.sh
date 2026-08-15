#!/usr/bin/env bash
set -euo pipefail

# ┌──────────────────────────────────────────────────────────┐
# │  Zimbra FOSS Builder + Installer — One-command script    │
# │  Builds the Zimbra installer for any Linux OS via Docker │
# │  then installs it on the mail server.                    │
# │                                                          │
# │  Usage:                                                  │
# │    ./build.sh                          # build + install│
# │    ./build.sh build --skip-install     # build only      │
# │    ./build.sh install [zcs-*.tgz]      # install only    │
# │    ./build.sh deploy user@host         # remote install  │
# └──────────────────────────────────────────────────────────┘

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
cd "${SCRIPT_DIR}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*" >&2; }
info() { echo -e "${BLUE}[i]${NC} $*"; }
header() { echo -e "\n${BOLD}${BLUE}═══ $* ═══${NC}\n"; }
dry()  { if [ "${DRY_RUN:-false}" = "true" ]; then info "[DRY-RUN] $*"; else "$@"; fi; }

# ── Build configuration (override via .env or environment) ─
ZIMBRA_VERSION="${ZIMBRA_VERSION:-latest}"
BASE_IMAGE="${BASE_IMAGE:-ubuntu:22.04}"
IMAGE_NAME="${IMAGE_NAME:-zimbra-builder}"
BUILD_DIR="${BUILD_DIR:-./builds}"
BUILD_UID="${BUILD_UID:-1000}"
BUILD_GID="${BUILD_GID:-1000}"
DOCKER_BUILD_OPTS="${DOCKER_BUILD_OPTS:-}"
DOCKER_RUN_OPTS="${DOCKER_RUN_OPTS:-}"
# DNS servers for the build container (space-separated). Defaults to public
# resolvers as a fallback for hosts whose /etc/resolv.conf points at the
# systemd-resolved stub (127.0.0.53), which containers can't reach. Set to an
# empty string to use the Docker daemon's DNS instead (e.g. corporate/VPN), or
# to your own list of resolvers.
DOCKER_DNS="${DOCKER_DNS:-8.8.8.8 1.1.1.1}"
NO_CACHE="${NO_CACHE:-false}"

# Deploy / SSH (set in .env or environment)
DEPLOY_SSH_KEY="${DEPLOY_SSH_KEY:-}"      # path to private key, e.g. ~/.ssh/id_ed25519
DEPLOY_SSH_PORT="${DEPLOY_SSH_PORT:-}"    # SSH port if not 22
DEPLOY_SSH_OPTS="${DEPLOY_SSH_OPTS:-}"    # extra ssh options, e.g. "-o ConnectTimeout=15"
DEPLOY_SSH_ARGS=()

# ── Install configuration (override via .env, install-config.env, or CLI) ─
HOSTNAME="${HOSTNAME:-mail.example.com}"
DOMAIN="${DOMAIN:-example.com}"
PUBLIC_IP="${PUBLIC_IP:-}"
TIMEZONE="${TIMEZONE:-America/New_York}"
ZIMBRA_ADMIN_PASSWORD="${ZIMBRA_ADMIN_PASSWORD:-}"
ZIMBRA_LDAP_PASSWORD="${ZIMBRA_LDAP_PASSWORD:-${ZIMBRA_ADMIN_PASSWORD}}"
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
ZEXTRAS_THEME_URL="${ZEXTRAS_THEME_URL:-}"
DISABLE_MODERN_UI="${DISABLE_MODERN_UI:-false}"
SMTP_ALT_PORT="${SMTP_ALT_PORT:-}"
MY_NETWORKS="${MY_NETWORKS:-}"
SKIP_DEPS="${SKIP_DEPS:-false}"
DEPS_MARKER="${DEPS_MARKER:-/var/lib/zimbra-installer/.deps_done}"
SKIP_SSL="${SKIP_SSL:-false}"
SKIP_THEME="${SKIP_THEME:-false}"
SKIP_RELAY="${SKIP_RELAY:-false}"
SKIP_INSTALL="${SKIP_INSTALL:-false}"     # build-only mode (no auto-install)
FORCE_REBUILD="${FORCE_REBUILD:-false}"     # rebuild even if installer .tgz already exists
CONFIG_FILE="${CONFIG_FILE:-}"

# Load .env if present
if [ -f .env ]; then
    set -a; source .env; set +a
    info "Loaded configuration from .env"
fi

# ═══════════════════════════════════════════════════════════
#  BUILD SECTION
# ═══════════════════════════════════════════════════════════

banner() {
    echo ""
    echo -e "${BLUE}  ╔══════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}  ║${NC}       ${BOLD}Zimbra FOSS Builder${NC}                      ${BLUE}║${NC}"
    echo -e "${BLUE}  ║${NC}    Build Zimbra from source in Docker       ${BLUE}║${NC}"
    echo -e "${BLUE}  ╚══════════════════════════════════════════════╝${NC}"
    echo ""
}

# Detect OS for nice default
detect_host_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        info "Host OS: ${PRETTY_NAME:-${ID} ${VERSION_ID}}"
    fi
}

check_prereqs() {
    header "Checking Prerequisites"
    
    local missing=()
    
    if ! command -v docker >/dev/null 2>&1; then
        missing+=("docker")
    fi
    
    if ! command -v git >/dev/null 2>&1; then
        missing+=("git")
    fi
    
    if [ ${#missing[@]} -gt 0 ]; then
        err "Missing required tools: ${missing[*]}"
        echo ""
        echo "Install them first:"
        echo "  Docker:  https://docs.docker.com/engine/install/"
        echo "  Git:     apt install git / dnf install git"
        exit 1
    fi
    
    # Check Docker is running
    if ! docker info >/dev/null 2>&1; then
        err "Docker daemon is not running or you don't have permissions"
        echo "Try: sudo usermod -aG docker $USER && newgrp docker"
        exit 1
    fi
    
    check_pv
    log "All prerequisites met"
}

# Warn if pv (the determinate upload progress bar) is missing, and show how to
# install it. Non-fatal: transfers fall back to scp's built-in meter.
check_pv() {
    if command -v pv >/dev/null 2>&1; then
        return 0
    fi
    warn "pv not found — uploads will fall back to scp's built-in meter."
    warn "Install pv for the full transfer progress bar:"
    if   command -v apt-get >/dev/null 2>&1; then warn "    sudo apt-get install pv"
    elif command -v dnf     >/dev/null 2>&1; then warn "    sudo dnf install pv"
    elif command -v yum     >/dev/null 2>&1; then warn "    sudo yum install pv"
    elif command -v zypper  >/dev/null 2>&1; then warn "    sudo zypper install pv"
    elif command -v pacman  >/dev/null 2>&1; then warn "    sudo pacman -S pv"
    elif command -v apk     >/dev/null 2>&1; then warn "    sudo apk add pv"
    elif command -v brew    >/dev/null 2>&1; then warn "    brew install pv"
    else                                        warn "    install the 'pv' package with your system package manager"
    fi
}

# ── Official Zimbra repo-based version resolution ─────────
# The build clones zm-build from this repo, so its release tags are the
# authoritative list of buildable FOSS versions (no wiki scraping).
ZIMBRA_REPO="https://github.com/Zimbra/zm-build.git"

# Fetch released versions from the official Zimbra zm-build repo tags
# Returns plain X.Y.Z FOSS release tags, sorted newest-first, one per line
fetch_released_versions() {
    # `read -t` bounds each line, so a stalled network can't hang the picker
    # (portable: macOS has no `timeout` command)
    local line
    while IFS= read -r -t 20 line; do
        printf '%s\n' "${line#*refs/tags/}"
    done < <(GIT_TERMINAL_PROMPT=0 git ls-remote --tags --refs "${ZIMBRA_REPO}" 2>/dev/null) | \
        grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | \
        sort -t. -k1,1nr -k2,2nr -k3,3nr -u || true
}

# Resolve Zimbra version from wiki or explicit value
resolve_version() {
    if [ "${ZIMBRA_VERSION}" = "latest" ]; then
        header "Resolving Latest Zimbra Version"
        info "Source: ${ZIMBRA_REPO}"
        
        local version
        version=$(fetch_released_versions | head -1 || true)
        
        if [ -z "${version}" ]; then
            warn "Could not determine latest version from ${ZIMBRA_REPO}, falling back to 10.1.16"
            ZIMBRA_VERSION="10.1.16"
        else
            ZIMBRA_VERSION="${version}"
        fi
        log "Latest released Zimbra FOSS version: ${ZIMBRA_VERSION}"
    fi
}

# Interactive version picker
choose_version() {
    header "Select Zimbra Version"
    info "Source: ${ZIMBRA_REPO}"
    
    local versions
    mapfile -t versions < <(fetch_released_versions)
    
    if [ ${#versions[@]} -eq 0 ]; then
        warn "Could not fetch versions from the official Zimbra repo (${ZIMBRA_REPO}), using 10.1.16"
        ZIMBRA_VERSION="10.1.16"
        return
    fi
    
    echo "  Available FOSS releases:"
    echo ""
    for i in "${!versions[@]}"; do
        if [ $i -eq 0 ]; then
            printf "  ${GREEN}%2d)${NC} %s ${BOLD}← latest${NC}\n" $((i+1)) "${versions[$i]}"
        else
            printf "  ${BLUE}%2d)${NC} %s\n" $((i+1)) "${versions[$i]}"
        fi
    done
    echo ""
    
    local choice
    read -rp "  Choose [1-${#versions[@]}] (default: 1): " choice
    choice="${choice:-1}"
    
    if [[ "${choice}" =~ ^[0-9]+$ ]] && [ "${choice}" -ge 1 ] && [ "${choice}" -le "${#versions[@]}" ]; then
        ZIMBRA_VERSION="${versions[$((choice-1))]}"
        log "Selected: Zimbra ${ZIMBRA_VERSION}"
    else
        ZIMBRA_VERSION="${versions[0]}"
        warn "Invalid choice, using latest: ${ZIMBRA_VERSION}"
    fi
}

# Interactive base image picker
choose_base_image() {
    header "Select Target OS"
    
    local images=(
        "ubuntu:24.04   (Ubuntu 24.04 Noble — newest LTS)"
        "ubuntu:22.04   (Ubuntu 22.04 Jammy — most tested)"
        "ubuntu:20.04   (Ubuntu 20.04 Focal)"
        "rockylinux:9   (Rocky Linux 9 — RHEL 9)"
        "rockylinux:8   (Rocky Linux 8 — RHEL 8)"
        "almalinux:9    (AlmaLinux 9 — RHEL 9)"
        "oraclelinux:9  (Oracle Linux 9 — RHEL 9)"
    )
    
    echo "  Available target OS images:"
    echo ""
    for i in "${!images[@]}"; do
        if [ $i -eq 0 ]; then
            printf "  ${GREEN}%2d)${NC} %s ${BOLD}← default${NC}\n" $((i+1)) "${images[$i]}"
        else
            printf "  ${BLUE}%2d)${NC} %s\n" $((i+1)) "${images[$i]}"
        fi
    done
    echo ""
    
    local choice
    read -rp "  Choose [1-${#images[@]}] (default: 1): " choice
    choice="${choice:-1}"
    
    if [[ "${choice}" =~ ^[0-9]+$ ]] && [ "${choice}" -ge 1 ] && [ "${choice}" -le "${#images[@]}" ]; then
        BASE_IMAGE="${images[$((choice-1))]%% *}"
        log "Selected: ${BASE_IMAGE}"
    else
        BASE_IMAGE="${images[0]%% *}"
        warn "Invalid choice, using default: ${BASE_IMAGE}"
    fi
}

# Derive image tag from base image
image_tag() {
    local tag
    # Sanitize: rockylinux/rockylinux:9 → rockylinux-rockylinux-9
    tag=$(echo "${BASE_IMAGE}" | sed 's/[/:]/-/g' | sed 's/\./-/g')
    echo "${IMAGE_NAME}:${tag}-zimbra${ZIMBRA_VERSION}"
}

# Derive the zm-build OS target token (UBUNTU22_64, RHEL9_64, ...) from
# BASE_IMAGE, mirroring scripts/entrypoint.sh resolve_os_target(). Used by the
# skip-if-built check so that switching BASE_IMAGE forces a rebuild even for
# the same Zimbra version. Empty string = unknown (match version only).
build_os_token() {
    local id version major image="${BASE_IMAGE##*/}"
    id="${image%%:*}"
    version="${image##*:}"
    major="${version%%.*}"
    case "${id}" in
        ubuntu|debian)                                      echo "UBUNTU${major}_64" ;;
        rocky|rockylinux|almalinux|rhel|centos|ol|oracle|oraclelinux) echo "RHEL${major}_64" ;;
        *)                                                  echo "" ;;
    esac
}

build_image() {
    header "Building Docker Image"
    
    local tag
    tag=$(image_tag)
    
    # Ensure scripts directory exists
    mkdir -p "${SCRIPT_DIR}/scripts"
    
    # Make entrypoint executable
    chmod +x "${SCRIPT_DIR}/scripts/entrypoint.sh" 2>/dev/null || true
    
    log "Base image:   ${BASE_IMAGE}"
    log "Zimbra ver:   ${ZIMBRA_VERSION}"
    log "Image tag:    ${tag}"
    ${NO_CACHE} && log "Docker cache:  disabled (--no-cache)"
    echo ""
    
    local cache_flag=""
    ${NO_CACHE} && cache_flag="--no-cache"
    
    docker build ${DOCKER_BUILD_OPTS} ${cache_flag} \
        --build-arg BASE_IMAGE="${BASE_IMAGE}" \
        --build-arg ZIMBRA_VERSION="${ZIMBRA_VERSION}" \
        --build-arg BUILD_UID="${BUILD_UID}" \
        --build-arg BUILD_GID="${BUILD_GID}" \
        -t "${tag}" \
        -f Dockerfile \
        .
    
    log "Image built: ${tag}"
}

# Find the newest Zimbra installer .tgz under BUILD_DIR, searching
# recursively. zm-build may nest the archive inside a versioned subdirectory
# (e.g. BUILD_DIR/UBUNTU24_64-DAFFODIL-10116-...-FOSS-.../) rather than leaving
# it at the top level, so a top-level-only glob would miss it. Prints the path
# to stdout, or nothing when not found. Optional argument restricts the
# filename pattern (a `find -name` glob).
find_newest_tgz() {
    local pattern="${1:-zcs-*.tgz}"
    # `-exec ls -t {} +` sorts the matches by mtime (newest first); `head -1`
    # keeps the newest. Files are few, so a single `ls` batch is guaranteed.
    find "${BUILD_DIR}" -type f -name "${pattern}" -exec ls -t {} + 2>/dev/null | head -1
}

# Find an already-built installer .tgz for the resolved version + target OS, if
# any. Prints the path to stdout, or nothing when not found.
find_built_tgz() {
    local os_token glob
    os_token=$(build_os_token)
    if [ -n "${os_token}" ]; then
        glob="zcs-${ZIMBRA_VERSION}_*.${os_token}.*.tgz"
    else
        glob="zcs-${ZIMBRA_VERSION}_*.tgz"
    fi
    find_newest_tgz "${glob}"
}

# Emit `--dns` flags for the build container from the space-separated
# DOCKER_DNS list. Empty DOCKER_DNS = no flags (use the daemon's DNS).
docker_dns_args() {
    if [ -z "${DOCKER_DNS}" ]; then
        return 0
    fi
    local dns args=()
    for dns in ${DOCKER_DNS}; do
        args+=(--dns "${dns}")
    done
    printf '%s\n' "${args[@]}"
}

run_build() {
    header "Building Zimbra ${ZIMBRA_VERSION}"
    
    # Skip the (2-6 hour) build if an installer for this version already exists.
    local existing
    existing=$(find_built_tgz)
    if [ -n "${existing}" ] && [ "${FORCE_REBUILD}" != "true" ]; then
        log "Installer already built: ${existing}"
        log "Skipping build — use --rebuild (or FORCE_REBUILD=true) to force a fresh build."
        return 0
    fi
    
    local tag
    tag=$(image_tag)
    
    # Create output directory
    mkdir -p "${BUILD_DIR}"
    
    log "Output directory: ${BUILD_DIR}"
    log "Target OS:        ${BASE_IMAGE}"
    log "Zimbra version:   ${ZIMBRA_VERSION}"
    log ""
    log "⏳ This will take 2-6 hours depending on hardware..."
    log "   Press Ctrl+C to cancel (build state will be lost)"
    echo ""
    
    docker run ${DOCKER_RUN_OPTS} \
        --rm \
        $(docker_dns_args) \
        -e ZIMBRA_VERSION="${ZIMBRA_VERSION}" \
        -e FORCE_REBUILD="${FORCE_REBUILD}" \
        -v "$(pwd)/${BUILD_DIR}:/home/build/installer-build/BUILDS" \
        "${tag}" \
        --build
}

show_info() {
    resolve_version
    
    header "Build Information"
    echo "  Base Image:       ${BASE_IMAGE}"
    echo "  Zimbra Version:   ${ZIMBRA_VERSION}"
    echo "  Image Name:       $(image_tag)"
    echo "  Output Dir:       $(pwd)/${BUILD_DIR}"
    echo "  Build UID/GID:    ${BUILD_UID}/${BUILD_GID}"
    echo ""
}

do_clean() {
    header "Cleaning Build Artifacts"
    
    if [ -d "${BUILD_DIR}" ]; then
        log "Removing ${BUILD_DIR}..."
        rm -rf "${BUILD_DIR}"
    fi
    
    # Remove builder images
    local images
    images=$(docker images -q "${IMAGE_NAME}" 2>/dev/null || true)
    if [ -n "${images}" ]; then
        log "Removing builder images..."
        echo "${images}" | xargs -r docker rmi -f
    fi
    
    log "Clean complete"
}

list_images() {
    echo ""
    echo "Available base images for --base-image:"
    echo ""
    echo "  Debian/Ubuntu:"
    echo "    ubuntu:22.04       (recommended, most tested)"
    echo "    ubuntu:24.04       (newer LTS)"
    echo "    ubuntu:20.04       (older LTS)"
    echo ""
    echo "  RHEL-compatible:"
    echo "    rockylinux:9       (RHEL 9 compatible)"
    echo "    rockylinux:8       (RHEL 8 compatible)"
    echo "    almalinux:9        (RHEL 9 compatible)"
    echo "    oraclelinux:9      (RHEL 9 compatible)"
    echo ""
}

do_test() {
    header "Testing Build Container"
    
    local tag
    tag=$(image_tag)
    
    docker run --rm "${tag}" --test
    
    log "Container test passed"
}

# ═══════════════════════════════════════════════════════════
#  DEPLOY SECTION
# ═══════════════════════════════════════════════════════════

# Builds the common ssh/scp option array (host-key, key, port, extra opts).
deploy_ssh_args() {
    DEPLOY_SSH_ARGS=(-o StrictHostKeyChecking=accept-new)
    [ -n "${DEPLOY_SSH_PORT}" ] && DEPLOY_SSH_ARGS+=(-p "${DEPLOY_SSH_PORT}")
    [ -n "${DEPLOY_SSH_KEY}" ] && DEPLOY_SSH_ARGS+=(-i "${DEPLOY_SSH_KEY}")
    if [ -n "${DEPLOY_SSH_OPTS}" ]; then
        local -a extra=()
        read -r -a extra <<< "${DEPLOY_SSH_OPTS}"
        DEPLOY_SSH_ARGS+=("${extra[@]}")
    fi
}

# Human-readable size (B, KB, MB, ...)
hr_size() {
    local val="$1" u=0
    local units=(B KB MB GB TB)
    while [ "${val}" -ge 1024 ] && [ "${u}" -lt 4 ]; do
        val=$((val / 1024)); u=$((u + 1))
    done
    printf '%s %s' "${val}" "${units[$u]}"
}

# Determinate-progress upload (pv bar if available, else scp's native meter)
transfer_file() {
    local src="$1" dest="$2" target="$3"
    shift 3
    local -a ssh_args=("$@")
    local -a scp_args=()
    local size a
    size=$(stat -c%s "${src}" 2>/dev/null || stat -f%z "${src}" 2>/dev/null || printf '0')
    log "  ⇪ $(basename "${src}")  ($(hr_size "${size}")) → ${target}:${dest}"
    if command -v pv >/dev/null 2>&1 && [ "${size}" -gt 0 ]; then
        pv -pterb -s "${size}" "${src}" | ssh "${ssh_args[@]}" "${target}" "cat > '${dest}'"
    else
        # scp uses -P for the port (ssh uses -p) — translate
        for a in "${ssh_args[@]}"; do
            if [ "${a}" = "-p" ]; then scp_args+=("-P"); else scp_args+=("${a}"); fi
        done
        scp "${scp_args[@]}" "${src}" "${target}:${dest}"
    fi
}

# Runs "$@" (an ssh command) streaming its output to the terminal while
# showing an animated progress line + elapsed time. Output is also captured to
# a temp log. Returns the command's exit status.
#
# Incremental reading uses a byte offset + a "pending" buffer holding any
# trailing partial line, so partially-written lines are never printed twice and
# the final output is always flushed.
run_stream() {
    local logfile rc=0 pid line i=0 start secs elapsed animate=false
    local off=0 pending=""
    local spinner='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    [ -t 1 ] && animate=true
    logfile=$(mktemp)
    start=$(date +%s)

    ( "$@" >"${logfile}" 2>&1 ) &
    pid=$!

    while :; do
        # append newly-appended bytes since the last offset. Read byte-exactly
        # (NUL-delimited read preserves trailing newlines) and advance the
        # offset by the bytes ACTUALLY consumed, so no byte is ever skipped
        # or duplicated, regardless of shell newline-stripping behavior.
        local block="" start_off="${off}"
        # `|| [ -n ]` keeps the final block: read returns EOF status when the
        # NUL delimiter is never found, but still fills the variable.
        while IFS= read -r -d '' block || [ -n "${block}" ]; do
            off=$((off + $(printf '%s' "${block}" | wc -c)))
            pending="${pending}${block}"
        done < <(tail -c +$((start_off + 1)) "${logfile}" 2>/dev/null)

        # emit every complete (newline-terminated) line, keeping only the
        # trailing partial line buffered for the next pass. %%/*# split at the
        # FIRST newline only, so partial lines are never printed prematurely.
        while [[ "${pending}" == *$'\n'* ]]; do
            line="${pending%%$'\n'*}"
            pending="${pending#*$'\n'}"
            line=${line%$'\r'}
            if ${animate}; then
                printf '\r\033[K%s\n' "${line}"
            else
                printf '%s\n' "${line}"
            fi
        done

        if ${animate}; then
            secs=$(( $(date +%s) - start ))
            if [ "${secs}" -ge 3600 ]; then
                elapsed=$(printf '%d:%02d:%02d' $((secs/3600)) $(((secs%3600)/60)) $((secs%60)))
            else
                elapsed=$(printf '%02d:%02d' $((secs/60)) $((secs%60)))
            fi
            printf '\r\033[K⏳ %s  %s elapsed' "${spinner:$i:1}" "${elapsed}"
            i=$(( (i + 1) % ${#spinner} ))
        fi

        if ! kill -0 "${pid}" 2>/dev/null; then
            break
        fi
        sleep 1
    done

    # final incremental read, then flush whatever remains (partial final line)
    local block="" start_off="${off}"
    while IFS= read -r -d '' block || [ -n "${block}" ]; do
        pending="${pending}${block}"
    done < <(tail -c +$((start_off + 1)) "${logfile}" 2>/dev/null)
    if [ -n "${pending}" ]; then
        pending=${pending%$'\r'}
        if ${animate}; then
            printf '\r\033[K%s\n' "${pending}"
        else
            printf '%s\n' "${pending}"
        fi
    fi

    wait "${pid}" 2>/dev/null
    rc=$?

    ${animate} && printf '\r\033[K'
    echo ""
    rm -f "${logfile}"
    return "${rc}"
}

do_deploy() {
    local target="${1:-}"
    local tgz_file="${2:-}"
    local config_file="${3:-}"
    local -a ssh_args=()
    local remote_tgz="" tty_flag="" ssh_err=""

    if [ -z "${target}" ]; then
        err "Deploy requires a target: ./build.sh deploy user@host"
        exit 1
    fi

    header "Deploying to ${target}"

    deploy_ssh_args
    ssh_args=("${DEPLOY_SSH_ARGS[@]}")
    check_pv

    # Find the installer .tgz
    if [ -z "${tgz_file}" ]; then
        tgz_file=$(find_newest_tgz) || true
    fi

    if [ -z "${tgz_file}" ] || [ ! -f "${tgz_file}" ]; then
        err "No Zimbra installer found: ${tgz_file:-${BUILD_DIR}/}"
        err "Run ./build.sh first, or specify: ./build.sh deploy user@host path/to/zcs-*.tgz (or --tgz)"
        exit 1
    fi

    log "Installer: ${tgz_file}"
    echo ""

    # ── Phase 1: connectivity + remote dir ─────────────────
    log "[1/4] Checking SSH access to ${target} ..."
    if ! ssh_err=$(ssh "${ssh_args[@]}" "${target}" 'mkdir -p /usr/src' 2>&1); then
        err "Cannot connect to ${target}."
        if [ -n "${ssh_err}" ]; then
            err "SSH said: $(printf '%s' "${ssh_err}" | head -1)"
        fi
        err "Install your SSH key first:"
        err "    ssh-copy-id ${target}"
        err "Or set DEPLOY_SSH_KEY / DEPLOY_SSH_PORT / DEPLOY_SSH_OPTS in .env."
        exit 1
    fi
    log "[1/4] ✓ SSH access OK"

    # ── Phase 2: installer tgz (determinate progress) ───────
    remote_tgz="/usr/src/$(basename "${tgz_file}")"
    log "[2/4] Uploading installer ..."
    transfer_file "${tgz_file}" "${remote_tgz}" "${target}" "${ssh_args[@]}"

    # ── Phase 3: build.sh (merged builder + installer) ─────
    log "[3/4] Uploading build script ..."
    transfer_file "${SCRIPT_DIR}/build.sh" "/usr/src/build.sh" "${target}" "${ssh_args[@]}"
    if [ -n "${config_file}" ] && [ -f "${config_file}" ]; then
        transfer_file "${config_file}" "/usr/src/install-config.env" "${target}" "${ssh_args[@]}"
    elif [ -f "${SCRIPT_DIR}/scripts/install-config.env" ]; then
        transfer_file "${SCRIPT_DIR}/scripts/install-config.env" "/usr/src/install-config.env" "${target}" "${ssh_args[@]}"
    else
        warn "No install config found — the remote install will run with defaults."
        warn "Create scripts/install-config.env (see README) for a fully automatic install."
    fi

    # ── Phase 4: run the installer (live output + progress) ─
    log "[4/4] Running installer on ${target} (15-45 min)..."
    echo ""
    if [[ "${target}" != root@* ]]; then
        tty_flag="-t"
        warn "Non-root target: you may be prompted for the remote sudo password."
        warn "Run deploy from an interactive terminal — non-TTY runs cannot prompt for sudo."
    fi
    if ! run_stream ssh "${ssh_args[@]}" ${tty_flag:+"${tty_flag}"} "${target}" "sudo bash /usr/src/build.sh install --config /usr/src/install-config.env ${remote_tgz}"; then
        warn ""
        warn "Install script exited with error. Check output above."
        warn "You can re-run manually on the server:"
        warn "  ssh ${target}"
        warn "  sudo bash /usr/src/build.sh install --config /usr/src/install-config.env ${remote_tgz}"
        exit 1
    fi

    echo ""
    log "Deploy complete!"
    log "Zimbra is now running on ${target}"
}

# ═══════════════════════════════════════════════════════════
#  INSTALL SECTION  (runs on the mail server, as root)
# ═══════════════════════════════════════════════════════════

# Detect OS family (sets OS_ID, OS_FAMILY, PKG_MGR)
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

check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        err "This script must be run as root"
        exit 1
    fi
    if [ "${DRY_RUN}" = "true" ]; then
        warn "DRY RUN MODE — no changes will be made"
    fi
}

# Idempotency check
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

load_config() {
    local cfg="${1:-}"
    if [ -n "${cfg}" ] && [ -f "${cfg}" ]; then
        log "Loading config: ${cfg}"
        set -a; source "${cfg}"; set +a
    fi
}

# ── Section 1: System Preparation ──────────────────────────

install_deps() {
    header "Installing Dependencies"
    
    if [ "${SKIP_DEPS}" = "true" ]; then
        warn "Skipping dependency installation"
        return 0
    fi
    
    # Idempotent: skip the (slow) package install + `dnf update` on re-runs.
    if [ -f "${DEPS_MARKER}" ] && [ "${FORCE_REINSTALL:-false}" != "true" ]; then
        info "Dependencies already installed (${DEPS_MARKER}), skipping."
        return 0
    fi
    
    if [ "${PKG_MGR}" = "apt" ]; then
        dry apt update
        dry apt -y install gcc make g++ openssl libxml2-dev perl net-tools \
            gnupg2 locate git software-properties-common openjdk-8-jdk \
            ant ant-optional ruby maven build-essential rsyslog debhelper \
            python3 python3-dev python3-venv libaugeas0 libaugeas-dev
    elif [ "${PKG_MGR}" = "dnf" ]; then
        dry dnf -y update
        dry dnf -y install gcc gcc-c++ make openssl libxml2-devel perl \
            net-tools gnupg2 mlocate git java-1.8.0-openjdk-devel ant \
            ruby ruby-devel maven rsyslog python3 python3-devel \
            augeas augeas-devel
    fi
    
    dry mkdir -p "$(dirname "${DEPS_MARKER}")"
    dry touch "${DEPS_MARKER}"
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

# ── Section 2: Zimbra Installation ─────────────────────────

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
        err "No Zimbra installer found. Provide a .tgz file, --url, or run the build first."
        err "Usage: $0 install [path/to/zcs-*.tgz]"
        exit 1
    fi
    
    log "Using installer: ${tgz_path}"
    
    # Canonicalize to an absolute path. find_newest_tgz returns a path
    # relative to the script dir (e.g. ./builds/...); the `cd /usr/src` below
    # would otherwise break that relative path, making tar report "No such
    # file or directory" for a file that clearly exists.
    tgz_path="$(cd "$(dirname "${tgz_path}")" && pwd)/$(basename "${tgz_path}")"

    # Extract
    cd /usr/src
    local zdir
    # `|| true`: tar tzf can fail on a truncated download; empty zdir is
    # handled below so it can never point the rm at /usr/src itself.
    zdir=$(tar tzf "${tgz_path}" 2>/dev/null | head -1 | cut -d/ -f1) || true
    
    if [ "${DRY_RUN}" = "true" ]; then
        info "[DRY-RUN] Would extract ${tgz_path} and run the Zimbra installer"
        return 0
    fi
    
    if [ -z "${zdir}" ]; then
        err "Could not read installer archive: ${tgz_path}"
        if [ ! -r "${tgz_path}" ]; then
            err "The file is not readable — fix permissions and retry:"
            err "    sudo chmod 644 '${tgz_path}'"
        elif [ ! -s "${tgz_path}" ]; then
            err "The file is empty — re-download or rebuild it."
        else
            err "The .tgz may be corrupt or truncated — re-download it and try again."
            info "tar said:"
            tar tzf "${tgz_path}" >/dev/null || true
        fi
        exit 1
    fi
    
    if [ -d "/usr/src/${zdir}" ]; then
        dry rm -rf "/usr/src/${zdir}"
    fi
    
    dry tar zxf "${tgz_path}"
    cd "/usr/src/${zdir}"
    
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

# ── Section 3: Post-Install Configuration ──────────────────

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

# ── Section 4: SSL / Let's Encrypt ─────────────────────────

setup_letsencrypt() {
    if [ "${SKIP_SSL}" = "true" ]; then return 0; fi
    
    header "Setting up Let's Encrypt SSL"
    
    # Idempotent: skip issuance if a cert already exists for the primary domain.
    # certbot's non-interactive mode would otherwise error on an existing cert.
    local fullchain="/etc/letsencrypt/live/${SSL_DOMAINS[0]}/fullchain.pem"
    local privkey="/etc/letsencrypt/live/${SSL_DOMAINS[0]}/privkey.pem"
    if [ -f "${fullchain}" ] && [ -f "${privkey}" ]; then
        info "Certificate already exists for ${SSL_DOMAINS[0]}, skipping issuance."
        return 0
    fi
    
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

# ── Section 5: Outbound Relay ──────────────────────────────

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

# ── Section 6: Zextras Theme ───────────────────────────────

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

# ── Section 7: Email Relay (for ISP multi-server) ──────────

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

# ── Section 8: Swap ────────────────────────────────────────

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

# ── Section 9: Snap Loop Fix ───────────────────────────────

fix_snap_loops() {
    header "Fixing Snap Loop Reports"
    
    local loop_devices
    # `|| true`: with pipefail, grep exits 1 when there are no loop devices,
    # which would otherwise abort the whole install under `set -e`.
    loop_devices=$(df -Th 2>/dev/null | grep '/dev/loop' | awk '{print ":"$1}' | tr -d '\n' | sed 's/^://') || true
    
    if [ -n "${loop_devices}" ]; then
        dry su - zimbra -c "zmlocalconfig -e zmstat_df_excludes='${loop_devices}'"
        dry su - zimbra -c 'zmstatctl restart'
        log "Snap loops excluded"
    fi
}

# ── Section 10: Final Cleanup & Restart ────────────────────

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

# ── Install orchestrator ───────────────────────────────────
run_install() {
    local tgz="${1:-}"
    local cfg="${CONFIG_FILE:-}"
    
    # Banner
    echo -e "\n${BLUE}${BOLD}  ╔══════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}${BOLD}  ║${NC}     ${BOLD}Zimbra FOSS — Universal Installer${NC}       ${BLUE}${BOLD}║${NC}"
    echo -e "${BLUE}${BOLD}  ╚══════════════════════════════════════════╝${NC}\n"
    
    detect_os
    check_root
    # Load config after the root check so a non-root run fails before
    # sourcing the (password-bearing) config file.
    if [ -z "${cfg}" ] && [ -f "${SCRIPT_DIR}/scripts/install-config.env" ]; then
        cfg="${SCRIPT_DIR}/scripts/install-config.env"
    fi
    load_config "${cfg}"
    check_already_installed
    
    # Resolve installer .tgz: explicit arg > ZIMBRA_TGZ_PATH > newest build
    if [ -z "${tgz}" ]; then
        tgz="${ZIMBRA_TGZ_PATH:-}"
    fi
    if [ -z "${tgz}" ]; then
        tgz=$(find_newest_tgz || true)
        if [ -n "${tgz}" ]; then
            log "Using newest build: ${tgz}"
        fi
    fi
    
    install_deps
    remove_postfix
    install_webmin
    set_hostname
    configure_hosts
    disable_ipv6
    disable_firewall
    install_zimbra "${tgz}"
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

# ═══════════════════════════════════════════════════════════
#  HELP
# ═══════════════════════════════════════════════════════════

show_help() {
    banner
    echo "Usage: ./build.sh [COMMAND] [OPTIONS]"
    echo ""
    echo "Commands:"
    echo "  build       Build Zimbra FOSS installer (default)"
    echo "  install     Install Zimbra on this server (after a build, or standalone)"
    echo "  deploy      Deploy installer to remote server via SSH"
    echo "  info        Show build configuration"
    echo "  clean       Remove build artifacts and images"
    echo "  test        Test the build container"
    echo "  list        List available base OS images"
    echo "  help        Show this help"
    echo ""
    echo "The default 'build' command also installs on this server when run as"
    echo "root. Use --skip-install to build only."
    echo ""
    echo "Build options (can also be set in .env file):"
    echo "  --version VERSION    Zimbra version (default: latest)"
    echo "  --base-image IMAGE   Docker base image (default: ubuntu:22.04)"
    echo "  --output DIR         Output directory (default: ./builds)"
    echo "  --skip-install       Build only, do not install afterwards"
    echo "  --rebuild            Rebuild even if the installer .tgz already exists"
    echo ""
    echo "Install options:"
    echo "  --config FILE        Install configuration file (default: scripts/install-config.env)"
    echo "  --dry-run            Show what would be done, don't do it"
    echo "  --skip-deps          Skip dependency installation"
    echo "  --skip-ssl           Skip SSL certificate setup"
    echo "  --skip-theme         Skip theme installation"
    echo "  --skip-relay         Skip relay configuration"
    echo "  --hostname NAME      Set server hostname"
    echo "  --domain NAME        Set email domain"
    echo "  --admin-pass PASS    Set admin password"
    echo "  --force              Re-install even if Zimbra is already installed"
    echo ""
    echo "Deploy / SSH (env vars, see .env):"
    echo "  DEPLOY_SSH_KEY       Path to an SSH private key, e.g. ~/.ssh/id_ed25519"
    echo "  DEPLOY_SSH_PORT      SSH port if not 22"
    echo "  DEPLOY_SSH_OPTS      Extra ssh options, e.g. \"-o ConnectTimeout=15\""
    echo ""
    echo "Examples:"
    echo "  ./build.sh                                    # Build latest + install here (as root)"
    echo "  ./build.sh build --skip-install               # Build only"
    echo "  ./build.sh install ./builds/zcs-*.tgz         # Install a specific build"
    echo "  ./build.sh install --config my.env            # Install with custom config"
    echo "  ./build.sh build --version 10.1.16            # Build specific version"
    echo "  ./build.sh build --base-image rockylinux:9    # Build for Rocky Linux 9"
    echo "  ./build.sh deploy root@mail.example.com       # Deploy to remote server"
    echo "  ./build.sh deploy user@host --config my.env   # Deploy with custom config"
    echo ""
    echo "Config files: .env (build) and scripts/install-config.env (install)"
    echo ""
}

show_install_help() {
    echo "Usage: ./build.sh install [OPTIONS] [ZIMBRA_TGZ_PATH]"
    echo ""
    echo "Options:"
    echo "  --config FILE      Load configuration from FILE (default: scripts/install-config.env)"
    echo "  --url URL          Download Zimbra installer from URL"
    echo "  --dry-run          Show what would be done, don't do it"
    echo "  --skip-deps        Skip dependency installation"
    echo "  --skip-ssl         Skip SSL certificate setup"
    echo "  --skip-theme       Skip theme installation"
    echo "  --skip-relay       Skip relay configuration"
    echo "  --hostname NAME    Set server hostname"
    echo "  --domain NAME      Set email domain"
    echo "  --admin-pass PASS  Set admin password"
    echo "  --force            Re-install even if Zimbra is already installed"
    echo ""
    echo "Examples:"
    echo "  $0 install zcs-10.1.16_GA_*.tgz"
    echo "  $0 install --config install-config.env zcs-*.tgz"
    echo "  $0 install --url https://example.com/zcs-*.tgz --dry-run"
}

# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

case "${1:-}" in
    -h|--help|help) show_help; exit 0 ;;
esac

# A first argument that is a long option (e.g. --base-image ...) implies 'build'
COMMAND="${1:-build}"
if [[ "${COMMAND}" == --* ]]; then
    COMMAND=build
else
    shift 2>/dev/null || true
fi

VERSION_EXPLICIT=false
BASE_IMAGE_EXPLICIT=false

# Parse global options; stop at the first positional argument (e.g. the
# deploy target), which is left untouched for the command handler below.
while [ $# -gt 0 ]; do
    case "$1" in
        --version)     ZIMBRA_VERSION="$2"; VERSION_EXPLICIT=true; shift 2 ;;
        --base-image)  BASE_IMAGE="$2"; BASE_IMAGE_EXPLICIT=true; shift 2 ;;
        --output)      BUILD_DIR="$2"; shift 2 ;;
        --uid)         BUILD_UID="$2"; shift 2 ;;
        --gid)         BUILD_GID="$2"; shift 2 ;;
        --no-cache)    NO_CACHE=true; shift ;;
        --skip-install) SKIP_INSTALL=true; shift ;;
        --rebuild)     FORCE_REBUILD=true; shift ;;
        *)             break ;;
    esac
done

case "${COMMAND}" in
    build)
        banner
        detect_host_os
        check_prereqs
        
        # Interactive pickers (skip if explicit args or non-TTY)
        if [ -t 0 ] && ! ${VERSION_EXPLICIT:-false}; then
            choose_version
        else
            resolve_version
        fi
        
        if [ -t 0 ] && ! ${BASE_IMAGE_EXPLICIT:-false}; then
            choose_base_image
        fi
        
        build_image
        run_build
        
        # ── Auto-install after a successful build ──────────
        if [ "${SKIP_INSTALL}" = "true" ]; then
            log "Skipping install step (--skip-install)"
        elif [ "$(id -u)" -eq 0 ]; then
            log "Build complete — running installer on this server..."
            run_install
        else
            warn "Build complete, but not running as root — skipping local install."
            warn "Run 'sudo ./build.sh install' to install here, or './build.sh deploy user@host'."
        fi
        ;;
    install)
        # NOTE: no `local` here — this runs at top level, not inside a function
        tgz=""
        cfg=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --config)      cfg="$2"; shift 2 ;;
                --url)         ZIMBRA_TGZ_URL="$2"; shift 2 ;;
                --dry-run)     DRY_RUN=true; shift ;;
                --skip-deps)   SKIP_DEPS=true; shift ;;
                --skip-ssl)    SKIP_SSL=true; shift ;;
                --skip-theme)  SKIP_THEME=true; shift ;;
                --skip-relay)  SKIP_RELAY=true; shift ;;
                --hostname)    HOSTNAME="$2"; shift 2 ;;
                --domain)      DOMAIN="$2"; shift 2 ;;
                --admin-pass)  ZIMBRA_ADMIN_PASSWORD="$2"; shift 2 ;;
                --force)       FORCE_REINSTALL=true; shift ;;
                -h|--help)     show_install_help; exit 0 ;;
                -*)
                    if [ -f "$1" ]; then
                        tgz="$1"; shift
                    else
                        err "Unknown option: $1"; show_install_help; exit 1
                    fi
                    ;;
                *)             tgz="$1"; shift ;;
            esac
        done
        CONFIG_FILE="${cfg}"
        run_install "${tgz}"
        ;;
    info)
        show_info
        ;;
    clean)
        do_clean
        ;;
    test)
        check_prereqs
        resolve_version
        build_image
        do_test
        ;;
    list)
        list_images
        ;;
    deploy)
        # NOTE: no `local` here — this runs at top level, not inside a function
        target=""
        tgz=""
        cfg=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --config) cfg="$2"; shift 2 ;;
                --tgz)    tgz="$2"; shift 2 ;;
                *)        if [ -z "${target}" ]; then target="$1";
                           elif [ -z "${tgz}" ]; then tgz="$1"; fi; shift ;;
            esac
        done
        do_deploy "${target}" "${tgz}" "${cfg}"
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        err "Unknown command: ${COMMAND}"
        show_help
        exit 1
        ;;
esac
