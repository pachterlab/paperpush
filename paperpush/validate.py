"""Pre-submission checks for a filled-in ``.sub`` file.

``validate`` takes a parsed :class:`~paperpush.subfile.SubFile` plus its
:class:`~paperpush.database.Venue` definition and returns a list of
:class:`Issue` objects. Errors must be fixed before a submission can proceed;
warnings are advisory. The checks here are deliberately venue-agnostic and
driven by the field metadata in the database.

``validate_report`` runs the same checks through a :class:`Checks` collector
and returns a :class:`~paperpush.report.ValidationReport` recording every
check, including the ones that passed and the ones that could not run or need
an AI reader (see :mod:`paperpush.report` and :mod:`paperpush.ai_validate`).

Two layers of checking run, both reporting through the same :class:`Issue`
list so the author sees one combined, actionable summary:

* A pydantic model built from the venue's field schema enforces the value
  rules encoded in ``submission_forms.json``: a ``choice`` field's value must be one of
  its ``options``; a ``filelist``'s per-line file-type subfield must be one of
  its ``type_options``; a ``boolean`` must read as yes/no; and no unknown field
  ids are allowed.
* Hand-written checks cover what a schema cannot: required-but-empty fields,
  files that must exist on disk (with PDF sanity), and author-list shape.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from pydantic import AfterValidator, BeforeValidator, ConfigDict, ValidationError, create_model

from .database import Field, Venue, accepted_options, options_command
from .subfile import SubFile

logger = logging.getLogger(__name__)

ERROR = "error"
WARNING = "warning"

# Below this DPI, raster figures tend to look soft in print.
MIN_FIGURE_DPI = 300
# A manuscript PDF smaller than this is suspiciously empty.
MIN_PDF_BYTES = 1024
# Bytes per megabyte. Portals' "MB" upload limits are interpreted as binary
# megabytes (MiB), matching how most file managers report file sizes.
BYTES_PER_MB = 1024 * 1024

# Tokens accepted for boolean fields, in either direction.
_TRUE = {"yes", "y", "true", "1", "on"}
_FALSE = {"no", "n", "false", "0", "off"}


@dataclass(frozen=True)
class Issue:
    level: str  # ERROR or WARNING
    field: str  # field id the issue concerns ("" for file-level)
    message: str
    # Id of the check that raised it (see paperpush.report); stamped by the
    # check runner, so producers need not set it.
    check: str = ""
    # The message says a rule could not be checked (an unreadable file, a
    # missing tool), rather than that it was broken.
    unchecked: bool = False

    @property
    def is_error(self) -> bool:
        return self.level == ERROR


def unchecked(field: str, message: str) -> Issue:
    """A WARNING saying a rule went unchecked, and why."""
    return Issue(WARNING, field, message, unchecked=True)


def _truthy_bool(value: str) -> bool:
    return value.strip().lower() in _TRUE


def _falsey_bool(value: str) -> bool:
    return value.strip().lower() in _FALSE


# The legacy author column order, used by venues that don't declare their own
# ``fields`` list. Most venues follow this; Bioinformatics (ScholarOne) is the
# exception and declares its own order in submission_forms.json. The ``?`` suffixes mark
# the optional columns (see ``_subfield_specs``): only a name is strictly
# required, matching the historical behaviour for venues that omit ``fields``.
DEFAULT_AUTHOR_FIELDS = ["name", "email?", "affiliation?", "orcid?", "corresponding"]


def _subfield_specs(fields: list[str]) -> list[tuple[str, bool]]:
    """Split a structured field's column list into ``(name, required)`` pairs.

    A trailing ``?`` on a column name marks it optional; every other column is
    required -- it must be present and non-empty on each item line. The marker
    is stripped from the returned name, so callers always work with the clean
    column name (``"department?"`` -> ``("department", False)``). This is the
    single place the ``?`` convention is interpreted, shared by ``parse_authors``
    (which only needs the names), ``_check_authors``, and ``_check_subfields``.
    """
    specs: list[tuple[str, bool]] = []
    for col in fields:
        if col.endswith("?"):
            specs.append((col[:-1], False))
        else:
            specs.append((col, True))
    return specs


def parse_authors(value: str, fields: list[str] | None = None) -> list[dict]:
    """Parse an author block into a list of dicts.

    Each non-empty line is the ``|``-delimited columns named by ``fields``, in
    order (default: ``Name | email | affiliation | ORCID | corresponding``).
    ``fields`` comes from the venue's ``authorlist`` definition so a venue
    can use a different column set -- Bioinformatics, for instance, is
    ``email | prefix | name | institution | country | city | corresponding``.
    Missing trailing columns are tolerated. The ``corresponding`` column is
    coerced to a bool; every other column is kept as its trimmed string. A
    ``?`` suffix on a column name (an optional-column marker, see
    ``_subfield_specs``) is stripped, so the returned dict keys are always the
    clean names.
    """
    if fields is None:
        fields = DEFAULT_AUTHOR_FIELDS
    names = [name for name, _ in _subfield_specs(fields)]
    authors: list[dict] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        parts += [""] * (len(names) - len(parts))
        authors.append({name: (_truthy_bool(parts[i]) if name == "corresponding" else parts[i]) for i, name in enumerate(names)})
    return authors


def _check_pdf(path: Path, field_id: str) -> list[Issue]:
    """Lightweight, dependency-free sanity checks on a PDF manuscript."""
    issues: list[Issue] = []
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [Issue(ERROR, field_id, f"cannot read {path}: {exc}")]

    if not data.startswith(b"%PDF-"):
        issues.append(
            Issue(
                ERROR,
                field_id,
                f"{path.name} does not look like a valid PDF " "(missing %PDF header)",
            )
        )
        return issues
    if len(data) < MIN_PDF_BYTES:
        issues.append(
            Issue(
                WARNING,
                field_id,
                f"{path.name} is only {len(data)} bytes; it may be empty",
            )
        )
    # Rough page count: count page objects. Good enough to flag a 0-page file.
    pages = data.count(b"/Type /Page") + data.count(b"/Type/Page")
    if pages == 0:
        # PDF 1.5+ can keep its page objects in compressed object streams.
        try:
            from pypdf import PdfReader

            pages = len(PdfReader(str(path)).pages)
        except Exception:  # noqa: BLE001 -- unreadable: leave the count at 0
            pages = 0
    if pages == 0:
        issues.append(Issue(WARNING, field_id, f"could not detect any pages in {path.name}"))
    return issues


def _check_file_field(field: Field, raw: str) -> list[Issue]:
    issues: list[Issue] = []
    path = Path(raw.strip()).expanduser()
    if not path.exists():
        issues.append(Issue(ERROR, field.id, f"file not found: {path}"))
        return issues
    if not path.is_file():
        issues.append(Issue(ERROR, field.id, f"not a regular file: {path}"))
        return issues
    if field.accept:
        if path.suffix.lower() not in {s.lower() for s in field.accept}:
            issues.append(
                Issue(
                    WARNING,
                    field.id,
                    f"{path.name} has extension '{path.suffix}'; " f"{field.label} expects one of {', '.join(field.accept)}",
                )
            )
    if field.max_file_size_mb:
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        if size is not None and size > field.max_file_size_mb * BYTES_PER_MB:
            issues.append(
                Issue(
                    ERROR,
                    field.id,
                    f"{path.name} is {size / BYTES_PER_MB:.1f} MB, over the " f"{field.max_file_size_mb:g} MB per-file limit for {field.label}",
                )
            )
    if path.suffix.lower() == ".pdf":
        issues.extend(_check_pdf(path, field.id))
    return issues


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError("expected a yes/no value (e.g. yes, no, true, false)")


def _type_options_validator(field: Field):
    """Reject a filelist line whose file-type subfield is not an allowed type.

    Each line is ``path | type | ...``; the ``type`` subfield (column 2), when
    given, must be one of the field's ``type_options``.
    """
    allowed = {opt.lower() for opt in (field.type_options or [])}

    def check(value: str) -> str:
        for lineno, line in enumerate(value.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2 and parts[1] and parts[1].lower() not in allowed:
                raise ValueError(f"line {lineno}: file type '{parts[1]}' is not allowed; " f"choose one of: {', '.join(field.type_options or [])}")
        return value

    return AfterValidator(check)


def _int_validator(field: Field):
    """Reject a non-integer value or one outside a field's inclusive bounds."""

    def check(value: str) -> str:
        try:
            number = int(value.strip())
        except ValueError:
            raise ValueError(f"'{value}' is not a whole number")
        if field.min_value is not None and number < field.min_value:
            raise ValueError(f"must be at least {field.min_value} (got {number})")
        if field.max_value is not None and number > field.max_value:
            raise ValueError(f"must be at most {field.max_value} (got {number})")
        return value

    return AfterValidator(check)


def _annotation_for(field: Field, custom_validator=None):
    """Map one venue field to the pydantic type used to validate its value.

    A ``choice`` field's closed option set is taken straight from its ``options``
    in ``submission_forms.json`` and enforced as a ``Literal``, so the allowed values live
    in one place (the database) and are not duplicated in this module. When the
    field sets ``options_recommended`` the list is advisory instead (the portal
    offers an "Other" free-text entry), so it validates as a plain string and an
    off-list value is only warned about in :func:`validate`.

    ``custom_validator``, when given, is a venue-specific check contributed by
    the venue's runner module (see
    :func:`paperpush.venues.get_field_validators`) -- a callable that takes
    the field's raw string and returns it, or raises ``ValueError`` with a clear
    message. It is layered on top of the base type as an ``AfterValidator`` so a
    venue can enforce rules the generic field metadata cannot express (for
    example that Nature's subject paths exist in the category tree).
    """
    if field.type == "choice" and field.options and not field.options_recommended:
        base, metadata = Literal[tuple(field.options)], []
    elif field.type == "boolean":
        base, metadata = bool, [BeforeValidator(_parse_bool)]
    elif field.type == "int":
        base, metadata = str, [_int_validator(field)]
    elif field.type == "filelist" and field.type_options:
        base, metadata = str, [_type_options_validator(field)]
    else:
        # text, textarea, authorlist, file, and unconstrained filelist: any string.
        # Existence and shape of files/authors are checked separately below.
        base, metadata = str, []

    if custom_validator is not None:
        metadata = [*metadata, AfterValidator(custom_validator)]

    annotation = base
    for item in metadata:
        annotation = Annotated[annotation, item]
    return Optional[annotation]


# One built pydantic model per venue slug; the field schema is static.
_MODELS: dict[str, Any] = {}


def _model_for(venue: Venue):
    model = _MODELS.get(venue.slug)
    if model is None:
        # Lazy import to avoid a module-load cycle (a venue runner imports this
        # module for parse_authors). Venue-specific field validators extend the
        # generic schema with rules the field metadata cannot express.
        from . import venues

        validators = venues.get_field_validators(venue.slug)
        logger.debug(
            "Building validation model for %s (%d fields, %d custom validator(s))",
            venue.slug,
            len(venue.fields),
            len(validators),
        )
        definitions = {field.id: (_annotation_for(field, validators.get(field.id)), None) for field in venue.fields}
        model = create_model(
            f"{venue.slug}_subfile",
            __config__=ConfigDict(extra="forbid"),
            **definitions,
        )
        _MODELS[venue.slug] = model
    return model


def _schema_issues(venue: Venue, values: dict[str, str]) -> list[Issue]:
    """Run the venue's pydantic schema over the filled values.

    Only non-blank values are checked here (an empty required field is caught
    by the required-field pass in :func:`validate`, with a clearer message).
    Each pydantic complaint becomes one ERROR :class:`Issue`.
    """
    filled = {k: v.strip() for k, v in values.items() if v and v.strip()}
    if not filled:
        raise NotApplicable("no fields filled in")
    try:
        _model_for(venue).model_validate(filled)
    except ValidationError as exc:
        labels = {f.id: f.label for f in venue.fields}
        options = {f.id: f.options for f in venue.fields if f.options}
        issues: list[Issue] = []
        for err in exc.errors():
            field_id = str(err["loc"][0]) if err["loc"] else ""
            if err["type"] == "extra_forbidden":
                issues.append(
                    Issue(
                        ERROR,
                        field_id,
                        f"unknown field '{field_id}' is not part of the " f"{venue.slug} template; remove it or fix the spelling",
                    )
                )
                continue
            label = labels.get(field_id, field_id)
            # A Literal mismatch (a closed ``choice`` field) reads more clearly
            # spelled against the field's own options than via pydantic's default
            # "Input should be ..." wording.
            if err["type"] == "literal_error":
                allowed = options.get(field_id, [])
                issues.append(
                    Issue(
                        ERROR,
                        field_id,
                        f"{label}: '{err.get('input')}' is not a valid option; choose one of: {', '.join(allowed)}",
                    )
                )
                continue
            # pydantic prefixes custom messages with "Value error, "; drop it.
            msg = err["msg"]
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, ") :]
            issues.append(Issue(ERROR, field_id, f"{label}: {msg}"))
        return issues
    return []


def _iter_upload_paths(venue: Venue, values: dict[str, str]):
    """Yield every existing upload file path named by the filled-in values.

    Covers both ``file`` fields (one path) and ``filelist`` fields (one path per
    non-empty line, taken from the leading ``path |`` segment). Paths that do
    not resolve to an existing regular file are skipped here -- their absence is
    reported separately by the per-field file checks.
    """
    for field in venue.fields:
        raw = values.get(field.id, "")
        if not raw.strip():
            continue
        if field.type == "file":
            candidates = [raw.strip()]
        elif field.type == "filelist":
            candidates = [line.split("|", 1)[0].strip() for line in raw.splitlines() if line.strip()]
        else:
            continue
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate).expanduser()
            if path.is_file():
                yield path


def _upload_size_issues(venue: Venue, values: dict[str, str]) -> list[Issue]:
    """Flag a submission whose combined upload size exceeds the portal's limit.

    Sums the size of every uploaded file (across all ``file`` and ``filelist``
    fields) and, when the venue declares ``max_upload_mb``, reports one
    file-level ERROR if the total is over that limit. Venues without a
    documented limit are skipped.
    """
    if not venue.max_upload_mb:
        return []
    total = 0
    for path in _iter_upload_paths(venue, values):
        try:
            total += path.stat().st_size
        except OSError:
            # Unreadable files are reported by the per-field checks; ignore here.
            continue
    limit_bytes = venue.max_upload_mb * BYTES_PER_MB
    if total > limit_bytes:
        total_mb = total / BYTES_PER_MB
        return [
            Issue(
                ERROR,
                "",
                f"total upload size is {total_mb:.1f} MB, over {venue.slug}'s " f"{venue.max_upload_mb:g} MB limit; reduce file sizes or split the submission",
            )
        ]
    return []


def _count_items(field: Field, raw: str) -> int:
    """Number of items in a multi-item field's value.

    A ``multichoice`` is comma-separated, so its items are the comma-delimited
    options; every other multi-item field (a ``filelist``, ``authorlist``, or a
    list-style ``textarea`` such as a reviewer list) carries one item per
    non-empty line.
    """
    if field.type == "multichoice":
        return sum(1 for c in raw.split(",") if c.strip())
    return sum(1 for line in raw.splitlines() if line.strip())


def _check_length(field: Field, raw: str) -> list[Issue]:
    """Flag a text value that runs past its word or character limit.

    ``word_count`` and ``character_count`` are upper bounds declared on a
    ``text``/``textarea`` field (or on a ``choice`` whose off-list value goes
    into a portal free-text box); either or both may be set. Words are
    whitespace-delimited tokens; characters count the trimmed value.
    ``min_character_count`` is the matching lower bound (e.g. arXiv's 20-character
    minimum abstract); a blank value is left to the required/optional check.
    """
    issues: list[Issue] = []
    if field.word_count is not None:
        words = len(raw.split())
        if words > field.word_count:
            issues.append(
                Issue(
                    ERROR,
                    field.id,
                    f"{field.label}: {words} words exceeds the {field.word_count}-word limit",
                )
            )
    if field.character_count is not None:
        chars = len(raw.strip())
        if chars > field.character_count:
            issues.append(
                Issue(
                    ERROR,
                    field.id,
                    f"{field.label}: {chars} characters exceeds the {field.character_count}-character limit",
                )
            )
    if field.min_character_count is not None:
        chars = len(raw.strip())
        if chars < field.min_character_count:
            issues.append(
                Issue(
                    ERROR,
                    field.id,
                    f"{field.label}: {chars} characters is under the " f"{field.min_character_count}-character minimum",
                )
            )
    return issues


def _is_http_url(value: str) -> bool:
    """True if ``value`` is an http/https URL with a host.

    A liberal check: an ``http``/``https`` scheme plus a non-empty network
    location. Enough to reject a bare word, a ``doi:...`` string, or a link with
    no scheme without attempting full RFC 3986 validation.
    """
    parsed = urllib.parse.urlsplit(value.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _check_url(field: Field, raw: str) -> list[Issue]:
    """Flag a ``require_url`` value whose lines are not valid http/https URLs.

    When a ``text``/``textarea`` field sets ``require_url``, every non-blank line
    of its value must be an http(s) link (e.g. a data-availability field that
    collects repository URLs). One ERROR is reported per offending line.
    """
    if not field.require_url:
        return []
    issues: list[Issue] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if line and not _is_http_url(line):
            issues.append(
                Issue(
                    ERROR,
                    field.id,
                    f"{field.label}: line {lineno} '{line}' is not a valid " "http/https URL",
                )
            )
    return issues


def _resolve_conditional(scalar: int | None, by, values: dict[str, str]) -> int | None:
    """Resolve a length limit that may depend on a sibling field's value.

    When ``by`` (a ``ConditionalLimit``) is set, the limit is selected by the
    current value of the field it names: the matching entry in its ``values``
    map, else its ``default``. Falls back to the plain ``scalar`` limit when no
    conditional limit resolves (no ``by``, or its value matches nothing and it
    carries no default).
    """
    if by is not None:
        selected = values.get(by.field, "").strip()
        if selected in by.values:
            return by.values[selected]
        if by.default is not None:
            return by.default
    return scalar


def _check_manuscript_length(field: Field, raw: str, values: dict[str, str]) -> list[Issue]:
    """Flag a manuscript whose length runs past its word or page limit.

    Each measure is capped in two scopes: before the reference list
    (``max_words_before_refs`` / ``max_pages_before_refs``) and across the whole
    document (``max_words`` / ``max_pages``). Any limit may instead be selected
    by a sibling field's value through its ``*_by`` companion (e.g. a page cap
    that varies with the article type); see :func:`_resolve_conditional`. The
    uploaded file is parsed and measured (see :mod:`paperpush.manuscript`); a
    LaTeX source is compiled to a scratch PDF for its page count when a TeX
    toolchain is installed. When the measure cannot be taken for the file's
    format -- a word count of a ``.doc`` binary, or a page count of a format
    without fixed pagination -- the limit is reported as unchecked (a WARNING)
    rather than assumed satisfied or violated.
    """
    word_before = _resolve_conditional(field.max_words_before_refs, field.max_words_before_refs_by, values)
    page_before = _resolve_conditional(field.max_pages_before_refs, field.max_pages_before_refs_by, values)
    word_total = _resolve_conditional(field.max_words, field.max_words_by, values)
    page_total = _resolve_conditional(field.max_pages, field.max_pages_by, values)
    if all(limit is None for limit in (word_before, page_before, word_total, page_total)):
        return []
    path = Path(raw.strip()).expanduser()
    if not path.is_file():
        # A missing/!regular file is already reported by _check_file_field.
        return []

    from . import manuscript

    issues: list[Issue] = []
    # Sections beyond the references that this venue leaves out of its main-text
    # count (an appendix, a required statement); empty for most venues.
    headings = tuple(field.main_text_end_headings or ())
    scope_before = "before references" if not headings else "in the main text"

    def _words_before(path: Path) -> int | None:
        return manuscript.words_before_references(path, headings)

    def _pages_before(path: Path) -> int | None:
        return manuscript.pages_before_references(path, headings)

    # (limit, counter, scope phrase). An empty scope means the whole document.
    for limit, count, scope in (
        (word_before, _words_before, scope_before),
        (word_total, manuscript.total_words, ""),
    ):
        if limit is None:
            continue
        words = count(path)
        where = f" {scope}" if scope else ""
        if words is None:
            issues.append(
                Issue(
                    WARNING,
                    field.id,
                    unchecked=True,
                    message=f"could not count words in {path.name} ('{path.suffix}' is not supported for word counting); the {limit}-word limit was not checked",
                )
            )
        elif words > limit:
            issues.append(
                Issue(
                    ERROR,
                    field.id,
                    f"{field.label}: {words} words{where} exceeds the {limit}-word limit",
                )
            )
    for limit, count, scope, unsupported in (
        (page_before, _pages_before, scope_before, "page counts can only be verified for PDF or LaTeX source rendered with latexmk/pdflatex"),
        (page_total, manuscript.total_pages, "", "page counts can only be verified for PDF, LaTeX source rendered with latexmk/pdflatex, or a Word (.docx) file with a saved page count"),
    ):
        if limit is None:
            continue
        pages = count(path)
        where = f" {scope}" if scope else ""
        if pages is None:
            issues.append(
                Issue(
                    WARNING,
                    field.id,
                    unchecked=True,
                    message=f"could not count pages in {path.name} ({unsupported}); the {limit}-page limit was not checked",
                )
            )
        elif pages > limit:
            issues.append(
                Issue(
                    ERROR,
                    field.id,
                    f"{field.label}: {pages} pages{where} exceeds the {limit}-page limit",
                )
            )
    return issues


def _upload_paths_or_skip(venue: Venue, values: dict[str, str]) -> list[Path]:
    """The existing upload files, or :class:`NotApplicable` when none are attached."""
    paths = list(_iter_upload_paths(venue, values))
    if not paths:
        raise NotApplicable("no files attached")
    return paths


def _findings_to_issues(findings) -> list[Issue]:
    """Turn scanner :class:`~paperpush.sensitive.Finding` objects into WARNINGs.

    A file-scoped finding names its file in the message (``... (in main.tex)``);
    a submission-level one (``where == "submission"``) is shown as-is.
    """
    issues: list[Issue] = []
    for finding in findings:
        if finding.where and finding.where != "submission":
            issues.append(Issue(WARNING, "", f"{finding.detail} (in {finding.where})"))
        else:
            issues.append(Issue(WARNING, "", finding.detail))
    return issues


def _link_issues(venue: Venue, values: dict[str, str]) -> list[Issue]:
    """Flag URLs in the manuscript files that an anonymous reader can't reach.

    Scans the referenced upload files (and archive/PDF text) for http(s) links
    and reports the ones that are definitively broken -- a 404/gone page, or a
    still-private GitHub repository. Runs by default (disable with
    ``--dont-check-links``) and makes network requests; unreachable-but-
    inconclusive links (timeouts, bot blocks) are left unreported.
    """
    from . import sensitive

    paths = _upload_paths_or_skip(venue, values)
    logger.info("Checking %d upload file(s) for unreachable links (%s)", len(paths), venue.slug)
    return _findings_to_issues(sensitive.scan_links(paths))


def _reference_issues(venue: Venue, values: dict[str, str], retracted: list | None = None, resolved: dict | None = None) -> list[Issue]:
    """Flag references whose DOI does not match the work they cite.

    Reads the submission's bibliography whichever way it ships it -- the ``.bib``
    files among the referenced uploads (including one inside a source archive),
    and the manuscript's own reference list -- resolves each DOI through
    doi.org, and reports the ones that are malformed, duplicated, unregistered,
    or registered to a different title/author/year, which is the signature of a
    DOI copied from the wrong row. Runs by default (disable with
    ``--dont-check-references``) and makes network requests; anything
    inconclusive is left unreported. The retracted works it finds are not
    reported here: they go into ``retracted``, for :func:`_retraction_issues`.
    ``resolved`` is filled with the registry records of the DOIs that check out,
    for :func:`_reference_version_issues`.
    """
    from . import references

    paths = _upload_paths_or_skip(venue, values)
    logger.info("Checking %d upload file(s) for reference DOI problems (%s)", len(paths), venue.slug)
    findings = references.scan_references(paths, resolved_out=resolved)
    if retracted is not None:
        retracted.extend(f for f in findings if f.category == references.RETRACTED)
    return _findings_to_issues([f for f in findings if f.category != references.RETRACTED])


def _retraction_issues(venue: Venue, values: dict[str, str], retracted: list | None) -> list[Issue]:
    """Report the cited works whose DOI record carries a retraction notice.

    The notices come with the DOI lookups :func:`_reference_issues` already
    made (``retracted`` is the list it filled), so this makes no requests of
    its own. ``None`` means that lookup did not finish.
    """
    _upload_paths_or_skip(venue, values)
    if retracted is None:
        raise Unchecked("the reference DOI lookup did not finish")
    return _findings_to_issues(retracted)


@dataclass
class _ReferenceContext:
    """What the reference checks share: the DOI lookups feed the search, and both feed the version check."""

    resolved: dict = field(default_factory=dict)
    refs: list | None = None
    files: list[str] = field(default_factory=list)
    search: object = None
    ai_tasks: list = field(default_factory=list)


# How many references a message lists by name.
_MAX_LISTED_REFERENCES = 8


def _listed(labels: list[str]) -> str:
    shown = ", ".join(labels[:_MAX_LISTED_REFERENCES])
    return shown + (f" and {len(labels) - _MAX_LISTED_REFERENCES} more" if len(labels) > _MAX_LISTED_REFERENCES else "")


def _bibliography_refs(venue: Venue, values: dict[str, str], ctx: _ReferenceContext) -> list:
    """The submission's references: its ``.bib`` entries, or else its manuscript's reference list."""
    if ctx.refs is not None:
        return ctx.refs
    from . import reference_search, references

    paths = _upload_paths_or_skip(venue, values)
    entries = references.collect_entries(paths)
    ctx.files = [str(p) for p in paths if p.suffix.lower() in references.BIB_EXTS]
    from .requirements_check import manuscript_files

    manuscripts = manuscript_files(venue, values)
    ctx.files += [str(path) for _, path in manuscripts]
    if entries:
        ctx.refs = reference_search.references_from_entries(entries)
    else:
        ctx.refs = []
        for _, path in manuscripts[:1]:
            try:
                ctx.refs = reference_search.references_from_text(path)
            except Exception:  # noqa: BLE001 -- an unreadable manuscript leaves nothing to search
                logger.debug("could not read the reference list of %s", path, exc_info=True)
    return ctx.refs


def _ai_files(paths: list[str]) -> list[str]:
    """The files to show the AI: archives are not unpacked for it, so leave them out when anything else is there."""
    from .sensitive import ARCHIVE_EXTS

    unique = list(dict.fromkeys(paths))
    readable = [p for p in unique if Path(p).suffix.lower() not in ARCHIVE_EXTS and "".join(Path(p).suffixes[-2:]).lower() not in ARCHIVE_EXTS]
    return readable or unique


def _unmatched_reference_issues(venue: Venue, values: dict[str, str], ctx: _ReferenceContext) -> Outcome:
    """Search the registries for every reference without a DOI (see :mod:`paperpush.reference_search`).

    A ``.bib`` journal or conference entry no registry can match is reported,
    as is one whose authors or year disagree with the work its title matches.
    A matched entry with no DOI is told the DOI. Near misses, unindexed kinds of
    work (books, software, ...), and reference-list entries read from a PDF or
    Word file are listed as unverified and queued for ``ai.unverified_references``.
    Makes network requests; runs with the DOI check (``--dont-check-references``).
    """
    from . import ai_validate, reference_search

    refs = _bibliography_refs(venue, values, ctx)
    if not refs:
        raise NotApplicable("found no .bib file or reference list to read")
    without_doi = [r for r in refs if not r.doi]
    if not without_doi:
        return Outcome([], f"all {len(refs)} references have a DOI (checked by the DOI check)")
    result = reference_search.search_references(refs)
    ctx.search = result
    issues = _findings_to_issues(result.findings)

    suggestions = result.suggestions
    if suggestions:
        listed = [f"{lk.ref.label} -> {lk.record['DOI'].lower()}" for lk in suggestions]
        issues.append(Issue(WARNING, "", f"{len(suggestions)} reference(s) have no DOI but match a registered work; add the DOI so readers (and this check) can follow them: {_listed(listed)}"))

    unverified = result.unverified
    unknown = result.by_status(reference_search.UNKNOWN)
    pending = [lk.ref for lk in unknown] + result.skipped
    if unverified or pending:
        labels = [lk.ref.label for lk in unverified] + [r.label for r in pending]
        why = []
        if unverified:
            why.append(f"{len(unverified)} could not be matched with certainty (near misses, or kinds of work the registries often miss)")
        if unknown:
            why.append(f"{len(unknown)} could not be looked up (the registries did not answer)")
        if result.skipped:
            why.append(f"{len(result.skipped)} were not looked up ({result.skipped_reason})")
        issues.append(unchecked("", f"{len(labels)} reference(s) without a DOI are unverified: {'; '.join(why)}. Check them by hand, or run the AI check: {_listed(labels)}"))
        if ctx.files:
            ctx.ai_tasks.append(
                ai_validate.AiTask(
                    id="ai.unverified_references",
                    title="References that could not be verified are real",
                    category="Integrity",
                    instructions=reference_search.ai_task_instructions(unverified + unknown, result.skipped),
                    files=_ai_files(ctx.files),
                )
            )
    searched = len(result.lookups)
    if searched and len(unknown) == searched and not result.findings:
        raise Unchecked("the registries (Crossref, OpenAlex, arXiv) did not answer")
    matched = len(result.by_status(reference_search.MATCHED))
    return Outcome(issues, f"looked up {searched} of {len(without_doi)} reference(s) without a DOI: {matched} matched a registered work")


def _reference_version_issues(venue: Venue, values: dict[str, str], ctx: _ReferenceContext) -> Outcome:
    """Report preprints that have been published, preprints cited with their own article, and old arXiv versions."""
    from . import reference_search

    refs = _bibliography_refs(venue, values, ctx)
    search = ctx.search
    cited: list = []
    labelled: set[str] = set()
    for r in refs:
        record = ctx.resolved.get(r.doi) if r.doi else None
        if record is not None:
            cited.append(reference_search.Cited(r.label, r.where, r.doi, record))
            labelled.add(r.doi)
    for doi, record in ctx.resolved.items():
        if doi not in labelled:
            cited.append(reference_search.Cited(f"the reference with DOI {doi}", "", doi, record))
    matched_dois: set[str] = set()
    if search is not None:
        for lk in search.by_status(reference_search.MATCHED):
            if lk.record and not lk.problems and lk.source != "arXiv" and lk.record.get("DOI"):
                doi = str(lk.record["DOI"]).lower()
                matched_dois.add(doi)
                cited.append(reference_search.Cited(lk.ref.label, lk.ref.where, doi, lk.record))
    arxiv_refs = [r for r in refs if r.arxiv_id]
    if not cited and not arxiv_refs:
        raise NotApplicable("no reference with a registry record or an arXiv identifier to check")
    all_dois = {r.doi for r in refs if r.doi} | set(ctx.resolved) | matched_dois
    findings = reference_search.version_findings(
        cited,
        arxiv_refs,
        all_dois,
        arxiv_records=getattr(search, "arxiv_records", None),
        arxiv_checked=getattr(search, "arxiv_checked", ()),
    )
    return Outcome(_findings_to_issues(findings), f"checked {len(cited) + len(arxiv_refs)} cited work(s) for a newer version")


def _sensitive_issues(venue: Venue, values: dict[str, str]) -> list[Issue]:
    """Scan every referenced upload for information not meant to be public.

    Runs the :mod:`paperpush.sensitive` scanner over each ``file``/``filelist``
    path (and, for LaTeX/source bundles, their archive members): pasted API keys
    and passwords, private keys, GPS coordinates in figure photos, editable
    Google-Docs links, and LaTeX ``%`` comments. It also nudges when the
    manuscript links no public code repository, and -- for an arXiv submission
    whose LaTeX source still carries comments/junk -- reminds the author to run
    ``arxiv_latex_cleaner``. (Reachability of the links it *does* cite is a
    separate, on-by-default check; see :func:`_link_issues`.)

    Findings are advisory (WARNING) -- they surface what would become public
    alongside the paper so the author can act, but they never block a
    submission. This is opt-in because reading and text-extracting every
    attachment is more work than the generic field checks.
    """
    from . import sensitive

    paths = _upload_paths_or_skip(venue, values)
    logger.info("Scanning %d upload file(s) for sensitive information (%s)", len(paths), venue.slug)
    findings = sensitive.scan_paths(paths)
    findings.extend(sensitive.scan_missing_code_link(paths))

    issues = _findings_to_issues(findings)
    issues.extend(_arxiv_cleaner_reminder(venue, findings))
    return issues


def _anonymity_issues(venue: Venue, values: dict[str, str], *, check_repos: bool = True) -> list[Issue]:
    """Flag information that identifies the authors of a double-blind submission.

    Runs for venues marked ``"anonymous": true`` in ``venues.json`` (and for any
    venue under ``validate --anonymous``). Scans the uploads, the ``.sub``'s
    free-text values, and every anonymous.4open.science repository they link
    for the authors' names, emails, ORCID/OpenReview IDs and affiliations (taken
    from the author list), author metadata, home-directory paths, an
    acknowledgments section, and camera-ready LaTeX switches (see
    :mod:`paperpush.anonymity`). ``check_repos`` fetches the linked mirrors,
    which needs network access. Findings are advisory WARNINGs.
    """
    from . import anonymity

    paths = list(_iter_upload_paths(venue, values))
    terms = anonymity.identity_terms(venue, values)
    field_texts = {f.label or f.id: values[f.id] for f in venue.fields if f.type in ("text", "textarea") and values.get(f.id, "").strip()}
    if not paths and not field_texts:
        raise NotApplicable("no files attached and no free-text fields filled in")
    logger.info("Checking %d upload file(s) for identifying information (%s, %d identity term(s))", len(paths), venue.slug, len(terms))
    return _findings_to_issues(anonymity.scan_submission(paths, terms, field_texts, check_repos=check_repos))


def _hidden_text_issues(venue: Venue, values: dict[str, str]) -> list[Issue]:
    """Flag text a reader cannot see, above all prompts aimed at AI reviewers.

    Scans every PDF, LaTeX source, and Word upload for white, invisible,
    microscopic, or off-page text (see :mod:`paperpush.hidden_text`). Hidden
    text that instructs a reviewer or language model is an ERROR -- conferences
    treat it as an ethics violation -- and other hidden prose, or visible text
    addressed to an AI reviewer, is a WARNING. Runs by default (disable with
    ``--dont-check-hidden-text``); makes no network requests.
    """
    from . import hidden_text

    paths = _upload_paths_or_skip(venue, values)
    logger.info("Checking %d upload file(s) for hidden text (%s)", len(paths), venue.slug)
    issues: list[Issue] = []
    for finding in hidden_text.scan_paths(paths):
        level = ERROR if finding.is_error else WARNING
        issues.append(Issue(level, "", f"{finding.detail} (in {finding.where})"))
    return issues


def _pdf_reader_issues(venue: Venue, values: dict[str, str], *, check_hidden_text: bool, check_manuscript: bool) -> list[Issue]:
    """Say once that PDF checks were skipped because pdfminer.six is not installed.

    The hidden-text scan and the modified-template check read PDFs glyph by
    glyph through the optional ``pdf`` extra. Without it they pass over every
    PDF silently, which would hide a prompt-injection error, so this WARNING
    names what was skipped and how to install it. Quiet when nothing a skipped
    check would read was uploaded.
    """
    from . import pdf_layout

    if pdf_layout.available():
        return []
    skipped: list[str] = []
    if check_hidden_text:
        skipped.append("hidden text / prompt injection")
    if check_manuscript:
        from .requirements import get_requirements

        reqs = get_requirements(venue.slug)
        if reqs is not None and reqs.manuscript.template_layout is not None:
            skipped.append("modified template")
    if not skipped:
        return []
    # A LaTeX manuscript is compiled to a PDF for the template check.
    measured = {".pdf", ".tex", ".zip"} if "modified template" in skipped else {".pdf"}
    if not any(path.suffix.lower() in measured for path in _iter_upload_paths(venue, values)):
        return []
    return [Issue(WARNING, "", f"PDF checks skipped ({', '.join(skipped)}): they need pdfminer.six; install it with `{pdf_layout.INSTALL_HINT}`")]


def _openreview_profile_issues(venue: Venue, values: dict[str, str]) -> list[Issue]:
    """Check the authors' OpenReview profiles, for venues submitted through OpenReview.

    Applies when the venue's author list carries an Open Review ID column (ICLR,
    AAAI). Looks each author up through the OpenReview API with the stored
    login (see :mod:`paperpush.openreview_profiles`): a missing, ambiguous, or
    inactive profile, a name mismatch, no current position, email suffixes the
    profile does not carry. Runs by default (disable with
    ``--dont-check-openreview-profiles``) and needs network access.
    """
    from . import openreview_profiles

    if not openreview_profiles.applies(venue):
        return []
    logger.info("Checking OpenReview author profiles (%s)", venue.slug)
    return [Issue(ERROR if p.error else WARNING, p.field, f"openreview: {p.message}") for p in openreview_profiles.check(venue, values)]


def _manuscript_requirement_issues(venue: Venue, values: dict[str, str]) -> list[Issue]:
    """Measure the uploads against the venue's author guidelines.

    Delegates to :func:`paperpush.requirements_check.check_manuscript_requirements`,
    which reads ``manuscript_requirements.json`` -- the companion to
    ``submission_forms.json`` that records what the guidelines demand of the manuscript
    itself (formats, length, sections, statements, title page, figures,
    references). Runs by default (disable with ``--dont-check-manuscript``).
    Reads and, for LaTeX, compiles the manuscript; makes no network requests.
    """
    from .requirements_check import check_manuscript_requirements

    return check_manuscript_requirements(venue, values)


# Scan categories that mean an arXiv source bundle still carries author-only
# content -- i.e. arxiv_latex_cleaner (or an equivalent) has not been run.
_UNCLEANED_LATEX = {"LaTeX comments", "LaTeX note comment", "unnecessary file"}


def _arxiv_cleaner_reminder(venue: Venue, findings) -> list[Issue]:
    """Remind an arXiv submitter to sanitise their LaTeX source, if it looks unclean.

    arXiv publishes the uploaded source, so leftover comments and build/VCS junk
    become public. When the venue is arXiv (or an arXiv-based alias) and the scan
    turned up any of those, emit one reminder to run ``arxiv_latex_cleaner``.
    Their absence is treated as "already cleaned", so a sanitised submission
    stays quiet.
    """
    from . import venues

    try:
        base = venues.submission_base(venue.slug)
    except Exception:
        logger.debug("Could not resolve submission base for %s; using slug as-is", venue.slug, exc_info=True)
        base = venue.slug
    if base != "arxiv":
        return []
    if not any(f.category in _UNCLEANED_LATEX for f in findings):
        return []
    return [
        Issue(
            WARNING,
            "",
            "arXiv publishes your uploaded LaTeX source publicly; run arxiv_latex_cleaner on " "your source to strip the comments and unneeded files above before submitting " "(`paperpush validate --arxiv-latex-cleaner` does this and validates the result)",
        )
    ]


class NotApplicable(Exception):
    """Raised by a check that has nothing to look at for this submission."""


class Unchecked(Exception):
    """Raised by a check that applies but cannot run here (the message says why)."""


@dataclass
class Outcome:
    """What a check function found, plus a note on what it measured."""

    issues: list[Issue]
    detail: str = ""


def _status_for(issues: list[Issue]) -> str:
    from . import report

    if any(i.is_error for i in issues):
        return report.FAILED
    if any(not i.unchecked for i in issues):
        return report.WARNING
    if issues:
        return report.UNCHECKED
    return report.PASSED


class Checks:
    """Runs checks and keeps one :class:`~paperpush.report.CheckResult` each.

    A check function returns a list of :class:`Issue` (or an :class:`Outcome`
    carrying a note on what it measured), raises :class:`NotApplicable` when
    there is nothing for it to look at, or :class:`Unchecked` when it cannot
    run. An unexpected exception is logged and recorded as unchecked, so one
    broken heuristic never hides the rest of the report.
    """

    def __init__(self, options: dict | None = None) -> None:
        from . import report

        self._report = report
        self.results: list = []
        self.ai_tasks: list = []
        # Settings the check modules read (e.g. ``cover_letter_max_age_days``).
        self.options: dict = dict(options or {})

    def run(self, check_id: str, title: str, category: str, fn, *args, **kwargs):
        report = self._report
        try:
            out = fn(*args, **kwargs)
        except NotApplicable as exc:
            return self.record(check_id, title, category, report.NOT_APPLICABLE, detail=str(exc))
        except Unchecked as exc:
            return self.record(check_id, title, category, report.UNCHECKED, detail=str(exc))
        except Exception as exc:  # noqa: BLE001 -- report it, keep validating
            logger.warning("check %s failed to run: %s", check_id, exc)
            logger.debug("check %s traceback", check_id, exc_info=True)
            return self.record(check_id, title, category, report.UNCHECKED, detail=f"the check itself failed ({type(exc).__name__}: {exc}); please report this")
        if out is None:
            out = Outcome([])
        elif isinstance(out, list):
            out = Outcome(out)
        return self.record(check_id, title, category, _status_for(out.issues), issues=out.issues, detail=out.detail)

    def record(self, check_id: str, title: str, category: str, status: str, *, issues: list[Issue] | None = None, detail: str = "", source: str = ""):
        from dataclasses import replace

        stamped = [replace(i, check=i.check or check_id) for i in (issues or [])]
        result = self._report.CheckResult(id=check_id, title=title, category=category, status=status, issues=stamped, detail=detail, source=source)
        self.results.append(result)
        return result

    def needs_ai(self, task) -> None:
        """Queue an :class:`~paperpush.ai_validate.AiTask`; it is resolved after the deterministic checks."""
        self.ai_tasks.append(task)


def _record_switched_off(checks: "Checks", check_id: str, title: str, category: str, venue: Venue, values: dict[str, str], detail: str) -> None:
    """Record a file-reading check that was switched off: unchecked, or not applicable when no files are attached."""
    from . import report

    try:
        _upload_paths_or_skip(venue, values)
    except NotApplicable as exc:
        checks.record(check_id, title, category, report.NOT_APPLICABLE, detail=str(exc))
        return
    checks.record(check_id, title, category, report.UNCHECKED, detail=detail)


class _Progress:
    """Reports how far :func:`validate_report` has got to an optional callback.

    ``callback(done, total, label)`` is called as each stage starts (``label``
    names it, ``done`` stages are finished) and once more at the end with
    ``done == total`` and ``label == "done"``. A failing callback is logged and
    ignored: a progress display must never break validation.
    """

    def __init__(self, callback, stages: list[str]) -> None:
        self._callback = callback
        self._total = len(stages)
        self._done = 0

    def _emit(self, label: str) -> None:
        if self._callback is None:
            return
        try:
            self._callback(self._done, self._total, label)
        except Exception:  # noqa: BLE001 -- a progress display must not break validation
            logger.debug("validate progress callback failed", exc_info=True)

    def run(self, label: str, fn, *args, **kwargs):
        """Report ``label`` as started, run ``fn``, then count the stage done."""
        self._emit(label)
        try:
            return fn(*args, **kwargs)
        finally:
            self._done = min(self._done + 1, self._total)

    def finish(self) -> None:
        self._done = self._total
        self._emit("done")


# The manuscript-check modules validate_report runs, in order, with the stage
# name the progress bar shows for each.
_MANUSCRIPT_STAGES = (
    ("requirements_check", "Author guidelines"),
    ("layout_check", "Page layout"),
    ("figure_check", "Figures"),
    ("citation_check", "Figure and table order"),
    ("consistency", "Form against the manuscript"),
    ("front_matter_check", "Abstract and front matter"),
    ("bibliography_check", "Reference list"),
    ("feature_check", "Additional manuscript checks"),
    ("reference_review", "Reference age and density"),
    ("cover_letter_check", "Cover letter"),
    ("prose_check", "Spelling and prose"),
)


def _run_manuscript_stages(bar: _Progress, checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    import importlib

    for name, label in _MANUSCRIPT_STAGES:
        module = importlib.import_module(f".{name}", __package__)
        bar.run(label, module.run_checks, checks, venue, values)


def _ai_only_report(checks: Checks, subfile: SubFile, venue: Venue, *, ai, subfile_path: str, initial_submission: bool, progress=None):
    """The :func:`validate_report` for ``ai_only``: just the checks that need AI."""
    from . import ai_validate, report
    from .requirements import any_format_initial_rule

    values = subfile.values
    bar = _Progress(progress, [label for _, label in _MANUSCRIPT_STAGES] + ["AI checks"])
    # These checks queue the AI tasks that apply (raster figures, a PDF's
    # template, title-page items, ...); their own results are not reported.
    _run_manuscript_stages(bar, checks, venue, values)
    checks.results = []
    tasks = bar.run("AI checks", ai_validate.resolve, checks, venue, values, ai)
    bar.finish()
    result = report.ValidationReport(venue=venue.slug, subfile=subfile_path, results=checks.results, ai_tasks=tasks, ai_only=True)
    result.initial_submission_rule = any_format_initial_rule(venue.slug, values)
    if initial_submission:
        result.initial_submission = True
        result.downgraded = downgrade_errors(result.results)
    if ai is not None:
        result.ai = ai.description
        result.ai_error = ai.error
    logger.info("Validated %s subfile (AI checks only): %d AI check(s)", venue.slug, len(tasks))
    return result


def _off(flag: str) -> Unchecked:
    return Unchecked(f"switched off ({flag})")


def validate(
    subfile: SubFile,
    venue: Venue,
    *,
    check_sensitive: bool = True,
    check_links: bool = True,
    check_references: bool = True,
    reference_check_full: bool = False,
    check_manuscript: bool = True,
    check_anonymous: Optional[bool] = None,
    check_hidden_text: bool = True,
    check_openreview: bool = True,
    initial_submission: bool = False,
    cover_letter_max_age_days: int = 7,
    references_alphabetical: bool = False,
    references_citation_order: bool = False,
) -> list[Issue]:
    """Return all issues found in ``subfile`` against ``venue``.

    Combines schema-level checks (allowed options, file types, booleans, and
    stray fields, via pydantic) with required-field, file-existence, and
    author-list checks. Every problem is reported, so the author can fix them
    in one pass. This is the flat list of problems from
    :func:`validate_report`, which also records the checks that passed or did
    not run; see it for what each flag does.
    """
    return validate_report(
        subfile,
        venue,
        check_sensitive=check_sensitive,
        check_links=check_links,
        check_references=check_references,
        reference_check_full=reference_check_full,
        check_manuscript=check_manuscript,
        check_anonymous=check_anonymous,
        check_hidden_text=check_hidden_text,
        check_openreview=check_openreview,
        initial_submission=initial_submission,
        cover_letter_max_age_days=cover_letter_max_age_days,
        references_alphabetical=references_alphabetical,
        references_citation_order=references_citation_order,
    ).issues()


def validate_report(
    subfile: SubFile,
    venue: Venue,
    *,
    check_sensitive: bool = True,
    check_links: bool = True,
    check_references: bool = True,
    reference_check_full: bool = False,
    check_manuscript: bool = True,
    check_anonymous: Optional[bool] = None,
    check_hidden_text: bool = True,
    check_openreview: bool = True,
    initial_submission: bool = False,
    cover_letter_max_age_days: int = 7,
    references_alphabetical: bool = False,
    references_citation_order: bool = False,
    ai=None,
    subfile_path: str = "",
    ai_only: bool = False,
    progress=None,
    files_only: bool = False,
    notes: Optional[list[str]] = None,
):
    """Run every check on ``subfile`` and return a :class:`~paperpush.report.ValidationReport`.

    The report holds one entry per check -- passed, failed, warning, could not
    run, needs AI, not implemented, or not applicable -- so the author sees what
    was verified as well as what is wrong.

    When ``check_sensitive`` is set (the default), the referenced upload files
    are scanned for information that was never meant to be shared -- secrets, GPS
    metadata, editable-document links, and LaTeX source comments (see
    :func:`_sensitive_issues`) -- and reported as advisory WARNINGs. When
    ``check_links`` is set (also the default), the URLs those files cite are
    probed and any that are unreachable (404/gone, including a still-private
    GitHub repo) are likewise flagged. When ``check_references`` is set (also
    the default), the DOIs in the submission's bibliography -- its ``.bib``
    files and the manuscript's own reference list -- are resolved and compared
    against the title, author, and year each reference claims (see
    :func:`_reference_issues`). Both make network requests. When
    ``check_manuscript`` is set (also the default), the manuscript, figures,
    and other uploads are measured against the venue's author guidelines as
    recorded in ``manuscript_requirements.json`` -- formats, length, required
    sections and statements, title-page items, figure resolution, reference
    count (see :mod:`paperpush.requirements_check`); a LaTeX manuscript is
    compiled to a scratch PDF for its page count. The same switch covers the
    page layout (:mod:`paperpush.layout_check`), the figures' resolution,
    lettering, and type (:mod:`paperpush.figure_check`), the order figures and
    tables are cited in (:mod:`paperpush.citation_check`), and whether the
    title, abstract, authors, keywords, and statements in the ``.sub`` match
    the manuscript and whether the word, figure, and table counts stated on its
    title page are right (:mod:`paperpush.consistency`), whether the cover
    letter is recently dated and names this venue
    (:mod:`paperpush.cover_letter_check`), whether the abstract cites
    references or lacks the structure the venue asks for and whether the
    manuscript gives a contact e-mail (:mod:`paperpush.front_matter_check`),
    whether every reference is cited and how many are self-citations
    (:mod:`paperpush.bibliography_check`), and the manuscript's prose
    for double spaces, repeated words, punctuation spacing, unbalanced
    brackets and quotes, common misspellings, and repetitive word choice
    (:mod:`paperpush.prose_check`).

    ``check_anonymous`` scans the submission for information identifying its
    authors, including every linked anonymous.4open.science repository (see
    :func:`_anonymity_issues`; network access is needed for the repositories).
    It defaults to the venue's ``anonymous`` flag, so double-blind venues are
    always checked; pass True to check any venue.

    ``check_hidden_text`` (default on) looks for white, invisible, microscopic,
    or off-page text in the uploads and reports hidden instructions to AI
    reviewers as errors (see :func:`_hidden_text_issues`). ``check_openreview``
    (default on; network) looks up each author's OpenReview profile for venues
    submitted through OpenReview (see :func:`_openreview_profile_issues`).

    ``initial_submission`` validates the file as an initial submission: every
    error is reported as a warning instead, so nothing blocks it. It is meant
    for venues whose guidelines let the initial submission be in any format
    (``manuscript.initial_submission_any_format``); that rule is put on the
    report (``initial_submission_rule``) whether or not the flag is set.

    ``cover_letter_max_age_days`` is how old the cover letter's date may be
    before it is reported (see :mod:`paperpush.cover_letter_check`, which also
    checks that the letter does not name another venue).

    ``ai`` (an :class:`~paperpush.ai_validate.AiOptions`) decides what happens
    to the checks only an LLM can judge: run them through a provider's API,
    take an agent's answers from a results file, or (None) list them as
    needing AI.

    ``ai_only`` reports only those AI checks. The manuscript checks still run,
    since they decide which AI checks apply, but their results are dropped, and
    the form, link, DOI, sensitive-data, anonymity, hidden-text, and OpenReview
    checks are skipped. It needs ``check_manuscript``.

    ``files_only`` is for files named without a ``.sub`` (see
    :mod:`paperpush.file_inputs`): ``subfile`` then holds only upload paths, so
    the form checks and the OpenReview lookup are reported as not applicable
    instead of failing on every empty field. With the generic venue
    (:func:`paperpush.database.generic_venue`) the author-guideline checks are
    not applicable too, leaving the checks that apply to any manuscript.
    ``notes`` are put at the top of the report (how the files were placed).

    ``progress``, if given, is called as ``progress(done, total, label)`` when
    each stage starts (``label`` names it) and once at the end with
    ``done == total``; the CLI's progress bar and the web page's use it.
    """
    from . import openreview_profiles, report

    if check_anonymous is None:
        check_anonymous = venue.anonymous
    logger.info(
        "Validating %s: %d field(s) (sensitive-scan=%s, link-check=%s, reference-check=%s, manuscript-check=%s, anonymity-check=%s, hidden-text-check=%s, openreview-check=%s)",
        venue.slug,
        len(venue.fields),
        check_sensitive,
        check_links,
        check_references,
        check_manuscript,
        check_anonymous,
        check_hidden_text,
        check_openreview,
    )
    values = subfile.values
    if reference_check_full and not check_manuscript:
        raise ValueError("reference_check_full needs check_manuscript to read citation contexts")
    checks = Checks(options={"cover_letter_max_age_days": cover_letter_max_age_days, "reference_check_full": reference_check_full, "references_alphabetical": references_alphabetical, "references_citation_order": references_citation_order})
    if ai_only:
        if not check_manuscript:
            raise ValueError("ai_only needs check_manuscript: the manuscript checks decide which AI checks apply")
        result = _ai_only_report(checks, subfile, venue, ai=ai, subfile_path=subfile_path, initial_submission=initial_submission, progress=progress)
        _set_scope(result, venue, values, files_only, notes)
        return result

    openreview_applies = openreview_profiles.applies(venue)
    optional = (
        (check_links, "Links"),
        (check_references, "Reference DOIs"),
        (check_references, "Reference search"),
        (check_sensitive, "Sensitive information"),
        (check_anonymous, "Anonymity"),
        (check_hidden_text, "Hidden text"),
        (openreview_applies and check_openreview, "OpenReview profiles"),
    )
    stages = ["Submission form"] + [label for on, label in optional if on]
    if check_manuscript:
        stages += [label for _, label in _MANUSCRIPT_STAGES] + ["AI checks"]
    bar = _Progress(progress, stages)

    form = "Submission form"

    def _form_checks() -> None:
        if files_only:
            # No .sub: check just the named files, and say the form was not.
            uploads = replace(venue, fields=[f for f in venue.fields if f.type in ("file", "filelist") and values.get(f.id, "").strip()])
            _field_checks(checks, uploads, values)
            checks.results = [r for r in checks.results if not r.id.startswith("form.")]
            checks.record("form", "Submission form fields", form, report.NOT_APPLICABLE, detail="no .sub file, so the form's fields were not checked (only the files named)")
        else:
            checks.run("form.schema", "Field values have the right type and known field ids", form, _schema_issues, venue, values)
            _field_checks(checks, venue, values)
        checks.run("files.total_size", "Combined upload size", "Files", _total_size_check, venue, values)

    bar.run("Submission form", _form_checks)

    integrity = "Integrity"
    if check_links:
        bar.run("Links", checks.run, "integrity.links", "Links cited in the files are reachable", integrity, _link_issues, venue, values)
    else:
        _record_switched_off(checks, "integrity.links", "Links cited in the files are reachable", integrity, venue, values, "switched off (--dont-check-links)")
    retractions_title = "No cited work has been retracted"
    unmatched_title = "References without a DOI match a registered work"
    versions_title = "References cite the journal article, or the latest preprint"
    if check_references:
        retracted: list = []
        ref_ctx = _ReferenceContext()
        dois = bar.run("Reference DOIs", checks.run, "integrity.dois", "Reference DOIs match the works they cite", integrity, _reference_issues, venue, values, retracted, ref_ctx.resolved)
        checks.run("integrity.retractions", retractions_title, integrity, _retraction_issues, venue, values, None if dois.status == report.UNCHECKED else retracted)
        bar.run("Reference search", checks.run, "integrity.unmatched_references", unmatched_title, integrity, _unmatched_reference_issues, venue, values, ref_ctx)
        checks.run("integrity.reference_versions", versions_title, integrity, _reference_version_issues, venue, values, ref_ctx)
        for task in ref_ctx.ai_tasks:
            checks.needs_ai(task)
    else:
        for check_id, title in (("integrity.dois", "Reference DOIs match the works they cite"), ("integrity.retractions", retractions_title), ("integrity.unmatched_references", unmatched_title), ("integrity.reference_versions", versions_title)):
            _record_switched_off(checks, check_id, title, integrity, venue, values, "switched off (--dont-check-references)")
    if check_sensitive:
        bar.run("Sensitive information", checks.run, "integrity.sensitive", "No secrets or private information in the files", integrity, _sensitive_issues, venue, values)
    else:
        checks.record("integrity.sensitive", "No secrets or private information in the files", integrity, report.UNCHECKED, detail="switched off (--dont-check-for-sensitive-info)")
    if check_anonymous:
        bar.run("Anonymity", checks.run, "integrity.anonymity", "Nothing identifies the authors (double-blind)", integrity, _anonymity_issues, venue, values)
    else:
        checks.record("integrity.anonymity", "Nothing identifies the authors (double-blind)", integrity, report.NOT_APPLICABLE, detail="the venue is not double-blind (use --anonymous to check anyway)")
    if check_hidden_text:
        bar.run("Hidden text", checks.run, "integrity.hidden_text", "No hidden text or prompts aimed at AI reviewers", integrity, _hidden_text_issues, venue, values)
    else:
        checks.record("integrity.hidden_text", "No hidden text or prompts aimed at AI reviewers", integrity, report.UNCHECKED, detail="switched off (--dont-check-hidden-text)")
    pdf_reader = _pdf_reader_issues(venue, values, check_hidden_text=check_hidden_text, check_manuscript=check_manuscript)
    if pdf_reader:
        checks.record("integrity.pdf_reader", "PDF glyph reader (pdfminer.six) available", integrity, report.UNCHECKED, issues=[replace_unchecked(i) for i in pdf_reader])
    if not openreview_applies:
        checks.record("integrity.openreview", "Authors' OpenReview profiles", integrity, report.NOT_APPLICABLE, detail="the venue is not submitted through OpenReview")
    elif files_only:
        checks.record("integrity.openreview", "Authors' OpenReview profiles", integrity, report.NOT_APPLICABLE, detail="no .sub file, so there is no author list to look up")
    elif check_openreview:
        bar.run("OpenReview profiles", checks.run, "integrity.openreview", "Authors' OpenReview profiles", integrity, _openreview_profile_issues, venue, values)
    else:
        checks.record("integrity.openreview", "Authors' OpenReview profiles", integrity, report.UNCHECKED, detail="switched off (--dont-check-openreview-profiles)")

    if check_manuscript:
        from . import requirements_check

        _run_manuscript_stages(bar, checks, venue, values)
        requirements_check.not_implemented(checks, venue, values)
    else:
        checks.record("guidelines", "Manuscript checked against the author guidelines", "Guidelines", report.UNCHECKED, detail="switched off (--dont-check-manuscript)")

    from . import ai_validate

    tasks = bar.run("AI checks", ai_validate.resolve, checks, venue, values, ai) if check_manuscript else []
    bar.finish()

    from .requirements import any_format_initial_rule

    result = report.ValidationReport(venue=venue.slug, subfile=subfile_path, results=checks.results, ai_tasks=tasks)
    _set_scope(result, venue, values, files_only, notes)
    result.initial_submission_rule = any_format_initial_rule(venue.slug, values)
    if initial_submission:
        result.initial_submission = True
        result.downgraded = downgrade_errors(result.results)
    if ai is not None:
        result.ai = ai.description
        result.ai_error = ai.error
    errors = len(result.errors)
    logger.info(
        "Validated %s subfile: %d issue(s) (%d error(s), %d warning(s))",
        venue.slug,
        len(result.issues()),
        errors,
        len(result.issues()) - errors,
    )
    return result


def _set_scope(result, venue: Venue, values: dict[str, str], files_only: bool, notes: Optional[list[str]]) -> None:
    """Record on ``result`` what was validated: a .sub, files for a venue, or files alone."""
    result.notes = list(notes or [])
    if venue.is_generic:
        result.scope = "general"
    elif files_only:
        result.scope = "files"
    if result.scope:
        result.files = [str(p) for p in _iter_upload_paths(venue, values)]


def downgrade_errors(results: list) -> int:
    """Turn every error in ``results`` into a warning, in place; return how many.

    A failed check becomes a warning check. Used by ``--initial-submission``.
    """
    from dataclasses import replace

    from . import report

    count = 0
    for result in results:
        if any(i.is_error for i in result.issues):
            count += sum(1 for i in result.issues if i.is_error)
            result.issues = [replace(i, level=WARNING) if i.is_error else i for i in result.issues]
        if result.status == report.FAILED:
            result.status = report.WARNING
    return count


def replace_unchecked(issue: Issue) -> Issue:
    from dataclasses import replace

    return replace(issue, unchecked=True)


def _total_size_check(venue: Venue, values: dict[str, str]) -> list[Issue]:
    if not venue.max_upload_mb:
        raise NotApplicable("the portal states no combined upload limit")
    return _upload_size_issues(venue, values)


def _field_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    """The per-field checks, each recorded as its own check in the report."""
    from . import report

    buckets: dict[str, list[Issue]] = {key: [] for key in ("required", "options", "counts", "lengths", "urls", "uploads", "manuscript_length", "authors", "subfields")}
    applied: set[str] = {"required"}

    for field in venue.fields:
        raw = values.get(field.id, "")
        present = bool(raw.strip())

        # A field is required either unconditionally (``required``) or when its
        # ``required_if`` target carries a value (e.g. funding_country is required
        # only once funding is given). A yes/no target is the exception: only an
        # affirmative triggers the requirement, since answering the question "no"
        # is itself a value but means the dependent fields do not apply (e.g.
        # Discrete Mathematics' repository fields under share_data).
        trigger_field = next((f for f in venue.fields if f.id == field.required_if), None) if field.required_if else None
        trigger_raw = values.get(field.required_if, "") if field.required_if else ""
        if trigger_field is not None and trigger_field.type == "boolean":
            triggered = _truthy_bool(trigger_raw)
        else:
            triggered = bool(trigger_raw.strip())
        required = field.required or triggered

        # Required fields must carry a value. A required confirmation boolean (a
        # consent checkbox, ``confirm: true``) must additionally be affirmative,
        # so an empty one asks to be set to yes rather than just "filled in". A
        # plain required yes/no question only needs an answer.
        if required and not present:
            if field.type == "boolean" and field.confirm:
                buckets["required"].append(Issue(ERROR, field.id, f"{field.label} must be confirmed (set to yes)"))
            elif field.type == "boolean":
                buckets["required"].append(Issue(ERROR, field.id, f"{field.label} must be answered (yes or no)"))
            elif field.required_if:
                trigger = trigger_field.label if trigger_field is not None else field.required_if
                when = "is yes" if trigger_field is not None and trigger_field.type == "boolean" else "is provided"
                buckets["required"].append(Issue(ERROR, field.id, f"{field.label} is required when {trigger} {when}"))
            else:
                buckets["required"].append(Issue(ERROR, field.id, f"{field.label} is required but empty"))
            continue

        if not present:
            continue

        # Option membership for the two option-bearing types. A ``choice`` holds
        # one value; a ``multichoice`` a comma-separated list, whose selection
        # count is bounded by the generic item-count check below.
        #
        # When ``options_recommended`` is set, the list is advisory: an off-list
        # value is allowed and only flagged as a WARNING (e.g. Bioinformatics'
        # free Keywords box, where any typed term is accepted, or Discrete
        # Mathematics' research-data statement, whose dropdown has an "Other"
        # free-text entry). Otherwise the list is closed. A closed multichoice is
        # not modelled in pydantic, so the ERROR is raised here; a closed
        # ``choice`` is already a ``Literal`` in the schema pass and would only be
        # reported twice. A ``text`` field with options (arXiv's primary_category,
        # drawn from a vocabulary file) holds one value from a closed list too.
        # Options written "code (Label)" also accept the bare code.
        if field.options and field.type in ("choice", "multichoice", "text"):
            applied.add("options")
            accepted = set(accepted_options(field))
            chosen = [c.strip() for c in raw.split(",")] if field.type == "multichoice" else [raw.strip()]
            for choice in chosen:
                if not choice or choice in accepted:
                    continue
                if field.options_recommended:
                    entered = "added as a custom keyword" if field.type == "multichoice" else "entered as custom text"
                    buckets["options"].append(
                        Issue(
                            WARNING,
                            field.id,
                            f"{field.label}: '{choice}' is not in the recommended " f"list; it will be {entered}",
                        )
                    )
                elif field.type in ("multichoice", "text"):
                    if field.options_file:
                        valid = f"run '{options_command(venue.slug, field.id)}' to list the valid options"
                    else:
                        valid = " | ".join(field.options)
                    buckets["options"].append(
                        Issue(
                            ERROR,
                            field.id,
                            f"{field.label}: '{choice}' is not a valid option " f"({valid})",
                        )
                    )

        # Inclusive item-count bounds on any multi-item field: a multichoice's
        # chosen options, or the lines of a filelist/authorlist/list-style
        # textarea (e.g. at least 4 recommended reviewers, at most N figures).
        if field.min_count is not None or field.max_count is not None:
            applied.add("counts")
            count = _count_items(field, raw)
            if field.min_count is not None and count < field.min_count:
                buckets["counts"].append(
                    Issue(
                        ERROR,
                        field.id,
                        f"{field.label}: provide at least {field.min_count} (got {count})",
                    )
                )
            if field.max_count is not None and count > field.max_count:
                buckets["counts"].append(
                    Issue(
                        ERROR,
                        field.id,
                        f"{field.label}: provide at most {field.max_count} (got {count})",
                    )
                )

        # Maximum word/character length on a text-bearing value (e.g. abstract).
        if field.word_count is not None or field.character_count is not None or field.min_character_count is not None:
            applied.add("lengths")
            buckets["lengths"].extend(_check_length(field, raw))

        # URL-format constraint on a text-bearing value (e.g. data availability).
        if field.require_url:
            applied.add("urls")
            buckets["urls"].extend(_check_url(field, raw))

        if field.type == "file":
            applied.add("uploads")
            buckets["uploads"].extend(_check_file_field(field, raw))
            length = _check_manuscript_length(field, raw, values)
            if any(getattr(field, name) is not None for name in ("max_words", "max_words_before_refs", "max_pages", "max_pages_before_refs", "max_words_by", "max_words_before_refs_by", "max_pages_by", "max_pages_before_refs_by")):
                applied.add("manuscript_length")
            buckets["manuscript_length"].extend(length)

        if field.type == "filelist":
            applied.add("uploads")
            # Each line is 'path | ...'; only the first segment is the path.
            for line in (l.strip() for l in raw.splitlines()):
                if line:
                    path_part = line.split("|", 1)[0].strip()
                    buckets["uploads"].extend(_check_file_field(field, path_part))

        # A required confirmation (e.g. author consent, ``confirm: true``) must
        # read as yes. An informational yes/no question accepts either answer.
        # Skip the unparseable case, which the schema pass already flagged.
        if field.type == "boolean" and field.required and field.confirm and not _truthy_bool(raw) and _falsey_bool(raw):
            buckets["required"].append(Issue(ERROR, field.id, f"{field.label} must be confirmed (set to yes)"))

        if field.type == "authorlist":
            applied.add("authors")
            buckets["authors"].extend(_check_authors(field, raw))
        elif field.type == "textarea" and field.fields:
            # A pipe-delimited structured textarea (funding, suggested
            # reviewers, ...): enforce its required columns per line.
            applied.add("subfields")
            buckets["subfields"].extend(_check_subfields(field, raw))

    form = "Submission form"
    titles = {
        "required": ("form.required", "Required fields are filled and confirmations given", form),
        "options": ("form.options", "Choices are valid options", form),
        "counts": ("form.counts", "Item counts within the form's limits", form),
        "lengths": ("form.lengths", "Text within the form's word/character limits", form),
        "urls": ("form.urls", "Link fields hold http(s) URLs", form),
        "authors": ("form.authors", "Author list is complete (names, corresponding author, emails)", form),
        "subfields": ("form.subfields", "Structured entries (funding, reviewers, ...) are complete", form),
        "uploads": ("files.uploads", "Upload files exist and have accepted types and sizes", "Files"),
        "manuscript_length": ("files.manuscript_length", "Manuscript within the portal's word/page limits", "Files"),
    }
    for key, (check_id, title, category) in titles.items():
        if key in applied:
            checks.record(check_id, title, category, _status_for(buckets[key]), issues=buckets[key])
        else:
            checks.record(check_id, title, category, report.NOT_APPLICABLE)


def _author_name(author: dict) -> str:
    """A display name for one parsed author line.

    Most venues carry a single ``name`` column, but some split it into
    ``first_name``/``last_name`` (Nature's eJournalPress form has separate name
    boxes). This returns whichever the column set provides, so the author-list
    checks work regardless of which convention a venue declares.
    """
    if author.get("name"):
        return author["name"].strip()
    parts = [author.get("first_name", "").strip(), author.get("last_name", "").strip()]
    return " ".join(p for p in parts if p)


# Author columns covered by dedicated checks, so the generic per-column
# presence check below skips them: the name identifier (handled by the
# name-or-OpenReview-ID check) and the ``corresponding`` flag (a yes/no marker
# whose empty value means "no", never "missing").
_AUTHOR_SPECIAL = {"name", "first_name", "last_name", "open_review_id", "corresponding"}


def _check_authors(field: Field, raw: str) -> list[Issue]:
    issues: list[Issue] = []
    authors = parse_authors(raw, field.fields)
    if not authors:
        issues.append(Issue(ERROR, field.id, "at least one author is required"))
        return issues
    specs = _subfield_specs(field.fields or DEFAULT_AUTHOR_FIELDS)
    # Most venues carry a "corresponding" (and "email") column, but some author
    # column sets do not -- AAAI 2027's OpenReview list is
    # ``open_review_id | name | email_suffixes | reciprocal_reviewer`` with no
    # corresponding author. Only apply the corresponding-author checks when the
    # column is actually present so those venues don't KeyError here.
    columns = [name for name, _ in specs]
    has_corresponding = "corresponding" in columns
    if has_corresponding:
        corresponding = [a for a in authors if a["corresponding"]]
        if len(corresponding) == 0:
            issues.append(
                Issue(
                    ERROR,
                    field.id,
                    "no corresponding author marked (set the last field to 'yes')",
                )
            )
        elif len(corresponding) > 1:
            names = ", ".join(_author_name(a) for a in corresponding)
            issues.append(
                Issue(
                    ERROR,
                    field.id,
                    f"{len(corresponding)} corresponding authors marked: {names} " "(mark exactly one)",
                )
            )
    # Columns required (unmarked, no ``?``) on every author line, minus the ones
    # with their own dedicated checks. When ``email`` is one of them, every
    # author needs an email; otherwise only the corresponding author does (the
    # historical rule), enforced in the fallback below.
    required_cols = [name for name, required in specs if required and name not in _AUTHOR_SPECIAL]
    email_required_all = "email" in required_cols
    for a in authors:
        # A name identifies the author, unless the column set offers an
        # OpenReview ID and this line supplies one instead.
        name_present = bool(_author_name(a)) or bool(a.get("open_review_id", "").strip())
        if not name_present:
            issues.append(Issue(ERROR, field.id, "an author line is missing a name"))
        who = _author_name(a) or "an author"
        # OpenReview venues (ICLR, AAAI): without an ID the portal finds the
        # author by a name search, which can match no profile or several.
        # Conflict lists are excluded -- a conflict with no profile is skipped.
        if "open_review_id" in columns and "author" in field.id.lower() and name_present and not a.get("open_review_id", "").strip():
            issues.append(Issue(WARNING, field.id, f"author '{who}' has no Open Review ID; every author needs an OpenReview profile, and without the ID the portal adds them by a name search that can match no profile or several (IDs look like ~Ada_Lovelace1)"))
        for col in required_cols:
            if not str(a.get(col, "")).strip():
                issues.append(Issue(ERROR, field.id, f"author '{who}' is missing {col.replace('_', ' ')}"))
    if has_corresponding and not email_required_all and "email" in columns:
        for a in authors:
            if a["corresponding"] and not a.get("email"):
                issues.append(
                    Issue(
                        ERROR,
                        field.id,
                        f"corresponding author '{_author_name(a)}' has no email",
                    )
                )
    return issues


def _check_subfields(field: Field, raw: str) -> list[Issue]:
    """Presence check for the required columns of a pipe-delimited multi-item
    field (funding, suggested reviewers, ...).

    Each non-empty line is split on ``|`` into the columns named by
    ``field.fields``; a required (unmarked) column left blank is an error. This
    is the generic counterpart to :func:`_check_authors`, used for every
    structured field except ``authorlist`` (which has its own richer, author-
    specific checks). Only columns before the first optional-or-missing one
    matter to the portal, but we report each required column independently so
    the message names exactly what is missing.
    """
    specs = _subfield_specs(field.fields or [])
    required = [(pos, name) for pos, (name, req) in enumerate(specs) if req]
    if not required:
        return []
    issues: list[Issue] = []
    line_no = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        line_no += 1
        cells = [c.strip() for c in line.split("|")]
        for pos, name in required:
            value = cells[pos] if pos < len(cells) else ""
            if not value:
                issues.append(Issue(ERROR, field.id, f"{field.label}: line {line_no} is missing {name.replace('_', ' ')}"))
    return issues
