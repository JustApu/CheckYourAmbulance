"""Schema guards, attacked directly: rows a buggy or hostile writer might try to insert.

Each case builds its rows by hand (bypassing generation) inside its own transaction and expects
MariaDB itself to refuse them. The positive path is covered by the generation and reconciliation
tests; the point here is that the invariants do not depend on the application behaving.
"""

import json
from contextlib import contextmanager
from datetime import timedelta

from django.db import DatabaseError, IntegrityError, connection, transaction

from cya.accounts.fields import new_id
from cya.accounts.models import Organization
from cya.checklists.models import Vehicle
from cya.conditions.models import ObligationDetermination
from cya.core.audit_repository import append_receipt
from cya.obligations.generation import reconcile_unit
from cya.obligations.models import (
    Obligation,
    ObligationSelection,
    OdometerObligation,
    RequirementBinding,
    RequirementVersion,
)
from cya.obligations.planning import RequirementRevived
from cya.obligations.tests.support import ObligationCase
from tools.command_identity import worker_operation_id

WORKER = "worker.probe"


class GuardCase(ObligationCase):
    def setUp(self):
        super().setUp()
        self.v1 = self.published_version()
        self.publish_schedule("test-0600")
        self.bind(reconcile=False)
        self.generate()
        self.due = self.at(hours=6)
        self.first = ObligationSelection.objects.get(item=self.suction, due_at=self.due, sequence=1)
        self.obligation = self.first.selected_obligation
        self.version = self.obligation.requirement_version
        self.counter = 0

    # -- helpers -------------------------------------------------------------------------

    def receipt(self):
        """A committed-in-this-transaction worker receipt to hang hand-made rows on."""
        self.counter += 1
        job_key = json.dumps(["probe", str(self.counter), new_id()], separators=(",", ":"))
        return append_receipt(
            organization=self.org,
            operation_id=str(
                worker_operation_id(str(self.org.pk), WORKER, "OBLIGATIONS_RECONCILE", job_key)
            ),
            command_type="OBLIGATIONS_RECONCILE",
            request_digest="0" * 64,
            response_status=200,
            response_body={},
            correlation_id=new_id(),
            service_identity=WORKER,
            job_key=job_key,
        )

    def custom_receipt(self, command_type, is_worker=True):
        self.counter += 1
        job_key = json.dumps(["probe", str(self.counter), new_id()], separators=(",", ":"))
        with transaction.atomic():
            if is_worker:
                return append_receipt(
                    organization=self.org,
                    operation_id=str(
                        worker_operation_id(str(self.org.pk), WORKER, command_type, job_key)
                    ),
                    command_type=command_type,
                    request_digest="0" * 64,
                    response_status=200,
                    response_body={},
                    correlation_id=new_id(),
                    service_identity=WORKER,
                    job_key=job_key,
                )
            return append_receipt(
                organization=self.org,
                actor_user=self.admin,
                operation_id=new_id(),
                command_type=command_type,
                request_digest="0" * 64,
                response_status=200,
                response_body={},
                correlation_id=new_id(),
            )

    @staticmethod
    def evidence(receipt, **overrides):
        base = {
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": None,
            "actor_roles_at_time": [],
            "service_identity": receipt.service_identity,
            "job_key": receipt.job_key,
        }
        base.update(overrides)
        return base

    def new_version(self, receipt, **overrides):
        fields = {
            name: getattr(self.version, name)
            for name in (
                "organization_id",
                "vehicle_id",
                "item_id",
                "item_def_id",
                "publication_id",
                "override_version_id",
                "binding_id",
                "schedule_rule_id",
                "conditional_rule_id",
            )
        }
        fields.update(overrides)
        return RequirementVersion.objects.create(id=new_id(), **fields, **self.evidence(receipt))

    def new_obligation(self, receipt, version=None, **overrides):
        fields = {
            "organization_id": self.org.pk,
            "vehicle_id": self.obligation.vehicle_id,
            "item_id": self.obligation.item_id,
            "requirement_version": version or self.new_version(receipt),
            "due_at": self.due,
            "early_at": self.due - timedelta(hours=1),
            "grace_at": self.due + timedelta(hours=1),
            "routine": True,
            "basis_at": receipt.recorded_at,
        }
        fields.update(overrides)
        return Obligation.objects.create(id=new_id(), **fields, **self.evidence(receipt))

    def superseded(self, receipt, obligation=None, **overrides):
        fields = {
            "organization_id": self.org.pk,
            "obligation_id": (obligation or self.obligation).pk,
            "vehicle_id": str(self.obligation.vehicle_id),
            "due_at": self.due,
            "determination_type": "SUPERSEDED",
            "evaluated_inputs": {},
        }
        fields.update(overrides)
        return ObligationDetermination.objects.create(
            id=new_id(), **fields, **self.evidence(receipt)
        )

    def event(self, receipt, **fields):
        base = {
            "organization_id": self.org.pk,
            "vehicle_id": self.obligation.vehicle_id,
            "item_id": self.obligation.item_id,
            "due_at": self.due,
        }
        base.update(fields)
        return ObligationSelection.objects.create(id=new_id(), **base, **self.evidence(receipt))

    def replace(self, receipt, *, previous=None, sequence=2, determination="auto", obligation=None):
        previous = previous or self.first
        if determination == "auto":
            determination = self.superseded(receipt, previous.selected_obligation)
        return self.event(
            receipt,
            sequence=sequence,
            selection_type="REPLACE",
            selected_obligation=obligation or self.new_obligation(receipt),
            previous_selection=previous,
            supersession_determination=determination,
        )

    def raw_odometer(self, receipt, service_identity="keep", job_key="keep", **overrides):
        existing = OdometerObligation.objects.first()
        fields = {
            "id": new_id(),
            "organization_id": self.org.pk,
            "vehicle_id": existing.vehicle_id,
            "schedule_rule_id": existing.schedule_rule_id,
            "scheduled_inspection_key": existing.scheduled_inspection_key,
            "due_at": existing.due_at,
            "early_at": existing.early_at,
            "grace_at": existing.grace_at,
        }
        fields.update(overrides)
        evidence = self.evidence(receipt)
        if service_identity != "keep":
            evidence["service_identity"] = service_identity
        if job_key != "keep":
            evidence["job_key"] = job_key
        return OdometerObligation.objects.create(**fields, **evidence)

    @contextmanager
    def refused(self, message, error=DatabaseError):
        """The database refuses the statements, for the stated reason (not just any failure)."""
        with self.assertRaisesRegex(error, message), transaction.atomic():
            yield

    def commit_valid_replace(self):
        with transaction.atomic():
            return self.replace(self.receipt())

    def commit_valid_retire(self):
        with transaction.atomic():
            receipt = self.receipt()
            return self.event(
                receipt,
                sequence=2,
                selection_type="RETIRE",
                previous_selection=self.first,
                supersession_determination=self.superseded(receipt),
            )


class AppendOnlyTests(GuardCase):
    def test_no_table_can_be_updated_or_deleted(self):
        binding = RequirementBinding.objects.first()
        odometer = OdometerObligation.objects.first()
        for row in (binding, self.version, self.obligation, self.first, odometer):
            table = type(row)._meta.db_table
            # Raw SQL: Django's own PROTECT would stop an ORM delete before the trigger runs.
            for operation, sql in (
                ("update", f"UPDATE {table} SET recorded_at = recorded_at WHERE id = %s"),
                ("delete", f"DELETE FROM {table} WHERE id = %s"),
            ):
                with self.subTest(table=table, op=operation), self.refused("append-only table"):
                    with connection.cursor() as cursor:
                        cursor.execute(sql, [row.pk])
        self.assertTrue(Obligation.objects.filter(pk=self.obligation.pk).exists())


class SelectionStreamTests(GuardCase):
    def test_a_valid_replacement_is_accepted(self):
        event = self.commit_valid_replace()
        self.assertEqual(event.sequence, 2)
        self.assertEqual(event.previous_selection_id, self.first.pk)

    def test_the_first_event_must_be_a_select_with_sequence_one_and_no_predecessor(self):
        for fields in (
            {"sequence": 2, "selection_type": "SELECT"},
            {"sequence": 1, "selection_type": "REPLACE"},
            {"sequence": 1, "selection_type": "RETIRE"},
            {"sequence": 1, "selection_type": "SELECT", "previous_selection": self.first},
        ):
            with self.subTest(fields=fields), self.refused("selection_shape|predecessor"):
                receipt = self.receipt()
                self.event(
                    receipt,
                    selected_obligation=None
                    if fields["selection_type"] == "RETIRE"
                    else self.new_obligation(receipt),
                    **fields,
                )

    def test_a_gap_in_the_sequence_is_refused(self):
        with self.refused("selection predecessor mismatch"):
            self.replace(self.receipt(), sequence=3)

    def test_the_predecessor_must_be_the_tail_of_the_same_anchor(self):
        other = ObligationSelection.objects.get(item=self.masks, due_at=self.due, sequence=1)
        with self.refused("selection predecessor mismatch|foreign key"):
            self.replace(self.receipt(), previous=other)

    def test_replacing_a_selected_obligation_needs_its_own_superseded_determination(self):
        with self.refused("selection supersession mismatch"):
            self.replace(self.receipt(), determination=None)
        wrong = {
            "another obligation": lambda r: self.superseded(
                r,
                ObligationSelection.objects.get(
                    item=self.masks, due_at=self.due
                ).selected_obligation,
            ),
            "not a supersession": lambda r: self.superseded(r, determination_type="WAIVED"),
            "another vehicle": lambda r: self.superseded(r, vehicle_id=str(self.vehicles[1].pk)),
            "another due instant": lambda r: self.superseded(
                r, due_at=self.due + timedelta(days=1)
            ),
        }
        for name, make in wrong.items():
            with self.subTest(name), self.refused("selection supersession mismatch"):
                receipt = self.receipt()
                self.replace(receipt, determination=make(receipt))

    def test_a_determination_from_another_command_is_refused(self):
        with self.refused("selection supersession mismatch"):
            earlier = self.superseded(self.receipt())
            self.replace(self.receipt(), determination=earlier)

    def test_the_selected_obligation_must_come_from_the_same_command(self):
        with self.refused("selected obligation was not created by this command"):
            receipt = self.receipt()
            elsewhere = self.new_obligation(self.receipt())
            self.replace(receipt, obligation=elsewhere)

    def test_the_selected_obligation_must_belong_to_the_anchor(self):
        with self.refused("foreign key"):
            receipt = self.receipt()
            other_item = self.new_obligation(
                receipt,
                item_id=self.masks.pk,
                requirement_version=self.new_version(
                    receipt, item_id=self.masks.pk, item_def_id=self.version.item_def_id
                ),
            )
            self.replace(receipt, obligation=other_item)

    def test_one_obligation_cannot_be_selected_twice(self):
        with self.refused("obligation already selected"):
            receipt = self.receipt()
            second = self.replace(receipt)
            self.replace(
                receipt,
                previous=second,
                sequence=3,
                obligation=second.selected_obligation,
                determination=self.superseded(receipt, second.selected_obligation),
            )

    def test_the_stream_cannot_fork(self):
        self.commit_valid_replace()
        with self.refused("Duplicate entry", IntegrityError):
            self.replace(self.receipt())  # same predecessor and sequence again

    def test_a_retirement_names_no_obligation_and_carries_a_determination(self):
        self.commit_valid_retire()
        for fields, message in (
            (
                {"selected_obligation": "new", "supersession_determination": "new"},
                "selection_shape",
            ),
            (
                {"selected_obligation": None, "supersession_determination": None},
                "selection supersession mismatch",
            ),
        ):
            with self.subTest(fields=fields), self.refused(message):
                receipt = self.receipt()
                self.event(
                    receipt,
                    sequence=2,
                    selection_type="RETIRE",
                    previous_selection=self.first,
                    selected_obligation=self.new_obligation(receipt)
                    if fields["selected_obligation"]
                    else None,
                    supersession_determination=self.superseded(receipt)
                    if fields["supersession_determination"]
                    else None,
                )

    def test_after_a_retirement_only_a_replace_without_supersession_may_follow(self):
        retire = self.commit_valid_retire()
        with self.refused("selection after retirement mismatch"):  # nothing left to retire
            receipt = self.receipt()
            self.event(
                receipt,
                sequence=3,
                selection_type="RETIRE",
                previous_selection=retire,
                supersession_determination=self.superseded(receipt),
            )
        with self.refused("selection after retirement mismatch"):  # supersedes nothing
            receipt = self.receipt()
            self.event(
                receipt,
                sequence=3,
                selection_type="REPLACE",
                previous_selection=retire,
                selected_obligation=self.new_obligation(receipt),
                supersession_determination=self.superseded(receipt),
            )
        with transaction.atomic():
            receipt = self.receipt()
            again = self.event(
                receipt,
                sequence=3,
                selection_type="REPLACE",
                previous_selection=retire,
                selected_obligation=self.new_obligation(receipt),
            )
        self.assertIsNone(again.supersession_determination_id)


class AttributionTests(GuardCase):
    def test_a_row_must_match_its_command_receipt(self):
        wrong = {
            "recorded_at": lambda r: {"recorded_at": r.recorded_at + timedelta(seconds=1)},
            "service identity": lambda r: {"service_identity": "worker.someone-else"},
            "job key": lambda r: {"job_key": '["another"]'},
            "claims a human": lambda r: {"actor_user": self.admin},
        }
        for name, overrides in wrong.items():
            with self.subTest(name), self.refused("obligation receipt attribution mismatch"):
                receipt = self.receipt()
                version = RequirementVersion.objects.get(pk=self.version.pk)
                Obligation.objects.create(
                    id=new_id(),
                    organization_id=self.org.pk,
                    vehicle_id=self.obligation.vehicle_id,
                    item_id=self.obligation.item_id,
                    requirement_version=version,
                    due_at=self.due + timedelta(days=5),
                    early_at=self.due + timedelta(days=5) - timedelta(hours=1),
                    grace_at=self.due + timedelta(days=5) + timedelta(hours=1),
                    routine=True,
                    basis_at=receipt.recorded_at,
                    **self.evidence(receipt, **overrides(receipt)),
                )

    def test_a_row_must_name_either_a_human_or_a_worker(self):
        with self.refused("odometer receipt attribution mismatch|odometer_ob_actor_kind"):
            receipt = self.receipt()
            self.raw_odometer(receipt, service_identity=None, job_key=None)

    def test_the_inspection_key_must_name_the_schedule_row_slot_and_instant(self):
        existing = OdometerObligation.objects.first()
        later = existing.due_at + timedelta(days=3)
        cases = {
            "an invented key": {"scheduled_inspection_key": "anything"},
            "the key of another instant": {"due_at": later, "early_at": later, "grace_at": later},
        }
        for name, overrides in cases.items():
            with self.subTest(name), self.refused("odometer inspection key mismatch"):
                self.raw_odometer(self.receipt(), **overrides)

    def test_cross_organization_references_are_refused(self):
        other_org = Organization.objects.create(name="Other Agency", station_zone="UTC")
        stranger = Vehicle.objects.create(organization=other_org, display_name="Elsewhere")
        with self.refused("foreign key"):
            receipt = self.receipt()
            self.new_obligation(receipt, vehicle_id=stranger.pk)

    def test_command_type_guards_reject_unrelated_commands(self):
        # 1. Obligation rejects non-OBLIGATIONS_RECONCILE
        unrelated_worker = self.custom_receipt("OTHER_WORKER_COMMAND")
        with self.refused("obligation receipt attribution mismatch"):
            self.new_obligation(unrelated_worker, version=self.version)

        # 2. RequirementVersion rejects non-OBLIGATIONS_RECONCILE
        with self.refused("requirement receipt attribution mismatch"):
            self.new_version(unrelated_worker)

        # 3. ObligationSelection rejects non-OBLIGATIONS_RECONCILE
        with self.refused("selection receipt attribution mismatch"):
            self.event(
                unrelated_worker,
                sequence=2,
                selection_type="REPLACE",
                selected_obligation=self.obligation,
            )

        # 4. OdometerObligation rejects non-OBLIGATIONS_RECONCILE
        with self.refused("odometer receipt attribution mismatch"):
            self.raw_odometer(unrelated_worker)

        # 5. RequirementBinding rejects non-REQUIREMENT_BIND
        unrelated_human = self.custom_receipt("ROLE_CHANGE", is_worker=False)
        with self.refused("binding receipt attribution mismatch"):
            RequirementBinding.objects.create(
                id=new_id(),
                organization_id=self.org.pk,
                vehicle_id=self.vehicle.pk,
                template_id=self.template.pk,
                slot="test-0600",
                revision=2,
                effective_at=self.at(hours=10),
                action="BIND",
                **self.evidence(
                    unrelated_human, actor_user=self.admin, service_identity=None, job_key=None
                ),
            )

        # Positive checks:
        with transaction.atomic():
            # Valid worker OBLIGATIONS_RECONCILE is accepted on all obligation tables
            valid_worker = self.receipt()
            v = self.new_version(valid_worker)
            self.assertIsNotNone(v.pk)
            o = self.new_obligation(valid_worker, version=v)
            self.assertIsNotNone(o.pk)

            # Valid human REQUIREMENT_BIND is accepted
            valid_human = self.custom_receipt("REQUIREMENT_BIND", is_worker=False)
            b = RequirementBinding.objects.create(
                id=new_id(),
                organization_id=self.org.pk,
                vehicle_id=self.vehicle.pk,
                template_id=self.template.pk,
                slot="test-0600",
                revision=2,
                effective_at=self.at(hours=10),
                action="BIND",
                **self.evidence(
                    valid_human, actor_user=self.admin, service_identity=None, job_key=None
                ),
            )
            self.assertIsNotNone(b.pk)


class UniquenessAndShapeTests(GuardCase):
    def test_the_natural_key_rejects_a_second_obligation_for_the_same_requirement(self):
        with self.refused("Duplicate entry", IntegrityError):
            self.new_obligation(self.receipt(), version=self.version)

    def test_a_second_odometer_obligation_for_the_same_inspection_is_refused(self):
        with self.refused("Duplicate entry", IntegrityError):
            self.raw_odometer(self.receipt())

    def test_windows_must_be_ordered(self):
        for early, grace in ((1, 1), (-1, -1), (2, -2)):
            with self.subTest(early=early, grace=grace), self.refused("obligation_window_order"):
                receipt = self.receipt()
                self.new_obligation(
                    receipt,
                    early_at=self.due + timedelta(hours=early),
                    grace_at=self.due + timedelta(hours=grace),
                )

    def test_ids_must_be_canonical_lowercase_uuids(self):
        with self.refused("cya_obl_obligation_uuid"):
            receipt = self.receipt()
            Obligation.objects.create(
                id=new_id().upper(),
                organization_id=self.org.pk,
                vehicle_id=self.obligation.vehicle_id,
                item_id=self.obligation.item_id,
                requirement_version=self.new_version(receipt),
                due_at=self.due + timedelta(days=9),
                early_at=self.due + timedelta(days=9),
                grace_at=self.due + timedelta(days=9),
                routine=True,
                basis_at=receipt.recorded_at,
                **self.evidence(receipt),
            )


class RequirementPinTests(GuardCase):
    def test_a_pin_must_be_internally_consistent(self):
        self.publish_schedule("other-slot")
        from cya.schedules.models import ScheduleRule

        other_schedule = ScheduleRule.objects.get(slot="other-slot")
        other_version = self.published_version()
        from cya.checklists.models import ChecklistItemDef, ChecklistPublication

        other_publication = ChecklistPublication.objects.get(checklist_version_id=other_version)
        other_def = ChecklistItemDef.objects.filter(
            checklist_version_id=other_version, item=self.suction
        ).get()
        cases = {
            "schedule of another slot": (
                {"schedule_rule_id": other_schedule.pk},
                "schedule mismatch",
            ),
            "definition from a version the publication does not govern": (
                {"item_def_id": other_def.pk},
                "definition mismatch",
            ),
            "unknown override": ({"override_version_id": new_id()}, "override mismatch"),
        }
        for name, (overrides, message) in cases.items():
            with self.subTest(name), self.refused(message):
                self.new_version(self.receipt(), **overrides)
        # A publication of a different version with its own definition is consistent.
        with transaction.atomic():
            self.new_version(
                self.receipt(),
                publication_id=other_publication.pk,
                item_def_id=other_def.pk,
            )

    def test_a_binding_that_is_not_in_force_cannot_back_a_requirement(self):
        self.clock.set(self.at(hours=1))
        self.bind(action="UNBIND", reconcile=False)
        unbound = RequirementBinding.objects.get(action="UNBIND")
        with self.refused("binding or publication mismatch"):
            self.new_version(self.receipt(), binding_id=unbound.pk)

    def test_a_conditional_rule_must_be_a_carry_over_rule_for_the_same_item(self):
        from cya.conditions.sync import create_conditional_rule

        rule = create_conditional_rule(
            organization=self.org,
            rule_type="CARRY_OVER_FROM_PRIOR_SHIFT",
            revision=1,
            typed_inputs={"item_id": str(self.masks.pk)},
            effective_at=self.at(),
            actor_user=self.admin,
            actor_roles_at_time=["ADMINISTRATOR"],
        )
        with self.refused(
            "conditional rule mismatch"
        ):  # rule is for masks, requirement for suction
            self.new_version(self.receipt(), conditional_rule_id=rule.pk)


class BindingChainTests(GuardCase):
    def raw_binding(self, receipt, **fields):
        base = {
            "organization_id": self.org.pk,
            "vehicle_id": self.vehicle.pk,
            "template_id": self.template.pk,
            "slot": "test-0600",
            "revision": 2,
            "action": "UNBIND",
            "effective_at": receipt.recorded_at + timedelta(hours=1),
        }
        base.update(fields)
        return RequirementBinding.objects.create(
            id=new_id(),
            **base,
            **self.evidence(receipt, actor_user=self.admin, service_identity=None, job_key=None),
        )

    def human_receipt(self, command_type="REQUIREMENT_BIND"):
        return append_receipt(
            organization=self.org,
            actor_user=self.admin,
            operation_id=new_id(),
            command_type=command_type,
            request_digest="0" * 64,
            response_status=200,
            response_body={},
            correlation_id=new_id(),
        )

    def test_revisions_are_consecutive_and_move_forward_in_time(self):
        with transaction.atomic():
            self.raw_binding(self.human_receipt())  # revision 2 is valid
        cases = {
            "a repeated revision": ({"revision": 2}, "binding revision chain mismatch|Duplicate"),
            "a skipped revision": ({"revision": 4}, "binding revision chain mismatch"),
            "a restarted chain": ({"revision": 1}, "binding revision chain mismatch|Duplicate"),
            "an unknown slot": (
                {"slot": "no-such-slot", "revision": 1},
                "binding names an unknown schedule slot",
            ),
        }
        for name, (fields, message) in cases.items():
            with self.subTest(name), self.refused(message):
                self.raw_binding(self.human_receipt(), **fields)

    def test_a_binding_cannot_take_effect_before_it_is_recorded(self):
        with self.refused("binding is retroactive"):
            receipt = self.human_receipt()
            self.raw_binding(receipt, effective_at=receipt.recorded_at - timedelta(seconds=1))

    def test_a_revision_must_take_effect_after_the_previous_one(self):
        self.clock.set(self.at(hours=1))
        self.bind(action="UNBIND", effective_at=self.at(hours=10), reconcile=False)  # revision 2
        with self.refused("binding revision chain mismatch"):
            receipt = self.human_receipt()
            self.raw_binding(
                receipt,
                revision=3,
                action="BIND",
                effective_at=receipt.recorded_at + timedelta(hours=1),  # before revision 2's
            )


class RevivalTests(GuardCase):
    def test_a_requirement_matching_an_archived_obligation_exactly_fails_loudly(self):
        # Unreachable through the commands (every change appends new pin rows), but if the
        # records ever allowed it, the unit must refuse rather than loop on a unique-key rollback.
        self.commit_valid_retire()  # the suction anchor now ends in RETIRE, its pins unchanged
        before = Obligation.objects.count()
        with self.assertRaisesRegex(RequirementRevived, "refusing to revive"):
            reconcile_unit(str(self.org.pk), str(self.vehicle.pk), self.due)
        self.assertEqual(Obligation.objects.count(), before)
        summary = self.generate()
        self.assertEqual({f.due_at for f in summary.failures}, {self.due})
        self.assertTrue(all("RequirementRevived" in f.error for f in summary.failures))
        self.assertEqual(self.generate(dry_run=True).failures[0].due_at, self.due)  # dry run too
