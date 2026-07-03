"""Detect when a venue submission portal changes out from under us.

These are *not* tests of paperpush's own code. They open a real submission
portal and check two things against a committed baseline:

  * **contract** -- every selector the venue's runner depends on still
    resolves on the live page (a break here is what would otherwise fail a real
    submission partway through); and
  * **fingerprint** -- the page's stable structure (form-control names, button
    labels, field labels, headings) still matches ``tests/portal_snapshots`` ,
    so a portal *adding* a required field we do not yet handle also trips the
    test, not just changes to selectors we already use.

A raw HTML diff is useless here: ScholarOne and bioRxiv embed per-session
tokens that change every load. The ``drift`` helper (``tests/drift.py``) builds the
fingerprint from human-meaningful structure and drops the volatile parts.

Running them
------------
They hit the network, so they carry ``@pytest.mark.portal`` and are skipped by a
bare ``pytest``. Opt in with ``pytest --run-portal`` (see ``tests/conftest.py``).

Both tests are parametrized over *every* venue in the database, so a venue
added to ``venues.json`` is checked automatically without editing this file.
A venue that does not yet have a drift spec (see ``SPECS`` below) is reported
as a skip naming what to add, rather than being silently left unchecked.

  * ``test_login_page_drift`` needs only network access -- it checks the public
    sign-in page and never signs in.
  * ``test_first_step_drift`` needs to be signed in. It reuses a saved Playwright
    session (created locally by running ``paperpush submit`` once for
    bioRxiv, or restored from a CI secret) and falls back to signing in with
    ``PAPERPUSH_TEST_USERNAME`` / ``PAPERPUSH_TEST_PASSWORD``. With
    neither it skips. Note: reaching the first wizard step creates a clearly
    titled dummy *draft* on the portal -- the unavoidable cost of exercising the
    first real form.

Baselines
---------
The expected fingerprints live in ``tests/portal_snapshots/<slug>.json``. The
first time (or after an intended portal change) regenerate them with
``pytest --run-portal --update-snapshots``, review the diff, and commit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import drift

from paperpush.database import list_venues
from paperpush.venues import common
from paperpush.venues.openrxiv import main as biorxiv
from paperpush.venues.openrxiv.biorxiv import VENUE as biorxiv_venue
from paperpush.venues.scholarone import bioinformatics

pytestmark = pytest.mark.portal

# Where committed baseline fingerprints live, one JSON file per venue keyed by
# page name ("login", "first_step").
SNAPSHOT_DIR = Path(__file__).parent / "portal_snapshots"

# Where screenshot + HTML captures land when a check fails. Git-ignored (see
# .gitignore): the captures embed per-session tokens, and they are diagnostic
# scratch, not baselines to commit.
FAILURE_DIR = SNAPSHOT_DIR / "_failures"

# How long to wait for a contract element before calling it missing.
CONTRACT_TIMEOUT_MS = 10000


# --- per-venue specs ------------------------------------------------------
#
# Each spec wires the generic test to one portal. The selector strings are
# imported from the venue's own runner module wherever they are already
# defined as constants, so the check and the real submitter can never disagree
# about what to look for. A few bioinformatics navigation labels are still inline
# literals in bioinformatics.py (the module is noted there as close to the raw
# recording); those are repeated here with a comment until they are lifted into
# constants.


def _biorxiv_login_contract(page):
    return [
        ("email field", page.get_by_role("textbox", name=biorxiv.LOGIN_EMAIL_LABEL)),
        (
            "password field",
            page.get_by_role("textbox", name=biorxiv.LOGIN_PASSWORD_LABEL),
        ),
        ("sign-in button", page.get_by_role("button", name=biorxiv.LOGIN_BUTTON_NAME)),
    ]


def _biorxiv_reach_first_step(page):
    page.goto(biorxiv.QUEUES_URL)
    page.get_by_role("link", name=biorxiv.BTN_SUBMIT_NEW).click()


def _biorxiv_first_step_contract(page):
    return [
        (
            f"server radio {biorxiv.SEL_SERVER_BIO}",
            page.locator(biorxiv.SEL_SERVER_BIO),
        ),
        (
            f"server radio {biorxiv.SEL_SERVER_MED}",
            page.locator(biorxiv.SEL_SERVER_MED),
        ),
        (
            f'button "{biorxiv.BTN_CONTINUE}"',
            page.get_by_role("button", name=biorxiv.BTN_CONTINUE),
        ),
    ]


def _bioinformatics_login_contract(page):
    return [
        ("username field", page.locator(bioinformatics.LOGIN_USERID)),
        ("password field", page.locator(bioinformatics.LOGIN_PASSWORD)),
        ("log-in button", page.locator(bioinformatics.LOGIN_BUTTON)),
    ]


def _bioinformatics_reach_first_step(page):
    page.goto(bioinformatics.PORTAL_URL)
    # The cookie banner sometimes covers the dashboard; dismiss it if shown.
    reject = page.get_by_role("button", name="Reject All")
    try:
        if reject.is_visible(timeout=3000):
            reject.click()
    except Exception:
        pass
    # Mirror run_bioinformatics' opening navigation (bioinformatics.py). These
    # labels are inline literals there; keep them in sync.
    page.get_by_role("link", name=bioinformatics.AUTHOR_LINK).click()
    page.get_by_role("link", name=" Start New Submission").click()
    page.get_by_role("link", name="Begin Submission").click()
    page.get_by_role("link", name="or continue without pre-").click()


def _bioinformatics_first_step_contract(page):
    return [
        ("title field", page.get_by_role("textbox", name="DOCUMENT_TITLE")),
        ("abstract field", page.get_by_role("textbox", name="DOCUMENT_ABSTRACT")),
        (
            "subject-category select",
            page.get_by_label("Please select your manuscript subject category:"),
        ),
    ]


class _Spec:
    def __init__(
        self,
        slug,
        module,
        login_url,
        login_contract,
        sign_in,
        reach_first_step,
        first_step_contract,
        logged_in,
    ):
        self.slug = slug
        self.module = module
        self.login_url = login_url
        self.login_contract = login_contract
        self.sign_in = sign_in
        self.reach_first_step = reach_first_step
        self.first_step_contract = first_step_contract
        self.logged_in = logged_in


# Per-venue drift specs. Every venue in the database is parametrized
# automatically (see ``_all_venue_slugs``); a venue *with* a spec here is
# fully checked, while one *without* a spec is skipped with a pointer to add
# one. So adding a venue to venues.json immediately surfaces it here as a
# visible coverage gap rather than silently going unchecked, and writing its
# spec below is all that is needed to start checking it.
SPECS = {
    "biorxiv": _Spec(
        slug="biorxiv",
        module=biorxiv,
        login_url=biorxiv.LOGIN_URL,
        login_contract=_biorxiv_login_contract,
        # biorxiv_venue.login navigates to the login URL itself.
        sign_in=lambda page, u, p: biorxiv_venue.login(page, u, p),
        reach_first_step=_biorxiv_reach_first_step,
        first_step_contract=_biorxiv_first_step_contract,
        logged_in=lambda page: biorxiv_venue.is_logged_in(page),
    ),
    "bioinformatics": _Spec(
        slug="bioinformatics",
        module=bioinformatics,
        login_url=bioinformatics.PORTAL_URL,
        login_contract=_bioinformatics_login_contract,
        # bioinformatics.VENUE.login navigates to the portal (its login form) itself.
        sign_in=lambda page, u, p: bioinformatics.VENUE.login(page, u, p),
        reach_first_step=_bioinformatics_reach_first_step,
        first_step_contract=_bioinformatics_first_step_contract,
        logged_in=lambda page: bioinformatics.VENUE.is_logged_in(page),
    ),
}


# --- venue discovery ------------------------------------------------------


def _all_venue_slugs() -> list[str]:
    """Every venue slug in the database, so the drift tests track the registry.

    Parametrizing over this (rather than over ``SPECS``) means a venue added
    to venues.json is covered the moment it exists: it is collected here and,
    until it has a spec, reported as a skip by :func:`_spec_or_skip`.
    """
    return [venue.slug for venue in list_venues()]


def _spec_or_skip(slug: str) -> "_Spec":
    """The drift spec for ``slug``, or a skip explaining how to add one.

    Keeps coverage gaps visible: a venue without a spec is not silently
    ignored, it is skipped with a pointer to the per-venue specs to copy.
    """
    spec = SPECS.get(slug)
    if spec is None:
        pytest.skip(f"no portal-drift spec for {slug!r} yet. Drift checking needs that " "venue's login/first-step selectors and navigation; add a _Spec " "entry for it to SPECS in tests/test_portal_drift.py (model it on the " "biorxiv/bioinformatics specs).")
    return spec


# --- baseline IO ------------------------------------------------------------


def _baseline_path(slug: str) -> Path:
    return SNAPSHOT_DIR / f"{slug}.json"


def _load_baseline(slug: str) -> dict:
    path = _baseline_path(slug)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_baseline(slug: str, page_name: str, fp: dict) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    data = _load_baseline(slug)
    data[page_name] = fp
    path = _baseline_path(slug)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --- shared assertions ------------------------------------------------------


def _skip_if_blocked(page, slug):
    """Skip (not fail) when an anti-bot wall stands between us and the real page.

    ScholarOne (bioinformatics) sits behind Cloudflare, which serves a "Just a
    moment..." interstitial to fresh headless browsers. That is not a portal UI
    change, so it must not read as drift. A saved session carrying a valid
    ``cf_clearance`` cookie usually passes the challenge; a cold fetch in CI does
    not. bioRxiv is not behind such a wall.
    """
    blocked = False
    try:
        title = (page.title() or "").lower()
        if "just a moment" in title or "attention required" in title:
            blocked = True
        elif page.locator("#challenge-form, #challenge-running, [id^=cf-chl]").count() > 0:
            blocked = True
    except Exception:
        pass
    if blocked:
        pytest.skip(f"{slug}: blocked by an anti-bot challenge (Cloudflare). A fresh " "headless browser cannot reach the page; supply a saved session " "with a valid cf_clearance cookie to check this portal.")


def _sign_in_or_skip_if_blocked(spec, page, slug, username, password):
    """Sign in, but treat an anti-bot wall met during sign-in as a skip.

    Sign-in navigates the portal afresh, so a bot-wall (Cloudflare) can appear
    *here* even when the first page load looked clear -- for instance when a saved
    session's ``cf_clearance`` cookie has expired, so the run falls back to a
    real sign-in and the re-navigation trips the challenge. Without this the
    absent login form surfaces as a venue ``...LoginError`` that reads like the
    sign-in form changed, which is a false drift signal. So when sign-in fails we
    first check whether the page is really an anti-bot wall (``_skip_if_blocked``
    skips if so); only a failure that is *not* a block propagates as a genuine
    error worth capturing and fixing.
    """
    try:
        spec.sign_in(page, username, password)
    except Exception:
        _skip_if_blocked(page, slug)
        raise


def _assert_contract(items):
    """Fail with the list of every missing selector, not just the first."""
    from playwright.sync_api import expect, TimeoutError as PWTimeout

    missing = []
    for desc, locator in items:
        try:
            expect(locator.first).to_be_visible(timeout=CONTRACT_TIMEOUT_MS)
        except (AssertionError, PWTimeout):
            missing.append(desc)
    assert not missing, "selector(s) the runner depends on are gone (portal UI changed):\n" + "\n".join(f"  - {m}" for m in missing)


def _check_fingerprint(slug, page_name, page, update_snapshots):
    """Compare the page's fingerprint to its baseline (or refresh the baseline)."""
    current = drift.fingerprint(page)
    if update_snapshots:
        _save_baseline(slug, page_name, current)
        pytest.skip(f"updated {slug} '{page_name}' baseline")
    baseline = _load_baseline(slug).get(page_name)
    if baseline is None:
        pytest.fail(f"no committed baseline for {slug} '{page_name}'. " "Generate it with: pytest --run-portal --update-snapshots")
    page_diff = drift.diff(baseline, current)
    assert not page_diff, f"{slug} '{page_name}' page structure changed vs. baseline:\n" + drift.format_diff(page_diff) + "\n\nIf this change is expected, refresh with: " "pytest --run-portal --update-snapshots"


def _capture_note(page, slug, page_name):
    """Dump a screenshot + HTML of the failed page and describe where they went.

    Called only on a real check failure (a missing selector or a fingerprint
    drift), so the person fixing the runner has the changed page to look at --
    the pixels and the DOM -- rather than only a text diff. Returns a string to
    append to the assertion message; empty when nothing could be captured (the
    capture itself never raises, so it cannot mask the original failure).
    """
    artifacts = common.capture_failure(page, slug, page_name, FAILURE_DIR)
    if not artifacts:
        return ""
    lines = "\n".join(f"  - {kind}: {path}" for kind, path in artifacts.items())
    return "\n\nSaved a capture of the failed page for offline diagnosis " "(open the screenshot, then grep the HTML for the changed control):\n" + lines


def _run_with_capture(page, slug, page_name, body):
    """Run ``body`` and, on any failure, capture the page before re-raising.

    ``body`` is a zero-argument callable covering everything that touches the
    live page for one check: sign-in, navigation to the step, and the contract
    and fingerprint assertions. A page can fail at any of those points -- a
    changed sign-in form, a renamed wizard link, a missing field -- and each is a
    "this page did not work" that a screenshot and the live HTML help diagnose,
    so the capture wraps the whole flow rather than only the final asserts.

    Every real error (:class:`AssertionError` from a contract/fingerprint check,
    a venue ``...LoginError`` from sign-in, a Playwright timeout from
    navigation) is an :class:`Exception`, so it is captured and re-raised with
    the artifact paths attached. ``pytest.skip`` / ``pytest.fail`` raise
    ``OutcomeException`` (a ``BaseException``, not an ``Exception``), so a blocked
    page or a missing baseline propagates untouched with no spurious capture.
    """
    try:
        body()
    except Exception as exc:
        note = _capture_note(page, slug, page_name)
        if note:
            # add_note (Python 3.11+) shows the paths in the traceback while
            # preserving the exception's type; fall back to stdout on 3.10.
            if hasattr(exc, "add_note"):
                exc.add_note(note)
            else:
                print(note)
        raise


def _saved_session(spec):
    """Path to a usable saved Playwright session for ``spec``, or None."""
    path = spec.module.session_path()
    return path if path.exists() else None


# --- the tests --------------------------------------------------------------


@pytest.mark.parametrize("slug", _all_venue_slugs())
def test_login_page_drift(slug, update_snapshots):
    """The public sign-in page still has our login fields and matches baseline.

    Needs only network access -- never signs in -- so it runs on every opted-in
    CI run regardless of whether portal credentials/sessions are configured.
    Parametrized over every venue in the registry; venues without a drift
    spec yet are skipped (see :func:`_spec_or_skip`).
    """
    from playwright.sync_api import sync_playwright

    spec = _spec_or_skip(slug)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(spec.login_url)
            _skip_if_blocked(page, slug)

            def body():
                _assert_contract(spec.login_contract(page))
                _check_fingerprint(slug, "login", page, update_snapshots)

            _run_with_capture(page, slug, "login", body)
        finally:
            context.close()
            browser.close()


@pytest.mark.parametrize("slug", _all_venue_slugs())
def test_first_step_drift(slug, update_snapshots):
    """Signed in, the first wizard step still has our fields and matches baseline.

    Reuses a saved session when one exists; otherwise signs in with the shared
    test account (``PAPERPUSH_TEST_USERNAME`` / ``_PASSWORD``). Skips when
    neither is available. Reaching the first step leaves a dummy draft on the
    portal. Parametrized over every venue in the registry; venues without a
    drift spec yet are skipped (see :func:`_spec_or_skip`).
    """
    import os

    from playwright.sync_api import sync_playwright

    spec = _spec_or_skip(slug)
    session = _saved_session(spec)
    username = os.environ.get("PAPERPUSH_TEST_USERNAME")
    password = os.environ.get("PAPERPUSH_TEST_PASSWORD")
    if not session and not (username and password):
        pytest.skip(f"no saved {slug} session and no PAPERPUSH_TEST_USERNAME/" "PASSWORD; cannot sign in to check the first wizard step")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(session)) if session else browser.new_context()
        page = context.new_page()
        try:

            def body():
                if session:
                    page.goto(spec.login_url)
                    _skip_if_blocked(page, slug)
                    if not spec.logged_in(page):
                        # A stale cookie is not a UI change; do not fail on it.
                        if not (username and password):
                            pytest.skip(f"saved {slug} session looks expired and no credentials " "to re-authenticate; re-run 'paperpush submit'")
                        _sign_in_or_skip_if_blocked(spec, page, slug, username, password)
                else:
                    _sign_in_or_skip_if_blocked(spec, page, slug, username, password)

                spec.reach_first_step(page)
                _assert_contract(spec.first_step_contract(page))
                _check_fingerprint(slug, "first_step", page, update_snapshots)

            _run_with_capture(page, slug, "first_step", body)
        finally:
            context.close()
            browser.close()
