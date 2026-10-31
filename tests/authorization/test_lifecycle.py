import unittest
from dataclasses import replace
from uuid import uuid4

from cya.authorization.lifecycle import (
    BootstrapRefused,
    InvalidRoleHistory,
    LastAdministrator,
    RoleEvent,
    current_roles,
    plan_first_administrator,
    plan_initial_crew,
    plan_initial_emt,
    plan_role_change,
    validate_deactivation,
)
from cya.authorization.policy import Role


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.org = str(uuid4())
        self.user = str(uuid4())
        self.grant = RoleEvent(str(uuid4()), self.org, self.user, 1, Role.EMT, "GRANT")

    def state(self, events):
        return current_roles(events, organization_id=self.org, user_id=self.user)

    def plan(self, state, role, action, active=True, admins=frozenset()):
        return plan_role_change(
            state, role, action, target_active=active, active_administrator_ids=admins
        )

    def test_initial_empty_state_has_no_permissions(self):
        state = self.state([])
        self.assertEqual(dict(state.grants), {})
        self.assertEqual(state.sequence, 0)

    def test_contract_role_codes_fold_without_display_label_mapping(self):
        codes = ("CREW", "SUPERVISOR", "SUPPLY_OFFICER", "ADMINISTRATOR")
        events = [
            RoleEvent(str(uuid4()), self.org, self.user, index, code, "GRANT")
            for index, code in enumerate(codes, 1)
        ]
        self.assertEqual(set(self.state(events).grants), set(codes))

    def test_grant_revoke_regrant_retains_exact_grant_identity(self):
        revoke = replace(
            self.grant, id=str(uuid4()), sequence=2, action="REVOKE", prior_grant_id=self.grant.id
        )
        regrant = replace(self.grant, id=str(uuid4()), sequence=3)
        state = self.state([self.grant, revoke, regrant])
        self.assertEqual(dict(state.grants), {Role.EMT: regrant.id})
        self.assertEqual(state.sequence, 3)
        with self.assertRaises(InvalidRoleHistory):
            self.state([self.grant, revoke, regrant, replace(revoke, id=str(uuid4()), sequence=4)])

    def test_wrong_scope_duplicate_ids_gaps_and_unknown_roles_fail_closed(self):
        malformed = (
            replace(self.grant, organization_id=str(uuid4())),
            replace(self.grant, user_id=str(uuid4())),
            replace(self.grant, id="invalid"),
            replace(self.grant, sequence=2),
            replace(self.grant, sequence=True),
            replace(self.grant, role="invented"),
            replace(self.grant, action="UPDATE"),
            replace(self.grant, prior_grant_id=str(uuid4())),
        )
        for event in malformed:
            with self.subTest(event=event), self.assertRaises(InvalidRoleHistory):
                self.state([event])
        with self.assertRaises(InvalidRoleHistory):
            self.state([self.grant, replace(self.grant, sequence=2, role=Role.SUPERVISOR)])

    def test_duplicate_grants_and_unmatched_revocations_are_rejected(self):
        with self.assertRaises(InvalidRoleHistory):
            self.state([self.grant, replace(self.grant, id=str(uuid4()), sequence=2)])
        with self.assertRaises(InvalidRoleHistory):
            self.state(
                [self.grant, replace(self.grant, id=str(uuid4()), sequence=2, role=Role.CREW)]
            )
        crew_grant = replace(self.grant, role=Role.CREW)
        with self.assertRaises(InvalidRoleHistory):
            self.state(
                [crew_grant, replace(crew_grant, id=str(uuid4()), sequence=2, role=Role.EMT)]
            )
        for prior in (None, str(uuid4())):
            with self.subTest(prior=prior), self.assertRaises(InvalidRoleHistory):
                self.state(
                    [
                        self.grant,
                        replace(
                            self.grant,
                            id=str(uuid4()),
                            sequence=2,
                            action="REVOKE",
                            prior_grant_id=prior,
                        ),
                    ]
                )

    def test_role_mutations_plan_one_event_or_an_idempotent_noop(self):
        state = self.state([self.grant])
        self.assertIsNone(self.plan(state, Role.EMT, "GRANT"))
        self.assertIsNone(self.plan(state, Role.CREW, "GRANT"))
        self.assertIsNone(self.plan(state, Role.SUPERVISOR, "REVOKE"))
        change = self.plan(state, Role.EMT, "REVOKE")
        self.assertEqual(
            (change.role, change.prior_grant_id, change.sequence), (Role.EMT, self.grant.id, 2)
        )
        change_via_crew = self.plan(state, Role.CREW, "REVOKE")
        self.assertEqual(
            (change_via_crew.role, change_via_crew.prior_grant_id, change_via_crew.sequence),
            (Role.EMT, self.grant.id, 2),
        )
        crew_grant = replace(self.grant, role=Role.CREW)
        crew_state = self.state([crew_grant])
        self.assertIsNone(self.plan(crew_state, Role.CREW, "GRANT"))
        self.assertIsNone(self.plan(crew_state, Role.EMT, "GRANT"))
        crew_revoke = self.plan(crew_state, Role.CREW, "REVOKE")
        self.assertEqual(
            (crew_revoke.role, crew_revoke.prior_grant_id, crew_revoke.sequence),
            (Role.CREW, crew_grant.id, 2),
        )
        crew_revoke_via_emt = self.plan(crew_state, Role.EMT, "REVOKE")
        self.assertEqual(
            (
                crew_revoke_via_emt.role,
                crew_revoke_via_emt.prior_grant_id,
                crew_revoke_via_emt.sequence,
            ),
            (Role.CREW, crew_grant.id, 2),
        )
        empty_state = self.state([])
        self.assertIsNone(self.plan(empty_state, Role.CREW, "REVOKE"))
        self.assertIsNone(self.plan(empty_state, Role.EMT, "REVOKE"))
        grant_from_empty = self.plan(empty_state, Role.CREW, "GRANT")
        self.assertEqual(
            (grant_from_empty.role, grant_from_empty.prior_grant_id, grant_from_empty.sequence),
            (Role.CREW, None, 1),
        )
        grant_emt_from_empty = self.plan(empty_state, Role.EMT, "GRANT")
        self.assertEqual(
            (
                grant_emt_from_empty.role,
                grant_emt_from_empty.prior_grant_id,
                grant_emt_from_empty.sequence,
            ),
            (Role.CREW, None, 1),
        )
        grant = self.plan(state, Role.SUPERVISOR, "GRANT")
        self.assertEqual(
            (grant.role, grant.prior_grant_id, grant.sequence), (Role.SUPERVISOR, None, 2)
        )
        with self.assertRaises(TypeError):
            state.grants[Role.ADMINISTRATOR] = str(uuid4())

    def test_last_active_administrator_cannot_be_revoked_or_disabled(self):
        state = self.state([replace(self.grant, role=Role.ADMINISTRATOR)])
        admins = frozenset({self.user})
        with self.assertRaises(LastAdministrator):
            self.plan(state, Role.ADMINISTRATOR, "REVOKE", admins=admins)
        with self.assertRaises(LastAdministrator):
            validate_deactivation(state, target_active=True, active_administrator_ids=admins)
        others = admins | {str(uuid4())}
        self.assertIsNotNone(self.plan(state, Role.ADMINISTRATOR, "REVOKE", admins=others))
        validate_deactivation(state, target_active=True, active_administrator_ids=others)
        # The next serialized request must use the refreshed snapshot.
        with self.assertRaises(LastAdministrator):
            self.plan(state, Role.ADMINISTRATOR, "REVOKE", admins=admins)

    def test_inactive_admin_and_non_admin_deactivation_do_not_remove_an_active_admin(self):
        admin = self.state([replace(self.grant, role=Role.ADMINISTRATOR)])
        self.assertIsNotNone(self.plan(admin, Role.ADMINISTRATOR, "REVOKE", active=False))
        validate_deactivation(admin, target_active=False, active_administrator_ids=frozenset())
        validate_deactivation(
            self.state([self.grant]), target_active=True, active_administrator_ids=frozenset()
        )

    def test_inconsistent_admin_snapshots_are_rejected(self):
        state = self.state([replace(self.grant, role=Role.ADMINISTRATOR)])
        for admins in (frozenset(), frozenset({"invalid"}), {self.user}):
            with self.subTest(admins=admins), self.assertRaises(InvalidRoleHistory):
                self.plan(state, Role.ADMINISTRATOR, "REVOKE", admins=admins)

    def test_initial_provisioning_plans_only_crew_and_rejects_existing_history(self):
        plan = plan_initial_crew(self.state([]))
        self.assertEqual((plan.role, plan.action, plan.sequence), (Role.CREW, "GRANT", 1))
        self.assertEqual(plan_initial_emt(self.state([])), plan)
        with self.assertRaises(InvalidRoleHistory):
            plan_initial_crew(self.state([self.grant]))

    def test_first_administrator_requires_active_previously_provisioned_crew_or_emt(self):
        for grant_role in (Role.CREW, Role.EMT):
            with self.subTest(grant_role=grant_role):
                state = self.state([replace(self.grant, role=grant_role)])
                plan = plan_first_administrator(
                    state, target_active=True, has_administrator_history=False
                )
                self.assertEqual(
                    (plan.role, plan.action, plan.sequence), (Role.ADMINISTRATOR, "GRANT", 2)
                )
                for active, history in ((False, False), (True, True), (True, None), (True, 0)):
                    with (
                        self.subTest(active=active, history=history),
                        self.assertRaises(BootstrapRefused),
                    ):
                        plan_first_administrator(
                            state, target_active=active, has_administrator_history=history
                        )
                with self.assertRaises(BootstrapRefused):
                    plan_first_administrator(
                        self.state([]), target_active=True, has_administrator_history=False
                    )
