# Oasis systemd lifecycle

The systemd units are intended for a single-node Linux deployment where Docker
Compose runs from `/opt/oasis`.

## Install

Prepare the repository and environment first:

```bash
cp .env.example .env
# Set real secrets and deployment values.
sudo OASIS_INSTALL_ROOT=/opt/oasis scripts/install-oasis.sh
```

The installer copies the repository while preserving `.env`, installs:

- `oasis.service`
- `oasis-backup.service`
- `oasis-backup.timer`

The default profile is `local`. Use `OASIS_MODE=production` during installation
for the production HTTPS profile.

```bash
sudo OASIS_MODE=production OASIS_INSTALL_ROOT=/opt/oasis \
  scripts/install-oasis.sh
```

## Service management

```bash
sudo systemctl status oasis.service
sudo systemctl status oasis-backup.timer
sudo systemctl list-timers oasis-backup.timer
sudo journalctl -u oasis.service -f
```

Run an immediate backup:

```bash
sudo systemctl start oasis-backup.service
sudo journalctl -u oasis-backup.service --no-pager
```

Backups are scheduled daily at 03:15 UTC with a randomized delay of up to 30
minutes. Configure off-host encryption and upload separately; the unit does not
send credentials or archives to a third-party service.

## Upgrade

The upgrade script requires explicit confirmation and performs a backup before
pulling images and recreating services:

```bash
sudo CONFIRM_UPGRADE=yes OASIS_MODE=production \
  scripts/upgrade-oasis.sh
scripts/smoke-oasis.sh production
```

Pin image versions and review release notes before upgrading production.

## Uninstall

The default uninstall removes systemd units and stops containers but preserves
`/opt/oasis`, `.env`, backups, and named Docker volumes:

```bash
sudo CONFIRM_UNINSTALL=yes scripts/uninstall-oasis.sh
```

To permanently remove Compose volumes and the installation directory, use the
separate explicit purge flag only after verifying an external backup:

```bash
sudo CONFIRM_UNINSTALL=yes PURGE_DATA=yes scripts/uninstall-oasis.sh
```
