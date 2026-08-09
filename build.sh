#!/usr/bin/env bash
set -euo pipefail

# ┌──────────────────────────────────────────────────────────┐
# │  Zimbra FOSS Builder — One-command build script          │
# │  Builds Zimbra installer for any Linux OS via Docker     │
# └──────────────────────────────────────────────────────────┘

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*" >&2; }
info() { echo -e "${BLUE}[i]${NC} $*"; }
header() { echo -e "\n${BOLD}${BLUE}═══ $* ═══${NC}\n"; }

# ── Configuration (override via .env or environment) ───────
ZIMBRA_VERSION="${ZIMBRA_VERSION:-latest}"
BASE_IMAGE="${BASE_IMAGE:-ubuntu:22.04}"
IMAGE_NAME="${IMAGE_NAME:-zimbra-builder}"
BUILD_DIR="${BUILD_DIR:-./builds}"
BUILD_UID="${BUILD_UID:-1000}"
BUILD_GID="${BUILD_GID:-1000}"
DOCKER_BUILD_OPTS="${DOCKER_BUILD_OPTS:-}"
DOCKER_RUN_OPTS="${DOCKER_RUN_OPTS:-}"
NO_CACHE="${NO_CACHE:-false}"

# Deploy / SSH (set in .env or environment)
DEPLOY_SSH_KEY="${DEPLOY_SSH_KEY:-}"      # path to private key, e.g. ~/.ssh/id_ed25519
DEPLOY_SSH_PORT="${DEPLOY_SSH_PORT:-}"    # SSH port if not 22
DEPLOY_SSH_OPTS="${DEPLOY_SSH_OPTS:-}"    # extra ssh options, e.g. "-o ConnectTimeout=15"
DEPLOY_SSH_ARGS=()

# Load .env if present
if [ -f .env ]; then
    set -a; source .env; set +a
    info "Loaded configuration from .env"
fi

# ── Show banner ────────────────────────────────────────────
banner() {
    echo ""
    echo -e "${BLUE}  ╔══════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}  ║${NC}       ${BOLD}Zimbra FOSS Builder${NC}                      ${BLUE}║${NC}"
    echo -e "${BLUE}  ║${NC}    Build Zimbra from source in Docker       ${BLUE}║${NC}"
    echo -e "${BLUE}  ╚══════════════════════════════════════════════╝${NC}"
    echo ""
}

# ── Detect OS for nice default ─────────────────────────────
detect_host_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        info "Host OS: ${PRETTY_NAME:-${ID} ${VERSION_ID}}"
    fi
}

# ── Check prerequisites ────────────────────────────────────
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

# ── Derive image tag from base image ───────────────────────
image_tag() {
    local tag
    # Sanitize: rockylinux/rockylinux:9 → rockylinux-rockylinux-9
    tag=$(echo "${BASE_IMAGE}" | sed 's/[/:]/-/g' | sed 's/\./-/g')
    echo "${IMAGE_NAME}:${tag}-zimbra${ZIMBRA_VERSION}"
}

# ── Build the Docker image ─────────────────────────────────
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

# ── Run the build ──────────────────────────────────────────
run_build() {
    header "Building Zimbra ${ZIMBRA_VERSION}"
    
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
        -e ZIMBRA_VERSION="${ZIMBRA_VERSION}" \
        -v "$(pwd)/${BUILD_DIR}:/home/build/installer-build/BUILDS" \
        "${tag}" \
        --build
}

# ── Show info ──────────────────────────────────────────────
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

# ── Clean build artifacts ──────────────────────────────────
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

# ── List available base images ─────────────────────────────
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

# ── Test the container ─────────────────────────────────────
do_test() {
    header "Testing Build Container"
    
    local tag
    tag=$(image_tag)
    
    docker run --rm "${tag}" --test
    
    log "Container test passed"
}

# ── Deploy helpers ─────────────────────────────────────────
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

# ── Deploy to remote server ────────────────────────────────
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
        tgz_file=$(ls -t "${BUILD_DIR}"/zcs-*.tgz 2>/dev/null | head -1)
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

    # ── Phase 3: install scripts ────────────────────────────
    log "[3/4] Uploading install scripts ..."
    transfer_file "${SCRIPT_DIR}/scripts/install.sh" "/usr/src/install.sh" "${target}" "${ssh_args[@]}"
    if [ -n "${config_file}" ] && [ -f "${config_file}" ]; then
        transfer_file "${config_file}" "/usr/src/install-config.env" "${target}" "${ssh_args[@]}"
    else
        transfer_file "${SCRIPT_DIR}/scripts/install-config.env" "/usr/src/install-config.env" "${target}" "${ssh_args[@]}"
    fi

    # ── Phase 4: run the installer (live output + progress) ─
    log "[4/4] Running installer on ${target} (15-45 min)..."
    echo ""
    if [[ "${target}" != root@* ]]; then
        tty_flag="-t"
        warn "Non-root target: you may be prompted for the remote sudo password."
        warn "Run deploy from an interactive terminal — non-TTY runs cannot prompt for sudo."
    fi
    if ! run_stream ssh "${ssh_args[@]}" ${tty_flag:+"${tty_flag}"} "${target}" "sudo bash /usr/src/install.sh --config /usr/src/install-config.env ${remote_tgz}"; then
        warn ""
        warn "Install script exited with error. Check output above."
        warn "You can re-run manually on the server:"
        warn "  ssh ${target}"
        warn "  sudo bash /usr/src/install.sh --config /usr/src/install-config.env ${remote_tgz}"
        exit 1
    fi

    echo ""
    log "Deploy complete!"
    log "Zimbra is now running on ${target}"
}

# ── Help ───────────────────────────────────────────────────
show_help() {
    banner
    echo "Usage: ./build.sh [COMMAND] [OPTIONS]"
    echo ""
    echo "Commands:"
    echo "  build       Build Zimbra FOSS installer (default)"
    echo "  deploy      Deploy installer to remote server via SSH"
    echo "  info        Show build configuration"
    echo "  clean       Remove build artifacts and images"
    echo "  test        Test the build container"
    echo "  list        List available base OS images"
    echo "  help        Show this help"
    echo ""
    echo "Options (can also be set in .env file):"
    echo "  --version VERSION    Zimbra version (default: latest)"
    echo "  --base-image IMAGE   Docker base image (default: ubuntu:22.04)"
    echo "  --output DIR         Output directory (default: ./builds)"
    echo ""
    echo "Deploy / SSH (env vars, see .env):"
    echo "  DEPLOY_SSH_KEY       Path to an SSH private key, e.g. ~/.ssh/id_ed25519"
    echo "  DEPLOY_SSH_PORT      SSH port if not 22"
    echo "  DEPLOY_SSH_OPTS      Extra ssh options, e.g. \"-o ConnectTimeout=15\""
    echo ""
    echo "Examples:"
    echo "  ./build.sh                                    # Build latest for Ubuntu 22.04"
    echo "  ./build.sh build --version 10.1.16            # Build specific version"
    echo "  ./build.sh build --base-image ubuntu:24.04    # Build for Ubuntu 24.04"
    echo "  ./build.sh build --base-image rockylinux:9    # Build for Rocky Linux 9"
    echo "  ./build.sh info                               # Show what will be built"
    echo "  ./build.sh deploy root@mail.example.com       # Deploy to remote server"
    echo "  ./build.sh deploy user@host --config my.env   # Deploy with custom config"
    echo ""
    echo "Config file: .env (see .env.example)"
    echo ""
}

# ── Parse arguments ────────────────────────────────────────
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
        *)             break ;;
    esac
done

# ── Execute ────────────────────────────────────────────────
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
