import re
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
            "timeout --kill-after=15s 300s npx playwright install --with-deps chromium", block
        )
        self.assertNotIn("test:browser-smoke", block)

    def test_the_retry_waits_for_a_leftover_apt_lock(self):
        # PR 274: attempt 1's apt-get outlived `timeout` and held the dpkg lock, so the retry
        # failed at once. apt must wait for the lock, within each attempt's time limit.
        install = WORKFLOW.index("Install pinned Chromium and system dependencies")
        smoke = WORKFLOW.index("Cross-device browser smoke")
        block = WORKFLOW[install:smoke]
        lock = re.search(r'DPkg::Lock::Timeout "(\d+)";', block)
        attempt = re.search(r"timeout --kill-after=15s (\d+)s npx playwright install", block)
        self.assertIsNotNone(lock)
        self.assertIsNotNone(attempt)
        self.assertLess(lock.start(), block.index("for attempt in 1 2"))
        self.assertIn("/etc/apt/apt.conf.d/90cya-lock-timeout", block)
        self.assertLess(int(lock.group(1)), int(attempt.group(1)))

    def test_the_job_cap_covers_two_attempts(self):
        start = WORKFLOW.index("  browser-smoke:\n")
        job = WORKFLOW[start : WORKFLOW.index("\n  django:\n", start)]
        cap = re.search(r"timeout-minutes: (\d+)", job)
        attempt = re.search(r"timeout --kill-after=15s (\d+)s npx playwright install", job)
        # Two attempts with their kill grace, plus at least 3 minutes for setup and the smoke run.
        self.assertGreaterEqual(int(cap.group(1)) * 60, 2 * (int(attempt.group(1)) + 15) + 180)

    def test_full_checks_remain_enabled_for_main_and_manual_dispatch(self):
        self.assertIn("  push:\n    branches: [main]", WORKFLOW)
        self.assertIn("  workflow_dispatch:\n", WORKFLOW)
        self.assertIn('if [ "${{ github.event_name }}" != "pull_request" ]; then', WORKFLOW)


if __name__ == "__main__":
    unittest.main()
