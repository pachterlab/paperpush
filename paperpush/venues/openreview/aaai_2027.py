"""AAAI 2027 submission runner (Playwright).

AAAI 2027 is submitted through OpenReview (https://openreview.net). This module
is a faithful port of a ``playwright codegen`` recording of the AAAI 2027
submission wizard, reshaped to the per-venue runner layout: a single
:class:`AAAI2027Venue` exposing :meth:`~AAAI2027Venue.login` and
:meth:`~AAAI2027Venue.run`, with the recording's hard-coded values replaced by
the parsed ``.sub`` field values and its repeated author / topic / reviewer /
conflict steps turned into loops.

Constraints carried from the venue over the raw recording:

* At least one author must be added to the author page, or the run aborts. Each
  author line is ``open_review_id | name | email_suffixes | reciprocal_reviewer``.
  Authors are resolved against OpenReview's profile search: if the line carries an
  OpenReview ID that ID is searched directly (no result is a fatal error);
  otherwise the name is searched (no result is fatal), a single hit is taken as-is
  (warning if the profile's email suffixes don't overlap the provided ones), and
  when several hits come back the one with the greatest email-suffix overlap is
  chosen -- ties broken by the order the suffixes were listed -- with a warning
  raised regardless. See :func:`_choose_result`.
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
import re

from ...database import get_venue
from ..base import Venue
from ..common import DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts, hold_open, open_run_context
from ..login import VenueLoginError

logger = logging.getLogger(__name__)

# JavaScript run against the OpenReview profile-search results: it turns each
# result row into ``{name, title, emails}``, where ``emails`` is the list of
# (masked) email addresses OpenReview shows for that profile. The email suffixes
# are what author lines are matched on (see :func:`_choose_result`).
_SEARCH_ROWS_JS = """
rows => rows.map(row => {
    const basic = row.querySelector("div[class*='basicInfo']");
    const lines = basic.innerText.split("\\n").map(x => x.trim()).filter(Boolean);

    return {
        name: lines[0],
        title: lines.slice(1).join(" "),
        emails: [...row.querySelectorAll("div[class*='authorEmails'] span")]
            .map(e => e.innerText.trim())
    };
})
"""


class AAAI2027LoginError(VenueLoginError):
    """Raised when an automatic AAAI 2027 (OpenReview) sign-in cannot be completed."""


def _lines(value: str) -> list[str]:
    """Split a newline-delimited ``.sub`` value into its trimmed, non-empty lines."""
    return [line.strip() for line in value.splitlines() if line.strip()]


def _suffixes(value: str) -> list[str]:
    """Parse an author's ``email_suffixes`` column into ordered, lowercased suffixes.

    The column is comma-separated, in decreasing order of likelihood of
    association with the author; that order is preserved so a tie in overlap can
    be broken by it (see :func:`_choose_result`).
    """
    return [s.strip().lower() for s in value.split(",") if s.strip()]


def _email_suffix(email: str) -> str:
    """The domain part (after ``@``) of one email address, lowercased."""
    email = email.strip().lower()
    return email.split("@", 1)[1] if "@" in email else ""


def _result_suffixes(result: dict) -> set[str]:
    """The set of email suffixes a profile-search result exposes."""
    return {s for s in (_email_suffix(e) for e in result.get("emails", [])) if s}


def _parse_authors(value: str) -> list[dict]:
    """Parse the AAAI ``authorlist`` ``.sub`` value into normalized author dicts.

    Each dict has ``open_review_id`` and ``name`` (trimmed strings), ``suffixes``
    (the ordered ``email_suffixes`` list) and ``reciprocal_reviewer`` (a bool).
    """
    from ...validate import _truthy_bool, parse_authors

    author_field = next((f for f in get_venue("aaai_2027").fields if f.type == "authorlist"), None)
    parsed = parse_authors(value, author_field.fields if author_field else None)
    authors: list[dict] = []
    for a in parsed:
        authors.append(
            {
                "open_review_id": a.get("open_review_id", "").strip(),
                "name": a.get("name", "").strip(),
                "suffixes": _suffixes(a.get("email_suffixes", "")),
                "reciprocal_reviewer": _truthy_bool(a.get("reciprocal_reviewer", "")),
            }
        )
    return authors


def _choose_result(results: list[dict], suffixes: list[str]) -> int:
    """Pick the best-matching profile-search result index for an author.

    ``results`` is the list of ``{name, title, emails}`` rows (in DOM order) and
    ``suffixes`` is the author's ordered ``email_suffixes``. The result with the
    greatest email-suffix overlap wins; ties are broken by the order the suffixes
    were listed (a match on an earlier, more-likely suffix beats a later one) and
    then by DOM order.
    """
    provided = set(suffixes)

    def rank(item: tuple[int, dict]) -> tuple[int, int, int]:
        index, result = item
        theirs = _result_suffixes(result)
        overlap = len(provided & theirs)
        order = next((i for i, s in enumerate(suffixes) if s in theirs), len(suffixes))
        return (-overlap, order, index)

    return min(enumerate(results), key=rank)[0]


def _warn(message: str) -> None:
    """Log and print a non-fatal warning about author resolution."""
    logger.warning(message)
    print(f"Warning: {message}")


def _validate_secondary_topics(value: str) -> str:
    """Validator: each secondary topic is from the AAAI list and there are at most 5.

    The per-line count is also bounded by ``max_count`` in ``venues.json``; this
    additionally enforces that each named topic is a member of the controlled
    vocabulary (the field is a free ``textarea`` because many topic names contain
    commas, so it cannot use the comma-separated ``multichoice`` closed-set check).
    """
    field = next(f for f in get_venue("aaai_2027").fields if f.id == "secondary_topics")
    allowed = set(field.options or [])
    topics = _lines(value)
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


class AAAI2027Venue(Venue):
    """The AAAI 2027 venue, submitted through OpenReview.

    The base class supplies the sign-in orchestration, session capture, and the
    login-state check. OpenReview shows a "Login" link only while signed *out*, so
    that link's presence is the signed-out marker (``logged_in_present_means_in``
    is ``False``).
    """

    slug = "aaai_2027"
    field_validators = FIELD_VALIDATORS

    #: The "Login" link renders only for a signed-out session, so its presence
    #: means signed out (hence ``logged_in_present_means_in = False``).
    logged_in_role = "link"
    logged_in_names = ("Login",)
    logged_in_present_means_in = False

    def login(self, page, username: str, password: str, *, timeout_ms: int = 15000) -> None:
        """Fill and submit the OpenReview sign-in form from stored credentials.

        Opens the AAAI 2027 portal, follows its "Login" link, types the email and
        password, and submits. Raises :class:`AAAI2027LoginError` if the signed-in
        page does not load, so :meth:`ensure_signed_in` can fall back to a manual
        sign-in rather than failing the whole run.
        """
        page.goto(self.login_url)
        page.get_by_role("link", name="Login").click()
        page.get_by_role("textbox", name="Email").fill(username)
        page.get_by_role("textbox", name="Password").fill(password)
        page.get_by_role("button", name="Login to OpenReview").click()
        if not self.is_logged_in(page, timeout_ms=timeout_ms):
            raise AAAI2027LoginError("submitted the credentials but OpenReview did not sign in -- the email " "or password may be wrong, or a step (CAPTCHA / two-factor) that can't " "be automated was added")

    def _add_author(self, page, author: dict, *, search_index: int = 0, required: bool = True) -> None:
        """Resolve one profile against OpenReview's search and add it.

        Searches by OpenReview ID when the line carries one, otherwise by name,
        applying the match rules in the module docstring. ``search_index`` selects
        which "search profiles" box to drive (0 = author list, 1 = conflicts list).
        When ``required`` is false a search that returns nothing is warned about and
        skipped rather than raising -- used for conflicts, which are dropped rather
        than aborting the run when not found.
        """
        search_by_id = bool(author["open_review_id"])
        term = author["open_review_id"] if search_by_id else author["name"]
        label = author["open_review_id"] or author["name"]

        box = page.get_by_role("textbox", name="search profiles by name or").nth(search_index)
        box.click()
        box.fill(term)
        page.get_by_role("button", name="Search").nth(search_index).click()
        page.wait_for_timeout(1000)
        results = page.locator("div[class*='searchResultRow']").evaluate_all(_SEARCH_ROWS_JS)
        if not search_by_id:
            results = [r for r in results if re.sub(r"\d+$", "", r["name"][1:]).strip().replace("_", " ") == author["name"].strip()]  # filter out rows whose names don't match the author name (ignoring OpenReview ID suffixes)

        if not results:
            if not required:
                _warn(f"{label!r} was not found in the OpenReview profile search; skipping")
                return
            kind = "OpenReview ID" if search_by_id else "author"
            raise ValueError(f"no OpenReview profile found for {kind} {term!r}")

        if search_by_id:
            index = 0
        elif len(results) == 1:
            index = 0
            if author["suffixes"] and not (set(author["suffixes"]) & _result_suffixes(results[0])):
                _warn(f"author {label!r}: the matched profile's email suffixes do not overlap the provided ones")
        else:
            _warn(f"author {label!r}: OpenReview returned {len(results)} matching profiles; " "selecting the one with the greatest email-suffix overlap")
            index = _choose_result(results, author["suffixes"])

        page.locator("div[class*='searchResultRow']").nth(index).get_by_role("button", name="plus").click()

    def run(
        self,
        values: dict,
        *,
        headless: bool = False,
        debug: bool = False,
        new_session: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Open OpenReview, sign in, then drive the AAAI 2027 wizard from a ``.sub``.

        The wizard structure is replayed from a recording; the hard-coded values
        are read from ``values`` and the repeated steps (authors, secondary topics,
        conflicts) are looped. The run stops before the final submit and leaves the
        browser open for review.
        """
        from playwright.sync_api import sync_playwright

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
        secondary_topics = _lines(values.get("secondary_topics", ""))
        countries = [c.strip() for c in values.get("country", "").split(",") if c.strip()]
        pdf_file = values.get("pdf_file", "").strip()
        reproducibility_checklist = values.get("reproducibility_checklist", "").strip()
        technical_supplement = values.get("technical_supplement", "").strip()
        media_supplement = values.get("media_supplement", "").strip()
        code_data_supplement = values.get("code_data_supplement", "").strip()
        conflicts = _parse_authors(values.get("conflicts", ""))

        with sync_playwright() as playwright:
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
            # OpenReview profile search (by ID or name, see _add_author).
            page.get_by_role("button", name="remove").click()
            for author in authors:
                self._add_author(page, author)

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
                self._add_author(page, conflict, search_index=1, required=False)

            # Consent, license, and signatures (replayed from the recording).
            page.get_by_role("checkbox", name="I confirm that all authors").check()

            logger.info("Filled the AAAI 2027 submission form for %r", title)

            # Leave the browser open at the filled form; never click the final submit.
            hold_open()


VENUE = AAAI2027Venue()
