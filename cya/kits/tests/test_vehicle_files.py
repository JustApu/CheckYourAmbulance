"""Vehicle file parsing and rendering (SPEC 2.1.0 IMP-1, IMP-4, IMP-6, IMP-7; issue #129)."""

import copy
import json

from django.test import SimpleTestCase

from cya.kits import vehicle_files
from cya.kits.library_files import FileErrors


class RoundTripTests(SimpleTestCase):
    def test_a_template_parses_back_to_its_vehicle_in_both_formats(self):
        for fmt in vehicle_files.FORMATS:
            with self.subTest(fmt=fmt):
                parsed = vehicle_files.parse(vehicle_files.template(fmt), fmt)
                self.assertEqual(parsed, vehicle_files.TEMPLATE_VEHICLE)
                again = vehicle_files.render(parsed, fmt)
                self.assertEqual(again, vehicle_files.template(fmt), "render is stable")

    def test_formula_cells_survive_a_csv_round_trip(self):
        vehicle = copy.deepcopy(vehicle_files.TEMPLATE_VEHICLE)
        vehicle["display_name"] = "=HYPERLINK(1)"
        vehicle["items"][0]["instructions"] = "'quoted"
        data = vehicle_files.render(vehicle, "csv")
        self.assertIn(b"'=HYPERLINK(1)", data)
        self.assertEqual(vehicle_files.parse(data, "csv"), vehicle)

    def test_expiration_tracked_round_trips_and_defaults_to_no(self):
        # Issue #152: the setting is never silently dropped.
        vehicle = copy.deepcopy(vehicle_files.TEMPLATE_VEHICLE)
        vehicle["items"][0]["expiration_tracked"] = True
        for fmt in vehicle_files.FORMATS:
            with self.subTest(fmt=fmt):
                parsed = vehicle_files.parse(vehicle_files.render(vehicle, fmt), fmt)
                self.assertEqual(parsed, vehicle)
        document = json.loads(vehicle_files.render(vehicle, "json"))
        del document["vehicle"]["items"][0]["expiration_tracked"]
        parsed = vehicle_files.parse(json.dumps(document).encode(), "json")
        self.assertIs(parsed["items"][0]["expiration_tracked"], False)
        csv_text = vehicle_files.render(vehicle, "csv").decode()
        self.assertIn(",yes,", csv_text)
        parsed = vehicle_files.parse(csv_text.replace(",yes,", ",,").encode(), "csv")
        self.assertIs(parsed["items"][0]["expiration_tracked"], False)
        with self.assertRaises(FileErrors) as caught:
            vehicle_files.parse(csv_text.replace(",yes,", ",maybe,").encode(), "csv")
        self.assertTrue(any("expiration_tracked" in e for e in caught.exception.errors))


class MalformedFileTests(SimpleTestCase):
    def errors(self, data, fmt):
        with self.assertRaises(FileErrors) as caught:
            vehicle_files.parse(data, fmt)
        return caught.exception.errors

    def json_file(self, **changes):
        document = json.loads(vehicle_files.template("json"))
        document.update(changes)
        return json.dumps(document).encode()

    def test_json_problems_are_all_reported_with_their_paths(self):
        document = json.loads(vehicle_files.template("json"))
        document["vehicle"]["colour"] = "red"
        document["vehicle"]["items"][0]["extra"] = 1
        document["vehicle"]["kits"] = {"not": "a list"}
        errors = self.errors(json.dumps(document).encode(), "json")
        self.assertEqual(len(errors), 3)
        self.assertTrue(any(e.startswith("vehicle:") for e in errors))
        self.assertTrue(any(e.startswith("vehicle.items[0]") for e in errors))
        self.assertTrue(any(e.startswith("vehicle.kits") for e in errors))

    def test_json_file_type_and_version_are_checked(self):
        self.assertTrue(self.errors(self.json_file(file_type="kit-library"), "json"))
        self.assertTrue(self.errors(self.json_file(schema_version=2), "json"))
        self.assertTrue(self.errors(b"[]", "json"))
        self.assertTrue(self.errors(b"{not json", "json"))

    def csv_lines(self):
        return vehicle_files.template("csv").decode().splitlines()

    def test_csv_needs_exactly_one_vehicle_row(self):
        lines = self.csv_lines()
        twice = "\r\n".join([*lines, lines[1]]).encode()
        self.assertTrue(any("exactly one vehicle" in e for e in self.errors(twice, "csv")))
        none = "\r\n".join([lines[0], *lines[2:]]).encode()
        self.assertTrue(any("no vehicle row" in e for e in self.errors(none, "csv")))

    def test_csv_rows_use_only_their_record_columns(self):
        header = vehicle_files.CSV_COLUMNS
        cells = {c: "" for c in header} | {
            "file_type": "vehicle",
            "record": "kit",
            "kit": "example-airway-kit",
            "compartment": "example-cabinet-1",
            "count": "2",
            "label": "stray",
        }
        lines = [*self.csv_lines(), ",".join(cells[c] for c in header)]
        errors = self.errors("\r\n".join(lines).encode(), "csv")
        self.assertTrue(any("leaves ['label'] empty" in e for e in errors))

    def test_csv_header_record_type_and_counts_are_checked(self):
        lines = self.csv_lines()
        self.assertTrue(self.errors(("a,b\r\n" + "\r\n".join(lines[1:])).encode(), "csv"))
        bad = lines[1].replace(",vehicle,", ",lorry,", 1)
        self.assertTrue(self.errors("\r\n".join([lines[0], bad]).encode(), "csv"))
        kit_row = next(line for line in lines if line.startswith("vehicle,kit,"))
        count = "\r\n".join([*lines, kit_row.rsplit(",", 1)[0] + ",two"]).encode()
        self.assertTrue(any("count" in e for e in self.errors(count, "csv")))

    def test_bounds_and_encoding(self):
        self.assertTrue(self.errors(b"", "json"))
        self.assertTrue(self.errors(b"\xff\xfe", "csv"))
        self.assertTrue(self.errors(b"x" * (1_000_001), "json"))
        self.assertTrue(self.errors(vehicle_files.template("json"), "xlsx"))
