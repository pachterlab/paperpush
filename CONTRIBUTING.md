# Contributing

## Adding a submission venue

To add support for a venue (journal, preprint server, conference, or other), provide the following three components:

1. Login and submit scripts: a `Venue` subclass in `paperpush/venues/<portal>/<venue>.py`, exposing one module-level `VENUE` instance.
2. Venue data: an entry in `paperpush/venues.json` with the submission fields and constraints.
3. Unit test(s): an entry named <slug> in `tests/sample_subfiles.json` to generate a sample submission file for unit testing. Optionally, more subfiles can be created for additional unit testing.

Each `Venue` subclass must implement the following:
- set `slug`: its key in `venues.json`
- set `logged_in_names`: a tuple of accessible names of a control that shows only when signed in
- implement **`login()`**: drive the login process
- implement **`submit()`**: drive the submission wizard, stopping before the final submit.

See `paperpush/venues/template.py` for a copy-ready starting point.

It is easiest to fill out these forms by making an account for the venue of interest (if you do not have one already), running `playwright codegen <login_url>`, clicking through the submission portal, then copying the generated code into `login()` and `submit()`. You can then replace hard-coded values with values and conditions that can be derived from the venues.json fields.

See [DEVELOPMENT.md](DEVELOPMENT.md) for tips on using playwright, debugging, and running unit tests.

## Before opening a pull request

- Run `python tests/sample_subfiles.py` to generate sample subfiles for the new venue.
- Run `pytest --run-portal -s --venue VENUE` and make sure that no new failures are introduced. (Requires a browser and login credentials for the venue.)
  - If you modify submission scripts that affect other venues, run `pytest --run-portal` to check all venues. (Requires a browser and login credentials for all venues.)
- Optionally, add "VENUE": "DATE" to `tests/submit_walkthrough_status.json`, and run `python scripts/gen_readme_venues.py` to reflect that it works in `venues.md`
- Format with `black . -l 99999`

Thank you for helping make venue submission a single click.