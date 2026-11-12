"""Regression tests for the acronym, heading-case, body-font-size, and title-length checks.

Each case is a deficiency found on the dev split of the planted-error benchmark:
an acronym used in a hyphenated compound or twice without a definition, a
mixed-case coinage ("TDistNN") defined only in a legend, a heading whose
capitalization departs from its level's style, a document-wide ``\\fontsize``
in a template-bound paper, and a title over the venue's character limit when
the .sub has no title.
"""

from __future__ import annotations

import os
from pathlib import Path

from paperpush import layout_check, template_check
from paperpush import style_check as sc
from paperpush.database import Field, Venue
from paperpush.requirements import ManuscriptRequirements, TitlePageRules
from paperpush.requirements_check import _manuscript_title_length_issues, _text_field_issues

MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
TITLE = Field(id="title", label="Title", type="text")
VENUE = Venue(slug="testj", name="Test Venue", fields=[MANUSCRIPT, TITLE])


# --- acronyms -----------------------------------------------------------------------------


def test_acronym_in_a_hyphenated_compound_is_a_use():
    body = ["We use a BPE-inspired scheme here.", "The BPE-like merge runs again, see BPE."]
    found = sc.acronyms(body)
    assert [s.split("'")[1] for s in found.undefined] == ["BPE"]
    # A hyphen before a digit or a capital is part of a name, not a compound.
    assert sc.acronyms(["We measured IL-6 twice. Then IL-6 again and HLA-DR."]).undefined == []


def test_acronym_used_twice_and_never_spelled_out_is_reported():
    found = sc.acronyms(["SemanticST had the best ARI and HOM scores on the data.", "Again HOM was highest of all the methods we ran."])
    assert any("'HOM' (2x" in s for s in found.undefined)
    # Once is often a name in passing.
    assert sc.acronyms(["Cells were edited via CRISPR in the first screen of the study."]).undefined == []


def test_acronyms_in_tables_count_as_uses_but_do_not_make_one_undefined():
    body = ["We compare with Erasing Stable Diffusion (ESD) and others in our experiments."]
    # Used only in a table: not "spelled out but never used again".
    assert sc.acronyms(body, tables=["VDU , SA , ESD"]).rarely_used == []
    assert sc.acronyms(body).rarely_used == ["'ESD'"]
    # A column of method names alone is not reported as undefined.
    assert sc.acronyms(["We ran the methods in the table on all the data."], tables=["SDPR , PANPRS", "SDPR , PANPRS"]).undefined == []


def test_acronym_spelled_out_in_a_legend_is_defined():
    floats = ["Results with the full width at half maximum (FWHM) of each peak."]
    found = sc.acronyms(["The FWHM was 0.05 here.", "A larger FWHM broadened the peaks."], floats=floats)
    assert found.undefined == []
    # A legend is read on its own, so the main text still has to spell it out.
    assert [s.split("'")[1] for s in found.legend_only] == ["FWHM"]


def test_acronym_spelled_out_in_main_text_and_again_in_a_legend_is_not_redefined():
    floats = ["Figure 1. The full width at half maximum (FWHM) of each peak."]
    body = ["We measured the full width at half maximum (FWHM).", "The FWHM was 0.05.", "A larger FWHM broadened the peaks."]
    found = sc.acronyms(body, floats=floats)
    assert found.redefined == [] and found.legend_only == [] and found.undefined == []


def test_mixed_case_acronym_defined_in_a_legend_is_tracked():
    abstract = ["We propose TDistNNs, which model heavy-tailed outputs well."]
    body = ["Here we implement TDistNNs in a deep learning framework for regression.", "Neural networks with t-distributed outputs (TDistNNs) are shown in the figure."]
    floats = ["Figure 1. The output layer of t-Distributed Neural Networks (TDistNNs) and its parameters."]
    found = sc.acronyms(body, abstract, floats)
    assert [s.split("'")[1] for s in found.before_definition] == ["TDistNN"]
    assert found.abstract_undefined == ["'TDistNN'"]
    # A mixed-case name whose initials the words before it do not spell is not an acronym.
    assert sc.mixed_case_acronyms(["We use deep learning frameworks (PyTorch, ImageNet) and (TinyNet)."]) == set()


def test_acronym_report_names_every_hit():
    hits = [f"'A{c}B' (2x, first in ...)" for c in "CDEFGHI"]
    issue = sc._report(hits, "acronym(s) used but never spelled out", "manuscript_file", "ms.tex", named=True)
    assert "(and 2 more: 'AHB', 'AIB')" in issue.message


def test_tex_floats_give_legends_and_table_cells(tmp_path):
    source = (
        "\\documentclass{article}\\begin{document}Text.\n\\begin{table}\\caption{Scores of all methods.}\\begin{tabular}{l|l} \\textbf{ESD} & SA \\\\ 1 & 2\\end{tabular}\\end{table}\n"
        "\\bibliography{refs}\n\\begin{figure}\\caption{An appendix XYZ figure.}\\end{figure}\\end{document}"
    )
    legends, cells = sc._tex_main_floats(source)
    assert legends == ["Scores of all methods."]
    assert any("ESD" in c for c in cells) and not any("XYZ" in p for p in legends + cells)


# --- heading capitalization -------------------------------------------------------------------


def test_heading_mixing_styles_is_named():
    headings = [("subsection", "Language Model Pre-Training"), ("subsection", "Code generation model Pre-Training"), ("subsection", "Vision Transformer Model Training")]
    hits, _ = sc.inconsistent_heading_case(headings)
    assert hits == ["'Code generation model Pre-Training' has 'generation', 'model' in lower case, others are Title Case"]
    headings = [("subsection", "The SEIRB Framework"), ("subsection", "Proposed Network-Based SEIRB model"), ("subsection", "Data Analysis")]
    assert "'Proposed Network-Based SEIRB model'" in sc.inconsistent_heading_case(headings)[0][0]


def test_heading_case_tie_is_settled_by_the_document():
    headings = [
        ("section", "Main Bijection"),
        ("section", "Lehmer Parking Functions and Catalan Numbers"),
        ("subsection", "Outcomes of lehmer parking functions and balanced spaced parenthesizations"),
        ("subsection", "Set Partitions and Balanced Spaced Parenthesizations"),
    ]
    hits, _ = sc.inconsistent_heading_case(headings, {"lehmer"})
    assert hits == ["'Outcomes of lehmer parking functions and balanced spaced parenthesizations' is Sentence case, others are Title Case"]


def test_heading_case_compares_appendix_headings_with_each_other():
    headings = [("section", "Models underperform on erroneous responses"), ("section", "Models default to assuming correct answers"), ("section", "Ethical Considerations"), ("section", "Model performance by question type"), ("section", "Language Model-Assisted Data Annotation"), ("section", "Natural Language Description Experiments")]
    parts = ["main", "main", "main", "appendix", "appendix", "appendix"]
    hits, _ = sc.inconsistent_heading_case(headings, parts=parts)
    assert any("'Model performance by question type' is Sentence case" in h for h in hits)
    assert any("'Ethical Considerations' is Title Case" in h for h in hits)


def test_heading_case_ignores_math_variables_cross_references_and_optional_lower_words():
    assert sc.heading_case("Relation to the $k$-dimensional Weighted Catalan Numbers") == "title"
    assert sc.heading_case("Probabilistic Neural Networks with t-Distributed Outputs") == "title"
    assert sc.heading_case("When are Reasoning Models Better and When Do They Fail?") == "title"
    assert sc.heading_case("Uncertainty in the simulation results seen in the Figures") == "sentence"
    assert sc.heading_case("Mapping in the Stereo-seq data", {"stereo"}) == "sentence"


def test_heading_case_latex_parts(tmp_path):
    tex = tmp_path / "ms.tex"
    tex.write_text("\\documentclass{article}\\begin{document}\\section{Introduction}\\section{Data Analysis}\\appendix\\section{More results}\\end{document}")
    assert sc._tex_heading_parts(tex, 3) == ["main", "main", "appendix"]


# --- body font size ---------------------------------------------------------------------------


def test_document_wide_fontsize_is_a_template_edit():
    source = "\\documentclass[11pt]{article}\n\\usepackage[review]{acl}\n\\AtBeginDocument{\\fontsize{9}{10.5}\\selectfont}\n\\begin{document}x\\end{document}"
    assert any("\\fontsize" in p for p in template_check.source_problems(source))
    assert any("shrinks the body font" in p for p in template_check.source_problems("\\AtBeginDocument{\\small}"))
    # In a table or caption it is routine.
    assert list(template_check.source_problems("\\begin{table}\\fontsize{8}{9}\\selectfont x\\end{table}")) == []


def test_measured_tex_font_size_follows_a_document_wide_fontsize():
    source = "\\documentclass[11pt]{article}\n\\AtBeginDocument{\\fontsize{9}{10.5}\\selectfont}\n\\begin{document}x\\end{document}"
    assert layout_check.measure_tex(source).font_size == 9.0
    assert layout_check.measure_tex("\\documentclass[11pt]{article}\n% \\AtBeginDocument{\\fontsize{9}{10.5}\\selectfont}\n").font_size == 11.0


def test_template_venues_state_their_body_font_size(monkeypatch):
    monkeypatch.setenv("PAPERPUSH_VENUE_DATA", "bundled")
    from paperpush.requirements import get_requirements

    sizes = {slug: get_requirements(slug).manuscript.font_size_pt for slug in ("acl_arr", "cvpr_2027", "tmlr")}
    assert sizes == {"acl_arr": 11, "cvpr_2027": 10, "tmlr": 10}


# --- title length ------------------------------------------------------------------------------


def _reqs(chars: int | None = None, words: int | None = None) -> ManuscriptRequirements:
    return ManuscriptRequirements(slug="testj", title_page=TitlePageRules(title_max_characters=chars, title_max_words=words))


def test_manuscript_title_over_the_limit_is_reported_without_a_sub_title(tmp_path):
    long_title = "Understanding Frogeye Leaf Spot: " + "A Comprehensive Study of Robustness, Efficiency, Scalability, and Generalization " * 3
    tex = tmp_path / "ms.tex"
    tex.write_text(f"\\documentclass{{article}}\n\\title{{{long_title}}}\n\\begin{{document}}\\maketitle Text.\\end{{document}}\n")
    values = {"manuscript_file": str(tex)}
    issues = _manuscript_title_length_issues(VENUE, _reqs(chars=200, words=30), values)
    assert len(issues) == 2 and "over the 200-character limit" in issues[0].message and "-word limit" in issues[1].message
    assert any("200-character" in i.message for i in _text_field_issues(VENUE, _reqs(chars=200), values))
    # Within the limit, or with a .sub title (checked as the field instead): nothing from the manuscript.
    assert _manuscript_title_length_issues(VENUE, _reqs(chars=1000), values) == []
    assert _manuscript_title_length_issues(VENUE, _reqs(chars=200), {**values, "title": "Short"}) == []


def test_acronym_glossary_entry_in_a_legend_is_a_definition():
    floats = ["Table 1. Benchmark. Detected: share found. FA: new false alarms per manuscript."]
    found = sc.acronyms(["We counted errors per run."], floats=floats, tables=["System , FA", "CLI , 2"])
    assert found.undefined == [] and found.legend_only == []


def test_latex_logo_is_not_an_acronym():
    found = sc.acronyms(["Comments in LATEX source.", "The LATEX build ran.", "A LATEX template."])
    assert found.undefined == []


def test_pdf_code_and_table_lines_are_not_prose():
    assert sc._pdf_table_or_code_line("# PDF (file, REQUIRED)")
    assert sc._pdf_table_or_code_line("pdf_file: manuscript.pdf")
    assert sc._pdf_table_or_code_line("@venue: naacl_2027_demo")
    assert sc._pdf_table_or_code_line("System Detected % Partial FA Min $")
    assert not sc._pdf_table_or_code_line("Model Context Protocol (MCP) Server")
    assert not sc._pdf_table_or_code_line("exposing tools over the Model Context Protocol")
    assert not sc._pdf_table_or_code_line("Note: the results are shown below.")
