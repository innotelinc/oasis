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

# With an explicit installer file (or --tgz path/to/zcs-*.tgz)
./build.sh deploy root@mail.example.com ./builds/zcs-10.1.16_GA_*.UBUNTU24_64.*.tgz
```

> **Note:** edit `scripts/install-config.env` (hostname, domain, public IP, admin
> passwords, and the relay settings from the port-25 section below) **before**
> deploying — the installer reads it during setup.

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
| Relay | Smarthost outbound relay (bypasses ISP port-25 block) |
| Theme | Zextras theme (optional) |
| Email Relay | Emailrelay local submission proxy (optional) |
| Swap | Creates swap file |

---

## Outbound Mail When Your ISP Blocks Port 25

Most residential/office ISPs block **outbound TCP port 25**, which makes direct
delivery to other mail servers impossible. There is no way around this from
your own connection: remote mail servers only accept mail on port 25, so
*something* off your network must do the final delivery.

### How it works here

1. Zimbra's Postfix relays **all** outbound mail to a smarthost on an **open**
   port (`587` submission, `465` SMTPS, or a custom port) with STARTTLS + AUTH.
2. The smarthost (not you) performs the final port-25 delivery to the internet.

Configure it in `scripts/install-config.env`:

```bash
RELAY_ENABLED=true
RELAY_HOST="[smtp.yourisp.net]:587"   # or [smtp.gmail.com]:587, [smtp.zoho.com]:587 ...
RELAY_USER="you@example.com"
RELAY_PASSWORD="your-password"
RELAY_TLS_LEVEL="may"                # may = STARTTLS if offered, encrypt = require TLS
```

The `[...]` brackets force Postfix to use the exact host:port (no MX lookup).

### Fully self-managed option: relay through your own VPS

If you don't want to depend on Google/Zoho, run emailrelay on any cheap VPS
(~$5/mo — VPS providers don't block port 25):

```bash
# 1. On the VPS (as root), install emailrelay as an authenticated relay:
git clone https://github.com/innotelinc/mail-platform.git
cd mail-platform/scripts && sudo bash setup-vps-relay.sh

# 2. It prints RELAY_HOST / RELAY_USER / RELAY_PASSWORD — put them in install-config.env:
RELAY_ENABLED=true
RELAY_HOST="[vps-ip-or-host]:587"
RELAY_USER="mailrelay"
RELAY_PASSWORD="<printed password>"
RELAY_TLS_LEVEL="encrypt"
```

`setup-vps-relay.sh` builds emailrelay from the innotelinc fork, listens on
`587` (override with `--port 2525`) with **STARTTLS required + auth** (the mode
Postfix uses natively), and delivers to the world via DNS MX. It also checks
that the VPS itself can reach outbound port 25 (some providers block it by
default). The emailrelay instance on the **mail server** (if enabled) is a
separate, optional local submission proxy — it does not replace this relay.

> **Delivery reputation:** for best deliverability, set a PTR/reverse-DNS record
> on the VPS IP, an `A` record for the mail host, and proper SPF + DKIM for your
> domain. Gmail/Google Workspace relays also require SPF/DKIM alignment.

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

# Outbound Relay (smarthost — bypasses ISP port-25 block)
RELAY_ENABLED=true
RELAY_HOST="[smtp.gmail.com]:587"   # or your ISP relay / own VPS
RELAY_USER="yourname@gmail.com"
RELAY_PASSWORD="your-app-password"
RELAY_TLS_LEVEL="may"

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

1. Reads the release tags from the official [Zimbra zm-build repository](https://github.com/Zimbra/zm-build) (`git ls-remote`)
2. Keeps only plain `X.Y.Z` FOSS release tags (excludes betas/RCs and `X.Y.Z.pN` patches)
3. Picks the highest version number
4. Defaults to 10.1.16 if the repo can't be reached

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
    ├── setup-vps-relay.sh    # emailrelay outbound relay for your VPS (port 25)
    └── install-config.env    # Install configuration template (gitignored)
```

---

## Troubleshooting

### Build fails with `open3: exec of rsync ... failed: No such file or directory`

The packaging stage of zm-build uses `rsync` to stage the installer files, but
older builder images didn't include it. Pull the latest code and rebuild the
Docker image (the image build is fast — the long part is the zm-build run):

```bash
git pull
./build.sh build --base-image ubuntu:24.04 --version 10.1.16
```

If you still hit it, rebuild the image with `--no-cache` to bypass a stale layer:

```bash
./build.sh build --no-cache
```

> **Tip:** run `./build.sh test` first as a pre-flight — it verifies all required
tools (including `rsync`) are present inside the container before you commit to
the long build. Note that the zm-build run always starts from scratch: the build
container runs with `--rm` and only `./builds` (the installer output) is kept, so
expect the full 2–6 hour run after a fix.

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

### Deploy fails with `mkdir: cannot create directory ... Permission denied` (or `Permission denied on ./builds/`)

The `./builds` output directory is bind-mounted into the container, but it was
created by your host user (e.g. root) while the build runs as the container's
`build` user (UID 1000). Current images auto-fix the mount ownership at
container start, so `git pull` and rebuild the image:

```bash
git pull
./build.sh build
```

Manual fallbacks (either one works):

```bash
chmod 777 ./builds
# Or set BUILD_UID/BUILD_GID in .env to match your user
```

> **Note:** the auto-fix chowns `./builds` to the container's build UID (default
> 1000) — bind mounts share the host directory's ownership. If your host user
> has a different UID, set `BUILD_UID`/`BUILD_GID` in `.env` to match, or use
> `sudo` to clean old artifacts.

### Docker daemon not running

```bash
sudo systemctl start docker
sudo usermod -aG docker $USER
newgrp docker
```

---

Proprietary — Innotel Inc.
