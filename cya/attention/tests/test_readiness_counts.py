"""RPT-5a fleet-readiness Overdue and Missed counts (SPEC 1.29.0/1.30.0, AC-36; issue #80).

Vehicle A follows a daily 06:00 schedule (early 05:00, grace G 07:00, pinned late limit L 19:00).
Nothing is checked in setUp, so every item's 06:00 obligation is Overdue after G and missed
after L. Each test changes one thing and compares counts before and after, read through both
the current (stored facts) and the as-of (event replay) path, which must agree.
"""

from datetime import timedelta

from cya.attention.readiness import fleet_readiness
from cya.attention.tests.support import AttentionCase
from cya.checks.commands import invalidate_chain
from cya.conditions.uti import complete_unable_obligation, invalidate_supervisor_completion
from cya.obligations.adhoc import request_inspection

US = timedelta(microseconds=1)


class Rpt5aCountTests(AttentionCase):
    def counts(self, at, as_of=None):
        """Overdue, Missed and the fleet totals of vehicle A at `at` (or as of `as_of`)."""
        org = str(self.org.pk)
        picture = fleet_readiness(org, now=at, as_of=as_of)
        if as_of is None:
            # The current path (stored facts) must equal the event replay at the same instant.
            self.assertEqual(picture, fleet_readiness(org, now=at, as_of=at))
        mine = next(v for v in picture.vehicles if v.vehicle_id == str(self.vehicle.pk))
        for code in ("OVERDUE", "MISSED"):
            self.assertEqual(
                picture.totals[code], sum(v.counts[code] for v in picture.vehicles), code
            )
        return mine.counts["OVERDUE"], mine.counts["MISSED"]

    def order(self, due_at, item=None, kit_id=None):
        return request_inspection(
            vehicle_id=str(self.vehicle.pk),
            due_at=due_at,
            items=[{"item_id": str((item or self.suction).pk), "kit_instance_id": kit_id}],
            explanation="Synthetic explicit order.",
            **self.context(self.supervisor),
        )

    def test_a_later_pass_clears_overdue_and_keeps_missed(self):  # AC-36 (1), (2)
        before = self.counts(self.at(days=1, hours=5, minutes=29))
        self.assertGreater(before[1], 0)  # every unchecked day-0 obligation is missed
        self.clock.set(self.at(days=1, hours=5, minutes=30))
        self.record(self.suction)
        self.assertEqual(
            self.counts(self.at(days=1, hours=5, minutes=31)), (before[0] - 1, before[1])
        )

    def test_a_later_fail_clears_overdue_but_the_item_stays_failed(self):  # AC-36 (3)
        before = self.counts(self.at(days=1, hours=5, minutes=29))
        self.clock.set(self.at(days=1, hours=5, minutes=30))
        self.failed(self.suction)
        at = self.at(days=1, hours=5, minutes=31)
        self.assertEqual(self.counts(at), (before[0] - 1, before[1]))
        picture = fleet_readiness(str(self.org.pk), now=at)
        mine = next(v for v in picture.vehicles if v.vehicle_id == str(self.vehicle.pk))
        self.assertEqual(mine.counts["FAILED"], 1)

    def test_after_g_before_l_overdue_clears_and_missed_starts_after_l(self):  # AC-36 (4), (9)
        self.clock.set(self.at(hours=8))
        self.order(self.at(hours=10))
        before = self.counts(self.at(hours=8, minutes=4))
        self.assertEqual(before[1], 0)
        self.clock.set(self.at(hours=8, minutes=5))
        self.record(self.suction)  # links to the order, whose on-time window holds it (SCH-2)
        self.assertEqual(self.counts(self.at(hours=8, minutes=10)), (before[0] - 1, 0))
        limit = self.at(hours=19)
        self.assertEqual(self.counts(limit - US)[1], 0)
        self.assertEqual(self.counts(limit)[1], 0)  # at exactly L it is not yet missed
        overdue, missed = self.counts(limit + US)
        self.assertEqual(missed, overdue + 1)  # the cleared one is missed but not Overdue

    def test_the_seven_day_edge_is_inclusive_at_the_due_instant(self):  # AC-36 (5)
        for day in range(1, 8):
            self.clock.set(self.at(days=day))
            self.assertEqual(self.generate().failures, [])
        per_day = self.counts(self.at(hours=19) + US)[1]
        edge = self.at(days=7, hours=6)  # day 0's due instant is exactly seven days earlier
        self.clock.set(edge)
        self.assertEqual(self.counts(edge)[1] - self.counts(edge + US)[1], per_day)

    def test_only_the_same_physical_kit_instance_clears(self):
        before = self.counts(self.at(days=1, hours=5, minutes=29))
        self.clock.set(self.at(days=1, hours=5, minutes=30))
        self.record(self.kit_item, kit_id=self.kit_b)
        self.assertEqual(self.counts(self.at(days=1, hours=5, minutes=31))[0], before[0] - 1)

    def test_retiring_the_checked_later_obligation_keeps_the_clearing(self):
        before = self.counts(self.at(days=1, hours=5, minutes=29))
        self.clock.set(self.at(days=1, hours=5, minutes=30))
        self.record(self.kit_item, kit_id=self.kit_b)
        self.clock.set(self.at(days=1, hours=5, minutes=40))
        self.deactivate(self.kit_b)  # retires kit B's 06:00 obligation, already checked
        self.assertEqual(self.counts(self.at(days=1, hours=5, minutes=45))[0], before[0] - 1)

    def unable_completed(self):
        self.clock.set(self.at(days=1, hours=5, minutes=30))
        body = self.failed(
            self.kit_item, kit_id=self.kit_b, outcome="UNABLE_TO_INSPECT", explanation="Synthetic."
        ).body
        self.clock.set(self.at(days=1, hours=5, minutes=35))
        return complete_unable_obligation(
            obligation_id=body["obligation_links"][0]["obligation_id"],
            check_id=body["check_id"],
            explanation="Synthetic supervisor decision.",
            **self.context(self.supervisor),
        )

    def test_a_supervisor_completion_survives_retirement(self):
        before = self.counts(self.at(days=1, hours=5, minutes=29))
        self.unable_completed()
        self.clock.set(self.at(days=1, hours=5, minutes=40))
        self.deactivate(self.kit_b)
        self.assertEqual(self.counts(self.at(days=1, hours=5, minutes=45))[0], before[0] - 1)

    def test_a_withdrawn_completion_does_not_clear_even_after_retirement(self):
        before = self.counts(self.at(days=1, hours=5, minutes=29))
        completion = self.unable_completed()
        self.clock.set(self.at(days=1, hours=5, minutes=37))
        invalidate_supervisor_completion(
            determination_id=str(completion.pk),
            explanation="Synthetic withdrawal.",
            **self.context(self.admin),
        )
        self.clock.set(self.at(days=1, hours=5, minutes=40))
        self.deactivate(self.kit_b)
        self.assertEqual(self.counts(self.at(days=1, hours=5, minutes=45))[0], before[0])

    def test_a_check_on_an_order_due_beyond_the_window_clears(self):
        before = self.counts(self.at(days=1))
        self.clock.set(self.at(days=1, minutes=1))
        self.order(self.at(days=4, hours=6))  # due 78 h ahead, outside the readiness window
        self.clock.set(self.at(days=1, minutes=5))
        self.record(self.suction)
        self.assertEqual(self.counts(self.at(days=1, minutes=10))[0], before[0] - 1)

    def test_a_check_linked_to_nothing_does_not_clear(self):
        before = self.counts(self.at(days=1, hours=4, minutes=58))
        self.clock.set(self.at(days=1, hours=4, minutes=59))  # before the 05:00 early window
        self.assertEqual(self.record(self.suction).body["obligation_links"], [])
        self.assertEqual(
            self.counts(
                self.at(
                    days=1,
                    hours=4,
                    minutes=59,
                )
            ),
            before,
        )

    def test_as_of_follows_the_record_as_known_then(self):  # AC-36 (6)
        self.clock.set(self.at(days=1, hours=5, minutes=30))
        check = self.record(self.suction).body["check_id"]
        known = self.counts(self.at(days=1, hours=5, minutes=40))
        self.clock.set(self.at(days=1, hours=5, minutes=50))
        invalidate_chain(check_id=check, explanation="Synthetic.", **self.context(self.admin))
        now = self.at(days=1, hours=5, minutes=55)
        self.assertEqual(self.counts(now, as_of=self.at(days=1, hours=5, minutes=40)), known)
        self.assertEqual(self.counts(now)[0], known[0] + 1)  # the current picture reflects it

    def test_newer_unsatisfied_candidates_do_not_hide_an_older_qualifying_one(self):
        # PR #96 review: each key is searched newest first in rounds; four newer, unsatisfied
        # later obligations (days 2 to 5) come before day 1's qualifying one, which must still
        # clear day 0, and with through both the visible-state and the facts path. Invalidating day 1's check must
        # remove the clearing.
        from cya.attention import readiness_counts
        from cya.obligations.current import current_rows, evaluate_facts
        from cya.obligations.state import State

        self.clock.set(self.at(days=1, hours=5, minutes=30))
        check = self.record(self.suction).body["check_id"]
        for day in range(2, 6):
            self.clock.set(self.at(days=day))
            self.assertEqual(self.generate().failures, [])
        at = self.at(days=5, hours=8)
        self.clock.set(at)
        org, vehicle = str(self.org.pk), str(self.vehicle.pk)

        def clearing(use_visible=True):
            states = [evaluate_facts(f, at) for f in current_rows(org, [vehicle], at)[vehicle]]
            overdue = [s.obligation for s in states if s.state == State.OVERDUE]
            visible = {s.obligation.id: s for s in states} if use_visible else None
            result = readiness_counts.latest_clearing_due(
                org, at, overdue, current=True, visible=visible
            )
            return result.get(
                (vehicle, str(self.suction.pk), "00000000-0000-0000-0000-000000000000")
            )

        self.assertEqual(clearing(), self.at(days=1, hours=6))  # decided from visible states
        self.assertEqual(clearing(use_visible=False), self.at(days=1, hours=6))  # facts path
        self.clock.set(self.at(days=5, hours=8, minutes=1))
        invalidate_chain(check_id=check, explanation="Synthetic.", **self.context(self.admin))
        at = self.at(days=5, hours=8, minutes=2)
        self.clock.set(at)
        self.assertIsNone(clearing())

    def test_a_checked_later_obligation_shown_as_exempt_still_clears(self):
        # The later obligation is checked at 05:30, then the vehicle goes out of service at 05:40,
        # before its 06:00 due instant: it displays as Exempt, which takes precedence over its
        # satisfaction, yet its effective linked check still clears day 0 (issue #80, revision 5).
        from cya.operations.commands import set_vehicle_status

        before = self.counts(self.at(days=1, hours=5, minutes=29))
        self.clock.set(self.at(days=1, hours=5, minutes=30))
        self.record(self.suction)
        self.clock.set(self.at(days=1, hours=5, minutes=40))
        set_vehicle_status(
            vehicle_id=str(self.vehicle.pk),
            status="OUT_OF_SERVICE",
            explanation="Synthetic reason.",
            **self.context(self.supervisor),
        )
        at = self.at(days=1, hours=6, minutes=30)
        picture = fleet_readiness(str(self.org.pk), now=at)
        mine = next(v for v in picture.vehicles if v.vehicle_id == str(self.vehicle.pk))
        self.assertGreater(mine.counts["EXEMPT"], 0)
        self.assertEqual(self.counts(at)[0], before[0] - 1)

    def test_every_hiding_display_state_is_checked_against_its_facts(self):
        # PR #96 review: the visible-state shortcut must equal the satisfaction rule. A later
        # obligation whose display state is Exempt, Waived or Suspended is judged from its facts:
        # with a qualifying check it clears day 0, and without one it does not.
        from dataclasses import replace

        from cya.attention import readiness_counts
        from cya.obligations.current import current_rows, evaluate_facts
        from cya.obligations.state import State

        self.clock.set(self.at(days=1, hours=5, minutes=30))
        self.record(self.suction)
        self.clock.set(self.at(days=2))
        self.assertEqual(self.generate().failures, [])
        at = self.at(days=2, hours=8)
        self.clock.set(at)
        org, vehicle, item = str(self.org.pk), str(self.vehicle.pk), str(self.suction.pk)
        states = [evaluate_facts(f, at) for f in current_rows(org, [vehicle], at)[vehicle]]
        suction = {s.obligation.due_at: s for s in states if s.obligation.item_id == item}
        checked, unchecked = suction[self.at(days=1, hours=6)], suction[self.at(days=2, hours=6)]
        self.assertEqual((checked.state, unchecked.state), (State.SATISFIED, State.OVERDUE))
        overdue = [s.obligation for s in states if s.state == State.OVERDUE]
        key = (vehicle, item, "00000000-0000-0000-0000-000000000000")

        def clearing(shown, hidden_state):
            visible = {s.obligation.id: s for s in states}
            visible[shown.obligation.id] = replace(shown, state=hidden_state)
            if shown is checked:  # the later obligation must not also be decided by day 2
                visible[unchecked.obligation.id] = replace(unchecked, state=hidden_state)
            return readiness_counts.latest_clearing_due(
                org, at, overdue, current=True, visible=visible
            ).get(key)

        for hidden in (State.EXEMPT, State.WAIVED, State.SUSPENDED):
            with self.subTest(hidden=hidden.value):
                self.assertEqual(clearing(checked, hidden), self.at(days=1, hours=6))
                self.assertEqual(clearing(unchecked, hidden), self.at(days=1, hours=6))

    def test_a_corrected_later_check_still_clears(self):
        from cya.checks.commands import correct_check

        before = self.counts(self.at(days=1, hours=5, minutes=29))
        self.clock.set(self.at(days=1, hours=5, minutes=30))
        check = self.failed(self.suction).body["check_id"]
        self.clock.set(self.at(days=1, hours=5, minutes=35))
        correct_check(
            check_id=check, expected_tip_check_id=check, outcome="PASSED", **self.context(self.crew)
        )
        self.assertEqual(self.counts(self.at(days=1, hours=5, minutes=40))[0], before[0] - 1)
