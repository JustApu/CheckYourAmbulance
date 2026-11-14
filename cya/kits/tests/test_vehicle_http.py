"""Vehicle files page over HTTP: roles, preview, confirm, downloads (SPEC 2.1.0 IMP-1 to IMP-7)."""

import base64
import re
import uuid

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from cya.checklists.models import Vehicle
from cya.core.models import AuditEvent
from cya.kits import vehicle_files
from cya.kits.tests.test_vehicle_import import VIN, VehicleImportCase
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

URL = "/administration/vehicle-files/"


@override_settings(**real_collaborator_settings())
class VehicleFilesHttpTests(VehicleImportCase):
    def setUp(self):
        super().setUp()
        self.harness = RoleHttpHarness(organization=self.org)
        self.admin_client, _ = self.harness.sign_in("admin")

    def token(self, client):
        page = client.get(URL)
        return re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()
        ).group(1)

    def preview(self, data, fmt="json", client=None):
        client = client or self.admin_client
        return client.post(
            URL,
            {
                "csrfmiddlewaretoken": self.token(client),
                "stage": "preview",
                "format": fmt,
                "vehicle": SimpleUploadedFile(f"vehicle.{fmt}", data),
            },
        )

    def confirm_fields(self, response):
        html = response.content.decode()
        fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', html))
        fields.pop("stage", None)
        return fields

    def confirm(self, fields):
        return self.admin_client.post(
            URL,
            {"csrfmiddlewaretoken": self.token(self.admin_client), "stage": "confirm", **fields},
        )

    def test_crew_is_refused_and_the_denial_is_audited(self):
        client, crew = self.harness.sign_in("crew")
        vehicle = self.vehicles[0].pk
        for path in (URL, URL + "template.json", f"{URL}{vehicle}/export.json"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 403)
        self.assertTrue(AuditEvent.objects.filter(actor_user=crew, outcome="DENIED").exists())

    def test_supervisors_download_but_cannot_import(self):
        client, _ = self.harness.sign_in("supervisor")
        page = client.get(URL)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Blank template (CSV)")
        self.assertNotContains(page, "Preview import")
        template = client.get(URL + "template.csv")
        self.assertEqual(template["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn('filename="vehicle-template.csv"', template["Content-Disposition"])
        self.assertEqual(
            vehicle_files.parse(template.content, "csv"), vehicle_files.TEMPLATE_VEHICLE
        )
        self.assertEqual(client.get(URL + "template.xlsx").status_code, 404)
        self.assertEqual(self.preview(self.file(), client=client).status_code, 403)
        self.assertFalse(Vehicle.objects.filter(key=VIN).exists())

    def test_preview_writes_nothing_then_confirm_imports(self):
        before = (Vehicle.objects.count(), AuditEvent.objects.count())
        preview = self.preview(self.file())
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "Vehicle: created")
        self.assertContains(preview, "3 added, 0 renamed")
        self.assertContains(preview, "Schedule: assigned test-0600")
        self.assertEqual((Vehicle.objects.count(), AuditEvent.objects.count()), before)
        fields = self.confirm_fields(preview)
        self.assertEqual(base64.b64decode(fields["data"]), self.file())
        done = self.confirm(fields)
        self.assertEqual(done.status_code, 302)
        self.assertIn("imported=1", done["Location"])
        landing = self.admin_client.get(done["Location"])
        self.assertContains(landing, "Imported.")
        self.assertContains(landing, VIN)
        self.assertEqual(
            AuditEvent.objects.filter(command__command_type="VEHICLE_IMPORT").count(), 1
        )
        again = self.confirm(fields)
        self.assertEqual(again.status_code, 302, "a replay of the same confirmation succeeds")
        self.assertEqual(Vehicle.objects.filter(key=VIN).count(), 1)

    def test_a_stale_tampered_or_reused_confirmation_is_refused(self):
        fields = self.confirm_fields(self.preview(self.file()))
        swapped = self.confirm(
            {**fields, "data": base64.b64encode(self.file(display_name="Other")).decode()}
        )
        self.assertEqual(swapped.status_code, 409, "a different file does not match the token")
        for name, value in (("data", "%%%"), ("operation_id", "not-a-uuid")):
            with self.subTest(field=name):
                self.assertEqual(self.confirm({**fields, name: value}).status_code, 400)
        self.assertFalse(Vehicle.objects.filter(key=VIN).exists())
        self.assertEqual(self.confirm(fields).status_code, 302)
        other = self.confirm_fields(self.preview(self.file(display_name="Renamed")))
        stale = self.confirm({**other, "operation_id": fields["operation_id"]})
        self.assertEqual(stale.status_code, 409)
        self.assertContains(stale, "preview it again", status_code=409)

    def test_malformed_and_missing_uploads_show_errors_without_writing(self):
        bad = self.preview(b"{not json")
        self.assertEqual(bad.status_code, 400)
        self.assertNotContains(bad, 'name="stage" value="confirm"', status_code=400)
        invalid = self.preview(self.file(vin="NOT-A-VIN"))
        self.assertEqual(invalid.status_code, 400)
        self.assertContains(invalid, "vin", status_code=400)
        missing = self.admin_client.post(
            URL,
            {
                "csrfmiddlewaretoken": self.token(self.admin_client),
                "stage": "preview",
                "format": "json",
            },
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(self.preview(self.file(), fmt="xlsx").status_code, 400)
        unknown = self.admin_client.post(
            URL, {"csrfmiddlewaretoken": self.token(self.admin_client), "stage": "other"}
        )
        self.assertEqual(unknown.status_code, 400)
        self.assertFalse(Vehicle.objects.filter(key=VIN).exists())

    def test_export_is_audited_and_needs_a_vin(self):
        self.assertContains(self.admin_client.get(URL), "Set the VIN first")
        no_vin = self.admin_client.get(f"{URL}{self.vehicles[0].pk}/export.json")
        self.assertEqual(no_vin.status_code, 409)
        unknown = self.admin_client.get(f"{URL}{uuid.uuid4()}/export.json")
        self.assertEqual(unknown.status_code, 404)
        self.confirm(self.confirm_fields(self.preview(self.file())))
        vehicle = Vehicle.objects.get(key=VIN)
        exported = self.admin_client.get(f"{URL}{vehicle.pk}/export.json")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported["Content-Type"], "application/json; charset=utf-8")
        self.assertIn(f'filename="vehicle-{VIN}.json"', exported["Content-Disposition"])
        self.assertEqual(vehicle_files.parse(exported.content, "json")["vin"], VIN)
        self.assertTrue(AuditEvent.objects.filter(command__command_type="VEHICLE_EXPORT").exists())
        self.assertEqual(self.admin_client.get(f"{URL}{vehicle.pk}/export.xlsx").status_code, 404)
