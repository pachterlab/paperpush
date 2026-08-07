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
* which venues offer ORCID at all, and which can actually drive it.

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


@pytest.mark.parametrize("slug", ["aaai_2027", "discrete_mathematics"])
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
        self.popup_page = _FakePage() if popup else None
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
