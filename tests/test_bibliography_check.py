"""paperpush.bibliography_check: every reference cited, and how many are self-citations."""

from __future__ import annotations

from pathlib import Path

from paperpush import bibliography_check as bc
from paperpush import report
from paperpush.database import Field, Venue
from paperpush.validate import Checks
from tests.conftest import _build_docx

MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
AUTHORS = Field(id="authors", label="Authors", type="authorlist")
VENUE = Venue(slug="testj", name="Test Venue", fields=[MANUSCRIPT, AUTHORS])
AUTHOR_LINES = "Jane Doe | jane@uni.edu | Uni | | yes\nJohn Q. Roe | | Uni | | no"


def _run(path: Path, authors: str = AUTHOR_LINES) -> dict[str, tuple[str, str, str]]:
    checks = Checks()
    bc.run_checks(checks, VENUE, {"manuscript_file": str(path), "authors": authors})
    return {res.id: (res.status, " | ".join(i.message for i in res.issues), res.detail) for res in checks.results}


def _md(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "ms.md"
    path.write_text(text, encoding="utf-8")
    return path


NUMBERED = """Title

Introduction

Cells grow [1, 2]. Models fail [3-4]. See also [9].

References

1. Doe, J. & Roe, J. Growth of cells. Nature 1, 2 (2019).
2. Doe, J. Other growth. Cell 3, 4 (2020).
3. Smith, A. Models. Science 5, 6 (2021).
2017.
4. Lee, B. Failing models. PNAS 7, 8 (2022).
5. Roe, J. Q. An uncited thing. eLife 9, 10 (2023).
"""


def test_numbered_uncited_and_undefined(tmp_path):
    status, message, _ = _run(_md(tmp_path, NUMBERED))["bibliography.uncited"]
    assert status == report.WARNING
    assert "1 of 5 references in the reference list of ms.md is never cited in the text: [5]" in message
    assert "cites 1 reference(s) that is not in its reference list: [9]" in message


def test_numbered_all_cited(tmp_path):
    text = NUMBERED.replace("See also [9].", "And [5].")
    status, _, detail = _run(_md(tmp_path, text))["bibliography.uncited"]
    assert status == report.PASSED and detail == "all 5 references cited"


def test_citations_in_legends_after_references_count(tmp_path):
    text = NUMBERED.replace("See also [9].", "") + "\nFigure 1. Growth, as in [5].\n"
    status, _, _ = _run(_md(tmp_path, text))["bibliography.uncited"]
    assert status == report.PASSED


def test_bracket_labels_mid_line(tmp_path):
    text = "Body [1] and [2].\n\nReferences\n\n[1] Doe, J. A thing. arXiv, 2017. [2] Lee, B. Another thing. In ICLR,\n2017. [3] Kim, C. Third thing. 2018.\n"
    status, message, _ = _run(_md(tmp_path, text))["bibliography.uncited"]
    assert status == report.WARNING and message.endswith(": [3]")


def test_alpha_labels(tmp_path):
    text = "Body [BMR+20, Cra17].\n\nReferences\n\n[BMR+20] Brown, T. GPT-3. 2020.\n[Cra17] Crawford, K. Bias. 2017.\n[ZHB+19] Zellers, R. Hellaswag. 2019.\n"
    status, message, _ = _run(_md(tmp_path, text))["bibliography.uncited"]
    assert status == report.WARNING and "[ZHB+19]" in message and "Cra17" not in message


def test_author_year(tmp_path):
    text = """Body text (Smith et al., 2020) and Lee and Kim (2019a).

References

Smith, A., Jones, B. (2020). A study of things. Journal 1, 2.
Lee, C., Kim, D. (2019a). Another study. Journal 3, 4.
Lee, C. (2019b). Not cited at all. Journal 5, 6.
"""
    status, message, _ = _run(_md(tmp_path, text))["bibliography.uncited"]
    assert status == report.WARNING
    assert "Lee (2019b)" in message and "Smith" not in message and "2019a" not in message


def test_superscript_citations_in_docx(tmp_path):
    path = tmp_path / "ms.docx"
    _build_docx(path, ["Cells grow.", "References", "1. Doe, J. Growth. Nature 1 (2019).", "2. Lee, B. Other. Cell 2 (2020)."])
    xml_sup = '<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>1</w:t></w:r>'
    import zipfile

    with zipfile.ZipFile(path) as z:
        members = {n: z.read(n) for n in z.namelist()}
    members["word/document.xml"] = members["word/document.xml"].replace(b"Cells grow.</w:t></w:r>", b"Cells grow.</w:t></w:r>" + xml_sup.encode())
    with zipfile.ZipFile(path, "w") as z:
        for n, data in members.items():
            z.writestr(n, data)
    status, message, _ = _run(path)["bibliography.uncited"]
    assert status == report.WARNING and message.endswith("(superscript citations): [2]")


def test_superscripts_unreadable_in_text(tmp_path):
    text = "Cells grow1,2.\n\nReferences\n\n1. Doe, J. Growth. 2019.\n2. Lee, B. Other. 2020.\n"
    status, _, _ = _run(_md(tmp_path, text))["bibliography.uncited"]
    assert status == report.UNCHECKED


def test_tex_bibitems(tmp_path):
    tex = tmp_path / "ms.tex"
    tex.write_text("Body \\cite{a,b} \\citep{zz}.\n\\begin{thebibliography}{9}\\bibitem{a} Doe, J. X.\\bibitem{b} Lee. Y.\\bibitem{c} Roe, J. Z.\\end{thebibliography}")
    status, message, _ = _run(tex)["bibliography.uncited"]
    assert status == report.WARNING
    assert "never cited in the text: c" in message and "not in its reference list: zz" in message


def test_tex_bib_only_cited_entries_print(tmp_path):
    tex = tmp_path / "ms.tex"
    tex.write_text("Body \\cite{a}.\n\\bibliography{refs}")
    (tmp_path / "refs.bib").write_text("@article{a, author={Doe, Jane}, title={X}, year={2020}}\n@article{b, author={Lee, B}, title={Y}, year={2021}}\n")
    status, _, detail = _run(tex)["bibliography.uncited"]
    assert status == report.PASSED and "BibTeX prints only cited entries" in detail
    tex.write_text("Body \\cite{a}.\\nocite{*}\n\\bibliography{refs}")
    status, message, _ = _run(tex)["bibliography.uncited"]
    assert status == report.WARNING and "(\\nocite{*} prints every .bib entry): b" in message


# --- self-citations ---------------------------------------------------------------------------


def test_is_self_citation():
    authors = [("Jane Doe", "Doe", "J"), ("John Q. Roe", "Roe", "J")]
    ref = lambda text: bc.Ref("1", "[1]", text)  # noqa: E731
    assert bc.is_self_citation(ref("Doe, J. & Lee, B. Growth. Nature (2019)."), authors)
    assert bc.is_self_citation(ref("Lee B, Doe J. Growth. Nature (2019)."), authors)
    assert bc.is_self_citation(ref("Jane Doe and Bo Lee. Growth. In ICLR, 2019."), authors)
    assert bc.is_self_citation(ref("J. Q. Roe. A thing. 2019."), authors)
    assert not bc.is_self_citation(ref("Doe, A. Someone else. 2019."), authors)
    assert not bc.is_self_citation(ref("Smith, A. The Doe family. 2019."), authors)


def test_self_citation_share(tmp_path):
    status, _, detail = _run(_md(tmp_path, NUMBERED))["bibliography.self_citations"]
    assert status == report.PASSED and detail.startswith("3 of 5 references (60%)")
    many = NUMBERED.replace("3. Smith, A.", "3. Doe, J.").replace("4. Lee, B.", "4. Roe, J.")
    status, message, _ = _run(_md(tmp_path, many))["bibliography.self_citations"]
    assert status == report.WARNING and "5 of 5 references (100%)" in message and "more than 80% of the list" in message


def test_self_citation_count(tmp_path):
    refs = "\n".join(f"{i}. Doe, J. Paper {i}. Journal (2020)." for i in range(1, 17))
    refs += "\n" + "\n".join(f"{i}. Lee, B. Paper {i}. Journal (2020)." for i in range(17, 41))
    text = "Body [" + ",".join(str(i) for i in range(1, 41)) + "].\n\nReferences\n\n" + refs + "\n"
    status, message, _ = _run(_md(tmp_path, text))["bibliography.self_citations"]
    assert status == report.WARNING and "16 of 40" in message and "more than 15" in message


def test_self_citations_need_authors(tmp_path):
    status, _, _ = _run(_md(tmp_path, NUMBERED), authors="")["bibliography.self_citations"]
    assert status == report.NOT_APPLICABLE
