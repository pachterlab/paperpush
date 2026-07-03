"""ScholarOne Manuscripts platform engine (Playwright).

Shared sign-in and author/keyword/reviewer helpers for ScholarOne venues at
``mc.manuscriptcentral.com/<venue>``; each per-venue runner supplies its own wizard.
"""

from __future__ import annotations

import logging
import re

from ..base import Venue
from ..login import VenueLoginError, fill_login_form

logger = logging.getLogger(__name__)

# The " Author" dashboard link only renders for a signed-in session, so its
# presence is the login-state signal.
AUTHOR_LINK = " Author"

# Title prefixes offered in the manual author-entry form's "Prefix" drop-down; the
# .sub "prefix" column must be one of these. Kept in step with venues.json.
AUTHOR_PREFIXES = ["Dr.", "Miss", "Mr.", "Mrs.", "Ms.", "Prof.", "Mx", "Dr"]

# Sign-in form selectors (shared across ScholarOne deployments).
LOGIN_USERID = "#USERID"
LOGIN_PASSWORD = "#PASSWORD"  # CSS selector, not a credential  # nosec B105
LOGIN_BUTTON = "#logInButton"

# The "Add Reviewer" form's RECOMMENDED / OPPOSED radio pair; the .sub stance
# column maps (case-insensitively) onto one of these names.
REVIEWER_STANCES = {"recommended": "RECOMMENDED", "opposed": "OPPOSED"}

# Minimum recommended (suggested) reviewers; opposed do not count. Deployments
# needing a different minimum (e.g. NAR's 6) pass their own.
MIN_RECOMMENDED_REVIEWERS = 4


class ScholarOneLoginError(VenueLoginError):
    """Raised when an automatic ScholarOne sign-in cannot be completed."""


def _split_keywords(raw: str) -> list[str]:
    """Parse a comma-separated keyword value from the ``.sub`` into a clean list."""
    return [kw.strip() for kw in (raw or "").split(",") if kw.strip()]


def _yes(value: str) -> bool:
    """Interpret a ``.sub`` boolean value as True only when it reads "yes"."""
    return (value or "").strip().lower() == "yes"


def _check_yes_no(page, field_id: str, yes: bool) -> None:
    """Check the Yes/No radio of a custom-field group (Yes first, No second)."""
    radios = page.locator(f'input[name="CUSTOM_FIELD_RESPONSE_ID{field_id}"]')
    radios.nth(0 if yes else 1).check()


def _add_keywords(page, keywords: list[str], *, portal: bool, keywords_box_name="Keywords") -> None:
    """Add keywords on the ScholarOne keywords step.

    The step has two identically driven pickers: a "Portal Keywords" box and a
    free "Keywords" box. Each keyword is typed and added via the picker's "Add"
    button (so a term outside the vocabulary is still entered). ``portal`` selects
    the picker; ``keywords_box_name`` is the box label ("Keywords" for
    Bioinformatics, "Key Words" for NAR). ``exact=True`` keeps "Keywords" from
    also matching "Portal Keywords".
    """
    box_name = f"Portal {keywords_box_name}" if portal else keywords_box_name
    add_group = f"Add Portal {keywords_box_name}" if portal else f"Add {keywords_box_name}"
    for keyword in keywords:
        logger.debug("Adding %s keyword: %s", "portal" if portal else "free", keyword)
        textbox = page.get_by_role("textbox", name=box_name, exact=True)
        textbox.fill(keyword)
        page.get_by_label(add_group).get_by_role("button", name="Add").click()
        page.wait_for_timeout(500)  # settle before the next keyword


def _answer_sanctions(page, values: dict) -> None:
    """Answer the three "Sanctions" yes/no questions (named / employed / export).

    Each is a Yes/No radio pair, so the six radios run 0..5 and question *n* has
    its "Yes" at index ``2 * n``. "No" is the default, so only an explicit "yes"
    is clicked. Values are stored "yes"/"no" in the .sub (default "no").
    """
    answers = (
        values.get("sanctions_named", "no"),
        values.get("sanctions_employed", "no"),
        values.get("sanctions_export_controlled", "no"),
    )
    radios = page.get_by_role("radio", name="radiobutton")
    for index, answer in enumerate(answers):
        if answer.strip().lower() == "yes":
            logger.info("Sanctions question %d answered Yes", index + 1)
            radios.nth(2 * index).check()


def _split_figures(raw: str) -> list[str]:
    """Parse the ``figures`` filelist into paths (one per line, ``| label`` dropped)."""
    paths: list[str] = []
    for line in (raw or "").splitlines():
        path = line.split("|")[0].strip()
        if path:
            paths.append(path)
    return paths


def _split_name(full_name: str) -> tuple[str, str]:
    """Split a ``.sub`` name into (given, family) on the last whitespace token.

    "Mary Anne Smith" -> ("Mary Anne", "Smith"); a single token is a given name.
    """
    parts = full_name.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def _create_author(page, author: dict) -> None:
    """Fill the manual create-author form when the email lookup found no account.

    The form is already open (see :func:`_add_author`). Fills prefix, given/family
    name, institution, country/region, and city from the ``.sub`` columns.
    ``institution`` is an ExtJS type-ahead whose suggestion must be clicked to
    commit, so it is typed rather than filled. Saved with "Add Created Author".
    """
    prefix = author.get("prefix", "").strip()
    if prefix and prefix not in AUTHOR_PREFIXES:
        raise ValueError(f"Unknown author prefix {prefix!r}; expected one of {AUTHOR_PREFIXES}")

    logger.info("Email not registered; creating author %s by hand", author.get("name", ""))

    if prefix:
        page.get_by_label("Prefix:").select_option(label=prefix)

    given, family = _split_name(author.get("name", ""))
    first_box = page.get_by_role("textbox", name="First (Given) Name:")
    first_box.click()
    first_box.fill(given)
    last_box = page.get_by_role("textbox", name="Last (Family) Name:")
    last_box.click()
    last_box.fill(family)

    institution = author.get("institution", "").strip()
    if not institution:
        raise ValueError("author creation requires an institution; the .sub 'institution' column is empty")

    # ExtJS type-ahead: type it (fill() won't fire the events that open the
    # boundlist); the first suggestion is committed, so a near match is accepted.
    inst_box = page.get_by_role("textbox", name="Insert Institution")
    inst_box.click()
    inst_box.press_sequentially(institution, delay=100)
    try:
        page.locator("li.x-boundlist-item:visible").first.click(timeout=1000)  # select first from dropdown
    except:
        page.keyboard.press("Enter")  # commit whatever is typed if no dropdown appears
        page.get_by_role("button", name="OK").click(timeout=2000)

    country = author.get("country", "").strip()
    if not country:
        raise ValueError("author creation requires a country/region; the .sub 'country' column is empty")
    if country in ["USA", "United States of America", "US"]:
        country = "United States"
    page.get_by_label("Country/Region").select_option(label=country)

    city = author.get("city", "").strip()
    if not city:
        raise ValueError("author creation requires a city; the .sub 'city' column is empty")
    city_box = page.get_by_role("textbox", name="City:")
    city_box.click()
    city_box.fill(city)

    page.get_by_role("button", name="Add Created Author").click()


def _add_author(page, author: dict) -> None:
    """Add one author on the ScholarOne author step, by email lookup.

    The email is searched; "Add Author Link" then either adds a registered author
    straight to the list or opens the manual create-author form (told apart by
    whether the "First (Given) Name" box appears), which :func:`_create_author`
    fills. The corresponding-author role is assigned later by :func:`_add_authors`.
    """
    from playwright.sync_api import TimeoutError as PWTimeout

    email = author["email"].strip()
    logger.info("Adding author %s <%s>", author.get("name", ""), email)

    box = page.get_by_role("textbox", name="Find using Author's email")
    box.click()
    box.fill(email)
    page.get_by_role("button", name="Search").click()
    add_author = page.get_by_role("button", name="Add Author")

    page.wait_for_timeout(3000)
    if add_author.count() > 0:
        add_author.click()
    else:
        page.get_by_label("Add Author Link").click()
        page.wait_for_timeout(3000)

        first_box = page.get_by_role("textbox", name="First (Given) Name:")
        if first_box.is_visible():
            _create_author(page, author)


def _add_authors(page, authors: list[dict], *, venue_label: str = "scholarone") -> None:
    """Rebuild the manuscript's author list on the ScholarOne author step.

    The seeded submitting-account author is removed first (editing it in place is
    error prone), then every ``.sub`` author is added in order via
    :func:`_add_author`, with a short settle between adds. Finally the author
    marked ``corresponding`` is assigned that role through its row's Actions menu
    (rows are numbered 1..N in add order). ``venue_label`` shapes error messages.
    """
    if not authors:
        raise ValueError(f"{venue_label}: the .sub 'authors' field is empty; at least one author is required")

    # Authors are looked up by email, so a missing address is unrecoverable.
    missing = [author.get("name") or "(unnamed)" for author in authors if not author.get("email", "").strip()]
    if missing:
        raise ValueError(f"{venue_label}: every author needs an email to be added on ScholarOne; " f"missing for: {', '.join(missing)}")

    corresponding = [index for index, author in enumerate(authors) if author.get("corresponding")]
    if len(corresponding) != 1:
        raise ValueError(f"{venue_label}: exactly one author must be marked corresponding in the .sub; " f"found {len(corresponding)}")

    # Remove the pre-filled submitting author (Actions "Remove" + "Yes").
    logger.info("Removing the pre-filled author before rebuilding the list")
    page.get_by_role("cell", name="Select...").first.get_by_label("Actions").select_option("Remove")
    page.get_by_role("button", name="Yes").click()

    for index, author in enumerate(authors):
        if index:
            page.wait_for_timeout(1000)
        logger.debug("Adding author %d/%d", index + 1, len(authors))
        _add_author(page, author)

    # Assign the corresponding author via its row's Actions menu. The trailing
    # word boundary pins the row number so "Drag 1" won't match "Drag 10".
    corr_row = corresponding[0] + 1
    logger.info("Assigning author %d as the corresponding author", corr_row)
    row = page.get_by_role("row", name=re.compile(rf"Drag {corr_row}\b"))
    row.get_by_label("Actions").select_option("Assign")
    page.get_by_role("button", name="Yes").click()


def _split_reviewers(raw: str) -> list[dict]:
    """Parse the ``reviewers`` field into reviewer dicts.

    One per line: ``name | email | stance | reason`` (stance "recommended"/
    "opposed", reason optional). Count rules are enforced in :func:`_add_reviewers`.
    """
    reviewers: list[dict] = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("|")]
        name = parts[0] if len(parts) > 0 else ""
        email = parts[1] if len(parts) > 1 else ""
        stance = (parts[2] if len(parts) > 2 and parts[2] else "recommended").lower()
        reason = parts[3] if len(parts) > 3 else ""
        if stance not in REVIEWER_STANCES:
            raise ValueError(f"reviewer stance {stance!r} for {name or email!r} must be one of {sorted(REVIEWER_STANCES)}")
        reviewers.append({"name": name, "email": email, "stance": stance, "reason": reason})
    return reviewers


def _add_reviewer(page, reviewer: dict, *, add_button: str = "Add Reviewer", submit_button: str = " Add New Reviewer ") -> None:
    """Add one suggested or opposed reviewer on the ScholarOne reviewer step.

    "Add Reviewer" opens an inline form (``#FIRST_NAME``/``#LAST_NAME``/``#EMAIL``,
    a RECOMMENDED/OPPOSED radio pair, optional ``#DOC_PERSON_REASON``); the .sub
    name is split by :func:`_split_name`. ``add_button`` / ``submit_button`` name
    the controls, which vary by deployment (NAR uses "Add Referee").
    """
    given, family = _split_name(reviewer["name"])
    logger.info("Adding %s reviewer %s <%s>", reviewer["stance"], reviewer["name"], reviewer["email"])

    page.get_by_role("link", name=add_button).click()
    first_box = page.locator("#FIRST_NAME")
    first_box.click()
    first_box.fill(given)
    last_box = page.locator("#LAST_NAME")
    last_box.click()
    last_box.fill(family)
    email_box = page.locator("#EMAIL")
    email_box.click()
    email_box.fill(reviewer["email"])
    page.get_by_role("radio", name=REVIEWER_STANCES[reviewer["stance"]]).check()
    reason = reviewer.get("reason", "").strip()
    if reason:
        reason_box = page.locator("#DOC_PERSON_REASON")
        reason_box.click()
        reason_box.fill(reason)
    page.get_by_role("button", name=submit_button).click()


def _add_reviewers(
    page,
    reviewers: list[dict],
    *,
    min_recommended: int = MIN_RECOMMENDED_REVIEWERS,
    max_opposed: int | None = None,
    add_button: str = "Add Reviewer",
    submit_button: str = " Add New Reviewer ",
    venue_label: str = "scholarone",
) -> None:
    """Add every suggested/opposed reviewer on the ScholarOne reviewer step.

    Enforces ``min_recommended`` (opposed do not count) and, when given,
    ``max_opposed`` before touching the form, so a bad list fails up front. A
    short settle between adds mirrors :func:`_add_authors`. ``add_button`` /
    ``submit_button`` pass through to :func:`_add_reviewer`; ``venue_label`` shapes
    error messages.
    """
    recommended = sum(1 for reviewer in reviewers if reviewer["stance"] == "recommended")
    if recommended < min_recommended:
        raise ValueError(f"{venue_label}: at least {min_recommended} recommended reviewers are required " f"(opposed reviewers do not count); the .sub 'reviewers' field has {recommended}")
    if max_opposed is not None:
        opposed = sum(1 for reviewer in reviewers if reviewer["stance"] == "opposed")
        if opposed > max_opposed:
            raise ValueError(f"{venue_label}: at most {max_opposed} opposed reviewers are allowed; " f"the .sub 'reviewers' field has {opposed}")

    for index, reviewer in enumerate(reviewers):
        if index:
            page.wait_for_timeout(1000)
        _add_reviewer(page, reviewer, add_button=add_button, submit_button=submit_button)


class ScholarOneVenue(Venue):
    """A venue on the ScholarOne Manuscripts platform.

    Shares sign-in and the author/keyword/reviewer helpers; each journal's ``run``
    lives in its per-journal subclass. The login-state check keys on the " Author"
    dashboard link (:attr:`logged_in_names`) and the portal/login URL is the
    venue's ``submission_url``. ScholarOne cannot script a fresh session capture,
    so :attr:`supports_session_capture` is ``False`` and ``save_session`` raises.
    """

    #: The dashboard link that only renders for a signed-in ScholarOne session.
    logged_in_names = (AUTHOR_LINK,)
    #: ScholarOne's recorded flow can't script a fresh sign-in; disable capture.
    supports_session_capture = False

    #* login
    def login(self, page, username: str, password: str, *, timeout_ms: int = 15000) -> None:
        """Fill and submit the ScholarOne sign-in form from stored credentials.

        Raises :class:`ScholarOneLoginError` if the author dashboard never loads,
        so :meth:`ensure_signed_in` can fall back to a manual sign-in.
        """
        logger.debug("Filling the ScholarOne sign-in form for %s", self.slug)
        page.goto(self.portal_url)
        fill_login_form(
            page,
            username,
            password,
            userid_sel=LOGIN_USERID,
            password_sel=LOGIN_PASSWORD,
            submit_sel=LOGIN_BUTTON,
            timeout_ms=timeout_ms,
        )
        # A successful sign-in lands on the author dashboard (" Author" link).
        if not self.is_logged_in(page, timeout_ms=timeout_ms):
            raise ScholarOneLoginError(
                "submitted the credentials but the signed-in author dashboard did "
                "not load -- the username or password may be wrong, or ScholarOne "
                "added a step (CAPTCHA / two-factor) that can't be automated"
            )
