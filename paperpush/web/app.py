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

The downloadable bundle (``.sub`` plus its files in one zip) keeps the bare
names, so ``paperpush validate`` and ``paperpush submit`` work on it unchanged
from inside the unzipped folder.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
import webbrowser
import zipfile
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .. import __version__
from .. import report as report_mod
from .. import venues as venues_pkg
from .._logging import configure_logging
from ..autofill import effective_role
from ..database import Venue, get_venue
from ..mcp_server import field_options, list_supported_venues
from ..requirements import get_requirements
from ..requirements import resolve as resolve_requirements
from ..subfile import default_filename
from ..subfile import parse as parse_subfile
from ..subfile import render_template
from ..validate import validate_report

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

DEFAULT_TTL_HOURS = 24.0
DEFAULT_MAX_WORKSPACE_MB = 500.0
_CHUNK = 1024 * 1024
_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
# Characters that would break a .sub line: the filelist column separator, line
# breaks, and the path separators that would turn a name back into a path.
_UNSAFE_NAME_CHARS = re.compile(r"[|/\\\x00-\x1f\x7f]")

# Checks the page re-runs in the browser as the author types (static/live.js
# ports them); everything they look at is in the form itself.
LIVE_CHECKS = frozenset(
    {
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


@dataclasses.dataclass(frozen=True)
class Settings:
    """Deployment knobs, read from ``PAPERPUSH_WEB_*`` environment variables."""

    data_dir: Path
    ttl_hours: float = DEFAULT_TTL_HOURS
    max_workspace_mb: float = DEFAULT_MAX_WORKSPACE_MB
    # Link and DOI checks make the server fetch URLs taken from uploaded
    # files. A public deployment may want them off.
    network_checks: bool = True
    # Allow visitors to run autofill and AI checks with their own API key.
    ai: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = os.environ.get("PAPERPUSH_WEB_DATA") or str(Path(tempfile.gettempdir()) / "paperpush-web")
        return cls(
            data_dir=Path(data_dir),
            ttl_hours=float(os.environ.get("PAPERPUSH_WEB_TTL_HOURS", DEFAULT_TTL_HOURS)),
            max_workspace_mb=float(os.environ.get("PAPERPUSH_WEB_MAX_WORKSPACE_MB", DEFAULT_MAX_WORKSPACE_MB)),
            network_checks=_env_flag("PAPERPUSH_WEB_NETWORK_CHECKS", True),
            ai=_env_flag("PAPERPUSH_WEB_AI", True),
        )


def ai_provider(settings: Settings) -> Optional[str]:
    """The provider configured in the environment, if any (informational only)."""
    if not settings.ai:
        return None
    from ..autofill import detect_provider

    return detect_provider()


def visitor_llm(settings: Settings, provider: Optional[str], api_key: Optional[str], what: str) -> tuple[Optional[str], Optional[str]]:
    """The provider and key to call for ``what`` (autofill or the AI checks).

    The visitor's key is sent with the request in the ``X-LLM-Key`` header,
    used for this request, and never written anywhere. A key is required.
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
    raise HTTPException(400, f"{what} needs an LLM API key: add yours on the page")


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


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
        for ws in self.root.iterdir():
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


# ---------------------------------------------------------------------------
# .sub <-> workspace paths
# ---------------------------------------------------------------------------


def _bare(path_text: str) -> str:
    return path_text.strip().replace("\\", "/").rsplit("/", 1)[-1]


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


def _scrub(message: str, files: Path, api_key: Optional[str] = None) -> str:
    """``message`` with workspace paths made bare and the visitor's API key, should a provider echo it, masked."""
    if api_key:
        message = message.replace(api_key, "[your API key]")
    return message.replace(f"{files}{os.sep}", "").replace(str(files), "")


def _localized_sub(text: str, files: Path):
    sub = parse_subfile(text)
    if not sub.venue:
        raise HTTPException(400, "the .sub has no '@venue:' line")
    venue = _venue_or_404(sub.venue)
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
        structured_headings=list(a.structured_headings or []) if a.structured else None,
        no_references=a.no_references,
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


def venue_detail(slug: str) -> dict[str, Any]:
    venue = _venue_or_404(slug)
    reqs = get_requirements(venue.slug)
    ms = manuscript_field(venue)
    return {
        "slug": venue.slug,
        "name": venue.name,
        "full_name": venue.full_name,
        "venue_type": venue.venue_type,
        "description": venue.description,
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
_SWITCHED_OFF = re.compile(r"switched off \(--[a-z-]+\)")


def _web_detail(detail: str, files: Path) -> str:
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


class ValidateRequest(BaseModel):
    text: str
    ai: bool = False
    check_links: bool = True
    check_references: bool = True
    anonymous: Optional[bool] = None


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    workspaces = Workspaces(settings)
    app = FastAPI(title="paperpush", version=__version__, docs_url=None, redoc_url=None)

    @app.get("/api/info")
    def info() -> dict[str, Any]:
        return {
            "version": __version__,
            "network_checks": settings.network_checks,
            "ai": settings.ai,
            "ai_provider": ai_provider(settings),
            "max_workspace_mb": settings.max_workspace_mb,
            "ttl_hours": settings.ttl_hours,
        }

    @app.get("/api/venues")
    def venues() -> list[dict[str, Any]]:
        return list_supported_venues()

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

    @app.post("/api/workspaces")
    def create_workspace() -> dict[str, Any]:
        return {"id": workspaces.create()}

    @app.get("/api/workspaces/{ws_id}")
    def get_workspace(ws_id: str) -> dict[str, Any]:
        return {"id": ws_id, "files": _list_files(workspaces.files_dir(ws_id))}

    @app.delete("/api/workspaces/{ws_id}")
    def delete_workspace(ws_id: str) -> dict[str, Any]:
        workspaces.delete(ws_id)
        return {"deleted": True}

    @app.post("/api/workspaces/{ws_id}/files")
    def upload(ws_id: str, files: list[UploadFile] = File(...)) -> dict[str, Any]:
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

    @app.delete("/api/workspaces/{ws_id}/files/{name}")
    def delete_file(ws_id: str, name: str) -> dict[str, Any]:
        target = workspaces.files_dir(ws_id)
        path = target / safe_name(name)
        if not path.is_file():
            raise HTTPException(404, f"no file {name!r}")
        path.unlink()
        return {"files": _list_files(target)}

    @app.post("/api/workspaces/{ws_id}/plan")
    def plan(ws_id: str, req: TextRequest) -> dict[str, Any]:
        """Every check validate would run on this .sub, grouped, without their results.

        Runs the offline checks (no links, DOIs, OpenReview, or AI) only to
        learn which checks apply; the page shows them grey until validated.
        """
        files = workspaces.files_dir(ws_id)
        sub, venue = _localized_sub(req.text, files)
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
            "requirements": (None if base is None else resolve_requirements(base, sub.values).to_dict()),
        }

    @app.post("/api/workspaces/{ws_id}/validate")
    def validate(
        ws_id: str,
        req: ValidateRequest,
        x_llm_provider: Optional[str] = Header(None),
        x_llm_key: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        files = workspaces.files_dir(ws_id)
        sub, venue = _localized_sub(req.text, files)
        ai = None
        if req.ai:
            provider, api_key = visitor_llm(settings, x_llm_provider, x_llm_key, "the AI checks")
            from ..ai_validate import AiOptions

            ai = AiOptions.api(provider=provider, api_key=api_key)
        network = settings.network_checks
        report = validate_report(
            sub,
            venue,
            check_links=req.check_links and network,
            check_references=req.check_references and network,
            check_anonymous=req.anonymous,
            # Uses the server's own stored OpenReview login, which is not the author's.
            check_openreview=False,
            ai=ai,
        )
        return {
            "venue": venue.slug,
            "ok": report.ok,
            "ai": report.ai,
            "ai_error": _scrub(report.ai_error, files, ai.api_key if ai else None),
            "counts": report.counts(),
            "network_checks": network,
            "checks": _report_payload(report, files),
        }

    @app.post("/api/workspaces/{ws_id}/autofill")
    def autofill(
        ws_id: str,
        req: TextRequest,
        x_llm_provider: Optional[str] = Header(None),
        x_llm_key: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        """Fill the form from the uploaded manuscript through the LLM API, like ``paperpush autofill --engine api``."""
        from ..autofill import AutofillApiError, DocumentInput
        from ..autofill import autofill as apply_autofill
        from ..autofill import extract_via_api

        files = workspaces.files_dir(ws_id)
        sub, venue = _localized_sub(req.text, files)
        provider, api_key = visitor_llm(settings, x_llm_provider, x_llm_key, "autofill")
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
    def bundle(ws_id: str, req: TextRequest) -> FileResponse:
        files = workspaces.files_dir(ws_id)
        slug = parse_subfile(req.text).venue
        venue = _venue_or_404(slug) if slug else None
        sub_name = default_filename(venue) if venue else "submission.sub"
        fd, tmp = tempfile.mkstemp(suffix=".zip", dir=files.parent)
        os.close(fd)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(sub_name, req.text)
            for path in sorted(files.iterdir()):
                if path.is_file():
                    zf.write(path, path.name)
        stem = Path(sub_name).stem
        return FileResponse(
            tmp,
            media_type="application/zip",
            filename=f"{stem}-submission.zip",
            background=BackgroundTask(os.unlink, tmp),
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


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
