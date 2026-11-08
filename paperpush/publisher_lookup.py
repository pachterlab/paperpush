"""Find the publisher of a journal paperpush does not list, so it can be checked against the house rules.

``paperpush validate --issn 1234-5678`` (or ``--journal "Journal of X"``) asks
Crossref's journal registry who publishes the journal, then matches Crossref's
publisher name against the ``crossref_names`` in ``$publishers``
(``manuscript_requirements.json``). The match picks the ``publisher:<slug>``
house rules to validate against (see :func:`paperpush.database.publisher_venue`).

Needs network access. Every failure (no network, unknown ISSN, a publisher with
no house rules) comes back as a :class:`Lookup` whose ``problem`` says what went
wrong, so the caller can print it and carry on with the general checks.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .requirements import Publisher, get_publisher, get_requirements, list_publishers, publisher_key

logger = logging.getLogger(__name__)

USER_AGENT = "paperpush-validate (+https://github.com/pachterlab/paperpush)"
CROSSREF_JOURNALS = "https://api.crossref.org/journals"
TIMEOUT_S = 15

_ISSN = re.compile(r"^\d{4}-?\d{3}[\dXx]$")


@dataclass(frozen=True)
class Lookup:
    """What the registry said about a journal."""

    journal: str = ""  # the journal's title, as Crossref records it
    issn: str = ""
    crossref_publisher: str = ""  # the publisher's name, as Crossref records it
    publisher: Optional[Publisher] = None  # the $publishers entry it matched
    problem: str = ""  # why no house rules can be used ("" when they can)

    @property
    def venue_slug(self) -> str:
        """The ``publisher:<slug>`` key to validate against, or "" when there is none."""
        return publisher_key(self.publisher.slug) if self.publisher is not None and not self.problem else ""

    def describe(self) -> str:
        """One line for the terminal."""
        who = f"{self.journal or self.issn} is published by {self.crossref_publisher}" if self.crossref_publisher else (self.journal or self.issn)
        return f"{who}: {self.problem}" if self.problem else f"{who}; checking it against {self.publisher.display_name}'s house rules"


def normalize_issn(issn: str) -> str:
    """``issn`` as ``NNNN-NNNC``, or raise ``ValueError``."""
    issn = issn.strip()
    if not _ISSN.match(issn):
        raise ValueError(f"{issn!r} is not an ISSN (8 characters, e.g. 1234-5678)")
    issn = issn.replace("-", "").upper()
    return f"{issn[:4]}-{issn[4:]}"


def _get(url: str) -> Optional[dict]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # nosec B310
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        logger.info("Crossref journal lookup failed: %s", exc)
    except (OSError, ValueError) as exc:
        logger.info("Crossref journal lookup failed: %s", exc)
    return None


def _fold(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).replace(" and ", " ").strip()


def match_publisher(crossref_name: str) -> Optional[Publisher]:
    """The ``$publishers`` entry whose Crossref names include ``crossref_name`` (or that it starts with)."""
    found = get_publisher(crossref_name)
    if found is not None:
        return found
    folded = _fold(crossref_name)
    for pub in list_publishers():
        for name in [pub.name, *pub.crossref_names]:
            name = _fold(name)
            if name and (folded.startswith(name + " ") or folded == name):
                return pub
    return None


def _result(journal: str, issn: str, crossref_publisher: str) -> Lookup:
    pub = match_publisher(crossref_publisher) if crossref_publisher else None
    if not crossref_publisher:
        return Lookup(journal, issn, problem="Crossref records no publisher for it")
    if pub is None:
        return Lookup(journal, issn, crossref_publisher, problem="paperpush has no house rules for that publisher")
    if get_requirements(publisher_key(pub.slug)) is None:
        return Lookup(journal, issn, crossref_publisher, pub, problem=f"paperpush has no house rules recorded for {pub.display_name} yet")
    return Lookup(journal, issn, crossref_publisher, pub)


def listed_venue(title: str):
    """The listed venue whose name or full name is ``title`` (case and punctuation aside), or None."""
    from .database import list_venues

    wanted = _fold(title)
    if not wanted:
        return None
    for venue in list_venues():
        names = {venue.name, venue.full_name, venue.full_name.split(" - ")[0], re.sub(r"\s*\(.*?\)\s*$", "", venue.full_name)}
        if wanted in {_fold(n) for n in names if n}:
            return venue
    return None


def lookup(issn: str = "", journal: str = "") -> Lookup:
    """The publisher of the journal with this ISSN (preferred) or title, from Crossref."""
    if issn:
        try:
            issn = normalize_issn(issn)
        except ValueError as exc:
            return Lookup(issn=issn, problem=str(exc))
        data = _get(f"{CROSSREF_JOURNALS}/{issn}")
        if data is None:
            return Lookup(issn=issn, problem="could not reach Crossref to look up the publisher")
        message = data.get("message") or {}
        if not message:
            return Lookup(issn=issn, problem="Crossref does not know this ISSN")
        return _result(str(message.get("title") or ""), issn, str(message.get("publisher") or ""))
    if not journal.strip():
        return Lookup(problem="give an ISSN or a journal title")
    data = _get(f"{CROSSREF_JOURNALS}?" + urllib.parse.urlencode({"query": journal.strip()[:200], "rows": 10}))
    if data is None:
        return Lookup(journal=journal, problem="could not reach Crossref to look up the publisher")
    items = [i for i in (data.get("message") or {}).get("items") or [] if isinstance(i, dict)]
    wanted = _fold(journal)
    exact = [i for i in items if _fold(str(i.get("title") or "")) == wanted]
    if not exact:
        near = ", ".join(f"'{i.get('title')}'" for i in items[:3] if i.get("title"))
        return Lookup(journal=journal, problem="Crossref has no journal titled exactly that" + (f" (closest: {near}); pass --issn instead" if near else "; pass --issn instead"))
    item = exact[0]
    issns = [str(x) for x in item.get("ISSN") or []]
    return _result(str(item.get("title") or journal), issns[0] if issns else "", str(item.get("publisher") or ""))


class UnknownPublisher(ValueError):
    """``--publisher`` names no publisher paperpush knows."""


@dataclass(frozen=True)
class Target:
    """The venue ``validate`` checks against for ``--publisher`` / ``--issn`` / ``--journal``."""

    venue: object = None  # a paperpush.database.Venue, or None for the general checks only
    message: str = ""  # what was found, for the terminal or the tool's notes


def resolve_target(current_venue: str = "", publisher: str = "", issn: str = "", journal: str = "") -> Optional[Target]:
    """The venue to validate a journal paperpush does not list against, or None when no option was given.

    ``current_venue`` is the venue the ``.sub`` (or ``--venue``) names, if any:
    the options are for a journal paperpush does not list, so a listed venue
    refuses them (its publisher's house rules already back its own). A title or
    ISSN that turns out to be a listed journal gets that venue. A lookup that
    finds no house rules gives ``Target(None, why)``: run the general checks.
    Raises :class:`UnknownPublisher` or ``ValueError`` when the options cannot be used.
    """
    if not (publisher or issn or journal):
        return None
    from .database import get_venue, publisher_venue
    from .requirements import is_publisher_key

    if current_venue and current_venue.lower() != "general" and not is_publisher_key(current_venue):
        raise ValueError(f"publisher, ISSN, and journal are for a journal paperpush does not list; {current_venue} is listed, and its publisher's house rules already back its own")
    if publisher:
        pub = get_publisher(publisher)
        if pub is None:
            raise UnknownPublisher(f"unknown publisher '{publisher}' (see 'paperpush publishers')")
        if not pub.has_rules:
            raise ValueError(f"paperpush has no house rules recorded for {pub.display_name} yet; only the general checks can run (leave the publisher out)")
        return Target(publisher_venue(pub.slug, journal=journal or ""), f"checking against {pub.display_name}'s house rules")
    listed = listed_venue(journal) if journal else None
    found = None
    if listed is None:
        found = lookup(issn=issn or "", journal=journal or "")
        listed = listed_venue(found.journal) if found.journal else None
    if listed is not None:
        return Target(get_venue(listed.slug), f"paperpush lists {listed.name} ({listed.slug}); checking it against its own guidelines")
    if not found.venue_slug:
        return Target(None, found.describe() + "; running the general checks only")
    return Target(publisher_venue(found.venue_slug, journal=found.journal or journal or ""), found.describe())
