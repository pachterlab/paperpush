"""Check a submission's bibliography against the DOI registry.

A manuscript's reference list is one of the few parts of a submission nobody
proofreads: entries are pasted in from a publisher page, a Google Scholar
"Cite" box, or an older ``.bib`` that has drifted, and a wrong ``doi`` field
survives every spell-check and compile. The result is a citation that renders
correctly but points somewhere else -- the classic failure being a DOI copied
from the row above, so the reference reads as one paper and resolves to
another.

This module is the bibliography back end for ``paperpush validate`` (which runs
it by default; ``--dont-check-references`` opts out). It does what `doi2bib
<https://doi2bib.org>`_ does -- ask ``doi.org`` what a DOI actually refers to --
and then compares that against what the reference claims:

* the DOI is well-formed (``10.NNNN/suffix``) and not cited by two references;
* it resolves at all (a 404 means the citation points at nothing);
* the registered title, first author, and year match the reference's.

The reference is read from whichever of the two forms a submission ships, since
most ship only one:

* a **BibTeX entry**, when the submission includes a ``.bib``. Its ``doi``,
  ``title``, ``author``, and ``year`` are separate labelled fields, so a
  disagreement can be pinned to a field and named by citation key.
* the **manuscript's own reference list** -- a PDF's extracted text, LaTeX
  source, a compiled ``.bbl`` -- where the title and authors have already been
  rendered into a sentence. There the registered record is held against the text
  of the entry the DOI sits in, which catches the same error without needing the
  fields. Only DOIs inside the reference list are compared this way: one in a
  data-availability sentence has no bibliographic text beside it, and is merely
  resolved.

Resolution uses DOI content negotiation: a request to ``https://doi.org/<doi>``
with an ``Accept: application/vnd.citationstyles.csl+json`` header, which the
registration agency (Crossref, DataCite, mEDRA, ...) answers with CSL JSON.
That is the same endpoint doi2bib asks for BibTeX from; CSL JSON is requested
instead because its fields are already separated, so no second BibTeX parse
stands between the response and the comparison.

Every finding is advisory, matching :mod:`paperpush.sensitive`: the point is to
show the author which citations look wrong so they can check them, not to block
a submission on a metadata record that may itself be imperfect. Comparisons are
deliberately loose (see :data:`TITLE_MATCH_RATIO` and :data:`YEAR_TOLERANCE`) so
routine differences -- LaTeX markup, a subtitle, an online-first year -- stay
quiet and only a genuinely different work is reported.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .sensitive import ARCHIVE_EXTS, MAX_ARCHIVE_MEMBERS, MAX_TEXT_BYTES, Finding, _decode, _dedup_paths, _iter_archive_members, _iter_text_blobs

logger = logging.getLogger(__name__)

# Cap the number of distinct DOIs we resolve over the network, so a review
# article with 300 references can't turn validation into a crawl. Reported when
# hit (see :func:`_truncation_finding`) so the truncation is never silent.
MAX_DOI_CHECKS = 100
# Resolutions are pure network waits, so they overlap well; this many run at once.
DOI_CHECK_WORKERS = 8
# Per-request budget. doi.org redirects to the registration agency, so this
# covers two hops; still short, because an inconclusive answer tells us nothing
# and waiting longer only slows validation down.
DOI_CHECK_TIMEOUT = 6.0
# 404/410 are the only codes that mean "no such DOI". Others (403 bot blocks,
# 406 no-CSL-for-this-agency, 5xx blips, timeouts) are inconclusive -> stay quiet.
_MISSING_STATUS = {404, 410}

# How similar a normalized entry title must be to the registered one before the
# pair is treated as the same work. Well below 1.0 because a .bib title
# legitimately differs in markup, subtitle, and series suffix; well above chance
# so two unrelated papers never match.
TITLE_MATCH_RATIO = 0.82
# Years may differ by this much without comment: online-first publication
# routinely straddles a year boundary, so the entry and the registry disagree by
# one without either being wrong.
YEAR_TOLERANCE = 1

# Outcome of asking doi.org about a DOI.
DOI_FOUND = "found"  # resolved; CSL JSON metadata returned
DOI_MISSING = "missing"  # 404/410 -- the DOI is not registered
DOI_UNKNOWN = "unknown"  # offline, blocked, or an agency that won't serve CSL

# A DOI as registered: the "10." prefix, a 4+ digit registrant code, then a
# suffix of any non-space characters. Deliberately permissive about the suffix,
# which has no standard shape beyond "not empty".
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
# Prefixes an author pastes in front of a DOI when copying it from a page.
_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:", "doi.org/")
# Punctuation a DOI picks up from the sentence or BibTeX field it was pasted into.
_DOI_TRAILING = ".,;:)]}'\"" + "\u201d\u2019"

# A BibTeX entry header: @type followed by the opening delimiter. @comment,
# @preamble, and @string are not references and are skipped.
_ENTRY_START = re.compile(r"@(\w+)\s*([{(])")
_NON_ENTRY_TYPES = {"comment", "preamble", "string"}
# A field assignment inside an entry body: name = <value>.
_FIELD_NAME = re.compile(r"([A-Za-z][A-Za-z0-9_+:-]*)\s*=\s*")
# " and " as BibTeX's author separator: surrounded by whitespace, so a surname
# like "Marchand" is not split.
_AUTHOR_SPLIT = re.compile(r"\s+and\s+", re.IGNORECASE)
# LaTeX accent markup, which a .bib uses where the registry sends a composed
# character: the punctuation accents (``\"u``, ``\'e``) and the one-letter
# commands that take a braced argument (``\c{c}``, ``\v{s}``). Both are dropped
# without leaving a space, so ``Gr{\"u}nbaum`` and ``Fran\c{c}ois`` fold to one
# word rather than two. The lookaheads keep ``\citep`` and ``\vspace`` out of it.
_TEX_ACCENT = re.compile(r"\\[`'^~\"=.](?=[A-Za-z{])|\\[cvuHdbrkt](?=\s*\{)")
# Any other LaTeX control word (``\emph``, ``\textbf``), which separates words
# and so becomes a space.
_TEX_COMMAND = re.compile(r"\\[A-Za-z]+")
# An escaped character (``\&``, ``\_``), which stands for the character itself.
_TEX_ESCAPE = re.compile(r"\\(.)")
# Any four-digit year a date-ish field might carry.
_YEAR_RE = re.compile(r"\b(1[0-9]{3}|2[01][0-9]{2})\b")

# Bibliography files, parsed entry by entry: a .bib is the one place a
# reference's DOI, title, author, and year sit in separate, labelled fields, so
# a mismatch can be attributed to a field. Everything else the submission ships
# -- the manuscript PDF, its LaTeX source, a compiled .bbl -- is read as prose
# by the citation pass below instead.
BIB_EXTS = {".bib"}

# A DOI written into running text. Unlike a .bib ``doi`` field this has no
# delimiters, so the match stops at whitespace and at the punctuation a
# reference list puts after a DOI; normalize_doi trims whatever else trails.
_DOI_IN_TEXT = re.compile(r"\b10\.\d{4,9}/[^\s<>\"'\]}),;]*")
# A DOI broken across a line by the PDF's line-wrapping: the rest of it is the
# run of DOI-legal characters that opens the next line. Used to rebuild the
# whole DOI before deciding a citation points at nothing (see
# :func:`_resolve_with_fallback`), since the truncated half would 404 and read
# as a broken citation when nothing is wrong with it.
# ``Pattern.match(text, pos)`` already anchors at pos, and \A would not (it
# stays pinned to the real start of the string), so this carries no anchor.
_DOI_CONTINUATION = re.compile(r"[ \t]*\r?\n[ \t]*([A-Za-z0-9._;()/:+-]+)")
# A hyphen the layout inserted at a line break, rejoined before the surrounding
# reference text is compared against the registered record.
_LINE_WRAP_HYPHEN = re.compile(r"-\s*\r?\n\s*")

# Where one reference entry ends and the next begins: a \bibitem, a numbered
# marker opening a line ("[12]", "12.", "(12)"), or a blank line. Anchoring the
# comparison to the entry a DOI actually sits in is what makes the check work at
# all -- a fixed window would reach into the neighbouring entry, and the
# neighbour is exactly where a DOI pasted from the row above came from, so its
# title would "confirm" the very error being looked for.
# ``\bibitem`` is followed by its key rather than a space, so only the numbered
# markers carry the trailing-whitespace requirement that keeps them from firing
# mid-entry.
_ENTRY_BOUNDARY = re.compile(r"(?m)^[ \t]*(?:\\bibitem\b|(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])\s)|\n[ \t]*\n")
# Hard caps on that entry, for a reference list whose entries carry no marker at
# all. A reference entry runs 150-300 characters, so these hold a whole typical
# one without running off across the page.
CONTEXT_BEFORE = 400
CONTEXT_AFTER = 160
# Fraction of a registered title's significant words that must appear in that
# text before the citation counts as matching it. Well below 1.0 because PDF
# text extraction drops and mangles words; the author-name signal below covers
# the numeric styles that print no title at all.
TITLE_CONTEXT_OVERLAP = 0.6
# Title words shorter than this carry too little signal to match on ("the",
# "of", "a"), so they are left out of the overlap.
_SIGNIFICANT_WORD_LEN = 4


@dataclass(frozen=True)
class Entry:
    """One parsed bibliography entry.

    ``key`` is the citation key (``lovelace1843``) and ``where`` the file it was
    read from, both used to name the entry in a finding. ``doi`` is normalized
    (bare ``10.x/y``, lower-cased) while ``raw_doi`` preserves what the file
    actually said, so a message can quote the author's own text. The remaining
    fields are the entry's claims, still in BibTeX form -- normalization happens
    at comparison time.
    """

    key: str
    where: str
    raw_doi: str
    doi: str
    title: str
    author: str
    year: str


@dataclass(frozen=True)
class Citation:
    """One DOI found in the submission's prose rather than in a ``.bib``.

    This is what a manuscript offers when it ships as a PDF, a ``.tex``, or a
    compiled ``.bbl``: the DOI is there, but the title, authors, and year around
    it have already been rendered into a sentence. ``context`` is that sentence
    -- the text surrounding the DOI, normalized for comparison -- and
    ``in_reference_list`` says whether the DOI sits after the reference heading,
    which is what makes that context a bibliography entry worth comparing rather
    than a passing mention of a dataset.

    ``continuation`` holds the DOI rebuilt across a line break, when the file
    wrapped one; :func:`_resolve_with_fallback` falls back to it so a wrapped
    DOI is not mistaken for a dead one.
    """

    doi: str
    where: str
    context: str
    in_reference_list: bool
    continuation: str = ""


# --- DOI shapes ------------------------------------------------------------


def normalize_doi(raw: str) -> str:
    """Reduce a written DOI to its bare, comparable form.

    Strips a resolver prefix (``https://doi.org/``, ``doi:``), surrounding
    whitespace and BibTeX braces, and trailing sentence punctuation, then
    lower-cases the result: DOIs are case-insensitive, so two entries citing the
    same work in different cases must compare equal. Returns ``""`` for a value
    that holds no DOI-ish text at all.
    """
    value = raw.strip().strip("{}").strip()
    for prefix in _DOI_PREFIXES:
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
            break
    value = value.strip().rstrip(_DOI_TRAILING)
    # \_ and friends: a DOI written into LaTeX often escapes its own punctuation.
    value = value.replace("\\_", "_").replace("\\%", "%").replace("\\&", "&").replace("\\#", "#")
    return value.lower()


def _is_well_formed(doi: str) -> bool:
    """Whether ``doi`` has the registered ``10.NNNN/suffix`` shape."""
    return bool(_DOI_RE.match(doi))


# --- BibTeX parsing --------------------------------------------------------
#
# A hand-rolled parser rather than a dependency: the checks below need exactly
# four fields (doi, title, author, year) off each entry, and the shapes that
# reach a real submission's .bib are narrow -- brace- or quote-delimited values,
# nested braces, escaped delimiters. What this deliberately does not implement
# is @string macro expansion and # concatenation; a value built that way is read
# as its literal text, which at worst leaves a field looking unset and the entry
# unchecked. It never invents a mismatch.


def _match_delimiter(text: str, start: int, opener: str) -> int:
    """Index of the delimiter closing the one at ``start``, or -1 if unclosed.

    Tracks nesting so a braced group inside a value (``{Notes on the {Analytical}
    Engine}``) closes at the right place, and skips the character after a
    backslash so an escaped delimiter does not count.
    """
    closer = "}" if opener == "{" else ")"
    depth = 0
    i = start
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _read_value(body: str, start: int) -> tuple[str, int]:
    """Read the field value beginning at ``start``; return it and the index after it.

    Handles the three BibTeX value forms: a braced group, a quoted string, and a
    bare token (a number, or an unexpanded @string macro name) that runs to the
    next comma.
    """
    if start >= len(body):
        return "", start
    char = body[start]
    if char == "{":
        end = _match_delimiter(body, start, "{")
        if end == -1:
            return body[start + 1 :], len(body)
        return body[start + 1 : end], end + 1
    if char == '"':
        i = start + 1
        depth = 0
        while i < len(body):
            if body[i] == "\\":
                i += 2
                continue
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
            elif body[i] == '"' and depth == 0:
                return body[start + 1 : i], i + 1
            i += 1
        return body[start + 1 :], len(body)
    end = body.find(",", start)
    if end == -1:
        return body[start:].strip(), len(body)
    return body[start:end].strip(), end


def _parse_entry_body(body: str) -> tuple[str, dict[str, str]]:
    """Split an entry's body into its citation key and its field mapping.

    ``body`` is the text between the entry's delimiters: the key up to the first
    comma, then ``name = value`` pairs. Field names are lower-cased, so ``DOI``
    and ``doi`` land in the same slot.
    """
    head, sep, rest = body.partition(",")
    key = head.strip()
    if not sep:
        return key, {}

    fields: dict[str, str] = {}
    pos = 0
    while True:
        match = _FIELD_NAME.search(rest, pos)
        if match is None:
            break
        value, pos = _read_value(rest, match.end())
        fields.setdefault(match.group(1).lower(), value.strip())
        # Skip the separating comma so the next name is found from a clean start.
        while pos < len(rest) and rest[pos] in ", \t\r\n":
            pos += 1
    return key, fields


def parse_bibtex(text: str, where: str = "") -> list[Entry]:
    """Parse every reference entry in a BibTeX document, in file order.

    ``@comment`` / ``@preamble`` / ``@string`` blocks are skipped: they are not
    references. A truncated final entry (an unclosed brace) is parsed from what
    is there rather than dropped, so a partial file still yields its earlier,
    complete entries.
    """
    entries: list[Entry] = []
    pos = 0
    while True:
        match = _ENTRY_START.search(text, pos)
        if match is None:
            break
        opener = match.group(2)
        end = _match_delimiter(text, match.end() - 1, opener)
        body_end = end if end != -1 else len(text)
        pos = body_end + 1
        if match.group(1).lower() in _NON_ENTRY_TYPES:
            continue
        key, fields = _parse_entry_body(text[match.end() : body_end])
        raw_doi = fields.get("doi", "")
        entries.append(
            Entry(
                key=key,
                where=where,
                raw_doi=raw_doi,
                doi=normalize_doi(raw_doi),
                title=fields.get("title", ""),
                author=fields.get("author", ""),
                year=fields.get("year", "") or fields.get("date", ""),
            )
        )
    return entries


def _iter_bib_blobs(paths: Iterable[Path]) -> Iterator[tuple[str, str]]:
    """Yield ``(where, text)`` for every bibliography file among ``paths``.

    Covers a ``.bib`` listed directly by a field and one bundled inside a source
    archive (``source.zip:ref.bib``), which is how a LaTeX submission usually
    ships its bibliography. This is the mirror image of
    :func:`paperpush.sensitive._iter_text_blobs`, which skips ``.bib`` files
    precisely because their URLs belong to the cited works rather than to this
    submission -- here the cited works are the point.
    """
    for path in _dedup_paths(paths):
        try:
            suffix = path.suffix.lower()
            double = "".join(path.suffixes[-2:]).lower()
            if suffix in ARCHIVE_EXTS or double in ARCHIVE_EXTS:
                try:
                    members = _iter_archive_members(path)
                except Exception:
                    logger.debug("could not open archive %s for reference scan; skipping", path, exc_info=True)
                    continue
                for count, (name, data) in enumerate(members):
                    if count >= MAX_ARCHIVE_MEMBERS:
                        break
                    if Path(name).suffix.lower() not in BIB_EXTS or len(data) > MAX_TEXT_BYTES:
                        continue
                    text = _decode(data)
                    if text is not None:
                        yield f"{path.name}:{name}", text
            elif suffix in BIB_EXTS:
                data = path.read_bytes()
                if len(data) <= MAX_TEXT_BYTES:
                    text = _decode(data)
                    if text is not None:
                        yield path.name, text
        except OSError:
            logger.debug("could not read %s for reference scan", path, exc_info=True)


def collect_entries(paths: Iterable[Path]) -> list[Entry]:
    """Every bibliography entry reachable from the submission's upload files."""
    entries: list[Entry] = []
    for where, text in _iter_bib_blobs(paths):
        entries.extend(parse_bibtex(text, where))
    return entries


# --- reading DOIs out of the manuscript itself -----------------------------
#
# Most submissions never ship a .bib. The manuscript arrives as a PDF, or as
# LaTeX with a compiled .bbl, and its reference list is already prose -- so the
# DOIs are still there to check, but the title/author/year they should agree
# with are in the sentence around them rather than in labelled fields. The pass
# below reads those DOIs and keeps that sentence, so a DOI can still be held
# against the work it resolves to.


def _reference_list_start(text: str) -> int | None:
    """Offset where the reference list begins in ``text``, or ``None``.

    Recognises a standalone "References"/"Bibliography" heading (reusing
    :mod:`paperpush.manuscript`, which already locates it to measure main-text
    length), a LaTeX ``\\section{References}``, and ``\\begin{thebibliography}``
    / ``\\bibliography``, which is what a compiled ``.bbl`` opens with. The
    earliest wins.

    This is what separates a bibliography entry from a passing mention: a DOI in
    a data-availability sentence has no title or author next to it, so comparing
    it against its registered record would flag every correctly cited dataset.
    Only DOIs after this offset are compared; the rest are merely resolved.
    """
    from . import manuscript

    found = (
        manuscript.main_text_end(text),
        manuscript._TEX_BIBLIOGRAPHY.search(text),
        manuscript._TEX_REFERENCE_SECTION.search(text),
    )
    starts = [m.start() for m in found if m is not None]
    return min(starts) if starts else None


def _entry_context(text: str, refs_at: int | None, start: int, end: int) -> str:
    """The single reference entry a DOI sits in, normalized for comparison.

    Walks out from the DOI to the nearest entry boundary on either side, so the
    comparison never crosses into the neighbouring reference and never reaches
    back above the reference heading. An entry with no marker to bound it falls
    back to :data:`CONTEXT_BEFORE` / :data:`CONTEXT_AFTER` characters.

    Words the layout hyphenated across a line break are rejoined before
    normalizing, so a title split as ``Intelli-`` / ``gence`` still matches the
    registered one.
    """
    floor = refs_at if refs_at is not None else 0
    begin = max(floor, start - CONTEXT_BEFORE)
    finish = min(len(text), end + CONTEXT_AFTER)
    # The last boundary before the DOI opens its entry; the first after closes it.
    for match in _ENTRY_BOUNDARY.finditer(text, begin, start):
        begin = match.end()
    closing = _ENTRY_BOUNDARY.search(text, end, finish)
    if closing is not None:
        finish = closing.start()
    return _normalize_text(_LINE_WRAP_HYPHEN.sub("", text[begin:finish]))


def collect_citations(paths: Iterable[Path]) -> list[Citation]:
    """Every DOI written into the submission's prose, in the order found.

    Reads the text of the manuscript and its companions -- a PDF's extracted
    text, LaTeX source, an archive's members -- via the same iterator the link
    scan uses, which already skips ``.bib`` (handled entry-by-entry above) and
    the ``.cls`` / ``.sty`` / ``.bst`` boilerplate whose DOIs belong to a
    template's author rather than to this submission.

    Each DOI is kept with the entry it sits in and whether that is inside the
    reference list. Repeats are collapsed differently on either side of that
    line: outside the reference list one mention of a DOI is as good as another,
    but *inside* it every occurrence is kept, because a reference list that
    cites one DOI twice has two entries to answer for -- and the second is where
    a DOI pasted from the row above shows up.

    A DOI the file wrapped across a line is recorded with its continuation so it
    can be rebuilt if the truncated form fails to resolve; one broken at the
    slash, which leaves nothing to resolve at all, is rebuilt here.
    """
    citations: list[Citation] = []
    for path in _dedup_paths(paths):
        for where, text in _iter_text_blobs(path):
            refs_at = _reference_list_start(text)
            seen_outside: set[str] = set()
            for match in _DOI_IN_TEXT.finditer(text):
                doi = normalize_doi(match.group(0))
                wrapped = _DOI_CONTINUATION.match(text, match.end())
                # Rebuilt from the raw match, not the normalized DOI: normalization
                # trims the trailing punctuation that a break can fall right after,
                # and that character is part of the DOI when the rest follows it.
                joined = normalize_doi(match.group(0) + wrapped.group(1)) if wrapped else ""
                if not _is_well_formed(doi):
                    # Wrapped at the slash: the continuation is the whole DOI.
                    if not _is_well_formed(joined):
                        continue
                    doi, joined = joined, ""

                in_list = refs_at is not None and match.start() >= refs_at
                if not in_list:
                    if doi in seen_outside:
                        continue
                    seen_outside.add(doi)
                context = _entry_context(text, refs_at, match.start(), match.end())
                citations.append(Citation(doi=doi, where=where, context=context, in_reference_list=in_list, continuation=joined))
    return citations


def _relevant_citations(citations: list[Citation], from_bib: set[str]) -> list[Citation]:
    """Drop the prose DOIs that another check already covers.

    A DOI a ``.bib`` entry describes is left to the field-by-field comparison,
    which is the more precise of the two. A DOI mentioned in the body *and*
    listed in the references is left to its reference-list occurrence, the only
    one carrying text worth comparing.
    """
    in_list = {c.doi for c in citations if c.in_reference_list}
    return [c for c in citations if c.doi not in from_bib and (c.in_reference_list or c.doi not in in_list)]


def _duplicate_citation_findings(citations: list[Citation]) -> list[Finding]:
    """Flag a reference list that cites the same DOI in more than one entry.

    The prose counterpart of the ``.bib`` duplicate check: without citation keys
    to name, the finding counts the entries instead.
    """
    findings: list[Finding] = []
    counts: dict[tuple[str, str], int] = {}
    for citation in citations:
        if citation.in_reference_list:
            counts[(citation.where, citation.doi)] = counts.get((citation.where, citation.doi), 0) + 1
    for (where, doi), count in counts.items():
        if count > 1:
            findings.append(
                Finding(
                    where,
                    "duplicate DOI",
                    f"{count} entries in the reference list cite DOI {doi}; the same work " "is listed (and numbered) more than once, or one of them has the wrong DOI",
                )
            )
    return findings


def _significant_words(title: str) -> list[str]:
    """The words of a title long enough to be worth matching on."""
    return [w for w in _normalize_text(title).split() if len(w) >= _SIGNIFICANT_WORD_LEN]


def _context_supports(citation: Citation, record: dict) -> bool:
    """Whether the text around a DOI looks like the work the DOI resolves to.

    Two independent signals, either of which is enough. The registered first
    author's family name appearing in the reference covers the numeric styles
    that print no article title at all (Nature, Science); enough of the
    registered title's significant words appearing covers everything else, and
    survives the words PDF extraction drops.

    Returns ``True`` when neither signal is available to judge on -- a record
    with no author and no usable title says nothing about the citation, and
    silence is the right answer.
    """
    words = set(citation.context.split())

    family = _normalize_text(_first_author_of(record))
    if family and family in words:
        return True

    title_words = _significant_words(_title_of(record))
    if title_words:
        hits = sum(1 for w in title_words if w in words)
        if hits / len(title_words) >= TITLE_CONTEXT_OVERLAP:
            return True

    return bool(not family and not title_words)


def _describe(record: dict) -> str:
    """How a finding names the work a DOI actually resolves to."""
    title = _title_of(record).strip()
    author = _first_author_of(record).strip()
    year = _year_of(record).strip()
    parts = [f'"{title}"' if title else "an untitled record"]
    if author:
        parts.append(f"by {author}")
    if year:
        parts.append(f"({year})")
    return " ".join(parts)


# --- registry lookup -------------------------------------------------------


def _resolve_doi(doi: str, *, timeout: float = DOI_CHECK_TIMEOUT) -> tuple[str, dict | None]:
    """Ask doi.org what ``doi`` refers to.

    Returns ``(DOI_FOUND, csl_json)`` when the DOI resolves and its agency
    serves CSL JSON, ``(DOI_MISSING, None)`` only on a 404/410 -- the DOI is not
    registered -- and ``(DOI_UNKNOWN, None)`` for everything else (offline,
    timeout, bot block, an agency that answers the redirect but not the content
    negotiation, unparseable JSON), so the caller stays silent rather than
    accusing a perfectly good citation.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    url = "https://doi.org/" + urllib.parse.quote(doi, safe="/:._-();+<>[]#")
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.citationstyles.csl+json",
            "User-Agent": "paperpush-validate (+https://github.com/pachterlab/paperpush)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # scheme is a literal https above  # nosec B310
            payload = resp.read(MAX_TEXT_BYTES)
    except urllib.error.HTTPError as exc:
        if exc.code in _MISSING_STATUS:
            return DOI_MISSING, None
        logger.debug("DOI %s returned HTTP %s; treating as unknown", doi, exc.code)
        return DOI_UNKNOWN, None
    except Exception:
        logger.debug("could not resolve DOI %s", doi, exc_info=True)
        return DOI_UNKNOWN, None

    try:
        record = json.loads(payload.decode("utf-8", "replace"))
    except ValueError:
        logger.debug("DOI %s did not return JSON", doi, exc_info=True)
        return DOI_UNKNOWN, None
    if not isinstance(record, dict):
        return DOI_UNKNOWN, None
    return DOI_FOUND, record


# --- comparing an entry against its registered record ----------------------


def _strip_accents(text: str) -> str:
    """Fold accented characters to their base letters (``Grün`` -> ``Grun``).

    A ``.bib`` writes an accent as LaTeX markup and the registry as a composed
    character; folding both makes the two comparable.
    """
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _strip_latex(value: str) -> str:
    """Turn LaTeX-marked-up text into the plain text it renders as.

    Accents are folded into the letter they sit on, control words become word
    separators, escaped characters stand for themselves, and grouping braces are
    dropped without leaving a gap -- so ``Gr{\\"u}nbaum`` reads as ``Grunbaum``
    and ``\\emph{C. elegans}`` as `` C. elegans``.
    """
    text = _TEX_ACCENT.sub("", value)
    text = _TEX_COMMAND.sub(" ", text)
    text = _TEX_ESCAPE.sub(r"\1", text)
    return text.replace("{", "").replace("}", "")


def _normalize_text(value: str) -> str:
    """Reduce a title (or any prose field) to comparable words.

    Renders away LaTeX markup, folds accents and case, and collapses everything
    that is not a letter or digit to single spaces -- so ``{The}
    \\emph{C. elegans} Genome`` and ``The C. elegans genome`` compare equal.
    """
    text = _strip_accents(_strip_latex(value)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _title_of(record: dict) -> str:
    """The registered title, which CSL may give as a string or a one-item list."""
    title = record.get("title") or record.get("container-title") or ""
    if isinstance(title, list):
        title = title[0] if title else ""
    return title if isinstance(title, str) else ""


def _first_author_of(record: dict) -> str:
    """The registered first author's family name (or a literal/organization name)."""
    authors = record.get("author")
    if not isinstance(authors, list):
        return ""
    for author in authors:
        if not isinstance(author, dict):
            continue
        name = author.get("family") or author.get("literal") or author.get("name") or ""
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def _year_of(record: dict) -> str:
    """The registered publication year, preferring the issued date.

    Falls back to the print and online publication dates, since some agencies
    populate only one of the three.
    """
    for key in ("issued", "published-print", "published-online", "published"):
        parts = (record.get(key) or {}).get("date-parts") if isinstance(record.get(key), dict) else None
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            year = parts[0][0]
            if isinstance(year, int):
                return str(year)
            if isinstance(year, str) and year.strip():
                return year.strip()
    return ""


def _entry_first_author(author_field: str) -> str:
    """The first author's family name as the ``.bib`` entry gives it.

    Handles both BibTeX name orders -- ``Lovelace, Ada`` (family first, comma
    separated) and ``Ada Lovelace`` (family last) -- and returns ``""`` for an
    empty field, leaving the author comparison unmade rather than guessed.
    """
    first = _strip_latex(_AUTHOR_SPLIT.split(author_field.strip(), maxsplit=1)[0]).strip()
    if not first:
        return ""
    if "," in first:
        return first.split(",", 1)[0].strip()
    parts = first.split()
    # "Ludwig van Beethoven": the particle belongs to the family name, but
    # comparing the last token alone is enough to tell two authors apart.
    return parts[-1] if parts else ""


def _year_value(raw: str) -> int | None:
    """The four-digit year in a BibTeX ``year``/``date`` value, if there is one."""
    match = _YEAR_RE.search(raw)
    return int(match.group(1)) if match else None


def _mismatches(entry: Entry, record: dict) -> list[str]:
    """The fields where ``entry`` disagrees with its registered record.

    Each comparison is skipped when either side is blank -- an absent field is
    an incomplete entry, not a wrong one -- and is loose enough that only a
    genuine difference is reported: titles must fall below
    :data:`TITLE_MATCH_RATIO` similarity, years must differ by more than
    :data:`YEAR_TOLERANCE`, and author names are compared on the folded family
    name alone.
    """
    problems: list[str] = []

    entry_title, registered_title = _normalize_text(entry.title), _normalize_text(_title_of(record))
    if entry_title and registered_title:
        ratio = difflib.SequenceMatcher(None, entry_title, registered_title).ratio()
        if ratio < TITLE_MATCH_RATIO:
            problems.append(f'title is "{entry.title.strip()}" but the DOI is registered to "{_title_of(record).strip()}"')

    entry_author = _normalize_text(_entry_first_author(entry.author))
    registered_author = _normalize_text(_first_author_of(record))
    if entry_author and registered_author and entry_author != registered_author:
        problems.append(f"first author is {_entry_first_author(entry.author).strip()} but the DOI is registered to {_first_author_of(record).strip()}")

    entry_year, registered_year = _year_value(entry.year), _year_value(_year_of(record))
    if entry_year and registered_year and abs(entry_year - registered_year) > YEAR_TOLERANCE:
        problems.append(f"year is {entry_year} but the DOI is registered as {registered_year}")

    return problems


# --- findings --------------------------------------------------------------


def _cite(entry: Entry) -> str:
    """How a finding names an entry: its citation key, or its title if unkeyed."""
    if entry.key:
        return f"bibliography entry '{entry.key}'"
    title = entry.title.strip().strip("{}")
    return f'bibliography entry "{title[:60]}"' if title else "a bibliography entry"


def _offline_findings(entries: list[Entry]) -> list[Finding]:
    """Bibliography problems visible without asking the registry anything.

    A DOI that is not shaped like a DOI would never have resolved, and the same
    DOI on two different keys means the reference list cites one work twice --
    which renders as two entries and, in a numbered style, two different numbers
    for the same paper.
    """
    findings: list[Finding] = []
    seen: dict[str, Entry] = {}
    for entry in entries:
        if not entry.doi:
            continue
        if not _is_well_formed(entry.doi):
            findings.append(
                Finding(
                    entry.where,
                    "malformed DOI",
                    f"{_cite(entry)} has DOI '{entry.raw_doi.strip()}', which is not a valid " "DOI (expected the form 10.1234/suffix); it will not resolve for readers",
                )
            )
            continue
        first = seen.setdefault(entry.doi, entry)
        if first is not entry and first.key != entry.key:
            findings.append(
                Finding(
                    entry.where,
                    "duplicate DOI",
                    f"{_cite(entry)} and '{first.key}' both cite DOI {entry.doi}; the same " "work is in the bibliography twice, so it will be listed (and numbered) twice",
                )
            )
    return findings


def _truncation_finding(total: int, checked: int) -> Finding:
    """Say so when the per-run DOI budget stopped the check part-way through."""
    return Finding(
        "submission",
        "DOI checks truncated",
        f"only the first {checked} of {total} DOIs in the bibliography were checked " f"against the registry (paperpush's per-run limit); the rest were not verified",
    )


def _resolve_with_fallback(candidate: tuple[str, str]) -> tuple[str, dict | None, str]:
    """Resolve a DOI, retrying its line-wrapped continuation if it is not found.

    ``candidate`` is ``(doi, continuation)``, the continuation being the DOI
    rebuilt across a line break (``""`` when the file did not wrap it). A DOI cut
    in half by PDF line-wrapping resolves to nothing, which would read as a
    broken citation; if the rebuilt form resolves instead, that is the DOI the
    author actually cited, so it is the one reported on. Returns the outcome plus
    whichever form it belongs to.
    """
    status, record = _resolve_doi(candidate[0])
    if status == DOI_MISSING and candidate[1] and candidate[1] != candidate[0] and _is_well_formed(candidate[1]):
        alt_status, alt_record = _resolve_doi(candidate[1])
        if alt_status == DOI_FOUND:
            return alt_status, alt_record, candidate[1]
    return status, record, candidate[0]


def _entry_findings(entry: Entry, status: str, record: dict | None) -> list[Finding]:
    """Report a ``.bib`` entry against the record its DOI resolved to."""
    if status == DOI_MISSING:
        return [
            Finding(
                entry.where,
                "unresolvable DOI",
                f"{_cite(entry)} cites DOI {entry.doi}, which is not registered " "(doi.org returns 404); check the DOI against the published article",
            )
        ]
    if status == DOI_FOUND and record is not None:
        problems = _mismatches(entry, record)
        if problems:
            return [
                Finding(
                    entry.where,
                    "DOI metadata mismatch",
                    f"{_cite(entry)} does not match the work its DOI ({entry.doi}) " f"points to: {'; '.join(problems)}. Check whether the DOI belongs to " "a different reference",
                )
            ]
    return []


def _citation_findings(citation: Citation, status: str, record: dict | None, doi: str) -> list[Finding]:
    """Report a DOI found in the manuscript's prose against its record.

    A DOI that resolves to nothing is reported wherever it appears. The
    comparison against the surrounding text is made only inside the reference
    list, where that text is a bibliography entry describing the work; elsewhere
    -- a dataset DOI in a data-availability sentence, a DOI in a footnote --
    there is nothing to compare it against, so a resolving DOI passes quietly.
    """
    if status == DOI_MISSING:
        where_in_text = "in the reference list" if citation.in_reference_list else "in the manuscript text"
        return [
            Finding(
                citation.where,
                "unresolvable DOI",
                f"DOI {doi}, cited {where_in_text}, is not registered (doi.org " "returns 404); check the DOI against the published article",
            )
        ]
    if status == DOI_FOUND and record is not None and citation.in_reference_list and not _context_supports(citation, record):
        return [
            Finding(
                citation.where,
                "DOI metadata mismatch",
                f"the reference citing DOI {doi} does not match the work it points " f"to: the DOI is registered to {_describe(record)}, which does not " "appear in the reference. Check whether the DOI belongs to a " "different reference",
            )
        ]
    return []


def scan_references(paths: Iterable[Path], *, check_registry: bool = True) -> list[Finding]:
    """Report references whose DOI does not match the work they cite.

    Reads the submission's bibliography two ways, because most submissions ship
    only one of them:

    * every BibTeX entry in a ``.bib`` file (including one bundled in a source
      archive), whose ``doi``, ``title``, ``author``, and ``year`` are separate
      fields and so can be compared field by field;
    * every DOI written into the manuscript's own text -- a PDF's reference
      list, LaTeX source, a compiled ``.bbl`` -- compared against the reference
      text around it, since by then the title and authors are prose.

    The offline problems (malformed and duplicated DOIs) are reported first,
    then each remaining DOI is resolved through doi.org. A DOI already covered
    by a ``.bib`` entry is not re-reported from the manuscript text: the
    field-by-field comparison is the more precise of the two.
    ``check_registry=False`` reports the offline problems but makes no network
    requests (used by tests and offline callers).

    The lookups are pure network waits, so they run on a small thread pool; a
    review article's reference list would otherwise dominate validation's
    runtime. At most :data:`MAX_DOI_CHECKS` distinct DOIs are resolved per run,
    and hitting that limit is itself reported. Findings stay in the order the
    entries and citations were encountered.
    """
    entries = collect_entries(paths)
    checkable = [e for e in entries if e.doi and _is_well_formed(e.doi)]
    citations = _relevant_citations(collect_citations(paths), {e.doi for e in checkable})

    findings = _offline_findings(entries) + _duplicate_citation_findings(citations)
    if not check_registry:
        return findings

    # One lookup per distinct DOI, but findings are per entry and per citation:
    # two references sharing a wrong DOI should each be told about it. The .bib
    # DOIs come first so a huge reference list cannot crowd them out of the
    # budget -- they are the ones that can be checked field by field.
    candidates = list(dict.fromkeys((e.doi, "") for e in checkable))
    from_bib = len(candidates)
    candidates += list(dict.fromkeys((c.doi, c.continuation) for c in citations))
    if not candidates:
        return findings

    budgeted = candidates[:MAX_DOI_CHECKS]
    logger.info(
        "Resolving %d of %d distinct DOI(s) (%d from .bib entries, %d from manuscript text)",
        len(budgeted),
        len(candidates),
        from_bib,
        len(candidates) - from_bib,
    )

    from concurrent.futures import ThreadPoolExecutor

    workers = min(DOI_CHECK_WORKERS, len(budgeted))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="paperpush-doicheck") as pool:
        resolved = {doi: outcome for (doi, _), outcome in zip(budgeted, pool.map(_resolve_with_fallback, budgeted))}

    for entry in checkable:
        status, record, _ = resolved.get(entry.doi, (DOI_UNKNOWN, None, entry.doi))
        findings.extend(_entry_findings(entry, status, record))

    for citation in citations:
        status, record, doi = resolved.get(citation.doi, (DOI_UNKNOWN, None, citation.doi))
        findings.extend(_citation_findings(citation, status, record, doi))

    if len(candidates) > len(budgeted):
        findings.append(_truncation_finding(len(candidates), len(budgeted)))
    return findings
