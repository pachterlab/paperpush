"""openRxiv submission runner (Playwright): bioRxiv and medRxiv.

Both run on the same openRxiv platform, so the wizard is implemented once and
parameterized by a :class:`Variant` (``cfg``); they diverge only on the opening
screens and the declarations page. Wizard selectors are named constants so
:func:`check_biorxiv` can walk the wizard and report which ones broke.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import expect, sync_playwright

from ...database import get_venue
from ...validate import parse_authors
from ..base import Venue
from ..common import DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts
from ..common import \
    declares_no_competing_interest as _declares_no_competing_interest
from ..common import hold_open, hold_open_on_failure, open_run_context
from ..common import parse_pipe_file_list as _parse_file_list
from ..common import parse_pipe_funders as _parse_funders
from ..common import session_path as _session_path
from ..common import split_name_first_middle_last as _split_name
from ..login import VenueLoginError, login_orcid

logger = logging.getLogger(__name__)


class OpenRxivLoginError(VenueLoginError):
    """Raised when the automatic credential sign-in could not complete (message names ``cfg.name``)."""


#* sign-in form selectors (targeted by accessible role/name; the host differs per variant)
LOGIN_EMAIL_LABEL = "Email:"
LOGIN_PASSWORD_LABEL = "Password:"  # form label, not a credential  # nosec B105
LOGIN_BUTTON_NAME = "Sign in"
# The ORCID hand-off, offered beside the credential form on the same page. Unlike
# Editorial Manager's popup, this navigates the tab it is clicked in; the shared
# login_orcid helper detects that, so only the control's name is declared here.
LOGIN_ORCID_BUTTON_NAME = "ORCID logo Log in with ORCiD"

#* shared selectors -- the runner and the interface checker share these, so one fix corrects both

# Buttons / links, identified by their visible accessible name.
BTN_SUBMIT_NEW = "Submit a new manuscript"  # link on the queue page
BTN_CONTINUE = "Continue"
BTN_BEGIN = "Begin Submission"
BTN_SAVE_CONTINUE = "Save / Continue"
BTN_ADD_AUTHOR = "Add Author"
BTN_SAVE = "Save"

# Pause between authors so a fast next "Add Author" click doesn't race the save.
AUTHOR_SAVE_PAUSE_MS = 3000
LINK_SELECT_FILES = "Select Files"
BTN_UPLOAD = "Upload Files"
# Funding step links (only present once "Yes, I have funding" is selected).
LINK_ADD_AWARD = "+ Add another award number"
LINK_ADD_FUNDER = "+ Add another funder"
# Per-key delay: the funder-name jQuery UI autocomplete fires its search only on
# real keystrokes, so the name is typed char by char (a plain fill() won't).
FUNDER_TYPE_DELAY_MS = 80

# Radio / checkbox / text labels.
DEFAULT_ARTICLE_SCOPE = "Research article with data"
COI_NONE_LABEL = "The authors have declared no"
NO_FUNDING_LABEL = "No, I don't have funding"

# Author-form textbox labels.
AUTHOR_FIELDS = {
    "email": "Email",
    "first": "First Name",
    "middle": "Middle Name(s)/Initial(s)",
    "last": "Last Name",
    "institution": "Institution",
}

# Element ids and form-control names (CSS selectors).
# bioRxiv opening screens.
SEL_SERVER_BIO = "#bio"
SEL_SERVER_MED = "#med"
SEL_RESULT_TYPE = 'select[name="category_toc_category"]'
# medRxiv opening screen: article-type gate (option 2 keeps the submission in scope).
SEL_ARTICLE_TYPE_ACCEPT = "#temp_article_type2"
# Shared manuscript-details / declarations selectors.
SEL_SUBJECT = 'select[name="category_subjcode"]'
SEL_TITLE = 'textarea[name="title"]'
SEL_ABSTRACT = 'textarea[name="ms_abstract"]'
# bioRxiv's two declarations-page affirmation checkboxes.
SEL_AU_STATEMENT = 'input[name="fixed_au_statement_one"]'
SEL_NOT_ELSEWHERE = 'input[name="fixed_not_elsewhere"]'
# Competing-interest textarea, shown when the authors DO declare an interest.
SEL_COI_STATEMENT = 'textarea[name="fixed_coi_stmt"]'
# Funder-name box (jQuery UI autocomplete); the id repeats per funder block, so
# callers scope it to the funder "group" and index with .nth().
SEL_FUNDER_NAME = "#funder-name"
SEL_DROPZONE = "#dd-dropzone-manuscript"
# The corresponding-author toggle is a Vuetify checkbox with no clickable label.
SEL_CORRESPONDING = ".v-input.v-input--selection-controls.v-input--checkbox " "> .v-input__control > .v-input__slot " "> .v-input--selection-controls__input " "> .v-input--selection-controls__ripple"

# medRxiv declarations page: a longer block of mandatory affirmations, all checked.
SEL_DECLARATION_CHECKBOXES = (
    'input[name="fixed_au_statement_one"]',
    'input[name="fixed_au_statement_two"]',
    'input[name="fixed_declaration_posting"]',
    'input[name="fixed_declaration_guidelines"]',
    'input[name="fixed_declaration_consent"]',
    'input[name="fixed_declaration_trials"]',
    'input[name="fixed_declaration_legality"]',
    'input[name="fixed_declaration_checklists"]',
)
# Human-subjects screening cascade: the first question is a Yes/No radio pair in
# a table cell, matched by a stable substring of the prompt.
HUMAN_SUBJECTS_Q_RE = re.compile(r"Does the study describe the use of any human data")
# Clinical-trial registration: a Yes/No/Not-applicable radio group; the .sub
# value maps to one of these element ids.
CLINICAL_TRIAL_IDS = {
    "Yes": "#clinical_trial_yesno_yes",
    "No": "#clinical_trial_yesno_no",
    "Not applicable": "#clinical_trial_yesno_NA",
}
DEFAULT_CLINICAL_TRIAL_ID = "#clinical_trial_yesno_NA"
# Free-text data-availability description textarea (medRxiv declarations page).
SEL_DATA_AVAILABILITY = 'textarea[name="fixed_data_availability"]'
# External-links URL group: input ``fixed_external_links_arr.1_1`` grows via the
# "add_button" link (see ``_fill_url_list``); medRxiv's clinical-protocols group
# uses ``cp_add_button`` to distinguish the two on the same page.
SEL_EXTERNAL_LINKS_PREFIX = "fixed_external_links_arr"
SEL_EXTERNAL_LINKS_ADD = "a.add_button"

# License name (as in the .sub) -> the reuse-page radio id. Only CC-BY was
# captured; the rest follow the same ``reuse_*`` pattern.
_LICENSE_IDS = {
    "CC-BY": "#reuse_cc_by",
    "CC-BY-NC": "#reuse_cc_by_nc",
    "CC-BY-ND": "#reuse_cc_by_nd",
    "CC-BY-NC-ND": "#reuse_cc_by_nc_nd",
    "CC0": "#reuse_cc0",
    "No reuse without permission": "#reuse_none",
}
DEFAULT_LICENSE_ID = "#reuse_cc_by"

# Best-effort draft cleanup (used by --discard). These delete/confirm control
# names are guesses; if none match, the discard is skipped (non-fatal).
DISCARD_DELETE_NAMES = ["Delete", "Remove", "Withdraw", "Discard", "Trash"]
DISCARD_CONFIRM_NAMES = ["Delete", "Yes", "Confirm", "OK", "Remove"]


@dataclass(frozen=True)
class Variant:
    """Per-venue configuration for one openRxiv server.

    Each field below gates one place the wizard diverges between bioRxiv and
    medRxiv (see the module docstring for the full list):

    * ``opening`` -- ``"biorxiv"`` (server radio + article-scope radio) or
      ``"medrxiv"`` (a single article-type gate radio).
    * ``has_result_type`` -- whether a result-type drop-down precedes the
      subject combobox.
    * ``code_url_on_title_page`` -- whether the optional data-availability links
      are entered on the title page (bioRxiv) rather than on the declarations
      page (medRxiv).
    * ``declaration_checkboxes`` -- the mandatory affirmation checkboxes ticked
      on the declarations page.
    * ``has_health_declarations`` -- whether the medRxiv-only human-subjects
      cascade, clinical-trial radio, and data-availability description plus links
      apply.
    """

    slug: str
    name: str
    opening: str = "biorxiv"
    has_result_type: bool = True
    code_url_on_title_page: bool = True
    declaration_checkboxes: tuple[str, ...] = (SEL_AU_STATEMENT, SEL_NOT_ELSEWHERE)
    has_health_declarations: bool = False

    @property
    def venue(self):
        """The database entry (URLs, fields) for this venue."""
        return get_venue(self.slug)

    @property
    def portal_origin(self) -> str:
        """Scheme+host of the submission portal (from the venue database)."""
        return "{0.scheme}://{0.netloc}/".format(urlsplit(self.venue.submission_url))

    @property
    def login_url(self) -> str:
        """The sign-in page (the portal origin)."""
        return self.portal_origin

    @property
    def queues_url(self) -> str:
        """The Author Area submission queue (redirects to sign-in when signed out)."""
        return self.portal_origin + "submission/queues"


VARIANTS = {
    "biorxiv": Variant("biorxiv", "bioRxiv"),
    "medrxiv": Variant(
        "medrxiv",
        "medRxiv",
        opening="medrxiv",
        has_result_type=False,
        code_url_on_title_page=False,
        declaration_checkboxes=SEL_DECLARATION_CHECKBOXES,
        has_health_declarations=True,
    ),
}

# Default variant for the bare ``biorxiv`` runner; ``medrxiv`` passes its own.
_DEFAULT = VARIANTS["biorxiv"]

# Module-level URL constants for the default variant, read directly by the
# portal-drift test; the runner/checker resolve URLs per variant via ``cfg``.
LOGIN_URL = _DEFAULT.login_url
QUEUES_URL = _DEFAULT.queues_url


def _queues_url(email: str | None = None, cfg: Variant = _DEFAULT) -> str:
    """The submission-queue URL, optionally prefilling the sign-in email field.

    The optional ``emailAddr`` query parameter prefills the sign-in form's email
    field (from the stored credential) for the manual fallback.
    """
    base = cfg.queues_url
    if not email:
        return base
    return f"{base}?MSTRServlet.emailAddr={quote(email)}"


def _parse_url_list(value: str) -> list[str]:
    """Split a multi-line URL field into a list of non-blank URLs (one per line)."""
    return [line.strip() for line in value.splitlines() if line.strip()]


def _fill_url_list(page, prefix: str, add_link_sel: str, urls: list[str]) -> None:
    """Fill a repeatable openRxiv URL group, adding rows as needed.

    Inputs are named ``{prefix}.1_{n}``; ``add_link_sel`` is the "+ Add URL" link
    clicked before every row past the first. Targeting by form-control name keeps
    two groups on one page (external-links / clinical-protocols) unambiguous.
    """
    for i, url in enumerate(urls, start=1):
        if i > 1:
            page.locator(add_link_sel).click()
        box = page.locator(f'input[name="{prefix}.{i}_1"]')
        box.click()
        box.fill(url)


def _attach_file(page, dropzone_sel: str, path: str) -> None:
    """Attach ``path`` to the dropzone at ``dropzone_sel``.

    "Select Files" is a link opening a native file chooser (not a file input),
    so we intercept the chooser the click opens and hand it the file.
    """
    with page.expect_file_chooser() as fc_info:
        page.locator(dropzone_sel).get_by_role("link", name=LINK_SELECT_FILES).click()
    fc_info.value.set_files(path)


def _select_funder_suggestion(page, timeout_ms: int = 8000) -> None:
    """Pick the first funder from the FundRef autocomplete dropdown.

    The box won't accept a free-typed funder, so we click the first match. jQuery
    UI leaves earlier (hidden) menus in the DOM, so scope to the visible one --
    otherwise the second funder's ``.first`` lands in the first funder's menu.
    """
    suggestion = page.locator(".ui-autocomplete:visible li").first
    suggestion.wait_for(state="visible", timeout=timeout_ms)
    suggestion.click()


def _add_funder(page, index: int, funder: dict) -> None:
    """Fill one funder (name + award numbers) on the funding step.

    ``index`` is 1-based (the caller clicks ``LINK_ADD_FUNDER`` for extras). Award
    inputs are ``fixed_fundref_awards_arr.{index}_{n}`` with ``n`` starting at 3;
    each extra award needs ``LINK_ADD_AWARD`` clicked first.
    """
    name_box = page.get_by_role("group").locator(SEL_FUNDER_NAME).nth(index - 1)
    name_box.wait_for(state="visible")
    name_box.click()
    # Type the name keystroke by keystroke so the jQuery UI autocomplete fires
    # its search; a plain fill() sets the value without opening the dropdown.
    name_box.press_sequentially(funder["name"], delay=FUNDER_TYPE_DELAY_MS)
    _select_funder_suggestion(page)
    for i, award in enumerate(funder["awards"]):
        if i > 0:
            page.get_by_role("link", name=LINK_ADD_AWARD).nth(index - 1).click()
        award_box = page.locator(f'input[name="fixed_fundref_awards_arr.{index}_{3 + i}"]')
        award_box.click()
        award_box.fill(award)


def _add_author(page, author: dict) -> None:
    """Fill one author into the openRxiv author form and save the entry.

    ``author`` is one dict from :func:`easyeditorial.validate.parse_authors`
    (keys: name, email, affiliation, orcid, corresponding).
    """
    first, middle, last = _split_name(author["name"])

    page.get_by_role("button", name=BTN_ADD_AUTHOR).click()
    page.get_by_role("textbox", name=AUTHOR_FIELDS["email"]).click()
    page.get_by_role("textbox", name=AUTHOR_FIELDS["email"]).fill(author["email"])
    page.get_by_role("textbox", name=AUTHOR_FIELDS["first"]).click()
    page.get_by_role("textbox", name=AUTHOR_FIELDS["first"]).fill(first)
    if middle:
        page.get_by_role("textbox", name=AUTHOR_FIELDS["middle"]).click()
        page.get_by_role("textbox", name=AUTHOR_FIELDS["middle"]).fill(middle)
    page.get_by_role("textbox", name=AUTHOR_FIELDS["last"]).click()
    page.get_by_role("textbox", name=AUTHOR_FIELDS["last"]).fill(last)
    page.get_by_role("textbox", name=AUTHOR_FIELDS["institution"]).click()
    page.get_by_role("textbox", name=AUTHOR_FIELDS["institution"]).fill(author["affiliation"])
    if author["corresponding"]:
        page.locator(SEL_CORRESPONDING).click()
    page.get_by_role("button", name=BTN_SAVE, exact=True).click()


def _open_submission(page, server: str, article_scope: str, cfg: Variant) -> None:
    """Click through the opening screens up to "Begin Submission".

    bioRxiv routes by server (``#bio`` / ``#med``) then an article-scope radio,
    each followed by "Continue"; medRxiv answers a single article-type gate
    (``SEL_ARTICLE_TYPE_ACCEPT``) then "Continue". Both then click "Begin
    Submission".
    """
    page.get_by_role("link", name=BTN_SUBMIT_NEW).click()
    if cfg.opening == "biorxiv":
        # "Biological research" routes to bioRxiv (#bio); anything else is a
        # medRxiv-style category. The #med id is inferred from the #bio pattern.
        if server and server != "Biological research":
            page.locator(SEL_SERVER_MED).check()
        else:
            page.locator(SEL_SERVER_BIO).check()
        page.get_by_role("button", name=BTN_CONTINUE).click()
        page.get_by_role("radio", name=article_scope).check()
        page.get_by_role("button", name=BTN_CONTINUE).click()
    else:
        # medRxiv article-type gate: keep the submission in scope (option 2).
        page.locator(SEL_ARTICLE_TYPE_ACCEPT).check()
        page.get_by_role("button", name=BTN_CONTINUE).click()
    page.get_by_role("button", name=BTN_BEGIN).click()


def _answer_health_declarations(page, human_subjects: str, public_data_only: str, public_data_source: str, ethics_statement: str, clinical_trial: str, data_availability: str, data_availability_links: list[str]) -> None:
    """Fill medRxiv's human-subjects, clinical-trial, and data-availability block.

    The medRxiv-only tail of the declarations page. "Yes" to human subjects
    reveals a public-data-only follow-up whose answer reveals one of two
    free-text boxes; then the clinical-trial radio, the data-availability
    description, and the links (which fill both the external and protocols groups).
    """
    fixed_research_subjects_value = "true" if human_subjects == "Yes" else "false"
    page.locator(f'input[name="fixed_research_subjects"][value="{fixed_research_subjects_value}"]').check()
    if human_subjects == "Yes":
        fixed_public_data_value = "true" if public_data_only == "Yes" else "false"
        page.locator(f'input[name="fixed_public_data"][value="{fixed_public_data_value}"]').check()
        if public_data_only == "Yes":
            # Only simulated/openly-public data: state where it came from.
            if public_data_source:
                box = page.get_by_role("textbox", name="State that source data were")
                box.click()
                box.fill(public_data_source)
        else:
            # Real human-subjects data: state the ethics oversight body.
            if ethics_statement:
                box = page.get_by_role("textbox", name="State full, non-abbreviated")
                box.click()
                box.fill(ethics_statement)
    # Clinical-trial registration radio (Yes / No / Not applicable).
    trial_id = CLINICAL_TRIAL_IDS.get(clinical_trial, DEFAULT_CLINICAL_TRIAL_ID)
    page.locator(trial_id).check()
    # Free-text data-availability description.
    if data_availability:
        page.locator(SEL_DATA_AVAILABILITY).fill(data_availability)
    # The same links populate both the external-dataset group and the
    # clinical-trial protocols group.
    if data_availability_links:
        _fill_url_list(page, SEL_EXTERNAL_LINKS_PREFIX, SEL_EXTERNAL_LINKS_ADD, data_availability_links)
        _fill_url_list(page, "fixed_clinical_protocols_arr", "a.cp_add_button", data_availability_links)


def submit_biorxiv(values: dict, headless: bool = False, debug: bool = False, new_session: bool = False, timeout: float = DEFAULT_TIMEOUT_SECONDS, keep_open_on_failure: bool = True, *, venue) -> None:
    """Open the openRxiv portal, sign in, then drive the submission wizard.

    Sign-in is handled by ``venue.ensure_signed_in`` (reuse a saved session, else
    stored credentials, else a manual sign-in). ``new_session=True`` discards any
    saved session first. ``values`` is the parsed ``.sub`` field map; the wizard
    stops at the Files page and never clicks a final submit. ``venue.variant``
    selects the openRxiv server and gates the opening/declarations differences.
    ``debug=True`` forces a headed browser and opens the Inspector at the first
    action via ``page.pause()``.
    """
    # The Inspector needs a visible browser; debugging headless makes no sense.
    if debug:
        headless = False

    cfg = venue.variant

    title = values.get("title", "")
    abstract = values.get("abstract", "")
    article_scope = values.get("article_scope", DEFAULT_ARTICLE_SCOPE)
    result_type = values.get("result_type", "")
    subject_category = values.get("subject_category", "")
    license_name = values.get("license", "").strip()
    server = values.get("server_suitability", "").strip()
    manuscript_file = values.get("manuscript_file", "").strip()
    # The free-text description is medRxiv-only; the links field (one URL per
    # line) is shared -- it is the bioRxiv title-page external-links group and,
    # on medRxiv, both the external-dataset and clinical-protocols groups.
    data_availability = values.get("data_availability", "").strip()
    data_availability_links = _parse_url_list(values.get("data_availability_links", ""))
    # medRxiv health-research declarations (consulted only when the variant has
    # them); the follow-up answers are read only when human_subjects == "Yes".
    human_subjects = values.get("human_subjects", "No").strip() or "No"
    public_data_only = values.get("public_data_only", "Yes").strip() or "Yes"
    public_data_source = values.get("public_data_source", "").strip()
    ethics_statement = values.get("ethics_statement", "").strip()
    clinical_trial = values.get("clinical_trial", "Not applicable").strip() or "Not applicable"
    competing_interest = values.get("competing_interest", "").strip()
    funders = _parse_funders(values.get("funding", ""))
    figure_files = _parse_file_list(values.get("figure_files", ""), ["path", "label"])
    supp_files = _parse_file_list(values.get("supplementary_files", ""), ["path", "type", "linktext"])
    authors = parse_authors(values.get("authors", ""))

    logger.info("Starting %s submission run (headless=%s, debug=%s): " "%d author(s), %d funder(s), %d figure(s), %d supplementary " "file(s)", cfg.name, headless, debug, len(authors), len(funders), len(figure_files), len(supp_files))

    with sync_playwright() as playwright, hold_open_on_failure(headless=headless, keep_open=keep_open_on_failure):
        browser = playwright.chromium.launch(headless=headless)

        context = open_run_context(browser, venue.session_path(), new_session=new_session)
        apply_default_timeouts(context, timeout)
        page = context.new_page()

        venue.ensure_signed_in(page, context, debug=debug)

        if debug:
            # Open the Inspector at the first action so you can step through the
            # wizard (sign in by hand first if no session/credentials did).
            page.pause()

        logger.debug("Signed in; starting a new manuscript submission")
        _open_submission(page, server, article_scope, cfg)
        # Selects are chosen by visible label so the .sub value maps directly.
        # bioRxiv has a result-type drop-down before the subject; medRxiv does not.
        if cfg.has_result_type and result_type:
            page.locator(SEL_RESULT_TYPE).select_option(label=result_type)
        if subject_category:
            page.locator(SEL_SUBJECT).select_option(label=subject_category)
        # A blank .sub title must not clobber a value already in the portal
        # (e.g. on a resumed draft); only write when we have something.
        if title:
            page.locator(SEL_TITLE).click()
            page.locator(SEL_TITLE).fill(title)
        # bioRxiv collects the optional data-availability links on the title page;
        # medRxiv collects them (plus a free-text description) on the declarations
        # page (below).
        if cfg.code_url_on_title_page and data_availability_links:
            _fill_url_list(page, SEL_EXTERNAL_LINKS_PREFIX, SEL_EXTERNAL_LINKS_ADD, data_availability_links)
        page.get_by_role("button", name=BTN_SAVE_CONTINUE).click()

        # Declarations page: the abstract, the mandatory affirmation checkboxes,
        # the competing-interest answer, and (medRxiv only) the health-research
        # screening block.
        if abstract:
            page.locator(SEL_ABSTRACT).click()
            page.locator(SEL_ABSTRACT).fill(abstract)
        for selector in cfg.declaration_checkboxes:
            page.locator(selector).check()
        # No competing interest -> the radio; a real declaration -> the textarea.
        if _declares_no_competing_interest(competing_interest):
            page.get_by_role("radio", name=COI_NONE_LABEL).check()
        else:
            page.locator(SEL_COI_STATEMENT).click()
            page.locator(SEL_COI_STATEMENT).fill(competing_interest)
        if cfg.has_health_declarations:
            _answer_health_declarations(page, human_subjects, public_data_only, public_data_source, ethics_statement, clinical_trial, data_availability, data_availability_links)
        page.get_by_role("button", name=BTN_SAVE_CONTINUE).click()

        # Repeat the author step for every author in the .sub list. Pause
        # between authors so a fast next "Add Author" click does not race the
        # save and drop the entry (the portal glitches and skips the author).
        logger.debug("Entering %d author(s)", len(authors))
        for i, author in enumerate(authors):
            if i > 0:
                page.wait_for_timeout(AUTHOR_SAVE_PAUSE_MS)
            logger.debug("Adding author %d/%d", i + 1, len(authors))
            _add_author(page, author)
        page.get_by_role("button", name=BTN_SAVE_CONTINUE).click()

        # No funders -> the "No funding" radio; otherwise declare each funder.
        if funders:
            page.get_by_role("radio", name="Yes, I have funding").check()
            # Selecting "Yes" renders the first funder block; wait for it before
            # filling so the first _add_funder does not race the form rendering.
            page.get_by_role("group").locator(SEL_FUNDER_NAME).first.wait_for(state="visible")
            for i, funder in enumerate(funders):
                if i > 0:
                    page.get_by_role("link", name=LINK_ADD_FUNDER).first.click()
                _add_funder(page, i + 1, funder)
        else:
            page.get_by_role("radio", name=NO_FUNDING_LABEL).check()
        page.get_by_role("button", name=BTN_SAVE_CONTINUE).click()
        license_id = _LICENSE_IDS.get(license_name, DEFAULT_LICENSE_ID)
        page.locator(license_id).check()
        page.get_by_role("button", name=BTN_SAVE_CONTINUE).click()
        page.get_by_role("button", name=BTN_SAVE_CONTINUE).click()
        # Attach the manuscript, then any figures/supplementary material. Each
        # goes through a native file chooser (see _attach_file).
        logger.info("Attaching manuscript file %s", manuscript_file)
        _attach_file(page, SEL_DROPZONE, manuscript_file)
        # Figures and supplementary files use separate dropzones, numbering, and
        # metadata. The manuscript is file 1, so figure label inputs (label_N)
        # count up from there; supplementary files are numbered independently
        # (num1, num2, ...).
        file_index = 1
        for entry in figure_files:
            _attach_file(page, "#dd-dropzone-image", entry["path"])
            file_index += 1
            if entry["label"]:
                page.locator(f'input[name="label_{file_index}"]').fill(entry["label"])
        for supp_index, entry in enumerate(supp_files, start=1):
            _attach_file(page, "#dd-dropzone-supplemental", entry["path"])
            if entry["type"]:
                page.locator(f'select[name^="addFileAttr_num{supp_index}' '_data_supp"]').select_option(entry["type"])
            if entry["linktext"]:
                page.locator(f'input[name="addFileAttr_num{supp_index}' '_data_supp_linktext"]').fill(entry["linktext"])
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.get_by_role("button", name=BTN_UPLOAD).click()
        if page.get_by_role("button", name=BTN_UPLOAD).count() > 0:
            page.get_by_role("button", name=BTN_UPLOAD).click()
        logger.info("Reached the Files page; stopping before the final submit")

        # Leave the browser open so you can review/finish by hand.
        hold_open()


# --- interface checker ----------------------------------------------------
# Walks the wizard with dummy data, asserting each selector submit_biorxiv relies
# on is present and stopping before the file upload, so a portal UI change shows
# up as a failed check rather than a failed submission.


def session_path(cfg: Variant = _DEFAULT):
    """Path to the saved browser session (Playwright storage_state) for ``cfg``."""
    return _session_path(cfg.slug)


class OpenRxivVenue(Venue):
    """A venue on the openRxiv platform, selected by its :class:`Variant`.

    bioRxiv and medRxiv each subclass this and set :attr:`variant`. The base class
    supplies the sign-in orchestration, session capture, and login-state check;
    this class only threads the variant into the runner, sign-in form, and URLs.
    """

    variant: Variant

    #: The queue-page link that only renders for a signed-in session.
    logged_in_names = (BTN_SUBMIT_NEW,)
    #: Both servers offer "Log in with ORCiD" beside the credential form.
    supports_orcid_login = True

    @property
    def portal_url(self) -> str:
        # The Author Area queue; requesting it unauthenticated redirects to sign-in.
        return self.variant.queues_url

    @property
    def login_url(self) -> str:
        return self.variant.login_url

    # display_name is inherited: the base returns get_venue(slug).name == variant.name.

    def _manual_signin_url(self, prefill_email: str | None) -> str:
        # openRxiv's sign-in form prefills the email from a query parameter.
        return _queues_url(prefill_email, cfg=self.variant)

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
        submit_biorxiv(
            values,
            headless=headless,
            debug=debug,
            new_session=new_session,
            timeout=timeout,
            keep_open_on_failure=keep_open_on_failure,
            venue=self,
        )

    def login(self, page, username: str, password: str, *, orcid: bool = False, timeout_ms: int = 15000) -> None:
        """Sign in to openRxiv from stored credentials.

        Both ways in start from the same page and end with the same queue check,
        so only the middle differs: by default the email/password form, with
        ``orcid`` the "Log in with ORCiD" button beside it -- handed to the shared
        :func:`~paperpush.venues.login.login_orcid`, with ``username``/``password``
        then being the author's ORCID iD and ORCID password.

        Raises :class:`OpenRxivLoginError` if a form field can't be found or the
        signed-in queue never loads, so :meth:`ensure_signed_in` can fall back to
        a manual sign-in.
        """
        cfg = self.variant
        logger.debug("Signing in to %s at %s (orcid=%s)", cfg.name, cfg.login_url, orcid)
        page.goto(cfg.login_url)

        if orcid:
            login_orcid(
                page,
                username,
                password,
                entry=page.get_by_role("button", name=LOGIN_ORCID_BUTTON_NAME),
                return_url=cfg.queues_url,
                venue_name=cfg.name,
                timeout_ms=timeout_ms,
                error=OpenRxivLoginError,
            )
        else:
            email = page.get_by_role("textbox", name=LOGIN_EMAIL_LABEL)
            pwd = page.get_by_role("textbox", name=LOGIN_PASSWORD_LABEL)
            try:
                email.wait_for(state="visible", timeout=timeout_ms)
                pwd.wait_for(state="visible", timeout=timeout_ms)
            except PWTimeout as exc:
                raise OpenRxivLoginError(f"could not find the email/password fields on the {cfg.name} sign-in " "page (the sign-in form may have changed)") from exc

            email.fill(username)
            pwd.fill(password)
            page.get_by_role("button", name=LOGIN_BUTTON_NAME).click()

        # Confirm we reached the submission queue (navigating there if the landing
        # page differs); if it still isn't visible, the sign-in did not take.
        if not self.is_logged_in(page, timeout_ms=timeout_ms):
            page.goto(cfg.queues_url)
            if not self.is_logged_in(page, timeout_ms=timeout_ms):
                if orcid:
                    raise OpenRxivLoginError(
                        f"signed in to ORCID but the {cfg.name} submission queue did not "
                        "load -- the ORCID iD or password may be wrong, or the ORCID "
                        f"account may not be linked to a {cfg.name} account yet (link it "
                        "once by signing in by hand)"
                    )
                raise OpenRxivLoginError(
                    f"submitted the credentials but the signed-in {cfg.name} submission "
                    "queue did not load -- the email or password may be wrong, or "
                    f"{cfg.name} added a step (CAPTCHA / two-factor) that can't be automated"
                )


@dataclass
class StepResult:
    """Outcome of checking one wizard step."""

    label: str
    status: str  # "ok" | "missing" | "skipped"
    detail: str = ""


@dataclass
class CheckReport:
    """Result of an interface check run.

    ``outcome`` is one of:
      * ``"ok"``            -- every checked selector was present
      * ``"changed"``       -- a selector is missing; the UI likely changed
      * ``"auth_required"`` -- could not get past sign-in (no/expired session)
    """

    outcome: str
    steps: list[StepResult] = field(default_factory=list)
    message: str = ""
    # None = discard not attempted; True/False = attempted and (not) succeeded.
    discarded: bool | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    @property
    def missing(self) -> list[StepResult]:
        return [s for s in self.steps if s.status == "missing"]


# Placeholder values typed during the walk. Clearly marked so anything that does
# leak into a draft is obvious as a test artifact, not a real submission.
_DUMMY_AUTHOR = {
    "name": "Interface Check",
    "email": "interface-check@example.invalid",
    "affiliation": "easyeditorial automated check",
    "orcid": "",
    "corresponding": True,
}
_DUMMY_TITLE = "AUTOMATED INTERFACE CHECK - DO NOT SUBMIT"


def _dummy_abstract(cfg: Variant) -> str:
    return f"This draft was created by easyeditorial's automated interface check to verify the {cfg.name} submission wizard. It is not a real submission and can be discarded."


def _discard_draft(page, title: str, timeout_ms: int = 8000, cfg: Variant = _DEFAULT) -> bool:
    """Best-effort delete of the dummy draft from the queue. Never raises.

    Returns True only if a delete control was found and clicked. The selectors
    are guesses (see ``DISCARD_DELETE_NAMES``); a miss is logged by the caller
    and leaves the draft in place rather than failing the run.
    """
    try:
        page.goto(cfg.queues_url)
        # Scope to the queue row holding our dummy title so we never touch a real
        # submission. Vuetify renders rows variously, so match by containing text.
        row = page.locator(f"tr:has-text({title!r}), .row:has-text({title!r}), " f"[role=row]:has-text({title!r})").first
        if row.count() == 0:
            return False
        clicked = False
        for name in DISCARD_DELETE_NAMES:
            ctrl = row.get_by_role("button", name=name)
            if ctrl.count() == 0:
                ctrl = row.get_by_role("link", name=name)
            if ctrl.count() > 0:
                ctrl.first.click()
                clicked = True
                break
        if not clicked:
            return False
        # Confirm if a confirmation dialog appears; ignore if none does.
        for name in DISCARD_CONFIRM_NAMES:
            confirm = page.get_by_role("button", name=name)
            try:
                if confirm.count() > 0 and confirm.first.is_visible(timeout=2000):
                    confirm.first.click()
                    break
            except Exception:
                continue
        return True
    except Exception as exc:
        logger.debug("check_%s: draft discard attempt failed (%s)", cfg.slug, exc)
        return False


def check_biorxiv(headless: bool = True, timeout_ms: int = 8000, discard: bool = False, cfg: Variant = _DEFAULT) -> CheckReport:
    """Walk the openRxiv wizard with dummy data and report any missing selectors.

    Loads the saved session and steps through the wizard, asserting each selector
    :func:`submit_biorxiv` depends on is visible before using it, stopping at the
    Files page. A missing selector is recorded (not raised) so the walk reports
    as much as it can in one pass. Completing the walk leaves a dummy draft in the
    queue; ``discard=True`` attempts a best-effort cleanup (see
    :func:`_discard_draft`), reported in ``CheckReport.discarded``.
    """
    path = session_path(cfg)
    if not path.exists():
        logger.warning("check_%s: no saved session at %s", cfg.slug, path)
        return CheckReport(
            outcome="auth_required",
            message=(f"No saved {cfg.name} session. Run 'easyeditorial submit' once " "to sign in and store a session."),
        )

    logger.info("check_%s: walking the wizard (headless=%s, discard=%s)", cfg.slug, headless, discard)
    steps: list[StepResult] = []
    discarded: bool | None = None
    dummy_abstract = _dummy_abstract(cfg)

    def check(label, locator, action=None):
        """Assert ``locator`` is visible; if so optionally run ``action``.

        Returns True when the step passed. On a miss, records it and returns
        False so the caller can stop the walk -- once a step's control is gone we
        can't reliably reach the steps after it.
        """
        try:
            expect(locator).to_be_visible(timeout=timeout_ms)
        except (AssertionError, PWTimeout) as exc:
            logger.warning("check_%s: selector missing for step %r", cfg.slug, label)
            steps.append(StepResult(label, "missing", str(exc).splitlines()[0]))
            return False
        if action is not None:
            action()
        logger.debug("check_%s: step OK: %s", cfg.slug, label)
        steps.append(StepResult(label, "ok"))
        return True

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(path))
        apply_default_timeouts(context)
        page = context.new_page()
        try:
            page.goto(cfg.queues_url)

            # Are we actually logged in? If the "Submit a new manuscript" link
            # never appears, treat it as an expired session rather than a UI
            # change, so a stale cookie doesn't masquerade as a false alarm.
            submit_link = page.get_by_role("link", name=BTN_SUBMIT_NEW)
            try:
                expect(submit_link).to_be_visible(timeout=timeout_ms)
            except (AssertionError, PWTimeout):
                logger.warning("check_%s: saved session appears expired", cfg.slug)
                return CheckReport(
                    outcome="auth_required",
                    message=(f"Saved {cfg.name} session looks expired (the submission " "queue did not load). Re-run 'easyeditorial submit' to " "sign in again."),
                )
            steps.append(StepResult(BTN_SUBMIT_NEW + " (link)", "ok"))
            submit_link.click()

            # Opening screens differ by variant: bioRxiv routes by server then
            # article scope; medRxiv answers a single article-type gate.
            if cfg.opening == "biorxiv":
                ok = check(f"server radio {SEL_SERVER_BIO}", page.locator(SEL_SERVER_BIO), lambda: page.locator(SEL_SERVER_BIO).check())
                ok = ok and check(f'button "{BTN_CONTINUE}"', page.get_by_role("button", name=BTN_CONTINUE), lambda: page.get_by_role("button", name=BTN_CONTINUE).click())
                ok = ok and check(f'article scope radio "{DEFAULT_ARTICLE_SCOPE}"', page.get_by_role("radio", name=DEFAULT_ARTICLE_SCOPE), lambda: page.get_by_role("radio", name=DEFAULT_ARTICLE_SCOPE).check())
                ok = ok and check(f'button "{BTN_CONTINUE}" (scope)', page.get_by_role("button", name=BTN_CONTINUE), lambda: page.get_by_role("button", name=BTN_CONTINUE).click())
            else:
                ok = check(f"article-type radio {SEL_ARTICLE_TYPE_ACCEPT}", page.locator(SEL_ARTICLE_TYPE_ACCEPT), lambda: page.locator(SEL_ARTICLE_TYPE_ACCEPT).check())
                ok = ok and check(f'button "{BTN_CONTINUE}"', page.get_by_role("button", name=BTN_CONTINUE), lambda: page.get_by_role("button", name=BTN_CONTINUE).click())
            ok = ok and check(f'button "{BTN_BEGIN}"', page.get_by_role("button", name=BTN_BEGIN), lambda: page.get_by_role("button", name=BTN_BEGIN).click())

            # Category selects: only assert they exist (their options are
            # dynamic, so we don't pick a specific label here). bioRxiv has a
            # result-type select before the subject; medRxiv has only the subject.
            if cfg.has_result_type:
                ok = ok and check(f"result-type select [{SEL_RESULT_TYPE}]", page.locator(SEL_RESULT_TYPE))
            ok = ok and check(f"subject select [{SEL_SUBJECT}]", page.locator(SEL_SUBJECT))

            ok = ok and check(f"title field [{SEL_TITLE}]", page.locator(SEL_TITLE), lambda: page.locator(SEL_TITLE).fill(_DUMMY_TITLE))
            ok = ok and check(f'button "{BTN_SAVE_CONTINUE}" (title)', page.get_by_role("button", name=BTN_SAVE_CONTINUE), lambda: page.get_by_role("button", name=BTN_SAVE_CONTINUE).click())

            ok = ok and check(f"abstract field [{SEL_ABSTRACT}]", page.locator(SEL_ABSTRACT), lambda: page.locator(SEL_ABSTRACT).fill(dummy_abstract))
            # Each mandatory declaration checkbox is its own selector worth checking.
            for selector in cfg.declaration_checkboxes:
                ok = ok and check(f"declaration checkbox [{selector}]", page.locator(selector), lambda s=selector: page.locator(s).check())
            ok = ok and check(f'COI radio "{COI_NONE_LABEL}"', page.get_by_role("radio", name=COI_NONE_LABEL), lambda: page.get_by_role("radio", name=COI_NONE_LABEL).check())
            if cfg.has_health_declarations:
                # Answer the human-subjects question "No" so the walk stays on the
                # simple path (a "Yes" would reveal the follow-up cascade).
                ok = ok and check("human-subjects question", page.get_by_role("cell", name=HUMAN_SUBJECTS_Q_RE), lambda: page.get_by_role("cell", name=HUMAN_SUBJECTS_Q_RE).get_by_label("No").check())
                ok = ok and check(f"clinical-trial radio [{DEFAULT_CLINICAL_TRIAL_ID}]", page.locator(DEFAULT_CLINICAL_TRIAL_ID), lambda: page.locator(DEFAULT_CLINICAL_TRIAL_ID).check())
            ok = ok and check(f'button "{BTN_SAVE_CONTINUE}" (abstract)', page.get_by_role("button", name=BTN_SAVE_CONTINUE), lambda: page.get_by_role("button", name=BTN_SAVE_CONTINUE).click())

            # Author step: check the Add Author button, then fill one dummy
            # author through the same path submit_biorxiv uses.
            ok = ok and check(f'button "{BTN_ADD_AUTHOR}"', page.get_by_role("button", name=BTN_ADD_AUTHOR))
            if ok:
                # Each author subfield is its own selector worth checking.
                missing_field = False
                page.get_by_role("button", name=BTN_ADD_AUTHOR).click()
                for key, label in AUTHOR_FIELDS.items():
                    if key == "middle":
                        continue  # optional; skip to avoid noise
                    box = page.get_by_role("textbox", name=label)
                    val = {
                        "email": _DUMMY_AUTHOR["email"],
                        "first": "Interface",
                        "last": "Check",
                        "institution": _DUMMY_AUTHOR["affiliation"],
                    }[key]
                    if not check(f'author field "{label}"', box, lambda b=box, v=val: b.fill(v)):
                        missing_field = True
                        break
                if not missing_field:
                    # Mirror the runner: the corresponding toggle is the Vuetify
                    # control, not the label text.
                    check("corresponding checkbox [Mark as Corresponding Author]", page.locator(SEL_CORRESPONDING).first, lambda: page.locator(SEL_CORRESPONDING).first.click())
                    ok = check(f'button "{BTN_SAVE}" (author)', page.get_by_role("button", name=BTN_SAVE, exact=True), lambda: page.get_by_role("button", name=BTN_SAVE, exact=True).click())
                else:
                    ok = False
            ok = ok and check(f'button "{BTN_SAVE_CONTINUE}" (authors)', page.get_by_role("button", name=BTN_SAVE_CONTINUE), lambda: page.get_by_role("button", name=BTN_SAVE_CONTINUE).click())

            ok = ok and check(f'funding radio "{NO_FUNDING_LABEL}"', page.get_by_role("radio", name=NO_FUNDING_LABEL), lambda: page.get_by_role("radio", name=NO_FUNDING_LABEL).check())
            ok = ok and check(f'button "{BTN_SAVE_CONTINUE}" (funding)', page.get_by_role("button", name=BTN_SAVE_CONTINUE), lambda: page.get_by_role("button", name=BTN_SAVE_CONTINUE).click())

            ok = ok and check(f"license radio [{DEFAULT_LICENSE_ID}]", page.locator(DEFAULT_LICENSE_ID), lambda: page.locator(DEFAULT_LICENSE_ID).check())
            ok = ok and check(f'button "{BTN_SAVE_CONTINUE}" (license)', page.get_by_role("button", name=BTN_SAVE_CONTINUE), lambda: page.get_by_role("button", name=BTN_SAVE_CONTINUE).click())
            ok = ok and check(f'button "{BTN_SAVE_CONTINUE}" (pre-files)', page.get_by_role("button", name=BTN_SAVE_CONTINUE), lambda: page.get_by_role("button", name=BTN_SAVE_CONTINUE).click())

            # Files page: assert the upload controls exist but never attach a
            # file or submit. This is the deliberate stopping point.
            ok = ok and check(f"manuscript dropzone [{SEL_DROPZONE}]", page.locator(SEL_DROPZONE))
            ok = ok and check(f'link "{LINK_SELECT_FILES}"', page.locator(SEL_DROPZONE).get_by_role("link", name=LINK_SELECT_FILES))

            # Clean up the dummy draft this walk created. Best-effort: a failure
            # here never changes the interface verdict, it just leaves the draft.
            if discard:
                discarded = _discard_draft(page, _DUMMY_TITLE, timeout_ms, cfg=cfg)
                logger.info("check_%s: dummy-draft discard succeeded=%s", cfg.slug, discarded)
        finally:
            context.close()
            browser.close()

    if any(s.status == "missing" for s in steps):
        logger.warning("check_%s: interface changed (%d missing selector(s))", cfg.slug, sum(1 for s in steps if s.status == "missing"))
        return CheckReport(
            outcome="changed",
            steps=steps,
            message=f"One or more {cfg.name} selectors were not found; the " "submission wizard interface may have changed.",
            discarded=discarded,
        )
    logger.info("check_%s: all %d checked selector(s) present", cfg.slug, len(steps))
    return CheckReport(
        outcome="ok",
        steps=steps,
        message=f"All checked {cfg.name} selectors are present.",
        discarded=discarded,
    )
