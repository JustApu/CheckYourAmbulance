"""Paper-entry batch headers (SPEC 8.0.0 LATE-2 to LATE-4, §3.3, §18.4; issue #9 Part B1)."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

from django.db import DatabaseError, transaction
from django.test import SimpleTestCase

from cya.authorization.commands import change_role
from cya.checklists import commands as checklist_commands
from cya.checklists.tests.support import ids
from cya.core.audit_commands import CommandDenied
from cya.core.models import CommandReceipt
from cya.kits.definitions import (
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
)
from cya.paper.commands import record_paper_entry_batch, resolve_performance
from cya.paper.models import PaperEntryBatch, PaperEntryKitVersion, PaperEntryPerformer
from cya.paper.tests.support import ZONE, PaperCase


class ResolutionTests(SimpleTestCase):
    """LATE-2 under TIME-3: the station zone's rules, with the conversion explained."""

    def test_an_ordinary_summer_time(self):
        instant, offset, how = resolve_performance(date(2026, 5, 31), time(8, 30), ZONE)
        self.assertEqual(instant, datetime(2026, 5, 31, 12, 30, tzinfo=UTC))
        self.assertEqual((offset, how), (-240, "EXACT"))

    def test_a_time_in_the_spring_forward_gap_moves_to_the_first_valid_instant(self):
        instant, offset, how = resolve_performance(date(2026, 3, 8), time(2, 30), ZONE)
        self.assertEqual(instant, datetime(2026, 3, 8, 7, 0, tzinfo=UTC))
        self.assertEqual((offset, how), (-240, "GAP_FORWARD"))

    def test_a_repeated_fall_back_time_takes_its_first_occurrence(self):
        instant, offset, how = resolve_performance(date(2026, 11, 1), time(1, 30), ZONE)
        self.assertEqual(instant, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
        self.assertEqual((offset, how), (-240, "AMBIGUOUS_FIRST"))

    def test_winter_time(self):
        instant, offset, how = resolve_performance(date(2026, 1, 15), time(23, 45), ZONE)
        self.assertEqual(instant, datetime(2026, 1, 16, 4, 45, tzinfo=UTC))
        self.assertEqual((offset, how), (-300, "EXACT"))


class RecordTests(PaperCase):
    def test_a_crew_member_enters_their_own_batch_header(self):
        result = self.enter()
        self.assertEqual(result.status, 201)
        batch = PaperEntryBatch.objects.get(pk=result.body["batch_id"])
        receipt = CommandReceipt.objects.get(pk=batch.command_id)
        # The paper time and the server entry time are kept apart (LATE-2, TIME-4).
        self.assertEqual(batch.performed_at, datetime(2026, 6, 1, 11, 30, tzinfo=UTC))
        self.assertEqual(batch.recorded_at, receipt.recorded_at)
        self.assertGreater(batch.recorded_at, batch.performed_at)
        self.assertEqual(
            (batch.performed_local_date, batch.performed_local_time, batch.performed_zone),
            (date(2026, 6, 1), time(7, 30), ZONE),
        )
        self.assertEqual(
            (batch.performed_utc_offset_minutes, batch.performed_resolution), (-240, "EXACT")
        )
        self.assertEqual(batch.actor_user_id, self.crew.pk)
        self.assertEqual(receipt.command_type, "PAPER_ENTRY_BATCH_RECORD")
        performers = PaperEntryPerformer.objects.filter(batch=batch)
        self.assertEqual([p.performer_id for p in performers], [self.crew.pk])
        self.assertEqual(
            result.body["performed_local"], "2026-06-01T07:30"
        )  # explains the conversion
        self.assertEqual(result.body["performed_at"], "2026-06-01T11:30:00+00:00")

    def test_a_replay_returns_the_stored_header_without_a_second_row(self):
        operation = self.context(self.crew)
        first = record_paper_entry_batch(
            vehicle_id=str(self.vehicle.pk),
            checklist_version_id=self.version,
            performed_on=date(2026, 6, 1),
            performed_time=time(7, 30),
            performer_ids=[str(self.crew.pk)],
            **operation,
        )
        again = record_paper_entry_batch(
            vehicle_id=str(self.vehicle.pk),
            checklist_version_id=self.version,
            performed_on=date(2026, 6, 1),
            performed_time=time(7, 30),
            performer_ids=[str(self.crew.pk)],
            **{**operation, "correlation_id": str(uuid4()), "request_attempt_id": str(uuid4())},
        )
        self.assertTrue(again.replayed)
        self.assertEqual(again.body["batch_id"], first.body["batch_id"])
        self.assertEqual(PaperEntryBatch.objects.count(), 1)
        changed = record_paper_entry_batch(
            vehicle_id=str(self.vehicle.pk),
            checklist_version_id=self.version,
            performed_on=date(2026, 6, 1),
            performed_time=time(7, 45),
            performer_ids=[str(self.crew.pk)],
            **{**operation, "correlation_id": str(uuid4()), "request_attempt_id": str(uuid4())},
        )
        self.assertTrue(changed.operation_reused)
        self.assertEqual(PaperEntryBatch.objects.count(), 1)

    def test_no_age_limit_applies(self):
        result = self.enter()
        self.clock.set(self.at(days=400))
        later = self.enter(performed_time=time(7, 31))
        self.assertEqual(result.status, later.status)

    def test_a_performance_time_after_now_is_refused(self):
        self.refused("PERFORMED_IN_FUTURE", performed_time=time(8, 1))

    def test_a_time_in_the_current_minute_but_after_now_is_refused(self):
        self.clock.set(datetime(2026, 6, 1, 12, 0, 30, tzinfo=UTC))  # 08:00:30 local
        self.assertEqual(self.enter(performed_time=time(8)).status, 201)
        self.refused("PERFORMED_IN_FUTURE", performed_time=time(8, 1))

    def test_malformed_values_are_refused_before_any_transaction(self):
        for overrides, path in (
            ({"performer_ids": []}, "performer_ids"),
            ({"performer_ids": ["not-a-uuid"]}, "performer_ids"),
            ({"performer_ids": [str(self.crew.pk)] * 2}, "performer_ids"),
            ({"performed_time": time(19, 30, 15)}, "performed_time"),
            ({"performed_on": "2026-06-01"}, "performed_on"),
            ({"kit_definition_version_ids": [self.kit_version] * 2}, "kit_definition_version_ids"),
        ):
            with self.subTest(path=path, overrides=overrides):
                error = self.refused("MALFORMED_REQUEST", **overrides)
                self.assertEqual(error.path, path)


class PermissionTests(PaperCase):
    def test_crew_cannot_attribute_paper_work_to_another_user(self):
        with self.assertRaises(CommandDenied):
            self.enter(actor=self.crew, performers=[self.supervisor])
        with self.assertRaises(CommandDenied):
            self.enter(actor=self.crew, performers=[self.crew, self.supervisor])
        self.assertFalse(PaperEntryBatch.objects.exists())

    def test_a_supply_officer_without_another_entering_role_cannot_enter(self):
        officer_only = self.make_user("officer-only")
        self.grant(officer_only, "SUPPLY_OFFICER")
        self.revoke_crew(officer_only)
        with self.assertRaises(CommandDenied):
            self.enter(actor=officer_only)
        self.assertFalse(PaperEntryBatch.objects.exists())

    def test_a_supervisor_may_name_themself_or_any_users_including_inactive_ones(self):
        supervisor_only = self.make_user("supervisor-only")
        self.grant(supervisor_only, "SUPERVISOR")
        self.revoke_crew(supervisor_only)
        self.assertEqual(self.enter(actor=supervisor_only).status, 201)
        former = self.make_user("former")
        former.active = False
        former.save(update_fields=["active"])
        result = self.enter(actor=self.supervisor, performers=[self.crew, former])
        stored = set(
            PaperEntryPerformer.objects.filter(batch_id=result.body["batch_id"]).values_list(
                "performer_id", flat=True
            )
        )
        self.assertEqual(stored, {self.crew.pk, former.pk})

    def test_an_administrator_may_attribute_to_others(self):
        self.assertEqual(self.enter(actor=self.admin, performers=[self.crew]).status, 201)

    def test_an_unknown_performer_is_not_found(self):
        error = self.refused(
            "NOT_FOUND", actor=self.supervisor, performer_ids=[str(self.crew.pk), str(uuid4())]
        )
        self.assertEqual(error.path, "performer_ids")

    def revoke_crew(self, user):
        change_role(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            target_id=str(user.pk),
            role="CREW",
            action="REVOKE",
            **ids(),
        )


class PrintedVersionTests(PaperCase):
    def test_a_draft_or_unknown_version_is_not_found(self):
        draft = checklist_commands.save_template_draft(
            template_id=str(self.checklist.pk), items=self.own_items(), **self.context()
        ).body["version_id"]
        self.refused("NOT_FOUND", checklist_version_id=draft)
        self.refused("NOT_FOUND", checklist_version_id=str(uuid4()))

    def test_another_vehicles_checklist_version_is_refused(self):
        other = self.vehicle_checklist(vehicle=self.vehicles[1], label="Synthetic B checklist")
        version_b = self.publish_own(checklist=other, vehicle=self.vehicles[1])
        self.clock.set(self.at(days=1))
        self.refused(
            "VERSION_NOT_FOR_VEHICLE",
            checklist_version_id=version_b,
            performed_on=date(2026, 6, 1),
            performed_time=time(19),
        )

    def test_a_version_not_yet_in_force_at_the_performance_time_is_refused(self):
        self.clock.set(self.at(days=1))
        later = self.publish_own(effective_at=self.at(days=1, hours=12))
        self.clock.set(self.at(days=2))
        self.refused(
            "VERSION_NOT_IN_FORCE",
            checklist_version_id=later,
            performed_on=date(2026, 6, 1),
            performed_time=time(8),
        )

    def test_a_since_replaced_printed_version_still_governs(self):
        self.clock.set(self.at(days=1))
        self.publish_own(placements=True)
        self.clock.set(self.at(days=2))
        result = self.enter(performed_on=date(2026, 6, 2), performed_time=time(9))
        self.assertEqual(result.body["checklist_version_id"], self.version)

    def test_an_inactive_vehicle_is_refused(self):
        self.vehicle.active = False
        self.vehicle.save(update_fields=["active"])
        self.refused("VEHICLE_INACTIVE")


class KitVersionTests(PaperCase):
    def test_a_placed_kit_definition_version_in_force_is_kept(self):
        result = self.enter(kit_definition_version_ids=[self.kit_version])
        rows = PaperEntryKitVersion.objects.filter(batch_id=result.body["batch_id"])
        self.assertEqual([str(r.kit_definition_version_id) for r in rows], [self.kit_version])

    def test_a_kit_definition_the_printed_version_does_not_place_is_refused(self):
        other = create_kit_definition(
            key="other-kit", label="Synthetic other kit", **self.context(self.supervisor)
        ).body["definition_id"]
        version = save_kit_definition_draft(
            definition_id=other,
            contents=[
                {
                    "item_id": str(self.kit_item.pk),
                    "display_order": 1,
                    "kind": "PRESENCE_CONDITION",
                    "allowed_outcomes": ["PASSED", "FAILED"],
                }
            ],
            **self.context(self.supervisor),
        ).body["version_id"]
        publish_kit_definition_version(version_id=version, **self.context(self.supervisor))
        self.clock.set(self.at(days=2))
        self.refused(
            "KIT_VERSION_NOT_IN_CHECKLIST",
            kit_definition_version_ids=[version],
            performed_on=date(2026, 6, 2),
            performed_time=time(8),
        )

    def test_an_unpublished_kit_version_is_not_found(self):
        self.refused("NOT_FOUND", kit_definition_version_ids=[self.kit_draft()])

    def test_two_versions_of_one_kit_definition_are_ambiguous(self):
        second = self.kit_draft()
        publish_kit_definition_version(version_id=second, **self.context(self.supervisor))
        self.clock.set(self.at(days=2))
        self.refused(
            "KIT_VERSION_AMBIGUOUS",
            kit_definition_version_ids=[self.kit_version, second],
            performed_on=date(2026, 6, 2),
            performed_time=time(8),
        )

    def test_a_kit_version_not_yet_in_force_at_the_performance_time_is_refused(self):
        self.clock.set(self.at(days=1))
        later = self.kit_draft()
        publish_kit_definition_version(version_id=later, **self.context(self.supervisor))
        self.clock.set(self.at(days=2))
        self.refused(
            "KIT_VERSION_NOT_IN_FORCE",
            kit_definition_version_ids=[later],
            performed_on=date(2026, 6, 1),
            performed_time=time(8),
        )


class GuardTests(PaperCase):
    """The database repeats the rules for a writer that bypasses the command (§18.4)."""

    def setUp(self):
        super().setUp()
        self.batch = PaperEntryBatch.objects.get(pk=self.enter().body["batch_id"])

    def evidence(self, **overrides):
        values = {
            "organization_id": self.org.pk,
            "command_id": self.batch.command_id,
            "recorded_at": self.batch.recorded_at,
            "actor_user_id": self.batch.actor_user_id,
            "actor_roles_at_time": self.batch.actor_roles_at_time,
        }
        values.update(overrides)
        return values

    def assert_refused(self, write):
        with self.assertRaises(DatabaseError), transaction.atomic():
            write()

    def test_rows_are_append_only(self):
        self.assert_refused(
            lambda: PaperEntryBatch.objects.filter(pk=self.batch.pk).update(
                performed_at=self.batch.performed_at - timedelta(hours=1)
            )
        )
        self.assert_refused(lambda: PaperEntryBatch.objects.filter(pk=self.batch.pk).delete())
        self.assert_refused(
            lambda: PaperEntryPerformer.objects.filter(batch=self.batch).update(
                performer_id=self.supervisor.pk
            )
        )

    def test_a_crew_actor_cannot_be_given_another_performer(self):
        self.assert_refused(
            lambda: PaperEntryPerformer.objects.create(
                batch=self.batch, performer_id=self.supervisor.pk, **self.evidence()
            )
        )

    def test_a_child_row_of_another_command_is_refused(self):
        other = PaperEntryBatch.objects.get(
            pk=self.enter(actor=self.supervisor, performers=[self.crew]).body["batch_id"]
        )
        self.assert_refused(
            lambda: PaperEntryPerformer.objects.create(
                batch=self.batch,
                performer_id=self.crew.pk,
                **self.evidence(
                    command_id=other.command_id,
                    recorded_at=other.recorded_at,
                    actor_user_id=other.actor_user_id,
                    actor_roles_at_time=other.actor_roles_at_time,
                ),
            )
        )

    def test_a_kit_version_the_checklist_does_not_place_is_refused(self):
        unplaced = create_kit_definition(
            key="loose-kit", label="Synthetic loose kit", **self.context(self.supervisor)
        ).body["definition_id"]
        version = save_kit_definition_draft(
            definition_id=unplaced,
            contents=[
                {
                    "item_id": str(self.kit_item.pk),
                    "display_order": 1,
                    "kind": "PRESENCE_CONDITION",
                    "allowed_outcomes": ["PASSED", "FAILED"],
                }
            ],
            **self.context(self.supervisor),
        ).body["version_id"]
        publish_kit_definition_version(version_id=version, **self.context(self.supervisor))
        self.assert_refused(
            lambda: PaperEntryKitVersion.objects.create(
                batch=self.batch, kit_definition_version_id=version, **self.evidence()
            )
        )

    def test_a_batch_without_its_own_receipt_is_refused(self):
        other_receipt = CommandReceipt.objects.exclude(pk=self.batch.command_id).latest(
            "recorded_at"
        )
        self.assert_refused(
            lambda: PaperEntryBatch.objects.create(
                vehicle_id=self.vehicle.pk,
                checklist_version_id=self.version,
                performed_at=self.batch.performed_at,
                performed_local_date=self.batch.performed_local_date,
                performed_local_time=self.batch.performed_local_time,
                performed_zone=ZONE,
                performed_utc_offset_minutes=-240,
                performed_resolution="EXACT",
                **self.evidence(
                    command_id=other_receipt.pk,
                    recorded_at=other_receipt.recorded_at,
                    actor_user_id=other_receipt.actor_user_id,
                ),
            )
        )
