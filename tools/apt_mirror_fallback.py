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
    """Add the official fallback, preserving comments and requiring one known primary."""
    lines = contents.splitlines()
    active = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if len(active) != 1:
        raise ValueError("expected exactly one active Ubuntu apt mirror")

    primary, metadata = _parts(active[0])
    parsed = urlsplit(primary)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.path.endswith("/ubuntu/")
    ):
        raise ValueError("unexpected primary Ubuntu apt mirror URI")
    if primary.rstrip("/") == FALLBACK.rstrip("/"):
        raise ValueError("primary mirror already is the configured fallback")

    if simulate_primary_failure:
        primary = urlunsplit((parsed.scheme, "primary-unavailable.invalid", parsed.path, "", ""))

    primary_line = f"{primary}\t{_with_priority(metadata, 1)}"
    fallback_line = f"{FALLBACK}\tpriority:2"
    output = []
    replaced = False
    for line in lines:
        if line == active[0] and not replaced:
            output.extend((primary_line, fallback_line))
            replaced = True
        else:
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
    print("Configured Ubuntu apt primary and priority-2 archive fallback.")


if __name__ == "__main__":
    main()
