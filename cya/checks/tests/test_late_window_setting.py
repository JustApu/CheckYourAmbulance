"""CYA_LATE_COMPLETION_HOURS: w_late of SPEC SCH-3 (amendment 1.26.0, issue #61)."""

import os
import runpy
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

SETTINGS_FILE = Path(settings.BASE_DIR) / "cya" / "settings.py"
NAME = "CYA_LATE_COMPLETION_HOURS"


class LateWindowSettingTests(SimpleTestCase):
    def load(self, value=None):
        environment = {k: v for k, v in os.environ.items() if k != NAME}
        if value is not None:
            environment[NAME] = value
        with mock.patch.dict(os.environ, environment, clear=True):
            return runpy.run_path(str(SETTINGS_FILE))[NAME]

    def test_the_default_is_12_hours_and_a_valid_value_is_used(self):
        self.assertEqual(self.load(), 12)
        self.assertEqual(self.load("0"), 0)  # late completion off
        self.assertEqual(self.load("168"), 168)

    def test_a_malformed_or_out_of_range_value_stops_startup(self):
        for value in ("-1", "169", "twelve", "1.5"):
            with self.subTest(value=value), self.assertRaises(ImproperlyConfigured):
                self.load(value)
