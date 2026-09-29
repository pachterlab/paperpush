"""Check the authors' OpenReview profiles before an OpenReview submission.

ICLR and AAAI take submissions on OpenReview, where every author must already
have a profile, and ICLR decides reciprocal-reviewer eligibility from those
profiles: "Incorrect information on your profile will be grounds for desk
rejection." The portal adds each author by searching their name, so a name that
matches no profile, or several, stalls the submission form.

For each line of the ``.sub``'s OpenReview author list this looks the author up
through the OpenReview API (by the Open Review ID when given, else by exact
name) and reports:

* no profile (an error when the ID was given; a warning when only the name
  was searched, since the name search is not exhaustive);
* several profiles with the same name and no ID to pick one;
* a profile that is not active yet (still in moderation);
* a name that does not match the profile, when both are given;
* no current position in the profile's career history;
* email suffixes that appear nowhere on the profile;
* for authors who will be reciprocal reviewers, no DBLP link or expertise;
* the same profile listed twice, and no reciprocal reviewer at all.

OpenReview challenges anonymous API clients, so the lookup signs in with the
login ``paperpush login <venue>`` stored. Without one the check is skipped
with a note.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger(__name__)

API = "https://api2.openreview.net"
TIMEOUT = 20.0
# Author-list columns (see venues.json) this check reads.
ID_COLUMN = "open_review_id"


@dataclass(frozen=True)
class Problem:
    error: bool
    field: str
    message: str


def _request(method: str, path: str, *, token: str | None = None, body: dict | None = None) -> tuple[int, dict | None]:
    """``(status, json)`` for one API call; ``(0, None)`` when the host is unreachable."""
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json", "User-Agent": "paperpush-validate"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # fixed https host  # nosec B310
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            payload = None
        return exc.code, payload
    except Exception:
        logger.debug("OpenReview request %s %s failed", method, path, exc_info=True)
        return 0, None


@lru_cache(maxsize=4)
def _login(username: str, password: str) -> tuple[str | None, str]:
    """``(token, reason)``: a session token, or None with why it could not be had."""
    status, payload = _request("POST", "/login", body={"id": username, "password": password})
    if status == 200 and payload and payload.get("token"):
        return payload["token"], ""
    if status == 0:
        return None, "OpenReview could not be reached"
    return None, "OpenReview rejected the stored login"


def _token(venue_slug: str) -> tuple[str | None, str]:
    from . import credentials, venues

    try:
        base = venues.submission_base(venue_slug)
    except Exception:
        base = venue_slug
    cred = credentials.get_credential(base)
    if cred is None or cred.method != "password":
        return None, f"no OpenReview login is stored for {base}; run 'paperpush login {base}' to enable it"
    token, reason = _login(cred.username, cred.password)
    if token is None:
        return None, f"{reason}; run 'paperpush login {base}' again if the password changed" if "rejected" in reason else reason
    return token, ""


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


def _names(profile: dict) -> list[str]:
    return [n.get("fullname", "") for n in profile.get("content", {}).get("names", []) if n.get("fullname")]


def _domains(profile: dict) -> set[str]:
    """Email domains and institution domains on a profile (emails are masked, domains are not)."""
    content = profile.get("content", {})
    domains = {e.rsplit("@", 1)[-1].lower() for e in content.get("emails", []) + content.get("emailsConfirmed", []) if "@" in e}
    for entry in content.get("history", []):
        domain = (entry.get("institution") or {}).get("domain")
        if domain:
            domains.add(domain.lower())
    return domains


def _has_current_position(profile: dict, year: int) -> bool:
    for entry in profile.get("content", {}).get("history", []):
        end = entry.get("end")
        if end is None or end == "" or (isinstance(end, (int, float)) and end >= year) or (isinstance(end, str) and end[:4].isdigit() and int(end[:4]) >= year):
            return True
    return False


def _suffix_matches(suffix: str, domains: set[str]) -> bool:
    suffix = suffix.strip().lower().lstrip("@.")
    return any(d == suffix or d.endswith("." + suffix) or suffix.endswith("." + d) for d in domains)


def _fetch_by_ids(ids: list[str], token: str) -> dict[str, dict] | None:
    if not ids:
        return {}
    status, payload = _request("GET", "/profiles?" + urllib.parse.urlencode({"ids": ",".join(ids)}), token=token)
    if status != 200 or payload is None:
        return None
    found: dict[str, dict] = {}
    for profile in payload.get("profiles", []):
        # A profile answers to its canonical id and to every ~username it holds.
        for key in {profile.get("id", "")} | {n.get("username", "") for n in profile.get("content", {}).get("names", [])}:
            if key:
                found[key] = profile
    return found


def _search_name(name: str, token: str) -> tuple[list[dict], int] | None:
    """Profiles whose name is exactly ``name``, and how many the search matched in all."""
    status, payload = _request("GET", "/profiles/search?fullname=" + urllib.parse.quote(name), token=token)
    if status != 200 or payload is None:
        return None
    profiles = payload.get("profiles", [])
    exact = [p for p in profiles if any(_norm(n) == _norm(name) for n in _names(p))]
    return exact, int(payload.get("count", len(profiles)) or 0)


def _authorlist_fields(venue):
    return [f for f in venue.fields if f.type == "authorlist" and any(c.rstrip("?") == ID_COLUMN for c in (f.fields or []))]


def applies(venue) -> bool:
    """Whether the venue's author list is an OpenReview one (has an Open Review ID column)."""
    return bool(_authorlist_fields(venue))


def check(venue, values: dict[str, str], *, today: _dt.date | None = None) -> list[Problem]:
    """Every OpenReview-profile problem in the ``.sub``'s author lists."""
    from .validate import parse_authors

    year = (today or _dt.date.today()).year
    fields = _authorlist_fields(venue)
    rows = [(f, a) for f in fields for a in parse_authors(values.get(f.id, ""), f.fields)]
    if not rows:
        return []
    problems: list[Problem] = []

    authors_field = next((f for f in fields if "author" in f.id.lower()), None)
    if authors_field is not None:
        # Only where the venue makes it a requirement (ICLR's help text says so).
        required = "reciprocal reviewing requirement" in (authors_field.help or "").lower()
        marked = [a for f, a in rows if f is authors_field and "reciprocal_reviewer" in a]
        if required and marked and not any(str(a.get("reciprocal_reviewer", "")).strip().lower() in ("yes", "y", "true", "1") for a in marked):
            problems.append(Problem(False, authors_field.id, f"no author is marked as a reciprocal reviewer; {venue.name} desk-rejects a submission none of whose authors registers to review"))

    token, reason = _token(venue.slug)
    if token is None:
        problems.append(Problem(False, "", f"OpenReview profile check skipped: {reason}"))
        return problems

    ids = sorted({str(a.get(ID_COLUMN, "")).strip() for _, a in rows if str(a.get(ID_COLUMN, "")).strip().startswith("~")})
    by_id = _fetch_by_ids(ids, token)
    if by_id is None:
        problems.append(Problem(False, "", "OpenReview profile check skipped: the profile lookup failed"))
        return problems

    seen: dict[str, str] = {}
    for field, author in rows:
        is_author = field is authors_field
        rid = str(author.get(ID_COLUMN, "")).strip()
        name = str(author.get("name", "")).strip()
        who = name or rid or "an author"
        profile = None
        if rid.startswith("~"):
            profile = by_id.get(rid)
            if profile is None:
                problems.append(Problem(is_author, field.id, f"{who}: no OpenReview profile has the ID {rid}" + ("; every author needs one before the deadline" if is_author else "; OpenReview will skip this conflict")))
                continue
        elif rid:
            problems.append(Problem(False, field.id, f"{who}: '{rid}' is not an OpenReview ID (they start with '~', e.g. ~Ada_Lovelace1); not checked"))
            continue
        elif name:
            result = _search_name(name, token)
            if result is None:
                problems.append(Problem(False, field.id, f"{who}: the OpenReview name search failed; not checked"))
                continue
            exact, total = result
            suffixes = [s for s in re.split(r"[,\s]+", str(author.get("email_suffixes", ""))) if s]
            if suffixes and len(exact) > 1:
                narrowed = [p for p in exact if any(_suffix_matches(s, _domains(p)) for s in suffixes)]
                exact = narrowed or exact
            if not exact:
                problems.append(Problem(False, field.id, f"{who}: no OpenReview profile is named exactly '{name}'" + ("; every author needs a profile, and the portal adds authors by this name -- add their Open Review ID" if is_author else "; OpenReview will skip this conflict")))
                continue
            if len(exact) > 1:
                shown = ", ".join(p.get("id", "") for p in exact[:5])
                problems.append(Problem(False, field.id, f"{who}: {len(exact)} OpenReview profiles share this name ({shown}{', ...' if len(exact) > 5 else ''}); add the Open Review ID so the right one is used"))
                continue
            profile = exact[0]
        else:
            continue

        pid = profile.get("id", rid)
        if pid in seen:
            problems.append(Problem(False, field.id, f"{who}: the same OpenReview profile ({pid}) is listed twice (also as {seen[pid]})"))
        seen[pid] = who
        if not is_author:
            continue  # a conflict only needs to exist

        state = str(profile.get("state", "") or "")
        if profile.get("active") is False or (state and not state.lower().startswith("active")):
            problems.append(Problem(True, field.id, f"{who}: OpenReview profile {pid} is not active ({state or 'inactive'}); new profiles without an institutional email are moderated, which can take up to two weeks"))
        if rid and name and not any(_norm(n) == _norm(name) for n in _names(profile)):
            problems.append(Problem(False, field.id, f"{who}: the name does not match OpenReview profile {rid} ({', '.join(_names(profile)) or 'no name'}); the portal adds authors by name"))
        if not _has_current_position(profile, year):
            problems.append(Problem(False, field.id, f"{who}: OpenReview profile {pid} lists no current position; OpenReview venues check eligibility and conflicts against profiles, so update Career & Education History"))
        suffixes = [s for s in re.split(r"[,\s]+", str(author.get("email_suffixes", ""))) if s]
        domains = _domains(profile)
        missing = [s for s in suffixes if not _suffix_matches(s, domains)]
        if missing and domains:
            problems.append(Problem(False, field.id, f"{who}: email suffix(es) {', '.join(missing)} appear nowhere on OpenReview profile {pid} ({', '.join(sorted(domains))})"))
        if str(author.get("reciprocal_reviewer", "")).strip().lower() in ("yes", "y", "true", "1"):
            content = profile.get("content", {})
            lacking = [label for key, label in (("dblp", "DBLP link"), ("expertise", "Expertise section")) if not content.get(key)]
            if lacking:
                problems.append(Problem(False, field.id, f"{who}: will review, but OpenReview profile {pid} has no {' or '.join(lacking)}; reviewer eligibility and paper matching use them"))
    return problems
