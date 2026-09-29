"""Read the positioned glyphs of a PDF: where each character sits, how big it is,
and how it is painted.

pypdf (used elsewhere for plain text) flattens a page to a string; the checks
that care about *appearance* need more. The template check measures the text
block and body font size to spot a squeezed template, and the hidden-text check
looks for characters a human reader cannot see (white fill, invisible render
mode, microscopic size, off the page). Both read the same :class:`Page` list,
parsed once per file by pdfminer.six and cached.

pdfminer.six is optional (``pip install 'paperpush[validate]'``). Without it
:func:`read_pages` returns None, both checks skip PDFs, and ``validate`` says so
once (see :func:`available`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Enough for any main text plus a long appendix; a larger PDF is read only this far.
MAX_PAGES = 80


INSTALL_HINT = "pip install 'paperpush[validate]'"


@lru_cache(maxsize=1)
def available() -> bool:
    """Whether the optional pdfminer.six dependency can be imported."""
    try:
        import pdfminer.pdfinterp  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(frozen=True)
class Glyph:
    """One painted character.

    Coordinates are PDF points with the origin at the page's bottom-left.
    ``size`` is the effective font size (font size scaled by the text and
    transformation matrices). ``fill`` is the non-stroking colour as components
    in ``colorspace`` (``DeviceGray``/``DeviceRGB``/``DeviceCMYK``, or another
    space's name), ``None`` when it is a pattern or unknown. ``render`` is the
    PDF text rendering mode (3 = invisible, 7 = clip only). ``in_figure`` marks
    a glyph drawn inside a Form XObject -- in a LaTeX paper, an included figure.
    """

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    font: str
    fill: tuple[float, ...] | None
    colorspace: str
    render: int
    in_figure: bool


@dataclass(frozen=True)
class Page:
    number: int  # 1-based
    width: float
    height: float
    x0: float  # the page box's lower-left corner (usually 0, 0)
    y0: float
    glyphs: tuple[Glyph, ...]
    # Boxes painted with something other than white -- filled shapes and
    # images -- that light-coloured text can legitimately sit on.
    backgrounds: tuple[tuple[float, float, float, float], ...] = ()

    def on_background(self, glyph: Glyph) -> bool:
        """Whether the glyph's centre lies on a painted (non-white) background."""
        cx, cy = (glyph.x0 + glyph.x1) / 2, (glyph.y0 + glyph.y1) / 2
        return any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in self.backgrounds)


def _fill(graphicstate) -> tuple[float, ...] | None:
    color = getattr(graphicstate, "ncolor", None)
    if color is None:
        return None
    if isinstance(color, (int, float)):
        return (float(color),)
    try:
        return tuple(float(c) for c in color)
    except (TypeError, ValueError):
        return None  # a pattern name, or a colour pdfminer could not resolve


def _read(path: Path, max_pages: int) -> list[Page] | None:
    try:
        from pdfminer.converter import PDFPageAggregator
        from pdfminer.layout import LTChar, LTCurve, LTFigure, LTImage, LTPage
        from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
        from pdfminer.pdfpage import PDFPage
    except ImportError:
        logger.debug("pdfminer.six not installed; skipping glyph-level PDF checks")
        return None

    class _Device(PDFPageAggregator):
        """Aggregator that also records each glyph's text rendering mode."""

        render_mode = 0

        def render_string(self, textstate, seq, ncs, graphicstate):
            self.render_mode = int(getattr(textstate, "render", 0) or 0)
            super().render_string(textstate, seq, ncs, graphicstate)

        def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate):
            advance = super().render_char(matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate)
            item = self.cur_item._objs[-1] if self.cur_item._objs else None
            if isinstance(item, LTChar):
                item.paperpush_render = self.render_mode
            return advance

    def walk(item, in_figure: bool, out: list[Glyph], backgrounds: list) -> None:
        for obj in item:
            if isinstance(obj, LTChar):
                cs = getattr(getattr(obj, "ncs", None), "name", "") or ""
                out.append(
                    Glyph(
                        text=obj.get_text(),
                        x0=obj.x0,
                        y0=obj.y0,
                        x1=obj.x1,
                        y1=obj.y1,
                        size=float(obj.size),
                        font=str(obj.fontname),
                        fill=_fill(obj.graphicstate),
                        colorspace=str(cs),
                        render=getattr(obj, "paperpush_render", 0),
                        in_figure=in_figure,
                    )
                )
            elif isinstance(obj, LTFigure):
                walk(obj, True, out, backgrounds)
            elif isinstance(obj, LTImage):
                backgrounds.append(obj.bbox)
            elif isinstance(obj, LTCurve) and getattr(obj, "fill", False):
                color = obj.non_stroking_color
                fill = (float(color),) if isinstance(color, (int, float)) else tuple(color) if isinstance(color, (tuple, list)) else None
                probe = Glyph("", 0, 0, 0, 0, 0, "", fill, "", 0, False)
                if fill is not None and not is_near_white(probe):
                    backgrounds.append(obj.bbox)

    pages: list[Page] = []
    try:
        with path.open("rb") as fh:
            manager = PDFResourceManager()
            device = _Device(manager, laparams=None)
            interpreter = PDFPageInterpreter(manager, device)
            for number, pdfpage in enumerate(PDFPage.get_pages(fh, maxpages=max_pages), start=1):
                try:
                    interpreter.process_page(pdfpage)
                    layout: LTPage = device.get_result()
                except Exception:
                    logger.debug("could not lay out page %d of %s; skipping it", number, path, exc_info=True)
                    continue
                glyphs: list[Glyph] = []
                backgrounds: list = []
                walk(layout, False, glyphs, backgrounds)
                x0, y0, x1, y1 = layout.bbox
                pages.append(Page(number, x1 - x0, y1 - y0, x0, y0, tuple(glyphs), tuple(backgrounds)))
    except Exception:
        logger.debug("could not read glyphs from %s", path, exc_info=True)
        return None
    return pages


@lru_cache(maxsize=8)
def _cached(path: str, mtime_ns: int, size: int, max_pages: int) -> tuple[Page, ...] | None:
    pages = _read(Path(path), max_pages)
    return None if pages is None else tuple(pages)


def read_pages(path: Path, max_pages: int = MAX_PAGES) -> list[Page] | None:
    """The glyphs of each page of ``path``, or None when it cannot be read.

    Cached on the file's path, size, and modification time, so the template and
    hidden-text checks share one parse.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    pages = _cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size, max_pages)
    return None if pages is None else list(pages)


def is_near_white(glyph: Glyph, threshold: float = 0.95) -> bool:
    """Whether a glyph is filled (near) white, i.e. invisible on a white page."""
    fill = glyph.fill
    if not fill:
        return False
    space = glyph.colorspace.upper()
    if "CMYK" in space or len(fill) == 4:
        return all(c <= 1 - threshold for c in fill)
    if "GRAY" in space or len(fill) == 1:
        return fill[0] >= threshold
    if "RGB" in space or len(fill) == 3:
        return all(c >= threshold for c in fill)
    return False
