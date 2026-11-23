"""A PDF older than the source it was made from: ``integrity.stale_pdf``.

Authors edit ``main.tex`` (or ``manuscript.docx``, ``paper.md``) and forget to
rebuild ``main.pdf`` before uploading it, so the editor reads last week's
draft. For each uploaded PDF this check looks beside it for a source file with
the same name (``.tex``, ``.docx``, ``.md``, ``.markdown``, ``.Rmd``,
``.qmd``) and asks two questions:

* **Text.** Does every prose sentence of the source appear in the PDF? The
  source is reduced to plain text without compiling it, and sentences that
  carry math, code, or markup leftovers are skipped, since their rendered form
  cannot be predicted. A sentence counts as present when most of its
  three-word chunks appear in the PDF's text with spacing ignored (PDF
  extraction often runs words together). This survives copying, which resets
  modification times.
* **Time.** Was the source (or a file it ``\\input``\\s) saved after the PDF?

A sentence missing from the PDF is a WARNING when the source is also newer, or
when two or more are missing. A newer source whose text could not be compared
(a scanned PDF, an unreadable source) is a WARNING on the time alone. A newer
source whose text all appears in the PDF passes, with a note: the edit did not
change the prose (a comment, a macro, a figure path).

Makes no network requests and never compiles anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .database import Venue
from .validate import WARNING, Issue, NotApplicable, Outcome

SOURCE_SUFFIXES = (".tex", ".docx", ".md", ".markdown", ".Rmd", ".rmd", ".qmd")
# Saved this much after the PDF before the source counts as newer (filesystem timestamp slack).
MTIME_SLACK_SECONDS = 2.0
# A source sentence is present when at least this share of its three-word chunks is in the PDF.
PRESENT_SHARE = 0.34
MIN_SENTENCE_TOKENS = 10
MAX_QUOTED = 3

# Leftovers of math or markup in the plain text of a source: a run of blanks where commands
# were dropped, operators, or three single characters in a row (variables of an inline formula).
_NOT_PROSE = re.compile(r"   |[=<>^_\\|$`*#~]|\b\w\b\s+\b\w\b\s+\b\w\b")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n\s*\n")
_TEX_INPUT = re.compile(r"\\(?:input|include|subfile)\s*\{\s*([^{}]+?)\s*\}")


@dataclass
class Comparison:
    """How a source's prose sentences fared against the PDF's text."""

    compared: int
    missing: list[str]


def find_source(pdf: Path) -> Path | None:
    """The source file beside ``pdf`` with the same name, or None."""
    for suffix in SOURCE_SUFFIXES:
        candidate = pdf.with_suffix(suffix)
        if candidate.is_file() and candidate != pdf:
            return candidate
    return None


def source_mtime(source: Path) -> float:
    """When ``source`` last changed: for LaTeX, the newest of it and the files it ``\\input``\\s."""
    newest = source.stat().st_mtime
    if source.suffix.lower() != ".tex":
        return newest
    seen = {source.resolve()}
    queue = [source]
    while queue and len(seen) < 64:
        current = queue.pop()
        try:
            text = current.read_text("utf-8", "replace")
        except OSError:
            continue
        text = re.sub(r"(?<!\\)%.*", "", text)
        for match in _TEX_INPUT.finditer(text):
            for name in (match.group(1), match.group(1) + ".tex"):
                target = (source.parent / name).resolve()
                if target.is_file() and target not in seen:
                    seen.add(target)
                    queue.append(target)
                    newest = max(newest, target.stat().st_mtime)
                    break
    return newest


def source_text(source: Path) -> str | None:
    """The plain text of ``source``, without compiling it; None if unreadable."""
    from . import manuscript

    suffix = source.suffix.lower()
    if suffix == ".tex":
        raw = manuscript._raw_tex_source(source)
        return None if raw is None else manuscript._tex_to_text(raw)
    if suffix == ".docx":
        return manuscript.docx_to_text(source)
    return manuscript.read_markdown(source)


def prose_sentences(text: str) -> list[tuple[str, list[str]]]:
    """``(sentence, tokens)`` for each sentence of ``text`` that is plain prose."""
    from .document import normalize, tokens

    out = []
    for sentence in _SENTENCE_BREAK.split(normalize(text)):
        if _NOT_PROSE.search(sentence):
            continue
        toks = tokens(sentence)
        words = [t for t in toks if t.isalpha() and len(t) > 2]
        if len(toks) >= MIN_SENTENCE_TOKENS and len(words) >= 0.7 * len(toks):
            out.append((" ".join(sentence.split()), toks))
    return out


def compare(source: str, pdf_text: str) -> Comparison:
    """Which prose sentences of ``source`` do not appear in ``pdf_text``."""
    from .document import tokens

    squashed = "".join(tokens(pdf_text))
    sentences = prose_sentences(source)
    missing = []
    for sentence, toks in sentences:
        chunks = ["".join(toks[i : i + 3]) for i in range(0, len(toks) - 2, 3)]
        if sum(chunk in squashed for chunk in chunks) < PRESENT_SHARE * len(chunks):
            missing.append(sentence)
    return Comparison(len(sentences), missing)


def _uploaded_pdfs(venue: Venue, values: dict[str, str]) -> list[tuple[str, Path]]:
    from .validate import _usable_path

    found: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for field in venue.fields:
        raw = values.get(field.id, "")
        if field.type == "file":
            candidates = [raw.strip()]
        elif field.type == "filelist":
            candidates = [line.split("|", 1)[0].strip() for line in raw.splitlines()]
        else:
            continue
        for candidate in candidates:
            path = _usable_path(candidate) if candidate else None
            if path is not None and path.is_file() and path.suffix.lower() == ".pdf" and path.resolve() not in seen:
                seen.add(path.resolve())
                found.append((field.id, path))
    return found


def _quote(sentence: str, limit: int = 90) -> str:
    return f'"{sentence[:limit].rstrip()}{"..." if len(sentence) > limit else ""}"'


def _age(seconds: float) -> str:
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'s' if n != 1 else ''}"
    return f"{int(seconds)} seconds"


def check(venue: Venue, values: dict[str, str]) -> Outcome:
    """Compare each uploaded PDF with the same-named source file beside it."""
    from .document import get

    pdfs = _uploaded_pdfs(venue, values)
    if not pdfs:
        raise NotApplicable("no PDF among the uploads")
    pairs = [(field_id, pdf, source) for field_id, pdf in pdfs if (source := find_source(pdf)) is not None]
    if not pairs:
        raise NotApplicable(f"no source file ({', '.join(dict.fromkeys(s.lower() for s in SOURCE_SUFFIXES))}) with the same name beside any uploaded PDF")
    issues: list[Issue] = []
    notes: list[str] = []
    for field_id, pdf, source in pairs:
        newer_by = source_mtime(source) - pdf.stat().st_mtime
        newer = newer_by > MTIME_SLACK_SECONDS
        page_texts = get(pdf).page_texts
        text = source_text(source)
        result = compare(text, "\n".join(page_texts)) if page_texts and text else None
        when = f"{source.name} was saved {_age(newer_by)} after {pdf.name}"
        if result is None or result.compared == 0:
            why = "the PDF's text could not be read" if not page_texts else f"{source.name} has no plain prose to compare" if text else f"{source.name} could not be read"
            if newer:
                issues.append(Issue(WARNING, field_id, f"{pdf.name} may be out of date: {when} ({why}, so only the file times were compared). Rebuild the PDF from {source.name} before uploading."))
            else:
                notes.append(f"{pdf.name}: not newer than {source.name}; {why}")
            continue
        if result.missing and (newer or len(result.missing) >= 2):
            quoted = tuple(_quote(s) for s in result.missing[:MAX_QUOTED])
            shown = "; ".join(quoted)
            more = f" and {len(result.missing) - MAX_QUOTED} more" if len(result.missing) > MAX_QUOTED else ""
            lead = f"{when}, and " if newer else ""
            verb = "is" if len(result.missing) == 1 else "are"
            issues.append(Issue(WARNING, field_id, f"{pdf.name} looks out of date with {source.name}: {lead}{len(result.missing)} of its {result.compared} prose sentences {verb} not in the PDF ({shown}{more}). Rebuild the PDF from {source.name} before uploading.", items=quoted))
        elif newer:
            notes.append(f"{when}, but all {result.compared} of its prose sentences appear in the PDF")
        else:
            notes.append(f"{pdf.name} matches {source.name} ({result.compared} prose sentences compared)")
    return Outcome(issues, "; ".join(notes))
