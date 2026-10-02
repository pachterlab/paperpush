"""Rank the supported venues against a loose user query.

The submission pipeline is slug-driven -- ``subfile``, ``login``, and ``submit``
all take a venue slug, which presumes the user already knows the exact slug of
their target venue. This module inverts that: it turns free-form text (an
abbreviation, a keyword, a venue type, or a portal name) into an ordered
shortlist of venues, so a human or an AI agent can go from "I want to put a
preprint somewhere" to ``paperpush subfile biorxiv`` without guessing at the
slug first.

Matching is purely local. Query tokens are scored against each venue's metadata
in ``venues.json`` (slug, name, full name, description, and type) and against
its submission portal as resolved from the module layout. There is no network
request, no LLM, and no user state.
"""

from __future__ import annotations

from dataclasses import dataclass

from .database import Venue, list_venues
from .venues import SLUG_TO_MODULE, submission_base


@dataclass(frozen=True)
class VenueHit:
    """A venue and how well it matched the query.

    ``score`` orders the shortlist (higher is better). ``reasons`` lists, in
    descending weight, the fields that matched a token -- e.g. ``slug`` beat
    ``name`` -- so a user can see why a venue ranked where it did.
    """

    venue: Venue
    score: int
    portal: str
    reasons: list[str]


def portal_of(slug: str) -> str:
    """The submission-portal name ``slug`` submits through, or ``""``.

    Resolves through :func:`submission_base` so a venue that shares a family
    portal (e.g. every AAAS journal signs in as ``science``) reports the portal
    its login actually runs on. Reads the module registry only -- it never
    imports a venue's Playwright code -- so it stays cheap and
    dependency-free.
    """
    module = SLUG_TO_MODULE.get(submission_base(slug.lower()))
    return "" if module is None else module.split(".", 1)[0]


def _score_tokens(venue: Venue, portal: str, tokens: list[str]) -> tuple[int, list[str]]:
    """Total score and per-field reasons for a venue against query tokens.

    ``tokens`` must already be lowercased. Each token contributes through the
    first matching field only; ``slug`` is the strongest signal, ``portal`` the
    weakest, so an exact slug outranks a keyword in a description.
    """
    score = 0
    reasons: list[str] = []
    name = venue.name.lower()
    full_name = venue.full_name.lower()
    description = venue.description.lower()
    for token in tokens:
        reason = ""
        if token == venue.slug:
            score += 100
            reason = "slug"
        elif venue.slug.startswith(token):
            score += 60
            reason = "slug-prefix"
        elif token == name:
            score += 80
            reason = "name"
        elif name.startswith(token):
            score += 40
            reason = "name-prefix"
        elif full_name and full_name.startswith(token):
            score += 35
            reason = "full-name-prefix"
        elif venue.venue_type == token:
            score += 20
            reason = "type"
        elif token in description:
            score += 15
            reason = "description"
        elif portal == token:
            score += 20
            reason = "portal"
        elif portal.startswith(token):
            score += 10
            reason = "portal-prefix"
        if reason:
            reasons.append(reason)
    return score, reasons


def pick_venues(
    query: str = "",
    *,
    venue_types: tuple[str, ...] = (),
    portals: tuple[str, ...] = (),
    venues: list[Venue] | None = None,
) -> list[VenueHit]:
    """Rank the non-deprecated venues against ``query``.

    ``query`` is a free-text string whose whitespace-separated tokens are scored
    against every candidate; only venues with at least one token match are
    returned (an empty ``query`` keeps every candidate, so filters alone can
    drive the shortlist). ``venue_types`` and ``portals`` narrow the field
    first: a venue is a candidate only if it passes both filters when either is
    given. Results are ordered by descending score and, on a tie, by slug.

    ``venues`` is the candidate list; it defaults to :func:`list_venues`. Unit
    tests may pass a fixed list instead, so this stays deterministic.
    """
    candidates = list_venues() if venues is None else list(venues)
    slug_to_hit: dict[str, VenueHit] = {}

    for venue in candidates:
        portal = portal_of(venue.slug)
        if venue_types and venue.venue_type not in venue_types:
            continue
        if portals and portal not in portals:
            continue
        slug_to_hit[venue.slug] = VenueHit(venue, 0, portal, [])

    tokens = query.strip().lower().split()
    if tokens:
        for slug, hit in list(slug_to_hit.items()):
            score, reasons = _score_tokens(hit.venue, hit.portal, tokens)
            if score <= 0:
                del slug_to_hit[slug]
            else:
                slug_to_hit[slug] = VenueHit(hit.venue, score, hit.portal, reasons)

    return sorted(slug_to_hit.values(), key=lambda hit: (-hit.score, hit.venue.slug))


def format_hits(hits: list[VenueHit]) -> list[str]:
    """Render hits as display lines, one per venue.

    Each line is ``slug  Name (type) -- description`` with the slug column
    padded to the widest slug in the list and the description truncated, so the
    output lines up whether there are two hits or twenty.
    """
    width = max((len(hit.venue.slug) for hit in hits), default=0)
    lines: list[str] = []
    for hit in hits:
        head = f"{hit.venue.slug:<{width}}  {hit.venue.name} ({hit.venue.venue_type})"
        description = " ".join(hit.venue.description.split())
        if description:
            head += f" -- {description[:72]}" + ("…" if len(description) > 72 else "")
        lines.append(head)
    return lines
