"""Integration regression spanning real role lookup, shell access and audit persistence.

Proves:
1. Canonical CREW role code works end-to-end through real role lookup, shell access,
   and audit persistence against MariaDB schema check constraints.
2. Legacy EMT role history is translated to canonical CREW without rewriting
   immutable historical evidence, and historical EMT audit rows remain valid.
"""

import io
import json
import time
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.test import Client, TransactionTestCase

from cya.accounts.middleware import SESSION_AUTH_TIME_KEY, SESSION_USER_KEY
from cya.accounts.models import Organization, User
from cya.authorization.commands import change_role
from cya.authorization.lifecycle import RoleChange
from cya.authorization.models import RoleGrant
from cya.authorization.persistence import (
    append_change,
    current_role_codes,
    digest,
)
from cya.authorization.policy import Role
from cya.authorization.roles import grant_initial_crew
from cya.core.audit_repository import append_audit, append_receipt
from cya.core.models import AuditEvent
from tools.command_identity import worker_operation_id


class RoleTransitionIntegrationTests(TransactionTestCase):
    def tearDown(self):
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)
        super().tearDown()

    def setUp(self):
        self.org = Organization.objects.create(
            name="Transition Agency", station_zone="America/New_York"
        )

    def client_for(self, user):
        client = Client()
        session = client.session
        session[SESSION_USER_KEY] = str(user.id)
        session[SESSION_AUTH_TIME_KEY] = int(time.time())
        session.save()
        client.cookies["cya_session"] = session.session_key
        return client

    def make_user(self, subject="1001"):
        return User.objects.create(
            organization=self.org,
            idp_issuer="https://accounts.google.com",
            idp_subject=subject,
            email=f"user{subject}@example.test",
            display_name=f"User {subject}",
        )

    def test_canonical_crew_role_end_to_end(self):
        user = self.make_user("crew1")
        correlation = str(uuid4())

        # 1. Real initial grant of canonical CREW
        with transaction.atomic():
            grant_initial_crew(user=user, command_id=correlation)

        grant = RoleGrant.objects.get(user=user)
        self.assertEqual(grant.role, "CREW")
        self.assertEqual(grant.action, "GRANT")

        # 2. Real uncached role lookup yields CREW
        self.assertEqual(current_role_codes(user), ("CREW",))

        # 3. Shell access: user sees crew sections and gets 200
        client = self.client_for(user)
        fleet_response = client.get("/fleet/")
        self.assertEqual(fleet_response.status_code, 200)
        self.assertContains(fleet_response, "Fleet")
        self.assertContains(fleet_response, "Inspection")
        self.assertContains(fleet_response, "Deficiencies")
        self.assertNotContains(fleet_response, "Administration")

        # 4. Accessing administration section is denied (403) and audits with roles_at_time=["CREW"]
        admin_response = client.get("/administration/")
        self.assertEqual(admin_response.status_code, 403)

        denial_audit = AuditEvent.objects.filter(
            actor_user=user, action="VIEW_ADMINISTRATION", outcome="DENIED"
        ).first()
        self.assertIsNotNone(denial_audit)
        self.assertEqual(denial_audit.roles_at_time, ["CREW"])

        # 5. First-administrator bootstrap succeeds from CREW target
        call_command(
            "bootstrap_administrator",
            organization=self.org.pk,
            user=user.pk,
            stdout=io.StringIO(),
        )
        self.assertEqual(current_role_codes(user), ("ADMINISTRATOR", "CREW"))

        # 6. Now administration is accessible (200)
        admin_response_after = client.get("/administration/")
        self.assertEqual(admin_response_after.status_code, 200)

    def test_historical_emt_backward_compatibility(self):
        user = self.make_user("legacy1")
        correlation = str(uuid4())

        # Simulate historical EMT grant without modifying historical data
        service = "identity.provisioning"
        job_key = json.dumps([user.pk, correlation], separators=(",", ":"))
        op_id = str(worker_operation_id(str(self.org.pk), service, "INITIAL_EMT", job_key))
        with transaction.atomic():
            receipt = append_receipt(
                organization=self.org,
                operation_id=op_id,
                service_identity=service,
                job_key=job_key,
                command_type="INITIAL_EMT",
                request_digest=digest(
                    {"user": user.pk, "role": "EMT", "origin": "INITIAL_PROVISIONING"}
                ),
                response_status=200,
                response_body={"code": "SAVED", "user_id": user.pk},
                correlation_id=correlation,
            )
            append_change(
                user=user,
                change=RoleChange(Role.EMT, "GRANT", None, 1),
                receipt=receipt,
                actor_roles=(),
            )
            append_audit(
                organization=self.org,
                command=receipt,
                service_identity=service,
                job_key=job_key,
                roles_at_time=[],
                request_attempt_id=correlation,
                correlation_id=correlation,
                action="INITIAL_EMT",
                outcome="SUCCEEDED",
                target_type="USER",
                target_id=user.pk,
                new_snapshot={
                    "schema_version": 1,
                    "values": {"role": "EMT", "action": "GRANT"},
                },
            )

        # Confirm historical row is preserved verbatim as 'EMT'
        persisted_grant = RoleGrant.objects.get(user=user)
        self.assertEqual(persisted_grant.role, "EMT")

        # Confirm role lookup translates legacy EMT to canonical CREW
        self.assertEqual(current_role_codes(user), ("CREW",))

        # Shell client receives CREW access
        client = self.client_for(user)
        self.assertEqual(client.get("/fleet/").status_code, 200)

        # Historical audit event with roles_at_time=["EMT"] is valid in MariaDB
        with transaction.atomic():
            historical_audit = append_audit(
                organization=self.org,
                actor_user=user,
                roles_at_time=["EMT"],
                request_attempt_id=str(uuid4()),
                correlation_id=str(uuid4()),
                action="HISTORICAL_CHECK",
                outcome="SUCCEEDED",
            )
        self.assertEqual(historical_audit.roles_at_time, ["EMT"])
        persisted_audit = AuditEvent.objects.get(pk=historical_audit.pk)
        self.assertEqual(persisted_audit.roles_at_time, ["EMT"])

        # First administrator bootstrap succeeds from legacy EMT target
        call_command(
            "bootstrap_administrator",
            organization=self.org.pk,
            user=user.pk,
            stdout=io.StringIO(),
        )
        self.assertEqual(current_role_codes(user), ("ADMINISTRATOR", "CREW"))

    def test_canonical_crew_commands_on_historical_emt_history(self):
        admin = self.make_user("admin_user")
        with transaction.atomic():
            grant_initial_crew(user=admin, command_id=str(uuid4()))
        call_command(
            "bootstrap_administrator",
            organization=self.org.pk,
            user=admin.pk,
            stdout=io.StringIO(),
        )

        legacy_user = self.make_user("legacy_member")
        correlation = str(uuid4())
        service = "identity.provisioning"
        job_key = json.dumps([legacy_user.pk, correlation], separators=(",", ":"))
        op_id = str(worker_operation_id(str(self.org.pk), service, "INITIAL_EMT", job_key))
        with transaction.atomic():
            receipt = append_receipt(
                organization=self.org,
                operation_id=op_id,
                service_identity=service,
                job_key=job_key,
                command_type="INITIAL_EMT",
                request_digest=digest(
                    {"user": legacy_user.pk, "role": "EMT", "origin": "INITIAL_PROVISIONING"}
                ),
                response_status=200,
                response_body={"code": "SAVED", "user_id": legacy_user.pk},
                correlation_id=correlation,
            )
            initial_grant = append_change(
                user=legacy_user,
                change=RoleChange(Role.EMT, "GRANT", None, 1),
                receipt=receipt,
                actor_roles=(),
            )
            append_audit(
                organization=self.org,
                command=receipt,
                service_identity=service,
                job_key=job_key,
                roles_at_time=[],
                request_attempt_id=correlation,
                correlation_id=correlation,
                action="INITIAL_EMT",
                outcome="SUCCEEDED",
                target_type="USER",
                target_id=legacy_user.pk,
                new_snapshot={
                    "schema_version": 1,
                    "values": {"role": "EMT", "action": "GRANT"},
                },
            )

        client = self.client_for(legacy_user)
        self.assertEqual(client.get("/fleet/").status_code, 200)

        # 1. Canonical CREW grant on existing EMT history must NOT duplicate grant
        grant_op = str(uuid4())
        res_grant = change_role(
            role="CREW",
            action="GRANT",
            organization_id=str(self.org.pk),
            actor_id=str(admin.pk),
            target_id=str(legacy_user.pk),
            operation_id=grant_op,
            correlation_id=str(uuid4()),
            request_attempt_id=str(uuid4()),
        )
        self.assertEqual(res_grant.status, 200)
        self.assertEqual(res_grant.body["code"], "SAVED")
        self.assertEqual(RoleGrant.objects.filter(user=legacy_user).count(), 1)
        self.assertEqual(RoleGrant.objects.filter(user=legacy_user).first().role, "EMT")
        self.assertEqual(current_role_codes(legacy_user), ("CREW",))

        # Direct database guard: inserting duplicate base role grant fails closed in MariaDB
        guard_fields = dict(
            organization=self.org,
            user=legacy_user,
            command=receipt,
            actor_user=None,
            actor_roles_at_time=[],
            recorded_at=receipt.recorded_at,
            sequence=2,
            role="CREW",
            action="GRANT",
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            RoleGrant.objects.create(**guard_fields)

        # Direct database guard: raw CREW revoke referencing an EMT grant is
        # rejected with role revocation mismatch
        invalid_revoke_fields = dict(
            organization=self.org,
            user=legacy_user,
            command=receipt,
            actor_user=None,
            actor_roles_at_time=[],
            recorded_at=receipt.recorded_at,
            sequence=2,
            role="CREW",
            action="REVOKE",
            prior_grant=initial_grant,
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            RoleGrant.objects.create(**invalid_revoke_fields)

        # Denial audit for legacy user records canonical normalized roles_at_time=['CREW']
        admin_response = client.get("/administration/")
        self.assertEqual(admin_response.status_code, 403)
        denial_audit = AuditEvent.objects.filter(
            actor_user=legacy_user, action="VIEW_ADMINISTRATION", outcome="DENIED"
        ).first()
        self.assertIsNotNone(denial_audit)
        self.assertEqual(denial_audit.roles_at_time, ["CREW"])

        # 2. Canonical CREW revoke must actually revoke the legacy grant and remove effective access
        revoke_op = str(uuid4())
        res_revoke = change_role(
            role="CREW",
            action="REVOKE",
            organization_id=str(self.org.pk),
            actor_id=str(admin.pk),
            target_id=str(legacy_user.pk),
            operation_id=revoke_op,
            correlation_id=str(uuid4()),
            request_attempt_id=str(uuid4()),
        )
        self.assertEqual(res_revoke.status, 200)
        self.assertEqual(res_revoke.body["code"], "SAVED")
        self.assertEqual(RoleGrant.objects.filter(user=legacy_user).count(), 2)
        revoke_grant = RoleGrant.objects.filter(user=legacy_user, sequence=2).get()
        self.assertEqual(revoke_grant.role, "EMT")
        self.assertEqual(revoke_grant.action, "REVOKE")
        self.assertEqual(revoke_grant.prior_grant_id, initial_grant.id)

        # Effective permissions removed
        self.assertEqual(current_role_codes(legacy_user), ())
        self.assertEqual(client.get("/fleet/").status_code, 403)

        # Re-revoking is an already-satisfied no-op
        res_re_revoke = change_role(
            role="CREW",
            action="REVOKE",
            organization_id=str(self.org.pk),
            actor_id=str(admin.pk),
            target_id=str(legacy_user.pk),
            operation_id=str(uuid4()),
            correlation_id=str(uuid4()),
            request_attempt_id=str(uuid4()),
        )
        self.assertEqual(res_re_revoke.status, 200)
        self.assertEqual(RoleGrant.objects.filter(user=legacy_user).count(), 2)

        # 3. Regrant CREW succeeds coherently
        regrant_op = str(uuid4())
        res_regrant = change_role(
            role="CREW",
            action="GRANT",
            organization_id=str(self.org.pk),
            actor_id=str(admin.pk),
            target_id=str(legacy_user.pk),
            operation_id=regrant_op,
            correlation_id=str(uuid4()),
            request_attempt_id=str(uuid4()),
        )
        self.assertEqual(res_regrant.status, 200)
        self.assertEqual(RoleGrant.objects.filter(user=legacy_user).count(), 3)
        regrant_row = RoleGrant.objects.filter(user=legacy_user, sequence=3).get()
        self.assertEqual(regrant_row.role, "CREW")
        self.assertEqual(regrant_row.action, "GRANT")
        self.assertIsNone(regrant_row.prior_grant_id)

        # Effective permissions restored
        self.assertEqual(current_role_codes(legacy_user), ("CREW",))
        self.assertEqual(client.get("/fleet/").status_code, 200)

        # Replay with regrant_op returns cached receipt response
        res_replay = change_role(
            role="CREW",
            action="GRANT",
            organization_id=str(self.org.pk),
            actor_id=str(admin.pk),
            target_id=str(legacy_user.pk),
            operation_id=regrant_op,
            correlation_id=str(uuid4()),
            request_attempt_id=str(uuid4()),
        )
        self.assertEqual(res_replay.status, 200)
        self.assertEqual(res_replay.body, res_regrant.body)
        self.assertEqual(RoleGrant.objects.filter(user=legacy_user).count(), 3)

    def test_audit_repository_role_validation(self):
        # CREW, EMT, and other canonical roles are permitted in audit_repository
        for allowed in (["CREW"], ["EMT"], ["SUPERVISOR"], ["CREW", "ADMINISTRATOR"]):
            with self.subTest(roles=allowed):
                with transaction.atomic():
                    event = append_audit(
                        organization=self.org,
                        actor_user=self.make_user(str(uuid4())[:8]),
                        roles_at_time=allowed,
                        request_attempt_id=str(uuid4()),
                        correlation_id=str(uuid4()),
                        action="TEST_ACTION",
                        outcome="SUCCEEDED",
                    )
                self.assertEqual(event.roles_at_time, allowed)

        # Unknown roles are rejected by audit_repository
        with self.assertRaises(ValidationError):
            with transaction.atomic():
                append_audit(
                    organization=self.org,
                    actor_user=self.make_user(str(uuid4())[:8]),
                    roles_at_time=["INVALID_ROLE"],
                    request_attempt_id=str(uuid4()),
                    correlation_id=str(uuid4()),
                    action="TEST_ACTION",
                    outcome="SUCCEEDED",
                )
