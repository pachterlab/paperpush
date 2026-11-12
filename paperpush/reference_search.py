"""Look up the references a DOI check cannot reach, and the ones citing an old version.

:mod:`paperpush.references` checks every reference that carries a DOI. This
module covers the two things that check leaves out.

**References without a DOI** (:func:`search_references`). A fabricated
reference rarely comes with a DOI, so the DOI check never sees it. Each one is
searched for:

* a ``.bib`` entry by its title, first author, and year, in Crossref
  (``query.bibliographic``) and, when Crossref has nothing, in OpenAlex, which
  also indexes conference papers and arXiv;
* an entry citing an arXiv identifier, in the arXiv API, which says outright
  whether the identifier exists;
* an entry of a manuscript's own reference list (a PDF or Word file with no
  ``.bib``), by the entry's whole text in Crossref, which is what
  ``query.bibliographic`` is built for.

A ``.bib`` journal or conference entry that no source can match is reported:
Crossref and OpenAlex between them index nearly every journal article, so a
miss is worth a look. Books, software, theses, web pages, and every
reference-list entry read from extracted text are not accused on a miss. They
are listed as unverified instead, and handed to the AI check
(``ai.unverified_references``) when one runs. An entry that does match a
registered work but has no DOI is told the DOI, so it can be added.

**Publication versions** (:func:`version_findings`). Cite the journal article
when one exists, otherwise the latest preprint version:

* a preprint whose registry record says it became a journal article
  (Crossref's ``is-preprint-of`` relation, the bioRxiv/medRxiv publication API,
  or arXiv's journal DOI and journal reference);
* a preprint cited alongside its own journal article, which lists one work
  twice;
* an arXiv identifier pinned to an older version (``v1`` when ``v3`` exists).

All lookups are advisory and silent when inconclusive: a service that is down
or blocks the request leaves the reference unverified rather than accused. Each
service gets a request budget and a circuit breaker, so a slow service cannot
stall validation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .references import (
    MAX_TEXT_BYTES,
    TITLE_MATCH_RATIO,
    YEAR_TOLERANCE,
    Entry,
    Finding,
    _RETRACTION_NOTICES,
    _entry_first_author,
    _first_author_of,
    _normalize_text,
    _retraction_finding,
    _significant_words,
    _strip_latex,
    _title_of,
    _work_title,
    _year_of,
    _year_value,
    normalize_doi,
    title_similarity,
)

logger = logging.getLogger(__name__)

USER_AGENT = "paperpush-validate (+https://github.com/pachterlab/paperpush)"
TIMEOUT = 8.0
# At most this many references are searched per run; the rest are reported as unverified.
MAX_SEARCHES = 60
# A failed request (timeout, 429, 5xx) is tried this many more times, this many seconds apart.
RETRIES = 1
RETRY_DELAY = 1.0
_RETRY_STATUS = {429, 500, 502, 503, 504}
# A service is given up on after this many consecutive failed requests.
MAX_CONSECUTIVE_FAILURES = 3
# Below TITLE_MATCH_RATIO but at least this similar is a near miss: maybe the
# same work with a garbled title, maybe a different one. Left to the AI check.
NEAR_TITLE_RATIO = 0.6
# ... provided this share of the significant title words is shared too: the
# character ratio alone rates two unrelated titles on one topic as near.
NEAR_WORD_OVERLAP = 0.5
# A reference-list entry (prose) supports a registered work when this share of
# the work's significant title words appears in it ...
PROSE_TITLE_OVERLAP = 0.8
# ... and is a near miss above this share.
PROSE_NEAR_OVERLAP = 0.5
# arXiv asks for one request every three seconds, and takes up to this many ids at once.
ARXIV_BATCH = 50
ARXIV_DELAY = 3.0
# Titles shorter than this many words are too generic to search on.
MIN_TITLE_WORDS = 3
# How many unverified references the AI task lists.
MAX_AI_ITEMS = 25

# Entry types a missing Crossref/OpenAlex record says something about. Other
# types (books, software, theses, reports, web pages) are often unindexed.
INDEXED_KINDS = {"article", "inproceedings", "conference", "incollection", "inbook", "proceedings"}

# Registry records that share a title with the work without being it.
_NOT_THE_WORK = re.compile(r"^(?:erratum|errata|correction|corrigendum|retraction|expression of concern|reply|response|comment on|author correction|publisher correction|faculty opinions)\b", re.IGNORECASE)
_NOT_WORK_TYPES = {"dataset", "component", "peer-review", "grant", "journal-issue", "journal-volume", "journal", "proceedings", "book-series", "report-series", "standard-series"}

# arXiv identifiers: new style (2101.00001v2) and old style (hep-th/9901001v1).
_ARXIV_ID = r"(\d{4}\.\d{4,5}|[a-z][a-z.-]+(?:\.[A-Z]{2})?/\d{7})(?:v(\d+))?"
_ARXIV_IN_TEXT = re.compile(r"(?:arxiv\s*[:.]?\s*(?:abs/)?|arxiv\.org/(?:abs|pdf)/|10\.48550/arxiv\.)" + _ARXIV_ID, re.IGNORECASE)
_ARXIV_BARE = re.compile(r"^\s*" + _ARXIV_ID + r"\s*$", re.IGNORECASE)
_PREPRINT_DOI_PREFIXES = ("10.1101/",)  # bioRxiv and medRxiv
# A venue that is the preprint server itself: "arXiv preprint arXiv:...", "CoRR", "ArXiv", "arXiv e-prints".
_PREPRINT_VENUE = re.compile(r"\barxiv\b|\bcorr\b|\bpreprint\b|\be-?prints?\b", re.IGNORECASE)
# Semantic Scholar's and DBLP's exports put the id in the volume: journal={ArXiv}, volume={abs/1810.04805}.
_ARXIV_VOLUME = re.compile(r"^\s*abs/" + _ARXIV_ID + r"\s*$", re.IGNORECASE)
# At most this many arXiv preprints per run are searched for a published version.
MAX_PUBLISHED_SEARCHES = 30
# The registered types a preprint's peer-reviewed version takes.
_PUBLISHED_TYPES = {"journal-article", "proceedings-article", "book-chapter"}
# A published version appears at most this many years before / after the preprint.
_PUBLISHED_YEARS_BEFORE, _PUBLISHED_YEARS_AFTER = 1, 5
# The published version found for each arXiv id this process (None: Crossref answered, and has none).
_PUBLISHED_CACHE: dict[str, dict | None] = {}

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"

# Outcome of a search.
MATCHED = "matched"
NEAR = "near"
NOT_FOUND = "not found"
UNKNOWN = "unknown"


@dataclass
class Reference:
    """One reference to look up, from a ``.bib`` or from a manuscript's reference list.

    ``label`` names it in a finding. A ``.bib`` entry has its ``title``,
    first-author ``author`` family name, and ``year`` as separate fields; an
    entry of a reference list has only its ``text``. ``indexed`` says a miss in
    Crossref and OpenAlex is worth reporting (a journal or conference entry
    from a ``.bib``).
    """

    label: str
    where: str
    title: str = ""
    author: str = ""
    year: str = ""
    text: str = ""
    kind: str = ""
    venue: str = ""
    doi: str = ""
    arxiv_id: str = ""
    arxiv_version: int | None = None

    @property
    def indexed(self) -> bool:
        return bool(self.title) and self.kind in INDEXED_KINDS


@dataclass
class Lookup:
    """What searching for one :class:`Reference` found."""

    ref: Reference
    status: str
    record: dict | None = None
    source: str = ""
    problems: list[str] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)


@dataclass
class SearchResult:
    """Everything :func:`search_references` found, for ``integrity.unmatched_references``."""

    findings: list[Finding] = field(default_factory=list)
    lookups: list[Lookup] = field(default_factory=list)
    # References not searched: over budget, or a service gave up.
    skipped: list[Reference] = field(default_factory=list)
    skipped_reason: str = ""
    # arXiv's answer for every id looked up (absent: arXiv has no such id); None if arXiv did not answer.
    arxiv_records: dict[str, dict] | None = None
    arxiv_checked: set[str] = field(default_factory=set)

    def by_status(self, status: str) -> list[Lookup]:
        return [lk for lk in self.lookups if lk.status == status]

    @property
    def suggestions(self) -> list[Lookup]:
        """Matched references with no DOI whose matched work has one."""
        return [lk for lk in self.lookups if lk.status == MATCHED and not lk.problems and not lk.ref.doi and lk.record and lk.record.get("DOI")]

    @property
    def unverified(self) -> list[Lookup]:
        """References a reader should check: near misses, and misses not reported as findings."""
        return [lk for lk in self.lookups if lk.status == NEAR or (lk.status == NOT_FOUND and not lk.ref.indexed)]


# --- HTTP -------------------------------------------------------------------------------


class _Service:
    """One remote service: its own request budget and circuit breaker."""

    def __init__(self, name: str, budget: int, delay: float = 0.0) -> None:
        self.name = name
        self.budget = budget
        self.delay = delay
        self.failures = 0
        self.down = False
        self._last = 0.0

    def get(self, url: str, accept: str = "application/json") -> bytes | None:
        """The response body, or None when the request failed (and is counted against the service)."""
        if self.down or self.budget <= 0:
            return None
        self.budget -= 1
        if self.delay:
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT})
        body = None
        for attempt in range(1 + RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # https literal URLs below  # nosec B310
                    body = resp.read(MAX_TEXT_BYTES)
                break
            except urllib.error.HTTPError as exc:
                error: Exception = exc
                if exc.code not in _RETRY_STATUS:
                    break
            except Exception as exc:  # noqa: BLE001 -- inconclusive, never an accusation
                error = exc
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY)
        if body is None:
            exc = error
            self._last = time.monotonic()
            self.failures += 1
            logger.debug("%s request failed (%s): %s", self.name, exc, url)
            if self.failures >= MAX_CONSECUTIVE_FAILURES:
                logger.info("%s stopped answering; leaving the rest of its lookups unchecked", self.name)
                self.down = True
            return None
        self._last = time.monotonic()
        self.failures = 0
        return body

    def get_json(self, url: str):
        body = self.get(url)
        if body is None:
            return None
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return None

    @property
    def available(self) -> bool:
        return not self.down and self.budget > 0


@dataclass
class _Services:
    crossref: _Service
    openalex: _Service
    arxiv: _Service
    biorxiv: _Service


def _services() -> _Services:
    return _Services(
        crossref=_Service("Crossref", MAX_SEARCHES),
        openalex=_Service("OpenAlex", MAX_SEARCHES),
        arxiv=_Service("arXiv", 10, delay=ARXIV_DELAY),
        biorxiv=_Service("bioRxiv", 40),
    )


# --- registry queries -------------------------------------------------------------------

_CROSSREF_FIELDS = "DOI,title,author,issued,type,relation,container-title,short-container-title,updated-by,update-to"


def crossref_search(query: str, service: _Service) -> list[dict] | None:
    """Crossref's best candidates for a bibliographic ``query`` (CSL-shaped), or None if it did not answer."""
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode({"query.bibliographic": query[:500], "rows": 5, "select": _CROSSREF_FIELDS})
    data = service.get_json(url)
    try:
        items = data["message"]["items"]
    except (TypeError, KeyError):
        return None
    return [i for i in items if isinstance(i, dict)]


def _openalex_record(work: dict) -> dict:
    """An OpenAlex work in the CSL shape the comparison helpers read."""
    doi = work.get("doi") or ""
    authors = []
    for a in work.get("authorships") or []:
        name = ((a or {}).get("author") or {}).get("display_name") or ""
        if name:
            authors.append({"family": name.split()[-1], "literal": name})
    year = work.get("publication_year")
    return {
        "title": work.get("display_name") or work.get("title") or "",
        "author": authors,
        "issued": {"date-parts": [[year]]} if year else {},
        "DOI": normalize_doi(doi) if doi else "",
        "type": work.get("type") or "",
    }


def openalex_search(title: str, service: _Service) -> list[dict] | None:
    """OpenAlex works whose title matches ``title`` (CSL-shaped), or None if it did not answer.

    Set ``OPENALEX_API_KEY`` to use your own daily budget instead of the shared keyless one.
    """
    # A comma separates filters, so the title goes in as plain words.
    params = {"filter": "title.search:" + _normalize_text(title)[:300], "per_page": 5, "select": "doi,display_name,publication_year,type,authorships"}
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if key:
        params["api_key"] = key
    data = service.get_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return None
    return [_openalex_record(w) for w in data["results"] if isinstance(w, dict)]


def arxiv_lookup(ids: list[str], service: _Service) -> dict[str, dict] | None:
    """arXiv's record for each id that exists, keyed by bare id, or None if arXiv did not answer.

    Each record has the CSL-shaped ``title``/``author``/``issued`` plus
    ``latest_version``, ``journal_doi``, and ``journal_ref``. An id arXiv does not
    know is simply absent from the result.
    """
    import xml.etree.ElementTree as ET  # nosec B405 -- a response from arXiv's own API

    def feed_entries(url: str) -> list | None:
        body = service.get(url, accept="application/atom+xml")
        if body is None:
            return None
        try:
            return ET.fromstring(body).findall(f"{_ATOM}entry")  # nosec B314
        except ET.ParseError:
            return None

    found: dict[str, dict] = {}
    for start in range(0, len(ids), ARXIV_BATCH):
        chunk = ids[start : start + ARXIV_BATCH]
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({"id_list": ",".join(chunk), "max_results": len(chunk)})
        entries = feed_entries(url)
        # arXiv now and then answers 200 with an empty feed: ask again before
        # believing it, and never believe it of a whole batch.
        if entries == []:
            entries = feed_entries(url)
            if entries == [] and len(chunk) > 1:
                return None
        if entries is None:
            return None
        for entry in entries:
            m = re.search(r"arxiv\.org/abs/" + _ARXIV_ID + r"$", (entry.findtext(f"{_ATOM}id") or "").strip(), re.IGNORECASE)
            if not m:
                continue
            authors = [{"family": n.split()[-1], "literal": n} for n in ((a.findtext(f"{_ATOM}name") or "").strip() for a in entry.findall(f"{_ATOM}author")) if n]
            year = (entry.findtext(f"{_ATOM}published") or "")[:4]
            found[_arxiv_key(m.group(1))] = {
                "title": " ".join((entry.findtext(f"{_ATOM}title") or "").split()),
                "author": authors,
                "issued": {"date-parts": [[int(year)]]} if year.isdigit() else {},
                "latest_version": int(m.group(2)) if m.group(2) else None,
                "journal_doi": normalize_doi(entry.findtext(f"{_ARXIV_NS}doi") or ""),
                "journal_ref": " ".join((entry.findtext(f"{_ARXIV_NS}journal_ref") or "").split()),
            }
    return found


def biorxiv_published_doi(doi: str, service: _Service) -> str | None:
    """The journal DOI a bioRxiv/medRxiv preprint was published as, "" if none, None if unknown."""
    for server in ("biorxiv", "medrxiv"):
        data = service.get_json(f"https://api.biorxiv.org/pubs/{server}/{urllib.parse.quote(doi, safe='/.')}")
        if not isinstance(data, dict):
            return None
        for item in data.get("collection") or []:
            published = normalize_doi(str((item or {}).get("published_doi") or ""))
            if published and published != "na":
                return published
        if data.get("collection"):
            return ""
    return ""


# --- reading the references -------------------------------------------------------------


def _arxiv_key(raw: str) -> str:
    """An arXiv identifier as arXiv's API knows it: lower-cased, an old-style one without its subject class (math.DG/0211159 -> math/0211159)."""
    raw = raw.lower()
    archive, slash, number = raw.partition("/")
    return f"{archive.split('.')[0]}/{number}" if slash else raw


def arxiv_id_of(entry: Entry) -> tuple[str, int | None]:
    """The arXiv id (bare, lower-cased) and pinned version an entry cites, if any."""
    f = entry.fields
    eprint = f.get("eprint", "")
    prefix = (f.get("archiveprefix", "") or f.get("eprinttype", "")).lower()
    m = _ARXIV_BARE.match(_strip_latex(eprint)) if eprint and prefix in ("arxiv", "") else None
    # Mendeley writes arxivId = {1206.4358}; Semantic Scholar journal = {ArXiv}, volume = {abs/1810.04805}.
    m = m or (_ARXIV_BARE.match(_strip_latex(f.get("arxivid", ""))) if f.get("arxivid") else None)
    venue = f.get("journal", "") or f.get("journaltitle", "")
    m = m or (_ARXIV_VOLUME.match(_strip_latex(f.get("volume", ""))) if re.search(r"arxiv|corr", venue, re.IGNORECASE) else None)
    if m:
        return _arxiv_key(m.group(1)), int(m.group(2)) if m.group(2) else None
    for name in ("doi", "eprint", "journal", "url", "note", "howpublished", "journaltitle", "booktitle", "publisher"):
        m = _ARXIV_IN_TEXT.search(_strip_latex(f.get(name, "")))
        if m:
            return _arxiv_key(m.group(1)), int(m.group(2)) if m.group(2) else None
    return "", None


def _cite(entry: Entry) -> str:
    return f"bibliography entry '{entry.key}'" if entry.key else f'bibliography entry "{_strip_latex(entry.title).strip()[:60]}"'


def references_from_entries(entries: Iterable[Entry]) -> list[Reference]:
    """One :class:`Reference` per ``.bib`` entry (each key once)."""
    refs: list[Reference] = []
    seen: set[str] = set()
    for e in entries:
        ident = e.key.lower() or e.title
        if ident in seen:
            continue
        seen.add(ident)
        arxiv_id, version = arxiv_id_of(e)
        refs.append(
            Reference(
                label=_cite(e),
                where=e.where,
                title=_strip_latex(e.title).strip() if "title" not in e.unresolved else "",
                author=_entry_first_author(e.author) if "author" not in e.unresolved else "",
                year=e.year,
                kind=e.kind,
                venue=_strip_latex(e.fields.get("journal", "") or e.fields.get("journaltitle", "") or e.fields.get("booktitle", "")).strip(),
                doi=e.doi,
                arxiv_id=arxiv_id,
                arxiv_version=version,
            )
        )
    return refs


def references_from_text(path: Path) -> list[Reference]:
    """One :class:`Reference` per entry of the manuscript's own reference list (no ``.bib``)."""
    from .bibliography_check import read_bibliography
    from .references import _DOI_IN_TEXT

    bib = read_bibliography(path)
    if bib is None or (bib.mode == "keys" and bib.printed_only_cited):
        return []
    refs: list[Reference] = []
    for r in bib.refs:
        text = " ".join(_strip_latex(r.authors).split())
        if len(text) < 25:
            continue
        doi_match = _DOI_IN_TEXT.search(text)
        arxiv = _ARXIV_IN_TEXT.search(text)
        label = f"reference {r.label}" if r.label.startswith("[") else f"reference '{r.label}'"
        refs.append(
            Reference(
                label=label,
                where=path.name,
                text=text,
                year=r.year,
                doi=normalize_doi(doi_match.group(0)) if doi_match else "",
                arxiv_id=_arxiv_key(arxiv.group(1)) if arxiv else "",
                arxiv_version=int(arxiv.group(2)) if arxiv and arxiv.group(2) else None,
            )
        )
    return refs


# --- matching ---------------------------------------------------------------------------


def word_overlap(a: str, b: str) -> float:
    """Shared significant title words, as a share of the longer title's."""
    wa, wb = set(_significant_words(_work_title(a))), set(_significant_words(_work_title(b)))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def _is_candidate(record: dict) -> bool:
    title = _title_of(record).strip()
    if any(isinstance(u, dict) and u.get("type") in _RETRACTION_NOTICES for u in record.get("update-to") or []):
        return False  # the notice itself, often titled "Retracted: ..." like the article
    return bool(title) and not _NOT_THE_WORK.match(title) and str(record.get("type", "")).lower() not in _NOT_WORK_TYPES


def _prose_overlap(text: str, record: dict) -> float:
    words = set(_normalize_text(text).split())
    title_words = _significant_words(_work_title(_title_of(record)))
    if not title_words:
        return 0.0
    return sum(1 for w in title_words if w in words) / len(title_words)


def _family(name: str) -> str:
    """The last word of a normalized family name, so "van Beethoven" and "Beethoven" agree."""
    words = _normalize_text(name).split()
    return words[-1] if words else ""


def _describe(record: dict) -> str:
    title = _title_of(record).strip()
    author = _first_author_of(record)
    year = _year_of(record)
    out = f'"{title}"'
    if author:
        out += f" by {author}"
    if year:
        out += f" ({year})"
    if record.get("DOI"):
        out += f", DOI {record['DOI'].lower()}"
    return out


def match_entry(ref: Reference, candidates: list[dict]) -> Lookup:
    """Compare a ``.bib`` reference with a source's candidates."""
    candidates = [c for c in candidates if _is_candidate(c)]
    scored = sorted(((title_similarity(ref.title, _title_of(c)), c) for c in candidates), key=lambda t: -t[0])
    near = [(score, c) for score, c in scored if score >= NEAR_TITLE_RATIO and (score >= TITLE_MATCH_RATIO or word_overlap(ref.title, _title_of(c)) >= NEAR_WORD_OVERLAP)]
    if not near:
        return Lookup(ref, NOT_FOUND, candidates=[c for _, c in scored[:2]])
    scored = near
    strong = [c for score, c in scored if score >= TITLE_MATCH_RATIO]
    if not strong:
        return Lookup(ref, NEAR, record=scored[0][1], candidates=[c for _, c in scored[:2]])

    # Of several works with this title, the one by the same first author and nearest year.
    want_author = _family(ref.author)
    want_year = _year_value(ref.year)

    def rank(c: dict) -> tuple[int, int]:
        same_author = bool(want_author) and _family(_first_author_of(c)) == want_author
        year = _year_value(_year_of(c))
        gap = abs(year - want_year) if year and want_year else 0
        return (0 if same_author else 1, gap)

    best = min(strong, key=rank)
    problems: list[str] = []
    found_author = _first_author_of(best)
    if want_author and found_author and _family(found_author) != want_author:
        listed = {_family(a.get("family") or a.get("literal") or a.get("name") or "") for a in best.get("author") or [] if isinstance(a, dict)}
        where = "is not among its authors" if want_author not in listed else "is not its first author"
        problems.append(f"the entry's first author {ref.author} {where} (the registered first author is {found_author})")
    found_year = _year_value(_year_of(best))
    if want_year and found_year and abs(found_year - want_year) > YEAR_TOLERANCE:
        problems.append(f"the entry says {want_year} but the work is registered as {found_year}")
    return Lookup(ref, MATCHED, record=best, problems=problems)


def match_text(ref: Reference, candidates: list[dict]) -> Lookup:
    """Compare a reference-list entry (prose) with Crossref's candidates."""
    # A title of a word or two ("New products") turns up in almost any entry.
    candidates = [c for c in candidates if _is_candidate(c) and len(_significant_words(_work_title(_title_of(c)))) >= MIN_TITLE_WORDS]
    words = set(_normalize_text(ref.text).split())
    scored = sorted(((_prose_overlap(ref.text, c), c) for c in candidates), key=lambda t: -t[0])
    for score, c in scored:
        if score < PROSE_TITLE_OVERLAP:
            break
        family = _family(_first_author_of(c))
        if not family or family in words:
            return Lookup(ref, MATCHED, record=c)
    if scored and scored[0][0] >= PROSE_NEAR_OVERLAP:
        return Lookup(ref, NEAR, record=scored[0][1], candidates=[c for _, c in scored[:2]])
    return Lookup(ref, NOT_FOUND, candidates=[c for _, c in scored[:2]])


def match_arxiv(ref: Reference, record: dict | None) -> Lookup:
    """Compare a reference citing an arXiv id with what arXiv has under that id."""
    if record is None:
        return Lookup(ref, MATCHED, problems=[f"it cites arXiv:{ref.arxiv_id}, which arXiv has no record of"], source="arXiv")
    if ref.title and title_similarity(ref.title, record["title"]) < NEAR_TITLE_RATIO:
        return Lookup(ref, MATCHED, record=record, source="arXiv", problems=[f'it cites arXiv:{ref.arxiv_id}, but that identifier is "{record["title"]}"'])
    if ref.text and not ref.title and _prose_overlap(ref.text, record) < PROSE_NEAR_OVERLAP:
        return Lookup(ref, MATCHED, record=record, source="arXiv", problems=[f'it cites arXiv:{ref.arxiv_id}, but that identifier is "{record["title"]}"'])
    return Lookup(ref, MATCHED, record=record, source="arXiv")


_WEB_PAGE_ENTRY = re.compile(r"(?is)\bhttps?://(?!(?:dx\.)?doi\.org)\S+.*\b(?:accessed|retrieved|last\s+visited)\b|\bweb\s?site\b.*\bhttps?://(?!(?:dx\.)?doi\.org)")


def is_web_page_entry(text: str) -> bool:
    """Whether a reference-list entry cites a web page ("... Web site. https://... Accessed May 1, 2026.")."""
    return bool(text) and "doi.org" not in text.lower() and bool(_WEB_PAGE_ENTRY.search(text))


# --- the searches -----------------------------------------------------------------------


def _search_one(ref: Reference, svc: _Services) -> Lookup:
    if ref.title:
        query = " ".join(x for x in (ref.title, ref.author, ref.year) if x)
        items = crossref_search(query, svc.crossref) if svc.crossref.available else None
        lookup = match_entry(ref, items) if items is not None else None
        if lookup is not None:
            lookup.source = "Crossref"
        works = None
        if lookup is None or lookup.status != MATCHED:
            works = openalex_search(ref.title, svc.openalex) if svc.openalex.available else None
            if works is not None:
                second = match_entry(ref, works)
                second.source = "OpenAlex" if lookup is None else "Crossref and OpenAlex"
                if lookup is None or second.status == MATCHED or (second.status == NEAR and lookup.status == NOT_FOUND):
                    lookup = second
                else:
                    lookup.source = "Crossref and OpenAlex"
                    lookup.candidates += second.candidates[:1]
        # A miss means something only when both registries answered.
        if lookup is not None and lookup.status == NOT_FOUND and (items is None or works is None):
            lookup.status = UNKNOWN
        return lookup or Lookup(ref, UNKNOWN)
    items = crossref_search(ref.text, svc.crossref) if svc.crossref.available else None
    if items is None:
        return Lookup(ref, UNKNOWN)
    lookup = match_text(ref, items)
    lookup.source = "Crossref"
    return lookup


def search_references(refs: list[Reference], *, services: _Services | None = None) -> SearchResult:
    """Look up every reference with no DOI; see the module docstring for what is reported."""
    svc = services or _services()
    result = SearchResult()

    arxiv_refs = [r for r in refs if not r.doi and r.arxiv_id]
    if arxiv_refs:
        ids = sorted({r.arxiv_id for r in arxiv_refs})
        records = arxiv_lookup(ids, svc.arxiv)
        if records is not None:
            result.arxiv_records = records
            result.arxiv_checked = set(ids)
        for r in arxiv_refs:
            result.lookups.append(match_arxiv(r, records.get(r.arxiv_id)) if records is not None else Lookup(r, UNKNOWN, source="arXiv"))

    # A web page is not in the registries; the link check tells whether it is there.
    todo = [r for r in refs if not r.doi and not r.arxiv_id and not is_web_page_entry(r.text) and (r.text or len(_normalize_text(r.title).split()) >= MIN_TITLE_WORDS)]
    for i, ref in enumerate(todo):
        if i >= MAX_SEARCHES:
            result.skipped = todo[i:]
            result.skipped_reason = f"paperpush searches at most {MAX_SEARCHES} references per run"
            break
        if not svc.crossref.available and not (ref.title and svc.openalex.available):
            result.skipped = todo[i:]
            result.skipped_reason = "the registries stopped answering"
            break
        result.lookups.append(_search_one(ref, svc))

    for lk in result.lookups:
        if lk.status == MATCHED and lk.problems and lk.source != "arXiv" and not lk.ref.indexed:
            # A book, a web page, a prose entry: the "match" may be a review or
            # a namesake, so a disagreement is for a reader to settle.
            lk.status = NEAR
            lk.candidates = [lk.record] + lk.candidates if lk.record else lk.candidates
            lk.problems = []
        if lk.status == MATCHED and lk.problems:
            what = f"the closest registered work is {_describe(lk.record)}" if lk.record else ""
            result.findings.append(Finding(lk.ref.where, "reference does not match a registered work", f"{lk.ref.label} looks wrong: {'; '.join(lk.problems)}" + (f"; {what}" if what and lk.source != "arXiv" else "") + ". Check it against the published work"))
        elif lk.status == NOT_FOUND and lk.ref.indexed:
            searched = lk.source or "Crossref"
            near = f" (the closest title found was {_describe(lk.candidates[0])})" if lk.candidates else ""
            result.findings.append(
                Finding(
                    lk.ref.where,
                    "no registered work",
                    f'{lk.ref.label} ("{lk.ref.title[:120]}", {lk.ref.author or "no author"}, {lk.ref.year or "no year"}) matches no work in {searched}{near}. Check that it exists and its title is right; a reference that cannot be found may be fabricated',
                )
            )
        elif lk.status == MATCHED and lk.source != "arXiv" and lk.record and lk.record.get("DOI"):
            result.findings += _retraction_finding(lk.ref.where, lk.ref.label, str(lk.record["DOI"]).lower(), lk.record)
    return result


# --- publication versions ---------------------------------------------------------------


@dataclass
class Cited:
    """A cited work whose registered record is known, for the version check."""

    label: str
    where: str
    doi: str
    record: dict
    arxiv_id: str = ""
    arxiv_version: int | None = None


def _published_as(record: dict) -> str:
    """The journal DOI a preprint record says it became, or ""."""
    relation = record.get("relation")
    if not isinstance(relation, dict):
        return ""
    for rel in relation.get("is-preprint-of") or []:
        if isinstance(rel, dict) and rel.get("id-type") == "doi" and rel.get("id"):
            return normalize_doi(str(rel["id"]))
    return ""


def _is_preprint(record: dict) -> bool:
    kind = str(record.get("type", "")).lower()
    return kind in ("posted-content", "preprint") or str(record.get("subtype", "")).lower() == "preprint"


def _cites_as_preprint(ref: Reference) -> bool:
    """Whether a reference citing an arXiv id cites it as the preprint, not as a published work that also gives the id."""
    if ref.doi and not ref.doi.startswith("10.48550/"):
        return False  # the journal DOI is there: the entry already cites the article
    if ref.text:
        return bool(_PREPRINT_VENUE.search(ref.text))
    return not ref.venue or bool(_PREPRINT_VENUE.search(ref.venue))


def _venue_named_in(text: str, record: dict) -> bool:
    """Whether reference-list ``text`` already names ``record``'s venue (its full name, short name, or acronym)."""
    words = set(_normalize_text(text).split())
    names = [str(n) for key in ("container-title", "short-container-title") for n in (record.get(key) or []) if n]
    for name in names:
        significant = _significant_words(name)
        if significant and sum(1 for w in significant if w in words) / len(significant) >= 0.8:
            return True
        for acronym in re.findall(r"\(([A-Z][A-Za-z]{1,9})\)|\b([A-Z]{3,9})\b", name):
            token = (acronym[0] or acronym[1]).lower()
            if token and token in words:
                return True
    return False


def _search_published(arxiv_id: str, title: str, author: str, year: int | None, service: _Service) -> dict | None:
    """Crossref's record of the published version of a preprint (see :func:`published_version`), cached by arXiv id."""
    if arxiv_id in _PUBLISHED_CACHE:
        return _PUBLISHED_CACHE[arxiv_id]
    if not title or len(_normalize_text(title).split()) < MIN_TITLE_WORDS or not author or not service.available:
        return None
    items = crossref_search(" ".join(x for x in (title, author, str(year or "")) if x), service)
    if items is None:
        return None  # not answered: not cached, so a later run asks again
    want = _family(author)
    ranked = []
    for item in items:
        doi = normalize_doi(str(item.get("DOI") or ""))
        kind = str(item.get("type", "")).lower()
        if not doi or doi.startswith(("10.48550/",) + _PREPRINT_DOI_PREFIXES) or kind not in _PUBLISHED_TYPES or _is_preprint(item) or not _is_candidate(item):
            continue
        score = title_similarity(title, _title_of(item))
        if score < TITLE_MATCH_RATIO or _family(_first_author_of(item)) != want:
            continue
        got = _year_value(_year_of(item))
        if year and got and not (year - _PUBLISHED_YEARS_BEFORE <= got <= year + _PUBLISHED_YEARS_AFTER):
            continue
        # The closest title first ("Invited Talk: U-Net ..." is a talk about the paper), then the nearest year.
        ranked.append((-round(score, 3), abs(got - year) if got and year else 0, len(ranked), item))
    found = min(ranked)[3] if ranked else None
    _PUBLISHED_CACHE[arxiv_id] = found
    return found


def published_version(ref: Reference, arxiv_record: dict | None, service: _Service) -> dict | None:
    """Crossref's record of the peer-reviewed version of the arXiv preprint ``ref`` cites, or None.

    arXiv's own record names a journal DOI only when the authors added one, and
    most never do (U-Net, ResNet, and BERT all lack it), so the preprint's title
    and first author are searched in Crossref. A journal article, conference
    paper, or book chapter with the same title (at least
    :data:`TITLE_MATCH_RATIO` similar), the same first author, and a year close
    to the preprint's is its published version. Other preprint servers'
    copies, and a registry that does not answer, give None. So does a
    reference-list entry that already names the venue ("In CVPR, 2016.
    arXiv:1512.03385"), which cites the published version with its preprint.
    """
    rec = arxiv_record or {}
    title = rec.get("title") or ref.title
    author = _first_author_of(rec) if rec.get("author") else ref.author
    year = _year_value(_year_of(rec)) if rec.get("issued") else None
    found = _search_published(ref.arxiv_id, title, author, year or _year_value(ref.year), service)
    if found is None or (ref.text and _venue_named_in(ref.text, found)):
        return None
    return found


def version_findings(
    cited: list[Cited],
    arxiv_refs: list[Reference],
    all_dois: set[str],
    *,
    services: _Services | None = None,
    arxiv_records: dict[str, dict] | None = None,
    arxiv_checked: Iterable[str] = (),
) -> list[Finding]:
    """Findings for preprints that have been published and arXiv ids pinned to an old version.

    ``cited`` are the works whose registry record is known (from the DOI check
    and the searches); ``arxiv_refs`` every reference citing an arXiv id;
    ``all_dois`` every DOI the bibliography cites, to tell a superseded preprint
    from one cited alongside its own article. ``arxiv_records`` /
    ``arxiv_checked`` are what :func:`search_references` already asked arXiv,
    so those ids are not asked again.
    """
    svc = services or _services()
    findings: list[Finding] = []
    done: set[str] = set()  # labels already reported

    def published(label: str, where: str, preprint: str, journal_doi: str) -> None:
        done.add(label)
        if journal_doi in all_dois:
            findings.append(Finding(where, "preprint and article both cited", f"{label} cites the preprint {preprint}, and the bibliography also cites its published article (DOI {journal_doi}); the same work is listed twice. Keep only the article"))
        else:
            findings.append(Finding(where, "preprint has been published", f"{label} cites the preprint {preprint}, which has since been published (DOI {journal_doi}); cite the journal article instead"))

    for c in cited:
        if c.label in done or not _is_preprint(c.record):
            continue
        journal = _published_as(c.record)
        if not journal and c.doi.startswith(_PREPRINT_DOI_PREFIXES) and svc.biorxiv.available:
            journal = biorxiv_published_doi(c.doi, svc.biorxiv) or ""
        if journal and journal != c.doi:
            published(c.label, c.where, f"(DOI {c.doi})", journal)

    records = dict(arxiv_records or {})
    checked = set(arxiv_checked)
    missing = sorted({r.arxiv_id for r in arxiv_refs} - checked)
    if missing and svc.arxiv.available:
        answer = arxiv_lookup(missing, svc.arxiv)
        if answer is not None:
            records.update(answer)
            checked |= set(missing)
    searches = 0
    for r in arxiv_refs:
        rec = records.get(r.arxiv_id)
        # An entry with the journal DOI and an eprint field (an ADS export) already cites the article.
        if r.label in done or (r.doi and not r.doi.startswith("10.48550/")):
            continue
        # An id arXiv was asked about and does not know is reported by the search; one it was not asked about (arXiv down) can still be searched by title.
        if rec is None and r.arxiv_id in checked:
            continue
        rec = rec or {}
        journal = rec.get("journal_doi") or ""
        if journal:
            published(r.label, r.where, f"arXiv:{r.arxiv_id}", journal)
            continue
        latest = rec.get("latest_version")
        if r.arxiv_version and latest and r.arxiv_version < latest:
            findings.append(Finding(r.where, "older preprint version", f"{r.label} cites arXiv:{r.arxiv_id}v{r.arxiv_version}, but the latest version is v{latest}; cite the latest version (or drop the version number)"))
        elif rec.get("journal_ref") and (not r.venue or "arxiv" in r.venue.lower()) and not r.text:
            findings.append(Finding(r.where, "preprint has been published", f'{r.label} cites arXiv:{r.arxiv_id}, which arXiv lists as published in "{rec["journal_ref"]}"; cite the published version if it is the same work'))
        elif _cites_as_preprint(r) and (r.arxiv_id in _PUBLISHED_CACHE or searches < MAX_PUBLISHED_SEARCHES):
            searches += r.arxiv_id not in _PUBLISHED_CACHE
            found = published_version(r, rec or None, svc.crossref)
            if found:
                doi = normalize_doi(str(found["DOI"]))
                venue = next((str(v) for v in found.get("container-title") or [] if v), "")
                year = _year_of(found)
                where = (f" in {venue}" if venue else "") + (f" ({year})" if year else "")
                if doi in all_dois:
                    findings.append(Finding(r.where, "preprint and article both cited", f"{r.label} cites the preprint arXiv:{r.arxiv_id}, and the bibliography also cites its published version (DOI {doi}); the same work is listed twice. Keep only the published version"))
                else:
                    findings.append(Finding(r.where, "preprint has been published", f"{r.label} cites the preprint arXiv:{r.arxiv_id}, which has since been published{where} (DOI {doi}); cite the peer-reviewed version instead"))
                done.add(r.label)
    return findings


# --- the AI task ------------------------------------------------------------------------


def ai_task_instructions(unverified: list[Lookup], skipped: list[Reference]) -> str:
    """Instructions for ``ai.unverified_references``: the references the lookups could not settle."""
    lines = []
    for lk in unverified[:MAX_AI_ITEMS]:
        ref = lk.ref
        claim = f'"{ref.title}", {ref.author or "?"}, {ref.year or "?"}' if ref.title else ref.text[:300]
        near = f" Closest registered work found: {_describe(lk.candidates[0] if lk.candidates else lk.record)}." if (lk.candidates or lk.record) else " No similar work was found in the registries."
        lines.append(f"- {ref.label} ({ref.where}): {claim}.{near}")
    for ref in skipped[: max(0, MAX_AI_ITEMS - len(lines))]:
        claim = f'"{ref.title}", {ref.author or "?"}, {ref.year or "?"}' if ref.title else ref.text[:300]
        lines.append(f"- {ref.label} ({ref.where}): {claim}. Not looked up.")
    more = len(unverified) + len(skipped) - len(lines)
    if more > 0:
        lines.append(f"- ... and {more} more in the bibliography.")
    return (
        "These references have no DOI, and a registry search (Crossref, OpenAlex, arXiv) could not confirm them. "
        "For each one, decide whether it is a real work that the entry describes correctly. If you can search the web or "
        "the registries, look each one up. Report 'fail' naming every reference that appears fabricated, or whose title, "
        "authors, or year describe a different work than the one that exists, and give the correct details where you found "
        "them. Books, software, web pages, theses, and reports are often unindexed: an entry is not fabricated just because "
        "the search missed it. Report 'pass' if every one checks out, and 'unclear' if you could not verify them (for "
        "example, with no way to search). Do not judge from memory alone.\n" + "\n".join(lines)
    )
