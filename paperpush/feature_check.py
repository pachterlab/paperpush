"""Checks requiring a closer reading of manuscript and title-page files."""

from __future__ import annotations

import re
import unicodedata
import zipfile
from pathlib import Path

from .ai_validate import AiTask
from .bibliography_check import read_bibliography
from .database import Venue
from .requirements import get_requirements, resolve
from .validate import Checks, Issue, NotApplicable, Outcome, Unchecked, WARNING


CATEGORY = "Manuscript details"


def _files(venue: Venue, values: dict[str, str], role: str) -> list[Path]:
    from .requirements_check import _existing, _fields_for, _paths

    return [p for field in _fields_for(venue, role) for p in _existing(_paths(field, values))]


def _fold(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.lower()) if not unicodedata.combining(c))


def _reference_order(venue: Venue, values: dict[str, str], alphabetical: bool, citation_order: bool) -> Outcome:
    paths = _files(venue, values, "manuscript")
    if not paths:
        raise NotApplicable("no manuscript file to read")
    path = paths[0]
    bib = read_bibliography(path)
    if bib is None or len(bib.refs) < 2:
        raise Unchecked(f"could not read at least two reference entries in {path.name}")
    base = get_requirements(venue.slug)
    rules = resolve(base, values).references if base else None
    style = (rules.style or "").lower() if rules else ""
    specified_alpha = bool(rules and ("alphabet" in style or re.search(r"\b(?:APA|Harvard)\b", rules.style or "", re.IGNORECASE)))
    specified_citation = bool(re.search(r"order of (first )?citation|order cited|citation order", style))
    venue_other_order = bool(style and not specified_alpha and not specified_citation) or bool(rules and rules.numbered is True)
    if specified_alpha or specified_citation:
        alphabetical, citation_order = specified_alpha, specified_citation
    elif venue_other_order:
        alphabetical, citation_order = False, False
    from . import document
    from .bibliography_check import _split

    doc = document.get(path)
    parts = _split(doc.text or "")
    if parts is None:
        raise Unchecked(f"could not isolate the reference list in {path.name}")
    body = _fold(parts[0])
    if bib.mode == "numbered":
        if venue_other_order and not (rules and rules.numbered is True):
            raise NotApplicable("the venue specifies another reference ordering")
        from .bibliography_check import _BRACKET, _first_surname, _numbers

        issues: list[Issue] = []
        if alphabetical:
            surnames = [_fold(_first_surname(ref.authors)) for ref in bib.refs]
            if not all(surnames) or surnames != sorted(surnames):
                issues.append(Issue(WARNING, "", f"{path.name}: numbered references are not alphabetized by first author's surname"))
        first_seen: list[str] = []
        for match in _BRACKET.finditer(body):
            for number in _numbers(match.group(1)):
                key = str(number)
                if key not in first_seen:
                    first_seen.append(key)
        if len(first_seen) < 2:
            if alphabetical and not citation_order:
                return Outcome(issues, "numbered citation order could not be measured; alphabetical order checked")
            raise Unchecked(f"could not read at least two numbered citations in {path.name} to determine first-citation order")
        listed = [ref.key for ref in bib.refs if ref.key in first_seen]
        if (citation_order or not alphabetical) and first_seen != listed:
            issues.append(Issue(WARNING, "", f"{path.name}: numbered references are not listed in order of first citation"))
        return Outcome(issues, "numbered reference order checked")
    if bib.mode != "author-year":
        raise Unchecked(f"{path.name} uses {bib.mode} references; ordering could not be assessed reliably")

    names = [_fold(ref.key.rsplit(" ", 1)[0]) for ref in bib.refs]
    alpha_ok = names == sorted(names)
    positions: list[int] = []
    for ref in bib.refs:
        surname = _fold(ref.key.rsplit(" ", 1)[0])
        year = ref.year[:4]
        match = re.search(rf"\b{re.escape(surname)}\b[^\n]{{0,100}}?\b{re.escape(year)}\b", body)
        if match:
            positions.append(match.start())
    cite_ok = len(positions) >= 2 and positions == sorted(positions)
    if not positions or len(positions) < 2:
        if not alpha_ok:
            raise Unchecked("reference list is not alphabetical, but citation order could not be measured reliably")
        cite_ok = False

    issues: list[Issue] = []
    if alphabetical and not alpha_ok:
        issues.append(Issue(WARNING, "", f"{path.name}: author-year references are not alphabetized"))
    if citation_order and not cite_ok:
        issues.append(Issue(WARNING, "", f"{path.name}: author-year references are not ordered by first citation"))
    if not (alphabetical or citation_order or venue_other_order) and not (alpha_ok or cite_ok):
        issues.append(Issue(WARNING, "", f"{path.name}: author-year references are neither alphabetical nor ordered by first citation"))
    return Outcome(issues, f"alphabetical: {'yes' if alpha_ok else 'no'}; first-citation order: {'yes' if cite_ok else 'no'}")


def _word_file_cleanliness(venue: Venue, values: dict[str, str]) -> Outcome:
    paths = _files(venue, values, "manuscript") + _files(venue, values, "title_page")
    word_files = [p for p in paths if p.suffix.lower() == ".docx"]
    if not word_files:
        raise NotApplicable("no Word (.docx) manuscript or title page")
    issues: list[Issue] = []
    for path in word_files:
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                xml = "\n".join(archive.read(name).decode("utf-8", "replace") for name in names if name.startswith("word/") and name.endswith(".xml"))
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            raise Unchecked(f"could not inspect {path.name}: {exc}") from exc
        if re.search(r"<w:(?:ins|del|moveFrom|moveTo|delText|cellIns|cellDel)\b", xml):
            issues.append(Issue(WARNING, "", f"{path.name} contains tracked changes; accept or reject them before submission if the venue requires a clean file"))
        if re.search(r"(?:ADDIN\s+EN\.CITE|ADDIN\s+ENDNOTE|EndNote\.Cite|\{[^{}]{1,100}\s+#\d+\})", xml, re.IGNORECASE):
            issues.append(Issue(WARNING, "", f"{path.name} contains live or temporary EndNote citations; check whether the venue requires plain-text citations"))
    return Outcome(issues, f"inspected {len(word_files)} Word file(s) for tracked changes and EndNote fields")


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    checks.run("details.reference_order", "Reference list is alphabetical or follows first citation", CATEGORY, _reference_order, venue, values, bool(checks.options.get("references_alphabetical")), bool(checks.options.get("references_citation_order")))
    checks.run("details.word_cleanliness", "Word files have no tracked changes or EndNote fields", CATEGORY, _word_file_cleanliness, venue, values)

    manuscripts = _files(venue, values, "manuscript")
    if not manuscripts:
        return
    title_pages = _files(venue, values, "title_page")
    files = [str(p) for p in manuscripts + title_pages]
    base = get_requirements(venue.slug)
    reqs = resolve(base, values) if base else None
    guideline = "Follow the named venue's requirements and the selected article type when they are supplied. Otherwise, judge whether the item is present and flag only a clear omission or inconsistency."
    tasks = [
        ("ethics", "Ethics approval and consent", "Check whether research involving humans or animals needs ethics approval and informed consent. If applicable, verify that the manuscript states approval or exemption, names the review board, and explains informed consent in the ethics section or Methods. Check a dedicated section's title and position against the venue rules. Do not infer approval from a heading alone."),
        ("declarations", "Required declarations (conflicts, data, contributions, funding) present and in the right place", "Check conflict of interest, data sharing or availability, author contributions, and funding statements. For each required item, verify the statement's substance, section title, and location against venue rules. A statement may correctly say none or not applicable. Identify each missing or misplaced item."),
        ("author_details", "Title page lists authors, affiliations, corresponding author, and contact emails", "Extract and report the title as printed on the title page. Check that all authors, affiliations, corresponding author designation, and contact email addresses are present where required. State whether each email domain is institutional for its stated affiliation; flag a public address as noncompliant only when the venue requires an institutional one. Check word count, keywords, and running head where required."),
        ("abstract_reporting", "Abstract has the required form and subheadings (plus PRISMA/CONSORT items for reviews and trials)", "Check that the manuscript has an abstract and that its structured or unstructured form and exact subheadings meet venue rules. If it is a systematic review or clinical trial, check the abstract against applicable PRISMA or CONSORT abstract reporting items, naming missing items; do not apply either checklist to unrelated article types."),
        ("supplementary", "Every mention of supplementary material has a matching uploaded item", "Check whether the manuscript refers to any uploaded supplementary material or appendix, and whether every such mention has a corresponding item. Treat a manuscript with no supplement and no appendix mention as not applicable."),
        ("trademarks", "Trademarked product names and ™/® symbols flagged", "Identify explicit trademark symbols and named commercial products likely to be registered marks. Report the name and location; do not assume every product name is a registered trademark, and return unclear if registration cannot be verified from the supplied files."),
    ]
    for suffix, title, instructions in tasks:
        checks.needs_ai(AiTask(id=f"ai.details.{suffix}", title=title, category=CATEGORY, instructions=f"{instructions} {guideline}", files=files))
