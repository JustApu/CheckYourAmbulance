"""Vehicle screens over HTTP (SPEC 4.8 CFG-1, CFG-4, CFG-6; issue #130 part 5a)."""

import re
import uuid

from django.test import override_settings

from cya.checklists.models import Compartment, Vehicle
from cya.core.models import AuditEvent
from cya.kits.positions import add_position
from cya.kits.tests.support import KitCase
from cya.operations import commands as status_commands
from cya.operations.models import VehicleActivityEvent
from cya.operations.vehicles import record_activity_baseline
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

LIST = "/administration/vehicles/"
VIN = "1FDXE4FS8KDC00051"  # synthetic
COPY_VIN = "1FDXE4FS8KDC00052"  # synthetic


@override_settings(**real_collaborator_settings())
class VehiclePagesTests(KitCase):
    def setUp(self):
        super().setUp()
        self.harness = RoleHttpHarness(organization=self.org)
        self.client_, _ = self.harness.sign_in("supervisor")
        self.url = f"{LIST}{self.vehicle.pk}/"
        self.duplicate_url = f"{self.url}duplicate/"

    def csrf(self, client, url):
        page = client.get(url)
        return re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()
        ).group(1)

    def post(self, url, client=None, **fields):
        client = client or self.client_
        fields.setdefault("operation_id", str(uuid.uuid4()))
        return client.post(url, {"csrfmiddlewaretoken": self.csrf(client, url), **fields})

    def create(self, **fields):
        return self.post(
            LIST,
            **{"stage": "create", "vin": VIN, "display_name": "Synthetic Medic 9", **fields},
        )

    def test_crew_and_supply_officers_are_refused_and_audited(self):
        for sub in ("crew", "officer"):
            client, user = self.harness.sign_in(sub)
            with self.subTest(role=sub):
                for url in (LIST, self.url, self.duplicate_url):
                    self.assertEqual(client.get(url).status_code, 403)
                self.assertTrue(
                    AuditEvent.objects.filter(actor_user=user, outcome="DENIED").exists()
                )
        officer, _ = self.harness.sign_in("officer")
        self.assertNotContains(officer.get("/administration/"), f'href="{LIST}"')

    def test_supervisors_reach_vehicles_from_administration(self):
        self.assertContains(self.client_.get("/administration/"), f'href="{LIST}"')
        listing = self.client_.get(LIST)
        self.assertContains(listing, f'href="{self.url}"')
        self.assertContains(listing, "Not recorded")  # no operational status yet
        page = self.client_.get(self.url)
        self.assertContains(page, f'href="{self.duplicate_url}"')
        self.assertContains(page, f'href="/administration/positions/{self.vehicle.pk}/"')
        self.assertEqual(self.client_.get(f"{LIST}{uuid.uuid4()}/").status_code, 404)
        # Text fields use the full-size admin form style (48 px), not the browser default
        # (issue #111 rendered check on PR #174).
        self.assertContains(listing, 'class="admin-form"', count=2)
        self.assertContains(page, 'class="admin-form"', count=1)
        self.assertContains(self.client_.get(self.duplicate_url), 'class="admin-form"', count=1)
        self.assertContains(listing, ".admin-form input:not([type=")

    def test_the_list_searches_and_keeps_active_apart_from_operational_status(self):
        status_commands.set_vehicle_status(
            vehicle_id=str(self.vehicle_b.pk),
            status="OUT_OF_SERVICE",
            explanation="Synthetic reason.",
            **self.context(self.supervisor),
        )
        listing = self.client_.get(LIST).content.decode()
        row_b = re.search(rf'href="{LIST}{self.vehicle_b.pk}/".*?</tr>', listing, re.S).group(0)
        self.assertIn("Active", row_b)
        self.assertIn("Out of Service", row_b)
        found = self.client_.get(LIST, {"q": self.vehicle_b.display_name})
        self.assertContains(found, f'href="{LIST}{self.vehicle_b.pk}/"')
        none = self.client_.get(LIST, {"q": "no-such-vehicle-zz"})
        self.assertContains(none, "No vehicle matches")
        self.assertNotContains(none, f'href="{self.url}"')

    def test_create_then_a_resubmitted_form_replays_instead_of_creating_twice(self):
        operation = str(uuid.uuid4())
        created = self.create(number="9", operation_id=operation)
        self.assertEqual(created.status_code, 302)
        vehicle = Vehicle.objects.get(key=VIN)
        self.assertEqual(created["Location"], f"{LIST}{vehicle.pk}/?saved=created")
        self.assertTrue(vehicle.active)
        self.assertEqual((vehicle.display_name, vehicle.number), ("Synthetic Medic 9", "9"))
        self.assertTrue(VehicleActivityEvent.objects.filter(vehicle=vehicle, active=True).exists())
        self.assertContains(self.client_.get(created["Location"]), "Vehicle created")
        again = self.create(number="9", operation_id=operation)
        self.assertEqual(again.status_code, 302)
        self.assertEqual(Vehicle.objects.filter(key=VIN).count(), 1)

    def test_create_refusals_keep_the_form(self):
        bad = self.create(vin="NOT-A-VIN")
        self.assertContains(bad, "vin must be 17 letters", status_code=400)
        self.assertContains(bad, 'value="Synthetic Medic 9"', status_code=400)
        self.assertEqual(self.create(display_name="  ").status_code, 400)
        self.assertEqual(self.create().status_code, 302)
        taken = self.create(display_name="Another")
        self.assertContains(taken, "another vehicle has that VIN", status_code=400)
        self.assertEqual(self.create(operation_id="not-a-uuid").status_code, 400)
        self.assertEqual(self.post(LIST, stage="other").status_code, 400)
        self.assertEqual(Vehicle.objects.filter(key=VIN).count(), 1)

    def test_edit_name_and_number(self):
        saved = self.post(self.url, stage="update", display_name="Synthetic Renamed", number="42")
        # A saved name goes back to the list, which names the vehicle (owner, 2026-11-18).
        self.assertEqual(saved["Location"], f"{LIST}?saved={self.vehicle.pk}")
        listing = self.client_.get(saved["Location"])
        # One status line, announced once (PR #218 review): the indicator carries the name.
        self.assertContains(
            listing, '<span class="save-state-text">Saved Synthetic Renamed.</span>', html=True
        )
        self.assertContains(listing, 'data-save-state="saved"', count=1)
        self.assertNotContains(
            self.client_.get(f"{LIST}?saved=not-a-vehicle"), 'data-save-state="saved"'
        )
        self.vehicle.refresh_from_db()
        self.assertEqual(
            (self.vehicle.display_name, self.vehicle.number), ("Synthetic Renamed", "42")
        )
        cleared = self.post(self.url, stage="update", display_name="Synthetic Renamed", number="")
        self.assertEqual(cleared.status_code, 302)
        self.vehicle.refresh_from_db()
        self.assertIsNone(self.vehicle.number)
        same = self.post(self.url, stage="update", display_name="Synthetic Renamed", number="")
        self.assertContains(same, "already has those values", status_code=400)
        # A refused edit keeps what was typed (PR #174 review).
        long_number = "9" * 51
        refused = self.post(self.url, stage="update", display_name="Typed name", number=long_number)
        self.assertContains(refused, "number must be empty or text", status_code=400)
        self.assertContains(refused, 'value="Typed name"', status_code=400)
        self.assertContains(refused, f'value="{long_number}"', status_code=400)
        self.post(self.url, stage="update", display_name="Synthetic Renamed", number="7")
        # A form without its number field is malformed, not a request to clear the number.
        partial = self.post(self.url, stage="update", display_name="Synthetic Other")
        self.assertContains(partial, "the form was not valid", status_code=400)
        self.vehicle.refresh_from_db()
        self.assertEqual(
            (self.vehicle.display_name, self.vehicle.number), ("Synthetic Renamed", "7")
        )

    def test_deactivation_previews_then_applies_and_a_stale_confirmation_is_refused(self):
        record_activity_baseline(self.org.pk)
        preview = self.post(self.url, stage="active-preview", active="0")
        self.assertContains(preview, "owes no routine inspection, and inspections due later")
        self.vehicle.refresh_from_db()
        self.assertTrue(self.vehicle.active)  # the preview changes nothing
        confirm = re.search(
            r'name="stage" value="active">\s*<input type="hidden" name="active" value="0">\s*'
            r'<input type="hidden" name="operation_id" value="([^"]+)"',
            preview.content.decode(),
        ).group(1)
        done = self.post(self.url, stage="active", active="0", operation_id=confirm)
        self.assertEqual(done["Location"], f"{self.url}?saved=deactivated")
        self.vehicle.refresh_from_db()
        self.assertFalse(self.vehicle.active)
        # The same confirmation again replays; a second, separate one is refused (CFG-6).
        self.assertEqual(
            self.post(self.url, stage="active", active="0", operation_id=confirm).status_code, 302
        )
        stale = self.post(self.url, stage="active", active="0")
        self.assertContains(stale, "already inactive", status_code=400)
        self.assertEqual(
            VehicleActivityEvent.objects.filter(vehicle=self.vehicle, active=False).count(), 1
        )
        self.assertContains(
            self.post(self.url, stage="active-preview", active="0"),
            "already in that state",
            status_code=400,
        )
        back = self.post(self.url, stage="active", active="1")
        self.assertEqual(back["Location"], f"{self.url}?saved=reactivated")
        self.vehicle.refresh_from_db()
        self.assertTrue(self.vehicle.active)
        self.assertEqual(self.post(self.url, stage="active", active="x").status_code, 400)

    def test_a_vehicle_without_activity_history_cannot_be_deactivated_yet(self):
        refused = self.post(self.url, stage="active", active="0")
        self.assertContains(refused, "record the activity baseline first", status_code=400)
        self.vehicle.refresh_from_db()
        self.assertTrue(self.vehicle.active)

    def test_duplicate_previews_then_copies_to_an_inactive_vehicle(self):
        preview = self.client_.get(self.duplicate_url)
        self.assertContains(preview, self.cabinet.label)
        self.assertContains(preview, "odometer and engine hours")  # never copied
        self.assertContains(preview, "None: no kit list is in force")
        token = re.search(r'name="base_token" value="([^"]+)"', preview.content.decode()).group(1)
        done = self.post(
            self.duplicate_url,
            stage="duplicate",
            vin=COPY_VIN,
            display_name="Synthetic copy",
            base_token=token,
        )
        self.assertEqual(done.status_code, 302)
        copy = Vehicle.objects.get(key=COPY_VIN)
        self.assertEqual(done["Location"], f"{LIST}{copy.pk}/?saved=duplicated")
        self.assertFalse(copy.active)
        self.assertTrue(
            Compartment.objects.filter(
                vehicle=copy, stable_slot_id=self.cabinet.stable_slot_id
            ).exists()
        )
        self.assertContains(self.client_.get(done["Location"]), "The copy is inactive")

    def test_a_changed_source_refuses_the_old_preview_and_shows_the_new_one(self):
        preview = self.client_.get(self.duplicate_url)
        token = re.search(r'name="base_token" value="([^"]+)"', preview.content.decode()).group(1)
        add_position(
            vehicle_id=str(self.vehicle.pk),
            kind="COMPARTMENT",
            label="Synthetic new drawer",
            key="new-drawer",
            display_order=None,
            **self.context(self.supervisor),
        )
        stale = self.post(
            self.duplicate_url,
            stage="duplicate",
            vin=COPY_VIN,
            display_name="Synthetic copy",
            base_token=token,
        )
        self.assertContains(stale, "changed since the preview", status_code=400)
        self.assertContains(stale, "Synthetic new drawer", status_code=400)
        self.assertContains(stale, f'value="{COPY_VIN}"', status_code=400)
        self.assertFalse(Vehicle.objects.filter(key=COPY_VIN).exists())
        missing = self.post(
            self.duplicate_url, stage="duplicate", vin=COPY_VIN, display_name="Synthetic copy"
        )
        self.assertContains(missing, "preview the duplication first", status_code=400)
        self.assertFalse(Vehicle.objects.filter(key=COPY_VIN).exists())
