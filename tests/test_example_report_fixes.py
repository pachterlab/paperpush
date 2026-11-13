"""False positives found while preparing the example arXiv report in docs/_static.

Each was raised on a published paper (arXiv:2501.09805, PLOS LaTeX template)
that has no such problem.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

from paperpush import style_check as sc
from paperpush.bibliography_check import _tex_bibliography
from paperpush.citation_check import _tex_float_captions, tex_undefined_refs
from paperpush.consistency import compare_passage, manuscript_title
from paperpush.references import bib_structure_findings
from paperpush.rigor_check import _resource_spans
from paperpush.sensitive import _iter_personal_findings
from paperpush import document


def test_lastpage_label_comes_from_its_package():
    assert tex_undefined_refs(r"\usepackage{lastpage,fancyhdr} \rfoot{\thepage/\pageref{LastPage}}") == []
    # Without the package the label is still undefined.
    assert tex_undefined_refs(r"\rfoot{\pageref{LastPage}}") == ["LastPage"]


def test_doi_is_not_a_phone_number():
    assert list(_iter_personal_findings("x.bib", "doi = {10.1016/J.CELL.2015.12.056},")) == []
    assert list(_iter_personal_findings("x.bib", "url = {https://doi.org/10.1016/j.cell.2015.12.056}")) == []
    assert [f.detail.split(":")[1].split("(")[0].strip() for f in _iter_personal_findings("x.txt", "Tel: +44 131 650 1234")] == ["phone number"]


def test_ambiguous_strain_name_needs_an_animal_nearby():
    table = r"\textbf{FCSRT-ICR} & 13.06 14.5 3.66 \\ ICR = Immediate Cued Recall"
    assert [s for s in _resource_spans(table) if s[0] == "organism"] == []
    mice = "Male ICR mice (8 weeks old) were housed in groups of four."
    assert [s[1] for s in _resource_spans(mice) if s[0] == "organism"] == ["ICR"]
    # An unambiguous strain needs no context.
    assert [s[1] for s in _resource_spans("We used C57BL/6J for all experiments.") if s[0] == "organism"] == ["C57BL/6J"]


def test_acronym_meaning_in_singular_and_plural_is_one_meaning():
    body = [
        "We studied regions of interest (ROIs) in the brain.",
        "Each region of interest (ROI) was segmented, and every ROI was then compared across the ROIs.",
    ]
    assert sc.acronyms(body).conflicting == []
    other = ["We used a region of interest (ROI).", "The return on investment (ROI) was high, and the ROI grew."]
    assert sc.acronyms(other).conflicting


def test_plos_supporting_information_legend_counts_as_caption():
    source = r"""
\paragraph*{S1 Fig.}
\label{FDR_fig}
{\bf FDR Plots.} This figure visualizes the FDR correction.
\begin{figure}[!h]
\includegraphics{FDR_fig.png}
\end{figure}
\paragraph*{S1 Table.}
{\bf Single-layer vs. multiplex results.}
\begin{table}[!ht]
\begin{tabular}{l} a \\ \end{tabular}
\end{table}
\begin{figure}
\includegraphics{loose.png}
\end{figure}
"""
    captions = {name: caption for _, _, caption, name in _tex_float_captions(source)}
    assert "FDR Plots" in captions["FDR_fig.png"]
    assert "Single-layer vs. multiplex" in captions["number 1"]
    # A float with neither a \caption nor a lead-in legend still has none.
    assert captions["loose.png"] is None


def test_passage_matches_when_pdf_text_lost_spaces_around_math():
    hay = document.tokens("We hypothesized thatnQwould illuminate granular changes in the brain.")
    issues, detail = compare_passage("the abstract", "abstract", "We hypothesized that nQ would illuminate granular changes in the brain.", hay, "main.pdf")
    assert issues == [] and "matches" in detail


def _glyphs(text: str, y: float, size: float) -> list:
    out, x = [], 50.0
    for ch in text:
        out.append(SimpleNamespace(text=ch, x0=x, x1=x + 0.5 * size, y0=y, size=size, in_figure=False, baseline=None))
        x += 0.5 * size if ch != " " else 0.8 * size
    return [g for g in out if g.text != " "]


def test_title_from_pdf_stops_before_a_same_size_heading():
    glyphs = (_glyphs("Multiplex Nodal Modularity", 670, 14.3) + _glyphs("a working memory task", 656, 14.3)
              + _glyphs("Abstract", 442, 14.3) + _glyphs("body text " * 30, 400, 10))
    doc = SimpleNamespace(tex_source=None, is_docx=False, glyph_pages=[SimpleNamespace(glyphs=glyphs)])
    assert manuscript_title(doc) == "Multiplex Nodal Modularity a working memory task"


def _source_zip(tmp_path: Path) -> Path:
    bib = """@article{Used, author={Ada Lovelace}, title={One}, journal={J}, year={2020}, eprint={1010.3775}, archivePrefix={arXiv}}
@article{Usedb, author={Ada Lovelace}, title={One}, journal={J}, year={2020}, eprint={1010.3775}, archivePrefix={arXiv}}
@article{Other, author={Alan Turing}, title={Two}, journal={J}, year={1950}}
"""
    tex = r"\documentclass{article}\begin{document}See \cite{Used, Other}.\bibliography{refs}\end{document}"
    (tmp_path / "refs.bib").write_text(bib)  # the unzipped sources, lying next to the archive
    path = tmp_path / "source.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("main.tex", tex)
        z.writestr("refs.bib", bib)
    return path


def test_zip_bibliography_is_read_once(tmp_path):
    path = _source_zip(tmp_path)
    bib = _tex_bibliography(path, document.get(path).tex_source)
    assert sorted(r.key for r in bib.refs) == ["Other", "Used", "Usedb"]


def test_uncited_duplicate_arxiv_entry_is_not_reported(tmp_path):
    path = _source_zip(tmp_path)
    findings, total = bib_structure_findings([path])
    assert total == 3
    assert not [f for f in findings if f.category == "duplicate arXiv preprint"]


def test_bare_percent_line_endings_are_not_comments_to_strip():
    from paperpush.sensitive import _iter_latex_findings

    macros = "\\newcommand\\thickcline[1]{%\n  \\cline{#1}%\n}\n\\begin{adjustwidth}{-2.25in}{0in} %\n"
    assert list(_iter_latex_findings("main.tex", macros)) == []
    found = list(_iter_latex_findings("main.tex", macros + "% reviewer 2 is wrong about this\n"))
    assert any("1 source comment line(s)" in f.detail for f in found)


def test_consortium_registered_before_the_people_is_not_the_first_author():
    from paperpush.references import Entry, _first_author_of, _mismatches

    record = {
        "title": ["Four distinct trajectories of tau deposition identified in Alzheimer's disease"],
        "author": [{"name": "the Alzheimer's Disease Neuroimaging Initiative", "sequence": "first"},
                   {"given": "Jacob W.", "family": "Vogel", "sequence": "first"},
                   {"given": "Alexandra L.", "family": "Young", "sequence": "additional"}],
        "issued": {"date-parts": [[2021]]},
    }
    assert _first_author_of(record) == "Vogel"
    entry = Entry(key="Vogel2021", kind="article", title="Four distinct trajectories of tau deposition identified in Alzheimer's disease",
                  author="Vogel, Jacob W. and Young, Alexandra L.", year="2021", where="refs.bib", raw_doi="", doi="")
    assert _mismatches(entry, record) == []
    group = Entry(key="ADNI2021", kind="article", title=entry.title, author="{the Alzheimer's Disease Neuroimaging Initiative}", year="2021", where="refs.bib", raw_doi="", doi="")
    assert _mismatches(group, record) == []
    # A record listing only a group still names it.
    assert _first_author_of({"author": [{"name": "ENCODE Project Consortium"}]}) == "ENCODE Project Consortium"
