"""Look up the references a DOI check cannot reach, and the ones citing an old version.

:mod:`paperpush.references` checks every reference that carries a DOI. This
module covers the two things that check leaves out.

**References without a DOI** (:func:`search_references`). A fabricated
reference rarely comes with a DOI, so the DOI check never sees it. Each one is
searched for:

* a ``.bib`` entry by its title, first author, and year, in Crossref
  (``query.bibliographic``) and, when Crossref has nothing, in OpenAlex, which
  also indexes conference papers and arXiv;
* an entry citing an arXiv identifier (an arxiv.org or alphaxiv.org link, an
  ``eprint`` field, ...), by the DOI arXiv registers for every
  paper (10.48550/arXiv.<id>) in DataCite, which says outright whether the
  identifier exists (arXiv's own API when DataCite does not answer);
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

**Books** (:func:`isbn_findings`, and the search). Crossref and OpenAlex
miss many books, so a book they cannot match is searched in Open Library by
its title and author; a match takes it off the unverified list, and a miss
still accuses nothing. Every ISBN a reference gives is checked offline for its
check digit (:mod:`paperpush.isbn`), then looked up in Open Library: one filed
under a plainly different title points at another book, which is reported.

**Publication versions** (:func:`version_findings`). Cite the journal article
when one exists, otherwise the latest preprint version:

* a preprint whose registry record says it became a journal article
  (Crossref's ``is-preprint-of`` relation, the bioRxiv/medRxiv publication API,
  or arXiv's journal DOI and journal reference);
* a preprint cited alongside its own journal article, which lists one work
  twice;
* an arXiv identifier, or a bioRxiv/medRxiv DOI written with its version
  (``...613029v1``), pinned to an older version (``v1`` when ``v3`` exists).

All lookups are advisory and silent when inconclusive: a service that is down
or blocks the request leaves the reference unverified rather than accused. Each
service gets a request budget and a circuit breaker, so a slow service cannot
stall validation.
"""

from __future__ import annotations

import hashlib
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

from .isbn import isbns_in_field, isbns_in_text
from .sensitive import LEVEL_LIKELY
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
    _strip_accents,
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
# A 429 (Crossref's and OpenAlex's rate limits) is waited out this many times, for the
# server's Retry-After or a growing delay, capped at RATE_LIMIT_MAX_WAIT seconds.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_MAX_WAIT = 30.0


def _rate_limit_wait(exc: urllib.error.HTTPError, n: int) -> float:
    try:
        asked = float((exc.headers or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        asked = 2.0 * n
    return max(0.5, min(asked, RATE_LIMIT_MAX_WAIT))


def _asked_wait(exc: urllib.error.HTTPError) -> float:
    """The Retry-After a 429 asked for in seconds, 0 when it gave none (or gave a date)."""
    try:
        return float((exc.headers or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        return 0.0


# Set PAPERPUSH_LOOKUP_CACHE to a directory to keep every successful registry response
# (Crossref, OpenAlex, arXiv, bioRxiv) on disk and answer the same query from there next
# time: repeated runs on one manuscript (its .tex and its PDF, or validate after each fix)
# then ask each registry once. Off by default; responses are keyed by URL, minus any API key.
_LOOKUP_CACHE_ENV = "PAPERPUSH_LOOKUP_CACHE"


def _lookup_cache_path(url: str, accept: str) -> Path | None:
    root = os.environ.get(_LOOKUP_CACHE_ENV, "").strip()
    if not root:
        return None
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k not in ("api_key", "mailto")])
    key = hashlib.sha256(f"{accept}\n{parts.scheme}://{parts.netloc}{parts.path}?{query}".encode()).hexdigest()
    return Path(root).expanduser() / key[:2] / key


def _lookup_cache_read(url: str, accept: str) -> bytes | None:
    path = _lookup_cache_path(url, accept)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _empty_search_answer(body: bytes) -> bool:
    """Whether a response is a search that found nothing (OpenAlex ``"count": 0``, DataCite ``"data": []``).

    Not cached: OpenAlex's free and paid searches both answered empty for days at a time (November
    2026) for titles they hold, and a cached empty answer would turn every later run's lookup into
    a confident miss.
    """
    if b'"count":0' not in body.replace(b" ", b"") and b'"data":[]' not in body.replace(b" ", b""):
        return False
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    meta = data.get("meta")
    return (isinstance(meta, dict) and meta.get("count") == 0 and not data.get("results")) or data.get("data") == []


def _lookup_cache_write(url: str, accept: str, body: bytes) -> None:
    path = _lookup_cache_path(url, accept)
    if path is None or _empty_search_answer(body):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(body)
        os.replace(tmp, path)  # atomic, so parallel runs never read half a file
    except OSError as exc:
        logger.debug("could not cache %s: %s", url, exc)
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
ARXIV_DELAY = 3.5
# arXiv requests a run spends on title searches.
ARXIV_TITLE_REQUESTS = 30
# DataCite requests a run spends: one per arXiv identifier cited, plus title searches. DataCite
# holds the DOI arXiv registers for every paper (10.48550/arXiv.<id>), and unlike arXiv's own API
# (one request per 3 s per client) it takes a batch of validate runs without refusing them.
DATACITE_REQUESTS = 250
# Titles shorter than this many words are too generic to search on.
MIN_TITLE_WORDS = 3
# How many unverified references the AI task lists.
MAX_AI_ITEMS = 25

# Open Library requests a run spends: two per batch of ISBNs (data and details), and one per book
# title searched when Crossref and OpenAlex did not match it.
OPENLIBRARY_REQUESTS = 30
OPENLIBRARY_ISBN_BATCH = 40
# Book titles searched in Open Library per run.
MAX_BOOK_SEARCHES = 15
# Finding categories of the ISBN check.
INVALID_ISBN = "invalid ISBN"
WRONG_ISBN = "ISBN belongs to another book"

# Finding categories of a reference the search says is wrong. Both rest on matching
# titles and names, so ``validate`` offers them to ``ai.flagged_references``.
WRONG_MATCH = "reference does not match a registered work"
NO_MATCH = "no registered work"

# Entry types a missing Crossref/OpenAlex record says something about. Other
# types (books, software, theses, reports, web pages) are often unindexed.
INDEXED_KINDS = {"article", "inproceedings", "conference", "incollection", "inbook", "proceedings"}

# Registry records that share a title with the work without being it.
_NOT_THE_WORK = re.compile(r"^(?:erratum|errata|correction|corrigendum|retraction|expression of concern|reply|response|comment on|author correction|publisher correction|faculty opinions)\b", re.IGNORECASE)
_NOT_WORK_TYPES = {"dataset", "component", "peer-review", "grant", "journal-issue", "journal-volume", "journal", "proceedings", "book-series", "report-series", "standard-series"}

# arXiv identifiers: new style (2101.00001v2) and old style (hep-th/9901001v1).
_ARXIV_ID = r"(\d{4}\.\d{4,5}|[a-z][a-z.-]+(?:\.[A-Z]{2})?/\d{7})(?:v(\d+))?"
# alphaXiv (alphaxiv.org/abs/<id>, /overview/<id>) is a discussion site over arXiv that uses arXiv's ids.
_ARXIV_IN_TEXT = re.compile(r"(?:arxiv\s*[:.]?\s*(?:abs/)?|(?:arxiv|alphaxiv)\s?\.\s?org/\s?(?:abs|pdf|html|overview)/\s?|10\.48550/arxiv\.)" + _ARXIV_ID, re.IGNORECASE)
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
    # The version a bioRxiv/medRxiv DOI was cited with ("...613029v2"), None when unpinned.
    preprint_version: int | None = None
    # The ISBNs the entry gives, as written (see :mod:`paperpush.isbn`).
    isbns: list[str] = field(default_factory=list)

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
    # A prose entry neither Crossref nor OpenAlex could match by its full text or its title.
    confident_miss: bool = False


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


# After a 429 from a service paced across processes, every process waits this long (times the
# attempt) before the next request to it.
SHARED_BACKOFF = 30.0


def _pace_across_processes(name: str, delay: float, backoff: float = 0.0) -> None:
    """Wait until ``delay`` seconds after the last request any paperpush process sent to ``name``.

    With ``backoff`` (after a 429), the next request of every process is put off that many
    seconds, and this one waits it out. The time of the next allowed request is kept in a lock
    file in the temporary directory; without ``fcntl`` (Windows) only this process is paced."""
    try:
        import fcntl
        import tempfile
    except ImportError:
        time.sleep(backoff)
        return
    path = Path(tempfile.gettempdir()) / f"paperpush-{re.sub(r'[^a-z0-9]+', '-', name.lower())}.pace"
    wait_after = 0.0
    try:
        with open(path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                try:
                    allowed = float(fh.read().strip() or 0)  # when the next request may go
                except ValueError:
                    allowed = 0.0
                now = time.time()
                if backoff:
                    allowed = max(allowed, now + backoff)
                    wait_after = allowed - now
                else:
                    # Capped, so a clock set back (or a stale file) never stalls a run.
                    wait = min(max(allowed - now, 0.0), delay + SHARED_BACKOFF * RATE_LIMIT_RETRIES)
                    if wait:
                        time.sleep(wait)  # holding the lock: the processes queue up
                    allowed = time.time() + delay
                fh.seek(0)
                fh.truncate()
                fh.write(f"{allowed:.6f}")
                fh.flush()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        logger.debug("could not pace %s requests across processes", name, exc_info=True)
        wait_after = backoff
    if wait_after:
        time.sleep(wait_after)


class _Service:
    """One remote service: its own request budget and circuit breaker."""

    def __init__(self, name: str, budget: int, delay: float = 0.0, shared_pace: bool = False) -> None:
        self.name = name
        self.budget = budget
        self.delay = delay
        # Keep ``delay`` between requests from every paperpush process on this machine, not just
        # this one: arXiv's one request per three seconds is per client, and several validate
        # runs at once (a batch of papers) were refused with 429 for the rest of the hour.
        self.shared_pace = shared_pace
        self.failures = 0
        self.down = False
        self.throttled_out = 0  # requests still refused with 429 after every wait
        self._last = 0.0

    def get(self, url: str, accept: str = "application/json", missing_ok: bool = False) -> bytes | None:
        """The response body, or None when the request failed (and is counted against the service).
        With ``missing_ok``, a 404 is an answer (the record does not exist): ``b""``."""
        cached = _lookup_cache_read(url, accept)
        if cached is not None:
            return cached
        if self.down or self.budget <= 0:
            return None
        self.budget -= 1
        if self.delay:
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            if self.shared_pace:
                _pace_across_processes(self.name, self.delay)
        req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT})
        body = None
        attempt = throttled = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # https literal URLs below  # nosec B310
                    body = resp.read(MAX_TEXT_BYTES)
                break
            except urllib.error.HTTPError as exc:
                error: Exception = exc
                if exc.code == 404 and missing_ok:
                    body = b""
                    break
                if exc.code not in _RETRY_STATUS:
                    break
                if exc.code == 429:
                    if throttled >= RATE_LIMIT_RETRIES:
                        self.throttled_out += 1
                    if _asked_wait(exc) > RATE_LIMIT_MAX_WAIT or self.throttled_out >= 2:
                        # Told to come back later than we ever wait (a spent daily budget), or
                        # refused through every wait twice: the rest of this run's queries would
                        # only wait out the same refusal (~90 s each), so give up on it now.
                        self.down = True
                        logger.info("%s is rate limiting this client; leaving the rest of its lookups unchecked", self.name)
                        break
                    if throttled >= RATE_LIMIT_RETRIES:
                        break
                    # Rate limited, not down: wait as long as asked rather than spend a retry.
                    throttled += 1
                    if self.shared_pace:
                        # Every process backs off, or the others keep the limit tripped.
                        _pace_across_processes(self.name, self.delay, backoff=SHARED_BACKOFF * throttled)
                        _pace_across_processes(self.name, self.delay)
                    else:
                        time.sleep(_rate_limit_wait(exc, throttled))
                    continue
            except Exception as exc:  # noqa: BLE001 -- inconclusive, never an accusation
                error = exc
            if attempt >= RETRIES:
                break
            attempt += 1
            time.sleep(RETRY_DELAY)
        if body is None:
            exc = error
            self._last = time.monotonic()
            self.failures += 1
            logger.debug("%s request failed (%s): %s", self.name, exc, url)
            if self.failures >= MAX_CONSECUTIVE_FAILURES and not self.down:
                logger.info("%s stopped answering; leaving the rest of its lookups unchecked", self.name)
                self.down = True
            return None
        self._last = time.monotonic()
        self.failures = self.throttled_out = 0
        # Not an arXiv feed with no entries: arXiv sends those now and then (and while throttling),
        # and a cached one made every later "ask again" read the same empty answer.
        if not (accept == "application/atom+xml" and b"<entry" not in body):
            _lookup_cache_write(url, accept, body)
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
    datacite: _Service = field(default_factory=lambda: _Service("DataCite", DATACITE_REQUESTS))
    openlibrary: _Service = field(default_factory=lambda: _Service("Open Library", OPENLIBRARY_REQUESTS))
    semanticscholar: _Service = field(default_factory=lambda: _Service("Semantic Scholar", S2_REQUESTS, delay=S2_DELAY, shared_pace=True))


def _services() -> _Services:
    return _Services(
        crossref=_Service("Crossref", MAX_SEARCHES),
        openalex=_Service("OpenAlex", MAX_SEARCHES * OPENALEX_REQUESTS_PER_SEARCH),
        # Id batches, plus title searches (one or two each) for references the other registries
        # could not match; the rest stay unverified. Paced across processes, so it is slow.
        arxiv=_Service("arXiv", 10 + ARXIV_TITLE_REQUESTS, delay=ARXIV_DELAY, shared_pace=True),
        biorxiv=_Service("bioRxiv", 40),
    )


# --- registry queries -------------------------------------------------------------------

# volume ... editor: for the detail check (paperpush.reference_details), which holds a matched
# entry against the rest of its record.
_CROSSREF_FIELDS = "DOI,title,author,issued,type,relation,container-title,short-container-title,updated-by,update-to,volume,issue,page,article-number,ISSN,editor,published-print,published-online,publisher,ISBN"


# Semantic Scholar's title match, the last look for a reference no other registry matched: it
# indexes the machine-learning venues Crossref and OpenAlex miss (PMLR, MLSys, USENIX, CoRL,
# OpenReview). Keyless use shares one pool, so it is paced at one request a second across processes.
S2_REQUESTS = 60
S2_DELAY = 1.1


def semantic_scholar_match(title: str, service: _Service) -> list[dict] | None:
    """Semantic Scholar's paper whose title best matches ``title`` (CSL-shaped), [] if it has none, or None if it did not answer."""
    query = _autocomplete_text(title)
    if len(query.split()) < MIN_TITLE_WORDS:
        return None
    url = "https://api.semanticscholar.org/graph/v1/paper/search/match?" + urllib.parse.urlencode({"query": query[:300], "fields": "title,year,authors,venue,externalIds"})
    body = service.get(url, missing_ok=True)
    if body is None:
        return None
    if not body:
        return []  # 404: "Title match not found"
    try:
        data = json.loads(body.decode("utf-8", "replace")).get("data") or []
    except (ValueError, AttributeError):
        return None
    out = []
    for item in data:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        authors = []
        for a in item.get("authors") or []:
            name = (a or {}).get("name") or ""
            parts = name.split()
            if parts:
                authors.append({"family": parts[-1], "given": " ".join(parts[:-1]), "literal": name})
        rec = {"title": item["title"], "author": authors, "type": "article", "container-title": item.get("venue") or ""}
        if item.get("year"):
            rec["issued"] = {"date-parts": [[int(item["year"])]]}
        doi = ((item.get("externalIds") or {}).get("DOI")) or ""
        if doi:
            rec["DOI"] = normalize_doi(doi)
        out.append(rec)
    return out


def crossref_search(query: str, service: _Service) -> list[dict] | None:
    """Crossref's best candidates for a bibliographic ``query`` (CSL-shaped), or None if it did not answer."""
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode({"query.bibliographic": query[:500], "rows": 5, "select": _CROSSREF_FIELDS})
    data = service.get_json(url)
    try:
        items = data["message"]["items"]
    except (TypeError, KeyError):
        return None
    return [i for i in items if isinstance(i, dict)]


# OpenAlex sometimes keeps a record's names unparsed: "Villegas, R" (family first) and
# "CortesCorinna" (family and given run together, from some arXiv records).
_RUN_TOGETHER_NAME = re.compile(r"^([A-Z][a-z]{2,})([A-Z][a-z]{2,})$")


def _openalex_author(name: str) -> dict:
    """A CSL author from an OpenAlex display name."""
    name = name.strip()
    if "," in name:
        family, given = (x.strip() for x in name.split(",", 1))
        if family and given:
            return {"family": family.split()[-1], "literal": f"{given} {family}"}
    m = _RUN_TOGETHER_NAME.match(name)
    if m:
        return {"family": m.group(1), "literal": f"{m.group(2)} {m.group(1)}"}
    return {"family": name.split()[-1], "literal": name}


def _openalex_record(work: dict) -> dict:
    """An OpenAlex work in the CSL shape the comparison helpers read."""
    doi = work.get("doi") or ""
    authors = []
    for a in work.get("authorships") or []:
        name = ((a or {}).get("author") or {}).get("display_name") or ""
        if name:
            authors.append(_openalex_author(name))
    year = work.get("publication_year")
    return {
        "title": work.get("display_name") or work.get("title") or "",
        "author": authors,
        "issued": {"date-parts": [[year]]} if year else {},
        "DOI": normalize_doi(doi) if doi else "",
        "type": work.get("type") or "",
    }


# OpenAlex prices a full-text title search (``filter=title.search``) at $0.001, so a key's $1/day
# (or the shared keyless $0.10/day) covers only ~1,000 searches, while its autocomplete and
# single-work endpoints are free. A title is first looked up through autocomplete, which
# matches a phrase anywhere in a work's title: the whole title, then its first four words and
# its last OPENALEX_CHUNK, so one misread or dropped word (or text run on after the title) does
# not hide the work. The OPENALEX_CANDIDATES titles most like the reference's are then fetched
# whole (authors, year, DOI) for the usual comparison; among equally good titles the most cited
# record comes first, since OpenAlex keeps duplicates (dataset mirrors, unparsed author names). Only when autocomplete finds no title at all like the
# reference's is the paid search run (the key's budget, then the keyless one); once both are
# spent, OpenAlex counts as not answering, and arXiv's free title search decides instead.
OPENALEX_CHUNK = 5
OPENALEX_CANDIDATES = 5
# Each OpenAlex lookup takes up to 3 autocomplete + 5 record requests (+1 paid search).
OPENALEX_REQUESTS_PER_SEARCH = 9
_OPENALEX_SELECT = "doi,display_name,publication_year,type,authorships"
_OPENALEX_ID = re.compile(r"W\d+$")
# Set once the key's paid budget (or the keyless one) refuses a search, for the rest of the process.
_openalex_paid_spent: dict[str, bool] = {}


def _openalex_key() -> str:
    return os.environ.get("OPENALEX_API_KEY", "").strip()


def _openalex_url(path: str, params: dict, keyed: bool = True) -> str:
    params = dict(params)
    if keyed and _openalex_key():
        params["api_key"] = _openalex_key()
    return f"https://api.openalex.org/{path}?" + urllib.parse.urlencode(params)


# Text a title read from a reference list can run on into: ", 2021", ".arXiv preprint ...", "In ICLR".
_TITLE_TAIL = re.compile(r"(?i)(?:[\s.,;:]+(?:arxiv\b.*|in\s+(?:proceedings|advances|international|the)\b.*|\(?(?:19|20)\d\d[a-z]?\)?))+\s*$")


def _autocomplete_text(title: str) -> str:
    """A title as autocomplete matches it: words, apostrophes kept ("Let's" is not "let s"),
    lower case, without what ran on after it."""
    title = _TITLE_TAIL.sub("", title.strip())
    title = re.sub(r"[\u2018\u2019`]", "'", title)
    return " ".join(re.sub(r"[^\w']+", " ", title.lower()).split())


def _openalex_autocomplete(query: str, service: _Service) -> list[dict] | None:
    data = service.get_json(_openalex_url("autocomplete/works", {"q": query[:200]}))
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return None
    return [r for r in data["results"] if isinstance(r, dict) and r.get("display_name") and _OPENALEX_ID.search(str(r.get("id", "")))]


def _openalex_paid_search(title: str, service: _Service) -> list[dict] | None:
    """OpenAlex's full-text title search: the key's budget first, then the shared keyless one."""
    params = {"filter": "title.search:" + _normalize_text(title)[:300], "per_page": 5, "select": _OPENALEX_SELECT}
    for keyed in ((True, False) if _openalex_key() else (False,)):
        which = "key" if keyed else "free"
        if _openalex_paid_spent.get(which):
            continue
        url = _openalex_url("works", params, keyed=keyed)
        cached = _lookup_cache_read(url, "application/json")
        body = cached if cached is not None else _openalex_paid_get(url, service)
        if body is None:
            _openalex_paid_spent[which] = True
            logger.info("OpenAlex's paid title search (%s budget) refused; trying %s", which, "the keyless budget" if keyed else "no further budget")
            continue
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return [_openalex_record(w) for w in data["results"] if isinstance(w, dict)]
    return None


def _openalex_paid_get(url: str, service: _Service) -> bytes | None:
    """One paid request, outside the service's circuit breaker: a spent budget (429) must not
    stop the free endpoints."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    for attempt in range(RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # nosec B310 -- https literal URL
                body = resp.read(MAX_TEXT_BYTES)
            _lookup_cache_write(url, "application/json", body)
            return body
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code not in _RETRY_STATUS:
                return None
        except Exception:  # noqa: BLE001 -- inconclusive
            pass
        time.sleep(RETRY_DELAY)
    return None


def openalex_search(title: str, service: _Service) -> list[dict] | None:
    """OpenAlex works whose title matches ``title`` (CSL-shaped), or None if it did not answer.

    Uses the free autocomplete and single-work endpoints first (see OPENALEX_CHUNK). Set
    ``OPENALEX_API_KEY`` to give the paid fallback search your own daily budget.
    """
    norm = _autocomplete_text(title)
    words = norm.split()
    if not words:
        return []
    queries = [norm]
    if len(words) >= OPENALEX_CHUNK + 2:
        queries += [" ".join(words[:4]), " ".join(words[-OPENALEX_CHUNK:])]
    found: dict[str, tuple[float, dict]] = {}
    for i, q in enumerate(queries):
        hits = _openalex_autocomplete(q, service)
        if hits is None:
            continue
        for h in hits:
            wid = _OPENALEX_ID.search(str(h["id"])).group(0)
            found.setdefault(wid, (title_similarity(title, str(h["display_name"])), h))
        # The whole-title hit can be a duplicate with unparsed authors; the first-words chunk
        # brings the other records of that title, so stop only after it.
        if i >= 1 and any(sim >= TITLE_MATCH_RATIO for sim, _ in found.values()):
            break
    def rank(item):
        sim, hit = item[1]
        return (sim >= TITLE_MATCH_RATIO, round(sim, 2) if sim < TITLE_MATCH_RATIO else 1.0, int(hit.get("cited_by_count") or 0))

    near = [(sim, wid) for wid, (sim, _) in sorted(found.items(), key=rank, reverse=True) if sim >= NEAR_TITLE_RATIO]
    if not near:
        # Autocomplete found no title like this one, but its index lacks works the full-text
        # search has ("Training Verifiers to Solve Math Word Problems"), so its miss is no answer.
        # Confirm with the paid search while a budget lasts; otherwise OpenAlex did not answer
        # (and arXiv's title search, in _search_one, may still settle it).
        return _openalex_paid_search(title, service)
    records = []
    for _, wid in near[:OPENALEX_CANDIDATES]:
        work = service.get_json(_openalex_url(f"works/{wid}", {"select": _OPENALEX_SELECT}))
        if isinstance(work, dict) and work.get("display_name"):
            records.append(_openalex_record(work))
    if not records:
        return None
    return records


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


# Words of a title searched in arXiv's title field (the longest, up to this many; then the 3 longest).
ARXIV_TITLE_WORDS = 5
_ARXIV_SKIP = {"through", "towards", "toward", "with", "without", "using", "based", "from", "into", "over", "under", "via", "their", "beyond", "between", "among", "about"}


def _arxiv_query_words(title: str) -> list[str]:
    words = list(dict.fromkeys(w for w in _significant_words(_autocomplete_text(title).replace("'", " ")) if w.isalpha() and w not in _ARXIV_SKIP))
    words.sort(key=len, reverse=True)
    return words


def arxiv_title_search(title: str, service: _Service) -> list[dict] | None:
    """arXiv papers whose title has the title's significant words (CSL-shaped), or None if arXiv
    did not answer. Free, unlike OpenAlex's full-text search, and it holds most of the machine
    learning literature that Crossref lacks."""
    import xml.etree.ElementTree as ET  # nosec B405 -- a response from arXiv's own API

    # The longest words, which a misremembered title ("through" for "via") or a symbol arXiv
    # spells otherwise ("I2SB" for "I$^2$SB") is least likely to touch.
    words = _arxiv_query_words(title)
    if len(words) < 2:
        return None
    entries = None
    for take in (ARXIV_TITLE_WORDS, 3):
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({"search_query": " AND ".join(f"ti:{w}" for w in words[:take]), "max_results": 5})
        body = service.get(url, accept="application/atom+xml")
        if body is None:
            return None
        try:
            entries = ET.fromstring(body).findall(f"{_ATOM}entry")  # nosec B314
        except ET.ParseError:
            return None
        if entries or len(words) <= 3:
            break
    out = []
    for entry in entries or []:
        m = re.search(r"arxiv\.org/abs/" + _ARXIV_ID + r"$", (entry.findtext(f"{_ATOM}id") or "").strip(), re.IGNORECASE)
        if not m:
            continue
        year = (entry.findtext(f"{_ATOM}published") or "")[:4]
        out.append({
            "title": " ".join((entry.findtext(f"{_ATOM}title") or "").split()),
            "author": [{"family": n.split()[-1], "literal": n} for n in ((a.findtext(f"{_ATOM}name") or "").strip() for a in entry.findall(f"{_ATOM}author")) if n],
            "issued": {"date-parts": [[int(year)]]} if year.isdigit() else {},
            "DOI": f"10.48550/arxiv.{_arxiv_key(m.group(1))}",
            "type": "posted-content",
        })
    return out


def _datacite_arxiv_record(attrs: dict) -> dict | None:
    """An arXiv paper's DataCite record in the shape :func:`arxiv_lookup` returns."""
    titles = [t.get("title") for t in attrs.get("titles") or [] if isinstance(t, dict) and t.get("title")]
    if not titles:
        return None
    authors = []
    for c in attrs.get("creators") or []:
        if not isinstance(c, dict):
            continue
        family, given, name = (c.get("familyName") or "").strip(), (c.get("givenName") or "").strip(), (c.get("name") or "").strip()
        if not family and name:
            family, _, given = (x.strip() for x in name.partition(",")) if "," in name else (name.split()[-1], "", " ".join(name.split()[:-1]))
        if family:
            authors.append({"family": family, "given": given, "literal": f"{given} {family}".strip()})
    year = str(attrs.get("publicationYear") or "")
    version = str(attrs.get("version") or "")
    journal = next((normalize_doi(str(r.get("relatedIdentifier") or "")) for r in attrs.get("relatedIdentifiers") or [] if isinstance(r, dict) and r.get("relationType") == "IsVersionOf" and str(r.get("relatedIdentifierType", "DOI")).upper() == "DOI" and not str(r.get("relatedIdentifier", "")).lower().startswith("10.48550/")), "")
    return {
        "title": " ".join(titles[0].split()),
        "author": authors,
        "issued": {"date-parts": [[int(year)]]} if year.isdigit() else {},
        "latest_version": int(version) if version.isdigit() else None,
        "journal_doi": journal,
        "journal_ref": "",
    }


def datacite_arxiv_lookup(ids: list[str], service: _Service) -> tuple[dict[str, dict], set[str]]:
    """arXiv's DataCite record for each id that exists, and the ids DataCite answered for (a 404
    means arXiv has no such paper: it registers a DOI for every one)."""
    found: dict[str, dict] = {}
    checked: set[str] = set()
    for i in ids:
        if not service.available:
            break
        body = service.get("https://api.datacite.org/dois/10.48550/arxiv." + urllib.parse.quote(i, safe="/."), accept="application/vnd.api+json", missing_ok=True)
        if body is None:
            continue
        checked.add(i)
        if not body:
            continue  # 404: no such paper
        try:
            rec = _datacite_arxiv_record(json.loads(body.decode("utf-8", "replace"))["data"]["attributes"])
        except (ValueError, KeyError, TypeError):
            checked.discard(i)
            continue
        if rec:
            found[i] = rec
    return found, checked


def _arxiv_records(ids: list[str], svc: _Services) -> tuple[dict[str, dict], set[str]]:
    """Records of the arXiv ids (absent: no such paper), and the ids that were checked: DataCite
    first, then arXiv's own API for any DataCite did not answer."""
    records, checked = datacite_arxiv_lookup(ids, svc.datacite) if svc.datacite.available else ({}, set())
    missing = [i for i in ids if i not in checked]
    if missing and svc.arxiv.available:
        answer = arxiv_lookup(missing, svc.arxiv)
        if answer is not None:
            records.update(answer)
            checked |= set(missing)
    return records, checked


def datacite_arxiv_title_search(title: str, service: _Service) -> list[dict] | None:
    """arXiv papers (through their DataCite DOIs) whose title has the title's longest words, or
    None if DataCite did not answer. The same query as :func:`arxiv_title_search`."""
    words = _arxiv_query_words(title)
    if len(words) < 2:
        return None
    # The whole title as a phrase first: a title of short words ("Let's Verify Step by Step") is
    # lost among the matches of its longest ones.
    queries = ['titles.title:"' + _autocomplete_text(title).replace('"', " ") + '"', "titles.title:(" + " AND ".join(words[:ARXIV_TITLE_WORDS]) + ")"]
    if len(words) > 3:
        queries.append("titles.title:(" + " AND ".join(words[:3]) + ")")
    data = None
    for query in queries:
        # Ten, not five: a title's own record can rank below newer papers quoting it.
        params = {"query": query, "client-id": "arxiv.content", "page[size]": 10}
        body = service.get("https://api.datacite.org/dois?" + urllib.parse.urlencode(params), accept="application/vnd.api+json")
        if body is None:
            return None
        try:
            data = json.loads(body.decode("utf-8", "replace")).get("data") or []
        except (ValueError, AttributeError):
            return None
        if data:
            break
    out = []
    for item in data or []:
        attrs = (item or {}).get("attributes") or {}
        rec = _datacite_arxiv_record(attrs)
        if rec:
            rec["DOI"] = normalize_doi(str(attrs.get("doi") or ""))
            rec["type"] = "posted-content"
            out.append(rec)
    return out


def biorxiv_latest_version(doi: str, service: _Service) -> int | None:
    """The latest version of a bioRxiv/medRxiv preprint, or None if unknown."""
    for server in ("biorxiv", "medrxiv"):
        data = service.get_json(f"https://api.biorxiv.org/details/{server}/{urllib.parse.quote(doi, safe='/.')}")
        if not isinstance(data, dict):
            return None
        versions = [int(v) for item in data.get("collection") or [] if str((item or {}).get("version") or "").isdigit() for v in [item["version"]]]
        if versions:
            return max(versions)
    return None


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


# --- books (Open Library) ------------------------------------------------------------------

# Entry kinds the Open Library title search is for: a whole book (a chapter's title is not in it).
BOOK_KINDS = {"book", "booklet", "manual", "mvbook", "collection", "proceedings"}
# A reference-list entry that reads like a book: a publisher, an edition, or an ISBN.
_BOOKISH = re.compile(
    r"\b(?:press|publish(?:ing|ers?)|verlag|springer|wiley|elsevier|academic|routledge|sage|pearson|mcgraw|"
    r"prentice|addison|o'reilly|crc|taylor\s*&\s*francis|\d+(?:st|nd|rd|th)\s+edn?\.?|edition|isbn)\b",
    re.IGNORECASE,
)


def _openlibrary_year(date: str) -> int | None:
    years = re.findall(r"(?<!\d)(1[5-9]\d\d|20\d\d)(?!\d)", date or "")
    return int(years[-1]) if years else None


def _openlibrary_book(data: dict, details: dict, isbn: str = "") -> dict:
    """An Open Library edition (``jscmd=data`` and ``jscmd=details``) in the CSL shape the comparisons read."""
    title = str(data.get("title") or details.get("title") or "").strip()
    subtitle = str(data.get("subtitle") or details.get("subtitle") or "").strip()
    authors = [_openalex_author(str(a.get("name"))) for a in data.get("authors") or [] if isinstance(a, dict) and a.get("name")]
    publishers = [str(p.get("name") if isinstance(p, dict) else p).strip() for p in data.get("publishers") or details.get("publishers") or []]
    year = _openlibrary_year(str(data.get("publish_date") or details.get("publish_date") or ""))
    ids = data.get("identifiers") or {}
    isbns = [*(ids.get("isbn_13") or details.get("isbn_13") or []), *(ids.get("isbn_10") or details.get("isbn_10") or [])]
    record = {
        "title": [f"{title}: {subtitle}" if subtitle else title],
        "short-title": [title],
        "author": authors,
        "issued": {"date-parts": [[year]]} if year else {},
        "type": "book",
        "publisher": publishers[0] if publishers else "",
        "publishers": [p for p in publishers if p],
        "edition": str(details.get("edition_name") or ""),
        "ISBN": [i for i in isbns if i] or ([isbn] if isbn else []),
        "URL": str(data.get("url") or ""),
        "source": "Open Library",
    }
    return record


def openlibrary_isbn_records(isbns: list[str], service: _Service) -> dict[str, dict] | None:
    """Open Library's edition record for each ISBN-13 it knows (absent: it does not), or None if it did not answer."""
    out: dict[str, dict] = {}
    for start in range(0, len(isbns), OPENLIBRARY_ISBN_BATCH):
        batch = isbns[start : start + OPENLIBRARY_ISBN_BATCH]
        keys = ",".join(f"ISBN:{i}" for i in batch)
        data = service.get_json(f"https://openlibrary.org/api/books?{urllib.parse.urlencode({'bibkeys': keys, 'jscmd': 'data', 'format': 'json'})}")
        if not isinstance(data, dict):
            return out or None
        details = service.get_json(f"https://openlibrary.org/api/books?{urllib.parse.urlencode({'bibkeys': keys, 'jscmd': 'details', 'format': 'json'})}")
        details = details if isinstance(details, dict) else {}
        for isbn in batch:
            found = data.get(f"ISBN:{isbn}")
            if isinstance(found, dict) and (found.get("title")):
                more = (details.get(f"ISBN:{isbn}") or {}).get("details") or {}
                out[isbn] = _openlibrary_book(found, more if isinstance(more, dict) else {}, isbn)
    return out


def openlibrary_search(title: str, author: str, year: str, service: _Service) -> list[dict] | None:
    """Open Library's books with this title (and author), CSL-shaped, or None if it did not answer.

    A work in Open Library spans its editions, so each candidate is dated by the
    edition year nearest the reference's, not by the first edition.
    """
    params = {"title": title[:200], "fields": "key,title,subtitle,author_name,first_publish_year,publish_year,publisher,isbn", "limit": 5}
    if author:
        params["author"] = author
    data = service.get_json("https://openlibrary.org/search.json?" + urllib.parse.urlencode(params))
    if not isinstance(data, dict):
        return None
    want = _year_value(year)
    out: list[dict] = []
    for doc in data.get("docs") or []:
        if not isinstance(doc, dict) or not doc.get("title"):
            continue
        years = [int(y) for y in doc.get("publish_year") or [] if isinstance(y, int) or str(y).isdigit()]
        chosen = min(years, key=lambda y: abs(y - want)) if years and want else doc.get("first_publish_year")
        subtitle = str(doc.get("subtitle") or "").strip()
        out.append(
            {
                "title": [f"{doc['title']}: {subtitle}" if subtitle else str(doc["title"])],
                "short-title": [str(doc["title"])],
                "author": [_openalex_author(str(n)) for n in doc.get("author_name") or [] if n],
                "issued": {"date-parts": [[chosen]]} if chosen else {},
                "type": "book",
                "publishers": [str(p) for p in doc.get("publisher") or [] if p],
                "publisher": "",
                "ISBN": [str(i) for i in doc.get("isbn") or []][:20],
                "URL": f"https://openlibrary.org{doc['key']}" if doc.get("key") else "",
                "source": "Open Library",
            }
        )
    return out


def _is_book_reference(ref: Reference) -> bool:
    if ref.title:
        return ref.kind in BOOK_KINDS
    return bool(ref.isbns) or bool(_BOOKISH.search(ref.text))


def _book_titles(record: dict) -> list[str]:
    return [t for key in ("title", "short-title") for t in (record.get(key) or []) if isinstance(t, str) and t.strip()]


def _isbn_title_match(ref: Reference, record: dict) -> str:
    """Whether the book an ISBN belongs to is the one the reference cites: "same", "other", or "unsure"."""
    titles = _book_titles(record)
    if not titles:
        return "unsure"
    if ref.title:
        score = max(title_similarity(ref.title, t) for t in titles)
        overlap = max(word_overlap(ref.title, t) for t in titles)
        if score >= TITLE_MATCH_RATIO:
            return "same"
        return "other" if score < NEAR_TITLE_RATIO and overlap < NEAR_WORD_OVERLAP else "unsure"
    words = set(_normalize_text(ref.text).split())
    shares = []
    for t in titles:
        significant = _significant_words(_work_title(t))
        if significant:
            shares.append(sum(1 for w in significant if w in words) / len(significant))
    if not shares:
        return "unsure"
    best = max(shares)
    return "same" if best >= PROSE_TITLE_OVERLAP else "other" if best < 0.34 else "unsure"


@dataclass
class IsbnResult:
    """What :func:`isbn_findings` found, for ``integrity.isbns``."""

    findings: list[Finding] = field(default_factory=list)
    # The reference's label -> the Open Library record of the book its ISBN belongs to (when it is that book).
    records: dict[str, dict] = field(default_factory=dict)
    # ISBNs read, valid ones, valid ones Open Library answered for, and whether it answered at all.
    read: int = 0
    valid: int = 0
    found: int = 0
    answered: bool = True


def isbn_findings(refs: list[Reference], *, services: _Services | None = None) -> IsbnResult:
    """Check every ISBN the references give: its check digit (offline), then the book Open Library has under it.

    An ISBN that fails its check digit, or has the wrong number of digits, is a
    warning. A valid ISBN that Open Library files under a plainly different
    title is a likely error, offered to ``ai.flagged_references``, because it
    points at another book (copied from another entry, or made up). One Open
    Library does not know is not reported: its catalogue has gaps.
    """
    from .isbn import isbn_problem, to_isbn13

    svc = services or _services()
    result = IsbnResult()
    wanted: dict[str, list[tuple[Reference, str]]] = {}
    for ref in refs:
        for raw in ref.isbns:
            result.read += 1
            problem = isbn_problem(raw)
            if problem:
                result.findings.append(Finding(ref.where, INVALID_ISBN, f"{ref.label} gives the ISBN {raw}, which is not a valid ISBN: {problem}. Check it against the book's copyright page or the publisher's site"))
                continue
            result.valid += 1
            wanted.setdefault(to_isbn13(raw), []).append((ref, raw))
    if not wanted:
        return result
    records = openlibrary_isbn_records(sorted(wanted), svc.openlibrary) if svc.openlibrary.available else None
    if records is None:
        result.answered = False
        return result
    for isbn, cited in wanted.items():
        record = records.get(isbn)
        if record is None:
            continue
        result.found += 1
        for ref, raw in cited:
            verdict = _isbn_title_match(ref, record)
            if verdict == "same":
                result.records.setdefault(ref.label, record)
            elif verdict == "other":
                own = ref.title or _prose_title(ref.text) or ref.text[:80]
                publisher = record.get("publisher") or ""
                result.findings.append(
                    Finding(
                        ref.where,
                        WRONG_ISBN,
                        f'{ref.label} ("{own[:100]}") gives the ISBN {raw}, which Open Library lists as {_describe(record)}{f", {publisher}" if publisher else ""}. Check the ISBN: it may be another book\'s, or made up',
                        LEVEL_LIKELY,
                    )
                )
    return result


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
                preprint_version=_preprint_version_of(e),
                isbns=isbns_in_field(_strip_latex(e.fields.get("isbn", ""))),
            )
        )
    return refs


def _preprint_version_of(entry: Entry) -> int | None:
    """The bioRxiv/medRxiv version a ``.bib`` entry cites, from its DOI or its URL."""
    from .references import _DOI_IN_TEXT, preprint_doi_version

    for value in (entry.raw_doi, entry.fields.get("url", ""), entry.fields.get("note", ""), entry.fields.get("howpublished", "")):
        m = _DOI_IN_TEXT.search(_strip_latex(value or "")) if value else None
        if m and (version := preprint_doi_version(m.group(0))):
            return version
    return None


def references_from_text(path: Path) -> list[Reference]:
    """One :class:`Reference` per entry of the manuscript's own reference list (no ``.bib``)."""
    from .bibliography_check import read_bibliography
    from .references import _DOI_IN_TEXT, preprint_doi_version

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
                preprint_version=preprint_doi_version(doi_match.group(0)) if doi_match else None,
                isbns=isbns_in_text(text),
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
    if str(record.get("type", "")).lower() == "reference-entry" and _PERSON_ENTRY_TITLE.match(title):
        return False  # an encyclopedia's entry about a person ("Bonferroni, Carlo Emilio"), not their work
    return bool(title) and not _NOT_THE_WORK.match(title) and str(record.get("type", "")).lower() not in _NOT_WORK_TYPES


# An encyclopedia entry titled with a person's name, family name first.
_PERSON_ENTRY_TITLE = re.compile(r"^[A-Z][\w'\u00c0-\u017f-]+(?:\s+[A-Z][\w'\u00c0-\u017f-]+)?,\s*[A-Z][\w.'\u00c0-\u017f-]*(?:\s+[A-Z][\w.'\u00c0-\u017f-]*){0,3}$")


def _umlaut_spelled(word: str) -> str:
    """A normalized name with a spelled-out umlaut folded back ("gloeckler" and "glockler" agree)."""
    return re.sub(r"([aou])e", r"\1", word)


def _prose_overlap(text: str, record: dict) -> float:
    words = set(_normalize_text(text).split())
    title_words = _significant_words(_work_title(_title_of(record)))
    if not title_words:
        return 0.0
    # A word the extraction glued to its neighbor where a line-break hyphen fell ("selfsupervised",
    # "visionlanguage") is still in the entry, read without spaces.
    squashed = re.sub(r"\W", "", _normalize_text(text))
    return sum(1 for w in title_words if w in words or (len(w) >= 4 and w in squashed)) / len(title_words)


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


# Problems a prose entry's own text shows against the work its title matches. One that
# starts with WRONG_AUTHORS is reported outright (see search_references); the rest are near
# misses for a reader, since a prose match may be a namesake.
WRONG_AUTHORS = "the entry's authors are not the work's"
# A prose entry holding this share of the title's significant words names that work.
PROSE_STRONG_TITLE = 0.9
# Below this share of the registered authors' family names in the entry, the list is wrong.
PROSE_AUTHOR_SHARE = 0.5


def _title_share_in(registered: str, own: str) -> float:
    """Share of the registered title's significant words that the entry's own title holds."""
    reg = set(_significant_words(_work_title(registered)))
    return len(reg & set(_significant_words(_work_title(own)))) / len(reg) if reg else 0.0


def _registered_families(record: dict) -> list[str]:
    out = []
    for a in record.get("author") or []:
        if isinstance(a, dict):
            fam = _family(a.get("family") or a.get("name") or "")
            if fam and len(fam) > 1:
                out.append(fam)
    return out


def _registered_name_words(record: dict) -> set[str]:
    """Every word of every registered author's name, family and given alike.

    Registries swap the two (OpenAlex's "Stefano Fiorini" as family "Stefano") and entries
    write the family name first without a comma ("Chen Siyu, Sheen Heejune"), so a name is
    known by any of its words.
    """
    out: set[str] = set()
    for a in record.get("author") or []:
        if isinstance(a, dict):
            for key in ("family", "given", "literal", "name"):
                out.update(w for w in _normalize_text(a.get(key) or "").split() if len(w) > 1)
    return out


def _registered_given_names(record: dict) -> list[str]:
    """The last word of each registered author's given name (where a swapped record keeps the family name)."""
    out = []
    for a in record.get("author") or []:
        if isinstance(a, dict):
            words = _normalize_text(a.get("given") or "").split()
            if words and len(words[-1]) > 1:
                out.append(words[-1])
    return out


def _listed_name_words(text: str, family: str) -> set[str]:
    """The other words of the listed name whose last word is ``family`` ("Chen Siyu" for "siyu")."""
    m = re.search(rf"(?i)([A-Z][\w'-]*)\s+{re.escape(family)}\b", _strip_accents(text[:700]))
    return {_normalize_text(m.group(1))} if m else set()


def _identical_title(own: str, registered: str) -> bool:
    """Whether the title read from an entry is the registered one word for word (case, accents,
    and punctuation aside), or differs only by a subtitle the entry leaves out after a colon."""
    a, b = _normalize_text(own), _normalize_text(_work_title(registered))
    if not a or not b:
        return False
    if a == b or title_similarity(own, registered) >= 0.97:
        return True
    return ":" in registered and a == _normalize_text(registered.split(":", 1)[0])


def _author_segment(text: str) -> str:
    """The author list that opens a reference-list entry, as one lower-case run of letters."""
    text = re.sub(r"^\s*(?:\[\d+\]|\d+[.)])\s*", "", text)
    end = _PROSE_AUTHORS_END.search(text, 0, 700)
    return re.sub(r"[^a-z]", "", _normalize_text(text[: end.start()] if end else text[:300]))


def _named_in(family: str, words: set[str], segment: str) -> bool:
    """Whether a registered family name is in an entry: as a word, or (four letters or more) inside
    its author list read without spaces, where extraction split it ("Khas Ahmadi" for Khasahmadi)."""
    return family in words or (len(family) >= 4 and family in segment)


# Capital letters, Latin-1 and Latin Extended-A included, so an initial like "Ł." does not end the list.
_UPPER = "".join(c for c in map(chr, range(0x250)) if c.isupper())
_PROSE_AUTHORS_END = re.compile(rf"(?<![{_UPPER}])\.\s+(?=\S)")


def _prose_title(text: str) -> str:
    """The title of a reference-list entry: the sentence after its author list (and year, if it comes first)."""
    text = re.sub(r"^\s*(?:\[\d+\]|\d+[.)])\s*", "", text)
    # BibTeX's case-protecting braces, copied out of the .bbl ("et al.{Quant-LLM}: ... {GPUs}").
    text = re.sub(r"\}(?=[^\s.,:;{}])", " ", re.sub(r"(?<=\S)\{", " ", text)).replace("{", "").replace("}", "")
    end = _PROSE_AUTHORS_END.search(text, 0, 500)
    if end is None:
        return ""
    rest = re.sub(r"^\(?(?:19|20)\d{2}[a-z]?\)?[.,:]?\s*", "", text[end.end():])
    # A title may hold a question ("Do LLMs Overcome Shortcut Learning? An Evaluation ..."),
    # so only a period or the venue's "In" ends it.
    # ... and a period glued to the venue, where the extraction lost the space ("geometries.arXiv
    # e-prints", "Haiku.Advances in ...", "InInternational Conference").
    stop = re.search(r"\.(?:\s|$)|\s+In\s+(?=[A-Z])|(?<=[a-z0-9)])\.(?=arXiv\b|In[A-Z]|[A-Z][a-z])|\s+In(?=[A-Z][a-z])", rest)
    title = rest[: stop.start()] if stop else rest[:200]
    # ICML's position papers: "Position: ..." is not in the registered title.
    title = re.sub(r"^Position\s*:\s*", "", title)
    # A year run on after a comma ("Mistral 7b, 2023").
    return re.sub(r",\s*(?:19|20)\d{2}[a-z]?\s*$", "", title)


def _entry_families(text: str) -> list[str]:
    """Family names of the authors a natbib-style entry lists ("Jake Snell, Kevin Swersky, and
    Richard Zemel." or "... Kyunghyun Cho, et al."), normalized; [] when the list cannot be read."""
    text = re.sub(r"^\s*(?:\[\d+\]|\d+[.)])\s*", "", text)
    end = _PROSE_AUTHORS_END.search(text, 0, 700)
    if end is None:
        return []
    names = re.split(r",\s*(?:and\s+)?|\s+and\s+", re.sub(r",?\s*et\s+al\.?$", "", text[: end.start() + 1].rstrip(".")))
    out = []
    for name in names:
        words = [w for w in name.split() if not re.fullmatch(r"[A-Z]\.?(?:-[A-Z]\.?)?", w)]
        if not words or len(words) > 4 or not words[-1][:1].isupper():
            return []  # not a plain name list ("Smith, J." order, or prose)
        out.append(_family(words[-1]))
    return out if len(out) >= 2 else []


def _entry_lead_author(text: str) -> str:
    """The first author's family name in a reference-list entry, in either name order."""
    text = re.sub(r"^\s*(?:\[\d+\]|\d+[.)])\s*", "", text)
    # A middle initial ("Diederik P. Kingma") does not end the name; a full stop after a word does.
    first = re.split(rf",|\s+and\s+|\s+et\s+al\b|(?<![{_UPPER}])\.\s", text, maxsplit=1)[0]
    words = [w for w in first.split() if not re.fullmatch(r"[A-Z]\.?(?:-[A-Z]\.?)?", w)]
    if not words:
        return ""
    # "Smith, J." puts the family name first; "John Smith," last.
    return words[0] if re.match(r"\s*[^\s,]+,\s*[A-Z]\.?(?:\s|,|$)", text) else words[-1]


def _entry_lead_given(text: str) -> tuple[str, str]:
    """(given name, family name) of a reference-list entry's first author when it is written
    given-name first ("Ethan Perez, ..."); ("", "") for "Perez, E." order or anything unclear."""
    text = re.sub(r"^\s*(?:\[\d+\]|\d+[.)])\s*", "", text)
    if re.match(r"\s*[^\s,]+,\s*[A-Z]\.?(?:\s|,|$)", text):
        return "", ""
    first = re.split(r",|\s+and\s+|\s+&\s+|\s+et\s+al\b|\.\s+(?=[A-Z][a-z])", text, maxsplit=1)[0].strip()
    words = first.split()
    if not 2 <= len(words) <= 4 or not all(w[:1].isupper() for w in words):
        return "", ""
    return _normalize_text(words[0]), _family(words[-1])


def _registered_lead_given(record: dict) -> tuple[str, str]:
    """(given name, family name) of the registered first person author, normalized; ("", "") if unknown."""
    for a in record.get("author") or []:
        if not isinstance(a, dict):
            continue
        family = a.get("family") or ""
        given = a.get("given") or ""
        if not given and a.get("literal") and family:
            lit = a["literal"].strip()
            given = lit[: -len(family)].strip() if lit.endswith(family) else ""
        if family and given:
            return _normalize_text(given), _family(family)
        return "", ""
    return "", ""


_TEAM_WORDS = {"team", "group", "lab", "labs", "research", "ai", "consortium", "collaboration", "contributors", "community", "project"}


def _lead_given_conflict(text: str, record: dict) -> str:
    """A problem when the entry's first author has the work's first author's family name but a
    different given name ("Ethan Perez" for Fabio Perez): the same surname, another person.
    Compared by initial, so "J." and "John" or "Jurgen" and "Juergen" agree, and skipped when
    the entry may have the name order reversed (its given name is the registered family name)."""
    given, family = _entry_lead_given(text)
    reg_given, reg_family = _registered_lead_given(record)
    if not (given and reg_given and family and family == reg_family):
        return ""
    # "NovelSeek Team" and "InternAgent Team" (one project, renamed): a team, not a person.
    if family in _TEAM_WORDS or reg_family in _TEAM_WORDS:
        return ""
    # Particles are not given names ("De Chezelles, Thibault Le Sellier").
    particles = {"de", "le", "la", "van", "von", "der", "den", "da", "di", "del", "du", "dos", "das"}
    given_words = [w for w in given.split() if w not in particles] or given.split()
    g, r = given_words[0], reg_given.split()[0]
    if g[0] == r[0] or g == reg_family or _family(r) == family:
        return ""
    # The entry's "given name" is one of the work's names (a reversed or garbled author list:
    # "Li Guo, Li and Liu"), or the co-authors the entry lists are the work's: the same people.
    names = _registered_name_words(record)
    if g in names:
        return ""
    listed = _entry_families(text)
    if len(listed) >= 3 and sum(f in names for f in listed[1:]) >= 0.8 * (len(listed) - 1):
        return ""
    # A preferred name among several given names ("Zhenyu Chen" for Dave Zhenyu Chen), also when
    # registered as an initial ("Tian Qi Chen" for Ricky T. Q. Chen, "Yuan Li" for Christy Y. Li).
    if g in reg_given.split() or any(part[:1] == g[:1] for part in reg_given.split()[1:]):
        return ""
    # The work's first author named in full anywhere in the entry: the entry read here may be
    # merged with the one before it ("... Haokun Lin ... Quantized LLMs. Ji Lin, Chuang Gan ...").
    if re.search(rf"\b{re.escape(r)}\s+(?:[a-z]\s+)?{re.escape(reg_family)}\b", _normalize_text(text)):
        return ""
    # "Wen-tau" / "Wen tau" / "Wentau": initials of hyphenated given names agree already.
    return f"{WRONG_AUTHORS}: its first author is {given.title()} {family.title()}, but the work's first author is {reg_given.title()} {reg_family.title()}"


_INDEXED_VENUE = re.compile(r"\bIn\s*(?:Proc|Advances|International|Annual|The\s+\w+\s+(?:Conference|Workshop)|Conference|Findings|Workshop|Forty|Thirty|Twenty)|Proceedings|Conference|Transactions|Journal|\barXiv\b|Letters|\bNature\b|\bScience\b", re.IGNORECASE)
_UNINDEXED_KIND = re.compile(r"\b(?:thesis|dissertation|technical\s+report|tech\.\s*rep|white\s*paper|blog|github|gitlab|hugging\s*face|huggingface|kaggle|accessed|retrieved|website|web\s+page|model\s+card|system\s+card|press|book|chapter|manual|documentation|patent|standard|lecture|slides|tutorial|preprint\s+server|ssrn|talk|keynote|developers\s+conference|science\s*(?:&|and)\s*business\s+media)\b", re.IGNORECASE)


def claims_indexed_venue(text: str) -> bool:
    """Whether a reference-list entry says it is a journal, conference, or arXiv paper, the kinds
    of work Crossref or OpenAlex index; books, reports, software, and web pages often are not."""
    return bool(text) and bool(_INDEXED_VENUE.search(text)) and not _UNINDEXED_KIND.search(text)


def _same_title(a: str, b: str) -> bool:
    """Whether two titles name one work. Character similarity rates titles one key word apart
    ("On the Weaknesses of ..." / "A Study of ...") as the same; shared significant words do not."""
    if not a or not b:
        return False
    return word_overlap(a, b) >= 0.9 or title_similarity(a, b) >= 0.95


def _credible_title(own: str, registered: str) -> bool:
    """Whether the span read as an entry's title plausibly is one (styles differ: Vancouver
    "Pachter L. Title." can leave the journal there), judged by its overlap with the work's."""
    return len(own.split()) >= MIN_TITLE_WORDS and word_overlap(own, registered) >= 0.5


def _title_conflict(own: str, registered: str) -> bool:
    """The entry's own title is readable and is a different one from the registered work's."""
    return _credible_title(own, registered) and not _same_title(own, registered)


_ORGANIZATION_AUTHOR = re.compile(
    r"(?:[A-Z][\w&-]*\s+){0,4}(?:AI|Research|Team|Lab|Labs|Inc|Corporation|Foundation|Institute|Group|Consortium|Collaboration|DeepMind|OpenAI|Anthropic|Google|Meta|Microsoft|NVIDIA|Mistral|Qwen|DeepSeek|xAI|Xiaomi|Alibaba|Tencent|Baidu|ByteDance|Moonshot|Kimi|Gemma|Apple|Amazon|IBM|Cohere|Zhipu|MiniMax|StepFun)(?:\s+(?:AI|Research|Team|Lab|Labs|Inc|Computer))?[.,]\s|(?:Project|Together)\s+[A-Z][\w-]*[.,]\s"
)


def _prose_problems(text: str, words: set[str], record: dict, title_score: float) -> list[str]:
    """Author and year disagreements between a reference-list entry and the work it matched.

    An entry that lists its authors in full should name most of the registered ones; one
    cut short with "et al." is held only to its first author. A year counts when any year in
    the entry is within YEAR_TOLERANCE (a preprint and its proceedings differ by one).
    """
    problems: list[str] = []
    families = _registered_families(record)[:10]
    # "Meta AI Research. Llama 2: ...": a work cited under the organization that released it
    # names none of its people, and that is a citation style, not a wrong author list.
    # ... and a work registered under a team ("Team, Zhang, Wang, ..." for Xiaomi's MiMo-Audio)
    # has no author list to hold an entry to.
    organization = bool(_ORGANIZATION_AUTHOR.match(text.strip())) or (bool(families) and families[0] in _TEAM_WORDS)
    if organization:
        families = []
    truncated = bool(re.search(r"\bet\s+al\b|\bothers\b", text, re.IGNORECASE))
    # The entry's own title must be the work's: a short registered title can be a subset of a
    # longer one ("On the Difficulty of Membership Inference Attacks").
    own = _prose_title(text)
    if _not_a_title(own):
        own = ""
    same_title = _credible_title(own, _title_of(record)) and _same_title(own, _title_of(record))
    name_words = _registered_name_words(record)
    segment = _author_segment(text)
    if families and not truncated and len(families) >= 2 and title_score >= PROSE_STRONG_TITLE and same_title:
        present = [f for f in families if _named_in(f, words, segment)]
        # A registry that swapped an author's names has the family name among the given ones.
        present += [a for a in _registered_given_names(record)[:10] if a in words and a not in present]
        present = present[: len(families)]
        lead = _family(_entry_lead_author(text))
        if len(present) < PROSE_AUTHOR_SHARE * len(families):
            listed = f"of its registered authors ({', '.join(families[:4])}{', ...' if len(families) > 4 else ''}) the entry names {len(present)} of {len(families)}"
            # A wrong first author is reported outright; the right first author with other
            # co-authors may be another version of the work (a solo working paper later
            # co-authored) or a review registered under its reviewer, so a reader decides.
            if lead and lead not in name_words:
                problems.append(f"{WRONG_AUTHORS}: {listed}")
            else:
                problems.append(f"the entry's co-authors differ from the work's: {listed}")
    listed = _entry_families(text)
    # Any word of a registered name: "Chen Siyu" lists the family name first.
    all_registered = name_words | {f for f in _registered_families(record)}
    for i, f in enumerate(listed):
        if f not in all_registered and (other := _listed_name_words(text, f) & all_registered):
            listed[i] = next(iter(other))
    if same_title and not problems and not organization and len(listed) >= 3 and all_registered and sum(f in all_registered for f in listed) < PROSE_AUTHOR_SHARE * len(listed):
        # The names the entry does list, even cut short by "et al.", should be the work's.
        lead = listed[0]
        named = f"of the {len(listed)} authors the entry lists ({', '.join(listed[:4])}{', ...' if len(listed) > 4 else ''}) {sum(f in all_registered for f in listed)} are among the work's"
        problems.append(f"{WRONG_AUTHORS}: {named}" if lead not in all_registered else f"the entry's co-authors differ from the work's: {named}")
    elif families and families[0] not in words and same_title:
        problems.append(f"the entry does not name the registered first author ({families[0]})")
    if same_title and not organization and not any(p.startswith(WRONG_AUTHORS) for p in problems):
        given = _lead_given_conflict(text, record)
        if given:
            # The right surname on another person outranks "the co-authors differ".
            problems = [given] + [p for p in problems if not p.startswith("the entry's co-authors differ")]
    found_year = _year_value(_year_of(record))
    years = [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", text)]
    far_year = bool(found_year and years and all(abs(y - found_year) > YEAR_TOLERANCE for y in years))
    if same_title and far_year:
        problems.append(f"the entry's year ({years[-1]}) is not the work's ({found_year})")
    # Reported outright only when the record is plainly the work the entry names: the very same
    # title. A review of a book, a reproduction study, or a namesake under a similar title carries
    # other people, so those go to a reader instead. A far year alone does not: a fabricated entry
    # often gives a real title a made-up year as well as made-up people.
    if any(p.startswith(WRONG_AUTHORS) for p in problems) and not _identical_title(own, _title_of(record)):
        problems = [p.replace(WRONG_AUTHORS, "the authors differ from those of a registered work with this title (perhaps another edition, a review, or a namesake)", 1) if p.startswith(WRONG_AUTHORS) else p for p in problems]
    return problems


def match_text(ref: Reference, candidates: list[dict]) -> Lookup:
    """Compare a reference-list entry (prose) with Crossref's candidates."""
    # A title of a word or two ("New products") turns up in almost any entry.
    # ... unless the entry's own title, read cleanly, is that short title ("Attention Is All You Need").
    own_title = _prose_title(ref.text)
    if _not_a_title(own_title):
        own_title = ""  # misread (a venue, a URL, names): the entry's own title is unknown
    candidates = [c for c in candidates if _is_candidate(c) and (len(_significant_words(_work_title(_title_of(c)))) >= MIN_TITLE_WORDS or _same_title(own_title, _title_of(c)))]
    words = set(_normalize_text(ref.text).split())
    scored = sorted(((_prose_overlap(ref.text, c), c) for c in candidates), key=lambda t: -t[0])
    first_fit = None
    for score, c in scored:
        if score < PROSE_TITLE_OVERLAP:
            break
        # Most of a registered title's words can sit in a different title on the same topic
        # ("A Study of RL for NMT" in "On the Weaknesses of RL for NMT"): when the entry's own
        # title can be read, it has to be that title.
        if _title_conflict(own_title, _title_of(c)):
            continue
        # A readable title of five or more words that shares under half its words with the
        # registered one is another work by the same people ("Type inference in the presence of
        # first-class polymorphism" is not "Constraint-based type inference and parametric
        # polymorphism"), however many of the registered title's words the entry holds.
        # The title read from the entry can run on into the venue ("Visual instruction
        # tuning.Advances in Neural ..."), so a registered title held almost whole in it still counts.
        if len(_significant_words(own_title)) >= 5 and word_overlap(own_title, _title_of(c)) < 0.5 and _title_share_in(_title_of(c), own_title) < 0.9:
            continue
        # A record with no authors (a book chapter, proceedings front matter, a journal's annual
        # index, a conference's name) has nothing to check the entry against but its title.
        if not _registered_families(c) and not _same_title(own_title, _title_of(c)):
            continue
        family = _family(_first_author_of(c))
        if not family or family in words or _umlaut_spelled(family) in {_umlaut_spelled(w) for w in words}:
            problems = _prose_problems(ref.text, words, c, score)
            if not problems:
                return Lookup(ref, MATCHED, record=c, problems=problems)
            # Another record of the same work may fit cleanly (a 2025 re-post of a 2017 paper
            # ahead of the paper itself), so the first fit with problems waits for the rest.
            if first_fit is None:
                first_fit = Lookup(ref, MATCHED, record=c, problems=problems)
    if first_fit is not None:
        return first_fit
    # A title that matches outright, under a first author the registered work does not have:
    # the entry names the wrong people (a fabricated author list on a real title).
    if scored and scored[0][0] >= PROSE_STRONG_TITLE and len(_registered_families(scored[0][1])) >= 2:
        c = scored[0][1]
        problems = _prose_problems(ref.text, words, c, scored[0][0])
        if problems:
            return Lookup(ref, MATCHED, record=c, problems=problems)
    if scored and scored[0][0] >= PROSE_NEAR_OVERLAP:
        return Lookup(ref, NEAR, record=scored[0][1], candidates=[c for _, c in scored[:2]])
    return Lookup(ref, NOT_FOUND, candidates=[c for _, c in scored[:2]])


def _same_authors(ref: Reference, record: dict) -> bool:
    """Whether a reference names the record's people: its first author, and most of the first few."""
    families = _registered_families(record)[:6]
    if not families:
        return False
    if ref.text:
        words = set(_normalize_text(ref.text[:600]).split())
        # Common surnames (Zhang, Yang, Li) turn up in any long author list, so the entry's own
        # first author has to be the work's.
        lead = _family(_entry_lead_author(ref.text))
        if lead and lead != families[0]:
            return False
        return families[0] in words and sum(f in words for f in families) >= max(1, len(families) // 2)
    return bool(ref.author) and _family(ref.author) == families[0]


def _registered_initials(record: dict) -> dict[str, set[str]]:
    """Each word of a registered author's name -> the initials of that author's other name words."""
    out: dict[str, set[str]] = {}
    for a in record.get("author") or []:
        if not isinstance(a, dict):
            continue
        words = []
        for key in ("given", "family") if (a.get("given") or a.get("family")) else ("literal", "name"):
            words += [w for w in _normalize_text(a.get(key) or "").split() if w]
        if not words and a.get("literal"):
            words = _normalize_text(a["literal"]).split()
        for w in words:
            out.setdefault(w, set()).update(x[0] for x in words if x != w)
    return out


def _listed_among(ref: Reference, record: dict) -> bool:
    """Whether the people a reference lists are among the record's authors, in any order.

    An arXiv record is the latest version, which can add authors and reorder them
    ("Jian Hu" alone on v1 of REINFORCE++, fourth of four later), so the entry's own names are
    held to the whole registered list rather than to its first places. Each name has to agree
    in full, family name and initial, since common surnames (Yang, Zhang, Li, Liu) turn up in
    any two long author lists.
    """
    if not ref.text:
        return False
    initials = _registered_initials(record)
    text = re.sub(r"^\s*(?:\[\d+\]|\d+[.)])\s*", "", ref.text)
    end = _PROSE_AUTHORS_END.search(text, 0, 700)
    if not initials or end is None:
        return False
    names = [n for n in re.split(r",\s*(?:and\s+)?|\s+and\s+|\s+&\s+", re.sub(r",?\s*et\s+al\.?$", "", text[: end.start() + 1].rstrip("."))) if n.strip()]
    names = [_normalize_text(n).split() for n in names]
    names = [n for n in names if 1 <= len(n) <= 4]
    if not names:
        return False

    def agrees(words: list[str]) -> bool:
        for w in words:
            others = {x[0] for x in words if x != w}
            if w in initials and (not others or others & initials[w]):
                return True
        return False

    return sum(agrees(n) for n in names) >= 0.8 * len(names)


def match_arxiv(ref: Reference, record: dict | None) -> Lookup:
    """Compare a reference citing an arXiv id with what arXiv has under that id."""
    if record is None:
        return Lookup(ref, MATCHED, problems=[f"it cites arXiv:{ref.arxiv_id}, which arXiv has no record of"], source="arXiv")
    # arXiv papers are often retitled between versions ("EraseBench: ..." became "Erasing More
    # Than Intended? ..."), so a title that disagrees counts only when the authors do too.
    same_people = _same_authors(ref, record) or _listed_among(ref, record)
    if ref.title and title_similarity(ref.title, record["title"]) < NEAR_TITLE_RATIO and not same_people:
        return Lookup(ref, MATCHED, record=record, source="arXiv", problems=[f'it cites arXiv:{ref.arxiv_id}, but that identifier is "{record["title"]}"'])
    if ref.text and not ref.title and _prose_overlap(ref.text, record) < PROSE_NEAR_OVERLAP and not same_people:
        return Lookup(ref, MATCHED, record=record, source="arXiv", problems=[f'it cites arXiv:{ref.arxiv_id}, but that identifier is "{record["title"]}"'])
    given = _lead_given_conflict(ref.text, record) if ref.text else ""
    if given:
        return Lookup(ref, MATCHED, record=record, source="arXiv", problems=[f"it cites arXiv:{ref.arxiv_id}, but {given.split(': ', 1)[1]}"])
    return Lookup(ref, MATCHED, record=record, source="arXiv")


_WEB_PAGE_ENTRY = re.compile(r"(?is)\bhttps?://(?!(?:dx\.)?doi\.org)\S+.*\b(?:accessed|retrieved|last\s+visited)\b|\bweb\s?site\b.*\bhttps?://(?!(?:dx\.)?doi\.org)")


def is_web_page_entry(text: str) -> bool:
    """Whether a reference-list entry cites a web page ("... Web site. https://... Accessed May 1, 2026.")."""
    return bool(text) and "doi.org" not in text.lower() and bool(_WEB_PAGE_ENTRY.search(text))


# --- the searches -----------------------------------------------------------------------


# What a misread reference-list entry yields as its "title": a URL, or a list of people
# ("Ilya Loshchilov and Frank Hutter").
_NAME = r"[A-Z][a-z'\u00c0-\u017f-]+(?:\s+[A-Z]\.)*\s+[A-Z][a-z'\u00c0-\u017f-]+"
_NAME_LIST = re.compile(rf"^{_NAME}(?:\s*,\s*{_NAME})*,?\s+and\s+{_NAME}\.?$")


# ... or the venue ("Proceedings of the National Academy of Sciences, 115(34):8505-8510, 2018",
# read after an author named "Weinan E." whose initial looked like the end of the author list).
_VENUE_TITLE = re.compile(r"(?i)^(?:in\s*)?(?:proceedings\b|proc\.|advances\s+in\b|journal\s+of\b|transactions\s+on\b|(?:ieee|acm)\b|international\s+conference\b|conference\s+on\b|(?:the\s+)?(?:forty|thirty|twenty)[\w-]*\s+(?:international|annual|conference)\b|the\s+\w+\s+(?:international|annual)\s+conference\b)|\b\d+\s*\(\d+\)\s*:\s*\d+")


def _not_a_title(title: str) -> bool:
    t = title.strip()
    return bool(re.search(r"(?i)https?:|www\.|arxiv\.org|\bdoi\.org", t)) or bool(_NAME_LIST.match(t)) or bool(_VENUE_TITLE.search(t))


def _arxiv_second_look(ref: Reference, lookup: Lookup | None, title: str, svc: _Services, match) -> tuple[Lookup | None, list[dict] | None]:
    """Search arXiv's titles for a reference Crossref and OpenAlex did not match; the better of
    the two lookups, and arXiv's answer (None if it did not answer)."""
    if len(title.split()) < MIN_TITLE_WORDS:
        return lookup, None
    arx = datacite_arxiv_title_search(title, svc.datacite) if svc.datacite.available else None
    if arx is None and svc.arxiv.available:
        arx = arxiv_title_search(title, svc.arxiv)
    if arx is None:
        return lookup, None
    second = match(ref, arx)
    # Not "arXiv": that source means the entry cited an arXiv identifier (see search_references).
    names = [n for n in re.split(r",? and |, ", (lookup.source if lookup is not None else "") or "Crossref") if n] + ["arXiv titles"]
    source = names[0] if len(names) == 1 else f"{names[0]} and {names[1]}" if len(names) == 2 else ", ".join(names[:-1]) + ", and " + names[-1]
    if _better(second, lookup) or (second.status == NEAR and lookup.status == UNKNOWN):
        second.source = source
        return second, arx
    lookup.source = source
    if not lookup.candidates and second.candidates:
        lookup.candidates = second.candidates[:1]
    return lookup, arx


def _settled(lookup: Lookup | None) -> bool:
    """Whether a lookup ends the search: a match, unless it says the entry names the wrong people.

    A title matched under other authors may be a namesake (a later edition, a review, a copy
    under the same title) when another registry holds the work the entry names, so the search
    goes on to the next registry before that is reported.
    """
    return lookup is not None and lookup.status == MATCHED and not any(p.startswith(WRONG_AUTHORS) for p in lookup.problems)


def _better(second: Lookup, lookup: Lookup | None) -> bool:
    """Whether the next registry's lookup replaces the one so far."""
    if lookup is None or _settled(second):
        return True
    if lookup.status == MATCHED:
        return False
    return second.status == MATCHED or (second.status == NEAR and lookup.status == NOT_FOUND)


def _search_one(ref: Reference, svc: _Services) -> Lookup:
    if ref.title:
        query = " ".join(x for x in (ref.title, ref.author, ref.year) if x)
        items = crossref_search(query, svc.crossref) if svc.crossref.available else None
        lookup = match_entry(ref, items) if items is not None else None
        if lookup is not None:
            lookup.source = "Crossref"
        works = None
        if not _settled(lookup):
            works = openalex_search(ref.title, svc.openalex) if svc.openalex.available else None
            if works is not None:
                second = match_entry(ref, works)
                second.source = "OpenAlex" if lookup is None else "Crossref and OpenAlex"
                if _better(second, lookup):
                    lookup = second
                else:
                    lookup.source = "Crossref and OpenAlex"
                    lookup.candidates += second.candidates[:1]
        arx = None
        if not _settled(lookup):
            lookup, arx = _arxiv_second_look(ref, lookup, ref.title, svc, match_entry)
        if not _settled(lookup):
            lookup = _s2_last_look(ref, lookup, ref.title, svc, match_entry)
        # A miss means something only when Crossref and a second registry (OpenAlex or arXiv) answered.
        if lookup is not None and lookup.status == NOT_FOUND and (items is None or (works is None and arx is None)):
            lookup.status = UNKNOWN
        return lookup or Lookup(ref, UNKNOWN)
    items = crossref_search(ref.text, svc.crossref) if svc.crossref.available else None
    lookup = match_text(ref, items) if items is not None else None
    if lookup is not None:
        lookup.source = "Crossref"
    # Crossref lacks much of computer science (ICLR, many NeurIPS and arXiv papers), so an entry
    # it cannot match is also searched in OpenAlex by its own title. A miss in both, for a
    # cleanly read title, is a confident miss.
    own = _prose_title(ref.text)
    if _not_a_title(own):
        own = ""  # misread: neither searched nor counted as a confident miss
    works = None
    if (not _settled(lookup)) and len(own.split()) >= MIN_TITLE_WORDS and svc.openalex.available:
        works = openalex_search(own, svc.openalex)
        if works is not None:
            second = match_text(ref, works)
            second.source = "OpenAlex" if lookup is None else "Crossref and OpenAlex"
            if _better(second, lookup):
                lookup = second
            else:
                lookup.source = "Crossref and OpenAlex"
                lookup.candidates += second.candidates[:1]
            lookup.confident_miss = lookup.status == NOT_FOUND and items is not None
    # arXiv's titles also when OpenAlex answered: OpenAlex's free search misses many recent preprints.
    if not _settled(lookup) and len(own.split()) >= MIN_TITLE_WORDS:
        lookup, arx = _arxiv_second_look(ref, lookup, own, svc, match_text)
        if arx is not None and lookup is not None:
            lookup.confident_miss = lookup.status == NOT_FOUND and items is not None
    if not _settled(lookup) and len(own.split()) >= MIN_TITLE_WORDS:
        lookup = _s2_last_look(ref, lookup, own, svc, match_text)
    return lookup or Lookup(ref, UNKNOWN)


# Compound halves that a line-break hyphen joined into one word when the extraction dropped the
# hyphen ("self-" / "supervised" read as "selfsupervised"); re-split for the last search only.
_GLUED_COMPOUND = re.compile(r"\b(self|vision|cross|zero|few|semi|multi|open|fine|end|data|text|image|video|language|graph|reward|policy|task|model|object|sample|knowledge|domain|time|high|low|large|long|token|human)(?=[a-z]{4,}\b)", re.IGNORECASE)


def _rehyphenate(title: str) -> str:
    """``title`` with glued compounds split back ("selfsupervised" -> "self-supervised", "Allin-one" -> "All-in-one")."""
    title = re.sub(r"\b(all|one|end)(in|to)-", r"\1-\2-", title, flags=re.IGNORECASE)
    return _GLUED_COMPOUND.sub(r"\1-", title)


def _s2_last_look(ref: Reference, lookup: Lookup | None, title: str, svc: _Services, match) -> Lookup | None:
    """Ask Semantic Scholar for a reference every other registry missed or matched under other people.

    A match there replaces the lookup; its "not found" is added to the registries named in the
    finding; no answer leaves the lookup as it was.
    """
    if lookup is None or lookup.status not in (NOT_FOUND, MATCHED) or not svc.semanticscholar.available:
        return lookup
    found = semantic_scholar_match(title, svc.semanticscholar)
    if found == [] and _rehyphenate(title) != title:
        found = semantic_scholar_match(_rehyphenate(title), svc.semanticscholar)
    if found is None:
        return lookup
    names = [n for n in re.split(r",? and |, ", lookup.source or "Crossref") if n] + ["Semantic Scholar"]
    source = names[0] if len(names) == 1 else f"{names[0]} and {names[1]}" if len(names) == 2 else ", ".join(names[:-1]) + ", and " + names[-1]
    if found:
        second = match(ref, found)
        if _better(second, lookup):
            second.source = source
            second.confident_miss = False
            return second
    lookup.source = source
    return lookup


def search_references(refs: list[Reference], *, services: _Services | None = None) -> SearchResult:
    """Look up every reference with no DOI; see the module docstring for what is reported."""
    svc = services or _services()
    result = SearchResult()

    arxiv_refs = [r for r in refs if not r.doi and r.arxiv_id]
    if arxiv_refs:
        ids = sorted({r.arxiv_id for r in arxiv_refs})
        records, checked = _arxiv_records(ids, svc)
        if checked:
            result.arxiv_records = records
            result.arxiv_checked = checked
        for r in arxiv_refs:
            result.lookups.append(match_arxiv(r, records.get(r.arxiv_id)) if r.arxiv_id in checked else Lookup(r, UNKNOWN, source="arXiv"))

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

    _book_second_look(result, svc)

    for lk in result.lookups:
        if lk.status == MATCHED and lk.problems and lk.source != "arXiv" and not lk.ref.indexed and not any(p.startswith(WRONG_AUTHORS) for p in lk.problems):
            # A book, a web page, a prose entry: the "match" may be a review or
            # a namesake, so a disagreement is for a reader to settle.
            lk.status = NEAR
            lk.candidates = [lk.record] + lk.candidates if lk.record else lk.candidates
            lk.problems = []
        if lk.status == MATCHED and lk.problems:
            what = f"the closest registered work is {_describe(lk.record)}" if lk.record else ""
            result.findings.append(Finding(lk.ref.where, WRONG_MATCH, f"{lk.ref.label} looks wrong: {'; '.join(lk.problems)}" + (f"; {what}" if what and lk.source != "arXiv" else "") + ". Check it against the published work", LEVEL_LIKELY))
        elif lk.status == NOT_FOUND and (lk.ref.indexed or (lk.confident_miss and claims_indexed_venue(lk.ref.text))):
            searched = lk.source or "Crossref"
            near = f" (the closest title found was {_describe(lk.candidates[0])})" if lk.candidates else ""
            result.findings.append(
                Finding(
                    lk.ref.where,
                    NO_MATCH,
                    f'{lk.ref.label} ("{(lk.ref.title or _prose_title(lk.ref.text))[:120]}", {lk.ref.author or _entry_lead_author(lk.ref.text) or "no author"}, {lk.ref.year or "no year"}) matches no work in {searched}{near}. Check that it exists and its title is right; a reference that cannot be found may be fabricated',
                    LEVEL_LIKELY,
                )
            )
        elif lk.status == MATCHED and lk.source != "arXiv" and lk.record and lk.record.get("DOI"):
            result.findings += _retraction_finding(lk.ref.where, lk.ref.label, str(lk.record["DOI"]).lower(), lk.record)
    return result


def _book_second_look(result: SearchResult, svc: _Services) -> None:
    """Search Open Library for the books Crossref and OpenAlex did not match.

    A match takes a book off the unverified list. A miss changes nothing: a book
    is never accused for want of a record.
    """
    searched = 0
    for i, lk in enumerate(result.lookups):
        ref = lk.ref
        if lk.status == MATCHED or lk.source == "arXiv" or not _is_book_reference(ref):
            continue
        if searched >= MAX_BOOK_SEARCHES or not svc.openlibrary.available:
            break
        if ref.title:
            title, author, match = ref.title, ref.author, match_entry
        else:
            title, author, match = _prose_title(ref.text), _entry_lead_author(ref.text), match_text
            if _not_a_title(title):
                continue
        if len(_normalize_text(title).split()) < MIN_TITLE_WORDS:
            continue
        searched += 1
        books = openlibrary_search(title, author, ref.year, svc.openlibrary)
        if not books:
            continue
        second = match(ref, books)
        if second.status == MATCHED and not second.problems:
            second.source = f"{lk.source}, then Open Library" if lk.source else "Open Library"
            result.lookups[i] = second


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
    preprint_version: int | None = None


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
        elif c.preprint_version and c.doi.startswith(_PREPRINT_DOI_PREFIXES) and svc.biorxiv.available:
            latest = biorxiv_latest_version(c.doi, svc.biorxiv)
            if latest and c.preprint_version < latest:
                done.add(c.label)
                findings.append(Finding(c.where, "older preprint version", f"{c.label} cites version {c.preprint_version} of the preprint {c.doi}, but the latest version is {latest}; cite the latest version (or drop the version number)"))

    records = dict(arxiv_records or {})
    checked = set(arxiv_checked)
    missing = sorted({r.arxiv_id for r in arxiv_refs} - checked)
    if missing:
        answer, asked = _arxiv_records(missing, svc)
        records.update(answer)
        checked |= asked
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


def flag_id(detail: str) -> str:
    """A short id for one flagged reference, the same on every run that reports it the same way.

    The agent names the flags it clears by these ids, in a results file written
    after one validate run and read by the next, so they come from the finding's
    text rather than its position.
    """
    return "ref-" + hashlib.sha1(detail.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


def flagged_task_instructions(flags: dict[str, str]) -> str:
    """Instructions for ``ai.flagged_references``: confirm or clear what the lookups reported.

    ``flags`` maps each flag's id (:func:`flag_id`) to the message the report shows.
    """
    shown = list(flags.items())[:MAX_AI_ITEMS]
    lines = [f"- {fid}: {message}" for fid, message in shown]
    if len(flags) > len(shown):
        lines.append(f"- ... and {len(flags) - len(shown)} more, which stay as reported.")
    return (
        "paperpush's registry lookups (doi.org, Crossref, OpenAlex, arXiv, Open Library) reported these references as likely errors. "
        "The lookups compare titles and author names as strings, so they misfire on a title written differently from the "
        "registered one, a conference paper indexed under its proceedings, a group or consortium author, an entry garbled "
        "by PDF text extraction, and kinds of work the registries index poorly. For each flag, look the reference up (the "
        "web, Crossref, OpenAlex, DBLP, arXiv, the publisher's page) and compare what you find with the entry in the files. "
        "A flag is a false positive only when the work exists and the entry's title, authors, year, and DOI (if it gives "
        "one) describe that work; a real work cited with another paper's DOI or another book's ISBN, the wrong authors, or the wrong year is "
        "flagged correctly. Do not judge from memory alone. Report 'pass' if every flag is a false positive, 'fail' if any "
        "flag is right (name each one, say what is wrong, and give the correct details where you found them), and "
        "'unclear' if you could not look them up. Whatever the status, add a 'cleared' list with the ids of the flags you "
        'verified to be false positives (for example "cleared": ["ref-0123abcd"]): the report lowers those to warnings. '
        "Leave out any flag you could not verify.\n" + "\n".join(lines)
    )
