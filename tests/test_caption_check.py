"""paperpush.citation_check: captions that are duplicated or missing."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperpush import citation_check as cc
from paperpush import report
from paperpush.database import Field, Venue
from paperpush.validate import Checks

MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
FIGURES = Field(id="figures", label="Figures", type="filelist")
TABLES = Field(id="tables", label="Tables", type="filelist")
VENUE = Venue(slug="testj", name="Test Venue", fields=[MANUSCRIPT, FIGURES, TABLES])


def _run(values: dict[str, str]) -> dict[str, tuple[str, str, str]]:
    checks = Checks()
    cc.run_checks(checks, VENUE, values)
    return {r.id: (r.status, " | ".join(i.message for i in r.issues), r.detail) for r in checks.results}


def _ms(tmp_path: Path, content: str, name: str = "ms.txt") -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def _png(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(path)


# --- duplicates ---------------------------------------------------------------------------------


def test_same_text_on_two_numbers():
    found = [cc.Caption("figure", 1, "Figure 1. Survival of treated mice over time."), cc.Caption("figure", 2, "Figure 2. Survival of treated mice over time.")]
    problems = cc.duplicate_caption_problems(found)
    assert len(problems) == 1 and "Figure 1 and Figure 2 have the same caption" in problems[0]


def test_same_number_twice():
    found = [cc.Caption("table", 1, "Table 1. Baseline characteristics of patients."), cc.Caption("table", 1, "Table 1. Adverse events by treatment arm.")]
    assert "2 different captions are numbered Table 1" in cc.duplicate_caption_problems(found)[0]


def test_same_caption_printed_twice():
    found = [cc.Caption("figure", 3, "Figure 3. Survival of treated mice."), cc.Caption("figure", 3, "Figure 3. Survival of treated mice.")]
    problems = cc.duplicate_caption_problems(found)
    assert problems == ["the caption of Figure 3 appears 2 times; keep one"]


def test_continued_table_and_short_titles_are_not_duplicates():
    found = [cc.Caption("table", 1, "Table 1. Baseline characteristics of patients."), cc.Caption("table", 1, "Table 1 (continued). Baseline characteristics of patients.")]
    assert cc.duplicate_caption_problems(found) == []
    assert cc.duplicate_caption_problems([cc.Caption("table", 1, "Table 1. Results."), cc.Caption("table", 2, "Table 2. Results.")]) == []


def test_figure_and_table_may_share_a_number():
    found = [cc.Caption("figure", 1, "Figure 1. Survival of treated mice."), cc.Caption("table", 1, "Table 1. Baseline characteristics of patients.")]
    assert cc.duplicate_caption_problems(found) == []


def test_duplicate_captions_in_a_manuscript(tmp_path):
    text = "We saw it (Fig. 1) and again (Fig. 2).\n\nFigure 1. Survival of treated mice over time.\n\nFigure 2. Survival of treated mice over time.\n"
    status, message, _ = _run({"manuscript_file": _ms(tmp_path, text)})["citations.duplicate_captions"]
    assert status == report.WARNING and "the same caption" in message


def test_distinct_captions_pass(tmp_path):
    text = "We saw it (Fig. 1) and again (Fig. 2).\n\nFigure 1. Survival of treated mice.\n\nFigure 2. Weight of treated mice.\n"
    results = _run({"manuscript_file": _ms(tmp_path, text)})
    assert results["citations.duplicate_captions"][0] == report.PASSED
    assert results["citations.missing_captions"][0] == report.PASSED


def test_duplicate_captions_in_latex_floats(tmp_path):
    source = "\\documentclass{article}\n\\begin{document}\n" + "\\begin{figure}\\caption{Survival of treated mice over time.}\\end{figure}\n" * 2 + "\\end{document}\n"
    status, message, _ = _run({"manuscript_file": _ms(tmp_path, source, "ms.tex")})["citations.duplicate_captions"]
    assert status == report.WARNING and "Figure 1 and Figure 2" in message


def test_subfigure_captions_do_not_stand_in_for_the_float(tmp_path):
    source = "\\begin{figure}\\begin{subfigure}\\caption{a}\\end{subfigure}\\caption{Survival of treated mice.}\\end{figure}\n"
    assert cc._tex_float_captions(source)[0][2] == "Survival of treated mice."


# --- missing ------------------------------------------------------------------------------------


def test_cited_with_no_captions_at_all(tmp_path):
    status, message, _ = _run({"manuscript_file": _ms(tmp_path, "We saw it (Fig. 1) and in Table 1.\n")})["citations.missing_captions"]
    assert status == report.WARNING and "Figure 1: no caption found" in message and "Table 1: no caption found" in message


def test_attached_numbered_file_with_no_caption(tmp_path):
    text = "We saw it (Fig. 1) and (Fig. 2).\n\nFigure 1. Survival of treated mice.\n"
    values = {"manuscript_file": _ms(tmp_path, text), "figures": _png(tmp_path, "Fig1.png") + "\n" + _png(tmp_path, "figure_2.png")}
    status, message, _ = _run(values)["citations.missing_captions"]
    assert status == report.WARNING and "Figure 2: no caption found" in message and "Figure 1:" not in message


def test_panel_files_of_one_figure_are_one_figure(tmp_path):
    text = "We saw it (Fig. 1).\n\nFigure 1. Survival of treated mice.\n"
    values = {"manuscript_file": _ms(tmp_path, text), "figures": _png(tmp_path, "Fig1a.png") + "\n" + _png(tmp_path, "Fig1b.png")}
    assert _run(values)["citations.missing_captions"][0] == report.PASSED


def test_attached_unnumbered_files_with_no_captions(tmp_path):
    values = {"manuscript_file": _ms(tmp_path, "Plain text with no figures named.\n"), "figures": _png(tmp_path, "survival.png")}
    status, message, _ = _run(values)["citations.missing_captions"]
    assert status == report.WARNING and "1 figure file(s) attached" in message


def test_caption_in_an_attached_legends_file_counts(tmp_path):
    legends = _ms(tmp_path, "Figure 1. Survival of treated mice.\n", "legends.txt")
    values = {"manuscript_file": _ms(tmp_path, "We saw it (Fig. 1).\n"), "figures": legends}
    assert _run(values)["citations.missing_captions"][0] == report.PASSED


def test_spreadsheet_table_needs_no_caption(tmp_path):
    table = tmp_path / "table1.xlsx"
    table.write_bytes(b"x")
    values = {"manuscript_file": _ms(tmp_path, "Plain text with nothing named.\n"), "tables": str(table)}
    assert _run(values)["citations.missing_captions"][0] == report.NOT_APPLICABLE


def test_latex_float_with_no_caption_is_named(tmp_path):
    source = "\\documentclass{article}\n\\begin{document}\nAs in Fig.~\\ref{fig:a}.\n" "\\begin{figure}\\includegraphics{a.png}\\label{fig:a}\\end{figure}\n" "\\begin{table}\\caption{Baseline characteristics.}\\end{table}\n" "\\begin{figure}\\includegraphics[width=2in]{plots/b.png}\\end{figure}\n\\end{document}\n"
    status, message, _ = _run({"manuscript_file": _ms(tmp_path, source, "ms.tex")})["citations.missing_captions"]
    assert status == report.WARNING and "'fig:a' has no \\caption" in message and "'b.png' has no \\caption" in message
    assert "no caption found" not in message  # the same figures are not reported twice


def test_latex_custom_caption_macro_counts_as_a_caption(tmp_path):
    source = "\\documentclass{article}\n\\begin{document}\n\\begin{figure}\\includegraphics{a.png}\\captionof{figure}{A.}\\end{figure}\n\\end{document}\n"
    assert _run({"manuscript_file": _ms(tmp_path, source, "ms.tex")})["citations.missing_captions"][0] == report.PASSED


def test_no_figures_is_not_applicable(tmp_path):
    results = _run({"manuscript_file": _ms(tmp_path, "Plain text with nothing named.\n")})
    assert results["citations.missing_captions"][0] == report.NOT_APPLICABLE
    assert results["citations.duplicate_captions"][0] == report.NOT_APPLICABLE


def test_no_manuscript_is_not_applicable():
    results = _run({})
    assert results["citations.missing_captions"][0] == report.NOT_APPLICABLE
    assert results["citations.duplicate_captions"][0] == report.NOT_APPLICABLE
