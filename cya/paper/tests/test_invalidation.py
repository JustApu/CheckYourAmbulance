"""Paper-entry batch invalidation (SPEC 8.0.0 INV-8, LATE-6, LATE-7, AC-43 (7)-(8); issue #9 B5).

As in test_paper_checks, the 2026-06-02 obligation of the suction item has E = 09:00Z,
G = 11:00Z and L = 23:00Z, and paper is entered on 2026-06-03 after L.
"""

import json
from datetime import time, timedelta
from uuid import uuid4

from django.db import DatabaseError, transaction

from cya.accounts.fields import new_id
from cya.checks.commands import CheckError, SelfInvalidationForbidden, correct_check
from cya.checks.models import CheckEvent, CheckRecord
from cya.core.audit_commands import CommandDenied
from cya.core.audit_repository import append_receipt
from cya.deficiencies.models import DeficiencyCategory, DeficiencyEvent, DeficiencyProjection
from cya.deficiencies.severity import ensure_default_severities
from cya.obligations.models import ObligationProjection
from cya.obligations.state import State
from cya.odometer.models import EngineHoursEntry, OdometerEntry, ReadingEvent
from cya.paper.commands import PaperEntryError
from cya.paper.invalidation import invalidate_paper_entry_batch
from cya.paper.models import PaperEntryBatch, PaperEntryInvalidationEvent
from cya.paper.tests.support import ENTRY, JUNE_2, PaperCheckCase
from cya.paper.tests.test_api import ApiCase
from cya.paper.tests.test_paper_readings import ReadingCase
from cya.paper.tests.test_paper_seals import PaperSealCase
from cya.seals.commands import SealError, SealRefusal
from cya.seals.models import SealSubstitution, SealSubstitutionObligation


class InvalidationCase(ReadingCase):
    def invalidate(self, batch_id, actor=None, explanation="Wrong paper time.", **context):
        args = self.context(actor or self.supervisor)
        args.update(context)
        return invalidate_paper_entry_batch(batch_id=batch_id, explanation=explanation, **args)


class BatchInvalidationTests(InvalidationCase):
    def test_a_supervisor_invalidates_a_crew_batch_and_every_linked_record(self):
        batch_id = self.batch(at=time(6, 15))
        check = self.paper(batch_id).body["check_id"]
        reading = self.reading("40210", batch_id, engine_hours="500")
        before = self.state_at(OdometerEntry.objects.get().recorded_at)
        self.assertEqual(before.state, State.SATISFIED)
        self.clock.set(ENTRY + timedelta(hours=1))
        result = self.invalidate(batch_id)
        self.assertEqual(result.status, 201)
        body = result.body
        self.assertEqual(body["code"], "PAPER_ENTRY_BATCH_INVALIDATED")
        self.assertEqual(body["invalidated_check_root_ids"], [check])
        self.assertEqual(
            sorted(body["invalidated_reading_root_ids"]),
            sorted([reading.body["entry_id"], reading.body["engine_hours_entry_id"]]),
        )
        event = PaperEntryInvalidationEvent.objects.get(pk=body["invalidation_event_id"])
        self.assertEqual((str(event.batch_id), event.actor_user_id), (batch_id, self.supervisor.pk))
        chain_event = CheckEvent.objects.get(root_check_id=check)
        self.assertEqual(
            (chain_event.event_type, chain_event.command.command_type, chain_event.explanation),
            ("INVALIDATION", "PAPER_ENTRY_BATCH_INVALIDATION", "Wrong paper time."),
        )
        self.assertIsNone(chain_event.replacement_paper_entry_batch_id)
        self.assertEqual(ReadingEvent.objects.filter(event_type="INVALIDATION").count(), 2)
        # Nothing is edited: the rows and their two times stay as recorded (INV-8).
        row = CheckRecord.objects.get(pk=check)
        self.assertEqual(row.performed_at, JUNE_2.replace(hour=10, minute=15))
        self.assertTrue(OdometerEntry.objects.filter(pk=reading.body["entry_id"]).exists())
        self.assertTrue(PaperEntryBatch.objects.filter(pk=batch_id).exists())
        # Derived compliance is recomputed without them; as-of history before the event is not.
        self.assertEqual(self.state_at(event.recorded_at).state, State.OVERDUE)
        self.assertEqual(
            self.state_at(event.recorded_at - timedelta(microseconds=1)).state, before.state
        )
        self.assertNotEqual(self.june_2_odometer(event.recorded_at).state.value, "SATISFIED")

    def test_a_replay_returns_the_stored_result_and_a_second_invalidation_is_refused(self):
        batch_id = self.batch()
        self.paper(batch_id)
        context = self.context(self.supervisor)
        first = invalidate_paper_entry_batch(batch_id=batch_id, explanation="Wrong.", **context)
        again = invalidate_paper_entry_batch(batch_id=batch_id, explanation="Wrong.", **context)
        self.assertEqual(again.body, first.body)
        self.assertEqual(PaperEntryInvalidationEvent.objects.count(), 1)
        with self.assertRaises(PaperEntryError) as caught:
            self.invalidate(batch_id, actor=self.admin)
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("BATCH_INVALIDATED", 409)
        )

    def test_an_invalidated_batch_takes_no_further_record(self):
        batch_id = self.batch()
        self.invalidate(batch_id)
        for record in (
            lambda: self.paper(batch_id),
            lambda: self.reading("40210", batch_id),
        ):
            with self.assertRaises(CheckError) as caught:
                record()
            self.assertEqual(
                (caught.exception.code, caught.exception.status), ("PAPER_BATCH_INVALIDATED", 409)
            )
        self.assertFalse(CheckRecord.objects.exists())
        self.assertFalse(OdometerEntry.objects.exists())

    def test_a_chain_an_administrator_already_invalidated_keeps_its_event(self):
        from cya.checks.commands import invalidate_chain

        batch_id = self.batch()
        check = self.paper(batch_id).body["check_id"]
        invalidate_chain(check_id=check, explanation="Admin.", **self.context(self.admin))
        result = self.invalidate(batch_id)
        self.assertEqual(result.body["invalidated_check_root_ids"], [])
        self.assertEqual(CheckEvent.objects.filter(root_check_id=check).count(), 1)

    def test_invalidating_a_failed_check_leaves_its_deficiency_open(self):
        batch_id = self.batch()
        failed = self.failed(batch_id)
        deficiency_id = failed.body["deficiency_ids"][0]
        self.invalidate(batch_id)
        self.assertEqual(
            DeficiencyProjection.objects.get(deficiency_id=deficiency_id).status, "OPEN"
        )  # INV-3

    def test_a_deficiency_its_correction_reported_in_error_reopens(self):
        batch_id = self.batch()
        failed = self.failed(batch_id)
        deficiency_id = failed.body["deficiency_ids"][0]
        correct_check(
            check_id=failed.body["check_id"],
            expected_tip_check_id=failed.body["check_id"],
            outcome="PASSED",
            **self.context(self.crew),
        )
        projection = DeficiencyProjection.objects.get(deficiency_id=deficiency_id)
        self.assertEqual(projection.status, "REPORTED_IN_ERROR")
        result = self.invalidate(batch_id)
        self.assertEqual(result.body["reopened_deficiency_ids"], [deficiency_id])
        last = DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).order_by("-sequence")[0]
        self.assertEqual((last.event_type, last.to_status), ("REOPENED", "OPEN"))  # INV-7

    def test_a_supervisor_completion_the_chain_supported_is_voided(self):
        from cya.conditions.models import ObligationDetermination
        from cya.conditions.uti import complete_unable_obligation

        batch_id = self.batch()
        unable = self.paper(
            batch_id,
            outcome="UNABLE_TO_INSPECT",
            explanation="Synthetic access restriction.",
            deficiency=self.new_deficiency(),
        )
        complete_unable_obligation(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            obligation_id=str(self.june_2_obligation().pk),
            check_id=unable.body["check_id"],
            explanation="Completed by supervisor.",
            operation_id=str(uuid4()),
            correlation_id=str(uuid4()),
            request_attempt_id=str(uuid4()),
        )
        result = self.invalidate(batch_id)
        self.assertEqual(len(result.body["voided_determination_ids"]), 1)
        voided = ObligationDetermination.objects.get(determination_type="INVALIDATED")
        self.assertEqual(voided.command.command_type, "PAPER_ENTRY_BATCH_INVALIDATION")  # UTI-3

    def new_deficiency(self):
        effective_at = self.clock_start - timedelta(days=1)
        ensure_default_severities(self.org.pk, effective_at)
        category = DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=new_id(),
            revision=1,
            label="Synthetic equipment fault",
            supply_related=False,
            effective_at=effective_at,
        )
        return {
            "create": {
                "category_id": str(category.stable_category_id),
                "severity": "HIGH",
                "summary": "Synthetic fault",
                "description": "Synthetic suction fault.",
            }
        }

    def failed(self, batch_id):
        return self.paper(batch_id, outcome="FAILED", deficiency=self.new_deficiency())


class PermissionTests(InvalidationCase):
    def test_the_entry_actor_may_not_invalidate_their_own_batch(self):
        batch_id = self.batch(actor=self.supervisor, performers=[self.crew])
        with self.assertRaises(SelfInvalidationForbidden):
            self.invalidate(batch_id, actor=self.supervisor)
        # Another Supervisor or Administrator must do it.
        self.assertEqual(self.invalidate(batch_id, actor=self.admin).status, 201)

    def test_crew_and_supply_officers_may_not_invalidate(self):
        batch_id = self.batch()
        for actor in (self.crew, self.officer):
            with self.subTest(actor=actor.display_name), self.assertRaises(CommandDenied):
                self.invalidate(batch_id, actor=actor)
        self.assertFalse(PaperEntryInvalidationEvent.objects.exists())

    def test_refusals(self):
        with self.assertRaises(PaperEntryError) as caught:
            self.invalidate(str(uuid4()))
        self.assertEqual((caught.exception.code, caught.exception.status), ("NOT_FOUND", 404))
        batch_id = self.batch()
        for explanation, code in (
            ("  ", "EXPLANATION_REQUIRED"),
            ("x" * 4001, "MALFORMED_REQUEST"),
        ):
            with self.subTest(code=code), self.assertRaises(PaperEntryError) as caught:
                self.invalidate(batch_id, explanation=explanation)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(PaperEntryError) as caught:
            self.invalidate("not-a-uuid")
        self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")


class GuardTests(InvalidationCase):
    def receipt(self, actor, batch_id, command_type="PAPER_ENTRY_BATCH_INVALIDATION"):
        with transaction.atomic():
            return append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=actor,
                command_type=command_type,
                request_digest="0" * 64,
                response_status=201,
                response_body={"code": "SYNTHETIC", "batch_id": batch_id},
                correlation_id=str(uuid4()),
            )

    def insert(self, receipt, actor, batch_id, roles=("SUPERVISOR",)):
        PaperEntryInvalidationEvent.objects.create(
            organization=self.org,
            batch_id=batch_id,
            explanation="Synthetic.",
            command=receipt,
            recorded_at=receipt.recorded_at,
            actor_user=actor,
            actor_roles_at_time=list(roles),
        )

    def test_the_invalidation_needs_every_chain_invalidated_and_another_supervisor(self):
        batch_id = self.batch()
        self.paper(batch_id)
        receipt = self.receipt(self.supervisor, batch_id)
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.insert(receipt, self.supervisor, batch_id)  # a check chain is still open
        own = self.batch(actor=self.supervisor, performers=[self.crew])
        receipt = self.receipt(self.supervisor, own)
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.insert(receipt, self.supervisor, own)  # the entry actor
        empty = self.batch()
        receipt = self.receipt(self.crew, empty)
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.insert(receipt, self.crew, empty, roles=("CREW",))  # no supervisor role
        receipt = self.receipt(self.supervisor, batch_id)
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.insert(receipt, self.supervisor, empty)  # the receipt names another batch
        self.assertFalse(PaperEntryInvalidationEvent.objects.exists())

    def test_a_paper_record_of_an_invalidated_batch_is_refused_by_the_database(self):
        batch_id = self.batch()
        self.paper(batch_id)
        row = CheckRecord.objects.get()

        def insert():
            receipt = self.receipt(self.crew, batch_id, command_type="CHECK_RECORD")
            check_id = str(uuid4())
            with transaction.atomic():
                CheckRecord.objects.create(
                    id=check_id,
                    root_check_id=check_id,
                    organization_id=self.org.pk,
                    command=receipt,
                    recorded_at=receipt.recorded_at,
                    actor_user=self.crew,
                    actor_roles_at_time=["CREW"],
                    operation_id=receipt.operation_id,
                    vehicle=self.vehicle,
                    item=self.suction,
                    item_def_id=row.item_def_id,
                    item_requirement_version_id=row.item_requirement_version_id,
                    checklist_version_id=self.version,
                    outcome="PASSED",
                    purpose="ROUTINE",
                    paper_entry_batch_id=batch_id,
                    performed_at=row.performed_at,
                )

        insert()  # the same row is accepted while the batch is in force
        self.invalidate(batch_id)
        with self.assertRaises(DatabaseError):
            insert()

    def test_engine_hours_alone_are_invalidated_too(self):
        batch_id = self.batch()
        self.reading("40210", batch_id, engine_hours="500")
        result = self.invalidate(batch_id)
        hours = EngineHoursEntry.objects.get()
        self.assertIn(str(hours.pk), result.body["invalidated_reading_root_ids"])


class MistakenNormalTests(PaperCheckCase):
    """INV-8: a check mistakenly saved in Normal mode, after its paper re-entry."""

    def invalidate(self, check_id, batch_id, actor=None, **context):
        from cya.checks.commands import invalidate_chain

        args = self.context(actor or self.supervisor)
        args.update(context)
        return invalidate_chain(
            check_id=check_id,
            explanation="Saved in Normal mode by mistake.",
            replacement_paper_entry_batch_id=batch_id,
            **args,
        )

    def mistaken(self, at=None, actor=None):
        return self.live(actor=actor, at=at or JUNE_2.replace(hour=12)).body["check_id"]

    def refused(self, code, check_id, batch_id, status=422):
        before = CheckEvent.objects.count()
        with self.assertRaises(CheckError) as caught:
            self.invalidate(check_id, batch_id)
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
        self.assertEqual(CheckEvent.objects.count(), before)

    def test_a_supervisor_invalidates_the_mistaken_check_naming_its_replacement(self):
        check_id = self.mistaken()
        batch_id = self.batch(at=time(6, 15))
        self.paper(batch_id, acknowledges_check_id=check_id)
        result = self.invalidate(check_id, batch_id)
        self.assertEqual(result.status, 201)
        self.assertEqual(result.body["replacement_paper_entry_batch_id"], batch_id)
        event = CheckEvent.objects.get(pk=result.body["invalidation_event_id"])
        self.assertEqual(str(event.replacement_paper_entry_batch_id), batch_id)
        self.assertEqual(event.command.command_type, "CHECK_INVALIDATION")

    def test_only_this_supervisor_exception_applies(self):
        check_id = self.mistaken()
        from cya.checks.commands import invalidate_chain

        with self.assertRaises(CommandDenied):  # no replacement: Administrator only (INV-1)
            invalidate_chain(
                check_id=check_id, explanation="Wrong.", **self.context(self.supervisor)
            )
        batch_id = self.batch(at=time(6, 15))
        self.paper(batch_id, acknowledges_check_id=check_id)
        with self.assertRaises(CommandDenied):
            self.invalidate(check_id, batch_id, actor=self.crew)

    def test_the_author_may_not_invalidate_their_own_check(self):
        check_id = self.mistaken(actor=self.supervisor)
        batch_id = self.batch(at=time(6, 15))
        self.paper(batch_id, acknowledges_check_id=check_id)
        with self.assertRaises(SelfInvalidationForbidden):
            self.invalidate(check_id, batch_id, actor=self.supervisor)

    def test_the_replacement_must_hold_the_same_work(self):
        check_id = self.mistaken()
        self.refused("REPLACEMENT_CHECK_MISSING", check_id, self.batch(at=time(6, 15)))
        self.refused("NOT_FOUND", check_id, str(uuid4()), status=404)
        other = self.batch(at=time(6, 15))
        self.paper(other, item=self.masks, quantity=6)
        self.refused("REPLACEMENT_CHECK_MISSING", check_id, other)

    def test_the_replacement_must_not_be_performed_after_the_mistaken_check(self):
        check_id = self.mistaken(at=JUNE_2.replace(hour=9, minute=5))
        batch_id = self.batch(at=time(6, 15))  # 10:15Z, after the 09:05Z save
        self.paper(batch_id, acknowledges_check_id=check_id)
        self.refused("REPLACEMENT_PERFORMED_LATER", check_id, batch_id)

    def test_a_paper_check_or_an_invalidated_replacement_is_refused(self):
        check_id = self.mistaken()
        batch_id = self.batch(at=time(6, 15))
        paper = self.paper(batch_id, acknowledges_check_id=check_id).body["check_id"]
        self.refused("PAPER_CHECK_NOT_NORMAL", paper, batch_id)
        invalidate_paper_entry_batch(
            batch_id=batch_id, explanation="Wrong.", **self.context(self.supervisor)
        )
        self.refused("REPLACEMENT_BATCH_INVALIDATED", check_id, batch_id)

    def test_a_live_invalidation_is_unchanged(self):
        from cya.checks.commands import invalidate_chain

        check_id = self.mistaken()
        result = invalidate_chain(check_id=check_id, explanation="Wrong.", **self.context())
        self.assertNotIn("replacement_paper_entry_batch_id", result.body)
        self.assertIsNone(
            CheckEvent.objects.get(
                pk=result.body["invalidation_event_id"]
            ).replacement_paper_entry_batch_id
        )


class SealInvalidationTests(PaperSealCase):
    """LATE-7: a paper substitution of an invalidated batch supports nothing; a break stays."""

    def invalidate(self, batch_id):
        return invalidate_paper_entry_batch(
            batch_id=batch_id, explanation="Wrong paper time.", **self.context(self.supervisor)
        )

    def test_a_substitution_of_an_invalidated_batch_satisfies_nothing(self):
        batch_id = self.batch()
        substitution = SealSubstitution.objects.get(
            pk=self.verify(batch_id).body["substitution_id"]
        )
        self.assertEqual(self.kit_state(substitution.recorded_at).state, State.SATISFIED)
        self.clock.set(self.at(hours=6, minutes=45))
        body = self.invalidate(batch_id).body
        self.assertEqual(body["voided_substitution_ids"], [str(substitution.pk)])
        link = SealSubstitutionObligation.objects.get()
        self.assertEqual(body["affected_obligation_ids"], [str(link.obligation_id)])
        event = PaperEntryInvalidationEvent.objects.get()
        self.assertNotEqual(self.kit_state(event.recorded_at).state, State.SATISFIED)
        before = self.kit_state(event.recorded_at - timedelta(microseconds=1))
        self.assertEqual(before.state, State.SATISFIED)  # as-of history keeps it until then
        facts = ObligationProjection.objects.get(obligation_id=link.obligation_id).facts
        self.assertIn("voided_at", facts["seal_substitutions"][0])
        # The obligation is open again: a live verification links it.
        self.assertEqual(self.verify().body["link_count"], 1)

    def test_a_paper_break_of_an_invalidated_batch_stays_in_force(self):
        batch_id = self.batch()
        broken = self.break_(batch_id).body["event_id"]
        body = self.invalidate(batch_id).body
        self.assertEqual(body["seal_break_event_ids"], [broken])
        with self.assertRaises(SealError):  # a full inspection and reseal are required first
            self.verify(expected=broken)

    def test_a_voided_substitution_is_no_longer_later_evidence(self):
        first = self.batch(at=time(5, 30))
        self.verify(first)
        with self.assertRaises(SealRefusal):
            self.break_(self.batch(at=time(5, 15)))
        self.invalidate(first)
        self.assertEqual(self.break_(self.batch(at=time(5, 15))).status, 200)

    def test_an_invalidated_batch_takes_no_further_seal_record(self):
        batch_id = self.batch()
        self.invalidate(batch_id)
        with self.assertRaises(CheckError) as caught:
            self.verify(batch_id)
        self.assertEqual(caught.exception.code, "PAPER_BATCH_INVALIDATED")


INVALIDATIONS = "/api/v1/paper-entry-batches/{}/invalidations"


def csrf(client):
    token = client.cookies.get("csrftoken")
    return {"HTTP_X_CSRFTOKEN": token.value} if token is not None else {}


class InvalidationApiTests(ApiCase):
    def invalidation(self, client, batch_id, body=None):
        body = body or {"operation_id": str(uuid4()), "explanation": "Wrong paper time."}
        return client.post(
            INVALIDATIONS.format(batch_id),
            data=json.dumps(body),
            content_type="application/json",
            **csrf(client),
        )

    def crew_batch(self):
        client, crew = self.sign_in("crew-http")
        response = self.post(client, self.body(crew))
        self.assertEqual(response.status_code, 201, response.content)
        return client, response.json()["result"]["batch_id"]

    def test_a_supervisor_invalidates_a_batch(self):
        crew_client, batch_id = self.crew_batch()
        client, _user = self.sign_in("supervisor-http", "SUPERVISOR")
        body = {"operation_id": str(uuid4()), "explanation": "Wrong paper time."}
        response = self.invalidation(client, batch_id, body)
        self.assertEqual(response.status_code, 201, response.content)
        result = response.json()
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(result["result"]["batch_id"], batch_id)
        self.assertNotIn("code", result["result"])
        again = self.invalidation(client, batch_id, body)
        self.assertEqual(again.json()["result"], result["result"])
        self.rejected(self.invalidation(client, batch_id), 409, "BATCH_INVALIDATED")
        self.rejected(self.invalidation(crew_client, batch_id), 403, "FORBIDDEN")

    def test_refusals(self):
        _crew_client, batch_id = self.crew_batch()
        client, _user = self.sign_in("supervisor-http", "SUPERVISOR")
        self.rejected(
            self.invalidation(client, batch_id, {"operation_id": str(uuid4())}),
            400,
            "MALFORMED_REQUEST",
            ["$"],
        )
        self.rejected(
            self.invalidation(client, batch_id, {"operation_id": str(uuid4()), "explanation": " "}),
            422,
            "EXPLANATION_REQUIRED",
            ["explanation"],
        )
        self.rejected(self.invalidation(client, str(uuid4())), 404, "NOT_FOUND", ["batch_id"])
        self.rejected(self.invalidation(self.client, batch_id), 401, "AUTHENTICATION_REQUIRED")

    def test_the_entry_actor_may_not_invalidate_their_own_batch(self):
        client, user = self.sign_in("supervisor-http", "SUPERVISOR")
        response = self.post(client, self.body(user, performer_ids=[str(self.crew.pk)]))
        batch_id = response.json()["result"]["batch_id"]
        self.rejected(self.invalidation(client, batch_id), 403, "SELF_INVALIDATION_FORBIDDEN")

    def test_a_mistaken_normal_invalidation_refusal_is_not_administrator_required(self):
        from cya.checks.commands import record_check

        client, _crew = self.sign_in("crew-http")
        check_id = record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.suction.pk),
            checklist_version_id=self.version,
            outcome="PASSED",
            purpose="ROUTINE",
            **self.context(self.crew),
        ).body["check_id"]
        url = f"/api/v1/checks/{check_id}/invalidations"
        body = {
            "operation_id": str(uuid4()),
            "explanation": "Saved in Normal mode by mistake.",
            "replacement_paper_entry_batch_id": str(uuid4()),
        }
        headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
        response = client.post(
            url, data=json.dumps(body), content_type="application/json", **headers
        )
        self.rejected(response, 403, "FORBIDDEN")
        response = client.post(
            url,
            data=json.dumps({**body, "replacement_paper_entry_batch_id": 7}),
            content_type="application/json",
            **headers,
        )
        self.rejected(response, 400, "MALFORMED_REQUEST", ["replacement_paper_entry_batch_id"])
        del body["replacement_paper_entry_batch_id"]
        response = client.post(
            url, data=json.dumps(body), content_type="application/json", **headers
        )
        self.rejected(response, 403, "ADMINISTRATOR_REQUIRED")
