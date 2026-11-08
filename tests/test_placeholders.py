"""``details.placeholders``: drafting leftovers in the submission's files."""

from __future__ import annotations

from zipfile import ZipFile

import pytest

from paperpush import placeholders
from paperpush.database import Field, Venue
from paperpush.validate import NotApplicable

VENUE = Venue(slug="testj", name="Test", fields=[Field(id="manuscript_file", label="Manuscript", type="file"), Field(id="cover_letter", label="Cover letter", type="file")])


@pytest.mark.parametrize(
    "text, kind",
    [
        ("The effect was XX% larger.", "number placeholder"),
        ("We enrolled n = X participants.", "number placeholder"),
        ("As shown before [ref].", "citation placeholder"),
        ("A known result [citation needed].", "citation placeholder"),
        ("Smith et al., 20XX found it.", "number placeholder"),
        ("See Figure X for the design.", "figure or table placeholder"),
        ("TODO: rewrite this paragraph.", "to-do note"),
        ("[Insert reference here]", "to-do note"),
        ("Dear [Editor name],", "template placeholder"),
        ("Lorem ipsum dolor sit amet.", "template placeholder"),
    ],
)
def test_placeholders_found(text, kind):
    assert kind in {k for k, _ in placeholders.scan_text(text)}


@pytest.mark.parametrize(
    "text",
    [
        "Female (XX) and male (XY) mice were compared.",
        "See Figure 2 and Table S1 (p = 0.01, n = 12).",
        "The X chromosome and the XIST gene.",
        "We reference prior work [12].",
    ],
)
def test_ordinary_text_is_clean(text):
    assert placeholders.scan_text(text) == []


def test_check_reports_each_file(tmp_path):
    paper = tmp_path / "paper.md"
    paper.write_text("Results\n\nThe effect was XX% larger [ref].", encoding="utf-8")
    letter = tmp_path / "letter.md"
    letter.write_text("Dear [Editor name], please consider our manuscript.", encoding="utf-8")
    out = placeholders.check(VENUE, {"manuscript_file": str(paper), "cover_letter": str(letter)})
    assert {i.field for i in out.issues} == {"manuscript_file", "cover_letter"}
    assert len(out.issues) == 3


def test_word_comments_and_highlighting(tmp_path):
    path = tmp_path / "paper.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", '<w:document><w:body><w:p><w:r><w:rPr><w:highlight w:val="yellow"/></w:rPr><w:t>check this</w:t></w:r></w:p></w:body></w:document>')
        archive.writestr("word/comments.xml", '<w:comments><w:comment w:id="0"><w:p><w:r><w:t>Is this right?</w:t></w:r></w:p></w:comment></w:comments>')
    messages = [i.message for i in placeholders.check(VENUE, {"manuscript_file": str(path)}).issues]
    assert any("1 comment(s)" in m and "Is this right?" in m for m in messages)
    assert any("highlighted" in m and "check this" in m for m in messages)


def test_latex_todo_notes(tmp_path):
    tex = tmp_path / "ms.tex"
    tex.write_text("\\documentclass{article}\\usepackage{todonotes}\\begin{document}Text \\todo{fix the stats} more.\\end{document}", encoding="utf-8")
    messages = [i.message for i in placeholders.check(VENUE, {"manuscript_file": str(tex)}).issues]
    assert any("todonotes" in m for m in messages) and any("fix the stats" in m for m in messages)


def test_not_applicable_without_files():
    with pytest.raises(NotApplicable):
        placeholders.check(VENUE, {})
