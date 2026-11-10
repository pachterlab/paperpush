"""Regression tests for the page-layout and figure checks of ``paperpush validate``.

Each test reproduces one defect found in review: a false warning, a missed
problem, a crash, or a check that passed without having run. All run offline.
"""

from __future__ import annotations

import io
import random
import zlib
import zipfile
from pathlib import Path

import pytest

from paperpush import colorblind as C
from paperpush import document, report
from paperpush import figure_check as F
from paperpush import layout_check as L
from paperpush import pdf_layout as P
from paperpush import requirements as r
from paperpush.database import Field, Venue
from paperpush.subfile import SubFile
from paperpush.validate import Unchecked, validate_report
from tests.pdf_builder import text_line, write_pdf

pytest.importorskip("pdfminer")
Image = pytest.importorskip("PIL.Image")

MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
FIGURES = Field(id="figure_files", label="Figures", type="filelist")
ALIASES = {"sections": {"Introduction": [], "Results": [], "Methods": []}, "statements": {"competing_interests": ["Competing interests"]}}
WORDS = "measurement sample protein assay cohort signal growth tissue model baseline reader figure pathway enzyme outcome trial patient control response gene".split()
LONG = "This is a long body paragraph that describes the results in detail and has many words in it. " * 6


class _Checks:
    def __init__(self):
        self.tasks = []

    def needs_ai(self, task):
        self.tasks.append(task)


class _Field:
    id = "manuscript_file"


@pytest.fixture
def run(monkeypatch):
    def _run(fields, values, db):
        monkeypatch.setattr(r, "_load_raw", lambda: {"$aliases": ALIASES, "testj": db})
        venue = Venue(slug="testj", name="T", fields=list(fields))
        return validate_report(SubFile(venue="testj", values=values), venue, check_links=False, check_references=False, check_sensitive=False, check_openreview=False)

    return _run


def _result(rep, check_id):
    return next(res for res in rep.results if res.id == check_id)


def _line(rng, words=9):
    return " ".join(rng.choice(WORDS) for _ in range(words))


def _measured(path: Path):
    return L._Measured(path, pdf=L.measure_pdf(P.read_pages(path)))


def _rotated(x, y, text, size=8):
    return f"BT /F1 {size:g} Tf 0 1 -1 0 {x:g} {y:g} Tm ({text}) Tj ET\n"


def _body_page(rng, *, spacing=24.0, lines=24, size=12, extra=""):
    return "".join(text_line(72, 708 - spacing * i, _line(rng), size=size) for i in range(lines)) + extra


def _image_pdf(path: Path, width: int, height: int, cm: str) -> Path:
    data = zlib.compress(bytes([200, 60, 60]) * (width * height))
    content = f"q {cm} cm /Im0 Do Q\n".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>",
        f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
SECT = '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
THEME = '<?xml version="1.0"?><a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:themeElements><a:fontScheme name="x"><a:majorFont><a:latin typeface="Calibri Light"/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/></a:minorFont></a:fontScheme></a:themeElements></a:theme>'


def _docx(path: Path, paragraphs: str, *, styles="", media=None, rels="") -> Path:
    doc = f'<?xml version="1.0"?><w:document {W}><w:body>{paragraphs}{SECT}</w:body></w:document>'
    style_part = f'<?xml version="1.0"?><w:styles {W}><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri"/><w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults>{styles}</w:styles>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", style_part)
        z.writestr("word/theme/theme1.xml", THEME)
        if rels:
            z.writestr("word/_rels/document.xml.rels", f'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        for name, data in (media or {}).items():
            z.writestr(name, data)
    return path


def _para(text, rpr=""):
    return f'<w:p><w:r>{"<w:rPr>" + rpr + "</w:rPr>" if rpr else ""}<w:t>{text}</w:t></w:r></w:p>'


def _svg(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


RULES = r.FigureRules(min_font_size_pt=5, max_width_mm=180, min_dpi=300, min_dpi_line_art=1000)


# --- 1: rotated figure text


def test_rotated_axis_label_has_its_real_size_and_is_one_run(tmp_path):
    path = write_pdf(tmp_path / "f.pdf", [text_line(100, 50, "Time (days)", size=8) + _rotated(50, 100, "Relative abundance", 8)], width=360, height=300)
    fig = F._pdf_figure(path, "fig", RULES, 1)
    assert sorted((t.text, t.size) for t in fig.texts) == [("Relative abundance", 8.0), ("Time (days)", 8.0)]
    assert F._text_size_check(_Checks(), F._Inventory([fig], True), RULES, "x").issues == []
    assert F._labels(fig) == []


# --- 2-4: page and line numbers


def test_page_number_above_the_ten_percent_band_counts(tmp_path):
    rng = random.Random(1)
    pages = [_body_page(rng, spacing=14, lines=30, size=10, extra=text_line(300, 100, str(i + 1), size=10)) for i in range(3)]
    pdf = L.measure_pdf(P.read_pages(write_pdf(tmp_path / "a.pdf", pages)))
    assert pdf.page_number_pages == 3


def test_digit_row_beside_a_text_line_is_not_a_page_number(tmp_path):
    rng = random.Random(2)
    pages = []
    for i in range(3):
        ops = ""
        for n in range(24):
            y = 700 - 24 * n
            ops += text_line(72, y, _line(rng), size=12) + text_line(58, y + 2, str(n + 1), size=7)
        pages.append(ops)
    pdf = L.measure_pdf(P.read_pages(write_pdf(tmp_path / "a.pdf", pages)))
    assert pdf.page_number_pages == 0 and pdf.line_number_pages == 3


def test_running_head_with_a_number_is_a_page_number_not_body(tmp_path):
    rng = random.Random(3)
    pages = [_body_page(rng, extra=text_line(72, 756, "Smith et al.", size=12) + text_line(530, 756, str(i + 1), size=12)) for i in range(3)]
    measured = _measured(write_pdf(tmp_path / "a.pdf", pages))
    assert measured.pdf.page_number_pages == 3
    assert measured.pdf.top > 72
    assert L._margins(_Field(), measured, 25.4)[0] == []


def test_dashed_page_number(tmp_path):
    rng = random.Random(4)
    pages = [_body_page(rng, extra=text_line(300, 30, f"- {i + 1} -", size=12)) for i in range(3)]
    measured = _measured(write_pdf(tmp_path / "a.pdf", pages))
    assert measured.pdf.page_number_pages == 3
    assert measured.pdf.bottom > 72


def test_word_style_line_numbers_close_to_the_text(tmp_path):
    rng = random.Random(5)
    pages, n = [], 0
    for page in range(3):
        ops = ""
        for i in range(24):
            n += 1
            y = 708 - 27.6 * i
            ops += text_line(72, y, _line(rng), size=12) + text_line(54 - 6.67 * len(str(n)), y, str(n), size=12)
        pages.append(ops + text_line(300, 30, str(page + 1), size=12))
    measured = _measured(write_pdf(tmp_path / "a.pdf", pages))
    assert L._line_numbers(_Field(), measured)[0] == []
    assert measured.pdf.left == pytest.approx(72, abs=0.5)


def test_table_index_column_is_not_line_numbers(tmp_path):
    rng = random.Random(6)
    ops = text_line(72, 730, "Table of the groups measured in the study", size=12)
    for i in range(20):
        y = 700 - 20 * i
        ops += text_line(72, y, str(i + 1), size=12) + text_line(100, y, _line(rng), size=12)
    pdf = L.measure_pdf(P.read_pages(write_pdf(tmp_path / "a.pdf", [ops] * 1)))
    assert pdf.line_number_pages == 0


# --- 5: spelling


def test_words_spelled_alike_are_not_variant_markers():
    us, uk, _, _ = L.spelling_counts("The analyses were literal and literally humoral; we analysed the tumor.")
    assert set(uk) == {"analysed"} and set(us) == {"tumor"}


def test_references_do_not_count_toward_the_spelling_variant(tmp_path, run):
    doc = _docx(tmp_path / "m.docx", _para("The colour of the tumour was measured.") + _para("References") + _para("1. Smith J. Tumor color in mice. J Biol 2020."))
    rep = run([MANUSCRIPT], {"manuscript_file": str(doc)}, {"manuscript": {"language": "British English"}})
    assert _result(rep, "layout.language").status == report.PASSED


# --- 6: line art


def test_large_rgb_line_art_is_classed_as_line_art(tmp_path):
    from PIL import ImageDraw

    image = Image.new("RGB", (4000, 2000), "white")
    draw = ImageDraw.Draw(image)
    for i in range(40):
        draw.line((i * 100, 0, 4000 - i * 97, 2000), fill="black", width=6)
    image.save(tmp_path / "l.png", dpi=(400, 400))
    fig = F._raster_figure(tmp_path / "l.png", "fig", RULES, 1)
    assert fig.line_art
    assert F._dpi_check(F._Inventory([fig], True), RULES, "x").issues


# --- 7: placement in Word


def _picture_docx(path: Path, body: str) -> Path:
    buffer = io.BytesIO()
    Image.new("RGB", (2000, 1000), "white").save(buffer, "PNG")
    return _docx(path, body, media={"word/media/image1.png": buffer.getvalue()}, rels='<Relationship Id="rId9" Type="x" Target="media/image1.png"/>')


DRAWING = '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="5486400" cy="2743200"/><a:graphic><a:graphicData><a:blip r:embed="rId9"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'


def test_word_figure_before_references_warns_when_they_go_last(tmp_path, run):
    doc = _picture_docx(tmp_path / "m.docx", _para("Introduction") + _para(LONG) + DRAWING + _para("References") + _para("1. Smith J."))
    res = _result(run([MANUSCRIPT], {"manuscript_file": str(doc)}, {"figures": {"placement": "end_of_manuscript"}}), "figures.placement")
    assert res.status == report.WARNING and "before the references" in res.issues[0].message


def test_word_figure_after_references_passes(tmp_path, run):
    doc = _picture_docx(tmp_path / "m.docx", _para("Introduction") + _para(LONG) + _para("References") + _para("1. Smith J.") + DRAWING)
    res = _result(run([MANUSCRIPT], {"manuscript_file": str(doc)}, {"figures": {"placement": "end_of_manuscript"}}), "figures.placement")
    assert res.status == report.PASSED


# --- 8-9: SVG


@pytest.mark.parametrize(
    "name,body,points",
    [
        ("pct", '<svg xmlns="http://www.w3.org/2000/svg" width="100%" viewBox="0 0 800 600"><text font-size="12">Label</text></svg>', 7.65),  # 800 px is scaled to the 180 mm width
        ("em", '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400"><g font-size="12"><text font-size="0.8em">Label</text></g></svg>', 7.2),
        ("shorthand", '<svg xmlns="http://www.w3.org/2000/svg" width="288pt" viewBox="0 0 288 216"><text style="font: 3px \'DejaVu Sans\'">Tiny</text></svg>', 3.0),
        ("css", '<svg xmlns="http://www.w3.org/2000/svg" width="288pt" viewBox="0 0 288 216"><style>.t{font-size:3px;font-family:Times}</style><text class="t">Tiny</text></svg>', 3.0),
        ("scale", '<svg xmlns="http://www.w3.org/2000/svg" width="288pt" viewBox="0 0 288 216"><g transform="scale(4)"><text font-size="3">Scaled</text></g></svg>', 12.0),
    ],
)
def test_svg_lettering_size(tmp_path, name, body, points):
    fig = F._svg_figure(_svg(tmp_path, f"{name}.svg", body), "fig", r.FigureRules(max_width_mm=180), 1)
    assert fig.kind == "vector" and fig.texts[0].size == pytest.approx(points, abs=0.05)


def test_svg_with_a_doctype_is_read_but_entities_are_refused(tmp_path):
    doctype = '<?xml version="1.0"?><!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd"><svg xmlns="http://www.w3.org/2000/svg" width="288pt" viewBox="0 0 288 216"><text font-size="10" font-family="Arial">Fine</text></svg>'
    fig = F._svg_figure(_svg(tmp_path, "d.svg", doctype), "fig", RULES, 1)
    assert fig.kind == "vector" and fig.texts[0].font == "Arial"
    assert L.safe_xml(b'<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>') is None


def test_malformed_viewbox_does_not_stop_validation(tmp_path, run):
    bad = _svg(tmp_path, "b.svg", '<svg xmlns="http://www.w3.org/2000/svg" width="288pt" viewBox="0 0 288 abc"><text font-size="10">Fine</text></svg>')
    doc = _docx(tmp_path / "m.docx", _para(LONG))
    rep = run([MANUSCRIPT, FIGURES], {"manuscript_file": str(doc), "figure_files": str(bad)}, {"figures": {"min_font_size_pt": 5, "max_width_mm": 180}})
    assert _result(rep, "figures.text_size").status == report.PASSED


# --- 10: legends


def test_word_legend_stops_at_its_paragraph(tmp_path, run):
    doc = _docx(tmp_path / "m.docx", _para("Introduction") + _para("Figure 1. A short caption of the experiment.") + _para(LONG) + _para(LONG) + _para("References") + _para("1. Smith"))
    res = _result(run([MANUSCRIPT], {"manuscript_file": str(doc)}, {"figures": {"legend_max_words": 50}}), "figures.legends")
    assert res.status == report.PASSED, res


# --- 11-12: colour mode, dimensions


@pytest.mark.parametrize("suffix", ["tif", "png"])
def test_sixteen_bit_greyscale_is_greyscale(tmp_path, suffix):
    path = tmp_path / f"g.{suffix}"
    Image.new("I;16", (1000, 800)).save(path, dpi=(600, 600))
    fig = F._raster_figure(path, "fig", RULES, 1)
    assert F._color_mode_check(F._Inventory([fig], True), r.FigureRules(color_mode=["RGB", "grayscale"])) == []


def test_oversized_vector_figure_is_reported(tmp_path):
    rules = r.FigureRules(max_width_mm=180, max_height_mm=240)
    big = F._pdf_figure(write_pdf(tmp_path / "h.pdf", [text_line(100, 50, "Label", size=8)], width=1400, height=1000), "fig", rules, 1)
    issues = F._dimensions_check(F._Inventory([big], True), rules)
    assert "494 mm wide" in issues[0].message and "353 mm tall" in issues[1].message


def test_raster_without_dpi_is_unchecked_beside_a_sized_figure(tmp_path):
    rules = r.FigureRules(max_width_mm=180)
    Image.new("RGB", (600, 600), "white").save(tmp_path / "no.png")
    Image.new("RGB", (600, 600), "white").save(tmp_path / "ok.png", dpi=(300, 300))
    figs = [F._raster_figure(tmp_path / n, "fig", rules, 1) for n in ("no.png", "ok.png")]
    issues = F._dimensions_check(F._Inventory(figs, True), rules)
    assert [i.unchecked for i in issues] == [True] and "no.png" in issues[0].message


# --- 13: colour blindness


def test_named_svg_colours_are_compared(tmp_path, run):
    named = _svg(tmp_path, "n.svg", '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect fill="brown"/><rect fill="forestgreen"/><rect style="fill: orangered"/><rect fill="olive"/></svg>')
    doc = _docx(tmp_path / "m.docx", _para(LONG))
    rep = run([MANUSCRIPT, FIGURES], {"manuscript_file": str(doc), "figure_files": str(named)}, {"figures": {"max_width_mm": 180}})
    assert _result(rep, "figures.colorblind").status == report.WARNING


def test_named_colour_parsing_ignores_keywords():
    assert C.svg_colours('<rect fill="none" stroke="currentColor" color="url(#a)"/>') == []


def test_unreadable_figure_is_unchecked_not_absent(tmp_path, run):
    (tmp_path / "bad.png").write_bytes(b"not an image")
    doc = _docx(tmp_path / "m.docx", _para(LONG))
    rep = run([MANUSCRIPT, FIGURES], {"manuscript_file": str(doc), "figure_files": str(tmp_path / "bad.png")}, {"figures": {"max_width_mm": 180}})
    assert _result(rep, "figures.colorblind").status == report.UNCHECKED


def test_eps_figure_is_unchecked_for_colour(tmp_path):
    eps = tmp_path / "f.eps"
    eps.write_text("%!PS-Adobe-3.0 EPSF-3.0\n")
    inv = F._Inventory([F.Figure("f.eps", eps, "fig", True, "other")], True)
    with pytest.raises(Unchecked):
        F._colorblind_check(Venue(slug="testj", name="T", fields=[]), {}, inv)


# --- 14: Word fonts


def test_theme_font_overrides_a_named_one(tmp_path):
    doc = _docx(tmp_path / "t.docx", _para(LONG, '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:asciiTheme="minorHAnsi" w:hAnsiTheme="minorHAnsi"/>'))
    assert list(L.measure_docx(document.get(doc)).fonts) == ["Calibri"]


def test_character_style_sets_font_and_size(tmp_path):
    style = '<w:style w:type="character" w:styleId="TNR12"><w:rPr><w:rFonts w:ascii="Times New Roman"/><w:sz w:val="24"/></w:rPr></w:style>'
    doc = _docx(tmp_path / "s.docx", _para(LONG, '<w:rStyle w:val="TNR12"/>'), styles=style)
    layout = L.measure_docx(document.get(doc))
    assert list(layout.fonts) == ["Times New Roman"] and list(layout.sizes) == [12.0]


def test_character_style_without_a_font_keeps_the_paragraph_font(tmp_path):
    style = '<w:style w:type="character" w:styleId="Link"><w:rPr><w:b/></w:rPr></w:style>'
    doc = _docx(tmp_path / "s.docx", _para(LONG, '<w:rStyle w:val="Link"/>'), styles=style)
    assert list(L.measure_docx(document.get(doc)).fonts) == ["Calibri"]


# --- 15: LaTeX source


@pytest.mark.parametrize("source", [r"\documentclass[12pt]{article}\renewcommand{\baselinestretch}{2}", r"\documentclass[12pt]{article}\usepackage{setspace}\begin{document}\begin{spacing}{2}x\end{spacing}"])
def test_tex_double_spacing_forms(source):
    assert L.measure_tex(source).double_spacing is True


def test_tex_one_and_a_half_stretch_is_not_double():
    assert L.measure_tex(r"\documentclass{article}\renewcommand{\baselinestretch}{1.2}").double_spacing is False


def test_review_option_numbers_lines_only_in_acmart():
    assert not L.measure_tex(r"\documentclass[review]{elsarticle}").line_numbers
    assert L.measure_tex(r"\documentclass[review]{acmart}").line_numbers


def test_class_that_may_set_spacing_is_not_warned_about():
    measured = L._Measured(Path("m.tex"), tex=L.measure_tex(r"\documentclass[review]{elsarticle}"))
    assert L._spacing(_Field(), measured)[0] is None
    article = L._Measured(Path("m.tex"), tex=L.measure_tex(r"\documentclass{article}"))
    assert L._spacing(_Field(), article)[0]


def test_tex_gobble_drops_page_numbers():
    assert not L.measure_tex(r"\documentclass{article}\pagenumbering{gobble}").page_numbers


def test_tex_helvet_alone_leaves_the_body_font():
    assert L.measure_tex(r"\documentclass{article}\usepackage{helvet}").font == "Computer Modern"
    assert L.measure_tex(r"\documentclass{article}\usepackage{helvet}\renewcommand{\familydefault}{\sfdefault}").font == "Helvetica"


def test_tex_rmdefault_and_palatino():
    assert L.measure_tex(r"\documentclass{article}\renewcommand{\rmdefault}{ptm}").font == "Times"
    assert L.measure_tex(r"\documentclass{article}\usepackage{mathpazo}").font == "Palatino"


# --- beyond the first fifteen


def test_rotated_image_resolution(tmp_path):
    turned = P.read_pages(_image_pdf(tmp_path / "r.pdf", 900, 300, "0 216 -72 0 300 300"))[0].images[0]
    upright = P.read_pages(_image_pdf(tmp_path / "u.pdf", 900, 300, "216 0 0 72 200 300"))[0].images[0]
    assert turned.dpi == pytest.approx(300, abs=1) and upright.dpi == pytest.approx(300, abs=1)
    assert turned.placed_width == pytest.approx(216, abs=1)


@pytest.mark.parametrize("rule,case", [("(A), (B), (C), (D) -- not embedded over any part of the image", "upper"), ("Uppercase bold", "upper"), ("Capital letters", "upper"), ("lettered panel labels, e.g. (A) or (a)", None), ("(a), (b)", "lower")])
def test_wanted_case(rule, case):
    assert F._wanted_case(rule) == case


def test_stray_axis_letter_is_not_a_panel_label(tmp_path):
    text = F.Text
    fig = F.Figure("f.pdf", tmp_path / "f.pdf", "fig", True, "vector", number=1, texts=[text("a", 8, "Arial-Bold", 0, 0), text("b", 8, "Arial-Bold", 0, 0), text("N", 8, "Arial", 0, 0), text("Time", 8, "Arial", 0, 0)])
    assert [t.text for t in F._labels(fig)] == ["a", "b"]


def test_mis_cased_panel_in_the_run_stays_a_label(tmp_path):
    text = F.Text
    fig = F.Figure("f.pdf", tmp_path / "f.pdf", "fig", True, "vector", texts=[text("a", 8, "Arial-Bold", 0, 0), text("B", 8, "Arial", 0, 0)])
    assert [t.text for t in F._labels(fig)] == ["a", "B"]


@pytest.mark.parametrize("stem,label,number", [("2023_results_fig3", "", 3), ("results_v2_fig3", "", 3), ("Figure_S1", "", 9), ("fig3_2024", "", 3), ("2", "", 2), ("x", "Figure 4", 4), ("2023_results", "", 9)])
def test_figure_number(stem, label, number):
    assert F._figure_number(Path(f"{stem}.png"), label, 9) == number


def test_raster_without_dpi_beside_a_measured_one_is_unchecked(tmp_path):
    rules = r.FigureRules(min_dpi=300)
    Image.new("RGB", (600, 600), "white").save(tmp_path / "no.png")
    Image.new("RGB", (600, 600), "white").save(tmp_path / "ok.png", dpi=(400, 400))
    figs = [F._raster_figure(tmp_path / n, "fig", rules, 1) for n in ("no.png", "ok.png")]
    out = F._dpi_check(F._Inventory(figs, True), rules, "x")
    assert [i.unchecked for i in out.issues] == [True] and "no.png" in out.issues[0].message
    with pytest.raises(Unchecked, match="could not tell the resolution"):
        F._dpi_check(F._Inventory(figs[:1], True), rules, "x")


def test_outlined_vector_beside_a_measured_one_is_unchecked(tmp_path):
    outlined = F._pdf_figure(write_pdf(tmp_path / "o.pdf", ["0 0 m 100 100 l S\n"], width=300, height=300), "fig", RULES, 1)
    measured = F._pdf_figure(write_pdf(tmp_path / "m.pdf", [text_line(100, 50, "Hello world", size=8)], width=300, height=300), "fig", RULES, 2)
    out = F._text_size_check(_Checks(), F._Inventory([outlined, measured], True), RULES, "x")
    assert [i.unchecked for i in out.issues] == [True] and "o.pdf" in out.issues[0].message


@pytest.mark.parametrize("font,skip,size,warns", [("Calibri", 20.1, 11, True), ("Calibri", 26.8, 11, False), ("Times New Roman", 27.6, 12, False), ("CMR12", 24.0, 12, False)])
def test_double_spacing_allows_for_tall_fonts(tmp_path, font, skip, size, warns):
    from collections import Counter

    pdf = L.PdfLayout(3, 612, 792, size, Counter({font: 100}), Counter({font: 100}), 72, 72, 72, 72, skip, 0, 0, 3)
    issues, _ = L._spacing(_Field(), L._Measured(Path("m.pdf"), pdf=pdf))
    assert bool(issues) is warns
