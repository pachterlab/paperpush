from pathlib import Path
from zipfile import ZipFile

from paperpush.database import Field, Venue
from paperpush.feature_check import _reference_order, _word_file_cleanliness, run_checks
from paperpush.validate import Checks


VENUE = Venue(slug="testj", name="Test", fields=[Field(id="manuscript_file", label="Manuscript", type="file")])


def _manuscript(tmp_path: Path, refs: list[str], citations: list[str]) -> dict[str, str]:
    path = tmp_path / "paper.md"
    path.write_text("Introduction\n\n" + " ".join(citations) + "\n\nReferences\n\n" + "\n".join(refs), encoding="utf-8")
    return {"manuscript_file": str(path)}


def _refs(*surnames: str) -> list[str]:
    return [f"{name}, A. (2020). A sufficiently long reference title. Journal 1, 2." for name in surnames]


def test_reference_order_warns_when_neither_order_holds(tmp_path):
    values = _manuscript(tmp_path, _refs("Meyer", "Zebra", "Apple"), ["Zebra (2020).", "Apple (2020).", "Meyer (2020)."])
    out = _reference_order(VENUE, values, False, False)
    assert len(out.issues) == 1
    assert "neither alphabetical nor ordered by first citation" in out.issues[0].message


def test_reference_order_passes_when_citation_order_holds(tmp_path):
    values = _manuscript(tmp_path, _refs("Zebra", "Apple", "Meyer"), ["Zebra (2020).", "Apple (2020).", "Meyer (2020)."])
    assert not _reference_order(VENUE, values, False, False).issues
    assert "not alphabetized" in _reference_order(VENUE, values, True, False).issues[0].message


def test_numbered_references_follow_first_citation(tmp_path):
    values = _manuscript(tmp_path, [
        "1. Meyer, M. A sufficiently long reference title. Journal 1, 2 (2019).",
        "2. Apple, A. Another sufficiently long reference title. Journal 1, 2 (2020).",
    ], ["The result follows [2] and later [1]."])
    out = _reference_order(VENUE, values, False, False)
    assert "not listed in order of first citation" in out.issues[0].message


def test_word_cleanliness_finds_tracked_changes_and_endnote(tmp_path):
    path = tmp_path / "paper.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", '<w:document><w:ins w:id="1"><w:t>edit</w:t></w:ins><w:instrText>ADDIN EN.CITE</w:instrText></w:document>')
    out = _word_file_cleanliness(VENUE, {"manuscript_file": str(path)})
    assert len(out.issues) == 2


def test_reader_checks_are_queued(tmp_path):
    values = _manuscript(tmp_path, _refs("Apple", "Meyer"), ["Apple (2020).", "Meyer (2020)."])
    checks = Checks()
    run_checks(checks, VENUE, values)
    assert {task.id for task in checks.ai_tasks} == {
        "ai.details.ethics", "ai.details.declarations", "ai.details.author_details",
        "ai.details.abstract_reporting", "ai.details.supplementary", "ai.details.trademarks",
        "ai.details.misplaced_content", "ai.details.heading_content", "ai.details.nih_grant_numbers",
    }


# --- affiliation superscripts ------------------------------------------------

from paperpush.feature_check import _affiliation_superscripts  # noqa: E402

from tests.pdf_builder import text_line, write_pdf  # noqa: E402

AUTHOR_VENUE = Venue(slug="testj", name="Test", fields=[
    Field(id="manuscript_file", label="Manuscript", type="file"),
    Field(id="authors", label="Authors", type="authorlist", fields=["name", "email?", "affiliation?", "orcid?", "corresponding"]),
])
AUTHORS = "Joe Rich | joe@example.edu | USC | | yes\nAnn Lee | | USC | | no"


def _run(path: Path):
    return _affiliation_superscripts(AUTHOR_VENUE, {"manuscript_file": str(path), "authors": AUTHORS})


def _docx(path: Path, body: str) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", f"<w:document><w:body>{body}</w:body></w:document>")
    return path


_SUP = '<w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'


def _p(*runs: tuple[str, bool]) -> str:
    return "<w:p>" + "".join(f'<w:r>{_SUP if sup else ""}<w:t xml:space="preserve">{t}</w:t></w:r>' for t, sup in runs) + "</w:p>"


def test_affiliation_inline_number_in_word_is_flagged(tmp_path):
    body = _p(("A title", False)) + _p(("Joe Rich", False), ("1", True), (", Ann Lee2", False))
    out = _run(_docx(tmp_path / "paper.docx", body))
    assert [i.message for i in out.issues] == ["paper.docx: the affiliation number after 'Ann Lee' is not superscripted (e.g. 'Ann Lee1' should be 'Ann Lee¹')"]


def test_affiliation_check_stops_at_the_abstract(tmp_path):
    body = _p(("Joe Rich", False), ("1", True)) + _p(("Abstract", False)) + _p(("As Ann Lee2 showed", False))
    assert not _run(_docx(tmp_path / "paper.docx", body)).issues


def test_affiliation_inline_number_in_latex_and_markdown(tmp_path):
    tex = tmp_path / "paper.tex"
    tex.write_text("\\documentclass{article}\\title{T}\\author{Joe Rich$^{1}$, Ann Lee2}\\begin{document}\\maketitle Joe Rich3\\end{document}")
    assert [i.message.split("'")[1] for i in _run(tex).issues] == ["Ann Lee"]
    md = tmp_path / "paper.md"
    md.write_text("# Title\n\nJoe Rich1, Ann Lee^2^\n\n## Abstract\n\nAnn Lee2\n")
    assert [i.message.split("'")[1] for i in _run(md).issues] == ["Joe Rich"]


def test_affiliation_inline_number_in_pdf_first_page_only(tmp_path):
    page1 = text_line(72, 700, "Joe Rich", size=10) + text_line(113, 704, "1", size=6) + text_line(120, 700, ", Ann Lee2", size=10)
    page2 = text_line(72, 700, "Joe Rich2 again", size=10)
    pdf = write_pdf(tmp_path / "paper.pdf", [page1, page2])
    assert [i.message.split("'")[1] for i in _run(pdf).issues] == ["Ann Lee"]


def test_affiliation_check_needs_authors(tmp_path):
    import pytest
    from paperpush.validate import NotApplicable

    with pytest.raises(NotApplicable):
        _affiliation_superscripts(AUTHOR_VENUE, {"manuscript_file": str(_docx(tmp_path / "p.docx", _p(("x", False))))})
