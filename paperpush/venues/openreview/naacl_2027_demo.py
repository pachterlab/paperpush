"""NAACL 2027 System Demonstrations track submission runner (Playwright).

The NAACL 2027 Demo Track is submitted through OpenReview, like ICLR 2027 and
AAAI 2027, so the profile-search author widget comes from
:mod:`paperpush.venues.openreview.main`; this module is the demo-track form. It is
a port of a ``paperpush codegen`` recording, with the recording's values replaced
by the parsed ``.sub`` field values and the author step turned into a loop.

Constraints carried from the venue over the raw recording:

* Sign-in goes straight to OpenReview's ``/login`` page. The group page shows a
  Cloudflare "Verifying your browser" check to a signed-out automated browser,
  which signing in skips, so the base class's "open the portal, click Login"
  never reaches the form. That interstitial has no "Login" link, only a "Sign in"
  one, so both names count as signed out (:attr:`NAACL2027DemoVenue.logged_in_names`).
* At least one author is required. Each author line is
  ``open_review_id | name | email_suffixes`` and every author must already have an
  OpenReview profile; the demo track has no reciprocal reviewing.
* The PDF is required, and so are the demo video and live demo links. Their
  format (http/https) is checked at validate time by ``require_url``, and their
  reachability by the ``integrity.links`` check. TL;DR and the ethics statement
  are optional and filled only when given.
* The two authorizations (sharing author emails with the Program Chairs, public
  release on acceptance) are ``.sub`` confirmations and must be ``yes``.
* The demo track offers exactly one license, ``CC BY 4.0``, so it is not a
  ``.sub`` field: the runner selects it (:data:`LICENSE`). It is on the form the
  run stops at for review.

The run stops before the final submit -- it leaves the browser open at the filled
form via :func:`~paperpush.venues.common.hold_open`.
"""

from __future__ import annotations

import logging
import re

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from ...validate import _truthy_bool
from ..common import DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts, hold_open, hold_open_on_failure, open_run_context
from ..recovery import guard, step
from ..recovery_channel import browser_launch_args
from .main import OpenReviewLoginError, OpenReviewVenue, parse_profiles

logger = logging.getLogger(__name__)

SLUG = "naacl_2027_demo"

#: OpenReview's sign-in page, sending the browser on to the demo-track group once
#: signed in. Loads without the Cloudflare check the group page shows.
LOGIN_URL = "https://openreview.net/login?redirect=%2Fgroup%3Fid%3Daclweb.org%2FNAACL%2F2027%2FDemo_Track"

#: Where the sign-in page sends the browser once the credentials are accepted.
SIGNED_IN_URL = re.compile(r"/group\?id=aclweb\.org")

#: The group-page button that opens the submission form.
OPEN_FORM_BUTTON = "NAACL 2027 Demo Track Submission"

#: The form's rows, as the recording addressed them: ``FORM_ROW.format(n)`` is the
#: n-th field row of the submission invitation.
FORM_ROW = "#invitation > div:nth-of-type(2) > div:nth-of-type({})"

#: Row number of each text field on the form (row 4 is the author widget, row 8
#: the PDF upload, rows 12-15 the two authorization radios).
TEXT_ROWS = {
    "title": 3,
    "keywords": 5,
    "abstract": 6,
    "tldr": 7,
    "demo_video_url": 9,
    "live_demo_url": 10,
    "ethics_statement": 11,
}

#: Row number of the license dropdown.
LICENSE_ROW = 16

#: The only license the demo track offers.
LICENSE = "CC BY 4.0"

#: Accessible name of the PDF upload button.
PDF_BUTTON = "Choose PDF"

#: The two authorization radios, keyed by their ``.sub`` field ids; each radio's
#: accessible name is the authorization sentence itself.
AUTHORIZATIONS = {
    "email_sharing": "We authorize the sharing of all author emails with Program Chairs.",
    "data_release": "We authorize the release of our submission and author names to the public in the event of acceptance.",
}


def _parse_authors(value: str) -> list[dict]:
    """Parse the demo-track ``authorlist`` ``.sub`` value into normalized author dicts."""
    return parse_profiles(SLUG, value)


def _text_input(page, field_id: str):
    """The input (or textarea) of one text field, by its row on the form."""
    return page.locator(FORM_ROW.format(TEXT_ROWS[field_id]) + " > div > div > div:nth-of-type(2)").locator("input, textarea").first


def _check_values(values: dict) -> None:
    """Refuse, before the browser opens, a ``.sub`` the form would reject."""
    if not _parse_authors(values.get("authors", "")):
        raise ValueError("no authors to add to the NAACL 2027 Demo Track form -- at least one is required")
    for field_id in ("title", "keywords", "abstract", "pdf_file", "demo_video_url", "live_demo_url"):
        if not values.get(field_id, "").strip():
            raise ValueError(f"the NAACL 2027 Demo Track form requires {field_id}")
    for field_id, sentence in AUTHORIZATIONS.items():
        if not _truthy_bool(values.get(field_id, "")):
            raise ValueError(f"the NAACL 2027 Demo Track form requires the authorization {sentence!r}; set {field_id} to yes")


class NAACL2027DemoVenue(OpenReviewVenue):
    """The NAACL 2027 System Demonstrations track, submitted through OpenReview."""

    slug = SLUG
    #: "Login" is OpenReview's signed-out header link; "Sign in" is the only link on
    #: the Cloudflare interstitial a signed-out automated browser gets instead.
    logged_in_names = ("Login", "Sign in")
    #: submit() guards its page, so a failed action can be recovered. OpenReview's
    #: final control is the generic "Submit".
    supports_recovery = True

    @property
    def login_url(self) -> str:
        """OpenReview's sign-in page, which skips the group page's Cloudflare check."""
        return LOGIN_URL

    def login(self, page, username: str, password: str, *, timeout_ms: int = 15000) -> None:
        """Sign in on OpenReview's ``/login`` page, which redirects to the demo-track group."""
        page.goto(LOGIN_URL)
        page.get_by_role("textbox", name="Email").fill(username)
        page.get_by_role("textbox", name="Password").fill(password)
        page.get_by_role("button", name="Login to OpenReview").click()
        try:
            page.wait_for_url(SIGNED_IN_URL, timeout=timeout_ms)
        except PWTimeout:
            raise OpenReviewLoginError("OpenReview stayed on the sign-in page -- the email or password may be wrong") from None
        if not self.is_logged_in(page, timeout_ms=timeout_ms):
            raise OpenReviewLoginError("submitted the credentials but OpenReview did not sign in -- the email " "or password may be wrong, or a step (CAPTCHA / two-factor) that can't " "be automated was added")

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
        """Open OpenReview, sign in, then fill the NAACL 2027 Demo Track form from a ``.sub``.

        Stops before the final submit and leaves the browser open for review.
        """
        if debug:
            headless = False  # the Inspector needs a visible browser

        _check_values(values)
        title = values.get("title", "").strip()
        authors = _parse_authors(values.get("authors", ""))
        pdf_file = values.get("pdf_file", "").strip()

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

            step("Open the submission form")
            page.get_by_role("button", name=OPEN_FORM_BUTTON).click()

            step("Title")
            _text_input(page, "title").fill(title)

            # Authors: remove the pre-filled entry (the submitter, added by
            # OpenReview), then add each author by profile search.
            step("Authors")
            page.get_by_role("button", name="remove", exact=True).click()
            for i, author in enumerate(authors):
                step(f"Authors: author {i + 1} of {len(authors)} ({author['open_review_id'] or author['name']})")
                self.add_profile(page, author)

            step("Keywords, abstract, and TL;DR")
            _text_input(page, "keywords").fill(values.get("keywords", "").strip())
            _text_input(page, "abstract").fill(values.get("abstract", "").strip())
            if values.get("tldr", "").strip():
                _text_input(page, "tldr").fill(values["tldr"].strip())

            step("PDF")

            def attach_pdf(page):
                with page.expect_file_chooser() as chooser:
                    page.get_by_role("button", name=PDF_BUTTON).click()
                chooser.value.set_files(pdf_file)

            page.attempt(f"attach {pdf_file}", attach_pdf)

            step("Demo video and live demo links")
            _text_input(page, "demo_video_url").fill(values.get("demo_video_url", "").strip())
            _text_input(page, "live_demo_url").fill(values.get("live_demo_url", "").strip())

            if values.get("ethics_statement", "").strip():
                step("Ethics statement")
                _text_input(page, "ethics_statement").fill(values["ethics_statement"].strip())

            step("Authorizations")
            for sentence in AUTHORIZATIONS.values():
                page.get_by_label(sentence, exact=True).check()

            step("License")
            page.locator(FORM_ROW.format(LICENSE_ROW)).locator(".dropdown-select__control, svg").first.click()
            page.get_by_role("option", name=LICENSE).click()

            logger.info("Filled the NAACL 2027 Demo Track submission form for %r", title)

            # Leave the browser open at the filled form; never click the final submit.
            hold_open()


VENUE = NAACL2027DemoVenue()
