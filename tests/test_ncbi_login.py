"""NCBI provider dispatch, staged forms, callback verification, and handoffs.

The pages model the selectors inspected on NCBI and its identity providers;
there are no credentials, browsers, network calls, or real account changes.
"""

from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from playwright.sync_api import TimeoutError as PWTimeout

from paperpush.venues import SLUG_TO_MODULE
from paperpush.venues.ncbi import main as ncbi


class Locator:
    def __init__(self, page, key):
        self.page, self.key = page, key

    @property
    def first(self):
        return self

    def is_visible(self):
        return self.key in self.page.visible

    def wait_for(self, **kwargs):
        if not self.is_visible():
            raise PWTimeout("control not visible")

    def click(self, **kwargs):
        self.wait_for()
        self.page.actions.append(("click", self.key))
        callback = self.page.clicks.get(self.key)
        if callback:
            callback()

    def fill(self, value, **kwargs):
        self.wait_for()
        self.page.actions.append(("fill", self.key, value))

    def inner_text(self, **kwargs):
        return self.page.username


class Page:
    def __init__(self, key="ncbi", *, popup=None, completes=True, chooser=False):
        self.key, self.cfg = key, ncbi.PROVIDERS[key]
        self.url = "about:blank"
        self.visible, self.clicks = set(), {}
        self.actions, self.visits = [], []
        self.username = ""
        self.closed = False
        self.popup, self.completes, self.chooser = popup, completes, chooser

    def locator(self, key):
        return Locator(self, key)

    def get_by_role(self, role, name, **kwargs):
        return Locator(self, f"{role}:{name}")

    def get_by_text(self, text, **kwargs):
        return Locator(self, text)

    def goto(self, url, **kwargs):
        self.visits.append(url)
        self.url = url
        if self.username:
            self.visible = {ncbi.NCBI_ACCOUNT_MARKER}
            return
        entry = f"link:{self.cfg.label}"
        self.visible = {entry}
        self.clicks[entry] = (self.popup or self).show_form

    def show_form(self):
        self.url = f"https://{self.cfg.hosts[0]}/signin"
        self.visible = {self.cfg.username}
        if self.chooser:
            self.visible = {"Use another account"}
            self.clicks["Use another account"] = lambda: self.visible.add(self.cfg.username)
        if self.cfg.next:
            self.visible.add(self.cfg.next)
            self.clicks[self.cfg.next] = self.show_password
        else:
            self.show_password()
        if self.key == "orcid":
            self.visible.add(f"button:{ncbi.ORCID_COOKIE_NAME}")

    def show_password(self):
        self.visible.update({self.cfg.password, self.cfg.submit})
        self.clicks[self.cfg.submit] = self.finish

    def finish(self):
        if self.completes:
            self.url = "https://www.ncbi.nlm.nih.gov/myncbi/"
            self.username = "Researcher"
            self.visible = {ncbi.NCBI_ACCOUNT_MARKER}

    @contextmanager
    def expect_popup(self, **kwargs):
        yield Mock(value=self.popup)
        if self.popup is None:
            raise PWTimeout("no popup")

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def wait_for_timeout(self, ms):
        pass


@pytest.mark.parametrize("key", ncbi.PROVIDERS)
def test_every_provider_fills_its_own_form_and_verifies_ncbi(key):
    page = Page(key)
    ncbi.login_ncbi(page, "researcher", "test-secret", provider=key, timeout_ms=1)
    cfg = page.cfg
    assert ("fill", cfg.username, "researcher") in page.actions
    assert ("fill", cfg.password, "test-secret") in page.actions
    assert page.actions[-1] == ("click", cfg.submit)
    assert ncbi.is_logged_in_ncbi(page)
    if cfg.next:
        assert page.actions.index(("click", cfg.next)) < page.actions.index(("fill", cfg.password, "test-secret"))


@pytest.mark.parametrize("value,expected", [(cfg.label, key) for key, cfg in ncbi.PROVIDERS.items()] + [("ERA_COMMONS", "era"), ("login_gov", "login.gov"), ("mslive", "microsoft")])
def test_provider_names_and_aliases(value, expected):
    assert ncbi._provider_key(value) == expected


@pytest.mark.parametrize("key", ["google", "microsoft"])
def test_saved_account_chooser_uses_requested_identity(key):
    page = Page(key, chooser=True)
    ncbi.login_ncbi(page, "requested@example.org", "test-secret", provider=key, timeout_ms=1)
    assert ("click", "Use another account") in page.actions
    assert ("fill", page.cfg.username, "requested@example.org") in page.actions


def test_orcid_rejects_cookie_banner_before_filling():
    page = Page("orcid")
    ncbi.login_ncbi(page, "identity", "test-secret", provider="orcid", timeout_ms=1)
    assert page.actions.index(("click", f"button:{ncbi.ORCID_COOKIE_NAME}")) < page.actions.index(("fill", "#username-input", "identity"))


def test_orcid_also_works_without_a_cookie_banner():
    page = Page("orcid")
    page.show_form()
    page.visible.discard(f"button:{ncbi.ORCID_COOKIE_NAME}")
    ncbi._fill_credentials(page, page.cfg, "identity", "test-secret", 1)
    assert ncbi.is_logged_in_ncbi(page)


def test_callback_is_observed_before_return_url_is_loaded():
    page = Page("google")
    login_url = ncbi.NCBI_LOGIN_URL + "?back_url=service-callback&state=preserve"
    return_url = "https://submit.ncbi.nlm.nih.gov/"
    ncbi.login_ncbi(page, "identity", "test-secret", provider="google", login_url=login_url, return_url=return_url, timeout_ms=1)
    assert page.visits == [login_url, return_url]


def test_provider_page_is_not_ncbi_success_or_reloaded_on_failure():
    page = Page("google", completes=False)
    with pytest.raises(ncbi.NCBILoginError):
        ncbi.login_ncbi(page, "identity", "test-secret", provider="google", return_url="https://submit.ncbi.nlm.nih.gov/", timeout_ms=1)
    assert not ncbi.is_logged_in_ncbi(page)
    assert page.visits == [ncbi.NCBI_LOGIN_URL]
    assert not page.closed


def test_existing_ncbi_session_does_not_fill_any_credentials():
    page = Page()
    page.username = "Researcher"
    ncbi.login_ncbi(page, "identity", "test-secret", timeout_ms=1)
    assert page.actions == []


def test_native_form_can_already_be_visible_without_a_provider_button():
    page = Page()

    def goto(url, **kwargs):
        page.visits.append(url)
        page.show_form()

    page.goto = goto
    ncbi.login_ncbi(page, "identity", "test-secret", timeout_ms=1)
    assert ncbi.is_logged_in_ncbi(page)
    assert ("click", "link:NCBI Account") not in page.actions


def test_provider_can_return_an_existing_session_without_a_form():
    page = Page("google")
    page.finish()
    ncbi._fill_credentials(page, page.cfg, "identity", "test-secret", 1)
    assert page.actions == []


def test_password_action_can_race_with_successful_provider_return():
    page = Page("microsoft")
    original_form = page.show_form

    def show_form():
        original_form()

        def callback():
            page.finish()
            raise PWTimeout("the password form was replaced by the NCBI return")

        page.clicks[page.cfg.next] = callback

    page.show_form = show_form
    ncbi.login_ncbi(page, "identity", "test-secret", provider="microsoft", timeout_ms=1)
    assert ncbi.is_logged_in_ncbi(page)


@pytest.mark.parametrize("host", ["https://accounts.google.com/", "https://ncbi.nlm.nih.gov.evil.example/", "http://www.ncbi.nlm.nih.gov/", "https://evil.example/"])
def test_account_marker_on_other_host_is_not_success(host):
    page = Page()
    page.username = "Researcher"
    page.url = host
    page.visible = {ncbi.NCBI_ACCOUNT_MARKER}
    assert not ncbi.is_logged_in_ncbi(page)


def test_hidden_links_and_empty_account_marker_do_not_indicate_success():
    page = Page()
    page.url = ncbi.NCBI_LOGIN_URL
    page.visible = {"#account_logout", "#account_settings"}
    assert not ncbi.is_logged_in_ncbi(page)
    page.visible.add(ncbi.NCBI_ACCOUNT_MARKER)
    assert not ncbi.is_logged_in_ncbi(page)


def test_password_is_not_sent_after_enterprise_redirect():
    page = Page("microsoft")
    page.show_form()

    def redirect():
        page.url = "https://sso.organization.example/"
        page.visible.add(page.cfg.password)

    page.clicks[page.cfg.next] = redirect
    with pytest.raises(ncbi.NCBILoginError, match="unrecognized"):
        ncbi._fill_credentials(page, page.cfg, "identity", "test-secret", 1)
    assert not any(action[0] == "fill" and action[1] == page.cfg.password for action in page.actions)


def test_identity_is_not_sent_to_unexpected_host():
    page = Page("google")
    page.show_form()
    page.url = "https://accounts.google.com.evil.example/"
    with pytest.raises(ncbi.NCBILoginError, match="unrecognized"):
        ncbi._fill_credentials(page, page.cfg, "identity", "test-secret", 1)
    assert page.actions == []


def test_manual_sign_in_can_omit_credentials(monkeypatch):
    page = Page("nih")

    def complete(prompt):
        page.finish()
        return True

    prompt = Mock(side_effect=complete)
    monkeypatch.setattr(ncbi, "wait_for_human", prompt)
    ncbi.login_ncbi(page, provider="nih", allow_manual=True, timeout_ms=1)
    assert prompt.call_count == 1
    assert not any(a[0] == "fill" for a in page.actions)
    assert ncbi.is_logged_in_ncbi(page)


def test_mfa_handoff_keeps_provider_page_and_rechecks_ncbi(monkeypatch):
    page = Page("login.gov", completes=False)

    def mfa(prompt):
        assert page.url.startswith("https://secure.login.gov/")
        assert "MFA" in prompt
        page.completes = True
        page.finish()
        return True

    monkeypatch.setattr(ncbi, "wait_for_human", mfa)
    ncbi.login_ncbi(page, "identity", "test-secret", provider="login.gov", allow_manual=True, timeout_ms=1)
    assert page.visits == [ncbi.NCBI_LOGIN_URL]


@pytest.mark.parametrize("terminal_available", [True, False])
def test_manual_handoff_cannot_claim_success_without_ncbi(monkeypatch, terminal_available):
    page = Page("nih", completes=False)
    monkeypatch.setattr(ncbi, "wait_for_human", lambda prompt: terminal_available)
    with pytest.raises(ncbi.NCBILoginError):
        ncbi.login_ncbi(page, provider="nih", allow_manual=True, timeout_ms=1)


def test_failed_popup_is_kept_open_for_recovery():
    popup = Page("orcid", completes=False)
    page = Page("orcid", popup=popup)
    with pytest.raises(ncbi.NCBILoginError):
        ncbi.login_ncbi(page, "identity", "test-secret", provider="orcid", timeout_ms=1)
    assert not popup.closed
    assert popup.url == "https://orcid.org/signin"
    assert page.visits == [ncbi.NCBI_LOGIN_URL]


def test_popup_is_closed_only_after_original_window_is_authenticated():
    popup = Page("orcid")
    page = Page("orcid", popup=popup)
    old_goto = page.goto

    def goto(url, **kwargs):
        if popup.username:
            assert not popup.closed
            page.username = popup.username  # shared browser-context cookies
        old_goto(url, **kwargs)

    page.goto = goto
    ncbi.login_ncbi(page, "identity", "test-secret", provider="orcid", timeout_ms=1)
    assert ncbi.is_logged_in_ncbi(page)
    assert popup.closed


def test_popup_success_does_not_hide_a_signed_out_original_window():
    popup = Page("orcid")
    page = Page("orcid", popup=popup)
    # No shared session appears in this opener despite the popup's callback.
    with pytest.raises(ncbi.NCBILoginError, match="original NCBI window"):
        ncbi.login_ncbi(page, "identity", "test-secret", provider="orcid", timeout_ms=1)
    assert not popup.closed


def test_self_closing_popup_preserves_the_openers_completed_callback():
    popup = Page("orcid")
    page = Page("orcid", popup=popup)
    original_form = popup.show_form

    def show_form():
        original_form()

        def callback():
            page.finish()
            popup.close()

        popup.clicks[popup.cfg.submit] = callback

    popup.show_form = show_form
    ncbi.login_ncbi(page, "identity", "test-secret", provider="orcid", timeout_ms=1)
    assert ncbi.is_logged_in_ncbi(page)
    assert page.visits == [ncbi.NCBI_LOGIN_URL]


@pytest.mark.parametrize("kwargs", [{"provider": "unknown"}, {"login_url": "http://account.ncbi.nlm.nih.gov/"}, {"return_url": "https://ncbi.nlm.nih.gov.evil.example/"}, {"login_url": "https://identity:secret@account.ncbi.nlm.nih.gov/"}, {"timeout_ms": -1}, {"username": ""}, {"password": ""}])
def test_invalid_arguments_fail_before_browser_actions(kwargs):
    page = Page()
    args = {"username": "identity", "password": "test-secret", **kwargs}
    with pytest.raises(ValueError):
        ncbi.login_ncbi(page, **args)
    assert page.visits == []


def test_browser_errors_do_not_expose_redirect_tokens_or_credentials(monkeypatch):
    page = Page()
    page.goto = Mock(side_effect=PWTimeout("https://auth.example/?state=secret-token password=test-secret"))
    with pytest.raises(ncbi.NCBILoginError) as error:
        ncbi.login_ncbi(page, "identity", "test-secret", timeout_ms=1)
    assert "secret-token" not in str(error.value)
    assert "test-secret" not in str(error.value)


def test_shared_helper_does_not_register_a_fictitious_submission_venue():
    assert "ncbi" not in SLUG_TO_MODULE


@pytest.mark.portal
@pytest.mark.parametrize("key", ncbi.PROVIDERS)
def test_ncbi_public_provider_entry(key):
    """Observe only the public forms; never type or submit credentials."""
    from playwright.sync_api import sync_playwright

    cfg = ncbi.PROVIDERS[key]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(ncbi.NCBI_LOGIN_URL, wait_until="domcontentloaded")
            page.get_by_role("link", name=cfg.label, exact=True).click()
            page.locator(cfg.username).wait_for(state="visible", timeout=30000)
            ncbi._check_host(page, cfg)
            if cfg.next:
                assert page.locator(cfg.next).is_visible()
            else:
                assert page.locator(cfg.password).is_visible()
                assert page.locator(cfg.submit).is_visible()
            assert not ncbi.is_logged_in_ncbi(page)
        finally:
            browser.close()
