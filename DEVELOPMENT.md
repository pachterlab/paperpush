# Development

## Install

```bash
git clone git@github.com:pachterlab/paperpush.git
cd paperpush
pip install -e .[dev]
```

## Adding a new venue

### 1. Fields

Scaffold the venue's fields in `paperpush/venues.json`:

```bash
python3 scripts/add_venue.py
```

Follow the prompts.

To deprecate a venue, add the field `deprecated:true` to its entry in `paperpush/venues.json`.

### 2. Venue class

Copy `paperpush/venues/template.py` to `paperpush/venues/<portal>/<venue>.py`
and register the slug in `SLUG_TO_MODULE` in `paperpush/venues/__init__.py`. (A
portal shared by several venues — Editorial Manager, Nature, ScholarOne, SNAPP,
openRxiv — keeps its Playwright engine in that subpackage's `main` module; each
venue is a thin subclass that sets `slug` + `variant`.)

You only fill in two methods and a few attributes; the base `Venue` class supplies
`is_logged_in`, `ensure_signed_in`, `save_session`, and `session_path`:

- Set `slug` and the login-state markers (`logged_in_names`, plus `logged_in_role` /
  `logged_in_present_means_in` / `login_frame_selector` / `supports_session_capture`
  as needed — see the template's comments).
- **`login`** — for a standard `#user` / `#password` / submit-button form this is one
  call to `paperpush.venues.login.fill_login_form` plus an `is_logged_in` check.
- **`submit`** — record the portal flow, then adapt it into the wizard steps:

  ```bash
  playwright codegen VENUE_LOGIN_URL
  ```

  Click through the submission pages and copy the generated steps into the `submit`
  method (the template has the launch / `ensure_signed_in` / `hold_open` scaffolding
  already). Use `page.pause()` to add a breakpoint in code, and stop before the final
  submit control.

For iterating on the script, the VS Code debugger is recommended:

- Add a debug configuration in `.vscode/launch.json`
- Set breakpoints in the code (breakpoint() or IDE breakpoints)
- Run the debugger

### 3. Tests

Add unit tests, and add at least one sample submission for the venue to
`tests/sample_subfiles.json` (keyed by filename, with the venue slug under
`journal` and the filled-in values under `fields`), then regenerate the
committed fixtures from it:

```bash
python tests/sample_subfiles.py
```

This regenerates the `.sub` files under `tests/sub_files/` **and** builds the
input files each sample references (manuscript, figures, cover letter, ...)
under `tests/manuscript_files/`. Those are produced from the shared
`SampleFiles` factory in `tests/conftest.py` by mapping each referenced
filename to a builder (see `_ASSET_BUILDERS` in `tests/sample_subfiles.py`); you
only touch that map if a sample references a genuinely new kind of file.

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

Update venues.md checklist after running `pytest tests/test_submit.py --run-portal` (included automatically in pre-commit hook and bi-monthly CI job):

```bash
python scripts/gen_readme_venues.py
```

## Formatting

```bash
black . -l 99999
```

## CI/CD

GitHub Actions (`.github/workflows/`). All scheduled jobs also allow manual runs
from the Actions tab. The portal-facing ones share one secret,
`PAPERPUSH_SESSIONS_B64` — a base64 tar.gz of the saved `*_session.json` files
(`( cd ~/.config/paperpush && tar -czf - *_session.json ) | base64`) — restored
by the `restore-portal-sessions` composite action (see each workflow's header).

**On every push / PR**

- `ci.yml` — runs `pytest` (real-portal tests are skipped) and checks that
  `venues.md` / `README.md` and `venues.schema.json` are in sync with their
  generators.

**Scheduled (live portals)**

- `submit.yml` (every 2 months) — Drives each venue with a
  session secret through the real submission wizard headless (stopping before the
  final submit, retrying up to 3× per venue), records ✅ (pass) / ❌ (fail) into
  `tests/submit_walkthrough_status.json`, regenerates the venue tables, and opens
  a PR: it auto-merges when only dates changed and stays open — with the failed
  venues named in a comment — when a venue flips ✅↔❌.
- `fingerprint.yml` (every 2 months) — the complementary signal to the
  walkthrough: diffs each portal's login page and first wizard step against the
  committed fingerprint in `tests/portal_snapshots` and fails loudly if the
  structure changed, catching a portal that drifted *without* breaking submission.
- `nature-categories.yml` (every 6 months) — re-scrapes Nature's subject-category
  tree from the live wizard and opens a PR if it drifted.

The submit walkthrough's pass/fail is the portal-health signal: a portal change
that breaks submission flips the venue to ❌ (and the PR stays open for review).
