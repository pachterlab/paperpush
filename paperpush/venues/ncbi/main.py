"""NCBI's account gateway, shared by NCBI submission services.

:func:`login_ncbi` is the login helper; callers own the browser and session
persistence. :class:`NCBIVenue` wraps it as a
:class:`~paperpush.venues.base.Venue` for the NCBI services paperpush lists
(SRA, GEO), so ``paperpush login <slug>`` signs in through it. Provider
selectors were inspected on the public sign-in pages; a successful
credential/MFA round trip still needs an author's account.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout

from ..base import Venue
from ..common import DEFAULT_TIMEOUT_SECONDS, wait_for_human
from ..login import ORCID_COOKIE_NAME, VenueLoginError, first_present

NCBI_LOGIN_URL = "https://account.ncbi.nlm.nih.gov/"
NCBI_ACCOUNT_MARKER = "#account_info"
NCBI_USERNAME_MARKER = "#uname_short"
POPUP_WAIT_MS = 1500


class NCBILoginError(VenueLoginError):
    """NCBI authentication needs attention in the existing browser window."""


@dataclass(frozen=True)
class Provider:
    label: str
    hosts: tuple[str, ...]
    username: str
    password: str
    submit: str
    next: str = ""


# Scope submit selectors to the credential form: NIH also has a PIV "Sign in"
# button, Microsoft an off-screen password input, and Login.gov dynamic IDs.
PROVIDERS = {
    "ncbi": Provider("NCBI Account", ("account.ncbi.nlm.nih.gov",), "#id_username", "#id_password", 'form.login-form input[type="submit"]'),
    "era": Provider("eRA Commons", ("auth.nih.gov",), "#USER", "#PASSWORD", 'button[type="submit"][onclick*="submitForm(1)"]'),
    "google": Provider("Google Account", ("accounts.google.com",), "#identifierId", 'input[name="Passwd"]:visible', "#passwordNext", "#identifierNext"),
    "orcid": Provider("ORCiD", ("orcid.org",), "#username-input", "#password", "#signin-button"),
    "login.gov": Provider("Login.gov", ("secure.login.gov",), "#user_email", 'input[name="user[password]"]', 'button[type="submit"][name="button"]'),
    "microsoft": Provider("Microsoft", ("login.microsoftonline.com", "login.live.com"), 'input[name="loginfmt"]:visible', 'input[name="passwd"]:not([aria-hidden="true"]):visible', "#idSIButton9", "#idSIButton9"),
    "nih": Provider("NIH Account", ("auth.nih.gov",), "#USERNAME", "#PASSWORD", 'button[type="submit"][onclick*="submitForm(1)"]'),
}

_PROVIDER_ALIASES = {
    **{cfg.label.casefold(): key for key, cfg in PROVIDERS.items()},
    "era_commons": "era",
    "era commons": "era",
    "logingov": "login.gov",
    "login_gov": "login.gov",
    "mslive": "microsoft",
}


def _provider_key(value: str) -> str:
    key = value.strip().casefold()
    key = _PROVIDER_ALIASES.get(key, key)
    if key not in PROVIDERS:
        raise ValueError(f"unknown NCBI login provider {value!r}; choose from {', '.join(PROVIDERS)}")
    return key


def _ncbi_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and not parsed.username and not parsed.password and (host == "ncbi.nlm.nih.gov" or host.endswith(".ncbi.nlm.nih.gov"))


def is_logged_in_ncbi(page) -> bool:
    """Check NCBI's populated, visible account control on an HTTPS NCBI page.

    The signed-out HTML contains hidden dashboard/logout links too, so their
    mere presence, the URL, or a provider's own signed-in page is insufficient.
    """
    if page.is_closed() or not _ncbi_url(page.url):
        return False
    try:
        return page.locator(NCBI_ACCOUNT_MARKER).is_visible() and bool(page.locator(NCBI_USERNAME_MARKER).inner_text(timeout=1000).strip())
    except PWError:
        # Navigation may replace the document while the provider returns.
        return False


def _check_host(page, cfg: Provider) -> None:
    parsed = urlsplit(page.url)
    if parsed.scheme != "https" or parsed.hostname not in cfg.hosts or parsed.username or parsed.password:
        raise NCBILoginError(f"{cfg.label} redirected to an unrecognized sign-in host; finish signing in in the browser (no credentials were filled on that host)")


def _fill_credentials(page, cfg: Provider, username: str, password: str, timeout_ms: int) -> None:
    identity = page.locator(cfg.username)
    # Google and Microsoft may show a saved-account chooser first. Selecting
    # "Use another account" honors the requested identity without guessing one.
    choice = page.get_by_text("Use another account", exact=True)
    control = first_present([identity, choice, page.locator(NCBI_ACCOUNT_MARKER)], timeout_ms)
    if is_logged_in_ncbi(page):
        return  # The identity provider reused an existing session.
    if control is None:
        raise NCBILoginError(f"could not find the {cfg.label} sign-in form; the provider may require interactive sign-in")
    _check_host(page, cfg)
    if not identity.is_visible():
        control.click(timeout=timeout_ms)
    identity.wait_for(state="visible", timeout=timeout_ms)
    _check_host(page, cfg)
    if cfg.label == "ORCiD":
        cookie = page.get_by_role("button", name=ORCID_COOKIE_NAME, exact=True)
        if cookie.is_visible():
            cookie.click(timeout=timeout_ms)
    identity.fill(username, timeout=timeout_ms)
    if cfg.next:
        page.locator(cfg.next).click(timeout=timeout_ms)
    secret = page.locator(cfg.password)
    secret.wait_for(state="visible", timeout=timeout_ms)
    # Enterprise federation after the username step can leave the expected
    # provider. Do not type its password into an unrelated organization's form.
    _check_host(page, cfg)
    secret.fill(password, timeout=timeout_ms)
    page.locator(cfg.submit).click(timeout=timeout_ms)


def _wait_signed_in(page, form, timeout_ms: int) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000 if timeout_ms else float("inf")
    while True:
        if is_logged_in_ncbi(page) or (form is not page and is_logged_in_ncbi(form)):
            return True
        if time.monotonic() >= deadline or page.is_closed():
            return False
        page.wait_for_timeout(100)


def login_ncbi(
    page,
    username: str = "",
    password: str | None = None,
    *,
    provider: str = "ncbi",
    login_url: str = NCBI_LOGIN_URL,
    return_url: str | None = None,
    timeout_ms: int = 15000,
    allow_manual: bool = False,
) -> None:
    """Authenticate through NCBI using one of its seven featured providers.

    ``provider`` is ncbi, era, google, orcid, login.gov, microsoft, or nih;
    the labels on NCBI's buttons are accepted too. Credentials belong to that
    provider. Both may be omitted with ``allow_manual=True`` to let the author
    do the entire sign-in in a headed browser; otherwise both are required.

    ``login_url`` can preserve a service's NCBI gateway URL and OAuth state.
    ``return_url``, if given, is visited only AFTER an authenticated NCBI return
    has been observed. Both must be HTTPS NCBI URLs. No callback is reloaded
    prematurely and no consent, account linking, MFA, PIV, or CAPTCHA is
    accepted automatically. ``allow_manual`` permits a terminal handoff for
    those steps; it must be false for unattended/headless callers.

    Raises ``NCBILoginError`` on failure, leaving the page/popup open so the
    caller can offer manual recovery. Does not save credentials or sessions.
    """
    key = _provider_key(provider)
    cfg = PROVIDERS[key]
    if not _ncbi_url(login_url) or (return_url is not None and not _ncbi_url(return_url)):
        raise ValueError("NCBI login_url and return_url must be HTTPS NCBI URLs")
    if timeout_ms < 0:
        raise ValueError("timeout_ms must be nonnegative")
    if bool(username) != bool(password) or (not username and not allow_manual):
        raise ValueError("supply both provider username and password, or omit both with allow_manual=True")

    form = page
    try:
        page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)
        entry = page.get_by_role("link", name=cfg.label, exact=True)
        candidates = [page.locator(NCBI_ACCOUNT_MARKER), entry]
        if key == "ncbi":
            candidates.append(page.locator(cfg.username))
        first_present(candidates, timeout_ms)
        if not is_logged_in_ncbi(page):
            if key == "ncbi":
                # The legacy credential form is hidden until this is selected.
                if not page.locator(cfg.username).is_visible():
                    entry.click(timeout=timeout_ms)
            else:
                try:
                    with page.expect_popup(timeout=POPUP_WAIT_MS) as popup:
                        entry.click(timeout=timeout_ms)
                    form = popup.value
                except PWTimeout:
                    form = page  # NCBI currently redirects in the same tab.
            if username and password:
                _fill_credentials(form, cfg, username, password, timeout_ms)
                if not _wait_signed_in(page, form, timeout_ms):
                    raise NCBILoginError(f"{cfg.label} has not returned an authenticated NCBI session; check the credentials or finish MFA, CAPTCHA, consent, or account linking in the browser")
            else:
                raise NCBILoginError(f"finish the {cfg.label} sign-in in the browser")
    except (PWError, NCBILoginError) as exc:
        # A provider can reuse a session or close its popup while a form action
        # is waiting. A verified NCBI return still counts as success then.
        if not (is_logged_in_ncbi(page) or (form is not page and is_logged_in_ncbi(form))):
            if not allow_manual:
                if isinstance(exc, NCBILoginError):
                    raise
                # Raw browser errors can contain redirect query strings; keep
                # the public error free of those and typed credential values.
                raise NCBILoginError(f"NCBI sign-in with {cfg.label} did not complete; finish any MFA, CAPTCHA, consent, or account-linking step in the open browser, or check whether the sign-in form changed") from None
            if not wait_for_human(f"Finish NCBI sign-in with {cfg.label} in the browser, including any MFA, CAPTCHA, consent, or account linking, then return here"):
                raise NCBILoginError("NCBI sign-in needs an interactive browser and terminal") from None
            if not _wait_signed_in(page, form, timeout_ms):
                raise NCBILoginError("NCBI did not show an authenticated account after the manual sign-in") from None

    # A popup may complete on NCBI without closing itself. Only now is it safe
    # to reload the opener to pick up the shared cookies and close the popup.
    if form is not page:
        if not is_logged_in_ncbi(page):
            page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)
        if not _wait_signed_in(page, page, timeout_ms):
            raise NCBILoginError("the provider signed in, but the original NCBI window is still signed out")
        if not form.is_closed():
            form.close()
    if return_url is not None:
        page.goto(return_url, wait_until="domcontentloaded", timeout=timeout_ms)


class NCBIVenue(Venue):
    """A service behind NCBI's account gateway; a leaf adds the submit runner, if it has one.

    ``login`` goes through :func:`login_ncbi` with any of its seven providers
    (``provider=``, stored by ``paperpush login <slug> --provider``; ``--orcid``
    takes the ORCiD one), then opens the service's portal. A leaf module sets
    ``LOGIN_ONLY = True`` so the venue registers for login and session capture
    without claiming ``paperpush submit`` support (GEO); a leaf that overrides
    :meth:`submit` drops it (SRA).

    The service portals do not carry NCBI's ``#account_info`` control, so
    :meth:`is_logged_in` reads a portal by its own header: on the portal, with
    the header rendered and the sign-in link (:attr:`signed_out_selector`)
    gone. NCBI's account page itself is read by :func:`is_logged_in_ncbi`.
    """

    login_providers = tuple(PROVIDERS)
    supports_orcid_login = True
    #: The portal's sign-in link, shown only while signed out.
    signed_out_selector: str = ""
    #: The header block that holds that link (and the account control when signed in).
    header_selector: str = ""

    @property
    def login_url(self) -> str:
        """NCBI's gateway with the portal as ``back_url``, as the portal's own sign-in link builds it."""
        return f"{NCBI_LOGIN_URL}?back_url={quote(self.portal_url, safe=':/')}"

    def login(self, page, username: str, password: str, *, orcid: bool = False, provider: str = "ncbi", timeout_ms: int = 15000) -> None:
        login_ncbi(page, username, password, provider="orcid" if orcid else (provider or "ncbi"), return_url=self.portal_url, timeout_ms=timeout_ms)

    def is_logged_in(self, page, *, timeout_ms: int | None = None) -> bool:
        if page.is_closed() or not _ncbi_url(page.url):
            return False
        if is_logged_in_ncbi(page):
            return True
        if not page.url.startswith(self.portal_url):
            return False
        timeout = self.logged_in_timeout_ms if timeout_ms is None else timeout_ms
        try:
            page.locator(self.header_selector).first.wait_for(state="attached", timeout=timeout)
            return not page.locator(self.signed_out_selector).first.is_visible()
        except PWError:
            return False

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
        raise NotImplementedError(f"{self.slug} has no submission runner yet; 'paperpush login {self.slug}' stores the NCBI sign-in only")
