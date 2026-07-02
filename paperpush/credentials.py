"""Secure storage for venue submission credentials.

``paperpush login <venue>`` collects a username and password for a
venue's submission system and stores them here so that ``submit`` can reuse
them without prompting again.

Two backends are supported, tried in order:

1. The operating system secret store, via the optional :mod:`keyring`
   dependency (the system keychain on macOS, Secret Service on Linux, the
   Credential Locker on Windows). This is preferred when a working backend is
   available.
2. A fallback JSON file under the user's config directory, created with
   owner-only permissions (mode ``0600``). This keeps credentials off other
   users' reach but, unlike the OS store, is not encrypted at rest; callers are
   warned when this path is used.

Set ``PAPERPUSH_KEYRING=0`` to force the file backend (useful for testing
or on hosts without a usable secret store).
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE_PREFIX = "paperpush"


@dataclass(frozen=True)
class Credential:
    """A stored credential for one venue.

    For a password login ``username``/``password`` are the submission-system
    login. For an ORCID login (``method == "orcid"``) ``username`` is the ORCID
    iD, ``orcid`` repeats it, ``display_name`` is the author's name when known,
    and ``password`` holds the OAuth access token if one was issued (empty when
    only the public iD is on record).
    """

    venue: str
    username: str
    password: str
    method: str = "password"
    orcid: str = ""
    display_name: str = ""


def _service_name(slug: str) -> str:
    return f"{SERVICE_PREFIX}:{slug.lower()}"


def config_dir() -> Path:
    """Return the directory used for paperpush's config and fallback store."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "paperpush"


def _file_store_path() -> Path:
    return config_dir() / "credentials.json"


def _keyring_enabled() -> bool:
    return os.environ.get("PAPERPUSH_KEYRING", "1").lower() not in {"0", "false", "no"}


def _get_keyring():
    """Return the :mod:`keyring` module if it has a working backend, else None."""
    if not _keyring_enabled():
        logger.debug("Keyring disabled via PAPERPUSH_KEYRING; using file store")
        return None
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
    except Exception as exc:
        logger.debug("keyring module unavailable (%s); using file store", exc)
        return None
    try:
        backend = keyring.get_keyring()
    except Exception as exc:
        logger.debug("Could not query keyring backend (%s); using file store", exc)
        return None
    if isinstance(backend, FailKeyring):
        logger.debug("Keyring resolved to the no-op fail backend; using file store")
        return None
    logger.debug("Using OS keyring backend %s", type(backend).__name__)
    return keyring


# --- file fallback backend ------------------------------------------------


def _read_file_store() -> dict:
    path = _file_store_path()
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read credential file %s (%s); " "treating it as empty", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write_file_store(data: dict) -> None:
    path = _file_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write with owner-only permissions from the start to avoid a window where
    # the file is world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
    finally:
        # Ensure permissions even if the file pre-existed with looser modes.
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


# --- public API -----------------------------------------------------------


def using_keyring() -> bool:
    """True if credentials will be stored in the OS secret store."""
    return _get_keyring() is not None


def save_credential(slug: str, username: str, password: str) -> bool:
    """Store ``username``/``password`` for ``slug``.

    Returns True if the OS keyring was used, False if the file fallback was.
    A keyring backend that is advertised but unusable at runtime (for example a
    locked or absent Secret Service on a headless host) is treated as
    unavailable and the file fallback is used instead.
    """
    slug = slug.lower()
    kr = _get_keyring()
    if kr is not None:
        try:
            kr.set_password(_service_name(slug), username, password)
        except Exception as exc:
            logger.warning("Keyring rejected the password for %s (%s); " "falling back to the file store", slug, exc)
            kr = None  # backend present but unusable; fall through to file
        else:
            # Record the username so we can look the password back up later.
            store = _read_file_store()
            entry = store.get(slug, {})
            entry["username"] = username
            entry.pop("password", None)  # never keep plaintext alongside keyring
            store[slug] = entry
            _write_file_store(store)
            logger.info("Saved password credential for %s to the OS keyring", slug)
            return True

    store = _read_file_store()
    store[slug] = {"username": username, "password": password}
    _write_file_store(store)
    logger.info("Saved password credential for %s to the file store", slug)
    return False


def save_orcid_credential(slug: str, orcid_id: str, name: str = "", token: str | None = None) -> bool:
    """Store an ORCID-based login for ``slug``.

    ``orcid_id`` becomes the credential's username. A non-empty ``token`` (the
    OAuth access token) is treated as a secret and goes to the OS keyring when
    one is available, mirroring :func:`save_credential`. The ORCID iD and name
    are public identifiers, so they are always recorded in the file store.

    Returns True if a token was written to the OS keyring, False otherwise (no
    token, or no usable keyring so the token fell back to the file store).
    """
    slug = slug.lower()
    entry: dict = {"method": "orcid", "username": orcid_id, "orcid": orcid_id}
    if name:
        entry["name"] = name

    stored_in_keyring = False
    if token:
        kr = _get_keyring()
        if kr is not None:
            try:
                kr.set_password(_service_name(slug), orcid_id, token)
                stored_in_keyring = True
            except Exception as exc:
                logger.warning("Keyring rejected the ORCID token for %s (%s); " "falling back to the file store", slug, exc)
                stored_in_keyring = False
        if not stored_in_keyring:
            entry["token"] = token

    store = _read_file_store()
    store[slug] = entry
    _write_file_store(store)
    logger.info("Saved ORCID credential for %s (iD %s, token=%s, keyring=%s)", slug, orcid_id, bool(token), stored_in_keyring)
    return stored_in_keyring


def get_credential(slug: str) -> Credential | None:
    """Return the stored credential for ``slug``, or None if there is none."""
    slug = slug.lower()
    store = _read_file_store()
    entry = store.get(slug)
    if not entry:
        logger.debug("No stored credential for %s", slug)
        return None

    if entry.get("method") == "orcid":
        orcid_id = entry.get("orcid") or entry.get("username", "")
        token = entry.get("token", "")
        if not token:
            kr = _get_keyring()
            if kr is not None and orcid_id:
                try:
                    token = kr.get_password(_service_name(slug), orcid_id) or ""
                except Exception as exc:
                    logger.warning("Could not read the ORCID token for %s from " "the keyring (%s)", slug, exc)
                    # clearing the token after a read failure, not a hardcoded secret
                    token = ""  # nosec B105
        logger.debug("Loaded ORCID credential for %s (iD %s, token=%s)", slug, orcid_id, bool(token))
        return Credential(venue=slug, username=orcid_id, password=token, method="orcid", orcid=orcid_id, display_name=entry.get("name", ""))

    kr = _get_keyring()
    if kr is not None and entry.get("username"):
        username = entry["username"]
        try:
            password = kr.get_password(_service_name(slug), username)
        except Exception as exc:
            logger.warning("Could not read the password for %s from the " "keyring (%s)", slug, exc)
            password = None
        if password is not None:
            logger.debug("Loaded password credential for %s from the keyring", slug)
            return Credential(venue=slug, username=username, password=password)

    if "password" in entry and "username" in entry:
        logger.debug("Loaded password credential for %s from the file store", slug)
        return Credential(venue=slug, username=entry["username"], password=entry["password"])
    logger.debug("Credential entry for %s is incomplete; treating as absent", slug)
    return None


def credential_location(slug: str) -> str | None:
    """Where ``slug``'s credential actually lives: 'keyring', 'file', or None."""
    slug = slug.lower()
    entry = _read_file_store().get(slug)
    if not entry:
        return None
    if entry.get("method") == "orcid":
        if entry.get("token"):
            return "file"
        cred = get_credential(slug)
        if cred is None:
            return None
        # A token recovered from the keyring lives there; an identity-only
        # ORCID login (public iD, no token) is recorded in the file store.
        return "keyring" if cred.password else "file"
    if "password" in entry:
        return "file"
    cred = get_credential(slug)
    return "keyring" if cred is not None else None


def delete_credential(slug: str) -> bool:
    """Remove any stored credential for ``slug``. Returns True if one existed."""
    slug = slug.lower()
    removed = False

    store = _read_file_store()
    entry = store.get(slug)

    kr = _get_keyring()
    if kr is not None and entry and entry.get("username"):
        try:
            kr.delete_password(_service_name(slug), entry["username"])
            removed = True
        except Exception as exc:
            logger.debug("Keyring had no password to delete for %s (%s)", slug, exc)

    if entry is not None:
        del store[slug]
        _write_file_store(store)
        removed = True

    logger.info("Deleted stored credential for %s (existed=%s)", slug, removed)
    return removed
