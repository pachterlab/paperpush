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

from .common import (DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts,
                     save_storage, session_path, wait_for_human)

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


def _login_supported(slug: str):
    """Return the venue object if it can verify a username/password sign-in.

    A venue qualifies when its :class:`~paperpush.venues.base.Venue` exposes
    the ``login(page, username, password)`` / ``is_logged_in(page)`` pair
    that the submission runner already uses (every ``Venue`` does, by contract).
    Returns ``None`` (rather than raising) when the venue has no module or it
    cannot be imported (e.g. Playwright is absent), so the caller can skip
    verification gracefully.
    """
    from . import submission_base, try_get_venue_impl

    impl = try_get_venue_impl(submission_base(slug))
    if impl is None:
        logger.debug("No verifiable login venue for %s", slug)
    return impl


def verify_login(
    slug: str,
    username: str,
    password: str,
    *,
    headless: bool = False,
    save_session: bool = True,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Confirm a username/password by driving the venue's real sign-in form.

    Launches a browser, fills the sign-in form via the venue's
    ``auto_login``, and checks ``is_logged_in``. When the automated fill fails
    in a headed browser -- which also covers a CAPTCHA or two-factor prompt that
    cannot be scripted -- the human is given a chance to finish signing in by hand
    in the open window before the check is repeated.

    On success the signed-in session is saved (so a later ``submit`` skips the
    sign-in) and the function returns. On failure it raises
    :class:`LoginVerificationError` with the reason. Raises the same error if
    Playwright is not installed, so the caller can decide whether to store the
    credentials unverified.
    """
    module = _login_supported(slug)
    if module is None:
        raise LoginVerificationError(f"{slug} has no automated sign-in to verify against")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LoginVerificationError("Playwright is not installed, so the sign-in could not be checked " "(pip install playwright && python -m playwright install chromium)") from exc

    from . import submission_base

    base_slug = submission_base(slug)

    logger.info("Verifying %s credentials by signing in (headless=%s)", base_slug, headless)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context()
        apply_default_timeouts(context, timeout)
        page = context.new_page()
        try:
            try:
                module.login(page, username, password)
                ok = module.is_logged_in(page)
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
    from playwright.sync_api import TimeoutError as PWTimeout

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
    from playwright.sync_api import TimeoutError as PWTimeout

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
