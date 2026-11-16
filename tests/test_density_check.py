"""Unit tests for :mod:`paperpush.density_check` (page fill and space balance at AI conferences)."""

from __future__ import annotations

import pytest

from paperpush import density_check, pdf_layout
from paperpush.database import get_venue
from paperpush.validate import NotApplicable
from tests.pdf_builder import text_line, write_pdf

needs_pdfminer = pytest.mark.skipif(not pdf_layout.available(), reason="needs pdfminer.six (pip install 'paperpush[validate]')")

LINE = "The quick brown fox jumps over the lazy dog while the model trains."


def _page(number: int, *, lines: int = 58, refs_at: int | None = None) -> str:
    stream = text_line(108, 757, "Under review as a conference paper at ICLR 2027")
    for i in range(lines):
        text = "References" if refs_at == i else f"{LINE} p{number}l{i:02d}"
        stream += text_line(108, 700 - i * 11, text)
    return stream + text_line(303, 30, str(number))


def _pdf(tmp_path, pages):
    return write_pdf(tmp_path / "paper.pdf", pages)


@needs_pdfminer
def test_full_pages_have_no_gaps(tmp_path):
    path = _pdf(tmp_path, [_page(1), _page(2), _page(3), _page(4, refs_at=0)])
    d = density_check.measure(pdf_layout.read_pages(path))
    assert d is not None and d.ref_found
    assert d.pages == []
    assert d.main_pages == pytest.approx(3.0, abs=0.1)


@needs_pdfminer
def test_half_empty_page_is_reported(tmp_path):
    path = _pdf(tmp_path, [_page(1), _page(2, lines=25), _page(3), _page(4, refs_at=20)])
    d = density_check.measure(pdf_layout.read_pages(path))
    assert [p.number for p in d.pages] == [2]
    assert d.pages[0].where == "bottom"
    assert d.pages[0].empty_share > 0.4
    # Three full pages plus a third of the fourth.
    assert d.main_pages == pytest.approx(3.35, abs=0.1)


def test_section_shares_from_text_follow_the_numbering():
    text = "\n".join(
        [
            "Title",
            "Abstract",
            "word " * 50,
            "1 I NTRODUCTION",
            "word " * 300,
            "2 R ELATED WORK",
            "word " * 600,
            "3 Method",
            "word " * 900,
            "4 Experiments",
            "word " * 900,
            "5 Conclusion",
            "word " * 100,
            "References",
            "word " * 500,
        ]
    )
    words = density_check.section_shares_from_text(text)
    assert words == {"Introduction": 300, "Related work": 600, "Other": 1800, "Conclusion": 100}


def test_numbered_list_items_are_not_sections():
    # Only one numbered heading in sequence: too few to split the paper.
    text = "1 Introduction\n" + "word " * 100 + "\n7 Items were removed\n" + "word " * 100
    assert density_check.section_shares_from_text(text) is None


def test_section_shares_from_tex_stop_at_the_appendix():
    source = r"""
\section{Introduction} one two three four
\section{Related Work} one two
\section{Our Method} one two three four five six
\section{Conclusion} one
\appendix
\section{Proofs} one two three four five six seven eight
"""
    assert density_check.section_shares_from_tex(source) == {"Introduction": 4, "Related work": 2, "Other": 6, "Conclusion": 1}


def test_journals_are_not_checked():
    with pytest.raises(NotApplicable):
        density_check.density_check(get_venue("nature"), {})
    with pytest.raises(NotApplicable):
        density_check.balance_check(get_venue("nature"), {})


def test_conferences_are_checked():
    assert density_check.applies(get_venue("iclr_2027"))
    assert density_check.applies(get_venue("cvpr_2027"))
    assert not density_check.applies(get_venue("tmlr"))


@needs_pdfminer
def test_main_text_ending_before_a_new_page_is_not_a_gap(tmp_path):
    # The main text stops a third of the way down page 3 and the
    # reproducibility statement starts page 4: the end of the paper, not a gap.
    path = _pdf(tmp_path, [_page(1), _page(2), _page(3, lines=20), _page(4, refs_at=0)])
    pages = pdf_layout.read_pages(path)
    d = density_check.measure(pages)
    assert d.pages == []
    assert d.main_pages == pytest.approx(2.35, abs=0.1)
