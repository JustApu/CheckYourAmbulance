"""Issue #15 crew actions over synthetic HTTP and the real command layer."""

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from django.test import Client, override_settings

from cya.deficiencies.closure import close_administrator
from cya.deficiencies.commands import (
    assign_deficiency,
    record_correction,
    report_deficiency,
    set_deadline,
    start_work,
)
from cya.deficiencies.models import DeficiencyEvent
from cya.deficiencies.suspension import suspend_checks
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
        self.assertNotContains(page, 'data-kind="extension"')  # no deadline yet (issue 20)
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

    def test_detail_summary_and_next_repair_action_precede_secondary_controls(self):
        pk = self.report(summary="Synthetic pump issue")
        opened = self.detail(pk)
        self.assertContains(opened, "Synthetic pump issue")
        self.assertContains(opened, "Affected scope")
        self.assertContains(opened, self.suction.label)
        self.assertContains(opened, "Next action")
        self.assertContains(opened, "Work updates and dependencies")
        self.assertContains(opened, 'data-kind="note"')
        self.assertContains(
            opened, '<details class="panel deficiency-details" id="work-updates">', html=False
        )
        with mock.patch("cya.inspections.workflow_views.roles_for", return_value={"SUPERVISOR"}):
            supervisor_open = self.detail(pk)
        self.assertContains(supervisor_open, "Start work")
        self.assertLess(
            supervisor_open.content.decode().index("Next action"),
            supervisor_open.content.decode().index('data-kind="start-work"'),
        )

        start_work(
            deficiency_id=pk,
            expected_event_id=self.latest(pk),
            **self.context(self.admin),
        )
        page = self.detail(pk)
        markup = page.content.decode()
        self.assertContains(page, "In Progress")
        self.assertContains(page, "Record repair")
        self.assertEqual(markup.count('data-kind="repair"'), 1)
        self.assertLess(markup.index("Next action"), markup.index('data-kind="repair"'))
        self.assertLess(
            markup.index('data-kind="repair"'), markup.index("Work updates and dependencies")
        )

    def test_role_union_supply_context_and_assignee_control_visibility(self):
        pk = self.report()
        # Without a deadline the server answers NO_DEADLINE, so no role is offered the form.
        self.assertFalse(self.detail(pk).context["can_request_extension"])
        set_deadline(
            deficiency_id=pk,
            deadline=self.now + timedelta(days=2),
            expected_event_id=self.latest(pk),
            **self.context(self.admin),
        )
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
        self.assertContains(self.detail(pk), 'data-kind="extension"')
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
        self.assertContains(page, "0 of 1 checks verified")
        self.assertContains(page, "0 of 1 verified · 1 outstanding")
        self.assertEqual(
            page.context["verification_progress"],
            {"total": 1, "verified": 0, "outstanding": 1},
        )
        screen = self.client.get(f"/deficiencies/{pk}/verification/")
        self.assertEqual(screen.status_code, 200)
        self.assertNotContains(screen, 'id="odometer-form"')
        config = self.config(screen)
        self.assertEqual(config["verification_for_deficiency_id"], pk)
        self.assertEqual(config["time_zone"], self.org.station_zone)
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

    def test_history_counts_the_check_suspensions_a_closure_ended(self):
        def suspend(pk, *items):
            suspend_checks(
                deficiency_id=pk,
                expected_event_id=self.latest(pk),
                items=[{"item_id": str(item.pk)} for item in items],
                **self.context(self.supervisor),
            )

        verified = self.pending()
        suspend(verified, self.suction)
        page = self.detail(verified)
        self.assertContains(page, "Routine checks suspended")
        self.assertContains(page, "Check suspensions — 1 in force")
        self.assertNotContains(page, "Check suspensions ended")
        row = self.client.get(f"/deficiencies/{verified}/verification-items/").json()["items"][0]
        saved = self.post(
            "/api/v1/checks",
            {
                "operation_id": str(uuid4()),
                "vehicle_id": str(self.vehicle.pk),
                "item_id": row["item_id"],
                "checklist_version_id": row["checklist_version_id"],
                "basis_check_id": row["latest_check_id"],
                "outcome": "PASSED",
                "purpose": "VERIFICATION",
                "verification_for_deficiency_id": verified,
            },
        )
        self.assertEqual(saved.json()["result"]["verification"]["status"], "CLOSED_VERIFIED")
        verified_closed = self.detail(verified)
        self.assertContains(verified_closed, "Check suspensions — 0 in force")
        self.assertContains(verified_closed, "Check suspensions ended: 1", count=1)

        closed = self.report()
        suspend(closed, self.suction)
        close_administrator(
            deficiency_id=closed,
            expected_event_id=self.latest(closed),
            explanation="Synthetic closure.",
            **self.context(self.admin),
        )
        closed_detail = self.detail(closed)
        self.assertContains(closed_detail, "Check suspensions — 0 in force")
        self.assertContains(closed_detail, "Check suspensions ended: 1", count=1)

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

    def test_verification_page_for_a_settled_deficiency_says_it_is_not_pending(self):
        # Issue 20: not a 503 "temporarily unavailable" page for a permanent state.
        pk = self.report()
        response = self.client.get(f"/deficiencies/{pk}/verification/")
        self.assertContains(response, "not awaiting verification", status_code=409)
        self.assertContains(response, "Its status is Open", status_code=409)
        self.assertContains(response, f'href="/deficiencies/{pk}/"', status_code=409)
        self.assertNotContains(response, "temporarily unavailable", status_code=409)
        self.assertEqual(
            self.client.get(f"/deficiencies/{self.pending()}/verification/").status_code, 200
        )

    def test_a_passed_follow_up_is_not_prefilled_and_revision_succeeds(self):
        # Issue 20: the server refuses a past follow-up, so the form must not resubmit one.
        pk = self.report()
        follow_up = self.now + timedelta(days=1)
        entered = self.change(
            pk, "dependencies", explanation="Synthetic vendor", follow_up_at=follow_up.isoformat()
        )
        self.assertEqual(entered.status_code, 201, entered.content)
        prefilled = f'value="{follow_up:%Y-%m-%dT%H:%M}"'
        page = self.detail(pk)
        self.assertContains(page, prefilled)
        self.assertNotContains(page, "has passed")

        self.clock.set(self.now + timedelta(days=2))
        page = self.detail(pk)
        self.assertNotContains(page, prefilled)
        self.assertContains(page, 'name="follow_up_at" type="datetime-local" value=""')
        self.assertContains(
            page, f"The follow-up set for {follow_up:%Y-%m-%d %H:%M} UTC (+0000) has passed"
        )
        # What the form now sends: the new explanation and an empty follow-up.
        revised = self.change(
            pk, "dependency-revisions", explanation="Synthetic revised", follow_up_at=None
        )
        self.assertEqual(revised.status_code, 201, revised.content)
        stale = self.change(
            pk,
            "dependency-revisions",
            explanation="Synthetic again",
            follow_up_at=follow_up.isoformat(),
        )
        self.assertEqual(stale.json()["code"], "INVALID_FOLLOW_UP")

    def test_deadline_display_puts_station_zone_only_on_a_set_value(self):
        self.org.station_zone = "America/New_York"
        self.org.save(update_fields=["station_zone"])
        pk = self.report()
        page = self.detail(pk).content.decode()
        self.assertIn("<dt>Deadline</dt><dd>Not set</dd>", page)
        self.assertIn("<dt>Governing deadline</dt><dd>Not set</dd>", page)
        deadline = page.split("<dt>Deadline</dt><dd>")[1].split("</dd>", 1)[0]
        self.assertEqual(deadline, "Not set")
        set_deadline(
            deficiency_id=pk,
            deadline=self.now + timedelta(days=2),
            expected_event_id=self.latest(pk),
            **self.context(self.admin),
        )
        page = self.detail(pk).content.decode()
        self.assertIn("<dt>Deadline</dt><dd>2026-06-02 20:00 EDT (-0400)</dd>", page)
        self.assertNotIn("Not set", page.split("<dt>Age since")[0].split("<dt>Deadline</dt>")[1])

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
            # The script re-reads verification-items: signed out is 401, not a redirect (issue 37).
            expected = 401 if route == "verification-items" else 302
            self.assertEqual(Client().get(url).status_code, expected, route)
            with override_settings(CYA_ROLE_LOOKUP=None):
                self.assertEqual(self.client.get(url).status_code, 403)
            # Exercise the reader's tenant boundary for an already-authorized other-org actor.
            other_actor = SimpleNamespace(organization_id=str(uuid4()))
            with mock.patch(
                "cya.inspections.workflow_views._authorized", return_value=(other_actor, None)
            ):
                self.assertEqual(self.client.get(url).status_code, 404)
