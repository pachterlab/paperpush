"""Access to the manuscript requirements database.

``venues.json`` describes a venue's *submission form* -- the fields the portal
asks for. This module reads its companion, ``manuscript_requirements.json``,
which describes what the venue's author guidelines demand of the *manuscript
itself*: accepted file formats and sizes, length limits, the sections and
statements the text must contain, title-page contents, figure resolution and
width, reference style, and so on. ``paperpush validate`` measures the files a
``.sub`` names against these rules (see :mod:`paperpush.requirements_check`).

The database is a JSON object keyed by venue slug. Every key of an entry is
optional -- a venue records only what its guidelines state -- and is grouped
into sections that share the same vocabulary across venues (``manuscript``,
``title_page``, ``abstract``, ``keywords``, ``sections``, ``statements``,
``figures``, ``tables``, ``supplementary``, ``references``, ``cover_letter``,
``upload``). Two mechanisms keep the file DRY:

* ``inherits``: an entry takes another venue's requirements wholesale and then
  overrides individual keys section by section (a key set to ``null`` drops the
  inherited value). This is how the AAAS family shares Science's rules.
* ``article_types``: per-article-type overrides, keyed by the exact option
  string of the ``.sub`` field named in ``article_type_field``. The top level
  holds the primary research-article rules; :func:`resolve` applies the
  overrides for the article type a filled ``.sub`` selects.

Keys starting with ``$`` (``$schema``, ``$aliases``) are metadata, not venues.
``$aliases`` maps each canonical section/statement name to the heading
wordings that count as it when the manuscript text is searched.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, fields as dataclass_fields
from dataclasses import field as dataclass_field
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from pydantic import Field as PField
from pydantic import TypeAdapter, ValidationError

from . import venue_data

logger = logging.getLogger(__name__)

# The copy shipped with the package; the file actually read may be a newer
# published copy (see paperpush.venue_data).
REQUIREMENTS_PATH = Path(__file__).with_name("manuscript_requirements.json")

# Where figures/tables sit for the initial submission.
Placement = Literal["separate_files", "end_of_manuscript", "inline", "end_or_inline"]
# Where figures/tables sit relative to the main manuscript file, from the
# manuscript's point of view.
ManuscriptPlacement = Literal["end", "inline", "separate", "end_or_inline"]
LegendsLocation = Literal["end_of_manuscript", "with_figure", "after_references"]

# Canonical ids for the items a title page may have to carry.
TitlePageItem = Literal[
    "title",
    "authors",
    "affiliations",
    "corresponding_author",
    "corresponding_email",
    "orcid",
    "keywords",
    "running_title",
    "word_count",
    "figure_count",
    "table_count",
    "author_contributions",
    "funding",
    "competing_interests",
    "abstract",
    "postal_address",
    "phone",
    "one_sentence_summary",
    "classification",
    "author_footnotes",
]

# Canonical ids for the declarations a manuscript may have to carry.
StatementId = Literal[
    "data_availability",
    "code_availability",
    "competing_interests",
    "author_contributions",
    "funding",
    "acknowledgements",
    "ethics_approval",
    "consent_to_participate",
    "consent_to_publish",
    "materials_availability",
    "lead_contact",
    "inclusion_and_diversity",
    "ai_use",
    "reproducibility",
    "ethics_statement",
    "clinical_trial_registration",
    "reporting_guidelines",
]


def _notes() -> Any:
    return dataclass_field(default_factory=list)


@dataclass(frozen=True)
class ManuscriptRules:
    """Rules for the main manuscript file: format, size, length, and layout."""

    formats: Annotated[Optional[list[str]], PField(description="File extensions accepted for the main manuscript upload (lowercase, with dot).")] = None
    max_file_size_mb: Annotated[Optional[float], PField(description="Maximum size of the manuscript file in MB.")] = None
    max_words: Annotated[Optional[int], PField(description="Maximum word count of the whole document, references included.")] = None
    max_words_before_refs: Annotated[Optional[int], PField(description="Maximum word count of the main text (before the reference list).")] = None
    max_pages: Annotated[Optional[int], PField(description="Maximum page count of the whole document.")] = None
    max_pages_before_refs: Annotated[Optional[int], PField(description="Maximum page count of the main text (before the reference list).")] = None
    main_text_end_headings: Annotated[Optional[list[str]], PField(description="Headings other than the reference list that end the main text for the before-refs limits (e.g. Appendix).")] = None
    count_excludes: Annotated[Optional[list[str]], PField(description="Parts of the manuscript the venue leaves out of its word/page count (informational).")] = None
    count_includes: Annotated[Optional[list[str]], PField(description="Parts the venue explicitly counts that authors might not expect (informational).")] = None
    max_display_items: Annotated[Optional[int], PField(description="Maximum number of figures and tables combined.")] = None
    line_numbers: Annotated[Optional[bool], PField(description="Continuous line numbers required.")] = None
    double_spacing: Annotated[Optional[bool], PField(description="Double line spacing required.")] = None
    page_numbers: Annotated[Optional[bool], PField(description="Page numbers required.")] = None
    font: Annotated[Optional[str], PField(description="Mandated body font, if any.")] = None
    font_size_pt: Annotated[Optional[float], PField(description="Mandated body font size in points, if any.")] = None
    margins_mm: Annotated[Optional[float], PField(description="Mandated page margins in mm, if any.")] = None
    page_size: Annotated[Optional[str], PField(description="Mandated page size (A4, US Letter), if any.")] = None
    single_file: Annotated[Optional[bool], PField(description="Manuscript must be a single file holding text, figures, and tables.")] = None
    figures_placement: Annotated[Optional[ManuscriptPlacement], PField(description="Where figures go relative to the main manuscript file.")] = None
    tables_placement: Annotated[Optional[ManuscriptPlacement], PField(description="Where tables go relative to the main manuscript file.")] = None
    anonymized: Annotated[Optional[bool], PField(description="Double-blind: no author names or affiliations in the manuscript.")] = None
    template_required: Annotated[Optional[bool], PField(description="Must use the venue's style file or template.")] = None
    template_url: Annotated[Optional[str], PField(description="URL of the venue's template.")] = None
    latex_class: Annotated[Optional[str], PField(description="Required LaTeX document class or style file.")] = None
    language: Annotated[Optional[str], PField(description="Mandated language variant, if any.")] = None
    notes: Annotated[list[str], PField(description="Rules with no structured key, one per string.")] = _notes()


@dataclass(frozen=True)
class TitlePageRules:
    """What the title page must carry."""

    required_items: Annotated[Optional[list[TitlePageItem]], PField(description="Canonical ids of the items the title page must carry.")] = None
    separate_file: Annotated[Optional[bool], PField(description="Title page is uploaded as its own file (double-blind venues).")] = None
    title_max_characters: Annotated[Optional[int], PField(description="Maximum title length in characters (spaces included).")] = None
    title_max_words: Annotated[Optional[int], PField(description="Maximum title length in words.")] = None
    running_title_max_characters: Annotated[Optional[int], PField(description="Maximum running/short title length in characters.")] = None
    notes: Annotated[list[str], PField(description="Rules with no structured key.")] = _notes()


@dataclass(frozen=True)
class AbstractRules:
    """Abstract length and structure."""

    max_words: Annotated[Optional[int], PField(description="Maximum abstract length in words.")] = None
    min_words: Annotated[Optional[int], PField(description="Minimum abstract length in words.")] = None
    max_characters: Annotated[Optional[int], PField(description="Maximum abstract length in characters.")] = None
    structured: Annotated[Optional[bool], PField(description="Abstract must be structured into labelled subsections.")] = None
    structured_headings: Annotated[Optional[list[str]], PField(description="The subsection labels a structured abstract must carry.")] = None
    no_references: Annotated[Optional[bool], PField(description="Abstract must not cite references.")] = None
    no_abbreviations: Annotated[Optional[bool], PField(description="Undefined abbreviations are banned in the abstract.")] = None
    notes: Annotated[list[str], PField(description="Rules with no structured key.")] = _notes()


@dataclass(frozen=True)
class KeywordRules:
    """How many keywords the manuscript must list."""

    min: Annotated[Optional[int], PField(description="Minimum number of keywords.")] = None
    max: Annotated[Optional[int], PField(description="Maximum number of keywords.")] = None
    notes: Annotated[list[str], PField(description="Rules with no structured key.")] = _notes()


@dataclass(frozen=True)
class SectionRules:
    """Section headings the manuscript body must (or may) carry."""

    required: Annotated[Optional[list[str]], PField(description="Canonical section names the manuscript must contain (matched via $aliases).")] = None
    optional: Annotated[Optional[list[str]], PField(description="Canonical section names the venue allows but does not require.")] = None
    order: Annotated[Optional[list[str]], PField(description="Prescribed section order, if the venue states one.")] = None
    combined_allowed: Annotated[Optional[list[str]], PField(description="Combined headings the venue accepts (e.g. Results and Discussion).")] = None
    notes: Annotated[list[str], PField(description="Rules with no structured key.")] = _notes()


@dataclass(frozen=True)
class StatementRules:
    """Declarations that must appear in the manuscript text."""

    required: Annotated[Optional[list[StatementId]], PField(description="Canonical ids of the declarations the manuscript must contain (matched via $aliases).")] = None
    optional: Annotated[Optional[list[StatementId]], PField(description="Declarations the venue accepts but does not require.")] = None
    notes: Annotated[list[str], PField(description="Rules with no structured key.")] = _notes()


@dataclass(frozen=True)
class FigureRules:
    """Figure file format, resolution, dimensions, and count."""

    formats: Annotated[Optional[list[str]], PField(description="Accepted figure file extensions.")] = None
    vector_formats: Annotated[Optional[list[str]], PField(description="Formats preferred for line art / vector figures.")] = None
    min_dpi: Annotated[Optional[int], PField(description="Minimum resolution for halftone/photographic figures.")] = None
    min_dpi_line_art: Annotated[Optional[int], PField(description="Minimum resolution for line art.")] = None
    min_dpi_combination: Annotated[Optional[int], PField(description="Minimum resolution for combination (halftone + line) figures.")] = None
    max_file_size_mb: Annotated[Optional[float], PField(description="Maximum size of each figure file in MB.")] = None
    max_count: Annotated[Optional[int], PField(description="Maximum number of main-text figures.")] = None
    single_column_width_mm: Annotated[Optional[float], PField(description="Single-column figure width in mm.")] = None
    double_column_width_mm: Annotated[Optional[float], PField(description="Double-column figure width in mm.")] = None
    max_width_mm: Annotated[Optional[float], PField(description="Maximum figure width in mm.")] = None
    max_height_mm: Annotated[Optional[float], PField(description="Maximum figure height in mm.")] = None
    color_mode: Annotated[Optional[list[str]], PField(description="Accepted colour spaces (RGB, CMYK, grayscale).")] = None
    min_font_size_pt: Annotated[Optional[float], PField(description="Minimum font size for figure lettering, in points.")] = None
    max_font_size_pt: Annotated[Optional[float], PField(description="Maximum font size for figure lettering, in points.")] = None
    font: Annotated[Optional[str], PField(description="Mandated or recommended figure font.")] = None
    legend_max_words: Annotated[Optional[int], PField(description="Maximum words per figure legend.")] = None
    legends_location: Annotated[Optional[LegendsLocation], PField(description="Where figure legends go.")] = None
    multipanel_labels: Annotated[Optional[str], PField(description="Required style of panel labels.")] = None
    color_charge: Annotated[Optional[bool], PField(description="Colour figures carry a charge.")] = None
    placement: Annotated[Optional[Placement], PField(description="Where figures go for the initial submission.")] = None
    notes: Annotated[list[str], PField(description="Rules with no structured key.")] = _notes()


@dataclass(frozen=True)
class TableRules:
    """Table file format, editability, count, and placement."""

    formats: Annotated[Optional[list[str]], PField(description="Accepted table file extensions.")] = None
    editable: Annotated[Optional[bool], PField(description="Tables must be editable text, not images.")] = None
    max_count: Annotated[Optional[int], PField(description="Maximum number of main-text tables.")] = None
    placement: Annotated[Optional[Placement], PField(description="Where tables go for the initial submission.")] = None
    notes: Annotated[list[str], PField(description="Rules with no structured key.")] = _notes()


@dataclass(frozen=True)
class SupplementaryRules:
    """Supplementary-file format, size, count, and bundling."""

    formats: Annotated[Optional[list[str]], PField(description="Accepted supplementary file extensions.")] = None
    max_file_size_mb: Annotated[Optional[float], PField(description="Maximum size of each supplementary file in MB.")] = None
    max_count: Annotated[Optional[int], PField(description="Maximum number of supplementary files.")] = None
    combined_single_pdf: Annotated[Optional[bool], PField(description="Supplementary text, figures, and tables must be combined into one PDF.")] = None
    notes: Annotated[list[str], PField(description="Rules with no structured key.")] = _notes()


@dataclass(frozen=True)
class ReferenceRules:
    """Reference style and count."""

    style: Annotated[Optional[str], PField(description="Reference style, in words.")] = None
    numbered: Annotated[Optional[bool], PField(description="Numbered (true) vs author-year (false) citations.")] = None
    max_count: Annotated[Optional[int], PField(description="Maximum number of references.")] = None
    include_titles: Annotated[Optional[bool], PField(description="Article titles are required in reference entries.")] = None
    doi_required: Annotated[Optional[bool], PField(description="DOIs are required in reference entries.")] = None
    unpublished_allowed: Annotated[Optional[bool], PField(description="Unpublished work may be cited in the list.")] = None
    preprints_allowed: Annotated[Optional[bool], PField(description="Preprints may be cited in the list.")] = None
    notes: Annotated[list[str], PField(description="Rules with no structured key.")] = _notes()


@dataclass(frozen=True)
class CoverLetterRules:
    """Whether a cover letter is required and how it is supplied."""

    required: Annotated[Optional[bool], PField(description="A cover letter is required.")] = None
    formats: Annotated[Optional[list[str]], PField(description="Accepted cover letter file extensions.")] = None
    max_words: Annotated[Optional[int], PField(description="Maximum cover letter length in words.")] = None
    notes: Annotated[list[str], PField(description="Rules with no structured key.")] = _notes()


@dataclass(frozen=True)
class UploadRules:
    """Size caps on the submission's uploads as a whole."""

    max_total_mb: Annotated[Optional[float], PField(description="Maximum combined size of one submission's uploads in MB.")] = None
    max_file_mb: Annotated[Optional[float], PField(description="Per-file size cap that applies to every upload, in MB.")] = None
    notes: Annotated[list[str], PField(description="Rules with no structured key.")] = _notes()


# The section classes, keyed by the JSON key each lives under. Shared by the
# loader (to build each section), :func:`resolve` (to merge article-type
# overrides), and the schema generator.
SECTION_TYPES: dict[str, type] = {
    "manuscript": ManuscriptRules,
    "title_page": TitlePageRules,
    "abstract": AbstractRules,
    "keywords": KeywordRules,
    "sections": SectionRules,
    "statements": StatementRules,
    "figures": FigureRules,
    "tables": TableRules,
    "supplementary": SupplementaryRules,
    "references": ReferenceRules,
    "cover_letter": CoverLetterRules,
    "upload": UploadRules,
}


@dataclass(frozen=True)
class ManuscriptRequirements:
    """One venue's manuscript requirements, with inheritance already applied.

    ``article_types`` holds the raw per-type override mappings; call
    :func:`resolve` with the filled ``.sub`` values to obtain the requirements
    that apply to the selected article type (the top-level sections merged with
    that type's overrides).
    """

    slug: str
    source_urls: Annotated[list[str], PField(description="Guideline pages the entry was taken from.")] = dataclass_field(default_factory=list)
    retrieved: Annotated[str, PField(description="Date (YYYY-MM-DD) the guidelines were last read.")] = ""
    inherits: Annotated[str, PField(description="Slug whose requirements this entry starts from. Empty for a standalone entry.")] = ""
    article_type_field: Annotated[str, PField(description="Id of the .sub field whose value selects an `article_types` override.")] = ""
    article_types: Annotated[dict[str, dict[str, Any]], PField(description="Per-article-type overrides, keyed by the exact option string; each value carries the same section keys as the top level.")] = dataclass_field(default_factory=dict)
    manuscript: ManuscriptRules = ManuscriptRules()
    title_page: TitlePageRules = TitlePageRules()
    abstract: AbstractRules = AbstractRules()
    keywords: KeywordRules = KeywordRules()
    sections: SectionRules = SectionRules()
    statements: StatementRules = StatementRules()
    figures: FigureRules = FigureRules()
    tables: TableRules = TableRules()
    supplementary: SupplementaryRules = SupplementaryRules()
    references: ReferenceRules = ReferenceRules()
    cover_letter: CoverLetterRules = CoverLetterRules()
    upload: UploadRules = UploadRules()
    notes: Annotated[list[str], PField(description="Venue-wide rules that fit no section.")] = _notes()

    @classmethod
    def from_dict(cls, slug: str, data: dict[str, Any]) -> "ManuscriptRequirements":
        sections = {key: _section_from_dict(typ, data.get(key)) for key, typ in SECTION_TYPES.items()}
        return cls(
            slug=slug.lower(),
            source_urls=list(data.get("source_urls", []) or []),
            retrieved=str(data.get("retrieved", "") or ""),
            inherits=str(data.get("inherits", "") or "").lower(),
            article_type_field=str(data.get("article_type_field", "") or ""),
            article_types={k: dict(v) for k, v in (data.get("article_types") or {}).items()},
            notes=list(data.get("notes", []) or []),
            **sections,
        )

    def to_dict(self) -> dict[str, Any]:
        """The requirements as plain JSON-able data, omitting unset keys."""
        out: dict[str, Any] = {"slug": self.slug}
        if self.source_urls:
            out["source_urls"] = list(self.source_urls)
        if self.retrieved:
            out["retrieved"] = self.retrieved
        if self.inherits:
            out["inherits"] = self.inherits
        if self.article_type_field:
            out["article_type_field"] = self.article_type_field
        if self.article_types:
            out["article_types"] = copy.deepcopy(self.article_types)
        for key in SECTION_TYPES:
            section = _section_to_dict(getattr(self, key))
            if section:
                out[key] = section
        if self.notes:
            out["notes"] = list(self.notes)
        return out


def _section_from_dict(typ: type, data: Any):
    """Build a section dataclass from its raw mapping, ignoring unknown keys."""
    if not data:
        return typ()
    known = {f.name for f in dataclass_fields(typ)}
    kwargs = {}
    for key, value in data.items():
        if key not in known:
            logger.warning("manuscript_requirements.json: ignoring unknown key %r in %s", key, typ.__name__)
            continue
        if key == "notes":
            kwargs[key] = list(value or [])
        elif value is not None:
            kwargs[key] = value
    return typ(**kwargs)


def _section_to_dict(section) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in dataclass_fields(section):
        value = getattr(section, f.name)
        if value is None or (f.name == "notes" and not value):
            continue
        out[f.name] = copy.deepcopy(value)
    return out


def _read_requirements(path: Path) -> dict[str, Any]:
    logger.debug("Loading manuscript requirements from %s", path)
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


@lru_cache(maxsize=1)
def _load_raw() -> dict[str, Any]:
    source = venue_data.active_source()
    if not REQUIREMENTS_PATH.exists():
        logger.warning("No manuscript requirements database at %s", REQUIREMENTS_PATH)
        bundled: dict[str, Any] = {}
    else:
        bundled = _read_requirements(REQUIREMENTS_PATH)
    data = bundled
    if source.kind != "bundled":
        path = source.path(venue_data.REQUIREMENTS_FILE)
        try:
            published = _read_requirements(path)
        except (OSError, ValueError) as exc:
            logger.warning("Could not read %s (%s); using the bundled copy", path, exc)
        else:
            # A local override is the author's own data, used as is; a published
            # copy is merged entry by entry onto what this version can read.
            data = published if source.kind == "override" else merge_published(bundled, published)
    logger.info("Loaded %d manuscript requirement entr(y/ies)", sum(1 for k in data if not k.startswith("$")))
    return data


def _merge_section(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Layer ``override`` onto ``base`` key by key.

    A key set to ``null`` removes the inherited value; ``notes`` lists are
    concatenated (an override adds notes rather than replacing them), and every
    other key is replaced outright -- a list such as ``formats`` is the whole
    new list, not a union, so an override can narrow as well as widen.
    """
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if value is None:
            merged.pop(key, None)
        elif key == "notes":
            merged[key] = list(merged.get(key, [])) + [n for n in value if n not in merged.get(key, [])]
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def merge_entries(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge one requirements mapping onto another, section by section.

    Used both for ``inherits`` (an entry onto its base venue) and for
    ``article_types`` (a type's overrides onto the top-level rules). Section
    keys merge via :func:`_merge_section`; ``article_types`` merge per type;
    scalar/list metadata (``source_urls``, ``retrieved``, ...) is replaced.
    """
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in SECTION_TYPES:
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = _merge_section(merged.get(key, {}) or {}, value)
        elif key == "article_types":
            types = dict(merged.get("article_types", {}) or {})
            for name, rules in (value or {}).items():
                types[name] = merge_entries(types.get(name, {}), rules) if rules is not None else {}
                if rules is None:
                    types.pop(name, None)
            merged["article_types"] = types
        elif key == "notes":
            merged[key] = list(merged.get(key, [])) + [n for n in (value or []) if n not in merged.get(key, [])]
        elif value is None:
            merged.pop(key, None)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _resolve_entry(slug: str, raw: dict[str, Any], seen: tuple[str, ...] = ()) -> dict[str, Any]:
    """The effective mapping for ``slug`` with ``inherits`` expanded."""
    data = raw[slug]
    base_slug = str(data.get("inherits", "") or "")
    if not base_slug:
        return data
    if base_slug in seen:
        raise KeyError(f"{slug!r}: circular inherits chain {seen + (base_slug,)}")
    base_key = next((k for k in raw if k.lower() == base_slug.lower()), None)
    if base_key is None:
        raise KeyError(f"{slug!r} inherits manuscript requirements from unknown venue {base_slug!r}")
    base = _resolve_entry(base_key, raw, seen + (slug,))
    merged = merge_entries(base, {k: v for k, v in data.items() if k != "inherits"})
    merged["inherits"] = base_slug
    return merged


# Keys an entry may carry at its top level (``slug`` is the database key, not a
# key inside the entry), and keys an ``article_types`` override may carry.
_ENTRY_KEYS = frozenset(f.name for f in dataclass_fields(ManuscriptRequirements)) - {"slug"}
_OVERRIDE_KEYS = frozenset(SECTION_TYPES) | {"notes"}
_ALIASES_ADAPTER = TypeAdapter(dict[str, dict[str, list[str]]])


@lru_cache(maxsize=None)
def _adapter(typ: type) -> TypeAdapter:
    return TypeAdapter(typ)


def _without_nulls(section: dict[str, Any]) -> dict[str, Any]:
    # A null drops an inherited key (see merge_entries), so it is always allowed.
    return {k: v for k, v in section.items() if v is not None}


def _rules_problem(rules: Any, allowed: frozenset[str], where: str) -> Optional[str]:
    """Why this version cannot read ``rules`` (an entry or an override), or None."""
    if not isinstance(rules, dict):
        return f"{where} is not an object"
    unknown = sorted(set(rules) - allowed)
    if unknown:
        return f"{where} has key(s) this version does not know: {', '.join(unknown)}"
    for key, typ in SECTION_TYPES.items():
        section = rules.get(key)
        if section is None:
            continue
        if not isinstance(section, dict):
            return f"{where}.{key} is not an object"
        known = {f.name for f in dataclass_fields(typ)}
        unknown = sorted(set(section) - known)
        if unknown:
            return f"{where}.{key} has rule(s) this version does not know: {', '.join(unknown)}"
        try:
            _adapter(typ).validate_python(_without_nulls(section))
        except ValidationError as exc:
            return f"{where}.{key} does not match this version's rule types ({exc.errors()[0]['msg']})"
    if not isinstance(rules.get("notes") or [], list):
        return f"{where}.notes is not a list"
    return None


def _entry_problem(slug: str, entry: Any) -> Optional[str]:
    """Why this version cannot apply the published ``entry``, or None if it can.

    The rules are only data, but ``validate`` is code: it measures the uploads
    against the keys and value types the installed section dataclasses define. A
    newer copy may add a rule this version would silently skip, or change a
    value's type in a way that would break the check at validation time, so
    such an entry is caught here, when the copy is loaded, instead.
    """
    if slug.startswith("$"):
        if slug == "$aliases":
            try:
                _ALIASES_ADAPTER.validate_python(entry)
            except ValidationError as exc:
                return f"$aliases does not match this version's shape ({exc.errors()[0]['msg']})"
        return None
    problem = _rules_problem(entry, _ENTRY_KEYS, slug)
    if problem:
        return problem
    top = {k: entry[k] for k in ("source_urls", "retrieved", "inherits", "article_type_field", "notes") if entry.get(k) is not None}
    try:
        _adapter(ManuscriptRequirements).validate_python({"slug": slug, **top})
    except ValidationError as exc:
        return f"{slug} does not match this version's entry types ({exc.errors()[0]['msg']})"
    overrides = entry.get("article_types") or {}
    if not isinstance(overrides, dict):
        return f"{slug}.article_types is not an object"
    for name, override in overrides.items():
        problem = _rules_problem(override, _OVERRIDE_KEYS, f"{slug}.article_types[{name!r}]")
        if problem:
            return problem
    return None


def merge_published(bundled: dict[str, Any], published: dict[str, Any]) -> dict[str, Any]:
    """Overlay a published requirements database onto the bundled one, entry by entry.

    The published copy is authoritative for the data: it may add, change, or
    drop an entry (no runner depends on these rules, so, unlike a venue in
    ``venues.json``, a new entry needs no release). Each entry is still checked
    against this version's rule vocabulary (:func:`_entry_problem`). An entry
    this version cannot read keeps its bundled version, if there is one. So does
    an entry that inherits from a held-back or missing base, so an entry is never
    resolved against a mix of old and new rules.
    """
    held_back: dict[str, str] = {}
    accepted: set[str] = set()
    for slug, entry in published.items():
        problem = _entry_problem(slug, entry)
        if problem:
            held_back[slug] = problem
        else:
            accepted.add(slug)

    # Drop entries whose base was not accepted, until nothing changes (a chain
    # of inheritance can take several passes).
    lowered = {s.lower(): s for s in accepted}
    changed = True
    while changed:
        changed = False
        for slug in sorted(accepted):
            base = str(published[slug].get("inherits", "") or "") if not slug.startswith("$") else ""
            if base and base.lower() not in lowered:
                accepted.discard(slug)
                lowered.pop(slug.lower(), None)
                held_back[slug] = f"{slug} inherits from {base!r}, which is held back or missing"
                changed = True

    for slug, problem in sorted(held_back.items()):
        logger.info("Published manuscript requirements: %s; using the bundled entry. " "Run 'pip install -U paperpush' to get it.", problem)

    merged: dict[str, Any] = {}
    for slug, entry in published.items():
        if slug in accepted:
            merged[slug] = entry
        elif slug in bundled:
            merged[slug] = bundled[slug]
    return merged


def check_requirements_file(path: Path) -> None:
    """Raise if the requirements database at ``path`` does not load.

    Checks the database this version would actually use -- the file merged onto
    the bundled copy by :func:`merge_published` -- so every entry must resolve
    and build.
    """
    published = _read_requirements(path)
    bundled = _read_requirements(REQUIREMENTS_PATH) if REQUIREMENTS_PATH.exists() else {}
    raw = merge_published(bundled, published)
    for slug in raw:
        if not slug.startswith("$"):
            ManuscriptRequirements.from_dict(slug, _resolve_entry(slug, raw))


def list_requirements() -> list[ManuscriptRequirements]:
    """Every venue entry in the database, sorted by slug."""
    raw = _load_raw()
    return sorted((ManuscriptRequirements.from_dict(k, _resolve_entry(k, raw)) for k in raw if not k.startswith("$")), key=lambda r: r.slug)


def get_requirements(slug: str) -> Optional[ManuscriptRequirements]:
    """The requirements for ``slug`` (case-insensitive), or None if none are recorded.

    A venue without an entry is not an error: ``paperpush validate`` simply has
    no manuscript rules to apply for it.
    """
    raw = _load_raw()
    key = slug.lower()
    for candidate in raw:
        if candidate.startswith("$"):
            continue
        if candidate.lower() == key:
            return ManuscriptRequirements.from_dict(candidate, _resolve_entry(candidate, raw))
    return None


def resolve(reqs: ManuscriptRequirements, values: dict[str, str]) -> ManuscriptRequirements:
    """The requirements that apply to the article type a ``.sub`` selects.

    When the entry names an ``article_type_field`` and the filled value matches
    one of its ``article_types`` keys, that type's overrides are merged onto the
    top-level rules. Otherwise the top-level rules apply unchanged. The result
    carries no ``article_types`` of its own.
    """
    if not reqs.article_type_field or not reqs.article_types:
        return reqs
    selected = values.get(reqs.article_type_field, "").strip()
    override = None
    for name, rules in reqs.article_types.items():
        if name.strip().lower() == selected.lower():
            override = rules
            break
    if override is None:
        return reqs
    base = reqs.to_dict()
    base.pop("article_types", None)
    base.pop("slug", None)
    merged = merge_entries(base, override)
    merged.pop("article_types", None)
    return ManuscriptRequirements.from_dict(reqs.slug, merged)


def heading_aliases(kind: str) -> dict[str, list[str]]:
    """The ``$aliases`` table for ``kind`` (``"sections"`` or ``"statements"``).

    Maps each canonical name to every heading wording that counts as it, the
    canonical name itself included. Lower-cased for matching.
    """
    raw = _load_raw()
    table = (raw.get("$aliases") or {}).get(kind) or {}
    out: dict[str, list[str]] = {}
    for canonical, wordings in table.items():
        names = [canonical] + list(wordings or [])
        out[canonical] = sorted({n.strip().lower() for n in names if n and n.strip()})
    return out
