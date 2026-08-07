"""Shared Playwright runner for the EditFlow author-submission form.

EditFlow is unusual among paperpush's portals: authors do not create or sign in
to an account before a new submission. They open the public form directly and
receive a private status link by email after submitting. Consequently this
runner declares ``requires_login = False`` and never stores credentials or a
browser session.

The form recorded for Combinatorica is EditFlow's standard mathematics-journal
flow: author agreement, author count and contact details, title and Mathematics
Subject Classification, then editor choice, arXiv reference, and abstract. The
runner deliberately stops on that last page before the live Submit control.
"""

from __future__ import annotations

import logging

from playwright.sync_api import sync_playwright

from ...validate import parse_authors
from ..base import Venue
from ..common import DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts, hold_open, hold_open_on_failure

logger = logging.getLogger(__name__)

NEXT_STEP = "Next Step"


def _next(page) -> None:
    """Advance one EditFlow page using its repeated Next Step control."""
    page.get_by_role("button", name=NEXT_STEP).click()


def _select_label_or_value(select, choice: str) -> None:
    """Select a stable visible label, accepting an internal value as fallback."""
    try:
        # EditFlow enhances the editor multi-select with JavaScript and hides
        # the real <select>. Selecting it still fires the normal input/change
        # events, but Playwright needs ``force`` because it is not visible.
        select.select_option(label=choice, force=True)
    except Exception:  # noqa: BLE001 -- a fixture may intentionally use the portal value
        select.select_option(value=choice, force=True)


class EditFlowVenue(Venue):
    """EditFlow's public, loginless new-submission form."""

    requires_login = False
    supports_session_capture = False

    def login(self, page, username: str, password: str, *, timeout_ms: int = 15000) -> None:
        """EditFlow has no author login; new submissions use the public form."""
        raise NotImplementedError(f"{self.slug} uses EditFlow's public submission form and does not require login")

    def submit(
        self,
        values: dict,
        headless: bool = False,
        debug: bool = False,
        new_session: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        keep_open_on_failure: bool = True,
    ) -> None:
        """Fill EditFlow from ``values`` and stop before its final Submit button."""
        if debug:
            headless = False

        if str(values.get("author_agreement", "")).strip().lower() not in {"yes", "true", "1", "on"}:
            raise ValueError("author_agreement must be confirmed before opening the EditFlow submission form")

        venue = self.display_name
        author_field = next((field for field in self._VENUE.fields if field.type == "authorlist"), None)
        authors = parse_authors(values.get("authors", ""), author_field.fields if author_field else None)
        if not authors:
            raise ValueError("at least one author is required")

        title = values.get("title", "").strip()
        msc_classification = values.get("msc_classification", "").strip()
        handling_editor = values.get("handling_editor", "").strip()
        arxiv_reference = values.get("arxiv_reference", "").strip()
        abstract = values.get("abstract", "").strip()

        logger.info("Starting %s EditFlow submission (%d author(s), headless=%s, debug=%s)", venue, len(authors), headless, debug)

        with sync_playwright() as playwright, hold_open_on_failure(headless=headless, keep_open=keep_open_on_failure):
            browser = playwright.chromium.launch(headless=headless)
            # EditFlow starts every new submission without an account or saved
            # session. ``new_session`` is accepted for the uniform runner API but
            # is therefore intentionally immaterial here.
            context = browser.new_context()
            apply_default_timeouts(context, timeout)
            page = context.new_page()
            self.ensure_signed_in(page, context, debug=debug)

            if debug:
                page.pause()

            # Step 0: this is an author attestation and is checked only because
            # the .sub's never-autofilled confirmation is explicitly affirmative.
            page.get_by_role("checkbox", name="I have read and understood").check()
            _next(page)

            # Step 1: declare the author count. EditFlow then shows an intermediate
            # author-list page before the individual author-details form.
            page.get_by_role("textbox", name="How many authors does your").fill(str(len(authors)))
            _next(page)
            _next(page)

            # Step 2: repeated author controls share accessible names, so address
            # them by author position. Email is entered first because EditFlow may
            # use it to recognize a returning author and prefill the other fields.
            emails = page.get_by_role("textbox", name="Email Address")
            given_names = page.get_by_role("textbox", name="Given Name")
            middle_names = page.get_by_role("textbox", name="Middle Name")
            family_names = page.get_by_role("textbox", name="Family Name")
            mr_ids = page.get_by_role("textbox", name="MR author ID")
            institutions = page.get_by_role("textbox", name="Institution")
            # The institution input and country menu both carry the same generic
            # accessible label ("This field is required."), so the recording's
            # label locator is ambiguous. Their numbered IDs are stable across
            # authors; select only the country controls by prefix.
            countries = page.locator('select[id^="author_paper-submission_country"]')

            for index, author in enumerate(authors):
                emails.nth(index).fill(author.get("email", ""))
                given_names.nth(index).fill(author.get("first_name", ""))
                middle = author.get("middle_name", "")
                if middle:
                    middle_names.nth(index).fill(middle)
                family_names.nth(index).fill(author.get("last_name", ""))
                mr_id = author.get("mr_author_id", "")
                if mr_id:
                    mr_ids.nth(index).fill(mr_id)
                institutions.nth(index).fill(author.get("institution", ""))
                countries.nth(index).select_option(label=author.get("country", ""))
            _next(page)

            # Step 3: the MSC control validates on navigation; a venue-specific
            # field validator catches malformed codes before the browser opens.
            page.get_by_role("textbox", name="Article Title").fill(title)
            page.locator("#mscp0").fill(msc_classification)
            _next(page)

            # Final form page. Editor choice is an author policy decision, so its
            # .sub field is never autofilled. Prefer a readable label; accept the
            # portal's numeric option value for stable test fixtures.
            _select_label_or_value(page.locator('[id="editor[]"]'), handling_editor)
            # Like the enhanced editor selector above, EditFlow may hide this
            # real validated input behind its arXiv widget. Forced fill targets
            # the underlying text control and still dispatches input events.
            arxiv_input = page.locator("#papers-arxiv_reference")
            arxiv_input.fill(arxiv_reference, force=True)
            # EditFlow validates the ID and updates its dependent required
            # fields from this jQuery change handler (not from input alone).
            arxiv_input.dispatch_event("change")
            page.locator("#papers-abstract").fill(abstract, force=True)

            logger.info("Reached %s's final EditFlow submission page; leaving the browser open for review (Submit is left to you)", venue)
            hold_open()
