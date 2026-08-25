"""ICLR 2027 submission runner (Playwright).

ICLR 2027 is submitted through OpenReview (https://openreview.net), like AAAI
2027, so signing in and the profile-search author widget come from
:mod:`paperpush.venues.openreview.main`; this module is the ICLR form itself.
It is a port of a ``playwright codegen`` recording of the ICLR 2027 submission
wizard, with the recording's hard-coded values replaced by the parsed ``.sub``
field values and its repeated author / reviewer / AI-disclosure steps turned into
loops.

Constraints carried from the venue over the raw recording:

* At least one author must be added to the author page, or the run aborts. Each
  author line is ``open_review_id | name | email_suffixes | reciprocal_reviewer``
  and every author must already have an OpenReview profile.
* The reciprocal reviewing author(s) are drawn from the submission's own authors:
  those whose ``reciprocal_reviewer`` column is set. The form's dropdown lists
  authors by their profile *name*, so a nominated author must carry a Name column
  even when the line also gives an OpenReview ID.
* The reciprocal reviewing exemption and the nomination are two halves of one
  answer, so they are cross-checked before the browser opens (see
  :func:`_check_reciprocal_reviewing`): ``We do not need an exemption.`` exactly
  when an author is nominated, and the free-text exemption *reason* filled in
  exactly when the exemption is the "different reason, specified below" option.
* AI assistance is one or more answers from the ICLR list, one per line, with
  ``No, not at all.`` and ``Yes, but for none of the above purposes.`` each
  mutually exclusive with every other answer (see :func:`_validate_ai_assistance`).
* The PDF (50 MB) and the single zipped supplementary-material file (100 MB) are
  both optional -- the PDF only until the full-paper deadline -- and each is
  uploaded only when the ``.sub`` names a file. Their size and extension limits
  are enforced at validate time by the field metadata.
* Of the three required declaration checkboxes, only the Code of Ethics is asked
  for in the ``.sub``. The other two acknowledge how ICLR works rather than
  asking anything of the author, and the form cannot be submitted without them,
  so the runner ticks them itself (see :data:`AUTOMATIC_DECLARATIONS`). ICLR 2027
  offers exactly one license, ``CC BY 4.0``.
* The PDF's main text is capped at 9 pages, checked at validate time: the venue
  field lists the headings ICLR excludes from that count (references, the
  reproducibility / ethics / AI-use statements, acknowledgements, appendix) as
  ``main_text_end_headings``, and page counting stops at the first of them.

The run stops before the final submit -- it leaves the browser open at the filled
form via :func:`~paperpush.venues.common.hold_open`.
"""

from __future__ import annotations

import logging
import re

from playwright.sync_api import sync_playwright

from ...database import get_venue
from ...validate import _truthy_bool
from ..common import DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts, hold_open, hold_open_on_failure, open_run_context
from .main import OpenReviewVenue, lines, parse_profiles

logger = logging.getLogger(__name__)

SLUG = "iclr_2027"

#: The exemption answer that means "no exemption": the one the form expects when
#: an author has been nominated as the reciprocal reviewer.
NO_EXEMPTION = "We do not need an exemption."

#: The exemption answer that hands the reason over to the free-text field.
OTHER_EXEMPTION = "I believe the submission is exempt for a different reason, specified below."

#: AI-assistance answers that mean "nothing else applies", so each must be the
#: only answer given.
EXCLUSIVE_AI_ANSWERS = (
    "No, not at all.",
    "Yes, but for none of the above purposes. Details are described in the paper.",
)

#: Accessible-name prefix of the Code of Ethics checkbox, the one declaration the
#: author makes for themselves in the ``.sub`` (field ``code_of_ethics``).
#: Prefixes rather than the full sentences throughout, because the names are long
#: and these are already unambiguous.
CODE_OF_ETHICS_DECLARATION = "I and all co-authors of this work have read, and commit to adhering to the ICLR"

#: Accessible-name prefixes of the declarations the runner ticks itself. These
#: acknowledge how ICLR works -- that submissions become public and cannot be
#: retracted, and that the submission instructions apply -- rather than asking
#: anything of the author, and the form cannot be submitted without them, so they
#: would be a ``.sub`` field with exactly one usable value. Nothing here is
#: hidden: both sentences are on the form the run stops at for review.
AUTOMATIC_DECLARATIONS = (
    "I and all co-authors of this work understand that all papers submitted to ICLR",  # paper visibility
    "I and all co-authors agree to",  # submission requirements
)


def _parse_authors(value: str) -> list[dict]:
    """Parse the ICLR ``authorlist`` ``.sub`` value into normalized author dicts."""
    return parse_profiles(SLUG, value)


def _options(field_id: str) -> list[str]:
    """The declared options of one ICLR field, from ``venues.json``."""
    field = next(f for f in get_venue(SLUG).fields if f.id == field_id)
    return list(field.options or [])


def _validate_ai_assistance(value: str) -> str:
    """Validator: the AI-assistance answers are from the list and are compatible.

    The field is a free ``textarea`` with one answer per line (the answers contain
    commas, so the comma-separated ``multichoice`` closed-set check cannot be
    used), which is why membership is enforced here. Two of the answers mean
    "nothing else applies" (:data:`EXCLUSIVE_AI_ANSWERS`), so either of those must
    stand alone.
    """
    allowed = set(_options("ai_assistance"))
    answers = lines(value)
    unknown = [a for a in answers if a not in allowed]
    if unknown:
        raise ValueError("AI assistance answers must be chosen from the ICLR 2027 list; " f"unknown: {unknown}")
    if len(answers) != len(set(answers)):
        raise ValueError("each AI assistance answer may be given only once")
    exclusive = [a for a in answers if a in EXCLUSIVE_AI_ANSWERS]
    if exclusive and len(answers) > 1:
        raise ValueError(f"{exclusive[0]!r} rules out every other AI assistance answer, " "so it must be the only one given")
    return value


def _validate_authors(value: str) -> str:
    """Validator: every nominated reciprocal reviewer carries a Name.

    The form's Reciprocal Reviewing Author dropdown lists authors by their
    OpenReview profile name, so a line marked ``yes`` cannot be selected from an
    OpenReview ID alone. (How many may be nominated is up to the author -- the
    dropdown accepts more than one.)
    """
    nameless = [a for a in _parse_authors(value) if a["reciprocal_reviewer"] and not a["name"]]
    if nameless:
        ids = ", ".join(a["open_review_id"] or "<unnamed>" for a in nameless)
        raise ValueError("an author marked as the reciprocal reviewer must also give a Name -- " f"the form's dropdown lists authors by name (missing for: {ids})")
    return value


def _check_reciprocal_reviewing(reciprocal: list[dict], exemption: str, reason: str) -> None:
    """Cross-check the nomination, the exemption, and the free-text reason.

    The three fields are one answer split across the form, and only the runner
    sees all of them at once (a ``.sub`` validator sees a single field's value),
    so this runs before the browser opens rather than at validate time. ICLR asks
    for an exemption exactly when no author is nominated, and for a written reason
    exactly when the exemption chosen is :data:`OTHER_EXEMPTION`.
    """
    if reciprocal and exemption != NO_EXEMPTION:
        names = ", ".join(a["name"] for a in reciprocal)
        raise ValueError(f"{names} is marked as the reciprocal reviewer, so the reciprocal reviewing " f"exemption must be {NO_EXEMPTION!r} (got {exemption!r})")
    if not reciprocal and exemption == NO_EXEMPTION:
        raise ValueError("no author is marked as the reciprocal reviewer, so an exemption is needed -- " "choose one of the other reciprocal reviewing exemption reasons")
    if reason and exemption != OTHER_EXEMPTION:
        raise ValueError(f"a reciprocal reviewing exemption reason was given, so the exemption must be " f"{OTHER_EXEMPTION!r} (got {exemption!r})")
    if exemption == OTHER_EXEMPTION and not reason:
        raise ValueError(f"the exemption {OTHER_EXEMPTION!r} needs a reciprocal reviewing exemption reason")


#: Custom field validators layered onto the generic schema (see
#: :func:`paperpush.venues.get_field_validators`).
FIELD_VALIDATORS = {
    "ai_assistance": _validate_ai_assistance,
    "authors": _validate_authors,
}


class ICLR2027Venue(OpenReviewVenue):
    """The ICLR 2027 venue, submitted through OpenReview.

    The OpenReview base class supplies the sign-in form, the profile-search author
    widget, and the login-state check; only :meth:`submit` is ICLR-specific.
    """

    slug = SLUG
    field_validators = FIELD_VALIDATORS

    def _open_dropdown(self, page, placeholder: str) -> None:
        """Open the single-select dropdown whose placeholder reads ``placeholder``.

        OpenReview's dropdowns are react-select widgets that all share the
        ``dropdown-select`` class, so they are told apart by the placeholder text
        they show while unset ("Select Primary Area", "Select License...", ...)
        rather than by position on the page.
        """
        page.locator(".dropdown-select").filter(has_text=placeholder).locator(".dropdown-select__input-container").first.click()

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
        """Open OpenReview, sign in, then drive the ICLR 2027 wizard from a ``.sub``.

        The wizard structure is replayed from a recording; the hard-coded values
        are read from ``values`` and the repeated steps (authors, reciprocal
        reviewers, AI-assistance answers) are looped. The run stops before the
        final submit and leaves the browser open for review.
        """
        if debug:
            headless = False  # the Inspector needs a visible browser

        title = values.get("title", "")
        authors = _parse_authors(values.get("authors", ""))
        if not authors:
            raise ValueError("no authors to add to the ICLR 2027 author page -- at least one is required")
        # The reciprocal reviewing author(s) come from the authors themselves:
        # those whose reciprocal_reviewer column is set.
        reciprocal = [a for a in authors if a["reciprocal_reviewer"]]
        _validate_authors(values.get("authors", ""))
        keywords = values.get("keywords", "").strip()
        tldr = values.get("tldr", "").strip()
        abstract = values.get("abstract", "")
        pdf_file = values.get("pdf_file", "").strip()
        supplementary_material = values.get("supplementary_material", "").strip()
        primary_area = values.get("primary_area", "").strip()
        exemption = values.get("reciprocal_reviewing_exemption", "").strip()
        exemption_reason = values.get("reciprocal_reviewing_exemption_reason", "").strip()
        ai_assistance = lines(values.get("ai_assistance", ""))
        license_name = values.get("license", "").strip()

        _check_reciprocal_reviewing(reciprocal, exemption, exemption_reason)
        if not ai_assistance:
            raise ValueError("the ICLR 2027 AI assistance disclosure is required -- give at least one answer")
        _validate_ai_assistance(values.get("ai_assistance", ""))
        if not _truthy_bool(values.get("code_of_ethics", "")):
            raise ValueError("ICLR 2027 requires every author to have read and committed to the ICLR Code " "of Ethics; set code_of_ethics to yes")

        with sync_playwright() as playwright, hold_open_on_failure(headless=headless, keep_open=keep_open_on_failure):
            browser = playwright.chromium.launch(headless=headless)
            context = open_run_context(browser, self.session_path(), new_session=new_session)
            apply_default_timeouts(context, timeout)
            page = context.new_page()

            self.ensure_signed_in(page, context, debug=debug)

            if debug:
                page.pause()

            # Navigate from the signed-in group page into the submission form.
            page.get_by_role("button", name="ICLR 2027 Conference").click()

            # Title. The form's text inputs are addressed by position, as recorded:
            # 0 is OpenReview's site-wide search box, 1 the title, 2 the author
            # profile search, 3 the keywords, 4 the TL;DR.
            page.get_by_role("textbox").nth(1).fill(title)

            # Authors: remove the pre-filled author entry (the submitter, added by
            # OpenReview), then add each author by profile search (by ID or name,
            # see add_profile).
            page.get_by_role("button", name="remove", exact=True).click()
            for author in authors:
                self.add_profile(page, author)

            # Keywords (comma-separated, as typed).
            page.get_by_role("textbox").nth(3).fill(keywords)

            # TL;DR (optional).
            if tldr:
                page.get_by_role("textbox").nth(4).fill(tldr)

            # Abstract -- the only textarea on the form.
            page.locator("textarea").fill(abstract)

            # PDF (optional until the full-paper deadline) and the single zipped
            # supplementary-material file (optional): set the file inputs directly
            # rather than clicking their "Choose ..." buttons, which would open the
            # OS file chooser.
            if pdf_file:
                page.get_by_label("pdf", exact=True).set_input_files(pdf_file)
            if supplementary_material:
                page.get_by_label("supplementary_material", exact=True).set_input_files(supplementary_material)

            # Primary area (exactly one).
            self._open_dropdown(page, "Select Primary Area")
            page.get_by_role("option", name=primary_area).click()

            # The three required declarations: the Code of Ethics, confirmed by
            # the .sub, plus the two policy acknowledgements the runner ticks
            # itself (see AUTOMATIC_DECLARATIONS).
            for name in (CODE_OF_ETHICS_DECLARATION, *AUTOMATIC_DECLARATIONS):
                page.get_by_role("checkbox", name=name).check()

            # Reciprocal reviewing author(s): the only multi-select on the form,
            # listing this submission's authors by profile name.
            for author in reciprocal:
                page.locator(".dropdown-select__value-container.dropdown-select__value-container--is-multi > .dropdown-select__input-container").first.click()
                page.get_by_role("option", name=author["name"], exact=True).click()

            # Reciprocal reviewing exemption, and its free-text reason when the
            # "different reason" option was chosen. _check_reciprocal_reviewing
            # has already established that the two agree with each other and with
            # the nomination above.
            self._open_dropdown(page, "Select Reciprocal Reviewing Exemption")
            page.get_by_role("option", name=exemption).click()
            if exemption_reason:
                # Among the still-empty text inputs this is the sixth: the site
                # search, title, profile search, keywords, and TL;DR precede it,
                # and the abstract -- filled above -- drops out of the filter.
                page.get_by_role("textbox").filter(has_text=re.compile(r"^$")).nth(5).fill(exemption_reason)

            # AI assistance disclosure (one or more checkboxes).
            for answer in ai_assistance:
                page.get_by_role("checkbox", name=answer).check()

            # License (ICLR 2027 offers only CC BY 4.0).
            placeholder = page.get_by_text("Select License...", exact=True)
            placeholder.locator("xpath=ancestor::div[contains(@class, 'dropdown-select__control')]").click()
            page.get_by_role("option", name=license_name).click()

            logger.info("Filled the ICLR 2027 submission form for %r", title)

            # Leave the browser open at the filled form; never click the final submit.
            hold_open()


VENUE = ICLR2027Venue()
