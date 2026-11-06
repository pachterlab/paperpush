---
name: paperpush-prepare-submission
description: >-
  End-to-end paperpush submission flow from a manuscript directory: resolve
  the .sub (user-provided, else an existing one in the manuscript directory, else
  created on the fly), autofill it, validate, then hand off to
  paperpush-submit to log in and fill the portal. Use for a
  general "prepare/submit my manuscript" request. Triggers: "prepare my
  manuscript for submission to <venue>", "submit my manuscript to <venue>",
  "get my paper ready to submit with paperpush".
---

# paperpush prepare & submit (Claude skill)

Orchestrates the full submission path for a manuscript directory when the user
has **not** named a `.sub` file. It generates the `.sub`, hands off to the
`paperpush-autofill` skill to fill it, validates (through `paperpush-validate`),
and then hands off to the `paperpush-submit` skill to check the login and fill
in the portal. This skill only sequences the steps; the other skills and the
`paperpush` commands do the real work.

If the user already has a filled `.sub` and only wants it sent to the portal,
use `paperpush-submit` directly instead.

## Inputs you need

1. **The manuscript directory** — required. If the user did not give it, ask in
   plain text (a path is free-text; do not use a multiple-choice prompt). Never
   guess it, scan the repository for candidate folders, or fall back to test
   fixtures.
2. **The venue slug** (e.g. `bioinformatics`). If the user did not say it, ask
   or infer it from their message. Confirm it is supported with
   `paperpush venues`.

Do **not** ask for a `.sub` path. Resolve it by this hierarchy: (1) an explicit
`.sub` the user provided; (2) else an existing `<venue>.sub.txt` in the manuscript
directory; (3) else create `<venue>.sub.txt` in the manuscript directory on the
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
   only blocks step 4. Never ask for their password in the chat.

   Skip this step for a validate-only venue (`paperpush venues` marks them;
   `submit` cannot fill their portal). Tell the user that up front, and stop
   after step 3.

1. **Resolve the `.sub` by hierarchy** (user-provided > in manuscript dir >
   create on the fly):
   - If the user named a `.sub` path, use it as-is.
   - Else if `<manuscript-dir>/<venue>.sub.txt` already exists, reuse it (do not
     overwrite unless the user asks to regenerate).
   - Else create it in the manuscript directory, with an absolute path:

     ```
     paperpush subfile <venue> -o <manuscript-dir>/<venue>.sub.txt
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
   not go on to step 4 until they are resolved. A file with validation errors
   will not submit. Relay warnings (including failed AI checks) as things for
   the user to confirm.

   **If the user asked to skip validation** ("skip validation", "submit
   anyway"), do not run this step. Go straight to step 4 and pass the request
   on.

4. **Log in and fill the portal.** Invoke the `paperpush-submit` skill (via
   the Skill tool) with the resolved `.sub` path, and say whether the user
   asked to skip validation. It re-checks the login with
   `paperpush login <venue> --check`, confirms with the user, runs `submit`,
   and follows the run, handing a failed step to `paperpush-portal-debug`.
   Relay its summary. The final submit is the
   user's to click.

## Rules

- **Always use absolute paths** for the `.sub` file and the manuscript directory.
- Never infer the manuscript directory; ask (in plain text) if it is missing.
- Generate the `.sub` in the manuscript directory, not the working directory.
- Check `paperpush login --list` up front and surface a missing login
  immediately (with the register-first note), so the user can log in while
  you do the rest of the work.
- Never run `paperpush login` yourself — it is interactive and belongs in the
  user's terminal.
- Never go on to `paperpush-submit` past a failed `validate`, unless the user
  asked to skip validation.
- Delegate field extraction to `paperpush-autofill`, the reader checks to
  `paperpush-validate`, and login + submit to `paperpush-submit`; do not
  duplicate their logic here.
