"""Offline tests of the actual policy against all approved permission cells."""

import json
import unittest
from pathlib import Path

from cya.authorization.policy import (
    POLICY,
    ROLE_ORDER,
    ResourceContext,
    permitted,
    unable_self_completion_allowed,
)

ROOT = Path(__file__).resolve().parents[2]


class PolicyTests(unittest.TestCase):
    def test_unable_self_completion_provisional_policy(self):
        self.assertTrue(unable_self_completion_allowed("supervisor", "supervisor"))
        self.assertFalse(unable_self_completion_allowed("supervisor", "crew"))
        self.assertFalse(unable_self_completion_allowed(None, "crew"))

    def test_every_matrix_cell_matches_the_production_policy(self):
        matrix = json.loads((ROOT / "tests/contracts/permission-matrix.json").read_text())
        self.assertEqual(set(POLICY), {row["action"] for row in matrix["rows"]})
        for row in matrix["rows"]:
            for index, role in enumerate(ROLE_ORDER):
                with self.subTest(action=row["action"], role=role):
                    actual = POLICY[row["action"]][index]
                    if actual == "other_only":
                        # INV-5's default is stricter than the matrix's role cell.
                        actual = "allow"
                    self.assertEqual(
                        actual,
                        row["by_role"][
                            {
                                "CREW": "Crew",
                                "EMT": "Crew",
                                "SUPERVISOR": "Supervisor",
                                "SUPPLY_OFFICER": "Supply Officer",
                                "ADMINISTRATOR": "Administrator",
                            }.get(role.value, role.value)
                        ],
                    )

    def test_all_permission_case_variants(self):
        cases = json.loads((ROOT / "tests/contracts/permission-cases.json").read_text())["tests"]
        for case in cases:
            for variant in case["tests"]:
                name = variant["context"]
                context = ResourceContext(
                    author_id="actor" if name == "actor_is_author" else "other",
                    assignee_id="actor" if name == "actor_is_assignee" else "other",
                    supply_related=name == "supply_deficiency",
                    intent={"status_change_request": "request", "view": "view"}.get(name, "mutate"),
                )
                with self.subTest(case=case["id"], context=name):
                    self.assertEqual(
                        permitted(
                            case["action"],
                            "actor",
                            [
                                {
                                    "Crew": "CREW",
                                    "EMT": "CREW",
                                    "Supervisor": "SUPERVISOR",
                                    "Supply Officer": "SUPPLY_OFFICER",
                                    "Administrator": "ADMINISTRATOR",
                                }.get(case["actor_role"], case["actor_role"])
                            ],
                            context=context,
                        ),
                        variant["expected"] in ("allow", "allow_if_other_preconditions_met"),
                    )

    def test_union_of_roles_does_not_require_every_role_to_allow(self):
        self.assertTrue(permitted("manage_users_roles", "actor", ["EMT", "ADMINISTRATOR"]))
        self.assertTrue(permitted("assign_deficiency_deadline", "actor", ["EMT", "SUPERVISOR"]))
        self.assertTrue(permitted("update_external_dependency", "actor", ["SUPPLY_OFFICER", "EMT"]))

    def test_unknown_missing_inactive_and_malformed_facts_fail_closed(self):
        for roles in ([], "ADMINISTRATOR", ["unknown"], ["ADMINISTRATOR", "unknown"], [None]):
            with self.subTest(roles=roles):
                self.assertFalse(permitted("manage_users_roles", "actor", roles))
        for actor in (None, "", 42):
            self.assertFalse(permitted("record_check", actor, ["EMT"]))
        self.assertFalse(permitted("record_check", "actor", ["EMT"], active=False))
        self.assertFalse(permitted("record_check", "actor", ["EMT"], active=1))
        self.assertFalse(permitted("unlisted_action", "actor", ["ADMINISTRATOR"]))
        self.assertFalse(permitted(None, "actor", ["ADMINISTRATOR"]))
        self.assertFalse(permitted("correct_check", "actor", ["ADMINISTRATOR"]))
        self.assertFalse(permitted("record_check", "actor", ["EMT"], context={"intent": "mutate"}))

    def test_admin_cannot_invalidate_own_check(self):
        self.assertFalse(
            permitted(
                "invalidate_others_check",
                "actor",
                ["ADMINISTRATOR"],
                context=ResourceContext(author_id="actor"),
            )
        )

    def test_truthy_supply_flag_is_not_a_boolean(self):
        for value in ("false", "true", 1, None):
            self.assertFalse(
                permitted(
                    "assign_deficiency_deadline",
                    "actor",
                    ["SUPPLY_OFFICER"],
                    context=ResourceContext(supply_related=value),
                )
            )

    def test_supply_conditions_and_role_union(self):
        for action in ("assign_deficiency_deadline", "update_external_dependency"):
            for scope in (False, True):
                with self.subTest(action=action, scope=scope):
                    context = ResourceContext(supply_related=scope)
                    self.assertEqual(
                        permitted(action, "actor", ["SUPPLY_OFFICER"], context=context), scope
                    )
                    self.assertTrue(
                        permitted(
                            action, "actor", ["SUPPLY_OFFICER", "SUPERVISOR"], context=context
                        )
                    )

    def test_supply_extension_requires_assignment_unless_another_role_allows(self):
        for assignee in (None, "other", "actor"):
            with self.subTest(assignee=assignee):
                context = ResourceContext(assignee_id=assignee)
                self.assertEqual(
                    permitted("request_extension", "actor", ["SUPPLY_OFFICER"], context=context),
                    assignee == "actor",
                )
                self.assertTrue(
                    permitted(
                        "request_extension", "actor", ["SUPPLY_OFFICER", "EMT"], context=context
                    )
                )

    def test_mixed_admin_roles_still_cannot_invalidate_own_check(self):
        for author in (None, "actor", "other"):
            with self.subTest(author=author):
                self.assertEqual(
                    permitted(
                        "invalidate_others_check",
                        "actor",
                        ROLE_ORDER,
                        context=ResourceContext(author_id=author),
                    ),
                    author == "other",
                )
