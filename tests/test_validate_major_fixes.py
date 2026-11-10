"""Regression tests for the major bugs found reviewing the non-AI checks of ``paperpush validate``.

Each test reproduces one defect: a wrong length, a missed problem, or a false
alarm on an ordinary manuscript. All run offline; DOI lookups are stubbed.
"""

from __future__ import annotations

import importlib
import zipfile
from pathlib import Path

import pytest

from paperpush import ai_policy, anonymity, citation_check as C, colorblind, cover_letter_check as CL, document
from paperpush import figure_check as F, layout_check as L, manuscript as M, pdf_layout as P, references as R
from paperpush import report, reproducibility_check as RP, requirements as RQ, requirements_check as RC, rigor_check as RG, sensitive
from paperpush.database import Field, Venue, get_venue, list_venues
from paperpush.subfile import SubFile
from paperpush.validate import NotApplicable, validate_report
from tests.pdf_builder import text_line, write_pdf

V = importlib.import_module("paperpush.validate")

MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
SUPPLEMENT = Field(id="supplementary_files", label="Supplement", type="filelist")
VENUE = Venue(slug="testj", name="Test Venue", fields=[MANUSCRIPT, SUPPLEMENT])


@pytest.fixture(autouse=True)
def bundled_data(monkeypatch):
    monkeypatch.setenv("PAPERPUSH_VENUE_DATA", "bundled")


def _write(tmp_path: Path, text: str, name: str = "m.txt") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _values(path: Path) -> dict[str, str]:
    return {"manuscript_file": str(path)}


# --- wrong length results --------------------------------------------------------------------


@pytest.mark.parametrize("first", range(100, 400, 7))
def test_line_numbers_of_three_digits_are_stripped_whole(first):
    lines = [f"text words here{first + i}" for i in range(25)]
    assert set(M._strip_line_numbers("\n".join(lines)).split("\n")) == {"text words here"}


def test_a_real_structured_abstract_keeps_its_labels():
    names = ["Abstract", "Background", "Methods", "Results", "Background", "Methods"]
    assert M.structured_abstract_labels(names, [0, 10, 200, 400, 900, 5000]) == {1, 2, 3}


def test_a_repeated_heading_far_from_the_abstract_is_no_structured_abstract():
    names = ["Abstract", "Background", "Results", "Methods", "Methods"]
    assert M.structured_abstract_labels(names, [0, 1000, 9000, 20000, 40000]) == set()


def test_a_subsection_repeating_the_first_section_is_no_structured_abstract():
    assert M.structured_abstract_labels(["Abstract", "Background", "Background", "Results"]) == set()


def test_section_order_ignores_a_repeat_after_the_references(tmp_path, monkeypatch):
    monkeypatch.setattr(RC, "manuscript_headings", lambda path: ["abstract", "introduction", "results", "methods", "references", "methods", "results"])
    reqs = RQ.ManuscriptRequirements.from_dict("testj", {"sections": {"order": ["Introduction", "Results", "Methods"]}})
    assert RC._section_order_issues(MANUSCRIPT, tmp_path / "m.txt", reqs).issues == []


@pytest.mark.parametrize("line", ["results. We compared the two groups", "methods: these were described above"])
def test_a_wrapped_sentence_is_no_run_in_heading(line):
    assert M._run_in_heading(line) is None


def test_a_capitalized_run_in_heading_is_still_read():
    assert M._run_in_heading("Funding: NIH grant 12")[0] == "Funding"


def test_whole_document_count_leaves_references_out(tmp_path):
    refs = "\n".join(f"{i}. Smith J. A long reference title about many things. Nature 1, {i} (2020)." for i in range(1, 31))
    path = _write(tmp_path, f"# Title\n\n## Introduction\n\n{'word ' * 300}\n\n## References\n\n{refs}\n", "m.md")
    words, missing = M.words_excluding(path, {"references": "references"}, {"references": "@references"}, whole=True)
    assert not missing and words < 320


def test_whole_document_count_keeps_text_after_the_references(tmp_path):
    path = _write(tmp_path, f"Introduction\n\n{'word ' * 50}\n\nReferences\n\n1. Smith J. A title. Nature 1, 1 (2020).\n\nAcknowledgements\n\n{'thanks ' * 40}\n")
    words, _ = M.words_excluding(path, {"references": "references"}, {"references": "@references"}, whole=True)
    assert words > 85


@pytest.mark.parametrize("slug, article_type, limit", [("nature", "Submit - Review", 7000), ("nature", "Submit - Perspective", 7000), ("cell", "Review", 8000)])
def test_article_type_limit_replaces_the_inherited_one(slug, article_type, limit):
    reqs = RQ.get_requirements(slug)
    resolved = RQ.resolve(reqs, {reqs.article_type_field: article_type})
    assert resolved.manuscript.max_words == limit
    assert resolved.manuscript.max_words_before_refs is None


def test_article_type_without_a_limit_keeps_the_top_level_one():
    assert RQ.resolve(RQ.get_requirements("nature"), {}).manuscript.max_words_before_refs == 4300


def test_wrapped_author_year_entries_are_counted(tmp_path):
    entries = "".join(f"Author{c}, A. and Other, B. A study of something quite\nlong that wraps. Journal of Things, 12:1-9, 20{10 + i}.\n" for i, c in enumerate("ABCDEF"))
    assert M.reference_count(_write(tmp_path, f"Intro.\n\nReferences\n\n{entries}")) == 6


def test_a_wrapped_author_list_stays_with_its_entry(tmp_path):
    tail = "Smith, J., Jones, K., King, Q.,\nGray, R., and Hall, S. (2020). A title. Nature, 1, 1-2.\nBrown, L. (2019). Another title. Cell, 2, 3-4.\n"
    entries = M.reference_entries(_write(tmp_path, f"Intro.\n\nReferences\n\n{tail}"))
    assert len(entries) == 2 and entries[0].startswith("Smith")


def test_text_after_the_references_is_not_an_entry(tmp_path):
    body = "Intro.\n\nReferences\n\nSmith, J. (2020). A title. Nature, 1, 1-2.\n\nAcknowledgments\nSupported by the NIH since 2019.\n"
    assert M.reference_count(_write(tmp_path, body)) == 1


# --- problems it missed ----------------------------------------------------------------------

_TERMS = [("author name", "Jane Quincy")]
_INTRO = "Introduction\n\nWe study things in depth.\n\nReferences\n\n[1] A. Smith. A paper. Nature 2020.\n\n"


def test_lettered_appendix_after_references_is_scanned_for_identity(tmp_path):
    path = _write(tmp_path, _INTRO + "A Implementation details\n\nOur code is at https://github.com/somelab/ourtool and was written by Jane Quincy.\n")
    details = [f.detail for f in anonymity.scan_submission([path], _TERMS, {}, check_repos=False)]
    assert any("Jane Quincy" in d for d in details) and any("github.com/somelab" in d for d in details)


def test_a_self_citation_in_a_wrapped_reference_stays_permitted(tmp_path):
    path = _write(tmp_path, _INTRO + "[2] Jane Quincy and others.\nA survey of deep models\nJournal of Models 12, 2021.\n")
    assert not [f for f in anonymity.scan_submission([path], _TERMS, {}, check_repos=False) if "Jane Quincy" in f.detail]


def test_line_numbers_near_the_edge_are_not_page_numbers(tmp_path):
    # As LaTeX's lineno package sets them: smaller, a point off the text's baseline, so rows of their own.
    ops = ""
    for n in range(31):
        y = 720 - 22 * n
        ops += text_line(72, y, "measurement sample protein assay cohort signal growth tissue", size=12) + text_line(56, y + 1.5, f"{n + 10}", size=6)
    pdf = L.measure_pdf(P.read_pages(write_pdf(tmp_path / "a.pdf", [ops] * 3)))
    assert pdf.page_number_pages == 0 and pdf.line_number_pages == 3


def test_a_page_number_beside_line_numbers_still_counts(tmp_path):
    ops = text_line(303, 40, "2", size=12)
    for n in range(28):
        y = 700 - 22 * n
        ops += text_line(72, y, "measurement sample protein assay cohort signal growth tissue", size=12) + text_line(56, y + 1.5, f"{n + 10}", size=6)
    pdf = L.measure_pdf(P.read_pages(write_pdf(tmp_path / "a.pdf", [ops] * 3)))
    assert pdf.page_number_pages == 3 and pdf.line_number_pages == 3


def test_every_page_of_a_figure_pdf_is_read(tmp_path):
    pytest.importorskip("pdfminer")
    path = write_pdf(tmp_path / "f.pdf", [text_line(72, 700, "Large label", size=12), text_line(72, 700, "tiny label", size=3)], width=300, height=300)
    fig = F._pdf_figure(path, "figure_files", RQ.FigureRules(), 1)
    assert min(t.size for t in fig.texts) == pytest.approx(3, abs=0.1)


def test_every_page_of_a_figure_pdf_is_read_for_colours(tmp_path):
    pytest.importorskip("pypdf")
    path = write_pdf(tmp_path / "f.pdf", ["0.8 0.1 0.1 rg 10 10 100 100 re f\n", "0.1 0.1 0.8 rg 10 10 100 100 re f\n"], width=300, height=300)
    colours = {colour for drawing in colorblind.pdf_figure_drawings(path, "f.pdf") for colour, _ in drawing.colours}
    assert len(colours) >= 2


def test_a_rule_no_check_reads_is_reported_as_not_implemented():
    assert "figures.min_dpi_combination" not in RC.HANDLED_RULES


TABLES = Field(id="table_files", label="Tables", type="filelist")
TABLE_VENUE = Venue(slug="testj", name="Test Venue", fields=[MANUSCRIPT, TABLES])
_BODY = "Introduction\n\nAs Table 1 shows, it worked.\n\n"
_REFS = "References\n\n1. Smith J. A title. Nature 1 (2020).\n\n"
_TABLE = "Table 1. Baseline characteristics.\n\n"


def _placement(tmp_path, placement, text, table_file=False, name="m.txt", venue=TABLE_VENUE):
    values = _values(_write(tmp_path, text, name))
    if table_file:
        values["table_files"] = str(_write(tmp_path, "a,b\n", "t1.csv"))
    reqs = RQ.ManuscriptRequirements.from_dict("testj", {"tables": {"placement": placement}})
    return RC._table_placement_check(venue, reqs, values)


def test_table_placement_reads_the_manuscript_rule_too(tmp_path):
    reqs = RQ.ManuscriptRequirements.from_dict("testj", {"manuscript": {"tables_placement": "end"}})
    outcome = RC._table_placement_check(TABLE_VENUE, reqs, _values(_write(tmp_path, _BODY + _TABLE + _REFS)))
    assert "before the references" in outcome.issues[0].message


def test_table_placement_no_rule_does_not_apply(tmp_path):
    reqs = RQ.ManuscriptRequirements.from_dict("testj", {})
    with pytest.raises(NotApplicable):
        RC._table_placement_check(TABLE_VENUE, reqs, _values(_write(tmp_path, _BODY)))


@pytest.mark.parametrize("placement", ["end_of_manuscript"])
def test_tables_after_the_references_satisfy_end(tmp_path, placement):
    assert _placement(tmp_path, placement, _BODY + _REFS + _TABLE).issues == []


def test_table_before_the_references_breaks_end(tmp_path):
    assert "Table 1 comes before" in _placement(tmp_path, "end_of_manuscript", _BODY + _TABLE + _REFS).issues[0].message


def test_end_without_a_reference_list_is_unchecked(tmp_path):
    assert _placement(tmp_path, "end_of_manuscript", _BODY + _TABLE).issues[0].unchecked


def test_inline_table_in_the_text_passes(tmp_path):
    assert _placement(tmp_path, "inline", _BODY + _TABLE + _REFS).issues == []


def test_inline_with_every_table_at_the_end_is_warned(tmp_path):
    assert "after the references" in _placement(tmp_path, "inline", _BODY + _REFS + _TABLE).issues[0].message


@pytest.mark.parametrize("text", [_BODY + _TABLE + _REFS, _BODY + _REFS + _TABLE])
def test_end_or_inline_accepts_either(tmp_path, text):
    assert _placement(tmp_path, "end_or_inline", text).issues == []


def test_separate_with_a_table_file_passes(tmp_path):
    assert _placement(tmp_path, "separate_files", _BODY + _REFS, table_file=True).issues == []


def test_separate_with_tables_only_in_the_manuscript_is_warned(tmp_path):
    assert "as a separate file" in _placement(tmp_path, "separate_files", _BODY + _TABLE + _REFS).issues[0].message


def test_separate_on_a_form_with_no_table_upload_is_quiet(tmp_path):
    assert _placement(tmp_path, "separate_files", _BODY + _TABLE + _REFS, venue=VENUE).issues == []


def test_in_manuscript_rule_with_only_a_table_file_is_warned(tmp_path):
    assert "no tables found" in _placement(tmp_path, "inline", _BODY + _REFS, table_file=True).issues[0].message


def test_a_manuscript_with_no_tables_does_not_apply(tmp_path):
    with pytest.raises(NotApplicable):
        _placement(tmp_path, "end_of_manuscript", "Introduction\n\nNo displays here.\n\n" + _REFS)


def test_supplementary_tables_are_not_main_tables(tmp_path):
    assert _placement(tmp_path, "end_of_manuscript", _BODY + "Supplementary Table 1. Extra.\n\n" + _REFS + _TABLE).issues == []


_TEX_TABLE = "\\begin{table}\\caption{Baseline}\\label{t}\\end{table}\n"
_TEX_HEAD = "\\documentclass{article}\n%s\\begin{document}\nAs Table~\\ref{t} shows.\n"
_TEX_BIB = "\\bibliography{refs}\n"


@pytest.fixture
def uncompiled(monkeypatch):
    monkeypatch.setattr(document.Document, "pdf", None)


def test_latex_table_before_the_bibliography_breaks_end(tmp_path, uncompiled):
    text = _TEX_HEAD % "" + _TEX_TABLE + _TEX_BIB + "\\end{document}\n"
    assert "before the references" in _placement(tmp_path, "end_of_manuscript", text, name="m.tex").issues[0].message


def test_latex_table_after_the_bibliography_satisfies_end(tmp_path, uncompiled):
    text = _TEX_HEAD % "" + _TEX_BIB + _TEX_TABLE + "\\end{document}\n"
    assert _placement(tmp_path, "end_of_manuscript", text, name="m.tex").issues == []


def test_endfloat_counts_as_placing_tables_at_the_end(tmp_path, uncompiled):
    text = _TEX_HEAD % "\\usepackage{endfloat}\n" + _TEX_TABLE + _TEX_BIB + "\\end{document}\n"
    assert _placement(tmp_path, "end_of_manuscript", text, name="m.tex").issues == []


def test_table_placement_runs_in_a_full_report(tmp_path, monkeypatch):
    monkeypatch.setattr(RQ, "_load_raw", lambda: {"$aliases": {"sections": {}, "statements": {}}, "testj": {"tables": {"placement": "end_of_manuscript"}}})
    path = _write(tmp_path, _BODY + _TABLE + _REFS)
    rep = validate_report(SubFile(venue="testj", values=_values(path)), TABLE_VENUE, check_links=False, check_references=False, check_sensitive=False, check_hidden_text=False, check_openreview=False)
    result = next(r for r in rep.results if r.id == "guidelines.table_placement")
    assert result.status == report.WARNING
    assert not [r for r in rep.results if r.id.endswith("tables.placement") and r.status == report.NOT_IMPLEMENTED]


def test_letter_to_nature_naming_nature_communications_is_caught():
    found = CL.other_venue_mentions("We submit this manuscript for consideration in Nature Communications.", get_venue("nature"), list_venues())
    assert [name for name, _ in found] == ["Nature Communications"]
    assert not CL.names_venue("We submit this manuscript for consideration in Nature Communications.", get_venue("nature"))


def test_letter_to_nature_communications_naming_nature_is_caught():
    found = CL.other_venue_mentions("We submit this manuscript for consideration in Nature.", get_venue("nature_communications"), list_venues())
    assert [name for name, _ in found] == ["Nature"]


def test_letter_naming_its_own_venue_is_quiet():
    venue = get_venue("nature_communications")
    text = "We submit this manuscript for consideration in Nature Communications."
    assert CL.other_venue_mentions(text, venue, list_venues()) == [] and CL.names_venue(text, venue)


def test_retractions_are_unchecked_when_nothing_could_be_looked_up(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "_reference_issues", lambda *a, **k: [])

    def no_answer(*a, **k):
        raise V.Unchecked("the registries did not answer")

    monkeypatch.setattr(V, "_unmatched_reference_issues", no_answer)
    path = _write(tmp_path, "Intro.\n\nReferences\n\n1. Smith J. A title. Nature 1, 1 (2020).\n")
    rep = validate_report(SubFile(venue="testj", values=_values(path)), VENUE, check_links=False, check_manuscript=False, check_sensitive=False, check_hidden_text=False, check_openreview=False)
    assert next(r for r in rep.results if r.id == "integrity.retractions").status == report.UNCHECKED


# --- false alarms ----------------------------------------------------------------------------


def _record(title: str, family: str, year: int) -> dict:
    return {"title": title, "author": [{"family": family, "given": "X"}], "issued": {"date-parts": [[year]]}}


def _stub_registry(monkeypatch, records: dict[str, dict]) -> None:
    monkeypatch.setattr(R, "_resolve_doi", lambda doi, timeout=6.0: (R.DOI_FOUND, records[doi]) if doi in records else (R.DOI_MISSING, None))


def test_word_hyperlink_targets_are_not_reference_entries(tmp_path, monkeypatch):
    body = '<w:document xmlns:w="w"><w:body><w:p><w:r><w:t>Intro text.</w:t></w:r></w:p><w:p><w:r><w:t>References</w:t></w:r></w:p>' "<w:p><w:r><w:t>1. Smith J. Deep sequencing of the mouse brain. Nature 2020;586:1-5. https://doi.org/10.1038/abc123</w:t></w:r></w:p></w:body></w:document>"
    rels = '<Relationships><Relationship Id="r" Type="hyperlink" Target="https://doi.org/10.1038/abc123" TargetMode="External"/></Relationships>'
    path = tmp_path / "m.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", body)
        z.writestr("word/_rels/document.xml.rels", rels)
    _stub_registry(monkeypatch, {"10.1038/abc123": _record("Deep sequencing of the mouse brain", "Smith", 2020)})
    assert R.scan_references([path]) == []


@pytest.mark.parametrize("head, tails", [("10.1038/s41586-", ("020-2649-2", "019-1234-5")), ("10.1016/j.cell.2020.", ("01.001", "02.002"))])
def test_dois_wrapped_mid_doi_stay_distinct(tmp_path, head, tails):
    refs = "".join(f"{i}. Author A. A title of work. Nature 1, 1 (2020). https://doi.org/{head}\n{tail}\n" for i, tail in enumerate(tails, 1))
    citations = R.collect_citations([_write(tmp_path, f"Intro.\n\nReferences\n\n{refs}")])
    assert sorted(c.doi for c in citations) == sorted((head + tail).lower() for tail in tails)


def test_a_sentence_after_a_doi_is_not_part_of_it(tmp_path):
    citations = R.collect_citations([_write(tmp_path, "Intro.\n\nReferences\n\n1. Author A. A title. Nature 1 (2020). doi:10.1038/abc123.\n2. Other B. Another. Cell 2 (2019).\n")])
    assert [c.doi for c in citations] == ["10.1038/abc123"]


def test_dataset_doi_after_the_references_is_no_reference(tmp_path):
    text = "Intro.\n\nReferences\n\n1. Smith J. A title. Nature 1 (2020). doi:10.1038/abc123\n\nData availability\n\nAll data are at https://doi.org/10.5281/zenodo.1234567\n"
    in_list = {c.doi: c.in_reference_list for c in R.collect_citations([_write(tmp_path, text)])}
    assert in_list == {"10.1038/abc123": True, "10.5281/zenodo.1234567": False}


def test_citations_in_methods_after_the_references_count():
    text = "Introduction\nWe saw effects in Figure 1.\n\nReferences\n1. Smith J. Nature 2020.\n\nMethods\nAs in Figure 2 and Extended Data Fig. 1 we measured.\n\nFigure 1. Legend one.\n\nFigure 2. Legend two.\n\nExtended Data Fig. 1. Legend three.\n"
    cited = {(m.series, m.number) for m in C.mentions(text) if not m.caption}
    assert cited == {("figure", 1), ("figure", 2), ("ed_figure", 1)}


def test_a_citation_inside_the_reference_list_is_still_ignored():
    text = "Intro cites Figure 1.\n\nReferences\n1. Smith J. See Figure 9 of that paper. Nature 2020.\n\nFigure 1. Legend.\n"
    assert {m.number for m in C.mentions(text) if not m.caption} == {1}


@pytest.mark.parametrize(
    "text, letters",
    [
        ("Figure 1A, B, and C show this.", ["A", "B", "C"]),
        ("Figures 2a and b show this.", ["a", "b"]),
        ("Figure 5a, b and c were similar.", ["a", "b", "c"]),
        ("Fig. 2a and a control were run.", ["a"]),
        ("Figure 2A and B cells were counted.", ["A"]),
    ],
)
def test_panel_lists(text, letters):
    assert [p.letter for p in C.panel_mentions(text)] == letters


def test_matplotlib_bold_svg_font_shorthand():
    css = F._css_declarations("font: 700 10px 'DejaVu Sans'")
    assert css["font-size"] == "10px" and css["font-family"] == "'DejaVu Sans'" and css["font-weight"] == "bold"


def test_unitless_svg_font_size_is_still_read():
    assert F._css_declarations("font: 12 Arial")["font-size"] == "12"


def test_trial_number_after_the_references_is_found(tmp_path):
    path = _write(tmp_path, "Patients were enrolled in this randomized controlled trial.\n\nReferences\n\n1. Smith J. A title. Nature 1 (2020).\n\nTrial registration: NCT01234567\n")
    assert RG._trial_registration_check(VENUE, _values(path)).issues == []


def test_code_link_after_the_references_is_found(tmp_path):
    path = _write(tmp_path, "We ran a python script and simulations with custom scripts.\n\nReferences\n\n1. Smith J. A title. Nature 1 (2020).\n\nCode availability\n\nCode is at https://github.com/lab/tool\n")
    assert RP.code_sources(VENUE, _values(path)).repos == ["https://github.com/lab/tool"]


def test_design_items_in_the_supplement_count(tmp_path):
    manuscript = _write(tmp_path, "Mice were housed. Mice were treated. Mice were weighed.\n\nReferences\n\n1. Smith J. A title. Nature 1 (2020).\n")
    supplement = _write(tmp_path, "Investigators were blinded. Animals were randomized to groups. A power analysis set the sample size. No animals were excluded. Male and female mice were used.\n", "s.txt")
    outcome = RG._design_check(VENUE, {"manuscript_file": str(manuscript), "supplementary_files": str(supplement)})
    assert outcome.issues == []


def test_cell_lines_named_only_in_references_do_not_apply(tmp_path):
    path = _write(tmp_path, "A theory paper.\n\nReferences\n\n1. Smith J. Autophagy flux in HeLa cells. J Cell Biol 1 (2020).\n")
    with pytest.raises(NotApplicable):
        RG._cell_lines_check(VENUE, _values(path), False)


def test_protein_recruitment_is_not_human_recruitment(tmp_path):
    path = _write(tmp_path, "BRCA1 was recruited to damaged chromatin. RAD51 was recruited to foci. 53BP1 was recruited to breaks.\n")
    with pytest.raises(NotApplicable):
        RG._design_check(VENUE, _values(path))


def test_human_recruitment_is_still_a_human_study(tmp_path):
    path = _write(tmp_path, "We recruited 40 patients. The patients were followed. Patients were seen monthly, and the cohort was stable.\n")
    assert RG._design_check(VENUE, _values(path)).issues


def test_source_data_is_not_a_code_statement(tmp_path):
    path = _write(tmp_path, "Cells were grown.\n\nData availability\n\nSource data are provided with this paper. All other data are available from the corresponding author upon reasonable request.\n")
    with pytest.raises(NotApplicable):
        RP._code_availability_check(VENUE, _values(path), False)


@pytest.mark.parametrize("sentence", ["We used images of faces to elicit emotional responses.", "We used perplexity to evaluate the text.", "We used anthropic disturbance scores to assess the text."])
def test_ordinary_words_are_not_ai_tools(sentence):
    assert ai_policy.classify(sentence) is None


@pytest.mark.parametrize("sentence", ["Claude (Anthropic) was used to edit the text.", "Perplexity AI was used to edit the text.", "We used Elicit to draft the text."])
def test_ai_tools_are_still_recognized(sentence):
    assert ai_policy.classify(sentence)[0]


@pytest.mark.parametrize("text", ["In the forward pass: activations are cached", "Second pass: alignments were filtered", "Secret: unlabelled samples were held"])
def test_prose_labels_are_not_credentials(text):
    assert not list(sensitive._iter_secret_findings("m.txt", text))


@pytest.mark.parametrize("text", ["password: hunter2hunter", "pass: Xk39fj2kd9", 'secret = "abcdefghij"'])
def test_real_credentials_are_still_flagged(text):
    assert list(sensitive._iter_secret_findings("m.txt", text))
