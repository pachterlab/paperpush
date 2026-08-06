"""Sign-in helpers shared across the venue submission portals.

The authentication seam pulled out of :mod:`paperpush.venues.common`: the two
login error types, the ``paperpush login`` verification driver, and the
building blocks a venue's :meth:`~paperpush.venues.base.Venue.login` and the
base class's :meth:`~paperpush.venues.base.Venue.is_logged_in` are written
from -- :func:`first_visible` (the fast-then-wait "first of several candidate
locators" engine) and :func:`fill_login_form` (the standard three-field form
fill). Session persistence and the generic browser helpers stay in
:mod:`paperpush.venues.common`; this module imports them.
"""

from __future__ import annotations

import logging

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from . import submission_base, try_get_venue_impl
from .common import (DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts,
                     save_storage, session_path, wait_for_human, _try)

logger = logging.getLogger(__name__)


class LoginVerificationError(Exception):
    """Raised when stored credentials could not be confirmed against a venue.

    Carries a human-readable reason: a wrong username/password, a sign-in form
    that has changed, an un-automatable step (CAPTCHA / two-factor), or a missing
    browser dependency. The ``login`` command turns this into a non-zero exit and
    leaves the bad credentials unstored.
    """


class VenueLoginError(Exception):
    """A venue's automated sign-in did not complete.

    The shared base class (:meth:`paperpush.venues.base.Venue.ensure_signed_in`)
    catches this to fall back to a manual sign-in, so every portal's own login
    error (``ScholarOneLoginError``, ``NatureLoginError``, ...) subclasses it
    rather than a bare :class:`Exception`. That keeps the fallback narrow -- a
    genuine bug in a ``login`` method still propagates -- while letting one
    orchestrator drive every portal. The message is the human-readable reason
    (wrong credentials, a changed form, an un-automatable CAPTCHA / two-factor).
    """


def orcid_unsupported(venue) -> NotImplementedError:
    """The error for asking a venue for an ORCID sign-in it cannot drive.

    One wording for the two places that check
    :attr:`~paperpush.venues.base.Venue.supports_orcid_login` before passing
    ``orcid=True``: :meth:`~paperpush.venues.base.Venue._sign_in_with_credential`
    (mid-submission, where it degrades to a manual sign-in) and
    :func:`verify_login` (at ``paperpush login --orcid`` time, where it refuses
    to open a browser it already knows cannot sign in). Lives here, not on
    ``Venue``, so the class keeps ``login`` and ``submit`` as its only operations.
    """
    return NotImplementedError(f"ORCID sign-in is not implemented for {venue.slug} yet; sign in with a " f"{venue.display_name} username and password instead " f"('paperpush login {venue.slug}')")


# --- the ORCID hand-off, shared by every portal that offers it ---------------

# ORCID's own sign-in page, which every portal hands off to, so these are the
# same everywhere. The identity field's accessible name really does carry double
# spaces ("Email  or  ORCID iD") -- that is how ORCID renders the label.
ORCID_IDENTITY_LABEL = "Email  or  ORCID iD"
ORCID_PASSWORD_LABEL = "Password"  # field label on ORCID's form, not a credential  # nosec B105
ORCID_SUBMIT_NAME = "Sign in to ORCID"
ORCID_COOKIE_NAME = "Reject Unnecessary Cookies"

# How long to wait for a popup window before concluding the portal signed in
# through the same tab instead. Short: it costs this much on every same-tab
# portal, and the popup (when there is one) opens as soon as the control is
# clicked. Not wasted time either way -- ORCID is loading during it.
ORCID_POPUP_WAIT_MS = 3000


def login_orcid(
    page,
    orcid_id: str,
    password: str,
    *,
    entry,
    return_url: str,
    venue_name: str,
    timeout_ms: int = 15000,
    error: type[Exception] = VenueLoginError,
) -> None:
    """Sign in through a portal's "Sign in with ORCID" control.

    The ORCID half of a venue's :meth:`~paperpush.venues.base.Venue.login` when
    it is called with ``orcid=True``, written once here because only the way in
    differs between portals -- ORCID's own form is identical everywhere.

    ``entry`` is the portal-side control that hands off to ORCID: one locator, or
    several candidates tried in order (see :func:`first_present`) when the wording
    varies. It is the only thing a venue must supply, because the two shapes it
    leads to are detected rather than declared:

    * a **popup window** (Editorial Manager's "Login using ORCID" link), or
    * a **same-tab navigation** (openRxiv's and arXiv's "Log in with ORCiD"
      button).

    Either way the credentials go into ORCID's form, and ``return_url`` is loaded
    afterwards so the portal picks up the new session -- closing the popup first
    if there was one. The caller checks ``is_logged_in`` from there; this raises
    ``error`` (its own :class:`VenueLoginError` subclass) only when a control it
    needs never appears.
    """
    candidates = list(entry) if isinstance(entry, (list, tuple)) else [entry]
    control = first_present(candidates, timeout_ms)
    if control is None:
        raise error(f"could not find the 'Sign in with ORCID' control on the {venue_name} " "sign-in page (the portal may have changed it); re-capture the " f"selectors with 'playwright codegen {return_url}'")

    try:
        with page.expect_popup(timeout=ORCID_POPUP_WAIT_MS) as popup_info:
            control.click()
        form, in_popup = popup_info.value, True
        logger.debug("%s opened ORCID in a popup window", venue_name)
    except PWTimeout:
        # No popup: the click navigated this tab to ORCID.
        form, in_popup = page, False
        logger.debug("%s signed in to ORCID in the same tab", venue_name)

    try:
        # ORCID's own cookie banner, shown only on a fresh browser profile.
        _try(lambda: form.get_by_role("button", name=ORCID_COOKIE_NAME).click(timeout=5000), "ORCID cookie banner")
        identity = form.get_by_role("textbox", name=ORCID_IDENTITY_LABEL)
        identity.wait_for(state="visible", timeout=timeout_ms)
        identity.fill(orcid_id)
        form.get_by_role("textbox", name=ORCID_PASSWORD_LABEL).fill(password)
        form.get_by_role("button", name=ORCID_SUBMIT_NAME).click()
        if in_popup:
            # The popup closes itself once ORCID hands the session back.
            _try(lambda: form.wait_for_event("close", timeout=timeout_ms), "ORCID popup close")
    except PWTimeout as exc:
        raise error(f"could not complete the ORCID sign-in for {venue_name} (ORCID's form " "may have changed, or the iD/password was rejected)") from exc
    finally:
        if in_popup:
            _try(form.close, "close the ORCID popup")

    # Re-enter the portal so it loads under the new session; the caller's
    # is_logged_in check then confirms the sign-in took.
    page.goto(return_url)


def _login_supported(slug: str):
    """Return the venue object if it can verify a username/password sign-in.

    A venue qualifies when its :class:`~paperpush.venues.base.Venue` exposes
    the ``login(page, username, password)`` / ``is_logged_in(page)`` pair
    that the submission runner already uses (every ``Venue`` does, by contract).
    Returns ``None`` (rather than raising) when the venue has no module or it
    cannot be imported (e.g. Playwright is absent), so the caller can skip
    verification gracefully.
    """
    impl = try_get_venue_impl(submission_base(slug))
    if impl is None:
        logger.debug("No verifiable login venue for %s", slug)
    return impl


def verify_login(
    slug: str,
    username: str,
    password: str,
    *,
    method: str = "password",
    headless: bool = False,
    save_session: bool = True,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Confirm a credential by driving the venue's real sign-in form.

    Launches a browser, fills the sign-in form via the venue's ``login`` -- with
    ``orcid=True`` for ``method="orcid"``, taking the same sign-in page's "Sign in
    with ORCID" branch -- and checks ``is_logged_in``. When the automated fill
    fails in a headed browser -- which also covers a CAPTCHA or two-factor prompt
    that cannot be scripted -- the human is given a chance to finish signing in by
    hand in the open window before the check is repeated.

    On success the signed-in session is saved (so a later ``submit`` skips the
    sign-in) and the function returns. On failure it raises
    :class:`LoginVerificationError` with the reason. Raises the same error if
    Playwright is not installed, so the caller can decide whether to store the
    credentials unverified. :class:`NotImplementedError` propagates unchanged
    from a venue with no recorded ORCID flow: that is not a bad credential, so
    the caller should report it rather than offer to store the pair anyway.
    """
    module = _login_supported(slug)
    if module is None:
        raise LoginVerificationError(f"{slug} has no automated sign-in to verify against")

    base_slug = submission_base(slug)
    via_orcid = method == "orcid"
    if via_orcid and not module.supports_orcid_login:
        # Check before launching: a portal whose ORCID flow has not been recorded
        # should say so in a line of output, not after a browser opens.
        raise orcid_unsupported(module)

    logger.info("Verifying %s %s credentials by signing in (headless=%s)", base_slug, method, headless)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context()
        apply_default_timeouts(context, timeout)
        page = context.new_page()
        try:
            try:
                # orcid= is passed only when set: a venue with no ORCID branch
                # never declares the parameter (see Venue.login).
                module.login(page, username, password, **({"orcid": True} if via_orcid else {}))
                ok = module.is_logged_in(page)
            except NotImplementedError:
                raise
            except Exception as exc:  # noqa: BLE001 -- module raises its own login error type
                logger.warning(
                    "Automated %s sign-in failed during verification: %s",
                    base_slug,
                    exc,
                )
                if headless:
                    raise LoginVerificationError(str(exc)) from exc
                # Headed: a wrong field, CAPTCHA, or two-factor step may just need
                # the human to finish in the window we already opened.
                print(f"Automated sign-in did not complete: {exc}")
                wait_for_human("Finish signing in to the browser window, then return here")
                ok = module.is_logged_in(page)
            if not ok:
                raise LoginVerificationError("the sign-in did not take -- the username or password may be wrong, " "or the site added a step (CAPTCHA / two-factor) that could not be " "completed")
            if save_session:
                save_storage(context, session_path(base_slug))
        finally:
            context.close()
            browser.close()


def first_present(locators, timeout_ms: int):
    """Return the first visible locator from ``locators``, or ``None``.

    Two passes. The fast pass checks each candidate with ``is_visible()``, which
    returns the current state immediately without spending any of the timeout
    budget; once the page has loaded this is the common case and returns at once,
    so candidates that will never match cost nothing. The slow pass is reached
    only when nothing is visible yet -- a post-submit navigation still in flight
    -- and waits on each candidate in turn, splitting ``timeout_ms`` across them
    so the total wait stays bounded.

    Shared by the base class's login-state check and the eJP/Nature sign-in form
    field lookup, which both need "first of several candidate locators to appear".
    """
    for locator in locators:
        try:
            if locator.first.is_visible():
                return locator.first
        except Exception:  # noqa: BLE001 -- a bad candidate is just "not present"
            continue

    per_try = max(1000, timeout_ms // max(1, len(locators)))
    for locator in locators:
        try:
            locator.first.wait_for(state="visible", timeout=per_try)
            return locator.first
        except PWTimeout:
            continue
    return None


def first_visible(root, role: str, names, timeout_ms: int):
    """Return the first visible locator matching ``role`` / one of ``names``.

    Tries each accessible name in turn so a check works across the small wording
    differences between portal deployments. ``root`` is anything exposing
    ``get_by_role`` -- a ``page`` or a ``frame_locator`` (Editorial Manager scopes
    its check to a content iframe). See :func:`first_present` for the fast/slow
    two-pass behavior.
    """
    return first_present([root.get_by_role(role, name=name) for name in names], timeout_ms)


def fill_login_form(
    page,
    username: str,
    password: str,
    *,
    userid_sel: str,
    password_sel: str,
    submit_sel: str,
    timeout_ms: int = 15000,
) -> None:
    """Fill and submit a standard three-field sign-in form.

    Waits for the username/password inputs, fills them, and clicks the submit
    control -- the whole body of a portal ``login`` whose form is a plain
    ``#user`` / ``#password`` / submit-button triple, so a new venue's ``login``
    method is just this call wrapped in its own error type plus an
    :meth:`~paperpush.venues.base.Venue.is_logged_in` check. Does *not* verify
    the sign-in took -- the caller does that -- so this stays a pure form-fill.

    Raises :class:`VenueLoginError` if a field never appears (the form changed);
    the caller may let that propagate or re-raise its own portal error type.
    """
    userid = page.locator(userid_sel)
    pwd = page.locator(password_sel)
    try:
        userid.wait_for(state="visible", timeout=timeout_ms)
        pwd.wait_for(state="visible", timeout=timeout_ms)
    except PWTimeout as exc:
        raise VenueLoginError("could not find the username/password fields on the sign-in page " "(the form may have changed)") from exc

    userid.fill(username)
    pwd.fill(password)
    page.locator(submit_sel).click()
