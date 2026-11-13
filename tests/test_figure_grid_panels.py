"""Small images in a grid are measured when one is far coarser than its row (dev3 finding)."""
from paperpush.figure_check import _grid_panels
from paperpush.pdf_layout import ImageBox


def _row(y, pxs, w=56.0):
    return [ImageBox(73 + i * 60, y, 73 + i * 60 + w, y + w, px, px, f"im{y}_{i}") for i, px in enumerate(pxs)]


def test_coarse_panel_in_sharp_row_is_measured():
    # panels under half the sharpest's resolution are measured; the 150 dpi floor then
    # passes the 192 px ones (246 dpi) and flags the 102 px one (131 dpi)
    row = _row(204, [192, 192, 512, 512, 102])
    assert _grid_panels(row) == {id(row[0]), id(row[1]), id(row[-1])}


def test_uniformly_coarse_grid_is_left_alone():
    assert _grid_panels(_row(299, [84, 84, 96, 96, 96])) == set()


def test_lone_or_paired_small_images_are_logos():
    assert _grid_panels(_row(100, [512, 40])) == set()


def test_two_by_two_grid_counts():
    imgs = _row(100, [512, 512]) + _row(200, [512, 100])
    assert _grid_panels(imgs) == {id(imgs[-1])}
