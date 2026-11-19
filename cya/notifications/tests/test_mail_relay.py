"""Outgoing-mail relay settings, host copy and page (SPEC 4.2.0 DEP-13, AC-38; issue #8).

No network: the SMTP transport is replaced by StubTransport through CYA_MAIL_RELAY_TRANSPORT.
"""

import json
import os
import re
import stat
import tempfile
import uuid
from io import StringIO
from unittest import mock

from cryptography.fernet import Fernet
from django.core.management import call_command
from django.core.management.base import CommandError
from django.forms.models import model_to_dict
from django.test import override_settings

from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent, CommandReceipt
from cya.notifications.commands import configure_mail_relay, request_mail_relay_test
from cya.notifications.models import MailRelay
from cya.notifications.relay import RelayError, SendFailure, decrypt_password, send_test
from cya.notifications.tests.support import NotificationCase
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

KEY = Fernet.generate_key().decode()
SECRET = "synthetic-relay-password-7"  # synthetic
STUB = "cya.notifications.tests.test_mail_relay.StubTransport"


class StubTransport:
    """Records what would be sent; `fail` makes the next send raise that code."""

    sent = []
    fail = None

    def send(self, relay, password, recipients, subject, body, headers=None):
        if StubTransport.fail:
            raise SendFailure(StubTransport.fail)
        StubTransport.sent.append(
            {
                "host": relay.host,
                "password": password,
                "to": recipients,
                "subject": subject,
                "body": body,
                "headers": headers or {},
            }
        )


def settings_for(**extra):
    return {
        "host": "smtp-relay.example.test",
        "port": 587,
        "tls_mode": "STARTTLS",
        "auth_mode": "PASSWORD",
        "username": "relay-user",
        "password": SECRET,
        "sender": "alerts@example.test",
        "operator_addresses": "operator@example.test",
        "deputy_addresses": "deputy@example.test\ndeputy2@example.test",
        **extra,
    }


@override_settings(CYA_MAIL_RELAY_KEY=KEY, CYA_MAIL_RELAY_TRANSPORT=STUB)
class RelayCommandTests(NotificationCase):
    def setUp(self):
        super().setUp()
        StubTransport.sent, StubTransport.fail = [], None

    def save(self, actor=None, **extra):
        return configure_mail_relay(**settings_for(**extra), **self.context(actor))

    def relay(self):
        return MailRelay.objects.get(organization=self.org)

    def test_an_administrator_saves_a_relay_and_the_password_is_only_ciphertext(self):
        result = self.save()
        self.assertEqual(result.body, {"code": "SAVED", "revision": 1})
        relay = self.relay()
        self.assertNotIn(SECRET, relay.password_ciphertext)
        self.assertEqual(decrypt_password(relay.password_ciphertext), SECRET)
        self.assertEqual(relay.deputy_addresses, ["deputy@example.test", "deputy2@example.test"])
        # AC-38(1): the audit record names the fields, never the password.
        event = AuditEvent.objects.get(action="MAIL_RELAY_SET", outcome="SUCCEEDED")
        stored = json.dumps(model_to_dict(event), default=str)
        self.assertNotIn(SECRET, stored)
        self.assertNotIn(relay.password_ciphertext, stored)
        self.assertIn('"password_set": true', stored)
        receipts = json.dumps(list(CommandReceipt.objects.values()), default=str)
        self.assertNotIn(SECRET, receipts)

    def test_only_an_administrator_may_change_the_relay(self):
        supply = self.make_user("supply")
        self.grant(supply, "SUPPLY_OFFICER")
        for actor in (self.supervisor, self.crew, supply):
            with self.subTest(actor=actor.idp_subject), self.assertRaises(CommandDenied):
                self.save(actor=actor)
            with self.subTest(test=actor.idp_subject), self.assertRaises(CommandDenied):
                request_mail_relay_test(**self.context(actor))
        self.assertFalse(MailRelay.objects.exists())

    def test_a_plaintext_relay_is_refused(self):
        # AC-38(2).
        for mode in ("NONE", "", "PLAIN"):
            with self.subTest(mode=mode), self.assertRaisesRegex(RelayError, "without encryption"):
                self.save(tls_mode=mode)
        self.assertFalse(MailRelay.objects.exists())

    def test_ip_authentication_stores_no_credential_and_drops_an_old_one(self):
        self.save()
        self.save(auth_mode="IP", username="ignored", password="ignored", base_revision=1)
        relay = self.relay()
        self.assertEqual((relay.username, relay.password_ciphertext), ("", ""))
        self.assertEqual(relay.revision, 2)

    def test_a_blank_password_keeps_the_stored_one(self):
        self.save()
        before = self.relay().password_ciphertext
        self.save(password=None, host="smtp.example.test", base_revision=1)
        relay = self.relay()
        self.assertEqual(relay.password_ciphertext, before)
        self.assertEqual(relay.host, "smtp.example.test")

    def test_a_password_is_required_for_password_authentication(self):
        with self.assertRaisesRegex(RelayError, "enter the relay password"):
            self.save(password=None)

    def test_validation_names_the_problem(self):
        cases = (
            ({"host": "not a host"}, "host name"),
            ({"port": "99999"}, "between 1 and 65535"),
            ({"username": ""}, "username"),
            ({"sender": "nobody"}, "sender address"),
            ({"operator_addresses": ""}, "at least one operator"),
            ({"deputy_addresses": "bad@"}, "deputy address"),
            (
                {"operator_addresses": " ".join(f"o{i}@example.test" for i in range(11))},
                "at most 10",
            ),
        )
        for extra, message in cases:
            with self.subTest(extra=extra), self.assertRaisesRegex(RelayError, message):
                self.save(**extra)

    def test_a_stale_save_is_refused(self):
        self.save()
        with self.assertRaisesRegex(RelayError, "changed since"):
            self.save(host="other.example.test", base_revision=0)

    @override_settings(CYA_MAIL_RELAY_KEY="")
    def test_without_the_key_a_password_cannot_be_saved(self):
        with self.assertRaisesRegex(RelayError, "no mail relay key"):
            self.save()
        self.save(auth_mode="IP", password=None)
        self.assertEqual(self.relay().auth_mode, "IP")

    def test_the_test_email_goes_to_every_operator_and_deputy(self):
        self.save()
        request_mail_relay_test(**self.context())
        self.assertEqual(send_test(self.relay()), "SENT")
        (message,) = StubTransport.sent
        self.assertEqual(message["password"], SECRET)
        self.assertEqual(
            message["to"],
            ["operator@example.test", "deputy@example.test", "deputy2@example.test"],
        )
        StubTransport.fail = "AUTH_FAILED"
        self.assertEqual(send_test(self.relay()), "AUTH_FAILED")
        StubTransport.fail = "a provider message with secrets"
        self.assertEqual(send_test(self.relay()), "UNKNOWN")

    def test_a_test_needs_saved_settings(self):
        with self.assertRaisesRegex(RelayError, "save the relay settings"):
            request_mail_relay_test(**self.context())


@override_settings(CYA_MAIL_RELAY_KEY=KEY, CYA_MAIL_RELAY_TRANSPORT=STUB)
class HostCopyTests(NotificationCase):
    def setUp(self):
        super().setUp()
        configure_mail_relay(**settings_for(), **self.context())
        self.directory = tempfile.mkdtemp()
        self.output = os.path.join(self.directory, "mail-relay.json")

    def tearDown(self):
        for name in os.listdir(self.directory):
            os.unlink(os.path.join(self.directory, name))
        os.rmdir(self.directory)
        super().tearDown()

    def export(self, organization=None):
        out = StringIO()
        call_command(
            "export_mail_relay",
            organization=organization or str(self.org.pk),
            output=self.output,
            stdout=out,
        )
        return out.getvalue()

    def test_the_host_copy_is_mode_0600_and_holds_what_alerts_need(self):
        printed = self.export()
        self.assertEqual(printed.strip(), "mail relay host copy written: revision 1")
        self.assertNotIn(SECRET, printed)
        # AC-38(3): root-owned in production (the export runs as root); always mode 0600.
        self.assertEqual(stat.S_IMODE(os.stat(self.output).st_mode), 0o600)
        with open(self.output) as handle:
            copy = json.load(handle)
        self.assertEqual(copy["password"], SECRET)
        self.assertEqual(copy["deputy_addresses"], ["deputy@example.test", "deputy2@example.test"])
        self.assertEqual(copy["tls_mode"], "STARTTLS")

    def test_a_failed_export_leaves_the_last_good_copy(self):
        self.export()
        with open(self.output) as handle:
            good = handle.read()
        with override_settings(CYA_MAIL_RELAY_KEY=Fernet.generate_key().decode()):
            with self.assertRaisesRegex(CommandError, "previous host copy is unchanged"):
                self.export()
        with open(self.output) as handle:
            self.assertEqual(handle.read(), good)
        self.assertEqual(os.listdir(self.directory), ["mail-relay.json"])

    def test_an_unconfigured_organization_writes_nothing(self):
        with self.assertRaisesRegex(CommandError, "no relay is configured"):
            self.export(organization=str(uuid.uuid4()))
        self.assertFalse(os.path.exists(self.output))


@override_settings(
    **real_collaborator_settings(), CYA_MAIL_RELAY_KEY=KEY, CYA_MAIL_RELAY_TRANSPORT=STUB
)
class RelayPageTests(NotificationCase):
    URL = "/administration/mail-relay/"

    def setUp(self):
        super().setUp()
        StubTransport.sent, StubTransport.fail = [], None
        self.harness = RoleHttpHarness(organization=self.org)
        self.client_, _ = self.harness.sign_in("admin")

    def post(self, client=None, **fields):
        client = client or self.client_
        page = client.get(self.URL)
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode())
        fields.setdefault("operation_id", str(uuid.uuid4()))
        return client.post(self.URL, {"csrfmiddlewaretoken": token.group(1), **fields})

    def form(self, **extra):
        values = settings_for(**extra)
        return {
            "stage": "save",
            "base_revision": "0",
            "host": values["host"],
            "port": str(values["port"]),
            "tls_mode": values["tls_mode"],
            "auth_mode": values["auth_mode"],
            "username": values["username"],
            "password": values["password"],
            "sender": values["sender"],
            "operator": values["operator_addresses"],
            "deputy": values["deputy_addresses"],
        }

    def test_others_are_refused_and_audited(self):
        for sub in ("supervisor", "crew"):
            client, user = self.harness.sign_in(sub)
            with self.subTest(role=sub):
                self.assertEqual(client.get(self.URL).status_code, 403)
                self.assertTrue(
                    AuditEvent.objects.filter(actor_user=user, outcome="DENIED").exists()
                )
                client.get("/")
                response = client.post(
                    self.URL,
                    {
                        "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
                        **self.form(),
                        "operation_id": str(uuid.uuid4()),
                    },
                )
                self.assertEqual(response.status_code, 403)
        self.assertFalse(MailRelay.objects.exists())
        administration = self.client_.get("/administration/")
        self.assertContains(administration, f'href="{self.URL}"')

    def test_saving_never_shows_the_password_back(self):
        saved = self.post(**self.form())
        self.assertEqual(saved.status_code, 302)
        page = self.client_.get(saved["Location"])
        self.assertContains(page, "Relay settings saved.")
        self.assertContains(page, "A password is saved and is never shown.")
        html = page.content.decode()
        self.assertNotIn(SECRET, html)
        self.assertNotIn(MailRelay.objects.get().password_ciphertext, html)
        self.assertIn('name="password" type="password"', html)
        self.assertNotRegex(html, r'name="password"[^>]*value=')

    def test_a_refused_save_keeps_what_was_typed_but_not_the_password(self):
        refused = self.post(**self.form(tls_mode="NONE"))
        self.assertContains(refused, "without encryption", status_code=400)
        self.assertContains(refused, 'value="smtp-relay.example.test"', status_code=400)
        self.assertNotContains(refused, SECRET, status_code=400)
        self.assertFalse(MailRelay.objects.exists())

    def test_the_test_button_reports_a_safe_result(self):
        self.post(**self.form())
        sent = self.post(stage="test")
        self.assertContains(sent, "Test email sent to every operator and deputy address.")
        StubTransport.fail = "CONNECT_FAILED"
        failed = self.post(stage="test")
        self.assertContains(failed, "The relay could not be reached.")
        # Issue #184 goals review: the glyph is decorative; screen readers hear only the words.
        self.assertContains(
            failed, '<span class="indicator-icon" aria-hidden="true">&#9888;</span>'
        )
        self.assertContains(sent, '<span class="indicator-icon" aria-hidden="true">&#10003;</span>')
        self.assertEqual(
            AuditEvent.objects.filter(action="MAIL_RELAY_TEST", outcome="SUCCEEDED").count(), 2
        )

    def test_given_up_email_is_listed_and_can_be_tried_again(self):
        # Codex review of PR #225: a relay refusal gives up; the administrator sends it again.
        self.post(**self.form())
        self.emit(user_ids=[str(self.crew.pk)], event_type="deficiency.reported")
        StubTransport.fail = "REJECTED"
        call_command("deliver_notifications", stdout=StringIO())
        page = self.client_.get(self.URL)
        self.assertContains(page, "1 notification email could not be sent and was given up.")
        self.assertContains(page, "<td>deficiency.reported</td>")
        self.assertContains(page, "<td>The relay refused it.</td>")
        retried = self.post(stage="requeue")
        self.assertEqual(retried.status_code, 302)
        page = self.client_.get(retried["Location"])
        self.assertContains(page, "1 email will be tried again on the next delivery run.")
        self.assertContains(page, "All notification email has been sent or is still being tried.")
        StubTransport.fail = None
        call_command("deliver_notifications", stdout=StringIO())
        self.assertEqual(len(StubTransport.sent), 1)

    def test_a_test_never_sends_through_a_revision_it_did_not_audit(self):
        # PR #188 review: the audited request names revision 1; a save before the send
        # (simulated by the request reporting an older revision) sends nothing.
        self.post(**self.form())
        stale = type("Result", (), {"body": {"code": "TEST_REQUESTED", "revision": 0}})()
        with mock.patch(
            "cya.notifications.relay_views.request_mail_relay_test", return_value=stale
        ):
            response = self.post(stage="test")
        self.assertContains(response, "The relay settings changed while the test was starting.")
        self.assertEqual(StubTransport.sent, [])
