"""Pre-submission checks for a filled-in ``.sub`` file.

``validate`` takes a parsed :class:`~paperpush.subfile.SubFile` plus its
:class:`~paperpush.database.Venue` definition and returns a list of
:class:`Issue` objects. Errors must be fixed before a submission can proceed;
warnings are advisory. The checks here are deliberately venue-agnostic and
driven by the field metadata in the database.

Two layers of checking run, both reporting through the same :class:`Issue`
list so the author sees one combined, actionable summary:

* A pydantic model built from the venue's field schema enforces the value
  rules encoded in ``venues.json``: a ``choice`` field's value must be one of
  its ``options``; a ``filelist``'s per-line file-type subfield must be one of
  its ``type_options``; a ``boolean`` must read as yes/no; and no unknown field
  ids are allowed.
* Hand-written checks cover what a schema cannot: required-but-empty fields,
  files that must exist on disk (with PDF sanity), and author-list shape.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from pydantic import AfterValidator, BeforeValidator, ConfigDict, ValidationError, create_model

from .database import Field, Venue, options_command
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

    @property
    def is_error(self) -> bool:
        return self.level == ERROR


def _truthy_bool(value: str) -> bool:
    return value.strip().lower() in _TRUE


def _falsey_bool(value: str) -> bool:
    return value.strip().lower() in _FALSE


# The legacy author column order, used by venues that don't declare their own
# ``fields`` list. Most venues follow this; Bioinformatics (ScholarOne) is the
# exception and declares its own order in venues.json. The ``?`` suffixes mark
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
    in ``venues.json`` and enforced as a ``Literal``, so the allowed values live
    in one place (the database) and are not duplicated in this module.

    ``custom_validator``, when given, is a venue-specific check contributed by
    the venue's runner module (see
    :func:`paperpush.venues.get_field_validators`) -- a callable that takes
    the field's raw string and returns it, or raises ``ValueError`` with a clear
    message. It is layered on top of the base type as an ``AfterValidator`` so a
    venue can enforce rules the generic field metadata cannot express (for
    example that Nature's subject paths exist in the category tree).
    """
    if field.type == "choice" and field.options:
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
    ``text``/``textarea`` field; either or both may be set. Words are
    whitespace-delimited tokens; characters count the trimmed value.
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
    uploaded file is parsed and measured (see :mod:`paperpush.manuscript`).
    When the measure cannot be taken for the file's format -- a word count of a
    ``.doc`` binary, or a page count of a format without fixed pagination -- the
    limit is reported as unchecked (a WARNING) rather than assumed satisfied or
    violated.
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
    # (limit, counter, scope phrase). An empty scope means the whole document.
    for limit, count, scope in (
        (word_before, manuscript.words_before_references, "before references"),
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
                    f"could not count words in {path.name} ('{path.suffix}' is not supported for word counting); the {limit}-word limit was not checked",
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
        (page_before, manuscript.pages_before_references, "before references", "page counts can only be verified for PDF"),
        (page_total, manuscript.total_pages, "", "page counts can only be verified for PDF or a Word (.docx) file with a saved page count"),
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
                    f"could not count pages in {path.name} ({unsupported}); the {limit}-page limit was not checked",
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

    paths = list(_iter_upload_paths(venue, values))
    logger.info("Checking %d upload file(s) for unreachable links (%s)", len(paths), venue.slug)
    return _findings_to_issues(sensitive.scan_links(paths))


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

    paths = list(_iter_upload_paths(venue, values))
    logger.info("Scanning %d upload file(s) for sensitive information (%s)", len(paths), venue.slug)
    findings = sensitive.scan_paths(paths)
    findings.extend(sensitive.scan_missing_code_link(paths))

    issues = _findings_to_issues(findings)
    issues.extend(_arxiv_cleaner_reminder(venue, findings))
    return issues


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
            "arXiv publishes your uploaded LaTeX source publicly; run arxiv_latex_cleaner on " "your source to strip the comments and unneeded files above before submitting",
        )
    ]


def validate(subfile: SubFile, venue: Venue, *, check_sensitive: bool = True, check_links: bool = True) -> list[Issue]:
    """Return all issues found in ``subfile`` against ``venue``.

    Combines schema-level checks (allowed options, file types, booleans, and
    stray fields, via pydantic) with required-field, file-existence, and
    author-list checks. Every problem is reported, so the author can fix them
    in one pass.

    When ``check_sensitive`` is set (the default), the referenced upload files
    are scanned for information that was never meant to be shared -- secrets, GPS
    metadata, editable-document links, and LaTeX source comments (see
    :func:`_sensitive_issues`) -- and reported as advisory WARNINGs. When
    ``check_links`` is set (also the default), the URLs those files cite are
    probed and any that are unreachable (404/gone, including a still-private
    GitHub repo) are likewise flagged; this makes network requests.
    """
    logger.info(
        "Validating %s: %d field(s) (sensitive-scan=%s, link-check=%s)",
        venue.slug,
        len(venue.fields),
        check_sensitive,
        check_links,
    )
    issues: list[Issue] = list(_schema_issues(venue, subfile.values))
    values = subfile.values

    issues.extend(_upload_size_issues(venue, values))
    if check_links:
        issues.extend(_link_issues(venue, values))
    if check_sensitive:
        issues.extend(_sensitive_issues(venue, values))

    for field in venue.fields:
        raw = values.get(field.id, "")
        present = bool(raw.strip())

        # A field is required either unconditionally (``required``) or when its
        # ``required_if`` target carries a value (e.g. funding_country is required
        # only once funding is given).
        required = field.required or bool(field.required_if and values.get(field.required_if, "").strip())

        # Required fields must carry a value. A required confirmation boolean (a
        # consent checkbox, ``confirm: true``) must additionally be affirmative,
        # so an empty one asks to be set to yes rather than just "filled in". A
        # plain required yes/no question only needs an answer.
        if required and not present:
            if field.type == "boolean" and field.confirm:
                issues.append(Issue(ERROR, field.id, f"{field.label} must be confirmed (set to yes)"))
            elif field.type == "boolean":
                issues.append(Issue(ERROR, field.id, f"{field.label} must be answered (yes or no)"))
            elif field.required_if:
                trigger = next((f.label for f in venue.fields if f.id == field.required_if), field.required_if)
                issues.append(Issue(ERROR, field.id, f"{field.label} is required when {trigger} is provided"))
            else:
                issues.append(Issue(ERROR, field.id, f"{field.label} is required but empty"))
            continue

        if not present:
            continue

        # Multichoice option validity is not modelled in pydantic (no such fields
        # today); keep a direct check so the capability is not silently lost. Its
        # selection-count bounds are handled by the generic item-count check below.
        # When ``options_recommended`` is set, the list is advisory: an off-list
        # value is allowed and only flagged as a WARNING (e.g. Bioinformatics' free
        # Keywords box, where any typed term is accepted).
        if field.type == "multichoice" and field.options:
            for choice in (c.strip() for c in raw.split(",")):
                if choice and choice not in field.options:
                    if field.options_recommended:
                        issues.append(
                            Issue(
                                WARNING,
                                field.id,
                                f"{field.label}: '{choice}' is not in the recommended " "list; it will be added as a custom keyword",
                            )
                        )
                    else:
                        if field.options_file:
                            valid = f"run '{options_command(venue.slug, field.id)}' to list the valid options"
                        else:
                            valid = " | ".join(field.options)
                        issues.append(
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
            count = _count_items(field, raw)
            if field.min_count is not None and count < field.min_count:
                issues.append(
                    Issue(
                        ERROR,
                        field.id,
                        f"{field.label}: provide at least {field.min_count} (got {count})",
                    )
                )
            if field.max_count is not None and count > field.max_count:
                issues.append(
                    Issue(
                        ERROR,
                        field.id,
                        f"{field.label}: provide at most {field.max_count} (got {count})",
                    )
                )

        # Maximum word/character length on a text-bearing value (e.g. abstract).
        issues.extend(_check_length(field, raw))

        # URL-format constraint on a text-bearing value (e.g. data availability).
        issues.extend(_check_url(field, raw))

        if field.type == "file":
            issues.extend(_check_file_field(field, raw))
            issues.extend(_check_manuscript_length(field, raw, values))

        if field.type == "filelist":
            # Each line is 'path | ...'; only the first segment is the path.
            for line in (l.strip() for l in raw.splitlines()):
                if line:
                    path_part = line.split("|", 1)[0].strip()
                    issues.extend(_check_file_field(field, path_part))

        # A required confirmation (e.g. author consent, ``confirm: true``) must
        # read as yes. An informational yes/no question accepts either answer.
        # Skip the unparseable case, which the schema pass already flagged.
        if field.type == "boolean" and field.required and field.confirm and not _truthy_bool(raw) and _falsey_bool(raw):
            issues.append(Issue(ERROR, field.id, f"{field.label} must be confirmed (set to yes)"))

        if field.type == "authorlist":
            issues.extend(_check_authors(field, raw))
        elif field.type == "textarea" and field.fields:
            # A pipe-delimited structured textarea (funding, suggested
            # reviewers, ...): enforce its required columns per line.
            issues.extend(_check_subfields(field, raw))

    errors = sum(1 for i in issues if i.is_error)
    logger.info(
        "Validated %s subfile: %d issue(s) (%d error(s), %d warning(s))",
        venue.slug,
        len(issues),
        errors,
        len(issues) - errors,
    )
    return issues


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
