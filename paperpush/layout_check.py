"""Check the manuscript's page layout against the author guidelines.

Venues that format the manuscript themselves still ask for a particular page:
a page size, margins, a body font and size, double spacing, line numbers,
page numbers, a template or LaTeX class, an English spelling variant. Each is
a field of ``manuscript`` in ``manuscript_requirements.json``; this module
measures the manuscript three ways, depending on what was uploaded:

* **PDF** (or LaTeX compiled to one): the positioned glyphs from
  :mod:`paperpush.pdf_layout`. The body text is the most common glyph size
  outside figures; text lines are those glyphs grouped by baseline, with page
  numbers, running heads, and margin line numbers split off; margins are the
  distances from the page edges to the text block; line spacing is the most
  common distance between consecutive baselines.
* **Word** (``.docx``): the page setup, styles, and run properties in the
  file's XML, resolved through the style chain and the theme fonts.
* **LaTeX source** that could not be compiled: the document-class options and
  the packages and commands that set the layout.

A departure is a WARNING: the measurements are good evidence, not proof.
"""

from __future__ import annotations

import logging
import re
import statistics
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import document
from .database import Field, Venue
from .requirements import ManuscriptRequirements, get_requirements, resolve
from .validate import WARNING, Checks, Issue, NotApplicable, Outcome, Unchecked

logger = logging.getLogger(__name__)

LAYOUT = "Layout"
PT_PER_MM = 72 / 25.4
# How far (points) a measured margin may fall short before it is reported.
MARGIN_TOLERANCE_PT = 3.0
# Body-text size tolerance, in points.
FONT_SIZE_TOLERANCE_PT = 0.5
# Baseline-to-baseline distance, as a multiple of the font size, from which
# lines count as double spaced (LaTeX's \doublespacing gives ~1.95, Word's ~2.3).
DOUBLE_SPACING_RATIO = 1.8
# Word's single line height, as a multiple of the font size, for the fonts tall
# enough that its 1.5 spacing would otherwise pass that ratio (Times and Arial are 1.15).
TALL_LINE_HEIGHT = {"Calibri": 1.22, "Verdana": 1.215}
PAGE_SIZES = {"us letter": (612.0, 792.0), "letter": (612.0, 792.0), "a4": (595.3, 841.9), "legal": (612.0, 1008.0)}
# A page needs this many body lines before it counts toward margins and spacing.
MIN_BODY_LINES = 12

# --- fonts ------------------------------------------------------------------------

# (pattern on the squashed lower-case font name, family, generic class). First match wins.
_FONT_FAMILIES: list[tuple[str, str, str]] = [
    (r"timesnewroman|timesnew|timesnr", "Times New Roman", "serif"),
    (r"^times|nimbusrom|nimbusroman|texgyretermes|termes|liberationserif|tinos|^ptm|stixtwotext|^stix(?!.*math)", "Times", "serif"),
    (r"arial|arimo|liberationsans", "Arial", "sans-serif"),
    (r"helvetica|nimbussan|nimbussans|texgyreheros|^heros|^phv", "Helvetica", "sans-serif"),
    (r"calibri|carlito", "Calibri", "sans-serif"),
    (r"cambria|caladea", "Cambria", "serif"),
    (r"myriad", "Myriad", "sans-serif"),
    (r"roboto", "Roboto", "sans-serif"),
    (r"verdana", "Verdana", "sans-serif"),
    (r"courier|nimbusmon|^cmtt|lmmono|consolas|menlo|^pcr", "Courier", "monospace"),
    (r"symbol|^cmsy|^cmmi|^cmex|^msam|^msbm|lmmath|stixmath|^cmr?math|mathematicalpi|^rsfs|^eufm|^txsy|^txmi|^pxsy|^pxmi|^ntxmi|^ntxsy", "Symbol", "symbol"),
    (r"^cmss|lmsans", "Computer Modern Sans", "sans-serif"),
    (r"^cm[a-z]*\d|lmroman|latinmodern|computermodern|^sfrm|^lmr", "Computer Modern", "serif"),
    (r"palatino|^pplr|texgyrepagella|bookantiqua|urwpalladio", "Palatino", "serif"),
    (r"georgia", "Georgia", "serif"),
    (r"garamond", "Garamond", "serif"),
    (r"dejavusans|opensans|sourcesans|^lato|segoe|frutiger|gillsans|futura|avenir|^inter|notosans|ubuntu|tahoma|trebuchet|franklingothic|^sfpro|helveticaneue", "", "sans-serif"),
    (r"dejavuserif|sourceserif|notoserif|minion|charter|bookman|baskerville|caslon|utopia|lucidabright|^libertine", "", "serif"),
]
# Families the guidelines treat as one (metric clones and near-identical faces).
_EQUIVALENT = [{"Times", "Times New Roman"}, {"Arial", "Helvetica"}]


def font_family(name: str) -> tuple[str, str]:
    """``(family, generic class)`` for a PDF/Word font name ("ABCDEF+TimesNewRomanPS-BoldMT")."""
    base = name.split("+", 1)[-1]
    squashed = re.sub(r"[^a-z0-9]", "", base.lower())
    for pattern, family, generic in _FONT_FAMILIES:
        if re.search(pattern, squashed):
            if family:
                return family, generic
            break
    else:
        generic = ""
    # An unlisted face: its name before any style suffix.
    family = re.split(r"[-,]", base)[0]
    family = re.sub(r"(?:PS)?MT$|PSMT$|(?<=[a-z])(?:Regular|Roman|Bold|Italic|Light|Medium)$", "", family) or base
    if not generic:
        generic = "sans-serif" if re.search(r"sans|gothic|grotesk", squashed) else "monospace" if "mono" in squashed else "serif" if re.search(r"serif|roman", squashed) else ""
    return family, generic


@dataclass(frozen=True)
class FontRule:
    """The fonts a guideline allows, read from its wording."""

    families: frozenset[str]
    generics: frozenset[str]
    only: bool  # "Times and Symbol fonts only": every text font must be allowed
    text: str

    def allows(self, family: str, generic: str) -> bool:
        if family in self.families or any(family in group and group & self.families for group in _EQUIVALENT):
            return True
        if family == "Symbol" or generic == "symbol":
            return True  # mathematical symbol fonts are never the "body font"
        return bool(generic and generic in self.generics)


def parse_font_rule(text: str) -> FontRule:
    lower = text.lower()
    squashed = re.sub(r"[^a-z0-9]", "", lower)
    families = set()
    for pattern, family, generic in _FONT_FAMILIES:
        if family and re.search(pattern.replace("^", ""), squashed):
            families.add(family)
    generics = set()
    if re.search(r"sans[- ]?serif|\bsans\b", lower):
        generics.add("sans-serif")
    if re.search(r"(?<!sans[- ])(?<!sans)\bserif\b", lower):
        generics.add("serif")
    # "Arial or a similar typeface", "e.g. Helvetica", "Myriad preferred":
    # the named faces stand for their class.
    if re.search(r"similar|e\.g\.|such as|preferred|like\b|or equivalent", lower):
        for pattern, family, generic in _FONT_FAMILIES:
            if family in families and generic not in ("symbol",):
                generics.add(generic)
    return FontRule(frozenset(families), frozenset(generics), bool(re.search(r"\bonly\b", lower)), text)


def font_problems(weights: Counter, rule: FontRule, what: str) -> list[str]:
    """Sentences for each font in ``weights`` (font name -> glyph count) the rule does not allow."""
    if not weights or (not rule.families and not rule.generics):
        return []
    by_family: Counter = Counter()
    generic_of: dict[str, str] = {}
    for name, count in weights.items():
        family, generic = font_family(name)
        by_family[family] += count
        generic_of[family] = generic
    total = sum(by_family.values())
    main, _ = by_family.most_common(1)[0]
    problems = []
    if not rule.allows(main, generic_of[main]):
        problems.append(f"{what} is set in {main}; the author guidelines ask for {rule.text}")
    if rule.only:
        for family, count in by_family.most_common():
            if family != main and count / total >= 0.02 and not rule.allows(family, generic_of[family]):
                problems.append(f"{what} also uses {family} ({100 * count / total:.0f}% of the text); the author guidelines allow {rule.text}")
    return problems


def unembedded_fonts(path: Path) -> list[str]:
    """Names of the fonts a PDF uses without embedding them (empty if unreadable)."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
    except Exception:  # noqa: BLE001 -- unreadable PDF: nothing to report
        return []
    missing: set[str] = set()

    def visit(resources) -> None:
        try:
            fonts = resources.get("/Font") if resources else None
            fonts = fonts.get_object() if fonts is not None else {}
            for ref in fonts.values():
                font = ref.get_object()
                if font.get("/Subtype") == "/Type3":
                    continue
                descriptor = font.get("/FontDescriptor")
                if descriptor is None and font.get("/DescendantFonts"):
                    descriptor = font["/DescendantFonts"][0].get_object().get("/FontDescriptor")
                descriptor = descriptor.get_object() if descriptor is not None else None
                if descriptor is None or not any(k in descriptor for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                    missing.add(str(font.get("/BaseFont", "?")).lstrip("/"))
            xobjects = resources.get("/XObject") if resources else None
            for ref in (xobjects.get_object().values() if xobjects is not None else []):
                obj = ref.get_object()
                if obj.get("/Subtype") == "/Form":
                    visit(obj.get("/Resources"))
        except Exception:  # noqa: BLE001 -- a malformed resource dictionary
            logger.debug("could not read the fonts of %s", path, exc_info=True)

    for page in reader.pages[:40]:
        visit(page.get("/Resources"))
    return sorted(missing)


# --- PDF measurement --------------------------------------------------------------

_PAGE_NUMBER = re.compile(r"^[-\u2013\u2014]?\s*(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\s*[-\u2013\u2014]?$", re.IGNORECASE)


@dataclass(frozen=True)
class PdfLayout:
    pages: int
    page_width: float
    page_height: float
    body_font: float
    body_fonts: Counter
    all_fonts: Counter
    left: float | None
    right: float | None
    top: float | None
    bottom: float | None
    baseline_skip: float | None
    line_number_pages: int
    page_number_pages: int
    full_pages: int


def _segments(glyphs, gap: float):
    """Split one baseline's glyphs (sorted by x) into runs separated by wide gaps."""
    runs = [[glyphs[0]]]
    for g in glyphs[1:]:
        if g.x0 - runs[-1][-1].x1 > gap:
            runs.append([g])
        else:
            runs[-1].append(g)
    return runs


def _strip_close_line_numbers(rows: dict[float, list], body: float) -> int:
    """Take numbers set just left of the text off their rows; returns how many.

    Word puts line numbers about a quarter inch from the text, at the text's
    size and on its baseline, too close to be a separate segment. They are told
    from a table's index column by being a column that all the text starts right of.
    """
    leading: dict[float, int] = {}
    for y, glyphs in rows.items():
        n = 0
        while n < min(5, len(glyphs)) and glyphs[n].text.isdigit():
            n += 1
        if 1 <= n <= 4 and n < len(glyphs) and glyphs[n].x0 - glyphs[n - 1].x1 > 0.8 * body:
            leading[y] = n
    if len(leading) < 5:
        return 0
    column, _ = Counter(round(rows[y][n - 1].x1) for y, n in leading.items()).most_common(1)[0]
    numbered = {y: n for y, n in leading.items() if abs(rows[y][n - 1].x1 - column) <= 1.5}
    if len(numbered) < 5 or any(glyphs[0].x0 < column for y, glyphs in rows.items() if y not in numbered):
        return 0
    for y, n in numbered.items():
        del rows[y][:n]
    return len(numbered)


def measure_pdf(pages) -> PdfLayout | None:
    """Measure body text, margins, spacing, and line/page numbering from glyph pages."""
    from .template_check import _repeated_rows, _squash

    text_glyphs = [g for p in pages for g in p.glyphs if not g.in_figure and g.text.strip()]
    if len(text_glyphs) < 200:
        return None
    sizes = Counter(round(g.size, 1) for g in text_glyphs)
    body = sizes.most_common(1)[0][0]
    body_fonts: Counter = Counter(g.font for g in text_glyphs if abs(g.size - body) <= 0.3)
    all_fonts: Counter = Counter(g.font for g in text_glyphs)
    repeated = _repeated_rows(pages, body)

    lefts: list[float] = []
    rights: list[float] = []
    tops: list[float] = []
    bottoms: list[float] = []
    gaps: list[float] = []
    line_number_pages = 0
    page_number_pages = 0
    full_pages = 0
    for page in pages:
        rows: dict[float, list] = {}
        for g in page.glyphs:
            if g.in_figure or not g.text.strip() or g.baseline is not None:
                continue
            rows.setdefault(round(g.y0 * 2) / 2, []).append(g)
        for glyphs in rows.values():
            glyphs.sort(key=lambda g: g.x0)
        body_lines: list[tuple[float, float, float]] = []  # (y, x0, x1)
        margin_digits = _strip_close_line_numbers(rows, body)
        has_page_number = False
        segmented = {y: _segments(glyphs, 2.5 * body) for y, glyphs in rows.items()}
        seg_text = lambda seg: "".join(g.text for g in seg).strip()  # noqa: E731
        # LaTeX sets its smaller line numbers a point off the text's baseline, so each is a row of its own.
        edge_digits = [seg for segs in segmented.values() for seg in {id(s): s for s in (segs[0], segs[-1])}.values() if seg_text(seg).isdigit() and len(seg_text(seg)) <= 4]
        # Line numbers form a column; a page number in a running head stands alone.
        in_column = lambda seg: sum(abs(o[-1].x1 - seg[-1].x1) < 2 or abs(o[0].x0 - seg[0].x0) < 2 for o in edge_digits) >= 3  # noqa: E731
        ys = sorted(segmented)
        band = page.height * 0.1
        for y, segments in segmented.items():
            # A head or foot: near the page's edge, or its outermost row, set off from the text.
            running = y - page.y0 < band or page.y0 + page.height - y < band or (len(ys) > 1 and ((y == ys[0] and ys[1] - y > 1.5 * body) or (y == ys[-1] and y - ys[-2] > 1.5 * body)))
            if running and len(segments) > 1 and any(_PAGE_NUMBER.match(seg_text(seg)) and not in_column(seg) for seg in (segments[0], segments[-1])):
                has_page_number = True
                continue
            kept = []
            for seg in segments:
                text = "".join(g.text for g in seg).strip()
                if text.isdigit() and len(text) <= 4 and (seg is segments[0] or seg is segments[-1]) and len(segments) > 1:
                    margin_digits += 1  # a line number beside a text line
                    continue
                kept.append(seg)
            if not kept:
                continue
            row_text = " ".join("".join(g.text for g in seg) for seg in kept).strip()
            # A digits-only row in the line-number column is a line number, even near the page's edge.
            if running and _PAGE_NUMBER.match(re.sub(r"\s+", " ", row_text)) and not (len(kept) == 1 and row_text.isdigit() and in_column(kept[0])):
                has_page_number = True
                continue
            if len(kept) == 1 and row_text.isdigit() and len(row_text) <= 4:
                margin_digits += 1  # a line number on an otherwise empty line
                continue
            body_glyphs = [g for seg in kept for g in seg if abs(g.size - body) <= 0.3]
            if len(body_glyphs) < 3:
                continue
            if (round(body_glyphs[0].y0), _squash("".join(g.text for g in body_glyphs))) in repeated:
                continue  # running head / footer
            body_lines.append((y, min(g.x0 for g in body_glyphs), max(g.x1 for g in body_glyphs)))
        if margin_digits >= 5:
            line_number_pages += 1
        if has_page_number:
            page_number_pages += 1
        if len(body_lines) < MIN_BODY_LINES:
            continue
        full_pages += 1
        body_lines.sort(key=lambda line: -line[0])
        tops.append(page.y0 + page.height - (body_lines[0][0] + 0.7 * body))
        bottoms.append(body_lines[-1][0] - page.y0)
        lefts.extend(x0 - page.x0 for _, x0, _ in body_lines)
        rights.extend(page.x0 + page.width - x1 for _, _, x1 in body_lines)
        for (y_above, _, _), (y_below, _, _) in zip(body_lines, body_lines[1:]):
            gap = y_above - y_below
            if 0.9 * body <= gap <= 3.2 * body:
                gaps.append(round(gap * 2) / 2)

    def low(values: list[float], q: float) -> float | None:
        if not values:
            return None
        values = sorted(values)
        return values[min(len(values) - 1, int(q * len(values)))]

    first = pages[0]
    return PdfLayout(
        pages=len(pages),
        page_width=first.width,
        page_height=first.height,
        body_font=body,
        body_fonts=body_fonts,
        all_fonts=all_fonts,
        left=low(lefts, 0.05),
        right=low(rights, 0.05),
        top=statistics.median(tops) if tops else None,
        bottom=statistics.median(bottoms) if bottoms else None,
        baseline_skip=Counter(gaps).most_common(1)[0][0] if gaps else None,
        line_number_pages=line_number_pages,
        page_number_pages=page_number_pages,
        full_pages=full_pages,
    )


# --- Word measurement ---------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


@dataclass(frozen=True)
class DocxLayout:
    page_width: float | None
    page_height: float | None
    margins: dict[str, float]  # points
    fonts: Counter  # font name -> characters
    sizes: Counter  # points -> characters
    spacing: Counter  # line-spacing multiple -> characters (None for exact/atLeast)
    line_numbers: bool
    page_numbers: bool
    template: str


def _twips(value: str | None) -> float | None:
    try:
        return float(value) / 20.0 if value is not None else None
    except ValueError:
        return None


def safe_xml(xml: str | bytes | None):
    """Parse XML from an uploaded file, or None.

    A document that declares entities (and with them, entity expansion) is
    refused rather than parsed. A bare ``<!DOCTYPE svg ...>``, which matplotlib
    and others write, is harmless: the standard library parser does not fetch
    external DTDs or entities.
    """
    if not xml:
        return None
    data = xml.encode("utf-8") if isinstance(xml, str) else xml
    if b"<!ENTITY" in data.upper():
        return None
    try:
        return ET.fromstring(data)  # nosec B314 -- entities refused above; no external entities in stdlib
    except ET.ParseError:
        return None


def measure_docx(doc: document.Document) -> DocxLayout | None:
    root = safe_xml(doc.docx_part("word/document.xml"))
    if root is None:
        return None
    theme = safe_xml(doc.docx_part("word/theme/theme1.xml"))
    theme_fonts = {}
    if theme is not None:
        for kind in ("minor", "major"):
            latin = theme.find(f".//{_A}{kind}Font/{_A}latin")
            if latin is not None:
                theme_fonts[kind] = latin.get("typeface", "")

    def font_of(rpr) -> str | None:
        if rpr is None:
            return None
        fonts = rpr.find(f"{_W}rFonts")
        if fonts is None:
            return None
        # A theme font overrides a named one on the same element (ECMA-376 17.3.2.26).
        themed = fonts.get(f"{_W}asciiTheme") or fonts.get(f"{_W}hAnsiTheme")
        if themed:
            return theme_fonts.get("major" if themed.startswith("major") else "minor")
        return fonts.get(f"{_W}ascii") or fonts.get(f"{_W}hAnsi") or None

    def size_of(rpr) -> float | None:
        if rpr is None:
            return None
        sz = rpr.find(f"{_W}sz")
        try:
            return float(sz.get(f"{_W}val")) / 2 if sz is not None else None
        except (TypeError, ValueError):
            return None

    def spacing_of(ppr) -> float | None | str:
        if ppr is None:
            return None
        sp = ppr.find(f"{_W}spacing")
        if sp is None or sp.get(f"{_W}line") is None:
            return None
        rule = sp.get(f"{_W}lineRule", "auto")
        try:
            line = float(sp.get(f"{_W}line"))
        except ValueError:
            return None
        return round(line / 240.0, 2) if rule == "auto" else "exact"

    styles_root = safe_xml(doc.docx_part("word/styles.xml"))
    # Without a styles part the defaults are the application's, which the file does not record.
    default = {"font": None, "size": None, "spacing": None}
    styles: dict[str, dict] = {}
    default_style = "Normal"
    if styles_root is not None:
        rpr = styles_root.find(f"{_W}docDefaults/{_W}rPrDefault/{_W}rPr")
        default["font"] = font_of(rpr) or theme_fonts.get("minor")
        default["size"] = size_of(rpr) or 10.0  # Word's built-in default when docDefaults omit it
        ppr = styles_root.find(f"{_W}docDefaults/{_W}pPrDefault/{_W}pPr")
        default["spacing"] = spacing_of(ppr) or 1.0
        for style in styles_root.findall(f"{_W}style"):
            sid = style.get(f"{_W}styleId", "")
            based = style.find(f"{_W}basedOn")
            styles[sid] = {
                "based": based.get(f"{_W}val") if based is not None else None,
                "font": font_of(style.find(f"{_W}rPr")),
                "size": size_of(style.find(f"{_W}rPr")),
                "spacing": spacing_of(style.find(f"{_W}pPr")),
            }
            if style.get(f"{_W}type") == "paragraph" and style.get(f"{_W}default") in ("1", "true"):
                default_style = sid

    def from_style(sid: str | None, key: str):
        seen = set()
        while sid and sid in styles and sid not in seen:
            seen.add(sid)
            if styles[sid][key] is not None:
                return styles[sid][key]
            sid = styles[sid]["based"]
        return None

    def resolved(sid: str | None, key: str):
        found = from_style(sid, key)
        return default[key] if found is None else found

    fonts: Counter = Counter()
    sizes: Counter = Counter()
    spacing: Counter = Counter()
    body = root.find(f"{_W}body")
    for para in (body.iter(f"{_W}p") if body is not None else []):
        ppr = para.find(f"{_W}pPr")
        pstyle = ppr.find(f"{_W}pStyle").get(f"{_W}val") if ppr is not None and ppr.find(f"{_W}pStyle") is not None else default_style
        line = spacing_of(ppr) or resolved(pstyle, "spacing")
        for run in para.iter(f"{_W}r"):
            text = "".join(t.text or "" for t in run.iter(f"{_W}t"))
            if not text.strip():
                continue
            rpr = run.find(f"{_W}rPr")
            rstyle = rpr.find(f"{_W}rStyle") if rpr is not None else None
            char_style = rstyle.get(f"{_W}val") if rstyle is not None else None
            font = font_of(rpr) or from_style(char_style, "font") or resolved(pstyle, "font")
            size = size_of(rpr) or from_style(char_style, "size") or resolved(pstyle, "size")
            if font:
                fonts[font] += len(text)
            if size:
                sizes[size] += len(text)
            if line:
                spacing[line] += len(text)

    sect = root.find(f".//{_W}body/{_W}sectPr")
    if sect is None:
        found = root.findall(f".//{_W}sectPr")
        sect = found[-1] if found else None
    width = height = None
    margins: dict[str, float] = {}
    line_numbers = False
    if sect is not None:
        size = sect.find(f"{_W}pgSz")
        if size is not None:
            width, height = _twips(size.get(f"{_W}w")), _twips(size.get(f"{_W}h"))
        mar = sect.find(f"{_W}pgMar")
        if mar is not None:
            for side in ("top", "bottom", "left", "right"):
                value = _twips(mar.get(f"{_W}{side}"))
                if value is not None:
                    margins[side] = abs(value)
        line_numbers = sect.find(f"{_W}lnNumType") is not None
    page_numbers = any(re.search(r"\bPAGE\b", xml) for name, xml in doc._docx_parts.items() if re.match(r"word/(?:footer|header)\d*\.xml$", name))
    template = ""
    settings = doc.docx_part("word/settings.xml") or ""
    match = re.search(r'<w:attachedTemplate r:id="([^"]+)"', settings)
    if match:
        rels = doc.docx_part("word/_rels/settings.xml.rels") or ""
        target = re.search(rf'Id="{re.escape(match.group(1))}"[^>]*Target="([^"]+)"', rels) or re.search(rf'Target="([^"]+)"[^>]*Id="{re.escape(match.group(1))}"', rels)
        template = target.group(1).rsplit("/", 1)[-1] if target else ""
    return DocxLayout(width, height, margins, fonts, sizes, spacing, line_numbers, page_numbers, template)


# --- LaTeX source -------------------------------------------------------------------

_DOCCLASS = re.compile(r"\\documentclass\s*(?:\[([^\]]*)\])?\s*\{([^}]*)\}")
_PACKAGE = re.compile(r"\\usepackage\s*(?:\[([^\]]*)\])?\s*\{([^}]*)\}")
_DIM = re.compile(r"(-?\d*\.?\d+)\s*(in|cm|mm|pt|bp)")
_DIM_PT = {"in": 72.0, "cm": 72 / 2.54, "mm": 72 / 25.4, "pt": 72 / 72.27, "bp": 1.0}


@dataclass(frozen=True)
class TexLayout:
    classes: list[str]
    packages: list[str]
    font_size: float | None
    font: str | None
    margins: dict[str, float]
    double_spacing: bool | None
    line_numbers: bool
    page_numbers: bool
    paper: str | None


def _dims(text: str) -> float | None:
    match = _DIM.search(text)
    return float(match.group(1)) * _DIM_PT[match.group(2)] if match else None


def measure_tex(source: str) -> TexLayout:
    classes, options = [], []
    for m in _DOCCLASS.finditer(source):
        classes.append(m.group(2).strip())
        options += [o.strip() for o in (m.group(1) or "").split(",")]
    packages: list[str] = []
    package_options: dict[str, str] = {}
    for m in _PACKAGE.finditer(source):
        for name in m.group(2).split(","):
            packages.append(name.strip())
            package_options[name.strip()] = m.group(1) or ""
    size = next((float(o[:-2]) for o in options if re.fullmatch(r"\d+(?:\.\d+)?pt", o)), None)
    if size is None and any(c in ("article", "report", "book", "amsart") for c in classes):
        size = 10.0
    font = None
    if {"times", "mathptmx", "newtxtext", "txfonts", "ptm", "tgtermes", "mathptm"} & set(packages):
        font = "Times"
    elif re.search(r"\\renewcommand\s*\{?\s*\\rmdefault\s*\}?\s*\{\s*ptm\s*\}", source):
        font = "Times"
    elif {"helvet", "arial", "uarial", "tgheros"} & set(packages) and re.search(r"\\familydefault\s*\}?\s*\{\s*\\sfdefault", source):  # these packages only set the sans family
        font = "Helvetica" if "helvet" in packages or "tgheros" in packages else "Arial"
    elif {"mathpazo", "palatino", "newpxtext", "pxfonts", "tgpagella"} & set(packages):
        font = "Palatino"
    elif m := re.search(r"\\setmainfont\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", source):
        font = m.group(1).strip()
    elif any(c in ("article", "report", "book", "amsart") for c in classes) and not {"fontspec", "libertine", "charter", "XCharter", "fourier", "bookman", "newcent", "kpfonts", "cmbright", "sourcesanspro", "sourceserifpro"} & set(packages):
        font = "Computer Modern"
    margins: dict[str, float] = {}
    geometry = package_options.get("geometry", "") + " " + " ".join(m.group(1) for m in re.finditer(r"\\(?:new)?geometry\s*\{([^}]*)\}", source))
    for key, value in re.findall(r"(margin|left|right|top|bottom|lmargin|rmargin|tmargin|bmargin|hmargin|vmargin)\s*=\s*\{?([^,}]+)", geometry):
        pts = _dims(value)
        if pts is None:
            continue
        sides = {"margin": ("left", "right", "top", "bottom"), "hmargin": ("left", "right"), "vmargin": ("top", "bottom"), "lmargin": ("left",), "rmargin": ("right",), "tmargin": ("top",), "bmargin": ("bottom",)}.get(key, (key,))
        for side in sides:
            margins[side] = pts
    double = None
    stretch = r"(?:\\setstretch\s*|\\linespread\s*|\\begin\s*\{\s*spacing\s*\}\s*|\\renewcommand\s*\{?\s*\\baselinestretch\s*\}?\s*)\{\s*"
    if re.search(r"\\doublespacing\b|" + stretch + r"(?:2|1\.[6-9])", source) or "doublespacing" in package_options.get("setspace", ""):
        double = True
    elif re.search(r"\\onehalfspacing\b|\\singlespacing\b|" + stretch + r"1(?:\.[0-5]\d*)?\s*\}", source):
        double = False
    line_numbers = bool(("lineno" in packages and re.search(r"\\linenumbers\b|\\begin\s*\{\s*linenumbers\s*\}", source)) or {"lineno", "linenumbers"} & set(options) or ("review" in options and "acmart" in classes))  # only acmart's review option numbers lines
    page_numbers = not re.search(r"\\pagestyle\s*\{\s*empty\s*\}|\\pagenumbering\s*\{\s*gobble\s*\}", source) and "nopageno" not in packages
    paper = next((o for o in options if o in ("letterpaper", "a4paper")), None) or next((o for o in ("letterpaper", "a4paper") if o in geometry), None)
    return TexLayout(classes, packages, size, font, margins, double, line_numbers, page_numbers, paper)


# --- the checks -----------------------------------------------------------------------


def _manuscripts(venue: Venue, values: dict[str, str]) -> list[tuple[Field, Path]]:
    from .requirements_check import manuscript_files

    files = manuscript_files(venue, values)
    if not files:
        raise NotApplicable("no manuscript file to measure")
    return files


@dataclass
class _Measured:
    """Whichever measurement a manuscript allows (PDF first, then Word, then LaTeX source)."""

    path: Path
    pdf: PdfLayout | None = None
    docx: DocxLayout | None = None
    tex: TexLayout | None = None
    why: str = ""


def _measure(path: Path) -> _Measured:
    from . import pdf_layout

    doc = document.get(path)
    out = _Measured(path)
    if doc.is_docx:
        out.docx = measure_docx(doc)
        if out.docx is None:
            out.why = f"could not read {path.name} as a Word file"
        return out
    if doc.is_tex and doc.tex_source:
        out.tex = measure_tex(doc.tex_source)
    if doc.is_pdf or doc.is_tex:
        if not pdf_layout.available():
            out.why = f"measuring {path.name} needs pdfminer.six ({pdf_layout.INSTALL_HINT})"
        elif doc.pdf is None:
            out.why = f"could not compile {path.name} to measure it (install latexmk or pdflatex)"
        elif doc.glyph_pages:
            out.pdf = measure_pdf(doc.glyph_pages)
            if out.pdf is None:
                out.why = f"too little text in {path.name} to measure its layout"
        else:
            out.why = f"could not read the text of {path.name}"
        return out
    out.why = f"cannot measure the layout of a '{path.suffix}' file"
    return out


def _measure_all(venue: Venue, values: dict[str, str]) -> list[tuple[Field, _Measured]]:
    return [(field, _measure(path)) for field, path in _manuscripts(venue, values)]


def _run_per_file(venue: Venue, values: dict[str, str], fn) -> Outcome:
    """Run ``fn(field, measurement)`` on each manuscript file.

    ``fn`` returns ``(issues, detail)``, or ``(None, reason)`` when the file's
    measurement cannot answer it; the check is unchecked only if no file can.
    """
    issues: list[Issue] = []
    details: list[str] = []
    reasons: list[str] = []
    measured = _measure_all(venue, values)
    for field, m in measured:
        found, detail = fn(field, m)
        if found is None:
            reasons.append(m.why or detail or f"could not measure {m.path.name}")
            continue
        issues.extend(found)
        if detail:
            details.append(detail if len(measured) == 1 else f"{m.path.name}: {detail}")
    if reasons and not details and not issues:
        raise Unchecked("; ".join(reasons))
    issues.extend(Issue(WARNING, "", reason, unchecked=True) for reason in reasons)
    return Outcome(issues, "; ".join(details))


def _issue(field: Field, message: str) -> Issue:
    return Issue(WARNING, field.id, message)


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    base = get_requirements(venue.slug)
    if base is None:
        return
    reqs = resolve(base, values)
    rules = reqs.manuscript
    logger.info("Checking the layout of the %s manuscript", venue.slug)

    def run(check_id: str, title: str, applies, reason: str, fn) -> None:
        if not applies:
            checks.record(check_id, title, LAYOUT, _na(), detail=reason)
            return
        checks.run(check_id, title, LAYOUT, _run_per_file, venue, values, fn)

    run("layout.template", "Uses the venue's Word template or LaTeX class (detected from the file)", rules.template_required or rules.latex_class, "the guidelines require no template", lambda f, m: _template(checks, f, m, reqs))
    run("layout.page_size", "Page size matches the guidelines (e.g., A4 or Letter)", rules.page_size, "the guidelines set no page size", lambda f, m: _page_size(f, m, rules.page_size))
    run("layout.margins", "Page margins match the guidelines", rules.margins_mm, "the guidelines set no margins", lambda f, m: _margins(f, m, rules.margins_mm))
    run("layout.font_size", "Body text size matches the guidelines", rules.font_size_pt, "the guidelines set no body font size", lambda f, m: _font_size(f, m, rules.font_size_pt))
    run("layout.font", "Body font matches the guidelines", rules.font, "the guidelines name no body font", lambda f, m: _font(f, m, rules.font))
    run("layout.line_spacing", "Text is double-spaced", rules.double_spacing, "the guidelines do not ask for double spacing", lambda f, m: _spacing(f, m))
    run("layout.line_numbers", "Lines numbered continuously", rules.line_numbers, "the guidelines do not ask for line numbers", lambda f, m: _line_numbers(f, m))
    run("layout.page_numbers", "Pages are numbered", rules.page_numbers, "the guidelines do not ask for page numbers", lambda f, m: _page_numbers(f, m))
    language = (rules.language or "").strip()
    applies = bool(language) and language.lower() not in ("english",)
    if applies:
        checks.run("layout.language", "Spelling follows the required English variant (US or UK)", LAYOUT, _language_check, venue, values, language)
    else:
        checks.record("layout.language", "Spelling follows the required English variant (US or UK)", LAYOUT, _na(), detail="the guidelines name no spelling variant")


def _na() -> str:
    from .report import NOT_APPLICABLE

    return NOT_APPLICABLE


def _template(checks: Checks, field: Field, m: _Measured, reqs: ManuscriptRequirements):
    from .ai_validate import AiTask

    rules = reqs.manuscript
    url = f" ({rules.template_url})" if rules.template_url else ""
    if m.tex is not None:
        if not rules.latex_class:
            checks.needs_ai(AiTask(id="ai.template", title="Looks prepared with the venue's template (judged from the layout when the file can't tell)", category=LAYOUT, instructions=f"The author guidelines require the venue's template{url}. Judge from the manuscript's source and layout whether it was prepared with that template. 'fail' if it plainly was not.", files=[str(m.path)]))
            raise Unchecked(f"LaTeX class {', '.join(m.tex.classes) or 'unknown'}; the guidelines name no class to compare, so an AI check is queued")
        names = set(m.tex.classes) | set(m.tex.packages)
        wanted = rules.latex_class
        if wanted in names or any(n.startswith(wanted) for n in names):
            return [], f"uses {wanted}"
        return [_issue(field, f"{m.path.name} does not load the '{wanted}' class or style (it uses \\documentclass{{{', '.join(m.tex.classes) or '?'}}}); the author guidelines require the venue's template{url}")], ""
    if m.docx is not None:
        task = AiTask(id="ai.template", title="Looks prepared with the venue's template (judged from the layout when the file can't tell)", category=LAYOUT, instructions=f"The author guidelines require the venue's template{url}. Judge from the manuscript's layout, styles, and front matter whether it was prepared with that template. 'fail' if it plainly was not.", files=[str(m.path)])
        checks.needs_ai(task)
        attached = f"attached to template '{m.docx.template}'" if m.docx.template else "no attached Word template recorded in the file"
        raise Unchecked(f"{m.path.name}: {attached}; which template made it is judged by the queued AI check")
    if m.pdf is not None:
        checks.needs_ai(AiTask(id="ai.template", title="Looks prepared with the venue's template (judged from the layout when the file can't tell)", category=LAYOUT, instructions=f"The author guidelines require the venue's {'LaTeX class ' + repr(rules.latex_class) if rules.latex_class else 'template'}{url}. Judge from the PDF's layout, fonts, headings, and front matter whether it was prepared with it. 'fail' if it plainly was not.", files=[str(m.path)]))
        raise Unchecked(f"{m.path.name} is a PDF: which template made it can only be judged by eye (an AI check is queued); upload the LaTeX source or Word file to check it directly")
    return None, ""


def _page_size(field: Field, m: _Measured, wanted: str):
    target = PAGE_SIZES.get(wanted.strip().lower())
    if target is None:
        raise Unchecked(f"unrecognised page size '{wanted}'")
    size = None
    if m.pdf is not None:
        size = (m.pdf.page_width, m.pdf.page_height)
    elif m.docx is not None and m.docx.page_width:
        size = (m.docx.page_width, m.docx.page_height)
    elif m.tex is not None and m.tex.paper:
        size = PAGE_SIZES["letter" if m.tex.paper == "letterpaper" else "a4"]
    if size is None:
        return None, ""
    w, h = size
    if abs(w - target[0]) > 3 or abs(h - target[1]) > 3:
        return [_issue(field, f"{m.path.name} pages are {w / 72:.2f} x {h / 72:.2f} in; the author guidelines ask for {wanted} ({target[0] / 72:.2f} x {target[1] / 72:.2f} in)")], ""
    return [], f"{wanted}"


def _margins(field: Field, m: _Measured, margin_mm: float):
    need = margin_mm * PT_PER_MM
    sides: dict[str, float | None] = {}
    if m.pdf is not None:
        sides = {"left": m.pdf.left, "right": m.pdf.right, "top": m.pdf.top, "bottom": m.pdf.bottom}
    elif m.docx is not None and m.docx.margins:
        sides = dict(m.docx.margins)
    elif m.tex is not None and m.tex.margins:
        sides = dict(m.tex.margins)
    else:
        return None, ""
    narrow = [f"{side} {value / PT_PER_MM:.1f} mm" for side, value in sides.items() if value is not None and value < need - MARGIN_TOLERANCE_PT]
    measured = ", ".join(f"{side} {value / PT_PER_MM:.0f} mm" for side, value in sides.items() if value is not None)
    if narrow:
        return [_issue(field, f"{m.path.name} has narrower margins than the {margin_mm:g} mm the author guidelines ask for: {', '.join(narrow)}")], ""
    return [], f"margins {measured}"


def _font_size(field: Field, m: _Measured, wanted: float):
    size = None
    if m.pdf is not None:
        size = m.pdf.body_font
    elif m.docx is not None and m.docx.sizes:
        size = m.docx.sizes.most_common(1)[0][0]
    elif m.tex is not None and m.tex.font_size:
        size = m.tex.font_size
    if size is None:
        return None, f"{m.path.name} does not record its font size"
    if abs(size - wanted) > FONT_SIZE_TOLERANCE_PT:
        return [_issue(field, f"body text of {m.path.name} is {size:g} pt; the author guidelines ask for {wanted:g} pt")], ""
    return [], f"body text {size:g} pt"


def _font(field: Field, m: _Measured, wanted: str):
    rule = parse_font_rule(wanted)
    if not rule.families and not rule.generics:
        raise Unchecked(f"could not tell which fonts '{wanted}' allows")
    if m.pdf is not None:
        weights = m.pdf.all_fonts if rule.only else m.pdf.body_fonts
    elif m.docx is not None and m.docx.fonts:
        weights = m.docx.fonts
    elif m.tex is not None and m.tex.font:
        weights = Counter({m.tex.font: 1})
    else:
        return None, f"{m.path.name} does not record its fonts"
    problems = font_problems(weights, rule, f"the text of {m.path.name}")
    main = font_family(weights.most_common(1)[0][0])[0]
    return [_issue(field, p) for p in problems], f"body font {main}"


def _spacing(field: Field, m: _Measured):
    if m.pdf is not None:
        if m.pdf.baseline_skip is None:
            return None, f"found no full pages of body text in {m.path.name} to measure its line spacing"
        ratio = m.pdf.baseline_skip / m.pdf.body_font
        family = font_family(m.pdf.body_fonts.most_common(1)[0][0])[0] if m.pdf.body_fonts else ""
        if ratio < max(DOUBLE_SPACING_RATIO, 1.5 * TALL_LINE_HEIGHT.get(family, 0) + 0.07):
            kind = "1.5-spaced" if ratio >= 1.4 else "single-spaced"
            return [_issue(field, f"{m.path.name} looks {kind}: its lines are {m.pdf.baseline_skip:g} pt apart ({ratio:.2f} x the {m.pdf.body_font:g} pt text); the author guidelines ask for double spacing")], ""
        return [], f"lines {ratio:.2f} x the font size apart"
    if m.docx is not None and m.docx.spacing:
        line, _ = m.docx.spacing.most_common(1)[0]
        if line == "exact" or not isinstance(line, float):
            raise Unchecked(f"{m.path.name} sets an exact line height; its spacing cannot be read as single or double")
        if line < 2.0 - 0.05:
            return [_issue(field, f"most of {m.path.name} is set at {line:g} line spacing; the author guidelines ask for double spacing")], ""
        return [], f"line spacing {line:g}"
    if m.tex is not None and m.tex.double_spacing is not None:
        if not m.tex.double_spacing:
            return [_issue(field, f"{m.path.name} sets single or 1.5 spacing; the author guidelines ask for double spacing")], ""
        return [], "double spacing set in the source"
    if m.tex is not None and not any(c in ("article", "report", "book", "amsart") for c in m.tex.classes):
        return None, f"{m.path.name} uses the class {', '.join(m.tex.classes) or 'unknown'}, which may set the line spacing itself; compile it to measure the spacing"
    if m.tex is not None:
        return [_issue(field, f"{m.path.name} does not set double spacing (no \\doublespacing, setspace, or \\linespread); the author guidelines ask for it")], ""
    return None, ""


def _line_numbers(field: Field, m: _Measured):
    if m.pdf is not None:
        if m.pdf.full_pages == 0:
            return None, f"found no full pages of body text in {m.path.name} to look for line numbers"
        share = m.pdf.line_number_pages / max(1, m.pdf.full_pages)
        if share < 0.6:
            return [_issue(field, f"no line numbers found in the margins of {m.path.name}; the author guidelines ask for continuous line numbers")], ""
        return [], f"line numbers on {m.pdf.line_number_pages} page(s)"
    if m.docx is not None:
        if not m.docx.line_numbers:
            return [_issue(field, f"{m.path.name} does not turn on line numbering (Layout > Line Numbers > Continuous); the author guidelines ask for it")], ""
        return [], "line numbering on"
    if m.tex is not None:
        if not m.tex.line_numbers:
            return [_issue(field, f"{m.path.name} does not number its lines (\\usepackage{{lineno}} and \\linenumbers); the author guidelines ask for it")], ""
        return [], "lineno in use"
    return None, ""


def _page_numbers(field: Field, m: _Measured):
    if m.pdf is not None:
        share = m.pdf.page_number_pages / max(1, m.pdf.pages)
        if m.pdf.pages > 1 and share < 0.75:
            return [_issue(field, f"page numbers found on only {m.pdf.page_number_pages} of {m.pdf.pages} pages of {m.path.name}; the author guidelines ask for page numbers")], ""
        return [], f"page numbers on {m.pdf.page_number_pages} of {m.pdf.pages} pages"
    if m.docx is not None:
        if not m.docx.page_numbers:
            return [_issue(field, f"{m.path.name} has no page-number field in its header or footer; the author guidelines ask for page numbers")], ""
        return [], "page-number field in header/footer"
    if m.tex is not None:
        if not m.tex.page_numbers:
            return [_issue(field, f"{m.path.name} sets \\pagestyle{{empty}}, which drops page numbers; the author guidelines ask for them")], ""
        return [], "page numbers left on"
    return None, ""


# --- spelling variant -------------------------------------------------------------------

# (American, British) stems that differ. -ize/-ise is handled apart: -ize is
# also Oxford (British) spelling, so only -ise marks British English.
_SPELLINGS: list[tuple[str, str]] = [
    ("color", "colour"), ("behavior", "behaviour"), ("favor", "favour"), ("labor", "labour"), ("neighbor", "neighbour"),
    ("tumor", "tumour"), ("humor", "humour"), ("vapor", "vapour"), ("rumor", "rumour"), ("center", "centre"),
    ("fiber", "fibre"), ("liter", "litre"), ("analyz", "analys"), ("catalyz", "catalys"), ("paralyz", "paralys"),
    ("modeling", "modelling"), ("modeled", "modelled"), ("labeling", "labelling"), ("labeled", "labelled"),
    ("signaling", "signalling"), ("traveled", "travelled"), ("canceled", "cancelled"), ("gray", "grey"),
    ("aluminum", "aluminium"), ("estrogen", "oestrogen"), ("hemoglobin", "haemoglobin"), ("anemia", "anaemia"),
    ("pediatric", "paediatric"), ("esophag", "oesophag"), ("leukemia", "leukaemia"), ("edema", "oedema"),
    ("defense", "defence"), ("artifact", "artefact"), ("aging", "ageing"), ("judgment", "judgement"),
    ("hematopoie", "haematopoie"), ("hematolog", "haematolog"),
]  # fmt: skip
# Endings a stem may take and still be the same word ("colored", "centres").
_ENDINGS = r"(?:|s|d|e|es|ed|ing|al|ally|ic|ist|ists|ation|ations|ly|r|rs|er|ers|ful|less|tic|tical|sis)"
# Words a stem and ending above would build that are spelled the same in both variants.
_SAME_IN_BOTH = {"analyses", "catalyses", "paralyses", "literal", "literals", "literally", "humoral", "coloration", "colorations"}
_IZE_STEMS = ["organi", "characteri", "normali", "optimi", "recogni", "minimi", "maximi", "utili", "visuali", "summari", "randomi", "categori", "generali", "speciali", "stabili", "standardi", "emphasi", "parameteri", "regulari", "locali", "initiali", "mobili", "polymeri", "neutrali", "prioriti", "harmoni", "critici"]  # fmt: skip
_IZE_ENDINGS = r"(?:e|es|ed|ing|ation|ations|er|ers)"


def spelling_counts(text: str) -> tuple[Counter, Counter, Counter, Counter]:
    """``(american, british, -ize, -ise)`` spellings found in ``text``, word -> count."""
    words = Counter(re.findall(r"[a-z]+", document.fold(text)))
    us: Counter = Counter()
    uk: Counter = Counter()
    ize: Counter = Counter()
    ise: Counter = Counter()
    for word, n in words.items():
        if word in _SAME_IN_BOTH:
            continue
        for american, british in _SPELLINGS:
            if re.fullmatch(re.escape(british) + _ENDINGS, word):
                uk[word] += n
                break
            if re.fullmatch(re.escape(american) + _ENDINGS, word):
                us[word] += n
                break
        else:
            for stem in _IZE_STEMS:
                if re.fullmatch(stem + "z" + _IZE_ENDINGS, word):
                    ize[word] += n
                    break
                if re.fullmatch(stem + "s" + _IZE_ENDINGS, word):
                    ise[word] += n
                    break
    return us, uk, ize, ise


def _language_check(venue: Venue, values: dict[str, str], language: str):
    from .manuscript import truncate_at_references
    from .requirements_check import manuscript_files

    files = manuscript_files(venue, values)
    if not files:
        raise NotApplicable("no manuscript file to read")
    lower = language.lower()
    issues: list[Issue] = []
    details: list[str] = []
    for field, path in files:
        text = document.get(path).text
        if text is None:
            issues.append(Issue(WARNING, field.id, f"could not read {path.name} as text; its spelling variant was not checked", unchecked=True))
            continue
        # The references keep their cited titles' spelling, whatever the manuscript's.
        us, uk, ize, ise = spelling_counts(truncate_at_references(text))
        sample = lambda c: ", ".join(w for w, _ in c.most_common(4))  # noqa: E731
        if "oxford" in lower:
            if us:
                issues.append(Issue(WARNING, field.id, f"{path.name} uses American spellings ({sample(us)}); the author guidelines ask for Oxford (British, -ize) spelling"))
            if ise:
                issues.append(Issue(WARNING, field.id, f"{path.name} uses -ise spellings ({sample(ise)}); Oxford spelling uses -ize"))
        elif "american" in lower and "british" not in lower:
            if uk or ise:
                issues.append(Issue(WARNING, field.id, f"{path.name} uses British spellings ({sample(uk + ise)}); the author guidelines ask for American English"))
        elif "british" in lower and "american" not in lower or "uk english" in lower:
            if us:
                issues.append(Issue(WARNING, field.id, f"{path.name} uses American spellings ({sample(us)}); the author guidelines ask for British English"))
        else:  # either, used consistently
            if us and (uk or ise):
                issues.append(Issue(WARNING, field.id, f"{path.name} mixes American ({sample(us)}) and British ({sample(uk + ise)}) spellings; the author guidelines ask for one variant used consistently"))
        if not issues:
            british = sum(uk.values()) + sum(ise.values())
            variant = "American" if sum(us.values()) > british else "British" if british else "no variant-specific"
            details.append(f"{variant} spelling ({sum(us.values())} American, {british} British forms)")
    return Outcome(issues, "; ".join(details))
