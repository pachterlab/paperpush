"""A venue listed for ``validate`` but not ``submit``.

The master list (``venues.json``) may hold a venue that has author-guideline
rules (``manuscript_requirements.json``) but no submission form. Such a venue
gets :func:`paperpush.database.default_form`, so the usual ``subfile`` ->
``validate`` flow works, while ``submit`` and ``login`` refuse it plainly.
These tests add one through a local data override.
"""

from __future__ import annotations

import json
import re

import pytest

from paperpush import database, mcp_server
from paperpush.cli import main
from paperpush.database import list_venues
from paperpush.requirements import REQUIREMENTS_PATH
from paperpush.subfile import load, replace_block, replace_scalar
from tests.conftest import _build_pdf

SLUG = "future_journal"


def _both(captured) -> str:
    """stdout and stderr together: the report prints to stdout, notes and the final hint to stderr."""
    return captured.out + captured.err


@pytest.fixture
def validate_only(tmp_path, monkeypatch):
    """Point paperpush at data that adds ``future_journal`` as a validate-only venue."""
    override = tmp_path / "data"
    override.mkdir()
    info = json.loads(database.DATABASE_PATH.read_text())
    info[SLUG] = {"name": "Future Journal", "venue_type": "journal", "anonymous": False}
    (override / "venues.json").write_text(json.dumps(info))
    rules = json.loads(REQUIREMENTS_PATH.read_text())
    rules[SLUG] = {
        "article_type_field": "article_type",
        "manuscript": {"max_words": 5000},
        "article_types": {"Research Article": {}, "Brief Report": {"manuscript": {"max_words": 10}}},
    }
    (override / "manuscript_requirements.json").write_text(json.dumps(rules))
    monkeypatch.setenv("PAPERPUSH_VENUE_DATA", str(override))
    database.reload()
    yield
    monkeypatch.undo()
    database.reload()


def test_validate_only_venue_has_the_default_form(validate_only):
    venue = database.get_venue(SLUG)
    assert not venue.has_form
    assert venue.supports_validate and not venue.supports_submit
    by_id = {f.id: f for f in venue.fields}
    assert {"title", "abstract", "authors", "manuscript_file", "figure_files", "supplementary_files"} <= set(by_id)
    # The article-type choice selects which guideline limits apply.
    assert by_id["article_type"].options == ["Research Article", "Brief Report"]


def test_submit_venues_report_both_supports():
    venue = database.get_venue("biorxiv")
    assert venue.has_form and venue.supports_submit and venue.supports_validate


def test_validate_checks_the_guidelines_and_submit_refuses(validate_only, tmp_path, capsys):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(_build_pdf(pages=2, title="Paper", body_lines=["word " * 40]))
    sub = tmp_path / f"{SLUG}.sub"
    assert main(["subfile", SLUG, "-o", str(sub)]) == 0
    text = sub.read_text()
    text = replace_scalar(text, "title", "A paper")
    text = replace_block(text, "abstract", "An abstract.")
    text = replace_block(text, "authors", "Ada Lovelace | ada@example.org | Example University | | yes")
    text = replace_scalar(text, "article_type", "Brief Report")
    text = replace_scalar(text, "manuscript_file", str(pdf))
    sub.write_text(text)
    assert load(sub).venue == SLUG
    capsys.readouterr()

    assert main(["validate", str(sub), "--dont-check-links", "--dont-check-references"]) != 0
    # The Brief Report limit, selected through the default form's article type.
    assert "exceeds the 10-word limit in the author guidelines" in _both(capsys.readouterr())

    assert main(["submit", str(sub)]) == 1
    assert "validate-only" in _both(capsys.readouterr())

    with pytest.raises(ValueError, match="validate-only"):
        mcp_server.submit(str(sub))


def test_login_has_nothing_to_do(validate_only, capsys):
    assert main(["login", SLUG, "--status"]) == 0
    assert "validate-only" in capsys.readouterr().out
    assert mcp_server.login_status(SLUG)["login_required"] is False
    assert mcp_server.login(SLUG)["status"] == "not_required"


def test_listings_mark_validate_only(validate_only, capsys):
    assert main(["venues"]) == 0
    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines() if line.strip()}
    # Validate-only: one checkmark (validate), none in the submit column.
    assert "Future Journal" in rows[SLUG] and rows[SLUG].count("\u2713") == 1
    assert rows["biorxiv"].count("\u2713") == 2
    listed = {v["slug"]: v for v in mcp_server.list_supported_venues()}
    assert listed[SLUG]["supports_submit"] is False
    assert listed[SLUG]["supports_validate"] is True
    assert listed["biorxiv"]["supports_submit"] is True


def test_venues_submit_hides_validate_only(validate_only, capsys):
    assert main(["venues", "--submit"]) == 0
    out = capsys.readouterr().out
    slugs = {line.split()[0] for line in out.splitlines() if line.strip()}
    assert SLUG not in slugs
    assert "biorxiv" in slugs


def test_venues_validate_lists_validate_only(validate_only, capsys):
    assert main(["venues", "--validate"]) == 0
    out = capsys.readouterr().out
    slugs = {line.split()[0] for line in out.splitlines() if line.strip()}
    assert SLUG in slugs
    assert "biorxiv" in slugs


def test_venues_type_filters_listing(capsys):
    assert main(["venues", "-t", "preprint", "--type", "conference", "--columns", "slug,type"]) == 0
    out = capsys.readouterr().out
    rows = [line.split() for line in re.split(r"See more information|For more details", out)[0].splitlines()[1:] if line.strip()]
    types = {row[1] for row in rows}
    assert types == {"Preprint", "Conference"}
    assert "biorxiv" in {row[0] for row in rows}
    assert "conferenceagent.vercel.app" in out


def test_venues_conference_link_only_with_conferences(capsys):
    assert main(["venues", "-t", "journal"]) == 0
    assert "conferenceagent.vercel.app" not in capsys.readouterr().out


def test_venues_validate_and_submit_combine(validate_only, capsys):
    assert main(["venues", "--validate", "--submit"]) == 0
    slugs = {line.split()[0] for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert SLUG not in slugs and "biorxiv" in slugs


def test_venues_rejects_unknown_type(capsys):
    with pytest.raises(SystemExit):
        main(["venues", "--type", "bogus"])


def test_old_venues_flag_is_gone(capsys):
    with pytest.raises(SystemExit):
        main(["--venues"])


# --- subject keywords ---------------------------------------------------------


def test_every_venue_has_known_keywords():
    from paperpush.database import VENUE_KEYWORDS

    for venue in list_venues(include_deprecated=True):
        assert venue.keywords, f"{venue.slug} has no keywords"
        assert set(venue.keywords) <= set(VENUE_KEYWORDS), venue.slug


def test_venues_keywords_filters_listing(capsys):
    assert main(["venues", "--keywords", "Radiology, math"]) == 0
    slugs = {line.split()[0] for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert {"radiology", "ieee_tmi", "discrete_mathematics", "combinatorica"} <= slugs
    assert "biorxiv" not in slugs and "iclr_2027" not in slugs


def test_venues_keywords_combines_with_submit(capsys):
    assert main(["venues", "--submit", "--keywords", "radiology"]) == 0
    out = capsys.readouterr().out
    # Every radiology venue is validate-only, so none survive the submit filter.
    assert "No venues match the given filters." in out


def test_venues_keywords_rejects_unknown_with_suggestion(capsys):
    assert main(["venues", "--keywords", "biology,bioinforamtics"]) == 2
    err = capsys.readouterr().err
    assert "unknown keyword 'bioinforamtics'" in err and "did you mean 'bioinformatics'" in err


def test_venues_fits_narrow_terminal_without_truncating(monkeypatch, capsys):
    import rich.console

    orig = rich.console.Console.__init__

    def narrow(self, *a, **k):
        k.setdefault("width", 80)
        k.setdefault("force_terminal", True)
        k.setdefault("no_color", True)
        orig(self, *a, **k)

    monkeypatch.setattr(rich.console.Console, "__init__", narrow)
    assert main(["venues"]) == 0
    out = capsys.readouterr().out
    table = re.sub(r"\x1b\[[0-9;]*m", "", out).split("For more details")[0].split("\n\n")[0]
    lines = table.splitlines()
    # Wraps instead of cutting off: every column fits, nothing is ellipsized.
    assert all(len(line) <= 80 for line in lines)
    assert "\u2026" not in table
    assert "Submit" in "".join(lines[:3])
    # Long slugs fold onto the next line rather than being truncated.
    slug_text = "".join(line[: line.index(" ")] if " " in line else line for line in lines if line.strip())
    for venue in list_venues():
        assert venue.slug in slug_text


def test_published_entry_without_keywords_keeps_bundled_ones():
    from paperpush.database import merge_published_info

    bundled = {"x": {"name": "X", "keywords": ["math"]}}
    merged = merge_published_info(bundled, {"x": {"name": "X2"}}, {})
    assert merged["x"] == {"name": "X2", "keywords": ["math"]}


# --- column selection -----------------------------------------------------------


def _header(out: str) -> list[str]:
    return out.splitlines()[0].split()


def test_venues_columns_selects_and_orders(capsys):
    assert main(["venues", "--columns", "Submit, slug"]) == 0
    out = capsys.readouterr().out
    assert _header(out) == ["Submit", "Slug"]
    assert "https://" not in re.split(r"See more information|For more details", out)[0]


def test_venues_hide_columns(capsys):
    assert main(["venues", "--hide-columns", "link,keywords"]) == 0
    assert _header(capsys.readouterr().out) == ["Slug", "Venue", "Type", "Validate", "Submit"]


def test_venues_columns_and_hide_columns_combine(capsys):
    assert main(["venues", "--columns", "slug,type,link", "--hide-columns", "link"]) == 0
    assert _header(capsys.readouterr().out) == ["Slug", "Type"]


def test_venues_columns_rejects_unknown_and_empty(capsys):
    assert main(["venues", "--columns", "slug,lnk"]) == 2
    assert "unknown column 'lnk' (did you mean 'link'?)" in capsys.readouterr().err
    assert main(["venues", "--columns", "slug", "--hide-columns", "slug"]) == 2
    assert "no columns left to show" in capsys.readouterr().err


def test_venues_deadline_column_only_with_conferences(monkeypatch, capsys):
    import paperpush.conferences as conferences

    info = conferences.ConferenceInfo(acronym="CVPR", name="CVPR", abstract_deadline="2027-11-06", paper_deadline="2027-11-13")

    class FakeCatalog:
        errors: dict = {}

        def find(self, *names, acronym=""):
            return info if "CVPR 2027" in names else None

    monkeypatch.setattr(conferences, "load_catalog", lambda **k: FakeCatalog())
    # Rank and deadline are opt-in: the default table leaves them out.
    assert main(["venues", "-t", "conference"]) == 0
    assert "Next deadline" not in capsys.readouterr().out
    assert main(["venues", "-t", "conference", "--columns", "slug,rank,deadline"]) == 0
    out = capsys.readouterr().out
    assert "Next deadline" in out
    # Abstract and paper deadlines sit on separate lines of the one cell.
    lines = out.splitlines()
    row = next(i for i, line in enumerate(lines) if line.startswith("cvpr_2027"))
    assert "abstract 2027-11-06" in lines[row] and "paper 2027-11-13" in lines[row + 1]
    # This conference has no rank data, so that empty column is dropped.
    assert "Rank" not in out.split("Next deadline:")[0]

    assert main(["venues", "-t", "journal", "--columns", "slug,deadline"]) == 0
    out = capsys.readouterr().out
    assert "Next deadline" not in out and "conferenceagent" not in out


def test_venues_rank_lists_what_each_venue_has(monkeypatch, capsys):
    import paperpush.conferences as conferences
    from paperpush import venue_metrics

    info = conferences.ConferenceInfo(acronym="CVPR", name="CVPR", core_rank="A*", ccf_rank="A", acceptance_rate="25.4% (2026)", size="massive")

    class FakeCatalog:
        errors: dict = {}

        def find(self, *names, acronym=""):
            return info if "CVPR 2027" in names else None

    monkeypatch.setattr(conferences, "load_catalog", lambda **k: FakeCatalog())
    monkeypatch.setattr(venue_metrics, "load", lambda: {"nature": {"mean_citedness_2yr": 21.04}})
    assert main(["venues", "-t", "conference", "-t", "journal", "--columns", "slug,rank"]) == 0
    lines = capsys.readouterr().out.splitlines()
    row = next(i for i, line in enumerate(lines) if line.startswith("cvpr_2027"))
    assert [line.split(maxsplit=1)[-1].strip() if i == 0 else line.strip() for i, line in enumerate(lines[row : row + 4])] == [
        "CORE A*",
        "CCF A",
        "accept rate 25.4% (2026)",
        "size massive",
    ]
    nature = next(line for line in lines if line.startswith("nature "))
    assert nature.split(maxsplit=1)[1].strip() == "citedness 21.0"
