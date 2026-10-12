#!/usr/bin/env python3
"""Judge the mail posture of every Stalwart domain — end to end.

Reads the domain's published zone through the **Cerulean** service bridge (the
authoritative view of what DNS actually answers) and grades the records a mail
platform is judged on:

* **MX** — at least one, and it targets this mail host;
* **SPF** — a `v=spf1` record at the apex, and whether it authorises the relay
  the estate actually sends through;
* **DMARC** — a `v=DMARC1` policy at `_dmarc`, and its `p=` stance;
* **DKIM** — the zone Stalwart generates advertises selectors
  (`*._domainkey.<domain>`); each must be published with a matching key, or a
  rotation silently breaks signing;
* **MTA-STS** — the `_mta-sts` policy and the `mta-sts` CNAME;
* **TLS-RPT** — `_smtp._tls`, so TLS failures are reported somewhere.

PASS / WARN / FAIL per line, a per-domain verdict, exit 1 on any FAIL and 2 when
a domain cannot be judged. It complements `scripts/oasis-health.sh` (which checks
the running stack) by covering what the world sees.

    python3 oasis-mail-health.py                 # every Stalwart domain
    python3 oasis-mail-health.py --domain innotel.us
    python3 oasis-mail-health.py --json
"""
import argparse
import json
import re
import sys
from pathlib import Path

CERULEAN_DIR = Path(__file__).resolve().parent / "cerulean"
sys.path.insert(0, str(CERULEAN_DIR))
from cerulean import Cerulean, CeruleanError  # noqa: E402
from cerulean_publish_zone import list_domains, parse_zone, stalwart_zone  # noqa: E402

# The relay the estate sends outbound through (ISP port 25 is blocked), so a
# correct SPF has to name it somewhere in the include chain.
RELAY_MARKERS = ("spf.mailjet.com",)


def values(records: list[dict], name: str, rtype: str) -> list[str]:
    name = name.lower().rstrip(".")
    return [str(r.get("value", "")).strip() for r in records
            if str(r.get("name", "")).lower().rstrip(".") == name
            and str(r.get("type", "")).upper() == rtype.upper()
            and not r.get("disabled")]


def grade(domain: str, records: list[dict], zone_text: str) -> list[tuple[str, str, str]]:
    apex = domain.lower()
    rows: list[tuple[str, str, str]] = []

    mx = values(records, apex, "MX")
    mx_ok = any(re.search(r"\bmail\.", v) for v in mx)
    rows.append(("MX", f"{len(mx)} record(s): {', '.join(mx) or '-'}",
                 "PASS" if mx_ok else ("FAIL" if not mx else "WARN not this mail host")))

    spf = [v for v in values(records, apex, "TXT") if v.lower().startswith("v=spf1")]
    if len(spf) > 1:
        rows.append(("SPF", f"{len(spf)} records — SPF must be a singleton", "FAIL"))
    elif not spf:
        rows.append(("SPF", "no v=spf1 record at the apex", "FAIL"))
    else:
        relay = any(m in spf[0] for m in RELAY_MARKERS)
        rows.append(("SPF", spf[0],
                     "PASS" if relay else "WARN no relay include (outbound from this host?)"))

    dmarc = [v for v in values(records, f"_dmarc.{apex}", "TXT")
             if v.lower().startswith("v=dmarc1")]
    if not dmarc:
        rows.append(("DMARC", "no v=DMARC1 record at _dmarc", "FAIL"))
    else:
        pol = re.search(r"\bp=(\w+)", dmarc[0])
        p = pol.group(1).lower() if pol else "?"
        rows.append(("DMARC", f"{dmarc[0]}",
                     "PASS" if p in ("reject", "quarantine") else "WARN p=none"))

    selectors = sorted({m.group(1) for m in re.finditer(
        r"^([\w.-]+)\._domainkey\.", zone_text, re.M)})
    if not selectors:
        rows.append(("DKIM", "no selector in Stalwart's zone", "WARN"))
    for sel in selectors:
        name = f"{sel}._domainkey.{apex}"
        want = [r["value"] for r in parse_zone(zone_text, apex) if r["name"] == name]
        live = values(records, name, "TXT")
        if not live:
            rows.append(("DKIM", f"{name} missing", "FAIL"))
        elif want and live[0] != want[0]:
            rows.append(("DKIM", f"{name} differs from Stalwart's key", "FAIL"))
        else:
            rows.append(("DKIM", f"{sel} published", "PASS"))

    mta = [v for v in values(records, f"_mta-sts.{apex}", "TXT")
           if v.lower().startswith("v=stsv1")]
    cname = values(records, f"mta-sts.{apex}", "CNAME")
    rows.append(("MTA-STS", f"_mta-sts={'yes' if mta else 'no'} cname={'yes' if cname else 'no'}",
                 "PASS" if (mta and cname) else "WARN"))

    tlsrpt = [v for v in values(records, f"_smtp._tls.{apex}", "TXT")
              if v.lower().startswith("v=tlsrptv1")]
    rows.append(("TLS-RPT", "policy published" if tlsrpt else "no _smtp._tls record",
                 "PASS" if tlsrpt else "WARN"))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--domain", action="append", help="limit to this domain (repeatable)")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    args = ap.parse_args()

    try:
        client = Cerulean()
        domains = args.domain or list_domains()
        if args.domain:
            available = set(list_domains())
            missing = set(args.domain) - available
            if missing:
                raise SystemExit(f"not a Stalwart domain: {', '.join(sorted(missing))}")
    except CeruleanError as exc:
        print(f"cannot judge: {exc}", file=sys.stderr)
        return 2

    report, failed, warned = {}, 0, 0
    for domain in domains:
        try:
            records = client.records(domain)
            text, _ = stalwart_zone(domain)
        except CeruleanError as exc:
            print(f"cannot judge {domain}: {exc}", file=sys.stderr)
            return 2
        rows = grade(domain, records, text)
        report[domain] = [{"check": c, "detail": d, "result": r} for c, d, r in rows]
        bad = [r for _, _, r in rows if r.startswith("FAIL")]
        warn = [r for _, _, r in rows if r.startswith("WARN")]
        failed += len(bad)
        warned += len(warn)

    if args.json:
        print(json.dumps({"domains": report, "fail": failed, "warn": warned}, indent=1))
    else:
        for domain, rows in report.items():
            width = max(len(r["check"]) for r in rows)
            print(f"\n{domain}")
            for r in rows:
                mark = r["result"].split()[0]
                extra = r["result"][len(mark):].strip()
                print(f"  {r['check']:{width}s}  {mark:4s} {r['detail']}"
                      + (f"  [{extra}]" if extra else ""))
        print(f"\n{failed} failure(s), {warned} warning(s) across {len(report)} domain(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
