"""Sign-in and submission for Science (AAAS).

Science submits through the AAAS Contributor Tracking System (CTS), an Angular
SPA at ``https://cts.sciencemag.org/scc/`` whose controls are addressed by their
visible names. Selectors were captured with ``playwright codegen``; re-capture
the same way if AAAS restyles the portal. The wizard is driven best-effort (each
step goes through :func:`_try`), stops before the final submit, and leaves the
browser open via :func:`hold_open`.
"""

from __future__ import annotations

import logging

from playwright.sync_api import sync_playwright

from ...database import get_venue
from ...validate import parse_authors
from ..base import Venue
from ..common import (DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts,
                      hold_open, open_run_context)
from ..login import VenueLoginError

logger = logging.getLogger(__name__)

# --- sign-in form selectors (CTS login view). Names match as substrings. --
LOGIN_USERNAME_NAME = "Username/Email Address"
LOGIN_PASSWORD_NAME = "Password"  # form field name, not a credential  # nosec B105
LOGIN_BUTTON_NAME = "Sign In"
# CTS may pop a session/notice dialog after sign-in; dismiss it (harmless if absent).
POST_LOGIN_DISMISS_NAMES = ("Cancel", "Close", "OK")
# Signed-in signals: the dashboard's "New Submission" action, else a sign-out link.
LINK_NEW_SUBMISSION = "New Submission"
LOGOUT_LINK_NAMES = ("Sign Out", "Sign out", "Log Out", "Logout", "Log out")

# --- submission wizard controls, addressed by visible name (substring match) --
BTN_SAVE_PROCEED = "Save and Proceed"
BTN_CONTINUE_TERMS = "Continue To Terms"
BTN_SAVE_CONTINUE = "Save And Continue To Submission"
BTN_UPLOAD_FILE = "Upload File"
BTN_CHOOSE_FILE = "Choose File"
BTN_SAVE = "Save"
BTN_PROCEED = "Proceed"
BTN_OK = "OK"
BTN_ADD_INSTITUTION = "Add Institution"
BTN_SEARCH = "Search"
BTN_SELECT = "Select"
BTN_ADD_AUTHOR = "Add Author"
BTN_ADD_ADDITIONAL_INSTITUTION = "Select Additional Institution"
BTN_ADD_FUNDER = "Add A Funding Source"
BTN_ADD_SUBJECT = "Add"

# Add Author flag checkboxes, keyed by the flag id in the wrapper's ``change`` attr.
FLAG_CO_FIRST_AUTHOR = "co-first-author"
FLAG_CORRESPONDING_AUTHOR = "corresponding-author"
FLAG_EQUAL_CONTRIBUTOR = "equal-contributor"

# Add Author dialog boxes and the institution-lookup country/file controls.
SEL_AUTHOR_FIRST = "input[name='firstName']"
SEL_AUTHOR_LAST = "input[name='lastName']"
SEL_AUTHOR_EMAIL = "input[name='email']"
SEL_COUNTRY = "#country"
SEL_FILE_INPUT = "input[type='file']"

# Manuscript-information title/abstract: CTS exposes no stable names, so try candidates.
SEL_TITLE_CANDIDATES = ("textarea[name='title']", "input[name='title']", "#title")
SEL_ABSTRACT_CANDIDATES = ("textarea[name='abstract']", "#abstract")

# Funding step: funder-name typeahead (Crossref Funder Registry), award box,
# investigator drop-down, and the typeahead popup.
SEL_FUNDER_NAME = "input[name='funder']"
SEL_FUNDER_AWARD = "input[ng-model='value']:visible"
SEL_FUNDER_INVESTIGATOR = "select[ng-model='obj.selected']"
SEL_TYPEAHEAD_MATCH = ".dropdown-menu li a, .dropdown-menu a"

# Subject-area step: ui-select toggle and its choices rows (two pickers by index).
SEL_SUBJECT_TOGGLE = ".ui-select-toggle"
SEL_SUBJECT_CHOICE = ".ui-select-choices-row"

# Additional-datasets step: repository drop-down, URI box, and the add button.
SEL_DATASET_REPO = "select[ng-model='obj.selected']"
SEL_DATASET_URI = "input[name='dataset-uri']"
BTN_ADD_DATASET = "button[ng-click='vm.addRepository()']"

# Reviewers step: forms whose first/last/email have stable names; optional boxes
# use an empty ``name`` and the excluded form's reason is the lone textarea.
BTN_ADD_SUGGESTED_REVIEWER = "Add Suggested Reviewers"
BTN_ADD_EXCLUDED_REVIEWER = "Add Excluded Reviewer"
SEL_REVIEWER_OPTIONAL = "input[ng-model='value'][name='']"
SEL_REVIEWER_TEXTAREA = "textarea[ng-model='value']"

# Common country abbreviations normalized to the CTS drop-down's full names.
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

_YES = {"yes", "y", "true", "1", "on"}


class ScienceLoginError(VenueLoginError):
    """Raised when an automatic Science (CTS) sign-in cannot be completed."""


# --- small helpers --------------------------------------------------------


def _is_yes(value: str) -> bool:
    """True when a ``.sub`` value reads as an affirmative (yes/true/1/on)."""
    return str(value or "").strip().lower() in _YES


def _try(fn, what: str) -> bool:
    """Run ``fn``; log and continue if it fails (for optional/account-dependent steps)."""
    try:
        fn()
        return True
    except Exception as exc:  # noqa: BLE001 -- best-effort optional step
        logger.warning("Science: skipped %s (%s)", what, exc)
        return False


def _click(page, name: str, exact: bool = False, settle_ms: int = 1200) -> None:
    """Click a CTS button by visible name and pause for the SPA view to settle.

    ``exact`` guards short names (e.g. "Save", "OK") from substring-matching a
    longer button on the same view.
    """
    page.get_by_role("button", name=name, exact=exact).first.click()
    page.wait_for_timeout(settle_ms)


def _click_link(page, name: str, settle_ms: int = 1500) -> None:
    """Click a CTS link by its visible name and let the SPA view settle."""
    page.get_by_role("link", name=name).first.click()
    page.wait_for_timeout(settle_ms)


def _agree_to_terms(page, settle_ms: int = 800) -> None:
    """Tick the terms agreement by clicking its styled label (the real input is hidden)."""
    page.locator("label").first.click()
    page.wait_for_timeout(settle_ms)


def _country_label(country: str) -> str:
    """Map a country name to the label shown in the CTS country drop-down."""
    value = (country or "").strip()
    return _COUNTRY_ALIASES.get(value.upper(), value)


def _fill_first(page, selectors, value: str, what: str) -> bool:
    """Fill ``value`` into the first present selector from ``selectors`` (blank is a no-op)."""
    if not value:
        return False
    for selector in selectors:
        box = page.locator(selector)
        if box.count():
            return _try(lambda b=box: b.first.fill(value), f"{what} ({selector})")
    logger.warning("Science: no box found for %s; skipping", what)
    return False


# --- .sub parsing (reuses the validate helpers) ---------------------------


def _parse_authors(value: str) -> list[dict]:
    """Parse the author block using the Science ``authorlist`` column set from the database."""
    author_field = next((f for f in ScienceVenue._VENUE.fields if f.type == "authorlist"), None)
    return parse_authors(value, author_field.fields if author_field else None)


def _parse_files(value: str) -> list[tuple[str, str]]:
    """Parse a file-list block into ``(path, second)`` tuples; lines without a path are skipped."""
    files: list[tuple[str, str]] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        path = parts[0].strip()
        file_type = parts[1].strip() if len(parts) > 1 else ""
        if path:
            files.append((path, file_type))
    return files


def _parse_funders(value: str) -> list[dict]:
    """Parse funding lines ``funder | award | investigator`` (last two optional; no name skips)."""
    funders: list[dict] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        name = parts[0] if parts else ""
        if not name:
            continue
        funders.append(
            {
                "funder": name,
                "award_number": parts[1] if len(parts) > 1 else "",
                "investigator": parts[2] if len(parts) > 2 else "",
            }
        )
    return funders


def _parse_subject_areas(value: str) -> list[str]:
    """Parse the comma-separated ``multichoice`` subject-area value into area names."""
    return [area.strip() for area in value.split(",") if area.strip()]


def _parse_datasets(value: str) -> list[dict]:
    """Parse dataset lines ``repository | URI`` (URI optional; no repository skips)."""
    datasets: list[dict] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        repository = parts[0] if parts else ""
        if not repository:
            continue
        datasets.append({"repository": repository, "uri": parts[1] if len(parts) > 1 else ""})
    return datasets


def _parse_reviewers(value: str) -> list[dict]:
    """Parse reviewer lines ``first | last | email | institution | extra`` (last two optional).

    ``extra`` is a department (suggested) or exclusion reason (excluded). Lines
    with no name and no email are skipped.
    """
    reviewers: list[dict] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        reviewer = {
            "first_name": parts[0] if len(parts) > 0 else "",
            "last_name": parts[1] if len(parts) > 1 else "",
            "email": parts[2] if len(parts) > 2 else "",
            "institution": parts[3] if len(parts) > 3 else "",
            "extra": parts[4] if len(parts) > 4 else "",
        }
        if reviewer["first_name"] or reviewer["last_name"] or reviewer["email"]:
            reviewers.append(reviewer)
    return reviewers


# --- sign-in --------------------------------------------------------------


def _dismiss_post_login_dialog(page, timeout_ms: int = 4000) -> bool:
    """Close the Cancel/Close/OK dialog CTS may show after sign-in (missing is not an error)."""
    for name in POST_LOGIN_DISMISS_NAMES:
        button = page.get_by_role("button", name=name, exact=True)
        try:
            if button.first.is_visible():
                logger.debug("Science: dismissing post-login dialog via %r", name)
                button.first.click()
                page.wait_for_timeout(500)
                return True
        except Exception:  # noqa: BLE001 -- a missing candidate is just "not present"
            continue
    return False


# --- submission wizard ----------------------------------------------------


def _select_publication(page, publication: str) -> None:
    """Choose the AAAS venue (first combobox) by label; leave the default if unmatched."""
    combo = page.get_by_role("combobox").first
    if combo.count() == 0:
        logger.warning("Science: no publication menu on the New Submission screen")
        return
    if _try(lambda: combo.select_option(label=publication), f"publication {publication!r}"):
        page.wait_for_timeout(800)
        return
    logger.warning("Science: publication %r did not match a menu option; leaving the default", publication)


def _select_article_type(page, article_type: str) -> None:
    """Choose the submission type (second combobox) by label; leave the default if unmatched."""
    combo = page.get_by_role("combobox").nth(1)
    if combo.count() == 0:
        logger.warning("Science: no article-type menu on the New Submission screen")
        return
    if article_type:
        if _try(lambda: combo.select_option(label=article_type), f"article type {article_type!r}"):
            return
        logger.warning("Science: article type %r did not match a menu option; leaving the default", article_type)


def _attach_file(page, path: str) -> bool:
    """Attach ``path`` via the hidden ``<input type="file">``, else the native file chooser."""
    file_input = page.locator(SEL_FILE_INPUT)
    if file_input.count():
        return _try(lambda: file_input.last.set_input_files(path), f"attach {path}")
    try:
        with page.expect_file_chooser() as fc:
            page.get_by_role("button", name=BTN_CHOOSE_FILE).first.click()
        fc.value.set_input_files(path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Science: could not attach %s (%s)", path, exc)
        return False


def _set_file_type(page, file_type: str) -> None:
    """Set the just-attached file's type via the last combobox (blank leaves the default)."""
    if not file_type:
        return
    combo = page.get_by_role("combobox").last
    _try(lambda: combo.select_option(label=file_type), f"file type {file_type!r}")


def _upload_file(page, path: str, file_type: str) -> None:
    """Attach one file on the reusable CTS upload dialog, set its type, and save it."""
    logger.info("Uploading %s as %r", path, file_type or "(default type)")
    _try(lambda: _click(page, BTN_UPLOAD_FILE, settle_ms=800), "open upload dialog")
    if not _attach_file(page, path):
        return
    # Let the upload progress bar finish before the type is set and saved.
    page.wait_for_timeout(3000)
    _try(lambda: _click(page, BTN_SAVE, exact=True), "save uploaded file")
    page.wait_for_timeout(3000)


def _upload_manuscript_files(page, word_path: str, combined_pdf: str) -> None:
    """Attach and save the two manuscript-step files: the Word/source, then the combined PDF."""
    inputs = page.locator(SEL_FILE_INPUT)
    upload_buttons = page.get_by_role("button", name=BTN_UPLOAD_FILE)
    logger.info("Uploading manuscript (Word/source) %s", word_path)
    _try(lambda: upload_buttons.first.click(), "open Word/source upload slot")
    if _try(lambda: inputs.nth(0).set_input_files(word_path), f"attach manuscript {word_path}"):
        page.wait_for_timeout(2000)  # let the upload progress bar finish
        _try(lambda: _click(page, BTN_SAVE, exact=True), "save manuscript (Word/source)")
    page.wait_for_timeout(1000)
    logger.info("Uploading combined PDF %s", combined_pdf)
    _try(lambda: upload_buttons.first.click(), "open combined PDF upload slot")
    if _try(lambda: inputs.first.set_input_files(combined_pdf), f"attach combined pdf {combined_pdf}"):
        page.wait_for_timeout(2000)  # let the upload progress bar finish
        _try(lambda: _click(page, BTN_SAVE, exact=True), "save combined PDF")
    _try(lambda: _click(page, BTN_PROCEED), "proceed past file upload")


def _select_org(page, org_id: str) -> str:
    """Click the institution result whose id span matches ``org_id`` (else the first).

    Returns the selected organization's name (for matching later in the
    additional-institution picker), or "" if nothing was selected.
    """
    name_link = "[ng-click='vm.selectOrg(item)']"
    try:
        matched = page.locator(f"xpath=//span[contains(normalize-space(.), '({org_id})')]" f"/ancestor-or-self::*[.//*[@ng-click='vm.selectOrg(item)']][1]" f"//*[@ng-click='vm.selectOrg(item)']").first
        target = matched if matched.count() else page.locator(name_link).first
        if not target.count():
            logger.warning("Science: no institution result to select for id %s", org_id)
            return ""
        name = page.locator(f"xpath=//span[normalize-space()='({org_id})']" f"/ancestor::li[1]//div[@ng-click='vm.selectOrg(item)']").first.inner_text().strip()
        target.click()
    except Exception as exc:  # noqa: BLE001 -- best-effort optional step
        logger.warning("Science: skipped select organization %s (%s)", org_id, exc)
        return ""
    page.wait_for_timeout(600)
    return name


def _add_institution(page, ringgold_id: str = "", ror_id: str = "") -> str:
    """Add one affiliation via the CTS institution lookup (Ringgold preferred, ROR fallback).

    The dialog's search boxes are addressed by position (Ringgold then ROR).
    Returns the resolved institution name (for the additional-institution
    picker), or "" if nothing was selected.
    """
    if not ringgold_id and not ror_id:
        logger.warning("Science: no institution ID given; skipping affiliation")
        return ""

    logger.info("Adding institution (ringgold=%s, ror=%s)", ringgold_id, ror_id)
    if not _try(lambda: _click(page, BTN_ADD_INSTITUTION), "open Add Institution dialog"):
        return ""

    # Ringgold-ID box first, ROR-ID box next (addressed by position).
    boxes = page.get_by_role("textbox")
    if ringgold_id:
        _try(lambda: boxes.nth(0).fill(ringgold_id), "ringgold_id")
    if ror_id:
        _try(lambda: boxes.nth(1).fill(ror_id), "ror_id")

    _try(lambda: _click(page, BTN_SEARCH, exact=True, settle_ms=2500), "run institution search")

    # Ringgold and ROR are two slots for the same org; keep whichever name comes back.
    name = ""
    if ringgold_id:
        name = _select_org(page, ringgold_id)
    if ror_id:
        name_ror = _select_org(page, ror_id) or name
        name = name if name else name_ror
    _try(lambda: _click(page, BTN_SELECT, exact=True), "link selected organization")
    _try(lambda: _click(page, BTN_SAVE, exact=True), "save institution affiliation")
    return name


def _check_author_flag(page, flag: str, what: str) -> None:
    """Tick one Add Author flag checkbox, selected by its ``flag`` id in the wrapper's ``change`` attr."""
    span = page.locator(f"div.dpp-white-checkbox[change*=\"'{flag}'\"] span[ng-click='onCheckBoxChange()']").first
    _try(lambda: span.click(), what)


def _pick_institution(page, name: str) -> bool:
    """Check the additional-institution ``div.text-bold`` row whose exact text matches ``name``."""
    row = page.locator("div.text-bold").get_by_text(name, exact=True).first
    if not row.count():
        logger.warning("Science: additional institution %r not offered in the picker", name)
        return False
    return _try(lambda: row.click(), f"pick additional institution {name}")


def _add_author(page, author: dict, inst_names: dict | None = None) -> None:
    """Add one author via the CTS Add Author dialog: names, email, flags, and affiliation.

    ``inst_names`` maps a Ringgold/ROR id to the institution name resolved by
    :func:`_add_institution`, used to pick the author's row in the picker.
    """
    inst_names = inst_names or {}
    first = author.get("first_name", "").strip()
    last = author.get("last_name", "").strip()
    if not first and not last:
        # Fall back to splitting a single "name" column on the last space.
        whole = (author.get("name") or "").strip()
        first, _, last = whole.rpartition(" ") if " " in whole else (whole, "", "")
    logger.info("Adding author %s %s", first, last)

    if not _try(lambda: _click(page, BTN_ADD_AUTHOR), "open Add Author dialog"):
        return

    _try(lambda: page.locator(SEL_AUTHOR_FIRST).fill(first), "author first name")
    _try(lambda: page.locator(SEL_AUTHOR_LAST).fill(last), "author last name")
    _try(lambda: page.locator(SEL_AUTHOR_EMAIL).fill(author["email"]), "author email")

    if _is_yes(author.get("first_author")):
        _check_author_flag(page, FLAG_CO_FIRST_AUTHOR, "mark co-first author")
    if _is_yes(author.get("corresponding")):
        _check_author_flag(page, FLAG_CORRESPONDING_AUTHOR, "mark corresponding author")
    if _is_yes(author.get("contributed_equally")):
        _check_author_flag(page, FLAG_EQUAL_CONTRIBUTOR, "mark equal contributor")

    # Assign the affiliation via the additional-institution picker, when shown.
    if page.get_by_role("button", name=BTN_ADD_ADDITIONAL_INSTITUTION).count():
        _try(lambda: _click(page, BTN_ADD_ADDITIONAL_INSTITUTION, settle_ms=800), "open additional-institution picker")
        institution = inst_names.get((author.get("ringgold_id") or "").strip()) or inst_names.get((author.get("ror_id") or "").strip()) or ""
        if institution:
            page.locator("li").filter(has_text=institution).locator(".org-select-box").click()

    _try(lambda: _click(page, "Finish"), "Add institution")
    _try(lambda: _click(page, BTN_SAVE), "Save author")


def _select_first_funder_match(page, settle_ms: int = 1000) -> None:
    """Click the first suggestion in the Funder Registry typeahead popup."""
    page.wait_for_timeout(settle_ms)
    page.locator(SEL_TYPEAHEAD_MATCH).first.click()


def _add_funding(page, funders: list[dict]) -> None:
    """Add each funding source: funder (via typeahead), award number, and investigator."""
    for funder in funders:
        name = funder.get("funder", "").strip()
        if not name:
            continue
        award = funder.get("award_number", "").strip()
        investigator = funder.get("investigator", "").strip()
        logger.info("Adding funding source %s (award %s)", name, award or "(none)")
        if not _try(lambda: _click(page, BTN_ADD_FUNDER), "open Add Funding Source"):
            continue
        # Type the funder name (>= 3 chars) and take the first Funder Registry suggestion.
        if _try(lambda: page.locator(SEL_FUNDER_NAME).first.fill(name), "funder name"):
            _try(lambda: _select_first_funder_match(page), f"select funder {name!r}")
        if award:
            _try(lambda: page.locator(SEL_FUNDER_AWARD).first.fill(award), "award number")
        if investigator:
            _try(
                lambda: page.locator(SEL_FUNDER_INVESTIGATOR).first.select_option(label=investigator),
                f"investigator {investigator!r}",
            )
        _try(lambda: _click(page, BTN_SAVE), "Award")


def _add_subject_areas(page, subject_areas: list[str]) -> None:
    """Add up to two subject areas: open each ui-select picker, click the match, then "Add"."""
    for index, area in enumerate(subject_areas[:2]):
        area = area.strip()
        if not area:
            continue
        logger.info("Adding subject area %s", area)
        toggle = page.locator(SEL_SUBJECT_TOGGLE).nth(index + 1)  # the first toggle is the desired editor
        if not _try(lambda t=toggle: t.click(), f"open subject-area picker {index + 2}"):
            continue
        page.wait_for_timeout(500)
        choice = page.locator(SEL_SUBJECT_CHOICE).filter(has=page.get_by_text(area, exact=True))
        _try(lambda c=choice: c.first.click(), f"choose subject area {area!r}")
        add_button = page.get_by_role("button", name=BTN_ADD_SUBJECT, exact=True).nth(index)
        _try(lambda b=add_button: b.click(), f"add subject area {area!r}")
        page.wait_for_timeout(800)


def _add_datasets(page, datasets: list[dict]) -> None:
    """Add each additional dataset: choose the repository, type the URI, click "Add"."""
    for dataset in datasets:
        repository = dataset.get("repository", "").strip()
        uri = dataset.get("uri", "").strip()
        if not repository and not uri:
            continue
        logger.info("Adding dataset %s (%s)", repository or "(no repository)", uri or "(no URI)")
        if repository:
            _try(
                lambda r=repository: page.locator(SEL_DATASET_REPO).last.select_option(label=r),
                f"dataset repository {repository!r}",
            )
        if uri:
            _try(lambda u=uri: page.locator(SEL_DATASET_URI).last.fill(u), "dataset URI")
        _try(lambda: page.locator(BTN_ADD_DATASET).first.click(), "Add dataset")
        page.wait_for_timeout(800)


def _add_reviewers(page, reviewers: list[dict], add_button: str, what: str, extra_in_textarea: bool = False) -> None:
    """Add suggested or excluded reviewers, one form per reviewer opened by ``add_button``.

    ``extra`` goes to a second optional box (department) or, when
    ``extra_in_textarea``, the excluded form's free-text reason textarea.
    """
    for reviewer in reviewers:
        first = reviewer.get("first_name", "").strip()
        last = reviewer.get("last_name", "").strip()
        email = reviewer.get("email", "").strip()
        if not (first or last or email):
            continue
        institution = reviewer.get("institution", "").strip()
        extra = reviewer.get("extra", "").strip()
        logger.info("Adding %s %s %s", what, first, last)
        if not _try(lambda: _click(page, add_button), f"open {what} form"):
            continue
        _try(lambda v=first: page.locator(SEL_AUTHOR_FIRST).last.fill(v), f"{what} first name")
        _try(lambda v=last: page.locator(SEL_AUTHOR_LAST).last.fill(v), f"{what} last name")
        _try(lambda v=email: page.locator(SEL_AUTHOR_EMAIL).last.fill(v), f"{what} email")
        if institution:
            _try(
                lambda v=institution: page.locator(SEL_REVIEWER_OPTIONAL).first.fill(v),
                f"{what} institution",
            )
        if extra:
            if extra_in_textarea:
                _try(lambda v=extra: page.locator(SEL_REVIEWER_TEXTAREA).last.fill(v), f"{what} reason")
            else:
                _try(lambda v=extra: page.locator(SEL_REVIEWER_OPTIONAL).nth(1).fill(v), f"{what} department")
        _try(lambda: _click(page, BTN_SAVE), f"save {what}")

    _try(lambda: _click(page, BTN_PROCEED), f"proceed with Reviewers")


def _enter_manuscript_info(
    page,
    title: str,
    abstract: str,
    funders: list[dict] | None = None,
    subject_areas: list[str] | None = None,
    datasets: list[dict] | None = None,
) -> None:
    """Fill the manuscript-information step: title, abstract, funding, subject areas, datasets."""
    _fill_first(page, SEL_TITLE_CANDIDATES, title, "title")
    _fill_first(page, SEL_ABSTRACT_CANDIDATES, abstract, "abstract")
    _add_funding(page, funders or [])
    _add_subject_areas(page, subject_areas or [])
    _try(lambda: _click(page, BTN_SAVE_PROCEED), "save and proceed")
    _add_datasets(page, datasets or [])
    _try(lambda: _click(page, BTN_PROCEED), "proceed")


def run_science(values: dict, headless: bool = False, debug: bool = False, new_session: bool = False, timeout: float = DEFAULT_TIMEOUT_SECONDS, *, venue) -> None:
    """Open Science, sign in, then drive the CTS submission wizard from a ``.sub``.

    Signs in via ``venue.ensure_signed_in``, then clicks through the captured CTS
    steps (venue/article type, terms, file uploads, institutions, authors,
    manuscript info, reviewers). ``new_session=True`` discards any saved session;
    ``debug=True`` forces a headed browser and opens the Inspector. Every action
    is best-effort; the run stops before the final submit and leaves the browser
    open via :func:`hold_open`.
    """
    # The Inspector needs a visible browser; debugging headless makes no sense.
    if debug:
        headless = False

    publication = values.get("publication", "").strip()
    article_type = values.get("article_type", "").strip()
    title = values.get("title", "")
    abstract = values.get("abstract", "")
    manuscript_file = values.get("manuscript_file", "").strip()
    combined_pdf = values.get("combined_pdf", "").strip()
    cover_letter = values.get("cover_letter", "").strip()
    supplementary_material = values.get("supplementary_material", "").strip()
    authors = _parse_authors(values.get("authors", ""))
    funders = _parse_funders(values.get("funders", ""))
    subject_areas = _parse_subject_areas(values.get("subject_areas", ""))
    datasets = _parse_datasets(values.get("additional_datasets", ""))
    suggested_reviewers = _parse_reviewers(values.get("suggested_reviewers", ""))
    excluded_reviewers = _parse_reviewers(values.get("excluded_reviewers", ""))

    logger.info(
        "Starting Science submission run (headless=%s, debug=%s): manuscript=%s, %d author(s)",
        headless,
        debug,
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

        logger.debug("Signed in; starting a new manuscript submission")
        new_link = page.get_by_role("link", name=LINK_NEW_SUBMISSION)
        if not new_link.first.is_visible():
            page.goto(venue.PORTAL_URL)
        new_link.first.wait_for(state="visible", timeout=30000)
        _click_link(page, LINK_NEW_SUBMISSION)

        # Choose the AAAS venue first (it repopulates the article-type menu), then the type.
        _select_publication(page, publication)
        _select_article_type(page, article_type)
        _try(lambda: _click(page, BTN_SAVE_PROCEED), "save and proceed past article type")

        # Terms: tick the agreement checkbox (its ng-disabled binding gates
        # "Save And Continue To Submission") and leave it checked, then continue.
        _try(lambda: _click(page, BTN_CONTINUE_TERMS), "continue to terms")
        if _is_yes(values.get("agree_terms", "")):
            _try(lambda: _agree_to_terms(page), "agree to terms (checkbox)")
        _try(lambda: _click(page, BTN_SAVE_CONTINUE), "save and continue past terms")

        # File-upload step: the two manuscript slots, then an optional cover letter/supplement.
        _upload_manuscript_files(page, manuscript_file, combined_pdf)
        if cover_letter:
            _upload_file(page, cover_letter, "Cover Letter")
        if supplementary_material:
            _upload_file(page, supplementary_material, "Supplementary Material")
        _try(lambda: _click(page, BTN_PROCEED), "proceed past file upload")
        _try(lambda: _click(page, BTN_OK, exact=True), "confirm file upload")

        # Authors step: add each distinct affiliation (deduped by Ringgold/ROR id), then authors.
        seen_institutions: set[tuple[str, str]] = set()
        inst_names: dict[str, str] = {}
        for author in authors:
            ringgold = (author.get("ringgold_id") or "").strip()
            ror = (author.get("ror_id") or "").strip()
            key = (ringgold, ror)
            if (ringgold or ror) and key not in seen_institutions:
                seen_institutions.add(key)
                name = _add_institution(page, ringgold, ror)
                # Map both ids to the resolved name for the picker later.
                if ringgold:
                    inst_names[ringgold] = name
                if ror:
                    inst_names[ror] = name
        for author in authors:
            submitter = _is_yes(author.get("submitter"))
            if submitter:
                continue  # the submitting author is already present on the step
            _add_author(page, author, inst_names)

        # add institution for submitting author
        for author_pos, author in enumerate(authors):
            submitter = _is_yes(author.get("submitter"))
            if not submitter:
                continue
            page.locator("a[ng-click='grid.appScope.updateAuthor(row.entity)']").first.click()
            if not _is_yes(author.get("first_author")):  # defaults first author
                _check_author_flag(page, FLAG_CO_FIRST_AUTHOR, "mark co-first author")
            if not _is_yes(author.get("corresponding")):  # defaults corresponding author
                _check_author_flag(page, FLAG_CORRESPONDING_AUTHOR, "mark corresponding author")
            if _is_yes(author.get("contributed_equally")):
                _check_author_flag(page, FLAG_EQUAL_CONTRIBUTOR, "mark equal contributor")

            if page.get_by_role("button", name=BTN_ADD_ADDITIONAL_INSTITUTION).count():
                _try(lambda: _click(page, BTN_ADD_ADDITIONAL_INSTITUTION, settle_ms=800), "open additional-institution picker")
                institution = inst_names.get((author.get("ringgold_id") or "").strip()) or inst_names.get((author.get("ror_id") or "").strip()) or ""
                if institution:
                    page.locator("li").filter(has_text=institution).locator(".org-select-box").click()

            _try(lambda: _click(page, "Finish"), "finish Add Institution")
            page.locator("span[ng-click='closePopup()']").click()

            if author_pos != 0:  # if submitting author is not first, then move them down
                page.locator("span[ng-click='grid.appScope.prepareOrderPopovers(row.entity.authorId)']").first.click()
                for i in range(author_pos):
                    page.locator("button[ng-click=\"grid.appScope.setOrderDirection(row.entity,'down')\"]").first.click()

        _try(lambda: _click(page, BTN_PROCEED), "proceed past authors")

        # Manuscript-information step: title, abstract, funding, subject areas, datasets.
        _enter_manuscript_info(page, title, abstract, funders, subject_areas, datasets)

        # Reviewers step: add the suggested reviewers, then the excluded ones.
        _add_reviewers(page, suggested_reviewers, BTN_ADD_SUGGESTED_REVIEWER, "suggested reviewer")
        page.wait_for_timeout(1000)
        _add_reviewers(page, excluded_reviewers, BTN_ADD_EXCLUDED_REVIEWER, "excluded reviewer", extra_in_textarea=True)

        logger.info("Reached the reviewers step; leaving the browser open " "for review (the remaining steps and the final submit are left to you)")
        hold_open()


class ScienceVenue(Venue):
    """The Science venue (AAAS CTS wizard), shared by the whole AAAS family.

    The differing publication is carried in the ``.sub`` rather than the code.
    """

    slug = "science"

    _VENUE = get_venue("science")
    SITE_URL = _VENUE.site_url
    PORTAL_URL = _VENUE.submission_url  # inherited portal_url resolves to this
    LOGIN_URL = PORTAL_URL.rstrip("/") + "/#/login"  # CTS sign-in view (#/login fragment)

    # Signed-in signals: the "New Submission" action, else a sign-out link.
    logged_in_names = (LINK_NEW_SUBMISSION, *LOGOUT_LINK_NAMES)
    # CTS is a slow-loading SPA, so the login-state check waits a little longer.
    logged_in_timeout_ms = 8000

    @property
    def login_url(self) -> str:
        return self.LOGIN_URL

    def run(
        self,
        values: dict,
        *,
        headless: bool = False,
        debug: bool = False,
        new_session: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        run_science(
            values,
            headless=headless,
            debug=debug,
            new_session=new_session,
            timeout=timeout,
            venue=self,
        )

    def login(self, page, username: str, password: str, *, timeout_ms: int = 20000) -> None:
        """Fill and submit the Science (CTS) sign-in form; raise ScienceLoginError on failure."""
        logger.debug("Filling the Science sign-in form at %s", self.LOGIN_URL)
        page.goto(self.LOGIN_URL)

        user_box = page.get_by_role("textbox", name=LOGIN_USERNAME_NAME)
        pwd_box = page.get_by_role("textbox", name=LOGIN_PASSWORD_NAME)
        try:
            user_box.first.wait_for(state="visible", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise ScienceLoginError("could not find the username field on the Science sign-in page " "(the CTS login view may have changed); re-capture the selectors " f"with 'playwright codegen {self.LOGIN_URL}'") from exc

        user_box.first.fill(username)
        pwd_box.first.fill(password)

        button = page.get_by_role("button", name=LOGIN_BUTTON_NAME)
        if button.count():
            button.first.click()
        else:
            # Fall back to submitting from the password field.
            logger.debug("No Sign In button matched; submitting via Enter")
            pwd_box.first.press("Enter")

        _dismiss_post_login_dialog(page)

        if not self.is_logged_in(page, timeout_ms=timeout_ms):
            raise ScienceLoginError("submitted the credentials but the signed-in Science dashboard did " "not load -- the login or password may be wrong, or CTS added a step " "(CAPTCHA / two-factor) that can't be automated")


VENUE = ScienceVenue()
