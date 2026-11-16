"""Rules added for PLOS ONE's author guidelines, each checked on a small manuscript.

Statements kept out of the manuscript (funding, competing interests), figures
that must not be embedded in it, the caption label ('Fig 1'), footnotes, heading
levels, forbidden fonts, citation style read from a short paper's few
citations, the statement fields a validate-only form gets, and the shape of an
autofill values file. All run offline.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from paperpush import citation_check as C
from paperpush import document
from paperpush import layout_check as L
from paperpush import requirements as r
from paperpush.autofill import parse_extraction
from paperpush.database import Field, Venue, default_form
from paperpush.subfile import SubFile
from paperpush.validate import validate_report

MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
FIGURES = Field(id="figure_files", label="Figures", type="filelist")
ALIASES = {
    "sections": {"Introduction": ["introduction"], "Results": ["results"]},
    "statements": {"funding": ["funding"], "competing_interests": ["competing interests"], "data_availability": ["data availability"]},
}
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
SECT = '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
HEADING_STYLES = "".join(f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/></w:style>' for n in range(1, 5))
DRAWING = '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="5486400" cy="2743200"/><a:graphic><a:graphicData><a:blip r:embed="rId9"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'


def _docx(path: Path, body: str, *, media: dict[str, bytes] | None = None, rels: str = "") -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {W}><w:body>{body}{SECT}</w:body></w:document>')
        z.writestr("word/styles.xml", f'<?xml version="1.0"?><w:styles {W}><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Arial"/><w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults>{HEADING_STYLES}</w:styles>')
        if rels:
            z.writestr("word/_rels/document.xml.rels", f'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        for name, data in (media or {}).items():
            z.writestr(name, data)
    return path


def _p(text: str, style: str = "", rpr: str = "") -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{ppr}<w:r>{"<w:rPr>" + rpr + "</w:rPr>" if rpr else ""}<w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


BODY = "".join(
    [
        _p("A short study", "Heading1"),
        _p("Introduction", "Heading1"),
        _p("We cite earlier work [1]. Fig 1 shows the values."),
        _p("Fig 1. The values."),
        _p("Results", "Heading1"),
        _p("The values rose."),
        _p("References", "Heading1"),
        _p("1. Example A. A paper. J Ex. 2024;1:1-3."),
    ]
)


@pytest.fixture
def run(monkeypatch):
    def _run(path: Path, db: dict, *, figures: list[Path] | None = None, fields=(MANUSCRIPT, FIGURES), extra: dict[str, str] | None = None):
        monkeypatch.setattr(r, "_load_raw", lambda: {"$aliases": ALIASES, "testj": db})
        venue = Venue(slug="testj", name="T", fields=list(fields))
        values = {"manuscript_file": str(path), "figure_files": "\n".join(str(f) for f in figures or []), **(extra or {})}
        return validate_report(SubFile(venue="testj", values=values), venue, check_links=False, check_references=False, check_sensitive=False, check_openreview=False)

    return _run


def _messages(rep, check_id: str) -> list[str]:
    res = next(res for res in rep.results if res.id == check_id)
    return [i.message for i in res.issues if not i.unchecked]


# --- statements kept out of the manuscript


def test_funding_section_in_the_manuscript_is_reported(tmp_path, run):
    path = _docx(tmp_path / "m.docx", BODY + _p("Funding", "Heading1") + _p("This work received no funding."))
    rep = run(path, {"statements": {"in_submission_form": ["funding"], "not_in_manuscript": ["funding", "competing_interests"]}})
    found = _messages(rep, "guidelines.statements")
    assert any("'Funding' section" in m and "submission system" in m for m in found)
    assert not any("ompeting" in m for m in found)


def test_no_forbidden_section_passes(tmp_path, run):
    rep = run(_docx(tmp_path / "m.docx", BODY), {"statements": {"not_in_manuscript": ["funding"]}})
    assert _messages(rep, "guidelines.statements") == []


# --- figures embedded in the manuscript


def _picture_docx(path: Path) -> Path:
    pil = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pil.new("RGB", (900, 450), "white").save(buffer, "PNG")
    return _docx(path, BODY + DRAWING, media={"word/media/image1.png": buffer.getvalue()}, rels='<Relationship Id="rId9" Type="x" Target="media/image1.png"/>')


def test_embedded_figure_reported_even_with_separate_files_when_forbidden(tmp_path, run):
    pil = pytest.importorskip("PIL.Image")
    path = _picture_docx(tmp_path / "m.docx")
    figure = tmp_path / "Fig1.tif"
    pil.new("RGB", (900, 450), "white").save(figure, dpi=(300, 300))
    rules = {"figures": {"placement": "separate_files", "embedded_forbidden": True}}
    assert any("embedded in m.docx" in m for m in _messages(run(path, rules, figures=[figure]), "figures.placement"))
    # Without the rule, a copy in the manuscript beside the uploaded files is fine.
    assert _messages(run(path, {"figures": {"placement": "separate_files"}}, figures=[figure]), "figures.placement") == []


# --- caption label


def test_caption_label_problems():
    assert C.caption_label_problems([(1, "Fig"), (2, "Fig")], "Fig", "m.docx") == []
    (one,) = C.caption_label_problems([(1, "Figure")], "Fig", "m.docx")
    assert "starts 'Figure 1'" in one and "'Fig 1'" in one
    (many,) = C.caption_label_problems([(1, "Fig."), (2, "Fig.")], "Fig", "m.docx")
    assert "2 figure captions" in many


def test_figure_caption_label_checked_in_word(tmp_path, run):
    body = BODY.replace("Fig 1. The values.", "Figure 1. The values.")
    found = _messages(run(_docx(tmp_path / "m.docx", body), {"figures": {"caption_label": "Fig"}}), "figures.legends")
    assert any("starts 'Figure 1'" in m for m in found)
    assert _messages(run(_docx(tmp_path / "ok.docx", BODY), {"figures": {"caption_label": "Fig"}}), "figures.legends") == []


# --- footnotes, heading levels, fonts


def test_footnotes_in_word_and_latex(tmp_path, run):
    note = '<w:p><w:r><w:t>Text.</w:t></w:r><w:r><w:footnoteReference w:id="1"/></w:r></w:p>'
    assert any("1 footnote(s)" in m for m in _messages(run(_docx(tmp_path / "m.docx", BODY + note), {"manuscript": {"footnotes_allowed": False}}), "layout.footnotes"))
    assert _messages(run(_docx(tmp_path / "ok.docx", BODY), {"manuscript": {"footnotes_allowed": False}}), "layout.footnotes") == []
    tex = tmp_path / "m.tex"
    tex.write_text("\\documentclass{article}\\begin{document}\\author{A\\thanks{Equal}}Text.\\footnote{A note.} % \\footnote{commented}\n\\end{document}")
    found, _ = L._footnotes(MANUSCRIPT, L._Measured(tex))
    assert len(found) == 1 and "1 \\footnote(s)" in found[0].message


def test_heading_depths(tmp_path):
    body = BODY + _p("Sub", "Heading2") + _p("Subsub", "Heading3") + _p("Deep", "Heading4")
    assert max(L.heading_depths(document.get(_docx(tmp_path / "m.docx", body)))) == 4
    tex = tmp_path / "m.tex"
    tex.write_text("\\begin{document}\\section{A}\\subsection{B}\\subsubsection{C}\\paragraph*{S1 Fig. Supporting figure.}\\end{document}")
    assert max(L.heading_depths(document.get(tex))) == 3
    md = tmp_path / "m.md"
    md.write_text("# Title\n\n## Intro\n\n### Part\n\ntext\n")
    assert L.heading_depths(document.get(md)) == [1, 2]
    plain = _docx(tmp_path / "plain.docx", _p("Introduction") + _p("Text."))
    assert L.heading_depths(document.get(plain)) is None


def test_heading_levels_over_the_limit(tmp_path, run):
    body = BODY + _p("Sub", "Heading2") + _p("Subsub", "Heading3") + _p("Deep", "Heading4")
    assert any("4 heading levels" in m for m in _messages(run(_docx(tmp_path / "m.docx", body), {"manuscript": {"max_heading_levels": 3}}), "layout.heading_levels"))


def test_symbol_font_reported(tmp_path, run):
    sym = '<w:p><w:r><w:t>p </w:t></w:r><w:r><w:sym w:font="Symbol" w:char="F0A3"/></w:r></w:p>'
    found = _messages(run(_docx(tmp_path / "m.docx", BODY + sym), {"manuscript": {"forbidden_fonts": ["Symbol"]}}), "layout.forbidden_fonts")
    assert any("Symbol" in m for m in found)
    run_font = _p("beta", rpr='<w:rFonts w:ascii="Symbol" w:hAnsi="Symbol"/>')
    assert _messages(run(_docx(tmp_path / "r.docx", BODY + run_font), {"manuscript": {"forbidden_fonts": ["Symbol"]}}), "layout.forbidden_fonts")
    assert _messages(run(_docx(tmp_path / "ok.docx", BODY), {"manuscript": {"forbidden_fonts": ["Symbol"]}}), "layout.forbidden_fonts") == []


# --- citation style from few citations


def test_author_year_citation_style_from_a_single_citation(tmp_path, run):
    body = BODY.replace("We cite earlier work [1].", "We cite earlier work (Example, 2024).").replace("1. Example A. A paper.", "Example A. A paper.")
    found = _messages(run(_docx(tmp_path / "m.docx", body), {"references": {"numbered": True}}), "guidelines.reference_style")
    assert any("author-year" in m and "1 citation like" in m for m in found)


# --- the form a validate-only venue gets


def test_plos_one_form_carries_its_declarations():
    fields = {f["id"]: f for f in default_form("plos_one")["fields"]}
    for fid in ("funding_statement", "competing_interests", "data_availability"):
        assert fields[fid]["required"] and fields[fid]["type"] == "textarea"
    assert "keep it out of the manuscript" in fields["funding_statement"]["help"]
    assert "keep it out" not in fields["data_availability"]["help"]


def test_statement_kept_out_of_the_manuscript_is_not_compared_with_it(tmp_path, run):
    statement = Field(id="competing_interests", label="Competing interests", type="textarea")
    funding = Field(id="funding_statement", label="Funding statement", type="textarea")
    db = {"statements": {"in_submission_form": ["funding", "competing_interests"], "not_in_manuscript": ["funding"]}}
    extra = {"funding_statement": "The author(s) received no specific funding for this work at all.", "competing_interests": "The authors have declared that no competing interests exist at all here."}
    rep = run(_docx(tmp_path / "m.docx", BODY), db, fields=(MANUSCRIPT, statement, funding), extra=extra)
    # Funding is kept out of the manuscript and competing interests are collected by the form: neither is reported missing from it.
    assert _messages(rep, "consistency.statements") == []


# --- autofill values file


@pytest.mark.parametrize("data", [{"title": "x"}, ["x"], {"fields": {"title": "x"}}, {"fields": [{"value": "x"}]}])
def test_malformed_values_file_is_refused(data):
    with pytest.raises(ValueError):
        parse_extraction(data)


def test_values_file_in_the_schema_parses():
    out = parse_extraction({"fields": [{"id": "title", "value": "T"}], "unfilled": [{"id": "license"}]})
    assert out.fields[0].id == "title" and out.unfilled == [("license", "")]
