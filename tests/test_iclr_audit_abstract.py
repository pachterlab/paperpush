"""Regression tests for ``front_matter.abstract_citations`` from the ICLR 2026 precision audit.

In all 10 flagged camera-ready papers the abstract had no citations: pdftotext
splits ICLR's small-caps "1 Introduction" into "1 I NTRODUCTION", which no
end-of-abstract pattern matched, so the check read ~550-650 words on into the
Introduction and reported its citations. These mirror that layout (and others)
in short synthetic text; real abstract citations must still be reported.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paperpush import document
from paperpush import front_matter_check as fm

ABSTRACT = (
    "We introduce a structure-aware point cloud generation framework that models part\n"
    "composition and symmetry. Our experiments show that the method achieves strong\n"
    "performance in generating diverse point clouds while maintaining symmetry.\n"
)
INTRO_CITES = (
    "Structure-aware shape generation aims to model the holistic surface (Chaudhuri et al., 2020).\n"
    "Prior work (Schor et al., 2019; Li et al., 2020) treats shapes holistically, as Qin et al. (2021) note [3].\n"
)


def _doc(tmp_path: Path, text: str, name: str = "ms.txt") -> document.Document:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return document.get(path)


def _abstract(tmp_path: Path, text: str, name: str = "ms.txt") -> str:
    found = fm.manuscript_abstract(_doc(tmp_path, text, name))
    assert found is not None
    return found[0]


def _cites(tmp_path: Path, text: str, name: str = "ms.txt") -> list[str]:
    """What the check reports for the manuscript's abstract: [] when it is not read or has no citations."""
    checkable = fm._checkable_abstract(_abstract(tmp_path, text, name))
    return fm.citations_in(checkable) if checkable else []


# --- the ICLR layout: the abstract ends at a split small-caps heading -------------------------------


@pytest.mark.parametrize(
    "heading",
    ["1 I NTRODUCTION", "I NTRODUCTION", "1 INTRODUCTION", "1 Introduction", "1. Introduction", "1 M OTIVATION", "1 Background and Related Work", "I. RELATED WORK"],
)
def test_abstract_ends_at_the_first_section_heading(tmp_path, heading):
    text = f"Title of the Paper\nA BSTRACT\n{ABSTRACT}{heading}\n{INTRO_CITES}"
    found = _abstract(tmp_path, text)
    assert "maintaining symmetry" in found and "Chaudhuri" not in found
    assert _cites(tmp_path, text) == []


@pytest.mark.parametrize("tail", ["The code is released at https://github.com/example/repo.\n", "accelerates discovery compared to existing methods. 1\n"])
def test_url_or_footnote_marker_at_the_end_of_the_abstract(tmp_path, tail):
    # 2baJBgfr9S (a GitHub URL) and Pa6ak2B9jJ (a footnote marker "1" before "1 I NTRODUCTION").
    text = f"ABSTRACT\n{ABSTRACT}{tail}1 I NTRODUCTION\n{INTRO_CITES}"
    found = _abstract(tmp_path, text)
    assert tail.strip() in found and "Chaudhuri" not in found
    assert _cites(tmp_path, text) == []


@pytest.mark.parametrize("heading", ["Keywords: shapes, symmetry", "Index Terms", "CCS Concepts", "K EYWORDS"])
def test_front_matter_after_the_abstract_ends_it(tmp_path, heading):
    text = f"Abstract\n{ABSTRACT}{heading}\n{INTRO_CITES}"
    if ":" in heading:  # "Keywords: ..." on one line is not a heading line; the Introduction still ends it
        text = f"Abstract\n{ABSTRACT}Keywords\nshapes, symmetry\n1 Introduction\n{INTRO_CITES}"
    assert "Chaudhuri" not in _abstract(tmp_path, text)


def test_a_heading_after_a_paragraph_break_ends_the_abstract(tmp_path):
    text = f"Abstract\n\n{ABSTRACT}\nRelated Work\n{INTRO_CITES}"
    found = _abstract(tmp_path, text)
    assert "maintaining symmetry" in found and "Chaudhuri" not in found


def test_figure_text_between_abstract_and_introduction():
    # sQGlhjKUC0: a teaser figure's labels and caption come out between the abstract and "1 INTRODUCTION".
    figure = "Baseline\nOurs\n20°\n40°\nViT Attn Map\nQuery\nFigure 2: Overview of the method (Liu et al., 2023).\n"
    rest = f"{ABSTRACT}{figure}1 INTRODUCTION\n{INTRO_CITES}"
    found = rest[: fm._end_at_heading(rest, pdf=True)]
    # Six labels are too few to read as figure text (see the inline-math test), but the caption ends it.
    assert "maintaining symmetry" in found and "Liu et al." not in found and "Figure 2" not in found


def test_a_caption_ends_the_abstract_in_any_file(tmp_path):
    text = f"Abstract\n{ABSTRACT}Figure 1: Overview (Liu et al., 2023).\n1 I NTRODUCTION\n{INTRO_CITES}"
    assert "Liu et al." not in _abstract(tmp_path, text)


def test_a_line_starting_with_one_inside_the_abstract_does_not_end_it(tmp_path):
    text = f"Abstract\nWe scale the tokenizer to\n1 billion parameters and train it on speech.\nIt works well.\n1 Introduction\n{INTRO_CITES}"
    found = _abstract(tmp_path, text)
    assert "It works well." in found and "Chaudhuri" not in found


# --- when the end cannot be found, nothing is reported -------------------------------------------------


def test_runaway_abstract_without_a_paragraph_break_is_not_read():
    runaway = ("word " * 500) + "(Smith et al., 2020)"
    assert fm._checkable_abstract(runaway) is None


def test_runaway_abstract_is_cut_to_its_first_paragraph():
    runaway = ABSTRACT + "\n\n" + ("word " * 500) + "(Smith et al., 2020)"
    assert fm._checkable_abstract(runaway).strip() == ABSTRACT.strip()


def test_check_reports_unchecked_rather_than_citations_from_a_runaway(tmp_path, monkeypatch):
    from paperpush.validate import Unchecked

    runaway = ("word " * 500) + "(Smith et al., 2020)"
    monkeypatch.setattr(fm, "_abstracts", lambda venue, values: ([("manuscript_file", "the abstract in ms.pdf", runaway, False)], []))
    monkeypatch.setattr(fm, "_rules", lambda venue, values: None)
    with pytest.raises(Unchecked, match="could not tell where"):
        fm._abstract_citations_check(None, {})


def test_sub_abstract_is_still_read_when_the_manuscripts_is_a_runaway(monkeypatch):
    runaway = ("word " * 500) + "(Smith et al., 2020)"
    sub = ("We built a thing. " * 40) + "As shown by Jones et al. (2021), it works."
    monkeypatch.setattr(fm, "_abstracts", lambda venue, values: ([("abstract", "the .sub's abstract", sub, False), ("manuscript_file", "the abstract in ms.pdf", runaway, False)], []))
    monkeypatch.setattr(fm, "_rules", lambda venue, values: None)
    outcome = fm._abstract_citations_check(None, {})
    assert len(outcome.issues) == 1 and "Jones et al. (2021" in outcome.issues[0].message and "Smith" not in outcome.issues[0].message


# --- real abstract citations still fire ----------------------------------------------------------------


@pytest.mark.parametrize("cite", ["as shown by Smith et al. (2023)", "as shown before [3]", "as reported previously (Smith and Jones, 2019)"])
def test_iclr_layout_abstract_citation_still_fires(tmp_path, cite):
    text = f"A BSTRACT\nWe extend a method {cite} to point clouds.\n{ABSTRACT}1 I NTRODUCTION\n{INTRO_CITES}"
    found = _cites(tmp_path, text)
    assert found and not any("Chaudhuri" in c for c in found)


def test_nature_style_abstract_citation_still_fires(tmp_path):
    # Unnumbered headings, blank lines between paragraphs, "Introduction" alone on a line.
    text = f"A Title\n\nAuthors\n\nAbstract\n\nCell atlases have grown quickly [1, 2]. We present a method.\n\nIntroduction\n\n{INTRO_CITES}"
    assert _cites(tmp_path, text) == ["[1, 2]"]


def test_acl_two_column_abstract_citation_still_fires(tmp_path):
    # Short two-column lines, then "1 Introduction".
    text = "Abstract\nPrior work (Devlin et al., 2019)\nfine-tunes large models; we\nshow that a small adapter\nsuffices on most tasks and\nreleases our code.\n1 Introduction\n" + INTRO_CITES
    found = _cites(tmp_path, text)
    assert found == ["(Devlin et al., 2019)"]


def test_figure_labels_without_a_caption_end_the_abstract_in_a_pdf():
    figure = "".join(f"{label}\n" for label in ["Baseline", "Ours", "20°", "40°", "ViT Attn Map", "Query", "LLM Sinks", "Image & Query", "Or"])
    rest = f"{ABSTRACT}{figure}(Liu et al., 2023)\nmore\n1 INTRODUCTION\n{INTRO_CITES}"
    found = rest[: fm._end_at_heading(rest, pdf=True)]
    assert "maintaining symmetry" in found and "Baseline" not in found and "Liu et al." not in found


def test_inline_math_broken_into_short_lines_does_not_end_the_abstract():
    # 0YjO70etNj: "rates of order O(N^-1/2) and O(N^-2/3) for the ..." comes out as six short lines.
    rest = "We show that the rates are of order O\n(\nN-1/2)\nand\nO\n(\nN-2/3)\nfor the simultaneous and alternating schemes, respectively.\n1 I NTRODUCTION\n" + INTRO_CITES
    found = rest[: fm._end_at_heading(rest, pdf=True)]
    assert "alternating schemes" in found and "Chaudhuri" not in found


def test_two_column_pdf_lines_are_not_mistaken_for_figure_text():
    rest = "Prior work (Devlin et al., 2019)\nfine-tunes large models; we\nshow that a small adapter\nsuffices on most tasks and\nreleases our code.\n1 Introduction\n" + INTRO_CITES
    found = rest[: fm._end_at_heading(rest, pdf=True)]
    assert "(Devlin et al., 2019)" in found and "Chaudhuri" not in found


def test_structured_abstract_labels_after_a_paragraph_break_do_not_end_it(tmp_path):
    text = "Abstract\n\nBackground\nPrior trials disagree (Smith et al., 2020).\n\nMethods\nWe pooled them.\n\nResults\nIt works.\n\nIntroduction\n" + INTRO_CITES
    found = _abstract(tmp_path, text)
    assert "It works." in found and "Chaudhuri" not in found
    assert _cites(tmp_path, text) == ["(Smith et al., 2020)"]
