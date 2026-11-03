"""Shared forms: one ``.sub`` that several people edit on the website.

"Share for editing" stores the form here and hands back a link
``/#/f/<id>/<key>``. Everything after ``#`` stays in the browser, so the server
never sees the key: the page encrypts the ``.sub`` with it (AES-GCM, see
``static/shared.js``) and this module stores only that ciphertext. Nobody who
reads the database -- the site's host included -- can read a shared form.

From the same key the page also derives a write token, sent with every change
or deletion. The server keeps only its SHA-256, so the form id alone (which
can turn up in access logs) is not enough to overwrite or delete a form.

Edits are compare-and-swap on ``version``: a change names the version it was
made on, and if someone else saved first the server answers 409 with the
current form, which the page merges field by field before trying again.

Storage is Postgres when ``PAPERPUSH_WEB_DATABASE_URL`` (or ``DATABASE_URL``)
is set -- e.g. a free Neon database, since the free Render plan's disk is
wiped on every deploy -- and otherwise an SQLite file in the data directory,
which is fine on one machine but does not survive a redeploy. A form nobody
has saved for ``ttl_days`` is deleted.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 90.0
# The page compresses before it encrypts, so even a 1 MB .sub is far below this.
MAX_CIPHERTEXT_CHARS = 2 * 1024 * 1024
FORM_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_CIPHERTEXT = re.compile(r"^[A-Za-z0-9_-]+$")
_SWEEP_EVERY_S = 3600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_forms (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    ciphertext TEXT NOT NULL,
    auth_hash TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
)
"""


class SharedFormError(Exception):
    """A request the store refuses; ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str, current: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.status = status
        self.current = current


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def check_token(token: Optional[str]) -> str:
    if not token or not _TOKEN.match(token):
        raise SharedFormError(403, "missing or malformed write token")
    return token


def check_ciphertext(ciphertext: str) -> str:
    if not ciphertext or len(ciphertext) > MAX_CIPHERTEXT_CHARS or not _CIPHERTEXT.match(ciphertext):
        raise SharedFormError(413 if len(ciphertext or "") > MAX_CIPHERTEXT_CHARS else 400, "the form is empty, malformed, or too large")
    return ciphertext


@dataclasses.dataclass
class SharedForms:
    """The shared-form table, in Postgres (``database_url``) or SQLite (``sqlite_path``)."""

    database_url: Optional[str] = None
    sqlite_path: Optional[Path] = None
    ttl_days: float = DEFAULT_TTL_DAYS
    _last_sweep: float = 0.0
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.database_url and self.sqlite_path is None:
            raise ValueError("SharedForms needs a database_url or a sqlite_path")
        if self.sqlite_path is not None and not self.database_url:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)
        logger.info("shared forms in %s", "Postgres" if self.durable else self.sqlite_path)

    @property
    def durable(self) -> bool:
        """Whether forms survive a redeploy (an external database, not a local file)."""
        return bool(self.database_url)

    # -- connections -------------------------------------------------------

    def _connect(self):
        if self.database_url:
            try:
                import psycopg
            except ImportError as exc:  # pragma: no cover - depends on the install
                raise RuntimeError("a database URL is set but psycopg is not installed: pip install 'paperpush[web]'") from exc
            return _PgConn(psycopg.connect(self.database_url, autocommit=False))
        return _SqliteConn(sqlite3.connect(self.sqlite_path, timeout=10))

    # -- operations --------------------------------------------------------

    def create(self, ciphertext: str, token: str) -> dict[str, Any]:
        check_ciphertext(ciphertext)
        check_token(token)
        self.maybe_sweep()
        form_id = secrets.token_urlsafe(18)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO shared_forms (id, version, ciphertext, auth_hash, created_at, updated_at) VALUES (?, 1, ?, ?, ?, ?)",
                (form_id, ciphertext, _hash(token), now, now),
            )
        logger.info("created shared form %s", form_id[:6])
        return {"id": form_id, "version": 1, "updated_at": now}

    def _row(self, conn, form_id: str) -> Optional[tuple]:
        return conn.fetchone("SELECT version, ciphertext, auth_hash, updated_at FROM shared_forms WHERE id = ?", (form_id,))

    def _live_row(self, conn, form_id: str) -> tuple:
        if not FORM_ID.match(form_id):
            raise SharedFormError(404, "no such shared form")
        row = self._row(conn, form_id)
        if row is None or row[3] < time.time() - self.ttl_days * 86400:
            raise SharedFormError(404, "no such shared form: it was deleted, or expired after going unedited")
        return row

    def get(self, form_id: str, known: Optional[int] = None) -> dict[str, Any]:
        """The form; without its ciphertext when the caller already has version ``known``."""
        with self._connect() as conn:
            version, ciphertext, _, updated_at = self._live_row(conn, form_id)
        out: dict[str, Any] = {"id": form_id, "version": version, "updated_at": updated_at}
        if known != version:
            out["ciphertext"] = ciphertext
        return out

    def update(self, form_id: str, base_version: int, ciphertext: str, token: Optional[str]) -> dict[str, Any]:
        check_ciphertext(ciphertext)
        token = check_token(token)
        now = time.time()
        with self._connect() as conn:
            version, current, auth_hash, updated_at = self._live_row(conn, form_id)
            if not secrets.compare_digest(auth_hash, _hash(token)):
                raise SharedFormError(403, "this link cannot change the form")
            changed = conn.execute(
                "UPDATE shared_forms SET version = version + 1, ciphertext = ?, updated_at = ? WHERE id = ? AND version = ?",
                (ciphertext, now, form_id, base_version),
            )
            if changed != 1:
                row = self._row(conn, form_id)
                if row is None:
                    raise SharedFormError(404, "no such shared form")
                raise SharedFormError(
                    409,
                    "someone else saved first",
                    {"id": form_id, "version": row[0], "ciphertext": row[1], "updated_at": row[3]},
                )
        return {"id": form_id, "version": base_version + 1, "updated_at": now}

    def delete(self, form_id: str, token: Optional[str]) -> None:
        token = check_token(token)
        with self._connect() as conn:
            _, _, auth_hash, _ = self._live_row(conn, form_id)
            if not secrets.compare_digest(auth_hash, _hash(token)):
                raise SharedFormError(403, "this link cannot delete the form")
            conn.execute("DELETE FROM shared_forms WHERE id = ?", (form_id,))
        logger.info("deleted shared form %s", form_id[:6])

    def sweep(self) -> int:
        """Delete forms unsaved for longer than ``ttl_days``; returns how many."""
        cutoff = time.time() - self.ttl_days * 86400
        with self._connect() as conn:
            gone = conn.execute("DELETE FROM shared_forms WHERE updated_at < ?", (cutoff,))
        if gone:
            logger.info("expired %d shared form(s)", gone)
        return gone

    def maybe_sweep(self) -> None:
        with self._lock:
            if time.time() - self._last_sweep < _SWEEP_EVERY_S:
                return
            self._last_sweep = time.time()
        try:
            self.sweep()
        except Exception:  # a failed cleanup must not fail the request
            logger.exception("could not expire shared forms")


class _SqliteConn:
    """A connection with ``?`` placeholders; commits on a clean exit."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()

    def execute(self, sql: str, params: tuple = ()) -> int:
        return self.conn.execute(sql, params).rowcount

    def fetchone(self, sql: str, params: tuple = ()):
        return self.conn.execute(sql, params).fetchone()


class _PgConn(_SqliteConn):
    """The same interface over psycopg, whose placeholders are ``%s``."""

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params)
            return cur.rowcount

    def fetchone(self, sql: str, params: tuple = ()):
        with self.conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params)
            return cur.fetchone()
