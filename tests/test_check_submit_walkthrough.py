"""Offline tests for scripts/check_submit_walkthrough.py (no portal, no gh)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("check_submit_walkthrough", REPO_ROOT / "scripts" / "check_submit_walkthrough.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_submit_walkthrough"] = module
    spec.loader.exec_module(module)
    return module


csw = _load()

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.test_submit" name="test_submit_walkthrough[biorxiv]"/>
  <testcase classname="tests.test_submit" name="test_submit_walkthrough[nature]">
    <failure message="AssertionError: submit nature failed all 3 attempts; last error: TimeoutError('Upload')">HOME/x/nature.log
assert rc == 0</failure>
  </testcase>
  <testcase classname="tests.test_submit" name="test_submit_walkthrough[arxiv]">
    <skipped type="pytest.skip" message="arxiv: no saved session"/>
  </testcase>
  <testcase classname="tests.test_submit" name="test_submit_command_for_each_subfile[biorxiv]"/>
</testsuite></testsuites>
"""


def _junit(tmp_path: Path) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT.replace("HOME", str(Path.home())), encoding="utf-8")
    return path


def test_classify_only_walkthrough_cases(tmp_path):
    outcomes = csw.classify(_junit(tmp_path))
    assert [(o.venue, o.group) for o in outcomes] == [("nature", "failed"), ("arxiv", "skipped"), ("biorxiv", "passed")]
    assert csw.failed_venues(outcomes) == ["nature"]


def test_report_redacts_home_and_says_what_to_do(tmp_path):
    body = csw.render_markdown(csw.classify(_junit(tmp_path)), "2027-02-01", "abcdef1234567", note="stopped early")
    assert str(Path.home()) not in body
    assert "~/x/nature.log" in body
    assert "`abcdef1234`" in body and "stopped early" in body
    assert "submit nature failed all 3 attempts" in body
    assert "paperpush login <slug>" in body


class FakeGh:
    def __init__(self, open_issues):
        self.open_issues = open_issues
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            return json.dumps(self.open_issues)
        if args[:2] == ["issue", "create"]:
            return "https://github.com/o/r/issues/7\n"
        return ""


def _outcomes(*groups):
    return [csw.Outcome(f"v{i}", g) for i, g in enumerate(groups)]


def test_failure_with_no_open_issue_opens_one():
    gh = FakeGh([])
    assert csw.report_to_github(_outcomes("failed", "passed"), "body", "2027-02-01", Path("."), gh=gh) == "opened https://github.com/o/r/issues/7"
    create = next(c for c in gh.calls if c[:2] == ["issue", "create"])
    assert "Submit walkthrough failed: v0 (2027-02-01)" in create and csw.LABEL in create


def test_failure_with_open_issue_comments_instead_of_duplicating():
    gh = FakeGh([{"number": 3, "url": "u3"}])
    csw.report_to_github(_outcomes("failed"), "body", "d", Path("."), gh=gh)
    assert not any(c[:2] == ["issue", "create"] for c in gh.calls)
    assert any(c[:3] == ["issue", "comment", "3"] for c in gh.calls)


def test_no_failure_never_opens_or_closes():
    gh = FakeGh([])
    assert csw.report_to_github(_outcomes("passed", "skipped"), "b", "d", Path("."), gh=gh) == "no failures; no issue filed"
    assert all(c[:2] == ["issue", "list"] for c in gh.calls)

    gh = FakeGh([{"number": 3, "url": "u3"}])
    csw.report_to_github(_outcomes("passed"), "b", "d", Path("."), gh=gh)
    assert not any("close" in c for c in gh.calls)
    assert any(c[:3] == ["issue", "comment", "3"] for c in gh.calls)


def test_main_dry_run_from_existing_junit(tmp_path, capsys):
    out = tmp_path / "out"
    assert csw.main(["--junit", str(_junit(tmp_path)), "--out-dir", str(out), "--dry-run"]) == 0
    assert (out / "report.md").exists()
    assert {o["group"] for o in json.loads((out / "report.json").read_text())["outcomes"]} == set(csw.GROUPS)
    assert "would file/update" in capsys.readouterr().out


def test_run_pytest_passes_venue_and_headless(tmp_path, monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, cmd, cwd, env, stdin):
            seen.update(cmd=cmd, env=env)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(csw.subprocess, "Popen", FakePopen)
    assert csw.run_pytest(tmp_path, tmp_path / "j.xml", ["nature"], True, 1) == (0, "")
    assert csw.TEST in seen["cmd"] and "--run-portal" in seen["cmd"] and "-s" not in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--venue") + 1] == "nature"
    assert seen["env"]["PAPERPUSH_WALKTHROUGH_HEADLESS"] == "1"
    assert seen["env"]["PYTHONPATH"].split(":")[0] == str(tmp_path)
