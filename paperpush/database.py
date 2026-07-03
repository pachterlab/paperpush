"""Access to the venue requirements database.

The database is a single JSON file (``venues.json``) shipped with the
package. Each top-level key is a venue slug (e.g. ``"biorxiv"``) mapping to a
venue definition that lists the fields required for submission.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from pydantic import Field as PField

logger = logging.getLogger(__name__)

DATABASE_PATH = Path(__file__).with_name("venues.json")

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
        PField(description="Sub-field names for a structured field such as " "`authorlist`."),
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
    # e.g. an abstract capped at 250 words. ``None`` means no limit on that
    # measure. Ignored for non-text fields.
    word_count: Annotated[
        Optional[int],
        PField(description="Maximum length in words for a `text`/`textarea` value."),
    ] = None
    character_count: Annotated[
        Optional[int],
        PField(description="Maximum length in characters for a `text`/`textarea` " "value."),
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
    # true or when its ``required_if`` target carries a value. Ignored when unset.
    required_if: Annotated[
        Optional[str],
        PField(description="Id of another field in the same venue; when that " "field has a non-empty value, this field becomes required. Combined with " "`required` (unconditional)."),
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
            require_url=data.get("require_url"),
            max_words_before_refs=data.get("max_words_before_refs"),
            max_pages_before_refs=data.get("max_pages_before_refs"),
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
class Venue:
    """A supported venue and the fields it requires."""

    # ``slug`` is the database key (the top-level key in venues.json), carried
    # on the resolved object for convenience; it is not itself a key inside a
    # venue entry, so the generated schema omits it.
    slug: str
    # ``name`` and ``fields`` are optional in venues.json -- ``from_dict``
    # defaults a missing name to the slug and missing fields to an empty list --
    # so they carry dataclass defaults, which keeps them optional in the schema.
    name: Annotated[str, PField(description="Display name. Defaults to the slug when omitted.")] = ""
    fields: Annotated[
        list[Field],
        PField(description="The fields this venue asks for during submission."),
    ] = dataclass_field(default_factory=list)
    full_name: Annotated[str, PField(description="Full, unabbreviated venue name.")] = ""
    submission_url: Annotated[str, PField(description="Submission/portal URL.")] = ""
    venue_type: Annotated[
        Literal["preprint", "venue", "conference"],
        PField(description="Kind of venue: a preprint server, a venue, or a conference."),
    ] = "venue"
    site_url: Annotated[str, PField(description="Public homepage URL.")] = ""
    submission_guide: Annotated[str, PField(description="URL of the author/submission guide.")] = ""
    description: Annotated[str, PField(description="Short description of the venue.")] = ""
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
    # Deprecated venues stay in the database (their sign-in/login wiring still
    # works via get_venue) but are hidden from list_venues, so they no longer
    # appear in `paperpush --venues` or the generated README.
    deprecated: Annotated[
        bool,
        PField(description="Hidden from listings but still reachable by get_venue."),
    ] = False
    # Slug this venue inherits its *schema* from, if any. An inheriting venue
    # takes the base venue's fields and other top-level keys wholesale, then
    # adds, overrides, or removes individual fields (see ``inherits`` /
    # ``removed_fields`` in ``venues.json`` and :func:`_resolve_entry`). This is
    # purely about the submission form. Whether a venue also submits *through*
    # another's portal -- sharing its runner, stored credentials, and saved
    # session -- is a separate, code-level fact declared next to the runners (see
    # :func:`paperpush.venues.submission_base`), so a venue can inherit
    # another's fields while still submitting through its own portal (e.g. the
    # Nature family: nature_methods inherits nature's schema but has its own eJP
    # deployment), or share a portal without inheriting (and vice versa). Empty
    # for a standalone venue.
    inherits: Annotated[
        str,
        PField(description="Slug this venue inherits its field schema from. " "Empty/absent for a standalone venue. Distinct from submission " "sharing (which portal/runner/login a venue submits through), which " "is defined in code, not here."),
    ] = ""

    @classmethod
    def from_dict(cls, slug: str, data: dict[str, Any]) -> "Venue":
        # The slug is the canonical, case-insensitive identifier for a venue,
        # so it is always normalized to lowercase.
        slug = slug.lower()
        return cls(
            slug=slug,
            name=data.get("name", slug),
            full_name=data.get("full_name", ""),
            submission_url=data.get("submission_url", ""),
            venue_type=data.get("venue_type", "venue"),
            site_url=data.get("site_url", ""),
            submission_guide=data.get("submission_guide", ""),
            description=data.get("description", ""),
            file_type_options=data.get("file_type_options"),
            max_upload_mb=data.get("max_upload_mb"),
            fields=[Field.from_dict(f) for f in data.get("fields", [])],
            deprecated=bool(data.get("deprecated", False)),
            inherits=str(data.get("inherits", "") or "").lower(),
        )


@lru_cache(maxsize=1)
def _load_raw() -> dict[str, Any]:
    logger.debug("Loading venue database from %s", DATABASE_PATH)
    with DATABASE_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    logger.info("Loaded %d venue entr(y/ies) from the database", len(data))
    return data


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


def list_venues(include_deprecated: bool = False) -> list[Venue]:
    """Return supported venues, sorted by lowercase slug.

    Venues are keyed by their lowercase slug, so any case variants in the
    database (e.g. ``"bioRxiv"`` and ``"biorxiv"``) collapse to a single entry
    rather than being listed twice.

    Deprecated venues are omitted unless ``include_deprecated`` is true. They
    remain reachable via ``get_venue`` so any wired-up login flow still works.
    """
    raw = _load_raw()
    by_slug: dict[str, Venue] = {}
    for slug in raw:
        venue = Venue.from_dict(slug, _resolve_entry(slug, raw))
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
    raw = _load_raw()
    key = slug.lower()
    for candidate, data in raw.items():
        if candidate.lower() == key or data.get("name", "").lower() == key:
            logger.debug("Resolved venue %r to %r", slug, candidate)
            return Venue.from_dict(candidate, _resolve_entry(candidate, raw))
    logger.debug("No venue matches %r", slug)
    raise KeyError(slug)
