"""A likely error when the manuscript refers to a supplement omitted from the submission."""

import pytest

from paperpush import document, report
from paperpush.database import Field, Venue
from paperpush.subfile import SubFile
from paperpush.validate import validate_report
from tests.conftest import _build_docx
from tests.pdf_builder import text_line, write_pdf


CHECK = "citations.supplementary_attachments"
MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
SUPPLEMENT = Field(id="supplementary_files", label="Supplement", type="filelist")


def _run(path=None, *, supplement=SUPPLEMENT, values=None, disable=False):
    venue = Venue(slug="test_supplement", name="Test", fields=[MANUSCRIPT, supplement])
    values = {**({"manuscript_file": str(path)} if path else {}), **(values or {})}
    result = validate_report(
        SubFile(venue=venue.slug, values=values), venue,
        enable=["citations"] if disable else [CHECK], disable=[CHECK] if disable else [],
        check_links=False, check_references=False, check_sensitive=False,
        check_hidden_text=False, check_openreview=False,
    )
    return result, next(item for item in result.results if item.id == CHECK)


@pytest.mark.parametrize("reference", [
    "Supplementary Material", "supplemental information", "Supporting Information",
    "Supplementary\nMethods", "Supplementary Appendix", "Supplementary File 1",
    "Supplement 1", "Additional file 2", "S1 Fig.", "S2 Table",
    "Fig. S1", "Table S2", "Supplementary Figure 1", "Supplementary Table 2",
    "Extended Data Fig. 1", "in the supplement",
])
def test_referenced_supplement_without_attachment_is_a_likely_error(tmp_path, reference):
    path = tmp_path / "manuscript.txt"
    path.write_text(f"See {reference} for further details.\n")
    validation, check = _run(path)
    assert check.status == report.LIKELY_ERROR
    assert len(check.issues) == 1
    issue = check.issues[0]
    assert issue.field == "manuscript_file"
    assert "no supplementary files are attached" in issue.message
    assert " ".join(reference.split()).rstrip(".") in issue.message
    assert issue.level == "likely_error"
    assert validation.ok  # A missing supplement is not a blocking error.


@pytest.mark.parametrize("field", [
    SUPPLEMENT,
    Field(id="supplementary_file", label="Supplement", type="file"),
    Field(id="technical_supplement", label="Technical supplement", type="file"),
])
def test_one_combined_supplement_satisfies_multiple_references(tmp_path, field):
    path = tmp_path / "manuscript.txt"
    path.write_text("See Supplementary Material, Fig. S1 and Table S2.\n")
    supplement = tmp_path / "combined.txt"
    supplement.write_text("Supplementary figures and tables.\n")
    value = str(supplement) + (" | All supplementary material" if field.type == "filelist" else "")
    _, check = _run(path, supplement=field, values={field.id: value})
    assert check.status == report.PASSED


@pytest.mark.parametrize("text", [
    "See Figure 1 and Table 2 for details.\n",
    "A supplementary analysis improved accuracy.\n",
    "Introduction\nResults are described here.\nReferences\nSmith (2020). Supplementary Material.\n",
])
def test_no_supplement_reference_does_not_warn(tmp_path, text):
    path = tmp_path / "manuscript.txt"
    path.write_text(text)
    _, check = _run(path)
    assert check.status == report.NOT_APPLICABLE


@pytest.mark.parametrize("suffix", [".pdf", ".docx", ".tex"])
def test_document_formats(tmp_path, monkeypatch, suffix):
    path = tmp_path / f"manuscript{suffix}"
    text = "See Supplementary Material for details."
    if suffix == ".pdf":
        write_pdf(path, [text_line(72, 700, text)])
    elif suffix == ".docx":
        _build_docx(path, [text])
    else:
        path.write_text(r"\documentclass{article}\begin{document}" + text + r"\end{document}")
        monkeypatch.setattr(document.Document, "pdf", property(lambda self: None))
    _, check = _run(path)
    assert check.status == report.LIKELY_ERROR


def test_unreadable_manuscript_is_unchecked(tmp_path):
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not a document")
    _, check = _run(path)
    assert check.status == report.UNCHECKED


def test_no_manuscript_is_not_applicable():
    _, check = _run()
    assert check.status == report.NOT_APPLICABLE


def test_check_can_be_disabled(tmp_path):
    path = tmp_path / "manuscript.txt"
    path.write_text("See Supplementary Material.\n")
    _, check = _run(path, disable=True)
    assert check.status == report.UNCHECKED


def test_a_supplement_captioned_in_the_manuscript_itself_is_not_reported(tmp_path):
    # An appendix carrying "Supplementary Figure 1" in the same file: nothing is missing.
    path = tmp_path / "manuscript.txt"
    path.write_text("Results are in Supplementary Figure 1.\n\nSupplementary Figure 1. Raw data for every sample.\n")
    _, check = _run(path)
    assert check.status == report.NOT_APPLICABLE


FILLER = " ".join(["The supplementary analysis repeats the main experiment on every held-out sample."] * 20)
BIB = "\nReferences\nA. Author. A paper title. Journal, 2020.\nB. Author. Another paper. Journal, 2021.\n"
MAIN = "Introduction\n" + " ".join(["The main text describes the method and its results in detail."] * 15) + "\n"


@pytest.mark.parametrize("text", [
    # The appendix sits at the end of the file, before the reference list (LaTeX \appendix before \bibliography is rare, Word often does this).
    MAIN + "Details are in the Supplementary Material.\n\nSupplementary Material\n" + FILLER + "\n" + BIB,
    # After the reference list.
    MAIN + "Details are in the Supporting Information.\n" + BIB + "\nSupporting Information\n" + FILLER + "\n",
    # A numbered note whose heading is in the file's own supplement.
    MAIN + "The derivation is in Supplementary Note 2.\n\nSupplementary Information\nSupplementary Note 1: Data\n" + FILLER + "\nSupplementary Note 2: Derivation\n" + FILLER + "\n" + BIB,
])
def test_supplement_at_the_end_of_the_same_file_is_not_reported(tmp_path, text):
    path = tmp_path / "manuscript.txt"
    path.write_text(text)
    _, check = _run(path)
    assert check.status == report.NOT_APPLICABLE, check.issues


@pytest.mark.parametrize("text", [
    # The note the text cites is not in the file's supplement.
    MAIN + "The derivation is in Supplementary Note 3.\n\nSupplementary Information\nSupplementary Note 1: Data\n" + FILLER + "\n" + BIB,
    # A data file or a video cannot be carried in the manuscript, supplement or not.
    MAIN + "Raw counts are in Supplementary Data 1.\n\nSupplementary Information\nSupplementary Data 1\n" + FILLER + "\n" + BIB,
    # Only a short list of the separate files' legends at the end.
    MAIN + "Details are in the Supplementary Information.\n\nSupplementary Information\nSupplementary Figures 1-5 and Supplementary Tables 1-3.\n" + BIB,
])
def test_supplement_not_carried_in_the_file_is_still_reported(tmp_path, text):
    path = tmp_path / "manuscript.txt"
    path.write_text(text)
    _, check = _run(path)
    assert check.status == report.LIKELY_ERROR
