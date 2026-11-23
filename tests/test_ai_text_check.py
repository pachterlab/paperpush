"""paperpush validate --ai-detection: the opt-in AI-text scan through Pangram (no network: the API is faked)."""

from __future__ import annotations

import importlib

import pytest

from paperpush import ai_text_check as at
from paperpush import report
from paperpush.cli import _shown_command, build_parser, main
from paperpush.database import Field, Venue
from paperpush.subfile import SubFile
from paperpush.validate import SIMILARITY, WARNING, Issue, _status_for

validate_mod = importlib.import_module("paperpush.validate")
OFFLINE = dict(check_links=False, check_references=False, check_sensitive=False, check_hidden_text=False, check_openreview=False, check_manuscript=False)
BODY = "We measured the expression of many genes in single cells across tissues. " * 40
TEXT = "Introduction\n" + BODY + "\nReferences\n1. Someone et al. A cited paper. Nature 2020.\n"
DISCLOSED = TEXT.replace("Introduction\n", "Introduction\n" + "AI use statement: ChatGPT was used to edit the language of the introduction.\n")


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for names in at.KEY_ENV.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    validate_mod._MODELS.clear()
    yield
    validate_mod._MODELS.clear()


def _answer(fraction_ai=0.4, assisted=0.1, windows=None):
    if windows is None:
        windows = [
            {"text": "We measured the expression of many genes.", "label": "AI-Generated", "ai_assistance_score": 0.95, "confidence": "High", "start_index": 0, "end_index": 41, "word_count": 7, "is_humanized": False},
            {"text": "Across tissues, a guess.", "label": "AI-Generated", "ai_assistance_score": 0.6, "confidence": "Low", "start_index": 41, "end_index": 65, "word_count": 4, "is_humanized": False},
            {"text": "Plain human text.", "label": "Human Written", "ai_assistance_score": 0.05, "confidence": "High", "start_index": 65, "end_index": 82, "word_count": 3, "is_humanized": False},
        ]
    return {"stage": "STAGE_SUCCESS", "version": "4.1", "headline": "AI Detected", "fraction_ai": fraction_ai, "fraction_ai_assisted": assisted, "fraction_human": round(1 - fraction_ai - assisted, 3), "windows": windows}


@pytest.fixture
def calls(monkeypatch):
    """Fake Pangram: POST /task starts a task, the first poll is still running, the second answers."""
    seen: list[tuple[str, dict | None, dict]] = []
    state = {"answer": _answer(), "polls": 0}

    def fake(url, headers, body=None):
        seen.append((url, body, headers))
        if body is not None:
            return {"task_id": "t1"}
        state["polls"] += 1
        return {"stage": "STAGE_PREPROCESSING", "task_id": "t1"} if state["polls"] == 1 else state["answer"]

    monkeypatch.setattr(at, "_request", fake)
    monkeypatch.setattr(at.time, "sleep", lambda s: None)
    return seen, state


def test_options_need_a_key(monkeypatch):
    with pytest.raises(ValueError, match="PANGRAM_API_KEY"):
        at.options()
    monkeypatch.setenv("PANGRAM_API_KEY", "env-key")
    opts = at.options(threshold=30)
    assert (opts.provider, opts.api_key, opts.threshold, opts.model) == ("pangram", "env-key", 30.0, None)
    with pytest.raises(ValueError, match="percentage"):
        at.options(api_key="k", threshold=120)


def test_scan_starts_a_task_and_polls_it(calls):
    seen, _ = calls
    result = at.scan(BODY, at.options(api_key="k", model="pangram-4"))
    assert seen[0][0] == at.TASK_URL and seen[0][1] == {"text": BODY.strip(), "model": "pangram-4"}
    assert seen[0][2] == {"x-api-key": "k"}
    assert [u for u, _, _ in seen[1:]] == [f"{at.TASK_URL}/t1"] * 2
    assert (result.percent_ai, result.percent_ai_assisted, result.version) == (40.0, 10.0, "4.1")
    assert [p.is_ai for p in result.passages] == [True, True, False]


def test_a_failed_task_is_an_error(calls):
    _, state = calls
    state["answer"] = {"stage": "STAGE_FAILED", "headline": "preprocessing: Input text contains no valid text"}
    with pytest.raises(at.AiTextError, match="no valid text"):
        at.scan(BODY, at.options(api_key="k"))


def test_over_the_threshold_is_a_similarity_finding_with_the_confident_passages(calls):
    issues, detail = at.check_text(BODY, at.options(api_key="k"), "manuscript_file")
    assert [i.level for i in issues] == [SIMILARITY, SIMILARITY]  # the low-confidence passage is not listed
    assert "40% of the text is AI-generated" in issues[0].message and "not proof" in issues[0].message
    assert "no statement on AI use" in issues[0].message
    assert "We measured the expression" in issues[1].message
    assert "40% AI-generated, 10% AI-assisted, 50% human" in detail and "flags at 20%" in detail


def test_a_disclosure_is_found_and_quoted(calls):
    issues, _ = at.check_text(BODY, at.options(api_key="k"), full_text=DISCLOSED)
    assert "has a statement on AI use" in issues[0].message and "ChatGPT was used to edit" in issues[0].message


def test_under_the_threshold_passes_with_the_shares_in_the_note(calls):
    _, state = calls
    state["answer"] = _answer(fraction_ai=0.05, windows=[])
    issues, detail = at.check_text(BODY, at.options(api_key="k"))
    assert issues == [] and "5% AI-generated" in detail


def test_humanized_passages_are_flagged_even_under_the_threshold(calls):
    _, state = calls
    state["answer"] = _answer(fraction_ai=0.0, windows=[{"text": "Reworded text.", "label": "Human Written", "confidence": "Medium", "is_humanized": True}])
    issues, _ = at.check_text(BODY, at.options(api_key="k"))
    assert len(issues) == 1 and issues[0].level == SIMILARITY and "humanizer" in issues[0].message


def test_long_text_is_split_and_the_shares_weighted_by_words(calls, monkeypatch):
    seen, state = calls
    monkeypatch.setattr(at, "CHUNK_CHARS", 1000)
    state["polls"] = 1  # every poll answers at once
    result = at.scan(BODY + "\n" + BODY, at.options(api_key="k"))
    assert sum(1 for _, body, _ in seen if body is not None) > 1
    assert result.percent_ai == 40.0


def test_disclosure_detection():
    assert at.disclosure("Declaration of generative AI use\nThe authors used Claude to proofread.")
    assert at.disclosure("We used a large language model to polish the wording of the abstract.")
    assert at.disclosure("No generative AI tools were used in writing this manuscript.")
    assert not at.disclosure("We measured gene expression in single cells.")


# --- in validate ------------------------------------------------------------


def _venue():
    return Venue(slug="test_aitext", name="Test", fields=[Field(id="title", label="Title", type="text"), Field(id="manuscript_file", label="Manuscript", type="file")])


def _check(rep):
    return next(r for r in rep.results if r.id == "integrity.ai_text")


def test_validate_leaves_it_off_by_default():
    rep = validate_mod.validate_report(SubFile(venue="test_aitext", values={"title": "T"}), _venue(), **OFFLINE)
    result = _check(rep)
    assert result.status == "unchecked" and "--ai-detection" in result.detail


def test_validate_scans_the_manuscript_without_its_references(calls, tmp_path):
    seen, _ = calls
    manuscript = tmp_path / "manuscript.txt"
    manuscript.write_text(DISCLOSED, encoding="utf-8")
    sub = SubFile(venue="test_aitext", values={"title": "T", "manuscript_file": str(manuscript)})
    rep = validate_mod.validate_report(sub, _venue(), ai_detection=at.options(api_key="k"), **OFFLINE)
    result = _check(rep)
    assert result.status == report.SIMILARITY and "40% AI-generated" in result.detail
    assert "A cited paper" not in seen[0][1]["text"]
    # Not an error, not a warning: the report still reads ready, with the scan to review.
    assert rep.ok and rep.similarities and not any(i.is_similarity for i in rep.warnings)
    text = report.to_text(rep)
    assert "Review the similarity scan (pink)" in text and "- [manuscript_file] AI text:" in text


def test_validate_reports_a_service_failure_as_not_checked(monkeypatch, tmp_path):
    def refuse(url, headers, body=None):
        raise at.AiTextError("the account is out of credits")

    monkeypatch.setattr(at, "_request", refuse)
    manuscript = tmp_path / "manuscript.txt"
    manuscript.write_text(TEXT, encoding="utf-8")
    sub = SubFile(venue="test_aitext", values={"title": "T", "manuscript_file": str(manuscript)})
    result = _check(validate_mod.validate_report(sub, _venue(), ai_detection=at.options(api_key="k"), **OFFLINE))
    assert result.status == "unchecked" and "out of credits" in result.detail


def test_cli_without_a_key_stops(capsys, tmp_path):
    assert main(["validate", str(tmp_path / "x.sub.txt"), "--ai-detection"]) == 2
    assert "needs an API key" in capsys.readouterr().err


def test_api_keys_on_the_command_line_are_masked_in_the_report():
    argv = ["validate", "x.sub.txt", "--ai-detection-api-key", "secret1", "--plagiarism-api-key=secret2", "--plagiarism"]
    args = build_parser().parse_args(argv)
    args.raw_argv = argv
    shown = _shown_command(args)
    assert "secret1" not in shown and "secret2" not in shown
    assert "--ai-detection-api-key '***'" in shown and "--plagiarism-api-key=***" in shown


# --- the similarity level ---------------------------------------------------


def test_similarity_sits_between_likely_errors_and_warnings():
    assert _status_for([Issue(SIMILARITY, "", "a"), Issue(WARNING, "", "b")]) == report.SIMILARITY
    assert _status_for([Issue(SIMILARITY, "", "a", unchecked=True)]) == report.UNCHECKED
    statuses = report.STATUSES
    assert statuses.index(report.LIKELY_ERROR) < statuses.index(report.SIMILARITY) < statuses.index(report.WARNING)
    assert report.DEFAULT_PALETTE.for_status(report.SIMILARITY) == "#bf3989"


def test_similarity_renders_in_every_format(tmp_path):
    rep = report.ValidationReport(venue="nature")
    rep.results = [report.CheckResult(id="integrity.plagiarism", title="plagiarism", category="Integrity", status=report.SIMILARITY, issues=[Issue(SIMILARITY, "", "plagiarism: 35% matches", check="integrity.plagiarism")])]
    assert "1 with similarity to review" in report.to_text(rep)
    assert "#bf3989" in report.to_html(rep)
    assert "Similarity" in report.to_markdown(rep)
