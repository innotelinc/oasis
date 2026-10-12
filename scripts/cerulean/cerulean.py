#!/usr/bin/env python3
"""Cerulean service-bridge client — the estate's authorized DNS/certificate authority.

Cerulean (TrustOps) owns the estate's DNS: it runs Technitium and is the only
principal allowed to write to it (regular + wildcard Let's Encrypt via DNS-01
straight into Technitium). Other stacks do not hold a Technitium credential;
they hold a path-scoped Cerulean **service key** and call:

    GET  /api/service/certificates                 list certificates (own tenant)
    POST /api/service/certificates                 request issuance  {domain, wildcard, name}
    GET  /api/service/certificates/:id             status
    GET  /api/service/certificates/:id/material    fullchain PEM + private key
    GET  /api/service/dns/records?zone=ZONE        list a zone
    POST /api/service/dns/records                  add   {zone, type, name, value, ttl, priority}
    DELETE /api/service/dns/records                remove{zone, type, name, value}

All calls authenticate with `Authorization: Bearer ceru_<prefix>_<secret>`.

The key is resolved, in order, from: the `key` argument, the
`CERULEAN_SERVICE_KEY` environment variable, `CERULEAN_SERVICE_KEY_FILE`, then
`/etc/oasis/cerulean-service.key` — and, if none of those exist, from
**Cerulean Vault** (`cerulean/oasis#cerulean_service_key`), read with this
platform's path-scoped token at `VAULT_TOKEN_FILE`
(`/etc/oasis/vault-token`; Cerulean mints it via `VAULT_PRODUCT_TOKENS=oasis`).
That is where the key lives now — the plaintext file is only a legacy fallback.
Nothing secret is printed.

Used by `stalwart_install_cert.py` (fetch the mail wildcard) and
`cerulean_publish_zone.py` (publish Stalwart's DKIM/SPF/DMARC/autoconfig zone).
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE = os.environ.get("CERULEAN_API_URL", "http://192.168.1.71:3003/api")
DEFAULT_KEY_FILE = "/etc/oasis/cerulean-service.key"
DEFAULT_VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://192.168.1.71:8200")
DEFAULT_VAULT_TOKEN_FILE = "/etc/oasis/vault-token"
VAULT_KV_PATH = "cerulean/data/oasis"
VAULT_KV_KEY = "cerulean_service_key"


class CeruleanError(RuntimeError):
    pass


def load_key(key: str | None = None) -> str:
    """Resolve the service key without ever echoing it."""
    if key:
        return key.strip()
    env = os.environ.get("CERULEAN_SERVICE_KEY")
    if env:
        return env.strip()
    path = os.environ.get("CERULEAN_SERVICE_KEY_FILE", DEFAULT_KEY_FILE)
    if Path(path).is_file():
        return Path(path).read_text(encoding="utf-8").strip()
    vault = _load_key_from_vault()
    if vault:
        return vault
    raise CeruleanError(
        "no service key: pass key=, set CERULEAN_SERVICE_KEY, install one at "
        f"{path}, or expose it in Cerulean Vault at {VAULT_KV_PATH}#{VAULT_KV_KEY} "
        "with VAULT_TOKEN_FILE (Cerulean platform admin mints one via POST /api/service/keys)")


def _load_key_from_vault() -> str | None:
    """Read the service key from Cerulean Vault (KV v2) with the platform token."""
    token_file = os.environ.get("VAULT_TOKEN_FILE", DEFAULT_VAULT_TOKEN_FILE)
    if not Path(token_file).is_file():
        return None
    token = Path(token_file).read_text(encoding="utf-8").strip()
    if not token:
        return None
    addr = os.environ.get("VAULT_ADDR", DEFAULT_VAULT_ADDR).rstrip("/")
    req = urllib.request.Request(f"{addr}/v1/{VAULT_KV_PATH}")
    req.add_header("X-Vault-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp).get("data", {}).get("data", {})
    except (urllib.error.URLError, ValueError):
        return None
    value = data.get(VAULT_KV_KEY)
    return value.strip() if isinstance(value, str) and value.strip() else None


class Cerulean:
    """Thin JSON client for the Cerulean service bridge."""

    def __init__(self, key: str | None = None, base: str = DEFAULT_BASE, timeout: int = 30) -> None:
        self.base = base.rstrip("/")
        self.key = load_key(key)
        self.timeout = timeout

    def call(self, path: str, method: str = "GET", body: dict | None = None,
             query: dict | None = None):
        url = f"{self.base}{path}"
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:400]
            raise CeruleanError(f"{method} {path}: HTTP {exc.code} {detail}") from None

    # ── certificates ────────────────────────────────────────────────────────
    def certificates(self) -> list[dict]:
        return self.call("/service/certificates")[1] or []

    def certificate(self, cert_id: int) -> dict | None:
        status, body = self.call(f"/service/certificates/{cert_id}")
        return body

    def find_wildcard(self, domain: str) -> dict | None:
        """The issued wildcard certificate whose SANs cover `domain`."""
        want = f"*.{domain.lstrip('*.').lower()}"
        for cert in self.certificates():
            names = {str(n).lower() for n in (cert.get("domains") or [cert.get("name")])}
            if want in names or cert.get("name", "").lower() == want:
                return cert
        return None

    def request_certificate(self, domain: str, wildcard: bool = True,
                            name: str | None = None) -> dict:
        payload: dict = {"domain": domain, "wildcard": wildcard}
        if name:
            payload["name"] = name
        return self.call("/service/certificates", "POST", payload)[1]

    def material(self, cert_id: int) -> dict:
        return self.call(f"/service/certificates/{cert_id}/material")[1] or {}

    # ── DNS records ─────────────────────────────────────────────────────────
    def records(self, zone: str) -> list[dict]:
        return self.call("/service/dns/records", query={"zone": zone})[1] or []

    def add_record(self, zone: str, rtype: str, name: str, value: str,
                   ttl: int = 3600, priority: int | None = None) -> dict:
        body = {"zone": zone, "type": rtype.upper(), "name": name, "value": value, "ttl": ttl}
        if priority is not None:
            body["priority"] = priority
        return self.call("/service/dns/records", "POST", body)[1]

    def delete_record(self, zone: str, rtype: str, name: str,
                      value: str | None = None) -> dict:
        body = {"zone": zone, "type": rtype.upper(), "name": name}
        if value is not None:
            body["value"] = value
        return self.call("/service/dns/records", "DELETE", body)[1]


if __name__ == "__main__":  # tiny self-probe: python3 cerulean.py
    c = Cerulean()
    certs = c.certificates()
    wild = c.find_wildcard("innotel.us")
    print(f"base           : {c.base}")
    print(f"certificates   : {len(certs)}")
    print(f"*.innotel.us   : id={wild['id']} status={wild.get('status')} "
          f"hasMaterial={wild.get('hasMaterial')} expires={wild.get('expiresAt')}"
          if wild else "*.innotel.us   : not found")
