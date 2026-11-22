"""A shared item appears in every checklist that lists it (SPEC 6.0.0 VW-5, AC-42 (2); issue #256 Part A).

Two checklists bound to one vehicle both list the suction item. A session's checklist screen
reads the rows of its own checklist version, so the shared item must be in each checklist's
rows, with one shared obligation and attention state. A read without a checklist version keeps
one row per item, as before.
"""

from uuid import uuid4

from django.test import override_settings

from cya.attention.checklist import ChecklistQueryError, vehicle_checklist
from cya.attention.tests.support import AttentionCase
from cya.checklists import commands as checklist_commands
from cya.checklists.models import ChecklistTemplate, Item
from cya.checks.commands import record_check
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings


class SharedChecklistMixin:
    def add_second_checklist(self):
        """Bind a second checklist that also lists the suction item, plus one item of its own."""
        self.second_only = Item.objects.create(organization=self.org, label="Synthetic second only")
        template = ChecklistTemplate.objects.create(
            organization=self.org, label="Synthetic second checklist"
        )
        version = checklist_commands.save_template_draft(
            template_id=str(template.pk),
            items=[self.spec(self.second_only, 5), self.spec(self.suction, 15)],
            **self.context(self.admin),
        ).body["version_id"]
        checklist_commands.publish_template_version(
            version_id=version, effective_at=None, **self.context(self.admin)
        )
        self.bind(self.vehicle, template=template, reconcile=False)
        self.assertEqual(self.generate().failures, [])
        self.second_version_id = version
        self.second_template = template

    def checklist_rows(self, version_id, **kw):
        return vehicle_checklist(
            str(self.org.pk), str(self.vehicle.pk), checklist_version_id=version_id, **kw
        ).rows

    def ids(self, rows):
        return [r.item_id for r in rows]


class SharedChecklistRowTests(SharedChecklistMixin, AttentionCase):
    def setUp(self):
        super().setUp()
        self.add_second_checklist()

    def test_a_read_without_a_checklist_keeps_one_row_per_item(self):
        rows = self.rows()
        self.assertEqual(self.ids(rows).count(str(self.suction.pk)), 1)
        self.assertIn(str(self.second_only.pk), self.ids(rows))
        # The vehicle-wide row carries one checklist's version; before VW-5 this was the only
        # row, so a session on the other checklist (which filters by its version) lost the item.
        # The first template by id wins, as before.
        versions = {
            str(self.template.pk): self.version_id,
            str(self.second_template.pk): self.second_version_id,
        }
        first = ChecklistTemplate.objects.filter(pk__in=list(versions)).order_by("pk").first().pk
        suction = next(r for r in rows if r.item_id == str(self.suction.pk))
        self.assertEqual(suction.checklist_version_id, versions[str(first)])

    def test_each_checklist_lists_the_shared_item_with_its_own_rows_only(self):
        second = self.checklist_rows(self.second_version_id)
        self.assertEqual(
            self.ids(second), [str(self.second_only.pk), str(self.suction.pk)]
        )  # the second checklist's own order
        self.assertTrue(all(r.checklist_version_id == self.second_version_id for r in second))
        first = self.checklist_rows(self.version_id)
        self.assertIn(str(self.suction.pk), self.ids(first))
        self.assertIn(str(self.masks.pk), self.ids(first))
        self.assertNotIn(str(self.second_only.pk), self.ids(first))
        self.assertTrue(all(r.checklist_version_id == self.version_id for r in first))

    def test_the_shared_item_has_one_state_in_both_checklists(self):
        def shared(version_id):
            return next(
                r for r in self.checklist_rows(version_id) if r.item_id == str(self.suction.pk)
            )

        self.assertEqual(
            (shared(self.version_id).obligation_state, shared(self.version_id).due_at),
            (
                shared(self.second_version_id).obligation_state,
                shared(self.second_version_id).due_at,
            ),
        )
        recorded = record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.suction.pk),
            checklist_version_id=self.second_version_id,
            outcome="PASSED",
            purpose="ROUTINE",
            **self.context(self.crew),
        )
        check_id = recorded.body["check_id"]
        for version_id in (self.version_id, self.second_version_id):
            row = shared(version_id)
            self.assertEqual(row.latest_check_id, check_id)
            self.assertEqual(row.latest_check_outcome, "PASSED")

    def test_progress_counts_the_checklists_own_rows(self):
        result = vehicle_checklist(
            str(self.org.pk), str(self.vehicle.pk), checklist_version_id=self.second_version_id
        )
        self.assertEqual(sum(g["total"] for g in result.group_progress), len(result.rows))

    def test_an_unknown_checklist_version_lists_nothing(self):
        self.assertEqual(self.checklist_rows(str(uuid4())), ())

    def test_a_malformed_checklist_version_is_refused(self):
        with self.assertRaises(ChecklistQueryError) as caught:
            self.checklist_rows("not-a-uuid")
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("INVALID_CHECKLIST_VERSION", 400)
        )


@override_settings(**real_collaborator_settings())
class SharedChecklistApiTests(SharedChecklistMixin, AttentionCase):
    def make_org(self):
        self.harness = RoleHttpHarness()
        return self.harness.org

    def setUp(self):
        super().setUp()
        self.add_second_checklist()
        self.client, self.user = self.harness.sign_in("crew-http")

    def get(self, query):
        return self.client.get(f"/api/v1/vehicles/{self.vehicle.pk}/checklist{query}")

    def test_the_session_screen_reads_its_checklists_rows(self):
        body = self.get(
            f"?needs_attention_only=false&checklist_version_id={self.second_version_id}"
        ).json()
        self.assertEqual(
            [i["item_id"] for i in body["items"]],
            [str(self.second_only.pk), str(self.suction.pk)],
        )
        self.assertEqual(sum(g["total"] for g in body["group_progress"]), 2)

    def test_a_malformed_checklist_version_is_a_client_error(self):
        response = self.get("?checklist_version_id=not-a-uuid")
        self.assertEqual(
            (response.status_code, response.json()["code"]), (400, "INVALID_CHECKLIST_VERSION")
        )
