"""INCOMPLETE: a check part of which could not be done because a service did not answer.

On ICLR 2026 papers validated in parallel, the registries throttled the run and most
reference lookups went unanswered; the report then read as "unverified references", which
looks like a problem with the paper. Lookups that were never made are now an incomplete
check (grey), not a finding: a check that found nothing else is INCOMPLETE, and one that
did find something keeps that status and is listed under "Incomplete" as well.
"""

from __future__ import annotations

import importlib
import json

from paperpush import report, reference_search
from paperpush.validate import LIKELY_ERROR, SUGGESTION, WARNING, Issue

v = importlib.import_module("paperpush.validate")  # the package exports a validate() function under the same name


def _gap(message="3 reference(s) were not checked"):
    return v.incomplete("", message)


def test_status_ignores_incomplete_parts_when_there_are_findings():
    assert v._status_for([]) == report.PASSED
    assert v._status_for([_gap()]) == report.INCOMPLETE
    # Style suggestions do not make a partly done check look finished.
    assert v._status_for([_gap(), Issue(SUGGESTION, "", "style")]) == report.INCOMPLETE
    assert v._status_for([_gap(), Issue(LIKELY_ERROR, "", "matches no work")]) == report.LIKELY_ERROR
    assert v._status_for([_gap(), Issue(WARNING, "", "a warning")]) == report.WARNING
    # An unchecked-only check is still "could not run".
    assert v._status_for([v.unchecked("", "no tool")]) == report.UNCHECKED


def _report():
    found = report.CheckResult("integrity.unmatched_references", "References match registered works", "Integrity", report.LIKELY_ERROR,
                               issues=[Issue(LIKELY_ERROR, "", "reference 'Bai (2024)' looks wrong"), _gap("16 could not be looked up (the registries did not answer)")])
    partial = report.CheckResult("integrity.links", "Links are reachable", "Integrity", report.INCOMPLETE, issues=[_gap("1 of 2 link(s) got no answer")])
    clean = report.CheckResult("prose.spelling", "Spelling", "Prose", report.PASSED)
    return report.ValidationReport(venue="iclr_2026", results=[found, partial, clean])


def test_a_check_with_findings_is_listed_in_both_sections():
    rep = _report()
    assert [r.id for r in rep.incomplete()] == ["integrity.unmatched_references", "integrity.links"]
    text = report.to_text(rep)
    likely = text.lower().index("likely errors")
    gray = text.lower().index("incomplete: a service")
    assert "2 incomplete" in text
    assert likely < gray
    # Under its finding: both issues. Under "Incomplete": only what was not done.
    assert "Bai (2024)" in text[likely:gray] and "could not be looked up" in text[likely:gray]
    assert "could not be looked up" in text[gray:] and "Bai (2024)" not in text[gray:]
    assert "1 of 2 link(s)" in text[gray:]


def test_json_marks_incomplete_issues_and_lists_the_checks():
    data = json.loads(json.dumps(_report().to_dict()))
    assert data["incomplete"] == ["integrity.unmatched_references", "integrity.links"]
    assert data["counts"][report.INCOMPLETE] == 1
    issues = data["checks"][0]["issues"]
    assert "incomplete" not in issues[0] and issues[1]["incomplete"] is True


def test_unanswered_reference_lookups_are_incomplete_not_unverified(monkeypatch):
    refs = [reference_search.Reference(label=f"reference 'Smith{i} (2020)'", where="m.pdf", text=f"Smith{i}. A paper. 2020.") for i in range(3)]
    result = reference_search.SearchResult(lookups=[
        reference_search.Lookup(refs[0], reference_search.MATCHED, record={"title": ["A paper"]}),
        reference_search.Lookup(refs[1], reference_search.NEAR),
        reference_search.Lookup(refs[2], reference_search.UNKNOWN),
    ])
    monkeypatch.setattr(reference_search, "search_references", lambda refs: result)
    ctx = v._ReferenceContext(refs=refs)
    out = v._unmatched_reference_issues(v.Venue(slug="general", name="General"), {}, ctx)
    near = [i for i in out.issues if i.unchecked]
    gap = [i for i in out.issues if i.incomplete]
    assert len(near) == 1 and "Smith1" in near[0].message and "near misses" in near[0].message
    # The phrase scripts look for to re-run throttled papers is kept.
    assert len(gap) == 1 and "Smith2" in gap[0].message and "1 could not be looked up (the registries did not answer)" in gap[0].message
    assert v._status_for(out.issues) == report.INCOMPLETE
