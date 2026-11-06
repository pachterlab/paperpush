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
