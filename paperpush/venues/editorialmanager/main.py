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
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from ... import credentials
from ...database import get_venue
from ...validate import parse_authors
from ..base import Venue
from ..common import (DEFAULT_TIMEOUT_SECONDS, _try, apply_default_timeouts,
                      hold_open, hold_open_on_failure, open_run_context)
from ..common import parse_pipe_funders as _parse_funders
from ..common import save_storage
from ..common import split_name_first_last as _split_name
from ..common import wait_for_human
from ..login import VenueLoginError, login_orcid

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Variant:
    """Per-venue configuration for one Editorial Manager deployment.

    Each toggle gates one control that drifts between deployments:
    ``cover_letter_label`` (the cover-letter item type, matched leniently
    against the menu's options by :func:`_select_matching_option`, so only a
    genuinely different *name* needs an entry here -- not a different ``*``
    prefix or capitalisation), the
    ``ask_*`` radios on the declarations page, ``alternate_contact_mode``
    (``"radio_textbox"`` / ``"textbox"`` / ``"single_field"``),
    ``has_comments_page``, ``open_access_only``, ``has_section_classifications``
    (PLOS Section/Category + Classifications step), ``annotate_manuscript_manually``,
    ``plos_declarations`` (PLOS's separate ``QR23_1_Q*`` question set, driving
    :func:`_answer_declarations_plos` instead of :func:`_answer_declarations`), and
    ``no_funding_label`` (the checkbox label used to declare no funding).

    Signing in is not a toggle here: every Editorial Manager deployment offers
    the same credential form and the same "Login using ORCID" link, and the one
    thing that differs -- the Cell Press panel leads with an "Elsevier account
    login" button and hides the form behind an "Alternatively..." link, PLOS
    shows the form directly -- is detected on the page rather than declared (see
    :meth:`EditorialManagerVenue.login`).
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
    no_funding_label: str = "Funding information is not"

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
        no_funding_label="The author(s) received no",
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

# The ORCID hand-off, offered on the same sign-in panel as the credential form.
# It opens ORCID in a popup window; the shared login_orcid helper detects that.
LOGIN_ORCID_LINK_NAME = "Login using ORCID"

# The Cell Press sign-in panel (Cell, Cell Genomics, Cell Systems) now leads
# with an Elsevier-account button and shows the username/password form only
# after this link is clicked. PLOS still shows the form directly, so both are
# looked for rather than configured per Variant.
ELSEVIER_LOGIN_BUTTON_NAME = "Elsevier account login"
EM_FORM_REVEAL_TEXT = "Alternatively, use your username and password"

# Elsevier's own sign-in (id.elsevier.com), which the button navigates to in
# the same tab. After the email is entered, the next page is one of three:
# a password field for a plain Elsevier account, an institutional sign-in
# button for an account tied to a university login (Shibboleth / two-factor,
# not automatable), or the Register form when Elsevier does not know the email.
ELSEVIER_EMAIL_LABEL = "Email"
ELSEVIER_CONTINUE_NAME = "Continue"
ELSEVIER_PASSWORD_LABEL = "Password"  # field label on Elsevier's form, not a credential  # nosec B105
ELSEVIER_SIGN_IN_NAME = "Sign in"
ELSEVIER_INSTITUTION_BUTTON_NAME = "Access through your institution"
ELSEVIER_REGISTER_PAGE_BUTTON_NAME = "I already have an account"
# Offered instead of the email field when Elsevier remembers an earlier account
# in this browser; clicking it brings the email field back.
ELSEVIER_OTHER_ACCOUNT_LINK_NAME = "Try Another way"

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


def _click_first(locators, what: str, timeout_ms: int = 5000) -> bool:
    """Click the first of ``locators`` that accepts a click; log and skip if none do.

    :func:`_try` covers a control that may be absent; this covers a control that
    is definitely there but whose selector drifts between deployments -- an
    accordion header rendered as a ``button`` on one journal and a ``tab`` on
    another, a popup button labelled "Add" here and "Add->" there. Candidates are
    tried in order and the first successful click wins, so the most specific
    spelling goes first.
    """
    last: Exception | None = None
    for locator in locators:
        try:
            locator.click(timeout=timeout_ms)
            return True
        except Exception as exc:  # noqa: BLE001 -- try the next spelling
            last = exc
    logger.warning("skipped %s (no candidate matched: %s)", what, last)
    return False


def _normalized(text: str) -> str:
    """Fold a drop-down label for comparison: no ``*``, collapsed spaces, lower case."""
    return " ".join((text or "").replace("*", " ").split()).lower()


def _select_matching_option(select, wanted: str) -> str:
    """Select the option of ``select`` that matches ``wanted``; return its label.

    Editorial Manager's menu labels drift between deployments -- a leading ``*``
    marks a required item type on some journals and not others, and the
    capitalisation of a multi-word label is not stable ("Cover letter" vs "Cover
    Letter") -- so ``select_option(label=...)``, which demands the whole string
    verbatim, silently skipped every type it could not match to the character.
    A numeric ``wanted`` is passed straight through as the option's value (how
    the recordings address these menus); anything else is matched against the
    live option list on the folded label (see :func:`_normalized`), exactly
    first and then as a substring. Raises ``ValueError`` naming the options that
    were on offer when nothing matches, so the caller's :func:`_try` says why.
    """
    # evaluate_all() does not auto-wait, so a menu that is still rendering would
    # otherwise read back as "no options" rather than being waited for.
    select.wait_for(state="visible")
    if wanted.isdigit():
        select.select_option(wanted)
        return wanted

    options = select.locator("option").evaluate_all("opts => opts.map(o => ({label: o.label || o.textContent || '', value: o.value}))")
    target = _normalized(wanted)
    match = [o for o in options if _normalized(o["label"]) == target] or [o for o in options if target and target in _normalized(o["label"])]
    if not match:
        offered = [o["label"].strip() for o in options if o["value"]]
        raise ValueError(f"{wanted!r} is not one of {offered}")
    select.select_option(value=match[0]["value"])
    return match[0]["label"].strip()


def _open_section(cf, name: str) -> bool:
    """Open one section of a wizard step's accordion (best-effort).

    The manuscript-data and section/category steps are accordions whose headers
    Editorial Manager now renders as ``button`` elements named for the section
    *plus* its instruction text ("Keywords Please provide ...", "Funding
    Information Please ..."), so the header is matched on the section name as a
    substring. Older deployments render the same headers as ARIA tabs, and some
    as plain text, so both are kept as fallbacks -- looking only for a ``tab``
    is what left Keywords and Funding Information unopened, and so unfilled.
    """
    return _click_first(
        [
            cf.get_by_role("button", name=name).first,
            cf.get_by_role("tab", name=name).first,
            cf.get_by_text(name, exact=True).first,
        ],
        f"open the {name} section",
        timeout_ms=4000,
    )


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
    """Enter a funder, picking its autocomplete suggestion when one appears.

    The funder box is backed by the Open Funder Registry: typing "NIH" offers a
    list of registry entries and the form only records a funder that was picked
    from it. The name is typed key by key (the suggestions are fetched from key
    events, so a plain ``fill`` offers none) and the first suggestion clicked;
    when none appears the typed text is left alone, and the "use what I typed"
    dialog that Editorial Manager then raises on save is answered by the caller.
    """
    textbox = cf.get_by_role("textbox", name="Find a Funder:")
    textbox.click()
    textbox.fill("")
    textbox.press_sequentially(value, delay=50)

    suggestions = cf.locator("ul.ui-autocomplete:visible li")
    try:
        suggestions.first.wait_for(state="visible", timeout=2000)
        suggestions.first.click()
    except PlaywrightTimeoutError:
        logger.warning("No funder suggestion appeared for %r; leaving typed text", value)


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


def _first_visible(page, locators, timeout_ms: int):
    """The first of ``locators`` that is visible within ``timeout_ms``, or ``None``.

    Polls every candidate together rather than waiting on each in turn (the
    shared :func:`~paperpush.venues.login.first_present` does the latter), so
    a panel whose controls appear in any order is found as soon as one does.
    The sign-in controls here are spread over two frames, which rules out a
    single combined locator. ``timeout_ms=0`` is a single pass.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for locator in locators:
            try:
                if locator.first.is_visible():
                    return locator.first
            except Exception:  # noqa: BLE001 -- a bad candidate is just "not present"
                continue
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(250)


def _panel_controls(page) -> list:
    """The controls that show once the sign-in panel is open, any one of which
    means it is: the username field (directly in the content frame for Cell,
    one iframe deeper for PLOS), or the Cell Press panel's Elsevier button and
    the link that reveals its username/password form."""
    cf = _content(page)
    return [
        cf.get_by_role("textbox", name="username"),
        cf.frame_locator(LOGIN_FRAME).get_by_role("textbox", name="username"),
        cf.get_by_role("button", name=ELSEVIER_LOGIN_BUTTON_NAME),
        cf.get_by_text(EM_FORM_REVEAL_TEXT),
    ]


def _login_available(page, timeout_ms: int = 4000) -> bool:
    """True when a sign-in control is shown (Cell sometimes lands already
    signed in, offering none)."""
    splash = _content(page).get_by_role("button", name="Log In")
    return _first_visible(page, [splash, *_panel_controls(page)], timeout_ms) is not None


def _reveal_login_panel(page, timeout_ms: int) -> None:
    """Get the sign-in panel showing.

    Cell hides the whole panel behind a splash "Log In" button; PLOS shows it
    directly. Best-effort: when no panel control appears even after the click,
    the caller's own lookup reports what is missing.
    """
    splash = _content(page).get_by_role("button", name="Log In")
    if _first_visible(page, [splash, *_panel_controls(page)], timeout_ms) is None:
        return
    if not splash.first.is_visible():
        return  # the panel itself is what showed
    _try(lambda: splash.click(timeout=5000), "reveal login panel")
    _first_visible(page, _panel_controls(page), timeout_ms)


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


def _username_frame(page, timeout_ms: int):
    """The frame locator (content frame or nested login iframe) showing a
    username field, or ``None`` if neither does within ``timeout_ms``."""
    cf = _content(page)
    frames = (cf, cf.frame_locator(LOGIN_FRAME))
    fields = [frame.get_by_role("textbox", name="username") for frame in frames]
    if _first_visible(page, fields, timeout_ms) is None:
        return None
    for frame, field in zip(frames, fields):
        if field.first.is_visible():
            return frame
    return None


def _login_form(page, timeout_ms: int):
    """Return the frame locator holding the username/password form.

    Probes the content frame and the nested login iframe for a visible username
    field. The Cell Press panel keeps that form collapsed behind its
    "Alternatively, use your username and password" link, so when nothing shows
    at first the link is clicked and the probe repeated. ``None`` if the form
    never appears within ``timeout_ms`` (the panel is likely still behind an
    unclicked splash "Log In" button, see :func:`_reveal_login_panel`).
    """
    frame = _username_frame(page, 0)
    if frame is not None:
        return frame
    reveal = _content(page).get_by_text(EM_FORM_REVEAL_TEXT)
    if _first_visible(page, [reveal], 3000) is not None:
        _try(lambda: reveal.click(timeout=3000), "reveal username/password form")
    return _username_frame(page, timeout_ms)


def _login_elsevier(page, email: str, password: str, *, cfg: Variant, timeout_ms: int) -> bool:
    """Sign in through the Cell Press panel's "Elsevier account login" button.

    The button navigates this tab to Elsevier's sign-in, which asks for the
    email first and only then shows what that account needs. Returns ``True``
    once the password has been submitted there (the caller checks that the
    author area actually loaded), and ``False`` when Elsevier does not know the
    email -- its Register form came up -- so the caller can try the same pair on
    the Editorial Manager form instead. An account that Elsevier routes through
    an institutional sign-in cannot be driven here: the institution picker is
    opened and left for the human, and :class:`EditorialManagerLoginError`
    says so. The email field is typed key by key rather than filled, because
    Elsevier enables "Continue" from key events.
    """
    button = _content(page).get_by_role("button", name=ELSEVIER_LOGIN_BUTTON_NAME)
    try:
        button.click(timeout=5000)
    except PWTimeout:
        logger.debug("No '%s' button on the %s sign-in panel", ELSEVIER_LOGIN_BUTTON_NAME, cfg.name)
        return False
    page.wait_for_load_state()
    _try(lambda: page.get_by_role("button", name="Accept all cookies").click(timeout=3000), "Elsevier cookie banner")
    _try(lambda: page.get_by_role("link", name=ELSEVIER_OTHER_ACCOUNT_LINK_NAME).click(timeout=1500), "Elsevier 'Try another way'")

    email_box = page.get_by_role("textbox", name=ELSEVIER_EMAIL_LABEL)
    try:
        email_box.wait_for(state="visible", timeout=timeout_ms)
    except PWTimeout as exc:
        raise EditorialManagerLoginError(f"the Elsevier sign-in page that {cfg.name}'s '{ELSEVIER_LOGIN_BUTTON_NAME}' button " "opens did not show its email field (Elsevier may have changed the page); " f"re-capture the selectors with 'playwright codegen {cfg.login_url}'") from exc
    email_box.click()
    email_box.press_sequentially(email, delay=20)
    page.get_by_role("button", name=ELSEVIER_CONTINUE_NAME).click(timeout=timeout_ms)

    password_box = page.get_by_role("textbox", name=ELSEVIER_PASSWORD_LABEL)
    institution = page.get_by_role("button", name=ELSEVIER_INSTITUTION_BUTTON_NAME)
    register = page.get_by_role("button", name=ELSEVIER_REGISTER_PAGE_BUTTON_NAME)
    if _first_visible(page, [password_box, institution, register], timeout_ms) is None:
        raise EditorialManagerLoginError(f"after entering {email}, the Elsevier sign-in page showed neither a password " "field nor an institutional sign-in button (Elsevier may have changed the " "page or added a step); finish signing in by hand in the browser window")
    if register.first.is_visible():
        logger.info("Elsevier does not know %s (it offered to register the address); " "not an Elsevier account", email)
        return False
    if password_box.first.is_visible():
        password_box.first.fill(password)
        page.get_by_role("button", name=ELSEVIER_SIGN_IN_NAME).click()
        return True
    # Only the institutional route is offered: open its picker for the human.
    _try(lambda: institution.first.click(timeout=5000), "open the institutional sign-in")
    raise EditorialManagerLoginError(f"Elsevier signs {email} in through your institution ('{ELSEVIER_INSTITUTION_BUTTON_NAME}'), " "which paperpush cannot drive: in the browser window pick your institution, " "sign in there (including any two-factor prompt), and wait for the " f"{cfg.name} author area to load")


# The article-type drop-down on the first wizard step. Editorial Manager now
# renders it as a plain ``<select>`` in an accordion (labelled by the accordion
# header, not a tab panel), with the same id on every deployment; only its
# option labels and values differ per journal (see each venue's ``article_type``
# options in ``venues.json``).
SEL_ARTICLE_TYPE = "#ddlArticleType"


def _select_article_type(cf, article_type: str) -> None:
    """Pick the article type; a numeric value is the option value, else the label."""
    select = cf.locator(SEL_ARTICLE_TYPE)
    _try(lambda: _select_matching_option(select, article_type), f"article type {article_type!r}")
    cf.get_by_role("button", name=" Proceed").click()


# The "Select Item Type" menu above the Browse button, which applies to the
# *next* file attached. Every file already attached carries its own copy of the
# same menu further down the page (``select.submissionItemDropDown``, id
# ``fileType_<n>``), sharing the accessible name, so the top menu is always
# addressed as ``.first``: without that the locator matches several elements as
# soon as one file is attached and Playwright's strict mode makes every item
# type after the first silently skip.
ITEM_TYPE_LABEL = "Select Item Type"
SEL_ROW_ITEM_TYPE = "select.submissionItemDropDown"
SEL_ROW_DESCRIPTION = "input[id^='description_']"

# Item types asked for by name. They are matched leniently against the menu's
# live option list (see :func:`_select_matching_option`), so the ``*`` some
# journals prefix onto a required type and the capitalisation of the second
# word do not have to be spelled the way each deployment happens to render it.
DECLARATION_ITEM_TYPE = "Declaration of Interests"
FIGURE_ITEM_TYPE = "Figure"

# PLOS is the one deployment that gives its main document an explicit item type
# rather than letting the first attachment default to it (option value 6,
# "Manuscript"), and wants that row described.
PLOS_MANUSCRIPT_ITEM_TYPE = "6"


def _upload(page, cf, path: str) -> None:
    """Attach one file: "Browse..." is a button (not a file input), so intercept
    the native file chooser it opens and hand it the file."""
    with page.expect_file_chooser() as fc_info:
        cf.get_by_role("button", name="Browse...").click()
    fc_info.value.set_files(path)


def _attached_row(cf, path: str) -> tuple[int, str]:
    """Locate the attached-files row holding ``path``: its index and file id.

    Editorial Manager does not append a new attachment to the end of the list --
    it re-sorts the rows into the journal's submission-item order, so on Cell the
    cover letter lands *above* the manuscript uploaded before it. Addressing "the
    last row" therefore corrected the wrong file (it retyped the manuscript as a
    cover letter), so the row is found by the file name shown in it instead.
    Returns ``(-1, "")`` when no row names that file, which is the signal to
    leave every row alone rather than guess at one.
    """
    name = Path(path).name.lower()
    menus = cf.locator(SEL_ROW_ITEM_TYPE)
    index, file_id = menus.evaluate_all(
        """(els, name) => {
            const rowText = el => ((el.closest('tr') || el.parentElement || {}).textContent || '').toLowerCase();
            const i = els.findIndex(el => rowText(el).includes(name));
            return [i, i < 0 ? '' : (els[i].id || '')];
        }""",
        name,
    )
    return int(index), str(file_id)


def _ensure_row_item_type(cf, path: str, item_type: str) -> None:
    """Make the row for ``path`` carry ``item_type``.

    The top menu is meant to apply to the next file chosen, but some deployments
    reset it to the placeholder as the upload starts, leaving the new row on the
    default type. That row's own copy of the menu is read back here and
    corrected only when it did not come out as asked -- a no-op when the type
    already took, which is the usual case.
    """
    index, _ = _attached_row(cf, path)
    if index < 0:
        logger.warning("No attached row names %s; leaving the item types alone", Path(path).name)
        return
    row = cf.locator(SEL_ROW_ITEM_TYPE).nth(index)
    if item_type.isdigit():
        if row.input_value() == item_type:
            return
    else:
        current = _normalized(row.evaluate("el => { const o = el.options[el.selectedIndex]; return o ? (o.label || o.text) : ''; }"))
        if current and _normalized(item_type) in current:
            return
    _select_matching_option(row, item_type)


def _ensure_row_description(cf, path: str, description: str) -> None:
    """Make the row for ``path`` carry ``description``.

    A row's controls share one Editorial Manager file id -- its item-type menu is
    ``fileType_<id>`` and its Description box ``description_<id>`` -- so the box
    is addressed from the id of the menu :func:`_attached_row` matched, falling
    back to the row's position when the ids are not paired that way.
    """
    index, file_id = _attached_row(cf, path)
    if index < 0:
        logger.warning("No attached row names %s; leaving the descriptions alone", Path(path).name)
        return
    suffix = file_id.split("_", 1)[1] if "_" in file_id else ""
    box = cf.locator(f"#description_{suffix}") if suffix else None
    if box is None or not box.count():
        box = cf.locator(SEL_ROW_DESCRIPTION).nth(index)
    if box.input_value().strip() != description:
        box.fill(description)


def _attach_one(page, cf, path: str, item_type: str, what: str, description: str = "") -> None:
    """Attach one file under ``item_type``, with an optional Description.

    Editorial Manager applies the item-type menu above the Browse button to the
    *next* file chosen, so the type (and description) are set first and the file
    picked second -- the order the recordings use. Afterwards *this file's own*
    row is read back and corrected where either did not take (see
    :func:`_attached_row`), since some deployments clear the top controls as the
    upload starts. An empty ``item_type`` leaves the menu alone, which is how the
    Cell family's manuscript takes the default main-document type.
    """
    logger.info("Uploading %s %s%s", what, path, f" (description={description})" if description else "")
    if item_type:
        _try(lambda: _select_matching_option(cf.get_by_label(ITEM_TYPE_LABEL).first, item_type), f"{what} item type {item_type!r}")
        page.wait_for_timeout(1000)
    if description:
        _try(lambda: cf.get_by_role("textbox", name="Description").first.fill(description), f"{what} description")
    _upload(page, cf, path)
    page.wait_for_timeout(2000)
    if item_type:
        _try(lambda: _ensure_row_item_type(cf, path, item_type), f"{what} item type on the attached row")
    if description:
        _try(lambda: _ensure_row_description(cf, path, description), f"{what} description on the attached row")


def _attach_files(page, cf, manuscript_file: str, cover_letter: str, declaration_file: str, figure_files: list[tuple[str, str]] | None = None, cfg: Variant = _DEFAULT) -> None:
    """Upload the manuscript, cover letter, optional declaration, and figures.

    Each attachment goes through :func:`_attach_one`, which sets the item type
    for the next file and reads the resulting row back. The manuscript takes the
    default (main document) type except where the deployment asks for one
    explicitly (PLOS); a figure's label, when present, becomes its Description.
    """
    # close popups - one for the "generated with AI" popup, and another for the writing workshop popup
    for _ in range(2):
        try:
            page.wait_for_timeout(2000)
            page.get_by_role("button", name="Close").click(timeout=4000)
        except:  # Button didn't appear, continue
            pass

    # The manuscript takes the main-document type: the default on the Cell
    # family, an explicit type on deployments that ask for one (PLOS).
    _attach_one(page, cf, manuscript_file, PLOS_MANUSCRIPT_ITEM_TYPE if cfg.annotate_manuscript_manually else "", "manuscript", description="Manuscript" if cfg.annotate_manuscript_manually else "")

    _try(lambda: page.get_by_role("button", name="Close").click(timeout=3000), "Close popup")

    if cover_letter:
        _attach_one(page, cf, cover_letter, cfg.cover_letter_label, "cover letter")

    if declaration_file:
        _attach_one(page, cf, declaration_file, DECLARATION_ITEM_TYPE, "declaration of interests")

    for figure, label in figure_files or []:
        _attach_one(page, cf, figure, FIGURE_ITEM_TYPE, "figure", description=label)

    _try(lambda: page.get_by_role("button", name="Close").click(timeout=3000), "Close popup")

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


def _enter_metadata(cf, title: str, abstract: str, keywords: str, short_title: str = "") -> None:
    """Enter title, abstract, and keywords on the manual-entry metadata step.

    The step is an accordion: the title section is open on arrival (and carries
    the Short Title box on the deployments that ask for one -- PLOS requires
    it), while Abstract and Keywords have to be opened by their header first
    (see :func:`_open_section`).
    """
    cf.get_by_role("button", name="Enter Data Manually").click()
    _try(lambda: cf.get_by_role("button", name="Yes, Enter Data Manually").click(), "confirm manual entry")

    if title:
        title_editor = cf.frame_locator('iframe[title="Rich Text Editor, fullTitleHtml"]')
        _try(lambda: _fill_rich_text(title_editor, title), "title")
    if short_title:
        # A plain textbox alongside the rich-text full title, not its own section.
        _try(lambda: cf.get_by_role("textbox", name="Short Title").first.fill(short_title), "short title")
    if abstract:
        # Abstract lives in its own accordion section; open it before the editor
        # iframe is interactable. #tlblAbstractTitle is the header's own id,
        # tried first because it is unambiguous where it exists.
        _click_first([cf.locator("#tlblAbstractTitle").first, cf.get_by_role("button", name="Abstract").first], "open the Abstract section", timeout_ms=4000)
        abstract_editor = cf.frame_locator('iframe[title="Rich Text Editor, abstractHtml"]')
        _try(lambda: _fill_rich_text(abstract_editor, abstract), "abstract")
    if keywords:
        _open_section(cf, "Keywords")
        _try(lambda: cf.locator("#txtKeywords").fill(keywords), "keywords")


def _parse_classifications(value: str) -> list[str]:
    """Parse the ``classifications`` field into a list of keywords (one per line)."""
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def _check_classification(popup, keyword: str) -> None:
    """Tick one classification in the Classifications popup.

    The popup lists classifications as ARIA ``treeitem`` rows, and a row's
    checkbox is labelled by the row as a whole (its number, its name, and its
    parent's), not by the classification on its own -- so looking the checkbox
    up by the keyword alone finds nothing and the step timed out on the first
    keyword. The row is located by name and its checkbox ticked from inside it;
    the flat checkbox list older deployments render is the last candidate.
    """
    row = popup.get_by_role("treeitem", name=keyword).first
    for box in (row.locator('input[type="checkbox"]').first, row.get_by_label(keyword).first, popup.get_by_role("checkbox", name=keyword).first):
        try:
            box.check(timeout=4000)
            return
        except Exception:  # noqa: BLE001 -- try the next spelling
            continue
    raise ValueError(f"no checkbox for classification {keyword!r} in the popup")


def _enter_section_classifications(page, cf, section: str, classifications: list[str]) -> None:
    """Set the PLOS Section/Category drop-down and add classifications (PLOS only).

    Both live on one accordion step. The Section/Category header is a landmark
    ``region`` on the current deployment (it used to be a ``tab``), so the menu
    is looked up under either and, failing that, on the page. Classifications
    open a separate popup: each keyword is searched, ticked in the result tree
    (see :func:`_check_classification`) and added, then the popup is submitted
    and closed. Best-effort throughout (see :func:`_try`); the step is always
    left with a Proceed so an empty classification list still advances.
    """
    page.wait_for_timeout(2000)

    if section:
        for role in ("region", "tabpanel"):
            container = cf.get_by_role(role, name="Section/Category")
            if container.count():
                select = container.first.get_by_label("Section/Category").first
                break
        else:
            select = cf.get_by_label("Section/Category").first
        _try(lambda: _select_matching_option(select, section), f"section {section!r}")

    if classifications:
        # Open the Classifications section, then its "Add Classifications" popup.
        _open_section(cf, "Classifications")
        _try(lambda: _add_classifications(page, cf, classifications), "add classifications")

    cf.get_by_role("button", name=" Proceed").click()


def _add_classifications(page, cf, classifications: list[str]) -> None:
    """Drive the Classifications popup for every keyword, then submit it."""
    with page.expect_popup() as popup_info:
        cf.get_by_role("button", name="Add Classifications").click()
    popup = popup_info.value

    for keyword in classifications:

        def _add(kw=keyword) -> None:
            search = popup.get_by_role("textbox", name="Search:")
            search.click()
            search.fill(kw)
            popup.get_by_role("button", name="Search").click()
            popup.wait_for_timeout(1000)
            _check_classification(popup, kw)
            # The move-into-selection button is "Add" on the current deployment
            # and "Add->" on older ones.
            _click_first([popup.get_by_role("button", name="Add", exact=True), popup.get_by_role("button", name="Add->"), popup.get_by_role("button", name="Add").first], f"add classification {kw!r} to the selection")

        _try(_add, f"add classification {keyword!r}")

    # Submit the popup's selected classifications and close the window.
    _click_first([popup.locator("#btnSubmit2"), popup.get_by_role("button", name="Submit").first], "submit classifications", timeout_ms=8000)
    _try(lambda: popup.close(), "close Classifications popup")


def _is_corresponding(author: dict) -> bool:
    """True when an author line is marked as the corresponding author."""
    return str(author.get("corresponding", "")).strip().lower() in {"yes", "y", "true", "1", "on"}


def _click_save(cf, name: str) -> None:
    """Click the save control of an inline Editorial Manager form.

    The wizard renders these as a toolbar icon (``.fl-flToolSave``); some
    deployments label the same control with text instead ("Save This Author",
    "Save This Award Number: ..."), so the icon is tried first and the named
    button second.
    """
    try:
        cf.locator(".fl-flToolSave:visible").first.click(timeout=3000)
    except Exception:  # noqa: BLE001 -- fall back to the labelled button
        cf.get_by_role("button", name=name).first.click(timeout=5000)


def _fill_author_address(cf, author: dict) -> None:
    """Fill the address half of an author form: institution, department, zip, country.

    Only the columns the venue's ``authorlist`` declares are present in
    ``author``, so a venue that does not collect a department simply has nothing
    to fill. PLOS makes Department and Zip or Postal Code required and rejects
    the form without them; both boxes only render once the institution has been
    entered, which is why they are filled after it.
    """
    if author.get("institution"):
        _try(lambda: _pick_or_enter_institution(cf, author["institution"]), "author institution")
    if author.get("department"):
        _try(lambda: cf.get_by_role("textbox", name="Department").first.fill(author["department"]), "author department")
    if author.get("zip_code"):
        _try(lambda: cf.get_by_role("textbox", name="Zip or Postal Code").first.fill(author["zip_code"]), "author zip or postal code")
    if author.get("country"):
        _try(lambda: cf.get_by_label("Country or Region *").select_option(_country_label(author["country"])), "author country")


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
    corresponding, the first line is treated as the pre-filled row. Either way
    the address half of the form goes through :func:`_fill_author_address`, so a
    venue that collects a department and postal code (PLOS makes both required)
    gets them from the ``.sub``'s author columns.
    """
    if not authors:
        return

    _open_section(cf, "Authors")

    # The pre-filled row is the corresponding author; fall back to the first line.
    corresponding = next((a for a in authors if _is_corresponding(a)), authors[0])

    for author in authors:
        page.wait_for_timeout(2000)
        if author is corresponding:

            def _edit(a=author) -> None:
                cf.get_by_role("button", name="Edit This Author").click()
                _fill_author_address(cf, a)
                _click_save(cf, "Save This Author")

            _try(_edit, f"edit corresponding author {author.get('name', '')}".strip())
            continue

        first, last = _split_name(author.get("name", ""))

        def _add(a=author, first=first, last=last) -> None:
            cf.get_by_role("button", name="+Add Another Author").nth(1).click()
            cf.get_by_role("textbox", name="Given/First Name *").fill(first)
            cf.get_by_role("textbox", name="Family/Last Name *").fill(last)
            cf.get_by_role("textbox", name="E-mail Address *").fill(a.get("email", ""))
            _fill_author_address(cf, a)
            _click_save(cf, "Save This Author")

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
    # True when nothing had to be typed free-hand, so no confirmation is due.
    institution_clicked = True
    if reviewer.get("institution"):
        institution_clicked = _pick_or_enter_institution(cf, reviewer["institution"])
    if reviewer.get("email"):
        _try(lambda: cf.get_by_role("textbox", name="E-mail").fill(reviewer["email"]), "reviewer email")
    if reviewer.get("reason"):
        _try(lambda: cf.get_by_role("textbox", name="Reason").fill(reviewer["reason"]), "reviewer reason")
    _click_save(cf, "Save This Reviewer")
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


def _enter_funding(page, cf, funders: list[dict], cfg: Variant = _DEFAULT) -> None:
    """Fill the Funding Information step, or mark it not available.

    Each funder is added via "+Add a Funding Source" (see :func:`_pick_funder`),
    then the optional award number. With no funders, the no-funding declaration
    box is checked instead; its label drifts between deployments (see
    ``cfg.no_funding_label``). The step is an accordion section that has to be
    opened first (see :func:`_open_section`) -- looking for an ARIA tab left it
    shut, and everything filled below went nowhere.
    """
    _open_section(cf, "Funding Information")
    page.wait_for_timeout(1000)

    if not funders:
        _try(lambda: cf.get_by_role("checkbox", name=cfg.no_funding_label).check(), "no-funding declaration")
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


def editorialmanager_run(values: dict, headless: bool = False, debug: bool = False, new_session: bool = False, timeout: float = DEFAULT_TIMEOUT_SECONDS, keep_open_on_failure: bool = True, *, venue) -> None:
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
    # Only the deployments that ask for one define this field (PLOS requires it).
    short_title = values.get("short_title", "").strip()
    abstract = values.get("abstract", "")
    keywords = values.get("keywords", "").strip()
    section = values.get("section", "").strip()
    classifications = _parse_classifications(values.get("classifications", ""))
    authors = _parse_authors(values.get("authors", ""), cfg)
    reviewers = _split_reviewers(values.get("reviewers", ""))
    funders = _parse_funders(values.get("funding", ""))

    logger.info("Starting %s submission run (headless=%s, debug=%s, type=%s, " "manuscript=%s, authors=%d, reviewers=%d, figures=%d)", cfg.name, headless, debug, article_type, manuscript_file, len(authors), len(reviewers), len(figure_files))

    with sync_playwright() as playwright, hold_open_on_failure(headless=headless, keep_open=keep_open_on_failure):
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
        _enter_metadata(cf, title, abstract, keywords, short_title)
        _enter_authors(page, cf, authors)
        _enter_funding(page, cf, funders, cfg=cfg)

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
    #: Every Editorial Manager deployment offers "Login using ORCID" on its
    #: sign-in panel, so this holds for the whole portal rather than per variant.
    supports_orcid_login = True

    # display_name is inherited: the base returns get_venue(slug).name == variant.name.

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
        editorialmanager_run(
            values,
            headless=headless,
            debug=debug,
            new_session=new_session,
            timeout=timeout,
            keep_open_on_failure=keep_open_on_failure,
            venue=self,
        )

    def login(self, page, username: str, password: str, *, orcid: bool = False, timeout_ms: int = 15000) -> None:
        """Sign in to Editorial Manager from stored credentials.

        Both ways in start from the same page and end with the same check, so
        only the middle differs: by default the credential form, with ``orcid``
        the "Login using ORCID" link handed to the shared
        :func:`~paperpush.venues.login.login_orcid`, in which case
        ``username``/``password`` are the author's ORCID iD and ORCID password.
        Cell hides the whole sign-in panel behind a splash "Log In" button (PLOS
        shows it directly) and *both* controls live on that panel, so it is
        revealed once, before the branch. The ORCID link is looked for in the
        content frame and in the nested login iframe, the same two roots
        :func:`_login_form` probes for the credential form, since PLOS renders its
        panel one level deeper than Cell does. On the Cell Press panel the
        credential branch also covers the newer "Elsevier account login" (an
        email plus Elsevier password): see :meth:`_login_with_password` for how
        the stored pair is routed. Raises :class:`EditorialManagerLoginError` if
        the author area never loads.
        """
        cfg = self.variant
        login_url = cfg.login_url
        logger.debug("Signing in to %s (Editorial Manager) at %s (orcid=%s)", cfg.name, login_url, orcid)
        page.goto(login_url)
        _dismiss_cookies(page)
        _reveal_login_panel(page, timeout_ms)

        if orcid:
            content = _content(page)
            login_orcid(
                page,
                username,
                password,
                entry=[root.get_by_role("link", name=LOGIN_ORCID_LINK_NAME) for root in (content, content.frame_locator(LOGIN_FRAME))],
                return_url=cfg.portal_url,
                venue_name=cfg.name,
                timeout_ms=timeout_ms,
                error=EditorialManagerLoginError,
            )
        else:
            self._login_with_password(page, username, password, timeout_ms=timeout_ms)

        if not self.is_logged_in(page, timeout_ms=timeout_ms):
            if orcid:
                # sometimes I must try one more time to click on the ORCID link
                _try(lambda: _content(page).get_by_role("button", name="Log In").click(timeout=5000), "reveal login form")
                with page.expect_popup() as page1_info:
                    _try(lambda: page.locator("iframe[name=\"content\"]").content_frame.get_by_role("link", name="Login using ORCID").click(timeout=2000), "try orcid again")  # works for Cell family
                    _try(lambda: page.locator("iframe[name=\"content\"]").content_frame.locator("iframe[name=\"login\"]").content_frame.get_by_role("link", name="Login using ORCID").click(timeout=2000), "try orcid again")  # works for PLOS family
                    
                if not self.is_logged_in(page, timeout_ms=timeout_ms):
                    raise EditorialManagerLoginError(f"signed in to ORCID but the {cfg.name} author area did not load -- the " "ORCID iD or password may be wrong, or the ORCID account may not be " f"linked to a {cfg.name} account yet (link it once by signing in by hand)")
            raise EditorialManagerLoginError(f"submitted the credentials but the signed-in {cfg.name} author area did " f"not load -- the username or password may be wrong (for the Cell Press " "journals the pair is either an Editorial Manager username/password or " f"an Elsevier account email/password), or {cfg.name} added a step " "(CAPTCHA / two-factor) that can't be automated")

    def _login_with_password(self, page, username: str, password: str, *, timeout_ms: int) -> None:
        """The username/password half of :meth:`login`, panel already revealed.

        A stored pair can belong to either of two forms on the Cell Press panel:
        the "Elsevier account login" (an email plus Elsevier password, driven by
        :func:`_login_elsevier`) or Editorial Manager's own username/password
        form. Elsevier accounts are always emails, so an email-shaped username
        is tried there first and falls through to the Editorial Manager form
        when Elsevier rejects it; anything else can only be an Editorial Manager
        username. PLOS offers no Elsevier button, so it goes straight to the
        form. Does not check that the sign-in took -- :meth:`login` does.
        """
        cfg = self.variant
        elsevier = _content(page).get_by_role("button", name=ELSEVIER_LOGIN_BUTTON_NAME)
        if "@" in username and elsevier.first.is_visible():
            logger.info("Trying %s as an Elsevier account on the %s panel", username, cfg.name)
            if _login_elsevier(page, username, password, cfg=cfg, timeout_ms=timeout_ms):
                if self.is_logged_in(page, timeout_ms=timeout_ms):
                    return
                logger.info("Elsevier did not accept the pair; trying the %s username/password form", cfg.name)
            page.goto(cfg.login_url)
            _reveal_login_panel(page, timeout_ms)

        frame = _login_form(page, timeout_ms)
        if frame is None:
            raise EditorialManagerLoginError(f"could not find the username/password fields on the {cfg.name} sign-in " "page (the Editorial Manager form may have changed); re-capture " f"the selectors with 'playwright codegen {cfg.login_url}'")
        frame.get_by_role("textbox", name="username").fill(username)
        frame.get_by_role("textbox", name="password").fill(password)
        frame.get_by_role("button", name="Author Login").click()

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

        # Shared branch: an ORCID credential signs in with orcid=True, anything
        # else through the Editorial Manager credential form.
        if self._sign_in_with_credential(page, cred):
            save_storage(context, session)
            logger.info("Signed in to %s; saved the session for reuse", cfg.name)
            print("Signed in; saved the session for next time.")
            return True

        # Manual fallback: reached when there are no usable credentials or the auto
        # sign-in failed. An Elsevier institutional sign-in fails part-way with
        # its institution picker open (see _login_elsevier); that page is left
        # for the human rather than reloading the portal over it.
        off_portal = page.url.startswith("http") and "editorialmanager.com" not in page.url
        if not off_portal:
            page.goto(cfg.login_url)
            _dismiss_cookies(page)

            # When no sign-in control is offered, Cell already kept us signed in:
            # save the session and skip the manual step.
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
