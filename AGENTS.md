# AGENTS.md

Guidance for any AI agent (Codex, Claude, or otherwise) that decides to use
`paperpush` to submit a manuscript to a venue - a journal, preprint server, or conference -
on a user's behalf.

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

## Before anything else: check the user is logged in

The pipeline ends in a real browser sign-in, and that sign-in is the one step you
cannot do for the user — it may need their password, ORCID, a CAPTCHA, or a
two-factor code. Check it **first**, before you read a single manuscript file, so
the user can log in while you work instead of discovering the blocker at the end:

```
paperpush login --list
```

It prints `Not logged in to any venues.` or a `Logged in to:` block with one
`<venue>: <who>` line per stored credential. If the target venue is not in that
list, stop and tell the user right away — in your very next message — to run the
login themselves in their terminal:

```
paperpush login <venue>
```

Say plainly that this opens a browser and is theirs to complete, and that you
will keep going in the meantime. Do not run `paperpush login` yourself, and do
not treat a missing login as a reason to skip the extraction work — do the
`.sub` creation, autofill, and validation regardless, then re-check
`paperpush login --list` before the submit step.

Some public submission systems do not have author accounts. If the target is
absent from `--list`, check it with `paperpush login <venue> --status` before
asking the user to sign in. A loginless venue reports that it does not require
login and exits successfully; proceed without credentials in that case.

(For a single venue, `paperpush login <venue> --status` gives the same answer;
`--list` is the one call that covers "am I logged in anywhere yet".)

**Connected over MCP instead of a shell?** `login_status` is this check, and
`login_status(venue=...)` is the per-venue form — it returns the exact
`login_command` to pass along when the venue is not signed in. The `login` tool
is the same guard in one call: it reports an existing login, and otherwise hands
back the command for the user to run, so the user's password never passes
through the conversation. (It does run the login itself when
`PAPERPUSH_USERNAME` and `PAPERPUSH_PASSWORD` are already set in the server's
environment.) Everything else in this document maps to a tool too —
`list_supported_venues`, `describe_venue` for the schema, `field_options`,
`create_subfile`, `read_subfile`, `autofill_subfile` (same `values.json` shape,
passed as the `values` argument), `validate_subfile`, `submit`, `submit_status`,
and `submit_close`. Pass **absolute paths**: the server has its own working
directory, so a relative path means something different to it than it does to
you.

`submit` is the one tool that does not finish its work before returning. The
wizard fills the form and then parks holding the browser open for the author, so
the tool hands back a `pid` as soon as the run has started: poll `submit_status`
for progress, tell the user to review and send the form themselves, and call
`submit_close` when they say they are done. It refuses to open a browser at all
unless the `.sub` validates and, when the portal requires authentication, the
venue already has a stored login — so run the login check above first.

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
   - `authors`: one line per author in the venue's column order, given in that
     field's `help` — usually `Name | email | affiliation | ORCID |
     corresponding` with exactly one corresponding author marked `yes`, but some
     venues reorder the columns and some (arXiv) take the name alone. Leave a
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
   `validate` → `login` → `submit`, stopping at the first failure. The `login`
   step is interactive, so it is the user's to complete in their terminal — if
   the pre-flight check above showed them logged out, make sure they have run
   `paperpush login <venue>` before you kick off the pipeline. The submit
   step opens the portal headed and stops before the final click so the user can
   review. Add `--headless`, `--force`, etc. as the user needs (see `--help`).
   `--orcid` signs in with the user's ORCID iD and ORCID password instead of a
   venue account — offer it only if the user says that is how they sign in. It
   works for bioRxiv, medRxiv, arXiv, the Editorial Manager journals (Cell
   family and PLOS Computational Biology), BMC Bioinformatics, and Genome
   Biology; other venues report that the flow is not recorded yet, and
   conference portals refuse it.

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
