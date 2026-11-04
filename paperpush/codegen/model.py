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

A *branch* is a choice between paths, such as the options of a "manuscript
type" question: the actions recorded between Branch and Next are the first
path, those between Next and Next (or End) the following ones. Each path is
keyed by the label of the element its first action acted on. It renders to a
``match`` on the branch's variable (default ``branch_1``), one ``case`` per
path, so a run takes the path its ``.sub`` value names.
"""

from __future__ import annotations

import ast
import copy
import json
import keyword
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: Node kinds that carry a value the author can swap for a variable.
VALUE_KINDS = ("goto", "fill", "select", "upload", "chooser_upload")
#: Node kinds that are actions (not markers).
ACTION_KINDS = ("goto", "new_page", "click", "dblclick", "fill", "press", "check", "uncheck", "select", "upload", "chooser_upload")
MARKER_KINDS = ("step", "branch", "next_path", "end_branch", "comment", "optional", "end_optional")

TARGET_PAPERPUSH = "paperpush"
TARGET_PYTHON = "python"
TARGETS = (TARGET_PAPERPUSH, TARGET_PYTHON)

#: Seconds within which a file chooser is taken to belong to the click before it.
CHOOSER_WINDOW_SECONDS = 300.0

# Names the rendered code already uses, so a variable may not take them.
_RESERVED = {"page", "context", "browser", "playwright", "values", "step", "fc_info", "sync_playwright", "guard", "match", "case"}

#: Kinds of editable line in the code panel (see :attr:`Rendered.edits`).
EDIT_BRANCH_NAME = "branch_name"
EDIT_OPTION = "option"


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
    return name.isidentifier() and not keyword.iskeyword(name) and name not in _RESERVED and not re.fullmatch(r"page\d*", name)


def _norm(s: Any) -> str:
    return " ".join(str(s).split()).casefold()


@dataclass
class Node:
    """One line (or block) of the recorded script.

    ``kind`` is an action (``goto``, ``click``, ``dblclick``, ``fill``, ``press``,
    ``check``, ``uncheck``, ``select``, ``upload``, ``chooser_upload``,
    ``new_page``) or a marker (``step``, ``branch``, ``next_path``, ``end_branch``,
    ``comment``, ``optional``, ``end_optional``).
    ``value`` holds the URL, the typed text, the option labels, the uploaded
    paths, the key, the step label, or (for a branch) the branch's variable name.
    ``var`` names the variable that replaces ``value`` in the code; ``var_locked``
    is set once the author chose a name, so it is never re-guessed. ``label`` is
    the text of the element acted on, and ``option`` the key of the branch path
    this action starts (the later actions of the path have none). ``popup`` names the page the action opened and ``dialog``
    says the action raised a dialog that was accepted.
    """

    kind: str
    page: str = "page"
    locator: list | None = None
    value: Any = None
    var: str | None = None
    sensitive: bool = False
    popup: str | None = None
    dialog: str | None = None
    var_locked: bool = False
    label: str | None = None
    option: str | None = None
    button: str | None = None
    modifiers: list[str] = field(default_factory=list)
    t: float = 0.0

    @property
    def has_value(self) -> bool:
        return self.kind in VALUE_KINDS

    def summary(self) -> str:
        """A short description for messages ("fill title")."""
        if self.kind == "step":
            return f"step {self.value!r}"
        if self.kind == "branch":
            return f"branch {self.value}"
        if self.kind == "comment":
            return f"options of {self.label!r}" if self.label else "options"
        if self.kind == "optional":
            return "optional section"
        if self.kind == "end_optional":
            return "end of optional section"
        if self.option is not None:
            return f"path {self.option!r}"
        if self.var:
            return f"{self.kind} {self.var}"
        if self.sensitive:
            return f"{self.kind} (hidden value)"
        if self.value not in (None, "", []):
            text = self.value if isinstance(self.value, str) else ", ".join(map(str, self.value))
            return f"{self.kind} {text[:40]!r}"
        return self.kind


def _default_option(node: Node) -> str:
    """A branch path's key: the label of the element, else what its locator names."""
    if node.label and node.label.strip():
        return " ".join(node.label.split())[:200]
    for method, args, kwargs in reversed(node.locator or []):
        if kwargs.get("name"):
            return str(kwargs["name"])
        if args and method != "nth":
            return re.sub(r"^[#.]", "", str(args[-1]))
    return node.kind


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
    def open_branch(self) -> int | None:
        """Index of the branch still open at the end of the script, if any."""
        current = None
        for i, node in enumerate(self.nodes):
            if node.kind == "branch":
                current = i
            elif node.kind == "end_branch":
                current = None
        return current

    @property
    def depth(self) -> int:
        """1 while a branch is open, else 0."""
        return 0 if self.open_branch() is None else 1

    def branch_of(self, index: int) -> int | None:
        """The branch node that node ``index`` is (or is a path of), if any."""
        current = None
        for i, node in enumerate(self.nodes[: index + 1]):
            if node.kind == "branch":
                current = i
            elif node.kind == "end_branch":
                current = None if i < index else current
        return current

    def branch_paths(self, branch: int) -> list[int]:
        """Indices of the first actions of the paths of the branch at ``branch``."""
        paths = []
        for i in range(branch + 1, len(self.nodes)):
            node = self.nodes[i]
            if node.kind in ("end_branch", "branch"):
                break
            if node.option is not None:
                paths.append(i)
        return paths

    def path_open(self) -> bool:
        """Whether the open branch has a path in progress (something recorded since Branch or Next)."""
        branch = self.open_branch()
        if branch is None:
            return False
        for node in reversed(self.nodes[branch + 1 :]):
            if node.kind == "next_path":
                return False
            if node.option is not None:
                return True
        return False

    def path_head(self, index: int) -> int | None:
        """The first action of the branch path that node ``index`` belongs to, if any."""
        for i in range(index, -1, -1):
            node = self.nodes[i]
            if node.option is not None:
                return i
            if node.kind in ("next_path", "branch", "end_branch"):
                return None
        return None

    def last_action(self, page: str | None = None) -> Node | None:
        """The latest node that is an action (not a marker), optionally on ``page``."""
        for node in reversed(self.nodes):
            if node.kind in MARKER_KINDS:
                return None
            if page is None or node.page == page:
                return node
        return None

    def branch_names(self) -> list[str]:
        return [n.value for n in self.nodes if n.kind == "branch"]

    def enter_branch(self, name: str | None = None) -> str:
        """Open a branch; what is recorded until :meth:`next_path` (or :meth:`exit_branch`) is its first path. Returns its name."""
        if self.open_branch() is not None:
            raise ValueError("a branch is already open; press End first")
        if name is None:
            used = set(self.branch_names()) | {n.var for n in self.nodes if n.var}
            n = 1
            while f"branch_{n}" in used:
                n += 1
            name = f"branch_{n}"
        elif not valid_variable_name(name):
            raise ValueError(f"{name!r} is not a usable variable name")
        self._checkpoint()
        self.nodes.append(Node("branch", value=name))
        return name

    def next_path(self) -> bool:
        """Close the path in progress, so the next action recorded starts the branch's next path."""
        if not self.path_open():
            return False
        self._checkpoint()
        self.nodes.append(Node("next_path"))
        return True

    def exit_branch(self) -> bool:
        if self.open_branch() is None:
            return False
        self._checkpoint()
        self.nodes.append(Node("end_branch"))
        return True

    def optional_open(self) -> bool:
        """Whether an Optional section is open at the end of the script."""
        open_ = False
        for node in self.nodes:
            if node.kind == "optional":
                open_ = True
            elif node.kind == "end_optional":
                open_ = False
        return open_

    def toggle_optional(self) -> bool:
        """Open an Optional section, or close the open one. Returns whether one is now open."""
        self._checkpoint()
        opening = not self.optional_open()
        self.nodes.append(Node("optional" if opening else "end_optional"))
        return opening

    def add_comment(self, label: str, options: list[str]) -> Node:
        """Note the options a dropdown (or list, or radio group) offers, as a comment in the code."""
        self._checkpoint()
        node = Node("comment", value=[str(o) for o in options], label=" ".join(str(label or "").split()))
        self.nodes.append(node)
        return node

    def add_step(self, label: str) -> None:
        self._checkpoint()
        self.nodes.append(Node("step", value=label.strip()))

    def rename_branch(self, index: int, name: str, *, locked: bool = True) -> None:
        """Rename the branch at ``index`` (``locked=False`` for an automatic guess, with no undo step)."""
        node = self.nodes[index]
        if node.kind != "branch":
            raise ValueError("that line is not a branch")
        name = name.strip()
        if not valid_variable_name(name):
            raise ValueError(f"{name!r} is not a usable variable name")
        if locked:
            self._checkpoint()
            node = self.nodes[index]
        node.value = name
        node.var_locked = node.var_locked or locked

    def set_option(self, index: int, key: str) -> None:
        """Change the key of the branch path at ``index``."""
        node = self.nodes[index]
        if node.option is None:
            raise ValueError("that line is not a branch path")
        branch = self.branch_of(index)
        others = {self.nodes[i].option for i in self.branch_paths(branch) if i != index} if branch is not None else set()
        if key in others:
            raise ValueError(f"the branch already has a path {key!r}")
        self._checkpoint()
        self.nodes[index].option = key

    # -- recording -----------------------------------------------------------
    def add(self, node: Node) -> Node:
        """Append a recorded action, merging it into the previous one where that is what happened.

        * More typing into the field just filled updates that ``fill``.
        * A second click with ``detail >= 2`` on the element just clicked turns it into ``dblclick``
          (pass the click count in ``node.value`` for clicks).
        * An upload that a button opened (``chooser_upload``) takes over the click before it.
        * A navigation right after another, or to the URL just opened, is the same navigation.
        * Inside an open branch, the first action after Branch or Next starts a path; the
          actions after it, until Next or End, are the rest of that path.

        Returns the node that now holds the action.
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
            prev = self.last_action(node.page)
            if prev is not None and prev.kind == "goto" and (node.t - prev.t < 5 or prev.value == node.value):
                return prev

        if self.open_branch() is not None and node.kind not in MARKER_KINDS and not self.path_open():
            branch = self.open_branch()
            taken = {self.nodes[i].option for i in self.branch_paths(branch)}
            key = base = _default_option(node)
            n = 2
            while key in taken:
                key, n = f"{base} {n}", n + 1
            node.option = key

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

    def variable_target(self, index: int | None) -> tuple[str, int] | None:
        """What the Variable button acts on for the line ``index``.

        ``("branch", i)`` renames the branch ``i`` (for the branch line or any of
        its paths), ``("value", i)`` swaps the value of a line that has one, and
        ``None`` means the line has nothing to name.
        """
        if index is None or not 0 <= index < len(self.nodes):
            return None
        node = self.nodes[index]
        if node.kind == "branch":
            return ("branch", index)
        if node.option is not None:
            branch = self.branch_of(index)
            if branch is not None:
                return ("branch", branch)
        if node.has_value:
            return ("value", index)
        return None

    def default_selection(self) -> int | None:
        """The line the Variable button acts on when none is selected: the latest that has something to name."""
        for i in range(len(self.nodes) - 1, -1, -1):
            if self.variable_target(i) is not None:
                return i
        return None

    def set_variable(self, index: int, name: str) -> list[int]:
        """Swap the value of node ``index`` for the variable ``name`` (``""`` puts the literal back).

        On a branch (or one of its paths) it renames the branch instead. Every
        other node with the same literal value and no variable yet gets the same
        variable. Returns the indices changed.
        """
        target = self.variable_target(index)
        if target is None:
            raise ValueError("that line has no value to replace; for a choice between options, record it as a Branch")
        name = name.strip()
        if target[0] == "branch":
            self.rename_branch(target[1], name)
            return [target[1]]
        if name and not valid_variable_name(name):
            raise ValueError(f"{name!r} is not a usable variable name")
        self._checkpoint()
        node = self.nodes[index]
        changed = [index]
        if name:
            for i, other in enumerate(self.nodes):
                if i != index and other.has_value and not other.var and not other.sensitive and other.value == node.value:
                    other.var = name
                    changed.append(i)
        node.var = name or None
        node.var_locked = True
        return changed

    def edit_line(self, kind: str, index: int, text: str) -> str:
        """Apply an edit the author typed over an editable line of the code panel. Returns a message."""
        text = text.strip()
        if kind == EDIT_BRANCH_NAME:
            m = re.match(r"^(?:match\s+)?([A-Za-z_]\w*)", text.lstrip("# "))
            if not m:
                raise ValueError("type the branch's name, e.g. match manuscript_type:")
            self.rename_branch(index, m.group(1))
            return f"Branch renamed to {m.group(1)}."
        if kind == EDIT_OPTION:
            m = re.match(r"^(?:case\s+)?(.+?)\s*:?\s*$", text)
            raw = m.group(1) if m else text
            try:
                key = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                key = raw.strip("'\"")
            if not isinstance(key, str) or not key.strip():
                raise ValueError('type the path as case "Option text":')
            self.set_option(index, key)
            return f"Path renamed to {key!r}."
        raise ValueError(f"line kind {kind!r} cannot be edited")

    # -- variables -----------------------------------------------------------
    def variables(self) -> list[tuple[str, Node]]:
        """Each variable the script uses, in order of first use, with the first node using it."""
        seen: dict[str, Node] = {}
        for i, node in enumerate(self.nodes):
            name = node.value if node.kind == "branch" else node.var
            if node.kind == "branch" and not self.branch_paths(i):
                continue
            if name and name not in seen:
                seen[name] = node
        return list(seen.items())

    def recorded_value(self, node: Node) -> Any:
        """The value the variable for ``node`` had while recording (a branch: the path taken last)."""
        if node.kind == "branch":
            paths = self.branch_paths(self.nodes.index(node))
            return self.nodes[paths[-1]].option if paths else ""
        value = node.value
        if node.kind in ("upload", "chooser_upload", "select") and isinstance(value, list) and len(value) == 1:
            return value[0]
        return value

    def page_names(self) -> list[str]:
        names = ["page"]
        for node in self.nodes:
            for name in (node.page, node.popup):
                if name and name not in names:
                    names.append(name)
        return names


def suggest_branch_name(keys: list[str], field_options: dict[str, list[str]], sub_values: dict[str, str]) -> str | None:
    """The venue field whose options are this branch's path keys, if one clearly is.

    A field qualifies when at least half the keys (and at least one) are among
    its options; failing that, a ``.sub`` field whose value is one of the keys.
    """
    if not keys:
        return None
    wanted = [_norm(k) for k in keys]
    best, best_hits = None, 0
    for fid, options in field_options.items():
        opts = {_norm(o) for o in options or []}
        hits = sum(1 for k in wanted if k in opts)
        if hits > best_hits:
            best, best_hits = fid, hits
    if best and best_hits * 2 >= len(wanted) and valid_variable_name(best):
        return best
    matches = [fid for fid, raw in sub_values.items() if raw and _norm(raw) in wanted and valid_variable_name(fid)]
    return matches[0] if len(matches) == 1 else None


def choose_path(script: Script, branch: int, value: Any) -> int | None:
    """Which path of ``branch`` a run with variable ``value`` takes (else the last one recorded)."""
    paths = script.branch_paths(branch)
    if not paths:
        return None
    if value not in (None, ""):
        for i in paths:
            if _norm(script.nodes[i].option) == _norm(value):
                return i
    return paths[-1]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


@dataclass
class Rendered:
    """Rendered code plus, per line, the node it came from (or None) and how it can be edited.

    ``edits[i]`` is :data:`EDIT_BRANCH_NAME` or :data:`EDIT_OPTION` for a line
    the author may retype in the code panel, else None.
    """

    lines: list[str]
    owners: list[int | None]
    edits: list[str | None]

    @property
    def code(self) -> str:
        return "\n".join(self.lines) + "\n"


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.owners: list[int | None] = []
        self.edits: list[str | None] = []

    def emit(self, text: str, owner: int | None = None, indent: int = 0, edit: str | None = None) -> None:
        self.lines.append(("    " * indent + text) if text else "")
        self.owners.append(owner)
        self.edits.append(edit)


def _value_expr(node: Node) -> str:
    """The code for a node's value: its variable, or the literal."""
    if node.var:
        return node.var
    value = node.value
    if node.kind in ("upload", "chooser_upload", "select") and isinstance(value, list) and len(value) == 1:
        value = value[0]
    return py_value(value)


def _action_line(node: Node) -> str:
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


def _render_action(node: Node, i: int, w: _Writer, target: str, indent: int, counter: list[int]) -> None:
    if node.dialog:
        w.emit(f'{node.page}.once("dialog", lambda dialog: dialog.accept())', i, indent)
    inner = indent
    if node.popup:
        w.emit(f"with {node.page}.expect_popup() as {node.popup}_info:", i, indent)
        inner = indent + 1

    if node.kind == "chooser_upload":
        click = render_locator(node.locator or [], node.page)
        guarded = target == TARGET_PAPERPUSH and node.page == "page" and not node.popup
        if guarded:
            counter[0] += 1
            fn = f"_attach_{counter[0]}"
            if w.lines and w.lines[-1].strip() and not w.lines[-1].rstrip().endswith(":"):
                w.emit("", i)
            w.emit(f"def {fn}(page):", i, inner)
            w.emit("with page.expect_file_chooser() as fc_info:", i, inner + 1)
            w.emit(f"{click}.click()", i, inner + 2)
            w.emit(f"fc_info.value.set_files({_value_expr(node)})", i, inner + 1)
            w.emit("", i)
            label = node.var or (node.value[0] if isinstance(node.value, list) and node.value else "file")
            w.emit(f"page.attempt({py_str('attach ' + str(label))}, {fn})", i, inner)
        else:
            w.emit(f"with {node.page}.expect_file_chooser() as fc_info:", i, inner)
            w.emit(f"{click}.click()", i, inner + 1)
            w.emit(f"fc_info.value.set_files({_value_expr(node)})", i, inner)
    else:
        w.emit(_action_line(node), i, inner)

    if node.popup:
        w.emit(f"{node.popup} = {node.popup}_info.value", i, indent)


def _render_body(script: Script, w: _Writer, target: str, base_indent: int) -> None:
    counter = [0]
    branch: str | None = None  # name of the open branch
    in_match = False  # whether that branch rendered a `match` header
    in_path = False  # whether a `case` of that branch is open
    for i, node in enumerate(script.nodes):
        if node.kind == "branch":
            branch = node.value
            in_path = False
            in_match = bool(script.branch_paths(i))
            if in_match:
                w.emit(f"match {branch}:", i, base_indent, EDIT_BRANCH_NAME)
            else:
                w.emit(f"# {branch}: no paths recorded yet", i, base_indent, EDIT_BRANCH_NAME)
            continue
        if node.kind == "end_branch":
            branch, in_path = None, False
            continue
        if node.kind == "next_path":
            in_path = False
            continue
        # Inside a case, lines sit in its body; between cases, comments sit with the cases.
        body = base_indent + 2 if branch is not None and in_path else base_indent
        if node.kind == "comment":
            indent = body if in_path else base_indent + (1 if branch is not None and in_match else 0)
            options = list(node.value or [])
            title = f"Options of {py_str(node.label)}" if node.label else "Options"
            w.emit(f"# {title} ({len(options)}):", i, indent)
            for option in options:
                w.emit(f"#   - {option}", i, indent)
            continue
        if node.kind in ("optional", "end_optional"):
            text = "# ---OPTIONAL---" if node.kind == "optional" else "# ---END OPTIONAL---"
            w.emit(text, i, body if in_path else base_indent + (1 if branch is not None and in_match else 0))
            continue
        if node.kind == "step":
            if branch is not None and in_match and not in_path:
                w.emit(f"# --- {node.value} ---", i, base_indent + 1)  # a match body holds only cases
            elif target == TARGET_PAPERPUSH:
                w.emit(f"step({py_str(node.value)})", i, body)
            else:
                w.emit(f"# --- {node.value} ---", i, body)
            continue
        if branch is not None and node.option is not None:
            in_path = True
            w.emit(f"case {py_str(node.option)}:", i, base_indent + 1, EDIT_OPTION)
            _render_action(node, i, w, target, base_indent + 2, counter)
            continue
        _render_action(node, i, w, target, body, counter)


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

    def owner(node: Node) -> int:
        return next(i for i, n in enumerate(script.nodes) if n is node)

    if target == TARGET_PAPERPUSH:
        w.emit(f"# {header or 'Recorded with `paperpush codegen`.'}")
        w.emit("# Paste into the venue runner's submit(): the values.get(...) lines go with its other")
        w.emit("# variables, the steps after `page = guard(page)`.")
        if variables:
            w.emit("")
            for name, node in variables:
                edit = EDIT_BRANCH_NAME if node.kind == "branch" else None
                w.emit(_declaration(name, node, file_fields), owner(node), 0, edit)
        w.emit("")
        _render_body(script, w, target, 0)
        return Rendered(w.lines, w.owners, w.edits)

    w.emit(f'"""{header or "Recorded with `paperpush codegen`."}"""')
    w.emit("")
    w.emit("from playwright.sync_api import sync_playwright")
    w.emit("")
    w.emit("values = {")
    for name, node in variables:
        edit = EDIT_BRANCH_NAME if node.kind == "branch" else None
        if node.sensitive:
            w.emit(f"{py_str(name)}: {py_str('')},  # not recorded: fill in", owner(node), 1, edit)
        else:
            w.emit(f"{py_str(name)}: {py_value(script.recorded_value(node))},", owner(node), 1, edit)
    w.emit("}")
    for name, node in variables:
        w.emit(f"{name} = values[{py_str(name)}]", owner(node), 0, EDIT_BRANCH_NAME if node.kind == "branch" else None)
    w.emit("")
    w.emit("with sync_playwright() as playwright:")
    w.emit("browser = playwright.chromium.launch(headless=False)", None, 1)
    w.emit("context = browser.new_context()", None, 1)
    w.emit("page = context.new_page()", None, 1)
    _render_body(script, w, target, 1)
    w.emit("page.pause()  # inspect before closing", None, 1)
    w.emit("context.close()", None, 1)
    w.emit("browser.close()", None, 1)
    return Rendered(w.lines, w.owners, w.edits)


def suggest_variable(value: Any, kind: str, sub_values: dict[str, str], file_fields: Iterable[str]) -> str | None:
    """The ``.sub`` field whose value this recorded value is, if exactly one is.

    Text and option labels match a field's value with whitespace normalized; an
    uploaded file matches a file field holding the same path or file name.
    """
    if not sub_values or value in (None, "", []):
        return None
    file_fields = set(file_fields)

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
    wanted = _norm(texts[0])
    matches = [fid for fid, raw in sub_values.items() if fid not in file_fields and raw and _norm(raw) == wanted]
    if len(matches) == 1 and valid_variable_name(matches[0]):
        return matches[0]
    return None
