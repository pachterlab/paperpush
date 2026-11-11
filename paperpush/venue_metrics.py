"""Citation metrics for the venues paperpush lists, from OpenAlex.

``venue_metrics.json`` holds, per venue slug, the OpenAlex source it was
matched to and that source's 2-year mean citedness: the mean number of
citations received in the last year by the works it published in the two years
before. It is OpenAlex's open counterpart of the Journal Impact Factor, close
to it but not the same number (the JIF is Clarivate's).

The file is kept out of ``venues.json`` so that older paperpush releases, which
reject venue entries with keys they do not know, keep reading the published
venue data. It is refreshed with ``python scripts/gen_venue_metrics.py``
(network), and ``paperpush venues`` only reads it.

Preprint servers get no value: a citation average of unreviewed preprints says
nothing about a venue's standing.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from functools import lru_cache
from pathlib import Path

METRICS_FILE = Path(__file__).with_name("venue_metrics.json")

# OpenAlex source types that can stand for each paperpush venue type.
_SOURCE_TYPES = {"journal": {"journal"}, "conference": {"conference", "book series"}}


@lru_cache(maxsize=1)
def load() -> dict[str, dict]:
    """``{slug: {"openalex_id", "openalex_name", "mean_citedness_2yr", ...}}``, or ``{}`` when the file is missing."""
    try:
        data = json.loads(METRICS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("$") and isinstance(v, dict)}


def mean_citedness_2yr(slug: str) -> float | None:
    """The venue's OpenAlex 2-year mean citedness, or ``None`` when paperpush has none."""
    value = load().get(slug, {}).get("mean_citedness_2yr")
    return float(value) if isinstance(value, (int, float)) else None


def _names(venue) -> list[str]:
    """The names to search OpenAlex for: the short name, and the full name without a " - tagline" or "(publisher)"."""
    full = re.sub(r"\s+-\s+.*$", "", venue.full_name or "")
    full = re.sub(r"\s*\([^)]*\)", "", full).strip()
    return list(dict.fromkeys(n for n in (venue.name, full) if n))


def match_source(venue, candidates: list[dict]) -> dict | None:
    """The OpenAlex source among ``candidates`` that is ``venue``: same name (or full name) and a fitting type.

    Ties go to the source with more works, so a journal beats a same-named
    proceedings series or a defunct namesake.
    """
    from .venue_select import _name_key

    names = {_name_key(n) for n in (venue.name, venue.full_name, *_names(venue))} - {""}
    types = _SOURCE_TYPES.get(venue.venue_type, {"journal"})
    hits = [c for c in candidates if c.get("type") in types and (_name_key(c.get("display_name") or "") in names or any(_name_key(a) in names for a in c.get("alternate_titles") or []))]
    return max(hits, key=lambda c: c.get("works_count") or 0) if hits else None


def build(venues, previous: dict[str, dict] | None = None, today: date | None = None, pause: float = 0.3) -> tuple[dict[str, dict], list[str], list[str]]:
    """Fetch fresh metrics for ``venues`` from OpenAlex.

    A venue already in ``previous`` keeps its ``openalex_id`` (so a match fixed
    by hand stays fixed) and only its numbers are refreshed; any other venue is
    looked up by name. Returns the new table, the venues that could not be
    matched, and the venues whose lookup failed (an OpenAlex error, as
    ``"slug (reason)"``), for the caller to report.
    """
    from . import openalex

    previous = previous or {}
    today = today or date.today()
    out: dict[str, dict] = {}
    unmatched: list[str] = []
    failed: list[str] = []
    pinned = {v.slug: previous[v.slug]["openalex_id"] for v in venues if previous.get(v.slug, {}).get("openalex_id")}
    records: dict[str, dict] | None
    try:
        records = openalex.sources(pinned.values()) if pinned else {}
    except openalex.OpenAlexError as exc:
        failed.extend(f"{slug} ({exc})" for slug in pinned)
        records = None
    for venue in venues:
        if venue.venue_type == "preprint":
            continue
        if venue.slug in pinned:
            if records is None:  # the batch fetch failed; the caller keeps the old entry
                continue
            source = records.get(openalex.short_id(pinned[venue.slug]))
        else:
            candidates: list[dict] = []
            try:
                for name in _names(venue):
                    time.sleep(pause)
                    data = openalex.get_json("/sources", {"search": name, "per_page": 10, "select": "id,display_name,alternate_titles,type,works_count,summary_stats"})
                    candidates.extend(s for s in data.get("results") or [] if isinstance(s, dict))
            except openalex.OpenAlexError as exc:
                failed.append(f"{venue.slug} ({exc})")
                continue
            source = match_source(venue, candidates)
        stats = (source or {}).get("summary_stats") or {}
        value = stats.get("2yr_mean_citedness")
        if not source or not isinstance(value, (int, float)):
            unmatched.append(venue.slug)
            continue
        out[venue.slug] = {
            "openalex_id": openalex.short_id(source.get("id") or ""),
            "openalex_name": source.get("display_name") or "",
            "mean_citedness_2yr": round(float(value), 2),
            "h_index": stats.get("h_index"),
            "fetched": today.isoformat(),
        }
    return out, unmatched, failed
