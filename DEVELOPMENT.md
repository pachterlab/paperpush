# Development

## Install

```bash
git clone git@github.com:pachterlab/paperpush.git
cd paperpush
pip install -e .[dev]
```

## Recording a runner with paperpush codegen

`paperpush codegen` allows you to convert interaction with a venue's graphical submission portal to a python Playwright script. It extends `playwright codegen` in the following ways:
- `undo/redo`: erases or restores a generated line of Playwright script
  - does not change the state of the webpage
- `branches`: create a switch statement to reflect a form with multiple choices
  - when you create a branch, you will continue down that path until you click `next`, at which point you will enter the next switch statement. 
  - you can end a branch to return back to the upper level
- `optional block`: a toggle that wraps code in an "optional" comment
- `dropdown choices`: click on an expanded dropdown list to copy all options as a comment
  - does not work unless the dropdown list is expanded (i.e., options are displayed on the screen)
- `variable`: replace a highlighted snippet with a variable inserted at the top of the script. 
  - great for replacing placeholder values with their variable name from the subfile 
- `HTML`: grab the HTML for a specific element, or copy for the entire page. 

`paperpush codegen` usage:

```bash
paperpush codegen --venue biorxiv -o biorxiv_steps.py          # reuse the saved sign-in, offer biorxiv's fields
paperpush codegen --sub biorxiv.sub                             # also bind values matching the .sub to its fields
paperpush codegen https://portal.example.org --target python    # a standalone Playwright script
```

The toolbar (drag it by its grip; the code panel shows below it):

| Button | Does |
| --- | --- |
| Record | start/stop recording (off: your clicks are not recorded) |
| Play / Pause / Stop | replay the script in a new tab of the same context; with `--sub`, bound variables replay the `.sub` values |
| Undo / Redo | undo or redo the last change to the script (not to the page) |
| Branch / Next / End | record a choice between paths, such as a question's options: press Branch, record the first option's actions, press Next before each further option, press End after the last, then carry on (e.g. click Continue) |
| Optional | toggle: press to start an optional section (`# ---OPTIONAL---`), press again to end it (`# ---END OPTIONAL---`) |
| Dropdown Choices | pick a dropdown, list or radio group: its choices go into the code as a comment (`# Options of "Article type" (3):`), to bind or branch on later |
| Variable | replace the selected line's value with a variable (`values.get(...)`), or rename the selected branch |
| HTML / Copy Page HTML | copy one picked element's HTML, or the page's HTML (all frames) |
| Code / Copy Code / Save Code | show or hide the code panel; copy the code; save it to a file (asks for the path, default `-o` or `paperpush_codegen.py`) |

A branch renders as a `match` on its variable, one `case` per path keyed by the
element's label, so a run takes the path its `.sub` value names:

```python
match server_suitability:
    case "Diagnostic tools or medical equipment tested in humans":
        page.get_by_role("radio", name="Diagnostic tools or medical equipment tested in humans").check()
    case "Biological research":
        page.get_by_role("radio", name="Biological research").check()
page.get_by_role("button", name="Continue").click()
```

The variable is `branch_N` until the paths match a venue field's options (then it
takes that field's name) or you rename it. Click the `match` line or a `case` line
in the code panel to retype the name or a path's key (Enter saves, Esc cancels).
Each path is one action. Replay takes the path the `.sub` value names, else the one
recorded last.

What it records for you: typing into one field is one `fill`; a button that opens a
file chooser becomes `expect_file_chooser` (one `page.attempt` unit in the default
`paperpush` target), with the real path read through DevTools; links that open
a tab become `expect_popup`; accepted `confirm()` dialogs add `page.once("dialog", ...)`;
iframes become `frame_locator(...)`; password fields are always a `password`
variable and never written out.

## Debugging

Use `page.pause()` to add a breakpoint in code, and stop before the final
submit control during `paperpush codegen`.

For iterating on the script, the VS Code debugger is recommended:

- Add a debug configuration in `.vscode/launch.json`
- Set breakpoints in the code (`breakpoint()` or IDE breakpoints)
- Run the debugger

### Recovering a run that gets stuck

`paperpush/venues/recovery.py` lets a run continue after one browser action
fails, instead of the whole run failing. A runner opts in by wrapping its page
once sign-in is done, then labelling each part of the wizard as it goes (see
`paperpush/venues/openrxiv/main.py`):

```python
from ..recovery import guard, step

venue.ensure_signed_in(page, context, debug=debug)  # sign-in stays unguarded
page = guard(page)
step("Authors page")
...
```

A failed action (`click`, `fill`, `check`, `select_option`, `wait_for`, ...) then
goes to the handler that the caller set with `recovery.use_handler(...)`. That
handler fixes the page and continues, retries the action, or aborts. With no
handler set, every failure aborts, so the run behaves as it did before.
`HumanHandler` asks in the terminal; given a `log_dir` (as `submit --recover
by-hand` and the `--ai` fallback do) it logs each failure to `recoveries.jsonl`
with `handler: "by-hand"` and the author's optional note on what they changed.
Every handler writes that log through `recovery_channel.append_log`. When some actions only make sense together,
such as the click that opens a file chooser plus handing it the file, run them as
one unit with `page.attempt("attach ms.pdf", fn)`. Steps run through `_try` are
optional and never go to the handler.

Watch for probes: an action the script expects might fail and handles itself,
such as `try: x.wait_for(timeout=2000) except PWTimeout: ...` or "try selector
A, then B". Put only the probe inside `with unrecovered():`. Otherwise every
expected miss goes to the handler, which under `--ai` is a paid model call.
`first_present` / `first_visible` and `ensure_signed_in` already run unrecovered.

Every runner, including `paperpush/venues/template/template.py`, supports recovery,
and `tests/test_recovery_coverage.py` checks that `supports_recovery` matches
whether the runner calls `guard(page)`. To convert a runner, set `supports_recovery = True` on its `Venue` class,
or `submit --recover` reports that it has no effect there. Then launch the
browser with `args=browser_launch_args()` (from `recovery_channel`) so
`--cdp-port` works. If the portal's final button has a label that isn't
generic, list it in `final_submit_names`, so a recovering agent can never press
it. `paperpush/venues/recovery_channel.py` holds the agent side of all this:
`AgentHandler`, the file protocol the MCP tools `submit_page` / `submit_act` /
`submit_resume` use, and `PageTools`, which carries out every agent action and
enforces the guardrails. `paperpush/venues/recovery_ai.py` is `submit --ai`: an
in-process tool-use loop (tools `look`, `act` and `finish`) with one small session
class per provider. All of them go through the same `PageTools`.

`submit` wraps the agent and AI handlers in `RememberingHandler`, which reuses a
fix within one run. An identical failure whose fix only waited or clicked a
named button/link is replayed without pausing. Any other remembered fix is
handed to the agent as `earlier_fix` (in `paused.json`, snapshots, and the
`--ai` prompt) when the failure matches apart from numbers (upload row 2 after
row 1). Fixes that typed, selected, or uploaded are never replayed, because a
repeated widget would get the previous entry's data.

#### When a run needed recovery: getting the fix to everyone

Each recovery is a bug report waiting to happen: the portal changed, and every
other install will hit the same step. `paperpush recovery-report [DIR]` (MCP:
`recovery_report`) drafts a GitHub issue from a run's `recoveries.jsonl`,
labelled `venue-recovery`. It is redacted in `paperpush/venues/recovery_report.py`:
typed values, `.sub` values, e-mails, paths, URL query strings. The author files it;
nothing is sent automatically. Fixes from these reports ship in a release. Keep the
selectors most likely to break (custom-field ids, button names, widget CSS) as named
constants at the top of the runner, so that fix is usually one line.
Never edit an installed copy of paperpush to fix a run: fix the live page through
the recovery tools, then report it.


## Agent skills

The skills live in `skills/<name>/SKILL.md`, and that copy is the one to edit.
Three things read it:

- **The plugins.** `.claude-plugin/`, `.codex-plugin/` and `.agents/plugins/` make the repo a
  Claude Code and a Codex plugin marketplace (`/plugin marketplace add
  pachterlab/paperpush`), so both install the skills straight from `skills/` on
  GitHub. Bump `version` in both `plugin.json` files with each release.
- **The wheel.** `scripts/sync_agent_docs.py` copies each skill unchanged into
  `paperpush/_skills/`, the same way it copies `AGENTS.md` into
  `paperpush/_docs/`. `paperpush install-skills` (`paperpush/skills.py`)
  installs them from there. Skills in `CONTRIBUTOR_SKILLS` (the ones that need
  a clone) are installed only with `--all` or by name. The pre-commit hook
  re-syncs the copy whenever `skills/` is in a commit.
- **This checkout.** `.claude/skills/<name>` are symlinks to `skills/<name>`,
  so Claude Code loads them here with nothing installed.

A new skill needs its directory in `skills/`, a symlink in `.claude/skills/`,
and a row in the skills table in `AGENTS.md`. `tests/test_skills.py` checks all
three, as well as the package copy.

## Website

`paperpush/web/app.py` is a FastAPI app that wraps the same functions as the CLI and the MCP
server. `paperpush/web/static/` is the page (plain JavaScript, no build step). Two of its
scripts port Python code and must stay in step with it. `tests/test_web.py` checks both
against the Python versions (when Node is installed), so change each pair together:

- `static/subfile.js` ports `parse` / `replace_scalar` / `replace_block` from `paperpush/subfile.py`.
- `static/checks.js` ports the form checks the page runs as the author types (`form.*` in
  `validate._field_checks`, and `requirements_check._text_fields_check`). A check added to
  `LIVE_CHECKS` in `app.py` needs a port there too.

The page puts each check `validate` reports into one of four blocks (`app.check_group`):
checked as you type, checked by Validate, checked only with AI, and not checked (rules
recorded as `not_implemented`).

`static/add-venue.html` / `add-venue.js` is the **Add venue** page (`/add-venue`). Its inputs are generated from `/api/venue-file/schema`, i.e. from the dataclasses through `paperpush/venuefile.py`, and the venue file it downloads is rendered and checked by the server (`/api/venue-file/render`), so the file format lives only in `venuefile.py`. It never writes the venue databases; `paperpush add-venue FILE` does.

`static/viewer.js` holds the reading aids: the side menu of fields, the stat tiles, the
filter and search bar, the description toggle, and the copy buttons. It reads only the DOM
(each field card's `st-*` status class and `data-*` attributes, which `app.js:renderStatus`
keeps current), because "Save this page" inlines it into the standalone HTML an author sends
to a PI or collaborator, where `app.js` does not run.

```bash
pip install -e .[dev]
paperpush-web --open -v
```

## Testing

Local tests only:

```bash
pytest
```

The sample `.sub` files in `tests/sub_files/<venue>/` point at input files
(manuscript, supplement, cover letter, figures, ...) under
`tests/manuscript_files/<venue>/`. Those input files are generated rather than
committed: pytest builds any that are missing at the start of each session. To
build them yourself, e.g. to run `paperpush submit tests/sub_files/<venue>/<venue>.sub`
by hand or from the VS Code debug configs on a fresh clone, or to regenerate
the sample `.sub` files after editing `tests/sample_subfiles.json`, run:

```bash
python tests/sample_subfiles.py
```

Including headless portal tests (requires login credentials):

```bash
# All venues
pytest --run-portal

# Single venue (e.g. Nature)
pytest --run-portal --venue nature

# Portal tests only (skip all offline tests)
pytest --run-portal-only

# Portal tests only, for one venue (or several: arxiv,biorxiv)
pytest --run-portal-only arxiv
```

Specifically run browser-based portal tests (requires a browser and login credentials):

```bash
# All venues
pytest tests/test_submit.py --run-portal -s

# Single venue (e.g. Nature)
pytest tests/test_submit.py --run-portal -s --venue nature

# Specific subfile (ie not default)
pytest tests/test_submit.py --run-portal -s --venue cell --subfile tests/sub_files/cell/cell_figures.sub

# Shorthand: only the submit walkthrough, optionally for named venue(s)
pytest --run-submit-only -s
pytest --run-submit-only arxiv -s
pytest --run-submit-only arxiv,biorxiv -s
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
`submission_forms.json`: that file describes the portal's form, this one the
guidelines. Every slug in either must also be listed in `venues.json`, the
master list (see [Venue databases](#venue-databases)).
An entry is optional: a venue without one is still fully supported, `validate`
simply has no manuscript rules to apply, and `paperpush requirements VENUE`
says so. Add one whenever the guidelines are available.

Every entry uses the same section keys, and each key is optional -- record only
what the guidelines state, and put rules with no structured key in that
section's `notes` list (one factual sentence each):

| section         | what it holds                                                                   |
|-----------------|---------------------------------------------------------------------------------|
| `manuscript`    | `formats`, `max_file_size_mb`, `max_words[_before_refs]`, `max_pages[_before_refs]`, `count_excludes`, `max_display_items`, `line_numbers`, `double_spacing`, `single_file`, `figures_placement`, `anonymized`, `template_required`, `template_layout`, `initial_submission_any_format`, ... |
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
  `submission_forms.json`) to the overrides for that type. The top level holds the primary
  research-article rules. `tests/test_requirements.py` checks every key is a
  real option of the field.

The `$aliases` key at the top of the file maps each canonical section and
statement name to the heading wordings that count as it ("Materials and
Methods" for `Methods`, "Conflict of interest" for `competing_interests`, ...).
Add a wording there when a venue uses a new one.

To keep rules from being reported twice, the checker applies a guideline rule
only when the `submission_forms.json` field does not already enforce the same
thing (`accept`, `max_file_size_mb`, `max_words`, `word_count`, `max_count`,
...); the portal's own limits are the enforced ones and stay in
`submission_forms.json`.

Page limits on a `.tex` manuscript need a rendered PDF: `paperpush.manuscript.build_pdf`
compiles it with `latexmk` (or `pdflatex` + `bibtex`) into a scratch directory,
never writing next to the source, and caches the build for the process. With
no TeX toolchain the limit is reported as unchecked. Tests that need a
toolchain are skipped when none is installed.

`manuscript.initial_submission_any_format` is set only when the guidelines let
the initial submission be in any format (formatting rules apply from revision
or acceptance); it holds that rule in one sentence. `validate` quotes it at the
top of the report, enables the web page's "initial submission (any format)"
checkbox, and `--initial-submission` then reports every error as a warning.
Keep the full wording in `notes` too if it carries other requirements.

`manuscript.template_layout` records the geometry of a mandatory template as
it comes out of LaTeX -- text-block edges, body font size, baseline skip,
running head, review line numbers -- so `validate` can tell when a paper was
squeezed out of it (see `paperpush/template_check.py`). Don't derive the numbers
from the style file: compile the official template unmodified, in its
submission/review mode, and measure it with the same code `validate` uses:

```bash
python scripts/measure_template.py path/to/compiled-template.pdf \
    --running-head "Under review as a conference paper at ICLR 2027"
```

Paste the printed object into the venue's `manuscript` section. Refresh it
whenever the venue publishes a new year's style files (the running head usually
changes with the year).

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

`scripts/scheduled_guidelines.sh` is the cron entry point (PATH for `claude`,
the `paperpush` conda env, a lock so runs never overlap, logs in
`.guideline_cache/logs/`). It is scheduled monthly on the maintainer's machine
rather than in GitHub Actions because datacenter addresses are bot-walled by
several publishers and the agent runs on the local Claude Code subscription:

```
0 3 1 * * /home/jrich/Desktop/paperpush/scripts/scheduled_guidelines.sh
```

## Venue data updates without a release

The venue data -- `venues.json`, `submission_forms.json`,
`manuscript_requirements.json`, their three schemas, and `paperpush/venues/_assets/*` -- is published on its own, so a data
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

- `venues.json` (`merge_published_info` in `paperpush/database.py`): the
  general metadata is data only, so changes reach users. A new venue is added
  when it is validate-only (no published form); a new venue with a form waits
  for the release that brings its runner.
- `submission_forms.json` (`merge_published` in `paperpush/database.py`): a
  runner is written against its venue's field ids and types. So labels, help text,
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

A source checkout uses the published venue data just as a pip install does
(refreshed daily, merged onto the checkout's files), so a clone gets data fixes
without a `git pull`; new runners and the venues that need them still come with
`git pull`. When you edit the venue data, opt in to your local files with
`PAPERPUSH_VENUE_DATA=bundled`, or published entries for existing venues will
override your edits. The tests and the generator scripts set it themselves. A
directory path reads the files from there instead. `PAPERPUSH_OFFLINE=1` uses the cache but never fetches, and
`PAPERPUSH_VENUE_DATA_URL` points at another published copy.

## Formatting

```bash
black . -l 99999
```

## CI/CD

GitHub Actions (`.github/workflows/`). `ci.yml` runs on every push and pull
request, `docs.yml` on pushes to `main` and every pull request, and all three allow manual runs from the Actions tab.
None touches a live portal, so no session secret is needed.

- `ci.yml` — runs `pytest` (real-portal tests are skipped) and checks that
  `venues.md` / `README.md`, the three schemas, `docs/schemas/` and
  `paperpush/subfile_templates/` are in sync with their generators.
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

## Venue databases

A venue's data lives in three files under `paperpush/`, so a venue can be
supported by `validate`, by `submit`, or both:

- `venues.json`, the master list. A venue exists exactly when it has an entry
  here: slug, name, type, homepage, `anonymous`, `deprecated`, ... (schema:
  `VenueInfo` in `paperpush/database.py`).
- `submission_forms.json`, the submission form: the fields the portal asks for,
  its URL and upload limits (schema: `SubmissionForm` and `Field`). A venue with
  a form and a runner under `paperpush/venues/` supports `submit`.
- `manuscript_requirements.json`, the author guidelines (see
  [Manuscript requirements](#manuscript-requirements)). A venue with an entry
  supports `validate`'s manuscript checks.

Support is worked out from the files rather than recorded as flags:
`Venue.supports_validate` and `Venue.supports_submit` in
`paperpush/database.py`, shown by `paperpush --venues` and in `venues.md`. A
validate-only venue (no form) gets `default_form()`: title, abstract, keywords,
authors, an article-type choice when its requirements have `article_types`, and
the manuscript, figure, table, supplementary and cover-letter files. So
`subfile`, `autofill` and `validate` work for it, while `submit` and `login`
refuse it. `tests/test_internals.py` checks that every form and requirements
entry names a listed venue.

Some limits are recorded in both `submission_forms.json` (on a portal field)
and `manuscript_requirements.json` (from the guidelines): the abstract and
title lengths, keyword counts, per-file size caps, the total upload cap,
accepted file formats, whether a cover letter is required, and per-article-type
page and word limits. `python scripts/check_venue_consistency.py` lists every
such pair that disagrees, giving both paths and values. It also checks that
each `article_type_field` names a field in the venue's form and that each
`article_types` key is one of that field's options. The pre-commit hook runs it
whenever a venue database is part of the commit. A mismatch is only a warning,
because a venue's portal and guidelines sometimes really do disagree (the
guidelines say 1000 characters, the portal stops at 998). Reconcile the value
when one file is wrong; when the venue itself disagrees, keep both, since
`validate` enforces the portal value when both exist. `--strict` makes it exit
1, and `--all-formats` also lists formats the portal accepts beyond the
guidelines (normally expected, so hidden by default).

To add a validate-only venue, add its entry to `venues.json` and its rules to
`manuscript_requirements.json`. It needs no code, so it also reaches installed
copies through the published venue data without a release.

To deprecate a venue, add the field `deprecated:true` to its entry in `paperpush/venues.json`.

### Field sections in `.sub` templates

`paperpush subfile` groups every venue's fields into the same sections, in this
order: `type`, `files`, `metadata`, `authors`, `reviewers`, `declarations`,
`additional` (`SECTION_TITLES` in `paperpush/database.py`; layout code in
`ordered_fields()` in `paperpush/subfile.py`). A field's section comes from, in
order of precedence:

1. its own `"section"` key in `submission_forms.json`;
2. `paperpush/common_fields.json`, which gives a section to the field ids many
   venues share (`title`, `abstract`, `manuscript_file`, `reviewers`, ...) and
   ranks them within it;
3. otherwise `additional`.

Within a section, the fields `common_fields.json` ranks come first, in rank
order, then the rest in the venue's own order. A field whose `required_if`
target would come after it is moved to just after the target.
`submission_forms.json` itself stays in portal order (the order you see when
clicking through the form), and `paperpush subfile --venue-order` renders that
order unchanged. Runners and validation read fields by id, so the layout never
affects them.

When you add a field, either reuse a common id (it is placed automatically) or
give the field a `"section"`. A field id used by several venues belongs in
`common_fields.json` instead. `tests/test_subfile.py` fails if a field is
placed by neither. After editing either file, run
`python scripts/gen_subfile_templates.py` to refresh
`paperpush/subfile_templates/` (the pre-commit hook and CI check this).

Every venue also declares `anonymous` (`true` for double-blind review, `false` otherwise; an entry without it counts as `false`). An anonymous venue gets the identifying-information check (`paperpush/anonymity.py`) on every `paperpush validate`: the attachments and any linked anonymous.4open.science repositories are searched for the authors' details. Set it for a new venue from its reviewing policy.