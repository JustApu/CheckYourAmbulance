"""Printable outage checklist (issue #9 Part A, SPEC §17.8 DEP-11)."""

from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError
from django.test import Client, SimpleTestCase, override_settings
from django.utils.html import strip_tags

from cya.accounts.returns import sign_in_location
from cya.attention.checklist import vehicle_checklist
from cya.checklists.models import ChecklistTemplate, ChecklistVersion, Vehicle
from cya.inspections import printable
from cya.inspections.tests.test_crew_ui import CrewUiCase, PageTests


def row(**over):
    base = {
        "item_label": "Suction unit",
        "instructions": "",
        "allowed_outcomes": ("PASSED", "FAILED", "UNABLE_TO_INSPECT", "NOT_APPLICABLE"),
        "kind": "PRESENCE_CONDITION",
        "expected_quantity": None,
        "unit": None,
        "kit_id": None,
        "kit_label": None,
        "compartment_id": None,
        "compartment_label": None,
        "system_id": None,
        "system_label": None,
        "checklist_version_id": "",
    }
    return SimpleNamespace(**(base | over))


class SectionTests(SimpleTestCase):
    def test_rows_group_by_place_in_order_and_kits_come_last_one_section_each(self):
        rows = [
            row(item_label="Kit gauze", kit_id="k1", kit_label="Trauma 1", compartment_label="C2"),
            row(item_label="Cab light", compartment_id="c1", compartment_label="Cab"),
            row(item_label="Siren", system_id="s1", system_label="Lights and sirens"),
            row(item_label="Kit gauze", kit_id="k2", kit_label="Trauma 2"),
            row(item_label="Cab mirror", compartment_id="c1", compartment_label="Cab"),
            row(item_label="Odometer", kind="VEHICLE_READING", allowed_outcomes=()),
        ]
        found = printable.sections(rows)
        self.assertEqual(
            [(s.heading, s.kit, [r.label for r in s.rows]) for s in found],
            [
                ("Cab", False, ["Cab light", "Cab mirror"]),
                ("Lights and sirens", False, ["Siren"]),
                ("Vehicle-wide", False, ["Odometer"]),
                ("Kit: Trauma 1 (C2)", True, ["Kit gauze"]),
                ("Kit: Trauma 2", True, ["Kit gauze"]),
            ],
        )

    def test_each_kind_gets_the_right_blank_and_expected_quantity(self):
        presence, quantity, measurement, reading = (
            printable.sections([r])[0].rows[0]
            for r in (
                row(),
                row(kind="QUANTITY", expected_quantity=6, unit="masks"),
                row(kind="MEASUREMENT", unit="psi", allowed_outcomes=("PASSED", "FAILED")),
                row(kind="VEHICLE_READING", allowed_outcomes=()),
            )
        )
        self.assertEqual(presence.outcomes, ("Pass", "Fail", "Unable", "N/A"))
        self.assertIsNone(presence.value_unit)
        self.assertEqual((quantity.expected, quantity.value_unit), ("6 masks", "masks"))
        self.assertEqual((measurement.outcomes, measurement.value_unit), (("Pass", "Fail"), "psi"))
        self.assertEqual((reading.outcomes, reading.value_unit, reading.expected), ((), "", ""))

    def test_no_rows_means_no_sections(self):
        self.assertEqual(printable.sections([]), [])


@override_settings(LOGGING_CONFIG=None)
class PrintPageTests(CrewUiCase):
    def url(self, vehicle_id=None):
        return f"/inspection/vehicles/{vehicle_id or self.vehicle.pk}/print/"

    def test_the_sheet_lists_the_current_checklist_with_its_version_and_blank_columns(self):
        page = self.client.get(self.url())
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        rows = vehicle_checklist(str(self.org.pk), str(self.vehicle.pk)).rows
        in_force = {r.checklist_version_id for r in rows}
        self.assertEqual(len(in_force), 1)
        version = ChecklistVersion.objects.get(pk=in_force.pop())
        for other in ChecklistVersion.objects.filter(template=self.template).exclude(pk=version.pk):
            self.assertNotIn(str(other.pk), html)  # only the version in force is printed
        template = ChecklistTemplate.objects.get(pk=self.template.pk)
        self.assertIn(f"{template.label}, revision {version.revision}", html)
        self.assertIn(str(version.pk), html)
        self.assertIn("Check Your Ambulance", html)
        for heading in ("Item", "Result", "Count or value", "Initials", "Time"):
            self.assertIn(f'<th scope="col">{heading}</th>', html)
        text = strip_tags(html)
        for item in (self.masks, self.kit_item):
            self.assertIn(item.label, text)
        self.assertEqual(html.count('class="sheet-section kit-section"'), 2)  # one per kit instance
        self.assertIn("Kit: Reference", text)
        self.assertIn("Kit: B", text)
        self.assertIn("then shred it", text)

    def test_the_sheet_carries_no_personal_data_or_results(self):
        self.open()  # a session and participant exist; neither may appear on paper
        html = self.client.get(self.url()).content.decode()
        self.assertNotIn(self.user.display_name, html)
        self.assertNotIn(self.user.email, html)
        self.assertNotIn("Sign out", html)
        self.assertNotIn("csrfmiddlewaretoken", html)

    def test_the_vehicle_page_links_to_the_sheet(self):
        html = self.client.get(f"/inspection/vehicles/{self.vehicle.pk}/").content.decode()
        self.assertIn(f'href="{self.url()}"', html)
        self.assertIn("Paper checklist for outages", html)

    def test_names_are_escaped(self):
        Vehicle.objects.filter(pk=self.vehicle.pk).update(display_name="<b>x</b> & Co")
        html = self.client.get(self.url()).content.decode()
        self.assertNotIn("<b>x</b>", html)
        self.assertIn("&lt;b&gt;x&lt;/b&gt; &amp; Co", html)

    def test_signed_out_wrong_ids_inactive_other_org_and_no_role_are_refused(self):
        anonymous = Client().get(self.url())
        self.assertEqual(
            (anonymous.status_code, anonymous["Location"]), (302, sign_in_location(self.url()))
        )
        for value in (str(uuid4()), "not-a-uuid"):
            with self.subTest(value=value):
                self.assertEqual(self.client.get(self.url(value)).status_code, 404)
        other = PageTests.make_other_org_vehicle(self)
        self.assertEqual(self.client.get(self.url(other.pk)).status_code, 404)
        with override_settings(CYA_ROLE_LOOKUP=None):
            self.assertEqual(self.client.get(self.url()).status_code, 403)
        Vehicle.objects.filter(pk=self.vehicle.pk).update(active=False)
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_a_failed_checklist_read_never_prints_a_partial_sheet(self):
        for failure in (DatabaseError("down"), ValueError("unresolvable")):
            with self.subTest(failure=type(failure).__name__):
                with (
                    mock.patch("cya.inspections.views.vehicle_checklist", side_effect=failure),
                    self.assertLogs("cya.inspections.views", "ERROR"),
                ):
                    page = self.client.get(self.url())
                self.assertEqual(page.status_code, 503)
                self.assertNotIn("then shred it", page.content.decode())

    def test_a_version_outside_the_organization_is_refused(self):
        stray = SimpleNamespace(**{**vars(row()), "checklist_version_id": str(uuid4())})
        with self.assertRaises(ValueError):
            printable.versions(str(self.org.pk), [stray])
