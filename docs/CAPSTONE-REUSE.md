# Capstone reuse notes

`innotelinc/capstone` was reviewed as a reference implementation. Its setup,
proxy automation, and smoke-test scripts provide useful operational patterns,
but Oasis does not copy Capstone's unrelated voice, PBX, Grist, n8n, or
telephony services.

Reference revision reviewed:

```text
84bb643ddc7ac337b70aa9d34173358d36d6b25c
```

## Adapted patterns

- Idempotent setup script with prerequisite validation.
- Generate secrets only when `.env` does not already exist.
- Preserve user configuration during reruns.
- Explicit local/production deployment modes.
- Compose-based service startup.
- Lightweight smoke testing with clear PASS/FAIL output.
- Separate operational scripts rather than embedding every action in Compose.

Oasis adaptations are in:

- `scripts/setup-oasis.sh`
- `scripts/smoke-oasis.sh`
- `scripts/backup-oasis.sh`
- `scripts/restore-oasis.sh`
- `scripts/init-certificates.sh`

## Not copied blindly

Capstone's installer can partition disks, configure a live USB system, and
install a much larger voice platform. Those actions are intentionally not part
of the Oasis setup script because disk partitioning and host replacement are
high-risk operations. Capstone's Nginx Proxy Manager automation also targets
Capstone-specific ports and hostnames; Oasis currently uses an internal Nginx
proxy with Authentik and its own service network.

Review the upstream repository and its license before redistributing derived
code. Keep upstream notices where required and record future borrowed files and
revisions here.
