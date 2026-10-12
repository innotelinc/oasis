#!/usr/bin/env python3
"""Administer mail users across both planes from one command.

A mail account on this estate is two records that have to agree:

* **Authentik** — the person: the user object and membership of the `Mail`
  group. Cerulean Authentik is the only identity source, and Stalwart's
  authentication directory is the external Authentik OIDC one, so SSO into the
  webmail (and every other platform) follows this group.
* **Stalwart** — the mailbox: a `User` account for the address, created with no
  password, because the password is Authentik's. Stalwart refuses credential
  writes for external-directory accounts by design.

Doing the add/subscribe from one place means they cannot disagree: `add` creates
the Authentik user and puts them in `Mail` (SSO works immediately), then creates
the Stalwart mailbox that receives their mail. `remove` reverses both.

    python3 oasis-mail-user.py list
    python3 oasis-mail-user.py list --domain innotel.us
    python3 oasis-mail-user.py add --email jane@innotel.us --name "Jane Doe" \
        --groups paid_users
    python3 oasis-mail-user.py add --email jane@innotel.us --apply
    python3 oasis-mail-user.py passwd --email jane@innotel.us --apply
    python3 oasis-mail-user.py remove --email jane@innotel.us --apply

Authentik credentials come from **Cerulean Vault** (`cerulean/oasis`:
`authentik_token`, `authentik_api_url`) with this platform's path-scoped token at
`VAULT_TOKEN_FILE` (`/etc/oasis/vault-token`); an env override exists for hosts
without Vault. The Stalwart admin comes from /etc/stalwart/stalwart.env. Nothing
secret is printed.
"""
import argparse
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://mail.innotel.us"
JMAP = ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"]
ENV_FILE = Path("/etc/stalwart/stalwart.env")
# Vault KV field names (a field name, not a credential).
VAULT_KV = "cerulean/data/oasis"
AUTHENTIK_FIELD = "authentik_token"
AUTHENTIK_URL_FIELD = "authentik_api_url"
DEFAULT_MAIL_GROUP = "Mail"


# ── secrets ─────────────────────────────────────────────────────────────────
def _vault() -> dict:
    addr = os.environ.get("VAULT_ADDR", "http://192.168.1.71:8200").rstrip("/")
    token_file = os.environ.get("VAULT_TOKEN_FILE", "/etc/oasis/vault-token")
    if not Path(token_file).is_file():
        return {}
    token = Path(token_file).read_text(encoding="utf-8").strip()
    req = urllib.request.Request(f"{addr}/v1/{VAULT_KV}")
    req.add_header("X-Vault-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("data", {}).get("data", {})
    except (urllib.error.URLError, ValueError):
        return {}


def authentik_config() -> tuple[str, str]:
    vault = _vault()
    token = os.environ.get("AUTHENTIK_TOKEN") or vault.get(AUTHENTIK_FIELD)
    base = (os.environ.get("AUTHENTIK_API_URL") or vault.get(AUTHENTIK_URL_FIELD)
            or "http://192.168.1.71:9000")
    if not token:
        raise SystemExit("no Authentik token: set AUTHENTIK_TOKEN or expose "
                         f"{AUTHENTIK_FIELD} in Cerulean Vault at {VAULT_KV}")
    return base.rstrip("/"), token


# ── Authentik ───────────────────────────────────────────────────────────────
class Authentik:
    def __init__(self) -> None:
        self.base, self.token = authentik_config()

    def call(self, path: str, method: str = "GET", body: dict | None = None):
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            raise SystemExit(f"authentik {method} {path}: HTTP {exc.code} {detail}")

    def users(self) -> list[dict]:
        return self.call("/api/v3/core/users/?page_size=200")["results"]

    def user(self, email: str) -> dict | None:
        want = email.lower()
        return next((u for u in self.users()
                     if (u.get("email") or "").lower() == want), None)

    def group(self, name: str) -> dict | None:
        res = self.call("/api/v3/core/groups/?" + urllib.parse.urlencode({"name": name}))
        return (res.get("results") or [None])[0]

    def ensure_group(self, name: str, apply: bool) -> dict | None:
        grp = self.group(name)
        if grp or not apply:
            return grp
        created = self.call("/api/v3/core/groups/", "POST", {"name": name})
        return created

    def create_user(self, username: str, email: str, name: str, group_ids: list[str]) -> dict:
        body = {"username": username, "email": email, "name": name or username,
                "is_active": True, "path": "users", "attributes": {},
                "groups": group_ids}
        return self.call("/api/v3/core/users/", "POST", body)

    def set_groups(self, pk: int, group_ids: list[str]) -> dict:
        """Replace the user's groups. Authentik wants group **PKs**, not names."""
        return self.call(f"/api/v3/core/users/{pk}/", "PATCH", {"groups": group_ids})

    def group_ids(self, names: list[str], apply: bool) -> list[str]:
        """Resolve group names to the PKs Authentik's write API requires."""
        ids = []
        for name in names:
            grp = self.ensure_group(name, apply)
            if not grp:
                raise SystemExit(f"could not resolve or create Authentik group {name!r}")
            ids.append(grp["pk"])
        return ids

    def set_active(self, pk: int, active: bool) -> dict:
        return self.call(f"/api/v3/core/users/{pk}/", "PATCH", {"is_active": active})

    def set_password(self, pk: int, password: str) -> None:
        self.call(f"/api/v3/core/users/{pk}/set_password/", "POST", {"password": password})

    def password_change_date(self, pk: int) -> str | None:
        return self.call(f"/api/v3/core/users/{pk}/").get("password_change_date")

    def delete_user(self, pk: int) -> None:
        self.call(f"/api/v3/core/users/{pk}/", "DELETE")

    def user_groups(self, user: dict) -> list[str]:
        return [g["name"] for g in (user.get("groups_obj") or [])]


# ── Stalwart ────────────────────────────────────────────────────────────────
def stalwart_admin() -> tuple[str, str]:
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if raw.strip().startswith("STALWART_RECOVERY_ADMIN="):
            user, _, password = raw.strip().split("=", 1)[1].partition(":")
            return user, password
    raise SystemExit(f"no STALWART_RECOVERY_ADMIN in {ENV_FILE}")


class Stalwart:
    def __init__(self) -> None:
        user, password = stalwart_admin()
        self.auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        req = urllib.request.Request(f"{BASE}/.well-known/jmap")
        req.add_header("Authorization", f"Basic {self.auth}")
        with urllib.request.urlopen(req, timeout=20) as r:
            self.api = json.load(r).get("apiUrl") or f"{BASE}/api"

    def call(self, method: str, payload: dict | None = None) -> dict:
        body = {"using": JMAP, "methodCalls": [[method, payload or {}, "c"]]}
        req = urllib.request.Request(self.api, data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", f"Basic {self.auth}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.load(r)["methodResponses"][0][1]
        if out.get("type") == "error":
            raise SystemExit(f"{method}: {out.get('type')} {out.get('description')}")
        return out

    def accounts(self) -> list[dict]:
        return self.call("x:Account/get").get("list") or []

    def account(self, address: str) -> dict | None:
        return next((a for a in self.accounts()
                     if (a.get("emailAddress") or "").lower() == address.lower()), None)

    def domain_id(self, domain: str) -> str:
        for dom in self.call("x:Domain/get").get("list") or []:
            if (dom.get("name") or "").lower() == domain.lower():
                return dom["id"]
        raise SystemExit(f"{domain}: no such domain in Stalwart — add it first "
                         "(scripts/cerulean or stalwart_add_domain.py)")

    def create_mailbox(self, address: str, description: str | None) -> dict:
        local, _, domain = address.partition("@")
        record: dict = {"@type": "User", "name": local,
                        "domainId": self.domain_id(domain),
                        "roles": {"@type": "User"}, "permissions": {"@type": "Inherit"}}
        if description:
            record["description"] = description
        out = self.call("x:Account/set", {"create": {"n": record}})
        if not out.get("created"):
            raise SystemExit("stalwart create failed: "
                             + json.dumps(out.get("notCreated") or out)[:300])
        return out["created"]["n"]

    def destroy(self, account_id: str) -> None:
        out = self.call("x:Account/set", {"destroy": [account_id]})
        if out.get("notDestroyed"):
            raise SystemExit("stalwart destroy failed: "
                             + json.dumps(out["notDestroyed"])[:300])


# ── commands ────────────────────────────────────────────────────────────────
def cmd_list(args, ak: Authentik, sw: Stalwart) -> int:
    accounts = {(a.get("emailAddress") or "").lower(): a for a in sw.accounts()}
    users = ak.users()
    if args.domain:
        users = [u for u in users
                 if (u.get("email") or "").lower().endswith(f"@{args.domain.lower()}")]
    print(f"{'email':34s} {'username':20s} {'active':6s} {'Mail':5s} {'mailbox':8s} groups")
    mismatched = 0
    for u in sorted(users, key=lambda x: x.get("email") or ""):
        email = (u.get("email") or "").lower()
        if not email:
            continue
        groups = ak.user_groups(u)
        has_box = email in accounts
        mail = DEFAULT_MAIL_GROUP in groups
        if not (has_box and mail and u.get("is_active")):
            mismatched += 1
        print(f"{email:34s} {str(u.get('username')):20s} "
              f"{str(u.get('is_active')):6s} {str(mail):5s} {str(has_box):8s} "
              f"{[g for g in groups if g in ('Mail', 'Oasis', 'paid_users')]}")
    print(f"\n{len(users)} user(s); {mismatched} not fully wired "
          "(active + Mail group + mailbox)")
    return 0


def cmd_add(args, ak: Authentik, sw: Stalwart) -> int:
    email = args.email.strip()
    local, _, domain = email.partition("@")
    if not local or not domain:
        raise SystemExit("--email must be localpart@domain")
    username = args.username or local
    groups = [g.strip() for g in (args.groups or "").split(",") if g.strip()]
    wanted = [DEFAULT_MAIL_GROUP] + [g for g in groups if g != DEFAULT_MAIL_GROUP]

    ak_user = ak.user(email)
    sw_account = sw.account(email)
    print(f"email    : {email}")
    print(f"authentik: {'exists pk=' + str(ak_user['pk']) + ' groups=' + str(ak.user_groups(ak_user)) if ak_user else 'absent'}")
    print(f"stalwart : {'exists id=' + sw_account['id'] if sw_account else 'absent'}")
    print(f"groups   : {wanted}")

    if not args.apply:
        print("\n--check: would create/complete the Authentik user and the mailbox")
        return 0

    group_ids = ak.group_ids(wanted, apply=True)

    if ak_user:
        merged = sorted(set(ak.user_groups(ak_user)) | set(wanted))
        ak.set_groups(ak_user["pk"], ak.group_ids(merged, apply=True))
        if not ak_user.get("is_active"):
            ak.set_active(ak_user["pk"], True)
        print(f"authentik: updated (groups={merged}, active)")
    else:
        created = ak.create_user(username, email, args.name or username, group_ids)
        ak_user = created
        print(f"authentik: created pk={created.get('pk')} username={created.get('username')}")

    if not sw_account:
        box = sw.create_mailbox(email, args.name)
        print(f"stalwart : created mailbox id={box.get('id')}")
    else:
        print("stalwart : mailbox already present")

    verify_user = ak.user(email)
    verify_box = sw.account(email)
    ok = (verify_user and DEFAULT_MAIL_GROUP in ak.user_groups(verify_user)
          and verify_box and verify_user.get("is_active"))
    print(f"\nverified : sso_group={DEFAULT_MAIL_GROUP in ak.user_groups(verify_user)} "
          f"mailbox={bool(verify_box)} active={verify_user.get('is_active')}")
    return 0 if ok else 1


def cmd_passwd(args, ak: Authentik, sw: Stalwart) -> int:
    """Rotate the mailbox's Authentik password and record it where the operator reads it."""
    email = args.email.strip()
    user = ak.user(email)
    if not user:
        raise SystemExit(f"{email}: no such Authentik user")
    out = Path(args.out or f"/root/{email.split('@')[0]}-credential.txt")
    print(f"email    : {email}")
    print(f"authentik: pk={user['pk']} username={user.get('username')} "
          f"active={user.get('is_active')} groups={ak.user_groups(user)}")
    print(f"file     : {out}{'' if out.exists() else ' (new)'}")
    if not args.apply:
        print("\n--check: would set a new random password and write it to the file")
        return 0

    before = ak.password_change_date(user["pk"])
    password = secrets.token_urlsafe(18)
    ak.set_password(user["pk"], password)
    after = ak.password_change_date(user["pk"])
    if after == before:
        print("FAIL: password_change_date did not move - password not applied",
              file=sys.stderr)
        return 1

    if out.exists():
        backup = out.with_name(f"{out.name}.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.bak")
        backup.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
        backup.chmod(0o600)
        print(f"backup   : {backup}")
    out.write_text(f"{email} / {password}\n", encoding="utf-8")
    out.chmod(0o600)
    print(f"rotated  : password_change_date {before} -> {after}")
    print(f"written  : {out} (mode 600; password not printed here)")
    return 0


def cmd_remove(args, ak: Authentik, sw: Stalwart) -> int:
    email = args.email.strip()
    ak_user = ak.user(email)
    sw_account = sw.account(email)
    print(f"email    : {email}")
    print(f"authentik: {'pk=' + str(ak_user['pk']) if ak_user else 'absent'}")
    print(f"stalwart : {'id=' + sw_account['id'] if sw_account else 'absent'}")
    action = "delete the Authentik user" if getattr(args, "purge", False) \
        else "deactivate the Authentik user"
    if not args.apply:
        print(f"\n--check: would {action} and destroy the mailbox")
        return 0
    if ak_user:
        if getattr(args, "purge", False):
            ak.delete_user(ak_user["pk"])
            print("authentik: deleted")
        else:
            ak.set_active(ak_user["pk"], False)
            print("authentik: deactivated (SSO access revoked)")
    if sw_account:
        sw.destroy(sw_account["id"])
        print("stalwart : mailbox destroyed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="join Authentik users and Stalwart mailboxes")
    p_list.add_argument("--domain")

    p_add = sub.add_parser("add", help="create/complete a mail user on both planes")
    p_add.add_argument("--email", required=True)
    p_add.add_argument("--name")
    p_add.add_argument("--username")
    p_add.add_argument("--groups", help="extra Authentik groups (comma-separated)")
    p_add.add_argument("--apply", action="store_true")

    p_rm = sub.add_parser("remove", help="deactivate the user and destroy the mailbox")
    p_rm.add_argument("--email", required=True)
    p_rm.add_argument("--purge", action="store_true",
                      help="delete the Authentik user instead of deactivating it")
    p_rm.add_argument("--apply", action="store_true")

    p_pw = sub.add_parser("passwd", help="rotate the user's Authentik password")
    p_pw.add_argument("--email", required=True)
    p_pw.add_argument("--out", help="credential file (default /root/<localpart>-credential.txt)")
    p_pw.add_argument("--apply", action="store_true")

    args = ap.parse_args()
    ak, sw = Authentik(), Stalwart()
    return {"list": cmd_list, "add": cmd_add, "remove": cmd_remove,
            "passwd": cmd_passwd}[args.cmd](args, ak, sw)


if __name__ == "__main__":
    sys.exit(main())
