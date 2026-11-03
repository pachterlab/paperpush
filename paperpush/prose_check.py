"""Mechanical proofreading of the manuscript's prose.

Not a grammar checker: each check here looks for one kind of slip that a
pattern can find with few false alarms, and every finding is a WARNING.

* ``prose.double_spaces`` -- two or more spaces between words. Two spaces
  after a sentence (after ``.``, ``?``, ``!`` or ``:``) are allowed.
* ``prose.repeated_words`` -- the same word twice in a row ("the the").
* ``prose.punctuation_spacing`` -- a space before ``,``, ``;``, ``:``, ``.``,
  ``?`` or ``!`` ("word ,"), or no space after a comma, semicolon, or full
  stop between words ("word,word", "end.Next").
* ``prose.brackets`` -- a paragraph whose parentheses, square brackets, or
  double quotes do not pair up.
* ``prose.spelling`` -- words on codespell's list of common misspellings
  ("teh", "recieve", "seperate"). codespell lists known typos rather than
  checking against a dictionary, so gene names, acronyms, and jargon are left
  alone. Needs the optional ``codespell`` package.

What can be checked depends on the file. A Word file, Markdown, or plain text
is read as written, so every check runs. LaTeX source is read with its math,
commands, citations, and URLs masked; double spaces are not checked there
because LaTeX does not print them. Text extracted from a PDF has spacing
artefacts of its own (justified lines, rejoined line breaks), so only repeated
words and spelling are checked in a PDF. The reference list is skipped
throughout.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import document
from .database import Field, Venue
from .validate import WARNING, Checks, Issue, NotApplicable, Outcome, Unchecked

logger = logging.getLogger(__name__)

PROSE = "Prose"
# Examples quoted per check and file; the rest are counted.
MAX_EXAMPLES = 5
# Characters of context either side of a finding.
CONTEXT = 25
# Stands in for masked math, code, URLs, and citations: not a word, a space, or punctuation.
_MASK = "\u220e"
INSTALL_HINT = "pip install codespell, or pip install 'paperpush[validate]'"

DOUBLE_SPACES = "double_spaces"
REPEATED = "repeated_words"
PUNCTUATION = "punctuation_spacing"
BRACKETS = "brackets"
SPELLING = "spelling"
ALL_CHECKS = frozenset({DOUBLE_SPACES, REPEATED, PUNCTUATION, BRACKETS, SPELLING})
PDF_CHECKS = frozenset({REPEATED, SPELLING})
TEX_CHECKS = ALL_CHECKS - {DOUBLE_SPACES}

# Words that are sometimes correctly doubled ("he said that that was", "had had").
_DOUBLING_OK = {"that", "had"}
# codespell entries of three letters or fewer are mostly abbreviations in a
# scientific paper (ser, aas, ehr, hsa, nd); keep only the unmistakable ones.
_SHORT_TYPOS = {"teh", "hte", "adn", "thn", "wih"}

# A URL, e-mail address, or DOI; trailing punctuation (a closing bracket, a full stop) is not part of it.
_URL_END = r"(?=[.,;:!?)\]'\"]*(?:\s|$))"
_URL = re.compile(
    r"(?:https?://|www\.|\bdoi:\s*|\b10\.\d{4,9}/)(?:[^\s()]|\([^\s()]*\))+?"
    + _URL_END
    + r"|\b[\w.+-]+@[\w-]+\.[\w.-]*\w|\b[\w-]+\.(?:com|org|edu|gov|net|io)\b(?:/\S*?)?"
    + _URL_END,
    re.IGNORECASE,
)


@dataclass
class _Text:
    """A file's prose, split into paragraphs, and which checks it supports."""

    path: Path
    paragraphs: list[str] = field(default_factory=list)
    checks: frozenset[str] = ALL_CHECKS
    why: str = ""  # why the file could not be read at all


# --- reading the prose -------------------------------------------------------------


def _blocks(text: str) -> list[str]:
    """Blank-line-separated blocks, each joined onto one line."""
    out = []
    for block in re.split(r"\n\s*\n", text):
        joined = " ".join(line.strip() for line in block.splitlines() if line.strip())
        if joined:
            out.append(joined)
    return out


_W_TOKEN = re.compile(r"</w:p>|<w:tab/>|<w:br\b[^>]*/>|<w:t(?:\s[^>]*)?>([^<]*)</w:t>")


def _docx_paragraphs(doc: document.Document) -> list[str] | None:
    """Each paragraph's text as typed: only ``w:t`` runs, so math and deleted text are left out."""
    xml = doc.docx_part("word/document.xml")
    if xml is None:
        return None
    paragraphs: list[str] = []
    current: list[str] = []
    for m in _W_TOKEN.finditer(xml):
        tag = m.group(0)
        if tag == "</w:p>":
            paragraphs.append("".join(current))
            current = []
        elif tag.startswith("<w:tab"):
            current.append("\t")
        elif tag.startswith("<w:br"):
            current.append("\n")
        else:
            current.append(html.unescape(m.group(1)))
    paragraphs.append("".join(current))
    text = "\n".join(paragraphs)
    from .manuscript import truncate_at_references

    text = truncate_at_references(text)
    return [p for p in (line.strip("\t") for line in text.split("\n")) if p.strip()]


def _markdown_paragraphs(text: str, is_markdown: bool) -> list[str]:
    if is_markdown:
        text = re.sub(r"(?ms)^(```|~~~).*?^\1[^\n]*$", "\n", text)  # fenced code
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
        text = re.sub(r"\$\$.*?\$\$", _MASK, text, flags=re.DOTALL)
        text = re.sub(r"(?<![\\$])\$[^$\n]+\$", _MASK, text)
        text = re.sub(r"`[^`\n]*`", _MASK, text)
        text = re.sub(r"\]\([^)\s]*\)", "]", text)  # [label](url)
        text = re.sub(r"(?m)^\s*\|.*$", "", text)  # table rows
        text = re.sub(r"(?m)^ {4,}\S.*$", "", text)  # indented code
        text = re.sub(
            r"(?m)^\s{0,3}(?:#+|[-*+]|\d+[.)]|>)\s+", "", text
        )  # headings, list markers, quotes
        text = re.sub(r"\[\^?[\w-]+\]", _MASK, text)  # footnote markers, [@citations]
    from .manuscript import truncate_at_references

    text = truncate_at_references(text)
    # A line of its own (a heading, a list item) is a paragraph of its own.
    return (
        _blocks(re.sub(r"\n(?=\s*\S[^\n]{0,60}\n\s*\n)", "\n\n", text))
        if is_markdown
        else _blocks(text)
    )


# Commands whose argument is not prose.
_TEX_DROP_ARG = r"(?:cite\w*|\w*ref|eqref|label|url|href|include\w*|input|usepackage|documentclass|bibliographystyle|bibliography|newcommand|renewcommand|providecommand|def|setlength|addtolength|vspace|hspace|begin|end|graphicspath|hypersetup|geometry|pagestyle|thispagestyle|DeclareMathOperator|si|SI|num|ang|unit|qty|ce)\*?"
_TEX_BREAK = r"\\(?:part|chapter|section|subsection|subsubsection|paragraph|subparagraph|caption|title|author|date|footnote)\b\*?"
_TEX_NO_PROSE_ENVS = r"(?:equation|align|gather|multline|eqnarray|displaymath|math|verbatim|lstlisting|minted|tabular|tabularx|longtable|tikzpicture|algorithmic|algorithm2e|filecontents|comment)\*?"


def _tex_paragraphs(source: str) -> list[str]:
    from .manuscript import _TEX_BIBLIOGRAPHY, _TEX_REFERENCE_SECTION

    text = source
    for pattern in (_TEX_BIBLIOGRAPHY, _TEX_REFERENCE_SECTION):
        m = pattern.search(text)
        if m:
            text = text[: m.start()]
    begin = re.search(r"\\begin\s*\{document\}", text)
    if begin:
        text = text[begin.end() :]
    text = re.sub(
        r"\\begin\s*\{(" + _TEX_NO_PROSE_ENVS + r")\}.*?\\end\s*\{\1\}",
        _MASK,
        text,
        flags=re.DOTALL,
    )
    text = re.sub(r"\\verb\*?(.).*?\1", _MASK, text)
    text = re.sub(r"\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)", _MASK, text, flags=re.DOTALL)
    text = re.sub(r"(?<!\\)\$(?:\\.|[^$\\])+\$", _MASK, text)
    # \cite{...}, \ref{...}, \url{...}, \href{url}{text} -> text kept, url masked.
    text = re.sub(r"\\href\s*\{[^}]*\}", "", text)
    for _ in range(2):  # \cite[p.~3][]{key} has up to two optional arguments
        text = re.sub(
            r"\\"
            + _TEX_DROP_ARG
            + r"\s*(?:\[[^\]]*\]\s*)*\{[^{}]*\}(?:\s*\{[^{}]*\})*",
            _MASK,
            text,
        )
    # A heading, caption, or item is a paragraph of its own.
    text = re.sub(
        _TEX_BREAK + r"\s*(?:\[[^\]]*\])?\s*(?:\{((?:[^{}]|\{[^{}]*\})*)\})?",
        lambda m: f"\n\n{m.group(1) or ''}\n\n",
        text,
    )
    text = re.sub(r"\\(?:item|maketitle|par)\b\s*(?:\[[^\]]*\])?|\\\\", "\n\n", text)
    text = re.sub(r"\\[,;:! ]|~", " ", text)  # thin and non-breaking spaces
    text = re.sub(r"\\([%&#_$])", r"\1", text)
    # A macro with no argument ("\\nCells{}") prints something; one with an argument ("\\emph{x}") prints that.
    text = re.sub(r"\\[a-zA-Z@]+\*?(?:\{\}|(?![a-zA-Z@*]|\s*[{\[]))", _MASK, text)
    text = re.sub(r"\\[a-zA-Z@]+\*?", "", text)
    text = re.sub(r"\\.", "", text)
    text = re.sub(r"[{}]", "", text)
    text = re.sub(_MASK + r"(?:[ \t]*" + _MASK + r")+", _MASK, text)
    return [block for block in _blocks(text) if re.search(r"[a-zA-Z]{3}", block)]


def read(path: Path) -> _Text:
    """The prose of ``path``: its paragraphs, and the checks its format supports."""
    doc = document.get(path)
    out = _Text(path)
    if doc.is_docx:
        paragraphs = _docx_paragraphs(doc)
        if paragraphs is None:
            out.why = f"could not read {path.name} as a Word file"
        out.paragraphs = paragraphs or []
        return out
    if doc.is_tex:
        if doc.tex_source is None:
            out.why = f"could not read the LaTeX source of {path.name}"
            return out
        out.paragraphs = _tex_paragraphs(doc.tex_source)
        out.checks = TEX_CHECKS
        return out
    if doc.suffix in (".md", ".markdown", ".txt", ".rst"):
        try:
            raw = path.read_text("utf-8", "replace")
        except OSError as exc:
            out.why = f"could not read {path.name} ({exc})"
            return out
        out.paragraphs = _markdown_paragraphs(raw, doc.suffix in (".md", ".markdown"))
        return out
    if doc.is_pdf:
        pages = doc.page_texts
        if pages is None:
            out.why = f"could not read the text of {path.name}"
            return out
        from .manuscript import main_text_end

        text = "\n\n\f".join(pages)
        end = main_text_end(text)
        if end:
            text = text[: end.start()]
        out.paragraphs = [p for p in text.split("\f") if p.strip()]
        out.checks = PDF_CHECKS
        return out
    out.why = f"cannot read the prose of a '{path.suffix}' file"
    return out


# --- the patterns ------------------------------------------------------------------------


def _snippet(text: str, start: int, end: int, mark=None) -> str:
    """``text[start:end]`` with some context, on one line, double spaces made visible."""
    lo = max(0, start - CONTEXT)
    hi = min(len(text), end + CONTEXT)
    if lo > 0:
        lo = text.find(" ", lo, start) + 1 or lo
    if hi < len(text):
        hi = text.rfind(" ", end, hi) if text.rfind(" ", end, hi) > end else hi
    middle = text[start:end]
    if mark:
        middle = mark(middle)
    out = text[lo:start] + middle + text[end:hi]
    out = re.sub(r"[\t\n]+", " ", out).replace(_MASK, "[…]")
    return (
        ("..." if lo > 0 else "") + out.strip(" ") + ("..." if hi < len(text) else "")
    )


def _mark(s: str) -> str:
    return f"\u00bb{s}\u00ab"


_DOUBLE_SPACE = re.compile(r"(?<=[^\s.?!:])( {2,})(?=\S)")


def double_spaces(paragraph: str) -> list[str]:
    """Two or more spaces between words, except after the end of a sentence."""
    found = []
    for m in _DOUBLE_SPACE.finditer(paragraph):
        before = paragraph[: m.start()].rstrip("\"')]\u201d\u2019")
        if before.endswith((".", "?", "!", ":")):
            continue
        found.append(
            _snippet(paragraph, m.start(), m.end(), lambda s: "\u2423" * len(s))
        )
    return found


_REPEAT = re.compile(r"(?<![\w-])([A-Za-z]+)(\s+)(\1)(?![\w-])", re.IGNORECASE)


def repeated_words(paragraph: str) -> list[str]:
    """The same word twice in a row, ignoring case. Across a line break only when the second is lower case."""
    found = []
    for m in _REPEAT.finditer(paragraph):
        word = m.group(1).lower()
        if word in _DOUBLING_OK or (len(word) == 1 and word != "a"):
            continue
        if "\n" in m.group(2) and not m.group(3)[0].islower():
            continue  # a heading followed by a sentence that starts with the same word
        found.append(_snippet(paragraph, m.start(), m.end()))
    return found


_SPACE_BEFORE = re.compile(
    r"(?<=[A-Za-z0-9)\]" + _MASK + r"\"'])( +)([,;:?!]|\.(?!\.))(?=\s|$|[\"')\]])"
)
_NO_SPACE_AFTER = re.compile(
    r"(?<![\w.])([a-z]{2,})([,;])([A-Za-z]{2,})|(?<![\w.])([a-z]{3,})(\.)([A-Z][a-z]{2,})"
)


def punctuation_spacing(paragraph: str) -> list[str]:
    """A space before a punctuation mark, or none after a comma, semicolon, or full stop between words."""
    masked = _URL.sub(lambda m: _MASK * len(m.group(0)), paragraph)
    found = []
    for m in _SPACE_BEFORE.finditer(masked):
        found.append((m.start(), _snippet(paragraph, m.start(1), m.end(2), _mark)))
    for m in _NO_SPACE_AFTER.finditer(masked):
        found.append((m.start(), _snippet(paragraph, m.start(), m.end(), _mark)))
    return [s for _, s in sorted(found)]


_ENUMERATOR = re.compile(r"(?:^|(?<=[\s(]))(?:[a-zA-Z]|[ivxIVX]{1,4}|\d{1,2})\)")
_PAIRS = {")": "(", "]": "["}


def unbalanced(paragraph: str) -> list[str]:
    """Parentheses, square brackets, or double quotes in ``paragraph`` that do not pair up."""
    text = _URL.sub(lambda m: _MASK * len(m.group(0)), paragraph)
    # "a) ... b) ..." enumerations and "(i)" are not unbalanced; blank the lone closer.
    if text.count("(") != text.count(")"):
        text = _ENUMERATOR.sub(
            lambda m: (
                m.group(0)[:-1] + " "
                if "(" not in text[max(0, m.start() - 1) : m.start() + 1]
                else m.group(0)
            ),
            text,
        )
    found = []
    stack: list[int] = []
    for i, ch in enumerate(text):
        if ch in "([":
            stack.append(i)
        elif ch in ")]":
            if stack and text[stack[-1]] == _PAIRS[ch]:
                stack.pop()
            else:
                found.append(
                    _snippet(paragraph, i, i + 1, _mark) + f" (unmatched '{ch}')"
                )
                break
    else:
        if stack:
            i = stack[0]
            found.append(
                _snippet(paragraph, i, i + 1, _mark) + f" (unclosed '{text[i]}')"
            )
    opening, closing = text.count("\u201c"), text.count("\u201d")
    tex_open, tex_close = text.count("``"), text.count("''")
    if opening != closing:
        found.append(
            _snippet(paragraph, 0, 0)
            + f" ({opening} opening and {closing} closing curly double quotes)"
        )
    elif tex_open and tex_open != tex_close:
        found.append(
            _snippet(paragraph, 0, 0) + f" ({tex_open} `` and {tex_close} '' quotes)"
        )
    elif text.count('"') % 2:
        i = text.rfind('"')
        found.append(
            _snippet(paragraph, i, i + 1, _mark)
            + " (odd number of straight double quotes)"
        )
    return found


# --- spelling (codespell) --------------------------------------------------------------


@lru_cache(maxsize=1)
def misspellings() -> dict[str, list[str]] | None:
    """codespell's main list of misspellings, word -> suggested fixes; None without codespell."""
    try:
        import codespell_lib
    except ImportError:
        return None
    path = Path(codespell_lib.__file__).parent / "data" / "dictionary.txt"
    table: dict[str, list[str]] = {}
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError as exc:
        logger.debug("could not read codespell's dictionary %s: %s", path, exc)
        return None
    for line in lines:
        wrong, sep, fixes = line.partition("->")
        if not sep:
            continue
        fixes = fixes.strip()
        if "," in fixes and not fixes.endswith(","):
            fixes = fixes.rsplit(",", 1)[
                0
            ]  # "wrong->right, reason the fix is not automatic"
        table[wrong.strip().lower()] = [
            f.strip() for f in fixes.split(",") if f.strip()
        ]
    return table


_WORD = re.compile(r"(?<![\w\-/.@'\u2019])[A-Za-z]+(?:['\u2019][a-z]+)?(?![\w\-/@])")


def spelling(paragraph: str, table: dict[str, list[str]]) -> list[str]:
    """Words of ``paragraph`` on codespell's list of misspellings, with its suggestions."""
    masked = _URL.sub(lambda m: _MASK * len(m.group(0)), paragraph)
    found = []
    for m in _WORD.finditer(masked):
        word = m.group(0)
        if not word.islower():
            continue  # a name, an abbreviation ("Proc. R. Soc. Lond."), an acronym, or a gene
        lower = word.lower()
        if len(lower) <= 3 and lower not in _SHORT_TYPOS:
            continue
        fixes = table.get(lower)
        if fixes:
            found.append(
                f"'{word}' (did you mean {' or '.join(repr(f) for f in fixes[:3])}?)"
            )
    return found


# --- the checks --------------------------------------------------------------------------


_CHECKS = {
    DOUBLE_SPACES: (
        "Double spaces between words",
        "double space(s) mid-sentence",
        double_spaces,
    ),
    REPEATED: ("Repeated words", "repeated word(s)", repeated_words),
    PUNCTUATION: (
        "Spacing around punctuation",
        "spacing problem(s) around punctuation",
        punctuation_spacing,
    ),
    BRACKETS: (
        "Brackets and quotes pair up",
        "unbalanced bracket(s) or quote(s)",
        unbalanced,
    ),
    SPELLING: ("Common misspellings (codespell)", "common misspelling(s)", None),
}
_SKIPPED = {
    DOUBLE_SPACES: {
        ".tex": "LaTeX does not print repeated spaces",
        ".pdf": "text extracted from a PDF has spacing artefacts",
    },
    PUNCTUATION: {".pdf": "text extracted from a PDF has spacing artefacts"},
    BRACKETS: {".pdf": "a PDF's text has no paragraphs to pair brackets within"},
}


def _manuscripts(venue: Venue, values: dict[str, str]) -> list[tuple[Field, _Text]]:
    from .requirements_check import manuscript_files

    files = manuscript_files(venue, values)
    if not files:
        raise NotApplicable("no manuscript file to read")
    return [(f, read(p)) for f, p in files]


def _check(name: str, venue: Venue, values: dict[str, str]) -> Outcome:
    title, noun, fn = _CHECKS[name]
    if name == SPELLING:
        table = misspellings()
        if table is None:
            raise Unchecked(f"needs codespell ({INSTALL_HINT})")
        fn = lambda p: spelling(p, table)  # noqa: E731
    issues: list[Issue] = []
    details: list[str] = []
    skipped: list[str] = []
    files = _manuscripts(venue, values)
    for fld, text in files:
        if text.why:
            issues.append(Issue(WARNING, fld.id, text.why, unchecked=True))
            continue
        if name not in text.checks:
            kind = ".tex" if text.checks == TEX_CHECKS else ".pdf"
            skipped.append(f"{text.path.name}: {_SKIPPED[name][kind]}")
            continue
        found = [hit for paragraph in text.paragraphs for hit in fn(paragraph)]
        if name == SPELLING:
            found = list(dict.fromkeys(found))  # each misspelt word once
        if found:
            shown = "; ".join(
                f'"{s}"' if name != SPELLING else s for s in found[:MAX_EXAMPLES]
            )
            more = (
                f" (and {len(found) - MAX_EXAMPLES} more)"
                if len(found) > MAX_EXAMPLES
                else ""
            )
            issues.append(
                Issue(
                    WARNING,
                    fld.id,
                    f"{text.path.name}: {len(found)} {noun}: {shown}{more}",
                )
            )
        else:
            details.append(
                f"none in {text.path.name}"
                if len(files) > 1
                else f"none found ({len(text.paragraphs)} paragraphs read)"
            )
    if skipped and not details and not issues:
        raise Unchecked("; ".join(skipped))
    details.extend(f"not checked in {s}" for s in skipped)
    return Outcome(issues, "; ".join(details))


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    logger.info("Proofreading the %s manuscript's prose", venue.slug)
    for name, (title, _, _) in _CHECKS.items():
        checks.run(f"prose.{name}", title, PROSE, _check, name, venue, values)
