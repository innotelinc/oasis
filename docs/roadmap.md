# Oasis — direction and roadmap

Oasis is the estate's **MailOps** platform: email, calendars, contacts and team
communication. This page records where the codebase is, the decision to move off
Zimbra, how the three candidate systems compare, what we borrow from each, and the
order we do it in.

## Where the codebase is now

The shipped foundation builds and deploys **Zimbra FOSS from source** in Docker and
wires Authentik SSO, NPM edge routing, wildcard certificates, health/compliance
reporting and mailbox migration around it (see [CHANGELOG.md](../CHANGELOG.md)).
The mail service itself is Zimbra; everything else in this repo is the platform
around it.

## Why we are moving off Zimbra

Zimbra is a large **Java** stack (JVM mailboxd + Jetty + Lucene + MariaDB/OpenLDAP)
that ships as a monolithic `zcs-*.tgz` installer. In practice it is:

- **Heavy** — a JVM and a bundled database on a dedicated host, idle memory well
  above what the estate's other platforms use, for the mail volumes we actually run.
- **Slow to build and to change** — a `zm-build` is minutes-to-hours, and its
  version and OS matrix make reproducible builds a project of their own.
- **Troublesome to operate** — its own installer owns the host (it has to swap
  Postfix out, manage its own LDAP/DB, and its upgrades are in-place and fragile).

None of that is a mail-delivery advantage. The engine we want is small, fast, and
not a second operating system.

## The candidates

| | **Stalwart** | **Bulwark** | **Forward Email** |
| --- | --- | --- | --- |
| What it is | all-in-one mail engine | webmail client | full mail service + platform |
| Language | Rust (single binary) | TypeScript / Next.js | JavaScript / Node.js |
| Protocols | SMTP, IMAP, POP3, **JMAP**, ManageSieve, CalDAV/CardDAV (roadmap) | JMAP client | SMTP, IMAP, POP3, MX, CalDAV, CardDAV, REST API |
| Storage | **embedded** (RocksDB; no external DB) | — | MongoDB + Redis + SQLite + encrypted mailbox blobs |
| Footprint | one process, tens of MB | one web app | ~10 Node services + Mongo + Redis + SQLite, Ansible, load balancer |
| Auth | OIDC / LDAP / internal | follows the server | its own accounts |
| Licence | AGPL | AGPL | MIT |
| Standout | modern, embedded, low-ops | polished JMAP webmail, no separate account store | encryption at rest, aliases/forwarding, API-first, DAV, deliverability tooling |

Forward Email is genuinely impressive and fully open source, but it is **not a
lighter Zimbra** — it trades a JVM monolith for a fleet of Node services, a Mongo
cluster, Redis and Ansible-managed hosts. It is the right shape for a hosted
multi-region service; it is the wrong shape for a box in the estate. What it has
that we want is its *feature model*, not its runtime.

## Decision

**Oasis is the mail *platform*, not a new mail engine.** We do not write our own
SMTP/IMAP/JMAP server — correctness, deliverability and security there are a
decade of work — and we do not adopt Forward Email's service topology.

- **Stalwart** is the engine: one Rust binary, embedded store, JMAP-native, low-ops.
- **Bulwark** is the webmail front end, a JMAP client with no account store of its own.
- **Cerulean** owns trust (DNS + certificates), **Cerulean Vault** owns secrets,
  **Authentik** owns identity — all three already integrated (see
  [scripts/cerulean/README.md](../scripts/cerulean/README.md)).
- **Forward Email** is a feature source: we deliberately absorb its best ideas into
  the layer above Stalwart rather than run its stack.

This keeps the thing we operate small (one mail process, one web app, no external
DB) while still reaching the feature set people judge a mail platform on.

## What we take from Forward Email

| Feature | What it gives users | How Oasis gets it |
| --- | --- | --- |
| Encrypted mailbox at rest | mail readable only by the mailbox owner | Stalwart store encryption / per-account at-rest encryption review |
| Aliases & forwarding rules | one inbox, many addresses; forward without a mailbox | Stalwart aliases + Group addresses + sieve rules |
| API-first administration | provision/change without the UI | `scripts/oasis-mail-user.py` today; a REST façade later |
| CalDAV / CardDAV | calendars and contacts in any client | Stalwart DAV when it lands; parity tracked here |
| Autodiscovery | a client configures itself from the address | MTA-STS + autoconfig/autodiscover records **already published** by the zone publisher |
| Per-domain automation | DNS, DKIM rotation, policy per domain | the Cerulean zone publisher + renewal timer |
| Deliverability tooling | bounce/feedback handling, reputation | Stalwart reporting + DMARC/TLS-RPT (already published) |

## Domains: mailbox domains vs sending domains

Not every domain hosted by the mail server is a place people log in. Two kinds, and
we should say which is which wherever a domain is added:

- **Mailbox domain** — a domain whose addresses are real mailboxes people sign into
  (today: `innotel.us`). Users are Authentik users in the `Mail` group.
- **Sending domain** — a domain that exists so a *product* can send transactional
  mail with its own SPF/DKIM/DMARC and reputation. It has one sender mailbox (or an
  app credential) and no user inboxes.

**Signara** is the estate's first declared sending domain. `signara.innotel.us`
carries Signara's outbound mail only: the sender mailbox is
`signara-alerts@signara.innotel.us` and the platform's From is
`no-reply@signara.innotel.us` (`SMTP_HELLO=signara.innotel.us` in Signara's config).
It consumes Oasis as transport and does not own mail; its SPF now names the estate
relay (`include:spf.mailjet.com`) because outbound leaves through it, not from the
server's own IP. Every future sending domain is added the same way and marked as
such, so "why does this platform have a mail domain?" always has an answer.

## Phases

0. **Trust + plumbing** *(done)* — Stalwart is Cerulean-only for DNS and TLS, the
   service key lives in Cerulean Vault, renewal runs on a timer, and
   `verify_trust.py` / `oasis-mail-health.py` / `oasis-mail-user.py` give one
   verdict on posture and one command for user administration wired to the
   Authentik `Mail` group.
1. **Identity-driven provisioning** — an Authentik user added to `Mail` gets a
   Stalwart mailbox automatically (event/webhook), so `add` in Authentik is the
   single act. Declare mailbox vs sending domains explicitly.
2. **Webmail + clients** — Bulwark behind the edge with Authentik SSO, JMAP
   autodiscovery, and CalDAV/CardDAV (Stalwart or a sidecar) for calendars/contacts.
3. **Operate** — backup/restore drill, drift check, and the mail posture checks
   wired into CI; alerting via the estate's alert pipeline.
4. **Hardening** — encrypted-at-rest review, alias/forwarding UX, and a REST façade
   over `oasis-mail-user.py` for Magnate/Zeus to provision on purchase.

Zimbra is retired as a target once phase 2 lands: the repo's installer, docs and
CI move to the Stalwart + Bulwark stack, and the Zimbra build path is archived.
