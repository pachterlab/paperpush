"""Check a submission's files against the venue's manuscript requirements.

:mod:`paperpush.requirements` loads what a venue's author guidelines demand of
the manuscript (``manuscript_requirements.json``); this module measures the
files a filled ``.sub`` names against those rules and reports
:class:`~paperpush.validate.Issue` objects, so ``paperpush validate`` shows one
combined list.

What is checked, and how:

* **Formats and sizes** of the manuscript, figures, tables, supplementary
  files, and cover letter -- but only for a field that does not already
  declare its own ``accept`` / ``max_file_size_mb`` in ``submission_forms.json``. The
  portal's own upload rules are the stricter, enforced ones and are reported
  by the generic field checks; the guideline rules fill in where the portal
  is silent.
* **Length** -- word and page limits on the manuscript, likewise only when the
  ``submission_forms.json`` field does not carry them. A LaTeX manuscript is compiled to
  a scratch PDF for its page count (see :func:`paperpush.manuscript.build_pdf`).
* **Abstract, title, keywords** -- read from the ``.sub`` values, again only
  when the field declares no limit of its own.
* **Sections and statements** -- the manuscript text is scanned for the
  headings that count as each required section/declaration (the ``$aliases``
  table); a missing one is a WARNING, since heading detection in a PDF is a
  heuristic.
* **Title page** -- the front matter is scanned for the items that leave a
  textual trace: the title, a corresponding e-mail, an ORCID, a keywords line,
  a running title, a word count.
* **Figures** -- format, size, and count against the cap. Resolution,
  lettering, panel labels, colour mode, print size, and placement are in
  :mod:`paperpush.figure_check`.
* **References** -- the reference list's length against the cap; numbered or
  author-year citations, numbered ones in order of first citation; DOIs on
  every entry; no preprints or unpublished work where the guidelines bar them.
* **Section order**, undefined **abbreviations** in the abstract, a separate
  **title page**, and the **cover letter**'s length.
* **Template** -- when the venue records its compiled template's geometry
  (``manuscript.template_layout``), the manuscript PDF's page size, margins,
  body font size, line spacing, running head, and review line numbers are
  measured against it; attached LaTeX source is read for the edits that change
  them (see :mod:`paperpush.template_check`).

Measured numeric violations (words, pages, sizes, counts) are ERRORs; anything
that relies on text extraction or image heuristics is a WARNING.

:func:`run_checks` records each of these as its own check in a
:class:`~paperpush.validate.Checks` collector, so the report shows what passed
as well as what failed; :func:`not_implemented` lists the venue's rules that no
check reads.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from pathlib import Path

from . import ai_policy
from .database import Field, Venue
from .manuscript import headings as manuscript_headings
from .manuscript import normalize_heading, pages_before_references, reference_count, section_word_counts, structured_abstract_labels, title_page_text, total_pages, total_words, words_before_references, words_excluding
from .report import NOT_APPLICABLE, NOT_IMPLEMENTED
from .requirements import ManuscriptRequirements, get_requirements, heading_aliases, resolve
from .validate import BYTES_PER_MB, ERROR, WARNING, Checks, Issue, NotApplicable, Outcome, Unchecked, unchecked

logger = logging.getLogger(__name__)


# Field ids that, absent an explicit mapping, play each role in a venue's form.
# Matched in order; the first id present in the venue wins for single-file
# roles, and every present id contributes for multi-file roles.
_ROLE_FIELD_IDS: dict[str, tuple[str, ...]] = {
    "manuscript": ("manuscript_file", "pdf_file", "combined_pdf", "manuscript"),
    "figures": ("figure_files", "figures"),
    "tables": ("table_files", "tables"),
    "supplementary": ("supplementary_files", "supplementary_file", "supplementary_material", "supplementary_tables", "supplement", "technical_supplement"),
    "cover_letter": ("cover_letter",),
    "title_page": ("title_page_file", "title_page"),
    "title": ("title",),
    "abstract": ("abstract",),
    "keywords": ("keywords",),
    "running_title": ("running_title", "short_title"),
}

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ORCID = re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b|\\orcid")
_KEYWORDS_LINE = re.compile(r"^\s*(?:key\s*words|keywords|index terms)\b|\\keywords|\\begin\s*\{\s*keywords?\s*\}|\\kwd\b", re.IGNORECASE | re.MULTILINE)
_RUNNING_TITLE = re.compile(r"\b(?:running|short)\s+(?:title|head(?:er|ing)?)\b|\\runningtitle|\\titlerunning|\\shorttitle|\\runninghead", re.IGNORECASE)
_WORD_COUNT = re.compile(r"\bword\s+count\b|\bwords\s*:\s*\d", re.IGNORECASE)
_AUTHOR_CONTRIB = re.compile(r"author\s+contributions?|credit", re.IGNORECASE)
_FUNDING = re.compile(r"\bfunding\b|\bgrant\b|\bsupported by\b", re.IGNORECASE)
_COMPETING = re.compile(r"competing\s+interests?|conflicts?\s+of\s+interest|declaration\s+of\s+interests?", re.IGNORECASE)
_ABSTRACT = re.compile(r"\babstract\b|\\begin\s*\{\s*abstract\s*\}", re.IGNORECASE)
_CORRESPONDING = re.compile(r"correspond|to whom correspondence|lead contact|\\corref|\\thanks", re.IGNORECASE)
_PHONE = re.compile(r"\b(?:tel(?:ephone)?|phone|ph)\b\.?\s*:?\s*\+?[\d(]|\+\d{1,3}[\s.-]?\(?\d[\d\s().-]{6,}\d", re.IGNORECASE)
_DISPLAY_COUNT = {
    "figure_count": re.compile(r"\b(?:number\s+of\s+)?figures?\s*[:=]\s*\d|\b\d+\s+(?:main\s+)?figures?\b", re.IGNORECASE),
    "table_count": re.compile(r"\b(?:number\s+of\s+)?tables?\s*[:=]\s*\d|\b\d+\s+tables?\b", re.IGNORECASE),
}
# Positive evidence only: a hit settles the item; no hit hands it to the AI checks.
_POSTAL = re.compile(r"\b\d{5}(?:-\d{4})?\b|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b|\b\d+\s+[A-Z][a-z]+\s+(?:Street|St\.|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Drive|Dr\.|Lane|Way)\b|\bP\.?\s?O\.?\s+Box\b")
_SUMMARY = re.compile(r"one[- ]sentence\s+summary|\bteaser\b|\bsummary\s+sentence\b", re.IGNORECASE)
_CLASSIFICATION = re.compile(r"\bclassification\b|\bsubject\s+(?:area|category)\b|\bmajor\s+subject", re.IGNORECASE)

# Title-page items that leave a trace the front matter can be searched for, and
# the pattern that finds it. Items not listed here (authors, affiliations, ...)
# cannot be told apart from ordinary text and are left to the author.
_TITLE_PAGE_PATTERNS: dict[str, re.Pattern[str]] = {
    "corresponding_email": _EMAIL,
    "orcid": _ORCID,
    "keywords": _KEYWORDS_LINE,
    "running_title": _RUNNING_TITLE,
    "word_count": _WORD_COUNT,
    "author_contributions": _AUTHOR_CONTRIB,
    "funding": _FUNDING,
    "competing_interests": _COMPETING,
    "abstract": _ABSTRACT,
    "corresponding_author": _CORRESPONDING,
    "phone": _PHONE,
    **_DISPLAY_COUNT,
}
# Items found by pattern when they can be; otherwise judged by the AI checks.
_TITLE_PAGE_AI_PATTERNS: dict[str, re.Pattern[str]] = {"postal_address": _POSTAL, "one_sentence_summary": _SUMMARY, "classification": _CLASSIFICATION}
_TITLE_PAGE_LABELS = {
    "corresponding_email": "corresponding author e-mail address",
    "orcid": "ORCID iD",
    "keywords": "keywords line",
    "running_title": "running (short) title",
    "word_count": "word count",
    "author_contributions": "author contributions statement",
    "funding": "funding statement",
    "competing_interests": "competing interests statement",
    "title": "manuscript title",
    "abstract": "abstract",
    "corresponding_author": "corresponding-author designation",
    "phone": "telephone number",
    "figure_count": "figure count",
    "table_count": "table count",
    "authors": "author names",
}
# Statement ids whose readable form is not just the id with spaces.
_STATEMENT_LABELS = {"ai_use": "AI-use", "ethics_statement": "ethics", "reproducibility": "reproducibility", "lead_contact": "lead contact", "consent_to_publish": "consent for publication"}


def upload_role_ids(role: str) -> tuple[str, ...]:
    """The ids of the upload box for ``role`` (``"figures"``, ``"tables"``, ...) across venues."""
    return _ROLE_FIELD_IDS.get(role, ())


def _fields_for(venue: Venue, role: str) -> list[Field]:
    ids = _ROLE_FIELD_IDS.get(role, ())
    by_id = {f.id: f for f in venue.fields}
    return [by_id[i] for i in ids if i in by_id]


def _paths(field: Field, values: dict[str, str]) -> list[Path]:
    raw = values.get(field.id, "")
    if not raw.strip():
        return []
    if field.type == "file":
        candidates = [raw.strip()]
    elif field.type == "filelist":
        candidates = [line.split("|", 1)[0].strip() for line in raw.splitlines() if line.strip()]
    else:
        return []
    return [Path(c).expanduser() for c in candidates if c]


def _existing(paths: list[Path]) -> list[Path]:
    return [p for p in paths if p.is_file()]


def _format_issues(field: Field, paths: list[Path], formats: list[str] | None, what: str) -> list[Issue]:
    """WARN on a file whose extension the guidelines do not list.

    Skipped when the field declares its own ``accept`` list: the portal's rule
    is already enforced by the generic file check and would only be repeated.
    """
    if not formats or field.accept:
        return []
    allowed = {f.lower() for f in formats}
    return [Issue(WARNING, field.id, f"{p.name} has extension '{p.suffix}'; the {what} guidelines accept {', '.join(formats)}") for p in paths if p.suffix.lower() not in allowed]


def _size_issues(field: Field, paths: list[Path], limit_mb: float | None, what: str) -> list[Issue]:
    """ERROR on a file over the guideline size cap, unless the field has its own."""
    if not limit_mb or field.max_file_size_mb:
        return []
    issues = []
    for p in paths:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > limit_mb * BYTES_PER_MB:
            issues.append(Issue(ERROR, field.id, f"{p.name} is {size / BYTES_PER_MB:.1f} MB, over the {limit_mb:g} MB {what} limit in the author guidelines"))
    return issues


def _count_issue(field: Field, count: int, limit: int | None, what: str) -> list[Issue]:
    """ERROR when a multi-file field carries more items than the cap, unless the field has its own."""
    if limit is None or field.max_count is not None or count <= limit:
        return []
    return [Issue(ERROR, field.id, f"{count} {what} listed; the author guidelines allow at most {limit}")]


# --- manuscript ------------------------------------------------------------


def _field_has_length_limits(field: Field) -> bool:
    return any(
        getattr(field, name) is not None
        for name in (
            "max_words",
            "max_words_before_refs",
            "max_pages",
            "max_pages_before_refs",
            "max_words_by",
            "max_words_before_refs_by",
            "max_pages_by",
            "max_pages_before_refs_by",
        )
    )


def _length_issues(field: Field, path: Path, reqs: ManuscriptRequirements) -> list[Issue]:
    """Word/page limits from the guidelines, when the field declares none.

    Mirrors ``validate._check_manuscript_length``: each limit is measured with
    :mod:`paperpush.manuscript`, an unmeasurable format yields a WARNING that the
    limit went unchecked, and an over-limit measure is an ERROR.
    """
    rules = reqs.manuscript
    if _field_has_length_limits(field):
        return []
    limits = (rules.max_words_before_refs, rules.max_pages_before_refs, rules.max_words, rules.max_pages)
    if all(limit is None for limit in limits):
        return []
    issues: list[Issue] = []
    endings = tuple(rules.main_text_end_headings or ())
    scope_before = "before references" if not endings else "in the main text"

    for limit, count, scope in (
        (rules.max_words_before_refs, lambda p: words_before_references(p, endings), scope_before),
        (rules.max_words, total_words, ""),
    ):
        if limit is None:
            continue
        words = count(path)
        where = f" {scope}" if scope else ""
        if words is None:
            issues.append(unchecked(field.id, f"could not count words in {path.name}; the {limit}-word limit in the author guidelines was not checked"))
        elif words > limit:
            issues.extend(_over_word_limit(field, path, words, limit, where, rules, endings, whole=not scope))

    is_tex = path.suffix.lower() in (".tex", ".zip")
    for limit, count, scope in (
        (rules.max_pages_before_refs, lambda p: pages_before_references(p, endings), scope_before),
        (rules.max_pages, total_pages, ""),
    ):
        if limit is None:
            continue
        pages = count(path)
        where = f" {scope}" if scope else ""
        if pages is None:
            how = "install latexmk or pdflatex so the LaTeX source can be rendered, or supply the built PDF" if is_tex else "page counts can only be verified for PDF, LaTeX source (rendered with latexmk/pdflatex), or a Word (.docx) file with a saved page count"
            issues.append(unchecked(field.id, f"could not count pages in {path.name} ({how}); the {limit}-page limit in the author guidelines was not checked"))
        elif pages > limit:
            rendered = " (rendered from the LaTeX source)" if is_tex else ""
            issues.append(Issue(ERROR, field.id, f"{field.label}: {pages} pages{where}{rendered} exceeds the {limit}-page limit in the author guidelines"))
    return issues


def _exclusion_kind(part: str) -> str | None:
    """What :func:`paperpush.manuscript.words_excluding` should look for to leave out ``part`` of a count_excludes list."""
    text = part.lower()
    for words, kind in (
        (("reference",), "@references"),
        (("supplement",), "@supplement"),
        (("caption", "legend"), "@legends"),
        (("table", "box"), "@tables"),
        (("section title", "heading"), "@titles"),
        (("title", "author", "affiliation", "keyword"), "@front"),
        (("figure",), "@figures"),
        (("abstract",), "Abstract"),
        (("method",), "Methods"),
        (("acknowledg",), "Acknowledgements"),
        (("funding",), "funding"),
    ):
        if any(word in text for word in words):
            return kind
    return None


def _over_word_limit(field: Field, path: Path, words: int, limit: int, where: str, rules, endings: tuple[str, ...], whole: bool) -> list[Issue]:
    """The issue for a word count over ``limit``, once the parts the venue excludes are left out.

    An ERROR only when every excluded part was found; otherwise the count may
    still hold text the venue does not count, so it is a WARNING.
    """
    over = Issue(ERROR, field.id, f"{field.label}: {words} words{where} exceeds the {limit}-word limit in the author guidelines")
    excludes = list(rules.count_excludes or [])
    if not excludes:
        return [over]
    wordings = {name: canonical for kind in ("statements", "sections") for canonical, names in heading_aliases(kind).items() for name in names}
    measured = words_excluding(path, wordings, {part: _exclusion_kind(part) for part in excludes}, endings, whole=whole)
    if measured is None:
        return [over]
    net, missing = measured
    if net <= limit:
        return []
    message = f"{field.label}: {net} words{where} without the parts the guidelines exclude ({', '.join(excludes)}) exceeds the {limit}-word limit in the author guidelines"
    if not missing:
        return [Issue(ERROR, field.id, message)]
    return [Issue(WARNING, field.id, f"{message}; could not locate {', '.join(missing)} in {path.name}, which the count should leave out, so check it by hand")]


# Wording that makes a statement in running text, without a heading of its own:
# "Institutional review board approval was not required", "Reporting followed
# the PRISMA-ScR checklist", "the requirement for informed consent was waived".
_ETHICS_BODY = r"(?:institutional\s+review\s+board|\bIRBs?\b|research\s+ethics\s+(?:board|committee)|ethics\s+(?:committee|board|review)|ethic(?:al|s)\s+(?:approval|clearance)|helsinki)"
_EXEMPT = r"(?:not\s+(?:required|needed|applicable)|exempt(?:ed|ion)?|waived|waiver|unnecessary)"
STATEMENT_IN_TEXT: dict[str, re.Pattern[str]] = {
    "ethics_approval": re.compile(_ETHICS_BODY, re.IGNORECASE),
    "consent_to_participate": re.compile(
        rf"\b(?:informed|written|verbal|parental|patient)\s+(?:\w+\s+){{0,2}}consent\b|\bconsent\b[^.]{{0,80}}\b(?:waived|obtained|exempt|not\s+required)|\bassent\b"
        # A review of published work or public records needs neither approval nor consent.
        rf"|{_ETHICS_BODY}[^.]{{0,80}}{_EXEMPT}|{_EXEMPT}[^.]{{0,80}}{_ETHICS_BODY}",
        re.IGNORECASE,
    ),
    "reporting_guidelines": re.compile(
        r"\b(?:PRISMA(?:-\w+)?|STROBE|CONSORT(?:-AI)?|STARD(?:-AI)?|TRIPOD(?:\+AI|-AI)?|CLAIM|ARRIVE|MOOSE|SPIRIT(?:-AI)?|CHEERS|SRQR|COREQ|SQUIRE|CARE|MI-CLEAR-LLM|DECIDE-AI|METRICS|CLEAR)\b"
        # Acronyms are matched case-sensitively ("CARE", not "care"); the wording is not.
        r"|(?i:\breport(?:ing|ed)?\b[^.]{0,80}\b(?:guidelines?|checklists?|statement|extension)\b|\b(?:guidelines?|checklists?)\b[^.]{0,40}\breporting\b)",
    ),
    # An AI-use disclosure is often a sentence in the Methods or Acknowledgements.
    "ai_use": ai_policy.IN_TEXT,
}


def _body_text(path: Path) -> str:
    """The manuscript's text before its reference list."""
    from . import document
    from .manuscript import _REFERENCE_HEADING

    text = document.get(path).text or ""
    heading = _REFERENCE_HEADING.search(text)
    return text[: heading.start()] if heading else text


def _heading_issues(field: Field, path: Path, reqs: ManuscriptRequirements) -> list[Issue]:
    """WARN on a required section or declaration whose heading is not found."""
    required_sections = list(reqs.sections.required or [])
    # rigor.trial_registration covers this one: it applies only to trials and
    # looks for the registry ID itself, which a heading does not show.
    required_statements = [s for s in (reqs.statements.required or []) if s != "clinical_trial_registration"]
    if not required_sections and not required_statements:
        return []
    found = manuscript_headings(path)
    if found is None:
        return [unchecked(field.id, f"could not read {path.name} as text; its section headings and declarations were not checked")]
    present = set(found)
    # A single combined heading ("Results and Discussion") satisfies each part.
    combined = {normalize_heading(c) for c in (reqs.sections.combined_allowed or [])}
    issues: list[Issue] = []

    def _has(canonical: str, aliases: dict[str, list[str]]) -> bool:
        wordings = set(aliases.get(canonical, [canonical.lower()]))
        if wordings & present:
            return True
        # "results and discussion" covers both "results" and "discussion".
        for heading in present & combined:
            if any(w in heading for w in wordings):
                return True
        return any(any(re.fullmatch(rf"(?:\d+\W*)?{re.escape(w)}", h) for w in wordings) for h in present)

    section_aliases = heading_aliases("sections")
    for name in required_sections:
        if not _has(name, section_aliases):
            issues.append(Issue(WARNING, field.id, f"no '{name}' section heading found in {path.name}; the author guidelines require one"))
    statement_aliases = heading_aliases("statements")
    body: str | None = None
    for name in required_statements:
        if not _has(name, statement_aliases):
            # Some journals (RSNA, AMA) put these in the first paragraph of the
            # Methods rather than under a heading of their own.
            if name in STATEMENT_IN_TEXT:
                if body is None:
                    body = _body_text(path)
                if body and STATEMENT_IN_TEXT[name].search(body):
                    continue
            label = _STATEMENT_LABELS.get(name, name.replace("_", " "))
            issues.append(Issue(WARNING, field.id, f"no {label} statement found in {path.name}; the author guidelines require one"))
    return issues


def _normalize_title(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _title_page_issues(field: Field, path: Path, reqs: ManuscriptRequirements, values: dict[str, str], venue: Venue | None = None) -> list[Issue]:
    """WARN on a title-page item that leaves no trace in the front matter."""
    items = list(reqs.title_page.required_items or [])
    if not items:
        return []
    front = title_page_text(path)
    if front is None:
        return [unchecked(field.id, f"could not read {path.name} as text; its title page was not checked")]
    if path.suffix.lower() in (".tex", ".zip"):
        from .manuscript import _tex_to_text

        # The front matter stays raw for \\orcid and the like; names and the title are matched without markup.
        front = f"{front}\n{_tex_to_text(front)}"
    issues: list[Issue] = []
    for item in items:
        if item == "title":
            title = values.get("title", "").strip()
            if title and _normalize_title(title) not in _normalize_title(front):
                issues.append(Issue(WARNING, field.id, f"the title in the .sub file was not found on the first pages of {path.name}; the title page must carry the manuscript title"))
            continue
        if item == "authors":
            if venue is None:
                continue
            missing = [name for name in _sub_author_surnames(venue, values) if _fold(name) not in _fold(front)]
            if missing:
                issues.append(Issue(WARNING, field.id, f"author(s) {', '.join(missing)} from the .sub not found on the first pages of {path.name}; the title page must list every author"))
            continue
        pattern = _TITLE_PAGE_PATTERNS.get(item)
        if pattern is None:
            continue
        if not pattern.search(front):
            issues.append(Issue(WARNING, field.id, f"no {_TITLE_PAGE_LABELS[item]} found on the first pages of {path.name}; the author guidelines require it on the title page"))
    return issues


def _fold(text: str) -> str:
    from .document import fold

    return re.sub(r"\s+", " ", fold(text))


def _sub_author_surnames(venue: Venue, values: dict[str, str]) -> list[str]:
    """The surname (last word of the name) of each author in the .sub's author list."""
    from .validate import _author_name, parse_authors

    surnames = []
    for field in venue.fields:
        if field.type != "authorlist":
            continue
        for author in parse_authors(values.get(field.id, ""), field.fields):
            name = _author_name(author)
            if name:
                surnames.append(name.split()[-1])
    return surnames


def title_page_ai_items(path: Path, reqs: ManuscriptRequirements) -> list[str]:
    """Required title-page items that text patterns cannot settle for ``path``."""
    front = title_page_text(path) or ""
    out = []
    for item in reqs.title_page.required_items or []:
        if item in _TITLE_PAGE_AI_PATTERNS:
            if not _TITLE_PAGE_AI_PATTERNS[item].search(front):
                out.append(item)
        elif item in ("author_footnotes", "affiliations"):
            out.append(item)
    return out


def _reference_issues(field: Field, path: Path, reqs: ManuscriptRequirements) -> list[Issue]:
    limit = reqs.references.max_count
    if limit is None:
        return []
    count = reference_count(path)
    if count is None:
        return [unchecked(field.id, f"could not count the references in {path.name}; the {limit}-reference limit in the author guidelines was not checked")]
    if count > limit:
        return [Issue(ERROR, field.id, f"{count} references in {path.name} exceeds the {limit}-reference limit in the author guidelines")]
    return []


# --- .sub text fields --------------------------------------------------------


def _text_field_issues(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    """Abstract, title, running title, and keyword limits read from the .sub.

    Each is applied only when the corresponding field declares no limit of its
    own (``word_count`` / ``character_count`` / ``min_count`` / ``max_count``),
    so a rule already enforced by the generic field check is not reported twice.
    """
    issues: list[Issue] = []
    for field in _fields_for(venue, "abstract"):
        raw = values.get(field.id, "").strip()
        if not raw:
            continue
        rules = reqs.abstract
        words = len(raw.split())
        if rules.max_words is not None and field.word_count is None and words > rules.max_words:
            issues.append(Issue(ERROR, field.id, f"{field.label}: {words} words exceeds the {rules.max_words}-word limit in the author guidelines"))
        if rules.min_words is not None and words < rules.min_words:
            issues.append(Issue(WARNING, field.id, f"{field.label}: {words} words is under the {rules.min_words}-word minimum in the author guidelines"))
        if rules.max_characters is not None and field.character_count is None and len(raw) > rules.max_characters:
            issues.append(Issue(ERROR, field.id, f"{field.label}: {len(raw)} characters exceeds the {rules.max_characters}-character limit in the author guidelines"))
        # abstract.structured and abstract.no_references: see front_matter_check.
    for field in _fields_for(venue, "title"):
        raw = values.get(field.id, "").strip()
        if not raw:
            continue
        rules = reqs.title_page
        if rules.title_max_characters is not None and field.character_count is None and len(raw) > rules.title_max_characters:
            issues.append(Issue(ERROR, field.id, f"{field.label}: {len(raw)} characters exceeds the {rules.title_max_characters}-character limit in the author guidelines"))
        if rules.title_max_words is not None and field.word_count is None and len(raw.split()) > rules.title_max_words:
            issues.append(Issue(ERROR, field.id, f"{field.label}: {len(raw.split())} words exceeds the {rules.title_max_words}-word limit in the author guidelines"))
    for field in _fields_for(venue, "running_title"):
        raw = values.get(field.id, "").strip()
        limit = reqs.title_page.running_title_max_characters
        if raw and limit is not None and field.character_count is None and len(raw) > limit:
            issues.append(Issue(ERROR, field.id, f"{field.label}: {len(raw)} characters exceeds the {limit}-character limit in the author guidelines"))
    for field in _fields_for(venue, "keywords"):
        raw = values.get(field.id, "").strip()
        if not raw:
            continue
        count = sum(1 for k in re.split(r"[,;\n]", raw) if k.strip())
        rules = reqs.keywords
        if rules.min is not None and field.min_count is None and count < rules.min:
            issues.append(Issue(ERROR, field.id, f"{field.label}: {count} keywords given; the author guidelines require at least {rules.min}"))
        if rules.max is not None and field.max_count is None and count > rules.max:
            issues.append(Issue(ERROR, field.id, f"{field.label}: {count} keywords given; the author guidelines allow at most {rules.max}"))
    return issues


# --- entry point -------------------------------------------------------------


def _template_issues(field: Field, path: Path, reqs: ManuscriptRequirements) -> list[Issue]:
    """Report a manuscript whose compiled layout departs from the venue's template.

    A WARNING per departure: the geometry is measured from extracted glyphs, so
    it is strong evidence of an edited template but not proof.
    """
    layout = reqs.manuscript.template_layout
    if layout is None:
        return []
    from . import manuscript, template_check

    pdf = manuscript._as_pdf(path)
    if pdf is None:
        return [unchecked(field.id, f"template: could not render {path.name} to a PDF, so its layout was not compared with the venue's template")]
    measured = template_check.measure(pdf, layout.running_head or "")
    if measured is None:
        logger.debug("could not measure the layout of %s", pdf)
        return [unchecked(field.id, f"template: could not measure the layout of {path.name} (a PDF with no extractable text, or pdfminer.six not installed), so it was not compared with the venue's template")]
    issues = [Issue(WARNING, field.id, f"template: {path.name}: {problem}") for problem in template_check.layout_problems(measured, layout, reqs.manuscript.font_size_pt)]
    if issues:
        style = f"'{reqs.manuscript.latex_class}' style" if reqs.manuscript.latex_class else "template"
        issues.append(Issue(WARNING, field.id, f"template: {path.name} does not match the venue's {style} as distributed; papers that modify it can be desk-rejected"))
    return issues


def _template_source_issues(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    """Report LaTeX source, in any upload, that edits a mandatory template's layout."""
    if not reqs.manuscript.template_required:
        return []
    from . import template_check

    issues: list[Issue] = []
    seen: set[Path] = set()
    for field in venue.fields:
        if field.type not in ("file", "filelist"):
            continue
        for path in _existing(_paths(field, values)):
            if path in seen:
                continue
            seen.add(path)
            for where, text in template_check.iter_latex_sources(path):
                for problem in template_check.source_problems(text):
                    issues.append(Issue(WARNING, field.id, f"template: {where} {problem}"))
    return issues


# --- reference list -----------------------------------------------------------

_BRACKET_CITE = re.compile(r"\[(\d{1,3}(?:\s*[,\u2013-]\s*\d{1,3})*)\]")
_AUTHOR_YEAR_CITE = re.compile(r"\(\s*(?:see\s+)?[A-Z][A-Za-z'\u2019-]+(?:\s+(?:et al\.?|and|&)\s*[A-Z]?[A-Za-z'\u2019-]*)?,?\s+(?:19|20)\d{2}[a-z]?|[A-Z][A-Za-z'\u2019-]+\s+et al\.?\s*\((?:19|20)\d{2}[a-z]?\)")
_RANGE_CONTEXT = re.compile(r"(?:range|interval|between|within|clipped|scaled|normali[sz]ed|bounded|\bto|=|\u2208)\s*$", re.IGNORECASE)
_CITING_TO = re.compile(r"\b(?:according|due|similar|compared|related|owing|refer|back|contrast)\s+to\s*$", re.IGNORECASE)
_DOI = re.compile(r"\b10\.\d{4,9}/\S+|\bdoi\s*[:.]", re.IGNORECASE)
_PREPRINT = re.compile(r"\b(?:biorxiv|medrxiv|arxiv|chemrxiv|psyarxiv|ssrn|research\s*square|preprints?\.org|preprint)\b", re.IGNORECASE)
_UNPUBLISHED = re.compile(r"\b(?:unpublished|personal\s+communication|pers\.\s*comm|in\s+preparation|manuscript\s+submitted|submitted\s+for\s+publication)\b", re.IGNORECASE)
_IN_ORDER = re.compile(r"order\s+of\s+(?:first\s+)?(?:citation|appearance|mention)|sequential|vancouver", re.IGNORECASE)


def _numbers(group: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"\s*,\s*", group):
        bounds = re.split(r"\s*[\u2013-]\s*", part)
        try:
            if len(bounds) == 2:
                low, high = int(bounds[0]), int(bounds[1])
                if 0 < high - low <= 50:
                    out.extend(range(low, high + 1))
                    continue
            out.append(int(bounds[0]))
        except ValueError:
            continue
    return out


def _citation_groups(body: str, listed: int | None) -> list[str]:
    """The bracketed number groups of ``body`` that are citations, not intervals such as "[0, 1]" or "in the range [5, 10]"."""
    groups: list[str] = []
    for match in _BRACKET_CITE.finditer(body):
        numbers = _numbers(match.group(1))
        if 0 in numbers:
            continue
        before = body[max(0, match.start() - 40) : match.start()]
        interval = len(numbers) == 2 and "," in match.group(1)
        if interval and ((listed and numbers[1] > listed) or (_RANGE_CONTEXT.search(before) and not _CITING_TO.search(before))):
            continue
        groups.append(match.group(1))
    return groups


def _reference_style_issues(field: Field, path: Path, reqs: ManuscriptRequirements) -> list[Issue]:
    """Numbered vs author-year citations, and numbered citations in order of first citation."""
    from . import document, manuscript

    rules = reqs.references
    doc = document.get(path)
    text = doc.text
    if text is None:
        return [unchecked(field.id, f"could not read {path.name} as text; its citation style was not checked")]
    body = manuscript.truncate_at_references(text)
    bracket = _citation_groups(body, reference_count(path))
    author_year = _AUTHOR_YEAR_CITE.findall(body)
    if len(bracket) < 3 and len(author_year) < 3:
        if doc.is_tex and doc.pdf is None:
            return [unchecked(field.id, f"the citation style of {path.name} can only be read from a compiled PDF; install latexmk or pdflatex, or upload the PDF")]
        return [unchecked(field.id, f"found too few bracketed or author-year citations in {path.name} to tell its citation style (superscript citations cannot be told apart from other numbers in extracted text)")]
    style = "numbered" if len(bracket) >= 2 * len(author_year) else "author-year" if len(author_year) >= 2 * len(bracket) else "mixed"
    issues: list[Issue] = []
    if rules.numbered is True and style == "author-year":
        issues.append(Issue(WARNING, field.id, f"{path.name} cites references author-year style ({len(author_year)} citations like '(Smith et al., 2020)'); the author guidelines ask for numbered citations"))
    elif rules.numbered is False and style == "numbered":
        issues.append(Issue(WARNING, field.id, f"{path.name} cites references by number ({len(bracket)} citations like '[1]'); the author guidelines ask for author-year citations"))
    elif style == "mixed":
        issues.append(Issue(WARNING, field.id, f"{path.name} mixes numbered ({len(bracket)}) and author-year ({len(author_year)}) citations"))
    if style == "numbered" and (rules.numbered or _IN_ORDER.search(rules.style or "")):
        seen: set[int] = set()
        for group in bracket:
            for number in _numbers(group):
                if number in seen:
                    continue
                missing = [n for n in range(1, number) if n not in seen]
                if missing:
                    issues.append(Issue(WARNING, field.id, f"reference [{number}] is cited before reference [{missing[0]}] in {path.name}; the author guidelines number references in order of first citation"))
                    return issues
                seen.add(number)
    return issues


def _reference_list_issues(field: Field, path: Path, reqs: ManuscriptRequirements) -> list[Issue]:
    """DOIs present, and no preprints or unpublished work where the guidelines bar them."""
    from .manuscript import reference_entries

    rules = reqs.references
    entries = reference_entries(path)
    if not entries:
        return [unchecked(field.id, f"could not find the reference list in {path.name}; DOIs, preprints, and unpublished work were not checked")]
    issues: list[Issue] = []
    if rules.doi_required:
        from .reference_search import is_web_page_entry

        # A web page has no DOI to give.
        missing = [e for e in entries if not _DOI.search(e) and not is_web_page_entry(e)]
        if missing:
            sample = "; ".join(_short(e) for e in missing[:3])
            issues.append(Issue(WARNING, field.id, f"{len(missing)} of {len(entries)} references in {path.name} have no DOI (e.g. {sample}); the author guidelines require DOIs"))
    if rules.preprints_allowed is False:
        hits = [e for e in entries if _PREPRINT.search(e)]
        if hits:
            issues.append(Issue(WARNING, field.id, f"{len(hits)} reference(s) in {path.name} cite preprints (e.g. {_short(hits[0])}); the author guidelines do not allow preprints in the reference list"))
    if rules.unpublished_allowed is False:
        hits = [e for e in entries if _UNPUBLISHED.search(e)]
        if hits:
            issues.append(Issue(WARNING, field.id, f"{len(hits)} reference(s) in {path.name} cite unpublished work (e.g. {_short(hits[0])}); the author guidelines do not allow it in the reference list"))
    return issues


def _short(text: str, limit: int = 70) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --- abstract abbreviations, section order, title page file --------------------

_ACRONYM = re.compile(r"\b(?=[A-Za-z0-9-]*[A-Z][A-Za-z0-9-]*[A-Z])[A-Za-z][A-Za-z0-9-]{1,11}\b")
# Abbreviations so standard that "no abbreviations" rules do not mean them.
_STANDARD_ABBREVIATIONS = {"DNA", "RNA", "mRNA", "ATP", "USA", "UK", "US", "EU", "UN", "AI", "II", "III", "IV"}
# The labels of a structured abstract, which are capitals but not abbreviations.
_ABSTRACT_LABELS = {"BACKGROUND", "INTRODUCTION", "CONTEXT", "RATIONALE", "IMPORTANCE", "OBJECTIVE", "OBJECTIVES", "PURPOSE", "AIM", "AIMS", "METHODS", "METHOD", "DESIGN", "SETTING", "SETTINGS", "PARTICIPANTS", "PATIENTS", "INTERVENTION", "INTERVENTIONS", "RESULTS", "FINDINGS", "CONCLUSION", "CONCLUSIONS", "INTERPRETATION", "RELEVANCE", "SIGNIFICANCE", "LIMITATIONS", "FUNDING", "REGISTRATION", "SUMMARY"}
# A run of several capitalized words ending in a colon: "MAIN OUTCOMES AND MEASURES:".
_ABSTRACT_LABEL_RUN = re.compile(r"\b[A-Z][A-Z-]+(?:\s+[A-Z][A-Z-]+)+(?=\s*:)")


def _is_initialism(token: str) -> bool:
    """Whether some hyphen-separated part of ``token`` is an initialism, unlike the capitals of "PubMed" or "McDonald"."""
    return any(sum(c.isupper() for c in part) >= 2 and not re.search(r"[A-Z][a-z]{2,}", part) for part in token.split("-"))


def _undefined_abbreviations(text: str, labels: tuple[str, ...] = ()) -> list[str]:
    found: list[str] = []
    skip = _ABSTRACT_LABELS | {word.upper() for label in labels for word in label.split()} | {word for run in _ABSTRACT_LABEL_RUN.findall(text) for word in run.split()}
    for match in _ACRONYM.finditer(text):
        token = match.group(0).strip("-")
        if token in _STANDARD_ABBREVIATIONS or token in found or token in skip or not _is_initialism(token):
            continue
        if re.search(rf"\(\s*{re.escape(token)}s?\s*\)", text):
            continue  # defined as "long form (ABC)"
        found.append(token)
    return found


def _section_order_issues(field: Field, path: Path, reqs: ManuscriptRequirements) -> list[Issue]:
    order = list(reqs.sections.order or [])
    found = manuscript_headings(path)
    if found is None:
        return [unchecked(field.id, f"could not read {path.name} as text; its section order was not checked")]
    aliases = {**heading_aliases("statements"), **heading_aliases("sections")}
    # A structured abstract's labels are not the body's sections.
    sections = {wording: canonical for canonical, wordings in heading_aliases("sections").items() for wording in wordings}
    # An abstract comes before the references, so a repeat after them cannot end one.
    refs_at = next((i for i, heading in enumerate(found) if re.sub(r"^\d+\W*", "", heading) in ("references", "bibliography")), len(found))
    labels = structured_abstract_labels([sections.get(re.sub(r"^\d+\W*", "", heading)) if index < refs_at else None for index, heading in enumerate(found)])
    found = [heading for index, heading in enumerate(found) if index not in labels]
    positions: list[tuple[str, int]] = []
    for name in order:
        key = next((k for k in aliases if k.lower() == name.lower().replace(" ", "_") or k.lower() == name.lower()), None)
        wordings = set(aliases.get(key, [])) | {name.lower()}
        index = next((i for i, heading in enumerate(found) if heading in wordings or any(re.fullmatch(rf"(?:\d+\W*)?{re.escape(w)}", heading) for w in wordings)), None)
        if index is not None:
            positions.append((name, index))
    if len(positions) < 2:
        return [unchecked(field.id, f"found fewer than two of the guideline sections ({', '.join(order)}) as headings in {path.name}; the order was not checked")]
    for (first, i), (second, j) in zip(positions, positions[1:]):
        if j < i:
            return [Issue(WARNING, field.id, f"'{second}' comes before '{first}' in {path.name}; the author guidelines order the sections {' > '.join(order)}")]
    return Outcome([], detail=f"found in order: {', '.join(name for name, _ in positions)}")


def _title_page_file_issues(venue: Venue, values: dict[str, str]) -> list[Issue]:
    fields = [f for f in venue.fields if "title_page" in f.id and f.type in ("file", "filelist")]
    if not fields:
        raise NotApplicable("the form has no separate title-page upload")
    issues = []
    for field in fields:
        if not values.get(field.id, "").strip():
            issues.append(Issue(WARNING, field.id, f"{field.label} is empty; the author guidelines ask for the title page as a separate file"))
    return issues


# --- entry point -----------------------------------------------------------------

# Every rule key (``section.key``) some check in validate reads, deterministically
# or through an AI task. A rule the venue sets that is in neither this set nor
# INFORMATIONAL_RULES is reported as not implemented.
HANDLED_RULES = frozenset(
    {
        "manuscript.formats", "manuscript.max_file_size_mb", "manuscript.max_words", "manuscript.max_words_before_refs",
        "manuscript.max_pages", "manuscript.max_pages_before_refs", "manuscript.main_text_end_headings", "manuscript.main_text_start_headings",
        "manuscript.max_display_items", "manuscript.line_numbers", "manuscript.double_spacing", "manuscript.page_numbers",
        "manuscript.font", "manuscript.font_size_pt", "manuscript.margins_mm", "manuscript.page_size",
        "manuscript.single_file", "manuscript.figures_placement", "manuscript.tables_placement", "manuscript.anonymized",
        "manuscript.template_required", "manuscript.latex_class", "manuscript.template_layout", "manuscript.language",
        "manuscript.thousands_separator", "manuscript.group_four_digit_numbers", "manuscript.et_al_period",
        "title_page.required_items", "title_page.separate_file", "title_page.title_max_characters",
        "title_page.title_max_words", "title_page.running_title_max_characters",
        "abstract.max_words", "abstract.min_words", "abstract.max_characters", "abstract.structured",
        "abstract.structured_headings", "abstract.no_references", "abstract.no_abbreviations",
        "keywords.min", "keywords.max",
        "sections.required", "sections.order", "sections.combined_allowed", "sections.max_words",
        "statements.required", "statements.ai_use_if_used", "statements.ai_use_locations", "statements.ai_use_cover_letter",
        "statements.ai_copyediting_exempt", "statements.ai_authorship_prohibited",
        "figures.formats", "figures.min_dpi", "figures.min_dpi_line_art",
        "figures.max_file_size_mb", "figures.max_count", "figures.single_column_width_mm", "figures.double_column_width_mm",
        "figures.max_width_mm", "figures.max_height_mm", "figures.color_mode", "figures.min_font_size_pt",
        "figures.max_font_size_pt", "figures.font", "figures.legend_max_words", "figures.legends_location",
        "figures.multipanel_labels", "figures.placement", "figures.print_grayscale",
        "tables.formats", "tables.editable", "tables.max_count", "tables.placement",
        "supplementary.formats", "supplementary.max_file_size_mb", "supplementary.max_count", "supplementary.combined_single_pdf", "supplementary.max_pages",
        "references.style", "references.numbered", "references.max_count", "references.include_titles",
        "references.doi_required", "references.unpublished_allowed", "references.preprints_allowed",
        "cover_letter.required", "cover_letter.formats", "cover_letter.max_words",
        "upload.max_total_mb", "upload.max_file_mb",
    }
)  # fmt: skip
# Rule keys that describe rather than constrain (what a count leaves out, a
# template's download link, a preference), or free text handed to the AI
# checks (``notes``).
INFORMATIONAL_RULES = frozenset({"manuscript.count_excludes", "manuscript.initial_submission_any_format", "manuscript.count_includes", "manuscript.template_url", "figures.vector_formats", "figures.color_charge", "sections.optional", "statements.optional", "revision.marked_manuscript", "notes"})

GUIDELINES = "Guidelines"


def _manuscript_paths(venue: Venue, values: dict[str, str]) -> list[tuple[Field, Path]]:
    return [(field, path) for field in _fields_for(venue, "manuscript") for path in _existing(_paths(field, values))]


def manuscript_files(venue: Venue, values: dict[str, str]) -> list[tuple[Field, Path]]:
    """The ``(field, path)`` of each manuscript file the .sub names that exists."""
    return _manuscript_paths(venue, values)


def _need(condition, reason: str) -> None:
    if not condition:
        raise NotApplicable(reason)


def _per_manuscript(venue: Venue, values: dict[str, str], fn, *args) -> list[Issue]:
    files = _manuscript_paths(venue, values)
    _need(files, "no manuscript file to read")
    issues: list[Issue] = []
    for field, path in files:
        out = fn(field, path, *args)
        if isinstance(out, Outcome):
            if len(files) == 1 and not out.issues:
                return out
            out = out.issues
        issues.extend(out)
    return issues


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    """Record the guideline checks from ``manuscript_requirements.json`` in ``checks``."""
    base = get_requirements(venue.slug)
    if base is None:
        logger.debug("No manuscript requirements recorded for %s", venue.slug)
        checks.record("guidelines", "Author-guideline checks ran (needs a venue with recorded guidelines)", GUIDELINES, NOT_APPLICABLE, detail="no venue given (name one with --venue to check its author guidelines)" if venue.is_general else f"no author-guideline rules are recorded for {venue.slug}")
        return
    reqs = resolve(base, values)
    logger.info("Checking %s against its manuscript requirements", venue.slug)
    run = lambda check_id, title, fn, *args: checks.run(check_id, title, GUIDELINES, fn, *args)  # noqa: E731

    run("guidelines.formats", "Files are in formats the guidelines accept", _formats_check, venue, reqs, values)
    run("guidelines.sizes", "Files within the guidelines' size limits", _sizes_check, venue, reqs, values)
    run("guidelines.length", "Manuscript within the guidelines' word/page limits", _length_check, venue, reqs, values)
    run("guidelines.text_fields", "Title, abstract, and keywords within the guidelines' limits", _text_fields_check, venue, reqs, values)
    run("guidelines.abstract_abbreviations", "Abstract defines its abbreviations", _abbreviations_check, venue, reqs, values)
    run("guidelines.sections", "Required section headings present", _sections_check, venue, reqs, values)
    run("guidelines.section_order", "Sections in the guidelines' order", _section_order_check, venue, reqs, values)
    run("guidelines.section_length", "Sections within the guidelines' word limits", _section_length_check, venue, reqs, values)
    run("guidelines.statements", "Required statements and declarations present", _statements_check, venue, reqs, values)
    run("guidelines.ai_policy", "Use of generative AI disclosed where the guidelines ask, and no AI tool listed as an author", ai_policy.check, venue, reqs, values)
    run("guidelines.title_page", "Title page carries the required items", _title_page_check, checks, venue, reqs, values)
    run("guidelines.title_page_file", "Title page uploaded as a separate file", _title_page_file_check, venue, reqs, values)
    run("guidelines.reference_count", "Reference count within the limit", _reference_count_check, venue, reqs, values)
    run("guidelines.reference_style", "Citation style matches the guidelines (numbered or author-year)", _reference_style_check, venue, reqs, values)
    run("guidelines.reference_list", "Reference entries follow the guidelines' rules on DOIs, preprints, and unpublished work", _reference_list_check, venue, reqs, values)
    run("guidelines.counts", "Number of figures, tables, and supplementary files within the guidelines' limits", _counts_check, venue, reqs, values)
    run("guidelines.table_placement", "Tables placed where the guidelines ask", _table_placement_check, venue, reqs, values)
    run("guidelines.supplementary", "Supplementary material bundled and within length as the guidelines ask", _supplementary_check, venue, reqs, values)
    run("guidelines.cover_letter", "Cover letter provided and within length", _cover_letter_check, venue, reqs, values)
    run("guidelines.template", "Page layout matches a compiled copy of the venue's template", _template_check, venue, reqs, values)


def _formats_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    rules = [("manuscript", reqs.manuscript.formats, "manuscript"), ("figures", reqs.figures.formats, "figure"), ("tables", reqs.tables.formats, "table"), ("supplementary", reqs.supplementary.formats, "supplementary-file"), ("cover_letter", reqs.cover_letter.formats, "cover-letter")]
    issues: list[Issue] = []
    applied = False
    for role, formats, what in rules:
        if not formats:
            continue
        for field in _fields_for(venue, role):
            paths = _existing(_paths(field, values))
            if paths and not field.accept:
                applied = True
            issues.extend(_format_issues(field, paths, formats, what))
    _need(applied, "no upload whose format the guidelines (rather than the portal) govern")
    return issues


def _sizes_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    rules = [("manuscript", reqs.manuscript.max_file_size_mb, "manuscript file"), ("figures", reqs.figures.max_file_size_mb, "per-figure"), ("supplementary", reqs.supplementary.max_file_size_mb, "per-supplementary-file")]
    issues: list[Issue] = []
    applied = False
    for role, limit, what in rules:
        if not limit:
            continue
        for field in _fields_for(venue, role):
            paths = _existing(_paths(field, values))
            applied = applied or bool(paths and not field.max_file_size_mb)
            issues.extend(_size_issues(field, paths, limit, what))
    if reqs.upload.max_file_mb:
        for field in venue.fields:
            if field.type not in ("file", "filelist") or field.max_file_size_mb:
                continue
            paths = _existing(_paths(field, values))
            applied = applied or bool(paths)
            issues.extend(_size_issues(field, paths, reqs.upload.max_file_mb, "per-file upload"))
    if reqs.upload.max_total_mb and not venue.max_upload_mb:
        applied = True
        total = 0
        for field in venue.fields:
            for path in _existing(_paths(field, values)):
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        if total > reqs.upload.max_total_mb * BYTES_PER_MB:
            issues.append(Issue(ERROR, "", f"total upload size is {total / BYTES_PER_MB:.1f} MB, over the {reqs.upload.max_total_mb:g} MB limit in the author guidelines"))
    _need(applied, "the guidelines set no size limit the portal does not already enforce")
    return issues


def _length_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    rules = reqs.manuscript
    _need(any(v is not None for v in (rules.max_words_before_refs, rules.max_pages_before_refs, rules.max_words, rules.max_pages)), "the guidelines set no word or page limit")
    files = [(f, p) for f, p in _manuscript_paths(venue, values) if not _field_has_length_limits(f)]
    _need(files, "no manuscript file, or the portal's own limits apply (see the Files checks)")
    return [i for field, path in files for i in _length_issues(field, path, reqs)]


def _text_fields_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    a, t, k = reqs.abstract, reqs.title_page, reqs.keywords
    _need(any(v is not None and v is not False for v in (a.max_words, a.min_words, a.max_characters, t.title_max_characters, t.title_max_words, t.running_title_max_characters, k.min, k.max)), "the guidelines set no title, abstract, or keyword limit")
    return _text_field_issues(venue, reqs, values)


def _abbreviations_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    _need(reqs.abstract.no_abbreviations, "the guidelines allow abbreviations in the abstract")
    issues: list[Issue] = []
    fields = [f for f in _fields_for(venue, "abstract") if values.get(f.id, "").strip()]
    _need(fields, "no abstract in the .sub")
    for field in fields:
        found = _undefined_abbreviations(values[field.id], tuple(reqs.abstract.structured_headings or ()))
        if found:
            issues.append(Issue(WARNING, field.id, f"{field.label}: undefined abbreviation(s) {', '.join(found[:8])}; the author guidelines ask for no (or only defined) abbreviations in the abstract"))
    return issues


def _sections_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    _need(reqs.sections.required, "the guidelines require no particular sections")
    return _per_manuscript(venue, values, _section_part, reqs)


def _section_part(field: Field, path: Path, reqs: ManuscriptRequirements) -> list[Issue]:
    return _heading_issues(field, path, replace(reqs, statements=replace(reqs.statements, required=None)))


def _statements_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    _need(reqs.statements.required, "the guidelines require no particular statements")
    return _per_manuscript(venue, values, lambda f, p: _heading_issues(f, p, replace(reqs, sections=replace(reqs.sections, required=None))))


def _section_order_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]):
    _need(reqs.sections.order, "the guidelines prescribe no section order")
    return _per_manuscript(venue, values, _section_order_issues, reqs)


def _section_length_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]):
    _need(reqs.sections.max_words, "the guidelines set no per-section word limit")
    return _per_manuscript(venue, values, _section_length_issues, reqs)


def _section_length_issues(field: Field, path: Path, reqs: ManuscriptRequirements):
    """WARN on a section over its word limit; sections are found by their headings."""
    limits = dict(reqs.sections.max_words or {})
    wordings: dict[str, str] = {}
    for kind in ("statements", "sections"):
        for canonical, names in heading_aliases(kind).items():
            for name in names:
                wordings.setdefault(name, canonical)
    # The limited sections' own names win over an alias another section shares
    # ("Results and Discussion" is listed under both Results and Discussion).
    for canonical in limits:
        wordings[canonical.lower()] = canonical
        for name in heading_aliases("sections").get(canonical, []):
            if name not in wordings or wordings[name] not in limits:
                wordings[name] = canonical
    for name in ("abstract", "summary", "acknowledgments", "acknowledgements", "conclusion", "conclusions", "references"):
        wordings.setdefault(name, name.title())
    counts = section_word_counts(path, wordings)
    if counts is None:
        return [unchecked(field.id, f"could not read {path.name} as text; its section word limits were not checked")]
    issues: list[Issue] = []
    measured: list[str] = []
    for name, limit in limits.items():
        words = counts.get(name)
        if words is None:
            issues.append(unchecked(field.id, f"no '{name}' heading found in {path.name}; its {limit}-word limit was not checked"))
            continue
        measured.append(f"{name} {words}/{limit}")
        if words > limit:
            issues.append(Issue(WARNING, field.id, f"{field.label}: the {name} section has about {words} words, over the {limit}-word limit in the author guidelines"))
    return Outcome(issues, detail="; ".join(measured)) if measured else issues


def _title_page_check(checks: Checks, venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    _need(reqs.title_page.required_items, "the guidelines list no required title-page items")
    files = _manuscript_paths(venue, values)
    # A separate title-page upload is where these items live, when there is one.
    title_pages = [(f, p) for f in venue.fields if "title_page" in f.id and f.type in ("file", "filelist") for p in _existing(_paths(f, values))]
    _need(files or title_pages, "no manuscript or title-page file to read")
    issues: list[Issue] = []
    for field, path in title_pages or files[:1]:
        issues.extend(_title_page_issues(field, path, reqs, values, venue))
        _queue_title_page_ai(checks, field, path, reqs)
    return issues


def _title_page_file_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    _need(reqs.title_page.separate_file, "the guidelines do not ask for a separate title page")
    return _title_page_file_issues(venue, values)


def _reference_count_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    _need(reqs.references.max_count is not None, "the guidelines set no reference limit")
    return _per_manuscript(venue, values, _reference_issues, reqs)


def _reference_style_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    _need(reqs.references.numbered is not None or _IN_ORDER.search(reqs.references.style or ""), "the guidelines do not say whether citations are numbered")
    return _per_manuscript(venue, values, _reference_style_issues, reqs)


def _reference_list_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    r = reqs.references
    _need(r.doi_required or r.preprints_allowed is False or r.unpublished_allowed is False, "the guidelines put no DOI, preprint, or unpublished-work rule on the reference list")
    return _per_manuscript(venue, values, _reference_list_issues, reqs)


def _manuscript_tables(path: Path) -> tuple[list[tuple[int, int]], int | None]:
    """Where the main tables sit in a manuscript: ``(number, offset)`` of each, and the references' offset or None.

    Read from the captions, so a layout table on a title page is not taken for
    one. A LaTeX source compiled to a PDF is read as printed; otherwise by where
    its table environments are, and the endfloat package sends those to the end.
    """
    from . import citation_check, document
    from .manuscript import _REFERENCE_HEADING, _TEX_BIBLIOGRAPHY, _TEX_REFERENCE_SECTION

    doc = document.get(path)
    if doc.is_tex and doc.pdf is None:
        source = doc.tex_source
        if source is None:
            raise Unchecked(f"could not read the LaTeX source of {path.name}")
        found = citation_check.tex_mentions(source)
        marks = [m.start() for m in (_TEX_BIBLIOGRAPHY.search(source), _TEX_REFERENCE_SECTION.search(source)) if m]
        refs_at = -1 if re.search(r"\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^}]*\bendfloat\b", source) else min(marks, default=None)
    else:
        text = doc.text
        if text is None:
            raise Unchecked(f"could not read {path.name} as text")
        found = citation_check.mentions(text)
        heading = _REFERENCE_HEADING.search(text)
        refs_at = heading.start() if heading else None
    return [(m.number, m.pos) for m in found if m.series == "table" and m.caption], refs_at


def _table_placement_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> Outcome:
    """Whether the tables are in separate files, in the manuscript, or after the references, as the guidelines ask."""
    placement = reqs.tables.placement or reqs.manuscript.tables_placement
    _need(placement, "the guidelines say nowhere in particular for tables to go")
    files = _manuscript_paths(venue, values)
    _need(files, "no manuscript file to read")
    field, path = files[0]
    table_fields = _fields_for(venue, "tables")
    attached = [(fld, p) for fld in table_fields for p in _existing(_paths(fld, values))]
    if placement == "combined_file" and len(attached) > 1:
        return Outcome([Issue(WARNING, attached[0][0].id, f"{len(attached)} table files attached; the author guidelines ask for all tables combined into one document")])
    if placement in ("separate", "separate_files", "combined_file") and attached:
        return Outcome([], f"{len(attached)} table file(s) attached")
    tables, refs_at = _manuscript_tables(path)
    if placement in ("separate", "separate_files", "combined_file"):
        if tables and table_fields:
            ask = "as a separate file" if placement != "combined_file" else "combined into one document"
            return Outcome([Issue(WARNING, field.id, f"the tables are only in {path.name}; the author guidelines ask for each table {ask}")])
        return Outcome([], f"{len(tables)} table(s) in the manuscript; the form has no table upload" if tables else "")
    at_end = placement in ("end", "end_of_manuscript")
    if not tables:
        _need(attached, "no tables found in the manuscript or attached")
        where = "at the end of the manuscript file" if at_end else "in the manuscript file"
        return Outcome([Issue(WARNING, attached[0][0].id, f"no tables found in {path.name}; the author guidelines ask for tables {where} (attaching them separately may not be enough)")])
    detail = f"{len(tables)} table(s) in the manuscript"
    if placement == "end_or_inline":
        return Outcome([], detail)
    if refs_at is None:
        return Outcome([unchecked(field.id, f"could not find the reference list in {path.name}, so the tables' position was not checked")], detail)
    before = [number for number, pos in tables if pos < refs_at]
    if at_end and before:
        return Outcome([Issue(WARNING, field.id, f"Table {', '.join(map(str, before))} comes before the references in {path.name}; the author guidelines ask for tables at the end of the manuscript")], detail)
    if not at_end and not before:
        return Outcome([Issue(WARNING, field.id, f"every table comes after the references in {path.name}; the author guidelines ask for tables in the text")], detail)
    return Outcome([], detail)


def _counts_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    from .figure_check import figure_file_count

    _need(any(v is not None for v in (reqs.figures.max_count, reqs.tables.max_count, reqs.manuscript.max_display_items, reqs.supplementary.max_count)), "the guidelines cap no figure, table, or supplementary-file count")
    issues: list[Issue] = []
    figure_total = 0
    for field in _fields_for(venue, "figures"):
        # A Word file of figures counts each picture in it.
        listed = sum(figure_file_count(Path(p)) for p in _paths(field, values))
        figure_total += listed
        issues.extend(_count_issue(field, listed, reqs.figures.max_count, "figures"))
    table_total = 0
    for field in _fields_for(venue, "tables"):
        listed = len(_paths(field, values))
        table_total += listed
        issues.extend(_count_issue(field, listed, reqs.tables.max_count, "tables"))
    display = reqs.manuscript.max_display_items
    # Nothing attached: the figures and tables are in the manuscript, if anywhere.
    count_figures = not figure_total and (reqs.figures.max_count is not None or display is not None)
    count_tables = not table_total and (reqs.tables.max_count is not None or display is not None)
    from_text = False
    details: list[str] = []
    if count_figures or count_tables:
        captions = _manuscript_display_counts(venue, values)
        if captions is None:
            if _manuscript_paths(venue, values):
                issues.append(unchecked("", "could not read the manuscript for its figure and table captions; the guidelines' figure and table limits were not checked"))
            elif not (figure_total or table_total or reqs.supplementary.max_count is not None):
                raise NotApplicable("no figures, tables, or manuscript to count")
        else:
            from_text = True
            for wanted, count, cap, what in ((count_figures, captions[0], reqs.figures.max_count, "figures"), (count_tables, captions[1], reqs.tables.max_count, "tables")):
                if not wanted:
                    continue
                details.append(f"{count} {what} counted from the manuscript's captions")
                if cap is not None and count > cap:
                    issues.append(Issue(WARNING, "", f"about {count} {what} captioned in the manuscript; the author guidelines allow at most {cap}"))
            figure_total = captions[0] if count_figures else figure_total
            table_total = captions[1] if count_tables else table_total
    if display is not None and figure_total + table_total > display:
        issues.append(Issue(WARNING if from_text else ERROR, "", f"{'about ' if from_text else ''}{figure_total} figures and {table_total} tables {'captioned in the manuscript' if from_text else 'listed'}; the author guidelines allow at most {display} display items"))
    supp_total = sum(len(_paths(field, values)) for field in _fields_for(venue, "supplementary"))
    if reqs.supplementary.max_count is not None and supp_total > reqs.supplementary.max_count:
        issues.append(Issue(ERROR, "", f"{supp_total} supplementary files listed; the author guidelines allow at most {reqs.supplementary.max_count}"))
    return Outcome(issues, "; ".join(details))


# A figure or table caption that opens a line: "Figure 2.", "Table 1:".
_DISPLAY_CAPTION = re.compile(r"^[ \t]*(Fig(?:ure)?\.?|Table)\s+(\d+)[a-z]?\s*[.:|]", re.IGNORECASE | re.MULTILINE)


def _manuscript_display_counts(venue: Venue, values: dict[str, str]) -> tuple[int, int] | None:
    """How many main figures and tables the manuscript captions, or None when it cannot be read."""
    from . import document

    figures: set[str] = set()
    tables: set[str] = set()
    tex = [0, 0]
    read = False
    for _, path in _manuscript_paths(venue, values):
        doc = document.get(path)
        if doc.is_tex and doc.tex_source:
            read = True
            tex[0] += len(re.findall(r"\\begin\s*\{\s*figure\*?\s*\}", doc.tex_source))
            tex[1] += len(re.findall(r"\\begin\s*\{\s*table\*?\s*\}", doc.tex_source))
            continue
        text = doc.text
        if not text or not text.strip():
            continue
        read = True
        for kind, number in _DISPLAY_CAPTION.findall(text):
            (tables if kind.lower() == "table" else figures).add(number)
    return (len(figures) + tex[0], len(tables) + tex[1]) if read else None


_DOCUMENT_SUFFIXES = (".doc", ".docx", ".rtf", ".odt", ".tex", ".md", ".txt")


def _supplementary_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    rules = reqs.supplementary
    _need(rules.combined_single_pdf or rules.max_pages is not None, "the guidelines neither ask for one combined supplementary PDF nor limit its pages")
    issues: list[Issue] = []
    applied = False
    for field in _fields_for(venue, "supplementary"):
        paths = _existing(_paths(field, values))
        if not paths:
            continue
        applied = True
        if rules.max_pages is not None:
            for path in paths:
                pages = total_pages(path)
                if pages is None:
                    issues.append(unchecked(field.id, f"could not count pages in {path.name} (a Word file needs a saved page count; export a PDF to check it); the {rules.max_pages}-page supplementary limit in the author guidelines was not checked"))
                elif pages > rules.max_pages:
                    issues.append(Issue(ERROR, field.id, f"{path.name}: {pages} pages exceeds the {rules.max_pages}-page supplementary limit in the author guidelines"))
        if not rules.combined_single_pdf:
            continue
        non_pdf = [p for p in paths if p.suffix.lower() != ".pdf"]
        if field.type == "file" and non_pdf:
            issues.append(Issue(WARNING, field.id, f"{paths[0].name}: the author guidelines ask for supplementary text, figures, and tables combined into a single PDF"))
        elif field.type == "filelist" and len([p for p in paths if p.suffix.lower() == ".pdf"]) > 1:
            issues.append(Issue(WARNING, field.id, f"{len(paths)} supplementary PDFs listed; the author guidelines ask for supplementary text, figures, and tables combined into a single PDF"))
        elif field.type == "filelist" and any(p.suffix.lower() in _DOCUMENT_SUFFIXES for p in paths):
            names = ", ".join(p.name for p in paths if p.suffix.lower() in _DOCUMENT_SUFFIXES)
            issues.append(Issue(WARNING, field.id, f"{names}: the author guidelines ask for supplementary text, figures, and tables combined into a single PDF"))
    _need(applied, "no supplementary files listed")
    return issues


def _cover_letter_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    rules = reqs.cover_letter
    _need(rules.required or rules.max_words, "the guidelines ask for no cover letter")
    fields = _fields_for(venue, "cover_letter")
    _need(fields, "the form has no cover-letter upload")
    issues: list[Issue] = []
    for field in fields:
        if rules.required and not field.required and not values.get(field.id, "").strip():
            issues.append(Issue(WARNING, field.id, f"{field.label} is empty; the author guidelines ask for a cover letter"))
        if rules.max_words:
            from . import document

            for path in _existing(_paths(field, values)):
                text = document.get(path).text
                if text is None or not text.strip():
                    issues.append(unchecked(field.id, f"could not read {path.name} as text; the {rules.max_words}-word cover-letter limit was not checked"))
                elif len(text.split()) > rules.max_words:
                    issues.append(Issue(WARNING, field.id, f"{path.name} is about {len(text.split())} words; the author guidelines ask for at most {rules.max_words}"))
    return issues


def _template_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    _need(reqs.manuscript.template_layout is not None or reqs.manuscript.template_required, "the venue has no mandatory template")
    issues: list[Issue] = []
    if reqs.manuscript.template_layout is not None:
        files = _manuscript_paths(venue, values)
        _need(files, "no manuscript file to measure")
        for field, path in files:
            issues.extend(_template_issues(field, path, reqs))
    issues.extend(_template_source_issues(venue, reqs, values))
    if reqs.manuscript.template_layout is None and not any(p.suffix.lower() in (".tex", ".zip", ".tar", ".gz", ".tgz") for f in venue.fields if f.type in ("file", "filelist") for p in _existing(_paths(f, values))):
        raise NotApplicable("no LaTeX source to read for edits to the template (the template itself is checked under Layout)")
    return issues


def _queue_title_page_ai(checks: Checks, field: Field, path: Path, reqs: ManuscriptRequirements) -> None:
    from .ai_validate import AiTask

    items = title_page_ai_items(path, reqs)
    if not items:
        return
    labels = ", ".join(_AI_TITLE_PAGE_ITEMS[i] for i in items)
    checks.needs_ai(
        AiTask(
            id="ai.title_page_items",
            title="Title-page items that need reading (address, footnotes, summary, classification)",
            category=GUIDELINES,
            instructions=f"Read the title page / first pages of {path.name}. The author guidelines require these items on the title page: {labels}. Report 'fail' naming each item that is missing, 'pass' if all are present.",
            files=[str(path)],
        )
    )


_AI_TITLE_PAGE_ITEMS = {
    "postal_address": "the corresponding author's full postal address",
    "author_footnotes": "author footnotes (equal contribution, present addresses, deceased)",
    "one_sentence_summary": "a one-sentence summary",
    "classification": "the subject classification",
    "affiliations": "every author's affiliation",
}


def not_implemented(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    """Record each guideline rule the venue sets that no check reads."""
    base = get_requirements(venue.slug)
    if base is None:
        return
    from .requirements import SECTION_TYPES

    reqs = resolve(base, values).to_dict()
    for section in SECTION_TYPES:
        for key, value in (reqs.get(section) or {}).items():
            rule = f"{section}.{key}"
            if rule in HANDLED_RULES or rule in INFORMATIONAL_RULES or key == "notes":
                continue
            shown = str(value) if len(str(value)) <= 80 else str(value)[:77] + "..."
            checks.record(f"not_implemented.{rule}", f"Guideline rule {rule} = {shown}", GUIDELINES, NOT_IMPLEMENTED, detail="paperpush has no check for this rule yet; check it by hand")


def check_manuscript_requirements(venue: Venue, values: dict[str, str]) -> list[Issue]:
    """Every issue the venue's manuscript requirements raise for ``values``.

    Returns an empty list when the venue has no entry in
    ``manuscript_requirements.json``. Files that do not exist are skipped here
    (their absence is reported by the generic file checks).
    """
    from . import figure_check, layout_check

    checks = Checks()
    run_checks(checks, venue, values)
    layout_check.run_checks(checks, venue, values)
    figure_check.run_checks(checks, venue, values)
    return [i for r in checks.results for i in r.issues]
