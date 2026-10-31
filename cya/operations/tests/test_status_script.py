"""Execute the offline status client harness when the optional local Node tool is available."""

import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase


class StatusScriptTests(SimpleTestCase):
    @skipUnless(shutil.which("node"), "optional Node client harness is unavailable")
    def test_offline_status_client_regressions(self):
        script = Path(__file__).with_name("status_script_harness.js")
        result = subprocess.run(
            [shutil.which("node"), str(script)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status script scenarios passed", result.stdout)
