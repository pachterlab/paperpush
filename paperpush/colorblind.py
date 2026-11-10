"""Find figure colours that readers with red-green colour blindness cannot tell apart.

About one man in twelve has a red-green colour-vision deficiency, and many
venues ask for figures that do not rely on red against green. This module
checks that without a model: it collects the colours a figure is drawn in,
simulates how each looks with protanopia and deuteranopia, and reports pairs
that are clearly different to normal colour vision but nearly the same to a
dichromat.

* **Collecting colours.** A raster figure is reduced to its main colours: the
  pixels are binned at 4 bits per channel, and bins holding at least
  :data:`MIN_PIXEL_SHARE` of the image are averaged and merged into clusters. A
  figure that is not made of a few flat colours (a photograph, a micrograph, a
  rendered scene) is left out, since its colours do not encode categories; see
  :data:`MIN_FLAT_SHARE`. A vector figure (a PDF or SVG) gives its colours
  directly: the fill and stroke colours its drawing instructions set. In a
  manuscript PDF only the drawings and images placed as figures are read, so
  coloured hyperlinks in the text do not count.
* **Simulating.** Each colour is passed through the Machado, Oliveira and
  Fernandes (2009) model of protanopia and deuteranopia at full severity,
  applied in linear RGB.
* **Comparing.** Colours are compared in CIELAB with CIEDE2000. A pair is
  reported when it is at least :data:`MIN_NORMAL_DELTA_E` apart for normal
  vision and under :data:`MAX_SIMULATED_DELTA_E` apart for either dichromat.

Only chromatic colours are compared: greys, near-black and near-white never
lose their difference to colour blindness. Everything here is pure Python
over a handful of colours, so it needs no imaging library beyond Pillow.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Two colours at least this far apart (CIEDE2000) read as different to normal vision...
MIN_NORMAL_DELTA_E = 20.0
# ... and closer than this to a dichromat read as the same.
MAX_SIMULATED_DELTA_E = 7.0
# Colours less saturated than this (CIELAB chroma) are greys, and colour
# blindness does not change how they compare.
MIN_CHROMA = 15.0
# Lightness bounds: near-black and near-white (including antialiasing tints) are left out.
MIN_LIGHTNESS = 10.0
MAX_LIGHTNESS = 92.0
# Raster colours below this share of the pixels are antialiasing, noise or labels.
MIN_PIXEL_SHARE = 0.0005
# A raster whose 64 commonest colours (at 4 bits a channel) cover less than this
# share of its pixels is a photograph or micrograph, not a chart: not checked.
MIN_FLAT_SHARE = 0.7
# Colours within this distance of each other are one colour (CIEDE2000).
MERGE_DELTA_E = 6.0
# At most this many colours of one figure are compared.
MAX_COLOURS = 40
# Pairs reported per figure.
MAX_PAIRS = 3
# Rasters are reduced to at most this many pixels on a side first.
THUMBNAIL_PX = 400

RGB = tuple[float, float, float]  # 0-255 sRGB

# Machado, Oliveira & Fernandes (2009), severity 1.0, for linear RGB.
_SIMULATIONS = {
    "protanopia": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deuteranopia": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
}


# --- colour science ------------------------------------------------------------------


def _to_linear(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _from_linear(c: float) -> float:
    c = min(1.0, max(0.0, c))
    c = 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return c * 255.0


def simulate(rgb: RGB, kind: str) -> RGB:
    """``rgb`` as seen with ``kind`` ("protanopia" or "deuteranopia")."""
    lin = [_to_linear(c) for c in rgb]
    m = _SIMULATIONS[kind]
    return tuple(_from_linear(sum(m[i][j] * lin[j] for j in range(3))) for i in range(3))  # type: ignore[return-value]


def to_lab(rgb: RGB) -> tuple[float, float, float]:
    """CIELAB (D65) of an sRGB colour."""
    r, g, b = (_to_linear(c) for c in rgb)
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(
    lab1: tuple[float, float, float], lab2: tuple[float, float, float]
) -> float:
    """CIEDE2000 colour difference."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    c_bar = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(c_bar**7 / (c_bar**7 + 25**7)))
    a1p, a2p = a1 * (1 + g), a2 * (1 + g)
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0
    dLp, dCp = L2 - L1, c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    else:
        dhp = h2p - h1p - 360 if h2p > h1p else h2p - h1p + 360
    dHp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2))
    l_bar, cp_bar = (L1 + L2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        hp_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hp_bar = (h1p + h2p) / 2
    else:
        hp_bar = (h1p + h2p + 360) / 2 if h1p + h2p < 360 else (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(hp_bar - 30))
        + 0.24 * math.cos(math.radians(2 * hp_bar))
        + 0.32 * math.cos(math.radians(3 * hp_bar + 6))
        - 0.20 * math.cos(math.radians(4 * hp_bar - 63))
    )
    d_theta = 30 * math.exp(-(((hp_bar - 275) / 25) ** 2))
    r_c = 2 * math.sqrt(cp_bar**7 / (cp_bar**7 + 25**7))
    s_l = 1 + 0.015 * (l_bar - 50) ** 2 / math.sqrt(20 + (l_bar - 50) ** 2)
    s_c = 1 + 0.045 * cp_bar
    s_h = 1 + 0.015 * cp_bar * t
    r_t = -math.sin(math.radians(2 * d_theta)) * r_c
    return math.sqrt(
        (dLp / s_l) ** 2
        + (dCp / s_c) ** 2
        + (dHp / s_h) ** 2
        + r_t * (dCp / s_c) * (dHp / s_h)
    )


def colour_name(rgb: RGB) -> str:
    """A plain name for a chromatic colour ("red", "dark green", "light blue", ...)."""
    import colorsys

    h, sat, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    hue = h * 360
    if hue < 15 or hue >= 345:
        name = "red"
    elif hue < 45:
        name = "orange"
    elif hue < 70:
        name = "yellow"
    elif hue < 168:
        name = "green"
    elif hue < 200:
        name = "cyan"
    elif hue < 255:
        name = "blue"
    elif hue < 290:
        name = "purple"
    elif hue < 312:
        name = "magenta"
    else:
        name = "pink"
    if name in ("red", "orange") and v < 0.65 and sat < 0.85:
        return "brown"
    if name == "yellow" and v < 0.8:
        return "olive"
    if name == "red" and sat < 0.5 and v > 0.8:
        return "pink"
    if v < 0.45:
        return "dark " + name
    if sat < 0.45 and v > 0.8:
        return "light " + name
    return name


def hex_code(rgb: RGB) -> str:
    return "#" + "".join(f"{int(round(c)):02x}" for c in rgb)


# --- the comparison ------------------------------------------------------------------


@dataclass(frozen=True)
class Confusion:
    """Two colours of one figure that a dichromat sees as nearly the same."""

    first: RGB
    second: RGB
    deficiency: str  # "protanopia" or "deuteranopia"
    normal: float  # CIEDE2000 for normal vision
    simulated: float  # ... and for the dichromat

    def describe(self) -> str:
        return f"{colour_name(self.first)} ({hex_code(self.first)}) and {colour_name(self.second)} ({hex_code(self.second)})"


def _chromatic(rgb: RGB) -> bool:
    L, a, b = to_lab(rgb)
    return MIN_LIGHTNESS < L < MAX_LIGHTNESS and math.hypot(a, b) >= MIN_CHROMA


def merge(colours: list[tuple[RGB, float]]) -> list[tuple[RGB, float]]:
    """Merge near-identical colours (weighted), keeping the :data:`MAX_COLOURS` commonest."""
    clusters: list[list] = []  # [sum_r, sum_g, sum_b, weight, lab of the seed]
    for rgb, weight in sorted(colours, key=lambda c: -c[1]):
        lab = to_lab(rgb)
        for cl in clusters:
            if delta_e(cl[4], lab) < MERGE_DELTA_E:
                cl[0] += rgb[0] * weight
                cl[1] += rgb[1] * weight
                cl[2] += rgb[2] * weight
                cl[3] += weight
                break
        else:
            clusters.append(
                [rgb[0] * weight, rgb[1] * weight, rgb[2] * weight, weight, lab]
            )
    merged = [
        ((cl[0] / cl[3], cl[1] / cl[3], cl[2] / cl[3]), cl[3])
        for cl in clusters
        if cl[3] > 0
    ]
    return sorted(merged, key=lambda c: -c[1])[:MAX_COLOURS]


# A colour within this distance (sRGB units) of a mix of two others is that mix.
BLEND_TOLERANCE = 14.0


def _is_blend(c: RGB, p: RGB, q: RGB) -> bool:
    """Whether ``c`` is (nearly) a mix of ``p`` and ``q``, as antialiasing makes at their edge."""
    d = [p[i] - q[i] for i in range(3)]
    norm = sum(x * x for x in d)
    if norm < 1:
        return False
    t = sum((c[i] - q[i]) * d[i] for i in range(3)) / norm
    if not 0.02 < t < 0.98:
        return False
    return math.dist(c, [q[i] + t * d[i] for i in range(3)]) <= BLEND_TOLERANCE


def _drop_blends(
    palette: list[tuple[RGB, float]], backgrounds: list[RGB]
) -> list[tuple[RGB, float]]:
    """Leave out the colours that are only a mix of a heavier one with the background or another.

    Antialiasing a red line on white gives pinks, and a red patch next to a
    green one gives browns; they are not colours the author chose.
    """
    kept: list[tuple[RGB, float]] = []
    for rgb, weight in palette:  # heaviest first
        others = [k for k, _ in kept]
        partners = backgrounds + others
        if any(_is_blend(rgb, a, b) for a in others for b in partners if a is not b):
            continue
        kept.append((rgb, weight))
    return kept


def confusions(
    colours: list[tuple[RGB, float]], *, raster: bool = False
) -> list[Confusion]:
    """The pairs of ``colours`` ((rgb, weight) items) a red-green dichromat would confuse.

    ``raster`` colours come from pixels, so the antialiasing blends between
    them and the background are dropped first (see :func:`_drop_blends`).
    Worst first, at most one pair per pair of colour names (so one red line and
    one green line in two shades are reported once), and at most :data:`MAX_PAIRS`.
    """
    palette = merge([(rgb, w) for rgb, w in colours if _chromatic(rgb)])
    if raster and palette:
        background = max(colours, key=lambda c: c[1])[0]
        palette = _drop_blends(
            palette, [background, (255.0, 255.0, 255.0), (0.0, 0.0, 0.0)]
        )
    labs = [to_lab(rgb) for rgb, _ in palette]
    sims = {
        kind: [to_lab(simulate(rgb, kind)) for rgb, _ in palette]
        for kind in _SIMULATIONS
    }
    found: list[Confusion] = []
    for i in range(len(palette)):
        for j in range(i + 1, len(palette)):
            normal = delta_e(labs[i], labs[j])
            if normal < MIN_NORMAL_DELTA_E:
                continue
            kind, simulated = min(
                ((k, delta_e(s[i], s[j])) for k, s in sims.items()),
                key=lambda ks: ks[1],
            )
            if simulated < MAX_SIMULATED_DELTA_E:
                found.append(
                    Confusion(palette[i][0], palette[j][0], kind, normal, simulated)
                )
    found.sort(key=lambda c: (c.simulated, -c.normal))
    out: list[Confusion] = []
    names: set[frozenset[str]] = set()
    for c in found:
        key = frozenset(
            (colour_name(c.first).split()[-1], colour_name(c.second).split()[-1])
        )
        if key in names:
            continue
        names.add(key)
        out.append(c)
        if len(out) == MAX_PAIRS:
            break
    return out


# --- reading a figure's colours ------------------------------------------------------


def image_colours(img) -> list[tuple[RGB, float]] | None:
    """The main colours of a Pillow image as (rgb, share of pixels), or None for a photograph."""
    from PIL import Image

    if img.mode in ("1", "L", "LA", "I", "F") or img.mode.startswith("I;16"):
        return []  # greyscale: nothing for colour blindness to change
    work = img.copy()
    if work.mode in ("RGBA", "LA", "PA") or (
        work.mode == "P" and "transparency" in work.info
    ):
        rgba = work.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        work = Image.alpha_composite(background, rgba)
    work = work.convert("RGB")
    # Nearest-neighbour keeps flat colours flat: resampling would invent blends.
    work.thumbnail((THUMBNAIL_PX, THUMBNAIL_PX), Image.NEAREST)
    flat = getattr(
        work, "get_flattened_data", None
    )  # Pillow >= 12.1; getdata is deprecated there
    pixels = list(flat() if flat is not None else work.getdata())
    if not pixels:
        return []
    bins: dict[int, list[int]] = {}
    for r, g, b in pixels:
        key = (r >> 4) << 8 | (g >> 4) << 4 | (b >> 4)
        acc = bins.get(key)
        if acc is None:
            bins[key] = [r, g, b, 1]
        else:
            acc[0] += r
            acc[1] += g
            acc[2] += b
            acc[3] += 1
    total = len(pixels)
    counts = sorted((acc[3] for acc in bins.values()), reverse=True)
    if sum(counts[:64]) / total < MIN_FLAT_SHARE:
        return None
    floor = max(MIN_PIXEL_SHARE * total, 4)
    return [
        ((acc[0] / acc[3], acc[1] / acc[3], acc[2] / acc[3]), acc[3] / total)
        for acc in bins.values()
        if acc[3] >= floor
    ]


def image_file_colours(path: Path) -> list[tuple[RGB, float]] | None:
    from PIL import Image

    with Image.open(path) as img:
        img.seek(0)
        return image_colours(img)


_SVG_HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_SVG_RGB = re.compile(r"rgba?\(\s*([\d.]+%?)\s*[, ]\s*([\d.]+%?)\s*[, ]\s*([\d.]+%?)")
_SVG_COLOUR_ATTR = re.compile(
    r"(?:fill|stroke|stop-color|color)\s*[:=]\s*[\"']?\s*([^;\"'>]+)", re.IGNORECASE
)


def _svg_component(value: str) -> float:
    return float(value[:-1]) * 2.55 if value.endswith("%") else float(value)


def svg_colours(text: str) -> list[tuple[RGB, float]]:
    """The fill, stroke and gradient-stop colours an SVG sets, each weighted by its uses."""
    from PIL import ImageColor

    counts: dict[RGB, float] = {}
    for m in _SVG_COLOUR_ATTR.finditer(text):
        value = m.group(1).strip()
        rgb: RGB | None = None
        h = _SVG_HEX.match(value)
        if h:
            digits = h.group(1)
            if len(digits) == 3:
                digits = "".join(c * 2 for c in digits)
            rgb = (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))
        else:
            r = _SVG_RGB.match(value)
            if r:
                try:
                    rgb = tuple(min(255.0, _svg_component(v)) for v in r.groups())  # type: ignore[assignment]
                except ValueError:
                    rgb = None
            else:
                # A CSS colour name: "red", "forestgreen".
                name = re.match(r"[a-zA-Z]+", value)
                if name and name.group(0).lower() in ImageColor.colormap:
                    rgb = ImageColor.getrgb(name.group(0).lower())[:3]  # type: ignore[assignment]
        if rgb is not None:
            counts[rgb] = counts.get(rgb, 0) + 1
    return list(counts.items())


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _operation_colour(operands) -> RGB | None:
    """The RGB a colour-setting operator's operands give, or None (grey, a pattern, ...)."""
    values = [_number(v) for v in operands]
    if any(v is None for v in values):
        return None  # a pattern name
    if len(values) == 3:
        return tuple(min(1.0, max(0.0, v)) * 255 for v in values)  # type: ignore[return-value]
    if len(values) == 4:
        c, m, y, k = (min(1.0, max(0.0, v)) for v in values)  # type: ignore[misc]
        return (
            (1 - c) * (1 - k) * 255,
            (1 - m) * (1 - k) * 255,
            (1 - y) * (1 - k) * 255,
        )
    return None


_COLOUR_OPS = {b"rg", b"RG", b"k", b"K", b"sc", b"SC", b"scn", b"SCN"}


def _stream_colours(
    stream, reader, counts: dict[RGB, float], depth: int = 0, seen: set | None = None
) -> None:
    """Add the colours set in a content stream, and the Form XObjects it draws, to ``counts``."""
    from pypdf.generic import ContentStream

    seen = set() if seen is None else seen
    try:
        operations = (
            ContentStream(stream, reader).operations
            if not isinstance(stream, ContentStream)
            else stream.operations
        )
    except Exception:  # noqa: BLE001 -- a stream pypdf cannot parse
        logger.debug("could not parse a content stream", exc_info=True)
        return
    for operands, operator in operations:
        if operator in _COLOUR_OPS:
            rgb = _operation_colour(operands)
            if rgb is not None:
                counts[rgb] = counts.get(rgb, 0) + 1
    if depth >= 4:
        return
    for form in _forms(stream):
        if id(form) not in seen:
            seen.add(id(form))
            _stream_colours(form, reader, counts, depth + 1, seen)


def _xobjects(owner) -> list:
    try:
        resources = owner.get("/Resources")
        resources = resources.get_object() if resources is not None else None
        xobjects = resources.get("/XObject") if resources else None
        xobjects = xobjects.get_object() if xobjects is not None else None
        return [xobjects[name].get_object() for name in xobjects] if xobjects else []
    except Exception:  # noqa: BLE001
        return []


def _forms(owner) -> list:
    return [x for x in _xobjects(owner) if x.get("/Subtype") == "/Form"]


def _form_width(form) -> float:
    try:
        x0, _y0, x1, _y1 = (float(v) for v in form["/BBox"])
        matrix = [float(v) for v in form.get("/Matrix", [1, 0, 0, 1, 0, 0])]
        return abs(x1 - x0) * max(abs(matrix[0]), abs(matrix[1]), 1e-9)
    except Exception:  # noqa: BLE001
        return 0.0


def _page_images(page, min_width_px: int) -> list:
    out = []
    try:
        for image in page.images:
            try:
                img = image.image
            except Exception:  # noqa: BLE001 -- a filter Pillow cannot decode
                continue
            if img is not None and img.width >= min_width_px:
                out.append(img)
    except Exception:  # noqa: BLE001
        logger.debug("could not list the images of a PDF page", exc_info=True)
    return out


@dataclass
class Drawing:
    """The colours of one figure, or one figure-sized part of a PDF page."""

    name: str
    colours: list[tuple[RGB, float]] | None  # None: photographic, not checked
    raster: bool = (
        False  # colours read from pixels (so antialiasing blends are dropped)
    )
    field_id: str = ""

    def confusions(self) -> list[Confusion]:
        return confusions(self.colours or [], raster=self.raster)


def pdf_figure_drawings(path: Path, name: str) -> list[Drawing]:
    """The colours of a figure file in PDF: its vector drawing, and each raster in it."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if not reader.pages:
        return []
    page = reader.pages[0]
    counts: dict[RGB, float] = {}
    contents = page.get_contents()
    if contents is not None:
        _stream_colours(contents, reader, counts)
    for form in _forms(page):
        _stream_colours(form, reader, counts)
    drawings = [Drawing(name, list(counts.items()))] if counts else []
    for n, img in enumerate(_page_images(page, 100), start=1):
        drawings.append(Drawing(f"{name} (image {n})", image_colours(img), raster=True))
    return drawings


# A drawing narrower than this (points) on a manuscript page is a logo or an icon.
MIN_FIGURE_WIDTH_PT = 108.0
# ... and so is a placed image narrower than this many pixels.
MIN_FIGURE_WIDTH_PX = 200
# Manuscript pages read at most.
MAX_PAGES = 120


def manuscript_drawings(path: Path, name: str) -> list[Drawing]:
    """The colours of the figures placed in a manuscript PDF, page by page.

    Only Form XObjects (what ``\\\\includegraphics`` of a PDF, or a placed
    vector drawing, becomes) and images count: the page's own content stream
    holds the text, whose link colours are not figure colours.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    drawings: list[Drawing] = []
    for number, page in enumerate(reader.pages[:MAX_PAGES], start=1):
        k = 0
        for form in _forms(page):
            if _form_width(form) < MIN_FIGURE_WIDTH_PT:
                continue
            counts: dict[RGB, float] = {}
            _stream_colours(form, reader, counts)
            if counts:
                k += 1
                drawings.append(
                    Drawing(f"{name} page {number}, drawing {k}", list(counts.items()))
                )
        for n, img in enumerate(_page_images(page, MIN_FIGURE_WIDTH_PX), start=1):
            drawings.append(
                Drawing(
                    f"{name} page {number}, image {n}", image_colours(img), raster=True
                )
            )
    return drawings
