"""issue 25 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/25) exit test: one authorized, persisted, audited action through the web UI.

Sign-in uses the offline Google stub (cya.testing.stubs); roles, receipts, role
history and audit are the real MariaDB-backed collaborators. This proves logic,
not the Workspace integration, which issue 25 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/25) verifies separately outside CI.
"""

from unittest import mock
from uuid import uuid4

from django.core.management import call_command
from django.test import Client, TransactionTestCase, override_settings

from cya.accounts.models import Organization, User
from cya.authorization.models import RoleGrant
from cya.authorization.persistence import current_role_codes
from cya.core.models import AuditEvent, CommandReceipt
from cya.testing.role_http import URL, RoleHttpHarness, real_collaborator_settings


@override_settings(**real_collaborator_settings())
class RoleAdministrationHttpTests(TransactionTestCase):
    def setUp(self):
        harness = RoleHttpHarness()
        self.org = harness.org
        self.sign_in = harness.sign_in
        self.make_administrator = harness.make_administrator
        self.form = harness.form
        self.change = harness.change
        self.counts = harness.counts

    def tearDown(self):
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)

    # Acceptance criteria -----------------------------------------------------

    def test_authorized_change_is_persisted_in_history_and_audited(self):
        admin_client, admin = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        before = self.counts()

        response = self.change(admin_client, crew)

        self.assertEqual((response.status_code, response["Location"]), (302, URL + "?saved=1"))
        self.assertEqual(current_role_codes(crew), ("CREW", "SUPERVISOR"))
        grant = RoleGrant.objects.get(user=crew, role="SUPERVISOR")
        self.assertEqual((grant.action, grant.actor_user_id), ("GRANT", admin.pk))
        self.assertEqual(grant.actor_roles_at_time, ["ADMINISTRATOR", "CREW"])
        audit = AuditEvent.objects.get(action="ROLE_CHANGE", outcome="SUCCEEDED")
        self.assertEqual((audit.actor_user_id, audit.target_id), (admin.pk, str(crew.pk)))
        self.assertEqual(audit.command_id, grant.command_id)
        self.assertEqual((self.counts()[0] - before[0], self.counts()[1] - before[1]), (1, 1))
        history = admin_client.get(URL + "?saved=1")
        self.assertContains(history, "SUPERVISOR")
        self.assertContains(history, crew.email)
        self.assertContains(history, "Saved")

    def test_unauthorized_user_is_refused_and_a_denial_is_recorded(self):
        _, admin = self.make_administrator("admin-1")
        client, crew = self.sign_in("crew-1")
        client.get("/")  # obtains a CSRF cookie; the Crew user is never shown the form
        grants = RoleGrant.objects.count()

        # A forged direct request: the Crew user tries to make themself an administrator.
        response = client.post(
            URL,
            {
                "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
                "operation_id": str(uuid4()),
                "target": str(crew.pk),
                "role": "ADMINISTRATOR",
                "action": "GRANT",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(RoleGrant.objects.count(), grants)
        self.assertEqual(current_role_codes(crew), ("CREW",))
        self.assertEqual(current_role_codes(admin), ("ADMINISTRATOR", "CREW"))
        self.assertFalse(CommandReceipt.objects.filter(command_type="ROLE_CHANGE").exists())
        denial = AuditEvent.objects.get(action="ROLE_CHANGE", outcome="DENIED")
        self.assertEqual((denial.actor_user_id, denial.roles_at_time), (crew.pk, ["CREW"]))
        self.assertEqual(denial.reason_code, "FORBIDDEN")

    def test_replaying_the_same_operation_id_yields_one_record(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        token, operation_id = self.form(admin_client)
        data = {
            "csrfmiddlewaretoken": token,
            "operation_id": operation_id,
            "target": str(crew.pk),
            "role": "SUPERVISOR",
            "action": "GRANT",
        }
        first = admin_client.post(URL, data)
        after_first = self.counts()
        second = admin_client.post(URL, data)

        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(self.counts(), after_first)
        self.assertEqual(RoleGrant.objects.filter(user=crew, role="SUPERVISOR").count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="ROLE_CHANGE").count(), 1)

    def test_reusing_an_operation_id_for_a_different_request_is_not_reported_saved(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        _, other = self.sign_in("crew-2")
        operation_id = str(uuid4())
        first = self.change(admin_client, crew, operation_id=operation_id)
        self.assertEqual(first.status_code, 302)
        after_first = self.counts()

        changes = {
            "action": {"action": "REVOKE"},
            "role": {"role": "SUPPLY_OFFICER"},
            "target": {"target_other": True},
        }
        for name, change in changes.items():
            with self.subTest(name):
                if change.pop("target_other", False):
                    response = self.change(admin_client, other, operation_id=operation_id)
                else:
                    response = self.change(admin_client, crew, operation_id=operation_id, **change)
                self.assertEqual(response.status_code, 409)
                self.assertContains(response, "was not applied", status_code=409)
                self.assertNotIn("Location", response)
        self.assertEqual(self.counts(), after_first)
        self.assertEqual(current_role_codes(crew), ("CREW", "SUPERVISOR"))
        self.assertEqual(current_role_codes(other), ("CREW",))

    def test_the_action_is_reachable_from_the_shell(self):
        client, _ = self.make_administrator("admin-1")
        home = client.get("/")
        self.assertContains(home, 'href="/administration/"')
        section = client.get("/administration/")
        self.assertContains(section, 'href="/administration/roles/"')
        page = client.get("/administration/roles/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'aria-current="page">Administration')
        self.assertContains(page, "Change a role")

    def test_a_crew_user_is_not_shown_the_link(self):
        client, _ = self.sign_in("crew-1")
        self.assertNotContains(client.get("/"), "/administration/")
        self.assertNotContains(
            client.get("/administration/"), "/administration/roles/", status_code=403
        )

    # Boundaries --------------------------------------------------------------

    def test_signed_out_requests_are_redirected_and_change_nothing(self):
        before = self.counts()
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.get(URL)["Location"], "/auth/login/")
        # CSRF is checked first, so a token-less signed-out POST is refused outright.
        self.assertEqual(client.post(URL, {}).status_code, 403)
        self.assertEqual(self.counts(), before)

    def test_missing_csrf_token_is_rejected_before_any_change(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        before = self.counts()
        response = admin_client.post(
            URL,
            {
                "operation_id": str(uuid4()),
                "target": str(crew.pk),
                "role": "SUPERVISOR",
                "action": "GRANT",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.counts(), before)

    def test_malformed_input_is_a_400_and_changes_nothing(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        before = self.counts()
        cases = {
            "unknown role": {"role": "ROOT"},
            "legacy EMT alias": {"role": "EMT"},
            "unknown action": {"action": "DELETE"},
            "target not a UUID": {"target_id": "not-a-uuid"},
            "target not canonical": {"target_id": str(crew.pk).upper()},
            "operation id not a UUID": {"operation_id": "not-a-uuid"},
            "operation id not UUID4": {"operation_id": "00000000-0000-1000-8000-000000000000"},
        }
        for name, case in cases.items():
            with self.subTest(name):
                case = dict(case)
                target = case.pop("target_id", str(crew.pk))
                token, fresh_id = self.form(admin_client)
                data = {
                    "csrfmiddlewaretoken": token,
                    "operation_id": fresh_id,
                    "target": target,
                    "role": "SUPERVISOR",
                    "action": "GRANT",
                    **case,
                }
                self.assertEqual(admin_client.post(URL, data).status_code, 400)
        self.assertEqual(self.counts(), before)

    def test_a_non_administrator_learns_nothing_from_error_responses(self):
        admin_client, admin = self.make_administrator("admin-1")
        client, crew = self.sign_in("crew-1")
        client.get("/")
        base = {
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
            "operation_id": str(uuid4()),
            "role": "SUPERVISOR",
            "action": "GRANT",
        }
        malformed = client.post(URL, {**base, "target": "not-a-uuid"})
        self.assertEqual(malformed.status_code, 400)
        for private in (admin.email, "Role history", "Change a role"):
            self.assertNotContains(malformed, private, status_code=400)
        # A missing target must look like a denial, not reveal which ids exist.
        missing = client.post(URL, {**base, "target": str(uuid4())})
        self.assertEqual(missing.status_code, 403)

    def test_unknown_or_cross_organization_target_is_not_found(self):
        admin_client, _ = self.make_administrator("admin-1")
        other = Organization.objects.create(name="Other Agency", station_zone="UTC")
        stranger = User.objects.create(
            organization=other,
            idp_issuer="https://accounts.google.com",
            idp_subject="stranger",
            email="stranger@other.example",
        )
        before = self.counts()
        response = self.change(admin_client, stranger)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(RoleGrant.objects.filter(user=stranger).count(), 0)
        self.assertEqual(self.counts()[:2], before[:2])
        self.assertTrue(AuditEvent.objects.filter(action="ROLE_CHANGE", outcome="FAILED").exists())

    def test_last_administrator_cannot_be_revoked(self):
        admin_client, admin = self.make_administrator("admin-1")
        response = self.change(admin_client, admin, role="ADMINISTRATOR", action="REVOKE")
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "last active administrator", status_code=409)
        self.assertEqual(current_role_codes(admin), ("ADMINISTRATOR", "CREW"))

    def test_audit_outage_returns_503_without_a_successful_change(self):
        admin_client, _ = self.make_administrator("admin-1")
        _, crew = self.sign_in("crew-1")
        before = self.counts()
        with mock.patch("cya.core.audit_commands.append_audit", side_effect=RuntimeError("down")):
            response = self.change(admin_client, crew)
        self.assertEqual(response.status_code, 503)
        self.assertNotContains(response, "down", status_code=503)
        self.assertEqual(self.counts()[:2], before[:2])
        self.assertEqual(current_role_codes(crew), ("CREW",))

    def test_history_is_administrator_only_and_denial_is_audited(self):
        client, crew = self.sign_in("crew-1")
        self.assertEqual(client.get(URL).status_code, 403)
        self.assertTrue(
            AuditEvent.objects.filter(
                actor_user=crew, action="VIEW_ADMINISTRATION", outcome="DENIED"
            ).exists()
        )
