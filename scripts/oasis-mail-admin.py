#!/usr/bin/env python3
"""Oasis mail administration — add and remove mail domains and users from a page.

Stalwart's own admin UI cannot create a user here: this estate's Stalwart
authenticates against the external Authentik OIDC directory, and Stalwart refuses
to write credentials for an external-directory account ("i cannot add another
user with sso enabled"). A mail account is therefore two records that must agree,
and this service is where they are created together:

  * **Authentik** — the person: the user object and membership of the account
    groups (`Mail`, plus any extras). Cerulean Authentik is the only identity
    source, so this is also the account's password store.
  * **Stalwart** — the mailbox: a `User` account with no credential, because the
    credential is Authentik's.

Domains are a single record (Stalwart), created the way innotel.us already is:
automatic DKIM, automatic DNS publishing against the estate's resolver, and an
automatic Let's Encrypt certificate.

## Who may use it

It is **OIDC-native** against Cerulean Authentik — the estate's sign-in posture
allows no third pattern (no forward-auth, no second password store). The browser
is sent through a real authorization-code + PKCE flow with the existing
`oasis-admin` client, the id_token is verified against Authentik's JWKS, and the
caller must be a member of `OASIS_ADMIN_GROUP` (default `Oasis`), checked against
Authentik's own group records rather than trusted from a token claim.

## Interface

    GET  /                      the page (redirects to /auth/login when signed out)
    GET  /auth/login            start the OIDC flow
    GET  /auth/callback         finish it
    GET  /auth/logout           clear the session
    GET  /api/state             JSON: domains and users
    POST /api/domains           {action: add|remove, name, description?}
    POST /api/users             {action: add|remove, email, name?, groups?}
    POST /api/users/password    {email}   rotate the Authentik password (returned once)

Reads the Stalwart recovery admin from /etc/stalwart/stalwart.env and the
Authentik admin token from Cerulean Vault (`/etc/oasis/vault-token`); the OIDC
client secret is fetched from Authentik at runtime rather than stored. Nothing
secret is logged or returned except a password the operator just asked for.
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
except ImportError:  # pragma: no cover - dependency is present on the hosts we deploy to
    rsa = None

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


def add_user(email: str, name: str, groups: list[str]) -> dict:
    ak, sw, _ = services()
    email = email.strip().lower()
    local, _, domain = email.partition("@")
    if not local or not domain:
        raise RuntimeError("email must be localpart@domain")
    if not sw.domain(domain):
        raise RuntimeError(f"{domain} is not a mail domain here — add the domain first")
    wanted = [DEFAULT_MAIL_GROUP] + [g for g in groups if g and g != DEFAULT_MAIL_GROUP]
    user = ak.user(email)
    result: dict = {"email": email, "groups": wanted}
    if user:
        merged = sorted(set(ak.user_groups(user)) | set(wanted))
        ak.set_groups(user["pk"], ak.group_ids(merged))
        if not user.get("is_active"):
            ak.set_active(user["pk"], True)
        result["authentik"] = f"updated groups={merged}"
    else:
        created = ak.create_user(local, email, name or local, ak.group_ids(wanted))
        user = created
        result["authentik"] = f"created pk={created.get('pk')}"
    if not sw.account(email):
        box = sw.create_mailbox(email, name)
        result["mailbox"] = f"created id={box.get('id')}"
    else:
        result["mailbox"] = "already present"
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
        raise RuntimeError(f"{email}: no such user")
    return result


def delegate_zone(name: str) -> dict:
    """Make `name` resolve, delegating it through Cerulean wherever Cerulean can.

    Cerulean owns the estate resolver, so delegation is its call, not the
    operator's:

    * A name inside a zone Cerulean already serves is delegated into that parent
      automatically — the child's NS records are written into the parent zone, so
      the name is delegated by the resolver itself rather than by hand. This is
      the common case: `signara.innotel.us` sits inside `innotel.us`.
    * A name at the apex of a domain nobody has delegated yet still needs its
      registrar pointed at the estate nameservers. No registrar API exists in
      this estate, so that one step is reported with the exact servers to use —
      it is a registration act, not something the mail service can perform.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cerulean import Cerulean, CeruleanError  # noqa: E402

    name = name.strip().lower().rstrip(".")
    client = Cerulean()
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
    # really are the parent's before trusting any of them.
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


def create_zone(name: str) -> dict:
    """Register this mail domain with Cerulean, which mints its authoritative zone.

    Cerulean (TrustOps) owns the estate resolver: it runs Technitium and is the
    only principal allowed to create zones and write records into it. Stalwart's
    own DNS publisher is switched off, so a new mail domain must have its zone
    created here first — otherwise there is nothing for DKIM/SPF/DMARC to be
    published into, and the name never resolves. Idempotent: a domain Cerulean
    already serves comes back with ``created: False`` rather than an error.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cerulean import Cerulean, CeruleanError  # noqa: E402

    name = name.strip().lower().rstrip(".")
    if not name or "." not in name:
        raise RuntimeError("that is not a domain name")
    try:
        result = Cerulean().ensure_zone(name)
    except CeruleanError as exc:
        raise RuntimeError(f"Cerulean could not create the zone: {exc}")
    result["resolver"] = "Cerulean (Technitium)"
    try:
        result["delegation"] = delegate_zone(name)
    except Exception as exc:  # noqa: BLE001 - the zone exists either way
        result["delegation"] = {"error": str(exc)[:200]}
    return result


def publish_dns(name: str) -> dict:
    """Write this domain's generated DKIM/SPF/DMARC (and friends) through Cerulean.

    Stalwart renders the zone but its own publisher is switched off — Cerulean is
    the only principal allowed to write the estate's resolver. Nothing is written
    into a zone the resolver does not already serve: a brand-new domain reports
    that its zone must be created first rather than inventing records for a name
    that does not resolve.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
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

    client = Cerulean()
    try:
        live_rows = client.records(name)
    except CeruleanError:
        live_rows = []

    def belongs(record_name) -> bool:
        n = str(record_name or "").lower().rstrip(".")
        return n == name or n.endswith("." + name)

    # The bridge ignores the ?zone= filter on read and answers with the zone it
    # does serve, so an unserved domain looks like it has records. Only publish
    # when every record that came back really is inside this zone; otherwise the
    # zone does not exist yet and writing here would mint a private zone that DNS
    # never delegates to (and that nothing will ever clean up).
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
        if rec["value"] in present:
            exact += 1
            continue
        kind = cz.classify(rec)
        if kind == "override":          # estate-owned: apex SPF/DMARC/MX and CAA issue
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


def add_domain(name: str, description: str | None) -> dict:
    _, sw, _ = services()
    name = name.strip().lower()
    if not name or "." not in name:
        raise RuntimeError("that is not a domain name")
    if sw.domain(name):
        raise RuntimeError(f"{name} already exists in Stalwart")
    made = sw.create_domain(name, description)
    after = sw.domain(name)
    result = {"domain": name, "id": made.get("id"),
              "enabled": bool(after and after.get("isEnabled")),
              "next": [f"MX at {MX_HOST} if this domain receives mail"]}
    # Mint the authoritative zone through Cerulean before publishing: Cerulean
    # owns the resolver, and DKIM/SPF/DMARC have nowhere to go until the zone
    # exists. Both steps are idempotent, so re-running add is safe.
    try:
        result["zone"] = create_zone(name)
    except Exception as exc:  # noqa: BLE001 - the domain is created either way
        result["zone"] = {"error": str(exc)[:200]}
    try:
        result["dns"] = publish_dns(name)
    except Exception as exc:  # noqa: BLE001 - the domain is created either way
        result["dns"] = {"error": str(exc)[:200]}
    return result


def delete_zone(name: str) -> dict:
    """Unregister this domain with Cerulean, which removes its authoritative zone.

    The counterpart of creating the zone: leaving it behind keeps the name
    answering (and its DKIM/SPF/DMARC live) after the domain it was for is gone,
    which nothing would ever clean up. Idempotent — a zone Cerulean does not
    serve reports ``deleted: False`` rather than failing. Cerulean refuses its
    own platform zones outright.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
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
    # name that no longer answers.
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
    sw.destroy_domain(name)
    result: dict = {"domain": name, "destroyed": True}
    try:
        result["zone"] = delete_zone(name)
    except Exception as exc:  # noqa: BLE001 - the domain is gone either way
        result["zone"] = {"error": str(exc)[:200]}
    return result


def rotate_password(email: str) -> dict:
    ak, _, _ = services()
    user = ak.user(email.strip().lower())
    if not user:
        raise RuntimeError(f"{email}: no such user")
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

    def json(self, code: int, obj: dict, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json", extra)

    def html(self, code: int, text: str, extra: dict | None = None) -> None:
        self._send(code, text.encode(), "text/html; charset=utf-8", extra)

    def redirect(self, url: str, extra: dict | None = None) -> None:
        self._send(302, b"", "text/plain", {"Location": url, **(extra or {})})

    def body_json(self) -> dict:
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

    # ── routing ──
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
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
            if path == "/api/users/password":
                return self.json(200, rotate_password(body.get("email", "")))
            self.json(404, {"error": "not found"})
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
