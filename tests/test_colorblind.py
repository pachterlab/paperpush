"""Tests for the red-green colour-blindness check on figures (:mod:`paperpush.colorblind`)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from paperpush import colorblind as cb
from paperpush import report
from paperpush import requirements as r
from paperpush.database import Field, Venue
from paperpush.subfile import SubFile
from paperpush.validate import validate_report
from tests.pdf_builder import write_pdf
from tests.test_validate_report import _write_pdf_with_form

validate_mod = importlib.import_module("paperpush.validate")

MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
FIGURES = Field(id="figure_files", label="Figures", type="filelist")

TAB10 = [
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
]
OKABE_ITO = [
    "#E69F00",
    "#56B4E9",
    "#009E73",
    "#F0E442",
    "#0072B2",
    "#D55E00",
    "#CC79A7",
    "#000000",
]


def _rgb(code: str) -> tuple[float, float, float]:
    code = code.lstrip("#")
    return tuple(float(int(code[i : i + 2], 16)) for i in (0, 2, 4))  # type: ignore[return-value]


def _palette(codes: list[str]) -> list:
    return [(_rgb(c), 1.0) for c in codes]


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    validate_mod._MODELS.clear()
    monkeypatch.setattr(r, "_load_raw", lambda: {"$aliases": {}, "testj": {}})
    yield
    validate_mod._MODELS.clear()


def _report(*fields: Field, **values: str) -> report.ValidationReport:
    venue = Venue(slug="testj", name="Test Venue", fields=list(fields))
    flags = {
        "check_links": False,
        "check_references": False,
        "check_sensitive": False,
        "check_openreview": False,
    }
    return validate_report(SubFile(venue="testj", values=values), venue, **flags)


def _result(rep: report.ValidationReport) -> report.CheckResult:
    return next(res for res in rep.results if res.id == "figures.colorblind")


def _chart(path: Path, colours: list[str]) -> Path:
    """An antialiased line-and-legend chart, saved as a PNG."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1200, 800), "white")
    draw = ImageDraw.Draw(img)
    for k, colour in enumerate(colours):
        draw.line(
            [
                (x, 400 + 150 * ((x // 60 + k) % 5 - 2) + 20 * k)
                for x in range(50, 1150, 40)
            ],
            fill=colour,
            width=6,
        )
        draw.rectangle([900, 60 + 40 * k, 940, 80 + 40 * k], fill=colour)
    img.resize((600, 400), Image.LANCZOS).save(path)
    return path


# --- colour science ---------------------------------------------------------------


def test_delta_e_matches_published_ciede2000_pair():
    # Sharma, Wu & Dalal (2005), test pair 1.
    assert cb.delta_e((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485)) == pytest.approx(
        2.0425, abs=1e-4
    )


def test_okabe_ito_palette_has_no_confusions():
    assert cb.confusions(_palette(OKABE_ITO)) == []


def test_tab10_confusions_are_the_known_problem_pairs():
    found = {
        frozenset((cb.colour_name(c.first), cb.colour_name(c.second)))
        for c in cb.confusions(_palette(TAB10))
    }
    assert frozenset(("orange", "green")) in found
    assert all(
        c.simulated < cb.MAX_SIMULATED_DELTA_E <= cb.MIN_NORMAL_DELTA_E <= c.normal
        for c in cb.confusions(_palette(TAB10))
    )


def test_red_and_green_are_confused_under_deuteranopia():
    (pair,) = cb.confusions(_palette(["#d62728", "#2ca02c"]))
    assert pair.deficiency == "deuteranopia"
    assert pair.describe() == "red (#d62728) and green (#2ca02c)"


def test_greys_and_lightness_only_differences_are_ignored():
    assert cb.confusions(_palette(["#000000", "#777777", "#ffffff"])) == []
    assert cb.confusions(_palette(["#08306b", "#6baed6"])) == []  # dark and light blue


def test_raster_antialiasing_blends_are_not_reported(tmp_path):
    colours = cb.image_file_colours(
        _chart(
            tmp_path / "okabe.png",
            ["#E69F00", "#56B4E9", "#009E73", "#0072B2", "#D55E00"],
        )
    )
    assert colours is not None and cb.confusions(colours, raster=True) == []
    colours = cb.image_file_colours(_chart(tmp_path / "rg.png", ["#d62728", "#2ca02c"]))
    (pair,) = cb.confusions(colours, raster=True)
    assert {cb.colour_name(pair.first), cb.colour_name(pair.second)} == {"red", "green"}


def test_photographs_are_not_compared():
    from PIL import Image

    noise = Image.merge("RGB", [Image.effect_noise((300, 200), 80) for _ in range(3)])
    assert cb.image_colours(noise) is None
    assert cb.image_colours(Image.new("L", (100, 100), 128)) == []


def test_svg_colours_are_read_from_fill_and_stroke():
    svg = '<svg><path fill="#d62728"/><path style="stroke: rgb(44, 160, 44)"/><rect fill="#fff"/></svg>'
    assert sorted(c for c, _ in cb.svg_colours(svg)) == [
        (44.0, 160.0, 44.0),
        (214, 39, 40),
        (255, 255, 255),
    ]


# --- the validate check -------------------------------------------------------------


def test_attached_raster_figure_is_flagged(tmp_path):
    fig = _chart(tmp_path / "fig1.png", ["#d62728", "#2ca02c"])
    result = _result(_report(FIGURES, figure_files=str(fig)))
    assert result.status == report.WARNING
    (issue,) = result.issues
    assert issue.field == "figure_files"
    assert (
        issue.message.startswith("fig1.png: ")
        and "red (#" in issue.message
        and "green (#" in issue.message
    )
    assert "deuteranopia" in issue.message and "Okabe-Ito" in issue.message


def test_attached_safe_figure_passes(tmp_path):
    fig = _chart(tmp_path / "fig1.png", ["#E69F00", "#0072B2"])
    result = _result(_report(FIGURES, figure_files=str(fig)))
    assert result.status == report.PASSED and "1 figure(s) checked" in result.detail


def test_attached_vector_pdf_figure_is_flagged(tmp_path):
    ops = "0.839 0.153 0.157 RG 2 w 10 10 m 390 190 l S 0.173 0.627 0.173 rg 10 100 50 50 re f\n"
    fig = write_pdf(tmp_path / "fig1.pdf", [ops], width=400, height=200)
    result = _result(_report(FIGURES, figure_files=str(fig)))
    assert (
        result.status == report.WARNING
        and "red" in result.issues[0].message
        and "green" in result.issues[0].message
    )


def test_manuscript_link_colours_are_not_figure_colours(tmp_path):
    # hyperref's default red links and green citations sit in the page's own
    # content stream, which is text; only the placed drawing counts.
    body = "BT /F1 10 Tf 1 0 0 rg 72 700 Td (a red link) Tj ET BT /F1 10 Tf 0 1 0 rg 72 680 Td (a green citation) Tj ET\n"
    safe_drawing = (
        "0.902 0.624 0 rg 10 10 80 80 re f 0 0.447 0.698 rg 100 10 80 80 re f"
    )
    pdf = tmp_path / "ms.pdf"
    _write_pdf_with_form(pdf, body + "q 1 0 0 1 100 200 cm /Fm1 Do Q\n", safe_drawing)
    result = _result(_report(MANUSCRIPT, manuscript_file=str(pdf)))
    assert result.status == report.PASSED, result.issues


def test_manuscript_drawing_is_flagged(tmp_path):
    drawing = (
        "0.839 0.153 0.157 rg 10 10 80 80 re f 0.173 0.627 0.173 rg 100 10 80 80 re f"
    )
    pdf = tmp_path / "ms.pdf"
    _write_pdf_with_form(pdf, "q 1 0 0 1 100 200 cm /Fm1 Do Q\n", drawing)
    result = _result(_report(MANUSCRIPT, manuscript_file=str(pdf)))
    assert result.status == report.WARNING
    assert result.issues[0].message.startswith("ms.pdf page 1, drawing 1: ")
