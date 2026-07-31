from __future__ import annotations

import logging

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from ...database import get_venue
from ...validate import _truthy_bool, parse_authors
from ..base import Venue
from ..common import (DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts,
                      hold_open, hold_open_on_failure, open_run_context, _try)
from ..login import VenueLoginError

logger = logging.getLogger(__name__)


class ArxivVenue(Venue):
    slug = "arxiv"  # * slug name
    logged_in_names = ("START NEW SUBMISSION",)  # * a button I expect to see when signed in

    _VENUE = get_venue(slug)
    SITE_URL = _VENUE.site_url
    LOGIN_URL = f"{SITE_URL}/login"
    USER_URL = f"{SITE_URL}/user"

    # * login
    def login(self, page, username: str, password: str, *, timeout_ms: int = 15000) -> None:
        """
        Fill and submit the arXiv sign-in form from stored credentials. Falls back to manual sign-in if the automatic attempt fails.
        """
        logger.debug("Filling the arXiv sign-in form at %s", self.LOGIN_URL)
        page.goto(self.LOGIN_URL)
        user_box = page.get_by_role("textbox", name="Username or e-mail")
        pwd_box = page.get_by_role("textbox", name="Password")

        try:
            user_box.wait_for(state="visible", timeout=timeout_ms)
            pwd_box.wait_for(state="visible", timeout=timeout_ms)
        except PWTimeout as exc:
            raise VenueLoginError("could not find the username/password fields on the arXiv sign-in " "page (the sign-in form may have changed)") from exc

        user_box.fill(username)
        pwd_box.fill(password)
        page.get_by_role("button", name="Submit").click()

        # A successful sign-in lands on the user dashboard, where the "START NEW SUBMISSION" link renders. If it never appears, the sign-in did not take (navigate to /user once in case the landing page differs).
        if not self.is_logged_in(page, timeout_ms=timeout_ms):
            page.goto(self.USER_URL)
            if not self.is_logged_in(page, timeout_ms=timeout_ms):
                raise VenueLoginError("submitted the credentials but the signed-in arXiv dashboard did " "not load -- the username or password may be wrong, or arXiv " "added a step (CAPTCHA / two-factor) that can't be automated")

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
        """
        Open arXiv, sign in, then drive the submission wizard from a ``.sub`` file.
        """
        # The Inspector needs a visible browser; debugging headless makes no sense.
        if debug:
            headless = False

        # grab values from the .sub file
        primary_archive = values.get("primary_archive", "").strip()
        primary_category = values.get("primary_category", "").strip()
        manuscript_file = values.get("manuscript_file", "").strip()
        title = values.get("title", "")
        abstract = values.get("abstract", "")
        comments = values.get("comments", "").strip()
        report_number = values.get("report_number", "").strip()
        journal_reference = values.get("journal_reference", "").strip()
        acm_class = values.get("acm_class", "").strip()
        msc_class = values.get("msc_class", "").strip()
        doi = values.get("doi", "").strip()
        crosslist_archives = values.get("crosslist_archives", "").strip()
        crosslist_categories = values.get("crosslist_categories", "").strip()
        keep_all_files = _truthy_bool(values.get("keep_all_files", "").strip() or "yes")

        # The arXiv metadata form takes one comma-separated *Author(s) string, and
        # the venue's author list is name-only, so the block collapses to the names.
        author_field = next((f for f in self._VENUE.fields if f.type == "authorlist"), None)
        author_lines = parse_authors(values.get("authors", ""), author_field.fields if author_field else None)
        authors = ", ".join(a["name"] for a in author_lines if a.get("name"))

        logger.info("Starting arXiv submission run (headless=%s, debug=%s, archive=%s, " "category=%s, manuscript=%s)", headless, debug, primary_archive, primary_category, manuscript_file)

        with sync_playwright() as playwright, hold_open_on_failure(headless=headless, keep_open=keep_open_on_failure):
            browser = playwright.chromium.launch(headless=headless)

            context = open_run_context(browser, self.session_path(), new_session=new_session)
            apply_default_timeouts(context, timeout)
            page = context.new_page()

            self.ensure_signed_in(page, context, debug=debug)

            if debug:
                page.pause()

            logger.debug("Signed in; starting a new submission")
            page.get_by_role("link", name="START NEW SUBMISSION").click()

            # Terms/policy acknowledgement
            page.locator('input[name="userinfo"]').check()
            checkbox = page.locator('input[name="agree_terms_conditions"]')
            checkbox.click()
            terms = page.locator("#modal-1-content")
            terms.evaluate("(el) => el.scrollTop = el.scrollHeight")
            page.get_by_role("button", name="Accept these Terms and return").click()

            # License/policy + category
            radios = page.get_by_role("radio")
            radios.first.check()
            radios.nth(2).check()
            page.locator("#select_arch").select_option(primary_archive)
            page.locator("#select_sc").select_option(primary_category)
            page.get_by_role("button", name="Continue").nth(1).click()

            # Upload manuscript
            logger.info("Uploading manuscript file %s", manuscript_file)
            with page.expect_file_chooser() as fc_info:
                page.get_by_role("button", name="Choose File").click()
            fc_info.value.set_files(manuscript_file)
            page.get_by_role("button", name="Upload").click()
            page.locator("#check-files-button-top").click()
            if manuscript_file.lower().endswith(".tex") or manuscript_file.lower().endswith(".zip"):
                page.wait_for_timeout(3000)
                if keep_all_files:
                    # keep all files
                    _try(lambda: page.get_by_role("link", name="Keep All").click(), "Keep All")
                    _try(lambda: page.locator("#check-files-button-top").click(timeout=120_000), "Accept and Continue")
                else:
                    # delete auto-selected files
                    _try(lambda: page.locator("#check-files-button-top").click(), "Accept and Continue")
                    _try(lambda: page.get_by_role("button", name="Confirm").click(), "Confirm")
            page.get_by_role("link", name="Continue").click()

            # Fill in the metadata form.
            page.get_by_role("textbox", name="*Title").fill(title)
            page.get_by_role("textbox", name="*Author(s)").fill(authors)
            page.locator('textarea[name="abstract"]').fill(abstract)
            if comments:
                page.locator('textarea[name="comments"]').fill(comments)
            if report_number:
                page.locator('input[name="report_num"]').fill(report_number)
            if journal_reference:
                page.locator('input[name="journal_ref"]').fill(journal_reference)
            if acm_class:
                page.locator('input[name="acm_class"]').fill(acm_class)
            if msc_class:
                page.locator('input[name="msc_class"]').fill(msc_class)
            if doi:
                page.locator('input[name="doi"]').fill(doi)
            page.get_by_role("button", name="Save and Continue").first.click()

            if crosslist_archives and crosslist_categories:
                for crosslist_archive, crosslist_category in zip(crosslist_archives.split(","), crosslist_categories.split(",")):
                    page.locator("#select_arch").select_option(crosslist_archive.strip())
                    page.locator("#select_sc").select_option(crosslist_category.strip())

            logger.info("Completed the recorded arXiv steps; leaving the browser open " "for review (cross-list categories, license, and the final " "submit are left to you)")

            # Leave the browser open so you can review/finish by hand.
            hold_open()


VENUE = ArxivVenue()
