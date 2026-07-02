# AGENTS.md

Guidance for any AI agent (Codex, Claude, or otherwise) that decides to use
`paperpush` to submit a manuscript to a venue or preprint server on a
user's behalf.

## The one rule: you are the extractor

`paperpush` splits submission into two layers:

- **You (the agent) propose values.** You read the manuscript files and decide
  what each submission field should contain.
- **The `paperpush` CLI writes and validates.** A deterministic core does
  all the writing, policy gating, path resolution, and validation, so you cannot
  overwrite a policy field or produce an invalid file.

**Do your own autofilling.** Read the manuscript yourself and hand the CLI a
`values.json`. Do **not** reach for `paperpush autofill --engine api` — that
path calls the Anthropic API and needs an `ANTHROPIC_API_KEY`. You already have
the manuscript in front of you; extract from it directly. Use `--engine api`
only if the user has explicitly set an API key and asked for it.

## Run the full pipeline, autofilling yourself

The whole flow — create the `.sub`, fill it from your values, validate, log in,
submit — runs from `scripts/paperpush_pipeline.py`. The autofill step
defaults to `--engine manual`, which applies the `values.json` **you** wrote. So
"run the full pipeline but do your own autofilling" is:

1. **Learn the fields.** Get the schema (roles + constraints) without needing a
   `.sub` to exist yet:

   ```
   paperpush schema <venue>
   ```

   Each field carries a `role`:
   - `extract`  — copy a value verbatim from the manuscript (title, abstract,
     author list, competing-interest / funding / data-availability statements,
     page/figure/table counts, a repo URL or accession printed in the paper).
   - `classify` — choose exactly one of the field's `options`, or answer
     `yes`/`no` for a boolean declaration. Use `medium` confidence at best so the
     value is flagged for the author.
   - `filemap`  — assign a file (or files) from the directory to the field.
   - `never`    — **do not fill.** Consent/attestations, license and payment
     choices, suggested/opposed reviewers, identifiers you'd have to invent
     (DOIs, manuscript IDs), and workflow flags. List these in `unfilled` with a
     one-line reason and move on.

2. **Read the files and write `values.json`.** Read the user-identified files
   directly (convert `.docx`/`.tex` to text first). Prefer a title page or
   LaTeX/Word source over a flattened PDF for author metadata. Then write:

   ```json
   {
     "fields": [
       {"id": "title", "value": "...", "confidence": "high", "source": "manuscript.pdf p.1"},
       {"id": "authors",
        "value": "Ada Lovelace | ada@example.edu | Example University |  | yes",
        "confidence": "medium", "source": "title_page.pdf"},
       {"id": "subject_category", "value": "Bioinformatics",
        "confidence": "medium", "source": "classified from abstract"},
       {"id": "manuscript_file", "value": "/abs/path/manuscript.pdf",
        "confidence": "high", "source": "directory"},
       {"id": "figure_files",
        "value": "/abs/path/figures/fig1.png | Figure 1\n/abs/path/figures/fig2.png | Figure 2",
        "confidence": "high", "source": "figures/"}
     ],
     "unfilled": [
       {"id": "license", "reason": "license is the author's policy choice"},
       {"id": "author_consent", "reason": "all authors must consent -- confirm yourself"}
     ]
   }
   ```

   Rules for the values:
   - `authors`: one line per author as `Name | email | affiliation | ORCID |
     corresponding`; mark exactly one corresponding author `yes`. Leave a
     subfield blank (keep the `|`) rather than inventing an email or ORCID.
   - `filemap`: use **absolute paths**; only the leading path segment is
     resolved, so extra columns (`| label`, or bioRxiv `| type | linktext`) are
     preserved as written.
   - `confidence`: `high` = copied verbatim / unambiguous file match; `medium` =
     inferred, reformatted, or a classification; `low` = a genuine guess. When
     unsure, lower the confidence rather than guessing high.
   - Never put a `never`-role field in `fields`; put it in `unfilled`.

3. **Run the pipeline** with your values (use absolute paths throughout):

   ```
   python scripts/paperpush_pipeline.py <venue> \
       -d /abs/path/to/manuscript-dir \
       --values /abs/path/to/values.json
   ```

   This runs `subfile` → `autofill --engine manual` (applying your values) →
   `validate` → `login` → `submit`, stopping at the first failure. The submit
   step opens the portal headed and stops before the final click so the user can
   review. Add `--headless`, `--orcid`, `--force`, etc. as the user needs (see
   `--help`).

If you prefer to drive the steps yourself instead of the pipeline script, the
autofill step alone is:

```
paperpush autofill <venue>.sub -d <manuscript-dir> --engine manual --values values.json
```

(Add `--dry-run` to preview without writing.)

## After it runs, tell the user what's left

`paperpush` deliberately leaves the `never` fields blank. Spell out for the
user exactly what remains: the `never` fields (each with its `unfilled` reason),
any review-flagged values to confirm, and any remaining validation errors (the
file will not submit until those are resolved).

## Claude Code note

If you are Claude Code, prefer the packaged skills, which encode this same
contract: `/paperpush-prepare-submission` for the end-to-end flow, or
`/paperpush-autofill` for the extraction step alone.
