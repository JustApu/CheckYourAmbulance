"""Paper-entered checks (SPEC 8.0.0 §5.3, SCH-1/2/6, LATE-4 to LATE-6, §19.4; issue #9 Part B2).

The vehicle's own checklist is assigned to a daily 06:00 slot in New York (UTC-4 in June), so
the 2026-06-02 obligation has E = 09:00Z, D = 10:00Z, G = 11:00Z and L = 23:00Z. Paper is
entered on 2026-06-03, after that L.
"""

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

from django.db import DatabaseError, transaction

from cya.attention.facts import latest_effective_checks
from cya.checks.commands import CheckError, correct_check
from cya.checks.conflicts import CheckConflict
from cya.checks.models import CheckObligationLink, CheckRecord
from cya.core.audit_commands import CommandDenied
from cya.obligations.projections import check_unit_key
from cya.obligations.state import State
from cya.paper.tests.support import ENTRY, JUNE_2, PaperCheckCase

L = JUNE_2.replace(hour=23)


class TimingTests(PaperCheckCase):
    def test_a_paper_check_entered_after_l_completes_its_performed_obligation_on_time(self):
        result = self.paper(self.batch(at=time(6, 15)))  # 10:15Z, inside E..G
        obligation = self.june_2_obligation()
        self.assertEqual(
            result.body["obligation_links"],
            [{"obligation_id": str(obligation.pk), "shared_rule_id": None, "timing": "ON_TIME"}],
        )
        row = CheckRecord.objects.get(pk=result.body["check_id"])
        self.assertEqual(row.performed_at, datetime(2026, 6, 2, 10, 15, tzinfo=UTC))
        self.assertGreater(row.recorded_at, L)  # the server entry instant, unchanged
        self.assertEqual(result.body["paper_entry"]["performed_at"], "2026-06-02T10:15:00+00:00")
        # Current compliance follows the performance instant; as-of before entry, the app did
        # not know of it and the obligation was missed (LATE-5, RPT-5a, AC-36 (2)).
        now = self.state_at(row.recorded_at)
        self.assertEqual(now.state, State.SATISFIED)
        self.assertEqual(now.completed_at, row.performed_at)
        self.assertEqual(now.late_seconds, 0)
        before = self.state_at(row.recorded_at - timedelta(microseconds=1))
        self.assertEqual(before.state, State.OVERDUE)

    def test_a_paper_check_after_g_and_by_l_is_a_late_completion(self):
        result = self.paper(self.batch(at=time(7, 30)))  # 11:30Z
        self.assertEqual([link["timing"] for link in result.body["obligation_links"]], ["LATE"])
        state = self.state_at(timezone_now(self))
        self.assertEqual(state.late_seconds, 30 * 60)

    def test_a_paper_check_performed_after_l_does_not_complete_that_obligation(self):
        result = self.paper(self.batch(at=time(19, 30)))  # 23:30Z, after L
        self.assertEqual(result.body["obligation_links"], [])
        self.assertNotEqual(self.state_at(timezone_now(self)).state, State.SATISFIED)

    def test_an_obligation_already_satisfied_by_a_known_check_is_not_linked(self):
        self.live(at=JUNE_2.replace(hour=10, minute=30))
        result = self.paper(self.batch(at=time(6, 10)), acknowledges_check_id=None)
        self.assertEqual(result.body["obligation_links"], [])

    def test_a_live_check_still_uses_its_server_instant(self):
        result = self.live(at=JUNE_2.replace(hour=10, minute=30))
        row = CheckRecord.objects.get(pk=result.body["check_id"])
        self.assertIsNone(row.performed_at)
        self.assertIsNone(row.paper_entry_batch_id)
        self.assertNotIn("paper_entry", result.body)


class PrintedVersionTests(PaperCheckCase):
    def test_a_since_replaced_printed_version_still_governs(self):
        batch_id = self.batch()
        self.publish_own(placements=True)  # a newer version, in force from now
        result = self.paper(batch_id)
        self.assertEqual(len(result.body["obligation_links"]), 1)

    def test_an_item_not_on_the_printed_version_is_refused(self):
        batch_id = self.batch()
        with self.assertRaises(CheckError) as caught:
            self.paper(batch_id, item=self.gauze_item())
        self.assertEqual(caught.exception.code, "ITEM_NOT_IN_CHECKLIST")

    def gauze_item(self):
        from cya.checklists.models import Item

        return Item.objects.create(organization=self.org, label="Synthetic gauze (not listed)")


class RefusalTests(PaperCheckCase):
    def refused(self, code, batch_id, **fields):
        before = CheckRecord.objects.count()
        with self.assertRaises(CheckError) as caught:
            self.paper(batch_id, **fields)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(CheckRecord.objects.count(), before)

    def test_another_users_batch_answers_as_absent(self):
        batch_id = self.batch(actor=self.supervisor, performers=[self.crew])
        with self.assertRaises(CheckError) as caught:
            self.paper(batch_id, actor=self.crew)
        self.assertEqual((caught.exception.code, caught.exception.status), ("NOT_FOUND", 404))

    def test_a_check_of_another_version_or_vehicle_is_refused(self):
        batch_id = self.batch()
        self.refused("PAPER_VERSION_MISMATCH", batch_id, checklist_version_id=str(uuid4()))
        self.refused("PAPER_BATCH_MISMATCH", batch_id, vehicle_id=str(self.vehicles[1].pk))

    def test_paper_takes_no_session_and_only_routine_checks(self):
        batch_id = self.batch()
        self.refused("MALFORMED_REQUEST", batch_id, session_id=str(uuid4()))
        self.refused(
            "MALFORMED_REQUEST",
            batch_id,
            purpose="VERIFICATION",
            verification_for_deficiency_id=str(uuid4()),
        )

    def test_roles_are_checked_again_for_every_record(self):
        batch_id = self.batch(actor=self.supervisor, performers=[self.crew])
        self.grant_revoke(self.supervisor, "SUPERVISOR", "REVOKE")
        with self.assertRaises(CommandDenied):
            self.paper(batch_id, actor=self.supervisor)

    def grant_revoke(self, user, role, action):
        from cya.authorization.commands import change_role
        from cya.checklists.tests.support import ids

        change_role(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            target_id=str(user.pk),
            role=role,
            action=action,
            **ids(),
        )


class ConflictTests(PaperCheckCase):
    def test_a_check_linked_to_the_same_obligation_warns_and_proceeding_keeps_both(self):
        newer = self.live(actor=self.admin, at=JUNE_2.replace(hour=10, minute=30))
        batch_id = self.batch(at=time(6, 10))
        with self.assertRaises(CheckConflict) as caught:
            self.paper(batch_id)
        self.assertEqual(caught.exception.newest["check_id"], newer.body["check_id"])
        self.assertEqual(caught.exception.obligation_id, str(self.june_2_obligation().pk))
        committed = self.paper(batch_id, acknowledges_check_id=newer.body["check_id"])
        self.assertEqual(committed.status, 201)
        self.assertEqual(CheckRecord.objects.filter(item=self.suction).count(), 2)

    def test_a_check_by_an_attributed_performer_is_not_a_conflict(self):
        self.live(actor=self.crew, at=JUNE_2.replace(hour=10, minute=30))
        batch_id = self.batch(actor=self.supervisor, performers=[self.crew])
        self.assertEqual(self.paper(batch_id, actor=self.supervisor).status, 201)

    def test_the_entry_actor_alone_does_not_count_as_a_performer(self):
        self.live(actor=self.supervisor, at=JUNE_2.replace(hour=10, minute=30))
        batch_id = self.batch(actor=self.supervisor, performers=[self.crew])
        with self.assertRaises(CheckConflict):
            self.paper(batch_id, actor=self.supervisor)

    def test_a_check_for_another_obligation_of_the_item_does_not_warn(self):
        self.live(actor=self.admin, at=JUNE_2.replace(hour=10, minute=30) + timedelta(days=1))
        self.assertEqual(self.paper(self.batch(at=time(6, 10))).status, 201)


class OrderingTests(PaperCheckCase):
    def test_a_paper_check_performed_earlier_does_not_become_the_current_check(self):
        live = self.live(at=JUNE_2.replace(hour=10, minute=30))
        paper = self.paper(self.batch(at=time(6, 10)), acknowledges_check_id=live.body["check_id"])
        row = CheckRecord.objects.get(pk=paper.body["check_id"])
        key = (str(self.suction.pk), check_unit_key(row, {}))
        latest = latest_effective_checks(
            str(self.org.pk), str(self.vehicle.pk), [key], row.recorded_at
        )
        self.assertEqual([f.check_id for f in latest], [live.body["check_id"]])

    def test_a_paper_check_performed_later_becomes_current(self):
        live = self.live(at=JUNE_2.replace(hour=9, minute=5))
        self.clock.set(ENTRY)
        paper = self.paper(self.batch(at=time(7, 30)), acknowledges_check_id=live.body["check_id"])
        row = CheckRecord.objects.get(pk=paper.body["check_id"])
        key = (str(self.suction.pk), check_unit_key(row, {}))
        latest = latest_effective_checks(
            str(self.org.pk), str(self.vehicle.pk), [key], row.recorded_at
        )
        self.assertEqual([f.check_id for f in latest], [paper.body["check_id"]])
        before = latest_effective_checks(
            str(self.org.pk),
            str(self.vehicle.pk),
            [key],
            row.recorded_at - timedelta(microseconds=1),
        )
        self.assertEqual([f.check_id for f in before], [live.body["check_id"]])


class CorrectionTests(PaperCheckCase):
    def test_a_correction_keeps_the_batch_and_the_performance_instant(self):
        first = self.paper(self.batch(at=time(6, 15)), item=self.masks, quantity=6)
        root = CheckRecord.objects.get(pk=first.body["check_id"])
        corrected = correct_check(
            check_id=str(root.pk),
            expected_tip_check_id=str(root.pk),
            outcome="PASSED",
            quantity=6,
            **self.context(self.crew),
        )
        row = CheckRecord.objects.get(pk=corrected.body["check_id"])
        self.assertEqual(
            (row.paper_entry_batch_id, row.performed_at),
            (root.paper_entry_batch_id, root.performed_at),
        )
        self.assertEqual(
            list(
                CheckObligationLink.objects.filter(check_record=row).values_list(
                    "obligation_id", flat=True
                )
            ),
            list(
                CheckObligationLink.objects.filter(check_record=root).values_list(
                    "obligation_id", flat=True
                )
            ),
        )


class GuardTests(PaperCheckCase):
    def test_a_paper_row_must_repeat_its_batch_performance_instant(self):
        result = self.paper(self.batch())
        row = CheckRecord.objects.get(pk=result.body["check_id"])
        from cya.core.audit_repository import append_receipt

        with transaction.atomic():
            receipt = append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=self.crew,
                command_type="CHECK_RECORD",
                request_digest="0" * 64,
                response_status=201,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )
        fields = {
            "organization_id": self.org.pk,
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": self.crew,
            "actor_roles_at_time": ["CREW"],
            "operation_id": receipt.operation_id,
            "vehicle": self.vehicle,
            "item": self.suction,
            "item_def_id": row.item_def_id,
            "item_requirement_version_id": row.item_requirement_version_id,
            "checklist_version_id": self.version,
            "outcome": "PASSED",
            "purpose": "ROUTINE",
        }
        for overrides in (
            {"paper_entry_batch_id": row.paper_entry_batch_id},  # no performance instant
            {"performed_at": row.performed_at},  # no batch
            {
                "paper_entry_batch_id": row.paper_entry_batch_id,
                "performed_at": row.performed_at - timedelta(minutes=1),
            },
        ):
            check_id = str(uuid4())
            with self.subTest(overrides=list(overrides)):
                with self.assertRaises(DatabaseError), transaction.atomic():
                    CheckRecord.objects.create(
                        id=check_id, root_check_id=check_id, **fields, **overrides
                    )


def timezone_now(case):
    return case.clock.now
