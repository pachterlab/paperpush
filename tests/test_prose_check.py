"""paperpush.prose_check: double spaces, repeated words, punctuation spacing, brackets, spelling."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperpush import prose_check as pc
from paperpush import report
from paperpush.database import Field, Venue
from paperpush.validate import Checks
from tests.conftest import _build_docx, _build_pdf

MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
VENUE = Venue(slug="testj", name="Test Venue", fields=[MANUSCRIPT])
IDS = [f"prose.{name}" for name in pc._CHECKS]
ALL_IDS = set(IDS) | {f"prose.{pc.REPETITION}"}

needs_codespell = pytest.mark.skipif(
    pc.misspellings() is None, reason="codespell is not installed"
)


def _run(path: Path) -> dict[str, tuple[str, str, str]]:
    checks = Checks()
    pc.run_checks(checks, VENUE, {"manuscript_file": str(path)})
    return {
        r.id: (r.status, " ".join(i.message for i in r.issues), r.detail)
        for r in checks.results
    }


# --- the patterns ------------------------------------------------------------------------


def test_double_spaces_mid_sentence_but_not_after_one():
    assert pc.double_spaces("The results  were good.") == [
        "The results\u2423\u2423were good."
    ]
    found = pc.double_spaces("It ended.  Then it began.  Why?  Because: two  spaces")
    assert len(found) == 1 and found[0].endswith("two\u2423\u2423spaces")
    assert pc.double_spaces('He said "stop."  Then left (as noted.)  Fine.') == []
    assert pc.double_spaces("Note:  this is allowed after a colon") == []


def test_repeated_words():
    assert len(pc.repeated_words("We saw the the cells and The the others.")) == 2
    assert pc.repeated_words("He said that that was fine; we had had enough.") == []
    assert pc.repeated_words("Variables x x and well-known known-unknowns.") == []
    assert len(pc.repeated_words("This is a a typo.")) == 1
    # A heading followed by a sentence starting with the same word, across a line break.
    assert pc.repeated_words("Results\nResults show that it works.") == []
    assert len(pc.repeated_words("we ran the\nthe model")) == 1


def test_punctuation_spacing():
    found = pc.punctuation_spacing(
        "The cells grew , then died ; next.Then it ran fast,then slow ."
    )
    assert len(found) == 5
    assert "\u00bb ,\u00ab" in found[0]
    clean = "Values of 1,000 and 0.5 (n = 3), see https://a.org/x,y and doi:10.1/a.b; e.g. this... and that. Fig. 2a, U.S. data, mail me@x.edu."
    assert pc.punctuation_spacing(clean) == []


def test_unbalanced_brackets_and_quotes():
    assert (
        pc.unbalanced('All (n = 3) [and 4] matched \u201cwell\u201d and "fine".') == []
    )
    assert "unclosed '('" in pc.unbalanced("We used (a kit and stopped.")[0]
    assert "unmatched ')'" in pc.unbalanced("We used a kit) and stopped.")[0]
    assert "unmatched ']'" in pc.unbalanced("We used (a kit] and stopped.")[0]
    assert pc.unbalanced("Choose a) one, b) two, or (iii) three.") == []
    assert "curly" in pc.unbalanced("He said \u201chi and left.")[0]
    assert "straight" in pc.unbalanced('He said "hi and left.')[0]
    assert pc.unbalanced("See (https://example.org/a_(b)) now.") == []


@needs_codespell
def test_spelling_flags_known_typos_but_not_jargon():
    table = pc.misspellings()
    found = pc.spelling(
        "We recieve teh data, seperate the CD4 and Ser cells; Proc Lond Soc; ser aas ehr.",
        table,
    )
    assert found == [
        "'recieve' (did you mean 'receive'?)",
        "'teh' (did you mean 'the'?)",
        "'seperate' (did you mean 'separate'?)",
    ]


def test_tex_masks_math_commands_and_citations():
    source = r"""\documentclass{article}
\begin{document}
\section{Results}
Results show $x = 1$ , as in \cite{a,b}. We used \textbf{bold}  words, \nCells{} cells,
and \ref{fig:a}. See \url{http://a.b/c,d}.
\begin{equation} a ,b \end{equation}
\item {[}a bracketed item{]}
\bibliography{refs}
teh the the
"""
    paragraphs = pc._tex_paragraphs(source)
    assert paragraphs[0] == "Results"
    body = " ".join(paragraphs)
    assert "\\" not in body and "$" not in body and "teh" not in body
    assert len(pc.punctuation_spacing(body)) == 1  # "$x = 1$ ," only
    assert pc.unbalanced(body) == []


# --- the checks --------------------------------------------------------------------------


@needs_codespell
def test_docx_runs_every_check(tmp_path):
    doc = tmp_path / "ms.docx"
    _build_docx(
        doc,
        [
            "A Clean Title",
            "We measured the the cells  carefully , and recieved (data.",
            "Plain text here.",
            "References",
            "Smith J. teh the the  ,",
        ],
    )
    results = _run(doc)
    assert set(results) == ALL_IDS
    for cid in IDS:
        status, message, _ = results[cid]
        assert status == report.WARNING, (cid, results[cid])
        assert "teh" not in message  # the reference list is skipped
    assert "recieved" in results["prose.spelling"][1]


def test_clean_docx_passes(tmp_path):
    doc = tmp_path / "ms.docx"
    _build_docx(
        doc,
        [
            "A Clean Title",
            "We measured the cells carefully (twice), and received data.  Then we stopped.",
        ],
    )
    results = _run(doc)
    for cid in IDS:
        if cid == "prose.spelling" and pc.misspellings() is None:
            assert results[cid][0] == report.UNCHECKED
            continue
        assert results[cid][0] == report.PASSED, (cid, results[cid])


def test_findings_are_capped(tmp_path):
    doc = tmp_path / "ms.md"
    doc.write_text(
        "\n\n".join(f"Paragraph {i} has the the typo." for i in range(8)) + "\n"
    )
    status, message, _ = _run(doc)["prose.repeated_words"]
    assert status == report.WARNING
    assert "8 repeated word(s)" in message and "(and 3 more)" in message


def test_markdown_skips_code_and_tables(tmp_path):
    doc = tmp_path / "ms.md"
    doc.write_text(
        "# Title\n\nSome prose here.\n\n```\nx  =  f(a ,b\n```\n\n| a  | b |\n|---|---|\n\nInline `a  ,b` code and $x ,y$ math.\n"
    )
    results = _run(doc)
    for cid in (
        "prose.double_spaces",
        "prose.punctuation_spacing",
        "prose.brackets",
        "prose.repeated_words",
    ):
        assert results[cid][0] == report.PASSED, (cid, results[cid])


def test_tex_skips_double_spaces(tmp_path):
    tex = tmp_path / "ms.tex"
    tex.write_text(
        "\\documentclass{article}\n\\begin{document}\nSome  prose with the the typo.\n\\end{document}\n"
    )
    results = _run(tex)
    assert results["prose.double_spaces"][0] == report.UNCHECKED
    assert "LaTeX does not print" in results["prose.double_spaces"][2]
    assert results["prose.repeated_words"][0] == report.WARNING


def test_pdf_gets_only_repeated_words_and_spelling(tmp_path):
    pdf = tmp_path / "ms.pdf"
    pdf.write_bytes(_build_pdf(body_lines=["We saw the the cells grow."]))
    results = _run(pdf)
    for cid in ("prose.double_spaces", "prose.punctuation_spacing", "prose.brackets"):
        assert results[cid][0] == report.UNCHECKED, (cid, results[cid])
    assert results["prose.repeated_words"][0] == report.WARNING


def test_spelling_unchecked_without_codespell(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "misspellings", lambda: None)
    doc = tmp_path / "ms.txt"
    doc.write_text("Some text.\n")
    status, _, detail = _run(doc)["prose.spelling"]
    assert status == report.UNCHECKED and "codespell" in detail


def test_no_manuscript_is_not_applicable():
    checks = Checks()
    pc.run_checks(checks, VENUE, {})
    assert all(r.status == report.NOT_APPLICABLE for r in checks.results)


# --- repetitive word choice ----------------------------------------------------------------


def test_stem_groups_inflections():
    assert {pc.stem(w) for w in ("result", "results", "resulted", "resulting")} == {"result"}
    assert pc.stem("studies") == pc.stem("study")
    assert pc.stem("mapped") == pc.stem("mapping") == "map"
    assert pc.stem("analysis") == "analysis" and pc.stem("genes") == "gene"


def test_repetition_in_sentence_and_openings():
    found = pc.repetition(["The assay used an assay buffer before each assay ran. We ran it. We saw it. We liked it. We stopped."])
    assert any("'assay' 3 times" in s for s in found.in_sentence)
    assert found.openings and "4 sentences in a row open with 'We'" in found.openings[0]
    assert pc.repetition(["We ran it. We saw it. We liked it. Then we stopped."]).openings == []


def test_repetition_spares_topic_words():
    text = ["Neurons fire when neurons near other neurons fire. " * 3]
    assert pc.repetition(text).in_sentence
    assert not pc.repetition(text, topic="How neurons fire").in_sentence


def test_repetition_stock_words_and_overused():
    filler = "However, the cells grew in the dish today. " * 6
    found = pc.repetition([filler + "Plain words follow here without anything special. " * 4])
    assert any(s.startswith("'however' 6 times") for s in found.style)
    noisy = ["The widget moved. " * 30 + "Other text appears here with several different words to read. " * 10]
    assert any(s.startswith("'widget'") for s in pc.repetition(noisy, topic="A paper").overused)
    assert not any("widget" in s for s in pc.repetition(noisy, topic="A widget paper").overused)
    assert pc.repetition(noisy).overused == []  # no topic to tell subject words from filler


def test_repetition_check_reports_and_lists_top_words(tmp_path):
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["Title", "We ran it. We saw it. We liked it. We stopped.", "References", "We a. We b. We c. We d."])
    status, message, detail = _run(doc)["prose.repetition"]
    assert status == report.WARNING
    assert "1 run(s) of sentences opening with the same word" in message
    assert "most frequent:" in detail
