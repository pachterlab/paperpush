"""Guards for the project's internal building blocks (no command driven here).

Consolidates three sets of low-level checks that back the CLI commands but are
not commands themselves:

1. **venues.schema.json** -- the generated schema stays in sync with the
   dataclasses, the database conforms, and the schema actually rejects malformed
   entries.
2. **manuscript measures** (:mod:`paperpush.manuscript`) -- word/page counts
   stop at the reference list and unsupported formats report ``None``.
3. **conftest sample-file builders** -- the fixtures the rest of the suite relies
   on produce valid PDFs, DOCX, figures, and LaTeX source bundles, so a builder
   regression surfaces here rather than as a confusing downstream error.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from PIL import Image

from paperpush import manuscript as m
from paperpush.database import DATABASE_PATH, Field
from paperpush.schema_models import build_schema
from paperpush.validate import _check_file_field
# Reuse the same low-level builders the sample fixtures use.
from tests.conftest import _build_docx, _build_pdf

# ===========================================================================
# venues.schema.json
# ===========================================================================
#
# The schema is built from the Field / Venue dataclasses (see
# paperpush.schema_models), the single source of truth for the venue
# database's shape. These tests check: no drift from the dataclasses, the
# database conforms, and the schema bites (rejects representative malformed
# entries and accepts the valid shapes that are easy to get wrong).

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "paperpush" / "venues.schema.json"


def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def test_schema_file_is_up_to_date():
    """The committed schema equals what the dataclasses generate (run
    scripts/gen_venues_schema.py to refresh)."""
    expected = json.dumps(build_schema(), indent=2, ensure_ascii=False) + "\n"
    actual = SCHEMA_PATH.read_text(encoding="utf-8")
    assert actual == expected, "venues.schema.json is out of date; run " "`python scripts/gen_venues_schema.py`"


def test_schema_is_well_formed():
    Draft202012Validator.check_schema(json.loads(SCHEMA_PATH.read_text()))


def test_database_validates_against_schema():
    data = json.loads(DATABASE_PATH.read_text(encoding="utf-8"))
    errors = sorted(_validator().iter_errors(data), key=lambda e: list(e.path))
    assert not errors, "venues.json violates its schema:\n" + "\n".join(f"  {'/'.join(map(str, e.path))}: {e.message}" for e in errors)


# Each entry is validated as the sole venue {"j": ...} (or with a "base" the
# inheriting cases derive from). ``reject`` is whether the schema should flag it.
_MALFORMED = [
    ("typo key", {"name": "J", "fields": [{"id": "x", "lable": "X", "type": "text"}]}),
    ("bad type enum", {"name": "J", "fields": [{"id": "x", "label": "X", "type": "nope"}]}),
    ("standalone missing type", {"name": "J", "fields": [{"id": "x", "label": "X"}]}),
    ("removed_fields on standalone", {"name": "J", "removed_fields": ["a"], "fields": []}),
    ("accept as string", {"name": "J", "fields": [{"id": "x", "label": "X", "type": "file", "accept": ".pdf"}]}),
]


@pytest.mark.parametrize("label,entry", _MALFORMED, ids=[case[0] for case in _MALFORMED])
def test_schema_rejects_malformed_entries(label, entry):
    assert list(_validator().iter_errors({"j": entry})), f"schema accepted a malformed entry: {label}"


def test_schema_accepts_omitted_name():
    # name is optional (defaults to the slug at load time).
    doc = {"j": {"fields": [{"id": "x", "label": "X", "type": "text"}]}}
    assert not list(_validator().iter_errors(doc))


def test_schema_accepts_inheriting_partial_override():
    doc = {
        "base": {"fields": [{"id": "x", "label": "X", "type": "text"}]},
        "j": {"inherits": "base", "fields": [{"id": "x", "default": "hi"}]},
    }
    assert not list(_validator().iter_errors(doc))


def test_schema_accepts_null_delete_in_override():
    # An inheriting override may null out an inherited key to drop it.
    doc = {
        "base": {"fields": [{"id": "x", "label": "X", "type": "text", "help": "h"}]},
        "j": {"inherits": "base", "fields": [{"id": "x", "help": None}]},
    }
    assert not list(_validator().iter_errors(doc))


# ===========================================================================
# manuscript measures (paperpush.manuscript)
# ===========================================================================
#
# These build genuine .tex, .docx, .pdf, and .zip manuscripts (the latter two
# via the conftest builders) and check that the word/page counts stop at the
# reference list and that unsupported formats report None rather than a
# misleading count.

# --- reference truncation (format-agnostic) --------------------------------


@pytest.mark.parametrize(
    "heading",
    [
        "References",
        "REFERENCES",
        "Bibliography",
        "Literature Cited",
        "References and Notes",
        "1. References",
    ],
)
def test_truncate_at_references_cuts_known_headings(heading):
    text = f"Body word one two.\n{heading}\nSmith 2020 ignored ignored."
    assert m.truncate_at_references(text).strip() == "Body word one two."


def test_truncate_keeps_text_when_no_references():
    text = "Just body text with no reference section at all."
    assert m.truncate_at_references(text) == text


def test_passing_mention_of_references_is_not_a_heading():
    # "references" inside a sentence must not be treated as the heading.
    text = "We compare our references to prior work here."
    assert m.truncate_at_references(text) == text


# --- LaTeX -----------------------------------------------------------------


def _tex(body: str, tmp_path: Path, name: str = "main.tex") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_tex_counts_words_before_bibliography(tmp_path):
    tex = _tex(
        r"""
\documentclass{article}
\begin{document}
\section{Intro}
one two three four five
\begin{thebibliography}{9}
\bibitem{a} ignored ignored ignored ignored ignored
\end{thebibliography}
\end{document}
""",
        tmp_path,
    )
    # "Intro" + the five body words = 6 words; bibitem text excluded.
    assert m.words_before_references(tex) == 6


def test_tex_strips_comments(tmp_path):
    tex = _tex("alpha beta % gamma delta should be ignored\nepsilon\n", tmp_path)
    assert m.words_before_references(tex) == 3


def test_tex_cuts_at_references_section(tmp_path):
    tex = _tex("body one two\n\\section*{References}\nignored ignored ignored\n", tmp_path)
    assert m.words_before_references(tex) == 3


def test_tex_has_no_page_count(tmp_path):
    tex = _tex("body text\n", tmp_path)
    assert m.pages_before_references(tex) is None


# --- DOCX ------------------------------------------------------------------


def test_docx_counts_words_before_references(tmp_path):
    path = tmp_path / "ms.docx"
    _build_docx(
        path,
        [
            "Title heading here",
            "one two three four",
            "References",
            "Smith ignored ignored ignored ignored",
        ],
    )
    # "Title heading here" (3) + "one two three four" (4) = 7.
    assert m.words_before_references(path) == 7


def test_docx_without_references_counts_all(tmp_path):
    path = tmp_path / "ms.docx"
    _build_docx(path, ["alpha beta", "gamma delta"])
    assert m.words_before_references(path) == 4


def test_docx_has_no_page_count(tmp_path):
    path = tmp_path / "ms.docx"
    _build_docx(path, ["body"])
    assert m.pages_before_references(path) is None


# --- PDF -------------------------------------------------------------------


def test_pdf_words_before_references(tmp_path):
    path = tmp_path / "ms.pdf"
    path.write_bytes(
        _build_pdf(
            pages=1,
            title="Body",
            body_lines=["alpha beta gamma", "References", "ignored ignored ignored"],
        )
    )
    text = m.manuscript_text(path)
    assert text is not None and "alpha beta gamma" in text
    # Everything from the "References" line on is dropped.
    assert "ignored" not in m.truncate_at_references(text)


def test_pdf_page_count_before_references(tmp_path):
    path = tmp_path / "ms.pdf"
    # References heading lands on the second page.
    body = ["main text only here"]
    pdf = _build_pdf(pages=3, title="Doc", body_lines=body)
    path.write_bytes(pdf)
    # No references anywhere -> full page count.
    assert m.pages_before_references(path) == 3


def test_pdf_page_count_stops_at_references(tmp_path):
    path = tmp_path / "ms.pdf"
    path.write_bytes(_build_pdf(pages=2, title="Doc", body_lines=["alpha", "References"]))
    # "References" appears on page 1, so one page precedes/contains the refs.
    assert m.pages_before_references(path) == 1


def test_non_pdf_returns_none_pages_even_if_supported_for_words(tmp_path):
    tex = _tex("body\n", tmp_path)
    assert m.words_before_references(tex) is not None
    assert m.pages_before_references(tex) is None


# --- ZIP LaTeX bundle ------------------------------------------------------


def test_zip_latex_bundle_word_count(tmp_path):
    path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("main.tex", "alpha beta gamma\n\\bibliography{ref}\nignored ignored\n")
        zf.writestr("ref.bib", "@article{x, title={ignored ignored ignored}}")
    assert m.words_before_references(path) == 3


def test_zip_without_tex_is_unsupported(tmp_path):
    path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("data.csv", "a,b,c\n")
    assert m.words_before_references(path) is None


# --- whole-document measures -----------------------------------------------


def test_total_words_includes_references(tmp_path):
    tex = _tex("alpha beta gamma\n\\bibliography{ref}\ndelta\n", tmp_path)
    # words_before_references stops at the bibliography; total_words does not.
    assert m.words_before_references(tex) == 3
    assert m.total_words(tex) == 3  # the bibliography marker still cuts source


def test_total_words_counts_full_pdf(tmp_path):
    path = tmp_path / "ms.pdf"
    path.write_bytes(_build_pdf(pages=1, title="Doc", body_lines=["alpha beta", "References", "gamma delta"]))
    before = m.words_before_references(path)
    total = m.total_words(path)
    assert total is not None and before is not None
    # The post-references words count toward the total but not the before-refs count.
    assert total > before


def test_total_pages_counts_full_pdf(tmp_path):
    path = tmp_path / "ms.pdf"
    path.write_bytes(_build_pdf(pages=3, title="Doc", body_lines=["alpha", "References"]))
    # References land on page 1, but the whole document is 3 pages.
    assert m.pages_before_references(path) == 1
    assert m.total_pages(path) == 3


def test_total_pages_uses_docx_cached_count(tmp_path):
    from tests.conftest import _CONTENT_TYPES, _RELS

    path = tmp_path / "ms.docx"
    app_xml = '<?xml version="1.0"?><Properties><Pages>5</Pages></Properties>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", "<w:document/>")
        z.writestr("docProps/app.xml", app_xml)
    assert m.total_pages(path) == 5


def test_total_pages_docx_without_cached_count_is_none(tmp_path):
    # _build_docx writes no docProps/app.xml, so there is no cached page count.
    path = tmp_path / "ms.docx"
    _build_docx(path, ["body"])
    assert m.total_pages(path) is None


def test_total_pages_unsupported_format_is_none(tmp_path):
    tex = _tex("body\n", tmp_path)
    assert m.total_pages(tex) is None


# --- unsupported formats ---------------------------------------------------


def test_unsupported_extension_returns_none(tmp_path):
    path = tmp_path / "ms.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0 legacy word binary")
    assert m.manuscript_text(path) is None
    assert m.words_before_references(path) is None
    assert m.pages_before_references(path) is None
    assert m.total_words(path) is None
    assert m.total_pages(path) is None


# ===========================================================================
# conftest sample-file builders
# ===========================================================================
#
# Sanity checks that the conftest sample-file builders produce valid files, so a
# builder regression surfaces here rather than as a confusing error downstream.


def test_manuscript_pdf_passes_pdf_checks(manuscript_pdf):
    data = manuscript_pdf.read_bytes()
    assert data.startswith(b"%PDF-")
    assert len(data) > 1024
    assert b"/Type /Page" in data

    # The validator's own PDF sanity check should report no issues.
    field = Field(id="manuscript_file", label="Manuscript", type="file", accept=[".pdf"])
    issues = _check_file_field(field, str(manuscript_pdf))
    assert issues == []


@pytest.mark.parametrize("ext", [".pdf", ".docx"])
def test_documents_build_for_each_extension(samples, ext):
    ms = samples.manuscript(ext)
    supp = samples.supplement(ext)
    assert ms.exists() and supp.exists()
    assert ms.suffix == ext and supp.suffix == ext
    if ext == ".docx":
        # A .docx is a zip with the core OOXML parts.
        with zipfile.ZipFile(ms) as z:
            names = set(z.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= names


@pytest.mark.parametrize("ext", [".png", ".jpg", ".jpeg", ".tif", ".tiff", ".pdf", ".svg"])
def test_figures_build_for_each_extension(samples, ext):
    fig = samples.figure("fig1", ext)
    assert fig.exists()
    assert fig.stat().st_size > 0
    if ext == ".svg":
        assert fig.read_text().lstrip().startswith("<?xml")
    elif ext != ".pdf":
        # Raster formats open as images of the requested size.
        with Image.open(fig) as img:
            assert img.size == (1200, 900)


def test_named_figure_fixtures(fig1_png, fig2_png):
    assert fig1_png.name == "fig1.png"
    assert fig2_png.name == "fig2.png"
    for fig in (fig1_png, fig2_png):
        with Image.open(fig) as img:
            assert img.format == "PNG"


def test_latex_bundle_is_valid_zip_of_sources(samples):
    bundle = samples.latex_bundle("manuscript_tex_bundle")
    assert bundle.name == "manuscript_tex_bundle.zip"

    with zipfile.ZipFile(bundle) as z:
        assert z.testzip() is None  # every member's CRC checks out
        names = set(z.namelist())
        main_tex = z.read("main.tex").decode("utf-8")
    # The bundle carries the LaTeX-like source files plus the figures it cites.
    assert {"main.tex", "ref.bib", "paperpush.cls", "paperpush.bst", "main.bbl", "figures/fig1.png", "figures/fig2.png"} <= names
    # main.tex is internally consistent with the other bundled files.
    assert "\\documentclass{paperpush}" in main_tex
    assert "\\bibliographystyle{paperpush}" in main_tex
    assert "\\bibliography{ref}" in main_tex
    assert "{figures/fig1.png}" in main_tex


def test_latex_bundle_is_deterministic(samples, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    first = samples.latex_bundle("a").read_bytes()
    # Build a second bundle from a fresh factory of the same type (avoid the
    # ambiguous 'import conftest' -- several conftest.py modules share that name).
    second = type(samples)(other).latex_bundle("b").read_bytes()
    # Same inputs -> byte-identical archive (fixed timestamps, sorted members).
    assert first == second
