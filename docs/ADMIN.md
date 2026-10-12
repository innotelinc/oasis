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

Stalwart authenticates against the external Authentik OIDC directory. With an
external directory it refuses to write credentials — so its own admin UI cannot
create a user ("*i cannot add another user with SSO enabled*"). There is no
Stalwart-side fix that keeps the estate's single identity store; the account must
be created in Authentik, and the mailbox created here as a side effect. That is
exactly what this page does.

Domains are a single Stalwart record. Adding one copies the way `innotel.us` is
already configured — automatic DKIM, automatic DNS publishing against the estate
resolver, and an automatic certificate — so a new domain is one click, not a
recipe. NS delegation at the registrar and the MX record stay manual (the
publisher never writes MX) and the page says so.

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
| `POST /api/domains` | `{action: add\|remove, name, description?}` |
| `POST /api/users` | `{action: add\|remove, email, name?, groups?}` |
| `POST /api/users/password` | `{email}` — rotate the Authentik password (returned once) |

Adding a user generates a password and returns it once; adding a domain returns
the follow-up steps that Stalwart does not do (NS, MX).

## Deploy

1. Install the script and unit:

   ```bash
   install -m 0644 scripts/oasis-mail-admin.py /opt/oasis/scripts/oasis-mail-admin.py
   install -m 0644 systemd/oasis-mail-admin.service /etc/systemd/system/
   install -d -m 0750 /etc/oasis
   install -m 0640 scripts/oasis-mail-admin.env.example /etc/oasis/oasis-mail-admin.env
   # set SESSION_SECRET, then:
   systemctl daemon-reload && systemctl enable --now oasis-mail-admin
   ```

2. Publish the name through the edge (NPM) and point DNS at the edge — the
   redirect URI is `https://admin.oasis.innotel.us/auth/callback`, so the host
   must answer on that exact name. The estate does this through the Cerulean DNS
   and NPM APIs, never by hand.

3. The `oasis-admin` Authentik provider must carry that redirect URI (it does)
   and be bound to the admin group (it is).
