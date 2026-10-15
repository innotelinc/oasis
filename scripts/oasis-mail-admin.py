#!/usr/bin/env python3
"""Oasis mail administration — add and remove mail domains and users from a page

Stalwart's own admin UI cannot create a user here: this estate signs in through
Cerulean Authentik (SSO), and when Stalwart authenticates a user against an
external directory it refuses to write credentials for that account ("i cannot add
another user with sso enabled"). A mail account is therefore two records that must
agree, and this service is where they are created together:

  * **Authentik** — the person: the user object and membership of the account
    groups (`Mail`, plus any extras). Cerulean Authentik is the only identity
    source, so this is also the account's password store
  * **Stalwart** — the mailbox: a `User` account with no credential, because the
    credential is Authentik's

Domains are a single record (Stalwart), created the way innotel.us already is:
automatic DKIM, automatic DNS publishing against the estate's resolver, and an
automatic Let's Encrypt certificate

## Who may use it

It is **OIDC-native** against Cerulean Authentik — the estate's sign-in posture
allows no third pattern (no forward-auth, no second password store). The browser
is sent through a real authorization-code + PKCE flow with the existing
`oasis-admin` client, the id_token is verified against Authentik's JWKS, and the
caller must be a member of `OASIS_ADMIN_GROUP` (default `Oasis`), checked against
Authentik's own group records rather than trusted from a token claim

## Interface

    GET  /                      the page (redirects to /auth/login when signed out)
    GET  /auth/login            start the OIDC flow
    GET  /auth/callback         finish it
    GET  /auth/logout           clear the session
    GET  /api/state             JSON: domains and users
    POST /api/tenants           {domain, email, name?, groups?, description?}
    POST /api/domains           {action: add|remove, name, description?}
    POST /api/users             {action: add|remove, email, name?, groups?}
    POST /api/users/password    {email}   rotate the Authentik password (returned once)

Reads the Stalwart recovery admin from /etc/stalwart/stalwart.env and the
Authentik admin token from Cerulean Vault (`/etc/oasis/vault-token`); the OIDC
client secret is fetched from Authentik at runtime rather than stored. Nothing
secret is logged or returned except a password the operator just asked for
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:  # pragma: no cover - dependency is present on the hosts we deploy to.
    rsa = None

def _add_cerulean_to_path() -> None:
    """Make the Cerulean client importable in either layout this file runs from

    In this repo the client lives in `scripts/cerulean/`; on the mail host it sits
    beside this script (`/opt/oasis/scripts/cerulean.py`). Both directories go on
    the path so `cerulean` and `cerulean_publish_zone` resolve either way —
    `scripts/cerulean/` alone is a namespace package with no `Cerulean` in it, so
    the script's own directory has to be there too
    """
    here = Path(__file__).resolve().parent
    seen: set[str] = set(sys.path)
    for candidate in (here / "cerulean", here):
        if candidate.is_dir() and str(candidate) not in seen:
            sys.path.insert(0, str(candidate))
            seen.add(str(candidate))


# ── configuration ───────────────────────────────────────────────────────────
LISTEN = os.environ.get("LISTEN", "0.0.0.0:8091")
ISSUER = os.environ.get("ISSUER", "https://auth.cerulean.innotel.us/application/o/oasis-admin/")
CLIENT_ID = os.environ.get("CLIENT_ID", "oasis-admin")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("REDIRECT_URI", "https://admin.oasis.innotel.us/auth/callback")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Oasis")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
SESSION_TTL = int(os.environ.get("SESSION_TTL", "3600"))
STALWART_BASE = os.environ.get("STALWART_BASE", "https://mail.innotel.us")
STALWART_ENV = Path(os.environ.get("STALWART_ENV", "/etc/stalwart/stalwart.env"))
VAULT_TOKEN_FILE = Path(os.environ.get("VAULT_TOKEN_FILE", "/etc/oasis/vault-token"))
VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://192.168.1.71:8200")
VAULT_KV = "cerulean/data/oasis"
MX_HOST = os.environ.get("MX_HOST", "mail.innotel.us")
# The estate's public nameservers — what an apex domain's registrar has to point
# at. Only used when the domain is *not* inside a zone the resolver already
# serves (a child is delegated into its parent automatically).
PUBLIC_NS = [h.strip() for h in os.environ.get("PUBLIC_NS", "ns1.innotel.us,ns2.innotel.us").split(",")
             if h.strip()]
# Zones the page must never remove, whatever else it is asked to do.
PROTECTED_ZONES = {z.strip().lower()
                   for z in os.environ.get("PROTECTED_ZONES", "innotel.us").split(",")
                   if z.strip()}
SERVICE = "oasis-mail-admin"
USING = ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"]
DEFAULT_MAIL_GROUP = "Mail"
COOKIE = "oasis_admin"

# ── machine surface — Cerulean only ─────────────────────────────────────────
# An application does **not** call this host. It calls **Cerulean**, the estate's
# trust owner, and Cerulean registers the tenant and its zone, then calls the
# route below to create the mail half of it. So the only credential that works
# here is a Cerulean service key (`ceru_…`), and it is verified *with Cerulean on
# every call* — there is no token list on this host to drift out of step, and
# revoking a key in Cerulean revokes it here on the next request. The estate has
# exactly one credential authority, and it is not this service.
#
# `MAIL_SERVICE_CLIENTS` is the second lock: only these addresses may reach
# `/api/service/*` at all, so the machine surface is not on the internet even for
# a caller holding a key. Cerulean's bridge is the caller, so it names Cerulean.
SERVICE_CLIENTS = {c.strip() for c in os.environ.get(
    "MAIL_SERVICE_CLIENTS", "192.168.1.71,127.0.0.1,::1").split(",") if c.strip()}
# What Cerulean must say the caller's key is scoped for. Both are Cerulean's
# vocabulary, not this service's: this service only asks Cerulean the question.
PROVISION_SCOPE = os.environ.get("PROVISION_SCOPE", "mail:provision")
READ_SCOPE = os.environ.get("READ_SCOPE", "mail:read")


class NotFound(RuntimeError):
    """The caller named something that is not here — a 404, not a bad request."""


if not SESSION_SECRET:
    for candidate in (Path("/etc/oasis/oasis-mail-admin.secret"),
                      Path(__file__).resolve().with_name(".oasis-mail-admin.secret")):
        if candidate.is_file():
            SESSION_SECRET = candidate.read_text(encoding="utf-8").strip()
            break
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_urlsafe(32)
    print("warning: no SESSION_SECRET configured; sessions will not survive a restart",
          file=sys.stderr)


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {SERVICE}: {msg}",
          flush=True)


# ── machine surface — Cerulean only ─────────────────────────────────────────
def bearer(header: str) -> str:
    """The bearer credential on an `Authorization` header, or "" when there is none."""
    value = (header or "").strip()
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


def dns_authority_key(scopes: list, caller_key: str) -> str:
    """Which Cerulean credential publishes a tenant's records.

    The caller's own key is used only when Cerulean says it may do the whole DNS
    job: publishing reads a zone before it writes it, so a key that may write and
    not read would leave the tenant's records unwritten while the answer named
    the caller as the authority. Anything less falls back to the platform's own
    key — decided from Cerulean's scopes, never guessed — and `authority` in the
    answer says which credential acted.
    """
    if scope_allows(scopes, "dns:write") and scope_allows(scopes, "dns:read"):
        return caller_key
    return ""


def scope_allows(scopes: list, scope: str) -> bool:
    """Whether a Cerulean key carrying `scopes` covers `scope` (`mail:provision`).

    The spelling rules are the bridge's own, mirrored exactly: `*` covers
    everything, `mail:*` covers every mail scope, and a bare prefix (`mail`,
    `dns`) covers the scopes underneath it. A key Cerulean would admit for a route
    must not be refused here, or the estate's own answer would only be half-read.
    """
    have = {str(s).strip().lower() for s in scopes or []}
    want = scope.lower()
    prefix = want.split(":")[0]
    return "*" in have or want in have or f"{prefix}:*" in have or prefix in have


def bridge_has_no_whoami(message: str) -> bool:
    """Whether a `whoami` failure means "this bridge has no such route".

    Two shapes seen in the field: a bridge old enough to 404 the path, and one
    whose web tier answers a path it does not have with its own page — HTTP 200
    and HTML, as the estate's Cerulean does (its SPA serves unmatched routes).
    Only these fall back; a 401/403 is the trust owner's actual answer and stands.
    """
    return "HTTP 404" in message or "non-JSON" in message


def cerulean_identity(presented: str) -> dict:
    """What Cerulean says `presented` is: `{name, scopes, …}`, or a refusal.

    Cerulean is the point of truth, so this service keeps no copy of who may call
    it: every request is adjudicated by asking the owner. A key Cerulean does not
    know (revoked, never minted, wrong estate) raises `CeruleanError`.
    """
    _add_cerulean_to_path()
    from cerulean import Cerulean, CeruleanError  # noqa: E402

    client = Cerulean(key=presented)
    try:
        return client.whoami()
    except CeruleanError as exc:
        # Rather than refusing service until Cerulean is upgraded, ask the
        # strongest question an older bridge can answer — is this key live, and
        # does Cerulean let it touch domains — and say plainly that the scopes
        # were not checked. A key the bridge does not know still fails here, so
        # the fallback admits nobody Cerulean refuses.
        if not bridge_has_no_whoami(str(exc)):
            raise
    client.domains()
    log("warning: Cerulean has no /service/whoami — verified the key by its domain "
        "authority instead of its scopes; deploy the whoami route for strict checks")
    return {"name": "cerulean-key", "verified": "domains",
            "scopes": [PROVISION_SCOPE, READ_SCOPE]}


def service_authorize(authorization: str, scope: str) -> tuple[dict | None, int, str]:
    """(client, status, error) for a machine request that needs `scope`.

    No credential is a 401, a key Cerulean does not recognise is a 401, and a key
    Cerulean does recognise but without the scope is a 403. When Cerulean cannot
    be reached this **refuses** (503) rather than falling back to anything local:
    without the trust owner there is no authority to check against. A browser
    session is a different principal, so there is deliberately no fallback to it.
    """
    _add_cerulean_to_path()
    from cerulean import CeruleanError  # noqa: E402

    presented = bearer(authorization)
    if not presented:
        return None, 401, "missing service token (Authorization: Bearer ceru_…)"
    if not presented.startswith("ceru_"):
        return None, 401, "not a Cerulean service key"
    try:
        identity = cerulean_identity(presented)
    except CeruleanError:
        return None, 401, "Cerulean does not recognise this service key"
    except (urllib.error.URLError, OSError) as exc:
        log(f"Cerulean unreachable while verifying a service key: {exc}")
        return None, 503, "Cerulean is unreachable — refusing to act without the trust owner"
    except Exception as exc:  # noqa: BLE001 - nothing is admitted unverified
        # Anything else means the key could not be checked, and an unchecked key is
        # refused. It must not surface as a client error: the caller's request may
        # be perfectly good while the trust owner is the thing misbehaving.
        log(f"could not verify a service key with Cerulean: {type(exc).__name__}: {exc}")
        return None, 503, "Cerulean could not confirm this key — refusing to act without the trust owner"
    if not scope_allows(identity.get("scopes") or [], scope):
        return None, 403, f"this key is not scoped for {scope} in Cerulean"
    return identity, 200, ""


# ── Cerulean Vault ──────────────────────────────────────────────────────────
def vault() -> dict:
    if not VAULT_TOKEN_FILE.is_file():
        return {}
    token = VAULT_TOKEN_FILE.read_text(encoding="utf-8").strip()
    req = urllib.request.Request(f"{VAULT_ADDR}/v1/{VAULT_KV}")
    req.add_header("X-Vault-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("data", {}).get("data", {})
    except (urllib.error.URLError, ValueError):
        return {}


# ── Authentik admin API ─────────────────────────────────────────────────────
class Authentik:
    def __init__(self) -> None:
        v = vault()
        self.token = os.environ.get("AUTHENTIK_TOKEN") or v.get("authentik_token")
        self.base = (os.environ.get("AUTHENTIK_API_URL") or v.get("authentik_api_url")
                     or "http://192.168.1.71:9000").rstrip("/")
        if not self.token:
            raise RuntimeError("no Authentik token (Cerulean Vault or AUTHENTIK_TOKEN)")

    def call(self, path: str, method: str = "GET", body: dict | None = None):
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            raise RuntimeError(f"authentik {method} {path}: HTTP {exc.code} {detail}")

    def users(self) -> list[dict]:
        return self.call("/api/v3/core/users/?page_size=500")["results"]

    def user(self, email: str) -> dict | None:
        want = email.lower()
        return next((u for u in self.users()
                     if (u.get("email") or "").lower() == want), None)

    def groups(self) -> list[dict]:
        return self.call("/api/v3/core/groups/?page_size=500")["results"]

    def group_ids(self, names: list[str]) -> list[int]:
        catalog = {g["name"].lower(): g["pk"] for g in self.groups()}
        ids = []
        for name in names:
            hit = catalog.get(name.lower())
            if hit is None:
                raise RuntimeError(f"Authentik group {name!r} does not exist")
            ids.append(hit)
        return ids

    def user_groups(self, user: dict) -> list[str]:
        return [g["name"] for g in (user.get("groups_obj") or [])]

    def create_user(self, username: str, email: str, name: str, group_ids: list[int]) -> dict:
        return self.call("/api/v3/core/users/", "POST", {
            "username": username, "email": email, "name": name or username,
            "is_active": True, "path": "users", "attributes": {}, "groups": group_ids})

    def set_groups(self, pk: int, group_ids: list[int]) -> None:
        self.call(f"/api/v3/core/users/{pk}/", "PATCH", {"groups": group_ids})

    def set_active(self, pk: int, active: bool) -> None:
        self.call(f"/api/v3/core/users/{pk}/", "PATCH", {"is_active": active})

    def set_password(self, pk: int, password: str) -> None:
        self.call(f"/api/v3/core/users/{pk}/set_password/", "POST", {"password": password})

    def delete_user(self, pk: int) -> None:
        self.call(f"/api/v3/core/users/{pk}/", "DELETE")

    def client_secret(self, client_id: str) -> str:
        for p in self.call("/api/v3/providers/oauth2/?page_size=500")["results"]:
            if p.get("client_id") == client_id and p.get("client_secret"):
                return p["client_secret"]
        raise RuntimeError(f"no OAuth2 provider with client_id {client_id!r}")


# ── Stalwart management API ─────────────────────────────────────────────────
def stalwart_admin() -> tuple[str, str]:
    for raw in STALWART_ENV.read_text(encoding="utf-8").splitlines():
        if raw.strip().startswith("STALWART_RECOVERY_ADMIN="):
            user, _, password = raw.strip().split("=", 1)[1].partition(":")
            return user, password
    raise RuntimeError(f"no STALWART_RECOVERY_ADMIN in {STALWART_ENV}")


class Stalwart:
    def __init__(self) -> None:
        user, password = stalwart_admin()
        self.auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        req = urllib.request.Request(f"{STALWART_BASE}/.well-known/jmap")
        req.add_header("Authorization", f"Basic {self.auth}")
        with urllib.request.urlopen(req, timeout=20) as r:
            self.api = json.load(r).get("apiUrl") or f"{STALWART_BASE}/api"

    def call(self, method: str, payload: dict | None = None) -> dict:
        body = {"using": USING, "methodCalls": [[method, payload or {}, "c"]]}
        req = urllib.request.Request(self.api, data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", f"Basic {self.auth}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.load(r)["methodResponses"][0][1]
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method}: HTTP {exc.code} {exc.read().decode()[:200]}")
        if out.get("type") == "error":
            raise RuntimeError(f"{method}: {out.get('type')} {out.get('description')}")
        return out

    def domains(self) -> list[dict]:
        return self.call("x:Domain/get").get("list") or []

    def domain(self, name: str) -> dict | None:
        return next((d for d in self.domains()
                     if (d.get("name") or "").lower() == name.lower()), None)

    def accounts(self) -> list[dict]:
        return self.call("x:Account/get").get("list") or []

    def account(self, address: str) -> dict | None:
        return next((a for a in self.accounts()
                     if (a.get("emailAddress") or "").lower() == address.lower()), None)

    def domain_id(self, domain: str) -> str:
        dom = self.domain(domain)
        if not dom:
            raise RuntimeError(f"{domain}: no such domain in Stalwart — add it first")
        return dom["id"]

    def create_mailbox(self, address: str, description: str | None) -> dict:
        local, _, domain = address.partition("@")
        record = {"@type": "User", "name": local, "domainId": self.domain_id(domain),
                  "roles": {"@type": "User"}, "permissions": {"@type": "Inherit"}}
        if description:
            record["description"] = description
        out = self.call("x:Account/set", {"create": {"n": record}})
        made = out.get("created") or {}
        if not made:
            raise RuntimeError("stalwart create failed: "
                               + json.dumps(out.get("notCreated") or out)[:300])
        return made["n"]

    def destroy_account(self, account_id: str) -> None:
        out = self.call("x:Account/set", {"destroy": [account_id]})
        if out.get("notDestroyed"):
            raise RuntimeError("stalwart destroy failed: "
                               + json.dumps(out["notDestroyed"])[:300])

    # ── domains ─────────────────────────────────────────────────────────
    def create_domain(self, name: str, description: str | None) -> dict:
        dkim = None
        for dom in self.domains():
            if (dom.get("dkimManagement") or {}).get("@type") == "Automatic":
                dkim = dom["dkimManagement"]
                break
        if dkim is None:
            dkim = {"@type": "Automatic",
                    "algorithms": {"Dkim1Ed25519Sha256": True, "Dkim1RsaSha256": True},
                    "selectorTemplate": "v{version}-{algorithm}-{date-%Y%m%d}"}
        dns_server = next((s for s in self.call("x:DnsServer/get").get("list") or []), None)
        provider = None
        for dom in self.domains():
            cert = dom.get("certificateManagement") or {}
            if cert.get("@type") == "Automatic" and cert.get("acmeProviderId"):
                provider = cert["acmeProviderId"]
                break
        record: dict = {
            "name": name, "isEnabled": True, "description": description,
            "dkimManagement": dkim, "subAddressing": {"@type": "Enabled"},
            "allowRelaying": False, "reportAddressUri": "mailto:postmaster",
        }
        if provider:
            record["certificateManagement"] = {"acmeProviderId": provider,
                                                "subjectAlternativeNames": {},
                                                "@type": "Automatic"}
        else:
            record["certificateManagement"] = {"@type": "Manual"}
        if dns_server:
            record["dnsManagement"] = {"dnsServerId": dns_server["id"], "origin": None,
                                       "publishRecords": {"dkim": True, "spf": True,
                                                          "dmarc": True},
                                       "@type": "Automatic"}
        out = self.call("x:Domain/set", {"create": {"n": record}})
        made = out.get("created") or {}
        if not made:
            raise RuntimeError("domain create failed: "
                               + json.dumps(out.get("notCreated") or out)[:300])
        return made["n"]

    def destroy_domain(self, name: str) -> None:
        dom = self.domain(name)
        if not dom:
            raise RuntimeError(f"{name}: no such domain")
        sigs = [s["id"] for s in self.call("x:DkimSignature/get").get("list") or []
                if s.get("domainId") == dom["id"]]
        if sigs:
            self.call("x:DkimSignature/set", {"destroy": sigs})
        out = self.call("x:Domain/set", {"destroy": [dom["id"]]})
        if not out.get("destroyed"):
            raise RuntimeError("domain destroy failed: "
                               + json.dumps(out.get("notDestroyed") or out)[:300])


# ── OIDC ────────────────────────────────────────────────────────────────────
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_dec(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Oidc:
    def __init__(self, authentik: Authentik) -> None:
        self.authentik = authentik
        self._meta: dict | None = None
        self._fetched = 0.0
        self._secret = ""

    @property
    def issuer(self) -> str:
        return ISSUER.rstrip("/") + "/"

    def metadata(self) -> dict:
        if self._meta and time.time() - self._fetched < 600:
            return self._meta
        url = self.issuer + ".well-known/openid-configuration"
        with urllib.request.urlopen(url, timeout=15) as r:
            self._meta = json.load(r)
        self._fetched = time.time()
        return self._meta

    def secret(self) -> str:
        if CLIENT_SECRET:
            return CLIENT_SECRET
        if not self._secret:
            self._secret = self.authentik.client_secret(CLIENT_ID)
        return self._secret

    def verify(self, id_token: str, nonce: str) -> dict:
        if rsa is None:
            raise RuntimeError("cryptography is not installed; cannot verify id_token")
        header_b64, payload_b64, sig_b64 = id_token.split(".")
        header = json.loads(b64url_dec(header_b64))
        if header.get("alg") != "RS256":
            raise RuntimeError(f"refusing id_token with alg={header.get('alg')!r}")
        with urllib.request.urlopen(self.metadata()["jwks_uri"], timeout=15) as r:
            jwks = json.load(r)
        key = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")), None)
        if key is None:
            raise RuntimeError("id_token signing key not found in JWKS")
        n = int.from_bytes(b64url_dec(key["n"]), "big")
        e = int.from_bytes(b64url_dec(key["e"]), "big")
        pub = rsa.RSAPublicNumbers(e, n).public_key()
        pub.verify(b64url_dec(sig_b64), f"{header_b64}.{payload_b64}".encode(),
                   asym_padding.PKCS1v15(), hashes.SHA256())
        claims = json.loads(b64url_dec(payload_b64))
        now = time.time()
        if claims.get("iss") not in (self.issuer, self.issuer.rstrip("/")):
            raise RuntimeError(f"id_token issuer {claims.get('iss')!r} != {self.issuer!r}")
        aud = claims.get("aud")
        aud = aud if isinstance(aud, list) else [aud]
        if CLIENT_ID not in aud:
            raise RuntimeError("id_token audience does not include this client")
        if float(claims.get("exp", 0)) < now:
            raise RuntimeError("id_token has expired")
        if nonce and claims.get("nonce") != nonce:
            raise RuntimeError("id_token nonce mismatch")
        return claims

    def userinfo(self, access_token: str) -> dict:
        req = urllib.request.Request(self.metadata()["userinfo_endpoint"])
        req.add_header("Authorization", f"Bearer {access_token}")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)

    def exchange(self, code: str, verifier: str) -> dict:
        body = urllib.parse.urlencode({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT_URI, "client_id": CLIENT_ID,
            "client_secret": self.secret(), "code_verifier": verifier}).encode()
        req = urllib.request.Request(self.metadata()["token_endpoint"], data=body)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)

    def authorize_url(self, state: str, nonce: str, challenge: str) -> str:
        return self.metadata()["authorization_endpoint"] + "?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
            "scope": "openid profile email", "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256"})


# ── session cookie ──────────────────────────────────────────────────────────
def sign(payload: bytes) -> str:
    mac = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
    return f"{b64url(payload)}.{b64url(mac)}"


def unsign(token: str) -> dict | None:
    try:
        body, mac = token.split(".", 1)
        payload = b64url_dec(body)
        expected = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, b64url_dec(mac)):
            return None
        data = json.loads(payload)
    except (ValueError, KeyError):
        return None
    return data if data.get("exp", 0) > time.time() else None


STATE = {"authentik": None, "stalwart": None, "oidc": None}
STATE_LOCK = threading.Lock()


def services() -> tuple[Authentik, Stalwart, Oidc]:
    with STATE_LOCK:
        if STATE["stalwart"] is None:
            STATE["authentik"] = Authentik()
            STATE["stalwart"] = Stalwart()
            STATE["oidc"] = Oidc(STATE["authentik"])
        return STATE["authentik"], STATE["stalwart"], STATE["oidc"]


def reset_services() -> None:
    with STATE_LOCK:
        STATE.update(authentik=None, stalwart=None, oidc=None)


# ── operations ──────────────────────────────────────────────────────────────
def list_state() -> dict:
    ak, sw, _ = services()
    accounts = {(a.get("emailAddress") or "").lower(): a for a in sw.accounts()
                if a.get("@type") == "User"}
    users = []
    for user in ak.users():
        email = (user.get("email") or "").lower()
        groups = ak.user_groups(user)
        mailbox = accounts.get(email)
        if email and ("@" in email) and (mailbox or DEFAULT_MAIL_GROUP in groups):
            users.append({"email": email, "username": user.get("username"),
                          "name": user.get("name"), "active": user.get("is_active"),
                          "groups": groups, "mailbox": bool(mailbox),
                          "accountId": (mailbox or {}).get("id")})
    users.sort(key=lambda u: u["email"])
    domains = sorted(({"name": d.get("name"), "id": d.get("id"),
                       "enabled": d.get("isEnabled"),
                       "description": d.get("description")} for d in sw.domains()),
                     key=lambda d: d["name"] or "")
    return {"domains": domains, "users": users,
            "mailDomains": [d["name"] for d in domains]}


def sign_in_name(email: str, taken: set) -> str:
    """A sign-in name no other identity already holds.

    A mailbox's localpart is unique only *inside* its domain — every mail domain
    is expected to answer `postmaster`, and this estate already has one — while an
    Authentik username is global. So when the bare localpart belongs to somebody
    else, the name is the whole address: unique because the address is, and the
    same account a retry finds (it is found by email, not by name).

    Without this the second tenant whose first mailbox is `postmaster@` cannot be
    provisioned at all: it dies on Authentik's own "This field must be unique"
    for a name the mail host chose, not one the caller asked for.
    """
    address = email.strip().lower()
    local = address.split("@", 1)[0]
    have = {str(name).strip().lower() for name in taken or []}
    return local if local not in have else address


def add_user(email: str, name: str, groups: list[str], rotate: bool = True) -> dict:
    """Create or extend one account on both planes.

    `rotate` writes a fresh Authentik password and returns it, and defaults to
    True because that is what the page means by **Add user**: an operator asking
    for an account wants a credential. A retried provisioning call passes False —
    the account already has a password the caller was handed once, and minting a
    second one would silently invalidate the first.

    Nothing already here is created twice: an existing Authentik user keeps its
    identity and has the requested groups *merged* into it, and an existing
    mailbox is left alone. `created` says whether this call actually made
    something that was not there.
    """
    ak, sw, _ = services()
    email = email.strip().lower()
    local, _, domain = email.partition("@")
    if not local or not domain:
        raise RuntimeError("email must be localpart@domain")
    if not sw.domain(domain):
        raise RuntimeError(f"{domain} is not a mail domain here — add the domain first")
    wanted = [DEFAULT_MAIL_GROUP] + [g for g in groups if g and g != DEFAULT_MAIL_GROUP]
    user = ak.user(email)
    result: dict = {"email": email, "created": not user}
    if user:
        merged = sorted(set(ak.user_groups(user)) | set(wanted))
        ak.set_groups(user["pk"], ak.group_ids(merged))
        if not user.get("is_active"):
            ak.set_active(user["pk"], True)
        result["authentik"] = f"updated groups={merged}"
        result["groups"] = merged
    else:
        created = ak.create_user(sign_in_name(email, {u.get("username") for u in ak.users()}),
                                 email, name or local, ak.group_ids(wanted))
        user = created
        result["authentik"] = f"created pk={created.get('pk')}"
        result["groups"] = sorted(ak.user_groups(user)) or wanted
    if not sw.account(email):
        box = sw.create_mailbox(email, name)
        result["mailbox"] = f"created id={box.get('id')}"
        result["created"] = True
    else:
        result["mailbox"] = "already present"
    result["password"] = None
    if rotate:
        password = secrets.token_urlsafe(18)
        ak.set_password(user["pk"], password)
        result["password"] = password
    return result


def remove_user(email: str) -> dict:
    ak, sw, _ = services()
    email = email.strip().lower()
    user = ak.user(email)
    box = sw.account(email)
    result: dict = {"email": email}
    if user:
        if user.get("username") in ("akadmin",) or email == "admin@innotel.us":
            raise RuntimeError("refusing to remove the estate administrator")
        ak.set_active(user["pk"], False)
        result["authentik"] = "deactivated (sign-in revoked)"
    if box:
        sw.destroy_account(box["id"])
        result["mailbox"] = "destroyed"
    if not user and not box:
        raise NotFound(f"{email}: no such user")
    return result


def delegate_zone(name: str, key: str = "") -> dict:
    """Make `name` resolve, delegating it through Cerulean wherever Cerulean can

    Cerulean owns the estate resolver, so delegation is its call, not the
    operator's:

    * A name inside a zone Cerulean already serves is delegated into that parent
      automatically — the child's NS records are written into the parent zone, so
      the name is delegated by the resolver itself rather than by hand. This is
      the common case: `signara.innotel.us` sits inside `innotel.us`
    * A name at the apex of a domain nobody has delegated yet still needs its
      registrar pointed at the estate nameservers. No registrar API exists in
      this estate, so that one step is reported with the exact servers to use —
      it is a registration act, not something the mail service can perform
    """
    _add_cerulean_to_path()
    from cerulean import Cerulean, CeruleanError  # noqa: E402

    name = name.strip().lower().rstrip(".")
    client = cerulean_client(key)
    served = sorted((str(d.get("name") or "").lower() for d in client.domains() if d.get("name")),
                    key=len, reverse=True)
    parent = next((z for z in served if z != name and name.endswith("." + z)), None)
    if not parent:
        return {"mode": "registrar", "nameservers": PUBLIC_NS,
                "action": (f"point {name}'s NS at {' and '.join(PUBLIC_NS)} at the "
                           "registrar — no registrar API exists here, so this is the "
                           "one step the mail service cannot do")}

    def in_parent(record: dict) -> bool:
        n = str(record.get("name") or "").lower().rstrip(".")
        return n == parent or n.endswith("." + parent)

    # The bridge ignores a zone it does not serve on read, so keep only rows that
    # really are the parent's before trusting any of them
    parent_rows = [r for r in client.records(parent) if in_parent(r)]
    targets = [str(r.get("value") or "").lower().rstrip(".")
               for r in parent_rows
               if str(r.get("type") or "").upper() == "NS"
               and str(r.get("name") or "").lower().rstrip(".") == parent]
    targets = [t for t in dict.fromkeys(targets) if t] or list(PUBLIC_NS)
    have = {str(r.get("value") or "").lower().rstrip(".")
            for r in parent_rows
            if str(r.get("type") or "").upper() == "NS"
            and str(r.get("name") or "").lower().rstrip(".") == name}
    added = []
    for target in targets:
        if target in have:
            continue
        try:
            client.add_record(parent, "NS", name, target, ttl=3600)
            added.append(target)
        except CeruleanError as exc:
            return {"mode": "estate", "parent": parent, "nameservers": targets,
                    "error": str(exc)[:200]}
    return {"mode": "estate", "parent": parent, "nameservers": targets, "added": len(added),
            "detail": f"delegated inside {parent} ({' '.join(targets)})"}


def cerulean_client(key: str = ""):
    """A Cerulean bridge client using `key`, or the platform's own when empty.

    Empty is not "no key": `cerulean.py` then resolves the platform's key from
    `CERULEAN_SERVICE_KEY`, the key file, or Cerulean Vault — the estate's normal
    path for this host. A caller's key is only used when Cerulean itself says the
    caller may make the write, so the estate's authority is never widened by a
    passed-through credential.
    """
    _add_cerulean_to_path()
    from cerulean import Cerulean  # noqa: E402

    return Cerulean(key=key or None)


def create_zone(name: str, key: str = "") -> dict:
    """Register this mail domain with Cerulean, which mints its authoritative zone

    Cerulean (TrustOps) owns the estate resolver: it runs Technitium and is the
    only principal allowed to create zones and write records into it. Stalwart's
    own DNS publisher is switched off, so a new mail domain must have its zone
    created here first — otherwise there is nothing for DKIM/SPF/DMARC to be
    published into, and the name never resolves. Idempotent: a domain Cerulean
    already serves comes back with ``created: False`` rather than an error
    """
    _add_cerulean_to_path()
    from cerulean import Cerulean, CeruleanError  # noqa: E402

    name = name.strip().lower().rstrip(".")
    if not name or "." not in name:
        raise RuntimeError("that is not a domain name")
    try:
        result = cerulean_client(key).ensure_zone(name)
    except CeruleanError as exc:
        raise RuntimeError(f"Cerulean could not create the zone: {exc}")
    result["resolver"] = "Cerulean (Technitium)"
    try:
        result["delegation"] = delegate_zone(name, key)
    except Exception as exc:  # noqa: BLE001 - the zone exists either way
        result["delegation"] = {"error": str(exc)[:200]}
    return result


def publish_dns(name: str, key: str = "") -> dict:
    """Write this domain's generated DKIM/SPF/DMARC (and friends) through Cerulean

    Stalwart renders the zone but its own publisher is switched off — Cerulean is
    the only principal allowed to write the estate's resolver. Nothing is written
    into a zone the resolver does not already serve: a brand-new domain reports
    that its zone must be created first rather than inventing records for a name
    that does not resolve
    """
    _add_cerulean_to_path()
    import cerulean_publish_zone as cz  # noqa: E402
    from cerulean import Cerulean, CeruleanError  # noqa: E402

    _, sw, _ = services()
    name = name.strip().lower()
    dom = sw.domain(name)
    if not dom:
        raise RuntimeError(f"{name}: no such domain")
    text = dom.get("dnsZoneFile") or ""
    if not text.strip():
        raise RuntimeError(f"{name}: Stalwart has not rendered a zone for this domain")

    client = cerulean_client(key)
    try:
        live_rows = client.records(name)
    except CeruleanError as exc:
        # A refused read is not a missing zone. Reporting "the resolver does not
        # serve this zone" sends an operator off to create a zone that already
        # exists, when the fault is this key's scopes: publishing *reads* the zone
        # before it writes it, so a key scoped `dns:write` alone cannot do it.
        return {"domain": name,
                "skipped": f"Cerulean could not be asked for the zone: {exc}"}

    def belongs(record_name) -> bool:
        n = str(record_name or "").lower().rstrip(".")
        return n == name or n.endswith("." + name)

    # This host does not send to the internet itself: the ISP blocks port 25, so
    # Stalwart's outbound route hands mail to the relay (`mailjet`) and the estate
    # SPF names that include. Stalwart renders a domain's own apex SPF as
    # `v=spf1 mx -all`, which authorises this host and *not* the relay — so a
    # tenant provisioned from that template would fail SPF at the recipient. The
    # include is therefore added when the record is first written; a zone that
    # already has an SPF keeps it (an operator's record is never rewritten), and
    # an empty SPF_RELAY_INCLUDE turns the rewrite off.
    # Set and empty means off, the way this service spells "disabled" everywhere
    # else (MAIL_SERVICE_CLIENTS, MAIL_PROVISION_URL): unset takes the estate's
    # relay, a value names a different one, and "" leaves Stalwart's record alone.
    relay = os.environ.get("SPF_RELAY_INCLUDE", "spf.mailjet.com").strip()

    def relay_aware(record: dict) -> dict:
        """Name the relay in a rendered SPF, so outbound mail authorises it."""
        value = str(record.get("value") or "")
        if not relay or record.get("type") != "TXT" or not value.lower().startswith("v=spf1"):
            return record
        terms = value.split()
        if any(relay in term for term in terms[1:]):
            return record
        return {**record, "value": " ".join([terms[0], f"include:{relay}", *terms[1:]])}

    # The bridge ignores the ?zone= filter on read and answers with the zone it
    # does serve, so an unserved domain looks like it has records. Only publish
    # when every record that came back really is inside this zone; otherwise the
    # zone does not exist yet and writing here would mint a private zone that DNS
    # never delegates to (and that nothing will ever clean up)
    if not live_rows or not any(belongs(r.get("name")) for r in live_rows):
        return {"domain": name, "skipped": "the resolver does not serve this zone — "
                "create the zone first (Cerulean mints it), then publish"}
    live_rows = [r for r in live_rows if belongs(r.get("name"))]

    live: dict[tuple[str, str], set[str]] = {}
    for r in live_rows:
        key = (str(r.get("name", "")).lower().rstrip("."), str(r.get("type", "")).upper())
        live.setdefault(key, set()).add(re.sub(r"\s+", " ", str(r.get("value", ""))).strip())

    added, exact = [], 0
    override = conflict = failed = 0
    for rec in cz.parse_zone(text, name):
        key = (rec["name"], rec["type"])
        present = live.get(key, set())
        rec = relay_aware(rec)
        if rec["value"] in present:
            exact += 1
            continue
        # An operator-owned record (apex SPF/DMARC/MX, the CAA issue grant) is kept
        # — but only a record that is actually there. A tenant whose zone Cerulean
        # just minted has no MX at all, and deferring to a record that does not
        # exist would leave the domain unable to receive the mail it was built for.
        kind = cz.placement(rec, present)
        if kind == "override":
            override += 1
            continue
        if kind == "singleton" and present:
            conflict += 1
            continue
        try:
            client.add_record(name, rec["type"], rec["name"], rec["value"], ttl=3600)
            live.setdefault(key, set()).add(rec["value"])
            added.append({"type": rec["type"], "name": rec["name"], "value": rec["value"][:90]})
        except CeruleanError as exc:
            failed += 1
            added.append({"type": rec["type"], "name": rec["name"], "failed": str(exc)[:120]})
    return {"domain": name, "added": len(added), "exact": exact, "override": override,
            "conflict": conflict, "failed": failed, "records": added[:25]}


def add_domain(name: str, description: str | None, existing_ok: bool = False,
               key: str = "") -> dict:
    """Create a mail domain: the Stalwart record, then its zone and its records.

    `existing_ok` turns this into an ensure — a domain already here is reported
    instead of refused, and the zone and record steps still run, because they are
    idempotent and self-healing (a domain whose zone was never minted gets one).
    Provisioning passes True; the page's **Add domain** passes False, where a
    duplicate name is a mistake worth surfacing.

    `key` is the Cerulean credential the zone and record writes are made with —
    empty means the platform's own from the vault (see `cerulean_client`).
    """
    _, sw, _ = services()
    name = name.strip().lower()
    if not name or "." not in name:
        raise RuntimeError("that is not a domain name")
    existing = sw.domain(name)
    if existing and not existing_ok:
        raise RuntimeError(f"{name} already exists in Stalwart")
    made, created = (existing, False) if existing else (sw.create_domain(name, description), True)
    after = sw.domain(name)
    result = {"domain": name, "created": created, "id": made.get("id"),
              "enabled": bool(after and after.get("isEnabled")),
              "next": [f"MX at {MX_HOST} if this domain receives mail"]}
    # Mint the authoritative zone through Cerulean before publishing: Cerulean
    # owns the resolver, and DKIM/SPF/DMARC have nowhere to go until the zone
    # exists. Both steps are idempotent, so re-running add is safe
    try:
        result["zone"] = create_zone(name, key)
    except Exception as exc:  # noqa: BLE001 - the domain is created either way
        result["zone"] = {"error": str(exc)[:200]}
    try:
        result["dns"] = publish_dns(name, key)
    except Exception as exc:  # noqa: BLE001 - the domain is created either way
        result["dns"] = {"error": str(exc)[:200]}
    return result


def delete_zone(name: str) -> dict:
    """Unregister this domain with Cerulean, which removes its authoritative zone

    The counterpart of creating the zone: leaving it behind keeps the name
    answering (and its DKIM/SPF/DMARC live) after the domain it was for is gone,
    which nothing would ever clean up. Idempotent — a zone Cerulean does not
    serve reports ``deleted: False`` rather than failing. Cerulean refuses its
    own platform zones outright
    """
    _add_cerulean_to_path()
    from cerulean import Cerulean, CeruleanError  # noqa: E402

    name = name.strip().lower().rstrip(".")
    if not name or "." not in name:
        raise RuntimeError("that is not a domain name")
    client = Cerulean()
    try:
        result = client.delete_zone(name)
    except CeruleanError as exc:
        raise RuntimeError(f"Cerulean could not remove the zone: {exc}")
    result["resolver"] = "Cerulean (Technitium)"
    # A child zone was delegated into its parent by `delegate_zone`; removing the
    # zone has to take that delegation with it, or the parent keeps pointing at a
    # name that no longer answers
    served = sorted((str(d.get("name") or "").lower() for d in client.domains() if d.get("name")),
                    key=len, reverse=True)
    parent = next((z for z in served if z != name and name.endswith("." + z)), None)
    if parent:
        try:
            rows = [r for r in client.records(parent)
                    if str(r.get("name") or "").lower().rstrip(".") == name
                    and str(r.get("type") or "").upper() == "NS"]
            for row in rows:
                client.delete_record(parent, "NS", name, str(row.get("value") or ""))
            result["delegation"] = {"parent": parent, "removed": len(rows)}
        except CeruleanError as exc:
            result["delegation"] = {"parent": parent, "error": str(exc)[:200]}
    return result


def remove_domain(name: str) -> dict:
    _, sw, _ = services()
    name = name.strip().lower()
    if name in PROTECTED_ZONES:
        raise RuntimeError(f"refusing to remove {name}")
    if not sw.domain(name):
        raise NotFound(f"{name}: no such domain")
    sw.destroy_domain(name)
    result: dict = {"domain": name, "destroyed": True}
    try:
        result["zone"] = delete_zone(name)
    except Exception as exc:  # noqa: BLE001 - the domain is gone either way
        result["zone"] = {"error": str(exc)[:200]}
    return result


# One lock per tenant, so two requests for the same domain cannot interleave and
# build it twice. In-process only, which is enough: this service is the single
# writer on its host.
TENANT_LOCKS: dict[str, threading.Lock] = {}
TENANT_LOCKS_GUARD = threading.Lock()


def tenant_lock(key: str) -> threading.Lock:
    with TENANT_LOCKS_GUARD:
        return TENANT_LOCKS.setdefault(key, threading.Lock())


def provision_tenant(domain: str, email: str, name: str = "", groups: list[str] | None = None,
                     description: str | None = None, key: str = "") -> dict:
    """Create a tenant — its mail domain and its first account — in **one write**.

    Called by **Cerulean**, which is where an application integrates: Cerulean has
    already registered the tenant and minted its zone by the time this runs, and
    this call leaves the estate with the rest — the Stalwart domain with its DKIM
    keys, the generated DKIM/SPF/DMARC published into the Cerulean zone, the
    delegation, one Authentik identity in the mail groups, and the matching
    Stalwart mailbox. The caller does not hold the Stalwart recovery admin or the
    Authentik bootstrap token, and nothing secret is written down here.

    `key` is the Cerulean credential the DNS writes are made with: the caller's
    own key when Cerulean says it may write records, otherwise empty, which means
    the platform's own key from the vault. `authority` in the answer says which,
    so a caller is never left guessing whose authority acted on its behalf.

    It is **idempotent**, because the callers are webhooks: a retry must not leave
    a second domain, a second mailbox, or — worse — a second password. Everything
    is created only when absent, an existing identity has the requested groups
    merged into it rather than replaced, and the password is minted only by the
    call that creates the account: a later call answers `password: null` with
    `existed: true`, which is how a caller tells a first delivery from a retry.
    Two requests for the same domain in flight at once are serialised, so neither
    sees a half-built tenant.

    The one deliberate limit: a retry cannot hand back a password it already
    handed out, so a caller that loses the first answer should rotate one
    (`POST /api/service/accounts/password`) rather than re-provision.
    """
    ak, sw, _ = services()
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain or "." not in domain:
        raise RuntimeError("that is not a domain name")
    if domain in PROTECTED_ZONES:
        raise RuntimeError(f"refusing to provision {domain}: it is a protected zone")
    address = (email or "").strip().lower()
    local, _, mail_domain = address.partition("@")
    if not local or not mail_domain:
        raise RuntimeError("email must be localpart@domain")
    if mail_domain != domain:
        raise RuntimeError(f"{address} is not at {domain} — a tenant's first account "
                           "has to live in the domain being created")

    with tenant_lock(domain):
        # "Existed" is all three records already agreeing. Checked before the
        # writes, so it describes the state the caller's request found.
        existed = bool(sw.domain(domain)) and bool(ak.user(address)) and bool(sw.account(address))
        made = add_domain(domain, description, existing_ok=True, key=key)
        account = add_user(address, name, groups or [], rotate=not existed)
        authority = "caller's Cerulean key" if key else "platform Cerulean key"
        log(f"provision {domain} + {address} (existed={existed}, dns via {authority})")
        return {"tenant": {"domain": domain, "id": made.get("id")},
                "domain": made, "account": account, "existed": existed,
                "authority": authority, "password": account.get("password")}


def rotate_password(email: str) -> dict:
    ak, _, _ = services()
    user = ak.user(email.strip().lower())
    if not user:
        raise NotFound(f"{email}: no such user")
    password = secrets.token_urlsafe(18)
    ak.set_password(user["pk"], password)
    return {"email": email, "password": password}


# ── HTTP ────────────────────────────────────────────────────────────────────
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Oasis Mail Admin</title>
<style>
:root{--bg:#0b1020;--panel:#141a2e;--ink:#e8ecff;--muted:#94a0c8;--line:#26304d;
--accent:#6366f1;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
header{padding:20px 24px;border-bottom:1px solid var(--line);display:flex;
align-items:center;gap:12px;justify-content:space-between}
h1{font-size:18px;margin:0}.crown{color:var(--accent)}
main{padding:24px;max-width:1080px;margin:0 auto}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:18px;margin-bottom:22px}
h2{font-size:15px;margin:0 0 12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600}
form{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
input,select{background:#0e1426;border:1px solid var(--line);color:var(--ink);
border-radius:8px;padding:9px 11px;font:inherit}
input[type=text],input[type=email]{min-width:220px}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:9px 14px;
font:inherit;cursor:pointer}button.ghost{background:#243052}
button.bad{background:var(--bad)}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;
background:#243052;color:var(--muted)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
#toast{margin-top:12px;min-height:20px;white-space:pre-wrap}
code{background:#0e1426;padding:2px 6px;border-radius:6px}
.small{color:var(--muted);font-size:13px}
</style></head><body>
<header><div><h1><span class="crown">&#9819;</span> Oasis Mail Admin</h1>
<div class="small" id="who"></div></div>
<a href="/auth/logout"><button class="ghost">Sign out</button></a></header>
<main>
<section><h2>Domains</h2>
<table><thead><tr><th>Domain</th><th>Status</th><th>Description</th><th></th></tr></thead>
<tbody id="domains"></tbody></table>
<form id="domainForm">
<input type="text" name="name" placeholder="example.com" required>
<input type="text" name="description" placeholder="optional description">
<button type="submit">Add domain</button></form>
<div class="small">Adding a domain here creates it in Stalwart (with automatic DKIM
and a certificate), mints its authoritative zone through <b>Cerulean</b>, delegates
it, and publishes the DKIM/SPF/DMARC records. A name inside a domain the estate
already serves is delegated automatically; a brand-new apex needs its registrar
pointed at the estate nameservers, which no API here can do for you. Removing a
domain takes its zone and records with it.</div></section>
<section><h2>Users</h2>
<table><thead><tr><th>Email</th><th>Groups</th><th>Mailbox</th><th>Active</th><th></th></tr></thead>
<tbody id="users"></tbody></table>
<form id="userForm">
<input type="email" name="email" placeholder="jane@example.com" required>
<input type="text" name="name" placeholder="Jane Doe">
<input type="text" name="groups" placeholder="extra groups, comma separated">
<button type="submit">Add user</button></form>
<div class="small">A user is created in Authentik (with the groups below) and as a
Stalwart mailbox. The generated password is shown once.</div></section>
<section><h2>Tenant</h2>
<div class="small">The one write an application makes, from the console: the domain
with its zone, an identity, and the first mailbox, in a single call. <b>Add
domain</b> then <b>Add user</b> above is the same work in two steps, for a domain
that already exists.</div>
<form id="tenantForm">
<input type="text" name="domain" placeholder="example.com" required>
<input type="email" name="email" placeholder="ops@example.com" required>
<input type="text" name="name" placeholder="Acme Ops">
<button type="submit">Add tenant</button></form>
<div class="small">Creates the mail domain with its DKIM keys, mints and delegates
its zone through <b>Cerulean</b>, publishes its DKIM/SPF/DMARC, creates the
Authentik identity and the Stalwart mailbox, and shows the password once. The
first address has to be at the domain, and the DNS writes are made with the
platform's Cerulean key because a browser session is not a machine caller.</div></section>
<div id="toast"></div>
</main>
<script>
const CSRF=window.CSRF_TOKEN;
async function api(path,body,method){
  const res=await fetch(path,{method:method||'GET',
    headers:body?{'Content-Type':'application/json','X-CSRF':CSRF}:{},
    body:body?JSON.stringify(body):undefined,credentials:'same-origin'});
  const text=await res.text();let data;try{data=JSON.parse(text)}catch(e){data={raw:text}}
  if(!res.ok)throw new Error(data.error||res.status+' '+text.slice(0,200));
  return data;
}
function toast(msg,cls){const t=document.getElementById('toast');
  t.className=cls||'';t.textContent=msg}
function row(cells){const tr=document.createElement('tr');
  cells.forEach(c=>{const td=document.createElement('td');
    if(c instanceof Node)td.appendChild(c);else td.innerHTML=c;tr.appendChild(td)});return tr}
async function load(){
  const s=await api('/api/state');
  document.getElementById('who').textContent=s.user.email+' \\u2022 groups: '+s.user.groups.join(', ');
  const d=document.getElementById('domains');d.innerHTML='';
  s.domains.forEach(x=>{const b=document.createElement('button');b.className='bad';
    b.textContent='Remove';b.onclick=async()=>{if(!confirm('Remove '+x.name+' and its DNS zone?'))return;
      try{const r=await api('/api/domains',{action:'remove',name:x.name},'POST');
      toast('removed '+x.name+(r.zone?(r.zone.error?' (zone: '+r.zone.error+')':
        ' and its zone'):''),'ok');load()}catch(e){toast(e.message,'bad')}};
    const zone=document.createElement('button');zone.className='ghost';zone.textContent='Create zone';
    zone.style.marginRight='6px';zone.onclick=async()=>{try{const r=await api('/api/domains',
      {action:'zone',name:x.name},'POST');
      const d=r.delegation;
      toast('Zone '+r.domain+' '+(r.created?'created':'already present')+' via '+r.resolver+
        (d&&!d.error?(d.mode==='estate'?('; delegated inside '+d.parent):'; at your registrar: '+d.action):''),
        'ok');load()}catch(e){toast(e.message,'bad')}};
    const pub=document.createElement('button');pub.className='ghost';pub.textContent='Publish DNS';
    pub.style.marginRight='6px';pub.onclick=async()=>{try{const r=await api('/api/domains',
      {action:'publish',name:x.name},'POST');
      toast(JSON.stringify(r),'ok');load()}catch(e){toast(e.message,'bad')}};
    const acts=document.createElement('span');acts.appendChild(zone);acts.appendChild(pub);acts.appendChild(b);
    d.appendChild(row([x.name,'<span class="pill">'+(x.enabled?'enabled':'disabled')+'</span>',
      x.description||'',acts]))});
  const u=document.getElementById('users');u.innerHTML='';
  s.users.forEach(x=>{const b=document.createElement('button');b.className='bad';
    b.textContent='Remove';b.onclick=async()=>{if(!confirm('Remove '+x.email+'?'))return;
      try{await api('/api/users',{action:'remove',email:x.email},'POST');
      toast('removed '+x.email,'ok');load()}catch(e){toast(e.message,'bad')}};
    const p=document.createElement('button');p.className='ghost';p.textContent='Reset password';
    p.style.marginRight='6px';p.onclick=async()=>{try{const r=await api('/api/users/password',
      {email:x.email},'POST');toast('New password for '+r.email+': '+r.password,'ok')}
      catch(e){toast(e.message,'bad')}};
    const actions=document.createElement('span');actions.appendChild(p);actions.appendChild(b);
    u.appendChild(row([x.email,(x.groups||[]).join(', '),
      x.mailbox?'<span class="ok">yes</span>':'<span class="warn">no</span>',
      x.active?'<span class="ok">yes</span>':'<span class="bad">no</span>',actions]))});
}
function summary(r){const bits=[];
  if(r.zone){
    if(r.zone.error)bits.push('zone: '+r.zone.error);
    else{bits.push('zone '+(r.zone.created?'created':'already present')+' via '+r.zone.resolver);
      const d=r.zone.delegation;
      if(d&&!d.error)bits.push(d.mode==='estate'?('delegated inside '+d.parent):
        ('at your registrar: '+d.action));
      else if(d&&d.error)bits.push('delegation: '+d.error)}}
  if(r.dns){
    if(r.dns.error)bits.push('DNS: '+r.dns.error);
    else if(r.dns.skipped)bits.push('DNS skipped: '+r.dns.skipped);
    else bits.push('DNS '+r.dns.added+' record(s)')}
  return bits.join('; ')}
document.getElementById('domainForm').onsubmit=async e=>{e.preventDefault();
  const f=new FormData(e.target);
  try{const r=await api('/api/domains',{action:'add',name:f.get('name'),
    description:f.get('description')||null},'POST');
    toast('Added '+r.domain+' — '+summary(r),'ok');
    e.target.reset();load()}catch(err){toast(err.message,'bad')}};
document.getElementById('userForm').onsubmit=async e=>{e.preventDefault();
  const f=new FormData(e.target);
  try{const r=await api('/api/users',{action:'add',email:f.get('email'),name:f.get('name'),
    groups:(f.get('groups')||'').split(',').map(s=>s.trim()).filter(Boolean)},'POST');
    toast('Added '+r.email+'\\nPassword (shown once): '+r.password,'ok');
    e.target.reset();load()}catch(err){toast(err.message,'bad')}};
document.getElementById('tenantForm').onsubmit=async e=>{e.preventDefault();
  const f=new FormData(e.target);
  try{const r=await api('/api/tenants',{domain:f.get('domain'),email:f.get('email'),
    name:f.get('name')||null},'POST');
    toast('Tenant '+r.tenant.domain+'\\nmailbox: '+r.account.email+
      '\\nauthority: '+r.authority+(r.existed?'\\n(already here — no new password)':
      '\\nPassword (shown once): '+r.password),'ok');
    e.target.reset();load()}catch(err){toast(err.message,'bad')}};
load().catch(e=>toast(e.message,'bad'));
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "oasis-mail-admin"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    # ── helpers ──
    def _send(self, code: int, body: bytes, ctype: str = "application/json",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (extra or {}).items():
            # a value may be a list so several Set-Cookie headers can be sent
            for item in (v if isinstance(v, (list, tuple)) else [v]):
                self.send_header(k, item)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        # A reply sent *before* the request body was read has to end the connection:
        # HTTP/1.1 keeps it for the next request, and those unread bytes would be
        # parsed as the next request line and logged as a bogus "Bad request syntax",
        # which makes a clean refusal (401) read like a broken request.
        unread_body = self.headers.get("Content-Length") or self.headers.get("Transfer-Encoding")
        if unread_body and not getattr(self, "_body_consumed", False):
            self.close_connection = True

    def json(self, code: int, obj: dict, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json", extra)

    def html(self, code: int, text: str, extra: dict | None = None) -> None:
        self._send(code, text.encode(), "text/html; charset=utf-8", extra)

    def redirect(self, url: str, extra: dict | None = None) -> None:
        self._send(302, b"", "text/plain", {"Location": url, **(extra or {})})

    def body_json(self) -> dict:
        self._body_consumed = True
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def session(self) -> dict | None:
        jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        morsel = jar.get(COOKIE)
        return unsign(morsel.value) if morsel else None

    def require_session(self) -> dict | None:
        session = self.session()
        if not session:
            self.json(401, {"error": "not signed in"})
            return None
        return session

    def require_csrf(self, session: dict) -> bool:
        if not hmac.compare_digest(self.headers.get("X-CSRF") or "", session.get("csrf", "")):
            self.json(403, {"error": "bad CSRF token"})
            return False
        return True

    # ── machine surface (Cerulean) ──
    def service_caller_allowed(self) -> bool:
        """Second lock: only the estate's trust owner may reach this surface.

        Cerulean already adjudicates every credential; this keeps the surface off
        the internet even for a caller holding a valid key, so mail provisioning
        is not something an application can do behind Cerulean's back.
        """
        peer = self.client_address[0] if self.client_address else ""
        if peer in SERVICE_CLIENTS:
            return True
        log(f"service refused from {peer}: not in MAIL_SERVICE_CLIENTS")
        self.json(403, {"error": "the machine surface is Cerulean-only"})
        return False

    def service(self, scope: str) -> dict | None:
        """Authorise a Cerulean request, or answer for it and return None.

        A Cerulean key and nothing else: no cookie, no CSRF token. Those belong to
        the browser session, which is a different principal — an operator's session
        must not stand in for a machine caller, and vice versa.
        """
        identity, status, error = service_authorize(self.headers.get("Authorization") or "", scope)
        if identity is None:
            log(f"service denied ({status}) {self.address_string()}: {error}")
            self.json(status, {"error": error})
            return None
        return identity

    def service_get(self, path: str) -> None:
        if not self.service_caller_allowed():
            return
        try:
            if path == "/api/service/state":
                identity = self.service(READ_SCOPE)
                if identity is None:
                    return
                state = list_state()
                state["cerulean"] = {"key": identity.get("name") or "cerulean",
                                     "scopes": identity.get("scopes") or []}
                return self.json(200, state)
            self.json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            log(f"error on GET {path}: {type(exc).__name__}: {exc}")
            self.json(500, {"error": str(exc)[:300]})

    def service_post(self, path: str) -> None:
        """`/api/service/provision` — the one write Cerulean makes here.

        Status codes follow what the caller has to decide: **201** when this call
        created the tenant, **200** when it was already here (a retry), **400** when
        the request is not usable, **401**/**403** for the credential, and **503**
        when Cerulean cannot be asked — because without the trust owner there is no
        authority to act under.
        """
        if not self.service_caller_allowed():
            return
        try:
            if path == "/api/service/provision":
                identity = self.service(PROVISION_SCOPE)
                if identity is None:
                    return
                # Whose Cerulean authority performs the DNS writes: the caller's
                # key when Cerulean says it may read *and* write records,
                # otherwise the platform's own. Decided from Cerulean's answer,
                # not guessed.
                scopes = identity.get("scopes") or []
                caller_key = bearer(self.headers.get("Authorization") or "")
                key = dns_authority_key(scopes, caller_key)
                body = self.body_json()
                result = provision_tenant(body.get("domain") or "",
                                          body.get("email") or "",
                                          body.get("name") or "",
                                          body.get("groups") or [],
                                          body.get("description"), key=key)
                log(f"service {identity.get('name') or 'cerulean'}: tenant "
                    f"{result['tenant']['domain']} "
                    f"({'existed' if result['existed'] else 'created'}, "
                    f"dns authority: {result['authority']})")
                return self.json(200 if result["existed"] else 201, result)
            self.json(404, {"error": "not found"})
        except NotFound as exc:
            self.json(404, {"error": str(exc)[:300]})
        except (RuntimeError, ValueError) as exc:
            log(f"error on POST {path}: {type(exc).__name__}: {exc}")
            self.json(400, {"error": str(exc)[:300]})
        except Exception as exc:  # noqa: BLE001
            log(f"error on POST {path}: {type(exc).__name__}: {exc}")
            self.json(500, {"error": str(exc)[:300]})

    # ── routing ──
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/service/"):
            return self.service_get(path)
        try:
            if path == "/":
                return self.page()
            if path == "/auth/login":
                return self.login()
            if path == "/auth/callback":
                return self.callback()
            if path == "/auth/logout":
                return self.logout()
            if path == "/api/state":
                return self.api_state()
            if path == "/healthz":
                return self.json(200, {"ok": True})
            self.json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            log(f"error on GET {path}: {type(exc).__name__}: {exc}")
            self.json(500, {"error": str(exc)[:300]})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/service/"):
            return self.service_post(path)
        try:
            session = self.session()
            if not session:
                return self.json(401, {"error": "not signed in"})
            if not self.require_csrf(session):
                return
            body = self.body_json()
            if path == "/api/domains":
                return self.api_domains(body)
            if path == "/api/users":
                return self.api_users(body)
            if path == "/api/tenants":
                return self.api_tenants(body)
            if path == "/api/users/password":
                return self.json(200, rotate_password(body.get("email", "")))
            self.json(404, {"error": "not found"})
        except NotFound as exc:
            self.json(404, {"error": str(exc)[:300]})
        except Exception as exc:  # noqa: BLE001
            log(f"error on POST {path}: {type(exc).__name__}: {exc}")
            self.json(400, {"error": str(exc)[:300]})

    # ── pages ──
    def page(self):
        session = self.session()
        if not session:
            return self.redirect("/auth/login")
        html = PAGE.replace("window.CSRF_TOKEN", json.dumps(session["csrf"]))
        self.html(200, html)

    def login(self):
        _, _, oidc = services()
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = b64url(hashlib.sha256(verifier.encode()).digest())
        signed = sign(json.dumps({"state": state, "nonce": nonce, "verifier": verifier,
                                  "exp": time.time() + 600}).encode())
        self.redirect(oidc.authorize_url(state, nonce, challenge),
                      {"Set-Cookie": f"{COOKIE}_flow={signed}; HttpOnly; Secure; "
                                     "SameSite=Lax; Path=/; Max-Age=600"})

    def callback(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        morsel = jar.get(f"{COOKIE}_flow")
        flow = unsign(morsel.value) if morsel else None
        if not flow:
            return self.html(400, "sign-in flow expired; go back and try again")
        if query.get("error"):
            return self.html(400, f"the identity provider refused: {query.get('error')}")
        if query.get("state", [None])[0] != flow["state"]:
            return self.html(400, "state mismatch")
        ak, _, oidc = services()
        tokens = oidc.exchange(query["code"][0], flow["verifier"])
        claims = oidc.verify(tokens["id_token"], flow["nonce"])
        info = oidc.userinfo(tokens["access_token"])
        email = (info.get("email") or claims.get("email") or "").lower()
        user = ak.user(email)
        if not user or not user.get("is_active"):
            return self.html(403, "that identity has no active account here")
        groups = ak.user_groups(user)
        if ADMIN_GROUP not in groups:
            return self.html(403, f"{email} is not in {ADMIN_GROUP}; ask for access")
        session = sign(json.dumps({"email": email, "name": user.get("name"),
                                   "groups": groups, "csrf": secrets.token_urlsafe(18),
                                   "exp": time.time() + SESSION_TTL}).encode())
        self.redirect("/", {"Set-Cookie": [
            f"{COOKIE}={session}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={SESSION_TTL}",
            f"{COOKIE}_flow=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0"]})

    def logout(self):
        self.redirect("/auth/login", {"Set-Cookie":
                      f"{COOKIE}=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0"})

    # ── api ──
    def api_state(self):
        session = self.require_session()
        if not session:
            return
        state = list_state()
        state["user"] = {"email": session["email"], "groups": session["groups"]}
        self.json(200, state)

    def api_domains(self, body: dict):
        action = (body.get("action") or "").lower()
        name = (body.get("name") or "").strip().lower()
        if action == "add":
            return self.json(200, add_domain(name, body.get("description")))
        if action == "remove":
            return self.json(200, remove_domain(name))
        if action == "zone":
            return self.json(200, create_zone(name))
        if action == "publish":
            return self.json(200, publish_dns(name))
        self.json(400, {"error": "action must be add, remove, zone or publish"})

    def api_users(self, body: dict):
        action = (body.get("action") or "").lower()
        email = (body.get("email") or "").strip().lower()
        if action == "add":
            return self.json(200, add_user(email, body.get("name") or "",
                                           body.get("groups") or []))
        if action == "remove":
            return self.json(200, remove_user(email))
        self.json(400, {"error": "action must be add or remove"})

    def api_tenants(self, body: dict):
        """The page's version of the single write an application makes."""
        domain = (body.get("domain") or "").strip().lower()
        email = (body.get("email") or "").strip().lower()
        log(f"tenant {domain} + {email}")
        return self.json(200, provision_tenant(domain, email, body.get("name") or "",
                                              body.get("groups") or [],
                                              body.get("description")))


def main() -> int:
    host, _, port = LISTEN.rpartition(":")
    server = ThreadingHTTPServer((host or "0.0.0.0", int(port)), Handler)
    log(f"listening on {host or '0.0.0.0'}:{port}, issuer {ISSUER}, group {ADMIN_GROUP}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
