"""Seal commands take identity from the authenticated request and bound their input."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import RequestFactory, SimpleTestCase

from cya.seals.api import ACTIONS, command
from cya.seals.commands import SealConflict


class SealApiTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = SimpleNamespace(pk="actor", organization_id="org")
        self.body = {
            "operation_id": "operation",
            "vehicle_id": "vehicle",
            "kit_id": "kit",
            "marking": "crew-id",
            "attested": True,
        }

    def request(self, body, user=None):
        request = self.factory.post(
            "/api/v1/seals/verify", data=json.dumps(body), content_type="application/json"
        )
        request.cya_user = user
        return request

    def test_authentication_is_required(self):
        self.assertEqual(command(self.request(self.body), "verify").status_code, 401)

    def test_identity_is_server_selected_and_unknown_fields_are_refused(self):
        result = SimpleNamespace(
            operation_reused=False,
            correlation_id="correlation",
            status=200,
            body={"code": "SEAL_SUBSTITUTED"},
        )
        handler = Mock(return_value=result)
        with patch.dict(ACTIONS, {"verify": (handler, ACTIONS["verify"][1])}):
            response = command(self.request(self.body, self.user), "verify")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(handler.call_args.kwargs["actor_id"], "actor")
            self.assertEqual(handler.call_args.kwargs["organization_id"], "org")
            response = command(self.request(self.body | {"actor_id": "other"}, self.user), "verify")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(handler.call_count, 1)

    def test_stale_seal_is_a_conflict(self):
        with patch.dict(
            ACTIONS, {"verify": (Mock(side_effect=SealConflict("stale")), ACTIONS["verify"][1])}
        ):
            self.assertEqual(command(self.request(self.body, self.user), "verify").status_code, 409)
