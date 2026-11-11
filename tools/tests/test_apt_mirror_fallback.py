import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from apt_mirror_fallback import FALLBACK, configure  # noqa: E402


class AptMirrorFallbackTests(unittest.TestCase):
    def test_primary_is_first_and_official_archive_is_fallback(self):
        result = configure("# runner mirror\nhttp://azure.archive.ubuntu.com/ubuntu/\n")
        self.assertEqual(
            result,
            "# runner mirror\n"
            "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
            f"{FALLBACK}\tpriority:2\n",
        )

    def test_existing_metadata_is_kept_and_priority_is_replaced(self):
        result = configure("https://mirror.example/ubuntu/\tpriority:9 arch:amd64\n")
        self.assertIn("https://mirror.example/ubuntu/\tpriority:1 arch:amd64\n", result)
        self.assertIn(f"{FALLBACK}\tpriority:2\n", result)

    def test_simulation_uses_reserved_unresolvable_domain(self):
        result = configure(
            "http://azure.archive.ubuntu.com/ubuntu/\n", simulate_primary_failure=True
        )
        self.assertIn("http://primary-unavailable.invalid/ubuntu/\tpriority:1\n", result)
        self.assertIn(f"{FALLBACK}\tpriority:2\n", result)

    def test_configuring_an_already_configured_list_is_idempotent(self):
        configured = configure("http://azure.archive.ubuntu.com/ubuntu/\n")
        self.assertEqual(configure(configured), configured)

    def test_rejects_unexpected_or_multiple_active_mirrors(self):
        for contents in (
            "# empty\n",
            "http://one.example/ubuntu/\nhttp://two.example/ubuntu/\n",
            f"http://one.example/ubuntu/\tpriority:1\n{FALLBACK}\tpriority:1\n",
            "file:/local/archive\n",
            "http://mirror.example/other/\n",
        ):
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                configure(contents)

    def test_rejects_fallback_as_primary(self):
        with self.assertRaises(ValueError):
            configure(f"{FALLBACK}\n")


if __name__ == "__main__":
    unittest.main()
