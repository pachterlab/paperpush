"""Unattended runs (CI, ``</dev/null``) must fail fast at sign-in, not hang or eat stdin.

Two regressions this guards against, both seen in the private repo's scheduled
Submit workflow:

* ``submit`` with no stored credentials started the interactive login, whose
  username prompt read the *workflow loop's* stdin (its list of .sub files), so
  only the first venue ever ran.
* With no live session, the manual sign-in prompt hit EOF and the run carried on
  signed out, failing later as an opaque selector timeout instead of saying the
  login was missing.
"""

from __future__ import annotations

import builtins
import io
import sys

import pytest

from paperpush import credentials, venues
from paperpush.cli import main
from paperpush.venues import base, common
from paperpush.venues.common import SignInRequired, wait_for_human


def _raise(exc):
    def _input(*_a, **_k):
        raise exc

    return _input


@pytest.mark.parametrize("exc", [EOFError(), OSError("reading from stdin while output is captured")])
def test_wait_for_human_reports_no_human(monkeypatch, exc):
    monkeypatch.setattr(builtins, "input", _raise(exc))
    assert wait_for_human("Sign in") is False


def test_wait_for_human_returns_on_enter_and_ctrl_c(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda *_a: "")
    assert wait_for_human("Sign in") is True
    monkeypatch.setattr(builtins, "input", _raise(KeyboardInterrupt()))
    assert wait_for_human("Sign in") is True


class _Page:
    def __init__(self):
        self.urls: list[str] = []

    def goto(self, url):
        self.urls.append(url)


def _stub_venue(monkeypatch):
    saved: list = []
    monkeypatch.setattr(base, "save_storage", lambda context, session: saved.append(session))

    class _Stub(base.Venue):
        slug = "cell"  # a real slug, so display_name / portal_url resolve

        def submit(self, values, **kwargs):  # pragma: no cover -- never called here
            raise AssertionError

        def login(self, page, username, password, *, orcid=False, timeout_ms=15000):  # pragma: no cover
            raise AssertionError("no credential is stored, so login must not run")

        def is_logged_in(self, page, *, timeout_ms=None):
            return False

    return _Stub(), saved


def test_manual_sign_in_without_a_terminal_raises_auth_required(monkeypatch):
    venue, saved = _stub_venue(monkeypatch)
    monkeypatch.setattr(builtins, "input", _raise(EOFError()))

    with pytest.raises(SignInRequired, match=r"^auth_required: .*paperpush login cell"):
        venue.ensure_signed_in(_Page(), object())
    # A signed-out session must not overwrite the saved one.
    assert saved == []


def test_manual_sign_in_with_a_human_still_saves_the_session(monkeypatch):
    venue, saved = _stub_venue(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda *_a: "")

    assert venue.ensure_signed_in(_Page(), object()) is True
    assert len(saved) == 1


def _stub_submit(monkeypatch, runner):
    import importlib

    import paperpush.subfile as subfile
    from paperpush.subfile import SubFile

    validate_mod = importlib.import_module("paperpush.validate")
    monkeypatch.setattr(subfile, "load", lambda path: SubFile(venue="biorxiv", values={}))
    monkeypatch.setattr(validate_mod, "validate", lambda sub, venue, **kwargs: [])
    monkeypatch.setattr(venues, "get_runner", lambda venue: runner)


def test_submit_without_a_terminal_does_not_read_stdin_for_a_login(monkeypatch, capsys):
    # What a CI loop's `while read subfile` leaves on stdin: the next .sub paths.
    stdin = io.StringIO("sub_files/nature/nature.sub\nsub_files/science/science.sub\n")
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.delenv("PAPERPUSH_USERNAME", raising=False)
    monkeypatch.delenv("PAPERPUSH_PASSWORD", raising=False)
    ran: list = []
    _stub_submit(monkeypatch, lambda *a, **k: ran.append(a))

    rc = main(["submit", "biorxiv.sub", "--headless"])

    assert rc == 0 and ran
    assert "no terminal to log in from" in capsys.readouterr().out
    assert stdin.read() == "sub_files/nature/nature.sub\nsub_files/science/science.sub\n"
    assert credentials.get_credential("biorxiv") is None


def test_submit_reports_auth_required_and_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    def _runner(*_a, **_k):
        raise common.SignInRequired("auth_required: bioRxiv needs a sign-in")

    _stub_submit(monkeypatch, _runner)

    rc = main(["submit", "biorxiv.sub", "--headless"])

    assert rc == 1
    assert "error: auth_required: bioRxiv needs a sign-in" in capsys.readouterr().err
