"""Printable outage checklist (issue #9 Part A, SPEC §17.8 DEP-11)."""

import re
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
from cya.kits.sealable import designate_compartment_sealable, designate_kit_sealable


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


class OutageSheetTests(SimpleTestCase):
    def test_rows_group_by_place_in_order_and_each_kit_is_one_row(self):
        rows = [
            row(item_label="Kit gauze", kit_id="k1", kit_label="Trauma 1", compartment_label="C2"),
            row(item_label="Cab light", compartment_id="c1", compartment_label="Cab"),
            row(item_label="Kit tape", kit_id="k1", kit_label="Trauma 1", compartment_label="C2"),
            row(item_label="Siren", system_id="s1", system_label="Lights and sirens"),
            row(item_label="Kit gauze", kit_id="k2", kit_label="Trauma 2"),
            row(item_label="Cab mirror", compartment_id="c1", compartment_label="Cab"),
            row(item_label="Odometer", kind="VEHICLE_READING", allowed_outcomes=()),
        ]
        sections, kits = printable.outage_sheet(rows)
        self.assertEqual(
            [(s.heading, [r.label for r in s.rows]) for s in sections],
            [
                ("Cab", ["Cab light", "Cab mirror"]),
                ("Lights and sirens", ["Siren"]),
                ("Vehicle-wide", ["Odometer"]),
            ],
        )
        # A kit's contents are on the kit reference, not the outage sheet.
        self.assertEqual([(k.label, k.place) for k in kits], [("Trauma 1", "C2"), ("Trauma 2", "")])

    def test_each_kind_gets_the_right_blank_and_expected_quantity(self):
        presence, quantity, measurement, reading = (
            printable.outage_sheet([r])[0][0].rows[0]
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

    def test_css_strings_cannot_break_out_of_the_style_element(self):
        quoted = printable.css_string('A "b" </style><script>x; \\ é')
        self.assertTrue(quoted.startswith('"') and quoted.endswith('"'))
        inner = quoted[1:-1]
        for dangerous in ('"', "<", ">", ";", "/"):
            self.assertNotIn(dangerous, inner)
        self.assertIn("\\000022", inner)  # the quote, as a 6-digit CSS escape
        self.assertEqual(printable.css_string("Plain, 1.0 (x)-y:z"), '"Plain, 1.0 (x)-y:z"')

    def test_the_reference_page_header_names_every_revision_and_version_id(self):
        header = printable.reference_page_header(
            [
                {"label": "Daily, revision 2", "id": "id-1"},
                {"label": "Weekly, revision 5", "id": "id-2"},
            ]
        )
        self.assertEqual(
            header, '"Kit reference: Daily, revision 2 (id-1)\\00003b Weekly, revision 5 (id-2)"'
        )
        self.assertEqual(printable.reference_page_header([]), '"Kit reference"')

    def test_no_rows_means_nothing_to_print(self):
        self.assertEqual(printable.outage_sheet([]), ([], []))

    def test_a_sealable_compartment_prints_one_sealed_section_and_marks_its_kits(self):
        rows = [
            row(item_label="Cab light", compartment_id="c1", compartment_label="Cab"),
            row(item_label="Drug box", compartment_id="c2", compartment_label="Locker"),
            row(
                item_label="Kit gauze",
                kit_id="k1",
                kit_label="Trauma 1",
                compartment_id="c2",
                compartment_label="Locker",
            ),
            row(
                item_label="Kit gauze",
                kit_id="k2",
                kit_label="Trauma 2",
                compartment_id="c3",
                compartment_label="Shelf",
            ),
            row(
                item_label="Kit tape",
                kit_id="k3",
                kit_label="Airway",
                compartment_id="c4",
                compartment_label="Bench",
            ),
        ]
        sections, kits = printable.outage_sheet(
            rows, sealable_compartments=frozenset({"c2", "c4"}), sealed_kits=frozenset({"k2"})
        )
        self.assertEqual(
            [(s.heading, s.sealed, [r.label for r in s.rows]) for s in sections],
            [
                ("Cab", False, ["Cab light"]),
                ("Locker", True, []),  # its items are covered by the cabinet seal (CAB-9)
                ("Bench", True, []),  # a sealable compartment holding only a kit still prints
            ],
        )
        self.assertEqual(
            [(k.label, k.sealed, k.in_sealed_compartment) for k in kits],
            [("Trauma 1", False, True), ("Trauma 2", True, False), ("Airway", False, True)],
        )


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
        self.assertIn(self.masks.label, text)
        # Each kit instance is one row; its contents are on the kit reference instead.
        self.assertNotIn(self.kit_item.label, text)
        # With no designations, kits are not sealed: each is one contents-checked row.
        self.assertEqual(html.count("Contents checked against the kit reference"), 2)
        self.assertNotIn("Sealed cabinet", html)
        self.assertIn("Reference", text)
        self.assertIn("then shred it", text)
        self.assertIn('href="/inspection/kit-reference/"', html)
        # The repeated header on every printed page carries the version ID, not only the masthead.
        repeated = re.findall(r'<tr class="repeat-identity">(.*?)</tr>', html, re.S)
        self.assertGreaterEqual(len(repeated), 2)  # one per printed table: each section, and kits
        for row in repeated:
            self.assertIn(str(version.pk), row)

    def test_designated_cabinets_and_kits_print_seal_rows(self):
        designate_kit_sealable(kit_id=self.kit_b, sealable=True, **self.context(self.supervisor))
        html = self.client.get(self.url()).content.decode()
        self.assertEqual(html.count('class="sealed-kit"'), 1)  # kit B only
        self.assertEqual(html.count("Contents checked against the kit reference"), 1)
        self.assertNotIn("Inside a sealed cabinet", html)

        designate_compartment_sealable(
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            sealable=True,
            **self.context(self.supervisor),
        )
        html = self.client.get(self.url()).content.decode()
        text = strip_tags(html)
        self.assertEqual(html.count('class="sealed-compartment"'), 1)
        self.assertIn("An intact seal covers every item and kit inside", text)
        self.assertNotIn(self.suction.label, text)  # the cabinet's items are covered by its seal
        self.assertIn(self.siren.label, text)  # a vehicle-system item is not in the cabinet
        self.assertEqual(text.count("Inside a sealed cabinet"), 2)  # both kits are in it

    def test_the_printed_instant_uses_the_station_zone_with_its_offset(self):
        self.org.station_zone = "America/New_York"
        self.org.save(update_fields=["station_zone"])
        for url in (self.url(), "/inspection/kit-reference/"):
            with self.subTest(url=url):
                text = strip_tags(self.client.get(url).content.decode())
                self.assertRegex(
                    text, r"in force at \d{4}-\d{2}-\d{2} \d{2}:\d{2} E[SD]T \(-0[45]00\)"
                )
                self.assertNotIn(" UTC", text.split("in force at", 1)[1][:40])

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


@override_settings(LOGGING_CONFIG=None)
class KitReferencePageTests(CrewUiCase):
    URL = "/inspection/kit-reference/"

    def test_the_reference_lists_each_kit_type_once_with_its_revision(self):
        page = self.client.get(self.URL)
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        text = strip_tags(html)
        template = ChecklistTemplate.objects.get(pk=self.template.pk)
        version = ChecklistVersion.objects.get(
            pk=vehicle_checklist(str(self.org.pk), str(self.vehicle.pk))
            .rows[0]
            .checklist_version_id
        )
        self.assertIn(f"{template.label}, revision {version.revision}", text)
        self.assertIn(str(version.pk), html)
        # Every printed page repeats the revision and version ID in its top margin.
        self.assertRegex(
            html, r"@page \{ @top-left \{ content: \"Kit reference: [^\"]*" + str(version.pk)
        )
        # Two kit instances share one kit type, so its contents appear once.
        self.assertEqual(text.count(self.kit_item.label), 1)
        self.assertNotIn(self.masks.label, text)  # loose items are not kit contents
        self.assertIn("laminate", text)
        self.assertNotIn(self.user.display_name, html)
        self.assertNotIn("Sign out", html)

    def test_a_template_without_a_version_in_force_is_left_out(self):
        ChecklistTemplate.objects.create(organization=self.org, label="Synthetic unpublished")
        text = strip_tags(self.client.get(self.URL).content.decode())
        self.assertNotIn("Synthetic unpublished", text)
        self.assertEqual(text.count(self.kit_item.label), 1)

    def test_with_no_kit_contents_in_force_there_is_nothing_to_print(self):
        with mock.patch("cya.inspections.printable.kit_reference", return_value=[]):
            text = strip_tags(self.client.get(self.URL).content.decode())
        self.assertIn("nothing to print", text)

    def test_signed_out_and_no_role_are_refused(self):
        anonymous = Client().get(self.URL)
        self.assertEqual(
            (anonymous.status_code, anonymous["Location"]), (302, sign_in_location(self.URL))
        )
        with override_settings(CYA_ROLE_LOOKUP=None):
            self.assertEqual(self.client.get(self.URL).status_code, 403)

    def test_a_failed_read_prints_nothing(self):
        with (
            mock.patch("cya.inspections.printable.kit_reference", side_effect=DatabaseError("x")),
            self.assertLogs("cya.inspections.views", "ERROR"),
        ):
            page = self.client.get(self.URL)
        self.assertEqual(page.status_code, 503)
        self.assertNotIn("laminate", page.content.decode())

    def test_an_unbounded_template_count_is_refused_rather_than_read(self):
        with (
            mock.patch.object(printable, "REFERENCE_TEMPLATE_LIMIT", 0),
            self.assertLogs("cya.inspections.views", "ERROR"),
        ):
            page = self.client.get(self.URL)
        self.assertEqual(page.status_code, 503)
