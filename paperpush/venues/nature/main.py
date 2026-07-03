"""Sign-in and submission wizard for the Nature family on eJournalPress (eJP).

One runner drives Nature, Nature Methods, and Nature Biotechnology; the few
per-venue differences live in a :class:`Variant`. Sign-in prefers a saved
Playwright session, then stored credentials (``easyeditorial login <slug>``),
then a manual sign-in. The eJP form varies between deployments, so ``login``
tries several candidate labels/selectors and raises :class:`NatureLoginError`
(dropping to a manual sign-in) if none match. Re-capture selectors with
``playwright codegen`` if Nature restyles the form.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from ...database import get_venue
from ...validate import parse_authors
from .. import get_venue_impl
from ..base import Venue
from ..common import (DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts,
                      hold_open, open_run_context)
from ..common import session_path as _session_path
from ..common import split_name_first_last as _split_name
from ..login import VenueLoginError

logger = logging.getLogger(__name__)


# Per-venue differences within the Nature eJP family, threaded into the shared
# engine as a ``cfg`` so the thin nature_methods/nature_biotech modules reuse it.
@dataclass(frozen=True)
class Variant:
    """Per-venue configuration for one member of the Nature eJP family.

    ``author_count_mode`` is ``"total"`` (ask for the full author count via
    ``num_authors``) or ``"additional"`` (Nature Methods/Biotech show four panes
    by default and ask only for contributors beyond those four).
    ``has_open_access`` / ``has_external_consultation`` say whether the
    declarations step offers those controls.
    """

    slug: str
    name: str
    supp_label: str = "Supplementary Information"
    author_count_mode: str = "total"
    has_open_access: bool = True
    has_external_consultation: bool = True
    # Dashboard link that starts a submission; Nature Biotech labels it "Submit Manuscript".
    submit_new_link: str = "Submit a new manuscript"

    @property
    def venue(self):
        """The database entry (URLs, author columns) for this venue."""
        return get_venue(self.slug)

    @property
    def portal_url(self) -> str:
        """The eJP submission-system entry point; redirects to sign-in when signed out."""
        return self.venue.submission_url

    @property
    def login_url(self) -> str:
        """The sign-in page; the portal route redirects there when signed out."""
        return self.portal_url


VARIANTS = {
    "nature": Variant("nature", "Nature"),
    "nature_methods": Variant(
        "nature_methods",
        "Nature Methods",
        supp_label="Supplementary and Additional Material",
        author_count_mode="additional",
        has_open_access=False,
        has_external_consultation=False,
    ),
    "nature_biotech": Variant(
        "nature_biotech",
        "Nature Biotechnology",
        supp_label="Supplemental & Additional Information",
        author_count_mode="additional",
        has_open_access=False,
        has_external_consultation=False,
        submit_new_link="Submit Manuscript",
    ),
}

_DEFAULT = VARIANTS["nature"]

# --- sign-in form selectors (eJournalPress login page) --------------------
# eJP deployments vary in wording, so each lookup tries the candidates in turn
# and uses the first present. Nature's own fields carry no label/aria-label, so
# _find_field falls back from the accessible-name labels to the id/name selectors.
LOGIN_USERNAME_LABELS = ("Login:", "Login", "Username:", "Email:", "E-mail:")
LOGIN_PASSWORD_LABELS = ("Password:", "Password")
LOGIN_BUTTON_NAMES = ("Login", "Log In", "Sign in", "Sign In")
LOGIN_USERNAME_SELECTORS = ("#login", "input[name='login']", "input[name='username']", "input[name='email']")
LOGIN_PASSWORD_SELECTORS = ("#password", "input[name='password']", "input[type='password']")
# Cookie-consent dialog (``dialog.cc-banner``) intercepts pointer events while
# open, blocking the Login click; _dismiss_cookie_banner clicks one to close it.
COOKIE_BANNER_BUTTON_NAMES = ("Reject optional cookies", "Accept all cookies", "Accept all", "Accept")
# A log-out control marks a signed-in eJP dashboard (see is_logged_in).
LOGOUT_LINK_NAMES = ("Log Out", "Logout", "Log out", "Sign Out", "Sign out")

# --- submission wizard selectors (eJP submit_ms flow) ---------------------
# Tabbed steps reached from the dashboard; navigation buttons are addressed by
# their visible value text. Re-capture with ``playwright codegen`` on drift.
BTN_CONTINUE = "Continue"
BTN_SAVE_CONTINUE = "Save and Continue"

# Manuscript-type splash: a group of ``ms_type_cde`` radios whose type name is
# free text beside each radio (no <label>). _select_manuscript_type reads that
# text and matches it against the ``.sub`` manuscript_type value.
SEL_MS_TYPE_RADIO = "input[name='ms_type_cde']"

# File-upload step: one reused ``file_<i>`` picker; each uploaded file gains an
# ``object_file_type_<id>`` type select, assigned in a second pass by upload
# order. Manuscript -> "Article File", cover letter -> "Author Cover Letter";
# any further files carry their own type from :data:`FILE_TYPE_OPTIONS` (by label).
SEL_FILE_TYPE = "select[name^='object_file_type_']"
FILE_TYPE_ARTICLE = "Article File"
FILE_TYPE_COVER_LETTER = "Author Cover Letter"
# File-type labels eJP offers; kept in sync with the ``additional_files`` field's
# ``type_options`` in venues.json.
FILE_TYPE_OPTIONS = (
    "Author Cover Letter",
    "Response to Referees Letter",
    "Article File",
    "Figure",
    "Table",
    "Extended Data",
    "Supplementary Table",
    "Supplementary Information",
    "Supplementary Video/Audio",
    "Related Manuscript File",
    "Source Data",
)

# Per-object metadata step (shown when files beyond the manuscript were
# uploaded): an ``object_title_<id>`` box per object, plus a ``color_<id>`` radio
# for figures and an ``object_desc_<id>`` description for supplementary files.
# Ordered by the id, which increments with upload order.
SEL_OBJECT_TITLE = "input[name^='object_title_']"

# Confirmation / data-policy steps. "verified" confirms the uploads; figshare is
# declined. Code Ocean: a blank ``code_availability`` keeps the default No; a
# statement switches the created radio to Yes, fills the text, and answers the
# obligatory enabled radio (No).
SEL_VERIFIED = "input[name='verified']"
SEL_CODE_OCEAN_NO = "#code_ocean_created_ind_no"
SEL_CODE_OCEAN_YES = "#code_ocean_created_ind_yes"
SEL_CODE_OCEAN_TXT = "#code_ocean_txt"
SEL_CODE_OCEAN_ENABLED_NO = "#code_ocean_enabled_ind_no"
SEL_FIGSHARE_NO = "#figshare_data_deposition_ind_no"

# Title / abstract step.
SEL_TITLE = "textarea[name='title']"
SEL_ABSTRACT = "textarea[name='abstract']"

# Authors step. ``num_authors`` + ``num_auth_button`` redraw the form with one
# sub-form per author. Nature Methods/Biotech instead default to four panes and
# take the number of *additional* contributors via ``more_contrib_cnt`` +
# "More Authors" (``author_count_mode == "additional"``).
SEL_AUTHORSHIP_IND = "input[name='authorship_ind']"
SEL_NUM_AUTHORS = "input[name='num_authors']"
SEL_NUM_AUTH_BUTTON = "input[name='num_auth_button']"
SEL_MORE_CONTRIB_CNT = "input[name='more_contrib_cnt']"
SEL_MORE_CONTRIB_BUTTON = "input[name='more_contrib']"
DEFAULT_AUTHOR_PANES = 4
# Whether the submitting account is the corresponding author: ``submitter_ind_no``
# (value 0) declares it is not; left unchecked otherwise, when an ``author_gender``
# drop-down also shows for the account holder.
SEL_SUBMITTER_NOT_CORR = "#submitter_ind_no"
SEL_AUTHOR_GENDER = "#author_gender"

# Author-box field-name prefixes; one fill routine (:func:`_fill_author_fields`)
# handles either sub-form by swapping the prefix. Corresponding author (pre-filled)
# uses ``corr_auth_``; each additional author uses ``contrib_auth_<N>_`` (1-based,
# .sub order), its pane expanded from a ``toggle_author_pane`` link first.
CORR_AUTHOR_PREFIX = "corr_auth_"
CONTRIB_AUTHOR_PREFIX = "contrib_auth_{n}_"

# The ``corr_auth_country`` drop-down lists full country names; a few common
# abbreviations are normalized to the option label, anything else tried verbatim.
_COUNTRY_ALIASES = {
    "US": "United States",
    "USA": "United States",
    "U.S.": "United States",
    "U.S.A.": "United States",
    "UNITED STATES OF AMERICA": "United States",
    "UK": "United Kingdom",
    "U.K.": "United Kingdom",
    "GREAT BRITAIN": "United Kingdom",
}

# --- subjects step selectors (eJP submit_ms flow) -------------------------
# A stack of dependent <select> menus: choosing a level fires
# ``get_next_level_subjects`` to repopulate the menu below it. Variable depth,
# so menus are addressed by 1-based level number; options matched by label. The
# "Add" button then commits the chosen path to the selected-subjects list.
SEL_SUBJECT_LEVEL = "#subject_level_{level}"
SEL_SUBJECT_ADD = "#subject_terms_add"


# --- declarations step selectors (eJP submit_ms flow) ---------------------
# Author declarations after the authors/subjects tabs. Radios by id (distinct
# yes/no ids), free-text boxes by name; a couple targeted by name+value where the
# id is ambiguous or unstable.
SEL_CONFLICT_NO = "#conflict_ind_1"  # none declared
SEL_CONFLICT_YES = "#conflict_ind_2"  # interests declared
SEL_CONFLICT_TXT = "textarea[name='conflict_of_interest_txt']"
SEL_DUAL_PUB_NO = "#dual_pub_ind_no"  # pre-checked
SEL_DUAL_PUB_YES = "#dual_pub_ind_yes"
SEL_DUAL_PUB_TXT = "textarea[name='dual_pub_txt']"
SEL_DOUBLE_BLIND_YES = "#double_blind_ind_yes"  # hide author info
SEL_DOUBLE_BLIND_NO = "#double_blind_ind_no"  # show it
SEL_MS_COMMENT = "textarea[name='ms_comment']"
SEL_OPEN_ACCESS = "#open_access_ind"
SEL_RESEARCH_SQUARE_DFA = "#research_square_dfa_ind"
# name+value: survives an id change / disambiguates a shared id.
SEL_EDITORIAL_OUTREACH = "input[name='editorial_outreach'][value='13']"
SEL_RESEARCH_SQUARE_IN_REVIEW_NO = "input[name='research_square_in_review_ind'][value='0']"
SEL_EXTERNAL_CONSULTATION_NO = "#external_consultation_ind_no"


NATURE_CATEGORIES_JSON = Path(__file__).parent.parent / "_assets" / "nature_categories.json"


def list_nature_categories(*levels: str) -> list[str]:
    """Return Nature subject categories one level below the path given in ``levels``.

    Pass the category names along the branch to drill into (level 1 first); the
    names one level deeper are returned, sorted. No args returns the level-1
    categories; a leaf returns an empty list. The tree is variable-depth (branches
    stop at level 2, 3, or 4). Names may also be given as one ``|``-separated
    string (the ``.sub`` subject_level format). Raises :class:`ValueError` for a
    name that is not a real branch.

    Surfaced by ``easyeditorial options nature.subject_level [PATH...]`` through
    :data:`FIELD_OPTION_LISTERS`, so an author can browse the tree while filling a
    ``.sub`` instead of running this module by hand.
    """
    with open(NATURE_CATEGORIES_JSON) as f:
        tree = json.load(f)

    # Accept separate args ("L1" "L2") or one ``|``-separated string; flatten both.
    names = [part.strip() for arg in levels for part in arg.split("|") if part.strip()]

    # Descend the tree one named level at a time.
    children = _normalize_category_children(tree)
    path: list[str] = []
    for name in names:
        path.append(name)
        if name not in children:
            raise ValueError(f"Unknown category: {' | '.join(path)}")
        children = _normalize_category_children(children[name].get("children"))

    return sorted(children)


# Browser-side scraper that walks the dependent subject-category menus on the
# eJP submission step. Kept in scripts/ so it can also be pasted into the console
# by hand; loaded here and injected with page.evaluate during a run.
NATURE_SCRAPE_JS = Path(__file__).resolve().parents[3] / "scripts" / "scrape_nature_categories.js"


def _normalize_category_children(children) -> dict:
    """Return ``children`` as the uniform recursive ``{name: {value, children}}`` map.

    Accepts a recursive dict (recursed into), a list of ``{name, value}`` leaves
    from the old three-level format (or a JSON string of one), or a falsy value
    (-> empty dict). Idempotent: already-recursive input is returned unchanged.
    """
    if not children:
        return {}
    if isinstance(children, str):
        children = json.loads(children)
    if isinstance(children, list):
        return {leaf["name"]: {"value": leaf["value"], "children": {}} for leaf in children}
    return {name: {"value": node["value"], "children": _normalize_category_children(node.get("children"))} for name, node in children.items()}


def convert_scraped_categories(scraped: dict) -> dict:
    """Convert the browser-scraped subject tree to the stored on-disk form.

    The scraper already returns the recursive tree, so this just normalizes it
    (also upgrading an older stored format). Idempotent.
    """
    return _normalize_category_children(scraped)


def _canonical_categories(tree: dict) -> str:
    """Normalized, sorted-key JSON of a category tree, for comparing two by content.

    A pure DOM reordering does not read as a change; only an added, removed,
    renamed, or re-valued subject makes two trees compare unequal.
    """
    return json.dumps(_normalize_category_children(tree), sort_keys=True, ensure_ascii=False)


def _scrape_categories(page, settle_ms: int = 1000, cfg: Variant = _DEFAULT):
    """Run the browser scraper on the current page and return the subject tree.

    Injects ``scrape_nature_categories.js`` and awaits ``scrapeNatureSubjects``.
    Only works on the submission step that renders the subject menus; returns
    ``None`` (with a warning) when ``#subject_level_1`` is absent.
    """
    if page.locator("#subject_level_1").count() == 0:
        logger.warning("%s: subject-category menus not on this page; skipping category check " "(open the submission step with the subject menus to run it)", cfg.name)
        return None

    js = NATURE_SCRAPE_JS.read_text()
    page.evaluate(js)
    logger.info("%s: scraping the subject-category tree (this walks every menu)", cfg.name)
    return page.evaluate("(ms) => window.scrapeNatureSubjects(ms)", settle_ms)


def verify_nature_categories(page, update: bool = True, settle_ms: int = 1000, cfg: Variant = _DEFAULT) -> bool:
    """Scrape Nature's live subject categories and compare them to the stored file.

    Returns ``True`` when the live tree matches ``nature_categories.json`` (or the
    menus aren't on the page). When they differ and ``update`` is set, rewrites
    the stored file and returns ``False`` so a later git step can commit it. Best
    effort: never raises.
    """
    try:
        scraped = _scrape_categories(page, settle_ms=settle_ms, cfg=cfg)
    except Exception as exc:  # noqa: BLE001 -- a scrape failure must not break login
        logger.warning("%s: could not scrape subject categories (%s); skipping check", cfg.name, exc)
        return True

    if scraped is None:
        return True

    fresh = convert_scraped_categories(scraped)

    if NATURE_CATEGORIES_JSON.exists():
        with open(NATURE_CATEGORIES_JSON) as f:
            stored = json.load(f)
        if _canonical_categories(stored) == _canonical_categories(fresh):
            logger.info("%s: subject categories are up to date", cfg.name)
            print(f"{cfg.name} subject categories are up to date.")
            return True
        logger.info("%s: subject categories have changed since the stored copy", cfg.name)
    else:
        logger.info("%s: no stored subject categories yet", cfg.name)

    if not update:
        print(f"{cfg.name} subject categories differ from the stored copy (not updated).")
        return False

    with open(NATURE_CATEGORIES_JSON, "w") as f:
        json.dump(fresh, f, indent=2, ensure_ascii=False)
        f.write("\n")
    logger.info("%s: updated %s", cfg.name, NATURE_CATEGORIES_JSON)
    print(f"Updated {cfg.name} subject categories -> {NATURE_CATEGORIES_JSON}\n" "Review and commit the change to the repository.")
    return False


class NatureLoginError(VenueLoginError):
    """Raised when an automatic Nature sign-in cannot be completed."""


def session_path(cfg: Variant = _DEFAULT):
    """Path to the saved browser session (Playwright storage_state)."""
    return _session_path(cfg.slug)


def _first_present(locators, timeout_ms: int):
    """Return the first visible locator from ``locators``, or ``None``.

    Fast pass: check each with ``is_visible()`` (no wait), the common case once
    loaded. Slow pass (nothing visible yet, e.g. a navigation in flight): wait on
    each in turn, splitting ``timeout_ms`` so the total stays bounded.
    """
    for locator in locators:
        try:
            if locator.first.is_visible():
                return locator.first
        except Exception:  # noqa: BLE001 -- a bad candidate is just "not present"
            continue

    per_try = max(1000, timeout_ms // max(1, len(locators)))
    for locator in locators:
        try:
            locator.first.wait_for(state="visible", timeout=per_try)
            return locator.first
        except PWTimeout:
            continue
    return None


def _first_visible(page, role: str, names, timeout_ms: int):
    """Return the first visible locator matching ``role``/one of ``names``.

    Tries each accessible name in turn (across eJP wording differences); see
    :func:`_first_present` for the two-pass behavior.
    """
    return _first_present([page.get_by_role(role, name=name) for name in names], timeout_ms)


def _find_field(page, labels, selectors, timeout_ms: int):
    """Locate a login form field, by id/name CSS ``selectors`` first, then ``labels``.

    Selectors are tried first because they match this deployment (Nature's inputs
    are unlabelled, so the accessible-name lookup would burn the timeout); the
    label candidates remain a fallback for deployments with labelled fields.
    Returns the first visible match or ``None``.
    """
    box = _first_present([page.locator(selector) for selector in selectors], timeout_ms)
    if box is not None:
        return box

    return _first_visible(page, "textbox", labels, timeout_ms)


def _dismiss_cookie_banner(page, timeout_ms: int = 4000) -> bool:
    """Close Nature's cookie-consent dialog if it is showing.

    The banner intercepts pointer events, blocking the Login click. Clicks the
    first matching consent button; returns True if one was clicked, False if no
    banner appeared within ``timeout_ms`` (not an error).
    """
    button = _first_visible(page, "button", COOKIE_BANNER_BUTTON_NAMES, timeout_ms)
    if button is None:
        logger.debug("No cookie-consent banner to dismiss")
        return False
    logger.debug("Dismissing the Nature cookie-consent banner")
    button.click()
    return True


def _venue(cfg: Variant):
    """The :class:`Venue` instance for a variant, so the module-level entry points
    can reuse the base class's shared sign-in orchestration and login-state check.
    """
    return get_venue_impl(cfg.slug)


def _type_into(box, text: str) -> None:
    """Enter ``text`` into ``box`` and make sure it landed.

    Some eJP fields only register a value typed key by key, so when a ``fill``
    does not read back, fall back to a click plus per-character typing.
    """
    box.fill(text)
    if box.input_value() == text:
        return
    logger.debug("fill did not stick; retyping the field key by key")
    box.click()
    box.fill("")
    box.press_sequentially(text, delay=20)


def login_nature(headless: bool = False, new_session: bool = False, cfg: Variant = _DEFAULT) -> None:
    """Open Nature, sign in, save the session, and leave the browser open.

    Signs in via ``ensure_signed_in`` (saved session, then stored credentials,
    then a manual sign-in) and leaves the browser at the author dashboard.
    ``new_session=True`` discards any saved session first (use after switching
    accounts).
    """
    session = session_path(cfg)
    if new_session and session.exists():
        logger.info("Discarding the saved %s session at %s", cfg.name, session)
        session.unlink()

    with sync_playwright() as playwright:
        # Headed so the manual-sign-in fallback (and any CAPTCHA/2FA) is visible.
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context()
        apply_default_timeouts(context)
        page = context.new_page()
        if _venue(cfg).ensure_signed_in(page, context):
            # Reload the (slow) portal only if the submission link isn't already up.
            if not page.get_by_role("link", name=cfg.submit_new_link).is_visible():
                page.goto(cfg.portal_url)
            print(f"Signed in to {cfg.name}; the browser is at the author dashboard.")
        if not headless:
            hold_open()
        browser.close()


# --- submission wizard ----------------------------------------------------


def _try(fn, what: str) -> bool:
    """Run ``fn``; log and continue if it fails, so a missing optional control
    does not abort the data-bearing steps that follow. Returns True on success.
    """
    try:
        fn()
        return True
    except Exception as exc:  # noqa: BLE001 -- best-effort optional step
        logger.warning("Nature: skipped %s (%s)", what, exc)
        return False


def _country_label(country: str) -> str:
    """Map a country name to the label shown in the eJP country drop-down."""
    value = (country or "").strip()
    return _COUNTRY_ALIASES.get(value.upper(), value)


def _is_corresponding(author: dict) -> bool:
    """True when an author line is marked as the corresponding author."""
    return bool(author.get("corresponding"))


def _parse_authors(value: str, cfg: Variant = _DEFAULT) -> list[dict]:
    """Parse the author block using the venue's ``authorlist`` columns.

    Nature's columns (``title | first_name | last_name | email | institution |
    country | city | corresponding``) differ from the package default, so they
    are read from the database rather than assumed.
    """
    author_field = next((f for f in cfg.venue.fields if f.type == "authorlist"), None)
    return parse_authors(value, author_field.fields if author_field else None)


def _click_button(page, name: str, timeout: int = 30000) -> None:
    """Click an eJP wizard button by its visible value text, then let it settle
    (the ``tvs_next`` buttons drive an in-page transition, not a navigation).
    """
    page.get_by_role("button", name=name).click(timeout=timeout)
    page.wait_for_timeout(1500)


def _fill_if_empty(page, selector: str, value: str) -> None:
    """Fill ``selector`` with ``value`` only when the field is blank, so a detail
    the signed-in account pre-filled is never overwritten by a placeholder.
    """
    if not value:
        return
    box = page.locator(selector)
    try:
        if box.input_value().strip():
            return
    except Exception:  # noqa: BLE001 -- treat an unreadable box as empty
        pass
    box.fill(value)


def _parse_files(value: str) -> list[tuple[str, str, str, str]]:
    """Parse the ``additional_files`` block into ``(path, type, title, extra)`` tuples.

    Each non-empty line is ``path | type | title | extra``; every column after the
    path is optional. ``type`` is the file's eJP type label (one of
    :data:`FILE_TYPE_OPTIONS`); ``title`` and ``extra`` feed the per-object
    metadata step (``extra`` = a figure's color or a supp file's description).
    Lines without a path are skipped.
    """
    files: list[tuple[str, str, str, str]] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        path = parts[0]
        file_type = parts[1] if len(parts) > 1 else ""
        title = parts[2] if len(parts) > 2 else ""
        extra = parts[3] if len(parts) > 3 else ""
        if path:
            files.append((path, file_type, title, extra))
    return files


def _parse_typed(value: str, file_type: str) -> list[tuple[str, str, str, str]]:
    """Parse a dedicated typed-file block into ``(path, file_type, title, extra)``.

    Used by the ``figure_files``/``table_files``/``supplementary_files``/
    ``supplementary_tables`` fields, whose type is fixed by the field. Each line
    is ``path | title | extra`` (last two optional); ``file_type`` is stamped on
    every entry. Lines without a path are skipped.
    """
    files: list[tuple[str, str, str, str]] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        path = parts[0]
        title = parts[1] if len(parts) > 1 else ""
        extra = parts[2] if len(parts) > 2 else ""
        if path:
            files.append((path, file_type, title, extra))
    return files


def _collect_extra_files(values: dict, cfg: Variant = _DEFAULT) -> list[tuple[str, str, str, str]]:
    """Build the eJP upload list: figures, then tables, then supplementary
    information and tables, then the generic ``additional_files`` (each naming its
    own type), in that order.
    """
    return _parse_typed(values.get("figure_files", ""), "Figure") + _parse_typed(values.get("table_files", ""), "Table") + _parse_typed(values.get("supplementary_files", ""), cfg.supp_label) + _parse_typed(values.get("supplementary_tables", ""), "Supplementary Table") + _parse_files(values.get("additional_files", ""))


def _is_described(type_label: str) -> bool:
    """True for files that get a title box on the per-object metadata step
    (figures and supplementary objects; not the Article File or letters).
    """
    label = type_label.strip()
    # "Supplement" covers both "Supplementary ..." (Nature, Nature Methods) and
    # Nature Biotechnology's "Supplemental & Additional Information".
    return label == "Figure" or label.startswith("Supplement") or label in ("Extended Data", "Source Data")


def _upload_files(page, files: list[tuple]) -> None:
    """Upload every file on the eJP upload step, then set each file's type.

    ``files`` is ``(path, type_label, ...)`` in upload order. Each file is handed
    to a reused ``file_<i>`` picker in a loop (attach, "Upload Files", repeat);
    only once they all exist are the ``object_file_type_`` selects assigned in a
    second pass, the Nth select to the Nth file. A blank ``type_label`` leaves the
    default; type assignment is best-effort. Blank-path entries are skipped.
    """
    files = [(entry[0], entry[1]) for entry in files if entry[0]]

    for i, (path, type_label) in enumerate(files):
        logger.info("Uploading %s as %r", path, type_label or "(default type)")
        page.locator(f"input[name='file_{i}']").set_input_files(path)
        page.wait_for_timeout(1500)

    _click_button(page, "Upload Files", timeout=120000)

    # eJP names each type drop-down ``object_file_type_<id>``, the id incrementing
    # with upload order. Order the selects by that numeric suffix rather than by
    # DOM position so the Nth file maps to the Nth-uploaded select even if eJP
    # renders the rows in some other order.
    names = page.locator(SEL_FILE_TYPE).evaluate_all("els => els.map(e => e.name)")
    names.sort(key=lambda name: int(name.rsplit("_", 1)[-1]))

    for index, (path, type_label) in enumerate(files):
        if not type_label:
            continue
        if index >= len(names):
            logger.warning("No file-type select for %s; skipping type %r", path, type_label)
            continue
        name = names[index]
        _try(
            lambda n=name, label=type_label: page.locator(f"select[name='{n}']").select_option(label=label),
            f"file type {type_label!r} for {path}",
        )


def _fill_author_fields(page, author: dict, prefix: str) -> None:
    """Fill one author's boxes given its eJP field-name ``prefix`` (``corr_auth_``
    or ``contrib_auth_<N>_``; both share a layout).

    Text boxes are filled only when blank (never overwriting a pre-filled detail);
    the title/country selects are set when the ``.sub`` names one, the country
    normalized for a few abbreviations. A one-column ``name`` is split as a
    fallback. Every field is best-effort.
    """
    first = author.get("first_name", "").strip()
    last = author.get("last_name", "").strip()
    if not first and not last:
        first, last = _split_name(author.get("name", ""))

    title = author.get("title", "").strip()
    if title:
        _try(lambda: page.locator(f"select[name='{prefix}title']").select_option(label=title), f"{prefix}title {title!r}")
    _try(lambda: _fill_if_empty(page, f"input[name='{prefix}first_nm']", first), f"{prefix}first_nm")
    _try(lambda: _fill_if_empty(page, f"input[name='{prefix}last_nm']", last), f"{prefix}last_nm")
    _try(lambda: _fill_if_empty(page, f"input[name='{prefix}email']", author.get("email", "")), f"{prefix}email")
    _try(lambda: _fill_if_empty(page, f"input[name='{prefix}org']", author.get("institution", "")), f"{prefix}org")
    if author.get("country"):
        _try(lambda: page.locator(f"select[name='{prefix}country']").select_option(label=_country_label(author["country"])), f"{prefix}country {author['country']!r}")


def _open_contrib_pane(page, n: int) -> None:
    """Expand the Nth additional-author pane so its boxes become fillable.

    The pane is collapsed behind a ``toggle_author_pane('contrib_auth_<N>_')``
    link; clicked only when a field isn't already visible (so an open pane isn't
    toggled shut). A missing toggle is a no-op.
    """
    name_box = page.locator(f"input[name='contrib_auth_{n}_last_nm']")
    if name_box.count() and name_box.first.is_visible():
        return
    toggle = page.locator(f'a[onclick*="contrib_auth_{n}_"]')
    if toggle.count() == 0:
        logger.debug("Nature: no Edit/View toggle for additional author %d", n)
        return
    _try(lambda: toggle.first.click(), f"open additional-author pane {n}")
    page.wait_for_timeout(300)


def _enter_authors(page, authors: list[dict]) -> None:
    """Fill every author on the eJP authors step, not just the corresponding one.

    The author marked corresponding (or the first line when none is marked) is the
    submitting account, whose pre-filled boxes use the ``corr_auth_`` prefix. Every
    other author, in ``.sub`` order, fills an additional-author pane
    (``contrib_auth_<N>_``, 1-based), expanded from its "Edit/View Author Details"
    toggle before its boxes are set.
    """
    if not authors:
        return
    corr_index = next((i for i, a in enumerate(authors) if _is_corresponding(a)), 0)
    _fill_author_fields(page, authors[corr_index], CORR_AUTHOR_PREFIX)

    others = [a for i, a in enumerate(authors) if i != corr_index]
    for n, author in enumerate(others, start=1):
        _open_contrib_pane(page, n)
        _fill_author_fields(page, author, CONTRIB_AUTHOR_PREFIX.format(n=n))


_YES = {"yes", "y", "true", "1", "on"}


def _is_yes(value: str) -> bool:
    """True when a ``.sub`` value reads as an affirmative (yes/true/1/on)."""
    return str(value or "").strip().lower() in _YES


def _check_radio(page, selector: str, what: str) -> None:
    """Best-effort: select a radio/checkbox identified by ``selector``."""
    _try(lambda: page.locator(selector).check(), what)


def _normalize_type_label(text: str) -> str:
    """Lowercase ``text`` and collapse non-alphanumeric runs to a space, so a
    ``.sub`` manuscript_type matches the free text beside a radio regardless of
    casing, punctuation, or whitespace.
    """
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _select_manuscript_type(page, label: str) -> None:
    """Select the ``ms_type_cde`` radio whose on-page text matches ``label``.

    eJP renders each type name beside its radio (no <label>), so the text is
    gathered per radio by trying, in order: a <label>; the nodes right after the
    radio; the next table cell; the whole row. Matched after normalization, so it
    survives eJP renumbering. A blank ``label`` leaves the default; a non-matching
    value raises :class:`ValueError` listing the offered options.
    """
    label = (label or "").strip()
    if not label:
        return
    # The submit-link click navigates to the (slow) manuscript-type splash, and
    # evaluate_all does not auto-wait, so scraping before the page renders yields
    # an empty option list. Wait for at least one type radio to attach first; if
    # none ever appears the scrape below still raises with the offered options.
    try:
        page.locator(SEL_MS_TYPE_RADIO).first.wait_for(state="attached", timeout=20000)
    except Exception:
        pass
    options = page.locator(SEL_MS_TYPE_RADIO).evaluate_all(
        """els => els.map(e => {
            const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
            // 1) associated or wrapping <label>.
            let lab = e.closest('label');
            if (!lab && e.id) lab = document.querySelector('label[for="' + e.id + '"]');
            if (lab && clean(lab.textContent)) return {value: e.value, text: lab.textContent};
            // 2) text/elements right after the radio (inline "<input> Article<br>").
            let after = '';
            for (let n = e.nextSibling; n; n = n.nextSibling) {
                if (n.nodeName === 'BR') break;
                after += n.textContent || '';
                if (n.nodeType === 1 && clean(after)) break;  // stop after first element with text
            }
            if (clean(after)) return {value: e.value, text: after};
            // 3) the next table cell ("<td>radio</td><td>name</td>").
            const cell = e.closest('td, th');
            if (cell && cell.nextElementSibling && clean(cell.nextElementSibling.textContent))
                return {value: e.value, text: cell.nextElementSibling.textContent};
            // 4) the whole row, then the parent, as last resorts.
            const row = e.closest('tr');
            if (row && clean(row.textContent)) return {value: e.value, text: row.textContent};
            return {value: e.value, text: e.parentElement ? e.parentElement.textContent : ''};
        })"""
    )
    logger.debug("manuscript-type options offered by eJP: %s", [(o["value"], _normalize_type_label(o["text"])) for o in options])
    want = _normalize_type_label(label)
    # Prefer an exact normalized match; fall back to a containment match so a
    # slightly fuller on-page label (e.g. trailing notes) still resolves.
    match = next((o["value"] for o in options if _normalize_type_label(o["text"]) == want), None)
    if match is None:
        match = next(
            (o["value"] for o in options if want and (want in _normalize_type_label(o["text"]))),
            None,
        )
    if match is None:
        offered = [o["text"].strip() for o in options]
        raise ValueError(f"manuscript type {label!r} is not offered; eJP lists: {offered}")
    _check_radio(page, f"{SEL_MS_TYPE_RADIO}[value='{match}']", f"manuscript type = {label}")


def _load_category_tree() -> dict:
    """Load the stored subject-category tree as the level-1 recursive children map
    from ``nature_categories.json``, against which a ``.sub`` path is validated.
    """
    with open(NATURE_CATEGORIES_JSON) as f:
        return _normalize_category_children(json.load(f))


def _parse_subject_paths(subject_value: str) -> list[list[str]]:
    """Parse the ``subject_level`` field into a list of category paths.

    One category per line, each a ``|``-separated path of level names (level 1
    first). Blank lines and blank levels are dropped.
    """
    paths: list[list[str]] = []
    for line in (subject_value or "").splitlines():
        parts = [part.strip() for part in line.split("|") if part.strip()]
        if parts:
            paths.append(parts)
    return paths


def validate_subject_path(path: list[str], tree: dict | None = None) -> None:
    """Raise ``ValueError`` unless ``path`` (level names, level 1 first) names a
    real branch of the stored tree; the first bad level is reported with the
    partial path. ``tree`` defaults to a fresh load of ``nature_categories.json``.
    """
    children = _load_category_tree() if tree is None else tree
    walked: list[str] = []
    for name in path:
        walked.append(name)
        if name not in children:
            raise ValueError(f"unknown subject category: {' | '.join(walked)}")
        children = children[name]["children"]


# Nature requires between two and five subject categories, each at least two
# levels deep (a level 1 and a level 2); levels beyond that are optional.
MIN_SUBJECT_LEVELS = 2
MIN_SUBJECT_ENTRIES = 2
MAX_SUBJECT_ENTRIES = 5


def validate_subjects(subject_value: str) -> list[list[str]]:
    """Parse and validate every category path in the ``subject_level`` field.

    Returns the parsed paths when valid. Enforces Nature's requirements: between
    :data:`MIN_SUBJECT_ENTRIES` and :data:`MAX_SUBJECT_ENTRIES` categories, each
    at least :data:`MIN_SUBJECT_LEVELS` levels deep and present in the stored
    tree; raises ``ValueError`` on the first violation. A blank field is allowed
    (empty list); the count floor applies only once a category is given.
    """
    tree = _load_category_tree()
    paths = _parse_subject_paths(subject_value)
    if paths and not (MIN_SUBJECT_ENTRIES <= len(paths) <= MAX_SUBJECT_ENTRIES):
        raise ValueError(f"choose between {MIN_SUBJECT_ENTRIES} and {MAX_SUBJECT_ENTRIES} subject categories, one per line (got {len(paths)})")
    for index, path in enumerate(paths, start=1):
        if len(path) < MIN_SUBJECT_LEVELS:
            raise ValueError(f"subject category {index} ({' | '.join(path)}) needs at least {MIN_SUBJECT_LEVELS} levels (level 1 | level 2)")
        validate_subject_path(path, tree)
    return paths


def _validate_subject_field(value: str) -> str:
    """pydantic ``AfterValidator`` around :func:`validate_subjects`: returns the
    value when valid, else raises ``ValueError``. Exposed via :data:`FIELD_VALIDATORS`.
    """
    validate_subjects(value)
    return value


# Custom field validators layered onto the pydantic schema (see
# get_field_validators), so a bad subject_level is caught by ``easyeditorial submit``.
FIELD_VALIDATORS = {"subject_level": _validate_subject_field}

# Custom option listers for ``easyeditorial options`` (see get_field_option_listers).
# The subject tree is variable-depth, so it is browsed a level at a time rather
# than dumped as a flat list the way a field's inline options are.
FIELD_OPTION_LISTERS = {"subject_level": list_nature_categories}


def _select_one_subject(page, path: list[str], settle_ms: int = 3000) -> None:
    """Enter one subject-category path through the dependent menus and commit it.

    Resets the menus, selects each level top-down (each selection fires
    ``get_next_level_subjects`` to populate the menu below, so a settle pause
    follows), then clicks "Add" to commit the path. Best-effort: a name matching
    no option, or a menu this branch lacks, is logged and skipped.
    """
    # Reset to the placeholder (option 0) so a stale selection isn't carried over.
    _try(lambda: page.locator(SEL_SUBJECT_LEVEL.format(level=1)).select_option(index=0), "reset subject menus")
    page.wait_for_timeout(settle_ms)

    for index, label in enumerate(path):
        level = index + 1
        selector = SEL_SUBJECT_LEVEL.format(level=level)
        if page.locator(selector).count() == 0:
            logger.warning(
                "Nature: subject level-%s menu is not present, so %r cannot be " "selected (the chosen branch has no level-%s menu)",
                level,
                label,
                level,
            )
            return
        if not _try(
            lambda s=selector, value=label: page.locator(s).select_option(label=value),
            f"subject level {level} = {label!r}",
        ):
            return
        page.wait_for_timeout(settle_ms)  # let get_next_level_subjects repopulate

    # Commit the path; without this click it isn't added to the subject list.
    _try(lambda: page.locator(SEL_SUBJECT_ADD).click(), "add subject category")
    page.wait_for_timeout(settle_ms)


def _select_subjects(page, subject_value: str, settle_ms: int = 3000) -> None:
    """Choose the dependent Nature subject-category menus from the ``.sub``.

    Validates every path in the ``subject_level`` field first (so a typo is
    reported rather than silently selecting nothing), then enters and commits each
    in turn. A blank field adds nothing; an invalid name raises (a ``.sub`` error
    worth surfacing).
    """
    paths = validate_subjects(subject_value)
    for path in paths:
        _select_one_subject(page, path, settle_ms=settle_ms)


def _enter_declarations(page, values: dict, cfg: Variant = _DEFAULT) -> None:
    """Fill the author-declarations tab(s) from the ``.sub``.

    Competing interests, prior/concurrent publication, peer-review anonymization,
    comments to the editor; ticks the Research Square (DFA) box and declines In
    Review, plus the open-access and external-consultation controls where the
    variant offers them. Every control is best-effort.
    """
    # Competing interests (required). 'yes' selects the interests-declared radio
    # and types the statement paragraph; anything else declares none.
    if _is_yes(values.get("competing_interests", "")):
        _check_radio(page, SEL_CONFLICT_YES, "competing interests = Yes")
        statement = values.get("competing_interests_statement", "").strip()
        if statement:
            _try(lambda: page.locator(SEL_CONFLICT_TXT).fill(statement), "competing interests statement")
    else:
        _check_radio(page, SEL_CONFLICT_NO, "competing interests = No")

    # Prior/concurrent publication. Defaults to No (the pre-checked option).
    if _is_yes(values.get("dual_publication", "")):
        _check_radio(page, SEL_DUAL_PUB_YES, "published elsewhere = Yes")
        statement = values.get("dual_publication_statement", "").strip()
        if statement:
            _try(lambda: page.locator(SEL_DUAL_PUB_TXT).fill(statement), "prior-publication statement")
    else:
        _check_radio(page, SEL_DUAL_PUB_NO, "published elsewhere = No")

    # Peer-review anonymization. Only set when the .sub names a preference;
    # 'double-anonymized' hides author information, otherwise show it.
    peer_review = values.get("peer_review", "").strip().lower()
    if peer_review.startswith("double"):
        _check_radio(page, SEL_DOUBLE_BLIND_YES, "double-anonymized peer review")
    elif peer_review.startswith("single"):
        _check_radio(page, SEL_DOUBLE_BLIND_NO, "single-anonymized peer review")

    # Free-text comments to the editor.
    comments = values.get("comments_to_editor", "").strip()
    if comments:
        _try(lambda: page.locator(SEL_MS_COMMENT).fill(comments), "comments to the editor")

    # Open-access opt-in: tick the box (offered only by some venues).
    if cfg.has_open_access:
        _check_radio(page, SEL_OPEN_ACCESS, "open access opt-in")

    # Research Square author dashboard (DFA) opt-in: tick the box.
    _check_radio(page, SEL_RESEARCH_SQUARE_DFA, "Research Square author dashboard")

    # Editorial-outreach mailing opt-in: tick the box.
    _check_radio(page, SEL_EDITORIAL_OUTREACH, "editorial outreach")

    # In Review preprint posting: decline (radio value 0).
    _check_radio(page, SEL_RESEARCH_SQUARE_IN_REVIEW_NO, "decline In Review")

    # External consultation: decline (offered only by some venues).
    if cfg.has_external_consultation:
        _check_radio(page, SEL_EXTERNAL_CONSULTATION_NO, "decline external consultation")


def _advance_to_subjects(page, values: dict, cfg: Variant = _DEFAULT) -> None:
    """Drive the eJP wizard from the dashboard to the subject-category step.

    Shared by :func:`run_nature` and :func:`check_categories` so the monthly
    category check walks the same path a real submission does. Starts a new
    manuscript and clicks through the upload, file-confirmation, data-policy,
    title/abstract, and author steps, leaving the page on the subject tab. The
    caller must already be signed in.
    """
    manuscript_file = values.get("manuscript_file", "").strip()
    cover_letter = values.get("cover_letter", "").strip()
    extra_files = _collect_extra_files(values, cfg)
    manuscript_title = values.get("title", "")
    abstract = values.get("abstract", "")
    authors = _parse_authors(values.get("authors", ""), cfg)

    # Wait for the submission link before clicking it: the dashboard loads slowly,
    # and clicking before it appears was a prior failure.
    submit_link = page.get_by_role("link", name=cfg.submit_new_link)
    submit_link.wait_for(state="visible", timeout=30000)
    submit_link.click()

    # Manuscript-type splash: pick the type named in the ``.sub`` (left at eJP's
    # default when blank, as for the monthly category check).
    _select_manuscript_type(page, values.get("manuscript_type", ""))
    _click_button(page, BTN_CONTINUE)

    # Upload step: manuscript as the Article File, optional cover letter, then any
    # further files. Each entry is ``(path, type, title, description)``; the
    # manuscript and cover letter carry no title/description.
    files = [(manuscript_file, FILE_TYPE_ARTICLE, "", "")]
    if cover_letter:
        files.append((cover_letter, FILE_TYPE_COVER_LETTER, "", ""))
    files.extend(extra_files)
    _upload_files(page, files)
    _click_button(page, BTN_SAVE_CONTINUE)

    # Per-object metadata step (only when files beyond the manuscript exist): a
    # title per figure/supplementary object, a color radio per figure, and an
    # optional description per supplementary file. Titles fall back to the file
    # name; the id increments with upload order.
    title_names = page.locator(SEL_OBJECT_TITLE).evaluate_all("els => els.map(e => e.name)")
    if title_names:
        # eJP renders a title box for every uploaded object (Article File and
        # cover letter included, which we don't describe), so classify each by the
        # controls beside it: a figure has ``color_<id>``, a supp file has
        # ``object_desc_<id>``, the manuscript/letter have neither (dropped). Kept
        # boxes, sorted by id, line up with the described entries of ``files``.
        objects = []
        for name in title_names:
            obj_id = name.rsplit("_", 1)[-1]
            is_figure = bool(page.locator(f"input[name='color_{obj_id}']").count())
            is_supp = bool(page.locator(f"textarea[name='object_desc_{obj_id}']").count())
            if is_figure or is_supp:
                objects.append((int(obj_id), name, obj_id, is_figure))
        objects.sort(key=lambda obj: obj[0])

        described = [(path, type_label, title, extra) for path, type_label, title, extra in files if path and _is_described(type_label)]
        if len(objects) != len(described):
            logger.warning(
                "Nature: %d figure/supplementary boxes on the page but %d such files in the .sub; matching by upload order",
                len(objects),
                len(described),
            )
        for (_, name, obj_id, is_figure), (path, _type, title, extra) in zip(objects, described):
            title = title or Path(path).stem
            _try(
                lambda n=name, t=title: page.locator(f"input[name='{n}']").fill(t),
                f"object title {title!r}",
            )
            if is_figure:
                # Fourth column is the color choice, defaulting to color.
                choice = extra.strip().lower()
                if choice not in ("color", "bw"):
                    choice = "color"
                _try(
                    lambda i=obj_id, c=choice: page.locator(f"input[name='color_{i}'][value='{c}']").check(),
                    f"figure color {choice!r} for object {obj_id}",
                )
            elif extra:
                # Fourth column is the optional supplementary-file description.
                _try(
                    lambda i=obj_id, d=extra: page.locator(f"textarea[name='object_desc_{i}']").fill(d),
                    f"object description for object {obj_id}",
                )
        _click_button(page, BTN_SAVE_CONTINUE)

    # Include all files in merged PDF (Nature only; other family venues omit this).
    if cfg.slug == "nature":
        checkboxes = page.locator('input[name="include_object[]"]')
        for i in range(checkboxes.count()):
            checkboxes.nth(i).check()

    # Confirm the uploaded files.
    _try(lambda: page.locator(SEL_VERIFIED).check(), "verify uploaded files")
    _click_button(page, BTN_SAVE_CONTINUE)

    # Code Ocean / figshare deposition step. A code-availability statement sets
    # created=Yes, fills the text, and answers the obligatory enabled radio (No);
    # otherwise decline (the default). figshare is always declined.
    code_availability = values.get("code_availability", "").strip()
    if code_availability:
        _try(lambda: page.locator(SEL_CODE_OCEAN_YES).check(), "code-ocean = Yes")
        _try(lambda: page.locator(SEL_CODE_OCEAN_TXT).fill(code_availability), "code-ocean statement")
        _try(lambda: page.locator(SEL_CODE_OCEAN_ENABLED_NO).check(), "code-ocean enabled = No")
    else:
        _try(lambda: page.locator(SEL_CODE_OCEAN_NO).check(), "code-ocean = No")
    _try(lambda: page.locator(SEL_FIGSHARE_NO).check(), "figshare = No")
    _click_button(page, BTN_SAVE_CONTINUE)
    _click_button(page, BTN_SAVE_CONTINUE)

    # Title and abstract.
    if manuscript_title:
        page.locator(SEL_TITLE).fill(manuscript_title)
    if abstract:
        page.locator(SEL_ABSTRACT).fill(abstract)
    _click_button(page, BTN_SAVE_CONTINUE)

    # Authors step: confirm authorship, set the author count, redraw the form,
    # then fill every author (corresponding sub-form plus each additional pane).
    _try(lambda: page.locator(SEL_AUTHORSHIP_IND).check(), "authorship confirmation")
    if cfg.author_count_mode == "additional":
        # These venues show four author panes by default and only ask for more
        # when the paper has more than four authors: type the number of
        # *additional* contributors beyond those four into ``more_contrib_cnt``
        # and click "More Authors" to redraw the form with the extra panes. With
        # four or fewer authors the default panes suffice, so skip the control.
        num_additional = max(0, len(authors) - DEFAULT_AUTHOR_PANES)
        if num_additional:
            _try(lambda: page.locator(SEL_MORE_CONTRIB_CNT).fill(str(num_additional)), "number of additional authors")
            _try(lambda: page.locator(SEL_MORE_CONTRIB_BUTTON).click(), "add additional authors")
            page.wait_for_timeout(1500)
    else:
        num_authors = str(len(authors) or 1)
        _try(lambda: page.locator(SEL_NUM_AUTHORS).fill(num_authors), "number of authors")
        _try(lambda: page.locator(SEL_NUM_AUTH_BUTTON).click(), "apply number of authors")
        page.wait_for_timeout(1500)

    # Whether the submitting account is the corresponding author; when it is, the
    # form shows a gender drop-down for the account holder, set from the .sub.
    are_you_corr = _is_yes(values.get("are_you_corresponding", ""))
    if not are_you_corr:
        _check_radio(page, SEL_SUBMITTER_NOT_CORR, "submitter is not the corresponding author")
    else:
        gender = values.get("corresponding_gender", "").strip()
        if gender:
            _try(lambda: page.locator(SEL_AUTHOR_GENDER).select_option(label=gender), f"corresponding-author gender {gender!r}")

    _enter_authors(page, authors)
    _click_button(page, BTN_SAVE_CONTINUE)


def run_nature(values: dict, headless: bool = False, debug: bool = False, new_session: bool = False, timeout: float = DEFAULT_TIMEOUT_SECONDS, *, venue) -> None:
    """Open Nature, sign in, then drive the eJP submission wizard from a ``.sub``.

    Signs in via ``ensure_signed_in``, then clicks through the captured eJP steps
    (upload, per-object metadata, data-policy, title/abstract, authors, subjects,
    declarations). ``new_session=True`` discards any saved session first;
    ``debug=True`` forces a headed browser and pauses in the Inspector.

    The run stops after the declarations step and leaves the browser open via
    :func:`hold_open`; it never clicks a final submit, so the rest is done by hand.
    """
    # The Inspector needs a visible browser; debugging headless makes no sense.
    if debug:
        headless = False

    cfg = venue.variant

    manuscript_file = values.get("manuscript_file", "").strip()
    logger.info("Starting %s submission run (headless=%s, debug=%s): " "manuscript=%s", cfg.name, headless, debug, manuscript_file)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = open_run_context(browser, venue.session_path(), new_session=new_session)
        apply_default_timeouts(context, timeout)
        page = context.new_page()

        venue.ensure_signed_in(page, context, debug=debug)

        if debug:
            page.pause()

        logger.debug("Signed in; starting a new manuscript submission")
        _advance_to_subjects(page, values, cfg)

        logger.debug("Reached subject category page")
        _select_subjects(page, values.get("subject_level", ""))
        _click_button(page, BTN_SAVE_CONTINUE)

        _enter_declarations(page, values, cfg)
        _click_button(page, BTN_SAVE_CONTINUE)

        logger.info("Reached the declarations step; leaving the browser open for " "review (the remaining steps and the final submit are left to you)")
        # Leave the browser open so you can review/finish by hand.
        hold_open()


# --- monthly subject-category check ---------------------------------------
# The subject menus render only partway through the wizard, so the live tree can
# only be scraped by driving a throwaway draft that far. The monthly GitHub
# Actions check walks one with the dummy data below and refreshes
# nature_categories.json. The draft is clearly titled so a leak reads as a test.
_DUMMY_TITLE = "AUTOMATED CATEGORY CHECK - DO NOT SUBMIT"
_DUMMY_ABSTRACT = "This draft was created by easyeditorial's automated monthly check to refresh " "Nature's stored subject-category list. It is not a real submission and can be " "discarded."
# One dummy author in the Nature authorlist column order
# (title | first | last | email | institution | country | city | corresponding).
_DUMMY_AUTHORS = "| Category | Check | category-check@example.invalid | easyeditorial automated check | United States | | yes"
_DUMMY_VALUES = {
    "title": _DUMMY_TITLE,
    "abstract": _DUMMY_ABSTRACT,
    "authors": _DUMMY_AUTHORS,
    "are_you_corresponding": "yes",
    "cover_letter": "",
    "additional_files": "",
}

# Best-effort draft-delete control candidates; wording varies by deployment.
DISCARD_DELETE_NAMES = ("Remove", "Delete", "Withdraw", "Discard")


def _write_dummy_pdf(path: Path) -> None:
    """Write a minimal one-page PDF (with a valid xref table) for the throwaway
    upload, so eJP accepts it as the Article File. Used only by
    :func:`check_categories` when no real manuscript is supplied.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(pdf)
    count = len(objects) + 1
    pdf += f"xref\n0 {count}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        pdf += f"{off:010d} 00000 n \n".encode()
    pdf += f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    path.write_bytes(pdf)


def _discard_draft(page, title: str, timeout_ms: int = 8000, cfg: Variant = _DEFAULT) -> bool:
    """Best-effort delete of the dummy draft from the eJP portal. Never raises.

    Returns True only when a delete control next to the dummy-titled row was found
    and clicked. eJP deletes via a native ``confirm()`` dialog, so a one-shot
    handler accepts it. A miss leaves the draft in place rather than failing.
    """
    try:
        page.once("dialog", lambda dialog: dialog.accept())
        page.goto(cfg.portal_url)
        page.wait_for_timeout(1500)
        # Scope to the dummy-titled row so a real submission is never touched.
        row = page.locator(f"tr:has-text({title!r}), [role=row]:has-text({title!r})").first
        if row.count() == 0:
            return False
        for name in DISCARD_DELETE_NAMES:
            ctrl = row.get_by_role("link", name=name)
            if ctrl.count() == 0:
                ctrl = row.get_by_role("button", name=name)
            if ctrl.count() > 0:
                ctrl.first.click()
                page.wait_for_timeout(1500)
                return True
        return False
    except Exception as exc:  # noqa: BLE001 -- cleanup must never fail the check
        logger.debug("check_categories: draft discard attempt failed (%s)", exc)
        return False


@dataclass
class CategoryCheckReport:
    """Result of a :func:`check_categories` run.

    ``outcome`` is one of ``"ok"`` (categories unchanged), ``"changed"`` (the
    stored file was refreshed), ``"auth_required"`` (no/expired session), or
    ``"error"`` (the walk failed). ``discarded`` is None when no cleanup was
    attempted, else whether the dummy draft was removed.
    """

    outcome: str
    message: str = ""
    discarded: bool | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in ("ok", "changed")


def check_categories(headless: bool = True, manuscript: str | None = None, discard: bool = True, cfg: Variant = _DEFAULT) -> CategoryCheckReport:
    """Walk a throwaway draft to the subjects step and refresh the category tree.

    Reuses the saved Nature session, walks the wizard with dummy data to the
    subject menus, then runs :func:`verify_nature_categories` (``update=True``) to
    rewrite ``nature_categories.json`` when the live tree differs. Leaves a
    clearly-titled dummy draft; ``discard=True`` best-effort deletes it. A
    throwaway PDF is generated unless ``manuscript`` names a real file. Intended
    for the monthly GitHub Actions check. Returns a report; never raises.
    """
    session = session_path(cfg)
    if not session.exists():
        return CategoryCheckReport(
            outcome="auth_required",
            message=f"No saved {cfg.name} session. Run 'easyeditorial submit' once to " "sign in and store a session, then retry.",
        )

    changed: bool | None = None
    discarded: bool | None = None
    with tempfile.TemporaryDirectory() as tmp:
        if manuscript:
            manuscript_file = manuscript
        else:
            manuscript_file = str(Path(tmp) / "category-check.pdf")
            _write_dummy_pdf(Path(manuscript_file))
        values = dict(_DUMMY_VALUES, manuscript_file=manuscript_file)

        logger.info("check_categories: walking a dummy draft to the subjects step " "(headless=%s, discard=%s)", headless, discard)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context(storage_state=str(session))
            apply_default_timeouts(context)
            page = context.new_page()
            try:
                page.goto(cfg.portal_url)
                if not _venue(cfg).is_logged_in(page):
                    return CategoryCheckReport(
                        outcome="auth_required",
                        message=f"Saved {cfg.name} session looks expired. Re-run 'easyeditorial " f"session {cfg.slug}' to refresh it, then retry.",
                    )
                _advance_to_subjects(page, values, cfg)
                # On the subjects step: update=True returns False only when the
                # tree changed (and was rewritten).
                up_to_date = verify_nature_categories(page, update=True, cfg=cfg)
                changed = not up_to_date
                if discard:
                    discarded = _discard_draft(page, _DUMMY_TITLE, cfg=cfg)
                    logger.info("check_categories: dummy-draft discard succeeded=%s", discarded)
            except Exception as exc:  # noqa: BLE001 -- report rather than crash CI
                logger.warning("check_categories: walk failed (%s)", exc)
                return CategoryCheckReport(outcome="error", message=f"Could not complete the {cfg.name} category check: {exc}", discarded=discarded)
            finally:
                context.close()
                browser.close()

    if changed:
        return CategoryCheckReport(outcome="changed", message=f"{cfg.name} subject categories changed; nature_categories.json was updated.", discarded=discarded)
    return CategoryCheckReport(outcome="ok", message=f"{cfg.name} subject categories are up to date.", discarded=discarded)


class EJPVenue(Venue):
    """A venue on the eJournalPress (eJP) platform, selected by its Variant.

    Nature and its siblings each subclass this and set :attr:`variant`; every
    method threads that variant into the shared module-level engine, so the
    wizard, sign-in, and session logic stay implemented once.
    """

    variant: Variant
    field_validators = FIELD_VALIDATORS
    field_option_listers = FIELD_OPTION_LISTERS
    #: Log-out control marking an authenticated eJP dashboard (see is_logged_in).
    logged_in_names = LOGOUT_LINK_NAMES

    def run(
        self,
        values: dict,
        *,
        headless: bool = False,
        debug: bool = False,
        new_session: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        run_nature(
            values,
            headless=headless,
            debug=debug,
            new_session=new_session,
            timeout=timeout,
            venue=self,
        )

    def login(self, page, username: str, password: str, *, timeout_ms: int = 15000) -> None:
        """Fill and submit the Nature (eJP) sign-in form from stored credentials.

        Dismisses the cookie banner, types the credentials into the (often
        unlabelled) eJP fields, and submits. Raises :class:`NatureLoginError` if a
        field/button can't be found or the dashboard never loads, so
        :meth:`ensure_signed_in` can fall back to a manual sign-in.
        """
        cfg = self.variant
        login_url = cfg.login_url
        logger.debug("Filling the %s sign-in form at %s", cfg.name, login_url)
        page.goto(login_url)
        _dismiss_cookie_banner(page)

        user_box = _find_field(page, LOGIN_USERNAME_LABELS, LOGIN_USERNAME_SELECTORS, timeout_ms)
        pwd_box = _find_field(page, LOGIN_PASSWORD_LABELS, LOGIN_PASSWORD_SELECTORS, timeout_ms)
        if user_box is None or pwd_box is None:
            raise NatureLoginError(
                f"could not find the login/password fields on the {cfg.name} sign-in page "
                "(the eJournalPress form may have changed); re-capture the selectors "
                "with 'playwright codegen %s'" % login_url
            )

        _type_into(user_box, username)
        _type_into(pwd_box, password)

        button = _first_visible(page, "button", LOGIN_BUTTON_NAMES, timeout_ms)
        if button is None:
            # No button matched: press Enter in the password field to submit.
            logger.debug("No login button matched by name; submitting via Enter")
            pwd_box.press("Enter")
        else:
            button.click()

        # A successful sign-in lands on the dashboard; if the log-out control
        # never appears, the sign-in did not take.
        if not self.is_logged_in(page, timeout_ms=timeout_ms):
            raise NatureLoginError(
                f"submitted the credentials but the signed-in {cfg.name} dashboard did not "
                f"load -- the login or password may be wrong, or {cfg.name} added a step "
                "(CAPTCHA / two-factor) that can't be automated"
            )
