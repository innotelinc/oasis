#!/usr/bin/env python3
"""Receive Resend's `email.received` webhook and inject the message into Stalwart.

Why this exists
---------------
The estate's public IP is on Comcast, which blocks **inbound** port 25, so MX
delivery to `mail.innotel.us` can never work. Inbound mail is therefore received
by Resend (MX `inbound-smtp.us-east-1.amazonaws.com`) and this service puts it
back into the local Stalwart, which owns the real mailbox.

Flow
----
1. Resend POSTs an `email.received` event. The payload carries metadata only.
2. Fetch the original message: `GET /emails/receiving/{email_id}`, then download
   the raw MIME from `raw.download_url`. Injecting the *raw* message preserves
   the sender, headers and DKIM signature, rather than rebuilding it from the
   parsed HTML/text.
3. Hand that message to Stalwart over SMTP from loopback - the listener path the
   server is already configured to treat as a relay (SPF not re-evaluated, its
   spam filter skipped, Received header added).

A webhook is retried on any non-2xx, so delivery is recorded per `email_id`
before the response is sent; a retry of a message already injected is answered
with 200 and not delivered twice.

Config (`/etc/oasis/resend-inbound.env`, all optional except the API key):
    RESEND_API_KEY=re_...            API key used to fetch received messages
    RESEND_WEBHOOK_SECRET=whsec_...  verify the Svix signature when set
    LISTEN=127.0.0.1:8099            bind address
    STALWART_SMTP=127.0.0.1:25       where to inject
    LOCAL_DOMAINS=innotel.us,signara.innotel.us
    STATE_FILE=/var/lib/resend-inbound/delivered.jsonl
"""
import base64
import hashlib
import hmac
import json
import os
import smtplib
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API = "https://api.resend.com"
ENV_FILE = Path(os.environ.get("RESEND_INBOUND_ENV", "/etc/oasis/resend-inbound.env"))

DEFAULTS = {
    "LISTEN": "127.0.0.1:8099",
    "STALWART_SMTP": "127.0.0.1:25",
    "LOCAL_DOMAINS": "innotel.us,signara.innotel.us",
    "STATE_FILE": "/var/lib/resend-inbound/delivered.jsonl",
    "RESEND_API_KEY": "",
    "RESEND_WEBHOOK_SECRET": "",
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if ENV_FILE.is_file():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip().strip('"').strip("'")
    for key in cfg:
        cfg[key] = os.environ.get(key, cfg[key])
    return cfg


CFG = load_config()
DOMAINS = {d.strip().lower() for d in CFG["LOCAL_DOMAINS"].split(",") if d.strip()}
STATE = Path(CFG["STATE_FILE"])
_lock = threading.Lock()


# ── helpers ─────────────────────────────────────────────────────────────────
def log(event: str, **fields) -> None:
    line = " ".join(f"{k}={v!r}" if " " in str(v) else f"{k}={v}" for k, v in fields.items())
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {event} {line}".rstrip(),
          flush=True)


def http(url: str, *, auth: bool = False, method: str = "GET") -> bytes:
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", "resend-inbound/1.0")
    req.add_header("Accept", "*/*")
    if auth:
        req.add_header("Authorization", f"Bearer {CFG['RESEND_API_KEY']}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def already_delivered(email_id: str) -> bool:
    """True when this event was injected before (webhook retry)."""
    if not STATE.is_file():
        return False
    for line in STATE.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(line).get("email_id") == email_id:
                return True
        except ValueError:
            continue
    return False


def remember(email_id: str, recipients: list[str], message_id: str) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"email_id": email_id, "recipients": recipients,
                             "message_id": message_id, "at": int(time.time())}) + "\n")


def verify_signature(body: bytes, headers) -> bool:
    """Svix signature check, as Resend sends it. Skipped when no secret is set."""
    secret = CFG["RESEND_WEBHOOK_SECRET"]
    if not secret:
        return True
    msg_id, timestamp, signature = (headers.get("svix-id"), headers.get("svix-timestamp"),
                                    headers.get("svix-signature"))
    if not (msg_id and timestamp and signature):
        return False
    key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    signed = b".".join([msg_id.encode(), timestamp.encode(), body])
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return any(part.split(",", 1)[1] == expected
               for part in signature.split()
               if part.startswith("v1,") and len(part.split(",", 1)) == 2)


def is_local(address: str) -> bool:
    _, _, domain = address.rpartition("@")
    return domain.strip().lower() in DOMAINS


def raw_message(email_id: str) -> bytes:
    """The original message bytes for a received email."""
    meta = json.loads(http(f"{API}/emails/receiving/{email_id}", auth=True) or b"{}")
    download = (meta.get("raw") or {}).get("download_url")
    if download:
        try:
            return http(download)
        except urllib.error.HTTPError as exc:
            log("raw-download-failed", email_id=email_id, status=exc.code)
        try:
            return http(download, auth=True)
        except urllib.error.HTTPError as exc:
            log("raw-download-failed-authed", email_id=email_id, status=exc.code)
    return rebuild_mime(meta)


def rebuild_mime(meta: dict) -> bytes:
    """Fallback: reconstruct a message from the parsed body when raw is absent."""
    headers = meta.get("headers") or {}
    body = meta.get("html") or meta.get("text") or ""
    ctype = "text/html" if meta.get("html") else "text/plain"
    lines = []
    for name in ("from", "to", "subject", "message-id", "date"):
        if headers.get(name):
            lines.append(f"{'-'.join(p.capitalize() for p in name.split('-'))}: {headers[name]}")
    lines += ["MIME-Version: 1.0", f"Content-Type: {ctype}; charset=utf-8", "", body]
    return "\r\n".join(lines).encode("utf-8", "replace")


def inject(sender: str, recipients: list[str], message: bytes) -> list[str]:
    """Hand the message to Stalwart; return the recipients that were accepted."""
    host, _, port = CFG["STALWART_SMTP"].rpartition(":")
    accepted = []
    with smtplib.SMTP(host or "127.0.0.1", int(port or "25"), timeout=60) as smtp:
        smtp.ehlo()
        code, _ = smtp.mail(sender)
        if code >= 400:
            raise RuntimeError(f"MAIL FROM refused: {code}")
        for rcpt in recipients:
            code, _ = smtp.rcpt(rcpt)
            if code < 400:
                accepted.append(rcpt)
            else:
                log("recipient-rejected", rcpt=rcpt, code=code)
        if accepted:
            code, detail = smtp.data(message)
            if code >= 400:
                raise RuntimeError(f"DATA refused: {code} {detail!r}")
    return accepted


def handle_received(data: dict) -> None:
    email_id = data.get("email_id")
    if not email_id:
        raise RuntimeError("event has no email_id")
    sender = (data.get("from") or "").strip("<>")
    candidates = list(data.get("received_for") or []) + list(data.get("to") or [])
    recipients = [a for a in dict.fromkeys(candidates) if is_local(a)]
    if not recipients:
        log("no-local-recipient", email_id=email_id, to=candidates)
        return
    if already_delivered(email_id):
        log("duplicate-skipped", email_id=email_id)
        return
    message = raw_message(email_id)
    if not message:
        raise RuntimeError("empty message")
    accepted = inject(sender or "unknown@localhost", recipients, message)
    if not accepted:
        # Stalwart refused every recipient (no such mailbox). Raising here would
        # have Resend retry the same message forever, so the event is recorded as
        # handled and reported loudly instead - the sender's server is the place
        # that can bounce it, and it already accepted this message.
        remember(email_id, [], str(data.get("message_id") or ""))
        log("dropped-no-mailbox", email_id=email_id, sender=sender,
            recipients=",".join(recipients))
        return
    remember(email_id, accepted, str(data.get("message_id") or ""))
    log("injected", email_id=email_id, sender=sender, recipients=",".join(accepted),
        bytes=len(message))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/healthz", ""):
            self._reply(200, {"status": "ok", "domains": sorted(DOMAINS)})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in ("/resend", "/hooks/resend"):
            self._reply(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if not verify_signature(body, self.headers):
            log("signature-invalid", ip=self.client_address[0])
            self._reply(401, {"error": "invalid signature"})
            return
        try:
            event = json.loads(body or b"{}")
        except ValueError:
            self._reply(400, {"error": "invalid json"})
            return
        if event.get("type") != "email.received":
            self._reply(200, {"ignored": event.get("type")})
            return
        with _lock:
            try:
                handle_received(event.get("data") or {})
            except Exception as exc:  # noqa: BLE001 - surface the reason to Resend's retry
                log("inject-failed", error=f"{type(exc).__name__}: {exc}")
                self._reply(500, {"error": str(exc)})
                return
        self._reply(200, {"ok": True})

    def log_message(self, fmt: str, *args) -> None:  # silence the default stderr access log
        return


def main() -> int:
    if not CFG["RESEND_API_KEY"]:
        print("RESEND_API_KEY is not set", file=sys.stderr)
        return 2
    host, _, port = CFG["LISTEN"].rpartition(":")
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    log("listening", addr=f"{host}:{port}", smtp=CFG["STALWART_SMTP"],
        domains=",".join(sorted(DOMAINS)),
        signature="on" if CFG["RESEND_WEBHOOK_SECRET"] else "off")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
