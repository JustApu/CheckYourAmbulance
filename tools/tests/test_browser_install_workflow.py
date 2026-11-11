import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github/workflows/checks.yml").read_text(encoding="utf-8")


class BrowserInstallWorkflowTests(unittest.TestCase):
    def test_fallback_probe_runs_only_on_manual_dispatch(self):
        probe = WORKFLOW.index("Verify apt mirror fallback on manual full-check run")
        configure = WORKFLOW.index("Configure runner apt mirrors")
        install = WORKFLOW.index("Install pinned Chromium and system dependencies")
        smoke = WORKFLOW.index("Cross-device browser smoke")
        probe_block = WORKFLOW[probe:configure]
        self.assertIn("if: github.event_name == 'workflow_dispatch'", probe_block)
        self.assertIn("--simulate-primary-failure", probe_block)
        self.assertIn("Dir::Etc::sourcelist=", probe_block)
        self.assertIn("Dir::Etc::sourceparts=-", probe_block)
        self.assertIn("timeout --kill-after=10s 120s sudo apt-get", probe_block)
        self.assertIn("http://archive.ubuntu.com/ubuntu/", probe_block)
        self.assertIn("::warning::The apt fallback diagnostic did not verify", probe_block)
        self.assertIn("trap restore_mirrors EXIT", probe_block)
        self.assertIn('if [ -f "$backup" ]; then', probe_block)
        self.assertIn(
            "sudo python3 tools/apt_mirror_fallback.py /etc/apt/apt-mirrors.txt",
            WORKFLOW[configure:install],
        )
        self.assertLess(probe, configure)
        self.assertLess(configure, install)
        self.assertLess(install, smoke)

    def test_fallback_probe_uses_an_exact_archive_uri_check(self):
        probe = WORKFLOW.index("Verify apt mirror fallback on manual full-check run")
        configure = WORKFLOW.index("Configure runner apt mirrors")
        probe_block = WORKFLOW[probe:configure]
        self.assertIn("http://archive.ubuntu.com/ubuntu/", probe_block)
        self.assertNotIn("grep -Fq 'archive.ubuntu.com'", probe_block)

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
