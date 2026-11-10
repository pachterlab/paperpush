"""Figure design checks that the guidelines rarely state but readers notice.

Two run without a model, on the colors each figure is drawn in (read as for
the color-blindness check, :mod:`paperpush.colorblind`):

* ``figures.grayscale`` (only for venues that print figures in gray, see
  :func:`prints_in_gray`): pairs of colors that are clearly different in
  color but have nearly the same lightness, so they merge when the figure is
  printed in gray (or photocopied). Only colors that look like categories
  are compared: a figure with more distinct colors than
  :data:`MAX_CATEGORICAL_COLOURS` is a continuous color scale and is skipped.
* ``figures.default_colors``: a figure drawn in a plotting tool's default
  colors (matplotlib, seaborn, ggplot2, Excel, Plotly), a sign that no shared
  style assigns colors by meaning.

The rest need a reader, so they go to one AI task, ``ai.figures.design``:
axis labels with units, tick labels and scale bars, zoomed axis limits, pie
charts and paired bar charts, color meaning that stays the same across
panels and figures, and color backed by a second channel.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from . import colorblind as cb
from .validate import WARNING, Checks, Issue, NotApplicable, Outcome, Unchecked

if TYPE_CHECKING:
    from .database import Venue

logger = logging.getLogger(__name__)

FIGURES = "Figures"
GRAYSCALE_TITLE = "Figure colors stay distinguishable when printed in gray"
DEFAULT_COLORS_TITLE = "Figures do not use a plotting tool's default colors"
DESIGN_TITLE = "Figure design: axis labels and units, ticks and scale bars, axis limits, chart type, consistent color meaning"

# Two colors whose CIELAB lightness differs by less than this merge in gray...
MAX_GRAY_DELTA_L = 6.0
# ... when they are at least this far apart (CIEDE2000) in color.
MIN_NORMAL_DELTA_E = cb.MIN_NORMAL_DELTA_E
# More chromatic colors than this in one figure is a continuous color scale (a heatmap).
MAX_CATEGORICAL_COLOURS = 12
# Pairs reported per figure.
MAX_PAIRS = 3
# A figure color within this CIEDE2000 distance of a default color is that color.
VECTOR_MATCH_DELTA_E = 2.5
RASTER_MATCH_DELTA_E = 5.0

# Each tool's default colors: the figure matches when it uses every color of
# one of the listed sets (the first colors its cycle hands out).
DEFAULT_PALETTES: list[tuple[str, list[list[str]]]] = [
    ("matplotlib's default color cycle (tab10)", [["#1f77b4", "#ff7f0e"], ["#1f77b4", "#2ca02c"]]),
    ("seaborn's default palette (deep)", [["#4c72b0", "#dd8452"], ["#4c72b0", "#55a868"]]),
    ("ggplot2's default hue palette", [["#f8766d", "#00bfc4"], ["#f8766d", "#00ba38", "#619cff"], ["#f8766d", "#7cae00", "#00bfc4", "#c77cff"]]),
    ("Excel's default chart colors", [["#4472c4", "#ed7d31"], ["#5b9bd5", "#ed7d31"]]),
    ("Plotly's default color sequence", [["#636efa", "#ef553b"]]),
]


def _rgb(hex_code: str) -> cb.RGB:
    h = hex_code.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


# --- color logic ------------------------------------------------------------------


def categorical_palette(colors: list[tuple[cb.RGB, float]], *, raster: bool = False) -> list[tuple[cb.RGB, float]]:
    """The chromatic colors of a figure, merged, with antialiasing blends dropped for rasters."""
    palette = cb.merge([(rgb, w) for rgb, w in colors if cb._chromatic(rgb)])
    if raster and palette:
        background = max(colors, key=lambda c: c[1])[0]
        palette = cb._drop_blends(palette, [background, (255.0, 255.0, 255.0), (0.0, 0.0, 0.0)])
    return palette


def gray_merges(colors: list[tuple[cb.RGB, float]], *, raster: bool = False) -> list[tuple[cb.RGB, cb.RGB, float, float]] | None:
    """Pairs of colors that differ in color but merge in gray: (first, second, ΔL*, ΔE).

    None when the figure has more than :data:`MAX_CATEGORICAL_COLOURS`
    colors, i.e. a continuous color scale whose colors are not categories.
    Worst first, at most :data:`MAX_PAIRS`.
    """
    palette = categorical_palette(colors, raster=raster)
    if len(palette) > MAX_CATEGORICAL_COLOURS:
        return None
    labs = [cb.to_lab(rgb) for rgb, _ in palette]
    found = []
    for i in range(len(palette)):
        for j in range(i + 1, len(palette)):
            normal = cb.delta_e(labs[i], labs[j])
            dl = abs(labs[i][0] - labs[j][0])
            if normal >= MIN_NORMAL_DELTA_E and dl < MAX_GRAY_DELTA_L:
                found.append((palette[i][0], palette[j][0], dl, normal))
    found.sort(key=lambda p: (p[2], -p[3]))
    return found[:MAX_PAIRS]


def default_palette(colors: list[tuple[cb.RGB, float]], *, raster: bool = False) -> tuple[str, list[str]] | None:
    """The plotting tool whose default colors the figure uses, and those colors, or None."""
    tolerance = RASTER_MATCH_DELTA_E if raster else VECTOR_MATCH_DELTA_E
    labs = [cb.to_lab(rgb) for rgb, _ in colors]

    def present(code: str) -> bool:
        target = cb.to_lab(_rgb(code))
        return any(cb.delta_e(target, lab) < tolerance for lab in labs)

    for name, sets in DEFAULT_PALETTES:
        for codes in sorted(sets, key=len, reverse=True):
            if all(present(c) for c in codes):
                return name, codes
    return None


# --- the checks ----------------------------------------------------------------------


def _describe(rgb: cb.RGB) -> str:
    return f"{cb.colour_name(rgb)} ({cb.hex_code(rgb)})"


def _grayscale_check(drawings: list, unread: list[str], inv) -> Outcome:
    checked = [d for d in drawings if d.colours is not None]
    if not drawings:
        if unread:
            raise Unchecked(f"could not read the colors of {', '.join(unread[:4])}")
        raise NotApplicable(inv.why_empty or "no figures found")
    if not checked:
        if unread:
            raise Unchecked(f"could not read the colors of {', '.join(unread[:4])}")
        raise NotApplicable("the figure images are photographs or micrographs, not charts, so their colors were not compared")
    issues: list[Issue] = []
    if unread:
        issues.append(Issue(WARNING, "", f"could not read the colors of {', '.join(unread[:4])}", unchecked=True))
    scales = 0
    for d in checked:
        pairs = gray_merges(d.colours or [], raster=d.raster)
        if pairs is None:
            scales += 1
            continue
        if not pairs:
            continue
        listed = "; ".join(f"{_describe(a)} and {_describe(b)}" for a, b, _, _ in pairs)
        worst = pairs[0]
        issues.append(
            Issue(
                WARNING,
                d.field_id,
                f"{d.name}: {listed} {'have' if len(pairs) == 1 else 'are pairs with'} nearly the same lightness (difference {worst[2]:.1f} in L*), " "so they merge when the figure is printed in gray. If color is all that tells them apart, add markers, line styles or direct labels, " "or pick colors that also differ in lightness",
            )
        )
    detail = f"{len(checked)} figure(s) checked" + (f"; {scales} with a continuous color scale not compared" if scales else "")
    return Outcome(issues, detail)


def _default_colors_check(drawings: list, unread: list[str], inv) -> Outcome:
    checked = [d for d in drawings if d.colours]
    if not checked:
        if unread:
            raise Unchecked(f"could not read the colors of {', '.join(unread[:4])}")
        raise NotApplicable(inv.why_empty or "no chart colors found in the figures")
    issues: list[Issue] = []
    by_tool: dict[str, tuple[list[str], list[str], str]] = {}  # tool -> (colors, figure names, field id)
    for d in checked:
        match = default_palette(d.colours or [], raster=d.raster)
        if match:
            by_tool.setdefault(match[0], (match[1], [], d.field_id))[1].append(d.name)
    for tool, (codes, names, field_id) in by_tool.items():
        issues.append(
            Issue(
                WARNING,
                field_id,
                f"{', '.join(names[:6])}{' and others' if len(names) > 6 else ''} use{'s' if len(names) == 1 else ''} {tool} ({', '.join(codes)}), the plotting tool's default colors. " "Load one shared style in every figure script and assign colors by meaning (e.g. from the Okabe-Ito palette), so the same thing has the same color in every figure",
            )
        )
    return Outcome(issues, f"{len(checked)} figure(s) checked")


_DESIGN_RULES = """\
- Axis labels: every axis and color bar names the quantity, with its unit in parentheses when it has one ("Time (s)", "Expression (log2 TPM)"). Fail bare "x"/"y", a column name left in ("df$time", "value"), or a unit missing from a physical quantity.
- Ticks and scale bars: plot axes keep their numeric tick labels; an image or micrograph has a scale bar (with its length stated) instead of axes.
- Axis limits: bar charts start at zero; an axis zoomed onto clustered values (accuracies of 0.95-0.98 drawn to fill the panel) fails unless an axis break or the legend makes the zoom plain.
- Chart type: no pie or donut charts (a bar or dot plot shows the same values more accurately); two quantities measured on the same items drawn as two separate bar charts should be one scatter of one against the other.
- Color meaning: each color means one thing across every panel and figure. Fail a color that stands for treatment in one panel and cell type in the next, or a group (e.g. Control) drawn in different colors in different figures.
- Color as the only channel: where color alone tells series or groups apart, a second channel (marker shape, line style, or a direct label) should too, so the figure survives gray printing and color-blind readers."""


def _design_task(checks: Checks, venue: "Venue", values: dict[str, str], inv) -> None:
    """Queue the AI check of the figure design rules a reader has to judge."""
    from .ai_validate import AiTask
    from .requirements_check import manuscript_files

    files: list[str] = []
    if inv.attached:
        lines = [f"- {f.name}" for f in inv.figures]
        files.extend(str(f.image_path or f.path) for f in inv.figures)
        where = "The figures are the attached files below."
    else:
        lines = []
        files.extend(str(p) for _, p in manuscript_files(venue, values))
        files.extend(str(f.image_path) for f in inv.figures if f.image_path)
        where = "The figures are embedded in the manuscript; look at each one."
    files = list(dict.fromkeys(files))
    if not files:
        return
    checks.needs_ai(
        AiTask(
            id="ai.figures.design",
            title=DESIGN_TITLE,
            category=FIGURES,
            instructions=(f"{where} Look at every figure and panel and check these design rules:\n{_DESIGN_RULES}\n" + ("\n".join(lines) + "\n" if lines else "") + "Judge only these rules, not whether the content matches the legend, nor lettering size or typeface; other checks cover those. " "Photographs and micrographs are judged only on the scale bar. Report 'fail' naming each figure and panel, what is wrong, and the fix " "(e.g. 'Figure 2b: y axis labeled \"value\" with no unit; Figure 4: a pie chart of cell-type shares, use a bar chart'), else 'pass'."),
            files=files,
        )
    )


def prints_in_gray(rules) -> bool:
    """Whether the venue's print edition may show the figures in gray.

    Set by ``figures.print_grayscale``, or implied by ``figures.color_charge``:
    where color in print costs extra, figures the authors do not pay for print in gray.
    """
    return bool(getattr(rules, "print_grayscale", None) or rules.color_charge)


def run_design_checks(checks: Checks, venue: "Venue", values: dict[str, str], inv, rules) -> None:
    """Run ``figures.grayscale`` and ``figures.default_colors``, and queue ``ai.figures.design``.

    The figures' colors are read once (cached on ``inv`` by ``_figure_drawings``)
    and shared with the color-blindness check.
    """
    from .figure_check import _figure_drawings
    from .report import NOT_APPLICABLE

    def drawings() -> tuple[list, list[str]]:
        unread: list[str] = []
        return _figure_drawings(venue, values, inv, unread), unread

    if prints_in_gray(rules):
        checks.run("figures.grayscale", GRAYSCALE_TITLE, FIGURES, lambda: _grayscale_check(*drawings(), inv))
    else:
        checks.record("figures.grayscale", GRAYSCALE_TITLE, FIGURES, NOT_APPLICABLE, detail="the venue does not print figures in gray (no print_grayscale or color_charge in its guidelines)")
    checks.run("figures.default_colors", DEFAULT_COLORS_TITLE, FIGURES, lambda: _default_colors_check(*drawings(), inv))
    if inv.figures:
        _design_task(checks, venue, values, inv)
