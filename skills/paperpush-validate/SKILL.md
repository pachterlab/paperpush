---
name: paperpush-validate
description: >-
  Run paperpush's full pre-submission validation on a .sub file, including the
  checks that need a reader's judgment (lettering in raster figures, panel
  labels, whether each figure and panel shows what its legend and the text
  say, the venue's free-text guideline rules, whether every .sub field says
  what the manuscript says), which you do yourself instead of through an API
  key. Produces the color-coded report of what passed, failed, and was not
  checked, optionally saved as PDF/HTML/Markdown/text. For a venue paperpush
  does not support yet, first builds a temporary entry from the venue's
  guidelines and validates against that. Triggers: "validate my
  sub", "check my submission against the guidelines", "is my manuscript ready
  for <venue>", "run the AI checks for validate".
---

# paperpush validate (Claude skill)

`paperpush validate` runs every deterministic check itself: form fields, files,
links, DOIs, registry searches for references without a DOI and for
preprints that have since been published, the `.bib` file's structure,
sensitive data, hidden text, the venue's author guidelines, page
layout (margins, font, size, spacing, line/page numbers, template), figures
(resolution, lettering size and typeface in vector figures, panel labels),
figure/table citation order, whether the title, abstract, authors,
keywords, and statements in the `.sub` match the manuscript, and mechanical
prose checks (double spaces, repeated words, punctuation spacing, unbalanced
brackets and quotes, common misspellings, numbers, abbreviations, acronyms).

Some checks need someone to *look* at the files. Without an API key they come
back as **needs AI**. You are that reader: the CLI writes those checks out as
tasks, you do them, and the CLI merges your verdicts into the report. No
`--ai` flag or API key is involved.

## Inputs

- The `.sub` file (absolute path). If the user did not give one, look for a
  `<venue>.sub.txt` in the manuscript directory they named. If there is none,
  don't make one just to validate: name the files instead, e.g.
  `paperpush validate --venue <venue> --manuscript <abs> --title-page <abs>
  --cover-letter <abs> --figures <abs> ... --supplement <abs>` (MCP: the same
  arguments to `validate_subfile` / `validation_ai_tasks`). Without a venue,
  leave `--venue` out: only the checks that apply to any manuscript run (links,
  references, hidden text, citation order, prose, ...). The form checks come
  back not applicable, so the report is about the files, not whether the
  submission is ready. Re-run with the same flags to merge `--ai-results`.
- Whether they want the report saved, and where (default: none; `-o` with no
  path writes `<sub stem>_validation.pdf` next to the `.sub`). Mention
  `--colorblind` if they ask for color-blind-friendly colors.
- Whether this is an initial submission, if the venue accepts an initial
  submission in any format (the report's "Format for initial submission" note;
  `describe_venue` shows `manuscript.initial_submission_any_format`). If so,
  add `--initial-submission` (MCP: `initial_submission=true`) so errors are
  reported as warnings.
- Whether they want the figures **reproduced** (rerun the authors' code and
  compare each figure with the submitted one). Only when they ask for it: add
  `--reproduce-figures` (MCP: `reproduce_figures=true`) to every command
  below, including the one that merges `--ai-results`. It runs the authors'
  code on this machine and can take `--reproduce-minutes` (default 30) per
  figure, so confirm before starting if they did not ask in so many words.

## Steps

0. **Venue paperpush does not support yet?** If the user names a venue
   (`--venue`, or the venue they want a `.sub` for) that `paperpush venues`
   does not list (try its name, abbreviation, and publisher's title), don't
   stop there and don't fall back to venue-free checks silently. Tell the
   user it is not supported, then build a **temporary** entry for it and
   validate against that:

   1. Run the `paperpush-add-venue-validate` skill (Skill tool) for the venue
      in its **temporary mode**: it reads the venue's guidelines and writes
      the entry into a draft data directory,
      `<scratch>/venue_drafts/<slug>/`, instead of paperpush's own databases.
      This works from a pip install too: no repo clone is needed.
   2. Run every `paperpush` command below (and `paperpush subfile <slug>` if
      a `.sub` is needed) with `PAPERPUSH_VENUE_DATA=<scratch>/venue_drafts/<slug>/data`
      set, so paperpush reads the draft entry. Without it the venue is
      unknown.
   3. In the summary (step 4), say the rules came from a temporary entry
      that was read from the venue's guidelines just now, so they are only
      as good as that reading. Point the user to the draft files they can
      hot-fix:
      - `<scratch>/venue_drafts/<slug>/<slug>.txt`: the venue file, the
        easy one to edit (every key is commented);
      - `<scratch>/venue_drafts/<slug>/data/manuscript_requirements.json`
        and `submission_forms.json`: the JSON entries validate reads.

      After editing the venue file, they (or you) rebuild the JSON and
      re-validate:
      ```
      paperpush add-venue <scratch>/venue_drafts/<slug>/<slug>.txt --data-dir <scratch>/venue_drafts/<slug>/data --overwrite
      PAPERPUSH_VENUE_DATA=<scratch>/venue_drafts/<slug>/data paperpush validate ...
      ```
      Say the draft is temporary: it lives in a scratch directory, so copy
      it somewhere if they want to keep it.
   4. End with the add-venue-validate skill's **share it** suggestion: send
      the entry upstream as a pull request so the venue is supported for
      everyone.

1. **Run the deterministic checks and export the AI tasks.**

   ```
   paperpush validate <abs .sub> --ai-tasks <scratch>/validate_tasks.json
   ```

   Add `--disable-integrity.links --disable-references` only if the user wants it
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
   - `ai.unverified_references` lists references that Crossref, OpenAlex,
     and arXiv could not confirm. Look each one up rather than judging from
     memory: search Crossref (`https://api.crossref.org/works?query.bibliographic=<title author year>`),
     the publisher's or arXiv's page, or the web, and compare title, authors,
     and year. `fail` names each reference that appears not to exist or that
     describes a different work, with the correct details where you found
     them; a book or web page the registries miss is not fabricated. If you
     cannot search, answer `unclear`.
   - `ai.figures.content_matches_text` lists each figure's legend and the
     sentences that cite it, tagged with the panels they cite. Look at every
     figure and every named panel and check that it shows what is said about
     it: the right kind of plot or image, variables, groups, and panel count,
     and the claimed trend or difference actually visible. `fail` names the
     figure and panel and the mismatch (e.g. the text cites Fig. 2b for the
     survival curve, which is panel c; the file attached as Figure 2 is the
     one Figure 3's legend describes). Do not fail it for style or lettering;
     other checks cover those.
   - `ai.figures.design` lists the design rules to check in every figure and
     panel: axis and color-bar labels with units, tick labels kept (images
     get a scale bar), bar charts from zero and no unexplained zoomed axes,
     no pie charts, paired bar charts as one scatter, the same color for the
     same thing across panels and figures, and a second channel (marker,
     line style, direct label) where color separates series. `fail` names
     the figure and panel, the problem, and the fix.
   - `ai.prose` is a proofread of the whole manuscript: read all of it, not a
     sample. List real errors (typos, grammar, tense that breaks the paper's
     own convention) with a quote and the correction; style preferences and
     defensible choices are not failures.
   - `ai.rigor.figure_provenance` (queued when the manuscript links or
     attaches code): clone the repository into the scratch directory, at the
     release or commit the paper cites, and read it without running anything.
     Put the panel-to-code mapping in `details`, one line per panel; it is
     what the reproduction tasks start from, so do this task first.
   - `ai.figure_reproduction.<n>` (only with `--reproduce-figures`): work in
     `<scratch>/reproduce/`, never in the manuscript directory. Build one
     isolated environment per repository from its own dependency files (a
     venv, conda env, renv library, or its Dockerfile) and reuse it across
     figures; never install into the user's global environment or add
     packages the repository does not declare without saying so. Run each
     figure's code with a timeout (`timeout`/`gtimeout`, or the Bash tool's
     own) inside the budget the task states, and stop a download that would
     exceed it. Then look at the output and the submitted figure side by
     side. Start `details` with `reproduced`, `differs`, `failed`, or
     `not attempted`, and map them to `pass`, `fail`, `fail`, and `unclear`
     as the task says: a figure you could not try (controlled-access data,
     over budget, no GPU) is `unclear`, never `fail`. Note what you had to
     change to get it running; a hard-coded path or an undeclared dependency
     is a finding even when the figure then reproduces. Remove the scratch
     environments at the end unless the user wants them kept.

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
   paperpush validate <abs .sub> --ai-results <scratch>/validate_results.json [-o <report path> [--overwrite]] [--colorblind] [--initial-submission]
   ```

   (MCP alternative: `validate_subfile(subfile, ai_results=[...], output=...)`.)
   Your verdicts appear marked "(AI)". A failed AI check is a warning, never a
   blocking error.

   If the user asks for the AI checks only, add `--ai-only` to both commands
   (MCP: `ai_only=true`): the report then lists just the AI checks and gives
   no ready-to-submit verdict. Leave it off otherwise, so the summary covers
   the deterministic checks too.

4. **Summarize for the user**, in this order: blocking errors (must fix),
   warnings (deterministic first, then AI), what could not be checked and why
   (could not run / not implemented), and where the saved report is. Relay
   heuristics as things to confirm, not as certainties.

## Rules

- Never edit the user's manuscript, figures, or `.sub` to make a check pass.
  Report; let them decide. (Fixing a typo in the `.sub` is fine if they ask.)
  If they ask you to fix what validate found, use `paperpush-fix`.
- Never add hidden text or reviewer instructions anywhere.
- Use absolute paths; keep the task and result JSON in a scratch directory, not
  the manuscript directory.
- Run the authors' code only for `ai.figure_reproduction.*` tasks, only in
  the scratch directory, and with no credentials (no tokens, keys, or signed-in
  sessions passed to it).
- A temporary venue entry (step 0) never goes into paperpush's own databases
  or the repo: always `--data-dir` plus `PAPERPUSH_VENUE_DATA`.
- If the user has an API key and prefers it, `paperpush validate <sub> --ai`
  does steps 1–3 in one call through the provider's API instead.
