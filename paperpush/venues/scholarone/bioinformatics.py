"""Bioinformatics submission runner (Playwright).

Bioinformatics (Oxford University Press) submits through ScholarOne
(``mc.manuscriptcentral.com/bioinformatics``). The shared platform machinery
lives in :mod:`paperpush.venues.scholarone`; this module keeps only the
Bioinformatics-specific pieces: :func:`submit_bioinformatics`, the upload step's
file-type labels, and the reproducibility checklist. Sign-in is inherited from
:class:`ScholarOneVenue`.

This runner is still close to the raw ``playwright codegen`` recording, so many
controls (``CUSTOM_FIELD_RESPONSE_ID...`` ids) are specific to the recorded
manuscript. Session handling mirrors the other runners (saved session reused,
``new_session`` forces a fresh sign-in, ``debug`` opens the Inspector).
"""

from __future__ import annotations

import logging

from playwright.sync_api import sync_playwright

from ...database import get_venue
from ...validate import parse_authors
from ..common import (DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts,
                      hold_open, hold_open_on_failure, open_run_context)
from ..common import session_path as _session_path
# Reuse the shared ScholarOne platform engine (author/keyword/reviewer/parse
# helpers). Sign-in is inherited from ScholarOneVenue.
from . import main as scholarone
from .main import (  # noqa: F401 -- re-exported as this module's public API
    AUTHOR_LINK, ScholarOneLoginError, _add_authors, _add_keywords,
    _add_reviewers, _answer_sanctions, _check_yes_no, _split_figures,
    _split_keywords, _split_reviewers, _yes)

logger = logging.getLogger(__name__)

_VENUE = get_venue("bioinformatics")
PORTAL_URL = _VENUE.submission_url

# Labels offered by the per-file "file type" dropdown on the upload step, read
# from venues.json; _upload_files asserts the live dropdown still matches.
FILE_TYPE_OPTIONS = _VENUE.file_type_options or []

# The specific labels this runner assigns (each must be in FILE_TYPE_OPTIONS;
# the assertion catches a label drifting out of the database at import time).
TYPE_MAIN_DOCUMENT = "Main Document (Word or Pdf)"
TYPE_FIGURE = "Figure"
TYPE_TABLE = "Table"
TYPE_SUPPLEMENTARY = "Supplementary Material for Review & Online Publication"
assert set((TYPE_MAIN_DOCUMENT, TYPE_FIGURE, TYPE_TABLE, TYPE_SUPPLEMENTARY)).issubset(FILE_TYPE_OPTIONS), "bioinformatics file_type_options in venues.json is missing a label this runner uses"


# Custom-field ids for the free-text follow-ups on the final declarations step
# (each shown only when its yes/no question is answered Yes).
CF_PREVIOUS_MS_ID = "27461"  # resubmission: previous manuscript number (text input)
CF_CONFLICT_STATEMENT = "27471"  # conflict of interest: "If yes, please state" (textarea)

# Custom-field ids for the reproducibility checklist (addressed by name as
# ``CUSTOM_FIELD_RESPONSE_ID<id>``; re-record if the portal regenerates them).
CF_ALGORITHM_CENTRAL = "1186859"
CF_CODE_IN_TEXT = "1186863"
CF_CODE_REPO_LINK = "2321314"
CF_SOFTWARE_INSTALLABLE = "1186865"
CF_SOFTWARE_TESTED = "1186866"
CF_TEST_DATASET = "1186868"
CF_DATA_DOCUMENTED = "1186869"
CF_DATA_SOURCES_LINK = "2321325"
CF_IS_ML_DL = "1186871"
CF_ML_SPLITS = "1186872"
CF_ML_TRANSFORMS = "1186873"
CF_ML_METRICS = "1186874"


def _fill_custom_text(page, field_id: str, value: str) -> None:
    """Fill a ScholarOne custom-field free-text input addressed by its id."""
    box = page.locator(f'input[name="CUSTOM_FIELD_RESPONSE_ID{field_id}"]')
    box.click()
    box.fill(value)


def _fill_custom_textarea(page, field_id: str, value: str) -> None:
    """Fill a ScholarOne custom-field <textarea> addressed by its id."""
    box = page.locator(f'textarea[name="CUSTOM_FIELD_RESPONSE_ID{field_id}"]')
    box.click()
    box.fill(value)


def _answer_declarations(page, values: dict) -> None:
    """Answer the four declaration questions on the file-upload / declarations step.

    All four default to the conservative answer, so a blank ``.sub`` reproduces
    the portal defaults (matching the previously hard-coded recording):

    * ``funding_to_report`` -- a named Yes/No radio pair ("Is there funding to
      report for this submission?").
    * ``resubmission`` and ``has_conflict_of_interest`` -- these share the page's
      generic "radiobutton" radios in document order: resubmission Yes/No at
      indices 0/1, conflict of interest Yes/No at 2/3 (mirrors the sanctions
      pattern). Each has an optional free-text follow-up filled only when the
      question is Yes: the previous manuscript number and the conflict-of-interest
      statement, respectively.
    * ``cited_unpublished_works`` -- a two-option radio addressed by its row label
      ("Yes, and I have included the relevant supplementary files" vs. "No, I have
      not cited any works that are unpublished or in press").
    """
    # Funding to report: a named Yes/No radio pair (distinct from the generic
    # "radiobutton" groups below, which is why it is addressed by its label).
    funding_yes = _yes(values.get("funding_to_report", "no"))
    page.get_by_role("radio", name="Yes" if funding_yes else "No").check()

    # Resubmission (indices 0/1) and conflict of interest (indices 2/3) share the
    # page's generic "radiobutton" radios in document order.
    radios = page.get_by_role("radio", name="radiobutton")
    resubmission_yes = _yes(values.get("resubmission", "no"))
    radios.nth(0 if resubmission_yes else 1).check()
    if resubmission_yes:
        previous_id = values.get("previous_manuscript_id", "").strip()
        if previous_id:
            _fill_custom_text(page, CF_PREVIOUS_MS_ID, previous_id)

    conflict_yes = _yes(values.get("has_conflict_of_interest", "no"))
    radios.nth(2 if conflict_yes else 3).check()
    if conflict_yes:
        statement = values.get("conflict_of_interest_details", "").strip()
        if statement:
            _fill_custom_textarea(page, CF_CONFLICT_STATEMENT, statement)

    # Cited unpublished / in-press works: a two-option radio picked by row label.
    if _yes(values.get("cited_unpublished_works", "no")):
        row_name = "radiobutton  Yes, and I have included the relevant supplementary files"
    else:
        row_name = "radiobutton  No, I have not cited any works that are unpublished or in press"
    page.get_by_role("row", name=row_name, exact=True).get_by_label("radiobutton").check()


def _answer_reproducibility(page, values: dict) -> None:
    """Answer the reproducibility checklist on the final declarations step.

    Each question is a Yes/No group from the matching ``.sub`` boolean (default
    "no"); two optional link inputs are filled only when provided. The three
    ML/DL questions are answered only when ``is_ml_dl_paper`` is yes (the portal
    hides them otherwise).
    """
    _check_yes_no(page, CF_ALGORITHM_CENTRAL, _yes(values.get("algorithm_or_software_central", "no")))
    _check_yes_no(page, CF_CODE_IN_TEXT, _yes(values.get("code_in_text", "no")))
    repo_link = values.get("code_repository_link", "").strip()
    if repo_link:
        _fill_custom_text(page, CF_CODE_REPO_LINK, repo_link)

    _check_yes_no(page, CF_SOFTWARE_INSTALLABLE, _yes(values.get("software_installable", "no")))
    _check_yes_no(page, CF_SOFTWARE_TESTED, _yes(values.get("software_tested", "no")))
    _check_yes_no(page, CF_TEST_DATASET, _yes(values.get("test_dataset_provided", "no")))
    _check_yes_no(page, CF_DATA_DOCUMENTED, _yes(values.get("data_documented", "no")))
    data_link = values.get("data_sources_link", "").strip()
    if data_link:
        _fill_custom_text(page, CF_DATA_SOURCES_LINK, data_link)

    is_ml_dl = _yes(values.get("is_ml_dl_paper", "no"))
    _check_yes_no(page, CF_IS_ML_DL, is_ml_dl)
    if is_ml_dl:
        logger.info("ML/DL paper: answering the three ML reproducibility questions")
        _check_yes_no(page, CF_ML_SPLITS, _yes(values.get("ml_train_test_splits", "no")))
        _check_yes_no(page, CF_ML_TRANSFORMS, _yes(values.get("ml_data_transformations", "no")))
        _check_yes_no(page, CF_ML_METRICS, _yes(values.get("ml_metrics_uncertainty", "no")))


def _upload_files(page, files: list[tuple[str, str]]) -> None:
    """Attach each ``(path, file_type)`` on the upload step and set its file type.

    Chooser triggers are numbered "Select File N" with a matching per-row dropdown
    at ``select[file-index="N"]`` (zero-based). Every ``file_type`` must be in
    FILE_TYPE_OPTIONS (checked before uploading); the first row's live dropdown is
    then checked against FILE_TYPE_OPTIONS to catch a ScholarOne change.
    """
    for _, file_type in files:
        if file_type not in FILE_TYPE_OPTIONS:
            raise ValueError(f"Unknown ScholarOne file type {file_type!r}; expected one of {FILE_TYPE_OPTIONS}")

    for index, (path, file_type) in enumerate(files):
        logger.info("Attaching file %d (%s) as %s", index + 1, path, file_type)
        with page.expect_file_chooser() as fc_info:
            page.get_by_text(f"Select File {index + 1}").click()
        fc_info.value.set_files(path)
        dropdown = page.locator(f'select[file-index="{index}"]')
        if index == 0:
            live = dropdown.locator("option").all_inner_texts()
            live = [opt for opt in live if not opt.startswith("Choose File Designation")]
            offered = [opt.strip() for opt in live if opt.strip()]
            if offered != FILE_TYPE_OPTIONS:
                raise RuntimeError("ScholarOne file-type options changed; update bioinformatics " f"file_type_options in venues.json. Live: {offered}; expected: {FILE_TYPE_OPTIONS}")
        dropdown.select_option(label=file_type)


def session_path():
    """Path to the saved Bioinformatics session (Playwright storage_state)."""
    return _session_path("bioinformatics")


def submit_bioinformatics(values: dict, headless: bool = False, debug: bool = False, new_session: bool = False, timeout: float = DEFAULT_TIMEOUT_SECONDS, keep_open_on_failure: bool = True, *, venue) -> None:
    """Open Bioinformatics (ScholarOne) and drive the submission wizard.

    ``values`` is the parsed ``.sub`` field map. Sign-in is handled by the
    inherited ``venue.ensure_signed_in``; ``new_session=True`` discards any saved
    session first and ``debug=True`` forces a headed browser and opens the
    Playwright Inspector at the first wizard action.
    """
    # The Inspector needs a visible browser; debugging headless makes no sense.
    if debug:
        headless = False

    manuscript_file = values["manuscript_file"].strip()

    # Upload files in attach order: manuscript, figures, tables, then optional
    # supplementary. Each pairs a path with its file-type label.
    upload_files: list[tuple[str, str]] = [(manuscript_file, TYPE_MAIN_DOCUMENT)]
    upload_files += [(figure, TYPE_FIGURE) for figure in _split_figures(values.get("figures", ""))]
    upload_files += [(table, TYPE_TABLE) for table in _split_figures(values.get("tables", ""))]
    supplementary_file = values.get("supplementary_file", "").strip()
    if supplementary_file:
        upload_files.append((supplementary_file, TYPE_SUPPLEMENTARY))

    # Authors, parsed with the Bioinformatics column order declared in venues.json
    # (email | prefix | name | institution | country | city | corresponding).
    author_field = next((f for f in _VENUE.fields if f.type == "authorlist"), None)
    authors = parse_authors(values.get("authors", ""), author_field.fields if author_field else None)

    logger.info("Starting Bioinformatics (ScholarOne) submission run " "(headless=%s, debug=%s, manuscript=%s, %d file(s) to upload, %d author(s))", headless, debug, manuscript_file, len(upload_files), len(authors))

    with sync_playwright() as playwright, hold_open_on_failure(headless=headless, keep_open=keep_open_on_failure):
        browser = playwright.chromium.launch(headless=headless)
        context = open_run_context(browser, venue.session_path(), new_session=new_session)
        apply_default_timeouts(context, timeout)
        page = context.new_page()

        venue.ensure_signed_in(page, context, debug=debug)

        if debug:
            page.pause()

        logger.debug("Signed in; starting a new submission")
        page.wait_for_timeout(3000)
        page.get_by_role("button", name="Reject All").click()
        page.get_by_role("link", name=AUTHOR_LINK).click()
        page.get_by_role("link", name=" Start New Submission").click()
        page.get_by_role("link", name="Begin Submission").click()
        page.get_by_role("link", name="or continue without pre-").click()

        article_type = values.get("article_type", "Original Paper").strip()
        page.get_by_role("radio", name=f"Choice {article_type}").check()
        # Guard required fields so a blank .sub value doesn't overwrite the portal.
        title = values.get("title", "").strip()
        if title:
            page.get_by_role("textbox", name="DOCUMENT_TITLE").click()
            page.get_by_role("textbox", name="DOCUMENT_TITLE").fill(title)
        abstract = values.get("abstract", "").strip()
        if abstract:
            page.get_by_role("textbox", name="DOCUMENT_ABSTRACT").click()
            page.get_by_role("textbox", name="DOCUMENT_ABSTRACT").fill(abstract)
        page.get_by_label("Select...").select_option(label=values.get("section", "").strip())

        # Two yes/no radio groups follow in document order: Next Generation
        # Sequencing first (index 0 = Yes, 1 = No), then Machine Learning
        # (index 2 = Yes, 3 = No). Both are stored as "yes"/"no" in the .sub.
        ngs_yes = values.get("next_gen_sequencing", "no").strip().lower() == "yes"
        ml_yes = values.get("machine_learning", "no").strip().lower() == "yes"
        radios = page.get_by_role("radio", name="radiobutton")
        radios.nth(0 if ngs_yes else 1).check()
        radios.nth(2 if ml_yes else 3).check()
        page.get_by_role("button", name="Save & Continue ").click()

        _upload_files(page, upload_files)
        page.get_by_role("link", name="Upload Selected Files").click()
        page.get_by_role("button", name="Save & Continue ").click()

        _add_keywords(page, _split_keywords(values["portal_keywords"]), portal=True)
        _add_keywords(page, _split_keywords(values.get("keywords", "")), portal=False)
        page.get_by_role("button", name="Save & Continue ").click()

        # Sanctions / export-control declaration. All three default to "No"
        # (the portal default), so this is a no-op unless the .sub answers Yes.
        _answer_sanctions(page, values)

        # Authors step: drop the error-prone pre-filled author, add each .sub
        # author by email lookup (creating any unknown ones by hand), and assign
        # the corresponding author.
        _add_authors(page, authors, venue_label="bioinformatics")

        page.get_by_role("checkbox", name="checkbox").check()
        page.get_by_role("button", name="Save & Continue ").click()

        _add_reviewers(page, _split_reviewers(values.get("reviewers", "")), venue_label="bioinformatics")
        page.get_by_role("button", name="Save & Continue ").click()

        # File upload step.
        page.get_by_label("Select File").set_input_files(values["cover_letter"].strip())
        page.get_by_role("button", name=" 2. Attach File").click()

        # Declarations (funding / resubmission / conflict of interest / cited
        # unpublished works), driven from the .sub values rather than the
        # recorded test answers.
        _answer_declarations(page, values)

        num_pages = values.get("num_pages", "").strip()
        if num_pages:
            page.locator('input[name="CUSTOM_FIELD_RESPONSE_ID474331"]').click()
            page.locator('input[name="CUSTOM_FIELD_RESPONSE_ID474331"]').fill(num_pages)
        num_tables = values.get("num_tables", "").strip()
        if num_tables:
            page.locator('input[name="CUSTOM_FIELD_RESPONSE_ID265368"]').click()
            page.locator('input[name="CUSTOM_FIELD_RESPONSE_ID265368"]').fill(num_tables)
        num_figures = values.get("num_figures", "").strip()
        if num_figures:
            page.locator('input[name="CUSTOM_FIELD_RESPONSE_ID265369"]').click()
            page.locator('input[name="CUSTOM_FIELD_RESPONSE_ID265369"]').fill(num_figures)
        page.get_by_role("row", name="checkbox *  This submission is on behalf of all authors and " "signifies that they are in complete agreement with the " "contents of the paper, that they are prepared to abide by the " "policies of the journal and that this paper has not been " "submitted elsewhere.", exact=True).get_by_label("checkbox").check()
        page.get_by_role("cell", name="checkbox *  I confirm that I am the corresponding author for " "the article I am submitting and that Oxford University Press " '("OUP") may retain my email address for the purpose of ' "communicating with me about the article. I agree to update my " "submission account immediately if my details change. If my " "article is accepted for publication OUP will contact me using " "the email address I have used in the online submission " "registration process. Please note that OUP does not retain " "copies of rejected articles.", exact=True).get_by_label("checkbox").check()
        page.get_by_role("row", name="checkbox *  I confirm that I have read the journal research " "data policy and my submission will follow the data policy " "requirements.", exact=True).get_by_label("checkbox").check()
        page.locator('input[name="CUSTOM_FIELD_RESPONSE_ID418683"]').nth(1).check()

        # Reproducibility checklist (code/data availability and ML/DL questions),
        # driven from the .sub values rather than the recorded test answers.
        _answer_reproducibility(page, values)

        page.locator('input[name="CUSTOM_FIELD_RESPONSE_ID113840"]').nth(1).check()
        page.locator('input[name="CUSTOM_FIELD_RESPONSE_ID996962"]').nth(1).check()
        page.get_by_role("button", name="Save & Continue ").click()
        logger.info("Completed the recorded Bioinformatics steps; " "leaving the browser open for review")

        # Leave the browser open so you can review/finish by hand.
        hold_open()


class BioinformaticsVenue(scholarone.ScholarOneVenue):
    """The Bioinformatics venue: a ScholarOne deployment (login shared, wizard local)."""

    slug = "bioinformatics"

    def submit(
        self,
        values: dict,
        *,
        headless: bool = False,
        debug: bool = False,
        new_session: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        keep_open_on_failure: bool = True,
    ) -> None:
        submit_bioinformatics(
            values,
            headless=headless,
            debug=debug,
            new_session=new_session,
            timeout=timeout,
            keep_open_on_failure=keep_open_on_failure,
            venue=self,
        )


VENUE = BioinformaticsVenue()
