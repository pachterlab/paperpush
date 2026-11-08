"""``paperpush format``: template search, Word and LaTeX reformatting, and the orchestration."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from paperpush import docx_format as dx
from paperpush import formatter, templates
from paperpush import requirements as r
from paperpush import tex_format
from paperpush.database import Field, Venue

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
CT = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'


def _docx(path: Path, body: str, styles: str | None = None, sect: str = '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>', extra: dict[str, str] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CT)
        zf.writestr("word/document.xml", f"<w:document {W}><w:body>{body}{sect}</w:body></w:document>")
        if styles is not None:
            zf.writestr("word/styles.xml", f"<w:styles {W}>{styles}</w:styles>")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return path


def _well_formed(path: Path) -> None:
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.endswith((".xml", ".rels")):
                ET.fromstring(zf.read(name))


# --- element helpers ----------------------------------------------------------


def test_set_child_keeps_schema_order():
    ppr = '<w:pPr><w:pStyle w:val="X"/><w:jc w:val="left"/></w:pPr>'
    out = dx.set_child(ppr, '<w:spacing w:line="480" w:lineRule="auto"/>', dx.PPR_ORDER)
    assert out.index("pStyle") < out.index("spacing") < out.index("jc")
    assert dx.set_child(out, None, dx.PPR_ORDER, tag="jc") == '<w:pPr><w:pStyle w:val="X"/><w:spacing w:line="480" w:lineRule="auto"/></w:pPr>'
    assert dx.set_child("<w:rPr/>", '<w:sz w:val="24"/>', dx.RPR_ORDER) == '<w:rPr><w:sz w:val="24"/></w:rPr>'


def test_with_attrs():
    assert dx.with_attrs('<w:spacing w:after="120"/>', line="480", lineRule="auto") == '<w:spacing w:after="120" w:line="480" w:lineRule="auto"/>'
    assert dx.with_attrs('<w:spacing w:line="240"/>', line=None) == "<w:spacing/>"


# --- Word: rules ----------------------------------------------------------------


def test_word_rules_set_body_text_page_and_numbers(tmp_path):
    styles = '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:asciiTheme="minorHAnsi"/><w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    body = '<w:p><w:pPr><w:spacing w:after="120" w:line="240" w:lineRule="auto"/></w:pPr><w:r><w:rPr><w:rFonts w:ascii="Georgia"/><w:sz w:val="22"/></w:rPr><w:t>Body text.</w:t></w:r><w:r><w:rPr><w:rFonts w:ascii="Symbol"/></w:rPr><w:t>a</w:t></w:r></w:p>'
    src = _docx(tmp_path / "ms.docx", body, styles)
    doc = dx.DocxFile.open(src)
    reqs = r.ManuscriptRequirements(slug="x", manuscript=r.ManuscriptRules(font="Times New Roman", font_size_pt=12, double_spacing=True, line_numbers=True, page_numbers=True, margins_mm=25.4, page_size="A4"))
    done, skipped = dx.apply_rules(doc, reqs)
    out = tmp_path / "out.docx"
    doc.save(out)
    _well_formed(out)
    assert not skipped
    text = " ".join(done)
    for phrase in ("Times New Roman", "12 pt", "double-spaced", "A4", "25.4 mm", "line numbers", "page numbers"):
        assert phrase in text, phrase
    document = doc.text("word/document.xml")
    assert '<w:pgSz w:w="11906" w:h="16838"/>' in document and "<w:lnNumType" in document and "<w:footerReference" in document
    assert 'w:ascii="Georgia"' not in document and 'w:ascii="Symbol"' in document  # symbol fonts are kept
    assert 'w:after="120"' in document and 'w:line="240"' not in document
    styles_xml = doc.text("word/styles.xml")
    assert 'w:ascii="Times New Roman"' in styles_xml and '<w:sz w:val="24"/>' in styles_xml and 'w:line="480"' in styles_xml
    # validate's own measurement agrees.
    from paperpush import document as docmod
    from paperpush.layout_check import measure_docx

    layout = measure_docx(docmod.Document(out))
    assert layout.line_numbers and layout.page_numbers


def test_word_rules_create_styles_when_missing(tmp_path):
    src = _docx(tmp_path / "ms.docx", "<w:p><w:r><w:t>x</w:t></w:r></w:p>")
    doc = dx.DocxFile.open(src)
    done, _ = dx.apply_rules(doc, r.ManuscriptRequirements(slug="x", manuscript=r.ManuscriptRules(double_spacing=True)))
    assert "word/styles.xml" in doc.parts and "body text double-spaced" in done
    doc.save(tmp_path / "out.docx")
    _well_formed(tmp_path / "out.docx")


def test_hand_set_heading_sizes_survive(tmp_path):
    body = "".join(f'<w:p><w:r><w:rPr><w:sz w:val="{sz}"/></w:rPr><w:t>t</w:t></w:r></w:p>' for sz in (22, 22, 22, 36))
    doc = dx.DocxFile.open(_docx(tmp_path / "ms.docx", body, ""))
    dx.strip_direct_formatting(doc, fonts=False, sizes=True, spacing=False)
    assert doc.text("word/document.xml").count("<w:sz ") == 1


@pytest.mark.parametrize("rule, font", [("Times New Roman 12 pt", "Times New Roman"), ("Arial or Helvetica", "Arial"), ("a 12-point serif font", "Times New Roman")])
def test_body_font(rule, font):
    assert dx.body_font(rule) == font


# --- Word: template -------------------------------------------------------------


def test_word_template_styles_page_setup_and_footer(tmp_path):
    t_styles = '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Arial"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
    t_sect = '<w:sectPr><w:footerReference w:type="default" r:id="rId9"/><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr>'
    template = _docx(
        tmp_path / "template.dotx",
        "<w:p/>",
        t_styles,
        t_sect,
        {
            "word/_rels/document.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/></Relationships>',
            "word/footer1.xml": f"<w:ftr {W}><w:p><w:r><w:t>Journal footer</w:t></w:r></w:p></w:ftr>",
        },
    )
    m_styles = '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style><w:style w:type="paragraph" w:styleId="Titre1"><w:name w:val="heading 1"/></w:style><w:style w:type="paragraph" w:styleId="MyQuote"><w:name w:val="My Quote"/></w:style>'
    body = '<w:p><w:pPr><w:pStyle w:val="Titre1"/></w:pPr><w:r><w:t>Intro</w:t></w:r></w:p><w:p><w:pPr><w:pStyle w:val="MyQuote"/></w:pPr><w:r><w:t>q</w:t></w:r></w:p>'
    doc = dx.DocxFile.open(_docx(tmp_path / "ms.docx", body, m_styles))
    done = dx.apply_template(doc, dx.DocxFile.open(template))
    out = tmp_path / "out.docx"
    doc.save(out)
    _well_formed(out)
    document = doc.text("word/document.xml")
    styles = doc.text("word/styles.xml")
    assert '<w:pStyle w:val="Heading1"/>' in document  # matched by name and renamed
    assert 'w:styleId="MyQuote"' in styles and 'w:ascii="Arial"' in styles  # own style kept, template's Normal used
    assert '<w:pgSz w:w="11906" w:h="16838"/>' in document and 'w:top="1134"' in document
    footer = [n for n in doc.parts if n.startswith("word/footer")]
    assert footer and b"Journal footer" in doc.parts[footer[0]]
    assert any("headers and footers" in d for d in done)


# --- template search ------------------------------------------------------------


@pytest.mark.parametrize(
    "url, text, kind",
    [
        ("https://x.org/files/Journal_Word_Template.docx", "Word template", "word"),
        ("https://x.org/kit/author-kit-2027.zip", "LaTeX author kit", "archive"),
        ("https://github.com/acl-org/acl-style-files", "ACL style files", "github"),
        ("https://www.overleaf.com/latex/templates/my-journal/abcd", "Overleaf template", "overleaf"),
        ("https://x.org/journal.cls", "class file", "latex"),
        ("https://cms.example.org/content/123/data/v1", "Download the journal article template package", "archive"),
    ],
)
def test_classify_template_links(url, text, kind):
    link = templates.classify_link(url, text, "page")
    assert link is not None and link.kind == kind


@pytest.mark.parametrize(
    "url, text",
    [
        ("https://x.org/files/conflict_of_interest_form.docx", "COI form template"),
        ("https://x.org/files/reporting-summary.docx", "Reporting summary"),
        ("https://x.org/files/Supplementary_Material.zip", "Supplementary material templates"),
        ("https://x.org/about", "About the journal"),
        ("https://github.com/someone/unrelated", "our code"),
    ],
)
def test_ignore_non_templates(url, text):
    assert templates.classify_link(url, text, "page") is None


def test_find_templates_follows_one_level():
    pages = {
        "https://j.org/guide": '<a href="/latex">LaTeX template instructions</a><a href="/forms/copyright.docx">Copyright form</a>',
        "https://j.org/latex": '<a href="/files/journal-template.zip">Download the LaTeX template (zip)</a>',
    }
    search = templates.find_templates(["https://j.org/guide"], fetch=lambda url, browser=True: (pages.get(url), "" if url in pages else "HTTP 404"))
    assert [link.url for link in search.links] == ["https://j.org/files/journal-template.zip"]
    assert search.pages == ["https://j.org/guide", "https://j.org/latex"]


def test_refused_pages_are_reported():
    search = templates.find_templates(["https://j.org/guide"], fetch=lambda url, browser=True: (None, "a bot check refused the page"))
    assert search.links == [] and "bot check" in search.refused[0]


def test_download_local_latex_zip_picks_matching_sample(tmp_path):
    archive = tmp_path / "kit.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("kit/journal.cls", "\\NeedsTeXFormat{LaTeX2e}")
        zf.writestr("kit/journal-num.bst", "")
        zf.writestr("kit/template-harv.tex", "\\documentclass[authoryear]{journal}")
        zf.writestr("kit/template-num.tex", "\\documentclass[num]{journal}")
        zf.writestr("../escape.txt", "x")
    tpl = templates.download(str(archive), prefer="numbered num")
    assert tpl.kind == "latex" and tpl.main_tex.name == "template-num.tex"
    assert {f.name for f in tpl.files} == {"journal.cls", "journal-num.bst"}
    assert not (tmp_path / "escape.txt").exists()


def test_download_local_word_template(tmp_path):
    path = _docx(tmp_path / "T.dotx", "<w:p/>", "")
    assert templates.download(str(path)).kind == "word"


# --- LaTeX ----------------------------------------------------------------------

_PAPER = "\\documentclass[11pt]{article}\n\\usepackage{amsmath,geometry}\n\\geometry{margin=1in}\n\\title{T}\n\\author{Jane Doe \\and John Roe}\n\\begin{document}\n\\maketitle\nText.\n\\bibliographystyle{plain}\n\\bibliography{refs}\n\\end{document}\n"


def test_latex_template(tmp_path):
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "journal.cls").write_text("\\LoadClass{article}")
    (kit / "journal.bst").write_text("")
    (kit / "sample.tex").write_text("\\documentclass[review]{journal}\n\\begin{frontmatter}\n\\author{A}\n\\affiliation{X}\n\\end{frontmatter}\n\\bibliographystyle{journal}\n")
    tpl = templates.Template(kind="latex", source="kit", path=kit, main_tex=kit / "sample.tex", files=[kit / "journal.cls", kit / "journal.bst"])
    paper = tmp_path / "ms" / "paper.tex"
    paper.parent.mkdir()
    paper.write_text(_PAPER)
    done, todo = tex_format.apply_template(paper, tpl)
    out = paper.read_text()
    assert out.startswith("\\documentclass[review]{journal}")
    assert "\\usepackage{amsmath}\n% \\usepackage{amsmath,geometry}" in out and "% \\geometry{margin=1in}" in out
    assert "\\bibliographystyle{journal}" in out and (paper.parent / "journal.cls").exists()
    assert "\\author{Jane Doe}\n\\author{John Roe}" in out
    assert any("\\begin{frontmatter}" in t for t in todo)


def test_latex_style_file_template_loads_package(tmp_path):
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "conf.sty").write_text("\\def\\And{x}")
    (kit / "conf.tex").write_text("\\documentclass{article}\n\\usepackage{conf,times}\n\\author{A \\And B}\n")
    tpl = templates.Template(kind="latex", source="kit", path=kit, main_tex=kit / "conf.tex", files=[kit / "conf.sty"])
    paper = tmp_path / "paper.tex"
    paper.write_text(_PAPER)
    tex_format.apply_template(paper, tpl)
    out = paper.read_text()
    assert "\\documentclass{article}\n% Added by paperpush format" in out and "\\usepackage{conf,times}" in out
    assert "\\author{Jane Doe \\and John Roe}" in out  # the style defines \And, so authors are left alone


def test_latex_rules(tmp_path):
    paper = tmp_path / "paper.tex"
    paper.write_text(_PAPER)
    reqs = r.ManuscriptRequirements(slug="x", manuscript=r.ManuscriptRules(font="Times New Roman", font_size_pt=12, double_spacing=True, line_numbers=True, margins_mm=25, page_size="A4"))
    done, skipped = tex_format.apply_rules(paper, reqs)
    out = paper.read_text()
    assert out.startswith("\\documentclass[12pt]{article}")
    for line in ("\\usepackage[a4paper,margin=25mm]{geometry}", "\\doublespacing", "\\linenumbers", "\\usepackage{mathptmx}"):
        assert line in out
    assert "% \\geometry{margin=1in}" in out and not skipped


# --- orchestration --------------------------------------------------------------


@pytest.fixture
def venue(monkeypatch):
    data = {"$aliases": {"sections": {}, "statements": {}}, "testj": {"source_urls": ["https://j.org/guide"], "manuscript": {"double_spacing": True, "page_numbers": True}, "references": {"style": "Vancouver"}}}
    monkeypatch.setattr(r, "_load_raw", lambda: data)
    monkeypatch.setattr(templates, "fetch_page", lambda url, browser=True: ("<html></html>", ""))
    return Venue(slug="testj", name="Test", fields=[Field(id="manuscript_file", label="Manuscript", type="file")])


def test_format_word_without_template_uses_rules(tmp_path, venue):
    src = _docx(tmp_path / "ms.docx", "<w:p><w:r><w:t>Hello.</w:t></w:r></w:p>", '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>')
    before = src.read_bytes()
    result = formatter.format_manuscript(src, venue, out_dir=tmp_path / "out", browser=False)
    assert src.read_bytes() == before  # the original is untouched
    assert result.output == tmp_path / "out" / "ms_testj.docx" and result.output.exists()
    assert "no template link found" in result.template_note
    assert {"body text double-spaced", "page numbers added in the footer"} <= set(result.changes)
    assert any("reference style" in t for t in result.left_to_author)
    status = {cid: st for cid, _, st, _ in result.checks}
    assert status["layout.line_spacing"] == "passed" and status["layout.page_numbers"] == "passed"
    assert "## Changes" in result.report.read_text()


def test_format_rejects_pdf(tmp_path, venue):
    pdf = tmp_path / "ms.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with pytest.raises(formatter.FormatError, match="PDF"):
        formatter.format_manuscript(pdf, venue, out_dir=tmp_path / "out")


def test_format_with_given_template(tmp_path, venue):
    template = _docx(tmp_path / "T.docx", "<w:p/>", '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Arial"/></w:rPr></w:style>', '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>')
    src = _docx(tmp_path / "ms.docx", "<w:p><w:r><w:t>Hello.</w:t></w:r></w:p>", '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>')
    result = formatter.format_manuscript(src, venue, out_dir=tmp_path / "out", template=str(template))
    assert result.template == str(template)
    assert any("styles taken from the template" in c for c in result.changes)


def test_cli_update_sub_with_manuscript_flag(tmp_path, venue, monkeypatch, capsys):
    """`--update-sub` also works when the manuscript is named with --manuscript (the .sub names a PDF)."""
    from paperpush import cli

    monkeypatch.setattr(cli, "get_venue", lambda slug: venue)
    src = _docx(tmp_path / "ms.docx", "<w:p><w:r><w:t>Hello.</w:t></w:r></w:p>", "")
    (tmp_path / "ms.pdf").write_bytes(b"%PDF-1.4")
    sub = tmp_path / "testj.sub.txt"
    sub.write_text("@venue: testj\nmanuscript_file: ms.pdf\n", encoding="utf-8")
    assert cli.main(["format", str(sub), "--manuscript", str(src), "--update-sub", "--no-browser"]) == 0
    assert f"manuscript_file: {tmp_path / 'formatted_testj' / 'ms_testj.docx'}" in sub.read_text()


def test_zip_bundle_upload_is_the_new_zip(tmp_path, venue):
    bundle = tmp_path / "paper.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("paper.tex", _PAPER)
    result = formatter.format_manuscript(bundle, venue, out_dir=tmp_path / "out", use_template=False, compile_pdf=False)
    assert result.upload == tmp_path / "out" / "paper_testj.zip" and result.upload.exists()
    assert result.to_dict()["upload"] == str(result.upload)
