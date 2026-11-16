"""Tests for the NCBI venues (SRA, GEO) and ``login --provider``.

SRA and GEO sign in through :func:`paperpush.venues.ncbi.login_ncbi`. GEO has
no submission runner, so it registers as a ``LOGIN_ONLY`` venue: login,
``--check`` and saved sessions work while ``submit`` still refuses it. SRA's
runner is covered in ``test_sra.py``. The helper itself is covered in
``test_ncbi_login.py``; credential storage is isolated by the autouse fixture
in ``conftest.py``.
"""

from __future__ import annotations

import pytest

from paperpush import credentials, mcp_server, venues
from paperpush.cli import main
from paperpush.database import get_venue
from paperpush.venues.login import login_options
from paperpush.venues.ncbi import main as ncbi_main
from paperpush.venues.ncbi.main import NCBIVenue

NCBI_SLUGS = ["sra", "geo"]
LOGIN_ONLY_NCBI_SLUGS = ["geo"]


# --- registration ------------------------------------------------------------


@pytest.mark.parametrize("slug", LOGIN_ONLY_NCBI_SLUGS)
def test_ncbi_venues_register_for_login_but_not_submit(slug):
    assert slug in venues.LOGIN_ONLY_SLUG_TO_MODULE
    assert slug not in venues.SLUG_TO_MODULE
    assert isinstance(venues.try_get_venue_impl(slug), NCBIVenue)
    assert venues.login_required(slug)
    assert not get_venue(slug).supports_submit
    with pytest.raises(KeyError):
        venues.get_runner(slug)


@pytest.mark.parametrize("slug", NCBI_SLUGS)
def test_ncbi_venue_offers_every_gateway_provider(slug):
    impl = venues.get_venue_impl(slug)
    assert impl.login_providers == tuple(ncbi_main.PROVIDERS)
    assert impl.login_providers[0] == "ncbi"
    assert venues.orcid_login_offered(slug)
    assert impl.login_url == f"https://account.ncbi.nlm.nih.gov/?back_url={impl.portal_url}"


def test_sra_registers_for_login_and_submit():
    assert "sra" in venues.SLUG_TO_MODULE
    assert "sra" not in venues.LOGIN_ONLY_SLUG_TO_MODULE
    assert isinstance(venues.try_get_venue_impl("sra"), NCBIVenue)
    assert venues.login_required("sra")
    assert get_venue("sra").supports_submit
    assert venues.get_runner("sra") == venues.get_venue_impl("sra").submit


def test_ncbi_venue_submit_is_not_implemented():
    with pytest.raises(NotImplementedError, match="no submission runner"):
        venues.get_venue_impl("geo").submit({})


# --- login_options ------------------------------------------------------------


def test_login_options_dispatches_provider_only_to_venues_that_list_it():
    sra = venues.get_venue_impl("sra")
    assert login_options(sra) == {}
    assert login_options(sra, "password", "google") == {"provider": "google"}
    assert login_options(sra, "orcid") == {"orcid": True}
    with pytest.raises(ValueError, match="choose from"):
        login_options(sra, "password", "facebook")
    with pytest.raises(NotImplementedError):
        login_options(sra, "github")
    biorxiv = venues.get_venue_impl("biorxiv")
    with pytest.raises(ValueError, match="single sign-in form"):
        login_options(biorxiv, "password", "google")


# --- NCBIVenue.login / is_logged_in ------------------------------------------


@pytest.mark.parametrize(("kwargs", "expected"), [({}, "ncbi"), ({"provider": "era"}, "era"), ({"orcid": True}, "orcid")])
def test_login_goes_through_login_ncbi_and_returns_to_the_portal(monkeypatch, kwargs, expected):
    calls = []
    monkeypatch.setattr(ncbi_main, "login_ncbi", lambda page, u, p, **kw: calls.append((page, u, p, kw)))
    geo = venues.get_venue_impl("geo")
    geo.login("PAGE", "user", "pw", **kwargs)
    assert calls == [("PAGE", "user", "pw", {"provider": expected, "return_url": geo.portal_url, "timeout_ms": 15000})]


class _Locator:
    def __init__(self, present=True, visible=False):
        self.present, self.visible = present, visible

    @property
    def first(self):
        return self

    def wait_for(self, state, timeout):
        if not self.present:
            raise ncbi_main.PWError("not attached")

    def is_visible(self):
        return self.visible

    def inner_text(self, timeout=None):
        return ""


class _Page:
    def __init__(self, url, *, header=True, sign_in_visible=False):
        self.url = url
        self._locators = {"#account_info": _Locator(present=False), "#uname_short": _Locator(present=False)}
        self.header, self.sign_in_visible = header, sign_in_visible

    def is_closed(self):
        return False

    def locator(self, selector):
        if selector in self._locators:
            return self._locators[selector]
        if selector in (".ncbi-header__account", ".myncbi"):
            return _Locator(present=self.header)
        return _Locator(visible=self.sign_in_visible)


@pytest.mark.parametrize(
    ("page_kwargs", "expected"),
    [
        ({}, True),  # portal header rendered, no sign-in link
        ({"sign_in_visible": True}, False),  # signed out
        ({"header": False}, False),  # page never rendered its header
    ],
)
def test_is_logged_in_reads_the_portal_header(page_kwargs, expected):
    sra = venues.get_venue_impl("sra")
    assert sra.is_logged_in(_Page(sra.portal_url, **page_kwargs), timeout_ms=10) is expected


def test_is_logged_in_is_false_off_the_portal():
    sra = venues.get_venue_impl("sra")
    assert sra.is_logged_in(_Page(sra.login_url), timeout_ms=10) is False
    assert sra.is_logged_in(_Page("https://evil.example/subs/sra/"), timeout_ms=10) is False


def test_stored_provider_reaches_login(monkeypatch):
    sra = venues.get_venue_impl("sra")
    seen = []
    monkeypatch.setattr(ncbi_main, "login_ncbi", lambda page, u, p, **kw: seen.append(kw["provider"]))
    cred = credentials.Credential(venue="sra", username="u@example.edu", password="pw", provider="google")
    assert sra._sign_in_with_credential(object(), cred) is True
    assert seen == ["google"]


# --- credentials ---------------------------------------------------------------


def test_provider_round_trips_through_the_store():
    credentials.save_credential("sra", "u@example.edu", "pw", provider="login.gov")
    cred = credentials.get_credential("sra")
    assert cred.provider == "login.gov" and cred.method == "password"


# --- CLI: paperpush login --provider -----------------------------------------


def test_cli_stores_the_provider(monkeypatch, capsys):
    monkeypatch.setenv("PAPERPUSH_USERNAME", "u@example.edu")
    monkeypatch.setenv("PAPERPUSH_PASSWORD", "pw")
    assert main(["login", "sra", "--provider", "Google", "--no-verify"]) == 0
    assert credentials.get_credential("sra").provider == "google"
    capsys.readouterr()
    assert main(["login", "sra", "--status"]) == 0
    assert "google account" in capsys.readouterr().out


def test_cli_verifies_with_the_provider(monkeypatch):
    seen = {}

    def fake_verify(slug, username, password, **kw):
        seen.update(slug=slug, **kw)

    monkeypatch.setattr("paperpush.venues.login.verify_login", fake_verify)
    monkeypatch.setenv("PAPERPUSH_USERNAME", "u@example.edu")
    monkeypatch.setenv("PAPERPUSH_PASSWORD", "pw")
    assert main(["login", "geo", "--provider", "era"]) == 0
    assert seen["slug"] == "geo" and seen["provider"] == "era"


def test_cli_rejects_an_unknown_provider(capsys):
    assert main(["login", "sra", "--provider", "facebook", "--no-verify"]) == 2
    assert "choose from: ncbi, era" in capsys.readouterr().err
    assert credentials.get_credential("sra") is None


def test_cli_rejects_provider_on_a_single_form_venue(capsys):
    assert main(["login", "biorxiv", "--provider", "google", "--no-verify"]) == 2
    assert "single sign-in form" in capsys.readouterr().err


def test_cli_rejects_provider_with_orcid(capsys):
    assert main(["login", "sra", "--orcid", "--provider", "google"]) == 2
    assert "cannot be combined" in capsys.readouterr().err


# --- MCP ---------------------------------------------------------------------


def test_mcp_login_passes_the_provider(monkeypatch):
    monkeypatch.delenv("PAPERPUSH_USERNAME", raising=False)
    monkeypatch.delenv("PAPERPUSH_PASSWORD", raising=False)
    result = mcp_server.login("sra", provider="nih")
    assert result["status"] == "action_required"
    assert result["command"] == "paperpush login sra --provider nih"
    with pytest.raises(ValueError, match="does not offer"):
        mcp_server.login("biorxiv", provider="google")
