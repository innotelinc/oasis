<div align="center">

# 🌴 Oasis — Open Enterprise Communication Platform

**Build and deploy Zimbra FOSS from source — one command, any Linux mail server.**

Oasis is an open-source deployment foundation for enterprise email and collaboration. This
repo is the first slice of the platform: a **Dockerized Zimbra FOSS build** (from official
source via `zm-build`) plus an idempotent installer that turns a bare server into a working
mail host — Let's Encrypt SSL, smarthost relay for ISP port-25 blocks, and verification
built in. Email transport, identity, calendar, contacts, files, automation, administration,
and observability land incrementally on top.

[![CI](https://github.com/innotelinc/mail-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/innotelinc/mail-platform/actions/workflows/ci.yml)
[![Release](https://github.com/innotelinc/mail-platform/actions/workflows/release.yml/badge.svg)](https://github.com/innotelinc/mail-platform/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

*Mail that is yours — from source to inbox.*

</div>

> **About Oasis** — a self-hosted, open-source enterprise communication platform. Build a
> current Zimbra FOSS release from source inside Docker, then deploy it to any Linux server
> with one command — SSL, outbound relay, and post-install verification included. The
> broader Oasis platform (identity, collaboration, observability) builds on this foundation.
> **Landing page:** [github.com/innotelinc/mail-platform](https://github.com/innotelinc/mail-platform)

---

## ✨ Highlights

| | | |
|---|---|---|
| 🐳 **Source-built Zimbra** | Official `zm-build` inside Docker — reproducible `zcs-*.tgz` installers for Ubuntu, Debian, Rocky, Alma, and Oracle Linux |
| 🚀 **One-command deploy** | `./build.sh deploy user@host` — SSH check → upload → install, with live progress and streaming output |
| ⚙️ **Full installer** | Dependencies, Postfix swap, hostname, Zimbra install, HTTPS redirect, Let's Encrypt SSL, relay, swap — all idempotent |
| 📮 **ISP-relay aware** | Smarthost outbound relay (587/465) bypasses port-25 blocks; or stand up your own `emailrelay` VPS with one script |
| ✅ **Pre-flight checks** | `./build.sh check` validates config, DNS resolution, and port-80 reachability before the long install |
| 🔁 **Idempotent builds** | Re-runs skip the 2–6 hour zm-build when an installer for the same version + OS already exists |
| 🩺 **Health & compliance** | Domain mail posture (MX/SPF/DKIM/DMARC/TLS/ports) with PASS/WARN/FAIL reports |
| 🔐 **Authentik identity** | OIDC SSO foundation for the platform services (`auth.<domain>`) |

## 🚀 Quick start

```bash
# 1. Clone the repo
git clone https://github.com/innotelinc/mail-platform
cd mail-platform

# 2. (Optional) Configure
cp .env.example .env   # edit with your preferences

# 3. Build the installer — then automatically installs on this server
#    when run as root (on a dev machine, add --skip-install)
./build.sh

# 4. Or deploy to a remote mail server (builds dir → server → auto-install)
./build.sh deploy root@mail.example.com
```

Build a specific version or target OS:

```bash
./build.sh build --version 10.1.16                 # specific Zimbra version
./build.sh build --base-image ubuntu:24.04         # Ubuntu 24.04
./build.sh build --base-image rockylinux:9         # Rocky Linux 9 (RHEL-compatible)
./build.sh build --no-cache                        # disable Docker cache
./build.sh build --rebuild                         # force rebuild even if .tgz exists
```

Before installing on a real server, edit `scripts/install-config.env` (hostname, domain,
public IP, admin passwords, relay settings) and run the diagnostics:

```bash
./build.sh check      # config, DNS, port 80, installer — exits non-zero on FAIL
```

## Commands

| Command | Description |
|---------|-------------|
| `./build.sh` | Build latest Zimbra, then install on this server (as root) |
| `./build.sh install [zcs-*.tgz]` | Install on this server (standalone) |
| `./build.sh check [--config FILE]` | Pre-install diagnostics: config, DNS, port 80, installer |
| `./build.sh build --skip-install` | Build only (e.g. on a dev machine) |
| `./build.sh deploy user@host` | Deploy and install on remote server via SSH |
| `./build.sh info` | Show build configuration |
| `./build.sh clean` | Remove all artifacts and images |
| `./build.sh test` | Test the build container |
| `./build.sh list` | List available base OS images |

## 🧱 Architecture

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

After a successful build, the installer lands in `./builds/` as
`zcs-<version>_GA_<hash>.<OS>_64.<timestamp>.tgz`.

## Requirements

| Component | Minimum |
|-----------|---------|
| Docker | 20.10+ |
| Disk space | ~15 GB free |
| RAM | 4+ GB recommended (8+ cores / 16 GB → ~2–3 h builds) |

Supported base images: `ubuntu:24.04`, `ubuntu:22.04`, `ubuntu:20.04`, `rockylinux:9`,
`rockylinux:8`, `almalinux:9`, `oraclelinux:9`.

## 📚 Documentation

| Document | Covers |
|---|---|
| [docs/build-and-deploy.md](docs/build-and-deploy.md) | Deploy (SSH auth, manual install), installer sections, port-25 smarthost relay, full `install-config.env` reference, troubleshooting |
| [docs/SYSTEMD.md](docs/SYSTEMD.md) | Host lifecycle: systemd installer + units |
| [docs/COMPLIANCE.md](docs/COMPLIANCE.md) | Health monitoring + compliance reports |
| [docs/MIGRATION.md](docs/MIGRATION.md) | Exchange / Google / Zimbra mailbox migration |
| [docs/BACKUP-RESTORE.md](docs/BACKUP-RESTORE.md) | Backup, restore, retention, DR |
| [docs/SSO.md](docs/SSO.md) | Authentik identity integration |

## Platform services

- **Edge hostnames** — `scripts/npm-proxy-hosts.py` provisions `app`, `api`, `auth`,
  `mail`, `files`, and `admin` hosts in Nginx Proxy Manager, with a wildcard
  `*.<domain>` Let's Encrypt cert via DNS-01 by default (`--check` for drift).
- **Health monitoring** — `scripts/oasis-health.sh` checks mail posture with exit codes
  for cron, `--json`, and dated Markdown compliance reports.
- **Migration** — `scripts/migrate-mailboxes.sh` (imapsync-based) moves Exchange, Google
  Workspace, and Zimbra mailboxes with per-provider presets.
- **Lifecycle** — `scripts/setup-oasis.sh` (local/production), `backup-oasis.sh`,
  `restore-oasis.sh`, `upgrade-oasis.sh`, `uninstall-oasis.sh`, `smoke-oasis.sh`.

## 📦 Releases

Release tags use `vMAJOR.MINOR.PATCH` (independent of Zimbra versions) and build with the
repo-controlled default `RELEASE_ZIMBRA_VERSION=10.1.16` — no GitHub repository variable
required. Pick a different Zimbra `X.Y.Z` from the Actions UI via `workflow_dispatch`.

## Project status

Under active development. The current release provides reproducible Zimbra FOSS builds,
mail-server deployment, the Authentik-backed identity stack, NPM edge provisioning with
wildcard certificates, domain health monitoring and compliance reporting, mailbox
migration tooling, backup/restore, and systemd lifecycle management. Multi-tenancy,
automation, and administration modules are planned and delivered incrementally.

## Licensing

Oasis is released under the [MIT License](LICENSE). Upstream Zimbra, imapsync, emailrelay,
Docker base images, and other dependencies retain their own licenses; consult their notices
before redistribution.

---

*Oasis — enterprise mail, self-hosted from source.*

## 🏛️ Platform stack

Oasis is the ecosystem's **MailOps** platform — email, calendars, contacts, and collaboration in the
[**Innotel Platform Stack**](https://github.com/innotelinc/innotel-platform-stack) — the
canonical single-responsibility architecture where Authentik owns identity, Infisical owns
secrets, Cerulean owns trust, ONYX owns storage, Magnate owns revenue, and every other
platform is a business function that consumes them. See
[docs/stack.md](docs/stack.md) for this platform's owns/consumes boundaries and its
Infisical secret setup.
