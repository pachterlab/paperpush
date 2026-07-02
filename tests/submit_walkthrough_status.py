"""Persisted record of when each venue's submit walkthrough last succeeded.

The submit walkthrough (``tests/test_submit_walkthrough.py``) drives a venue's
live portal and stops before the final submit. When it succeeds for a venue it
calls :func:`record_success` here, which stamps that venue's slug with the
current date in ``submit_walkthrough_status.json``. The README generator
(``scripts/gen_readme_venues.py``) reads the same file via :func:`load_status`
to render the "Submit walkthrough" column, so the date shown next to each
venue is exactly the date its walkthrough test last passed.

Keeping the path and (de)serialization in one module means the test (writer) and
the generator (reader) cannot drift apart on format or location.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

# Directory of this module == the tests directory; the repo root is its parent.
TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

# The JSON record, ``{slug: "YYYY-MM-DD"}``, and the test that maintains it.
STATUS_PATH = TESTS_DIR / "submit_walkthrough_status.json"
WALKTHROUGH_TEST = TESTS_DIR / "test_submit_walkthrough.py"


def load_status(path: Path = STATUS_PATH) -> dict[str, str]:
    """Return the recorded ``{slug: ISO date}`` map, or ``{}`` if none exists."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {str(k): str(v) for k, v in data.items()}


def record_success(slug: str, *, path: Path = STATUS_PATH, today: datetime.date | None = None) -> str:
    """Stamp ``slug`` with today's date and persist the status file.

    Returns the ISO date written. The file is kept sorted by slug and
    pretty-printed so its diffs read cleanly in version control.
    """
    date = (today or datetime.date.today()).isoformat()
    status = load_status(path)
    status[slug] = date
    path.write_text(json.dumps(dict(sorted(status.items())), indent=2) + "\n", encoding="utf-8")
    return date
