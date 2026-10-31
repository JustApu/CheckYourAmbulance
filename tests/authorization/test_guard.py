import unittest

from cya.authorization.guard import (
    AuditUnavailable,
    PermissionDenied,
    Principal,
    record_denial,
    require_permission,
)


def actor(roles=("CREW",), organization="org", active=True):
    return Principal(organization, "actor", frozenset(roles), active)


class GuardTests(unittest.TestCase):
    def test_forbidden_direct_command_never_runs_mutation(self):
        mutations = []
        with self.assertRaises(PermissionDenied) as caught:
            require_permission(actor(), "org", "manage_users_roles")
            mutations.append("role granted")
        audit = []
        record_denial(caught.exception, audit.append)
        self.assertEqual(mutations, [])
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0].actor_user_id, "actor")
        self.assertEqual(audit[0].actor_roles, ("CREW",))
        self.assertEqual(audit[0].action, "manage_users_roles")

    def test_unlisted_action_has_safe_denial_record(self):
        with self.assertRaises(PermissionDenied) as caught:
            require_permission(actor(("ADMINISTRATOR",)), "org", "private-input" * 1000)
        audit = []
        record_denial(caught.exception, audit.append)
        self.assertEqual(audit[0].action, "UNLISTED_ACTION")
        self.assertNotIn("private-input", repr(audit))

    def test_no_role_or_permission_cache_survives_revocation(self):
        require_permission(actor(("ADMINISTRATOR",)), "org", "manage_users_roles")
        with self.assertRaises(PermissionDenied):
            require_permission(actor(), "org", "manage_users_roles")

    def test_inactive_missing_and_cross_organization_principals_are_denied(self):
        for principal in (actor(active=False), None, actor(organization="other")):
            with self.subTest(principal=principal):
                with self.assertRaises(PermissionDenied) as caught:
                    require_permission(principal, "org", "record_check")
                if principal is None or principal.organization_id != "org":
                    self.assertIsNone(caught.exception.denial.actor_user_id)

    def test_denial_audit_failure_has_no_success_fallback(self):
        with self.assertRaises(PermissionDenied) as caught:
            require_permission(actor(), "org", "manage_users_roles")

        def unavailable(denial):
            raise OSError("synthetic audit failure")

        for sink in (None, unavailable):
            with self.subTest(sink=sink):
                with self.assertRaises(AuditUnavailable):
                    record_denial(caught.exception, sink)
