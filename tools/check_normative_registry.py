#!/usr/bin/env python3
"""Verify that Git blob IDs registered in AGENTS.md match repository file contents.

Read-only, offline, and deterministic.

Usage: tools/check_normative_registry.py [--agents-file PATH] [--root DIR]
Exit status: 0 = all hashes match, 1 = mismatch or missing file, 2 = usage/read error.
"""

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REGISTRY_ROW_RE = re.compile(
    r"\|\s*\[(?P<label>[^\]]+)\]\((?P<path>[^)]+)\)\s*\|\s*`?(?P<blob_id>[0-9a-fA-F]{40})`?\s*\|\s*(?P<authority>[^|]+?)\s*\|"
)

MANDATORY_DOCUMENTS = {
    "docs/coordination.md",
    "docs/review.md",
    "docs/engineering-standards.md",
    "docs/model-efficiency.md",
    "CONTRIBUTING.md",
}


@dataclass(frozen=True)
class RegistryEntry:
    label: str
    path: str
    blob_id: str
    authority: str
    line_number: int


def git_blob_id(data: bytes) -> str:
    """Return the Git blob SHA-1 hex digest for given raw bytes.

    Git hashes blobs with a header of the form: 'blob <size_in_bytes>\\0'.
    """
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def parse_registry(agents_md_text: str) -> list[RegistryEntry]:
    """Parse registry rows from AGENTS.md text."""
    entries: list[RegistryEntry] = []
    for line_no, line in enumerate(agents_md_text.splitlines(), start=1):
        line = line.strip()
        if not line.startswith("|"):
            continue
        m = REGISTRY_ROW_RE.search(line)
        if m:
            entries.append(
                RegistryEntry(
                    label=m.group("label").strip(),
                    path=m.group("path").strip(),
                    blob_id=m.group("blob_id").strip().lower(),
                    authority=m.group("authority").strip(),
                    line_number=line_no,
                )
            )
    return entries


def check_registry(
    agents_file: Path,
    repo_root: Path,
    mandatory_documents: set[str] | None = None,
) -> list[str]:
    """Validate all entries registered in agents_file against files in repo_root.

    Returns a list of human-readable error messages. An empty list indicates success.
    """
    if mandatory_documents is None:
        mandatory_documents = MANDATORY_DOCUMENTS

    try:
        content = agents_file.read_text(encoding="utf-8")
    except OSError as err:
        return [f"error: failed to read {agents_file}: {err}"]

    entries = parse_registry(content)
    if not entries:
        return [f"{agents_file}: no registered normative document entries found in table"]

    problems: list[str] = []
    registered_paths = {entry.path.lstrip("/") for entry in entries}
    missing_mandatory = mandatory_documents - registered_paths
    if missing_mandatory:
        for missing in sorted(missing_mandatory):
            problems.append(
                f"{agents_file}: mandatory document missing from registry table: {missing}"
            )
    for entry in entries:
        # Normalize relative path (remove leading slash if present)
        rel_path = entry.path.lstrip("/")
        target_path = (repo_root / rel_path).resolve()

        if not target_path.is_file():
            problems.append(
                f"{agents_file}:{entry.line_number}: registered file does not exist: {entry.path}"
            )
            continue

        try:
            file_bytes = target_path.read_bytes()
        except OSError as err:
            problems.append(
                f"{agents_file}:{entry.line_number}: failed to read {entry.path}: {err}"
            )
            continue

        actual_blob_id = git_blob_id(file_bytes)
        if actual_blob_id != entry.blob_id:
            problems.append(
                f"{agents_file}:{entry.line_number}: blob ID mismatch for {entry.path}: "
                f"registered `{entry.blob_id}`, but actual content blob ID is `{actual_blob_id}`. "
                f"Update AGENTS.md using 'git hash-object {rel_path}'."
            )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify Git blob IDs in AGENTS.md normative registry."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Repository root directory (default: current directory)",
    )
    parser.add_argument(
        "--agents-file",
        type=Path,
        default=None,
        help="Path to AGENTS.md (default: <root>/AGENTS.md)",
    )
    args = parser.parse_args(argv)

    repo_root = args.root.resolve()
    agents_file = args.agents_file.resolve() if args.agents_file else (repo_root / "AGENTS.md")

    if not agents_file.is_file():
        sys.stderr.write(f"error: AGENTS.md not found at {agents_file}\n")
        return 2

    problems = check_registry(agents_file, repo_root)
    if problems:
        for p in problems:
            sys.stderr.write(f"{p}\n")
        sys.stderr.write(f"normative registry: {len(problems)} problem(s)\n")
        return 1

    entries = parse_registry(agents_file.read_text(encoding="utf-8"))
    sys.stdout.write(f"normative registry: {len(entries)} registered document(s) verified\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
