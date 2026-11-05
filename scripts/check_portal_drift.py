#!/usr/bin/env python3
"""Run the portal-drift tests and file a GitHub issue when a portal changed.

``tests/test_portal_drift.py`` checks each venue's live submission portal against
the committed baselines in ``tests/portal_snapshots``: the selectors the runner
depends on must still resolve, and the page structure (field names, labels,
buttons, headings) must still match. It hits real portals, so CI skips it. This
script runs it unattended and reports the result where a maintainer will see it:

* every test outcome is sorted into one of five groups --
  ``drift`` (a selector is gone or the page changed: the runner needs fixing),
  ``no_baseline`` (a spec exists but no snapshot was ever committed),
  ``no_spec`` (the venue has a runner but no drift spec),
  ``not_checked`` (bot wall, no saved session, expired session) and ``passed``;
* the report is written to ``.portal_drift/report.md`` and ``report.json``;
* when anything drifted, an issue labelled ``portal-drift`` is opened, or, if
  one is already open, the new report is added to it as a comment. A run with
  no drift comments on an open issue (so the maintainer knows the portals were
  checked again) but never closes it: closing is for whoever fixed the runner or
  refreshed the snapshot.

Only drift opens an issue. Coverage gaps (``no_spec`` / ``no_baseline``) and
venues that could not be reached are listed in the report and in any issue, but
they are standing conditions, not news.

    python scripts/check_portal_drift.py                  # run, report, file/update the issue
    python scripts/check_portal_drift.py --dry-run        # run and report; print the issue instead
    python scripts/check_portal_drift.py --venue biorxiv  # one venue (repeatable)
    python scripts/check_portal_drift.py --junit FILE --dry-run   # re-report an earlier run

``scripts/scheduled_portal_drift.sh`` is the cron entry point; it runs this
against a fresh checkout of ``origin/main`` so a maintainer's working tree never
affects the result.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # fixed pytest/gh invocations, never a shell  # nosec B404
import sys
import xml.etree.ElementTree as ET  # parses the junit file this script wrote  # nosec B405
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / ".portal_drift"
LABEL = "portal-drift"
LABEL_COLOR = "d93f0b"
LABEL_DESCRIPTION = "A venue's submission portal changed (scripts/check_portal_drift.py)"

GROUPS = ("drift", "no_baseline", "no_spec", "not_checked", "passed")
GROUP_TITLES = {
    "drift": "Drift: the runner needs fixing",
    "no_baseline": "No committed baseline",
    "no_spec": "No drift spec (portal not checked)",
    "not_checked": "Could not be checked this run",
    "passed": "Unchanged",
}
# Longest failure text kept per test in the report; pytest's tracebacks for a
# Playwright failure run to hundreds of lines and the useful part comes first.
MAX_DETAIL_CHARS = 4000

_CASE_NAME = re.compile(r"^(?P<test>[^\[]+)\[(?P<slug>[^\]]+)\]$")


@dataclass
class Outcome:
    slug: str
    page: str  # "login" or "first_step"
    group: str
    message: str = ""
    detail: str = ""


def _page_of(test_name: str) -> str:
    if "login" in test_name:
        return "login"
    if "first_step" in test_name:
        return "first_step"
    return test_name


def _redact(text: str) -> str:
    """Drop the maintainer's home directory from paths (the issue may be public)."""
    home = str(Path.home())
    return text.replace(home, "~") if home and home != "/" else text


def classify(junit_path: Path) -> list[Outcome]:
    """Sort every drift test case in a junit XML file into one of :data:`GROUPS`."""
    root = ET.parse(junit_path).getroot()  # our own pytest output  # nosec B314
    outcomes: list[Outcome] = []
    for case in root.iter("testcase"):
        m = _CASE_NAME.match(case.get("name", ""))
        if not m or "drift" not in m.group("test"):
            continue
        slug, page = m.group("slug"), _page_of(m.group("test"))
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        skipped = case.find("skipped")
        if problem is not None:
            message = problem.get("message", "") or ""
            detail = (problem.text or "").strip()
            group = "no_baseline" if "no committed baseline" in message + detail else "drift"
            outcomes.append(Outcome(slug, page, group, _redact(message), _redact(detail)[:MAX_DETAIL_CHARS]))
        elif skipped is not None:
            message = skipped.get("message", "") or (skipped.text or "")
            group = "no_spec" if "no portal-drift spec" in message else "not_checked"
            outcomes.append(Outcome(slug, page, group, _redact(message.strip())))
        else:
            outcomes.append(Outcome(slug, page, "passed"))
    return sorted(outcomes, key=lambda o: (GROUPS.index(o.group), o.slug, o.page))


def drifted_venues(outcomes: list[Outcome]) -> list[str]:
    return sorted({o.slug for o in outcomes if o.group == "drift"})


def _short(message: str) -> str:
    first = message.strip().splitlines()[0] if message.strip() else ""
    first = re.sub(r"^(Skipped|Failed): ", "", first)
    return first if len(first) <= 200 else first[:197] + "..."


def render_markdown(outcomes: list[Outcome], run_date: str, commit: str = "") -> str:
    """The report, as the body of a GitHub issue or comment."""
    lines = [f"Portal drift check, {run_date}" + (f" (main at `{commit[:10]}`)" if commit else "") + "."]
    counts = {g: len({o.slug for o in outcomes if o.group == g}) for g in GROUPS}
    lines.append("")
    lines.append(" · ".join(f"{GROUP_TITLES[g]}: {counts[g]} venue(s)" for g in GROUPS))
    for group in GROUPS:
        items = [o for o in outcomes if o.group == group]
        if not items:
            continue
        lines += ["", f"### {GROUP_TITLES[group]}", ""]
        if group == "no_spec":
            slugs = sorted({o.slug for o in items})
            lines.append(", ".join(f"`{s}`" for s in slugs))
            lines.append("")
            lines.append("Add a `_Spec` for each to `SPECS` in `tests/test_portal_drift.py`.")
            continue
        for o in items:
            note = f": {_short(o.message)}" if o.message and group != "passed" else ""
            lines.append(f"- `{o.slug}` {o.page}{note}")
            if group == "drift" and o.detail:
                lines += ["", "  <details><summary>details</summary>", "", "  ```", *("  " + ln for ln in o.detail.splitlines()), "  ```", "  </details>"]
    if counts["drift"]:
        lines += [
            "",
            "Next: run `pytest tests/test_portal_drift.py --run-portal --venue <slug>` locally and look at "
            "`tests/portal_snapshots/_failures/`. If the portal changed on purpose and the runner still works, "
            "refresh the baseline with `--update-snapshots` and commit it; if a selector is gone, fix the runner "
            "(the `paperpush-portal-debug` skill walks through it). Close this issue once both are merged.",
        ]
    return "\n".join(lines) + "\n"


# --- running pytest ---------------------------------------------------------


def run_pytest(repo_dir: Path, junit_path: Path, venues: list[str]) -> int:
    """Run the drift tests in ``repo_dir`` and return pytest's exit code."""
    cmd = [sys.executable, "-m", "pytest", "tests/test_portal_drift.py", "--run-portal-only", "-p", "no:cacheprovider", "-q", "-rs", f"--junitxml={junit_path}"]
    for v in venues:
        cmd += ["--venue", v]
    env = dict(os.environ)
    # Import the checkout under test, not whichever copy is pip-installed in -e mode.
    env["PYTHONPATH"] = os.pathsep.join(p for p in [str(repo_dir), env.get("PYTHONPATH", "")] if p)
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=repo_dir, env=env).returncode  # nosec B603


def head_commit(repo_dir: Path) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True).stdout.strip()  # nosec B603 B607
    except (OSError, subprocess.CalledProcessError):
        return ""


# --- GitHub issue -----------------------------------------------------------


def _gh(args: list[str], cwd: Path) -> str:
    return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout  # nosec B603 B607


def report_to_github(outcomes: list[Outcome], body: str, run_date: str, cwd: Path, gh=_gh) -> str:
    """Open or update the ``portal-drift`` issue; returns what was done."""
    drifted = drifted_venues(outcomes)
    open_issues = json.loads(gh(["issue", "list", "--label", LABEL, "--state", "open", "--json", "number,url"], cwd) or "[]")
    if open_issues:
        number, url = open_issues[0]["number"], open_issues[0]["url"]
        if drifted:
            gh(["issue", "comment", str(number), "--body", body], cwd)
            return f"commented on {url} (drift: {', '.join(drifted)})"
        gh(["issue", "comment", str(number), "--body", "No drift detected in this run; this issue stays open until someone closes it.\n\n" + body], cwd)
        return f"commented on {url} (no drift this run)"
    if not drifted:
        return "no drift; no issue filed"
    gh(["label", "create", LABEL, "--color", LABEL_COLOR, "--description", LABEL_DESCRIPTION, "--force"], cwd)
    title = f"Portal drift: {', '.join(drifted)} ({run_date})"
    url = gh(["issue", "create", "--title", title[:120], "--label", LABEL, "--body", body], cwd).strip()
    return f"opened {url}"


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo-dir", type=Path, default=REPO_ROOT, help="checkout whose tests and code are run (default: this one)")
    parser.add_argument("--venue", action="append", default=[], help="check only this venue slug (repeatable)")
    parser.add_argument("--junit", type=Path, help="report on this existing junit XML instead of running pytest")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR, help="where report.md/report.json go (default: .portal_drift/)")
    parser.add_argument("--dry-run", action="store_true", help="print the issue body instead of filing it")
    args = parser.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    junit = args.junit
    if junit is None:
        junit = args.out_dir / "junit.xml"
        code = run_pytest(args.repo_dir.resolve(), junit, args.venue)
        # 0: all passed/skipped, 1: some failed. Anything else (usage error,
        # collection error, nothing collected) means the check itself broke.
        if code not in (0, 1) or not junit.exists():
            print(f"ERROR: pytest exited {code}; the drift check did not run", file=sys.stderr)
            return 2

    outcomes = classify(junit)
    if not outcomes:
        print("ERROR: no portal-drift test cases in the junit report", file=sys.stderr)
        return 2
    run_date = date.today().isoformat()
    body = render_markdown(outcomes, run_date, head_commit(args.repo_dir))
    (args.out_dir / "report.md").write_text(body, encoding="utf-8")
    (args.out_dir / "report.json").write_text(json.dumps({"date": run_date, "outcomes": [asdict(o) for o in outcomes]}, indent=2) + "\n", encoding="utf-8")
    print(body)

    if args.dry_run:
        print(f"[dry-run] would {'file/update' if drifted_venues(outcomes) else 'not file'} a '{LABEL}' issue")
        return 0
    print(report_to_github(outcomes, body, run_date, args.repo_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
