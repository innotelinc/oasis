# Domain health monitoring and compliance reporting

Oasis ships `scripts/oasis-health.sh` to monitor the health of a live
deployment and produce a compliance report for the domain. It checks the
things that determine whether the platform is reachable, mail is deliverable,
and the deployment meets common email compliance expectations.

## What it checks

Per configured hostname (`app`, `api`, `auth`, `mail`, `files`, `admin` by
default):

- **DNS** — the `A` record resolves.
- **TLS** — a certificate is served on `:443`, the chain verifies, and the
  certificate has not expired or entered the near-expiry window.
- **HTTPS** — the site responds over HTTPS.

For the base domain (mail compliance):

- **MX** — records exist so the internet can route inbound mail.
- **SPF** — a `v=spf1` record exists; `-all` (hard fail) is the recommended
  policy.
- **DKIM** — at least one selector is published under
  `<selector>._domainkey.<domain>` (checked: `zimbra`, `default`, `mail`,
  `oasisdkim`, `k1`).
- **DMARC** — a `_dmarc` record exists; `p=none` is reported as a warning
  because it does not protect against spoofing.
- **Ports** — SMTP `25`/`465`/`587` and IMAP `993` / POP `995` reachability.

## Usage

```bash
# Full check against Oasis module hostnames (reads OASIS_BASE_DOMAIN from .env)
scripts/oasis-health.sh

# Write a dated Markdown compliance report under reports/
scripts/oasis-health.sh --report

# Machine-readable summary for dashboards or cron
scripts/oasis-health.sh --json
# {"domain":"oasis.example.com","pass":18,"warn":2,"fail":0}

# Only failures and warnings
scripts/oasis-health.sh --quiet

# Specific domain and hostnames, custom resolver
scripts/oasis-health.sh --domain example.com --hosts "mail app" --dns-resolver 1.1.1.1
```

Exit codes are cron-friendly: `0` healthy, `1` warnings, `2` failures.
Dependencies: `dig` (dnsutils), `openssl`, and optionally `curl` (HTTPS
status codes; the TLS check works without it).

### Example cron

```bash
# Daily at 06:00, keep the last 30 reports
0 6 * * * root /opt/oasis/scripts/oasis-health.sh --report --quiet \
  && find /opt/oasis/reports -name 'oasis-compliance-*.md' -mtime +30 -delete
```

## Reading a report

Reports land in `reports/oasis-compliance-<timestamp>.md` as a table:

| check | status | detail |
|---|---|---|
| tls app.example.com | PASS | certificate valid until Jul 24 2027 (261 days, valid chain) |
| dmarc example.com | WARN | policy=none; tighten to quarantine or reject |
| mx example.com | FAIL | no MX records; mail will not be delivered |

The report also lists the six module gateway hostnames and which service each
exposes. Fix the FAIL entries before opening the deployment to real users;
treat WARN entries as hardening backlog.

## What "compliance" means here

These checks cover the email-policy signals most commonly audited for a mail
platform: DNS records that authenticate outbound mail (SPF, DKIM, DMARC) and
the TLS posture of its public endpoints. They are operational checks, not a
substitute for an organizational security/compliance program. Authentik SSO
policy, application authorization, and tenant isolation are covered in
`docs/SSO.md` and `config/authentik/README.md`.

## Related tooling

- `scripts/smoke-oasis.sh local|production` — quick container + readiness
  smoke test.
- `scripts/init-certificates.sh` — first-time production certificate issuance
  for the stack proxy.
- `scripts/npm-proxy-hosts.py` — provisions NPM proxy hosts and the wildcard
  Let's Encrypt certificate that these checks validate.
- `docs/BACKUP-RESTORE.md` — recovery procedures.