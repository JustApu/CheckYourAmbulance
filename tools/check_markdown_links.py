#!/usr/bin/env python3
"""Check that relative links in Markdown files point at existing files and headings.

Read-only. External links (any URL with a scheme, such as https: or mailto:)
are not fetched, so the check needs no network and is deterministic.

Usage: tools/check_markdown_links.py [--root DIR] [FILE ...]
With no FILE arguments, every Markdown file tracked by git under DIR is checked.
Exit status: 0 = all links resolve, 1 = broken links found, 2 = usage or I/O error.
"""

import argparse
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote

FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
# A closing run of '#' counts only when preceded by a space ("C#" keeps its '#').
ATX_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
INLINE_CODE_RE = re.compile(r"(`+)(.+?)\1")
# [text](target) and ![alt](target), with an optional "title" or 'title'.
INLINE_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]*)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
REFERENCE_DEF_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)")
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
MD_LINK_TEXT_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
HTML_TAG_RE = re.compile(r"<[^>]+>")


def github_slug(heading_text):
    """Return the anchor GitHub generates for a heading (before de-duplication).

    GitHub lowercases the rendered text, drops every character that is not a
    letter, digit, underscore, hyphen or space, and turns each space into a
    hyphen (runs are not collapsed).
    """
    text = MD_LINK_TEXT_RE.sub(r"\1", heading_text)
    text = HTML_TAG_RE.sub("", text)
    text = text.replace("`", "").replace("*", "")
    text = text.strip().lower()
    kept = []
    for char in text:
        category = unicodedata.category(char)
        if char in "-_ " or category[0] in ("L", "N"):
            kept.append(char)
    return "".join(kept).replace(" ", "-")


def markdown_lines(text):
    """Yield (line_number, line) for lines outside fenced code blocks."""
    fence = None
    for number, line in enumerate(text.splitlines(), start=1):
        match = FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)[0] * len(match.group(1))
                continue
            yield number, line
        elif match and match.group(1).startswith(fence):
            fence = None


def heading_anchors(text):
    """Return the set of anchors GitHub generates for the headings in text."""
    anchors = set()
    seen = {}
    for _, line in markdown_lines(text):
        match = ATX_HEADING_RE.match(line)
        if not match:
            continue
        slug = github_slug(match.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def link_targets(text):
    """Yield (line_number, target) for each link in text, ignoring code."""
    for number, line in markdown_lines(text):
        definition = REFERENCE_DEF_RE.match(line)
        if definition:
            yield number, definition.group(1)
            continue
        without_code = INLINE_CODE_RE.sub("", line)
        for match in INLINE_LINK_RE.finditer(without_code):
            yield number, match.group(1)


def resolve_target(root, source, path_part):
    """Return the absolute path a link refers to, or None if it leaves root."""
    if path_part.startswith("/"):
        candidate = root / path_part.lstrip("/")
    else:
        candidate = source.parent / path_part
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


class AnchorCache:
    """Reads each target Markdown file once and remembers its anchors."""

    def __init__(self):
        self._anchors = {}

    def get(self, path):
        if path not in self._anchors:
            self._anchors[path] = heading_anchors(path.read_text(encoding="utf-8"))
        return self._anchors[path]


def check_file(root, source, cache):
    """Return a list of 'file:line: message' strings for broken links in source."""
    problems = []
    rel_source = source.relative_to(root)
    text = source.read_text(encoding="utf-8")
    for number, target in link_targets(text):
        if not target or SCHEME_RE.match(target):
            continue
        path_part, _, fragment = target.partition("#")
        path_part = unquote(path_part)
        fragment = unquote(fragment)
        if path_part:
            resolved = resolve_target(root, source, path_part)
            if resolved is None:
                problems.append(f"{rel_source}:{number}: link leaves the repository: {target}")
                continue
            if not resolved.exists():
                problems.append(f"{rel_source}:{number}: missing file: {target}")
                continue
        else:
            resolved = source
        # Fragments on non-Markdown targets (e.g. #L10 line anchors) are not checked.
        if fragment and resolved.is_file() and resolved.suffix.lower() == ".md":
            if fragment not in cache.get(resolved):
                problems.append(f"{rel_source}:{number}: missing heading anchor: {target}")
    return problems


def tracked_markdown_files(root):
    """Return the Markdown files git tracks under root."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.md"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(f"git ls-files failed (exit {result.returncode}): {result.stderr.strip()}")
    return [root / name for name in result.stdout.split("\0") if name]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    parser.add_argument("files", nargs="*", help="Markdown files to check (default: all tracked)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    try:
        files = [Path(f).resolve() for f in args.files] or tracked_markdown_files(root)
        cache = AnchorCache()
        problems = []
        for source in files:
            problems.extend(check_file(root, source, cache))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    for problem in problems:
        print(problem)
    print(f"checked {len(files)} Markdown file(s): {len(problems)} broken link(s)", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
