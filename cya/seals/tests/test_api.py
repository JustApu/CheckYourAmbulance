"""Seal commands take identity from the authenticated request and bound their input."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import RequestFactory, SimpleTestCase

from cya.seals.api import ACTIONS, command, inspection_scope, vehicle_subjects
from cya.seals.commands import SealAuditOpen, SealConflict
from cya.seals.locking import SealLockError
from cya.seals.scope import SealScopeError


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

    def test_unavailable_complete_scope_is_reported_as_a_server_failure(self):
        for error in (SealLockError("lock unavailable"), SealScopeError("scope unavailable")):
            with (
                self.subTest(error=error),
                patch.dict(ACTIONS, {"verify": (Mock(side_effect=error), ACTIONS["verify"][1])}),
            ):
                response = command(self.request(self.body, self.user), "verify")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(json.loads(response.content)["code"], "CONFIGURATION_UNAVAILABLE")

    def test_open_audit_refusal_is_actionable(self):
        with patch.dict(
            ACTIONS,
            {"verify": (Mock(side_effect=SealAuditOpen("open")), ACTIONS["verify"][1])},
        ):
            response = command(self.request(self.body, self.user), "verify")
            self.assertEqual(response.status_code, 422)
            self.assertEqual(json.loads(response.content)["code"], "AUDIT_REQUIRES_SEAL")

    def test_read_views_are_authenticated_and_use_current_organization(self):
        vehicle_id, kit_id = str(uuid4()), str(uuid4())
        request = self.factory.get("/seals")
        request.cya_user = None
        self.assertEqual(vehicle_subjects(request, vehicle_id).status_code, 401)
        request.cya_user = self.user
        with patch("cya.seals.api.readers.subjects", return_value=[]) as subjects:
            self.assertEqual(vehicle_subjects(request, vehicle_id).status_code, 200)
            subjects.assert_called_once_with("org", vehicle_id)
        with patch("cya.seals.api.readers.inspection_scope", side_effect=LookupError):
            self.assertEqual(inspection_scope(request, vehicle_id, "kit", kit_id).status_code, 404)
