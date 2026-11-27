"""Paper seal verifications, breaks and reseals (SPEC 8.5.0 LATE-7, §10.1; issue #9 Part B4).

A sealed synthetic kit is resealed at about 05:00 (UTC station zone) and its 06:00 obligation
has E = 05:00 and G = 07:00. Paper is entered at 06:30 for work performed at 05:30.
"""

from datetime import date, time
from uuid import uuid4

from django.db import DatabaseError, transaction

from cya.checklists.models import ChecklistItemDef
from cya.checks.commands import CheckError
from cya.core.audit_repository import append_receipt
from cya.kits.sealable import designate_compartment_sealable, designate_kit_sealable
from cya.kits.tests.support import KitCase
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.state import State
from cya.paper.commands import record_paper_entry_batch
from cya.seals.commands import SealError, SealRefusal, break_seal, verify_seal
from cya.seals.models import (
    SealEvent,
    SealInspection,
    SealInspectionItem,
    SealInspectionObligation,
    SealSubstitution,
    SealSubstitutionObligation,
)
from cya.seals.reseal import reseal


class PaperSealCase(KitCase):
    KIT_FIELDS = {}  # the kit item's checklist row fields
    CABINET_SEALABLE = False

    def setUp(self):
        super().setUp()
        self.kit_id = self.add_kit("Synthetic kit").body["kit_id"]
        designate_kit_sealable(kit_id=self.kit_id, sealable=True, **self.context(self.supervisor))
        if self.CABINET_SEALABLE:
            designate_compartment_sealable(
                vehicle_id=str(self.vehicle.pk),
                compartment_id=str(self.cabinet.pk),
                sealable=True,
                **self.context(self.supervisor),
            )
        self.publish_schedule("test-0600")
        # After the cutover (SPEC 5.0.0) the vehicle's own checklist governs; a batch header
        # names its version (B1).
        self.record_cutover()
        own = self.vehicle_checklist()
        version = self.publish_checklist(
            own, [self.spec(self.kit_item, kit_id=self.kit_id, **self.KIT_FIELDS)]
        )
        self.own = own
        self.own_version = version
        self.bind(template=own, reconcile=False)
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.at(hours=5))
        self.seal = self._reseal(version)
        self.clock.set(self.at(hours=6, minutes=30))

    def _reseal(self, version):
        with transaction.atomic():
            receipt = append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=self.crew,
                command_type="SEAL_RESEAL",
                request_digest="0" * 64,
                response_status=200,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )
            stamp = {
                "organization": self.org,
                "command": receipt,
                "recorded_at": receipt.recorded_at,
                "actor_user": self.crew,
                "actor_roles_at_time": ["CREW"],
            }
            seal = SealEvent.objects.create(
                **stamp,
                vehicle=self.vehicle,
                kit_id=self.kit_id,
                sequence=1,
                event_type="RESEAL",
                marking="sample",
                no_expiration=True,
            )
            inspection = SealInspection.objects.create(
                **stamp,
                reseal=seal,
                fresh_entry_attestation=True,
                written_marking_attestation=True,
            )
            SealInspectionItem.objects.create(
                **stamp,
                inspection=inspection,
                item=self.kit_item,
                kit_id=self.kit_id,
                instance_key=self.kit_id,
                item_def=ChecklistItemDef.objects.get(
                    checklist_version_id=version, item=self.kit_item
                ),
                outcome="PASSED",
            )
        return seal

    def batch(self, at=time(5, 30), actor=None, performers=None):
        actor = actor or self.crew
        return record_paper_entry_batch(
            vehicle_id=str(self.vehicle.pk),
            checklist_version_id=self.own_version,
            performed_on=date(2026, 6, 1),
            performed_time=at,
            performer_ids=[str(u.pk) for u in (performers or [actor])],
            **self.context(actor),
        ).body["batch_id"]

    def verify(self, batch_id=None, expected=None, actor=None):
        return verify_seal(
            vehicle_id=str(self.vehicle.pk),
            kit_id=self.kit_id,
            expected_tip_id=str(expected or self.seal.pk),
            marking="sample",
            attested=True,
            paper_entry_batch_id=batch_id,
            **self.context(actor or self.crew),
        )

    def break_(self, batch_id=None, expected=None):
        return break_seal(
            vehicle_id=str(self.vehicle.pk),
            kit_id=self.kit_id,
            expected_tip_id=str(expected or self.seal.pk),
            reason="Synthetic break",
            paper_entry_batch_id=batch_id,
            **self.context(self.crew),
        )

    def kit_state(self, instant):
        states = get_vehicle_obligations_as_of(
            str(self.vehicle.pk), instant, organization_id=str(self.org.pk)
        )
        link = SealSubstitutionObligation.objects.first()
        return next(s for s in states if s.obligation.id == str(link.obligation_id))


class VerifyTests(PaperSealCase):
    def test_a_paper_verification_relies_on_the_seal_and_completes_at_the_performance_time(self):
        result = self.verify(self.batch())
        substitution = SealSubstitution.objects.get(pk=result.body["substitution_id"])
        self.assertEqual(substitution.performed_at, self.at(hours=5, minutes=30))
        self.assertEqual(substitution.seal_event_id, self.seal.pk)
        self.assertEqual(result.body["link_count"], 1)
        self.assertIn("paper_entry", result.body)
        state = self.kit_state(substitution.recorded_at)
        self.assertEqual((state.state, state.outcome), (State.SATISFIED, "SEAL_SUBSTITUTION"))
        self.assertEqual(state.completed_at, substitution.performed_at)

    def test_no_seal_intact_at_the_performance_time_is_refused(self):
        with self.assertRaises(SealRefusal) as caught:
            self.verify(self.batch(at=time(4, 59)))
        self.assertEqual(caught.exception.code, "SEAL_NOT_INTACT_AT_PERFORMANCE")
        self.assertFalse(SealSubstitution.objects.exists())

    def test_a_seal_broken_after_the_performance_time_still_supports_it(self):
        broken = self.break_()
        result = self.verify(self.batch(), expected=broken.body["event_id"])
        self.assertEqual(
            SealSubstitution.objects.get(pk=result.body["substitution_id"]).seal_event_id,
            self.seal.pk,
        )

    def test_another_users_batch_answers_as_absent(self):
        batch_id = self.batch(actor=self.supervisor, performers=[self.crew])
        with self.assertRaises(CheckError) as caught:
            self.verify(batch_id, actor=self.crew)
        self.assertEqual(caught.exception.code, "NOT_FOUND")


class BreakTests(PaperSealCase):
    def test_a_paper_break_is_appended_at_its_performance_time(self):
        result = self.break_(self.batch())
        event = SealEvent.objects.get(pk=result.body["event_id"])
        self.assertEqual((event.event_type, event.sequence), ("BREAK", 2))
        self.assertEqual(event.performed_at, self.at(hours=5, minutes=30))
        self.assertEqual(event.marking, "sample")

    def test_a_paper_break_before_later_evidence_is_refused(self):
        self.verify()  # a live substitution now, after 05:30
        with self.assertRaises(SealRefusal) as caught:
            self.break_(self.batch())
        self.assertEqual(caught.exception.code, "SEAL_LATER_EVIDENCE")
        self.assertEqual(SealEvent.objects.count(), 1)

    def test_a_paper_verification_after_a_paper_break_is_refused(self):
        broken = self.break_(self.batch(at=time(5, 30)))
        with self.assertRaises(SealRefusal):
            self.verify(self.batch(at=time(5, 45)), expected=broken.body["event_id"])
        earlier = self.verify(self.batch(at=time(5, 15)), expected=broken.body["event_id"])
        self.assertEqual(earlier.status, 200)

    def test_an_audit_request_is_not_entered_from_paper(self):
        from cya.seals.commands import SealError, request_seal_audit

        with self.assertRaises(SealError):
            request_seal_audit(
                vehicle_id=str(self.vehicle.pk),
                kit_id=self.kit_id,
                expected_tip_id=str(self.seal.pk),
                paper_entry_batch_id=str(uuid4()),
                **self.context(self.supervisor),
            )

    def test_the_paper_window_is_judged_at_the_performance_time(self):
        self.clock.set(self.at(days=1, hours=12))  # entered after the 06:00 obligation's L (19:00)
        result = self.verify(self.batch(at=time(5, 30)))
        self.assertEqual(result.body["link_count"], 1)
        self.assertEqual(
            SealSubstitutionObligation.objects.get().late_eligible, False
        )  # on time at 05:30


class ResealCase(PaperSealCase):
    """SPEC 8.5.0 LATE-7: a paper reseal copied from the printed reseal section."""

    KIT_FIELDS = {"kind": "QUANTITY", "expected_quantity": 2, "expiration_tracked": True}

    def observation(self, **fields):
        row = {
            "item_id": str(self.kit_item.pk),
            "kit_id": self.kit_id,
            "outcome": None,
            "count": 2,
            "expiration": "2026-12-31",
            "measurement": None,
        }
        return row | fields

    def reseal_(self, batch_id, expected, observations=None, slot=False):
        return reseal(
            vehicle_id=str(self.vehicle.pk),
            **({"slot_id": self.slot} if slot else {"kit_id": self.kit_id}),
            expected_tip_id=str(expected) if expected else None,
            marking="paper-id",
            inspected_attestation=True,
            written_attestation=True,
            observations=observations or [self.observation()],
            paper_entry_batch_id=batch_id,
            **self.context(self.crew),
        )

    def kit_events(self):
        return list(
            SealEvent.objects.filter(kit_id=self.kit_id)
            .order_by("sequence")
            .values_list("event_type", flat=True)
        )


class ResealTests(ResealCase):
    def test_a_paper_reseal_is_placed_and_judged_at_the_performance_time(self):
        batch_id = self.batch()
        broken = self.break_(batch_id)
        result = self.reseal_(batch_id, broken.body["event_id"])
        event = SealEvent.objects.get(pk=result.body["event_id"])
        self.assertEqual((event.event_type, event.sequence), ("RESEAL", 3))
        self.assertEqual(event.performed_at, self.at(hours=5, minutes=30))
        self.assertEqual(str(event.paper_entry_batch_id), batch_id)
        self.assertEqual(event.expiration, date(2026, 12, 31))
        self.assertIn("paper_entry", result.body)
        item = SealInspectionItem.objects.get(inspection__reseal=event)
        self.assertEqual((item.count, item.expiration), (2, date(2026, 12, 31)))
        # Its obligation link is judged at 05:30, inside the 06:00 obligation's window.
        link = SealInspectionObligation.objects.get(inspection_item=item)
        states = get_vehicle_obligations_as_of(
            str(self.vehicle.pk), event.recorded_at, organization_id=str(self.org.pk)
        )
        state = next(s for s in states if s.obligation.id == str(link.obligation_id))
        self.assertEqual((state.state, state.outcome), (State.SATISFIED, "PASSED"))
        self.assertEqual(state.completed_at, event.performed_at)
        # The new seal is the one in force: a live verification relies on it.
        verified = verify_seal(
            vehicle_id=str(self.vehicle.pk),
            kit_id=self.kit_id,
            expected_tip_id=str(event.pk),
            marking="paper-id",
            attested=True,
            **self.context(self.crew),
        )
        self.assertEqual(
            SealSubstitution.objects.get(pk=verified.body["substitution_id"]).seal_event_id,
            event.pk,
        )

    def test_a_missing_count_or_expiration_date_is_refused_and_the_seal_stays_open(self):
        batch_id = self.batch()
        broken = self.break_(batch_id)
        for missing in ({"count": None}, {"expiration": None}):
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                self.reseal_(batch_id, broken.body["event_id"], [self.observation(**missing)])
        self.assertEqual(self.kit_events(), ["RESEAL", "BREAK"])

    def test_an_intact_seal_needs_its_paper_break_first(self):
        with self.assertRaisesRegex(SealError, "break the intact seal"):
            self.reseal_(self.batch(), self.seal.pk)

    def test_a_paper_reseal_before_a_later_seal_record_is_refused(self):
        early = self.batch(at=time(5, 30))
        broken = self.break_(early)
        live = reseal(
            vehicle_id=str(self.vehicle.pk),
            kit_id=self.kit_id,
            expected_tip_id=broken.body["event_id"],
            marking="live-id",
            inspected_attestation=True,
            written_attestation=True,
            observations=[self.observation()],
            **self.context(self.crew),
        )
        again = self.break_(expected=live.body["event_id"])
        with self.assertRaises(SealRefusal) as caught:
            self.reseal_(early, again.body["event_id"])
        self.assertEqual(caught.exception.code, "SEAL_LATER_EVIDENCE")
        self.assertEqual(self.kit_events(), ["RESEAL", "BREAK", "RESEAL", "BREAK"])

    def test_an_item_no_longer_identifiable_under_the_printed_version_is_refused(self):
        # A newer checklist version is in force at the performance time, but the batch names the
        # version printed on the sheet: the section's item cannot be mapped (LATE-4).
        self.publish_checklist(
            self.own, [self.spec(self.kit_item, kit_id=self.kit_id, **self.KIT_FIELDS)]
        )
        self.clock.set(self.at(hours=7, minutes=30))
        batch_id = self.batch(at=time(7, 0))
        broken = self.break_(batch_id)
        with self.assertRaises(SealRefusal) as caught:
            self.reseal_(batch_id, broken.body["event_id"])
        self.assertEqual(caught.exception.code, "PAPER_ITEM_UNIDENTIFIABLE")
        self.assertEqual(self.kit_events(), ["RESEAL", "BREAK"])

    def test_a_paper_reseal_takes_no_live_inspection_start(self):
        with self.assertRaises(SealError):
            reseal(
                vehicle_id=str(self.vehicle.pk),
                kit_id=self.kit_id,
                marking="paper-id",
                inspected_attestation=True,
                written_attestation=True,
                observations=[self.observation()],
                start_operation_id=str(uuid4()),
                paper_entry_batch_id=str(uuid4()),
                **self.context(self.crew),
            )


class CabinetResealTests(ResealCase):
    """CAB-5 and CAB-9: the cabinet's reseal section covers the kit inside it too."""

    CABINET_SEALABLE = True

    def test_a_paper_cabinet_reseal_inspects_the_kit_inside(self):
        batch_id = self.batch()
        with self.assertRaisesRegex(SealError, "nested kit seal"):
            self.reseal_(batch_id, None, slot=True)  # the kit's own seal was intact at 05:30
        self.break_(batch_id)  # the kit's seal, broken on paper at 05:30
        result = self.reseal_(batch_id, None, slot=True)
        event = SealEvent.objects.get(pk=result.body["event_id"])
        self.assertEqual((event.kit_id, str(event.compartment_slot_id)), (None, self.slot))
        self.assertEqual(event.performed_at, self.at(hours=5, minutes=30))
        self.assertEqual(
            str(SealInspectionItem.objects.get(inspection__reseal=event).kit_id), self.kit_id
        )


class PaperGuardTests(PaperSealCase):
    """Migration 0010: the database repeats the placement rules for paper seal events."""

    def receipt(self, command_type, body=None):
        return append_receipt(
            organization=self.org,
            operation_id=str(uuid4()),
            actor_user=self.crew,
            command_type=command_type,
            request_digest="0" * 64,
            response_status=200,
            response_body=body or {"code": "SYNTHETIC"},
            correlation_id=str(uuid4()),
        )

    def event(self, batch_id, at, event_type="RESEAL", prior=None, command="SEAL_RESEAL"):
        receipt = self.receipt(command)
        prior = prior or self.seal
        return SealEvent.objects.create(
            organization=self.org,
            command=receipt,
            recorded_at=receipt.recorded_at,
            actor_user=self.crew,
            actor_roles_at_time=["CREW"],
            vehicle=self.vehicle,
            kit_id=self.kit_id,
            sequence=prior.sequence + 1,
            prior_event=prior,
            event_type=event_type,
            marking="paper-id" if event_type == "RESEAL" else None,
            no_expiration=event_type == "RESEAL",
            paper_entry_batch_id=batch_id,
            performed_at=at,
        )

    def test_a_paper_audit_request_is_refused(self):
        batch_id = self.batch()
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.event(
                batch_id,
                self.at(hours=5, minutes=30),
                event_type="AUDIT_REQUEST",
                command="SEAL_AUDIT_REQUEST",
            )

    def test_a_paper_reseal_keeps_the_qualifying_order_and_has_no_live_start(self):
        late, early = self.batch(at=time(5, 30)), self.batch(at=time(5, 15))
        broken = SealEvent.objects.get(pk=self.break_(late).body["event_id"])
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.event(early, self.at(hours=5, minutes=15), prior=broken)  # before its break
        with transaction.atomic():
            resealed = self.event(late, self.at(hours=5, minutes=30), prior=broken)
            start = self.receipt(
                "SEAL_INSPECTION_START",
                {
                    "vehicle_id": str(self.vehicle.pk),
                    "kit_id": self.kit_id,
                    "slot_id": None,
                    "expected_tip_id": str(broken.pk),
                },
            )
        with self.assertRaisesRegex(DatabaseError, "no live start"), transaction.atomic():
            SealInspection.objects.create(
                organization=self.org,
                command=resealed.command,
                recorded_at=resealed.recorded_at,
                actor_user=self.crew,
                actor_roles_at_time=["CREW"],
                reseal=resealed,
                break_event=broken,
                fresh_entry_attestation=True,
                written_marking_attestation=True,
                start_receipt=start,
            )
