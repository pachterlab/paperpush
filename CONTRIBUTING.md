# Contributing

## Adding a submission venue

See the YouTube tutorial [PaperPush Contributor Tutorial](https://youtu.be/hn1RNcvHuYw)

To add support for a venue (journal, preprint server, conference, or other), provide the following components. `python scripts/add_venue.py` writes the first two entries interactively.

1. Venue listing: an entry in `paperpush/venues.json`, the master list, with the venue's general metadata (name, type, homepage, `anonymous`, ...).
2. Submission form (for `submit`): an entry in `paperpush/submission_forms.json` with the submission fields and constraints.
3. Login and submit scripts (for `submit`): a `Venue` subclass in `paperpush/venues/<portal>/<venue>.py`, exposing one module-level `VENUE` instance.
4. Manuscript requirements (for `validate`): an entry in `paperpush/manuscript_requirements.json` with what the venue's author guidelines require of the manuscript itself (formats, length limits, required sections and statements, title-page items, figure rules, references, etc.).

A venue needs 1 plus either 2-3 (submit), 4 (validate), or both. A validate-only venue (1 and 4) needs no code: it gets a default form with the title, authors, and manuscript files that `validate` reads.
5. Unit test(s): an entry named <slug> in `tests/sample_subfiles.json` to generate a sample submission file for unit testing. Optionally, more subfiles can be created for additional unit testing.

Each `Venue` subclass must implement the following:
- set `slug`: its key in `venues.json` and `submission_forms.json`
- implement **`submit()`**: drive the submission wizard, stopping before the final submit.
- for a portal that requires an account, set `logged_in_names` to accessible names of a signed-in-only control and implement **`login()`** to drive the login process.
- for a public portal with no author login, set `requires_login = False` and `supports_session_capture = False`; keep a `login()` method that raises `NotImplementedError` so the uniform venue interface remains explicit. The CLI and MCP server then skip credentials automatically. See `paperpush/venues/editflow/main.py`.

See `paperpush/venues/template/template.py` for a copy-ready starting point.

It is easiest to fill out these forms by making an account for the venue of interest (if you do not have one already), running `playwright codegen <login_url>`, clicking through the submission portal, then copying the generated code into `login()` and `submit()`. You can then replace hard-coded values with values and conditions that can be derived from the submission_forms.json fields.

See [DEVELOPMENT.md](DEVELOPMENT.md) for tips on using playwright, debugging, and running unit tests.

## Before opening a pull request

- Run `python tests/sample_subfiles.py` to generate sample subfiles for the new venue.
- Run `python scripts/gen_venues_schema.py` if you changed the dataclasses behind `venues.json`, `submission_forms.json` or `manuscript_requirements.json` (CI checks the schemas are in sync), and `pytest tests/test_requirements.py` to check the new requirements entry loads and matches the venue's form.
- Run `pytest --run-portal -s --venue VENUE` and make sure that no new failures are introduced. (Requires a browser and login credentials for the venue.)
  - If you modify submission scripts that affect other venues, run `pytest --run-portal` to check all venues. (Requires a browser and login credentials for all venues.)
- Optionally, add "VENUE": "DATE" to `tests/submit_walkthrough_status.json`, and run `python scripts/gen_readme_venues.py` to reflect that it works in `venues.md`
- Format with `black . -l 99999`

Thank you for helping make venue submission a single click.
