"""Load synthetic JSON fixtures from tests/fixtures/ (the single fixture directory).

Conventions (also in tests/README.md): one JSON object per file, hand-written
synthetic data only, and a top-level "synthetic": true marker. This loader
refuses anything else, so copied production data cannot slip in unmarked.
"""

import json
from pathlib import Path

from django.conf import settings

FIXTURE_DIR = Path(settings.BASE_DIR) / "tests" / "fixtures"


class FixtureError(Exception):
    """A fixture is missing, malformed or not marked synthetic."""


def load_fixture(name, fixture_dir=FIXTURE_DIR):
    """Return the parsed fixture tests/fixtures/<name>, e.g. "base.json"."""
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".json"):
        raise FixtureError(f"fixture name must be a plain *.json file name, not {name!r}")
    path = Path(fixture_dir) / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureError(f"cannot load fixture {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("synthetic") is not True:
        raise FixtureError(f'fixture {path} must be an object with "synthetic": true')
    return data
