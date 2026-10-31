"""Database guards for deficiencies: freeze, append-only, receipt attribution, scope ties."""

from datetime import timedelta
from uuid import uuid4

from django.db import DatabaseError, connection, transaction

from cya.checklists.models import Compartment, KitInstance
from cya.core.audit_repository import append_receipt
from cya.deficiencies.models import (
    Deficiency,
    DeficiencyAffectedItem,
    DeficiencyCategory,
    DeficiencyEvent,
    DeficiencyEvidence,
    DeficiencyLink,
    DeficiencyLock,
    DeficiencyProjection,
    DeficiencySeverity,
)
from cya.deficiencies.tests.support import DeficiencyCase


class GuardTests(DeficiencyCase):
    def setUp(self):
        super().setUp()
        self.result = self.failed()
        self.row = Deficiency.objects.get()
        self.check = self.row.command  # the creating command's receipt
        self.second_id = self.failed(self.siren).body["deficiency_ids"][0]

    def refused(self, operation):
        with self.assertRaises(DatabaseError), transaction.atomic():
            operation()

    def receipt(self, command_type="DEFICIENCY_REPORT", *, actor=None, operation_id=None):
        with transaction.atomic():
            return append_receipt(
                organization=self.org,
                operation_id=operation_id or str(uuid4()),
                actor_user=actor or self.crew,
                command_type=command_type,
                request_digest="0" * 64,
                response_status=201,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )

    def stamp(self, receipt, actor=None):
        return {
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": actor or self.crew,
            "actor_roles_at_time": ["CREW"],
        }

    def header(self, receipt=None, **overrides):
        receipt = receipt or self.receipt()
        fields = {
            "organization_id": self.org.pk,
            "operation_id": receipt.operation_id,
            "reported_at": receipt.recorded_at,
            "reporter": self.crew,
            "category_version": self.plain,
            "severity_version": self.level("LOW"),
            "summary": "Synthetic",
            "description": "Synthetic.",
            "vehicle": self.vehicle,
            **self.stamp(receipt),
            **overrides,
        }
        return Deficiency.objects.create(**fields)

    def test_a_valid_header_is_accepted_so_the_refusals_below_are_meaningful(self):
        self.header()
        self.assertEqual(Deficiency.objects.count(), 3)

    def test_the_creation_header_is_frozen_but_never_deleted(self):
        rows = Deficiency.objects.filter(pk=self.row.pk)
        for field, value in {
            "summary": "Changed",
            "severity_version": self.level("LOW"),
            "description": "Changed.",
            "item": self.masks,
            "vehicle": self.vehicles[1],
            "category_version": self.stock,
            "operation_id": str(uuid4()),
            "reported_at": self.row.reported_at + timedelta(seconds=1),
            "actor_roles_at_time": ["ADMINISTRATOR"],
            "kit": None,
        }.items():
            with self.subTest(field):
                if field == "kit":
                    continue  # unchanged null-to-null is not a change
                self.refused(lambda f=field, v=value: rows.update(**{f: v}))
        self.refused(rows.delete)
        self.row.refresh_from_db()
        self.assertEqual(self.row.summary, "Synthetic summary")

    def test_a_null_target_cannot_become_a_value(self):
        extra = self.header()
        self.refused(lambda: Deficiency.objects.filter(pk=extra.pk).update(item=self.suction))

    def test_the_projection_may_change_state_but_not_its_anchor(self):
        projection = DeficiencyProjection.objects.get(deficiency=self.row)
        rows = DeficiencyProjection.objects.filter(pk=projection.pk)
        rows.update(status="IN_PROGRESS", assignee=self.supervisor)
        self.refused(lambda: rows.update(deficiency_id=self.second_id))
        self.refused(lambda: rows.update(organization_id=uuid4()))
        self.refused(lambda: rows.update(status="DONE"))

    def test_a_lock_cannot_be_retargeted(self):
        lock = DeficiencyLock.objects.get(deficiency=self.row)
        self.refused(
            lambda: DeficiencyLock.objects.filter(pk=lock.pk).update(deficiency_id=self.second_id)
        )

    def test_the_append_only_tables_reject_update_and_delete(self):
        for model, field, value in (
            (DeficiencyEvent, "explanation", "x"),
            (DeficiencyAffectedItem, "item_id", self.masks.pk),
            (DeficiencyEvidence, "note", "x"),
        ):
            with self.subTest(model.__name__):
                rows = model.objects.all()
                self.refused(lambda r=rows, f=field, v=value: r.update(**{f: v}))
                self.refused(rows.delete)

    def test_a_category_revision_is_frozen(self):
        rows = DeficiencyCategory.objects.filter(pk=self.plain.pk)
        self.refused(lambda: rows.update(supply_related=True))
        self.refused(rows.delete)
        self.plain.refresh_from_db()
        self.assertFalse(self.plain.supply_related)

    def test_a_header_must_match_its_receipt(self):
        receipt = self.receipt()
        self.refused(lambda: self.header(receipt, operation_id=str(uuid4())))
        self.refused(lambda: self.header(receipt, reporter=self.supervisor))
        self.refused(lambda: self.header(receipt, actor_user=self.supervisor))
        self.refused(
            lambda: self.header(receipt, reported_at=receipt.recorded_at + timedelta(seconds=1))
        )
        self.refused(lambda: self.header(self.receipt("CHECK_CORRECTION")))
        self.refused(lambda: self.header(self.receipt("DEFICIENCY_LINK_ADD")))

    def test_a_header_needs_a_category_in_force_and_a_consistent_target(self):
        future = self.category(
            "Synthetic future", supply_related=False, when=self.now + timedelta(days=1)
        )
        self.refused(lambda: self.header(category_version=future))
        other = Compartment.objects.get(
            vehicle=self.vehicles[1], stable_slot_id=self.cabinet.stable_slot_id
        )
        self.refused(lambda: self.header(compartment=other))
        mine = Compartment.objects.get(
            vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )
        self.header(compartment=mine)
        self.refused(lambda: self.header(compartment=mine, item=self.suction))
        kit = KitInstance.objects.create(
            organization=self.org, label="Synthetic kit", vehicle=self.vehicles[1]
        )
        self.refused(lambda: self.header(kit=kit))
        retired = DeficiencySeverity.objects.create(
            organization=self.org,
            code="LOW",
            revision=2,
            label="Low",
            description="Retired.",
            rank=1,
            active=False,
            effective_at=self.now - timedelta(hours=1),
        )
        self.refused(lambda: self.header(severity_version=retired))  # a retired level
        # The original revision of the retired level is superseded, so it cannot be used either.
        self.refused(lambda: self.header(severity_version=self.level("LOW", 1)))
        # The same for a superseded category revision and a superseded severity revision.
        newer = self.category(
            "Synthetic mechanical v2",
            supply_related=True,
            revision=2,
            stable=self.plain.stable_category_id,
            when=self.now - timedelta(hours=1),
        )
        self.refused(lambda: self.header(category_version=self.plain))
        self.header(category_version=newer, severity_version=self.level("MEDIUM"))
        later = DeficiencySeverity.objects.create(
            organization=self.org,
            code="HIGH",
            revision=2,
            label="High",
            description="Not yet in force.",
            rank=3,
            active=True,
            effective_at=self.now + timedelta(days=1),
        )
        self.refused(lambda: self.header(severity_version=later))  # not yet effective
        self.refused(lambda: self.header(summary=" "))

    def test_affected_items_belong_to_the_creating_command(self):
        later = self.receipt()
        definition = DeficiencyAffectedItem.objects.get(deficiency=self.row).definition_id
        self.refused(
            lambda: DeficiencyAffectedItem.objects.create(
                organization_id=self.org.pk,
                deficiency=self.row,
                item=self.suction,
                definition_id=definition,
                **self.stamp(later),
            )
        )
        self.refused(
            lambda: DeficiencyAffectedItem.objects.create(
                organization_id=self.org.pk,
                deficiency=self.row,
                item=self.masks,  # not the item this definition belongs to
                definition_id=definition,
                **self.stamp(self.check),
            )
        )

    def test_events_cannot_skip_fork_or_misreport(self):
        receipt = self.receipt()
        header = self.header(receipt)
        base = {"organization_id": self.org.pk, "deficiency": header, **self.stamp(receipt)}
        bad = [
            {"sequence": 2, "event_type": "REPORTED", "to_status": "OPEN"},
            {"sequence": 1, "event_type": "REPORTED", "to_status": "CLOSED_VERIFIED"},
            {"sequence": 1, "event_type": "REPORTED", "to_status": "OPEN", "from_status": "OPEN"},
            {"sequence": 1, "event_type": "REPORTED", "to_status": "OPEN", "explanation": "x"},
            {"sequence": 1, "event_type": "ASSIGNED", "to_status": "OPEN"},
        ]
        for fields in bad:
            with self.subTest(fields):
                self.refused(lambda f=fields: DeficiencyEvent.objects.create(**base, **f))
        stray = self.receipt()  # a command that did not create the header
        self.refused(
            lambda: DeficiencyEvent.objects.create(
                organization_id=self.org.pk,
                deficiency=self.row,
                sequence=1,
                event_type="REPORTED",
                to_status="OPEN",
                **self.stamp(stray),
            )
        )
        self.assertEqual(DeficiencyEvent.objects.count(), 2)

    def test_evidence_must_be_a_failed_or_unable_check_of_the_same_vehicle_and_command(self):
        passed = self.record(outcome="PASSED", item=self.masks, quantity=6)
        receipt = self.receipt("CHECK_RECORD")
        base = {"organization_id": self.org.pk, "deficiency": self.row, **self.stamp(receipt)}
        self.refused(
            lambda: DeficiencyEvidence.objects.create(
                check_record_id=passed.body["check_id"], **base
            )
        )
        failed_check = self.result.body["check_id"]
        self.refused(  # the same check cannot be evidence for the same deficiency twice
            lambda: DeficiencyEvidence.objects.create(check_record_id=failed_check, **base)
        )
        # one Failed check may support another deficiency of its vehicle (issue 81 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/81))
        other = {**base, "deficiency_id": self.second_id}
        other.pop("deficiency")
        DeficiencyEvidence.objects.create(check_record_id=failed_check, **other)
        self.refused(lambda: DeficiencyEvidence.objects.create(**base))  # no source
        self.refused(
            lambda: DeficiencyEvidence.objects.create(
                check_record_id=failed_check, note="both", **base
            )
        )
        self.refused(  # duplicate evidence for one check
            lambda: DeficiencyEvidence.objects.create(
                check_record_id=failed_check, **{**base, **self.stamp(self.check)}
            )
        )
        self.assertEqual(DeficiencyEvidence.objects.count(), 3)

    def link(self, receipt, first, second, link_type="ADD", prior=None, **extra):
        lower, upper = sorted([first, second])
        return DeficiencyLink.objects.create(
            organization_id=self.org.pk,
            lower_id=lower,
            upper_id=upper,
            link_type=link_type,
            prior_link_event=prior,
            **self.stamp(receipt),
            **extra,
        )

    def test_link_rows_are_ordered_alternating_and_cannot_fork(self):
        first = str(self.row.pk)
        add = self.link(self.receipt("DEFICIENCY_LINK_ADD"), first, self.second_id)
        self.refused(lambda: self.link(self.receipt("DEFICIENCY_LINK_ADD"), first, self.second_id))
        self.refused(  # wrong command type for a removal
            lambda: self.link(
                self.receipt("DEFICIENCY_LINK_ADD"), first, self.second_id, "REMOVE", add
            )
        )
        self.refused(  # an add that follows an add
            lambda: self.link(
                self.receipt("DEFICIENCY_LINK_ADD"), first, self.second_id, "ADD", add
            )
        )
        self.refused(  # a removal with nothing to remove
            lambda: self.link(
                self.receipt("DEFICIENCY_LINK_REMOVE"), first, self.second_id, "REMOVE", None
            )
        )
        removal = self.link(
            self.receipt("DEFICIENCY_LINK_REMOVE"), first, self.second_id, "REMOVE", add
        )
        self.refused(  # a second successor of the same row forks the history
            lambda: self.link(
                self.receipt("DEFICIENCY_LINK_REMOVE"), first, self.second_id, "REMOVE", add
            )
        )
        self.link(self.receipt("DEFICIENCY_LINK_ADD"), first, self.second_id, "ADD", removal)
        self.assertEqual(DeficiencyLink.objects.count(), 3)

    def test_a_link_must_be_in_canonical_order_and_not_a_self_link(self):
        lower, upper = sorted([str(self.row.pk), self.second_id])
        receipt = self.receipt("DEFICIENCY_LINK_ADD")
        base = {"organization_id": self.org.pk, "link_type": "ADD", **self.stamp(receipt)}
        self.refused(lambda: DeficiencyLink.objects.create(lower_id=upper, upper_id=lower, **base))
        self.refused(lambda: DeficiencyLink.objects.create(lower_id=lower, upper_id=lower, **base))

    def test_links_and_prior_links_cannot_cross_pairs(self):
        first = str(self.row.pk)
        third = self.header().pk
        add = self.link(self.receipt("DEFICIENCY_LINK_ADD"), first, self.second_id)
        self.refused(
            lambda: self.link(
                self.receipt("DEFICIENCY_LINK_REMOVE"), first, str(third), "REMOVE", add
            )
        )

    def test_roles_and_ids_are_checked_by_the_database(self):
        receipt = self.receipt()
        self.refused(lambda: self.header(receipt, actor_roles_at_time=["SYNTHETIC"]))
        self.refused(lambda: self.header(receipt, id="not-a-uuid"))
        with connection.cursor() as cursor:
            self.refused(
                lambda: cursor.execute(
                    "INSERT INTO deficiency_category (id, organization_id, stable_category_id, "
                    "revision, label, supply_related, effective_at) VALUES (%s,%s,%s,1,'x',0,NOW())",
                    [str(uuid4()), self.org.pk, "not-a-uuid"],
                )
            )

    def effect(self, command_type, event_type, **fields):
        """A correction-effect event row after the first deficiency's last event."""
        last = DeficiencyEvent.objects.filter(deficiency=self.row).order_by("-sequence").first()
        receipt = self.receipt(command_type)
        values = {
            "organization_id": self.org.pk,
            "deficiency": self.row,
            "sequence": last.sequence + 1,
            "predecessor_event": last,
            "event_type": event_type,
            "from_status": last.to_status,
            "to_status": "REPORTED_IN_ERROR",
            "explanation": "Synthetic.",
            "correction_ref": self.result.body["check_id"],
            **self.stamp(receipt),
            **fields,
        }
        return receipt, DeficiencyEvent.objects.create(**values)

    def test_correction_effect_events_need_their_command_chain_and_shape(self):
        root = self.result.body["check_id"]
        # no correction event was written by these receipts, so none of them may stand
        self.refused(lambda: self.effect("CHECK_CORRECTION", "REPORTED_IN_ERROR"))
        self.refused(lambda: self.effect("CHECK_RECORD", "REPORTED_IN_ERROR"))
        self.refused(lambda: self.effect("CHECK_CORRECTION", "CORRECTION_NOTICE", to_status="OPEN"))
        self.refused(
            lambda: self.effect(
                "CHECK_INVALIDATION", "REOPENED", from_status="REPORTED_IN_ERROR", to_status="OPEN"
            )
        )
        # shape: unknown chain, missing reference, missing explanation, impossible transitions
        self.refused(
            lambda: self.effect(
                "CHECK_CORRECTION", "REPORTED_IN_ERROR", correction_ref=str(uuid4())
            )
        )
        self.refused(
            lambda: self.effect("CHECK_CORRECTION", "REPORTED_IN_ERROR", correction_ref=None)
        )
        self.refused(lambda: self.effect("CHECK_CORRECTION", "REPORTED_IN_ERROR", explanation=None))
        self.refused(
            lambda: self.effect(
                "CHECK_CORRECTION", "REPORTED_IN_ERROR", from_status="AWAITING_EXTERNAL_DEPENDENCY"
            )
        )
        self.refused(
            lambda: self.effect(
                "CHECK_CORRECTION", "CORRECTION_NOTICE", to_status="CLOSED_VERIFIED"
            )
        )
        self.refused(lambda: self.effect("CHECK_CORRECTION", "REOPENED", to_status="OPEN"))
        self.refused(
            lambda: self.effect("CHECK_CORRECTION", "REPORTED_IN_ERROR", assignee=self.supervisor)
        )
        self.assertEqual(DeficiencyEvent.objects.filter(deficiency=self.row).count(), 1)
        self.assertTrue(root)

    def test_a_real_correction_writes_events_the_guards_accept_and_a_stray_reopening_is_refused(
        self,
    ):
        from cya.checks.commands import correct_check

        root = self.result.body["check_id"]
        correct_check(
            check_id=root,
            expected_tip_check_id=root,
            outcome="PASSED",
            **self.context(self.crew),
        )
        self.assertEqual(
            list(
                DeficiencyEvent.objects.filter(deficiency=self.row)
                .order_by("sequence")
                .values_list("event_type", flat=True)
            ),
            ["REPORTED", "REPORTED_IN_ERROR"],
        )
        # a reopening without an invalidation of the chain by the same command is refused
        self.refused(
            lambda: self.effect(
                "CHECK_INVALIDATION", "REOPENED", from_status="REPORTED_IN_ERROR", to_status="OPEN"
            )
        )
        # and a second event cannot follow a stale predecessor (the chain cannot fork)
        first = DeficiencyEvent.objects.get(deficiency=self.row, sequence=1)
        receipt = self.receipt("CHECK_CORRECTION")
        self.refused(
            lambda: DeficiencyEvent.objects.create(
                organization_id=self.org.pk,
                deficiency=self.row,
                sequence=2,
                predecessor_event=first,
                event_type="CORRECTION_NOTICE",
                from_status="OPEN",
                to_status="OPEN",
                explanation="Synthetic.",
                correction_ref=root,
                **self.stamp(receipt),
            )
        )
