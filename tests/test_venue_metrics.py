"""venue_metrics: matching venues to OpenAlex sources, and the citedness column in `paperpush venues`."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from paperpush import openalex, venue_metrics
from paperpush.cli import main


def _venue(slug, name, full_name, venue_type="journal"):
    return SimpleNamespace(slug=slug, name=name, full_name=full_name, venue_type=venue_type)


def test_names_trim_tagline_and_publisher():
    assert venue_metrics._names(_venue("nature", "Nature", "Nature - International weekly journal of science")) == ["Nature"]
    assert venue_metrics._names(_venue("ajhg", "AJHG", "The American Journal of Human Genetics (Cell Press)")) == ["AJHG", "The American Journal of Human Genetics"]


def test_match_source_prefers_same_name_journal_with_most_works():
    venue = _venue("nejm", "NEJM", "The New England Journal of Medicine")
    candidates = [
        {"id": "S1", "display_name": "NEJM Evidence", "type": "journal", "works_count": 700},
        {"id": "S2", "display_name": "New England Journal of Medicine", "type": "journal", "works_count": 190000},
        {"id": "S3", "display_name": "The New England Journal of Medicine", "type": "journal", "works_count": 20},
        {"id": "S4", "display_name": "New England Journal of Medicine", "type": "book series", "works_count": 900000},
    ]
    assert venue_metrics.match_source(venue, candidates)["id"] == "S2"
    assert venue_metrics.match_source(_venue("x", "Other", "Other journal"), candidates) is None


def test_build_keeps_pinned_ids_skips_preprints_and_reports_failures(monkeypatch):
    venues = [
        _venue("arxiv", "arXiv", "arXiv", "preprint"),
        _venue("pinned", "Pinned", "Pinned Journal"),
        _venue("bioinformatics", "Bioinformatics", "Bioinformatics (Oxford University Press)"),
        _venue("down", "Down", "Down Journal"),
    ]
    monkeypatch.setattr(openalex, "sources", lambda ids: {"S9": {"id": "https://openalex.org/S9", "display_name": "Pinned J.", "summary_stats": {"2yr_mean_citedness": 3.456, "h_index": 50}}})

    def get_json(path, params):
        if params["search"] == "Down":
            raise openalex.OpenAlexError("HTTP 429")
        return {"results": [{"id": "https://openalex.org/S5", "display_name": "Bioinformatics", "type": "journal", "works_count": 19000, "summary_stats": {"2yr_mean_citedness": 5.0, "h_index": 400}}]}

    monkeypatch.setattr(openalex, "get_json", get_json)
    out, unmatched, failed = venue_metrics.build(venues, {"pinned": {"openalex_id": "S9"}}, today=date(2026, 11, 11), pause=0)
    assert "arxiv" not in out and "arxiv" not in unmatched
    assert out["pinned"] == {"openalex_id": "S9", "openalex_name": "Pinned J.", "mean_citedness_2yr": 3.46, "h_index": 50, "fetched": "2026-11-11"}
    assert out["bioinformatics"]["openalex_id"] == "S5"
    assert failed == ["down (HTTP 429)"] and unmatched == []


def test_venues_table_shows_citedness_after_type(monkeypatch, capsys):
    monkeypatch.setattr(venue_metrics, "load", lambda: {"nature": {"mean_citedness_2yr": 21.04}})
    assert main(["venues", "--columns", "slug,type,citedness"]) == 0
    out = capsys.readouterr().out
    header = next(line for line in out.splitlines() if line.startswith("Slug"))
    assert header.index("Type") < header.index("2-yr citedness")
    nature = next(line for line in out.splitlines() if line.startswith("nature "))
    assert nature.rstrip().endswith("21.0")
    assert "not the Clarivate figure" in out


def test_shipped_metrics_file_is_readable():
    data = venue_metrics.load()
    assert data, "paperpush/venue_metrics.json is missing or empty; run scripts/gen_venue_metrics.py"
    assert all(isinstance(v.get("mean_citedness_2yr"), (int, float)) for v in data.values())
