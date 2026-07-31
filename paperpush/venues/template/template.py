from __future__ import annotations

import logging

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from ...database import get_venue
from ...validate import parse_authors
from ..base import Venue
from ..common import (DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts,
                      hold_open, hold_open_on_failure, open_run_context, _try)
from ..login import VenueLoginError

logger = logging.getLogger(__name__)


class TemplateVenue(Venue):  #!!! change to the name of the venue
    slug = "template"  #!!! change to the slug name of the venue
    logged_in_names = ("NAME1", "NAME2", ...)  #!!! change to the names of buttons/links that appear when signed in

    # * login
    def login(self, page, username: str, password: str, *, timeout_ms: int = 15000) -> None:
        """
        Fill and submit the template sign-in form from stored credentials. Falls back to manual sign-in if the automatic attempt fails.
        """
        #!!! write login script here

        # A successful sign-in lands on the user dashboard, where the "START NEW SUBMISSION" link renders. If it never appears, the sign-in did not take (navigate to /user once in case the landing page differs).
        if not self.is_logged_in(page, timeout_ms=timeout_ms):
            user_url = getattr(self, "USER_URL", None)
            if not user_url:
                raise VenueLoginError("submitted the credentials but the signed-in template dashboard did " "not load -- the username or password may be wrong, or template " "added a step (CAPTCHA / two-factor) that can't be automated")
            page.goto(user_url)
            if not self.is_logged_in(page, timeout_ms=timeout_ms):
                raise VenueLoginError("submitted the credentials but the signed-in template dashboard did " "not load -- the username or password may be wrong, or template " "added a step (CAPTCHA / two-factor) that can't be automated")

    # * submit
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
        Open template, sign in, then drive the submission wizard from a ``.sub`` file.
        """
        # The Inspector needs a visible browser; debugging headless makes no sense.
        if debug:
            headless = False

        #!!! define variables from values dict here

        with sync_playwright() as playwright, hold_open_on_failure(headless=headless, keep_open=keep_open_on_failure):
            browser = playwright.chromium.launch(headless=headless)

            context = open_run_context(browser, self.session_path(), new_session=new_session)
            apply_default_timeouts(context, timeout)
            page = context.new_page()

            self.ensure_signed_in(page, context, debug=debug)

            if debug:
                page.pause()

            #!!! write the submission script here

            logger.info("Completed the recorded steps; leaving the browser open " "for review (cross-list categories, license, and the final " "submit are left to you)")

            # Leave the browser open so you can review/finish by hand.
            hold_open()


VENUE = TemplateVenue()  #!!! change to the name of the venue class
