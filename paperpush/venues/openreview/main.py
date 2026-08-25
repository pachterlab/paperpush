"""Shared OpenReview submission engine.

OpenReview (https://openreview.net) hosts the submission form for many
conferences. The wizards differ conference by conference -- each venue asks for
its own topics, declarations, and supplements -- but the parts that are
OpenReview rather than the conference are the same everywhere:

* the sign-in form behind the portal's "Login" link, and the fact that that link
  renders only while signed *out* (see :class:`OpenReviewVenue`),
* the author widget, which resolves every author against OpenReview's profile
  search rather than taking a typed name or email, and
* the ``<open_review_id> | <name> | <email_suffixes> | <reciprocal_reviewer>``
  author-line format the ``.sub`` files use to feed that search.

Those live here; each conference is a leaf module (``<slug>.py`` exposing a
module-level ``VENUE``) that subclasses :class:`OpenReviewVenue` and implements
only :meth:`~paperpush.venues.base.Venue.submit` for its own form.

Author resolution is the fiddly half. A ``.sub`` author line names a person, but
OpenReview needs a *profile*, and names are not unique there (``~Jane_Smith1``,
``~Jane_Smith2``, ...). So :meth:`OpenReviewVenue.add_profile` searches the
profile directory and picks a row by these rules:

* a line carrying an OpenReview ID is searched by that ID and the single result
  taken; no result is a fatal error,
* otherwise the name is searched, rows whose names do not match are dropped, and
  a single remaining hit is taken as-is -- with a warning when the profile's
  email suffixes do not overlap the ones the line provided,
* when several rows remain, the one with the greatest email-suffix overlap wins
  (ties broken by the order the suffixes were listed), with a warning either way,
* and ``required=False`` turns "no result" from an error into a warning-and-skip,
  for lists (e.g. self-declared conflicts) where a missing profile should not
  abort a run.
"""

from __future__ import annotations

import logging
import re

from ...database import get_venue
from ...validate import _truthy_bool, parse_authors
from ..base import Venue
from ..login import VenueLoginError

logger = logging.getLogger(__name__)

# JavaScript run against the OpenReview profile-search results: it turns each
# result row into ``{name, title, emails}``, where ``emails`` is the list of
# (masked) email addresses OpenReview shows for that profile. The email suffixes
# are what author lines are matched on (see :func:`choose_result`).
SEARCH_ROWS_JS = """
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

#: Selector of one row in OpenReview's profile-search results.
SEARCH_ROW_SELECTOR = "div[class*='searchResultRow']"


class OpenReviewLoginError(VenueLoginError):
    """Raised when an automatic OpenReview sign-in cannot be completed."""


def lines(value: str) -> list[str]:
    """Split a newline-delimited ``.sub`` value into its trimmed, non-empty lines."""
    return [line.strip() for line in value.splitlines() if line.strip()]


def _suffixes(value: str) -> list[str]:
    """Parse an author's ``email_suffixes`` column into ordered, lowercased suffixes.

    The column is comma-separated, in decreasing order of likelihood of
    association with the author; that order is preserved so a tie in overlap can
    be broken by it (see :func:`choose_result`).
    """
    return [s.strip().lower() for s in value.split(",") if s.strip()]


def _email_suffix(email: str) -> str:
    """The domain part (after ``@``) of one email address, lowercased."""
    email = email.strip().lower()
    return email.split("@", 1)[1] if "@" in email else ""


def _result_suffixes(result: dict) -> set[str]:
    """The set of email suffixes a profile-search result exposes."""
    return {s for s in (_email_suffix(e) for e in result.get("emails", [])) if s}


def parse_profiles(slug: str, value: str) -> list[dict]:
    """Parse an OpenReview author-list ``.sub`` value into normalized dicts.

    Each dict has ``open_review_id`` and ``name`` (trimmed strings), ``suffixes``
    (the ordered ``email_suffixes`` list) and ``reciprocal_reviewer`` (a bool).
    The column names come from the venue's own ``authorlist`` field, so a venue
    that renames or reorders them needs no change here.
    """
    author_field = next((f for f in get_venue(slug).fields if f.type == "authorlist"), None)
    parsed = parse_authors(value, author_field.fields if author_field else None)
    profiles: list[dict] = []
    for a in parsed:
        profiles.append(
            {
                "open_review_id": a.get("open_review_id", "").strip(),
                "name": a.get("name", "").strip(),
                "suffixes": _suffixes(a.get("email_suffixes", "")),
                "reciprocal_reviewer": _truthy_bool(a.get("reciprocal_reviewer", "")),
            }
        )
    return profiles


def choose_result(results: list[dict], suffixes: list[str]) -> int:
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


def warn(message: str) -> None:
    """Log and print a non-fatal warning about author resolution."""
    logger.warning(message)
    print(f"Warning: {message}")


class OpenReviewVenue(Venue):
    """Base class for a conference submitted through OpenReview.

    Supplies the sign-in form and the profile-search author widget; a leaf venue
    sets :attr:`~paperpush.venues.base.Venue.slug` and implements
    :meth:`~paperpush.venues.base.Venue.submit` for its own conference form.
    OpenReview shows a "Login" link only while signed *out*, so that link's
    presence is the signed-out marker (``logged_in_present_means_in`` is
    ``False``).
    """

    #: The "Login" link renders only for a signed-out session, so its presence
    #: means signed out (hence ``logged_in_present_means_in = False``).
    logged_in_role = "link"
    logged_in_names = ("Login",)
    logged_in_present_means_in = False

    def login(self, page, username: str, password: str, *, timeout_ms: int = 15000) -> None:
        """Fill and submit the OpenReview sign-in form from stored credentials.

        Opens the conference portal, follows its "Login" link, types the email and
        password, and submits. Raises :class:`OpenReviewLoginError` if the
        signed-in page does not load, so
        :meth:`~paperpush.venues.base.Venue.ensure_signed_in` can fall back to a
        manual sign-in rather than failing the whole run.
        """
        page.goto(self.login_url)
        page.get_by_role("link", name="Login").click()
        page.get_by_role("textbox", name="Email").fill(username)
        page.get_by_role("textbox", name="Password").fill(password)
        page.get_by_role("button", name="Login to OpenReview").click()
        if not self.is_logged_in(page, timeout_ms=timeout_ms):
            raise OpenReviewLoginError("submitted the credentials but OpenReview did not sign in -- the email " "or password may be wrong, or a step (CAPTCHA / two-factor) that can't " "be automated was added")

    def add_profile(self, page, author: dict, *, search_index: int = 0, required: bool = True) -> None:
        """Resolve one profile against OpenReview's search and add it.

        Searches by OpenReview ID when the line carries one, otherwise by name,
        applying the match rules in the module docstring. ``search_index`` selects
        which "search profiles" box to drive when a form has more than one (e.g.
        0 = author list, 1 = conflicts list). When ``required`` is false a search
        that returns nothing is warned about and skipped rather than raising --
        used for lists where a missing profile should not abort the run.
        """
        search_by_id = bool(author["open_review_id"])
        term = author["open_review_id"] if search_by_id else author["name"]
        label = author["open_review_id"] or author["name"]

        box = page.get_by_role("textbox", name="search profiles by name or").nth(search_index)
        box.click()
        box.fill(term)
        page.get_by_role("button", name="Search").nth(search_index).click()
        page.wait_for_timeout(1000)
        results = page.locator(SEARCH_ROW_SELECTOR).evaluate_all(SEARCH_ROWS_JS)
        if not search_by_id:
            results = [r for r in results if re.sub(r"\d+$", "", r["name"][1:]).strip().replace("_", " ") == author["name"].strip()]  # filter out rows whose names don't match the author name (ignoring OpenReview ID suffixes)

        if not results:
            if not required:
                warn(f"{label!r} was not found in the OpenReview profile search; skipping")
                return
            kind = "OpenReview ID" if search_by_id else "author"
            raise ValueError(f"no OpenReview profile found for {kind} {term!r}")

        if search_by_id:
            index = 0
        elif len(results) == 1:
            index = 0
            if author["suffixes"] and not (set(author["suffixes"]) & _result_suffixes(results[0])):
                warn(f"author {label!r}: the matched profile's email suffixes do not overlap the provided ones")
        else:
            warn(f"author {label!r}: OpenReview returned {len(results)} matching profiles; " "selecting the one with the greatest email-suffix overlap")
            index = choose_result(results, author["suffixes"])

        page.locator(SEARCH_ROW_SELECTOR).nth(index).get_by_role("button", name="plus").click()
