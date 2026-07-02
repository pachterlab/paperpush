# Portal page fingerprints

Committed baselines for `tests/test_portal_drift.py`. One JSON file per venue
(`biorxiv.json`, `bioinformatics.json`), each keyed by page name (`login`,
`first_step`). Every page maps to a structural fingerprint produced by
`tests/drift.py`'s `fingerprint`: the sorted form-control names, button
labels, field labels, and headings of that page, with per-session tokens and
hidden inputs stripped out.

The drift test diffs the live portal page against these files and fails when the
structure has changed, catching a portal redesign before it breaks a real
submission.

## Generating / refreshing baselines

There is no baseline until you capture one against the live portal:

```bash
# login-page baselines need only network access:
pytest --run-portal --update-snapshots tests/test_portal_drift.py::test_login_page_drift

# first-wizard-step baselines need to be signed in (saved session, or
# PAPERPUSH_TEST_USERNAME / PAPERPUSH_TEST_PASSWORD). This creates a
# dummy draft on the portal:
pytest --run-portal --update-snapshots tests/test_portal_drift.py::test_first_step_drift
```

Review the resulting diff, confirm the change is one the portal actually made
(not a logged-out page or a transient banner), then commit the updated JSON.

When the test later fails with a structural diff, that is the signal to update
the matching selector(s) in `paperpush/venues/<venue>.py`, then refresh
the baseline the same way.
