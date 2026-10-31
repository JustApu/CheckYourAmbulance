import io
from uuid import uuid4

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.http import HttpResponse, StreamingHttpResponse
from django.template import engines
from django.template.response import SimpleTemplateResponse
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import Resolver404, resolve

from cya.accounts import signin
from cya.accounts.models import Organization, User
from cya.authorization.backend import AuthorizationUnavailable
from cya.authorization.guard import require_permission
from cya.authorization.lifecycle import InvalidRoleHistory, RoleEvent
from cya.authorization.policy import ResourceContext, Role
from cya.authorization.requests import authorized_request, permission_required
from cya.authorization.roles import grant_initial_emt
from cya.authorization.tests.stubs import configure_backend
from cya.integrations import VerifiedIdentity
from cya.testing.stubs import AUDIT

BACKEND_PATH = "cya.authorization.tests.stubs.recording_backend"


@override_settings(CYA_AUTHORIZATION_BACKEND=BACKEND_PATH)
class AuthorizationInterfaceTests(TestCase):
    def setUp(self):
        self.backend = configure_backend()
        self.org = Organization.objects.create(name="Sample Agency", station_zone="UTC")
        self.user = User.objects.create(
            organization=self.org,
            idp_issuer="https://identity.example.test",
            idp_subject="member-subject",
            email="member@example.test",
            display_name="Synthetic Member",
        )
        self.backend.set_roles(self.user, Role.CREW)
        self.calls = []

    def request(self, user=True, **data):
        request = RequestFactory().post("/synthetic/protected/", data)
        request.cya_user = self.user if user is True else user
        return request

    def protected(self, action, context=None, body=None):
        def view(request):
            self.calls.append("called")
            return body(request) if body else HttpResponse("ok")

        return permission_required(
            action,
            organization_loader=lambda request: str(self.org.pk),
            context_loader=(lambda request, user: context) if context else None,
        )(view)

    def bootstrap(self, **options):
        out = io.StringIO()
        call_command(
            "bootstrap_administrator",
            organization=str(self.org.pk),
            user=str(self.user.pk),
            stdout=out,
            **options,
        )
        return out.getvalue()

    @override_settings(
        CYA_INITIAL_ROLE_GRANTER="cya.authorization.roles.grant_initial_crew",
        CYA_AUTH_AUDIT_SINK="cya.testing.stubs.AUDIT",
    )
    def test_signin_calls_real_crew_collaborator_inside_provisioning_transaction(self):
        AUDIT.fail = False
        AUDIT.fail_with = None
        AUDIT.events.clear()
        identity = VerifiedIdentity(
            "https://identity.example.test",
            "new-subject",
            "new@example.test",
            "New Member",
            "example.test",
        )
        correlation = str(uuid4())
        created = signin._find_or_provision(identity, self.org, correlation, None)
        user_id, change, received, origin = self.backend.appends[0]
        self.assertEqual(
            (user_id, change.role, received, origin),
            (str(created.pk), Role.CREW, correlation, "INITIAL_PROVISIONING"),
        )
        self.assertEqual(change.sequence, 1)
        self.assertEqual([e.action for e in AUDIT.events], ["USER_PROVISIONED", "SIGN_IN"])

    @override_settings(
        CYA_INITIAL_ROLE_GRANTER="cya.authorization.roles.grant_initial_crew",
        CYA_AUTH_AUDIT_SINK="cya.testing.stubs.AUDIT",
    )
    def test_role_writer_failure_rolls_back_provisioned_user(self):
        self.backend.fail_append = True
        identity = VerifiedIdentity(
            "https://identity.example.test",
            "failed-subject",
            "failed@example.test",
            "Failed Member",
            "example.test",
        )
        with self.assertRaises(AuthorizationUnavailable):
            signin._find_or_provision(identity, self.org, str(uuid4()), None)
        self.assertFalse(User.objects.filter(idp_subject="failed-subject").exists())

    def test_initial_granter_refuses_existing_role_history_and_invalid_correlation(self):
        with self.assertRaises(InvalidRoleHistory):
            grant_initial_emt(user=self.user, command_id=str(uuid4()))
        with self.assertRaises(ValueError):
            grant_initial_emt(user=self.user, command_id="not-a-uuid")
        self.assertEqual(self.backend.appends, [])

    def test_bootstrap_dry_run_reads_history_without_appending_or_auditing(self):
        self.assertIn("Dry run", self.bootstrap(dry_run=True))
        self.assertEqual(self.backend.appends, [])
        self.assertEqual(self.backend.denials, [])
        self.assertEqual(len(self.backend.events[str(self.user.pk)]), 1)

    def test_bootstrap_appends_only_first_administrator_and_refuses_repeat(self):
        self.assertIn("completed", self.bootstrap())
        user_id, change, correlation, origin = self.backend.appends[0]
        self.assertEqual(
            (user_id, change.role, change.sequence, origin),
            (str(self.user.pk), Role.ADMINISTRATOR, 2, "OPERATOR_BOOTSTRAP"),
        )
        self.assertEqual(len(correlation), 36)
        with self.assertRaises(CommandError):
            self.bootstrap()
        self.assertEqual(len(self.backend.appends), 1)

    def test_bootstrap_refuses_inactive_or_unprovisioned_target(self):
        User.objects.filter(pk=self.user.pk).update(active=False)
        with self.assertRaises(CommandError):
            self.bootstrap()
        User.objects.filter(pk=self.user.pk).update(active=True)
        self.backend.events.clear()
        with self.assertRaises(CommandError):
            self.bootstrap()

    def test_bootstrap_refuses_prior_inactive_administrator_and_wrong_org(self):
        other = User.objects.create(
            organization=self.org,
            idp_issuer=self.user.idp_issuer,
            idp_subject="old-admin",
            email="old-admin@example.test",
            active=False,
        )
        self.backend.set_roles(other, Role.ADMINISTRATOR)
        with self.assertRaises(CommandError):
            self.bootstrap()
        self.backend.events.pop(str(other.pk))
        other_org = Organization.objects.create(name="Other Agency", station_zone="UTC")
        with self.assertRaises(CommandError):
            call_command(
                "bootstrap_administrator", organization=str(other_org.pk), user=str(self.user.pk)
            )

    def test_bootstrap_rejects_bad_ids_and_writer_failure(self):
        for org, user in (("bad", str(self.user.pk)), (str(self.org.pk), "bad")):
            with self.assertRaises(CommandError):
                call_command("bootstrap_administrator", organization=org, user=user)
        self.backend.fail_append = True
        with self.assertRaises(CommandError):
            self.bootstrap()

    def test_inactive_organization_blocks_bootstrap_and_requests(self):
        Organization.objects.filter(pk=self.org.pk).update(active=False)
        with self.assertRaises(CommandError):
            self.bootstrap(dry_run=True)
        self.assertEqual(self.protected("record_check")(self.request()).status_code, 403)
        self.assertEqual(self.backend.appends, [])
        self.assertEqual(self.calls, [])

    def test_missing_and_cross_organization_request_identities_are_denied(self):
        other_org = Organization.objects.create(name="Other Agency", station_zone="UTC")
        other_user = User.objects.create(
            organization=other_org,
            idp_issuer=self.user.idp_issuer,
            idp_subject="other-member",
            email="other@example.test",
        )
        view = self.protected("record_check")
        self.assertEqual(view(self.request(user=other_user)).status_code, 403)
        missing = User(
            id=str(uuid4()),
            organization=self.org,
            idp_issuer=self.user.idp_issuer,
            idp_subject="missing-member",
        )
        self.assertEqual(view(self.request(user=missing)).status_code, 403)
        self.assertEqual(self.calls, [])

    @override_settings(CYA_AUTHORIZATION_BACKEND="cya.authorization.tests.stubs.absent_factory")
    def test_misconfigured_backend_does_not_run_view_or_expose_exception(self):
        response = self.protected("record_check")(self.request())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content, b'{"code": "AUTHORIZATION_UNAVAILABLE"}')
        self.assertEqual(self.calls, [])

    @override_settings(CYA_AUTHORIZATION_BACKEND=None)
    def test_missing_backend_fails_closed_in_command_and_view(self):
        with self.assertRaises(CommandError):
            self.bootstrap(dry_run=True)
        response = self.protected("record_check")(self.request())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.calls, [])

    def test_direct_forbidden_request_cannot_forge_roles_and_is_audited(self):
        response = self.protected("manage_users_roles")(self.request(roles="Administrator"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.backend.denials[0].actor_roles, (Role.CREW,))

    def test_legacy_emt_role_denial_audits_canonical_crew_actor_roles(self):
        self.backend.set_roles(self.user, Role.EMT)
        response = self.protected("manage_users_roles")(self.request())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.backend.denials[0].actor_roles, (Role.CREW,))

    def test_roles_are_loaded_again_after_revocation(self):
        self.backend.set_roles(self.user, Role.ADMINISTRATOR)
        view = self.protected("manage_users_roles")
        self.assertEqual(view(self.request()).status_code, 200)
        grant = self.backend.events[str(self.user.pk)][0]
        self.backend.events[str(self.user.pk)].append(
            RoleEvent(
                str(uuid4()),
                str(self.org.pk),
                str(self.user.pk),
                2,
                Role.ADMINISTRATOR,
                "REVOKE",
                grant.id,
            )
        )
        self.assertEqual(view(self.request()).status_code, 403)
        self.assertEqual(self.calls, ["called"])

    def test_stale_active_user_and_missing_principal_are_refused(self):
        User.objects.filter(pk=self.user.pk).update(active=False)
        view = self.protected("record_check")
        self.assertEqual(view(self.request()).status_code, 403)
        self.assertEqual(view(self.request(user=None)).status_code, 403)
        self.assertEqual(self.calls, [])

    def test_conditional_scope_uses_server_context_instead_of_posted_flags(self):
        self.backend.set_roles(self.user, Role.SUPPLY_OFFICER)
        for action in ("assign_deficiency_deadline", "update_external_dependency"):
            with self.subTest(action=action):
                deny = self.protected(action, ResourceContext(supply_related=False))
                allow = self.protected(action, ResourceContext(supply_related=True))
                self.assertEqual(deny(self.request(supply_related="true")).status_code, 403)
                self.assertEqual(allow(self.request(supply_related="false")).status_code, 200)

    def test_extension_assignment_and_admin_self_invalidation_are_server_owned(self):
        self.backend.set_roles(self.user, Role.SUPPLY_OFFICER)
        own = self.protected("request_extension", ResourceContext(assignee_id=str(self.user.pk)))
        other = self.protected("request_extension", ResourceContext(assignee_id=str(uuid4())))
        self.assertEqual(own(self.request()).status_code, 200)
        self.assertEqual(other(self.request(assignee_id=str(self.user.pk))).status_code, 403)
        self.backend.set_roles(self.user, Role.ADMINISTRATOR, Role.CREW)
        self_check = self.protected(
            "invalidate_others_check", ResourceContext(author_id=str(self.user.pk))
        )
        other_check = self.protected(
            "invalidate_others_check", ResourceContext(author_id=str(uuid4()))
        )
        self.assertEqual(self_check(self.request()).status_code, 403)
        self.assertEqual(other_check(self.request()).status_code, 200)

    def test_denial_audit_failure_is_503_and_rejected_changes_roll_back_first(self):
        def body(request):
            Organization.objects.filter(pk=self.org.pk).update(name="Transient")
            require_permission(None, str(self.org.pk), "manage_users_roles")

        self.backend.on_denial = lambda: self.assertEqual(
            Organization.objects.get(pk=self.org.pk).name, "Sample Agency"
        )
        view = self.protected("record_check", body=body)
        self.assertEqual(view(self.request()).status_code, 403)
        self.backend.fail_denial = True
        self.assertEqual(view(self.request()).status_code, 503)

    def test_unknown_action_and_bad_role_history_do_not_run_view(self):
        response = self.protected("unlisted-action")(self.request())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.backend.denials[-1].action, "UNLISTED_ACTION")
        self.backend.events[str(self.user.pk)] = ["malformed"]
        self.assertEqual(self.protected("record_check")(self.request()).status_code, 503)
        self.assertEqual(self.calls, [])

    def test_streaming_and_async_views_cannot_escape_the_guard_transaction(self):
        response = self.protected(
            "record_check", body=lambda request: StreamingHttpResponse(["x"])
        )(self.request())
        self.assertEqual(response.status_code, 503)

        async def asynchronous(request):
            return HttpResponse("must not run")

        with self.assertRaises(TypeError):
            permission_required(
                "record_check", organization_loader=lambda request: str(self.org.pk)
            )(asynchronous)

    def test_no_bootstrap_http_route_exists(self):
        with self.assertRaises(Resolver404):
            resolve("/auth/bootstrap_administrator/")

    @override_settings(TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates"}])
    def test_deferred_response_replacements_are_rendered_and_checked_inside_transaction(self):
        for replacement, expected in (
            (HttpResponse("replacement"), 200),
            (StreamingHttpResponse(["x"]), 503),
        ):
            with self.subTest(expected=expected):
                response = SimpleTemplateResponse(engines["django"].from_string("synthetic"))
                response.add_post_render_callback(lambda response, result=replacement: result)
                actual = self.protected(
                    "record_check", body=lambda request, result=response: result
                )(self.request())
                self.assertEqual(actual.status_code, expected)
                if expected == 200:
                    self.assertIs(actual, replacement)


class InitialGrantTransactionTests(SimpleTestCase):
    def test_initial_grant_refuses_to_open_its_own_transaction(self):
        user = User(organization_id=str(uuid4()), idp_issuer="synthetic", idp_subject="synthetic")
        with self.assertRaises(AuthorizationUnavailable):
            grant_initial_emt(user=user, command_id=str(uuid4()))


@override_settings(CYA_AUTHORIZATION_BACKEND=BACKEND_PATH)
class RequestTransactionTests(TransactionTestCase):
    def tearDown(self):
        # Schema-owner cleanup only: retained attribution forbids ordinary DELETE.
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)
        super().tearDown()

    def setUp(self):
        self.backend = configure_backend()
        self.org = Organization.objects.create(name="Sample Agency", station_zone="UTC")
        self.user = User.objects.create(
            organization=self.org,
            idp_issuer="https://identity.example.test",
            idp_subject="member-subject",
            email="member@example.test",
        )
        self.backend.set_roles(self.user, Role.CREW)
        self.request = RequestFactory().post("/synthetic/protected/")
        self.request.cya_user = self.user

    def test_denial_callback_runs_outside_transaction_after_real_rollback(self):
        def observe_rollback():
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(Organization.objects.get(pk=self.org.pk).name, "Sample Agency")

        self.backend.on_denial = observe_rollback

        @permission_required("record_check", organization_loader=lambda request: str(self.org.pk))
        def view(request):
            Organization.objects.filter(pk=self.org.pk).update(name="Must roll back")
            require_permission(None, str(self.org.pk), "manage_users_roles")

        self.assertEqual(view(self.request).status_code, 403)
        self.assertEqual(len(self.backend.denials), 1)

    def test_guard_rejects_surrounding_application_transaction(self):
        with transaction.atomic():
            with self.assertRaises(RuntimeError):
                with authorized_request(self.request, str(self.org.pk), "record_check"):
                    self.fail("Nested guard must not run a command")
        self.assertEqual(self.backend.denials, [])
