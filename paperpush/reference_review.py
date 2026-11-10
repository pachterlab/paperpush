"""Reference-age and citation-density measurements, with opt-in source reviews.

Age is a prompt to inspect the literature, not a verdict on a reference.
Citation density is reported as a measurement because fields and article types
have no universal healthy citations-per-word threshold.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from . import document
from .ai_validate import AiTask
from .bibliography_check import _YEAR, _numbers, _split, _unwrap, citation_groups, read_bibliography
from .database import Venue
from .validate import WARNING, Checks, Issue, NotApplicable, Outcome, Unchecked


# Half or more of a reference list being old is worth a look; a few old sources are not.
OLD_SHARE_TO_REPORT = 0.5
_URL_OR_DOI = re.compile(r"https?://\S+|\b10\.\d{4,9}/\S+")
_PARENTHESIZED_YEAR = re.compile(r"\(\s*((?:19|20)\d{2})[a-z]?\s*\)")
# A year that is not one end of a page range ("2001-2010").
_LONE_YEAR = re.compile(r"(?<![\d\u2013-])\b((?:19|20)\d{2})[a-z]?\b(?!\s*[\u2013-]\s*\d)")


def _entry_year(text: str, current: int) -> int | None:
    """The publication year of a reference written as prose: the one in parentheses, else the last one (titles and pages come earlier)."""
    text = _URL_OR_DOI.sub(" ", text)
    for pattern in (_PARENTHESIZED_YEAR, _LONE_YEAR):
        years = [int(m.group(1)) for m in pattern.finditer(text) if int(m.group(1)) <= current]
        if years:
            return years[-1]
    return None


def _source(venue: Venue, values: dict[str, str]):
    from .requirements_check import manuscript_files

    files = manuscript_files(venue, values)
    if not files:
        raise NotApplicable("no manuscript file to read")
    field, path = files[0]
    bib = read_bibliography(path)
    if bib is None:
        raise Unchecked(f"could not find the reference list in {path.name}")
    return field, path, bib


def _printed_refs(bib):
    if bib.printed_only_cited and bib.cited is not None:
        return [r for r in bib.refs if r.key in bib.cited]
    return bib.refs


def _age(venue: Venue, values: dict[str, str]) -> Outcome:
    field, path, bib = _source(venue, values)
    refs = _printed_refs(bib)
    if not refs:
        raise NotApplicable(f"no references in {path.name}")
    current = date.today().year
    dated = []
    for ref in refs:
        match = _YEAR.search(ref.year) if ref.year else None
        year = (int(match.group(1)) if match else None) if ref.year else _entry_year(ref.authors, current)
        if year is not None and year <= current:
            dated.append((ref, current - year))
    if not dated:
        raise Unchecked(f"could not read publication years from {path.name}")
    older_10 = [(ref, years) for ref, years in dated if years > 10]
    older_15 = [(ref, years) for ref, years in dated if years > 15]
    detail = (f"{len(older_10)} of {len(dated)} dated references are over 10 years old; "
              f"{len(older_15)} are over 15 years old ({len(refs) - len(dated)} undated)")
    if len(older_10) < OLD_SHARE_TO_REPORT * len(dated):
        return Outcome([], detail)
    examples = ", ".join(f"{ref.label} ({years} years)" for ref, years in older_10[:8])
    if len(older_10) > 8:
        examples += f", and {len(older_10) - 8} more"
    return Outcome([Issue(WARNING, field.id, f"{detail}: {examples}; check whether newer work should also be cited (age alone does not make a source unsuitable)")])


def _density(venue: Venue, values: dict[str, str]) -> Outcome:
    _, path, bib = _source(venue, values)
    doc = document.get(path)
    if doc.is_tex and doc.tex_source is not None:
        from .front_matter_check import TEX_CITE

        body = re.split(r"\\(?:begin\s*\{\s*thebibliography\s*\}|bibliography\s*\{)", doc.tex_source, maxsplit=1)[0]
        count = sum(len([k for k in m.group(1).split(",") if k.strip()]) for m in TEX_CITE.finditer(body))
        words = len(document.tokens(re.sub(r"\\[A-Za-z]+(?:\[[^]]*\])?", " ", body)))
    else:
        if doc.text is None:
            raise Unchecked(f"could not extract text from {path.name}")
        parts = _split(doc.text)
        if parts is None:
            raise Unchecked(f"could not separate the reference list from {path.name}")
        body = parts[0]
        words = len(document.tokens(body))
        if bib.mode == "numbered":
            count = sum(len(_numbers(group)) for group in citation_groups(body, max(int(r.key) for r in bib.refs)))
        elif bib.mode == "labels":
            keys = {r.key for r in bib.refs}
            count = sum(sum(k.strip() in keys for k in re.split(r"[,;]", group)) for group in re.findall(r"\[([^\[\]]{2,300})\]", body))
        elif bib.mode == "author-year":
            unwrapped = _unwrap(body)
            count = sum(len(re.findall(rf"(?<![A-Za-z]){re.escape(r.key.split()[0])}(?![a-z])[^;\n]{{0,80}}?\b{re.escape(r.year)}\b", unwrapped)) for r in bib.refs if r.year)
        else:
            raise Unchecked(f"could not count citation occurrences in {path.name}")
    if not words:
        raise Unchecked(f"could not count body words in {path.name}")
    return Outcome([], f"{count} citation occurrences in {words} words ({count / words * 1000:.1f} per 1,000 words); no universal density threshold applies")


def _extended_tasks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    try:
        _, path, bib = _source(venue, values)
    except (NotApplicable, Unchecked):
        return
    refs = _printed_refs(bib)
    files = [str(path)]
    if path.suffix.lower() == ".tex":
        from .bibliography_check import _BIB_NAMES, _keys

        source = path.read_text(encoding="utf-8", errors="replace")
        for match in _BIB_NAMES.finditer(source):
            for name in _keys(match.group(1)):
                candidate = path.parent / (name if name.lower().endswith(".bib") else name + ".bib")
                if candidate.is_file():
                    files.append(str(candidate))
    for index, ref in enumerate(refs, 1):
        if bib.cited is not None and ref.key not in bib.cited:
            continue
        identity = f"{ref.label}: {ref.authors[:500]}"
        checks.needs_ai(AiTask(
            id=f"ai.reference_source.{index}", title=f"Source supports claims: {ref.label}", category="References",
            instructions=(f"Review reference {identity}. Find every claim in the manuscript that cites it, then read the source's abstract or full paper from a reliable link. "
                          "Report fail only for a specific unsupported or contradicted claim, naming its location and the source evidence. "
                          "Report pass only if you verified every claim. If the paper is inaccessible, the citation cannot be mapped, or support remains uncertain, report unclear. "
                          "Do not infer support from the title or journal alone."), files=files))
        checks.needs_ai(AiTask(
            id=f"ai.reference_primary.{index}", title=f"Original source of claims: {ref.label}", category="References",
            instructions=(f"Review reference {identity}. Find every claim in the manuscript that cites it, then read the source's full text to see where each claim originates. An abstract alone cannot settle this: without the full text report unclear. "
                          "Report fail only if the manuscript cites this source for a specific finding or number that the source presents as background and attributes to another work; name that work, quote the attributing passage, confirm that work actually says it, and phrase the finding as a suggestion to consider citing it directly (a reference limit or a later replication can make this source the better citation). "
                          "Follow the attribution one step only: do not trace the original work's own sources. "
                          "Report pass if the source produced the claim through its own data or analysis, or the claim is about its own synthesis, such as a review's conclusion or a meta-analysis estimate. "
                          "Report not_applicable if every citation is general background where citing a review is normal, or cites the source for a method, tool, or dataset it provides. "
                          "If the attribution is ambiguous, report unclear."), files=files))
        checks.needs_ai(AiTask(
            id=f"ai.reference_journal.{index}", title=f"Journal indexing: {ref.label}", category="References",
            instructions=(f"Review reference {identity}. If this is a journal article, verify the journal's identity and current indexing using an authoritative index or the index's own journal record. "
                          "Report fail if you confirm it is unindexed or misrepresented, with the index checked and the journal details; pass only if you confirm indexing. "
                          "For a book, preprint, thesis, proceedings, or other non-journal work report not_applicable. If the venue or index cannot be verified, report unclear. "
                          "An unindexed journal alone does not establish that the cited research is invalid."), files=files))


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    checks.run("bibliography.age", "Share of references more than 10 years old", "References", _age, venue, values)
    checks.run("bibliography.density", "Citation density (citations per 1,000 words), reported for information", "References", _density, venue, values)
    if checks.options.get("reference_check_extended"):
        _extended_tasks(checks, venue, values)
