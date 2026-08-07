# Contributing

## Adding a submission venue

See the YouTube tutorial [PaperPush Contributor Tutorial](https://youtu.be/hn1RNcvHuYw)

To add support for a venue (journal, preprint server, conference, or other), provide the following three components:

1. Login and submit scripts: a `Venue` subclass in `paperpush/venues/<portal>/<venue>.py`, exposing one module-level `VENUE` instance.
2. Venue data: an entry in `paperpush/venues.json` with the submission fields and constraints.
3. Unit test(s): an entry named <slug> in `tests/sample_subfiles.json` to generate a sample submission file for unit testing. Optionally, more subfiles can be created for additional unit testing.

Each `Venue` subclass must implement the following:
- set `slug`: its key in `venues.json`
- set `logged_in_names`: a tuple of accessible names of a control that shows only when signed in
- implement **`login()`**: drive the login process
- implement **`submit()`**: drive the submission wizard, stopping before the final submit.

Signing in stays one method. If the venue's sign-in page also offers a "Sign in with ORCID" button, handle it inside `login()` rather than adding a second entry point:
- take an `orcid: bool = False` keyword on `login()`, and when it is set drive that button — click through to ORCID's popup, type the author's ORCID iD and ORCID password, and return once the portal is signed in. Keep the shared parts (loading the page, dismissing cookie banners, the final signed-in check) outside the branch; if the ORCID half is long, put it in a private `_login_orcid()` helper.
- set `supports_orcid_login = True`.
- Do neither otherwise. Callers check `supports_orcid_login` and refuse before signing in, so a venue with no ORCID branch is never passed `orcid=True` and does not need the parameter at all. `paperpush/venues/editorialmanager/main.py` has a worked example.
- Whether the `--orcid` *option* is offered for a venue at all is separate, and comes from its `venue_type` (journals only) plus the exclusions in `paperpush/venues/__init__.py`.

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