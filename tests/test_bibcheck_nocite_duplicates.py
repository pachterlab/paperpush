"""Uncited entries put in the list by \\nocite, entries named by their text, and one work listed twice."""

from __future__ import annotations

from pathlib import Path

from paperpush import bibliography_check as bc
from paperpush import report
from paperpush.database import Field, Venue
from paperpush.validate import Checks

MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
VENUE = Venue(slug="testj", name="Test Venue", fields=[MANUSCRIPT])


def _run(path: Path) -> dict[str, tuple[str, str, str]]:
    checks = Checks()
    bc.run_checks(checks, VENUE, {"manuscript_file": str(path)})
    return {res.id: (res.status, " | ".join(i.message for i in res.issues), res.detail) for res in checks.results}


BIB = """@article{doe2020, author={Doe, Jane and Lee, Bo}, title={Growth of cells in culture}, journal={Cell}, year={2020}}
@article{lee2021, author={Lee, Bo}, title={Models that fail}, journal={PNAS}, year={2021}}
@book{stanley1, author={Stanley, Richard P.}, title={Enumerative Combinatorics}, volume={1}, year={2012}}
@book{stanley2, author={Stanley, Richard P.}, title={Enumerative Combinatorics}, volume={2}, year={1999}}
"""


def _tex(tmp_path: Path, body: str, bib: str = BIB) -> Path:
    (tmp_path / "refs.bib").write_text(bib)
    tex = tmp_path / "ms.tex"
    tex.write_text(body + "\n\\bibliography{refs}\n")
    return tex


# --- \nocite ---------------------------------------------------------------------------------


def test_nocite_key_never_cited_is_reported(tmp_path):
    tex = _tex(tmp_path, "Cells grow \\cite{doe2020}.\n\\nocite{lee2021}")
    status, message, _ = _run(tex)["bibliography.uncited"]
    assert status == report.WARNING
    assert "by \\nocite but never cited in the text: lee2021" in message


def test_nocite_key_also_cited_is_fine(tmp_path):
    tex = _tex(tmp_path, "Cells grow \\cite{doe2020} \\citep{lee2021}.\n\\nocite{lee2021}")
    assert _run(tex)["bibliography.uncited"][0] == report.PASSED


def test_nocite_star_is_not_reported_as_nocite(tmp_path):
    tex = _tex(tmp_path, "Cells grow \\cite{doe2020,lee2021,stanley1}.\n\\nocite{*}")
    status, message, _ = _run(tex)["bibliography.uncited"]
    assert status == report.WARNING and "by \\nocite" not in message and "stanley2" in message


def test_nocite_with_bibitems(tmp_path):
    tex = tmp_path / "ms.tex"
    tex.write_text("Body \\cite{a}.\\nocite{b}\n\\begin{thebibliography}{9}\\bibitem{a} Doe, J. X.\\bibitem{b} Lee. Y.\\end{thebibliography}")
    status, message, _ = _run(tex)["bibliography.uncited"]
    assert status == report.WARNING and "by \\nocite but never cited in the text: b" in message
    assert "1 of 2 entries" not in message  # reported once, not also as a plain uncited entry


def test_numbered_uncited_entry_is_named_by_its_text(tmp_path):
    md = tmp_path / "ms.md"
    md.write_text("Body [1].\n\nReferences\n\n[1] Doe, J. Growth of cells. Cell, 2020.\n[2] Kawai, T. Keratin in the skin barrier. Nature, 2015.\n")
    status, message, _ = _run(md)["bibliography.uncited"]
    assert status == report.WARNING and '[2] "Kawai, T. Keratin in the skin barrier.' in message


# --- duplicates in the .bib ------------------------------------------------------------------


def test_same_work_under_two_keys(tmp_path):
    bib = BIB + "@article{doe2020_copy, author={Doe, J. and Lee, B.}, title={Growth of {Cells} in Culture}, journal={Cell}, year={2020}}\n"
    tex = _tex(tmp_path, "Cells \\cite{doe2020} and \\cite{doe2020_copy}.", bib)
    status, message, _ = _run(tex)["bibliography.entries"]
    assert status == report.WARNING
    assert "'doe2020' and 'doe2020_copy' are the same work" in message and "listed twice" in message


def test_duplicate_not_printed_is_not_reported(tmp_path):
    # Only one of the two keys is cited, so BibTeX prints the work once.
    bib = BIB + "@article{doe2020_copy, author={Doe, J.}, title={Growth of cells in culture}, year={2020}}\n"
    tex = _tex(tmp_path, "Cells \\cite{doe2020}.", bib)
    assert _run(tex)["bibliography.entries"][0] == report.PASSED


def test_volumes_of_one_book_are_different_works(tmp_path):
    tex = _tex(tmp_path, "See \\cite{stanley1,stanley2}.")
    assert _run(tex)["bibliography.entries"][0] == report.PASSED


def test_same_title_other_first_author_is_not_a_duplicate(tmp_path):
    bib = BIB + "@article{roe2020, author={Roe, Q.}, title={Growth of cells in culture}, year={2020}}\n"
    tex = _tex(tmp_path, "Cells \\cite{doe2020,roe2020}.", bib)
    assert _run(tex)["bibliography.entries"][0] == report.PASSED


# --- duplicates in a printed reference list --------------------------------------------------


def test_numbered_list_with_one_work_twice(tmp_path):
    md = tmp_path / "ms.md"
    md.write_text(
        "Body [1, 2, 3].\n\nReferences\n\n"
        "[1] Doe, J. and Lee, B. Growth of cells in culture. Cell, 2020.\n"
        "[2] Lee, B. Models that fail. PNAS, 2021.\n"
        "[3] Doe, J. and Lee, B. Growth of cells in culture. Cell, 2020.\n"
    )
    status, message, _ = _run(md)["bibliography.entries"]
    assert status == report.WARNING and "[1] and [3] are the same work" in message


def test_author_year_list_with_one_work_twice(tmp_path):
    md = tmp_path / "ms.md"
    md.write_text(
        "Body (Doe et al., 2020a; Doe et al., 2020b; Lee, 2021).\n\nReferences\n\n"
        "Doe, J., Lee, B. (2020a). Growth of cells in culture. Cell 1, 2.\n"
        "Doe, J., Lee, B. (2020b). Growth of cells in culture. Cell 1, 2.\n"
        "Lee, B. (2021). Models that fail. PNAS 3, 4.\n"
    )
    status, message, _ = _run(md)["bibliography.entries"]
    assert status == report.WARNING and "listed twice" in message


def test_printed_list_without_duplicates_passes(tmp_path):
    md = tmp_path / "ms.md"
    md.write_text("Body [1, 2].\n\nReferences\n\n[1] Doe, J. Growth of cells in culture. Cell, 2020.\n[2] Doe, J. Growth of cells in culture II. Cell, 2021.\n")
    assert _run(md)["bibliography.entries"][0] == report.PASSED


def test_back_reference_pages_do_not_hide_a_duplicate(tmp_path):
    md = tmp_path / "ms.md"
    md.write_text(
        "Body [1, 2, 3].\n\nReferences\n\n"
        "[1] Richard Blundell and Monica Costa Dias. Alternative approaches to evaluation. Journal of human resources, 44(3):565-640, 2009. 3\n"
        "[2] Richard Blundell and Monica Costa Dias. Alternative approaches to evaluation. Journal of human resources, 44(3):565-640, 2009. 4, 7\n"
        "[3] Lee, B. Models that fail. PNAS, 2021.\n"
    )
    status, message, _ = _run(md)["bibliography.entries"]
    assert status == report.WARNING and "[1] and [2] are the same work" in message
