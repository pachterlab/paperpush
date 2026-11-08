---
name: paperpush-submit
description: >-
  Fill in a venue's submission portal from a paperpush .sub file: confirm the
  stored login still signs in, then run `paperpush submit`, which walks the
  portal wizard and stops before the final submit button for the author to
  review and send. Hands a run that fails partway to paperpush-portal-debug.
  Handles a request to skip validation. Triggers: "submit my sub", "fill out the submission portal for
  <venue>", "fill out submission portal to nature. skip validation", "push
  this .sub to the portal", "log in and submit".
---

# paperpush submit (Claude skill)

Takes a `.sub` file to the venue's portal: checks the login, then runs
`paperpush submit`, which signs in, clicks through the wizard typing in the
values from the `.sub`, and **stops before the final submit button**. The
author reviews the filled form in the portal and sends it themselves.

`paperpush-prepare-submission` calls this skill as its last step, after
autofill and validate. Called directly, it does not autofill or run the
AI validation checks; it only submits.

## Inputs you need

1. **The `.sub` file.** Resolve it in this order:
   1. a `.sub` path the user gave;
   2. else, if they gave a manuscript directory, `<manuscript-dir>/<venue>.sub.txt`;
   3. else a single `<venue>.sub.txt` in the current working directory.

   If none exists, or there are several candidates, ask in plain text for
   the path. Do not scan the repository for one or fall back to test fixtures.
   If the user has a manuscript directory but no `.sub` yet, there is nothing
   to submit: hand off to `paperpush-prepare-submission` instead. If they
   asked to skip validation, carry that request over.
2. **The venue.** It is declared in the `.sub` (`venue:` near the top). If
   the user named a different venue, stop and ask which is right.
3. **Whether to skip validation.** Skip it only when the user says so: "skip
   validation", "don't validate", "submit anyway", "ignore the errors", or
   similar. Otherwise validation runs.

Use absolute paths throughout.

## Steps

### 0. Check the venue can be submitted to

Validate-only venues have no runner, and `submit` refuses them. Check first:

```
paperpush venues
```

or `describe_venue(<venue>)` over MCP. If the venue is validate-only, tell the
user that paperpush can check the manuscript against the venue's guidelines but
cannot fill in its portal. Give them the `submission_url` and stop.

### 0b. Check it is an initial submission

`paperpush submit` opens a **new** submission. A revision goes through the
venue's own "submit revision" link for the existing manuscript; sent through
`submit`, it becomes a duplicate submission. Before going on, look for signs
that the `.sub` is a revision:

- the `.sub` or a file it names sits in a `revision<N>/` directory (as made by
  `paperpush-respond-to-reviewers`);
- the `.sub` names, or sits beside, a response-to-reviewers document
  (`response_to_reviewers.*`, `rebuttal.*`, `reply to reviewers`, ...);
- the user calls it a revision or resubmission after review.

If any applies, **warn the user and ask** whether they really want a new
submission. Tell them a revision is uploaded through the venue's "submit
revision" link, and that `paperpush-respond-to-reviewers` prepares the files.
Go on only if they say yes; then pass `confirm_new_submission=true` to the
MCP `submit` tool (or `--yes` to the CLI). The tool and the CLI make the same
check: MCP returns `confirmation_required` with the `signals`, and the CLI
asks in the terminal (or stops, without a terminal). Never set
`confirm_new_submission` / `--yes` without the user's answer.

### 1. Check the login

```
paperpush login <venue> --check
```

This signs in headless with the stored credentials, so it also catches a
password changed since they logged in. Over MCP, call
`login_status(venue=<venue>, check=true)` instead.

- Exit 0, or `check.status` `ok`, means the login works. A note that the
  venue "could not be checked", or `check.status` `unchecked`, also counts as a
  pass. Some venues need no login (`login_status` says so); go on to step 2.
- Exit 1, or `failed` / `timed_out`, means nothing is stored or the sign-in
  failed. **Stop and ask the user** to run, in their own terminal,

  ```
  ! paperpush login <venue>
  ```

  This opens a browser and may need their password, ORCID, CAPTCHA, or
  two-factor code. In the same message, tell them:
  - If they have no account on the venue's submission system yet, they must
    register there first. Give them the portal URL: `submission_url` from
    `describe_venue`, or without MCP
    `python -c "from paperpush.database import get_venue; print(get_venue('<venue>').submission_url)"`.
    paperpush cannot create accounts.
  - `paperpush login` tries the sign-in before it saves anything, so a wrong
    username or password is reported right away and nothing is stored. If it
    fails, they should check the details (or reset the password on the
    portal) and run it again.

  When they say they are done, run the check again. Never run
  `paperpush login` yourself, and never ask for a password in the chat.

### 2. Confirm, then submit

Filling in a portal is an outward-facing action. Confirm explicitly with the
user before you start, unless they already asked for it in this request (for
example, "fill out the submission portal to nature"). In either case, say that
the final submit is theirs to click.

**Default: validate first.** Prefer the MCP tool, so you can follow and
recover the run:

```
submit(subfile=<abs .sub>)
```

or the CLI:

```
paperpush submit <abs .sub>
```

(The CLI keeps a headed browser open and does not return until the author is
done, so without MCP have the user run it with `!`.) Both validate the `.sub` first and refuse to open a browser if it has errors
(MCP returns `blocked` with the list). If that happens, report the exact
errors and stop. Offer to run the `paperpush-validate` skill for the full
report, `paperpush-fix` to fix what it can in the files and the `.sub`, or
`paperpush-autofill` to fill missing fields. Do not quietly retry
with validation skipped.

**When the user asked to skip validation**, pass it through:

```
submit(subfile=<abs .sub>, skip_validate=true)
```

Without MCP, have the user run `! paperpush submit <abs .sub> --skip-validate
--recover by-hand` themselves: a by-hand recovery asks questions in the
terminal, which a command you run in the background cannot answer. Older
paperpush MCP servers have no `skip_validate`; fall back to the same command.
Before you start, tell the user plainly:
the portal will be filled with whatever the `.sub` holds, and **an empty
required field or a missing file will most likely break the run partway or
leave the form incomplete**. Skipping validation does not skip the login
check in step 1. A `.sub` with no values at all is not worth sending to a
portal: point that out and suggest autofill first.

### 3. Follow the run and recover from a failed step

**MCP run.** `submit` returns a `pid` as soon as the browser opens. Poll
`submit_status(pid)`:

- It reaches the review page: tell the user the form is filled and waiting
  for them in the browser.
- `recovery.paused`: a step failed and the run is waiting (for 30 minutes).
  Invoke the `paperpush-portal-debug` skill (via the Skill tool) with the
  `pid`, the `.sub`, and the venue. From then on it owns the run, through
  the review page, the change log, the bug report, and `submit_close`. Do not
  fix the page yourself first, and skip step 4 below.
- The run has ended with a non-zero exit code instead of pausing (the
  venue's runner cannot pause, or a pause ran past its 30 minutes): tell the
  user which step it stopped at (from the output tail) and offer to hand it
  to `paperpush-portal-debug`, which starts a fresh run to reproduce it.
- `action_required` (no stored login): go back to step 1.

**CLI run in the user's terminal.** With `--recover by-hand`, a failed step
pauses there. The user fixes the page in the browser and chooses to continue.
If they would rather you fix it, hand off to `paperpush-portal-debug` the same
way.

### 4. Wrap up (runs that did not need portal-debug)

- Summarize what was filled in, and any validation that was skipped.
- Call `submit_close(pid)` only when the user says they are done with the
  browser (they have reviewed it and sent it, or they want to stop).

## Rules

- Never run `paperpush login` yourself or ask for a password.
- Never press, or try to press, the final submit button.
- `submit` is for initial submissions. Warn and ask before submitting a `.sub`
  that looks like a revision (step 0b).
- Skip validation only when the user asks for it, and warn them what that risks.
- Do not autofill, run validation checks, or create a `.sub` here; those
  belong to `paperpush-autofill`, `paperpush-validate`, and
  `paperpush-prepare-submission`.
- Always use absolute paths.
