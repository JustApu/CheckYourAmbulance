"""Live status hints (issue 148 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/148)): identifier-only, after commit, silent on rollback and refusal."""

from unittest.mock import patch

from asgiref.sync import async_to_sync

from cya.core.audit_commands import CommandDenied
from cya.deficiencies.tests.support import DeficiencyCase
from cya.live_updates import tests as live_tests
from cya.operations import commands
from cya.operations.commands import StatusError
from cya.operations.models import StatusRequest, VehicleStatusEvent
from cya.operations.status import current_status
from cya.operations.tests.support import StatusCase


class HintCase(StatusCase):
    def setUp(self):
        super().setUp()
        self.layer = live_tests.PublishAfterCommitTests.RecordingLayer()
        self.channel = async_to_sync(self.layer.new_channel)()
        async_to_sync(self.layer.group_add)(
            f"vehicle.{self.org.pk}.{self.vehicle.pk}", self.channel
        )
        patcher = patch("cya.live_updates.publish.get_channel_layer", return_value=self.layer)
        patcher.start()
        self.addCleanup(patcher.stop)

    def hints(self):
        return [(m["kind"], m["event_id"]) for _group, m in self.layer.sent]


class StatusHintTests(HintCase):
    def test_set_status_publishes_one_identifier_only_hint_for_the_new_event(self):
        result = self.set_status("OUT_OF_SERVICE")
        self.assertEqual(self.hints(), [("status.changed", result.body["event_id"])])
        message = self.layer.sent[0][1]
        self.assertEqual(
            set(message), {"type", "organization_id", "vehicle_id", "event_id", "kind"}
        )
        self.assertEqual(message["vehicle_id"], str(self.vehicle.pk))

    def test_request_publishes_a_request_hint_and_changes_no_status(self):
        result = self.request("OUT_OF_SERVICE")
        self.assertEqual(self.hints(), [("status.request", result.body["request_id"])])
        self.assertIsNone(current_status(self.org.pk, self.vehicle.pk))

    def test_approval_publishes_the_status_change_and_the_decision_and_denial_only_the_latter(self):
        asked = self.request("OUT_OF_SERVICE").body["request_id"]
        self.layer.sent.clear()
        approved = self.decide(asked, "APPROVE")
        self.assertEqual(
            self.hints(),
            [
                ("status.changed", approved.body["event_id"]),
                ("status.request", approved.body["decision_id"]),
            ],
        )
        again = self.request("LIMITED_SERVICE").body["request_id"]
        self.layer.sent.clear()
        denied = self.decide(again, "DENY")
        self.assertEqual(self.hints(), [("status.request", denied.body["decision_id"])])

    def test_replay_publishes_nothing_more(self):
        context = self.context(self.supervisor)

        def send():
            return commands.set_vehicle_status(
                vehicle_id=str(self.vehicle.pk),
                status="OUT_OF_SERVICE",
                explanation="Synthetic reason.",
                **context,
            )

        first, replay = send(), send()
        self.assertEqual(first.body, replay.body)
        self.assertEqual(len(self.layer.sent), 1)

    def test_refused_and_invalid_commands_publish_nothing(self):
        asked = self.request("OUT_OF_SERVICE").body["request_id"]
        self.layer.sent.clear()
        self.set_status("OUT_OF_SERVICE")
        self.layer.sent.clear()
        with self.assertRaises(CommandDenied):
            self.set_status("IN_SERVICE", actor=self.crew)
        with self.assertRaises(CommandDenied):
            self.decide(asked, "APPROVE", actor=self.crew)
        with self.assertRaises(StatusError):
            self.set_status("OUT_OF_SERVICE")  # already out of service
        with self.assertRaises(StatusError):
            self.decide(asked, "DENY", explanation=" ")
        self.assertEqual(self.layer.sent, [])

    def test_a_rolled_back_approval_publishes_nothing(self):
        asked = self.request("OUT_OF_SERVICE").body["request_id"]
        self.layer.sent.clear()
        before = StatusRequest.objects.count()
        # The event is appended and its hint scheduled before the decision row fails.
        with patch.object(StatusRequest.objects, "create", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                self.decide(asked, "APPROVE")
        self.assertEqual(self.layer.sent, [])
        self.assertFalse(VehicleStatusEvent.objects.exists())
        self.assertEqual(StatusRequest.objects.count(), before)

    def test_a_failed_delivery_leaves_the_committed_status_in_place(self):
        with patch.object(self.layer, "group_send", side_effect=RuntimeError("synthetic outage")):
            self.set_status("OUT_OF_SERVICE")
        self.assertEqual(current_status(self.org.pk, self.vehicle.pk).status, "OUT_OF_SERVICE")


class NoAutomaticStatusTests(DeficiencyCase, HintCase):
    def test_a_critical_failed_check_changes_neither_status_nor_requests(self):
        self.set_status("IN_SERVICE")
        self.layer.sent.clear()
        before = (VehicleStatusEvent.objects.count(), StatusRequest.objects.count())
        result = self.failed(create=self.new_fields(severity="CRITICAL"))
        self.assertEqual(result.body["code"], "CHECK_RECORDED")
        self.assertEqual(
            (VehicleStatusEvent.objects.count(), StatusRequest.objects.count()), before
        )
        self.assertEqual(current_status(self.org.pk, self.vehicle.pk).status, "IN_SERVICE")
        self.assertNotIn("status.changed", [kind for kind, _ in self.hints()])
