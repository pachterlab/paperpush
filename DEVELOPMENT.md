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
An entry is optional: a venue without one is still fully supported, `validate`
simply has no manuscript rules to apply, and `paperpush requirements VENUE`
says so. Add one whenever the guidelines are available.

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

### Keeping entries current

`scripts/check_guidelines.py` re-reads every page named in `source_urls` and
reports which ones changed since their entry was written. It renders each page
in a browser, hashes the text of its main content (PDF and `.docx` sources are
downloaded and their text extracted), and compares that with the hash recorded
in `scripts/guideline_fingerprints.json`. It never calls an LLM, so it is cheap
enough to run on a schedule; only the venues it flags need to be re-read.

```bash
python scripts/check_guidelines.py --diff          # check every page, show what changed
python scripts/check_guidelines.py --venue nature  # one venue
python scripts/check_guidelines.py --accept nature # after updating the entry: advance its baseline
python scripts/check_guidelines.py --record-new    # after adding a venue: record its pages
```

Each page comes back `unchanged`, `changed`, `new` (no baseline), `unreadable`
(fetch failed, bot wall, or almost no text: never treated as a change) or `gone`
(404/410: the entry needs a new URL). The JSON report
(`.guideline_cache/report.json`) marks a venue `needs_review` when a page
changed or is gone, or when it has unreadable pages and `retrieved` is more than
`--stale-days` (180) old. Changed pages carry a line diff against the recorded
text, which is kept in the gitignored `.guideline_cache/`.

The baseline moves only through `--accept` / `--record-new`. Commit
`guideline_fingerprints.json` together with the edit to
`manuscript_requirements.json` (and its new `retrieved` date): merging that
commit is what marks the change as handled, and a change nobody acted on is
detected again on the next run. Several publishers refuse headless browsers, so
the browser runs headed; without a display the script re-runs itself under
`xvfb-run`.

`scripts/update_guidelines.py` is the scheduled form of that loop: it runs the
check and, only for the venues that need review, starts a headless Claude Code
agent (`claude -p`, the local subscription) that is given the entry, the diff of
each changed page and the current text of all the venue's pages, and edits the
entry. A run in which nothing changed starts no agent.

```bash
python scripts/update_guidelines.py                  # check, update, open a pull request
python scripts/update_guidelines.py --dry-run        # same, but push nothing
python scripts/update_guidelines.py --dry-run --skip-check --venue cell   # reuse the last report
```

It works in a git worktree (`.guideline_cache/worktree`, branch
`guideline-updates`, from `origin/main`), so your checkout is never touched. The
agent can read, search the web and edit, plus one command: `check_guidelines.py
--text URL`, for pages that refuse its own fetcher. An edit is kept only if the
file still loads, no other venue's entry moved, and no test in
`tests/test_requirements.py` started failing; each venue is one commit, with
`retrieved` set to the day of the run. Baselines advance only for pages whose
venues were all handled, so a venue the agent failed on is picked up again next
time. The branch is then pushed and a pull request opened; while that pull
request is open, later runs start from its branch and add to it. Nothing is
published to users until the pull request is merged.

## Venue data updates without a release

The venue data -- `venues.json`, `manuscript_requirements.json`, their two
schemas, and `paperpush/venues/_assets/*` -- is published on its own, so a data
fix reaches installed copies without a PyPI release:

1. After `ci.yml` passes on `main`, `venue-data.yml` runs
   `scripts/build_venue_data.py`, which validates the data against its schemas,
   loads it with the package's own loader, and writes it with a `manifest.json`
   (data format, source commit, sha256 of every file). If a data file changed,
   the result is committed to the `venue-data` branch.
2. Installed copies fetch that branch (`paperpush/venue_data.py`) at most once a
   day, verify every hash, check that the set loads, and cache it in
   `~/.cache/paperpush/venue-data`. `paperpush update-venues` refreshes the
   cache right away, and `paperpush --venues` shows which copy is in use. With
   no network or no cache, the bundled copy is used.

Each installed version takes, entry by entry, only what its code can use. An
entry it can't use keeps its bundled version, and so does any entry that
inherits from it:

- `venues.json` (`merge_published` in `paperpush/database.py`): a runner is
  written against its venue's field ids and types. So labels, help text,
  options, limits, `required` and URLs reach users without a release. New
  venues, and adding, removing, renaming, reordering or retyping a field, wait
  for the next release.
- `manuscript_requirements.json` (`merge_published` in
  `paperpush/requirements.py`): no runner depends on these rules, so the
  published copy can change, add or drop any entry. But `validate` can only
  apply rules it knows. So an entry that uses a rule key or section this
  version's dataclasses lack, or a value of a different type, waits for the next
  release.
- The `_assets` vocabulary files are data only and always reach users.

Bump `DATA_FORMAT` in `paperpush/venue_data.py` when a data change needs new
code to be *read* correctly (a new key the loader must understand, a changed
merge rule). Installed versions with the old format then keep their bundled data
until they upgrade.

In a source checkout, paperpush always reads the files in the checkout, so your
edits are what you see, test and generate from. Set `PAPERPUSH_VENUE_DATA` to
change the source: `remote` fetches the published copy even in a checkout,
`bundled` uses only the packaged files, and a directory path reads the files
from there. `PAPERPUSH_OFFLINE=1` uses the cache but never fetches, and
`PAPERPUSH_VENUE_DATA_URL` points at another published copy.

## Formatting

```bash
black . -l 99999
```

## CI/CD

GitHub Actions (`.github/workflows/`). `ci.yml` and `docs.yml` run on every
push and pull request, and all three allow manual runs from the Actions tab.
None touches a live portal, so no session secret is needed.

- `ci.yml` — runs `pytest` (real-portal tests are skipped) and checks that
  `venues.md` / `README.md` and `venues.schema.json` are in sync with their
  generators.
- `docs.yml` — builds the Sphinx docs with warnings as errors. Check only; it
  does not publish. The docs are hosted by Read the Docs
  (<https://paperpush.readthedocs.io>), which builds them from `.readthedocs.yaml`
  via a webhook, outside of Actions. Read the Docs sets `fail_on_warning` too,
  but a failure there does not block a PR — `docs.yml` is what turns broken docs
  into a red check before the merge.
- `venue-data.yml` — after `ci.yml` passes on `main`, publishes the venue data
  to the `venue-data` branch that installed copies fetch (see "Venue data
  updates without a release" above).

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