"""Tests for paperpush.venues.recovery_report: drafting a redacted bug report."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from paperpush.cli import main
from paperpush.venues import recovery_report as rr
from paperpush.venues.recovery_channel import LOG_FILE

HOME = str(Path.home())


def _log(directory: Path, *entries: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / LOG_FILE).open("a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def _entry(**changes) -> dict:
    entry = {
        "action": "click()",
        "target": "locator('textarea[name=\"CUSTOM_FIELD_RESPONSE_ID474374\"]').first",
        "step": "Page 6: declarations",
        "url": "https://mc.manuscriptcentral.com/nar?PARAMS=xik_secret_session_token#x",
        "attempt": 1,
        "error": "TimeoutError: Locator.click: Timeout 10000ms exceeded.",
        "resolution": "retry",
        "agent_actions": [
            {"op": "fill", "target": {"css": "[role=dialog] input"}, "value": "National Institutes of Health"},
            {"op": "press", "target": {"css": "[role=dialog] input"}, "value": "h"},
            {"op": "press", "target": {"css": "[role=dialog] input"}, "value": "Tab"},
            {"op": "upload", "target": {"label": "FILE_TO_UPLOAD"}, "value": f"{HOME}/papers/secret-draft.pdf"},
            {"op": "click", "target": {"text": "Joseph M. Rich", "exact": True}},
            {"op": "wait", "value": 2000},
        ],
        "note": "Typed Joseph M. Rich <jmrich@caltech.edu> and National Institutes of Health (cc helpdesk@example.org) from /private/x/y.pdf into the modal on https://mc.manuscriptcentral.com/nar/x",
        "handler": "agent",
        "at": "2026-11-03T11:38:27",
    }
    entry.update(changes)
    return entry


SUB = {"authors": "jmrich@caltech.edu | Mx | Joseph M. Rich | Caltech | United States | Pasadena | yes", "funding": "National Institutes of Health | UM1HG012077", "article_type": "Standard Article"}


@pytest.fixture
def run(tmp_path):
    directory = tmp_path / "nucleic_acids_research-20261103-113000-000001"
    rr.write_run_info(directory, "nucleic_acids_research", "nucleic_acids_research")
    _log(directory, _entry(), _entry(step="Page 2: file upload", action="set_input_files('/Users/someone/main.pdf')", target="get_by_label('FILE_TO_UPLOAD').nth(0)", agent_actions=[]))
    (directory / "screenshot-1.png").write_bytes(b"png")
    return directory


def test_report_is_redacted(run):
    report = rr.build_report(run, sub_values=SUB)
    text = report.title + report.body
    for secret in ("Joseph M. Rich", "jmrich@caltech.edu", "National Institutes of Health", "secret-draft.pdf' ", "xik_secret_session_token", "helpdesk@example.org", HOME, "/Users/someone", "UM1HG012077"):
        assert secret not in text, secret
    assert "<authors>" in text and "<funding>" in text and "<email>" in text
    assert '"value": "<29 characters typed>"' in text
    assert '"value": "<character key>"' in text and '"value": "Tab"' in text
    assert '"value": "secret-draft.pdf"' in text  # file name kept, folder dropped
    assert "set_input_files('main.pdf')" in text
    assert "https://mc.manuscriptcentral.com/nar\n" in text  # query string and fragment dropped
    assert "from y.pdf into the modal on https://mc.manuscriptcentral.com/nar/x" in text  # bare path cut, URL kept


def test_report_content(run):
    report = rr.build_report(run, sub_values=SUB)
    assert report.venue == "nucleic_acids_research" and report.entries == 2
    assert report.title == "[nucleic_acids_research] submit needed recovery: Page 6: declarations (+1 more step)"
    assert "fixed outside the recovery tools" in report.body and "step retried" in report.body
    assert report.screenshots == [str(run / "screenshot-1.png")]
    assert Path(report.saved_to).read_text().startswith(f"# {report.title}")


def test_issue_url_is_prefilled(run):
    report = rr.build_report(run, sub_values=SUB)
    parts = urlsplit(report.url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == rr.ISSUES_URL
    query = parse_qs(parts.query)
    assert query["title"] == [report.title] and query["labels"] == [rr.ISSUE_LABEL] and query["body"] == [report.body]
    assert not report.truncated


def test_long_reports_are_cut_to_fit_the_url(tmp_path):
    directory = tmp_path / "biorxiv-20261103-113000-000001"
    _log(directory, *[_entry(note="x" * 800) for _ in range(20)])
    report = rr.build_report(directory, sub_values={})
    assert report.truncated and len(report.url) <= rr.MAX_URL_LENGTH
    assert "recovery-report.md" in parse_qs(urlsplit(report.url).query)["body"][0]
    assert report.venue == "biorxiv"  # from the directory name when run.json is missing


def test_sub_values_are_read_from_the_recorded_subfile(tmp_path):
    sub = tmp_path / "nar.sub"
    sub.write_text("@venue: nucleic_acids_research\nfunding: |\n  National Institutes of Health | UM1HG012077\n", encoding="utf-8")
    directory = tmp_path / "run"
    rr.write_run_info(directory, "nucleic_acids_research", "nucleic_acids_research", sub)
    _log(directory, _entry())
    assert "National Institutes of Health" not in rr.build_report(directory).body


def test_no_log_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        rr.build_report(tmp_path)


def test_latest_run_with_recoveries(tmp_path):
    old, new, empty = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _log(old, _entry())
    _log(new, _entry())
    empty.mkdir()
    past = time.time() - 100
    os.utime(old / LOG_FILE, (past, past))
    assert rr.latest_run_with_recoveries(tmp_path) == new
    assert rr.latest_run_with_recoveries(tmp_path / "missing") is None


def test_cli_prints_the_draft_and_never_sends(run, capsys, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    assert main(["recovery-report", str(run)]) == 0
    out = capsys.readouterr().out
    assert "NOT been sent" in out and rr.ISSUES_URL in out
    assert opened == []
    assert main(["recovery-report", str(run), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["entries"] == 2
    assert main(["recovery-report", str(run), "--open"]) == 0
    assert len(opened) == 1 and opened[0].startswith(rr.ISSUES_URL)


def test_cli_without_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rr, "runs_root", lambda: tmp_path)
    assert main(["recovery-report"]) == 1
    assert "No submission run" in capsys.readouterr().err


def test_submit_points_at_the_report_only_after_a_recovery(tmp_path, capsys):
    from paperpush import cli
    from paperpush.venues.recovery_channel import PageTools, RememberingHandler

    tools = PageTools(tmp_path)
    handler = RememberingHandler(object(), tools)
    cli._print_recovery_report_hint(handler)
    cli._print_recovery_report_hint(None)
    assert capsys.readouterr().err == ""
    _log(tmp_path, _entry())
    cli._print_recovery_report_hint(handler)
    assert f"paperpush recovery-report {tmp_path}" in capsys.readouterr().err


def test_by_hand_entries_read_sensibly(tmp_path):
    _log(
        tmp_path,
        _entry(handler="by-hand", agent_actions=[], resolution="resolved", note="clicked Continue instead of Next"),
        _entry(handler="by-hand", agent_actions=[], resolution="retry", note=""),
        _entry(handler="by-hand", agent_actions=[], resolution="abort", note=""),
    )
    body = rr.build_report(tmp_path, sub_values={}).body
    assert "by-hand; outcome: **fixed, run continued**" in body
    assert "the clicks are not recorded; see the note above" in body
    assert "and no note was left" in body
    assert "Not fixed: the run stopped here" in body
