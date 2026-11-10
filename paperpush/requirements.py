"""Access to the manuscript requirements database.

``submission_forms.json`` describes a venue's *submission form* -- the fields the portal
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
``upload``, ``revision``). Two mechanisms keep the file DRY:

* ``inherits``: an entry takes another venue's requirements wholesale and then
  overrides individual keys section by section (a key set to ``null`` drops the
  inherited value). This is how the AAAS family shares Science's rules.
* publisher fallback: ``$publishers.<slug>.rules`` holds a publisher's house
  rules (Elsevier's Guide for Authors, Springer Nature's editorial policies,
  ...), loaded as the entry ``publisher:<slug>`` (they live under ``$`` so
  older versions, which read every other top-level key as a venue, skip them). A venue whose ``venues.json`` entry names that
  ``publisher`` starts from them, so a rule the venue's own guidelines do not
  state falls back to the publisher's; a key the venue sets (``null``
  included) wins. Rules taken this way are recorded on the result
  (``from_publisher``) and ``validate`` reports what they catch as warnings,
  never errors: a house rule may not hold for every journal. A journal
  paperpush does not list can be checked against its publisher's rules alone
  (``paperpush validate --publisher``). ``$publishers`` also names each
  publisher and the names Crossref uses for it.
* ``article_types``: per-article-type overrides, keyed by the exact option
  string of the ``.sub`` field named in ``article_type_field``. The top level
  holds the primary research-article rules; :func:`resolve` applies the
  overrides for the article type a filled ``.sub`` selects.

Keys starting with ``$`` (``$schema``, ``$aliases``, ``$publishers``) are metadata, not venues.
``$aliases`` maps each canonical section/statement name to the heading
wordings that count as it when the manuscript text is searched.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import json
import logging
import re
from dataclasses import dataclass, fields as dataclass_fields, replace
from dataclasses import field as dataclass_field
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal, Optional

from pydantic import Field as PField
from pydantic import TypeAdapter, ValidationError

from . import venue_data

logger = logging.getLogger(__name__)

# The copy shipped with the package; the file actually read may be a newer
# published copy (see paperpush.venue_data).
REQUIREMENTS_PATH = Path(__file__).with_name("manuscript_requirements.json")

# A publisher's house rules are stored as an ordinary entry under this prefix
# (``publisher:elsevier``); ``$publishers`` holds each publisher's metadata.
PUBLISHER_PREFIX = "publisher:"
PUBLISHERS_KEY = "$publishers"
_PUBLISHER_SLUG = re.compile(r"^[a-z0-9_]+$")


def publisher_key(slug: str) -> str:
    """The database key of a publisher's house rules: ``publisher:<slug>``."""
    return PUBLISHER_PREFIX + slug.strip().lower().removeprefix(PUBLISHER_PREFIX)


def is_publisher_key(key: str) -> bool:
    """Whether ``key`` names a publisher's house rules rather than a venue."""
    return key.lower().startswith(PUBLISHER_PREFIX)


def is_venue_key(key: str) -> bool:
    """Whether ``key`` is a venue entry (not ``$`` metadata, not a publisher)."""
    return not key.startswith("$") and not is_publisher_key(key)

# Where figures/tables sit for the initial submission. ``combined_file``: one
# document (a Word file or PDF) holding all of them, each legend after its figure.
Placement = Literal["separate_files", "end_of_manuscript", "inline", "end_or_inline", "combined_file"]
# Where figures/tables sit relative to the main manuscript file, from the
# manuscript's point of view.
ManuscriptPlacement = Literal["end", "inline", "separate", "end_or_inline"]
LegendsLocation = Literal["end_of_manuscript", "with_figure", "after_references"]
# How the thousands in a number are grouped (manuscript.thousands_separator).
ThousandsSeparator = Literal["comma", "space"]
# How a revised manuscript's changes are marked in the "marked" copy sent with
# the response to reviewers. ``none``: the venue wants no marked copy.
MarkedManuscript = Literal["none", "red_text", "highlight", "track_changes", "other"]

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


# Where a manuscript may disclose its use of generative AI (statements.ai_use_locations).
AiUseLocation = Literal["methods", "acknowledgements", "statement", "supplementary", "figure_captions"]


def _notes() -> Any:
    return dataclass_field(default_factory=list)


@dataclass(frozen=True)
class TemplateLayout:
    """Geometry of the venue's template in a compiled PDF, for spotting an edited one.

    Measured from the official template compiled unchanged (see
    :mod:`paperpush.template_check`), in PDF points with the origin at the
    page's bottom-left. Text positions are the bottom of the glyph boxes of the
    body-text lines: ``text_top_pt`` is the highest first line, ``text_bottom_pt``
    the lowest last line of a full page.
    """

    page_width_pt: Annotated[Optional[float], PField(description="Page width in points.")] = None
    page_height_pt: Annotated[Optional[float], PField(description="Page height in points.")] = None
    text_left_pt: Annotated[Optional[float], PField(description="x of the text block's left edge.")] = None
    text_right_pt: Annotated[Optional[float], PField(description="x of the text block's right edge (justified line ends).")] = None
    text_top_pt: Annotated[Optional[float], PField(description="y of the highest body-text line on a page.")] = None
    text_bottom_pt: Annotated[Optional[float], PField(description="y of the lowest body-text line on a full page.")] = None
    body_font_pt: Annotated[Optional[float], PField(description="Body font size in points.")] = None
    baseline_skip_pt: Annotated[Optional[float], PField(description="Distance between consecutive body-text baselines in points.")] = None
    running_head: Annotated[Optional[str], PField(description="Running head the submission version prints on page 1 (e.g. 'Under review as a conference paper at ICLR 2027').")] = None
    margin_line_numbers: Annotated[Optional[bool], PField(description="The submission version prints line numbers in the margin.")] = None
    tolerance_pt: Annotated[float, PField(description="How far, in points, a measured edge may sit from the template's before it is reported.")] = 3.0


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
    main_text_start_headings: Annotated[Optional[list[str]], PField(description="Headings that start the main text the venue's word count covers, when it is not the Introduction (e.g. Main, Results). Used to check a word count stated on the title page.")] = None
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
    template_layout: Annotated[Optional[TemplateLayout], PField(description="Geometry of the compiled template, checked against the manuscript PDF to catch shrunken margins, font, or line spacing.")] = None
    language: Annotated[Optional[str], PField(description="Mandated language variant, if any.")] = None
    thousands_separator: Annotated[Optional[ThousandsSeparator], PField(description="How numbers of 10,000 and over group their thousands: comma (10,000) or space (10 000, often a thin space). Empty when the guidelines do not say; `validate` then only checks that the manuscript is consistent.")] = None
    group_four_digit_numbers: Annotated[Optional[bool], PField(description="Whether four-digit numbers take the thousands separator too: yes for 1,000, no for 1000 (SI and some house styles). Empty when the guidelines do not say.")] = None
    et_al_period: Annotated[Optional[bool], PField(description="Whether 'et al' takes a full stop: yes for 'et al.', no for 'et al' (AMA and RSNA house style). Empty when the guidelines do not say; `validate` then asks for 'et al.'.")] = None
    initial_submission_any_format: Annotated[Optional[str], PField(description="Set only when the venue lets the initial submission be in any format (formatting rules apply from revision or acceptance): the venue's rule, in one sentence. `validate` prints it at the top of the report and `--initial-submission` turns errors into warnings.")] = None
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
    structured: Annotated[Optional[bool], PField(description="Abstract must be structured into labeled subsections.")] = None
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
    max_words: Annotated[Optional[dict[str, int]], PField(description="Word limit per section, keyed by canonical section name (matched via $aliases); a section runs from its heading to the next recognised heading.")] = None
    notes: Annotated[list[str], PField(description="Rules with no structured key.")] = _notes()


@dataclass(frozen=True)
class StatementRules:
    """Declarations that must appear in the manuscript text."""

    required: Annotated[Optional[list[StatementId]], PField(description="Canonical ids of the declarations the manuscript must contain (matched via $aliases).")] = None
    optional: Annotated[Optional[list[StatementId]], PField(description="Declarations the venue accepts but does not require.")] = None
    ai_use_if_used: Annotated[Optional[bool], PField(description="Use of generative AI (LLMs, image generators) must be disclosed when there was any. To require a statement whether or not AI was used, list ai_use in required instead.")] = None
    ai_use_locations: Annotated[Optional[list[AiUseLocation]], PField(description="Where in the manuscript the AI-use disclosure may go; any one of them will do: methods, acknowledgements, statement (a section of its own), supplementary (the supplementary material), figure_captions (the legend of each AI-touched figure). Unset: anywhere.")] = None
    ai_use_cover_letter: Annotated[Optional[bool], PField(description="The AI-use disclosure must also be made in the cover letter.")] = None
    ai_copyediting_exempt: Annotated[Optional[bool], PField(description="AI used only to improve language, grammar, or spelling (copy editing) need not be disclosed.")] = None
    ai_authorship_prohibited: Annotated[Optional[bool], PField(description="AI tools (ChatGPT, LLMs) may not be listed as authors.")] = None
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
    color_mode: Annotated[Optional[list[str]], PField(description="Accepted color spaces (RGB, CMYK, grayscale).")] = None
    min_font_size_pt: Annotated[Optional[float], PField(description="Minimum font size for figure lettering, in points.")] = None
    max_font_size_pt: Annotated[Optional[float], PField(description="Maximum font size for figure lettering, in points.")] = None
    font: Annotated[Optional[str], PField(description="Mandated or recommended figure font.")] = None
    legend_max_words: Annotated[Optional[int], PField(description="Maximum words per figure legend.")] = None
    legends_location: Annotated[Optional[LegendsLocation], PField(description="Where figure legends go.")] = None
    multipanel_labels: Annotated[Optional[str], PField(description="Required style of panel labels.")] = None
    color_charge: Annotated[Optional[bool], PField(description="Color figures carry a charge.")] = None
    print_grayscale: Annotated[Optional[bool], PField(description="The print edition shows figures in grayscale, so colors that differ only in hue merge (figures.grayscale checks this; color_charge implies it).")] = None
    placement: Annotated[Optional[Placement], PField(description="Where figures go for the initial submission. combined_file: every figure in one document (Word or PDF), in citation order, each legend after its figure.")] = None
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
    max_pages: Annotated[Optional[int], PField(description="Maximum pages of each supplementary file (a PDF, or a Word file's saved page count).")] = None
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


@dataclass(frozen=True)
class RevisionRules:
    """What a revised manuscript is sent with: the marked copy and the response to reviewers."""

    marked_manuscript: Annotated[Optional[MarkedManuscript], PField(description="How the changes are marked in the marked-up copy of a revised manuscript: none (no marked copy wanted), red_text, highlight, track_changes (Word Track Changes), or other (described in notes). Unset: not stated (paperpush-respond-to-reviewers defaults to red_text).")] = None
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
    "revision": RevisionRules,
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
    revision: RevisionRules = RevisionRules()
    notes: Annotated[list[str], PField(description="Venue-wide rules that fit no section.")] = _notes()
    # Set when the entry is resolved, never stored in the database (see
    # _RUNTIME_KEYS): the publisher whose house rules fill the gaps, and which
    # rules came from it -- "section.key" paths, plus "article_types.<name>"
    # for a whole per-type override. Notes taken from the publisher are tagged
    # in their text instead (see publisher_note_tag).
    publisher: str = ""
    from_publisher: frozenset[str] = frozenset()

    @property
    def publisher_rules_only(self) -> bool:
        """Whether every rule came from the publisher (a journal paperpush does not list)."""
        return is_publisher_key(self.slug)

    def without_publisher_rules(self) -> "ManuscriptRequirements":
        """These requirements with every rule taken from the publisher removed."""
        if not self.publisher:
            return self
        data = self.to_dict()
        data.pop("slug", None)
        data.pop("publisher", None)
        data.pop("from_publisher", None)
        tag = publisher_note_tag(self.publisher)
        for path in self.from_publisher:
            head, _, key = path.partition(".")
            if head == "article_types":
                (data.get("article_types") or {}).pop(key, None)
            elif isinstance(data.get(head), dict):
                data[head].pop(key, None)
        for key in list(SECTION_TYPES) + ["notes"]:
            if key == "notes":
                data["notes"] = [n for n in data.get("notes", []) if not n.startswith(tag)]
            elif isinstance(data.get(key), dict) and "notes" in data[key]:
                data[key]["notes"] = [n for n in data[key]["notes"] if not n.startswith(tag)]
        return ManuscriptRequirements.from_dict(self.slug, data)

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
            publisher=str(data.get("publisher", "") or "").lower(),
            from_publisher=frozenset(data.get("from_publisher") or ()),
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
        if self.publisher:
            out["publisher"] = self.publisher
            out["from_publisher"] = sorted(self.from_publisher)
        return out


# Keys inside a section whose value is itself a small dataclass.
_NESTED_TYPES: dict[str, type] = {"template_layout": TemplateLayout}


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
        elif key in _NESTED_TYPES and isinstance(value, dict):
            kwargs[key] = _section_from_dict(_NESTED_TYPES[key], value)
        elif value is not None:
            kwargs[key] = value
    return typ(**kwargs)


def _section_to_dict(section) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in dataclass_fields(section):
        value = getattr(section, f.name)
        if value is None or (f.name == "notes" and not value):
            continue
        out[f.name] = _section_to_dict(value) if f.name in _NESTED_TYPES else copy.deepcopy(value)
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
    return _expand_publishers(data)


def _expand_publishers(data: dict[str, Any]) -> dict[str, Any]:
    """``data`` plus a ``publisher:<slug>`` entry for each publisher's house rules.

    The rules are stored inside ``$publishers`` (``$publishers.<slug>.rules``)
    rather than as top-level entries, because versions of paperpush from before
    publisher fallback read every top-level key as a venue, and would refuse a
    published copy with rules for a "venue" ``venues.json`` does not list. The
    loaded database exposes them as ``publisher:<slug>`` entries, so they resolve
    and inherit like any other.
    """
    table = data.get(PUBLISHERS_KEY) or {}
    if not isinstance(table, dict) or not any(isinstance(v, dict) and v.get("rules") for v in table.values()):
        return data
    out = dict(data)
    for slug, entry in table.items():
        if isinstance(entry, dict) and entry.get("rules"):
            out[publisher_key(str(slug))] = entry["rules"]
    return out


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
_RUNTIME_KEYS = frozenset({"slug", "publisher", "from_publisher"})
_ENTRY_KEYS = frozenset(f.name for f in dataclass_fields(ManuscriptRequirements)) - _RUNTIME_KEYS
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
        elif slug == PUBLISHERS_KEY:
            return _publishers_problem(entry)
        return None
    problem = _rules_problem(entry, _ENTRY_KEYS, slug)
    if problem:
        return problem
    if not is_publisher_key(slug) and is_publisher_key(str(entry.get("inherits", "") or "")):
        return f"{slug} inherits {entry['inherits']!r}; name the publisher with `publisher` in venues.json instead (its house rules then back the venue's own)"
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
    ``submission_forms.json``, a new entry needs no release). Each entry is still checked
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


def check_requirements_file(path: Path) -> list[str]:
    """Raise if the requirements database at ``path`` does not load.

    Checks the database this version would actually use -- the file merged onto
    the bundled copy by :func:`merge_published` -- so every entry must resolve
    and build. Returns the venue slugs the file at ``path`` has rules for.
    """
    published = _read_requirements(path)
    bundled = _read_requirements(REQUIREMENTS_PATH) if REQUIREMENTS_PATH.exists() else {}
    raw = _expand_publishers(merge_published(bundled, published))
    for slug in raw:
        if not slug.startswith("$"):
            ManuscriptRequirements.from_dict(slug, _resolve_entry(slug, raw))
    return [slug for slug in published if is_venue_key(slug)]


def list_requirements() -> list[ManuscriptRequirements]:
    """Every venue entry in the database, sorted by slug (publishers' house rules excluded).

    Each is the venue's own rules, without its publisher's (see
    :func:`get_requirements` for the rules ``validate`` applies).
    """
    raw = _load_raw()
    return sorted((ManuscriptRequirements.from_dict(k, _resolve_entry(k, raw)) for k in raw if is_venue_key(k)), key=lambda r: r.slug)


# ---------------------------------------------------------------------------
# Publishers


@dataclass(frozen=True)
class Publisher:
    """A publisher, as ``$publishers`` records it."""

    slug: str
    name: Annotated[str, PField(description="Display name, e.g. 'Springer Nature'.")] = ""
    site_url: Annotated[str, PField(description="URL of the publisher's author guidelines (its house rules), or its homepage.")] = ""
    crossref_names: Annotated[list[str], PField(description="Publisher names as Crossref records them (e.g. 'Elsevier BV'), for finding the publisher of a journal paperpush does not list from its ISSN.")] = dataclass_field(default_factory=list)
    parent: Annotated[str, PField(description="Slug of the publisher this one is an imprint of (Cell Press: elsevier). Informational; the house rules chain through `inherits`.")] = ""
    rules: Annotated[dict[str, Any], PField(description="The publisher's house rules: the rules it states for all its journals, with the same keys as a venue entry. They back the rules of every venue whose venues.json entry names this publisher; `inherits` may name another publisher's rules as `publisher:<slug>`. Empty when paperpush holds none.")] = dataclass_field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or self.slug

    @property
    def has_rules(self) -> bool:
        """Whether the database holds this publisher's house rules."""
        return bool(self.rules)


_PUBLISHER_KEYS = frozenset(f.name for f in dataclass_fields(Publisher)) - {"slug"}


def _publishers_problem(entry: Any) -> Optional[str]:
    if not isinstance(entry, dict):
        return f"{PUBLISHERS_KEY} is not an object"
    for slug, data in entry.items():
        if not _PUBLISHER_SLUG.match(str(slug)):
            return f"{PUBLISHERS_KEY}: publisher slug {slug!r} is not lowercase letters, digits, and underscores"
        if not isinstance(data, dict):
            return f"{PUBLISHERS_KEY}.{slug} is not an object"
        unknown = sorted(set(data) - _PUBLISHER_KEYS)
        if unknown:
            return f"{PUBLISHERS_KEY}.{slug} has key(s) this version does not know: {', '.join(unknown)}"
        try:
            _adapter(Publisher).validate_python({"slug": slug, **{k: v for k, v in data.items() if k != "rules"}})
        except ValidationError as exc:
            return f"{PUBLISHERS_KEY}.{slug} does not match this version's types ({exc.errors()[0]['msg']})"
        rules = data.get("rules")
        if rules:
            problem = _entry_problem(publisher_key(str(slug)), rules)
            if problem:
                return f"{PUBLISHERS_KEY}.{slug}.rules: {problem}"
            base = str(rules.get("inherits", "") or "").lower()
            if base and (not is_publisher_key(base) or not (entry.get(base[len(PUBLISHER_PREFIX) :]) or {}).get("rules")):
                return f"{PUBLISHERS_KEY}.{slug}.rules inherits {base!r}, which is not another publisher's house rules"
    return None


def list_publishers() -> list[Publisher]:
    """Every publisher ``$publishers`` records, sorted by slug."""
    table = _load_raw().get(PUBLISHERS_KEY) or {}
    return sorted((Publisher(slug=str(k).lower(), **{f: v for f, v in (d or {}).items() if f in _PUBLISHER_KEYS}) for k, d in table.items()), key=lambda p: p.slug)


def get_publisher(name: str) -> Optional[Publisher]:
    """The publisher with this slug, display name, or Crossref name (case-insensitive), or None."""
    wanted = _fold_name(name.removeprefix(PUBLISHER_PREFIX))
    if not wanted:
        return None
    for pub in list_publishers():
        if wanted in {_fold_name(pub.slug), _fold_name(pub.name), *(_fold_name(n) for n in pub.crossref_names)}:
            return pub
    return None


def _fold_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def publisher_note_tag(publisher: str) -> str:
    """The prefix marking a note taken from ``publisher``'s house rules."""
    pub = get_publisher(publisher)
    return f"[{pub.display_name if pub else publisher} house rule] "


# validate re-runs a check without the publisher's rules to tell which issues
# they caused (see paperpush.validate.Checks); this switches get_requirements
# to the venue's own rules for the duration.
_OWN_RULES_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar("paperpush_own_rules_only", default=False)


@contextlib.contextmanager
def own_rules_only() -> Iterator[None]:
    """Within the block, :func:`get_requirements` leaves out publishers' house rules."""
    token = _OWN_RULES_ONLY.set(True)
    try:
        yield
    finally:
        _OWN_RULES_ONLY.reset(token)


def _find_key(raw: dict[str, Any], key: str) -> Optional[str]:
    key = key.lower()
    return next((k for k in raw if not k.startswith("$") and k.lower() == key), None)


def venue_publisher(slug: str) -> str:
    """The ``publisher`` slug ``venues.json`` records for the venue, or ""."""
    from .database import _load_info

    info = _load_info()
    key = next((k for k in info if k.lower() == slug.lower()), None)
    return str((info.get(key) or {}).get("publisher", "") or "").strip().lower() if key else ""


def _tag_notes(entry: dict[str, Any], tag: str) -> dict[str, Any]:
    """``entry`` with every note (top level, sections, article types) prefixed by ``tag``."""
    out = copy.deepcopy(entry)

    def tag_all(rules: dict[str, Any]) -> None:
        if rules.get("notes"):
            rules["notes"] = [n if n.startswith(tag) else tag + n for n in rules["notes"]]
        for key in SECTION_TYPES:
            section = rules.get(key)
            if isinstance(section, dict) and section.get("notes"):
                section["notes"] = [n if n.startswith(tag) else tag + n for n in section["notes"]]

    tag_all(out)
    for override in (out.get("article_types") or {}).values():
        if isinstance(override, dict):
            tag_all(override)
    return out


def _rule_paths(entry: dict[str, Any]) -> set[str]:
    """The ``section.key`` path of every rule ``entry`` sets (notes excluded)."""
    paths = {f"{section}.{key}" for section in SECTION_TYPES for key in (entry.get(section) or {}) if key != "notes"}
    paths |= {f"article_types.{name}" for name in (entry.get("article_types") or {})}
    return paths


def _with_publisher(slug: str, own: Optional[dict[str, Any]], publisher: str, raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """``own`` (a resolved venue entry, or None) layered onto ``publisher``'s house rules.

    Returns None when the database holds no house rules for ``publisher``. The
    result records the publisher and which rules came from it (``from_publisher``).
    The publisher's per-type overrides apply only to a venue with no entry of its
    own: its ``article_types`` are keyed by the venue's own option strings.
    """
    pkey = _find_key(raw, publisher_key(publisher))
    if pkey is None:
        return None
    base = _tag_notes(_resolve_entry(pkey, raw), publisher_note_tag(publisher))
    base.pop("inherits", None)
    if own is None:
        merged = base
        taken = _rule_paths(base)
    else:
        base.pop("article_types", None)
        base.pop("article_type_field", None)
        own_body = {k: v for k, v in own.items() if k != "inherits"}
        merged = merge_entries(base, own_body)
        if own.get("inherits"):
            merged["inherits"] = own["inherits"]
        else:
            merged.pop("inherits", None)
        for key in ("source_urls", "retrieved"):
            if not own.get(key):
                merged.pop(key, None)
        taken = {p for p in _rule_paths(base) if p not in _rule_paths(own_body) and _path_in(merged, p) and not _path_nulled(own_body, p)}
    merged["publisher"] = publisher
    merged["from_publisher"] = sorted(taken)
    return merged


def _path_in(entry: dict[str, Any], path: str) -> bool:
    head, _, key = path.partition(".")
    return isinstance(entry.get(head), dict) and key in entry[head]


def _path_nulled(entry: dict[str, Any], path: str) -> bool:
    head, _, key = path.partition(".")
    return head in entry and (entry[head] is None or (isinstance(entry[head], dict) and key in entry[head] and entry[head][key] is None))


def _chain(slug: str, raw: dict[str, Any]) -> list[str]:
    """``slug`` and every entry it inherits from, lower-cased, nearest first."""
    out: list[str] = []
    key: Optional[str] = _find_key(raw, slug)
    while key is not None and key.lower() not in out:
        out.append(key.lower())
        base = str(raw[key].get("inherits", "") or "")
        key = _find_key(raw, base) if base else None
    return out


def get_requirements(slug: str) -> Optional[ManuscriptRequirements]:
    """The requirements ``validate`` applies for ``slug`` (case-insensitive), or None if none are recorded.

    The venue's own rules, layered onto its publisher's house rules when
    ``venues.json`` names a ``publisher`` the database has rules for (the
    result's ``from_publisher`` says which rules came from there). A venue
    with no entry of its own gets its publisher's rules alone. ``slug`` may also
    be a ``publisher:<slug>`` key, for a journal paperpush does not list: every
    rule then counts as the publisher's.

    A venue without either is not an error: ``paperpush validate`` simply has
    no manuscript rules to apply for it.
    """
    raw = _load_raw()
    own_only = _OWN_RULES_ONLY.get()
    if is_publisher_key(slug):
        key = _find_key(raw, slug)
        if key is None:
            return None
        if own_only:
            return ManuscriptRequirements(slug=key.lower())
        publisher = key[len(PUBLISHER_PREFIX) :].lower()
        merged = _with_publisher(key, None, publisher, raw)
        return None if merged is None else ManuscriptRequirements.from_dict(key, merged)
    key = _find_key(raw, slug)
    own = _resolve_entry(key, raw) if key is not None else None
    publisher = "" if own_only else venue_publisher(slug)
    if publisher and publisher_key(publisher) not in _chain(slug, raw):
        merged = _with_publisher(key or slug, own, publisher, raw)
        if merged is not None:
            return ManuscriptRequirements.from_dict(key or slug, merged)
    return None if own is None else ManuscriptRequirements.from_dict(key, own)


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
    # A type's own limit replaces the top-level one, whichever scope each is in:
    # a 7,000-word Review is not also held to the 4,300 words of an Article.
    stated = override.get("manuscript") or {}
    for pair in (("max_words", "max_words_before_refs"), ("max_pages", "max_pages_before_refs")):
        for own, inherited in (pair, pair[::-1]):
            if stated.get(own) is not None and inherited not in stated:
                (merged.get("manuscript") or {}).pop(inherited, None)
    resolved = ManuscriptRequirements.from_dict(reqs.slug, merged)
    if reqs.publisher:
        # The override's rules belong to whoever wrote the override.
        set_here = _rule_paths(override)
        taken = set(reqs.from_publisher) - {p for p in reqs.from_publisher if p.startswith("article_types.")}
        taken = (taken | set_here) if f"article_types.{name}" in reqs.from_publisher else (taken - set_here)
        resolved = replace(resolved, from_publisher=frozenset(taken))
    return resolved


def any_format_initial_rule(slug: str, values: Optional[dict[str, str]] = None) -> str:
    """The venue's "any format at initial submission" rule, or "" if it has none.

    Reads ``manuscript.initial_submission_any_format`` for the article type
    ``values`` selects (the top-level rules when ``values`` is None).
    """
    reqs = get_requirements(slug)
    if reqs is None:
        return ""
    if values is not None:
        reqs = resolve(reqs, values)
    return (reqs.manuscript.initial_submission_any_format or "").strip()


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
