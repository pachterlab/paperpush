---
name: paperpush-add-venue
description: >-
  Give a venue full `paperpush submit` support: a Venue class with Playwright
  login() and submit() methods, the portal's submission fields in
  submission_forms.json, a sample .sub with ground-truth documents plus unit
  tests, and a live run through the portal that stops before the final submit.
  For a venue that already has a runner, run its existing tests (offline and
  live), fix what breaks, and write any tests that are missing. Triggers: "add
  <venue> to paperpush", "support submitting to <journal>", "write the runner
  for <venue>", "fix <venue>'s submit", "<venue>'s tests are failing".
---

# Add a venue with submit support (or fix one)

**Needs a clone of the paperpush repo** (`git clone
https://github.com/pachterlab/paperpush` and `pip install -e .[dev]`), run from
its root. This skill edits the venue databases, runner code, and tests, and
uses the repo's `scripts/` and `tests/`. Working from a pip install? Ask the
user to clone the repo first.

Before writing any code, read `README.md` ("Adding new venues", "For AI
agents"), `CONTRIBUTING.md` ("Adding a submission venue"), `DEVELOPMENT.md`
(codegen, recovery, testing, venue databases), and `AGENTS.md`. CLAUDE.md
requires it. They are the authority, and this skill only puts their steps in
order.

## 0. Pick the path

```
paperpush venues | grep -i <name>
ls paperpush/venues/*/ | grep -i <slug>
```

- **Venue has a runner** → go to **Path B: existing venue**.
- **Otherwise** → **Path A: new venue**. If `paperpush requirements <slug>`
  shows no entry, run the `paperpush-add-venue-validate` skill first (Skill
  tool). That records the guidelines and the `[venues]` entry. Then come back.

## Credentials (both paths)

You need a working account to explore the portal and test the runner.
**Never ask for a password in chat, and never write one to a file or
command line.**

1. Check: `paperpush login <slug> --status` (after the runner exists), or
   `paperpush login --list`.
2. If there is no stored login, find the venue's **account registration
   link** on its site and give it to the user. Ask them to create an account,
   or to tell you if they already have one. Ask whether they sign in with
   ORCID.
3. Once `login()` is written (step A4), ask the user to run
   `! paperpush login <slug>` in this session. This prompts them for the
   password, verifies it with your `login()` in a real browser, and stores
   it in the OS keyring (or paperpush's encrypted file fallback). If it fails,
   the error tells you what to fix in `login()`. Ask them to run it again
   after the fix.
4. Until then, explore the signed-in portal through a browser the user signs
   into themselves: `paperpush codegen <login_url>`, or a headed Playwright
   page you open and they type into. Save the session state to a scratch file
   for later runs.

Portals with no author accounts (see `paperpush/venues/editflow/main.py`)
skip all of this: set `requires_login = False` and
`supports_session_capture = False`.

## Path A: new venue

**A1. Find the portal family.** Many journals share one portal engine:
`scholarone`, `editorialmanager`, `snapp`, `editflow`, `nature`, `openrxiv`,
`openreview`. If the venue uses one, write a thin leaf that reuses its `main`
engine (see `paperpush/venues/nature/nature_machine_intelligence.py`), not a
new engine.

**A2. Walk the portal and record it.** With the user's signed-in session, go
through the whole submission wizard with test values and stop before the
final submit control. The best tool is `paperpush codegen --venue <slug>`
once the venue is in `venues.json`, or `paperpush codegen <login_url>`
before. For each page, record:
- every field: its label, type, required or not, options word for word,
  limits, and accepted file types;
- the page order, the buttons between pages, and any widgets (author
  editors, upload tables, file-type dropdowns, iframes, confirm dialogs);
- the exact label of the control that sends the submission.

If the walk makes a draft, title it something obviously fake (for example
"PAPERPUSH TEST – DO NOT REVIEW") and tell the user it exists.

**A3. Write the form.** Run `paperpush add-venue --template --from <slug> -o
<scratch>/<slug>.txt` (or `--template <slug>` if the venue is not in
`venues.json` yet). Set `[form] include: yes`, `submission_url`, and one
`[field]` block per portal field **in portal order**. Reuse common ids
(`title`, `abstract`, `authors`, `manuscript_file`, ...). Give every other
field a `section`. Then:

```
paperpush add-venue <scratch>/<slug>.txt --dry-run
paperpush add-venue <scratch>/<slug>.txt --overwrite
python scripts/check_venue_consistency.py
python scripts/gen_subfile_templates.py
```

**A4. Write the runner.** Copy `paperpush/venues/template/template.py` to
`paperpush/venues/<portal>/<slug>.py` and follow its `#!!!` markers:
- `slug`, `logged_in_names` (controls only a signed-in user sees),
  `final_submit_names` (the send label from A2), and `supports_recovery =
  True`. Add `supports_orcid_login` only if you wrote the ORCID branch.
- `login()`: fill the sign-in form, and raise `VenueLoginError` with a clear
  message when the dashboard never loads.
- `submit()`: keep the template's skeleton (`open_run_context`,
  `ensure_signed_in`, then `page = guard(page)`). Start each wizard page with
  `step("<page name>")`. Put an upload click and its file chooser in one
  `page.attempt(...)`. Put each probe that is expected to fail in
  `with unrecovered():`. Read every value from `values` by field id.
- Put the selectors that are likely to break (portal ids, button names,
  widget CSS) in named constants at the top of the file.
- End the module with `VENUE = <Class>()`. Discovery finds it by that
  name.
- Stop before the final submit and hand off with `hold_open`, as the
  template does. **Never click the send control, in code or by hand.**

Then do credentials step 3 and have the user run `paperpush login <slug>`.

**A5. Ground-truth sample and tests.**
- Add `<slug>.sub.txt` to `tests/sample_subfiles.json`, with values for every
  required field and each option branch you want covered. Its files live
  under `tests/manuscript_files/<slug>/`. They are generated, not
  committed, so run `python tests/sample_subfiles.py` to build them and to
  write `tests/sub_files/<slug>/<slug>.sub.txt`. Add more variants
  (`<slug>_figures.sub.txt`, ...) for branches such as article types and
  optional uploads.
- Write `tests/test_<slug>.py` with offline tests in the style of
  `tests/test_nature_machine_intelligence.py`: dispatch through
  `get_runner`, form and field invariants, and each tricky helper (author
  rows, file metadata, branch choices) checked against a `Mock` page.

**A6. Test until it passes**, then go to **Verify** below.

## Path B: existing venue

1. Run its offline tests first:
   ```
   pytest tests/test_<slug>.py tests/test_venue_interface.py tests/test_recovery_coverage.py tests/test_submit.py tests/test_subfile.py -q -k "<slug> or not portal"
   ```
   If `tests/test_<slug>.py` or the `tests/sample_subfiles.json` entry is
   missing, write it as in A5.
2. Check credentials (above), then run it live:
   ```
   python tests/sample_subfiles.py
   pytest tests/test_submit.py --run-portal -s --venue <slug>
   ```
3. For each failure, open the live page at the failing step and compare it
   with the runner (use the `paperpush-portal-debug` skill for a stall). Make
   the smallest fix, add a regression test, and rerun. If the portal's
   fields changed, update `submission_forms.json` through a venue file
   (A3), and update the runner to match.
4. Check for open `venue-recovery` issues for this venue
   (`gh issue list --label venue-recovery`). They record steps that already
   failed for users.

## Verify (both paths)

```
pytest -q                                                   # whole offline suite
pytest tests/test_submit.py --run-portal -s --venue <slug>  # live walkthrough
python scripts/gen_readme_venues.py                         # status tables
```

The live run must reach the review page with every value entered, and must
stop before the final submit. Ask the user to look at the open browser and
confirm the portal shows what the `.sub` says. Do not call the venue working
until they confirm. Report any step blocked by a CAPTCHA, two-factor prompt,
slow PDF build, or downtime separately; never count an unobserved step as
passing.

## Report

List the files added or changed, the test results (offline and live, with
output on failure), any test draft left in the portal, and anything you could
not automate. Do not commit unless the user asks.

Then, for a new runner or a real fix, suggest sending it upstream as a pull
request, as in `paperpush-add-venue-validate`'s **Share it** step: from a
branch, commit only this venue's files (runner, venue data, regenerated
templates and tables, `tests/test_<slug>.py`, `tests/sample_subfiles.json`
and `tests/sub_files/<slug>/`, `scripts/guideline_fingerprints.json`), then
`gh pr create --repo pachterlab/paperpush`, or open one at
https://github.com/pachterlab/paperpush/compare. Only suggest it: don't
push or open the PR unless the user says yes. Never commit credentials or
saved session state.
