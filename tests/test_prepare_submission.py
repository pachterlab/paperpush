"""Tests for ``paperpush prepare-submission``: subfile -> autofill -> validate -> login -> submit.

Every step it chains is stubbed (the API autofill, the validator, the login
check, and submit), so these test only the sequencing: which steps run, where
it stops, and what it hands to submit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paperpush import cli, credentials, venues
from paperpush.report import CheckResult, ValidationReport
from paperpush.validate import ERROR, WARNING, Issue

VENUE = "biorxiv"


def _report(errors: int = 0, warnings: int = 0) -> ValidationReport:
    issues = [Issue(ERROR, "", f"error {i}") for i in range(errors)] + [Issue(WARNING, "", f"warning {i}") for i in range(warnings)]
    return ValidationReport(venue=VENUE, results=[CheckResult(id="stub", title="stub", category="stub", status="failed" if errors else "passed", issues=issues)])


@pytest.fixture
def stubs(monkeypatch, tmp_path):
    """Stub the chained steps; return a dict recording what ran, plus the manuscript dir."""
    ran: dict = {"autofill": 0, "submit": [], "login": 0, "report": _report()}
    ms = tmp_path / "ms"
    ms.mkdir()
    (ms / "manuscript.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def fake_autofill(args):
        ran["autofill"] += 1
        assert args.engine == "api" and args.provider == "anthropic"
        return 0

    monkeypatch.setattr(cli, "_cmd_autofill", fake_autofill)
    monkeypatch.setattr(cli, "validate_report_for", lambda *a, **k: ran["report"])
    monkeypatch.setattr("paperpush.report.print_report", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_cmd_submit", lambda args: ran["submit"].append(args) or 0)
    monkeypatch.setattr(cli, "_check_login", lambda venue, args: 0)
    monkeypatch.setattr(cli, "_cmd_login", lambda args: ran.__setitem__("login", ran["login"] + 1) or 0)
    monkeypatch.setattr(venues, "login_required", lambda slug: True)
    monkeypatch.setattr(credentials, "get_credential", lambda slug: object())
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    ran["ms"] = ms
    return ran


def _run(ms: Path, *extra: str) -> int:
    return cli.main(["prepare-submission", VENUE, "-p", str(ms), *extra])


def test_full_run_creates_sub_and_hands_off_to_submit(stubs):
    ms = stubs["ms"]
    assert _run(ms, "--yes") == 0
    sub = ms / f"{VENUE}.sub.txt"
    assert sub.exists()
    assert stubs["autofill"] == 1
    (submit_args,) = stubs["submit"]
    assert submit_args.subfile == str(sub)
    # Validated here, so submit must not validate again.
    assert submit_args.skip_validate and submit_args._validated


def test_existing_sub_is_not_autofilled_again(stubs):
    ms = stubs["ms"]
    assert _run(ms, "--yes", "--no-submit") == 0
    assert _run(ms, "--yes", "--no-submit") == 0
    assert stubs["autofill"] == 1
    assert _run(ms, "--yes", "--no-submit", "--refill") == 0
    assert stubs["autofill"] == 2


def test_validation_errors_stop_before_submit(stubs, capsys):
    stubs["report"] = _report(errors=2)
    assert _run(stubs["ms"], "--yes") == 1
    assert not stubs["submit"]
    assert "2 error(s) block the submission" in capsys.readouterr().err


def test_many_warnings_stop_without_terminal_unless_yes(stubs):
    stubs["report"] = _report(warnings=cli._PREPARE_MAX_WARNINGS + 1)
    # --no-submit: warnings never block a validate-only stop.
    assert _run(stubs["ms"], "--no-submit") == 0
    assert _run(stubs["ms"]) == 1
    assert not stubs["submit"]
    assert _run(stubs["ms"], "--yes") == 0
    assert len(stubs["submit"]) == 1


def test_no_terminal_and_no_yes_never_opens_the_portal(stubs):
    assert _run(stubs["ms"]) == 1
    assert not stubs["submit"]


def test_missing_login_without_terminal_stops(stubs, monkeypatch, capsys):
    monkeypatch.setattr(credentials, "get_credential", lambda slug: None)
    assert _run(stubs["ms"], "--yes") == 1
    assert not stubs["submit"] and not stubs["login"]
    assert f"paperpush login {VENUE}" in capsys.readouterr().err


def test_missing_api_key_fails_before_writing_the_sub(stubs, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert _run(stubs["ms"], "--yes") == 1
    assert not (stubs["ms"] / f"{VENUE}.sub.txt").exists()


def test_sub_for_another_venue_is_refused(stubs):
    ms = stubs["ms"]
    (ms / f"{VENUE}.sub.txt").write_text("@venue: arxiv\n", encoding="utf-8")
    assert _run(ms, "--yes") == 1
    assert not stubs["submit"]


def test_venue_may_come_last(stubs):
    ms = stubs["ms"]
    assert cli.main(["prepare-submission", "-p", str(ms), VENUE, "--yes", "--no-submit"]) == 0
    assert (ms / f"{VENUE}.sub.txt").exists()
    assert stubs["autofill"] == 1


def test_missing_venue_is_an_error(stubs, capsys):
    assert cli.main(["prepare-submission", "-p", str(stubs["ms"]), "--yes"]) == 2
    assert "no venue given" in capsys.readouterr().err
    # A last word that is an existing path is a manuscript input, not a venue.
    assert cli.main(["prepare-submission", "-p", str(stubs["ms"]), str(stubs["ms"] / "manuscript.pdf"), "--yes"]) == 2
    assert not stubs["autofill"]


@pytest.mark.parametrize("venue", ["not_a_real_venue", "general"])
def test_unsupported_venue_is_refused(stubs, venue):
    assert cli.main(["prepare-submission", venue, "-p", str(stubs["ms"]), "--yes"]) == 2
    assert cli.main(["prepare-submission", "-p", str(stubs["ms"]), venue, "--yes"]) == 2
    assert not stubs["autofill"] and not list(stubs["ms"].glob("*.sub.txt"))


def test_validate_only_venue_stops_after_validation(stubs, capsys):
    assert cli.main(["prepare-submission", "bmc_biology", "-p", str(stubs["ms"]), "--yes"]) == 0
    assert not stubs["submit"]
    assert "Stopping after validation" in capsys.readouterr().out
