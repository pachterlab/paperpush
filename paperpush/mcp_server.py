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
from .subfile import default_filename, parse as parse_subfile, render_template, write_template
from .validate import Issue
from .validate import validate as _run_validate

logger = logging.getLogger(__name__)

# How long to let a shelled-out `paperpush login` run before giving up. The
# default sign-in check opens a real browser, so this has to leave room for a
# page load and a CAPTCHA, but not hang the client forever.
LOGIN_TIMEOUT_SECONDS = 300.0

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

    This is the same data `paperpush schema <venue>` prints.
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
        "max_upload_mb": resolved.max_upload_mb,
        "file_type_options": resolved.file_type_options,
        "default_subfile_name": default_filename(resolved),
        "fields": field_schema(resolved),
    }


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
) -> dict[str, Any]:
    """Create a blank `.sub` submission template for a venue.

    `path` is where to write it; pass an absolute path, since a relative one is
    resolved against the server's working directory rather than yours. Omit it
    to write `<venue>.sub` there.

    Refuses to clobber an existing file unless `overwrite` is true.
    """
    resolved = _venue_or_error(venue)
    target = _resolve(path, None) if path else Path(default_filename(resolved)).resolve()
    try:
        written = write_template(resolved, target, fill_defaults=fill_defaults, overwrite=overwrite)
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
) -> dict[str, Any]:
    """Run paperpush's pre-submission checks on a `.sub` file.

    Checks required fields, value types and option sets, length and count
    limits, and that referenced upload files exist, are the right type, and fit
    the portal's size caps.

    Two heavier passes are on by default and can be turned off:
    `check_links` probes the URLs cited in the manuscript for 404s and
    still-private repositories (needs network); `check_sensitive` scans the
    referenced files for API keys, passwords, private keys, GPS data in
    figures, and LaTeX source comments.

    `errors` block submission; `warnings` are advisory. `ok` is true when there
    are no errors.
    """
    path, text, venue = _load_subfile(subfile, manuscript_dir)
    issues = _run_validate(parse_subfile(text), venue, check_sensitive=check_sensitive, check_links=check_links)
    errors = [i for i in issues if i.is_error]
    warnings = [i for i in issues if not i.is_error]
    return {
        "path": str(path),
        "venue": venue.slug,
        "ok": not errors,
        "errors": _issue_dicts(errors),
        "warnings": _issue_dicts(warnings),
    }


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


def login_status(venue: Optional[str] = None) -> dict[str, Any]:
    """Report which venues have stored submission credentials.

    Call this before `login` (or before telling the user to run
    `paperpush login`) -- it is the cheap, offline check for whether a sign-in
    is needed at all. Never touches the network and never returns a secret.

    With no argument, lists every stored login. With `venue` (a slug or a `.sub`
    path), reports just that one, including the `login_command` to run if it is
    not signed in.

    Venues that submit through a shared portal (the AAAS and Nature families)
    share one credential, stored under the base venue; each sibling is listed
    separately here, since that is how an author thinks about it.
    """
    if venue is not None:
        slug = _slug_from_reference(venue)
        resolved = _venue_or_error(slug)
        base = venues_pkg.submission_base(resolved.slug)
        credential = credentials.get_credential(base)
        entry: dict[str, Any] = {
            "venue": resolved.slug,
            "credential_stored_under": base,
            "shares_login_with_base_venue": base != resolved.slug,
            "logged_in": credential is not None,
        }
        if credential is None:
            entry["login_command"] = " ".join(["paperpush", "login", base])
            return entry
        entry["method"] = credential.method
        entry["identity"] = credential.orcid if credential.method == "orcid" else credential.username
        if credential.display_name:
            entry["display_name"] = credential.display_name
        entry["stored_in"] = "OS keyring" if credentials.credential_location(base) == "keyring" else "config file"
        return entry

    logins: list[dict[str, Any]] = []
    for credential in credentials.list_credentials():
        identity = credential.orcid if credential.method == "orcid" else credential.username
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
    command = _paperpush_command("login", base)
    display_command = " ".join(["paperpush", "login", base])

    existing = credentials.get_credential(base)
    if existing is not None:
        identity = existing.orcid if existing.method == "orcid" else existing.username
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
            "reason": (
                f"No credentials are stored for {base} and signing in needs a username and "
                "password. Ask the user to run this command in their own terminal -- it will "
                "prompt them, and the password never passes through this conversation."
            ),
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
            "reason": (
                f"`{display_command}` did not finish within {timeout_seconds:g}s. The sign-in "
                "check may be waiting on a CAPTCHA or two-factor prompt. Ask the user to run "
                "the command themselves, or retry with verify=false to store the credentials "
                "without checking them."
            ),
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

    def __init__(self, process: subprocess.Popen, log_path: Path, log_file: IO[bytes], venue: str, subfile: Path):
        self.process = process
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
        return {
            "pid": self.pid,
            "venue": self.venue,
            "subfile": str(self.subfile),
            "started_at": self.started_at,
            "running": exit_code is None,
            "exit_code": exit_code,
            "log_path": str(self.log_path),
            "log_tail": lines[-LOG_TAIL_LINES:],
        }


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


def submit(
    subfile: str,
    manuscript_dir: Optional[str] = None,
    headless: bool = False,
    new_session: bool = False,
    close_on_failure: bool = False,
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
    - The venue must already have stored credentials. Without them the run would
      stall on a password prompt it has no terminal to ask on, so it comes back
      as `action_required` with the `paperpush login` command instead. Call
      `login_status` first to avoid the round trip.

    `headless` runs without a visible window -- only useful for a smoke test,
    since the point is a browser the author can look at. `new_session` discards
    the saved browser session and signs in fresh, for use after switching
    accounts. `close_on_failure` shuts the browser if the run breaks partway;
    the default leaves it open at the step that failed so the page can be read.
    """
    path, _, venue = _load_subfile(subfile, manuscript_dir)

    try:
        venues_pkg.get_runner(venue.slug)
    except KeyError:
        raise ValueError(f"no submission runner is registered for {venue.slug!r}; " "this venue can be prepared but not driven") from None

    checked = validate_subfile(str(path), check_links=False, check_sensitive=False)
    if not checked["ok"]:
        return {
            "status": "blocked",
            "venue": venue.slug,
            "subfile": str(path),
            "errors": checked["errors"],
            "reason": "The .sub file has validation errors. Fix them and call submit again; no browser was opened.",
        }

    base = venues_pkg.submission_base(venue.slug)
    if credentials.get_credential(base) is None:
        return {
            "status": "action_required",
            "venue": venue.slug,
            "credential_stored_under": base,
            "subfile": str(path),
            "command": f"paperpush login {base}",
            "reason": (
                f"No credentials are stored for {base}. Submitting would stall on a sign-in "
                "prompt with no terminal to answer it, so nothing was launched. Ask the user "
                "to run this command in their own terminal first."
            ),
        }

    command = _paperpush_command("submit", str(path))
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

    run = SubmissionRun(process, log_path, log_file, venue.slug, path)
    _RUNS[run.pid] = run
    logger.info("submit: launched %s for %s as pid %d (log: %s)", venue.slug, path, run.pid, log_path)

    return {
        "status": "running",
        "pid": run.pid,
        "venue": venue.slug,
        "subfile": str(path),
        "log_path": str(log_path),
        "headless": headless,
        "next_step": (
            "The browser is opening and the wizard will fill itself in from the .sub file. "
            "Poll submit_status for progress. It stops before the final submit -- tell the "
            "user to review the form in the browser and click submit themselves, then call "
            "submit_close to shut the browser."
        ),
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
    _RUNS.pop(pid, None)
    _reap_finished_runs()

    snapshot["status"] = "already_finished" if already_finished else "closed"
    return snapshot


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
    autofill_subfile,
    login_status,
    login,
    submit,
    submit_status,
    submit_close,
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
        instructions=(
            f"paperpush {__version__} -- prepare and check academic manuscript submissions.\n\n"
            "Typical flow: list_supported_venues -> describe_venue -> create_subfile -> "
            "autofill_subfile (you extract the values from the manuscript; the tool applies "
            "them) -> validate_subfile -> login_status -> submit.\n\n"
            "Pass absolute paths: this server has its own working directory, so a relative "
            "path means something different here than it does to you. Tools that take a "
            "manuscript_dir resolve relative paths against it.\n\n"
            "submit fills the venue's form and stops before the final submit button, leaving "
            "the browser open for the author to review and send themselves -- so it returns a "
            "pid, not a finished run. Follow it with submit_status; call submit_close when the "
            "author says they are done. It needs a stored login (check login_status first) and "
            "a .sub file that passes validation; it refuses rather than opening a browser "
            "otherwise.\n\n"
            "Signing in is the one step you cannot do for the user: login hands back a "
            "`paperpush login <venue>` command for them to run in their own terminal."
        ),
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
