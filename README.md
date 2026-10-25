<div align="center">

# 🌴 Loomix — Mail Platform

**Stalwart's mail engine behind the Loomix Mail client — email, calendars, contacts and files on your own infrastructure.**

Loomix is the Innotel ecosystem's **MailOps** platform. It runs **Stalwart** as the
mail engine (SMTP, IMAP, JMAP, Sieve, calendars, contacts and file storage) behind
**Loomix Mail**, the one browser client for mail, calendar, contacts, files and
admin. Identity is **Authentik**, DNS and certificates are **Cerulean**, edge
routing is **NPM**, and the operator tools — tenant administration, migration,
health and compliance reporting — ship in this repo.

**Loomix is the product name for the platform formerly called Oasis.** The
repository, scripts and `oasis.<domain>` hostnames keep their names for
continuity; everything a customer sees says Loomix.

[![CI](https://github.com/innotelinc/oasis/actions/workflows/ci.yml/badge.svg)](https://github.com/innotelinc/oasis/actions/workflows/ci.yml)
[![Conformity](https://github.com/innotelinc/oasis/actions/workflows/conform.yml/badge.svg)](https://github.com/innotelinc/oasis/actions/workflows/conform.yml)
[![Release](https://github.com/innotelinc/oasis/actions/workflows/release.yml/badge.svg)](https://github.com/innotelinc/oasis/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Theme: Unity](https://img.shields.io/badge/theme-Unity-6366f1)](https://github.com/innotelinc/innotel-platform-stack/blob/main/standards/unity/README.md)

*Mail that is yours — from domain to inbox.*

</div>

---

## Why Loomix

| Problem | Loomix answer |
| --- | --- |
| Mail, calendar, contacts and files in four products | One engine (**Stalwart**) and one client (**Loomix Mail**) behind one login |
| Identity sprawl across mail and admin | **Authentik** OIDC for both; one disabled account closes both doors |
| DNS and certificates as manual chores | **Cerulean** owns the zone and the wildcard certificate; the platform asks, never holds a resolver key |
| Provisioning is a recipe of CLI calls | `oasis-mail-admin.py` creates the domain, its DKIM, its Cerulean zone, the identity and the mailbox — or all of it as one tenant call |
| Mail posture is discovered after a bounce | Daily MX/SPF/DKIM/DMARC/MTA-STS/TLS-RPT checks, dated reports and an exit code a timer can alert on |
| Moving off Exchange/Google/Zimbra is bespoke | `migrate-mailboxes.sh` with imapsync and per-provider presets |

> **About Loomix** — a self-hosted, open-source mail and collaboration platform.
> Stalwart carries the mail; Loomix Mail is the client; Cerulean Authentik,
> Cerulean Vault and Cerulean trust are the platform behind them.
> **Landing page:** [innotelinc.github.io/oasis](https://innotelinc.github.io/oasis)

---

## 🧱 Architecture

```
                    ┌──────────────────────────────┐
  browser ────────► │  Loomix Mail (Stalwart client)      │  mail · calendar · contacts · files · admin
                    │  Next.js, JMAP client        │
                    └──────────────┬───────────────┘
                                   │ JMAP / HTTPS
                    ┌──────────────▼───────────────┐
                    │  Stalwart                    │  SMTP · IMAP · JMAP · Sieve
                    │  one Rust service, embedded  │  calendars · contacts · file storage
                    └──────────────┬───────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
  Authentik                  Cerulean DNS/TLS              NPM Edge
  identity + OIDC            zone · DKIM · certs          app/api/auth/mail/files/admin
```

The mail host runs Stalwart natively and Loomix Mail as its only container. The
operator scripts in `scripts/` are installed as systemd units alongside them.

## 🌐 Live surfaces

| Host | What it serves |
| --- | --- |
| `loomix.art`, `loomix.lol` | Loomix Mail — webmail, calendar, contacts, files (new brand domains) |
| `mail.loomix.art`, `mail.loomix.lol` | JMAP discovery, autoconfig, autodiscover, MTA-STS for the Loomix domains |
| `oasis.<domain>` / `app.<domain>` | Loomix Mail on the legacy estate hostnames |
| `api.<domain>` | the client's API and JMAP session |
| `auth.<domain>` | Authentik SSO |
| `mail.<domain>` | JMAP discovery, autoconfig, autodiscover, MTA-STS |
| `files.<domain>` | Stalwart file storage |
| `admin.<domain>` | domains, users, tenant provisioning, platform health |

`mail.<domain>` and `app.<domain>` are names for the same platform; the apex
(`oasis.<domain>`) is the front door.

## ✨ Highlights

| | | |
|---|---|---|
| 📧 **Stalwart engine** | SMTP, IMAP, JMAP, Sieve, calendars, contacts and file storage — one binary, embedded store, no external database |
| 🖥️ **Loomix Mail** | Mail, calendar, contacts and files behind one login, with admin, themes, plugins and settings |
| 🔐 **Authentik SSO** | OIDC-native sign-in for the client and the admin page; no second password store |
| 🌐 **Edge + wildcard TLS** | `app`, `api`, `auth`, `mail`, `files`, `admin` in NPM, with a wildcard Let's Encrypt certificate |
| 🧑‍💼 **Tenant administration** | Add/remove domains and users; the Stalwart record, Authentik identity, Cerulean zone, DKIM/SPF/DMARC and delegation are written together |
| 🩺 **Health & compliance** | MX/SPF/DKIM/DMARC/MTA-STS/TLS-RPT posture plus host/TLS/SSO checks, with PASS/WARN/FAIL, `--json`, dated Markdown reports and timer-friendly exit codes |
| 📦 **Migration presets** | imapsync from Exchange, Google Workspace and Zimbra, with passfiles and dry-run |
| 🛠️ **Autodiscovery** | Published `autoconfig`/`autodiscover` names, SRV records, MTA-STS and TLS-RPT for every hosted domain |

## 🎨 Brand

The Loomix mark is a palm on an indigo-to-violet tile, with the water line reading as
both an oasis and an underscore. Source assets live in
[`web/landing/brand/`](web/landing/brand):

| File | Use |
| --- | --- |
| `loomix-mark.svg` | app icon, favicon, avatar |
| `loomix-logo.svg` / `loomix-logo-inverse.svg` | wordmark on light / dark |
| `loomix-og.png` | 1200×630 share card |
| `icon-192.png`, `icon-512.png`, `icon-maskable-512.png`, `apple-touch-icon.png`, `favicon.ico` | PWA and browser icons |

The same set is shipped by the client in `webmail-work/public/branding/` as
`Loomix_*`; set `APP_NAME=Loomix Mail` and point `FAVICON_URL`,
`LOGIN_LOGO_LIGHT_URL` and `LOGIN_LOGO_DARK_URL` at those files.

## 🚀 Quick start

```bash
git clone https://github.com/innotelinc/oasis.git
cd oasis
cp .env.example .env        # secrets are generated, or resolved through Cerulean Vault
./scripts/setup-oasis.sh    # identity + edge stack (local or production profile)
```

Then provision mail from the admin page (or its CLI):

```bash
python3 scripts/oasis-mail-admin.py    # OIDC-native admin at admin.<domain>
```

A domain added there gets its Stalwart record, DKIM keys, Cerulean zone and
delegation; a user gets an Authentik identity and the matching Stalwart mailbox.
Removing either takes the whole chain back.

## 🛠️ Operator commands

| Command | What it does |
| --- | --- |
| `scripts/setup-oasis.sh` | Local/production stack lifecycle, edge hostnames and certificate sync |
| `scripts/oasis-mail-admin.py` | OIDC-native admin page and tenant API: domains, users, DKIM, zones, delegation |
| `scripts/oasis-mail-user.py` | CLI for one mailbox; `passwd` rotates the Authentik password |
| `scripts/oasis-mail-health.py` | Grades MX/SPF/DMARC/DKIM/MTA-STS/TLS-RPT for every Stalwart domain; `--json`, exit 2 on FAIL |
| `scripts/oasis-health.sh` | Host/TLS/SSO checks for every surface; `--report` writes dated Markdown |
| `scripts/migrate-mailboxes.sh` | imapsync migration with Exchange/Google/Zimbra presets, passfiles and `--dry-run` |
| `scripts/cerulean/` | Certificate fetch, DKIM/autoconfig publication and zone verification through Cerulean |
| `scripts/npm-proxy-hosts.py` | Reproducible NPM hostnames with a wildcard certificate and drift check |
| `scripts/verify.sh`, `scripts/smoke-oasis.sh` | Post-deploy verification |

## 🩺 Health and compliance

The two checks answer different questions and both run on a timer:

- `oasis-mail-health.py` reads the authoritative zone back through Cerulean and grades
  what the world sees — MX, SPF, DMARC, every published DKIM selector, MTA-STS and
  TLS-RPT. Any FAIL exits 2, so a systemd timer or monitoring wrapper sees it.
- `oasis-health.sh` probes the platform itself — DNS, TLS chain and expiry, HTTPS for
  each surface — and writes a dated Markdown report with `--report`.

The Loomix admin page shows the latest result of both, with the report timestamp, so
the operator does not have to open a shell to see that a certificate is close to
expiry or a DKIM selector is unpublished.

## 📚 Documentation

| Document | Covers |
|---|---|
| [docs/ADMIN.md](docs/ADMIN.md) | The admin page and tenant API: domains, users, zones, delegation, OIDC |
| [docs/COMPLIANCE.md](docs/COMPLIANCE.md) | Health monitoring, compliance reports, exit codes |
| [docs/MIGRATION.md](docs/MIGRATION.md) | Exchange / Google Workspace / Zimbra mailbox migration |
| [docs/BACKUP-RESTORE.md](docs/BACKUP-RESTORE.md) | Backup, restore, retention, DR |
| [docs/SSO.md](docs/SSO.md) | Authentik identity integration |
| [docs/SYSTEMD.md](docs/SYSTEMD.md) | Host lifecycle: systemd units |
| [docs/stack.md](docs/stack.md) | What Loomix owns, provides, consumes and does not own |
| [docs/roadmap.md](docs/roadmap.md) | Direction, engine decision and delivery phases |

## 📦 Releases

Release tags use `vMAJOR.MINOR.PATCH` and are independent of the Stalwart and
Loomix Mail versions they deploy. The release workflow publishes the Compose
bundle, SBOM, checksums and provenance attestations.

## Project status

Loomix v0.2.0 ships the merged platform: Stalwart as the engine, Loomix Mail as
the client, Authentik identity, Cerulean DNS/TLS, tenant administration, edge
hostnames with a wildcard certificate, migration presets, and health/compliance
reporting. The roadmap's next milestone is the v1.0 LTS hardening pass.

The repo still carries the historical Zimbra builder (`build.sh`, `Dockerfile`,
`scripts/install-oasis.sh`) from the v0.1 line. It is **retired**: it is not the
platform path, is not deployed in the estate, and is kept only for reproducible
builds of that older line.

## License

Loomix is released under the [MIT License](LICENSE). Stalwart is AGPL-3.0,
Loomix Mail is AGPL-3.0 (a fork of Bulwark Webmail), imapsync and the other dependencies retain
their own licenses; consult their notices before redistribution.

## 🏛️ Platform stack

Loomix is the ecosystem's **MailOps** platform — email, calendars, contacts and
collaboration in the [**Innotel Platform Stack**](https://github.com/innotelinc/innotel-platform-stack),
the canonical single-responsibility architecture where Cerulean Authentik owns
identity, Cerulean Vault owns secrets, Cerulean owns trust, ONYX owns storage,
Magnate owns revenue, and NPM Edge owns the edge. See [docs/stack.md](docs/stack.md)
for this platform's owns/consumes boundaries and its Cerulean Vault setup.
