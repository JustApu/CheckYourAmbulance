"""Execute the offline client harness when the optional local Node tool is available."""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import skipUnless

from django.test import SimpleTestCase

from cya.inspections.workflows import _determination_leaves


class DeterminationHistoryTests(SimpleTestCase):
    def test_supersession_not_uuid_order_selects_the_current_determination(self):
        original = SimpleNamespace(
            pk="z-parent", obligation_id="obligation", prior_determination_id=None
        )
        child = SimpleNamespace(
            pk="a-child", obligation_id="obligation", prior_determination_id="z-parent"
        )
        for rows in ([child, original], [original, child]):
            self.assertIs(_determination_leaves(rows)["obligation"], child)

    def test_incomplete_or_branched_determinations_fail_closed(self):
        parent = SimpleNamespace(
            pk="parent", obligation_id="obligation", prior_determination_id=None
        )
        child = SimpleNamespace(
            pk="child", obligation_id="obligation", prior_determination_id="parent"
        )
        branch = SimpleNamespace(
            pk="branch", obligation_id="obligation", prior_determination_id="parent"
        )
        for rows in ([child], [parent, child, branch]):
            with self.assertRaises(ValueError):
                _determination_leaves(rows)


class WorkflowScriptTests(SimpleTestCase):
    @skipUnless(shutil.which("node"), "optional Node client harness is unavailable")
    def test_offline_client_workflow_regressions(self):
        script = Path(__file__).with_name("workflow_script_harness.js")
        result = subprocess.run(
            [shutil.which("node"), str(script)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
