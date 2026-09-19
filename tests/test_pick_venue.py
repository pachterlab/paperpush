"""Tests for ``paperpush pick-venue`` and :mod:`paperpush.venue_select`.

Ranking and filtering are pure functions over venue metadata, so most tests use
hand-built :class:`~paperpush.database.Venue` objects through the ``venues=``
parameter of :func:`~paperpush.venue_select.pick_venues` -- deterministic no
matter how ``venues.json`` evolves. A smaller set of tests runs against the
shipped database and the ``pick-venue`` CLI end to end via
:func:`paperpush.cli.main`.
"""

from __future__ import annotations

import json

from paperpush.database import Venue
from paperpush.venue_select import format_hits, pick_venues, portal_of


def _venue(slug, name="", description="", venue_type="journal", full_name=""):
    return Venue(slug=slug, name=name or slug, description=description, venue_type=venue_type, full_name=full_name)


# The tiny fixed corpus the ranking tests below run against. Portal names here
# are real module-layout portals but these slugs are invented, so scoring never
# depends on the shipped database.
_FAKE_VENUES = [
    _venue("prex", "Preprint X", "an open archive for preprints", "preprint", "Preprint X Full"),
    _venue("jx", "Journal X", "a journal about widgets", "journal"),
    _venue("cx27", "Conf X 2027", "conference on widgets", "conference"),
]


def test_exact_slug_beats_prefix_and_description():
    hits = pick_venues("jx", venues=_FAKE_VENUES)
    assert [hit.venue.slug for hit in hits] == ["jx"]
    assert hits[0].score == 100
    assert hits[0].reasons == ["slug"]


def test_slug_prefix_outranks_description_and_name_prefix():
    hits = pick_venues("pre", venues=_FAKE_VENUES)
    assert hits[0].venue.slug == "prex"
    assert hits[0].reasons == ["slug-prefix"]


def test_name_prefix_scores_below_exact_slug():
    hits = pick_venues("journal", venues=_FAKE_VENUES)
    assert hits[0].venue.slug == "jx"
    assert hits[0].reasons == ["name-prefix"]


def test_venue_type_token_matches_type():
    hits = pick_venues("conference", venues=_FAKE_VENUES)
    assert [hit.venue.slug for hit in hits] == ["cx27"]
    assert "type" in hits[0].reasons


def test_description_keyword_matches():
    hits = pick_venues("widgets", venues=_FAKE_VENUES)
    assert {hit.venue.slug for hit in hits} == {"jx", "cx27"}
    assert all(hit.reasons == ["description"] for hit in hits)


def test_multi_token_scores_add_up():
    hits = pick_venues("jx widgets", venues=_FAKE_VENUES)
    by_slug = {hit.venue.slug: hit for hit in hits}
    assert by_slug["jx"].score == 100 + 15
    assert by_slug["cx27"].score == 15


def test_ties_break_by_slug():
    hits = pick_venues("widgets", venues=_FAKE_VENUES)
    assert [hit.venue.slug for hit in hits] == ["cx27", "jx"]
    assert hits[0].score == hits[1].score


def test_nothing_matching_returns_empty():
    assert pick_venues("flurb-no-such-term", venues=_FAKE_VENUES) == []


def test_query_is_case_insensitive():
    hits = pick_venues("JX", venues=_FAKE_VENUES)
    assert [hit.venue.slug for hit in hits] == ["jx"]


def test_empty_query_keeps_everything_sorted_by_slug():
    hits = pick_venues("", venues=_FAKE_VENUES)
    assert [hit.venue.slug for hit in hits] == ["cx27", "jx", "prex"]
    assert all(hit.score == 0 for hit in hits)


def test_type_filter_narrows_before_scoring():
    hits = pick_venues("", venue_types=("preprint",), venues=_FAKE_VENUES)
    assert [hit.venue.slug for hit in hits] == ["prex"]
    assert pick_venues("jx", venue_types=("preprint",), venues=_FAKE_VENUES) == []


def test_query_and_filter_compose():
    # Filters narrow the field first; the query then only ranks within it --
    # "widgets" matches jx too, but the type filter keeps it out.
    hits = pick_venues("widgets", venue_types=("conference",), venues=_FAKE_VENUES)
    assert [hit.venue.slug for hit in hits] == ["cx27"]


def test_format_hits_pads_slugs_and_truncates_descriptions():
    long = "x" * 200
    hits = pick_venues("", venues=[_venue("ab", "A B", long), _venue("cdef", "C D", "short")])
    lines = format_hits(hits)
    assert len(lines) == 2
    assert lines[0].startswith("ab    A B (journal) -- ")
    assert lines[0].endswith("…")
    assert lines[1].startswith("cdef  C D (journal) -- short")


def test_portal_of_resolves_family_members():
    assert portal_of("biorxiv") == "openrxiv"
    assert portal_of("cell") == "editorialmanager"
    # AAAS siblings submit through the shared science runner.
    assert portal_of("science_advances") == "science"
    assert portal_of("no-such-venue") == ""


def test_live_preprints_are_found():
    slugs = {hit.venue.slug for hit in pick_venues("preprint")}
    assert {"arxiv", "biorxiv", "medrxiv"} <= slugs


def test_live_openreview_portal_matches():
    slugs = {hit.venue.slug for hit in pick_venues("", portals=("openreview",))}
    assert "iclr_2027" in slugs
    # aaai_2027 is deprecated, and deprecated venues stay out of selection.
    assert "aaai_2027" not in slugs


def test_live_exact_slug_ranks_first():
    hits = pick_venues("nature_methods")
    assert hits[0].venue.slug == "nature_methods"


def test_cli_picks_a_venue(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "iclr"]) == 0
    out = capsys.readouterr().out
    assert "iclr_2027" in out
    assert "subfile" in out


def test_cli_json_is_machine_readable(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--json", "cell"]) == 0
    out = capsys.readouterr().out
    matches = json.loads(out)
    assert matches[0]["slug"] == "cell"
    assert matches[0]["venue_type"] == "journal"
    assert {"slug", "name", "venue_type", "portal", "score"} <= set(matches[0])


def test_cli_type_filter(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--all", "-t", "preprint"]) == 0
    out = capsys.readouterr().out
    venue_lines = [line for line in out.splitlines() if line and not line.startswith("Use ")]
    assert len(venue_lines) == 3
    assert all("(preprint)" in line for line in venue_lines)


def test_cli_limit_and_hint(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "-n", "1", "bio"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    listed = [line for line in lines if line and not line.startswith("Use ")]
    assert len(listed) == 1
    # Even a single displayed row keeps the next-step hint, pointing at the
    # top-ranked hit rather than the visible row.
    assert f"paperpush subfile {pick_venues('bio')[0].venue.slug}" in out


def test_cli_zero_limit_keeps_the_next_step_hint(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "-n", "0", "bio"]) == 0
    out = capsys.readouterr().out
    assert f"paperpush subfile {pick_venues('bio')[0].venue.slug}" in out


def test_cli_rejects_missing_input(capsys):
    from paperpush.cli import main

    assert main(["pick-venue"]) == 2
    err = capsys.readouterr().err
    assert "needs a query" in err


def test_cli_rejects_a_query_with_no_match(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "zzz-no-such-venue"]) == 1
    err = capsys.readouterr().err
    assert "no venues matched" in err


def test_cli_rejects_filters_with_no_match(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--portal", "no-such-portal"]) == 1
    err = capsys.readouterr().err
    assert "no venues matched" in err
