#!/usr/bin/env python3
"""One verdict on the mail platform's trust posture — for CI and drift-check.

Answers, read-only, the questions the individual scripts each answer in part:

* Is every Stalwart domain `dnsManagement: Manual` (Cerulean owns DNS)?
* Is every domain `certificateManagement: Manual` (Cerulean owns TLS)?
* Is there **no** direct Technitium `DnsServer` and **no** `AcmeProvider` left —
  i.e. does Stalwart have any route to Technitium or a CA at all?
* Does the certificate Stalwart serves match the one Cerulean issued (same
  expiry and SANs)?
* Would publishing Stalwart's generated zone report any `conflict` — a record
  Cerulean would have to write over a value it cannot safely replace?

It never writes. Exit status is 0 when everything passes, 1 when any check
fails (and 2 when a check cannot be judged — a missing key, an unreachable
Cerulean — because "cannot tell" must not read as "clean").

    python3 verify_trust.py            # human report
    python3 verify_trust.py --json     # machine report
    python3 verify_trust.py --strict   # also fail on estate-owned override drift
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cerulean import Cerulean, CeruleanError  # noqa: E402
from cerulean_publish_zone import classify, list_domains, parse_zone, stalwart_zone  # noqa: E402
from stalwart_install_cert import Api, decode_leaf  # noqa: E402

DOMAIN = "innotel.us"


def check_domains(api: "Api", domains: list[str]) -> list[tuple[str, str, str]]:
    rows = []
    detail = {d["name"]: d for d in api.call("x:Domain/get").get("list") or []}
    for name in domains:
        dom = detail.get(name, {})
        dns = (dom.get("dnsManagement") or {}).get("@type")
        cert = (dom.get("certificateManagement") or {}).get("@type")
        ok = dns == "Manual" and cert == "Manual"
        rows.append(("domain", f"{name} dns/cert management",
                     "PASS" if ok else f"FAIL dns={dns} cert={cert}"))
    return rows


def check_no_direct(api: "Api") -> list[tuple[str, str, str]]:
    servers = api.call("x:DnsServer/get").get("list") or []
    providers = api.call("x:AcmeProvider/get").get("list") or []
    rows = [("trust", "no direct Technitium DnsServer",
             "PASS" if not servers else f"FAIL {len(servers)} DnsServer(s)")]
    rows.append(("trust", "no Stalwart ACME provider",
                 "PASS" if not providers else f"FAIL {len(providers)} AcmeProvider(s)"))
    return rows


def check_certificate(api: "Api", client: "Cerulean") -> list[tuple[str, str, str]]:
    issued = client.find_wildcard(DOMAIN)
    if not issued or not issued.get("hasMaterial"):
        return [("cert", "Cerulean wildcard present", "FAIL none issued")]
    material = client.material(issued["id"])
    want_after, want_sans = decode_leaf(material["certificate"])
    certs = api.call("x:Certificate/get").get("list") or []
    names = {f"*.{DOMAIN}", DOMAIN}
    match = next((c for c in certs
                  if names & set((c.get("subjectAlternativeNames") or {}).keys())), None)
    if not match:
        return [("cert", "Stalwart has the wildcard installed", "FAIL not found")]
    from stalwart_install_cert import parse_iso
    installed_after = parse_iso(match.get("notValidAfter"))
    installed_sans = {str(s).lower() for s in (match.get("subjectAlternativeNames") or {})}
    ok = installed_after == want_after and installed_sans == want_sans
    return [("cert", "installed wildcard matches Cerulean",
             "PASS" if ok else f"FAIL installed={installed_after} cerulean={want_after}")]


def check_zones(domains: list[str], client: "Cerulean", strict: bool
                ) -> list[tuple[str, str, str]]:
    rows = []
    for domain in domains:
        text, _ = stalwart_zone(domain)
        parsed = [r for r in parse_zone(text, domain) if r["type"] != "TLSA"]
        have: dict[tuple[str, str], set[str]] = {}
        for r in client.records(domain):
            key = (str(r.get("name", "")).lower().rstrip("."), str(r.get("type", "")).upper())
            have.setdefault(key, set()).add(str(r.get("value", "")).strip())
        conflicts = [r for r in parsed
                     if r["value"] not in have.get((r["name"], r["type"]), set())
                     and classify(r) == "singleton"
                     and have.get((r["name"], r["type"]))]
        overrides = [r for r in parsed
                     if r["value"] not in have.get((r["name"], r["type"]), set())
                     and classify(r) == "override" and have.get((r["name"], r["type"]))]
        state = "FAIL" if conflicts else ("FAIL" if (strict and overrides) else "PASS")
        note = ""
        if conflicts:
            note = f" {len(conflicts)} conflict(s)"
        elif strict and overrides:
            note = f" {len(overrides)} override(s) drifted"
        elif overrides:
            note = f" {len(overrides)} estate-owned override(s) kept"
        rows.append(("zone", f"{domain} records publishable", state + note))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strict", action="store_true",
                    help="treat estate-owned override drift as a failure")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    args = ap.parse_args()

    rows: list[tuple[str, str, str]] = []
    try:
        api = Api()
        client = Cerulean()
    except (SystemExit, CeruleanError) as exc:
        print(f"cannot judge: {exc}", file=sys.stderr)
        return 2

    try:
        domains = list_domains()
        rows += check_no_direct(api)
        rows += check_domains(api, domains)
        rows += check_certificate(api, client)
        rows += check_zones(domains, client, args.strict)
    except (SystemExit, CeruleanError) as exc:
        print(f"cannot judge: {exc}", file=sys.stderr)
        return 2

    failed = [r for r in rows if r[2].startswith("FAIL")]
    if args.json:
        print(json.dumps({"checks": [{"group": g, "name": n, "result": s}
                                     for g, n, s in rows],
                          "ok": not failed}, indent=1))
    else:
        width = max(len(r[1]) for r in rows)
        for group, name, status in rows:
            print(f"  [{group:6s}] {name:{width}s}  {status}")
        print(f"\n{'OK' if not failed else 'DRIFT'}: {len(rows) - len(failed)}/{len(rows)} checks pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
