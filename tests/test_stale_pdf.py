"""Tests for ``integrity.stale_pdf`` (paperpush.stale_pdf)."""

from __future__ import annotations

import os

from paperpush import report, stale_pdf
from paperpush.database import Field, Venue
from paperpush.subfile import SubFile
from paperpush.validate import validate_report
from tests.pdf_builder import text_line, write_pdf

SENTENCES = [
    "We measured the growth of bacterial colonies across several temperatures and media.",
    "Colonies grown on rich media expanded faster than those grown on minimal media.",
    "These results suggest that nutrient availability limits expansion more than temperature does.",
]
NEW_SENTENCE = "A second experiment repeated the whole protocol with a different strain of yeast cells."
OTHER_NEW = "Further controls confirmed that humidity in the incubator had no measurable effect on growth."


def _pdf(path, sentences):
    lines = "".join(text_line(72, 720 - 20 * i, s) for i, s in enumerate(sentences))
    return write_pdf(path, [lines])


def _tex(path, sentences):
    body = "\n\n".join(sentences)
    path.write_text(f"\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n", encoding="utf-8")
    return path


def _age(path, seconds):
    """Set ``path``'s modification time ``seconds`` into the past."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime - seconds))


def _check(pdf):
    venue = Venue(slug="testj", name="Test Venue", fields=[Field(id="manuscript_file", label="Manuscript", type="file")])
    subfile = SubFile(venue="testj", values={"manuscript_file": str(pdf)})
    result = validate_report(subfile, venue, check_links=False, check_references=False, check_manuscript=False)
    return next(c for c in result.results if c.id == "integrity.stale_pdf")


def test_up_to_date_pdf_passes(tmp_path):
    _tex(tmp_path / "main.tex", SENTENCES)
    pdf = _pdf(tmp_path / "main.pdf", SENTENCES)
    _age(tmp_path / "main.tex", 60)
    result = _check(pdf)
    assert result.status == report.PASSED
    assert "matches main.tex" in result.detail


def test_newer_source_with_new_sentence_warns(tmp_path):
    pdf = _pdf(tmp_path / "main.pdf", SENTENCES)
    _age(pdf, 3600)
    _tex(tmp_path / "main.tex", SENTENCES + [NEW_SENTENCE])
    result = _check(pdf)
    assert result.status == report.WARNING
    message = result.issues[0].message
    assert "saved 1 hour after main.pdf" in message
    assert "1 of its 4 prose sentences" in message
    assert "A second experiment" in message


def test_newer_source_with_same_text_passes_with_note(tmp_path):
    pdf = _pdf(tmp_path / "main.pdf", SENTENCES)
    _age(pdf, 3600)
    _tex(tmp_path / "main.tex", SENTENCES)
    result = _check(pdf)
    assert result.status == report.PASSED
    assert "all 3 of its prose sentences appear in the PDF" in result.detail


def test_copied_files_with_two_new_sentences_warn_without_times(tmp_path):
    # Same modification time (as after unzipping a download), but the text differs.
    _tex(tmp_path / "main.tex", SENTENCES + [NEW_SENTENCE, OTHER_NEW])
    pdf = _pdf(tmp_path / "main.pdf", SENTENCES)
    os.utime(tmp_path / "main.tex", (pdf.stat().st_atime, pdf.stat().st_mtime))
    result = _check(pdf)
    assert result.status == report.WARNING
    assert "saved" not in result.issues[0].message
    assert "2 of its 5 prose sentences" in result.issues[0].message


def test_one_missing_sentence_without_newer_source_passes(tmp_path):
    _tex(tmp_path / "main.tex", SENTENCES + [NEW_SENTENCE])
    pdf = _pdf(tmp_path / "main.pdf", SENTENCES)
    _age(tmp_path / "main.tex", 60)
    assert _check(pdf).status == report.PASSED


def test_input_file_newer_than_pdf_counts(tmp_path):
    (tmp_path / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\n\\input{body}\n\\end{document}\n", encoding="utf-8")
    (tmp_path / "body.tex").write_text("\n\n".join(SENTENCES + [NEW_SENTENCE]), encoding="utf-8")
    pdf = _pdf(tmp_path / "main.pdf", SENTENCES)
    _age(pdf, 600)
    _age(tmp_path / "main.tex", 1200)
    result = _check(pdf)
    assert result.status == report.WARNING
    assert "saved 10 minutes after" in result.issues[0].message


def test_markdown_source(tmp_path):
    pdf = _pdf(tmp_path / "paper.pdf", SENTENCES)
    _age(pdf, 120)
    (tmp_path / "paper.md").write_text("# Results\n\n" + "\n\n".join(SENTENCES + [NEW_SENTENCE]) + "\n", encoding="utf-8")
    result = _check(pdf)
    assert result.status == report.WARNING
    assert "paper.md" in result.issues[0].message


def test_math_sentences_are_not_compared(tmp_path):
    tex = _tex(tmp_path / "main.tex", ["The loss is $L = \\sum_i x_i^2 + \\lambda \\|w\\|$ over all of the samples in the batch.", SENTENCES[0]])
    assert [s for s, _ in stale_pdf.prose_sentences(stale_pdf.source_text(tex))] == [SENTENCES[0]]


def test_run_together_pdf_words_still_match():
    pdf_text = SENTENCES[0].replace("colonies across several", "coloniesacrossseveral")
    assert stale_pdf.compare(SENTENCES[0], pdf_text).missing == []


def test_no_source_is_not_applicable(tmp_path):
    pdf = _pdf(tmp_path / "main.pdf", SENTENCES)
    assert _check(pdf).status == report.NOT_APPLICABLE


def test_no_pdf_is_not_applicable(tmp_path):
    tex = _tex(tmp_path / "main.tex", SENTENCES)
    assert _check(tex).status == report.NOT_APPLICABLE
