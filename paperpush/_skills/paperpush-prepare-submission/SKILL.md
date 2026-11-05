---
name: paperpush-prepare-submission
description: >-
  End-to-end paperpush submission flow from a manuscript directory: resolve
  the .sub (user-provided, else an existing one in the manuscript directory, else
  created on the fly), autofill it, validate, then log in and submit. Use for a
  general "prepare/submit my manuscript" request. Triggers: "prepare my
  manuscript for submission to <venue>", "submit my manuscript to <venue>",
  "get my paper ready to submit with paperpush".
---

# paperpush prepare & submit (Claude skill)

Orchestrates the full submission path for a manuscript directory when the user
has **not** named a `.sub` file. It generates the `.sub`, hands off to the
`paperpush-autofill` skill to fill it, validates, and then (with the user's
go-ahead) logs in and submits. This skill only sequences the steps; each
underlying `paperpush` command does the real work.

## Inputs you need

1. **The manuscript directory** — required. If the user did not give it, ask in
   plain text (a path is free-text; do not use a multiple-choice prompt). Never
   guess it, scan the repository for candidate folders, or fall back to test
   fixtures.
2. **The venue slug** (e.g. `bioinformatics`). If the user did not say it, ask
   or infer it from their message. Confirm it is supported with
   `paperpush venues`.

Do **not** ask for a `.sub` path. Resolve it by this hierarchy: (1) an explicit
`.sub` the user provided; (2) else an existing `<venue>.sub` in the manuscript
directory; (3) else create `<venue>.sub` in the manuscript directory on the
fly. See step 1.

## Steps

0. **Check the login first, before any other work.** Run:

   ```
   paperpush login --list
   ```

   If the venue is not listed (or the output is `Not logged in to any venues.`),
   tell the user immediately — in your next message, not at the end — to run

   ```
   paperpush login <venue>
   ```

   themselves in a terminal, since it opens a browser and may need their
   password, ORCID, CAPTCHA, or two-factor code. In the same message, tell them:
   - If they have no account on the venue's submission system yet, they must
     register there first. Give them the portal URL: `submission_url` from
     the `describe_venue` MCP tool, or without MCP
     `python -c "from paperpush.database import get_venue; print(get_venue('<venue>').submission_url)"`.
     paperpush cannot create accounts.
   - `paperpush login` signs in with what they type before saving it, so a
     wrong username or password is reported then and nothing is stored. If it
     fails, they should check the details (or reset the password on the portal)
     and run it again.

   Then **carry on with steps 1-3 anyway** while they do it; a missing login
   only blocks step 5. Never ask for their password in the chat.

1. **Resolve the `.sub` by hierarchy** (user-provided > in manuscript dir >
   create on the fly):
   - If the user named a `.sub` path, use it as-is.
   - Else if `<manuscript-dir>/<venue>.sub` already exists, reuse it (do not
     overwrite unless the user asks to regenerate).
   - Else create it in the manuscript directory, with an absolute path:

     ```
     paperpush subfile <venue> -o <manuscript-dir>/<venue>.sub
     ```

   A newly created `.sub` lives alongside the manuscript. Use the resolved path
   for every later step.

2. **Autofill the `.sub`.** Invoke the `paperpush-autofill` skill (via the
   Skill tool), passing the `.sub` path from step 1 and the manuscript
   directory. That skill reads the files, extracts field values, and applies
   them through `paperpush autofill`. Let it run to completion and relay its
   summary of what was filled and what it left for the user.

3. **Validate.** Run the pre-submission checks:

   ```
   paperpush validate <resolved-sub-path>
   ```

   The report lists checks that **need AI** (lettering in raster figures, the
   guidelines' free-text rules, whether each field matches the manuscript).
   Run them by invoking the `paperpush-validate` skill (via the Skill tool) on
   the same `.sub`; it does them itself, with no API key, and merges the
   verdicts into the report.

   If validation reports errors, **stop here.** Report the exact errors and the
   review-flagged / `never` fields the autofill skill left for the user, and do
   not proceed to login or submit until they are resolved. A file with
   validation errors will not submit. Relay warnings (including failed AI
   checks) as things for the user to confirm.

4. **Confirm the login still works.** Run:

   ```
   paperpush login <venue> --check
   ```

   This signs in headless with the stored credentials, so it also catches a
   password changed since they logged in (`--list` and `--status` only show
   that something is stored). Exit 0 means go on to step 5; a printed note
   that the venue "could not be checked" also counts as a pass. Exit 1 means
   nothing is stored or the sign-in failed: stop and wait, and repeat the
   step-0 ask (including the registration note) that the user run
   `paperpush login <venue>` in their terminal. Over MCP, call
   `login_status(venue=..., check=true)` instead: `check.status` `ok` or
   `unchecked` is a pass; `failed` or `timed_out` means ask for the login. If `--check` fails only
   because the venue shows a CAPTCHA or two-factor prompt, that interactive
   login is also the fix. Logging in is theirs to drive; do not run it for
   them.

5. **Submit (only after the user confirms).** Submitting is outward-facing.
   Confirm explicitly first, then run:

   ```
   paperpush submit <resolved-sub-path>
   ```

   This opens a headed browser and walks the portal wizard, stopping before the
   final click so the user reviews everything in the submission system. Make
   clear that the final submit is theirs to click.

   If the paperpush MCP tools are connected, use the `submit` tool instead. A
   step that fails pauses the run
   rather than ending it. `submit_status` then shows `recovery.paused`. Look at
   the page with `submit_page`, fix it with `submit_act`, and continue with
   `submit_resume` (put what had changed in `note`). Tell the user what you
   fixed. Never try to send the submission; it is refused anyway.

   Fix a failing step on the live page, never by editing paperpush's installed
   code. If steps needed recovery, then once the user is done, offer them the
   redacted bug report: `recovery_report(pid)` (CLI: `paperpush
   recovery-report <dir>`). Show them the draft and give them the issue link to
   file it themselves if they want the venue fixed for everyone.

## Rules

- **Always use absolute paths** for the `.sub` file and the manuscript directory.
- Never infer the manuscript directory; ask (in plain text) if it is missing.
- Generate the `.sub` in the manuscript directory, not the working directory.
- Check `paperpush login --list` up front and surface a missing login
  immediately (with the register-first note), so the user can log in while
  you do the rest of the work. Re-check with `paperpush login <venue> --check`
  before submitting.
- Never run `paperpush login` yourself — it is interactive and belongs in the
  user's terminal.
- Do not run `submit` without explicit user confirmation, and never past a
  failed `validate`.
- Delegate all field extraction to `paperpush-autofill`; do not duplicate its
  logic here.
