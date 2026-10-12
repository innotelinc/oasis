# 🌴 Oasis — Platform Stack Role

**Classification: MailOps**

Enterprise email and collaboration: mail transport, calendars, contacts, and team communications on your own infrastructure.

This page declares Oasis's role in the
[**Innotel Platform Stack**](https://github.com/innotelinc/innotel-platform-stack) —
the canonical single-responsibility architecture. The stack is defined in exactly one
place; this page links each product to it and states what this platform owns, consumes,
provides, and explicitly does not own.

## Owns

- Email
- Calendars
- Contacts
- Collaboration
- Mail security
- Mail routing
- Team communications

## Provides

- Mail platform for the ecosystem

## Consumes

- Authentik — identity, SSO
- Cerulean Vault — secrets, SMTP credentials, relay credentials
- Cerulean — certificates and trust
- Magnate — subscriptions and entitlements
- NPM Edge — public routing, TLS termination at the edge

## Explicitly does NOT own

- Identity (Authentik)
- Secrets (Cerulean Vault)
- Billing (Magnate)


## Secrets (Cerulean Vault)

The platform's SecretOps is **Cerulean Vault** — HashiCorp Vault, KV v2, hosted by
Cerulean — with `vault://<mount>/<path>#<key>` references in `.env`.

Cerulean mints this stack's **path-scoped** token (its policy covers only
`cerulean/data/oasis`, never a sibling's secrets) and renews it in place. Copy it
to `./data/vault/token/oasis.token`, then move any plaintext values across:

```bash
VAULT_ADDR=http://<cerulean-host>:8200 \
  VAULT_TOKEN_FILE=./data/vault/token/oasis.token \
  VAULT_PREFIX=cerulean VAULT_PATH=oasis \
  python3 scripts/vault-migrate.py --from-env-file .env \
    --keys POSTGRES_PASSWORD,AUTHENTIK_CLIENT_SECRET
```

`vault-migrate.py` never prints a value, unions with whatever is already at the
path (so a re-run is a no-op, not an overwrite), and accepts either `.env` or a
legacy Infisical workspace as its source.

A `vault://` value is the platform's reference *form*; it is resolved by whichever
layer consumes it (ONYX's Go services, Distro's Node control plane, Zeus at boot,
Atlas at setup). This repo has no resolver, so `.env` must hold the resolved
value — a reference left in place reaches the container as a literal string.

## Trust (Cerulean)

**Cerulean owns DNS and TLS for the mail platform.** It runs the estate's Technitium
and is the only principal permitted to write to it, and it issues the mail wildcard's
Let's Encrypt certificate (`*.innotel.us`) with a DNS‑01 challenge written straight
into Technitium. Oasis never talks to Technitium or to an ACME CA itself; it holds a
path‑scoped Cerulean service key (`ceru_…`, kept in Cerulean Vault at
`cerulean/oasis#cerulean_service_key`) and pulls certificate material and pushes DNS
records over Cerulean's service bridge.

The mail server reflects that: every Stalwart domain is `dnsManagement: Manual` and
`certificateManagement: Manual`, the direct Technitium `DnsServer` (a TSIG key) and the
server's own `AcmeProvider` are removed, and `scripts/cerulean/` does the rest —
`stalwart_cerulean_only.py` flips the posture, `stalwart_install_cert.py` installs the
wildcard, `cerulean_publish_zone.py` publishes Stalwart's generated DKIM/SPF/DMARC/MTA‑STS/
autoconfig records (singleton‑ and override‑aware, so it never duplicates an SPF/DMARC/CAA
or overwrites an estate‑owned value), and `oasis-cerulean-renew.timer` runs both daily.
See [`scripts/cerulean/README.md`](../scripts/cerulean/README.md) for the full model.

## Golden rules

- **Authentik = Identity** · **Cerulean Vault = Secrets** · **Cerulean = Trust** ·
  **ONYX = Storage** · **Magnate = Revenue** · **NPM Edge = Edge** — everything else is a business function.
- No platform duplicates another's responsibility.
- No credit in commits, footers, or headers to anyone but the project owner.

---

*Oasis · MailOps · [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack)*
