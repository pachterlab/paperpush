"""The :class:`Venue` contract every submission venue implements.

A venue is a small object exposing the operations the rest of the package drives a
submission portal through. Only two are venue-specific and abstract, so a new
journal is as little work as possible:

* :meth:`~Venue.submit` -- fill and drive the submission wizard from a ``.sub``.
* :meth:`~Venue.login` -- fill and submit the sign-in form. One method covers
  both ways in: its ``orcid`` argument selects the portal's "Sign in with ORCID"
  branch over its own credential form. A venue that offers that branch sets
  :attr:`~Venue.supports_orcid_login` and handles the flag inside ``login``
  (splitting the branch into a private helper of its own if it is long); a venue
  that does not is never passed ``orcid=True``.

Everything else is provided by this base class and steered by a handful of class
attributes:

* :meth:`~Venue.is_logged_in` reads the ``logged_in_*`` attributes (which role /
  accessible names mark a signed-in page, and whether their presence means signed
  *in* or *out*) -- a venue declares those instead of writing the check.
* :meth:`~Venue.ensure_signed_in` is the one shared sign-in orchestrator (reuse a
  saved session, else stored credentials via :meth:`~Venue.login`, else a manual
  sign-in); no venue reimplements it. A loginless portal sets
  :attr:`~Venue.requires_login` to ``False`` and the orchestrator simply opens
  its submission page.
* :meth:`~Venue.session_path` / :meth:`~Venue.save_session` locate and capture the
  saved Playwright session; :attr:`~Venue.field_validators` defaults to empty.

``submit`` and ``login`` are abstract, so a subclass that omits one fails to
instantiate rather than only breaking deep in a run; that is what lets the
dispatch in :mod:`paperpush.venues` and the login check in
:mod:`paperpush.venues.login` treat every venue uniformly.

Each venue module exposes exactly one instance named ``VENUE``;
:func:`paperpush.venues.get_venue_impl` resolves a slug to it. Portals shared
by several venues (eJP, Editorial Manager, ScholarOne, SNAPP, openRxiv) put the
Playwright logic in one engine and one portal base class parameterized by a
frozen ``Variant``; each venue is then a subclass that selects its ``variant``
(see e.g. :mod:`paperpush.venues.nature.main`). Venues on a portal of their
own (arXiv, Science) subclass :class:`Venue` directly. A venue that cannot script
a fresh sign-in (the ScholarOne journals) sets
:attr:`~Venue.supports_session_capture` to ``False`` so ``save_session`` raises
rather than pretending to capture one.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from playwright.sync_api import sync_playwright

from .. import credentials
from ..database import get_venue
from .common import (DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts,
                     save_storage)
from .common import session_path as _slug_session_path
from .common import wait_for_human
from .login import VenueLoginError, first_visible, orcid_unsupported

logger = logging.getLogger(__name__)


class Venue(ABC):
    """Uniform interface a submission venue exposes to the rest of the package.

    Subclasses set :attr:`slug`, declare the ``logged_in_*`` login-state
    attributes, and implement :meth:`submit` and :meth:`login`; the base supplies
    :meth:`is_logged_in`, :meth:`ensure_signed_in`, :meth:`session_path`, and
    :meth:`save_session`. A portal whose sign-in form or dashboard lives at a URL
    other than the venue's ``submission_url`` overrides :attr:`portal_url` /
    :attr:`login_url` (the shared-portal classes read them off their ``variant``).
    """

    #: The venue's database slug (its key in ``venues.json``). Set on the subclass.
    slug: str
    #: Optional custom per-field validators layered onto the pydantic schema; see
    #: :func:`paperpush.venues.get_field_validators`.
    field_validators: dict[str, Callable[[str], str]] = {}
    #: Optional custom option listers for fields whose allowed values are not a
    #: flat list in the field metadata (e.g. Nature's variable-depth subject
    #: tree); surfaced by ``paperpush options``. See
    #: :func:`paperpush.venues.get_field_option_listers`.
    field_option_listers: dict[str, Callable[..., list[str]]] = {}

    # -- Login-state detection (consumed by :meth:`is_logged_in`) --------------
    #: Playwright role of the marker that tells signed-in from signed-out.
    logged_in_role: str = "link"
    #: Accessible names of that marker; the first visible one wins, so a tuple
    #: covers the wording differences between a portal's deployments.
    logged_in_names: tuple[str, ...] = ()
    #: Polarity: ``True`` when a visible marker means signed *in* (a dashboard
    #: link); ``False`` when it means signed *out* (a sign-in field, as on SNAPP).
    logged_in_present_means_in: bool = True
    #: How long :meth:`is_logged_in` waits for the marker before deciding.
    logged_in_timeout_ms: int = 6000
    #: CSS selector of the iframe the marker lives in, when a portal renders its
    #: dashboard inside one (Editorial Manager); ``None`` roots the check on the page.
    login_frame_selector: str | None = None
    #: Whether :meth:`save_session` can capture a signed-in session by hand. The
    #: ScholarOne journals cannot, so they set this ``False`` and ``save_session``
    #: raises rather than opening a browser that can never sign in.
    supports_session_capture: bool = True
    #: Whether the portal requires an author to sign in before submitting.
    #: Loginless systems (EditFlow) set this ``False``: the CLI then skips the
    #: credential prompt, :meth:`ensure_signed_in` only opens ``portal_url``, and
    #: :meth:`save_session` refuses because there is no session to capture.
    requires_login: bool = True
    #: Whether :meth:`login` accepts ``orcid=True`` -- i.e. whether this portal's
    #: "Sign in with ORCID" control has been recorded. Default ``False``, and it
    #: is the contract: callers check this attribute and refuse with
    #: :func:`~paperpush.venues.login.orcid_unsupported` rather than passing the
    #: flag, so a venue that has not recorded that flow never has to guard
    #: against it. Independent of
    #: :func:`paperpush.venues.orcid_login_offered`, which says whether the
    #: ``--orcid`` *option* applies to the venue at all.
    supports_orcid_login: bool = False

    # -- The two venue-specific operations (required) --------------------------

    @abstractmethod
    def submit(
        self,
        values: dict,
        *,
        headless: bool = False,
        debug: bool = False,
        new_session: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        keep_open_on_failure: bool = True,
    ) -> None:
        """Drive the submission wizard from a ``.sub``'s parsed field values.

        ``keep_open_on_failure`` (the default) leaves a headed run's browser
        window open at the step that raised, instead of letting Playwright close
        it as the exception unwinds; implementations get this by wrapping their
        ``sync_playwright`` block in
        :func:`~paperpush.venues.common.hold_open_on_failure`.
        """

    @abstractmethod
    def login(self, page, username: str, password: str, *, orcid: bool = False, timeout_ms: int = 15000) -> None:
        """Fill and submit the venue's sign-in form; raise on failure.

        Should raise a subclass of
        :class:`paperpush.venues.login.VenueLoginError` when the sign-in does
        not take, so :meth:`ensure_signed_in` can fall back to a manual sign-in.
        The standard three-field form is a one-liner via
        :func:`paperpush.venues.login.fill_login_form`.

        ``orcid`` selects the same page's "Sign in with ORCID" branch: the portal
        link opens ORCID's popup, and ``username``/``password`` are the author's
        ORCID iD (or registered email) and ORCID password rather than a venue
        account. One method rather than two keeps the shared parts -- loading the
        sign-in page, dismissing cookie banners, checking the author area actually
        loaded -- written once; a long ORCID branch belongs in a private helper
        the venue keeps to itself, not in a second entry point.

        Only a venue that sets :attr:`supports_orcid_login` is ever passed the
        flag: both callers check that first and refuse otherwise, so a venue with
        no ORCID branch omits the parameter entirely rather than carrying one it
        would only ignore.
        """

    # -- Portal URLs (default to the venue database) ---------------------------

    @property
    def portal_url(self) -> str:
        """Dashboard URL used to check for a live session. Overridable."""
        return get_venue(self.slug).submission_url

    @property
    def login_url(self) -> str:
        """URL of the sign-in form (for the manual fallback). Defaults to the portal."""
        return self.portal_url

    @property
    def display_name(self) -> str:
        """Human-readable venue name used in log and console messages."""
        return get_venue(self.slug).name

    def _manual_signin_url(self, prefill_email: str | None) -> str:
        """URL to open for the manual sign-in fallback.

        Defaults to :attr:`login_url`. A portal whose sign-in form can prefill the
        email from a query parameter (openRxiv) overrides this to save the human
        retyping their address.
        """
        return self.login_url

    # -- Login state and sign-in (shared; not overridden in the common case) ---

    def _login_root(self, page):
        """Locator root the login-state marker is searched under.

        The page itself, or the content iframe named by
        :attr:`login_frame_selector` when a portal renders its dashboard in one.
        """
        if self.login_frame_selector:
            return page.frame_locator(self.login_frame_selector)
        return page

    def is_logged_in(self, page, *, timeout_ms: int | None = None) -> bool:
        """Return whether ``page`` shows a signed-in session.

        Config-driven from the ``logged_in_*`` attributes: looks for the first
        visible ``logged_in_role`` element named by one of ``logged_in_names``
        under :meth:`_login_root`, then applies ``logged_in_present_means_in`` so a
        portal whose only reliable marker is the *sign-in* field (SNAPP) reads the
        opposite polarity. A venue with an irreducibly custom check may still
        override this method.
        """
        if timeout_ms is None:
            timeout_ms = self.logged_in_timeout_ms
        present = first_visible(self._login_root(page), self.logged_in_role, self.logged_in_names, timeout_ms) is not None
        return present if self.logged_in_present_means_in else not present

    def _sign_in_with_credential(self, page, cred) -> bool:
        """Drive :meth:`login` for a stored credential. True if the sign-in took.

        The one place the stored credential's ``method`` becomes an argument: an
        ORCID credential calls ``login(..., orcid=True)``, anything else the plain
        form. A venue that has not recorded its ORCID branch is not called at all
        -- :attr:`supports_orcid_login` is checked first. Every foreseeable
        failure (no ORCID branch, or a
        :class:`~paperpush.venues.login.VenueLoginError` because the sign-in did
        not take) returns ``False`` after explaining why, so the caller falls back
        to a manual sign-in rather than aborting a submission. ``cred`` may be
        ``None`` or incomplete, which is simply "nothing to sign in with".

        Shared by this class's :meth:`ensure_signed_in` and the portal overrides
        of it. Does not save the session -- the caller owns that, since it holds
        the browser context.
        """
        if cred is None or not cred.username or not cred.password:
            return False
        name = self.display_name
        via_orcid = cred.method == "orcid"
        if via_orcid and not self.supports_orcid_login:
            exc = orcid_unsupported(self)
            logger.warning("No automated ORCID sign-in for %s: %s", name, exc)
            print(f"Cannot sign in with ORCID automatically: {exc}")
            return False
        kind = "ORCID credentials" if via_orcid else "credentials"
        try:
            logger.info("Signing in to %s with stored %s", name, kind)
            print(f"Signing in to {name} with your stored {kind}…")
            # Passed only when set, so a venue with no ORCID branch does not have
            # to declare the parameter just to ignore it.
            if via_orcid:
                self.login(page, cred.username, cred.password, orcid=True)
            else:
                self.login(page, cred.username, cred.password)
        except VenueLoginError as exc:
            logger.warning("Automatic %s sign-in failed: %s", name, exc)
            print(f"Automatic sign-in failed: {exc}")
            logger.info("Falling back to a manual %s sign-in", name)
            print("Falling back to a manual sign-in.")
            return False
        return True

    def ensure_signed_in(self, page, context, *, debug: bool = False) -> bool:
        """Get ``page`` to a signed-in portal without retyping a login.

        Order of preference:
          1. A live session (the saved ``storage_state`` already loaded into
             ``context``, or cookies the portal still honors) -- silent.
          2. Stored credentials (``paperpush login <slug>``): fill the sign-in
             form via :meth:`login` -- with ``orcid=True`` when the stored
             credential is an ORCID one -- then save the session so the next run
             skips even this step.
          3. Manual sign-in -- only if there are no usable credentials or the
             credential sign-in failed.

        Returns ``True`` if it left the page signed in. Returns ``False`` only in
        debug mode's manual path, where the caller's ``page.pause()`` lets you sign
        in by hand in the Inspector.
        """
        name = self.display_name

        if not self.requires_login:
            page.goto(self.portal_url)
            logger.info("%s does not require sign-in", name)
            return True

        session = self.session_path()

        page.goto(self.portal_url)
        if self.is_logged_in(page):
            logger.info("Reusing the saved %s session", name)
            return True
        if session.exists():
            logger.info("Saved %s session expired; signing in again", name)

        cred = None
        try:
            cred = credentials.get_credential(self.slug)
        except Exception as exc:  # noqa: BLE001 -- a missing/unreadable credential is not fatal
            logger.debug("Could not load stored %s credentials (%s)", name, exc)
            cred = None

        if self._sign_in_with_credential(page, cred):
            save_storage(context, session)
            logger.info("Signed in to %s; saved the session for reuse", name)
            print("Signed in; saved the session for next time.")
            return True

        # No usable credentials, or the auto sign-in failed: show the sign-in page,
        # prefilling the known email where the portal supports it (see the hook).
        page.goto(self._manual_signin_url(cred.username if cred else None))
        if debug:
            logger.debug("Debug mode: leaving sign-in to the Inspector pause")
            return False

        logger.info("Falling back to a manual %s sign-in", name)
        wait_for_human(f"Sign in to {name} in the browser window")
        save_storage(context, session)
        return True

    # -- Session persistence ---------------------------------------------------

    def session_path(self) -> Path:
        """Path to this venue's saved Playwright session (storage_state).

        Defaults to the per-slug location under the config directory. A venue that
        stores its session under a shared portal's slug overrides this.
        """
        return _slug_session_path(self.slug)

    def save_session(self, headless: bool = False) -> str:
        """Open the portal, sign in, and save the session for later reuse.

        Launches a browser, drives :meth:`ensure_signed_in` (stored credentials
        when present, otherwise a manual sign-in), and writes the post-login
        ``storage_state`` so a later ``submit`` starts already signed in. A venue
        whose recorded flow cannot script a fresh sign-in sets
        :attr:`supports_session_capture` to ``False``, and this raises instead.
        """
        if not self.requires_login:
            raise NotImplementedError(f"{self.slug} does not require or use a saved login session")
        if not self.supports_session_capture:
            raise NotImplementedError(f"{self.slug} does not support capturing a session by hand")

        path = self.session_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("Opening %s to capture a signed-in session", self.display_name)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context()
            apply_default_timeouts(context)
            page = context.new_page()
            self.ensure_signed_in(page, context)
            context.storage_state(path=str(path))
            browser.close()
        logger.info("Captured %s session to %s", self.display_name, path)
        return str(path)
