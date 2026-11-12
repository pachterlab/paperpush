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
* ``prose.repetition`` -- repetitive word choice, counted the way
  `word_counter <https://github.com/josephrich98/word_counter>`_ does (case
  folded, stop words dropped, inflections grouped): a stock word ("however",
  "notably", "novel", "robust", ...) used at least 5 times and 2.5 times per
  1,000 words; a word outside the title, abstract, and keywords that makes up
  2.5% of the content words (25 uses or more); the same content word (again
  outside the title, abstract, and keywords) three times in one sentence; and
  four or more sentences in a row that open with the same word. The passing note lists the most frequent words.
* ``prose.spelling`` -- words on codespell's list of common misspellings
  ("teh", "recieve", "seperate"). codespell lists known typos rather than
  checking against a dictionary, so gene names, acronyms, and jargon are left
  alone. Needs the optional ``codespell`` package.

Numbers, abbreviation conventions, and acronyms are checked by
:mod:`paperpush.style_check` and reported here too (``prose.numbers``,
``prose.conventions``, ``prose.acronyms``), and the AI task ``ai.prose``
(spelling and typos beyond codespell's list, grammar, and tense) is queued.

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
import math
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
_DOUBLING_OK = {"that", "had", "log", "ln"}  # "a log log plot"
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
    out = _read(path)
    if not out.why and not out.paragraphs:  # a scanned PDF has no text to check, which is not the same as no problems
        out.why = f"found no text in {path.name} (is it scanned, or empty?)"
    return out


def _read(path: Path) -> _Text:
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


def _table_cells(paragraph: str, m: re.Match[str]) -> bool:
    """Whether the repeat ``m`` in a PDF's text reads as neighbouring table cells ("NA NA 34", "Female Female")."""
    if m.group(3)[0].isupper():
        return True
    if m.group(1).lower() in _STOP_WORDS:
        return False
    return bool(re.match(r"\s+[\d.,]+(?:\s|$)", paragraph[m.end() :]) or re.search(r"(?:^|\s)[\d.,]+\s+$", paragraph[: m.start()]))


def repeated_words(paragraph: str, *, pdf: bool = False) -> list[str]:
    """The same word twice in a row, ignoring case. Across a line break only when the second is lower case."""
    found = []
    for m in _REPEAT.finditer(paragraph):
        word = m.group(1).lower()
        if word in _DOUBLING_OK or (len(word) == 1 and word != "a"):
            continue
        if "\n" in m.group(2) and not m.group(3)[0].islower():
            continue  # a heading followed by a sentence that starts with the same word
        if pdf and _table_cells(paragraph, m):
            continue
        found.append(_snippet(paragraph, m.start(), m.end()))
    return found


_SPACE_BEFORE = re.compile(
    r"(?<=[A-Za-z0-9)\]" + _MASK + r"\"'])( +)([,;:?!]|\.(?!\.))(?=\s|$|[\"')\]])"
)
_NO_SPACE_AFTER = re.compile(
    r"(?<![\w.])([a-z]{2,})([,;])([A-Za-z]{2,})|(?<![\w.])([a-z]{3,})(\.)([A-Z][a-z]{2,})(?![a-z]*[A-Z])"
)


# A module name before a dot is code ("torch.Tensor"), not two sentences run together.
_CODE_MODULES = frozenset("torch numpy pandas scipy sklearn scanpy anndata keras tensorflow matplotlib seaborn jax networkx biopython".split())


def punctuation_spacing(paragraph: str) -> list[str]:
    """A space before a punctuation mark, or none after a comma, semicolon, or full stop between words."""
    masked = _URL.sub(lambda m: _MASK * len(m.group(0)), paragraph)
    found = []
    for m in _SPACE_BEFORE.finditer(masked):
        if m.group(2) == ":" and masked[m.start() - 1].isdigit() and re.match(r"\s*\d", masked[m.end() :]):
            continue  # a ratio set with spaces ("1 : 100")
        found.append((m.start(), _snippet(paragraph, m.start(1), m.end(2), _mark)))
    for m in _NO_SPACE_AFTER.finditer(masked):
        if m.group(4) in _CODE_MODULES:
            continue
        found.append((m.start(), _snippet(paragraph, m.start(), m.end(), _mark)))
    return [s for _, s in sorted(found)]


_ENUMERATOR = re.compile(r"(?:^|(?<=[\s(]))(?:[a-zA-Z]|[ivxIVX]{1,4}|\d{1,2})\)")
_PAIRS = {")": "(", "]": "["}
# A half-open interval: "[0, 1)", "(0, 1]".
_NUM_END = r"\s*[-\u2212]?(?:\d+(?:\.\d+)?|\.\d+|\u221e|inf\w*)\s*"
_HALF_OPEN = re.compile(rf"\[{_NUM_END},{_NUM_END}\)|\({_NUM_END},{_NUM_END}\]", re.IGNORECASE)


def unbalanced(paragraph: str) -> list[str]:
    """Parentheses, square brackets, or double quotes in ``paragraph`` that do not pair up."""
    text = _URL.sub(lambda m: _MASK * len(m.group(0)), paragraph)
    text = _HALF_OPEN.sub(lambda m: _MASK * len(m.group(0)), text)
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
    elif text.count('"') % 2 and (text.count('"') - len(re.findall(r'(?<=\d)"', text))) % 2:  # 3" is inches
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
        "No double spaces between words",
        "double space(s) mid-sentence",
        double_spaces,
    ),
    REPEATED: ("No accidentally doubled words (e.g., 'the the')", "repeated word(s)", repeated_words),
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
        check = (lambda p: repeated_words(p, pdf=True)) if name == REPEATED and text.checks == PDF_CHECKS else fn  # noqa: E731
        found = [hit for paragraph in text.paragraphs for hit in check(paragraph)]
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


# --- repetitive word choice -------------------------------------------------------------
#
# After josephrich98/word_counter: lower-case the words, drop stop words, fold
# inflections together, and count. Rather than NLTK's lemmatizer (which needs
# corpora downloaded at run time) a light suffix-stripper groups "result",
# "results", "resulted", and "resulting"; the most common spelling names the group.

REPETITION = "repetition"
# NLTK's English stop-word list.
_STOP_WORDS = frozenset(
    """i me my myself we our ours ourselves you your yours yourself yourselves he him his himself she her hers herself it its itself
    they them their theirs themselves what which who whom this that these those am is are was were be been being have has had having
    do does did doing a an the and but if or because as until while of at by for with about against between into through during before
    after above below to from up down in out on off over under again further then once here there when where why how all any both each
    few more most other some such no nor not only own same so than too very s t can will just don should now d ll m o re ve y ain aren
    couldn didn doesn hadn hasn haven isn ma mightn mustn needn shan shouldn wasn weren won wouldn also may might could would one two
    using used use however within without among whether thus""".split()
)
def _inflections(base: str) -> set[str]:
    """``base`` with the regular plural and verb endings ("enhance" -> "enhances", "enhanced", "enhancing")."""
    if base.endswith("e"):
        return {base, base + "s", base + "d", base[:-1] + "ing"}
    if base.endswith("y") and base[-2] not in "aeiou":
        return {base, base[:-1] + "ies", base[:-1] + "ied", base + "ing"}
    if base.endswith(("s", "x", "ch", "sh")):
        return {base, base + "es", base + "ed", base + "ing"}
    return {base, base + "s", base + "ed", base + "ing"}


def _word_groups(spec: str) -> dict[str, str]:
    """Each form in ``spec`` mapped to the name it is reported under.

    ``spec`` is comma-separated groups. A group's first word names it and is
    inflected; any further words are irregular forms ("shed shedding",
    "meticulous meticulously").
    """
    out: dict[str, str] = {}
    for group in spec.split(","):
        name, *extra = group.split()
        for form in _inflections(name) | set(extra):
            out[form] = name
    return out


# Stock words: transitions, intensifiers, and the stock verbs and nouns of academic prose, which read as filler when
# leaned on. Reported at _STYLE_MIN_COUNT uses and _STYLE_PER_1000 per 1,000 words. Many are among the "style" excess
# words of Kobak et al. 2025 (Science Advances 11:eadt3813; list at github.com/berenslab/llm-excess-vocab, MIT).
_STOCK_WORDS = _word_groups(
    """however, moreover, furthermore, additionally, notably, importantly, interestingly, indeed, therefore, consequently,
    overall, specifically, particularly, clearly, crucially, ultimately, essentially, basically, very, highly, extremely,
    remarkably, substantially, considerably, dramatically, strikingly, potentially, subsequently, primarily, predominantly,
    effectively, typically, novel, robust, comprehensive, crucial, notable, vital, substantial, valuable, diverse, various,
    landscape, leverage, utilize, highlight, emphasize emphasise emphasises emphasised emphasising, perform, enhance
    enhancement enhancements, improve, demonstrate, facilitate, address, tackle, exhibit, achieve, ensure, explore exploration,
    examine, empower, problem, challenge, framework, paradigm, insight, aspect, capability capabilities, methodology,
    advancement"""
)
# LLM markers: words that became far more common in papers after ChatGPT (Kobak et al. 2025; also Liang et al. 2024,
# arXiv:2404.01268) and that ordinary prose rarely needs. A few uses already stand out, hence the lower bar.
# Left out although on their list: words with a plain technical sense ("elevated levels", "viral shedding").
_MARKER_WORDS = _word_groups(
    """delve, showcase, underscore, intricate intricately intricacy intricacies, meticulous meticulously, commendable,
    pivotal, realm, multifaceted, groundbreaking, unparalleled, invaluable, noteworthy, nuanced nuance nuances, interplay,
    interconnectedness, unravel unraveling unravelling, unveil, illuminate, elucidate, harness, bolster, burgeoning,
    transformative, revolutionize revolutionise revolutionizing, seamless seamlessly, uncharted, underexplored, unexplored,
    pave paving, adept, akin, amidst, amid, endeavor endeavour endeavours, encompass, encapsulate, garner, grapple, surmount,
    propel propelled propelling, orchestrate, foster, embrace, formidable, swift swiftly, poised, pinpoint, heighten, accentuate, juxtapose, culminate, hinge, furnish, solidify, impactful, imperative, versatility,
    innovative, pioneering, compelling, remarkable, renowned, strategically, unlock, spearhead, necessitate, intertwine"""
)
_STYLE_MIN_COUNT = 5
_STYLE_PER_1000 = 2.5
_MARKER_MIN_COUNT = 3
_MARKER_PER_1000 = 0.3
# An adjective or adverb (outside the title, abstract, and keywords) is overused at this many uses and this share of all content words.
# Nouns are left out: a paper's subject ("studies", "devices") recurs by necessity.
_OVERUSED_MIN_COUNT = 12
_OVERUSED_SHARE = 0.005
# Telling adjectives and adverbs apart with no tagger or corpora to download: by suffix,
# less the common nouns that share one, plus the frequent ones no suffix marks.
_ADJECTIVE_SUFFIXES = ("ous", "ive", "ful", "less", "able", "ible", "al", "ic", "ary", "ant", "ent")
_NOT_ADVERBS = frozenset("family assembly anomaly reply supply apply multiply rely imply comply ally italy july bully belly jelly".split())
_NOT_ADJECTIVES = frozenset(
    """animal hospital journal signal trial material interval individual proposal terminal capital approval removal survival arrival
    renewal referral portal principal rival denial dial vial metal petal pedal festival manual
    clinic metric topic logic music traffic panic fabric rubric epidemic pandemic tactic mechanic critic statistic republic
    objective archive motive initiative directive narrative perspective detective executive
    summary boundary library salary diary glossary dictionary anniversary secretary commentary
    table cable variable unless enable
    give live five drive arrive derive survive strive thrive deprive revive vary reveal appeal conceal deal
    want grant represent prevent invent meant spent went sent lent rent bent
    participant plant variant mutant implant assistant applicant consultant inhabitant infant antioxidant occupant
    patient student agent parent event content percent component resident president incident accident client talent gradient
    coefficient opponent recipient ingredient respondent solvent extent intent descent consent dissent""".split()
)
_MODIFIER_WORDS = frozenset(
    """great good bad strong weak major minor main key wide broad deep quite somewhat""".split()
)
# Verbs that end like adjectives: "receive", "perceive", "achieve", "believe", "retrieve".
_VERB_ENDINGS = ("eive", "ieve")
# Plain adjectives and adverbs that carry no style: no reader notices "final" or "additional" repeated,
# so they are neither listed as most frequent nor reported as overused.
_PLAIN_MODIFIERS = frozenset(
    """first final last next initial original additional general total actual available possible different current previous
    several respective individual potential usual normal internal external less supplementary supplemental experimental
    empirical theoretical january february""".split()
)
# The same content word this many times in one sentence.
_SENTENCE_REPEATS = 3
# This many consecutive sentences opening with the same word.
_SAME_OPENINGS = 4
# Longest sentence read for repeats: a longer one is usually a table, list, or author block run together.
_MAX_SENTENCE_WORDS = 50
# Shortest word counted as a content word.
_MIN_CONTENT_LEN = 4
# Top words listed in the passing note, each used at least _TOP_MIN_COUNT times and _TOP_PER_1000 times per 1,000 words
# (5 uses in a 3,000-word paper, 12 in an 8,000-word one): a handful of uses in a whole paper is not worth a note.
_TOP_WORDS = 5
_TOP_MIN_COUNT = 5
_TOP_PER_1000 = 1.5
_ITEM_LABEL = re.compile(r"[A-Za-z]+\.?\s+[A-Z]?\d+[A-Za-z]?\s*[:.]")
_REPETITION_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*[A-Za-z]|[A-Za-z]")
# A sentence break: end punctuation, then space and a capital (or a masked item), not after a common abbreviation.
_SENTENCE_END = re.compile(r"(?<!\bal)(?<!\bFig)(?<!\bFigs)(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\bEq)(?<!\bRef)(?<!\bcf)(?<!\bapprox)(?<!\bNo)(?<![A-Z])[.!?]+[\"')\]]*\s+(?=[A-Z\"(" + _MASK + r"])")


def stem(word: str) -> str:
    """``word`` with its plural or verb ending stripped (a grouping key, not a dictionary form)."""
    w = word.lower().replace("’", "'")
    if w.endswith("'s"):
        w = w[:-2]
    for suffix, repl in (("ies", "y"), ("ied", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if not w.endswith(suffix) or len(w) - len(suffix) < 3:
            continue
        if suffix == "s" and w.endswith(("ss", "us", "is")):
            continue
        if suffix == "es" and not w.endswith(("ches", "shes", "xes", "zes", "sses", "oes")):
            continue  # "cases", "genes" -> drop just the s
        base = w[: -len(suffix)] + repl
        # "running" -> "run", "mapped" -> "map"
        if suffix in ("ing", "ed") and len(base) >= 4 and base[-1] == base[-2] and base[-1] not in "lsz":
            base = base[:-1]
        return base
    return w


def _words(text: str) -> list[str]:
    return _REPETITION_WORD.findall(text)


def _is_content(word: str) -> bool:
    return len(word) >= _MIN_CONTENT_LEN and word.lower() not in _STOP_WORDS and not (word.isupper() and len(word) > 1)


def _is_modifier(word: str) -> bool:
    """Whether ``word`` reads as an adjective or adverb (a heuristic: see ``_ADJECTIVE_SUFFIXES``)."""
    w = word.lower()
    if w in _MODIFIER_WORDS:
        return True
    if w in _PLAIN_MODIFIERS:
        return False
    if w.endswith("ly") and len(w) >= 5:
        return w not in _NOT_ADVERBS
    return w.endswith(_ADJECTIVE_SUFFIXES) and not w.endswith(_VERB_ENDINGS) and not w.endswith("ment") and w not in _NOT_ADJECTIVES


def _sentences(paragraph: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(paragraph) if s.strip()]


def _main_paragraphs(paragraphs: list[str]) -> list[str]:
    """The paragraphs before the reference list."""
    from .manuscript import _REFERENCE_HEADING

    for i, p in enumerate(paragraphs):
        if len(p.split()) <= 4 and _REFERENCE_HEADING.match(p.strip()):
            return paragraphs[:i]
    return paragraphs


def _front_matter(paragraphs: list[str]) -> str:
    """What comes before the Introduction: a summary statement, key points, and the like also name the subject."""
    for i, p in enumerate(paragraphs):
        if re.fullmatch(r"(?:\d+\.?\s*)?introduction\.?", p.strip(), re.IGNORECASE):
            return " ".join(paragraphs[:i])
    return ""


@dataclass
class Repetition:
    """What :func:`repetition` found in a text, by kind, plus its most frequent content words."""

    style: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    overused: list[str] = field(default_factory=list)
    in_sentence: list[str] = field(default_factory=list)
    openings: list[str] = field(default_factory=list)
    top: list[tuple[str, int]] = field(default_factory=list)
    top_min: int = _TOP_MIN_COUNT
    words: int = 0


def repetition(paragraphs: list[str], topic: str = "") -> Repetition:
    """Repetitive word choice in ``paragraphs``.

    ``topic`` (the title, abstract, and keywords) names the subject of the
    paper: words in it are expected to recur, so they are never reported as
    overused across the paper.
    """
    from collections import Counter

    out = Repetition()
    all_words = [w for p in paragraphs for w in _words(p)]
    out.words = len(all_words)
    if not all_words:
        return out
    per_1000 = 1000 / len(all_words)

    topic_words = {w.lower() for w in _words(topic)}
    topic_stems = {stem(w) for w in topic_words}
    for forms, least, rate, hits in ((_STOCK_WORDS, _STYLE_MIN_COUNT, _STYLE_PER_1000, out.style), (_MARKER_WORDS, _MARKER_MIN_COUNT, _MARKER_PER_1000, out.markers)):
        counts = Counter(forms[w.lower()] for w in all_words if w.lower() in forms)
        for word, count in counts.most_common():
            if any(forms.get(t) == word for t in topic_words):
                continue  # "Challenges in ..." as a title: the word is the subject
            if count >= least and count * per_1000 >= rate:
                hits.append(f"'{word}' {count} times ({count * per_1000:.1f} per 1,000 words)")

    content = [w for w in all_words if _is_content(w)]
    # Only adjectives and adverbs outside the title, abstract, and keywords can be overused.
    groups = Counter(stem(w) for w in content if _is_modifier(w) and stem(w) not in topic_stems)
    spelling: dict[str, Counter] = {}
    for w in content:
        spelling.setdefault(stem(w), Counter())[w.lower()] += 1
    name = {k: c.most_common(1)[0][0] for k, c in spelling.items()}
    out.top_min = max(_TOP_MIN_COUNT, math.ceil(_TOP_PER_1000 * len(all_words) / 1000))
    out.top = [(name[k], n) for k, n in groups.most_common(_TOP_WORDS) if n >= out.top_min]
    if topic_stems:
        for key, count in groups.most_common(10):
            if count >= _OVERUSED_MIN_COUNT and count / len(content) >= _OVERUSED_SHARE:
                out.overused.append(f"'{name[key]}' {count} times ({100 * count / len(content):.1f}% of content words)")

    for paragraph in paragraphs:
        sentences = _sentences(paragraph)
        for sentence in sentences:
            tokens = sentence.split()
            if len(tokens) > _MAX_SENTENCE_WORDS or sum(1 for t in tokens if re.fullmatch(r"[\"'(]*[A-Za-z][A-Za-z'\u2019-]*[.,;:)\"']*", t)) < 0.7 * len(tokens):
                continue
            counts = Counter(stem(w) for w in _words(sentence) if _is_content(w) and stem(w) not in topic_stems)
            for key, count in counts.items():
                if count >= _SENTENCE_REPEATS:
                    snippet = re.sub(r"\s+", " ", sentence.strip())
                    snippet = snippet if len(snippet) <= 160 else snippet[:157] + "..."
                    out.in_sentence.append(f"'{name.get(key, key)}' {count} times in \"{snippet}\"")
        run: list[str] = []
        for sentence in sentences + [""]:
            # "Appendix S1: Search strategies. Appendix S2: ..." is a list of items, not prose.
            first = "" if _ITEM_LABEL.match(sentence.strip()) else (_words(sentence)[:1] or [""])[0].lower()
            if run and first and first == _words(run[0])[0].lower():
                run.append(sentence)
                continue
            if len(run) >= _SAME_OPENINGS:
                opener = _words(run[0])[0]
                start = re.sub(r"\s+", " ", run[0].strip())[:60]
                out.openings.append(f"{len(run)} sentences in a row open with '{opener}' (\"{start}...\")")
            run = [sentence] if first else []
    return out


_REPETITION_KINDS = (
    ("style", "stock word(s) used often"),
    ("markers", "word(s) that became much more common in papers written with LLMs, used several times"),
    ("overused", "adjective(s) or adverb(s) outside the title, abstract, and keywords used unusually often"),
    ("in_sentence", "sentence(s) repeating a word"),
    ("openings", "run(s) of sentences opening with the same word"),
)


def _repetition_check(venue: Venue, values: dict[str, str]) -> Outcome:
    from .front_matter_check import manuscript_abstract

    topic = " ".join(values.get(k, "") for k in ("title", "abstract", "keywords"))
    issues: list[Issue] = []
    details: list[str] = []
    files = _manuscripts(venue, values)
    for fld, text in files:
        if text.why:
            issues.append(Issue(WARNING, fld.id, text.why, unchecked=True))
            continue
        own = manuscript_abstract(document.get(text.path), values.get("abstract", ""))
        found = repetition(_main_paragraphs(text.paragraphs), " ".join((topic, own[0] if own else "", _front_matter(text.paragraphs))))
        for kind, noun in _REPETITION_KINDS:
            hits = getattr(found, kind)
            if hits:
                shown = "; ".join(hits[:MAX_EXAMPLES])
                more = f" (and {len(hits) - MAX_EXAMPLES} more)" if len(hits) > MAX_EXAMPLES else ""
                issues.append(Issue(WARNING, fld.id, f"{text.path.name}: {len(hits)} {noun}: {shown}{more}; vary the wording"))
        top = ", ".join(f"{w} ({n})" for w, n in found.top)
        prefix = f"{text.path.name}: " if len(files) > 1 else ""
        details.append(f"{prefix}{found.words:,} words; most frequent adjectives and adverbs: {top}" if top else f"{prefix}{found.words:,} words; no adjective or adverb used {found.top_min} or more times" if found.words else f"{prefix}no words read")
    return Outcome(issues, "; ".join(details))


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    logger.info("Proofreading the %s manuscript's prose", venue.slug)
    for name, (title, _, _) in _CHECKS.items():
        checks.run(f"prose.{name}", title, PROSE, _check, name, venue, values)
    checks.run(f"prose.{REPETITION}", "Varied word choice (no heavily repeated adjectives or adverbs)", PROSE, _repetition_check, venue, values)
    from . import style_check

    checks.run("prose.numbers", "Numbers of 10 or more in digits, thousands grouped one way", PROSE, style_check._numbers_check, venue, values)
    checks.run("prose.conventions", "Abbreviations and compounds written one way (i.e., e.g., et al., vs., Fig.)", PROSE, style_check._conventions_check, venue, values)
    checks.run("prose.acronyms", "Acronyms spelled out at first use", PROSE, style_check._acronym_check, venue, values)
    checks.run("prose.heading_case", "Headings of the same level capitalized one way", PROSE, style_check._heading_case_check, venue, values)
    task = style_check.ai_prose_task(venue, values)
    if task is not None:
        checks.needs_ai(task)
