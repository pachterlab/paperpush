"""Regression tests for the guideline checks and the manuscript text they read."""

from __future__ import annotations

import os
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("PAPERPUSH_VENUE_DATA", "bundled")

from paperpush import ai_policy, database, layout_check, requirements, requirements_check as check, template_check  # noqa: E402
from paperpush import manuscript as m  # noqa: E402
from paperpush.validate import Checks  # noqa: E402
from tests.pdf_builder import text_line, write_pdf  # noqa: E402

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


@pytest.fixture(autouse=True)
def no_latex_builds(monkeypatch):
    """Read LaTeX as source, whether or not this machine has a TeX toolchain."""
    monkeypatch.setattr(m, "build_pdf", lambda path: None)


def words(n: int, word: str = "lorem") -> str:
    return " ".join([word] * n)


def para(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def make_docx(path: Path, body: str) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {NS}><w:body>{body}</w:body></w:document>')
    return path


def scanned_pdf(path: Path) -> Path:
    write_pdf(path, ["0 0 m 10 10 l S\n"] * 5)
    return path


def venue_values(slug: str, manuscript: Path, **extra) -> tuple:
    venue = database.get_venue(slug)
    field = check._fields_for(venue, "manuscript")[0]
    return venue, {field.id: str(manuscript), **extra}


def run(slug: str, manuscript: Path, check_id: str, reqs=None, **extra):
    venue, values = venue_values(slug, manuscript, **extra)
    fn = {"guidelines.length": check._length_check, "guidelines.sections": check._sections_check, "guidelines.statements": check._statements_check, "guidelines.section_order": check._section_order_check, "guidelines.reference_count": check._reference_count_check, "guidelines.reference_style": check._reference_style_check, "guidelines.counts": check._counts_check, "guidelines.template": check._template_check, "guidelines.cover_letter": check._cover_letter_check, "guidelines.ai_policy": ai_policy.check}[check_id]
    checks = Checks()
    checks.run(check_id, "t", "c", fn, venue, reqs or requirements.get_requirements(slug), values)
    return checks.results[-1]


def markdown(path: Path, sections: dict[str, int]) -> Path:
    path.write_text("# Title of the work\n\n" + "\n\n".join(f"## {name}\n\n{words(n)}" for name, n in sections.items()) + "\n\n## References\n\n1. A reference\n", encoding="utf-8")
    return path


# --- count_excludes -------------------------------------------------------------


@pytest.mark.parametrize(
    ("slug", "sections"),
    [
        ("jama", {"Abstract": 350, "Introduction": 400, "Methods": 1000, "Results": 800, "Discussion": 700, "Figure Legends": 400}),
        ("nature", {"Abstract": 200, "Introduction": 1000, "Results": 2000, "Discussion": 1000, "Methods": 3000, "Figure Legends": 800}),
        ("lancet", {"Abstract": 300, "Introduction": 500, "Methods": 1000, "Results": 1000, "Discussion": 800, "Figure Legends": 300}),
        ("cell", {"Summary": 150, "Introduction": 1500, "Results": 3000, "Discussion": 2000, "STAR Methods": 4000}),
        ("frontiers_genetics", {"Abstract": 300, "Introduction": 3000, "Methods": 4000, "Results": 3000, "Discussion": 1800, "Acknowledgments": 200, "Funding": 300}),
    ],
)
def test_count_excludes_within_limit_once_excluded_parts_are_left_out(tmp_path, slug, sections):
    assert requirements.get_requirements(slug).manuscript.count_excludes
    result = run(slug, markdown(tmp_path / "ms.md", sections), "guidelines.length")
    assert result.status == "passed", [i.message for i in result.issues]


def _count_excludes_venues() -> list[str]:
    return [v.slug for v in database.list_venues() if (r := requirements.get_requirements(v.slug)) and r.manuscript.count_excludes and r.manuscript.max_words_before_refs and not r.manuscript.main_text_end_headings and not check._field_has_length_limits(check._fields_for(v, "manuscript")[0])]


def _manuscript_for(slug: str, path: Path, body: int) -> Path:
    excludes = " ".join(requirements.get_requirements(slug).manuscript.count_excludes).lower()
    sections = {"Abstract": 400 if "abstract" in excludes else 0, "Introduction": body, "Methods": 3000 if "method" in excludes else 0, "Acknowledgements": 300 if "acknowledg" in excludes else 0, "Funding": 200 if "funding" in excludes else 0, "Figure Legends": 500 if "figure legend" in excludes or "caption" in excludes else 0}
    return markdown(path, {name: n for name, n in sections.items() if n})


def test_count_excludes_sweep_covers_many_venues():
    assert len(_count_excludes_venues()) >= 20


@pytest.mark.parametrize("slug", _count_excludes_venues())
def test_count_excludes_every_venue(tmp_path, slug):
    limit = requirements.get_requirements(slug).manuscript.max_words_before_refs
    assert run(slug, _manuscript_for(slug, tmp_path / "ok.md", limit - 50), "guidelines.length").status == "passed"
    assert run(slug, _manuscript_for(slug, tmp_path / "long.md", limit + 500), "guidelines.length").status in ("failed", "warning")


def test_count_excludes_over_limit_with_everything_located_is_an_error(tmp_path):
    result = run("jama", markdown(tmp_path / "ms.md", {"Abstract": 300, "Introduction": 2000, "Methods": 3000, "Figure Legends": 100, "Acknowledgements": 20, "Tables": 50}), "guidelines.length")
    assert result.status == "failed"
    assert "without the parts" in result.issues[0].message


def test_count_excludes_warns_when_a_part_cannot_be_located(tmp_path):
    # A PDF has no paragraph ends to bound a caption, and a table is not locatable at all.
    lines = [text_line(50, 700, f"Introduction {words(10)} Table 1 shows it.")]
    path = write_pdf(tmp_path / "ms.pdf", ["".join(text_line(50, 700 - 12 * i, words(14)) for i in range(50)) + "".join(lines)] * 6)
    reqs = requirements.get_requirements("jama")
    reqs = replace(reqs, manuscript=replace(reqs.manuscript, count_excludes=["tables"], max_words_before_refs=100))
    result = run("jama", path, "guidelines.length", reqs=reqs)
    assert result.status == "warning"
    assert "could not locate tables" in result.issues[0].message


def test_count_excludes_structured_abstract_labels_stay_in_the_abstract(tmp_path):
    path = tmp_path / "ms.txt"
    path.write_text(f"Title\n\nAbstract\nBackground\n{words(100)}\nResults\n{words(100)}\n\nBackground\n{words(30)}\nResults\n{words(20)}\n\nReferences\n1. A\n", encoding="utf-8")
    wordings = {"abstract": "Abstract", "background": "Background", "results": "Results"}
    net, missing = m.words_excluding(path, wordings, {"abstract": "Abstract"})
    assert net == 1 + 30 + 1 + 20 and not missing  # the title above the Abstract is left out with it


def test_count_excludes_tex_abstract_environment_and_tables(tmp_path):
    path = tmp_path / "m.tex"
    path.write_text("\\documentclass{article}\n\\begin{document}\n\\begin{abstract}\n" + words(100) + "\n\\end{abstract}\n\\section{Introduction}\n" + words(50) + "\n\\begin{table}\\caption{" + words(40) + "}\\end{table}\n\\end{document}\n", encoding="utf-8")
    net, missing = m.words_excluding(path, {"introduction": "Introduction"}, {"abstract": "Abstract", "tables": "@tables"})
    assert net == 51 and not missing


# --- AI policy ------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Dr|Claude|Dupont|c@x.edu|MIT|USA|Paris|yes", "Dr|Ann|Bard|a@x.edu|MIT|USA|Paris|no"])
def test_a_person_named_like_a_tool_is_not_an_ai_author(name):
    venue = database.get_venue("nature")
    author_field = next(f for f in venue.fields if f.type == "authorlist")
    assert ai_policy._ai_authors(venue, {author_field.id: name}) == []


@pytest.mark.parametrize("name", ["Dr|Chat|GPT|c@x.edu|MIT|USA|Paris|yes", "Dr||ChatGPT|c@x.edu|MIT|USA|Paris|yes", "Dr|Google|Gemini|c@x.edu|MIT|USA|Paris|yes"])
def test_a_tool_listed_as_author_is_still_caught(name):
    venue = database.get_venue("nature")
    author_field = next(f for f in venue.fields if f.type == "authorlist")
    assert ai_policy._ai_authors(venue, {author_field.id: name})


@pytest.mark.parametrize(
    ("sentence", "disclosure", "negative"),
    [
        ("The authors used ChatGPT to draft the discussion text but did not use it to analyze data.", True, False),
        ("We used ChatGPT without modification to draft the introduction text.", True, False),
        ("GPT-4 did not improve accuracy on clinical text classification.", False, False),
        ("GPT-4 generated more accurate summaries of clinical text than Llama 3.", False, False),
        ("No generative AI was used in the preparation of this manuscript.", True, True),
        ("We wrote this text without the use of ChatGPT.", True, True),
    ],
)
def test_classify_separates_use_denial_and_research_subject(sentence, disclosure, negative):
    verdict = ai_policy.classify(sentence)
    assert verdict[:2] == (disclosure, negative)


def test_research_sentence_does_not_satisfy_required_ai_statement(tmp_path):
    path = tmp_path / "ms.txt"
    path.write_text("Intro\n\nResults\nGPT-4 did not improve accuracy on clinical text classification.\n\nReferences\n1. X\n", encoding="utf-8")
    result = run("cvpr_2027", path, "guidelines.statements")
    assert any("AI-use" in i.message for i in result.issues)


# --- text extraction ------------------------------------------------------------


def test_docx_ignores_field_codes_deletions_and_fallback_copies(tmp_path):
    field = "<w:p><w:r><w:instrText>ADDIN EN.CITE " + words(300, "code") + "</w:instrText></w:r><w:r><w:t>(1)</w:t></w:r></w:p>"
    deleted = "<w:p><w:del><w:r><w:delText>" + words(500, "gone") + "</w:delText></w:r></w:del></w:p>"
    box = '<w:p><mc:AlternateContent xmlns:mc="x"><mc:Choice>' + para(words(5, "box")) + "</mc:Choice><mc:Fallback>" + para(words(5, "box")) + "</mc:Fallback></mc:AlternateContent></w:p>"
    path = make_docx(tmp_path / "ms.docx", para(words(100)) + field + deleted + box)
    assert m.total_words(path) == 106


def line_numbered_page(start: int, lines: list[str]) -> str:
    return "\n".join(f"{line}{start + i}" for i, line in enumerate(lines))


def test_line_numbers_glued_to_lines_are_stripped():
    page = line_numbered_page(1, ["Title of it", "Abstract", words(9), words(9), "1 Introduction", words(9), words(9), words(9), "2 Methods", words(9), "References", words(9)])
    text = m._strip_line_numbers(page).split("\n")
    assert text[1] == "Abstract" and text[4] == "1 Introduction" and text[10] == "References"


def test_every_fifth_line_numbering_is_stripped():
    lines = [words(9) for _ in range(30)]
    for i in range(4, 30, 5):
        lines[i] += str(i + 1)
    lines[9] = "References10"
    assert m._strip_line_numbers("\n".join(lines)).split("\n")[9] == "References"


def test_ordinary_trailing_numbers_are_kept():
    page = "\n".join(f"{words(5)} {n}" for n in (2019, 12, 7, 2020, 3, 44, 9, 1))
    assert m._strip_line_numbers(page) == page


@pytest.mark.parametrize("heading", ["Reference List", "Reference list:", "References Cited", "Cited References"])
def test_reference_heading_wordings(tmp_path, heading):
    path = tmp_path / "ms.txt"
    path.write_text(f"Body text here.\n\n{heading}\n\n1. A ref\n", encoding="utf-8")
    assert m.words_before_references(path) == 3


def test_tex_input_is_followed(tmp_path):
    (tmp_path / "intro.tex").write_text("\\section{Introduction}\n" + words(900) + "\n", encoding="utf-8")
    main = tmp_path / "main.tex"
    main.write_text("\\documentclass{article}\n\\begin{document}\n\\input{intro}\n\\bibliography{refs}\n\\end{document}\n", encoding="utf-8")
    assert m.words_before_references(main) >= 900
    assert "introduction" in m.headings(main)


def test_zip_follows_input_from_its_root_in_order(tmp_path):
    path = tmp_path / "b.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("main.tex", "\\documentclass{article}\n\\begin{document}\n\\input{z_background}\n\\input{a_methods}\n\\bibliography{r}\n\\end{document}\n")
        zf.writestr("a_methods.tex", "\\section{Methods}\n" + words(10))
        zf.writestr("z_background.tex", "\\section{Background}\n" + words(10))
    assert m.headings(path)[:2] == ["background", "methods"]
    assert m.words_before_references(path) >= 20


def test_scanned_pdf_is_unreadable_not_empty(tmp_path):
    path = scanned_pdf(tmp_path / "s.pdf")
    assert m.manuscript_text(path) is None and m.title_page_text(path) is None
    assert run("nature", path, "guidelines.length").status == "unchecked"
    assert run("nature", path, "guidelines.ai_policy").status == "unchecked"


def test_scanned_cover_letter_is_unchecked(tmp_path):
    path = scanned_pdf(tmp_path / "c.pdf")
    venue = database.get_venue("plos_biology")
    checks = Checks()
    checks.run("g", "t", "c", check._cover_letter_check, venue, requirements.get_requirements("plos_biology"), {"cover_letter": str(path)})
    assert checks.results[-1].status == "unchecked"


def test_tex_word_count_skips_preamble_and_keys(tmp_path):
    path = tmp_path / "m.tex"
    preamble = "\\documentclass{article}\n\\newcommand{\\foo}{" + words(100, "macro") + "}\n\\begin{document}\n"
    path.write_text(preamble + "word\\cite{smith2020,jones}\\label{x}\\ref{y} " + words(9) + "\n\\end{document}\n", encoding="utf-8")
    assert m.words_before_references(path) == 10


def test_tex_preamble_title_and_authors_are_kept(tmp_path):
    assert "Great Title" in m._tex_to_text("\\documentclass{article}\\title{Great Title}\\newcommand{\\x}{no}\\begin{document}Body\\end{document}")


def test_tex_total_words_includes_references(tmp_path):
    path = tmp_path / "m.tex"
    path.write_text("\\documentclass{article}\n\\begin{document}\n" + words(100) + "\n\\begin{thebibliography}{9}\n\\bibitem{a} " + words(500, "ref") + "\n\\end{thebibliography}\n\\end{document}\n", encoding="utf-8")
    assert m.total_words(path) >= 600


@pytest.mark.parametrize(
    ("source", "expected"),
    [("Jos\\'e", "Jos\u00e9"), ('M\\"{u}ller', "M\u00fcller"), ("S{\\o}rensen", "S\u00f8rensen"), ("Na\\-ive", "Naive"), ("Erd\\H{o}s", "Erd\u0151s"), ("{\\'e}", "\u00e9"), ("\\'{\\i}", "\u00ed"), ("\\c{c}", "\u00e7"), ("\\omega", "\\omega")],
)
def test_tex_accents_decoded(source, expected):
    assert m.decode_tex_accents(source) == expected


def test_tex_to_text_and_source_decode_accents(tmp_path):
    assert "M\u00fcller" in m._tex_to_text('Hans M\\"uller')
    path = tmp_path / "m.tex"
    path.write_text('\\author{Hans M\\"uller}\n', encoding="utf-8")
    assert "M\u00fcller" in m._raw_tex_source(path)


# --- references -----------------------------------------------------------------


def test_reference_count_ignores_unused_bib_entries(tmp_path):
    (tmp_path / "refs.bib").write_text("\n".join(f"@article{{k{i},title={{T}},year=2020}}" for i in range(200)), encoding="utf-8")
    path = tmp_path / "m.tex"
    path.write_text("\\documentclass{article}\n\\begin{document}\nA\\cite{k1,k2}.\n\\bibliography{refs}\n\\end{document}\n", encoding="utf-8")
    assert m.reference_count(path) == 2
    assert len(m.reference_entries(path)) == 2
    path.write_text("\\documentclass{article}\n\\begin{document}\n\\nocite{*}\n\\bibliography{refs}\n\\end{document}\n", encoding="utf-8")
    assert m.reference_count(path) == 200


def test_reference_count_is_not_fooled_by_a_wrapped_year(tmp_path):
    entries = [f"Smith, A., Jones, B. (2021). A title of the paper number {i}. Nature 12, 1-{i}." for i in range(100)]
    entries[7] = "Smith, A., Jones, B.\n2021. A wrapped entry title. Nature 12, 1-9."
    path = tmp_path / "ms.txt"
    path.write_text("Intro\n\nReferences\n\n" + "\n".join(entries) + "\n", encoding="utf-8")
    assert m.reference_count(path) >= 99


def test_reference_count_stops_at_what_follows_the_list(tmp_path):
    refs = "\n".join(f"{i}. Ref {i} Smith and colleagues, Nature 2020." for i in range(1, 41))
    steps = "\n".join(f"{i}. Step {i} of the protocol." for i in range(1, 16))
    path = tmp_path / "ms.txt"
    path.write_text(f"Intro\n\nReferences\n\n{refs}\n\nMethods\n\n{steps}\n", encoding="utf-8")
    assert m.reference_count(path) == 40


@pytest.mark.parametrize("entry", ["Smith J, Lee K (2021) A title of a paper. Nature 12:1-9", "Smith, J. (2021). A title of a paper. Nature 12, 1-9.", "Smith, J., 2021. A title of a paper. Nature 12, 1-9."])
def test_author_year_entry_formats(tmp_path, entry):
    path = tmp_path / "ms.txt"
    path.write_text("Intro\n\nReferences\n\n" + "\n".join([entry] * 5) + "\n", encoding="utf-8")
    assert m.reference_count(path) == 5
    assert len(m.reference_entries(path)) == 5


def test_author_year_continuation_line_is_not_an_entry():
    assert not m._AUTHOR_YEAR_REF.match("Nature 12, 1-9 (2021).")


def test_interval_is_not_a_citation(tmp_path):
    path = tmp_path / "ms.txt"
    path.write_text("Intro [1]. Values were clipped to [5, 10] and [0, 1]. Next [2]. Then [3].\n\nReferences\n" + "\n".join(f"{i}. Smith A. Title text of some length here, Nature 2020." for i in range(1, 21)) + "\n", encoding="utf-8")
    assert run("nature_communications", path, "guidelines.reference_style").status == "passed"


def test_two_citations_after_according_to_still_count():
    assert check._citation_groups("according to [5, 10]", 20) == ["5, 10"]


# --- silent passes --------------------------------------------------------------


def test_counts_read_captions_when_nothing_is_attached(tmp_path):
    path = tmp_path / "ms.txt"
    path.write_text("Intro\n\n" + "\n".join(f"Figure {i}. A caption." for i in range(1, 6)) + "\nTable 1. A caption.\nTable 2: Another.\n\nReferences\n1. A\n", encoding="utf-8")
    result = run("science", path, "guidelines.counts")
    assert result.status == "warning"
    assert "display items" in result.issues[0].message


def test_counts_unchecked_for_an_unreadable_manuscript(tmp_path):
    assert run("nature", scanned_pdf(tmp_path / "s.pdf"), "guidelines.counts").status == "unchecked"


def test_template_unchecked_when_the_pdf_cannot_be_measured(tmp_path):
    assert run("iclr_2027", scanned_pdf(tmp_path / "s.pdf"), "guidelines.template").status == "unchecked"


@pytest.mark.parametrize("name", ["lt.tex", "lt.docx"])
def test_layout_template_is_unchecked_and_queues_ai_without_a_class(tmp_path, name):
    path = tmp_path / name
    if name.endswith(".tex"):
        path.write_text("\\documentclass{article}\\begin{document}\\section{I}\\end{document}", encoding="utf-8")
    else:
        make_docx(path, para("Hello"))
    venue, values = venue_values("cvpr_2027", path)
    checks = Checks()
    layout_check.run_checks(checks, venue, values)
    assert next(r for r in checks.results if r.id == "layout.template").status == "unchecked"
    assert any(t.id == "ai.template" for t in checks.ai_tasks)


# --- headings -------------------------------------------------------------------


def test_run_in_statements_are_found(tmp_path):
    body = para("Data availability. The data that support the findings of this study are openly available from Zenodo.") + para("Competing interests: None declared.") + para("Author contributions: AB designed the study and CD wrote the paper.")
    found = m.headings(make_docx(tmp_path / "ms.docx", body))
    assert {"data availability", "competing interests", "author contributions"} <= set(found)


def test_tex_implicit_and_escaped_headings(tmp_path):
    path = tmp_path / "m.tex"
    path.write_text("\\documentclass{article}\n\\begin{document}\n\\begin{abstract}x\\end{abstract}\n\\section{Materials \\& Methods}\nx\n\\section{Results \\& Discussion}\ny\n\\section{Limits\\label{s:l}}\nz\n\\bmhead{Data availability}\nd\n\\begin{acknowledgments}t\\end{acknowledgments}\n\\bibliography{r}\n\\end{document}\n", encoding="utf-8")
    assert m.headings(path) == ["abstract", "materials and methods", "results and discussion", "limits", "data availability", "acknowledgements", "references"]


def test_abstract_labels_and_proper_nouns_are_not_abbreviations():
    text = "BACKGROUND: We searched PubMed and McDonald data. MAIN OUTCOMES AND MEASURES: A survey. RESULTS: MRI improved."
    assert check._undefined_abbreviations(text) == ["MRI"]


def test_hyphenated_initialism_still_flagged():
    assert check._undefined_abbreviations("We used CRISPR-Cas9 here.") == ["CRISPR-Cas9"]


def test_structured_abstract_does_not_set_section_order(tmp_path):
    body = "\n".join(f"{name}\n{words(50)}" for name in ("Background", "Methods", "Results", "Discussion", "Conclusions"))
    path = tmp_path / "ms.txt"
    path.write_text(f"A title of the paper here for testing\n\nAbstract\nBackground\nWe study.\nResults\nIt worked.\nConclusions\nGood.\n\n{body}\nReferences\n1. A ref\n", encoding="utf-8")
    assert run("bmc_bioinformatics", path, "guidelines.section_order").status == "passed"


def test_real_section_order_problem_is_still_reported(tmp_path):
    path = tmp_path / "ms.txt"
    path.write_text("Abstract\n" + words(30) + "\nBackground\nx\nResults\ny\nMethods\nz\nReferences\n1. A\n", encoding="utf-8")
    assert run("bmc_bioinformatics", path, "guidelines.section_order").status == "warning"


# --- title page and supplementary -----------------------------------------------


def title_page_issues(path: Path, items: list[str], values: dict | None = None) -> list:
    venue = database.get_venue("nature")
    reqs = requirements.get_requirements("nature")
    reqs = replace(reqs, title_page=replace(reqs.title_page, required_items=items))
    return check._title_page_issues(check._fields_for(venue, "manuscript")[0], path, reqs, values or {}, venue)


def test_title_page_reads_through_latex_markup(tmp_path):
    path = tmp_path / "t.tex"
    path.write_text('\\documentclass{article}\n\\title{Growth of \\emph{E. coli} in \\textbf{low} oxygen}\n\\author{Hans M\\"uller}\n\\begin{document}\\maketitle\n\\section{Introduction}x\n\\end{document}\n', encoding="utf-8")
    values = {"title": "Growth of E. coli in low oxygen", "authors": "Dr|Hans|M\u00fcller|c@x.edu|MIT|USA|Paris|yes"}
    assert title_page_issues(path, ["title", "authors"], values) == []


def test_running_head_counts_as_running_title(tmp_path):
    path = tmp_path / "t.txt"
    path.write_text("My title\nRunning head: A short one\n\nIntro\n", encoding="utf-8")
    assert title_page_issues(path, ["running_title"]) == []


def test_elsarticle_keyword_environment_counts(tmp_path):
    path = tmp_path / "t.tex"
    path.write_text("\\documentclass{elsarticle}\\begin{document}\\begin{frontmatter}\\begin{keyword}a \\sep b\\end{keyword}\\end{frontmatter}\\section{I}\\end{document}", encoding="utf-8")
    assert title_page_issues(path, ["keywords"]) == []


def test_supplementary_pdf_plus_document_warns(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "b.docx").write_text("x", encoding="utf-8")
    venue = database.get_venue("ajhg")
    checks = Checks()
    checks.run("g", "t", "c", check._supplementary_check, venue, requirements.get_requirements("ajhg"), {"supplementary_files": f"{tmp_path / 'a.pdf'}\n{tmp_path / 'b.docx'}"})
    assert checks.results[-1].status == "warning"
    assert "b.docx" in checks.results[-1].issues[0].message


def test_supplementary_data_files_beside_one_pdf_pass(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "b.xlsx").write_text("x", encoding="utf-8")
    venue = database.get_venue("ajhg")
    checks = Checks()
    checks.run("g", "t", "c", check._supplementary_check, venue, requirements.get_requirements("ajhg"), {"supplementary_files": f"{tmp_path / 'a.pdf'}\n{tmp_path / 'b.xlsx'}"})
    assert checks.results[-1].status == "passed"


def test_template_module_still_reads_sources(tmp_path):
    path = tmp_path / "m.tex"
    path.write_text("\\usepackage[margin=1in]{geometry}\n", encoding="utf-8")
    assert list(template_check.source_problems(path.read_text()))
