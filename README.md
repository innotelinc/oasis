# Innotel Mail Platform &bull; [mail.innotel.us](https://mail.innotel.us)

Build the latest Zimbra FOSS (Free and Open Source Software) installer from source using Docker, then deploy and install it on your mail server — all from one tool. Works for **any Linux OS** — Ubuntu, Debian, Rocky Linux, AlmaLinux, Oracle Linux, RHEL.

Built with **Docker** + **Zimbra zm-build** from official Zimbra FOSS source.

---

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/innotelinc/mail-platform.git
cd mail-platform

# 2. (Optional) Configure
cp .env.example .env   # edit with your preferences

# 3. Build the installer
./build.sh

# 4. Deploy to your mail server (builds dir → server → auto-install)
./build.sh deploy root@mail.example.com
```

---

## Commands

| Command | Description |
|---------|-------------|
| `./build.sh` | Build latest Zimbra (interactive version + OS picker) |
| `./build.sh deploy user@host` | Deploy and install on remote server via SSH |
| `./build.sh info` | Show build configuration |
| `./build.sh clean` | Remove all artifacts and images |
| `./build.sh test` | Test the build container |
| `./build.sh list` | List available base OS images |

---

## Build Options

```bash
# Build specific Zimbra version
./build.sh build --version 10.1.16

# Build for Ubuntu 24.04
./build.sh build --base-image ubuntu:24.04

# Build for Rocky Linux 9 (RHEL 9 compatible)
./build.sh build --base-image rockylinux:9

# Disable Docker cache
./build.sh build --no-cache

# All options combined
./build.sh build --base-image ubuntu:24.04 --version 10.1.16 --output ./my-builds
```

---

## Architecture

```
┌─────────────────────────────────────────────┐
│  Docker Container                            │
│  ┌───────────────────────────────────────┐   │
│  │  zm-build (source compilation)        │   │
│  │  ├─ OpenJDK 8                         │   │
│  │  ├─ Ant + Maven                       │   │
│  │  ├─ GCC toolchain                     │   │
│  │  └─ Perl, Ruby, Python                │   │
│  └───────────────┬───────────────────────┘   │
│                  │ output .tgz               │
└──────────────────┼──────────────────────────┘
                   ▼
            ./builds/zcs-*.tgz
                   │
    ┌──────────────┼──────────────┐
    ▼              ▼              ▼
┌────────┐  ┌──────────┐  ┌──────────┐
│ Ubuntu │  │  Rocky   │  │  Alma    │
│ Debian │  │  Linux   │  │  Linux   │
└────────┘  └──────────┘  └──────────┘
      Any Linux mail server target
```

---

## Installing on Your Mail Server

### Option A: Automated (`./build.sh deploy`)

```bash
# Single command — copies installer + script, runs everything
./build.sh deploy root@mail.example.com

# With custom config
./build.sh deploy root@mail.example.com --config my-install.env
```

### Option B: Manual Install

```bash
# 1. Copy files to server
scp ./builds/zcs-*.tgz scripts/install.sh scripts/install-config.env root@mail.example.com:/usr/src/

# 2. Edit config on the server
ssh root@mail.example.com
cd /usr/src
nano install-config.env   # Set passwords, domain, relay, etc.

# 3. Run the installer
sudo bash install.sh --config install-config.env zcs-*.tgz

# Or skip certain sections:
sudo bash install.sh --skip-ssl --skip-theme --config install-config.env zcs-*.tgz
```

---

## What install.sh Does

The install script covers the complete server setup:

| Section | What it does |
|---------|-------------|
| Dependencies | Installs GCC, Java 8, Ant, Maven, Perl, etc. |
| Postfix | Stops and removes existing Postfix |
| Webmin | Installs Webmin for server administration |
| Hostname/DNS | Sets hostname, /etc/hosts, dnsmasq |
| IPv6/Firewall | Disables IPv6 and firewalls |
| Zimbra Install | Extracts and runs Zimbra installer |
| Post-config | HTTPS redirect, LMTP, Amavis, SpamAssassin |
| SSL | Let's Encrypt certificate + deployment |
| Relay | Gmail/ISP outbound relay configuration |
| Theme | Zextras theme (optional) |
| Email Relay | Emailrelay for ISP multi-server setups |
| Swap | Creates swap file |

---

## Configuration Reference

### install-config.env

```bash
# Server Identity
HOSTNAME=mail.example.com
DOMAIN=example.com
PUBLIC_IP=1.2.3.4
TIMEZONE="America/New_York"

# Zimbra Admin (set passwords!)
ZIMBRA_ADMIN_PASSWORD="YourSecurePassword123"
ZIMBRA_LDAP_PASSWORD="YourSecurePassword123"

# Let's Encrypt
LETSENCRYPT_EMAIL=admin@example.com
SSL_DOMAINS=("mail.example.com")

# Outbound Relay (Gmail SMTP)
RELAY_ENABLED=true
RELAY_HOST="[smtp.gmail.com]:587"
RELAY_USER="yourname@gmail.com"
RELAY_PASSWORD="your-app-password"

# Skip sections
SKIP_SSL=false
SKIP_THEME=true
SKIP_RELAY=false
SKIP_DNS=false
SKIP_DEPS=false
DRY_RUN=false
```

---

## Version Resolution

When using `ZIMBRA_VERSION=latest` (default), the script:

1. Queries the [Zimbra FOSS Releases wiki](https://wiki.zimbra.com/wiki/Zimbra_Foss_Source_Code_Only_Releases)
2. Finds the highest version number (excluding beta/RC)
3. Falls back to release branches if no tag exists
4. Defaults to 10.1.16 if nothing is found

---

## Docker Compose

```bash
docker compose up --build
```

Environment variables from `.env` are automatically loaded:

```bash
ZIMBRA_VERSION=10.1.16 BASE_IMAGE=ubuntu:24.04 docker compose up --build
```

---

## Output

After a successful build, the installer lands in `./builds/`:

```
./builds/
└── zcs-10.1.16_GA_XXXXXXX.UBUNTU22_64.YYYYMMDDHHMMSS.tgz
```

---

## Requirements

| Component | Minimum |
|-----------|---------|
| Docker | 20.10+ |
| Git | Any recent |
| Disk space | ~15 GB free |
| RAM | 4+ GB recommended |
| Internet | Required (clone repos, download deps) |

---

## Build Time

| Hardware | Approximate Time |
|----------|-----------------|
| 8 cores, 16 GB RAM | ~2–3 hours |
| 4 cores, 8 GB RAM | ~4–6 hours |
| 2 cores, 4 GB RAM | ~8+ hours |

---

## Available Target OS Images

```bash
./build.sh list
```

| Image | Description |
|-------|-------------|
| `ubuntu:24.04` | Ubuntu 24.04 Noble (newest LTS) |
| `ubuntu:22.04` | Ubuntu 22.04 Jammy (most tested) |
| `ubuntu:20.04` | Ubuntu 20.04 Focal |
| `rockylinux:9` | Rocky Linux 9 (RHEL 9) |
| `rockylinux:8` | Rocky Linux 8 (RHEL 8) |
| `almalinux:9` | AlmaLinux 9 (RHEL 9) |
| `oraclelinux:9` | Oracle Linux 9 (RHEL 9) |

---

## Project Structure

```
.
├── build.sh                  # One-command build + deploy script
├── Dockerfile                # Multi-distro builder image
├── docker-compose.yml        # Docker Compose for easy builds
├── .env.example              # Environment config template
└── scripts/
    ├── entrypoint.sh         # Container entrypoint (detect OS, run zm-build)
    ├── install.sh            # Universal Zimbra installer (Ubuntu + RHEL)
    └── install-config.env    # Install configuration template
```

---

## Troubleshooting

### Build fails with "No such branch"

The version tag might not exist. Check available versions:

```bash
git ls-remote --tags https://github.com/Zimbra/zm-build.git | grep -E '[0-9]+\.[0-9]+\.[0-9]+$'
```

### Out of memory

Add resource limits in `docker-compose.yml` or use `--memory` flag:

```bash
docker run --memory=8g ...
```

### Permission denied on ./builds/

```bash
chmod 777 ./builds
# Or set BUILD_UID/BUILD_GID in .env to match your user
```

### Docker daemon not running

```bash
sudo systemctl start docker
sudo usermod -aG docker $USER
newgrp docker
```

---

Proprietary — Innotel Inc.
