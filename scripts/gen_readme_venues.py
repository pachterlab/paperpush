#!/usr/bin/env python3
"""Regenerate the supported-venue listings from venues.json.

Two artifacts are kept in sync with the database:

* the detailed "Supported venues" table in ``venues.md``, between

      <!-- BEGIN SUPPORTED VENUES -->
      <!-- END SUPPORTED VENUES -->

* the short "Supported venues" summary in ``README.md``, grouped by
  ``venue_type`` (preprint servers / journals / conferences), between

      <!-- BEGIN SUPPORTED VENUES -->
      <!-- END SUPPORTED VENUES -->

In both files the surrounding prose is left untouched. Run it after editing
``paperpush/venues.json``:

    python scripts/gen_readme_venues.py        # rewrite both files in place
    python scripts/gen_readme_venues.py --check # exit 1 if either is out of date

The ``--check`` form is suitable for CI or a pre-commit hook.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Import the package directly from the repo without requiring an install.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from paperpush.database import list_venues  # noqa: E402
from tests.submit_walkthrough_status import STATUS_PATH  # noqa: E402
from tests.submit_walkthrough_status import load_status

README_PATH = REPO_ROOT / "README.md"
VENUES_PATH = REPO_ROOT / "venues.md"
BEGIN = "<!-- BEGIN SUPPORTED VENUES -->"
END = "<!-- END SUPPORTED VENUES -->"
BEGIN_VENUES = "<!-- BEGIN SUPPORTED VENUES -->"
END_VENUES = "<!-- END SUPPORTED VENUES -->"

# Ordered venue_type groups for the README summary. Each tuple is
# (venue_type value in venues.json, human-facing heading). A group with no
# venues still renders (as "none yet") so the set of supported kinds is always
# visible.
VENUE_GROUPS = [
    ("preprint", "Preprint servers"),
    ("journal", "Journals"),
    ("conference", "Conferences"),
]

# Submission platform (the manuscript-submission system the runner drives) each
# venue uses, keyed by slug. This is README presentation kept out of
# ``venues.json``: it describes the third-party portal (Editorial Manager,
# ScholarOne, ...) the venue happens to run on, which is independent of the
# database inheritance. The
# names match the platforms identified in each runner's module docstring (e.g.
# ``paperpush/venues/editorialmanager/cell.py`` -> Editorial Manager,
# ``science/science.py`` -> the
# AAAS Contributor Tracking System). Lookup falls back to the inherited base
# slug, then to a dash, so an unmapped venue still renders sensibly.
PLATFORMS = {
    "aaai_2027": "OpenReview",
    "discrete_mathematics": "Editorial Manager",
    "arxiv": "arXiv (native)",
    "biorxiv": "openRxiv",
    "medrxiv": "openRxiv",
    "bioinformatics": "ScholarOne Manuscripts",
    "nature": "eJournalPress",
    "nature_biotech": "eJournalPress",
    "nature_methods": "eJournalPress",
    "cell": "Editorial Manager",
    "cell_systems": "Editorial Manager",
    "cell_genomics": "Editorial Manager",
    "combinatorica": "EditFlow",
    "plos_compbio": "Editorial Manager",
    "genome_biology": "Springer Nature Snapp",
    "science": "AAAS Contributor Tracking System",
    "science_advances": "AAAS Contributor Tracking System",
    "science_immunology": "AAAS Contributor Tracking System",
    "science_robotics": "AAAS Contributor Tracking System",
    "science_signaling": "AAAS Contributor Tracking System",
    "science_translational_medicine": "AAAS Contributor Tracking System",
}

# Submit-walkthrough verification status is no longer hand-maintained here.
# It is recorded automatically by the ``test_submit_walkthrough`` case in
# ``tests/test_submit.py`` into
# ``tests/submit_walkthrough_status.json`` (keyed by slug -> ISO date of the
# last successful live-portal run) and loaded below. Re-running the walkthrough
# test for a venue refreshes its date; this generator simply reflects it.
WALKTHROUGH = load_status(STATUS_PATH)


def walkthrough_cell(slug: str) -> str:
    """Render the submit-walkthrough status cell as plain text.

    The cell text is the last successful run date (``✅ YYYY-MM-DD``) when the
    walkthrough test has recorded one for this slug, or ``❌`` otherwise.
    """
    date = WALKTHROUGH.get(slug)
    return f"✅ {date}" if date else "❌"


def _cell(text: str) -> str:
    """Escape a value for safe inclusion in a Markdown table cell."""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def platform_of(venue) -> str:
    """Return the submission-platform label for a venue.

    Lookup prefers the venue's own slug, then the slug it inherits from
    (``inherits``), and falls back to a dash for any venue not yet mapped in
    ``PLATFORMS``.
    """
    return PLATFORMS.get(venue.slug) or PLATFORMS.get(venue.inherits) or "—"


def _linked_name(venue) -> str:
    """Render a venue's display name as a Markdown link to its public site.

    Prefers the public homepage (``site_url``), falling back to the submission
    portal (``submission_url``); an unlinked name is returned when neither is set.
    """
    link = venue.site_url or venue.submission_url
    name = _cell(venue.name)
    return f"[{name}]({link})" if link else name


def build_table() -> str:
    """Render the supported-venues Markdown table from the database."""
    venues = list_venues()
    lines = [
        f"_{len(venues)} supported venue" + ("s" if len(venues) != 1 else "") + " (auto-generated from `paperpush/venues.json`)._",
        "",
        "| Slug | Venue | Venue type | Submission platform | Description | Submit walkthrough |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for j in venues:
        lines.append(f"| `{j.slug}` | {_linked_name(j)} | {_cell(j.venue_type.capitalize())} | {_cell(platform_of(j))} | {_cell(j.description)} | {walkthrough_cell(j.slug)} |")
    return "\n".join(lines)


def build_venue_groups() -> str:
    """Render the README "Supported venues" summary, grouped by ``venue_type``.

    Each configured group (see :data:`VENUE_GROUPS`) becomes a line listing its
    venues as links, in the database's display order; an empty group renders as
    "none yet" so every supported kind of venue stays visible.
    """
    venues = list_venues()
    by_type: dict[str, list] = {}
    for j in venues:
        by_type.setdefault(j.venue_type, []).append(j)

    paragraphs = []
    for venue_type, heading in VENUE_GROUPS:
        members = by_type.get(venue_type, [])
        listing = ", ".join(_linked_name(j) for j in members) if members else "_none yet_"
        paragraphs.append(f"**{heading}:** {listing}")
    return "\n\n".join(paragraphs)


def _replace_section(text: str, begin: str, end: str, body: str, where: str) -> str:
    """Return ``text`` with the content between ``begin``/``end`` replaced by ``body``."""
    if begin not in text or end not in text:
        raise SystemExit(f"Markers not found in {where}. Add a section bounded by\n" f"  {begin}\n  {end}")
    pre, rest = text.split(begin, 1)
    _, post = rest.split(end, 1)
    return f"{pre}{begin}\n{body}\n{end}{post}"


def _sync(path: Path, begin: str, end: str, body: str, *, check: bool) -> bool:
    """Rewrite (or, in ``check`` mode, verify) one file's generated section.

    Returns True when the file is already up to date, False when it is (or would
    be) changed.
    """
    current = path.read_text(encoding="utf-8")
    updated = _replace_section(current, begin, end, body, path.name)
    if current == updated:
        return True
    if check:
        print(
            f"{path.name} is out of date. Run: python scripts/gen_readme_venues.py",
            file=sys.stderr,
        )
    else:
        path.write_text(updated, encoding="utf-8")
        print(f"Updated {path.name}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if either file is out of date instead of rewriting it",
    )
    args = parser.parse_args()

    # Evaluate both so --check reports every stale file, not just the first.
    venues_ok = _sync(VENUES_PATH, BEGIN, END, build_table(), check=args.check)
    readme_ok = _sync(README_PATH, BEGIN_VENUES, END_VENUES, build_venue_groups(), check=args.check)

    if args.check:
        return 0 if venues_ok and readme_ok else 1
    if venues_ok and readme_ok:
        print("All venue listings already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
