"""Issue #15 crew actions over synthetic HTTP and the real command layer."""

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from django.test import Client, override_settings

from cya.deficiencies.commands import (
    assign_deficiency,
    record_correction,
    report_deficiency,
    set_deadline,
    start_work,
)
from cya.deficiencies.models import DeficiencyEvent
from cya.inspections.tests.test_crew_ui import CrewUiCase


class DeficiencyActionsUiTests(CrewUiCase):
    def report(self, **fields):
        return self.failed(actor=self.user, create=self.new_fields(**fields)).body[
            "deficiency_ids"
        ][0]

    def latest(self, pk):
        return str(DeficiencyEvent.objects.filter(deficiency_id=pk).latest("sequence").pk)

    def change(self, pk, action, **fields):
        return self.post(
            f"/api/v1/deficiencies/{pk}/{action}",
            {
                "operation_id": str(uuid4()),
                "expected_event_id": self.latest(pk),
                **fields,
            },
        )

    def detail(self, pk):
        return self.client.get(f"/deficiencies/{pk}/")

    def pending(self, item=None, **fields):
        pk = report_deficiency(
            vehicle_id=str(self.vehicle.pk),
            target={"item_id": str((item or self.suction).pk)},
            **self.new_fields(**fields),
            **self.context(self.user),
        ).body["deficiency_id"]
        start_work(deficiency_id=pk, expected_event_id=self.latest(pk), **self.context(self.admin))
        record_correction(
            deficiency_id=pk, expected_event_id=self.latest(pk), **self.context(self.user)
        )
        self.clock.set(self.now + timedelta(seconds=2))
        return pk

    def test_crew_forms_and_dependency_notes_round_trip_with_stale_refusal(self):
        pk = self.report()
        page = self.detail(pk)
        self.assertContains(page, 'data-kind="extension"')
        self.assertContains(page, 'data-kind="note"')
        self.assertContains(page, 'data-kind="dependency"')
        self.assertNotContains(page, 'data-kind="dependency-exit"')
        event = self.latest(pk)
        entered = self.change(
            pk,
            "dependencies",
            explanation="<script>Synthetic vendor</script>",
            follow_up_at=(self.now + timedelta(days=2)).isoformat(),
        )
        self.assertEqual(entered.status_code, 201, entered.content)
        page = self.detail(pk)
        self.assertContains(page, 'data-kind="dependency-exit"')
        self.assertContains(page, "&lt;script&gt;Synthetic vendor&lt;/script&gt;")
        self.assertContains(page, "No automatic reminder")
        stale = self.post(
            f"/api/v1/deficiencies/{pk}/progress-notes",
            {
                "operation_id": str(uuid4()),
                "expected_event_id": event,
                "note": "Synthetic stale",
            },
        )
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "DEFICIENCY_CHANGED"))
        self.assertEqual(
            self.change(pk, "dependency-revisions", explanation="Synthetic revised").status_code,
            201,
        )
        self.assertEqual(
            self.change(pk, "progress-notes", note="Synthetic progress").status_code, 201
        )
        self.assertContains(self.detail(pk), "Synthetic progress")
        self.assertEqual(self.change(pk, "dependency-exits").status_code, 201)
        self.assertContains(self.detail(pk), "Dependency ended")

    def test_role_union_supply_context_and_assignee_control_visibility(self):
        pk = self.report()
        for roles, dependency, extension in [
            ({"CREW"}, True, True),
            ({"SUPERVISOR"}, True, True),
            ({"ADMINISTRATOR"}, True, True),
            ({"SUPPLY_OFFICER"}, False, False),
            ({"SUPPLY_OFFICER", "CREW"}, True, True),
        ]:
            with (
                self.subTest(roles=roles),
                mock.patch("cya.inspections.workflow_views.roles_for", return_value=roles),
            ):
                page = self.detail(pk)
                self.assertEqual(page.context["can_update_dependency"], dependency)
                self.assertEqual(page.context["can_request_extension"], extension)
                self.assertEqual(page.context["can_add_note"], dependency)
        assign_deficiency(
            deficiency_id=pk,
            assignee_id=str(self.user.pk),
            expected_event_id=self.latest(pk),
            **self.context(self.admin),
        )
        with mock.patch(
            "cya.inspections.workflow_views.roles_for", return_value={"SUPPLY_OFFICER"}
        ):
            self.assertTrue(self.detail(pk).context["can_request_extension"])
            self.assertFalse(self.detail(pk).context["can_update_dependency"])
        supply_pk = self.report(category=self.stock)
        with mock.patch(
            "cya.inspections.workflow_views.roles_for", return_value={"SUPPLY_OFFICER"}
        ):
            self.assertTrue(self.detail(supply_pk).context["can_update_dependency"])
            self.assertTrue(self.detail(supply_pk).context["can_add_note"])

    def test_deadline_extension_fields_and_idempotent_replay(self):
        pk = self.report()
        assign_deficiency(
            deficiency_id=pk,
            assignee_id=str(self.user.pk),
            expected_event_id=self.latest(pk),
            **self.context(self.admin),
        )
        set_deadline(
            deficiency_id=pk,
            deadline=self.now + timedelta(days=2),
            expected_event_id=self.latest(pk),
            **self.context(self.admin),
        )
        body = {
            "operation_id": str(uuid4()),
            "expected_event_id": self.latest(pk),
            "reason": "Synthetic parts delay",
            "proposed_deadline": (self.now + timedelta(days=5)).isoformat(),
        }
        url = f"/api/v1/deficiencies/{pk}/extension-requests"
        first = self.post(url, body)
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(first.json()["result"], self.post(url, body).json()["result"])
        page = self.detail(pk)
        for text in (
            "Governing deadline",
            "Age since original report",
            "Synthetic parts delay",
            self.user.display_name,
            "2026-06-06",
        ):
            self.assertContains(page, text)
        denied = self.change(
            pk,
            "extension-decisions",
            request_event_id=first.json()["result"]["event_id"],
            decision="APPROVED",
            explanation="Synthetic forged approval",
        )
        self.assertEqual(denied.status_code, 403)

    def test_verification_screen_uses_check_protocol_and_closes_once(self):
        pk = self.pending()
        page = self.detail(pk)
        self.assertContains(page, "Outstanding")
        self.assertContains(page, "Perform verification checks")
        screen = self.client.get(f"/deficiencies/{pk}/verification/")
        self.assertEqual(screen.status_code, 200)
        self.assertNotContains(screen, 'id="odometer-form"')
        config = self.config(screen)
        self.assertEqual(config["verification_for_deficiency_id"], pk)
        self.assertNotIn("session_id", config)
        items = self.client.get(config["urls"]["checklist"]).json()
        self.assertEqual(len(items["items"]), 1)
        row = items["items"][0]
        body = {
            "operation_id": str(uuid4()),
            "vehicle_id": str(self.vehicle.pk),
            "item_id": row["item_id"],
            "checklist_version_id": row["checklist_version_id"],
            "basis_check_id": row["latest_check_id"],
            "outcome": "PASSED",
            "purpose": "VERIFICATION",
            "verification_for_deficiency_id": pk,
        }
        saved = self.post("/api/v1/checks", body)
        self.assertEqual(saved.status_code, 201, saved.content)
        self.assertEqual(saved.json()["result"]["verification"]["status"], "CLOSED_VERIFIED")
        self.assertEqual(saved.json()["result"], self.post("/api/v1/checks", body).json()["result"])
        closed = self.detail(pk)
        self.assertContains(closed, "Closed — Verified")
        self.assertNotContains(closed, 'data-kind="note"')
        self.assertEqual(self.client.get(config["urls"]["checklist"]).json()["items"], [])

    def test_scope_keeps_each_physical_kit_and_routine_pass_does_not_verify(self):
        pk = self.pending(self.kit_item, summary="Synthetic kit deficiency")
        # A routine Pass can satisfy its ordinary obligation, but not this scope.
        self.record(self.kit_item, actor=self.user, kit_id=self.reference)
        items = self.client.get(f"/deficiencies/{pk}/verification-items/").json()
        self.assertEqual({r["kit_id"] for r in items["items"]}, {self.reference, self.kit_b})
        self.assertEqual(items["remaining_scope_rows"], 2)
        self.assertContains(self.detail(pk), "Outstanding", count=2)

    def test_verification_failure_preserves_history_and_returns_to_work(self):
        pk = self.pending()
        row = self.client.get(f"/deficiencies/{pk}/verification-items/").json()["items"][0]
        failed = self.post(
            "/api/v1/checks",
            {
                "operation_id": str(uuid4()),
                "vehicle_id": str(self.vehicle.pk),
                "item_id": row["item_id"],
                "checklist_version_id": row["checklist_version_id"],
                "basis_check_id": row["latest_check_id"],
                "outcome": "FAILED",
                "purpose": "VERIFICATION",
                "verification_for_deficiency_id": pk,
            },
        )
        self.assertEqual(failed.status_code, 201, failed.content)
        self.assertContains(self.detail(pk), "Verification failed — back in progress")
        self.assertContains(self.detail(pk), "Repair recorded")
        self.assertContains(self.detail(pk), "Record repair")

    def test_verification_access_and_fail_closed_unavailable(self):
        pk = self.pending()
        for route in ("verification", "verification-items"):
            for identity in (str(uuid4()), "malformed"):
                self.assertEqual(
                    self.client.get(f"/deficiencies/{identity}/{route}/").status_code, 404
                )
            with mock.patch(
                "cya.inspections.workflow_views.verification_scope",
                side_effect=ValueError("Synthetic missing evidence"),
            ):
                response = self.client.get(f"/deficiencies/{pk}/{route}/")
                self.assertEqual(response.status_code, 503)
                self.assertNotContains(response, "Synthetic missing evidence", status_code=503)

    def test_removed_current_checklist_rows_remain_outstanding(self):
        pk = self.pending()
        with mock.patch(
            "cya.inspections.workflow_views.vehicle_checklist",
            return_value=SimpleNamespace(
                rows=[],
                vehicle_status={"status": "IN_SERVICE", "restrictions": None},
            ),
        ):
            response = self.client.get(f"/deficiencies/{pk}/verification-items/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])
        self.assertEqual(response.json()["remaining_scope_rows"], 1)
        self.assertEqual(response.json()["unavailable_scope_rows"], 1)

    def test_verification_routes_require_sign_in_and_roles_and_scope_the_organization(self):
        pk = self.pending()
        for route in ("verification", "verification-items"):
            url = f"/deficiencies/{pk}/{route}/"
            self.assertEqual(Client().get(url).status_code, 302)
            with override_settings(CYA_ROLE_LOOKUP=None):
                self.assertEqual(self.client.get(url).status_code, 403)
            # Exercise the reader's tenant boundary for an already-authorized other-org actor.
            other_actor = SimpleNamespace(organization_id=str(uuid4()))
            with mock.patch(
                "cya.inspections.workflow_views._authorized", return_value=(other_actor, None)
            ):
                self.assertEqual(self.client.get(url).status_code, 404)
