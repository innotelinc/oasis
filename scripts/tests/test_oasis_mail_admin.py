#!/usr/bin/env python3
"""What the mail administration service must do, judged without the estate.

These tests never touch Stalwart, Authentik or Cerulean: `services()` is given
fakes and the Cerulean writes are stubbed, so what is judged is this script's own
decisions — that a tenant is built in one write, that a second identical write
changes nothing and hands back no new password, and that a machine caller is
admitted only on the trust owner's word.

Run:  python3 -m unittest discover -s scripts/tests
"""

import importlib.util
import json
import os
import sys
import types
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "oasis-mail-admin.py"

os.environ.setdefault("SESSION_SECRET", "test-session-secret")

SPEC = importlib.util.spec_from_file_location("oasis_mail_admin", SCRIPT)
mail_admin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mail_admin)


# ── the two planes, as fakes ────────────────────────────────────────────────
class FakeAuthentik:
    """Enough of the Authentik admin API to hold identities and their groups."""

    def __init__(self):
        self.catalog = {"Mail": 1, "Oasis": 2, "paid_users": 3}
        self.by_email = {}
        self.created = []
        self.passwords = []
        self.active_changes = []

    def users(self):
        return list(self.by_email.values())

    def user(self, email):
        return self.by_email.get((email or "").lower())

    def group_ids(self, names):
        ids = []
        for name in names:
            if name.lower() not in {n.lower() for n in self.catalog}:
                raise RuntimeError(f"Authentik group {name!r} does not exist")
            ids.append(self.catalog[name])
        return ids

    def user_groups(self, user):
        return user["groups_obj"]

    def create_user(self, username, email, name, group_ids):
        if any(u["username"].lower() == username.lower() for u in self.by_email.values()):
            # What the real Authentik answers — the collision a second tenant's
            # `postmaster@` used to die on.
            raise RuntimeError('authentik POST /api/v3/core/users/: HTTP 400 '
                               '{"username":["This field must be unique."]}')
        user = {"pk": len(self.by_email) + 1, "username": username, "email": email,
                "name": name, "is_active": True,
                "groups_obj": sorted(n for n, i in self.catalog.items() if i in group_ids)}
        self.by_email[email.lower()] = user
        self.created.append(email.lower())
        return user

    def set_groups(self, pk, group_ids):
        for user in self.by_email.values():
            if user["pk"] == pk:
                user["groups_obj"] = sorted(n for n, i in self.catalog.items()
                                            if i in group_ids)

    def set_active(self, pk, active):
        for user in self.by_email.values():
            if user["pk"] == pk:
                user["is_active"] = active
        self.active_changes.append((pk, active))

    def set_password(self, pk, password):
        self.passwords.append(password)

    def delete_user(self, pk):
        for email, user in list(self.by_email.items()):
            if user["pk"] == pk:
                del self.by_email[email]


class FakeStalwart:
    """Enough of the Stalwart management API to hold domains and mailboxes."""

    def __init__(self):
        self.domains_by_name = {}
        self.accounts_by_email = {}
        self.destroyed_domains = []
        self.destroyed_accounts = []

    def domains(self):
        return list(self.domains_by_name.values())

    def domain(self, name):
        return self.domains_by_name.get((name or "").lower())

    def create_domain(self, name, description):
        record = {"id": f"d{len(self.domains_by_name) + 1}", "name": name.lower(),
                  "isEnabled": True, "description": description}
        self.domains_by_name[name.lower()] = record
        return record

    def destroy_domain(self, name):
        self.destroyed_domains.append(name)
        self.domains_by_name.pop(name.lower(), None)

    def accounts(self):
        return list(self.accounts_by_email.values())

    def account(self, address):
        return self.accounts_by_email.get((address or "").lower())

    def create_mailbox(self, address, description):
        record = {"id": f"a{len(self.accounts_by_email) + 1}", "@type": "User",
                  "emailAddress": address.lower(), "description": description}
        self.accounts_by_email[address.lower()] = record
        return record

    def destroy_account(self, account_id):
        self.destroyed_accounts.append(account_id)
        for email, record in list(self.accounts_by_email.items()):
            if record["id"] == account_id:
                del self.accounts_by_email[email]


def fake_cerulean(whoami=None, domains=None, whoami_raises=None, domains_raises=None,
                  records=None, records_raises=None):
    """A stand-in for the bridge client module, so nothing leaves the process.

    A refusal is described as `(kind, message)` rather than as an exception
    instance: the exception class is created here, and a test cannot name it
    before this call returns.
    """
    module = types.ModuleType("cerulean")
    writes: list = []

    class CeruleanError(RuntimeError):
        pass

    def refuse(spec):
        kind, message = spec
        if kind == "url":
            raise urllib.error.URLError(message)
        if kind == "timeout":
            raise TimeoutError(message)
        if kind == "boom":
            raise ValueError(message)
        raise CeruleanError(message)

    class Cerulean:
        def __init__(self, key=None, base="", timeout=30):
            self.key = key

        def whoami(self):
            if whoami_raises:
                refuse(whoami_raises)
            return whoami or {}

        def domains(self):
            if domains_raises:
                refuse(domains_raises)
            return domains if domains is not None else []

        def records(self, zone):
            if records_raises:
                refuse(records_raises)
            return records if records is not None else []

        def add_record(self, zone, rtype, name, value, ttl=3600, priority=None):
            writes.append({"zone": zone, "type": rtype, "name": name, "value": value})
            return {"ok": True}

    Cerulean.writes = writes
    module.Cerulean = Cerulean
    module.CeruleanError = CeruleanError
    return module, CeruleanError


class ProvisioningTest(unittest.TestCase):
    """One write builds a tenant; the same write again builds nothing."""

    def setUp(self):
        self.ak, self.sw = FakeAuthentik(), FakeStalwart()
        mail_admin.STATE.update(authentik=self.ak, stalwart=self.sw, oidc=object())
        self.calls = []

        def zone(name, key=""):
            self.calls.append(("zone", name, key))
            return {"domain": name, "created": True}

        def dns(name, key=""):
            self.calls.append(("dns", name, key))
            return {"domain": name, "added": 3}

        self.original = (mail_admin.create_zone, mail_admin.publish_dns)
        mail_admin.create_zone = zone
        mail_admin.publish_dns = dns

    def tearDown(self):
        mail_admin.create_zone, mail_admin.publish_dns = self.original
        mail_admin.STATE.update(authentik=None, stalwart=None, oidc=None)

    def provision(self, domain="acme.innotel.us", email="jane@acme.innotel.us", **kw):
        return mail_admin.provision_tenant(domain, email, **kw)

    def test_one_write_creates_the_domain_the_zone_and_the_account(self):
        result = self.provision(name="Jane Doe", groups=["paid_users"])
        self.assertFalse(result["existed"])
        self.assertTrue(result["password"])
        self.assertTrue(self.sw.domain("acme.innotel.us"))
        self.assertTrue(self.sw.account("jane@acme.innotel.us"))
        self.assertTrue(self.ak.user("jane@acme.innotel.us"))
        self.assertEqual(["Mail", "paid_users"], result["account"]["groups"])
        self.assertEqual([("zone", "acme.innotel.us", ""), ("dns", "acme.innotel.us", "")],
                         self.calls)

    def test_the_zone_is_minted_before_records_are_published(self):
        self.provision()
        self.assertEqual(["zone", "dns"], [c[0] for c in self.calls])

    def test_a_retry_creates_nothing_and_returns_no_new_password(self):
        first = self.provision()
        second = self.provision()
        self.assertFalse(first["existed"])
        self.assertTrue(second["existed"])
        self.assertIsNone(second["password"])
        self.assertEqual(1, len(self.ak.created))
        self.assertEqual(1, len(self.ak.passwords))
        self.assertEqual(1, len(self.sw.domains_by_name))
        self.assertEqual(1, len(self.sw.accounts_by_email))

    def test_a_retry_keeps_the_password_the_first_call_handed_out(self):
        first = self.provision()
        self.provision()
        self.assertEqual(1, len(self.ak.passwords))
        self.assertEqual(first["password"], self.ak.passwords[0])

    def test_adding_a_second_account_to_a_tenant_does_not_touch_the_domain(self):
        self.provision()
        self.calls.clear()
        result = self.provision(email="sam@acme.innotel.us", name="Sam")
        # A second address is a new account, so this call did create something and
        # hands back that account's password — but the domain is left alone.
        self.assertFalse(result["existed"])
        self.assertTrue(result["password"])
        self.assertTrue(self.sw.account("sam@acme.innotel.us"))
        self.assertEqual(1, len(self.sw.domains_by_name))
        self.assertEqual(2, len(self.sw.accounts_by_email))

    def test_reprovisioning_merges_groups_rather_than_replacing_them(self):
        self.provision(groups=["paid_users"])
        result = self.provision(groups=["Oasis"])
        # The answer describes the account as it now stands, not just the ask.
        self.assertEqual(["Mail", "Oasis", "paid_users"], result["account"]["groups"])
        self.assertIn("paid_users", self.ak.user("jane@acme.innotel.us")["groups_obj"])
        self.assertIn("Oasis", self.ak.user("jane@acme.innotel.us")["groups_obj"])

    def test_the_callers_cerulean_key_is_used_when_it_is_offered(self):
        result = self.provision(key="ceru_abc_def")
        self.assertEqual("caller's Cerulean key", result["authority"])
        self.assertEqual({"ceru_abc_def"}, {c[2] for c in self.calls})

    def test_the_platform_key_is_named_as_the_authority_when_no_key_is_offered(self):
        result = self.provision()
        self.assertEqual("platform Cerulean key", result["authority"])
        self.assertEqual({""}, {c[2] for c in self.calls})

    def test_the_first_account_must_live_in_the_tenant_domain(self):
        with self.assertRaises(RuntimeError):
            self.provision(domain="acme.innotel.us", email="jane@other.innotel.us")

    def test_a_protected_zone_cannot_be_provisioned(self):
        with self.assertRaises(RuntimeError):
            self.provision(domain="innotel.us", email="jane@innotel.us")

    def test_a_domain_that_is_not_a_domain_is_refused(self):
        for domain in ("", "localhost"):
            with self.assertRaises(RuntimeError):
                self.provision(domain=domain, email=f"jane@{domain or 'x'}")


class DomainTest(unittest.TestCase):
    def setUp(self):
        self.ak, self.sw = FakeAuthentik(), FakeStalwart()
        mail_admin.STATE.update(authentik=self.ak, stalwart=self.sw, oidc=object())
        self.original = (mail_admin.create_zone, mail_admin.publish_dns)
        mail_admin.create_zone = lambda name, key="": {"domain": name, "created": True}
        mail_admin.publish_dns = lambda name, key="": {"domain": name, "added": 0}

    def tearDown(self):
        mail_admin.create_zone, mail_admin.publish_dns = self.original
        mail_admin.STATE.update(authentik=None, stalwart=None, oidc=None)

    def test_add_refuses_a_duplicate_but_ensure_reports_it(self):
        mail_admin.add_domain("acme.innotel.us", None)
        with self.assertRaises(RuntimeError):
            mail_admin.add_domain("acme.innotel.us", None)
        again = mail_admin.add_domain("acme.innotel.us", None, existing_ok=True)
        self.assertFalse(again["created"])
        self.assertEqual(1, len(self.sw.domains_by_name))

    def test_removing_something_that_is_not_here_is_a_not_found(self):
        with self.assertRaises(mail_admin.NotFound):
            mail_admin.remove_domain("gone.innotel.us")
        with self.assertRaises(mail_admin.NotFound):
            mail_admin.remove_user("nobody@innotel.us")

    def test_a_free_localpart_is_the_sign_in_name(self):
        self.sw.create_domain("acme.innotel.us", None)
        mail_admin.add_user("jane@acme.innotel.us", "Jane", [], rotate=False)
        self.assertEqual("jane", self.ak.user("jane@acme.innotel.us")["username"])

    def test_a_localpart_somebody_else_holds_does_not_stop_the_tenant(self):
        """The estate already has a `postmaster`, and every domain is expected to.

        A mailbox's localpart is unique only inside its domain while an Authentik
        username is global, so the second tenant whose first mailbox is
        `postmaster@` used to die on Authentik's raw "must be unique" — for a name
        the mail host chose, not one the caller asked for. The identity is named
        by the whole address instead, which is unique because the address is.
        """
        self.ak.by_email["postmaster@denovocredit.com"] = {
            "pk": 99, "username": "postmaster", "email": "postmaster@denovocredit.com",
            "name": "postmaster", "is_active": True, "groups_obj": ["Mail"]}
        self.sw.create_domain("acme.innotel.us", None)

        result = mail_admin.add_user("postmaster@acme.innotel.us", "", [], rotate=False)

        self.assertTrue(result["created"])
        self.assertEqual("postmaster@acme.innotel.us",
                         self.ak.user("postmaster@acme.innotel.us")["username"])
        self.assertEqual("postmaster", self.ak.user("postmaster@denovocredit.com")["username"])


class SignInNameTest(unittest.TestCase):
    """The name an identity gets when its mailbox localpart is already taken."""

    def test_the_localpart_is_the_name_until_it_belongs_to_somebody_else(self):
        self.assertEqual("jane",
                         mail_admin.sign_in_name("jane@acme.innotel.us",
                                                 {"postmaster", "ops"}))
        self.assertEqual("postmaster@acme.innotel.us",
                         mail_admin.sign_in_name("postmaster@acme.innotel.us",
                                                 {"postmaster"}))

    def test_the_comparison_ignores_case_and_the_answer_is_normalised(self):
        self.assertEqual("postmaster@acme.innotel.us",
                         mail_admin.sign_in_name("Postmaster@Acme.Innotel.US",
                                                 {"Postmaster"}))
        self.assertEqual("jane", mail_admin.sign_in_name("Jane@acme.innotel.us", set()))


class PublishTest(unittest.TestCase):
    """A publish that wrote nothing has to say which fault it hit."""

    def setUp(self):
        self.sw = FakeStalwart()
        record = self.sw.create_domain("acme.innotel.us", "")
        record["dnsZoneFile"] = (
            "acme.innotel.us. 3600 IN MX 10 mail.innotel.us.\n"
            "acme.innotel.us. 3600 IN TXT \"v=spf1 mx -all\"\n"
            "_dmarc.acme.innotel.us. 3600 IN TXT \"v=DMARC1; p=reject\"\n"
        )
        mail_admin.STATE.update(authentik=FakeAuthentik(), stalwart=self.sw, oidc=object())
        self.original_services = mail_admin.services
        self.original_client = mail_admin.cerulean_client
        mail_admin.services = lambda: (None, self.sw, None)

    def tearDown(self):
        mail_admin.services = self.original_services
        mail_admin.cerulean_client = self.original_client
        mail_admin.STATE.update(authentik=None, stalwart=None, oidc=None)

    def publish(self, key="", **fake):
        module, _ = fake_cerulean(**fake)
        self.writes = module.Cerulean.writes
        sys.modules["cerulean"] = module
        mail_admin.cerulean_client = lambda k="": module.Cerulean(k or None)
        try:
            return mail_admin.publish_dns("acme.innotel.us", key=key)
        finally:
            del sys.modules["cerulean"]

    def test_a_refused_zone_read_is_not_reported_as_a_missing_zone(self):
        """The live failure this was written for: a key scoped `dns:write` alone.

        The publish reads the zone to stay idempotent, Cerulean refused the read,
        and the operator was told the resolver does not serve the zone — sending
        them to create a zone that already existed. A refusal is reported as one.
        """
        result = self.publish(
            key="ceru_write_only",
            records_raises=("http", "GET /service/dns/records: HTTP 403 "
                                    "Insufficient scope — requires: dns, dns:read, domains, *"))
        self.assertIn("could not be asked", result["skipped"])
        self.assertIn("Insufficient scope", result["skipped"])
        self.assertNotIn("does not serve", result["skipped"])

    def test_a_zone_the_resolver_does_not_serve_still_says_so(self):
        """A read that succeeds outside this zone means the zone is not there."""
        result = self.publish(
            records=[{"name": "innotel.us", "type": "MX", "value": "mail.innotel.us"}])
        self.assertIn("does not serve", result["skipped"])

    def test_a_freshly_minted_zone_gets_the_apex_records_mail_needs(self):
        """The zone Cerulean minted for a tenant holds NS and SOA and nothing else.

        Deferring to an operator's MX there would defer to a record that does not
        exist, leaving the tenant unable to receive mail — the one thing the write
        was for. Stalwart's own MX/SPF/DMARC are therefore written.
        """
        result = self.publish(records=[{"name": "acme.innotel.us", "type": "NS",
                                       "value": "ns1.innotel.us"}])
        written = {(w["name"], w["type"]) for w in self.writes}
        self.assertIn(("acme.innotel.us", "MX"), written)
        self.assertIn(("acme.innotel.us", "TXT"), written)
        self.assertIn(("_dmarc.acme.innotel.us", "TXT"), written)
        self.assertEqual(0, result["override"])
        self.assertEqual(0, result["failed"])

    def test_a_new_zone_gets_a_relay_aware_spf(self):
        """Outbound leaves through the relay, so the tenant's SPF has to name it.

        Stalwart renders `v=spf1 mx -all`, which authorises this host and not the
        smarthost it actually hands mail to; the recipient would fail SPF. The
        include is added to the record the publish writes.
        """
        self.publish(records=[{"name": "acme.innotel.us", "type": "NS",
                                       "value": "ns1.innotel.us"}])
        spf = [w["value"] for w in self.writes
               if w["name"] == "acme.innotel.us" and w["type"] == "TXT"]
        self.assertEqual(["v=spf1 include:spf.mailjet.com mx -all"], spf)

    def test_the_relay_include_is_configurable_and_can_be_off(self):
        fresh = [{"name": "acme.innotel.us", "type": "NS", "value": "ns1.innotel.us"}]
        original = os.environ.get("SPF_RELAY_INCLUDE")
        try:
            os.environ["SPF_RELAY_INCLUDE"] = "spf.example.net"
            self.publish(records=fresh)
            named = [w["value"] for w in self.writes
                     if w["name"] == "acme.innotel.us" and w["type"] == "TXT"]
            self.assertEqual(["v=spf1 include:spf.example.net mx -all"], named)

            self.writes.clear()
            os.environ["SPF_RELAY_INCLUDE"] = ""
            self.publish(records=fresh)
            untouched = [w["value"] for w in self.writes
                         if w["name"] == "acme.innotel.us" and w["type"] == "TXT"]
            self.assertEqual(["v=spf1 mx -all"], untouched)
        finally:
            if original is None:
                del os.environ["SPF_RELAY_INCLUDE"]
            else:
                os.environ["SPF_RELAY_INCLUDE"] = original

    def test_an_operators_spf_is_never_made_relay_aware_under_them(self):
        """The relay include is for a record *this* publish writes, not a live one."""
        result = self.publish(records=[
            {"name": "acme.innotel.us", "type": "NS", "value": "ns1.innotel.us"},
            {"name": "acme.innotel.us", "type": "TXT",
             "value": "v=spf1 include:another-relay.example -all"},
        ])
        written = {(w["name"], w["type"]) for w in self.writes}
        self.assertNotIn(("acme.innotel.us", "TXT"), written)
        self.assertEqual(1, result["override"])

    def test_an_operators_own_apex_record_is_kept(self):
        """The protection the override was for: a live MX is the operator's choice."""
        result = self.publish(records=[
            {"name": "acme.innotel.us", "type": "NS", "value": "ns1.innotel.us"},
            {"name": "acme.innotel.us", "type": "MX",
             "value": "10 router.example.net"},
        ])
        written = {(w["name"], w["type"]) for w in self.writes}
        self.assertNotIn(("acme.innotel.us", "MX"), written)
        self.assertEqual(1, result["override"])

    def test_a_record_already_matching_is_not_rewritten(self):
        result = self.publish(records=[
            {"name": "acme.innotel.us", "type": "NS", "value": "ns1.innotel.us"},
            {"name": "acme.innotel.us", "type": "MX", "value": "10 mail.innotel.us"},
        ])
        written = {(w["name"], w["type"]) for w in self.writes}
        self.assertNotIn(("acme.innotel.us", "MX"), written)
        self.assertGreaterEqual(result["exact"], 1)


class PageTest(unittest.TestCase):
    """The console has to reach the one write, not only the machine surface."""

    def test_the_page_carries_the_tenant_form_and_posts_to_the_tenant_route(self):
        self.assertIn('id="tenantForm"', mail_admin.PAGE)
        self.assertIn("/api/tenants", mail_admin.PAGE)

    def test_the_page_still_carries_the_domain_and_user_forms(self):
        for marker in ('id="domainForm"', 'id="userForm"'):
            self.assertIn(marker, mail_admin.PAGE)


class ServiceScopeTest(unittest.TestCase):
    """Cerulean's answer decides; this service keeps no list of its own."""

    def test_a_bearer_credential_is_read_case_insensitively(self):
        self.assertEqual("ceru_a_b", mail_admin.bearer("Bearer ceru_a_b"))
        self.assertEqual("ceru_a_b", mail_admin.bearer("bearer ceru_a_b"))
        self.assertEqual("", mail_admin.bearer(""))
        self.assertEqual("", mail_admin.bearer("Basic ceru_a_b"))

    def test_scopes_follow_ceruleans_own_sugar(self):
        self.assertTrue(mail_admin.scope_allows(["mail:provision"], "mail:provision"))
        self.assertTrue(mail_admin.scope_allows(["mail:*"], "mail:provision"))
        self.assertTrue(mail_admin.scope_allows(["*"], "mail:provision"))
        self.assertFalse(mail_admin.scope_allows([], "mail:provision"))
        self.assertFalse(mail_admin.scope_allows(["dns:write"], "mail:provision"))
        self.assertTrue(mail_admin.scope_allows(["dns:write"], "dns:write"))
        # Bare prefixes are the bridge's own spelling: `dns` covers `dns:read`.
        self.assertTrue(mail_admin.scope_allows(["dns"], "dns:read"))
        self.assertTrue(mail_admin.scope_allows(["tenants"], "tenants:write"))

    def test_the_callers_key_publishes_only_when_it_can_read_and_write(self):
        """Publishing reads a zone before it writes it, so `dns:write` alone is not
        enough to act under the caller's authority. A key that may not read is
        handed the platform's own, and the answer says so.
        """
        self.assertEqual("ceru_abc_def",
                         mail_admin.dns_authority_key(["dns:write", "dns:read"],
                                                      "ceru_abc_def"))
        self.assertEqual("ceru_abc_def",
                         mail_admin.dns_authority_key(["dns"], "ceru_abc_def"))
        self.assertEqual("ceru_abc_def",
                         mail_admin.dns_authority_key(["*"], "ceru_abc_def"))
        self.assertEqual("",
                         mail_admin.dns_authority_key(["dns:write"], "ceru_abc_def"))
        self.assertEqual("",
                         mail_admin.dns_authority_key(["mail:provision"], "ceru_abc_def"))

    def test_no_credential_is_unauthorised(self):
        identity, status, _ = mail_admin.service_authorize("", "mail:provision")
        self.assertIsNone(identity)
        self.assertEqual(401, status)

    def test_a_credential_that_is_not_a_cerulean_key_is_refused_before_asking(self):
        identity, status, _ = mail_admin.service_authorize("Bearer hunter2", "mail:provision")
        self.assertIsNone(identity)
        self.assertEqual(401, status)

    def test_ceruleans_verdict_is_what_admits_a_caller(self):
        module, _ = fake_cerulean(
            whoami={"name": "genesis", "scopes": ["mail:provision"]})
        sys.modules["cerulean"] = module
        try:
            identity, status, _ = mail_admin.service_authorize(
                "Bearer ceru_abc_def", "mail:provision")
        finally:
            del sys.modules["cerulean"]
        self.assertEqual(200, status)
        self.assertEqual("genesis", identity["name"])

    def test_a_key_cerulean_does_not_know_is_unauthorised(self):
        module, _ = fake_cerulean(
            whoami_raises=("http", "GET /service/whoami: HTTP 401 no such key"))
        sys.modules["cerulean"] = module
        try:
            identity, status, _ = mail_admin.service_authorize(
                "Bearer ceru_gone_def", "mail:provision")
        finally:
            del sys.modules["cerulean"]
        self.assertIsNone(identity)
        self.assertEqual(401, status)

    def test_a_key_without_the_scope_is_forbidden(self):
        module, _ = fake_cerulean(whoami={"name": "genesis", "scopes": ["dns:write"]})
        sys.modules["cerulean"] = module
        try:
            identity, status, _ = mail_admin.service_authorize(
                "Bearer ceru_abc_def", "mail:provision")
        finally:
            del sys.modules["cerulean"]
        self.assertIsNone(identity)
        self.assertEqual(403, status)

    def test_an_unreachable_cerulean_refuses_rather_than_guessing(self):
        module, _ = fake_cerulean(whoami_raises=("url", "connection refused"))
        sys.modules["cerulean"] = module
        try:
            identity, status, error = mail_admin.service_authorize(
                "Bearer ceru_abc_def", "mail:provision")
        finally:
            del sys.modules["cerulean"]
        self.assertIsNone(identity)
        self.assertEqual(503, status)
        self.assertIn("trust owner", error)

    def test_whoami_absent_falls_back_to_the_live_key_check_and_says_so(self):
        module, _ = fake_cerulean(
            whoami_raises=("http", "GET /service/whoami: HTTP 404 not found"),
            domains=[{"name": "innotel.us"}])
        sys.modules["cerulean"] = module
        try:
            identity = mail_admin.cerulean_identity("ceru_abc_def")
        finally:
            del sys.modules["cerulean"]
        self.assertEqual("domains", identity["verified"])
        self.assertIn(mail_admin.PROVISION_SCOPE, identity["scopes"])

    def test_whoami_absent_and_the_key_refused_is_still_a_refusal(self):
        module, CeruleanError = fake_cerulean(
            whoami_raises=("http", "GET /service/whoami: HTTP 404 not found"),
            domains_raises=("http", "GET /service/domains: HTTP 401 no such key"))
        sys.modules["cerulean"] = module
        try:
            with self.assertRaises(CeruleanError):
                mail_admin.cerulean_identity("ceru_gone_def")
        finally:
            del sys.modules["cerulean"]

    def test_a_bridge_that_answers_whoami_with_its_own_page_still_admits_a_live_key(self):
        """A web tier can serve a path it does not have with its own page.

        The estate's Cerulean answers an unknown path with the SPA: HTTP **200** and
        HTML, not a 404. That is still "this bridge has no whoami route", so a live
        key must keep working — the check just falls back to the domains authority.
        """
        module, _ = fake_cerulean(
            whoami_raises=("http", "GET /service/whoami: HTTP 200 non-JSON reply "
                                   "('<!doctype html>')"),
            domains=[{"name": "innotel.us"}])
        sys.modules["cerulean"] = module
        try:
            identity = mail_admin.cerulean_identity("ceru_abc_def")
        finally:
            del sys.modules["cerulean"]
        self.assertEqual("domains", identity["verified"])
        self.assertIn(mail_admin.PROVISION_SCOPE, identity["scopes"])

    def test_a_page_for_whoami_does_not_turn_a_refused_key_into_a_client_error(self):
        """The failure this was written for, seen live.

        An un-upgraded Cerulean answered whoami with its SPA page, the fallback
        asked `/service/domains`, and Cerulean refused the key — but the refusal
        arrived as a JSON parse error escaping as 400, which reads like the caller's
        request was malformed. A refused key is a 401, whatever the bridge's web tier
        does with unknown paths.
        """
        module, _ = fake_cerulean(
            whoami_raises=("http", "GET /service/whoami: HTTP 200 non-JSON reply "
                                   "('<!doctype html>')"),
            domains_raises=("http", "GET /service/domains: HTTP 401 "
                                    "{'error':'Invalid or revoked service API key'}"))
        sys.modules["cerulean"] = module
        try:
            identity, status, error = mail_admin.service_authorize(
                "Bearer ceru_gone_def", "mail:provision")
        finally:
            del sys.modules["cerulean"]
        self.assertIsNone(identity)
        self.assertEqual(401, status)
        self.assertIn("does not recognise", error)

    def test_an_unexpected_verification_failure_refuses_rather_than_400ing(self):
        module, _ = fake_cerulean(whoami_raises=("boom", "something else entirely"))
        sys.modules["cerulean"] = module
        try:
            identity, status, error = mail_admin.service_authorize(
                "Bearer ceru_abc_def", "mail:provision")
        finally:
            del sys.modules["cerulean"]
        self.assertIsNone(identity)
        self.assertEqual(503, status)
        self.assertIn("trust owner", error)


class MachineSurfaceTest(unittest.TestCase):
    """The surface is Cerulean-only: an address lock in front, Cerulean behind."""

    def test_only_the_named_addresses_may_call_it(self):
        self.assertIn("192.168.1.71", mail_admin.SERVICE_CLIENTS)
        self.assertIn("127.0.0.1", mail_admin.SERVICE_CLIENTS)
        self.assertNotIn("0.0.0.0", mail_admin.SERVICE_CLIENTS)


class ClientContractTest(unittest.TestCase):
    """The bridge client must speak the routes Cerulean implements."""

    def setUp(self):
        path = HERE.parent / "cerulean" / "cerulean.py"
        spec = importlib.util.spec_from_file_location("cerulean_client_under_test", path)
        self.client_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.client_module)
        self.calls = []

    def client(self):
        client = self.client_module.Cerulean(key="ceru_abc_def")
        client.call = lambda path, method="GET", body=None, query=None: (
            self.calls.append((method, path, body)), (200, {"ok": True}))[1]
        return client

    def test_whoami_is_a_get_on_the_service_route(self):
        self.client().whoami()
        self.assertEqual([("GET", "/service/whoami", None)], self.calls)

    def test_provisioning_posts_the_whole_tenant_in_one_call(self):
        self.client().provision_tenant("acme.innotel.us", "jane@acme.innotel.us",
                                       name="Jane", groups=["paid"])
        method, path, body = self.calls[0]
        self.assertEqual(("POST", "/service/tenants"), (method, path))
        self.assertEqual({"domain": "acme.innotel.us", "email": "jane@acme.innotel.us",
                          "name": "Jane", "groups": ["paid"]}, body)

    def test_a_non_json_reply_is_a_refusal_and_says_so(self):
        """A JSON bridge that answers with a web page is not answering at all.

        Letting the parse error out would make the *caller* look wrong (a 400) when
        what happened is that the route is not there. The client refuses with the
        status and a taste of the body instead.
        """
        class FakeResponse:
            status = 200

            def read(self):
                return b"<!doctype html><html><body>Cerulean</body></html>"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        original = self.client_module.urllib.request.urlopen
        self.client_module.urllib.request.urlopen = lambda *a, **k: FakeResponse()
        try:
            with self.assertRaises(self.client_module.CeruleanError) as caught:
                self.client_module.Cerulean(key="ceru_abc_def").call("/service/whoami")
        finally:
            self.client_module.urllib.request.urlopen = original
        self.assertIn("non-JSON", str(caught.exception))
        self.assertIn("HTTP 200", str(caught.exception))

    def test_an_explicit_key_wins_over_the_environment(self):
        os.environ["CERULEAN_SERVICE_KEY"] = "ceru_env_env_env"
        try:
            self.assertEqual("ceru_explicit_key",
                             self.client_module.load_key("ceru_explicit_key"))
            self.assertEqual("ceru_env_env_env", self.client_module.load_key())
        finally:
            del os.environ["CERULEAN_SERVICE_KEY"]


if __name__ == "__main__":
    unittest.main()
