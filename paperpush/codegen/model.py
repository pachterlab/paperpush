"""The recorded script: the actions, the edits made to it, and the code it renders to.

Everything here is plain data, with no browser involved, so the recorder's
editing (undo/redo, branches, variables, merging a click with the file chooser
it opened) can be tested on its own. :mod:`paperpush.codegen.session` feeds it
events from the page and turns its nodes back into Playwright calls on replay.

A *locator* is a list of segments ``[method, args, kwargs]``, the chain of calls
off the page that finds an element, for example
``[["frame_locator", ["#editor"], {}], ["get_by_role", ["button"], {"name": "Next"}]]``.
The same list renders to code and is applied to a live page on replay, so the
two can never disagree.
"""

from __future__ import annotations

import copy
import json
import keyword
from dataclasses import dataclass, field
from typing import Any, Iterable

#: Node kinds that carry a value the author can swap for a variable.
VALUE_KINDS = ("goto", "fill", "select", "upload", "chooser_upload")
#: Node kinds that act on an element.
ELEMENT_KINDS = ("click", "dblclick", "fill", "press", "check", "uncheck", "select", "upload", "chooser_upload")

TARGET_PAPERPUSH = "paperpush"
TARGET_PYTHON = "python"
TARGETS = (TARGET_PAPERPUSH, TARGET_PYTHON)

#: Seconds within which a file chooser is taken to belong to the click before it.
CHOOSER_WINDOW_SECONDS = 300.0

# Names the rendered code already uses, so a variable may not take them.
_RESERVED = {"page", "context", "browser", "playwright", "values", "step", "fc_info", "sync_playwright", "guard"}


def py_str(value: str) -> str:
    """A double-quoted Python literal for ``value`` (JSON escapes are valid Python)."""
    return json.dumps(value, ensure_ascii=False)


def py_value(value: Any) -> str:
    """A Python literal for a str, number, bool, None, or list of those."""
    if isinstance(value, str):
        return py_str(value)
    if isinstance(value, bool) or value is None:
        return repr(value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(py_value(v) for v in value) + "]"
    return py_str(str(value))


def render_segment(segment: list) -> str:
    """One locator segment as code: ``get_by_role("button", name="Next")``."""
    method, args, kwargs = segment[0], list(segment[1] or []), dict(segment[2] or {})
    if method == "nth" and args == [0]:
        return "first"
    parts = [py_value(a) for a in args] + [f"{k}={py_value(v)}" for k, v in kwargs.items()]
    return f"{method}({', '.join(parts)})"


def render_locator(locator: list, page: str = "page") -> str:
    """A locator chain as code rooted at ``page``."""
    return ".".join([page] + [render_segment(s) for s in locator])


def apply_locator(root: Any, locator: list) -> Any:
    """Build the live Playwright locator that ``locator`` describes, off ``root``."""
    current = root
    for method, args, kwargs in locator:
        if method == "nth" and list(args) == [0]:
            current = current.first
            continue
        current = getattr(current, method)(*(args or []), **(kwargs or {}))
    return current


def valid_variable_name(name: str) -> bool:
    """Whether ``name`` can be used as a variable in the rendered code."""
    return name.isidentifier() and not keyword.iskeyword(name) and name not in _RESERVED and not name.startswith("page")


@dataclass
class Node:
    """One line (or block) of the recorded script.

    ``kind`` is an action (``goto``, ``click``, ``dblclick``, ``fill``, ``press``,
    ``check``, ``uncheck``, ``select``, ``upload``, ``chooser_upload``,
    ``new_page``) or a structural marker (``step``, ``branch``, ``end_branch``).
    ``value`` holds the URL, the typed text, the option labels, the uploaded
    paths, the key, the step label, or the branch condition. ``var`` names the
    variable that replaces ``value`` in the code.
    """

    kind: str
    page: str = "page"
    locator: list | None = None
    value: Any = None
    var: str | None = None
    sensitive: bool = False
    popup: str | None = None
    button: str | None = None
    modifiers: list[str] = field(default_factory=list)
    t: float = 0.0

    @property
    def has_value(self) -> bool:
        return self.kind in VALUE_KINDS

    def summary(self) -> str:
        """A short description for messages ("fill title")."""
        if self.kind in ("step", "branch"):
            return f"{self.kind} {self.value!r}"
        if self.var:
            return f"{self.kind} {self.var}"
        if self.sensitive:
            return f"{self.kind} (hidden value)"
        if self.value not in (None, "", []):
            text = self.value if isinstance(self.value, str) else ", ".join(map(str, self.value))
            return f"{self.kind} {text[:40]!r}"
        return self.kind


class Script:
    """The recorded nodes plus undo/redo history.

    Every edit that changes the script takes a snapshot first, so undo restores
    the script exactly as it was. Typing into the same field again only updates
    the existing ``fill`` (one undo step per field, not per keystroke).
    """

    def __init__(self) -> None:
        self.nodes: list[Node] = []
        self._undo: list[list[Node]] = []
        self._redo: list[list[Node]] = []

    # -- history -------------------------------------------------------------
    def _checkpoint(self) -> None:
        self._undo.append(copy.deepcopy(self.nodes))
        self._redo.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(copy.deepcopy(self.nodes))
        self.nodes = self._undo.pop()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(copy.deepcopy(self.nodes))
        self.nodes = self._redo.pop()
        return True

    # -- structure -----------------------------------------------------------
    @property
    def depth(self) -> int:
        """How many branches are open at the end of the script."""
        depth = 0
        for node in self.nodes:
            if node.kind == "branch":
                depth += 1
            elif node.kind == "end_branch" and depth:
                depth -= 1
        return depth

    def last_action(self, page: str | None = None) -> Node | None:
        """The latest node that is an action (not a marker), optionally on ``page``."""
        for node in reversed(self.nodes):
            if node.kind in ("step", "branch", "end_branch"):
                return None
            if page is None or node.page == page:
                return node
        return None

    def enter_branch(self, condition: str) -> None:
        condition = condition.strip()
        compile(condition, "<branch condition>", "eval")  # raises SyntaxError on a bad condition
        self._checkpoint()
        self.nodes.append(Node("branch", value=condition))

    def exit_branch(self) -> bool:
        if not self.depth:
            return False
        self._checkpoint()
        self.nodes.append(Node("end_branch"))
        return True

    def add_step(self, label: str) -> None:
        self._checkpoint()
        self.nodes.append(Node("step", value=label.strip()))

    # -- recording -----------------------------------------------------------
    def add(self, node: Node) -> Node:
        """Append a recorded action, merging it into the previous one where that is what happened.

        * More typing into the field just filled updates that ``fill``.
        * A second click with ``detail >= 2`` on the element just clicked turns it into ``dblclick``
          (pass the click count in ``node.value`` for clicks).
        * An upload that a button opened (``chooser_upload``) takes over the click before it.
        * Returns the node that now holds the action.
        """
        last = self.nodes[-1] if self.nodes else None
        same_target = last is not None and last.page == node.page and last.locator == node.locator

        if node.kind == "fill" and same_target and last.kind == "fill":
            last.value = node.value
            last.t = node.t
            return last

        if node.kind == "click":
            clicks = node.value or 1
            node.value = None
            if clicks >= 2 and same_target and last.kind in ("click", "dblclick"):
                self._checkpoint()
                last = self.nodes[-1]
                last.kind = "dblclick"
                last.t = node.t
                return last

        if node.kind == "chooser_upload":
            prev = self.last_action(node.page)
            if prev is not None and prev is last and prev.kind == "click" and node.t - prev.t <= CHOOSER_WINDOW_SECONDS:
                self._checkpoint()
                last = self.nodes[-1]
                last.kind = "chooser_upload"
                last.value = node.value
                last.var = node.var
                last.t = node.t
                return last
            # No click to attach the chooser to: set the files on the input itself.
            node.kind = "upload"
            node.locator = node.locator or []

        if node.kind == "goto":
            # A redirect right after a navigation is the same navigation.
            if last is not None and last.kind == "goto" and last.page == node.page and node.t - last.t < 5:
                return last

        self._checkpoint()
        self.nodes.append(node)
        return node

    def mark_popup(self, opener: str, name: str) -> Node | None:
        """Record that the latest action on ``opener`` opened the page ``name``."""
        last = self.last_action(opener)
        if last is None or last.popup:
            return None
        self._checkpoint()
        last = self.last_action(opener)
        last.popup = name
        return last

    def default_selection(self) -> int | None:
        """Index of the latest node with a value (what the Variable button acts on by default)."""
        for i in range(len(self.nodes) - 1, -1, -1):
            if self.nodes[i].has_value:
                return i
        return None

    def set_variable(self, index: int, name: str) -> list[int]:
        """Swap the value of node ``index`` for the variable ``name`` (``""`` puts the literal back).

        Every other node with the same literal value and no variable yet gets the
        same variable. Returns the indices changed.
        """
        if not 0 <= index < len(self.nodes) or not self.nodes[index].has_value:
            raise ValueError("that line has no value to replace")
        name = name.strip()
        if name and not valid_variable_name(name):
            raise ValueError(f"{name!r} is not a usable variable name")
        self._checkpoint()
        target = self.nodes[index]
        changed = [index]
        if name:
            for i, node in enumerate(self.nodes):
                if i != index and node.has_value and not node.var and not node.sensitive and node.value == target.value:
                    node.var = name
                    changed.append(i)
        target.var = name or None
        return changed

    # -- variables -----------------------------------------------------------
    def variables(self) -> list[tuple[str, Node]]:
        """Each variable the script uses, in order of first use, with the first node using it."""
        seen: dict[str, Node] = {}
        for node in self.nodes:
            if node.var and node.var not in seen:
                seen[node.var] = node
        return list(seen.items())

    def page_names(self) -> list[str]:
        names = ["page"]
        for node in self.nodes:
            for name in (node.page, node.popup):
                if name and name not in names:
                    names.append(name)
        return names


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


@dataclass
class Rendered:
    """Rendered code plus, for each line, the index of the node it came from (or None)."""

    lines: list[str]
    owners: list[int | None]

    @property
    def code(self) -> str:
        return "\n".join(self.lines) + "\n"


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.owners: list[int | None] = []

    def emit(self, text: str, owner: int | None = None, indent: int = 0) -> None:
        self.lines.append(("    " * indent + text) if text else "")
        self.owners.append(owner)


def _value_expr(node: Node) -> str:
    """The code for a node's value: its variable, or the literal."""
    if node.var:
        return node.var
    value = node.value
    if node.kind in ("upload", "chooser_upload") and isinstance(value, list) and len(value) == 1:
        value = value[0]
    if node.kind == "select" and isinstance(value, list) and len(value) == 1:
        value = value[0]
    return py_value(value)


def _action_line(node: Node, target: str) -> str:
    """One action as a single line of code (chooser uploads are handled by the caller)."""
    if node.kind == "goto":
        return f"{node.page}.goto({_value_expr(node)})"
    if node.kind == "new_page":
        return f"{node.page} = context.new_page()"
    if node.kind == "press" and not node.locator:
        return f"{node.page}.keyboard.press({py_str(node.value)})"
    loc = render_locator(node.locator or [], node.page)
    if node.kind in ("click", "dblclick"):
        opts = []
        if node.button and node.button != "left":
            opts.append(f"button={py_str(node.button)}")
        if node.modifiers:
            opts.append(f"modifiers={py_value(node.modifiers)}")
        return f"{loc}.{node.kind}({', '.join(opts)})"
    if node.kind == "fill":
        return f"{loc}.fill({_value_expr(node)})"
    if node.kind == "press":
        return f"{loc}.press({py_str(node.value)})"
    if node.kind in ("check", "uncheck"):
        return f"{loc}.{node.kind}()"
    if node.kind == "select":
        return f"{loc}.select_option(label={_value_expr(node)})"
    if node.kind == "upload":
        return f"{loc}.set_input_files({_value_expr(node)})"
    raise ValueError(f"cannot render a {node.kind!r} node")


def _render_body(script: Script, w: _Writer, target: str, base_indent: int) -> None:
    indent = base_indent
    open_bodies: list[int] = []  # line count when each open branch began
    attach_count = 0

    for i, node in enumerate(script.nodes):
        if node.kind == "branch":
            w.emit(f"if {node.value}:", i, indent)
            indent += 1
            open_bodies.append(len(w.lines))
            continue
        if node.kind == "end_branch":
            if not open_bodies:
                continue
            if len(w.lines) == open_bodies.pop():
                w.emit("pass", i, indent)
            indent -= 1
            continue
        if node.kind == "step":
            if target == TARGET_PAPERPUSH:
                w.emit(f"step({py_str(node.value)})", i, indent)
            else:
                w.emit(f"# --- {node.value} ---", i, indent)
            continue

        inner = indent
        if node.popup:
            w.emit(f"with {node.page}.expect_popup() as {node.popup}_info:", i, indent)
            inner = indent + 1

        if node.kind == "chooser_upload":
            click = render_locator(node.locator or [], node.page)
            guarded = target == TARGET_PAPERPUSH and node.page == "page" and not node.popup
            if guarded:
                attach_count += 1
                fn = f"_attach_{attach_count}"
                w.emit(f"def {fn}(page):", i, inner)
                w.emit("with page.expect_file_chooser() as fc_info:", i, inner + 1)
                w.emit(f"{click}.click()", i, inner + 2)
                w.emit(f"fc_info.value.set_files({_value_expr(node)})", i, inner + 1)
                label = node.var or (node.value[0] if isinstance(node.value, list) and node.value else "file")
                w.emit(f"page.attempt({py_str('attach ' + str(label))}, {fn})", i, inner)
            else:
                w.emit(f"with {node.page}.expect_file_chooser() as fc_info:", i, inner)
                w.emit(f"{click}.click()", i, inner + 1)
                w.emit(f"fc_info.value.set_files({_value_expr(node)})", i, inner)
        else:
            w.emit(_action_line(node, target), i, inner)

        if node.popup:
            w.emit(f"{node.popup} = {node.popup}_info.value", i, indent)

    while open_bodies:
        if len(w.lines) == open_bodies.pop():
            w.emit("pass", None, indent)
        indent -= 1


def _declaration(name: str, node: Node, file_fields: Iterable[str]) -> str:
    strip = ".strip()" if node.kind in ("upload", "chooser_upload") or name in set(file_fields) else ""
    return f'{name} = values.get({py_str(name)}, ""){strip}'


def render(script: Script, target: str = TARGET_PAPERPUSH, *, file_fields: Iterable[str] = (), header: str = "") -> Rendered:
    """Render the script as code.

    ``paperpush`` renders the body of a venue runner's ``submit()``: the
    ``values.get(...)`` lines, then the steps, with ``step()`` labels and each
    button-opened upload as one ``page.attempt`` unit (see DEVELOPMENT.md).
    ``python`` renders a standalone Playwright script with the recorded values in
    a ``values`` dict.
    """
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; expected one of {', '.join(TARGETS)}")
    file_fields = list(file_fields)
    w = _Writer()
    variables = script.variables()

    if target == TARGET_PAPERPUSH:
        w.emit(f"# {header or 'Recorded with `paperpush codegen`.'}")
        w.emit("# Paste into the venue runner's submit(): the values.get(...) lines go with its other")
        w.emit("# variables, the steps after `page = guard(page)`.")
        if variables:
            w.emit("")
            for name, node in variables:
                w.emit(_declaration(name, node, file_fields), script.nodes.index(node))
        w.emit("")
        _render_body(script, w, target, 0)
        return Rendered(w.lines, w.owners)

    w.emit(f'"""{header or "Recorded with `paperpush codegen`."}"""')
    w.emit("")
    w.emit("from playwright.sync_api import sync_playwright")
    w.emit("")
    w.emit("values = {")
    for name, node in variables:
        if node.sensitive:
            w.emit(f"{py_str(name)}: {py_str('')},  # not recorded: fill in", script.nodes.index(node), 1)
        else:
            value = node.value
            if node.kind in ("upload", "chooser_upload", "select") and isinstance(value, list) and len(value) == 1:
                value = value[0]
            w.emit(f"{py_str(name)}: {py_value(value)},", script.nodes.index(node), 1)
    w.emit("}")
    for name, _node in variables:
        w.emit(f"{name} = values[{py_str(name)}]")
    w.emit("")
    w.emit("with sync_playwright() as playwright:")
    w.emit("browser = playwright.chromium.launch(headless=False)", None, 1)
    w.emit("context = browser.new_context()", None, 1)
    w.emit("page = context.new_page()", None, 1)
    _render_body(script, w, target, 1)
    w.emit("page.pause()  # inspect before closing", None, 1)
    w.emit("context.close()", None, 1)
    w.emit("browser.close()", None, 1)
    return Rendered(w.lines, w.owners)


def suggest_variable(value: Any, kind: str, sub_values: dict[str, str], file_fields: Iterable[str]) -> str | None:
    """The ``.sub`` field whose value this recorded value is, if exactly one is.

    Text and option labels match a field's value with whitespace normalized; an
    uploaded file matches a file field holding the same path or file name.
    """
    if not sub_values or value in (None, "", []):
        return None
    file_fields = set(file_fields)

    def norm(s: str) -> str:
        return " ".join(str(s).split()).casefold()

    if kind in ("upload", "chooser_upload"):
        import os

        paths = value if isinstance(value, list) else [value]
        names = {os.path.basename(str(p)) for p in paths}
        for fid in file_fields:
            raw = sub_values.get(fid) or ""
            entries = [line.split("|", 1)[0].strip() for line in raw.splitlines() if line.strip()]
            if any(e in paths or os.path.basename(e) in names for e in entries):
                return fid
        return None

    texts = value if isinstance(value, list) else [value]
    if len(texts) != 1 or not str(texts[0]).strip():
        return None
    wanted = norm(texts[0])
    matches = [fid for fid, raw in sub_values.items() if fid not in file_fields and raw and norm(raw) == wanted]
    if len(matches) == 1 and valid_variable_name(matches[0]):
        return matches[0]
    return None
