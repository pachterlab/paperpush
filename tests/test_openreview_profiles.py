"""Unit tests for :mod:`paperpush.openreview_profiles`.

The OpenReview API is replaced by a small fake (profiles keyed by id, a name
search, a login endpoint), so the tests run offline; conftest keeps credential
storage in ``tmp_path``.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse

import pytest

from paperpush import credentials, openreview_profiles
from paperpush.database import get_venue
from paperpush.subfile import SubFile
from paperpush.validate import ERROR, WARNING, validate

TODAY = dt.date(2026, 9, 29)


def _profile(pid: str, name: str, *, domains=("caltech.edu",), end=None, state="Active Institutional", dblp=True, expertise=True) -> dict:
    content = {
        "names": [{"fullname": name, "username": pid, "preferred": True}],
        "emails": [f"****@{d}" for d in domains],
        "history": [{"position": "PhD student", "institution": {"name": "Uni", "domain": domains[0]}, "start": 2020, "end": end}],
    }
    if dblp:
        content["dblp"] = "https://dblp.org/pid/x"
    if expertise:
        content["expertise"] = [{"keywords": ["ml"]}]
    return {"id": pid, "active": True, "state": state, "content": content}


PROFILES = {
    "~Ada_Lovelace1": _profile("~Ada_Lovelace1", "Ada Lovelace"),
    "~Charles_Babbage1": _profile("~Charles_Babbage1", "Charles Babbage", domains=("cam.ac.uk",), end=2020),
    "~Grace_Hopper1": _profile("~Grace_Hopper1", "Grace Hopper", state="Needs Moderation", dblp=False),
    "~Alan_Turing1": _profile("~Alan_Turing1", "Alan Turing", domains=("manchester.ac.uk",)),
    "~Alan_Turing2": _profile("~Alan_Turing2", "Alan Turing", domains=("princeton.edu",)),
}


@pytest.fixture
def fake_api(monkeypatch):
    calls: list[str] = []

    def request(method, path, *, token=None, body=None):
        calls.append(f"{method} {path}")
        if path == "/login":
            return (200, {"token": "tok"}) if body == {"id": "me@example.org", "password": "pw"} else (400, {"name": "Error"})
        assert token == "tok"
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        if path.startswith("/profiles/search"):
            name = query["fullname"][0]
            hits = [p for p in PROFILES.values() if p["content"]["names"][0]["fullname"] == name]
            return 200, {"profiles": hits, "count": len(hits)}
        if path.startswith("/profiles?"):
            ids = query["ids"][0].split(",")
            return 200, {"profiles": [PROFILES[i] for i in ids if i in PROFILES]}
        raise AssertionError(path)

    monkeypatch.setattr(openreview_profiles, "_request", request)
    credentials.save_credential("iclr_2027", "me@example.org", "pw")
    return calls


def _check(authors: str) -> list[tuple[bool, str]]:
    problems = openreview_profiles.check(get_venue("iclr_2027"), {"authors": authors}, today=TODAY)
    return [(p.error, p.message) for p in problems]


def test_applies_only_to_openreview_author_lists():
    assert openreview_profiles.applies(get_venue("iclr_2027"))
    assert openreview_profiles.applies(get_venue("aaai_2027"))
    assert not openreview_profiles.applies(get_venue("biorxiv"))


def test_complete_profiles_pass(fake_api):
    assert _check("~Ada_Lovelace1 | Ada Lovelace | caltech.edu | yes") == []


def test_profiles_found_by_exact_name(fake_api):
    assert _check(" | Ada Lovelace | caltech.edu | yes") == []


def test_unknown_id_is_an_error(fake_api):
    ((error, message),) = _check("~Nobody_Here1 | Nobody | | yes")
    assert error and "no OpenReview profile has the ID ~Nobody_Here1" in message


def test_unknown_name_is_a_warning(fake_api):
    problems = _check(" | Nobody Here | | no\n~Ada_Lovelace1 | Ada Lovelace | | yes")
    assert problems == [(False, "Nobody Here: no OpenReview profile is named exactly 'Nobody Here'; every author needs a profile, and the portal adds authors by this name -- add their Open Review ID")]


def test_ambiguous_name_needs_an_id_unless_suffix_disambiguates(fake_api):
    ((error, message),) = _check(" | Alan Turing | | yes")
    assert not error and "2 OpenReview profiles share this name" in message
    assert _check(" | Alan Turing | princeton.edu | yes") == []


def test_profile_state_history_suffix_and_reviewer_details(fake_api):
    messages = dict((m, e) for e, m in _check("~Grace_Hopper1 | Grace Hopper | caltech.edu | yes\n~Charles_Babbage1 | Charles Babbage | caltech.edu | no"))
    text = "\n".join(messages)
    assert "Grace Hopper: OpenReview profile ~Grace_Hopper1 is not active (Needs Moderation)" in text
    assert "no DBLP link" in text
    assert "Charles Babbage: OpenReview profile ~Charles_Babbage1 lists no current position" in text
    assert "email suffix(es) caltech.edu appear nowhere on OpenReview profile ~Charles_Babbage1" in text
    assert [e for m, e in messages.items() if "not active" in m] == [True]


def test_name_mismatch_and_duplicates(fake_api):
    text = "\n".join(m for _, m in _check("~Ada_Lovelace1 | Augusta King | | yes\n | Ada Lovelace | | no"))
    assert "the name does not match OpenReview profile ~Ada_Lovelace1" in text
    assert "listed twice" in text


def test_no_reciprocal_reviewer_is_flagged_for_iclr(fake_api):
    text = "\n".join(m for _, m in _check("~Ada_Lovelace1 | Ada Lovelace | caltech.edu | no"))
    assert "no author is marked as a reciprocal reviewer" in text


def test_ids_are_fetched_in_one_batch(fake_api):
    _check("~Ada_Lovelace1 | Ada Lovelace | | yes\n~Alan_Turing1 | Alan Turing | | no")
    assert [c for c in fake_api if c.startswith("GET /profiles?")] == ["GET /profiles?ids=~Ada_Lovelace1%2C~Alan_Turing1"]


def test_skipped_without_a_stored_login(monkeypatch):
    monkeypatch.setattr(openreview_profiles, "_request", lambda *a, **k: pytest.fail("no request without a login"))
    ((error, message),) = _check("~Ada_Lovelace1 | Ada Lovelace | | yes")
    assert not error and "paperpush login iclr_2027" in message


def test_rejected_login_is_reported(fake_api):
    openreview_profiles._login.cache_clear()
    credentials.save_credential("iclr_2027", "me@example.org", "wrong")
    ((error, message),) = _check("~Ada_Lovelace1 | Ada Lovelace | | yes")
    assert not error and "rejected the stored login" in message


def test_aaai_conflicts_only_need_to_exist(fake_api):
    credentials.save_credential("aaai_2027", "me@example.org", "pw")
    values = {"authors": "~Ada_Lovelace1 | Ada Lovelace | | yes", "conflicts": "~Charles_Babbage1 | Charles Babbage | | \n~Nobody_Here1 | | |"}
    problems = openreview_profiles.check(get_venue("aaai_2027"), values, today=TODAY)
    assert [(p.error, p.field) for p in problems] == [(False, "conflicts")]
    assert "OpenReview will skip this conflict" in problems[0].message


def test_validate_reports_profiles_with_levels(fake_api):
    sub = SubFile(venue="iclr_2027", values={"authors": "~Nobody_Here1 | Nobody | | yes\n | Alan Turing | | no"})
    issues = [i for i in validate(sub, get_venue("iclr_2027"), check_links=False, check_references=False, check_sensitive=False, check_manuscript=False, check_anonymous=False, check_hidden_text=False) if i.message.startswith("openreview:")]
    assert [(i.level, i.field) for i in issues] == [(ERROR, "authors"), (WARNING, "authors")]
