#!/usr/bin/env python3
"""Flip Stalwart to **Cerulean-only** trust: it stops writing DNS itself.

On this estate Stalwart still holds a *direct* Technitium `DnsServer` (a TSIG
key to 192.168.1.71) and uses it for two things: publishing the DKIM/SPF/DMARC
records of each domain (`dnsManagement: Automatic`) and solving its own Let's
Encrypt DNS-01 challenges (`certificateManagement: Automatic`). Both violate the
rule that **Cerulean is the only principal allowed to use Technitium** — and the
TSIG path is broken anyway (hickory-dns rejects Technitium's truncated HMACs).

This sets every local domain to:

    dnsManagement         = Manual   (Cerulean's publisher owns the records)
    certificateManagement = Manual   (the cert is installed from Cerulean)

and then removes the now-unreferenced direct Technitium `DnsServer` and the
unused `AcmeProvider` that used it, so the estate has exactly one Technitium
writer: Cerulean.

    python3 stalwart_cerulean_only.py                 # check (default)
    python3 stalwart_cerulean_only.py --apply         # flip + drop the TSIG server
    python3 stalwart_cerulean_only.py --domain innotel.us --apply
    python3 stalwart_cerulean_only.py --apply --keep-acme-providers

`--keep-acme-providers` leaves Stalwart's ACME accounts in place (they become
inert once their DnsServer is gone). Nothing secret is printed.
"""
import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://mail.innotel.us"
JMAP = ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"]


def admin() -> tuple[str, str]:
    for raw in Path("/etc/stalwart/stalwart.env").read_text(encoding="utf-8").splitlines():
        if raw.strip().startswith("STALWART_RECOVERY_ADMIN="):
            user, _, password = raw.strip().split("=", 1)[1].partition(":")
            return user, password
    raise SystemExit("no STALWART_RECOVERY_ADMIN")


class Api:
    def __init__(self) -> None:
        user, password = admin()
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
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.load(r)["methodResponses"][0][1]
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"{method}: HTTP {exc.code} {exc.read().decode()[:400]}")
        if out.get("type") == "error":
            raise SystemExit(f"{method}: {out.get('type')} {out.get('description')}")
        if out.get("notUpdated"):
            raise SystemExit(f"{method}: update failed: {json.dumps(out['notUpdated'])[:400]}")
        if out.get("notDestroyed"):
            raise SystemExit(f"{method}: destroy failed: {json.dumps(out['notDestroyed'])[:400]}")
        return out


def is_manual(value: dict | None) -> bool:
    return (value or {}).get("@type") == "Manual"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--domain", action="append",
                    help="limit to this domain (repeatable; default: every domain)")
    ap.add_argument("--keep-acme-providers", action="store_true",
                    help="leave Stalwart's ACME accounts in place (they become inert)")
    ap.add_argument("--apply", action="store_true", help="write (default is a check)")
    args = ap.parse_args()

    api = Api()
    domains = api.call("x:Domain/get").get("list") or []
    servers = api.call("x:DnsServer/get").get("list") or []
    providers = api.call("x:AcmeProvider/get").get("list") or []

    wanted = {d.lower() for d in (args.domain or [])}
    targets = [d for d in domains if not wanted or (d.get("name") or "").lower() in wanted]
    if wanted:
        missing = wanted - {str(d.get("name") or "").lower() for d in targets}
        if missing:
            raise SystemExit(f"no such domain(s) in Stalwart: {', '.join(sorted(missing))}")

    print(f"domains        : {len(targets)} of {len(domains)} selected")
    print(f"dns servers    : {len(servers)} "
          + ("[" + ", ".join(f"{s.get('@type')} id={s.get('id')}" for s in servers) + "]"
             if servers else "(none)"))
    print(f"acme providers : {len(providers)} "
          + ("[" + ", ".join(f"id={p.get('id')}" for p in providers) + "]"
             if providers else "(none)"))

    changes = 0
    for dom in targets:
        name, did = dom.get("name"), dom["id"]
        dns_manual = is_manual(dom.get("dnsManagement"))
        cert_manual = is_manual(dom.get("certificateManagement"))
        print(f"\n{name}  (id={did})")
        print(f"  dnsManagement         : {json.dumps(dom.get('dnsManagement'))}")
        print(f"  certificateManagement : {json.dumps(dom.get('certificateManagement'))}")
        if dns_manual and cert_manual:
            print("  already Cerulean-only; nothing to change")
            continue
        changes += 1
        if not args.apply:
            print("  --check: would set dns + certificate management to Manual")
            continue
        patch: dict = {}
        if not dns_manual:
            patch["dnsManagement"] = {"@type": "Manual"}
        if not cert_manual:
            patch["certificateManagement"] = {"@type": "Manual"}
        api.call("x:Domain/set", {"update": {did: patch}})
        print(f"  -> Manual ({', '.join(patch)})")

    flipped_ids = {t["id"] for t in targets}

    def still_referenced(server_id: str) -> list[str]:
        if args.apply:
            current = api.call("x:Domain/get").get("list") or []
        else:
            current = [d for d in domains if d["id"] not in flipped_ids]
        return [d.get("name") for d in current
                if d.get("dnsManagement", {}).get("dnsServerId") == server_id]

    if servers:
        for srv in servers:
            sid = srv.get("id")
            refs = still_referenced(sid)
            if refs:
                print(f"\nDnsServer id={sid}: still referenced by {refs}; not removing")
                continue
            if not args.apply:
                print(f"\n--check: would destroy DnsServer id={sid} "
                      f"({srv.get('@type')} -> {srv.get('host')}, key '{srv.get('keyName')}')")
                changes += 1
                continue
            api.call("x:DnsServer/set", {"destroy": [sid]})
            print(f"\nDnsServer id={sid}: destroyed — Stalwart no longer talks to Technitium")
            changes += 1

    if providers and not args.keep_acme_providers:
        for prov in providers:
            pid = prov.get("id")
            if not args.apply:
                print(f"--check: would destroy AcmeProvider id={pid} "
                      f"(accountUri={prov.get('accountUri')})")
                changes += 1
                continue
            api.call("x:AcmeProvider/set", {"destroy": [pid]})
            print(f"AcmeProvider id={pid}: destroyed — certificates come from Cerulean")
            changes += 1

    if not changes:
        print("\nalready Cerulean-only; nothing to change")
    elif not args.apply:
        print(f"\n--check: {changes} change(s) pending; pass --apply to write")
    else:
        print(f"\napplied {changes} change(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
