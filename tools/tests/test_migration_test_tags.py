"""Offline tests for tools/migration_test_tags.py (issue #158)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import migration_test_tags as tags  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


class UntaggedClassTests(unittest.TestCase):
    def test_direct_executor_use_needs_the_tag(self):
        source = (
            "from django.db.migrations.executor import MigrationExecutor\n"
            "class A:\n    def test(self):\n        MigrationExecutor(None).migrate([])\n"
        )
        self.assertEqual(tags.untagged_classes(source), ["A"])
        tagged = '@tag("migration_rollback")\n' + source.split("\n", 1)[1]
        self.assertEqual(tags.untagged_classes(tagged), [])

    def test_call_command_migrate_and_module_helpers_count(self):
        source = (
            "def reverse():\n    call_command('migrate', 'app', '0001')\n"
            "class Uses:\n    def test(self):\n        reverse()\n"
            "class Other:\n    def test(self):\n        call_command('check')\n"
        )
        self.assertEqual(tags.untagged_classes(source), ["Uses"])

    def test_a_tag_with_another_name_does_not_count(self):
        source = '@tag("slow")\nclass A:\n    def test(self):\n        executor.migrate([])\n'
        self.assertEqual(tags.untagged_classes(source), ["A"])

    def test_exit_codes_follow_the_repository_check_convention(self):
        import io
        import tempfile
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()) as out:
            tests = Path(root) / "app" / "tests"
            tests.mkdir(parents=True)
            module = tests / "test_x.py"
            module.write_text("class A:\n    def test(self):\n        executor.migrate([])\n")
            self.assertEqual(tags.main(["prog", root]), 1)
            self.assertIn("app/tests/test_x.py: class A", out.getvalue())
            module.write_text('@tag("migration_rollback")\nclass A:\n    executor.migrate([])\n')
            self.assertEqual(tags.main(["prog", root]), 0)
            module.write_text("class (:\n")
            self.assertEqual(tags.main(["prog", root]), 2)

    def test_the_repository_has_no_untagged_migration_test(self):
        self.assertEqual(tags.scan(REPO / "cya"), [])


if __name__ == "__main__":
    unittest.main()
