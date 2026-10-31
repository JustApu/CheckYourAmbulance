"""Offline tests for tools/check_normative_registry.py."""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_normative_registry as registry  # noqa: E402


class GitBlobIdTests(unittest.TestCase):
    def test_empty_content_matches_known_git_empty_blob(self):
        # The standard Git blob SHA-1 for an empty file (blob 0\0)
        self.assertEqual(
            registry.git_blob_id(b""),
            "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
        )

    def test_known_string_matches_git_hash(self):
        # 'test\n' -> blob 5\0test\n
        self.assertEqual(
            registry.git_blob_id(b"test\n"),
            "9daeafb9864cf43055ae93beb0afd6c7d144bfa4",
        )

    def test_multibyte_utf8_calculates_byte_length_not_character_length(self):
        # '💡' is 4 bytes in UTF-8 + 1 newline = 5 bytes
        data = "💡\n".encode()
        self.assertEqual(len(data), 5)
        expected_hash = registry.git_blob_id(data)
        self.assertEqual(len(expected_hash), 40)
        self.assertEqual(registry.git_blob_id(data), expected_hash)


class ParseRegistryTests(unittest.TestCase):
    def test_parses_valid_rows(self):
        sample = """
# Some header
| Repository document | Content blob ID | Authority |
|---|---|---|
| [/docs/coordination.md](docs/coordination.md) | `f6dfbbe469b49a5ae4c1a7b540517a8622730f7c` | Normative |
| [CONTRIBUTING.md](CONTRIBUTING.md) | `139e9ad1523f44230a110943df6665da3a01e802` | Non-normative |
"""
        entries = registry.parse_registry(sample)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].label, "/docs/coordination.md")
        self.assertEqual(entries[0].path, "docs/coordination.md")
        self.assertEqual(entries[0].blob_id, "f6dfbbe469b49a5ae4c1a7b540517a8622730f7c")
        self.assertEqual(entries[0].authority, "Normative")
        self.assertEqual(entries[0].line_number, 5)

        self.assertEqual(entries[1].label, "CONTRIBUTING.md")
        self.assertEqual(entries[1].path, "CONTRIBUTING.md")
        self.assertEqual(entries[1].blob_id, "139e9ad1523f44230a110943df6665da3a01e802")
        self.assertEqual(entries[1].authority, "Non-normative")
        self.assertEqual(entries[1].line_number, 6)

    def test_ignores_non_matching_tables_and_text(self):
        sample = """
| Name | Value |
|---|---|
| Foo | Bar |
Just some random text.
"""
        self.assertEqual(registry.parse_registry(sample), [])


class SyntheticCheckRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "docs").mkdir()

        # Create sample target file
        doc_file = self.root / "docs" / "test_doc.md"
        doc_file.write_text("# Test Doc\n", encoding="utf-8")
        self.doc_blob_id = registry.git_blob_id(doc_file.read_bytes())

    def tearDown(self):
        self._tmp.cleanup()

    def test_matching_blob_id_reports_no_problems(self):
        agents_md = self.root / "AGENTS.md"
        agents_md.write_text(
            f"| [/docs/test_doc.md](docs/test_doc.md) | `{self.doc_blob_id}` | Normative |\n",
            encoding="utf-8",
        )
        problems = registry.check_registry(agents_md, self.root, mandatory_documents=set())
        self.assertEqual(problems, [])

    def test_mismatched_blob_id_is_reported(self):
        agents_md = self.root / "AGENTS.md"
        bogus_id = "0" * 40
        agents_md.write_text(
            f"| [/docs/test_doc.md](docs/test_doc.md) | `{bogus_id}` | Normative |\n",
            encoding="utf-8",
        )
        problems = registry.check_registry(agents_md, self.root, mandatory_documents=set())
        self.assertEqual(len(problems), 1)
        self.assertIn("blob ID mismatch", problems[0])
        self.assertIn(self.doc_blob_id, problems[0])
        self.assertIn(bogus_id, problems[0])

    def test_missing_file_is_reported(self):
        agents_md = self.root / "AGENTS.md"
        agents_md.write_text(
            f"| [/docs/missing.md](docs/missing.md) | `{self.doc_blob_id}` | Normative |\n",
            encoding="utf-8",
        )
        problems = registry.check_registry(agents_md, self.root, mandatory_documents=set())
        self.assertEqual(len(problems), 1)
        self.assertIn("registered file does not exist", problems[0])

    def test_missing_mandatory_document_is_reported(self):
        agents_md = self.root / "AGENTS.md"
        agents_md.write_text(
            f"| [/docs/test_doc.md](docs/test_doc.md) | `{self.doc_blob_id}` | Normative |\n",
            encoding="utf-8",
        )
        problems = registry.check_registry(
            agents_md,
            self.root,
            mandatory_documents={"docs/test_doc.md", "docs/missing_normative.md"},
        )
        self.assertEqual(len(problems), 1)
        self.assertIn(
            "mandatory document missing from registry table: docs/missing_normative.md",
            problems[0],
        )

    def test_empty_registry_is_reported(self):
        agents_md = self.root / "AGENTS.md"
        agents_md.write_text("# AGENTS\nNo table here.\n", encoding="utf-8")
        problems = registry.check_registry(agents_md, self.root, mandatory_documents=set())
        self.assertEqual(len(problems), 1)
        self.assertIn("no registered normative document entries found", problems[0])


class MainCLITests(unittest.TestCase):
    def test_missing_agents_file_returns_exit_2(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            empty_root = Path(tmpdir)
            err = io.StringIO()
            with redirect_stderr(err):
                exit_code = registry.main(["--root", str(empty_root)])
            self.assertEqual(exit_code, 2)
            self.assertIn("error: AGENTS.md not found", err.getvalue())

    def test_mismatch_returns_exit_1(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            doc_file = root / "doc.md"
            doc_file.write_text("content\n", encoding="utf-8")
            agents_md = root / "AGENTS.md"
            agents_md.write_text(
                "| [doc.md](doc.md) | `0123456789abcdef0123456789abcdef01234567` | Normative |\n",
                encoding="utf-8",
            )
            err = io.StringIO()
            with redirect_stderr(err):
                exit_code = registry.main(["--root", str(root)])
            self.assertEqual(exit_code, 1)
            self.assertIn("blob ID mismatch", err.getvalue())


class LiveRepositoryRegistryTests(unittest.TestCase):
    """Verify that the repository's real AGENTS.md matches real files."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.agents_md = self.repo_root / "AGENTS.md"

    def test_registry_contains_mandatory_documents(self):
        content = self.agents_md.read_text(encoding="utf-8")
        entries = registry.parse_registry(content)
        registered_paths = {e.path.lstrip("/") for e in entries}

        expected_paths = {
            "docs/coordination.md",
            "docs/review.md",
            "docs/engineering-standards.md",
            "docs/model-efficiency.md",
            "CONTRIBUTING.md",
        }
        self.assertTrue(
            expected_paths.issubset(registered_paths),
            f"Missing expected registered paths: {expected_paths - registered_paths}",
        )

    def test_live_agents_md_registry_hashes_match_repository_files(self):
        problems = registry.check_registry(self.agents_md, self.repo_root)
        self.assertEqual(
            problems,
            [],
            "Normative registry hash drift detected:\n" + "\n".join(problems),
        )


if __name__ == "__main__":
    unittest.main()
