"""Detect a manuscript that was squeezed out of its venue's template.

Venues with a mandatory style file (ICLR, NeurIPS, ...) desk-reject papers that
win space by editing it: shrinking the margins, the body font, or the line
spacing. The compiled PDF gives that away, because its geometry no longer
matches the template's. ``manuscript_requirements.json`` records that geometry
per venue under ``manuscript.template_layout`` (measured from the official
template compiled as-is), and this module measures the uploaded PDF the same
way and reports every dimension that moved.

When LaTeX source is attached (directly or in a bundle), it is also read for
the edits that produce those changes -- ``geometry``, ``\\setlength{\\textheight}``,
``\\linespread`` -- and for heavy use of negative ``\\vspace``, which the PDF
cannot show.

Measurements use the body text only: glyphs at the body font size outside
figures, grouped into lines. The running head, page numbers, and the review
line-number ruler are left out.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .pdf_layout import MAX_PAGES, Page, read_pages
from .requirements import TemplateLayout

logger = logging.getLogger(__name__)

# Pages measured: the same page cap as the hidden-text scan, so both checks
# share one cached parse of the PDF.
MAX_TEMPLATE_PAGES = MAX_PAGES
# Only pages carrying at least this many body lines vote on the top/bottom of
# the text block (a title-only or figure-only page does not reach either edge).
MIN_LINES_FOR_EXTENT = 20
# A page's text reaching past the template's top/bottom is reported once this
# many pages do it (a single overfull page is a LaTeX warning, not a template).
MIN_PAGES_OVER = 2


@dataclass(frozen=True)
class _Line:
    page: int
    y: float
    x0: float
    x1: float
    text: str


@dataclass(frozen=True)
class LayoutMeasurement:
    """What the manuscript's body text looks like, in PDF points."""

    pages: int
    page_width: float
    page_height: float
    body_font: float | None
    baseline_skip: float | None
    left: float | None
    right: float | None
    top_by_page: tuple[float, ...]
    bottom_by_page: tuple[float, ...]
    first_page_text: str
    margin_line_numbers: bool


def _squash(text: str) -> str:
    """Text without whitespace, lowercased (pdfminer's raw glyphs carry no spaces)."""
    return re.sub(r"\s+", "", text).lower()


def _mode(values, ndigits: int = 1) -> float | None:
    counts = Counter(round(v, ndigits) for v in values)
    return counts.most_common(1)[0][0] if counts else None


def _repeated_rows(pages: list[Page], body_font: float) -> set[tuple[int, str]]:
    """``(y, text)`` of lines repeated at the same height on many pages.

    Running heads and footers repeat verbatim page after page; body text never
    does. Found this way rather than by wording, because a header's extracted
    text can be garbled by its font encoding.
    """
    counts: Counter = Counter()
    for page in pages:
        rows: dict[int, list] = {}
        for g in page.glyphs:
            if not g.in_figure and g.text.strip() and abs(g.size - body_font) <= 0.3:
                rows.setdefault(round(g.y0), []).append(g)
        for y, glyphs in rows.items():
            counts[(y, _squash("".join(g.text for g in sorted(glyphs, key=lambda g: g.x0))))] += 1
    threshold = max(3, len(pages) // 2)
    return {key for key, n in counts.items() if n >= threshold}


def _lines(page: Page, body_font: float, running_head: str, repeated: set[tuple[int, str]] = frozenset()) -> list[_Line]:
    """Body-text lines of one page, running head and page number removed."""
    rows: dict[float, list] = {}
    for g in page.glyphs:
        if g.in_figure or not g.text.strip() or abs(g.size - body_font) > 0.3:
            continue
        rows.setdefault(round(g.y0 * 2) / 2, []).append(g)
    head = _squash(running_head) if running_head else ""
    lines: list[_Line] = []
    for y, glyphs in rows.items():
        glyphs.sort(key=lambda g: g.x0)
        text = "".join(g.text for g in glyphs)
        squashed = _squash(text)
        if squashed.isdigit() and len(squashed) <= 3:
            continue  # page number
        if head and head in squashed:
            continue
        if (round(glyphs[0].y0), squashed) in repeated:
            continue  # running head / footer
        if re.fullmatch(r"(underreviewas|publishedas)[a-z0-9]*", squashed):
            continue  # another year's/venue's running head
        lines.append(_Line(page.number, y, glyphs[0].x0, glyphs[-1].x1, text))
    return sorted(lines, key=lambda line: -line.y)


def measure(path: Path, running_head: str = "", max_pages: int = MAX_TEMPLATE_PAGES) -> LayoutMeasurement | None:
    """Measure the body-text geometry of a PDF, or None when it cannot be read."""
    pages = read_pages(path, max_pages)
    if not pages:
        return None
    sizes = [g.size for p in pages for g in p.glyphs if not g.in_figure and g.text.strip()]
    body_font = _mode(sizes)
    if body_font is None:
        return None

    all_lines: list[_Line] = []
    tops: list[float] = []
    bottoms: list[float] = []
    gaps: list[float] = []
    repeated = _repeated_rows(pages, body_font)
    for page in pages:
        lines = _lines(page, body_font, running_head, repeated)
        all_lines.extend(lines)
        if len(lines) >= MIN_LINES_FOR_EXTENT:
            tops.append(lines[0].y)
            bottoms.append(lines[-1].y)
        for above, below in zip(lines, lines[1:]):
            gap = above.y - below.y
            if 0.8 * body_font <= gap <= 1.6 * body_font:
                gaps.append(gap)

    first = pages[0]
    first_text = "".join(g.text for g in first.glyphs)
    left_edge = _mode([line.x0 for line in all_lines], 0)
    # Small digits left of the text block: the review line-number ruler.
    ruler = sum(1 for g in first.glyphs if g.text.isdigit() and g.size < body_font - 0.5 and left_edge is not None and g.x1 < left_edge)
    return LayoutMeasurement(
        pages=len(pages),
        page_width=first.width,
        page_height=first.height,
        body_font=body_font,
        baseline_skip=_mode(gaps),
        left=left_edge,
        right=_mode([line.x1 for line in all_lines], 0),
        top_by_page=tuple(tops),
        bottom_by_page=tuple(bottoms),
        first_page_text=first_text,
        margin_line_numbers=ruler >= 20,
    )


def layout_problems(m: LayoutMeasurement, layout: TemplateLayout, font_size_pt: float | None = None) -> Iterator[str]:
    """Each way the measured PDF departs from the template, as a sentence."""
    tol = layout.tolerance_pt
    if layout.page_width_pt and layout.page_height_pt:
        if abs(m.page_width - layout.page_width_pt) > 2 or abs(m.page_height - layout.page_height_pt) > 2:
            yield f"page size is {m.page_width / 72:.2f} x {m.page_height / 72:.2f} in; the template uses {layout.page_width_pt / 72:.2f} x {layout.page_height_pt / 72:.2f} in"
    expected_font = layout.body_font_pt or font_size_pt
    if expected_font and m.body_font is not None and m.body_font < expected_font - 0.25:
        yield f"body text is {m.body_font:g}pt; the template sets {expected_font:g}pt"
    if layout.baseline_skip_pt and m.baseline_skip is not None and m.baseline_skip < layout.baseline_skip_pt - 0.25:
        yield f"line spacing is {m.baseline_skip:g}pt baseline to baseline; the template uses {layout.baseline_skip_pt:g}pt (\\linespread or \\baselinestretch changed?)"
    if layout.text_left_pt is not None and m.left is not None and abs(m.left - layout.text_left_pt) > tol:
        yield f"text starts {m.left / 72:.2f} in from the left edge; the template's margin is {layout.text_left_pt / 72:.2f} in"
    if layout.text_right_pt is not None and m.right is not None and abs(m.right - layout.text_right_pt) > tol:
        width, expected = (m.right - (m.left or 0)) / 72, (layout.text_right_pt - (layout.text_left_pt or 0)) / 72
        yield f"text block is {width:.2f} in wide; the template's is {expected:.2f} in"
    if layout.text_top_pt is not None:
        over = [t for t in m.top_by_page if t > layout.text_top_pt + tol]
        if len(over) >= MIN_PAGES_OVER:
            yield f"body text starts {(max(over) - layout.text_top_pt) / 72:.2f} in above the template's top margin on {len(over)} page(s)"
    if layout.text_bottom_pt is not None:
        over = [b for b in m.bottom_by_page if b < layout.text_bottom_pt - tol]
        if len(over) >= MIN_PAGES_OVER:
            yield f"body text runs {(layout.text_bottom_pt - min(over)) / 72:.2f} in into the template's bottom margin on {len(over)} page(s)"
    if layout.running_head and _squash(layout.running_head) not in _squash(m.first_page_text):
        yield f"the running head '{layout.running_head}' is missing from page 1 (wrong template year, camera-ready mode, or an edited style file)"
    if layout.margin_line_numbers and not m.margin_line_numbers:
        yield "the review line numbers in the margin are missing (camera-ready mode or an edited style file)"


# --- LaTeX source ---------------------------------------------------------------

_SOURCE_EDITS = [
    (re.compile(r"\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^}]*\bgeometry\b[^}]*\}|\\(?:new)?geometry\s*\{"), "loads/changes page geometry"),
    (re.compile(r"\\(?:setlength|addtolength)\s*\{?\s*\\(textwidth|textheight|oddsidemargin|evensidemargin|topmargin|headheight|headsep|footskip|marginparwidth|columnsep|voffset|hoffset)\b"), "changes \\{0}"),
    (re.compile(r"\\(?:textwidth|textheight|oddsidemargin|evensidemargin|topmargin)\s*=?\s*-?[\d.]"), "assigns a page dimension"),
    (re.compile(r"\\linespread\s*\{|\\renewcommand\s*\{?\s*\\baselinestretch|\\setstretch\s*\{|\\usepackage\s*(?:\[[^\]]*\])?\s*\{\s*setspace\s*\}"), "changes line spacing"),
    (re.compile(r"\\(?:setlength|addtolength)\s*\{?\s*\\(baselineskip|parskip|abovedisplayskip|belowdisplayskip|textfloatsep|floatsep|intextsep|abovecaptionskip|belowcaptionskip)\b"), "changes \\{0}"),
    (re.compile(r"\\usepackage\s*(?:\[[^\]]*\])?\s*\{\s*(?:savetrees|titlesec|fullpage|a4wide)\s*\}"), "loads a space-saving package"),
    # Document-wide only: \fontsize and \small inside a table or caption are routine.
    (re.compile(r"\\begin\s*\{\s*document\s*\}\s*\\(?:small|footnotesize|scriptsize)\b"), "shrinks the body font"),
]
_NEGATIVE_VSPACE = re.compile(r"\\vspace\*?\s*\{\s*-")
# Negative \vspace this many times or more is reported (a few are routine).
MAX_NEGATIVE_VSPACE = 10


def _strip_comments(text: str) -> str:
    from .sensitive import _latex_comment

    out = []
    for line in text.splitlines():
        comment = _latex_comment(line)
        out.append(line if comment is None else line[: len(line) - len(comment) - 1])
    return "\n".join(out)


def source_problems(text: str) -> Iterator[str]:
    """Template-changing edits in one LaTeX source file, as sentences."""
    body = _strip_comments(text)
    seen: set[str] = set()
    for pattern, what in _SOURCE_EDITS:
        for match in pattern.finditer(body):
            message = what.format(*(match.groups() or ("",)))
            if message in seen:
                continue
            seen.add(message)
            yield f"{message} ({match.group(0).strip()[:60]})"
    negatives = len(_NEGATIVE_VSPACE.findall(body))
    if negatives >= MAX_NEGATIVE_VSPACE:
        yield f"uses negative \\vspace {negatives} times to pull text together"


def iter_latex_sources(path: Path) -> Iterator[tuple[str, str]]:
    """``(where, text)`` for each ``.tex`` file at ``path`` or inside it (an archive)."""
    from .sensitive import ARCHIVE_EXTS, MAX_TEXT_BYTES, _decode, _iter_archive_members

    suffix = path.suffix.lower()
    double = "".join(path.suffixes[-2:]).lower()
    try:
        if suffix == ".tex":
            yield path.name, path.read_text("utf-8", "replace")
        elif suffix in ARCHIVE_EXTS or double in ARCHIVE_EXTS:
            for name, data in _iter_archive_members(path):
                if name.lower().endswith(".tex") and data and len(data) <= MAX_TEXT_BYTES:
                    text = _decode(data)
                    if text is not None:
                        yield f"{path.name}:{name}", text
    except Exception:
        logger.debug("could not read LaTeX source from %s", path, exc_info=True)
