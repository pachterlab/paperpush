"""Recover a submission run in place when one of its browser actions fails.

A runner is a long straight-line Playwright script. When one action fails
(a renamed button, a slow page, a modal in the way), the exception used to
unwind the whole run. This module puts a recovery point under each action:

* :func:`guard` wraps the run's ``page``. The wrapper and every locator it hands
  out behave like the real ones, except that a failing *action* (``click``,
  ``fill``, ``check``, ``select_option``, ``wait_for``, ...) is passed to the
  active :class:`RecoveryHandler` before anything is raised.
* The handler sees what the script was trying to do (:class:`FailedAction`) and
  returns a :class:`Resolution`. ``RESOLVED`` means the page is now in the state
  that action was meant to produce, so the action counts as done and the script
  carries on with its next line. ``RETRY`` runs the action again. ``ABORT``
  raises the original error, the same as with no wrapper.
* :func:`step` labels the part of the wizard the script is in ("Authors page"),
  so a handler, and the log, can say *where* the run was and not just *which
  selector* failed.
* :meth:`GuardedPage.attempt` makes a group of actions one recoverable unit. It
  is for sequences that only make sense together, such as a click that opens a
  native file chooser plus handing it the file. Actions inside the unit are not
  recovered one by one. A failure anywhere in it goes to the handler as one
  failure, described by the unit.

The handler comes from :func:`use_handler`, a context manager the caller (the
``submit`` command, the MCP server) puts around the run, so no runner's
``submit`` signature changes. With no handler set, :class:`RaiseHandler` is used.
It aborts every failure, which keeps a run exactly as it behaved before this
module existed. :class:`HumanHandler` asks in the terminal instead: fix the
page by hand, then continue or retry.

Sign-in is not guarded. Runners pass the raw page to ``ensure_signed_in`` and
call :func:`guard` afterwards, because a password, CAPTCHA, or two-factor
prompt is only for the author to handle.
"""

from __future__ import annotations

import enum
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from playwright.sync_api import Error as PlaywrightError

logger = logging.getLogger(__name__)

#: Locator methods that act on the page or wait for it. A failure in one of these
#: goes to the recovery handler. Everything else (``count``, ``is_visible``,
#: ``text_content``...) is a query that the script branches on, so it passes
#: straight through. Re-raising a "not found" from a query would change what the
#: script does next, which is not a recovery.
LOCATOR_ACTIONS = frozenset(
    {
        "check",
        "uncheck",
        "set_checked",
        "click",
        "dblclick",
        "tap",
        "hover",
        "fill",
        "clear",
        "press",
        "press_sequentially",
        "type",
        "select_option",
        "set_input_files",
        "wait_for",
    }
)

#: Methods on a page or locator that return another locator. Their result is
#: wrapped too, so the chain stays guarded.
LOCATOR_FACTORIES = frozenset(
    {
        "locator",
        "get_by_role",
        "get_by_text",
        "get_by_label",
        "get_by_placeholder",
        "get_by_alt_text",
        "get_by_title",
        "get_by_test_id",
        "nth",
        "filter",
        "and_",
        "or_",
        "frame_locator",
    }
)

#: Locator / frame-locator properties that return another locator. ``content_frame``
#: turns an ``iframe`` locator into a frame locator, the way Editorial Manager
#: and SNAPP reach their wizards.
LOCATOR_PROPERTIES = frozenset({"first", "last", "content_frame", "owner"})


class Resolution(enum.Enum):
    """What a :class:`RecoveryHandler` decided about one failed action."""

    #: The page is now in the state the action was meant to produce. Treat the
    #: action as done and continue with the script's next line.
    RESOLVED = "resolved"
    #: Something in the way was cleared (a modal, a slow page). Run the action again.
    RETRY = "retry"
    #: Give up. The original error is raised.
    ABORT = "abort"


@dataclass(frozen=True)
class FailedAction:
    """One action that failed, as handed to a :class:`RecoveryHandler`.

    ``action`` is the method that failed, written as a call
    (``click()``, ``fill('Jane Doe')``). ``target`` is the locator chain it was
    called on, written the way the script built it
    (``get_by_role('button', name='Save / Continue')``). ``step`` is the label
    from the most recent :func:`step` call, or ``None``. ``attempt`` counts from
    1 and goes up on each ``RETRY`` of the same action.
    """

    action: str
    target: str
    error: BaseException
    step: str | None
    url: str
    attempt: int = 1

    def describe(self) -> str:
        """A one-line summary for logs and prompts."""
        where = f" during {self.step!r}" if self.step else ""
        target = f"{self.target}." if self.target else ""
        return f"{target}{self.action} failed{where} on {self.url}: {type(self.error).__name__}: {_first_line(self.error)}"


@dataclass
class RecoveryEvent:
    """A record of one failure and how it was resolved, kept on the guard."""

    failure: FailedAction
    resolution: Resolution


class RecoveryHandler(Protocol):
    """Decides what happens when a guarded action fails.

    ``page`` is the real (unguarded) Playwright page, so whatever the handler
    does there is not itself intercepted. A handler may act on the page before
    returning ``RESOLVED`` or ``RETRY``.
    """

    def recover(self, page: Any, failure: FailedAction) -> Resolution: ...


class RaiseHandler:
    """Abort every failure, the same as running without a guard (the default)."""

    def recover(self, page: Any, failure: FailedAction) -> Resolution:
        return Resolution.ABORT


class HumanHandler:
    """Ask in the terminal: the author fixes the page by hand, then picks how to go on.

    With no terminal to ask on (stdin closed or captured, as under the MCP
    server), the failure is aborted. The run then fails exactly as it does
    without a handler.

    With ``log_dir``, every failure and its outcome is appended to
    ``recoveries.jsonl`` there (``handler: "by-hand"``), the same log the agent
    and ``--ai`` write, so ``paperpush recovery-report`` can turn it into a bug
    report. Clicks made in the browser cannot be recorded, so after a fix the
    author is asked, optionally, what they changed; that note is the record.
    The directory is created on the first failure, and ``on_first_log`` (if
    given) runs then, to record the run's details next to the log.
    """

    PROMPT = "    [Enter] I did it by hand, continue   [r] retry the step   [a] abort the run: "
    NOTE_PROMPT = "    What did you change? One line helps fix this venue for everyone (Enter to skip): "

    def __init__(
        self,
        input_fn: Callable[[str], str] = input,
        output: Callable[[str], None] = print,
        *,
        log_dir: str | os.PathLike | None = None,
        on_first_log: Callable[[], None] | None = None,
    ):
        self._input = input_fn
        self._print = output
        self.log_dir = Path(log_dir) if log_dir is not None else None
        self._on_first_log = on_first_log
        self._logged = False

    def recover(self, page: Any, failure: FailedAction) -> Resolution:
        resolution = self._ask(failure)
        note = self._ask_note() if resolution is not Resolution.ABORT and self.log_dir is not None else ""
        self._log(failure, resolution, note)
        return resolution

    def _ask(self, failure: FailedAction) -> Resolution:
        self._print(f"\n>>> The run got stuck: {failure.describe()}")
        self._print("    The browser is paused there. Fix the page by hand, or do this step yourself.")
        while True:
            try:
                answer = self._input(self.PROMPT).strip().lower()
            except (EOFError, KeyboardInterrupt, OSError):
                return Resolution.ABORT
            if answer in ("", "c", "continue"):
                return Resolution.RESOLVED
            if answer in ("r", "retry"):
                return Resolution.RETRY
            if answer in ("a", "abort", "q", "quit"):
                return Resolution.ABORT
            self._print(f"    Unrecognized answer {answer!r}.")

    def _ask_note(self) -> str:
        try:
            return self._input(self.NOTE_PROMPT).strip()
        except (EOFError, KeyboardInterrupt, OSError):
            return ""

    def _log(self, failure: FailedAction, resolution: Resolution, note: str) -> None:
        if self.log_dir is None:
            return
        # Imported here: recovery_channel imports this module.
        from .recovery_channel import append_log

        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            if not self._logged and self._on_first_log is not None:
                self._on_first_log()
        except OSError as exc:
            logger.warning("recovery: could not create %s: %s", self.log_dir, exc)
            return
        self._logged = True
        append_log(self.log_dir, failure, resolution, [], note, handler="by-hand")


class FallbackHandler:
    """Ask each handler in turn until one does not abort.

    ``submit --ai`` in a terminal uses this as AI first, then the person, so a
    step the model gives up on is still the author's to fix by hand.
    """

    def __init__(self, *handlers: RecoveryHandler):
        self.handlers = handlers

    def recover(self, page: Any, failure: FailedAction) -> Resolution:
        for handler in self.handlers:
            resolution = handler.recover(page, failure)
            if resolution is not Resolution.ABORT:
                return resolution
        return Resolution.ABORT


_handler: ContextVar[RecoveryHandler | None] = ContextVar("paperpush_recovery_handler", default=None)
_step: ContextVar[str | None] = ContextVar("paperpush_recovery_step", default=None)
# >0 while inside an attempt() unit or an optional step, where single actions are
# not recovered on their own.
_suspended: ContextVar[int] = ContextVar("paperpush_recovery_suspended", default=0)


@contextmanager
def use_handler(handler: RecoveryHandler | None) -> Iterator[None]:
    """Make ``handler`` the one :func:`guard` picks up for runs inside this block."""
    token = _handler.set(handler)
    try:
        yield
    finally:
        _handler.reset(token)


def current_handler() -> RecoveryHandler:
    """The handler set by :func:`use_handler`, or :class:`RaiseHandler`."""
    return _handler.get() or RaiseHandler()


def step(label: str) -> None:
    """Record that the script has moved on to the part of the wizard named ``label``."""
    logger.debug("step: %s", label)
    _step.set(label)


def current_step() -> str | None:
    """The label from the latest :func:`step` call, or ``None``."""
    return _step.get()


@contextmanager
def unrecovered() -> Iterator[None]:
    """Turn off per-action recovery inside this block.

    For any action whose failure the caller already expects and handles: a
    best-effort step (:func:`paperpush.venues.common._try`), or a probe such as
    ``try: x.wait_for(timeout=2000) except PWTimeout: <other branch>``. Without
    this, every such expected failure would first go to the handler, which under
    ``--ai`` means a model call. Wrap the probe::

        with unrecovered():
            x.wait_for(state="visible", timeout=2000)
    """
    token = _suspended.set(_suspended.get() + 1)
    try:
        yield
    finally:
        _suspended.reset(token)


@dataclass
class _GuardState:
    raw_page: Any
    handler: RecoveryHandler
    max_attempts: int
    events: list[RecoveryEvent] = field(default_factory=list)

    def run(self, action: str, target: str, call: Callable[[], Any]) -> Any:
        """Run ``call``. On failure, ask the handler and act on its answer."""
        if _suspended.get():
            return call()
        attempt = 1
        while True:
            try:
                return call()
            except PlaywrightError as exc:
                failure = FailedAction(action=action, target=target, error=exc, step=current_step(), url=_page_url(self.raw_page), attempt=attempt)
                logger.warning("guarded action failed: %s", failure.describe())
                resolution = self._ask(failure)
                self.events.append(RecoveryEvent(failure, resolution))
                logger.info("recovery: %s -> %s", failure.describe(), resolution.value)
                if resolution is Resolution.RESOLVED:
                    return None
                if resolution is Resolution.RETRY and attempt < self.max_attempts:
                    attempt += 1
                    continue
                raise

    def _ask(self, failure: FailedAction) -> Resolution:
        try:
            resolution = self.handler.recover(self.raw_page, failure)
        except Exception:  # noqa: BLE001 -- a broken handler must not hide the real failure
            logger.exception("recovery handler raised; aborting the failed action")
            return Resolution.ABORT
        if not isinstance(resolution, Resolution):
            logger.error("recovery handler returned %r, not a Resolution; aborting", resolution)
            return Resolution.ABORT
        return resolution


class GuardedLocator:
    """A Playwright locator (or frame locator) whose actions go through the recovery handler.

    A frame locator has no actions of its own, only the factories that reach
    into the frame, so the same wrapper serves both.
    """

    __slots__ = ("_locator", "_state", "_desc")

    def __init__(self, locator: Any, state: _GuardState, desc: str):
        self._locator = locator
        self._state = state
        self._desc = desc

    def unwrap(self) -> Any:
        """The real locator (for ``expect`` and other APIs that type-check it)."""
        return self._locator

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._locator, name)
        if name in LOCATOR_PROPERTIES:
            return GuardedLocator(attr, self._state, f"{self._desc}.{name}")
        if name in LOCATOR_FACTORIES:
            return _wrap_factory(attr, self._state, f"{self._desc}.", name)
        if name in LOCATOR_ACTIONS:
            return _wrap_action(attr, self._state, self._desc, name)
        return attr

    def __repr__(self) -> str:
        return f"<GuardedLocator {self._desc}>"


class GuardedPage:
    """A Playwright page whose locators' actions go through the recovery handler.

    Everything not listed in :data:`LOCATOR_FACTORIES` passes straight to the
    real page: navigation, ``once``, ``expect_file_chooser``, ``wait_for_timeout``,
    ``pause``. The failures recorded so far are in :attr:`events`.
    """

    __slots__ = ("_state",)

    def __init__(self, state: _GuardState):
        self._state = state

    @property
    def events(self) -> list[RecoveryEvent]:
        return self._state.events

    def unwrap(self) -> Any:
        """The real page."""
        return self._state.raw_page

    def attempt(self, description: str, fn: Callable[[Any], Any]) -> Any:
        """Run ``fn(page)`` as one recoverable unit described by ``description``.

        Single actions inside ``fn`` are not recovered on their own. If anything
        in ``fn`` raises a Playwright error, the handler gets one failure
        described as ``description``. On ``RETRY`` the whole unit runs again.
        """

        def call() -> Any:
            with unrecovered():
                return fn(self)

        return self._state.run(description, "", call)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._state.raw_page, name)
        if name in LOCATOR_FACTORIES:
            return _wrap_factory(attr, self._state, "", name)
        return attr

    def __repr__(self) -> str:
        return f"<GuardedPage {self._state.raw_page!r}>"


def guard(page: Any, handler: RecoveryHandler | None = None, *, max_attempts: int = 3) -> GuardedPage:
    """Wrap ``page`` so its failing actions go to ``handler`` (default: :func:`current_handler`).

    ``max_attempts`` caps how many times one action may be retried before its
    error is raised. It also clears the step label left over from an earlier run.
    """
    if isinstance(page, GuardedPage):
        return page
    _step.set(None)
    return GuardedPage(_GuardState(raw_page=page, handler=handler or current_handler(), max_attempts=max_attempts))


def _wrap_factory(method: Callable, state: _GuardState, prefix: str, name: str) -> Callable:
    def factory(*args: Any, **kwargs: Any) -> GuardedLocator:
        args = tuple(a.unwrap() if isinstance(a, GuardedLocator) else a for a in args)
        kwargs = {k: (v.unwrap() if isinstance(v, GuardedLocator) else v) for k, v in kwargs.items()}
        return GuardedLocator(method(*args, **kwargs), state, f"{prefix}{_format_call(name, args, kwargs)}")

    return factory


def _wrap_action(method: Callable, state: _GuardState, target: str, name: str) -> Callable:
    def action(*args: Any, **kwargs: Any) -> Any:
        return state.run(_format_call(name, args, kwargs), target, lambda: method(*args, **kwargs))

    return action


def _format_call(name: str, args: tuple, kwargs: dict) -> str:
    parts = [_short_repr(a) for a in args] + [f"{k}={_short_repr(v)}" for k, v in kwargs.items()]
    return f"{name}({', '.join(parts)})"


def _short_repr(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else ""


def _page_url(page: Any) -> str:
    try:
        return str(page.url)
    except Exception:  # noqa: BLE001 -- a closed page has no url; the failure is still worth reporting
        return "<unknown>"
