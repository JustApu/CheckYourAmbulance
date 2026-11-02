"""Fleet readiness (issue 17): RPT-7 states kept apart, deficiency facts, bounds and as-of."""

import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest import mock, skipUnless

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from cya.attention import readiness
from cya.attention.tests.support import AttentionCase
from cya.checks.commands import correct_check, invalidate_chain
from cya.deficiencies import acknowledgement, service
from cya.deficiencies.commands import assign_deficiency, regrade_severity, set_deadline
from cya.deficiencies.dependency import enter_dependency
from cya.deficiencies.models import DeficiencyEvent
from cya.deficiencies.suspension import suspend_checks
from cya.inspections.tests.test_crew_ui import CrewUiCase
from cya.obligations.state import ObligationState, State
from cya.operations.commands import set_vehicle_status


class ReadinessCase(AttentionCase):
    def picture(self, **kw):
        return readiness.fleet_readiness(str(self.org.pk), now=self.clock.now, **kw)

    def mine(self, picture=None):
        picture = picture or self.picture()
        return next(v for v in picture.vehicles if v.vehicle_id == str(self.vehicle.pk))

    def latest(self, pk):
        return str(DeficiencyEvent.objects.filter(deficiency_id=pk).latest("sequence").pk)

    def into_window(self):
        self.clock.set(self.clock.now + timedelta(hours=5))  # inside the 06:00 window


class StateTests(ReadinessCase):
    def test_a_fresh_fleet_shows_only_open_states(self):
        vehicle = self.mine()
        self.assertEqual(vehicle.status, "IN_SERVICE")
        self.assertGreater(vehicle.counts["UPCOMING"] + vehicle.counts["CURRENT"], 0)
        open_states = {"OVERDUE", "DUE", "CURRENT", "UPCOMING"}
        recorded = {k for k, _label in readiness.CATEGORIES if k not in open_states}
        self.assertTrue(all(vehicle.counts[k] == 0 for k in recorded))
        self.assertEqual(vehicle.deficiencies, [])
        self.assertEqual(vehicle.suspensions, [])

    def test_recorded_outcomes_are_counted_apart_never_as_one_flag(self):
        self.into_window()
        self.record(self.suction)  # Passed
        self.failed(self.masks, quantity=1)  # Failed, with a deficiency
        self.failed(
            self.kit_item,
            kit_id=self.reference,
            outcome="UNABLE_TO_INSPECT",
            explanation="Synthetic reason.",
        )
        vehicle = self.mine()
        self.assertEqual(vehicle.counts["PASSED"], 1)
        self.assertEqual(vehicle.counts["FAILED"], 1)
        self.assertEqual(vehicle.counts["UNABLE"], 1)  # still owed, with its Unable check
        self.assertEqual(vehicle.progress["checked"], 2)
        self.assertEqual(len(vehicle.deficiencies), 2)  # the Failed and the Unable reports
        self.assertNotIn("compliant", json.dumps(vehicle.counts).lower())

    def test_invalidated_and_superseded_checks_are_counted(self):
        self.into_window()
        bad = self.record(self.masks, quantity=6).body["check_id"]
        invalidate_chain(check_id=bad, explanation="Synthetic reason.", **self.context(self.admin))
        wrong = self.failed(self.suction).body["check_id"]
        correct_check(
            check_id=wrong,
            expected_tip_check_id=wrong,
            outcome="PASSED",
            **self.context(self.crew),
        )  # the author's correction replaces the Failed entry, which is superseded
        vehicle = self.mine()
        self.assertEqual(vehicle.counts["INVALIDATED"], 1)
        self.assertEqual(vehicle.counts["SUPERSEDED"], 1)
        self.assertEqual(vehicle.counts["PASSED"], 1)  # the corrected suction check
        self.assertEqual(vehicle.counts["FAILED"], 0)

    def test_suspended_and_exempt_are_their_own_states(self):
        self.into_window()
        pk = self.failed(self.suction).body["deficiency_ids"][0]
        suspend_checks(
            deficiency_id=pk,
            expected_event_id=self.latest(pk),
            items=[{"item_id": str(self.suction.pk)}],
            **self.context(self.supervisor),
        )
        vehicle = self.mine()
        self.assertEqual(len(vehicle.suspensions), 1)
        self.assertEqual(vehicle.suspensions[0]["label"], self.suction.label)
        # Today's suction obligation keeps its Failed check (recorded before the suspension);
        # tomorrow's, inside the window, is Suspended.
        self.assertEqual((vehicle.counts["FAILED"], vehicle.counts["SUSPENDED"]), (1, 1))
        set_vehicle_status(
            vehicle_id=str(self.vehicle.pk),
            status="OUT_OF_SERVICE",
            explanation="Synthetic reason.",
            **self.context(self.supervisor),
        )
        self.clock.set(self.clock.now + timedelta(days=1))
        vehicle = self.mine()
        self.assertEqual(vehicle.status, "OUT_OF_SERVICE")
        self.assertGreater(vehicle.counts["EXEMPT"], 0)

    def test_every_rpt7_category_has_its_own_count(self):
        # States that need rules or late checks are fed in directly; the counting is the same code.
        base = self.mine()  # a real history to hang the synthetic states on
        self.assertTrue(base)
        obligation = mock.Mock(id="o", due_at=self.clock.now)
        states = [
            ObligationState(obligation, State.SATISFIED, "c1", "NOT_APPLICABLE"),
            ObligationState(obligation, State.SATISFIED, "d1", "SUPERVISOR_COMPLETED"),
            ObligationState(obligation, State.SATISFIED, "c2", "PASSED", None, 3600),
            ObligationState(obligation, State.WAIVED, "w1"),
            ObligationState(obligation, State.WAIVED, "w2"),
            ObligationState(obligation, State.EXEMPT, "s1"),
            ObligationState(obligation, State.SUSPENDED, "x1"),
            ObligationState(obligation, State.OVERDUE),
        ]
        history = mock.Mock(
            determinations=[
                mock.Mock(id="w1", rule=mock.Mock(id="carry"), obligation_id="o"),
                mock.Mock(id="w2", rule=mock.Mock(id="seal"), obligation_id="o"),
            ],
            check_records=(),
            check_decisions=(),
            check_links=(),
        )
        for d in history.determinations:
            d.determination_type = "WAIVED"
            d.recorded_at = self.clock.now
        with (
            mock.patch.object(readiness, "load_projection_history", return_value=history),
            mock.patch.object(readiness, "rebuild_vehicle_projection", return_value=states),
            mock.patch.object(readiness, "replay_checks", return_value=((), ())),
            mock.patch.object(readiness, "_carry_over_rules", return_value={"carry"}),
        ):
            counts, progress = readiness._obligations(
                str(self.org.pk), str(self.vehicle.pk), self.clock.now
            )
        expected = {
            "NOT_APPLICABLE": 1,
            "UNABLE_COMPLETED": 1,
            "PASSED": 1,
            "COMPLETED_LATE": 1,
            "CARRIED_OVER": 1,
            "WAIVED": 1,
            "EXEMPT": 1,
            "SUSPENDED": 1,
            "OVERDUE": 1,
        }
        self.assertEqual({k: v for k, v in counts.items() if v}, expected)
        self.assertEqual(progress, {"checked": 3, "owed": 4})


class DeficiencyFactTests(ReadinessCase):
    def test_overdue_dependency_acknowledgement_and_age_are_shown(self):
        pk = self.failed(self.suction).body["deficiency_ids"][0]
        assign_deficiency(
            deficiency_id=pk,
            assignee_id=str(self.crew.pk),
            expected_event_id=self.latest(pk),
            **self.context(self.supervisor),
        )
        set_deadline(
            deficiency_id=pk,
            deadline=self.clock.now + timedelta(hours=1),
            expected_event_id=self.latest(pk),
            **self.context(self.supervisor),
        )
        follow_up = self.clock.now + timedelta(days=3)
        enter_dependency(
            deficiency_id=pk,
            explanation="Synthetic vendor.",
            follow_up_at=follow_up,
            expected_event_id=self.latest(pk),
            **self.context(self.supervisor),
        )
        acknowledgement.set_acknowledgement_maximum(maximum_days=3, **self.context(self.admin))
        acknowledgement.acknowledge_escalation(
            deficiency_id=pk,
            explanation="Synthetic review.",
            action_plan="Synthetic plan.",
            period_days=1,
            expected_event_id=self.latest(pk),
            **self.context(self.supervisor),
        )
        self.clock.set(self.clock.now + timedelta(hours=2))
        row = self.mine().deficiencies[0]
        self.assertEqual(row["status"], "AWAITING_EXTERNAL_DEPENDENCY")
        self.assertTrue(row["overdue"])
        self.assertTrue(row["awaiting_dependency"])
        self.assertEqual(row["follow_up_at"], follow_up)
        self.assertIsNotNone(row["acknowledged_until"])
        self.assertEqual(row["severity"]["code"], "HIGH")
        self.assertEqual(self.mine().by_status, {"AWAITING_EXTERNAL_DEPENDENCY": 1})
        # Once the acknowledgement expires it is no longer shown as active.
        self.clock.set(self.clock.now + timedelta(days=2))
        self.assertIsNone(self.mine().deficiencies[0]["acknowledged_until"])


class FanOutTests(ReadinessCase):
    """PR 34 review: the deficiency list is bounded in the database and facts are read in bulk."""

    def queries(self):
        with CaptureQueriesContext(connection) as captured:
            self.picture()
        return captured.captured_queries

    def test_the_deficiency_list_query_carries_the_limit(self):
        for item in (self.suction, self.masks, self.siren):
            self.failed(item, **({"quantity": 1} if item == self.masks else {}))
        with mock.patch.object(readiness, "DEFICIENCY_LIMIT", 1):
            sql = [
                q["sql"]
                for q in self.queries()
                if "`deficiency`" in q["sql"] and "status_then" in q["sql"]
            ]
            vehicle = self.mine()
        self.assertTrue(sql)
        self.assertTrue(all("LIMIT 2" in q for q in sql), sql)
        self.assertEqual((len(vehicle.deficiencies), vehicle.deficiencies_truncated), (1, True))

    def test_queries_do_not_grow_with_the_number_of_deficiencies(self):
        self.failed(self.suction)
        one = len(self.queries())
        self.failed(self.masks, quantity=1)
        self.failed(self.siren)
        self.assertEqual(len(self.mine().deficiencies), 3)
        self.assertEqual(len(self.queries()), one)

    def test_bulk_facts_match_the_per_deficiency_readers(self):
        pk = self.failed(self.suction).body["deficiency_ids"][0]
        set_deadline(
            deficiency_id=pk,
            deadline=self.clock.now + timedelta(hours=1),
            expected_event_id=self.latest(pk),
            **self.context(self.supervisor),
        )
        enter_dependency(
            deficiency_id=pk,
            explanation="Synthetic vendor.",
            follow_up_at=self.clock.now + timedelta(days=2),
            expected_event_id=self.latest(pk),
            **self.context(self.supervisor),
        )
        acknowledgement.set_acknowledgement_maximum(maximum_days=3, **self.context(self.admin))
        acknowledgement.acknowledge_escalation(
            deficiency_id=pk,
            explanation="Synthetic review.",
            action_plan="Synthetic plan.",
            period_days=1,
            expected_event_id=self.latest(pk),
            **self.context(self.supervisor),
        )
        regrade_severity(
            deficiency_id=pk,
            severity="LOW",
            explanation="Synthetic re-grade.",
            expected_event_id=self.latest(pk),
            **self.context(self.supervisor),
        )
        self.clock.set(self.clock.now + timedelta(hours=2))
        org, at = str(self.org.pk), self.clock.now
        row = self.mine().deficiencies[0]
        deadline = service.extension_state(org, pk, as_of=at)
        acknowledged = acknowledgement.acknowledgement_state(org, pk, as_of=at)["acknowledgement"]
        self.assertEqual(
            (
                row["governing_deadline"],
                row["overdue"],
                row["age_days"],
                row["long_running"],
                row["extension_count"],
                row["follow_up_at"],
                row["acknowledged_until"],
                row["severity"]["code"],
            ),
            (
                deadline["governing_deadline"],
                deadline["overdue"],
                deadline["age_seconds"] // 86400,
                deadline["long_running"],
                deadline["extension_count"],
                service.dependency_state(org, pk, as_of=at)["dependency"]["follow_up_at"],
                acknowledged["acknowledgement_until"],
                service.severity_history(org, pk)["severity"]["code"],
            ),
        )
        self.assertEqual(row["severity"]["code"], "LOW")


class BoundsAndHistoryTests(ReadinessCase):
    def test_vehicle_and_deficiency_limits_say_they_cut_the_list(self):
        self.failed(self.suction)
        self.failed(self.masks, quantity=1)
        with mock.patch.object(readiness, "DEFICIENCY_LIMIT", 1):
            vehicle = self.mine()
        self.assertEqual(len(vehicle.deficiencies), 1)
        self.assertTrue(vehicle.deficiencies_truncated)
        with mock.patch.object(readiness, "VEHICLE_LIMIT", 1):
            picture = self.picture()
        self.assertEqual(len(picture.vehicles), 1)
        self.assertEqual(picture.truncated, len(self.vehicles) > 1)

    def test_an_as_of_picture_is_unchanged_by_later_events(self):
        self.into_window()
        self.record(self.suction)
        then = self.clock.now
        before = self.mine(self.picture(as_of=then))
        self.failed(self.masks, quantity=1)
        set_vehicle_status(
            vehicle_id=str(self.vehicle.pk),
            status="LIMITED_SERVICE",
            explanation="Synthetic reason.",
            restrictions="Synthetic restriction.",
            **self.context(self.supervisor),
        )
        later = self.mine(self.picture(as_of=then))
        self.assertEqual(
            (later.status, later.counts, later.deficiencies), (before.status, before.counts, [])
        )
        self.assertEqual(self.mine().status, "LIMITED_SERVICE")

    def test_a_future_or_naive_as_of_is_refused(self):
        with self.assertRaises(ValueError):
            self.picture(as_of=self.clock.now + timedelta(days=1))
        with self.assertRaises(ValueError):
            self.picture(as_of=self.clock.now.replace(tzinfo=None))


class ReadinessHttpTests(CrewUiCase):
    URL = "/api/v1/fleet-readiness"

    def supervisor_client(self):
        client, user = self.harness.sign_in("ready-sup")
        self.grant(user, "SUPERVISOR")
        return client

    def test_crew_are_refused_and_supervisors_read_json_and_the_screen(self):
        self.assertEqual(self.client.get(self.URL).status_code, 403)
        self.assertEqual(self.client.get("/fleet/readiness/").status_code, 403)
        self.assertEqual(self.client.get("/fleet/readiness/panel/").status_code, 403)
        sup = self.supervisor_client()
        body = sup.get(self.URL).json()
        self.assertEqual(body["status"], "OK")
        self.assertEqual(
            [c["code"] for c in body["categories"]], [c for c, _label in readiness.CATEGORIES]
        )
        vehicle = next(v for v in body["vehicles"] if v["vehicle_id"] == str(self.vehicle.pk))
        self.assertEqual(set(vehicle["obligations"]), {c for c, _l in readiness.CATEGORIES})
        page = sup.get("/fleet/readiness/")
        self.assertContains(page, "readiness-config")
        self.assertContains(page, "Obligations by state")
        self.assertNotContains(page, 'compliant" flag:')

    def test_as_of_is_validated_and_shown_as_a_static_picture(self):
        sup = self.supervisor_client()
        for query in (
            "?as_of=nope",
            "?as_of=2026-06-01T00:00:00",
            "?other=1",
            "?as_of=2999-01-01T00:00:00%2B00:00",
        ):
            with self.subTest(query):
                self.assertEqual(sup.get(self.URL + query).status_code, 400)
        then = (timezone.now() - timedelta(minutes=1)).isoformat()
        page = sup.get("/fleet/readiness/", {"as_of": then})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "This past picture does not update.")
        self.assertNotContains(page, "readiness-config")


class ReadinessScriptTests(SimpleTestCase):
    @skipUnless(shutil.which("node"), "optional Node client harness is unavailable")
    def test_offline_readiness_script_regressions(self):
        script = Path(__file__).with_name("readiness_script_harness.js")
        result = subprocess.run(
            [shutil.which("node"), str(script)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
