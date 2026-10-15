# Oasis mail administration

Add and remove mail **domains** and **users** from one page, with every user
created in both planes at once. The service is `scripts/oasis-mail-admin.py`; the
CLI equivalent lives in `scripts/oasis-mail-user.py`.

## Why this exists (and not the Stalwart UI)

A mail account here is two records that have to agree:

| Plane | What it is | Who writes it |
| --- | --- | --- |
| **Authentik** | the person, their groups, their password | `scripts/oasis-mail-admin.py` |
| **Stalwart** | the mailbox (a `User` account, **no** credential) | `scripts/oasis-mail-admin.py` |

This estate signs in through Cerulean Authentik (SSO). When Stalwart
authenticates a user against an external directory it refuses to write credentials
for that account ("*i cannot add another user with SSO enabled*") — so its own admin
UI cannot create a user. There is no Stalwart-side fix that keeps the estate's
single identity store; the account must be created in Authentik, and the mailbox
created here as a side effect. That is exactly what this page does.

A mail domain lives in two planes too: a **Stalwart** record (DKIM keys,
automatic certificate) and an authoritative **DNS zone** that **Cerulean** owns
and mints. Adding a domain does both — it creates the Stalwart domain, has
Cerulean mint the zone, delegates the name, and publishes the generated
DKIM/SPF/DMARC into it — so a new domain is one click, not a recipe. Removing a
domain takes all of it back: the mailbox domain, the zone, its records, and the
delegation. Only one thing stays outside the service, because it is not a DNS
write at all: for a brand-new apex, the **registrar** step that points the domain
at the estate nameservers. The **MX** is the operator's routing choice and an MX
already in the zone is never overwritten — but a zone that has none (the zone
Cerulean just minted for a tenant) gets Stalwart's, so the domain can receive the
mail it was built for.

## Sign-in

The page is **OIDC-native** against Cerulean Authentik — the estate's sign-in
posture allows no other pattern (no forward-auth, no second password store). The
browser is run through a real authorization-code + PKCE flow with the existing
`oasis-admin` client, the `id_token` is verified against Authentik's JWKS
(RS256, `iss`/`aud`/`exp`/`nonce`), and the caller must be a member of
`OASIS_ADMIN_GROUP` (default `Oasis`) — checked against Authentik's own group
records, never trusted from a token claim.

The client secret is read from Authentik at runtime rather than stored in the
environment; sessions are HMAC-signed cookies with a CSRF token on every write.

## Interface

| Route | Purpose |
| --- | --- |
| `GET /` | the page (redirects to `/auth/login` when signed out) |
| `GET /auth/login`, `GET /auth/callback` | the OIDC flow |
| `GET /auth/logout` | clear the session |
| `GET /api/state` | JSON: domains and users |
| `POST /api/domains` | `{action: add\|remove\|zone\|publish, name, description?}` |
| `POST /api/users` | `{action: add\|remove, email, name?, groups?}` |
| `POST /api/users/password` | `{email}` — rotate the Authentik password (returned once) |

Adding a user generates a password and returns it once; adding a domain returns
what it did (zone, delegation, records) and the few follow-ups that are not DNS
writes here (MX, and the registrar step for a new apex). `remove` also deletes the
name's Cerulean zone and its delegation, not just the Stalwart record.

### Zones and DNS

Cerulean (TrustOps) owns the estate resolver: it runs Technitium and is the only
principal allowed to create zones and write records into it. Stalwart's own
publisher is deliberately off. So:

- Adding a domain mints its authoritative zone **through Cerulean**
  (`POST /api/service/domains`, authenticated with the Cerulean service key in
  the vault) before publishing anything into it.
- The **Create zone** action does the same for a domain that was added earlier
  (or whose zone was never created). It is idempotent — a zone Cerulean already
  serves comes back as *already present* rather than an error.
- **Publish DNS** then writes the domain's generated DKIM/SPF/DMARC (and
  SRV/MTA-STS/TLS-RPT/autoconfig) records into that zone through Cerulean. The
  SPF it writes also names the estate's outbound relay (`SPF_RELAY_INCLUDE`,
  default `spf.mailjet.com`), because this host hands mail to a smarthost rather
  than sending it itself; an SPF the zone already has is left alone.
- Removing a domain unregisters the zone again (`DELETE /api/service/domains`),
  which drops the zone and every record in it, and removes the delegation the
  page had written into the parent. Cerulean refuses its own platform zones, and
  `PROTECTED_ZONES` (default `innotel.us`) refuses them here too.

### Delegation

Delegation is Cerulean's call, so the page does it rather than telling you to:

- A name **inside a domain the estate already serves** (e.g. `signara.innotel.us`
  inside `innotel.us`) is delegated automatically — the child's NS records are
  written into the parent zone, pointing at the parent's own nameservers. Remove
  takes them out again. This is the common case and needs nothing from you.
- A name at the **apex of a domain nobody has delegated yet** needs its
  **registrar** pointed at the estate nameservers (`PUBLIC_NS`, default
  `ns1.innotel.us,ns2.innotel.us`). No registrar API exists in this estate, so
  the page reports the exact servers to use instead of pretending it did it —
  registering and delegating a domain is the one act that is not a DNS write.

Publish is still guarded: if the resolver does not serve the zone, nothing is
written — the service checks that the records the resolver returns actually
belong to the zone before writing, so it never mints a private zone DNS would not
delegate to. With zone creation wired in, that branch is only reached when
Cerulean itself could not create the zone. A read that Cerulean *refuses* (a key
without `dns:read`, say) is reported as that refusal, not as a zone that is
missing.

## Provisioning tenants from another platform

This page is for operators. An application does not drive it: it calls Cerulean,
which registers the tenant's zone and has the mail half built here
(`POST /api/service/provision`). The whole contract — the one call, its scopes,
its idempotency and its error codes — is in `docs/MAIL-PROVISIONING.md`.

## Deploy

1. Install the script and unit from this repo:

   ```bash
   install -m 0644 scripts/oasis-mail-admin.py /opt/oasis/scripts/oasis-mail-admin.py
   # the page imports these beside it (Cerulean bridge client + zone parser)
   install -m 0644 scripts/cerulean/cerulean.py \
     scripts/cerulean/cerulean_publish_zone.py /opt/oasis/scripts/
   install -m 0644 systemd/oasis-mail-admin.service /etc/systemd/system/
   install -d -m 0750 /etc/oasis
   install -m 0640 scripts/oasis-mail-admin.env.example /etc/oasis/oasis-mail-admin.env
   # set SESSION_SECRET, then:
   systemctl daemon-reload && systemctl enable --now oasis-mail-admin
   ```

   This file and its companion scripts describe the **Oasis mail stack as shipped
   in this repo** — Compose, Zimbra, npm-proxy-hosts, `local|production` setup
   modes, and `scripts/setup-oasis.sh`. The estate this machine actually runs is
   **Stalwart on the host**, configured by `recon/cerulean.py` and
   `/opt/oasis/scripts/setup-oasis.sh`; the environment template
   `scripts/oasis-mail-admin.env.example` (and the operations in this script) are
   written for that estate, which is why they read `STALWART_ENV` and
   `PROTECTED_ZONES` rather than any Compose/Zimbra settings.

2. Publish the name through the edge (NPM) and point DNS at the edge — the
   redirect URI is `https://admin.oasis.innotel.us/auth/callback`, so the host
   must answer on that exact name. The estate does this through the Cerulean DNS
   and NPM APIs, never by hand.

3. The `oasis-admin` Authentik provider must carry that redirect URI (it does)
   and be bound to the admin group (it is).
