"""AAAI 2027 submission runner (Playwright).

AAAI 2027 is submitted through OpenReview (https://openreview.net). This module
is a faithful port of a ``playwright codegen`` recording of the AAAI 2027
submission wizard, reshaped to the per-venue runner layout: a single
:class:`AAAI2027Venue` exposing :meth:`~AAAI2027Venue.submit`, with the
recording's hard-coded values replaced by the parsed ``.sub`` field values and
its repeated author / topic / reviewer / conflict steps turned into loops.
Signing in and the OpenReview profile-search author widget are not AAAI-specific
and come from :mod:`paperpush.venues.openreview.main`.

Constraints carried from the venue over the raw recording:

* At least one author must be added to the author page, or the run aborts. Each
  author line is ``open_review_id | name | email_suffixes | reciprocal_reviewer``
  and is resolved against OpenReview's profile search (see
  :meth:`~paperpush.venues.openreview.main.OpenReviewVenue.add_profile`).
* Exactly one primary topic and up to five secondary topics, each from the AAAI
  2027 topic list (``_assets/aaai_2027_topics.txt``); one or more countries of
  institutions from ``_assets/aaai_2027_countries.txt``, each selected in turn.
  The topic/country closed sets are enforced at validate time by the field
  ``options``.
* The PDF, reproducibility checklist, technical supplement, media supplement, and
  code/data supplement are all optional; each is uploaded only when the ``.sub``
  names a file.
* The reciprocal reviewer is drawn from the submission's own authors: the single
  author (0 or 1) whose ``reciprocal_reviewer`` column is set. If one is marked
  the "we have nominated" declaration is checked, otherwise the "no author
  qualifies" declaration is. Self-declared conflicts of interest use the same line
  format and profile-search logic as authors (the ``reciprocal_reviewer`` column
  is left blank), must *not* be authors, and a conflict the profile search does
  not return is warned about and skipped rather than aborting the run.

The run stops before the final submit -- it leaves the browser open at the filled
form via :func:`~paperpush.venues.common.hold_open`.
"""

from __future__ import annotations

import logging

from playwright.sync_api import sync_playwright

from ...database import get_venue
from ..common import DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts, hold_open, hold_open_on_failure, open_run_context
from .main import OpenReviewVenue, lines, parse_profiles

logger = logging.getLogger(__name__)

SLUG = "aaai_2027"


def _parse_authors(value: str) -> list[dict]:
    """Parse the AAAI ``authorlist`` ``.sub`` value into normalized author dicts."""
    return parse_profiles(SLUG, value)


def _validate_secondary_topics(value: str) -> str:
    """Validator: each secondary topic is from the AAAI list and there are at most 5.

    The per-line count is also bounded by ``max_count`` in ``venues.json``; this
    additionally enforces that each named topic is a member of the controlled
    vocabulary (the field is a free ``textarea`` because many topic names contain
    commas, so it cannot use the comma-separated ``multichoice`` closed-set check).
    """
    field = next(f for f in get_venue(SLUG).fields if f.id == "secondary_topics")
    allowed = set(field.options or [])
    topics = lines(value)
    if len(topics) > 5:
        raise ValueError(f"at most 5 secondary topics are allowed (got {len(topics)})")
    unknown = [t for t in topics if t not in allowed]
    if unknown:
        raise ValueError("secondary topics must be chosen from the AAAI 2027 topic list; " f"unknown: {unknown}")
    return value


def _validate_authors(value: str) -> str:
    """Validator: at most one author is marked as the reciprocal reviewer.

    The reciprocal reviewer is drawn from the authors themselves rather than a
    separate list, so it must be 0 or 1 of them (name-or-ID presence is checked by
    the generic author-list validator). See :func:`_parse_authors`.
    """
    reciprocal = [a for a in _parse_authors(value) if a["reciprocal_reviewer"]]
    if len(reciprocal) > 1:
        names = ", ".join(a["name"] or a["open_review_id"] for a in reciprocal)
        raise ValueError(f"at most one author may be marked as the reciprocal reviewer (got {len(reciprocal)}: {names})")
    return value


#: Custom field validators layered onto the generic schema (see
#: :func:`paperpush.venues.get_field_validators`).
FIELD_VALIDATORS = {
    "secondary_topics": _validate_secondary_topics,
    "authors": _validate_authors,
}


class AAAI2027Venue(OpenReviewVenue):
    """The AAAI 2027 venue, submitted through OpenReview.

    The OpenReview base class supplies the sign-in form, the profile-search author
    widget, and the login-state check; only :meth:`submit` is AAAI-specific.
    """

    slug = SLUG
    field_validators = FIELD_VALIDATORS

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
        """Open OpenReview, sign in, then drive the AAAI 2027 wizard from a ``.sub``.

        The wizard structure is replayed from a recording; the hard-coded values
        are read from ``values`` and the repeated steps (authors, secondary topics,
        conflicts) are looped. The run stops before the final submit and leaves the
        browser open for review.
        """
        if debug:
            headless = False  # the Inspector needs a visible browser

        title = values.get("title", "")
        authors = _parse_authors(values.get("authors", ""))
        if not authors:
            raise ValueError("no authors to add to the AAAI 2027 author page -- at least one is required")
        author_names = {a["name"].lower() for a in authors if a["name"]}
        # The reciprocal reviewer comes from the authors themselves: the single
        # author (0 or 1) whose reciprocal_reviewer column is set.
        reciprocal = [a for a in authors if a["reciprocal_reviewer"]]
        if len(reciprocal) > 1:
            names = ", ".join(a["name"] or a["open_review_id"] for a in reciprocal)
            raise ValueError(f"at most one author may be marked as the reciprocal reviewer (got {len(reciprocal)}: {names})")
        tldr = values.get("tldr", "").strip()
        abstract = values.get("abstract", "")
        primary_topic = values.get("primary_topic", "").strip()
        secondary_topics = lines(values.get("secondary_topics", ""))
        countries = [c.strip() for c in values.get("country", "").split(",") if c.strip()]
        pdf_file = values.get("pdf_file", "").strip()
        reproducibility_checklist = values.get("reproducibility_checklist", "").strip()
        technical_supplement = values.get("technical_supplement", "").strip()
        media_supplement = values.get("media_supplement", "").strip()
        code_data_supplement = values.get("code_data_supplement", "").strip()
        conflicts = _parse_authors(values.get("conflicts", ""))

        with sync_playwright() as playwright, hold_open_on_failure(headless=headless, keep_open=keep_open_on_failure):
            browser = playwright.chromium.launch(headless=headless)
            context = open_run_context(browser, self.session_path(), new_session=new_session)
            apply_default_timeouts(context, timeout)
            page = context.new_page()

            self.ensure_signed_in(page, context, debug=debug)

            if debug:
                page.pause()

            # Navigate from the signed-in group page into the submission form.
            page.get_by_role("button", name="AAAI 2027 Conference").click()

            # Title
            page.get_by_role("textbox").nth(1).fill(title)

            # Authors: remove the pre-filled author entry, then add each author by
            # OpenReview profile search (by ID or name, see add_profile).
            page.get_by_role("button", name="remove").click()
            for author in authors:
                self.add_profile(page, author)

            # TL;DR (optional).
            if tldr:
                page.get_by_role("textbox").nth(3).click()
                page.get_by_role("textbox").nth(3).fill(tldr)

            # Abstract.
            page.locator("textarea").click()
            page.locator("textarea").fill(abstract)

            # Primary topic (exactly one).
            page.locator(".dropdown-select__input-container").first.click()
            page.get_by_role("option", name=primary_topic).click()

            # Secondary topics (0-5).
            for topic in secondary_topics:
                page.locator(".dropdown-select__value-container.dropdown-select__value-container--is-multi > .dropdown-select__input-container").first.click()
                page.get_by_role("option", name=topic).click()

            # Countries of institutions (one or more): select each in turn from
            # the dropdown, typing to filter the long country list each time.
            country_box = page.get_by_role("combobox", name="Select Country Of Institutions")
            for country in countries:
                country_box.click()
                country_box.fill(country)
                page.get_by_role("option", name=country, exact=True).click()

            # PDF (optional).
            if pdf_file:
                page.locator('input[type="file"]').nth(0).set_input_files(pdf_file)

            # Reproducibility checklist (optional).
            if reproducibility_checklist:
                page.locator('input[type="file"]').nth(1).set_input_files(reproducibility_checklist)

            # Technical supplement (optional).
            if technical_supplement:
                page.locator('input[type="file"]').nth(2).set_input_files(technical_supplement)

            # Media supplement (optional).
            if media_supplement:
                page.locator('input[type="file"]').nth(3).set_input_files(media_supplement)

            # Code and data supplement (optional).
            if code_data_supplement:
                page.locator('input[type="file"]').nth(4).set_input_files(code_data_supplement)
                page.get_by_role("button", name="Choose Code And Data").set_input_files(code_data_supplement)

            # Reciprocal reviewer (0 or 1, drawn from the authors): search the
            # author's OpenReview profile and add them.
            for author in reciprocal:
                term = author["open_review_id"] or author["name"]
                page.get_by_role("textbox", name="search profiles by name or").nth(1).click()
                page.get_by_role("textbox", name="search profiles by name or").nth(1).fill(term)
                page.get_by_role("button", name="Search").nth(1).click()
                page.get_by_role("button", name="plus").first.click()

            # Declare the reviewer-nomination stance based on whether one was nominated.
            if reciprocal:
                page.get_by_role("radio", name="We have nominated a qualified").check()
            else:
                page.get_by_role("radio", name="We declare that no author").check()

            # Self-declared conflicts of interest: same format and profile-search
            # logic as the author list (the reciprocal_reviewer column is unused),
            # but a conflict must NOT be one of the authors, and a profile the
            # search does not return is skipped rather than aborting the run.
            for conflict in conflicts:
                if conflict["name"] and conflict["name"].lower() in author_names:
                    raise ValueError(f"self-declared conflict {conflict['name']!r} must not be one of the submission's authors")
                self.add_profile(page, conflict, search_index=1, required=False)

            # Consent, license, and signatures (replayed from the recording).
            page.get_by_role("checkbox", name="I confirm that all authors").check()

            logger.info("Filled the AAAI 2027 submission form for %r", title)

            # Leave the browser open at the filled form; never click the final submit.
            hold_open()


VENUE = AAAI2027Venue()
