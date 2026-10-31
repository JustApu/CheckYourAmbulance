"""HTTP authorization, strict shapes, replay and return-to-service affordance."""

from datetime import timedelta
from uuid import uuid4

from django.utils import timezone

from cya.obligations.models import Obligation
from cya.operations.tests.test_api import StatusHttpCase


class AdHocHttpTests(StatusHttpCase):
    def setUp(self):
        super().setUp()
        self.published_version()
        self.url = f"/api/v1/vehicles/{self.vehicle.pk}/ad-hoc-inspections"
        self.body = {
            "operation_id": str(uuid4()),
            "items": [{"item_id": str(self.suction.pk), "kit_instance_id": None}],
            "due_at": (timezone.now() + timedelta(hours=3)).isoformat(),
            "explanation": "Synthetic inspection.",
        }
        # The vehicle needs a checklist binding for the same scope the checklist screen uses.
        from datetime import time
        from unittest import mock

        from cya.checklists.tests.support import ids
        from cya.obligations.commands import bind_requirement
        from cya.schedules.commands import publish_rule
        from cya.schedules.engine import Rule

        publish_rule(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            slot="adhoc-http",
            rule=Rule(
                zone="UTC", wall_time=time(6), effective_at=timezone.now(), recurrence="DAILY"
            ),
            **ids(),
        )
        with mock.patch("cya.obligations.commands.reconcile_vehicle"):
            bind_requirement(
                organization_id=str(self.org.pk),
                actor_id=str(self.admin.pk),
                vehicle_id=str(self.vehicle.pk),
                template_id=str(self.template.pk),
                slot="adhoc-http",
                **ids(),
            )

    def test_post_roles_strict_input_and_replay(self):
        for role, expected in [
            ("CREW", 403),
            ("SUPPLY_OFFICER", 403),
            ("SUPERVISOR", 200),
            ("ADMINISTRATOR", 200),
        ]:
            client, token = self.as_role(role, "adhoc-" + role.lower())
            body = {**self.body, "operation_id": str(uuid4())}
            response = self.post(self.url, body, client, token)
            self.assertEqual(response.status_code, expected, response.content)
            if expected == 200:
                replay = self.post(self.url, body, client, token)
                self.assertEqual(
                    replay.json()["result"]["obligation_ids"],
                    response.json()["result"]["obligation_ids"],
                )
        self.assertEqual(Obligation.objects.filter(routine=False).count(), 2)
        client, token = self.as_role("SUPERVISOR", "adhoc-shape")
        for delta in [
            {"due_at": "bad"},
            {"due_at": "2027-01-01T01:00:00"},
            {"actor_id": str(uuid4())},
            {"items": []},
            {"items": [{}]},
        ]:
            response = self.post(self.url, {**self.body, **delta}, client, token)
            self.assertEqual(response.status_code, 400, response.content)

    def test_return_to_service_offers_form_without_creating_any_order(self):
        self.set_status("OUT_OF_SERVICE")
        self.set_status("IN_SERVICE")
        self.assertFalse(Obligation.objects.exists())
        client, _token = self.as_role("SUPERVISOR", "adhoc-page")
        html = client.get(f"/status/vehicles/{self.vehicle.pk}/").content.decode()
        self.assertIn('data-kind="adhoc"', html)
        self.assertIn("Due at (UTC)", html)
        self.assertIn(str(self.suction.pk), html)
        self.assertNotIn(
            'data-kind="adhoc"',
            self.client.get(f"/status/vehicles/{self.vehicle.pk}/").content.decode(),
        )
        self.assertFalse(Obligation.objects.exists())

    def test_inspection_choice_failure_keeps_status_and_status_controls_available(self):
        from unittest import mock

        self.set_status("IN_SERVICE")
        client, _token = self.as_role("SUPERVISOR", "adhoc-unavailable")
        with mock.patch(
            "cya.attention.checklist.vehicle_checklist",
            side_effect=ValueError("synthetic evidence limit"),
        ):
            response = client.get(f"/status/vehicles/{self.vehicle.pk}/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Inspection choices could not be loaded", html)
        self.assertIn('data-kind="set"', html)
        self.assertNotIn('data-kind="adhoc"', html)
