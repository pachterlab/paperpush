"""Tests for the ``paperpush login`` command, including ORCID sign-in.

Covers four areas:

* username/password login -- storing credentials, ``--status``, and the
  verify-before-storing sign-in check (``paperpush login <venue>``);
* the login-for-each-venue loop -- ``login`` stores credentials for every
  venue in the database, redirecting portal-sharing venues to their base
  slug;
* ORCID sign-in (``paperpush login --orcid``) -- iD/email validation, the
  iD-and-password collection that mirrors the username/password path,
  verification down the venue's ORCID branch, public record parsing, and
  filling an author block from a fetched profile;
* which venues offer ORCID at all, and which can actually drive it;
* credential-storage branches -- the OS-keyring paths of
  ``save_credential`` / ``get_credential`` / ``delete_credential``, the file
  fallback, and the corrupted-store / incomplete-entry handling;
* the ``verify_login`` driver -- its browser control flow (success, headless
  and headed failure, the ORCID branch, saved-session reuse) exercised
  against a stub Playwright stack, plus the shared ``first_present`` /
  ``first_visible`` / ``fill_login_form`` helpers.

Credential storage is kept off the real keychain/config by the autouse
``_isolate_user_state`` fixture in ``conftest.py``.
"""

import pytest

from playwright.sync_api import TimeoutError as _PW_TIMEOUT

from paperpush import credentials, orcid, subfile, venues
from paperpush.cli import main
from paperpush.database import get_venue, list_venues
from paperpush.validate import parse_authors

VENUES = list_venues()
SLUGS = [j.slug for j in VENUES]

_USERNAME = "researcher@example.edu"
_PASSWORD = "s3cret-token"


# --- username/password login -----------------------------------------------


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


def test_login_asks_for_the_password_once(monkeypatch):
    # The default prompt asks for the password a single time -- a typo is caught
    # by the sign-in check, so a second prompt would only be in the way.
    import getpass as getpass_mod

    from paperpush.venues import login as venues_login

    prompts = []
    monkeypatch.setattr(venues_login, "verify_login", lambda slug, u, p, **k: None)
    monkeypatch.setenv("PAPERPUSH_USERNAME", "researcher@example.edu")
    monkeypatch.delenv("PAPERPUSH_PASSWORD", raising=False)
    monkeypatch.setattr(getpass_mod, "getpass", lambda prompt="": prompts.append(prompt) or "s3cret-token")

    rc = main(["login", "biorxiv"])

    assert rc == 0
    assert prompts == ["Password: "]
    assert credentials.get_credential("biorxiv").password == "s3cret-token"


def test_login_confirm_password_asks_twice(monkeypatch):
    # --confirm-password brings back the second prompt.
    import getpass as getpass_mod

    from paperpush.venues import login as venues_login

    prompts = []
    monkeypatch.setattr(venues_login, "verify_login", lambda slug, u, p, **k: None)
    monkeypatch.setenv("PAPERPUSH_USERNAME", "researcher@example.edu")
    monkeypatch.delenv("PAPERPUSH_PASSWORD", raising=False)
    monkeypatch.setattr(getpass_mod, "getpass", lambda prompt="": prompts.append(prompt) or "s3cret-token")

    rc = main(["login", "biorxiv", "--confirm-password"])

    assert rc == 0
    assert prompts == ["Password: ", "Confirm password: "]
    assert credentials.get_credential("biorxiv").password == "s3cret-token"


def test_login_confirm_password_rejects_a_mismatch(monkeypatch, capsys):
    import getpass as getpass_mod

    typed = iter(["s3cret-token", "s3cret-typo"])
    monkeypatch.setenv("PAPERPUSH_USERNAME", "researcher@example.edu")
    monkeypatch.delenv("PAPERPUSH_PASSWORD", raising=False)
    monkeypatch.setattr(getpass_mod, "getpass", lambda prompt="": next(typed))

    rc = main(["login", "biorxiv", "--confirm-password"])
    err = capsys.readouterr().err

    assert rc == 1
    assert "passwords do not match" in err
    assert credentials.get_credential("biorxiv") is None


def test_login_orcid_asks_for_the_password_once(monkeypatch):
    import getpass as getpass_mod

    from paperpush.venues import login as venues_login

    prompts = []
    monkeypatch.setattr(venues_login, "verify_login", lambda slug, u, p, **k: None)
    monkeypatch.delenv("PAPERPUSH_PASSWORD", raising=False)
    monkeypatch.setattr(getpass_mod, "getpass", lambda prompt="": prompts.append(prompt) or "orcid-token")

    rc = main(["login", "biorxiv", "--orcid-id", "0000-0002-1825-0097"])

    assert rc == 0
    assert prompts == ["ORCID password: "]


def test_login_orcid_confirm_password_asks_twice(monkeypatch):
    import getpass as getpass_mod

    from paperpush.venues import login as venues_login

    prompts = []
    monkeypatch.setattr(venues_login, "verify_login", lambda slug, u, p, **k: None)
    monkeypatch.delenv("PAPERPUSH_PASSWORD", raising=False)
    monkeypatch.setattr(getpass_mod, "getpass", lambda prompt="": prompts.append(prompt) or "orcid-token")

    rc = main(["login", "biorxiv", "--orcid-id", "0000-0002-1825-0097", "--confirm-password"])

    assert rc == 0
    assert prompts == ["ORCID password: ", "Confirm ORCID password: "]


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
    if not venues.login_required(base):
        assert "does not require login" in out
        assert credentials.get_credential(base) is None
        return
    cred = credentials.get_credential(base)
    assert cred is not None, f"no credential stored for {venue.slug} (base {base})"
    assert cred.username == _USERNAME
    assert cred.password == _PASSWORD  # username/password round-trip through CLI storage


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


@pytest.mark.parametrize("value", ["jc@brown.edu", "a.b+tag@sub.example.ac.uk"])
def test_emails_accepted_as_identities(value):
    # ORCID's own field is labelled "Email or ORCID iD", so login --orcid takes
    # either -- but only an iD is an iD.
    assert orcid.is_valid_identity(value)
    assert not orcid.is_valid_id(value)


@pytest.mark.parametrize("value", ["not-an-email", "@brown.edu", "jc@brown", "jc @brown.edu", "0000-0002-1825-0096"])
def test_bad_identities_rejected(value):
    assert not orcid.is_valid_identity(value)


def test_credentials_roundtrip(tmp_path, monkeypatch):
    # An ORCID login stores an iD and password like any other credential; only
    # the method differs, and that is what picks the venue's sign-in path.
    monkeypatch.setenv("PAPERPUSH_KEYRING", "0")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert credentials.get_credential("cell") is None
    assert credentials.save_orcid_credential("cell", VALID_ID, _PASSWORD, name="Josiah Carberry") is False

    cred = credentials.get_credential("cell")
    assert cred.method == "orcid"
    assert cred.orcid == VALID_ID
    assert cred.identity == VALID_ID
    assert cred.display_name == "Josiah Carberry"
    assert cred.password == _PASSWORD
    assert credentials.credential_location("cell") == "file"

    assert credentials.delete_credential("cell") is True
    assert credentials.get_credential("cell") is None


def test_credentials_roundtrip_with_email_identity(tmp_path, monkeypatch):
    # Signing in with the registered email leaves the orcid field empty -- there
    # is no iD to record -- so the identity falls back to the username.
    monkeypatch.setenv("PAPERPUSH_KEYRING", "0")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    credentials.save_orcid_credential("cell", "jc@brown.edu", _PASSWORD)

    cred = credentials.get_credential("cell")
    assert cred.method == "orcid"
    assert cred.orcid == ""
    assert cred.identity == "jc@brown.edu"
    assert cred.password == _PASSWORD


# --- credential-storage branches --------------------------------------------
#
# The roundtrips above always hit the file backend (the autouse fixture sets
# PAPERPUSH_KEYRING=0). These cover the OS-keyring paths and the odd states of
# the file store, driving them with a fake keyring so no real secret store is
# touched.


class _FakeKeyring:
    """A keyring substitute whose per-call failures toggle on request."""

    def __init__(self, *, fail_set=False, fail_get=False, fail_delete=False):
        self.store = {}
        self.fail_set = fail_set
        self.fail_get = fail_get
        self.fail_delete = fail_delete

    def set_password(self, service, username, password):
        if self.fail_set:
            raise RuntimeError("keyring locked")
        self.store[(service, username)] = password

    def get_password(self, service, username):
        if self.fail_get:
            raise RuntimeError("keyring locked")
        return self.store.get((service, username))

    def delete_password(self, service, username):
        if self.fail_delete:
            raise RuntimeError("keyring locked")
        self.store.pop((service, username), None)


def _use_keyring(monkeypatch, keyring):
    """Make ``credentials`` resolve keyring calls to a fake backend."""
    monkeypatch.setattr(credentials, "_get_keyring", lambda: keyring)


def test_credential_saved_to_keyring_keeps_password_out_of_file(monkeypatch):
    # The password goes to the secret store and is never written to the file;
    # the file records only the non-secret fields, so get_credential goes back
    # to the keyring for the password and reports the entry as keyring-backed.
    keyring = _FakeKeyring()
    _use_keyring(monkeypatch, keyring)

    assert credentials.save_credential("cell", _USERNAME, _PASSWORD) is True

    cred = credentials.get_credential("cell")
    assert cred.username == _USERNAME
    assert cred.password == _PASSWORD
    assert keyring.store == {("paperpush:cell", _USERNAME): _PASSWORD}
    assert credentials._read_file_store()["cell"] == {"username": _USERNAME}
    assert credentials.credential_location("cell") == "keyring"
    assert credentials.using_keyring() is True


def test_credential_keyring_write_failure_falls_back_to_file(monkeypatch):
    # A keyring backend that is advertised but rejects the password at write
    # time (a locked Secret Service, say) degrades to the file store, whose
    # password then satisfies later reads.
    keyring = _FakeKeyring(fail_set=True)
    _use_keyring(monkeypatch, keyring)

    assert credentials.save_credential("cell", _USERNAME, _PASSWORD) is False
    assert keyring.store == {}

    cred = credentials.get_credential("cell")
    assert cred.password == _PASSWORD
    assert credentials.credential_location("cell") == "file"


def test_credential_keyring_read_failure_falls_back_to_file_password(monkeypatch):
    # The keyring becoming unreadable later (it was usable when the password
    # was saved) never strands an author: the file-stored password takes over.
    keyring = _FakeKeyring(fail_set=True)
    _use_keyring(monkeypatch, keyring)
    assert credentials.save_credential("cell", _USERNAME, _PASSWORD) is False

    keyring.fail_set = False
    keyring.fail_get = True  # now the keyring is present but unreadable

    cred = credentials.get_credential("cell")
    assert cred.password == _PASSWORD
    assert credentials.credential_location("cell") == "file"


def test_delete_credential_clears_keyring_and_file_entry(monkeypatch):
    keyring = _FakeKeyring()
    _use_keyring(monkeypatch, keyring)
    credentials.save_credential("cell", _USERNAME, _PASSWORD)

    assert credentials.delete_credential("cell") is True
    assert keyring.store == {}
    assert credentials.get_credential("cell") is None
    assert credentials.delete_credential("cell") is False


def test_incomplete_file_entry_treated_as_absent(monkeypatch):
    # A file entry without a password (the keyring holds it, or storage broke)
    # cannot satisfy get_credential on its own.
    credentials._write_file_store({"cell": {"username": _USERNAME}})
    assert credentials.get_credential("cell") is None


def test_corrupt_file_store_treated_as_empty(monkeypatch):
    # A truncated or hand-edited store must not crash login: read as empty,
    # so the next save starts fresh rather than failing.
    path = credentials._file_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert credentials._read_file_store() == {}

    _use_keyring(monkeypatch, _FakeKeyring())
    assert credentials.save_credential("cell", _USERNAME, _PASSWORD) is True
    assert credentials.get_credential("cell") is not None


def test_credential_unknown_location_is_none(monkeypatch):
    _use_keyring(monkeypatch, _FakeKeyring())
    assert credentials.credential_location("never_stored") is None


def test_service_name_lowercases_the_slug():
    assert credentials._service_name("Cell") == "paperpush:cell"


def test_using_keyring_reflects_the_backend(monkeypatch):
    monkeypatch.setattr(credentials, "_get_keyring", lambda: _FakeKeyring())
    assert credentials.using_keyring() is True


def test_using_keyring_false_without_a_backend(monkeypatch):
    monkeypatch.setattr(credentials, "_get_keyring", lambda: None)
    assert credentials.using_keyring() is False


def _fake_keyring_modules(monkeypatch):
    """Install a stand-in ``keyring`` package into sys.modules.

    Returns the fake ``keyring`` module and its ``FailKeyring`` marker class so
    a test can point ``get_keyring`` at whatever backend it wants. monkeypatch
    restores the real modules afterwards.
    """
    import sys
    import types
    from types import SimpleNamespace

    keyring_mod = types.ModuleType("keyring")
    fail_mod = types.ModuleType("keyring.backends.fail")

    class _FailBackend:
        pass

    fail_mod.Keyring = _FailBackend
    keyring_mod.backends = SimpleNamespace(fail=fail_mod)
    monkeypatch.setitem(sys.modules, "keyring", keyring_mod)
    monkeypatch.setitem(sys.modules, "keyring.backends", keyring_mod.backends)
    monkeypatch.setitem(sys.modules, "keyring.backends.fail", fail_mod)
    return keyring_mod, _FailBackend


def test_get_keyring_returns_none_when_keyring_missing(monkeypatch):
    import builtins

    monkeypatch.setenv("PAPERPUSH_KEYRING", "1")
    real_import = builtins.__import__

    def _no_keyring(name, *args, **kwargs):
        if name == "keyring" or name.startswith("keyring."):
            raise ImportError("no keyring installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_keyring)
    assert credentials._get_keyring() is None


def test_get_keyring_returns_none_when_backend_query_fails(monkeypatch):
    monkeypatch.setenv("PAPERPUSH_KEYRING", "1")
    keyring_mod, _ = _fake_keyring_modules(monkeypatch)

    def _boom():
        raise RuntimeError("no backend")

    keyring_mod.get_keyring = _boom
    assert credentials._get_keyring() is None


def test_get_keyring_returns_none_for_the_fail_backend(monkeypatch):
    monkeypatch.setenv("PAPERPUSH_KEYRING", "1")
    keyring_mod, fail = _fake_keyring_modules(monkeypatch)
    keyring_mod.get_keyring = lambda: fail()
    assert credentials._get_keyring() is None


def test_get_keyring_accepts_a_working_backend(monkeypatch):
    monkeypatch.setenv("PAPERPUSH_KEYRING", "1")
    keyring_mod, _ = _fake_keyring_modules(monkeypatch)
    keyring_mod.get_keyring = lambda: object()
    assert credentials._get_keyring() is keyring_mod


def test_login_orcid_stores_id_and_password(monkeypatch, capsys):
    # The command prompts for an ORCID iD and password (env vars stand in for the
    # prompts here) and stores them; --no-verify skips the browser check.
    monkeypatch.setenv("PAPERPUSH_ORCID_ID", VALID_ID)
    monkeypatch.setenv("PAPERPUSH_PASSWORD", _PASSWORD)

    rc = main(["login", "cell_genomics", "--orcid", "--no-verify"])
    out = capsys.readouterr().out

    assert rc == 0, out
    cred = credentials.get_credential("cell_genomics")
    assert cred.method == "orcid"
    assert cred.username == VALID_ID
    assert cred.password == _PASSWORD  # the ORCID password, stored like any other
    assert "Stored ORCID login" in out


def test_login_orcid_verifies_down_the_orcid_branch(monkeypatch, capsys):
    # Verification drives the same venue sign-in as a password login, flagged so
    # the venue takes its "Sign in with ORCID" path.
    from paperpush.venues import login as venues_login

    calls = []
    monkeypatch.setattr(venues_login, "verify_login", lambda slug, u, p, **k: calls.append((slug, u, p, k.get("method"))))
    monkeypatch.setenv("PAPERPUSH_ORCID_ID", VALID_ID)
    monkeypatch.setenv("PAPERPUSH_PASSWORD", _PASSWORD)

    rc = main(["login", "cell", "--orcid"])
    out = capsys.readouterr().out

    assert rc == 0
    assert calls == [("cell", VALID_ID, _PASSWORD, "orcid")]
    assert "Sign-in confirmed" in out
    assert credentials.get_credential("cell") is not None


def test_login_orcid_rejects_a_bad_id(monkeypatch, capsys):
    monkeypatch.setenv("PAPERPUSH_ORCID_ID", "0000-0002-1825-0096")  # bad check digit
    monkeypatch.setenv("PAPERPUSH_PASSWORD", _PASSWORD)

    rc = main(["login", "cell", "--orcid", "--no-verify"])
    err = capsys.readouterr().err

    assert rc == 1
    assert "not a valid ORCID iD" in err
    assert credentials.get_credential("cell") is None


def test_login_orcid_does_not_store_when_unimplemented(monkeypatch, capsys):
    # A journal that offers ORCID but whose flow paperpush cannot drive fails
    # cleanly rather than storing a credential that can never be used.
    from paperpush.venues import login as venues_login

    def _unimplemented(slug, u, p, **k):
        raise NotImplementedError("ORCID sign-in is not implemented for nature yet")

    monkeypatch.setattr(venues_login, "verify_login", _unimplemented)
    monkeypatch.setenv("PAPERPUSH_ORCID_ID", VALID_ID)
    monkeypatch.setenv("PAPERPUSH_PASSWORD", _PASSWORD)

    rc = main(["login", "nature", "--orcid"])
    err = capsys.readouterr().err

    assert rc == 1
    assert "not implemented" in err
    assert credentials.get_credential("nature") is None


# --- which venues offer ORCID sign-in --------------------------------------


@pytest.mark.parametrize("venue", VENUES, ids=SLUGS)
def test_orcid_offered_for_journals_and_preprints(venue):
    """``--orcid`` applies wherever a portal has an ORCID button.

    Journals and preprint servers alike, minus the ones known to have no ORCID
    control (Discrete Mathematics); conference portals sign in with their own
    accounts. A venue whose flow is actually recorded always qualifies.
    """
    impl = venues.try_get_venue_impl(venues.submission_base(venue.slug))
    implemented = impl is not None and impl.supports_orcid_login
    expected = implemented or (venues.login_required(venue.slug) and venue.venue_type in {"journal", "preprint"} and venue.slug != "discrete_mathematics")
    assert venues.orcid_login_offered(venue.slug) is expected


@pytest.mark.parametrize("slug", ["aaai_2027", "iclr_2027", "discrete_mathematics"])
def test_login_orcid_refused_where_not_offered(slug, monkeypatch, capsys):
    monkeypatch.setenv("PAPERPUSH_ORCID_ID", VALID_ID)
    monkeypatch.setenv("PAPERPUSH_PASSWORD", _PASSWORD)

    rc = main(["login", slug, "--orcid", "--no-verify"])
    err = capsys.readouterr().err

    assert rc == 2
    assert "does not offer signing in with ORCID" in err
    assert credentials.get_credential(slug) is None


def test_loginless_venue_needs_no_orcid_credentials(monkeypatch, capsys):
    monkeypatch.setenv("PAPERPUSH_ORCID_ID", VALID_ID)
    monkeypatch.setenv("PAPERPUSH_PASSWORD", _PASSWORD)

    rc = main(["login", "combinatorica", "--orcid", "--no-verify"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "does not require login" in captured.out
    assert captured.err == ""
    assert credentials.get_credential("combinatorica") is None


@pytest.mark.parametrize("slug", ["cell", "cell_systems", "cell_genomics", "plos_compbio", "biorxiv", "medrxiv", "arxiv", "bmc_bioinformatics", "genome_biology"])
def test_recorded_venues_implement_orcid_sign_in(slug):
    # The deployments whose ORCID hand-off has been captured: Editorial Manager's
    # popup (the whole portal -- Cell Press and PLOS alike), the same-tab button
    # (openRxiv, arXiv), and the Springer Nature IDP's link (Snapp).
    impl = venues.try_get_venue_impl(slug)
    if impl is None:
        pytest.skip("venue implementation not importable (Playwright missing)")
    assert impl.supports_orcid_login is True


@pytest.mark.parametrize("slug", ["nature", "science", "bioinformatics", "nucleic_acids_research", "discrete_mathematics", "combinatorica"])
def test_other_venues_do_not_advertise_orcid(slug):
    # The loginless Combinatorica and account-based Discrete Mathematics do not
    # offer ORCID. The other listed portals have no driveable ORCID flow yet.
    impl = venues.try_get_venue_impl(slug)
    if impl is None:
        pytest.skip("venue implementation not importable (Playwright missing)")
    assert impl.supports_orcid_login is False


# --- the shared ORCID hand-off ----------------------------------------------
#
# login_orcid is the one piece of browser driving both portal shapes share, so
# it is exercised against a stub page rather than a real browser: what matters is
# that it detects a popup versus a same-tab navigation, types into whichever one
# holds ORCID's form, and returns to the portal afterwards.


class _FakeLocator:
    def __init__(self, page, key, visible=True):
        self.page, self.key, self._visible = page, key, visible

    @property
    def first(self):
        return self

    def is_visible(self):
        return self._visible

    def wait_for(self, **kwargs):
        if not self._visible:
            raise _PW_TIMEOUT(f"{self.key} never appeared")

    def click(self, **kwargs):
        if not self._visible:
            raise _PW_TIMEOUT(f"{self.key} not clickable")
        self.page.actions.append(("click", self.key))

    def fill(self, value, **kwargs):
        self.page.actions.append(("fill", self.key, value))


class _FakePage:
    """A page whose ORCID controls all exist; ``popup`` picks the hand-off shape."""

    def __init__(self, *, popup=False, missing=()):
        self.popup_page = _FakePage(missing=missing) if popup else None
        self.missing = set(missing)
        self.actions = []
        self.goto_urls = []
        self.closed = False

    def get_by_role(self, role, name=""):
        return _FakeLocator(self, name, visible=name not in self.missing)

    def expect_popup(self, timeout=None):
        page = self

        class _Ctx:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                if page.popup_page is None:
                    raise _PW_TIMEOUT("no popup")
                return False

            @property
            def value(self_inner):
                return page.popup_page

        return _Ctx()

    def goto(self, url):
        self.goto_urls.append(url)

    def wait_for_event(self, event, timeout=None):
        pass

    def close(self):
        self.closed = True


def _run_login_orcid(page, **kwargs):
    from paperpush.venues.login import login_orcid

    login_orcid(
        page,
        VALID_ID,
        _PASSWORD,
        entry=page.get_by_role("button", name="Log in with ORCiD"),
        return_url="https://portal.example/queues",
        venue_name="Example",
        **kwargs,
    )


def test_login_orcid_same_tab_fills_the_page_itself():
    # openRxiv / arXiv: clicking the button navigates this tab to ORCID.
    page = _FakePage(popup=False)
    _run_login_orcid(page)

    assert ("fill", "Email  or  ORCID iD", VALID_ID) in page.actions
    assert ("fill", "Password", _PASSWORD) in page.actions
    assert ("click", "Sign in to ORCID") in page.actions
    assert page.goto_urls == ["https://portal.example/queues"]


def test_login_orcid_popup_fills_the_popup_and_closes_it():
    # Editorial Manager: the link opens ORCID in a separate window.
    page = _FakePage(popup=True)
    _run_login_orcid(page)

    popup = page.popup_page
    assert ("fill", "Email  or  ORCID iD", VALID_ID) in popup.actions
    assert ("click", "Sign in to ORCID") in popup.actions
    assert popup.closed is True
    # Nothing was typed into the portal tab, which only navigates back.
    assert not [a for a in page.actions if a[0] == "fill"]
    assert page.goto_urls == ["https://portal.example/queues"]


def test_login_orcid_raises_the_venues_error_when_the_control_is_missing():
    from paperpush.venues.login import VenueLoginError

    class _Boom(VenueLoginError):
        pass

    page = _FakePage(popup=False, missing={"Log in with ORCiD"})
    with pytest.raises(_Boom, match="Sign in with ORCID"):
        _run_login_orcid(page, error=_Boom)
    assert page.goto_urls == []  # never left the sign-in page


def test_login_orcid_survives_a_missing_cookie_banner():
    # The banner shows only on a fresh profile; its absence is not a failure.
    page = _FakePage(popup=False, missing={"Reject Unnecessary Cookies"})
    _run_login_orcid(page)
    assert ("click", "Sign in to ORCID") in page.actions


def test_every_orcid_venue_accepts_the_flag():
    """A venue that advertises ORCID must actually take ``orcid`` on ``login``.

    The flag is only ever passed to these, so this is the check that the pair
    stays in step -- a venue cannot claim the branch without implementing it.
    """
    import inspect

    for slug in sorted(venues.SLUG_TO_MODULE):
        impl = venues.try_get_venue_impl(slug)
        if impl is None or not impl.supports_orcid_login:
            continue
        params = inspect.signature(impl.login).parameters
        assert "orcid" in params, f"{slug} advertises ORCID but login() takes no orcid argument"


@pytest.mark.parametrize("slug", ["plos_compbio", "nature", "science", "bioinformatics", "cell"])
def test_login_is_the_only_sign_in_entry_point(slug):
    # Signing in is one operation with two branches: a venue exposes `login` and
    # `submit`, never a second public sign-in method.
    impl = venues.try_get_venue_impl(slug)
    if impl is None:
        pytest.skip("venue implementation not importable (Playwright missing)")
    public = {name for name in dir(impl) if not name.startswith("_") and "orcid" in name.lower()}
    assert public == {"supports_orcid_login"}, f"{slug} exposes an extra public ORCID method: {public}"


def test_orcid_unsupported_names_the_venue():
    from paperpush.venues.login import orcid_unsupported

    impl = venues.try_get_venue_impl("nature")
    if impl is None:
        pytest.skip("venue implementation not importable (Playwright missing)")
    exc = orcid_unsupported(impl)
    assert isinstance(exc, NotImplementedError)
    assert "nature" in str(exc)


# --- which sign-in form a stored credential drives --------------------------


def _stub_venue(*, orcid_ok=True, fails=False):
    """A Venue whose one `login` records how it was called instead of driving one."""
    base = pytest.importorskip("paperpush.venues.base")

    class _Stub(base.Venue):
        slug = "cell"  # a real slug, so display_name resolves through the database
        calls: list = []
        supports_orcid_login = orcid_ok

        def submit(self, values, **kwargs):  # pragma: no cover -- never called here
            raise AssertionError("submit should not run")

        def login(self, page, username, password, *, orcid=False, timeout_ms=15000):
            self.calls.append(("orcid" if orcid else "password", username, password))
            if fails:
                raise base.VenueLoginError("wrong password")

    venue = _Stub()
    venue.calls = []
    return venue


def test_stored_orcid_credential_takes_the_orcid_branch():
    venue = _stub_venue()
    cred = credentials.Credential(venue="cell", username=VALID_ID, password=_PASSWORD, method="orcid", orcid=VALID_ID)

    assert venue._sign_in_with_credential(object(), cred) is True
    assert venue.calls == [("orcid", VALID_ID, _PASSWORD)]


def test_stored_password_credential_takes_the_default_branch():
    venue = _stub_venue()
    cred = credentials.Credential(venue="cell", username=_USERNAME, password=_PASSWORD)

    assert venue._sign_in_with_credential(object(), cred) is True
    assert venue.calls == [("password", _USERNAME, _PASSWORD)]


def test_orcid_credential_on_an_unimplemented_venue_falls_back(capsys):
    # A submission must not crash because the ORCID branch is missing: report it,
    # never call login with the flag, and let the caller sign in by hand.
    venue = _stub_venue(orcid_ok=False)
    cred = credentials.Credential(venue="cell", username=VALID_ID, password=_PASSWORD, method="orcid", orcid=VALID_ID)

    assert venue._sign_in_with_credential(object(), cred) is False
    assert venue.calls == []
    assert "Cannot sign in with ORCID automatically" in capsys.readouterr().out


def test_failed_orcid_sign_in_falls_back(capsys):
    venue = _stub_venue(fails=True)
    cred = credentials.Credential(venue="cell", username=VALID_ID, password="wrong", method="orcid", orcid=VALID_ID)

    assert venue._sign_in_with_credential(object(), cred) is False
    assert "Falling back to a manual sign-in" in capsys.readouterr().out


@pytest.mark.parametrize("cred", [None, credentials.Credential(venue="cell", username="", password="")])
def test_no_usable_credential_signs_nothing_in(cred):
    venue = _stub_venue()
    assert venue._sign_in_with_credential(object(), cred) is False
    assert venue.calls == []


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


# --- the verify_login driver ------------------------------------------------
#
# verify_login is the browser-launching gate behind ``paperpush login``. These
# replace the whole Playwright stack with stubs so the driver's control flow --
# success, headless vs headed failure, the ORCID branch, saved-session reuse --
# is exercised without opening a browser.


class _FakeVerifyLoginModule:
    """A venue implementation whose ``login``/``is_logged_in`` record calls."""

    slug = "cell"  # a real slug, so orcid_unsupported can name the venue
    display_name = "Cell"

    def __init__(self, *, orcid_ok=False, login_error=None, logged_in=True):
        self.supports_orcid_login = orcid_ok
        self._login_error = login_error
        self.logged_in = logged_in
        self.login_calls = []
        self.is_logged_in_calls = 0

    def is_logged_in(self, page):
        self.is_logged_in_calls += 1
        return self.logged_in

    def login(self, page, username, password, *, orcid=False, timeout_ms=15000):
        self.login_calls.append((username, password, orcid))
        if self._login_error is not None:
            raise self._login_error


def _stub_verify_login(monkeypatch, module):
    """Point verify_login's browser and venue at stubs; record the actions."""
    from pathlib import Path

    from paperpush.venues import login as controls

    captured = {
        "headless": None,
        "save_calls": [],
        "human_prompts": [],
        "closed": [],
    }

    class _Chromium:
        @staticmethod
        def launch(headless=False):
            captured["headless"] = headless
            return _Browser()

    class _Browser:
        def new_context(self):
            return _Context()

        def close(self):
            captured["closed"].append("browser")

    class _Context:
        def new_page(self):
            return object()

        def close(self):
            captured["closed"].append("context")

    class _Playwright:
        chromium = _Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(controls, "sync_playwright", lambda: _Playwright())
    monkeypatch.setattr(controls, "_login_supported", lambda slug: module)
    monkeypatch.setattr(controls, "submission_base", lambda slug: slug)
    monkeypatch.setattr(controls, "apply_default_timeouts", lambda *a, **k: None)
    monkeypatch.setattr(controls, "save_storage", lambda context, session: captured["save_calls"].append(str(session)))
    monkeypatch.setattr(controls, "session_path", lambda slug: Path(f"/tmp/session-{slug}"))
    monkeypatch.setattr(controls, "wait_for_human", lambda prompt: captured["human_prompts"].append(prompt))
    return captured


def test_verify_login_signs_in_and_saves_the_session(monkeypatch):
    from paperpush.venues.login import verify_login

    module = _FakeVerifyLoginModule()
    captured = _stub_verify_login(monkeypatch, module)

    verify_login("cell", _USERNAME, _PASSWORD)

    assert module.login_calls == [(_USERNAME, _PASSWORD, False)]
    assert captured["headless"] is False
    assert captured["save_calls"] == ["/tmp/session-cell"]
    assert captured["human_prompts"] == []
    assert captured["closed"] == ["context", "browser"]


def test_verify_login_passes_headless_through(monkeypatch):
    from paperpush.venues.login import verify_login

    module = _FakeVerifyLoginModule()
    captured = _stub_verify_login(monkeypatch, module)

    verify_login("cell", _USERNAME, _PASSWORD, headless=True)

    assert captured["headless"] is True
    assert module.login_calls == [(_USERNAME, _PASSWORD, False)]


def test_verify_login_orcid_branch_uses_the_venue_level_flag(monkeypatch):
    from paperpush.venues.login import verify_login

    module = _FakeVerifyLoginModule(orcid_ok=True)
    _stub_verify_login(monkeypatch, module)

    verify_login("cell", VALID_ID, _PASSWORD, method="orcid")

    assert module.login_calls == [(VALID_ID, _PASSWORD, True)]


def test_verify_login_no_automated_sign_in_raises(monkeypatch):
    from paperpush.venues.login import LoginVerificationError, verify_login

    captured = _stub_verify_login(monkeypatch, None)

    with pytest.raises(LoginVerificationError, match="no automated sign-in"):
        verify_login("cell", _USERNAME, _PASSWORD)
    assert captured["headless"] is None  # never launched a browser


def test_verify_login_orcid_not_offered_raises_not_implemented(monkeypatch):
    from paperpush.venues.login import verify_login

    module = _FakeVerifyLoginModule(orcid_ok=False)
    captured = _stub_verify_login(monkeypatch, module)

    with pytest.raises(NotImplementedError, match="not implemented"):
        verify_login("cell", VALID_ID, _PASSWORD, method="orcid")
    assert captured["headless"] is None  # refused before opening a browser


def test_verify_login_headless_failure_raises_login_error(monkeypatch):
    from paperpush.venues.login import LoginVerificationError, verify_login

    module = _FakeVerifyLoginModule(login_error=RuntimeError("bad sign-in"))
    _stub_verify_login(monkeypatch, module)

    with pytest.raises(LoginVerificationError, match="bad sign-in"):
        verify_login("cell", _USERNAME, _PASSWORD, headless=True)


def test_verify_login_headed_failure_lets_the_human_finish(monkeypatch):
    # A headed failure (CAPTCHA, two-factor, a changed field) pauses for the
    # author to finish by hand, then re-checks before declaring the sign-in in.
    from paperpush.venues.login import verify_login

    module = _FakeVerifyLoginModule(login_error=RuntimeError("captcha"))
    captured = _stub_verify_login(monkeypatch, module)

    verify_login("cell", _USERNAME, _PASSWORD)

    assert captured["human_prompts"]
    assert captured["save_calls"] == ["/tmp/session-cell"]
    assert module.is_logged_in_calls == 1  # the re-check after the manual step


def test_verify_login_reports_a_sign_in_that_did_not_take(monkeypatch):
    from paperpush.venues.login import LoginVerificationError, verify_login

    module = _FakeVerifyLoginModule(logged_in=False)
    captured = _stub_verify_login(monkeypatch, module)

    with pytest.raises(LoginVerificationError, match="did not take"):
        verify_login("cell", _USERNAME, _PASSWORD)
    assert captured["human_prompts"] == []  # no exception, so no manual step
    assert captured["save_calls"] == []


def test_verify_login_propagates_not_implemented(monkeypatch):
    from paperpush.venues.login import verify_login

    module = _FakeVerifyLoginModule(login_error=NotImplementedError("no orcid flow"))
    _stub_verify_login(monkeypatch, module)

    with pytest.raises(NotImplementedError, match="no orcid flow"):
        verify_login("cell", _USERNAME, _PASSWORD)


# --- the shared login helpers -----------------------------------------------
#
# first_present / first_visible / fill_login_form are the building blocks the
# portal login methods are written from; like login_orcid above, they are
# exercised against stub pages.


def test_first_present_returns_the_first_visible_locator():
    from paperpush.venues.login import first_present

    visible = _FakeLocator(None, "alpha", visible=True)
    hidden = _FakeLocator(None, "beta", visible=False)
    assert first_present([visible, hidden], 1000) is visible


def test_first_present_returns_none_when_nothing_is_visible():
    from paperpush.venues.login import first_present

    assert first_present([_FakeLocator(None, "alpha", visible=False)], 1000) is None


def test_first_present_waits_for_a_locator_that_appears():
    from paperpush.venues.login import first_present

    class _Appears:
        @property
        def first(self):
            return self

        def is_visible(self):
            return False

        def wait_for(self, **kwargs):
            pass  # appears within the wait budget

    loc = _Appears()
    assert first_present([loc], 1000) is loc


def test_first_present_empty_list_returns_none():
    from paperpush.venues.login import first_present

    assert first_present([], 5000) is None


def test_first_visible_searches_the_names_in_order():
    from paperpush.venues.login import first_visible

    page = _FakePage()
    found = first_visible(page, "button", ["Sign out", "Logout"], 1000)
    assert found is not None
    assert found.key == "Sign out"


def test_first_visible_returns_none_when_no_name_matches():
    from paperpush.venues.login import first_visible

    page = _FakePage(missing={"neither", "nor"})
    assert first_visible(page, "button", ["neither", "nor"], 1000) is None


class _SigninForm:
    """A stub page whose locator() calls are recorded like the real form's."""

    def __init__(self, *, visible=True):
        self.actions = []
        self._visible = visible

    def locator(self, sel):
        return _FakeLocator(self, sel, visible=self._visible)


def test_fill_login_form_fills_and_submits_the_three_field_form():
    from paperpush.venues.login import fill_login_form

    form = _SigninForm()
    fill_login_form(form, _USERNAME, _PASSWORD, userid_sel="#user", password_sel="#password", submit_sel="#signin")

    assert ("fill", "#user", _USERNAME) in form.actions
    assert ("fill", "#password", _PASSWORD) in form.actions
    assert ("click", "#signin") in form.actions


def test_fill_login_form_raises_when_a_field_is_missing():
    from paperpush.venues.login import VenueLoginError, fill_login_form

    form = _SigninForm(visible=False)
    with pytest.raises(VenueLoginError, match="could not find"):
        fill_login_form(form, _USERNAME, _PASSWORD, userid_sel="#user", password_sel="#password", submit_sel="#signin")


def test_login_orcid_raises_when_the_orcid_form_field_is_missing():
    # The control was found and the popup opened, but ORCID's own form is not
    # where it should be -- the sign-in cannot be completed.
    from paperpush.venues.login import VenueLoginError

    page = _FakePage(popup=True, missing={"Email  or  ORCID iD"})
    with pytest.raises(VenueLoginError, match="could not complete the ORCID sign-in"):
        _run_login_orcid(page)
    assert page.popup_page.closed is True  # the popup is still cleaned up
