"""Kit library page over HTTP: roles, preview, confirm, downloads (SPEC 4.7, issue #129)."""

import base64
import json
import re

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TransactionTestCase, override_settings

from cya.checklists.models import Item
from cya.core.models import AuditEvent
from cya.kits import library_files
from cya.kits.models import KitDefinition, KitDefinitionVersion
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

URL = "/administration/kits/"


def library(quantity=6, key="jump-kit"):
    kit = {
        "key": key,
        "label": "Jump kit",
        "contents": [
            {
                "item": "Masks",
                "kind": "QUANTITY",
                "quantity": quantity,
                "outcomes": ["PASSED", "FAILED"],
            }
        ],
    }
    return json.dumps({"file_type": "kit-library", "schema_version": 1, "kits": [kit]}).encode()


@override_settings(**real_collaborator_settings())
class KitLibraryHttpTests(TransactionTestCase):
    def setUp(self):
        harness = RoleHttpHarness()
        self.org = harness.org
        self.harness = harness
        self.admin_client, self.admin = harness.make_administrator("admin-1")

    def tearDown(self):
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)

    def token(self, client):
        page = client.get(URL)
        return re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()
        ).group(1)

    def supervisor(self):
        client, user = self.harness.sign_in("supervisor-1")
        self.assertEqual(
            self.harness.change(self.admin_client, user, role="SUPERVISOR").status_code, 302
        )
        return client, user

    def preview(self, data, fmt="json", client=None):
        client = client or self.admin_client
        upload = SimpleUploadedFile(f"kits.{fmt}", data)
        return client.post(
            URL,
            {
                "csrfmiddlewaretoken": self.token(client),
                "stage": "preview",
                "format": fmt,
                "library": upload,
            },
        )

    def confirm_fields(self, response):
        html = response.content.decode()
        fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', html))
        fields.pop("stage", None)
        return fields

    def confirm(self, fields, client=None):
        client = client or self.admin_client
        return client.post(
            URL, {"csrfmiddlewaretoken": self.token(client), "stage": "confirm", **fields}
        )

    def test_crew_is_refused_and_the_denial_is_audited(self):
        client, crew = self.harness.sign_in("crew-1")
        for path in (URL, URL + "template.json", URL + "export.csv"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 403)
        self.assertTrue(AuditEvent.objects.filter(actor_user=crew, outcome="DENIED").exists())

    def test_supervisors_download_but_cannot_import(self):
        client, user = self.supervisor()
        page = client.get(URL)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Blank template (JSON)")
        self.assertNotContains(page, "Preview import")
        template = client.get(URL + "template.csv")
        self.assertEqual(template["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment;", template["Content-Disposition"])
        self.assertEqual(library_files.parse(template.content, "csv"), library_files.TEMPLATE_KITS)
        refused = self.preview(library(), client=client)
        self.assertEqual(refused.status_code, 403)
        self.assertFalse(KitDefinition.objects.exists())

    def test_preview_writes_nothing_then_confirm_imports_drafts(self):
        before = (Item.objects.count(), AuditEvent.objects.count())
        preview = self.preview(library())
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "1 kit to create")
        self.assertContains(preview, "New items: Masks")
        self.assertEqual((Item.objects.count(), AuditEvent.objects.count()), before)
        fields = self.confirm_fields(preview)
        self.assertEqual(base64.b64decode(fields["data"]), library())
        done = self.confirm(fields)
        self.assertEqual(done.status_code, 302)
        self.assertIn("imported=1", done["Location"])
        self.assertIn("created=1", done["Location"])
        self.assertEqual(KitDefinitionVersion.objects.get(definition__key="jump-kit").revision, 1)
        landing = self.admin_client.get(done["Location"])
        self.assertContains(landing, "Imported as drafts: 1 created")
        self.assertContains(landing, "jump-kit")
        self.assertContains(landing, "Not published")

    def test_a_stale_or_tampered_confirmation_is_refused(self):
        fields = self.confirm_fields(self.preview(library()))
        Item.objects.create(
            organization=self.org, label="Masks"
        )  # resolution changes after preview
        stale = self.confirm(fields)
        self.assertEqual(stale.status_code, 409)
        self.assertContains(stale, "preview it again", status_code=409)
        self.assertFalse(KitDefinition.objects.exists())
        for name, value in (("data", "%%%"), ("operation_id", "not-a-uuid")):
            with self.subTest(field=name):
                response = self.confirm(
                    {**self.confirm_fields(self.preview(library())), name: value}
                )
                self.assertEqual(response.status_code, 400)
        swapped = self.confirm(
            {
                **self.confirm_fields(self.preview(library())),
                "data": base64.b64encode(library(quantity=9)).decode(),
            }
        )
        self.assertEqual(
            swapped.status_code, 409, "a file other than the previewed one does not match its token"
        )
        self.assertFalse(KitDefinition.objects.exists())

    def test_malformed_and_missing_uploads_show_errors_without_writing(self):
        bad = self.preview(b"{not json")
        self.assertEqual(bad.status_code, 400)
        self.assertContains(bad, "not valid JSON", status_code=400)
        self.assertNotContains(bad, "Import as drafts", status_code=400)
        missing = self.admin_client.post(
            URL,
            {
                "csrfmiddlewaretoken": self.token(self.admin_client),
                "stage": "preview",
                "format": "json",
            },
        )
        self.assertEqual(missing.status_code, 400)
        wrong_format = self.preview(library(), fmt="xlsx")
        self.assertEqual(wrong_format.status_code, 400)
        unknown_stage = self.admin_client.post(
            URL, {"csrfmiddlewaretoken": self.token(self.admin_client), "stage": "other"}
        )
        self.assertEqual(unknown_stage.status_code, 400)
        self.assertFalse(KitDefinition.objects.exists())

    def test_export_is_audited_with_counts_and_round_trips(self):
        self.confirm(self.confirm_fields(self.preview(library())))
        exported = self.admin_client.get(URL + "export.json")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported["Content-Type"], "application/json; charset=utf-8")
        audit = AuditEvent.objects.get(command__command_type="KIT_LIBRARY_EXPORT")
        self.assertEqual(audit.new_snapshot["values"]["kits"], 1)
        self.assertNotIn("Masks", json.dumps(audit.new_snapshot))
        again = self.preview(exported.content)
        self.assertContains(again, "0 kits to create, 0 to change (as a new draft), 1 unchanged")
        self.assertEqual(self.admin_client.get(URL + "export.xlsx").status_code, 404)
