"""Sign-in and submission for Discrete Mathematics (Elsevier).

Discrete Mathematics submits through Elsevier's newer portal at
``https://submit.elsevier.com/DISCM`` (a React app addressed by accessible names
and a few ``data-testid`` hooks). Selectors were captured with ``playwright
codegen``; re-capture the same way if Elsevier restyles the portal. The wizard is
driven straight through, stops before the final submit, and leaves the browser
open via :func:`hold_open`.
"""

from __future__ import annotations

import logging
import re
import time

from playwright.sync_api import sync_playwright

from ...database import get_venue
from ...validate import parse_authors
from ..base import Venue
from ..common import DEFAULT_TIMEOUT_SECONDS, _try, apply_default_timeouts, hold_open, hold_open_on_failure, open_run_context, parse_pipe_funders, split_name_first_last
from ..login import VenueLoginError
from ..recovery import GuardedPage, guard, step, unrecovered
from ..recovery_channel import browser_launch_args

logger = logging.getLogger(__name__)

_YES = {"yes", "y", "true", "1", "on"}

INSTITUTION_ACCESS_BUTTON = "Access through your"
# The Elsevier ID email box (its accessible name comes from aria-labelledby).
ELSEVIER_EMAIL_SELECTOR = "#bdd-email, input[name='pf.username']"
INSTITUTION_LINK = "Caltech Library Sign in with"  #! hard-coded for Caltech
# The cookie banner reappears across the sign-in navigations.
COOKIE_ACCEPT = "Accept all cookies"
# Start-submission control on the dashboard; present only once signed in (the
# logged-out landing page shows a plain "Start a submission" button instead).
START_SUBMISSION_TESTID = "submission-start-button"
# Author institution autocomplete: per-key typing delay, and how long to wait for
# the suggestion list after typing.
INSTITUTION_TYPE_DELAY_MS = 80
INSTITUTION_SUGGEST_MS = 8000
# Funders step: the "Did you receive any funding?" radios, and the per-funder
# row's organisation autocomplete, grant-number box and grant-recipient drop-down
# (the last two carry a per-row uuid in their ids).
FUNDERS_YES_SELECTOR = "#funders-yes"
FUNDERS_NO_SELECTOR = "#funders-no"
FUNDER_SEARCH_SELECTOR = "#value_input-select-institution-search"
FUNDER_GRANTS_SELECTOR = "[id^='grant-numbers-']"
FUNDER_RECIPIENT_SELECTOR = "[id^='trigger-input-grant-recipient-']"
# Research data step: the repository-name drop-down.
REPOSITORY_COMBOBOX = "#trigger-input-rdl-repository-name"
# Review step declarations, ticked when the .sub sets declarations_confirmed:
# publishing ethics, copyright terms, submission declarations, authorship
# contributions, and all funders mentioned.
DECLARATION_CHECKBOX_IDS = (
    "ethics-checkbox",
    "copyright-checkbox",
    "submission-declaration-checkbox",
    "authorship-declaration-checkbox",
    "funding-checkbox",
)
# The logged-out landing page's start control (the login flow clicks it to reach sign-in).
SIGNED_OUT_START_LABEL = "Start a submission"
# Fallback name for the same control, should the testid change.
START_SUBMISSION_NAME = re.compile(r"start (a )?new submission", re.I)
# Dashboard checkbox gating the start button; Elsevier writes the apostrophe as ' or ’.
TERMS_LABEL = re.compile(r"I accept Elsevier.s terms", re.I)
# The terms box itself: the only checkbox in the dashboard's #main, plus the path
# codegen recorded for it as a fallback.
TERMS_CHECKBOX_SELECTOR = "#main input[type='checkbox']"
TERMS_CHECKBOX_RECORDED = "#main > div > div > div > div:nth-of-type(2) > div > div > div:nth-of-type(1) > div:nth-of-type(2) > input"
# Budget for the between-steps "are we already signed in?" probe in login(). Short
# on purpose: it runs at every step, and a signed-in dashboard is already rendered.
LOGIN_PROBE_MS = 15000
# How long to wait for the author to approve the Caltech Duo push, and the
# button Duo shows afterwards to remember the browser.
DUO_WAIT_MS = 180000
DUO_TRUST_BUTTON = "Yes, this is my device"
# Upload-step item-type dropdowns.
REQUIRED_ITEMS_DROPDOWN = "#trigger-input-required-items-dropdown"
OPTIONAL_ITEMS_DROPDOWN = "#trigger-input-optional-items-dropdown"


class DiscreteMathematicsLoginError(VenueLoginError):
    """Raised when an automatic Discrete Mathematics (Elsevier) sign-in fails."""


# --- small helpers -----------------------------------------------------------


def _is_yes(value: str) -> bool:
    """True when a ``.sub`` value reads as an affirmative (yes/true/1/on)."""
    return str(value or "").strip().lower() in _YES


def _accept_cookies(page) -> None:
    """Dismiss the cookie banner if it is showing (it reappears across navigations)."""
    banner = page.get_by_role("button", name=COOKIE_ACCEPT)
    try:
        # A banner that vanishes between the check and the click is expected,
        # so the click is not handed to the recovery handler.
        with unrecovered():
            if banner.first.is_visible():
                banner.first.click()
    except Exception:  # noqa: BLE001 -- a missing banner is fine
        pass


def _accept_terms_and_start(page, timeout_ms: int = 30000) -> None:
    """Tick "I accept Elsevier's terms and conditions", then start a new submission.

    The dashboard renders late, and the start button stays disabled until the
    terms box is ticked, so wait for the dashboard, match the terms label loosely
    (Elsevier writes the apostrophe as either ' or ’), confirm the tick took
    (falling back to clicking the label for a custom-styled checkbox), then wait
    for the start button to enable before clicking it.
    """
    start = page.get_by_test_id(START_SUBMISSION_TESTID).or_(page.get_by_role("button", name=START_SUBMISSION_NAME)).first
    start.wait_for(state="visible", timeout=timeout_ms)

    # The terms box carries no accessible label, so the label match is only a
    # first choice; the dashboard's sole checkbox in #main (and, failing that, the
    # recorded codegen path) finds it today.
    terms = page.get_by_label(TERMS_LABEL).or_(page.locator(TERMS_CHECKBOX_SELECTOR)).or_(page.locator(TERMS_CHECKBOX_RECORDED)).first
    try:
        with unrecovered():
            terms.wait_for(state="attached", timeout=10000)
    except Exception:  # noqa: BLE001 -- no terms box on this dashboard (already accepted)
        logger.debug("No Elsevier terms checkbox on the dashboard")
    else:
        if not terms.is_checked():
            logger.debug("Accepting Elsevier's terms and conditions")
            try:
                with unrecovered():
                    terms.check(timeout=5000)
            except Exception:  # noqa: BLE001 -- custom-styled box: force the tick
                terms.check(force=True)

    # The start button enables only once the terms are accepted.
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline and not start.is_enabled():
        page.wait_for_timeout(250)
    logger.debug("Signed in; starting a new submission")
    start.click()


def _choose_option(page, wanted: str) -> str:
    """Pick ``wanted`` from an open Elsevier drop-down, else its "Other" entry.

    The caller has already clicked the combobox. Returns the option chosen, so a
    caller can fill the free-text box that "Other" reveals.
    """
    options = page.get_by_role("option")
    options.first.wait_for(state="visible", timeout=15000)
    texts = [t.strip() for t in options.all_inner_texts()]
    choice = wanted if wanted in texts else "Other"
    if choice != wanted:
        logger.info("Discrete Mathematics: %r is not a listed option; choosing 'Other'", wanted)
    page.get_by_role("option", name=choice, exact=True).click()
    return choice


def _parse_files(value: str) -> list[str]:
    """Return the file paths from a ``filelist`` block (first ``|`` column per line)."""
    paths = []
    for line in value.splitlines():
        path = line.split("|")[0].strip()
        if path:
            paths.append(path)
    return paths


def _select_upload_item(page, dropdown_sel: str, item_label: str) -> None:
    """Open an upload dropdown and pick the item type by label (accessible name, then text)."""
    page.locator(dropdown_sel).click()
    option = page.get_by_role("option", name=item_label)
    if not option.count():
        option = page.get_by_text(item_label, exact=False)
    option.first.click()


def _attach_upload(page, path: str, slot_index: int) -> None:
    """Attach ``path`` to the ``slot_index``-th upload slot (0 = the first).

    The upload control is a ``<button>`` that opens a native file chooser, not an
    ``<input type=file>``, so we set the files on the chooser the click raises.
    """
    def attach(page) -> None:
        button = page.get_by_test_id("upload-button").nth(slot_index)
        with page.expect_file_chooser() as fc_info:
            button.click()
        fc_info.value.set_files(path)

    # The click and the file chooser it opens only make sense together, so they
    # are one recoverable unit when the page is guarded.
    if isinstance(page, GuardedPage):
        page.attempt(f"attach {path!r} to upload slot {slot_index}", attach)
    else:
        attach(page)
    page.wait_for_timeout(2000)  # let the upload progress bar finish


def _pick_institution(page, institution: str) -> None:
    """Resolve the institution autocomplete after ``institution`` has been typed.

    Takes the first suggestion on an exact (case-insensitive) match. If it merely
    starts or ends with the typed value, take it but warn about the imperfect
    match; otherwise keep the manually entered text and warn more loudly. No
    suggestion at all is also warned.
    """
    suggestions = page.get_by_role("menuitem")
    if not suggestions.count():
        logger.warning("Discrete Mathematics: no institution suggestions for %r; keeping the entered value", institution)
        return

    suggestion = (suggestions.first.inner_text() or "").strip()
    entered = institution.strip().casefold()
    found = suggestion.casefold()

    if found == entered:
        suggestions.first.click()
    elif entered and (found.startswith(entered) or found.endswith(entered)):
        logger.warning("Discrete Mathematics: institution %r does not exactly match suggestion %r; taking the suggestion", institution, suggestion)
        suggestions.first.click()
    else:
        logger.warning("Discrete Mathematics: institution %r is not a prefix/suffix of suggestion %r; keeping the manually entered value", institution, suggestion)
        page.keyboard.press("Escape")  # keep the typed text instead of a suggestion
        page.get_by_role("combobox", name="Institution").fill(institution)


def _add_author(page, author: dict) -> None:
    """Add one author via the Add-another-author dialog: names, institution, email, flag."""
    first, last = split_name_first_last((author.get("name") or "").strip())
    email = (author.get("email") or "").strip()
    institution = (author.get("institution") or "").strip()
    logger.info("Adding author %s %s", first, last)

    page.get_by_label("First name").fill(first)
    page.get_by_label("Last name").fill(last)

    if institution:
        # Type key by key (not fill) so the autocomplete fires on each keystroke,
        # then give the suggestion list time to load before choosing from it.
        box = page.get_by_placeholder("Type at least three")
        box.click()
        box.fill("")
        box.press_sequentially(institution, delay=INSTITUTION_TYPE_DELAY_MS)
        try:
            with unrecovered():
                page.get_by_role("menuitem").first.wait_for(state="visible", timeout=INSTITUTION_SUGGEST_MS)
        except Exception:  # noqa: BLE001 -- no suggestions; _pick_institution warns and keeps the text
            pass
        page.wait_for_timeout(500)  # let the list settle on the full query
        _pick_institution(page, institution)

    if email:
        page.get_by_label("Email address").fill(email)
    if _is_yes(author.get("corresponding")):
        page.get_by_label("This is the corresponding").check()

    page.get_by_label("Confirm").click()


def _parse_funders(value: str) -> list[dict]:
    """Parse the ``funding`` field into ``{"name", "awards", "recipient"}`` dicts.

    Each non-empty line is ``Funder name | award1, award2 | grant recipient``;
    the shared parser reads the first two columns and the optional third names
    the author who received the grant.
    """
    funders = parse_pipe_funders(value)
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    for funder, line in zip(funders, lines):
        parts = [p.strip() for p in line.split("|")]
        funder["recipient"] = parts[2] if len(parts) > 2 else ""
    return funders


def _add_funder(page, funder: dict) -> None:
    """Fill the open funder row: organisation, grant numbers, recipient; then Confirm."""
    name = funder["name"]
    logger.info("Adding funder %s", name)
    box = page.locator(FUNDER_SEARCH_SELECTOR).last
    box.click()
    box.fill("")
    box.press_sequentially(name, delay=INSTITUTION_TYPE_DELAY_MS)
    suggestions = page.get_by_role("menuitem")
    try:
        with unrecovered():
            suggestions.first.wait_for(state="visible", timeout=INSTITUTION_SUGGEST_MS)
    except Exception:  # noqa: BLE001
        raise ValueError(f"no funder suggestions for {name!r}; the funder name must match one the portal offers") from None
    page.wait_for_timeout(500)  # let the list settle on the full query
    # Each suggestion is "Name\nCOUNTRY City"; prefer an exact name match.
    texts = suggestions.all_inner_texts()
    names = [t.split("\n", 1)[0].strip() for t in texts]
    wanted = name.casefold()
    index = next((k for k, n in enumerate(names) if n.casefold() == wanted), 0)
    if names[index].casefold() != wanted:
        logger.warning("Discrete Mathematics: funder %r has no exact suggestion; taking %r", name, texts[0].replace("\n", " · "))
    suggestions.nth(index).click()

    if funder["awards"]:
        page.locator(FUNDER_GRANTS_SELECTOR).last.fill("; ".join(funder["awards"]))

    recipient = funder.get("recipient", "")
    if recipient:
        page.locator(FUNDER_RECIPIENT_SELECTOR).last.click()
        option = page.get_by_role("option", name=recipient, exact=True)
        if option.count():
            option.first.click()
        else:
            logger.warning("Discrete Mathematics: grant recipient %r is not one of the authors offered; leaving it blank", recipient)
            page.keyboard.press("Escape")

    page.get_by_label("Confirm", exact=True).click()


class DiscreteMathematicsVenue(Venue):
    """Discrete Mathematics on Elsevier's ``submit.elsevier.com`` submission portal."""

    slug = "discrete_mathematics"
    # The dashboard is a slow single-page app; is_logged_in returns as soon as
    # either the signed-in or the signed-out page renders, so this is only a cap.
    logged_in_timeout_ms = 20000

    _VENUE = get_venue(slug)
    SITE_URL = _VENUE.site_url
    PORTAL_URL = _VENUE.submission_url  # inherited portal_url resolves to this
    LOGIN_URL = "https://submit.elsevier.com/DISCM"
    #: submit() guards its page, so a failed action can be recovered.
    supports_recovery = True

    @property
    def login_url(self) -> str:
        return self.LOGIN_URL

    def is_logged_in(self, page, *, timeout_ms: int | None = None) -> bool:
        """Signed in iff the dashboard's ``submission-start-button`` control is present.

        The logged-out landing page shows a plain "Start a submission" button
        instead, so the testid distinguishes the two states. The dashboard is a
        slow single-page app, so rather than give up after a fixed probe, wait for
        whichever page renders first -- the signed-in dashboard or the signed-out
        landing page -- and answer from that. Only ``timeout_ms`` with neither
        rendered reads as signed out.
        """
        if timeout_ms is None:
            timeout_ms = self.logged_in_timeout_ms
        signed_in = page.get_by_test_id(START_SUBMISSION_TESTID)
        signed_out = page.get_by_label(SIGNED_OUT_START_LABEL, exact=True)
        try:
            with unrecovered():
                signed_in.or_(signed_out).first.wait_for(state="visible", timeout=timeout_ms)
        except Exception:  # noqa: BLE001 -- neither page rendered in the budget
            return False
        try:
            return signed_in.first.is_visible()
        except Exception:  # noqa: BLE001
            return False

    # * run
    def submit(
        self,
        values: dict,
        headless: bool = False,
        debug: bool = False,
        new_session: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        keep_open_on_failure: bool = True,
    ) -> None:
        """Open Discrete Mathematics, sign in, then drive the Elsevier wizard from a ``.sub``."""
        # The Inspector needs a visible browser; debugging headless makes no sense.
        if debug:
            headless = False

        # grab values from the .sub file
        article_type = values.get("article_type", "").strip()
        title = values.get("title", "")
        abstract = values.get("abstract", "")
        keywords = values.get("keywords", "").strip()
        manuscript_file = values.get("manuscript_file", "").strip()
        declaration_file = values.get("declaration_file", "").strip()
        no_competing_interests = _is_yes(values.get("no_competing_interests", ""))
        figures = _parse_files(values.get("figure_files", ""))
        supplements = _parse_files(values.get("supplementary_files", ""))
        latex_source = values.get("latex_source", "").strip()
        # Research data: either link a deposited dataset (share_data) or decline
        # and pick a statement. The repository fields are read either way but
        # only used -- and only required -- when sharing.
        share_data = _is_yes(values.get("share_data", ""))
        data_repository_name = values.get("data_repository_name", "").strip()
        data_repository_url = values.get("data_repository_url", "").strip()
        source_of_data = values.get("source_of_data", "").strip().lower()
        dataset_title = values.get("dataset_title", "").strip()
        data_statement = values.get("data_statement", "").strip()
        declarations_confirmed = _is_yes(values.get("declarations_confirmed", ""))

        if share_data:
            missing = [
                name
                for name, value in (
                    ("data_repository_name", data_repository_name),
                    ("data_repository_url", data_repository_url),
                    ("source_of_data", source_of_data),
                    ("dataset_title", dataset_title),
                )
                if not value
            ]
            if missing:
                raise ValueError("share_data is yes but " + ", ".join(missing) + " " + ("was" if len(missing) == 1 else "were") + " not provided")
            if source_of_data not in ("original", "reference"):
                raise ValueError(f"source_of_data must be 'original' or 'reference', not {source_of_data!r}")
        elif not data_statement:
            raise ValueError("share_data is no but no data_statement was provided")

        funders = _parse_funders(values.get("funding", ""))

        author_field = next((f for f in self._VENUE.fields if f.type == "authorlist"), None)
        authors = parse_authors(values.get("authors", ""), author_field.fields if author_field else None)

        logger.info("Starting Discrete Mathematics submission run (headless=%s, debug=%s): manuscript=%s, %d author(s)", headless, debug, manuscript_file, len(authors))

        with sync_playwright() as playwright, hold_open_on_failure(headless=headless, keep_open=keep_open_on_failure):
            # browser_launch_args() is empty unless `submit --cdp-port` asked for a CDP endpoint.
            browser = playwright.chromium.launch(headless=headless, args=browser_launch_args())
            context = open_run_context(browser, self.session_path(), new_session=new_session)
            apply_default_timeouts(context, timeout)
            page = context.new_page()

            self.ensure_signed_in(page, context, debug=debug)
            # Sign-in stays unguarded. From here on a failed action goes to the
            # recovery handler (see DEVELOPMENT.md, "Recovering a run that gets stuck").
            page = guard(page)

            if debug:
                page.pause()

            step("Dashboard: terms and start submission")
            _accept_cookies(page)
            _accept_terms_and_start(page)

            # Article type.
            step("Article type")
            page.get_by_label(article_type).check()
            page.get_by_label("Save and continue").click()

            # File uploads: manuscript is the first required item.
            step("File uploads: manuscript")
            slot = 0
            logger.info("Uploading manuscript %s", manuscript_file)
            _select_upload_item(page, REQUIRED_ITEMS_DROPDOWN, "Manuscript")
            _attach_upload(page, manuscript_file, slot)
            slot += 1

            step("File uploads: competing interests")
            # Competing interests: upload a declaration, or tick the "none" confirmation.
            if no_competing_interests:
                checkbox = page.locator("#doiCheckbox")
                checkbox.click()
            else:
                if not declaration_file:
                    raise ValueError("no_competing_interests is false but no declaration_file was provided")
                logger.info("Uploading declaration of competing interests %s", declaration_file)
                _select_upload_item(page, REQUIRED_ITEMS_DROPDOWN, "Declaration of competing")
                _attach_upload(page, declaration_file, slot)
                slot += 1

            # Optional items (figures, LaTeX source, supplements) each open a fresh slot.
            for i, figure in enumerate(figures):
                step(f"File uploads: figure {i + 1} of {len(figures)}")
                logger.info("Uploading figure %s", figure)
                _select_upload_item(page, OPTIONAL_ITEMS_DROPDOWN, "Figure")
                _attach_upload(page, figure, slot)
                slot += 1
            if latex_source:
                step("File uploads: LaTeX source")
                logger.info("Uploading LaTeX source %s", latex_source)
                _select_upload_item(page, OPTIONAL_ITEMS_DROPDOWN, "LaTeX source files")
                _attach_upload(page, latex_source, slot)
                slot += 1
            for i, supplement in enumerate(supplements):
                step(f"File uploads: supplementary file {i + 1} of {len(supplements)}")
                logger.info("Uploading supplementary material %s", supplement)
                _select_upload_item(page, OPTIONAL_ITEMS_DROPDOWN, "Supplementary material")
                _attach_upload(page, supplement, slot)
                slot += 1

            page.wait_for_timeout(2000)  # let the upload progress bars finish
            page.get_by_label("Save and continue").click()

            step("Metadata")
            # Metadata (keywords are semicolon-separated).
            page.get_by_test_id("title-field-container").get_by_label("Your title").fill(title)
            page.get_by_test_id("abstract-field-container").get_by_label("Your abstract").fill(abstract)
            page.get_by_test_id("keywords-field-container").get_by_label("Your keywords").fill(keywords)
            page.get_by_label("Save and continue").click()

            # Authors.
            step("Authors")
            for i, author in enumerate(authors):
                step(f"Authors: author {i + 1} of {len(authors)}")
                if i > 0:
                    page.get_by_label("Add another author").click()
                _add_author(page, author)
            page.get_by_label("Save and continue").click()

            step("Funders")
            # Funders: answer "Did you receive any funding?" from the .sub's funding
            # field -- "No" when it is empty, else "Yes" and one row per funder.
            # Wait for this screen first so the Yes/No lookup cannot hit an
            # author-page control while Elsevier is still saving that step.
            page.wait_for_url(lambda url: "/steps/funders" in url, timeout=60000)
            answer = FUNDERS_YES_SELECTOR if funders else FUNDERS_NO_SELECTOR
            radio = page.locator(answer).or_(page.get_by_label("Yes" if funders else "No", exact=True)).first
            radio.wait_for(state="attached", timeout=60000)
            try:
                with unrecovered():
                    radio.check(timeout=5000)
            except Exception:  # noqa: BLE001 -- the radio may be visually hidden (tabindex=-1)
                radio.check(force=True)
            for i, funder in enumerate(funders):
                step(f"Funders: funder {i + 1} of {len(funders)}")
                if i > 0:
                    page.get_by_label("Add another funder").click()
                _add_funder(page, funder)
            page.get_by_label("Save and continue").click()
            page.wait_for_url(lambda url: "/steps/funders" not in url, timeout=60000)

            step("Research data")
            # Research data ("Provide additional information"): either link the
            # deposited dataset, or decline and pick a statement (an off-list one
            # goes in the "Other" free-text box). Wait for this screen's URL before
            # looking up its controls: a bare "No" lookup could otherwise hit the
            # funders page while Elsevier is still saving it.
            page.wait_for_url(lambda url: "/steps/questions" in url, timeout=60000)
            no_data = page.get_by_label("No", exact=True)
            no_data.wait_for(state="attached", timeout=60000)
            if share_data:
                page.get_by_label("Yes", exact=True).check()
                page.get_by_placeholder("Paste your data repository link here").fill(data_repository_url)
                page.locator(REPOSITORY_COMBOBOX).click()
                _choose_option(page, data_repository_name)
                if data_repository_name == "Other":
                    page.get_by_placeholder("Type your repository name here").fill(data_repository_name)  #! unverified
                if source_of_data == "original":
                    page.get_by_label("Original data", exact=True).check()
                elif source_of_data == "reference":
                    page.get_by_label("Reference data", exact=True).check()
                page.get_by_placeholder("Type your title here").fill(dataset_title)
            else:
                no_data.check()
                # Declining reveals a single statement drop-down.
                page.locator("#main [role=combobox]").last.click()
                if _choose_option(page, data_statement) == "Other":
                    page.locator("#main textarea").last.fill(data_statement)
            page.get_by_label("Save and continue").click()

            step("Final declarations")
            # Final declarations: tick the standard boxes only when the author confirmed.
            page.wait_for_url(lambda url: "/steps/review" in url, timeout=60000)
            if declarations_confirmed:
                for box_id in DECLARATION_CHECKBOX_IDS:
                    box = page.locator(f"#{box_id}")
                    box.wait_for(state="attached", timeout=30000)
                    if not box.is_checked():
                        box.check(force=True)  # styled checkbox: the input itself may be hidden

            logger.info("Reached the final declarations step; leaving the browser open for review (the final submit is left to you)")
            hold_open()

    # * login
    def login(self, page, username: str, password: str, *, timeout_ms: int = 20000) -> None:
        """Drive the Elsevier sign-in from stored credentials.

        Walks the recorded federated flow: start a submission, enter the email,
        either use password or enter institutional information.

        The flow short-circuits between steps: any point at which the dashboard's
        start-submission control is visible means the session is already signed in
        (Elsevier can restore one mid-flow, and the recorded steps drift as the
        portal is restyled), so the sign-in returns successfully rather than
        pressing on through steps whose selectors no longer apply.
        """

        def signed_in(what: str) -> bool:
            """True if the dashboard marker is up (returns early on the signed-out page)."""
            if not self.is_logged_in(page, timeout_ms=LOGIN_PROBE_MS):
                return False
            logger.info("Discrete Mathematics: already signed in at %s; skipping the rest of the sign-in", what)
            return True

        logger.debug("Filling the Discrete Mathematics (Elsevier) sign-in flow at %s", self.LOGIN_URL)
        page.goto(self.LOGIN_URL)
        _accept_cookies(page)
        if signed_in("the landing page"):
            return

        page.get_by_label(SIGNED_OUT_START_LABEL, exact=True).click()
        _accept_cookies(page)
        if signed_in("the start-a-submission step"):
            return

        # Elsevier's sign-in opens on an email box -- unless it remembers the
        # account from an earlier visit (cookies in the saved session), in which
        # case it skips straight to "Access through your organization". Wait for
        # whichever comes first.
        email_box = page.get_by_role("textbox", name="Email").or_(page.locator(ELSEVIER_EMAIL_SELECTOR))
        institution_button = page.get_by_role("button", name=INSTITUTION_ACCESS_BUTTON)
        try:
            email_box.or_(institution_button).or_(page.get_by_test_id(START_SUBMISSION_TESTID)).first.wait_for(state="visible", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            if signed_in("the email step"):
                return
            raise DiscreteMathematicsLoginError("could not find the email field on the Elsevier sign-in page " "(the portal may have changed); re-capture the selectors with " f"'playwright codegen {self.LOGIN_URL}'") from exc
        if page.get_by_test_id(START_SUBMISSION_TESTID).first.is_visible():
            logger.info("Discrete Mathematics: already signed in; skipping the rest of the sign-in")
            return
        remembered = not email_box.first.is_visible()
        if remembered:
            logger.info("Elsevier remembered the account; skipping the email step")
        else:
            email_box.first.type(username)
            _try(lambda: page.get_by_role("button", name="Continue").click(), "continue past email")
            if signed_in("the email step"):
                return

        password_box = page.get_by_role("textbox", name="Password")
        if not remembered and password_box.count() > 0:
            password_box.first.fill(password)
            page.get_by_label("Sign in").click()
            if signed_in("the email step"):
                return
            if not self.is_logged_in(page, timeout_ms=timeout_ms):
                raise DiscreteMathematicsLoginError("submitted the credentials but the signed-in Elsevier dashboard " "did not load -- the username or password may be wrong, or the " "sign-in needs a two-factor (Duo) approval that can't be automated")

        # Institution federation: "Access through your institution"
        if not username.lower().endswith("@caltech.edu"):
            raise DiscreteMathematicsLoginError("the institutional sign-in flow is hard-coded for Caltech; " "use a Caltech email address to test the institutional login")

        _try(lambda: page.get_by_role("button", name=INSTITUTION_ACCESS_BUTTON).click(), "access through institution")
        _accept_cookies(page)
        _try(lambda: page.get_by_role("link", name=INSTITUTION_LINK).click(), "choose Institution")

        # Institution federated login form (username + password).
        user_box = page.get_by_role("textbox", name="Username")
        pwd_box = page.get_by_role("textbox", name="Password")
        try:
            user_box.first.wait_for(state="visible", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise DiscreteMathematicsLoginError("reached the institutional step but the Institution username/password " "form did not load (the federated login may have changed)") from exc
        # get part of username before "@" symbol
        institution_username = username.split("@")[0]
        user_box.first.fill(institution_username)
        pwd_box.first.fill(password)
        pwd_box.first.press("Enter")

        # Caltech then asks for a Duo verified push, which only the author can
        # approve on their phone. Show the code and wait for the approval, clicking
        # through Duo's "Yes, this is my device" trust prompt when it appears.
        self._wait_for_duo(page, timeout_ms)

        # If the dashboard still does not come up, hand back to the manual fallback.
        if not self.is_logged_in(page, timeout_ms=timeout_ms):
            page.goto(self.PORTAL_URL)
            if not self.is_logged_in(page, timeout_ms=timeout_ms):
                raise DiscreteMathematicsLoginError("submitted the credentials but the signed-in Elsevier dashboard " "did not load -- the username or password may be wrong, or the " "sign-in needs a two-factor (Duo) approval that can't be automated")

    def _wait_for_duo(self, page, timeout_ms: int) -> None:
        """Wait (up to :data:`DUO_WAIT_MS`) for the author to approve a Duo prompt.

        Returns at once if the page never reaches Duo. Otherwise prints the
        verification code Duo shows (for a verified push) and polls until the page
        leaves Duo, clicking the "Yes, this is my device" trust button on the way.
        """
        try:
            page.wait_for_url(re.compile(r"duosecurity\.com|submit\.elsevier\.com"), timeout=timeout_ms)
        except Exception:  # noqa: BLE001 -- neither came up; the caller's check decides
            return
        if "duosecurity.com" not in page.url:
            return
        code = ""
        try:
            page.wait_for_timeout(2000)
            match = re.search(r"\b(\d{3,6})\b", page.locator("body").inner_text(timeout=5000))
            code = match.group(1) if match else ""
        except Exception:  # noqa: BLE001
            pass
        msg = f"Approve the Duo prompt on your phone{f' (verification code {code})' if code else ''}; waiting up to {DUO_WAIT_MS // 1000}s…"
        logger.info("Discrete Mathematics: %s", msg)
        print(msg, flush=True)
        trust = page.get_by_role("button", name=DUO_TRUST_BUTTON)
        deadline = time.monotonic() + DUO_WAIT_MS / 1000
        while time.monotonic() < deadline and "duosecurity.com" in page.url:
            if trust.count() and trust.first.is_visible():
                _try(lambda: trust.first.click(), "trust this device in Duo")
            page.wait_for_timeout(1000)


VENUE = DiscreteMathematicsVenue()
