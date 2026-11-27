"""Paper entry pages and the paper checklist read (SPEC 8.0.0 REC-5, LATE-1 to LATE-8; issue #9 B6a).

Over real HTTP sign-in. The vehicle's own checklist places a synthetic jump kit; a sheet performed
on 2026-06-02 at 06:15 New York time is entered on 2026-06-03 (PaperCheckCase). Rendered client
behavior is exercised by paper_entry_script_harness.js and workflow_script_harness.js.
"""

import json
import re
from datetime import date, time
from uuid import uuid4

from django.test import override_settings

from cya.authorization.commands import change_role
from cya.checklists.tests.support import ids
from cya.checks.commands import record_check
from cya.paper.commands import record_paper_entry_batch
from cya.paper.invalidation import invalidate_paper_entry_batch
from cya.paper.tests.support import ZONE, PaperCheckCase
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings


@override_settings(**real_collaborator_settings())
class PaperUiCase(PaperCheckCase):
    def make_org(self):
        self.harness = RoleHttpHarness()
        org = self.harness.org
        org.station_zone = ZONE
        org.save(update_fields=["station_zone"])
        return org

    def sign_in(self, subject, role=None, revoke_crew=False):
        client, user = self.harness.sign_in(subject)
        if role:
            self.grant(user, role)
        if revoke_crew:
            change_role(
                organization_id=str(self.org.pk),
                actor_id=str(self.admin.pk),
                target_id=str(user.pk),
                role="CREW",
                action="REVOKE",
                **ids(),
            )
        client.get("/")  # obtains the CSRF cookie
        return client, user

    def sheet(self, user, performers=None, kits=None):
        return record_paper_entry_batch(
            vehicle_id=str(self.vehicle.pk),
            checklist_version_id=self.version,
            performed_on=date(2026, 6, 2),
            performed_time=time(6, 15),
            performer_ids=[str(p.pk) for p in (performers or [user])],
            kit_definition_version_ids=kits or [],
            **self.context(user),
        ).body["batch_id"]

    def config(self, response):
        found = re.search(
            r'<script id="cya-config" type="application/json">(.*?)</script>',
            response.content.decode(),
            re.S,
        )
        self.assertIsNotNone(found, "page carries its config")
        return json.loads(found.group(1))

    def setup_url(self, template=None):
        return (
            f"/inspection/vehicles/{self.vehicle.pk}/paper-entry/"
            f"?template={template or self.checklist.pk}"
        )

    def checklist_url(self, **query):
        query = "&".join(f"{k}={v}" for k, v in query.items())
        return f"/api/v1/vehicles/{self.vehicle.pk}/checklist?{query}"


class VehiclePageTests(PaperUiCase):
    def test_crew_get_a_secondary_paper_link_per_own_checklist(self):
        client, _user = self.sign_in("paper-crew")
        page = client.get(f"/inspection/vehicles/{self.vehicle.pk}/").content.decode()
        self.assertIn(f'href="{self.setup_url()}"', page.replace("&amp;", "&"))
        self.assertIn('class="button paper-entry-link"', page)
        self.assertIn("Paper entry: copy an outage sheet", page)
        self.assertRegex(page, r'class="button primary start-session"', "Start stays primary")

    def test_a_user_with_no_paper_role_gets_no_link_and_no_setup_page(self):
        client, _user = self.sign_in("paper-officer", role="SUPPLY_OFFICER", revoke_crew=True)
        page = client.get(f"/inspection/vehicles/{self.vehicle.pk}/")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('class="button paper-entry-link"', page.content.decode())
        self.assertEqual(client.get(self.setup_url()).status_code, 403)

    def test_recent_own_paper_entries_can_be_continued(self):
        client, user = self.sign_in("paper-crew")
        batch_id = self.sheet(user)
        other = self.sheet(self.supervisor, performers=[user])  # someone else's entry
        gone = self.sheet(user)
        invalidate_paper_entry_batch(
            batch_id=gone, explanation="Synthetic wrong time.", **self.context(self.supervisor)
        )
        page = client.get(f"/inspection/vehicles/{self.vehicle.pk}/").content.decode()
        self.assertIn("Your recent paper entries", page)
        self.assertIn(f'href="/inspection/paper-entry/{batch_id}/"', page)
        self.assertNotIn(other, page)
        self.assertNotIn(gone, page, "an invalidated entry is not offered for continuing")
        self.assertIn("Continue copying", page)


class SetupPageTests(PaperUiCase):
    def test_crew_setup_offers_the_printed_versions_and_names_only_themself(self):
        client, user = self.sign_in("paper-crew")
        response = client.get(self.setup_url())
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        config = self.config(response)
        self.assertEqual(config["vehicle_id"], str(self.vehicle.pk))
        self.assertFalse(config["can_attribute"])
        self.assertEqual(config["own_name"], user.display_name)
        self.assertEqual(
            config["urls"],
            {
                "batches": "/api/v1/paper-entry-batches",
                "performers": "/api/v1/paper-entry-performers",
            },
        )
        [version] = config["versions"]
        self.assertEqual(version["version_id"], self.version)
        self.assertTrue(version["current"])
        self.assertIn(f"revision 1 — Version ID {self.version}", version["label"])
        [kit] = version["kits"]
        self.assertEqual(kit["definition_id"], self.definition)
        self.assertEqual([v["version_id"] for v in kit["versions"]], [self.kit_version])
        self.assertIn(f"Kit version {self.kit_version}", kit["versions"][0]["label"])
        self.assertIn('id="performer-self"', page)
        self.assertNotIn('id="performer-search"', page)
        self.assertIn(f"They are read in the {ZONE} time zone.", page)
        self.assertIn(
            'id="paper-errors" class="banner banner-error" role="alert" tabindex="-1"', page
        )
        self.assertIn("<legend>When the checks were done</legend>", page)
        self.assertIn('name="csrfmiddlewaretoken"', page)
        self.assertIn('<input id="paper-date" type="date" required', page, "no date is prefilled")

    def test_a_supervisor_chooses_performers_by_search(self):
        client, _user = self.sign_in("paper-sup", role="SUPERVISOR")
        response = client.get(self.setup_url())
        self.assertTrue(self.config(response)["can_attribute"])
        page = response.content.decode()
        self.assertIn('id="performer-search"', page)
        self.assertIn("Add myself", page)

    def test_names_reach_the_page_only_as_escaped_json(self):
        client, user = self.sign_in("paper-crew")
        user.display_name = "Synthetic </script><b>Crew</b>"
        user.save(update_fields=["display_name"])
        page = client.get(self.setup_url()).content.decode()
        self.assertNotIn("<b>Crew</b>", page)
        self.assertIn("\\u003C/script\\u003E\\u003Cb\\u003ECrew", page)

    def test_an_unknown_missing_or_foreign_checklist_is_404(self):
        client, _user = self.sign_in("paper-crew")
        for url in (
            f"/inspection/vehicles/{self.vehicle.pk}/paper-entry/",
            self.setup_url("not-an-id"),
            self.setup_url(str(uuid4())),
            self.setup_url(str(self.template.pk)),  # not this vehicle's own checklist
            f"/inspection/vehicles/{uuid4()}/paper-entry/?template={self.checklist.pk}",
        ):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 404)

    def test_signed_out_visitors_are_sent_to_sign_in(self):
        from django.test import Client

        response = Client().get(self.setup_url())
        self.assertEqual(response.status_code, 302)


class PaperScreenTests(PaperUiCase):
    def test_the_screen_is_the_checklist_in_paper_mode(self):
        client, user = self.sign_in("paper-crew")
        batch_id = self.sheet(user)
        response = client.get(f"/inspection/paper-entry/{batch_id}/")
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        config = self.config(response)
        self.assertNotIn("session_id", config)
        self.assertEqual(config["checklist_version_id"], self.version)
        self.assertEqual(
            (config["state"], config["view"], config["needs_attention_only"]),
            ("OPEN", "NORMAL", False),
        )
        paper = config["paper_entry"]
        self.assertEqual(paper["batch_id"], batch_id)
        self.assertEqual(paper["performed_at"], "2026-06-02T10:15:00+00:00")
        self.assertEqual(paper["performed_label"], "2026-06-02 06:15")
        self.assertEqual(paper["performers"], [user.display_name])
        self.assertFalse(paper["has_records"])
        self.assertEqual(
            set(config["urls"]),
            {
                "checklist",
                "checks",
                "odometer",
                "vehicle",
                "socket",
                "suggestions",
                "deficiency_detail",
                "print_checklist",
                "kit_reference",
                "paper_seals",
            },
        )
        # The sheet's seal records (B6c); never the live seal page of the vehicle.
        self.assertEqual(
            config["urls"]["paper_seals"], f"/inspection/paper-entry/{batch_id}/seals/"
        )
        self.assertIn("Seal records from this sheet", page)
        self.assertNotIn(f"/inspection/vehicles/{self.vehicle.pk}/seals/", page)
        self.assertIn("Paper entry", page)
        self.assertIn(
            "2026-06-02 06:15 (America/New_York); saved as 2026-06-02 10:15 UTC.",
            page,
        )
        self.assertIn(f"Version ID {self.version}", page)
        self.assertIn("You are recorded as the person entering these records", page)
        self.assertIn("are not listed, because no kit version was named for them", page)
        self.assertIn('id="paper-print-offer"', page)
        self.assertIn(
            "Keep the paper sheet until every record you copied from it shows Saved", page
        )
        self.assertIn('id="odometer-form"', page)
        self.assertNotIn('id="view-normal"', page)
        self.assertNotIn('id="checklist-bar"', page)
        self.assertNotIn("Sealed kits and cabinets", page)
        self.assertIn('<dd id="paper-performers"></dd>', page, "performers only via json_script")

    def test_records_already_entered_are_known_and_an_invalidated_batch_is_read_only(self):
        client, user = self.sign_in("paper-crew")
        batch_id = self.sheet(user)
        self.paper(batch_id, actor=user)
        self.assertTrue(
            self.config(client.get(f"/inspection/paper-entry/{batch_id}/"))["paper_entry"][
                "has_records"
            ]
        )
        invalidate_paper_entry_batch(
            batch_id=batch_id, explanation="Synthetic wrong time.", **self.context(self.supervisor)
        )
        response = client.get(f"/inspection/paper-entry/{batch_id}/")
        self.assertEqual(self.config(response)["state"], "INVALIDATED")
        page = response.content.decode()
        self.assertIn("This paper entry was invalidated.", page)
        self.assertNotIn('id="odometer-form"', page)

    def test_another_users_or_a_malformed_batch_is_404(self):
        client, _user = self.sign_in("paper-crew")
        theirs = self.sheet(self.supervisor, performers=[self.crew])
        for batch_id in (theirs, str(uuid4()), "not-an-id"):
            with self.subTest(batch_id=batch_id):
                self.assertEqual(
                    client.get(f"/inspection/paper-entry/{batch_id}/").status_code, 404
                )


class PaperChecklistApiTests(PaperUiCase):
    def test_the_sheets_rows_name_the_batch_check_and_the_paper_source(self):
        client, user = self.sign_in("paper-crew")
        batch_id = self.sheet(user)
        check_id = record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.suction.pk),
            checklist_version_id=self.version,
            outcome="PASSED",
            paper_entry_batch_id=batch_id,
            **self.context(user),
        ).body["check_id"]
        response = client.get(
            self.checklist_url(
                view="NORMAL", needs_attention_only="false", paper_entry_batch_id=batch_id
            )
        )
        self.assertEqual(response.status_code, 200, response.content)
        rows = {r["item_id"]: r for r in response.json()["items"]}
        row = rows[str(self.suction.pk)]
        self.assertEqual(row["paper_entry_check"], {"check_id": check_id, "outcome": "PASSED"})
        self.assertEqual(row["latest_check_paper"]["performers"], [user.display_name])
        self.assertEqual(row["latest_check_paper"]["entered_by"], user.display_name)
        self.assertIsNone(rows[str(self.masks.pk)]["paper_entry_check"])
        live = client.get(self.checklist_url(view="NORMAL")).json()["items"]
        live_row = next(r for r in live if r["item_id"] == str(self.suction.pk))
        self.assertIsNone(live_row["paper_entry_check"])
        self.assertEqual(
            live_row["latest_check_paper"],
            row["latest_check_paper"],
            "marked Paper entry in Normal mode too",
        )

    def test_refusals(self):
        client, user = self.sign_in("paper-crew")
        batch_id = self.sheet(user)
        theirs = self.sheet(self.supervisor, performers=[self.crew])
        for query, status in (
            ({"paper_entry_batch_id": batch_id, "session_id": str(uuid4())}, 400),
            ({"paper_entry_batch_id": batch_id, "checklist_version_id": self.version}, 400),
            ({"paper_entry_batch_id": "not-an-id"}, 400),
            ({"paper_entry_batch_id": theirs}, 404),
            ({"paper_entry_batch_id": str(uuid4())}, 404),
        ):
            with self.subTest(query=query):
                self.assertEqual(client.get(self.checklist_url(**query)).status_code, status)
        other = client.get(
            f"/api/v1/vehicles/{self.vehicles[1].pk}/checklist?paper_entry_batch_id={batch_id}"
        )
        self.assertEqual(other.status_code, 404, "another vehicle's read cannot name the batch")
