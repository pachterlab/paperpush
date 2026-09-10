"""The Cell Press sign-in panel and its Elsevier-account branch.

Driven against a stub page rather than a browser: what matters is which
control the helpers look for, in which order, and what they do when the
panel shows the Elsevier button instead of the username/password form (Cell
Press since 2026) or the form directly (PLOS).
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright")

from paperpush.venues.editorialmanager import main as em  # noqa: E402


class _Locator:
    """A control named by its accessible name / text; ``first`` is itself."""

    def __init__(self, page, key):
        self.page, self.key = page, key

    @property
    def first(self):
        return self

    def is_visible(self):
        return self.key in self.page.visible

    def wait_for(self, **kwargs):
        if not self.is_visible():
            raise em.PWTimeout(f"{self.key} never appeared")

    def click(self, **kwargs):
        if not self.is_visible():
            raise em.PWTimeout(f"{self.key} not clickable")
        self.page.actions.append(("click", self.key))
        self.page.visible |= self.page.reveals.get(self.key, set())

    def fill(self, value, **kwargs):
        self.page.actions.append(("fill", self.key, value))

    def press_sequentially(self, value, **kwargs):
        self.page.actions.append(("type", self.key, value))


class _Page:
    """One namespace of controls for the page and every frame under it.

    ``visible`` names the controls showing now; ``reveals`` maps a control to
    the ones that appear once it is clicked (the splash "Log In", the
    "Alternatively..." link, Elsevier's "Continue").
    """

    def __init__(self, visible=(), reveals=None):
        self.visible = set(visible)
        self.reveals = {k: set(v) for k, v in (reveals or {}).items()}
        self.actions = []
        self.goto_urls = []
        self.url = "about:blank"

    # frames share the namespace: the stub does not model iframe scoping
    def frame_locator(self, selector):
        return self

    def get_by_role(self, role, name=""):
        return _Locator(self, name)

    def get_by_text(self, text):
        return _Locator(self, text)

    def goto(self, url):
        self.goto_urls.append(url)
        self.url = url

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, ms):
        pass


CELL = em.VARIANTS["cell"]
USERNAME_FIELD = "username"
ALT = em.EM_FORM_REVEAL_TEXT
ELSEVIER = em.ELSEVIER_LOGIN_BUTTON_NAME


def _cell_panel_page(**kwargs):
    """Cell Press: splash "Log In" -> Elsevier button + "Alternatively..." link
    -> (after that link) the username/password form."""
    return _Page(visible={"Log In"}, reveals={"Log In": {ELSEVIER, ALT}, ALT: {USERNAME_FIELD, "password", "Author Login"}}, **kwargs)


# --- the panel ---------------------------------------------------------------


def test_reveal_clicks_the_splash_then_finds_the_new_panel():
    page = _cell_panel_page()
    em._reveal_login_panel(page, timeout_ms=100)
    assert page.actions == [("click", "Log In")]
    assert ELSEVIER in page.visible


def test_reveal_leaves_a_panel_that_already_shows_alone():
    # PLOS shows the form directly, with no splash to click.
    page = _Page(visible={USERNAME_FIELD, "password", "Author Login"})
    em._reveal_login_panel(page, timeout_ms=100)
    assert page.actions == []


def test_login_form_opens_the_alternatively_link_when_the_form_is_hidden():
    page = _cell_panel_page()
    page.visible |= page.reveals["Log In"]
    frame = em._login_form(page, timeout_ms=100)
    assert frame is not None
    assert ("click", ALT) in page.actions
    assert USERNAME_FIELD in page.visible


def test_login_form_takes_a_visible_form_without_clicking_anything():
    page = _Page(visible={USERNAME_FIELD, "password", "Author Login"})
    assert em._login_form(page, timeout_ms=100) is not None
    assert page.actions == []


def test_login_form_is_none_when_nothing_shows():
    assert em._login_form(_Page(), timeout_ms=100) is None


def test_login_available_recognises_every_shape_of_the_panel():
    assert em._login_available(_Page(visible={"Log In"}), timeout_ms=50)
    assert em._login_available(_Page(visible={ELSEVIER, ALT}), timeout_ms=50)
    assert em._login_available(_Page(visible={USERNAME_FIELD}), timeout_ms=50)
    # Cell already kept us signed in: nothing to click.
    assert not em._login_available(_Page(visible={"Submit New Manuscript"}), timeout_ms=50)


# --- the Elsevier branch -----------------------------------------------------


def _elsevier_page(next_step: set[str]):
    """The open Cell panel, whose Elsevier button leads to the email page and
    whose "Continue" leads to ``next_step`` (what Elsevier shows for the email)."""
    return _Page(visible={ELSEVIER, ALT}, reveals={ELSEVIER: {em.ELSEVIER_EMAIL_LABEL, em.ELSEVIER_CONTINUE_NAME}, em.ELSEVIER_CONTINUE_NAME: next_step})


def test_elsevier_plain_account_types_the_email_and_fills_the_password():
    page = _elsevier_page({em.ELSEVIER_PASSWORD_LABEL, em.ELSEVIER_SIGN_IN_NAME})
    assert em._login_elsevier(page, "me@example.org", "pw", cfg=CELL, timeout_ms=100) is True
    assert ("click", ELSEVIER) in page.actions
    assert ("type", em.ELSEVIER_EMAIL_LABEL, "me@example.org") in page.actions
    assert ("click", em.ELSEVIER_CONTINUE_NAME) in page.actions
    assert ("fill", em.ELSEVIER_PASSWORD_LABEL, "pw") in page.actions
    assert page.actions[-1] == ("click", em.ELSEVIER_SIGN_IN_NAME)


def test_elsevier_unknown_email_is_reported_as_not_an_elsevier_account():
    # Elsevier answers an address it does not know with its Register form.
    page = _elsevier_page({"Register", em.ELSEVIER_REGISTER_PAGE_BUTTON_NAME})
    assert em._login_elsevier(page, "me@example.org", "pw", cfg=CELL, timeout_ms=100) is False
    assert not [a for a in page.actions if a[0] == "fill"]


def test_elsevier_institutional_account_opens_the_picker_and_hands_off():
    page = _elsevier_page({em.ELSEVIER_INSTITUTION_BUTTON_NAME})
    with pytest.raises(em.EditorialManagerLoginError, match="institution"):
        em._login_elsevier(page, "me@university.edu", "pw", cfg=CELL, timeout_ms=100)
    assert page.actions[-1] == ("click", em.ELSEVIER_INSTITUTION_BUTTON_NAME)
    assert not [a for a in page.actions if a[0] == "fill"]


def test_elsevier_prefers_the_password_when_both_routes_are_offered():
    page = _elsevier_page({em.ELSEVIER_PASSWORD_LABEL, em.ELSEVIER_SIGN_IN_NAME, em.ELSEVIER_INSTITUTION_BUTTON_NAME})
    assert em._login_elsevier(page, "me@university.edu", "pw", cfg=CELL, timeout_ms=100) is True
    assert ("click", em.ELSEVIER_INSTITUTION_BUTTON_NAME) not in page.actions


def test_elsevier_unexpected_page_raises_with_advice():
    page = _elsevier_page({"Something new"})
    with pytest.raises(em.EditorialManagerLoginError, match="neither a password field nor an institutional"):
        em._login_elsevier(page, "me@example.org", "pw", cfg=CELL, timeout_ms=100)


def test_elsevier_returns_false_when_the_panel_has_no_elsevier_button():
    # PLOS: no such button, so the caller goes straight to the form.
    page = _Page(visible={USERNAME_FIELD, "password", "Author Login"})
    assert em._login_elsevier(page, "me@example.org", "pw", cfg=em.VARIANTS["plos_compbio"], timeout_ms=100) is False
    assert page.actions == []


# --- routing a stored pair -----------------------------------------------------


class _Venue(em.EditorialManagerVenue):
    slug = "cell"
    variant = CELL

    def __init__(self, logged_in_after_elsevier: bool):
        self._elsevier_ok = logged_in_after_elsevier
        self.checks = 0

    def is_logged_in(self, page, *, timeout_ms=None):
        self.checks += 1
        return self._elsevier_ok


def test_email_username_goes_to_elsevier_first():
    page = _elsevier_page({em.ELSEVIER_PASSWORD_LABEL, em.ELSEVIER_SIGN_IN_NAME})
    _Venue(True)._login_with_password(page, "me@example.org", "pw", timeout_ms=100)
    assert ("fill", em.ELSEVIER_PASSWORD_LABEL, "pw") in page.actions
    assert ("click", "Author Login") not in page.actions


def test_email_username_falls_back_to_the_form_when_elsevier_rejects_it():
    page = _elsevier_page({"Register", em.ELSEVIER_REGISTER_PAGE_BUTTON_NAME})
    # Reloading the portal shows the splash again, behind which the panel waits.
    page.reveals.update({"Log In": {ELSEVIER, ALT}, ALT: {USERNAME_FIELD, "password", "Author Login"}})
    original_goto = page.goto

    def goto(url):
        original_goto(url)
        page.visible = {"Log In"}

    page.goto = goto
    _Venue(False)._login_with_password(page, "me@example.org", "pw", timeout_ms=100)
    assert page.goto_urls == [CELL.login_url]
    assert ("fill", USERNAME_FIELD, "me@example.org") in page.actions
    assert ("fill", "password", "pw") in page.actions
    assert page.actions[-1] == ("click", "Author Login")


def test_plain_username_never_touches_elsevier():
    page = _Page(visible={ELSEVIER, ALT}, reveals={ALT: {USERNAME_FIELD, "password", "Author Login"}})
    venue = _Venue(False)
    venue._login_with_password(page, "jdoe", "pw", timeout_ms=100)
    assert ("click", ELSEVIER) not in page.actions
    assert ("click", ALT) in page.actions
    assert page.actions[-1] == ("click", "Author Login")
    assert venue.checks == 0


def test_plos_form_is_filled_directly():
    page = _Page(visible={USERNAME_FIELD, "password", "Author Login"})
    _Venue(False)._login_with_password(page, "me@example.org", "pw", timeout_ms=100)
    assert page.actions == [("fill", USERNAME_FIELD, "me@example.org"), ("fill", "password", "pw"), ("click", "Author Login")]


def test_missing_form_raises_the_venue_error():
    with pytest.raises(em.EditorialManagerLoginError, match="username/password fields"):
        _Venue(False)._login_with_password(_Page(), "jdoe", "pw", timeout_ms=100)
