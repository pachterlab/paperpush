"""Tests for ``validate --arxiv-latex-cleaner`` (paperpush.latex_cleaner)."""

from __future__ import annotations

import tarfile
import zipfile

import pytest

from paperpush import latex_cleaner
from paperpush.cli import main
from paperpush.database import Field, Venue

# The cleaner itself is the optional arxiv_latex_cleaner ("paperpush[validate]").
needs_cleaner = pytest.mark.skipif(not latex_cleaner.available(), reason="needs arxiv_latex_cleaner (pip install 'paperpush[validate]')")

TEX = "\\documentclass{article}\n% TODO tidy before arxiv\n\\begin{document}\nHi\n\\end{document}\n"


def _venue(*fields: Field, slug: str = "testj") -> Venue:
    return Venue(slug=slug, name="Test Venue", fields=list(fields))


def _zip(path, members: dict[str, str]):
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return path


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


@needs_cleaner
def test_clean_zip_writes_cleaned_copy(tmp_path):
    src = _zip(tmp_path / "source.zip", {"main.tex": TEX, "main.aux": "junk"})
    venue = _venue(Field(id="manuscript_file", label="Source", type="file"))

    values, cleaned, failures = latex_cleaner.clean_values(venue, {"manuscript_file": str(src)})

    out = tmp_path / "source_arXiv.zip"
    assert failures == []
    assert values["manuscript_file"] == str(out)
    assert [(c.original, c.cleaned) for c in cleaned] == [(src, out)]
    with zipfile.ZipFile(out) as zf:
        assert sorted(zf.namelist()) == ["main.tex"]
        assert "TODO" not in zf.read("main.tex").decode()
    # The original is untouched.
    with zipfile.ZipFile(src) as zf:
        assert "main.aux" in zf.namelist()


@needs_cleaner
def test_clean_tar_gz_keeps_format(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "main.tex").write_text(TEX, encoding="utf-8")
    src = tmp_path / "source.tar.gz"
    with tarfile.open(src, "w:gz") as tf:
        tf.add(tmp_path / "s" / "main.tex", arcname="main.tex")
    venue = _venue(Field(id="manuscript_file", label="Source", type="file"))

    values, cleaned, _ = latex_cleaner.clean_values(venue, {"manuscript_file": str(src)})

    out = tmp_path / "source_arXiv.tar.gz"
    assert values["manuscript_file"] == str(out)
    with tarfile.open(out) as tf:
        text = tf.extractfile("./main.tex").read().decode()
    assert "TODO" not in text


@needs_cleaner
def test_clean_tex_and_filelist_subfields(tmp_path):
    paper = tmp_path / "paper"
    paper.mkdir()
    tex = paper / "main.tex"
    tex.write_text(TEX, encoding="utf-8")
    venue = _venue(
        Field(id="manuscript_file", label="Source", type="file"),
        Field(id="supp", label="Supplements", type="filelist"),
    )

    values, cleaned, _ = latex_cleaner.clean_values(venue, {"manuscript_file": str(tex), "supp": f"{tex} | Supplementary Material"})

    out = (tmp_path / "paper_arXiv" / "main.tex").resolve()
    assert values["manuscript_file"] == str(out)
    assert values["supp"] == f"{out} | Supplementary Material"
    assert "TODO" not in out.read_text(encoding="utf-8")
    assert len(cleaned) == 2


@needs_cleaner
def test_cli_validate_arxiv_latex_cleaner_silences_reminder(tmp_path, monkeypatch, capsys):
    import paperpush.cli as cli
    import paperpush.venues as venues

    j = _venue(Field(id="manuscript_file", label="Source", type="file", required=True), slug="arxiv")
    monkeypatch.setattr(cli, "get_venue", lambda slug: j)
    monkeypatch.setattr(venues, "submission_base", lambda slug: "arxiv")
    src = _zip(tmp_path / "source.zip", {"main.tex": TEX})
    sub = tmp_path / "arxiv.sub"
    sub.write_text(f"@venue: arxiv\nmanuscript_file: {src}\n", encoding="utf-8")

    rc = main(["validate", str(sub), "--arxiv-latex-cleaner", "--dont-check-links", "--dont-check-references"])
    err = capsys.readouterr().err

    assert rc == 0
    assert f"cleaned {src} -> {tmp_path / 'source_arXiv.zip'}" in err
    # The cleaned copy has no comments, so the "run arxiv_latex_cleaner" reminder stays quiet.
    assert "run arxiv_latex_cleaner" not in err
    assert "source comment" not in err


def test_cli_validate_arxiv_latex_cleaner_missing_package(tmp_path, monkeypatch, capsys):
    import paperpush.cli as cli

    monkeypatch.setattr(cli, "get_venue", lambda slug: _venue(Field(id="manuscript_file", label="Source", type="file")))
    monkeypatch.setattr(latex_cleaner, "available", lambda: False)
    sub = tmp_path / "x.sub"
    sub.write_text("@venue: testj\n", encoding="utf-8")

    rc = main(["validate", str(sub), "--arxiv-latex-cleaner"])

    assert rc == 1
    assert "paperpush[validate]" in capsys.readouterr().err
