#!/usr/bin/env python3
"""Run the live submit walkthrough and file a GitHub issue for venues that broke.

``tests/test_submit.py::test_submit_walkthrough`` runs the real ``paperpush
submit`` for every venue that has a runner, through every page of the portal's
form, and stops before the final submit button. Each venue gets three attempts;
only a venue that fails all three counts as broken. It hits real portals with
the maintainer's saved logins, so CI never runs it. This script runs it
unattended and reports the result where a maintainer will see it:

* every venue is sorted into ``failed``, ``skipped`` or ``passed``;
* the report is written to ``.submit_walkthrough/report.md`` and ``report.json``;
* when a venue failed, an issue labelled ``submit-walkthrough`` is opened, or,
  if one is already open, the new report is added to it as a comment. A run
  with no failures comments on an open issue (so the maintainer knows the
  portals were walked again) but never closes it: closing is for whoever fixed
  the runner.

Each run leaves an unsent draft on every portal it walks.

    python scripts/check_submit_walkthrough.py                  # run, report, file/update the issue
    python scripts/check_submit_walkthrough.py --dry-run        # run and report; print the issue instead
    python scripts/check_submit_walkthrough.py --venue biorxiv  # one venue (repeatable)
    python scripts/check_submit_walkthrough.py --headless       # no browser windows
    python scripts/check_submit_walkthrough.py --junit FILE --dry-run   # re-report an earlier run

``scripts/scheduled_submit_walkthrough.sh`` is the scheduled entry point; it
runs this against a fresh checkout of ``origin/main`` so a maintainer's working
tree never affects the result.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess  # fixed pytest/gh/git invocations, never a shell  # nosec B404
import sys
import xml.etree.ElementTree as ET  # parses the junit file this script wrote  # nosec B405
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / ".submit_walkthrough"
TEST = "tests/test_submit.py::test_submit_walkthrough"
LABEL = "submit-walkthrough"
LABEL_COLOR = "d93f0b"
LABEL_DESCRIPTION = "A venue's live submit walkthrough failed (scripts/check_submit_walkthrough.py)"
# A runner waiting on a human (an expired login, a CAPTCHA) must not hold the
# whole run forever.
DEFAULT_TIMEOUT_HOURS = 4

GROUPS = ("failed", "skipped", "passed")
GROUP_TITLES = {"failed": "Failed all attempts: the runner needs fixing", "skipped": "Skipped", "passed": "Walked to the final page"}
# Longest failure text kept per venue; a Playwright traceback runs to hundreds
# of lines and the useful part comes first.
MAX_DETAIL_CHARS = 4000

_CASE_NAME = re.compile(r"^test_submit_walkthrough\[(?P<id>[^\]]+)\]$")


@dataclass
class Outcome:
    venue: str  # the test id: the venue slug, or a .sub stem with --subfile
    group: str
    message: str = ""
    detail: str = ""


def _redact(text: str) -> str:
    """Drop the maintainer's home directory from paths (the issue may be public)."""
    home = str(Path.home())
    return text.replace(home, "~") if home and home != "/" else text


def classify(junit_path: Path) -> list[Outcome]:
    """Sort every walkthrough case in a junit XML file into one of :data:`GROUPS`."""
    root = ET.parse(junit_path).getroot()  # our own pytest output  # nosec B314
    outcomes: list[Outcome] = []
    for case in root.iter("testcase"):
        m = _CASE_NAME.match(case.get("name", ""))
        if not m:
            continue
        venue = m.group("id")
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        skipped = case.find("skipped")
        if problem is not None:
            message, detail = problem.get("message", "") or "", (problem.text or "").strip()
            outcomes.append(Outcome(venue, "failed", _redact(message), _redact(detail)[:MAX_DETAIL_CHARS]))
        elif skipped is not None:
            outcomes.append(Outcome(venue, "skipped", _redact((skipped.get("message", "") or skipped.text or "").strip())))
        else:
            outcomes.append(Outcome(venue, "passed"))
    return sorted(outcomes, key=lambda o: (GROUPS.index(o.group), o.venue))


def failed_venues(outcomes: list[Outcome]) -> list[str]:
    return [o.venue for o in outcomes if o.group == "failed"]


def _short(message: str) -> str:
    first = message.strip().splitlines()[0] if message.strip() else ""
    first = re.sub(r"^(Skipped|Failed|AssertionError): ", "", first)
    return first if len(first) <= 200 else first[:197] + "..."


def render_markdown(outcomes: list[Outcome], run_date: str, commit: str = "", note: str = "") -> str:
    """The report, as the body of a GitHub issue or comment."""
    lines = [f"Submit walkthrough, {run_date}" + (f" (main at `{commit[:10]}`)" if commit else "") + "."]
    if note:
        lines += ["", note]
    lines += ["", " · ".join(f"{GROUP_TITLES[g]}: {sum(o.group == g for o in outcomes)}" for g in GROUPS)]
    for group in GROUPS:
        items = [o for o in outcomes if o.group == group]
        if not items:
            continue
        lines += ["", f"### {GROUP_TITLES[group]}", ""]
        if group == "passed":
            lines.append(", ".join(f"`{o.venue}`" for o in items))
            continue
        for o in items:
            lines.append(f"- `{o.venue}`" + (f": {_short(o.message)}" if o.message else ""))
            if o.detail:
                lines += ["", "  <details><summary>details</summary>", "", "  ```", *("  " + ln for ln in o.detail.splitlines()), "  ```", "  </details>"]
    if failed_venues(outcomes):
        lines += [
            "",
            "Next: re-run one venue in a visible browser with `pytest tests/test_submit.py --run-portal -s --venue <slug>` "
            "and see where it stops. An expired login fails here too: `paperpush login <slug>` fixes that. If the portal "
            "changed, fix the runner (the `paperpush-portal-debug` skill walks through it). Close this issue once the fix is merged.",
        ]
    return "\n".join(lines) + "\n"


# --- running pytest ---------------------------------------------------------


def run_pytest(repo_dir: Path, junit_path: Path, venues: list[str], headless: bool, timeout_hours: float) -> tuple[int | None, str]:
    """Run the walkthrough in ``repo_dir``; returns (pytest exit code or None on timeout, note)."""
    # No -s: with pytest capturing stdin, each runner's hold_open returns at the
    # final page instead of waiting for Ctrl+D, so venues run back to back.
    cmd = [sys.executable, "-m", "pytest", TEST, "--run-portal", "-p", "no:cacheprovider", "-q", "-rs", f"--junitxml={junit_path}"]
    for v in venues:
        cmd += ["--venue", v]
    env = dict(os.environ)
    # Import the checkout under test, not whichever copy is pip-installed in -e mode.
    env["PYTHONPATH"] = os.pathsep.join(p for p in [str(repo_dir), env.get("PYTHONPATH", "")] if p)
    if headless:
        env["PAPERPUSH_WALKTHROUGH_HEADLESS"] = "1"
    print("$ " + " ".join(cmd), flush=True)
    proc = subprocess.Popen(cmd, cwd=repo_dir, env=env, stdin=subprocess.DEVNULL)  # nosec B603
    try:
        return proc.wait(timeout=timeout_hours * 3600), ""
    except subprocess.TimeoutExpired:
        # Interrupt rather than kill, so pytest still writes the junit report
        # for the venues it finished.
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return None, f"The run was stopped after {timeout_hours:g} hours; venues not listed below were not walked."


def head_commit(repo_dir: Path) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True).stdout.strip()  # nosec B603 B607
    except (OSError, subprocess.CalledProcessError):
        return ""


# --- GitHub issue -----------------------------------------------------------


def _gh(args: list[str], cwd: Path) -> str:
    return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout  # nosec B603 B607


def report_to_github(outcomes: list[Outcome], body: str, run_date: str, cwd: Path, gh=_gh) -> str:
    """Open or update the ``submit-walkthrough`` issue; returns what was done."""
    failed = failed_venues(outcomes)
    open_issues = json.loads(gh(["issue", "list", "--label", LABEL, "--state", "open", "--json", "number,url"], cwd) or "[]")
    if open_issues:
        number, url = open_issues[0]["number"], open_issues[0]["url"]
        if failed:
            gh(["issue", "comment", str(number), "--body", body], cwd)
            return f"commented on {url} (failed: {', '.join(failed)})"
        gh(["issue", "comment", str(number), "--body", "Every venue walked through in this run; this issue stays open until someone closes it.\n\n" + body], cwd)
        return f"commented on {url} (no failures this run)"
    if not failed:
        return "no failures; no issue filed"
    gh(["label", "create", LABEL, "--color", LABEL_COLOR, "--description", LABEL_DESCRIPTION, "--force"], cwd)
    title = f"Submit walkthrough failed: {', '.join(failed)} ({run_date})"
    url = gh(["issue", "create", "--title", title[:120], "--label", LABEL, "--body", body], cwd).strip()
    return f"opened {url}"


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo-dir", type=Path, default=REPO_ROOT, help="checkout whose tests and code are run (default: this one)")
    parser.add_argument("--venue", action="append", default=[], help="walk only this venue slug (repeatable)")
    parser.add_argument("--headless", action="store_true", help="run the browsers headless (more portals bot-wall a headless browser)")
    parser.add_argument("--timeout-hours", type=float, default=DEFAULT_TIMEOUT_HOURS, help=f"stop the whole run after this long (default {DEFAULT_TIMEOUT_HOURS})")
    parser.add_argument("--junit", type=Path, help="report on this existing junit XML instead of running pytest")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR, help="where report.md/report.json go (default: .submit_walkthrough/)")
    parser.add_argument("--dry-run", action="store_true", help="print the issue body instead of filing it")
    args = parser.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    junit, note = args.junit, ""
    if junit is None:
        junit = args.out_dir / "junit.xml"
        junit.unlink(missing_ok=True)
        code, note = run_pytest(args.repo_dir.resolve(), junit, args.venue, args.headless, args.timeout_hours)
        # 0: all passed/skipped, 1: some failed. Anything else (usage error,
        # collection error, nothing collected) means the check itself broke.
        if code not in (0, 1, None) or not junit.exists():
            print(f"ERROR: pytest exited {code}; the walkthrough did not run" + (f" ({note})" if note else ""), file=sys.stderr)
            return 2

    outcomes = classify(junit)
    if not outcomes:
        print("ERROR: no submit-walkthrough cases in the junit report", file=sys.stderr)
        return 2
    run_date = date.today().isoformat()
    body = render_markdown(outcomes, run_date, head_commit(args.repo_dir), note)
    (args.out_dir / "report.md").write_text(body, encoding="utf-8")
    (args.out_dir / "report.json").write_text(json.dumps({"date": run_date, "note": note, "outcomes": [asdict(o) for o in outcomes]}, indent=2) + "\n", encoding="utf-8")
    print(body)

    if args.dry_run:
        print(f"[dry-run] would {'file/update' if failed_venues(outcomes) else 'not file'} a '{LABEL}' issue")
        return 0
    print(report_to_github(outcomes, body, run_date, args.repo_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
