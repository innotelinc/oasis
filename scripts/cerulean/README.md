# Cerulean trust integration

Cerulean is the estate's **TrustOps** platform and the only principal allowed to use
Technitium DNS. It owns the mail wildcard's Let's Encrypt issuance (DNS‑01 written
straight into Technitium) and exposes both the certificate material and the DNS
records over its service bridge. Oasis holds a **path-scoped** `ceru_…` service key
and never talks to Technitium or to an ACME CA itself.

These scripts make Stalwart a pure consumer of that trust:

| Script | Role |
| --- | --- |
| `cerulean.py` | Service-bridge client (`Bearer ceru_…`). Resolves the key from `CERULEAN_SERVICE_KEY`, `/etc/oasis/cerulean-service.key`, or **Cerulean Vault** (`cerulean/oasis#cerulean_service_key`) via `VAULT_TOKEN_FILE`. |
| `stalwart_cerulean_only.py` | Flips every Stalwart domain to `dnsManagement: Manual` + `certificateManagement: Manual` and removes the direct Technitium `DnsServer` and the unused `AcmeProvider`, so Stalwart stops writing DNS/ACME and Cerulean is the sole Technitium writer. |
| `stalwart_install_cert.py` | Fetches the `*.innotel.us` wildcard (chain + key) from Cerulean, writes it to `/etc/stalwart/tls/`, and registers it as the default `Certificate`. Idempotent: a matching expiry + SAN set is a no‑op so the timer is cheap. |
| `cerulean_publish_zone.py` | Publishes Stalwart's generated zone through the bridge. Records are classed `exact` / `add` / `singleton` / `override` so a run never duplicates a singleton (SPF, DMARC, MX, CAA `issue`, CNAME) and never writes over an estate‑owned value. |
| `cerulean-renew.sh` | Runs the installer then the publisher for every domain; the systemd timer calls this. |
| `verify_trust.py` | One read-only verdict on the whole posture (Manual domains, no TSIG/ACME route, cert matches Cerulean, zones conflict-free) for CI/drift-check; exit 2 when it cannot judge. |
| `systemd/oasis-cerulean-renew.{service,timer}` | Daily renewal, `Persistent=true` so a missed run catches up. |

## Domesticated records

A few records are the *estate's*, not the template's, and the publisher reports them as
`override` (kept, never written):

- apex + `mail` **SPF** — relay‑aware (`include:spf.mailjet.com`) because ISP port 25 is blocked;
- apex **MX** routing preference;
- `_dmarc` **DMARC** — carries a real `rua`;
- the CAA **`issue`** line — a plain `letsencrypt.org` grant, *not* the `accounturi=` pin that
  would lock issuance to a single ACME account.

TLSA is skipped: Stalwart emits it, but the bridge has no TLSA record shape.

## Install

```bash
install -d -m 755 /opt/oasis/scripts
install -m 644 cerulean.py stalwart_cerulean_only.py stalwart_install_cert.py \
  cerulean_publish_zone.py /opt/oasis/scripts/
install -m 755 cerulean-renew.sh /opt/oasis/scripts/
install -m 644 /path/to/repo/systemd/oasis-cerulean-renew.* /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now oasis-cerulean-renew.timer
```

## Run

Everything is a check by default; `--apply` writes.

```bash
python3 stalwart_cerulean_only.py                 # is Stalwart Cerulean-only?
python3 stalwart_cerulean_only.py --apply         # flip it (drops the TSIG server)
python3 stalwart_install_cert.py                  # is the wildcard up to date?
python3 stalwart_install_cert.py --apply          # fetch + install (--issue mints it)
python3 cerulean_publish_zone.py --all-domains    # what would be published?
python3 cerulean_publish_zone.py --all-domains --apply
systemctl start oasis-cerulean-renew.service      # the timer's exact job
```

`--strict` makes `cerulean_publish_zone.py` exit non-zero when an `override` record has
drifted, which is the form a CI check wants; `conflict` and `failed` are always failures.

## Secrets

The service key lives in **Cerulean Vault** at `cerulean/oasis#cerulean_service_key`.
Cerulean mints this stack's path-scoped token (`VAULT_PRODUCT_TOKENS=oasis` there) and
writes it to `/vault/token/oasis.token`; copy it to `/etc/oasis/vault-token`. The
plaintext file at `/etc/oasis/cerulean-service.key` is only a legacy fallback.
