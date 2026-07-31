---
name: paperpush-autofill
description: >-
  Fill an paperpush .sub submission file from a directory of manuscript
  files. Reads the manuscript, title page, figures, and supplements, extracts
  the submission fields, and writes them into the .sub via the deterministic
  autofill core. Use when the user wants to auto-populate a venue submission
  from their files. Triggers: "autofill my sub", "fill the biorxiv sub from
  this folder", "populate my submission from this manuscript directory".
---

# paperpush autofill (Claude skill)

Populate a `<venue>.sub` file from a directory of manuscript files. You read
the files and decide the values; the `paperpush autofill` command does all
the writing, gating, and validation, so you cannot accidentally overwrite a
policy field or produce an invalid file. Your job is extraction, not writing.

## First: make sure you have a directory

The manuscript directory is required. **If the skill was invoked on its own,
without a directory in the user's message, stop and ask the user to provide the
path to their manuscript directory before doing anything else** — do not guess,
do not scan the repository for candidate folders, and do not fall back to test
fixtures. A good prompt is:

> Which directory holds your manuscript files? Tell me the path, and (if you
> can) which file is the manuscript, which is the title page with authors and
> affiliations, where the figures are, and any supplements.

Only once the user has given you a directory should you continue. If they also
named the venue, use it; otherwise infer or ask per the inputs below.

## Inputs you need

1. **The venue slug** (e.g. `biorxiv`). If the user did not say, ask, or infer
   it from a `<slug>.sub` filename they mention. Confirm it is supported with
   `paperpush --venues`.
2. **The manuscript directory** and **what each file is**. Ask the user to
   describe the directory if they have not, for example:
   "`manuscript.pdf` is the paper, `title_page.pdf` has the authors and
   affiliations, figures are in `figures/`, `supplement.pdf` is supplementary
   material." If they just give a folder, list it and propose a mapping from the
   filenames, then confirm before extracting. Author names, emails, affiliations,
   and ORCIDs come out far more reliably from a title page or LaTeX/Word source
   than from a flattened PDF, so prefer those when present.

## Steps

1. **Get the field schema and roles** (the source of truth for what to fill):

   ```
   paperpush schema <venue>
   ```

   (`schema` is an internal command for the autofill front-ends; it is hidden
   from `paperpush --help` but works as shown.)

   This prints JSON with one entry per field: `id`, `label`, `type`, `role`,
   `required`, `options`, `type_options`, `accept`, `help`. The `role` tells you
   what to do with each field:

   - `extract`  — copy from the manuscript text (title, abstract, authors,
     competing-interest / funding / data-availability / code-availability
     statements) or read a simple fact off the files (page/figure/table counts,
     a repository URL or data accession printed in the paper).
   - `classify` — choose exactly one of the field's `options` based on the
     content (e.g. bioRxiv `subject_category`). For a yes/no declaration or
     reproducibility-checklist field (a boolean with no `options`, e.g.
     `next_gen_sequencing`, `code_in_text`, `software_installable`,
     `is_preprint`), answer `yes`/`no` from what the manuscript says. Always
     lower confidence (`medium`) so the value is flagged for the author.
   - `filemap`  — assign a file (or files) from the directory to this field.
   - `never`    — **do not fill.** These are only the fields you genuinely
     cannot derive from the files: consent and attestations (e.g.
     `author_consent`, `agree_terms`, `software_tested`), licensing and payment
     choices, suggested/opposed reviewers, specific identifiers you would
     otherwise have to invent (DOIs, manuscript IDs), demographic/portal
     metadata, and revision/workflow flags. Fill everything that *is* derivable;
     list only these in `unfilled`, with a short reason, and move on.

2. **Render the skeleton** if the `.sub` does not exist yet (its comments also
   restate each field's options and help):

   ```
   paperpush subfile <venue> --fill-defaults -o <venue>.sub
   ```

3. **Read the files and extract values.** Read the user-identified files with
   the Read tool (PDFs and images are read directly). For a `.docx` or `.tex`
   source, read or convert it to text first. For each non-`never` field, produce
   a value:

   - `extract`: pull the statement verbatim where it exists. For `authors`, emit
     one line per author in the column format that field's `help` gives — usually
     `Name | email | affiliation | ORCID | corresponding`, marking exactly one
     corresponding author `yes` and the rest `no`, but some venues reorder the
     columns and some (arXiv) take the name alone. Leave a subfield blank (keep
     the `|`) if the source does not give it — never invent an email or ORCID.
   - `classify`: pick one option from `options` and explain your choice in
     `source`. Use `medium` confidence at best.
   - `filemap`: give an **absolute path** to each file (e.g.
     `/path/to/figures/fig1.png`), one per line. For a `filelist` field
     add the optional columns the `help` describes (`path | label`, or for
     bioRxiv supplements `path | type | linktext` where `type` is from
     `type_options`). Only the leading path segment is resolved, so keep it
     absolute; the other columns are preserved as written.

   Set `confidence` honestly per value:
   - `high`   — copied verbatim from the source, or an unambiguous file match.
   - `medium` — inferred, lightly reformatted, or a classification.
   - `low`    — a guess you are unsure about.

4. **Write `values.json`** in this schema (this is the contract the command
   reads — keep it exactly):

   ```json
   {
     "fields": [
       {"id": "title", "value": "...", "confidence": "high", "source": "manuscript.pdf p.1"},
       {"id": "authors", "value": "Ada Lovelace | ada@example.edu | Example University |  | yes",
        "confidence": "medium", "source": "title_page.pdf"},
       {"id": "subject_category", "value": "Bioinformatics", "confidence": "medium",
        "source": "classified from abstract"},
       {"id": "manuscript_file", "value": "manuscript.pdf", "confidence": "high", "source": "directory"},
       {"id": "figure_files", "value": "figures/fig1.png | Figure 1\nfigures/fig2.png | Figure 2",
        "confidence": "high", "source": "figures/"}
     ],
     "unfilled": [
       {"id": "license", "reason": "license is your policy choice"},
       {"id": "author_consent", "reason": "all authors must consent — confirm yourself"}
     ]
   }
   ```

   Do **not** put any `never`-role field in `fields`; put it in `unfilled`.

5. **Apply it** (use absolute paths for the `.sub`, `-d`, and `--values`):

   ```
   paperpush autofill <venue>.sub -d <manuscript-dir> --engine manual --values values.json
   ```

   Add `--dry-run` first if you want to preview without writing. The command
   prints a summary of what was filled, what needs review, and what was left for
   the user, and re-validates the result.

6. **Report to the user.** Relay the command's summary, then make the handoff
   unambiguous. State plainly:
   - which fields you filled with high confidence,
   - which need their review (classifications, yes/no declarations, and anything
     below high confidence) — name them so they can scan quickly,
   - **what remains for them to do** — see the checklist below,
   - any validation errors still in the file.

   ### What remains for the user (always spell this out)
   After the skill runs, only these are left for the user, because the skill
   cannot know or attest to them:
   - **`never` fields** — consent/attestations (e.g. `author_consent`,
     `software_tested`), license and payment choices, **suggested/opposed
     reviewers**, specific identifiers you would have to invent (e.g.
     `preprint_doi`), and revision/workflow flags. List the exact ones left in
     this file, each with the one-line reason from `unfilled`.
   - **Any review-flagged values** they should confirm or correct in the `.sub`.
   - **Remaining validation errors** (the command lists these) — the file will
     not submit until they are resolved.

   Then give them the next commands, with the absolute `.sub` path:
   ```
   paperpush login <venue>
   paperpush submit <venue>.sub
   ```
   Remind them these open the portal and stop before the final click, so they
   can review everything in the submission system before submitting.

## Rules

- **Always use absolute paths** — for the `.sub` file, the manuscript directory
  passed to `-d`, the `--values` file, and every file path inside `values.json`.
  Never pass a bare filename or a `~`/relative path.
- Fill every field you can derive from the files (all `extract`, `classify`, and
  `filemap` roles), including yes/no declarations and reproducibility-checklist
  booleans. Leave for the user only the `never` fields and anything you truly
  cannot determine — do not punt a derivable field just because it is a
  declaration.
- Never write `never`-role fields. Never invent emails, ORCIDs, funders, DOIs,
  or licenses not present in the source.
- Prefer source documents (title page, `.tex`, `.docx`) over a flattened PDF for
  author metadata.
- When unsure, lower the confidence rather than guessing high — low-confidence
  values are flagged for the user, not silently trusted.
- Keep `values.json` strictly in the schema above; the command validates ids
  against the venue and ignores anything unknown.
