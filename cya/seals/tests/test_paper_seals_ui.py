"""The paper seals page and its scope read (SPEC 8.5.0 LATE-7; issue #9 B6c).

Over real HTTP sign-in, with the fixtures of the B6a paper pages: a sheet performed on 2026-06-02
at 06:15 New York time is entered on 2026-06-03. Rendered client behavior is exercised by
vehicle_script_harness.js.
"""

import json
import re
from unittest.mock import patch
from uuid import uuid4

from cya.inspections.tests.test_paper_entry_ui import PaperUiCase
from cya.paper.invalidation import invalidate_paper_entry_batch
from cya.paper.models import PaperEntryBatch

SCOPE = {"subject": {}, "items": [], "kit_presence": [], "kit_lines": []}


class PaperSealsCase(PaperUiCase):
    def seals_url(self, batch_id):
        return f"/inspection/paper-entry/{batch_id}/seals/"

    def seal_config(self, response):
        found = re.search(
            r'<script id="seal-config" type="application/json">(.*?)</script>',
            response.content.decode(),
            re.S,
        )
        self.assertIsNotNone(found, "page carries its config")
        return json.loads(found.group(1))

    def scope_url(self, query="", kind="kit", subject=None):
        return f"/api/v1/vehicles/{self.vehicle.pk}/seals/{kind}/{subject or uuid4()}" + (
            f"?{query}" if query else ""
        )


class PaperSealsPageTests(PaperSealsCase):
    def test_the_entry_actor_sees_the_seal_page_in_paper_mode(self):
        client, user = self.sign_in("paper-crew")
        batch_id = self.sheet(user)
        response = client.get(self.seals_url(batch_id))
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        config = self.seal_config(response)
        paper = config["paper_entry"]
        self.assertEqual(paper["batch_id"], batch_id)
        self.assertEqual(paper["checklist_version_id"], self.version)
        self.assertEqual(paper["state"], "OPEN")
        self.assertEqual(paper["performed_at"], "2026-06-02T10:15:00+00:00")
        self.assertEqual(paper["performers"], [user.display_name])
        self.assertFalse(paper["has_records"])
        self.assertFalse(config["can_request_audit"], "an audit request is never copied from paper")
        self.assertEqual(config["vehicle_id"], str(self.vehicle.pk))
        self.assertEqual(
            config["scope_url"], f"/api/v1/vehicles/{self.vehicle.pk}/seals/KIND/SUBJECT"
        )
        self.assertIn("<h1>Seal records from this sheet</h1>", page)
        self.assertIn('id="paper-banner"', page)
        self.assertIn("2026-06-02 06:15 (America/New_York); saved as 2026-06-02 10:15 UTC.", page)
        self.assertIn(f"Version ID {self.version}", page)
        self.assertIn("Copy each seal record as it is written on the sheet", page)
        self.assertNotIn("Tap each result", page)
        self.assertIn("Enter the seal records in the order they are written on the sheet", page)
        self.assertIn("cannot be placed before a later seal record", page)
        self.assertIn('id="paper-print-offer"', page)
        self.assertIn('id="seal-error" class="banner banner-error" role="alert"', page)
        self.assertIn('<dd id="paper-performers"></dd>', page, "performers only via json_script")
        self.assertIn(f'href="/inspection/paper-entry/{batch_id}/"', page)
        self.assertNotIn("Sealed kits and cabinets", page)
        # On screen the zone is named, never "station time" (the shell's script comments aside).
        content = page[page.index("<h1>") : page.index('<script id="seal-config"')]
        self.assertNotIn("station time", content.lower())

    def test_only_the_entry_actor_may_open_it(self):
        _client, user = self.sign_in("paper-crew")
        batch_id = self.sheet(user)
        other, _ = self.sign_in("other-crew")
        supervisor, _ = self.sign_in("paper-supervisor", role="SUPERVISOR")
        for client in (other, supervisor):
            self.assertEqual(client.get(self.seals_url(batch_id)).status_code, 404)
        for value in (str(uuid4()), "not-an-id"):
            self.assertEqual(other.get(self.seals_url(value)).status_code, 404)

    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        _client, user = self.sign_in("paper-crew")
        batch_id = self.sheet(user)
        response = self.client.get(self.seals_url(batch_id))  # the default client never signed in
        self.assertIn(response.status_code, (302, 303))

    def test_names_are_escaped_and_reach_the_page_only_as_json(self):
        client, user = self.sign_in("paper-crew")
        user.display_name = '<b id="unsafe">Sample</b></script><script>unsafe()</script>'
        user.save(update_fields=["display_name"])
        batch_id = self.sheet(user)
        response = client.get(self.seals_url(batch_id))
        page = response.content.decode()
        self.assertNotIn('<b id="unsafe">', page)
        self.assertNotIn("<script>unsafe()", page)
        self.assertEqual(
            self.seal_config(response)["paper_entry"]["performers"], [user.display_name]
        )

    def test_an_invalidated_entry_is_read_only(self):
        client, user = self.sign_in("paper-crew")
        batch_id = self.sheet(user)
        invalidate_paper_entry_batch(
            batch_id=batch_id, explanation="Synthetic wrong time.", **self.context(self.supervisor)
        )
        response = client.get(self.seals_url(batch_id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seal_config(response)["paper_entry"]["state"], "INVALIDATED")
        self.assertIn("This paper entry was invalidated.", response.content.decode())

    def test_the_paper_checklist_screen_links_to_it(self):
        client, user = self.sign_in("paper-crew")
        batch_id = self.sheet(user)
        response = client.get(f"/inspection/paper-entry/{batch_id}/")
        self.assertEqual(self.config(response)["urls"]["paper_seals"], self.seals_url(batch_id))
        self.assertIn(
            f'href="{self.seals_url(batch_id)}">Seal records from this sheet</a>',
            response.content.decode(),
        )

    def test_the_live_page_is_unchanged(self):
        client, _user = self.sign_in("paper-supervisor", role="SUPERVISOR")
        response = client.get(f"/inspection/vehicles/{self.vehicle.pk}/seals/")
        self.assertEqual(response.status_code, 200)
        config = self.seal_config(response)
        self.assertNotIn("paper_entry", config)
        self.assertTrue(config["can_request_audit"])
        page = response.content.decode()
        self.assertIn("<h1>Sealed kits and cabinets</h1>", page)
        self.assertNotIn('id="paper-banner"', page)


class PaperScopeApiTests(PaperSealsCase):
    def test_the_scope_is_read_at_the_sheets_time(self):
        client, user = self.sign_in("paper-crew")
        batch_id = self.sheet(user)
        performed_at = PaperEntryBatch.objects.get(pk=batch_id).performed_at
        subject = str(uuid4())
        with patch("cya.seals.api.readers.inspection_scope", return_value=SCOPE) as read:
            response = client.get(
                self.scope_url(f"paper_entry_batch_id={batch_id}", kind="slot", subject=subject)
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), SCOPE)
            read.assert_called_once_with(
                str(self.org.pk), str(self.vehicle.pk), "slot", subject, at=performed_at
            )
            read.reset_mock()
            self.assertEqual(client.get(self.scope_url(subject=subject)).status_code, 200)
            read.assert_called_once_with(str(self.org.pk), str(self.vehicle.pk), "kit", subject)

    def test_another_users_or_vehicles_batch_answers_404(self):
        _client, user = self.sign_in("paper-crew")
        batch_id = self.sheet(user)
        other, _ = self.sign_in("other-crew")
        own, _ = self.sign_in("paper-crew")
        with patch("cya.seals.api.readers.inspection_scope", return_value=SCOPE) as read:
            for client, url in (
                (other, self.scope_url(f"paper_entry_batch_id={batch_id}")),
                (own, self.scope_url(f"paper_entry_batch_id={uuid4()}")),
                (
                    own,
                    f"/api/v1/vehicles/{self.vehicles[1].pk}/seals/kit/{uuid4()}"
                    f"?paper_entry_batch_id={batch_id}",
                ),
            ):
                with self.subTest(url=url):
                    response = client.get(url)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json()["code"], "NOT_FOUND")
            read.assert_not_called()

    def test_the_batch_takes_no_other_key(self):
        client, user = self.sign_in("paper-crew")
        batch_id = self.sheet(user)
        with patch("cya.seals.api.readers.inspection_scope", return_value=SCOPE) as read:
            for query in (
                f"paper_entry_batch_id={batch_id}&at=2026-06-03T00:00:00Z",
                f"paper_entry_batch_id={batch_id}&session_id={uuid4()}",
                f"paper_entry_batch_id={batch_id}&paper_entry_batch_id={batch_id}",
                "paper_entry_batch_id=not-an-id",
                "paper_entry_batch_id=",
            ):
                with self.subTest(query=query):
                    response = client.get(self.scope_url(query))
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["code"], "MALFORMED_REQUEST")
            read.assert_not_called()

    def test_sign_in_is_required(self):
        response = self.client.get(self.scope_url(f"paper_entry_batch_id={uuid4()}"))
        self.assertEqual(response.status_code, 401)
