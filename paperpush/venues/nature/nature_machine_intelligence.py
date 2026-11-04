"""The Nature Machine Intelligence venue: a Nature eJP deployment.

Thin binding over :mod:`paperpush.venues.nature.main` -- selects the Nature Machine Intelligence
Variant and exposes the package's ``VENUE`` object plus the ``login_nature_machine_intelligence`` /
``check_categories`` entry points. See ``main.py`` for the implementation.
"""

from __future__ import annotations

from . import main
from .main import FIELD_OPTION_LISTERS, FIELD_VALIDATORS, CategoryCheckReport, NatureLoginError, Variant, convert_scraped_categories, list_nature_categories  # noqa: F401 -- re-exported as this module's public API


class NatureMachineIntelligenceVenue(main.EJPVenue):
    """The Nature Machine Intelligence venue: a Nature eJP deployment."""

    slug = "nature_machine_intelligence"
    variant = main.VARIANTS["nature_machine_intelligence"]


VENUE = NatureMachineIntelligenceVenue()


def login_nature_machine_intelligence(headless: bool = False, new_session: bool = False) -> None:
    """Open Nature Machine Intelligence, sign in, and leave the browser open (see :func:`main.login_nature`)."""
    main.login_nature(headless=headless, new_session=new_session, cfg=VENUE.variant)


def verify_nature_categories(page, update: bool = True, settle_ms: int = 1000) -> bool:
    """Scrape Nature Machine Intelligence's live subject categories (see :func:`main.verify_nature_categories`)."""
    return main.verify_nature_categories(page, update=update, settle_ms=settle_ms, cfg=VENUE.variant)


def check_categories(headless: bool = True, manuscript: str | None = None, discard: bool = True):
    """Refresh the stored subject-category tree (see :func:`main.check_categories`)."""
    return main.check_categories(headless=headless, manuscript=manuscript, discard=discard, cfg=VENUE.variant)
