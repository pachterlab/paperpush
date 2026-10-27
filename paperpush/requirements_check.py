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

from .database import Field, Venue
from .manuscript import headings as manuscript_headings
from .manuscript import normalize_heading, pages_before_references, reference_count, title_page_text, total_pages, total_words, words_before_references
from .report import NOT_APPLICABLE, NOT_IMPLEMENTED
from .requirements import ManuscriptRequirements, get_requirements, heading_aliases, resolve
from .validate import BYTES_PER_MB, ERROR, WARNING, Checks, Issue, NotApplicable, Outcome, unchecked

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
    "title": ("title",),
    "abstract": ("abstract",),
    "keywords": ("keywords",),
    "running_title": ("running_title", "short_title"),
}

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ORCID = re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b|\\orcid")
_KEYWORDS_LINE = re.compile(r"^\s*(?:key\s*words|keywords|index terms)\b|\\keywords", re.IGNORECASE | re.MULTILINE)
_RUNNING_TITLE = re.compile(r"\b(?:running|short)\s+title\b|\\runningtitle|\\titlerunning", re.IGNORECASE)
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
            issues.append(Issue(ERROR, field.id, f"{field.label}: {words} words{where} exceeds the {limit}-word limit in the author guidelines"))

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


def _heading_issues(field: Field, path: Path, reqs: ManuscriptRequirements) -> list[Issue]:
    """WARN on a required section or declaration whose heading is not found."""
    required_sections = list(reqs.sections.required or [])
    required_statements = list(reqs.statements.required or [])
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
    for name in required_statements:
        if not _has(name, statement_aliases):
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
        if rules.structured and rules.structured_headings:
            lower = raw.lower()
            missing = [h for h in rules.structured_headings if h.lower() not in lower]
            if missing:
                issues.append(Issue(WARNING, field.id, f"{field.label}: the author guidelines ask for a structured abstract with the headings {', '.join(rules.structured_headings)}; missing: {', '.join(missing)}"))
        if rules.no_references and re.search(r"\[\d+(?:[,–-]\s*\d+)*\]|\bet al\.,?\s*\(?(?:19|20)\d{2}", raw):
            issues.append(Issue(WARNING, field.id, f"{field.label}: looks like it cites references, which the author guidelines do not allow in the abstract"))
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
        return []
    measured = template_check.measure(pdf, layout.running_head or "")
    if measured is None:
        logger.debug("could not measure the layout of %s", pdf)
        return []
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


def _reference_style_issues(field: Field, path: Path, reqs: ManuscriptRequirements) -> list[Issue]:
    """Numbered vs author-year citations, and numbered citations in order of first citation."""
    from . import document, manuscript

    rules = reqs.references
    doc = document.get(path)
    text = doc.text
    if text is None:
        return [unchecked(field.id, f"could not read {path.name} as text; its citation style was not checked")]
    body = manuscript.truncate_at_references(text)
    bracket = _BRACKET_CITE.findall(body)
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
        missing = [e for e in entries if not _DOI.search(e)]
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


def _undefined_abbreviations(text: str) -> list[str]:
    found: list[str] = []
    for match in _ACRONYM.finditer(text):
        token = match.group(0).strip("-")
        if token in _STANDARD_ABBREVIATIONS or token in found:
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
        "manuscript.max_pages", "manuscript.max_pages_before_refs", "manuscript.main_text_end_headings",
        "manuscript.max_display_items", "manuscript.line_numbers", "manuscript.double_spacing", "manuscript.page_numbers",
        "manuscript.font", "manuscript.font_size_pt", "manuscript.margins_mm", "manuscript.page_size",
        "manuscript.single_file", "manuscript.figures_placement", "manuscript.tables_placement", "manuscript.anonymized",
        "manuscript.template_required", "manuscript.latex_class", "manuscript.template_layout", "manuscript.language",
        "title_page.required_items", "title_page.separate_file", "title_page.title_max_characters",
        "title_page.title_max_words", "title_page.running_title_max_characters",
        "abstract.max_words", "abstract.min_words", "abstract.max_characters", "abstract.structured",
        "abstract.structured_headings", "abstract.no_references", "abstract.no_abbreviations",
        "keywords.min", "keywords.max",
        "sections.required", "sections.order", "sections.combined_allowed",
        "statements.required",
        "figures.formats", "figures.min_dpi", "figures.min_dpi_line_art", "figures.min_dpi_combination",
        "figures.max_file_size_mb", "figures.max_count", "figures.single_column_width_mm", "figures.double_column_width_mm",
        "figures.max_width_mm", "figures.max_height_mm", "figures.color_mode", "figures.min_font_size_pt",
        "figures.max_font_size_pt", "figures.font", "figures.legend_max_words", "figures.legends_location",
        "figures.multipanel_labels", "figures.placement",
        "tables.formats", "tables.editable", "tables.max_count", "tables.placement",
        "supplementary.formats", "supplementary.max_file_size_mb", "supplementary.max_count", "supplementary.combined_single_pdf",
        "references.style", "references.numbered", "references.max_count", "references.include_titles",
        "references.doi_required", "references.unpublished_allowed", "references.preprints_allowed",
        "cover_letter.required", "cover_letter.formats", "cover_letter.max_words",
        "upload.max_total_mb", "upload.max_file_mb",
    }
)  # fmt: skip
# Rule keys that describe rather than constrain (what a count leaves out, a
# template's download link, a preference), or free text handed to the AI
# checks (``notes``).
INFORMATIONAL_RULES = frozenset({"manuscript.count_excludes", "manuscript.count_includes", "manuscript.template_url", "figures.vector_formats", "figures.color_charge", "sections.optional", "statements.optional", "notes"})

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
        checks.record("guidelines", "Manuscript checked against the author guidelines", GUIDELINES, NOT_APPLICABLE, detail=f"no author-guideline rules are recorded for {venue.slug}")
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
    run("guidelines.statements", "Required statements and declarations present", _statements_check, venue, reqs, values)
    run("guidelines.title_page", "Title page carries the required items", _title_page_check, checks, venue, reqs, values)
    run("guidelines.title_page_file", "Title page uploaded as a separate file", _title_page_file_check, venue, reqs, values)
    run("guidelines.reference_count", "Reference count within the limit", _reference_count_check, venue, reqs, values)
    run("guidelines.reference_style", "Citation style (numbered or author-year, in order)", _reference_style_check, venue, reqs, values)
    run("guidelines.reference_list", "Reference entries (DOIs, preprints, unpublished work)", _reference_list_check, venue, reqs, values)
    run("guidelines.counts", "Number of figures, tables, and supplementary files", _counts_check, venue, reqs, values)
    run("guidelines.supplementary", "Supplementary material bundled as the guidelines ask", _supplementary_check, venue, reqs, values)
    run("guidelines.cover_letter", "Cover letter provided and within length", _cover_letter_check, venue, reqs, values)
    run("guidelines.template", "Layout matches the venue's compiled template", _template_check, venue, reqs, values)


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
    _need(any(v is not None and v is not False for v in (a.max_words, a.min_words, a.max_characters, a.structured, a.no_references, t.title_max_characters, t.title_max_words, t.running_title_max_characters, k.min, k.max)), "the guidelines set no title, abstract, or keyword limit")
    return _text_field_issues(venue, reqs, values)


def _abbreviations_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    _need(reqs.abstract.no_abbreviations, "the guidelines allow abbreviations in the abstract")
    issues: list[Issue] = []
    fields = [f for f in _fields_for(venue, "abstract") if values.get(f.id, "").strip()]
    _need(fields, "no abstract in the .sub")
    for field in fields:
        found = _undefined_abbreviations(values[field.id])
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


def _counts_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    _need(any(v is not None for v in (reqs.figures.max_count, reqs.tables.max_count, reqs.manuscript.max_display_items, reqs.supplementary.max_count)), "the guidelines cap no figure, table, or supplementary-file count")
    issues: list[Issue] = []
    figure_total = 0
    for field in _fields_for(venue, "figures"):
        listed = len(_paths(field, values))
        figure_total += listed
        issues.extend(_count_issue(field, listed, reqs.figures.max_count, "figures"))
    table_total = 0
    for field in _fields_for(venue, "tables"):
        listed = len(_paths(field, values))
        table_total += listed
        issues.extend(_count_issue(field, listed, reqs.tables.max_count, "tables"))
    if reqs.manuscript.max_display_items is not None and figure_total + table_total > reqs.manuscript.max_display_items:
        issues.append(Issue(ERROR, "", f"{figure_total} figures and {table_total} tables listed; the author guidelines allow at most {reqs.manuscript.max_display_items} display items"))
    supp_total = sum(len(_paths(field, values)) for field in _fields_for(venue, "supplementary"))
    if reqs.supplementary.max_count is not None and supp_total > reqs.supplementary.max_count:
        issues.append(Issue(ERROR, "", f"{supp_total} supplementary files listed; the author guidelines allow at most {reqs.supplementary.max_count}"))
    return issues


def _supplementary_check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    _need(reqs.supplementary.combined_single_pdf, "the guidelines do not ask for one combined supplementary PDF")
    issues: list[Issue] = []
    applied = False
    for field in _fields_for(venue, "supplementary"):
        paths = _existing(_paths(field, values))
        if not paths:
            continue
        applied = True
        non_pdf = [p for p in paths if p.suffix.lower() != ".pdf"]
        if field.type == "file" and non_pdf:
            issues.append(Issue(WARNING, field.id, f"{paths[0].name}: the author guidelines ask for supplementary text, figures, and tables combined into a single PDF"))
        elif field.type == "filelist" and len([p for p in paths if p.suffix.lower() == ".pdf"]) > 1:
            issues.append(Issue(WARNING, field.id, f"{len(paths)} supplementary PDFs listed; the author guidelines ask for supplementary text, figures, and tables combined into a single PDF"))
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
                if text is None:
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
