"""Offline tests for scripts/check_portal_drift.py (no portal, no gh)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("check_portal_drift", REPO_ROOT / "scripts" / "check_portal_drift.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_portal_drift"] = module
    spec.loader.exec_module(module)
    return module


cpd = _load()

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.test_portal_drift" name="test_login_page_drift[biorxiv]"/>
  <testcase classname="tests.test_portal_drift" name="test_first_step_drift[biorxiv]">
    <failure message="AssertionError: selector(s) the runner depends on are gone (portal UI changed):&#10;  - title field">HOME/x/_failures/biorxiv.png
assert not missing</failure>
  </testcase>
  <testcase classname="tests.test_portal_drift" name="test_login_page_drift[bioinformatics]">
    <failure message="Failed: no committed baseline for bioinformatics 'login'. Generate it with: pytest --run-portal --update-snapshots"/>
  </testcase>
  <testcase classname="tests.test_portal_drift" name="test_first_step_drift[bioinformatics]">
    <skipped type="pytest.skip" message="bioinformatics: blocked by an anti-bot challenge (Cloudflare)."/>
  </testcase>
  <testcase classname="tests.test_portal_drift" name="test_login_page_drift[nature]">
    <skipped type="pytest.skip" message="no portal-drift spec for 'nature' yet."/>
  </testcase>
  <testcase classname="tests.test_other" name="test_unrelated[x]"/>
</testsuite></testsuites>
"""


def _junit(tmp_path: Path) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT.replace("HOME", str(Path.home())), encoding="utf-8")
    return path


def test_classify_sorts_every_outcome(tmp_path):
    outcomes = cpd.classify(_junit(tmp_path))
    got = {(o.slug, o.page): o.group for o in outcomes}
    assert got == {
        ("biorxiv", "first_step"): "drift",
        ("biorxiv", "login"): "passed",
        ("bioinformatics", "login"): "no_baseline",
        ("bioinformatics", "first_step"): "not_checked",
        ("nature", "login"): "no_spec",
    }
    assert [o.group for o in outcomes] == sorted((o.group for o in outcomes), key=cpd.GROUPS.index)
    assert cpd.drifted_venues(outcomes) == ["biorxiv"]


def test_report_redacts_home_and_lists_groups(tmp_path):
    body = cpd.render_markdown(cpd.classify(_junit(tmp_path)), "2026-12-01", "abcdef1234567")
    assert str(Path.home()) not in body
    assert "~/x/_failures/biorxiv.png" in body
    assert "`abcdef1234`" in body
    for group in ("drift", "no_baseline", "no_spec", "not_checked", "passed"):
        assert cpd.GROUP_TITLES[group] in body
    assert "--update-snapshots" in body  # next steps are given when something drifted


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
    return [cpd.Outcome(f"v{i}", "login", g) for i, g in enumerate(groups)]


def test_drift_with_no_open_issue_opens_one():
    gh = FakeGh([])
    result = cpd.report_to_github(_outcomes("drift", "passed"), "body", "2026-12-01", Path("."), gh=gh)
    assert result == "opened https://github.com/o/r/issues/7"
    create = next(c for c in gh.calls if c[:2] == ["issue", "create"])
    assert "Portal drift: v0 (2026-12-01)" in create and cpd.LABEL in create


def test_drift_with_open_issue_comments_instead_of_duplicating():
    gh = FakeGh([{"number": 3, "url": "u3"}])
    cpd.report_to_github(_outcomes("drift"), "body", "2026-12-01", Path("."), gh=gh)
    assert not any(c[:2] == ["issue", "create"] for c in gh.calls)
    assert any(c[:3] == ["issue", "comment", "3"] for c in gh.calls)


def test_no_drift_never_opens_or_closes():
    gh = FakeGh([])
    assert cpd.report_to_github(_outcomes("passed", "no_spec", "not_checked"), "b", "d", Path("."), gh=gh) == "no drift; no issue filed"
    assert all(c[:2] == ["issue", "list"] for c in gh.calls)

    gh = FakeGh([{"number": 3, "url": "u3"}])
    cpd.report_to_github(_outcomes("passed"), "b", "d", Path("."), gh=gh)
    assert not any("close" in c for c in gh.calls)
    assert any(c[:3] == ["issue", "comment", "3"] for c in gh.calls)


def test_main_dry_run_from_existing_junit(tmp_path, capsys):
    out = tmp_path / "out"
    assert cpd.main(["--junit", str(_junit(tmp_path)), "--out-dir", str(out), "--dry-run"]) == 0
    assert (out / "report.md").exists()
    data = json.loads((out / "report.json").read_text())
    assert {o["group"] for o in data["outcomes"]} == set(cpd.GROUPS)
    assert "would file/update" in capsys.readouterr().out
