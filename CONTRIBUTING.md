# Contributing

## Adding a submission venue

### Claude Code (or another AI agent):
- `/paperpush-add-venue Add VENUE (LINK_TO_GUIDELINES) to PaperPush`
  - to add just `validate` and a basic .sub file, without `login`/`submit` support and a comprehensive .sub file: `/paperpush-add-venue-validate Add VENUE (LINK_TO_GUIDELINES) to PaperPush (validate-only)`
  - to add `login`/`submit` support and a comprehensive .sub file to a venue that supports `validate`: `/paperpush-add-venue Add login/submit support for VENUE to PaperPush`
  
You must have an account to the VENUE you are adding for the AI agent to navigate it and convert it into a script. It will ask you to log in on the command line once it implements the login process (if login is required by VENUE). 

As of the time of writing this (November 2026), AI agents are capable of autonomously navigating submission portals and converting them to Playwright scripts, but the process can take a lot of tokens and may stall at times. It can improve the model's efficiency to provide the HTML or Playwright snippet from `paperpush codegen` for a portion where the agent stalls.
  
### Manual
  
See the YouTube tutorial [PaperPush Contributor Tutorial](https://youtu.be/hn1RNcvHuYw)

A venue has the following ocmponents:
1. Venue listing: an entry in `paperpush/venues.json`, the master list, with the venue's general metadata (name, type, homepage, `anonymous`, etc.).
2. Submission form (for `subfile`, `autofill`, basic `validate`, `submit`): an entry in `paperpush/submission_forms.json` with the submission fields and constraints, in the order the portal asks for them. This is what becomes the .sub file. See [Field sections](DEVELOPMENT.md#field-sections-in-sub-templates).
3. Manuscript requirements (for `validate`): an entry in `paperpush/manuscript_requirements.json` with what the venue's author guidelines require of the manuscript itself (formats, length limits, required sections and statements, title-page items, figure rules, references, etc.).
4. Login and submit scripts (for `login`, `submit`): a `Venue` subclass in `paperpush/venues/<portal>/<venue>.py`. See `paperpush/venues/template/template.py` for a copy-ready starting point, or view any of the instances of supported journals (`paperpush --venues submit`). Each `Venue` subclass must implement the following:
- set `slug`: its key in `venues.json`
- implement **`submit()`**: drive the submission wizard, stopping before the final submit.
- for a portal that requires an account, set `logged_in_names` to accessible names of a signed-in-only control and implement **`login()`** to drive the login process.
- for a public portal with no author login, set `requires_login = False` and `supports_session_capture = False`; keep a `login()` method that raises `NotImplementedError` so the uniform venue interface remains explicit. The CLI and MCP server then skip credentials automatically. See `paperpush/venues/editflow/main.py`.
5. Test fixtures (for login, submit): an entry in tests/sample_subfiles.json with a filled-in sample submission for the venue. If a file field points to a new kind of file (a filename stem with no builder), add that stem to _ASSET_BUILDERS in tests/sample_subfiles.py.

All new venues need 1-2. 3 (extensive `validate` support) is highly recommended, and 4-5 (`submit` support) are recommended if feasible.

To implement 4, I recommend `paperpush codegen`. See [DEVELOPMEND.md](DEVELOPMENT.md#recording-a-runner-with-paperpush-codegen).

See [DEVELOPMENT.md](DEVELOPMENT.md) for tips on using playwright, debugging, and running unit tests.

### Before opening a pull request

Run the following scripts
- `python scripts/gen_subfile_templates.py`: refresh `paperpush/subfile_templates/`.
- If you changed the dataclasses behind one of the JSON database files:
  - `python scripts/gen_venues_schema.py`: CI checks the schemas are in sync
  - `pytest tests/test_requirements.py`: check the new requirements entry loads and matches the venue's form
- If `submit` supported: 
  - `python tests/sample_subfiles.py`: generate sample subfiles for the new venue
  - `python scripts/check_venue_consistency.py VENUE`: enforce agreement between `manuscript_requirements.json` and `submission_forms.json`
  - `pytest --run-portal -s --venue VENUE`: Run unit tests make sure that no new failures are introduced. (Requires a browser and login credentials for the venue).
    - If you modify submission scripts that affect other venues, run `pytest --run-portal` to check all venues. (Requires a browser and login credentials for all venues).
    - Optionally, add "VENUE": "DATE" to `tests/submit_walkthrough_status.json`, and run `python scripts/gen_readme_venues.py` to reflect that it works in `venues.md`
- `black . -l 99999`: formatting
  
  Thank you for your contribution!
