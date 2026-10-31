"""Administrator invalidation of a correction chain (issue 77 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/77); SPEC INV-1..INV-6, AC-14, AC-15, AC-32)."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from django.db import DatabaseError, connection, transaction

from cya.checks.commands import (
    CheckError,
    SelfInvalidationForbidden,
    correct_check,
    invalidate_chain,
)
from cya.checks.conflicts import CheckConflict
from cya.checks.models import CheckEvent, CheckObligationLink, CheckRecord
from cya.checks.state import CheckDecision, CheckSnapshot, effective_checks
from cya.checks.tests.support import CheckCase
from cya.checks.tests.test_obligation_links import ObligationLinkCleanupMixin
from cya.core.audit_commands import CommandDenied
from cya.core.audit_repository import append_receipt
from cya.core.models import AuditEvent, CommandReceipt
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.state import State
from cya.obligations.tests.support import ObligationCase


class ChainMixin:
    def correct(self, check, tip, *, actor=None, **fields):
        return correct_check(
            check_id=str(check),
            expected_tip_check_id=str(tip),
            outcome="PASSED",
            **self.context(actor or self.crew),
            **fields,
        )

    def invalidate(self, check, *, actor=None, explanation="Synthetic reason.", **context):
        args = self.context(actor or self.admin)
        args.update(context)
        return invalidate_chain(check_id=str(check), explanation=explanation, **args)

    def counts(self):
        return (
            CheckEvent.objects.count(),
            CheckRecord.objects.count(),
            CommandReceipt.objects.count(),
        )

    def effective(self, root_id, as_of=None):
        from django.utils import timezone

        rows = CheckRecord.objects.filter(root_check_id=root_id)
        events = CheckEvent.objects.filter(root_check_id=root_id)
        return effective_checks(
            [
                CheckSnapshot(
                    str(r.pk),
                    str(r.root_check_id),
                    str(r.predecessor_check_id) if r.predecessor_check_id else None,
                    r.recorded_at,
                    r.outcome,
                )
                for r in rows
            ],
            [
                CheckDecision(
                    str(e.root_check_id),
                    str(e.selected_check_id),
                    e.event_type,
                    e.recorded_at,
                    str(e.replacement_check_id) if e.replacement_check_id else None,
                )
                for e in events
            ],
            as_of=as_of or timezone.now(),
        )[0]


class InvalidationTests(ChainMixin, CheckCase):
    def chain(self):
        """A crew member's check corrected twice; returns (root, first correction, tip)."""
        root = self.record().body["check_id"]
        first = self.correct(root, root, explanation="One.").body["check_id"]
        tip = self.correct(root, first, explanation="Two.").body["check_id"]
        return root, first, tip

    def rejected(self, code, status=None, **kwargs):
        before = self.counts()
        with self.assertRaises(CheckError) as caught:
            self.invalidate(**kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        if status is not None:
            self.assertEqual(caught.exception.status, status)
        self.assertEqual(self.counts(), before)
        return caught.exception

    def test_an_administrator_invalidates_another_users_chain_and_nothing_is_modified(self):
        root, first, tip = self.chain()
        before = {r.pk: r for r in CheckRecord.objects.all()}
        result = self.invalidate(first)  # any row of the chain resolves to its root
        self.assertEqual(result.status, 201)
        event = CheckEvent.objects.get(pk=result.body["invalidation_event_id"])
        self.assertEqual(
            (
                event.event_type,
                event.root_check_id,
                event.selected_check_id,
                event.replacement_check_id,
            ),
            ("INVALIDATION", root, tip, None),
        )
        self.assertEqual(event.explanation, "Synthetic reason.")
        self.assertEqual(
            (event.actor_user_id, event.command.command_type), (self.admin.pk, "CHECK_INVALIDATION")
        )
        self.assertIn("ADMINISTRATOR", event.actor_roles_at_time)
        self.assertEqual(result.body["root_check_id"], root)
        self.assertEqual(
            (
                result.body["affected_obligation_ids"],
                result.body["voided_determination_ids"],
                result.body["reopened_deficiency_ids"],
            ),
            ([], [], []),
        )
        after = {r.pk: r for r in CheckRecord.objects.all()}
        self.assertEqual(before.keys(), after.keys())  # AC-15: nothing added, nothing changed
        for pk, row in before.items():
            for field in ("outcome", "recorded_at", "actor_user_id", "predecessor_check_id"):
                self.assertEqual(getattr(row, field), getattr(after[pk], field))

    def test_the_whole_chain_is_excluded_and_no_earlier_outcome_revives(self):
        root, first, tip = self.chain()
        before = self.effective(root)
        self.assertEqual((before.check_id, before.invalidated), (tip, False))
        self.invalidate(root)
        after = self.effective(root)
        self.assertTrue(after.invalidated)
        self.assertEqual(set(after.superseded_ids) | {after.check_id}, {root, first, tip})

    def test_a_later_correction_is_refused_and_the_author_records_a_fresh_check(self):
        root, _first, tip = self.chain()
        self.invalidate(root)
        before = self.counts()
        with self.assertRaises(CheckError) as caught:
            self.correct(root, tip)
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("CHAIN_INVALIDATED", 409)
        )
        self.assertEqual(self.counts(), before)
        fresh = self.record()  # INV-2a: a fresh check, not a correction
        self.assertNotEqual(CheckRecord.objects.get(pk=fresh.body["check_id"]).root_check_id, root)

    def test_a_second_invalidation_is_refused_and_a_replay_returns_the_first_result(self):
        root = self.record().body["check_id"]
        operation_id = str(uuid4())
        first = self.invalidate(root, operation_id=operation_id)
        before = self.counts()
        replay = self.invalidate(root, operation_id=operation_id)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.body, first.body)
        self.assertEqual(self.counts(), before)
        self.rejected("CHAIN_INVALIDATED", 409, check=root)  # a different operation id

    def test_only_administrators_may_invalidate_and_a_refusal_changes_nothing(self):
        root = self.record().body["check_id"]
        self.grant(self.crew, "SUPPLY_OFFICER")
        officer = self.make_user("officer-two")
        self.grant(officer, "SUPPLY_OFFICER")
        for actor in (self.supervisor, officer):
            before = self.counts()
            with self.subTest(actor.idp_subject), self.assertRaises(CommandDenied):
                self.invalidate(root, actor=actor)
            self.assertEqual(self.counts(), before)
            audit = AuditEvent.objects.filter(
                action="CHECK_INVALIDATION", outcome="DENIED", actor_user=actor
            ).last()
            self.assertIsNotNone(audit, actor.idp_subject)
            self.assertNotIn("ADMINISTRATOR", audit.roles_at_time)
        self.assertEqual(CheckEvent.objects.count(), 0)

    def test_an_administrator_cannot_invalidate_their_own_chain_and_the_denial_is_attributed(self):
        own = self.record(actor=self.admin).body["check_id"]
        before = self.counts()
        with self.assertRaises(SelfInvalidationForbidden) as caught:
            self.invalidate(own)
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("SELF_INVALIDATION_FORBIDDEN", 403)
        )
        self.assertEqual(self.counts(), before)
        audit = AuditEvent.objects.get(action="CHECK_INVALIDATION", outcome="DENIED")
        self.assertEqual(audit.actor_user_id, self.admin.pk)  # AUD-1: attributed, not anonymous
        self.assertIn("ADMINISTRATOR", audit.roles_at_time)
        self.assertEqual(audit.reason_code, "FORBIDDEN")
        self.assertEqual(self.correct(own, own, actor=self.admin).status, 201)  # correction works

    def test_a_missing_or_blank_explanation_changes_nothing(self):
        root = self.record().body["check_id"]
        for explanation in (None, "", "   ", 5):
            with self.subTest(explanation=explanation):
                error = self.rejected("EXPLANATION_REQUIRED", check=root, explanation=explanation)
                self.assertEqual((error.status, error.path), (422, "explanation"))
        with self.assertRaises(CheckError) as caught:
            self.invalidate(root, explanation="x" * 5000)
        self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")

    def test_unknown_foreign_and_malformed_targets_are_refused(self):
        self.rejected("NOT_FOUND", 404, check=str(uuid4()))
        for bad in ("not-a-uuid", "", str(uuid4()).upper()):
            with self.subTest(bad), self.assertRaises(CheckError) as caught:
                self.invalidate(bad)
            self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")
        root = self.record().body["check_id"]
        with self.assertRaises(CheckError):
            self.invalidate(root, operation_id="not-a-uuid")
        self.assertEqual(CheckEvent.objects.count(), 0)

    def test_the_database_refuses_an_invalid_invalidation_from_a_writer_that_skips_the_command(
        self,
    ):
        root = self.record().body["check_id"]
        tip = self.correct(root, root).body["check_id"]
        first = CheckRecord.objects.get(pk=root)

        def insert(
            *,
            selected=tip,
            roles=("ADMINISTRATOR",),
            explanation="Why.",
            command_type="CHECK_INVALIDATION",
            replacement=None,
            **extra,
        ):
            with transaction.atomic():
                receipt = append_receipt(
                    organization=self.org,
                    operation_id=str(uuid4()),
                    actor_user=self.admin,
                    command_type=command_type,
                    request_digest="0" * 64,
                    response_status=201,
                    response_body={"code": "SYNTHETIC"},
                    correlation_id=str(uuid4()),
                )
                return CheckEvent.objects.create(
                    id=str(uuid4()),
                    organization_id=self.org.pk,
                    command=receipt,
                    recorded_at=receipt.recorded_at,
                    actor_user=self.admin,
                    actor_roles_at_time=list(roles),
                    root_check_id=root,
                    selected_check_id=selected,
                    event_type="INVALIDATION",
                    replacement_check_id=replacement,
                    explanation=explanation,
                    **extra,
                )

        for label, kwargs in {
            "not the tip": {"selected": first.pk},
            "not an administrator": {"roles": ("CREW",)},
            "no explanation": {"explanation": None},
            "a replacement": {"replacement": tip},
            "another command type": {"command_type": "CHECK_CORRECTION"},
        }.items():
            with self.subTest(label), self.assertRaises(DatabaseError):
                insert(**kwargs)
        insert()  # the valid event is accepted, so the refusals above are meaningful
        with self.assertRaises(DatabaseError):
            insert()  # INV-6: a second invalidation of the same chain

    def test_the_database_refuses_a_correction_of_an_invalidated_chain(self):
        root = self.record().body["check_id"]
        self.invalidate(root)
        tip = CheckRecord.objects.get(pk=root)
        with self.assertRaises(DatabaseError), transaction.atomic():
            receipt = append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=self.crew,
                command_type="CHECK_CORRECTION",
                request_digest="0" * 64,
                response_status=201,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )
            replacement = CheckRecord.objects.create(
                id=str(uuid4()),
                organization_id=self.org.pk,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=self.crew,
                actor_roles_at_time=["CREW"],
                operation_id=receipt.operation_id,
                vehicle_id=tip.vehicle_id,
                item_id=tip.item_id,
                item_def_id=tip.item_def_id,
                checklist_version_id=tip.checklist_version_id,
                outcome="PASSED",
                purpose="ROUTINE",
                root_check_id=root,
                predecessor_check_id=tip.pk,
            )
            CheckEvent.objects.create(
                id=str(uuid4()),
                organization_id=self.org.pk,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=self.crew,
                actor_roles_at_time=["CREW"],
                root_check_id=root,
                selected_check_id=tip.pk,
                event_type="CORRECTION",
                replacement_check=replacement,
            )

    def test_a_concurrent_correction_and_invalidation_never_leave_a_fork_or_a_stale_tip(self):
        root = self.record().body["check_id"]

        def run(job):
            try:
                return job()
            except Exception as error:  # asserted below
                return error
            finally:
                connection.close()

        jobs = [
            lambda: self.correct(root, root),
            lambda: self.invalidate(root),
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result(timeout=120) for future in [pool.submit(run, j) for j in jobs]]
        correction, invalidation = results
        self.assertTrue(
            hasattr(invalidation, "body"), invalidation
        )  # the administrator's wins or follows
        event = CheckEvent.objects.get(event_type="INVALIDATION")
        if hasattr(correction, "body"):
            # The correction committed first, so the invalidation selected the new tip.
            self.assertEqual(str(event.selected_check_id), correction.body["check_id"])
        else:
            self.assertEqual(correction.code, "CHAIN_INVALIDATED")
            self.assertEqual(event.selected_check_id, root)
        self.assertTrue(self.effective(root).invalidated)
        self.assertEqual(CheckEvent.objects.filter(event_type="INVALIDATION").count(), 1)


class InvalidationObligationTests(ObligationLinkCleanupMixin, ChainMixin, ObligationCase):
    def setUp(self):
        super().setUp()
        self.publish_schedule("test-0600", recurrence="DAILY")
        self.version_id = self.published_version(self.base_items())
        self.bind(reconcile=False)
        self.assertEqual(self.generate().failures, [])
        self.other = self.make_user("crew-two")
        self.clock.set(self.at(hours=5, minutes=15))

    def record(self, *, actor=None, **extra):
        from cya.checks.commands import record_check

        return record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.suction.pk),
            checklist_version_id=self.version_id,
            outcome="PASSED",
            purpose="ROUTINE",
            **extra,
            **self.context(actor or self.crew),
        )

    def suction_state(self, instant):
        states = get_vehicle_obligations_as_of(
            str(self.vehicle.pk),
            instant,
            organization_id=str(self.org.pk),
            start=self.at(hours=6),
            end=self.at(hours=6),
        )
        return next(s for s in states if s.obligation.item_id == str(self.suction.pk))

    def test_invalidation_returns_the_obligation_to_unsatisfied_and_history_is_unchanged(self):
        root = self.record().body["check_id"]
        satisfied_at = self.clock.now
        self.assertEqual(self.suction_state(satisfied_at).state, State.SATISFIED)
        links = CheckObligationLink.objects.count()
        self.clock.set(self.at(hours=5, minutes=40))
        result = self.invalidate(root)
        invalidated_at = CheckEvent.objects.get(pk=result.body["invalidation_event_id"]).recorded_at
        obligation_id = CheckObligationLink.objects.get(check_record_id=root).obligation_id
        self.assertEqual(result.body["affected_obligation_ids"], [obligation_id])
        self.assertEqual(CheckObligationLink.objects.count(), links)  # nothing deleted (AC-15)
        # INV-2: unsatisfied again now ...
        self.assertNotEqual(self.suction_state(self.clock.now).state, State.SATISFIED)
        # ... while an as-of query before the invalidation still shows it satisfied (REG-6).
        before = invalidated_at.replace(microsecond=max(invalidated_at.microsecond - 1, 0))
        self.assertEqual(self.suction_state(before).state, State.SATISFIED)

    def test_a_fresh_check_after_invalidation_satisfies_the_obligation_without_a_warning(self):
        root = self.record().body["check_id"]
        self.clock.set(self.at(hours=5, minutes=40))
        self.invalidate(root)
        self.clock.set(self.at(hours=5, minutes=45))
        # Another crew member records without any basis: the invalidated check is not "newest".
        fresh = self.record(actor=self.other)
        self.assertEqual(fresh.status, 201)
        self.assertEqual(len(fresh.body["obligation_links"]), 1)
        self.assertEqual(self.suction_state(self.clock.now).state, State.SATISFIED)

    def test_without_invalidation_the_second_user_is_still_warned(self):
        self.record()
        self.clock.set(self.at(hours=5, minutes=20))
        with self.assertRaises(CheckConflict):
            self.record(actor=self.other)
