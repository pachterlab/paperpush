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