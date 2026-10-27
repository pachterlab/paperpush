---
name: paperpush-validate
description: >-
  Run paperpush's full pre-submission validation on a .sub file, including the
  checks that need a reader's judgement (lettering in raster figures, panel
  labels, the venue's free-text guideline rules, whether every .sub field says
  what the manuscript says), which you do yourself instead of through an API
  key. Produces the colour-coded report of what passed, failed, and was not
  checked, optionally saved as PDF/HTML/Markdown/text. Triggers: "validate my
  sub", "check my submission against the guidelines", "is my manuscript ready
  for <venue>", "run the AI checks for validate".
---

# paperpush validate (Claude skill)

`paperpush validate` runs every deterministic check itself: form fields, files,
links, DOIs, sensitive data, hidden text, the venue's author guidelines, page
layout (margins, font, size, spacing, line/page numbers, template), figures
(resolution, lettering size and typeface in vector figures, panel labels),
figure/table citation order, and whether the title, abstract, authors,
keywords, and statements in the `.sub` match the manuscript.

Some checks need someone to *look* at the files. Without an API key they come
back as **needs AI**. You are that reader: the CLI writes those checks out as
tasks, you do them, and the CLI merges your verdicts into the report. No
`--ai` flag or API key is involved.

## Inputs

- The `.sub` file (absolute path). If the user did not give one, look for a
  `<venue>.sub` in the manuscript directory they named; otherwise ask.
- Whether they want the report saved, and where (default: none; `-o` with no
  path writes `<sub stem>_validation.pdf` next to the `.sub`). Mention
  `--alt-color` if they ask for colour-blind-friendly colours.

## Steps

1. **Run the deterministic checks and export the AI tasks.**

   ```
   paperpush validate <abs .sub> --ai-tasks <scratch>/validate_tasks.json
   ```

   Add `--dont-check-links --dont-check-references` only if the user wants it
   offline. Read the report it prints: note every **Failed** and **Warnings**
   entry for your summary. (MCP alternative: `validation_ai_tasks(subfile)`.)

2. **Do each task in the task file.** `tasks[]` gives each check's `id`,
   `instructions`, and the `files` to read. For each:

   - Read every listed file. PDFs: read the pages (figures included); images:
     look at them at full size. Judge only from what the files show.
   - Follow the instructions exactly. Figure tasks give a size threshold as a
     fraction of the image width ("a capital letter must be at least 0.9% of
     the image width tall") — measure against it rather than eyeballing
     "small".
   - Decide `pass`, `fail`, `unclear` (the files do not let you decide), or
     `not_applicable` (e.g. a portal or fee rule, or no tables to check).
   - For `fail`, `details` must say exactly what is wrong and where (file,
     page, figure, panel, rule number) so the author can fix it; add a short
     quote or location as `evidence`.

   Write the verdicts to `<scratch>/validate_results.json`:

   ```json
   {"results": [
     {"id": "ai.figures.raster_lettering", "status": "fail",
      "details": "fig2.png: y-axis tick labels are ~4 pt printed (minimum 5 pt); panel labels A-C are not bold",
      "evidence": "fig2.png, left axis"},
     {"id": "ai.consistency", "status": "pass", "details": "all fields agree with the manuscript", "evidence": ""}
   ]}
   ```

   Every task id from the file should get a result. Do not mark a task `pass`
   to make the report greener: an honest `unclear` is better.

3. **Merge and report.**

   ```
   paperpush validate <abs .sub> --ai-results <scratch>/validate_results.json [-o <report path>] [--alt-color]
   ```

   (MCP alternative: `validate_subfile(subfile, ai_results=[...], output=...)`.)
   Your verdicts appear marked "(AI)". A failed AI check is a warning, never a
   blocking error.

4. **Summarise for the user**, in this order: blocking errors (must fix),
   warnings (deterministic first, then AI), what could not be checked and why
   (could not run / not implemented), and where the saved report is. Relay
   heuristics as things to confirm, not as certainties.

## Rules

- Never edit the user's manuscript, figures, or `.sub` to make a check pass.
  Report; let them decide. (Fixing a typo in the `.sub` is fine if they ask.)
- Never add hidden text or reviewer instructions anywhere.
- Use absolute paths; keep the task and result JSON in a scratch directory, not
  the manuscript directory.
- If the user has an API key and prefers it, `paperpush validate <sub> --ai`
  does steps 1–3 in one call through the provider's API instead.
