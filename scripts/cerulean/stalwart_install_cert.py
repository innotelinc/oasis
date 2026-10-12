#!/usr/bin/env python3
"""Install Stalwart's server certificate from Cerulean and make it the default.

Stalwart's own ACME/DNS-01 path cannot work on this estate: it writes the
challenge through a *direct* Technitium TSIG key, and hickory-dns rejects
Technitium's MACs ("Please file an issue with hickory-dns to support truncated
HMACs with TSIG"). More importantly, Technitium is owned by **Cerulean** — the
only principal authorized to write to it. So Stalwart does not need to talk to
DNS at all for its certificate: **Cerulean already issues the estate's wildcards**
(regular + wildcard Let's Encrypt via DNS-01) and exposes the material over its
service bridge.

This fetches the `*.innotel.us` wildcard (and its chain) from Cerulean with a
`ceru_…` service key, writes it to Stalwart's TLS files, and registers it as a
file-backed `Certificate` with `SystemSettings.defaultCertificateId` pointing at
it, so SNI-less clients get a trusted certificate too.

    python3 stalwart_install_cert.py                       # check (default: Cerulean)
    python3 stalwart_install_cert.py --apply               # fetch + install
    python3 stalwart_install_cert.py --domain innotel.us --issue --apply
    python3 stalwart_install_cert.py --from file --apply   # use --cert/--key on disk

`--issue` asks Cerulean to mint the wildcard if none exists yet (Cerulean does the
Technitium DNS-01 for you), then waits for the material. Without it, a missing
certificate is reported and nothing is written.

Nothing secret is printed. The Cerulean key comes from CERULEAN_SERVICE_KEY,
CERULEAN_SERVICE_KEY_FILE, or /etc/oasis/cerulean-service.key (see cerulean.py).
"""
import argparse
import base64
import json
import os
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cerulean import Cerulean, CeruleanError  # noqa: E402

BASE = "https://mail.innotel.us"
JMAP = ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"]
DEFAULT_CERT = "/etc/stalwart/tls/fullchain.pem"
DEFAULT_KEY = "/etc/stalwart/tls/privkey.pem"
STALWART_USER = "stalwart"


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
                return json.load(r)["methodResponses"][0][1]
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"{method}: HTTP {exc.code} {exc.read().decode()[:400]}")


def decode_leaf(pem: str) -> tuple[datetime, set[str]]:
    """(notAfter, {SANs}) of the first certificate in a PEM bundle."""
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as fh:
        fh.write(pem)
        path = fh.name
    try:
        info = ssl._ssl._test_decode_cert(path)  # noqa: SLF001 - stable stdlib helper
    finally:
        os.unlink(path)
    not_after = datetime.fromtimestamp(
        ssl.cert_time_to_seconds(info["notAfter"]), tz=timezone.utc)
    sans = {str(v).lower() for (kind, v) in info.get("subjectAltName", ()) if kind == "DNS"}
    return not_after.replace(microsecond=0), sans


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(microsecond=0)
    except ValueError:
        return None


def write_secure(path: str, data: str, mode: int) -> None:
    """Atomic write; match ownership Stalwart expects when the account resolves."""
    target = Path(path)
    tmp = target.with_name(target.name + ".new")
    tmp.write_text(data, encoding="utf-8")
    os.chmod(tmp, mode)
    try:
        import grp
        import pwd
        os.chown(tmp, pwd.getpwnam(STALWART_USER).pw_uid, grp.getgrnam(STALWART_USER).gr_gid)
    except (KeyError, ImportError, PermissionError):
        pass  # keep root ownership when the stalwart account is not resolvable
    os.replace(tmp, target)


def fetch_from_cerulean(args) -> tuple[str, str, dict]:
    """Return (cert_pem, key_pem, cert_meta) for the wildcard, from Cerulean."""
    client = Cerulean()
    cert = client.find_wildcard(args.domain)
    if cert is None and args.issue:
        print(f"no wildcard for {args.domain} in Cerulean; requesting issuance…")
        client.request_certificate(args.domain, wildcard=True)
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            cert = client.find_wildcard(args.domain)
            if cert and cert.get("hasMaterial"):
                break
            time.sleep(5)
        if not cert:
            raise SystemExit(f"Cerulean did not report a wildcard for {args.domain} "
                             f"within {args.timeout}s")
    if cert is None:
        raise SystemExit(
            f"no wildcard for {args.domain} in Cerulean — pass --issue to request one")
    if not cert.get("hasMaterial"):
        raise SystemExit(
            f"Cerulean certificate {cert['id']} has no material yet "
            f"(status={cert.get('status')}, error={cert.get('error')})")

    material = client.material(cert["id"])
    if not material.get("certificate") or not material.get("key"):
        raise SystemExit(f"Cerulean certificate {cert['id']} material is incomplete")
    print(f"cerulean cert  : id={cert['id']} name={cert.get('name')} "
          f"expires={cert.get('expiresAt')}")
    return material["certificate"], material["key"], cert


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="source", choices=("cerulean", "file"),
                    default="cerulean", help="where the certificate comes from")
    ap.add_argument("--domain", default="innotel.us",
                    help="wildcard domain to fetch from Cerulean (default innotel.us)")
    ap.add_argument("--cert", default=DEFAULT_CERT, help="file-mode certificate path")
    ap.add_argument("--key", default=DEFAULT_KEY, help="file-mode private-key path")
    ap.add_argument("--issue", action="store_true",
                    help="ask Cerulean to mint the wildcard if it does not exist")
    ap.add_argument("--timeout", type=int, default=300,
                    help="seconds to wait for Cerulean issuance (with --issue)")
    ap.add_argument("--apply", action="store_true", help="fetch/write (default is a check)")
    args = ap.parse_args()

    cert_pem = key_pem = None
    source_meta: dict = {}
    if args.source == "cerulean":
        try:
            cert_pem, key_pem, source_meta = fetch_from_cerulean(args)
        except CeruleanError as exc:
            raise SystemExit(f"cerulean: {exc}")
    else:
        for path in (args.cert, args.key):
            if not Path(path).is_file():
                raise SystemExit(f"missing file: {path}")
        cert_pem = Path(args.cert).read_text(encoding="utf-8")
        key_pem = Path(args.key).read_text(encoding="utf-8")

    want_after, want_sans = decode_leaf(cert_pem)

    api = Api()
    certs = api.call("x:Certificate/get").get("list") or []
    wildcard_names = {f"*.{args.domain.lstrip('*.')}", args.domain.lstrip("*.")}
    match = next((c for c in certs
                  if wildcard_names & set((c.get("subjectAlternativeNames") or {}).keys())), None)
    settings = api.call("x:SystemSettings/get", {"ids": ["singleton"]}).get("list") or []
    current = settings[0] if settings else {}
    installed_after = installed_sans = None
    if match:
        installed_after = parse_iso(match.get("notValidAfter"))
        installed_sans = {str(s).lower() for s in (match.get("subjectAlternativeNames") or {})}

    print(f"certificates   : {len(certs)}; wildcard present: {bool(match)}")
    print(f"source         : {args.source}" +
          (f" (cert id {source_meta.get('id')})" if source_meta.get("id") else ""))
    print(f"wanted         : {want_after.isoformat()} {sorted(want_sans)}")
    print(f"installed      : {installed_after.isoformat() if installed_after else '-'} "
          f"{sorted(installed_sans) if installed_sans else ''}")
    print(f"defaultCertId  : {current.get('defaultCertificateId')}")

    same = (match and installed_after == want_after and installed_sans == want_sans
            and current.get("defaultCertificateId") == match["id"])
    if same:
        print("\nalready up to date; nothing to change")
        return 0

    if not args.apply:
        action = "update" if match else "create"
        print(f"\n--check: would {action} the wildcard certificate in Stalwart")
        return 0

    if args.source == "cerulean":
        write_secure(DEFAULT_CERT, cert_pem, 0o644)
        write_secure(DEFAULT_KEY, key_pem, 0o600)
        print(f"wrote          : {DEFAULT_CERT} + {DEFAULT_KEY}")
        ref_paths = (DEFAULT_CERT, DEFAULT_KEY)
    else:
        ref_paths = (args.cert, args.key)

    ref = lambda p: {"@type": "File", "filePath": p}          # noqa: E731
    if match:
        cert_id = match["id"]
        out = api.call("x:Certificate/set", {"update": {cert_id: {
            "certificate": ref(ref_paths[0]), "privateKey": ref(ref_paths[1])}}})
        print("certificate update:", json.dumps(out.get("updated") or out.get("notUpdated") or out)[:300])
    else:
        out = api.call("x:Certificate/set", {"create": {"tls": {
            "certificate": ref(ref_paths[0]), "privateKey": ref(ref_paths[1])}}})
        created = (out.get("created") or {}).get("tls")
        if not created:
            raise SystemExit("create failed: " + json.dumps(out.get("notCreated") or out)[:400])
        cert_id = created["id"]
        print("certificate created:", cert_id)

    after = api.call("x:Certificate/get", {"ids": [cert_id]}).get("list") or [{}]
    info = after[0]
    print("  SAN      :", sorted((info.get("subjectAlternativeNames") or {})))
    print("  issuer   :", info.get("issuer"))
    print("  valid    :", info.get("notValidBefore"), "->", info.get("notValidAfter"))

    if current.get("defaultCertificateId") != cert_id:
        out = api.call("x:SystemSettings/set", {"update": {"singleton": {
            "defaultCertificateId": cert_id}}})
        print("defaultCertificateId:", json.dumps(out.get("updated") or out.get("notUpdated") or out)[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
