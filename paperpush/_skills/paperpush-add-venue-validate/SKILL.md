---
name: paperpush-add-venue-validate
description: >-
  Read a venue's author-guidelines and submission-instructions pages and record
  them in paperpush: the manuscript rules in manuscript_requirements.json and
  the basic submission fields in submission_forms.json, through a venue file
  and `paperpush add-venue`. Given a venue paperpush already has, re-read its
  guideline pages and update what changed. No submit runner is written (that is
  paperpush-add-venue). Triggers: "add <venue>'s guidelines to paperpush",
  "make <venue> validate-only in paperpush", "re-check <venue>'s author
  guidelines", "update <venue>'s word limits", "fill in this venue file".
---

# Add or refresh a venue's guidelines (validate support)

**Needs a clone of the paperpush repo** (`git clone
https://github.com/pachterlab/paperpush` and `pip install -e .[dev]`), run from
its root. This skill edits the venue databases, runner code, and tests, and
uses the repo's `scripts/` and `tests/`. Working from a pip install? Ask the
user to clone the repo first.

This gives `paperpush validate` a venue's rules, and gives `subfile` /
`autofill` its basic fields. It writes no Playwright code. For `submit`
support, run `paperpush-add-venue` afterwards; it calls this skill first.

Read `DEVELOPMENT.md` ("Manuscript requirements", "Keeping entries current",
"Venue databases") and the "Adding a submission venue" part of
`CONTRIBUTING.md` before you start. They define the rules this skill follows.

## Inputs

- **The venue**: a name, slug, or URL. Get the slug from `paperpush venues`.
  A new slug is lowercase letters, digits, and underscores (`nature_genetics`).
- **The guideline URLs**, if the user has them. Otherwise find the venue's own
  author-guidelines and submission-instructions pages. Use only pages the venue
  publishes, never third-party summaries.

## 1. New venue or existing one?

```
paperpush venues | grep -i <name>
paperpush requirements <slug>
```

- **New**: `paperpush add-venue --template <slug> -o <scratch>/<slug>.txt`
- **Existing** (or a sister journal of one, e.g. another Nature title):
  `paperpush add-venue --template --from <existing> -o <scratch>/<slug>.txt`

For an existing venue in a source checkout, see what changed since the entry
was written. This calls no LLM:

```
python scripts/check_guidelines.py --diff --venue <slug>
```

Each page comes back `unchanged`, `changed` (with a diff), `new`, `unreadable`,
or `gone` (it needs a new URL). Re-read the `changed` and `gone` pages
carefully. Still skim the `unchanged` ones against the entry, because an entry
can be wrong from the start.

## 2. Read the pages

- Read the guidelines for every article type, plus the
  submission/initial-submission instructions, figure guidelines, and
  reference style. Follow links to separate pages for figures, LaTeX
  templates, and cover letters. PDF and `.docx` guideline files count as pages.
- Publisher sites (Nature, Science, Cell, Springer, Elsevier, Wiley) usually
  block plain fetches. Render them with Playwright (headed if headless is
  refused), or run `python scripts/check_guidelines.py --text <URL>`.
- Write down each page you used. They all go in `source_urls`.

## 3. Fill in the venue file

Read the whole file first. Every key has a comment describing it.

- **Record only what the pages state.** An empty key means "the venue does not
  say", and paperpush then checks nothing for it. Never fill a key with a
  typical value.
- `[venue]`: name, full name, type, homepage, guide URL. Set
  `anonymous: yes` only for double-blind review. Set `keywords` to the subject
  areas it covers, from the listed choices (e.g. `medicine, radiology`).
- `[requirements.*]`: the primary research-article rules. Use the structured
  keys where they fit, with canonical ids (`data_availability`, `Methods`, ...;
  see `$aliases` in `manuscript_requirements.json`). Put every other rule in
  that section's `notes`, one factual sentence per line. Set `source_urls`, and
  set `retrieved` to today's date (`YYYY-MM-DD`).
- `[article_type NAME]`: one block per other article type, with only the keys
  that differ. NAME must match an option of the article-type field exactly.
- If the guidelines let the initial submission be in any format, put that rule
  in one sentence in `manuscript.initial_submission_any_format`.
- If a template is mandatory and the official LaTeX template is public, compile
  it unmodified in review mode and run
  `python scripts/measure_template.py <pdf> --running-head "<head>"`. Paste the
  output into `manuscript.template_layout`.

### `[form]`: the basic fields

- **New venue, or one with no runner** (`paperpush venues` shows no submit
  support): set `include: yes` and keep only the fields that the submission
  instructions show the portal asks for. Usually these are the article type,
  title, abstract, keywords, authors, manuscript file, figures, tables,
  supplementary files, and cover letter. Reuse the common ids from the comment
  so `common_fields.json` places them, copy limits and `accept` lists from the
  instructions, and copy article-type `options` word for word. Set
  `submission_url`. If the instructions say nothing about the portal, set
  `include: no` and the venue gets `default_form()`.
- **Venue with a runner**: the runner reads these field ids and types, so never
  add, remove, rename, retype, or reorder a field here. Only correct labels,
  help text, options, limits, and `required`, and only where the pages show
  they are wrong. Structural form changes belong in `paperpush-add-venue`.

## 4. Check, merge, regenerate

```
paperpush add-venue <scratch>/<slug>.txt --dry-run      # fix every error (each has a line number)
paperpush add-venue <scratch>/<slug>.txt [--overwrite]  # --overwrite for an existing venue
python scripts/check_venue_consistency.py               # portal vs guideline limits
python scripts/gen_subfile_templates.py
python scripts/gen_readme_venues.py
```

Treat consistency warnings as questions. A portal limit that differs from the
guidelines is fine only when the venue really states both values. In that case
keep both.

Then record the guideline baselines (source checkout only):

```
python scripts/check_guidelines.py --record-new          # new venue / new URLs
python scripts/check_guidelines.py --accept <slug>       # existing venue you just updated
```

Run the tests that cover these files:

```
pytest tests/test_requirements.py tests/test_venue_consistency.py tests/test_subfile.py tests/test_internals.py tests/test_venuefile.py tests/test_validate_only_venue.py -q
```

Run `paperpush subfile <slug> -o <scratch>/<slug>.sub.txt` and
`paperpush requirements <slug>` to check the result reads correctly. If the
repo has sample manuscripts for this venue (`tests/sub_files/<slug>/`), also
run `paperpush validate` on them.

## 5. Report

Tell the user:

- what was added or changed, as a short before/after list for an update;
- which pages it came from, and which pages were unreadable or gone;
- what you could not determine (form fields behind a login, ambiguous
  limits), and the keys you left empty because of it;
- whether `submit` works yet. It does not unless the venue already had a
  runner; offer `paperpush-add-venue` for that.

Do not commit unless asked. If they do ask, commit
`scripts/guideline_fingerprints.json` in the same commit as the entry, as
DEVELOPMENT.md requires.
