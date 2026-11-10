"""Tests for the figure design checks (:mod:`paperpush.figure_design_check`).

``figures.grayscale`` (colors that merge in gray), ``figures.default_colors``
(a plotting tool's default palette), and the ``ai.figures.design`` task. All
run offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paperpush import figure_design_check as D
from paperpush import report
from paperpush.database import Venue
from paperpush.subfile import SubFile
from paperpush.validate import NotApplicable, validate_report
from tests.test_validate_fixes_layout import FIGURES, LONG, MANUSCRIPT, _docx, _para, _result

Image = pytest.importorskip("PIL.Image")

OKABE_ITO = ["#E69F00", "#56B4E9", "#009E73", "#F0E442", "#0072B2", "#D55E00", "#CC79A7"]
VIRIDIS = ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"]


def _colors(*codes: str) -> list:
    return [(D._rgb(c), 1.0) for c in codes]


@pytest.fixture
def run(monkeypatch):
    from paperpush import requirements as r

    def _run(values, db=None):
        monkeypatch.setattr(r, "_load_raw", lambda: {"$aliases": {}, "testj": db or {"figures": {"max_width_mm": 180}}})
        venue = Venue(slug="testj", name="T", fields=[MANUSCRIPT, FIGURES])
        return validate_report(SubFile(venue="testj", values=values), venue, check_links=False, check_references=False, check_sensitive=False, check_openreview=False)

    return _run


def _svg(path: Path, *codes: str) -> Path:
    rects = "".join(f'<rect x="{20 * i}" width="20" height="20" fill="{c}"/>' for i, c in enumerate(codes))
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">{rects}</svg>')
    return path


# --- grayscale ---------------------------------------------------------------------


def test_colors_of_equal_lightness_merge_in_gray():
    pairs = D.gray_merges(_colors(*OKABE_ITO))
    assert pairs and {D.cb.hex_code(pairs[0][0]), D.cb.hex_code(pairs[0][1])} == {"#e69f00", "#56b4e9"}
    assert len(pairs) <= D.MAX_PAIRS


def test_lightness_ramp_passes_grayscale():
    assert D.gray_merges(_colors(*VIRIDIS)) == []


def test_continuous_scale_is_not_compared():
    import colorsys

    wheel = [tuple(255.0 * v for v in colorsys.hsv_to_rgb(i / 20, 0.9, 0.8)) for i in range(20)]
    assert len(D.categorical_palette([(c, 1.0) for c in wheel])) > D.MAX_CATEGORICAL_COLOURS
    assert D.gray_merges([(c, 1.0) for c in wheel]) is None


def test_grays_are_not_compared():
    assert D.gray_merges([((120.0, 120.0, 120.0), 1.0), ((200.0, 60.0, 60.0), 1.0), ((0.0, 0.0, 0.0), 1.0)]) == []


# --- default colors ---------------------------------------------------------------


@pytest.mark.parametrize(
    "codes, tool",
    [
        (["#1f77b4", "#ff7f0e", "#000000"], "matplotlib"),
        (["#4c72b0", "#dd8452"], "seaborn"),
        (["#F8766D", "#00BA38", "#619CFF"], "ggplot2"),
        (["#4472c4", "#ed7d31"], "Excel"),
        (["#636efa", "#ef553b"], "Plotly"),
    ],
)
def test_default_palettes_are_recognised(codes, tool):
    match = D.default_palette(_colors(*codes))
    assert match and match[0].startswith(tool)


def test_one_default_color_alone_is_not_a_match():
    assert D.default_palette(_colors("#1f77b4", "#E69F00")) is None
    assert D.default_palette(_colors(*OKABE_ITO)) is None


def test_raster_default_colors_matched_with_tolerance():
    assert D.default_palette([((40.0, 128.0, 188.0), 0.4), ((252.0, 132.0, 22.0), 0.3)], raster=True)
    assert D.default_palette([((40.0, 128.0, 188.0), 0.4), ((252.0, 132.0, 22.0), 0.3)], raster=False) is None


# --- through validate --------------------------------------------------------------


GRAY_PRINT = {"figures": {"max_width_mm": 180, "print_grayscale": True}}


def test_validate_reports_default_colors(tmp_path, run):
    fig = _svg(tmp_path / "fig1.svg", "#1f77b4", "#ff7f0e", "#2ca02c")
    doc = _docx(tmp_path / "m.docx", _para(LONG))
    rep = run({"manuscript_file": str(doc), "figure_files": str(fig)})
    default = _result(rep, "figures.default_colors")
    assert default.status == report.WARNING and "matplotlib" in default.issues[0].message and "fig1.svg" in default.issues[0].message
    assert _result(rep, "figures.grayscale").status == report.NOT_APPLICABLE  # the venue prints in color


@pytest.mark.parametrize("figures", [GRAY_PRINT["figures"], {"max_width_mm": 180, "color_charge": True}])
def test_validate_flags_gray_merge_for_venues_that_print_in_gray(tmp_path, run, figures):
    merged = _svg(tmp_path / "merge.svg", "#E69F00", "#56B4E9")
    doc = _docx(tmp_path / "m.docx", _para(LONG))
    rep = run({"manuscript_file": str(doc), "figure_files": str(merged)}, {"figures": figures})
    gray = _result(rep, "figures.grayscale")
    assert gray.status == report.WARNING and "merge.svg" in gray.issues[0].message
    assert _result(rep, "figures.default_colors").status == report.PASSED


def test_photograph_is_not_applicable(tmp_path, run):
    import random

    rng = random.Random(1)
    img = Image.new("RGB", (120, 120))
    img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(120 * 120)])
    photo = tmp_path / "micrograph.png"
    img.save(photo)
    doc = _docx(tmp_path / "m.docx", _para(LONG))
    rep = run({"manuscript_file": str(doc), "figure_files": str(photo)}, GRAY_PRINT)
    assert _result(rep, "figures.grayscale").status == report.NOT_APPLICABLE
    assert _result(rep, "figures.default_colors").status == report.NOT_APPLICABLE


def test_gray_check_skipped_for_venues_that_print_in_color(tmp_path, run):
    merged = _svg(tmp_path / "merge.svg", "#E69F00", "#56B4E9")
    doc = _docx(tmp_path / "m.docx", _para(LONG))
    rep = run({"manuscript_file": str(doc), "figure_files": str(merged)}, {"figures": {"max_width_mm": 180, "color_charge": False}})
    gray = _result(rep, "figures.grayscale")
    assert gray.status == report.NOT_APPLICABLE and "gray" in gray.detail


def test_figure_colors_read_once_for_all_color_checks(tmp_path, run, monkeypatch):
    from paperpush import figure_check as F

    calls = []
    real = F._read_figure_drawings
    monkeypatch.setattr(F, "_read_figure_drawings", lambda *a: calls.append(1) or real(*a))
    merged = _svg(tmp_path / "merge.svg", "#1f77b4", "#ff7f0e")
    doc = _docx(tmp_path / "m.docx", _para(LONG))
    rep = run({"manuscript_file": str(doc), "figure_files": str(merged)}, GRAY_PRINT)
    for check in ("figures.colorblind", "figures.grayscale", "figures.default_colors"):
        assert _result(rep, check).status != report.NOT_APPLICABLE
    assert len(calls) == 1


def test_cached_unread_names_reach_every_caller(tmp_path):
    from paperpush import figure_check as F

    eps = tmp_path / "f.eps"
    eps.write_text("%!PS-Adobe-3.0 EPSF-3.0\n")
    inv = F._Inventory([F.Figure("f.eps", eps, "fig", True, "other")], True)
    venue = Venue(slug="testj", name="T", fields=[])
    for _ in range(2):
        unread: list[str] = []
        assert F._figure_drawings(venue, {}, inv, unread) == [] and unread == ["f.eps"]


def test_no_figures_is_not_applicable():
    class Inv:
        why_empty = "no figures found"

    with pytest.raises(NotApplicable):
        D._grayscale_check([], [], Inv())


def test_design_task_queued_for_attached_figures(tmp_path, run):
    fig = _svg(tmp_path / "fig1.svg", "#0072B2", "#E69F00")
    doc = _docx(tmp_path / "m.docx", _para(LONG))
    rep = run({"manuscript_file": str(doc), "figure_files": str(fig)})
    task = next(t for t in rep.ai_tasks if t.id == "ai.figures.design")
    assert str(fig) in task.files
    for rule in ("unit", "scale bar", "pie", "scatter", "zero", "Color meaning", "second channel"):
        assert rule in task.instructions
    assert _result(rep, "ai.figures.design").status == report.NEEDS_AI
