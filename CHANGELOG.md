# Changelog

All notable changes to Oasis are documented here.

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

This is an early deployment foundation. The full Oasis collaboration suite—including production mail, calendar, contacts, files, automation, administration, compliance, and monitoring modules—will be delivered incrementally; mail, calendar, contacts, and files are served by the deployed Zimbra mail server behind the module hostnames.

## [0.1.0] - 2026-09-02

### Fixed

- Prevented Oasis `vMAJOR.MINOR.PATCH` tags from being passed to the Zimbra builder as Zimbra versions.
- Added release-time validation for the configured `RELEASE_ZIMBRA_VERSION` value.

### Added

- Initial Oasis deployment foundation based on Docker Compose.
- PostgreSQL and Redis infrastructure services with health checks and persistent storage.
- Authentik server and worker integration for centralized identity and OIDC SSO.
- Local HTTP and production HTTPS reverse-proxy profiles.
- Certbot ACME HTTP-01 certificate issuance and renewal support.
- Oasis Nginx Proxy Manager automation for app, API, Authentik, mail, files, and admin hostnames.
- Idempotent setup, upgrade, uninstall, and smoke-test scripts.
- PostgreSQL, Redis, and Authentik backup and restore tooling.
- systemd service and daily backup timer units.
- Authentik bootstrap, OIDC provider, SSO, backup, and systemd documentation.
- GitHub Actions CI with ShellCheck, Python, Compose, YAML, and secret scanning.
- Tagged release workflow with GHCR image publishing, SBOM generation, vulnerability scanning, provenance attestations, checksums, and Compose bundle assets.
- Separate Oasis release versioning from the upstream Zimbra builder version through the repository-controlled `RELEASE_ZIMBRA_VERSION=10.1.16` default and optional manual workflow input.

### Security

- Secrets are generated locally and excluded from source control.
- Database and cache services are isolated on an internal Compose network.
- Destructive restore, upgrade, uninstall, and purge operations require explicit confirmation.
- Production proxy configuration requires TLS certificates and ACME validation.

### Status

This is an early deployment foundation. The full Oasis collaboration suite—including production mail, calendar, contacts, files, automation, administration, compliance, and monitoring modules—will be delivered incrementally.
