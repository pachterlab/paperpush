"""Check the cover letter is current and addressed to this venue.

A cover letter is usually written once and carried from one submission to the
next, so the two slips editors see most often are an old date and the name of
the journal it was last sent to. This module checks both:

* **date** -- the first date in the letterhead (the text before the
  salutation) must be no more than ``cover_letter_max_age_days`` old (7 by
  default; ``paperpush validate --cover-letter-max-age``) and not in the
  future. A numeric date that reads two ways (03/11/2026) is reported only when
  both readings are stale. A LaTeX letter dated ``\\today`` always passes. A
  letter with no date is reported as not checked.
* **venue** -- the letter must not name another venue in a sentence about the
  submission ("for consideration in *Nature*", "Dear Editors of *Cell*"). The
  names come from the venue database; a sentence that reads as a citation of
  earlier work ("published in", "previously", "et al.") is skipped. When the
  letter names neither this venue nor another, whether it is addressed to the
  right one is left to the ``ai.cover_letter_venue`` task.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import date
from pathlib import Path

from . import document
from .database import Field, Venue
from .validate import WARNING, Checks, Issue, NotApplicable, Outcome, unchecked

logger = logging.getLogger(__name__)

COVER_LETTER = "Cover letter"
DEFAULT_MAX_AGE_DAYS = 7
# A letter written in a time zone ahead of the reader's may carry tomorrow's date.
_FUTURE_GRACE_DAYS = 1
# Lines of the letter read as its letterhead when there is no salutation.
_LETTERHEAD_LINES = 20


def _today() -> date:
    """Today's date (a seam for tests)."""
    return date.today()


# --- dates ----------------------------------------------------------------------

_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
_MONTHS["sept"] = 9
_MONTH = r"(?P<mon>January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>(?:19|20)\d{2})"
_DATE_PATTERNS = (
    ("mdy", re.compile(rf"\b{_MONTH}\s+{_DAY},?\s+{_YEAR}\b", re.IGNORECASE)),
    ("dmy", re.compile(rf"\b{_DAY}(?:\s+of)?\s+{_MONTH},?\s+{_YEAR}\b", re.IGNORECASE)),
    (
        "iso",
        re.compile(r"\b(?P<year>(?:19|20)\d{2})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})\b"),
    ),
    (
        "numeric",
        re.compile(r"(?<![\d.])(?P<a>\d{1,2})[/.-](?P<b>\d{1,2})[/.-](?P<year>(?:19|20)\d{2}|\d{2})(?![\d.])"),
    ),
    (
        "my",
        re.compile(r"\b(?P<mon>January|February|March|April|May|June|July|August|September|October|November|December),?\s+(?P<year>(?:19|20)\d{2})\b"),
    ),
)
_SALUTATION = re.compile(
    r"^\s*(?:Dear\b|To\s+whom\b|To\s+the\s+editors?\b|Editors?\s*,|Editor-in-Chief\s*,)",
    re.IGNORECASE | re.MULTILINE,
)
_TEX_TODAY = re.compile(r"\\today\b")


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_dates(text: str) -> tuple[str, list[date]] | None:
    """The first date written in ``text``: ``(as written, readings)``, or None.

    ``readings`` holds every calendar date the text can mean -- two for an
    ambiguous numeric date (03/11/2026), one otherwise. A date given only as a
    month and year ("November 2026") reads as both the first and the last day
    of that month, so it is stale only once the whole month is.
    """
    best: tuple[int, str, list[date]] | None = None
    for kind, pattern in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            if best is not None and m.start() >= best[0]:
                break
            year = int(m.group("year"))
            if year < 100:
                year += 2000
            if kind in ("mdy", "dmy"):
                readings = [
                    _safe_date(
                        year,
                        _MONTHS[m.group("mon").lower().rstrip(".")],
                        int(m.group("day")),
                    )
                ]
            elif kind == "iso":
                readings = [_safe_date(year, int(m.group("m")), int(m.group("d")))]
            elif kind == "numeric":
                a, b = int(m.group("a")), int(m.group("b"))
                readings = [_safe_date(year, a, b), _safe_date(year, b, a)]
            else:
                month = _MONTHS[m.group("mon").lower()]
                readings = [
                    date(year, month, 1),
                    date(year, month, calendar.monthrange(year, month)[1]),
                ]
            readings = sorted({r for r in readings if r is not None})
            if readings:
                best = (m.start(), m.group(0), readings)
                break
    return None if best is None else (best[1], best[2])


def letterhead(text: str) -> str:
    """The part of a letter before its salutation, where the date is written."""
    match = _SALUTATION.search(text)
    if match:
        return text[: match.start()]
    return "\n".join(text.splitlines()[:_LETTERHEAD_LINES])


def date_issue(
    field_id: str,
    name: str,
    written: str,
    readings: list[date],
    today: date,
    max_age_days: int,
) -> tuple[list[Issue], str]:
    """Issues for a letter dated ``readings`` (as ``written``), and a note on what was found."""
    ages = [(today - r).days for r in readings]
    if all(age > max_age_days for age in ages):
        age = min(ages)
        either = " (either way it is read)" if len(readings) > 1 else ""
        return [
            Issue(
                WARNING,
                field_id,
                f"{name} is dated {written}, {age} days ago{either}; cover letters should be dated within {max_age_days} days of submission -- update the date",
            )
        ], ""
    if all(age < -_FUTURE_GRACE_DAYS for age in ages):
        return [
            Issue(
                WARNING,
                field_id,
                f"{name} is dated {written}, {-max(ages)} days in the future; check the date",
            )
        ], ""
    if len(readings) > 1:
        return (
            [],
            f"dated {written} (read as {' or '.join(r.isoformat() for r in readings)}), within {max_age_days} days",
        )
    return [], f"dated {written}, {max(ages[0], 0)} day(s) ago"


# --- the checks -------------------------------------------------------------------


def _letters(venue: Venue, values: dict[str, str]) -> list[tuple[Field, Path]]:
    from .requirements_check import _existing, _fields_for, _paths

    fields = _fields_for(venue, "cover_letter")
    if not fields:
        raise NotApplicable("the form has no cover-letter upload")
    letters = [(f, p) for f in fields for p in _existing(_paths(f, values))]
    if not letters:
        raise NotApplicable("no cover letter attached")
    return letters


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    max_age = int(checks.options.get("cover_letter_max_age_days", DEFAULT_MAX_AGE_DAYS))
    checks.run(
        "cover_letter.date",
        f"Cover letter dated within the last {max_age} days",
        COVER_LETTER,
        _date_check,
        venue,
        values,
        max_age,
    )
    checks.run(
        "cover_letter.venue",
        "Cover letter addressed to the venue" if venue.is_general or venue.journal_unnamed else f"Cover letter addressed to {venue.name or venue.slug}",
        COVER_LETTER,
        _venue_check,
        checks,
        venue,
        values,
    )


def _date_check(venue: Venue, values: dict[str, str], max_age_days: int) -> Outcome:
    issues: list[Issue] = []
    details: list[str] = []
    for field, path in _letters(venue, values):
        doc = document.get(path)
        source = doc.tex_source if doc.is_tex else None
        if source is not None and _TEX_TODAY.search(letterhead(source)):
            details.append(f"{path.name} is dated \\today (the date it is compiled)")
            continue
        text = doc.text
        if text is None:
            issues.append(
                unchecked(
                    field.id,
                    f"could not read {path.name} as text; its date was not checked",
                )
            )
            continue
        found = parse_dates(letterhead(text))
        if found is None:
            issues.append(
                unchecked(
                    field.id,
                    f"found no date at the top of {path.name}; its age was not checked",
                )
            )
            continue
        written, readings = found
        found_issues, detail = date_issue(field.id, path.name, written, readings, _today(), max_age_days)
        issues.extend(found_issues)
        if detail:
            details.append(f"{path.name} is {detail}")
    return Outcome(issues, "; ".join(details))


# --- venue names ---------------------------------------------------------------------

# A sentence about this submission: where it is being sent, or to whom.
_SUBMISSION_CONTEXT = re.compile(
    r"\bsubmi|\bconsider|\bpublication\s+(?:in|at|by)\b|\beditors?\b|\breaders(?:hip)?\b|\baudience\b|\b(?:fit|suited|suitable|appropriate)\s+for\b|\bscope\b|\bDear\b|\bjournal\b",
    re.IGNORECASE,
)
# A sentence that cites earlier work in another venue rather than addressing it.
_CITATION_CONTEXT = re.compile(
    r"\bpublished\b|\bpreviously\b|\bearlier\b|\bprior\s+work\b|\bet\s+al\b|\breported\s+in\b|\bdescribed\s+in\b|\bshown\s+in\b|\(\s*(?:19|20)\d{2}\s*\)",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+|\n|$)")
_YEAR_SUFFIX = re.compile(r"\s+(?:19|20)\d{2}$")


def venue_names(venue: Venue) -> list[str]:
    """The ways a letter may name ``venue``: its name, with and without a year, and its full name."""
    out: list[str] = []
    for raw in (venue.name, venue.full_name.split(" - ")[0] if venue.full_name else ""):
        name = re.sub(r"\s*\([^)]*\)", "", raw or "").strip()
        for candidate in (name, _YEAR_SUFFIX.sub("", name)):
            for variant in (candidate, re.sub(r"^The\s+", "", candidate)):
                if variant and variant not in out:
                    out.append(variant)
    return out


def _name_pattern(name: str) -> re.Pattern[str]:
    # A one-word name is matched as written ("Cell", not "cell types"); a longer
    # one ignoring case ("PLOS Computational Biology" / "Plos computational biology").
    flags = re.IGNORECASE if len(name.split()) > 1 else 0
    body = r"\s+".join(re.escape(w) for w in name.split())
    return re.compile(rf"(?<![\w-]){body}(?![\w-])", flags)


def _mask(text: str, names: list[str]) -> str:
    for name in sorted(names, key=len, reverse=True):
        text = _name_pattern(name).sub(lambda m: "\u2591" * len(m.group(0)), text)
    return text


def other_venue_mentions(text: str, venue: Venue, others: list[Venue]) -> list[tuple[str, str]]:
    """``(venue name, sentence)`` for each other venue ``text`` names in a sentence about the submission."""
    own = venue_names(venue)
    own_folded = [n.lower() for n in own]
    # Longest names first, so "Nature Methods" is not also reported as "Nature".
    candidates: list[str] = []
    for other in others:
        if other.slug == venue.slug:
            continue
        for name in venue_names(other):
            # A name inside this venue's own ("Nature" in "Nature Communications") is not another venue.
            if len(name) < 3 or any(name.lower() in o for o in own_folded) or name in candidates:
                continue
            candidates.append(name)
    candidates.sort(key=len, reverse=True)
    masked = _mask(document.normalize(text), own)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for sentence in _SENTENCE.findall(masked):
        if not _SUBMISSION_CONTEXT.search(sentence) or _CITATION_CONTEXT.search(sentence):
            continue
        rest = sentence
        for name in candidates:
            pattern = _name_pattern(name)
            if pattern.search(rest):
                key = name.lower()
                if key not in seen:
                    seen.add(key)
                    found.append(
                        (
                            name,
                            re.sub(r"\s+", " ", sentence).strip().replace("\u2591", ""),
                        )
                    )
                rest = pattern.sub(lambda m: "\u2591" * len(m.group(0)), rest)
    return found


def names_venue(text: str, venue: Venue) -> bool:
    """Whether ``text`` names ``venue`` anywhere."""
    normalized = document.normalize(text)
    return any(_name_pattern(n).search(normalized) for n in venue_names(venue))


def _venue_check(checks: Checks, venue: Venue, values: dict[str, str]) -> Outcome:
    from .ai_validate import AiTask
    from .database import list_venues

    if venue.is_general:
        raise NotApplicable("no venue given, so there is no venue the letter should name")
    if venue.journal_unnamed:
        raise NotApplicable("only the publisher was given, so there is no journal the letter should name (pass --issn or --journal to name it)")
    target = venue.name or venue.slug
    others = list_venues()
    issues: list[Issue] = []
    details: list[str] = []
    for field, path in _letters(venue, values):
        text = document.get(path).text
        if text is None:
            issues.append(
                unchecked(
                    field.id,
                    f"could not read {path.name} as text; the venue it is addressed to was not checked",
                )
            )
            continue
        mentions = other_venue_mentions(text, venue, others)
        for name, sentence in mentions[:3]:
            snippet = sentence if len(sentence) <= 120 else sentence[:117] + "..."
            issues.append(
                Issue(
                    WARNING,
                    field.id,
                    f"{path.name} names {name} ('{snippet}'), not {target}; is it left over from an earlier submission?",
                )
            )
        if mentions:
            continue
        if names_venue(text, venue):
            details.append(f"{path.name} names {target}")
            continue
        issues.append(
            unchecked(
                field.id,
                f"{path.name} does not name {target}; whether it is addressed to the right venue is left to the AI check",
            )
        )
        checks.needs_ai(
            AiTask(
                id="ai.cover_letter_venue",
                title=f"Cover letter addressed to {target}",
                category=COVER_LETTER,
                instructions=(
                    f"Read the cover letter {path.name}. This submission is going to {target}"
                    + (f" ({venue.full_name})" if venue.full_name and venue.full_name != target else "")
                    + ". Report 'fail' if the letter is addressed to, or asks for consideration by, a different journal or conference, quoting the passage; "
                    "'pass' if it is addressed to this venue or names no venue at all."
                ),
                files=[str(path)],
            )
        )
    return Outcome(issues, "; ".join(details))


__all__ = [
    "DEFAULT_MAX_AGE_DAYS",
    "date_issue",
    "letterhead",
    "names_venue",
    "other_venue_mentions",
    "parse_dates",
    "run_checks",
    "venue_names",
]
