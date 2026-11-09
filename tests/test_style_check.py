"""paperpush.style_check: numbers, abbreviation conventions, acronyms, and the AI prose task."""

from __future__ import annotations

from pathlib import Path

from paperpush import report
from paperpush import style_check as sc
from paperpush.database import Field, Venue
from paperpush.prose_check import run_checks
from paperpush.validate import Checks
from tests.conftest import _build_docx

MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
VENUE = Venue(slug="testj", name="Test Venue", fields=[MANUSCRIPT])


def _run(path: Path) -> tuple[dict[str, tuple[str, str, str]], Checks]:
    checks = Checks()
    run_checks(checks, VENUE, {"manuscript_file": str(path)})
    return {r.id: (r.status, " | ".join(i.message for i in r.issues), r.detail) for r in checks.results}, checks


# --- numbers -------------------------------------------------------------------------------


def test_words_to_number():
    assert sc.words_to_number("twenty-five") == 25
    assert sc.words_to_number("one hundred and twelve") == 112
    assert sc.words_to_number("two thousand") == 2000
    assert sc.words_to_number("Ten") == 10


def test_spelled_numbers_of_ten_or_more_are_flagged():
    found = sc.spelled_numbers("We enrolled twenty-five patients and one hundred and twelve controls over fifteen days.")
    assert [f.split("(write ")[1] for f in found] == ["25)", "112)", "15)"]
    assert "\u00bbtwenty-five\u00ab" in found[0]


def test_spelled_numbers_spares_small_numbers_sentence_starts_and_ordinals():
    assert sc.spelled_numbers("We used three mice and nine rats.") == []
    assert sc.spelled_numbers("Twelve mice died. Fifteen survived.") == []
    assert sc.spelled_numbers("In the twenty-first century, a million cells and hundreds of genes.") == []
    # "and" joins only after a scale word: "two and ten" is two numbers.
    assert [f.split("(write ")[1] for f in sc.spelled_numbers("We kept two and ten were lost.")] == ["10)"]
    # After an abbreviation's full stop, the sentence has not ended.
    assert len(sc.spelled_numbers("As in Fig. twelve and Smith et al. twenty")) == 2


def test_digit_sentence_starts():
    found = sc.digit_sentence_starts("We stopped (Fig. 2). 15 patients died. See Fig. 3 for more. It ended. 7 mice ran.")
    assert len(found) == 2 and "\u00bb15\u00ab" in found[0] and "\u00bb7\u00ab" in found[1]
    assert sc.digit_sentence_starts("Shown in Fig. 3 here, and in Section 2. 3 cases follow.") == []
    assert sc.digit_sentence_starts("15 patients opened the paragraph.") == []  # a heading or list item may open with digits



def test_numbers_in_skips_years_labels_and_ids():
    text = "In 2019 (Fig. 1000, accession 123456, p. 1200) GSE12345, chr1:123456, 10.1000/abc, v2.10000, 0012345 and 1000\u20132000 [12,345]."
    assert [(n.digits, n.style) for n in sc.numbers_in(text)] == [(4, "none")]  # only the range's 1000
    assert sc.numbers_in("Table 2 100 mice") == []  # a plain space between a label and a count is not grouping
    assert [(n.digits, n.style) for n in sc.numbers_in("12\u2009000 and 1\u202f500 and 12 000 and 12,000.5")] == [(5, "space"), (4, "space"), (5, "space"), (5, "comma")]


def test_thousands_grouping_flags_long_ungrouped_numbers():
    [(desc, hits)] = sc.thousands_grouping(["We sequenced 12000 cells and 1,500 genes."])
    assert "10,000 or more" in desc and "commas" in desc and hits == ["We sequenced \u00bb12000\u00ab cells and 1,500 genes."]
    [(desc, _)] = sc.thousands_grouping(["We had 12345 cells and 15 000 genes."])
    assert "spaces (1 000)" in desc  # the manuscript's own separator is suggested


def test_thousands_grouping_four_digits_written_one_way():
    [(desc, hits)] = sc.thousands_grouping(["From 1000 reads to 1,500 and 2,500 and 3,500 samples."])
    assert "no separator" in desc and len(hits) == 1 and "\u00bb1000\u00ab" in hits[0]
    [(desc, hits)] = sc.thousands_grouping(["We kept 1500, 2500, and 1,200 cells."])
    assert "grouped" in desc.split(",")[0] and len(hits) == 1 and "\u00bb1,200\u00ab" in hits[0]
    # One style throughout is fine, either way.
    assert sc.thousands_grouping(["We kept 1500 and 2500 cells in 2020."]) == []
    assert sc.thousands_grouping(["We kept 1,500 and 12,500 cells."]) == []


def test_thousands_grouping_mixed_separators():
    [(desc, hits)] = sc.thousands_grouping(["We had 12 000 and 13,000 and 14,000 cells."])
    assert "spaces" in desc and "2 other(s) use commas" in desc and len(hits) == 1



def test_thousands_grouping_enforces_the_venues_separator():
    # Consistent spaces pass on their own, but not when the venue writes commas.
    assert sc.thousands_grouping(["We had 12 000 and 15 000 cells."]) == []
    [(desc, hits)] = sc.thousands_grouping(["We had 12 000 and 15 000 cells."], separator="comma")
    assert "venue's guidelines group thousands with commas" in desc and len(hits) == 2
    [(desc, _)] = sc.thousands_grouping(["We had 12000 cells."], separator="space")
    assert "spaces (1 000) (the venue's guidelines)" in desc


def test_thousands_grouping_enforces_four_digit_rule():
    # One way throughout passes the consistency check, but not the venue's rule.
    [(desc, hits)] = sc.thousands_grouping(["We kept 1500 and 2500 cells."], group_four=True)
    assert "no separator" in desc and len(hits) == 2
    [(desc, hits)] = sc.thousands_grouping(["We kept 1,500 and 12,500 cells."], group_four=False)
    assert "write 1000" in desc and len(hits) == 1 and "\u00bb1,500\u00ab" in hits[0]
    assert sc.thousands_grouping(["We kept 1500 and 12,500 cells."], separator="comma", group_four=False) == []


def test_numbers_check_reads_the_venues_rules(monkeypatch, tmp_path):
    from paperpush import requirements as r

    doc = tmp_path / "ms.md"
    doc.write_text("# Title\n\n## Introduction\n\nWe sequenced 1,500 cells and 12,000 reads.\n")
    assert _run(doc)[0]["prose.numbers"][0] == report.PASSED
    base = r.ManuscriptRequirements(slug="testj", manuscript=r.ManuscriptRules(thousands_separator="space", group_four_digit_numbers=False))
    monkeypatch.setattr(r, "get_requirements", lambda slug: base)
    status, message, _ = _run(doc)[0]["prose.numbers"]
    assert status == report.WARNING and "write 1000" in message and "group thousands with spaces" in message


# --- conventions ---------------------------------------------------------------------------


def test_malformed_abbreviations():
    found = sc.conventions(["We used a net (ie a model), Smith et. al and Lee et al, showed it."]).problems
    assert any("malformed 'i.e.'/'e.g.'" in p and "\u00bbie\u00ab" in p for p in found)
    assert any(p.startswith("2 malformed 'et al.'") for p in found)


def test_mixed_forms_are_reported_once_each():
    found = sc.conventions(["We used e.g., a model, i.e. a net, and e.g., a tree.", "A vs. B and C vs D, see Fig. 2 and Figure 3."]).problems
    assert any("'i.e.'/'e.g.' written two ways: 'with a comma' 2x" in p for p in found)
    assert any("'vs.' written two ways" in p for p in found)
    assert any("Figure citations mid-sentence written two ways" in p for p in found)


def test_consistent_forms_pass():
    paragraphs = ["We used e.g., a model, i.e., a net, as Smith et al. did (Fig. 1).", "Figure 2 shows A vs. B; see Fig. 3."]
    assert sc.conventions(paragraphs).problems == []
    # Supplementary figures are their own series.
    assert sc.conventions(["See Fig. 1 and Supplementary Figure 2."]).problems == []


def test_hyphenated_and_closed_compounds():
    found = sc.conventions(["We did pre-processing first.", "Then preprocessing again, and well-known steps."]).problems
    assert len(found) == 1 and "'pre-processing'" in found[0] and "'preprocessing' 1x" in found[0]
    assert sc.conventions(["We did pre-processing first.", "Then preprocessing."], hyphens=False).problems == []


# --- acronyms ------------------------------------------------------------------------------


BODY = [
    "Introduction",
    "We trained a convolutional neural network (CNN). The CNN was good, and the SVM was tried before the support vector machine (SVM) won.",
    "The RF did well. The RF did badly. The RF stopped. A graph neural network (GNN) helped once.",
    "We spelled out the convolutional neural network (CNN) again. DNA and USA are known.",
    "Figure 1. The XYZ and XYZ and XYZ are in a legend, not the text.",
]


def test_acronym_problems():
    found = sc.acronyms(BODY)
    assert [s.split("'")[1] for s in found.before_definition] == ["SVM"]
    assert found.redefined == ["'CNN' (2 times)"]
    assert [s.split("'")[1] for s in found.undefined] == ["RF"]
    assert found.rarely_used == ["'GNN'"]


def test_acronyms_in_abstract_and_main_text_are_defined_separately():
    abstract = ["We used a long short-term memory (LSTM) and a GAN on the data."]
    body = ["The LSTM worked. A generative adversarial network (GAN) helped. The GAN and LSTM ran."]
    found = sc.acronyms(body, abstract)
    assert [s.split("'")[1] for s in found.abstract_only] == ["LSTM"]
    assert found.abstract_undefined == ["'GAN'"]


def test_headings_and_plurals():
    found = sc.acronyms(["Results with CNNs", "We trained convolutional neural networks (CNNs). The CNNs and the CNN ran."])
    assert found.before_definition == [] and found.redefined == [] and found.rarely_used == []


def test_split_abstract_drops_front_matter():
    paragraphs = ["A Title", "Jane Doe, UCLA, CA, USA", "Abstract", "We study the CNN on twenty data sets with care.", "Introduction", "Body text here."]
    abstract, body = sc.split_abstract(paragraphs, "We study the CNN on twenty data sets with care.")
    assert abstract == ["We study the CNN on twenty data sets with care."]
    assert body == ["Introduction", "Body text here."]
    assert sc.split_abstract(paragraphs, None) == ([], paragraphs)


# --- on a manuscript -------------------------------------------------------------------------


def test_checks_run_on_a_docx(tmp_path):
    doc = tmp_path / "ms.docx"
    _build_docx(
        doc,
        [
            "A Title",
            "Abstract",
            "We trained a convolutional neural network (CNN) on twenty mice.",
            "Introduction",
            "The CNN ran, i.e. it worked, e.g., on data. See Fig. 1 and Figure 2. The CNN stopped.",
            "Figure 1. A legend with Figure 3 and e.g. a note.",
            "References",
            "Smith J, twenty-five things, et. al.",
        ],
    )
    results, checks = _run(doc)
    status, message, _ = results["prose.numbers"]
    assert status == report.WARNING and "twenty" in message and "twenty-five" not in message  # the reference list is skipped
    assert "separator" not in message
    status, message, _ = results["prose.conventions"]
    assert status == report.WARNING and "with a comma" in message and "Fig." in message and "et al." not in message
    status, message, _ = results["prose.acronyms"]
    assert status == report.WARNING and "'CNN'" in message and "spelled out only in the abstract" in message
    task = next(t for t in checks.ai_tasks if t.id == sc.AI_PROSE_ID)
    assert task.files == [str(doc)] and "Tense" in task.instructions and "American and British" in task.instructions


def test_clean_markdown_passes(tmp_path):
    doc = tmp_path / "ms.md"
    doc.write_text(
        "# Title\n\n## Abstract\n\nWe trained a convolutional neural network (CNN) on 20 mice.\n\n"
        "## Introduction\n\nWe trained a convolutional neural network (CNN), i.e., a model, on 20 mice (Fig. 1). The CNN worked well, e.g., on noisy data, and the CNN ran fast.\n"
    )
    results, _ = _run(doc)
    for cid in ("prose.numbers", "prose.conventions", "prose.acronyms"):
        assert results[cid][0] == report.PASSED, (cid, results[cid])
    assert "1 acronym(s) defined at first use" in results["prose.acronyms"][2]


def test_tex_floats_are_not_first_use(tmp_path):
    tex = tmp_path / "ms.tex"
    tex.write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "\\begin{figure}\\caption{The CNN, the CNN, and the CNN.}\\label{fig:a}\\end{figure}\n"
        "We trained a convolutional neural network (CNN) here. The CNN ran (Fig.~\\ref{fig:a}). The CNN stopped.\n"
        "\\end{document}\n"
    )
    results, _ = _run(tex)
    assert results["prose.acronyms"][0] == report.PASSED, results["prose.acronyms"]


def test_ai_task_names_the_guidelines_spelling_variant(monkeypatch, tmp_path):
    from paperpush import requirements as r

    doc = tmp_path / "ms.txt"
    doc.write_text("Some text.\n")
    base = r.ManuscriptRequirements(slug="testj", manuscript=r.ManuscriptRules(language="British English"))
    monkeypatch.setattr(r, "get_requirements", lambda slug: base)
    task = sc.ai_prose_task(VENUE, {"manuscript_file": str(doc)})
    assert "'British English' variant" in task.instructions


def test_no_manuscript_queues_no_ai_task():
    checks = Checks()
    run_checks(checks, VENUE, {})
    assert not checks.ai_tasks
    assert all(r.status == report.NOT_APPLICABLE for r in checks.results)


def test_et_al_without_full_stop_as_house_style():
    paragraphs = ["Kamran et al [5] identified 789 articles, as Smith et. al did."]
    default = sc.conventions(paragraphs).problems
    assert any(p.startswith("2 malformed 'et al.'") for p in default)
    house = sc.conventions(paragraphs, et_al_period=False).problems
    assert len(house) == 1 and house[0].startswith("1 malformed 'et al'") and "et. al" in house[0]


# --- acronyms defined twice, with two meanings, or never used ---------------------------------


def test_acronym_never_used_and_defined_twice_are_flagged():
    found = sc.acronyms(["We used a hidden Markov model (HMM) here.", "We fit a random forest (RF) once. The RF was fast.", "A random forest (RF) again."])
    assert found.rarely_used == ["'HMM'"]
    assert found.redefined == ["'RF' (2 times)"]
    assert found.conflicting == []


def test_acronym_with_two_meanings_is_conflicting_not_just_redefined():
    found = sc.acronyms(["We fit a random forest (RF) once. The RF was fast.", "We measured relative fluorescence (RF) too."])
    assert found.conflicting == ["'RF' ('random forest', 'relative fluorescence')"]
    assert found.redefined == []


def test_acronym_meanings_ignore_plural_and_case_and_unmatched_initials():
    found = sc.acronyms(["Convolutional Neural Networks (CNNs) ran. The CNNs ran.", "A convolutional neural network (CNN) ran."])
    assert found.conflicting == [] and found.redefined == ["'CNN' (2 times)"]
    assert sc._expansion("an electroencephalogram ", "EEG") is None
    assert sc._expansion("the Department of Energy ", "DOE") == "department of energy"
    assert sc._expansion("the Department of Energy ", "DE") == "department energy"
    assert sc._expansion("a genome-wide association study ", "GWAS") == "genome wide association study"


def test_acronym_meaning_differs_between_abstract_and_main_text():
    found = sc.acronyms(["We fit a random forest (RF) and the RF ran."], ["We measured relative fluorescence (RF) and the RF rose."])
    assert found.conflicting == ["'RF' ('random forest', 'relative fluorescence')"]


# --- heading capitalization -------------------------------------------------------------------


def test_heading_case_styles():
    assert sc.heading_case("Materials and methods") == "sentence"
    assert sc.heading_case("Materials and Methods") == "title"
    assert sc.heading_case("MATERIALS AND METHODS") == "upper"
    assert sc.heading_case("materials and methods") == "lower"
    # One word, an acronym, or a mix shows no style.
    assert sc.heading_case("Introduction") is None
    assert sc.heading_case("RNA") is None
    assert sc.heading_case("Cell Culture and media") is None
    assert sc.heading_case("Comparison with GPT") is None
    # Lower-case starts that are names, not a style.
    assert sc.heading_case("scRNA sequencing") == "sentence"
    assert sc.heading_case("k-means clustering") == "sentence"
    assert sc.heading_case("Single-Cell Atlas") == "title"
    # A name capitalized in the text is not Title Case.
    assert sc.heading_case("Mapping in Drosophila") == "title"
    assert sc.heading_case("Mapping in Drosophila", {"drosophila"}) is None


def test_inconsistent_heading_case_is_per_level():
    headings = [("section", "Materials and methods"), ("section", "Author contributions"), ("section", "Data Availability"), ("subsection", "Cell Culture"), ("subsection", "Mouse Models")]
    hits, styled = sc.inconsistent_heading_case(headings)
    assert styled == 5
    assert hits == ["'Data Availability' is Title Case, others are Sentence case"]


def _heading_result(path: Path, **values: str):
    checks = Checks()
    run_checks(checks, VENUE, {"manuscript_file": str(path), **values})
    result = next(r for r in checks.results if r.id == "prose.heading_case")
    return result.status, " | ".join(i.message for i in result.issues), result.detail


def test_heading_case_in_latex(tmp_path):
    tex = tmp_path / "ms.tex"
    tex.write_text(
        "\\documentclass{article}\n\\begin{document}\n\\section{Materials and methods}\nText here.\n\\section*{Author contributions}\nText here.\n"
        "\\section{Data \\emph{Availability}}\nText here.\n\\subsection{Cell Culture}\nText here.\n\\end{document}\n"
    )
    status, message, _ = _heading_result(tex)
    assert status == report.WARNING and "'Data Availability' is Title Case" in message and "Cell Culture" not in message


def test_heading_case_in_markdown_skips_the_title_and_names(tmp_path):
    doc = tmp_path / "ms.md"
    doc.write_text("# A Study of Mice\n\n## Materials and methods\n\nWe bred flies from Boston here.\n\n## Author contributions\n\nAll did it.\n\n## Flies from Boston\n\nDone.\n")
    status, _, detail = _heading_result(doc, title="A Study of Mice")
    assert status == report.PASSED and "capitalized consistently" in detail


def test_heading_case_in_word_uses_heading_styles(tmp_path):
    from zipfile import ZipFile

    def para(style: str, text: str) -> str:
        props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        return f"<w:p>{props}<w:r><w:t>{text}</w:t></w:r></w:p>"

    path = tmp_path / "ms.docx"
    body = para("Heading1", "Materials and methods") + para("Heading1", "Data Availability") + para("Heading1", "Author contributions") + para("", "Some Short Line Here") + para("", "We did the work on mice in the lab today.")
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", f"<w:document><w:body>{body}</w:body></w:document>")
    status, message, _ = _heading_result(path)
    assert status == report.WARNING and "'Data Availability'" in message and "Some Short" not in message


def test_heading_case_unmarked_file_reads_only_standard_names(tmp_path):
    doc = tmp_path / "ms.txt"
    doc.write_text("Some Odd Short Line\n\nMaterials and methods\n\nWe did the work.\n\nData Availability\n\nOn request.\n\nAuthor contributions\n\nAll.\n")
    status, message, _ = _heading_result(doc)
    assert status == report.WARNING and "'Data Availability'" in message and "Odd" not in message


def test_heading_case_not_applicable_without_styled_headings(tmp_path):
    doc = tmp_path / "ms.md"
    doc.write_text("## Introduction\n\nText.\n\n## Results\n\nText.\n")
    assert _heading_result(doc)[0] == report.NOT_APPLICABLE


def test_acronyms_read_as_main_text_when_there_is_no_introduction(tmp_path):
    doc = tmp_path / "ms.md"
    doc.write_text("# A title\n\n## Abstract\n\nWe trained a model on mice.\n\n## Materials and methods\n\nWe used a hidden Markov model (HMM) on the mice here.\n")
    status, message, _ = _run(doc)[0]["prose.acronyms"]
    assert status == report.WARNING and "'HMM'" in message
