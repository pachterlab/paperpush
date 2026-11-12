---
name: paperpush-prepare-submission
description: >-
  End-to-end paperpush submission flow from a manuscript directory: resolve
  the .sub (user-provided, else an existing one in the manuscript directory, else
  created on the fly), autofill it, validate (offering paperpush-fix when
  validate finds errors or more than 5 warnings), then hand off to
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
2. **The venue slug** (e.g. `bioinformatics`). If the user did not say it,
   infer it from their message; if they haven't chosen one, offer to run the
   `paperpush-pick-venue` skill (Skill tool) for the manuscript and continue
   here with the venue they pick. Look it up with `paperpush venues` (try its
   name, abbreviation, and publisher's title). If it is not listed, don't stop:
   see [Venue paperpush does not support](#venue-paperpush-does-not-support).

**Initial submissions only.** If the manuscript directory is a `revision<N>/`
directory or holds a response-to-reviewers document (`response_to_reviewers.*`,
`rebuttal.*`, ...), or the user says it is a revision after review, warn them
before any other work: paperpush submits new manuscripts, and a revision is
uploaded through the venue's own "submit revision" link. Offer
`paperpush-respond-to-reviewers` to prepare the revision, and continue here
only if they confirm they want a new submission (then pass that confirmation
on to `paperpush-submit`).

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
   `submit` cannot fill their portal) or a venue paperpush does not support.
   Tell the user that up front, and stop after step 3.

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
   summary of what was filled and what it left for the user. If the user asked
   you to confirm the attestations (consent, submission terms), tell the
   autofill skill to add `--confirm-attestations`. Don't add it otherwise.

3. **Validate.** Run the pre-submission checks:

   ```
   paperpush validate <resolved-sub-path>
   ```

   The report lists checks that **need AI** (lettering in raster figures,
   whether each figure and panel shows what its legend and the text say, the
   guidelines' free-text rules, whether each field matches the manuscript).
   Run them by invoking the `paperpush-validate` skill (via the Skill tool) on
   the same `.sub`; it does them itself, with no API key, and merges the
   verdicts into the report.

   Then decide whether to go on, from the counts in the merged report (failed
   AI checks count as warnings):

   - **Errors, or more than 5 warnings: stop here.** Summarize them in three
     groups: must fix (errors), worth checking (warnings, deterministic first,
     then AI), and could not be checked. Add the review-flagged / `never`
     fields the autofill skill left for the user. Then ask whether to run the
     `paperpush-fix` skill on the `.sub`: it backs up the files, fixes what it
     safely can in the manuscript sources and the `.sub`, validates again, and
     reports what needs the user. Ask rather than run it, because it edits the
     manuscript. If they say yes, invoke `paperpush-fix` (via the Skill tool)
     and relay its report, then apply this same rule to its final validation.
     If they say no, leave the fixes to them. Either way, do not go on to step
     4 while any error remains: a file with validation errors will not submit.
     With no errors left and more than 5 warnings, go on only if the user says
     to continue anyway.
   - **No errors and 5 warnings or fewer:** list the warnings as things for
     the user to confirm, and ask whether to continue to the portal anyway
     before step 4. With no warnings at all, go straight on.

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

## Venue paperpush does not support

A venue `paperpush venues` does not list can still be prepared and checked, but
not submitted. Tell the user so up front, then:

- **Before step 1**, run the `paperpush-add-venue-validate` skill (Skill tool)
  for the venue in its **temporary mode**. It reads the venue's guidelines and
  writes a draft entry to `<scratch>/venue_drafts/<slug>/data`, outside
  paperpush's own databases.
- Run steps 1-3 with `PAPERPUSH_VENUE_DATA=<scratch>/venue_drafts/<slug>/data`
  set on every `paperpush` command (`subfile`, `autofill`, `validate`), and
  give that path to `paperpush-autofill` and `paperpush-validate` when you
  invoke them. Tell `paperpush-validate` the draft entry already exists so it
  does not build a second one. Without the variable, paperpush does not know
  the venue.
- Skip step 0 (no login) and **stop after step 3: never run step 4 or
  `paperpush-submit`.** paperpush has no runner for this venue's portal. Tell
  the user to upload the files through the venue's own portal, using the
  validated `.sub` as their checklist.
- In the summary, say the rules came from a temporary entry read from the
  guidelines just now, and pass on `paperpush-validate`'s pointers to the draft
  files and its suggestion to send the entry upstream as a pull request.

## Rules

- **Always use absolute paths** for the `.sub` file and the manuscript directory.
- Never infer the manuscript directory; ask (in plain text) if it is missing.
- Generate the `.sub` in the manuscript directory, not the working directory.
- Check `paperpush login --list` up front and surface a missing login
  immediately (with the register-first note), so the user can log in while
  you do the rest of the work.
- Never run `paperpush login` yourself — it is interactive and belongs in the
  user's terminal.
- Never run `paperpush-submit` for a validate-only venue or a venue paperpush
  does not support.
- Never go on to `paperpush-submit` past a failed `validate`, or past warnings
  the user has not seen and agreed to continue with, unless the user asked to
  skip validation.
- Offer `paperpush-fix` before running it; never edit the manuscript without
  the user's go-ahead.
- Delegate field extraction to `paperpush-autofill`, the reader checks to
  `paperpush-validate`, fixing to `paperpush-fix`, and login + submit to
  `paperpush-submit`; do not duplicate their logic here.
