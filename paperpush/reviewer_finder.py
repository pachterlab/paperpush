"""Suggest peer reviewers for a manuscript from OpenAlex (``paperpush find-reviewers``).

Many venues ask authors to suggest reviewers. This module finds researchers
who recently published work like the manuscript's and screens out the usual
conflicts of interest:

1. The works most similar to the manuscript, from the last :data:`YEARS`
   years, are looked up in OpenAlex (:func:`paperpush.openalex.similar_works`).
   Each of their authors scores the work's similarity times a recency factor;
   first and last authors count fully and middle authors less, and a paper with
   a long author list counts less for each of them.
2. The manuscript's own authors are looked up in OpenAlex (by ORCID iD when
   given, else by name, preferring a record at their stated institution).
   Excluded are the authors themselves, anyone who co-authored a work with
   them in the last :data:`COAUTHOR_YEARS` years, anyone at one of their
   institutions, and every name the author asks to exclude (``--exclude``, or
   the opposed and excluded reviewers in the ``.sub``).
3. The best-scoring candidates left (an h-index under 20 lowers the score a
   little, down to half at 0, to favour established researchers) are returned with their last known
   institution, ORCID iD, OpenAlex id, and the similar works that put them on
   the list -- at most one per group: a candidate at the same institution as a
   better-ranked one, and on one of their similar works, is left out.

OpenAlex holds no e-mail addresses, so the author has to add them. Matching is
by OpenAlex author records, whose names and affiliations are not always clean:
the list is a starting point for the author's own judgement, not a
conflict-of-interest guarantee.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import openalex

logger = logging.getLogger(__name__)

# How far back similar works count, and how far back a co-authorship is a conflict.
YEARS = 6
COAUTHOR_YEARS = 4
# Most manuscript authors looked up (each name lookup is one OpenAlex request).
MAX_AUTHOR_LOOKUPS = 20
# A paper with more authors than this counts proportionally less for each.
_CROWDED = 15
# How many candidates beyond ``limit`` get their details fetched, to replace
# those a last-known institution rules out.
_SPARE = 3
# The h-index at which a candidate's experience counts fully in the ranking.
_ESTABLISHED_H = 20


@dataclass(frozen=True)
class ManuscriptAuthor:
    """One of the manuscript's authors, as the .sub or the command line gives them."""

    name: str
    orcid: str = ""
    affiliation: str = ""


@dataclass
class Candidate:
    """A suggested reviewer."""

    openalex_id: str
    name: str
    orcid: str = ""
    institution: str = ""
    institution_ids: set[str] = field(default_factory=set)
    institution_names: set[str] = field(default_factory=set)
    score: float = 0.0
    works: list[tuple[float, str, int | None, str]] = field(default_factory=list)  # (weight, title, year, doi)
    works_count: int | None = None
    h_index: int | None = None

    def papers(self, n: int = 3) -> list[tuple[str, int | None, str]]:
        """The ``n`` similar works that weigh most for this candidate: (title, year, doi)."""
        seen: set[str] = set()
        out = []
        for _, title, year, doi in sorted(self.works, key=lambda w: -w[0]):
            if title not in seen:
                seen.add(title)
                out.append((title, year, doi))
        return out[:n]


@dataclass(frozen=True)
class Excluded:
    name: str
    reason: str
    openalex_id: str = ""


@dataclass
class ReviewerSearch:
    """The result of :func:`find_reviewers`."""

    candidates: list[Candidate]
    excluded: list[Excluded]
    query: str
    works: int
    since_year: int
    authors_found: list[str] = field(default_factory=list)  # manuscript authors matched in OpenAlex
    authors_missing: list[str] = field(default_factory=list)  # ... and those not found
    notes: list[str] = field(default_factory=list)
    top_score: float = 1.0

    def to_dict(self) -> dict:
        return {
            "source": "OpenAlex",
            "search": self.query,
            "similar_works": self.works,
            "since_year": self.since_year,
            "reviewers": [candidate_dict(c, self.top_score) for c in self.candidates],
            "excluded": [{"name": e.name, "reason": e.reason, "openalex_id": e.openalex_id or None} for e in self.excluded],
            "manuscript_authors_found": list(self.authors_found),
            "manuscript_authors_not_found": list(self.authors_missing),
            "notes": list(self.notes),
        }


def candidate_dict(c: Candidate, top: float = 1.0) -> dict:
    return {
        "name": c.name,
        "email": "",
        "institution": c.institution,
        "orcid": c.orcid,
        "openalex_id": c.openalex_id,
        "score": max(1, round(100 * c.score / (top or 1.0))),
        "works_count": c.works_count,
        "h_index": c.h_index,
        "similar_works": [{"title": t, "year": y, "doi": d} for t, y, d in c.papers()],
    }


EMAIL_NOTE = "OpenAlex has no e-mail addresses: look each reviewer's up (institution page, a recent paper's corresponding address) before submitting."


def name_key(name: str) -> tuple[str, str]:
    """(surname, first initial) of a personal name, accent- and case-folded."""
    words = openalex.fold(name).split()
    if not words:
        return ("", "")
    return (words[-1], words[0][0] if len(words) > 1 else "")


def same_name(a: str, b: str) -> bool:
    ka, kb = name_key(a), name_key(b)
    return bool(ka[0]) and ka[0] == kb[0] and (not ka[1] or not kb[1] or ka[1] == kb[1])


def _institutions(record: dict) -> list[tuple[str, str]]:
    return [(openalex.short_id(i.get("id") or ""), i.get("display_name") or "") for i in (record.get("last_known_institutions") or []) if isinstance(i, dict)]


def _in_affiliation(institution: str, affiliations: Iterable[str]) -> bool:
    """Whether an institution name appears in one of the authors' affiliation strings."""
    key = openalex.fold(institution)
    if len(key.split()) < 2:
        return False
    return any(re.search(rf"\b{re.escape(key)}\b", openalex.fold(a)) for a in affiliations if a)


def _recency(year: int | None, this_year: int) -> float:
    if not year:
        return 0.5
    age = this_year - int(year)
    return 1.0 if age <= 2 else 0.8 if age <= 4 else 0.6


def _resolve_authors(authors: list[ManuscriptAuthor]) -> tuple[dict[str, str], dict[str, tuple[str, str]], list[str], list[str]]:
    """Look the manuscript's authors up in OpenAlex.

    Returns ``(ids, institutions, found, missing)``: OpenAlex author id -> the
    manuscript author's name; institution id -> (institution name, author
    name); and the names matched and not matched.
    """
    ids: dict[str, str] = {}
    insts: dict[str, tuple[str, str]] = {}
    found: list[str] = []
    missing: list[str] = []
    orcid_records: dict[str, list[dict]] = {}
    orcids = [openalex.bare_orcid(a.orcid) for a in authors if openalex.bare_orcid(a.orcid)]
    if orcids:
        for record in openalex.authors_by_orcid(orcids):
            orcid_records.setdefault(openalex.bare_orcid(record.get("orcid") or ""), []).append(record)
    for i, author in enumerate(authors):
        records = orcid_records.get(openalex.bare_orcid(author.orcid), [])
        if not records and i < MAX_AUTHOR_LOOKUPS and len(author.name.split()) >= 2:
            named = [r for r in openalex.author_search(author.name) if same_name(r.get("display_name") or "", author.name)]
            at_home = [r for r in named if author.affiliation and any(_in_affiliation(n, [author.affiliation]) for _, n in _institutions(r))]
            # Name-only matches can be namesakes: keep the most prolific two.
            records = at_home or sorted(named, key=lambda r: -(r.get("works_count") or 0))[:2]
        if not records:
            missing.append(author.name)
            continue
        found.append(author.name)
        for record in records:
            ids[openalex.short_id(record.get("id") or "")] = author.name
            for iid, iname in _institutions(record):
                if iid:
                    insts.setdefault(iid, (iname, author.name))
    return ids, insts, found, missing


def _coauthors(author_ids: dict[str, str], since_year: int) -> dict[str, tuple[str, int | None]]:
    """OpenAlex id -> (manuscript author, year) for everyone who co-authored with them since ``since_year``."""
    out: dict[str, tuple[str, int | None]] = {}
    for work in openalex.works_by_authors(author_ids, since_year=since_year):
        ids = [openalex.short_id(((a or {}).get("author") or {}).get("id") or "") for a in work.get("authorships") or []]
        ours = [author_ids[i] for i in ids if i in author_ids]
        if not ours:
            continue
        year = work.get("publication_year")
        for aid in ids:
            if aid and aid not in author_ids:
                prev = out.get(aid)
                if prev is None or (year or 0) > (prev[1] or 0):
                    out[aid] = (ours[0], year)
    return out


def find_reviewers(
    title: str = "",
    abstract: str = "",
    keywords: Iterable[str] = (),
    *,
    authors: Iterable[ManuscriptAuthor] = (),
    exclude: Iterable[str] = (),
    limit: int = 10,
    years: int = YEARS,
    coauthor_years: int = COAUTHOR_YEARS,
    this_year: int | None = None,
) -> ReviewerSearch:
    """Suggest up to ``limit`` reviewers for a manuscript (see the module docstring).

    Raises :class:`paperpush.openalex.OpenAlexError` when OpenAlex does not
    answer and :class:`ValueError` when there is nothing to search on.
    """
    this_year = this_year or date.today().year
    since = this_year - years
    authors = [a for a in authors if a.name.strip()]
    exclude = [e for e in exclude if e.strip()]
    search, works = openalex.similar_works(title, abstract, keywords, since_year=since)
    notes = [EMAIL_NOTE]

    author_ids, author_insts, found, missing = _resolve_authors(authors) if authors else ({}, {}, [], [])
    if not authors:
        notes.append("No author list was given (pass a .sub or --author), so co-authors and colleagues were not screened out.")
    coauthors = _coauthors(author_ids, this_year - coauthor_years) if author_ids else {}
    affiliations = [a.affiliation for a in authors if a.affiliation]

    candidates: dict[str, Candidate] = {}
    for work in works:
        authorships = [a for a in work.get("authorships") or [] if isinstance(a, dict)]
        if not authorships:
            continue
        year = work.get("publication_year")
        weight = float(work.get("similarity") or 0) * _recency(year, this_year) * min(1.0, _CROWDED / len(authorships))
        doi = (work.get("doi") or "").replace("https://doi.org/", "")
        for authorship in authorships:
            person = authorship.get("author") or {}
            aid = openalex.short_id(person.get("id") or "")
            name = person.get("display_name") or ""
            if not aid or not name:
                continue
            w = weight * (1.0 if authorship.get("author_position") in ("first", "last") else 0.6)
            cand = candidates.get(aid)
            if cand is None:
                cand = candidates[aid] = Candidate(aid, name, orcid=openalex.bare_orcid(person.get("orcid") or ""))
            cand.score += w
            cand.works.append((w, work.get("display_name") or "", year, doi))
            for inst in authorship.get("institutions") or []:
                if isinstance(inst, dict):
                    if inst.get("id"):
                        cand.institution_ids.add(openalex.short_id(inst["id"]))
                    if inst.get("display_name"):
                        cand.institution_names.add(inst["display_name"])
                        cand.institution = cand.institution or inst["display_name"]

    def conflict(c: Candidate) -> str:
        if c.openalex_id in author_ids:
            return "a manuscript author"
        for a in authors:
            if same_name(c.name, a.name):
                return f"same name as manuscript author {a.name}"
        for e in exclude:
            if same_name(c.name, e):
                return f"excluded ({e})"
        if c.openalex_id in coauthors:
            who, year = coauthors[c.openalex_id]
            return f"co-authored with {who}" + (f" ({year})" if year else "")
        for iid in c.institution_ids & set(author_insts):
            name, who = author_insts[iid]
            return f"shares an institution with {who} ({name})"
        for iname in c.institution_names:
            if _in_affiliation(iname, affiliations):
                return f"shares an institution with the authors ({iname})"
        return ""

    excluded: list[Excluded] = []
    ranked = sorted(candidates.values(), key=lambda c: (-c.score, c.name))
    kept: list[Candidate] = []
    for c in ranked:
        reason = conflict(c)
        if reason:
            excluded.append(Excluded(c.name, reason, c.openalex_id))
            continue
        kept.append(c)
        if len(kept) >= limit * _SPARE:
            break

    # The current institution can differ from the one on the matching papers.
    details = openalex.authors(c.openalex_id for c in kept) if kept else {}
    final: list[Candidate] = []
    for c in kept:
        record = details.get(c.openalex_id) or {}
        current = _institutions(record)
        if current:
            c.institution = current[0][1] or c.institution
            c.institution_ids |= {i for i, _ in current if i}
            c.institution_names |= {n for _, n in current if n}
        c.orcid = c.orcid or openalex.bare_orcid(record.get("orcid") or "")
        c.name = record.get("display_name") or c.name
        c.works_count = record.get("works_count")
        c.h_index = (record.get("summary_stats") or {}).get("h_index")
        reason = conflict(c)
        if reason:
            excluded.append(Excluded(c.name, reason, c.openalex_id))
            continue
        # Prefer established researchers: an h-index of 20 or more counts fully, 0 counts half.
        if c.h_index is not None:
            c.score *= 0.5 + 0.5 * min(1.0, math.log1p(c.h_index) / math.log1p(_ESTABLISHED_H))
        final.append(c)
    # One reviewer per group: a candidate at the same institution as a better
    # one, and on one of their similar works, is that person's colleague.
    chosen: list[Candidate] = []
    for c in sorted(final, key=lambda c: (-c.score, c.name)):
        titles = {t for _, t, _, _ in c.works}
        peer = next((o for o in chosen if (o.institution_ids & c.institution_ids) and titles & {t for _, t, _, _ in o.works}), None)
        if peer is not None:
            excluded.append(Excluded(c.name, f"same group as {peer.name}, who is suggested", c.openalex_id))
            continue
        chosen.append(c)
        if len(chosen) >= limit:
            break
    final = chosen
    if missing:
        notes.append(f"Not found in OpenAlex, so their co-authors were not screened out: {', '.join(missing)}.")
    top = final[0].score if final else 1.0
    return ReviewerSearch(final, excluded, search, len(works), since, found, missing, notes, top)


# ---------------------------------------------------------------------------
# Writing the suggestions into a .sub
# ---------------------------------------------------------------------------

REVIEWER_FIELD_IDS = ("suggested_reviewers", "reviewers")


def reviewer_field(venue):
    """The venue's suggested-reviewer field, or None."""
    by_id = {f.id: f for f in venue.fields}
    return next((by_id[i] for i in REVIEWER_FIELD_IDS if i in by_id), None)


def _stance(field_help: str) -> str:
    return "recommended" if "'recommended'" in (field_help or "") else "suggested"


def reviewer_line(c: Candidate, columns: list[str], field_help: str = "") -> str:
    """One ``.sub`` line for ``c`` in the field's column order (``name | email | ...``)."""
    first, _, last = c.name.rpartition(" ")
    papers = c.papers(1)
    reason = f"Published similar work: {papers[0][0]}" + (f" ({papers[0][1]})" if papers and papers[0][1] else "") if papers else ""
    values = {
        "name": c.name,
        "first_name": first or c.name,
        "last_name": last if first else "",
        "email": "",
        "institution": c.institution,
        "affiliation": c.institution,
        "department": "",
        "stance": _stance(field_help),
        "reason": reason.replace("|", "/"),
        "orcid": c.orcid,
    }
    columns = list(columns or ["name", "email?", "institution?", "stance?", "reason?"])
    # Empty optional columns at the end are left off.
    while columns and columns[-1].endswith("?") and not values.get(columns[-1].rstrip("?")):
        columns.pop()
    return " | ".join(values.get(col.rstrip("?"), "") for col in columns)


def opposed_names(venue, values: dict[str, str]) -> list[str]:
    """Names the .sub already excludes: opposed reviewers and the excluded-reviewers field."""
    from .validate import parse_authors

    names: list[str] = []
    for f in venue.fields:
        if f.id not in REVIEWER_FIELD_IDS + ("excluded_reviewers", "opposed_reviewers"):
            continue
        for row in parse_authors(values.get(f.id, ""), f.fields or ["name", "email?", "institution?", "stance?", "reason?"]):
            name = row.get("name") or " ".join(p for p in (row.get("first_name", ""), row.get("last_name", "")) if p)
            if name and (f.id in ("excluded_reviewers", "opposed_reviewers") or str(row.get("stance", "")).strip().lower() == "opposed"):
                names.append(name.strip())
    return names


def subfile_authors(venue, values: dict[str, str]) -> list[ManuscriptAuthor]:
    """The manuscript's authors from the .sub's author-list fields."""
    from .validate import parse_authors

    out: list[ManuscriptAuthor] = []
    for f in venue.fields:
        if f.type != "authorlist" or "author" not in f.id.lower():
            continue
        for row in parse_authors(values.get(f.id, ""), f.fields):
            name = (row.get("name") or " ".join(p for p in (row.get("first_name", ""), row.get("last_name", "")) if p)).strip()
            if name:
                out.append(ManuscriptAuthor(name, str(row.get("orcid", "") or ""), str(row.get("affiliation", "") or row.get("institution", "") or "")))
    return out


def write_to_subfile(text: str, venue, candidates: list[Candidate]) -> tuple[str, int]:
    """``text`` (a .sub's contents) with ``candidates`` added to its reviewer field.

    Existing lines are kept; a candidate already listed (by name) is skipped.
    Returns the new text and how many lines were added. Raises
    :class:`ValueError` when the venue's form has no reviewer field.
    """
    from . import subfile as subfile_mod
    from .validate import parse_authors

    target = reviewer_field(venue)
    if target is None:
        raise ValueError(f"{venue.slug}'s submission form has no suggested-reviewer field")
    current = subfile_mod.parse(text).values.get(target.id, "")
    columns = target.fields or ["name", "email?", "institution?", "stance?", "reason?"]
    listed = [row.get("name") or f"{row.get('first_name', '')} {row.get('last_name', '')}".strip() for row in parse_authors(current, columns)]
    new = [reviewer_line(c, columns, target.help) for c in candidates if not any(same_name(c.name, n) for n in listed if n)]
    if not new:
        return text, 0
    body = "\n".join([line for line in current.splitlines() if line.strip()] + new)
    if subfile_mod.find_block(text, target.id) is not None:
        return subfile_mod.replace_block(text, target.id, body), len(new)
    if re.search(rf"^\s*{re.escape(target.id)}\s*:", text, re.MULTILINE):
        # A one-line (empty) value: turn it into a block.
        lines = text.splitlines()
        for i, raw in enumerate(lines):
            head, sep, _ = raw.partition(":")
            if sep and head.strip() == target.id:
                lines[i : i + 1] = [f"{head}: |"] + [f"{subfile_mod.BLOCK_INDENT}{line}" for line in body.splitlines()]
                break
        return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), len(new)
    sep = "" if text.endswith("\n") else "\n"
    return text + sep + f"{target.id}: |\n" + "\n".join(f"{subfile_mod.BLOCK_INDENT}{line}" for line in body.splitlines()) + "\n", len(new)


def format_reviewers(search: ReviewerSearch) -> list[str]:
    """Display lines for a reviewer search."""
    lines: list[str] = []
    width = len(str(len(search.candidates)))
    for i, c in enumerate(search.candidates, 1):
        facts = [x for x in (c.institution, f"ORCID {c.orcid}" if c.orcid else "", f"OpenAlex {c.openalex_id}", f"h-index {c.h_index}" if c.h_index is not None else "") if x]
        lines.append(f"{i:>{width}}. {c.name} -- {'; '.join(facts)}")
        for title, year, _doi in c.papers():
            title = " ".join(title.split())
            lines.append(f"{'':>{width}}    {title[:90]}{'…' if len(title) > 90 else ''}" + (f" ({year})" if year else ""))
    return lines


# ---------------------------------------------------------------------------
# Inputs: a .sub, a manuscript, or the parts (shared by the CLI and MCP tool)
# ---------------------------------------------------------------------------


def parse_author(value: str) -> ManuscriptAuthor:
    """``"Name"`` or ``"Name | affiliation"``; an ORCID iD anywhere in it is picked out."""
    orcid = openalex.bare_orcid(value)
    text = value.replace(orcid, "") if orcid else value
    parts = [p.strip() for p in text.split("|") if p.strip()]
    return ManuscriptAuthor(parts[0] if parts else value.strip(), orcid, " | ".join(parts[1:]))


@dataclass
class Inputs:
    """What :func:`find_reviewers` searches on, gathered by :func:`gather_inputs`."""

    title: str = ""
    abstract: str = ""
    keywords: list[str] = field(default_factory=list)
    authors: list[ManuscriptAuthor] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    sub_path: Path | None = None
    venue: object | None = None
    source: str = ""  # where the manuscript was read from ("" for options only)

    def is_empty(self) -> bool:
        return not (self.title or self.abstract or self.keywords)


def gather_inputs(
    path: str | Path | None = None,
    *,
    manuscript: str | Path | None = None,
    title: str = "",
    abstract: str = "",
    keywords: Iterable[str] = (),
    authors: Iterable[str] = (),
    exclude: Iterable[str] = (),
) -> Inputs:
    """Collect the search inputs from a .sub or manuscript ``path`` and the explicit parts.

    A .sub supplies its title, abstract, keywords, authors, and the reviewers it
    already opposes or excludes, and its manuscript file when it lacks a title
    or abstract; any other file is read as the manuscript. Explicit ``title``
    and ``abstract`` win; keywords, authors, and exclusions add up. Raises
    :class:`ValueError` for a missing or unreadable file or an unknown venue.
    """
    from .database import get_venue
    from .subfile import is_subfile_name, load as load_subfile
    from .venue_select import _split_keywords, extract_manuscript

    def add_keywords(new: Iterable[str]) -> None:
        for k in _split_keywords(new):
            if k.lower() not in (x.lower() for x in got.keywords):
                got.keywords.append(k)

    got = Inputs(title=(title or "").strip(), abstract=(abstract or "").strip(), authors=[parse_author(a) for a in authors if a.strip()], exclude=[e.strip() for e in exclude if e.strip()])
    add_keywords(keywords)
    if path:
        given = Path(path).expanduser()
        if not given.is_file():
            raise ValueError(f"no such file: {given}")
        if is_subfile_name(given):
            sub = load_subfile(given)
            try:
                venue = get_venue(sub.venue)
            except KeyError as exc:
                raise ValueError(f"{given} names an unknown venue {sub.venue!r}") from exc
            values = sub.values
            got.sub_path, got.venue, got.source = given, venue, str(given)
            got.title = got.title or values.get("title", "").strip()
            got.abstract = got.abstract or values.get("abstract", "").strip()
            add_keywords(values.get("keywords", "").splitlines())
            got.authors += subfile_authors(venue, values)
            got.exclude += opposed_names(venue, values)
            if not (got.title and got.abstract) and not manuscript:
                from .requirements_check import manuscript_files

                files = manuscript_files(venue, values)
                manuscript = files[0][1] if files else None
        else:
            manuscript = manuscript or given
    if manuscript:
        mq = extract_manuscript(Path(manuscript).expanduser())
        got.title = got.title or mq.title
        got.abstract = got.abstract or mq.abstract or mq.body
        add_keywords(mq.keywords)
        got.source = got.source or mq.source
    return got
