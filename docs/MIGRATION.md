# Migrating to Oasis

Oasis replaces Zimbra-class deployments, so existing mailboxes most often come
from Microsoft Exchange (including Microsoft 365), Google Workspace, or another
Zimbra server. This guide covers moving mailboxes (IMAP) and related data
(calendar, contacts) onto the Oasis mail server with minimal downtime.

## Overview

| Data | Recommended path |
|---|---|
| Mail (Inbox, folders, sent, drafts) | IMAP sync with `scripts/migrate-mailboxes.sh` (imapsync) |
| Calendar | Exchange/Google export (`.ics`), Google Takeout, Zimbra export, then import into the target webmail calendar |
| Contacts | Exchange/Google export (`.vcf` / `.csv`), Google Takeout, Zimbra export, then import |
| Files | FTP/WebDAV/Drive-style exports, or Zimbra Briefcase export |
| Aliases, distribution lists, forwarding | Recreate on Oasis via the Zimbra admin or `zmprov` |

The mailbox utility below handles the email migration. Calendar, contacts, and
files are exported from each provider and imported into the target mailbox;
the exact import steps depend on the final webmail client and are described in
the provider sections.

## General cutover flow

1. **Provision target mailboxes** on the Oasis mail server before migrating so
   IMAP login succeeds (`zmprov ca user@domain password`, or via the admin UI).
2. **Enable IMAP** on the source system (see provider notes below).
3. **Run the migration** per mailbox, or script it over a user list. First run
   with `--dry-run`, then a real run, then a final incremental run right before
   cutover.
4. **Verify** folder trees and mail counts on the target (IMAP client or
   webmail).
5. **Switch MX** records for the domain to the Oasis server once sync is
   complete. Migrate calendars/contacts at the same time.
6. **Keep the old server in read-only mode** for 30+ days as a fallback, then
   decommission.

## Mailbox migration utility

`scripts/migrate-mailboxes.sh` wraps imapsync and works with any IMAP4
provider. It uses the local `imapsync` binary when available and falls back to
the official imapsync Docker image. Passwords never appear on the command line
(passfiles or environment variables only).

```bash
# Generate a dry run first
scripts/migrate-mailboxes.sh \
  --source google --source-user sam@example.com \
  --target-host mail.example.com --target-user sam@example.com \
  --source-passfile ./sam-source.pass --target-passfile ./sam-target.pass \
  --dry-run

# Real migration
scripts/migrate-mailboxes.sh \
  --source google --source-user sam@example.com \
  --target-host mail.example.com --target-user sam@example.com \
  --source-passfile ./sam-source.pass --target-passfile ./sam-target.pass
```

Options include `--exclude`/`--include` folder filters (default excludes
Trash/Spam/Junk/Deleted), `--source-tls`/`--target-tls`
(`ssl|starttls|none`), `--delete` for two-way deletion sync (off by default),
and `--backend imapsync|docker`. Run with `--help` for the full list.

### Automating many mailboxes

Loop over a user list:

```bash
while read -r user; do
  scripts/migrate-mailboxes.sh --source google \
    --source-user "$user" --target-host mail.example.com --target-user "$user" \
    --source-passfile "${user}.pass" --target-passfile target.pass
done < users.txt
```

Store source passwords per mailbox in protected files, or sync with an
OAuth-token based method if the provider requires it (see Google below).

## From Exchange / Microsoft 365

- Enable IMAP4 on the mailbox server (Exchange admin center). For Microsoft
  365, IMAP is enabled at `admin.exchange.microsoft.com`.
- Use a mailbox that has basic authentication IMAP available. Exchange Online
  basic auth requires IMAP to be enabled; Microsoft increasingly requires
  OAuth for IMAP, in which case use an OAuth access token with imapsync
  (`--authmech1 XOAUTH2`). The simplest reliable route for most tenants is a
  legacy app-password-capable account or per-user app passwords where enabled.
- Ports: `993` (SSL) — the `exchange` preset sets this plus SSL.
- Calendar/contacts: Microsoft 365 supports vCard/`.ics` export from Outlook
  and full-mailbox export via eDiscovery (PST), which can then be imported.

## From Google Workspace

- Enable IMAP access for each user at `myaccount.google.com` → Security →
  Less secure apps/App passwords, or set the organizational IMAP setting to
  "Allow users to use IMAP".
- Create an **app password** for the account used for migration (Google's
  regular password will not work for IMAP). App passwords do not apply to
  accounts with strong/advanced protection; use OAuth in that case.
- Host `imap.gmail.com:993` with SSL — the `google` preset defaults to this.
- Google enforces connection limits (roughly 15 concurrent and rate limits per
  account); migrate accounts sequentially or in small parallel batches.
- Calendar/contacts: Google Takeout provides `.ics` and `.vcf` exports.

## From Zimbra

- Your old Zimbra server exposes IMAP on `993` (SSL). Point the source at it
  directly; the `zimbra` preset does this.
- If the old server is still online, this is a same-vendor-to-same-vendor
  IMAP sync, which imapsync handles well including folder naming.
- Zimbra-specific bulk export (mailbox `tgz` export) is available through the
  Zimbra admin console and `zmmailbox`. For very large sites, exporting a
  mailbox TGZ and importing it on Oasis avoids re-uploading every message over
  IMAP.
- Calendar/contacts live in Zimbra's Webmail; export them via the Webmail
  preferences before decommissioning.

## Verification checklist

1. Folder hierarchy matches the source (`--automap` handles renamed folders;
   check any unmapped ones in the imapsync log).
2. Message counts match: compare `SELECT INBOX` counts on both sides.
3. Drafts, Sent, and custom folders were copied, and Trash/Spam were excluded
   as intended.
4. A test message sent from inside and outside the domain arrives and is
   readable after MX cutover.
5. SPF, DKIM, and DMARC for the domain pass `scripts/oasis-health.sh` — see
   `docs/COMPLIANCE.md` before going live with new mail flow.