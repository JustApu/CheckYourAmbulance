"""Paper seal verifications and breaks (SPEC 8.0.0 LATE-7, §10.1; issue #9 Part B4).

A sealed synthetic kit is resealed at about 05:00 (UTC station zone) and its 06:00 obligation
has E = 05:00 and G = 07:00. Paper is entered at 06:30 for work performed at 05:30.
"""

from datetime import date, time
from uuid import uuid4

from django.db import transaction

from cya.checklists.models import ChecklistItemDef
from cya.checks.commands import CheckError
from cya.core.audit_repository import append_receipt
from cya.kits.sealable import designate_kit_sealable
from cya.kits.tests.support import KitCase
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.state import State
from cya.paper.commands import record_paper_entry_batch
from cya.seals.commands import SealRefusal, break_seal, verify_seal
from cya.seals.models import (
    SealEvent,
    SealInspection,
    SealInspectionItem,
    SealSubstitution,
    SealSubstitutionObligation,
)


class PaperSealCase(KitCase):
    def setUp(self):
        super().setUp()
        self.kit_id = self.add_kit("Synthetic kit").body["kit_id"]
        designate_kit_sealable(kit_id=self.kit_id, sealable=True, **self.context(self.supervisor))
        self.publish_schedule("test-0600")
        # After the cutover (SPEC 5.0.0) the vehicle's own checklist governs; a batch header
        # names its version (B1).
        self.record_cutover()
        own = self.vehicle_checklist()
        version = self.publish_checklist(own, [self.spec(self.kit_item, kit_id=self.kit_id)])
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

    def test_only_a_break_or_verification_is_entered_from_paper(self):
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
