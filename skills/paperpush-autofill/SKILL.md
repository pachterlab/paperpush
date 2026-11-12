---
name: paperpush-autofill
description: >-
  Fill a paperpush .sub submission file from a directory of manuscript
  files. Reads the manuscript, title page, figures, and supplements, extracts
  the submission fields, and writes them into the .sub via the deterministic
  autofill core. Use when the user wants to auto-populate a venue submission
  from their files. Also updates an already-filled .sub after the manuscript
  changes, reusing the paths it records and keeping hand edits. Triggers:
  "autofill my sub", "fill the biorxiv sub from this folder", "populate my
  submission from this manuscript directory", "update my sub, the manuscript
  changed", "re-autofill", "refresh the sub from the new draft".
---

# paperpush autofill (Claude skill)

Populate a `<venue>.sub.txt` file (creating it with `paperpush subfile` first if
it does not exist) from a directory of manuscript files. You read
the files and decide the values; the `paperpush autofill` command does all
the writing, gating, and validation, so you cannot accidentally overwrite a
policy field or produce an invalid file. Your job is extraction, not writing.

## First: fresh fill or update?

If the user points you at a `.sub` that already has values in it (a title, an
abstract, or file paths, not just the blank template), or asks to "update",
"refresh", or "re-autofill" it, this is an **update**: follow
[Updating an existing .sub](#updating-an-existing-sub) below. It replaces the
start-up questions with what the file already records. Otherwise this is a
fresh fill: continue here.

## Fresh fill: make sure you have a directory

The manuscript directory is required. **If the skill was invoked on its own,
without a directory or a filled `.sub` in the user's message, stop and ask the user to provide the
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
   it from a `<slug>.sub.txt` filename they mention. Confirm it is supported with
   `paperpush venues`.
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

2. **Make sure the `.sub` exists — create it if it does not.** `paperpush
   autofill` fills an existing `.sub`; it does not create one, so never skip
   this check. Resolve the path in this order:
   - If the user named a `.sub` path, use it. If that file does not exist yet,
     create it at that path with the command below.
   - Else if `<manuscript-dir>/<venue>.sub.txt` already exists, reuse it (do not
     overwrite it unless the user asks to regenerate).
   - Else create it in the manuscript directory:

   ```
   paperpush subfile <venue> -o <manuscript-dir>/<venue>.sub.txt
   ```

   Use an absolute path for `-o`, check the file exists afterwards, and use
   that resolved path for every later step. The new file's comments also
   restate each field's options and help.

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

5. **Apply it** (use absolute paths for the `.sub`, `-p`, and `--values`):

   ```
   paperpush autofill <venue>.sub.txt -p <manuscript-dir> --engine manual --values values.json
   ```

   `-p` takes several directories and/or documents if the files are spread
   out (e.g. `-p <ms-dir> <figures-dir> <cover-letter.docx>`); it stops at the
   next flag or the first `.sub` file.

   **Attestations.** Add `--confirm-attestations` only when the user asked you
   to confirm the attestations (e.g. "and say yes to the consent boxes"). It
   sets every blank attestation (a required yes/no confirmation whose only
   accepted answer is yes; the `.sub` marks it "must be yes to submit") to
   `yes` on behalf of all authors, and prints each sentence under "Confirmed
   with --confirm-attestations". Relay those sentences to the user verbatim.
   Never add it to clear a validation error or because the attestation "has
   to be yes anyway". Without it, attestations stay in `unfilled` like every
   other `never` field.

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
     this file, each with the one-line reason from `unfilled`. For the
     attestations among them, add that the user can set them to `yes` or ask
     you to rerun with `--confirm-attestations`.
   - **Any review-flagged values** they should confirm or correct in the `.sub`.
   - **Remaining validation errors** (the command lists these) — the file will
     not submit until they are resolved.

   Then give them the next commands, with the absolute `.sub` path:
   ```
   paperpush login <venue>
   paperpush submit <venue>.sub.txt
   ```
   Remind them these open the portal and stop before the final click, so they
   can review everything in the submission system before submitting.

## Updating an existing .sub

Use this when the `.sub` is already filled and the manuscript files have
changed (a new draft, new figures, a reordered author list). The goal is to
bring the `.sub` in line with the files **without** re-asking what the file
already tells you and **without** losing anything the author typed in by hand.

1. **Read the `.sub` and take what it already knows.** The `@venue:` line is
   the venue. The `filemap` fields (manuscript, title page, figures,
   supplements, cover letter) give the file mapping, and their directories are
   the manuscript directory (or directories) for `-p`. Don't ask the start-up
   questions again. Ask only about what the file can't answer:
   - a recorded path that no longer exists (renamed or moved file): propose the
     likely replacement from a listing of that directory and confirm it;
   - new files in those directories that no field points at yet (a new figure,
     a new supplement): propose where each goes and confirm it.
   If the user named a different directory in their message, use theirs.

2. **Re-extract every derivable field from the current files**, exactly as in
   steps 1, 3, and 4 of the fresh fill (schema, extraction, `values.json`),
   with the same roles, confidence rules, and `never` handling. Re-propose
   every field, not only the ones you think changed: the command compares each
   value against the file and leaves matches untouched.

3. **Preview the update** with both `--dry-run` and `--keep-existing`:

   ```
   paperpush autofill <venue>.sub.txt -p <manuscript-dir> --engine manual --values values.json --keep-existing --dry-run
   ```

   The summary sorts every field into:
   - **Unchanged**: the file already says what the manuscript says.
   - **Filled / need your review**: fields that were blank or still at their
     template default, now filled.
   - **Kept your existing value**: the file holds a value and the manuscript
     now says something different. Each one shows both versions.

4. **Settle the kept fields with the user.** A kept field is either a hand edit
   the author wants to keep (a shortened title for the portal, a corrected
   affiliation) or a stale value from the old draft. You can't tell which from
   the file, so show the user each one as *in the file* vs *manuscript now* and
   ask which to keep. You may recommend: a new figure path, page count, or
   figure count is almost always stale; a reworded statement that differs from
   the manuscript only in formatting is likely a deliberate edit. Don't decide
   on their behalf when it's a judgment call.

5. **Apply.** Run the same command without `--dry-run`, keeping
   `--keep-existing`, so only blank and default fields are written. Then, for
   the kept fields the user chose to update, write a second `values.json`
   holding only those fields and apply it **without** `--keep-existing`:

   ```
   paperpush autofill <venue>.sub.txt -p <manuscript-dir> --engine manual --values values_accepted.json
   ```

   Fields the user wants left alone stay out of that second file entirely.
   If the user says up front to take everything from the new draft ("just
   overwrite it from the manuscript"), skip `--keep-existing` and step 4, and
   apply once. The summary marks each replaced value "[replaced the previous
   value]".

6. **Report** as in step 6 of the fresh fill, but lead with what changed:
   the fields updated from the new draft, the ones the user chose to keep, any
   remapped or newly added files, and then what remains (the `never` fields
   and validation errors, as before). Since the manuscript changed, suggest
   running `paperpush-validate` again.

## Rules

- **Always use absolute paths** — for the `.sub` file, the manuscript directory
  passed to `-p`, the `--values` file, and every file path inside `values.json`.
  Never pass a bare filename or a `~`/relative path.
- Fill every field you can derive from the files (all `extract`, `classify`, and
  `filemap` roles), including yes/no declarations and reproducibility-checklist
  booleans. Leave for the user only the `never` fields and anything you truly
  cannot determine — do not punt a derivable field just because it is a
  declaration.
- Never write `never`-role fields. Never pass `--confirm-attestations` unless
  the user asked for it. Never invent emails, ORCIDs, funders, DOIs,
  or licenses not present in the source.
- Prefer source documents (title page, `.tex`, `.docx`) over a flattened PDF for
  author metadata.
- When unsure, lower the confidence rather than guessing high — low-confidence
  values are flagged for the user, not silently trusted.
- Keep `values.json` strictly in the schema above; the command validates ids
  against the venue and ignores anything unknown.
- On an update, never overwrite a value the author set without their say-so:
  preview with `--keep-existing`, and apply a differing value only after the
  user picked it (or told you to take everything from the new draft).
