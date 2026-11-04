"""Model Context Protocol server exposing the paperpush toolkit.

Runs paperpush as an MCP server over stdio so any MCP client (Claude Code,
Claude Desktop, an IDE extension) can browse venues, create and inspect ``.sub``
files, autofill them, validate them, and check which venues have stored
credentials -- with typed arguments and structured results instead of parsed
CLI output.

    paperpush-mcp                 # or: python -m paperpush.mcp_server

Two things shape the design:

**stdio is the protocol channel.** On stdio transport the server's *stdout* is
the JSON-RPC stream, so nothing here may print to it. Every tool returns a
value; diagnostics go through :mod:`logging`, which
:func:`paperpush._logging.configure_logging` points at stderr.

**Paths are resolved explicitly.** The server process has its own working
directory, unrelated to the client's, so a bare ``figures/fig1.pdf`` would mean
different things to the two of them. Every path argument is resolved against an
explicit base -- ``manuscript_dir`` where there is one -- and absolute paths are
always safe. See :func:`_resolve`.

The tool functions below are plain functions with no MCP dependency;
:func:`build_server` registers them on a ``FastMCP`` instance. That keeps the
``mcp`` package an optional install and lets the tests call the tools directly.

**Submitting runs detached.** ``paperpush submit`` fills the portal and then
parks in :func:`~paperpush.venues.common.hold_open`, blocking on ``input()``
forever so the browser stays up for the author to review -- Playwright closes
any browser it launched the moment the process exits. A blocking
``subprocess.run`` therefore cannot both leave that window open *and* return, so
:func:`submit` launches the run in its own session and hands back a pid;
:func:`submit_status` and :func:`submit_close` cover the rest of its life. See
:func:`submit` for why its stdin is a pipe rather than the inherited stream.

One thing is deliberately *not* exposed: ``login``'s credential prompts.
Collecting a username and password needs the user's own terminal, and routing a
password through a tool call would write it into the model's context and the
client's transcript. :func:`login` is a *guard* instead: it reports an existing
login, and otherwise hands back the exact command for the user to run. It shells
out only when both ``PAPERPUSH_USERNAME`` and ``PAPERPUSH_PASSWORD`` are already
set in the server's own environment.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Literal, Optional

from . import __version__
from . import credentials
from . import venues as venues_pkg
from ._logging import configure_logging
from .autofill import autofill as _apply_autofill
from .autofill import field_schema, parse_extraction
from .database import Venue, get_venue, list_venues
from .subfile import default_filename, parse as parse_subfile, render_template, resolve_paths, write_template
from .validate import Issue
from .validate import validate_report as _run_validate_report

logger = logging.getLogger(__name__)

# How long to let a shelled-out `paperpush login` run before giving up. The
# default sign-in check opens a real browser, so this has to leave room for a
# page load and a CAPTCHA, but not hang the client forever.
LOGIN_TIMEOUT_SECONDS = 300.0
# The headless `login --check` never waits on a human, so it needs only the page
# loads of one sign-in.
LOGIN_CHECK_TIMEOUT_SECONDS = 120.0

# How many lines of a submission's output to hand back per status check. Enough
# to see the step it reached or the traceback that stopped it, without pasting a
# whole run into the conversation.
LOG_TAIL_LINES = 40

Confidence = Literal["low", "medium", "high"]

# Submissions launched by this server, keyed by pid.
#
# This registry is not just bookkeeping for `submit_status` -- it is what keeps
# each run alive. The child blocks on `input()` to hold its browser open, so its
# stdin must stay open; that stdin is the write end of a pipe owned by the Popen
# object here. Drop the reference and CPython finalizes the Popen, closes the
# pipe, the child's `input()` raises EOFError, the process exits, and Playwright
# takes the browser down with it. See :func:`submit`.
_RUNS: dict[int, "SubmissionRun"] = {}
# Recovery directories of closed runs, so recovery_report still finds them by pid.
_CLOSED_RECOVER_DIRS: dict[int, Path] = {}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve(path: str, base: Optional[str] = None) -> Path:
    """Turn a client-supplied path into an absolute one.

    An absolute path is taken as given. A relative one is resolved against
    ``base`` when supplied (the manuscript directory, for tools that have one)
    and against the server's working directory otherwise -- which is rarely
    what the caller means, hence the docstring advice to pass absolute paths.
    """
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    root = Path(base).expanduser() if base else Path.cwd()
    return root / p


def _venue_or_error(slug: str) -> Venue:
    """Look up a venue, raising a ValueError the client can show verbatim."""
    try:
        return get_venue(slug)
    except KeyError:
        known = ", ".join(v.slug for v in list_venues())
        raise ValueError(f"unknown venue {slug!r}. Supported venues: {known}") from None


def _slug_from_reference(reference: str) -> str:
    """Resolve a venue reference that may be a slug or a ``.sub`` file.

    Accepts ``biorxiv``, ``biorxiv.sub``, or ``/path/to/biorxiv.sub`` -- callers
    (and the people instructing them) tend to use these interchangeably. For an
    existing file the venue recorded *inside* it wins over the filename, since
    the file is the authority on which venue it targets.
    """
    reference = reference.strip()
    if not reference:
        raise ValueError("a venue slug or .sub file path is required")
    path = Path(reference).expanduser()
    if path.suffix != ".sub":
        return reference
    if path.is_file():
        try:
            recorded = parse_subfile(path.read_text(encoding="utf-8")).venue
        except OSError:
            recorded = ""
        if recorded:
            return recorded
    return path.stem


def _load_subfile(subfile: str, base: Optional[str] = None) -> tuple[Path, str, Venue]:
    """Read a ``.sub`` file and resolve the venue it targets.

    Returns the resolved path, the file's text, and the venue. As in the CLI,
    the venue comes from the file's own ``venue:`` line, falling back to the
    filename stem for a file that predates it or was hand-written.
    """
    path = _resolve(subfile, base)
    if not path.is_file():
        raise ValueError(f"no .sub file at {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    return path, text, _venue_or_error(parse_subfile(text).venue or path.stem)


def _issue_dicts(issues: list[Issue]) -> list[dict[str, str]]:
    return [{"level": i.level, "field": i.field, "message": i.message} for i in issues]


def _paperpush_command(*args: str) -> list[str]:
    """Build an argv that invokes this same paperpush install.

    ``sys.executable -m paperpush`` rather than a bare ``paperpush``: the server
    may well be running in a different virtualenv than the one whose script sits
    on ``PATH``, and the subprocess must share this process's venue database and
    credential store.
    """
    return [sys.executable, "-m", "paperpush", *args]


# ---------------------------------------------------------------------------
# Venue catalogue
# ---------------------------------------------------------------------------


# Why a venue with author guidelines but no submission form has no login or submit.
_VALIDATE_ONLY_REASON = "paperpush can check a manuscript against this venue's author guidelines (validate) but cannot submit to it, so there is nothing to log in to."


def list_supported_venues(include_deprecated: bool = False) -> list[dict[str, Any]]:
    """List the venues paperpush can submit to.

    Returns one summary per venue: slug (the identifier every other tool takes),
    display name, kind, portal URL, and how many fields its submission form has.
    Call `describe_venue` for the fields themselves.
    """
    return [
        {
            "slug": venue.slug,
            "name": venue.name,
            "full_name": venue.full_name,
            "venue_type": venue.venue_type,
            "description": venue.description,
            "submission_url": venue.submission_url,
            "submission_guide": venue.submission_guide,
            "login_required": venue.has_form and venues_pkg.login_required(venue.slug),
            "supports_validate": venue.supports_validate,
            "supports_submit": venue.supports_submit,
            "field_count": len(venue.fields),
            "required_field_count": sum(1 for f in venue.fields if f.required),
        }
        for venue in list_venues(include_deprecated=include_deprecated)
    ]


def describe_venue(venue: str) -> dict[str, Any]:
    """Describe one venue's submission form, field by field.

    Each field carries its id, label, type, whether it is required, and its
    autofill role: `extract` (copy from the manuscript), `classify` (a judgment
    call to confirm), `filemap` (a path to a file on disk), or `never` (a
    policy/consent field a human must set). Constraints -- option lists,
    accepted file extensions, word and character caps, item counts -- come
    along too, so a caller can propose values that will pass validation.
    `manuscript_requirements` carries what the venue's author guidelines
    demand of the manuscript itself (formats, length, sections, statements,
    title page, figures, references); `validate_subfile` checks against it.

    This is the same data `paperpush schema <venue>` prints, plus the
    requirements `paperpush requirements <venue>` prints.
    """
    resolved = _venue_or_error(venue)
    return {
        "slug": resolved.slug,
        "name": resolved.name,
        "full_name": resolved.full_name,
        "venue_type": resolved.venue_type,
        "description": resolved.description,
        "submission_url": resolved.submission_url,
        "submission_guide": resolved.submission_guide,
        "site_url": resolved.site_url,
        "login_required": resolved.has_form and venues_pkg.login_required(resolved.slug),
        "supports_validate": resolved.supports_validate,
        "supports_submit": resolved.supports_submit,
        "max_upload_mb": resolved.max_upload_mb,
        "anonymous": resolved.anonymous,
        "file_type_options": resolved.file_type_options,
        "default_subfile_name": default_filename(resolved),
        "fields": field_schema(resolved),
        "manuscript_requirements": _requirements_dict(resolved.slug),
    }


def _requirements_dict(slug: str) -> Optional[dict[str, Any]]:
    """The venue's manuscript requirements as plain data, or None if none are recorded."""
    from .requirements import get_requirements

    reqs = get_requirements(slug)
    return None if reqs is None else reqs.to_dict()


def field_options(venue: str, field: str, path: Optional[list[str]] = None) -> dict[str, Any]:
    """List the values a `choice` or `multichoice` field accepts.

    Use this rather than guessing at a controlled vocabulary: several venues
    draw their options from shared keyword files too long to inline in a
    template or an error message.

    A few fields are trees rather than flat lists (Nature's subject areas, for
    one). For those, pass `path` as the category names to descend before
    listing -- `path=["Biological sciences"]` lists that category's children.
    An empty `path` lists the top level.
    """
    resolved = _venue_or_error(venue)
    definition = next((f for f in resolved.fields if f.id == field), None)
    if definition is None:
        known = ", ".join(f.id for f in resolved.fields)
        raise ValueError(f"venue {resolved.slug!r} has no field {field!r}. Fields: {known}")

    # A venue may register a custom lister for a field whose values are not a
    # flat list; it takes precedence and is the only path that accepts `path`.
    lister = venues_pkg.get_field_option_listers(resolved.slug).get(field)
    if lister is not None:
        try:
            options = lister(*(path or []))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return {"venue": resolved.slug, "field": field, "hierarchical": True, "path": path or [], "options": options}

    if path:
        raise ValueError(f"{resolved.slug}.{field} is a flat list; it does not take a path")
    return {
        "venue": resolved.slug,
        "field": field,
        "hierarchical": False,
        "path": [],
        "options": definition.options or [],
        "options_are_recommendations": definition.options_recommended,
    }


# ---------------------------------------------------------------------------
# .sub files
# ---------------------------------------------------------------------------


def create_subfile(
    venue: str,
    path: Optional[str] = None,
    fill_defaults: bool = True,
    overwrite: bool = False,
    venue_order: bool = False,
) -> dict[str, Any]:
    """Create a blank `.sub` submission template for a venue.

    Fields are grouped into the sections every venue shares (manuscript type,
    files, details, authors, reviewers, declarations, additional). Set
    `venue_order` to list them in the order the venue's portal asks instead.

    `path` is where to write it; pass an absolute path, since a relative one is
    resolved against the server's working directory rather than yours. Omit it
    to write `<venue>.sub` there.

    Refuses to clobber an existing file unless `overwrite` is true.
    """
    resolved = _venue_or_error(venue)
    target = _resolve(path, None) if path else Path(default_filename(resolved)).resolve()
    try:
        written = write_template(resolved, target, fill_defaults=fill_defaults, overwrite=overwrite, venue_order=venue_order)
    except FileExistsError:
        raise ValueError(f"{target} already exists; pass overwrite=true to replace it") from None
    except OSError as exc:
        raise ValueError(f"cannot write {target}: {exc}") from exc

    return {
        "path": str(written),
        "venue": resolved.slug,
        "field_count": len(resolved.fields),
        "required_field_count": sum(1 for f in resolved.fields if f.required),
        "filled_with_defaults": fill_defaults,
        "venue_order": venue_order,
    }


def read_subfile(subfile: str, manuscript_dir: Optional[str] = None) -> dict[str, Any]:
    """Read a `.sub` file and report which fields still need values.

    Returns the venue it targets, every field's current value, and the ids of
    the required fields that are still empty. Relative paths resolve against
    `manuscript_dir` when given.
    """
    path, text, venue = _load_subfile(subfile, manuscript_dir)
    values = parse_subfile(text).values
    empty_required = [f.id for f in venue.fields if f.required and not values.get(f.id, "").strip()]
    return {
        "path": str(path),
        "venue": venue.slug,
        "values": values,
        "empty_required_fields": empty_required,
    }


def validate_subfile(
    subfile: str,
    manuscript_dir: Optional[str] = None,
    check_links: bool = True,
    check_sensitive: bool = True,
    check_references: bool = True,
    check_manuscript: bool = True,
    anonymous: Optional[bool] = None,
    check_hidden_text: bool = True,
    check_openreview: bool = True,
    ai_results: Optional[list[dict[str, Any]]] = None,
    output: Optional[str] = None,
    colorblind: bool = False,
    initial_submission: bool = False,
    ai_only: bool = False,
    cover_letter_max_age_days: int = 7,
) -> dict[str, Any]:
    """Run paperpush's pre-submission checks on a `.sub` file and report every check.

    Checks required fields, value types and option sets, length and count
    limits, and that referenced upload files exist, are the right type, and fit
    the portal's size caps.

    Three heavier passes are on by default and can be turned off:
    `check_links` probes the URLs cited in the manuscript for 404s and
    still-private repositories (needs network); `check_references` resolves the
    DOIs in the submission's bibliography -- its .bib files, and the
    manuscript's own reference list -- and reports references whose title,
    author, or year does not match the work the DOI points to (also needs
    network);
    `check_sensitive` scans the referenced files for API keys, passwords,
    private keys, GPS data in figures, and LaTeX source comments.
    `check_manuscript` measures the uploads against the venue's author
    guidelines (`manuscript_requirements.json`, also returned by
    `describe_venue`): manuscript format and length (LaTeX is compiled for a
    page count), required sections and statements, title-page items, page
    layout (page size, margins, body font and size, double spacing, line and
    page numbers, template, spelling variant), figures (resolution, lettering
    size and typeface, panel labels, placement, legends), the order figures and
    tables are cited in, whether the title, abstract, authors, keywords,
    and statements in the `.sub` match the manuscript, whether the word,
    figure, and table counts stated on the title page are right, whether the
    cover letter is recently dated and names this venue, whether the abstract
    cites references (or lacks the structure the venue asks for), whether the
    manuscript gives a contact e-mail, whether every reference is cited and how
    many are self-citations, and the prose (double spaces, repeated words,
    punctuation spacing, unbalanced brackets and quotes, common misspellings,
    repetitive word choice).
    `anonymous` checks a double-blind submission for information identifying
    its authors -- their names, emails, IDs and affiliations from the author
    list, author metadata, home-directory paths, acknowledgments -- in the
    attached files and in every linked anonymous.4open.science repository
    (needs network). It defaults to the venue's `anonymous` flag (on for ICLR
    and AAAI); pass true to check any venue.
    `check_hidden_text` looks for white, invisible, microscopic, or off-page
    text in the PDF/LaTeX/Word uploads; a hidden instruction to an AI reviewer
    (prompt injection) is an error. `check_openreview` looks up each author's
    OpenReview profile for OpenReview venues (ICLR, AAAI) using the stored
    login -- missing, ambiguous, or inactive profiles, name or email-suffix
    mismatches, no current position (needs network).

    Some checks need a reader's judgement (lettering in raster figures, the
    guidelines' free-text rules, whether every `.sub` field says what the
    manuscript says). They come back with status `needs_ai` and are listed in
    `ai_tasks`: do each task yourself by reading the files it names, then call
    this tool again with `ai_results` = `[{"id", "status": "pass"|"fail"|
    "unclear"|"not_applicable", "details", "evidence"}]` to merge your verdicts
    (or get the same list from `validation_ai_tasks`). `ai_only` reports just
    those checks and skips the deterministic ones (the manuscript is still read
    to find which apply; it cannot be combined with `check_manuscript=false`).

    `cover_letter_max_age_days` (default 7): the cover letter's date may be at
    most this old; the letter is also checked for the name of a different
    venue left over from an earlier submission.

    `initial_submission` validates the file as an initial submission: every
    error comes back as a warning, so nothing blocks it. Use it when the venue
    lets the initial submission be in any format; that rule, when the venue
    has one, is returned as `initial_submission_rule` either way.

    `checks` lists every check with its status: passed, failed, warning,
    unchecked (could not run here), needs_ai, not_implemented, or
    not_applicable. `errors` block submission; `warnings` are advisory. `ok` is
    true when there are no errors. `output`, an absolute path ending in .pdf,
    .html, .md, or .txt, also saves the colour-coded report there
    (`colorblind` for a colour-blind-friendly palette, the same as
    `paperpush validate --colorblind`).
    """
    from . import report as report_mod
    from .ai_validate import AiOptions, tasks_payload

    path, text, venue = _load_subfile(subfile, manuscript_dir)
    ai = AiOptions.from_results(ai_results, "the agent") if ai_results is not None else None
    result = _run_validate_report(resolve_paths(parse_subfile(text), path.parent), venue, check_sensitive=check_sensitive, check_links=check_links, check_references=check_references, check_manuscript=check_manuscript, check_anonymous=anonymous, check_hidden_text=check_hidden_text, check_openreview=check_openreview, initial_submission=initial_submission, cover_letter_max_age_days=cover_letter_max_age_days, ai=ai, subfile_path=str(path), ai_only=ai_only)
    out: dict[str, Any] = {
        "path": str(path),
        "venue": venue.slug,
        "ok": result.ok,
        "initial_submission_rule": result.initial_submission_rule,
        "initial_submission": result.initial_submission,
        "downgraded": result.downgraded,
        "ai_only": result.ai_only,
        "errors": _issue_dicts(result.errors),
        "warnings": _issue_dicts(result.warnings),
        "counts": result.counts(),
        "checks": [r.to_dict() for r in result.results],
        "ai_tasks": tasks_payload([t for t in result.ai_tasks if t.id in {r.id for r in result.by_status(report_mod.NEEDS_AI)}], venue, str(path))["tasks"],
    }
    if output:
        saved = report_mod.save(result, report_mod.output_path(str(_resolve(output, manuscript_dir)), str(path)), alt_color=colorblind)
        out["report"] = str(saved)
    return out


def validation_ai_tasks(subfile: str, manuscript_dir: Optional[str] = None) -> dict[str, Any]:
    """The validation checks that need a reader's judgement, for you to do yourself.

    Runs the deterministic checks offline (no link, DOI, or OpenReview lookups)
    to find which judgement checks apply -- lettering in raster figures, the
    venue's free-text guideline rules, whether each `.sub` field agrees with
    the manuscript, and so on -- and returns them as tasks: an id, the
    instructions, and the files to read. Do each one, then pass your verdicts
    to `validate_subfile` as `ai_results` (`[{"id", "status": "pass"|"fail"|
    "unclear"|"not_applicable", "details", "evidence"}]`). No API key is
    needed: you are the reader.
    """
    from .ai_validate import tasks_payload

    path, text, venue = _load_subfile(subfile, manuscript_dir)
    result = _run_validate_report(resolve_paths(parse_subfile(text), path.parent), venue, check_links=False, check_references=False, check_openreview=False, subfile_path=str(path))
    return tasks_payload(result.ai_tasks, venue, str(path))


def autofill_subfile(
    subfile: str,
    manuscript_dir: str,
    values: dict[str, Any],
    min_confidence: Confidence = "low",
    output: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write extracted field values into a `.sub` file, through paperpush's gates.

    You do the extraction; this applies it. Read the manuscript yourself, call
    `describe_venue` for the field list and roles, then pass `values` in the
    documented shape:

        {"fields": [{"id": "title", "value": "...", "confidence": "high",
                     "source": "manuscript.pdf p.1"}],
         "unfilled": [["consent", "a policy field only the author can set"]]}

    `confidence` is `high` (copied verbatim / unambiguous), `medium`, or `low`
    -- lower it rather than guessing high. Values below `min_confidence` are
    reported but not written.

    The gates are deliberate and not negotiable from here: `never`-role fields
    are always left alone, `classify`-role and sub-`high` values are written
    but flagged for review, and `filemap` paths are resolved against
    `manuscript_dir`. The result is validated before returning.

    `subfile` may name a file that does not exist yet -- a fresh template is
    rendered from the venue in its filename. Relative paths resolve against
    `manuscript_dir`. Set `dry_run` to see the outcome without writing.
    """
    directory = _resolve(manuscript_dir, None)
    if not directory.is_dir():
        raise ValueError(f"manuscript directory not found: {directory}")

    sub_path = _resolve(subfile, str(directory))
    if sub_path.is_file():
        _, text, venue = _load_subfile(str(sub_path))
        created_from_template = False
    else:
        venue = _venue_or_error(sub_path.stem)
        # render_template is what `subfile` writes; autofill on a missing file
        # starts from the same place rather than an empty document.
        text = render_template(venue, fill_defaults=True)
        created_from_template = True

    try:
        extraction = parse_extraction(values)
    except (TypeError, AttributeError, ValueError) as exc:
        raise ValueError(f"`values` is not a valid extraction payload: {exc}") from exc

    result = _apply_autofill(text, venue, extraction, manuscript_dir=directory, min_confidence=min_confidence)

    out_path = _resolve(output, str(directory)) if output else sub_path
    if not dry_run:
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(result.text, encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot write {out_path}: {exc}") from exc

    def _outcomes(items) -> list[dict[str, str]]:
        return [
            {
                "id": o.id,
                "label": o.label,
                "value": o.value,
                "confidence": o.confidence,
                "source": o.source,
                "note": o.note,
            }
            for o in items
        ]

    return {
        "path": str(out_path),
        "venue": venue.slug,
        "written": not dry_run,
        "started_from_fresh_template": created_from_template,
        "filled": _outcomes(result.filled),
        "needs_review": _outcomes(result.review),
        "skipped": _outcomes(result.skipped),
        "left_for_the_author": [{"id": fid, "reason": reason} for fid, reason in extraction.unfilled],
        # autofill is a preparation step, not the final gate -- it leaves policy
        # fields empty on purpose, so leftover errors here are expected. Run
        # validate_subfile once a human has filled those in.
        "errors": _issue_dicts(result.errors),
        "warnings": _issue_dicts(result.warnings),
    }


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _check_stored_login(base: str, timeout_seconds: float = LOGIN_CHECK_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run `paperpush login <base> --check` and classify the outcome.

    A subprocess, like `login`: the check drives Playwright's sync API, which
    cannot run inside the server's event loop. The CLI prints only the venue and
    the login's identity, never the password.
    """
    command = _paperpush_command("login", base) + ["--check"]
    display_command = f"paperpush login {base} --check"
    logger.info("login_status: running %s", display_command)
    try:
        # Audited for B603: fixed argv list; the only interpolated element is a
        # venue slug already checked against the database by _venue_or_error.
        completed = subprocess.run(  # nosec B603
            command,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds if timeout_seconds > 0 else None,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timed_out", "command": display_command, "reason": f"the sign-in check did not finish within {timeout_seconds:g}s"}
    except OSError as exc:
        raise ValueError(f"could not run {display_command}: {exc}") from exc

    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    if completed.returncode == 0:
        status = "unchecked" if "could not be checked" in completed.stdout else "ok"
    else:
        status = "failed"
    return {"status": status, "command": display_command, "output": output}


def login_status(venue: Optional[str] = None, check: bool = False) -> dict[str, Any]:
    """Report which venues have stored submission credentials.

    Call this before `login` (or before telling the user to run
    `paperpush login`) -- it is the cheap, offline check for whether a sign-in
    is needed at all. By default it never touches the network, and it never
    returns a secret.

    With no argument, lists every stored login. With `venue` (a slug or a `.sub`
    path), reports just that one, including the `login_command` to run if it is
    not signed in.

    `check=true` (needs `venue`) also signs in with the stored credentials in a
    headless browser -- `paperpush login <venue> --check` -- and adds a `check`
    entry: `status` is `ok`, `failed` (e.g. the password changed since login, or
    the portal showed a CAPTCHA / two-factor prompt), `unchecked` (paperpush
    cannot sign in to this venue automatically; treat as a pass) or `timed_out`.
    On `failed` or `timed_out`, `logged_in` is false and `login_command` is set:
    ask the user to run it in their own terminal. Use this right before `submit`.

    Venues that submit through a shared portal (the AAAS and Nature families)
    share one credential, stored under the base venue; each sibling is listed
    separately here, since that is how an author thinks about it.
    """
    if check and venue is None:
        raise ValueError("check=true needs a venue: it signs in to that one venue's portal")
    if venue is not None:
        slug = _slug_from_reference(venue)
        resolved = _venue_or_error(slug)
        base = venues_pkg.submission_base(resolved.slug)
        if not resolved.has_form or not venues_pkg.login_required(base):
            return {
                "venue": resolved.slug,
                "credential_stored_under": base,
                "shares_login_with_base_venue": base != resolved.slug,
                "login_required": False,
                "logged_in": True,
                "reason": _VALIDATE_ONLY_REASON if not resolved.has_form else "This venue uses a public submission form and does not require login.",
            }
        credential = credentials.get_credential(base)
        entry: dict[str, Any] = {
            "venue": resolved.slug,
            "credential_stored_under": base,
            "shares_login_with_base_venue": base != resolved.slug,
            "login_required": True,
            "logged_in": credential is not None,
        }
        if credential is None:
            entry["login_command"] = " ".join(["paperpush", "login", base])
            return entry
        entry["method"] = credential.method
        entry["identity"] = credential.identity
        if credential.display_name:
            entry["display_name"] = credential.display_name
        entry["stored_in"] = "OS keyring" if credentials.credential_location(base) == "keyring" else "config file"
        if check:
            entry["check"] = _check_stored_login(base)
            if entry["check"]["status"] in ("failed", "timed_out"):
                entry["logged_in"] = False
                entry["login_command"] = " ".join(["paperpush", "login", base])
        return entry

    logins: list[dict[str, Any]] = []
    for credential in credentials.list_credentials():
        identity = credential.identity
        for slug in [credential.venue, *venues_pkg.submission_aliases(credential.venue)]:
            logins.append(
                {
                    "venue": slug,
                    "credential_stored_under": credential.venue,
                    "method": credential.method,
                    "identity": identity,
                    "display_name": credential.display_name,
                }
            )
    return {"logged_in_venues": sorted(entry["venue"] for entry in logins), "logins": logins}


def login(
    venue: str,
    verify: bool = True,
    verify_headless: bool = False,
    timeout_seconds: float = LOGIN_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Ensure credentials are stored for a venue, running `paperpush login` if it can.

    Checks `login_status` first and returns `already_logged_in` without doing
    anything when credentials exist -- so it is safe to call unconditionally
    before a submission.

    When there is no stored login, what happens depends on the environment:

    - If `PAPERPUSH_USERNAME` and `PAPERPUSH_PASSWORD` are both set in the
      *server's* environment, it runs `paperpush login <venue>` for real and
      returns `logged_in` or `failed`.
    - Otherwise it returns `action_required` with the exact `command` for the
      user to run in their own terminal.

    There is deliberately no way to pass a username or password to this tool: a
    credential sent as a tool argument ends up in the model's context and the
    client's transcript. Collecting one belongs in the user's terminal.

    `verify` (on by default) opens a browser and checks the credentials against
    the venue's real sign-in before storing them, so a typo surfaces now rather
    than at submit time. That browser may need a human for a CAPTCHA or a
    two-factor prompt, which is why this can time out -- when it does, the user
    should run the command themselves.

    Accepts a slug or a `.sub` path. A venue that submits through another's
    portal is redirected to that base venue, where the shared credential lives.
    """
    slug = _slug_from_reference(venue)
    resolved = _venue_or_error(slug)
    base = venues_pkg.submission_base(resolved.slug)
    if not resolved.has_form or not venues_pkg.login_required(base):
        return {
            "status": "not_required",
            "venue": resolved.slug,
            "credential_stored_under": base,
            "reason": _VALIDATE_ONLY_REASON if not resolved.has_form else "This venue uses a public submission form and does not require login.",
        }
    command = _paperpush_command("login", base)
    display_command = " ".join(["paperpush", "login", base])

    existing = credentials.get_credential(base)
    if existing is not None:
        identity = existing.identity
        return {
            "status": "already_logged_in",
            "venue": resolved.slug,
            "credential_stored_under": base,
            "method": existing.method,
            "identity": identity,
        }

    if not verify:
        command.append("--no-verify")
        display_command += " --no-verify"
    elif verify_headless:
        command.append("--verify-headless")
        display_command += " --verify-headless"

    # The CLI collects the username and password from these env vars when they
    # are set, and prompts otherwise. A prompt has no terminal to read from
    # here, so unless both are already present, hand the command back instead of
    # running a subprocess that can only fail.
    if not (os.environ.get("PAPERPUSH_USERNAME") and os.environ.get("PAPERPUSH_PASSWORD")):
        return {
            "status": "action_required",
            "venue": resolved.slug,
            "credential_stored_under": base,
            "command": display_command,
            "reason": (f"No credentials are stored for {base} and signing in needs a username and " "password. Ask the user to run this command in their own terminal -- it will " "prompt them, and the password never passes through this conversation."),
        }

    logger.info("login: running %s for %s", display_command, base)
    try:
        # Audited for B603 (subprocess without shell=True check): argv is a
        # fixed list, never a shell string, and its only interpolated element is
        # a venue slug already checked against the database by _venue_or_error.
        completed = subprocess.run(  # nosec B603
            command,
            capture_output=True,
            text=True,
            # No terminal to prompt on: any unexpected prompt should hit EOF and
            # fail fast rather than block until the timeout.
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds if timeout_seconds > 0 else None,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timed_out",
            "venue": resolved.slug,
            "credential_stored_under": base,
            "command": display_command,
            "reason": (f"`{display_command}` did not finish within {timeout_seconds:g}s. The sign-in " "check may be waiting on a CAPTCHA or two-factor prompt. Ask the user to run " "the command themselves, or retry with verify=false to store the credentials " "without checking them."),
        }
    except OSError as exc:
        raise ValueError(f"could not run {display_command}: {exc}") from exc

    if completed.returncode == 0 and credentials.get_credential(base) is not None:
        return {
            "status": "logged_in",
            "venue": resolved.slug,
            "credential_stored_under": base,
            "command": display_command,
            "output": completed.stdout.strip(),
        }
    return {
        "status": "failed",
        "venue": resolved.slug,
        "credential_stored_under": base,
        "command": display_command,
        "exit_code": completed.returncode,
        "output": (completed.stderr or completed.stdout).strip(),
        "reason": "The sign-in did not complete. Ask the user to run the command in their own terminal.",
    }


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


class SubmissionRun:
    """A detached ``paperpush submit`` process and the log it is writing."""

    def __init__(self, process: subprocess.Popen, log_path: Path, log_file: IO[bytes], venue: str, subfile: Path, recover_dir: Optional[Path] = None, cdp_port: Optional[int] = None):
        self.process = process
        self.recover_dir = recover_dir
        self.cdp_port = cdp_port
        self.log_path = log_path
        self.log_file = log_file
        self.venue = venue
        self.subfile = subfile
        self.started_at = datetime.now().isoformat(timespec="seconds")

    @property
    def pid(self) -> int:
        return self.process.pid

    def snapshot(self) -> dict[str, Any]:
        """Current state plus the tail of the run's output."""
        exit_code = self.process.poll()
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        report: dict[str, Any] = {
            "pid": self.pid,
            "venue": self.venue,
            "subfile": str(self.subfile),
            "started_at": self.started_at,
            "running": exit_code is None,
            "exit_code": exit_code,
            "log_path": str(self.log_path),
            "log_tail": lines[-LOG_TAIL_LINES:],
        }
        if self.recover_dir is not None:
            from .venues import recovery_channel

            paused = recovery_channel.read_paused(self.recover_dir) if exit_code is None else None
            report["recovery"] = {
                "enabled": True,
                "directory": str(self.recover_dir),
                "paused": paused,
                "resolved_so_far": recovery_channel.read_log(self.recover_dir),
            }
            if paused:
                report["next_step"] = _PAUSED_NEXT_STEP
            elif report["recovery"]["resolved_so_far"]:
                report["recovery"]["report"] = (
                    "Steps of this run needed recovery, so the venue script is probably out of date for everyone. "
                    "Once the author is done, call recovery_report to draft a redacted GitHub issue, show them the draft, "
                    "and give them the link to file it only if they want to. Never file it yourself."
                )
        if self.cdp_port is not None:
            from .venues.recovery_channel import cdp_endpoint

            report["cdp_endpoint"] = cdp_endpoint(self.cdp_port)
        return report


def _submit_log_path(venue: str) -> Path:
    """A fresh log file for one submission run, under paperpush's config dir."""
    directory = credentials.config_dir() / "submit-logs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"{venue}-{stamp}.log"
    # Two runs of the same venue inside one second would collide.
    suffix = 2
    while path.exists():
        path = directory / f"{venue}-{stamp}-{suffix}.log"
        suffix += 1
    return path


def _reap_finished_runs() -> None:
    """Drop finished runs whose output has already been collected."""
    for pid, run in list(_RUNS.items()):
        if run.process.poll() is not None and run.log_file.closed:
            del _RUNS[pid]


_PAUSED_NEXT_STEP = (
    "A step failed and the run is paused in its browser, waiting for you. Call submit_page to see "
    "the page (accessibility snapshot plus a screenshot path) and what the script was trying to do. "
    "Then use submit_act to make the page look the way that step should have left it (click, fill, "
    "select, check, upload a .sub file). Finally call submit_resume with 'resolved' if the step is "
    "now done, 'retry' to have the script try it again, or 'abort' to stop. Never try to send the "
    "submission; it is refused, and that step is the author's."
)


def _supports_recovery(base_slug: str) -> bool:
    """Whether the venue's runner can pause and resume on a failed step."""
    try:
        return bool(getattr(venues_pkg.get_venue_impl(base_slug), "supports_recovery", False))
    except (KeyError, ImportError):
        return False


def _submit_run_dir(venue: str) -> Path:
    """A fresh recovery directory for one submission run."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    directory = credentials.config_dir() / "submit-runs" / f"{venue}-{stamp}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _paused_run(pid: int) -> "SubmissionRun":
    run = _RUNS.get(pid)
    if run is None:
        raise ValueError(f"no submission with pid {pid} was started by this server; " f"known pids: {sorted(_RUNS) or 'none'}")
    if run.recover_dir is None:
        raise ValueError(f"submission {pid} was started without recovery, so it cannot be paused or resumed")
    exit_code = run.process.poll()
    if exit_code is not None:
        raise ValueError(f"submission {pid} has already exited (code {exit_code})")
    return run


def _send(run: "SubmissionRun", command: dict[str, Any]) -> dict[str, Any]:
    from .venues import recovery_channel

    try:
        return recovery_channel.send_command(run.recover_dir, command, is_alive=lambda: run.process.poll() is None)
    except recovery_channel.ChannelError as exc:
        raise ValueError(f"submission {run.pid}: {exc}") from None


def submit(
    subfile: str,
    manuscript_dir: Optional[str] = None,
    headless: bool = False,
    new_session: bool = False,
    close_on_failure: bool = False,
    recover: bool = True,
    expose_cdp: bool = False,
) -> dict[str, Any]:
    """Open the venue's portal and fill in the submission form from a `.sub` file.

    This does **not** submit the manuscript. It signs in, clicks through the
    wizard typing in the values from the file, and stops before the final submit
    button, leaving the browser open on the review page for the author to check
    and send themselves.

    Returns as soon as the run has started, not when it finishes -- the run
    parks with its browser open and would otherwise never return. Use
    `submit_status` to follow it and `submit_close` to shut the browser when the
    author is done. A `pid` in the result means the browser is opening.

    Two preflights run first, and both stop before any browser opens:

    - The `.sub` file is validated. Errors come back as `blocked`, with the same
      list `validate_subfile` gives.
    - A venue that requires login must already have stored credentials. Without
      them the run would stall on a password prompt it has no terminal to ask on,
      so it comes back as `action_required` with the `paperpush login` command
      instead. Loginless public forms skip this preflight. Call `login_status`
      first to tell which case applies.

    `headless` runs without a visible window -- only useful for a smoke test,
    since the point is a browser the author can look at. `new_session` discards
    the saved browser session and signs in fresh, for use after switching
    accounts. `close_on_failure` shuts the browser if the run breaks partway;
    the default leaves it open at the step that failed so the page can be read.

    `recover` (default on, for venues whose runner supports it) pauses the run
    when one step fails instead of failing it. `submit_status` then reports
    `recovery.paused` with what failed. Look at the page with `submit_page`,
    fix it with `submit_act`, and continue with `submit_resume`. The paused run
    waits 30 minutes for that, then fails as it would without `recover`.

    `expose_cdp` also opens a Chrome DevTools Protocol endpoint on 127.0.0.1
    (reported as `cdp_endpoint`), so a client with a shell can attach with
    `playwright-cli attach --cdp=<endpoint>`. Leave it off unless that is
    needed: anything on the machine can then drive the signed-in browser, and
    the guardrails `submit_act` enforces do not apply there.
    """
    path, _, venue = _load_subfile(subfile, manuscript_dir)

    if not venue.has_form:
        raise ValueError(f"{venue.slug} is validate-only: {_VALIDATE_ONLY_REASON}")
    try:
        venues_pkg.get_runner(venue.slug)
    except KeyError:
        raise ValueError(f"no submission runner is registered for {venue.slug!r}; " "this venue can be prepared but not driven") from None

    checked = validate_subfile(str(path), check_links=False, check_sensitive=False, check_references=False, check_openreview=False)
    if not checked["ok"]:
        return {
            "status": "blocked",
            "venue": venue.slug,
            "subfile": str(path),
            "errors": checked["errors"],
            "reason": "The .sub file has validation errors. Fix them and call submit again; no browser was opened.",
        }

    base = venues_pkg.submission_base(venue.slug)
    if venues_pkg.login_required(base) and credentials.get_credential(base) is None:
        return {
            "status": "action_required",
            "venue": venue.slug,
            "credential_stored_under": base,
            "subfile": str(path),
            "command": f"paperpush login {base}",
            "reason": (f"No credentials are stored for {base}. Submitting would stall on a sign-in " "prompt with no terminal to answer it, so nothing was launched. Ask the user " "to run this command in their own terminal first."),
        }

    command = _paperpush_command("submit", str(path))
    recover_dir: Optional[Path] = None
    cdp_port: Optional[int] = None
    if (recover or expose_cdp) and _supports_recovery(base):
        if recover:
            recover_dir = _submit_run_dir(venue.slug)
            command += ["--recover", "agent", "--recover-dir", str(recover_dir)]
        if expose_cdp:
            from .venues.recovery_channel import free_port

            cdp_port = free_port()
            command += ["--cdp-port", str(cdp_port)]
    if headless:
        command.append("--headless")
    if new_session:
        command.append("--new-session")
    if close_on_failure:
        command.append("--close-on-failure")

    log_path = _submit_log_path(venue.slug)
    log_file = log_path.open("wb")
    try:
        # stdin is a pipe we open and never write to. It cannot be inherited --
        # this process's stdin is the client's JSON-RPC stream, and a child
        # reading it would eat the protocol -- and it cannot be DEVNULL, because
        # hold_open's `input()` would hit EOF immediately, the process would
        # exit, and Playwright would close the very browser this call exists to
        # leave open. An open, silent pipe blocks instead, which is the hold.
        #
        # start_new_session detaches the child from this server's process group,
        # so quitting the client does not take the author's browser down mid-review.
        process = subprocess.Popen(  # nosec B603
            command,
            stdin=subprocess.PIPE,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        log_file.close()
        raise ValueError(f"could not start the submission: {exc}") from exc

    run = SubmissionRun(process, log_path, log_file, venue.slug, path, recover_dir=recover_dir, cdp_port=cdp_port)
    _RUNS[run.pid] = run
    logger.info("submit: launched %s for %s as pid %d (log: %s)", venue.slug, path, run.pid, log_path)

    return {
        "status": "running",
        "pid": run.pid,
        "venue": venue.slug,
        "subfile": str(path),
        "log_path": str(log_path),
        "headless": headless,
        "recovery": recover_dir is not None,
        **({"cdp_endpoint": f"http://127.0.0.1:{cdp_port}"} if cdp_port is not None else {}),
        "next_step": ("The browser is opening and the wizard will fill itself in from the .sub file. " "Poll submit_status for progress. If it reports recovery.paused, a step failed: " "inspect it with submit_page, fix it with submit_act, then call submit_resume. " "It stops before the final submit -- tell the " "user to review the form in the browser and click submit themselves, then call " "submit_close to shut the browser."),
    }


def submit_status(pid: Optional[int] = None) -> dict[str, Any]:
    """Check on a submission started by `submit`.

    With no argument, reports every run this server has launched. With `pid`,
    reports just that one.

    Each report says whether the run is still going, its exit code once it is
    not, and the tail of its output -- which is where the step it reached, or
    the traceback that stopped it, will appear.

    A run that is still `running` with the wizard finished is the normal, good
    end state: it is parked holding the browser open for the author.
    """
    if pid is not None:
        run = _RUNS.get(pid)
        if run is None:
            raise ValueError(f"no submission with pid {pid} was started by this server; " f"known pids: {sorted(_RUNS) or 'none'}")
        return run.snapshot()
    return {"submissions": [run.snapshot() for run in _RUNS.values()]}


def submit_close(pid: int) -> dict[str, Any]:
    """Close the browser a submission is holding open, ending the run.

    Call this once the author has reviewed the filled form -- and submitted it
    themselves, if they chose to. Ending the process is what closes the window,
    since Playwright owns the browser it launched.

    Nothing in the portal is undone by this: whatever the wizard typed stays as
    the venue recorded it.
    """
    run = _RUNS.get(pid)
    if run is None:
        raise ValueError(f"no submission with pid {pid} was started by this server; " f"known pids: {sorted(_RUNS) or 'none'}")

    already_finished = run.process.poll() is not None
    if not already_finished:
        run.process.terminate()
        try:
            run.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("submit_close: pid %d ignored terminate; killing", pid)
            run.process.kill()
            run.process.wait(timeout=10)

    if run.process.stdin is not None and not run.process.stdin.closed:
        run.process.stdin.close()
    run.log_file.close()
    snapshot = run.snapshot()
    if run.recover_dir is not None:
        _CLOSED_RECOVER_DIRS[pid] = run.recover_dir
    _RUNS.pop(pid, None)
    _reap_finished_runs()

    snapshot["status"] = "already_finished" if already_finished else "closed"
    return snapshot


def recovery_report(pid: Optional[int] = None, directory: Optional[str] = None) -> dict[str, Any]:
    """Draft a bug report from a submission run whose steps needed recovery.

    When steps of a `submit` run failed and were fixed (with submit_act /
    submit_resume), the run logged what failed and what worked instead. On a
    pip install nobody else sees that log, so the next author hits the same
    broken step. This turns it into a GitHub issue draft for the paperpush
    maintainers: `title`, a Markdown `body`, and a prefilled `issue_url`.

    The draft is redacted (typed values, file paths, e-mail addresses, values
    from the .sub, URL query strings) and screenshots are left out, but it is
    still the author's to share. Show them the title and body, and give them the
    `issue_url` to review and submit themselves if they agree. Never open or
    submit it on their behalf.

    Pass the `pid` of a run this server started (also after submit_close), or
    the run's recovery `directory` (absolute path).
    """
    from .venues import recovery_report as rr

    if directory:
        root = Path(directory).expanduser()
    elif pid is not None:
        run = _RUNS.get(pid)
        root = run.recover_dir if run is not None else _CLOSED_RECOVER_DIRS.get(pid)
        if root is None:
            raise ValueError(f"no submission with pid {pid} and a recovery directory was started by this server")
    else:
        root = rr.latest_run_with_recoveries()
        if root is None:
            raise ValueError("no submission run with recoveries was found; pass pid or directory")
    try:
        return rr.build_report(root).as_dict()
    except FileNotFoundError as exc:
        raise ValueError(str(exc)) from None


def submit_page(pid: int) -> dict[str, Any]:
    """Look at the page a paused submission is stuck on.

    Returns the URL, the step label, what the script was trying to do and the
    error it got (`failure`), an accessibility snapshot of the page
    (`aria_snapshot`, roles and accessible names, which are what `submit_act`
    targets), and the path of a fresh screenshot to view.

    Only works while `submit_status` reports `recovery.paused`.
    """
    return _send(_paused_run(pid), {"op": "snapshot"})


def submit_act(
    pid: int,
    action: Literal["click", "fill", "select", "check", "uncheck", "press", "upload", "wait"],
    target: Optional[dict[str, Any]] = None,
    value: Optional[str] = None,
) -> dict[str, Any]:
    """Do one thing on the page of a paused submission.

    `target` picks the element and names exactly one of:
    `{"role": "button", "name": "Save / Continue"}` (optionally `"exact": true`),
    `{"label": "First Name"}`, `{"text": "No, I don't have funding"}`,
    `{"placeholder": "..."}`, or `{"css": "#reuse_cc_by"}`. Add `"nth": 0` when
    several elements match.

    `value` is the text for `fill`, the option label (or value) for `select`, the
    key for `press` (e.g. `Tab`, `Escape`), an absolute file path for `upload`,
    or milliseconds for `wait` (no target; at most 10000).

    Refused: anything whose label reads as the final submit (`Submit`,
    `Approve`, ...), pressing Enter, and uploading a file the `.sub` does not
    name. The reply has `ok` and, on failure, `error`. Call `submit_page` again
    to see the result.
    """
    command: dict[str, Any] = {"op": action}
    if target is not None:
        command["target"] = target
    if value is not None:
        command["value"] = value
    if action != "wait" and target is None:
        raise ValueError(f"{action} needs a target")
    return _send(_paused_run(pid), command)


def submit_resume(pid: int, resolution: Literal["resolved", "retry", "abort"], note: str = "") -> dict[str, Any]:
    """Let a paused submission carry on.

    - `resolved`: the page is now in the state the failed step should have left
      it in, so the script skips that step and goes on with the next one.
    - `retry`: the obstacle is gone (a dialog closed, a slow page loaded); run
      the failed step again.
    - `abort`: give up. The run fails as it would without recovery, with the
      browser left open at that page.

    `note` says what was wrong and what fixed it, e.g. "button renamed to
    'Next'". It is saved with the run's recovery log so the venue script can be
    fixed later.
    """
    reply = _send(_paused_run(pid), {"op": "resume", "resolution": resolution, "note": note})
    return {**reply, "pid": pid, "next_step": "Poll submit_status: the run continues, and may pause again at a later step."}


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


def venues_resource() -> str:
    """The full catalogue of supported venues, as JSON."""
    return json.dumps(list_supported_venues(), indent=2)


def venue_resource(slug: str) -> str:
    """One venue's submission form and field definitions, as JSON."""
    return json.dumps(describe_venue(slug), indent=2)


# ---------------------------------------------------------------------------
# Server assembly
# ---------------------------------------------------------------------------

TOOLS = (
    list_supported_venues,
    describe_venue,
    field_options,
    create_subfile,
    read_subfile,
    validate_subfile,
    validation_ai_tasks,
    autofill_subfile,
    login_status,
    login,
    submit,
    submit_status,
    submit_page,
    submit_act,
    submit_resume,
    submit_close,
    recovery_report,
)


def _server_class():
    """Return the MCP SDK's server class, whichever release is installed.

    The class was renamed from ``FastMCP`` to ``MCPServer`` in MCP SDK 2.0. The
    parts used here -- ``tool()``, ``resource()``, ``run()`` -- kept the same
    shape across the rename, so supporting both is a matter of finding the
    class. Newer name first.
    """
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP

        return FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise SystemExit("paperpush's MCP server needs the 'mcp' package.\n" "Install it with:  pip install 'paperpush[mcp]'") from exc


def build_server():
    """Build the MCP server with every tool and resource registered."""
    server_class = _server_class()

    # MCP SDK 2.0 reports a server version to the client; 1.x has no such
    # parameter and rejects the keyword, so only pass it where it lands.
    import inspect

    extra: dict[str, Any] = {}
    if "version" in inspect.signature(server_class.__init__).parameters:
        extra["version"] = __version__

    server = server_class(
        "paperpush",
        **extra,
        instructions=(f"paperpush {__version__} -- prepare and check academic manuscript submissions.\n\n" "Typical flow: list_supported_venues -> describe_venue -> create_subfile -> " "autofill_subfile (you extract the values from the manuscript; the tool applies " "them) -> validate_subfile -> login_status -> submit.\n\n" "validate_subfile reports every check (passed, failed, not checked). Checks that need a " "reader's judgement come back as needs_ai with tasks in `ai_tasks` (or from " "validation_ai_tasks): do them yourself and pass your verdicts back as `ai_results`.\n\n" "Pass absolute paths: this server has its own working directory, so a relative " "path means something different here than it does to you. Tools that take a " "manuscript_dir resolve relative paths against it.\n\n" "submit fills the venue's form and stops before the final submit button, leaving " "the browser open for the author to review and send themselves -- so it returns a " "pid, not a finished run. Follow it with submit_status; if a step fails the run pauses " "(recovery.paused) for you to fix with submit_page, submit_act and submit_resume. Call submit_close when the " "author says they are done. If steps needed recovery, offer the author a redacted bug report " "(recovery_report) that they can file so the venue gets fixed for everyone. It needs a .sub file that passes validation and, for " "portals that require authentication, a stored login (check login_status first); " "it refuses rather than opening a browser otherwise.\n\n" "Signing in is the one step you cannot do for the user: login hands back a " "`paperpush login <venue>` command for them to run in their own terminal."),
    )
    for tool in TOOLS:
        server.tool()(tool)
    server.resource("paperpush://venues")(venues_resource)
    server.resource("paperpush://venue/{slug}")(venue_resource)
    return server


def main(argv: Optional[list[str]] = None) -> int:
    """Run the MCP server on stdio."""
    # Diagnostics must go to stderr; stdout carries the JSON-RPC stream.
    configure_logging(verbosity=0)
    logger.info("paperpush %s MCP server starting on stdio", __version__)
    build_server().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
