#!/bin/bash
set -euo pipefail

# ┌──────────────────────────────────────────────────────────┐
# │  Zimbra FOSS Builder — Entrypoint                        │
# │  Detects OS, resolves latest version, runs zm-build      │
# └──────────────────────────────────────────────────────────┘

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }
info() { echo -e "${BLUE}[INFO]${NC} $*"; }

# ── Detect OS ──────────────────────────────────────────────
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS_ID="${ID}"
        OS_VERSION="${VERSION_ID}"
        OS_CODENAME="${VERSION_CODENAME:-}"
        OS_NAME="${PRETTY_NAME:-${ID} ${VERSION_ID}}"
    elif [ -f /etc/redhat-release ]; then
        OS_ID="rhel"
        OS_VERSION=$(rpm -q --qf "%{VERSION}" "$(rpm -q --whatprovides redhat-release)")
        OS_NAME="$(cat /etc/redhat-release)"
    else
        err "Cannot detect OS"
        exit 1
    fi
    log "Detected OS: ${OS_NAME}"
    log "OS ID: ${OS_ID}, Version: ${OS_VERSION}"
}

# ── Official Zimbra repo for version resolution ────────────
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

# ── Resolve latest Zimbra version ──────────────────────────
resolve_version() {
    local requested="${1:-latest}"

    if [ "${requested}" = "latest" ]; then
        log "Fetching latest released Zimbra FOSS version from official repo..."
        log "  ${ZIMBRA_REPO}"
        
        local version
        version=$(fetch_released_versions | head -1 || true)
        
        if [ -z "${version}" ]; then
            err "Could not determine latest version from ${ZIMBRA_REPO}. Falling back to 10.1.16"
            ZIMBRA_VERSION="10.1.16"
        else
            ZIMBRA_VERSION="${version}"
        fi
    else
        ZIMBRA_VERSION="${requested}"
    fi
    
    # Validate version format
    if ! echo "${ZIMBRA_VERSION}" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
        err "Invalid version format: ${ZIMBRA_VERSION}. Expected X.Y.Z"
        exit 1
    fi
    
    # Determine build release name (DAFFODIL for 10.x)
    local major
    major=$(echo "${ZIMBRA_VERSION}" | cut -d. -f1)
    case "${major}" in
        10) BUILD_RELEASE="DAFFODIL" ;;
        9)  BUILD_RELEASE="KEIPLER" ;;
        8)  BUILD_RELEASE="JOULE" ;;
        *)  BUILD_RELEASE="DAFFODIL" ; warn "Unknown major version ${major}, defaulting to DAFFODIL" ;;
    esac
    
    log "Zimbra Version: ${ZIMBRA_VERSION}"
    log "Build Release: ${BUILD_RELEASE}"
    
    export ZIMBRA_VERSION BUILD_RELEASE
}

# ── Determine OS target for build ──────────────────────────
resolve_os_target() {
    case "${OS_ID}" in
        ubuntu|debian)
            local ver
            ver=$(echo "${OS_VERSION}" | cut -d. -f1)
            BUILD_OS="UBUNTU${ver}_64"
            ;;
        rocky|almalinux|rhel|centos|ol|oracle)
            local ver
            ver=$(echo "${OS_VERSION}" | cut -d. -f1)
            BUILD_OS="RHEL${ver}_64"
            ;;
        *)
            warn "Unknown OS ID: ${OS_ID}, attempting generic build"
            BUILD_OS="UBUNTU22_64"
            ;;
    esac
    log "Build Target: ${BUILD_OS}"
    export BUILD_OS
}

# ── Generate git-default-tag list ──────────────────────────
generate_tag_list() {
    local major minor patch
    major=$(echo "${ZIMBRA_VERSION}" | cut -d. -f1)
    minor=$(echo "${ZIMBRA_VERSION}" | cut -d. -f2)
    patch=$(echo "${ZIMBRA_VERSION}" | cut -d. -f3)
    
    local tag_list="${ZIMBRA_VERSION}"
    
    # Add all previous patch versions (current minor)
    local p=$((patch - 1))
    while [ $p -ge 0 ]; do
        tag_list="${tag_list},${major}.${minor}.${p}"
        p=$((p - 1))
    done
    
    # Add previous minor versions (from 0 up to the last known patch)
    local m=$((minor - 1))
    while [ $m -ge 0 ]; do
        local max_p=20  # reasonable upper bound; zm-build will skip missing tags
        local pp=$max_p
        while [ $pp -ge 0 ]; do
            tag_list="${tag_list},${major}.${m}.${pp}"
            pp=$((pp - 1))
        done
        m=$((m - 1))
    done
    
    info "Git default tags: ${tag_list}" >&2
    echo "${tag_list}"
}

# ── Main build function ────────────────────────────────────

# Clone zm-build with retries to survive transient DNS/network failures.
# GIT_CLONE_ATTEMPTS / GIT_CLONE_DELAY tune the retry behavior. Each attempt
# removes any partial clone so git never trips over a leftover directory.
git_clone_retry() {
    local branch="$1"
    local attempts="${GIT_CLONE_ATTEMPTS:-5}"
    local delay="${GIT_CLONE_DELAY:-10}"
    local i=1
    while [ "${i}" -le "${attempts}" ]; do
        rm -rf zm-build 2>/dev/null || true
        if git clone --depth 1 --branch "${branch}" \
            https://github.com/Zimbra/zm-build.git 2>&1 | sed 's/^/  /'; then
            return 0
        fi
        if [ "${i}" -lt "${attempts}" ]; then
            warn "Clone of ${branch} failed (attempt ${i}/${attempts}) — retrying in ${delay}s..."
            sleep "${delay}"
        fi
        i=$((i + 1))
    done
    return 1
}

do_build() {
    log "============================================"
    log "  Zimbra FOSS Builder"
    log "  Version: ${ZIMBRA_VERSION}"
    log "  Release: ${BUILD_RELEASE}"
    log "  Target:  ${BUILD_OS}"
    log "  OS:      ${OS_NAME}"
    log "============================================"
    
    # Skip the (2-6 hour) build if an installer for this version + target OS
    # already exists in the mounted output dir (e.g. a previous `docker run`
    # reused ./builds). Matching ${BUILD_OS} means changing the target OS
    # (e.g. ubuntu:24.04 → rockylinux:9) triggers a rebuild.
    local existing os_glob
    if [ -n "${BUILD_OS:-}" ]; then
        os_glob="zcs-${ZIMBRA_VERSION}_*.${BUILD_OS}.*.tgz"
    else
        os_glob="zcs-${ZIMBRA_VERSION}_*.tgz"
    fi
    existing=$(ls -t "${OUTPUT_DIR}"/${os_glob} 2>/dev/null | head -1 || true)
    if [ -n "${existing}" ] && [ "${FORCE_REBUILD:-false}" != "true" ]; then
        log "Installer already built: ${existing}"
        log "Skipping build — set FORCE_REBUILD=true to force a fresh build."
        return 0
    fi
    
    # Create build directory — fix ownership in case Docker volume
    # mount created parent dirs as root-owned
    local uid_gid="$(id -u):$(id -g)"
    sudo mkdir -p "${BUILD_DIR}"
    sudo chown "${uid_gid}" "${BUILD_DIR}"
    # The BUILDS dir is a bind mount from the host (./builds), whose
    # ownership may not match this container's build user. Fix it so the
    # Deploy phase can write archives/ and the .tgz into the mount.
    sudo mkdir -p "${OUTPUT_DIR}"
    sudo chown "${uid_gid}" "${OUTPUT_DIR}"
    cd "${BUILD_DIR}"
    
    # Clean any previous build artifacts
    rm -rf zm-build 2>/dev/null || true
    
    # Pre-flight: fail fast (with a clear message) if the container cannot
    # resolve the repos it needs, instead of a cryptic "Could not resolve host"
    # surfacing mid-clone.
    if ! getent hosts github.com >/dev/null 2>&1; then
        err "DNS resolution failed — cannot resolve github.com."
        err "Check the container's network/DNS (or host /etc/resolv.conf) and re-run."
        exit 1
    fi
    
    # Clone zm-build — try tag first, then release branch. Each attempt retries
    # a few times so a transient DNS/network blip doesn't abort the whole build.
    log "Cloning zm-build (version ${ZIMBRA_VERSION})..."
    if ! git_clone_retry "${ZIMBRA_VERSION}"; then
        warn "Tag ${ZIMBRA_VERSION} not found, trying release/${ZIMBRA_VERSION}..."
        if ! git_clone_retry "release/${ZIMBRA_VERSION}"; then
            err "Failed to clone zm-build (tag ${ZIMBRA_VERSION} and release/${ZIMBRA_VERSION})."
            err "Check network/DNS access to github.com and re-run."
            exit 1
        fi
    fi
    
    cd zm-build
    
    # Generate tag list
    local tag_list
    tag_list=$(generate_tag_list)
    
    # Determine build number
    local build_no
    build_no=$(echo "${ZIMBRA_VERSION}" | tr -d '.')"0000"
    
    # Run the build
    log "Starting zm-build build.pl..."
    log "This will take 2-6 hours depending on your hardware..."
    log "Build output will be in: ${OUTPUT_DIR}"
    echo ""
    
    ENV_CACHE_CLEAR_FLAG=true \
        ./build.pl \
        --ant-options -DskipTests=true \
        --git-default-tag="${tag_list}" \
        --build-release-no="${ZIMBRA_VERSION}" \
        --build-type=FOSS \
        --build-release="${BUILD_RELEASE}" \
        --build-release-candidate=GA \
        --build-thirdparty-server=files.zimbra.com \
        --no-interactive \
        --build-no="${build_no}"
    
    local build_exit=$?
    
    if [ ${build_exit} -eq 0 ]; then
        log "============================================"
        log "  ✅ BUILD SUCCESSFUL!"
        log "  Installer: ${OUTPUT_DIR}/"
        ls -lh "${OUTPUT_DIR}/" 2>/dev/null || true
        log "============================================"
    else
        err "============================================"
        err "  ❌ BUILD FAILED (exit code: ${build_exit})"
        err "  Check logs above for details"
        err "============================================"
    fi
    
    return ${build_exit}
}

# ── Show info ──────────────────────────────────────────────
do_info() {
    detect_os
    resolve_version "${ZIMBRA_VERSION:-latest}"
    resolve_os_target
    
    echo ""
    log "============================================"
    log "  Build Configuration"
    log "============================================"
    log "  Zimbra Version:   ${ZIMBRA_VERSION}"
    log "  Build Release:    ${BUILD_RELEASE}"
    log "  Target OS:        ${BUILD_OS}"
    log "  Host OS:          ${OS_NAME}"
    log "  Build Dir:        ${BUILD_DIR}"
    log "  Output Dir:       ${OUTPUT_DIR}"
    log "============================================"
    echo ""
}

# ── Self-test ──────────────────────────────────────────────
do_test() {
    log "Running self-test..."
    
    detect_os
    
    # Check required tools
    local missing=()
    for tool in git java ant mvn gcc g++ make perl ruby rsync; do
        if ! command -v "${tool}" >/dev/null 2>&1; then
            missing+=("${tool}")
        fi
    done
    
    if [ ${#missing[@]} -gt 0 ]; then
        err "Missing tools: ${missing[*]}"
        exit 1
    fi
    
    # Check Java version
    java -version 2>&1 | head -1
    
    log "✅ Self-test passed"
}

# ── Main ───────────────────────────────────────────────────
main() {
    detect_os
    
    case "${1:-}" in
        --build)
            resolve_version "${ZIMBRA_VERSION:-latest}"
            resolve_os_target
            do_build
            ;;
        --info)
            do_info
            ;;
        --test)
            do_test
            ;;
        --version)
            resolve_version "${ZIMBRA_VERSION:-latest}"
            echo "${ZIMBRA_VERSION}"
            ;;
        --help|-h)
            echo "Usage: $0 [--build|--info|--test|--version]"
            echo ""
            echo "  --build    Build Zimbra FOSS from source (default)"
            echo "  --info     Show build configuration without building"
            echo "  --test     Run self-test to verify dependencies"
            echo "  --version  Resolve and print latest Zimbra version"
            echo ""
            echo "Environment variables:"
            echo "  ZIMBRA_VERSION   Specific version to build (default: latest)"
            echo "  BUILD_HOME       Build home directory"
            echo "  BUILD_DIR        Build working directory"
            ;;
        *)
            resolve_version "${ZIMBRA_VERSION:-latest}"
            resolve_os_target
            do_build
            ;;
    esac
}

main "$@"
