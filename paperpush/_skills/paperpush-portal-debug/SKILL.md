---
name: paperpush-portal-debug
description: >-
  Debug a `paperpush submit` run that stalls or fails partway through a
  venue's portal: drive the live browser to get past the failing step, keep a
  log of every change the Playwright runner needs, and (in a source checkout)
  put those changes into the runner with a regression test. Never sends the
  submission. Triggers: "submit is stuck", "paperpush submit failed at
  <step>", "the <venue> portal changed", "debug the submit run live", "fix the
  <venue> runner".
---

# Debug a stalled `paperpush submit`

A stall almost always means the portal and the runner
(`paperpush/venues/<portal>/<slug>.py`) disagree: a renamed button, a new
question, a slow upload, a moved iframe. Your job has two parts: get the live
run through to the review page, and **record each change the script needs**
so the next run does not stall at the same step.

Read `DEVELOPMENT.md` ("Debugging", "Recovering a run that gets stuck") and
the recovery part of `AGENTS.md` first.

## The boundary

- Never press the final submit/approve control, and never press Enter on a
  form that could send it. Leave the final send to the author. Do not use
  the CDP endpoint, `javascript_tool`, or any other route to get around the
  recovery guardrails.
- Never ask for the user's password. A sign-in failure is a `login()` bug: have
  the user run `paperpush login <slug>` in a separate terminal after you fix
  it (not with `!` in this session: that gets no keyboard input, so the
  password prompt fails).
- **Installed copy vs source checkout.** Run `python -c "import paperpush, os;
  print(os.path.dirname(paperpush.__file__))"`. Edit runner code only when that
  path is inside this git checkout (an editable install). For a pip install,
  fix only the live page, and hand the changes to the user as a recovery
  report.

## 1. Reproduce once, paused instead of failed

**Handed a run that is already paused** (from `paperpush-submit`, with a
`pid`): do not start a new one. Call `submit_status(pid)` to read
`recovery.paused` and go straight to step 2. Once it reaches the review
page, wait for the author: they review and send it themselves.

Find the `.sub`, the venue, and where the run stopped (the user's output, or
the last `step(...)` label in the runner). Read the runner around that step
before you open anything.

**With the paperpush MCP tools (preferred):** run
`submit(subfile=<abs>, recover=True)`, then poll `submit_status(pid)` until
`recovery.paused` appears. It reports the step, the failed action, the
error, and any `earlier_fix`.

**From the shell:**
```
paperpush submit <abs .sub> --recover agent --recover-dir <scratch>/recover --cdp-port 0
```
Or use `--recover by-hand` so the user can fix the page in the terminal, or
`--debug` to step through in the Playwright Inspector. Start one run at a
time, and resume it rather than restarting from the beginning.

## 2. Look, then act on the live page

1. `submit_page(pid)`: get the accessibility snapshot and a screenshot. Read
   both, and compare the controls you see with what the runner's selector
   expected.
2. Find the cause. Typical ones: a label changed (selector), a new required
   question, a control inside an iframe or a new tab, a modal or cookie banner
   covering the page, an upload still processing (needs a longer bounded
   wait), or a value the portal rejects.
3. `submit_act(pid, action, target, value)`: do the smallest action that
   leaves the page as the failed step should have left it. Target controls by
   role and name where you can (`{"role": "button", "name": "Next"}`).
4. `submit_resume(pid, "resolved" | "retry", note)`. In `note`, give the old
   selector or behavior, the new one, and what you did. The note goes into
   `recoveries.jsonl` and is how the fix gets into the runner.

Repeat for each pause until the run reaches the review page. For a
long-running fix (a PDF build, a slow upload), use bounded waits, tell the user
what you are waiting for, and stop after the wait limit instead of
retrying forever. If MCP is not available, attach to the CDP endpoint with
`playwright-cli attach --cdp=<url>` (or Playwright `connect_over_cdp`) only to
**look** and to do the same small actions; the boundary above still holds.

## 3. Keep the change log

While you work, keep `<scratch>/runner_changes.md` up to date, one entry per
fix:

```
## <step label>: <one-line symptom>
- runner: paperpush/venues/<portal>/<slug>.py:<line>  (<function / constant>)
- failed: <action + selector the script used>  ->  <error>
- live page: <what the control is now: role, name, frame, timing>
- live fix: <submit_act calls that worked>
- script change: <exact replacement code, e.g. NEXT_BUTTON = "Continue">
- test: <regression test that pins it, or "none: why">
```

This log is required even when you cannot edit the code. It is what you hand
over.

## 4. Put the fixes in the runner (source checkout only)

1. Make each change from the log. Change the named selector constants at the
   top of the runner rather than inline strings. Keep probes that are
   expected to miss inside `with unrecovered():`. Keep each upload click and
   its file chooser in one `page.attempt(...)`.
2. For each change, add a focused offline regression test to
   `tests/test_<slug>.py`, on a `Mock` page as in
   `tests/test_nature_machine_intelligence.py`. If a new portal question
   needs a value, add the field through a venue file (`paperpush add-venue
   --template --from <slug>`), and add it to the sample in
   `tests/sample_subfiles.json`.
3. Run `pytest tests/test_<slug>.py tests/test_recovery_coverage.py
   tests/test_venue_interface.py -q`, then rerun the live path once to the
   review page **without recovery** (`paperpush submit <sub>` or
   `pytest tests/test_submit.py --run-portal -s --venue <slug>`). The fix is
   verified only if this run gets through without a pause.

On a pip install, skip this step. Once the author is done, call
`recovery_report(pid)` (CLI: `paperpush recovery-report <recover dir>`), show
them the redacted draft and the change log, and give them the `issue_url` so
they can file it themselves.

## 5. Finish

When the author says they are finished with the browser, call
`submit_close(pid)`. Then report briefly:
- the cause of each stall and the fix, with links to the change log entries;
- whether the runner was changed and the test results (offline, plus the
  live rerun without recovery);
- the last page you saw, and anything still blocked by the portal (downtime,
  CAPTCHA, account access, slow conversion), stated as not seen working.
