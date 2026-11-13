"""Conference metadata for ``pick-venue``: rankings, deadlines, format, and archival status.

OpenAlex says which conferences publish work like a manuscript's, but not when
they take submissions, whether they want a paper or an abstract, or how they
rank. Three open sources fill that in, each feed downloaded at most once a day and
cached under ``~/.cache/paperpush/conferences``:

* **conference-agent** (https://conferenceagent.vercel.app): a curated list of
  conferences in AI, medicine, genomics, and more, with deadlines, the
  submission format (abstract, paper, poster), size, and submission
  guidelines. It is the only one of the three with the abstract-only meetings
  of biology and medicine, which publish no proceedings and so are missing
  from OpenAlex.
* **CORE** (https://portal.core.edu.au/conf-ranks/): the CORE rankings of
  computer-science conferences (A*, A, B, C), about 950 of them.
* **ccfddl** (https://ccfddl.com): computer-science conference deadlines, with
  each conference's CCF and CORE rank, about 400 of them, and a second feed of
  their yearly acceptance rates (submitted and accepted papers).

:class:`Catalog` matches a conference name (an OpenAlex source name, a
paperpush venue name, or an acronym) against all three and merges what they
say into one :class:`ConferenceInfo`. A feed that cannot be reached and has no
cached copy is skipped and named in :attr:`Catalog.errors`; set
``PAPERPUSH_OFFLINE=1`` to use only cached copies.

"Archival" here means the conference publishes full papers in proceedings that
count as a publication. A non-archival meeting (an abstract, poster, or talk)
does not, so the work can still go to a journal afterwards.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

CONFERENCE_AGENT_URL = "https://conferenceagent.vercel.app/data/conferences.json"
CORE_URL = "https://portal.core.edu.au/conf-ranks/?search=&by=all&source=CORE2023&sort=atitle&page=1&do=Export"
CCFDDL_URL = "https://ccfddl.com/conference/allconf.json"
CCFDDL_ACCEPT_URL = "https://ccfddl.com/conference/allacc.json"

USER_AGENT = "paperpush (+https://github.com/pachterlab/paperpush)"
TIMEOUT = 20
#: How long a downloaded feed is used before it is fetched again.
MAX_AGE_SECONDS = 24 * 3600

#: ccfddl's subject codes, as pick-venue keywords.
CCF_SUBJECTS = {
    "AI": "artificial intelligence",
    "CG": "computer graphics",
    "CT": "theory of computation",
    "DB": "databases, data mining",
    "DS": "computer systems",
    "HI": "human-computer interaction",
    "MX": "interdisciplinary computing",
    "NW": "networks",
    "SC": "security",
    "SE": "software engineering",
}

#: ANZSRC 2020 field-of-research codes that CORE tags conferences with.
CORE_FIELDS = {
    "46": "computing",
    "4601": "applied computing",
    "4602": "artificial intelligence",
    "4603": "computer vision, multimedia",
    "4604": "security, privacy",
    "4605": "data management, data science",
    "4606": "distributed systems",
    "4607": "graphics, games",
    "4608": "human-computer interaction",
    "4609": "information systems",
    "4610": "information studies",
    "4611": "machine learning",
    "4612": "software engineering",
    "4613": "theory of computation",
    "4006": "communications engineering",
    "4009": "electronics",
}

#: Words that do not tell one conference from another ("Proceedings of the
#: International Conference on ...").
_NOISE = frozenset(
    "a an and at by for in of on the to proceedings proceeding annual international conference conferences "
    "symposium meeting congress ieee cvf acm usenix siam society advances edition joint".split()
)
#: Words that mark a satellite venue (a workshop, the Findings, a demo track):
#: one name carrying them never matches a name without them.
_SATELLITE = frozenset("workshop workshops findings companion demo demos demonstrations student doctoral tutorial tutorials adjunct".split())


@dataclass(frozen=True)
class ConferenceInfo:
    """What the feeds say about one conference. Empty strings mean "not known"."""

    acronym: str = ""
    name: str = ""
    link: str = ""
    keywords: tuple[str, ...] = ()
    core_rank: str = ""  # A*, A, B, C, or another CORE label ("National", "Unranked")
    ccf_rank: str = ""  # A, B, C (China Computer Federation)
    abstract_deadline: str = ""  # YYYY-MM-DD, the next one
    paper_deadline: str = ""  # YYYY-MM-DD, the next one
    deadline_estimated: bool = False  # the dates are last cycle's moved forward a year
    deadline_timezone: str = ""
    start_date: str = ""
    location: str = ""
    format: str = ""  # "paper", "abstract", or "abstract + paper"
    limit: str = ""  # e.g. "8 pages", "300 words (abstract)"
    archival: str = ""  # "yes", "no", or "" (unknown)
    size: str = ""  # conference-agent: small, medium, large, massive
    acceptance_rate: str = ""  # ccfddl: the latest year's, e.g. "23.4% (2025)"
    attendance: int | None = None
    sources: tuple[str, ...] = ()

    def deadlines_text(self) -> str:
        """The next deadlines as one short line, e.g. ``abstract 2027-05-04, paper 2027-05-11``."""
        dated = []
        if self.abstract_deadline and self.abstract_deadline != self.paper_deadline:
            dated.append((self.abstract_deadline, "abstract"))
        if self.paper_deadline:
            dated.append((self.paper_deadline, "abstract" if self.format == "abstract" and not dated else "paper"))
        text = ", ".join(f"{label} {day}" for day, label in sorted(dated))
        return f"~{text} (est.)" if text and self.deadline_estimated else text

    def to_dict(self) -> dict:
        return {
            "acronym": self.acronym or None,
            "name": self.name or None,
            "link": self.link or None,
            "keywords": list(self.keywords),
            "core_rank": self.core_rank or None,
            "ccf_rank": self.ccf_rank or None,
            "abstract_deadline": self.abstract_deadline or None,
            "paper_deadline": self.paper_deadline or None,
            "deadline_estimated": self.deadline_estimated,
            "deadline_timezone": self.deadline_timezone or None,
            "start_date": self.start_date or None,
            "location": self.location or None,
            "format": self.format or None,
            "limit": self.limit or None,
            "archival": {"yes": True, "no": False}.get(self.archival),
            "size": self.size or None,
            "acceptance_rate": self.acceptance_rate or None,
            "attendance": self.attendance,
            "sources": list(self.sources),
        }


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def tokens(name: str) -> frozenset[str]:
    """The distinguishing words of a conference name, lower-cased.

    Parenthesized parts (an acronym, a publisher), years, ordinals, and the
    words in :data:`_NOISE` are dropped. An OpenAlex name made of two names
    joined by ``/`` keeps only the first.
    """
    # "A/Proceedings - A" names one venue twice; "IEEE/CVF Conference ..." does not.
    first, sep, _ = (name or "").partition("/")
    name = first if sep and len(first.split()) >= 3 else (name or "").replace("/", " ")
    name = re.sub(r"\([^)]*\)", " ", name)
    words = re.findall(r"[a-z0-9]+", _fold(name))
    return frozenset(w for w in words if w not in _NOISE and not w.isdigit() and not re.fullmatch(r"\d+(st|nd|rd|th)", w))


def acronym_of(name: str) -> str:
    """The acronym given in parentheses in ``name`` (``"... (EMNLP)"`` -> ``EMNLP``), or ``""``."""
    for inner in re.findall(r"\(([^)]*)\)", name or ""):
        inner = inner.strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9&\-*]{1,15}", inner) and any(c.isupper() for c in inner[1:] + inner[:1]):
            return inner
    return ""


def _acronym_key(acronym: str) -> str:
    """``"NeurIPS 2026"`` / ``"neurips"`` -> ``neurips``: the acronym without years or punctuation."""
    return re.sub(r"[^a-z]", "", re.sub(r"\d{2,4}$", "", _fold(acronym).strip()))


def name_match(a: frozenset[str], b: frozenset[str]) -> int:
    """How well two token sets name the same conference: 0 (no), else higher is closer.

    Equal sets match best. Otherwise the smaller set (at least three words) must
    lie inside the larger, with at most two extra words, and a satellite word
    (workshop, findings, ...) on one side only rules a match out.
    """
    if not a or not b:
        return 0
    if (a & _SATELLITE) != (b & _SATELLITE):
        return 0
    if a == b:
        return 100
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    if len(small) >= 3 and small <= large and len(large) - len(small) <= 2:
        return 90 - 10 * (len(large) - len(small))
    return 0


@dataclass(frozen=True)
class _Entry:
    """One feed's record, indexed for matching."""

    acronym_key: str
    names: tuple[frozenset[str], ...]
    info: ConferenceInfo


def _words(name: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", _fold(name)))


def _best(entries: Iterable[_Entry], name_tokens: Iterable[frozenset[str]], acronyms: Iterable[str], raw_names: Iterable[str] = ()) -> ConferenceInfo | None:
    """The entry whose name (or acronym) matches best, or ``None``.

    Ties go to the entry sharing the most words with ``raw_names`` counting the
    usual noise words, so "International Conference on Computer Vision" is ICCV
    rather than the "Computer Vision Conference".
    """
    raw = [_words(n) for n in raw_names if n]
    keys = {k for k in (_acronym_key(a) for a in acronyms) if len(k) >= 2}
    queries = [t for t in name_tokens if t]
    best, best_score = None, (0, 0.0)
    for entry in entries:
        named = max((name_match(q, n) for q in queries for n in entry.names), default=0)
        same_acronym = bool(entry.acronym_key) and entry.acronym_key in keys
        if keys and entry.acronym_key and not same_acronym and min((len(n) for n in entry.names), default=0) < 3:
            named = 0  # "Computer Vision Conference" (CVC) is not ICCV just because both say "computer vision"
        if same_acronym and not named and any(len(q) >= 2 for q in queries) and not any(q & n for q in queries for n in entry.names):
            same_acronym = False  # ATS the thoracic society is not ATS the test symposium
        score = max(named, 95 if same_acronym else 0) + (10 if same_acronym and named else 0)
        if not score:
            continue
        words = _words(entry.info.name)
        overlap = max((len(r & words) / len(r | words) for r in raw if r | words), default=0.0)
        score = (score, overlap)
        if score > best_score:
            best, best_score = entry.info, score
    return best


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def _day(value) -> date | None:
    """A ``YYYY-MM-DD[ HH:MM:SS]`` string as a date, or ``None`` (``TBD``, blank)."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _next_year(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 29 February
        return d.replace(year=d.year + 1, day=28)


def next_cycle(abstract: date | None, paper: date | None, today: date) -> tuple[date | None, date | None, bool]:
    """The next abstract and paper deadlines, and whether they are estimated.

    While the paper deadline (or, without one, the abstract deadline) is still
    ahead, the dates stand, minus an abstract deadline that has passed. Once
    both have passed, both move forward by the same number of years.
    """
    key = paper or abstract
    if key is None:
        return None, None, False
    if key >= today:
        return (abstract if abstract and abstract >= today else None), paper, False
    years = 0
    while key < today:
        key, years = _next_year(key), years + 1
    shift = lambda d: _shift(d, years) if d else None  # noqa: E731
    return shift(abstract), shift(paper), True


def _shift(d: date, years: int) -> date:
    for _ in range(years):
        d = _next_year(d)
    return d



# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "paperpush" / "conferences"


def _offline() -> bool:
    return os.environ.get("PAPERPUSH_OFFLINE", "").lower() in {"1", "true", "yes"}


def _download(url: str) -> bytes:
    """GET ``url``. Tests patch this; the suite-wide fixture makes it fail."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # nosec B310 -- fixed https hosts
        return resp.read()


def _feed(name: str, url: str, *, refresh: bool = False) -> bytes:
    """A feed's raw bytes: the cached copy when fresh, else downloaded (falling back to a stale copy).

    Raises :class:`OSError` when there is neither a download nor a cached copy.
    """
    path = cache_dir() / name
    fresh = path.exists() and time.time() - path.stat().st_mtime < MAX_AGE_SECONDS
    if path.exists() and (fresh or _offline()) and not refresh:
        return path.read_bytes()
    if _offline():
        raise OSError(f"no cached copy of {name} (PAPERPUSH_OFFLINE is set)")
    try:
        data = _download(url)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        if path.exists():
            logger.info("could not refresh %s (%s); using the cached copy", name, exc)
            return path.read_bytes()
        raise OSError(f"could not download {url} ({exc})") from exc
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:  # a read-only home still gets the data
        logger.info("could not cache %s: %s", name, exc)
    return data


def _limit_from_text(text: str, what: str = "") -> str:
    """The first word, character, or page limit stated in free text, e.g. ``"300 words"``."""
    m = re.search(r"(\d[\d,]*)\s*-?\s*(words?|characters?|pages?)\b", text or "", re.IGNORECASE)
    if not m:
        return ""
    unit = m.group(2).lower().rstrip("s") + "s"
    out = f"{m.group(1)} {unit}"
    return f"{out} ({what})" if what else out


def _format_label(fmt: str) -> str:
    f = (fmt or "").lower()
    if "paper" in f:
        return "abstract + paper" if "abstract" in f else "paper"
    if f.strip():
        return "abstract"
    return ""


def parse_conference_agent(raw: bytes, today: date) -> list[_Entry]:
    data = json.loads(raw.decode("utf-8", "replace"))
    rows = data.get("conferences") if isinstance(data, dict) else data
    entries = []
    for c in rows or []:
        if not isinstance(c, dict):
            continue
        fmt = _format_label(c.get("format") or "")
        archival = "yes" if fmt in ("paper", "abstract + paper") else ("no" if fmt == "abstract" else "")
        abstract, paper, estimated = next_cycle(
            _day(c.get("upcoming_abstract_deadline")) or _day(c.get("prior_abstract_deadline")),
            _day(c.get("upcoming_paper_deadline")) or _day(c.get("prior_paper_deadline")),
            today,
        )
        limit = _limit_from_text(c.get("paper_guidelines") or "") or _limit_from_text(c.get("abstract_guidelines") or "", "abstract")
        keywords = [k.strip() for k in (c.get("subcategory") or "").split(",") if k.strip()] or [k.strip() for k in (c.get("category") or "").split(",") if k.strip()]
        info = ConferenceInfo(
            acronym=c.get("acronym") or "",
            name=c.get("name") or "",
            link=c.get("url") or "",
            keywords=tuple(keywords),
            abstract_deadline=abstract.isoformat() if abstract else "",
            paper_deadline=paper.isoformat() if paper else "",
            deadline_estimated=estimated,
            deadline_timezone=c.get("deadline_time") or "",
            start_date=c.get("upcoming_start_date") or "",
            location=c.get("upcoming_location") or "",
            format=fmt,
            limit=limit,
            archival=archival,
            size=c.get("size") or "",
            attendance=c.get("attendance") if isinstance(c.get("attendance"), int) else None,
            sources=("conference-agent",),
        )
        entries.append(_Entry(_acronym_key(info.acronym), (tokens(info.name),), info))
    return entries


def parse_core(raw: bytes) -> list[_Entry]:
    """CORE's CSV export: id, title, acronym, source, rank, primary?, then field-of-research codes."""
    entries = []
    for row in csv.reader(io.StringIO(raw.decode("utf-8", "replace"))):
        if len(row) < 5 or not row[1].strip():
            continue
        title, acronym, rank = row[1].strip(), row[2].strip(), row[4].strip()
        if rank.lower() in ("rank", ""):
            continue
        fields = [CORE_FIELDS[c.strip()] for c in row[6:9] if c.strip() in CORE_FIELDS]
        info = ConferenceInfo(acronym=acronym, name=title, keywords=tuple(dict.fromkeys(fields)), core_rank=rank, format="paper", archival="yes", sources=("CORE",))
        entries.append(_Entry(_acronym_key(acronym), (tokens(title),), info))
    return entries


def _rank(value) -> str:
    """A ccfddl rank, or ``""`` for its "not ranked" markers."""
    text = str(value or "").strip()
    return "" if text in ("N", "None", "-") else text


def parse_ccfddl(raw: bytes, today: date) -> list[_Entry]:
    data = json.loads(raw.decode("utf-8", "replace"))
    entries = []
    for c in data if isinstance(data, list) else []:
        if not isinstance(c, dict):
            continue
        rank = c.get("rank") or {}
        best = None  # (deadline, abstract, conf, estimated)
        latest = None
        for conf in c.get("confs") or []:
            for t in conf.get("timeline") or []:
                d, a = _day(t.get("deadline")), _day(t.get("abstract_deadline"))
                if d is None:
                    continue
                if d >= today and (best is None or d < best[0]):
                    best = (d, a, conf, False)
                if latest is None or d > latest[0]:
                    latest = (d, a, conf)
        if best is None and latest is not None:
            a, d, est = next_cycle(latest[1], latest[0], today)
            best = (d, a, latest[2], est)
        elif best is not None and best[1] and best[1] < today:
            best = (best[0], None, best[2], False)  # the abstract deadline has passed
        conf = best[2] if best else ((c.get("confs") or [{}])[-1] or {})
        info = ConferenceInfo(
            acronym=c.get("title") or "",
            name=c.get("description") or "",
            link=conf.get("link") or "",
            keywords=(CCF_SUBJECTS[c["sub"]],) if c.get("sub") in CCF_SUBJECTS else (),
            core_rank=_rank(rank.get("core")),
            ccf_rank=_rank(rank.get("ccf")),
            abstract_deadline=best[1].isoformat() if best and best[1] else "",
            paper_deadline=best[0].isoformat() if best else "",
            deadline_estimated=bool(best and best[3]),
            deadline_timezone=conf.get("timezone") or "",
            location=conf.get("place") or "",
            format="paper",
            archival="yes",
            sources=("ccfddl",),
        )
        entries.append(_Entry(_acronym_key(info.acronym), (tokens(info.name),), info))
    return entries


def parse_ccfddl_acceptance(raw: bytes) -> dict[str, str]:
    """ccfddl's acceptance-rate feed: conference title -> the latest year's rate, e.g. ``"23.4% (2025)"``."""
    data = json.loads(raw.decode("utf-8", "replace"))
    out = {}
    for c in data if isinstance(data, list) else []:
        if not isinstance(c, dict) or not c.get("title"):
            continue
        rates = [r for r in c.get("accept_rates") or [] if isinstance(r, dict) and isinstance(r.get("rate"), (int, float)) and isinstance(r.get("year"), int)]
        if rates:
            latest = max(rates, key=lambda r: r["year"])
            out[c["title"]] = f"{latest['rate'] * 100:.1f}% ({latest['year']})"
    return out


def merge(primary: ConferenceInfo | None, *others: ConferenceInfo | None) -> ConferenceInfo | None:
    """Fill ``primary``'s blanks from ``others`` in order; keywords and sources are combined."""
    infos = [i for i in (primary, *others) if i is not None]
    if not infos:
        return None
    out = infos[0]
    for other in infos[1:]:
        updates = {}
        for f in ("acronym", "name", "link", "core_rank", "ccf_rank", "deadline_timezone", "start_date", "location", "format", "limit", "archival", "size", "acceptance_rate"):
            if not getattr(out, f) and getattr(other, f):
                updates[f] = getattr(other, f)
        if not out.paper_deadline and not out.abstract_deadline and (other.paper_deadline or other.abstract_deadline):
            updates.update(abstract_deadline=other.abstract_deadline, paper_deadline=other.paper_deadline, deadline_estimated=other.deadline_estimated)
            if other.deadline_timezone:
                updates["deadline_timezone"] = other.deadline_timezone
        if out.attendance is None and other.attendance is not None:
            updates["attendance"] = other.attendance
        updates["keywords"] = tuple(dict.fromkeys(out.keywords + other.keywords))
        updates["sources"] = tuple(dict.fromkeys(out.sources + other.sources))
        out = replace(out, **updates)
    return out


@dataclass
class Catalog:
    """The feeds, indexed for :meth:`find`."""

    agent: list[_Entry] = field(default_factory=list)
    core: list[_Entry] = field(default_factory=list)
    ccfddl: list[_Entry] = field(default_factory=list)
    acceptance: dict[str, str] = field(default_factory=dict)  # ccfddl title -> acceptance rate
    errors: dict[str, str] = field(default_factory=dict)

    def find(self, *names: str, acronym: str = "") -> ConferenceInfo | None:
        """Everything the feeds say about the conference called any of ``names`` (or ``acronym``).

        conference-agent, the curated feed, comes first for deadlines, format,
        and limits; ccfddl's deadlines are used when it has none. CORE's own
        export is the source of the CORE rank, with ccfddl's copy of it as the
        fallback.
        """
        name_tokens = [tokens(n) for n in names if n]
        acronyms = [a for a in [acronym, *(acronym_of(n) for n in names)] if a]
        # A bare acronym given as the name ("CVPR") counts as one.
        acronyms += [n for n in names if n and re.fullmatch(r"[A-Za-z][A-Za-z0-9\-]{1,15}", n.strip()) and any(ch.isupper() for ch in n)]
        agent = _best(self.agent, name_tokens, acronyms, names)
        ccf = _best(self.ccfddl, name_tokens, acronyms, names)
        core = _best(self.core, name_tokens, acronyms, names)
        if core is not None and ccf is not None and core.core_rank:
            ccf = replace(ccf, core_rank="")  # CORE's own export wins
        return merge(agent, core, ccf)

    def agent_conferences(self) -> list[ConferenceInfo]:
        """Every conference-agent entry, enriched from CORE and ccfddl."""
        out = []
        for e in self.agent:
            core = _best(self.core, e.names, [e.info.acronym], [e.info.name])
            ccf = _best(self.ccfddl, e.names, [e.info.acronym], [e.info.name])
            out.append(merge(e.info, core, ccf))
        return out


def load_catalog(*, today: date | None = None, refresh: bool = False) -> Catalog:
    """Read the feeds (cached for a day). A feed that fails is skipped and named in ``errors``."""
    today = today or date.today()
    return _load_catalog(today, refresh)


@lru_cache(maxsize=4)
def _load_catalog(today: date, refresh: bool) -> Catalog:
    cat = Catalog()
    for name, url, attr, parse in (
        ("conference-agent.json", CONFERENCE_AGENT_URL, "agent", lambda raw: parse_conference_agent(raw, today)),
        ("core.csv", CORE_URL, "core", parse_core),
        ("ccfddl.json", CCFDDL_URL, "ccfddl", lambda raw: parse_ccfddl(raw, today)),
        ("ccfddl-acceptance.json", CCFDDL_ACCEPT_URL, "acceptance", parse_ccfddl_acceptance),
    ):
        label = name.rsplit(".", 1)[0]
        try:
            setattr(cat, attr, parse(_feed(name, url, refresh=refresh)))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            cat.errors[label] = str(exc)
            logger.info("conference feed %s unavailable: %s", label, exc)
    # The acceptance feed is keyed by the same titles as ccfddl's deadline feed.
    cat.ccfddl = [replace(e, info=replace(e.info, acceptance_rate=cat.acceptance[e.info.acronym])) if e.info.acronym in cat.acceptance else e for e in cat.ccfddl]
    return cat


def clear_cache() -> None:
    """Forget the parsed feeds (tests; the files on disk are kept)."""
    _load_catalog.cache_clear()
