"""The validation report: one entry per check, with what happened to it.

:func:`paperpush.validate.validate_report` runs every check that applies to a
``.sub`` and records a :class:`CheckResult` for each, so the author sees not
only what failed but what passed, what could not be checked, and why. A check
ends in one of these states:

* ``passed`` -- the check ran and found nothing wrong.
* ``failed`` -- it found at least one error (blocks submission).
* ``warning`` -- it found only advisory problems.
* ``unchecked`` -- it applies, but could not run here: switched off by a flag,
  a tool or optional package is missing, or the file could not be read.
* ``needs_ai`` -- it can only be judged by reading the files the way a person
  does (text in a raster figure, a free-text guideline rule); it runs with
  ``paperpush validate --ai`` or from an agent's results (``--ai-results``).
* ``not_implemented`` -- the venue's guidelines state the rule, but paperpush
  has no check for it yet.
* ``not_applicable`` -- the venue states no such rule, or the submission has
  nothing the check would look at.

The rest of this module renders a report for the terminal (with :mod:`rich`)
and saves it as PDF, HTML, Markdown, or plain text (see :func:`save`). Colours
follow the status: green for passed, red for failed, yellow for anything not
checked; ``alt_color`` swaps in a colour-blind-safe palette (blue / vermillion
/ grey, after Okabe and Ito), and every line also carries a text marker so the
status never depends on colour alone.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .validate import Issue

logger = logging.getLogger(__name__)

PASSED = "passed"
FAILED = "failed"
WARNING = "warning"
UNCHECKED = "unchecked"
NEEDS_AI = "needs_ai"
NOT_IMPLEMENTED = "not_implemented"
NOT_APPLICABLE = "not_applicable"

STATUSES = (FAILED, WARNING, UNCHECKED, NEEDS_AI, NOT_IMPLEMENTED, PASSED, NOT_APPLICABLE)

# Section headings, in the order the report shows them.
_HEADINGS = {
    FAILED: "Failed",
    WARNING: "Warnings",
    UNCHECKED: "Not checked: could not run",
    NEEDS_AI: "Not checked: needs AI",
    NOT_IMPLEMENTED: "Not checked: not implemented",
    PASSED: "Passed",
    NOT_APPLICABLE: "Not applicable",
}
# Markers for the terminal, HTML, and Markdown, and the ASCII ones the PDF's
# standard fonts can draw.
_MARKS = {FAILED: "✖", WARNING: "▲", UNCHECKED: "?", NEEDS_AI: "?", NOT_IMPLEMENTED: "–", PASSED: "✔", NOT_APPLICABLE: "·"}
_ASCII_MARKS = {FAILED: "[FAIL]", WARNING: "[WARN]", UNCHECKED: "[SKIP]", NEEDS_AI: "[AI]", NOT_IMPLEMENTED: "[TODO]", PASSED: "[PASS]", NOT_APPLICABLE: "[N/A]"}


@dataclass(frozen=True)
class Palette:
    """Hex colours for each status (the terminal, HTML, and PDF all use these)."""

    passed: str
    failed: str
    warning: str
    unchecked: str
    not_implemented: str
    muted: str

    def for_status(self, status: str) -> str:
        return {
            PASSED: self.passed,
            FAILED: self.failed,
            WARNING: self.warning,
            UNCHECKED: self.unchecked,
            NEEDS_AI: self.unchecked,
            NOT_IMPLEMENTED: self.not_implemented,
            NOT_APPLICABLE: self.muted,
        }[status]


# Green / red / yellow, dark enough to read on white paper.
DEFAULT_PALETTE = Palette(passed="#1a7f37", failed="#cf222e", warning="#d1242f", unchecked="#b08800", not_implemented="#9a6700", muted="#6e7781")
# Okabe-Ito blue and vermillion, with grey for "not checked": distinguishable
# under the common forms of colour-blindness.
ALT_PALETTE = Palette(passed="#0072b2", failed="#d55e00", warning="#d55e00", unchecked="#666666", not_implemented="#666666", muted="#999999")


def palette(alt_color: bool = False) -> Palette:
    return ALT_PALETTE if alt_color else DEFAULT_PALETTE


@dataclass
class CheckResult:
    """What happened to one check.

    ``issues`` holds the problems a failed/warning check found (and, for an
    ``unchecked`` one, the messages saying why). ``detail`` is a one-line note:
    what a passed check measured, or why a check did not run. ``source`` is
    ``"ai"`` for a check an LLM judged.
    """

    id: str
    title: str
    category: str
    status: str
    issues: list["Issue"] = field(default_factory=list)
    detail: str = ""
    source: str = ""

    def to_dict(self) -> dict:
        out = {"id": self.id, "title": self.title, "category": self.category, "status": self.status}
        if self.detail:
            out["detail"] = self.detail
        if self.source:
            out["source"] = self.source
        if self.issues:
            out["issues"] = [{"level": i.level, "field": i.field, "message": i.message} for i in self.issues]
        return out


@dataclass
class ValidationReport:
    """Every check run on one ``.sub``, plus how the AI checks were handled."""

    venue: str
    subfile: str = ""
    results: list[CheckResult] = field(default_factory=list)
    # How the needs-AI checks were handled: "" (not run), or a description such
    # as "anthropic claude-opus-5-5" or "agent results from results.json".
    ai: str = ""
    ai_error: str = ""
    # The AI tasks (paperpush.ai_validate.AiTask) behind the needs-AI checks.
    ai_tasks: list = field(default_factory=list)

    def by_status(self, status: str) -> list[CheckResult]:
        return [r for r in self.results if r.status == status]

    def counts(self) -> dict[str, int]:
        return {s: len(self.by_status(s)) for s in STATUSES}

    def issues(self) -> list["Issue"]:
        """Every issue, in check order: the flat list ``validate`` returns."""
        return [i for r in self.results for i in r.issues]

    @property
    def errors(self) -> list["Issue"]:
        return [i for i in self.issues() if i.is_error]

    @property
    def warnings(self) -> list["Issue"]:
        return [i for i in self.issues() if not i.is_error]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "venue": self.venue,
            "subfile": self.subfile,
            "ok": self.ok,
            "ai": self.ai,
            "ai_error": self.ai_error,
            "counts": self.counts(),
            "checks": [r.to_dict() for r in self.results],
        }


# --- layout shared by every output format -------------------------------------
#
# The report is laid out once as a list of (role, text, indent, status) lines;
# each renderer only maps roles to its own styling. Roles: "title", "meta",
# "summary", "heading", "check", "item", "note", "footer".


@dataclass(frozen=True)
class _Line:
    role: str
    text: str
    indent: int = 0
    status: str = ""


def _summary_line(report: ValidationReport) -> list[tuple[str, str]]:
    """``(status, "N passed")`` pairs for the summary line under the verdict."""
    counts = report.counts()
    labels = [(PASSED, "passed"), (FAILED, "failed"), (WARNING, "with warnings"), (UNCHECKED, "could not run"), (NEEDS_AI, "need AI"), (NOT_IMPLEMENTED, "not implemented")]
    return [(status, f"{counts[status]} {label}") for status, label in labels]


def _ai_hint(report: ValidationReport) -> str:
    if report.ai_error:
        return f"AI checks could not run: {report.ai_error}"
    if report.ai:
        return f"AI checks: {report.ai}"
    return "AI checks: off. Run with --ai (and an ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY), or let an agent do them with the paperpush-validate skill (--ai-tasks / --ai-results)."


def _layout(report: ValidationReport, *, show_passed_details: bool = True) -> list[_Line]:
    lines: list[_Line] = [_Line("title", f"paperpush validation report: {Path(report.subfile).name if report.subfile else report.venue}")]
    if report.subfile:
        lines.append(_Line("meta", f"File: {report.subfile}"))
    lines.append(_Line("meta", f"Venue: {report.venue}"))
    lines.append(_Line("meta", _ai_hint(report)))
    verdict = "Ready to submit: no blocking errors." if report.ok else f"Not ready: {len(report.errors)} error(s) must be fixed before submitting."
    lines.append(_Line("summary", verdict, status=PASSED if report.ok else FAILED))
    for status in STATUSES:
        results = report.by_status(status)
        if not results:
            continue
        lines.append(_Line("heading", f"{_HEADINGS[status]} ({len(results)})", status=status))
        if status == NOT_APPLICABLE:
            lines.append(_Line("note", ", ".join(f"{r.title}" for r in results), indent=1, status=status))
            continue
        # A note every check in the section shares (e.g. how to run the AI
        # checks) is said once, under the heading.
        shared = results[0].detail if len(results) > 1 and all(r.detail == results[0].detail and not r.issues for r in results) else ""
        if shared:
            lines.append(_Line("note", shared, indent=1, status=status))
        for result in results:
            suffix = " (AI)" if result.source == "ai" else ""
            lines.append(_Line("check", f"[{result.category}] {result.title}{suffix}", indent=1, status=status))
            if result.detail and not shared and (status != PASSED or show_passed_details):
                lines.append(_Line("note", result.detail, indent=2, status=status))
            if status in (PASSED, NOT_APPLICABLE):
                continue
            for issue in result.issues:
                where = f"[{issue.field}] " if issue.field else ""
                level = "error" if issue.is_error else ("not checked" if getattr(issue, "unchecked", False) else "warning")
                item_status = FAILED if issue.is_error else (UNCHECKED if getattr(issue, "unchecked", False) else WARNING)
                if status in (UNCHECKED, NEEDS_AI, NOT_IMPLEMENTED):
                    item_status = status
                lines.append(_Line("item", f"{level}: {where}{issue.message}", indent=2, status=item_status))
    return lines


# --- terminal ----------------------------------------------------------------


def print_report(report: ValidationReport, *, alt_color: bool = False, console=None, show_passed_details: bool = True) -> None:
    """Print ``report`` to the terminal with rich colours."""
    from rich.console import Console
    from rich.text import Text

    console = console or Console()
    pal = palette(alt_color)
    if not console.is_terminal:
        # Piped or captured: keep each message on one line, so it can be grepped.
        console.soft_wrap = True
    for line in _layout(report, show_passed_details=show_passed_details):
        color = pal.for_status(line.status) if line.status else None
        pad = "  " * line.indent
        if line.role == "title":
            console.print(Text(line.text, style="bold"))
        elif line.role == "meta":
            console.print(Text(line.text, style="dim"))
        elif line.role == "summary":
            console.print()
            console.print(Text(line.text, style=f"bold {color}"))
            summary = Text()
            for i, (status, text) in enumerate(_summary_line(report)):
                if i:
                    summary.append("  ·  ", style="dim")
                summary.append(f"{_MARKS[status]} {text}", style=pal.for_status(status))
            console.print(summary)
        elif line.role == "heading":
            console.print()
            console.print(Text(line.text.upper(), style=f"bold {color} underline"))
        elif line.role == "check":
            bold = "bold " if line.status in (FAILED, WARNING) else ""
            text = Text(pad)
            text.append(f"{_MARKS[line.status]} ", style=f"bold {color}")
            text.append(line.text, style=f"{bold}{color}")
            console.print(text)
        elif line.role == "item":
            text = Text(pad + "- ", style=color)
            text.append(line.text, style=color)
            console.print(text)
        else:  # note
            console.print(Text(pad + line.text, style=f"{color} italic" if color else "italic"))


# --- files -------------------------------------------------------------------

FORMATS = (".pdf", ".html", ".htm", ".md", ".txt")


def output_path(requested: str, subfile: str) -> Path:
    """Where to save the report.

    ``requested`` empty means "next to the .sub, as PDF"; a path without one of
    the supported extensions gets ``.pdf`` added; a path naming a directory gets
    ``<subfile stem>_validation.pdf`` inside it.
    """
    stem = Path(subfile).stem or "paperpush"
    if not requested:
        return Path(subfile).with_name(f"{stem}_validation.pdf")
    path = Path(requested).expanduser()
    if path.is_dir():
        return path / f"{stem}_validation.pdf"
    if path.suffix.lower() not in FORMATS:
        return path.with_name(path.name + ".pdf")
    return path


def to_text(report: ValidationReport) -> str:
    out: list[str] = []
    for line in _layout(report):
        pad = "  " * line.indent
        if line.role == "title":
            out += [line.text, "=" * len(line.text)]
        elif line.role == "summary":
            out += ["", line.text, "  ".join(f"{_ASCII_MARKS[s]} {t}" for s, t in _summary_line(report))]
        elif line.role == "heading":
            out += ["", line.text.upper(), "-" * len(line.text)]
        elif line.role == "check":
            out.append(f"{pad}{_ASCII_MARKS[line.status]} {line.text}")
        elif line.role == "item":
            out.append(f"{pad}- {line.text}")
        else:
            out.append(f"{pad}{line.text}")
    return "\n".join(out) + "\n"


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("*", "\\*").replace("_", "\\_")


def to_markdown(report: ValidationReport) -> str:
    out: list[str] = []
    for line in _layout(report):
        if line.role == "title":
            out += [f"# {_md_escape(line.text)}", ""]
        elif line.role == "meta":
            out += [f"*{_md_escape(line.text)}*", ""]
        elif line.role == "summary":
            out += [f"**{_md_escape(line.text)}**", "", " · ".join(f"{_MARKS[s]} {t}" for s, t in _summary_line(report)), ""]
        elif line.role == "heading":
            out += ["", f"## {_MARKS[line.status]} {_md_escape(line.text)}", ""]
        elif line.role == "check":
            out.append(f"- {_MARKS[line.status]} **{_md_escape(line.text)}**")
        elif line.role == "item":
            out.append(f"    - {_md_escape(line.text)}")
        elif line.indent >= 2:
            out.append(f"    *{_md_escape(line.text)}*")
        else:
            out.append(_md_escape(line.text))
    return "\n".join(out) + "\n"


def to_html(report: ValidationReport, *, alt_color: bool = False) -> str:
    pal = palette(alt_color)
    body: list[str] = []
    open_list = False

    def close() -> None:
        nonlocal open_list
        if open_list:
            body.append("</ul>")
            open_list = False

    for line in _layout(report):
        text = html.escape(line.text)
        color = pal.for_status(line.status) if line.status else "inherit"
        if line.role == "title":
            body.append(f"<h1>{text}</h1>")
        elif line.role == "meta":
            body.append(f'<p class="meta">{text}</p>')
        elif line.role == "summary":
            chips = "".join(f'<span class="chip" style="color:{pal.for_status(s)};border-color:{pal.for_status(s)}">{_MARKS[s]} {html.escape(t)}</span>' for s, t in _summary_line(report))
            body.append(f'<p class="verdict" style="color:{color}">{text}</p><p class="chips">{chips}</p>')
        elif line.role == "heading":
            close()
            body.append(f'<h2 style="color:{color};border-color:{color}">{_MARKS[line.status]} {text}</h2><ul>')
            open_list = True
        elif line.role == "check":
            weight = "600" if line.status in (FAILED, WARNING) else "500"
            body.append(f'<li class="check" style="color:{color};font-weight:{weight}"><span class="mark">{_MARKS[line.status]}</span> {text}</li>')
        elif line.role == "item":
            body.append(f'<li class="item" style="color:{color}">{text}</li>')
        else:
            cls = "note" if line.indent >= 2 else "na"
            body.append(f'<li class="{cls}" style="color:{color}">{text}</li>')
    close()
    title = html.escape(f"paperpush validation: {report.subfile or report.venue}")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg: #ffffff; --fg: #1f2328; --muted: #59636e; }}
body {{ background: var(--bg); color: var(--fg); font: 15px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 16px; }}
h1 {{ font-size: 1.5rem; margin-bottom: .25rem; }}
h2 {{ font-size: 1.1rem; border-bottom: 2px solid; padding-bottom: .2rem; margin-top: 1.75rem; }}
.meta {{ color: var(--muted); margin: .1rem 0; }}
.verdict {{ font-weight: 700; font-size: 1.1rem; margin-top: 1rem; }}
.chip {{ display: inline-block; border: 1px solid; border-radius: 999px; padding: 0 .6rem; margin: 0 .35rem .35rem 0; font-size: .9rem; }}
ul {{ list-style: none; padding-left: 0; }}
li.check {{ margin-top: .45rem; }}
li.item {{ margin-left: 2.2rem; text-indent: -0.9rem; }}
li.item::before {{ content: "– "; }}
li.note {{ margin-left: 1.3rem; font-style: italic; }}
li.na {{ font-style: italic; }}
.mark {{ display: inline-block; width: 1.1rem; }}
</style></head><body>
{chr(10).join(body)}
</body></html>
"""


def save(report: ValidationReport, path: Path, *, alt_color: bool = False) -> Path:
    """Write ``report`` to ``path`` in the format its extension names."""
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix in (".html", ".htm"):
        path.write_text(to_html(report, alt_color=alt_color), encoding="utf-8")
    elif suffix == ".md":
        path.write_text(to_markdown(report), encoding="utf-8")
    elif suffix == ".txt":
        path.write_text(to_text(report), encoding="utf-8")
    else:
        from .pdf_report import render_pdf

        path.write_bytes(render_pdf(_layout(report), _summary_line(report), palette(alt_color), _ASCII_MARKS))
    logger.info("Saved the validation report to %s", path)
    return path
