"""The rehearsal history generator (issue #7, Part A) on a tiny synthetic agency."""

from datetime import UTC, datetime
from unittest import mock

from django.test import TransactionTestCase

from cya.accounts.models import Organization, User
from cya.checks.models import CheckObligationLink, CheckRecord
from cya.core.models import AuditEvent
from cya.deficiencies.models import Deficiency, DeficiencyProjection
from cya.obligations.models import Obligation
from tools import rehearsal_history

END = datetime(2026, 6, 20, 12, tzinfo=UTC)


class RehearsalHistoryTests(TransactionTestCase):
    def generate(self, **kw):
        lines = []
        options = {"vehicles": 2, "items": 3, "days": 8, "crew": 2, "seed": 3, "end": END}
        counts = rehearsal_history.generate(**{**options, **kw}, progress=lines.append)
        return counts, lines

    def test_every_item_is_checked_daily_and_old_deficiencies_are_closed(self):
        with mock.patch.object(rehearsal_history, "FAILED_RATE", 0.4):
            counts, lines = self.generate()
        checks = 2 * 3 * 8
        self.assertEqual(
            sum(counts.get(k, 0) for k in ("PASSED", "FAILED", "UNABLE_TO_INSPECT")), checks
        )
        self.assertEqual(CheckRecord.objects.count(), checks)
        # Every routine check is linked to its obligation; nothing was recorded outside a window.
        self.assertEqual(CheckObligationLink.objects.count(), checks)
        self.assertGreaterEqual(Obligation.objects.count(), checks)
        self.assertGreater(counts["FAILED"], 0)
        self.assertEqual(
            Deficiency.objects.count(), counts["FAILED"] + counts.get("UNABLE_TO_INSPECT", 0)
        )
        self.assertGreater(counts["CLOSED"], 0)
        self.assertEqual(
            DeficiencyProjection.objects.filter(status="CLOSED_ADMINISTRATOR").count(),
            counts["CLOSED"],
        )
        self.assertEqual(len(lines), 8)
        self.assertTrue(lines[-1].startswith("day 8/8 2026-06-18"))
        # Synthetic only: every user is an example.test address, and the work is audited.
        self.assertTrue(all(u.email.endswith("@example.test") for u in User.objects.all()))
        self.assertGreater(AuditEvent.objects.count(), checks)
        # The history ends before the end instant and starts in the past.
        self.assertLess(CheckRecord.objects.order_by("-recorded_at").first().recorded_at, END)

    def test_a_database_with_data_or_an_unsupported_size_is_refused(self):
        with self.assertRaises(ValueError):
            self.generate(vehicles=0)
        with self.assertRaises(ValueError):
            self.generate(items=501)
        self.assertFalse(Organization.objects.exists())
        self.generate(days=1, vehicles=1, items=1)
        with self.assertRaisesRegex(RuntimeError, "already holds an organization"):
            self.generate(days=1, vehicles=1, items=1)

    def test_a_generation_failure_stops_the_run(self):
        failing = mock.Mock(failures=["synthetic failure"])
        with (
            mock.patch.object(rehearsal_history, "run_generation", return_value=failing),
            self.assertRaisesRegex(RuntimeError, "obligation generation failed on day 0"),
        ):
            self.generate(days=1, vehicles=1, items=1)
