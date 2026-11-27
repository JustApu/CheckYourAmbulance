"""Paper reseal evidence: kit presence and kit-list counts (SPEC 8.5.0 LATE-7; issue #9 Part B4).

SEAL-6 and CAB-5 need a fresh presence check for each covered physical kit and a fresh count for
each kit-list line of a cabinet. For a paper reseal, LATE-7 takes them as paper checks of the
reseal's own batch, resolved at the printed versions: a check of another batch, or a live check,
is not fresh for it. Nothing here mocks the lines: kit K is of definition D, placed by vehicle A's
own checklist and sitting in the sealable cabinet, and the vehicle's kit list requires one D
there. The station zone is UTC; paper is entered at 06:30 for work performed at 05:30.
"""

import json
from datetime import date, time
from uuid import uuid4

from django.test import override_settings

from cya.checklists import commands as checklist_commands
from cya.checks.commands import record_check
from cya.kits.commands import add_kit_instance
from cya.kits.definitions import (
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
)
from cya.kits.kit_lists import publish_vehicle_kit_list, save_vehicle_kit_list_draft
from cya.kits.models import KitDefinition, VehicleKitRequirement
from cya.kits.sealable import designate_compartment_sealable, designate_kit_sealable
from cya.kits.tests.support import KitCase
from cya.paper.commands import record_paper_entry_batch
from cya.seals.commands import SealRefusal
from cya.seals.models import (
    SealEvent,
    SealInspectionKitCount,
    SealInspectionKitInstance,
    SealInspectionKitPresence,
)
from cya.seals.reseal import reseal
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

RESEAL = "/api/v1/seals/reseal"


class PaperResealEvidenceCase(KitCase):
    def setUp(self):
        super().setUp()
        supervisor = self.context(self.supervisor)
        self.definition = create_kit_definition(
            key="jump-kit", label="Synthetic jump kit", **supervisor
        ).body["definition_id"]
        self.kit_version = self.kit_draft()
        publish_kit_definition_version(version_id=self.kit_version, **self.context(self.supervisor))
        self.presence_item = str(KitDefinition.objects.get(pk=self.definition).presence_item_id)
        self.kit_id = add_kit_instance(
            label="Synthetic jump kit 1",
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            definition_id=self.definition,
            **self.context(self.supervisor),
        ).body["kit_id"]
        designate_kit_sealable(kit_id=self.kit_id, sealable=True, **self.context(self.supervisor))
        designate_compartment_sealable(
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            sealable=True,
            **self.context(self.supervisor),
        )
        kit_list = save_vehicle_kit_list_draft(
            vehicle_id=str(self.vehicle.pk),
            lines=[
                {
                    "definition_id": self.definition,
                    "compartment_id": str(self.cabinet.pk),
                    "required_count": 1,
                    "display_order": 1,
                }
            ],
            **self.context(self.supervisor),
        ).body["version_id"]
        publish_vehicle_kit_list(version_id=kit_list, **self.context(self.supervisor))
        self.line_id = str(VehicleKitRequirement.objects.get(version_id=kit_list).pk)
        self.publish_schedule("test-0600")
        self.record_cutover()
        own = self.vehicle_checklist()
        self.own_version = self.own_checklist_version(own)
        self.bind(template=own, reconcile=False)
        self.assertEqual(self.generate().failures, [])
        self.vehicle_b_version = (
            self.own_checklist_version(self.vehicle_checklist(vehicle=self.vehicle_b))
            if self.VEHICLE_B_CHECKLIST
            else None
        )
        self.actor = self.crew
        self.clock.set(self.at(hours=6, minutes=30))

    VEHICLE_B_CHECKLIST = False

    def own_checklist_version(self, checklist):
        """A loose item left Not placed (outside the cabinet) and the placement of D."""
        version_id = checklist_commands.save_template_draft(
            template_id=str(checklist.pk),
            items=[self.spec(self.siren, 30, compartment_id=None)],
            placements=[{"definition_id": self.definition, "display_order": 90}],
            **self.context(),
        ).body["version_id"]
        self.publish(version_id)
        return version_id

    def kit_draft(self):
        return save_kit_definition_draft(
            definition_id=self.definition,
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

    def batch(self, at=time(5, 30), actor=None, performers=None, kits=None, vehicle=None):
        actor = actor or self.actor
        return record_paper_entry_batch(
            vehicle_id=str((vehicle or self.vehicle).pk),
            checklist_version_id=self.vehicle_b_version if vehicle else self.own_version,
            performed_on=date(2026, 6, 1),
            performed_time=at,
            performer_ids=[str(u.pk) for u in (performers or [actor])],
            kit_definition_version_ids=[self.kit_version] if kits is None else kits,
            **self.context(actor),
        ).body["batch_id"]

    def presence_check(self, batch_id=None):
        """The kit's presence check: a paper check of batch_id, or a live check without one."""
        return self._check(batch_id, kit_id=self.kit_id, kit_presence=True)

    def count_check(self, batch_id=None):
        """The count of the cabinet's kit-list line: a paper check of batch_id, or live."""
        return self._check(batch_id, kit_requirement_id=self.line_id)

    def _check(self, batch_id, **fields):
        return record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=self.presence_item,
            checklist_version_id=self.own_version,
            purpose="ROUTINE",
            outcome=None,
            quantity=1,
            paper_entry_batch_id=batch_id,
            **fields,
            **self.context(self.actor),
        ).body["check_id"]

    def observations(self):
        return [
            {
                "item_id": str(self.kit_item.pk),
                "kit_id": self.kit_id,
                "outcome": "PASSED",
                "count": None,
                "expiration": None,
                "measurement": None,
            }
        ]

    def reseal_fields(self, batch_id, *, slot=False, counts=(), presence=()):
        return {
            "vehicle_id": str(self.vehicle.pk),
            **({"slot_id": self.slot} if slot else {"kit_id": self.kit_id}),
            "marking": "paper-id",
            "inspected_attestation": True,
            "written_attestation": True,
            "observations": self.observations(),
            "kit_count_checks": [{"line_id": self.line_id, "check_id": c} for c in counts],
            "kit_presence_checks": [{"kit_id": self.kit_id, "check_id": p} for p in presence],
            "paper_entry_batch_id": batch_id,
        }

    def reseal_(self, batch_id, **kwargs):
        return reseal(**self.reseal_fields(batch_id, **kwargs), **self.context(self.actor))


class KitPaperResealEvidenceTests(PaperResealEvidenceCase):
    """SEAL-6: a kit placed by its definition needs its own presence check, from the batch."""

    def test_a_presence_check_of_the_same_batch_certifies_the_kit_reseal(self):
        batch_id = self.batch()
        check_id = self.presence_check(batch_id)
        result = self.reseal_(batch_id, presence=[check_id])
        event = SealEvent.objects.get(pk=result.body["event_id"])
        self.assertEqual((str(event.kit_id), event.event_type), (self.kit_id, "RESEAL"))
        self.assertEqual(event.performed_at, self.at(hours=5, minutes=30))
        self.assertEqual(str(event.paper_entry_batch_id), batch_id)
        presence = SealInspectionKitPresence.objects.get(inspection__reseal=event)
        self.assertEqual(
            (str(presence.check_record_id), str(presence.kit_id), presence.count),
            (check_id, self.kit_id, 1),
        )
        self.assertEqual(str(presence.item_id), self.presence_item)
        self.assertFalse(SealInspectionKitCount.objects.exists())

    def test_a_missing_presence_check_or_one_of_another_batch_is_refused(self):
        batch_id = self.batch()
        other = self.presence_check(self.batch(at=time(5, 15)))
        with self.assertRaisesRegex(ValueError, "needs a fresh presence check"):
            self.reseal_(batch_id)
        with self.assertRaisesRegex(ValueError, "presence check is not current and fresh"):
            self.reseal_(batch_id, presence=[other])
        self.assertFalse(SealEvent.objects.exists())

    def test_a_live_presence_check_is_not_fresh_for_a_paper_reseal(self):
        batch_id = self.batch()
        live = self.presence_check()
        with self.assertRaisesRegex(ValueError, "presence check is not current and fresh"):
            self.reseal_(batch_id, presence=[live])
        self.assertFalse(SealEvent.objects.exists())

    def test_a_kit_item_not_identifiable_at_the_printed_kit_version_is_refused(self):
        # D's version 2 is in force at 06:45, but the batch names version 1 as printed (LATE-4).
        publish_kit_definition_version(version_id=self.kit_draft(), **self.context(self.supervisor))
        self.clock.set(self.at(hours=7))
        batch_id = self.batch(at=time(6, 45))
        check_id = self.presence_check(batch_id)
        with self.assertRaises(SealRefusal) as caught:
            self.reseal_(batch_id, presence=[check_id])
        self.assertEqual(caught.exception.code, "PAPER_ITEM_UNIDENTIFIABLE")
        self.assertFalse(SealEvent.objects.exists())


class CabinetPaperResealEvidenceTests(PaperResealEvidenceCase):
    """CAB-5: the cabinet's kit-list line count and the kit inside's presence, from the batch."""

    def test_count_and_presence_checks_of_the_same_batch_certify_the_cabinet_reseal(self):
        batch_id = self.batch()
        count_id = self.count_check(batch_id)
        presence_id = self.presence_check(batch_id)
        result = self.reseal_(batch_id, slot=True, counts=[count_id], presence=[presence_id])
        event = SealEvent.objects.get(pk=result.body["event_id"])
        self.assertEqual((event.kit_id, str(event.compartment_slot_id)), (None, self.slot))
        self.assertEqual(event.performed_at, self.at(hours=5, minutes=30))
        count = SealInspectionKitCount.objects.get(inspection__reseal=event)
        self.assertEqual(
            (str(count.check_record_id), str(count.kit_requirement_id), count.count),
            (count_id, self.line_id, 1),
        )
        self.assertEqual(
            [str(k) for k in SealInspectionKitInstance.objects.values_list("kit_id", flat=True)],
            [self.kit_id],
        )
        presence = SealInspectionKitPresence.objects.get(inspection__reseal=event)
        self.assertEqual(str(presence.check_record_id), presence_id)

    def test_a_missing_count_or_presence_check_or_a_count_of_another_batch_is_refused(self):
        other = self.count_check(self.batch(at=time(5, 15)))
        batch_id = self.batch()
        count_id = self.count_check(batch_id)
        presence_id = self.presence_check(batch_id)
        with self.assertRaisesRegex(ValueError, "each current kit-list line needs"):
            self.reseal_(batch_id, slot=True, presence=[presence_id])
        with self.assertRaisesRegex(ValueError, "needs a fresh presence check"):
            self.reseal_(batch_id, slot=True, counts=[count_id])
        with self.assertRaisesRegex(ValueError, "kit-count check is not current and fresh"):
            self.reseal_(batch_id, slot=True, counts=[other], presence=[presence_id])
        self.assertFalse(SealEvent.objects.exists())

    def test_a_live_count_is_not_fresh_for_a_paper_reseal(self):
        batch_id = self.batch()
        presence_id = self.presence_check(batch_id)
        live = self.count_check()
        with self.assertRaisesRegex(ValueError, "kit-count check is not current and fresh"):
            self.reseal_(batch_id, slot=True, counts=[live], presence=[presence_id])
        self.assertFalse(SealEvent.objects.exists())


@override_settings(**real_collaborator_settings())
class PaperResealApiTests(PaperResealEvidenceCase):
    """POST /api/v1/seals/reseal with paper_entry_batch_id: parsing and refusal mapping."""

    VEHICLE_B_CHECKLIST = True

    def make_org(self):
        self.harness = RoleHttpHarness()
        return self.harness.org

    def setUp(self):
        super().setUp()
        self.client_, self.actor = self.harness.sign_in("crew-http")
        self.client_.get("/")  # obtains the CSRF cookie

    def post(self, batch_id, extra=None, **kwargs):
        body = {
            "operation_id": str(uuid4()),
            **{k: v for k, v in self.reseal_fields(batch_id, **kwargs).items() if v != []},
        }
        return self.client_.post(
            RESEAL,
            data=json.dumps(body | (extra or {})),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.client_.cookies["csrftoken"].value,
        )

    def rejected(self, response, status, code, paths=()):
        self.assertEqual(response.status_code, status, response.content)
        body = response.json()
        self.assertEqual((body["status"], body["code"]), ("REJECTED", code))
        self.assertEqual([e["path"] for e in body["field_errors"]], list(paths))

    def test_a_paper_reseal_names_the_users_own_batch(self):
        batch_id = self.batch()
        response = self.post(batch_id, presence=[self.presence_check(batch_id)])
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body["status"], body["result"]["code"]), ("COMMITTED", "SEAL_RESEALED"))
        self.assertEqual(
            body["result"]["paper_entry"],
            {"batch_id": batch_id, "performed_at": "2026-06-01T05:30:00+00:00"},
        )
        event = SealEvent.objects.get(pk=body["result"]["event_id"])
        self.assertEqual(str(event.actor_user_id), str(self.actor.pk))

    def test_batch_refusals_map_to_their_status(self):
        batch_id = self.batch()
        presence = [self.presence_check(batch_id)]
        # A malformed id is refused like any malformed seal id, before the batch is read.
        self.rejected(self.post("not-a-uuid", presence=presence), 422, "INVALID_SEAL_REQUEST")
        # Another user's batch answers as absent, even one naming this user as performer.
        theirs = self.batch(actor=self.supervisor, performers=[self.actor])
        self.rejected(
            self.post(theirs, presence=presence), 404, "NOT_FOUND", ["paper_entry_batch_id"]
        )
        # An unknown id: its (empty) performers are not this user alone, so the SPEC 3.3
        # others-entry action applies, which crew lacks.
        self.rejected(self.post(str(uuid4()), presence=presence), 403, "FORBIDDEN")
        # The user's own batch for another vehicle.
        elsewhere = self.batch(vehicle=self.vehicle_b, kits=[])
        self.rejected(
            self.post(elsewhere, presence=presence),
            422,
            "PAPER_BATCH_MISMATCH",
            ["paper_entry_batch_id"],
        )
        # Live-only inputs are refused with a batch.
        for extra in (
            {"start_operation_id": str(uuid4())},
            {"nested_substitution_ids": [str(uuid4())]},
        ):
            with self.subTest(extra=list(extra)):
                self.rejected(
                    self.post(batch_id, presence=presence, extra=extra),
                    422,
                    "INVALID_SEAL_REQUEST",
                )
        self.assertFalse(SealEvent.objects.exists())

    def test_paper_reseal_refusals_keep_their_codes(self):
        # A refusal (SealRefusal) answers 422 with its own code: the batch names no printed
        # version of D, so the kit's items cannot be identified (LATE-4).
        unnamed = self.batch(at=time(5, 15), kits=[])
        self.rejected(
            self.post(unnamed, presence=[self.presence_check(unnamed)]),
            422,
            "PAPER_ITEM_UNIDENTIFIABLE",
        )
        batch_id = self.batch()
        presence = [self.presence_check(batch_id)]
        # A missing presence check is an invalid request, as for a live reseal.
        self.rejected(self.post(batch_id), 422, "INVALID_SEAL_REQUEST")
        self.assertFalse(SealEvent.objects.exists())
        self.assertEqual(self.post(batch_id, presence=presence).status_code, 200)
