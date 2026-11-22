"""Hold each reference against the whole of its registered record, and look up the journals cited.

The DOI check (:mod:`paperpush.references`) and the registry search
(:mod:`paperpush.reference_search`) decide *which* work a reference is. They
compare its title, first author, and year, loosely, and stop there. Once the
work is known, the rest of the entry can be held against its record too. This
module does that with the records those checks already fetched, so
:func:`detail_findings` makes no requests of its own:

* **authors**: every author, not only the first. Names the entry lists that
  the work does not have, registered authors it leaves out (unless the list
  ends in "et al." or "and others"), and an order different from the
  registered one. A reference-list entry read from a PDF or Word file that
  carries a DOI gets the author check the search makes for entries without one;
* **journal**: the journal the entry names, against the registered title, its
  abbreviation, and its acronym ("Proc. Natl. Acad. Sci. USA" and "PNAS" are
  both the Proceedings of the National Academy of Sciences);
* **year, volume, issue, pages**: against the registered values (the first
  page or the article number, and the last page; a year within one of any
  registered date);
* **books**: the edition, against the registered one, and the publisher,
  against the registered publisher(s), loosely ("Springer" is "Springer-Verlag
  New York"; a publisher that differs is a suggestion, since imprints merge
  and are renamed). The records come from Crossref or, for a book with no
  DOI, Open Library (see :mod:`paperpush.reference_search`), whose author
  lists mix in editors and translators, so their authors are not compared
  name by name;
* **completeness**: a journal article with no journal, a book chapter with no
  book title or editors, a journal named as the author, and a journal article
  with no volume or pages, quoting the registered values to add.

:func:`journal_indexing_findings` looks the cited journals up in OpenAlex by
ISSN and reports the ones in none of the indexes OpenAlex tracks (the CWTS core
list of journals with international peer review, DOAJ, MEDLINE, SciELO, the
Norwegian register). That is a prompt to check the journal, not proof that it
is predatory: OpenAlex has no list of predatory journals, and a young or
regional journal can be sound and unindexed.

Comparisons are loose in the same way as the DOI check's: an absent value on
either side is never compared, and only journal articles are held to a journal
name and page numbers, since conference names and book series are cited too
many ways to compare.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field

from .references import _AUTHOR_SPLIT, Entry, _normalize_text, _strip_latex, _year_value
from .sensitive import LEVEL_LIKELY, LEVEL_WARNING, Finding

logger = logging.getLogger(__name__)

LEVEL_SUGGESTION = "suggestion"

# Finding categories.
DETAILS = "reference details differ from the registered work"
WRONG_AUTHORS = "reference authors are not the work's"
INCOMPLETE = "incomplete reference"
MISSING_NUMBERS = "reference without volume or pages"
UNINDEXED = "cited journal not indexed"
UNKNOWN_JOURNAL = "cited journal not in OpenAlex"
PUBLISHER = "reference publisher differs from the registered one"

# Of an entry's listed authors, below this share among the registered ones the list is another work's.
AUTHOR_SHARE = 0.5
# A registered author list longer than this is often cut short without "et al.", so missing
# names are not reported.
MAX_FULL_LIST = 25
# How many names a message lists.
_MAX_NAMES = 5
YEAR_TOLERANCE = 1


@dataclass
class Matched:
    """One reference whose registered record is known.

    ``entry`` is set for a ``.bib`` entry, ``text`` for an entry of the
    manuscript's own reference list. ``check_authors`` is False when the
    registry search already compared the authors (a reference-list entry
    without a DOI), so they are not reported twice.
    """

    label: str
    where: str
    record: dict
    entry: Entry | None = None
    text: str = ""
    doi: str = ""
    check_authors: bool = True


# --- the registered record ----------------------------------------------------------------


def _one(value) -> str:
    """A CSL value that may be a string, a number, or a list of them, as one string."""
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, (str, int)) and str(v).strip()), "")
    if isinstance(value, int):
        return str(value)
    return _clean(value) if isinstance(value, str) else ""


def _clean(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", text)).split())


_KINDS = {"article-journal": "journal-article", "chapter": "book-chapter", "paper-conference": "proceedings-article"}


def record_kind(record: dict) -> str:
    """The record's work type in Crossref's words (``journal-article``, ``book-chapter``, ...)."""
    kind = str(record.get("type") or "").lower()
    return _KINDS.get(kind, kind)


def record_journal(record: dict) -> str:
    return _one(record.get("container-title"))


def _short_names(record: dict) -> list[str]:
    out: list[str] = []
    for key in ("container-title-short", "short-container-title"):
        value = record.get(key)
        for name in value if isinstance(value, list) else [value]:
            if isinstance(name, str) and name.strip() and _clean(name) not in out:
                out.append(_clean(name))
    return out


def record_volume(record: dict) -> str:
    return _one(record.get("volume"))


def record_issue(record: dict) -> str:
    issue = _one(record.get("issue"))
    if not issue and isinstance(record.get("journal-issue"), dict):
        issue = _one(record["journal-issue"].get("issue"))
    return issue


def record_pages(record: dict) -> tuple[str, str]:
    """(first page, last page) of the registered page range."""
    page = _one(record.get("page"))
    if not page:
        return "", ""
    parts = re.split(r"\s*[-–—]+\s*", page, maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def record_article_number(record: dict) -> str:
    return _one(record.get("article-number"))


def _people(record: dict, role: str = "author") -> list[dict]:
    return [a for a in record.get(role) or [] if isinstance(a, dict) and isinstance(a.get("family"), str) and a["family"].strip()]


def record_years(record: dict) -> set[int]:
    """Every year the record dates the work by (issued, print, online)."""
    years: set[int] = set()
    for key in ("issued", "published-print", "published-online", "published"):
        value = record.get(key)
        parts = value.get("date-parts") if isinstance(value, dict) else None
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            year = _year_value(str(parts[0][0]))
            if year:
                years.add(year)
    return years


def record_issns(record: dict) -> list[str]:
    value = record.get("ISSN")
    out: list[str] = []
    for issn in value if isinstance(value, list) else [value]:
        if isinstance(issn, str):
            compact = re.sub(r"[^0-9Xx]", "", issn).upper()
            if len(compact) == 8 and f"{compact[:4]}-{compact[4:]}" not in out:
                out.append(f"{compact[:4]}-{compact[4:]}")
    return out


def _digits(value: str) -> str:
    """The number in a page, volume, or article number ("e1007820" -> "1007820", "S12" -> "12")."""
    found = re.findall(r"\d+", value or "")
    return found[0].lstrip("0") or "0" if found else ""


# --- books: publisher and edition -----------------------------------------------------------

_BOOK_KINDS = frozenset({"book", "monograph", "edited-book", "reference-book", "book-set", "book-chapter", "book-section", "book-part"})
# Words that say a name is a publisher's rather than which one: "Springer-Verlag New York" is
# Springer, "Cambridge University Press" is Cambridge University. Places an imprint adds are here
# too; "University" is kept, but never matches on its own.
_PUBLISHER_GENERIC = frozenset(
    "the of and a press publishing publishers publisher publications publication pub pubs inc incorporated ltd limited llc "
    "co company corp corporation group gmbh verlag ag sa bv plc imprint books book media international intl global "
    "new york london berlin heidelberg cham dordrecht boston oxford-uk singapore usa uk us nature science business "
    "springerlink online service".split()
)


def record_publishers(record: dict) -> list[str]:
    names = [record.get("publisher")] + list(record.get("publishers") or [])
    return list(dict.fromkeys(_clean(n) for n in names if isinstance(n, str) and n.strip()))


def _publisher_words(name: str) -> list[str]:
    """A publisher's name's distinctive words, in order."""
    tokens = _normalize_text(name.replace("&", " and ").replace("-", " ")).split()
    tokens = ["university" if t in ("univ", "u") else t for t in tokens]
    return [t for t in tokens if t not in _PUBLISHER_GENERIC and len(t) > 1]


def publisher_tokens(name: str) -> set[str]:
    return set(_publisher_words(name))


def _publisher_acronym(name: str) -> str:
    """ "Massachusetts Institute of Technology Press" -> "mit"."""
    words = _publisher_words(name)
    return "".join(w[0] for w in words) if len(words) > 1 else ""


def same_publisher(cited: str, record: dict) -> bool | None:
    """Whether ``cited`` names one of the record's publishers (None: nothing to compare)."""
    own = publisher_tokens(cited)
    registered = [(name, publisher_tokens(name)) for name in record_publishers(record)]
    registered = [(name, tokens) for name, tokens in registered if tokens]
    if not registered or not own:
        return None
    for name, tokens in registered:
        if (own & tokens) - {"university"}:
            return True
        # "MIT Press" for "Massachusetts Institute of Technology Press", and the reverse.
        if own == {_publisher_acronym(name)} or tokens == {_publisher_acronym(cited)}:
            return True
    return False


def publisher_named_in(text: str, record: dict) -> bool | None:
    """Whether reference-list ``text`` names one of the record's publishers (None: nothing to compare)."""
    registered = [publisher_tokens(n) - {"university"} for n in record_publishers(record)]
    registered = [r for r in registered if r]
    if not registered:
        return None
    words = set(_normalize_text(text.replace("-", " ")).split())
    return any(names & words for names in registered)


_ORDINAL_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12}
_EDITION_NUMBER = re.compile(r"^\s*(\d{1,2})(?:st|nd|rd|th)?\b", re.IGNORECASE)
_EDITION_IN_TEXT = re.compile(r"\b(?:(\d{1,2})(?:st|nd|rd|th)|(" + "|".join(_ORDINAL_WORDS) + r"))\s+(?:rev(?:ised)?\.?\s+|expanded\s+)?(?:edn?\b\.?|edition\b)", re.IGNORECASE)


def edition_number(value: str) -> int | None:
    """The number of an edition as a .bib or a registry writes it ("2", "2nd", "Second", "2nd ed.")."""
    value = _strip_latex(value or "").strip()
    if not value:
        return None
    m = _EDITION_NUMBER.match(value)
    if m:
        return int(m.group(1))
    word = _normalize_text(value).split()
    return _ORDINAL_WORDS.get(word[0]) if word else None


def edition_in_text(text: str) -> int | None:
    m = _EDITION_IN_TEXT.search(text)
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else _ORDINAL_WORDS[m.group(2).lower()]


def record_edition(record: dict) -> int | None:
    return edition_number(_one(record.get("edition")) or _one(record.get("edition-number")))


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _book_problems(cited_edition: int | None, record: dict) -> list[str]:
    registered = record_edition(record)
    if cited_edition and registered and cited_edition != registered:
        return [f"it cites the {_ordinal(cited_edition)} edition but the work is the {_ordinal(registered)} edition"]
    return []


def _is_book_record(record: dict) -> bool:
    return record_kind(record) in _BOOK_KINDS


def _from_open_library(record: dict) -> bool:
    return record.get("source") == "Open Library"


# --- journal names --------------------------------------------------------------------------

_JOURNAL_STOP = frozenset("the of and for in on a an und de la le des du der die et y".split())
# Places a journal's name can end with in one form and not the other.
_TAILS = (("united", "states", "america"), ("u", "s", "a"), ("usa",), ("uk",))


def journal_tokens(name: str) -> list[str]:
    """A journal name's words, normalized, without stop words, a parenthesized place, or a trailing "USA"."""
    name = re.sub(r"\([^)]*\)", " ", name).replace("&", " and ")
    tokens = [t for t in _normalize_text(name).split() if t not in _JOURNAL_STOP]
    for tail in _TAILS:
        if len(tokens) > len(tail) and tuple(tokens[-len(tail):]) == tail:
            return tokens[: -len(tail)]
    return tokens


def _abbreviates(short: str, full: str) -> bool:
    """Whether ``short`` can be an abbreviation of ``full``: same first letter, letters in order ("natl" of "national")."""
    if short == full:
        return True
    if not short or not full or short[0] != full[0] or len(short) > len(full) or short.isdigit() or full.isdigit():
        return False
    letters = iter(full)
    return all(ch in letters for ch in short)


def _acronym(tokens: list[str]) -> str:
    return "".join(t[0] for t in tokens) if len(tokens) >= 2 else ""


def _same_tokens(a: list[str], b: list[str]) -> bool:
    if not a or not b:
        return False
    if "".join(a) == "".join(b):
        return True  # "Bio informatics", "PLoS ONE" / "PLOSONE"
    if len(a) == len(b) and all(_abbreviates(x, y) or _abbreviates(y, x) for x, y in zip(a, b)):
        return True
    for x, y in ((a, b), (b, a)):
        if len(x) == 1 and len(x[0]) >= 3 and x[0] == _acronym(y):
            return True
    return False


def journal_variants(record: dict) -> list[list[str]]:
    """The registered journal's names as word lists: the full title, its short forms, and the parts of a title with a colon."""
    variants: list[list[str]] = []
    for name in [record_journal(record)] + _short_names(record):
        parts = [name] + ([p for p in name.split(":") if p.strip()] if ":" in name else [])
        for part in parts:
            tokens = journal_tokens(part)
            if tokens and tokens not in variants:
                variants.append(tokens)
    return variants


def same_journal(cited: str, record: dict) -> bool | None:
    """Whether a journal name names the record's journal; None when either side is unknown."""
    tokens = journal_tokens(_strip_latex(cited))
    variants = journal_variants(record)
    if not tokens or not variants:
        return None
    return any(_same_tokens(tokens, v) for v in variants)


_WORD = re.compile(r"[^\W\d_]+")


def _word_spans(text: str) -> list[tuple[str, int, int]]:
    out = []
    for m in _WORD.finditer(text):
        for part in _normalize_text(m.group(0)).split():
            if part not in _JOURNAL_STOP:
                out.append((part, m.start(), m.end()))
    return out


def find_journal(text: str, record: dict) -> tuple[bool, int | None]:
    """Whether reference-list ``text`` names the record's journal, and where its name ends (None if not known)."""
    spans = _word_spans(text)
    words = [s[0] for s in spans]
    for variant in journal_variants(record):
        n = len(variant)
        if n == 1:
            # One word must be written whole: "Science" is not "s." in someone's initials.
            for i, w in enumerate(words):
                if w == variant[0]:
                    return True, spans[i][2]
            continue
        for i in range(len(words) - n + 1):
            if all(_abbreviates(words[i + k], variant[k]) for k in range(n)) and any(len(words[i + k]) > 1 for k in range(n)):
                return True, spans[i + n - 1][2]
        acronym = _acronym(variant)
        if len(acronym) >= 3:
            for i, w in enumerate(words):
                if w == acronym:
                    return True, spans[i][2]
    # A name split by the layout ("Bioinfor- matics", "Bio informatics").
    squeezed = re.sub(r"[^a-z]", "", _normalize_text(text))
    for name in [record_journal(record)] + _short_names(record):
        joined = re.sub(r"[^a-z]", "", _normalize_text(re.sub(r"\([^)]*\)", " ", name)))
        if len(joined) >= 6 and joined in squeezed:
            return True, None
    return False, None


# --- authors ---------------------------------------------------------------------------------


def _name_words(name: str) -> set[str]:
    """A written name's normalized words, initials left out."""
    return {w for w in _normalize_text(_strip_latex(name)).split() if len(w) > 1}


def _family_key(person: dict) -> str:
    words = _normalize_text(person.get("family") or "").split()
    return words[-1] if words else ""


def _registered_name(person: dict) -> str:
    given = (person.get("given") or "").strip()
    return f"{given} {person['family'].strip()}".strip()


def _matches_person(words: set[str], person: dict) -> bool:
    key = _family_key(person)
    return bool(key) and (key in words or _normalize_text(person.get("family") or "").replace(" ", "") in words)


def _listed(names: list[str]) -> str:
    shown = ", ".join(names[:_MAX_NAMES])
    return shown + (f" and {len(names) - _MAX_NAMES} more" if len(names) > _MAX_NAMES else "")


def bib_author_problems(author_field: str, record: dict) -> tuple[list[str], bool]:
    """Differences between a ``.bib`` author list and the registered one; True when the list is another work's."""
    people = _people(record)
    names = [n.strip() for n in _AUTHOR_SPLIT.split(author_field.strip()) if n.strip()]
    truncated = bool(names) and _normalize_text(names[-1]) in ("others", "et al", "al")
    if truncated:
        names = names[:-1]
    if not people or len(names) < 1:
        return [], False
    entry_words = [_name_words(n) for n in names]
    # An organization in the list ("{The ENCODE Project Consortium}") is not compared.
    entry_words = [w for w in entry_words if w and len(w) <= 4]
    if not entry_words:
        return [], False
    matched_entry = [any(_matches_person(w, p) for p in people) for w in entry_words]
    strangers = [names[i] for i, ok in enumerate(matched_entry) if not ok]
    if len(entry_words) >= 2 and sum(matched_entry) < AUTHOR_SHARE * len(entry_words):
        return [f"of the {len(entry_words)} authors it lists, {sum(matched_entry)} are among the work's (registered: {_listed([_registered_name(p) for p in people])})"], True
    problems: list[str] = []
    if strangers:
        problems.append(f"it lists {_listed([_strip_latex(s) for s in strangers])}, who {'is' if len(strangers) == 1 else 'are'} not among the work's authors")
    if not truncated and len(people) <= MAX_FULL_LIST:
        missing = [_registered_name(p) for p in people if not any(_matches_person(w, p) for w in entry_words)]
        if missing:
            problems.append(f"it leaves out registered author{'s' if len(missing) > 1 else ''} {_listed(missing)} (end the list with 'and others' if it is cut short on purpose)")
    if not problems and not truncated and len(entry_words) == len(people):
        order = [next((j for j, p in enumerate(people) if _matches_person(w, p)), -1) for w in entry_words]
        if order != sorted(order):
            problems.append(f"its authors are in a different order from the registered one ({_listed([_registered_name(p) for p in people])})")
    return problems, False


def prose_author_problems(text: str, record: dict) -> tuple[list[str], bool]:
    """The registry search's author comparison, for a reference-list entry that has a DOI."""
    from . import reference_search as rs

    words = set(_normalize_text(text).split())
    problems = rs._prose_problems(text, words, record, rs._prose_overlap(text, record))
    problems = [p for p in problems if not p.startswith("the entry's year")]
    wrong = any(p.startswith(rs.WRONG_AUTHORS) for p in problems)
    # With the DOI pinning the work, an entry that leaves out its first author and names under
    # half of the others lists other people, even when one of its names is a co-author's.
    if not wrong and any(p.startswith("the entry does not name the registered first author") for p in problems) and any(p.startswith("the entry's co-authors differ") for p in problems):
        wrong = True
    return [p.split(": ", 1)[1] if p.startswith(rs.WRONG_AUTHORS) else p for p in problems], wrong


def _lead(text: str) -> str:
    """The author part of a reference-list entry: up to its year or the first full stop after a word."""
    text = re.sub(r"^\s*(?:\[\d+\]|\d+[.)])\s*", "", text)
    m = re.search(r"\(\s*(?:19|20)\d\d|\.\s+(?=\S)", text)
    return text[: m.start()] if m else text[:120]


def journal_as_author(author: str, record: dict) -> bool:
    """Whether the author given is the journal (``Journal of Academic Ethics. (2025). ...``) and the work has people as authors."""
    if not _people(record) or not author.strip():
        return False
    return bool(same_journal(author, record))


# --- numbers -------------------------------------------------------------------------------

_LINKS = re.compile(r"https?://\S+|\bdoi\s*:?\s*10\.\S+|\b10\.\d{4,9}/\S+|\b(?:PMID|PMCID|ISSN|ISBN)\s*:?\s*\S+", re.IGNORECASE)
_VOL_PAGES = re.compile(r"(?<![\d.])(\d{1,5})\s*(?:\(\s*([^()]{1,15}?)\s*\))?\s*[,:]\s*(?:pp?\.\s*)?([A-Za-z]{0,2}\d{1,8})(?:\s*[-–—]\s*([A-Za-z]{0,2}\d{1,8}))?(?!\d)")
_VOL_WORD = re.compile(r"\bvol(?:ume)?\.?\s*(\d{1,5})", re.IGNORECASE)
_ISSUE_WORD = re.compile(r"\b(?:no|num|number|issue)\.?\s*(\d{1,5})\b", re.IGNORECASE)
_PAGES_WORD = re.compile(r"\bpp?\.\s*([A-Za-z]{0,2}\d{1,8})(?:\s*[-–—]+\s*([A-Za-z]{0,2}\d{1,8}))?", re.IGNORECASE)
_YEARISH = re.compile(r"^(?:1[89]|20)\d\d$")


@dataclass
class CitedNumbers:
    volume: str = ""
    issue: str = ""
    first_page: str = ""
    last_page: str = ""
    numbers: set = field(default_factory=set)


def cited_numbers(tail: str) -> CitedNumbers:
    """The volume, issue, and first page a reference-list entry gives after its journal name."""
    tail = _LINKS.sub(" ", tail)
    out = CitedNumbers(numbers={d.lstrip("0") or "0" for d in re.findall(r"\d+", tail)})
    m = _VOL_WORD.search(tail)
    if m:
        out.volume = m.group(1)
    m = _ISSUE_WORD.search(tail)
    if m:
        out.issue = m.group(1)
    m = _PAGES_WORD.search(tail)
    if m:
        out.first_page = m.group(1)
        out.last_page = m.group(2) or ""
    if out.volume and out.first_page:
        return out
    pos = 0
    while True:
        m = _VOL_PAGES.search(tail, pos)
        if m is None:
            break
        pos = m.start(1) + len(m.group(1))
        volume, issue, first, last = m.groups()
        # "2020, 17, 261": a year, then the volume; "17, 2020": the volume, then a year.
        if _YEARISH.match(volume) or (_YEARISH.match(first) and not last):
            continue
        out.volume = out.volume or volume
        if issue and not _YEARISH.match(issue.strip()):
            out.issue = out.issue or issue.strip()
        if not out.first_page:
            out.first_page, out.last_page = first, last or ""
        break
    return out


def _number_problems(cited: CitedNumbers, record: dict, *, tail_known: bool) -> tuple[list[str], list[str]]:
    """(differences, values missing) between the numbers an entry gives and the registered ones."""
    problems: list[str] = []
    missing: list[str] = []
    volume = record_volume(record)
    issue = record_issue(record)
    first, last = record_pages(record)
    article = record_article_number(record)
    if volume.isdigit() and cited.volume.isdigit() and int(cited.volume) != int(volume):
        problems.append(f"volume is {cited.volume} but the work is in volume {volume}")
    elif volume and not cited.volume and tail_known and _digits(volume) not in cited.numbers:
        missing.append(f"volume {volume}")
    if issue.isdigit() and cited.issue.isdigit() and int(cited.issue) != int(issue):
        problems.append(f"issue is {cited.issue} but the work is in issue {issue}")
    registered_pages = {d for d in (_digits(first), _digits(article)) if d}
    shown = f"{first}-{last}" if first and last else first or article
    if registered_pages and cited.first_page and _digits(cited.first_page) not in registered_pages:
        problems.append(f"pages start at {cited.first_page} but the work is {'on pages ' + shown if first else 'article ' + shown}")
    elif first and last and cited.first_page and cited.last_page and _digits(cited.first_page) == _digits(first):
        # The first page agrees, so the range is the work's; the end is held against the record's.
        cited_end, registered_end = full_last_page(cited.first_page, cited.last_page), full_last_page(first, last)
        if cited_end and registered_end and cited_end != registered_end and int(cited_end) >= int(_digits(first)):
            problems.append(f"pages end at {cited_end} but the work ends on page {registered_end}")
    elif registered_pages and not cited.first_page and tail_known and not registered_pages & cited.numbers:
        missing.append(f"pages {shown}" if first else f"article number {shown}")
    return problems, missing


def _bib_numbers(entry: Entry) -> CitedNumbers:
    f = entry.fields
    pages = _strip_latex(f.get("pages", "")).strip()
    parts = re.split(r"\s*[-–—]+\s*", pages, maxsplit=1) if pages else [""]
    first, last = parts[0], (parts[1] if len(parts) > 1 else "")
    if not first:
        first, last = _strip_latex(f.get("eid", "") or f.get("articleno", "") or f.get("article-number", "")).strip(), ""
    return CitedNumbers(volume=_strip_latex(f.get("volume", "")).strip(), issue=_strip_latex(f.get("number", "") or f.get("issue", "")).strip(), first_page=first, last_page=last)


def full_last_page(first: str, last: str) -> str:
    """A last page written short ("1234-9", "261-72") in full ("1239", "272"); as written otherwise."""
    a, b = _digits(first), _digits(last)
    if a and b and len(b) < len(a) and last.strip().isdigit():
        return a[: len(a) - len(b)] + b
    return b


# --- comparison ----------------------------------------------------------------------------


_PREPRINT = re.compile(r"\barxiv\b|\bbiorxiv\b|\bmedrxiv\b|\bpreprint\b|\bssrn\b|\bresearch\s*square\b", re.IGNORECASE)


def _bib_comparison(m: Matched) -> tuple[list[str], list[str], list[str], list[str], bool, list[str]]:
    """(differences, incomplete, missing numbers, author problems, wrong authors, publisher notes) for a .bib entry."""
    entry, record = m.entry, m.record
    assert entry is not None
    differences: list[str] = []
    incomplete: list[str] = []
    missing: list[str] = []
    authors: list[str] = []
    wrong = False
    kind = record_kind(record)
    journal = record_journal(record)
    cited_journal = entry.fields.get("journal", "") or entry.fields.get("journaltitle", "")
    notes: list[str] = []
    if journal_as_author(entry.author, record):
        incomplete.append(f"its author is the journal ({_strip_latex(entry.author).strip()}); the work's authors are {_listed([_registered_name(p) for p in _people(record)])}")
    elif m.check_authors and entry.author and "author" not in entry.unresolved and not _from_open_library(record):
        authors, wrong = bib_author_problems(entry.author, record)
    if kind == "journal-article" and entry.kind == "article":
        if cited_journal.strip():
            if same_journal(cited_journal, record) is False:
                differences.append(f"journal is {_strip_latex(cited_journal).strip()} but the work was published in {journal}")
        elif journal:
            incomplete.append(f"it names no journal; the work was published in {journal}")
        problems, absent = _number_problems(_bib_numbers(entry), record, tail_known=True)
        differences += problems
        missing += absent
    if kind == "book-chapter" and entry.kind in ("incollection", "inbook", "article", "misc"):
        booktitle = entry.fields.get("booktitle", "")
        if journal and not booktitle.strip() and not (entry.kind == "inbook" and entry.title and same_journal(entry.title, record)):
            incomplete.append(f"it is a book chapter but names no book; the book is {journal}")
        editors = _people(record, "editor")
        if editors and not entry.fields.get("editor", "").strip():
            missing.append(f"editors {_listed([_registered_name(p) for p in editors])}")
    if _is_book_record(record) and entry.kind in ("book", "inbook", "incollection", "booklet", "manual", "mvbook", "collection", "proceedings", "misc"):
        if "edition" not in entry.unresolved:
            differences += _book_problems(edition_number(entry.fields.get("edition", "")), record)
        cited_publisher = _strip_latex(entry.fields.get("publisher", "")).strip()
        if cited_publisher and "publisher" not in entry.unresolved and same_publisher(cited_publisher, record) is False:
            notes.append(f"its publisher is {cited_publisher} but the work's is {' / '.join(record_publishers(record)[:3])}")
    return differences, incomplete, missing, authors, wrong, notes


def _text_comparison(m: Matched) -> tuple[list[str], list[str], list[str], list[str], bool, list[str]]:
    """(differences, incomplete, missing numbers, author problems, wrong authors, publisher notes) for a reference-list entry."""
    text, record = re.sub(r"(\w)-\s+(\w)", r"\1\2", m.text), m.record
    differences: list[str] = []
    incomplete: list[str] = []
    missing: list[str] = []
    authors: list[str] = []
    wrong = False
    kind = record_kind(record)
    journal = record_journal(record)
    notes: list[str] = []
    lead = _lead(text)
    if journal_as_author(lead, record):
        incomplete.append(f"its author is the journal ({lead.strip(' .,')}); the work's authors are {_listed([_registered_name(p) for p in _people(record)])}")
    elif m.check_authors and not _from_open_library(record):
        authors, wrong = prose_author_problems(text, record)
    if m.doi:
        # The search already held the year of an entry it matched by its text.
        found = record_years(record)
        years = [int(y) for y in re.findall(r"(?<![\d/.])(?:19|20)\d{2}(?![\d/])", _LINKS.sub(" ", text))]
        if found and years and all(min(abs(y - f) for f in found) > YEAR_TOLERANCE for y in years):
            differences.append(f"year is {years[-1]} but the work is dated {min(found)}")
    # An entry citing the preprint of a work the search matched to its journal version is the
    # version check's business, not a wrong journal.
    if kind == "journal-article" and journal and not (not m.doi and _PREPRINT.search(text)):
        named, end = find_journal(text, record)
        if not named:
            differences.append(f"it does not name the work's journal, {journal} (it gives another journal, or none)")
        elif end is not None:
            problems, absent = _number_problems(cited_numbers(text[end:]), record, tail_known=True)
            differences += problems
            missing += absent
    if kind == "book-chapter" and journal:
        named, _ = find_journal(text, record)
        if not named:
            incomplete.append(f"it is a book chapter but does not name the book, {journal}")
        editors = _people(record, "editor")
        if editors and not any(_matches_person(set(_normalize_text(text).split()), p) for p in editors):
            missing.append(f"editors {_listed([_registered_name(p) for p in editors])}")
    if _is_book_record(record):
        differences += _book_problems(edition_in_text(text), record)
        if publisher_named_in(_LINKS.sub(" ", text), record) is False:
            notes.append(f"it does not name the work's publisher, {' / '.join(record_publishers(record)[:3])} (it gives another, or none)")
    return differences, incomplete, missing, authors, wrong, notes


def _describe(record: dict) -> str:
    from .references import _describe as describe

    journal = record_journal(record)
    return describe(record) + (f", {journal}" if journal and record_kind(record) in ("journal-article", "book-chapter") else "")


def detail_findings(matched: list[Matched]) -> list[Finding]:
    """Compare each reference with its registered record field by field (see the module docstring)."""
    findings: list[Finding] = []
    for m in matched:
        try:
            parts = _bib_comparison(m) if m.entry is not None else _text_comparison(m)
        except Exception:  # noqa: BLE001 -- an odd record leaves the entry unchecked, never accused
            logger.debug("could not compare %s with its record", m.label, exc_info=True)
            continue
        differences, incomplete, missing, authors, wrong, notes = parts
        work = f"the work {'its DOI points to' if m.doi else 'it matches'} ({_describe(m.record)})"
        if wrong:
            findings.append(Finding(m.where, WRONG_AUTHORS, f"{m.label} names other authors than {work}: {'; '.join(authors)}. Check the author list against the published work", LEVEL_LIKELY))
        elif authors:
            differences = differences + authors
        if differences:
            findings.append(Finding(m.where, DETAILS, f"{m.label} differs from {work}: {'; '.join(differences)}", LEVEL_WARNING))
        if incomplete:
            findings.append(Finding(m.where, INCOMPLETE, f"{m.label} is incomplete: {'; '.join(incomplete)}", LEVEL_WARNING))
        if missing:
            findings.append(Finding(m.where, MISSING_NUMBERS, f"{m.label} could add the {', '.join(missing)} of {work}", LEVEL_SUGGESTION))
        if notes:
            findings.append(Finding(m.where, PUBLISHER, f"{m.label} names another publisher than {work}: {'; '.join(notes)}. Imprints merge and are renamed, so confirm the publisher the edition you used names", LEVEL_SUGGESTION))
    return findings


# --- journal indexing ----------------------------------------------------------------------

# OpenAlex ``listed_in`` values that mean a vetted index; national lists other than Norway's
# take nearly any journal from their country.
_MAJOR_LISTS = ("cwts-core", "doaj", "medline", "pubmed", "scielo", "norway")
OPENALEX_SOURCE_BATCH = 40
_SOURCE_SELECT = "id,display_name,issn,issn_l,type,host_organization_name,is_core,is_in_doaj,is_in_scielo,listed_in,is_preprint_repository,works_count"


@dataclass
class CitedJournal:
    name: str
    issns: list[str]
    labels: list[str] = field(default_factory=list)
    where: str = ""


def cited_journals(matched: list[Matched]) -> list[CitedJournal]:
    """The journals of the journal articles cited, each once (two records sharing an ISSN are one journal)."""
    journals: list[CitedJournal] = []
    for m in matched:
        if record_kind(m.record) != "journal-article":
            continue
        issns = record_issns(m.record)
        if not issns:
            continue
        found = next((j for j in journals if set(j.issns) & set(issns)), None)
        if found is None:
            found = CitedJournal(record_journal(m.record) or issns[0], [], where=m.where)
            journals.append(found)
        found.issns += [i for i in issns if i not in found.issns]
        if m.label not in found.labels:
            found.labels.append(m.label)
    return journals


def lookup_sources(issns: list[str], service) -> tuple[dict[str, dict], bool]:
    """OpenAlex source records by ISSN, and whether every batch was answered."""
    from .reference_search import _openalex_url

    out: dict[str, dict] = {}
    complete = True
    for i in range(0, len(issns), OPENALEX_SOURCE_BATCH):
        batch = issns[i : i + OPENALEX_SOURCE_BATCH]
        params = {"filter": "issn:" + "|".join(batch), "per-page": 50}
        data = service.get_json(_openalex_url("sources", {**params, "select": _SOURCE_SELECT}))
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            # A field OpenAlex renamed makes ``select`` a 400; the whole record still has the rest.
            data = service.get_json(_openalex_url("sources", params))
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            complete = False
            continue
        for src in data["results"]:
            if isinstance(src, dict):
                for issn in (src.get("issn") or []) + [src.get("issn_l") or ""]:
                    if isinstance(issn, str) and issn:
                        out.setdefault(issn.upper(), src)
    return out, complete


def is_indexed(source: dict) -> bool:
    if source.get("is_core") or source.get("is_in_doaj") or source.get("is_in_scielo"):
        return True
    lists = [str(x).lower() for x in source.get("listed_in") or []]
    return any(x.startswith(_MAJOR_LISTS) for x in lists)


def _cited_by(journal: CitedJournal) -> str:
    return f"{_listed(journal.labels)} cite{'s' if len(journal.labels) == 1 else ''}"


def journal_indexing_findings(journals: list[CitedJournal], sources: dict[str, dict]) -> tuple[list[Finding], int]:
    """Findings for the journals in no index OpenAlex tracks, and how many journals were found in OpenAlex."""
    findings: list[Finding] = []
    found = 0
    for j in journals:
        source = next((sources[i] for i in j.issns if i in sources), None)
        if source is None:
            findings.append(Finding(j.where, UNKNOWN_JOURNAL, f"{_cited_by(j)} {j.name} (ISSN {', '.join(j.issns)}), which OpenAlex does not list. Check that the journal is genuine and peer reviewed", LEVEL_WARNING))
            continue
        found += 1
        if source.get("is_preprint_repository") or str(source.get("type") or "journal").lower() != "journal" or is_indexed(source):
            continue
        name = source.get("display_name") or j.name
        publisher = source.get("host_organization_name")
        lists = [str(x) for x in source.get("listed_in") or []]
        also = f"; it is only on {', '.join(lists)}" if lists else ""
        findings.append(
            Finding(
                j.where,
                UNINDEXED,
                f"{_cited_by(j)} {name}{f' ({publisher})' if publisher else ''}, which is in none of the indexes OpenAlex tracks (the CWTS core list of journals with international peer review, DOAJ, MEDLINE, SciELO, the Norwegian register){also}. "
                "Check the journal's peer review and indexing (Scopus, Web of Science) before relying on it; this alone does not make the cited work wrong",
                LEVEL_WARNING,
            )
        )
    return findings, found
