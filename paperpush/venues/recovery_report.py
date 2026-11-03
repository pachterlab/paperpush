"""Turn a submission run's recovery log into a bug report for the venue script.

When a step of ``paperpush submit`` fails and an agent (or ``--ai``) fixes it
in the browser, :class:`~paperpush.venues.recovery_channel.PageTools` logs what
failed and what worked to ``recoveries.jsonl`` in the run's recovery
directory. On a ``pip`` install nobody else ever sees that log, so the next
person hits the same broken step. This module drafts a GitHub issue from it:

* :func:`write_run_info` records which venue and paperpush version the run used
  (``run.json``), so a report can say so without the ``.sub``.
* :func:`build_report` reads the log and returns a title, a Markdown body, and a
  prefilled ``issues/new`` URL. It never sends anything: the author reads the
  draft and decides whether to file it.

The draft is redacted before the author sees it, because a recovery log is
full of manuscript data: values the agent typed are replaced by their length,
uploaded paths by their file name, anything that matches a ``.sub`` value by the
field it came from, e-mail addresses, the home directory, and URL query strings
(ScholarOne carries session tokens there). Screenshots are never included; the
report lists where they are so the author can attach one they are happy to
share.
"""

from __future__ import annotations

import json
import platform
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urlsplit, urlunsplit

from .. import __version__
from .recovery_channel import LOG_FILE, read_log

RUN_FILE = "run.json"
REPORT_FILE = "recovery-report.md"
ISSUES_URL = "https://github.com/pachterlab/paperpush/issues/new"
ISSUE_LABEL = "venue-recovery"
#: GitHub rejects very long issue URLs; past this the body in the URL is cut
#: and the author pastes the full report from the saved file instead.
MAX_URL_LENGTH = 7500

#: How each resolution reads in a report.
_OUTCOMES = {"resolved": "fixed, run continued", "retry": "step retried", "abort": "run stopped"}
#: Agent actions whose ``value`` is data the author typed, never the portal's.
_TYPED_OPS = frozenset({"fill"})
#: Script actions (as logged, ``name(args)``) whose arguments are typed data.
_TYPED_CALLS = ("fill(", "type(", "press_sequentially(")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# A quoted absolute path, as _format_call writes set_input_files('/a/b.pdf').
_PATH_RE = re.compile(r"""(['"])(/[^'"]+|[A-Za-z]:\\[^'"]+)\1""")
# An unquoted absolute path in free text (an agent's note). The look-behind
# keeps URLs ("https://host/a/b") and relative paths intact.
_BARE_PATH_RE = re.compile(r"(?<![\w:/.~])(/(?:[\w.@+-]+/)+[\w.@+-]+)")
#: Shortest .sub value worth masking; shorter ones ("yes", "no", "1") would
#: blank out ordinary words in selectors.
_MIN_SECRET_LEN = 5


@dataclass
class RecoveryReport:
    """A drafted issue: nothing in it has been sent anywhere."""

    venue: str
    title: str
    body: str
    url: str
    entries: int
    screenshots: list[str] = field(default_factory=list)
    saved_to: str | None = None
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "title": self.title,
            "body": self.body,
            "issue_url": self.url,
            "entries": self.entries,
            "screenshots": self.screenshots,
            "saved_to": self.saved_to,
            "body_truncated_in_url": self.truncated,
        }


def write_run_info(directory: str | Path, venue: str, base_slug: str, subfile: str | Path | None = None) -> None:
    """Record the run's venue, paperpush version, and .sub path in ``run.json``.

    The .sub path is kept only so :func:`build_report` can mask its values; it
    is never written into a report.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    info = {
        "venue": venue,
        "base": base_slug,
        "paperpush_version": __version__,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "subfile": str(Path(subfile).resolve()) if subfile else None,
    }
    (directory / RUN_FILE).write_text(json.dumps(info, indent=2), encoding="utf-8")


def read_run_info(directory: str | Path) -> dict[str, Any]:
    try:
        return json.loads((Path(directory) / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def runs_root() -> Path:
    """Where ``submit`` puts a run's recovery directory when none is given."""
    from .. import credentials

    return credentials.config_dir() / "submit-runs"


def latest_run_with_recoveries(root: str | Path | None = None) -> Path | None:
    """The most recently changed run directory that has a recovery log."""
    root = Path(root) if root else runs_root()
    candidates = [p for p in root.glob("*/") if (p / LOG_FILE).is_file()] if root.is_dir() else []
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p / LOG_FILE).stat().st_mtime)


def build_report(directory: str | Path, *, sub_values: dict[str, str] | None = None, save: bool = True) -> RecoveryReport:
    """Draft an issue from the recovery log in ``directory``.

    ``sub_values`` (the run's parsed ``.sub``) are masked wherever they appear;
    when omitted they are read from the ``.sub`` named in ``run.json``, if it is
    still there. Raises :class:`FileNotFoundError` when the run logged nothing.
    """
    directory = Path(directory)
    entries = read_log(directory)
    if not entries:
        raise FileNotFoundError(f"no recoveries were logged in {directory} ({LOG_FILE} is missing or empty)")
    info = read_run_info(directory)
    venue = info.get("venue") or _venue_from_dirname(directory)
    if sub_values is None:
        sub_values = _load_sub_values(info.get("subfile"))
    masker = _Masker(sub_values or {})

    sections = [_entry_markdown(i, entry, masker) for i, entry in enumerate(entries, 1)]
    steps = []
    for entry in entries:
        label = entry.get("step") or "(unlabelled step)"
        if label not in steps:
            steps.append(label)
    title = f"[{venue}] submit needed recovery: {steps[0]}" + (f" (+{len(steps) - 1} more step{'s' if len(steps) > 2 else ''})" if len(steps) > 1 else "")

    header = (
        f"**Venue:** `{venue}`  \n"
        f"**paperpush:** {info.get('paperpush_version') or __version__}  \n"
        f"**Python / OS:** {platform.python_version()} / {platform.system()} {platform.release()}  \n"
        f"**Recoveries:** {len(entries)}\n\n"
        "A step of `paperpush submit` failed and was fixed in the browser during the run. "
        "Below is what failed and what worked instead, so the venue script can be updated.\n\n"
        "<!-- Redacted by paperpush: typed values, file paths, e-mails, .sub values, and URL query strings. "
        "Please read it before filing and remove anything else you do not want to share. -->\n"
    )
    screenshots = sorted(str(p) for p in directory.glob("screenshot-*.png"))
    footer = "\n---\n_Drafted by `paperpush recovery-report`. Screenshots were not included; they show the author's manuscript._\n"
    body = header + "\n".join(sections) + footer

    saved_to = None
    if save:
        path = directory / REPORT_FILE
        path.write_text(f"# {title}\n\n{body}", encoding="utf-8")
        saved_to = str(path)
    url, truncated = _issue_url(title, body, saved_to)
    return RecoveryReport(venue=venue, title=title, body=body, url=url, entries=len(entries), screenshots=screenshots, saved_to=saved_to, truncated=truncated)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Masker:
    """Replace personal data in text with a placeholder naming where it came from."""

    def __init__(self, sub_values: dict[str, str]):
        pairs: list[tuple[str, str]] = []
        for field_id, raw in sub_values.items():
            for piece in _pieces(raw):
                pairs.append((piece, f"<{field_id}>"))
        # Longest first, so "Joseph M. Rich" is masked before "Joseph".
        self._pairs = sorted(set(pairs), key=lambda p: -len(p[0]))
        self._home = str(Path.home())

    def __call__(self, text: Any) -> str:
        text = "" if text is None else str(text)
        for secret, placeholder in self._pairs:
            if secret in text:
                text = text.replace(secret, placeholder)
        text = _PATH_RE.sub(lambda m: f"{m.group(1)}{_basename(m.group(2))}{m.group(1)}", text)
        text = _BARE_PATH_RE.sub(lambda m: _basename(m.group(1)), text)
        text = _EMAIL_RE.sub("<email>", text)
        if self._home and self._home != "/":
            text = text.replace(self._home, "~")
        return text


def _pieces(raw: str) -> Iterable[str]:
    """The maskable parts of one .sub value: each line, and each ``|`` column."""
    for line in str(raw or "").splitlines():
        line = line.strip()
        if len(line) >= _MIN_SECRET_LEN:
            yield line
        for cell in line.split("|"):
            cell = cell.strip()
            if len(cell) >= _MIN_SECRET_LEN:
                yield cell


def _basename(path: str) -> str:
    return re.split(r"[\\/]", path.rstrip("\\/"))[-1]


def _strip_query(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _redact_call(action: str, mask: _Masker) -> str:
    """A logged script call (``fill('x')``) with typed arguments removed."""
    for prefix in _TYPED_CALLS:
        if action.startswith(prefix):
            return f"{prefix}<typed value>)"
    return mask(action)


def _redact_agent_action(action: dict[str, Any], mask: _Masker) -> dict[str, Any]:
    out: dict[str, Any] = {"op": action.get("op")}
    if "target" in action:
        target = action["target"]
        out["target"] = {k: (mask(v) if isinstance(v, str) else v) for k, v in target.items()} if isinstance(target, dict) else mask(target)
    if "value" in action:
        value = action["value"]
        op = action.get("op")
        if op in _TYPED_OPS:
            out["value"] = f"<{len(str(value))} characters typed>"
        elif op == "upload":
            out["value"] = _basename(str(value))
        elif op == "wait":
            out["value"] = value
        elif op == "press" and len(str(value)) == 1:
            out["value"] = "<character key>"  # the last letter of something typed, not a named key
        else:
            out["value"] = mask(value)
    return out


def _entry_markdown(index: int, entry: dict[str, Any], mask: _Masker) -> str:
    action = _redact_call(str(entry.get("action") or ""), mask)
    target = mask(entry.get("target") or "")
    worked = [_redact_agent_action(a, mask) for a in entry.get("agent_actions") or []]
    handler = entry.get("handler") or "agent"
    if entry.get("model"):
        handler += f" ({entry.get('provider')}/{entry.get('model')})"
    lines = [
        f"### {index}. {mask(entry.get('step') or '(unlabelled step)')}",
        "",
        f"- **Failed:** `{target + '.' if target else ''}{action}`",
        f"- **Error:** `{mask(entry.get('error') or '')}`",
        f"- **Page:** {_strip_query(str(entry.get('url') or ''))}",
        f"- **Handled by:** {handler}; outcome: **{_OUTCOMES.get(str(entry.get('resolution')), entry.get('resolution'))}**",
    ]
    note = mask(entry.get("note") or "")
    if note:
        lines.append(f"- **Note:** {note}")
    if worked:
        lines += ["", "What worked:", "", "```json", *(json.dumps(a) for a in worked), "```"]
    elif entry.get("resolution") == "abort":
        lines += ["", "_Not fixed: the run stopped here._"]
    elif entry.get("handler") == "by-hand":
        lines += ["", "_Fixed by hand in the browser; the clicks are not recorded" + ("; see the note above._" if note else ", and no note was left._")]
    else:
        lines += ["", "_Nothing was recorded: the page was fixed outside the recovery tools._"]
    return "\n".join(lines) + "\n"


def _issue_url(title: str, body: str, saved_to: str | None) -> tuple[str, bool]:
    def make(text: str) -> str:
        return f"{ISSUES_URL}?{urlencode({'title': title, 'labels': ISSUE_LABEL, 'body': text})}"

    url = make(body)
    if len(url) <= MAX_URL_LENGTH:
        return url, False
    note = "\n\n_(Cut to fit the link: paste the full report from " + (f"`{_basename(saved_to)}` in the run's recovery directory" if saved_to else "the saved report") + ".)_"
    lo, hi = 0, len(body)
    while lo < hi:  # longest prefix of the body that still fits
        mid = (lo + hi + 1) // 2
        if len(make(body[:mid] + note)) <= MAX_URL_LENGTH:
            lo = mid
        else:
            hi = mid - 1
    return make(body[:lo] + note), True


def _load_sub_values(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    try:
        from ..subfile import parse

        return parse(Path(path).read_text(encoding="utf-8")).values
    except (OSError, ValueError):
        return {}


def _venue_from_dirname(directory: Path) -> str:
    # _default_recover_dir names runs "<venue>-YYYYmmdd-HHMMSS-ffffff".
    match = re.match(r"(.+?)-\d{8}-\d{6}", directory.name)
    return match.group(1) if match else "unknown"
