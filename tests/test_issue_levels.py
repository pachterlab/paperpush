"""Error, likely error, and warning: which findings get which level, and how the report shows them.

An error is certain and blocks. A likely error would be critical if real (an identity
leak, a fabricated reference, a modified template) but is not certain: it is shown in
orange between the failed checks and the warnings, and it does not block on its own.
"""

from paperpush import ai_validate, anonymity, report, sensitive, template_check
from paperpush.sensitive import LEVEL_ERROR, LEVEL_LIKELY, LEVEL_WARNING, Finding
from paperpush.validate import ERROR, LIKELY_ERROR, WARNING, Issue, _findings_to_issues, _status_for, downgrade_errors


def _report(*issues_by_check):
    rep = report.ValidationReport(venue="testvenue")
    for n, issues in enumerate(issues_by_check):
        rep.results.append(report.CheckResult(id=f"c{n}", title=f"check {n}", category="Integrity", status=_status_for(list(issues)), issues=list(issues)))
    return rep


# --- levels and statuses ---------------------------------------------------------------


def test_findings_keep_their_level_through_validate():
    issues = _findings_to_issues([Finding("a.pdf", "x", "certain", LEVEL_ERROR), Finding("a.pdf", "x", "likely", LEVEL_LIKELY), Finding("a.pdf", "x", "plain")])
    assert [i.level for i in issues] == [ERROR, LIKELY_ERROR, WARNING]
    assert issues[1].is_likely_error and not issues[1].is_error


def test_check_status_is_failed_then_likely_then_warning():
    assert _status_for([Issue(ERROR, "", "a"), Issue(LIKELY_ERROR, "", "b")]) == report.FAILED
    assert _status_for([Issue(LIKELY_ERROR, "", "b"), Issue(WARNING, "", "c")]) == report.LIKELY_ERROR
    assert _status_for([Issue(WARNING, "", "c")]) == report.WARNING
    # A "could not check" note is not a finding, whatever its level.
    assert _status_for([Issue(LIKELY_ERROR, "", "not scanned", unchecked=True)]) == report.UNCHECKED


def _result(check_id, *issues):
    return report.CheckResult(id=check_id, title=check_id, category="X", status=_status_for(list(issues)), issues=list(issues))


def test_initial_submission_downgrades_formatting_errors_and_likely_errors():
    keywords = _result("form.counts", Issue(ERROR, "keywords", "7 keywords; the limit is 5"))
    template = _result("guidelines.template", Issue(WARNING, "", "margin 0.8in"), Issue(LIKELY_ERROR, "", "modified template"))
    length = _result("guidelines.length", Issue(ERROR, "", "11 pages; the limit is 10"), Issue(WARNING, "", "x", unchecked=True))
    assert downgrade_errors([keywords, template, length]) == 3
    for res in (keywords, template, length):
        assert res.status == report.WARNING
        assert not any(i.is_error or i.is_likely_error for i in res.issues)


def test_initial_submission_keeps_integrity_and_other_non_format_problems():
    kept = [
        _result("integrity.hidden_text", Issue(ERROR, "", "hidden prompt")),
        _result("integrity.anonymity", Issue(ERROR, "", "camera-ready"), Issue(LIKELY_ERROR, "", "author name appears")),
        _result("integrity.plagiarism", Issue(LIKELY_ERROR, "", "35% matches")),
        _result("integrity.unmatched_references", Issue(LIKELY_ERROR, "", "matches no work")),
        _result("integrity.dois", Issue(LIKELY_ERROR, "", "DOI registered to another paper")),
        _result("guidelines.ai_policy", Issue(ERROR, "", "ChatGPT listed as an author")),
        _result("ai.unverified_references", Issue(LIKELY_ERROR, "", "no such paper")),
        _result("citations.supplementary_attachments", Issue(LIKELY_ERROR, "", "supplement not attached")),
        _result("rigor.trial_registration", Issue(LIKELY_ERROR, "", "no registration number")),
        _result("ai.details.ethics", Issue(LIKELY_ERROR, "", "no ethics approval")),
    ]
    before = [(r.status, [i.level for i in r.issues]) for r in kept]
    assert downgrade_errors(kept) == 0
    assert [(r.status, [i.level for i in r.issues]) for r in kept] == before


def test_an_initial_submission_with_an_integrity_error_is_not_ready():
    rep = report.ValidationReport(venue="nature", initial_submission=True, initial_submission_rule="any format")
    rep.results = [_result("integrity.hidden_text", Issue(ERROR, "", "hidden prompt")), _result("form.counts", Issue(ERROR, "", "too many keywords"))]
    rep.downgraded = downgrade_errors(rep.results)
    assert rep.downgraded == 1 and not rep.ok
    text = report.to_text(rep)
    assert "Not ready: 1 integrity error(s) must be fixed even for an initial submission." in text
    assert "Ready to submit" not in text
    assert "integrity problems (identity leaks, hidden prompts, fabricated references, plagiarism) still count" in text


# --- the report --------------------------------------------------------------------------


def test_a_likely_error_does_not_block_but_changes_the_verdict():
    rep = _report([Issue(LIKELY_ERROR, "", "reference 3 matches no work")], [Issue(WARNING, "", "acronym")])
    assert rep.ok and len(rep.likely_errors) == 1
    assert [i.message for i in rep.warnings] == ["acronym"]  # likely errors are not warnings
    text = report.to_text(rep)
    assert "No blocking errors, but confirm the 1 likely error(s) before submitting" in text
    assert "LIKELY ERRORS (CONFIRM BEFORE SUBMITTING) (1)" in text
    assert "[LIKELY] [Integrity] check 0" in text and "likely error: reference 3 matches no work" in text
    assert "[LIKELY] 1 with likely errors" in text
    assert rep.counts()[report.LIKELY_ERROR] == 1


def test_errors_only_still_shows_likely_errors():
    hide = report.hidden_statuses(errors_only=True)
    assert report.FAILED not in hide and report.LIKELY_ERROR not in hide and report.WARNING in hide


def test_likely_errors_have_their_own_colour_and_mark():
    for pal in (report.DEFAULT_PALETTE, report.ALT_PALETTE):
        colours = {s: pal.for_status(s) for s in (report.FAILED, report.LIKELY_ERROR, report.WARNING)}
        assert len(set(colours.values())) == 3
    assert report.DEFAULT_PALETTE.likely == "#bc4c00"
    rep = _report([Issue(LIKELY_ERROR, "", "x")])
    assert "#bc4c00" in report.to_html(rep) and "◆" in report.to_markdown(rep)


def test_no_summary_chip_for_likely_errors_when_there_are_none():
    assert not any(s == report.LIKELY_ERROR for s, _ in report._summary_line(_report([Issue(WARNING, "", "x")])))


# --- anonymity ---------------------------------------------------------------------------


def test_author_name_in_body_is_likely_but_affiliation_is_a_warning():
    terms = [("author name", "Jane Q Public"), ("affiliation", "Example Institute")]
    text = "Our earlier work with Jane Q Public was done at Example Institute."
    by_label = {("author name" if "author name" in f.detail else "affiliation"): f.level for f in anonymity.scan_text("main.tex", text, terms, is_manuscript=False)}
    assert by_label == {"author name": LEVEL_LIKELY, "affiliation": LEVEL_WARNING}


def test_camera_ready_latex_is_an_error():
    text = "\\documentclass{article}\n\\usepackage[final]{acl}\n\\begin{document}x\\end{document}\n"
    levels = [f.level for f in anonymity.scan_text("main.tex", text, [], is_latex=True) if "camera-ready" in f.detail]
    assert levels == [LEVEL_ERROR]


# --- other integrity checks --------------------------------------------------------------


def test_a_credential_is_a_likely_error():
    found = list(sensitive._iter_secret_findings("code.py", "key = 'AKIAIOSFODNN7EXAMPLE'\n"))
    assert found and all(f.level == LEVEL_LIKELY for f in found)


def test_hidden_prompt_is_an_error_a_visible_one_and_other_hidden_text_a_warning():
    from paperpush import hidden_text
    from paperpush.validate import _hidden_text_level

    def level(category):
        return _hidden_text_level(hidden_text.Finding("paper.pdf", category, "..."))

    assert level(hidden_text.HIDDEN_INJECTION) == ERROR
    # A paper on prompt injection quotes such text as its subject.
    assert level(hidden_text.VISIBLE_INJECTION) == WARNING
    assert level(hidden_text.HIDDEN_TEXT) == WARNING


def test_template_edits_split_into_layout_changes_and_spacing_tweaks():
    def minor(src):
        return [template_check.is_minor_source_problem(p) for p in template_check.source_problems(src)]

    assert minor("\\usepackage{geometry}") == [False]
    assert minor("\\setlength{\\textwidth}{7in}") == [False]
    assert minor("\\begin{document}\\small text") == [False]
    assert minor("\\setlength{\\parskip}{0pt}") == [True]
    assert minor("\\setlength{\\abovecaptionskip}{2pt}") == [True]


# --- AI checks ---------------------------------------------------------------------------


def test_only_critical_ai_failures_are_likely_errors():
    assert {"ai.unverified_references", "ai.flagged_references", "ai.template", "ai.rigor.trial_registration", "ai.details.ethics", "ai.anonymity"} == set(ai_validate.CRITICAL_AI_TASKS)
    assert "ai.figures.design" not in ai_validate.CRITICAL_AI_TASKS and "ai.prose" not in ai_validate.CRITICAL_AI_TASKS
