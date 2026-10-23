"""A venue listed for ``validate`` but not ``submit``.

The master list (``venues.json``) may hold a venue that has author-guideline
rules (``manuscript_requirements.json``) but no submission form. Such a venue
gets :func:`paperpush.database.default_form`, so the usual ``subfile`` ->
``validate`` flow works, while ``submit`` and ``login`` refuse it plainly.
These tests add one through a local data override.
"""

from __future__ import annotations

import json

import pytest

from paperpush import database, mcp_server
from paperpush.cli import main
from paperpush.requirements import REQUIREMENTS_PATH
from paperpush.subfile import load, replace_block, replace_scalar
from tests.conftest import _build_pdf

SLUG = "future_journal"


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
    assert "exceeds the 10-word limit in the author guidelines" in capsys.readouterr().err

    assert main(["submit", str(sub)]) == 1
    assert "validate-only" in capsys.readouterr().err

    with pytest.raises(ValueError, match="validate-only"):
        mcp_server.submit(str(sub))


def test_login_has_nothing_to_do(validate_only, capsys):
    assert main(["login", SLUG, "--status"]) == 0
    assert "validate-only" in capsys.readouterr().out
    assert mcp_server.login_status(SLUG)["login_required"] is False
    assert mcp_server.login(SLUG)["status"] == "not_required"


def test_listings_mark_validate_only(validate_only, capsys):
    assert main(["--venues"]) == 0
    assert f"Future Journal ({SLUG}) -- validate only" in capsys.readouterr().out
    listed = {v["slug"]: v for v in mcp_server.list_supported_venues()}
    assert listed[SLUG]["supports_submit"] is False
    assert listed[SLUG]["supports_validate"] is True
    assert listed["biorxiv"]["supports_submit"] is True
