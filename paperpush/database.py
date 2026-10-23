"""Access to the venue databases.

Three JSON files shipped with the package describe the venues:

* ``venues.json`` -- the master list: one entry per venue slug with its general
  metadata (name, type, homepage, double-blind review, ...). A venue exists
  exactly when it has an entry here.
* ``submission_forms.json`` -- the submission form for each venue paperpush can
  submit to: the fields the portal asks for and the portal-level facts its
  runner needs.
* ``manuscript_requirements.json`` -- the author-guideline rules ``validate``
  measures the manuscript against (read by :mod:`paperpush.requirements`).

A venue may have a form, requirements, or both, so one can be supported by
``validate`` without ``submit`` or the other way round (see
:attr:`Venue.supports_validate` and :attr:`Venue.supports_submit`). A venue
without a form gets :func:`default_form`, so it still has a ``.sub`` to fill.

A newer copy may also be published separately from releases and cached locally
(see :mod:`paperpush.venue_data`). The bundled files are always the baseline: a
published form replaces a bundled one only when the installed runner can drive
it, and a published master entry is used only for a venue this version can
serve (see :func:`merge_published` and :func:`merge_published_info`).
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from dataclasses import field as dataclass_field
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from pydantic import Field as PField
from pydantic import TypeAdapter, ValidationError

from . import venue_data

logger = logging.getLogger(__name__)

# The copies shipped with the package. The files actually read may come from a
# published copy instead; see paperpush.venue_data.
DATABASE_PATH = Path(__file__).with_name("venues.json")
FORMS_PATH = Path(__file__).with_name("submission_forms.json")

# Directory holding shared keyword/vocabulary lists that a field can pull in via
# its ``options_file`` key (see :func:`_load_options_file`).
ASSETS_DIR = DATABASE_PATH.parent / "venues" / "_assets"


@lru_cache(maxsize=None)
def _load_options_file(name: str) -> list[str]:
    """Return the newline-separated options stored in ``venues/_assets/<name>``.

    Blank lines and leading/trailing whitespace are ignored. The result backs a
    field's ``options`` so a long controlled vocabulary (e.g. the Bioinformatics
    portal keywords or the PLOS Computational Biology classifications) lives in
    one shared file rather than being inlined in ``venues.json``.
    """
    # A published copy may add or update a vocabulary; the bundled one backs it.
    path = venue_data.active_source().assets / name
    if not path.is_file():
        path = ASSETS_DIR / name
    logger.debug("Loading field options from %s", path)
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line]


def options_command(venue_slug: str, field_id: str) -> str:
    """The ``paperpush options`` invocation that lists a field's choices.

    When a field's options come from an ``options_file`` (a long controlled
    vocabulary such as the arXiv categories), inlining the full list into help
    text and error messages is unwieldy. Instead we point the user at this
    command, which prints the file's contents. Kept here so every surface that
    references it (the ``.sub`` template, validation errors, the CLI) stays in
    sync on the exact command string.
    """
    return f"paperpush options {venue_slug}.{field_id}"


# Widget/value kind a field may take, and the roles an autofill engine may use to
# populate it. These are the single source for both the runtime dataclasses below
# and the generated JSON schema (paperpush.schema_models reads them off the
# dataclass annotations via pydantic). They are not enforced at runtime -- the
# stdlib dataclass does not validate -- but document the accepted values and
# drive editor autocomplete.
FieldType = Literal[
    "text",
    "textarea",
    "choice",
    "multichoice",
    "boolean",
    "int",
    "file",
    "filelist",
    "authorlist",
]
AutofillRole = Literal["extract", "classify", "filemap", "never", ""]


@dataclass(frozen=True)
class ConditionalLimit:
    """A length limit whose value depends on a sibling field's value.

    Some venues set a manuscript's page or word cap by article type -- e.g.
    Bioinformatics allows up to 7 pages for an Original Paper but 4 for an
    Applications Note. ``field`` names the sibling field that selects the limit
    (``article_type``), ``values`` maps each of that field's values to the limit
    that applies, and ``default`` is the limit used when the selected value is
    not in the map. Used as the ``*_by`` companion to a scalar ``max_*`` limit on
    a manuscript ``file`` field (see :class:`Field`); the scalar acts as the
    fallback when no conditional limit resolves.
    """

    field: Annotated[
        str,
        PField(description="Id of the sibling field whose value selects the limit."),
    ]
    values: Annotated[
        dict[str, int],
        PField(description="Map from the selector field's value to the limit that applies."),
    ]
    default: Annotated[
        Optional[int],
        PField(description="Limit used when the selector's value is absent from " "`values`. None means no limit applies then."),
    ] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConditionalLimit":
        return cls(
            field=data["field"],
            values=dict(data.get("values", {})),
            default=data.get("default"),
        )


def _opt_conditional_limit(data: Any) -> Optional[ConditionalLimit]:
    """Build a :class:`ConditionalLimit` from a raw ``*_by`` mapping, or None."""
    return ConditionalLimit.from_dict(data) if data else None


@dataclass(frozen=True)
class Field:
    """A single field a venue asks for during submission.

    The per-attribute descriptions below are attached with
    ``Annotated[..., pydantic.Field(description=...)]`` so they serve double duty:
    documentation for readers of this class and the field descriptions in the
    generated ``venues.schema.json`` (see :mod:`paperpush.schema_models`).
    The annotations are inert at runtime -- this is a plain stdlib dataclass and
    performs no validation.
    """

    id: Annotated[
        str,
        PField(description="Stable identifier, unique within the venue; the key " "used in the .sub file and for inheritance merges."),
    ]
    label: Annotated[str, PField(description="Human-readable prompt shown for the field.")]
    type: Annotated[FieldType, PField(description="Widget/value kind.")]
    required: Annotated[bool, PField(description="Whether submission requires a value.")] = False
    help: Annotated[str, PField(description="Guidance shown alongside the field.")] = ""
    options: Annotated[
        Optional[list[str]],
        PField(description="Allowed values for `choice`/`multichoice`."),
    ] = None
    options_file: Annotated[
        Optional[str],
        PField(description="Name of a newline-separated keyword file under " "`venues/_assets/` whose lines become this field's `options`. Lets a " "long controlled vocabulary live in one shared text file instead of being " "inlined (and duplicated) in the database. When set, it supersedes any " "inline `options`."),
    ] = None
    # For a ``choice``/``multichoice`` field, whether ``options`` is an advisory
    # recommendation rather than a closed set. When true, a value outside
    # ``options`` is allowed and reported only as a WARNING during validation, not
    # an ERROR (e.g. Bioinformatics' free Keywords box, where the controlled
    # vocabulary is a suggestion list but any typed term is accepted). When
    # false/unset, ``options`` is enforced as a closed set. Ignored when the field
    # has no ``options``.
    options_recommended: Annotated[
        bool,
        PField(description="For a `choice`/`multichoice` field, whether `options` is " "advisory (a value outside the list is a WARNING, not an ERROR) rather " "than a closed set."),
    ] = False
    default: Annotated[Any, PField(description="Default value (any JSON type).")] = None
    accept: Annotated[
        Optional[list[str]],
        PField(description="Permitted file extensions for `file`/`filelist`, e.g. " '[".pdf", ".docx"]. Omit (not []) when the upload imposes no ' "extension restriction."),
    ] = None
    fields: Annotated[
        Optional[list[str]],
        PField(description="Sub-field (column) names for a structured multi-item " "field such as an `authorlist` or a pipe-delimited `textarea` (funding, " "suggested reviewers). Each item line is `|`-delimited in this order. A " "trailing `?` marks an optional column (e.g. `department?`); every other " "column is required -- validation flags an item whose required column is " "blank. Author name and the `corresponding` flag have their own checks and " "are always effectively required regardless of marker."),
    ] = None
    type_options: Annotated[
        Optional[list[str]],
        PField(description="Per-file 'file type' dropdown choices applied to this " "field's uploads."),
    ] = None
    # Inclusive bounds on how many items a multi-item field may carry: the chosen
    # options of a ``multichoice`` (e.g. Bioinformatics' portal keywords require
    # 3-6), or the lines of a ``filelist``/``authorlist``/list-style ``textarea``
    # (e.g. at least 4 recommended reviewers, at most N figures). ``None`` means
    # unbounded on that side. Ignored for single-value fields.
    min_count: Annotated[
        Optional[int],
        PField(description="Inclusive lower bound on item count for a multi-item " "field (multichoice selections, filelist/authorlist/list-textarea " "lines)."),
    ] = None
    max_count: Annotated[
        Optional[int],
        PField(description="Inclusive upper bound on item count for a multi-item " "field."),
    ] = None
    # For a text-bearing field (``text``/``textarea``), the maximum length of the
    # value in words (``word_count``) and/or characters (``character_count``);
    # e.g. an abstract capped at 250 words. Also applies to a ``choice`` with
    # ``options_recommended``, where an off-list value is typed into the portal's
    # free-text box and so carries that box's limit. ``None`` means no limit on
    # that measure. Ignored for other field types.
    word_count: Annotated[
        Optional[int],
        PField(description="Maximum length in words for a `text`/`textarea` value."),
    ] = None
    character_count: Annotated[
        Optional[int],
        PField(description="Maximum length in characters for a `text`/`textarea` " "value, or for the free text of a `choice` with `options_recommended`."),
    ] = None
    # The lower bound counterpart to ``character_count``: the shortest value the
    # portal accepts (e.g. arXiv rejects an abstract under 20 characters).
    # ``None`` means no minimum.
    min_character_count: Annotated[
        Optional[int],
        PField(description="Minimum length in characters for a `text`/`textarea` " "value (the lower-bound counterpart to `character_count`)."),
    ] = None
    # For a text-bearing field (``text``/``textarea``), require every non-blank
    # line of the value to be a valid http/https URL (e.g. a data-availability
    # statement that must hold repository links). ``None``/false imposes no URL
    # constraint. Ignored for non-text fields.
    require_url: Annotated[
        Optional[bool],
        PField(description="For a `text`/`textarea`, require each non-blank line of " "the value to be a valid http/https URL."),
    ] = None
    # For a manuscript ``file`` field, the maximum length of the manuscript. Each
    # measure comes in two scopes -- before the reference list
    # (``max_words_before_refs`` / ``max_pages_before_refs``) and the whole
    # document (``max_words`` / ``max_pages``) -- so a venue can cap whichever
    # it specifies. The uploaded file is parsed and measured during validation
    # (word counts work for .tex/.docx/.pdf; page counts for .pdf, plus a
    # best-effort cached count for .docx totals). ``None`` means no limit on that
    # measure. See ``paperpush.manuscript``.
    max_words_before_refs: Annotated[
        Optional[int],
        PField(description="For a manuscript `file`, the maximum word count of the " "main text before the reference list."),
    ] = None
    max_pages_before_refs: Annotated[
        Optional[int],
        PField(description="For a manuscript `file` (PDF), the maximum page count " "before the reference list."),
    ] = None
    # Sections other than the references that a venue excludes from its
    # before-refs limits -- an appendix, or a required statement a conference
    # asks for outside the page count (ICLR's ethics / reproducibility / AI-use
    # statements). Each entry is a heading *phrase*, matched as a standalone
    # heading line, and the earliest of them or the reference list ends the main
    # text. Setting it also sharpens the page count: a heading that starts its
    # own page means the main text ended on the page before. See
    # ``paperpush.manuscript.main_text_end``.
    main_text_end_headings: Annotated[
        Optional[list[str]],
        PField(description="For a manuscript `file`, headings *other than the reference " "list* that end the main text for the `*_before_refs` limits (e.g. " "`Appendix`, `Ethics Statement`) -- sections the venue excludes from " "its word/page count. Each is a heading phrase matched on a line of " "its own; the earliest match, references included, ends the main text. " "When set, a heading that starts its own page (only a running head or " "page number ahead of it) counts the main text as ending on the " "previous page."),
    ] = None
    max_words: Annotated[
        Optional[int],
        PField(description="For a manuscript `file`, the maximum word count of the " "whole document (references included)."),
    ] = None
    max_pages: Annotated[
        Optional[int],
        PField(description="For a manuscript `file`, the maximum page count of the " "whole document. Verified for PDF; for .docx a best-effort cached count."),
    ] = None
    # When a venue sets one of the four limits above by the value of another
    # field (e.g. Bioinformatics' page cap varies with ``article_type``), the
    # ``*_by`` companion encodes that dependency as a ``ConditionalLimit``. When
    # both a scalar and its ``*_by`` are present, the conditional wins where it
    # resolves and the scalar is the fallback. ``None`` means the limit, if any,
    # is the scalar alone.
    max_words_before_refs_by: Annotated[
        Optional[ConditionalLimit],
        PField(description="`max_words_before_refs` selected by a sibling field's " "value (e.g. by article type)."),
    ] = None
    max_pages_before_refs_by: Annotated[
        Optional[ConditionalLimit],
        PField(description="`max_pages_before_refs` selected by a sibling field's " "value."),
    ] = None
    max_words_by: Annotated[
        Optional[ConditionalLimit],
        PField(description="`max_words` selected by a sibling field's value."),
    ] = None
    max_pages_by: Annotated[
        Optional[ConditionalLimit],
        PField(description="`max_pages` selected by a sibling field's value " "(e.g. Bioinformatics' page cap by article type)."),
    ] = None
    # For an ``int`` field, the inclusive bounds on the numeric value (e.g.
    # Bioinformatics' page count must be > 0, encoded as ``min_value`` 1).
    # ``None`` means unbounded on that side. Ignored for non-int fields.
    min_value: Annotated[
        Optional[int],
        PField(description="Inclusive lower bound on the numeric value of an `int` " "field."),
    ] = None
    max_value: Annotated[
        Optional[int],
        PField(description="Inclusive upper bound on the numeric value of an `int` " "field."),
    ] = None
    # For a file-upload field (``file`` or ``filelist``), the maximum size, in
    # megabytes, the portal allows for each individual uploaded file (e.g. Cell
    # caps each figure file at 20 MB). Every file the field names is checked
    # against this during validation. ``None`` means no per-file limit. Ignored
    # for non-file fields. Distinct from the venue-level ``max_upload_mb``,
    # which bounds the combined size of all uploads.
    max_file_size_mb: Annotated[
        Optional[float],
        PField(description="Maximum size, in megabytes, of each individual file " "uploaded for a `file`/`filelist` field. Distinct from the " "venue-level max_upload_mb."),
    ] = None
    # How an autofill engine may populate this field (see paperpush.autofill):
    #   "extract"  -- copy from the manuscript text (title, abstract, authors...)
    #   "classify" -- pick from `options` by judgment; always flagged for review
    #   "filemap"  -- assign a file from the manuscript directory
    #   "never"    -- policy/legal/workflow field; left for the author to set
    # An empty value means "infer from `type`" (see autofill.effective_role).
    autofill: Annotated[
        AutofillRole,
        PField(description="How an autofill engine may populate the field: extract | " "classify | filemap | never. Empty string means infer from `type`."),
    ] = ""
    # For a ``boolean`` field, whether it is a consent/confirmation checkbox that
    # must be affirmative ("yes") to submit, as opposed to an informational
    # yes/no question for which "no" is a valid answer. Only meaningful when
    # ``required`` is true; ignored for non-boolean fields.
    confirm: Annotated[
        bool,
        PField(description="For a required `boolean`, whether it is a consent " "checkbox that must be affirmative (vs. an informational yes/no " "question)."),
    ] = False
    # Conditional requirement: the id of another field in the same venue whose
    # non-empty value makes this field required. Layered on top of ``required``
    # (which is the unconditional case): a field is required when ``required`` is
    # true or when its ``required_if`` target carries a value. A ``boolean``
    # target is the exception -- it triggers only when answered yes, since "no"
    # is a value but means the dependent fields do not apply. Ignored when unset.
    required_if: Annotated[
        Optional[str],
        PField(description="Id of another field in the same venue; when that " "field has a non-empty value, this field becomes required. A `boolean` " 'target triggers only on an affirmative answer, since "no" means the ' "dependent fields do not apply. Combined with `required` (unconditional)."),
    ] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Field":
        return cls(
            id=data["id"],
            label=data["label"],
            type=data["type"],
            required=bool(data.get("required", False)),
            help=data.get("help", ""),
            options=(_load_options_file(data["options_file"]) if data.get("options_file") else data.get("options")),
            options_file=data.get("options_file"),
            options_recommended=bool(data.get("options_recommended", False)),
            default=data.get("default"),
            accept=data.get("accept"),
            fields=data.get("fields"),
            type_options=data.get("type_options"),
            min_count=data.get("min_count"),
            max_count=data.get("max_count"),
            word_count=data.get("word_count"),
            character_count=data.get("character_count"),
            min_character_count=data.get("min_character_count"),
            require_url=data.get("require_url"),
            max_words_before_refs=data.get("max_words_before_refs"),
            max_pages_before_refs=data.get("max_pages_before_refs"),
            main_text_end_headings=data.get("main_text_end_headings"),
            max_words=data.get("max_words"),
            max_pages=data.get("max_pages"),
            max_words_before_refs_by=_opt_conditional_limit(data.get("max_words_before_refs_by")),
            max_pages_before_refs_by=_opt_conditional_limit(data.get("max_pages_before_refs_by")),
            max_words_by=_opt_conditional_limit(data.get("max_words_by")),
            max_pages_by=_opt_conditional_limit(data.get("max_pages_by")),
            min_value=data.get("min_value"),
            max_value=data.get("max_value"),
            max_file_size_mb=data.get("max_file_size_mb"),
            autofill=data.get("autofill", ""),
            confirm=bool(data.get("confirm", False)),
            required_if=data.get("required_if"),
        )


@dataclass(frozen=True)
class VenueInfo:
    """What the master database (``venues.json``) records about a venue.

    The general metadata every venue carries whatever paperpush can do with it.
    The submission form lives in ``submission_forms.json`` (:class:`SubmissionForm`)
    and the author-guideline rules in ``manuscript_requirements.json``; a venue may
    have either, both, or (briefly, while being added) neither.
    """

    # ``slug`` is the database key (the top-level key in venues.json), carried
    # on the resolved object for convenience; it is not itself a key inside a
    # venue entry, so the generated schema omits it.
    slug: str
    name: Annotated[str, PField(description="Display name. Defaults to the slug when omitted.")] = ""
    full_name: Annotated[str, PField(description="Full, unabbreviated venue name.")] = ""
    venue_type: Annotated[
        Literal["preprint", "journal", "conference"],
        PField(description="Kind of venue: a preprint server, a journal, or a conference."),
    ] = "journal"
    # Double-blind review: reviewers must not learn who the authors are. When
    # set, ``paperpush validate`` scans the attachments (and any linked
    # anonymous.4open.science mirror) for identifying information; see
    # :mod:`paperpush.anonymity`.
    anonymous: Annotated[
        bool,
        PField(description="Anonymous (double-blind) review: validate checks the " "attached files and linked anonymous.4open.science repositories " "for information identifying the authors."),
    ] = False
    # Deprecated venues stay in the database (their sign-in/login wiring still
    # works via get_venue) but are hidden from list_venues, so they no longer
    # appear in `paperpush --venues` or the generated README.
    deprecated: Annotated[
        bool,
        PField(description="Hidden from listings but still reachable by get_venue."),
    ] = False
    site_url: Annotated[str, PField(description="Public homepage URL.")] = ""
    submission_guide: Annotated[str, PField(description="URL of the author/submission guide.")] = ""
    description: Annotated[str, PField(description="Short description of the venue.")] = ""


@dataclass(frozen=True)
class SubmissionForm:
    """A venue's submission form, as recorded in ``submission_forms.json``.

    The fields the portal asks for and the portal-level facts its runner needs.
    Only venues paperpush can submit to have one; a validate-only venue gets
    :func:`default_form` instead.
    """

    fields: Annotated[
        list[Field],
        PField(description="The fields this venue asks for during submission."),
    ] = dataclass_field(default_factory=list)
    submission_url: Annotated[str, PField(description="Submission/portal URL.")] = ""
    # Labels offered by a portal's per-file "file type" dropdown on its upload
    # step (e.g. ScholarOne's "Main Document", "Figure", ...). Venue-level
    # rather than per-field because the same dropdown applies to every file the
    # upload widget accepts. Empty for venues whose upload has no such control.
    file_type_options: Annotated[
        Optional[list[str]],
        PField(description="Labels offered by the portal's per-file 'file type' " "dropdown (venue-level because the same dropdown applies to every " "upload)."),
    ] = None
    # Maximum combined size, in megabytes, of all files uploaded for one
    # submission, as documented by the portal's author/submission guide. The
    # sum of every ``file`` and ``filelist`` upload is checked against this
    # during validation. None when the portal publishes no such limit.
    max_upload_mb: Annotated[
        Optional[float],
        PField(description="Maximum combined size, in megabytes, of all files in one " "submission."),
    ] = None
    # Slug this form inherits from, if any. An inheriting form takes the base
    # form's fields and other keys wholesale, then adds, overrides, or removes
    # individual fields (see ``inherits`` / ``removed_fields`` in
    # ``submission_forms.json`` and :func:`_resolve_entry`). This is purely
    # about the submission form. Whether a venue also submits *through*
    # another's portal -- sharing its runner, stored credentials, and saved
    # session -- is a separate, code-level fact declared next to the runners (see
    # :func:`paperpush.venues.submission_base`), so a venue can inherit
    # another's fields while still submitting through its own portal (e.g. the
    # Nature family: nature_methods inherits nature's form but has its own eJP
    # deployment), or share a portal without inheriting (and vice versa). Empty
    # for a standalone form.
    inherits: Annotated[
        str,
        PField(description="Slug this venue inherits its field schema from. " "Empty/absent for a standalone venue. Distinct from submission " "sharing (which portal/runner/login a venue submits through), which " "is defined in code, not here."),
    ] = ""


@dataclass(frozen=True)
class Venue(SubmissionForm, VenueInfo):
    """A venue: its master entry joined with its submission form.

    ``has_form`` is false for a venue with no entry in ``submission_forms.json``
    (validate-only); its ``fields`` are then :func:`default_form`'s, enough for a
    ``.sub`` to name the manuscript files that ``validate`` measures.
    """

    has_form: bool = True

    @property
    def supports_validate(self) -> bool:
        """Whether ``manuscript_requirements.json`` has rules for this venue."""
        from .requirements import get_requirements

        return get_requirements(self.slug) is not None

    @property
    def supports_submit(self) -> bool:
        """Whether this venue has a submission form and an installed runner."""
        from .venues import SLUG_TO_MODULE, submission_base

        return self.has_form and submission_base(self.slug) in SLUG_TO_MODULE

    @classmethod
    def from_dict(cls, slug: str, data: dict[str, Any], has_form: bool = True) -> "Venue":
        """Build a venue from its master entry merged with its (resolved) form."""
        # The slug is the canonical, case-insensitive identifier for a venue,
        # so it is always normalized to lowercase.
        slug = slug.lower()
        return cls(
            slug=slug,
            name=data.get("name", slug),
            full_name=data.get("full_name", ""),
            submission_url=data.get("submission_url", ""),
            venue_type=data.get("venue_type", "journal"),
            site_url=data.get("site_url", ""),
            submission_guide=data.get("submission_guide", ""),
            description=data.get("description", ""),
            file_type_options=data.get("file_type_options"),
            max_upload_mb=data.get("max_upload_mb"),
            fields=[Field.from_dict(f) for f in data.get("fields", [])],
            anonymous=bool(data.get("anonymous", False)),
            deprecated=bool(data.get("deprecated", False)),
            inherits=str(data.get("inherits", "") or "").lower(),
            has_form=has_form,
        )


# Keys of a master entry and of a form entry, for the loaders and for
# check_venue_data. ``removed_fields`` is a form-only loader directive.
INFO_KEYS = frozenset(f.name for f in dataclass_fields(VenueInfo)) - {"slug"}
FORM_KEYS = frozenset(f.name for f in dataclass_fields(SubmissionForm)) | {"removed_fields"}


def default_form(slug: str) -> dict[str, Any]:
    """The form a validate-only venue gets: just what ``validate`` reads.

    Field ids are the ones :mod:`paperpush.requirements_check` looks up by role
    (``manuscript_file``, ``figure_files``, ...), so the manuscript checks find
    the files without a portal-specific form. An ``article_type`` choice is
    added when the venue's requirements vary by article type, named after the
    requirements' ``article_type_field`` so :func:`paperpush.requirements.resolve`
    picks the selected type's limits.
    """
    from .requirements import get_requirements

    fields: list[dict[str, Any]] = [
        {"id": "title", "autofill": "extract", "label": "Manuscript title", "type": "text", "required": True, "help": "The full title of the manuscript."},
        {"id": "abstract", "autofill": "extract", "label": "Abstract", "type": "textarea", "required": True, "help": "The abstract as it appears in the manuscript."},
        {"id": "keywords", "autofill": "extract", "label": "Keywords", "type": "text", "help": "Optional. Comma-separated keywords, as listed in the manuscript."},
        {
            "id": "authors",
            "autofill": "extract",
            "label": "Authors",
            "type": "authorlist",
            "required": True,
            "help": "One author per line, in order. Format: Name | email | affiliation | ORCID | corresponding(yes/no). Mark exactly one author as the corresponding author.",
            "fields": ["name", "email?", "affiliation?", "orcid?", "corresponding"],
        },
    ]
    reqs = get_requirements(slug)
    if reqs is not None and reqs.article_types:
        fields.append(
            {
                "id": reqs.article_type_field or "article_type",
                "autofill": "classify",
                "label": "Article type",
                "type": "choice",
                "help": "Selects which of the author guidelines' per-article-type limits apply.",
                "options": list(reqs.article_types),
            }
        )
    fields += [
        {"id": "manuscript_file", "autofill": "filemap", "label": "Manuscript file", "type": "file", "required": True, "help": "Path to the manuscript (PDF, Word, or LaTeX source)."},
        {"id": "figure_files", "autofill": "filemap", "label": "Figure files", "type": "filelist", "help": "Optional. One figure file per line as 'path | label' (label optional)."},
        {"id": "table_files", "autofill": "filemap", "label": "Table files", "type": "filelist", "help": "Optional. One table file per line as 'path | label' (label optional)."},
        {"id": "supplementary_files", "autofill": "filemap", "label": "Supplementary files", "type": "filelist", "help": "Optional. One supplementary file per line as 'path | label' (label optional)."},
        {"id": "cover_letter", "autofill": "filemap", "label": "Cover letter", "type": "file", "help": "Optional. Path to the cover letter."},
    ]
    return {"fields": fields}


def _read_database(path: Path) -> dict[str, Any]:
    logger.debug("Loading venue database from %s", path)
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _source_copy(name: str) -> Optional[dict[str, Any]]:
    """The active source's copy of data file ``name``, or None to use the bundled one."""
    source = venue_data.active_source()
    if source.kind == "bundled":
        return None
    try:
        return _read_database(source.path(name))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s from %s (%s); using the bundled copy", name, source.root, exc)
        return None


@lru_cache(maxsize=1)
def _load_raw() -> dict[str, Any]:
    """The submission forms, keyed by slug (``submission_forms.json``)."""
    bundled = _read_database(FORMS_PATH)
    published = _source_copy(venue_data.FORMS_FILE)
    # A local override is the author's own data, used as is; a published copy
    # is merged venue by venue onto what this version can drive.
    if published is None:
        data = bundled
    elif venue_data.active_source().kind == "override":
        data = published
    else:
        data = merge_published(bundled, published)
    logger.info("Loaded %d submission form(s) (%s)", len(data), venue_data.active_source().describe())
    return data


@lru_cache(maxsize=1)
def _load_info() -> dict[str, Any]:
    """The master venue list, keyed by slug (``venues.json``)."""
    bundled = _read_database(DATABASE_PATH)
    published = _source_copy(venue_data.VENUES_FILE)
    if published is None:
        data = bundled
    elif venue_data.active_source().kind == "override":
        data = published
    else:
        data = merge_published_info(bundled, published, _source_copy(venue_data.FORMS_FILE) or {})
    logger.info("Loaded %d venue(s) from the master list", len(data))
    return data


def _field_shape(slug: str, raw: dict[str, Any]) -> list[tuple[str, str]]:
    """The (id, type) of each field of ``slug``, after inheritance, in order.

    This is what a venue's runner is written against: it reads fields by id and
    fills each according to its type. Everything else about a field -- label,
    help, options, limits, whether it is required -- is data the runner does not
    hard-code, so it can change without a code release.
    """
    return [(f.get("id", ""), f.get("type", "text")) for f in _resolve_entry(slug, raw).get("fields", [])]


def merge_published(bundled: dict[str, Any], published: dict[str, Any]) -> dict[str, Any]:
    """Overlay a published venue database onto the bundled one, venue by venue.

    A published entry replaces the bundled entry only when

    * the bundled database has the venue (a new venue needs its runner, which
      only a release can bring),
    * its fields keep the bundled shape (:func:`_field_shape`) -- adding,
      removing, renaming, reordering or retyping a field needs runner changes,
    * it resolves (e.g. its ``inherits`` base exists), and
    * any venue it inherits from is itself taken from the published copy, so an
      entry is never resolved against a mix of old and new bases.

    Every other venue keeps its bundled entry, and a note is logged so a user on
    an old version knows an upgrade would bring more.
    """
    accepted: set[str] = set()
    for slug in bundled:
        if slug not in published:
            continue
        try:
            if _field_shape(slug, published) == _field_shape(slug, bundled):
                accepted.add(slug)
        except KeyError as exc:
            logger.warning("Published venue data: %s does not resolve (%s); keeping the bundled entry", slug, exc)

    # Drop entries whose base was not accepted, until nothing changes (a chain
    # of inheritance can take several passes).
    changed = True
    while changed:
        changed = False
        for slug in sorted(accepted):
            base = str(published[slug].get("inherits", "") or "")
            if base and base not in accepted and base.lower() not in accepted:
                accepted.discard(slug)
                changed = True

    held_back = sorted(set(published) - accepted)
    if held_back:
        logger.info("Published venue data for %s needs a newer paperpush; using the bundled entries. " "Run 'pip install -U paperpush' to get them.", ", ".join(held_back))
    return {slug: published[slug] if slug in accepted else entry for slug, entry in bundled.items()}


@lru_cache(maxsize=1)
def _info_adapter() -> TypeAdapter:
    return TypeAdapter(VenueInfo)


def _info_problem(slug: str, entry: Any) -> Optional[str]:
    """Why this version cannot use the master-list ``entry``, or None."""
    if not isinstance(entry, dict):
        return f"{slug} is not an object"
    unknown = sorted(set(entry) - INFO_KEYS)
    if unknown:
        return f"{slug} has key(s) this version does not know: {', '.join(unknown)}"
    try:
        _info_adapter().validate_python({"slug": slug, **entry})
    except ValidationError as exc:
        return f"{slug} does not match this version's types ({exc.errors()[0]['msg']})"
    return None


def merge_published_info(bundled: dict[str, Any], published: dict[str, Any], published_forms: dict[str, Any]) -> dict[str, Any]:
    """Overlay a published master list onto the bundled one, venue by venue.

    A published entry is used when this version can read it (known keys and
    value types) and either the bundled list already has the venue or the venue
    is validate-only -- it has no published submission form. A new venue that
    can be submitted to needs its runner, which only a release can bring, so it
    is held back; a new validate-only venue needs nothing but data and is added.
    A bundled venue the published list omits keeps its bundled entry.
    """
    merged = dict(bundled)
    held_back: list[str] = []
    for slug, entry in published.items():
        problem = _info_problem(slug, entry)
        if problem:
            logger.info("Published venue list: %s; keeping the bundled entry", problem)
            held_back.append(slug)
        elif slug in bundled or slug not in published_forms:
            merged[slug] = entry
        else:
            held_back.append(slug)
    if held_back:
        logger.info("Published venue list entries for %s need a newer paperpush. " "Run 'pip install -U paperpush' to get them.", ", ".join(sorted(held_back)))
    return merged


def check_venue_data(root: Path) -> None:
    """Raise if the venue data in directory ``root`` does not load.

    Run on a downloaded copy before it replaces the cache: every master entry
    must be readable, every form must belong to a listed venue, resolve and
    build, every ``options_file`` it names must exist, and the manuscript
    requirements must load and name only listed venues.
    """
    from .requirements import check_requirements_file

    info = _read_database(root / venue_data.VENUES_FILE)
    if not isinstance(info, dict) or not info:
        raise ValueError("venues.json holds no venues")
    for slug, entry in info.items():
        problem = _info_problem(slug, entry)
        if problem:
            raise ValueError(f"venues.json: {problem}")
    forms = _read_database(root / venue_data.FORMS_FILE)
    if not isinstance(forms, dict):
        raise ValueError("submission_forms.json is not a JSON object")
    for slug in forms:
        if slug not in info:
            raise ValueError(f"submission_forms.json has a form for {slug!r}, which venues.json does not list")
        entry = _resolve_entry(slug, forms)
        unknown = sorted(set(forms[slug]) - FORM_KEYS)
        if unknown:
            raise ValueError(f"submission_forms.json: {slug} has unknown key(s) {', '.join(unknown)}")
        Venue.from_dict(slug, {**entry, "fields": []})
        for f in entry.get("fields", []):
            name = f.get("options_file")
            if name and not (root / venue_data.ASSETS_SUBDIR / name).is_file() and not (ASSETS_DIR / name).is_file():
                raise ValueError(f"{slug}.{f.get('id')} names a missing options_file {name!r}")
            Field.from_dict({k: v for k, v in f.items() if k != "options_file"})
    requirement_slugs = check_requirements_file(root / venue_data.REQUIREMENTS_FILE)
    unlisted = sorted(set(requirement_slugs) - set(info))
    if unlisted:
        raise ValueError(f"manuscript_requirements.json has rules for {', '.join(unlisted)}, which venues.json does not list")


def reload() -> None:
    """Re-resolve the venue data source and drop every cached load of it."""
    from . import requirements

    venue_data.reset()
    _load_raw.cache_clear()
    _load_info.cache_clear()
    _load_options_file.cache_clear()
    requirements._load_raw.cache_clear()


# Keys an inheriting entry uses to declare how it derives from its base rather
# than to describe a key of the resulting venue; they are consumed by
# _resolve_entry and not copied verbatim onto the merged venue.
_INHERIT_META_KEYS = ("inherits", "removed_fields")


def _merge_fields(base_fields: list[dict], overrides: list[dict], removed: list[str], slug: str) -> list[dict]:
    """Merge an inheriting venue's field edits onto its base venue's fields.

    The result starts from ``base_fields`` (in order) and applies, by field
    ``id``:

    * **modify** -- an override whose ``id`` matches an inherited field is merged
      key-by-key onto it, so an entry need only restate the keys it changes (e.g.
      ``{"id": "publication", "default": "Science Advances"}`` retargets just the
      default and keeps the inherited label, options, and help). A key set to
      ``null`` is dropped from the inherited field.
    * **add** -- an override whose ``id`` is new is appended as a full field
      definition, in the order listed after the inherited fields.
    * **remove** -- any id in ``removed_fields`` is dropped entirely.

    ``slug`` is used only for clear error messages.
    """
    merged: dict[str, dict] = {f["id"]: copy.deepcopy(f) for f in base_fields}
    order: list[str] = [f["id"] for f in base_fields]

    for fid in removed:
        if fid not in merged:
            raise KeyError(f"{slug!r} removes unknown field {fid!r} (not defined by its base)")
        del merged[fid]
        order.remove(fid)

    for override in overrides:
        fid = override.get("id")
        if not fid:
            raise KeyError(f"{slug!r} has a field override without an 'id'")
        if fid in merged:
            # Modify: layer the override's keys onto the inherited field; a key
            # whose value is null removes it (e.g. to clear an inherited default).
            for key, value in override.items():
                if value is None:
                    merged[fid].pop(key, None)
                else:
                    merged[fid][key] = value
        else:
            # Add: a brand-new field, appended after the inherited ones.
            merged[fid] = copy.deepcopy(override)
            order.append(fid)

    return [merged[fid] for fid in order]


def _resolve_entry(slug: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Return the effective venue data for ``slug``, expanding inheritance.

    A standalone entry is returned unchanged. An inheriting entry sets
    ``"inherits": "<base-slug>"`` and takes the base venue's fields, file
    types, portal URLs, and everything else wholesale, then customizes:

    * top-level keys it restates (typically ``name``, ``full_name``, and
      ``description``) override the base's;
    * its ``fields`` list adds new fields and modifies inherited ones by ``id``;
    * its ``removed_fields`` list drops inherited fields by ``id``.

    See :func:`_merge_fields` for the field-level rules. This is how the AAAS
    family preselects each venue's ``publication`` while sharing Science's
    submission format and runner, and how a future Nature or Cell sibling could
    tweak or drop a field or two without restating the whole schema.

    Raises :class:`KeyError` if the entry names a base venue not in ``raw`` or
    removes a field the base does not define.
    """
    data = raw[slug]
    base_slug = data.get("inherits")
    if not base_slug:
        return data
    base = raw.get(base_slug) or raw.get(base_slug.lower())
    if base is None:
        raise KeyError(f"{slug!r} inherits from unknown venue {base_slug!r}")
    merged = copy.deepcopy(base)
    merged["inherits"] = base_slug
    # Field edits are handled by _merge_fields; everything else is a plain
    # top-level override (name, full_name, description, submission_url, ...).
    for key, value in data.items():
        if key in _INHERIT_META_KEYS or key == "fields":
            continue
        merged[key] = value
    merged["fields"] = _merge_fields(
        base.get("fields", []),
        data.get("fields", []),
        data.get("removed_fields", []),
        slug,
    )
    return merged


def _build(slug: str, info: dict[str, Any], forms: dict[str, Any]) -> Venue:
    """The :class:`Venue` for master entry ``info[slug]``, joined with its form."""
    form_key = next((k for k in forms if k.lower() == slug.lower()), None)
    if form_key is None:
        form, has_form = default_form(slug), False
    else:
        form, has_form = _resolve_entry(form_key, forms), True
    return Venue.from_dict(slug, {**form, **{k: v for k, v in info[slug].items() if k in INFO_KEYS}}, has_form=has_form)


def list_venues(include_deprecated: bool = False) -> list[Venue]:
    """Return every venue in the master list, sorted by lowercase slug.

    Venues are keyed by their lowercase slug, so any case variants in the
    database (e.g. ``"bioRxiv"`` and ``"biorxiv"``) collapse to a single entry
    rather than being listed twice. The list includes validate-only and
    submit-only venues alike; see :attr:`Venue.supports_validate` and
    :attr:`Venue.supports_submit`.

    Deprecated venues are omitted unless ``include_deprecated`` is true. They
    remain reachable via ``get_venue`` so any wired-up login flow still works.
    """
    info, forms = _load_info(), _load_raw()
    by_slug: dict[str, Venue] = {}
    for slug in info:
        venue = _build(slug, info, forms)
        if venue.deprecated and not include_deprecated:
            continue
        by_slug.setdefault(venue.slug, venue)
    return sorted(by_slug.values(), key=lambda j: j.slug)


def get_venue(slug: str) -> Venue:
    """Return a single venue by slug (case-insensitive).

    Matching is done against the lowercase slug or display name, so callers may
    pass ``"biorxiv"``, ``"bioRxiv"``, or ``"BIORXIV"`` interchangeably.

    Raises ``KeyError`` if no venue matches.
    """
    info, forms = _load_info(), _load_raw()
    key = slug.lower()
    for candidate, data in info.items():
        if candidate.lower() == key or data.get("name", "").lower() == key:
            logger.debug("Resolved venue %r to %r", slug, candidate)
            return _build(candidate, info, forms)
    logger.debug("No venue matches %r", slug)
    raise KeyError(slug)
