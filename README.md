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

# 3. Build the installer — then automatically installs on this server
#    when run as root (on a dev machine, add --skip-install)
./build.sh

# 4. Or deploy to a remote mail server (builds dir → server → auto-install)
./build.sh deploy root@mail.example.com
```

---

## Commands

| Command | Description |
|---------|-------------|
| `./build.sh` | Build latest Zimbra, then install on this server (as root) |
| `./build.sh install [zcs-*.tgz]` | Install on this server (standalone) |
| `./build.sh build --skip-install` | Build only (e.g. on a dev machine) |
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

# Force a rebuild even if the installer .tgz already exists
./build.sh build --rebuild

# All options combined
./build.sh build --base-image ubuntu:24.04 --version 10.1.16 --output ./my-builds
```

### Idempotent / incremental builds

`build.sh` and the container entrypoint both skip the long zm-build run when an
installer for the same **Zimbra version + target OS** already exists in
`./builds/`:

```bash
./build.sh            # re-run: skips the 2-6 hour build, then installs
```

- The match includes the target OS (`UBUNTU22_64`, `RHEL9_64`, …), so switching
  `--base-image` (e.g. `ubuntu:22.04` → `rockylinux:9`) triggers a fresh build
  even for the same version.
- Force a rebuild with `--rebuild` or `FORCE_REBUILD=true`:

```bash
./build.sh build --rebuild
FORCE_REBUILD=true docker compose up --build
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

**Progress:** deploy runs in 4 phases with live output — `[1/4]` SSH check,
`[2/4]` installer upload, `[3/4]` build-script upload (build.sh contains the
installer), `[4/4]` remote install.
Transfers show a progress bar (`pv` if installed, otherwise scp's native meter —
`apt install pv` to get the bar), and the long install phase streams the
server's output in real time with an animated spinner + elapsed-time line.

> **Note:** edit `scripts/install-config.env` (hostname, domain, public IP, admin
> passwords, and the relay settings from the port-25 section below) **before**
> deploying — the installer reads it during setup.

### How deploy authentication works

`deploy` uses your normal SSH setup — it doesn't manage passwords itself. It
connects as the user you give it (`root@host`), so make sure **your SSH public
key** is installed on the server:

```bash
ssh-copy-id root@mail.example.com   # one-time; then deploys run passwordless
```

Control the connection via `.env` (or environment):

```bash
DEPLOY_SSH_KEY="~/.ssh/id_ed25519"      # specific key instead of the agent/default
DEPLOY_SSH_PORT=2222                     # non-standard SSH port
DEPLOY_SSH_OPTS="-o ConnectTimeout=15"  # extra ssh options
```

Details:
- **SSH key / agent** → no password is ever asked (this is the "deploy without
  a password" case).
- **No key installed** → `scp`/`ssh` prompt for the password interactively.
- **`root@host`** → the remote install already runs as root, so no sudo prompt.
- **`user@host`** → you'll be prompted for the remote sudo password once (the
  install phase runs `sudo bash build.sh install`).
- If the key is rejected you'll get a connection error telling you to run
  `ssh-copy-id` or set `DEPLOY_SSH_KEY`/`DEPLOY_SSH_PORT`/`DEPLOY_SSH_OPTS`.

### Option B: Manual Install

```bash
# 1. Copy files to server
scp ./builds/zcs-*.tgz build.sh scripts/install-config.env root@mail.example.com:/usr/src/

# 2. Edit config on the server
ssh root@mail.example.com
cd /usr/src
nano install-config.env   # Set passwords, domain, relay, etc.

# 3. Run the installer (build.sh contains the installer)
sudo bash build.sh install --config install-config.env zcs-*.tgz

# Or skip certain sections:
sudo bash build.sh install --skip-ssl --skip-theme --config install-config.env zcs-*.tgz
```

---

## What the Installer Does

The installer (built into `build.sh` as the `install` command) covers the complete server setup:

| Section | What it does |
|---------|-------------|
| Dependencies | Installs GCC, Java 8, Ant, Maven, Perl, etc. |
| Postfix | Stops and removes existing Postfix |
| Webmin | Installs Webmin for server administration |
| Hostname | Sets hostname and /etc/hosts |
| IPv6/Firewall | Disables IPv6 and firewalls |
| Zimbra Install | Extracts and runs Zimbra installer |
| Post-config | HTTPS redirect, LMTP, Amavis, SpamAssassin |
| SSL | Let's Encrypt certificate + deployment |
| Relay | Smarthost outbound relay (bypasses ISP port-25 block) |
| Theme | Zextras theme (optional) |
| Email Relay | Emailrelay local submission proxy (optional) |
| Swap | Creates swap file |

Re-runs are idempotent: dependency installation and Let's Encrypt issuance are
skipped when already done (marker file / existing certificate), and the Zimbra
install is skipped if `/opt/zimbra/.install_done` exists.

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
SKIP_DEPS=false
DRY_RUN=false

# Idempotency — re-runs skip steps already completed
DEPS_MARKER=/var/lib/zimbra-installer/.deps_done   # skip dep install once this exists
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
FORCE_REBUILD=true docker compose up --build   # skip the already-built check
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
├── build.sh                  # One-command build + install + deploy script
├── Dockerfile                # Multi-distro builder image
├── docker-compose.yml        # Docker Compose for easy builds
├── .env.example              # Environment config template
└── scripts/
    ├── entrypoint.sh         # Container entrypoint (detect OS, run zm-build)
    ├── setup-vps-relay.sh    # emailrelay outbound relay for your VPS (port 25)
    ├── verify.sh             # Post-install verification
    └── install-config.env    # Install configuration template (gitignored)
```

---

## Troubleshooting

### Build fails with `DNS resolution failed — cannot resolve github.com`

The builder's pre-flight DNS check (`getent hosts github.com`) fails when the
container can't resolve DNS — even though the image itself built fine. The
usual culprit on Ubuntu hosts is systemd-resolved: Docker copies the host's
`/etc/resolv.conf` into the container, and that file points at the
systemd-resolved stub (`nameserver 127.0.0.53`), which only listens on the
*host's* loopback — so every lookup from inside the container fails.

Confirm it on the host:

```bash
cat /etc/resolv.conf                        # ← look for 127.0.0.53
cat /etc/docker/daemon.json                 # check for an existing dns override
docker run --rm alpine nslookup github.com
```

**Built-in fallback** — `build.sh` now passes `--dns 8.8.8.8 --dns 1.1.1.1` to
the build container by default (and `docker-compose.yml` sets the same), so this
error shouldn't appear on systemd-resolved hosts. To use your own resolvers —
or the Docker daemon's DNS (e.g. corporate/VPN) — set `DOCKER_DNS` in `.env`:

```bash
DOCKER_DNS=""                     # use the Docker daemon's DNS
DOCKER_DNS="10.0.0.1 10.0.0.2"    # your own resolvers
```

**Permanent fix** — configure the Docker daemon to use a reachable resolver for
every container. Edit `/etc/docker/daemon.json`:

```json
{
  "dns": ["8.8.8.8", "1.1.1.1"]
}
```

Then `sudo systemctl restart docker` and re-run the build. If you're behind a
corporate network or VPN, use your internal resolver (e.g. `10.x.x.x`) instead
of the public ones.

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
the long build. The build container runs with `--rm` and only `./builds` (the
installer output) is kept; if an installer for the same version + OS already
exists there, the run is skipped — use `--rebuild` to force a fresh 2–6 hour run.

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
