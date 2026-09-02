#!/usr/bin/env python3
"""Synchronize Oasis hostnames with Nginx Proxy Manager.

The script uses only the NPM REST API and is idempotent. It never prints
passwords or API tokens. Use --check for a read-only drift check and --no-prune
to avoid deleting old Oasis-managed hosts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

HOSTS = [
    {"key": "app", "sub": "app", "port": 3000, "websocket": True, "description": "Oasis web application"},
    {"key": "api", "sub": "api", "port": 8000, "websocket": True, "description": "Oasis API"},
    {"key": "auth", "sub": "auth", "port": 9000, "websocket": True, "description": "Authentik identity provider"},
    {"key": "mail", "sub": "mail", "port": 8080, "websocket": True, "description": "Oasis webmail"},
    {"key": "files", "sub": "files", "port": 8081, "websocket": True, "description": "Oasis file service"},
    {"key": "admin", "sub": "admin", "port": 3001, "websocket": True, "description": "Oasis administration"},
]

class NpmError(RuntimeError):
    pass


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def setting(env: dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key) or env.get(key) or default


class NpmApi:
    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, payload: Any = None) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise NpmError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise NpmError(f"NPM is unreachable: {exc.reason}") from exc

    def login(self, identity: str, secret: str) -> None:
        result = self.call("POST", "/api/tokens", {"identity": identity, "secret": secret})
        self.token = (result or {}).get("token", "")
        if not self.token:
            raise NpmError("NPM login returned no token")

    def hosts(self) -> list[dict[str, Any]]:
        return self.call("GET", "/api/nginx/proxy-hosts") or []

    def create_host(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.call("POST", "/api/nginx/proxy-hosts", payload)

    def update_host(self, host_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.call("PUT", f"/api/nginx/proxy-hosts/{host_id}", payload)

    def delete_host(self, host_id: int) -> None:
        self.call("DELETE", f"/api/nginx/proxy-hosts/{host_id}")


def desired_payload(domain: str, host: dict[str, Any], upstream: str, ssl: bool) -> dict[str, Any]:
    return {
        "domain_names": [domain],
        "forward_scheme": "http",
        "forward_host": upstream,
        "forward_port": host["port"],
        "certificate_id": 0,
        "ssl_forced": ssl,
        "http2_support": True,
        "block_exploits": True,
        "caching_enabled": False,
        "allow_websocket_upgrade": host["websocket"],
        "access_list_id": 0,
        "advanced_config": "",
        "meta": {"oasis_managed": True, "description": host["description"]},
        "locations": [],
        "enabled": True,
    }


def normalise(host: dict[str, Any]) -> dict[str, Any]:
    return {
        "domain_names": sorted(host.get("domain_names", [])),
        "forward_scheme": host.get("forward_scheme"),
        "forward_host": host.get("forward_host"),
        "forward_port": host.get("forward_port"),
        "ssl_forced": host.get("ssl_forced", False),
        "http2_support": host.get("http2_support", False),
        "block_exploits": host.get("block_exploits", False),
        "caching_enabled": host.get("caching_enabled", False),
        "allow_websocket_upgrade": host.get("allow_websocket_upgrade", False),
        "access_list_id": host.get("access_list_id", 0),
        "enabled": host.get("enabled", True),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift without changing NPM")
    parser.add_argument("--no-prune", action="store_true", help="do not remove stale Oasis-managed hosts")
    parser.add_argument("--no-ssl", action="store_true", help="do not force HTTPS")
    parser.add_argument("--include", default="", help="comma-separated host keys; default is all")
    args = parser.parse_args()

    env = load_env()
    base_domain = setting(env, "OASIS_BASE_DOMAIN", setting(env, "AUTH_DOMAIN", ""))
    upstream = setting(env, "NPM_UPSTREAM_HOST", "host.docker.internal")
    api_url = setting(env, "NPM_API_URL", "http://127.0.0.1:81")
    token = setting(env, "NPM_API_TOKEN")
    email = setting(env, "NPM_ADMIN_EMAIL")
    password = setting(env, "NPM_ADMIN_PASSWORD")
    ssl = not args.no_ssl

    if not base_domain:
        print("Set OASIS_BASE_DOMAIN or AUTH_DOMAIN", file=sys.stderr)
        return 2
    if not token and (not email or not password):
        print("Set NPM_API_TOKEN or both NPM_ADMIN_EMAIL and NPM_ADMIN_PASSWORD", file=sys.stderr)
        return 2

    selected = {x.strip() for x in args.include.split(",") if x.strip()} or {h["key"] for h in HOSTS}
    selected_hosts = [h for h in HOSTS if h["key"] in selected]
    unknown = selected - {h["key"] for h in HOSTS}
    if unknown:
        print(f"Unknown Oasis host key(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    api = NpmApi(api_url, token)
    try:
        if not token:
            api.login(email, password)
        existing = api.hosts()
    except NpmError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    by_domain = {
        domain.lower(): host for host in existing for domain in host.get("domain_names", [])
    }
    desired_domains: set[str] = set()
    failed = False

    for host in selected_hosts:
        domain = f"{host['sub']}.{base_domain}"
        desired_domains.add(domain.lower())
        payload = desired_payload(domain, host, upstream, ssl)
        current = by_domain.get(domain.lower())
        if current is None:
            print(f"CREATE {domain} -> {upstream}:{host['port']}")
            if not args.check:
                try:
                    api.create_host(payload)
                except NpmError as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    failed = True
        elif normalise(current) != normalise(payload):
            print(f"UPDATE {domain} -> {upstream}:{host['port']}")
            if not args.check:
                try:
                    api.update_host(int(current["id"]), payload)
                except NpmError as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    failed = True
        else:
            print(f"OK     {domain}")

    if not args.no_prune:
        for current in existing:
            meta = current.get("meta") or {}
            domains = {d.lower() for d in current.get("domain_names", [])}
            if meta.get("oasis_managed") is True and not domains.intersection(desired_domains):
                print(f"PRUNE  {', '.join(sorted(domains))}")
                if not args.check:
                    try:
                        api.delete_host(int(current["id"]))
                    except NpmError as exc:
                        print(f"ERROR: {exc}", file=sys.stderr)
                        failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
