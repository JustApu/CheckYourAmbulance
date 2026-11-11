"""Configure a deterministic primary and fallback for the Ubuntu apt mirror list."""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

FALLBACK = "http://archive.ubuntu.com/ubuntu/"


def _parts(line: str) -> tuple[str, list[str]]:
    uri, separator, metadata = line.partition("\t")
    if not separator:
        uri, separator, metadata = line.partition(" ")
    return uri, metadata.split() if separator else []


def _with_priority(metadata: list[str], priority: int) -> str:
    remaining = [item for item in metadata if not item.startswith("priority:")]
    return " ".join([f"priority:{priority}", *remaining])


def configure(contents: str, *, simulate_primary_failure: bool = False) -> str:
    """Add the fallback after the existing mirror set, preserving comments."""
    lines = contents.splitlines()
    active = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    parsed_lines = [_parts(line) for line in active]
    if not parsed_lines:
        raise ValueError("expected at least one active Ubuntu apt mirror")

    fallback_rows = [pair for pair in parsed_lines if pair[0] == FALLBACK]
    if len(fallback_rows) > 1:
        raise ValueError("duplicate official apt fallback entries")
    if fallback_rows and "priority:2" not in fallback_rows[0][1]:
        raise ValueError("unexpected priority for configured apt fallback")

    primary_rows = [pair for pair in parsed_lines if pair[0] != FALLBACK]
    if not primary_rows:
        raise ValueError("expected at least one primary Ubuntu apt mirror")

    primary_lines = []
    seen = set()
    for index, (primary, metadata) in enumerate(primary_rows, start=1):
        parsed = urlsplit(primary)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.path.endswith("/ubuntu/")
        ):
            raise ValueError("unexpected primary Ubuntu apt mirror URI")
        if primary in seen:
            raise ValueError("duplicate primary Ubuntu apt mirror URI")
        seen.add(primary)
        if fallback_rows and "priority:1" not in metadata:
            raise ValueError("unexpected priority for configured primary apt mirror")

        if simulate_primary_failure:
            primary = urlunsplit(
                (parsed.scheme, f"primary-{index}-unavailable.invalid", parsed.path, "", "")
            )
        primary_lines.append(f"{primary}\t{_with_priority(metadata, 1)}")

    fallback_line = f"{FALLBACK}\tpriority:2"
    output = []
    replaced = False
    for line in lines:
        if line in active and not replaced:
            output.extend((*primary_lines, fallback_line))
            replaced = True
        elif line not in active:
            output.append(line)
    return "\n".join(output).rstrip("\n") + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--simulate-primary-failure", action="store_true")
    args = parser.parse_args()
    original = args.path.read_text(encoding="utf-8")
    updated = configure(original, simulate_primary_failure=args.simulate_primary_failure)
    args.path.write_text(updated, encoding="utf-8")
    print("Configured Ubuntu apt mirrors and priority-2 archive fallback.")


if __name__ == "__main__":
    main()
