import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github/workflows/checks.yml").read_text(encoding="utf-8")


class BrowserInstallWorkflowTests(unittest.TestCase):
    def test_mirror_probe_precedes_dependency_install_and_smoke(self):
        probe = WORKFLOW.index("Verify apt mirror fallback and configure runner mirrors")
        install = WORKFLOW.index("Install pinned Chromium and system dependencies")
        smoke = WORKFLOW.index("Cross-device browser smoke")
        self.assertLess(probe, install)
        self.assertLess(install, smoke)
        self.assertIn("--simulate-primary-failure", WORKFLOW[probe:install])
        self.assertIn("archive.ubuntu.com", WORKFLOW[probe:install])
        self.assertIn("sudo apt-get -o Debug::Acquire::http=true update", WORKFLOW[probe:install])

    def test_only_install_is_retried_with_bounded_attempts(self):
        install = WORKFLOW.index("Install pinned Chromium and system dependencies")
        smoke = WORKFLOW.index("Cross-device browser smoke")
        block = WORKFLOW[install:smoke]
        self.assertIn("for attempt in 1 2", block)
        self.assertIn(
            "timeout --kill-after=15s 180s npx playwright install --with-deps chromium", block
        )
        self.assertNotIn("test:browser-smoke", block)

    def test_full_checks_remain_enabled_for_main_and_manual_dispatch(self):
        self.assertIn("  push:\n    branches: [main]", WORKFLOW)
        self.assertIn("  workflow_dispatch:\n", WORKFLOW)
        self.assertIn('if [ "${{ github.event_name }}" != "pull_request" ]; then', WORKFLOW)


if __name__ == "__main__":
    unittest.main()
