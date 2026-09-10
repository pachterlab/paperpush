"""Unit tests for the manuscript requirements database and its checks.

Covers three layers:

1. :mod:`paperpush.requirements` -- loading ``manuscript_requirements.json``,
   ``inherits`` expansion, and ``article_types`` resolution against a filled
   ``.sub``.
2. :mod:`paperpush.requirements_check` -- each rule applied to hand-built
   venues and sample files (formats, sizes, length, sections, statements,
   title page, figures, references, .sub text fields), and the "do not
   double-report a limit the field already enforces" contract.
3. The shipped database -- every supported venue has an entry, the file
   conforms to its generated schema, and the schema is up to date.

The checks run against an injected in-memory database (see ``_use``), so each
test pins one rule without depending on what the shipped file says about a
real venue.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from paperpush import manuscript as m
from paperpush import requirements as r
from paperpush.database import Field, Venue, list_venues
from paperpush.requirements_check import check_manuscript_requirements
from paperpush.schema_models import build_requirements_schema
from paperpush.validate import ERROR, WARNING, validate
from paperpush.subfile import SubFile
from tests.conftest import _build_docx, _build_pdf

REPO_ROOT = Path(__file__).resolve().parent.parent
REQ_SCHEMA_PATH = REPO_ROOT / "paperpush" / "manuscript_requirements.schema.json"

_ALIASES = {
    "sections": {"Introduction": ["Background"], "Results": [], "Discussion": [], "Methods": ["Materials and Methods", "Materials & Methods", "Online Methods", "STAR Methods"]},
    "statements": {"data_availability": ["Data availability", "Availability of data and materials"], "competing_interests": ["Competing interests", "Conflict of interest", "Declaration of interests"], "author_contributions": ["Author contributions", "Authors' contributions", "CRediT authorship contribution statement"]},
}


@pytest.fixture
def _use(monkeypatch):
    """Inject an in-memory requirements database for the duration of a test."""

    def install(db: dict) -> None:
        data = {"$aliases": _ALIASES, **db}
        monkeypatch.setattr(r, "_load_raw", lambda: data)

    yield install


def _venue(*fields: Field, slug: str = "testj", max_upload_mb=None) -> Venue:
    return Venue(slug=slug, name="Test Venue", fields=list(fields), max_upload_mb=max_upload_mb)


MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
FIGURES = Field(id="figure_files", label="Figures", type="filelist")
SUPP = Field(id="supplementary_file", label="Supplement", type="file")
ABSTRACT = Field(id="abstract", label="Abstract", type="textarea")
TITLE = Field(id="title", label="Title", type="text")
KEYWORDS = Field(id="keywords", label="Keywords", type="textarea")
ARTICLE_TYPE = Field(id="article_type", label="Article type", type="choice", options=["Research", "Note"])


def _check(venue: Venue, values: dict[str, str]):
    return check_manuscript_requirements(venue, values)


def _messages(issues, level=None):
    return [i.message for i in issues if level is None or i.level == level]


# ===========================================================================
# loading and resolution
# ===========================================================================


def test_missing_venue_has_no_requirements(_use):
    _use({"other": {"manuscript": {"max_words": 10}}})
    assert r.get_requirements("testj") is None
    assert _check(_venue(MANUSCRIPT), {}) == []


def test_entry_sections_are_loaded_and_unknown_keys_ignored(_use):
    _use({"testj": {"retrieved": "2026-09-10", "source_urls": ["https://x"], "manuscript": {"max_words": 10, "bogus": 1, "notes": ["a"]}, "figures": {"min_dpi": 300}}})
    reqs = r.get_requirements("TESTJ")
    assert reqs.slug == "testj"
    assert reqs.manuscript.max_words == 10
    assert reqs.manuscript.notes == ["a"]
    assert reqs.figures.min_dpi == 300
    assert reqs.abstract.max_words is None
    assert reqs.to_dict()["manuscript"] == {"max_words": 10, "notes": ["a"]}
    assert "abstract" not in reqs.to_dict()


def test_inherits_merges_section_by_section(_use):
    _use(
        {
            "base": {"manuscript": {"max_words": 100, "formats": [".pdf"], "notes": ["b1"]}, "figures": {"min_dpi": 300, "max_count": 8}, "notes": ["v1"]},
            "child": {"inherits": "base", "manuscript": {"max_words": 50, "notes": ["c1"]}, "figures": {"max_count": None}, "abstract": {"max_words": 150}},
        }
    )
    child = r.get_requirements("child")
    assert child.inherits == "base"
    assert child.manuscript.max_words == 50
    assert child.manuscript.formats == [".pdf"]  # inherited, untouched
    assert child.manuscript.notes == ["b1", "c1"]  # notes accumulate
    assert child.figures.min_dpi == 300
    assert child.figures.max_count is None  # null drops the inherited value
    assert child.abstract.max_words == 150
    assert child.notes == ["v1"]


def test_inherits_unknown_base_is_an_error(_use):
    _use({"child": {"inherits": "nope"}})
    with pytest.raises(KeyError):
        r.get_requirements("child")


def test_article_type_override_resolves_from_sub_value(_use):
    _use({"testj": {"article_type_field": "article_type", "manuscript": {"max_pages": 20}, "figures": {"max_count": 8}, "article_types": {"Note": {"manuscript": {"max_pages": 6}, "figures": {"max_count": 2}}}}})
    base = r.get_requirements("testj")
    assert r.resolve(base, {"article_type": "Research"}).manuscript.max_pages == 20
    note = r.resolve(base, {"article_type": "note"})  # case-insensitive
    assert note.manuscript.max_pages == 6
    assert note.figures.max_count == 2
    assert note.article_types == {}
    assert r.resolve(base, {}).manuscript.max_pages == 20


def test_heading_aliases_include_canonical_name(_use):
    _use({})
    aliases = r.heading_aliases("sections")
    assert "methods" in aliases["Methods"]
    assert "materials and methods" in aliases["Methods"]
    assert r.heading_aliases("nope") == {}


# ===========================================================================
# checks: formats, sizes, counts
# ===========================================================================


def test_manuscript_format_warns_only_when_field_has_no_accept(_use, tmp_path):
    _use({"testj": {"manuscript": {"formats": [".pdf"]}}})
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["hello"])
    issues = _check(_venue(MANUSCRIPT), {"manuscript_file": str(doc)})
    assert any("guidelines accept .pdf" in msg for msg in _messages(issues, WARNING))
    # The field's own accept list is enforced elsewhere; not repeated here.
    strict = Field(id="manuscript_file", label="Manuscript", type="file", accept=[".pdf"])
    assert _check(_venue(strict), {"manuscript_file": str(doc)}) == []


def test_file_size_cap_is_an_error_unless_field_has_its_own(_use, tmp_path):
    _use({"testj": {"figures": {"max_file_size_mb": 0.001}}})
    fig = tmp_path / "fig.png"
    fig.write_bytes(b"\x89PNG" + b"0" * 5000)
    issues = _check(_venue(FIGURES), {"figure_files": str(fig)})
    assert any(i.level == ERROR and "MB" in i.message for i in issues)
    own = Field(id="figure_files", label="Figures", type="filelist", max_file_size_mb=10)
    assert not [i for i in _check(_venue(own), {"figure_files": str(fig)}) if "MB" in i.message]


def test_figure_count_cap(_use, tmp_path):
    _use({"testj": {"figures": {"max_count": 1}}})
    figs = "\n".join(str(tmp_path / f"f{i}.png") for i in range(3))
    issues = _check(_venue(FIGURES), {"figure_files": figs})
    assert any("at most 1" in msg for msg in _messages(issues, ERROR))


def test_display_item_cap_counts_figures_and_tables(_use, tmp_path):
    tables = Field(id="table_files", label="Tables", type="filelist")
    _use({"testj": {"manuscript": {"max_display_items": 2}}})
    values = {"figure_files": "a.png\nb.png", "table_files": "t.docx"}
    issues = _check(_venue(FIGURES, tables), values)
    assert any("display items" in msg for msg in _messages(issues, ERROR))


def test_total_upload_cap_only_when_venue_declares_none(_use, tmp_path):
    _use({"testj": {"upload": {"max_total_mb": 0.001}}})
    big = tmp_path / "ms.pdf"
    big.write_bytes(_build_pdf(pages=1) + b"0" * 5000)
    issues = _check(_venue(MANUSCRIPT), {"manuscript_file": str(big)})
    assert any("total upload size" in msg for msg in _messages(issues, ERROR))
    assert not [i for i in _check(_venue(MANUSCRIPT, max_upload_mb=50), {"manuscript_file": str(big)}) if "total upload" in i.message]


def test_supplementary_single_pdf_rule(_use, tmp_path):
    _use({"testj": {"supplementary": {"combined_single_pdf": True}}})
    doc = tmp_path / "supp.docx"
    _build_docx(doc, ["x"])
    issues = _check(_venue(SUPP), {"supplementary_file": str(doc)})
    assert any("single PDF" in msg for msg in _messages(issues, WARNING))


def test_cover_letter_recommended_when_empty(_use):
    _use({"testj": {"cover_letter": {"required": True}}})
    cover = Field(id="cover_letter", label="Cover letter", type="file")
    issues = _check(_venue(cover), {})
    assert any("cover letter" in msg for msg in _messages(issues, WARNING))


# ===========================================================================
# checks: length
# ===========================================================================


def test_word_limit_from_guidelines_when_field_has_none(_use, tmp_path):
    _use({"testj": {"manuscript": {"max_words": 5}}})
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["one two three four five six seven"])
    issues = _check(_venue(MANUSCRIPT), {"manuscript_file": str(doc)})
    assert any("exceeds the 5-word limit" in msg for msg in _messages(issues, ERROR))
    own = Field(id="manuscript_file", label="Manuscript", type="file", max_words=1000)
    assert _check(_venue(own), {"manuscript_file": str(doc)}) == []


def test_page_limit_counts_pdf_pages(_use, tmp_path):
    _use({"testj": {"manuscript": {"max_pages": 2}}})
    pdf = tmp_path / "ms.pdf"
    pdf.write_bytes(_build_pdf(pages=3))
    issues = _check(_venue(MANUSCRIPT), {"manuscript_file": str(pdf)})
    assert any("3 pages exceeds the 2-page limit" in msg for msg in _messages(issues, ERROR))


def test_page_limit_on_tex_warns_without_toolchain(_use, tmp_path, monkeypatch):
    _use({"testj": {"manuscript": {"max_pages": 2}}})
    monkeypatch.setattr(m, "tex_toolchain", lambda: None)
    tex = tmp_path / "ms.tex"
    tex.write_text("\\documentclass{article}\\begin{document}x\\end{document}", encoding="utf-8")
    issues = _check(_venue(MANUSCRIPT), {"manuscript_file": str(tex)})
    assert any(i.level == WARNING and "latexmk" in i.message for i in issues)


@pytest.mark.skipif(m.tex_toolchain() is None, reason="needs latexmk or pdflatex")
def test_page_limit_on_tex_renders_the_source(_use, tmp_path):
    _use({"testj": {"manuscript": {"max_pages": 1}}})
    tex = tmp_path / "ms.tex"
    tex.write_text("\\documentclass{article}\n\\begin{document}\nA\\newpage B\\newpage C\n\\end{document}\n", encoding="utf-8")
    issues = _check(_venue(MANUSCRIPT), {"manuscript_file": str(tex)})
    assert any("3 pages (rendered from the LaTeX source) exceeds the 1-page limit" in msg for msg in _messages(issues, ERROR))


# ===========================================================================
# checks: structure (sections, statements, title page, references)
# ===========================================================================


def _pdf_with(tmp_path: Path, lines: list[str], name: str = "ms.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(_build_pdf(pages=1, body_lines=lines))
    return path


def test_required_sections_matched_via_aliases(_use, tmp_path):
    _use({"testj": {"sections": {"required": ["Introduction", "Methods", "Results"]}}})
    pdf = _pdf_with(tmp_path, ["Background", "text", "2. Materials and Methods", "more"])
    issues = _check(_venue(MANUSCRIPT), {"manuscript_file": str(pdf)})
    messages = _messages(issues, WARNING)
    assert not any("'Introduction'" in msg for msg in messages)
    assert not any("'Methods'" in msg for msg in messages)
    assert any("'Results'" in msg for msg in messages)


def test_combined_heading_satisfies_both_sections(_use, tmp_path):
    _use({"testj": {"sections": {"required": ["Results", "Discussion"], "combined_allowed": ["Results and Discussion"]}}})
    pdf = _pdf_with(tmp_path, ["Results and Discussion", "text"])
    assert _check(_venue(MANUSCRIPT), {"manuscript_file": str(pdf)}) == []


def test_required_statements_found_as_run_in_headings(_use, tmp_path):
    _use({"testj": {"statements": {"required": ["data_availability", "competing_interests", "author_contributions"]}}})
    pdf = _pdf_with(tmp_path, ["Data availability. All data are deposited.", "Conflict of interest", "none"])
    issues = _check(_venue(MANUSCRIPT), {"manuscript_file": str(pdf)})
    messages = _messages(issues, WARNING)
    assert any("author contributions statement" in msg for msg in messages)
    assert not any("data availability" in msg for msg in messages)
    assert not any("competing interests" in msg for msg in messages)


def test_sections_in_latex_source(_use, tmp_path):
    _use({"testj": {"sections": {"required": ["Methods", "Discussion"]}, "statements": {"required": ["data_availability"]}}})
    tex = tmp_path / "ms.tex"
    tex.write_text("\\documentclass{article}\\begin{document}\n\\section{Online Methods}\n\\paragraph{Data availability} x\n\\end{document}", encoding="utf-8")
    issues = _check(_venue(MANUSCRIPT), {"manuscript_file": str(tex)})
    messages = _messages(issues, WARNING)
    assert messages == [msg for msg in messages if "'Discussion'" in msg], messages


def test_title_page_items(_use, tmp_path):
    _use({"testj": {"title_page": {"required_items": ["title", "corresponding_email", "orcid", "keywords", "authors"]}}})
    pdf = _pdf_with(tmp_path, ["A Study of Things", "Ada Lovelace", "ada@example.edu", "Keywords: a, b"])
    issues = _check(_venue(MANUSCRIPT, TITLE), {"manuscript_file": str(pdf), "title": "A study of things"})
    messages = _messages(issues, WARNING)
    assert any("ORCID" in msg for msg in messages)
    assert not any("e-mail" in msg or "title" in msg.lower() and "not found" in msg for msg in messages)
    assert not any("keywords line" in msg for msg in messages)
    # A title that differs from the .sub is flagged.
    issues = _check(_venue(MANUSCRIPT, TITLE), {"manuscript_file": str(pdf), "title": "Something else"})
    assert any("title in the .sub file was not found" in msg for msg in _messages(issues, WARNING))


def test_reference_count_limit(_use, tmp_path):
    _use({"testj": {"references": {"max_count": 2}}})
    pdf = _pdf_with(tmp_path, ["Body", "References", "1. A. Author, Title, 2020.", "2. B. Author, Title, 2021.", "3. C. Author, Title, 2022."])
    issues = _check(_venue(MANUSCRIPT), {"manuscript_file": str(pdf)})
    assert any("3 references" in msg and "2-reference limit" in msg for msg in _messages(issues, ERROR))
    no_refs = _pdf_with(tmp_path, ["Body only"], name="norefs.pdf")
    issues = _check(_venue(MANUSCRIPT), {"manuscript_file": str(no_refs)})
    assert any("could not count the references" in msg for msg in _messages(issues, WARNING))


def test_reference_count_from_bibitems_and_bib(tmp_path):
    tex = tmp_path / "a.tex"
    tex.write_text("\\begin{thebibliography}{9}\\bibitem{a} x \\bibitem{b} y\\end{thebibliography}", encoding="utf-8")
    assert m.reference_count(tex) == 2
    tex2 = tmp_path / "b.tex"
    tex2.write_text("\\bibliography{refs}", encoding="utf-8")
    (tmp_path / "refs.bib").write_text("@article{a, title={x}}\n@string{s = 1}\n@book{b, title={y}}\n@misc{c, title={z}}\n", encoding="utf-8")
    assert m.reference_count(tex2) == 3


# ===========================================================================
# checks: .sub text fields
# ===========================================================================


def test_abstract_limits_and_structure(_use):
    _use({"testj": {"abstract": {"max_words": 3, "structured": True, "structured_headings": ["Background", "Results"], "no_references": True}}})
    issues = _check(_venue(ABSTRACT), {"abstract": "Background: one two three four [1]"})
    assert any("exceeds the 3-word limit" in msg for msg in _messages(issues, ERROR))
    assert any("missing: Results" in msg for msg in _messages(issues, WARNING))
    assert any("cites references" in msg for msg in _messages(issues, WARNING))
    # A field that already caps words is not double-reported.
    capped = Field(id="abstract", label="Abstract", type="textarea", word_count=250)
    assert not [i for i in _check(_venue(capped), {"abstract": "one two three four"}) if i.level == ERROR]


def test_title_and_keyword_limits(_use):
    _use({"testj": {"title_page": {"title_max_characters": 10}, "keywords": {"min": 2, "max": 3}}})
    issues = _check(_venue(TITLE, KEYWORDS), {"title": "A very long title indeed", "keywords": "one"})
    assert any("exceeds the 10-character limit" in msg for msg in _messages(issues, ERROR))
    assert any("at least 2" in msg for msg in _messages(issues, ERROR))
    issues = _check(_venue(TITLE, KEYWORDS), {"title": "Short", "keywords": "a, b, c, d"})
    assert any("at most 3" in msg for msg in _messages(issues, ERROR))


# ===========================================================================
# checks: figures
# ===========================================================================


def test_figure_dpi_from_metadata_and_from_pixel_width(_use, samples):
    _use({"testj": {"figures": {"min_dpi": 300, "max_width_mm": 180}}})
    low = samples.figure("low", ".png", size=(600, 400), dpi=72)
    issues = _check(_venue(FIGURES), {"figure_files": str(low)})
    assert any("72 dpi" in msg for msg in _messages(issues, WARNING))
    good = samples.figure("good", ".png", size=(2000, 1500), dpi=300)  # 169 mm wide
    assert _check(_venue(FIGURES), {"figure_files": str(good)}) == []
    # No DPI metadata: judged by the pixel width at the venue's print width.
    from PIL import Image

    bare = samples.root / "bare.png"
    Image.new("RGB", (800, 600)).save(bare)
    issues = _check(_venue(FIGURES), {"figure_files": str(bare)})
    assert any("800 px wide" in msg and "113 dpi" in msg for msg in _messages(issues, WARNING))


def test_figure_too_wide_at_its_dpi(_use, samples):
    _use({"testj": {"figures": {"min_dpi": 300, "max_width_mm": 100}}})
    wide = samples.figure("wide", ".png", size=(3000, 600), dpi=300)  # 254 mm
    issues = _check(_venue(FIGURES), {"figure_files": str(wide)})
    assert any("254 mm wide" in msg for msg in _messages(issues, WARNING))


def test_figure_color_mode(_use, samples, tmp_path):
    from PIL import Image

    _use({"testj": {"figures": {"color_mode": ["RGB"]}}})
    cmyk = tmp_path / "cmyk.jpg"
    Image.new("CMYK", (100, 100)).save(cmyk)
    issues = _check(_venue(FIGURES), {"figure_files": str(cmyk)})
    assert any("cmyk" in msg for msg in _messages(issues, WARNING))
    gray = tmp_path / "gray.png"
    Image.new("L", (100, 100)).save(gray)
    assert not [i for i in _check(_venue(FIGURES), {"figure_files": str(gray)}) if "grayscale" in i.message]


# ===========================================================================
# integration with validate()
# ===========================================================================


def test_validate_runs_and_can_skip_the_manuscript_check(_use, tmp_path):
    _use({"testj": {"manuscript": {"max_words": 1}}})
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["one two three"])
    sub = SubFile(venue="testj", values={"manuscript_file": str(doc)})
    venue = _venue(MANUSCRIPT)
    on = validate(sub, venue, check_sensitive=False, check_links=False, check_references=False)
    assert any("1-word limit in the author guidelines" in i.message for i in on)
    off = validate(sub, venue, check_sensitive=False, check_links=False, check_references=False, check_manuscript=False)
    assert not any("author guidelines" in i.message for i in off)


def test_validate_cli_flag(_use, tmp_path, monkeypatch, capsys):
    from paperpush.cli import main

    _use({"biorxiv": {"manuscript": {"max_words": 1}}})
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["one two three"])
    sub = tmp_path / "biorxiv.sub"
    sub.write_text(f"@venue: biorxiv\nmanuscript_file: {doc}\n", encoding="utf-8")
    assert main(["validate", str(sub), "--dont-check-links", "--dont-check-references", "--dont-check-for-sensitive-info"]) == 1
    assert "1-word limit" in capsys.readouterr().err
    main(["validate", str(sub), "--dont-check-links", "--dont-check-references", "--dont-check-for-sensitive-info", "--dont-check-manuscript"])
    assert "1-word limit" not in capsys.readouterr().err


def test_requirements_command(_use, capsys):
    from paperpush.cli import main

    _use({"biorxiv": {"retrieved": "2026-09-10", "manuscript": {"formats": [".pdf"], "notes": ["No typesetting."]}, "article_type_field": "article_type", "article_types": {"Note": {"manuscript": {"max_pages": 6}}}}})
    assert main(["requirements", "biorxiv"]) == 0
    out = capsys.readouterr().out
    assert "formats: .pdf" in out and "No typesetting." in out and "Note" in out
    assert main(["requirements", "biorxiv", "--json", "--article-type", "Note"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["manuscript"]["max_pages"] == 6
    assert main(["requirements", "nope"]) == 2
    _use({})
    assert main(["requirements", "biorxiv"]) == 1


def test_mcp_describe_venue_carries_requirements(_use):
    from paperpush.mcp_server import describe_venue, validate_subfile  # noqa: F401 -- import guards the signature

    _use({"biorxiv": {"figures": {"min_dpi": 300}}})
    assert describe_venue("biorxiv")["manuscript_requirements"]["figures"] == {"min_dpi": 300}


# ===========================================================================
# the shipped database
# ===========================================================================


def _shipped() -> dict:
    return json.loads(r.REQUIREMENTS_PATH.read_text(encoding="utf-8"))


def test_requirements_schema_is_up_to_date():
    expected = json.dumps(build_requirements_schema(), indent=2, ensure_ascii=False) + "\n"
    assert REQ_SCHEMA_PATH.read_text(encoding="utf-8") == expected, "manuscript_requirements.schema.json is out of date; run `python scripts/gen_venues_schema.py`"


def test_shipped_database_validates_against_schema():
    validator = Draft202012Validator(json.loads(REQ_SCHEMA_PATH.read_text(encoding="utf-8")))
    errors = sorted(validator.iter_errors(_shipped()), key=lambda e: list(e.path))
    assert not errors, "manuscript_requirements.json violates its schema:\n" + "\n".join(f"  {'/'.join(map(str, e.path))}: {e.message}" for e in errors)


def test_schema_rejects_unknown_section_key():
    validator = Draft202012Validator(json.loads(REQ_SCHEMA_PATH.read_text(encoding="utf-8")))
    assert list(validator.iter_errors({"j": {"figures": {"min_dpis": 300}}}))
    assert list(validator.iter_errors({"j": {"figurez": {}}}))
    assert not list(validator.iter_errors({"j": {"inherits": "k", "figures": {"min_dpi": None}, "article_types": {"Note": {"manuscript": {"max_pages": 6}}}}}))


def test_every_supported_venue_has_requirements():
    shipped = _shipped()
    missing = [v.slug for v in list_venues() if v.slug not in shipped]
    assert not missing, f"venues without manuscript requirements: {missing}"


def test_shipped_entries_load_and_resolve():
    for reqs in r.list_requirements():
        assert reqs.retrieved, f"{reqs.slug}: missing retrieved date"
        assert reqs.source_urls, f"{reqs.slug}: missing source_urls"
        for name in reqs.article_types:
            resolved = r.resolve(reqs, {reqs.article_type_field: name})
            assert resolved.article_types == {}
        if reqs.article_types:
            assert reqs.article_type_field, f"{reqs.slug}: article_types without article_type_field"


def test_article_type_keys_match_the_venue_form():
    """Each ``article_types`` key must be an option of the field it is selected by."""
    from paperpush.database import get_venue

    for reqs in r.list_requirements():
        if not reqs.article_types:
            continue
        venue = get_venue(reqs.slug)
        field = next((f for f in venue.fields if f.id == reqs.article_type_field), None)
        assert field is not None, f"{reqs.slug}: article_type_field {reqs.article_type_field!r} is not a field"
        options = {o.lower() for o in (field.options or [])}
        unknown = [k for k in reqs.article_types if k.lower() not in options]
        assert not unknown, f"{reqs.slug}: article_types not in {field.id} options: {unknown}"


def test_alias_tables_cover_the_shipped_canonical_names():
    shipped = _shipped()
    aliases = shipped["$aliases"]
    for slug, entry in shipped.items():
        if slug.startswith("$"):
            continue
        for name in (entry.get("sections") or {}).get("required") or []:
            assert name in aliases["sections"], f"{slug}: section {name!r} has no $aliases entry"
        for name in (entry.get("statements") or {}).get("required") or []:
            assert name in aliases["statements"], f"{slug}: statement {name!r} has no $aliases entry"
