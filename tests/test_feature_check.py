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
        "1. Apple, A. A sufficiently long reference title. Journal 1, 2 (2019).",
        "2. Meyer, M. Another sufficiently long reference title. Journal 1, 2 (2020).",
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
    }
