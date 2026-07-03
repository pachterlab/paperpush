"""Tests for the ``paperpush login`` command, including ORCID sign-in.

Covers three areas:

* username/password login -- storing credentials, ``--status``, and the
  verify-before-storing sign-in check (``paperpush login <venue>``);
* the login-for-each-venue loop -- ``login`` stores credentials for every
  venue in the database, redirecting portal-sharing venues to their base
  slug;
* ORCID sign-in (``paperpush login --orcid``) -- iD validation, public
  record parsing, the authorize URL, credential round-trips, and filling an
  author block from a fetched profile.

Credential storage is kept off the real keychain/config by the autouse
``_isolate_user_state`` fixture in ``conftest.py``.
"""

import pytest

from paperpush import credentials, orcid, subfile, venues
from paperpush.cli import main
from paperpush.database import get_venue, list_venues
from paperpush.validate import parse_authors

VENUES = list_venues()
SLUGS = [j.slug for j in VENUES]

_USERNAME = "researcher@example.edu"
_PASSWORD = "s3cret-token"


# --- username/password login -----------------------------------------------


def test_login_biorxiv_runs(monkeypatch, capsys):
    # Supply username/password through the environment so nothing prompts.
    # --no-verify keeps this storage smoke test off the network.
    monkeypatch.setenv("PAPERPUSH_USERNAME", "researcher@example.edu")
    monkeypatch.setenv("PAPERPUSH_PASSWORD", "s3cret-token")

    rc = main(["login", "biorxiv", "--no-verify"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "biorxiv" in out

    cred = credentials.get_credential("biorxiv")
    assert cred is not None
    assert cred.username == "researcher@example.edu"
    assert cred.password == "s3cret-token"


def test_login_status_runs(monkeypatch, capsys):
    # --status should run cleanly whether or not a credential is stored.
    rc = main(["login", "biorxiv", "--status"])
    out = capsys.readouterr().out
    assert rc == 1  # not logged in yet
    assert "biorxiv" in out

    monkeypatch.setenv("PAPERPUSH_USERNAME", "researcher@example.edu")
    monkeypatch.setenv("PAPERPUSH_PASSWORD", "s3cret-token")
    assert main(["login", "biorxiv", "--no-verify"]) == 0
    capsys.readouterr()

    assert main(["login", "biorxiv", "--status"]) == 0
    assert "Logged in" in capsys.readouterr().out


def test_login_list_empty(capsys):
    # --list with nothing stored reports no logins and exits cleanly.
    rc = main(["login", "--list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Not logged in" in out


def test_login_list_shows_venues_and_usernames(monkeypatch, capsys):
    monkeypatch.setenv("PAPERPUSH_PASSWORD", "s3cret-token")
    monkeypatch.setenv("PAPERPUSH_USERNAME", "alice@example.edu")
    assert main(["login", "biorxiv", "--no-verify"]) == 0
    monkeypatch.setenv("PAPERPUSH_USERNAME", "bob@example.edu")
    assert main(["login", "arxiv", "--no-verify"]) == 0
    capsys.readouterr()

    rc = main(["login", "--list"])
    out = capsys.readouterr().out
    assert rc == 0
    # Both venues appear, each paired with the username used to log in.
    assert "biorxiv: alice@example.edu" in out
    assert "arxiv: bob@example.edu" in out


def test_login_list_shows_shared_family_as_separate_venues(monkeypatch, capsys):
    # The AAAS siblings share Science's one login, so --list shows each on its
    # own line as if separately logged in -- the user thinks in journals, even
    # though the credential is stored once under the science base slug.
    monkeypatch.setenv("PAPERPUSH_PASSWORD", "s3cret-token")
    monkeypatch.setenv("PAPERPUSH_USERNAME", "alice@example.edu")
    assert main(["login", "science", "--no-verify"]) == 0
    capsys.readouterr()

    rc = main(["login", "--list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "science: alice@example.edu" in out
    assert "science_advances: alice@example.edu" in out
    assert "science_immunology: alice@example.edu" in out


def test_login_without_venue_or_list_errors(capsys):
    rc = main(["login"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "venue is required" in err


def test_login_verifies_before_storing(monkeypatch, capsys):
    # By default, login confirms the credentials by signing in. A confirmed
    # sign-in stores the credentials.
    from paperpush.venues import login as venues_login

    calls = []
    monkeypatch.setattr(venues_login, "verify_login", lambda slug, u, p, **k: calls.append((slug, u, p)))
    monkeypatch.setenv("PAPERPUSH_USERNAME", "researcher@example.edu")
    monkeypatch.setenv("PAPERPUSH_PASSWORD", "s3cret-token")

    rc = main(["login", "biorxiv"])
    out = capsys.readouterr().out

    assert rc == 0
    assert calls == [("biorxiv", "researcher@example.edu", "s3cret-token")]
    assert "Sign-in confirmed" in out
    assert credentials.get_credential("biorxiv") is not None


def test_login_does_not_store_when_verification_fails(monkeypatch, capsys):
    # A failed sign-in check leaves the bad credentials unstored and exits non-zero.
    from paperpush.venues import login as venues_login

    def _boom(slug, u, p, **k):
        raise venues_login.LoginVerificationError("the username or password may be wrong")

    monkeypatch.setattr(venues_login, "verify_login", _boom)
    monkeypatch.setenv("PAPERPUSH_USERNAME", "researcher@example.edu")
    monkeypatch.setenv("PAPERPUSH_PASSWORD", "wrong-token")

    rc = main(["login", "biorxiv"])
    err = capsys.readouterr().err

    assert rc == 1
    assert "sign-in check failed" in err
    assert credentials.get_credential("biorxiv") is None


# --- login for each venue ------------------------------------------------


@pytest.mark.parametrize("venue", VENUES, ids=SLUGS)
def test_login_command_for_each_venue(venue, monkeypatch, capsys):
    """``paperpush login <venue>`` stores credentials for every venue.

    ``--no-verify`` skips the browser sign-in check, so this exercises the login
    dispatch and storage offline. A venue that submits through another's portal
    (the AAAS family) redirects its credentials to the base slug, so the stored
    credential is looked up under :func:`venues.submission_base`.
    """
    monkeypatch.setenv("PAPERPUSH_USERNAME", _USERNAME)
    monkeypatch.setenv("PAPERPUSH_PASSWORD", _PASSWORD)

    rc = main(["login", venue.slug, "--no-verify"])
    out = capsys.readouterr().out

    assert rc == 0, out
    base = venues.submission_base(venue.slug)
    cred = credentials.get_credential(base)
    assert cred is not None, f"no credential stored for {venue.slug} (base {base})"
    assert cred.username == _USERNAME


# --- ORCID sign-in ---------------------------------------------------------

# A canonical, checksum-valid ORCID iD (ORCID's documented example).
VALID_ID = "0000-0002-1825-0097"

SAMPLE_RECORD = {
    "person": {
        "name": {"given-names": {"value": "Josiah"}, "family-name": {"value": "Carberry"}},
        "emails": {"email": [{"email": "jc@brown.edu", "primary": True}]},
    },
    "activities-summary": {"employments": {"affiliation-group": [{"summaries": [{"employment-summary": {"organization": {"name": "Brown University"}}}]}]}},
}


@pytest.mark.parametrize(
    "value",
    [
        VALID_ID,
        f"https://orcid.org/{VALID_ID}",
        VALID_ID.replace("-", ""),
    ],
)
def test_valid_ids_accepted(value):
    assert orcid.is_valid_id(value)
    assert orcid.normalize_id(value) == VALID_ID


@pytest.mark.parametrize(
    "value",
    [
        "0000-0002-1825-0096",  # wrong check digit
        "1234",  # too short
        "0000-0002-1825-009Z",  # bad check character
    ],
)
def test_invalid_ids_rejected(value):
    assert not orcid.is_valid_id(value)


def test_parse_record_extracts_name_email_affiliation():
    profile = orcid.parse_record(VALID_ID, SAMPLE_RECORD)
    assert profile.orcid_id == VALID_ID
    assert profile.name == "Josiah Carberry"
    assert profile.email == "jc@brown.edu"
    assert profile.affiliation == "Brown University"


def test_parse_record_tolerates_private_sections():
    profile = orcid.parse_record(VALID_ID, {"person": {}})
    assert profile.orcid_id == VALID_ID
    assert profile.name == "" and profile.email == "" and profile.affiliation == ""


def test_fetch_profile_uses_transport(monkeypatch):
    monkeypatch.setattr(orcid, "_http_get_json", lambda url: SAMPLE_RECORD)
    profile = orcid.fetch_profile(VALID_ID)
    assert profile.affiliation == "Brown University"


def test_authorize_url_has_required_params():
    url = orcid.authorize_url("APP-123", "http://127.0.0.1:5000/callback")
    assert "client_id=APP-123" in url
    assert "response_type=code" in url
    assert "scope=%2Fauthenticate" in url
    assert url.startswith(orcid.authorize_endpoint())


def test_credentials_roundtrip_with_token(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERPUSH_KEYRING", "0")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert credentials.get_credential("biorxiv") is None
    assert credentials.save_orcid_credential("biorxiv", VALID_ID, name="Josiah Carberry", token="tok") is False

    cred = credentials.get_credential("biorxiv")
    assert cred.method == "orcid"
    assert cred.orcid == VALID_ID
    assert cred.display_name == "Josiah Carberry"
    assert cred.password == "tok"
    assert credentials.credential_location("biorxiv") == "file"

    assert credentials.delete_credential("biorxiv") is True
    assert credentials.get_credential("biorxiv") is None


def test_credentials_identity_only(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERPUSH_KEYRING", "0")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    credentials.save_orcid_credential("biorxiv", VALID_ID, name="Josiah Carberry")
    cred = credentials.get_credential("biorxiv")
    assert cred is not None
    assert cred.password == ""  # no token issued
    assert cred.orcid == VALID_ID


def test_fill_author_block_matches_by_name():
    profile = orcid.parse_record(VALID_ID, SAMPLE_RECORD)
    block = "Josiah Carberry |  |  |  | yes\nA. Coauthor | a@x.org | X University | | no"
    new_block, matched = orcid.fill_author_block(block, profile)
    assert matched == "Josiah Carberry"
    authors = {a["name"]: a for a in parse_authors(new_block)}
    assert authors["Josiah Carberry"]["orcid"] == VALID_ID
    assert authors["Josiah Carberry"]["email"] == "jc@brown.edu"  # filled
    assert authors["Josiah Carberry"]["affiliation"] == "Brown University"
    assert authors["A. Coauthor"]["email"] == "a@x.org"  # untouched
    assert authors["A. Coauthor"]["orcid"] == ""


def test_fill_author_block_falls_back_to_corresponding():
    profile = orcid.OrcidProfile(orcid_id=VALID_ID, name="Unlisted Person")
    block = "First Author | f@x.org | X | | no\nSecond Author | s@x.org | Y | | yes"
    _, matched = orcid.fill_author_block(block, profile)
    assert matched == "Second Author"


def test_replace_block_preserves_other_content():
    venue = get_venue("biorxiv")
    text = subfile.render_template(venue)
    block = "Solo Author | s@x.org | X | | yes"
    updated = subfile.replace_block(text, "authors", block)

    assert subfile.find_block(updated, "authors") == block
    # Other fields and comments survive intact.
    assert "@venue: biorxiv" in updated
    assert "# Manuscript title" in updated
    assert subfile.parse(updated).values["authors"] == block
