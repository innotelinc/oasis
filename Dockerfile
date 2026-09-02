# ┌──────────────────────────────────────────────────────────┐
# │  Oasis — Zimbra FOSS Builder Multi-Distro Image         │
# │  Builds the latest Zimbra FOSS installer from source     │
# │  using zm-build. Supports any Linux OS as target.        │
# └──────────────────────────────────────────────────────────┘

# ── Choose your target distro ──────────────────────────────
#   ubuntu:22.04       (default, most tested)
#   ubuntu:24.04       (newer LTS)
#   rockylinux:9       (RHEL 9 compatible)
#   rockylinux:8       (RHEL 8 compatible)
#   almalinux:9        (RHEL 9 compatible)
#   oraclelinux:9      (RHEL 9 compatible)
# ────────────────────────────────────────────────────────────
ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="Oasis Zimbra FOSS Builder"
LABEL org.opencontainers.image.description="Oasis build container for reproducible Zimbra FOSS packages"
LABEL org.opencontainers.image.source="https://github.com/innotelinc/oasis"

# ── Re-declared ARGs (required after FROM for scope) ───────
ARG ZIMBRA_VERSION=latest
ARG BUILD_USER=build
ARG BUILD_UID=1000
ARG BUILD_GID=1000
ARG TZ=Etc/UTC

# ── Environment ────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=${TZ}
ENV ZIMBRA_VERSION=${ZIMBRA_VERSION}
ENV BUILD_HOME=/home/${BUILD_USER}
ENV BUILD_DIR=${BUILD_HOME}/installer-build
ENV OUTPUT_DIR=${BUILD_HOME}/installer-build/BUILDS

# ── Stage 1: Install base dependencies ─────────────────────
RUN set -ex; \
    if command -v apt-get >/dev/null 2>&1; then \
        apt-get update && \
        apt-get install -y --no-install-recommends \
            ca-certificates curl wget git lsb-release \
            tzdata sudo gnupg2 fakeroot; \
        rm -rf /var/lib/apt/lists/*; \
    elif command -v dnf >/dev/null 2>&1; then \
        dnf -y install --setopt=tsflags=nodocs \
            ca-certificates curl wget git redhat-lsb-core \
            tzdata sudo gnupg2 fakeroot && \
        dnf clean all; \
    elif command -v yum >/dev/null 2>&1; then \
        yum -y install --setopt=tsflags=nodocs \
            ca-certificates curl wget git redhat-lsb-core \
            tzdata sudo gnupg2 fakeroot && \
        yum clean all; \
    fi

# ── Stage 2: Create build user ─────────────────────────────
RUN set -ex; \
    if getent group ${BUILD_GID} >/dev/null 2>&1; then \
        groupmod -n ${BUILD_USER} "$(getent group ${BUILD_GID} | cut -d: -f1)" 2>/dev/null || true; \
    else \
        groupadd -g ${BUILD_GID} ${BUILD_USER}; \
    fi; \
    if id ${BUILD_UID} >/dev/null 2>&1; then \
        usermod -l ${BUILD_USER} -d ${BUILD_HOME} -m "$(id -un ${BUILD_UID})" 2>/dev/null || true; \
    else \
        useradd -u ${BUILD_UID} -g ${BUILD_GID} -m -d ${BUILD_HOME} -s /bin/bash ${BUILD_USER}; \
    fi; \
    echo "${BUILD_USER} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# ── Stage 3: Install Zimbra build dependencies ─────────────
# Based on official zm-build requirements + community best practices
RUN set -ex; \
    if command -v apt-get >/dev/null 2>&1; then \
        apt-get update && \
        apt-get install -y --no-install-recommends \
            gcc g++ make build-essential \
            openjdk-8-jdk ant ant-optional \
            ruby ruby-dev maven \
            perl libxml2-dev libperl-dev \
            pkg-config libtool autoconf automake \
            openssl libssl-dev \
            rsyslog debhelper devscripts fakeroot \
            python3 python3-dev python3-pip python3-venv \
            libaugeas0 libaugeas-dev \
            net-tools iproute2 dnsutils \
            rsync \
            git-lfs \
            ; \
        rm -rf /var/lib/apt/lists/*; \
    elif command -v dnf >/dev/null 2>&1; then \
        dnf -y install --setopt=tsflags=nodocs \
            gcc gcc-c++ make \
            java-1.8.0-openjdk-devel ant \
            ruby ruby-devel maven \
            perl perl-devel libxml2-devel \
            pkgconfig libtool autoconf automake \
            openssl openssl-devel \
            rsyslog rpm-build fakeroot \
            python3 python3-devel python3-pip \
            augeas augeas-devel \
            net-tools iproute bind-utils \
            rsync \
            git-lfs \
            ; \
        dnf clean all; \
    elif command -v yum >/dev/null 2>&1; then \
        yum -y install --setopt=tsflags=nodocs \
            gcc gcc-c++ make \
            java-1.8.0-openjdk-devel ant \
            ruby ruby-devel maven \
            perl perl-devel libxml2-devel \
            pkgconfig libtool autoconf automake \
            openssl openssl-devel \
            rsyslog rpm-build fakeroot \
            python3 python3-devel python3-pip \
            augeas augeas-devel \
            net-tools iproute bind-utils \
            rsync \
            git-lfs \
            ; \
        yum clean all; \
    fi

# ── Stage 4: Ensure scripts directory & copy ──────────────
RUN mkdir -p ${BUILD_HOME}/scripts
COPY --chown=${BUILD_USER}:${BUILD_USER} scripts/ ${BUILD_HOME}/scripts/

# Pre-create build & output dirs owned by build user so that
# Docker volume mounts don't create them as root-owned.
RUN mkdir -p ${BUILD_DIR} ${OUTPUT_DIR} \
    && chown -R ${BUILD_USER}:${BUILD_USER} ${BUILD_DIR} ${OUTPUT_DIR}

# ── Stage 5: Switch to build user ──────────────────────────
USER ${BUILD_USER}
WORKDIR ${BUILD_HOME}

# ── Stage 6: Make entrypoint executable & set it ───────────
RUN chmod +x ${BUILD_HOME}/scripts/entrypoint.sh
# ENTRYPOINT uses shell form (not JSON) so path is fixed to default user
ENTRYPOINT ["/home/build/scripts/entrypoint.sh"]
CMD ["--build"]
