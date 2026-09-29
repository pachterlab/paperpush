"""Unit tests for :mod:`paperpush.template_check` (modified-template detection).

Synthetic PDFs lay out body text on a known grid (``tests/pdf_builder.py``):
a template-conforming one, and ones with a wider text block, tighter line
spacing, a smaller font, or no running head. The LaTeX source checks run on
inline strings.
"""

from __future__ import annotations

import pytest

from paperpush import pdf_layout, template_check
from paperpush.database import get_venue
from paperpush.requirements import TemplateLayout, get_requirements
from paperpush.subfile import SubFile
from paperpush.validate import validate
from tests.pdf_builder import text_line, write_pdf

# The PDF tests read glyphs through the optional pdfminer.six ("paperpush[validate]").
needs_pdfminer = pytest.mark.skipif(not pdf_layout.available(), reason="needs pdfminer.six (pip install 'paperpush[validate]')")

HEAD = "Under review as a conference paper at ICLR 2027"
LINE = "The quick brown fox jumps over the lazy dog while the model trains."


def _page(number: int, *, left: float = 108, top: float = 700, lines: int = 58, skip: float = 11, size: float = 10, head: str = HEAD) -> str:
    stream = text_line(108, 757, head, size=size) if head else ""
    for i in range(lines):
        # Distinct per page and line, as real body text is (identical rows at the
        # same height on every page would read as a running head).
        stream += text_line(left, top - i * skip, f"{LINE} p{number}l{i:02d}", size=size)
    # Review line numbers in the left margin, and the page number.
    for i in range(lines):
        stream += text_line(75, top - i * skip, f"{i:03d}", size=8)
    return stream + text_line(303, 30, str(number), size=size)


def _pdf(tmp_path, **kwargs):
    return write_pdf(tmp_path / "paper.pdf", [_page(n, **kwargs) for n in range(1, 5)])


def _layout(tmp_path) -> TemplateLayout:
    """The layout a conforming synthetic paper measures as (its right edge depends on LINE)."""
    m = template_check.measure(_pdf(tmp_path), HEAD)
    return TemplateLayout(
        page_width_pt=612,
        page_height_pt=792,
        text_left_pt=108,
        text_right_pt=m.right,
        text_top_pt=700,
        text_bottom_pt=m.bottom_by_page[0],
        body_font_pt=10,
        baseline_skip_pt=11,
        running_head=HEAD,
        margin_line_numbers=True,
    )


def _problems(path, layout) -> list[str]:
    return list(template_check.layout_problems(template_check.measure(path, HEAD), layout))


@needs_pdfminer
def test_conforming_pdf_measures_clean(tmp_path):
    layout = _layout(tmp_path)
    m = template_check.measure(_pdf(tmp_path), HEAD)
    assert (m.body_font, m.baseline_skip, m.left) == (10, 11, 108)
    assert m.margin_line_numbers
    assert list(template_check.layout_problems(m, layout)) == []


@needs_pdfminer
def test_shrunken_margin_detected(tmp_path):
    layout = _layout(tmp_path)
    (tmp_path / "wide").mkdir()
    problems = _problems(_pdf(tmp_path / "wide", left=86), layout)
    assert any("from the left edge" in p for p in problems)


@needs_pdfminer
def test_tighter_line_spacing_detected(tmp_path):
    layout = _layout(tmp_path)
    (tmp_path / "tight").mkdir()
    problems = _problems(_pdf(tmp_path / "tight", skip=10), layout)
    assert any("line spacing is 10pt" in p for p in problems)


@needs_pdfminer
def test_smaller_font_detected(tmp_path):
    layout = _layout(tmp_path)
    (tmp_path / "small").mkdir()
    problems = _problems(_pdf(tmp_path / "small", size=9), layout)
    assert any("body text is 9pt" in p for p in problems)


@needs_pdfminer
def test_text_below_bottom_margin_detected(tmp_path):
    layout = _layout(tmp_path)
    (tmp_path / "long").mkdir()
    problems = _problems(_pdf(tmp_path / "long", lines=62), layout)
    assert any("bottom margin" in p for p in problems)


@needs_pdfminer
def test_missing_running_head_detected(tmp_path):
    layout = _layout(tmp_path)
    (tmp_path / "nohead").mkdir()
    problems = _problems(_pdf(tmp_path / "nohead", head=""), layout)
    assert any("running head" in p for p in problems)


@needs_pdfminer
def test_repeated_header_is_not_body_text(tmp_path):
    # A running head whose text extracts garbled is still recognised as a header
    # because it repeats at the same height on every page.
    layout = _layout(tmp_path)
    (tmp_path / "garbled").mkdir()
    problems = _problems(_pdf(tmp_path / "garbled", head="Publiced as a conference paper at ICLR 2026"), layout)
    assert not any("top margin" in p for p in problems)


@pytest.mark.parametrize(
    "source,expected",
    [
        ("\\usepackage[margin=1in]{geometry}", "page geometry"),
        ("\\addtolength{\\textheight}{0.5in}", "changes \\textheight"),
        ("\\setlength{\\textwidth}{6in}", "changes \\textwidth"),
        ("\\linespread{0.95}", "line spacing"),
        ("\\renewcommand{\\baselinestretch}{0.9}", "line spacing"),
        ("\\setlength{\\textfloatsep}{2pt}", "changes \\textfloatsep"),
        ("\\usepackage{savetrees}", "space-saving package"),
        ("\\begin{document}\\small", "shrinks the body font"),
    ],
)
def test_source_edits_detected(source, expected):
    assert any(expected in p for p in template_check.source_problems(source))


def test_commented_source_edits_ignored():
    assert list(template_check.source_problems("% \\usepackage{geometry}\n% \\linespread{0.9}\n")) == []


def test_many_negative_vspaces_detected():
    few = "\\vspace{-2pt}\n" * 3
    many = "\\vspace{-2pt}\n" * template_check.MAX_NEGATIVE_VSPACE
    assert list(template_check.source_problems(few)) == []
    assert any("negative \\vspace" in p for p in template_check.source_problems(many))


# --- requirements data and validate wiring ---------------------------------------------


def test_iclr_requirements_carry_the_template_layout():
    layout = get_requirements("iclr_2027").manuscript.template_layout
    assert layout is not None
    assert (layout.text_left_pt, layout.text_right_pt, layout.body_font_pt, layout.baseline_skip_pt) == (108, 504, 10, 11)
    assert layout.running_head == HEAD


@needs_pdfminer
def test_validate_flags_edited_template_for_iclr(tmp_path):
    (tmp_path / "wide").mkdir()
    pdf = _pdf(tmp_path / "wide", left=80, skip=10)
    tex = tmp_path / "main.tex"
    tex.write_text("\\documentclass{article}\\usepackage{iclr2027_conference}\\linespread{0.9}\n", encoding="utf-8")
    venue = get_venue("iclr_2027")
    sub = SubFile(venue="iclr_2027", values={"pdf_file": str(pdf), "supplementary_material": str(tex)})
    messages = [i.message for i in validate(sub, venue, check_links=False, check_references=False, check_sensitive=False, check_anonymous=False, check_hidden_text=False, check_openreview=False) if i.message.startswith("template:")]
    assert any("from the left edge" in m for m in messages)
    assert any("line spacing" in m for m in messages)
    assert any("main.tex changes line spacing" in m for m in messages)
    assert any("desk-rejected" in m for m in messages)
