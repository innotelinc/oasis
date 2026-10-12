#!/usr/bin/env python3
"""Publish Stalwart's generated zone records into DNS **through Cerulean**.

Stalwart renders a full zone for each mail domain (DKIM, SPF, DMARC, MTA-STS,
SRV/autoconfig CNAMEs, CAA, MX…). Its own publisher is now off (`dnsManagement:
Manual`, see `stalwart_cerulean_only.py`), so those records must be written by
the one principal allowed to touch Technitium: **Cerulean**. This takes the zone
Stalwart generates and adds whatever is missing via Cerulean's service bridge
(`POST /api/service/dns/records`).

Not every line of Stalwart's template is ours to write. Each record falls into
one class, so a run can never create a duplicate or a conflict:

* ``exact``    – ``(name, type, value)`` already present → skip.
* ``add``      – additive kinds (SRV, non-SPF/DMARC ``TXT`` such as MTA-STS,
                 TLS-RPT, DKIM, ua-auto-config, and CAA ``iodef``) → add when
                 that value is missing. Several values at one name are legal.
* ``singleton``– kinds a name may hold only one of (CNAME, and ``TXT`` at a name
                 that already holds one) → add only when the name has none,
                 otherwise report ``CONFLICT`` and leave the live value alone.
* ``override`` – the estate owns these on purpose: apex/mail **SPF**, **DMARC**,
                 **MX** routing, and the CAA ``issue`` line (a plain
                 ``letsencrypt.org`` grant, *not* Stalwart's ``accounturi=`` pin,
                 so Cerulean's own ACME account can still issue). Reported as
                 ``override`` and never written; ``--strict`` makes them fail so
                 CI notices if the live value vanishes.

    python3 cerulean_publish_zone.py --domain innotel.us            # check
    python3 cerulean_publish_zone.py --domain innotel.us --apply    # write
    python3 cerulean_publish_zone.py --zone-file zone.txt --zone innotel.us --apply

TLSA is skipped: Stalwart emits it, but the bridge has no TLSA record shape.
Nothing secret is printed. The Cerulean key comes from CERULEAN_SERVICE_KEY,
CERULEAN_SERVICE_KEY_FILE, or /etc/oasis/cerulean-service.key (see cerulean.py).
"""
import argparse
import base64
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cerulean import Cerulean, CeruleanError  # noqa: E402

BASE = "https://mail.innotel.us"
JMAP = ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"]
DEFAULT_TYPES = ("TXT", "SRV", "CNAME", "CAA", "MX")


def admin() -> tuple[str, str]:
    for raw in Path("/etc/stalwart/stalwart.env").read_text(encoding="utf-8").splitlines():
        if raw.strip().startswith("STALWART_RECOVERY_ADMIN="):
            user, _, password = raw.strip().split("=", 1)[1].partition(":")
            return user, password
    raise SystemExit("no STALWART_RECOVERY_ADMIN")


def list_domains() -> list[str]:
    """Every mail domain Stalwart hosts (for --all-domains / the renewal timer)."""
    user, password = admin()
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(f"{BASE}/.well-known/jmap")
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=20) as r:
        api = json.load(r).get("apiUrl") or f"{BASE}/api"
    body = {"using": JMAP, "methodCalls": [["x:Domain/get", {}, "c"]]}
    req = urllib.request.Request(api, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        domains = json.load(r)["methodResponses"][0][1].get("list") or []
    return [d["name"] for d in domains if d.get("name")]


def stalwart_zone(domain: str) -> tuple[str, str]:
    """Return (zone_text, dnsManagement_json) for `domain` from Stalwart."""
    user, password = admin()
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(f"{BASE}/.well-known/jmap")
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=20) as r:
        api = json.load(r).get("apiUrl") or f"{BASE}/api"
    body = {"using": JMAP, "methodCalls": [["x:Domain/get", {}, "c"]]}
    req = urllib.request.Request(api, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            domains = json.load(r)["methodResponses"][0][1].get("list") or []
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"x:Domain/get: HTTP {exc.code} {exc.read().decode()[:300]}")
    dom = next((d for d in domains if (d.get("name") or "").lower() == domain.lower()), None)
    if not dom:
        raise SystemExit(f"{domain}: no such domain in Stalwart")
    dns = json.dumps(dom.get("dnsManagement") or {"@type": "Manual"})
    return dom.get("dnsZoneFile") or "", dns


def _flatten(text: str) -> list[str]:
    """Join parenthesised multi-line records into single logical lines."""
    flat, buf = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        buf = f"{buf} {line}".strip() if buf else line
        if buf.count("(") != buf.count(")"):
            continue
        flat.append(buf)
        buf = ""
    if buf:
        flat.append(buf)
    return flat


def parse_zone(text: str, zone: str) -> list[dict]:
    """Flatten the zone file into Cerulean `value` strings per record type."""
    zone = zone.rstrip(".").lower()
    records = []
    for line in _flatten(text):
        line = re.sub(r"[()]", "", line)
        m = re.match(r"^(\S+)\s+(?:\d+\s+)?IN\s+([A-Z0-9]+)\s+(.*)$", line, re.I)
        if not m:
            continue
        name, rtype, rest = m.group(1).rstrip("."), m.group(2).upper(), m.group(3).strip()
        if name == "@":
            name = zone
        fqdn = name if name.lower().endswith(zone) else f"{name}.{zone}"
        if rtype == "TXT":
            value = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', rest)) or rest.strip('"')
        elif rtype == "CNAME":
            value = rest.split()[0].rstrip(".")
        elif rtype == "MX":
            pref, host = rest.split()
            value = f"{pref} {host.rstrip('.')}"
        elif rtype == "SRV":
            pri, weight, port, target = rest.split()
            value = f"{pri} {weight} {port} {target.rstrip('.')}"
        elif rtype == "CAA":
            flags, tag, val = rest.split(None, 2)
            value = f'{flags} {tag} "{val.strip().strip(chr(34))}"'
        else:
            continue
        records.append({"name": fqdn.lower(), "type": rtype, "value": value})
    return records


def classify(rec: dict) -> str:
    """How this record relates to the live zone: add / singleton / override."""
    rtype, value = rec["type"], rec["value"]
    if rtype == "CNAME":
        return "singleton"
    if rtype == "MX":
        return "override"          # mail routing preference is the operator's
    if rtype == "TXT" and re.match(r"^v=spf1\b", value):
        return "override"          # estate SPF (relay-aware) wins
    if rtype == "TXT" and re.match(r"^v=DMARC1\b", value):
        return "override"          # estate DMARC (with a rua) wins
    if rtype == "CAA" and re.search(r"^\S+\s+issue\b", value):
        return "override"          # plain letsencrypt.org grant, not the accounturi pin
    return "add"                   # SRV, iodef, MTA-STS/TLS-RPT/DKIM/ua-auto-config TXT


def publish(domain: str, zone: str, text: str, dns_mode: str, args,
            client: Cerulean) -> tuple[int, int, int, int, int]:
    """Publish one domain's zone; return (added, exact, override, conflict, failed)."""
    wanted_types = {t.strip().upper() for t in args.types.split(",") if t.strip()}
    parsed_all = parse_zone(text, zone)
    parsed = [r for r in parsed_all if r["type"] in wanted_types]
    skipped_types = sorted({r["type"] for r in parsed_all} - wanted_types)

    have_values: dict[tuple[str, str], set[str]] = {}
    for r in client.records(zone):
        key = (str(r.get("name", "")).lower().rstrip("."), str(r.get("type", "")).upper())
        have_values.setdefault(key, set()).add(
            re.sub(r"\s+", " ", str(r.get("value", ""))).strip())

    print(f"domain         : {domain or '-'}  (dnsManagement={dns_mode})")
    print(f"zone           : {zone}")
    print(f"records in zone: {len(parsed)} "
          f"(types {' '.join(sorted({r['type'] for r in parsed}))}"
          + (f"; skipped {' '.join(skipped_types)}" if skipped_types else "") + ")")

    added = exact = override = conflict = failed = 0
    for rec in parsed:
        key, kind = (rec["name"], rec["type"]), classify(rec)
        present = have_values.get(key, set())
        if rec["value"] in present:
            exact += 1
            continue
        if kind == "override":
            override += 1
            if present:
                print(f"  override {rec['type']:5s} {rec['name']:38s} "
                      f"live={sorted(present)[0][:48]!r} (kept; zone template differs)")
            continue
        if kind == "singleton" and present:
            conflict += 1
            print(f"  CONFLICT {rec['type']:5s} {rec['name']:38s} "
                  f"live={sorted(present)[0][:48]!r} vs zone={rec['value'][:48]!r}")
            continue
        if not args.apply:
            print(f"  would add {rec['type']:5s} {rec['name']:38s} {rec['value'][:64]}")
            continue
        try:
            client.add_record(zone, rec["type"], rec["name"], rec["value"], ttl=args.ttl)
            print(f"  add       {rec['type']:5s} {rec['name']:38s} {rec['value'][:64]}")
            have_values.setdefault(key, set()).add(rec["value"])
            added += 1
        except CeruleanError as exc:
            print(f"  FAIL      {rec['type']:5s} {rec['name']:38s} {exc}")
            failed += 1

    print(f"added={added} exact={exact} override={override} conflict={conflict} "
          f"failed={failed}" + ("" if args.apply else "  (check only: pass --apply to write)"))
    print()
    return added, exact, override, conflict, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--domain", help="Stalwart domain to render (e.g. innotel.us)")
    ap.add_argument("--all-domains", action="store_true",
                    help="publish every domain Stalwart hosts (renewal timer)")
    ap.add_argument("--zone", help="DNS zone to write into (default: --domain)")
    ap.add_argument("--zone-file", help="use this zone text instead of Stalwart's")
    ap.add_argument("--types", default=",".join(DEFAULT_TYPES),
                    help=f"record types to publish (default {','.join(DEFAULT_TYPES)})")
    ap.add_argument("--ttl", type=int, default=3600)
    ap.add_argument("--strict", action="store_true",
                    help="treat override (estate-owned) records as drift, for CI")
    ap.add_argument("--apply", action="store_true", help="write (default is a check)")
    args = ap.parse_args()

    if not (args.domain or args.all_domains or args.zone_file):
        raise SystemExit("pass --domain, --all-domains or --zone-file")
    if args.zone and not (args.domain or args.zone_file):
        raise SystemExit("pass --zone with --zone-file")

    if args.zone_file and not args.all_domains:
        zone = (args.zone or args.domain or "").lower()
        if not zone:
            raise SystemExit("pass --zone with --zone-file")
        text = Path(args.zone_file).read_text(encoding="utf-8")
        client = Cerulean()
        a, e, o, c, f = publish(args.domain, zone, text, "(from file)", args, client)
        return 1 if f or c or (args.strict and o) else 0

    domains = list_domains() if args.all_domains else [args.domain]
    client = Cerulean()
    totals = [0, 0, 0, 0, 0]
    for domain in domains:
        zone = (args.zone or domain).lower()
        text, dns_mode = stalwart_zone(domain)
        for i, n in enumerate(publish(domain, zone, text, dns_mode, args, client)):
            totals[i] += n
    if len(domains) > 1:
        print(f"TOTAL over {len(domains)} domains: added={totals[0]} exact={totals[1]} "
              f"override={totals[2]} conflict={totals[3]} failed={totals[4]}")
    failed, conflict, override = totals[4], totals[3], totals[2]
    return 1 if failed or conflict or (args.strict and override) else 0


if __name__ == "__main__":
    sys.exit(main())
