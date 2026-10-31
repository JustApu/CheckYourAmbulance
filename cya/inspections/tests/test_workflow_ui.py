"""Offline HTTP/markup coverage for the issue 86 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/86) workflow shells and real command paths."""

from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError
from django.test import Client, override_settings
from django.utils import timezone

from cya.accounts.models import Organization
from cya.checklists.models import Vehicle
from cya.checks.commands import invalidate_chain
from cya.checks.models import CheckRecord
from cya.deficiencies.commands import (
    assign_deficiency,
    regrade_severity,
    set_deadline,
    start_work,
)
from cya.deficiencies.dependency import (
    add_progress_note,
    end_dependency,
    enter_dependency,
    revise_dependency,
)
from cya.deficiencies.extensions import decide_extension, request_extension
from cya.deficiencies.models import Deficiency, DeficiencyEvent, DeficiencyProjection
from cya.inspections.tests.test_crew_ui import CrewUiCase
from cya.inspections.workflows import bounded


class WorkflowUiTests(CrewUiCase):
    def history(self):
        return self.client.get(f"/inspection/vehicles/{self.vehicle.pk}/check-history/")

    def report_page(self):
        return self.client.get(f"/inspection/vehicles/{self.vehicle.pk}/deficiencies/new/")

    def correction(self, check_id, **extra):
        return self.post(
            f"/api/v1/checks/{check_id}/corrections",
            {
                "operation_id": str(uuid4()),
                "expected_tip_check_id": check_id,
                "outcome": "PASSED",
                **extra,
            },
        )

    def test_new_pages_require_sign_in_and_current_roles(self):
        urls = (
            "/deficiencies/",
            f"/deficiencies/{uuid4()}/",
            f"/inspection/vehicles/{self.vehicle.pk}/check-history/",
            f"/inspection/vehicles/{self.vehicle.pk}/deficiencies/new/",
        )
        for url in urls:
            response = Client().get(url)
            self.assertEqual((response.status_code, response["Location"]), (302, "/auth/login/"))
            with override_settings(CYA_ROLE_LOOKUP=None):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_foreign_unknown_and_malformed_resources_are_not_disclosed(self):
        other = Organization.objects.create(name="Synthetic other", station_zone="UTC")
        vehicle = Vehicle.objects.create(organization=other, display_name="Synthetic elsewhere")
        for value in (str(vehicle.pk), str(uuid4()), "not-a-uuid"):
            self.assertEqual(
                self.client.get(f"/inspection/vehicles/{value}/check-history/").status_code, 404
            )
            self.assertEqual(
                self.client.get(f"/inspection/vehicles/{value}/deficiencies/new/").status_code, 404
            )
        for value in (str(uuid4()), "not-a-uuid"):
            self.assertEqual(self.client.get(f"/deficiencies/{value}/").status_code, 404)

    def test_report_page_shows_scope_choices_severity_criteria_and_no_automatic_link(self):
        response = self.report_page()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(str(self.suction.pk), html)
        self.assertIn(str(self.cabinet.pk), html)
        self.assertIn(str(self.system.pk), html)
        for level in response.context["options"]["severities"]:
            self.assertIn(level["label"], html)
            self.assertIn(level["description"], html)
        self.assertIn("Choose the affected scope", html)
        self.assertIn('name="related_deficiency_ids" multiple', html)
        self.assertIn("not merge or close", html)

    def test_report_form_payload_commits_once_and_read_views_show_the_report(self):
        operation = str(uuid4())
        body = {
            "operation_id": operation,
            "vehicle_id": str(self.vehicle.pk),
            "target": {"item_id": str(self.suction.pk)},
            **self.new_fields(),
        }
        first = self.post("/api/v1/deficiencies", body)
        self.assertEqual(first.status_code, 201, first.content)
        replay = self.post("/api/v1/deficiencies", body)
        self.assertEqual(replay.json()["result"], first.json()["result"])
        self.assertEqual(Deficiency.objects.count(), 1)
        pk = first.json()["result"]["deficiency_id"]
        detail = self.client.get(f"/deficiencies/{pk}/")
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'aria-current="page"')
        self.assertEqual(
            next(item for item in detail.context["cya_menu"] if item["current"])["key"],
            "deficiencies",
        )
        self.assertIn("Open", detail.content.decode())
        self.assertIn(self.user.display_name, detail.content.decode())
        self.assertIn(f"/deficiencies/{pk}/", self.client.get("/deficiencies/").content.decode())

    def test_own_correction_is_distinct_from_repair_and_other_users_get_fresh_check_path(self):
        own = self.failed(actor=self.user).body["check_id"]
        other = self.record(self.siren, actor=self.crew).body["check_id"]
        page = self.history()
        controls = {str(row["record"].pk): row for row in page.context["checks"]}
        self.assertTrue(controls[own]["can_correct"])
        self.assertFalse(controls[own]["can_invalidate"])
        self.assertFalse(controls[other]["can_correct"])
        self.assertFalse(controls[other]["can_invalidate"])
        self.assertIn("not repair or verification", page.content.decode())
        self.assertIn("Record a fresh independent check", page.content.decode())
        denied = self.correction(other)
        self.assertEqual((denied.status_code, denied.json()["code"]), (403, "AUTHOR_REQUIRED"))

    def test_correction_keeps_original_and_displays_reported_in_error_history(self):
        first = self.failed(actor=self.user)
        check_id, deficiency_id = first.body["check_id"], first.body["deficiency_ids"][0]
        corrected = self.correction(check_id)
        self.assertEqual(corrected.status_code, 201, corrected.content)
        rows = self.history().context["checks"]
        self.assertEqual({r["state"] for r in rows}, {"Effective", "Superseded"})
        self.assertEqual(CheckRecord.objects.get(pk=check_id).outcome, "FAILED")
        detail = self.client.get(f"/deficiencies/{deficiency_id}/").content.decode()
        self.assertIn("Reported in Error", detail)
        self.assertIn("Reported", detail)  # the original creation event remains visible
        self.assertNotIn("REPORTED", detail)
        self.assertEqual(DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).count(), 2)

    def test_lifecycle_history_labels_keep_notice_cancellation_and_reopening_distinct(self):
        first = self.failed(actor=self.user)
        pk = first.body["deficiency_ids"][0]
        second = self.failed(self.siren, actor=self.user, deficiency={"existing_id": pk})
        self.assertEqual(self.correction(first.body["check_id"]).status_code, 201)
        self.assertEqual(self.correction(second.body["check_id"]).status_code, 201)
        invalidate_chain(
            check_id=second.body["check_id"],
            explanation="Synthetic invalidation.",
            **self.context(self.admin),
        )
        response = self.client.get(f"/deficiencies/{pk}/")
        self.assertEqual(response.status_code, 200)
        labels = [event.type_label for event in response.context["events"]]
        self.assertEqual(
            labels,
            [
                "Reported",
                "Entry corrected — supervisor review needed",
                "Reported in Error",
                "Reopened",
            ],
        )
        self.assertEqual(
            [event.status_label for event in response.context["events"]],
            ["Open", "Open", "Reported in Error", "Open"],
        )
        for label in labels:
            self.assertContains(response, label)
        for raw_type in ("REPORTED", "CORRECTION_NOTICE", "REPORTED_IN_ERROR", "REOPENED"):
            self.assertNotContains(response, raw_type)

    def test_assignment_and_work_events_have_readable_labels(self):
        pk = self.failed(actor=self.user).body["deficiency_ids"][0]

        def newest():
            return str(DeficiencyEvent.objects.filter(deficiency_id=pk).latest("sequence").pk)

        assign_deficiency(
            deficiency_id=pk,
            assignee_id=str(self.user.pk),
            expected_event_id=newest(),
            **self.context(self.admin),
        )
        set_deadline(
            deficiency_id=pk,
            deadline=timezone.now() + timedelta(days=2),
            expected_event_id=newest(),
            **self.context(self.admin),
        )
        start_work(deficiency_id=pk, expected_event_id=newest(), **self.context(self.admin))
        response = self.client.get(f"/deficiencies/{pk}/")
        self.assertEqual(
            [(e.type_label, e.status_label) for e in response.context["events"]],
            [
                ("Reported", "Open"),
                ("Assigned", "Open"),
                ("Deadline set", "Open"),
                ("Work started", "In Progress"),
            ],
        )
        for raw_type in ("ASSIGNED", "DEADLINE_SET", "WORK_STARTED"):
            self.assertNotContains(response, raw_type)
        self.assertContains(response, "Record repair")
        correction = self.post(
            f"/api/v1/deficiencies/{pk}/corrections",
            {
                "operation_id": str(uuid4()),
                "expected_event_id": newest(),
            },
        )
        self.assertEqual(correction.status_code, 201, correction.content)
        self.assertEqual(correction.json()["result"]["status"], "PENDING_VERIFICATION")
        updated = self.client.get(f"/deficiencies/{pk}/")
        self.assertContains(updated, "Repair recorded")
        self.assertNotContains(updated, "Record repair")

    def test_dependency_and_progress_note_events_have_readable_labels_and_show_their_text(self):
        pk = self.failed(actor=self.user).body["deficiency_ids"][0]

        def newest():
            return str(DeficiencyEvent.objects.filter(deficiency_id=pk).latest("sequence").pk)

        def call(command, **extra):
            return command(
                deficiency_id=pk,
                expected_event_id=newest(),
                **extra,
                **self.context(self.admin),
            )

        call(enter_dependency, explanation="Synthetic: waiting for the vendor.")
        call(revise_dependency, explanation="Synthetic: vendor changed the date.")
        call(add_progress_note, note="Synthetic: the part was ordered.")
        call(end_dependency)
        response = self.client.get(f"/deficiencies/{pk}/")
        self.assertEqual(
            [(e.type_label, e.status_label) for e in response.context["events"]][1:],
            [
                ("Awaiting external dependency", "Awaiting External Dependency"),
                ("Dependency updated", "Awaiting External Dependency"),
                ("Progress note", "Awaiting External Dependency"),
                ("Dependency ended", "Open"),
            ],
        )
        self.assertContains(response, "Synthetic: the part was ordered.")
        for raw_type in (
            "DEPENDENCY_ENTERED",
            "DEPENDENCY_REVISED",
            "DEPENDENCY_ENDED",
            "PROGRESS_NOTE",
        ):
            self.assertNotContains(response, raw_type)

    def test_extension_events_have_readable_labels_and_hide_the_raw_types(self):
        pk = self.failed(actor=self.user).body["deficiency_ids"][0]

        def newest():
            return str(DeficiencyEvent.objects.filter(deficiency_id=pk).latest("sequence").pk)

        def context():
            return {"deficiency_id": pk, "expected_event_id": newest(), **self.context(self.admin)}

        set_deadline(deadline=timezone.now() + timedelta(days=2), **context())
        asked = request_extension(
            reason="Synthetic: the part is late.",
            proposed_deadline=timezone.now() + timedelta(days=5),
            **context(),
        )
        decide_extension(
            request_event_id=asked.body["event_id"],
            decision="DENIED",
            explanation="Synthetic: not needed.",
            **context(),
        )
        response = self.client.get(f"/deficiencies/{pk}/")
        self.assertEqual(
            [e.type_label for e in response.context["events"]][-2:],
            ["Extension requested", "Extension decided"],
        )
        for raw_type in ("EXTENSION_REQUESTED", "EXTENSION_DECIDED"):
            self.assertNotContains(response, raw_type)

    def test_admin_invalidation_controls_follow_roles_and_mandatory_explanation(self):
        root = self.failed(actor=self.crew).body["check_id"]
        endpoint = f"/api/v1/checks/{root}/invalidations"
        denied = self.post(
            endpoint, {"operation_id": str(uuid4()), "explanation": "Synthetic reason."}
        )
        self.assertEqual(denied.status_code, 403)
        self.grant(self.user, "ADMINISTRATOR")
        self.assertTrue(self.history().context["checks"][0]["can_invalidate"])
        missing = self.post(endpoint, {"operation_id": str(uuid4()), "explanation": ""})
        self.assertEqual(missing.status_code, 422)
        accepted = self.post(
            endpoint, {"operation_id": str(uuid4()), "explanation": "Synthetic reason."}
        )
        self.assertEqual(accepted.status_code, 201, accepted.content)
        self.assertEqual(self.history().context["checks"][0]["state"], "Invalidated")
        self.assertEqual(DeficiencyProjection.objects.get().status, "OPEN")

    def test_unable_completion_is_distinct_from_pass_and_does_not_close_deficiency(self):
        self.clock.set(self.at(hours=5, minutes=30))
        self.grant(self.user, "SUPERVISOR")
        unable = self.failed(
            actor=self.user,
            outcome="UNABLE_TO_INSPECT",
            explanation="Synthetic access restriction.",
        )
        control = self.history().context["checks"][0]["obligations"][0]
        self.assertTrue(control["can_complete"])
        body = {
            "operation_id": str(uuid4()),
            "check_id": unable.body["check_id"],
            "explanation": "Synthetic supervisor review.",
        }
        completed = self.post(control["complete_url"], body)
        self.assertEqual(completed.status_code, 201, completed.content)
        history = self.history()
        self.assertIn("Completed by supervisor — unable to inspect", history.content.decode())
        self.assertIn("self-completed", history.content.decode())
        self.assertFalse(history.context["checks"][0]["obligations"][0]["can_complete"])
        self.assertEqual(DeficiencyProjection.objects.get().status, "OPEN")
        checklist = self.client.get(f"/api/v1/vehicles/{self.vehicle.pk}/checklist").json()
        row = next(r for r in checklist["items"] if r["item_id"] == str(self.suction.pk))
        self.assertEqual(row["obligation_outcome"], "SUPERVISOR_COMPLETED")
        self.grant(self.user, "ADMINISTRATOR")
        control = self.history().context["checks"][0]["obligations"][0]
        invalidated = self.post(
            control["invalidate_url"],
            {
                "operation_id": str(uuid4()),
                "explanation": "Synthetic determination invalidation.",
            },
        )
        self.assertEqual(invalidated.status_code, 201, invalidated.content)
        history = self.history()
        events = history.context["checks"][0]["obligations"][0]["history"]
        self.assertEqual(
            [event.determination_type for event in events], ["SUPERVISOR_COMPLETED", "INVALIDATED"]
        )
        self.assertIn("Completed by supervisor — unable to inspect", history.content.decode())
        self.assertEqual(DeficiencyProjection.objects.get().status, "OPEN")

    def test_crew_cannot_complete_an_unable_check_even_with_a_manual_post(self):
        self.clock.set(self.at(hours=5, minutes=30))
        unable = self.failed(
            actor=self.user, outcome="UNABLE_TO_INSPECT", explanation="Synthetic restriction."
        )
        control = self.history().context["checks"][0]["obligations"][0]
        self.assertFalse(control["can_complete"])
        response = self.post(
            control["complete_url"],
            {
                "operation_id": str(uuid4()),
                "check_id": unable.body["check_id"],
                "explanation": "Synthetic review.",
            },
        )
        self.assertEqual((response.status_code, response.json()["code"]), (403, "FORBIDDEN"))

    def test_links_are_explicit_reversible_and_do_not_change_status(self):
        first = self.failed(actor=self.user).body["deficiency_ids"][0]
        second = self.failed(
            self.siren, actor=self.user, create=self.new_fields(summary="Synthetic related report")
        ).body["deficiency_ids"][0]
        url = f"/api/v1/deficiencies/{first}/links"
        linked = self.post(url, {"operation_id": str(uuid4()), "related_deficiency_id": second})
        self.assertEqual(linked.status_code, 201, linked.content)
        detail = self.client.get(f"/deficiencies/{first}/")
        self.assertEqual([str(r.pk) for r in detail.context["related"]], [second])
        removed = self.post(
            f"/api/v1/deficiencies/{first}/link-removals",
            {"operation_id": str(uuid4()), "related_deficiency_id": second},
        )
        self.assertEqual(removed.status_code, 201, removed.content)
        self.assertEqual(self.client.get(f"/deficiencies/{first}/").context["related"], [])
        self.assertEqual(
            set(DeficiencyProjection.objects.values_list("status", flat=True)), {"OPEN"}
        )

    def test_a_re_grade_shows_in_the_list_and_detail_and_is_labelled_in_the_history(self):
        pk = self.failed(actor=self.user).body["deficiency_ids"][0]  # reported as High
        before = self.client.get(f"/deficiencies/{pk}/").content.decode()
        self.assertIn("High", before)
        regrade_severity(
            deficiency_id=pk,
            severity="LOW",
            explanation="Synthetic reason for the new grade.",
            expected_event_id=DeficiencyEvent.objects.filter(deficiency_id=pk)
            .latest("sequence")
            .pk,
            **self.context(self.supervisor),
        )
        detail = self.client.get(f"/deficiencies/{pk}/").content.decode()
        listing = self.client.get("/deficiencies/").content.decode()
        self.assertIn("Low", detail)
        self.assertNotIn("SEVERITY_CHANGED", detail)
        self.assertIn("Severity changed", detail)
        self.assertIn("Low", listing)
        self.assertNotIn("High", listing)

    def test_inactive_vehicle_history_remains_readable(self):
        self.record(actor=self.user)
        Vehicle.objects.filter(pk=self.vehicle.pk).update(active=False)
        self.assertEqual(self.history().status_code, 200)
        self.assertEqual(self.report_page().status_code, 404)

    def test_kit_instance_and_escaping_are_retained_in_history(self):
        self.failed(
            self.kit_item,
            actor=self.user,
            kit_id=self.reference,
            create=self.new_fields(summary="<script>Synthetic</script>"),
        )
        html = self.history().content.decode()
        self.assertIn("Reference", html)
        pk = Deficiency.objects.get().pk
        detail = self.client.get(f"/deficiencies/{pk}/").content.decode()
        self.assertIn("&lt;script&gt;Synthetic&lt;/script&gt;", detail)
        self.assertNotIn("<script>Synthetic</script>", detail)

    def test_failed_check_label_is_not_a_pass_and_attention_stays_red(self):
        self.clock.set(self.at(hours=5, minutes=30))
        self.failed(actor=self.user)
        data = self.client.get(f"/api/v1/vehicles/{self.vehicle.pk}/checklist").json()
        row = next(r for r in data["items"] if r["item_id"] == str(self.suction.pk))
        self.assertEqual(
            (row["obligation_outcome"], row["item_attention"]), ("FAILED", "ATTENTION")
        )
        self.assertIn("Inspected — failed", self.history().content.decode())

    def test_session_includes_report_choices_and_matching_links(self):
        session = self.open().json()["result"]["session_id"]
        response = self.client.get(f"/inspection/sessions/{session}/")
        html = response.content.decode()
        self.assertIn('id="outcome-dialog"', html)
        self.assertIn("Severity — choose using these criteria", html)
        self.assertEqual(
            self.config(response)["urls"]["suggestions"], "/api/v1/deficiencies/suggestions"
        )
        self.assertIn("DEFICIENCY", self.config(response)["urls"]["deficiency_detail"])

    def test_unavailable_evidence_is_503_and_report_options_fail_visibly(self):
        with mock.patch(
            "cya.inspections.workflow_views.check_history",
            side_effect=ValueError("Synthetic invalid history"),
        ):
            response = self.history()
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, "History temporarily unavailable", status_code=503)
        self.assertIn("could not be loaded completely", response.content.decode())
        self.assertNotIn("Synthetic invalid history", response.content.decode())
        session = self.open().json()["result"]["session_id"]
        with mock.patch(
            "cya.inspections.views.report_options",
            side_effect=DatabaseError("Synthetic unavailable"),
        ):
            response = self.client.get(f"/inspection/sessions/{session}/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Report choices could not be loaded", response.content.decode())
        self.failed(actor=self.user)
        with self.assertRaises(ValueError):
            bounded(DeficiencyProjection.objects.all(), limit=0)

    def test_report_configuration_failure_has_a_reporting_title_and_safe_503(self):
        for error in (ValueError, DatabaseError):
            with (
                self.subTest(error=error.__name__),
                mock.patch(
                    "cya.inspections.workflow_views.report_options",
                    side_effect=error("Synthetic unavailable configuration"),
                ),
                self.assertLogs("cya.inspections.workflow_views", level="ERROR") as logs,
            ):
                response = self.report_page()
            self.assertContains(response, "Reporting temporarily unavailable", status_code=503)
            self.assertContains(
                response, "This report form could not be loaded completely", status_code=503
            )
            self.assertNotContains(response, "History temporarily unavailable", status_code=503)
            self.assertNotContains(response, "Synthetic unavailable configuration", status_code=503)
            self.assertNotIn("Synthetic unavailable configuration", " ".join(logs.output))
