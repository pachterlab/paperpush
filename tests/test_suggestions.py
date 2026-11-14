"""The suggestion tier: style findings that are reported but never counted as warnings."""

from __future__ import annotations

from paperpush import ai_validate, report
from paperpush import style_check as sc
from paperpush.validate import ERROR, SUGGESTION, WARNING, Checks, Issue, _status_for, promote_suggestions


def test_status_for_ranks_suggestion_below_warning_and_above_unchecked():
    hint = Issue(SUGGESTION, "", "vary the wording")
    assert _status_for([hint]) == report.SUGGESTION
    assert _status_for([hint, Issue(WARNING, "", "advisory")]) == report.WARNING
    assert _status_for([hint, Issue(ERROR, "", "broken")]) == report.FAILED
    # A finding beats a "could not check" note, as a warning does.
    assert _status_for([hint, Issue(WARNING, "", "unreadable", unchecked=True)]) == report.SUGGESTION


def test_checks_run_records_a_suggestion():
    checks = Checks()
    result = checks.run("prose.x", "Style", "Prose", lambda: [Issue(SUGGESTION, "manuscript", "4 repeated words")])
    assert result.status == report.SUGGESTION
    assert result.issues[0].check == "prose.x"


def _sample() -> report.ValidationReport:
    return report.ValidationReport(
        venue="testj",
        subfile="paper.sub",
        results=[
            report.CheckResult("a", "Passes", "Cat", report.PASSED),
            report.CheckResult("b", "Advisory", "Cat", report.WARNING, issues=[Issue(WARNING, "", "looks single-spaced"), Issue(SUGGESTION, "", "a side note on style")]),
            report.CheckResult("c", "Word choice", "Prose", report.SUGGESTION, issues=[Issue(SUGGESTION, "manuscript", "'robust' used 9 times; vary the wording")]),
        ],
    )


def test_suggestions_are_not_warnings():
    rep = _sample()
    assert [i.message for i in rep.warnings] == ["looks single-spaced"]
    assert len(rep.suggestions) == 2
    assert rep.ok
    counts = rep.counts()
    assert counts[report.WARNING] == 1 and counts[report.SUGGESTION] == 1
    data = rep.to_dict()
    assert data["counts"]["suggestion"] == 1 and data["strict_style"] is False


def test_text_report_lists_suggestions_with_their_own_marker():
    text = report.to_text(_sample())
    assert "SUGGESTIONS (STYLE) (1)" in text
    assert "[HINT] [Prose] Word choice" in text
    assert "suggestion: [manuscript] 'robust' used 9 times" in text
    # A suggestion under a warning check is labelled as one, not as a warning.
    assert "suggestion: a side note on style" in text
    assert "[HINT] 1 with suggestions" in text


def test_hiding_suggestions_counts_them_instead():
    text = report.to_text(_sample(), hide=report.hidden_statuses(hide_suggestions=True))
    assert "SUGGESTIONS (STYLE)" not in text and "robust" not in text and "a side note" not in text
    assert "Not shown: 2 suggestion(s) on style (show them with --suggestions)." in text
    # The warning they sat beside stays.
    assert "warning: looks single-spaced" in text


def test_errors_only_hides_suggestions():
    assert report.SUGGESTION in report.hidden_statuses(errors_only=True)


def test_suggestions_have_a_colour_in_both_palettes():
    assert report.DEFAULT_PALETTE.for_status(report.SUGGESTION) == "#0969da"
    alt = report.ALT_PALETTE.for_status(report.SUGGESTION)
    # Blue means passed in the colour-blind palette, so suggestions take another hue there.
    assert alt != report.ALT_PALETTE.for_status(report.PASSED)
    assert "#0969da" in report.to_html(_sample())


def test_strict_style_promotes_suggestions_to_warnings():
    rep = _sample()
    assert promote_suggestions(rep.results) == 2
    assert rep.counts()[report.SUGGESTION] == 0 and rep.counts()[report.WARNING] == 2
    assert not rep.suggestions and len(rep.warnings) == 3


def test_strict_style_note_on_the_report():
    rep = _sample()
    rep.strict_style, rep.promoted = True, promote_suggestions(rep.results)
    assert "Validated with --strict-style: 2 suggestion(s) on style shown as warnings." in report.to_text(rep)


def test_conventions_marks_consistency_as_style_and_mistakes_as_not():
    found = sc.conventions(["We used a net (ie a model).", "A vs. B and C vs D and E vs. F."])
    malformed = [p for p in found.problems if "malformed" in p]
    two_ways = [p for p in found.problems if "written two ways" in p]
    assert malformed and two_ways
    assert all(p not in found.style for p in malformed)
    assert all(p in found.style for p in two_ways)


def test_number_issues_are_suggestions_unless_the_venue_sets_the_rule():
    spelled = sc._report(["twenty-five patients"], "number(s) spelled out", "manuscript", "ms.docx", level=SUGGESTION)
    assert spelled.is_suggestion
    venue_rule = [desc for desc, _ in sc.thousands_grouping(["We counted 1,500 and 12 000 cells."], separator="comma")]
    assert venue_rule and all("venue's guidelines" in d for d in venue_rule)
    no_rule = [desc for desc, _ in sc.thousands_grouping(["We counted 1,500 and 3500 cells."])]
    assert no_rule and all("venue's guidelines" not in d for d in no_rule)


def test_ai_suggest_verdict_is_recorded_as_a_suggestion():
    parsed = ai_validate.parse_results({"results": [{"id": "ai.prose", "status": "suggestion", "details": "p. 3: a 70-word sentence"}]})
    assert parsed["ai.prose"]["status"] == "suggest"
    checks = Checks()
    task = ai_validate.AiTask(id="ai.prose", title="Prose", category="Prose", instructions="", files=["/tmp/ms.txt"])
    ai = ai_validate.AiOptions.from_results([{"id": "ai.prose", "status": "suggest", "details": "p. 3: a 70-word sentence"}], "test")
    from paperpush.database import Venue

    checks.needs_ai(task)
    ai_validate.resolve(checks, Venue(slug="general", name="General"), {}, ai)
    (result,) = [r for r in checks.results if r.id == "ai.prose"]
    assert result.status == report.SUGGESTION and result.source == "ai"
    assert result.issues[0].is_suggestion
