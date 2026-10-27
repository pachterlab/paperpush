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
``max_font_size_pt``), the typeface (``font``), the panel labels
(``multipanel_labels``: their case and weight in the figures, and that each
panel the text cites -- "Fig. 2d" -- exists), the colour mode and print size of
attached raster files, and where the figures are placed (``placement``,
``manuscript.figures_placement``, ``manuscript.single_file``).
"""

from __future__ import annotations

import logging
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
    mode: str = ""
    line_art: bool = False
    texts: list[Text] = field(default_factory=list)
    page: int = 0  # page of the manuscript, for an embedded figure
    number: int | None = None  # figure number, when known
    image_path: Path | None = None  # a viewable image file, for the AI checks


def _print_width_mm(rules: FigureRules) -> float | None:
    return rules.max_width_mm or rules.double_column_width_mm or rules.single_column_width_mm


def _figure_number(path: Path, label: str, index: int) -> int:
    for text in (label, path.stem):
        match = re.search(r"(?:fig(?:ure)?)?[\s_-]*(\d{1,2})(?!\d)", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return index


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
            small.thumbnail((400, 400))
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
    rows: dict[float, list] = {}
    for g in glyphs:
        if g.text.strip():
            rows.setdefault(round(g.y0 * 2) / 2, []).append(g)
    texts: list[Text] = []
    for y, row in rows.items():
        row.sort(key=lambda g: g.x0)
        run = [row[0]]
        for g in row[1:]:
            if g.x0 - run[-1].x1 > 1.5 * max(g.size, run[-1].size):
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
    pages = pdf_layout.read_pages(path, max_pages=1) if pdf_layout.available() else None
    if not pages:
        fig.kind = "other"
        return fig
    page = pages[0]
    width_pt = page.width
    max_mm = _print_width_mm(rules)
    scale = min(1.0, (max_mm / MM_PER_INCH * 72) / width_pt) if max_mm else 1.0
    fig.print_width_mm = width_pt * scale / 72 * MM_PER_INCH
    fig.texts = _texts_from_glyphs(page.glyphs, scale)
    images = [im for im in page.images if im.dpi]
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


def _svg_length(value: str | None) -> float | None:
    """An SVG length in CSS pixels (1/96 in)."""
    if not value:
        return None
    match = re.match(r"\s*(-?\d*\.?\d+)\s*(px|pt|mm|cm|in|pc)?", value)
    if not match:
        return None
    number = float(match.group(1))
    return number * {"px": 1.0, None: 1.0, "pt": 96 / 72, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0, "pc": 16.0}[match.group(2)]


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
    view = [float(v) for v in re.split(r"[\s,]+", root.get("viewBox", "").strip()) if v] if root.get("viewBox") else []
    width_px = _svg_length(root.get("width")) or (view[2] if len(view) == 4 else None)
    if not width_px:
        fig.kind = "other"
        return fig
    user_to_px = width_px / view[2] if len(view) == 4 and view[2] else 1.0
    max_mm = _print_width_mm(rules)
    width_mm = width_px / 96 * MM_PER_INCH
    scale = min(1.0, max_mm / width_mm) if max_mm else 1.0
    fig.print_width_mm = width_mm * scale

    def walk(node, size: float | None, family: str, weight: str) -> None:
        style = dict(item.split(":", 1) for item in (node.get("style") or "").split(";") if ":" in item)
        style = {k.strip(): v.strip() for k, v in style.items()}
        size = _svg_length(style.get("font-size") or node.get("font-size")) or size
        family = style.get("font-family") or node.get("font-family") or family
        weight = style.get("font-weight") or node.get("font-weight") or weight
        tag = node.tag.rsplit("}", 1)[-1]
        if tag in ("text", "tspan") and (node.text or "").strip() and size:
            pt = size * user_to_px * 72 / 96 * scale
            font = family.split(",")[0].strip().strip("'\"") + ("-Bold" if weight in ("bold", "700", "800", "900") else "")
            fig.texts.append(Text(node.text.strip(), pt, font, 0.0, 0.0))
        for child in node:
            walk(child, size, family, weight)

    walk(root, 16.0, "sans-serif", "normal")
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
            if suffix in RASTER:
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
            width_pt = image.x1 - image.x0
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
            if box is None or box[2] - box[0] < MIN_EMBEDDED_WIDTH_PT:
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
        fig = Figure(f"{doc.path.name} picture {n} ({Path(name).name})", doc.path, field_id, False, "raster")
        fig.print_width_mm = width_in * MM_PER_INCH
        try:
            with Image.open(io.BytesIO(data)) as img:
                fig.width_px, fig.height_px, fig.mode = img.width, img.height, img.mode
                fig.dpi = img.width / width_in
                small = img.copy()
                small.thumbnail((400, 400))
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
            checks.record(check_id, title, FIGURES, NOT_APPLICABLE if "no figure files attached and no figures" in inv.why_empty else UNCHECKED, detail=inv.why_empty)
        else:
            checks.run(check_id, title, FIGURES, fn, *args)

    run("figures.dpi", "Figure resolution (dpi)", rules.min_dpi or rules.min_dpi_line_art, "the guidelines set no minimum resolution", _dpi_check, inv, rules, where)
    run("figures.text_size", "Figure lettering size", rules.min_font_size_pt or rules.max_font_size_pt, "the guidelines set no figure lettering size", _text_size_check, checks, inv, rules, where)
    run("figures.font", "Figure lettering typeface", rules.font, "the guidelines name no figure font", _font_check, checks, inv, rules, where)
    run("figures.lettering", "Figure panel labels", True, "", _lettering_check, checks, venue, values, inv, rules)
    run("figures.color_mode", "Figure colour mode", rules.color_mode and inv.attached, "the guidelines set no colour mode (or no figure files are attached)", _color_mode_check, inv, rules)
    run("figures.dimensions", "Figure print size", (rules.max_width_mm or rules.max_height_mm) and inv.attached, "the guidelines set no maximum figure size (or no figure files are attached)", _dimensions_check, inv, rules)
    placement = rules.placement or reqs.manuscript.figures_placement or ("inline" if reqs.manuscript.single_file else None)
    checks.run("figures.placement", "Figures placed where the guidelines ask", FIGURES, _placement_check, venue, values, inv, placement, reqs)


def _dpi_check(inv: _Inventory, rules: FigureRules, where: str) -> Outcome:
    issues: list[Issue] = []
    measured: list[float] = []
    for fig in inv.figures:
        if fig.dpi is None:
            continue
        minimum = rules.min_dpi_line_art if fig.line_art and rules.min_dpi_line_art else rules.min_dpi or rules.min_dpi_line_art
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
            issues.append(Issue(WARNING, fig.field_id, f"{fig.name} is {fig.dpi:.0f} dpi{where_from} ({fig.width_px} px wide); the author guidelines ask for at least {minimum} dpi for {kind}"))
    if not measured:
        raise Unchecked(f"no raster images among the {where}; vector figures have no resolution to check")
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


def _text_size_check(checks: Checks, inv: _Inventory, rules: FigureRules, where: str) -> Outcome:
    issues: list[Issue] = []
    smallest: list[float] = []
    for fig in inv.figures:
        if fig.kind != "vector" or not fig.texts:
            continue
        sizes = [t.size for t in fig.texts]
        smallest.append(min(sizes))
        if rules.min_font_size_pt:
            small = [t for t in fig.texts if t.size < rules.min_font_size_pt - 0.25 and t.text.strip()]
            if small:
                examples = ", ".join(f"'{t.text[:20]}' {t.size:.1f} pt" for t in sorted(small, key=lambda t: t.size)[:3])
                issues.append(Issue(WARNING, fig.field_id, f"{fig.name}: {len(small)} text item(s) print smaller than the {rules.min_font_size_pt:g} pt minimum (e.g. {examples})"))
        if rules.max_font_size_pt:
            # Panel labels are often set larger on purpose; leave single letters out.
            large = [t for t in fig.texts if t.size > rules.max_font_size_pt + 0.25 and not _PANEL.match(t.text.strip())]
            if large:
                examples = ", ".join(f"'{t.text[:20]}' {t.size:.1f} pt" for t in sorted(large, key=lambda t: -t.size)[:3])
                issues.append(Issue(WARNING, fig.field_id, f"{fig.name}: {len(large)} text item(s) print larger than the {rules.max_font_size_pt:g} pt maximum (e.g. {examples})"))
    queued = _raster_ai_task(checks, inv, rules)
    others = [f.name for f in inv.figures if f.kind == "other"]
    if others:
        issues.append(Issue(WARNING, "", f"cannot read the lettering of {', '.join(others[:4])} (not a PDF, SVG, or raster image); check its size by hand", unchecked=True))
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

    families = sorted({font_family(name)[0] for name in used})
    return Outcome(issues, f"fonts used: {', '.join(families[:6])}" + (f"; {queued} raster figure(s) need the AI check" if queued else ""))


# --- panel labels ---------------------------------------------------------------------

_FIG_PANEL_CITE = re.compile(r"\b(?:Fig(?:ure)?s?\.?)\s*(\d{1,2})((?:[a-zA-Z](?:\s*[,–-]\s*(?:\d{1,2})?[a-zA-Z])*))(?![a-zA-Z])")


def cited_panels(text: str) -> dict[int, set[str]]:
    """Panels the text cites, per figure number: "Fig. 2a-c, 3d" -> {2: {a, b, c}, 3: {d}}."""
    out: dict[int, set[str]] = {}
    for match in _FIG_PANEL_CITE.finditer(text):
        before = text[max(0, match.start() - 25) : match.start()].lower()
        if "supplementary" in before or "extended data" in before:
            continue
        number = int(match.group(1))
        letters = re.findall(r"[a-zA-Z]", match.group(2))
        if not letters:
            continue
        expanded: set[str] = set()
        spec = re.sub(r"\s+", "", match.group(2))
        for part in spec.split(","):
            bounds = re.split(r"[–-]", re.sub(r"\d", "", part))
            if len(bounds) == 2 and len(bounds[0]) == 1 and len(bounds[1]) == 1 and bounds[0].lower() <= bounds[1].lower():
                start, end = ord(bounds[0]), ord(bounds[1])
                expanded.update(chr(c) for c in range(start, end + 1))
            else:
                expanded.update(c for c in part if c.isalpha())
        out.setdefault(number, set()).update(expanded)
    return out


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
    return candidates


def _wanted_case(rule: str) -> str | None:
    lower = rule.lower()
    if re.search(r"upper|capital|\(A, B", rule) or re.search(r"\bA/B/C\b|\bA, B, C\b", rule):
        return "upper"
    if re.search(r"lower", lower) or re.search(r"\(a, b|\ba, b, c\b|\ba\), b\)", rule):
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
            if fig.number is not None and len(cites.get(fig.number, set())) >= 2:
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
    if labelled:
        details.append(f"panel labels found in {labelled} vector figure(s)")
    queued = _raster_ai_task(checks, inv, rules)
    if queued:
        details.append(f"{queued} raster figure(s) need the AI check")
    if not cites and not labelled and not issues:
        raise Unchecked("no panel citations in the text and no panel labels in vector figures to check" + (f"; {queued} raster figure(s) are queued for the AI check" if queued else ""))
    return Outcome(issues, "; ".join(details))


# --- attached raster files: colour mode and print size ------------------------------------


def _color_mode_check(inv: _Inventory, rules: FigureRules) -> list[Issue]:
    allowed = {m.strip().upper() for m in rules.color_mode or []}
    issues = []
    rasters = [f for f in inv.figures if f.kind == "raster" and f.mode]
    if not rasters:
        raise NotApplicable("no raster figure files to read a colour mode from")
    for fig in rasters:
        mode = {"L": "GRAYSCALE", "LA": "GRAYSCALE", "1": "GRAYSCALE", "P": "RGB", "RGB": "RGB", "RGBA": "RGB", "CMYK": "CMYK"}.get(fig.mode, fig.mode.upper())
        if mode not in allowed and not (mode == "GRAYSCALE" and {"RGB", "CMYK"} & allowed):
            issues.append(Issue(WARNING, fig.field_id, f"{fig.name} is {mode.lower()}; the author guidelines accept {', '.join(rules.color_mode or [])}"))
    return issues


def _dimensions_check(inv: _Inventory, rules: FigureRules) -> list[Issue]:
    issues = []
    sized = [f for f in inv.figures if f.kind == "raster" and f.dpi_from_metadata and f.dpi]
    if not sized:
        raise Unchecked("no figure file records its resolution, so its print size is unknown")
    for fig in sized:
        width_mm = fig.width_px / fig.dpi * MM_PER_INCH
        height_mm = fig.height_px / fig.dpi * MM_PER_INCH
        if rules.max_width_mm and width_mm > rules.max_width_mm * 1.02:
            issues.append(Issue(WARNING, fig.field_id, f"{fig.name} is {width_mm:.0f} mm wide at {fig.dpi:.0f} dpi; the author guidelines allow at most {rules.max_width_mm:g} mm"))
        if rules.max_height_mm and height_mm > rules.max_height_mm * 1.02:
            issues.append(Issue(WARNING, fig.field_id, f"{fig.name} is {height_mm:.0f} mm tall at {fig.dpi:.0f} dpi; the author guidelines allow at most {rules.max_height_mm:g} mm"))
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
        refs_page = _references_page(doc)
        if pages and refs_page and min(pages) < refs_page:
            issues.append(Issue(WARNING, "", f"a figure appears on page {min(pages)} of {files[0][1].name}, before the references (page {refs_page}); the author guidelines ask for figures at the end of the manuscript"))
        return Outcome(issues, f"{len(embedded)} figure(s) in the manuscript")
    return Outcome(issues, f"{len(embedded)} figure(s) in the manuscript")


def _references_page(doc: document.Document) -> int | None:
    from .manuscript import _REFERENCE_HEADING

    for number, text in enumerate(doc.page_texts or [], start=1):
        if _REFERENCE_HEADING.search(text):
            return number
    return None
