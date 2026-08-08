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
    
    log "All prerequisites met"
}

# ── Wiki-based version resolution ─────────────────────────
WIKI_URL="https://wiki.zimbra.com/wiki/Zimbra_Foss_Source_Code_Only_Releases"

# Fetch released versions from the Zimbra wiki
# Returns versions sorted newest-first, one per line
fetch_released_versions() {
    curl -sL --max-time 15 "${WIKI_URL}" 2>/dev/null | \
        awk '/^[0-9]+\.[0-9]+\.[0-9]+/{ver=$0; next} /^Released$/{print ver}' | \
        sort -t. -k1,1nr -k2,2nr -k3,3nr
}

# Resolve Zimbra version from wiki or explicit value
resolve_version() {
    if [ "${ZIMBRA_VERSION}" = "latest" ]; then
        header "Resolving Latest Zimbra Version"
        info "Source: ${WIKI_URL}"
        
        local version
        version=$(fetch_released_versions | head -1)
        
        ZIMBRA_VERSION="${version:-10.1.16}"
        log "Latest released Zimbra FOSS version: ${ZIMBRA_VERSION}"
    fi
}

# Interactive version picker
choose_version() {
    header "Select Zimbra Version"
    info "Source: ${WIKI_URL}"
    
    local versions
    mapfile -t versions < <(fetch_released_versions)
    
    if [ ${#versions[@]} -eq 0 ]; then
        warn "Could not fetch versions from wiki, using 10.1.16"
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

# ── Deploy to remote server ────────────────────────────────
do_deploy() {
    local target="${1:-}"
    local tgz_file="${2:-}"
    local config_file="${3:-}"
    
    if [ -z "${target}" ]; then
        err "Deploy requires a target: ./build.sh deploy user@host"
        exit 1
    fi
    
    header "Deploying to ${target}"
    
    # Find the installer .tgz
    if [ -z "${tgz_file}" ]; then
        tgz_file=$(ls -t "${BUILD_DIR}"/zcs-*.tgz 2>/dev/null | head -1)
    fi
    
    if [ -z "${tgz_file}" ] || [ ! -f "${tgz_file}" ]; then
        err "No Zimbra installer found in ${BUILD_DIR}/"
        err "Run ./build.sh first, or specify: ./build.sh deploy user@host path/to/zcs-*.tgz"
        exit 1
    fi
    
    log "Installer: ${tgz_file}"
    
    # Copy installer to remote
    log "Copying installer to ${target}:/usr/src/ ..."
    scp "${tgz_file}" "${target}:/usr/src/" || {
        err "SCP failed. Check SSH access to ${target}"
        exit 1
    }
    
    local remote_tgz="/usr/src/$(basename "${tgz_file}")"
    
    # Copy install scripts
    log "Copying install scripts..."
    scp "${SCRIPT_DIR}/scripts/install.sh" "${target}:/usr/src/install.sh"
    scp "${SCRIPT_DIR}/scripts/install-config.env" "${target}:/usr/src/install-config.env" 2>/dev/null || true
    
    # Copy custom config if specified
    if [ -n "${config_file}" ] && [ -f "${config_file}" ]; then
        scp "${config_file}" "${target}:/usr/src/install-config.env"
    fi
    
    # Run the install
    log "Running installer on ${target}..."
    echo ""
    echo "  ┌─────────────────────────────────────────────┐"
    echo "  │  Connecting to ${target}"
    echo "  │  The install will take 15-45 minutes"
    echo "  │  You'll be prompted for sudo password"
    echo "  └─────────────────────────────────────────────┘"
    echo ""
    
    ssh -t "${target}" "sudo bash /usr/src/install.sh --config /usr/src/install-config.env ${remote_tgz}" || {
        warn "Install script exited with error. Check output above."
        warn "You can re-run manually on the server:"
        warn "  ssh ${target}"
        warn "  sudo bash /usr/src/install.sh --config /usr/src/install-config.env ${remote_tgz}"
        exit 1
    }
    
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
COMMAND="${1:-build}"
shift 2>/dev/null || true

VERSION_EXPLICIT=false
BASE_IMAGE_EXPLICIT=false

while [ $# -gt 0 ]; do
    case "$1" in
        --version)     ZIMBRA_VERSION="$2"; VERSION_EXPLICIT=true; shift 2 ;;
        --base-image)  BASE_IMAGE="$2"; BASE_IMAGE_EXPLICIT=true; shift 2 ;;
        --output)      BUILD_DIR="$2"; shift 2 ;;
        --uid)         BUILD_UID="$2"; shift 2 ;;
        --gid)         BUILD_GID="$2"; shift 2 ;;
        --no-cache)    NO_CACHE=true; shift ;;
        *)             warn "Unknown option: $1"; shift ;;
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
        local target="${1:-}"; shift
        local tgz=""; local cfg=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --config) cfg="$2"; shift 2 ;;
                --tgz)    tgz="$2"; shift 2 ;;
                *)        shift ;;
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
