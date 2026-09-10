"""Check a submission's files against the venue's manuscript requirements.

:mod:`paperpush.requirements` loads what a venue's author guidelines demand of
the manuscript (``manuscript_requirements.json``); this module measures the
files a filled ``.sub`` names against those rules and reports
:class:`~paperpush.validate.Issue` objects, so ``paperpush validate`` shows one
combined list.

What is checked, and how:

* **Formats and sizes** of the manuscript, figures, tables, supplementary
  files, and cover letter -- but only for a field that does not already
  declare its own ``accept`` / ``max_file_size_mb`` in ``venues.json``. The
  portal's own upload rules are the stricter, enforced ones and are reported
  by the generic field checks; the guideline rules fill in where the portal
  is silent.
* **Length** -- word and page limits on the manuscript, likewise only when the
  ``venues.json`` field does not carry them. A LaTeX manuscript is compiled to
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
* **Figures** -- resolution (from the image's DPI metadata, or inferred from
  its pixel width at the venue's maximum print width), colour mode, and
  dimensions; count against the cap.
* **References** -- the reference list's length against the cap.

Measured numeric violations (words, pages, sizes, counts) are ERRORs; anything
that relies on text extraction or image heuristics is a WARNING.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .database import Field, Venue
from .manuscript import headings as manuscript_headings
from .manuscript import normalize_heading, pages_before_references, reference_count, title_page_text, total_pages, total_words, words_before_references
from .requirements import FigureRules, ManuscriptRequirements, get_requirements, heading_aliases, resolve
from .validate import BYTES_PER_MB, ERROR, WARNING, Issue

logger = logging.getLogger(__name__)

MM_PER_INCH = 25.4

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
}
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
            issues.append(Issue(WARNING, field.id, f"could not count words in {path.name}; the {limit}-word limit in the author guidelines was not checked"))
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
            issues.append(Issue(WARNING, field.id, f"could not count pages in {path.name} ({how}); the {limit}-page limit in the author guidelines was not checked"))
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
        return [Issue(WARNING, field.id, f"could not read {path.name} as text; its section headings and declarations were not checked")]
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


def _title_page_issues(field: Field, path: Path, reqs: ManuscriptRequirements, values: dict[str, str]) -> list[Issue]:
    """WARN on a title-page item that leaves no trace in the front matter."""
    items = list(reqs.title_page.required_items or [])
    if not items:
        return []
    front = title_page_text(path)
    if front is None:
        return [Issue(WARNING, field.id, f"could not read {path.name} as text; its title page was not checked")]
    issues: list[Issue] = []
    for item in items:
        if item == "title":
            title = values.get("title", "").strip()
            if title and _normalize_title(title) not in _normalize_title(front):
                issues.append(Issue(WARNING, field.id, f"the title in the .sub file was not found on the first pages of {path.name}; the title page must carry the manuscript title"))
            continue
        pattern = _TITLE_PAGE_PATTERNS.get(item)
        if pattern is None:
            continue
        if not pattern.search(front):
            issues.append(Issue(WARNING, field.id, f"no {_TITLE_PAGE_LABELS[item]} found on the first pages of {path.name}; the author guidelines require it on the title page"))
    return issues


def _reference_issues(field: Field, path: Path, reqs: ManuscriptRequirements) -> list[Issue]:
    limit = reqs.references.max_count
    if limit is None:
        return []
    count = reference_count(path)
    if count is None:
        return [Issue(WARNING, field.id, f"could not count the references in {path.name}; the {limit}-reference limit in the author guidelines was not checked")]
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


# --- figures -----------------------------------------------------------------

_RASTER = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp"}


def _image_info(path: Path):
    """(width_px, height_px, dpi or None, mode) for a raster image, or None."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - pillow is a hard dependency
        return None
    try:
        with Image.open(path) as img:
            dpi = img.info.get("dpi")
            xdpi = None
            if dpi:
                try:
                    xdpi = float(dpi[0])
                except (TypeError, ValueError, IndexError):
                    xdpi = None
                if xdpi is not None and xdpi <= 1:
                    xdpi = None  # PIL reports 1 for "unset"; ignore.
            return img.width, img.height, xdpi, img.mode
    except Exception as exc:  # noqa: BLE001 -- any unreadable/unsupported image
        logger.debug("Could not read image %s: %s", path, exc)
        return None


def _figure_file_issues(field: Field, path: Path, rules: FigureRules) -> list[Issue]:
    """Resolution, colour mode, and print size of one raster figure."""
    if path.suffix.lower() not in _RASTER:
        return []
    info = _image_info(path)
    if info is None:
        return []
    width_px, height_px, dpi, mode = info
    issues: list[Issue] = []
    # The width the figure will be printed at: the largest the venue allows, so
    # a figure passes if it would be sharp enough at any column width.
    print_width_mm = rules.max_width_mm or rules.double_column_width_mm or rules.single_column_width_mm
    min_dpi = rules.min_dpi
    if min_dpi is not None:
        if dpi is not None:
            # PNG stores resolution in pixels per metre, so 300 dpi round-trips
            # as 299.9994; allow that rounding.
            if dpi < min_dpi - 0.5:
                issues.append(Issue(WARNING, field.id, f"{path.name} is {dpi:.0f} dpi; the author guidelines ask for at least {min_dpi} dpi"))
        elif print_width_mm:
            # No metadata: what resolution would it have at the print width?
            effective = width_px / (print_width_mm / MM_PER_INCH)
            if effective < min_dpi:
                issues.append(Issue(WARNING, field.id, f"{path.name} is {width_px} px wide, which is {effective:.0f} dpi at the {print_width_mm:g} mm print width; the author guidelines ask for at least {min_dpi} dpi"))
        else:
            single_width_in = 3.5  # a typical single column when the venue gives no width
            if width_px / single_width_in < min_dpi:
                issues.append(Issue(WARNING, field.id, f"{path.name} is only {width_px} px wide with no resolution metadata; the author guidelines ask for at least {min_dpi} dpi"))
    if rules.color_mode:
        allowed = {m.strip().upper() for m in rules.color_mode}
        image_mode = {"L": "GRAYSCALE", "LA": "GRAYSCALE", "1": "GRAYSCALE", "P": "RGB", "RGB": "RGB", "RGBA": "RGB", "CMYK": "CMYK"}.get(mode, mode.upper())
        if image_mode not in allowed and not (image_mode == "GRAYSCALE" and {"RGB", "CMYK"} & allowed):
            issues.append(Issue(WARNING, field.id, f"{path.name} is {image_mode.lower()}; the author guidelines accept {', '.join(rules.color_mode)}"))
    if dpi is not None:
        width_mm = width_px / dpi * MM_PER_INCH
        height_mm = height_px / dpi * MM_PER_INCH
        if rules.max_width_mm and width_mm > rules.max_width_mm * 1.02:
            issues.append(Issue(WARNING, field.id, f"{path.name} is {width_mm:.0f} mm wide at {dpi:.0f} dpi; the author guidelines allow at most {rules.max_width_mm:g} mm"))
        if rules.max_height_mm and height_mm > rules.max_height_mm * 1.02:
            issues.append(Issue(WARNING, field.id, f"{path.name} is {height_mm:.0f} mm tall at {dpi:.0f} dpi; the author guidelines allow at most {rules.max_height_mm:g} mm"))
    return issues


# --- entry point -------------------------------------------------------------


def check_manuscript_requirements(venue: Venue, values: dict[str, str]) -> list[Issue]:
    """Every issue the venue's manuscript requirements raise for ``values``.

    Returns an empty list when the venue has no entry in
    ``manuscript_requirements.json``. Files that do not exist are skipped here
    (their absence is reported by the generic file checks).
    """
    base = get_requirements(venue.slug)
    if base is None:
        logger.debug("No manuscript requirements recorded for %s", venue.slug)
        return []
    reqs = resolve(base, values)
    logger.info("Checking %s against its manuscript requirements", venue.slug)
    issues: list[Issue] = []

    issues.extend(_text_field_issues(venue, reqs, values))

    manuscript_fields = _fields_for(venue, "manuscript")
    for field in manuscript_fields:
        paths = _existing(_paths(field, values))
        issues.extend(_format_issues(field, paths, reqs.manuscript.formats, "manuscript"))
        issues.extend(_size_issues(field, paths, reqs.manuscript.max_file_size_mb, "manuscript file"))
        for path in paths:
            issues.extend(_length_issues(field, path, reqs))
            issues.extend(_heading_issues(field, path, reqs))
            issues.extend(_title_page_issues(field, path, reqs, values))
            issues.extend(_reference_issues(field, path, reqs))

    figure_total = 0
    for field in _fields_for(venue, "figures"):
        paths = _existing(_paths(field, values))
        figure_total += len(_paths(field, values))
        issues.extend(_format_issues(field, paths, reqs.figures.formats, "figure"))
        issues.extend(_size_issues(field, paths, reqs.figures.max_file_size_mb, "per-figure"))
        issues.extend(_count_issue(field, len(_paths(field, values)), reqs.figures.max_count, "figures"))
        for path in paths:
            issues.extend(_figure_file_issues(field, path, reqs.figures))

    table_total = 0
    for field in _fields_for(venue, "tables"):
        paths = _existing(_paths(field, values))
        table_total += len(_paths(field, values))
        issues.extend(_format_issues(field, paths, reqs.tables.formats, "table"))
        issues.extend(_count_issue(field, len(_paths(field, values)), reqs.tables.max_count, "tables"))

    if reqs.manuscript.max_display_items is not None and figure_total + table_total > reqs.manuscript.max_display_items:
        issues.append(Issue(ERROR, "", f"{figure_total} figures and {table_total} tables listed; the author guidelines allow at most {reqs.manuscript.max_display_items} display items"))

    supp_fields = _fields_for(venue, "supplementary")
    supp_total = 0
    for field in supp_fields:
        paths = _existing(_paths(field, values))
        supp_total += len(_paths(field, values))
        issues.extend(_format_issues(field, paths, reqs.supplementary.formats, "supplementary-file"))
        issues.extend(_size_issues(field, paths, reqs.supplementary.max_file_size_mb, "per-supplementary-file"))
        if reqs.supplementary.combined_single_pdf and field.type == "file" and paths and paths[0].suffix.lower() != ".pdf":
            issues.append(Issue(WARNING, field.id, f"{paths[0].name}: the author guidelines ask for supplementary text, figures, and tables combined into a single PDF"))
    if reqs.supplementary.max_count is not None and supp_total > reqs.supplementary.max_count:
        issues.append(Issue(ERROR, "", f"{supp_total} supplementary files listed; the author guidelines allow at most {reqs.supplementary.max_count}"))

    for field in _fields_for(venue, "cover_letter"):
        paths = _existing(_paths(field, values))
        issues.extend(_format_issues(field, paths, reqs.cover_letter.formats, "cover-letter"))
        if reqs.cover_letter.required and not field.required and not values.get(field.id, "").strip():
            issues.append(Issue(WARNING, field.id, f"{field.label} is empty; the author guidelines ask for a cover letter"))

    if reqs.upload.max_total_mb and not venue.max_upload_mb:
        total = 0
        for field in venue.fields:
            for path in _existing(_paths(field, values)):
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        if total > reqs.upload.max_total_mb * BYTES_PER_MB:
            issues.append(Issue(ERROR, "", f"total upload size is {total / BYTES_PER_MB:.1f} MB, over the {reqs.upload.max_total_mb:g} MB limit in the author guidelines"))

    return issues
