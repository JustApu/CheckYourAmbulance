"""The rehearsal history generator (issue #7, Part A) on a tiny synthetic agency."""

from datetime import UTC, datetime, timedelta
from unittest import mock

from django.core.management import call_command
from django.test import TransactionTestCase

from cya.accounts.models import Organization, User
from cya.checks.models import CheckObligationLink, CheckRecord
from cya.core.models import AuditEvent
from cya.deficiencies.models import Deficiency, DeficiencyProjection
from cya.obligations.models import Obligation
from tools import rehearsal_history

END = datetime(2026, 6, 20, 12, tzinfo=UTC)


class RehearsalHistoryTests(TransactionTestCase):
    def tearDown(self):
        # The commands commit for real. Truncate (reset_sequences) because the append-only
        # triggers refuse the DELETE of a plain flush.
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)
        super().tearDown()

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

    def test_extend_continues_the_same_history_to_the_given_instant(self):
        # Issue #70: the load history is extended to the present before a rehearsal.
        self.generate()
        before = CheckRecord.objects.count()
        last = CheckRecord.objects.order_by("-recorded_at").first().recorded_at
        lines = []
        until = END + timedelta(days=3)  # three more mornings have passed by then
        counts = rehearsal_history.extend(until=until, seed=5, progress=lines.append)
        added = sum(counts.get(k, 0) for k in ("PASSED", "FAILED", "UNABLE_TO_INSPECT"))
        self.assertEqual(added, 2 * 3 * len(lines))
        self.assertEqual(len(lines), 5)  # the mornings of 06-19 to 06-23
        self.assertEqual(CheckRecord.objects.count(), before + added)
        newest = CheckRecord.objects.order_by("-recorded_at").first().recorded_at
        self.assertGreater(newest, last)
        self.assertLess(newest, until)
        # Every new check is linked to its obligation, as in the generated history.
        self.assertEqual(CheckObligationLink.objects.count(), CheckRecord.objects.count())
        # Nothing to add when no further morning has passed.
        self.assertEqual(rehearsal_history.extend(until=until, progress=lines.append), {})

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
