"""Let an outside agent fix a paused submission run in its live browser.

:class:`AgentHandler` is the :class:`~paperpush.venues.recovery.RecoveryHandler`
behind ``paperpush submit --recover agent``. When a guarded action fails, it
pauses the run and waits for an agent in another process (the MCP server, on
behalf of Claude Code or another client) to look at the page, act on it, and
say how to continue.

The two processes talk through files in one *recovery directory*:

``paused.json``
    Written by the runner while it is paused: the failed action, the step label,
    the URL, and a screenshot path. Removed on resume.
``command.json`` / ``reply.json``
    One request and its answer at a time. The client writes a command, the runner
    carries it out on the real page and writes the reply. Both files are written
    to a temporary name and renamed, so neither side ever reads half a file.
``recoveries.jsonl``
    One line per failure the agent resolved: what failed, what the agent did,
    and its note. This is the raw material for fixing the venue script itself
    (a renamed button shows up here with the control that worked instead).

The runner carries out every action itself, so the guardrails are enforced in
one place no matter which client sends the commands:

* Nothing whose label reads as the portal's final submit (``Submit``,
  ``Approve``, ... or the venue's own :attr:`Venue.final_submit_names`) can be
  clicked, and ``Enter`` cannot be pressed. Sending the submission stays the
  author's decision.
* Uploads are limited to files named in the ``.sub``.
* There is no navigation and no JavaScript evaluation.

An agent with a shell can also attach to the same browser over CDP
(``submit --cdp-port``, then ``playwright-cli attach --cdp=<url>``). That route
bypasses these guardrails, which is why it is off unless asked for.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from playwright.sync_api import Error as PlaywrightError

from .recovery import FailedAction, Resolution

logger = logging.getLogger(__name__)

PAUSED_FILE = "paused.json"
COMMAND_FILE = "command.json"
REPLY_FILE = "reply.json"
LOG_FILE = "recoveries.jsonl"

#: How long a paused run waits for the agent to resume it before giving up and
#: failing the way an unguarded run does.
PAUSE_TIMEOUT_SECONDS = 30 * 60
#: How long the client waits for the runner to answer one command.
REPLY_TIMEOUT_SECONDS = 60.0
#: How often the paused runner looks for a command.
POLL_MS = 200
#: Cap on the accessibility snapshot handed back, in characters.
SNAPSHOT_LIMIT = 30_000
#: Cap on one ``wait`` command.
MAX_WAIT_MS = 10_000

#: Labels of the control that sends the submission, matched after lower-casing
#: and collapsing whitespace. A venue adds its own wording through
#: ``Venue.final_submit_names``.
DEFAULT_FINAL_SUBMIT_NAMES = (
    "submit",
    "submit manuscript",
    "submit submission",
    "submit paper",
    "submit now",
    "confirm submission",
    "complete submission",
    "finish submission",
    "send",
    "send submission",
)
#: Labels that start like a final step on any portal ("Approve Submission",
#: "Finalize and send").
FINAL_SUBMIT_PREFIX_RE = re.compile(r"^(approve|finali[sz]e|build pdf for approval)\b")

ACTIONS = ("click", "fill", "select", "check", "uncheck", "press", "upload", "wait")
TARGET_KINDS = ("role", "label", "text", "placeholder", "css")
BLOCKED_KEYS = frozenset({"enter", "numpadenter"})

_cdp_port: ContextVar[int | None] = ContextVar("paperpush_cdp_port", default=None)


class ChannelError(RuntimeError):
    """The client could not reach a paused run (not paused, no answer, bad reply)."""


# ---------------------------------------------------------------------------
# Browser launch: the optional CDP endpoint
# ---------------------------------------------------------------------------


def free_port() -> int:
    """A TCP port on 127.0.0.1 that is free right now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def use_cdp_port(port: int | None) -> Iterator[None]:
    """Have runners inside this block open a CDP endpoint on ``127.0.0.1:port``."""
    token = _cdp_port.set(port)
    try:
        yield
    finally:
        _cdp_port.reset(token)


def browser_launch_args() -> list[str]:
    """Extra Chromium arguments for a runner's ``launch()`` (empty unless CDP is on)."""
    port = _cdp_port.get()
    if port is None:
        return []
    return [f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"]


def cdp_endpoint(port: int) -> str:
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# What the agent may touch
# ---------------------------------------------------------------------------


def allowed_upload_paths(values: dict[str, str]) -> set[str]:
    """Every existing file named anywhere in the ``.sub`` values.

    File fields take a path per line, sometimes followed by ``|``-separated
    metadata (``fig1.png | Figure 1``), so each piece is tried. Only paths that
    exist are kept, as resolved absolute paths.
    """
    found: set[str] = set()
    for value in values.values():
        if not isinstance(value, str):
            continue
        for line in value.splitlines():
            for piece in line.split("|"):
                piece = piece.strip()
                if not piece or len(piece) > 4096:
                    continue
                try:
                    path = Path(piece).expanduser()
                    if path.is_file():
                        found.add(str(path.resolve()))
                except OSError:
                    continue
    return found


def is_final_submit_label(label: str, extra_names: Iterable[str] = ()) -> bool:
    """Whether ``label`` reads as the control that sends the submission."""
    norm = " ".join(label.lower().split())
    if not norm:
        return False
    names = {" ".join(n.lower().split()) for n in (*DEFAULT_FINAL_SUBMIT_NAMES, *extra_names)}
    return norm in names or bool(FINAL_SUBMIT_PREFIX_RE.match(norm))


def build_locator(page: Any, target: dict[str, Any]) -> Any:
    """Turn an agent's target description into a Playwright locator.

    ``target`` names exactly one of ``role`` (with optional ``name`` and
    ``exact``), ``label``, ``text``, ``placeholder``, or ``css``, plus an
    optional 0-based ``nth`` for when several elements match.
    """
    if not isinstance(target, dict):
        raise ValueError("target must be an object such as {'role': 'button', 'name': 'Next'}")
    kinds = [k for k in TARGET_KINDS if k in target]
    if len(kinds) != 1:
        raise ValueError(f"target must name exactly one of {', '.join(TARGET_KINDS)}; got {sorted(target)}")
    kind = kinds[0]
    exact = bool(target.get("exact", False))
    if kind == "role":
        kwargs: dict[str, Any] = {}
        if target.get("name") is not None:
            kwargs["name"] = str(target["name"])
            kwargs["exact"] = exact
        locator = page.get_by_role(str(target["role"]), **kwargs)
    elif kind == "label":
        locator = page.get_by_label(str(target["label"]), exact=exact)
    elif kind == "text":
        locator = page.get_by_text(str(target["text"]), exact=exact)
    elif kind == "placeholder":
        locator = page.get_by_placeholder(str(target["placeholder"]), exact=exact)
    else:
        locator = page.locator(str(target["css"]))
    if "nth" in target:
        locator = locator.nth(int(target["nth"]))
    return locator


def describe_target(target: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in target.items())


_LABEL_JS = "el => [el.getAttribute('aria-label'), el.innerText, el.value, el.getAttribute('title')].filter(Boolean).join(' ')"


# ---------------------------------------------------------------------------
# Runner side
# ---------------------------------------------------------------------------


def append_log(directory: str | os.PathLike, failure: FailedAction, resolution: Resolution, actions: list, note: str, **extra: Any) -> None:
    """Append one handled failure to ``recoveries.jsonl`` in ``directory``.

    The one format every handler writes (agent, ``--ai``, by hand), which
    ``read_log``, the MCP status, and ``paperpush recovery-report`` read.
    """
    entry = {**failure_dict(failure), "resolution": resolution.value, "agent_actions": actions, "note": note, **extra, "at": _now()}
    try:
        with (Path(directory) / LOG_FILE).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.warning("recovery: could not append to %s: %s", LOG_FILE, exc)


class PageTools:
    """The actions a recovering agent may take on a paused page, with the guardrails.

    Shared by :class:`AgentHandler` (an agent in another process) and
    :class:`paperpush.venues.recovery_ai.ApiAgentHandler` (a model called from
    inside the run), so both enforce the same rules. ``directory`` receives the
    screenshots and ``recoveries.jsonl``.
    """

    def __init__(self, directory: str | os.PathLike, *, allowed_uploads: Iterable[str] = (), final_submit_names: Iterable[str] = ()):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.allowed_uploads = {str(Path(p).resolve()) for p in allowed_uploads}
        self.final_submit_names = tuple(final_submit_names)
        self._screens = 0
        #: How an earlier, similar failure in this run was fixed, set by
        #: :class:`RememberingHandler` so the agent can repeat it.
        self.hint: dict[str, Any] | None = None

    def act(self, page: Any, op: str, target: dict[str, Any] | None = None, value: Any = None) -> dict[str, Any]:
        """Carry out one action. Returns ``{"ok": True, ...}`` or ``{"ok": False, "error": ...}``."""
        if op not in ACTIONS:
            return {"ok": False, "error": f"unknown action {op!r}; use one of {', '.join(ACTIONS)}"}
        try:
            detail = self._act(page, op, target, value)
        except (PlaywrightError, ValueError, TypeError, PermissionError) as exc:
            return {"ok": False, "error": _error_text(exc)}
        return {"ok": True, "url": page.url, **detail}

    def _act(self, page: Any, op: str, target: dict[str, Any] | None, value: Any) -> dict[str, Any]:
        timeout = 5000
        if op == "wait":
            ms = max(0, min(int(value or 1000), MAX_WAIT_MS))
            page.wait_for_timeout(ms)
            return {"waited_ms": ms}
        locator = build_locator(page, target)
        if op in ("click", "press"):
            self._refuse_final_submit(locator, target, timeout)
        if op == "click":
            locator.click(timeout=timeout)
        elif op == "fill":
            locator.fill(str(value if value is not None else ""), timeout=timeout)
        elif op == "select":
            try:
                locator.select_option(label=str(value), timeout=timeout)
            except PlaywrightError:
                locator.select_option(value=str(value), timeout=timeout)
        elif op == "check":
            locator.check(timeout=timeout)
        elif op == "uncheck":
            locator.uncheck(timeout=timeout)
        elif op == "press":
            key = str(value or "")
            if key.split("+")[-1].lower() in BLOCKED_KEYS:
                raise PermissionError("pressing Enter is not allowed: it can send a form, and sending the submission is the author's step")
            locator.press(key, timeout=timeout)
        elif op == "upload":
            path = str(Path(str(value or "")).expanduser().resolve())
            if path not in self.allowed_uploads:
                raise PermissionError(f"{value!r} is not one of the files named in the .sub; allowed: {sorted(self.allowed_uploads) or 'none'}")
            is_file_input = locator.evaluate("el => el.tagName === 'INPUT' && el.type === 'file'", timeout=timeout)
            if is_file_input:
                locator.set_input_files(path, timeout=timeout)
            else:
                self._refuse_final_submit(locator, target, timeout)
                with page.expect_file_chooser(timeout=timeout) as chooser:
                    locator.click(timeout=timeout)
                chooser.value.set_files(path)
        return {}

    def _refuse_final_submit(self, locator: Any, target: dict[str, Any], timeout: int) -> None:
        labels = [str(target.get(k) or "") for k in ("name", "text", "label")]
        try:
            labels.append(locator.evaluate(_LABEL_JS, timeout=timeout) or "")
        except PlaywrightError:
            pass  # the action itself will report a missing element
        for label in labels:
            if is_final_submit_label(label, self.final_submit_names):
                raise PermissionError(f"{label.strip()!r} looks like the final submit control; the agent may not press it -- " "sending the submission is the author's step")

    def snapshot(self, page: Any, failure: FailedAction) -> dict[str, Any]:
        """The page as an agent sees it: URL, step, failure, accessibility tree, screenshot path."""
        try:
            aria = page.locator("body").aria_snapshot(timeout=5000)
        except PlaywrightError as exc:
            aria = f"<no accessibility snapshot: {exc}>"
        return {
            "ok": True,
            "url": page.url,
            "title": _safe(page.title),
            "step": failure.step,
            "failure": failure_dict(failure),
            "aria_snapshot": aria[:SNAPSHOT_LIMIT],
            "aria_snapshot_truncated": len(aria) > SNAPSHOT_LIMIT,
            "screenshot": self.screenshot(page),
            **({"earlier_fix": self.hint} if self.hint else {}),
        }

    def screenshot(self, page: Any) -> str | None:
        self._screens += 1
        path = self.directory / f"screenshot-{self._screens}.png"
        try:
            page.screenshot(path=str(path), timeout=10000)
        except PlaywrightError as exc:
            logger.warning("recovery: screenshot failed: %s", exc)
            return None
        return str(path)

    def log(self, failure: FailedAction, resolution: Resolution, actions: list, note: str, **extra: Any) -> None:
        """Append one handled failure to ``recoveries.jsonl``."""
        append_log(self.directory, failure, resolution, actions, note, **extra)


class AgentHandler:
    """Pause on a failure and carry out an outside agent's commands until it resumes."""

    def __init__(
        self,
        directory: str | os.PathLike,
        *,
        allowed_uploads: Iterable[str] = (),
        final_submit_names: Iterable[str] = (),
        pause_timeout: float = PAUSE_TIMEOUT_SECONDS,
        output=print,
    ):
        self.tools = PageTools(directory, allowed_uploads=allowed_uploads, final_submit_names=final_submit_names)
        self.directory = self.tools.directory
        self.pause_timeout = pause_timeout
        self._print = output
        # Leftovers from a crashed run would be read as this run's.
        for name in (PAUSED_FILE, COMMAND_FILE, REPLY_FILE):
            (self.directory / name).unlink(missing_ok=True)

    def recover(self, page: Any, failure: FailedAction) -> Resolution:
        actions: list[dict[str, Any]] = []
        screenshot = self.tools.screenshot(page)
        hint = {"earlier_fix": self.tools.hint} if self.tools.hint else {}
        _write_json(self.directory / PAUSED_FILE, {**failure_dict(failure), "screenshot": screenshot, "paused_at": _now(), **hint})
        self._print(f"\n>>> Paused for agent recovery: {failure.describe()}")
        self._print(f"    Recovery directory: {self.directory}")
        deadline = time.monotonic() + self.pause_timeout
        try:
            while time.monotonic() < deadline:
                command = self._take_command()
                if command is None:
                    try:
                        page.wait_for_timeout(POLL_MS)
                    except PlaywrightError:
                        self._print(">>> The browser went away while paused; aborting.")
                        return Resolution.ABORT
                    continue
                reply, resolution = self._execute(page, failure, command, actions)
                _write_json(self.directory / REPLY_FILE, {"id": command.get("id"), **reply})
                if resolution is not None:
                    note = str(command.get("note") or "")
                    self.tools.log(failure, resolution, actions, note, handler="agent")
                    self._print(f">>> Agent resumed the run: {resolution.value}" + (f" ({note})" if note else ""))
                    return resolution
            self._print(f">>> No agent resumed the run within {self.pause_timeout:.0f}s; aborting.")
            return Resolution.ABORT
        finally:
            (self.directory / PAUSED_FILE).unlink(missing_ok=True)

    def _take_command(self) -> dict[str, Any] | None:
        path = self.directory / COMMAND_FILE
        if not path.exists():
            return None
        try:
            command = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            command = {"op": "invalid", "error": str(exc)}
        path.unlink(missing_ok=True)
        return command if isinstance(command, dict) else {"op": "invalid", "error": "command is not an object"}

    def _execute(self, page: Any, failure: FailedAction, command: dict[str, Any], actions: list) -> tuple[dict[str, Any], Resolution | None]:
        op = command.get("op")
        if op == "snapshot":
            return self.tools.snapshot(page, failure), None
        if op == "resume":
            try:
                resolution = Resolution(str(command.get("resolution", "")).lower())
            except ValueError as exc:
                return {"ok": False, "error": _error_text(exc)}, None
            return {"ok": True, "resolution": resolution.value}, resolution
        if op in ACTIONS:
            reply = self.tools.act(page, op, command.get("target"), command.get("value"))
            if reply["ok"]:
                actions.append({"op": op, **{k: command[k] for k in ("target", "value") if k in command}})
            return reply, None
        return {"ok": False, "error": command.get("error") or f"unknown op {op!r}"}, None


#: Agent actions that only wait or press a named button/link. A fix made of
#: these alone carries no data from the .sub, so repeating it for an identical
#: failure cannot type the previous entry's values into the next one.
_REPLAYABLE_ROLES = frozenset({"button", "link", "tab"})


def _replayable(actions: list[dict[str, Any]]) -> bool:
    if not actions:
        return False  # "resolved" with nothing done (e.g. fixed by hand): never skip a step silently
    for action in actions:
        op = action.get("op")
        if op == "wait":
            continue
        target = action.get("target") or {}
        if op == "click" and isinstance(target, dict) and target.get("role") in _REPLAYABLE_ROLES and set(target) <= {"role", "name", "exact", "nth"}:
            continue
        return False
    return True


def _similar_key(failure: FailedAction) -> tuple:
    """The failure with numbers blurred: row 1 and row 2 of the same widget match."""
    name = failure.action.split("(", 1)[0]
    return (failure.step, name, re.sub(r"\d+", "#", failure.target or ""))


class RememberingHandler:
    """Reuse what fixed a failure earlier in the same run.

    Wraps the handler that does the real work (:class:`AgentHandler`, or the
    in-process model of ``submit --ai``). For each failure it resolves, it
    remembers the actions that worked. Then:

    * The same action on the same element failing again (first attempt) is
      fixed by replaying those actions, without pausing, when they only wait or
      click a named button or link (:func:`_replayable`). A fix that typed,
      selected, or uploaded something is never replayed: on a repeated widget it
      would enter the previous row's data.
    * Otherwise, a failure that matches an earlier one apart from numbers (row
      2 after row 1) goes to the wrapped handler with the earlier fix attached
      as ``earlier_fix`` (in ``paused.json`` and page snapshots), so the agent
      can repeat it with the right index and values.

    Nothing is kept between runs; each run starts with an empty memory.
    """

    def __init__(self, inner: Any, tools: PageTools, output=print):
        self.inner = inner
        self.tools = tools
        self._print = output
        self._exact: dict[tuple, dict[str, Any]] = {}
        self._similar: dict[tuple, dict[str, Any]] = {}
        # A reused --recover-dir holds earlier runs' entries; they are not this run's.
        self._seen_log = len(read_log(tools.directory))

    def recover(self, page: Any, failure: FailedAction) -> Resolution:
        exact_key = (failure.step, failure.action, failure.target)
        known = self._exact.get(exact_key)
        if known and failure.attempt == 1 and _replayable(known["actions"]):
            resolution = self._replay(page, failure, known)
            if resolution is not None:
                return resolution
        earlier = self._similar.get(_similar_key(failure))
        self.tools.hint = earlier
        try:
            resolution = self.inner.recover(page, failure)
        finally:
            self.tools.hint = None
        if resolution is not Resolution.ABORT:
            self._remember(failure, resolution)
        return resolution

    def _replay(self, page: Any, failure: FailedAction, known: dict[str, Any]) -> Resolution | None:
        self._print(f"\n>>> Step failed the same way as before: {failure.describe()}")
        self._print("    Repeating the fix that worked last time...")
        for action in known["actions"]:
            reply = self.tools.act(page, action["op"], action.get("target"), action.get("value"))
            if not reply.get("ok"):
                self._print(f"    That no longer works ({reply.get('error')}); handing over.")
                return None
        resolution = Resolution(known["resolution"])
        self.tools.log(failure, resolution, known["actions"], f"replayed the fix from {known['at']}", handler="replay")
        self._seen_log += 1
        self._print(f">>> Replayed: {resolution.value}")
        return resolution

    def _remember(self, failure: FailedAction, resolution: Resolution) -> None:
        # The wrapped handler logged what it did; read back its newest entry.
        entries = read_log(self.tools.directory)
        new = entries[self._seen_log :]
        self._seen_log = len(entries)
        if not new:
            return
        entry = new[-1]
        record = {"resolution": resolution.value, "actions": entry.get("agent_actions") or [], "note": entry.get("note") or "", "at": entry.get("at") or _now(), "action": failure.action, "target": failure.target}
        self._exact[(failure.step, failure.action, failure.target)] = record
        self._similar[_similar_key(failure)] = record


def recovery_dir_of(handler: Any) -> Path | None:
    """The recovery directory a handler logs to, looking through wrappers."""
    seen = 0
    while handler is not None and seen < 5:
        tools = getattr(handler, "tools", None)
        if isinstance(tools, PageTools):
            return tools.directory
        log_dir = getattr(handler, "log_dir", None)  # HumanHandler
        if isinstance(log_dir, Path):
            return log_dir
        handlers = getattr(handler, "handlers", None)  # FallbackHandler
        handler = handlers[0] if handlers else getattr(handler, "inner", None)
        seen += 1
    return None


# ---------------------------------------------------------------------------
# Client side (the MCP server)
# ---------------------------------------------------------------------------


def read_paused(directory: str | os.PathLike) -> dict[str, Any] | None:
    """The pause record if the run is waiting for an agent, else ``None``."""
    try:
        return json.loads((Path(directory) / PAUSED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_log(directory: str | os.PathLike) -> list[dict[str, Any]]:
    """Every recovery the agent resolved so far in this run."""
    entries = []
    try:
        lines = (Path(directory) / LOG_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def send_command(directory: str | os.PathLike, command: dict[str, Any], *, timeout: float = REPLY_TIMEOUT_SECONDS, is_alive=lambda: True) -> dict[str, Any]:
    """Hand ``command`` to the paused run in ``directory`` and wait for its reply."""
    directory = Path(directory)
    if read_paused(directory) is None:
        raise ChannelError("the run is not paused for recovery, so there is nothing to act on")
    reply_path = directory / REPLY_FILE
    reply_path.unlink(missing_ok=True)
    command = {**command, "id": f"{time.time_ns()}"}
    _write_json(directory / COMMAND_FILE, command)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if reply_path.exists():
            try:
                reply = json.loads(reply_path.read_text(encoding="utf-8"))
            except ValueError:
                time.sleep(0.05)
                continue
            if reply.get("id") == command["id"]:
                reply_path.unlink(missing_ok=True)
                reply.pop("id", None)
                return reply
        if not is_alive():
            raise ChannelError("the submission process exited before answering")
        time.sleep(0.1)
    (directory / COMMAND_FILE).unlink(missing_ok=True)
    raise ChannelError(f"the run did not answer within {timeout:.0f}s")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def failure_dict(failure: FailedAction) -> dict[str, Any]:
    return {
        "action": failure.action,
        "target": failure.target,
        "step": failure.step,
        "url": failure.url,
        "attempt": failure.attempt,
        "error": f"{type(failure.error).__name__}: {str(failure.error).strip().splitlines()[0] if str(failure.error).strip() else ''}",
        "summary": failure.describe(),
    }


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text.splitlines()[0] if text else ''}"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _safe(fn) -> str:
    try:
        return fn()
    except PlaywrightError:
        return ""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
