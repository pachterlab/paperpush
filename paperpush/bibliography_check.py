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
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import document
from .database import Field, Venue
from .validate import WARNING, Checks, Issue, NotApplicable, Outcome, Unchecked

logger = logging.getLogger(__name__)

REFERENCES = "References"
MAX_EXAMPLES = 8
# More self-citations than this is reported, whatever the length of the list.
MAX_SELF_CITATIONS = 15
# ... as is a larger share than this of a list of at least MIN_REFERENCES_FOR_SHARE.
MAX_SELF_CITATION_SHARE = 0.8
MIN_REFERENCES_FOR_SHARE = 5


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
    items = _BIBITEM.findall(source)
    if items:
        refs = [Ref(key.strip(), key.strip(), re.sub(r"\s+", " ", body).strip()) for key, body in items]
        return Bibliography("keys", refs, cited | nocite)
    bibs: list[Path] = []
    for m in _BIB_NAMES.finditer(source):
        for name in _keys(m.group(1)):
            bibs.append(path.parent / (name if name.lower().endswith(".bib") else name + ".bib"))
    sources = [p for p in bibs if p.is_file()]
    if path.suffix.lower() == ".zip":
        sources.append(path)
    entries = references.collect_entries(sources) if sources else []
    if not entries:
        return None
    refs = [Ref(e.key, e.key, e.author, e.year) for e in entries]
    every = "*" in nocite
    return Bibliography("keys", refs, cited | (nocite - {"*"}), printed_only_cited=not every, note="\\nocite{*} prints every .bib entry" if every else "")


# --- text reference lists ----------------------------------------------------------------

_NUMBER_LABEL = re.compile(r"^\s*(?:\[(\d{1,4})\]|(\d{1,4})[.)])\s")
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


def _split(text: str) -> tuple[str, str] | None:
    """``(body, reference list)`` of ``text``: the reference list, and everything else (legends after it are kept)."""
    from .manuscript import _AFTER_REFERENCES, _REFERENCE_HEADING

    heading = _REFERENCE_HEADING.search(text)
    if heading is None:
        return None
    tail = text[heading.end() :]
    end = _AFTER_REFERENCES.search(tail)
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


_AUTHOR_YEAR_HEAD = re.compile(r"^[^.]{0,250}?(?:\.[^.]{0,60}?){0,8}?[.,]\s*\(?(?:19|20)\d{2}[a-z]?\)?[.,:]")


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
    head = re.split(r",| and |\.\s+(?=\(?(?:19|20)\d{2})", _NUMBER_LABEL.sub("", entry, count=1), maxsplit=1)[0]
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
    if len(numbered) >= 2:
        refs = [Ref(n, f"[{n}]", entry) for n, entry in _entries(tail, numbered)]
        last = max(int(n) for n, _ in _entries(tail, numbered))
        cited = {str(n) for g in citation_groups(body, last) for n in _numbers(g)}
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

    starts = author_year_starts(tail)
    if not starts:
        return None
    entries: list[str] = []
    for a, b in zip(starts, starts[1:] + [len(tail)]):
        entry = re.sub(r"\s+", " ", tail[a:b]).strip()
        # A real entry opens with its authors and year ("..., Name. 2015.", "Smith, J. (2015)."); a
        # line that does not is the previous entry running on (a venue, "Preprint, arXiv:...").
        if entries and (len(entry) < 25 or not _AUTHOR_YEAR_HEAD.search(entry[:300])):
            entries[-1] += " " + entry
        elif entry:
            entries.append(entry)
    folded = _unwrap(_fold(body))
    folded_tail = _unwrap(_fold(tail))
    refs: list[Ref] = []
    cited: set[str] = set()
    for entry in entries:
        year = _YEAR.search(entry)
        candidates = _surname_candidates(entry)
        if not candidates or year is None:
            continue
        suffix = year.group(2) or "[a-z]?"
        hit = next((c for c in candidates if re.search(rf"(?<![A-Za-z]){re.escape(_fold(c))}(?![a-z])[^;\n]{{0,80}}?\b{year.group(1)}{suffix}(?![\da-z])", folded)), None)
        if not hit:
            # An appendix after the reference list cites in the same shape: "Boteva et al., 2016", "Holm (2024)".
            hit = next((c for c in candidates if re.search(rf"(?<![A-Za-z]){re.escape(_fold(c))}(?: et al\.?| (?:and|&) [A-Z][\w-]+)?,? \(?{year.group(1)}{suffix}(?![\da-z])", folded_tail)), None)
        surname = hit or candidates[0]
        key = f"{surname} {year.group(0)}"
        refs.append(Ref(key, f"{surname} ({year.group(0)})", entry, year.group(0)))
        if hit:
            cited.add(key)
    return Bibliography("author-year", refs, cited) if refs else None


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
    checks.run("bibliography.self_citations", "Self-citations within limits (at most 15, and at most 80% of references)", REFERENCES, _self_citation_check, venue, values)
    checks.run("bibliography.entries", "BibTeX entries are complete, closed, and have unique keys", REFERENCES, _bib_entries_check, venue, values)


def _manuscripts(venue: Venue, values: dict[str, str]) -> list[tuple[Field, Path]]:
    from .requirements_check import manuscript_files

    files = manuscript_files(venue, values)
    if not files:
        raise NotApplicable("no manuscript file to read")
    return files


def _list(items: list[str]) -> str:
    shown = ", ".join(items[:MAX_EXAMPLES])
    return shown + (f" and {len(items) - MAX_EXAMPLES} more" if len(items) > MAX_EXAMPLES else "")


def _uncited_check(venue: Venue, values: dict[str, str]) -> Outcome:
    field, path = _manuscripts(venue, values)[0]
    bib = read_bibliography(path)
    if bib is None:
        raise Unchecked(f"could not find the reference list in {path.name}")
    if bib.cited is None:
        raise Unchecked(f"found no bracketed citations ([1]) in {path.name} to match its {len(bib.refs)} numbered references against (superscript citations cannot be told apart from other numbers in extracted text)")
    issues: list[Issue] = []
    defined = [r.key for r in bib.refs]
    uncited = [r.label for r in bib.refs if r.key not in bib.cited]
    if bib.printed_only_cited:
        uncited = []
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


def _bib_entries_check(venue: Venue, values: dict[str, str]) -> Outcome:
    """Structural problems in the submission's ``.bib`` files (see :func:`paperpush.references.bib_structure_findings`)."""
    from . import references
    from .validate import _findings_to_issues, _iter_upload_paths

    paths = list(_iter_upload_paths(venue, values))
    findings, total = references.bib_structure_findings(paths)
    if not total:
        raise NotApplicable("no .bib file among the uploads")
    return Outcome(_findings_to_issues(findings), f"{total} entries read")


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
        return Outcome([Issue(WARNING, field.id, f"{summary} ({limit}); editors and reviewers may see this as excessive self-citation: {_list([r.label for r in own])}")])
    return Outcome([], summary)


__all__ = ["Bibliography", "Ref", "docx_superscript_numbers", "is_self_citation", "read_bibliography", "run_checks"]
