"""Aries Editorial Manager submission engine (Playwright).

The Cell Press family (Cell, Cell Systems, Cell Genomics) and PLOS Computational
Biology all submit through the Editorial Manager wizard at
``editorialmanager.com/<slug>``. The wizard is implemented once here and
parameterized by a :class:`Variant`; each venue is a thin per-slug binding module
that selects its Variant. :func:`run` opens a browser, drives the wizard from a
parsed ``.sub``, and leaves the window open via :func:`hold_open` without ever
clicking the final submit/build-PDF step.

The whole wizard lives inside ``iframe[name="content"]`` (see :func:`_content`);
the declarations page drifts between deployments, so each control there is
clicked through :func:`_try`, which logs and continues rather than aborting.
Re-capture selectors with ``playwright codegen
https://www.editorialmanager.com/<slug>/`` if a venue restyles the wizard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect, sync_playwright

from ... import credentials
from ...database import get_venue
from ...validate import parse_authors
from ..base import Venue
from ..common import (DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts,
                      hold_open, open_run_context)
from ..common import parse_pipe_funders as _parse_funders
from ..common import save_storage
from ..common import split_name_first_last as _split_name
from ..common import wait_for_human
from ..login import VenueLoginError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Variant:
    """Per-venue configuration for one Editorial Manager deployment.

    Each toggle gates one control that drifts between deployments:
    ``cover_letter_label`` (the cover-letter item-type label), the
    ``ask_*`` radios on the declarations page, ``alternate_contact_mode``
    (``"radio_textbox"`` / ``"textbox"`` / ``"single_field"``),
    ``has_comments_page``, ``open_access_only``, ``has_section_classifications``
    (PLOS Section/Category + Classifications step), ``annotate_manuscript_manually``,
    and ``plos_declarations`` (PLOS's separate ``QR23_1_Q*`` question set, driving
    :func:`_answer_declarations_plos` instead of :func:`_answer_declarations`).
    """

    slug: str
    name: str
    cover_letter_label: str = "*Cover letter"
    ask_previous_version: bool = True
    ask_transparent_peer_review: bool = False
    ask_publish_review: bool = True
    alternate_contact_mode: str = "radio_textbox"
    has_comments_page: bool = False
    open_access_only: bool = False
    has_section_classifications: bool = False
    annotate_manuscript_manually: bool = False
    plos_declarations: bool = False

    @property
    def venue(self):
        """The database entry (URLs, author columns) for this venue."""
        return get_venue(self.slug)

    @property
    def portal_url(self) -> str:
        """The Editorial Manager submission-system entry point."""
        return self.venue.submission_url

    @property
    def login_url(self) -> str:
        """The sign-in page (same as the portal entry point)."""
        return self.portal_url


VARIANTS = {
    "cell": Variant("cell", "Cell"),
    "cell_systems": Variant(
        "cell_systems",
        "Cell Systems",
        cover_letter_label="*Cover Letter",
        ask_previous_version=False,
        ask_transparent_peer_review=True,
        ask_publish_review=False,
        alternate_contact_mode="single_field",
        has_comments_page=True,
    ),
    "cell_genomics": Variant(
        "cell_genomics",
        "Cell Genomics",
        cover_letter_label="*Cover Letter",
        ask_transparent_peer_review=True,
        ask_publish_review=False,
        alternate_contact_mode="textbox",
        open_access_only=True,
    ),
    "plos_compbio": Variant(
        "plos_compbio",
        "PLOS Computational Biology",
        cover_letter_label="*Cover Letter",
        annotate_manuscript_manually=True,
        has_section_classifications=True,
        plos_declarations=True,
    ),
}

# Default variant, used when a caller does not pass one.
_DEFAULT = VARIANTS["cell"]

# The content iframe that hosts the whole Editorial Manager wizard.
CONTENT_FRAME = 'iframe[name="content"]'

# The credential form: directly in the content frame (Cell) or one level deeper
# in this iframe (PLOS).
LOGIN_FRAME = 'iframe[name="login"]'

# The author-area link that only renders once signed in.
SUBMIT_NEW_LINK = "Submit New Manuscript"

# Normalize spellings/abbreviations to the label shown in the "Country or Region"
# drop-down; anything not listed falls through to the stripped input unchanged.
_COUNTRY_ALIASES = {"UNITED STA`TES OF AMERICA": "us", "UNITED STATES": "us", "USA": "us", "AFGHANISTAN": "af", "ÅLAND ISLANDS": "ax", "ALBANIA": "al", "ALGERIA": "dz", "AMERICAN SAMOA": "as", "ANDORRA": "ad", "ANGOLA": "ao", "ANGUILLA": "ai", "ANTARCTICA": "aq", "ANTIGUA AND BARBUDA": "ag", "ARGENTINA": "ar", "ARMENIA": "am", "ARUBA": "aw", "AUSTRALIA": "au", "AUSTRIA": "at", "AZERBAIJAN": "az", "BAHAMAS": "bs", "BAHRAIN": "bh", "BANGLADESH": "bd", "BARBADOS": "bb", "BELARUS": "by", "BELGIUM": "be", "BELIZE": "bz", "BENIN": "bj", "BERMUDA": "bm", "BHUTAN": "bt", "BOLIVIA, PLURINATIONAL STATE OF": "bo", "BONAIRE, SINT EUSTATIUS AND SABA": "bq", "BOSNIA AND HERZEGOVINA": "ba", "BOTSWANA": "bw", "BOUVET ISLAND": "bv", "BRAZIL": "br", "BRITISH INDIAN OCEAN TERRITORY": "io", "BRUNEI DARUSSALAM": "bn", "BULGARIA": "bg", "BURKINA FASO": "bf", "BURUNDI": "bi", "CABO VERDE": "cv", "CAMBODIA": "kh", "CAMEROON": "cm", "CANADA": "ca", "CAYMAN ISLANDS": "ky", "CENTRAL AFRICAN REPUBLIC": "cf", "CHAD": "td", "CHILE": "cl", "CHINA": "cn", "CHRISTMAS ISLAND": "cx", "COCOS (KEELING) ISLANDS": "cc", "COLOMBIA": "co", "COMOROS": "km", "CONGO": "cg", "CONGO, THE DEMOCRATIC REPUBLIC OF THE": "cd", "COOK ISLANDS": "ck", "COSTA RICA": "cr", "CÔTE D'IVOIRE": "ci", "CROATIA": "hr", "CUBA": "cu", "CURAÇAO": "cw", "CYPRUS": "cy", "CZECHIA": "cz", "DENMARK": "dk", "DJIBOUTI": "dj", "DOMINICA": "dm", "DOMINICAN REPUBLIC": "do", "EAST TIMOR": "tl", "ECUADOR": "ec", "EGYPT": "eg", "EL SALVADOR": "sv", "EQUATORIAL GUINEA": "gq", "ERITREA": "er", "ESTONIA": "ee", "ESWATINI": "sz", "ETHIOPIA": "et", "FALKLAND ISLANDS (MALVINAS)": "fk", "FAROE ISLANDS": "fo", "FIJI": "fj", "FINLAND": "fi", "FRANCE": "fr", "FRENCH GUIANA": "gf", "FRENCH POLYNESIA": "pf", "FRENCH SOUTHERN TERRITORIES": "tf", "GABON": "ga", "GAMBIA": "gm", "GEORGIA": "ge", "GERMANY": "de", "GHANA": "gh", "GIBRALTAR": "gi", "GREECE": "gr", "GREENLAND": "gl", "GRENADA": "gd", "GUADELOUPE": "gp", "GUAM": "gu", "GUATEMALA": "gt", "GUERNSEY": "gg", "GUINEA": "gn", "GUINEA-BISSAU": "gw", "GUYANA": "gy", "HAITI": "ht", "HEARD ISLAND AND MCDONALD ISLANDS": "hm", "HOLY SEE": "va", "HONDURAS": "hn", "HONG KONG": "hk", "HUNGARY": "hu", "ICELAND": "is", "INDIA": "in", "INDONESIA": "id", "IRAN, ISLAMIC REPUBLIC OF": "ir", "IRAQ": "iq", "IRELAND": "ie", "ISLE OF MAN": "im", "ISRAEL": "il", "ITALY": "it", "JAMAICA": "jm", "JAPAN": "jp", "JERSEY": "je", "JORDAN": "jo", "KAZAKHSTAN": "kz", "KENYA": "ke", "KIRIBATI": "ki", "KOREA, DEMOCRATIC PEOPLE'S REPUBLIC OF": "kp", "KOREA, REPUBLIC OF": "kr", "KUWAIT": "kw", "KYRGYZSTAN": "kg", "LAO PEOPLE'S DEMOCRATIC REPUBLIC": "la", "LATVIA": "lv", "LEBANON": "lb", "LESOTHO": "ls", "LIBERIA": "lr", "LIBYA": "ly", "LIECHTENSTEIN": "li", "LITHUANIA": "lt", "LUXEMBOURG": "lu", "MACAO": "mo", "MADAGASCAR": "mg", "MALAWI": "mw", "MALAYSIA": "my", "MALDIVES": "mv", "MALI": "ml", "MALTA": "mt", "MARSHALL ISLANDS": "mh", "MARTINIQUE": "mq", "MAURITANIA": "mr", "MAURITIUS": "mu", "MAYOTTE": "yt", "MEXICO": "mx", "MICRONESIA, FEDERATED STATES OF": "fm", "MOLDOVA, REPUBLIC OF": "md", "MONACO": "mc", "MONGOLIA": "mn", "MONTENEGRO": "me", "MONTSERRAT": "ms", "MOROCCO": "ma", "MOZAMBIQUE": "mz", "MYANMAR": "mm", "NAMIBIA": "na", "NAURU": "nr", "NEPAL": "np", "NETHERLANDS, KINGDOM OF THE": "nl", "NEW CALEDONIA": "nc", "NEW ZEALAND": "nz", "NICARAGUA": "ni", "NIGER": "ne", "NIGERIA": "ng", "NIUE": "nu", "NORFOLK ISLAND": "nf", "NORTH MACEDONIA": "mk", "NORTHERN MARIANA ISLANDS": "mp", "NORWAY": "no", "OMAN": "om", "PAKISTAN": "pk", "PALAU": "pw", "PALESTINE, STATE OF": "ps", "PANAMA": "pa", "PAPUA NEW GUINEA": "pg", "PARAGUAY": "py", "PERU": "pe", "PHILIPPINES": "ph", "PITCAIRN": "pn", "POLAND": "pl", "PORTUGAL": "pt", "PUERTO RICO": "pr", "QATAR": "qa", "RÉUNION": "re", "ROMANIA": "ro", "RUSSIAN FEDERATION": "ru", "RWANDA": "rw", "SAINT BARTHÉLEMY": "bl", "SAINT HELENA, ASCENSION AND TRISTAN DA CUNHA": "sh", "SAINT KITTS AND NEVIS": "kn", "SAINT LUCIA": "lc", "SAINT MARTIN (FRENCH PART)": "mf", "SAINT PIERRE AND MIQUELON": "pm", "SAINT VINCENT AND THE GRENADINES": "vc", "SAMOA": "ws", "SAN MARINO": "sm", "SAO TOME AND PRINCIPE": "st", "SAUDI ARABIA": "sa", "SENEGAL": "sn", "SERBIA": "rs", "SEYCHELLES": "sc", "SIERRA LEONE": "sl", "SINGAPORE": "sg", "SINT MAARTEN (DUTCH PART)": "sx", "SLOVAKIA": "sk", "SLOVENIA": "si", "SOLOMON ISLANDS": "sb", "SOMALIA": "so", "SOUTH AFRICA": "za", "SOUTH GEORGIA AND THE SOUTH SANDWICH ISLANDS": "gs", "SOUTH SUDAN": "ss", "SPAIN": "es", "SRI LANKA": "lk", "SUDAN": "sd", "SURINAME": "sr", "SVALBARD AND JAN MAYEN ISLANDS": "sj", "SWEDEN": "se", "SWITZERLAND": "ch", "SYRIAN ARAB REPUBLIC": "sy", "TAIWAN": "tw", "TAJIKISTAN": "tj", "TANZANIA, UNITED REPUBLIC OF": "tz", "THAILAND": "th", "TOGO": "tg", "TOKELAU": "tk", "TONGA": "to", "TRINIDAD AND TOBAGO": "tt", "TUNISIA": "tn", "TÜRKIYE": "tr", "TURKMENISTAN": "tm", "TURKS AND CAICOS ISLANDS": "tc", "TUVALU": "tv", "UGANDA": "ug", "UKRAINE": "ua", "UNITED ARAB EMIRATES": "ae", "UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND": "gb", "UNITED STATES MINOR OUTLYING ISLANDS": "um", "URUGUAY": "uy", "UZBEKISTAN": "uz", "VANUATU": "vu", "VENEZUELA, BOLIVARIAN REPUBLIC OF": "ve", "VIET NAM": "vn", "VIRGIN ISLANDS, BRITISH": "vg", "VIRGIN ISLANDS, U.S.": "vi", "WALLIS AND FUTUNA ISLANDS": "wf", "WESTERN SAHARA": "eh", "YEMEN": "ye", "ZAMBIA": "zm", "ZIMBABWE": "zw"}


class EditorialManagerLoginError(VenueLoginError):
    """Raised when an automatic Editorial Manager sign-in cannot be completed."""


def _country_label(country: str) -> str:
    """Map a country name to the label shown in the Editorial Manager drop-down."""
    value = (country or "").strip()
    return _COUNTRY_ALIASES.get(value.upper(), value)


def _content(page):
    """The Editorial Manager content frame (re-resolved on each action)."""
    return page.frame_locator(CONTENT_FRAME)


def _try(fn, what: str) -> bool:
    """Run ``fn``; log and continue if it fails.

    The Editorial Manager declarations page is a long list of policy questions
    whose exact controls drift between deployments. A missing or moved control
    there should not abort the data-bearing steps that follow, so each optional
    click goes through here. Returns True if ``fn`` ran without raising.
    """
    try:
        fn()
        return True
    except Exception as exc:  # noqa: BLE001 -- best-effort optional step
        logger.warning("Editorial Manager: skipped %s (%s)", what, exc)
        return False

def _pick_or_enter_institution(cf, institution: str) -> bool:
    """Enter an institution into Editorial Manager.

    If an autocomplete suggestion appears, click the first suggestion.
    Otherwise leave the typed text alone (Editorial Manager will ask
    whether to use the entered institution when Save is clicked).

    Returns whether or not a suggestion was clicked.
    """
    textbox = cf.get_by_role("textbox", name="Institution")

    textbox.click()
    textbox.fill("")
    textbox.press_sequentially(institution, delay=50)

    # Wait briefly for the autocomplete popup.
    suggestions = cf.locator("ul.ui-autocomplete:visible li")

    try:
        suggestions.first.wait_for(state="visible", timeout=1500)
        suggestions.first.click()
        return True
    except PlaywrightTimeoutError:
        # No suggestion appeared; keep the typed text.
        logger.warning("No autocomplete suggestion appeared for institution %r; leaving typed text", institution)
    return False


def _pick_funder(cf, value: str) -> None:
    """Fill the funder field verbatim (no autocomplete suggestion is picked)."""
    textbox = cf.get_by_role("textbox", name="Find a Funder:")
    textbox.click()
    textbox.fill(value)


def _parse_authors(value: str, cfg: Variant = _DEFAULT) -> list[dict]:
    """Parse the author block into dicts using the venue's author column set."""
    author_field = next((f for f in cfg.venue.fields if f.type == "authorlist"), None)
    return parse_authors(value, author_field.fields if author_field else None)


def _parse_figures(value: str) -> list[tuple[str, str]]:
    """Parse the figure block into ``(path, label)`` tuples.

    Each non-empty line is ``path | label`` (label optional; used as the figure's
    Description at upload, blank when omitted).
    """
    figures: list[tuple[str, str]] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        path = parts[0].strip()
        label = parts[1].strip() if len(parts) > 1 else ""
        if path:
            figures.append((path, label))
    return figures


# The reviewer step's two add buttons, keyed by the ``.sub`` stance column.
REVIEWER_ADD_BUTTONS = {
    "suggested": "+Add Suggested Reviewer",
    "opposed": "+Add Opposed Reviewer",
}


def _split_reviewers(raw: str) -> list[dict]:
    """Parse the ``reviewers`` field into a list of reviewer dicts.

    One reviewer per line, columns separated by ``|``:
    ``name | email | institution | stance | reason``. ``stance`` is "suggested"
    or "opposed" (case-insensitive, default "suggested"); ``email``,
    ``institution``, and ``reason`` are optional. Blank lines are skipped.
    """
    reviewers: list[dict] = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("|")]
        name = parts[0] if len(parts) > 0 else ""
        email = parts[1] if len(parts) > 1 else ""
        institution = parts[2] if len(parts) > 2 else ""
        stance = (parts[3] if len(parts) > 3 and parts[3] else "suggested").lower()
        reason = parts[4] if len(parts) > 4 else ""
        if stance not in REVIEWER_ADD_BUTTONS:
            raise ValueError(f"editorial_manager: reviewer stance {stance!r} for {name or email!r} must be one of {sorted(REVIEWER_ADD_BUTTONS)}")
        reviewers.append({"name": name, "email": email, "institution": institution, "stance": stance, "reason": reason})
    return reviewers


def _login_available(page, timeout_ms: int = 4000) -> bool:
    """True when a sign-in control is shown (Cell sometimes lands already
    signed in, offering none)."""
    cf = _content(page)
    for locator in (
        cf.get_by_role("button", name="Log In"),
        cf.get_by_role("textbox", name="username"),
        cf.frame_locator(LOGIN_FRAME).get_by_role("textbox", name="username"),
    ):
        try:
            expect(locator.first).to_be_visible(timeout=timeout_ms)
            return True
        except (AssertionError, PWTimeout):
            continue
    return False


def _open_main_menu(page) -> None:
    """Click the "Main Menu" tab (``#MainMenu``, outside the content frame) to
    load the author main menu. Best-effort: absent when the menu already shows."""

    def _click() -> None:
        for ctx in [page, *page.frames]:
            link = ctx.locator("#MainMenu")
            if link.count():
                link.first.click(timeout=5000)
                return
        raise RuntimeError("Main Menu tab not found on the page or any frame")

    _try(_click, "open Main Menu")
    # Let the content frame load the main menu before the next step.
    _content(page).get_by_role("link", name=SUBMIT_NEW_LINK).wait_for(state="visible", timeout=5000)
    page.wait_for_timeout(1000)


def _dismiss_cookies(page) -> None:
    """Best-effort dismissal of the OneTrust cookie banner (shown only on a fresh
    browser); any failure is ignored."""

    def _seq() -> None:
        page.get_by_role("button", name="Cookie settings, Opens the").click(timeout=2000)
        page.locator(".ot-switch-nob").first.click()
        page.locator("div:nth-child(4) > .ot-acc-hdr > .ot-tgl > .ot-switch > .ot-switch-nob").click()
        page.get_by_role("button", name="Confirm my choices").click()

    _try(_seq, "cookie banner")


def _login_form(page, timeout_ms: int):
    """Return the frame locator holding the credential form.

    Probes the content frame and the nested login iframe for a visible username
    field; ``None`` if neither shows one within ``timeout_ms`` (likely still
    behind an unclicked splash "Log In" button).
    """
    cf = _content(page)
    for frame in (cf, cf.frame_locator(LOGIN_FRAME)):
        try:
            frame.get_by_role("textbox", name="username").wait_for(state="visible", timeout=timeout_ms)
            return frame
        except (AssertionError, PWTimeout):
            continue
    return None


def _select_article_type(cf, article_type: str) -> None:
    """Pick the article type; a numeric value is the option value, else the label."""
    select = cf.get_by_role("tabpanel", name="Select Article Type").get_by_label("Select Article Type")
    if article_type.isdigit():
        _try(lambda: select.select_option(article_type), f"article type value {article_type!r}")
    else:
        _try(lambda: select.select_option(label=article_type), f"article type {article_type!r}")
    cf.get_by_role("button", name=" Proceed").click()


def _upload(page, cf, path: str) -> None:
    """Attach one file: "Browse..." is a button (not a file input), so intercept
    the native file chooser it opens and hand it the file."""
    with page.expect_file_chooser() as fc_info:
        cf.get_by_role("button", name="Browse...").click()
    fc_info.value.set_files(path)


def _attach_files(page, cf, manuscript_file: str, cover_letter: str, declaration_file: str, figure_files: list[tuple[str, str]] | None = None, cfg: Variant = _DEFAULT) -> None:
    """Upload the manuscript, cover letter, optional declaration, and figures.

    The manuscript takes the default (main document) item type; the others set
    their item type (by label) before browsing. A figure's label, when present,
    is filled into the Description field first.
    """
    logger.info("Uploading manuscript %s", manuscript_file)

    # close the "generated with AI" popup
    try:
        page.wait_for_timeout(2000)
        page.get_by_role("button", name="Close").click(timeout=4000)
    except:  # Button didn't appear, continue
        pass

    _upload(page, cf, manuscript_file)
    page.wait_for_timeout(2000)
    if cfg.annotate_manuscript_manually:
        page.locator('iframe[name="content"]').content_frame.locator("select.submissionItemDropDown").nth(0).select_option("6")
        page.locator('iframe[name="content"]').content_frame.locator("input[id^='description_']").first.fill("Manuscript")

    if cover_letter:
        logger.info("Uploading cover letter %s", cover_letter)
        _try(lambda: cf.get_by_label("Select Item Type").select_option(label=cfg.cover_letter_label), "cover letter item type")
        page.wait_for_timeout(2000)
        _upload(page, cf, cover_letter)
        page.wait_for_timeout(2000)

    if declaration_file:
        logger.info("Uploading declaration of interests %s", declaration_file)
        _try(lambda: cf.get_by_label("Select Item Type").select_option(label="*Declaration of Interests form"), "declaration item type")
        page.wait_for_timeout(2000)
        _upload(page, cf, declaration_file)
        page.wait_for_timeout(2000)

    for figure, label in figure_files or []:
        logger.info("Uploading figure %s (label=%s)", figure, label or "")
        _try(lambda: cf.get_by_label("Select Item Type").select_option(label="Figure"), "figure item type")
        page.wait_for_timeout(2000)
        if label:
            _try(lambda: cf.get_by_role("textbox", name="Description").fill(label), "figure description")
        _upload(page, cf, figure)
        page.wait_for_timeout(2000)

    # Proceed off the attach-files step, then past the file-order confirmation.
    cf.get_by_role("button", name=" Proceed").click()


def _answer_declarations(cf, related_work: str, original_code: bool, code_url: str, alternate_contact: str, confirm_declarations: bool, cfg: Variant = _DEFAULT) -> None:
    """Click through the Cell declarations page.

    Most answers default to "No"; ``related_work`` and ``original_code`` come from
    the ``.sub``. Each click is best-effort (see :func:`_try`); several controls
    are gated on the :class:`Variant` where deployments diverge.
    """
    if confirm_declarations:
        _try(lambda: cf.get_by_role("checkbox", name="The paper conforms to all").check(), "conforms declaration")
        _try(lambda: cf.get_by_role("checkbox", name="All appropriate contributors").check(), "contributors declaration")
        _try(lambda: cf.get_by_role("checkbox", name="All authors have seen the").check(), "authors-seen declaration")
        _try(lambda: cf.get_by_role("checkbox", name="I will share all status").check(), "status-update declaration")

    # Presubmission inquiry / solicited submission: No.
    _try(lambda: cf.get_by_role("cell", name="Please select a response Yes – I submitted a presubmission inquiry").get_by_label("No").check(), "presubmission inquiry")
    # A previous version of this paper submitted to a Cell Press venue: No
    # (offered only by some deployments).
    if cfg.ask_previous_version:
        _try(lambda: cf.get_by_role("radio", name="No – No version of this paper").check(), "previous version")

    # Related work in press / under consideration elsewhere: from the .sub.
    if related_work.strip().lower() == "yes":
        _try(lambda: cf.get_by_role("cell", name="Related work Do you or any of your co-authors").get_by_label("Yes").check(), "related work = Yes")
        _try(lambda: cf.get_by_role("checkbox", name="I confirm that the related").check(), "related-work confirmation")
    else:
        _try(lambda: cf.get_by_role("cell", name="Related work Do you or any of your co-authors").get_by_label("No").check(), "related work = No")

    # Co-consideration (multi-venue submission): No.
    _try(lambda: cf.get_by_role("cell", name="Co-consideration If you want to have your manuscript considered").get_by_label("No").check(), "co-consideration")
    # Share data with editors: No.
    _try(lambda: cf.get_by_role("radio", name="No, I do not want to share my").check(), "share data")
    # Transparent peer review: No (offered only by some deployments).
    if cfg.ask_transparent_peer_review:
        _try(lambda: cf.get_by_role("cell", name="Transparent Peer Review").get_by_label("No").check(), "transparent peer review")
    # Standardized datasets: No.
    _try(lambda: cf.get_by_role("cell", name="Standardized datasets").get_by_label("No").check(), "standardized datasets")

    # Original code: from the .sub. When yes, fill the repository URL textarea.
    if original_code:
        _try(lambda: cf.get_by_role("cell", name="Original code Does this manuscript report original").get_by_label("Yes", exact=True).check(), "original code = Yes")
        if code_url:
            _try(lambda: cf.locator('textarea[name="QR6_1$Q169$Q170$RSP_170"]').fill(code_url), "code repository URL")
    else:
        _try(lambda: cf.get_by_role("cell", name="Original code Does this manuscript report original").get_by_label("No", exact=True).check(), "original code = No")

    # New macromolecule / small-molecule structures: No.
    _try(lambda: cf.get_by_role("cell", name="Structures Does your manuscript report new structure").get_by_label("No", exact=True).check(), "structures")
    # Do not publish the review process (offered only by some deployments).
    if cfg.ask_publish_review:
        _try(lambda: cf.get_by_role("radio", name="I do not wish to publish my").check(), "publish-review preference")

    # Alternate contact (mode varies by deployment). Only touched when the .sub
    # provides one, so a blank value never overwrites an address already present.
    if alternate_contact:
        if cfg.alternate_contact_mode == "radio_textbox":
            _try(lambda: cf.locator('input[name="QR6_1$Q213$RSP_213"]').nth(2).check(), "alternate-contact option")
            _try(lambda: cf.get_by_role("textbox").first.fill(alternate_contact), "alternate-contact email")
        elif cfg.alternate_contact_mode == "single_field":
            _try(lambda: cf.locator('input[name="QR4_1$Q48$RSP_48"]').fill(alternate_contact), "alternate-contact")
        else:  # "textbox"
            _try(lambda: cf.get_by_role("textbox").first.fill(alternate_contact), "alternate-contact email")

    # Proceed off the declarations page.
    cf.get_by_role("button", name=" Proceed").click()


def _answer_declarations_plos(
    page,
    cf,
    *,
    competing_interests: str = "",
    data_availability: str = "",
    funding_statement: str = "",
    funding_country: str = "",
    previous_interactions: str = "",
    prior_submission: str = "",
    preprint_doi: str = "",
    related_work: str = "",
    human_participants: str = "No",
) -> None:
    """Click through the PLOS Computational Biology declarations page.

    PLOS's deployment shares no controls with the Cell-family page: every question
    is a ``QR23_1_Q*`` widget addressed by element id, so it gets its own replay,
    selected by the Variant's ``plos_declarations`` flag. Fixed drop-downs and the
    closing radio are replayed from the recording; free-text statements come from
    the ``.sub``. The two conditional drop-downs (financial disclosure, related
    manuscript) reveal a text box only on their affirmative option. Each step is
    best-effort (see :func:`_try`); re-capture with ``playwright codegen`` if the
    page is restyled.
    """
    def _select_country(select, country):
        # Match the .sub country against the option labels by leading text,
        # case-insensitively. A blank value is a no-op.
        country = (country or "").strip().lower()
        if not country:
            return
        options = select.locator("option").evaluate_all("opts => opts.map(o => ({label: o.textContent.trim(), value: o.value}))")
        for opt in options:
            if opt["label"].strip().lower().startswith(country):
                select.select_option(value=opt["value"])
                return
        raise ValueError(f"funding country {country!r} not found in the drop-down")

    # Opening policy drop-down (replayed from the recording).
    _try(lambda: cf.locator("#QR23_1_Q46916_RSP_46916").select_option("324295"), "plos declaration Q46916")

    # Financial disclosure: the "funded" option reveals the statement box and a
    # follow-up drop-down; otherwise pick "no funding". The statement is a textarea
    # addressed by its ``name`` (its ``RSP_46959`` id is the wrapper, not the field).
    if funding_statement:
        _try(lambda: cf.locator("#QR23_1_Q46958_RSP_46958").select_option("326587"), "financial disclosure = funded")
        _try(lambda: cf.locator('textarea[name="QR23_1$Q46958$Q46959$RSP_46959"]').fill(funding_statement), "funding statement")
        _try(lambda: _select_country(cf.locator("#QR23_1_Q46958_Q46960_RSP_46960"), funding_country), "funding country")
    else:
        _try(lambda: cf.locator("#QR23_1_Q46958_RSP_46958").select_option("326588"), "financial disclosure = no funding")

    # Competing-interests statement.
    if competing_interests:
        _try(lambda: cf.locator("#QR23_1_Q46919_RSP_46919").fill(competing_interests), "competing interests")

    # Data-availability statement.
    if data_availability:
        _try(lambda: cf.locator('textarea[name="QR23_1$Q197$RSP_197"]').fill(data_availability), "data availability")

    # Human participants/data/specimens: "Yes" also checks the three confirmation
    # boxes; "No" (the default) selects the negative option.
    if human_participants.strip().lower() in {"yes", "y", "true", "1", "on"}:
        _try(lambda: cf.locator("#QR23_1_Q47036_RSP_47036").select_option("328402"), "human participants = yes")
        _try(lambda: cf.get_by_role("checkbox", name="I confirm that all relevant").check(), "human participants confirm 1")
        _try(lambda: cf.get_by_role("checkbox", name="I confirm that any data").check(), "human participants confirm 2")
        _try(lambda: cf.get_by_role("checkbox", name="I confirm that the data").check(), "human participants confirm 3")
    else:
        _try(lambda: cf.locator("#QR23_1_Q47036_RSP_47036").select_option("328403"), "human participants = no")

    # Related work, copyright, & dual submission free-text ("No" or explanation).
    if not related_work:
        related_work = "No"
    if related_work:
        _try(lambda: cf.locator("#QR23_1_Q46921_RSP_46921").fill(related_work), "related work")

    # Optional prior-interaction disclosures: checked and filled only when the
    # .sub provides the text.
    if previous_interactions:
        _try(lambda: cf.get_by_role("checkbox", name="I have had previous").check(), "previous-interactions checkbox")
        _try(lambda: cf.locator("#QR23_1_Q46922_Q46923_RSP_46923").fill(previous_interactions), "previous-interactions detail")
    if prior_submission:
        _try(lambda: cf.get_by_role("checkbox", name="This manuscript was").check(), "prior-submission checkbox")
        _try(lambda: cf.locator("#QR23_1_Q46922_Q46924_RSP_46924").fill(prior_submission), "prior-submission detail")

    # Related manuscript: the affirmative option reveals a DOI box; select and fill
    # it only when a DOI is given, else leave the question at its default.
    if preprint_doi:
        _try(lambda: cf.locator("#QR23_1_Q46909_RSP_46909").select_option("324170"), "related manuscript = yes")
        _try(lambda: cf.locator("#QR23_1_Q46909_Q46910_RSP_46910").fill(preprint_doi), "related manuscript DOI")
    else:
        _try(lambda: cf.locator("#QR23_1_Q46909_RSP_46909").select_option("324171"), "related manuscript = no")
        _try(lambda: cf.locator("#QR23_1_Q46909_Q46911_RSP_46911").select_option("324173"),"bioRxiv posting = no")


    # Closing agreement radio and policy drop-down (replayed from the recording).
    _try(lambda: cf.get_by_role("radio", name="No - I do not agree to").check(), "plos agreement radio")
    _try(lambda: cf.locator("#QR23_1_Q46961_RSP_46961").select_option("326839"), "plos declaration Q46961")

    # Proceed off the declarations page.
    page.wait_for_timeout(1000)
    cf.get_by_role("button", name=" Proceed").click()
    print("If it hangs here, try refreshing the open browser")


def _enter_comments(page, cf, comments: str) -> None:
    """Fill the optional comments-to-editor page (only when ``has_comments_page``)."""
    page.wait_for_timeout(2000)
    if comments:
        _try(lambda: cf.locator('textarea[name="QR4_1$Q49$RSP_49"]').fill(comments), "comments to editor")
    cf.get_by_role("button", name=" Proceed").click()


def _fill_rich_text(editor, text: str) -> None:
    """Type a value into a CKEditor body.

    ``fill()`` writes the contenteditable DOM directly and does not drive
    CKEditor's change handling, leaving its hidden (validated) field empty.
    Focusing and typing dispatches the real key events; a trailing blur commits
    the change.
    """
    body = editor.locator("body")
    body.click()
    body.press_sequentially(text)
    body.blur()


def _enter_metadata(cf, title: str, abstract: str, keywords: str) -> None:
    """Enter title, abstract, and keywords on the manual-entry metadata step."""
    cf.get_by_role("button", name="Enter Data Manually").click()
    _try(lambda: cf.get_by_role("button", name="Yes, Enter Data Manually").click(), "confirm manual entry")

    if title:
        title_editor = cf.frame_locator('iframe[title="Rich Text Editor, fullTitleHtml"]')
        _try(lambda: _fill_rich_text(title_editor, title), "title")
    if abstract:
        # Abstract lives on its own tab; click it before the editor iframe is interactable.
        _try(lambda: cf.locator("#tlblAbstractTitle").click(), "open Abstract tab")
        abstract_editor = cf.frame_locator('iframe[title="Rich Text Editor, abstractHtml"]')
        _try(lambda: _fill_rich_text(abstract_editor, abstract), "abstract")
    if keywords:
        # Keywords are on a separate tab as well.
        _try(lambda: cf.get_by_role("tab", name="Keywords").click(), "open Keywords tab")
        _try(lambda: cf.locator("#txtKeywords").fill(keywords), "keywords")


def _parse_classifications(value: str) -> list[str]:
    """Parse the ``classifications`` field into a list of keywords (one per line)."""
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def _enter_section_classifications(page, cf, section: str, classifications: list[str]) -> None:
    """Set the PLOS Section/Category drop-down and add classifications (PLOS only).

    A numeric ``section`` is the option value, else the label. Classifications open
    a separate popup: each keyword is searched, its checkbox checked, and added with
    "Add->", then the popup is submitted and closed. Best-effort (see :func:`_try`).
    """
    if section:
        select = cf.get_by_role("tabpanel", name="Section/Category").get_by_label("Section/Category")
        if section.isdigit():
            _try(lambda: select.select_option(section), f"section value {section!r}")
        else:
            _try(lambda: select.select_option(label=section), f"section {section!r}")

    if not classifications:
        return

    # Open the Classifications step, then its "Add Classifications" popup window.
    _try(lambda: cf.get_by_text("Classifications", exact=True).click(), "open Classifications step")
    try:
        with page.expect_popup() as popup_info:
            cf.get_by_role("button", name="Add Classifications").click()
        popup = popup_info.value
    except Exception as exc:  # noqa: BLE001 -- best-effort optional step
        logger.warning("%s: skipped open Classifications popup (%s)", "PLOS Computational Biology", exc)
        return

    for keyword in classifications:

        def _add(kw=keyword) -> None:
            search = popup.get_by_role("textbox", name="Search:")
            search.click()
            search.fill(kw)
            popup.get_by_role("button", name="Search").click()
            popup.get_by_role("checkbox", name=kw).first.check()
            popup.get_by_role("button", name="Add->").click()

        _try(_add, f"add classification {keyword!r}")

    # Submit the popup's selected classifications and close the window.
    _try(lambda: popup.locator("#btnSubmit2").click(timeout=8000), "submit classifications")
    _try(lambda: popup.close(), "close Classifications popup")
    cf.get_by_role("button", name=" Proceed").click()


def _is_corresponding(author: dict) -> bool:
    """True when an author line is marked as the corresponding author."""
    return str(author.get("corresponding", "")).strip().lower() in {"yes", "y", "true", "1", "on"}


def _drag_author(cf, page, from_idx: int, to_idx: int):
    # Filter out hidden/template handles
    all_handles = cf.locator("td.fl-listitem-sortable")
    handles = []

    for i in range(all_handles.count()):
        handle = all_handles.nth(i)
        if handle.bounding_box() is not None:
            handles.append(handle)

    n = len(handles)
    if not (0 <= from_idx < n and 0 <= to_idx < n):
        raise IndexError(f"Author index out of range: {from_idx}->{to_idx} " f"(there are {n} visible authors)")

    src = handles[from_idx].bounding_box()
    dst = handles[to_idx].bounding_box()

    page.mouse.move(
        src["x"] + src["width"] / 2,
        src["y"] + src["height"] / 2,
    )
    page.mouse.down()

    # Exceed jQuery UI drag threshold
    page.mouse.move(
        src["x"] + src["width"] / 2,
        src["y"] + src["height"] / 2 + 8,
        steps=5,
    )

    # Drop just below the destination
    page.mouse.move(
        dst["x"] + dst["width"] / 2,
        dst["y"] + dst["height"] + 10,
        steps=max(40, abs(to_idx - from_idx) * 30),
    )

    page.mouse.up()


def _enter_authors(page, cf, authors: list[dict]) -> None:
    """Fill the Authors step.

    Editorial Manager pre-fills one author (the corresponding author / account
    holder), so the ``corresponding=yes`` line is applied by editing that row and
    every other line is added via "+Add Another Author". With no line marked
    corresponding, the first line is treated as the pre-filled row.
    """
    if not authors:
        return

    _try(lambda: cf.get_by_text("Authors", exact=True).click(), "open Authors step")

    # The pre-filled row is the corresponding author; fall back to the first line.
    corresponding = next((a for a in authors if _is_corresponding(a)), authors[0])

    for author in authors:
        page.wait_for_timeout(2000)
        if author is corresponding:

            def _edit(a=author) -> None:
                cf.get_by_role("button", name="Edit This Author").click()
                if a.get("institution"):
                    _try(lambda: _pick_or_enter_institution(cf, a["institution"]), "corresponding author institution")
                if a.get("country"):
                    cf.get_by_label("Country or Region *").select_option(_country_label(a["country"]))

                cf.locator(".fl-flToolSave:visible").click(timeout=3000)

            _try(_edit, f"edit corresponding author {author.get('name', '')}".strip())
            continue

        first, last = _split_name(author.get("name", ""))

        def _add(a=author, first=first, last=last) -> None:
            cf.get_by_role("button", name="+Add Another Author").nth(1).click()
            cf.get_by_role("textbox", name="Given/First Name *").fill(first)
            cf.get_by_role("textbox", name="Family/Last Name *").fill(last)
            cf.get_by_role("textbox", name="E-mail Address *").fill(a.get("email", ""))
            if a.get("institution"):
                _try(lambda: _pick_or_enter_institution(cf, a["institution"]), "author institution")
            if a.get("country"):
                cf.get_by_label("Country or Region *").select_option(_country_label(a["country"]))
            cf.locator(".fl-flToolSave:visible").click(timeout=3000)

        _try(_add, f"add author {author.get('name', '')}".strip())

    # drag corresponding from 0 to the index of corresponding author
    corresponding_index = authors.index(corresponding)
    if corresponding_index != 0:
        _drag_author(cf, page, 0, corresponding_index)


def _add_reviewer(page, cf, reviewer: dict) -> None:
    """Add one suggested or opposed reviewer.

    The stance-specific add button (``nth(1)``, past the hidden template row) opens
    the inline form; the name is split on its last space. The "Institution *" field
    is an async autocomplete that must be typed and picked (see
    :func:`_pick_autocomplete`), not filled verbatim. Optional fields go through
    :func:`_try`.
    """
    first, last = _split_name(reviewer["name"])
    logger.info("Adding %s reviewer %s <%s>", reviewer["stance"], reviewer["name"], reviewer.get("email", ""))

    cf.get_by_role("button", name=REVIEWER_ADD_BUTTONS[reviewer["stance"]]).nth(1).click()
    cf.get_by_role("textbox", name="Given/First Name *").fill(first)
    cf.get_by_role("textbox", name="Family/Last Name *").fill(last)
    if reviewer.get("institution"):
        institution_clicked = _pick_or_enter_institution(cf, reviewer["institution"])
    if reviewer.get("email"):
        _try(lambda: cf.get_by_role("textbox", name="E-mail").fill(reviewer["email"]), "reviewer email")
    if reviewer.get("reason"):
        _try(lambda: cf.get_by_role("textbox", name="Reason").fill(reviewer["reason"]), "reviewer reason")
    cf.locator(".fl-flToolSave:visible").click(timeout=3000)
    if not institution_clicked:
        cf.get_by_role("button", name="OK").click(timeout=1000)



def _enter_reviewers(page, cf, reviewers: list[dict]) -> None:
    """Fill the reviewer-preferences step.

    The two lists have their own add buttons: Suggested is open on arrival, while
    Opposed is collapsed until its heading is clicked. Reviewers are processed in
    two passes (suggested, then opposed) so each pass works against a visible list,
    with a short settle between adds. Best-effort (see :func:`_try`).
    """
    if not reviewers:
        cf.get_by_role("button", name=" Proceed").click()
        return

    suggested = [r for r in reviewers if r["stance"] == "suggested"]
    opposed = [r for r in reviewers if r["stance"] == "opposed"]

    def _add_all(group: list[dict]) -> None:
        for index, reviewer in enumerate(group):
            if index:
                page.wait_for_timeout(2000)
            _try(lambda r=reviewer: _add_reviewer(page, cf, r), f"add reviewer {reviewer.get('name', '')}".strip())

    if suggested:
        _add_all(suggested)

    if opposed:
        _try(lambda: cf.get_by_text("Oppose Reviewers").first.click(), "open Oppose Reviewers section")
        page.wait_for_timeout(1000)
        _add_all(opposed)

    cf.get_by_role("button", name=" Proceed").click()


def _enter_funding(page, cf, funders: list[dict]) -> None:
    """Fill the Funding Information step, or mark it not available.

    Each funder is added via "+Add a Funding Source" (see :func:`_pick_funder`),
    then the optional award number. With no funders, the "Funding information is
    not available" box is checked instead.
    """
    _try(lambda: cf.get_by_role("tab", name="Funding Information").click(), "open Funding Information tab")

    if not funders:
        _try(lambda: cf.get_by_role("checkbox", name="Funding information is not").check(), "no-funding declaration")
        return

    def click_visible_tool(cf, toolname: str):
        buttons = cf.locator(f'[data-toolname="{toolname}"]')
        for i in range(buttons.count()):
            b = buttons.nth(i)
            if b.is_visible():
                b.click(timeout=3000)
                return
        raise RuntimeError(f"No visible {toolname} button found")

    for funder in funders:

        def _add(f=funder) -> None:
            cf.get_by_role("button", name="+Add a Funding Source").nth(1).click()
            _try(lambda: _pick_funder(cf, f["name"]), f"funder suggestion {f['name']!r}")
            if f["awards"]:
                _try(lambda: cf.get_by_role("textbox", name="Award Number:").fill(f["awards"][0]), "award number")
            # cf.get_by_role("button", name="Save This Award Number").click()
            click_visible_tool(cf, "Save")
            page.wait_for_timeout(2000)
            ok = cf.get_by_role("button", name="OK")
            _try(lambda: ok.click(timeout=3000) if ok.count() else None, "funding save OK dialog")

        _try(_add, f"add funder {funder['name']!r}")


def _select_publishing_options(page, cf, open_access: bool, cfg: Variant = _DEFAULT) -> None:
    """Set the open-access publishing option after the PDF is built.

    "Publishing Options" opens a separate Elsevier window offering two models as
    ``data-testid`` panels: ``gold-panel-new-design`` (open access) and
    ``subscription-panel-new-design``. The choice is saved with "Save and return".
    Each action is best-effort (see :func:`_try`).
    """
    # Opens a separate window (captured via expect_popup); the button only appears
    # once the PDF build is far enough along, so allow a generous wait.
    try:
        with page.expect_popup() as popup_info:
            cf.get_by_role("button", name="Publishing Options").click(timeout=15000)
        popup = popup_info.value
    except Exception as exc:  # noqa: BLE001 -- best-effort optional step
        logger.warning("Editorial Manager: skipped open Publishing Options popup (%s)", exc)
        return

    # The popup loads with its own OneTrust cookie banner; dismiss it first.
    _dismiss_cookies(popup)

    # Pick the model by test id (gold = open access); some venues offer gold only.
    if cfg.open_access_only:
        test_id = "gold-panel-new-design"
        page.wait_for_timeout(2000)
    else:
        test_id = "gold-panel-new-design" if open_access else "subscription-panel-new-design"
    _try(lambda: popup.get_by_test_id(test_id).click(timeout=8000), f"select publishing model (open_access={open_access})")

    # Save the choice and close the popup window.
    _try(lambda: popup.get_by_role("button", name="Save and return").click(timeout=8000), "Save and return")
    _try(lambda: popup.close(), "close Publishing Options popup")

    # Back on the wizard, advance past the publishing-options step.
    page.wait_for_timeout(2000)
    _try(lambda: cf.get_by_role("button", name="Proceed").click(timeout=8000), "Proceed past publishing options")


def editorialmanager_run(values: dict, headless: bool = False, debug: bool = False, new_session: bool = False, timeout: float = DEFAULT_TIMEOUT_SECONDS, *, venue) -> None:
    """Open the portal, sign in, then drive the Editorial Manager wizard from a ``.sub``.

    Sign-in is handled by ``venue.ensure_signed_in`` (saved session, stored
    credentials, then a manual sign-in). ``new_session`` discards any saved
    session; ``debug`` forces a headed browser and an Inspector pause. Leaves the
    browser open via :func:`hold_open` without clicking the final submit.
    """
    if debug:
        headless = False

    cfg = venue.variant

    article_type = values.get("article_type", "").strip()
    manuscript_file = values.get("manuscript_file", "").strip()
    cover_letter = values.get("cover_letter", "").strip()
    declaration_file = values.get("declaration_file", "").strip()
    figure_files = _parse_figures(values.get("figure_files", ""))
    related_work = values.get("related_work", "No")
    original_code = str(values.get("original_code", "")).strip().lower() in {"yes", "y", "true", "1", "on"}
    code_url = values.get("code_url", "").strip()
    alternate_contact = values.get("alternate_contact", "").strip()
    confirm_declarations = str(values.get("declarations_confirmed", "")).strip().lower() in {"yes", "y", "true", "1", "on"}
    # PLOS declarations page (a separate question set; see _answer_declarations_plos).
    competing_interests = values.get("competing_interests", "").strip()
    data_availability = values.get("data_availability", "").strip()
    previous_interactions = values.get("previous_interactions", "").strip()
    prior_submission = values.get("prior_submission", "").strip()
    preprint_doi = values.get("preprint_doi", "").strip()
    human_participants = values.get("human_participants", "No").strip()
    open_access = str(values.get("open_access", "")).strip().lower() in {"yes", "y", "true", "1", "on"}
    title = values.get("title", "")
    abstract = values.get("abstract", "")
    keywords = values.get("keywords", "").strip()
    section = values.get("section", "").strip()
    classifications = _parse_classifications(values.get("classifications", ""))
    authors = _parse_authors(values.get("authors", ""), cfg)
    reviewers = _split_reviewers(values.get("reviewers", ""))
    funders = _parse_funders(values.get("funding", ""))

    logger.info("Starting %s submission run (headless=%s, debug=%s, type=%s, " "manuscript=%s, authors=%d, reviewers=%d, figures=%d)", cfg.name, headless, debug, article_type, manuscript_file, len(authors), len(reviewers), len(figure_files))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = open_run_context(browser, venue.session_path(), new_session=new_session)
        apply_default_timeouts(context, timeout)
        page = context.new_page()

        venue.ensure_signed_in(page, context, debug=debug)

        if debug:
            page.pause()

        cf = _content(page)

        logger.debug("Signed in; opening the Main Menu, then starting a new submission")
        _open_main_menu(page)
        cf.get_by_role("link", name=SUBMIT_NEW_LINK).click()
        # One of two housekeeping popups may follow; handle whichever appears.
        _try(lambda: cf.get_by_role("button", name="Start a new submission").click(timeout=5000), "start-new-submission popup")
        _try(lambda: cf.get_by_role("button", name="Close").click(timeout=5000), "new-submission popup")

        _select_article_type(cf, article_type)

        _attach_files(page, cf, manuscript_file, cover_letter, declaration_file, figure_files, cfg=cfg)
        if cfg.has_section_classifications:
            _enter_section_classifications(page, cf, section, classifications)
        _enter_reviewers(page, cf, reviewers)
        if cfg.plos_declarations:
            _answer_declarations_plos(
                page,
                cf,
                competing_interests=competing_interests,
                data_availability=data_availability,
                funding_statement=values.get("funding", "").strip(),
                funding_country=values.get("funding_country", "").strip(),
                previous_interactions=previous_interactions,
                prior_submission=prior_submission,
                preprint_doi=preprint_doi,
                related_work=related_work,
                human_participants=human_participants,
            )
        else:
            _answer_declarations(cf, related_work, original_code, code_url, alternate_contact, confirm_declarations, cfg=cfg)
        if cfg.has_comments_page:
            _enter_comments(page, cf, values.get("comments", ""))
        _enter_metadata(cf, title, abstract, keywords)
        _enter_authors(page, cf, authors)
        _enter_funding(page, cf, funders)

        # "Save & Submit Later" is avoided here: it erases entered manuscript data.
        _try(lambda: cf.get_by_role("button", name="Build PDF for Approval").click(), "Build PDF for Approval")

        # After the PDF is built, set the open-access publishing option.
        _select_publishing_options(page, cf, open_access, cfg=cfg)

        logger.info("Completed the recorded %s steps; leaving the browser open " "for review (the final submit / PDF build is left to you)", cfg.name)
        hold_open()


class EditorialManagerVenue(Venue):
    """A venue on the Editorial Manager platform, selected by its Variant.

    Cell, Cell Genomics, Cell Systems, and PLOS Computational Biology each subclass
    this and set :attr:`variant`. This class threads the variant into the wizard
    runner and sign-in form, and overrides :meth:`ensure_signed_in` to keep
    Editorial Manager's "already signed in" shortcut (see :func:`_login_available`).
    """

    variant: Variant

    #: The author-area link that only renders for a signed-in session.
    logged_in_names = (SUBMIT_NEW_LINK,)
    #: EM hosts its whole author area inside this iframe, so the login-state
    #: marker is searched under it rather than on the top-level page.
    login_frame_selector = CONTENT_FRAME

    # display_name is inherited: the base returns get_venue(slug).name == variant.name.

    def run(
        self,
        values: dict,
        *,
        headless: bool = False,
        debug: bool = False,
        new_session: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        editorialmanager_run(
            values,
            headless=headless,
            debug=debug,
            new_session=new_session,
            timeout=timeout,
            venue=self,
        )

    def login(self, page, username: str, password: str, *, timeout_ms: int = 15000) -> None:
        """Fill and submit the Editorial Manager sign-in form from stored credentials.

        Locates the credential form (see :func:`_login_form`), clicking a splash
        "Log In" reveal button first if needed, submits it, and raises
        :class:`EditorialManagerLoginError` if the author area never loads.
        """
        cfg = self.variant
        login_url = cfg.login_url
        logger.debug("Filling the %s (Editorial Manager) sign-in form at %s", cfg.name, login_url)
        page.goto(login_url)
        _dismiss_cookies(page)

        try:
            # Cell hides the form behind a splash "Log In" button; PLOS shows it
            # directly. Look for the form first, revealing it only if absent.
            frame = _login_form(page, 4000)
            if frame is None:
                _try(lambda: _content(page).get_by_role("button", name="Log In").click(timeout=5000), "reveal login form")
                frame = _login_form(page, timeout_ms)
            if frame is None:
                raise PWTimeout("username field never became visible")
            frame.get_by_role("textbox", name="username").fill(username)
            frame.get_by_role("textbox", name="password").fill(password)
            frame.get_by_role("button", name="Author Login").click()
        except PWTimeout as exc:
            raise EditorialManagerLoginError(f"could not find the username/password fields on the {cfg.name} sign-in " "page (the Editorial Manager form may have changed); re-capture " "the selectors with 'playwright codegen %s'" % login_url) from exc

        if not self.is_logged_in(page, timeout_ms=timeout_ms):
            raise EditorialManagerLoginError(f"submitted the credentials but the signed-in {cfg.name} author area did " f"not load -- the username or password may be wrong, or {cfg.name} added " "a step (CAPTCHA / two-factor) that can't be automated")

    def ensure_signed_in(self, page, context, *, debug: bool = False) -> bool:
        """Get the page to a signed-in Editorial Manager author area.

        Overrides the base orchestrator for two quirks: a settle after loading the
        portal (the content frame is slow to populate), and the
        :func:`_login_available` shortcut for when Cell lands already signed in.
        """
        cfg = self.variant
        session = self.session_path()

        if session.exists():
            logger.debug("Trying the saved %s session at %s", cfg.name, session)
            page.goto(cfg.portal_url)
            page.wait_for_timeout(2000)
            if self.is_logged_in(page):
                logger.info("Reusing the saved %s session", cfg.name)
                return True
            logger.info("Saved %s session expired; re-authenticating", cfg.name)

        cred = None
        try:
            cred = credentials.get_credential(cfg.slug)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not load stored %s credentials (%s)", cfg.name, exc)
            cred = None

        if cred and cred.method == "password" and cred.username and cred.password:
            try:
                logger.info("Signing in to %s with stored credentials", cfg.name)
                print(f"Signing in to {cfg.name} with your stored credentials…")
                self.login(page, cred.username, cred.password)
                save_storage(context, session)
                print("Signed in; saved the session for next time.")
                return True
            except EditorialManagerLoginError as exc:
                logger.warning("Automatic %s sign-in failed: %s", cfg.name, exc)
                print(f"Automatic sign-in failed: {exc}")
                print("Falling back to a manual sign-in.")

        # Manual fallback: reached when there are no usable credentials or the auto
        # sign-in failed.
        page.goto(cfg.login_url)
        _dismiss_cookies(page)

        # When no sign-in control is offered, Cell already kept us signed in: save
        # the session and skip the manual step.
        if not _login_available(page):
            logger.info("%s already kept us signed in; skipping the sign-in step", cfg.name)
            save_storage(context, session)
            return True

        if debug:
            logger.debug("Debug mode: leaving sign-in to the Inspector pause")
            return False

        logger.info("Falling back to a manual %s sign-in", cfg.name)
        wait_for_human(f"Sign in to {cfg.name} in the browser window")
        save_storage(context, session)
        return True




    # # if there is a match
    # page.locator("iframe[name=\"content\"]").content_frame.get_by_role("textbox", name="Institution").fill("Caltech")  # do a fill sequentially
    # page.locator("iframe[name=\"content\"]").content_frame.locator("#ui-id-99").get_by_text("Caltech").click()
    # page.locator("iframe[name=\"content\"]").content_frame.get_by_role("button", name="Save This Joe Rich,").click()

    # # if there is not a match
    # page.locator("iframe[name=\"content\"]").content_frame.get_by_role("textbox", name="Institution").fill("Caltechxxxx")
    # page.locator("iframe[name=\"content\"]").content_frame.get_by_role("button", name="Save This Joe Rich, Caltech").click()
    # page.locator("iframe[name=\"content\"]").content_frame.get_by_role("button", name="OK").click()