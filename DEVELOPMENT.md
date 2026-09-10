# Development

## Install

```bash
git clone git@github.com:pachterlab/paperpush.git
cd paperpush
pip install -e .[dev]
```

## Debugging

Use `page.pause()` to add a breakpoint in code, and stop before the final
submit control during `playwright codegen`.

For iterating on the script, the VS Code debugger is recommended:

- Add a debug configuration in `.vscode/launch.json`
- Set breakpoints in the code (`breakpoint()` or IDE breakpoints)
- Run the debugger


## Testing

Local tests only:

```bash
pytest
```

Including headless portal tests (requires login credentials):

```bash
# All venues
pytest --run-portal

# Single venue (e.g. Nature)
pytest --run-portal --venue nature
```

Specifically run browser-based portal tests (requires a browser and login credentials):

```bash
# All venues
pytest tests/test_submit.py --run-portal -s

# Single venue (e.g. Nature)
pytest tests/test_submit.py --run-portal -s --venue nature

# Specific subfile (ie not default)
pytest tests/test_submit.py --run-portal -s --venue nature --subfile tests/sub_files/nature.sub
```

Update portal snapshots:

```bash
# All venues
pytest tests/test_portal_drift.py --run-portal --update-snapshots

# Single venue (e.g. Nature)
pytest tests/test_portal_drift.py --run-portal --update-snapshots --venue nature
```

Update venues.md checklist after running `pytest tests/test_submit.py --run-portal` (also run automatically by the pre-commit hook):

```bash
python scripts/gen_readme_venues.py
```

## Manuscript requirements

`paperpush/manuscript_requirements.json` records, per venue slug, what the
venue's author guidelines require of the manuscript itself. `paperpush validate`
measures the files a `.sub` names against it (`paperpush/requirements_check.py`),
and `paperpush requirements VENUE` prints it. It is the companion of
`venues.json`: that file describes the portal's form, this one the guidelines.

Every entry uses the same section keys, and each key is optional -- record only
what the guidelines state, and put rules with no structured key in that
section's `notes` list (one factual sentence each):

| section         | what it holds                                                                   |
|-----------------|---------------------------------------------------------------------------------|
| `manuscript`    | `formats`, `max_file_size_mb`, `max_words[_before_refs]`, `max_pages[_before_refs]`, `count_excludes`, `max_display_items`, `line_numbers`, `double_spacing`, `single_file`, `figures_placement`, `anonymized`, `template_required`, ... |
| `title_page`    | `required_items` (canonical ids: `title`, `authors`, `affiliations`, `corresponding_email`, `orcid`, `keywords`, `running_title`, `word_count`, ...), `title_max_characters`, `running_title_max_characters` |
| `abstract`      | `max_words`, `min_words`, `max_characters`, `structured`, `structured_headings`, `no_references` |
| `keywords`      | `min`, `max`                                                                    |
| `sections`      | `required`, `optional`, `order`, `combined_allowed` -- canonical names (`Introduction`, `Results`, `Discussion`, `Methods`, ...) |
| `statements`    | `required`, `optional` -- canonical ids (`data_availability`, `competing_interests`, `author_contributions`, `funding`, `ethics_approval`, ...) |
| `figures`       | `formats`, `min_dpi[_line_art]`, `max_file_size_mb`, `max_count`, `single_column_width_mm`, `double_column_width_mm`, `max_width_mm`, `max_height_mm`, `color_mode`, `font`, `legend_max_words`, `placement`, ... |
| `tables`        | `formats`, `editable`, `max_count`, `placement`                                 |
| `supplementary` | `formats`, `max_file_size_mb`, `max_count`, `combined_single_pdf`               |
| `references`    | `style`, `numbered`, `max_count`, `include_titles`, `doi_required`              |
| `cover_letter`  | `required`, `formats`, `max_words`                                              |
| `upload`        | `max_total_mb`, `max_file_mb`                                                   |

The full key list with descriptions is the generated
`paperpush/manuscript_requirements.schema.json` (from the dataclasses in
`paperpush/requirements.py`; regenerate with `python scripts/gen_venues_schema.py`).
The editor picks it up automatically through `.vscode/settings.json`, and
`python scripts/gen_schema_docs.py` renders both this schema and
`venues.schema.json` as the Markdown reference tables under `docs/schemas/`
(CI checks they are in sync; the pre-commit hook regenerates them when a schema
is committed).

Every entry also carries `source_urls` (the guideline pages it was read from)
and `retrieved` (the date, `YYYY-MM-DD`). Two mechanisms keep entries short:

- `"inherits": "<slug>"` starts from another venue's entry and overrides keys
  section by section (a key set to `null` drops the inherited value; `notes`
  accumulate).
- `"article_type_field"` names the `.sub` field that selects the article type
  and `"article_types"` maps each of that field's option strings (exactly as in
  `venues.json`) to the overrides for that type. The top level holds the primary
  research-article rules. `tests/test_requirements.py` checks every key is a
  real option of the field.

The `$aliases` key at the top of the file maps each canonical section and
statement name to the heading wordings that count as it ("Materials and
Methods" for `Methods`, "Conflict of interest" for `competing_interests`, ...).
Add a wording there when a venue uses a new one.

To keep rules from being reported twice, the checker applies a guideline rule
only when the `venues.json` field does not already enforce the same thing
(`accept`, `max_file_size_mb`, `max_words`, `word_count`, `max_count`, ...);
the portal's own limits are the enforced ones and stay in `venues.json`.

Page limits on a `.tex` manuscript need a rendered PDF: `paperpush.manuscript.build_pdf`
compiles it with `latexmk` (or `pdflatex` + `bibtex`) into a scratch directory,
never writing next to the source, and caches the build for the process. With
no TeX toolchain the limit is reported as unchecked. Tests that need a
toolchain are skipped when none is installed.

## Formatting

```bash
black . -l 99999
```

## CI/CD

GitHub Actions (`.github/workflows/`). Both workflows run on every push and pull
request, and allow manual runs from the Actions tab. Neither touches a live
portal, so no session secret is needed.

- `ci.yml` — runs `pytest` (real-portal tests are skipped) and checks that
  `venues.md` / `README.md` and `venues.schema.json` are in sync with their
  generators.
- `docs.yml` — builds the Sphinx docs with warnings as errors. Check only; it
  does not publish. The docs are hosted by Read the Docs
  (<https://paperpush.readthedocs.io>), which builds them from `.readthedocs.yaml`
  via a webhook, outside of Actions. Read the Docs sets `fail_on_warning` too,
  but a failure there does not block a PR — `docs.yml` is what turns broken docs
  into a red check before the merge.

**Portal health is checked locally, not in CI.** The scheduled workflows that
drove live portals (`submit.yml`, `fingerprint.yml`, `nature-categories.yml`)
were removed when this repo went public: they relied on a `PAPERPUSH_SESSIONS_B64`
secret carrying live logins to real accounts, which is unsafe in a public repo
where anyone who can edit a workflow could exfiltrate it. Run the walkthrough
yourself instead:

```bash
pytest tests/test_submit.py --run-portal
python scripts/gen_readme_venues.py
```

The submit walkthrough's pass/fail is the portal-health signal: a portal change
that breaks submission flips the venue to ❌ in
`tests/submit_walkthrough_status.json`. Regenerate the venue tables after any
run — CI fails if `venues.md` is out of sync with that file.

To deprecate a venue, add the field `deprecated:true` to its entry in `paperpush/venues.json`.