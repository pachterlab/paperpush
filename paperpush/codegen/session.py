"""The browser half of ``paperpush codegen``: run the recorder and keep the script in step.

:class:`CodegenSession` opens a headed Chromium with ``recorder.js`` injected
into every frame. The page reports what the author does through the
``__ppRecord`` binding and sends toolbar commands through ``__ppCommand``. Both
only queue a message. The main loop drains the queue, updates the
:class:`~paperpush.codegen.model.Script`, rewrites the output file, and pushes
the new state back to every page (``window.__ppSetState``). Playwright's sync
API dispatches events only while a call is waiting, so the loop waits on the
page in short slices.

The code panel can also live in a separate window (the toolbar's code
panel has a toggle in its top-left corner): a page in a browser context of its
own, so nothing done there is recorded, showing the same panel through the same
script.

Play replays the script in a new tab of the same context, so a saved sign-in
carries over. At a branch it takes the path the ``.sub`` value of the branch's
variable names, else the path recorded last. Load reads code saved earlier
(either target) back into the script and plays it.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import shutil
import subprocess  # nosec B404 -- only to hand text to the platform's clipboard tool
import sys
import time
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any

from .model import Node, Rendered, Script, apply_locator, choose_path, parse_code, render, suggest_branch_name, suggest_variable

logger = logging.getLogger(__name__)

RECORDER_JS = Path(__file__).with_name("recorder.js")

#: An address-bar navigation is one that no recorded action led to in this many seconds.
NAVIGATION_QUIET_SECONDS = 8.0
#: How long each replayed action may take.
REPLAY_TIMEOUT_MS = 15000
#: Toolbar commands that change the script, refused while a replay runs.
EDIT_COMMANDS = {"load", "record", "undo", "redo", "branch", "nextpath", "endbranch", "optional", "step", "variable", "comment", "editline"}
#: What the toolbar's pick mode is for: copying an element's HTML, or noting a dropdown's options.
PICK_PURPOSES = ("html", "options")

# Names an iframe element in its parent frame; runs in the parent.
_FRAME_SELECTOR_JS = """(el) => {
  if (window.__ppFrameSelector) return window.__ppFrameSelector(el);
  if (el.id) return '#' + CSS.escape(el.id);
  if (el.name) return `${el.localName}[name='${CSS.escape(el.name)}']`;
  return el.localName;
}"""

#: Where the separate code window's page is served from (routed, never fetched).
CODE_WINDOW_URL = "http://paperpush-codegen.invalid/code"
_CODE_WINDOW_HTML = '<!doctype html><meta charset="utf-8"><title>paperpush codegen: code</title><body style="margin:0;background:#0d1117">'

_HOST_TAG = re.compile(r"<paperpush-codegen\b[^>]*>\s*</paperpush-codegen>")


def copy_to_clipboard(text: str) -> bool:
    """Put ``text`` on the system clipboard with the platform's own tool; False when there is none."""
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    elif sys.platform.startswith("win"):
        candidates = [["clip"]]
    else:
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for cmd in candidates:
        exe = shutil.which(cmd[0])
        if not exe:
            continue
        try:
            subprocess.run([exe, *cmd[1:]], input=text.encode("utf-8"), check=True, timeout=5)  # nosec B603
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("clipboard tool %s failed: %s", cmd[0], exc)
    return False


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return text[0] if text else type(exc).__name__


class CodegenSession:
    """One recording session: a browser, the pages it opened, and the script being recorded."""

    def __init__(
        self,
        *,
        url: str = "about:blank",
        target: str = "paperpush",
        output: str | Path | None = None,
        sub_values: dict[str, str] | None = None,
        fields: list[str] | None = None,
        file_fields: list[str] | None = None,
        field_options: dict[str, list[str]] | None = None,
        storage_state: str | Path | None = None,
        header: str = "",
        clipboard=copy_to_clipboard,
    ) -> None:
        self.url = url
        self.target = target
        self.output = Path(output) if output else None
        self.sub_values = dict(sub_values or {})
        self.fields = list(fields or self.sub_values.keys())
        self.file_fields = list(file_fields or [])
        self.field_options = dict(field_options or {})
        self.storage_state = str(storage_state) if storage_state else None
        self.header = header or f"Recorded with `paperpush codegen` on {_dt.date.today().isoformat()}."
        self.clipboard = clipboard

        self.script = Script()
        self.queue: deque[tuple] = deque()
        self.pages: dict[Any, str] = {}
        self.replay_pages: set[Any] = set()
        self.cdp: dict[Any, Any] = {}
        self.recording = True
        self.playing = False
        self.paused = False
        self.stop_requested = False
        self.play_requested = False
        self.picking: str | None = None
        self.selected: int | None = None
        self.message = ""
        self.save_path = str(self.output) if self.output else "paperpush_codegen.py"
        #: Values declared by loaded code (the python target's ``values`` dict); the .sub's win.
        self.loaded_values: dict[str, Any] = {}
        self.last_event: dict[str, float] = {}
        self.context = None
        self.code_window = None  # the separate code window's page, while it is open
        self._dirty = True

    # ------------------------------------------------------------------ run
    def run(self, *, headless: bool = False) -> Rendered:
        """Open the browser and record until every window is closed (or Ctrl+C). Returns the code."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            try:
                kwargs = {"storage_state": self.storage_state} if self.storage_state else {}
                self.context = context = browser.new_context(no_viewport=not headless, **kwargs)
                self.attach(context)
                page = context.new_page()
                self._register_page(page, "page")
                if self.url and self.url != "about:blank":
                    self.script.add(Node("goto", value=self.url, t=time.monotonic()))
                    self.last_event["page"] = time.monotonic()
                    try:
                        page.goto(self.url)
                    except Exception as exc:  # the author can still navigate by hand
                        self.message = f"Could not open {self.url}: {_first_line(exc)}"
                self.loop()
            except KeyboardInterrupt:
                pass
            finally:
                self.flush_output()
                try:
                    browser.close()
                except Exception:  # already gone
                    pass
        return self.rendered()

    def attach(self, context) -> None:
        """Install the bindings, the recorder script and the page listener on ``context``."""
        context.expose_binding("__ppRecord", lambda source, evt: self.queue.append(("record", source.get("page"), source.get("frame"), evt)))
        context.expose_binding("__ppCommand", lambda source, cmd: self.queue.append(("cmd", source.get("page"), source.get("frame"), cmd)))
        context.add_init_script(script=RECORDER_JS.read_text(encoding="utf-8"))
        context.on("page", lambda p: self.queue.append(("page", p, None, self.playing)))

    def loop(self) -> None:
        """Process events until no page is left open."""
        while True:
            self.drain()
            if self.play_requested:
                self.play_requested = False
                self.replay()
                continue
            if not self.pump(50):
                return

    def pump(self, ms: int) -> bool:
        """Let Playwright deliver events for ``ms``; False once every page is closed."""
        for page in list(self.context.pages):
            if page.is_closed():
                continue
            try:
                page.wait_for_timeout(ms)
                return True
            except Exception:  # that page closed mid-wait; try another
                continue
        return False

    # -------------------------------------------------------------- events
    def drain(self) -> None:
        while self.queue:
            item = self.queue.popleft()
            try:
                self.handle(item)
            except Exception as exc:
                logger.debug("codegen: handling %s failed", item[0], exc_info=True)
                self.message = f"Error: {_first_line(exc)}"
                self._dirty = True
        if self._dirty:
            self._dirty = False
            self.flush_output()
            self.push_state()

    def handle(self, item: tuple) -> None:
        kind, page, frame, payload = item
        if kind == "record":
            self.on_record(page, frame, payload)
        elif kind == "cmd":
            self.on_command(page, frame, payload)
        elif kind == "page":
            self.on_new_page(page, during_replay=payload)
        elif kind == "nav":
            self.on_navigation(page, frame)
        elif kind == "dialog":
            self.on_dialog(page, payload)
        elif kind == "codeclosed":
            if page is self.code_window:  # the author closed the window itself
                self.close_code_window()

    def _register_page(self, page, name: str) -> None:
        self.pages[page] = name
        page.on("framenavigated", lambda f: self.queue.append(("nav", page, f, None)))
        page.on("dialog", lambda d: self._accept_dialog(page, d))

    def _accept_dialog(self, page, dialog) -> None:
        # Accept so the wizard can go on (a listener stops Playwright from dismissing it),
        # and note it on the action that raised it.
        info = {"type": dialog.type, "message": dialog.message}
        try:
            dialog.accept()
        except Exception:  # already handled
            pass
        self.queue.append(("dialog", page, None, info))

    def on_dialog(self, page, info: dict) -> None:
        if self.playing or not self.recording or page not in self.pages:
            return
        last = self.script.last_action(self.pages[page])
        if last is not None:
            last.dialog = "accept"
        self.message = f"Accepted a {info['type']} dialog: {info['message'][:80]!r}"
        self._dirty = True

    def on_new_page(self, page, *, during_replay: bool) -> None:
        if during_replay:
            self.replay_pages.add(page)
        if during_replay or page in self.replay_pages or page in self.pages:
            return
        name = f"page{len(self.pages)}"
        self._register_page(page, name)
        self._dirty = True
        if not self.recording:
            return
        try:
            opener = page.opener()
        except Exception:
            opener = None
        if opener is not None and opener in self.pages and self.script.mark_popup(self.pages[opener], name):
            self.message = f"The last action opened a new tab: {name}"
        else:
            self.script.add(Node("new_page", page=name, t=time.monotonic()))
            self.last_event[name] = 0.0  # its first navigation is a goto

    def on_navigation(self, page, frame) -> None:
        if self.playing or not self.recording or frame is not page.main_frame:
            return
        name = self.pages.get(page)
        if name is None:
            return
        url = frame.url
        if not url or url == "about:blank" or url.startswith(("chrome-error:", "data:", "chrome:")):
            return
        now = time.monotonic()
        if now - self.last_event.get(name, 0.0) < NAVIGATION_QUIET_SECONDS:
            return
        # Nothing the author did here led to this page: typed URL, bookmark, back/forward.
        self.script.add(Node("goto", page=name, value=url, t=now))
        self.last_event[name] = now
        self._dirty = True

    def frame_chain(self, frame) -> list:
        """The ``frame_locator`` segments from the page down to ``frame``."""
        chain: list = []
        current = frame
        while current is not None and current.parent_frame is not None:
            try:
                selector = current.frame_element().evaluate(_FRAME_SELECTOR_JS)
            except Exception:
                selector = f"iframe[name='{current.name}']" if current.name else "iframe"
            chain.insert(0, ["frame_locator", [selector], {}])
            current = current.parent_frame
        return chain

    def on_record(self, page, frame, evt: dict) -> None:
        if self.playing or not self.recording:
            return
        name = self.pages.get(page)
        if name is None:
            return
        kind = evt.get("kind")
        if kind not in ("click", "fill", "press", "check", "uncheck", "select", "upload", "chooser_upload"):
            return
        now = time.monotonic()
        self.last_event[name] = now
        locator = evt.get("locator")
        if locator is not None:
            locator = self.frame_chain(frame) + locator
        label = str(evt.get("label") or "").strip() or None
        node = Node(kind, page=name, locator=locator, value=evt.get("value"), t=now, sensitive=bool(evt.get("sensitive")), label=label)
        if kind == "click":
            node.value = int(evt.get("detail") or 1)
            node.button = evt.get("button") or "left"
            node.modifiers = list(evt.get("modifiers") or [])
        if kind in ("upload", "chooser_upload"):
            node.value = self.file_paths(page, frame, str(evt.get("token") or ""), list(evt.get("value") or []))
        if node.sensitive:
            node.var = "password"

        held = self.script.add(node)
        if held.has_value and not held.var and not held.var_locked:
            held.var = suggest_variable(held.value, held.kind, self.sub_values, self.file_fields)
        self._name_open_branch()
        self.message = ""
        self._dirty = True

    def _name_open_branch(self) -> None:
        """Name the open branch after the venue field whose options its paths are, until the author names it."""
        branch = self.script.open_branch()
        if branch is None or self.script.nodes[branch].var_locked:
            return
        keys = [self.script.nodes[i].option for i in self.script.branch_paths(branch)]
        guess = suggest_branch_name(keys, self.field_options, self.sub_values)
        taken = set(self.script.branch_names()) - {self.script.nodes[branch].value}
        if guess and guess not in taken and guess != self.script.nodes[branch].value:
            self.script.rename_branch(branch, guess, locked=False)

    def file_paths(self, page, frame, token: str, names: list[str]) -> list[str]:
        """The full paths of the files just chosen (Chrome only exposes names to the page).

        Asks the DevTools protocol for each ``File``'s path. Falls back to the
        bare names, which the author then points at real files (or binds to a
        ``.sub`` field).
        """
        if not re.fullmatch(r"[0-9]+-[0-9]+", token) or not names:
            return names
        try:
            cdp = self.cdp.get(page)
            if cdp is None:
                cdp = self.cdp[page] = self.context.new_cdp_session(page)
            frame_id = self._cdp_frame_id(cdp, page, frame)
            world = cdp.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "paperpush-codegen"})
            paths = []
            for i in range(len(names)):
                expr = f"document.querySelector('[data-pp-upload=\"{token}\"]').files[{i}]"
                res = cdp.send("Runtime.evaluate", {"expression": expr, "contextId": world["executionContextId"]})
                info = cdp.send("DOM.getFileInfo", {"objectId": res["result"]["objectId"]})
                paths.append(info["path"])
            return paths
        except Exception as exc:
            logger.debug("codegen: could not read upload paths: %s", exc)
            self.message = "Recorded the file names only; point them at the real files (or a .sub field with Variable)."
            return names

    @staticmethod
    def _cdp_frame_id(cdp, page, frame) -> str:
        tree = cdp.send("Page.getFrameTree")["frameTree"]
        if frame is page.main_frame:
            return tree["frame"]["id"]
        stack = [tree]
        while stack:
            node = stack.pop()
            info = node["frame"]
            if info.get("url") == frame.url and (info.get("name") or "") == (frame.name or ""):
                return info["id"]
            stack.extend(node.get("childFrames", []))
        raise LookupError("frame not found in the DevTools frame tree")

    # ------------------------------------------------------------ commands
    def on_command(self, page, frame, cmd: dict) -> None:
        name = cmd.get("cmd")
        self._dirty = True
        if name == "hello":
            return  # the state push after draining reaches the new document
        if self.playing and name in EDIT_COMMANDS:
            self.message = "Stop the replay first."
            return
        if name == "record":
            self.recording = not self.recording
            self.message = "Recording." if self.recording else "Recording stopped; your actions are not recorded."
        elif name == "play":
            if self.playing and self.paused:
                self.paused = False
            elif not self.playing:
                self.play_requested = True
        elif name == "pause":
            if self.playing:
                self.paused = True
        elif name == "stop":
            self.stop_requested = True
            self.paused = False
        elif name == "undo":
            self.message = "Undone." if self.script.undo() else "Nothing to undo."
            self.selected = None
        elif name == "redo":
            self.message = "Redone." if self.script.redo() else "Nothing to redo."
            self.selected = None
        elif name == "branch":
            try:
                branch = self.script.enter_branch()
            except ValueError as exc:
                self.message = f"{str(exc)[0].upper()}{str(exc)[1:]}."
                return
            stack = self.script.branch_stack()
            if len(stack) > 1:
                outer = self.script.nodes[stack[-2]].value
                self.message = f"Branch {branch} (level {len(stack)}), nested in {outer}: record its first option's actions, press Next before each further option, and End to go back to {outer}."
            else:
                self.message = f"Branch {branch}: record the first option's actions, press Next before each further option, and End before the action that follows the choice."
        elif name == "nextpath":
            branch = self.script.open_branch()
            if branch is None:
                self.message = "Not in a branch."
            elif self.script.next_path():
                paths = len(self.script.branch_paths(branch))
                self.message = f"Path {paths} done: what you record now is path {paths + 1}."
            else:
                self.message = "This path has no actions yet: record the option's actions first."
        elif name == "endbranch":
            branch = self.script.open_branch()
            if self.script.exit_branch():
                paths = len(self.script.branch_paths(branch))
                outer = self.script.open_branch()
                back = f"; back in {self.script.nodes[outer].value}" if outer is not None else ""
                self.message = f"Ended {self.script.nodes[branch].value} with {paths} path{'s' if paths != 1 else ''}{back}."
            else:
                self.message = "Not in a branch."
        elif name == "optional":
            if self.script.toggle_optional():
                self.message = "Optional section started: what you record now is marked optional. Press Optional again to end it."
            else:
                self.message = "Optional section ended."
        elif name == "step":
            label = str(cmd.get("label") or "").strip()
            if label:
                self.script.add_step(label)
        elif name == "variable":
            self.on_variable(cmd)
        elif name == "comment":
            self.on_comment(cmd)
        elif name == "editline":
            try:
                self.message = self.script.edit_line(str(cmd.get("kind")), int(cmd.get("index")), str(cmd.get("text") or ""))
            except (TypeError, ValueError, IndexError) as exc:
                self.message = str(exc)
        elif name == "select":
            index = cmd.get("index")
            if isinstance(index, int) and 0 <= index < len(self.script.nodes):
                self.selected = index
                self.message = f"Selected line: {self.script.nodes[index].summary()}"
        elif name == "copy":
            self.copy(self.rendered().code, "the code")
        elif name == "copyhtml":
            self.copy(self.page_html(page), "the page HTML")
        elif name == "save":
            self.save(str(cmd.get("path") or ""))
        elif name == "load":
            self.load(str(cmd.get("path") or ""))
        elif name == "codewindow":
            if self.code_window is None:
                self.open_code_window()
            else:
                self.close_code_window()
                self.message = "The code is back in this window."
        elif name == "focuscode":
            if self.code_window is not None:
                try:
                    self.code_window.bring_to_front()
                except Exception:  # closed meanwhile
                    self.close_code_window()
        elif name == "pick":
            self.picking = cmd.get("purpose") if cmd.get("purpose") in PICK_PURPOSES else "html"
            self.message = ""
        elif name == "cancelpick":
            self.picking = None
        elif name == "picked":
            self.on_picked(cmd)

    def on_picked(self, cmd: dict) -> None:
        purpose, self.picking = self.picking, None
        if purpose == "options":
            if self.playing:
                self.message = "Stop the replay first."
                return
            found = cmd.get("options") or {}
            options = [str(o) for o in found.get("options") or [] if str(o).strip()]
            if not options and found.get("unloaded"):
                what = f"{found.get('label')!r}" if found.get("label") else "This dropdown"
                self.message = f"{what} has no options loaded yet: open it (or type in its search box) so its choices show, then pick it again."
                return
            if not options:
                self.message = "That element has no options to list: pick a dropdown, a list, or a radio button (open a custom dropdown first)."
                return
            node = self.script.add_comment(str(found.get("label") or ""), options)
            self.message = f"Noted {len(options)} options of {node.label!r}." if node.label else f"Noted {len(options)} options."
        else:
            self.copy(str(cmd.get("html") or ""), "the element's HTML")

    # --------------------------------------------------------- code window
    def open_code_window(self) -> None:
        """Show the code panel in a window of its own (a separate browser context, so nothing there is recorded)."""
        try:
            context = self.context.browser.new_context(no_viewport=True)
        except Exception as exc:
            self.message = f"Could not open the code window: {_first_line(exc)}"
            return
        try:
            context.expose_binding("__ppCommand", lambda source, cmd: self.queue.append(("cmd", source.get("page"), source.get("frame"), cmd)))
            context.add_init_script(script="window.__ppCodeWindow = true;")
            context.add_init_script(script=RECORDER_JS.read_text(encoding="utf-8"))
            context.route(CODE_WINDOW_URL, lambda route: route.fulfill(status=200, content_type="text/html", body=_CODE_WINDOW_HTML))
            page = context.new_page()
            self._size_window(context, page, 720, 900)
            page.on("close", lambda p: self.queue.append(("codeclosed", p, None, None)))
            page.goto(CODE_WINDOW_URL)
        except Exception as exc:
            self.message = f"Could not open the code window: {_first_line(exc)}"
            with suppress(Exception):
                context.close()
            return
        self.code_window = page
        self.message = "The code is in its own window; press ⧈ Embed there (or close it) to bring it back."

    @staticmethod
    def _size_window(context, page, width: int, height: int) -> None:
        with suppress(Exception):
            cdp = context.new_cdp_session(page)
            window = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send("Browser.setWindowBounds", {"windowId": window, "bounds": {"width": width, "height": height}})
            cdp.detach()

    def close_code_window(self) -> None:
        """Close the code window (if open); the code shows in the toolbar again."""
        page, self.code_window = self.code_window, None
        self._dirty = True
        if page is not None:
            with suppress(Exception):
                page.context.close()

    def save(self, path: str) -> None:
        """Write the code to ``path`` (relative to where codegen was started); it becomes the next default."""
        path = path.strip()
        if not path:
            self.message = "Give a file name to save the code to."
            return
        target = Path(path).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.rendered().code, encoding="utf-8")
        except OSError as exc:
            self.message = f"Could not save to {target}: {exc.strerror or exc}"
            return
        self.save_path = path
        self.message = f"Saved the code to {target.resolve()}."

    def load(self, path: str, *, play: bool = True) -> bool:
        """Replace the script with the code saved at ``path`` (one undo step) and, with ``play``, replay it."""
        path = path.strip()
        if not path:
            self.message = "Give the path of the code to load."
            return False
        source = Path(path).expanduser()
        try:
            loaded = parse_code(source.read_text(encoding="utf-8"))
        except OSError as exc:
            self.message = f"Could not read {source}: {exc.strerror or exc}"
            return False
        except ValueError as exc:
            self.message = f"Could not load {source.name}: {exc}"
            return False
        self.script.replace(loaded.nodes)
        self.loaded_values = loaded.values
        self.selected = None
        self.save_path = path
        missing = sorted({n.var for n in loaded.nodes if n.var and n.value in (None, "", []) and not self.sub_values.get(n.var)})
        actions = sum(1 for n in loaded.nodes if n.kind not in ("step", "branch", "next_path", "end_branch", "comment", "optional", "end_optional"))
        note = f" No value for {', '.join(missing)} (give a .sub with --sub)." if missing else ""
        self.message = f"Loaded {actions} action{'s' if actions != 1 else ''} from {source.name}.{note}"
        if play:
            self.play_requested = True
        return True

    def variable_index(self, requested: Any) -> int | None:
        """The line the Variable button acts on: the selected one, else the latest that has something to name."""
        if isinstance(requested, int) and 0 <= requested < len(self.script.nodes):
            return requested
        if self.selected is not None and self.selected < len(self.script.nodes):
            return self.selected
        return self.script.default_selection()

    def on_variable(self, cmd: dict) -> None:
        index = self.variable_index(cmd.get("index"))
        if index is None:
            self.message = "Nothing to name yet: record a typed value, a choice, an upload, or a Branch first."
            return
        if cmd.get("name") is None:  # the toolbar asks only to explain why it cannot open the prompt
            self.message = f"Line '{self.script.nodes[index].summary()}' has no value to replace. Select a line in the code (click it), or record a choice between options as a Branch."
            return
        try:
            changed = self.script.set_variable(index, str(cmd.get("name")))
        except ValueError as exc:
            self.message = str(exc)
            return
        target = self.script.variable_target(index)
        if target and target[0] == "branch":
            self.message = f"Branch renamed to {self.script.nodes[target[1]].value}."
            return
        var = self.script.nodes[index].var
        more = f" (and {len(changed) - 1} more)" if len(changed) > 1 else ""
        self.message = f"Line now uses {var}{more}." if var else "Put the recorded value back."

    def note_index(self, requested: Any = None) -> int | None:
        """The action the Comment button annotates: the selected line's (or the one before it), else the latest."""
        if not (isinstance(requested, int) and 0 <= requested < len(self.script.nodes)):
            requested = self.selected if self.selected is not None and self.selected < len(self.script.nodes) else None
        return self.script.note_target(requested)

    def on_comment(self, cmd: dict) -> None:
        index = self.note_index(cmd.get("index"))
        if index is None:
            self.message = "Nothing to comment on yet: record an action first."
            return
        try:
            self.script.set_note(index, str(cmd.get("text") or ""))
        except ValueError as exc:
            self.message = str(exc)
            return
        node = self.script.nodes[index]
        self.message = f"Comment added to '{node.summary()}'." if node.note else f"Comment removed from '{node.summary()}'."

    def copy(self, text: str, what: str) -> None:
        if self.clipboard(text):
            self.message = f"Copied {what} ({len(text):,} characters)."
            return
        if self.output is not None:
            self.message = f"No clipboard tool found; the code is in {self.output}."
        else:
            self.message = "No clipboard tool found (install wl-copy, xclip or xsel)."
        print(text)

    def page_html(self, page) -> str:
        parts = []
        for frame in page.frames:
            try:
                html = _HOST_TAG.sub("", frame.content())
            except Exception as exc:
                html = f"<!-- could not read: {_first_line(exc)} -->"
            label = "main frame" if frame is page.main_frame else f"frame {frame.name or '(unnamed)'}"
            parts.append(f"<!-- paperpush codegen: {label} {frame.url} -->\n{html}")
        return "\n\n".join(parts)

    # -------------------------------------------------------------- replay
    def replay_plan(self) -> list[int]:
        """The node indices a replay runs: everything outside branches, and one path per branch (nested ones too)."""
        plan: list[int] = []
        # One entry per open branch: [the path the replay takes, the path being walked].
        stack: list[list[int | None]] = []

        def on_chosen_paths() -> bool:
            return all(chosen == current for chosen, current in stack)

        for i, node in enumerate(self.script.nodes):
            if stack and node.option is not None:
                stack[-1][1] = i  # this node starts a path of the innermost branch
            if node.kind == "branch":
                value = self.sub_values.get(node.value) or self.loaded_values.get(node.value)
                chosen = choose_path(self.script, i, value) if on_chosen_paths() else None
                stack.append([chosen, None])
                continue
            if node.kind == "end_branch":
                if stack:
                    stack.pop()
                continue
            if node.kind == "next_path":
                if stack:
                    stack[-1][1] = None
                continue
            if node.kind in ("step", "comment", "optional", "end_optional"):
                continue
            if on_chosen_paths():
                plan.append(i)
        return plan

    def replay(self) -> None:
        """Run the script in a new tab, honouring Pause / Resume / Stop from the toolbar."""
        nodes = list(self.script.nodes)
        plan = self.replay_plan()
        self.playing, self.paused, self.stop_requested = True, False, False
        page = self.context.new_page()
        self.replay_pages.add(page)
        page.set_default_timeout(REPLAY_TIMEOUT_MS)
        pages = {"page": page}
        self.message = f"{self.message} Playing in a new tab..." if self.message.startswith("Loaded ") else "Playing in a new tab..."
        self._dirty = True
        current = None
        try:
            for i in plan:
                if not self._wait_while_paused():
                    self.message = "Replay stopped."
                    break
                current = node = nodes[i]
                self.selected = i
                self.message = f"Playing line: {node.summary()}"
                self._dirty = True
                self.drain()
                self._replay_node(node, pages)
            else:
                self.message = "Replay finished."
        except Exception as exc:
            what = current.summary() if current else "start"
            self.message = f"Replay failed at {what}: {_first_line(exc)}"
        finally:
            self.playing = self.paused = False
            self.selected = None
            self._dirty = True
            self.drain()

    def _wait_while_paused(self) -> bool:
        self.drain()
        while self.paused and not self.stop_requested:
            if not self.pump(100):
                return False
            self.drain()
        return not self.stop_requested

    def replay_value(self, node: Node) -> Any:
        """The value to replay: the bound ``.sub`` field's value when there is one, else what was recorded."""
        raw = self.sub_values.get(node.var or "") if node.var else None
        if node.kind in ("upload", "chooser_upload"):
            if raw:
                paths = [line.split("|", 1)[0].strip() for line in raw.splitlines() if line.strip()]
                paths = [p for p in paths if os.path.exists(p)]
                if paths:
                    return paths
            return list(node.value or [])
        if raw:
            return [raw] if node.kind == "select" else raw
        return node.value

    def _replay_node(self, node: Node, pages: dict) -> None:
        if node.kind == "new_page":
            pages[node.page] = self.context.new_page()
            self.replay_pages.add(pages[node.page])
            return
        page = pages.get(node.page)
        if page is None:
            raise RuntimeError(f"{node.page} was never opened in this replay")

        def act() -> None:
            value = self.replay_value(node)
            if node.kind == "goto":
                page.goto(value)
                return
            if node.kind == "press" and not node.locator:
                page.keyboard.press(node.value)
                return
            loc = apply_locator(page, node.locator or [])
            if node.kind in ("click", "dblclick"):
                opts: dict = {}
                if node.button and node.button != "left":
                    opts["button"] = node.button
                if node.modifiers:
                    opts["modifiers"] = node.modifiers
                getattr(loc, node.kind)(**opts)
            elif node.kind == "fill":
                loc.fill(value)
            elif node.kind == "press":
                loc.press(node.value)
            elif node.kind in ("check", "uncheck"):
                getattr(loc, node.kind)()
            elif node.kind == "select":
                loc.select_option(label=value)
            elif node.kind == "upload":
                loc.set_input_files(value)
            elif node.kind == "chooser_upload":
                with page.expect_file_chooser() as fc_info:
                    loc.click()
                fc_info.value.set_files(value)

        if node.dialog:
            page.once("dialog", lambda d: d.accept())
        if node.popup:
            with page.expect_popup() as info:
                act()
            pages[node.popup] = info.value
            self.replay_pages.add(info.value)
            info.value.set_default_timeout(REPLAY_TIMEOUT_MS)
        else:
            act()

    # --------------------------------------------------------------- state
    def rendered(self) -> Rendered:
        return render(self.script, self.target, file_fields=self.file_fields, header=self.header)

    def flush_output(self) -> None:
        if self.output is None:
            return
        tmp = self.output.with_name(self.output.name + ".tmp")
        tmp.write_text(self.rendered().code, encoding="utf-8")
        os.replace(tmp, self.output)

    def state(self) -> dict:
        rendered = self.rendered()
        selected = self.selected if self.selected is not None and self.selected < len(self.script.nodes) else None
        var_index = self.variable_index(None)
        var_target = self.script.variable_target(var_index)
        variable = None
        if var_target is not None:
            kind, i = var_target
            node = self.script.nodes[i]
            if kind == "branch":
                variable = {"index": var_index, "kind": "branch", "current": node.value, "what": f"branch {node.value}"}
            else:
                suggest = None if node.sensitive else suggest_variable(node.value, node.kind, self.sub_values, self.file_fields)
                variable = {"index": var_index, "kind": "value", "current": node.var or suggest or "", "what": node.summary()}
        return {
            "recording": self.recording,
            "playing": self.playing,
            "paused": self.paused,
            "picking": self.picking,
            "depth": self.script.depth,
            "pathOpen": self.script.path_open(),
            "optional": self.script.optional_open(),
            "branch": self.script.nodes[self.script.open_branch()].value if self.script.depth else "",
            "branches": [self.script.nodes[i].value for i in self.script.branch_stack()],
            "can_undo": self.script.can_undo,
            "can_redo": self.script.can_redo,
            "lines": rendered.lines,
            "owners": rendered.owners,
            "edits": rendered.edits,
            "selected": selected,
            "variable": variable,
            "variableIndex": var_index,
            "comment": self._comment_state(),
            "message": self.message,
            "fields": self.fields,
            "savePath": self.save_path,
            "codeWindow": self.code_window is not None,
        }

    def _comment_state(self) -> dict | None:
        index = self.note_index()
        if index is None:
            return None
        node = self.script.nodes[index]
        return {"index": index, "current": node.note or "", "what": node.summary()}

    def push_state(self) -> None:
        full = self.state()
        brief = {k: full[k] for k in ("recording", "playing", "paused", "picking")}
        for page in list(self.context.pages if self.context else []):
            if page.is_closed():
                continue
            for frame in page.frames:
                payload = full if frame is page.main_frame else brief
                if page in self.replay_pages:
                    payload = {**payload, "hidden": True}
                try:
                    frame.evaluate("s => window.__ppSetState && window.__ppSetState(s)", payload)
                except Exception:  # navigating or detached; it says hello when it loads
                    pass
        if self.code_window is not None and not self.code_window.is_closed():
            try:
                self.code_window.evaluate("s => window.__ppSetState && window.__ppSetState(s)", full)
            except Exception:  # still loading; it says hello when it loads
                pass
