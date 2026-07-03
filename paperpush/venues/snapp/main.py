"""Springer Nature "Snapp" submission engine (Playwright).

Several venues submit through Springer Nature's platform at
``https://submission.springernature.com`` (internally "Snapp"), the BMC family
among them (Genome Biology, BMC Bioinformatics). The wizard is implemented once
here and parameterized by a :class:`Variant`, so each venue is a thin per-slug
binding module (``genome_biology``, ``bmc_bioinformatics``) that selects its
Variant and re-exposes the public API. Sign-in goes through the Springer Nature
identity provider, which the portal redirects to when not authenticated.

The run stops on the final review page (after the preprint-posting question)
without clicking the final-submit control, leaving the browser open via
:func:`hold_open` so the submission is finished by hand.

Re-capture selectors with ``playwright codegen
https://submission.springernature.com/`` if the wizard is restyled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ...database import get_venue
from ..base import Venue
from ..common import DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts, hold_open, open_run_context, split_name_first_last as _split_name
from ..login import VenueLoginError

logger = logging.getLogger(__name__)

# The Springer Nature IDP gateway used by venues that sign in directly rather than
# via a portal redirect; it scopes the flow to the venue's context code. Re-capture
# from a live sign-in if Springer Nature rotates the gateway parameters.
_BMC_BIOINFORMATICS_LOGIN_URL = (
    "https://idp-personal-authenticator.springernature.com/gateway"
    "?response_type=code"
    "&redirect_uri=https%3A%2F%2Fidp.springernature.com%2Fauthed%2Fpersonal"
    "&state=bd6fbfe3-77fe-4065-a1b0-c020912c465a"
    "&context_code=12859"
    "&target_redirect_uri=https%3A%2F%2Fsubmission.springernature.com%2Fnew-submission%2F12859%2F3"
    "&context_type=submission"
)


@dataclass(frozen=True)
class Variant:
    """Per-venue configuration for one Springer Nature "Snapp" deployment.

    ``context_code`` scopes a new submission (the wizard lives under
    ``/new-submission/<context>/<step>``; the recording entered at step 3).
    ``login_url`` is an explicit IDP gateway to sign in through; when ``None``,
    sign-in opens the new-submission URL and lets the portal redirect to the IDP.
    """

    slug: str
    name: str
    context_code: str
    login_url: str | None = None
    country_options_file: str | None = None

    @property
    def venue(self):
        """The database entry (URLs, author columns) for this venue."""
        return get_venue(self.slug)

    @property
    def portal_url(self) -> str:
        """The submission portal entry point (redirects to the IDP when signed out)."""
        return self.venue.submission_url

    @property
    def new_submission_url(self) -> str:
        """The new-submission wizard entry (article type + file upload, step 3)."""
        return f"https://submission.springernature.com/new-submission/{self.context_code}/3"

    @property
    def auto_login_url(self) -> str:
        """Where the venue's ``login`` navigates: the IDP gateway, or the wizard (portal redirect)."""
        return self.login_url or self.new_submission_url

    @property
    def manual_login_url(self) -> str:
        """Where the manual-sign-in fallback navigates: the IDP gateway, or the portal."""
        return self.login_url or self.portal_url


VARIANTS = {
    "genome_biology": Variant(
        "genome_biology",
        "Genome Biology",
        "13059",
        country_options_file="genome_biology_institution_countries.txt",
    ),
    "bmc_bioinformatics": Variant(
        "bmc_bioinformatics",
        "BMC Bioinformatics",
        "12859",
        login_url=_BMC_BIOINFORMATICS_LOGIN_URL,
    ),
}

# Default variant when a caller passes none; the binding modules pass their own.
_DEFAULT = VARIANTS["genome_biology"]

# The IDP occasionally rejects a correct email/password, so the sign-in flow is
# retried once (two attempts total) before falling back to a manual login.
_LOGIN_ATTEMPTS = 2

_YES = {"yes", "y", "true", "1", "on"}


class SnappLoginError(VenueLoginError):
    """Raised when an automatic Springer Nature (Snapp) sign-in cannot be completed."""


def session_path(cfg: Variant = _DEFAULT):
    """Path to the saved session (Playwright storage_state)."""
    from ..common import session_path as _session_path

    return _session_path(cfg.slug)


def _is_yes(value: str) -> bool:
    """True when a ``.sub`` value reads as an affirmative (yes/true/1/on)."""
    return str(value or "").strip().lower() in _YES


def _try(fn, what: str) -> bool:
    """Run ``fn``; log and continue if it fails. Returns True if it ran without raising.

    Several Springer Nature steps are optional or drift between releases, so a
    missing/moved control there should not abort the data-bearing steps that follow.
    """
    try:
        fn()
        return True
    except Exception as exc:  # noqa: BLE001 -- best-effort optional step
        logger.warning("Snapp: skipped %s (%s)", what, exc)
        return False


# --- parsing helpers ------------------------------------------------------


def _parse_authors(value: str, cfg: Variant = _DEFAULT) -> list[dict]:
    """Parse the author block into dicts using the venue's author column set."""
    from ...validate import parse_authors

    author_field = next((f for f in cfg.venue.fields if f.type == "authorlist"), None)
    return parse_authors(value, author_field.fields if author_field else None)


# Common ways authors write the United States, all of which the country drop-down
# lists only as "United States". Keys are letters-only, lower-cased (see
# :func:`_country_key`), so "U.S.", "U.S.A.", and "U S A" all collapse.
_US_ALIASES = {
    "us",
    "usa",
    "unitedstates",
    "unitedstatesofamerica",
    "america",
    "theunitedstates",
    "theunitedstatesofamerica",
}


def _country_key(country: str) -> str:
    """Reduce a country string to letters only, lower-cased, for loose matching."""
    return "".join(ch for ch in country.lower() if ch.isalpha())


def _normalize_country(country: str) -> str:
    """Map common US spellings to "United States"; return anything else stripped."""
    country = country.strip()
    if _country_key(country) in _US_ALIASES:
        return "United States"
    return country


def _resolve_country(country: str, allowed: list[str] | None) -> str:
    """Normalize ``country`` and, when ``allowed`` is given, match it (case-insensitively).

    A value not in the controlled list is returned as-is with a warning rather than
    aborting, since the affiliation step is best-effort.
    """
    country = _normalize_country(country)
    if not allowed or not country:
        return country
    match = next((c for c in allowed if c.lower() == country.lower()), None)
    if match is None:
        logger.warning("Snapp: country %r is not an allowed institution country; leaving as-is", country)
        return country
    return match


def _load_allowed_countries(options_file: str | None) -> list[str]:
    """Load the controlled institution-country list; ``[]`` when the variant has none.

    The drop-down's leading placeholder ("Select country or territory") is dropped.
    """
    if not options_file:
        return []
    from ...database import _load_options_file

    return [c for c in _load_options_file(options_file) if not c.lower().startswith("select country")]


def _affiliations_from_authors(authors: list[dict]) -> list[tuple[str, str, str, str]]:
    """Derive the unique ``(institution, department, city, country)`` tuples to add.

    Each institution must be added (via the search box) before an author can pick
    it, so distinct tuples are collected in first-appearance order; authors with no
    institution are skipped.
    """
    out: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for author in authors:
        institution = (author.get("institution") or "").strip()
        department = (author.get("department") or "").strip()
        city = (author.get("city") or "").strip()
        country = (author.get("country") or "").strip()
        if not institution:
            continue
        key = (institution, department, city, country)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _parse_funders(value: str) -> list[tuple[str, str]]:
    """Parse the funding block into ``(funder, grant_id)`` tuples (``funder | grant id``)."""
    out: list[tuple[str, str]] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        funder = parts[0]
        grant_id = parts[1] if len(parts) > 1 else ""
        if funder:
            out.append((funder, grant_id))
    return out


# --- sign-in --------------------------------------------------------------


def _dismiss_cookies(page, timeout_ms: int = 4000) -> bool:
    """Close the cookie-consent banner if showing (it intercepts clicks). Rejects optional cookies.

    Returns True if clicked, False if no banner appeared (not an error).
    """
    try:
        page.get_by_role("button", name="Reject optional cookies").click(timeout=timeout_ms)
        return True
    except Exception:  # noqa: BLE001 -- absent banner is fine (a timeout or no match)
        logger.debug("No cookie-consent banner to dismiss")
        return False


def _submit_credentials(page, username: str, password: str, timeout_ms: int) -> None:
    """Drive one email->password pass through the visible IDP sign-in form.

    Assumes the email field is showing. Fills the email, continues (past the
    account-chooser interstitial some flows re-ask the email on), then fills the
    password and submits. Raises :class:`SnappLoginError` if the password field
    never appears (unrecognized email, or an inserted CAPTCHA / two-factor step).
    """
    from playwright.sync_api import TimeoutError as PWTimeout

    email = page.get_by_role("textbox", name="Email address")
    email.fill(username)
    page.get_by_role("button", name="Continue").first.click()
    page.wait_for_timeout(1500)

    # Some IDP flows show an account-chooser interstitial that re-asks for the
    # email before the password; re-enter it best-effort when it reappears.
    if email.is_visible():
        _try(lambda: email.fill(username), "re-enter email")
        _try(lambda: page.get_by_role("button", name="Continue").first.click(), "continue past email")
        page.wait_for_timeout(1500)

    pwd = page.get_by_role("textbox", name="Please enter your password")
    try:
        pwd.wait_for(state="visible", timeout=timeout_ms)
    except PWTimeout as exc:
        raise SnappLoginError("reached the Springer Nature sign-in but the password field did not " "appear -- the email may be unrecognized, or the identity provider " "added a step (CAPTCHA / two-factor) that can't be automated") from exc
    pwd.fill(password)
    page.get_by_role("button", name="Continue").first.click()
    page.wait_for_timeout(2500)


def login(venue, headless: bool = False, new_session: bool = False) -> None:
    """Open the portal, sign in via ``venue.ensure_signed_in``, and hold the browser.

    ``new_session=True`` discards any saved session first, forcing a fresh sign-in.
    """
    from playwright.sync_api import sync_playwright

    session = venue.session_path()
    if new_session and session.exists():
        logger.info("Discarding the saved %s session at %s", venue.display_name, session)
        session.unlink()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context()
        apply_default_timeouts(context)
        page = context.new_page()
        if venue.ensure_signed_in(page, context):
            print(f"Signed in to {venue.display_name}; the browser is at the submission portal.")
        if not headless:
            hold_open()
        browser.close()


# --- repeated wizard actions ----------------------------------------------


def _click_next(page) -> None:
    """Advance the wizard with the single 'next' pagination control."""
    page.locator('[data-test="step-pagination-next"]').click()
    page.wait_for_timeout(1500)


def _fill_rich_text(page, frame_title: str, text: str) -> None:
    """Fill a TinyMCE rich-text editor (an iframe, addressed by its title)."""
    editor = page.locator(f'iframe[title="{frame_title}"]').content_frame
    editor.get_by_role("paragraph").click()
    editor.get_by_label("Rich Text Area. Press ALT-0").fill(text)


def _add_affiliation(page, institution: str, department: str, city: str = "", country: str = "", allowed_countries: list[str] | None = None) -> None:
    """Add one affiliation: search the institution, fill country/city/department, save.

    ``country`` is normalized (US aliases mapped) and, when ``allowed_countries`` is
    given, validated against that controlled list. Best-effort throughout (see :func:`_try`).
    """
    country = _resolve_country(country, allowed_countries)
    name_box = page.get_by_role("textbox", name="Institution name (E.g.")
    name_box.fill(institution)

    _try(lambda: page.get_by_label("Country").select_option(label=country), f"affiliation country {country!r}")

    city_box = page.get_by_role("textbox", name="City")
    city_box.click()
    _try(lambda: city_box.fill(city), f"affiliation city {city!r}")

    if department:
        details = page.get_by_role("textbox", name="Institution details (optional")
        details.click()
        _try(lambda: details.fill(department), f"affiliation department {department!r}")
    _try(lambda: page.get_by_test_id("save-add-institution-button").click(), "save affiliation")
    page.wait_for_timeout(1000)


def _add_author(page, index: int, author: dict, allowed_countries: list[str] | None = None) -> None:
    """Add one author row on the authors page.

    The first author (``author-0``) is filled in place; each later author is
    appended with "Add another author" and addressed via its ``author-<index>`` test
    id. Names come from the ``.sub`` columns (falling back to splitting a single
    name), and the primary affiliation is selected by the institution name.
    """
    if index == 0:
        scope = page
    else:
        page.get_by_role("button", name="Add another author").click(timeout=1000)
        page.wait_for_timeout(1000)
        scope = page.get_by_test_id(f"author-{index}")

    first = (author.get("first_name") or "").strip()
    last = (author.get("last_name") or "").strip()
    if not first and not last:
        first, last = _split_name(author.get("name", ""))

    given = scope.get_by_role("textbox", name="Given names")
    given.click()
    _try(lambda: given.fill(first), f"author {index} given names")
    family = scope.get_by_role("textbox", name="Family name")
    family.click()
    _try(lambda: family.fill(last), f"author {index} family name")
    email_box = scope.get_by_role("textbox", name="Email (Institutional email if")
    email_box.click()
    _try(lambda: email_box.fill(author.get("email", "")), f"author {index} email")

    institution = (author.get("institution") or "").strip()
    if institution:
        country = (author.get("country") or "").strip()
        country = _resolve_country(country, allowed_countries)
        institution += f", {country}"
        department = (author.get("department") or "").strip()
        if department:
            institution += f", {department}"
    else:
        institution = "No affiliation"
    _try(lambda: scope.get_by_label("Primary affiliation").select_option(label=institution), f"author {index} affiliation {institution!r}")


# --- wizard pages ---------------------------------------------------------


def _enter_details(page, title: str, abstract: str, cover_letter: str) -> None:
    """Fill the manuscript-details page: title, abstract, and cover letter."""
    if title:
        _try(lambda: _fill_rich_text(page, "Title. Rich Text Area", title), "title")
        page.wait_for_timeout(1000)
    if abstract:
        _try(lambda: _fill_rich_text(page, "Abstract. Rich Text Area", abstract), "abstract")
    if cover_letter:
        page.locator("#cover_letter").set_input_files(cover_letter)


def _enter_authors(page, authors: list[dict], author_contributions: str, allowed_countries: list[str] | None = None) -> None:
    """Fill the authors page: affiliations, author rows, corresponding author, contributions.

    Distinct affiliations are added first (each author then picks one by name), then
    the author rows, then the corresponding author (marked ``corresponding=yes``,
    defaulting to the first), then the contributions statement.
    """
    entered_institutions = page.locator('#institution-list strong').all_text_contents()
    for institution, department, city, country in _affiliations_from_authors(authors):
        if institution in entered_institutions:
            logger.info("Affiliation %r already entered; skipping", institution)
            continue
        _add_affiliation(page, institution, department, city, country, allowed_countries)

    try:  # get the list of already-entered authors to avoid duplicates (see #22)
        entered_authors = [t.split(" - ", 1)[1] for t in page.locator("[data-author-row-header]").all_text_contents()]
    except Exception:
        entered_authors = []

    for index, author in enumerate(authors):
        if author.get("name", "").strip() in entered_authors:
            logger.info("Author %r already entered; skipping", author.get("name"))
            continue
        _add_author(page, index, author, allowed_countries)

    # The corresponding-author drop-down is keyed by author index ("0", "1", ...).
    corresponding_selected = bool(page.locator("#primary-corresponding-author").input_value())
    if not corresponding_selected:
        page.wait_for_timeout(1000)  # wait for the author rows to be added
        corresponding_index = next((i for i, a in enumerate(authors) if _is_yes(a.get("corresponding"))), 0)
        _try(lambda: page.locator("[data-test=\"primary-corresponding-author-select\"]").select_option(str(corresponding_index)), "corresponding author")

    if author_contributions:
        _try(lambda: page.locator("[data-test=\"author-contributions\"]").fill(author_contributions), "author contributions")


def _enter_declarations(page, values: dict) -> None:
    """Click through the declarations page, driving the data-bearing answers.

    Consent confirmations and the recording's default "No" answers are clicked
    unconditionally; competing interests, data availability, acknowledgements, and
    funding come from the ``.sub``. Every control is best-effort (see :func:`_try`).
    Each ``get_by_text`` argument is a unique leading fragment of the visible text.
    """
    # Consent to submit (required).
    _try(lambda: page.get_by_text("By submitting my article I").click(), "consent to submit")

    # Competing interests: from the .sub.
    if _is_yes(values.get("competing_interests", "")):
        _try(lambda: page.get_by_text("Yes, the authors have").click(), "competing interests = Yes")
        statement = values.get("competing_interests_statement", "").strip()
        if statement:
            box = page.get_by_role("textbox", name="Specify your competing")
            box.click()
            _try(lambda: box.fill(statement), "competing interests statement")
    else:
        _try(lambda: page.get_by_text("No, I declare that the").click(), "competing interests = No")

    # Prior/concurrent publication (recording default: No).
    _try(lambda: page.get_by_text("No, the results/data/figures").click(), "prior publication = No")
    # Confirm the corresponding author (required).
    _try(lambda: page.get_by_text("I confirm the corresponding").click(), "confirm corresponding author")
    # Third-party material reuse (recording default: No).
    _try(lambda: page.get_by_text("No, all of the material is").click(), "third-party material = No")

    # Data availability: when a statement is given, declare data were used and add
    # it; otherwise the not-applicable default.
    statement = values.get("data_availability_statement", "").strip()
    if statement:
        _try(lambda: page.get_by_text("Yes. I used or generated").click(), "data used/generated = Yes")
        box = page.get_by_role("textbox", name="The statement added here will")
        box.click()
        _try(lambda: box.fill(statement), "data availability statement")
    else:
        _try(lambda: page.get_by_text("No or not applicable. This").click(), "data availability = No/NA")

    # Acknowledgements (optional).
    acknowledgements = values.get("acknowledgements", "").strip()
    if acknowledgements:
        page.get_by_role("textbox", name="Acknowledgements (optional)").fill(acknowledgements)

    # Funding: from the .sub. When funded, add each funder (attributed to the first
    # author, as in the recording).
    funders = _parse_funders(values.get("funding", ""))
    if _is_yes(values.get("research_funding", "")) or funders:
        _try(lambda: page.get_by_text("Yes, this research did").click(), "research funding = Yes")
        for funder, grant_id in funders:
            _try(lambda: page.locator('[data-test="author-with-research-funding"]').select_option("0"), "funding author")
            funder_box = page.get_by_role("textbox", name="Funder name (E.g. UK Space")
            funder_box.click()
            _try(lambda f=funder: funder_box.fill(f), f"funder name {funder!r}")
            if grant_id:
                grant_box = page.get_by_role("textbox", name="Grant ID (optional)")
                grant_box.click()
                _try(lambda g=grant_id: grant_box.fill(g), f"grant id {grant_id!r}")
            _try(lambda: page.get_by_test_id("save-add-funding-button").click(), "save funding")
            page.wait_for_timeout(1000)
    else:
        _try(lambda: page.get_by_text("No, this research did not").click(), "research funding = No")


def run(values: dict, headless: bool = False, debug: bool = False, new_session: bool = False, timeout: float = DEFAULT_TIMEOUT_SECONDS, *, venue) -> None:
    """Open the portal, sign in, then drive the Springer Nature wizard from ``values``.

    Clicks through the recorded steps (article type + files, details, authors,
    declarations) and stops on the review page after the preprint-posting question,
    leaving the browser open via :func:`hold_open`. Sign-in is handled by
    ``venue.ensure_signed_in``; ``new_session=True`` discards any saved session,
    ``debug=True`` forces a headed browser and opens the Inspector at the first action.
    """
    from playwright.sync_api import sync_playwright

    # The Inspector needs a visible browser; debugging headless makes no sense.
    if debug:
        headless = False

    cfg = venue.variant

    article_type = values.get("article_type", "").strip() or "research"
    title = values.get("title", "")
    abstract = values.get("abstract", "")
    manuscript_file = values.get("manuscript_file", "").strip()
    cover_letter = values.get("cover_letter", "").strip()
    # Springer Nature exposes a single non-manuscript upload slot, so figures and the
    # supplementary file both upload there; the .sub keeps them separate for clarity.
    supplementary_file = values.get("supplementary_file", "").strip()
    authors = _parse_authors(values.get("authors", ""), cfg)
    author_contributions = values.get("author_contributions", "")

    logger.info(
        "Starting %s submission run (headless=%s, debug=%s, " "type=%s, manuscript=%s, authors=%d)",
        cfg.name,
        headless,
        debug,
        article_type,
        manuscript_file,
        len(authors),
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = open_run_context(browser, venue.session_path(), new_session=new_session)
        apply_default_timeouts(context, timeout)
        page = context.new_page()

        venue.ensure_signed_in(page, context, debug=debug)

        if debug:
            page.pause()

        _dismiss_cookies(page)
        logger.debug("Signed in; opening a new submission")
        page.goto(cfg.new_submission_url)
        _dismiss_cookies(page)

        # Files page: article type, start the submission, then upload the files.
        _try(lambda: page.get_by_label("Article type:").select_option(article_type), f"article type {article_type!r}")
        _try(lambda: page.get_by_role("button", name="Start submission").click(), "start submission")
        page.wait_for_timeout(1500)
        if manuscript_file:
            page.locator("#manuscript").set_input_files(manuscript_file)
        if supplementary_file:
            page.locator("#supplementary").set_input_files(supplementary_file)
        _click_next(page)

        # Details page: title, abstract, cover letter.
        _enter_details(page, title, abstract, cover_letter)
        _click_next(page)

        # Authors page: affiliations, authors, corresponding author, contributions.
        _enter_authors(page, authors, author_contributions, _load_allowed_countries(cfg.country_options_file))
        _click_next(page)

        # Declarations page.
        _enter_declarations(page, values)
        _click_next(page)

        # Review page: answer the preprint-posting question, then stop before the
        # "request check" / final-submit control and leave the browser open.
        _try(lambda: page.get_by_text("want to post our").first.click(), "preprint posting = No")

        logger.info("Completed the recorded %s steps; leaving the browser " "open for review (the final submit is left to you)", cfg.name)
        hold_open()


# Engine entry point, aliased so the Venue subclass's ``run`` method does not shadow it.
_ENGINE_RUN = run


class SnappVenue(Venue):
    """A venue on Springer Nature's Snapp platform, selected by its Variant.

    BMC Bioinformatics and Genome Biology each subclass this and set
    :attr:`variant`. Springer Nature is the inverted-polarity case: the IDP always
    shows the "Email address" field while signing in, so its *presence* means signed
    *out* (:attr:`logged_in_present_means_in` is ``False``).
    """

    variant: Variant

    #: The IDP sign-in email field; its presence means we are NOT signed in.
    logged_in_role = "textbox"
    logged_in_names = ("Email address",)
    logged_in_present_means_in = False

    # portal_url and display_name are inherited (base returns the venue's
    # submission_url == variant.portal_url, and .name).
    @property
    def login_url(self) -> str:
        # Where the manual-sign-in fallback opens (IDP gateway, or the portal).
        return self.variant.manual_login_url

    def run(
        self,
        values: dict,
        *,
        headless: bool = False,
        debug: bool = False,
        new_session: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        _ENGINE_RUN(
            values,
            headless=headless,
            debug=debug,
            new_session=new_session,
            timeout=timeout,
            venue=self,
        )

    def login(self, page, username: str, password: str, *, timeout_ms: int = 20000) -> None:
        """Sign in through the Springer Nature IDP from credentials.

        Navigates to :attr:`Variant.auto_login_url`, dismisses the cookie banner,
        then runs the email->password form via :func:`_submit_credentials`. The IDP
        intermittently bounces a correct sign-in back to the email page; an explicit
        gateway link carries a one-time ``state`` token that cannot be cleanly
        reloaded, so the same pass is repeated in place (up to :data:`_LOGIN_ATTEMPTS`
        times). Raises :class:`SnappLoginError` on failure so :meth:`ensure_signed_in`
        can fall back to a manual sign-in.
        """
        from playwright.sync_api import TimeoutError as PWTimeout

        cfg = self.variant
        login_url = cfg.auto_login_url
        logger.debug("Signing in to %s via the Springer Nature IDP", cfg.name)
        page.goto(login_url)
        _dismiss_cookies(page)

        email = page.get_by_role("textbox", name="Email address")
        try:
            email.wait_for(state="visible", timeout=timeout_ms)
        except PWTimeout as exc:
            raise SnappLoginError(
                "could not find the email field on the Springer Nature sign-in page "
                "(the identity-provider form may have changed); re-capture the "
                f"selectors with 'playwright codegen {login_url}'"
            ) from exc

        for attempt in range(1, _LOGIN_ATTEMPTS + 1):
            _submit_credentials(page, username, password, timeout_ms)
            if self.is_logged_in(page, timeout_ms=8000):
                return
            # Kicked back: the signed-in portal did not load and the email box is
            # showing again. Retry the same pass in place while attempts remain.
            logger.warning("%s sign-in bounced back to the email page (attempt %d/%d)", cfg.name, attempt, _LOGIN_ATTEMPTS)
            if attempt < _LOGIN_ATTEMPTS:
                print(f"Sign-in bounced back to the email page (attempt {attempt}/{_LOGIN_ATTEMPTS}); retrying…")
                _try(lambda: email.wait_for(state="visible", timeout=timeout_ms), "wait for the email field to return")

        raise SnappLoginError(
            "submitted the credentials but the signed-in Springer Nature portal "
            "did not load -- the email or password may be wrong, or the identity "
            "provider added a step (CAPTCHA / two-factor) that can't be automated"
        )
