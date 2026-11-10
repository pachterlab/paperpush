"""Local reference measurements and explicitly opted-in source reviews."""

from __future__ import annotations

from datetime import date

from paperpush import report
from paperpush.database import Field, Venue
from paperpush.subfile import SubFile
from paperpush.validate import Checks, validate_report
from paperpush import reference_review


VENUE = Venue(slug="review_test", name="Review Test", fields=[Field(id="manuscript_file", label="Manuscript", type="file")])


def _run(tmp_path, body, *, full=False):
    path = tmp_path / "paper.md"
    path.write_text(body, encoding="utf-8")
    checks = Checks(options={"reference_check_extended": full})
    reference_review.run_checks(checks, VENUE, {"manuscript_file": str(path)})
    return {r.id: r for r in checks.results}, checks.ai_tasks


def test_age_and_density_are_local_checks(tmp_path):
    old = date.today().year - 16
    recent = date.today().year - 2
    results, tasks = _run(tmp_path, f"""Introduction

Cells grow [1, 2]. More cells grow [1].

References

1. Doe, J. Cell growth. Journal of Cells ( {old} ).
2. Roe, A. New cells. Journal of Cells ( {recent} ).
""")
    assert results["bibliography.age"].status == report.WARNING
    assert "over 15 years old" in results["bibliography.age"].issues[0].message
    assert results["bibliography.density"].status == report.PASSED
    assert "3 citation occurrences" in results["bibliography.density"].detail
    assert tasks == []


def test_full_review_queues_per_source_tasks(tmp_path):
    results, tasks = _run(tmp_path, """Introduction

Cells grow [1].

References

1. Doe, J. Cell growth. Journal of Cells (2024).
2. Roe, A. Other cells. Journal of Cells (2023).
""", full=True)
    assert results["bibliography.age"].status == report.PASSED
    by_id = {t.id: t for t in tasks}
    assert set(by_id) == {"ai.reference_source.1", "ai.reference_primary.1", "ai.reference_journal.1"}
    assert "abstract or full paper" in by_id["ai.reference_source.1"].instructions
    assert "authoritative index" in by_id["ai.reference_journal.1"].instructions
    primary = by_id["ai.reference_primary.1"].instructions
    assert "full text" in primary and "report unclear" in primary
    assert "one step only" in primary
    assert "consider citing it directly" in primary


def test_unreadable_reference_list_is_not_a_pass(tmp_path):
    results, tasks = _run(tmp_path, "No reference heading or list here.", full=True)
    assert results["bibliography.age"].status == report.UNCHECKED
    assert results["bibliography.density"].status == report.UNCHECKED
    assert tasks == []


def test_full_flag_reaches_report_and_ai_only(tmp_path):
    path = tmp_path / "paper.md"
    path.write_text("Introduction\n\nCells grow [1, 2].\n\nReferences\n\n1. Doe, J. Cell growth. Journal of Cells (2024).\n2. Roe, A. Other cells. Journal of Cells (2023).\n")
    sub = SubFile(venue=VENUE.slug, values={"manuscript_file": str(path)})
    flags = dict(check_links=False, check_references=False, check_sensitive=False, check_openreview=False, reference_check_extended=True)
    full = validate_report(sub, VENUE, **flags)
    only = validate_report(sub, VENUE, ai_only=True, **flags)
    assert {task.id for task in full.ai_tasks} >= {"ai.reference_source.1", "ai.reference_primary.1", "ai.reference_journal.1"}
    assert {task.id for task in only.ai_tasks} >= {"ai.reference_source.1", "ai.reference_primary.1", "ai.reference_journal.1"}
    assert any(result.id == "bibliography.density" for result in full.results)
    assert not any(result.id == "bibliography.density" for result in only.results)
