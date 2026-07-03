"""Populate a ``.sub`` file from values extracted from a manuscript directory.

This module is the deterministic core shared by both autofill front-ends: the
``paperpush autofill`` command (which extracts values with the Anthropic
API) and the Claude Code skill (where Claude reads the files and supplies the
values). Neither front-end writes the ``.sub`` itself; both hand a set of
proposed ``{field id -> value}`` extractions to :func:`autofill`, which decides
what may be written and writes it surgically through the same
:mod:`paperpush.subfile` helpers a hand edit would use.

Two gates protect the file from a careless or over-reaching extractor:

* **Role gate.** Every field has an autofill *role* (``extract``, ``classify``,
  ``filemap``, or ``never``; see :func:`effective_role`). A ``never`` field --
  licenses, consent attestations, workflow flags -- is left at its template
  default and reported, never written. This is the single most important
  safety rule and it lives here in Python, not in any prompt.
* **Confidence gate.** Each proposal carries a confidence (``high`` / ``medium``
  / ``low``). Anything below ``min_confidence`` is skipped. A written value that
  is a ``classify`` field, or is below ``high`` confidence, is flagged for the
  author to review rather than presented as settled.

Every written field is run back through :func:`paperpush.validate.validate`
so an autofilled file carries the same guarantees as one filled in by hand.

The API extraction engine (``paperpush autofill --engine api``) lives in the
"API extraction engine" section at the bottom of this module: it reads the
manuscript files and asks the Anthropic API to propose values, returning the same
:class:`Extraction` the manual engine produces so both flow through the identical
gates above. ``anthropic`` is an optional dependency, imported lazily, so the
deterministic core works without it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from dataclasses import field as _dc_field
from pathlib import Path

from .database import Field, Venue
from .manuscript import docx_to_text
from .subfile import _MULTILINE_TYPES, parse, replace_block, replace_scalar
from .validate import Issue, validate

logger = logging.getLogger(__name__)

# Autofill roles. A field's role is read from its ``autofill`` attribute, or
# inferred from its ``type`` when that is blank (see :func:`effective_role`).
EXTRACT = "extract"
CLASSIFY = "classify"
FILEMAP = "filemap"
NEVER = "never"
_ROLES = {EXTRACT, CLASSIFY, FILEMAP, NEVER}

# Fallback role per field type when a field declares no explicit ``autofill``.
_ROLE_BY_TYPE = {
    "file": FILEMAP,
    "filelist": FILEMAP,
    "choice": CLASSIFY,
    "multichoice": CLASSIFY,
    "boolean": NEVER,
}

# Confidence levels, ordered low -> high.
_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
DEFAULT_CONFIDENCE = "medium"

# What happened to each field, for the summary the front-ends print.
FILLED = "filled"  # written, high confidence, not a judgment call
REVIEW = "review"  # written but the author should confirm it
SKIPPED_POLICY = "skipped_policy"  # a ``never`` field; left at its default
SKIPPED_LOW = "skipped_low"  # below the confidence threshold; not written
SKIPPED_EMPTY = "skipped_empty"  # proposal had no value to write
UNKNOWN = "unknown"  # not a field in this venue's template


def effective_role(field: Field) -> str:
    """Return the autofill role for ``field``.

    Uses the field's explicit ``autofill`` value when set and recognized,
    otherwise infers a role from its ``type`` (files map, choices classify,
    booleans are never touched, everything else is extracted).
    """
    if field.autofill in _ROLES:
        return field.autofill
    if field.autofill:
        logger.warning("Field %r has unknown autofill role %r; inferring from " "type %r", field.id, field.autofill, field.type)
    return _ROLE_BY_TYPE.get(field.type, EXTRACT)


def field_schema(venue: Venue) -> list[dict]:
    """Return each field's id, role, and constraints as plain dicts.

    This is the authoritative description an extractor (the API engine or the
    Claude skill) needs to know what to fill and how: which fields to extract
    versus classify versus map to files versus leave alone, plus the closed
    option sets and accepted file types. Built straight from the venue
    definition so the roles never drift from ``venues.json``.
    """
    out: list[dict] = []
    for f in venue.fields:
        out.append(
            {
                "id": f.id,
                "label": f.label,
                "type": f.type,
                "role": effective_role(f),
                "required": f.required,
                "help": f.help,
                "options": f.options,
                "type_options": f.type_options,
                "accept": f.accept,
                "min_count": f.min_count,
                "max_count": f.max_count,
                "word_count": f.word_count,
                "character_count": f.character_count,
            }
        )
    return out


@dataclass(frozen=True)
class Proposal:
    """One extracted value an engine proposes for a field."""

    id: str
    value: str
    confidence: str = DEFAULT_CONFIDENCE
    source: str = ""


@dataclass(frozen=True)
class Extraction:
    """The full set of proposals an engine produced for a manuscript."""

    fields: list[Proposal] = _dc_field(default_factory=list)
    # Fields the engine deliberately left for the author, with a reason.
    unfilled: list[tuple[str, str]] = _dc_field(default_factory=list)


@dataclass(frozen=True)
class Outcome:
    """What :func:`autofill` decided to do with one field."""

    id: str
    label: str
    action: str  # one of FILLED / REVIEW / SKIPPED_* / UNKNOWN
    value: str = ""
    confidence: str = ""
    source: str = ""
    note: str = ""


@dataclass(frozen=True)
class AutofillResult:
    """The rewritten ``.sub`` text plus a record of every decision."""

    text: str
    outcomes: list[Outcome]
    issues: list[Issue]

    def _by_action(self, *actions: str) -> list[Outcome]:
        wanted = set(actions)
        return [o for o in self.outcomes if o.action in wanted]

    @property
    def filled(self) -> list[Outcome]:
        return self._by_action(FILLED)

    @property
    def review(self) -> list[Outcome]:
        return self._by_action(REVIEW)

    @property
    def skipped(self) -> list[Outcome]:
        return self._by_action(SKIPPED_POLICY, SKIPPED_LOW, SKIPPED_EMPTY, UNKNOWN)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.is_error]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if not i.is_error]


def parse_extraction(data: dict) -> Extraction:
    """Build an :class:`Extraction` from the shared JSON schema.

    The schema is::

        {"fields":   [{"id", "value", "confidence"?, "source"?}, ...],
         "unfilled": [{"id", "reason"?}, ...]}

    Both front-ends emit this shape, so the same loader serves the API engine
    and the skill's ``--values`` file.
    """
    proposals: list[Proposal] = []
    for item in data.get("fields", []) or []:
        proposals.append(
            Proposal(
                id=str(item["id"]),
                value="" if item.get("value") is None else str(item.get("value")),
                confidence=str(item.get("confidence") or DEFAULT_CONFIDENCE).lower(),
                source=str(item.get("source") or ""),
            )
        )
    unfilled = [(str(u["id"]), str(u.get("reason") or "")) for u in (data.get("unfilled", []) or [])]
    return Extraction(fields=proposals, unfilled=unfilled)


def _resolve_one_path(path_str: str, manuscript_dir: Path | None) -> str:
    """Make a single file path resolvable from the current directory.

    If the path as given does not exist but the same name does inside
    ``manuscript_dir``, rewrite it to that location so the file checks in
    :mod:`paperpush.validate` (which resolves paths against the working
    directory) find it. Otherwise the value is left untouched for validation
    to flag.
    """
    path_str = path_str.strip()
    if not path_str or manuscript_dir is None:
        return path_str
    if Path(path_str).expanduser().exists():
        return path_str
    candidate = manuscript_dir / path_str
    if candidate.exists():
        return str(candidate)
    # Also try just the basename, in case the engine quoted a longer path.
    candidate = manuscript_dir / Path(path_str).name
    if candidate.exists():
        return str(candidate)
    return path_str


def _resolve_filemap(field: Field, value: str, manuscript_dir: Path | None) -> str:
    """Resolve the file path(s) in a ``filemap`` field's value.

    For a ``file`` field the whole value is a path. For a ``filelist`` each
    non-blank line is ``path | extra | ...``; only the leading path segment is
    resolved, the remaining ``| ...`` columns (label, type, link text) are
    preserved as written.
    """
    if field.type == "file":
        return _resolve_one_path(value, manuscript_dir)

    out_lines: list[str] = []
    for line in value.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        parts[0] = _resolve_one_path(parts[0], manuscript_dir)
        out_lines.append(" | ".join(parts) if len(parts) > 1 else parts[0])
    return "\n".join(out_lines)


def _write_field(text: str, field: Field, value: str) -> str:
    """Write ``value`` into ``field`` using the right surgical helper."""
    if field.type in _MULTILINE_TYPES:
        return replace_block(text, field.id, value)
    return replace_scalar(text, field.id, value)


def autofill(
    text: str,
    venue: Venue,
    extraction: Extraction,
    manuscript_dir: str | Path | None = None,
    min_confidence: str = "low",
) -> AutofillResult:
    """Apply ``extraction`` to the ``.sub`` ``text`` for ``venue``.

    Returns an :class:`AutofillResult` holding the rewritten text and an
    :class:`Outcome` for every proposed field. The original ``text`` is never
    mutated; fields that are skipped keep whatever value they already carried
    (typically the template default).

    ``min_confidence`` is the floor below which a proposal is not written at all
    (``"low"`` writes everything proposed; ``"high"`` writes only sure things).
    ``manuscript_dir`` is used to resolve relative file paths in ``filemap``
    fields against the directory the files actually live in.
    """
    by_id = {f.id: f for f in venue.fields}
    floor = _CONFIDENCE_ORDER.get(min_confidence.lower(), 0)
    mdir = Path(manuscript_dir).expanduser() if manuscript_dir is not None else None

    result = text
    outcomes: list[Outcome] = []

    for prop in extraction.fields:
        field = by_id.get(prop.id)
        if field is None:
            outcomes.append(Outcome(prop.id, prop.id, UNKNOWN, value=prop.value, confidence=prop.confidence, source=prop.source, note=f"not a field in the {venue.slug} template"))
            continue

        role = effective_role(field)
        if role == NEVER:
            outcomes.append(Outcome(field.id, field.label, SKIPPED_POLICY, source=prop.source, note="policy/consent field -- left at its default for you to set"))
            continue

        confidence = prop.confidence if prop.confidence in _CONFIDENCE_ORDER else DEFAULT_CONFIDENCE

        value = prop.value
        if role == FILEMAP:
            value = _resolve_filemap(field, value, mdir)

        if not value.strip():
            outcomes.append(Outcome(field.id, field.label, SKIPPED_EMPTY, confidence=confidence, source=prop.source, note="no value to write"))
            continue

        if _CONFIDENCE_ORDER[confidence] < floor:
            outcomes.append(Outcome(field.id, field.label, SKIPPED_LOW, value=value, confidence=confidence, source=prop.source, note=f"{confidence} confidence is below the {min_confidence} " "threshold"))
            continue

        result = _write_field(result, field, value)

        needs_review = role == CLASSIFY or confidence != "high"
        if role == CLASSIFY:
            note = "classified from the manuscript -- confirm it is correct"
        elif confidence != "high":
            note = f"{confidence} confidence -- please verify"
        else:
            note = ""
        outcomes.append(Outcome(field.id, field.label, REVIEW if needs_review else FILLED, value=value, confidence=confidence, source=prop.source, note=note))

    issues = validate(parse(result), venue)
    logger.info("autofill %s: %d filled, %d to review, %d skipped, %d " "validation issue(s)", venue.slug, sum(1 for o in outcomes if o.action == FILLED), sum(1 for o in outcomes if o.action == REVIEW), sum(1 for o in outcomes if o.action.startswith("skipped") or o.action == UNKNOWN), len(issues))
    return AutofillResult(text=result, outcomes=outcomes, issues=issues)


# ---------------------------------------------------------------------------
# API extraction engine (``paperpush autofill --engine api``)
#
# The second autofill front-end (the Claude Code skill is the first): it reads
# the manuscript files and asks the Anthropic API to propose field values,
# returning the same ``Extraction`` the manual engine produces so both flow
# through the deterministic gates above. ``anthropic`` is an optional dependency
# (``pip install paperpush[autofill]``), imported lazily below.
# ---------------------------------------------------------------------------


DEFAULT_MODEL = "claude-opus-4-8"


class AutofillApiError(RuntimeError):
    """A problem preparing or running the API extraction (not a model refusal)."""


@dataclass(frozen=True)
class DocumentInput:
    """One manuscript document to show the model, with a role label."""

    label: str  # e.g. "manuscript", "title_page", "supplement"
    path: Path


def _docx_text(path: Path) -> str:
    """Extract visible paragraph text from a ``.docx``, raising on failure.

    Delegates to the standard-library extractor in :mod:`paperpush.manuscript`
    (the single implementation, which also handles tabs and XML entities) and
    turns its ``None`` failure into an :class:`AutofillApiError` so the API engine
    surfaces an unreadable document instead of sending the model empty text.
    """
    text = docx_to_text(path)
    if text is None:
        raise AutofillApiError(f"could not read {path.name}")
    return text.strip()


def _read_text(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        return _docx_text(path)
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raise AutofillApiError(f"could not read {path.name}: {exc}") from exc


def _document_blocks(documents: list[DocumentInput]) -> list[dict]:
    """Build the user-content blocks for the manuscript documents.

    PDFs become base64 ``document`` blocks; other formats are extracted to text
    and wrapped in a labelled text block.
    """
    blocks: list[dict] = []
    for doc in documents:
        if not doc.path.is_file():
            raise AutofillApiError(f"{doc.label} file not found: {doc.path}")
        if doc.path.suffix.lower() == ".pdf":
            data = base64.standard_b64encode(doc.path.read_bytes()).decode("ascii")
            blocks.append(
                {
                    "type": "document",
                    "title": f"{doc.label}: {doc.path.name}",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": data,
                    },
                }
            )
        else:
            text = _read_text(doc.path)
            blocks.append(
                {
                    "type": "text",
                    "text": f"=== {doc.label} ({doc.path.name}) ===\n{text}",
                }
            )
    return blocks


def _extraction_schema(field_ids: list[str]) -> dict:
    """JSON schema forcing the model's output into the shared extraction shape."""
    return {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": field_ids},
                        "value": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "source": {"type": "string"},
                    },
                    "required": ["id", "value", "confidence", "source"],
                    "additionalProperties": False,
                },
            },
            "unfilled": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": field_ids},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["fields", "unfilled"],
        "additionalProperties": False,
    }


def _field_brief(venue: Venue) -> tuple[list[dict], list[str]]:
    """The requested (non-``never``) fields as a brief, plus their ids."""
    brief: list[dict] = []
    for f in field_schema(venue):
        if f["role"] == NEVER:
            continue
        entry = {k: f[k] for k in ("id", "label", "type", "role", "required", "help")}
        if f["options"]:
            entry["options"] = f["options"]
        if f["type_options"]:
            entry["file_types"] = f["type_options"]
        if f["accept"]:
            entry["accepts"] = f["accept"]
        for key in ("min_count", "max_count", "word_count", "character_count"):
            if f.get(key) is not None:
                entry[key] = f[key]
        brief.append(entry)
    return brief, [e["id"] for e in brief]


_SYSTEM = "You prepare academic venue submissions. Read the attached manuscript " "documents and propose values for the listed submission fields. Rules: " "(1) Extract values that appear in the text verbatim where possible. " "(2) For a 'classify' field, choose exactly one of its options. " "(3) For a 'filemap' field, assign one or more file paths from the directory " "listing, given relative to the manuscript directory; one per line, using " "the column format described in the field's help. " "(4) Never invent emails, ORCID iDs, DOIs, funders, or licenses that are not " "present in the documents -- leave a subfield blank instead. " "(5) Set confidence honestly: 'high' only for verbatim copies or unambiguous " "file matches, 'medium' for inference or classification, 'low' for guesses. " "(6) Put any field you cannot fill in 'unfilled' with a brief reason. " "Authors use the format 'Name | email | affiliation | ORCID | corresponding' " "with exactly one corresponding author marked 'yes'."


def _build_prompt(venue: Venue, documents: list[DocumentInput], file_listing: list[str]) -> tuple[str, list[dict], dict, list[str]]:
    brief, field_ids = _field_brief(venue)
    content = _document_blocks(documents)
    instructions = f"Target venue: {venue.full_name or venue.name} " f"(slug: {venue.slug}).\n\n" "Files available in the manuscript directory (use these relative paths " "for 'filemap' fields):\n" + "\n".join(f"  {p}" for p in file_listing) + "\n\nFields to fill (JSON):\n" + json.dumps(brief, indent=2) + "\n\nReturn the extraction now."
    content.append({"type": "text", "text": instructions})
    schema = _extraction_schema(field_ids)
    return _SYSTEM, content, schema, field_ids


def extract_via_api(
    venue: Venue,
    documents: list[DocumentInput],
    file_listing: list[str],
    model: str = DEFAULT_MODEL,
    max_tokens: int = 16000,
) -> Extraction:
    """Ask the Anthropic API to propose field values; return an Extraction.

    Raises :class:`AutofillApiError` for setup problems (missing dependency, no
    API key, unreadable file) and for a model refusal.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise AutofillApiError("the 'api' engine needs the anthropic package; install it with " "'pip install paperpush[autofill]'.") from exc

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise AutofillApiError("no API key found; set ANTHROPIC_API_KEY (or use '--engine manual' " "with the Claude skill).")

    system, content, schema, _ = _build_prompt(venue, documents, file_listing)
    logger.info("autofill api: calling %s for %s (%d document(s), %d field(s))", model, venue.slug, len(documents), len(schema["properties"]["fields"]["items"]["properties"]["id"]["enum"]))

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.APIError as exc:
        raise AutofillApiError(f"the API request failed: {exc}") from exc

    if response.stop_reason == "refusal":
        raise AutofillApiError("the model declined to process this request " f"({getattr(response.stop_details, 'category', None) or 'refusal'}).")

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AutofillApiError(f"the model returned output that was not valid JSON: {exc}") from exc

    logger.info("autofill api: %s proposed %d field(s), %d unfilled (usage: " "%s in / %s out)", venue.slug, len(data.get("fields", [])), len(data.get("unfilled", [])), response.usage.input_tokens, response.usage.output_tokens)
    return parse_extraction(data)
