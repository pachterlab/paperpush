"""FastAPI app behind the paperpush website.

The page (``static/``) holds the ``.sub`` text itself -- in memory, and in the
browser's storage between visits -- so the server keeps no submission state.
What it does keep is a *workspace*: a private directory, named by an
unguessable id, holding the files the author uploaded. A workspace that has not
been used for :data:`DEFAULT_TTL_HOURS` is deleted.

Paths in a ``.sub`` written on the website are bare file names (``paper.pdf``),
the names of uploaded files. Before validating, :func:`localize_values` points
every upload field at the workspace copy of that file -- and only there, so a
``.sub`` naming ``/etc/passwd`` or ``../other/paper.pdf`` cannot reach anything
outside its own workspace. Messages that come back carry workspace paths, which
:func:`_scrub` turns back into bare names before they reach the page.

The page sorts the checks ``validate`` runs into four groups (see
:func:`check_group`): the ones it re-runs itself as the author types
(``live``), the ones only ``validate`` can do because they read the uploaded
files (``validate``), the ones only an LLM can judge (``ai``), and the
guideline rules paperpush has no check for (``manual``). ``/plan`` lists them
before anything has been checked; ``/validate`` runs them.

The downloadable bundle (:func:`write_bundle`) is one folder holding the
``.sub`` and every file it names, figures, tables, and supplementary files
in their own subfolders, with
the ``.sub``'s paths rewritten to match, so ``paperpush validate`` and
``paperpush submit`` work on it unchanged from inside the unzipped folder.

``paperpush subfile|autofill|validate --web`` serves the same page for one
``.sub`` file on the author's own machine (:func:`serve_local`). That
*local session* (:class:`LocalSession`) is the workspace :data:`LOCAL_WS`:
paths in the ``.sub`` are real paths, relative to the directory paperpush was
run from, as on the command line; uploads land next to the manuscript; and
the page saves every edit back to the ``.sub`` file (``PUT /api/local``).
Every local request carries the session's random token in the
``X-Paperpush-Token`` header, so no other web page can reach those files.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .. import __version__
from .. import report as report_mod
from .. import venuefile
from .. import venues as venues_pkg
from .._logging import configure_logging
from ..autofill import effective_role
from ..database import GENERAL_SLUG, Venue, accepted_options, get_venue
from ..filematch import match_files
from ..mcp_server import field_options, list_supported_venues
from ..requirements import any_format_initial_rule, get_requirements
from ..requirements import resolve as resolve_requirements
from ..requirements_check import upload_role_ids
from ..subfile import default_filename, is_subfile_name, subfile_stem
from ..subfile import parse as parse_subfile
from ..subfile import replace_block, replace_scalar, resolve_relative_path
from ..subfile import render_template
from ..validate import validate_report
from .shared import DEFAULT_TTL_DAYS, SharedFormError, SharedForms

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

DEFAULT_TTL_HOURS = 24.0
DEFAULT_MAX_WORKSPACE_MB = 500.0
_CHUNK = 1024 * 1024
_MAX_SUB_BYTES = 1024 * 1024
_MAX_ZIP_MEMBERS = 2000
_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
# How often the server deletes expired workspaces and shared forms on its own,
# so files are gone within this long of their TTL even when nobody visits.
_SWEEP_EVERY_S = 900.0
# Sent with every response (see _SecurityHeaders). No inline script or style:
# the page's scripts and stylesheet are files under /static.
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
_SECURITY_HEADERS = (
    (b"content-security-policy", _CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
)
_HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
# Characters that would break a .sub line: the filelist column separator, line
# breaks, and the path separators that would turn a name back into a path.
_UNSAFE_NAME_CHARS = re.compile(r"[|/\\\x00-\x1f\x7f]")

# Checks the page re-runs in the browser as the author types (static/live.js
# ports them); everything they look at is in the form itself.
LIVE_CHECKS = frozenset(
    {
        "form.schema",
        "form.required",
        "form.options",
        "form.counts",
        "form.lengths",
        "form.urls",
        "form.authors",
        "form.subfields",
        "guidelines.text_fields",
    }
)
GROUPS = ("live", "validate", "ai", "manual")

# Field roles (see requirements_check._ROLE_FIELD_IDS) whose guideline limits
# the page checks as the author types.
_LIVE_ROLE_IDS = {
    "title": ("title",),
    "abstract": ("abstract",),
    "keywords": ("keywords",),
    "running_title": ("running_title", "short_title"),
}
_MANUSCRIPT_IDS = ("manuscript_file", "pdf_file", "combined_pdf", "manuscript")
_TITLE_PAGE_IDS = ("title_page", "title_page_file")
_SUPPLEMENT_IDS = (
    "supplementary_files",
    "supplementary_file",
    "supplementary_material",
    "supplement",
)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


LOCAL_WS = "local"
_LOCAL_LIST_LIMIT = 2000


@dataclasses.dataclass(frozen=True)
class LocalSession:
    """One ``.sub`` file on this machine, opened in the page by ``--web``.

    ``root`` is the directory relative paths in the ``.sub`` resolve against:
    the ``.sub`` file's own folder, as for the CLI. A relative path found only
    in the current working directory is still used from there. ``manuscript_dirs`` are the
    ``autofill -p`` inputs: their files are offered in the page's file pickers
    and to autofill. ``action`` is what the page starts with once it opens:
    ``"autofill"``, ``"validate"``, ``"validate-ai"``, or nothing; a validate
    action ending in ``"+initial"`` ticks "initial submission (any format)"
    first (``validate --web --initial-submission``).
    """

    sub_path: Path
    root: Path
    manuscript_dirs: tuple[Path, ...] = ()
    action: Optional[str] = None
    token: str = dataclasses.field(default_factory=lambda: secrets.token_urlsafe(24))

    @property
    def upload_dir(self) -> Path:
        """Where files added in the page are saved: beside the manuscript, else beside the ``.sub``."""
        return self.manuscript_dirs[0] if self.manuscript_dirs else self.sub_path.parent

    def display(self, path: Path) -> str:
        """``path`` as a ``.sub`` names it: relative to ``root`` when inside it, else absolute."""
        resolved = path.resolve()
        try:
            return str(resolved.relative_to(self.root.resolve()))
        except ValueError:
            return str(resolved)

    def resolve(self, path_text: str) -> Path:
        return Path(resolve_relative_path(path_text, self.root))

    def scrub(self, message: str) -> str:
        """``message`` with absolute paths under ``root`` made relative again."""
        for root in dict.fromkeys((str(self.root.resolve()), str(self.root))):
            message = message.replace(f"{root}{os.sep}", "")
        return message

    def list_files(self) -> list[dict[str, Any]]:
        """The files the page may pick from: the manuscript directories, the upload directory, and whatever the ``.sub`` names."""
        found: dict[str, int] = {}

        def add(path: Path) -> None:
            if len(found) < _LOCAL_LIST_LIMIT and path.is_file():
                found.setdefault(self.display(path), path.stat().st_size)

        for directory in self.manuscript_dirs:
            for path in sorted(directory.rglob("*")):
                if not any(part.startswith((".", "__")) for part in path.relative_to(directory).parts):
                    add(path)
        if self.upload_dir.is_dir():
            for path in sorted(self.upload_dir.iterdir()):
                if not path.name.startswith(".") and not is_subfile_name(path):
                    add(path)
        try:
            sub = parse_subfile(self.sub_path.read_text(encoding="utf-8"))
            venue = get_venue(sub.venue or GENERAL_SLUG)
        except (OSError, KeyError):
            venue = None
        if venue is not None:
            for field in venue.fields:
                if field.type in ("file", "filelist"):
                    for line in sub.values.get(field.id, "").splitlines():
                        if line.split("|", 1)[0].strip():
                            add(self.resolve(line.split("|", 1)[0]))
        return [{"name": name, "size": size} for name, size in found.items()]


@dataclasses.dataclass(frozen=True)
class Settings:
    """Deployment knobs, read from ``PAPERPUSH_WEB_*`` environment variables."""

    data_dir: Path
    ttl_hours: float = DEFAULT_TTL_HOURS
    max_workspace_mb: float = DEFAULT_MAX_WORKSPACE_MB
    # Link, DOI, and anonymity lookups make the server fetch URLs taken from
    # uploaded files. A public server (no ``local`` session) fetches them through
    # :mod:`paperpush.net_guard`, which refuses internal addresses.
    network_checks: bool = True
    # Allow visitors to run autofill and AI checks with their own API key.
    ai: bool = True
    # Set by ``--web``: the page edits this .sub file on the author's machine.
    local: Optional[LocalSession] = None
    # Shared forms ("Share for editing"): stored in this Postgres database
    # (e.g. Neon), else in an SQLite file in data_dir that a redeploy wipes.
    shared: bool = True
    database_url: Optional[str] = None
    shared_ttl_days: float = DEFAULT_TTL_DAYS

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = os.environ.get("PAPERPUSH_WEB_DATA") or str(Path(tempfile.gettempdir()) / "paperpush-web")
        return cls(
            data_dir=Path(data_dir),
            ttl_hours=float(os.environ.get("PAPERPUSH_WEB_TTL_HOURS", DEFAULT_TTL_HOURS)),
            max_workspace_mb=float(os.environ.get("PAPERPUSH_WEB_MAX_WORKSPACE_MB", DEFAULT_MAX_WORKSPACE_MB)),
            network_checks=_env_flag("PAPERPUSH_WEB_NETWORK_CHECKS", True),
            ai=_env_flag("PAPERPUSH_WEB_AI", True),
            shared=_env_flag("PAPERPUSH_WEB_SHARED", True),
            database_url=os.environ.get("PAPERPUSH_WEB_DATABASE_URL") or os.environ.get("DATABASE_URL") or None,
            shared_ttl_days=float(os.environ.get("PAPERPUSH_WEB_SHARED_TTL_DAYS", DEFAULT_TTL_DAYS)),
        )


def visitor_plagiarism(settings: Settings, req: "ValidateRequest", provider: Optional[str], api_key: Optional[str], *, local: bool = False):
    """The :class:`~paperpush.plagiarism_check.PlagiarismOptions` for ``req``, or None when it asks for no scan.

    Like :func:`visitor_llm`: the visitor's Winston AI or Originality.ai key
    comes in the ``X-Plagiarism-Key`` header, is used for this request, and is
    never written anywhere. Only a ``local`` session may fall back on a key set
    in the environment (``WINSTON_API_KEY`` / ``ORIGINALITY_API_KEY``), so a
    public server never spends its own credits.
    """
    if not req.plagiarism:
        return None
    if not settings.network_checks:
        raise HTTPException(400, "the plagiarism scan is switched off on this server (it sends the manuscript's text to an outside service)")
    from .. import plagiarism_check

    api_key = (api_key or "").strip() or None
    if api_key is None and not local:
        raise HTTPException(400, "the plagiarism scan needs a Winston AI or Originality.ai API key: add yours on the page")
    try:
        return plagiarism_check.options(provider or None, api_key, req.plagiarism_accept, req.plagiarism_threshold)
    except ValueError as exc:
        raise HTTPException(400, str(exc).replace(" (or pass --plagiarism-api-key)", ", or add a key on the page")) from None


def visitor_ai_detection(settings: Settings, req: "ValidateRequest", api_key: Optional[str], *, local: bool = False):
    """The :class:`~paperpush.ai_text_check.AiTextOptions` for ``req``, or None when it asks for no AI-text scan.

    Like :func:`visitor_plagiarism`: the visitor's Pangram key comes in the
    ``X-Ai-Detection-Key`` header, is used for this request, and is never
    written anywhere. Only a ``local`` session may fall back on
    ``PANGRAM_API_KEY`` in the environment.
    """
    if not req.ai_detection:
        return None
    if not settings.network_checks:
        raise HTTPException(400, "the AI-text scan is switched off on this server (it sends the manuscript's text to an outside service)")
    from .. import ai_text_check

    api_key = (api_key or "").strip() or None
    if api_key is None and not local:
        raise HTTPException(400, "the AI-text scan needs a Pangram API key: add yours on the page")
    try:
        return ai_text_check.options(None, api_key, req.ai_detection_threshold)
    except ValueError as exc:
        raise HTTPException(400, str(exc).replace(" (or pass --ai-detection-api-key)", ", or add a key on the page")) from None


def ai_detection_env_key() -> bool:
    """Whether a Pangram key is set in the environment (for a local session's page)."""
    from .. import ai_text_check

    return any(os.environ.get(v, "").strip() for v in ai_text_check.KEY_ENV["pangram"])


def plagiarism_env_provider() -> Optional[str]:
    """The plagiarism service whose key is set in the environment, if any (for a local session's page)."""
    from .. import plagiarism_check

    for provider in plagiarism_check.PROVIDERS:
        if any(os.environ.get(v, "").strip() for v in plagiarism_check.KEY_ENV[provider]):
            return provider
    return None


def ai_provider(settings: Settings) -> Optional[str]:
    """The provider configured in the environment, if any (informational only)."""
    if not settings.ai:
        return None
    from ..autofill import detect_provider

    return detect_provider()


def visitor_llm(settings: Settings, provider: Optional[str], api_key: Optional[str], what: str, *, local: bool = False) -> tuple[Optional[str], Optional[str]]:
    """The provider and key to call for ``what`` (autofill or the AI checks).

    The visitor's key is sent with the request in the ``X-LLM-Key`` header,
    used for this request, and never written anywhere. A key is required,
    except in a ``local`` session, where an API key set in the environment
    (as for the CLI) will do.
    """
    if not settings.ai:
        raise HTTPException(400, f"{what} is switched off on this server")
    api_key = (api_key or "").strip() or None
    provider = (provider or "").strip().lower() or None
    if api_key:
        from ..autofill import AutofillApiError, resolve_provider

        try:
            return resolve_provider(provider, api_key=api_key), api_key
        except AutofillApiError as exc:
            raise HTTPException(400, str(exc)) from None
    if local:
        from ..autofill import AutofillApiError, resolve_provider

        try:
            return resolve_provider(provider, alternative="add a key on the page"), None
        except AutofillApiError as exc:
            raise HTTPException(400, str(exc)) from None
    raise HTTPException(400, f"{what} needs an LLM API key: add yours on the page")


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


class _SecurityHeaders:
    """ASGI middleware adding the security headers to every HTTP response.

    A plain ASGI wrapper rather than ``@app.middleware("http")`` so streamed
    responses (validate's NDJSON progress) pass through untouched. API answers
    carry manuscript content, so they are also marked ``no-store``. HSTS is
    sent only behind HTTPS (Render's proxy says so in ``X-Forwarded-Proto``),
    never to a page on 127.0.0.1.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_headers = dict(scope.get("headers") or [])
        https = scope.get("scheme") == "https" or request_headers.get(b"x-forwarded-proto", b"").split(b",")[0].strip() == b"https"
        api = scope.get("path", "").startswith("/api/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                extra = list(_SECURITY_HEADERS)
                if api:
                    extra.append((b"cache-control", b"no-store"))
                if https:
                    extra.append(_HSTS)
                headers.extend((k, v) for k, v in extra if k not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class Workspaces:
    """Per-author upload directories under ``settings.data_dir``."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self) -> str:
        self.sweep()
        ws_id = secrets.token_urlsafe(24)
        (self.root / ws_id / "files").mkdir(parents=True)
        logger.info("created workspace %s", ws_id[:6])
        return ws_id

    def files_dir(self, ws_id: str) -> Path:
        """The upload directory of a live workspace; 404 for anything else."""
        if not _WORKSPACE_ID.match(ws_id):
            raise HTTPException(404, "unknown workspace")
        ws = self.root / ws_id
        files = ws / "files"
        if not files.is_dir():
            raise HTTPException(404, "unknown workspace")
        # Using a workspace keeps it alive.
        os.utime(ws)
        return files

    def delete(self, ws_id: str) -> None:
        files = self.files_dir(ws_id)
        shutil.rmtree(files.parent, ignore_errors=True)

    def sweep(self) -> None:
        """Delete workspaces unused for longer than the TTL."""
        cutoff = time.time() - self.settings.ttl_hours * 3600
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return
        for ws in entries:
            try:
                if ws.is_dir() and ws.stat().st_mtime < cutoff:
                    shutil.rmtree(ws, ignore_errors=True)
                    logger.info("expired workspace %s", ws.name[:6])
            except OSError:
                continue


def safe_name(name: str) -> str:
    """Reduce an uploaded file's name to one safe to store and to put in a ``.sub``."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = _UNSAFE_NAME_CHARS.sub("_", base).strip().lstrip(".")
    if not base:
        raise HTTPException(400, f"unusable file name {name!r}")
    return base[:200]


def _list_files(files: Path) -> list[dict[str, Any]]:
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted(files.iterdir()) if p.is_file()]


def _workspace_bytes(files: Path) -> int:
    return sum(p.stat().st_size for p in files.iterdir() if p.is_file())


def import_zip(archive: Path, files: Path, cap_bytes: int) -> dict[str, Any]:
    """Unpack ``archive`` flat into the workspace ``files``.

    Every member keeps only its file name, the same bare name a ``.sub`` path
    is reduced to by :func:`localize_values`, so a ``.sub`` naming
    ``figures/fig1.pdf`` finds the ``fig1.pdf`` that came out of the zip.
    ``.sub`` members come back as text instead of being stored. Hidden files,
    macOS resource forks, symlinks, and a second member with a name already
    taken in this archive are skipped. The whole import is refused (and
    nothing kept) if it would take the workspace over ``cap_bytes``, counting
    the bytes actually decompressed rather than what the archive claims.
    """
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        raise HTTPException(400, "that is not a readable .zip file") from None
    subs: list[dict[str, str]] = []
    saved: list[str] = []
    paths: list[str] = []  # where each saved file sat in the zip, for :func:`match_files`
    skipped: list[dict[str, str]] = []
    taken: set[str] = set()
    staging = Path(tempfile.mkdtemp(prefix=".import-", dir=files.parent))
    try:
        with zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            if len(members) > _MAX_ZIP_MEMBERS:
                raise HTTPException(413, f"the zip holds {len(members)} files; at most {_MAX_ZIP_MEMBERS} can be imported")
            # Replaced files free their old size.
            budget = cap_bytes - _workspace_bytes(files)
            for member in members:
                parts = member.filename.replace("\\", "/").split("/")
                base = parts[-1]
                if not base or any(p.startswith(".") or p == "__MACOSX" for p in parts):
                    continue
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    skipped.append({"name": member.filename, "reason": "symbolic link"})
                    continue
                if is_subfile_name(base):
                    with zf.open(member) as src:
                        data = src.read(_MAX_SUB_BYTES + 1)
                    if len(data) > _MAX_SUB_BYTES:
                        skipped.append({"name": member.filename, "reason": "too large for a .sub file"})
                    else:
                        subs.append({"name": safe_name(base), "text": data.decode("utf-8", errors="replace")})
                    continue
                name = safe_name(base)
                if name in taken:
                    skipped.append({"name": member.filename, "reason": f"another file in the zip is already named {name}"})
                    continue
                taken.add(name)
                existing = files / name
                if existing.is_file():
                    budget += existing.stat().st_size
                with zf.open(member) as src, (staging / name).open("wb") as out:
                    while chunk := src.read(_CHUNK):
                        budget -= len(chunk)
                        if budget < 0:
                            raise HTTPException(
                                413,
                                f"the zip's contents do not fit: each workspace holds at most {cap_bytes / _CHUNK:g} MB",
                            )
                        out.write(chunk)
                saved.append(name)
                paths.append("/".join(parts[:-1] + [name]))
        for name in saved:
            (staging / name).replace(files / name)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        # Corrupt members, unsupported compression, or encryption.
        raise HTTPException(400, f"could not unpack the zip: {exc}") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"subs": subs, "saved": saved, "paths": paths, "skipped": skipped}


# ---------------------------------------------------------------------------
# .sub <-> workspace paths
# ---------------------------------------------------------------------------


def _bare(path_text: str) -> str:
    return path_text.strip().replace("\\", "/").rsplit("/", 1)[-1]


class MissingFiles(Exception):
    """Files a ``.sub`` names that are not there to bundle."""

    def __init__(self, missing: list[str]):
        super().__init__(", ".join(missing))
        self.missing = missing


def write_bundle(zf: zipfile.ZipFile, folder: str, sub_name: str, text: str, venue: Optional[Venue], resolve, extra: tuple[Path, ...] = ()) -> None:
    """Write the ``.sub`` and every file it names into ``folder/`` in ``zf``.

    ``resolve`` turns a path as the ``.sub`` names it into the file to pack.
    Whatever is in the venue's figures upload box goes in ``folder/figures/``,
    its tables box in ``folder/tables/``, its supplementary box in
    ``folder/supplementary/``, every other file beside the ``.sub``, and the ``.sub`` written into the zip names
    them there, so the unzipped folder validates and submits as it is. Two
    different files with the same name get ``-2``, ``-3`` ... ``extra`` files
    (uploads the ``.sub`` does not name) go beside the ``.sub`` too. Raises
    :class:`MissingFiles`, before writing anything, if a named file is not found.
    """
    placed: dict[Path, str] = {}  # source file -> its path inside the folder
    taken: set[str] = {sub_name}

    def place(src: Path, subdir: str) -> str:
        key = src.resolve()
        if key in placed:
            return placed[key]
        stem, suffix = Path(src.name).stem, Path(src.name).suffix
        name, n = f"{subdir}{src.name}", 1
        while name.lower() in taken:
            n += 1
            name = f"{subdir}{stem}-{n}{suffix}"
        taken.add(name.lower())
        placed[key] = name
        return name

    missing: list[str] = []
    rewrites: list[tuple[str, str, bool]] = []  # field id, new value, is a block
    for field in venue.fields if venue else ():
        if field.type not in ("file", "filelist"):
            continue
        raw = parse_subfile(text).values.get(field.id, "")
        if not raw.strip():
            continue
        subdir = next((f"{role}/" for role in ("figures", "tables", "supplementary") if field.id in upload_role_ids(role)), "")
        lines = raw.splitlines() if field.type == "filelist" else [raw.strip()]
        out_lines = []
        for line in lines:
            path_part, sep, rest = line.partition("|")
            if not path_part.strip():
                out_lines.append(line)
                continue
            src = resolve(path_part.strip())
            if src is None or not src.is_file():
                missing.append(path_part.strip())
                out_lines.append(line)
                continue
            new = place(src, subdir)
            out_lines.append(f"{new} {sep}{rest}" if sep else new)
        rewrites.append((field.id, "\n".join(out_lines), field.type == "filelist"))
    if missing:
        raise MissingFiles(list(dict.fromkeys(missing)))
    for fid, value, block in rewrites:
        text = replace_block(text, fid, value) if block else replace_scalar(text, fid, value)
    for src in extra:
        if src.is_file() and src.resolve() not in placed:
            place(src, "")
    zf.writestr(f"{folder}/{sub_name}", text)
    for src, name in placed.items():
        zf.write(src, f"{folder}/{name}")


def localize_values(venue: Venue, values: dict[str, str], files: Path) -> dict[str, str]:
    """Point every upload path in ``values`` at the workspace copy of that file.

    Only the file name survives, whatever directory the ``.sub`` names, so a
    path can never resolve outside ``files``. For a ``filelist`` only each
    line's leading path segment changes; the ``| label`` columns are kept.
    """
    out = dict(values)
    for field in venue.fields:
        raw = values.get(field.id, "")
        if not raw.strip():
            continue
        if field.type == "file":
            out[field.id] = str(files / _bare(raw))
        elif field.type == "filelist":
            lines = []
            for line in raw.splitlines():
                if not line.strip():
                    lines.append(line)
                    continue
                path_part, sep, rest = line.partition("|")
                lines.append(f"{files / _bare(path_part)} {sep}{rest}" if sep else str(files / _bare(path_part)))
            out[field.id] = "\n".join(lines)
    return out


def _scrub(message: str, files: Path, api_key=None) -> str:
    """``message`` with workspace paths made bare and the visitor's API key(s), should a provider echo one, masked."""
    for key in [api_key] if isinstance(api_key, str) else api_key or ():
        if key:
            message = message.replace(key, "[your API key]")
    return message.replace(f"{files}{os.sep}", "").replace(str(files), "")


def _scrub_checks(checks, *api_keys: Optional[str]):
    """``checks`` (the report payload) with each of ``api_keys`` masked wherever a service echoed it."""
    keys = [k for k in api_keys if k]
    if not keys:
        return checks
    import json

    text = json.dumps(checks)
    for key in keys:
        text = text.replace(key, "[your API key]")
    return json.loads(text)


def _stream_progress(run, files: Path, api_key=None):
    """Yield NDJSON lines for ``run(progress)``: progress events, then its result.

    ``run`` works in a thread and reports each stage through ``progress``; this
    generator relays them as ``{"progress": {"done", "total", "label"}}`` lines
    and ends with ``{"result": ...}``, or ``{"error": ...}`` if ``run`` raised.
    """
    import json
    import queue

    events: queue.Queue = queue.Queue()
    sentinel = object()

    def progress(done: int, total: int, label: str) -> None:
        events.put({"progress": {"done": done, "total": total, "label": label}})

    def work() -> None:
        try:
            events.put({"result": run(progress)})
        except HTTPException as exc:
            events.put({"error": _scrub(str(exc.detail), files, api_key)})
        except Exception as exc:  # noqa: BLE001 -- report it to the page
            logger.exception("validate failed")
            events.put({"error": _scrub(f"validation failed ({type(exc).__name__}: {exc})", files, api_key)})
        finally:
            events.put(sentinel)

    threading.Thread(target=work, name="paperpush-validate", daemon=True).start()
    while True:
        event = events.get()
        if event is sentinel:
            return
        yield json.dumps(event) + "\n"


def localize_local(venue: Venue, values: dict[str, str], local: LocalSession) -> dict[str, str]:
    """Make every relative upload path in ``values`` absolute against the local session's root."""
    out = dict(values)
    for field in venue.fields:
        raw = values.get(field.id, "")
        if field.type not in ("file", "filelist") or not raw.strip():
            continue
        lines = []
        for line in raw.splitlines():
            path_part, sep, rest = line.partition("|")
            if not path_part.strip():
                lines.append(line)
                continue
            path = str(local.resolve(path_part))
            lines.append(f"{path} {sep}{rest}" if sep else path)
        out[field.id] = "\n".join(lines)
    return out


def _localized_sub(text: str, files: Path, local: Optional[LocalSession] = None):
    sub = parse_subfile(text)
    venue = _venue_or_404(sub.venue or GENERAL_SLUG)
    if local is not None:
        sub.values = localize_local(venue, sub.values, local)
    else:
        sub.values = localize_values(venue, sub.values, files)
    return sub, venue


# ---------------------------------------------------------------------------
# Venue data for the page
# ---------------------------------------------------------------------------


def _venue_or_404(slug: str) -> Venue:
    try:
        return get_venue(slug)
    except KeyError:
        raise HTTPException(404, f"unknown venue {slug!r}") from None


def _field_dict(venue: Venue, field) -> dict[str, Any]:
    data = dataclasses.asdict(field)
    data["role"] = effective_role(field)
    # Values the live checks accept: the options plus, for "code (Label)"
    # options (arXiv categories), the bare codes the .sub actually stores.
    data["accepted_options"] = accepted_options(field)
    # Fields whose options form a tree (Nature's subject areas) are browsed a
    # level at a time through /options rather than shipped as one list.
    data["hierarchical"] = field.id in venues_pkg.get_field_option_listers(venue.slug)
    return data


def _first_field(venue: Venue, ids: tuple[str, ...], types: tuple[str, ...] = ("file", "filelist")):
    by_id = {f.id: f for f in venue.fields}
    return next((by_id[i] for i in ids if i in by_id and by_id[i].type in types), None)


def manuscript_field(venue: Venue):
    """The upload field holding the main manuscript (the box at the top of the page)."""
    return _first_field(venue, _MANUSCRIPT_IDS, ("file",))


def accepted_manuscript_formats(venue: Venue) -> list[str]:
    """Extensions the manuscript upload takes: the portal's list, else the guidelines'."""
    field = manuscript_field(venue)
    if field is not None and field.accept:
        return list(field.accept)
    reqs = get_requirements(venue.slug)
    if reqs is not None and reqs.manuscript.formats:
        return list(reqs.manuscript.formats)
    return []


def live_rules(venue: Venue, values: dict[str, str]) -> dict[str, dict[str, Any]]:
    """The author-guideline limits on form fields, for the page to check as the author types.

    Mirrors ``requirements_check._text_fields_check``: a limit the field itself
    declares (``word_count``, ``max_count``, ...) wins, so only the guideline
    limits the form does not already carry are listed.
    """
    base = get_requirements(venue.slug)
    if base is None:
        return {}
    reqs = resolve_requirements(base, values)
    by_id = {f.id: f for f in venue.fields}
    rules: dict[str, dict[str, Any]] = {}

    def put(role: str, **limits: Any) -> None:
        for fid in _LIVE_ROLE_IDS[role]:
            if fid in by_id:
                kept = {k: v for k, v in limits.items() if v not in (None, [], False)}
                if kept:
                    rules[fid] = kept
                return

    def own(role: str, attr: str):
        fid = next((i for i in _LIVE_ROLE_IDS[role] if i in by_id), None)
        return getattr(by_id[fid], attr) if fid else None

    a, t, k = reqs.abstract, reqs.title_page, reqs.keywords
    put(
        "abstract",
        max_words=a.max_words if own("abstract", "word_count") is None else None,
        min_words=a.min_words,
        max_characters=(a.max_characters if own("abstract", "character_count") is None else None),
        structured_headings=list(a.structured_headings or []) if a.structured else None,  # shown as a hint
    )
    put(
        "title",
        max_characters=(t.title_max_characters if own("title", "character_count") is None else None),
        max_words=t.title_max_words if own("title", "word_count") is None else None,
    )
    put(
        "running_title",
        max_characters=(t.running_title_max_characters if own("running_title", "character_count") is None else None),
    )
    put(
        "keywords",
        min_keywords=k.min if own("keywords", "min_count") is None else None,
        max_keywords=k.max if own("keywords", "max_count") is None else None,
    )
    return rules


def venue_schema_issues(venue: Venue, report) -> list[dict[str, str]]:
    """The ``form.schema`` findings of the venue's own field validators.

    ``static/checks.js`` ports the generic schema but not these (Nature's
    subject tree, ...), so the plan hands them to the page to show as it types.
    """
    custom = set(venues_pkg.get_field_validators(venue.slug))
    if not custom:
        return []
    result = next((r for r in report.results if r.id == "form.schema"), None)
    if result is None:
        return []
    return [{"level": i.level, "field": i.field, "message": i.message} for i in result.issues if i.field in custom]


# How the page shows the placeholder "no venue" (:func:`paperpush.database.general_venue`):
# the first option in the venue picker, for checking files before choosing a venue.
GENERAL_OPTION: dict[str, Any] = {
    "slug": GENERAL_SLUG,
    "name": "General",
    "full_name": "General (no specific venue)",
    "venue_type": "general",
    "description": "The usual files and metadata, checked against the rules that apply to any manuscript (no venue's author guidelines).",
}


def _house_rules_options() -> list[dict[str, Any]]:
    """A picker option per publisher with house rules, for a journal paperpush does not list."""
    from ..database import publisher_venue
    from ..requirements import list_publishers

    options = []
    for pub in list_publishers():
        if not pub.has_rules:
            continue
        venue = publisher_venue(pub.slug)
        options.append({"slug": venue.slug, "name": f"Other {pub.display_name} journal", "full_name": f"Another {pub.display_name} journal (checked against {pub.display_name}'s house rules)", "venue_type": "journal", "publisher": pub.slug, "description": venue.description, "keywords": [], "submission_url": "", "submission_guide": pub.site_url, "login_required": False, "supports_validate": True, "supports_submit": False})
    return options


def venue_list() -> list[dict[str, Any]]:
    """The venues for the picker: "General" first, then every supported venue, then each publisher's house rules."""
    general = {
        **GENERAL_OPTION,
        "keywords": [],
        "submission_url": "",
        "submission_guide": "",
        "login_required": False,
        "supports_validate": True,
        "supports_submit": False,
    }
    return [general, *list_supported_venues(), *_house_rules_options()]


def venue_detail(slug: str) -> dict[str, Any]:
    venue = _venue_or_404(slug)
    reqs = get_requirements(venue.slug)
    ms = manuscript_field(venue)
    shown = GENERAL_OPTION if venue.is_general else {}
    return {
        "slug": venue.slug,
        "name": shown.get("name", venue.name),
        "full_name": shown.get("full_name", venue.full_name),
        "venue_type": shown.get("venue_type", venue.venue_type),
        "description": shown.get("description", venue.description),
        "submission_url": venue.submission_url,
        "submission_guide": venue.submission_guide,
        "site_url": venue.site_url,
        "anonymous": venue.anonymous,
        "max_upload_mb": venue.max_upload_mb,
        "has_form": venue.has_form,
        "supports_submit": venue.supports_submit,
        "login_required": venue.has_form and venues_pkg.login_required(venue.slug),
        "subfile_name": default_filename(venue),
        "template": render_template(venue, fill_defaults=True),
        "fields": [_field_dict(venue, f) for f in venue.fields],
        "manuscript_field": ms.id if ms is not None else None,
        "manuscript_formats": accepted_manuscript_formats(venue),
        "manuscript_requirements": None if reqs is None else reqs.to_dict(),
        # Non-empty when the initial submission may be in any format: the page
        # enables its "initial submission" checkbox only then.
        "initial_submission_rule": any_format_initial_rule(venue.slug),
    }


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_group(result: dict[str, Any]) -> str:
    """Which block of the page a check belongs in (one of :data:`GROUPS`)."""
    if result["id"] in LIVE_CHECKS:
        return "live"
    if result["id"].startswith("ai.") or result.get("source") == "ai" or result["status"] == report_mod.NEEDS_AI:
        return "ai"
    if result["status"] == report_mod.NOT_IMPLEMENTED:
        return "manual"
    return "validate"


# The CLI flag a "switched off" note names means nothing on the website.
_SWITCHED_OFF = re.compile(r"switched off \(--[a-z._-]+\)")


_PLAGIARISM_OFF = re.compile(r"^off by default: run with --plagiarism.*$")
_AI_DETECTION_OFF = re.compile(r"^off by default: run with --ai-detection.*$")


def _web_detail(detail: str, files: Path) -> str:
    detail = _AI_DETECTION_OFF.sub("off by default: tick AI-text scan next to Validate (sends the manuscript's text to Pangram; costs credits on your account)", detail)
    detail = _PLAGIARISM_OFF.sub("off by default: tick Plagiarism scan next to Validate (sends the manuscript's text to Winston AI or Originality.ai; costs credits on your account)", detail)
    detail = detail.replace("network checks disabled (--disable-network-checks)", "network checks disabled (the box next to Validate)")
    return _SWITCHED_OFF.sub("switched off on this server", _scrub(detail, files))


def _report_payload(report, files: Path, *, with_status: bool = True) -> list[dict[str, Any]]:
    checks = []
    for result in report.to_dict()["checks"]:
        item = {
            "id": result["id"],
            "title": result["title"],
            "category": result["category"],
            "group": check_group(result),
            "applicable": result["status"] != report_mod.NOT_APPLICABLE,
        }
        if with_status:
            item["status"] = result["status"]
            item["source"] = result.get("source", "")
            item["detail"] = _web_detail(result.get("detail", ""), files)
            item["issues"] = [{**i, "message": _scrub(i["message"], files)} for i in result.get("issues", [])]
        elif result["status"] == report_mod.NOT_APPLICABLE:
            # Why it does not apply yet (usually: no manuscript uploaded).
            item["detail"] = _web_detail(result.get("detail", ""), files)
        checks.append(item)
    return checks


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class TextRequest(BaseModel):
    text: str


class PickRequest(BaseModel):
    multiple: bool = False
    prompt: str = "Choose a file"


def native_picker_available() -> bool:
    """Whether a local session can open the Finder's file picker (macOS only)."""
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def pick_files_native(prompt: str, multiple: bool) -> list[Path]:
    """Open the Finder's "choose file" dialog; the chosen paths, or ``[]`` if cancelled."""
    prompt = prompt.replace("\\", "").replace('"', "'")[:200]
    script = f'set chosen to choose file with prompt "{prompt}"'
    if multiple:
        script += " with multiple selections allowed"
    script += """
set out to ""
if class of chosen is list then
  repeat with f in chosen
    set out to out & POSIX path of f & linefeed
  end repeat
else
  set out to POSIX path of chosen
end if
out"""
    proc = subprocess.run(["osascript", "-e", "activate", "-e", script], capture_output=True, text=True)
    if proc.returncode != 0:
        if "-128" in proc.stderr:  # the author pressed Cancel
            return []
        raise RuntimeError(proc.stderr.strip() or "osascript failed")
    return [Path(line) for line in proc.stdout.splitlines() if line.strip()]


class SharedCreateRequest(BaseModel):
    ciphertext: str
    token: str


class SharedUpdateRequest(BaseModel):
    ciphertext: str
    base_version: int


class MatchRequest(BaseModel):
    paths: list[str]


class VenueDocRequest(BaseModel):
    """The "Add venue" editor's document (see paperpush.venuefile.doc_from_json)."""

    doc: dict[str, Any]
    filename: str = "VENUE.txt"
    # Editing an existing venue: its slug being taken is expected, not an error.
    replacing: bool = False


class ValidateRequest(BaseModel):
    text: str
    ai: bool = False
    check_links: bool = True
    check_references: bool = True
    # False: the page's "disable network checks" box (paperpush validate --disable-network-checks).
    network: bool = True
    anonymous: Optional[bool] = None
    # Report every formatting error as a warning; integrity errors stay (paperpush validate --initial-submission).
    initial_submission: bool = False
    # Report every suggestion on style as a warning (paperpush validate --strict-style).
    strict_style: bool = False
    # Also run the plagiarism scan (paperpush validate --plagiarism) with the
    # visitor's key from the X-Plagiarism-Key header. Costs credits on their account.
    plagiarism: bool = False
    # Sources that may match (paperpush validate --plagiarism-accept): domains, pages, or DOIs.
    plagiarism_accept: list[str] = []
    # Percent of unaccepted matching text that makes a warning (--plagiarism-threshold).
    plagiarism_threshold: Optional[float] = None
    # Also run the AI-text detection (paperpush validate --ai-detection) with the
    # visitor's Pangram key from the X-Ai-Detection-Key header. Costs credits on their account.
    ai_detection: bool = False
    # Percent of AI-generated text that makes a similarity finding (--ai-detection-threshold).
    ai_detection_threshold: Optional[float] = None
    # Answer with NDJSON: {"progress": {...}} lines as each stage starts, then
    # one {"result": {...}} (or {"error": "..."}) line. The page's progress bar.
    stream: bool = False


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.network_checks and settings.local is None:
        # The checks fetch URLs from visitors' files: never let one reach the server's own network.
        from .. import net_guard

        net_guard.install()
    workspaces = Workspaces(settings)
    # Not for --web: a form shared from this computer would be out of reach.
    shared_forms = (
        SharedForms(database_url=settings.database_url, sqlite_path=settings.data_dir / "shared_forms.sqlite3", ttl_days=settings.shared_ttl_days)
        if settings.shared and settings.local is None
        else None
    )
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # Expire workspaces and shared forms on a timer, not only when someone
        # creates a workspace, so the stated retention holds on a quiet server.
        stop = threading.Event()

        def sweeper() -> None:
            while not stop.wait(_SWEEP_EVERY_S):
                try:
                    workspaces.sweep()
                    if shared_forms is not None:
                        shared_forms.maybe_sweep()
                except Exception:  # a failed cleanup must not stop the next one
                    logger.exception("scheduled sweep failed")

        threading.Thread(target=sweeper, name="paperpush-sweep", daemon=True).start()
        try:
            yield
        finally:
            stop.set()

    app = FastAPI(title="paperpush", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(_SecurityHeaders)

    def local_session(token: Optional[str]) -> LocalSession:
        local = settings.local
        if local is None:
            raise HTTPException(404, "paperpush is not editing a local .sub file")
        if not token or not secrets.compare_digest(token, local.token):
            raise HTTPException(403, "this page is not the one paperpush opened; rerun the command with --web")
        return local

    def workspace(ws_id: str, token: Optional[str]) -> tuple[Path, Optional[LocalSession]]:
        """The directory upload paths resolve in, and the local session if this is one."""
        if ws_id == LOCAL_WS:
            local = local_session(token)
            return local.root, local
        return workspaces.files_dir(ws_id), None

    def upload_local(local: LocalSession, files: list[UploadFile]) -> dict[str, Any]:
        target = local.upload_dir
        target.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        for upload_file in files:
            dest = target / safe_name(upload_file.filename or "")
            partial = target / f".{dest.name}.part"
            with partial.open("wb") as out:
                shutil.copyfileobj(upload_file.file, out, _CHUNK)
            partial.replace(dest)
            saved.append(local.display(dest))
        logger.info("local: saved %d file(s) to %s", len(saved), target)
        return {"saved": saved, "files": local.list_files()}

    @app.get("/api/info")
    def info() -> dict[str, Any]:
        return {
            "version": __version__,
            "network_checks": settings.network_checks,
            "ai": settings.ai,
            "ai_provider": ai_provider(settings),
            # Run locally (--web), a plagiarism key in the environment will do.
            "plagiarism_provider": plagiarism_env_provider() if settings.local is not None and settings.network_checks else None,
            "ai_detection_env": bool(settings.local is not None and settings.network_checks and ai_detection_env_key()),
            "max_workspace_mb": settings.max_workspace_mb,
            "ttl_hours": settings.ttl_hours,
            "local": settings.local is not None,
            "native_picker": settings.local is not None and native_picker_available(),
            "shared": {"durable": shared_forms.durable, "ttl_days": shared_forms.ttl_days} if shared_forms else None,
        }

    @app.get("/api/local")
    def get_local(x_paperpush_token: Optional[str] = Header(None)) -> dict[str, Any]:
        """The local .sub file this page edits, and what to do once it opens."""
        local = local_session(x_paperpush_token)
        try:
            text = local.sub_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(500, f"cannot read {local.sub_path}: {exc}") from None
        return {
            "path": local.display(local.sub_path),
            "venue": parse_subfile(text).venue or GENERAL_SLUG,
            "text": text,
            "action": local.action,
            "workspace": LOCAL_WS,
            "files": local.list_files(),
        }

    @app.get("/api/local/ping")
    def ping_local(x_paperpush_token: Optional[str] = Header(None)) -> dict[str, Any]:
        """Keep-alive from an open page (a background preview server stops when idle)."""
        local_session(x_paperpush_token)
        return {"ok": True}

    @app.post("/api/local/pick")
    def pick_local(req: PickRequest, x_paperpush_token: Optional[str] = Header(None)) -> dict[str, Any]:
        """Open the Finder on the author's machine and return the files they choose, in place (nothing is copied)."""
        local = local_session(x_paperpush_token)
        if not native_picker_available():
            raise HTTPException(501, "no native file picker on this system")
        try:
            paths = pick_files_native(req.prompt, req.multiple)
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from None
        return {"files": [{"name": local.display(p), "size": p.stat().st_size} for p in paths if p.is_file()]}

    @app.put("/api/local")
    def save_local(req: TextRequest, x_paperpush_token: Optional[str] = Header(None)) -> dict[str, Any]:
        """Write the page's .sub text back to the file."""
        local = local_session(x_paperpush_token)
        parsed = parse_subfile(req.text)
        if not parsed.venue and not parsed.values:
            raise HTTPException(400, "the text has no '@venue:' line or fields, so it is not a .sub file; not saved")
        partial = local.sub_path.with_name(f".{local.sub_path.name}.part")
        try:
            partial.write_text(req.text, encoding="utf-8")
            partial.replace(local.sub_path)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise HTTPException(500, f"cannot write {local.display(local.sub_path)}: {exc}") from None
        return {"saved": True, "path": local.display(local.sub_path)}

    @app.get("/api/venues")
    def venues() -> list[dict[str, Any]]:
        return venue_list()

    @app.get("/api/venues/{slug}")
    def venue(slug: str) -> dict[str, Any]:
        return venue_detail(slug)

    @app.get("/api/venues/{slug}/fields/{field_id}/options")
    def options(slug: str, field_id: str, path: Optional[str] = None) -> dict[str, Any]:
        _venue_or_404(slug)
        parts = [p.strip() for p in path.split("|")] if path else []
        try:
            return field_options(slug, field_id, parts or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/venues/{slug}/match")
    def match(slug: str, req: MatchRequest) -> dict[str, Any]:
        """Which of the venue's upload fields each file belongs in, by its name and folder (no AI)."""
        venue = _venue_or_404(slug)
        return match_files(venue, req.paths[:_MAX_ZIP_MEMBERS]).to_dict(venue)

    @app.post("/api/pick-venue")
    def pick_venue(
        title: str = Form(""),
        abstract: str = Form(""),
        keywords: str = Form(""),
        venue_type: str = Form("all"),
        archival: str = Form("archival"),
        ai: bool = Form(False),
        limit: int = Form(15),
        ws_id: str = Form(""),
        manuscript_name: str = Form(""),
        manuscript: Optional[UploadFile] = File(None),
        x_llm_provider: Optional[str] = Header(None),
        x_llm_key: Optional[str] = Header(None),
        x_paperpush_token: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        """Recommend venues for a manuscript, like ``paperpush pick-venue``.

        The manuscript is a file sent with the request, or one already uploaded
        to workspace ``ws_id`` (``manuscript_name``); the title, abstract, and
        keywords typed on the page replace (or, for keywords, add to) what is
        read from it. Venues come from OpenAlex (journals and conferences that
        published similar work) plus conferences matched on subject; with
        ``ai`` an LLM also ranks the venues paperpush supports, with reasons.
        """
        from ..openalex import OpenAlexError
        from ..venue_select import VENUE_TYPE_CHOICES, archival_venue_types, find_journals, pick_venues, rank_via_api, rows_from_hits, select, shortlist

        if venue_type not in VENUE_TYPE_CHOICES:
            raise HTTPException(400, f"venue_type must be one of {', '.join(VENUE_TYPE_CHOICES)}")
        if archival not in ("archival", "non-archival", "all"):
            raise HTTPException(400, "archival must be archival, non-archival, or all")
        try:
            types = archival_venue_types(archival, VENUE_TYPE_CHOICES[venue_type])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        provider = api_key = None
        files, local = workspace(ws_id, x_paperpush_token) if ws_id else (None, None)
        if ai:
            provider, api_key = visitor_llm(settings, x_llm_provider, x_llm_key, "the AI recommendation", local=local is not None)
        words = [k.strip() for k in re.split(r"[,;\n]", keywords) if k.strip()]
        limit = max(1, min(limit, 50))
        with tempfile.TemporaryDirectory(prefix="paperpush-pick-") as tmp:
            path: Optional[Path] = None
            if manuscript is not None and manuscript.filename:
                path = Path(tmp) / safe_name(manuscript.filename)
                written = 0
                with path.open("wb") as out:
                    while chunk := manuscript.file.read(_CHUNK):
                        written += len(chunk)
                        if written > settings.max_workspace_mb * _CHUNK:
                            raise HTTPException(413, f"the manuscript is larger than {settings.max_workspace_mb:g} MB")
                        out.write(chunk)
            elif files is not None and manuscript_name:
                path = local.resolve(manuscript_name) if local is not None else files / safe_name(Path(manuscript_name).name)
                if not Path(path).is_file():
                    path = None
            if not (words or title.strip() or abstract.strip() or path):
                raise HTTPException(400, "give a title, abstract, keywords, or a manuscript")
            try:
                selection = select(keywords=words, title=title, abstract=abstract, manuscript=str(path) if path else None, venue_types=types)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            mq = selection.manuscript
            if mq is not None and mq.is_empty():
                raise HTTPException(400, "found no title, abstract, or text to go on in the manuscript")
            read = {"title": mq.title, "abstract": mq.abstract, "keywords": list(mq.keywords)} if mq is not None else None

            notes: list[str] = []
            journals = None
            beyond_supported = mq is not None and settings.network_checks
            if beyond_supported:
                try:
                    journals = find_journals(mq, venue_types=types)
                except (OpenAlexError, ValueError) as exc:
                    notes.append(f"OpenAlex could not be searched ({exc}); only the venues paperpush supports were ranked.")
                if journals is not None and not journals.hits:
                    notes.append("OpenAlex found no venues publishing similar work; only the venues paperpush supports were ranked.")
            catalog = None
            if (not types or "conference" in types) and (beyond_supported or any(h.venue.venue_type == "conference" for h in selection.hits)):
                from ..conferences import load_catalog

                catalog = load_catalog()
                for feed, error in catalog.errors.items():
                    notes.append(f"Conference data from {feed} is unavailable ({error}).")
            rows = shortlist(selection, journals, catalog, venue_types=types, archival=archival, subject_conferences=beyond_supported)

            ai_rows = None
            ai_info = None
            if ai and mq is not None:
                from ..autofill import AutofillApiError

                candidates = [h.venue for h in pick_venues("", venue_types=types)]
                try:
                    ranking = rank_via_api(mq, candidates, limit=limit, provider=provider, api_key=api_key)
                except AutofillApiError as exc:
                    raise HTTPException(502, _scrub(str(exc), Path(tmp), api_key)) from None
                by_slug = {h.venue.slug: h for h in pick_venues("", venues=candidates)}
                ai_rows = rows_from_hits([by_slug[p.slug] for p in ranking.picks if p.slug in by_slug], catalog, reasons={p.slug: f"{p.fit}: {p.reason}" for p in ranking.picks}, basis="ai")
                ai_info = {"model": ranking.model, "subjects": ranking.subjects}

        def scrubbed(row) -> dict[str, Any]:
            data = row.to_dict()
            data.pop("conference", None)
            return data

        return {
            "read": read,
            "subjects": list(selection.subjects),
            "venues": [scrubbed(r) for r in rows[:limit]],
            "total_venues": len(rows),
            "openalex": bool(journals is not None and journals.hits),
            "ai": ai_info,
            "ai_venues": [scrubbed(r) for r in ai_rows] if ai_rows is not None else None,
            "notes": notes,
        }

    @app.post("/api/workspaces")
    def create_workspace() -> dict[str, Any]:
        return {"id": workspaces.create()}

    @app.get("/api/workspaces/{ws_id}")
    def get_workspace(ws_id: str, x_paperpush_token: Optional[str] = Header(None)) -> dict[str, Any]:
        files, local = workspace(ws_id, x_paperpush_token)
        return {"id": ws_id, "files": local.list_files() if local else _list_files(files)}

    @app.delete("/api/workspaces/{ws_id}")
    def delete_workspace(ws_id: str, x_paperpush_token: Optional[str] = Header(None)) -> dict[str, Any]:
        _, local = workspace(ws_id, x_paperpush_token)
        if local is not None:
            return {"deleted": False}  # the author's own files stay put
        workspaces.delete(ws_id)
        return {"deleted": True}

    @app.post("/api/workspaces/{ws_id}/files")
    def upload(ws_id: str, files: list[UploadFile] = File(...), x_paperpush_token: Optional[str] = Header(None)) -> dict[str, Any]:
        _, local = workspace(ws_id, x_paperpush_token)
        if local is not None:
            return upload_local(local, files)
        target = workspaces.files_dir(ws_id)
        budget = int(settings.max_workspace_mb * _CHUNK) - _workspace_bytes(target)
        saved: list[str] = []
        for upload_file in files:
            name = safe_name(upload_file.filename or "")
            dest = target / name
            # Replacing a file frees its old size first.
            if dest.is_file():
                budget += dest.stat().st_size
            partial = target.parent / f".{name}.part"
            written = 0
            with partial.open("wb") as out:
                while chunk := upload_file.file.read(_CHUNK):
                    written += len(chunk)
                    if written > budget:
                        out.close()
                        partial.unlink(missing_ok=True)
                        raise HTTPException(
                            413,
                            f"{name} does not fit: each workspace holds at most {settings.max_workspace_mb:g} MB",
                        )
                    out.write(chunk)
            partial.replace(dest)
            budget -= written
            saved.append(name)
        logger.info("workspace %s: stored %d file(s)", ws_id[:6], len(saved))
        return {"saved": saved, "files": _list_files(target)}

    @app.post("/api/workspaces/{ws_id}/import")
    def import_files(ws_id: str, files: list[UploadFile] = File(...)) -> dict[str, Any]:
        """Take ``.sub`` files and ``.zip`` archives (a manuscript folder, or a bundle downloaded here).

        A ``.sub`` comes back as text for the page to open; it is not stored.
        A zip is unpacked flat into the workspace -- see :func:`import_zip`.
        """
        target = workspaces.files_dir(ws_id)
        cap = int(settings.max_workspace_mb * _CHUNK)
        subs: list[dict[str, str]] = []
        saved: list[str] = []
        paths: list[str] = []
        skipped: list[dict[str, str]] = []
        for upload_file in files:
            name = upload_file.filename or ""
            lower = name.lower()
            if is_subfile_name(lower):
                data = upload_file.file.read(_MAX_SUB_BYTES + 1)
                if len(data) > _MAX_SUB_BYTES:
                    raise HTTPException(413, f"{safe_name(name)} is too large for a .sub file")
                subs.append({"name": safe_name(name), "text": data.decode("utf-8", errors="replace")})
            elif lower.endswith(".zip"):
                fd, tmp = tempfile.mkstemp(suffix=".zip", dir=target.parent)
                try:
                    written = 0
                    with os.fdopen(fd, "wb") as out:
                        while chunk := upload_file.file.read(_CHUNK):
                            written += len(chunk)
                            if written > cap:
                                raise HTTPException(
                                    413,
                                    f"{safe_name(name)} does not fit: each workspace holds at most {settings.max_workspace_mb:g} MB",
                                )
                            out.write(chunk)
                    result = import_zip(Path(tmp), target, cap)
                finally:
                    Path(tmp).unlink(missing_ok=True)
                subs += result["subs"]
                saved += result["saved"]
                paths += result["paths"]
                skipped += result["skipped"]
            else:
                raise HTTPException(400, f"{safe_name(name)}: only .sub and .zip files can be opened here")
        logger.info("workspace %s: imported %d file(s), %d .sub", ws_id[:6], len(saved), len(subs))
        return {"subs": subs, "saved": saved, "paths": paths, "skipped": skipped, "files": _list_files(target)}

    @app.delete("/api/workspaces/{ws_id}/files/{name}")
    def delete_file(ws_id: str, name: str, x_paperpush_token: Optional[str] = Header(None)) -> dict[str, Any]:
        if ws_id == LOCAL_WS:
            local_session(x_paperpush_token)
            raise HTTPException(403, "paperpush does not delete files on your computer; clear the field instead")
        target = workspaces.files_dir(ws_id)
        path = target / safe_name(name)
        if not path.is_file():
            raise HTTPException(404, f"no file {name!r}")
        path.unlink()
        return {"files": _list_files(target)}

    @app.post("/api/workspaces/{ws_id}/plan")
    def plan(ws_id: str, req: TextRequest, x_paperpush_token: Optional[str] = Header(None)) -> dict[str, Any]:
        """Every check validate would run on this .sub, grouped, without their results.

        Runs the offline checks (no links, DOIs, OpenReview, or AI) only to
        learn which checks apply; the page shows them grey until validated.
        """
        files, local = workspace(ws_id, x_paperpush_token)
        sub, venue = _localized_sub(req.text, files, local)
        report = validate_report(
            sub,
            venue,
            check_links=False,
            check_references=False,
            check_openreview=False,
        )
        base = get_requirements(venue.slug)
        return {
            "venue": venue.slug,
            "checks": _report_payload(report, files, with_status=False),
            "live_rules": live_rules(venue, sub.values),
            "schema_issues": venue_schema_issues(venue, report),
            "requirements": (None if base is None else resolve_requirements(base, sub.values).to_dict()),
            "initial_submission_rule": report.initial_submission_rule,
        }

    @app.post("/api/workspaces/{ws_id}/validate")
    def validate(
        ws_id: str,
        req: ValidateRequest,
        x_llm_provider: Optional[str] = Header(None),
        x_llm_key: Optional[str] = Header(None),
        x_plagiarism_provider: Optional[str] = Header(None),
        x_plagiarism_key: Optional[str] = Header(None),
        x_ai_detection_key: Optional[str] = Header(None),
        x_paperpush_token: Optional[str] = Header(None),
    ) -> Any:
        files, local = workspace(ws_id, x_paperpush_token)
        sub, venue = _localized_sub(req.text, files, local)
        if not req.network and (req.plagiarism or req.ai_detection):
            raise HTTPException(400, "the plagiarism and AI-text scans send the manuscript's text to an outside service; untick 'disable network checks' to run them")
        plagiarism = visitor_plagiarism(settings, req, x_plagiarism_provider, x_plagiarism_key, local=local is not None)
        ai_detection = visitor_ai_detection(settings, req, x_ai_detection_key, local=local is not None)
        ai = None
        if req.ai:
            provider, api_key = visitor_llm(settings, x_llm_provider, x_llm_key, "the AI checks", local=local is not None)
            from ..ai_validate import AiOptions

            ai = AiOptions.api(provider=provider, api_key=api_key)
        network = settings.network_checks and req.network

        def run(progress=None) -> dict[str, Any]:
            report = validate_report(
                sub,
                venue,
                check_links=req.check_links and network,
                check_references=req.check_references and network,
                check_anonymous=req.anonymous,
                # Uses the server's own stored OpenReview login: the author's only
                # when paperpush runs on their machine.
                check_openreview=local is not None and network,
                # The visitor's box: no request at all. A server with network checks off
                # skips the lookups above but keeps its own wording ("switched off on this server").
                network=req.network,
                initial_submission=req.initial_submission,
                strict_style=req.strict_style,
                ai=ai,
                plagiarism=plagiarism,
                ai_detection=ai_detection,
                progress=progress,
            )
            return _validate_payload(report, venue, files, ai, network, plagiarism, ai_detection)

        if not req.stream:
            return run()
        return StreamingResponse(_stream_progress(run, files, _secrets(ai, plagiarism, ai_detection)), media_type="application/x-ndjson")

    def _secrets(ai, plagiarism, ai_detection=None) -> list[str]:
        return [k for k in (ai.api_key if ai else None, plagiarism.api_key if plagiarism else None, ai_detection.api_key if ai_detection else None) if k]

    def _validate_payload(report, venue: Venue, files: Path, ai, network: bool, plagiarism=None, ai_detection=None) -> dict[str, Any]:
        secrets = _secrets(ai, plagiarism, ai_detection)
        return {
            "venue": venue.slug,
            "ok": report.ok,
            "ai": report.ai,
            "plagiarism": plagiarism.name if plagiarism else None,
            "ai_detection": ai_detection.name if ai_detection else None,
            "ai_error": _scrub(report.ai_error, files, secrets),
            "counts": report.counts(),
            "network_checks": network,
            "initial_submission_rule": report.initial_submission_rule,
            "initial_submission": report.initial_submission,
            "downgraded": report.downgraded,
            "strict_style": report.strict_style,
            "checks": _scrub_checks(_report_payload(report, files), *([plagiarism.api_key] if plagiarism else []), *([ai_detection.api_key] if ai_detection else [])),
            # The same report `paperpush validate -o report.txt` writes, for the Download button.
            "report_text": _scrub(report_mod.to_text(report), files, secrets),
        }

    @app.post("/api/workspaces/{ws_id}/autofill")
    def autofill(
        ws_id: str,
        req: TextRequest,
        x_llm_provider: Optional[str] = Header(None),
        x_llm_key: Optional[str] = Header(None),
        x_paperpush_token: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        """Fill the form from the uploaded manuscript through the LLM API, like ``paperpush autofill --engine api``."""
        from ..autofill import AutofillApiError, DocumentInput
        from ..autofill import autofill as apply_autofill
        from ..autofill import extract_via_api

        files, local = workspace(ws_id, x_paperpush_token)
        sub, venue = _localized_sub(req.text, files, local)
        provider, api_key = visitor_llm(settings, x_llm_provider, x_llm_key, "autofill", local=local is not None)
        ms = manuscript_field(venue)
        manuscript = Path(sub.values.get(ms.id, "")) if ms is not None else None
        if manuscript is None or not sub.values.get(ms.id, "").strip() or not manuscript.is_file():
            raise HTTPException(400, "upload the manuscript first")
        documents = [DocumentInput("manuscript", manuscript)]
        for label, ids in (
            ("title_page", _TITLE_PAGE_IDS),
            ("supplement", _SUPPLEMENT_IDS),
        ):
            field = _first_field(venue, ids)
            raw = sub.values.get(field.id, "") if field is not None else ""
            first = raw.strip().splitlines()[0].split("|", 1)[0].strip() if raw.strip() else ""
            if first and Path(first).is_file():
                documents.append(DocumentInput(label, Path(first)))
        if local is not None:
            listing = [f["name"] for f in local.list_files()]
        else:
            listing = [p.name for p in sorted(files.iterdir()) if p.is_file()]
        try:
            extraction = extract_via_api(venue, documents, listing, provider=provider, api_key=api_key)
        except AutofillApiError as exc:
            raise HTTPException(502, _scrub(str(exc), files, api_key)) from None
        result = apply_autofill(req.text, venue, extraction, manuscript_dir=files)

        def outcomes(items) -> list[dict[str, str]]:
            return [
                {
                    "id": o.id,
                    "label": o.label,
                    "value": _scrub(o.value, files),
                    "confidence": o.confidence,
                    "note": o.note,
                }
                for o in items
            ]

        return {
            "text": _scrub(result.text, files),
            "filled": outcomes(result.filled),
            "needs_review": outcomes(result.review),
            "skipped": outcomes(result.skipped),
            "left_for_the_author": [{"id": fid, "reason": reason} for fid, reason in extraction.unfilled],
        }

    @app.post("/api/workspaces/{ws_id}/bundle")
    def bundle(ws_id: str, req: TextRequest, x_paperpush_token: Optional[str] = Header(None)) -> FileResponse:
        """The ``.sub`` and its files in one zipped folder (:func:`write_bundle`).

        On a local session the files come from this computer, where the
        ``.sub``'s paths point; on the website from the workspace, by bare name,
        along with any upload the ``.sub`` does not name.
        """
        files, local = workspace(ws_id, x_paperpush_token)
        venue = _venue_or_404(parse_subfile(req.text).venue or GENERAL_SLUG)
        sub_name = default_filename(venue)
        stem = subfile_stem(sub_name)
        if local is not None:
            resolve, extra, tmp_dir = local.resolve, (), None
        else:
            resolve = lambda p: files / _bare(p)  # noqa: E731
            extra = tuple(sorted(p for p in files.iterdir() if p.is_file()))
            tmp_dir = files.parent
        fd, tmp = tempfile.mkstemp(suffix=".zip", dir=tmp_dir)
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                write_bundle(zf, f"{stem}-submission", sub_name, req.text, venue, resolve, extra)
        except MissingFiles as exc:
            os.unlink(tmp)
            where = "on this computer" if local is not None else "among the uploaded files"
            names = [local.scrub(m) for m in exc.missing] if local is not None else exc.missing
            raise HTTPException(400, f"Not found {where}: {', '.join(names)}. Fix the path{'s' if len(names) > 1 else ''} in the form, or upload the file{'s' if len(names) > 1 else ''}, then download again.") from None
        return FileResponse(
            tmp,
            media_type="application/zip",
            filename=f"{stem}-submission.zip",
            background=BackgroundTask(os.unlink, tmp),
        )

    # ---- the "Add venue" page: builds a venue file for `paperpush add-venue`.
    # It never writes the venue databases; the author downloads the file.

    @app.get("/api/venue-file/schema")
    def venue_file_schema() -> dict[str, Any]:
        return venuefile.schema()

    @app.get("/api/venue-file/start")
    def venue_file_start(source: Optional[str] = Query(None, alias="from")) -> dict[str, Any]:
        """A new venue's starting document, or an existing venue's entries."""
        if not source:
            return venuefile.doc_to_json(venuefile.starter_doc())
        try:
            return venuefile.doc_to_json(venuefile.doc_for(source))
        except KeyError:
            raise HTTPException(404, f"unknown venue {source!r}") from None

    @app.post("/api/venue-file/parse")
    def venue_file_parse(req: TextRequest) -> dict[str, Any]:
        """Read an uploaded venue file into the editor's document."""
        if len(req.text) > _MAX_SUB_BYTES:
            raise HTTPException(413, "venue file too large")
        try:
            doc = venuefile.parse(req.text)
        except venuefile.VenueFileError as exc:
            return {"doc": None, "problems": exc.problems}
        return {"doc": venuefile.doc_to_json(doc), "problems": []}

    @app.post("/api/venue-file/render")
    def venue_file_render(req: VenueDocRequest) -> dict[str, Any]:
        """The venue file for the editor's document, and what `add-venue` would say about it."""
        try:
            doc = venuefile.doc_from_json(req.doc)
            result = venuefile.check(doc, force=req.replacing)
            text = venuefile.render(doc, safe_name(req.filename) if req.filename.strip() else "VENUE.txt")
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(400, f"cannot build the venue file: {exc}") from None
        return {"text": text, "errors": result.errors, "warnings": result.warnings, "slug": doc.slug}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- shared forms (see shared.py) ----------------------------------------
    # The bodies are ciphertext the server cannot read; the write token comes
    # in the X-Paperpush-Share-Token header.

    def shared_store() -> SharedForms:
        if shared_forms is None:
            raise HTTPException(404, "sharing forms for editing is switched off on this server")
        return shared_forms

    def shared_call(fn, *args):
        try:
            return fn(*args)
        except SharedFormError as exc:
            if exc.current is not None:
                return JSONResponse({"detail": str(exc), "current": exc.current}, status_code=exc.status)
            raise HTTPException(exc.status, str(exc)) from None

    @app.post("/api/shared")
    def shared_create(req: SharedCreateRequest) -> Any:
        return shared_call(shared_store().create, req.ciphertext, req.token)

    @app.get("/api/shared/{form_id}")
    def shared_get(form_id: str, known: Optional[int] = None) -> Any:
        return shared_call(shared_store().get, form_id, known)

    @app.put("/api/shared/{form_id}")
    def shared_update(form_id: str, req: SharedUpdateRequest, x_paperpush_share_token: Optional[str] = Header(None)) -> Any:
        return shared_call(shared_store().update, form_id, req.base_version, req.ciphertext, x_paperpush_share_token)

    @app.delete("/api/shared/{form_id}")
    def shared_delete(form_id: str, x_paperpush_share_token: Optional[str] = Header(None)) -> Any:
        shared_call(shared_store().delete, form_id, x_paperpush_share_token)
        return {"deleted": True}

    @app.get("/add-venue", include_in_schema=False)
    def add_venue_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "add-venue.html")

    # Browsers (Safari especially) ask for these at the site root regardless
    # of the <link> tags, e.g. for bookmarks, Favorites, and the home screen.
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.ico")

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    @app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
    def apple_touch_icon() -> FileResponse:
        return FileResponse(STATIC_DIR / "apple-touch-icon.png")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, Any]:
        """For the host's health check and the uptime monitor: cheap, no files read."""
        return {"ok": True, "version": __version__}

    @app.get("/privacy", include_in_schema=False)
    def privacy_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "privacy.html")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def serve_local(
    sub_path: Path,
    *,
    manuscript_dirs: tuple[Path, ...] = (),
    action: Optional[str] = None,
    port: int = 0,
    open_browser: bool = True,
    token: Optional[str] = None,
    idle_timeout: Optional[float] = None,
    colorblind: bool = False,
) -> int:
    """Open ``sub_path`` in the page on this machine and serve it until Ctrl-C (``--web``).

    Listens on 127.0.0.1 only. Edits in the page are written back to
    ``sub_path``; relative paths in it resolve against the current directory,
    as they do for the CLI. ``token`` fixes the session token (so a parent
    process can print the link before this one is up); with ``idle_timeout``
    the server stops by itself once that many seconds pass without a request
    (an open page pings it every minute, so it stays up while one is open).
    ``colorblind`` opens the page with its colour-blind-friendly colours
    switched on (``paperpush validate --web --colorblind``).
    """
    import uvicorn

    host = "127.0.0.1"
    port = port or _free_port(host)
    sub_path = Path(sub_path).resolve()
    venue = parse_subfile(sub_path.read_text(encoding="utf-8")).venue or GENERAL_SLUG
    local = LocalSession(
        sub_path=sub_path,
        root=sub_path.parent,
        manuscript_dirs=tuple(Path(d).resolve() for d in manuscript_dirs),
        action=action,
        **({"token": token} if token else {}),
    )
    settings = dataclasses.replace(Settings.from_env(), local=local)
    url = local_url(host, port, local.token, venue, colorblind=colorblind)
    print(f"{'Opening' if open_browser else 'Open'} {local.display(sub_path)} in your browser: {url}")
    print("Edits in the page are saved to the file as you make them. Press Ctrl-C to stop.")
    if open_browser:
        webbrowser.open(url)
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    if idle_timeout:
        _stop_when_idle(app, server, idle_timeout)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


def local_url(host: str, port: int, token: str, venue: str, *, colorblind: bool = False) -> str:
    """The address of the page with a local ``.sub`` open in it (``colorblind`` adds ``palette=alt``)."""
    palette = "&palette=alt" if colorblind else ""
    return f"http://{host}:{port}/?local={token}{palette}#/v/{venue}"


def _stop_when_idle(app: FastAPI, server, idle_timeout: float) -> None:
    """Ask ``server`` to exit once ``idle_timeout`` seconds pass with no request."""
    import threading
    import time

    last = [time.monotonic()]

    @app.middleware("http")
    async def touch(request, call_next):
        last[0] = time.monotonic()
        return await call_next(request)

    def watch() -> None:
        while not server.should_exit:
            time.sleep(min(30.0, idle_timeout / 4))
            if time.monotonic() - last[0] > idle_timeout:
                logger.info("local: no requests for %.0fs; stopping", idle_timeout)
                server.should_exit = True

    threading.Thread(target=watch, name="paperpush-idle", daemon=True).start()


def serve_app(*, port: int = 0, open_browser: bool = True) -> int:
    """Serve the page on this machine, with no .sub open yet, until Ctrl-C (``paperpush web``).

    The same page as the website, but local: no cold start, uploads stay on
    this computer (in the usual temporary workspaces), and link checks and an
    API key from the environment work as they do for the CLI. Listens on
    127.0.0.1 only.
    """
    import uvicorn

    host = "127.0.0.1"
    port = port or _free_port(host)
    url = f"http://{host}:{port}/"
    print(f"paperpush {__version__} web page: {url}")
    print("Press Ctrl-C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        uvicorn.run(create_app(Settings.from_env()), host=host, port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Serve the website (``paperpush-web``)."""
    parser = argparse.ArgumentParser(prog="paperpush-web", description="Serve the paperpush website.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to listen on (0.0.0.0 in a container)",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--open", action="store_true", help="open the page in a browser once it is up")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args(argv)

    import uvicorn

    configure_logging(verbosity=args.verbose)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"  # nosec B104
    logger.warning("paperpush %s website at %s", __version__, url)
    if args.open:
        webbrowser.open(url)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
