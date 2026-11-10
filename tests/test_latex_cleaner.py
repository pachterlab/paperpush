"""Tests for the ``integrity.latex_cleaner`` preview (paperpush.latex_cleaner)."""

from __future__ import annotations

import hashlib
import tarfile
import zipfile

import pytest

from paperpush import latex_cleaner, report
from paperpush.cli import main
from paperpush.database import Field, Venue
from paperpush.subfile import SubFile
from paperpush.validate import validate_report

# The cleaner itself is the optional arxiv_latex_cleaner ("paperpush[validate]").
needs_cleaner = pytest.mark.skipif(not latex_cleaner.available(), reason="needs arxiv_latex_cleaner (pip install 'paperpush[validate]')")

TEX = "\\documentclass{article}\n% TODO tidy before arxiv\n\\begin{document}\nHi\n\\end{document}\n"
CLEAN_TEX = "\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}\n"
FLAGS = ["--disable-integrity.links", "--disable-references"]


def _venue(*fields: Field, slug: str = "testj") -> Venue:
    return Venue(slug=slug, name="Test Venue", fields=list(fields))


def _zip(path, members: dict[str, str]):
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return path


def _snapshot(root) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def _validate_arxiv(tmp_path, monkeypatch, capsys, source, *flags, slug="arxiv"):
    import paperpush.cli as cli
    import paperpush.venues as venues

    j = _venue(Field(id="manuscript_file", label="Source", type="file", required=True), slug=slug)
    monkeypatch.setattr(cli, "get_venue", lambda s: j)
    monkeypatch.setattr(venues, "submission_base", lambda s: slug)
    sub = tmp_path / f"{slug}.sub"
    sub.write_text(f"@venue: {slug}\nmanuscript_file: {source}\n", encoding="utf-8")
    rc = main(["validate", str(sub), *FLAGS, *flags])
    captured = capsys.readouterr()
    return rc, captured.err + captured.out


def _check(source):
    """The ``integrity.latex_cleaner`` result for a venue taking ``source`` as its one upload."""
    venue = _venue(Field(id="manuscript_file", label="Source", type="file"))
    subfile = SubFile(venue="testj", values={"manuscript_file": str(source)})
    result = validate_report(subfile, venue, check_links=False, check_references=False, check_manuscript=False)
    return next(c for c in result.results if c.id == "integrity.latex_cleaner")


def test_is_latex_source(tmp_path):
    tex = tmp_path / "main.tex"
    tex.write_text(TEX, encoding="utf-8")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    assert latex_cleaner.is_latex_source(tex)
    assert latex_cleaner.is_latex_source(_zip(tmp_path / "src.zip", {"main.tex": TEX}))
    assert not latex_cleaner.is_latex_source(_zip(tmp_path / "figs.zip", {"fig.txt": "x"}))
    assert not latex_cleaner.is_latex_source(pdf)
    assert not latex_cleaner.is_latex_source(tmp_path / "missing.tex")


def test_changed_lines_ignores_line_endings_and_final_newline():
    # The cleaner rewrites these even in a clean file; they are not changes.
    assert latex_cleaner._changed_lines(b"a\r\nb\r\n\r\n", b"a\nb\n") == []
    assert latex_cleaner._changed_lines(b"a\nb", b"a\nb\n") == []
    assert latex_cleaner._changed_lines(b"a\n% x\nb % y\n", b"a\nb %\n") == [2, 3]


def test_describe_lists_counts_and_never_the_comment_text(tmp_path):
    preview = latex_cleaner.Preview(tmp_path / "s.zip", {"main.tex": [4, 6, 10, 11, 12]}, ["main.aux"])
    assert preview.describe() == "s.zip: arxiv_latex_cleaner would change 5 line(s) in 1 file(s) (main.tex: lines 4, 6, 10-12) and remove 1 file(s) (main.aux)"


@needs_cleaner
def test_preview_bundle_reports_changes_and_writes_nothing(tmp_path):
    src = _zip(tmp_path / "source.zip", {"main.tex": TEX, "main.aux": "junk", "refs.bib": "@article{a,title={T}}"})
    before = _snapshot(tmp_path)

    previews, failures = latex_cleaner.preview([src])

    assert failures == []
    assert [p.source for p in previews] == [src.resolve()]
    assert previews[0].changed == {"main.tex": [2]}
    # --keep_bib: the .bib the reference checks read stays.
    assert previews[0].removed == ["main.aux"]
    assert _snapshot(tmp_path) == before


@needs_cleaner
def test_preview_tar_gz(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "main.tex").write_text(TEX, encoding="utf-8")
    src = tmp_path / "source.tar.gz"
    with tarfile.open(src, "w:gz") as tf:
        tf.add(tmp_path / "s" / "main.tex", arcname="main.tex")

    previews, failures = latex_cleaner.preview([src])

    assert failures == []
    assert previews[0].changed == {"main.tex": [2]}


@needs_cleaner
def test_preview_loose_tex_leaves_the_directory_alone(tmp_path):
    tex = tmp_path / "main.tex"
    tex.write_text(TEX, encoding="utf-8")
    # Neighbours are not uploaded with the .tex, so they are not reported.
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "main.aux").write_text("junk", encoding="utf-8")
    before = _snapshot(tmp_path)

    previews, failures = latex_cleaner.preview([tex])

    assert failures == []
    assert previews[0].source == tex.resolve()
    assert previews[0].changed == {"main.tex": [2]}
    assert previews[0].removed == []
    assert _snapshot(tmp_path) == before


@needs_cleaner
def test_preview_clean_source_reports_nothing(tmp_path):
    tex = tmp_path / "main.tex"
    # No final newline: the cleaner adds one, which is not a change.
    tex.write_text(CLEAN_TEX.rstrip("\n"), encoding="utf-8")
    assert latex_cleaner.preview([tex]) == ([], [])


@needs_cleaner
def test_preview_runs_from_an_empty_directory(tmp_path, monkeypatch):
    # `python -m` imports from its working directory, so a planted re.py there
    # must not run. It would kill the cleaner and show up as a failure.
    (tmp_path / "re.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    tex = tmp_path / "main.tex"
    tex.write_text(TEX, encoding="utf-8")
    previews, failures = latex_cleaner.preview([tex])
    assert failures == []
    assert previews[0].changed == {"main.tex": [2]}


def test_preview_reports_a_cleaner_failure(tmp_path, monkeypatch):
    src = _zip(tmp_path / "source.zip", {"main.tex": TEX})

    def boom(folder):
        raise latex_cleaner.CleanerError("exploded")

    monkeypatch.setattr(latex_cleaner, "_run_cleaner", boom)
    assert latex_cleaner.preview([src]) == ([], [(src.resolve(), "exploded")])


@needs_cleaner
def test_validate_warns_on_arxiv_and_changes_nothing(tmp_path, monkeypatch, capsys):
    src = _zip(tmp_path / "source.zip", {"main.tex": TEX})
    before = _snapshot(tmp_path)

    rc, out = _validate_arxiv(tmp_path, monkeypatch, capsys, src)

    assert rc == 0
    assert "arxiv_latex_cleaner would change 1 line(s)" in out
    assert "TODO tidy" not in out.split("arxiv_latex_cleaner would change")[1].split("\n")[0]
    after = _snapshot(tmp_path)
    assert {k: v for k, v in after.items() if k != "arxiv.sub"} == before


@needs_cleaner
def test_validate_is_not_a_warning_on_other_venues(tmp_path, monkeypatch):
    import paperpush.venues as venues

    monkeypatch.setattr(venues, "submission_base", lambda s: s)
    src = _zip(tmp_path / "source.zip", {"main.tex": TEX})

    check = _check(src)

    assert check.status == report.PASSED
    assert "would change 1 line(s)" in check.detail


@needs_cleaner
def test_validate_can_be_disabled(tmp_path, monkeypatch, capsys):
    src = _zip(tmp_path / "source.zip", {"main.tex": TEX})
    _, out = _validate_arxiv(tmp_path, monkeypatch, capsys, src, "--disable-integrity.latex_cleaner")
    assert "arxiv_latex_cleaner would change" not in out


def test_missing_package_is_unchecked_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(latex_cleaner, "available", lambda: False)
    src = _zip(tmp_path / "source.zip", {"main.tex": TEX})

    check = _check(src)

    assert check.status == report.UNCHECKED
    assert "paperpush[validate]" in check.detail


def test_arxiv_reminder_only_without_the_package(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(latex_cleaner, "available", lambda: False)
    tex = tmp_path / "main.tex"
    tex.write_text(TEX, encoding="utf-8")

    _, out = _validate_arxiv(tmp_path, monkeypatch, capsys, tex)

    assert "run arxiv_latex_cleaner on your source" in " ".join(out.split())
    assert "paperpush-fix" in out


def test_no_latex_upload_is_not_applicable(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    assert _check(pdf).status == report.NOT_APPLICABLE


def test_old_flag_is_accepted_and_explains_itself(tmp_path, monkeypatch, capsys):
    tex = tmp_path / "main.pdf"
    tex.write_bytes(b"%PDF-1.4\n")
    rc, out = _validate_arxiv(tmp_path, monkeypatch, capsys, tex, "--arxiv-latex-cleaner")
    assert rc in (0, 1)
    assert "validate never changes your files" in out
    assert not list(tmp_path.glob("*_arXiv*"))
