"""Tests for the ``ai.anonymity`` reader's check (:func:`paperpush.anonymity.ai_task`).

The keyword scan in :mod:`paperpush.anonymity` cannot see self-identification
phrased in words it does not list ("as we showed in [5]", "our lab's cohort").
These tests cover when validate queues the reader's task, what the task holds
(the reviewer-visible files, the authors as context, the findings the keyword
scan already reported), and how a verdict is reported. Everything is offline.
"""

from __future__ import annotations

from paperpush import anonymity, report
from paperpush.ai_validate import CRITICAL_AI_TASKS, AiOptions
from paperpush.database import Field, Venue
from paperpush.subfile import SubFile
from paperpush.validate import validate_report

AUTHORS = Field(id="authors", label="Authors", type="authorlist")
MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
SUPP = Field(id="supplement_file", label="Supplement", type="file")
TITLE_PAGE = Field(id="title_page_file", label="Title page", type="file")
COVER = Field(id="cover_letter_file", label="Cover letter", type="file")

AUTHOR_LINE = "Ada Lovelace | ada@example.org | Analytical Engine Institute | 0000-0001-2345-6789 | yes"
# Self-identifying in a way no keyword list catches: no name, no "our previous work".
SUBTLE = "\\section{Method}\nAs we demonstrated in [5], the engine converges. Data were collected at our hospital.\n"
OFFLINE = dict(check_links=False, check_references=False, check_sensitive=False, check_hidden_text=False, check_openreview=False)


def _venue(*fields: Field, anonymous: bool = True) -> Venue:
    return Venue(slug="testj", name="Test", fields=list(fields), anonymous=anonymous)


def _files(tmp_path, body: str = SUBTLE) -> dict[str, str]:
    paths = {}
    for name, text in (("manuscript_file", body), ("supplement_file", "Extra results."), ("title_page_file", "Ada Lovelace, Analytical Engine Institute"), ("cover_letter_file", "Dear editor, Ada Lovelace")):
        path = tmp_path / f"{name}.tex"
        path.write_text(text)
        paths[name] = str(path)
    return paths


def _result(rep, check_id: str):
    return next((r for r in rep.results if r.id == check_id), None)


# --- what the task holds ----------------------------------------------------------


def test_task_reads_reviewer_files_only(tmp_path):
    values = {"authors": AUTHOR_LINE, **_files(tmp_path)}
    task = anonymity.ai_task(_venue(AUTHORS, MANUSCRIPT, SUPP, TITLE_PAGE, COVER), values)
    assert task.id == "ai.anonymity"
    assert sorted(task.files) == sorted([values["manuscript_file"], values["supplement_file"]])


def test_task_gives_authors_and_affiliations_as_context(tmp_path):
    values = {"authors": AUTHOR_LINE, **_files(tmp_path)}
    task = anonymity.ai_task(_venue(AUTHORS, MANUSCRIPT), values)
    assert "Ada Lovelace" in task.instructions
    assert "Analytical Engine Institute" in task.instructions
    # Emails and ORCID iDs do not help the reader judge prose; they stay out.
    assert "ada@example.org" not in task.instructions
    assert "0000-0001-2345-6789" not in task.instructions


def test_task_without_author_list_has_no_author_context(tmp_path):
    task = anonymity.ai_task(_venue(MANUSCRIPT), _files(tmp_path))
    assert "the authors are" not in task.instructions
    # The general guidance is still there.
    assert "third person" in task.instructions and "our lab" in task.instructions


def test_task_lists_known_findings_once_and_caps_them(tmp_path):
    known = ["anonymity: finding A", "anonymity: finding A", ""] + [f"anonymity: n{i}" for i in range(40)]
    task = anonymity.ai_task(_venue(MANUSCRIPT), _files(tmp_path), known=known)
    assert task.instructions.count("finding A") == 1
    assert "do not repeat" in task.instructions
    assert "n18" in task.instructions and "n19" not in task.instructions


# --- when validate queues it ------------------------------------------------------


def test_validate_queues_task_for_anonymous_venue(tmp_path):
    sub = SubFile(venue="testj", values={"authors": AUTHOR_LINE, **_files(tmp_path)})
    rep = validate_report(sub, _venue(AUTHORS, MANUSCRIPT), **OFFLINE)
    result = _result(rep, "ai.anonymity")
    assert result is not None and result.status == report.NEEDS_AI
    assert any(t.id == "ai.anonymity" for t in rep.ai_tasks)


def test_validate_skips_task_for_regular_venue_unless_asked(tmp_path):
    sub = SubFile(venue="testj", values={"authors": AUTHOR_LINE, **_files(tmp_path)})
    venue = _venue(AUTHORS, MANUSCRIPT, anonymous=False)
    assert _result(validate_report(sub, venue, **OFFLINE), "ai.anonymity") is None
    assert _result(validate_report(sub, venue, check_anonymous=True, **OFFLINE), "ai.anonymity").status == report.NEEDS_AI


def test_validate_passes_keyword_findings_to_the_task(tmp_path):
    files = _files(tmp_path, body="\\section{Intro}\nThis builds on work by Ada Lovelace at our lab.\n")
    sub = SubFile(venue="testj", values={"authors": AUTHOR_LINE, **files})
    rep = validate_report(sub, _venue(AUTHORS, MANUSCRIPT), **OFFLINE)
    task = next(t for t in rep.ai_tasks if t.id == "ai.anonymity")
    keyword_hits = [i.message for i in _result(rep, "integrity.anonymity").issues]
    assert keyword_hits, "the keyword scan should have found the author's name"
    assert all(hit in task.instructions for hit in keyword_hits)


def test_task_can_be_switched_off(tmp_path):
    sub = SubFile(venue="testj", values={"authors": AUTHOR_LINE, **_files(tmp_path)})
    rep = validate_report(sub, _venue(AUTHORS, MANUSCRIPT), disable=["ai.anonymity"], **OFFLINE)
    assert _result(rep, "ai.anonymity").status == report.UNCHECKED
    # The keyword scan still runs.
    assert _result(rep, "integrity.anonymity").status != report.UNCHECKED


def test_ai_only_report_includes_task(tmp_path):
    sub = SubFile(venue="testj", values={"authors": AUTHOR_LINE, **_files(tmp_path)})
    rep = validate_report(sub, _venue(AUTHORS, MANUSCRIPT), ai_only=True, **OFFLINE)
    assert _result(rep, "ai.anonymity").status == report.NEEDS_AI


# --- how a verdict is reported ----------------------------------------------------


def test_fail_verdict_is_a_likely_error_and_does_not_block(tmp_path):
    assert "ai.anonymity" in CRITICAL_AI_TASKS
    sub = SubFile(venue="testj", values={"authors": AUTHOR_LINE, **_files(tmp_path)})
    verdict = {"results": [{"id": "ai.anonymity", "status": "fail", "details": "manuscript_file.tex, Method: 'As we demonstrated in [5]' claims [5] as the authors' own", "evidence": "As we demonstrated in [5]"}]}
    rep = validate_report(sub, _venue(AUTHORS, MANUSCRIPT), ai=AiOptions.from_results(verdict), **OFFLINE)
    result = _result(rep, "ai.anonymity")
    assert result.status == report.LIKELY_ERROR
    assert result.source == "ai"
    assert "As we demonstrated in [5]" in result.issues[0].message
    assert not any(i.is_error for i in result.issues)


def test_pass_verdict_is_reported_as_passed(tmp_path):
    sub = SubFile(venue="testj", values={"authors": AUTHOR_LINE, **_files(tmp_path, body="We propose a method. Smith et al. [5] studied engines.")})
    verdict = {"results": [{"id": "ai.anonymity", "status": "pass", "details": "no self-identifying passages"}]}
    rep = validate_report(sub, _venue(AUTHORS, MANUSCRIPT), ai=AiOptions.from_results(verdict), **OFFLINE)
    assert _result(rep, "ai.anonymity").status == report.PASSED
