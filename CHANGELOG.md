# Changelog

All notable changes to Loomix are documented here.

## [Unreleased]

### Changed

- **`signara.innotel.us` retired as a mail domain.** Signara sends as
  `alertmanager@innotel.us` (From `Signara Alerts <alertmanager@innotel.us>`,
  `SMTP_HELLO=innotel.us`), so the separate sending domain earns nothing: its mail
  is now signed, routed and reported with `innotel.us`'s own SPF/DKIM/DMARC and
  reputation. The one account on the domain (`signara-alerts@signara.innotel.us`)
  moved to `signara-alerts@innotel.us` keeping its mailbox and credential; the
  Stalwart domain and its DKIM keys, the Mailjet `*@signara.innotel.us` sender and
  its catch-all parseroute, the receiver's `LOCAL_DOMAINS` and the check's
  `MX_DOMAINS` entries, and the domain's MX/SPF/DMARC/MTA-STS/TLS-RPT/autoconfig
  records are gone. The zone itself stays — it is the Signara platform's web zone,
  so its eight `A` records were left alone. `docs/roadmap.md` and
  `MAILJET-MIGRATION.md` record the procedure, including the guard that now catches
  a half-done retirement and the six inert Stalwart autodiscovery SRVs, which needed
  a Cerulean fix (under Fixed) before they could be removed — it is deployed on the
  i4 edge now and all six are gone, leaving the zone's `A`, `NS` and `SOA`.
- **Loomix Mail rebrand.** The client is now **Loomix Mail** everywhere: the new palm-and-water mark (indigo → violet), wordmark, favicon, PWA icons and 1200×630 share card, shipped in `web/landing/brand/` and in the client at `public/branding/Loomix_*`. The webmail image moves to `1.17.0`, sets `APP_NAME=Loomix Mail`, and every user-facing default (login, tab title, About, setup wizard, manifest, share card) reads Loomix. Stalwart keeps the mail; its hosted-domain logos now carry the Loomix mark too.
- **Loomix is the new product name for the platform formerly called Oasis.** The
  repository, operator scripts, systemd units and existing `oasis.<domain>`
  hostnames keep their names for continuity; the product, client and marketing
  surfaces read Loomix. `loomix.art` and `loomix.lol` are added as mail domains
  with Cerulean zones, NPM hosts and Stalwart mailboxes.

### Added

- **Retired-mail-domain guard in `mailjet-inbound-check.py`** — `RETIRED_DOMAINS`
  names what has left inbound mail, and the check fails while a listed domain still
  publishes `MX` or is still named in `MX_DOMAINS` or the receiver's
  `LOCAL_DOMAINS`. Each half of a half-done retirement looks healthy on its own,
  which is the point: a leftover MX keeps delivering into a mailbox nobody reads,
  and a leftover list entry revives the domain the day its MX comes back.
- **Twelve new mail domains**, each a Cerulean zone and a Stalwart domain:
  `telno.services`, `telno.mobi`, `telno.tel`, `telno.online`, `innotel.tech`,
  `denovo.credit`, `telno.digital`, `telno.directory`, `num.directory`,
  `number.directory`, `orion.locker`, `telno.network`. Every zone carries the
  house record set (apex `A`, in-zone `NS ns1`/`ns2` with their own `A`, `MX 10
  mail.innotel.us`, SPF with the relay include, DMARC, CAA) plus the records
  Stalwart renders (ed25519 and RSA DKIM, SRV autodiscovery, MTA-STS, TLS-RPT,
  client autoconfiguration), published through Cerulean. `MX`, `autoconfig` and
  `autodiscover` name `mail.innotel.us` rather than a per-zone `mail.<zone>`,
  because that is the name the mail host's certificate covers; the apexes still
  need their **registrar** nameservers moved off the registrar's parking service
  (`ns1.<zone>` + `ns2.<zone>`, or the estate's `ns1.innotel.us` +
  `ns2.innotel.us`), which no API here can do.
- **Mail administration page** (`scripts/oasis-mail-admin.py`, `systemd/oasis-mail-admin.service`) — add and remove mail domains and users from one surface. A user is created in Authentik (with the right groups and a password) and as the matching Stalwart mailbox in the same request; a domain is created in Stalwart with automatic DKIM, and its generated DKIM/SPF/DMARC records are published to the estate resolver through Cerulean (Stalwart's own publisher is off). Adding a domain also mints its authoritative DNS zone **through Cerulean** (the estate's TrustOps platform and the only Technitium writer) before publishing, with a standalone **Create zone** action for domains added earlier; `scripts/cerulean/cerulean.py` gained `ensure_zone()` for the bridge's `POST /service/domains`. Publishing is still guarded: nothing is written into a zone the resolver does not serve, so the service never mints a private zone DNS would not delegate to. It is OIDC-native against Cerulean Authentik using the existing `oasis-admin` client, gated to a configurable Authentik group, with a CSRF token on every write. Documented in `docs/ADMIN.md`; configuration template in `scripts/oasis-mail-admin.env.example`.
- `scripts/oasis-mail-admin.py` — **domain delegation and teardown**. Adding a domain delegates it: a name inside a domain the estate already serves has its NS records written into the parent zone through Cerulean (and removed again on delete), so no manual step is needed for the common case; a brand-new apex gets the registrar step reported with the exact nameservers (`PUBLIC_NS`), since no registrar API exists in the estate. Removing a domain now unregisters its zone through Cerulean (`DELETE /api/service/domains`, added to the bridge) so the zone, its records and its delegation do not outlive the domain. Cerulean refuses the estate's own platform zones; `PROTECTED_ZONES` (default `innotel.us`) refuses them here too.
- `scripts/oasis-mail-user.py passwd` — rotate a mailbox's Authentik password and write it to a `600` credential file, backing up the previous one.
- **Platform health in the admin page** — `GET /api/posture` returns the newest dated report (`oasis-health.sh --report`) and the mail posture (`oasis-mail-health.py`), and the page's first section renders both with PASS/WARN/FAIL counts, the generation time and a `stale` flag when neither artifact is newer than 36 hours (`OASIS_HEALTH_STALE_SECONDS`). The page reads the timer's artifacts rather than probing inside a request, so an operator page cannot stall on a DNS/TLS sweep. Paths are configurable with `OASIS_REPORT_DIR` and `OASIS_POSTURE_FILE`; documented in `docs/ADMIN.md`.
- `README.md` and the landing page describe the shipped platform — Stalwart engine, Loomix Mail client, Authentik identity, Cerulean DNS/TLS, tenant administration and the live `app`/`api`/`auth`/`mail`/`files`/`admin` surfaces — instead of the retired Zimbra builder.

### Fixed

- **Cerulean can delete `SRV`, `CAA` and `PTR` records**
  (`server/src/services/technitium.ts`): `recordValueForDelete()` had no SRV branch
  and sent `text` for CAA/PTR, where Technitium validates
  `priority`/`weight`/`port`/`target`, `flags`/`tag`/`value` and `ptrName` — so
  those deletes failed (`Parameter 'priority' missing`) and the records stayed. Each
  now sends its own RDATA (priority `0` included, which the query builder must not
  drop as empty) and refuses a value that does not carry it instead of guessing one;
  pinned in `server/tests/technitium.test.ts`. Deployed on the i4 edge on
  2026-10-27 (image built from the edge's own tree, `cerulean-cerulean:pre-srv-fix`
  kept as the rollback tag), which is what let the six leftover
  `signara.innotel.us` SRVs be removed.
- `scripts/oasis-health.sh --quiet` aborted at the first PASS (exit 1, no summary) because `[ "$QUIET" = false ] && printf …` is the function's return value under `set -e`. The print is now guarded by an `if`, so a quiet run completes with the correct exit code.

- `scripts/oasis-mail-user.py` resolved Authentik groups to a list of **names**, but Authentik's write API requires group **PKs**; `add` would fail to set groups. It now resolves names to PKs through one helper, and reports a clear error for a group that does not exist.

## [0.2.0] - 2026-09-02

### Added

- Nginx Proxy Manager integration in `scripts/setup-oasis.sh`: production setup now synchronizes the `app`, `api`, `auth`, `mail`, `files`, and `admin` hostnames automatically when NPM credentials are configured.
- Automatic wildcard Let's Encrypt certificate provisioning in `scripts/npm-proxy-hosts.py`: DNS-01 wildcard (`*.<domain>`) when `NPM_DNS_PROVIDER`/`NPM_DNS_CREDENTIALS` are set, otherwise one HTTP-01 SAN certificate covering the hostnames; certificates are matched by domain set and never reissued unnecessarily.
- Domain health monitoring and compliance reporting via `scripts/oasis-health.sh` with MX/SPF/DKIM/DMARC checks, TLS certificate validation, port reachability, `--json` summaries, exit codes for cron, and dated Markdown reports (`--report`). Documented in `docs/COMPLIANCE.md`.
- Mailbox migration utilities for Exchange, Google Workspace, and Zimbra via `scripts/migrate-mailboxes.sh` (imapsync with Docker fallback, provider presets, passfile-based secrets, dry-run and delete-sync options). Documented in `docs/MIGRATION.md`.
- MIT license.

### Changed

- Production setup next-steps now point at the health/compliance checker.
- Removed upstream reference-implementation attribution; operational patterns remain but are no longer credited to another project.

### Status

This is an early deployment foundation. The full Loomix collaboration suite—including production mail, calendar, contacts, files, automation, administration, compliance, and monitoring modules—will be delivered incrementally; mail, calendar, contacts, and files are served by the deployed Zimbra mail server behind the module hostnames.

## [0.1.0] - 2026-09-02

### Fixed

- Prevented Loomix `vMAJOR.MINOR.PATCH` tags from being passed to the Zimbra builder as Zimbra versions.
- Added release-time validation for the configured `RELEASE_ZIMBRA_VERSION` value.

### Added

- Initial Loomix deployment foundation based on Docker Compose.
- PostgreSQL and Redis infrastructure services with health checks and persistent storage.
- Authentik server and worker integration for centralized identity and OIDC SSO.
- Local HTTP and production HTTPS reverse-proxy profiles.
- Certbot ACME HTTP-01 certificate issuance and renewal support.
- Loomix Nginx Proxy Manager automation for app, API, Authentik, mail, files, and admin hostnames.
- Idempotent setup, upgrade, uninstall, and smoke-test scripts.
- PostgreSQL, Redis, and Authentik backup and restore tooling.
- systemd service and daily backup timer units.
- Authentik bootstrap, OIDC provider, SSO, backup, and systemd documentation.
- GitHub Actions CI with ShellCheck, Python, Compose, YAML, and secret scanning.
- Tagged release workflow with GHCR image publishing, SBOM generation, vulnerability scanning, provenance attestations, checksums, and Compose bundle assets.
- Separate Loomix release versioning from the upstream Zimbra builder version through the repository-controlled `RELEASE_ZIMBRA_VERSION=10.1.16` default and optional manual workflow input.

### Security

- Secrets are generated locally and excluded from source control.
- Database and cache services are isolated on an internal Compose network.
- Destructive restore, upgrade, uninstall, and purge operations require explicit confirmation.
- Production proxy configuration requires TLS certificates and ACME validation.

### Status

This is an early deployment foundation. The full Loomix collaboration suite—including production mail, calendar, contacts, files, automation, administration, compliance, and monitoring modules—will be delivered incrementally.
