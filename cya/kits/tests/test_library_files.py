"""Kit-library file parsing and rendering, without a database (SPEC 4.7, issue #129)."""

import json

from django.test import SimpleTestCase

from cya.kits import library_files as files


def json_file(kits, **top):
    document = {"file_type": "kit-library", "schema_version": 1, "kits": kits, **top}
    return json.dumps(document).encode()


KIT = {
    "key": "jump-kit",
    "label": "Jump kit",
    "contents": [
        {
            "item": "Masks",
            "kind": "QUANTITY",
            "quantity": 6,
            "outcomes": ["PASSED", "FAILED"],
            "expiration_tracked": True,
        },
        {"item": "Gauge", "kind": "MEASUREMENT", "unit": "psi"},
    ],
}


class ParseTests(SimpleTestCase):
    def test_json_parses_to_the_normal_form_with_defaults(self):
        (kit,) = files.parse(json_file([KIT]), "json")
        self.assertEqual((kit["key"], kit["label"]), ("jump-kit", "Jump kit"))
        masks, gauge = kit["contents"]
        self.assertEqual(masks["expected_quantity"], 6)
        self.assertTrue(masks["expiration_tracked"])
        self.assertEqual(gauge["allowed_outcomes"], files.DEFAULT_OUTCOMES)
        self.assertEqual(
            (gauge["unit"], gauge["instructions"], gauge["expiration_tracked"]), ("psi", "", False)
        )

    def test_every_format_round_trips(self):
        kits = files.parse(json_file([KIT]), "json")
        for fmt in files.FORMATS:
            with self.subTest(fmt=fmt):
                self.assertEqual(files.parse(files.render(kits, fmt), fmt), kits)

    def test_csv_groups_rows_by_kit_trims_names_and_keeps_order(self):
        header = ",".join(files.CSV_COLUMNS)
        text = "\r\n".join(
            [
                header,
                "kit-library, jump-kit ,Jump kit, Masks ,QUANTITY,6,,PASSED|FAILED,yes,",
                "kit-library,other-kit,Other kit,Tape,PRESENCE_CONDITION,,,,no,Check the roll",
                "kit-library,jump-kit,Jump kit,Gauge,MEASUREMENT,,psi,,,",
                "",
            ]
        )
        jump, other = files.parse(("﻿" + text).encode(), "csv")
        self.assertEqual([e["item"] for e in jump["contents"]], ["Masks", "Gauge"])
        self.assertEqual(jump["key"], "jump-kit")
        self.assertEqual(jump["contents"][0]["allowed_outcomes"], ["PASSED", "FAILED"])
        self.assertEqual(other["contents"][0]["instructions"], "Check the roll")

    def test_formula_cells_are_neutralized_on_export_and_restored_on_import(self):
        kits = [{**files.TEMPLATE_KITS[0], "label": "=HYPERLINK(1)"}]
        rendered = files.render(kits, "csv").decode()
        self.assertIn("'=HYPERLINK(1)", rendered)
        self.assertEqual(files.parse(rendered.encode(), "csv")[0]["label"], "=HYPERLINK(1)")

    def test_templates_parse_back_to_their_examples(self):
        for fmt in files.FORMATS:
            with self.subTest(fmt=fmt):
                self.assertEqual(files.parse(files.template(fmt), fmt), files.TEMPLATE_KITS)

    def test_malformed_files_report_every_problem_with_its_location(self):
        header = ",".join(files.CSV_COLUMNS)
        cases = {
            ("json", b""): "empty",
            ("json", b"\xff\xfe"): "not UTF-8",
            ("json", b"{not json"): "not valid JSON",
            ("json", b"[]"): "top level",
            ("json", json_file([KIT], file_type="vehicle")): "file_type",
            ("json", json_file([KIT], schema_version=2)): "schema_version",
            ("json", json_file([KIT], extra=1)): "unknown fields",
            ("json", json_file([])): "kits",
            ("json", json_file([{**KIT, "colour": "red"}])): "kits[0]",
            ("json", json_file([{**KIT, "contents": []}])): "kits[0].contents",
            ("json", json_file([{**KIT, "contents": ["x"]}])): "kits[0].contents[0]",
            ("csv", b"item,kind\r\nA,B\r\n"): "header",
            ("csv", f"{header}\r\nvehicle,k,K,A,QUANTITY,1,,,no,\r\n".encode()): "row 2, file_type",
            (
                "csv",
                f"{header}\r\nkit-library,k,K,A,QUANTITY,six,,,no,\r\n".encode(),
            ): "row 2, quantity",
            (
                "csv",
                f"{header}\r\nkit-library,k,K,A,QUANTITY,1,,,maybe,\r\n".encode(),
            ): "row 2, expiration_tracked",
            ("csv", f"{header}\r\nkit-library,k,K,A,QUANTITY,1,,,no\r\n".encode()): "row 2",
            (
                "csv",
                f"{header}\r\nkit-library,k,K,A,QUANTITY,1,,,no,\r\nkit-library,k,Other,B,QUANTITY,1,,,no,\r\n".encode(),
            ): "row 3, kit_label",
            ("csv", f"{header}\r\n".encode()): "no kit rows",
            ("csv", b'item,"unclosed\r\n'): "not valid CSV",
            ("xlsx", b"x"): "format",
        }
        for (fmt, data), expected in cases.items():
            with (
                self.subTest(expected=expected, fmt=fmt),
                self.assertRaises(files.FileErrors) as raised,
            ):
                files.parse(data, fmt)
            self.assertTrue(
                any(expected in e for e in raised.exception.errors), raised.exception.errors
            )

    def test_size_and_count_limits_are_enforced(self):
        with self.assertRaises(files.FileErrors):
            files.parse(b" " * (files.MAX_BYTES + 1), "json")
        many = [{**KIT, "key": f"k{n}"} for n in range(files.MAX_KITS + 1)]
        with self.assertRaises(files.FileErrors):
            files.parse(json_file(many), "json")
        errors = files._Collector()
        for n in range(files.MAX_ERRORS + 50):
            errors.add("x", str(n))
        self.assertEqual(len(errors.errors), files.MAX_ERRORS)
