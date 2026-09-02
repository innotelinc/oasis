# Oasis backup, restore, and health operations

The initial Oasis stack stores state in PostgreSQL, Redis, and Authentik media /
templates. Backups must be copied to storage outside the host and protected
with encryption and restricted access.

## Health checks

Compose health checks cover PostgreSQL, Redis, and Authentik readiness:

```bash
docker compose ps
docker inspect --format '{{.Name}} {{.State.Health.Status}}' \
  oasis-postgres oasis-redis oasis-authentik-server
```

A service being healthy means it is ready to accept requests, not that the
whole Oasis platform is operational. Also test DNS, TLS, Authentik login, and
an application login after maintenance.

## Create a backup

Run from the repository root while the stack is running:

```bash
set -a; . ./.env; set +a
BACKUP_DIR=/var/backups/oasis \
  POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  REDIS_PASSWORD="$REDIS_PASSWORD" \
  scripts/backup-oasis.sh
```

The script creates a mode-600 compressed archive containing:

- PostgreSQL custom-format dump.
- Redis RDB snapshot.
- Authentik `/media` and `/templates` data.
- A manifest and SHA-256 checksums.

Backups should be encrypted before leaving the host, uploaded to independent
storage, and periodically tested on a separate recovery host. Do not place
backup archives under a publicly served directory.

## Scheduling

Use a systemd timer, cron, or an existing enterprise backup system. A typical
schedule is daily backups with 14–30 days of retention, plus monthly archives.
Set `RETENTION_DAYS` to match policy. Monitor exit status and backup age.

## Restore

A restore is disruptive and replaces the current Authentik database and data.
First stop application services that use Authentik, preserve the current state,
and verify the archive. Then run:

```bash
set -a; . ./.env; set +a
CONFIRM_RESTORE=yes \
  POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  REDIS_PASSWORD="$REDIS_PASSWORD" \
  scripts/restore-oasis.sh /var/backups/oasis/oasis-YYYYmmddTHHMMSSZ.tar.gz
```

The script validates checksums when available, restores PostgreSQL, replaces
Redis's RDB file, restores Authentik media/templates, and starts Authentik.
Afterward verify:

1. `docker compose ps` shows all required services healthy.
2. Authentik administrator login and MFA work.
3. OIDC discovery and at least one Oasis application login work.
4. Tenant/group mappings and policy behavior are correct.
5. Reverse-proxy TLS and DNS are healthy.

## Recovery objectives

Define service-specific RPO/RTO with stakeholders. Redis is primarily a queue
and cache for Authentik, while PostgreSQL is authoritative for configuration and
identity data. If Redis restoration is unavailable, a clean Redis instance may
be preferable; Authentik should be restarted and verified after rebuilding its
queue state.

The scripts do not back up TLS private keys, `.env` files, external DNS records,
OAuth client secrets stored elsewhere, or host-level configuration. Back those
up through the organization's secret manager and infrastructure backup policy.
