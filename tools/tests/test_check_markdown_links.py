"""Offline tests for tools/check_markdown_links.py (synthetic files only)."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_markdown_links as links  # noqa: E402


class SlugTests(unittest.TestCase):
    def test_matches_github_anchor_rules(self):
        cases = {
            "17.5 Hosting, database engine, backup (OQ-24)": "175-hosting-database-engine-backup-oq-24",
            "Appendix A. Stack comparison note (non-binding)": "appendix-a-stack-comparison-note-non-binding",
            "Roadmap, milestones and dependencies": "roadmap-milestones-and-dependencies",
            "A — B": "a--b",
            "**Bold** and `code`": "bold-and-code",
            "[Linked](other.md) heading": "linked-heading",
            "snake_case name": "snake_case-name",
            "Use C#": "use-c",
        }
        for heading, expected in cases.items():
            with self.subTest(heading=heading):
                self.assertEqual(links.github_slug(heading), expected)

    def test_duplicate_headings_get_numbered_suffixes(self):
        anchors = links.heading_anchors("# Notes\n## Notes\n### Notes\n")
        self.assertEqual(anchors, {"notes", "notes-1", "notes-2"})

    def test_closing_hashes_need_a_preceding_space(self):
        self.assertEqual(links.heading_anchors("## Closed ##\n## C#\n"), {"closed", "c"})

    def test_headings_inside_code_fences_are_ignored(self):
        anchors = links.heading_anchors("# Real\n```\n# Fake\n```\n~~~~\n# Also fake\n~~~~\n")
        self.assertEqual(anchors, {"real"})


class CheckFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "docs").mkdir()
        (self.root / "docs" / "guide.md").write_text("# Guide\n## Step two\n", encoding="utf-8")
        (self.root / "data.json").write_text("{}", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, text, name="README.md"):
        source = self.root / name
        source.write_text(text, encoding="utf-8")
        return links.check_file(self.root, source, links.AnchorCache())

    def test_valid_links_pass(self):
        text = (
            "# Top\n"
            "[guide](docs/guide.md) [step](docs/guide.md#step-two) [self](#top)\n"
            "[root](/docs/guide.md) ![img](data.json) [dir](docs/) [line](data.json#L3)\n"
            "[t1](docs/guide.md \"Title\") [t2](docs/guide.md 'Title')\n"
            "[ext](https://example.invalid/missing) [mail](mailto:someone@example.invalid)\n"
            "[ref]: docs/guide.md#guide\n"
        )
        self.assertEqual(self.check(text), [])

    def test_missing_file_is_reported_with_line(self):
        problems = self.check("ok\n[gone](docs/missing.md)\n")
        self.assertEqual(problems, ["README.md:2: missing file: docs/missing.md"])

    def test_broken_link_with_single_quoted_title_is_reported(self):
        self.assertEqual(len(self.check("[t](missing.md 'Title')\n")), 1)

    def test_missing_anchor_is_reported(self):
        problems = self.check("[bad](docs/guide.md#step-three)\n[bad self](#nowhere)\n")
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("missing heading anchor" in p for p in problems))

    def test_broken_reference_definition_is_reported(self):
        self.assertEqual(len(self.check("[ref]: nothing-here.md\n")), 1)

    def test_link_escaping_repository_is_reported(self):
        problems = self.check("[up](../../etc/passwd)\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("leaves the repository", problems[0])

    def test_links_in_code_are_ignored(self):
        self.assertEqual(self.check("`[x](missing.md)`\n```\n[y](missing.md)\n```\n"), [])

    def test_percent_encoded_path_is_decoded(self):
        (self.root / "my file.md").write_text("# H\n", encoding="utf-8")
        self.assertEqual(self.check("[s](my%20file.md#h)\n"), [])

    def test_link_from_subdirectory_resolves_relative_to_it(self):
        self.assertEqual(self.check("[up](../data.json)\n", name="docs/other.md"), [])


class MainTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def test_checks_only_git_tracked_files_and_sets_exit_status(self):
        self.git("init", "-q")
        (self.root / "a.md").write_text("[b](b.md)\n", encoding="utf-8")
        (self.root / "untracked.md").write_text("[x](missing.md)\n", encoding="utf-8")
        self.git("add", "a.md")
        self.assertEqual(links.main(["--root", str(self.root)]), 1)  # b.md is missing
        (self.root / "b.md").write_text("# B\n", encoding="utf-8")
        self.assertEqual(links.main(["--root", str(self.root)]), 0)

    def test_non_repository_root_is_a_usage_error(self):
        self.assertEqual(links.main(["--root", str(self.root)]), 2)

    def test_undecodable_file_is_a_usage_error(self):
        bad = self.root / "bad.md"
        bad.write_bytes(b"\xff\xfe\x00 not utf-8")
        self.assertEqual(links.main(["--root", str(self.root), str(bad)]), 2)


if __name__ == "__main__":
    unittest.main()
