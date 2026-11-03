"""Load-test preparation (issue #10) on a tiny synthetic rehearsal agency."""

from datetime import UTC, datetime, timedelta
from unittest import mock

from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.core.management import call_command
from django.test import TransactionTestCase
from django.utils import timezone

from cya.accounts.middleware import SESSION_AUTH_TIME_KEY, SESSION_USER_KEY
from cya.accounts.models import User
from cya.checklists.models import Vehicle
from cya.obligations.models import Obligation
from tools import load_prepare, rehearsal_history


class LoadPrepareTests(TransactionTestCase):
    def setUp(self):
        end = timezone.now() - timedelta(days=1)
        rehearsal_history.generate(
            vehicles=2, items=3, days=1, crew=15, seed=5, end=end, progress=lambda line: None
        )

    def tearDown(self):
        # The commands commit for real; truncate, since the append-only triggers refuse DELETE.
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)
        super().tearDown()

    def prepare(self, scale):
        with mock.patch.object(load_prepare, "_refuse_unless_rehearsal"):
            return load_prepare.prepare(scale)

    def test_a_step_adds_what_is_missing_opens_a_slot_and_mints_working_sessions(self):
        plan = self.prepare(1)
        self.assertEqual(len(plan["vehicle_ids"]), 5)
        self.assertEqual(plan["created"], {"vehicles": 3, "users": 1})  # one more supervisor
        crew = [u for u in plan["users"] if u["role"] == "crew"]
        supervisors = [u for u in plan["users"] if u["role"] == "supervisor"]
        self.assertEqual(len(crew), 15)
        self.assertEqual(len(supervisors), 2)
        for vehicle_id in plan["vehicle_ids"]:
            self.assertEqual(sum(u["vehicle_id"] == vehicle_id for u in crew), 3)
        self.assertEqual(Vehicle.objects.count(), 5)
        self.assertTrue(all(u.email.endswith("@example.test") for u in User.objects.all()))

        # Every vehicle and item has an obligation for the new slot whose window is open now.
        due = datetime.fromisoformat(plan["due_at"])
        slot = Obligation.objects.filter(due_at=due)
        self.assertEqual(slot.count(), 5 * 3)
        now = timezone.now()
        self.assertTrue(all(o.early_at <= now <= o.grace_at for o in slot))
        self.assertEqual(due.tzinfo, UTC)

        # Each cookie is a valid session for its user, so the pages accept it.
        for user in plan["users"]:
            session = SessionStore(session_key=user["cookie"]).load()
            self.assertEqual(session[SESSION_USER_KEY], user["user_id"])
            self.assertIsInstance(session[SESSION_AUTH_TIME_KEY], int)
        self.client.cookies["cya_session"] = crew[0]["cookie"]
        page = self.client.get(f"/inspection/vehicles/{crew[0]['vehicle_id']}/", secure=True)
        self.assertEqual(page.status_code, 200)
        self.client.cookies["cya_session"] = supervisors[1]["cookie"]
        self.assertEqual(self.client.get("/fleet/readiness/", secure=True).status_code, 200)

    def test_a_larger_step_reuses_the_agency_and_opens_its_own_slot(self):
        first = self.prepare(1)
        second = self.prepare(2)
        self.assertEqual(second["created"], {"vehicles": 5, "users": 17})  # 15 crew, 2 supervisors
        self.assertEqual(second["vehicle_ids"][:5], first["vehicle_ids"])
        self.assertNotEqual(second["slot"], first["slot"])
        self.assertEqual(Vehicle.objects.count(), 10)
        self.assertEqual(User.objects.filter(email__contains="load-").count(), 1 + 15 + 2)
        third = self.prepare(2)
        self.assertEqual(third["created"], {"vehicles": 0, "users": 0})

    def test_refusals(self):
        with self.assertRaisesRegex(RuntimeError, "_rehearsal only"):
            load_prepare.prepare(1)  # the test database is not a rehearsal database
        for bad in (0, 21, 1.5, "2"):
            with self.subTest(scale=bad), self.assertRaises(ValueError):
                self.prepare(bad)
        self.assertEqual(Vehicle.objects.count(), 2)
