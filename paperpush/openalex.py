"""A small OpenAlex client for ``pick-venue`` and ``find-reviewers``.

OpenAlex (https://openalex.org) indexes most of the scholarly record -- works,
their authors and institutions, and the journals and repositories (sources)
they appear in -- behind a free JSON API. Two commands read it:

* ``paperpush pick-venue`` groups the works most similar to a manuscript by the
  source they appeared in, to suggest journals beyond the ones paperpush
  supports (:func:`paperpush.venue_select.find_journals`).
* ``paperpush find-reviewers`` collects the authors of those works and screens
  out the manuscript's own authors, their recent co-authors, and colleagues
  (:mod:`paperpush.reviewer_finder`).

Without a key every request shares OpenAlex's small free daily budget with
everyone on the same IP address. Set ``OPENALEX_API_KEY`` to use your own. The
User-Agent is generic: no user e-mail address is ever sent.

Every request goes through :func:`get_json`, which raises
:class:`OpenAlexError` when OpenAlex cannot be reached or answers with an
error, so callers can fall back cleanly. Tests patch :func:`get_json`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Iterable

logger = logging.getLogger(__name__)

BASE_URL = "https://api.openalex.org"
USER_AGENT = "paperpush (+https://github.com/pachterlab/paperpush)"
TIMEOUT = 20
# OpenAlex caps an ``a|b|c`` filter at 50 values (and ``per_page`` at 200).
BATCH = 50
MAX_PER_PAGE = 200
_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRY_DELAY = 2.0
RETRIES = 3


def _retry_after(exc: urllib.error.HTTPError, attempt: int) -> float:
    """Seconds to wait before retrying: the server's Retry-After (capped), else a growing delay."""
    try:
        return min(float(exc.headers.get("Retry-After", "")), 30.0)
    except (TypeError, ValueError, AttributeError):
        return _RETRY_DELAY * (attempt + 1)

# The most terms a similarity search ORs together.
MAX_TERMS = 20
# Works scoring under this share of the best match's similarity are dropped (see rerank).
MIN_SIMILARITY = 0.2

_STOPWORDS = frozenset(
    """a about above across after again against all almost also although always among an and another any are around as at
    based be because been before being below between both but by can could did do does done due during each either
    enable enables especially etc even ever every few for from further given had has have having here how however i if in
    into is it its itself just less like made make makes many may more most much must new no nor not novel now of off
    often on once one only onto or other others our ours out over own per present presents propose proposed provide
    provides rather same several shall she should show shows shown since so some such than that the their them then
    there these they this those though three through thus to too toward towards two under until up upon us use used
    uses using various very via was we well were what when where whether which while who whose why will with within
    without would yet you your approach approaches method methods result results study studies paper work works data
    analysis analyses find found finding findings first second however here demonstrate demonstrates demonstrated
    describe describes described develop developed report reported high higher low lower large small different important
    including include includes across known key level levels across across within allow allows allowed""".split()
)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*")


class OpenAlexError(RuntimeError):
    """OpenAlex could not be reached, or it answered with an error."""


def fold(text: str) -> str:
    """Lower-case ``text`` with accents removed and punctuation turned into spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def short_id(value: str) -> str:
    """``https://openalex.org/S123`` -> ``S123`` (an already short id is kept)."""
    return (value or "").rstrip("/").rsplit("/", 1)[-1]


def bare_orcid(value: str) -> str:
    """The ``0000-0000-0000-0000`` part of an ORCID iD or URL, or ``""``."""
    m = re.search(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", value or "")
    return m.group(0) if m else ""


def get_json(path: str, params: dict | None = None) -> dict:
    """GET ``BASE_URL + path`` and return the decoded JSON object.

    Adds ``OPENALEX_API_KEY`` when it is set and retries on a rate limit or
    server error (up to :data:`RETRIES` times). Raises :class:`OpenAlexError` on
    anything else.
    """
    params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if key:
        params["api_key"] = key
    url = f"{BASE_URL}{path}" + ("?" + urllib.parse.urlencode(params, safe=":|,") if params else "")
    shown = re.sub(r"api_key=[^&]+", "api_key=…", url)
    for attempt in range(RETRIES + 1):
        last = attempt == RETRIES
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # nosec B310 -- fixed https host
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code in _RETRY_STATUS and not last:
                time.sleep(_retry_after(exc, attempt))
                continue
            hint = " (too many requests: OpenAlex's shared free budget may be used up; set OPENALEX_API_KEY to use your own)" if exc.code == 429 else ""
            raise OpenAlexError(f"OpenAlex answered HTTP {exc.code}{hint}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            if not last:
                time.sleep(_RETRY_DELAY * (attempt + 1))
                continue
            raise OpenAlexError(f"could not reach OpenAlex ({exc})") from exc
        if not isinstance(data, dict) or not isinstance(data.get("results", []), list):
            raise OpenAlexError("OpenAlex returned an unexpected response")
        logger.debug("OpenAlex %s -> %d result(s)", shown, len(data.get("results") or []))
        return data
    raise OpenAlexError("could not reach OpenAlex")  # pragma: no cover -- the loop always returns or raises


def _batches(values: Iterable[str], size: int = BATCH) -> list[list[str]]:
    items = list(dict.fromkeys(v for v in values if v))
    return [items[i : i + size] for i in range(0, len(items), size)]


def search_terms(title: str = "", abstract: str = "", keywords: Iterable[str] = (), max_terms: int = MAX_TERMS) -> str:
    """An OpenAlex ``search`` string for works similar to a manuscript.

    OpenAlex requires every word of a plain search to match, which a whole
    abstract never does. Instead the keywords (as quoted phrases), the title's
    content words, and the abstract's most frequent content words are ORed
    together; OpenAlex then ranks works by how many of them match, and how well.
    """
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        term = " ".join(term.replace('"', " ").split())
        key = fold(term)
        if term and key and key not in seen and len(terms) < max_terms:
            seen.add(key)
            terms.append(f'"{term}"' if (" " in term or "-" in term) else term)

    for kw in keywords:
        if len(kw.split()) <= 4:
            add(kw)
    for word in _WORD.findall(title or ""):
        if word.lower() not in _STOPWORDS and len(word) > 2:
            add(word)
    counts = Counter(w for w in (m.lower() for m in _WORD.findall(abstract or "")) if w not in _STOPWORDS and len(w) >= 5)
    for word, _ in counts.most_common():
        if len(terms) >= max_terms:
            break
        add(word)
    return " OR ".join(terms)


_WORK_FIELDS = "id,doi,display_name,publication_year,relevance_score,primary_location,authorships,abstract_inverted_index"


def _tokens(text: str) -> list[str]:
    """Content words of ``text``, lower-cased, with a plural ``s`` dropped."""
    out = []
    for word in _WORD.findall(text or ""):
        word = word.lower()
        if word in _STOPWORDS or len(word) < 3:
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.append(word)
    return out


def work_abstract(work: dict) -> str:
    """A work's abstract, rebuilt from OpenAlex's inverted index (``""`` when absent)."""
    index = work.get("abstract_inverted_index") or {}
    if not isinstance(index, dict):
        return ""
    slots: dict[int, str] = {}
    for word, positions in index.items():
        for pos in positions or ():
            if isinstance(pos, int):
                slots[pos] = word
    return " ".join(slots[i] for i in sorted(slots))


def rerank(works: list[dict], title: str = "", abstract: str = "", keywords: Iterable[str] = ()) -> list[dict]:
    """``works`` re-scored by how much of their title and abstract they share with the manuscript.

    An ORed search ranks a work that matches one common word of the query
    nearly as high as one matching its rare ones, so each work's title and
    abstract are compared with the manuscript's (TF-IDF cosine over the
    retrieved works; the manuscript's title and keywords count double). Each
    returned work carries ``similarity`` in [0, 1] (the best match is 1, the
    weight callers use), best first; works sharing little with the manuscript
    (under :data:`MIN_SIMILARITY` of the best match) are dropped, and so is the
    manuscript itself when OpenAlex already has it.
    """
    import math

    query_tokens = _tokens(abstract) + 2 * (_tokens(title) + _tokens(" ".join(keywords)))
    # The manuscript itself, when OpenAlex already has it, is not "similar work".
    own = fold(title)
    works = [w for w in works if not own or fold(w.get("display_name") or "") != own]
    if not query_tokens or not works:
        return []
    docs = [_tokens(f"{w.get('display_name') or ''} {w.get('display_name') or ''} {work_abstract(w)}") for w in works]
    df = Counter(t for d in docs + [query_tokens] for t in set(d))
    n = len(docs) + 1

    def vector(tokens: list[str]) -> dict[str, float]:
        tf = Counter(tokens)
        return {t: (1 + math.log(c)) * math.log((n + 1) / df[t]) for t, c in tf.items()}

    q = vector(query_tokens)
    q_norm = math.sqrt(sum(v * v for v in q.values())) or 1.0
    scored: list[tuple[float, dict]] = []
    for work, doc in zip(works, docs):
        v = vector(doc)
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        scored.append((sum(q[t] * v[t] for t in q.keys() & v.keys()) / (q_norm * norm), work))
    best = max((s for s, _ in scored), default=0.0)
    if best <= 0:
        return []
    kept = [dict(w, similarity=round(s / best, 4)) for s, w in scored if s >= MIN_SIMILARITY * best]
    return sorted(kept, key=lambda w: -w["similarity"])


def similar_works(title: str = "", abstract: str = "", keywords: Iterable[str] = (), *, since_year: int | None = None, per_page: int = MAX_PER_PAGE, types: str = "article|preprint|review") -> tuple[str, list[dict]]:
    """The works most similar to a manuscript, best match first (see :func:`rerank`), and the search used.

    Raises :class:`ValueError` when there is nothing to search on, and
    :class:`OpenAlexError` when OpenAlex does not answer.
    """
    query = search_terms(title, abstract, keywords)
    if not query:
        raise ValueError("nothing to search on: give a title, abstract, or keywords")
    filters = [f"type:{types}"] if types else []
    if since_year:
        filters.append(f"from_publication_date:{since_year}-01-01")
    data = get_json("/works", {"search": query, "filter": ",".join(filters), "per_page": min(per_page, MAX_PER_PAGE), "select": _WORK_FIELDS})
    works = [w for w in data.get("results") or [] if isinstance(w, dict)]
    return query, rerank(works, title, abstract, keywords)


def sources(ids: Iterable[str]) -> dict[str, dict]:
    """Source records (journals, repositories, ...) keyed by short id, fetched 50 at a time."""
    out: dict[str, dict] = {}
    select = "id,display_name,issn_l,homepage_url,host_organization_name,is_oa,is_in_doaj,apc_usd,summary_stats,type"
    for batch in _batches(short_id(i) for i in ids):
        data = get_json("/sources", {"filter": "openalex_id:" + "|".join(batch), "per_page": len(batch), "select": select})
        for src in data.get("results") or []:
            out[short_id(src.get("id", ""))] = src
    return out


_AUTHOR_FIELDS = "id,display_name,orcid,last_known_institutions,works_count,summary_stats"


def authors(ids: Iterable[str]) -> dict[str, dict]:
    """Author records keyed by short id, fetched 50 at a time."""
    out: dict[str, dict] = {}
    for batch in _batches(short_id(i) for i in ids):
        data = get_json("/authors", {"filter": "openalex_id:" + "|".join(batch), "per_page": len(batch), "select": _AUTHOR_FIELDS})
        for a in data.get("results") or []:
            out[short_id(a.get("id", ""))] = a
    return out


def authors_by_orcid(orcids: Iterable[str]) -> list[dict]:
    """Author records for ORCID iDs (bare or URL form)."""
    found: list[dict] = []
    for batch in _batches(bare_orcid(o) for o in orcids):
        data = get_json("/authors", {"filter": "orcid:" + "|".join(batch), "per_page": MAX_PER_PAGE, "select": _AUTHOR_FIELDS})
        found.extend(a for a in data.get("results") or [] if isinstance(a, dict))
    return found


def author_search(name: str, per_page: int = 5) -> list[dict]:
    """Authors whose name matches ``name``, most prolific first."""
    data = get_json("/authors", {"search": name, "per_page": per_page, "select": _AUTHOR_FIELDS})
    return [a for a in data.get("results") or [] if isinstance(a, dict)]


def works_by_authors(author_ids: Iterable[str], *, since_year: int | None = None, max_pages: int = 2) -> list[dict]:
    """Recent works by any of ``author_ids`` (their authorships only), up to ``max_pages`` pages per batch."""
    works: list[dict] = []
    for batch in _batches(short_id(i) for i in author_ids):
        filters = ["authorships.author.id:" + "|".join(batch)]
        if since_year:
            filters.append(f"from_publication_date:{since_year}-01-01")
        for page in range(1, max_pages + 1):
            data = get_json("/works", {"filter": ",".join(filters), "per_page": MAX_PER_PAGE, "page": page, "select": "id,publication_year,authorships"})
            results = [w for w in data.get("results") or [] if isinstance(w, dict)]
            works.extend(results)
            if len(results) < MAX_PER_PAGE:
                break
    return works
