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

### Legacy: the Infisical profile

This stack currently still imports its credentials into an **Infisical** workspace and
derives `.env` from it. Enable it with:

```bash
# generate the required keys and add them to .env
openssl rand -base64 32   # INFISICAL_ENCRYPTION_KEY
openssl rand -hex 16      # INFISICAL_AUTH_SECRET
openssl rand -hex 16      # INFISICAL_DB_PASSWORD

# start the profile and provision the workspace + import .env secrets
docker compose -f docker-compose.yml -f compose.infisical.yml --profile infisical up -d
bash scripts/infisical-setup.sh
```

See [compose.infisical.yml](../compose.infisical.yml) and
[scripts/infisical-setup.py](../scripts/infisical-setup.py) for details.

## Golden rules

- **Authentik = Identity** · **Cerulean Vault = Secrets** · **Cerulean = Trust** ·
  **ONYX = Storage** · **Magnate = Revenue** · **NPM Edge = Edge** — everything else is a business function.
- No platform duplicates another's responsibility.
- No credit in commits, footers, or headers to anyone but the project owner.

---

*Oasis · MailOps · [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack)*
