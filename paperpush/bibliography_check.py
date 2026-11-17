"""Check the reference list against the text that cites it.

* ``bibliography.uncited`` -- every entry in the reference list is cited in
  the text, and every citation in the text has an entry. How a citation is
  matched to its entry depends on the manuscript:

  - **LaTeX source**: the keys of ``\\cite`` (``\\citep``, ``\\parencite``,
    ...) and ``\\nocite`` commands against the ``\\bibitem`` keys of an inline
    bibliography, or the entries of the ``.bib`` file(s) it names. BibTeX
    prints only the entries that are cited, so with a ``.bib`` an uncited entry
    matters only under ``\\nocite{*}``, which prints them all; a ``\\cite``
    whose key is in no ``.bib`` is reported either way (it prints as "?").
  - **numbered references** (PDF, Word, Markdown): the numbers cited in
    brackets ("[3]", "[2, 5-7]") -- or, in a Word file, as superscripts --
    against the entries' numbers.
  - **author-year references**: each entry's first-author surname and year
    ("Smith ... 2020") looked for together in the text ("Smith et al., 2020",
    "Smith and Lee (2020)").

  The text searched is everything except the reference list itself, so
  citations in figure legends printed after it count.
* ``bibliography.citation_style`` -- the text cites in the reference list's
  own style: bracketed numbers ("[3]") for a numbered list, "(Smith et al.,
  2020)" for an author-year list, "[BMR+20]" for an alphabetic one. A text
  that cites "(Smith et al., 2020)" against a numbered list leaves every entry
  uncited, and a reader cannot find the work. LaTeX source is not checked: its
  bibliography style prints both.
* ``bibliography.self_citations`` -- the share of references that include
  one of this paper's authors (from the ``.sub``'s author list). A surname
  counts only next to the author's first initial ("Rich, J.", "J. Rich",
  "Joseph Rich"), so another author who shares a surname is rarely counted.
  Reported when more than :data:`MAX_SELF_CITATIONS` references, or more than
  :data:`MAX_SELF_CITATION_SHARE` of a list of at least
  :data:`MIN_REFERENCES_FOR_SHARE`, are self-citations.

* ``bibliography.entries`` -- the ``.bib`` files themselves: duplicate
  citation keys, entries with no key or no closing brace, journal and
  conference entries with no title or author, and one arXiv preprint under two
  keys (see :func:`paperpush.references.bib_structure_findings`).

All are WARNINGs: references are read from extracted text.
"""

from __future__ import annotations

import logging
import re
import datetime
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import document
from .database import Field, Venue
from .validate import SUGGESTION, WARNING, Checks, Issue, NotApplicable, Outcome, Unchecked

logger = logging.getLogger(__name__)

REFERENCES = "References"
MAX_EXAMPLES = 8
# More self-citations than this is reported, whatever the length of the list.
MAX_SELF_CITATIONS = 15
# ... as is a larger share than this of a list of at least MIN_REFERENCES_FOR_SHARE.
MAX_SELF_CITATION_SHARE = 0.8
MIN_REFERENCES_FOR_SHARE = 5
# An author-year list of at least this many entries with more than this share unmatched is read as
# a parsing failure, and bibliography.uncited reports nothing for it.
UNHEALTHY_MIN_ENTRIES = 10
UNHEALTHY_UNCITED_SHARE = 0.5


@dataclass
class Ref:
    """One reference-list entry: how it is cited (``key``), and the text holding its authors."""

    key: str
    label: str
    authors: str
    year: str = ""


@dataclass
class Bibliography:
    """A manuscript's reference list and the citations to it."""

    mode: str  # "keys" (LaTeX), "numbered", "labels" ([BMR+20]), or "author-year"
    refs: list[Ref]
    cited: set[str] | None  # keys or numbers cited; None when citations could not be read
    printed_only_cited: bool = False  # BibTeX: only cited entries reach the reference list
    note: str = ""
    # LaTeX: keys put in the reference list by \\nocite{key} that no \\cite command cites.
    nocite_only: list[str] = field(default_factory=list)


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


# --- LaTeX -------------------------------------------------------------------------------

_BIBITEM = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}(.*?)(?=\\bibitem|\\end\s*\{\s*thebibliography|$)", re.DOTALL)
_NOCITE = re.compile(r"\\nocite\s*\{([^}]*)\}")
# A macro the author wrapped a citation command in.
_CITE_MACRO = re.compile(r"\\(?:re|provide)?newcommand\*?\s*\{?\s*\\([A-Za-z]+)\s*\}?\s*\[\d\]\s*(?:\[[^\]]*\]\s*)?\{[^\n]*?\\[A-Za-z]*cite[a-z]*\*?\s*(?:\[[^\]]*\]\s*)*\{#\d\}")
_BIB_NAMES = re.compile(r"\\(?:bibliography|addbibresource)\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}")


def _keys(group: str) -> list[str]:
    return [k.strip() for k in group.split(",") if k.strip()]


def _tex_bibliography(path: Path, source: str) -> Bibliography | None:
    from . import references
    from .front_matter_check import TEX_CITE

    cited = {k for m in TEX_CITE.finditer(source) for k in _keys(m.group(1))}
    # \\newcommand{\\mycite}[1]{\\cite{#1}}: "#1" is no key, and \\mycite{a} cites a.
    cited = {k for k in cited if "#" not in k}
    for macro in _CITE_MACRO.finditer(source):
        cited |= {k for m in re.finditer(r"\\" + macro.group(1) + r"(?![A-Za-z])\s*(?:\[[^\]]*\]\s*)*\{([^{}#]*)\}", source) for k in _keys(m.group(1))}
    nocite = {k for m in _NOCITE.finditer(source) for k in _keys(m.group(1))}
    # \\nocite{key} prints an entry the text never points to: in the reference list, but uncited.
    named = list(dict.fromkeys(k for m in _NOCITE.finditer(source) for k in _keys(m.group(1)) if k != "*" and k not in cited))
    items = _BIBITEM.findall(source)
    if items:
        refs = [Ref(key.strip(), key.strip(), re.sub(r"\s+", " ", body).strip()) for key, body in items]
        known = {r.key for r in refs}
        return Bibliography("keys", refs, cited | nocite, nocite_only=[k for k in named if k in known])
    bibs: list[Path] = []
    for m in _BIB_NAMES.finditer(source):
        for name in _keys(m.group(1)):
            bibs.append(path.parent / (name if name.lower().endswith(".bib") else name + ".bib"))
    # A .zip carries its own .bib: the files beside it (often the unzipped sources)
    # are not what is uploaded, and reading both would count every entry twice.
    sources = [path] if path.suffix.lower() == ".zip" else [p for p in bibs if p.is_file()]
    entries = references.collect_entries(sources) if sources else []
    if not entries:
        return None
    # One entry per key: BibTeX keeps the first definition of a key.
    refs = list({e.key: Ref(e.key, e.key, e.author, e.year) for e in reversed(entries)}.values())[::-1]
    every = "*" in nocite
    known = {r.key for r in refs}
    return Bibliography("keys", refs, cited | (nocite - {"*"}), printed_only_cited=not every, note="\\nocite{*} prints every .bib entry" if every else "", nocite_only=[k for k in named if k in known])


# --- text reference lists ----------------------------------------------------------------

_NUMBER_LABEL = re.compile(r"^\s*(?:\[(\d{1,4})\]|(\d{1,4})[.)])\s")
_PAREN_GROUP = re.compile(r"\(\s*(\d{1,4}(?:\s*[,;\u2013\u2014-]\s*\d{1,4})*)\s*\)")
_BRACKET = re.compile(r"\[\s*(\d{1,4}(?:\s*[,;\u2013\u2014-]\s*\d{1,4})*)\s*\]")
_YEAR = re.compile(r"\b((?:19|20)\d{2})([a-z]?)\b")
_SURNAME = re.compile(r"[^\W\d_](?:[^\W\d_]|['\u2019-])+")
_PARTICLES = {"van", "von", "de", "der", "den", "da", "di", "du", "del", "della", "la", "le", "dos", "das", "ten", "ter"}


def _numbers(group: str) -> list[int]:
    from .requirements_check import _numbers as numbers

    return numbers(group.replace("\u2014", "-").replace(";", ","))


def citation_groups(body: str, last: int) -> list[str]:
    """The bracketed number groups of ``body`` that are citations of a list numbered up to ``last``.

    "[0, 1]" is an interval (references count from 1), and a group of several
    numbers all beyond the list ("[18-25]" with 12 references) is a range of
    values. A lone "[45]" stays: that is a citation with no entry.
    """
    groups = []
    for group in _BRACKET.findall(body):
        numbers = _numbers(group)
        if not numbers or min(numbers) < 1 or (len(numbers) > 1 and min(numbers) > last):
            continue
        groups.append(group)
    return groups


def _unwrap(text: str) -> str:
    """``text`` with its line wraps turned into spaces (a blank line still ends a paragraph)."""
    return re.sub(r"[ \t]*\n[ \t]*(?=\S)", " ", text)


# An appendix heading that ends in a number, which _AFTER_REFERENCES (all capitals, ending in a
# letter) misses: "A P ROOF OF THEOREM 1" (small capitals split by extraction, ICLR gDxJK8yvZU)
# was read as a reference entry, and the appendix's own citations as part of the list. One or two
# digits only, so an all-capitals author line ending in a year ("J. SMITH 2001") is no heading.
_NUMBERED_APPENDIX_HEADING = re.compile(r"(?m)^[ \t]*[A-Z](?:\.\d+)*\.?[ \t]+(?=[A-Z ]*[A-Z]{3})[A-Z][A-Z \t&:,()-]{3,60}?[ \t]+\d{1,2}(?:\.\d+)*[ \t]*$")


def _split(text: str) -> tuple[str, str] | None:
    """``(body, reference list)`` of ``text``: the reference list, and everything else (legends after it are kept)."""
    from .manuscript import _AFTER_REFERENCES, _REFERENCE_HEADING

    heading = _REFERENCE_HEADING.search(text)
    if heading is None:
        return None
    tail = text[heading.end() :]
    end = _AFTER_REFERENCES.search(tail)
    appendix = _NUMBERED_APPENDIX_HEADING.search(tail)
    if appendix is not None and (end is None or appendix.start() < end.start()):
        end = appendix
    if end is None:
        return text[: heading.start()], tail
    return text[: heading.start()] + "\n" + tail[end.start() :], tail[: end.start()]


# Entry markers: "[12] " anywhere a new entry can start, or "12. " / "12) " opening a line.
_BRACKET_MARK = re.compile(r"(?:^|(?<=\s))\[(\d{1,4})\]\s+(?=\S)")
_LINE_MARK = re.compile(r"(?m)^[ \t]*(\d{1,4})[.)][ \t]+(?=\S)")
# Alphabetic labels, "[BMR+20]", "[Cra17]", "[LHCG19b]".
_ALPHA_MARK = re.compile(r"(?:^|(?<=\s))\[([A-Z][A-Za-z]{0,6}\+?\d{2}[a-z]?)\]\s+(?=\S)")
# Gap allowed between consecutive entry numbers before a marker is taken for something else (a year, a page).
_MAX_NUMBER_GAP = 3
# How far into the reference list (characters, after page furniture) its first number may sit.
NUMBERED_START_WITHIN = 600


def _sequential(marks: list[re.Match[str]]) -> list[re.Match[str]]:
    """The markers that count up from 1, skipping ones that cannot be the next entry (a year opening a line)."""
    out: list[re.Match[str]] = []
    last = 0
    for m in marks:
        n = int(m.group(1))
        if last < n <= last + _MAX_NUMBER_GAP:
            out.append(m)
            last = n
    return out


def _entries(tail: str, marks: list[re.Match[str]]) -> list[tuple[str, str]]:
    bounds = [m.start() for m in marks] + [len(tail)]
    return [(m.group(1), re.sub(r"\s+", " ", tail[m.end() : b]).strip()) for m, b in zip(marks, bounds[1:])]


_W_PARA = re.compile(r"<w:p\b.*?</w:p>", re.DOTALL)
_W_RUN = re.compile(r"<w:r\b.*?</w:r>", re.DOTALL)
_W_TEXT = re.compile(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>")


def docx_superscript_numbers(doc: document.Document) -> set[int] | None:
    """Reference numbers a Word file cites as superscripts before its reference list, or None if unreadable."""
    from .manuscript import _REFERENCE_HEADING

    xml = doc.docx_part("word/document.xml")
    if xml is None:
        return None
    found: set[int] = set()
    for para in _W_PARA.findall(xml):
        plain = "".join(_W_TEXT.findall(para))
        if len(plain.split()) <= 4 and _REFERENCE_HEADING.match(plain.strip()):
            break
        run_text = ""
        for run in _W_RUN.findall(para) + [""]:
            if 'w:vertAlign w:val="superscript"' in run:
                run_text += "".join(_W_TEXT.findall(run))
                continue
            group = run_text.strip().strip(",")
            if re.fullmatch(r"\d{1,4}(?:\s*[,\u2013\u2014-]\s*\d{1,4})*", group):
                found.update(_numbers(group))
            run_text = ""
    return found


def _first_surname(entry: str) -> str:
    words = _NUMBER_LABEL.sub("", entry, count=1).split()
    for i, w in enumerate(words[:6]):
        bare = w.strip(",.;:()[]")
        if bare.lower() in _PARTICLES and i + 1 < len(words):
            continue
        m = _SURNAME.match(bare)
        if m and m.group(0)[0].isupper():
            return m.group(0)
    return ""


def _sort_surname(entry: str) -> str:
    """The surname a reference list is alphabetized by, for either name order.

    "Smith, J., ..." gives Smith; "Noushin Ahmadpour, Meher Kantroo, ..." (given name first,
    BibTeX's plain and IEEE styles) gives Ahmadpour, not Noushin.
    """
    names = _NUMBER_LABEL.sub("", entry, count=1).strip()
    head = re.split(r",| and |\.\s+(?=[A-Z][a-z]+\b[^,]*$)", names, maxsplit=1)[0].split()
    # Initials, with or without full stops ("J.", "PM" in Vancouver's "Berry PM"), and suffixes.
    words = [w for w in head if not re.fullmatch(r"(?:[A-Z]\.-?)+|[A-Z]{1,3}|Jr\.?|Sr\.?|I{2,3}|IV", w)]
    if len(head) >= 2 and len(words) >= 1 and not names.split()[0].endswith(","):
        last = words[-1].strip(".;:()[]")
        if _SURNAME.match(last) and last[0].isupper():
            return last
    return _first_surname(entry)


_AUTHOR_YEAR_HEAD = re.compile(r"^[^.]{0,250}?(?:\.[^.]{0,60}?){0,8}?[.,]\s*\(?(?:19|20)\d{2}[a-z]?\)?[.,:]")
# The end of an entry's author list in the natbib author-year styles (ICLR, NeurIPS, ICML, *ACL):
# "Jort F. Gemmeke, Daniel P. W. Ellis, and Marvin Ritter. Audioset: ..., 2017." The year
# closes the entry, so _AUTHOR_YEAR_HEAD misses it. An initial ("F.") is no end; "et al." is.
_AUTHOR_LIST_END = re.compile(r"(?<!\b[A-Z])\.(?:\s|$|(?=[A-Z][a-z]))")
# Two "Given Surname," names opening an entry ("Kevin Black, Noah Brown, ..."): an author list,
# even when a later name is lower-cased ("brian ichter") or the title follows without a space.
_NAME_WORD = r"(?:[A-Z][\w'.\u2019-]*|van|von|de|der|den|da|di|du|le|la|del|dos|das)"
_TWO_NAMES = re.compile(rf"(?:[A-Z][\w'.\u2019-]*(?:\s+{_NAME_WORD}){{1,3}},\s+){{2}}")
_NAME_TOKEN_LOWER = frozenset({"and", "et", "al", "al.", "&", "van", "von", "de", "der", "den", "da", "di", "du", "le", "la", "del", "dos", "das", "ter", "ten", "bin", "ibn", "y"})


# "Surname, I. I." names, as the ICML/APA-like styles print them: "Lewis, P., Yih, W.-t., and Kiela, D.".
_SURNAME_INITIALS = r"[A-Z][\w'\u2019-]*(?:\s+(?:van|von|de|der|den|da|di|du|le|la|del)\s+[A-Z][\w'\u2019-]*)*,\s+[A-Z]\.(?:\s?-?[A-Za-z]\.)*"
# A whole list of them, ended by the last author's initial and the title: an initial alone is no
# end of an author list, so _AUTHOR_LIST_END runs past "Kiela, D." into the title.
_INITIALS_LIST_HEAD = re.compile(rf"(?:{_SURNAME_INITIALS},\s+)*(?:(?:and|&)\s+)?{_SURNAME_INITIALS}(?:,?\s+et al\.)?\s*(?=[A-Z0-9\"\u201c])")


def _author_list_head(entry: str) -> bool:
    """Whether ``entry`` opens with a bare list of author names ended by a period, as natbib's author-year styles print it.

    A run-on line ("Conference on Acoustics, Speech and Signal Processing ...") has
    lowercase words that are not name particles, so it stays with the entry before.
    """
    if _INITIALS_LIST_HEAD.match(entry):
        return True
    end = _AUTHOR_LIST_END.search(entry, 0, 2500)
    if end is None:
        return False
    tokens = re.findall(r"[^\s,]+", entry[: end.start()])
    if not tokens or len(tokens) > 400:
        return False
    for token in tokens:
        if token.lower() in _NAME_TOKEN_LOWER:
            continue
        letter = next((c for c in token if c.isalpha()), "")
        # An accent split off by extraction ("Doll ´ar") leaves a token opening with the mark.
        if not letter or (not letter.isupper() and not re.match(r"[\u00b4`\u00a8~^\u02c6\u02dc\u00b8'-]", token)):
            return False
    return tokens[0][:1].isupper()


# PDF text often sets an accent as its own glyph beside the letter ("Schl \u00a8uter",
# "Doll \u00b4ar"), which breaks the surname. The spacing accent goes onto the next letter.
_SPACING_ACCENTS = {"\u00a8": "\u0308", "\u00b4": "\u0301", "`": "\u0300", "\u02c6": "\u0302", "\u02dc": "\u0303", "\u00b8": "\u0327",
                    "\u02c7": "\u030c", "\u02d8": "\u0306", "\u02d9": "\u0307", "\u02da": "\u030a", "\u02dd": "\u030b", "\u02db": "\u0328"}
_SPLIT_ACCENT = re.compile(r"(?<=[A-Za-z]) ?([" + "".join(_SPACING_ACCENTS) + r"]) ?([A-Za-z\u0131])")


def join_split_accents(text: str) -> str:
    import unicodedata

    # A dotless i takes the accent as an i ("Pelegr\u00b4\u0131n" -> "Pelegr\u00edn").
    return _SPLIT_ACCENT.sub(lambda m: unicodedata.normalize("NFC", m.group(2).replace("\u0131", "i") + _SPACING_ACCENTS[m.group(1)]), text)


# Kerning can split an initial from its period ("Kathy Y . Wei") or a capital from its word ("Richard A V olz").
_KERNED_INITIAL = re.compile(r"\b([A-Z]) \.(?=[\s,;])")
_KERNED_CAPITAL = re.compile(r"\b([VWY]) ([a-z]{2,})\b")


def unkern(text: str) -> str:
    return _KERNED_CAPITAL.sub(r"\1\2", _KERNED_INITIAL.sub(r"\1.", text))


_NOT_A_YEAR = re.compile(r"(?:arXiv:?\s*|abs/|\d[-\u2013\u2014]|10\.\d{4,9}/\S*?)$", re.IGNORECASE)


_LATEST_YEAR = datetime.date.today().year + 1


def _in_url_or_date(entry: str, m: re.Match[str]) -> bool:
    """A year inside a link ("https://.../2018/...") or an access date ("Accessed: 2025-09-13")."""
    start = max(entry.rfind(" ", 0, m.start()), 0)
    end = entry.find(" ", m.end())
    token = entry[start: end if end != -1 else len(entry)]
    if "/" in token or re.match(r"-\d{2}-\d{2}\b", entry[m.end():]):
        return True
    # Nor an ISSN ("ISSN 2076-3417", "1939-1471") or a number no year can be yet.
    return bool(re.match(r"-\d{3}[\dXx]\b", entry[m.end():])) or int(m.group(1)) > _LATEST_YEAR


def entry_year(entry: str) -> re.Match[str] | None:
    """The publication year of a reference-list entry.

    Not the first year-like number: an arXiv id ("2010.11929"), a page range ("1877-1901"), or
    a DOI can come first. A parenthesized year ("(2015)") is the year; otherwise the last one
    left, where the natbib styles print it.
    """
    # Nor one inside a DOI split by a line wrap ("doi: 10.1016/j. tibtech.2014.11.010").
    found = [m for m in _YEAR.finditer(entry) if not _NOT_A_YEAR.search(entry[max(0, m.start() - 40): m.start()]) and not re.match(r"\.\d", entry[m.end():]) and not re.search(r"[A-Za-z]\.$", entry[max(0, m.start() - 2): m.start()]) and not _in_url_or_date(entry, m)]
    if not found:
        return None
    for m in found:
        if entry[m.start() - 1: m.start()] == "(" and entry[m.end(): m.end() + 1] == ")":
            return m
    return found[-1]


def _runs_on(entry: str) -> bool:
    """Whether ``entry`` opens like the rest of a venue name ("Conference on Acoustics, ...") rather than an author list."""
    raw = entry.split()[:3]
    # The rest of a URL wrapped onto the next line ("Module_Networks_CVPR_2016_paper.html.", ICLR
    # 14dlTHVxDX), which otherwise opened an entry and swallowed the next one.
    if raw and ("_" in raw[0] or re.search(r"\.(?:html?|pdf|php)\.?$", raw[0])):
        return True
    if raw and re.fullmatch(r"[A-Z][\w&-]*\.", raw[0]):
        return False  # a one-word author ("Anthropic. System card: ...")
    words = [w.strip(",.;:()[]") for w in raw]
    return any(w.isalpha() and w.islower() and w not in _NAME_TOKEN_LOWER for w in words[1:])


def _page_furniture(doc: document.Document) -> set[str]:
    """Running heads and feet: a first or last line that recurs on three or more pages."""
    from collections import Counter

    seen: Counter[str] = Counter()
    for page in doc.page_texts or ():
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        for line in {*(lines[:1]), *(lines[-1:])}:
            if len(line) > 12 and not line.isdigit():
                seen[line] += 1
    return {line for line, n in seen.items() if n >= 3}


def _surname_candidates(entry: str) -> list[str]:
    """The first author's possible surnames, for either name order.

    "Smith, J., ..." gives Smith; "Jacob Devlin, Ming-Wei Chang, ..." (the
    *ACL style, given name first) gives Devlin as well as Jacob, so the entry
    is matched to "Devlin et al. (2019)" in the text.
    """
    out = []
    first = _first_surname(entry)
    if first:
        out.append(first)
    names = _NUMBER_LABEL.sub("", entry, count=1)
    # A single author with no comma ("Abraham H Maslow. A theory of ...") ends at the list's period.
    cut = _AUTHOR_LIST_END.search(names, 0, 400)
    if cut and not _YEAR.match(names[cut.end():].lstrip("( ")):
        names = names[: cut.start()]
    head = re.split(r",| and |\.\s+(?=\(?(?:19|20)\d{2})|\s+et\s+al\b", names, maxsplit=1)[0]
    words = [w.strip(",.;:()[]") for w in head.split()]
    words = [w for w in words if w and not re.fullmatch(r"[A-Z]\.?(?:-[A-Z]\.?)?", w)]
    if 1 < len(words) <= 5:
        last = words[-1]
        m = _SURNAME.match(last)
        if m and m.group(0)[0].isupper() and m.group(0) not in out:
            out.append(m.group(0))
    return out


def _text_bibliography(doc: document.Document, path: Path) -> Bibliography | None:
    from .manuscript import author_year_starts

    text = doc.text
    if text is None:
        return None
    parts = _split(text)
    if parts is None:
        return None
    body, tail = parts

    numbered = max((_sequential(list(p.finditer(tail))) for p in (_BRACKET_MARK, _LINE_MARK)), key=len)
    # A numbered list starts right under its heading. Two "1." / "2." lines deep in the tail
    # are an enumerated list in an appendix the tail ran into, not the references.
    if numbered and numbered[0].start() > NUMBERED_START_WITHIN and len(numbered) < len(author_year_starts(tail)) // 2:
        numbered = []
    if len(numbered) >= 2:
        refs = [Ref(n, f"[{n}]", entry) for n, entry in _entries(tail, numbered)]
        last = max(int(n) for n, _ in _entries(tail, numbered))
        cited = {str(n) for g in citation_groups(body, last) for n in _numbers(g)}
        # natbib's numbers style can print citations in parentheses ("(1; 13; 8)", "Refs. (30; 31)").
        # A parenthesized number may also be an equation or a list item, so they count only when
        # brackets cite under half the list, and as citations only, never as missing entries.
        if len(cited) < len(refs) / 2:
            paren = {str(n) for g in _PAREN_GROUP.findall(body) for n in _numbers(g) if 1 <= n <= last}
            if len(paren) > len(cited):
                cited |= paren
        note = ""
        if not cited and doc.is_docx:
            cited = {str(n) for n in docx_superscript_numbers(doc) or ()}
            note = "superscript citations"
        return Bibliography("numbered", refs, cited or None, note=note)

    alpha = list(_ALPHA_MARK.finditer(tail))
    if len(alpha) >= 2:
        refs = [Ref(label, f"[{label}]", entry) for label, entry in _entries(tail, alpha)]
        known = {r.key for r in refs}
        groups = re.findall(r"\[([^\[\]]{2,300})\]", body)
        cited = {k.strip() for g in groups for k in re.split(r"[,;]", g) if k.strip() in known}
        return Bibliography("labels", refs, cited)

    tail = unkern(join_split_accents(tail))
    furniture = _page_furniture(doc)
    if furniture:
        tail = "".join(line for line in tail.splitlines(keepends=True) if line.strip() not in furniture)
    # A page number on a line of its own breaks an entry ("doi: 10.1109/" / "4" / "TBDATA.2019...").
    tail = "".join(line for line in tail.splitlines(keepends=True) if not re.fullmatch(r"\s*\d{1,3}\s*", line))
    # The text cites with the same broken accents and kerning, and a citation can run across a
    # page break ("Xue et al." / "7" / running head / "(2025)") or a hyphenated name ("Akhound-" / "Sadegh").
    body = unkern(join_split_accents(body))
    body = "".join(line for line in body.splitlines(keepends=True) if line.strip() not in furniture and not line.strip().isdigit())
    body = re.sub(r"(?<=[A-Za-z]-)[ \t]*\n[ \t]*(?=[A-Z])", "", body)
    # A page break leaves a run of blank lines where its number and running head were, which kept
    # "Ren et al." / ... / "(2025)" (ICLR 3KqASrNJGK) apart; one blank line, a paragraph break, stays.
    body = re.sub(r"\n(?:[ \t]*\n){2,}", "\n", body)
    starts = author_year_starts(tail)
    if not starts:
        return None
    starts = sorted(set(starts) | set(_entries_after_a_closed_entry(tail, starts)))
    entries: list[str] = []
    for a, b in zip(starts, starts[1:] + [len(tail)]):
        entry = re.sub(r"\s+", " ", tail[a:b]).strip()
        # A real entry opens with its authors and year ("..., Name. 2015.", "Smith, J. (2015).") or,
        # in the natbib styles, with its authors and a year at the end; a line that does neither is
        # the previous entry running on (a venue, "Preprint, arXiv:...").
        # After an entry closed by its URL, an author list opens the next entry even before its year
        # has been joined back on (see open_venue below).
        after_url = bool(entries) and bool(re.search(r"https?://\S*[^/\s-]\.$", entries[-1]))
        names = (_author_list_head(entry) or bool(_TWO_NAMES.match(entry))) and (bool(_YEAR.search(entry)) or after_url)
        # A volume and pages this early is a venue line, even one in capitals that reads like names
        # ("IEEE Trans. Mob. Comput., 24(5):4053-4071, 2025.", ICLR BtpXep9qxC).
        head = not _runs_on(entry) and not _heading_like(entry) and not _VENUE_OPENING.search(entry[:80]) and not _VOLUME_PAGES.search(entry[:40]) and not _PUBLISHER_LINE.match(entry) and (names or (bool(_AUTHOR_YEAR_HEAD.search(entry[:300])) and not _venue_line(entry)))
        # An entry with no year that stops on an abbreviation ("... In IEEE Conf. Comput.") ran on into
        # this one ("Vis. Pattern Recog., 2023.", ICLR iXp4UywUfr), which reads like a one-word author.
        open_venue = bool(entries) and entry_year(entries[-1]) is None and bool(_ENDS_MID_VENUE.search(entries[-1]))
        if entries and (len(entry) < 25 or not head or open_venue):
            entries[-1] += " " + entry
        elif entry and not _heading_like(entry):
            entries.append(entry)
    folded = _unwrap(_fold(body))
    folded_tail = _unwrap(_fold(tail))
    parsed = [(entry, entry_years(entry), _surname_candidates(entry)) for entry in entries]
    parsed = [(e, ys, cs) for e, ys, cs in parsed if ys and cs]
    # How many entries each surname leads, to accept a yearless "LoRA (Hu et al.)" (ICLR OswqOlTYR2)
    # only when it can point to one entry.
    from collections import Counter

    leads = Counter(_fold(_likely_surname(e, cs)) for e, _, cs in parsed)
    refs: list[Ref] = []
    cited: set[str] = set()
    for entry, years, candidates in parsed:
        year = years[0]
        hit = next((c for c in candidates if _cited_with_year(_fold(c), year, folded, folded_tail)), None)
        if not hit:
            # Two entries merged by extraction hold two years, and the one read can be the other
            # entry's ("Kang ... 2024. Diederik P Kingma ... 2014", ICLR wjaTz8nYjD; "Grimmett ...
            # 2001. Ju He ... 2025", 7DeARTwvwL). A citation to any of them counts.
            hit = next((c for y in years[1:] for c in candidates if _cited_with_year(_fold(c), y, folded, folded_tail)), None)
        if not hit:
            # A given name glued to the surname by extraction ("FıratÖncel", ICLR wjaTz8nYjD, cited as
            # "Öncel et al., 2024"): the part after the glue is the surname. Tried last, and never used
            # as the label, so "DeepMind" or "McCloskey" is not renamed.
            glued = [m.group(1) for c in candidates if (m := re.search(r"[a-z\u0131]([A-Z\u00c0-\u00de][a-z]{2,})$", c))]
            hit = next((g for g in glued for y in years if _cited_with_year(_fold(g), y, folded, folded_tail)), None)
        if not hit:
            likely = _likely_surname(entry, candidates)
            hit = likely if leads[_fold(likely)] == 1 and _cited_without_year(_fold(likely), folded) else None
        surname = hit or _likely_surname(entry, candidates)
        key = f"{surname} {year.group(0)}"
        refs.append(Ref(key, f"{surname} ({year.group(0)})", entry, year.group(0)))
        if hit:
            cited.add(key)
    return Bibliography("author-year", refs, cited) if refs else None


def _likely_surname(entry: str, candidates: list[str]) -> str:
    """The first author's surname as best guessed: the last name for "Jacob Devlin, ..." (given name
    first, the natbib styles) and the first word for "Devlin, J., ..."."""
    lead = _NUMBER_LABEL.sub("", entry, count=1)
    # "Erickson AG, ..." (Vancouver: surname, then initials with no comma) is surname-first too. The
    # initials need a comma or a year after them, or two letters before a full stop: "Edward J. Hu"
    # and "George A Miller" (given name, middle initial, ICLR OswqOlTYR2 / 2OcklgJiU0) are given-first.
    given_first = len(candidates) > 1 and not re.match(r"\s*[^\s,]+,", lead) and not re.match(r"\s*\S+\s+(?:[A-Z]{1,3}(?:[,;]|\s+\(?(?:19|20)\d{2})|[A-Z]{2,3}\.\s)", lead)
    return candidates[1] if given_first else candidates[0]


# Before a surname: no letter, or a lowercase letter that extraction glued to it ("Chain of
# ThoughtWei et al. (2022)", ICLR kHhMs642rR; "Maximal InequalityRinaldo (2018)", KfjpyOcPQj).
_SURNAME_START = r"(?:(?<![A-Za-z])|(?<=[a-z])(?=[A-Z]))"
# ... or, for a surname of four letters or more, a capital: a model name glued to its citation
# ("Llama-3-8BDubey et al. (2024)", "Mistral-7BJiang et al. (2023a)", ICLR 9U51rOnGko). A short
# surname ("Li", "Wu") could sit inside an acronym.
_LONG_SURNAME_START = r"(?:(?<![A-Za-z])|(?<=[A-Za-z])(?=[A-Z]))"


def _start(surname: str) -> str:
    return _LONG_SURNAME_START if len(surname) >= 4 else _SURNAME_START
# One year of a chain of the same authors' years, with its suffixes: "2018; ", "2023d;a; ", "2024c;d, ".
_CHAIN_YEAR = r"(?:19|20)\d{2}[a-z]?(?:\s*[;,]\s*[a-z](?![\w]))*\s*[;,]\s*"


def _cited_with_year(surname: str, year: re.Match[str], folded: str, folded_tail: str) -> bool:
    """Whether the text cites ``surname`` with ``year``, alone or in a chain of the same authors' years."""
    suffix = year.group(2) or "[a-z]?"
    # The year can follow others of the same authors ("Wang et al., 2018; 2019", "Qin et al. (2024c;d;
    # 2025e; 2023b; 2025a;c)"), and a suffix can stand for its year ("Liu et al. (2023b; 2022b;a)" is
    # 2022a, ICLR KAGR7Mqu4h).
    target = rf"{year.group(1)}{suffix}(?![\da-z])"
    if year.group(2):
        target = rf"(?:{target}|{year.group(1)}[a-z](?:\s*[;,]\s*[a-z])*\s*[;,]\s*{year.group(2)}(?![\w]))"
    chain = rf"\b(?:{_CHAIN_YEAR}){{0,12}}{target}"
    if re.search(rf"{_start(surname)}{re.escape(surname)}(?![a-z])[^;\n]{{0,80}}?{chain}", folded):
        return True
    # An appendix after the reference list cites in the same shape: "Boteva et al., 2016", "Holm (2024)".
    return bool(re.search(rf"{_start(surname)}{re.escape(surname)}(?: et al\.?| (?:and|&) [A-Z][\w-]+)?,? \(?{year.group(1)}{suffix}(?![\da-z])", folded_tail))


def _cited_without_year(surname: str, folded: str) -> bool:
    """A citation that names the authors with no year: "LoRA (Hu et al.)", "(Smith & Lee)", "(Chen)".

    "et al." or a second author is required, so a word that happens to be a surname ("Hu", "Wang") does not count.
    """
    # A single author in parentheses, "architectures from (Chen)" (ICLR CVqYCYpq75), counts too.
    return bool(re.search(rf"{_start(surname)}{re.escape(surname)}(?: et al\b| (?:and|&) [A-Z][\w'-]+\s*\))|\({re.escape(surname)}\)", folded))


def entry_years(entry: str) -> list[re.Match[str]]:
    """Every year an entry may be cited with: :func:`entry_year` first, then the others it holds (when two entries merged)."""
    first = entry_year(entry)
    if first is None:
        return []
    others = [m for m in _YEAR.finditer(entry) if m.group(1) != first.group(1) and not _NOT_A_YEAR.search(entry[max(0, m.start() - 40): m.start()]) and not re.match(r"\.\d", entry[m.end():]) and not re.search(r"[A-Za-z]\.$", entry[max(0, m.start() - 2): m.start()]) and not _in_url_or_date(entry, m)]
    return [first] + others


# A line that closes an entry with its year ("... llms. 2024."): extraction can then miss that the
# next line starts another entry (author_year_starts reads ". 2024." as the year after the authors,
# with the title to follow), and two entries merge.
_CLOSED_BY_YEAR = re.compile(r"(?:19|20)\d{2}[a-z]?\.\s*$")
# ... or with an ISBN or ISSN ("Curran Associates Inc. ISBN 9781510860964.", ICLR ta8BKRa1bl), which never follows the authors.
_CLOSED_BY_ISBN = re.compile(r"\bIS[BS]N:?\s*[\dXx-]{8,17}\.\s*$")
# ... or with its URL (natbib's ML styles end an entry "..., 2025a. URL https://arxiv.org/abs/2507.13347."):
# the fabricated "Zihang Wang et al. Uncertainty-aware ensemble nerf ..." after one (ICLR iXp4UywUfr) merged
# into it, and the real entry's match hid it.
# The URL must end the line with a full stop: one wrapped after "/" or "-" runs on to the next line.
_CLOSED_BY_URL = re.compile(r"(?:19|20)\d{2}[a-z]?\.\s+URL\s+https?://\S*[^/\s-]\.\s*$")


def _entries_after_a_closed_entry(tail: str, starts: list[int]) -> list[int]:
    """Extra entry starts: a line after one that closes an entry (title, then year) that opens with an author list and has a year.

    "Feiyang Kang, ... Ruoxi Jia. AutoScale: ... llms. 2024." / "Diederik P Kingma and Jimmy Ba. Adam: ..., 2014."
    (ICLR wjaTz8nYjD) and "Frederick Eberhardt, ... 2005." / "Paul Erdos and Alfred Renyi. ..." (ta8BKRa1bl).
    The line before must hold a title as well as the authors (two sentence ends before its year), so the
    title line of "Ashish Vaswani and Noam Shazeer. 2017." (ACL style, year after the authors) is not taken for authors.
    """
    out: list[int] = []
    bounds = starts + [len(tail)]
    for a, b in zip(bounds, bounds[1:]):
        offset = a
        lines = tail[a:b].splitlines(keepends=True)
        for i, line in enumerate(lines):
            offset += len(line)
            by_url = _CLOSED_BY_URL.search(line)
            if i + 1 >= len(lines) or not (_CLOSED_BY_YEAR.search(line) or _CLOSED_BY_ISBN.search(line) or by_url):
                continue
            before = re.sub(r"\s+", " ", tail[a:offset]).strip()
            before = _CLOSED_BY_YEAR.sub("", before)
            # A URL after the year closes an entry in any style; the title-then-year test is for a bare year.
            if not _CLOSED_BY_ISBN.search(line) and not by_url and len(_AUTHOR_LIST_END.findall(before)) < 2:
                continue
            rest = re.sub(r"\s+", " ", tail[offset:b]).strip()
            # After a URL the next entry's year can sit in a fragment split off it ("... Conf. Comput." /
            # "Vis. Pattern Recog., 2023."), which the assembly joins back; a bare year needs it here.
            if not rest or _runs_on(rest) or not (_author_list_head(rest) or _TWO_NAMES.match(rest)) or (entry_year(rest) is None and not by_url):
                continue
            start = offset + len(tail[offset:b]) - len(tail[offset:b].lstrip())
            out.append(start)
            a = start
    return out


# What extraction can set on a line of its own that is no entry: an appendix heading ("A P ROOF OF
# THEOREM 1", ICLR gDxJK8yvZU; all capitals, or small capitals split into "P ROOF") or the opening
# of a proof or theorem ("Proof. It has been shown ...").
_HEADING_LIKE = re.compile(r"^(?:[A-Z](?:\.\d+)*\.?\s+)?(?:[A-Z]+\s+){0,2}[A-Z]{2,}(?:\s+[A-Z]{2,}|\s+[A-Z]\s?[A-Z]{2,})+\b(?![a-z.,])|^(?:Proof|Theorem|Lemma|Corollary|Proposition|Definition|Remark|Assumption)\b(?:\s+\d+(?:\.\d+)*)?\s*[.:]")
# A venue and volume line that ran on from the entry before: "Bio Systems, 37(1-2):147-166, 1996."
# (ICLR Etu7q4KcZ7), "Science, 211(4481):453-458, 1981.", "ISSN 2835-8856. URL ...", "Curran Associates Inc. ISBN ...",
# "OpenReview.net, 2025. URL ..." (90tCp2KszA), "Graph.36, 6, Article 176 (Nov. 2017)" (5llEo7Hx1k).
_VOLUME_PAGES = re.compile(r"\d+\s*\(\s*\d[^)]{0,12}\)|\d+\s*:\s*\d+|\b\d{1,5}\s*[-–—]\s*\d{1,5}\b|\bpp?\.\s*\d")
_VENUE_OPENING = re.compile(r"^(?:ISSN|ISBN|URL|doi|DOI|https?:|In\b|Proceedings\b|Conference\b|Journal\b|Transactions\b|[A-Za-z]+\.(?:net|org|com)\b|[A-Za-z]+\.\d)|\b(?:ISSN|ISBN)\b")


# A publisher and year alone: "American Mathematical Soc., 2004." (ICLR P8RzsaEN2s).
_PUBLISHER_LINE = re.compile(r"^[^.]{0,60}?\b(?:Soc\.|Society|Press|Inc\.|Ltd\.?|Publishers?|Publishing|Verlag|Springer|Elsevier|Wiley|Associates)\s*,?\s*(?:19|20)\d{2}[a-z]?\.?\s*$")


# An abbreviated venue word at the end of an entry: "Comput.", "Conf.", "Proc." (not an initial, "J.").
_ENDS_MID_VENUE = re.compile(r"\b[A-Z][a-z]{2,}\.$")


def _venue_line(entry: str) -> bool:
    """Whether ``entry`` opens like a venue, volume, or publisher line rather than authors (it then joins the entry before)."""
    year = entry_year(entry)
    before = entry[: year.start()] if year is not None else entry[:120]
    return bool(_VENUE_OPENING.search(before[:80]) or _VOLUME_PAGES.search(before))


def _heading_like(entry: str) -> bool:
    return bool(_HEADING_LIKE.match(entry))


def read_bibliography(path: Path) -> Bibliography | None:
    """The reference list of the manuscript at ``path`` and what its text cites, or None."""
    doc = document.get(path)
    if doc.is_tex and doc.tex_source is not None:
        found = _tex_bibliography(path, doc.tex_source)
        if found is not None:
            return found
    return _text_bibliography(doc, path)


# --- self-citations ------------------------------------------------------------------------


def _author_names(venue: Venue, values: dict[str, str]) -> list[tuple[str, str, str]]:
    """``(display name, surname, first initial)`` for each author in the .sub."""
    from .validate import _author_name, parse_authors

    field = next((f for f in venue.fields if f.type == "authorlist" and values.get(f.id, "").strip()), None)
    if field is None:
        return []
    out: list[tuple[str, str, str]] = []
    for author in parse_authors(values[field.id], field.fields):
        if author.get("last_name"):
            surname, given = author["last_name"].strip(), author.get("first_name", "").strip()
            name = _author_name(author)
        else:
            name = _author_name(author)
            parts = name.replace(".", ". ").split()
            if not parts:
                continue
            surname, given = parts[-1], parts[0] if len(parts) > 1 else ""
        if surname:
            out.append((name, _fold(surname), _fold(given[:1]).upper()))
    return out


def _author_part(ref: Ref) -> str:
    """The part of an entry that lists its authors (before its year, when that comes early)."""
    from .references import _strip_latex

    text = _fold(_strip_latex(ref.authors))
    year = _YEAR.search(text)
    if year and year.start() < len(text) * 0.6 and year.start() > 10:
        return text[: year.start()]
    return text


def is_self_citation(ref: Ref, authors: list[tuple[str, str, str]]) -> bool:
    text = _author_part(ref)
    for _, surname, initial in authors:
        s = re.escape(surname)
        if not re.search(rf"(?<![A-Za-z]){s}(?![a-z])", text):
            continue
        if not initial:
            return True
        i = re.escape(initial)
        # "Rich, J." / "Rich J" / "Rich, Joseph" | "J. Rich" / "J Rich" / "Joseph Rich" / "J. A. Rich"
        if re.search(rf"(?<![A-Za-z]){s}\s*,?\s+{i}", text) or re.search(rf"(?<![A-Za-z]){i}[A-Za-z'-]*\.?(?:\s*[A-Z]\.?)*\s+{s}(?![a-z])", text):
            return True
    return False


# --- the checks -----------------------------------------------------------------------------


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    checks.run("bibliography.uncited", "Every reference is cited, and every citation has a reference", REFERENCES, _uncited_check, venue, values)
    checks.run("bibliography.citation_style", "In-text citations match the reference list's style (numbered or author-year)", REFERENCES, _citation_style_check, venue, values)
    checks.run("bibliography.self_citations", "Self-citations within limits (at most 15, and at most 80% of references)", REFERENCES, _self_citation_check, venue, values)
    checks.run("bibliography.entries", "BibTeX entries are complete, closed, and have unique keys, and no work is listed twice", REFERENCES, _bib_entries_check, venue, values)


def _manuscripts(venue: Venue, values: dict[str, str]) -> list[tuple[Field, Path]]:
    from .requirements_check import manuscript_files

    files = manuscript_files(venue, values)
    if not files:
        raise NotApplicable("no manuscript file to read")
    return files


def _list(items: list[str]) -> str:
    shown = ", ".join(items[:MAX_EXAMPLES])
    return shown + (f" and {len(items) - MAX_EXAMPLES} more" if len(items) > MAX_EXAMPLES else "")


def _excerpt(text: str, limit: int = 70) -> str:
    """The opening of a reference entry (its authors and, usually, its title), cut at a word."""
    text = re.sub(r"\s+", " ", _NUMBER_LABEL.sub("", text, count=1)).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."


def _named(ref: Ref, mode: str) -> str:
    """How a finding names an entry: a LaTeX key as is; a numbered or author-year entry with its opening text.

    "[37]" alone does not say which work it is, so the entry's authors and title come with it.
    """
    if mode == "keys":
        return ref.label
    return f'{ref.label} "{_excerpt(ref.authors)}"'


def _uncited_check(venue: Venue, values: dict[str, str]) -> Outcome:
    field, path = _manuscripts(venue, values)[0]
    bib = read_bibliography(path)
    if bib is None:
        raise Unchecked(f"could not find the reference list in {path.name}")
    if bib.cited is None:
        raise Unchecked(f"found no bracketed citations ([1]) in {path.name} to match its {len(bib.refs)} numbered references against (superscript citations cannot be told apart from other numbers in extracted text)")
    issues: list[Issue] = []
    defined = [r.key for r in bib.refs]
    uncited = [_named(r, bib.mode) for r in bib.refs if r.key not in bib.cited]
    if bib.printed_only_cited:
        uncited = []
    # Over half of a long author-year list "uncited" means the list or its citations were misread
    # (the audit of accepted ICLR papers found almost every such flag a parsing error), or the text
    # cites in another style, which bibliography.citation_style reports. A list of names would be noise.
    if bib.mode == "author-year" and len(bib.refs) >= UNHEALTHY_MIN_ENTRIES and len(uncited) > len(bib.refs) * UNHEALTHY_UNCITED_SHARE:
        raise Unchecked(f"could not match the citations in {path.name} to its reference list ({len(uncited)} of {len(bib.refs)} author-year entries unmatched, so the list or its citations were probably misread)")
    if bib.nocite_only:
        n = len(bib.nocite_only)
        issues.append(Issue(WARNING, field.id, f"{n} {'entry is' if n == 1 else 'entries are'} put in the reference list of {path.name} by \\nocite but never cited in the text: {_list(bib.nocite_only)}"))
        uncited = [u for u in uncited if u not in bib.nocite_only]
    if uncited:
        what = "entries" if bib.mode != "numbered" else "references"
        why = f" ({bib.note})" if bib.note else ""
        issues.append(Issue(WARNING, field.id, f"{len(uncited)} of {len(bib.refs)} {what} in the reference list of {path.name} {'is' if len(uncited) == 1 else 'are'} never cited in the text{why}: {_list(uncited)}"))
    if bib.mode in ("keys", "numbered", "labels"):
        known = set(defined)
        undefined = sorted(bib.cited - known, key=lambda k: (int(k) if k.isdigit() else 0, k))
        if undefined:
            shown = [f"[{k}]" if bib.mode != "keys" else k for k in undefined]
            issues.append(Issue(WARNING, field.id, f"{path.name} cites {len(undefined)} reference(s) that {'are' if len(undefined) > 1 else 'is'} not in its reference list: {_list(shown)}"))
    via = f" ({bib.note})" if bib.note and bib.mode == "numbered" else ""
    if issues:
        return Outcome(issues)
    if bib.printed_only_cited:
        return Outcome(issues, f"{len(bib.cited & set(defined))} cited keys, all in the .bib (BibTeX prints only cited entries)")
    return Outcome(issues, f"all {len(bib.refs)} references cited{via}")


# Fewer citations of the other style than this are not enough to call the text's style.
MIN_STYLE_CITATIONS = 3
_LIST_STYLE = {"numbered": "numbered", "labels": "alphabetic-label", "author-year": "author-year"}
_STYLE_EXAMPLE = {"numbered": "[1]", "labels": "[Smi20]", "author-year": "(Smith et al., 2020)"}


def _citation_style_check(venue: Venue, values: dict[str, str]) -> Outcome:
    """Whether the text cites in the style of the reference list (numbered, alphabetic labels, or author-year)."""
    from .requirements_check import _AUTHOR_YEAR_CITE

    field, path = _manuscripts(venue, values)[0]
    doc = document.get(path)
    if doc.is_tex and doc.tex_source is not None and _tex_bibliography(path, doc.tex_source) is not None:
        raise NotApplicable(f"{path.name} is LaTeX source: its bibliography style prints the citations and the list alike")
    bib = read_bibliography(path)
    if bib is None or doc.text is None:
        raise Unchecked(f"could not find the reference list in {path.name}")
    parts = _split(doc.text)
    body = parts[0] if parts else doc.text
    numbered = citation_groups(body, len(bib.refs)) if bib.mode != "numbered" else []
    labels = re.findall(r"\[[A-Z][A-Za-z]{0,6}\+?\d{2}[a-z]?(?:\s*[,;]\s*[A-Z][A-Za-z]{0,6}\+?\d{2}[a-z]?)*\]", body) if bib.mode != "labels" else []
    author_year = _AUTHOR_YEAR_CITE.findall(body) if bib.mode != "author-year" else []
    own = len((bib.cited or set()) & {r.key for r in bib.refs})
    found = [(style, n) for style, n in (("numbered", len(numbered)), ("labels", len(labels)), ("author-year", len(author_year))) if n >= MIN_STYLE_CITATIONS]
    # The other style must outnumber the entries the list's own style cites: a few "(Smith,
    # 2020)" beside a fully cited numbered list are prose ("as Smith (2020) showed [4]").
    found = [(style, n) for style, n in found if n > own]
    listed = f"{len(bib.refs)} {_LIST_STYLE[bib.mode]} references"
    if not found:
        return Outcome([], f"citations match the {listed} ({own} cited in that style)")
    style, n = max(found, key=lambda x: x[1])
    return Outcome([Issue(WARNING, field.id, f"{path.name} cites {_LIST_STYLE[style]} style ({n} citations like '{_STYLE_EXAMPLE[style]}') but its reference list is {_LIST_STYLE[bib.mode]} ({listed}, {own} cited like '{_STYLE_EXAMPLE[bib.mode]}'); cite in the reference list's style, or reformat the list")])


# --- one work listed twice ------------------------------------------------------------------

# A title shorter than this (normalized) is too generic to say two entries are one work.
MIN_DUPLICATE_TITLE_CHARS = 12
# A reference-list entry shorter than this (normalized words) is too little text to compare.
MIN_DUPLICATE_ENTRY_WORDS = 6


def _bib_first_surname(author: str) -> str:
    """The first author's surname in a BibTeX ``author`` field ("Smith, J. and ..." or "John Smith and ...")."""
    from .references import _normalize_text

    first = re.split(r"\s+and\s+", author.strip(), maxsplit=1)[0]
    name = first.split(",", 1)[0] if "," in first else first
    words = _normalize_text(name).split()
    return words[-1] if words else ""


def duplicate_entry_findings(entries: list) -> list:
    """Two printed ``.bib`` entries, under different keys, for one work: the same title and first author.

    The DOI and arXiv checks catch a duplicate only when both entries carry the
    identifier; a book or an older article usually has neither, so the title is
    compared instead. Two volumes or editions of one book (``volume``,
    ``edition`` differing) are different works and are not reported.
    """
    from .references import _normalize_text
    from .sensitive import Finding

    findings = []
    groups: dict[tuple[str, str], list] = {}
    seen_keys: set[str] = set()
    for entry in entries:
        key = (entry.key or "").lower()
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        title = _normalize_text(entry.title)
        surname = _bib_first_surname(entry.author or entry.fields.get("editor", ""))
        if len(title) < MIN_DUPLICATE_TITLE_CHARS or not surname:
            continue
        groups.setdefault((title, surname), []).append(entry)
    for group in groups.values():
        first = group[0]
        for other in group[1:]:
            if any(_normalize_text(first.fields.get(f, "")) != _normalize_text(other.fields.get(f, "")) and first.fields.get(f, "").strip() and other.fields.get(f, "").strip() for f in ("volume", "edition", "number", "part")):
                continue
            years = "" if first.year == other.year or not (first.year and other.year) else f" (years {first.year} and {other.year})"
            title = re.sub(r"[{}]", "", first.title).strip()
            findings.append(Finding(other.where, "duplicate reference", f"bibliography entries '{first.key}' and '{other.key}' are the same work (\"{title[:80]}\", {first.author.split(' and ')[0].strip('{} ')}){years}; it is listed twice in the reference list, under two numbers or labels. Cite one key and delete the other"))
    return findings


def _entry_words(text: str) -> list[str]:
    """A reference-list entry as comparable words, without its number and with "2020a"/"2020b" read as 2020."""
    from .references import _normalize_text

    text = _NUMBER_LABEL.sub("", text, count=1)
    # hyperref's back-references: the pages citing the entry, after its closing period ("... 2009. 3, 5").
    text = re.sub(r"(?<=\.)\s+\d{1,4}(?:\s*[,\u2013-]\s*\d{1,4})*\s*$", "", text)
    text = re.sub(r"\b((?:19|20)\d{2})[a-z]\b", r"\1", text)
    return _normalize_text(text).split()


def duplicate_ref_findings(refs: list[Ref], where: str) -> list:
    """Two entries of a printed reference list with the same text: one work listed twice.

    Read from the manuscript itself (a PDF, Word file, or ``\\bibitem`` list),
    where there are no fields to compare: the entries' words, without the
    number and the "a"/"b" an author-year style adds to tell the two apart,
    must be identical.
    """
    from .sensitive import Finding

    findings = []
    seen: dict[tuple[str, ...], Ref] = {}
    for ref in refs:
        words = tuple(_entry_words(ref.authors))
        if len(words) < MIN_DUPLICATE_ENTRY_WORDS:
            continue
        first = seen.setdefault(words, ref)
        if first is not ref:
            findings.append(Finding(where, "duplicate reference", f'reference-list entries {first.label} and {ref.label} are the same work ("{_excerpt(ref.authors, 90)}"); it is listed twice in the reference list'))
    return findings


def _bib_entries_check(venue: Venue, values: dict[str, str]) -> Outcome:
    """Structural problems in the submission's ``.bib`` files (see :func:`paperpush.references.bib_structure_findings`),
    and one work listed twice -- in the ``.bib``, or, without one, in the manuscript's printed reference list."""
    from . import references
    from .validate import _findings_to_issues, _iter_upload_paths

    paths = list(_iter_upload_paths(venue, values))
    findings, total = references.bib_structure_findings(paths)
    if total:
        findings += duplicate_entry_findings(references.printed_entries(paths))
        return Outcome(_findings_to_issues(findings), f"{total} entries read")
    try:
        _, path = _manuscripts(venue, values)[0]
    except NotApplicable:
        raise NotApplicable("no .bib file or manuscript among the uploads") from None
    bib = read_bibliography(path)
    if bib is None or not bib.refs:
        raise NotApplicable("no .bib file among the uploads, and no reference list read from the manuscript")
    findings = duplicate_ref_findings(bib.refs, path.name)
    return Outcome(_findings_to_issues(findings), f"{len(bib.refs)} reference-list entries read" + ("" if findings else ", none listed twice"))


def _self_citation_check(venue: Venue, values: dict[str, str]) -> Outcome:
    authors = _author_names(venue, values)
    if not authors:
        raise NotApplicable("no author names in the .sub")
    field, path = _manuscripts(venue, values)[0]
    bib = read_bibliography(path)
    if bib is None:
        raise Unchecked(f"could not find the reference list in {path.name}")
    refs = bib.refs
    if bib.printed_only_cited and bib.cited is not None:
        refs = [r for r in refs if r.key in bib.cited]
    if not refs:
        raise NotApplicable(f"no references in {path.name}")
    own = [r for r in refs if is_self_citation(r, authors)]
    share = len(own) / len(refs)
    summary = f"{len(own)} of {len(refs)} references ({100 * share:.0f}%) include an author of this paper"
    too_many = len(own) > MAX_SELF_CITATIONS
    too_large = len(refs) >= MIN_REFERENCES_FOR_SHARE and share > MAX_SELF_CITATION_SHARE
    if too_many or too_large:
        limit = f"more than {MAX_SELF_CITATIONS}" if too_many else f"more than {MAX_SELF_CITATION_SHARE:.0%} of the list"
        return Outcome([Issue(SUGGESTION, field.id, f"{summary} ({limit}); editors and reviewers may see this as excessive self-citation: {_list([r.label for r in own])}")])
    return Outcome([], summary)


__all__ = ["Bibliography", "Ref", "docx_superscript_numbers", "is_self_citation", "read_bibliography", "run_checks"]
