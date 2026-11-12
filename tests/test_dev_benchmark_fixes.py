"""Regression tests for checks added after the planted-error benchmark's dev runs."""

from pathlib import Path


def test_anonymity_flags_github_profile_and_first_person_self_reference():
    from paperpush import anonymity

    text = r"This work extends our earlier system developed at the University of X (Lee et al., 2024), with code at \url{https://github.com/lee-lab}."
    details = [f.detail for f in anonymity.scan_text("m.tex", text, [], is_latex=True)]
    assert any("github.com/lee-lab" in d for d in details)
    assert any("first-person reference" in d for d in details)


def test_third_person_citation_is_not_a_self_reference():
    from paperpush import anonymity

    details = [f.detail for f in anonymity.scan_text("m.tex", "Lee et al. (2024) proposed an earlier system.", [], is_latex=True)]
    assert not any("first-person" in d for d in details)


def test_personal_data_needs_a_label():
    from paperpush.sensitive import _iter_personal_findings

    found = [f.detail for f in _iter_personal_findings("m", "Ann B. (born 14 March 1987; medical record number 30155887) withdrew.")]
    assert any("date of birth" in d for d in found) and any("medical record" in d for d in found)
    assert not list(_iter_personal_findings("m", "Patients born in 1990 were excluded; 30155887 reads were mapped."))


def test_tex_upload_brings_its_bib(tmp_path: Path):
    from paperpush.validate import _tex_bib_files

    (tmp_path / "refs.bib").write_text("@article{a, title={T}}")
    tex = tmp_path / "main.tex"
    tex.write_text("\\documentclass{article}\\begin{document}x\\bibliography{refs,missing}\\end{document}")
    assert _tex_bib_files(tex) == [tmp_path / "refs.bib"]


def test_bib_percent_is_not_a_latex_comment():
    from paperpush.sensitive import _scan_text_blob

    found = list(_scan_text_blob("refs.bib", b"@misc{a, url={https://x.org/a%20b}}\n% note\n", is_latex=True))
    assert not any(f.category == "LaTeX comments" for f in found)


def test_acl_style_author_year_entries_give_the_surname():
    from paperpush.bibliography_check import _surname_candidates

    assert "Bender" in _surname_candidates("Emily M. Bender, Timnit Gebru, and Shmargaret Shmitchell. 2021. On the dangers")
    assert "Adelani" in _surname_candidates("David Ifeoluwa Adelani, Hannah Liu, and X. 2024. SIB-200")
    assert _surname_candidates("Smith, J., and Doe, A. (2020). Title.")[0] == "Smith"


def test_continuation_line_is_not_an_author_year_entry():
    from paperpush.bibliography_check import _AUTHOR_YEAR_HEAD

    assert _AUTHOR_YEAR_HEAD.search("Vera Boteva, Demian Gholipour, and Stefan Riezler. 2016. A Full-Text Learning")
    assert _AUTHOR_YEAR_HEAD.search("Smith, J. (2020). Title of the work.")
    assert not _AUTHOR_YEAR_HEAD.search("InProceedings of the 18th Conference of the European Chapter, pages 226-245, St. Julian's")


def test_general_lettering_floor_flags_mostly_tiny_text(tmp_path):
    from paperpush import figure_check as F
    from paperpush import requirements as r

    texts = [F.Text(w, 2.0, "Helvetica", 0, 0) for w in ("Loss", "Accuracy", "Epoch", "Baseline")]
    fig = F.Figure("f.pdf", tmp_path / "f.pdf", "fig", True, "vector", texts=texts)

    class _C:
        def record(self, *a, **k):
            pass

        def add_ai_task(self, *a, **k):
            pass

    out = F._text_size_check(_C(), F._Inventory([fig], True), r.FigureRules(), "x")
    assert out.issues and "too small to read" in out.issues[0].message
