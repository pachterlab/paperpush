"""Sign-in and submission for Discrete Mathematics (Elsevier).

Discrete Mathematics submits through Elsevier's newer portal at
``https://submit.elsevier.com/DISCM`` (a React app addressed by accessible names
and a few ``data-testid`` hooks). Selectors were captured with ``playwright
codegen``; re-capture the same way if Elsevier restyles the portal. The wizard is
driven straight through, stops before the final submit, and leaves the browser
open via :func:`hold_open`.

Sign-in authenticates through the author's institution. The recorded flow is
Caltech-specific (Elsevier -> "Access through your institution" -> Caltech Library
-> Duo two-factor); the Duo step can't be automated, so a failed auto sign-in
hands back to a manual sign-in and the saved session is reused later. Point it at
another institution by re-recording :meth:`~DiscreteMathematicsVenue.login`.
"""

from __future__ import annotations

import logging

from playwright.sync_api import sync_playwright

from ...database import get_venue
from ...validate import parse_authors
from ..base import Venue
from ..common import (DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts,
                      hold_open, open_run_context, split_name_first_last)
from ..login import VenueLoginError

logger = logging.getLogger(__name__)

_YES = {"yes", "y", "true", "1", "on"}

# Sign-in institution (Caltech-specific; see the module docstring).
INSTITUTION_ACCESS_BUTTON = "Access through your"
INSTITUTION_LINK = "Caltech Library Sign in with"
# The cookie banner reappears across the sign-in navigations.
COOKIE_ACCEPT = "Accept all cookies"
# Start-submission control on the dashboard; present only once signed in (the
# logged-out landing page shows a plain "Start a submission" button instead).
START_SUBMISSION_TESTID = "submission-start-button"
# Upload-step item-type dropdowns.
REQUIRED_ITEMS_DROPDOWN = "#trigger-input-required-items-dropdown"
OPTIONAL_ITEMS_DROPDOWN = "#trigger-input-optional-items-dropdown"


class DiscreteMathematicsLoginError(VenueLoginError):
    """Raised when an automatic Discrete Mathematics (Elsevier) sign-in fails."""


# --- small helpers -----------------------------------------------------------


def _is_yes(value: str) -> bool:
    """True when a ``.sub`` value reads as an affirmative (yes/true/1/on)."""
    return str(value or "").strip().lower() in _YES


def _try(fn, what: str) -> bool:
    """Run ``fn`` best-effort; log and continue on failure (the fragile login flow)."""
    try:
        fn()
        return True
    except Exception as exc:  # noqa: BLE001 -- best-effort optional step
        logger.warning("Discrete Mathematics: skipped %s (%s)", what, exc)
        return False


def _accept_cookies(page) -> None:
    """Dismiss the cookie banner if it is showing (it reappears across navigations)."""
    banner = page.get_by_role("button", name=COOKIE_ACCEPT)
    try:
        if banner.first.is_visible():
            banner.first.click()
    except Exception:  # noqa: BLE001 -- a missing banner is fine
        pass


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
    button = page.get_by_test_id("upload-button").nth(slot_index)
    with page.expect_file_chooser() as fc_info:
        button.click()
    fc_info.value.set_files(path)
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

    page.get_by_role("button", name="Add another author").click()
    page.get_by_role("textbox", name="First name").fill(first)
    page.get_by_role("textbox", name="Last name").fill(last)

    if institution:
        page.get_by_role("combobox", name="Institution").fill(institution)
        page.wait_for_timeout(800)
        _pick_institution(page, institution)

    if email:
        page.get_by_role("textbox", name="Email address").fill(email)
    if _is_yes(author.get("corresponding")):
        page.get_by_role("checkbox", name="This is the corresponding").check()

    page.get_by_role("button", name="Confirm").click()


class DiscreteMathematicsVenue(Venue):
    """Discrete Mathematics on Elsevier's ``submit.elsevier.com`` submission portal."""

    slug = "discrete_mathematics"

    _VENUE = get_venue(slug)
    SITE_URL = _VENUE.site_url
    PORTAL_URL = _VENUE.submission_url  # inherited portal_url resolves to this
    LOGIN_URL = "https://submit.elsevier.com/DISCM"

    @property
    def login_url(self) -> str:
        return self.LOGIN_URL

    def is_logged_in(self, page, *, timeout_ms: int | None = None) -> bool:
        """Signed in iff the dashboard's ``submission-start-button`` control is present.

        The logged-out landing page shows a plain "Start a submission" button
        instead, so the testid distinguishes the two states.
        """
        if timeout_ms is None:
            timeout_ms = self.logged_in_timeout_ms
        marker = page.get_by_test_id(START_SUBMISSION_TESTID)
        try:
            marker.first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:  # noqa: BLE001 -- not visible within the budget means signed out
            return False

    # * run
    def submit(
        self,
        values: dict,
        headless: bool = False,
        debug: bool = False,
        new_session: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
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
        data_statement = values.get("data_statement", "").strip()
        declarations_confirmed = _is_yes(values.get("declarations_confirmed", ""))

        author_field = next((f for f in self._VENUE.fields if f.type == "authorlist"), None)
        authors = parse_authors(values.get("authors", ""), author_field.fields if author_field else None)

        logger.info("Starting Discrete Mathematics submission run (headless=%s, debug=%s): manuscript=%s, %d author(s)", headless, debug, manuscript_file, len(authors))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            context = open_run_context(browser, self.session_path(), new_session=new_session)
            apply_default_timeouts(context, timeout)
            page = context.new_page()

            self.ensure_signed_in(page, context, debug=debug)

            if debug:
                page.pause()

            _accept_cookies(page)

            logger.debug("Signed in; starting a new submission")
            page.get_by_test_id(START_SUBMISSION_TESTID).click()

            # Article type.
            if article_type:
                page.get_by_role("radio", name=article_type).check()
            page.get_by_role("button", name="Save and continue").click()

            # File uploads: manuscript is the first required item.
            slot = 0
            logger.info("Uploading manuscript %s", manuscript_file)
            _select_upload_item(page, REQUIRED_ITEMS_DROPDOWN, "Manuscript")
            _attach_upload(page, manuscript_file, slot)
            slot += 1

            # Competing interests: upload a declaration, or tick the "none" confirmation.
            if no_competing_interests:
                # Custom React checkbox: it tracks state via a data-checked attribute,
                # not the native `checked` property, so check() would wrongly report the
                # click as ineffective. A plain click toggles it.
                page.get_by_role("checkbox", name="I confirm that no authors of").click()
            else:
                if not declaration_file:
                    raise ValueError("no_competing_interests is false but no declaration_file was provided")
                logger.info("Uploading declaration of competing interests %s", declaration_file)
                _select_upload_item(page, REQUIRED_ITEMS_DROPDOWN, "Declaration of competing")
                _attach_upload(page, declaration_file, slot)
                slot += 1

            # Optional items (figures, LaTeX source, supplements) each open a fresh slot.
            for figure in figures:
                logger.info("Uploading figure %s", figure)
                _select_upload_item(page, OPTIONAL_ITEMS_DROPDOWN, "Figure")
                _attach_upload(page, figure, slot)
                slot += 1
            if latex_source:
                logger.info("Uploading LaTeX source %s", latex_source)
                _select_upload_item(page, OPTIONAL_ITEMS_DROPDOWN, "LaTeX source files")
                _attach_upload(page, latex_source, slot)
                slot += 1
            for supplement in supplements:
                logger.info("Uploading supplementary material %s", supplement)
                _select_upload_item(page, OPTIONAL_ITEMS_DROPDOWN, "Supplementary material")
                _attach_upload(page, supplement, slot)
                slot += 1

            page.wait_for_timeout(2000)  # let the upload progress bars finish
            page.get_by_test_id("submission-uploads-save-button").click()

            # Metadata (keywords are semicolon-separated).
            page.get_by_role("textbox", name="Your title").fill(title)
            page.get_by_role("textbox", name="Your abstract").fill(abstract)
            page.get_by_role("textbox", name="Your keywords").fill(keywords)
            page.get_by_role("button", name="Save and continue").click()

            # Authors.
            for author in authors:
                _add_author(page, author)
            page.get_by_role("button", name="Save and continue").click()

            # Research data: decline to share, then pick the no-data statement.
            page.get_by_role("radio", name="No", exact=True).check()
            if data_statement:
                page.get_by_text("Select an option", exact=True).click()
                page.get_by_text(data_statement, exact=True).click()
            page.get_by_role("button", name="Save and continue").click()

            # Final declarations: tick the standard boxes only when the author confirmed.
            if declarations_confirmed:
                for name in (
                    "I have read and accept the ethics in publishing policy",
                    "I have read and accept the copyright terms",
                    "I confirm I have mentioned",
                ):
                    page.get_by_role("checkbox", name=name).check()

            logger.info("Reached the final declarations step; leaving the browser open for review (the final submit is left to you)")
            hold_open()

    # * login
    def login(self, page, username: str, password: str, *, timeout_ms: int = 20000) -> None:
        """Drive the Elsevier institutional (Caltech) sign-in from stored credentials.

        Walks the recorded federated flow: start a submission, enter the email,
        choose "Access through your institution", follow the Caltech Library link,
        and fill the username/password form. The Duo two-factor step cannot be
        scripted, so if the signed-in dashboard does not appear this raises
        :class:`DiscreteMathematicsLoginError`, letting ``ensure_signed_in`` fall
        back to a manual sign-in (where the human approves Duo in the open window).
        """
        logger.debug("Filling the Discrete Mathematics (Elsevier) sign-in flow at %s", self.LOGIN_URL)
        page.goto(self.LOGIN_URL)
        _accept_cookies(page)

        _try(lambda: page.get_by_role("button", name="Start a submission").click(), "start a submission (pre-login)")
        _accept_cookies(page)

        email_box = page.get_by_role("textbox", name="Email")
        try:
            email_box.first.wait_for(state="visible", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise DiscreteMathematicsLoginError("could not find the email field on the Elsevier sign-in page " "(the portal may have changed); re-capture the selectors with " f"'playwright codegen {self.LOGIN_URL}'") from exc
        email_box.first.fill(username)
        _try(lambda: page.get_by_role("button", name="Continue").click(), "continue past email")

        # Institution federation: "Access through your institution" -> Caltech Library.
        _try(lambda: page.get_by_role("button", name=INSTITUTION_ACCESS_BUTTON).click(), "access through institution")
        _accept_cookies(page)
        _try(lambda: page.get_by_role("link", name=INSTITUTION_LINK).click(), "choose Caltech Library")

        # Caltech federated login form (username + password).
        user_box = page.get_by_role("textbox", name="Username")
        pwd_box = page.get_by_role("textbox", name="Password")
        try:
            user_box.first.wait_for(state="visible", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise DiscreteMathematicsLoginError("reached the institutional step but the Caltech username/password " "form did not load (the federated login may have changed)") from exc
        user_box.first.fill(username)
        pwd_box.first.fill(password)
        pwd_box.first.press("Enter")

        # Duo two-factor ("Yes, this is my device") cannot be automated; if the
        # dashboard does not come up, hand back to the manual fallback.
        if not self.is_logged_in(page, timeout_ms=timeout_ms):
            page.goto(self.PORTAL_URL)
            if not self.is_logged_in(page, timeout_ms=timeout_ms):
                raise DiscreteMathematicsLoginError("submitted the credentials but the signed-in Elsevier dashboard " "did not load -- the username or password may be wrong, or the " "sign-in needs a two-factor (Duo) approval that can't be automated")


VENUE = DiscreteMathematicsVenue()
