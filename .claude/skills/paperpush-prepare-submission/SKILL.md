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
   `paperpush --venues`.

Do **not** ask for a `.sub` path. Resolve it by this hierarchy: (1) an explicit
`.sub` the user provided; (2) else an existing `<venue>.sub` in the manuscript
directory; (3) else create `<venue>.sub` in the manuscript directory on the
fly. See step 1.

## Steps

1. **Resolve the `.sub` by hierarchy** (user-provided > in manuscript dir >
   create on the fly):
   - If the user named a `.sub` path, use it as-is.
   - Else if `<manuscript-dir>/<venue>.sub` already exists, reuse it (do not
     overwrite unless the user asks to regenerate).
   - Else create it in the manuscript directory, with an absolute path:

     ```
     paperpush subfile <venue> --fill-defaults -o <manuscript-dir>/<venue>.sub
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

   If validation reports errors, **stop here.** Report the exact errors and the
   review-flagged / `never` fields the autofill skill left for the user, and do
   not proceed to login or submit until they are resolved. A file with
   validation errors will not submit.

4. **Log in (only after the user confirms).** Logging in opens a browser and may
   require the user's password, ORCID, CAPTCHA, or two-factor prompt, so confirm
   before running it and let the user drive the browser:

   ```
   paperpush login <venue>
   ```

   You can check existing state first with `paperpush login <venue>
   --status`; skip the login if credentials are already stored and valid.

5. **Submit (only after the user confirms).** Submitting is outward-facing.
   Confirm explicitly first, then run:

   ```
   paperpush submit <resolved-sub-path>
   ```

   This opens a headed browser and walks the portal wizard, stopping before the
   final click so the user reviews everything in the submission system. Make
   clear that the final submit is theirs to click.

## Rules

- **Always use absolute paths** for the `.sub` file and the manuscript directory.
- Never infer the manuscript directory; ask (in plain text) if it is missing.
- Generate the `.sub` in the manuscript directory, not the working directory.
- Do not run `login` or `submit` without explicit user confirmation, and never
  past a failed `validate`.
- Delegate all field extraction to `paperpush-autofill`; do not duplicate its
  logic here.
