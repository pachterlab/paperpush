"""Severity boundaries: explicit constraints, uncertain findings, optional advice."""

from __future__ import annotations

import csv
import importlib
from datetime import date
from pathlib import Path

import pytest

from paperpush import report, requirements, style_check
from paperpush.cover_letter_check import date_issue
from paperpush.database import Field, Venue
from paperpush.subfile import SubFile
from paperpush.validate import (
    ERROR,
    SUGGESTION,
    WARNING,
    Checks,
    Issue,
    PublisherFallback,
    validate_report,
)

validation = importlib.import_module("paperpush.validate")
MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")


def test_audit_covers_every_registered_check_once():
    from paperpush.check_ids import known_ids

    audit = Path(__file__).resolve().parents[1] / "docs" / "validation-severity.rst"
    rows = list(csv.reader(line.strip() for line in audit.read_text().splitlines() if line.startswith('   "')))
    assert len(rows) == len(known_ids())
    assert {row[0] for row in rows} == set(known_ids())
    assert all(len(row) == 3 and row[1] and row[2] for row in rows)


@pytest.mark.parametrize(
    "rules, field, values, check_id",
    [
        (
            {"abstract": {"min_words": 5}},
            Field(id="abstract", label="Abstract", type="textarea"),
            {"abstract": "Too short."},
            "guidelines.text_fields",
        ),
    ],
)
def test_explicit_guideline_constraints_block_validation(monkeypatch, rules, field, values, check_id):
    monkeypatch.setattr(requirements, "_load_raw", lambda: {"$aliases": {}, "severity_test": rules})
    venue = Venue(slug="severity_test", name="Test", fields=[field])
    result = validate_report(SubFile(venue=venue.slug, values=values), venue, enable=[check_id])
    assert not result.ok
    check = next(r for r in result.results if r.id == check_id)
    assert check.status == report.FAILED
    assert check.issues and all(i.is_error for i in check.issues)


@pytest.mark.parametrize(
    "rules, field, check_id",
    [
        ({"cover_letter": {"required": True}}, Field(id="cover_letter", label="Cover letter", type="file"), "guidelines.cover_letter"),
        ({"title_page": {"separate_file": True}}, Field(id="title_page_file", label="Title page", type="file"), "guidelines.title_page_file"),
    ],
)
def test_upload_only_the_guidelines_ask_for_warns(monkeypatch, rules, field, check_id):
    # The portal does not require it, so an empty optional upload must not block submit.
    monkeypatch.setattr(requirements, "_load_raw", lambda: {"$aliases": {}, "severity_test": rules})
    venue = Venue(slug="severity_test", name="Test", fields=[field])
    result = validate_report(SubFile(venue=venue.slug, values={}), venue, enable=[check_id])
    check = next(r for r in result.results if r.id == check_id)
    assert check.status == report.WARNING and not result.errors


@pytest.mark.parametrize(
    "role, rules",
    [
        ("cover_letter", {"cover_letter": {"required": True}}),
        ("title_page_file", {"title_page": {"separate_file": True}}),
    ],
)
def test_required_upload_is_not_reported_twice(monkeypatch, role, rules):
    monkeypatch.setattr(requirements, "_load_raw", lambda: {"$aliases": {}, "severity_test": rules})
    field = Field(id=role, label=role, type="file", required=True)
    venue = Venue(slug="severity_test", name="Test", fields=[field])
    result = validate_report(
        SubFile(venue=venue.slug, values={}),
        venue,
        enable=["form.required", "guidelines"],
    )
    assert len(result.errors) == 1 and result.errors[0].check == "form.required"


@pytest.mark.parametrize("in_form", [True, False])
def test_unacceptable_file_type_errors_only_against_the_portal_list(monkeypatch, tmp_path, in_form):
    # The portal's accept list is what its upload box enforces: an error. A format list read
    # from the guidelines can miss formats the portal takes (arXiv's archives): a warning.
    monkeypatch.setattr(
        requirements,
        "_load_raw",
        lambda: {
            "$aliases": {},
            "severity_test": {"manuscript": {"formats": [".pdf"]}},
        },
    )
    path = tmp_path / "manuscript.txt"
    path.write_text("A manuscript in an unsupported format.")
    field = Field(
        id="manuscript_file",
        label="Manuscript",
        type="file",
        accept=[".pdf"] if in_form else None,
    )
    venue = Venue(slug="severity_test", name="Test", fields=[field])
    result = validate_report(
        SubFile(venue=venue.slug, values={field.id: str(path)}),
        venue,
        enable=["files.uploads", "guidelines.formats"],
    )
    if in_form:
        assert len(result.errors) == 1 and result.errors[0].check == "files.uploads"
    else:
        assert not result.errors
        check = next(r for r in result.results if r.id == "guidelines.formats")
        assert check.status == report.WARNING


def test_guideline_formats_match_multi_part_extensions(monkeypatch, tmp_path):
    monkeypatch.setattr(requirements, "_load_raw", lambda: {"$aliases": {}, "severity_test": {"manuscript": {"formats": [".tex", ".tar.gz"]}}})
    path = tmp_path / "source.tar.gz"
    path.write_bytes(b"")
    field = Field(id="manuscript_file", label="Manuscript", type="file")
    venue = Venue(slug="severity_test", name="Test", fields=[field])
    result = validate_report(SubFile(venue=venue.slug, values={field.id: str(path)}), venue, enable=["guidelines.formats"])
    check = next(r for r in result.results if r.id == "guidelines.formats")
    assert check.status == report.PASSED, check.issues


def test_required_keywords_warn_but_optional_keywords_only_suggest(monkeypatch, tmp_path):
    from paperpush.front_matter_check import _keywords_check

    path = tmp_path / "paper.md"
    path.write_text("# A title\n\n## Introduction\n\nA manuscript without keywords.\n")
    values = {MANUSCRIPT.id: str(path)}
    venue = Venue(slug="severity_test", name="Test", fields=[MANUSCRIPT])
    for minimum, level in [(None, SUGGESTION), (3, WARNING)]:
        monkeypatch.setattr(
            requirements,
            "_load_raw",
            lambda: {"$aliases": {}, "severity_test": {"keywords": {"min": minimum}}},
        )
        outcome = _keywords_check(venue, values)
        assert len(outcome.issues) == 1 and outcome.issues[0].level == level


@pytest.mark.parametrize(
    "body, level, phrase",
    [
        ("A hidden Markov model (HMM) helped once.", SUGGESTION, "never used again"),
        (
            "A hidden Markov model (HMM) helped. The HMM ran. A hidden Markov model (HMM) won.",
            SUGGESTION,
            "more than once",
        ),
        (
            "The hidden Markov model helped. A hidden Markov model (HMM) ran. The HMM won.",
            SUGGESTION,
            "before the acronym is defined",
        ),
        ("The HMM ran. The HMM won. The HMM stopped.", WARNING, "never spelled out"),
        (
            "A hidden Markov model (HMM) helped. A hierarchical mixture model (HMM) ran. The HMM won.",
            WARNING,
            "different meanings",
        ),
    ],
)
def test_acronym_preferences_are_distinguished_from_undecodable_acronyms(tmp_path, body, level, phrase):
    path = tmp_path / "paper.md"
    path.write_text("# A title\n\n## Introduction\n\n" + body)
    venue = Venue(slug="severity_test", name="Test", fields=[MANUSCRIPT])
    outcome = style_check._acronym_check(venue, {MANUSCRIPT.id: str(path)})
    finding = next(i for i in outcome.issues if phrase in i.message)
    assert finding.level == level


def test_reference_version_findings_levels(monkeypatch):
    from paperpush import reference_search
    from paperpush.sensitive import Finding

    findings = [
        Finding("refs.bib", category, category)
        for category in (
            "preprint has been published",
            "older preprint version",
            "preprint and article both cited",
        )
    ]
    monkeypatch.setattr(reference_search, "version_findings", lambda *args, **kwargs: findings)
    ctx = validation._ReferenceContext(refs=[], resolved={"10.1234/example": {"DOI": "10.1234/example"}})
    outcome = validation._reference_version_issues(Venue(slug="general", name="General"), {}, ctx)
    # A published preprint or a work listed twice is a reference error; an older arXiv version is a preference.
    assert [i.level for i in outcome.issues] == [WARNING, SUGGESTION, WARNING]


def test_stale_letter_suggests_while_future_letter_warns():
    today = date(2026, 11, 15)
    for written, level in [
        (date(2026, 10, 1), SUGGESTION),
        (date(2026, 12, 1), WARNING),
    ]:
        issues, _ = date_issue("cover_letter", "letter.docx", str(written), [written], today, 7)
        assert len(issues) == 1 and issues[0].level == level


def test_publisher_fallback_never_promotes_optional_advice():
    own = requirements.ManuscriptRequirements(slug="severity_test")
    full = requirements.ManuscriptRequirements(slug="severity_test", keywords=requirements.KeywordRules(max=3))
    checks = Checks()
    checks.publisher_fallback = PublisherFallback("Publisher", "Venue", True, own)

    def rule(reqs):
        return (
            [
                Issue(ERROR, "keywords", "too many"),
                Issue(SUGGESTION, "keywords", "consider different wording"),
            ]
            if reqs.keywords.max
            else []
        )

    result = checks.run("guidelines.text_fields", "Limits", "Guidelines", rule, full)
    assert [i.level for i in result.issues] == [WARNING, SUGGESTION]
    assert all("house rule" in i.message for i in result.issues)
    assert len(checks.results) == 1
    assert not checks._rerunning


def test_strict_style_promotes_new_optional_findings(monkeypatch, tmp_path):
    monkeypatch.setattr(requirements, "_load_raw", lambda: {"$aliases": {}})
    path = tmp_path / "paper.md"
    path.write_text("# A title\n\n## Introduction\n\nThe color and colour were measured.\n")
    venue = Venue(slug="severity_test", name="Test", fields=[MANUSCRIPT])
    sub = SubFile(venue=venue.slug, values={MANUSCRIPT.id: str(path)})
    normal = validate_report(sub, venue, enable=["prose.spelling_variant"])
    strict = validate_report(sub, venue, enable=["prose.spelling_variant"], strict_style=True)
    assert len(normal.suggestions) == 1 and not normal.warnings
    assert len(strict.warnings) == 1 and not strict.suggestions and strict.promoted == 1
