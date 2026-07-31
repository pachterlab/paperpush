"""ORCID-based sign-in for journal submission systems.

Many editorial managers (Editorial Manager, ScholarOne, eJournalPress, and the
target system here) offer a "Sign in with ORCID" button instead of a separate
username and password. This module provides the pieces ``paperpush login
--orcid`` needs:

* :func:`is_valid_id` / :func:`normalize_id` validate and canonicalize an ORCID
  iD, including its ISO 7064 MOD 11-2 checksum, so a typo is caught before any
  network call.
* :func:`fetch_profile` reads the author's public ORCID record (name, primary
  affiliation, public email) so those fields can be filled in programmatically.
* :func:`run_oauth_flow` performs the three-legged OAuth "/authenticate" flow
  used by the real ORCID button: it opens the browser, captures the redirect on
  a loopback server, and exchanges the code for the authenticated iD. This path
  is only available when a registered ORCID client is configured (see below).

Configuration (all optional):

* ``PAPERPUSH_ORCID_CLIENT_ID`` / ``PAPERPUSH_ORCID_CLIENT_SECRET`` --
  credentials of a registered ORCID API client. When both are set the full
  OAuth flow is used; otherwise ``login --orcid`` falls back to an assisted flow
  that opens the ORCID sign-in page and asks for the iD, then reads the public
  record.
* ``PAPERPUSH_ORCID_SANDBOX=1`` -- talk to ORCID's sandbox
  (``sandbox.orcid.org``) instead of the production registry.

Only the Python standard library is used, so the dependency footprint does not
grow.
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 15  # seconds for any ORCID API call
OAUTH_TIMEOUT = 300  # seconds to wait for the user to authorize in the browser


class OrcidError(Exception):
    """Raised when an ORCID identity cannot be obtained or looked up."""


# --- endpoints ------------------------------------------------------------


def _sandbox() -> bool:
    return os.environ.get("PAPERPUSH_ORCID_SANDBOX", "0").lower() in {"1", "true", "yes", "on"}


def _base_host() -> str:
    return "sandbox.orcid.org" if _sandbox() else "orcid.org"


def authorize_endpoint() -> str:
    return f"https://{_base_host()}/oauth/authorize"


def token_endpoint() -> str:
    return f"https://{_base_host()}/oauth/token"


def signin_url() -> str:
    return f"https://{_base_host()}/signin"


def public_api_base() -> str:
    host = "pub.sandbox.orcid.org" if _sandbox() else "pub.orcid.org"
    return f"https://{host}/v3.0"


# --- identity model -------------------------------------------------------


@dataclass(frozen=True)
class OrcidProfile:
    """The public-facing identity tied to an ORCID iD."""

    orcid_id: str
    name: str = ""
    email: str = ""
    affiliation: str = ""


# --- iD validation --------------------------------------------------------


def normalize_id(value: str) -> str:
    """Return ``value`` as a bare, hyphen-grouped ORCID iD.

    Accepts a full URL (``https://orcid.org/0000-0002-1825-0097``), a bare iD
    with or without hyphens, and is case-insensitive for the trailing ``X``
    check digit. Does not validate the checksum; use :func:`is_valid_id`.
    """
    text = value.strip()
    # Strip a URL prefix if present.
    if "orcid.org/" in text:
        text = text.split("orcid.org/", 1)[1]
    text = text.strip("/").strip()
    compact = text.replace("-", "").replace(" ", "").upper()
    if len(compact) == 16:
        return "-".join(compact[i : i + 4] for i in range(0, 16, 4))
    return text


def _checksum_char(first_15: str) -> str:
    """ISO 7064 MOD 11-2 check character for the first 15 digits."""
    total = 0
    for ch in first_15:
        total = (total + int(ch)) * 2
    remainder = total % 11
    result = (12 - remainder) % 11
    return "X" if result == 10 else str(result)


def is_valid_id(value: str) -> bool:
    """True if ``value`` is a structurally valid ORCID iD with a good checksum."""
    compact = normalize_id(value).replace("-", "")
    if len(compact) != 16:
        return False
    body, check = compact[:15], compact[15]
    if not body.isdigit():
        return False
    if check not in "0123456789X":
        return False
    return _checksum_char(body) == check


# --- public record lookup -------------------------------------------------


def _http_get_json(url: str) -> dict:
    """GET ``url`` and parse a JSON body. Isolated so tests can stub it."""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "paperpush",
        },
    )
    logger.debug("ORCID GET %s", url)
    # Defense in depth: the URL is always built from the hardcoded https ORCID
    # hosts above, but guard the scheme so a future caller can't slip in a
    # file:// or other local scheme (the concern behind bandit B310).
    if not url.lower().startswith("https://"):
        raise OrcidError(f"refusing to fetch non-HTTPS ORCID URL: {url}")
    try:
        # scheme guarded above, so file:// and custom schemes are rejected
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.warning("ORCID GET %s returned HTTP %s", url, exc.code)
        if exc.code == 404:
            raise OrcidError("no public ORCID record found for that iD") from exc
        raise OrcidError(f"ORCID API returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("ORCID GET %s could not be reached (%s)", url, exc)
        raise OrcidError(f"could not reach the ORCID API ({exc})") from exc
    except json.JSONDecodeError as exc:
        logger.warning("ORCID GET %s returned an unreadable body", url)
        raise OrcidError("ORCID API returned an unreadable response") from exc


def parse_record(orcid_id: str, record: dict) -> OrcidProfile:
    """Extract the fields paperpush cares about from a v3.0 ORCID record.

    The structure is read defensively: any section the author has marked
    private (or that is simply absent) is skipped rather than raising.
    """
    name = ""
    person = record.get("person") or {}
    name_block = person.get("name") or {}
    given = ((name_block.get("given-names") or {}).get("value") or "").strip()
    family = ((name_block.get("family-name") or {}).get("value") or "").strip()
    credit = ((name_block.get("credit-name") or {}).get("value") or "").strip()
    if credit:
        name = credit
    else:
        name = " ".join(part for part in (given, family) if part)

    email = ""
    emails = ((person.get("emails") or {}).get("email")) or []
    for entry in emails:
        value = (entry.get("email") or "").strip()
        if value:
            # Prefer one flagged primary; otherwise take the first.
            if entry.get("primary"):
                email = value
                break
            if not email:
                email = value

    affiliation = ""
    activities = record.get("activities-summary") or {}
    employments = activities.get("employments") or {}
    groups = employments.get("affiliation-group") or []
    for group in groups:
        for summary in group.get("summaries") or []:
            org = (summary.get("employment-summary") or {}).get("organization") or {}
            org_name = (org.get("name") or "").strip()
            if org_name:
                affiliation = org_name
                break
        if affiliation:
            break

    return OrcidProfile(orcid_id=normalize_id(orcid_id), name=name, email=email, affiliation=affiliation)


def fetch_profile(orcid_id: str) -> OrcidProfile:
    """Look up the public record for ``orcid_id`` from the ORCID public API."""
    if not is_valid_id(orcid_id):
        raise OrcidError(f"'{orcid_id}' is not a valid ORCID iD")
    canonical = normalize_id(orcid_id)
    logger.debug("Fetching public ORCID record for %s", canonical)
    record = _http_get_json(f"{public_api_base()}/{canonical}/record")
    profile = parse_record(canonical, record)
    logger.info("Read public ORCID record for %s (name=%s, affiliation=%s)", canonical, bool(profile.name), bool(profile.affiliation))
    return profile


# --- three-legged OAuth ("Sign in with ORCID") ----------------------------


def oauth_available() -> bool:
    """True if a registered ORCID client is configured for the full OAuth flow."""
    return bool(os.environ.get("PAPERPUSH_ORCID_CLIENT_ID") and os.environ.get("PAPERPUSH_ORCID_CLIENT_SECRET"))


def open_signin() -> bool:
    """Open the ORCID sign-in page in a browser. Returns True if a browser opened."""
    try:
        return webbrowser.open(signin_url())
    except Exception:
        return False


def authorize_url(client_id: str, redirect_uri: str, scope: str = "/authenticate") -> str:
    """Build the ORCID authorization URL the browser is sent to."""
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": scope,
            "redirect_uri": redirect_uri,
        }
    )
    return f"{authorize_endpoint()}?{query}"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """One-shot handler that captures the ``?code=`` (or ``?error=``) redirect."""

    def do_GET(self):  # noqa: N802 (name mandated by BaseHTTPRequestHandler)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.server.oauth_code = (params.get("code") or [None])[0]
        self.server.oauth_error = (params.get("error_description") or params.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if self.server.oauth_code:
            body = "<h2>ORCID sign-in complete.</h2>" "<p>You can close this tab and return to the terminal.</p>"
        else:
            body = "<h2>ORCID sign-in failed.</h2>" "<p>Return to the terminal for details.</p>"
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, fmt, *args):  # route the default stderr logging to us
        logger.debug("ORCID callback server: " + fmt, *args)


def _exchange_code(code: str, redirect_uri: str) -> dict:
    """Trade an authorization code for a token payload (incl. the ORCID iD)."""
    data = urllib.parse.urlencode(
        {
            "client_id": os.environ["PAPERPUSH_ORCID_CLIENT_ID"],
            "client_secret": os.environ["PAPERPUSH_ORCID_CLIENT_SECRET"],
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        token_endpoint(),
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    logger.debug("Exchanging ORCID authorization code at %s", token_endpoint())
    try:
        # token_endpoint() is a hardcoded https URL; not attacker-controlled
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.warning("ORCID token exchange failed (HTTP %s)", exc.code)
        raise OrcidError(f"token exchange failed (HTTP {exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.warning("ORCID token exchange failed (%s)", exc)
        raise OrcidError(f"token exchange failed ({exc})") from exc


def run_oauth_flow() -> tuple[str, str, str]:
    """Run the interactive "Sign in with ORCID" flow.

    Opens the browser to ORCID, waits for the loopback redirect, and exchanges
    the resulting code for the authenticated identity. Returns
    ``(orcid_id, name, access_token)``. Raises :class:`OrcidError` on any
    failure or timeout.
    """
    if not oauth_available():
        raise OrcidError("no ORCID client configured " "(set PAPERPUSH_ORCID_CLIENT_ID/SECRET)")
    client_id = os.environ["PAPERPUSH_ORCID_CLIENT_ID"]

    # Bind a loopback server on an ephemeral port for the redirect URI.
    try:
        server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    except OSError as exc:
        raise OrcidError(f"could not start a local redirect server ({exc})") from exc
    server.oauth_code = None
    server.oauth_error = None
    server.timeout = OAUTH_TIMEOUT
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    logger.debug("Started ORCID OAuth redirect server on 127.0.0.1:%d", port)

    url = authorize_url(client_id, redirect_uri)
    opened = False
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if not opened:
        # Headless host: the caller should still be able to complete it by hand.
        print(f"Open this URL to authorize with ORCID:\n  {url}")

    # Service exactly one request (the redirect), bounded by the timeout.
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(OAUTH_TIMEOUT)
    try:
        if thread.is_alive():
            logger.warning("Timed out after %ds waiting for ORCID authorization", OAUTH_TIMEOUT)
            raise OrcidError("timed out waiting for ORCID authorization")
        if server.oauth_error:
            logger.warning("ORCID authorization reported an error: %s", server.oauth_error)
            raise OrcidError(f"ORCID reported: {server.oauth_error}")
        if not server.oauth_code:
            raise OrcidError("no authorization code was returned by ORCID")
        logger.debug("Received ORCID authorization code; exchanging for a token")
        payload = _exchange_code(server.oauth_code, redirect_uri)
    finally:
        server.server_close()

    orcid_id = normalize_id(payload.get("orcid", ""))
    if not is_valid_id(orcid_id):
        raise OrcidError("ORCID did not return a valid iD")
    name = (payload.get("name") or "").strip()
    token = (payload.get("access_token") or "").strip()
    logger.info("Completed ORCID OAuth flow for %s (token issued=%s)", orcid_id, bool(token))
    return orcid_id, name, token


# --- programmatic field population ----------------------------------------


def _name_matches(author_name: str, profile_name: str) -> bool:
    a = set(author_name.lower().replace(",", " ").split())
    b = set(profile_name.lower().replace(",", " ").split())
    return bool(a) and bool(b) and len(a & b) >= 1


def fill_author_block(block_text: str, profile: OrcidProfile, fields: list[str] | None = None) -> tuple[str, str | None]:
    """Fill ``profile`` into the matching author line of an author block.

    The author block is the value of an ``authorlist`` field: one author per
    line, ``|``-delimited in the venue's column order (``fields``, defaulting to
    ``Name | email | affiliation | ORCID | corresponding``). The target line is
    chosen by name match against the profile, falling back to the corresponding
    author, then the sole author if there is only one. Only columns the venue
    actually declares are written -- arXiv's list is name-only, so there is
    nothing to fill and the block comes back unchanged. Where the column exists,
    the ORCID one is always set; empty email and affiliation columns are filled
    from the public record but existing values are left untouched.

    Returns ``(new_block_text, matched_name)`` where ``matched_name`` is None if
    no author line could be matched.
    """
    # Local import avoids a circular import at module load time.
    from .validate import (DEFAULT_AUTHOR_FIELDS, _author_name,
                           _subfield_specs, parse_authors)

    columns = [name for name, _ in _subfield_specs(fields or DEFAULT_AUTHOR_FIELDS)]
    authors = parse_authors(block_text, fields)
    if not authors:
        return block_text, None

    target = None
    if profile.name:
        for author in authors:
            if _name_matches(_author_name(author), profile.name):
                target = author
                break
    if target is None and "corresponding" in columns:
        corresponding = [a for a in authors if a["corresponding"]]
        if len(corresponding) == 1:
            target = corresponding[0]
    if target is None and len(authors) == 1:
        target = authors[0]
    if target is None:
        logger.debug("No author line matched ORCID iD %s among %d author(s)", profile.orcid_id, len(authors))
        return block_text, None
    matched_name = _author_name(target)
    logger.debug("Matched ORCID iD %s to author %r", profile.orcid_id, matched_name)

    if "orcid" in columns:
        target["orcid"] = profile.orcid_id
    if "email" in columns and not target["email"] and profile.email:
        target["email"] = profile.email
    if "affiliation" in columns and not target["affiliation"] and profile.affiliation:
        target["affiliation"] = profile.affiliation

    def _cell(author: dict, col: str) -> str:
        if col == "corresponding":
            return "yes" if author[col] else "no"
        return author[col]

    lines = [" | ".join(_cell(author, col) for col in columns) for author in authors]
    return "\n".join(lines), matched_name
