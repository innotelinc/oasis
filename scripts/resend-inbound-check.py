#!/usr/bin/env python3
"""Watch the Resend inbound path and shout if it stops delivering.

The path is:  sender -> public MX (Resend) -> webhook -> this host's receiver ->
Stalwart. Every hop can break on its own - the receiving MX can fall out of DNS,
Resend can flip the domain back to unverified, the webhook can be deleted, the
edge proxy can lose the host, or the receiver can stop. Nothing in the estate
watches any of it, so a break looks the same as a quiet mailbox.

The check probes each hop from the outside in, and alerts on a **change of
state**, not on every run, so a persistent fault pages once instead of every 15
minutes.

    python3 resend_inbound_check.py            # check, print, alert on change
    python3 resend_inbound_check.py --quiet    # only print failures
    python3 resend_inbound_check.py --reset    # forget the last state

Alert channels (both optional, either is enough):
  * email to `ALERT_TO` through the local Stalwart - this keeps working when the
    inbound path is broken, because local delivery does not use it.
  * ntfy, when NTFY_URL / NTFY_TOPIC / NTFY_TOKEN are set.

Exit codes: 0 healthy, 1 broken, 2 the check itself could not run.
"""
import argparse
import json
import smtplib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILE = Path("/etc/oasis/resend-inbound.env")
CHECK_ENV_FILE = Path("/etc/oasis/resend-inbound-check.env")

DEFAULTS = {
    "HEALTH_URL": "https://resend.innotel.us/healthz",
    "MX_DOMAIN": "innotel.us",
    "MX_HOST": "inbound-smtp.us-east-1.amazonaws.com",
    "RESEND_API_KEY": "",
    "ALERT_TO": "admin@innotel.us",
    "ALERT_FROM": "Mail Health Check <postmaster@innotel.us>",
    "SMTP": "127.0.0.1:587",
    "INJECT": "127.0.0.1:25",
    # Deliberately *not* STATE_FILE: /etc/oasis/resend-inbound.env sets that to
    # the receiver's delivery journal, and sharing it would make this check
    # overwrite the journal every 15 minutes - losing the receiver's
    # already-delivered history, so a webhook retry would be injected twice.
    "CHECK_STATE_FILE": "/var/lib/resend-inbound/check-state.json",
    "NTFY_URL": "",
    "NTFY_TOPIC": "",
    "NTFY_TOKEN": "",
}


def load_env(path: Path, into: dict) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        into[key.strip()] = value.strip().strip('"').strip("'")


CFG = dict(DEFAULTS)
load_env(ENV_FILE, CFG)
load_env(CHECK_ENV_FILE, CFG)
STATE = Path(CFG["CHECK_STATE_FILE"])


# ── individual probes ───────────────────────────────────────────────────────
def probe_service() -> tuple[bool, str]:
    out = subprocess.run(["systemctl", "is-active", "resend-inbound"],
                         capture_output=True, text=True)
    state = out.stdout.strip()
    return state == "active", f"resend-inbound is {state or 'unknown'}"


def probe_smtp() -> tuple[bool, str]:
    host, _, port = CFG["SMTP"].rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port or "587")), timeout=10):
            return True, f"submission {host}:{port} reachable"
    except OSError as exc:
        return False, f"submission {host}:{port} unreachable: {exc}"


def probe_health() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(CFG["HEALTH_URL"], timeout=20) as resp:
            body = json.loads(resp.read() or b"{}")
        if body.get("status") != "ok":
            return False, f"{CFG['HEALTH_URL']} returned {body!r}"
        return True, f"{CFG['HEALTH_URL']} ok for {','.join(body.get('domains', []))}"
    except Exception as exc:  # noqa: BLE001 - any failure here is the finding
        return False, f"{CFG['HEALTH_URL']} failed: {type(exc).__name__}: {exc}"


def probe_mx() -> tuple[bool, str]:
    url = ("https://dns.google/resolve?name=" + CFG["MX_DOMAIN"] + "&type=MX")
    req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            answers = json.load(resp).get("Answer", [])
    except Exception as exc:  # noqa: BLE001
        return False, f"could not resolve MX for {CFG['MX_DOMAIN']}: {exc}"
    ranked = []
    for record in answers:
        data = str(record.get("data", "")).rstrip(".").split()
        if len(data) == 2:
            ranked.append((int(data[0]), data[1]))
    if not ranked:
        return False, f"{CFG['MX_DOMAIN']} has no MX records"
    best = min(ranked)[1]
    if best != CFG["MX_HOST"]:
        return False, (f"{CFG['MX_DOMAIN']} MX prefers {best!r}, not {CFG['MX_HOST']!r} "
                       f"(mail is not reaching Resend)")
    return True, f"MX prefers {best} (priority {min(ranked)[0]})"


def resend(path: str) -> dict:
    req = urllib.request.Request("https://api.resend.com" + path, headers={
        "Authorization": f"Bearer {CFG['RESEND_API_KEY']}",
        "Accept": "application/json",
        # Resend sits behind Cloudflare, which rejects the default urllib agent.
        "User-Agent": "curl/8.5.0",
    })
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.load(resp)


def probe_resend() -> tuple[bool, str]:
    if not CFG["RESEND_API_KEY"]:
        return True, "resend: no API key configured, skipped"
    try:
        domains = resend("/domains").get("data", [])
    except Exception as exc:  # noqa: BLE001
        return False, f"resend: cannot list domains: {exc}"
    problems = []
    summary = []
    for domain in domains:
        if (domain.get("capabilities") or {}).get("receiving") != "enabled":
            continue
        detail = resend(f"/domains/{domain['id']}")
        receiving = [r for r in detail.get("records", []) if r.get("record") == "Receiving"]
        status = receiving[0].get("status") if receiving else "missing"
        summary.append(f"{domain['name']}={status}")
        if status != "verified":
            problems.append(f"{domain['name']} receiving record is {status}")
    try:
        hooks = resend("/webhooks").get("data", [])
    except Exception as exc:  # noqa: BLE001
        return False, f"resend: cannot list webhooks: {exc}"
    if not [h for h in hooks if "email.received" in (h.get("events") or [])]:
        problems.append("no webhook subscribed to email.received")
    if problems:
        return False, "; ".join(problems)
    return True, "resend: " + (", ".join(summary) or "no receiving domains") + \
        f", {len(hooks)} webhook(s)"


CHECKS = (("service", probe_service), ("smtp", probe_smtp), ("health", probe_health),
          ("mx", probe_mx), ("resend", probe_resend))


# ── alerting ────────────────────────────────────────────────────────────────
def alert_email(subject: str, body: str) -> str:
    """Deliver locally over the loopback listener.

    Authenticated submission cannot be used: the estate's accessible credential
    is the recovery admin, which owns no address that Stalwart lets it send
    from (`501 You are not allowed to send from this address`). Injecting on
    loopback is the same path the inbound receiver uses, needs no credential at
    all, and still works when the Resend path is down because it never leaves
    the host.
    """
    host, _, port = CFG["INJECT"].rpartition(":")
    message = (f"From: {CFG['ALERT_FROM']}\r\nTo: {CFG['ALERT_TO']}\r\n"
               f"Subject: {subject}\r\nDate: {time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime())}\r\n"
               f"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n")
    try:
        with smtplib.SMTP(host or "127.0.0.1", int(port or "25"), timeout=30) as smtp:
            smtp.ehlo()
            code, _ = smtp.mail("mailcheck@localhost")
            if code >= 400:
                raise RuntimeError(f"MAIL FROM refused: {code}")
            code, _ = smtp.rcpt(CFG["ALERT_TO"])
            if code >= 400:
                raise RuntimeError(f"RCPT TO refused: {code}")
            code, detail = smtp.data(message.encode("utf-8"))
            if code >= 400:
                raise RuntimeError(f"DATA refused: {code} {detail!r}")
        return f"email: delivered to {CFG['ALERT_TO']}"
    except Exception as exc:  # noqa: BLE001
        return f"email: failed: {type(exc).__name__}: {exc}"


def alert_ntfy(subject: str, body: str) -> str:
    if not (CFG["NTFY_URL"] and CFG["NTFY_TOPIC"] and CFG["NTFY_TOKEN"]):
        return "ntfy: not configured"
    req = urllib.request.Request(
        CFG["NTFY_URL"].rstrip("/") + "/" + CFG["NTFY_TOPIC"],
        data=body.encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {CFG['NTFY_TOKEN']}")
    req.add_header("Title", subject)
    req.add_header("Priority", "high")
    req.add_header("Tags", "warning,email")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return f"ntfy: HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return f"ntfy: failed: {type(exc).__name__}: {exc}"


def notify(broken: bool, failures: list[str]) -> list[str]:
    subject = ("[mail] Resend inbound is broken" if broken
               else "[mail] Resend inbound recovered")
    header = ("Inbound mail for this estate is NOT being delivered."
              if broken else "Inbound mail is flowing again.")
    body = f"{header}\n\n" + "\n".join(f"- {f}" for f in failures) + \
        f"\n\nChecked {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
    results = [alert_email(subject, body)]
    results.append(alert_ntfy(subject, body))
    return results


# ── main ────────────────────────────────────────────────────────────────────
def read_state() -> bool | None:
    if not STATE.is_file():
        return None
    try:
        return bool(json.loads(STATE.read_text(encoding="utf-8")).get("broken"))
    except ValueError:
        return None


def write_state(broken: bool, failures: list[str]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"broken": broken, "failures": failures,
                                 "at": int(time.time())}), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quiet", action="store_true", help="print only failures")
    ap.add_argument("--reset", action="store_true", help="forget the last state")
    ap.add_argument("--test-alert", action="store_true",
                    help="send a test alert on every channel and exit")
    args = ap.parse_args()

    if args.test_alert:
        for result in notify(True, ["this is a test of the alert channels"]):
            print(result)
        return 0

    if args.reset and STATE.is_file():
        STATE.unlink()

    failures, reports, broken = [], [], False
    for name, probe in CHECKS:
        try:
            ok, detail = probe()
        except Exception as exc:  # noqa: BLE001 - a crashing probe is a failure
            ok, detail = False, f"{name} probe crashed: {type(exc).__name__}: {exc}"
        reports.append(f"  {'ok  ' if ok else 'FAIL'} {name:8s} {detail}")
        if not ok:
            broken, _ = True, failures.append(f"{name}: {detail}")

    previous = read_state()
    if not args.quiet or broken:
        print(f"resend-inbound check {'BROKEN' if broken else 'healthy'}")
        print("\n".join(reports))

    if previous is None or previous != broken:
        results = notify(broken, failures or ["(no detail)"])
        print(("  ALERT " if broken else "  NOTE  ") + "; ".join(results))
        write_state(broken, failures)
    elif broken:
        print("  still broken; already alerted (no repeat)")
    return 1 if broken else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"check could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
