"""Lettering size of raster figures as placed in the manuscript (figures.text_size).

A raster figure's lettering is pixels; its printed size is estimated from the
height of its letter-shaped components times the points per pixel at which the
manuscript PDF draws the image.
"""

import pytest
from PIL import Image, ImageDraw, ImageFont

from paperpush import figure_check as F
from paperpush import requirements as r

WORDS = ["Accuracy (%)", "Training epoch", "Baseline model", "Held-out split", "Learning rate", "Validation loss", "Number of samples", "Mean score"]


def _text_figure(size_px: int = 40, lines: int = 8, width: int = 1200) -> Image.Image:
    """A white figure with ``lines`` lines of black text set at ``size_px`` pixels."""
    font = ImageFont.load_default(size=size_px)
    img = Image.new("RGB", (width, int(lines * size_px * 1.8) + 40), "white")
    draw = ImageDraw.Draw(img)
    for i in range(lines):
        draw.text((30, 20 + i * size_px * 1.8), WORDS[i % len(WORDS)], fill="black", font=font)
    return img


def test_estimate_scales_with_placed_width():
    img = _text_figure(40)
    # 1200 px printed 120 pt wide: 0.1 pt per pixel, so the 40 px font prints at about 4 pt.
    small = F.raster_lettering_pt(img, 60.0)
    large = F.raster_lettering_pt(img, 480.0)
    assert small and large
    median_small = sorted(small)[len(small) // 2]
    median_large = sorted(large)[len(large) // 2]
    assert 1.4 < median_small < 3.0  # 2 pt font, give or take the estimate
    assert 12 < median_large < 24  # 16 pt font
    assert F._too_small(small, F.GENERAL_MIN_FONT_PT)
    assert not F._too_small(large, F.GENERAL_MIN_FONT_PT)


def test_photo_like_and_sparse_images_are_not_judged():
    import random

    rng = random.Random(0)
    noise = Image.new("L", (400, 300))
    noise.putdata([rng.randrange(256) for _ in range(400 * 300)])
    assert F.raster_lettering_pt(noise, 60.0) is None  # not mostly light: a photo
    assert F.raster_lettering_pt(_text_figure(40, lines=2), 60.0) is None  # too little lettering


def test_dotted_lines_are_not_lettering():
    img = Image.new("L", (800, 600), 255)
    draw = ImageDraw.Draw(img)
    for row in range(10):
        for x in range(20, 780, 30):
            draw.ellipse((x, 30 + row * 55, x + 12, 42 + row * 55), fill=0)
    assert F.raster_lettering_pt(img, 40.0) is None


def test_small_inset_among_readable_labels_is_not_flagged():
    # most lines tiny, but the figure's main labels are well above the floor: a code listing or inset
    sizes = [2.5] * 12 + [7.0] * 6
    assert not F._too_small(sizes, 4)
    assert F._too_small([2.5] * 12 + [4.5] * 2, 4)


class _Checks:
    def needs_ai(self, *a, **k):
        pass


def test_text_size_check_flags_raster_placed_too_small(tmp_path):
    path = tmp_path / "fig3.png"
    _text_figure(40).save(path)
    fig = F._raster_figure(path, "figures", r.FigureRules(), 3)
    # without a known placement nothing is estimated (the venue's print width says nothing about the author's layout)
    with pytest.raises(F.Unchecked):
        F._text_size_check(_Checks(), F._Inventory([fig], True), r.FigureRules(), "x")
    fig.placed_width_mm, fig.placed_page = 60.0 / 72 * F.MM_PER_INCH, 4
    out = F._text_size_check(_Checks(), F._Inventory([fig], True), r.FigureRules(), "x")
    assert len(out.issues) == 1
    message = out.issues[0].message
    assert "fig3.png" in message and "page 4" in message and "under the 4 pt" in message
    fig.placed_width_mm = 480.0 / 72 * F.MM_PER_INCH
    with pytest.raises(F.Unchecked):  # readable: no issue, and raster lettering is still left to the AI check
        F._text_size_check(_Checks(), F._Inventory([fig], True), r.FigureRules(), "x")


def test_place_rasters_tells_same_sized_images_apart(tmp_path, monkeypatch):
    pytest.importorskip("pdfminer")
    pytest.importorskip("pypdf")
    from paperpush import requirements_check

    shrunk = _text_figure(40)
    other = Image.new("RGB", shrunk.size, "white")
    ImageDraw.Draw(other).rectangle((100, 100, 900, 300), fill="black")  # same pixel size, different picture
    out = tmp_path / "manuscript.pdf"
    # Pillow places each page's image at its own resolution: 1200 px at 1200/(width/72) dpi.
    shrunk_dpi = shrunk.width / (100 / 72)
    other_dpi = other.width / (450 / 72)
    shrunk.save(tmp_path / "p1.pdf", resolution=shrunk_dpi)
    other.save(tmp_path / "p2.pdf", resolution=other_dpi)
    import pypdf

    writer = pypdf.PdfWriter()
    for name in ("p2.pdf", "p1.pdf"):
        writer.append(str(tmp_path / name))
    writer.write(str(out))

    fig_path = tmp_path / "shrunk.png"
    shrunk.save(fig_path)
    fig = F._raster_figure(fig_path, "figures", r.FigureRules(), 1)
    monkeypatch.setattr(requirements_check, "manuscript_files", lambda venue, values: [(None, out)])
    inv = F._Inventory([fig], True)
    F._place_rasters(None, {}, inv)
    # two same-sized images: the widest placement is assumed until the pixels are compared
    assert fig.placed_page == 1
    assert fig.placed_width_mm == pytest.approx(450 / 72 * F.MM_PER_INCH, rel=0.02)
    out = F._text_size_check(_Checks(), inv, r.FigureRules(), "x", None, {})
    assert fig.placed_page == 2
    assert fig.placed_width_mm == pytest.approx(100 / 72 * F.MM_PER_INCH, rel=0.02)
    assert len(out.issues) == 1 and "page 2" in out.issues[0].message
