# Provisioning mail tenants — one write, through Cerulean

Creating a mail tenant in this estate is **one request**. An application holds a
single Cerulean service key and calls Cerulean; Cerulean registers the zone and
has the mail half built. The application never holds a resolver token, a Stalwart
credential or an Authentik bootstrap token, and it never talks to the mail host
directly.

```
application ──(its Cerulean key)──▶ POST /api/service/tenants   [Cerulean]
                                        │
                                        ├─ registers the zone (Cerulean is the only Technitium writer)
                                        ├─ ensures the tenant's Authentik group
                                        └─ POST /api/service/provision          [mail host]
                                              (the caller's own key, forwarded)
                                                  ├─ Stalwart domain + DKIM keys
                                                  ├─ DKIM/SPF/DMARC into the Cerulean zone
                                                  ├─ delegation NS in the parent zone
                                                  ├─ Authentik identity in the mail groups
                                                  └─ Stalwart mailbox (one-time password)
```

## Why it is one call and not four

A tenant is **two planes that must agree**: a name the resolver answers for, and
a mail domain with an identity and a mailbox behind it. Trying to keep four
coordinated writes in an application's own code — Stalwart domain, Cerulean zone,
delegation and records, Authentik identity — is how estates end up with zones
that answer for domains nobody can log in to. The coordination belongs to the
platform that owns the planes, so the contract is a single call and the caller
does not need to know how many writes are behind it.

**Trust has one owner: Cerulean.** The mail host does not keep a list of who may
call it. Every request is adjudicated by asking Cerulean (`GET /api/service/whoami`)
what the presented key is and what it is scoped for, so revoking a key in Cerulean
takes effect on the next request everywhere, with no second list to drift.

## The call

```
POST /api/service/tenants
Authorization: Bearer ceru_<prefix>_<secret>
Content-Type: application/json

{
  "domain": "acme.example",          # required — the tenant's domain
  "email": "ops@acme.example",       # required — its first account; must be at that domain
  "name": "Acme Ops",                # optional — the person's display name
  "groups": ["paid_users"],          # optional — extra Authentik groups beyond `Mail`
  "description": "Acme"              # optional — the Stalwart domain's description
}
```

Scopes the key needs (minted in Cerulean at `POST /api/service/keys`):

| Scope | For |
|---|---|
| `tenants:write` (or `tenants`, `tenant`, `*`) | this endpoint |
| `mail:provision` | the mail hop — the mail host checks the forwarded key against Cerulean |
| `dns:write` **and** `dns:read` (or `dns`, `*`) *(optional)* | lets the caller's own key perform the DNS writes; without both, the platform key does |

`dns:write` is about *whose* authority is recorded, not about whether the work
happens: the answer says so either way, in `authority`. Both scopes are needed
because publishing **reads** a zone before it writes it — a key that may write and
not read would be credited as the authority while the tenant's records went
unwritten, so it is handed the platform's key instead.

### Answer

```json
{
  "tenant": {"id": 7, "slug": "acme"},
  "domain": {"name": "acme.example", "created": true},
  "mail": {
    "tenant": {"domain": "acme.example", "id": "..."},
    "domain": {"domain": "acme.example", "created": true, "enabled": true,
               "zone": {"...": "Cerulean's answer"},
               "dns": {"...": "the records written"}},
    "account": {"email": "ops@acme.example", "created": true, "groups": ["Mail"],
                "authentik": "created pk=...", "mailbox": "created id=..."},
    "existed": false,
    "authority": "caller's Cerulean key",
    "password": "one-time-value"
  }
}
```

**201** when this call created the tenant, **200** when it was already there.
The password comes back **only** from the call that created the account.

## Idempotency

The caller is usually a billing webhook, so it will retry, and a retry must not
leave a second domain, a second mailbox, or — worse — a second password.

- The zone is registered once; a retry finds it and reports `"created": false`.
- The Stalwart domain and its DKIM keys exist once.
- The Authentik identity is created once; re-asking **merges** groups into it
  rather than replacing them, so a retry can never strip a group an operator added.
- The mailbox exists once.
- The password is minted only by the call that creates the account. A retry
  answers `"password": null` with `"existed": true`; that pair is how a caller
  tells a first delivery from a retry.
- Two identical requests in flight at once are serialised per domain, so neither
  sees a half-built tenant.

A retry deliberately re-asks the mail host even when the zone is already there,
so a tenant deleted behind the platform's back is rebuilt by the next retry.

The one limit worth knowing: a retry cannot hand back a password it already gave
out. A caller that loses the original answer should **rotate** rather than
re-provision.

## Errors

| Status | Meaning | What the caller should do |
|---|---|---|
| 400 | `domain` or `email` missing/unusable, or the address is not at the domain | fix the request; nothing was created |
| 401 | no key, or a key Cerulean does not know (revoked, other estate) | get a key from Cerulean |
| 403 | the key lacks `tenants:write` (or, on the mail hop, `mail:provision`) | ask Cerulean for the scope |
| 502 | the mail host refused or could not be reached — the body carries `domain.created` and the mail host's own answer | fix and retry; the zone is already registered |
| 503 | mail provisioning is not configured on Cerulean (`MAIL_PROVISION_URL`) | configure it; `domain.created` says what did happen |

A 502/503 is not a silent half-tenant: the response always says which parts
exist, and the call is safe to repeat.

## The mail host's own surface

Cerulean is the only caller. That is enforced twice: a Cerulean key **and** a
source-address allow-list (`MAIL_SERVICE_CLIENTS`, default
`192.168.1.71,127.0.0.1,::1`), so the machine surface is not on the internet even
for somebody holding a valid key.

| Route | Scope | Purpose |
|---|---|---|
| `POST /api/service/provision` | `mail:provision` | the one write above |
| `GET /api/service/state` | `mail:read` | domains and accounts as the mail host sees them |

There is deliberately **no** fallback to a browser session: an operator's cookie
is a different principal from a machine caller. When Cerulean cannot be asked,
the service refuses (503) rather than acting without the trust owner.

## Configuration

Cerulean (`cerulean-src/server/.env` or the deployment's):

| Variable | Meaning |
|---|---|
| `MAIL_PROVISION_URL` | where the mail host's provisioning surface is, e.g. `http://192.168.1.15:8091`. Empty disables the endpoint. |
| `MAIL_PROVISION_TIMEOUT_MS` | how long to wait on the mail host (default 30000) |

Mail host (`/etc/oasis/oasis-mail-admin.env`):

| Variable | Meaning |
|---|---|
| `MAIL_SERVICE_CLIENTS` | addresses allowed to reach `/api/service/*` (default `192.168.1.71,127.0.0.1,::1`) |
| `CERULEAN_API_URL` | the bridge to ask, default `http://192.168.1.71:3003/api` |
| `PROVISION_SCOPE` / `READ_SCOPE` | scope names required for the two routes (default `mail:provision` / `mail:read`) |

## Example

```bash
# A key with both scopes, minted once in Cerulean
KEY=ceru_xxxxxxxxxxxxxxxx_yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy

curl -sS -X POST http://192.168.1.71:3003/api/service/tenants \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"domain":"acme.example","email":"ops@acme.example","name":"Acme Ops"}'
```

Run it twice: the second answer is **200**, `domain.created` is `false`, and
`mail.password` is `null`. That is the whole retry story.

## Granular operations

The one-write call is the integration. For the rare operator case, the mail
administration page (and its session API) still does the individual steps —
add/remove a domain, add/remove a mailbox, rotate a password — and Cerulean's
bridge still exposes the pieces (`POST /api/service/domains`,
`POST|DELETE /api/service/dns/records`). Applications should not use those; they
exist so an operator can repair a single plane without re-provisioning a tenant.
