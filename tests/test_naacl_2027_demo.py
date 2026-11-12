"""NAACL 2027 Demo Track: dispatch, form invariants, sign-in, and the form-filling helpers."""

from unittest.mock import MagicMock, Mock

import pytest

from paperpush.database import get_venue
from paperpush.requirements import get_requirements
from paperpush.subfile import load as parse_file
from paperpush.validate import validate
from paperpush.venues import get_runner, get_venue_impl
from paperpush.venues.openreview import naacl_2027_demo as naacl
from paperpush.venues.openreview.main import OpenReviewLoginError, OpenReviewVenue

SLUG = "naacl_2027_demo"
SAMPLE = "tests/sub_files/naacl_2027_demo/naacl_2027_demo.sub.txt"


def _values(**overrides):
    values = dict(parse_file(SAMPLE).values)
    values.update(overrides)
    return values


def test_runner_is_an_openreview_venue():
    impl = get_venue_impl(SLUG)
    assert impl is naacl.VENUE
    assert isinstance(impl, OpenReviewVenue)
    assert impl.supports_recovery
    assert impl.login_url == naacl.LOGIN_URL
    assert impl.session_path() != get_venue_impl("iclr_2027").session_path()


def test_form_fields_in_portal_order():
    venue = get_venue(SLUG)
    assert [f.id for f in venue.fields] == ["title", "authors", "keywords", "abstract", "tldr", "pdf_file", "demo_video_url", "live_demo_url", "ethics_statement", "email_sharing", "data_release"]
    fields = {f.id: f for f in venue.fields}
    assert fields["authors"].fields == ["open_review_id?", "name", "email_suffixes?"]
    assert fields["pdf_file"].required and fields["pdf_file"].accept == [".pdf"]
    assert fields["pdf_file"].max_pages_before_refs == 6
    assert fields["demo_video_url"].require_url and fields["live_demo_url"].require_url
    assert fields["demo_video_url"].required and fields["live_demo_url"].required
    assert not fields["tldr"].required and not fields["ethics_statement"].required
    assert fields["email_sharing"].confirm and fields["data_release"].confirm
    assert venue.anonymous is False


def test_requirements_entry():
    req = get_requirements(SLUG)
    assert req.manuscript.max_pages_before_refs == 6
    assert req.manuscript.formats == [".pdf"]
    assert req.statements.ai_use_locations == ["acknowledgements"]


def test_every_text_field_has_a_row():
    """Each text field the runner fills is a form field, and no two share a row."""
    ids = {f.id for f in get_venue(SLUG).fields}
    assert set(naacl.TEXT_ROWS) <= ids
    assert len(set(naacl.TEXT_ROWS.values())) == len(naacl.TEXT_ROWS)
    assert set(naacl.AUTHORIZATIONS) <= ids


@pytest.mark.parametrize("value", ["not a url", "www.example.com"])
def test_url_fields_must_be_http_links(value):
    sub = parse_file(SAMPLE)
    sub.values["live_demo_url"] = value
    issues = validate(sub, get_venue(SLUG), check_links=False, check_references=False, check_sensitive=False, check_manuscript=False, check_anonymous=False, check_hidden_text=False)
    assert any(i.field == "live_demo_url" and "not a valid http/https URL" in i.message for i in issues)


def test_link_check_probes_url_field_values(monkeypatch):
    """integrity.links probes the demo links typed into the .sub, not only links in the files."""
    import importlib

    from paperpush import sensitive

    validate_mod = importlib.import_module("paperpush.validate")

    probed = []

    def reach(url, **kwargs):
        probed.append(url)
        return "dead" not in url

    monkeypatch.setattr(sensitive, "_url_is_reachable", reach)
    outcome = validate_mod._link_issues(get_venue(SLUG), {"demo_video_url": "https://example.com/dead-video", "live_demo_url": "https://example.com/demo"})
    assert {"https://example.com/dead-video", "https://example.com/demo"} <= set(probed)
    messages = [i.message for i in outcome.issues]
    assert any("dead-video" in m and "Demo video URL" in m for m in messages)
    assert not any("example.com/demo " in m for m in messages)


def test_check_values_refuses_incomplete_subs():
    naacl._check_values(_values())
    with pytest.raises(ValueError, match="at least one"):
        naacl._check_values(_values(authors=""))
    with pytest.raises(ValueError, match="live_demo_url"):
        naacl._check_values(_values(live_demo_url=""))
    with pytest.raises(ValueError, match="email_sharing"):
        naacl._check_values(_values(email_sharing="no"))


def test_parse_authors():
    authors = naacl._parse_authors("~Ann_Lee1 | | \n | Bob Ray | mit.edu, harvard.edu\n")
    assert [a["open_review_id"] for a in authors] == ["~Ann_Lee1", ""]
    assert authors[1]["name"] == "Bob Ray"
    assert authors[1]["suffixes"] == ["mit.edu", "harvard.edu"]


def test_text_input_addresses_the_field_row():
    page = MagicMock()
    naacl._text_input(page, "demo_video_url")
    page.locator.assert_called_once_with("#invitation > div:nth-of-type(2) > div:nth-of-type(9) > div > div > div:nth-of-type(2)")
    page.locator.return_value.locator.assert_called_once_with("input, textarea")


def test_cloudflare_interstitial_reads_as_signed_out(monkeypatch):
    """The 'Sign in' link of the Cloudflare check must not be taken for a signed-in page."""
    from paperpush.venues import base

    seen = {}

    def first_visible(root, role, names, timeout_ms):
        seen["names"] = names
        return object() if "Sign in" in names else None

    monkeypatch.setattr(base, "first_visible", first_visible)
    assert naacl.VENUE.is_logged_in(Mock()) is False
    assert "Sign in" in seen["names"] and "Login" in seen["names"]


def test_login_goes_straight_to_the_sign_in_page(monkeypatch):
    page = MagicMock()
    monkeypatch.setattr(naacl.NAACL2027DemoVenue, "is_logged_in", lambda self, page, timeout_ms=None: True)
    naacl.VENUE.login(page, "me@example.org", "pw")
    page.goto.assert_called_once_with(naacl.LOGIN_URL)
    page.get_by_role.assert_any_call("button", name="Login to OpenReview")


def test_login_raises_when_still_on_sign_in_page():
    from playwright.sync_api import TimeoutError as PWTimeout

    page = MagicMock()
    page.wait_for_url.side_effect = PWTimeout("timeout")
    with pytest.raises(OpenReviewLoginError, match="sign-in page"):
        naacl.VENUE.login(page, "me@example.org", "wrong")


def test_submit_dispatch_refuses_before_opening_a_browser(monkeypatch):
    launched = Mock()
    monkeypatch.setattr(naacl, "sync_playwright", launched)
    with pytest.raises(ValueError):
        get_runner(SLUG)(_values(data_release="no"), headless=True, keep_open_on_failure=False)
    launched.assert_not_called()
