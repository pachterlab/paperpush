"""Helpers shared by the per-venue Playwright submission runners.

Each venue has its own module in this package (``biorxiv.py``,
``bioinformatics.py``) that drives that venue's submission wizard. The few
pieces that are not venue-specific live here so the runners share one
implementation instead of each keeping a private copy:

* :func:`wait_for_human` / :func:`hold_open` -- pausing for a manual step and
  keeping the launched browser open after the run finishes.
* :func:`session_path` / :func:`save_storage` -- persisting and locating a
  signed-in Playwright session so later runs skip the login.
* :func:`capture_failure` -- dumping a screenshot and the live HTML of a page
  when a portal check fails, so the changed page can be inspected (and the
  broken selector fixed) offline instead of only from a text diff.

The sign-in helpers (the login error types, ``verify_login``, ``first_visible``,
``fill_login_form``) live next door in :mod:`paperpush.venues.login`, which
imports the session/browser helpers from here.

Playwright is assumed to be installed (``pip install playwright && python -m
playwright install chromium``); the runners import it directly rather than
guarding the import.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..credentials import config_dir

logger = logging.getLogger(__name__)

# Default cap, in seconds, on how long Playwright waits for any single action or
# navigation before giving up. Playwright's own default is 30 seconds; the
# runners lower it so a missing element or a stuck page fails fast instead of
# hanging. The ``login`` and ``submit`` commands expose this as a ``--timeout``
# option, threaded down to :func:`apply_default_timeouts`.
DEFAULT_TIMEOUT_SECONDS = 10.0


def apply_default_timeouts(context, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
    """Cap a context's default action and navigation waits at ``timeout_seconds``.

    Call this on a freshly created browser context, before its first
    ``new_page()``, so every page the context opens inherits the cap. Both the
    per-action timeout (clicks, fills, ``wait_for``) and the navigation timeout
    (``goto``, ``wait_for_load_state``) are set. A ``timeout_seconds`` of 0
    disables the cap, restoring Playwright's "wait forever" behavior.
    """
    ms = int(timeout_seconds * 1000)
    context.set_default_timeout(ms)
    context.set_default_navigation_timeout(ms)


def wait_for_human(prompt: str) -> None:
    """Block until the human presses Enter in the terminal."""
    try:
        input(f"\n>>> {prompt}\n    Press Enter here to continue... ")
    except (EOFError, KeyboardInterrupt):
        pass


def hold_open() -> None:
    """Keep the script (and so the launched browser) alive indefinitely.

    Playwright closes any browser it launched as soon as the ``sync_playwright``
    block exits, so the only way to leave the window open after the click-through
    finishes is to never return. Block here until the human kills the process
    (Ctrl+C / closing the terminal), which is what finally closes the browser.
    """
    print("\nScript finished. The browser is left open for you to take over.")
    print("Press Ctrl+C here (or close this terminal) to close the browser.")
    try:
        while True:
            input()
    except (EOFError, KeyboardInterrupt, OSError):
        # Reaching this point means the run already succeeded -- hold_open is
        # only ever called after a runner finishes the wizard, and its sole job
        # is to pause for a human. So any inability to read a keypress means
        # "no human to wait for; just return cleanly" rather than fail the run:
        #   EOFError        -- stdin is /dev/null (attended `-s` run, EOF)
        #   KeyboardInterrupt-- the human pressed Ctrl+C
        #   OSError         -- stdin is captured by pytest (a bare `pytest
        #                      --run-portal` status check, or the headless CI
        #                      refresh), whose fake stdin raises rather than EOFs
        pass


def session_path(slug: str) -> Path:
    """Path to a venue's saved browser session (Playwright storage_state)."""
    return config_dir() / f"{slug}_session.json"


def save_storage(context, session: Path) -> None:
    """Persist the current browser session for reuse by later runs."""
    session.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(session))
    logger.debug("Saved browser session to %s", session)


def open_run_context(browser, session: Path, *, new_session: bool = False):
    """Open a browser context for a submission run, reusing a saved session.

    When a saved session (Playwright ``storage_state``) exists at ``session``, it
    is loaded into the new context so the run lands straight on the signed-in
    dashboard; the caller's :meth:`~paperpush.venues.base.Venue.ensure_signed_in`
    then verifies it is still valid and, if not, falls back to a fresh sign-in.
    ``new_session`` discards any saved session first, forcing a fresh sign-in
    (use it after switching accounts so a stale session is not silently reused).

    Returns the new context; the caller still applies timeouts and opens pages.
    """
    if new_session and session.exists():
        session.unlink()
        logger.info("Discarded the saved session at %s; starting fresh", session)
        print("Starting a new browser session (discarded the saved one).")
    if session.exists():
        return browser.new_context(storage_state=str(session))
    return browser.new_context()


def capture_failure(page, slug: str, page_name: str, dest_dir: Path) -> dict[str, Path]:
    """Save a screenshot and the live HTML of ``page`` for offline diagnosis.

    When a portal check fails -- a selector the runner depends on is gone, or the
    page's structure drifted from its committed baseline -- the failure text says
    *that* something changed but not *what* the page now looks like. This writes
    two artifacts so the changed page can be inspected, and the broken selector
    re-derived, without re-running against the live portal:

      * ``<slug>_<page_name>.png``  -- a full-page screenshot, and
      * ``<slug>_<page_name>.html`` -- the page's current DOM (``page.content()``).

    Both are best-effort: a browser that has already crashed or navigated away
    may refuse one or the other, so each is guarded and a failure to capture
    never masks the original test failure. Returns a dict of the artifacts
    actually written, keyed ``"screenshot"`` / ``"html"`` (empty if both failed).

    The captures embed whatever the live page held at failure time, including
    per-session tokens, so ``dest_dir`` must be git-ignored (it is: see the
    ``tests/portal_snapshots/_failures`` rule in ``.gitignore``).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    stem = f"{slug}_{page_name}"

    png = dest_dir / f"{stem}.png"
    try:
        page.screenshot(path=str(png), full_page=True)
        written["screenshot"] = png
    except Exception as exc:  # noqa: BLE001 -- capture is best-effort, never fatal
        logger.warning("capture_failure: screenshot of %s failed (%s)", stem, exc)

    html = dest_dir / f"{stem}.html"
    try:
        html.write_text(page.content(), encoding="utf-8")
        written["html"] = html
    except Exception as exc:  # noqa: BLE001 -- capture is best-effort, never fatal
        logger.warning("capture_failure: HTML dump of %s failed (%s)", stem, exc)

    return written


# ---------------------------------------------------------------------------
# Pure-data helpers shared by the per-venue runners.
#
# These reshape a name or parse a ``.sub`` field value for a submission form and
# carry no Playwright or venue-specific logic, so every runner that needs one
# imports it from here instead of keeping a private copy.
# ---------------------------------------------------------------------------


def split_name_first_last(name: str) -> tuple[str, str]:
    """Split a "First Last" name into ``(first, last)`` on the last space.

    Forms with separate first/last (or given/family) name boxes get a single
    ``.sub`` name column split on its last space; a one-word name becomes the
    first name with an empty last name.
    """
    name = (name or "").strip()
    if " " not in name:
        return name, ""
    first, _, last = name.rpartition(" ")
    return first, last


def split_name_first_middle_last(name: str) -> tuple[str, str, str]:
    """Split a "First [Middle...] Last" name into ``(first, middle, last)``.

    Forms with separate first/middle/last boxes get the first token as the first
    name, the last token as the surname, and anything in between as the middle
    name(s). A single token becomes the first name.
    """
    parts = name.split()
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", ""
    return parts[0], " ".join(parts[1:-1]), parts[-1]


def parse_pipe_funders(value: str) -> list[dict]:
    """Parse a funding block into ``{"name", "awards"}`` dicts.

    Each non-empty line is ``Funder name | award1, award2`` with the awards list
    optional. The funder name must match a suggestion offered by the venue's
    funder autocomplete.
    """
    funders: list[dict] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        awards: list[str] = []
        if len(parts) > 1 and parts[1]:
            awards = [a.strip() for a in parts[1].split(",") if a.strip()]
        funders.append({"name": parts[0], "awards": awards})
    return funders


def parse_pipe_file_list(value: str, fields: list[str]) -> list[dict]:
    """Parse a ``|``-delimited file block into a list of dicts.

    Each non-empty line is ``fields[0] | fields[1] | ...`` with trailing fields
    optional (filled with ""). Shared by the figure and supplementary lists,
    which differ only in their per-file metadata columns.
    """
    entries: list[dict] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        parts += [""] * (len(fields) - len(parts))
        entries.append({field: parts[i] for i, field in enumerate(fields)})
    return entries


def declares_no_competing_interest(statement: str) -> bool:
    """True when a competing-interest statement is a "none" declaration.

    An empty statement, or one that says there is no competing interest, is the
    default "none" case; anything else is a real declaration the form must carry
    verbatim.
    """
    s = statement.strip().lower()
    return not s or "no competing interest" in s or "declared no" in s
