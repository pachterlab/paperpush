"""alphaXiv's direct publishing flow (the home page's "Publish" dialog).

The dialog walks Upload, Extract, Edit, Review, Published. Pressing "Submit" on
the Edit step starts an automated review that publishes the paper by itself, so
the run stops on the Edit step and never presses it.
"""

from __future__ import annotations

import logging
import re

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from ...database import get_venue
from ...validate import _truthy_bool, parse_authors
from ..base import Venue
from ..common import DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts, hold_open, hold_open_on_failure, open_run_context
from ..login import VenueLoginError
from ..recovery import guard, step, unrecovered
from ..recovery_channel import browser_launch_args

logger = logging.getLogger(__name__)

SIGNIN_URL = "https://www.alphaxiv.org/signin"
PUBLISH_BUTTON = "Publish"
UPLOAD_PROMPT = "Click to select a PDF file"
TITLE = "#submission-title"
ABSTRACT = "#submission-abstract"
DATE = "#submission-date"
SLUG = "#submission-slug"
BIBTEX = "#submission-bibtex"
AUTHOR_NAME = "Author name"
ADD_AUTHOR = "Add author"
REMOVE_AUTHOR = "Remove author"
ORGANIZATION_NAME = "Organization name"
ADD_ORGANIZATION = "Add organization"
REMOVE_ORGANIZATION = "Remove organization"
RIGHTS_LABEL = "I have the right to publish this content on alphaXiv"
PICKER_XPATH = "xpath=//label[starts-with(normalize-space(.),'{label}')]/following-sibling::*[1]"
# The upload is parsed by alphaXiv's extraction service before the form appears.
EXTRACTION_TIMEOUT_MS = 300_000
DIALOG_READY_TIMEOUT_MS = 30_000


class AlphaXivError(RuntimeError):
    """The run cannot continue for a reason the author has to resolve."""


def _lines(value) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _items(value) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _validate_date(value: str) -> str:
    if value.strip() and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        raise ValueError("publication_date must be written as YYYY-MM-DD")
    return value


FIELD_VALIDATORS = {"publication_date": _validate_date}


def _set_rows(page, names: list[str], placeholder: str, add_name: str, remove_name: str) -> None:
    """Make an author or organization editor hold exactly ``names``, in order."""
    rows = page.get_by_placeholder(placeholder)
    for index, name in enumerate(names):
        if index >= rows.count():
            page.get_by_role("button", name=add_name, exact=True).click()
        row = rows.nth(index)
        row.fill(name)
        # Typing opens a suggestion list over the next control. Escape would also close the dialog, so drop focus instead.
        row.blur()
    while rows.count() > len(names):
        page.get_by_role("button", name=remove_name, exact=True).last.click()


def _set_picker(page, label: str, items: list[str]) -> None:
    """Replace a category picker's chips with ``items``; Enter takes the best match."""
    box = page.locator(PICKER_XPATH.format(label=label))
    field = box.locator("input")
    for _ in range(box.locator("button").count()):
        field.press("Backspace")
    for count, item in enumerate(items, start=1):
        field.fill(item)
        field.press("Enter")
        if box.locator("button").count() != count:
            raise AlphaXivError(f"alphaXiv's {label} picker has no match for {item!r}")


class AlphaXiv(Venue):
    slug = "alphaxiv"
    logged_in_names = ("My Library", "Settings")
    supports_recovery = True
    final_submit_names = ("Submit", "Submit paper for review", "Publish anyway")
    _VENUE = get_venue(slug)

    @property
    def login_url(self) -> str:
        return SIGNIN_URL

    def login(self, page, username: str, password: str, *, timeout_ms: int = 15000) -> None:
        """Sign in through alphaXiv's two-step email, then password, form."""
        page.goto(SIGNIN_URL)
        email = page.locator("input#email")
        email.fill(username)
        email.press("Enter")
        field = page.locator('input[name="password"]')
        try:
            page.wait_for_function("() => { const e = document.querySelector('input[name=password]'); return e && e.getAttribute('aria-hidden') !== 'true' && e.tabIndex !== -1; }", timeout=timeout_ms)
        except PWTimeout as exc:
            raise VenueLoginError("alphaXiv did not ask for a password after the email step; this account may use Google, Apple, Hugging Face, or an emailed code") from exc
        field.fill(password)
        field.press("Enter")
        try:
            page.wait_for_url(lambda url: "/signin" not in url, timeout=timeout_ms)
        except PWTimeout as exc:
            raise VenueLoginError("submitted the credentials but alphaXiv stayed on the sign-in page; the email or password may be wrong, or a verification step was added") from exc
        if not self.is_logged_in(page, timeout_ms=timeout_ms):
            raise VenueLoginError("alphaXiv left the sign-in page but the signed-in navigation did not load")

    def submit(
        self,
        values: dict,
        headless: bool = False,
        debug: bool = False,
        new_session: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        keep_open_on_failure: bool = True,
    ) -> None:
        """Upload the PDF and fill alphaXiv's Edit step, stopping before Submit."""
        if debug:
            headless = False
        if headless:
            raise AlphaXivError("alphaXiv's Publish dialog needs a visible browser: its live connection does not open in headless Chromium, so run without --headless")

        authors = [author["name"] for author in parse_authors(values["authors"], ["name"])]
        organizations = _lines(values.get("organizations"))
        categories = _items(values.get("categories"))
        subcategories = _items(values.get("subcategories"))

        with sync_playwright() as playwright, hold_open_on_failure(headless=headless, keep_open=keep_open_on_failure):
            browser = playwright.chromium.launch(headless=headless, args=browser_launch_args())
            context = open_run_context(browser, self.session_path(), new_session=new_session)
            apply_default_timeouts(context, timeout)
            page = context.new_page()

            self.ensure_signed_in(page, context, debug=debug)
            page = guard(page)

            if debug:
                page.pause()

            step("Open Publish dialog")
            page.goto(self.portal_url)
            page.get_by_role("button", name=PUBLISH_BUTTON, exact=True).first.click()
            prompt = page.get_by_text(UPLOAD_PROMPT)
            with unrecovered():
                try:
                    prompt.wait_for(state="visible", timeout=DIALOG_READY_TIMEOUT_MS)
                except PWTimeout as exc:
                    raise AlphaXivError("alphaXiv is not offering a new upload, most likely because an unfinished draft already exists. Open Publish on alphaxiv.org, finish it or press Cancel draft, then run this again") from exc

            step("Upload PDF")

            def attach(target) -> None:
                with target.expect_file_chooser() as chooser:
                    target.get_by_text(UPLOAD_PROMPT).click()
                chooser.value.set_files(values["manuscript_file"])

            page.attempt("attach the paper PDF", attach)

            step("Wait for extraction")
            page.locator(TITLE).wait_for(state="visible", timeout=EXTRACTION_TIMEOUT_MS)

            step("Edit metadata")
            page.locator(TITLE).fill(values["title"])
            page.locator(ABSTRACT).fill(values["abstract"])
            _set_rows(page, authors, AUTHOR_NAME, ADD_AUTHOR, REMOVE_AUTHOR)
            if organizations:
                _set_rows(page, organizations, ORGANIZATION_NAME, ADD_ORGANIZATION, REMOVE_ORGANIZATION)
            if values.get("publication_date"):
                page.locator(DATE).fill(values["publication_date"].strip())
            if values.get("url_slug"):
                page.locator(SLUG).fill(values["url_slug"].strip())
            if categories:
                _set_picker(page, "Categories", categories)
            if subcategories:
                _set_picker(page, "Subcategories", subcategories)
            if values.get("bibtex"):
                page.locator(BIBTEX).fill(values["bibtex"])
            if _truthy_bool(values.get("agree_terms", "")):
                page.get_by_text(RIGHTS_LABEL).click()

            logger.info("Filled alphaXiv's Edit step; review it and press Submit yourself. Submit starts an automated review that publishes the paper without another confirmation.")
            hold_open()


VENUE = AlphaXiv()
