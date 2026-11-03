"""Tests for the cover-letter checks and the counts stated on the title page.

Covers :mod:`paperpush.cover_letter_check` (the letter's date and the venue it
names) and the stated word / figure / table counts in
:mod:`paperpush.consistency`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from paperpush import consistency, cover_letter_check, report
from paperpush.cli import main
from paperpush.database import Field, Venue, list_venues
from paperpush.validate import Checks
from tests.conftest import _build_docx

COVER = Field(id="cover_letter", label="Cover letter", type="file")
MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
FIGURES = Field(id="figure_files", label="Figures", type="filelist")
ABSTRACT = Field(id="abstract", label="Abstract", type="textarea")
TODAY = date(2026, 11, 3)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(cover_letter_check, "_today", lambda: TODAY)


def _status(checks: Checks, check_id: str) -> tuple[str, str]:
    res = next(x for x in checks.results if x.id == check_id)
    return res.status, " ".join(i.message for i in res.issues) + " " + res.detail


# ===========================================================================
# cover letter: date
# ===========================================================================


@pytest.mark.parametrize(
    "text, readings",
    [
        ("November 3, 2026", [date(2026, 11, 3)]),
        ("Nov. 3rd, 2026", [date(2026, 11, 3)]),
        ("3 November 2026", [date(2026, 11, 3)]),
        ("the 3rd of November, 2026", [date(2026, 11, 3)]),
        ("2026-11-03", [date(2026, 11, 3)]),
        ("03/11/2026", [date(2026, 3, 11), date(2026, 11, 3)]),
        ("25/10/2026", [date(2026, 10, 25)]),
        ("November 2026", [date(2026, 11, 1), date(2026, 11, 30)]),
    ],
)
def test_parse_dates_reads_common_formats(text, readings):
    found = cover_letter_check.parse_dates(f"Department of Biology\n{text}\n")
    assert found is not None and found[1] == readings


def test_parse_dates_takes_the_first_date():
    assert cover_letter_check.parse_dates("1 October 2026\nWe cite work from 5 May 2020.")[1] == [date(2026, 10, 1)]
    assert cover_letter_check.parse_dates("No date here, just 12 cells and 2026 samples.") is None


def test_letterhead_stops_at_the_salutation():
    text = "Lab of X\nNovember 1, 2026\nDear Dr. Smith,\nOn 1 January 2020 we began."
    assert "January" not in cover_letter_check.letterhead(text)


@pytest.mark.parametrize(
    "readings, warn, phrase",
    [
        ([date(2026, 11, 1)], False, "2 day(s) ago"),
        ([date(2026, 10, 27)], False, "7 day(s) ago"),
        ([date(2026, 10, 26)], True, "8 days ago"),
        ([date(2026, 11, 4)], False, ""),  # tomorrow: a time-zone difference
        ([date(2026, 11, 20)], True, "in the future"),
        ([date(2026, 3, 11), date(2026, 11, 3)], False, "read as"),  # ambiguous, one reading is fresh
        ([date(2026, 3, 10), date(2026, 10, 3)], True, "either way"),
    ],
)
def test_date_issue(readings, warn, phrase):
    issues, detail = cover_letter_check.date_issue("cover_letter", "letter.docx", "the date", readings, TODAY, 7)
    assert bool(issues) == warn
    assert phrase in (issues[0].message if issues else detail)


def _letter_checks(tmp_path: Path, paragraphs: list[str], venue: Venue | None = None, max_age: int = 7) -> Checks:
    letter = tmp_path / "cover_letter.docx"
    _build_docx(letter, paragraphs)
    venue = venue or Venue(slug="cell", name="Cell", full_name="Cell (Cell Press)", fields=[COVER])
    checks = Checks(options={"cover_letter_max_age_days": max_age})
    cover_letter_check.run_checks(checks, venue, {"cover_letter": str(letter)})
    return checks


def test_stale_cover_letter_is_a_warning(tmp_path):
    checks = _letter_checks(tmp_path, ["Lab of Biology", "12 October 2026", "Dear Editors,", "We submit our manuscript to Cell for consideration."])
    status, text = _status(checks, "cover_letter.date")
    assert status == report.WARNING and "22 days ago" in text
    assert _status(checks, "cover_letter.venue")[0] == report.PASSED


def test_max_age_option(tmp_path):
    checks = _letter_checks(tmp_path, ["12 October 2026", "Dear Editors,", "Text."], max_age=30)
    assert _status(checks, "cover_letter.date")[0] == report.PASSED
    assert "within the last 30 days" in next(x for x in checks.results if x.id == "cover_letter.date").title


def test_undated_cover_letter_is_unchecked(tmp_path):
    checks = _letter_checks(tmp_path, ["Dear Editors,", "We submit our manuscript to Cell."])
    assert _status(checks, "cover_letter.date")[0] == report.UNCHECKED


def test_latex_letter_dated_today_passes(tmp_path):
    letter = tmp_path / "letter.tex"
    letter.write_text("\\documentclass{letter}\n\\date{\\today}\n\\begin{document}\n\\opening{Dear Editors,}\nWe submit to Cell.\n\\end{document}\n")
    checks = Checks()
    venue = Venue(slug="cell", name="Cell", fields=[COVER])
    cover_letter_check.run_checks(checks, venue, {"cover_letter": str(letter)})
    status, text = _status(checks, "cover_letter.date")
    assert status == report.PASSED and "\\today" in text


def test_no_cover_letter_is_not_applicable(tmp_path):
    checks = Checks()
    cover_letter_check.run_checks(checks, Venue(slug="cell", name="Cell", fields=[MANUSCRIPT]), {})
    assert _status(checks, "cover_letter.date")[0] == report.NOT_APPLICABLE
    assert _status(checks, "cover_letter.venue")[0] == report.NOT_APPLICABLE


# ===========================================================================
# cover letter: venue named
# ===========================================================================


def test_cover_letter_naming_another_venue_is_a_warning(tmp_path):
    checks = _letter_checks(tmp_path, ["November 2, 2026", "Dear Editors of Nature,", "We are pleased to submit our manuscript for consideration for publication in Nature."])
    status, text = _status(checks, "cover_letter.venue")
    assert status == report.WARNING and "names Nature" in text and "not Cell" in text


def test_cited_venues_and_common_words_are_not_flagged(tmp_path):
    paragraphs = [
        "November 2, 2026",
        "Dear Editors,",
        "We submit our manuscript for consideration in Cell.",
        "We previously published a related method in Nature Methods (2024).",
        "Single-cell science is a growing field, and the nature of cell fate is central to this submission.",
    ]
    checks = _letter_checks(tmp_path, paragraphs)
    status, text = _status(checks, "cover_letter.venue")
    assert status == report.PASSED and "names Cell" in text


def test_names_inside_the_target_venue_are_not_other_venues():
    venue = Venue(slug="nature_communications", name="Nature Communications", full_name="Nature Communications")
    text = "Dear Editors, we submit our work for consideration in Nature Communications."
    assert cover_letter_check.other_venue_mentions(text, venue, list_venues()) == []


def test_longest_other_name_wins():
    venue = Venue(slug="cell", name="Cell")
    found = cover_letter_check.other_venue_mentions("We submit this manuscript to Nature Methods for consideration.", venue, list_venues())
    assert [name for name, _ in found] == ["Nature Methods"]


def test_letter_naming_no_venue_goes_to_ai(tmp_path):
    checks = _letter_checks(tmp_path, ["November 2, 2026", "Dear Editors,", "We submit our manuscript to your journal."])
    assert _status(checks, "cover_letter.venue")[0] == report.UNCHECKED
    assert [t.id for t in checks.ai_tasks] == ["ai.cover_letter_venue"]


def test_venue_names_variants():
    venue = Venue(slug="iclr_2027", name="ICLR 2027", full_name="The Fifteenth International Conference on Learning Representations")
    names = cover_letter_check.venue_names(venue)
    assert "ICLR" in names and "Fifteenth International Conference on Learning Representations" in names


# ===========================================================================
# stated counts
# ===========================================================================


@pytest.mark.parametrize(
    "front, expected",
    [
        ("Word count: 4,512", [("text", 4512)]),
        ("Main text word count: 4512", [("text", 4512)]),
        ("Word count (excluding Methods, references and legends): 3,500", [("text", 3500)]),
        ("Abstract: 150 words; Main text: 4,512 words", [("abstract", 150), ("text", 4512)]),
        ("Abstract: 150 words, Main text: 4,512 words", [("abstract", 150), ("text", 4512)]),
        ("Figures: 6\nTables: 2", [("figures", 6), ("tables", 2)]),
        ("6 figures, 2 tables, 3 supplementary figures", [("figures", 6), ("tables", 2)]),
        ("Number of figures: six", [("figures", 6)]),
        ("Display items: 8", [("display_items", 8)]),
        ("Word limit: 3,000", []),
        ("Title: 12 words", []),
        ("Figure legends: 900 words", []),
        ("150-250 words", []),
        ("Received 3 November 2026", []),
        ("See Figure 1 and Table 2", []),
    ],
)
def test_stated_counts(front, expected):
    assert [(k, n) for k, n, _ in consistency.stated_counts(front)] == expected


def _words(n: int, word: str = "word") -> str:
    return " ".join([word] * n)


def _manuscript(tmp_path: Path, front: list[str], intro_words: int = 300, results_words: int = 200, figures: int = 3) -> Path:
    doc = tmp_path / "ms.docx"
    cites = " ".join(f"See Figure {i}." for i in range(1, figures + 1))
    _build_docx(
        doc,
        [
            "A Title",
            *front,
            "Abstract",
            "We study things in a short abstract here.",
            "Introduction",
            _words(intro_words),
            "Results",
            cites + " " + _words(results_words),
            "Acknowledgements",
            _words(80, "thanks"),
            "References",
            "1. A. Ref. Journal 2020.",
        ],
    )
    return doc


def _count_checks(tmp_path: Path, doc: Path, values: dict[str, str] | None = None, fields: tuple[Field, ...] = ()) -> Checks:
    checks = Checks()
    venue = Venue(slug="testj", name="Test", fields=[MANUSCRIPT, ABSTRACT, *fields])
    consistency.run_checks(checks, venue, {"manuscript_file": str(doc), **(values or {})})
    return checks


def test_matching_word_count_passes(tmp_path):
    # Introduction (1 + 300) + Results (1 + 6 + 200) = 508 words to the Acknowledgements.
    doc = _manuscript(tmp_path, ["Word count: 510"])
    status, text = _status(_count_checks(tmp_path, doc), "consistency.word_counts")
    assert status == report.PASSED, text


def test_stale_word_count_is_a_warning(tmp_path):
    doc = _manuscript(tmp_path, ["Word count: 900"], intro_words=1000, results_words=1000)
    status, text = _status(_count_checks(tmp_path, doc), "consistency.word_counts")
    assert status == report.WARNING and "'Word count: 900'" in text and "update the stated count" in text


def test_abstract_word_count_against_the_sub(tmp_path):
    doc = _manuscript(tmp_path, ["Abstract: 30 words"])
    status, text = _status(_count_checks(tmp_path, doc, {"abstract": _words(8, "short")}), "consistency.word_counts")
    assert status == report.WARNING and "abstract in the .sub is 8 words" in text
    status, _ = _status(_count_checks(tmp_path, doc, {"abstract": _words(31, "short")}), "consistency.word_counts")
    assert status == report.PASSED


def test_no_stated_counts_is_not_applicable(tmp_path):
    checks = _count_checks(tmp_path, _manuscript(tmp_path, []))
    assert _status(checks, "consistency.word_counts")[0] == report.NOT_APPLICABLE
    assert _status(checks, "consistency.display_counts")[0] == report.NOT_APPLICABLE


def test_main_text_starts_after_the_abstract_without_an_introduction():
    text = "Title\nAbstract\nWe find that cells divide quickly under stress.\n" + _words(100) + "\nReferences\n1. Ref."
    scopes = consistency.main_text_scopes(text, (), (), abstract="We find that cells divide quickly under stress.")
    assert scopes == {"from the end of the abstract to the references": 100}


def test_methods_after_results_is_a_scope():
    text = "Introduction\n" + _words(50) + "\nResults\n" + _words(50) + "\nMethods\n" + _words(400) + "\nReferences\n"
    scopes = consistency.main_text_scopes(text, (), ())
    assert scopes["from the Introduction to Methods"] == 102
    assert scopes["from the Introduction to the references"] == 503


def test_display_counts(tmp_path):
    doc = _manuscript(tmp_path, ["Figures: 3"], figures=3)
    assert _status(_count_checks(tmp_path, doc), "consistency.display_counts")[0] == report.PASSED
    doc = _manuscript(tmp_path, ["Figures: 5"], figures=3)
    status, text = _status(_count_checks(tmp_path, doc), "consistency.display_counts")
    assert status == report.WARNING and "has 3 main figures" in text


# ===========================================================================
# CLI
# ===========================================================================


def test_cli_passes_the_cover_letter_max_age(monkeypatch, tmp_path):
    from paperpush import cli

    seen = {}

    def fake(subfile, venue_def, subfile_path, *, ai=None, **flags):
        seen.update(flags)
        raise SystemExit(0)

    monkeypatch.setattr(cli, "validate_report_for", fake)
    sub = tmp_path / "biorxiv.sub"
    main(["subfile", "biorxiv", "-o", str(sub)])
    with pytest.raises(SystemExit):
        main(["validate", str(sub), "--cover-letter-max-age", "14"])
    assert seen["cover_letter_max_age_days"] == 14
