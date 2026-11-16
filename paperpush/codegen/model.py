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

Branches nest: Branch pressed inside a path opens a branch within that path
(Next and End then act on the innermost open branch). Pressed before the path
has any action, the new branch is itself the start of the path; the path takes
its key from the first action recorded inside it (``path N`` until then).
"""

from __future__ import annotations

import ast
import contextlib
import copy
import io
import json
import keyword
import re
import tokenize
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
    this action (or nested branch) starts (the later actions of the path have
    none); ``option_auto`` marks a nested branch's placeholder key, replaced by
    the label of the first action recorded inside it. ``popup`` names the page the action opened and ``dialog``
    says the action raised a dialog that was accepted. ``note`` is the author's comment on an action,
    rendered at the end of its line (``line_of_code  # note``).
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
    option_auto: bool = False
    button: str | None = None
    modifiers: list[str] = field(default_factory=list)
    note: str | None = None
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

    def replace(self, nodes: list[Node]) -> None:
        """Swap the whole script for ``nodes`` (one undo step), as when loading saved code."""
        self._checkpoint()
        self.nodes = list(nodes)

    # -- structure -----------------------------------------------------------
    def branch_stack(self) -> list[int]:
        """Indices of the branches still open at the end of the script, outermost first."""
        stack: list[int] = []
        for i, node in enumerate(self.nodes):
            if node.kind == "branch":
                stack.append(i)
            elif node.kind == "end_branch" and stack:
                stack.pop()
        return stack

    def open_branch(self) -> int | None:
        """Index of the innermost branch still open at the end of the script, if any."""
        stack = self.branch_stack()
        return stack[-1] if stack else None

    @property
    def depth(self) -> int:
        """How many branches are open (nested) at the end of the script."""
        return len(self.branch_stack())

    def parent_branch(self, index: int) -> int | None:
        """The innermost branch that node ``index`` sits inside (not counting the node itself)."""
        stack: list[int] = []
        for i, node in enumerate(self.nodes[:index]):
            if node.kind == "branch":
                stack.append(i)
            elif node.kind == "end_branch" and stack:
                stack.pop()
        return stack[-1] if stack else None

    def branch_of(self, index: int) -> int | None:
        """The branch node that node ``index`` is (or is inside), if any."""
        if 0 <= index < len(self.nodes) and self.nodes[index].kind == "branch":
            return index
        return self.parent_branch(index)

    def _branch_level(self, branch: int):
        """Yield ``(index, node)`` for the nodes directly in ``branch`` (nested branches' insides skipped)."""
        level = 0
        for i in range(branch + 1, len(self.nodes)):
            node = self.nodes[i]
            if node.kind == "end_branch":
                if level == 0:
                    return
                level -= 1
                continue
            if level == 0:
                yield i, node
            if node.kind == "branch":
                level += 1

    def branch_paths(self, branch: int) -> list[int]:
        """Indices of the first nodes (actions or nested branches) of the paths of the branch at ``branch``."""
        return [i for i, node in self._branch_level(branch) if node.option is not None]

    def path_open(self) -> bool:
        """Whether the innermost open branch has a path in progress (something recorded since Branch or Next)."""
        branch = self.open_branch()
        if branch is None:
            return False
        open_ = False
        for _, node in self._branch_level(branch):
            if node.kind == "next_path":
                open_ = False
            elif node.option is not None:
                open_ = True
        return open_

    def path_head(self, index: int) -> int | None:
        """The first node of the branch path that node ``index`` belongs to (in its innermost branch), if any."""
        level = 0
        for i in range(index, -1, -1):
            node = self.nodes[i]
            if node.kind == "end_branch" and i != index:
                level += 1
                continue
            if node.kind == "branch" and i != index:
                if level == 0:
                    return None  # reached the branch this node is in
                level -= 1
                if level == 0 and node.option is not None:
                    return i  # a nested branch that starts this path
                continue
            if level:
                continue
            if node.option is not None:
                return i
            if node.kind in ("next_path", "end_branch"):
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
        """Open a branch; what is recorded until :meth:`next_path` (or :meth:`exit_branch`) is its first path. Returns its name.

        Inside an open branch it opens a nested one: part of the path in
        progress, or, when that path has nothing yet, the start of a new path.
        """
        if name is None:
            used = set(self.branch_names()) | {n.var for n in self.nodes if n.var}
            n = 1
            while f"branch_{n}" in used:
                n += 1
            name = f"branch_{n}"
        elif not valid_variable_name(name):
            raise ValueError(f"{name!r} is not a usable variable name")
        node = Node("branch", value=name)
        parent = self.open_branch()
        if parent is not None and not self.path_open():
            node.option = self._unique_option(parent, f"path {len(self.branch_paths(parent)) + 1}")
            node.option_auto = True
        self._checkpoint()
        self.nodes.append(node)
        return name

    def _unique_option(self, branch: int, base: str, exclude: int | None = None) -> str:
        """``base``, numbered if another path of ``branch`` already has that key."""
        taken = {self.nodes[i].option for i in self.branch_paths(branch) if i != exclude}
        key, n = base, 2
        while key in taken:
            key, n = f"{base} {n}", n + 1
        return key

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

    def note_target(self, index: int | None) -> int | None:
        """The action the Comment button annotates: line ``index`` if it is an action, else the latest action before it."""
        if index is None or not 0 <= index < len(self.nodes):
            index = len(self.nodes) - 1
        for i in range(index, -1, -1):
            if self.nodes[i].kind in ACTION_KINDS:
                return i
        return None

    def set_note(self, index: int, text: str) -> None:
        """Set the comment at the end of action ``index``'s line (``""`` removes it)."""
        node = self.nodes[index]
        if node.kind not in ACTION_KINDS:
            raise ValueError("only an action's line can take a comment")
        note = clean_note(text)
        if note == (node.note or ""):
            return
        self._checkpoint()
        self.nodes[index].note = note or None

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
        branch = self.parent_branch(index)
        others = {self.nodes[i].option for i in self.branch_paths(branch) if i != index} if branch is not None else set()
        if key in others:
            raise ValueError(f"the branch already has a path {key!r}")
        self._checkpoint()
        self.nodes[index].option = key
        self.nodes[index].option_auto = False

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

        branch = self.open_branch()
        if branch is not None and node.kind not in MARKER_KINDS and not self.path_open():
            node.option = self._unique_option(branch, _default_option(node))

        self._checkpoint()
        if node.option is not None:
            self._claim_placeholder_keys(branch, node.option)
        self.nodes.append(node)
        return node

    def _claim_placeholder_keys(self, branch: int, key: str) -> None:
        """Key the nested branches that start paths, and are still waiting for one, after the first action inside them."""
        while branch is not None and self.nodes[branch].option_auto:
            parent = self.parent_branch(branch)
            if parent is None:
                break
            self.nodes[branch].option = self._unique_option(parent, key, exclude=branch)
            self.nodes[branch].option_auto = False
            branch = parent

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
            branch = self.parent_branch(index)
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


def clean_note(text: Any) -> str:
    """An author's comment as one line, without the leading ``#``."""
    return " ".join(str(text or "").split()).lstrip("#").strip()


def _with_note(line: str, node: Node) -> str:
    return f"{line}  # {node.note}" if node.note else line


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
            w.emit(_with_note(f"page.attempt({py_str('attach ' + str(label))}, {fn})", node), i, inner)
        else:
            w.emit(f"with {node.page}.expect_file_chooser() as fc_info:", i, inner)
            w.emit(f"{click}.click()", i, inner + 1)
            w.emit(_with_note(f"fc_info.value.set_files({_value_expr(node)})", node), i, inner)
    else:
        w.emit(_with_note(_action_line(node), node), i, inner)

    if node.popup:
        w.emit(f"{node.popup} = {node.popup}_info.value", i, indent)


@dataclass
class _Frame:
    """An open branch while rendering: where its ``match`` sits and the state of its current ``case``."""

    indent: int  # indentation of the ``match`` line
    in_match: bool  # whether the branch rendered a ``match`` header (it has paths)
    in_path: bool = False  # whether a ``case`` is open
    statements: int = 0  # statements in the open ``case`` so far


def _render_body(script: Script, w: _Writer, target: str, base_indent: int) -> None:
    counter = [0]
    stack: list[_Frame] = []

    def between() -> int:
        """Indent for a line inside the innermost branch but outside its cases (a match body holds only cases)."""
        f = stack[-1]
        return f.indent + (1 if f.in_match else 0)

    def body() -> int:
        """Indent for a statement at the current position."""
        if not stack:
            return base_indent
        return stack[-1].indent + 2 if stack[-1].in_path else between()

    def close_case() -> None:
        """End the innermost branch's open ``case``; one with no statement gets ``pass``."""
        f = stack[-1]
        if f.in_path and not f.statements:
            w.emit("pass", None, f.indent + 2)
        f.in_path = False

    def statement() -> None:
        if stack:
            stack[-1].statements += 1

    def open_case(node: Node, i: int) -> None:
        f = stack[-1]
        f.in_path, f.statements = True, 0
        w.emit(f"case {py_str(node.option)}:", i, f.indent + 1, EDIT_OPTION)

    for i, node in enumerate(script.nodes):
        if node.kind == "branch":
            if stack and node.option is not None:
                open_case(node, i)
            indent = body()
            in_match = bool(script.branch_paths(i))
            if in_match:
                w.emit(f"match {node.value}:", i, indent, EDIT_BRANCH_NAME)
                statement()
            else:
                w.emit(f"# {node.value}: no paths recorded yet", i, indent, EDIT_BRANCH_NAME)
            stack.append(_Frame(indent=indent, in_match=in_match))
            continue
        if node.kind == "end_branch":
            if stack:
                close_case()
                stack.pop()
            continue
        if node.kind == "next_path":
            if stack:
                close_case()
            continue
        in_path = bool(stack) and stack[-1].in_path
        if node.kind == "comment":
            indent = body() if (in_path or not stack) else between()
            options = list(node.value or [])
            title = f"Options of {py_str(node.label)}" if node.label else "Options"
            w.emit(f"# {title} ({len(options)}):", i, indent)
            for option in options:
                w.emit(f"#   - {option}", i, indent)
            continue
        if node.kind in ("optional", "end_optional"):
            text = "# ---OPTIONAL---" if node.kind == "optional" else "# ---END OPTIONAL---"
            w.emit(text, i, body() if (in_path or not stack) else between())
            continue
        if node.kind == "step":
            if stack and not in_path:
                w.emit(f"# --- {node.value} ---", i, between())
            elif target == TARGET_PAPERPUSH:
                w.emit(f"step({py_str(node.value)})", i, body())
                statement()
            else:
                w.emit(f"# --- {node.value} ---", i, body())
            continue
        if stack and node.option is not None:
            open_case(node, i)
        _render_action(node, i, w, target, body(), counter)
        statement()
    while stack:
        close_case()
        stack.pop()


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


# --------------------------------------------------------------------------
# Loading saved code back into a script
# --------------------------------------------------------------------------

_ACTION_METHODS = {
    "click": "click",
    "dblclick": "dblclick",
    "fill": "fill",
    "press": "press",
    "check": "check",
    "uncheck": "uncheck",
    "select_option": "select",
    "set_input_files": "upload",
}
_STEP_COMMENT = re.compile(r"^#\s*---\s*(.*?)\s*---\s*$")
_OPTIONS_COMMENT = re.compile(r"^#\s*Options(?: of (\".*\"))?\s*\(\d+\):\s*$")
_OPTION_ITEM = re.compile(r"^#\s{3}-\s(.*)$")


@dataclass
class Loaded:
    """Code read back by :func:`parse_code`: its nodes and the values it declares (the ``python`` target's ``values`` dict)."""

    nodes: list[Node]
    values: dict[str, Any]


class _Parser:
    """Turn code rendered by :func:`render` (either target) back into nodes."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.nodes: list[Node] = []
        self.values: dict[str, Any] = {}
        self.notes: dict[int, str] = {}
        self.markers = self._comment_markers(text, self.notes)
        self.dialog_pending: set[str] = set()

    # -- comments (steps, options, optional sections) -------------------------
    @staticmethod
    def _comment_markers(text: str, notes: dict[int, str]) -> list[tuple[int, Node]]:
        """The comment lines that are markers (steps, options, optional sections); comments after code go in ``notes`` by line."""
        comments: list[tuple[int, str]] = []
        with contextlib.suppress(tokenize.TokenError, SyntaxError):  # ast.parse reports broken code properly
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type == tokenize.COMMENT:
                    if tok.line[: tok.start[1]].strip():
                        notes[tok.start[0]] = clean_note(tok.string)
                    else:
                        comments.append((tok.start[0], tok.string.strip()))
        markers: list[tuple[int, Node]] = []
        i = 0
        while i < len(comments):
            line, text_ = comments[i]
            i += 1
            if text_ == "# ---OPTIONAL---":
                markers.append((line, Node("optional")))
            elif text_ == "# ---END OPTIONAL---":
                markers.append((line, Node("end_optional")))
            elif m := _OPTIONS_COMMENT.match(text_):
                label = json.loads(m.group(1)) if m.group(1) else ""
                options = []
                while i < len(comments) and comments[i][0] == comments[i - 1][0] + 1 and (item := _OPTION_ITEM.match(comments[i][1])):
                    options.append(item.group(1))
                    i += 1
                markers.append((line, Node("comment", value=options, label=label)))
            elif m := _STEP_COMMENT.match(text_):
                markers.append((line, Node("step", value=m.group(1))))
        return markers

    def _flush_markers(self, before: int | None) -> None:
        while self.markers and (before is None or self.markers[0][0] < before):
            self.nodes.append(self.markers.pop(0)[1])

    # -- expressions ---------------------------------------------------------
    @staticmethod
    def _fail(node: ast.AST, what: str) -> ValueError:
        return ValueError(f"line {getattr(node, 'lineno', '?')}: {what}")

    def _literal(self, node: ast.AST) -> Any:
        try:
            return ast.literal_eval(node)
        except (ValueError, SyntaxError):
            raise self._fail(node, f"expected a literal value, found {ast.unparse(node)!r}") from None

    def _value(self, node: ast.AST) -> tuple[Any, str | None]:
        """A value argument: ``(literal, None)`` or ``(recorded value, variable name)``."""
        if isinstance(node, ast.Name):
            return self.values.get(node.id), node.id
        return self._literal(node), None

    def _locator(self, node: ast.AST) -> tuple[str, list]:
        """``page.get_by_role(...).first`` -> ``("page", [segments])``."""
        segments: list = []
        while True:
            if isinstance(node, ast.Name):
                return node.id, list(reversed(segments))
            if isinstance(node, ast.Attribute) and node.attr == "first":
                segments.append(["nth", [0], {}])
                node = node.value
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                args = [self._literal(a) for a in node.args]
                kwargs = {k.arg: self._literal(k.value) for k in node.keywords}
                segments.append([node.func.attr, args, kwargs])
                node = node.func.value
            else:
                raise self._fail(node, f"cannot read the locator {ast.unparse(node)!r}")

    # -- statements ----------------------------------------------------------
    def parse(self) -> Loaded:
        try:
            tree = ast.parse(self.text)
        except SyntaxError as exc:
            raise ValueError(f"line {exc.lineno}: {exc.msg}") from None
        body = list(tree.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]  # docstring
        self._statements(body)
        self._flush_markers(None)
        return Loaded(self.nodes, self.values)

    def _statements(self, body: list[ast.stmt]) -> None:
        defs: dict[str, ast.FunctionDef] = {}
        i = 0
        while i < len(body):
            stmt = body[i]
            i += 1
            self._flush_markers(stmt.lineno)
            before = len(self.nodes)
            consumed = self._statement(stmt, body[i] if i < len(body) else None, defs)
            # A comment after an action's line is its note; compound statements (match, popup blocks)
            # had theirs attached while their bodies were read.
            chooser = isinstance(stmt, ast.With) and "expect_file_chooser" in ast.unparse(stmt.items[0].context_expr)
            if not isinstance(stmt, (ast.Match, ast.With)) or chooser:
                last = body[i + consumed - 1] if consumed else stmt
                self._attach_note(before, stmt.lineno, last.end_lineno or last.lineno)
            i += consumed

    def _attach_note(self, before: int, first: int, last: int) -> None:
        """Give the actions added since ``before`` the comment found at the end of lines ``first``..``last``."""
        note = next((self.notes[n] for n in range(first, last + 1) if self.notes.get(n)), None)
        if note:
            for node in self.nodes[before:]:
                if node.kind in ACTION_KINDS and node.note is None:
                    node.note = note

    def _statement(self, stmt: ast.stmt, after: ast.stmt | None, defs: dict[str, ast.FunctionDef]) -> int:
        """Read one statement; returns how many statements after it it used too."""
        if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.Pass)):
            return 0
        if isinstance(stmt, ast.FunctionDef):
            defs[stmt.name] = stmt
            return 0
        if isinstance(stmt, ast.Assign) and self._declaration(stmt):
            return 0
        if isinstance(stmt, ast.With):
            consumed = self._with(stmt, after)
            if consumed is not None:
                return consumed
        if isinstance(stmt, ast.Match):
            self._match(stmt)
            return 0
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) and self._call(stmt.value, defs):
            return 0
        if isinstance(stmt, ast.Assign) and self._new_page(stmt):
            return 0
        raise self._fail(stmt, f"not a recorded action: {ast.unparse(stmt).splitlines()[0]!r}")

    def _declaration(self, stmt: ast.Assign) -> bool:
        """``values = {...}``, ``x = values[...]``, ``x = values.get(...)``, and the python target's browser setup."""
        if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
            return False
        name, value = stmt.targets[0].id, stmt.value
        if name == "values" and isinstance(value, ast.Dict):
            self.values.update(self._literal(value))
            return True
        src = ast.unparse(value)
        if re.fullmatch(r"values(\[.*\]|\.get\(.*\))(\.strip\(\))?", src):
            return True
        if name in ("browser", "context") or (name == "page" and src == "context.new_page()" and not self.nodes):
            return True
        return False

    def _new_page(self, stmt: ast.Assign) -> bool:
        if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and ast.unparse(stmt.value) == "context.new_page()":
            self._add(Node("new_page", page=stmt.targets[0].id))
            return True
        return False

    def _with(self, stmt: ast.With, after: ast.stmt | None) -> int | None:
        """Read a ``with`` block; returns how many statements after it it used too (None: not one of ours)."""
        if len(stmt.items) != 1:
            return None
        item = stmt.items[0]
        ctx = item.context_expr
        if isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Name) and ctx.func.id == "sync_playwright":
            self._statements(stmt.body)  # the python target's whole script
            return 0
        if not (isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute) and isinstance(ctx.func.value, ast.Name)):
            return None
        page, method = ctx.func.value.id, ctx.func.attr
        var = item.optional_vars.id if isinstance(item.optional_vars, ast.Name) else None
        if method == "expect_popup" and var and var.endswith("_info"):
            popup = var[: -len("_info")]
            before = len(self.nodes)
            self._statements(stmt.body)
            actions = [n for n in self.nodes[before:] if n.kind in ACTION_KINDS]
            if not actions:
                raise self._fail(stmt, "a popup block with no action")
            actions[-1].popup = popup
            return 1 if after is not None and ast.unparse(after) == f"{popup} = {var}.value" else 0
        if method == "expect_file_chooser":
            self._add(self._chooser(page, stmt, after))
            return 1
        return None

    def _chooser(self, page: str, stmt: ast.With, after: ast.stmt | None) -> Node:
        """``with page.expect_file_chooser() as fc_info: <loc>.click()`` followed by ``fc_info.value.set_files(x)``."""
        if len(stmt.body) != 1 or not isinstance(stmt.body[0], ast.Expr) or not isinstance(stmt.body[0].value, ast.Call):
            raise self._fail(stmt, "a file chooser block should hold one click")
        click = stmt.body[0].value
        if not (isinstance(click.func, ast.Attribute) and click.func.attr == "click"):
            raise self._fail(stmt, "a file chooser block should hold one click")
        _, locator = self._locator(click.func.value)
        if not (isinstance(after, ast.Expr) and isinstance(after.value, ast.Call) and ast.unparse(after.value.func) == "fc_info.value.set_files" and after.value.args):
            raise self._fail(stmt, "a file chooser block should be followed by fc_info.value.set_files(...)")
        value, var = self._value(after.value.args[0])
        return Node("chooser_upload", page=page, locator=locator, value=_as_list(value), var=var, var_locked=bool(var))

    def _match(self, stmt: ast.Match) -> None:
        if not isinstance(stmt.subject, ast.Name):
            raise self._fail(stmt, "a branch should match on a variable")
        branch = Node("branch", value=stmt.subject.id, var_locked=True)
        self._add(branch)
        first = True
        for case in stmt.cases:
            if not isinstance(case.pattern, ast.MatchValue):
                raise self._fail(case.pattern, 'a branch path should be case "Option":')
            key = self._literal(case.pattern.value)
            if not first:
                self.nodes.append(Node("next_path"))
            before = len(self.nodes)
            self._statements(case.body)
            head = next((n for n in self.nodes[before:] if n.kind in ACTION_KINDS or n.kind == "branch"), None)
            if head is None:
                if self.nodes and self.nodes[-1].kind == "next_path":
                    self.nodes.pop()
                continue  # an empty path: nothing to keep
            head.option = str(key)
            first = False
        self.nodes.append(Node("end_branch"))

    def _call(self, call: ast.Call, defs: dict[str, ast.FunctionDef]) -> bool:
        func = call.func
        if isinstance(func, ast.Name) and func.id == "step" and len(call.args) == 1:
            self.nodes.append(Node("step", value=str(self._literal(call.args[0]))))
            return True
        if not isinstance(func, ast.Attribute):
            return False
        method = func.attr
        src = ast.unparse(func)
        if method in ("pause", "close") and src in ("page.pause", "context.close", "browser.close"):
            return True
        if method == "once" and isinstance(func.value, ast.Name) and call.args and self._literal(call.args[0]) == "dialog":
            self.dialog_pending.add(func.value.id)
            return True
        if method == "attempt" and len(call.args) == 2 and isinstance(call.args[1], ast.Name) and call.args[1].id in defs:
            fn = defs[call.args[1].id]
            if len(fn.body) != 2 or not isinstance(fn.body[0], ast.With):
                raise self._fail(fn, f"cannot read {fn.name}")
            self._add(self._chooser(func.value.id if isinstance(func.value, ast.Name) else "page", fn.body[0], fn.body[1]))
            return True
        if method == "goto" and isinstance(func.value, ast.Name) and call.args:
            value, var = self._value(call.args[0])
            self._add(Node("goto", page=func.value.id, value=value, var=var, var_locked=bool(var)))
            return True
        if method == "press" and isinstance(func.value, ast.Attribute) and func.value.attr == "keyboard" and isinstance(func.value.value, ast.Name):
            self._add(Node("press", page=func.value.value.id, value=self._literal(call.args[0])))
            return True
        kind = _ACTION_METHODS.get(method)
        if kind is None:
            return False
        page, locator = self._locator(func.value)
        node = Node(kind, page=page, locator=locator)
        kwargs = {k.arg: k.value for k in call.keywords}
        if kind in ("click", "dblclick"):
            node.button = self._literal(kwargs["button"]) if "button" in kwargs else "left"
            node.modifiers = list(self._literal(kwargs["modifiers"])) if "modifiers" in kwargs else []
        elif kind == "press":
            node.value = self._literal(call.args[0])
        elif kind in ("fill", "upload", "select"):
            arg = kwargs.get("label") if kind == "select" else (call.args[0] if call.args else None)
            if arg is None:
                raise self._fail(call, f"{method} needs a value")
            value, var = self._value(arg)
            node.value = _as_list(value) if kind in ("upload", "select") else value
            node.var, node.var_locked = var, bool(var)
            if var == "password":
                node.sensitive = True
        self._add(node)
        return True

    def _add(self, node: Node) -> None:
        if node.page in self.dialog_pending:
            node.dialog = "accept"
            self.dialog_pending.discard(node.page)
        self.nodes.append(node)


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def parse_code(text: str) -> Loaded:
    """Read code that :func:`render` produced (either target) back into nodes.

    Raises ValueError, naming the line, for code that is not a recorded action
    (hand-written logic cannot be replayed step by step).
    """
    return _Parser(text).parse()
