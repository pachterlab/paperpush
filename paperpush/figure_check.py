"""Check the figures: resolution, lettering size and font, panel labels, and more.

The figures checked are the figure files the ``.sub`` attaches or, when it
attaches none, the figures embedded in the manuscript itself -- the images and
vector drawings placed in a PDF (or a LaTeX source compiled to one), or the
pictures in a Word file. Each figure is reduced to a :class:`Figure`:

* a **raster** figure has a pixel size and the width it is printed at, hence an
  effective resolution in dpi. Its lettering is pixels, so its text size, font,
  and panel labels can only be judged by eye: those go to an AI task.
* a **vector** figure (a PDF or SVG file, or a drawing in the manuscript PDF)
  carries its text as glyphs, with a size and a font. A figure file is scaled
  to the venue's maximum print width first, so the sizes are the printed ones.

Against ``figures`` in ``manuscript_requirements.json`` this checks the
resolution (``min_dpi``, and ``min_dpi_line_art`` for figures with only a few
flat colours), the smallest and largest lettering (``min_font_size_pt`` /
``max_font_size_pt``), the typeface (``font``) and that it is the same in
every figure, the panel labels
(``multipanel_labels``: their case and weight in the figures, and that each
panel the text cites -- "Fig. 2d" -- exists, and that each labelled panel is
cited), the colour mode and print size of
attached raster files, and where the figures are placed (``placement``,
``manuscript.figures_placement``, ``manuscript.single_file``).
"""

from __future__ import annotations

import logging
import math
import re
import statistics
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import document
from .database import Venue
from .requirements import FigureRules, ManuscriptRequirements, get_requirements, resolve
from .validate import WARNING, Checks, Issue, NotApplicable, Outcome, Unchecked

logger = logging.getLogger(__name__)

FIGURES = "Figures"
MM_PER_INCH = 25.4
RASTER = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".webp"}
# Embedded images narrower than this (points) are logos and icons, not figures.
MIN_EMBEDDED_WIDTH_PT = 108.0
# A vector drawing with lettering counts as a figure down to this width.
MIN_EMBEDDED_DRAWING_WIDTH_PT = 54.0
# A raster figure with at most this many colours is line art.
LINE_ART_COLOURS = 16
# Letters that can label panels.
_PANEL = re.compile(r"^\(?([a-zA-Z])[).]?$")


@dataclass
class Text:
    """One run of figure lettering: its text, printed size (pt), font, and position."""

    text: str
    size: float
    font: str
    x: float
    y: float

    @property
    def bold(self) -> bool:
        return bool(re.search(r"bold|black|heavy|semibold|demi", self.font, re.IGNORECASE))


@dataclass
class Figure:
    name: str  # "fig1.png", or "manuscript.pdf page 3, image 1"
    path: Path  # the file that holds it
    field_id: str
    attached: bool
    kind: str  # "raster", "vector", or "other"
    width_px: int = 0
    height_px: int = 0
    dpi: float | None = None  # as printed / placed
    dpi_from_metadata: bool = False
    print_width_mm: float | None = None
    file_width_mm: float | None = None  # the file's own size, unscaled (vector files, Word pictures)
    file_height_mm: float | None = None
    mode: str = ""
    line_art: bool = False
    texts: list[Text] = field(default_factory=list)
    page: int = 0  # page of the manuscript, for an embedded figure
    offset: int = 0  # where a Word picture's drawing starts in document.xml
    number: int | None = None  # figure number, when known
    image_path: Path | None = None  # a viewable image file, for the AI checks


def _print_width_mm(rules: FigureRules) -> float | None:
    return rules.max_width_mm or rules.double_column_width_mm or rules.single_column_width_mm


def _figure_number(path: Path, label: str, index: int) -> int:
    # A number after "fig" first, then one standing alone: not part of a year, nor a version or supplementary number.
    for pattern in (r"fig(?:ure)?[\s_.-]*(\d{1,2})(?!\d)", r"(?<![\dvs])(\d{1,2})(?!\d)"):
        for text in (label, path.stem):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return int(match.group(1))
    return index


# Below this a raster figure looks blurry in print or when zoomed; applied when
# the venue's guidelines set no minimum of their own.
GENERAL_MIN_DPI = 150
# Printed lettering below this is unreadable; applied when the venue sets no minimum of its own.
GENERAL_MIN_FONT_PT = 4


# --- attached figure files ------------------------------------------------------


def _raster_figure(path: Path, field_id: str, rules: FigureRules, number: int) -> Figure:
    from PIL import Image

    fig = Figure(path.name, path, field_id, True, "raster", number=number, image_path=path)
    try:
        with Image.open(path) as img:
            fig.width_px, fig.height_px, fig.mode = img.width, img.height, img.mode
            dpi = img.info.get("dpi")
            if dpi:
                try:
                    value = float(dpi[0])
                    if value > 1:
                        fig.dpi, fig.dpi_from_metadata = value, True
                except (TypeError, ValueError, IndexError):
                    pass
            small = img.copy()
            small.thumbnail((400, 400), Image.NEAREST)  # resampling would invent greys
            colours = small.convert("RGB").getcolors(maxcolors=LINE_ART_COLOURS)
            fig.line_art = colours is not None
    except Exception as exc:  # noqa: BLE001 -- any unreadable image
        logger.debug("could not read %s: %s", path, exc)
        fig.kind = "other"
        return fig
    width = _print_width_mm(rules)
    if fig.dpi is not None:
        fig.print_width_mm = fig.width_px / fig.dpi * MM_PER_INCH
    elif width:
        fig.print_width_mm = width
        fig.dpi = fig.width_px / (width / MM_PER_INCH)
    return fig


def _texts_from_glyphs(glyphs, scale: float) -> list[Text]:
    """Group glyphs into runs (same baseline, close together) of printed lettering."""
    from .pdf_layout import _gap

    rows: dict[tuple[float, float], list] = {}
    for g in glyphs:
        if g.text.strip():
            key = (g.angle, round(g.baseline[0] * 2) / 2) if g.baseline else (0.0, round(g.y0 * 2) / 2)
            rows.setdefault(key, []).append(g)
    texts: list[Text] = []
    for row in rows.values():
        row.sort(key=lambda g: g.baseline[1] if g.baseline else g.x0)
        run = [row[0]]
        for g in row[1:]:
            if _gap(run[-1], g) > 1.5 * max(g.size, run[-1].size):
                texts.append(_text(run, scale))
                run = [g]
            else:
                run.append(g)
        texts.append(_text(run, scale))
    return texts


def _text(run, scale: float) -> Text:
    sizes = Counter(round(g.size, 1) for g in run)
    fonts = Counter(g.font for g in run)
    from .pdf_layout import join_glyphs

    return Text(join_glyphs(run), sizes.most_common(1)[0][0] * scale, fonts.most_common(1)[0][0], run[0].x0, run[0].y0)


def _pdf_figure(path: Path, field_id: str, rules: FigureRules, number: int) -> Figure:
    from . import pdf_layout

    fig = Figure(path.name, path, field_id, True, "vector", number=number)
    pages = pdf_layout.read_pages(path) if pdf_layout.available() else None
    if not pages:
        fig.kind = "other"
        return fig
    page = pages[0]
    width_pt = page.width
    max_mm = _print_width_mm(rules)
    scale = min(1.0, (max_mm / MM_PER_INCH * 72) / width_pt) if max_mm else 1.0
    fig.print_width_mm = width_pt * scale / 72 * MM_PER_INCH
    fig.file_width_mm, fig.file_height_mm = width_pt / 72 * MM_PER_INCH, page.height / 72 * MM_PER_INCH
    # Every page: a figure PDF can hold several figures.
    fig.texts = [text for p in pages for text in _texts_from_glyphs(p.glyphs, scale)]
    images = [im for p in pages for im in p.images if im.dpi]
    if images and not fig.texts:
        # A raster image wrapped in a PDF: its resolution is the image's, as scaled.
        largest = max(images, key=lambda im: (im.x1 - im.x0) * (im.y1 - im.y0))
        fig.kind = "raster"
        fig.width_px, fig.height_px = largest.width_px, largest.height_px
        fig.dpi = largest.dpi / scale
    elif images:
        fig.dpi = min(im.dpi for im in images) / scale
        fig.width_px = min(images, key=lambda im: im.dpi).width_px
    return fig


def _svg_length(value: str | None, em: float | None = None) -> float | None:
    """An SVG length in CSS pixels (1/96 in).

    ``em`` is what a relative length (em, ex, %) is relative to; without one, such a length is None.
    """
    if not value:
        return None
    match = re.match(r"\s*(-?\d*\.?\d+)\s*(px|pt|mm|cm|in|pc|rem|em|ex|%)?", value, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).lower() if match.group(2) else None
    if unit in ("em", "ex", "%", "rem"):
        if em is None:
            return None
        return number * {"em": em, "ex": em / 2, "%": em / 100, "rem": 16.0}[unit]
    return number * {"px": 1.0, None: 1.0, "pt": 96 / 72, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0, "pc": 16.0}[unit]


def _css_declarations(text: str) -> dict[str, str]:
    """``{property: value}`` of a CSS declaration list, the ``font`` shorthand expanded."""
    out = {k.strip().lower(): v.strip() for k, v in (item.split(":", 1) for item in text.split(";") if ":" in item)}
    # A size with a unit first: the "700" of Matplotlib's "font: 700 10px 'DejaVu Sans'" is the weight.
    font = out.get("font", "")
    shorthand = re.search(r"(?:^|\s)(\d*\.?\d+(?:px|pt|mm|cm|in|pc|rem|em|ex|%))(?:/\S+)?\s+(.+)$", font) or re.search(r"(?:^|\s)(\d*\.?\d+)(?:/\S+)?\s+(.+)$", font)
    if shorthand:
        out.setdefault("font-size", shorthand.group(1))
        out.setdefault("font-family", shorthand.group(2))
        if re.search(r"\bbold\b|\b[789]00\b", out["font"][: shorthand.start()]):
            out.setdefault("font-weight", "bold")
    return out


def _svg_css(root) -> list[tuple[str, dict[str, str]]]:
    """The simple rules of the SVG's ``<style>`` blocks: (selector, declarations), classes last so they win."""
    rules: list[tuple[str, dict[str, str]]] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == "style" and node.text:
            for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", node.text, flags=re.DOTALL)):
                rules.extend((s.strip(), _css_declarations(body)) for s in selectors.split(","))
    return sorted(rules, key=lambda rule: "." in rule[0])


def _svg_zoom(transform: str | None) -> float:
    """How much a ``transform`` scales text vertically."""
    zoom = 1.0
    for name, args in re.findall(r"(scale|matrix)\s*\(([^)]*)\)", transform or ""):
        try:
            values = [float(v) for v in re.split(r"[\s,]+", args.strip()) if v]
        except ValueError:
            continue
        if name == "scale" and values:
            zoom *= abs(values[-1])
        elif name == "matrix" and len(values) == 6:
            zoom *= math.hypot(values[2], values[3])
    return zoom


def _svg_figure(path: Path, field_id: str, rules: FigureRules, number: int) -> Figure:
    fig = Figure(path.name, path, field_id, True, "vector", number=number)
    from .layout_check import safe_xml

    try:
        root = safe_xml(path.read_bytes())
    except OSError as exc:
        logger.debug("could not read %s: %s", path, exc)
        root = None
    if root is None:
        fig.kind = "other"
        return fig
    try:
        view = [float(v) for v in re.split(r"[\s,]+", root.get("viewBox", "").strip()) if v] if root.get("viewBox") else []
    except ValueError:
        view = []
    width_px = _svg_length(root.get("width")) or (view[2] if len(view) == 4 else None)
    if not width_px:
        fig.kind = "other"
        return fig
    user_to_px = width_px / view[2] if len(view) == 4 and view[2] else 1.0
    max_mm = _print_width_mm(rules)
    width_mm = width_px / 96 * MM_PER_INCH
    scale = min(1.0, max_mm / width_mm) if max_mm else 1.0
    fig.print_width_mm = width_mm * scale
    height_px = _svg_length(root.get("height")) or (view[3] * user_to_px if len(view) == 4 else None)
    fig.file_width_mm, fig.file_height_mm = width_mm, height_px / 96 * MM_PER_INCH if height_px else None
    rules = _svg_css(root)

    def walk(node, size: float, family: str, weight: str, zoom: float) -> None:
        tag = node.tag.rsplit("}", 1)[-1]
        classes = (node.get("class") or "").split()
        style: dict[str, str] = {}
        for selector, declarations in rules:
            name, _, cls = selector.partition(".")
            if name in ("", "*", tag) and (not cls or cls in classes):
                style.update(declarations)
        style.update(_css_declarations(node.get("style") or ""))
        size = _svg_length(style.get("font-size") or node.get("font-size"), size) or size
        family = style.get("font-family") or node.get("font-family") or family
        weight = style.get("font-weight") or node.get("font-weight") or weight
        zoom *= _svg_zoom(node.get("transform"))
        if tag in ("text", "tspan") and (node.text or "").strip() and size:
            pt = size * zoom * user_to_px * 72 / 96 * scale
            font = family.split(",")[0].strip().strip("'\"") + ("-Bold" if weight in ("bold", "700", "800", "900") else "")
            fig.texts.append(Text(node.text.strip(), pt, font, 0.0, 0.0))
        for child in node:
            walk(child, size, family, weight, zoom)

    walk(root, 16.0, "sans-serif", "normal", 1.0)
    return fig


def attached_figures(venue: Venue, values: dict[str, str], rules: FigureRules) -> list[Figure]:
    from .requirements_check import _fields_for

    figures: list[Figure] = []
    index = 0
    for fld in _fields_for(venue, "figures"):
        for line in values.get(fld.id, "").splitlines():
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split("|")]
            path = Path(parts[0]).expanduser()
            if not path.is_file():
                continue
            index += 1
            label = parts[1] if len(parts) > 1 else ""
            number = _figure_number(path, label, index)
            suffix = path.suffix.lower()
            if suffix == ".docx":
                # One Word file holding several figures (a journal that asks for them combined).
                pictures = _embedded_docx(document.get(path), fld.id)
                for n, fig in enumerate(pictures):
                    fig.attached = True
                    fig.number = index + n
                index += max(len(pictures) - 1, 0)
                figures.extend(pictures or [Figure(path.name, path, fld.id, True, "other", number=number)])
            elif suffix in RASTER:
                figures.append(_raster_figure(path, fld.id, rules, number))
            elif suffix == ".pdf":
                figures.append(_pdf_figure(path, fld.id, rules, number))
            elif suffix == ".svg":
                figures.append(_svg_figure(path, fld.id, rules, number))
            else:
                figures.append(Figure(path.name, path, fld.id, True, "other", number=number))
    return figures


# --- figures embedded in the manuscript -----------------------------------------------


def _embedded_pdf(doc: document.Document, field_id: str) -> list[Figure]:
    pages = doc.glyph_pages or []
    figures: list[Figure] = []
    for page in pages:
        taken: set[int] = set()
        n = 0
        for image in page.images:
            width_pt = image.placed_width
            if width_pt < MIN_EMBEDDED_WIDTH_PT or image.dpi is None:
                continue
            n += 1
            fig = Figure(f"{doc.path.name} page {page.number}, image {n}", doc.path, field_id, False, "raster", page=page.number)
            fig.width_px, fig.height_px, fig.dpi = image.width_px, image.height_px, image.dpi
            fig.print_width_mm = width_pt / 72 * MM_PER_INCH
            if image.figure:
                taken.add(image.figure)
            figures.append(fig)
        # Vector drawings: Form XObjects (a LaTeX \includegraphics of a PDF) with lettering.
        by_figure: dict[int, list] = {}
        for g in page.glyphs:
            if g.figure and g.text.strip():
                by_figure.setdefault(g.figure, []).append(g)
        for number, glyphs in sorted(by_figure.items()):
            box = page.figure_boxes[number - 1] if number - 1 < len(page.figure_boxes) else None
            # A small drawing with real lettering is a figure placed too small, not a logo.
            if box is None or box[2] - box[0] < (MIN_EMBEDDED_DRAWING_WIDTH_PT if len(glyphs) >= 8 else MIN_EMBEDDED_WIDTH_PT):
                continue
            fig = Figure(f"{doc.path.name} page {page.number}, drawing {number}", doc.path, field_id, False, "vector", page=page.number)
            fig.print_width_mm = (box[2] - box[0]) / 72 * MM_PER_INCH
            fig.texts = _texts_from_glyphs(glyphs, 1.0)
            images = [im for im in page.images if im.figure == number and im.dpi]
            if images:
                fig.dpi = min(im.dpi for im in images)
            figures.append(fig)
    return figures


_EMU_PER_INCH = 914400


def _embedded_docx(doc: document.Document, field_id: str) -> list[Figure]:
    from PIL import Image
    import io

    xml = doc.docx_part("word/document.xml") or ""
    rels = doc.docx_part("word/_rels/document.xml.rels") or ""
    targets = dict(re.findall(r'Id="([^"]+)"[^>]*?Target="([^"]+)"', rels))
    targets.update({k: v for v, k in re.findall(r'Target="([^"]+)"[^>]*?Id="([^"]+)"', rels)})
    figures: list[Figure] = []
    scratch: Path | None = None
    for n, drawing in enumerate(re.finditer(r"<w:drawing>.*?</w:drawing>", xml, re.DOTALL), start=1):
        block = drawing.group(0)
        extent = re.search(r'<wp:extent cx="(\d+)" cy="(\d+)"', block)
        embed = re.search(r'r:embed="([^"]+)"', block)
        if not extent or not embed or embed.group(1) not in targets:
            continue
        width_in = int(extent.group(1)) / _EMU_PER_INCH
        if width_in * 72 < MIN_EMBEDDED_WIDTH_PT:
            continue
        target = targets[embed.group(1)].lstrip("/")
        name = target if target.startswith("word/") else f"word/{target}"
        data = doc.docx_media(name)
        if data is None:
            continue
        fig = Figure(f"{doc.path.name} picture {n} ({Path(name).name})", doc.path, field_id, False, "raster", offset=drawing.start())
        fig.print_width_mm = fig.file_width_mm = width_in * MM_PER_INCH
        fig.file_height_mm = int(extent.group(2)) / _EMU_PER_INCH * MM_PER_INCH
        try:
            with Image.open(io.BytesIO(data)) as img:
                fig.width_px, fig.height_px, fig.mode = img.width, img.height, img.mode
                fig.dpi = img.width / width_in
                small = img.copy()
                small.thumbnail((400, 400), Image.NEAREST)  # resampling would invent greys
                fig.line_art = small.convert("RGB").getcolors(maxcolors=LINE_ART_COLOURS) is not None
                if scratch is None:
                    scratch = Path(tempfile.mkdtemp(prefix="paperpush-docx-figures-"))
                fig.image_path = scratch / Path(name).name
                fig.image_path.write_bytes(data)
        except Exception:  # noqa: BLE001 -- EMF/WMF and other formats PIL cannot open
            fig.kind = "other"
        figures.append(fig)
    return figures


def embedded_figures(venue: Venue, values: dict[str, str]) -> list[Figure]:
    from .requirements_check import manuscript_files

    figures: list[Figure] = []
    for fld, path in manuscript_files(venue, values):
        doc = document.get(path)
        if doc.is_docx:
            figures.extend(_embedded_docx(doc, fld.id))
        elif doc.is_pdf or doc.is_tex:
            figures.extend(_embedded_pdf(doc, fld.id))
    return figures


@dataclass
class _Inventory:
    figures: list[Figure]
    attached: bool
    why_empty: str = ""
    # The figures' colours, read once by _figure_drawings: (drawings, names not read).
    drawings: tuple[list, list[str]] | None = field(default=None, repr=False, compare=False)


def inventory(venue: Venue, values: dict[str, str], rules: FigureRules) -> _Inventory:
    """The figures to check: the attached files, else those embedded in the manuscript."""
    from . import pdf_layout

    attached = attached_figures(venue, values, rules)
    if attached:
        return _Inventory(attached, True)
    embedded = embedded_figures(venue, values)
    why = ""
    if not embedded:
        from .requirements_check import manuscript_files

        files = manuscript_files(venue, values)
        if not files:
            why = "no figure files and no manuscript to look in"
        elif any(document.get(p).suffix in (".pdf", ".tex", ".zip") for _, p in files) and not pdf_layout.available():
            why = f"no figure files attached, and finding the figures in the manuscript PDF needs pdfminer.six ({pdf_layout.INSTALL_HINT})"
        else:
            why = "no figure files attached and no figures found in the manuscript"
    return _Inventory(embedded, False, why)


# --- the checks ------------------------------------------------------------------------


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    from .report import NOT_APPLICABLE, UNCHECKED

    base = get_requirements(venue.slug)
    if base is None:
        return
    reqs = resolve(base, values)
    rules = reqs.figures
    logger.info("Checking the %s figures", venue.slug)
    inv = inventory(venue, values, rules)
    where = "attached figure files" if inv.attached else "figures embedded in the manuscript"

    def run(check_id: str, title: str, applies, reason: str, fn, *args) -> None:
        if not applies:
            checks.record(check_id, title, FIGURES, NOT_APPLICABLE, detail=reason)
        elif not inv.figures:
            checks.record(check_id, title, FIGURES, NOT_APPLICABLE if inv.why_empty.startswith(("no figure files attached and no figures", "no figure files and no manuscript")) else UNCHECKED, detail=inv.why_empty)
        else:
            checks.run(check_id, title, FIGURES, fn, *args)

    # With no venue minimum, still flag figures below a general floor that any
    # printed or zoomed-in PDF shows as blurry.
    run("figures.dpi", "Figures meet the minimum resolution (dpi)", True, "", _dpi_check, inv, rules, where)
    # With no venue rule, lettering below a general floor is still flagged.
    run("figures.text_size", "Figure lettering within the guidelines' size range", True, "", _text_size_check, checks, inv, rules, where)
    run("figures.font", "Figure lettering uses the required typeface", rules.font, "the guidelines name no figure font", _font_check, checks, inv, rules, where)
    run("figures.font_consistency", CONSISTENCY_TITLE, True, "", _font_consistency_check, checks, venue, values, inv, rules, where)
    run("figures.lettering", "Figure panels labeled in the required style (e.g., a, b, c)", True, "", _lettering_check, checks, venue, values, inv, rules)
    run("figures.color_mode", "Figure colour mode (RGB, CMYK, grayscale) matches the guidelines", rules.color_mode and inv.attached, "the guidelines set no colour mode (or no figure files are attached)", _color_mode_check, inv, rules)
    run("figures.dimensions", "Figures within the guidelines' maximum width and height", (rules.max_width_mm or rules.max_height_mm) and inv.attached, "the guidelines set no maximum figure size (or no figure files are attached)", _dimensions_check, inv, rules)
    if inv.figures:
        _content_task(checks, venue, values, inv)
    # Reads the colours itself (pypdf and Pillow), so it does not need the inventory's figures.
    checks.run("figures.colorblind", COLORBLIND_TITLE, FIGURES, _colorblind_check, venue, values, inv)
    from .figure_design_check import run_design_checks

    run_design_checks(checks, venue, values, inv, rules)
    placement = rules.placement or reqs.manuscript.figures_placement or ("inline" if reqs.manuscript.single_file else None)
    checks.run("figures.placement", "Figures placed where the guidelines ask", FIGURES, _placement_check, venue, values, inv, placement, reqs)


def _dpi_check(inv: _Inventory, rules: FigureRules, where: str) -> Outcome:
    issues: list[Issue] = []
    measured: list[float] = []
    unknown: list[str] = []
    for fig in inv.figures:
        if fig.dpi is None:
            if fig.kind == "raster" or fig.path.suffix.lower() in RASTER:
                unknown.append(fig.name)
            continue
        minimum = rules.min_dpi_line_art if fig.line_art and rules.min_dpi_line_art else rules.min_dpi or rules.min_dpi_line_art
        general = not minimum
        minimum = minimum or GENERAL_MIN_DPI
        measured.append(fig.dpi)
        if fig.dpi < minimum - 0.5:
            kind = "line art" if fig.line_art and rules.min_dpi_line_art else "figure"
            # Say where the print width came from when it was not the file's own metadata.
            if not fig.attached and fig.print_width_mm:
                where_from = f" as placed ({fig.print_width_mm:.0f} mm wide)"
            elif not fig.dpi_from_metadata and fig.print_width_mm:
                where_from = f" at the {fig.print_width_mm:g} mm print width"
            else:
                where_from = ""
            if general:
                issues.append(Issue(WARNING, fig.field_id, f"{fig.name} is {fig.dpi:.0f} dpi{where_from} ({fig.width_px} px wide), below the {GENERAL_MIN_DPI} dpi that prints sharply (the guidelines set no minimum; journals usually ask for 300 dpi)"))
            else:
                issues.append(Issue(WARNING, fig.field_id, f"{fig.name} is {fig.dpi:.0f} dpi{where_from} ({fig.width_px} px wide); the author guidelines ask for at least {minimum} dpi for {kind}"))
    why = f"could not tell the resolution of {', '.join(unknown[:4])}: the image records no dpi, or could not be read, and the guidelines give no print width to work it out from"
    if not measured:
        raise Unchecked(why if unknown else f"no raster images among the {where}; vector figures have no resolution to check")
    if unknown:
        issues.append(Issue(WARNING, "", why, unchecked=True))
    return Outcome(issues, f"{len(measured)} raster figure(s) in the {where}, lowest {min(measured):.0f} dpi")


def _raster_ai_task(checks: Checks, inv: _Inventory, rules: FigureRules) -> int:
    """Queue the AI check for lettering in raster figures; returns how many figures it covers."""
    from .ai_validate import AiTask

    rasters = [f for f in inv.figures if f.kind == "raster"]
    if not rasters:
        return 0
    wants = []
    if rules.min_font_size_pt:
        wants.append(f"no lettering smaller than {rules.min_font_size_pt:g} pt when printed")
    if rules.max_font_size_pt:
        wants.append(f"no lettering larger than {rules.max_font_size_pt:g} pt")
    if rules.font:
        wants.append(f"the typeface is {rules.font}")
    if rules.multipanel_labels:
        wants.append(f"multi-panel figures label their panels {rules.multipanel_labels}")
    if len(inv.figures) > 1:
        wants.append("all figures use the same typeface (this is good practice even where the guidelines are silent)")
    if not wants:
        return 0
    lines = []
    files: list[str] = []
    for fig in rasters:
        source = fig.image_path or fig.path
        files.append(str(source))
        size = ""
        if fig.print_width_mm and rules.min_font_size_pt:
            # A capital letter is ~0.7 of the font size tall; as a fraction of the
            # figure's width this does not depend on how the image is scaled.
            fraction = 0.7 * rules.min_font_size_pt / (fig.print_width_mm / MM_PER_INCH * 72)
            size = f"; printed {fig.print_width_mm:.0f} mm wide, so a capital letter must be at least {100 * fraction:.2f}% of the image width tall"
            if fig.width_px:
                size += f" (about {fraction * fig.width_px:.0f} px of its {fig.width_px} px width)"
        where = f" (page {fig.page} of {fig.path.name})" if fig.page and fig.image_path is None else ""
        lines.append(f"- {fig.name}{where}{size}")
    checks.needs_ai(
        AiTask(
            id="ai.figures.raster_lettering",
            title="Lettering in raster figures (size, typeface, panel labels)",
            category=FIGURES,
            instructions="These figures are pixel images, so their lettering cannot be measured from the file. The author guidelines ask for: " + "; ".join(wants) + ".\n" + "\n".join(lines) + "\nReport 'fail' naming each figure and what is wrong (e.g. axis tick labels too small, a serif font, unlabelled panels), else 'pass'.",
            files=list(dict.fromkeys(files)),
        )
    )
    return len(rasters)


def _raster_panels_task(checks: Checks, inv: _Inventory, cites: dict[int, set[str]]) -> None:
    """Queue the AI check that every panel of a raster figure is cited (its labels cannot be read)."""
    from .ai_validate import AiTask

    rasters = [f for f in inv.figures if f.kind == "raster" and f.number in cites]
    if not rasters:
        return
    lines = [f"- {f.name} (Figure {f.number}): the text cites panel(s) {', '.join(sorted(cites[f.number], key=str.lower))}" for f in rasters]
    checks.needs_ai(
        AiTask(
            id="ai.figures.raster_panels_cited",
            title="Every panel of the raster figures is cited",
            category=FIGURES,
            instructions="Read the panel labels (a, b, c... or A, B, C...) drawn in each figure below and compare them with the panels the text cites.\n" + "\n".join(lines) + "\nReport 'fail' naming each figure with a labelled panel the text never cites (e.g. 'Figure 2 has panel e, which the text never cites'), else 'pass'. A panel the text cites but the figure does not label is also a 'fail'.",
            files=list(dict.fromkeys(str(f.image_path or f.path) for f in rasters)),
        )
    )


# A sentence ends at . ! or ? followed by a capital, except after "Fig.", "et al.", "e.g." and the like.
_SENTENCE_END = re.compile(r"(?<!\bFig)(?<!\bFigs)(?<!\bal)(?<!e\.g)(?<!i\.e)(?<!\bvs)(?<!\bref)(?<!\brefs)(?<!\bSuppl)(?<!\bSupp)(?<!\bno)(?<!\bEq)[.!?](?=\s+[A-Z(\[])")
_MAX_SENTENCE_CHARS = 800
_MAX_SENTENCES_PER_FIGURE = 8
_MAX_LEGEND_CHARS = 1500


def _sentence_around(text: str, ends: list[int], start: int, end: int, *, paragraphs: bool = False) -> tuple[int, int]:
    """The span of the sentence holding ``text[start:end]``, capped at whole words a few hundred characters out.

    ``paragraphs``: a line of ``text`` is a paragraph, so a sentence stops at a
    line break (and a heading above it is left out).
    """
    import bisect

    i = bisect.bisect_left(ends, start)
    left = ends[i - 1] + 1 if i else 0
    right = next((e + 1 for e in ends[i:] if e >= end), len(text))
    if paragraphs:
        left = max(left, text.rfind("\n", 0, start) + 1)
        line_end = text.find("\n", end)
        right = min(right, line_end if line_end >= 0 else len(text))
    if start - left > _MAX_SENTENCE_CHARS // 2:
        left = start - _MAX_SENTENCE_CHARS // 2
        while left < start and not text[left - 1].isspace():
            left += 1
    if right - end > _MAX_SENTENCE_CHARS // 2:
        right = end + _MAX_SENTENCE_CHARS // 2
        while right > end and not text[right].isspace():
            right -= 1
    return left, right


def figure_descriptions(text: str, *, paragraphs: bool = False) -> tuple[dict[int, str], dict[int, list[tuple[str, set[str]]]]]:
    """What ``text`` says about each main figure.

    Returns the legends (figure number -> legend text) and, per figure, the
    sentences that cite it with the panel letters each one cites:
    ``{2: [("Survival fell (Fig. 2b).", {"b"}), ...]}``. ``paragraphs``: each
    line of ``text`` is a whole paragraph (a Word file).
    """
    from .citation_check import legends, mentions, panel_mentions

    found_legends = {number: legend for number, _, legend in legends(text, paragraphs=paragraphs)}
    ends = [m.start() for m in _SENTENCE_END.finditer(text)]
    panels = panel_mentions(text, "figure")
    sentences: dict[int, list[tuple[str, set[str]]]] = {}
    seen: set[tuple[int, int]] = set()
    for m in mentions(text):
        if m.series != "figure" or m.caption:
            continue
        span = _sentence_around(text, ends, m.pos, m.pos + len(m.text), paragraphs=paragraphs)
        if (m.number, span[0]) in seen:
            continue
        seen.add((m.number, span[0]))
        letters = {p.letter for p in panels if p.number == m.number and span[0] <= p.pos < span[1]}
        sentence = " ".join(text[span[0] : span[1]].split())
        sentences.setdefault(m.number, []).append((sentence, letters))
    return found_legends, sentences


def _content_task(checks: Checks, venue: Venue, values: dict[str, str], inv: _Inventory) -> None:
    """Queue the AI check that each figure, and each of its panels, shows what its legend and the text say."""
    from .ai_validate import AiTask
    from .citation_check import _document_files, legends, tex_legends
    from .requirements_check import manuscript_files

    manuscripts = manuscript_files(venue, values)
    found_legends: dict[int, str] = {}
    sentences: dict[int, list[tuple[str, set[str]]]] = {}
    for _, path in manuscripts:
        doc = document.get(path)
        text = doc.text
        if text:
            got_legends, got_sentences = figure_descriptions(text, paragraphs=doc.is_docx)
            for number, legend in got_legends.items():
                found_legends.setdefault(number, legend)
            for number, items in got_sentences.items():
                sentences.setdefault(number, []).extend(items)
        if doc.is_tex and doc.pdf is None and doc.tex_source:
            for number, _, legend in tex_legends(doc.tex_source):
                found_legends.setdefault(number, legend)
    # Figures combined in their own document carry their legends with them.
    for _, path in _document_files(venue, values, ("figures",)):
        doc = document.get(path)
        for number, _, legend in legends(doc.text or "", paragraphs=doc.is_docx):
            found_legends.setdefault(number, legend)
    numbers = sorted(set(found_legends) | set(sentences))
    if not numbers:
        return

    def described(number: int) -> list[str]:
        out = []
        if number in found_legends:
            legend = found_legends[number]
            out.append(f"  Legend: {legend[:_MAX_LEGEND_CHARS]}{' [...]' if len(legend) > _MAX_LEGEND_CHARS else ''}")
        cited = sentences.get(number, [])
        for sentence, letters in cited[:_MAX_SENTENCES_PER_FIGURE]:
            which = f"panel(s) {', '.join(sorted(letters, key=str.lower))}" if letters else "the whole figure"
            out.append(f"  Text, on {which}: \"{sentence}\"")
        if len(cited) > _MAX_SENTENCES_PER_FIGURE:
            out.append(f"  ... and {len(cited) - _MAX_SENTENCES_PER_FIGURE} more citing sentence(s) in the manuscript")
        panel_letters = sorted({c for _, letters in cited for c in letters}, key=str.lower)
        if panel_letters:
            out.append(f"  Panels the text cites: {', '.join(panel_letters)}")
        return out or ["  (no legend or citing sentence found; judge it from the manuscript)"]

    lines: list[str] = []
    files: list[str] = []
    numbered = {f.number: f for f in inv.figures if f.number is not None}
    if inv.attached and numbered:
        for number in sorted(set(numbers) | set(numbered)):
            fig = numbered.get(number)
            if fig is None:
                lines.append(f"Figure {number}: no attached file has this number")
            else:
                lines.append(f"Figure {number}: {fig.name}")
                files.append(str(fig.image_path or fig.path))
            lines.extend(described(number))
        where = "Each figure below is an attached file."
    else:
        for number in numbers:
            lines.append(f"Figure {number}:")
            lines.extend(described(number))
        files.extend(str(p) for _, p in manuscripts)
        files.extend(str(f.image_path) for f in inv.figures if f.image_path)
        where = "The figures are embedded in the manuscript; find each one by its caption or position."
    if not files:
        return
    checks.needs_ai(
        AiTask(
            id="ai.figures.content_matches_text",
            title="Each figure and panel shows what its legend and the text say",
            category=FIGURES,
            instructions=(
                f"{where} Look at each figure and compare what it shows with its legend and with the sentences that cite it.\n"
                "- Whole figure: it is the figure the legend describes (the kind of plot or image, the variables and axes, the conditions or groups, the number of panels), "
                "and not another figure's file; what the text says the figure shows (a trend, a difference between groups, a value, a structure) is visible in it.\n"
                "- Each panel: every panel the legend or the text names by letter shows what they say it shows. A sentence citing 'Fig. 2b' for a survival curve fails "
                "if panel b is a bar chart and the survival curve is panel c. A legend describing panels a-d fails if the figure has only three.\n"
                + "\n".join(lines)
                + "\nJudge only whether the content matches the description, not the figure's style, lettering, or statistics. Report 'fail' naming each figure and panel "
                "that does not match and how (e.g. 'Figure 3b is a heatmap of expression, but the text cites it for the Kaplan-Meier curve, which is Figure 3c'; "
                "'the file attached as Figure 2 shows what the legend of Figure 3 describes'), else 'pass'."
            ),
            files=list(dict.fromkeys(files)),
        )
    )


def _outlined(inv: _Inventory) -> list[str]:
    """Vector figures with no readable text: any lettering they have is drawn as shapes."""
    return [f.name for f in inv.figures if f.kind == "vector" and not f.texts]


def _text_size_check(checks: Checks, inv: _Inventory, rules: FigureRules, where: str) -> Outcome:
    issues: list[Issue] = []
    smallest: list[float] = []
    for fig in inv.figures:
        if fig.kind != "vector" or not fig.texts:
            continue
        sizes = [t.size for t in fig.texts]
        smallest.append(min(sizes))
        floor = rules.min_font_size_pt or GENERAL_MIN_FONT_PT
        small = [t for t in fig.texts if t.size < floor - 0.25 and t.text.strip()]
        # the general floor is a readability backstop: flag only when most of the lettering is under it
        if small and (rules.min_font_size_pt or len(small) >= max(3, len(fig.texts) // 2)):
            examples = ", ".join(f"'{t.text[:20]}' {t.size:.1f} pt" for t in sorted(small, key=lambda t: t.size)[:3])
            rule = f"the {rules.min_font_size_pt:g} pt minimum" if rules.min_font_size_pt else f"{GENERAL_MIN_FONT_PT:g} pt, too small to read in print (the guidelines set no minimum; 5-7 pt is usual)"
            issues.append(Issue(WARNING, fig.field_id, f"{fig.name}: {len(small)} text item(s) print smaller than {rule} (e.g. {examples})"))
        if rules.max_font_size_pt:
            # Panel labels are often set larger on purpose; leave single letters out.
            large = [t for t in fig.texts if t.size > rules.max_font_size_pt + 0.25 and not _PANEL.match(t.text.strip())]
            if large:
                examples = ", ".join(f"'{t.text[:20]}' {t.size:.1f} pt" for t in sorted(large, key=lambda t: -t.size)[:3])
                issues.append(Issue(WARNING, fig.field_id, f"{fig.name}: {len(large)} text item(s) print larger than the {rules.max_font_size_pt:g} pt maximum (e.g. {examples})"))
    queued = _raster_ai_task(checks, inv, rules)
    others = [f.name for f in inv.figures if f.kind == "other"]
    if others:
        issues.append(Issue(WARNING, "", f"cannot read the lettering of {', '.join(others[:4])} (unreadable, or not a PDF, SVG, or raster image); check its size by hand", unchecked=True))
    outlined = _outlined(inv)
    if outlined and smallest:
        issues.append(Issue(WARNING, "", f"no lettering to measure in {', '.join(outlined[:4])} (its text may be outlined to shapes); check its size by hand", unchecked=True))
    if not smallest:
        raise Unchecked(f"no vector lettering to measure in the {where}" + (f"; {queued} raster figure(s) are queued for the AI check" if queued else ""))
    detail = f"{len(smallest)} vector figure(s) measured, smallest text {min(smallest):.1f} pt" + (f"; {queued} raster figure(s) need the AI check" if queued else "")
    return Outcome(issues, detail)


def _font_check(checks: Checks, inv: _Inventory, rules: FigureRules, where: str) -> Outcome:
    from .layout_check import font_problems, parse_font_rule

    rule = parse_font_rule(rules.font or "")
    if not rule.families and not rule.generics:
        raise Unchecked(f"could not tell which fonts '{rules.font}' allows")
    issues: list[Issue] = []
    used: Counter = Counter()
    for fig in inv.figures:
        if fig.kind != "vector" or not fig.texts:
            continue
        weights = Counter()
        for t in fig.texts:
            weights[t.font] += len(t.text)
        used.update(weights)
        issues.extend(Issue(WARNING, fig.field_id, p) for p in font_problems(weights, rule, fig.name))
    if "embed" in (rules.font or "").lower():
        from .layout_check import unembedded_fonts

        for fig in inv.figures:
            if fig.attached and fig.path.suffix.lower() == ".pdf":
                missing = unembedded_fonts(fig.path)
                if missing:
                    issues.append(Issue(WARNING, fig.field_id, f"{fig.name} does not embed the font(s) {', '.join(missing[:4])}; the author guidelines ask for all fonts to be embedded"))
    queued = _raster_ai_task(checks, inv, rules)
    if not used:
        raise Unchecked(f"no vector lettering whose font can be read in the {where}" + (f"; {queued} raster figure(s) are queued for the AI check" if queued else ""))
    from .layout_check import font_family

    outlined = _outlined(inv)
    if outlined:
        issues.append(Issue(WARNING, "", f"no lettering to read in {', '.join(outlined[:4])} (its text may be outlined to shapes); check its typeface by hand", unchecked=True))
    families = sorted({font_family(name)[0] for name in used})
    return Outcome(issues, f"fonts used: {', '.join(families[:6])}" + (f"; {queued} raster figure(s) need the AI check" if queued else ""))


CONSISTENCY_TITLE = "Figure lettering uses one typeface across the figures"
# A second typeface counts as mixed in when it sets at least this share of a
# figure's lettering and at least MIXED_MIN_CHARS characters of it.
MIXED_SHARE = 0.1
MIXED_MIN_CHARS = 10
# A figure needs this many characters of lettering for its typeface to count.
MIN_CHARS_FOR_FONT = 5


def _family_key(family: str) -> str:
    """One name for faces the guidelines treat as the same (Arial and Helvetica)."""
    from .layout_check import _EQUIVALENT

    for group in _EQUIVALENT:
        if family in group:
            return "/".join(sorted(group))
    return family


def _families(weights: Counter) -> Counter:
    """Characters per typeface (``_family_key``), symbol and math fonts left out."""
    from .layout_check import font_family

    out: Counter = Counter()
    for name, count in weights.items():
        family, generic = font_family(name)
        if family == "Symbol" or generic == "symbol":
            continue
        out[_family_key(family)] += count
    return out


def _body_family(venue: Venue, values: dict[str, str]) -> str | None:
    """The manuscript's main body-text typeface, if a manuscript can be measured."""
    from .layout_check import _measure_all

    try:
        measured = _measure_all(venue, values)
    except Exception as exc:  # noqa: BLE001 -- the comparison is only a note
        logger.debug("could not measure the manuscript's body font: %s", exc)
        return None
    for _, m in measured:
        if m.pdf is not None and m.pdf.body_fonts:
            weights = m.pdf.body_fonts
        elif m.docx is not None and m.docx.fonts:
            weights = m.docx.fonts
        elif m.tex is not None and m.tex.font:
            weights = Counter({m.tex.font: 1})
        else:
            continue
        families = _families(weights)
        if families:
            return families.most_common(1)[0][0]
    return None


def _font_consistency_check(checks: Checks, venue: Venue, values: dict[str, str], inv: _Inventory, rules: FigureRules, where: str) -> Outcome:
    """Each figure set in one typeface, and every figure in the same one.

    Symbol and math fonts are left out, and Arial and Helvetica count as one.
    The manuscript's body font is reported alongside but not flagged: many
    venues want sans-serif figures with serif text.
    """
    issues: list[Issue] = []
    main_of: dict[str, list[str]] = {}  # typeface -> figures set mainly in it
    for fig in inv.figures:
        if fig.kind != "vector" or not fig.texts:
            continue
        weights: Counter = Counter()
        for t in fig.texts:
            weights[t.font] += len(t.text.strip())
        families = _families(weights)
        total = sum(families.values())
        if total < MIN_CHARS_FOR_FONT:
            continue
        (main, _), *others = families.most_common()
        main_of.setdefault(main, []).append(fig.name)
        mixed = [(family, count) for family, count in others if count >= MIXED_MIN_CHARS and count / total >= MIXED_SHARE]
        if mixed:
            parts = ", ".join(f"{family} ({100 * count / total:.0f}%)" for family, count in mixed)
            issues.append(Issue(WARNING, fig.field_id, f"{fig.name} mixes typefaces: {main} ({100 * families[main] / total:.0f}% of its lettering) and {parts}; set all of a figure's lettering in one typeface"))
    if len(main_of) > 1:
        groups = sorted(main_of.items(), key=lambda kv: -len(kv[1]))
        listed = "; ".join(f"{family}: {', '.join(names[:4])}{' and more' if len(names) > 4 else ''}" for family, names in groups)
        issues.append(Issue(WARNING, "", f"the figures are set in different typefaces ({listed}); use one typeface for the lettering of every figure"))
    queued = _raster_ai_task(checks, inv, rules)
    if not main_of:
        raise Unchecked(f"no vector lettering whose font can be read in the {where}" + (f"; {queued} raster figure(s) are queued for the AI check" if queued else ""))
    detail = f"figure typeface{'s' if len(main_of) > 1 else ''}: {', '.join(sorted(main_of))}"
    body = _body_family(venue, values)
    if body:
        same = body in main_of and len(main_of) == 1
        detail += f"; body text in {body}" + ("" if same else " (a different typeface from the figures, which most venues allow)")
    if queued:
        detail += f"; {queued} raster figure(s) need the AI check"
    return Outcome(issues, detail)


# --- panel labels ---------------------------------------------------------------------

def cited_panels(text: str) -> dict[int, set[str]]:
    """Panels the text cites, per figure number: "Fig. 2a-c, 3d" -> {2: {a, b, c}, 3: {d}}."""
    from .citation_check import panel_mentions

    out: dict[int, set[str]] = {}
    for c in panel_mentions(text, "figure"):
        out.setdefault(c.number, set()).add(c.letter)
    return out


def _label_run(letters: set[str]) -> list[str]:
    """The panel letters a, b, c... with no gap, so a stray axis letter ('x') is left out."""
    have = {c.lower() for c in letters}
    run: list[str] = []
    while chr(ord("a") + len(run)) in have:
        run.append(chr(ord("a") + len(run)))
    return run


def _labels(fig: Figure) -> list[Text]:
    """Single-letter text items that look like panel labels (as large as the lettering, or bold)."""
    if not fig.texts:
        return []
    median = statistics.median(t.size for t in fig.texts)
    candidates = [t for t in fig.texts if _PANEL.match(t.text.strip()) and (t.size >= median - 0.1 or t.bold)]
    letters = sorted({_PANEL.match(t.text.strip()).group(1).lower() for t in candidates})
    # Labels run a, b, c...: keep them only when they start at 'a' and there are two or more.
    if len(letters) < 2 or letters[0] != "a":
        return []
    # A letter past the run (an axis's 'N') is a label only if styled like panel a.
    letter = lambda t: _PANEL.match(t.text.strip()).group(1)  # noqa: E731
    run = set(_label_run(set(letters)))
    anchor = max((t for t in candidates if letter(t).lower() == "a"), key=lambda t: (t.bold, t.size))
    return [t for t in candidates if letter(t).lower() in run or (letter(t).isupper() == letter(anchor).isupper() and t.bold == anchor.bold)]


def _wanted_case(rule: str) -> str | None:
    lower = rule.lower()
    if re.search(r"upper|capital", lower) or re.search(r"\(A, B|\bA/B/C\b|\bA, B, C\b|\(A\),\s*\(B\)", rule):
        return "upper"
    if re.search(r"lower", lower) or re.search(r"\(a, b|\ba, b, c\b|\ba\), b\)|\(a\),\s*\(b\)", rule):
        return "lower"
    return None


def _lettering_check(checks: Checks, venue: Venue, values: dict[str, str], inv: _Inventory, rules: FigureRules) -> Outcome:
    from .requirements_check import manuscript_files

    rule = rules.multipanel_labels or ""
    case = _wanted_case(rule)
    bold = "bold" in rule.lower()
    issues: list[Issue] = []
    details: list[str] = []
    # 1. How the text cites panels.
    cites: dict[int, set[str]] = {}
    for fld, path in manuscript_files(venue, values):
        text = document.get(path).text
        if text:
            for number, letters in cited_panels(text).items():
                cites.setdefault(number, set()).update(letters)
    all_cited = {c for letters in cites.values() for c in letters}
    if case and all_cited:
        wrong = sorted(c for c in all_cited if (c.isupper() if case == "lower" else c.islower()))
        if wrong and len(wrong) * 2 >= len(all_cited):
            issues.append(Issue(WARNING, "", f"the text cites figure panels in {'upper' if case == 'lower' else 'lower'} case (e.g. Fig. {min(cites)}{wrong[0]}); the author guidelines label panels {rule}"))
    if cites:
        details.append(f"text cites panels of {len(cites)} figure(s)")
    # 2. The labels drawn in vector figures.
    labelled = 0
    for fig in inv.figures:
        if fig.kind != "vector":
            continue
        labels = _labels(fig)
        if not labels:
            if not fig.texts:
                if len(cites.get(fig.number, set())) >= 2:
                    issues.append(Issue(WARNING, fig.field_id, f"{fig.name}: no text to read for its panel labels (it may be outlined to shapes); check them by hand", unchecked=True))
            elif fig.number is not None and len(cites.get(fig.number, set())) >= 2:
                issues.append(Issue(WARNING, fig.field_id, f"{fig.name}: the text cites panels {', '.join(sorted(cites[fig.number]))} of Figure {fig.number}, but no panel labels were found in the figure"))
            continue
        labelled += 1
        letters = {_PANEL.match(t.text.strip()).group(1) for t in labels}
        if case and any((c.isupper() if case == "lower" else c.islower()) for c in letters):
            issues.append(Issue(WARNING, fig.field_id, f"{fig.name}: panels are labelled {', '.join(sorted(letters))}; the author guidelines label panels {rule}"))
        if bold and not all(t.bold for t in labels):
            issues.append(Issue(WARNING, fig.field_id, f"{fig.name}: some panel labels are not bold; the author guidelines ask for {rule}"))
        if fig.number is not None and fig.number in cites:
            have = {c.lower() for c in letters}
            missing = sorted(c for c in cites[fig.number] if c.lower() not in have)
            if missing:
                issues.append(Issue(WARNING, fig.field_id, f"{fig.name}: the text cites panel(s) {', '.join(missing)} of Figure {fig.number}, which the figure does not label (it has {', '.join(sorted(have))})"))
            cited = {c.lower() for c in cites[fig.number]}
            uncited = [c for c in _label_run(letters) if c not in cited]
            if uncited:
                issues.append(Issue(WARNING, fig.field_id, f"{fig.name}: panel(s) {', '.join(uncited)} of Figure {fig.number} {'is' if len(uncited) == 1 else 'are'} never cited in the text (it cites {', '.join(sorted(cited))}); cite every panel, e.g. Fig. {fig.number}{uncited[0]}"))
    if labelled:
        details.append(f"panel labels found in {labelled} vector figure(s)")
    queued = _raster_ai_task(checks, inv, rules)
    if queued:
        details.append(f"{queued} raster figure(s) need the AI check")
    _raster_panels_task(checks, inv, cites)
    if not cites and not labelled and not issues:
        raise Unchecked("no panel citations in the text and no panel labels in vector figures to check" + (f"; {queued} raster figure(s) are queued for the AI check" if queued else ""))
    return Outcome(issues, "; ".join(details))


# --- attached raster files: colour mode and print size ------------------------------------


COLORBLIND_TITLE = "Figure colours distinguishable with red-green colour blindness"


def _figure_drawings(venue: Venue, values: dict[str, str], inv: _Inventory, unread: list[str] | None = None) -> list:
    """The figures' colours, as :class:`~paperpush.colorblind.Drawing` items.

    The names of figures whose colours could not be read are added to ``unread``.
    Read once per inventory: the colour checks (colour blindness, gray printing,
    default colors) share the result.
    """
    if inv.drawings is None:
        missed: list[str] = []
        inv.drawings = (_read_figure_drawings(venue, values, inv, missed), missed)
    drawings, missed = inv.drawings
    if unread is not None:
        unread.extend(missed)
    return list(drawings)


def _read_figure_drawings(venue: Venue, values: dict[str, str], inv: _Inventory, unread: list[str]) -> list:
    from . import colorblind
    from .requirements_check import manuscript_files

    drawings: list[colorblind.Drawing] = []

    def add(make, *args) -> None:
        try:
            drawings.extend(make(*args))
        except Exception:  # noqa: BLE001 -- one unreadable figure does not stop the rest
            logger.debug("could not read the colours of %s", args[1], exc_info=True)
            unread.append(str(args[1]))

    def raster(path: Path, name: str, field_id: str) -> list:
        return [colorblind.Drawing(name, colorblind.image_file_colours(path), raster=True, field_id=field_id)]

    def pdf(path: Path, name: str, field_id: str, embedded: bool) -> list:
        found = colorblind.manuscript_drawings(path, name) if embedded else colorblind.pdf_figure_drawings(path, name)
        for d in found:
            d.field_id = field_id
        return found

    def svg(path: Path, name: str, field_id: str) -> list:
        return [colorblind.Drawing(name, colorblind.svg_colours(path.read_text("utf-8", "replace")), field_id=field_id)]

    if inv.attached:
        for fig in inv.figures:
            suffix = fig.path.suffix.lower()
            if suffix in RASTER:
                add(raster, fig.path, fig.name, fig.field_id)
            elif suffix == ".pdf":
                add(pdf, fig.path, fig.name, fig.field_id, False)
            elif suffix == ".svg":
                add(svg, fig.path, fig.name, fig.field_id)
            elif fig.image_path is not None:  # a picture inside a Word file of figures
                add(raster, fig.image_path, fig.name, fig.field_id)
            else:  # a format with no colours to read here (EPS, EMF, ...)
                unread.append(fig.name)
        return drawings
    for fld, path in manuscript_files(venue, values):
        doc = document.get(path)
        if doc.is_docx:
            for fig in inv.figures:
                if fig.path == path and fig.image_path is not None:
                    add(raster, fig.image_path, fig.name, fld.id)
                elif fig.path == path:
                    unread.append(fig.name)
        elif doc.pdf is not None:
            add(pdf, doc.pdf, path.name, fld.id, True)
    return drawings


def _colorblind_check(venue: Venue, values: dict[str, str], inv: _Inventory) -> Outcome:
    unread: list[str] = []
    drawings = _figure_drawings(venue, values, inv, unread)
    checked = [d for d in drawings if d.colours is not None]
    photos = len(drawings) - len(checked)
    could_not = f"could not read the colours of {', '.join(unread[:4])}"
    if not drawings:
        if unread:
            raise Unchecked(could_not)
        raise NotApplicable(inv.why_empty or "no figures found")
    if not checked:
        if unread:
            raise Unchecked(could_not)
        if photos:
            raise NotApplicable(f"the {photos} figure image(s) are photographs or micrographs, not charts, so their colours were not compared")
        raise Unchecked("could not read the colours of the figures")
    issues: list[Issue] = []
    if unread:
        issues.append(Issue(WARNING, "", could_not, unchecked=True))
    for d in checked:
        pairs = d.confusions()
        if not pairs:
            continue
        worst = pairs[0]
        listed = "; ".join(c.describe() for c in pairs)
        issues.append(
            Issue(
                WARNING,
                d.field_id,
                f"{d.name}: {listed} {'look' if len(pairs) == 1 else 'are pairs that look'} alike with red-green colour blindness "
                f"(simulated {worst.deficiency}: colour difference {worst.simulated:.0f}, against {worst.normal:.0f} with normal vision). "
                "Use a colour-blind-safe palette (such as Okabe-Ito or viridis), or tell them apart by lightness, marker, line style or direct labels as well",
            )
        )
    detail = f"{len(checked)} figure(s) checked" + (f"; {photos} photographic image(s) not compared" if photos else "")
    return Outcome(issues, detail)


def _color_mode_check(inv: _Inventory, rules: FigureRules) -> list[Issue]:
    allowed = {m.strip().upper() for m in rules.color_mode or []}
    issues = []
    rasters = [f for f in inv.figures if f.kind == "raster" and f.mode]
    if not rasters:
        raise NotApplicable("no raster figure files to read a colour mode from")
    for fig in rasters:
        mode = {"L": "GRAYSCALE", "LA": "GRAYSCALE", "1": "GRAYSCALE", "I": "GRAYSCALE", "F": "GRAYSCALE", "I;16": "GRAYSCALE", "I;16B": "GRAYSCALE", "I;16L": "GRAYSCALE", "I;16N": "GRAYSCALE", "P": "RGB", "RGB": "RGB", "RGBA": "RGB", "CMYK": "CMYK"}.get(fig.mode, fig.mode.upper())
        if mode not in allowed and not (mode == "GRAYSCALE" and {"RGB", "CMYK"} & allowed):
            issues.append(Issue(WARNING, fig.field_id, f"{fig.name} is {mode.lower()}; the author guidelines accept {', '.join(rules.color_mode or [])}"))
    return issues


def _dimensions_check(inv: _Inventory, rules: FigureRules) -> list[Issue]:
    issues = []
    unknown: list[str] = []
    sized = 0
    for fig in inv.figures:
        if fig.file_width_mm:
            width_mm, height_mm, at = fig.file_width_mm, fig.file_height_mm, ""
        elif fig.kind == "raster" and fig.dpi_from_metadata and fig.dpi:
            width_mm, height_mm, at = fig.width_px / fig.dpi * MM_PER_INCH, fig.height_px / fig.dpi * MM_PER_INCH, f" at {fig.dpi:.0f} dpi"
        else:
            unknown.append(fig.name)
            continue
        sized += 1
        if rules.max_width_mm and width_mm > rules.max_width_mm * 1.02:
            issues.append(Issue(WARNING, fig.field_id, f"{fig.name} is {width_mm:.0f} mm wide{at}; the author guidelines allow at most {rules.max_width_mm:g} mm"))
        if rules.max_height_mm and height_mm and height_mm > rules.max_height_mm * 1.02:
            issues.append(Issue(WARNING, fig.field_id, f"{fig.name} is {height_mm:.0f} mm tall{at}; the author guidelines allow at most {rules.max_height_mm:g} mm"))
    if not sized:
        raise Unchecked("no figure file records its resolution or size, so its print size is unknown")
    if unknown:
        issues.append(Issue(WARNING, "", f"{', '.join(unknown[:4])} record no resolution, so print size is unknown", unchecked=True))
    return issues


# --- placement -----------------------------------------------------------------------------


def _placement_check(venue: Venue, values: dict[str, str], inv: _Inventory, placement: str | None, reqs: ManuscriptRequirements) -> Outcome:
    from .requirements_check import _fields_for, manuscript_files

    if not placement:
        raise NotApplicable("the guidelines say nowhere in particular for figures to go")
    files = manuscript_files(venue, values)
    if not files:
        raise NotApplicable("no manuscript file")
    has_fields = bool(_fields_for(venue, "figures"))
    attached = any(f.attached for f in inv.figures)
    embedded = inv.figures if not inv.attached else embedded_figures(venue, values)
    issues: list[Issue] = []
    if placement == "combined_file":
        return _combined_placement(venue, values, inv, embedded)
    if placement in ("separate", "separate_files"):
        if has_fields and not attached and embedded:
            issues.append(Issue(WARNING, "", "the figures are only embedded in the manuscript; the author guidelines ask for each figure as a separate file"))
        return Outcome(issues, "figures attached as separate files" if attached else "")
    if not embedded:
        doc = document.get(files[0][1])
        if (doc.is_pdf or doc.is_tex) and doc.glyph_pages is None:
            raise Unchecked("finding the figures in the manuscript needs pdfminer.six")
        if attached:
            issues.append(Issue(WARNING, "", f"no figures found in {files[0][1].name}; the author guidelines ask for figures {'in the manuscript file' if placement != 'end' else 'at the end of the manuscript file'} (attaching them separately may not be enough)"))
        return Outcome(issues)
    if placement in ("end", "end_of_manuscript"):
        pages = [f.page for f in embedded if f.page]
        doc = document.get(files[0][1])
        if doc.is_docx:
            refs_at = _docx_references_offset(doc)
            placed = [f.offset for f in embedded if f.offset]
            if refs_at is None:
                issues.append(Issue(WARNING, "", f"could not find the reference list in {files[0][1].name}, so the figures' position was not checked", unchecked=True))
            elif placed and min(placed) < refs_at:
                issues.append(Issue(WARNING, "", f"a figure appears before the references in {files[0][1].name}; the author guidelines ask for figures at the end of the manuscript"))
            return Outcome(issues, f"{len(embedded)} figure(s) in the manuscript")
        refs_page = _references_page(doc)
        if pages and not refs_page:
            issues.append(Issue(WARNING, "", f"could not find the reference list in {files[0][1].name}, so the figures' position was not checked", unchecked=True))
        if pages and refs_page and min(pages) < refs_page:
            issues.append(Issue(WARNING, "", f"a figure appears on page {min(pages)} of {files[0][1].name}, before the references (page {refs_page}); the author guidelines ask for figures at the end of the manuscript"))
        return Outcome(issues, f"{len(embedded)} figure(s) in the manuscript")
    return Outcome(issues, f"{len(embedded)} figure(s) in the manuscript")


COMBINED_FORMATS = (".docx", ".pdf")


def _combined_placement(venue: Venue, values: dict[str, str], inv: _Inventory, embedded: list[Figure]) -> Outcome:
    """``placement: combined_file``: every figure in one document, not one file per figure."""
    from .requirements_check import _fields_for, _paths

    files = [Path(p) for fld in _fields_for(venue, "figures") for p in _paths(fld, values)]
    files = [p for p in files if p.is_file()]
    combined = [p for p in files if p.suffix.lower() in COMBINED_FORMATS]
    ask = "the author guidelines ask for all figures combined into one document (e.g. a Word file), in citation order, each legend after its figure"
    if not files:
        if embedded:
            return Outcome([Issue(WARNING, "", f"the figures are only embedded in the manuscript; {ask}")])
        raise NotApplicable("no figure files attached")
    if len(files) > 1:
        field_id = next((f.field_id for f in inv.figures if f.attached), "")
        return Outcome([Issue(WARNING, field_id, f"{len(files)} figure files attached; {ask}")])
    if not combined:
        field_id = next((f.field_id for f in inv.figures if f.attached), "")
        return Outcome([Issue(WARNING, field_id, f"{files[0].name} is a single image; {ask}")])
    count = sum(1 for f in inv.figures if f.attached and f.path == combined[0])
    return Outcome([], f"{combined[0].name} combines the figures" + (f" ({count} picture(s))" if combined[0].suffix.lower() == ".docx" else ""))


def figure_file_count(path: Path) -> int:
    """How many figures one figure file holds: the pictures in a Word file, else 1."""
    if path.suffix.lower() == ".docx" and path.is_file():
        return max(len(_embedded_docx(document.get(path), "")), 1)
    return 1


def _docx_references_offset(doc: document.Document) -> int | None:
    """Where the references heading's paragraph starts in document.xml, or None."""
    from .manuscript import _REFERENCE_HEADING

    for para in re.finditer(r"<w:p[ >].*?</w:p>", doc.docx_part("word/document.xml") or "", re.DOTALL):
        if _REFERENCE_HEADING.search("".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", para.group(0))).strip()):
            return para.start()
    return None


def _references_page(doc: document.Document) -> int | None:
    from .manuscript import _REFERENCE_HEADING

    for number, text in enumerate(doc.page_texts or [], start=1):
        if _REFERENCE_HEADING.search(text):
            return number
    return None
