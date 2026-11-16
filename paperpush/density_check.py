"""How full a conference paper's pages are, and how its space is split.

Reviewers at AI conferences (ICLR, NeurIPS, CVPR, ACL, ...) read a paper that
leaves its page limit unused, or that has half-empty pages from badly placed
figures, as less finished. They also notice when the space goes to the wrong
places: a three-page introduction, a related-work section longer than the
method, a page of equations with no figure in sight. None of this breaks a
rule, so every finding here is a SUGGESTION.

Two checks, both measured on the manuscript PDF (a LaTeX source is compiled
first):

* ``layout.density`` -- large empty vertical gaps on the main-text pages
  (a float pushed to the next page leaves the bottom of this one blank), and a
  main text that stops well short of the venue's page limit.
* ``layout.section_balance`` -- the share of the main text each part takes
  (introduction, related work, conclusion, limitations, the rest), the share of
  the text block given to figures, and the share of lines that are display
  equations, against what papers accepted at these venues usually do.

The ranges were set from the main text of accepted ICLR papers: a value
outside the range most accepted papers fall in is reported, with the measured
value, as something to look at. A theory paper may rightly be equation-heavy,
a position paper figure-free, so these never become warnings.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import document
from .database import Field, Venue
from .requirements import get_requirements, resolve
from .validate import SUGGESTION, Checks, Issue, NotApplicable, Outcome, Unchecked

logger = logging.getLogger(__name__)

LAYOUT = "Layout"

# --- thresholds -------------------------------------------------------------
# A vertical gap in the text block counts as empty space once it is taller than
# this many body lines (a section break or the space around a figure is 2-3).
GAP_LINES = 5.0
# A page is reported when its empty gaps add up to this share of the text block
# (no accepted ICLR 2026 paper of 195 has such a page; 99th percentile 15%).
PAGE_EMPTY_SHARE = 0.20
# The main text is reported as short when it leaves at least this much of the
# page limit unused (in pages).
UNUSED_PAGES = 0.5
# Share of the main text's words, (low, high); None = no bound. In 195 accepted
# ICLR 2026 papers (5th-95th percentile): introduction 10%-29%, related work up
# to 19%, conclusion up to 7%, limitations up to 7%; introduction + related
# work + background up to 45%. Each bound below flags at most 4% of them.
SECTION_RANGES: dict[str, tuple[float | None, float | None]] = {
    "Introduction": (0.06, 0.30),
    "Related work": (None, 0.20),
    "Conclusion": (None, 0.10),
    "Limitations": (None, 0.12),
}
# Introduction + related work + background before the paper's own contribution.
SETUP_MAX = 0.50
# Share of the main-text block covered by figures (images and included graphics);
# accepted ICLR 2026 papers: 5th-95th percentile 2%-20%. Too few is reported
# only when the main text has no figure caption at all.
FIGURE_RANGE = (0.05, 0.35)
# Share of main-text lines that are display equations; accepted ICLR 2026
# papers: median 3%, 95th percentile 9%, none over 15%.
EQUATION_MAX = 0.15

# Section kinds, by heading wording (normalised, numbering stripped).
_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Introduction", re.compile(r"^(introduction|intro|motivation)$")),
    # Combined: held to the introduction + related work + background bound only.
    ("Introduction and related work", re.compile(r"^(introduction|motivation) and (related work|related works|background|prior work)$")),
    ("Related work", re.compile(r"^(related work|related works|prior work|previous work|literature review|related literature|background and related work|related work and background)$")),
    ("Background", re.compile(r"^(background|preliminaries|preliminary|problem setup|problem setting|problem formulation|notation|setup)$")),
    ("Limitations", re.compile(r"^(limitations?|limitations and future work|limitations and broader impacts?|broader impacts?|broader impact statement|societal impact)$")),
    ("Conclusion", re.compile(r"^(conclusions?|concluding remarks|conclusions? and future work|summary|future work)$")),
    # A discussion carries results and their interpretation: no bound.
    ("Discussion", re.compile(r"^(discussion|discussions|discussion and conclusions?|conclusions? and discussion|discussion and limitations|discussion and future work)$")),
)
_END = re.compile(r"^(references|bibliography|appendix|appendices|supplementary material|acknowledge?ments?|reproducibility|ethics|ethical considerations|impact statement)\b")
# Fonts that set mathematics (Computer Modern, Latin Modern, AMS, STIX, txfonts, ...).
_MATH_FONT = re.compile(r"CMMI|CMSY|CMEX|CMBSY|MSAM|MSBM|EUFM|EUSM|RSFS|LMMath|Math|STIXTwoMath|txmi|txsy|txex|pxmi|pxsy|pxex|MTMI|MTSY|rtxmi|ntxmi|zxmi|zxsy", re.IGNORECASE)


def applies(venue: Venue) -> bool:
    """Whether these checks run: conference venues (all AI/ML/NLP/vision ones in paperpush)."""
    return getattr(venue, "venue_type", "") == "conference"


# --- measurement --------------------------------------------------------------


@dataclass
class _Row:
    y0: float
    y1: float
    x0: float
    x1: float
    text: str
    math_share: float
    size: float
    # The row's text split where a wide horizontal gap separates it (columns).
    parts: tuple[str, ...] = ()


@dataclass
class PageFill:
    number: int
    empty_share: float  # share of the text block's height in large gaps
    largest_gap_lines: float
    where: str  # "bottom", "top", "middle"


@dataclass
class Density:
    """What the main-text pages of a PDF look like."""

    main_pages: float  # full pages plus the used share of the last one
    last_page_fill: float
    pages: list[PageFill] = field(default_factory=list)
    figure_share: float = 0.0
    equation_share: float = 0.0
    figure_count: int = 0
    table_count: int = 0
    ref_found: bool = False


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _wrappers(page) -> frozenset[int]:
    """Figure numbers of Form XObjects that cover the whole page.

    Some tools (watermarking, page stamping) wrap each page's content in one
    page-sized XObject; the text inside it is body text, not a figure.
    """
    area = page.width * page.height
    return frozenset(i for i, (x0, y0, x1, y1) in enumerate(page.figure_boxes, start=1) if (x1 - x0) * (y1 - y0) >= 0.85 * area)


def _in_figure(g, wrappers: frozenset[int]) -> bool:
    return g.in_figure and g.figure not in wrappers


def _rows(page, left: float, right: float) -> list[_Row]:
    """Text rows of a page inside the text block's width, figures' text left out."""
    wrappers = _wrappers(page)
    kept = [g for g in page.glyphs if not _in_figure(g, wrappers) and g.text.strip() and not g.angle and left - 3 <= g.x1 and g.x0 <= right + 3]
    # A glyph joins the row whose vertical extent holds its centre line, so
    # small capitals, sub- and superscripts stay on their line.
    kept.sort(key=lambda g: -(g.y0 + g.y1))
    groups: list[list] = []
    lo = hi = 0.0
    for g in kept:
        centre = (g.y0 + g.y1) / 2
        if groups and lo - 0.5 <= centre <= hi + 0.5:
            groups[-1].append(g)
            lo, hi = min(lo, g.y0), max(hi, g.y1)
        else:
            groups.append([g])
            lo, hi = g.y0, g.y1
    rows: list[_Row] = []
    for glyphs in groups:
        glyphs.sort(key=lambda g: g.x0)
        text = "".join(g.text for g in glyphs)
        math = sum(1 for g in glyphs if _MATH_FONT.search(g.font))
        size = statistics.median(g.size for g in glyphs)
        parts, start = [], 0
        for k in range(1, len(glyphs) + 1):
            if k == len(glyphs) or glyphs[k].x0 - glyphs[k - 1].x1 > 1.2 * size:
                parts.append("".join(g.text for g in glyphs[start:k]))
                start = k
        rows.append(_Row(min(g.y0 for g in glyphs), max(g.y1 for g in glyphs), glyphs[0].x0, max(g.x1 for g in glyphs), text, math / len(glyphs), size, tuple(parts)))
    rows.sort(key=lambda r: -r.y1)
    return rows


def _furniture_key(r: _Row) -> tuple[int, str]:
    """A row's height and text, with the digits of a short row (a page number) masked."""
    text = _squash(r.text)
    return (round(r.y0 / 3), re.sub(r"\d+", "#", text) if len(text) <= 40 else text)


def _furniture(pages, rows_by_page: list[list[_Row]]) -> set[tuple[int, str]]:
    """``(rounded y, text)`` of running heads, footers, and page numbers."""
    counts: Counter = Counter()
    for rows in rows_by_page:
        for r in rows:
            counts[_furniture_key(r)] += 1
    threshold = max(3, len(pages) // 2)
    return {k for k, n in counts.items() if n >= threshold}


def _is_furniture(r: _Row, furniture: set) -> bool:
    if _furniture_key(r) in furniture:
        return True
    return bool(re.fullmatch(r"\d{1,3}", _squash(r.text)))


def _merge(intervals: list[tuple[float, float]], slack: float) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for lo, hi in sorted(intervals):
        if out and lo <= out[-1][1] + slack:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]


# What follows the main text and is not counted in a conference's page limit
# (squashed: lower-case, no spaces).
_REF_HEADING = re.compile(r"^(\d+\.?|[a-z]\.?)?(references|bibliography|referencesandnotes|acknowledge?ments?|reproducibility|reproducibilitystatement|ethics|ethicsstatement|ethicalconsiderations|impactstatement|broaderimpactstatement|appendix|appendices)$")


def _is_end_heading(text: str, end_names: set[str]) -> bool:
    squashed = _squash(text)
    if _REF_HEADING.match(squashed):
        return True
    bare = re.sub(r"^(\d+\.?|[a-z]\.)", "", squashed)
    return bool(end_names) and bare in end_names


_CAPTION = re.compile(r"^(figure|fig\.|table)(\d+)[:.|]", re.IGNORECASE)


def measure(pages, end_headings: tuple[str, ...] = (), max_main_pages: int | None = None) -> Density | None:
    """Measure the main text of ``pages`` (from :func:`paperpush.pdf_layout.read_pages`).

    The main text ends at the reference list's heading, or at an earlier one of
    ``end_headings`` (a venue's own, such as ACL's "Limitations"). When no such
    heading is found, only the first ``max_main_pages`` pages are measured, so
    an appendix is not taken for main text.
    """
    end_names = {_squash(h) for h in end_headings}
    if not pages:
        return None
    wrapped = [_wrappers(p) for p in pages]
    sizes = Counter(round(g.size * 2) / 2 for p, w in zip(pages, wrapped) for g in p.glyphs if not _in_figure(g, w) and g.text.strip() and not g.angle)
    if not sizes:
        return None
    body = sizes.most_common(1)[0][0]
    # The text block's width: where body-size rows start and end.
    starts, ends = [], []
    for p, w in zip(pages, wrapped):
        groups: dict[int, list] = {}
        for g in p.glyphs:
            if not _in_figure(g, w) and g.text.strip() and abs(g.size - body) <= 0.6:
                groups.setdefault(round(g.y0), []).append(g)
        for gl in groups.values():
            if len(gl) >= 30:
                starts.append(min(g.x0 for g in gl))
                ends.append(max(g.x1 for g in gl))
    if len(starts) < 20:
        return None
    starts.sort()
    ends.sort()
    left = starts[len(starts) // 20]
    right = ends[-1 - len(ends) // 20]

    rows_by_page = [_rows(p, left, right) for p in pages]
    furniture = _furniture(pages, rows_by_page)
    rows_by_page = [[r for r in rows if not _is_furniture(r, furniture)] for rows in rows_by_page]

    # Where the main text ends: the reference-list heading.
    end_page, end_y, ref_found = len(pages) - 1, None, False
    for i, rows in enumerate(rows_by_page):
        if i == 0:
            continue
        hit = next((r for r in rows if any(_is_end_heading(part, end_names) for part in r.parts or (r.text,))), None)
        if hit is not None:
            end_page, end_y, ref_found = i, hit.y1, True
            break
    if not ref_found and max_main_pages:
        end_page = min(end_page, max_main_pages - 1)

    # The text block's height: the median top and bottom of the full pages.
    tops = [rows[0].y1 for rows in rows_by_page if len(rows) >= 25]
    bottoms = [rows[-1].y0 for rows in rows_by_page if len(rows) >= 25]
    if len(tops) < 2:
        return None
    top, bottom = statistics.median(tops), statistics.median(bottoms)
    height = top - bottom
    if height <= 0:
        return None
    line = body * 1.2
    width = right - left

    result = Density(main_pages=0.0, last_page_fill=1.0, ref_found=ref_found)
    fig_area = block_area = 0.0
    eq_rows = text_rows = 0
    captions: dict[str, set[str]] = {"figure": set(), "table": set()}
    for i in range(end_page + 1):
        page = pages[i]
        floor = bottom
        if i == end_page and end_y is not None:
            floor = end_y + 0.5 * line  # stop above the reference heading
        rows = [r for r in rows_by_page[i] if r.y0 >= floor - 1 and r.y1 <= top + line]
        boxes = [b for n, b in enumerate(page.figure_boxes, start=1) if n not in wrapped[i]]
        boxes = [b for b in boxes + [(im.x0, im.y0, im.x1, im.y1) for im in page.images] + [b for b in page.backgrounds if (b[2] - b[0]) * (b[3] - b[1]) < 0.8 * page.width * page.height]]
        boxes = [(max(b[0], left), max(b[1], floor), min(b[2], right), min(b[3], top)) for b in boxes]
        boxes = [b for b in boxes if b[2] > b[0] and b[3] > b[1]]
        intervals = [(r.y0, r.y1) for r in rows] + [(b[1], b[3]) for b in boxes]
        if i == end_page and end_y is not None and not intervals:
            break  # the references start at the top of this page
        merged = _merge(intervals, 0.0)
        span = top - floor
        # Figures: the union of their boxes' area (rows of figure-sized boxes are merged per strip).
        fig_strips = _merge([(b[1], b[3]) for b in boxes if (b[3] - b[1]) > line], 0.0)
        for lo, hi in fig_strips:
            xs = [b for b in boxes if b[1] < hi and b[3] > lo]
            covered = sum(x1 - x0 for x0, x1 in _merge([(b[0], b[2]) for b in xs], 0.0))
            fig_area += (hi - lo) * min(covered, width)
        block_area += span * width
        for r in rows:
            text_rows += 1
            if r.math_share >= 0.35 and (r.x1 - r.x0) < 0.85 * width:
                eq_rows += 1
            m = _CAPTION.match(_squash(r.text))
            if m:
                captions["table" if m.group(1).lower() == "table" else "figure"].add(m.group(2))
        if not merged:
            continue
        # Empty gaps: between content, and above/below it (except the last page's bottom).
        last = i == end_page or (i == len(pages) - 1)
        gaps: list[tuple[float, str]] = []
        gaps.append((top - merged[-1][1], "top"))
        for (_, a_hi), (b_lo, _) in zip(merged, merged[1:]):
            gaps.append((b_lo - a_hi, "middle"))
        if not last:
            gaps.append((merged[0][0] - floor, "bottom"))
        big = [(g, w) for g, w in gaps if g > GAP_LINES * line]
        empty = sum(g for g, _ in big)
        if big:
            g, where = max(big)
            result.pages.append(PageFill(page.number, empty / height, g / line, where))
        if last:
            used = (top - merged[0][0]) / height
            result.last_page_fill = max(0.0, min(1.0, used))
    pages_used = end_page + 1
    if end_y is not None and end_page < len(pages) and not any(r.y0 >= end_y + 0.5 * line for r in rows_by_page[end_page]):
        # The references start a page: the main text ended on the one before,
        # whose empty bottom is the end of the paper, not a gap.
        pages_used = end_page
        result.last_page_fill = 1.0
        if result.pages and result.pages[-1].number == end_page:
            ended = result.pages.pop()
            result.last_page_fill = max(0.0, 1.0 - ended.empty_share)
    result.main_pages = (pages_used - 1) + result.last_page_fill
    result.figure_share = fig_area / block_area if block_area else 0.0
    result.equation_share = eq_rows / text_rows if text_rows else 0.0
    result.figure_count = len(captions["figure"])
    result.table_count = len(captions["table"])
    return result


# --- section shares -----------------------------------------------------------

# "3 Method", "4. EXPERIMENTS AND RESULTS": a heading is words, not an equation or a sentence.
_NUMBERED = re.compile(r"^\s*(\d{1,2})\.?\s+([A-Z][A-Za-z][A-Za-z0-9 \-:,&'/\u2013\u2014?]{0,70})$")
_SMALL_CAPS = re.compile(r"\b([A-Z]) ([A-Z]{2,})\b")
_TEX_TOP = re.compile(r"\\section\*?\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{[^{}]*\})*)\}")


def _kind(heading: str) -> str:
    name = re.sub(r"^\s*(?:\d+(?:\.\d+)*\.?|[A-Z]\.)\s+", "", heading).strip().lower()
    name = re.sub(r"[^a-z ]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    for kind, pattern in _KINDS:
        if pattern.match(name):
            return kind
    if _END.match(name):
        return "END"
    return "Other"


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z][A-Za-z'-]*", text))


def section_shares_from_text(text: str) -> dict[str, int] | None:
    """Words in each kind of top-level section of extracted text (PDF, Word).

    Top-level headings are the numbered ones ("3 Method"), taken only when
    their numbers run 1, 2, 3, ... -- which keeps numbered list items and table
    rows out -- plus the standard unnumbered headings.
    """
    lines = text.splitlines()
    marks: list[tuple[int, str]] = []
    expected = 1
    for i, line in enumerate(lines):
        # Small capitals come out of a PDF as "I NTRODUCTION"; rejoin them.
        s = _SMALL_CAPS.sub(r"\1\2", line.strip())
        m = _NUMBERED.match(s)
        if m and int(m.group(1)) == expected and len(s.split()) <= 10 and not s.endswith("."):
            marks.append((i, s))
            expected += 1
            continue
        # An unnumbered heading ("Limitations and Future Work", "Ethics
        # Statement", "References"), capitalised and on its own line.
        # Only the kinds venues set unnumbered: a lone "Summary" or "Discussion"
        # line may be a label in a figure or table.
        if s[:1].isupper() and len(s.split()) <= 6 and not s.endswith(".") and (s.lower() == "abstract" or _kind(s) in ("Limitations", "END")):
            marks.append((i, s))
    if expected < 4:
        return None  # not a numbered-section paper we can split reliably
    counts: Counter = Counter()
    for j, (i, heading) in enumerate(marks):
        kind = _kind(heading)
        if kind == "END" or heading.strip().lower() == "abstract":
            if kind == "END":
                break
            continue
        stop = marks[j + 1][0] if j + 1 < len(marks) else len(lines)
        counts[kind] += _word_count("\n".join(lines[i + 1 : stop]))
    return dict(counts)


def section_shares_from_tex(source: str) -> dict[str, int] | None:
    """Words in each kind of ``\\section`` of LaTeX source, up to the bibliography or appendix."""
    from .manuscript import _tex_to_text

    cut = len(source)
    for pattern in (r"\\appendix\b", r"\\bibliography\b", r"\\begin\s*\{thebibliography\}", r"\\printbibliography\b"):
        m = re.search(pattern, source)
        if m:
            cut = min(cut, m.start())
    body = source[:cut]
    marks = list(_TEX_TOP.finditer(body))
    if len(marks) < 3:
        return None
    counts: Counter = Counter()
    for j, m in enumerate(marks):
        kind = _kind(_tex_to_text(m.group(1)))
        if kind == "END":
            break
        stop = marks[j + 1].start() if j + 1 < len(marks) else len(body)
        counts[kind] += _word_count(_tex_to_text(body[m.end() : stop]))
    return dict(counts)


def section_words(path: Path) -> dict[str, int] | None:
    from .manuscript import _raw_tex_source, manuscript_text

    if path.suffix.lower() in (".tex", ".zip"):
        source = _raw_tex_source(path)
        if source is not None:
            return section_shares_from_tex(source)
    text = manuscript_text(path)
    if text is None:
        return None
    return section_shares_from_text(text)


# --- the checks ---------------------------------------------------------------


def _pct(x: float) -> str:
    return f"{round(100 * x)}%"


def _density_for(path: Path, end_headings: tuple[str, ...] = (), max_main_pages: int | None = None) -> Density:
    from . import pdf_layout

    if not pdf_layout.available():
        raise Unchecked(f"measuring {path.name} needs pdfminer.six ({pdf_layout.INSTALL_HINT})")
    doc = document.get(path)
    if doc.is_docx:
        raise Unchecked(f"{path.name} is a Word file; page fill is measured on a PDF or LaTeX source")
    if doc.pdf is None:
        raise Unchecked(f"could not compile {path.name} to measure it (install latexmk or pdflatex)")
    measured = measure(doc.glyph_pages, end_headings, max_main_pages)
    if measured is None:
        raise Unchecked(f"too little text in {path.name} to measure how full its pages are")
    return measured


def _manuscript(venue: Venue, values: dict[str, str]) -> tuple[Field, Path]:
    from .requirements_check import manuscript_files

    files = manuscript_files(venue, values)
    if not files:
        raise NotApplicable("no manuscript file to measure")
    return files[0]


def _page_rules(venue: Venue, values: dict[str, str]) -> tuple[int | None, tuple[str, ...]]:
    """The venue's main-text page limit and the headings its main text ends at."""
    reqs = get_requirements(venue.slug)
    if reqs is None:
        return None, ()
    rules = resolve(reqs, values).manuscript
    return rules.max_pages_before_refs or rules.max_pages, tuple(rules.main_text_end_headings or ())


def density_check(venue: Venue, values: dict[str, str]) -> Outcome:
    if not applies(venue):
        raise NotApplicable("only checked for AI conference venues")
    field_, path = _manuscript(venue, values)
    limit, endings = _page_rules(venue, values)
    d = _density_for(path, endings, limit)
    issues: list[Issue] = []
    notes = [f"main text {d.main_pages:.1f} pages" + (f" of {limit}" if limit else "")]
    sparse = [p for p in d.pages if p.empty_share >= PAGE_EMPTY_SHARE]
    if sparse:
        listed = ", ".join(f"p. {p.number} ({_pct(p.empty_share)} empty, largest gap ~{round(p.largest_gap_lines)} lines at the {p.where})" for p in sparse[:6])
        issues.append(Issue(SUGGESTION, field_.id, f"{path.name}: {len(sparse)} main-text page(s) have large empty areas: {listed}. Reviewers at AI conferences read pages with large blank areas as an unpolished paper; usually a float pushed to the next page leaves the gap. Try moving the figure or table earlier, [t] or [tbp] placement, resizing it, or putting two side by side."))
    if limit and d.ref_found and limit - d.main_pages >= UNUSED_PAGES:
        issues.append(Issue(SUGGESTION, field_.id, f"{path.name}: the main text fills about {d.main_pages:.1f} of the {limit} pages allowed. Most accepted papers at these venues use the full limit, and a paper that stops short can read as less complete; consider moving key results, ablations, or a figure up from the appendix."))
    if not d.ref_found:
        notes.append("no reference-list heading found, so the end of the main text is a guess" + (f" (the first {limit} pages were measured)" if limit else ""))
    return Outcome(issues, "; ".join(notes))


def balance_check(venue: Venue, values: dict[str, str]) -> Outcome:
    if not applies(venue):
        raise NotApplicable("only checked for AI conference venues")
    field_, path = _manuscript(venue, values)
    issues: list[Issue] = []
    notes: list[str] = []
    words = section_words(path)
    if words:
        total = sum(words.values())
        if total >= 1500:
            share = {k: v / total for k, v in words.items()}
            notes.append(", ".join(f"{k} {_pct(v)}" for k, v in sorted(share.items(), key=lambda kv: -kv[1])) + " of main-text words")
            for kind, (low, high) in SECTION_RANGES.items():
                value = share.get(kind)
                if value is None:
                    continue
                # Reported only when the rounded share is past the bound, so the message never reads "12%, under 12%".
                if high is not None and round(100 * value) > round(100 * high):
                    issues.append(Issue(SUGGESTION, field_.id, f"{path.name}: the {kind} section takes {_pct(value)} of the main text; accepted papers at these venues usually keep it to {_pct(high)} or less, leaving the space for the method and results."))
                elif low is not None and round(100 * value) < round(100 * low):
                    issues.append(Issue(SUGGESTION, field_.id, f"{path.name}: the {kind} section takes only {_pct(value)} of the main text; accepted papers at these venues usually give it at least {_pct(low)} to motivate the problem and state the contributions."))
            setup = sum(share.get(k, 0.0) for k in ("Introduction", "Introduction and related work", "Related work", "Background"))
            if setup > SETUP_MAX:
                issues.append(Issue(SUGGESTION, field_.id, f"{path.name}: introduction, related work, and background together take {_pct(setup)} of the main text, so the paper's own contribution starts late; consider moving some related work to the appendix."))
            if "Limitations" not in share:
                from .manuscript import manuscript_text

                text = manuscript_text(path) or ""
                if not re.search(r"\blimitations?\b", text, re.IGNORECASE):
                    issues.append(Issue(SUGGESTION, field_.id, f"{path.name}: the paper never discusses its limitations; reviewers at AI conferences expect at least a paragraph on them (several venues require one)."))
    else:
        notes.append("section shares not measured (no numbered top-level sections found)")
    try:
        limit, endings = _page_rules(venue, values)
        d = _density_for(path, endings, limit)
    except Unchecked as exc:
        if not words:
            raise
        notes.append(str(exc))
        d = None
    if d is not None:
        notes.append(f"figures cover {_pct(d.figure_share)} of the main-text area; display equations are {_pct(d.equation_share)} of its lines")
        low, high = FIGURE_RANGE
        if d.figure_share < low and d.figure_count == 0:
            issues.append(Issue(SUGGESTION, field_.id, f"{path.name}: the main text has no figures. Accepted papers at AI conferences nearly always have at least an overview or results figure, which reviewers use to grasp the paper quickly."))
        elif d.figure_share > high:
            issues.append(Issue(SUGGESTION, field_.id, f"{path.name}: figures cover about {_pct(d.figure_share)} of the main-text area; with so little text, consider shrinking them or moving some to the appendix."))
        if d.equation_share > EQUATION_MAX:
            issues.append(Issue(SUGGESTION, field_.id, f"{path.name}: about {_pct(d.equation_share)} of the main-text lines are display equations, which is dense for these venues; consider moving derivations to the appendix and keeping the main results and intuition in the text (fine to leave for a theory paper)."))
    if not words and d is None:
        raise Unchecked("could not measure the manuscript's sections or layout")
    return Outcome(issues, "; ".join(notes))


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    checks.run("layout.density", "Main-text pages are full and use the page limit (AI conferences)", LAYOUT, density_check, venue, values)
    checks.run("layout.section_balance", "Space split between sections, figures, and equations is typical (AI conferences)", LAYOUT, balance_check, venue, values)
