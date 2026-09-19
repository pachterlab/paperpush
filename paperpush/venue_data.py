"""Where the venue data files are read from, and how they are kept current.

The venue data -- ``venues.json``, ``manuscript_requirements.json``, their
JSON schemas (``venues.schema.json``, ``manuscript_requirements.schema.json``)
and the controlled-vocabulary files under ``venues/_assets/`` -- ships inside
the package, but it changes far more often than the code: a portal renames a
category, a journal raises a word limit. So the same files are also published
on their own (see ``scripts/build_venue_data.py`` and the ``venue-data``
workflow) and fetched at runtime, letting a data fix reach every installed
copy without a PyPI release.

Resolution, controlled by ``PAPERPUSH_VENUE_DATA``:

* ``auto`` (default) -- use the cached remote copy, refreshing it at most once
  per :data:`REFRESH_INTERVAL_SECONDS`; fall back to the bundled copy when there
  is no usable cache. In a source checkout (the package sits next to ``.git``)
  ``auto`` means ``bundled``, so a contributor's edits to ``venues.json`` are
  what they see and what the tests and generators read.
* ``remote`` -- as ``auto``, but also in a source checkout.
* ``bundled`` -- only the files shipped with the installed package.
* a directory path -- read the files from that directory (same layout as the
  published data; a ``manifest.json`` is optional). For testing data changes.

``PAPERPUSH_OFFLINE=1`` never touches the network but still uses a cached copy,
and ``PAPERPUSH_VENUE_DATA_URL`` points at a different published copy.

A remote copy is only as trustworthy as its checks, since it drives a browser
that fills real submission forms. Every file is verified against the sha256 in
the manifest, the manifest's :data:`DATA_FORMAT` must match this version's, and
the whole set must load (:func:`paperpush.database.check_venue_data`) before
it replaces the cache. The code/data coupling is handled per venue in
:mod:`paperpush.database`: a remote venue entry is only used when its fields
match the shape the installed runner was written against.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from . import __version__

logger = logging.getLogger(__name__)

#: Version of the published data's layout and meaning. Bump it when a change to
#: the data needs new code to read it (a new key the loader must understand, a
#: changed merge rule). Clients only accept a manifest with their own format, so
#: installed versions keep their bundled data until they upgrade.
DATA_FORMAT = 1

DEFAULT_URL = "https://raw.githubusercontent.com/pachterlab/paperpush/venue-data/"
REFRESH_INTERVAL_SECONDS = 24 * 60 * 60
# How soon to try again after a failed or incompatible check, or when the cache
# was written by another paperpush version.
RETRY_INTERVAL_SECONDS = 60 * 60
# Short, so a slow or absent network costs a command little; the next command
# after the refresh interval tries again.
TIMEOUT_SECONDS = 3.0

PACKAGE_DIR = Path(__file__).parent
VENUES_FILE = "venues.json"
REQUIREMENTS_FILE = "manuscript_requirements.json"
VENUES_SCHEMA_FILE = "venues.schema.json"
REQUIREMENTS_SCHEMA_FILE = "manuscript_requirements.schema.json"
MANIFEST_FILE = "manifest.json"
ASSETS_SUBDIR = "_assets"

#: Files every published copy carries, besides the ``_assets`` vocabularies. The
#: schemas travel with the data they describe: each is generated from the code
#: that published it, so a copy's schema is the one its data was validated
#: against (and ``manuscript_requirements.json``'s relative ``$schema`` resolves
#: next to it).
DATA_FILES = (VENUES_FILE, REQUIREMENTS_FILE, VENUES_SCHEMA_FILE, REQUIREMENTS_SCHEMA_FILE)

# The only paths a manifest may list. Anything else (``../``, subdirectories,
# unexpected names) is rejected so a manifest can never write outside the cache.
_ALLOWED_PATH = re.compile(r"^(" + "|".join(re.escape(f) for f in DATA_FILES) + r"|_assets/[A-Za-z0-9_.-]+)$")

# Cache bookkeeping, next to the data files.
_STATE_FILE = ".state.json"

SourceKind = Literal["bundled", "remote", "override"]


@dataclass(frozen=True)
class DataSource:
    """The directory venue data is read from, and where it came from."""

    kind: SourceKind
    #: Directory holding ``venues.json`` and ``manuscript_requirements.json``.
    root: Path
    #: Directory holding the ``options_file`` vocabularies.
    assets: Path
    #: The manifest of a published copy (None for the bundled data).
    manifest: Optional[dict] = None

    def path(self, name: str) -> Path:
        """Path of one of :data:`DATA_FILES` in this source.

        Falls back to the bundled file when this source lacks it (an override
        directory holding only ``venues.json``, say).
        """
        candidate = self.root / name
        return candidate if candidate.is_file() else PACKAGE_DIR / name

    def describe(self) -> str:
        """One line naming the data in use, for ``paperpush --venues`` and logs."""
        if self.kind == "bundled":
            return f"bundled with paperpush {__version__}"
        if self.kind == "override":
            return f"local override {self.root}"
        m = self.manifest or {}
        stamp = str(m.get("published_at", "unknown date"))
        commit = str(m.get("commit", ""))[:7]
        return f"published {stamp}" + (f" ({commit})" if commit else "")


def bundled_source() -> DataSource:
    return DataSource("bundled", PACKAGE_DIR, PACKAGE_DIR / "venues" / ASSETS_SUBDIR)


def cache_dir() -> Path:
    """Directory holding the cached remote copy."""
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "paperpush" / "venue-data"


def data_url() -> str:
    url = os.environ.get("PAPERPUSH_VENUE_DATA_URL", "").strip() or DEFAULT_URL
    return url if url.endswith("/") else url + "/"


def _offline() -> bool:
    return os.environ.get("PAPERPUSH_OFFLINE", "").lower() in {"1", "true", "yes"}


def _in_source_checkout() -> bool:
    return (PACKAGE_DIR.parent / ".git").exists()


def _mode() -> str:
    return os.environ.get("PAPERPUSH_VENUE_DATA", "").strip() or "auto"


def uses_published() -> bool:
    """Whether this process reads the published copy (rather than bundled or override data)."""
    mode = _mode()
    return mode == "remote" or (mode == "auto" and not _in_source_checkout())


# ---------------------------------------------------------------------------
# Choosing a source
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def active_source() -> DataSource:
    """The data source for this process, refreshing the cache if it is due.

    Resolved once per process: a long-running MCP server keeps the data it
    started with until :func:`reset` (``paperpush update-venues`` calls it).
    """
    mode = _mode()
    if mode not in {"auto", "remote", "bundled"}:
        root = Path(mode).expanduser()
        if not (root / VENUES_FILE).is_file():
            logger.warning("PAPERPUSH_VENUE_DATA=%s has no %s; using the bundled venue data", root, VENUES_FILE)
            return bundled_source()
        logger.info("Venue data: using the local override %s", root)
        return DataSource("override", root, root / ASSETS_SUBDIR, _read_json(root / MANIFEST_FILE))
    if not uses_published():
        logger.debug("Venue data: using the bundled copy (mode=%s)", mode)
        return bundled_source()

    if not _offline() and _refresh_due():
        try:
            refresh()
        except Exception as exc:  # never let a data refresh break a command
            logger.info("Could not refresh the venue data (%s); using what is on disk", exc)
            _update_state(checked_at=time.time())
    return _cached_source() or bundled_source()


def reset() -> None:
    """Forget the resolved source so the next access re-resolves it."""
    active_source.cache_clear()


def _cached_source() -> Optional[DataSource]:
    """The cached remote copy, or None when there is none this version can use."""
    root = cache_dir()
    manifest = _read_json(root / MANIFEST_FILE)
    if not manifest or not all((root / name).is_file() for name in DATA_FILES):
        return None
    if manifest.get("data_format") != DATA_FORMAT:
        logger.info("Cached venue data has format %r, this paperpush reads %r; using the bundled copy", manifest.get("data_format"), DATA_FORMAT)
        return None
    # A cache written by another paperpush version may predate that version's
    # bundled data; only trust it once this version has refreshed it.
    if _read_json(root / _STATE_FILE).get("fetched_by") != __version__:
        logger.info("Cached venue data was fetched by another paperpush version; using the bundled copy")
        return None
    return DataSource("remote", root, root / ASSETS_SUBDIR, manifest)


def _refresh_due() -> bool:
    state = _read_json(cache_dir() / _STATE_FILE)
    interval = REFRESH_INTERVAL_SECONDS if state.get("fetched_by") == __version__ else RETRY_INTERVAL_SECONDS
    return time.time() - float(state.get("checked_at", 0)) >= interval


# ---------------------------------------------------------------------------
# Refreshing the cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefreshResult:
    """Outcome of :func:`refresh`."""

    #: ``updated`` (new data cached), ``current`` (cache already up to date), or
    #: ``incompatible`` (published data needs a newer paperpush).
    status: Literal["updated", "current", "incompatible"]
    manifest: dict
    message: str


def _http_get(url: str, etag: Optional[str] = None) -> tuple[int, bytes, Optional[str]]:
    """GET ``url``; return (status, body, etag). A 304 comes back with no body."""
    if not url.startswith("https://"):
        raise ValueError(f"refusing to fetch venue data over a non-HTTPS URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": f"paperpush/{__version__}"})
    if etag:
        request.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310 -- scheme checked above
            return response.status, response.read(), response.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, b"", etag
        raise


def refresh(force: bool = False) -> RefreshResult:
    """Fetch the published venue data into the cache.

    Downloads the manifest (conditionally, by ETag, unless ``force``), then only
    the files whose hash differs from the cached copy. The new set is assembled
    and checked in a staging directory and swapped in only once it passes, so a
    failed or partial download leaves the previous cache intact.

    Raises on network errors and on data that fails verification.
    """
    root = cache_dir()
    state = _read_json(root / _STATE_FILE)
    base = data_url()
    # An ETag is only stored alongside a cache this version verified, so a 304
    # means that cache is still the published one.
    etag = None if force or state.get("fetched_by") != __version__ else state.get("etag")

    status, body, new_etag = _http_get(base + MANIFEST_FILE, etag)
    if status == 304:
        _update_state(checked_at=time.time())
        return RefreshResult("current", _read_json(root / MANIFEST_FILE), "venue data is up to date")

    manifest = json.loads(body.decode("utf-8"))
    if manifest.get("data_format") != DATA_FORMAT:
        # Keep whatever cache there is, and fetch the manifest in full next time.
        _update_state(checked_at=time.time(), etag=None)
        return RefreshResult(
            "incompatible",
            manifest,
            f"the published venue data (format {manifest.get('data_format')}) needs a newer paperpush " f"than {__version__} (format {DATA_FORMAT}); run 'pip install -U paperpush' to get it",
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or not all(name in files for name in DATA_FILES):
        raise ValueError(f"venue data manifest must list {', '.join(DATA_FILES)}")
    for rel in files:
        if not _ALLOWED_PATH.match(rel):
            raise ValueError(f"venue data manifest lists an unexpected path: {rel!r}")

    old_manifest = _read_json(root / MANIFEST_FILE)
    if old_manifest.get("files") == files and all((root / rel).is_file() for rel in files):
        _update_state(checked_at=time.time(), etag=new_etag, fetched_by=__version__)
        return RefreshResult("current", manifest, "venue data is up to date")

    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".venue-data-", dir=root.parent))
    try:
        (staging / ASSETS_SUBDIR).mkdir()
        for rel, digest in files.items():
            cached = root / rel
            if cached.is_file() and _sha256(cached.read_bytes()) == digest:
                shutil.copyfile(cached, staging / rel)
                continue
            _, content, _ = _http_get(base + rel)
            if _sha256(content) != digest:
                raise ValueError(f"venue data file {rel} does not match its manifest hash")
            (staging / rel).write_bytes(content)
        (staging / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        # Must load cleanly before it can replace anything. Imported here:
        # database imports this module.
        from .database import check_venue_data

        check_venue_data(staging)

        if root.exists():
            shutil.rmtree(root)
        staging.rename(root)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    _update_state(checked_at=time.time(), etag=new_etag, fetched_by=__version__)
    logger.info("Venue data updated to %s", manifest.get("published_at"))
    return RefreshResult("updated", manifest, f"venue data updated ({DataSource('remote', root, root, manifest).describe()})")


def clear_cache() -> None:
    """Delete the cached remote copy; the bundled data is used until the next refresh."""
    shutil.rmtree(cache_dir(), ignore_errors=True)
    reset()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    """Hex sha256 of a file, as recorded in the manifest."""
    return _sha256(path.read_bytes())


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _update_state(**changes) -> None:
    """Merge ``changes`` into the cache's bookkeeping file.

    Keys: ``checked_at`` (last time the published copy was checked, successfully
    or not), ``etag`` (of the manifest the cache holds), and ``fetched_by`` (the
    paperpush version that last verified the cache against the published copy).
    """
    root = cache_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        state = _read_json(root / _STATE_FILE)
        state.update(changes)
        (root / _STATE_FILE).write_text(json.dumps(state) + "\n", encoding="utf-8")
    except OSError as exc:  # a read-only home should not break a command
        logger.debug("Could not record the venue data check: %s", exc)
