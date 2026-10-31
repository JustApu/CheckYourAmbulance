"""POST /api/v1/deficiencies command: independent reports, scope, assignment, links (issue 80 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/80))."""

from datetime import timedelta
from uuid import uuid4

from django.utils import timezone

from cya.checklists.models import Compartment, VehicleSystem
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent
from cya.deficiencies.commands import report_deficiency
from cya.deficiencies.models import (
    Deficiency,
    DeficiencyAffectedItem,
    DeficiencyEvent,
    DeficiencyLink,
    DeficiencyProjection,
)
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.tests.support import DeficiencyCase


class ReportTests(DeficiencyCase):
    def report(self, target=None, *, actor=None, vehicle=None, category=None, **extra):
        context = self.context(actor or self.crew) | extra.pop("context", {})
        arguments = {
            "vehicle_id": str((vehicle or self.vehicle).pk),
            "target": target if target is not None else {"item_id": str(self.suction.pk)},
            **self.new_fields(category),
            **extra,
        }
        return report_deficiency(**arguments, **context)

    def rejected(self, code, status=422, **kwargs):
        before = self.counts()
        with self.assertRaises(DeficiencyError) as caught:
            self.report(**kwargs)
        self.assertEqual(
            (caught.exception.code, caught.exception.status), (code, status), str(caught.exception)
        )
        self.assertEqual(self.counts(), before)
        return caught.exception

    def denied(self, **kwargs):
        before = self.counts()
        with self.assertRaises(CommandDenied):
            self.report(**kwargs)
        self.assertEqual(self.counts(), before)

    def test_an_item_report_stores_the_header_scope_event_and_projection(self):
        result = self.report()
        self.assertEqual(result.status, 201)
        deficiency = Deficiency.objects.get()
        event = DeficiencyEvent.objects.get()
        self.assertEqual(result.body["deficiency_id"], deficiency.pk)
        self.assertEqual(result.body["creation_event_id"], event.pk)
        self.assertEqual(result.body["related_link_event_ids"], [])
        self.assertEqual(
            (
                deficiency.item_id,
                deficiency.compartment_id,
                deficiency.kit_id,
                deficiency.vehicle_id,
            ),
            (self.suction.pk, None, None, self.vehicle.pk),
        )
        self.assertEqual(deficiency.reporter_id, self.crew.pk)
        self.assertEqual(deficiency.actor_roles_at_time, ["CREW"])
        self.assertEqual(
            (event.event_type, event.to_status, event.sequence), ("REPORTED", "OPEN", 1)
        )
        self.assertEqual(
            list(DeficiencyAffectedItem.objects.values_list("item_id", flat=True)),
            [self.suction.pk],
        )
        self.assertEqual(DeficiencyProjection.objects.get().status, "OPEN")
        audit = AuditEvent.objects.get(action="DEFICIENCY_REPORT")
        self.assertEqual(audit.outcome, "SUCCEEDED")
        self.assertNotIn("Synthetic summary", str(audit.new_snapshot))

    def test_a_compartment_report_snapshots_every_item_in_it(self):
        row = Compartment.objects.get(
            vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )
        self.report({"compartment_id": str(row.pk)})
        deficiency = Deficiency.objects.get()
        self.assertEqual((deficiency.compartment_id, deficiency.item_id), (row.pk, None))
        self.assertEqual(
            sorted(DeficiencyAffectedItem.objects.values_list("item_id", flat=True)),
            sorted(
                [self.suction.pk, self.masks.pk, self.gauge.pk, self.special.pk, self.limited.pk]
            ),
        )

    def test_a_compartment_of_another_vehicle_is_not_found(self):
        other = Compartment.objects.get(
            vehicle=self.vehicles[1], stable_slot_id=self.cabinet.stable_slot_id
        )
        self.rejected("NOT_FOUND", 404, target={"compartment_id": str(other.pk)})

    def test_a_vehicle_systems_report_snapshots_the_system_items_and_names_no_target_row(self):
        row = VehicleSystem.objects.get(vehicle=self.vehicle)
        self.report({"vehicle": True, "system_ids": [str(row.pk)]})
        deficiency = Deficiency.objects.get()
        self.assertEqual(
            (deficiency.item_id, deficiency.compartment_id, deficiency.kit_id), (None,) * 3
        )
        self.assertEqual(
            sorted(DeficiencyAffectedItem.objects.values_list("item_id", flat=True)),
            sorted([self.siren.pk, self.odometer.pk]),
        )

    def test_targets_must_be_exactly_one_known_form(self):
        item = str(self.suction.pk)
        for label, target in {
            "none": None,
            "string": "item",
            "empty": {},
            "two forms": {"item_id": item, "compartment_id": item},
            "vehicle alone": {"vehicle": True},
            "vehicle false": {"vehicle": False, "system_ids": [item]},
            "no systems": {"vehicle": True, "system_ids": []},
            "systems not a list": {"vehicle": True, "system_ids": item},
            "repeated systems": {"vehicle": True, "system_ids": [item, item]},
            "too many systems": {"vehicle": True, "system_ids": [str(uuid4()) for _ in range(51)]},
            "bad id": {"item_id": "nope"},
            "unknown key": {"item_id": item, "extra": 1},
        }.items():
            with self.subTest(label):
                before = self.counts()
                with self.assertRaises(DeficiencyError) as caught:
                    report_deficiency(
                        vehicle_id=str(self.vehicle.pk),
                        target=target,
                        **self.new_fields(),
                        **self.context(self.crew),
                    )
                self.assertEqual(
                    (caught.exception.code, caught.exception.status), ("MALFORMED_REQUEST", 400)
                )
                self.assertEqual(self.counts(), before)

    def test_unknown_kit_target_is_not_found(self):
        self.rejected("NOT_FOUND", 404, target={"kit_id": str(uuid4())})

    def test_a_target_outside_the_vehicles_checklist_is_an_empty_scope(self):
        self.rejected("EMPTY_SCOPE", target={"item_id": str(self.stray.pk)})
        self.rejected("EMPTY_SCOPE", target={"item_id": str(uuid4())})

    def test_a_checklist_slot_the_vehicle_lacks_fails_the_report_instead_of_shrinking_scope(self):
        from cya.checklists.models import Vehicle

        bare = Vehicle.objects.create(organization=self.org, display_name="Synthetic bare")
        before = self.counts()
        with self.assertRaises(DeficiencyError) as caught:
            self.report(vehicle=bare)
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("CHECKLIST_UNAVAILABLE", 409)
        )
        self.assertEqual(self.counts(), before)

    def test_an_unknown_system_is_not_found(self):
        self.rejected("NOT_FOUND", 404, target={"vehicle": True, "system_ids": [str(uuid4())]})

    def test_unknown_or_inactive_vehicles_and_session_ids_are_refused(self):
        before = self.counts()
        with self.assertRaises(DeficiencyError) as caught:
            self.report(vehicle=type("V", (), {"pk": uuid4()})())
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.vehicle.active = False
        self.vehicle.save()
        self.rejected("VEHICLE_INACTIVE")
        self.vehicle.active = True
        self.vehicle.save()
        self.rejected("NOT_YET_SUPPORTED", session_id=str(uuid4()))
        self.assertEqual(self.counts(), before)

    def test_text_severity_and_category_are_validated(self):
        self.rejected("INVALID_SEVERITY", severity="SEVERE")
        self.rejected("UNKNOWN_CATEGORY", category_id=str(uuid4()))
        for field, value in {
            "summary": "   ",
            "description": "",
            "category_id": "nope",
        }.items():
            with self.subTest(field):
                self.rejected("MALFORMED_REQUEST", 400, **{field: value})

    def test_a_crew_report_cannot_assign_or_set_a_deadline(self):
        deadline = timezone.now() + timedelta(days=3)
        self.denied(assignee_id=str(self.supervisor.pk))
        self.denied(deadline=deadline)
        self.report()  # the unassigned report stays valid, visible and unassigned
        self.assertIsNone(DeficiencyProjection.objects.get().assignee_id)

    def test_a_supervisor_and_an_administrator_may_assign_and_set_a_deadline(self):
        deadline = timezone.now() + timedelta(days=3)
        for actor in (self.supervisor, self.admin):
            with self.subTest(actor=actor.display_name):
                self.report(actor=actor, assignee_id=str(self.crew.pk), deadline=deadline)
        for projection in DeficiencyProjection.objects.all():
            self.assertEqual(projection.assignee_id, self.crew.pk)
            self.assertEqual(projection.deadline, deadline)
        event = DeficiencyEvent.objects.first()
        self.assertEqual((event.assignee_id, event.deadline), (self.crew.pk, deadline))

    def test_a_supply_officer_assigns_only_supply_related_categories(self):
        self.denied(actor=self.supply, assignee_id=str(self.crew.pk))
        self.report(actor=self.supply, category=self.stock, assignee_id=str(self.crew.pk))
        self.assertEqual(DeficiencyProjection.objects.get().assignee_id, self.crew.pk)
        # Without an assignment or deadline a supply officer reports like anyone else.
        self.report(actor=self.supply)
        self.assertEqual(Deficiency.objects.count(), 2)

    def test_the_denied_assignment_is_audited_as_the_actors_denied_action(self):
        self.denied(assignee_id=str(self.supervisor.pk))
        audit = AuditEvent.objects.get(action="DEFICIENCY_REPORT")
        self.assertEqual((audit.outcome, audit.actor_user_id), ("DENIED", self.crew.pk))
        self.assertEqual(audit.roles_at_time, ["CREW"])

    def test_the_assignee_must_be_an_active_user_of_this_organization(self):
        self.rejected("INVALID_ASSIGNEE", actor=self.supervisor, assignee_id=str(uuid4()))
        self.crew.active = False
        self.crew.save()
        self.rejected("INVALID_ASSIGNEE", actor=self.supervisor, assignee_id=str(self.crew.pk))

    def test_a_deadline_must_be_a_future_timezone_aware_instant(self):
        self.rejected(
            "INVALID_DEADLINE", actor=self.supervisor, deadline=timezone.now() - timedelta(hours=1)
        )
        self.rejected("MALFORMED_REQUEST", 400, actor=self.supervisor, deadline="tomorrow")

    def test_related_deficiencies_are_linked_only_as_the_crew_member_chose(self):
        first = self.report().body["deficiency_id"]
        second = self.report(vehicle=self.vehicles[1]).body["deficiency_id"]
        # The new deficiency is not linked to any open match the server might have suggested.
        self.assertEqual(DeficiencyLink.objects.count(), 0)
        result = self.report(related_deficiency_ids=[second, first])
        new = result.body["deficiency_id"]
        links = DeficiencyLink.objects.order_by("lower_id", "upper_id")
        self.assertEqual(
            sorted(result.body["related_link_event_ids"]),
            sorted(links.values_list("pk", flat=True)),
        )
        for link in links:
            self.assertEqual((link.link_type, link.prior_link_event_id), ("ADD", None))
            self.assertLess(link.lower_id, link.upper_id)
            self.assertIn(new, (link.lower_id, link.upper_id))
        # Linking neither merges responsibility nor changes any other deficiency.
        self.assertEqual(
            set(DeficiencyProjection.objects.values_list("status", flat=True)), {"OPEN"}
        )
        self.assertEqual(DeficiencyEvent.objects.count(), 3)

    def test_related_ids_must_exist_in_this_organization_and_not_repeat(self):
        self.rejected("NOT_FOUND", 404, related_deficiency_ids=[str(uuid4())])
        known = self.report().body["deficiency_id"]
        self.rejected("MALFORMED_REQUEST", 400, related_deficiency_ids=[known, known])
        self.rejected("MALFORMED_REQUEST", 400, related_deficiency_ids="x")
        self.rejected(
            "MALFORMED_REQUEST", 400, related_deficiency_ids=[str(uuid4()) for _ in range(51)]
        )

    def test_a_replay_returns_the_stored_result_and_creates_nothing_more(self):
        operation_id = str(uuid4())
        first = self.report(context={"operation_id": operation_id})
        counts = self.counts()
        again = self.report(context={"operation_id": operation_id})
        self.assertEqual((again.body, again.replayed), (first.body, True))
        self.assertEqual(self.counts(), counts)
        different = self.report(severity="LOW", context={"operation_id": operation_id})
        self.assertTrue(different.operation_reused)
        self.assertEqual(self.counts(), counts)

    def test_a_failure_after_the_header_rolls_everything_back(self):
        from unittest import mock

        known = self.report().body["deficiency_id"]
        before = self.counts()
        with (
            mock.patch("cya.deficiencies.service.insert_link", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            self.report(related_deficiency_ids=[known])
        self.assertEqual(self.counts(), before)

    def test_an_inactive_user_is_denied_and_the_denial_is_audited(self):
        self.crew.active = False
        self.crew.save()
        self.denied()
        audit = AuditEvent.objects.get(action="DEFICIENCY_REPORT")
        self.assertEqual(audit.outcome, "DENIED")

    def test_related_links_are_audited_in_the_reports_snapshot_only_as_ids(self):
        known = self.report().body["deficiency_id"]
        self.report(related_deficiency_ids=[known])
        audit = AuditEvent.objects.filter(action="DEFICIENCY_REPORT", outcome="SUCCEEDED").latest(
            "recorded_at"
        )
        self.assertEqual(audit.new_snapshot["values"]["related_deficiency_ids"], [known])

    def test_every_role_may_report(self):
        for actor in (self.crew, self.supervisor, self.supply, self.admin):
            self.report(actor=actor)
        self.assertEqual(Deficiency.objects.count(), 4)
