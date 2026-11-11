"""``paperpush convert``: the math translator, the readers and writers, and the round trips."""

from __future__ import annotations

import base64
import json
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

import pytest
from PIL import Image

from paperpush import cli, convert
from paperpush.convert import mathconv as mc
from paperpush.convert.model import Bibliography, Cite, Equation, Figure, Heading, ListBlock, Log, Math, Para, Ref, Table, all_inlines, plain_text
from paperpush.convert.read_docx import DocxReader
from paperpush.convert.read_md import MarkdownReader, parse_front
from paperpush.convert.read_tex import TexReader, strip_comments
from paperpush.convert.write_docx import MANIFEST_NS

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
CT = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'

BIB = """@article{smith2020, author = {Smith, Jane and Lee, Kai}, title = {{RNA} analysis}, journal = {Nature}, year = {2020}, doi = {10.1/x}}
@book{knuth84, author = {Knuth, Donald}, title = {Book}, publisher = {AW}, year = 1984}
"""

TEX = r"""\documentclass{article}
\usepackage{amsmath,graphicx,booktabs}
\usepackage[numbers]{natbib}
\newcommand{\R}{\mathbb{R}}
\title{A Test}
\author{Ada Lovelace}
\begin{document}
\maketitle
\begin{abstract}
An abstract with $x \in \R$.
\end{abstract}

\section{Introduction}\label{sec:intro}
% gone
Prior work~\cite{smith2020} and \citet{knuth84}. See Figure~\ref{fig:a}, Table~\ref{tab:b}, Eq.~\eqref{eq:c}.\footnote{A note.}

\begin{equation}\label{eq:c}
E = mc^2
\end{equation}

\begin{figure}
\includegraphics[width=0.5\textwidth]{fig}
\caption{A figure.}\label{fig:a}
\end{figure}

\begin{table}
\caption{A table.}\label{tab:b}
\begin{tabular}{lr}
\toprule
a & b \\
\midrule
1 & 2 \\
\bottomrule
\end{tabular}
\end{table}

A closing paragraph with \texttt{code}.

\bibliographystyle{unsrtnat}
\bibliography{refs}
\end{document}
"""


def _project(tmp_path: Path, tex: str = TEX) -> Path:
    (tmp_path / "paper.tex").write_text(tex, encoding="utf-8")
    (tmp_path / "refs.bib").write_text(BIB, encoding="utf-8")
    Image.new("RGB", (300, 200), "white").save(tmp_path / "fig.png")
    return tmp_path / "paper.tex"


def _docx(path: Path, body: str, extra: dict[str, str] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CT)
        zf.writestr("word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return path


def _p(text: str, props: str = "") -> str:
    return f'<w:p>{props}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _bold(text: str) -> str:
    return f'<w:p><w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _read_docx(path: Path, tmp_path: Path, **kw):
    log = Log()
    return DocxReader(path, log, tmp_path / "img", **kw).read(), log


def _replace_in_document(src: Path, dst: Path, old: str, new: str) -> None:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "word/document.xml":
                text = data.decode("utf-8")
                assert old in text
                data = text.replace(old, new).encode("utf-8")
            zout.writestr(info, data)


# --- math ---------------------------------------------------------------------


def _tree(xml: str):
    return ET.fromstring(f"<m:oMath {mc.NS_DECL}>{xml}</m:oMath>")


@pytest.mark.parametrize(
    "latex",
    [r"E = mc^2", r"\frac{a+b}{\sqrt{c}}", r"\sum_{i=1}^{n} x_i^2", r"\int_0^\infty e^{-x}\,dx", r"\hat{\beta} = (X^T X)^{-1}", r"\left[\frac{a}{b}\right]", r"\begin{pmatrix} a & b \\ c & d \end{pmatrix}", r"f = \begin{cases} 1 & x>0 \\ 0 & \text{else} \end{cases}", r"\mathbf{x} \in \mathbb{R}^{n}", r"\lim_{x \to 0} \frac{\sin x}{x}", r"\sqrt[3]{x}", r"\binom{n}{k}", r"\overline{x} + \underbrace{a}_{c}", r"x' + y''", r"\operatorname{softmax}(z)"],
)
def test_math_survives_both_directions(latex):
    xml, unsupported = mc.latex_to_omml(latex)
    assert not unsupported
    back = mc.omml_to_latex(_tree(xml))
    again, _ = mc.latex_to_omml(back)
    assert mc.signature_of_xml(xml) == mc.signature_of_xml(again)


def test_alignment_point_never_lands_inside_a_subscript():
    xml, _ = mc.latex_to_omml(r"V &= \int_{x} p \\ W &= 1", rows=True)
    rows, has_rows = mc.omml_rows_to_latex(_tree(xml))
    assert has_rows and "_{&" not in rows.replace(" ", "")
    xml, _ = mc.latex_to_omml(r"V = & \int_{x} p", rows=True)
    assert "_{&" not in mc.omml_rows_to_latex(_tree(xml))[0].replace(" ", "")


def test_signature_ignores_what_word_rewrites():
    ours, _ = mc.latex_to_omml(r"\frac{a}{b} - c")
    word = "<m:f><m:num><m:r><m:t>a</m:t></m:r></m:num><m:den><m:r><m:rPr/><m:t>b</m:t></m:r></m:den></m:f><m:r><m:t>-</m:t></m:r><m:r><m:t>c</m:t></m:r>"
    assert mc.signature_of_xml(ours) == mc.signature_of_xml(word)


def test_signature_notices_an_edit():
    a, _ = mc.latex_to_omml("x + y")
    b, _ = mc.latex_to_omml("x + z")
    assert mc.signature_of_xml(a) != mc.signature_of_xml(b)


def test_unknown_math_is_reported_and_compiles():
    xml, unsupported = mc.latex_to_omml(r"\foo x")
    assert unsupported == {r"\foo"}
    assert r"\textbackslash{}foo" in mc.omml_to_latex(_tree(xml))


def test_word_math_italic_letters_become_plain():
    assert mc.omml_to_latex(_tree("<m:r><m:t>𝑎</m:t></m:r>")) == "a"


# --- LaTeX reading ------------------------------------------------------------


def test_strip_comments_keeps_escaped_percent():
    assert strip_comments("95\\% sure % not this\nnext\n") == "95\\% sure next\n"


def _tex(tmp_path: Path, body: str, preamble: str = ""):
    path = tmp_path / "m.tex"
    path.write_text(f"\\documentclass{{article}}\n{preamble}\n\\begin{{document}}\n{body}\n\\end{{document}}\n", encoding="utf-8")
    log = Log()
    return TexReader(path, log).read(), log


def test_macros_expand(tmp_path):
    doc, log = _tex(tmp_path, r"Value \val{3}.", r"\newcommand{\val}[1]{v=#1}")
    assert plain_text(doc.blocks[0].children) == "Value v=3."
    assert not log.warnings


def test_definition_in_the_body_moves_to_the_preamble(tmp_path):
    doc, log = _tex(tmp_path, "\\newcommand{\\foo}[1]{#1!}\n\nSay \\foo{hi}.")
    assert "\\newcommand{\\foo}" in doc.preamble
    assert plain_text(doc.blocks[0].children) == "Say hi!."
    assert not log.warnings


def test_macros_from_a_local_style_file(tmp_path):
    (tmp_path / "venue.sty").write_text("\\def\\eg{e.g.}\n\\def\\internal@x{y}\n", encoding="utf-8")
    doc, log = _tex(tmp_path, r"As \eg{} this.", r"\usepackage{venue}")
    assert plain_text(doc.blocks[0].children) == "As e.g. this."
    assert not log.warnings


def test_two_tables_in_one_environment(tmp_path):
    body = "\\begin{table}\n\\begin{tabular}{l}\na\\\\\n\\end{tabular}\n\\caption{One}\\label{tab:1}\n\\begin{tabular}{l}\nb\\\\\n\\end{tabular}\n\\caption{Two}\n\\vspace{1em}\n\\label{tab:2}\n\\end{table}"
    doc, _ = _tex(tmp_path, body)
    tables = [b for b in doc.blocks if isinstance(b, Table)]
    assert [t.label for t in tables] == ["tab:1", "tab:2"]


def test_tag_numbers_a_row_of_a_starred_environment(tmp_path):
    doc, _ = _tex(tmp_path, "\\begin{align*}\na &= b \\tag{1}\\label{eq:x}\n\\end{align*}")
    assert doc.blocks[0].numbered and doc.blocks[0].row_labels == ["eq:x"]


def test_label_on_a_nonumber_row_names_the_next_number(tmp_path):
    doc, _ = _tex(tmp_path, "\\begin{align}\n\\label{eq:x}\na &= b \\nonumber \\\\\nc &= d\n\\end{align}")
    assert doc.blocks[0].row_labels == ["eq:x"]


def test_multirow_covered_cell_is_not_a_new_column(tmp_path):
    body = "\\begin{tabular}{ll}\n\\multirow{2}{*}{A} & 1 \\\\\n & 2 \\\\\n\\end{tabular}"
    doc, _ = _tex(tmp_path, body, r"\usepackage{multirow}")
    table = doc.blocks[0]
    assert table.rows[0][0].rowspan == 2
    assert [plain_text(c.children) for c in table.rows[1]] == ["2"]


def test_dollars_inside_text_inside_math(tmp_path):
    doc, _ = _tex(tmp_path, r"See $\left[\text{3$\times$3}\right]$ here.")
    assert sum(isinstance(n, Math) for n in all_inlines(doc)) == 1


def test_iffalse_is_a_comment(tmp_path):
    doc, _ = _tex(tmp_path, "Kept.\n\\iffalse\nGone.\n\\fi")
    assert "Gone" not in plain_text(doc.blocks[0].children)


def test_style_and_bibliography_in_separate_groups_are_one_list(tmp_path):
    (tmp_path / "r.bib").write_text(BIB, encoding="utf-8")
    doc, _ = _tex(tmp_path, "Text \\cite{knuth84}.\n\n\\bibliographystyle{plain}\n\nMore.\n\n{\\small\n\\bibliography{r}\n}")
    assert sum(isinstance(b, Bibliography) for b in doc.blocks) == 1


def test_url_escapes_are_not_doubled(tmp_path):
    doc, _ = _tex(tmp_path, r"\url{https://a.org/x\#y}")
    link = doc.blocks[0].children[0]
    assert link.url == "https://a.org/x#y"


# --- Markdown reading ---------------------------------------------------------


def test_front_matter():
    front, body = parse_front('---\ntitle: "T"\nauthor:\n  - name: A\n    affiliation: U\nkeywords: [a, b]\nabstract: |\n  Line.\n---\nBody\n')
    assert front["title"] == "T" and front["author"][0]["affiliation"] == "U" and front["keywords"] == ["a", "b"] and "Line." in front["abstract"]
    assert body == "Body\n"


def _md(tmp_path: Path, text: str):
    path = tmp_path / "m.md"
    path.write_text(text, encoding="utf-8")
    log = Log()
    return MarkdownReader(path, log).read(), log


def test_markdown_citations_and_crossrefs(tmp_path):
    doc, _ = _md(tmp_path, "Text [@a; @b] and @c, see @fig:x.\n\n![Cap.](f.png){#fig:x}\n")
    nodes = list(all_inlines(doc))
    assert [n.keys for n in nodes if isinstance(n, Cite)] == [["a", "b"], ["c"]]
    assert [n.label for n in nodes if isinstance(n, Ref)] == ["fig:x"]


def test_markdown_email_is_not_a_citation(tmp_path):
    doc, _ = _md(tmp_path, "Write to ada@example.org today.\n")
    assert not [n for n in all_inlines(doc) if isinstance(n, Cite)]


def test_markdown_pipe_table_caption(tmp_path):
    doc, _ = _md(tmp_path, "| a | b |\n|---|--:|\n| 1 | 2 |\n\n: Cap. {#tbl:x}\n")
    table = doc.blocks[0]
    assert isinstance(table, Table) and table.label == "tbl:x" and table.aligns == ["left", "right"]


def test_markdown_nested_list(tmp_path):
    doc, _ = _md(tmp_path, "- one\n  - two\n- three\n")
    assert isinstance(doc.blocks[0], ListBlock) and len(doc.blocks[0].items) == 2
    assert isinstance(doc.blocks[0].items[0][1], ListBlock)


# --- round trips --------------------------------------------------------------


def _to_word(tmp_path: Path, **kw) -> Path:
    result = convert.convert_manuscript(_project(tmp_path), "docx", out_dir=tmp_path / "w", **kw)
    assert result.clean, result.warnings
    final = tmp_path / "round.docx"
    shutil.copy(result.output, final)
    return final


def test_tex_to_docx_package_is_well_formed(tmp_path):
    docx = _to_word(tmp_path)
    with zipfile.ZipFile(docx) as zf:
        for name in zf.namelist():
            if name.endswith((".xml", ".rels")):
                ET.fromstring(zf.read(name))
        document = zf.read("word/document.xml").decode()
    assert "<m:oMath>" in document and "PAPERPUSH_CITE" in document and " REF _pp_fig_a" in document and "SEQ Figure" in document


def _relationship_ids_resolve(docx: Path) -> None:
    """Word refuses a file with an r:id its own part's relationships do not define."""
    with zipfile.ZipFile(docx) as zf:
        names = set(zf.namelist())
        for part in ("word/document.xml", "word/footnotes.xml"):
            used = set(re.findall(r'r:(?:id|embed)="([^"]+)"', zf.read(part).decode("utf-8")))
            rels_name = part.replace("word/", "word/_rels/") + ".rels"
            defined = set(re.findall(r'Id="([^"]+)"', zf.read(rels_name).decode("utf-8"))) if rels_name in names else set()
            assert used <= defined, (part, used - defined)


def test_link_in_a_footnote_resolves_in_the_footnote_part(tmp_path):
    tex = TEX.replace(r"\footnote{A note.}", r"\footnote{See \href{https://a.org/x}{the site}.}")
    result = convert.convert_manuscript(_project(tmp_path, tex), "docx", out_dir=tmp_path / "w")
    _relationship_ids_resolve(result.output)
    doc, _ = _read_docx(result.output, tmp_path)
    from paperpush.convert.model import Link

    assert [n.url for n in all_inlines(doc) if isinstance(n, Link)] == ["https://a.org/x"]


def test_round_trip_restores_every_block(tmp_path):
    docx = _to_word(tmp_path)
    result = convert.convert_manuscript(docx, "tex", out_dir=tmp_path / "t", compile_pdf=False)
    assert result.clean, result.warnings
    assert result.generated_blocks == 0
    text = result.output.read_text(encoding="utf-8")
    assert r"Prior work~\cite{smith2020} and \citet{knuth84}." in text
    assert r"\newcommand{\R}{\mathbb{R}}" in text and r"\bibliography{refs}" in text
    assert (tmp_path / "t" / "refs.bib").is_file() and (tmp_path / "t" / "fig.png").is_file()


def test_an_edit_regenerates_only_its_block(tmp_path):
    docx = _to_word(tmp_path)
    edited = tmp_path / "edited.docx"
    _replace_in_document(docx, edited, "A closing paragraph with ", "A revised paragraph with ")
    result = convert.convert_manuscript(edited, "tex", out_dir=tmp_path / "t", compile_pdf=False)
    assert result.generated_blocks == 1
    text = result.output.read_text(encoding="utf-8")
    assert r"A revised paragraph with \texttt{code}." in text and "A closing" not in text
    assert r"Prior work~\cite{smith2020}" in text


def test_multi_paragraph_theorem_is_not_duplicated(tmp_path):
    tex = TEX.replace("A closing paragraph", "\\begin{theorem}\nFirst part.\n\nSecond part.\n\\end{theorem}\n\nA closing paragraph").replace("\\title{A Test}", "\\newtheorem{theorem}{Theorem}\n\\title{A Test}")
    result = convert.convert_manuscript(_project(tmp_path, tex), "docx", out_dir=tmp_path / "w")
    shutil.copy(result.output, tmp_path / "round.docx")
    back = convert.convert_manuscript(tmp_path / "round.docx", "tex", out_dir=tmp_path / "t", compile_pdf=False)
    assert back.output.read_text(encoding="utf-8").count("Second part.") == 1


def test_no_source_embeds_no_latex(tmp_path):
    tex = TEX.replace("A closing paragraph", "\\newcommand{\\secret}{x}\nA closing paragraph")
    result = convert.convert_manuscript(_project(tmp_path, tex), "docx", out_dir=tmp_path / "w", embed_source=False)
    with zipfile.ZipFile(result.output) as zf:
        packed = ET.fromstring(zf.read("customXml/item1.xml")).text
    manifest = json.loads(zlib.decompress(base64.b64decode(packed)))
    assert set(manifest) == {"version", "labels", "citation_style"}


def test_no_source_still_converts_and_keeps_links(tmp_path):
    result = convert.convert_manuscript(_project(tmp_path), "docx", out_dir=tmp_path / "w", embed_source=False)
    back = convert.convert_manuscript(result.output, "tex", out_dir=tmp_path / "t", compile_pdf=False)
    text = back.output.read_text(encoding="utf-8")
    assert r"\cite{smith2020}" in text and r"\citet{knuth84}" in text and r"\ref{fig:a}" in text and r"\eqref{eq:c}" in text and r"\label{fig:a}" in text


@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="needs a TeX installation")
def test_generated_latex_compiles(tmp_path):
    result = convert.convert_manuscript(_project(tmp_path), "docx", out_dir=tmp_path / "w", embed_source=False)
    back = convert.convert_manuscript(result.output, "tex", out_dir=tmp_path / "t")
    assert back.compiled is True and back.pdf.is_file()


def test_references_to_a_vanished_theorem_keep_their_number(tmp_path):
    tex = TEX.replace("A closing paragraph", "\\begin{theorem}\\label{thm:a}\nTrue.\n\\end{theorem}\n\nBy Theorem~\\ref{thm:a}, a closing paragraph").replace("\\title{A Test}", "\\newtheorem{theorem}{Theorem}\n\\title{A Test}")
    result = convert.convert_manuscript(_project(tmp_path, tex), "docx", out_dir=tmp_path / "w", embed_source=False)
    back = convert.convert_manuscript(result.output, "tex", out_dir=tmp_path / "t", compile_pdf=False)
    text = back.output.read_text(encoding="utf-8")
    assert "Theorem~1, a closing" in text and r"\ref{thm:a}" not in text


def test_md_to_docx_and_tex(tmp_path):
    (tmp_path / "refs.bib").write_text(BIB, encoding="utf-8")
    Image.new("RGB", (100, 100), "white").save(tmp_path / "f.png")
    (tmp_path / "p.md").write_text("---\ntitle: T\nbibliography: refs.bib\n---\n\n# Intro\n\nText [@smith2020], see @fig:x and @eq:y.\n\n$$ a = b $$ {#eq:y}\n\n![Cap.](f.png){#fig:x}\n", encoding="utf-8")
    word = convert.convert_manuscript(tmp_path / "p.md", "docx", out_dir=tmp_path / "w")
    assert word.clean, word.warnings
    tex = convert.convert_manuscript(tmp_path / "p.md", "tex", out_dir=tmp_path / "t", compile_pdf=False)
    assert tex.clean, tex.warnings
    text = tex.output.read_text(encoding="utf-8")
    assert r"\citep{smith2020}" in text and r"Figure~\ref{fig:x}" in text and r"\includegraphics" in text


def test_adjacent_math_does_not_open_display_math(tmp_path):
    from paperpush.convert.write_tex import TexWriter
    from paperpush.convert.model import Document

    writer = TexWriter(Document(), Log(), tmp_path, source_dir=tmp_path)
    assert "$$" not in writer.inlines([Math("a"), Math("b")])


# --- Word written by hand -----------------------------------------------------

CSL = {"citationItems": [{"id": 7, "uris": ["http://z/1"], "itemData": {"id": 7, "type": "article-journal", "title": "Paper", "container-title": "Cell", "author": [{"family": "Zed", "given": "Amy"}], "issued": {"date-parts": [[2018]]}, "DOI": "10.1/z"}}]}


def _field(instr: str, result: str) -> str:
    return f'<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve">{instr}</w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>{result}</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>'


def test_typed_numeric_citations_match_the_reference_list(tmp_path):
    body = _bold("A Title") + _p("Earlier work [1, 2] and [9].") + _bold("References") + _p("1. Doe J. One. Nature. 2019.") + _p("2. Roe B. Two. Cell. 2021.")
    doc, _ = _read_docx(_docx(tmp_path / "a.docx", body), tmp_path)
    cites = [n for n in all_inlines(doc) if isinstance(n, Cite)]
    assert [c.keys for c in cites] == [["doe2019", "roe2021"]]
    assert "[9]" in plain_text(doc.blocks[0].children)


def test_typed_author_year_citations(tmp_path):
    body = _bold("A Title") + _p("As shown (Doe, 2019) and by Roe et al. (2021), in (2020) alone.") + _bold("References") + _p("Doe J. One. Nature. 2019.") + _p("Roe B, Poe C. Two. Cell. 2021.")
    doc, log = _read_docx(_docx(tmp_path / "a.docx", body), tmp_path)
    cites = [n for n in all_inlines(doc) if isinstance(n, Cite)]
    assert [(c.keys, c.mode) for c in cites] == [(["doe2019"], "paren"), (["roe2021"], "text")]
    assert doc.citation_style == "author-year" and not log.warnings


def test_superscript_power_is_not_a_citation(tmp_path):
    sup = '<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>1</w:t></w:r>'
    body = _bold("A Title") + f"<w:p><w:r><w:t>Value 10</w:t></w:r>{sup}<w:r><w:t> and a claim</w:t></w:r>{sup}</w:p>" + _bold("References") + _p("1. Doe J. One. Nature. 2019.")
    doc, _ = _read_docx(_docx(tmp_path / "a.docx", body), tmp_path)
    assert len([n for n in all_inlines(doc) if isinstance(n, Cite)]) == 1


def test_zotero_field_becomes_a_bib_entry(tmp_path):
    instr = " ADDIN ZOTERO_ITEM CSL_CITATION " + json.dumps(CSL) + " "
    body = _bold("A Title") + f"<w:p>{_field(instr, '(Zed, 2018)')}</w:p>"
    doc, _ = _read_docx(_docx(tmp_path / "a.docx", body), tmp_path)
    assert doc.bib["zed2018"].fields["doi"] == "10.1/z"
    assert [n.keys for n in all_inlines(doc) if isinstance(n, Cite)] == [["zed2018"]]


@pytest.mark.parametrize("choice,kept,gone", [("accept", "new", "old"), ("reject", "old", "new")])
def test_tracked_changes(tmp_path, choice, kept, gone):
    body = '<w:p><w:ins w:id="1" w:author="X"><w:r><w:t>new </w:t></w:r></w:ins><w:del w:id="2" w:author="X"><w:r><w:delText>old </w:delText></w:r></w:del><w:r><w:t>text</w:t></w:r></w:p>'
    doc, log = _read_docx(_docx(tmp_path / "a.docx", body), tmp_path, tracked=choice)
    text = " ".join(plain_text(b.children) for b in doc.blocks if isinstance(b, Para))
    assert kept in text and gone not in text
    assert any("tracked change" in w for w in log.warnings)


def test_hidden_text_is_left_out_and_reported(tmp_path):
    body = "<w:p><w:r><w:t>Seen </w:t></w:r><w:r><w:rPr><w:vanish/></w:rPr><w:t>hidden</w:t></w:r></w:p>"
    doc, log = _read_docx(_docx(tmp_path / "a.docx", body), tmp_path)
    assert "hidden" not in plain_text(doc.blocks[0].children)
    assert any("hidden text" in w for w in log.warnings)


def test_a_sentence_is_not_a_caption(tmp_path):
    body = _p("Figure 2 shows the result.")
    doc, _ = _read_docx(_docx(tmp_path / "a.docx", body), tmp_path)
    assert isinstance(doc.blocks[0], Para)


def test_bold_lead_in_is_not_a_heading_when_styles_are_used(tmp_path):
    styles = f'<w:styles {W}><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style></w:styles>'
    body = _p("Real", '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>') + _bold("Bold lead in") + _p("Text.")
    doc, _ = _read_docx(_docx(tmp_path / "a.docx", body, {"word/styles.xml": styles}), tmp_path)
    assert [type(b) for b in doc.blocks] == [Heading, Para, Para]


def test_figure_after_the_references_survives(tmp_path):
    tex = TEX.replace("\\end{document}", "\\appendix\n\\begin{figure}\n\\includegraphics{fig}\n\\caption{Late.}\\label{fig:z}\n\\end{figure}\n\\end{document}")
    result = convert.convert_manuscript(_project(tmp_path, tex), "docx", out_dir=tmp_path / "w")
    assert result.counts["figures"] == (2, 2), result.counts


def test_equation_on_its_own_line(tmp_path):
    body = "<w:p><m:oMathPara><m:oMath><m:r><m:t>x</m:t></m:r></m:oMath></m:oMathPara></w:p>"
    doc, _ = _read_docx(_docx(tmp_path / "a.docx", body), tmp_path)
    assert isinstance(doc.blocks[0], Equation) and doc.blocks[0].latex == "x"


# --- safety -------------------------------------------------------------------


def test_doctype_is_refused(tmp_path):
    path = tmp_path / "a.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", f'<!DOCTYPE x [<!ENTITY a "b">]><w:document {W}><w:body/></w:document>')
    with pytest.raises(convert.ConvertError, match="DOCTYPE"):
        convert.convert_manuscript(path, "tex", out_dir=tmp_path / "o")


@pytest.mark.parametrize("payload", [r"\immediate\write18{rm -rf x}", r"\input{/etc/passwd}", r"\directlua{os.execute('x')}"])
def test_manifest_with_dangerous_latex_is_ignored(tmp_path, payload):
    manifest = {"version": 1, "preamble": "\\documentclass{article}\n" + payload, "blocks": {}}
    packed = base64.b64encode(zlib.compress(json.dumps(manifest).encode())).decode()
    item = f'<pp:manifest xmlns:pp="{MANIFEST_NS}">{packed}</pp:manifest>'
    path = _docx(tmp_path / "a.docx", _p("Text."), {"customXml/item1.xml": item})
    result = convert.convert_manuscript(path, "tex", out_dir=tmp_path / "o", compile_pdf=False)
    assert payload not in result.output.read_text(encoding="utf-8")
    assert any("manifest" in w for w in result.warnings)


def test_restored_figure_cannot_write_outside_the_output(tmp_path):
    from paperpush.convert.model import Document, Image as Img
    from paperpush.convert.write_tex import TexWriter

    (tmp_path / "out").mkdir()
    (tmp_path / "secret.png").write_bytes(b"x")
    doc = Document(manifest={"images": {"abc": "x"}})
    writer = TexWriter(doc, Log(), tmp_path / "out", source_dir=tmp_path / "out")
    block = Figure(images=[Img(path="", original="abc")])
    assert writer.figure_files_ok(block, r"\includegraphics{../secret.png}") is False
    assert not (tmp_path / "secret.png.copy").exists()


# --- the command --------------------------------------------------------------


@pytest.mark.parametrize("name,error", [("a.pdf", "PDF"), ("a.doc", "legacy"), ("a.xyz", "converts")])
def test_rejected_inputs(tmp_path, name, error):
    path = tmp_path / name
    path.write_bytes(b"x")
    with pytest.raises(convert.ConvertError, match=error):
        convert.convert_manuscript(path, "docx", out_dir=tmp_path / "o")


def test_same_format_points_at_format(tmp_path):
    path = tmp_path / "a.tex"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(convert.ConvertError, match="paperpush format"):
        convert.convert_manuscript(path, "tex", out_dir=tmp_path / "o")


def test_never_writes_beside_the_original(tmp_path):
    with pytest.raises(convert.ConvertError, match="never overwritten"):
        convert.convert_manuscript(_project(tmp_path), "docx", out_dir=tmp_path)


def test_original_is_untouched(tmp_path):
    main = _project(tmp_path)
    before = main.read_bytes()
    convert.convert_manuscript(main, "docx", out_dir=tmp_path / "w")
    assert main.read_bytes() == before


def test_report_is_written(tmp_path):
    result = convert.convert_manuscript(_project(tmp_path), "docx", out_dir=tmp_path / "w")
    report = result.report.read_text(encoding="utf-8")
    assert "Result: clean" in report and "| figures | 1 | 1 |" in report


def test_missing_bib_entry_makes_it_not_clean(tmp_path):
    result = convert.convert_manuscript(_project(tmp_path, TEX.replace(r"\cite{smith2020}", r"\cite{nobody}")), "docx", out_dir=tmp_path / "w")
    assert not result.clean and any("nobody" in w for w in result.warnings)


def test_cli_json_and_strict(tmp_path, capsys):
    main = _project(tmp_path, TEX.replace(r"\cite{smith2020}", r"\cite{nobody}"))
    code = cli.main(["convert", "--manuscript", str(main), "--to", "docx", "-o", str(tmp_path / "w"), "--json", "--strict"])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["clean"] is False


def test_cli_needs_a_manuscript(capsys):
    assert cli.main(["convert", "--to", "docx"]) == 1
    assert "give the manuscript" in capsys.readouterr().err


def test_mcp_tool_is_registered():
    from paperpush import mcp_server

    assert callable(mcp_server.convert_manuscript)


def test_mcp_tool_converts(tmp_path):
    from paperpush import mcp_server

    out = mcp_server.convert_manuscript("docx", manuscript=str(_project(tmp_path)), output_dir=str(tmp_path / "w"))
    assert out["clean"] is True and out["upload"].endswith(".docx")


def test_output_reads_in_the_rest_of_paperpush(tmp_path):
    from paperpush.manuscript import docx_to_text

    result = convert.convert_manuscript(_project(tmp_path), "docx", out_dir=tmp_path / "w", embed_source=False)
    text = docx_to_text(result.output)
    assert "A closing paragraph" in text and "ADDIN" not in text and "PAPERPUSH" not in text
