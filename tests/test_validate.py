"""Unit tests for :mod:`paperpush.validate`.

These exercise the pre-submission checks directly against hand-built
:class:`~paperpush.database.Venue` / :class:`~paperpush.database.Field`
definitions and :class:`~paperpush.subfile.SubFile` values, so each rule is
tested in isolation without depending on the shipped ``venues.json``. A few
tests at the end cover the ``paperpush validate`` CLI command end to end.

File-existence and PDF checks use the ``samples`` factory from ``conftest.py``
to write genuinely valid files into ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import paperpush.validate as validate_mod
import sample_subfiles
from paperpush import subfile
from paperpush.cli import main
from paperpush.database import Field, Venue, get_venue
from paperpush.subfile import SubFile
from paperpush.validate import (
    ERROR,
    WARNING,
    Issue,
    parse_authors,
    validate,
)
from sample_subfiles import REPO_ROOT, scenarios


@pytest.fixture(autouse=True)
def _clear_model_cache():
    """Drop the per-slug pydantic model cache so each test's hand-built venue
    is validated against its own fields rather than a model another test cached
    under the same slug."""
    validate_mod._MODELS.clear()
    yield
    validate_mod._MODELS.clear()


# --- builders --------------------------------------------------------------


def _venue(*fields: Field, slug: str = "testj") -> Venue:
    """A minimal venue carrying just the given fields."""
    return Venue(slug=slug, name="Test Venue", fields=list(fields))


def _sub(values: dict[str, str], venue: str = "testj") -> SubFile:
    return SubFile(venue=venue, values=values)


def _validate(venue: Venue, values: dict[str, str]) -> list[Issue]:
    return validate(_sub(values), venue)


def _errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.is_error]


def _warnings(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if not i.is_error]


def _for(issues: list[Issue], field_id: str) -> list[Issue]:
    return [i for i in issues if i.field == field_id]


# --- Issue -----------------------------------------------------------------


def test_issue_is_error_property():
    assert Issue(ERROR, "f", "m").is_error is True
    assert Issue(WARNING, "f", "m").is_error is False


# --- parse_authors ---------------------------------------------------------


def test_parse_authors_default_columns():
    authors = parse_authors("Ada Lovelace | ada@example.edu | Example U | 0000-0002-1825-0097 | yes")
    assert len(authors) == 1
    a = authors[0]
    assert a["name"] == "Ada Lovelace"
    assert a["email"] == "ada@example.edu"
    assert a["affiliation"] == "Example U"
    assert a["orcid"] == "0000-0002-1825-0097"
    assert a["corresponding"] is True


def test_parse_authors_skips_blank_lines_and_trims():
    block = "\n  Ada Lovelace | ada@example.edu | U | | yes  \n\n   \n"
    authors = parse_authors(block)
    assert len(authors) == 1
    assert authors[0]["name"] == "Ada Lovelace"
    assert authors[0]["orcid"] == ""


def test_parse_authors_tolerates_missing_trailing_columns():
    # Only a name is given; the remaining columns default to empty / False.
    authors = parse_authors("Alan Turing")
    assert authors[0]["name"] == "Alan Turing"
    assert authors[0]["email"] == ""
    assert authors[0]["corresponding"] is False


@pytest.mark.parametrize(
    "token,expected",
    [
        ("yes", True),
        ("y", True),
        ("true", True),
        ("1", True),
        ("no", False),
        ("", False),
        ("maybe", False),
    ],
)
def test_parse_authors_corresponding_coercion(token, expected):
    authors = parse_authors(f"Name | e@x.org | U | | {token}")
    assert authors[0]["corresponding"] is expected


def test_parse_authors_custom_field_order():
    # Bioinformatics-style column set.
    fields = [
        "email",
        "prefix",
        "name",
        "institution",
        "country",
        "city",
        "corresponding",
    ]
    authors = parse_authors("ada@x.org | Dr | Ada | Example U | UK | London | yes", fields)
    assert authors[0]["email"] == "ada@x.org"
    assert authors[0]["prefix"] == "Dr"
    assert authors[0]["name"] == "Ada"
    assert authors[0]["country"] == "UK"
    assert authors[0]["corresponding"] is True


# --- required fields -------------------------------------------------------


def test_required_text_field_empty_is_error():
    j = _venue(Field(id="title", label="Title", type="text", required=True))
    issues = _validate(j, {"title": ""})
    errs = _errors(issues)
    assert len(errs) == 1
    assert "required but empty" in errs[0].message
    assert errs[0].field == "title"


def test_required_field_whitespace_only_is_empty():
    j = _venue(Field(id="title", label="Title", type="text", required=True))
    issues = _validate(j, {"title": "   "})
    assert any("required but empty" in i.message for i in _errors(issues))


def test_required_field_present_passes():
    j = _venue(Field(id="title", label="Title", type="text", required=True))
    assert _errors(_validate(j, {"title": "A Manuscript"})) == []


def test_optional_empty_field_no_issue():
    j = _venue(Field(id="note", label="Note", type="text", required=False))
    assert _validate(j, {"note": ""}) == []


def test_required_boolean_confirm_empty_asks_to_confirm():
    j = _venue(Field(id="consent", label="Consent", type="boolean", required=True, confirm=True))
    errs = _errors(_validate(j, {"consent": ""}))
    assert len(errs) == 1
    assert "must be confirmed" in errs[0].message


def test_required_boolean_question_empty_asks_to_answer():
    j = _venue(
        Field(
            id="prior",
            label="Prior submission",
            type="boolean",
            required=True,
            confirm=False,
        )
    )
    errs = _errors(_validate(j, {"prior": ""}))
    assert len(errs) == 1
    assert "must be answered" in errs[0].message


def test_required_confirm_boolean_set_no_is_error():
    j = _venue(Field(id="consent", label="Consent", type="boolean", required=True, confirm=True))
    errs = _errors(_validate(j, {"consent": "no"}))
    assert any("must be confirmed" in i.message for i in errs)


def test_required_confirm_boolean_set_yes_passes():
    j = _venue(Field(id="consent", label="Consent", type="boolean", required=True, confirm=True))
    assert _errors(_validate(j, {"consent": "yes"})) == []


def test_informational_boolean_no_is_accepted():
    j = _venue(Field(id="prior", label="Prior", type="boolean", required=True, confirm=False))
    assert _errors(_validate(j, {"prior": "no"})) == []


# --- choice ----------------------------------------------------------------


def test_choice_invalid_option_is_error():
    j = _venue(
        Field(
            id="kind",
            label="Article type",
            type="choice",
            options=["Research", "Review"],
        )
    )
    errs = _errors(_validate(j, {"kind": "Editorial"}))
    assert len(errs) == 1
    assert "not a valid option" in errs[0].message
    # The valid options are spelled out for the author.
    assert "Research" in errs[0].message and "Review" in errs[0].message


def test_choice_valid_option_passes():
    j = _venue(
        Field(
            id="kind",
            label="Article type",
            type="choice",
            options=["Research", "Review"],
        )
    )
    assert _errors(_validate(j, {"kind": "Review"})) == []


# --- boolean parsing -------------------------------------------------------


def test_boolean_unparseable_is_error():
    j = _venue(Field(id="flag", label="Flag", type="boolean"))
    errs = _errors(_validate(j, {"flag": "perhaps"}))
    assert len(errs) == 1
    assert "yes/no" in errs[0].message


@pytest.mark.parametrize("value", ["yes", "no", "true", "FALSE", "1", "0", "on", "off"])
def test_boolean_accepts_known_tokens(value):
    j = _venue(Field(id="flag", label="Flag", type="boolean"))
    assert _errors(_validate(j, {"flag": value})) == []


# --- int -------------------------------------------------------------------


def test_int_non_integer_is_error():
    j = _venue(Field(id="pages", label="Pages", type="int"))
    errs = _errors(_validate(j, {"pages": "ten"}))
    assert any("not a whole number" in i.message for i in errs)


def test_int_below_minimum_is_error():
    j = _venue(Field(id="pages", label="Pages", type="int", min_value=1))
    errs = _errors(_validate(j, {"pages": "0"}))
    assert any("at least 1" in i.message for i in errs)


def test_int_above_maximum_is_error():
    j = _venue(Field(id="pages", label="Pages", type="int", max_value=10))
    errs = _errors(_validate(j, {"pages": "11"}))
    assert any("at most 10" in i.message for i in errs)


def test_int_within_bounds_passes():
    j = _venue(Field(id="pages", label="Pages", type="int", min_value=1, max_value=10))
    assert _errors(_validate(j, {"pages": "5"})) == []


# --- multichoice -----------------------------------------------------------


def test_multichoice_invalid_option_is_error():
    j = _venue(Field(id="kw", label="Keywords", type="multichoice", options=["a", "b", "c"]))
    errs = _errors(_validate(j, {"kw": "a, z"}))
    assert any("not a valid option" in i.message for i in errs)
    # Inline options are spelled out in the error.
    assert any("a | b | c" in i.message for i in errs)


def test_multichoice_file_backed_invalid_option_points_at_command():
    """A file-backed field points at the options command instead of dumping the list."""
    j = _venue(
        Field(
            id="cat",
            label="Category",
            type="multichoice",
            options=["a", "b", "c"],
            options_file="some_vocab.txt",
        ),
        slug="myvenue",
    )
    errs = _errors(_validate(j, {"cat": "z"}))
    assert len(errs) == 1
    assert "paperpush options myvenue.cat" in errs[0].message
    # The full option list is not inlined.
    assert "a | b | c" not in errs[0].message


def test_multichoice_below_min_count_is_error():
    j = _venue(
        Field(
            id="kw",
            label="Keywords",
            type="multichoice",
            options=["a", "b", "c"],
            min_count=3,
        )
    )
    errs = _errors(_validate(j, {"kw": "a, b"}))
    assert any("at least 3" in i.message for i in errs)


def test_multichoice_above_max_count_is_error():
    j = _venue(
        Field(
            id="kw",
            label="Keywords",
            type="multichoice",
            options=["a", "b", "c"],
            max_count=2,
        )
    )
    errs = _errors(_validate(j, {"kw": "a, b, c"}))
    assert any("at most 2" in i.message for i in errs)


def test_multichoice_within_bounds_passes():
    j = _venue(
        Field(
            id="kw",
            label="Keywords",
            type="multichoice",
            options=["a", "b", "c"],
            min_count=1,
            max_count=3,
        )
    )
    assert _errors(_validate(j, {"kw": "a, c"})) == []


# --- item count on line-based multi-item fields ----------------------------


def test_filelist_below_min_count_is_error(samples):
    one = str(samples.document("a", ".pdf"))
    j = _venue(Field(id="figs", label="Figures", type="filelist", min_count=2))
    errs = _errors(_validate(j, {"figs": one}))
    assert any("at least 2" in i.message and "got 1" in i.message for i in errs)


def test_textarea_reviewers_below_min_count_is_error():
    j = _venue(Field(id="reviewers", label="Reviewers", type="textarea", min_count=4))
    errs = _errors(_validate(j, {"reviewers": "Ann | a@x.org\nBeth | b@x.org"}))
    assert any("at least 4" in i.message and "got 2" in i.message for i in errs)


def test_filelist_above_max_count_is_error(samples):
    paths = "\n".join(str(samples.document(f"f{i}", ".pdf")) for i in range(3))
    j = _venue(Field(id="figs", label="Figures", type="filelist", max_count=2))
    errs = _errors(_validate(j, {"figs": paths}))
    assert any("at most 2" in i.message and "got 3" in i.message for i in errs)


def test_filelist_count_ignores_blank_lines(samples):
    paths = str(samples.document("a", ".pdf")) + "\n\n" + str(samples.document("b", ".pdf"))
    j = _venue(Field(id="figs", label="Figures", type="filelist", min_count=2, max_count=2))
    assert _errors(_validate(j, {"figs": paths})) == []


# --- text length limits ----------------------------------------------------


def test_word_count_over_limit_is_error():
    j = _venue(Field(id="abstract", label="Abstract", type="textarea", word_count=3))
    errs = _errors(_validate(j, {"abstract": "one two three four"}))
    assert any("4 words" in i.message and "3-word" in i.message for i in errs)


def test_word_count_at_limit_passes():
    j = _venue(Field(id="abstract", label="Abstract", type="textarea", word_count=3))
    assert _errors(_validate(j, {"abstract": "one two three"})) == []


def test_character_count_over_limit_is_error():
    j = _venue(Field(id="title", label="Title", type="text", character_count=5))
    errs = _errors(_validate(j, {"title": "abcdef"}))
    assert any("6 characters" in i.message and "5-character" in i.message for i in errs)


def test_character_count_at_limit_passes():
    j = _venue(Field(id="title", label="Title", type="text", character_count=5))
    assert _errors(_validate(j, {"title": "abcde"})) == []


# --- require_url -----------------------------------------------------------


def test_require_url_accepts_http_and_https():
    j = _venue(Field(id="data", label="Data", type="textarea", require_url=True))
    assert _errors(_validate(j, {"data": "https://github.com/me/repo"})) == []
    assert _errors(_validate(j, {"data": "http://example.org/x"})) == []


def test_require_url_accepts_one_url_per_line():
    j = _venue(Field(id="data", label="Data", type="textarea", require_url=True))
    assert _errors(_validate(j, {"data": "https://a.org\nhttps://b.org/x"})) == []


def test_require_url_rejects_prose_and_reports_line():
    j = _venue(Field(id="data", label="Data", type="textarea", require_url=True))
    errs = _errors(_validate(j, {"data": "Data available upon request"}))
    assert any("line 1" in i.message and "not a valid http/https URL" in i.message for i in errs)


def test_require_url_rejects_non_http_scheme():
    j = _venue(Field(id="data", label="Data", type="textarea", require_url=True))
    assert any("not a valid http/https URL" in i.message for i in _errors(_validate(j, {"data": "doi:10.1/x"})))


def test_require_url_blank_value_is_not_flagged():
    j = _venue(Field(id="data", label="Data", type="textarea", require_url=True, required=False))
    assert _errors(_validate(j, {"data": ""})) == []


# --- manuscript length before references -----------------------------------


def _pdf_manuscript(tmp_path, *, pages=1, title="Body", body_lines=None) -> Path:
    from tests.conftest import _build_pdf

    path = tmp_path / "manuscript.pdf"
    path.write_bytes(_build_pdf(pages=pages, title=title, body_lines=body_lines))
    return path


def test_manuscript_word_count_over_limit_is_error(tmp_path):
    ms = _pdf_manuscript(tmp_path, body_lines=["one two three four five"])
    j = _venue(Field(id="ms", label="Manuscript", type="file", max_words_before_refs=3))
    errs = _errors(_validate(j, {"ms": str(ms)}))
    assert any("words before references" in i.message and "3-word limit" in i.message for i in errs)


def test_manuscript_word_count_under_limit_passes(tmp_path):
    ms = _pdf_manuscript(tmp_path, body_lines=["short body"])
    j = _venue(Field(id="ms", label="Manuscript", type="file", max_words_before_refs=100000))
    assert _errors(_validate(j, {"ms": str(ms)})) == []


def test_manuscript_word_count_unsupported_format_warns(tmp_path):
    doc = tmp_path / "manuscript.doc"
    doc.write_bytes(b"\xd0\xcf\x11\xe0 legacy binary word file")
    j = _venue(Field(id="ms", label="Manuscript", type="file", max_words_before_refs=10))
    warns = _warnings(_validate(j, {"ms": str(doc)}))
    assert any("could not count words" in i.message for i in warns)


def test_manuscript_page_count_over_limit_is_error(tmp_path):
    ms = _pdf_manuscript(tmp_path, pages=3, title="Doc")
    j = _venue(Field(id="ms", label="Manuscript", type="file", max_pages_before_refs=2))
    errs = _errors(_validate(j, {"ms": str(ms)}))
    assert any("pages before references" in i.message and "2-page limit" in i.message for i in errs)


def test_manuscript_page_count_non_pdf_warns(tmp_path):
    from tests.conftest import _build_docx

    docx = tmp_path / "manuscript.docx"
    _build_docx(docx, ["body text"])
    j = _venue(Field(id="ms", label="Manuscript", type="file", max_pages_before_refs=5))
    warns = _warnings(_validate(j, {"ms": str(docx)}))
    assert any("page counts can only be verified for PDF" in i.message for i in warns)


def test_manuscript_total_page_count_over_limit_is_error(tmp_path):
    ms = _pdf_manuscript(tmp_path, pages=3, title="Doc")
    j = _venue(Field(id="ms", label="Manuscript", type="file", max_pages=2))
    errs = _errors(_validate(j, {"ms": str(ms)}))
    # Whole-document scope, so the message has no "before references" qualifier.
    assert any("3 pages exceeds the 2-page limit" in i.message for i in errs)


def test_manuscript_total_word_count_over_limit_is_error(tmp_path):
    ms = _pdf_manuscript(tmp_path, body_lines=["one two three four five"])
    j = _venue(Field(id="ms", label="Manuscript", type="file", max_words=3))
    errs = _errors(_validate(j, {"ms": str(ms)}))
    assert any("words exceeds the 3-word limit" in i.message and "before references" not in i.message for i in errs)


def test_manuscript_total_page_count_docx_unsupported_warns(tmp_path):
    from tests.conftest import _build_docx

    docx = tmp_path / "manuscript.docx"
    _build_docx(docx, ["body text"])  # no cached page count
    j = _venue(Field(id="ms", label="Manuscript", type="file", max_pages=5))
    warns = _warnings(_validate(j, {"ms": str(docx)}))
    assert any("Word (.docx) file with a saved page count" in i.message for i in warns)


def _bioinformatics_like(**kwargs) -> Venue:
    """A venue whose manuscript page cap varies with an article_type field."""
    from paperpush.database import ConditionalLimit

    by = ConditionalLimit(field="article_type", values={"Original Paper": 7, "Applications Note": 4})
    return _venue(
        Field(id="article_type", label="Type", type="choice"),
        Field(id="ms", label="Manuscript", type="file", max_pages_by=by, **kwargs),
    )


def test_conditional_page_limit_selected_by_article_type(tmp_path):
    ms = _pdf_manuscript(tmp_path, pages=5, title="Doc")
    j = _bioinformatics_like()
    # 5 pages is fine for an Original Paper (7) ...
    assert _errors(_validate(j, {"ms": str(ms), "article_type": "Original Paper"})) == []
    # ... but over the 4-page Applications Note cap.
    errs = _errors(_validate(j, {"ms": str(ms), "article_type": "Applications Note"}))
    assert any("5 pages exceeds the 4-page limit" in i.message for i in errs)


def test_conditional_limit_unmatched_value_falls_back_to_scalar(tmp_path):
    ms = _pdf_manuscript(tmp_path, pages=5, title="Doc")
    # The selector value matches nothing in the map and there is no default, so
    # the scalar max_pages applies as the fallback.
    j = _bioinformatics_like(max_pages=3)
    errs = _errors(_validate(j, {"ms": str(ms), "article_type": "Review"}))
    assert any("5 pages exceeds the 3-page limit" in i.message for i in errs)


def test_conditional_limit_uses_default_when_value_absent(tmp_path):
    from paperpush.database import ConditionalLimit

    ms = _pdf_manuscript(tmp_path, pages=5, title="Doc")
    by = ConditionalLimit(field="article_type", values={"Original Paper": 7}, default=2)
    j = _venue(
        Field(id="article_type", label="Type", type="choice"),
        Field(id="ms", label="Manuscript", type="file", max_pages_by=by),
    )
    errs = _errors(_validate(j, {"ms": str(ms), "article_type": "Review"}))
    assert any("5 pages exceeds the 2-page limit" in i.message for i in errs)


def test_manuscript_length_skipped_when_file_missing(tmp_path):
    # A missing file is reported once (by the file check); no length warning piles on.
    j = _venue(Field(id="ms", label="Manuscript", type="file", max_words_before_refs=10))
    issues = _validate(j, {"ms": str(tmp_path / "nope.pdf")})
    assert any("file not found" in i.message for i in _errors(issues))
    assert not any("could not count" in i.message for i in issues)


# --- file existence --------------------------------------------------------


def test_file_field_missing_is_error():
    j = _venue(Field(id="ms", label="Manuscript", type="file"))
    errs = _errors(_validate(j, {"ms": "/no/such/file.pdf"}))
    assert len(errs) == 1
    assert "file not found" in errs[0].message


def test_file_field_directory_is_error(tmp_path):
    j = _venue(Field(id="ms", label="Manuscript", type="file"))
    errs = _errors(_validate(j, {"ms": str(tmp_path)}))
    assert any("not a regular file" in i.message for i in errs)


def test_file_field_existing_pdf_passes(manuscript_pdf):
    j = _venue(Field(id="ms", label="Manuscript", type="file", accept=[".pdf", ".docx"]))
    assert _errors(_validate(j, {"ms": str(manuscript_pdf)})) == []


def test_file_field_wrong_extension_is_warning(samples):
    fig = samples.figure("fig1", ".png")
    j = _venue(Field(id="ms", label="Manuscript", type="file", accept=[".pdf", ".docx"]))
    issues = _validate(j, {"ms": str(fig)})
    assert _errors(issues) == []
    warns = _warnings(issues)
    assert len(warns) == 1
    assert "expects one of" in warns[0].message


def test_file_field_expands_user(monkeypatch, tmp_path, samples):
    ms = samples.manuscript(".pdf")
    monkeypatch.setenv("HOME", str(tmp_path))
    rel = "~/" + ms.relative_to(tmp_path).as_posix()
    j = _venue(Field(id="ms", label="Manuscript", type="file"))
    assert _errors(_validate(j, {"ms": rel})) == []


# --- PDF sanity ------------------------------------------------------------


def test_pdf_bad_header_is_error(tmp_path):
    bad = tmp_path / "fake.pdf"
    bad.write_bytes(b"not a pdf at all" * 100)
    j = _venue(Field(id="ms", label="Manuscript", type="file"))
    errs = _errors(_validate(j, {"ms": str(bad)}))
    assert any("does not look like a valid PDF" in i.message for i in errs)


def test_pdf_too_small_is_warning(tmp_path):
    tiny = tmp_path / "tiny.pdf"
    tiny.write_bytes(b"%PDF-1.4\n/Type /Page\n%%EOF\n")
    j = _venue(Field(id="ms", label="Manuscript", type="file"))
    issues = _validate(j, {"ms": str(tiny)})
    assert _errors(issues) == []
    assert any("may be empty" in i.message for i in _warnings(issues))


def test_pdf_no_pages_is_warning(tmp_path):
    # A header-valid PDF, over 1 KB, but with no /Type /Page object.
    blob = b"%PDF-1.4\n" + b"% padding line\n" * 200 + b"%%EOF\n"
    pdf = tmp_path / "nopages.pdf"
    pdf.write_bytes(blob)
    j = _venue(Field(id="ms", label="Manuscript", type="file"))
    issues = _validate(j, {"ms": str(pdf)})
    assert _errors(issues) == []
    assert any("could not detect any pages" in i.message for i in _warnings(issues))


# --- filelist --------------------------------------------------------------


def test_filelist_checks_each_path(samples):
    fig1 = samples.figure("fig1", ".png")
    j = _venue(Field(id="figs", label="Figures", type="filelist"))
    block = f"{fig1}\n/no/such/fig2.png"
    errs = _errors(_validate(j, {"figs": block}))
    assert len(errs) == 1
    assert "fig2.png" in errs[0].message


def test_filelist_only_path_segment_is_checked(samples):
    fig1 = samples.figure("fig1", ".png")
    j = _venue(Field(id="figs", label="Figures", type="filelist"))
    # 'path | caption' -- only the path before the pipe is treated as a file.
    block = f"{fig1} | A caption with a slash / in it"
    assert _errors(_validate(j, {"figs": block})) == []


def test_filelist_type_option_invalid_is_error(samples):
    fig1 = samples.figure("fig1", ".png")
    j = _venue(
        Field(
            id="figs",
            label="Figures",
            type="filelist",
            type_options=["Figure", "Table"],
        )
    )
    block = f"{fig1} | Diagram"
    errs = _errors(_validate(j, {"figs": block}))
    assert any("is not allowed" in i.message for i in errs)


# --- combined upload size --------------------------------------------------


def _venue_with_limit(limit_mb, *fields: Field) -> Venue:
    return Venue(slug="testj", name="Test Venue", fields=list(fields), max_upload_mb=limit_mb)


def test_upload_over_limit_is_error(tmp_path):
    big = tmp_path / "big.pdf"
    big.write_bytes(b"%PDF-1.4\n/Type /Page\n" + b"x" * (2 * 1024 * 1024))
    j = _venue_with_limit(1, Field(id="ms", label="Manuscript", type="file"))
    errs = _errors(_validate(j, {"ms": str(big)}))
    assert any("total upload size" in i.message and "limit" in i.message for i in errs)


def test_upload_sums_file_and_filelist(tmp_path):
    ms = tmp_path / "ms.pdf"
    ms.write_bytes(b"%PDF-1.4\n/Type /Page\n" + b"x" * (700 * 1024))
    fig = tmp_path / "fig.png"
    fig.write_bytes(b"x" * (500 * 1024))
    # 700 KB + 500 KB = 1.17 MB, over a 1 MB combined limit.
    j = _venue_with_limit(
        1,
        Field(id="ms", label="Manuscript", type="file"),
        Field(id="figs", label="Figures", type="filelist"),
    )
    errs = _errors(_validate(j, {"ms": str(ms), "figs": str(fig)}))
    assert any("total upload size" in i.message for i in errs)


def test_upload_under_limit_passes(tmp_path):
    small = tmp_path / "small.pdf"
    small.write_bytes(b"%PDF-1.4\n/Type /Page\n" + b"x" * (100 * 1024))
    j = _venue_with_limit(50, Field(id="ms", label="Manuscript", type="file"))
    assert not any("total upload size" in i.message for i in _errors(_validate(j, {"ms": str(small)})))


def test_upload_no_limit_skips_check(tmp_path):
    big = tmp_path / "big.pdf"
    big.write_bytes(b"%PDF-1.4\n/Type /Page\n" + b"x" * (5 * 1024 * 1024))
    j = _venue(Field(id="ms", label="Manuscript", type="file"))  # no max_upload_mb
    assert not any("total upload size" in i.message for i in _validate(j, {"ms": str(big)}))


# --- per-file size limit ---------------------------------------------------


def test_file_over_per_file_limit_is_error(tmp_path):
    big = tmp_path / "big.png"
    big.write_bytes(b"x" * (3 * 1024 * 1024))
    j = _venue(Field(id="ms", label="Manuscript", type="file", max_file_size_mb=1))
    errs = _errors(_validate(j, {"ms": str(big)}))
    assert any("per-file limit" in i.message and i.field == "ms" for i in errs)


def test_file_under_per_file_limit_passes(tmp_path):
    small = tmp_path / "small.png"
    small.write_bytes(b"x" * (200 * 1024))
    j = _venue(Field(id="ms", label="Manuscript", type="file", max_file_size_mb=5))
    assert not any("per-file limit" in i.message for i in _validate(j, {"ms": str(small)}))


def test_filelist_per_file_limit_flags_offending_line(tmp_path):
    ok = tmp_path / "fig1.png"
    ok.write_bytes(b"x" * (200 * 1024))
    big = tmp_path / "fig2.png"
    big.write_bytes(b"x" * (3 * 1024 * 1024))
    j = _venue(Field(id="figs", label="Figures", type="filelist", max_file_size_mb=1))
    errs = _errors(_validate(j, {"figs": f"{ok}\n{big}"}))
    assert len(errs) == 1
    assert "fig2.png" in errs[0].message and "per-file limit" in errs[0].message


def test_no_per_file_limit_skips_check(tmp_path):
    big = tmp_path / "big.png"
    big.write_bytes(b"x" * (5 * 1024 * 1024))
    j = _venue(Field(id="ms", label="Manuscript", type="file"))  # no max_file_size_mb
    assert not any("per-file limit" in i.message for i in _validate(j, {"ms": str(big)}))


def test_filelist_type_option_valid_passes(samples):
    fig1 = samples.figure("fig1", ".png")
    j = _venue(
        Field(
            id="figs",
            label="Figures",
            type="filelist",
            type_options=["Figure", "Table"],
        )
    )
    block = f"{fig1} | Figure"
    assert _errors(_validate(j, {"figs": block})) == []


# --- authorlist ------------------------------------------------------------

_AUTHOR_FIELD = Field(id="authors", label="Authors", type="authorlist")


def test_authorlist_empty_is_error():
    # An empty authorlist that is required triggers the required-field check.
    j = _venue(Field(id="authors", label="Authors", type="authorlist", required=True))
    errs = _errors(_validate(j, {"authors": ""}))
    assert any("required" in i.message for i in errs)


def test_authorlist_no_corresponding_is_error():
    j = _venue(_AUTHOR_FIELD)
    block = "Ada Lovelace | ada@x.org | U | | no"
    errs = _errors(_validate(j, {"authors": block}))
    assert any("no corresponding author" in i.message for i in errs)


def test_authorlist_multiple_corresponding_is_error():
    j = _venue(_AUTHOR_FIELD)
    block = "Ada | ada@x.org | U | | yes\nAlan | alan@x.org | U | | yes"
    errs = _errors(_validate(j, {"authors": block}))
    assert any("2 corresponding authors" in i.message for i in errs)


def test_authorlist_missing_name_is_error():
    j = _venue(_AUTHOR_FIELD)
    block = " | ada@x.org | U | | yes"
    errs = _errors(_validate(j, {"authors": block}))
    assert any("missing a name" in i.message for i in errs)


def test_authorlist_corresponding_without_email_is_error():
    j = _venue(_AUTHOR_FIELD)
    block = "Ada Lovelace | | U | | yes"
    errs = _errors(_validate(j, {"authors": block}))
    assert any("has no email" in i.message for i in errs)


def test_authorlist_valid_passes():
    j = _venue(_AUTHOR_FIELD)
    block = "Ada Lovelace | ada@x.org | U | | yes\nAlan Turing | alan@x.org | U | | no"
    assert _errors(_validate(j, {"authors": block})) == []


def test_authorlist_split_name_columns():
    # eJournalPress-style first_name/last_name columns instead of one 'name'.
    field = Field(
        id="authors",
        label="Authors",
        type="authorlist",
        fields=["first_name", "last_name", "email", "corresponding"],
    )
    j = _venue(field)
    block = "Ada | Lovelace | ada@x.org | yes"
    assert _errors(_validate(j, {"authors": block})) == []
    # A line with neither name part is flagged.
    bad = " |  | ada@x.org | yes"
    assert any("missing a name" in i.message for i in _errors(_validate(j, {"authors": bad})))


# --- unknown fields --------------------------------------------------------


def test_unknown_field_is_error():
    j = _venue(Field(id="title", label="Title", type="text"))
    errs = _errors(_validate(j, {"title": "ok", "bogus": "value"}))
    assert any("unknown field 'bogus'" in i.message for i in errs)


# --- combined --------------------------------------------------------------


def test_multiple_issues_reported_together():
    j = _venue(
        Field(id="title", label="Title", type="text", required=True),
        Field(id="kind", label="Type", type="choice", options=["A", "B"]),
        Field(id="ms", label="Manuscript", type="file"),
    )
    issues = _validate(j, {"title": "", "kind": "Z", "ms": "/no/file.pdf"})
    fields_with_errors = {i.field for i in _errors(issues)}
    assert {"title", "kind", "ms"} <= fields_with_errors


# --- CLI command -----------------------------------------------------------


def _write_sub(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_cli_validate_passing_file(tmp_path, manuscript_pdf, monkeypatch, capsys):
    # Stub the venue lookup so the CLI validates against a known schema.
    j = _venue(Field(id="ms", label="Manuscript", type="file", required=True), slug="biorxiv")
    import paperpush.cli as cli

    monkeypatch.setattr(cli, "get_venue", lambda slug: j)
    sub = _write_sub(tmp_path / "biorxiv.sub", f"@venue: biorxiv\nms: {manuscript_pdf}\n")

    rc = main(["validate", str(sub)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "passed validation" in out


def test_cli_validate_reports_errors(tmp_path, monkeypatch, capsys):
    j = _venue(Field(id="ms", label="Manuscript", type="file", required=True), slug="biorxiv")
    import paperpush.cli as cli

    monkeypatch.setattr(cli, "get_venue", lambda slug: j)
    sub = _write_sub(tmp_path / "biorxiv.sub", "@venue: biorxiv\nms:\n")

    rc = main(["validate", str(sub)])
    err = capsys.readouterr().err

    assert rc == 1
    assert "must be" in err
    assert "paperpush validate" in err


def test_cli_validate_unknown_venue(tmp_path, capsys):
    sub = _write_sub(tmp_path / "x.sub", "@venue: not_a_real_venue\n")
    rc = main(["validate", str(sub)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown venue" in err


def test_cli_validate_missing_file(capsys):
    rc = main(["validate", "/no/such/file.sub"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "cannot read" in err


# --- validate across every committed sample .sub ---------------------------
#
# The two tests above validate hand-built schemas and the CLI's error paths;
# these instead sweep every committed sample submission file so a newly added
# venue or variant is covered automatically. SCENARIOS comes from
# sample_subfiles (the single source of truth for the tests/sub_files/ fixtures).

SCENARIOS = scenarios()
# Sample ids without the ".sub" suffix, e.g. "biorxiv", "biorxiv_figures".
SUB_IDS = [s.filename[: -len(".sub")] for s in SCENARIOS]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SUB_IDS)
def test_validate_command_for_each_subfile(scenario, monkeypatch, capsys):
    """``paperpush validate <file>`` passes for every committed sample.

    Run from the repo root so the sample's repo-relative file references (e.g.
    ``tests/manuscript_files/...``) resolve during the file-existence checks.
    """
    monkeypatch.chdir(REPO_ROOT)
    rc = main(["validate", str(scenario.path)])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "ready to submit" in out


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SUB_IDS)
def test_sample_validates_against_schema(scenario):
    """Every committed sample validates cleanly against its venue's schema."""
    parsed = subfile.load(scenario.path)
    # The consolidated validator reports schema, required-field, and file
    # checks as Issues; a submittable sample must raise no errors.
    issues = validate(parsed, get_venue(scenario.venue))
    errors = [i for i in issues if i.is_error]
    assert not errors, "; ".join(f"[{i.field}] {i.message}" for i in errors)
