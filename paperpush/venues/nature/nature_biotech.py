"""The Nature Biotechnology venue: an eJP deployment sharing Nature's flow.

Thin binding over :mod:`paperpush.venues.nature.main` -- selects the Nature
Biotechnology Variant and exposes the package's ``VENUE`` object plus the
``login_nature_biotech`` / ``check_categories`` entry points.
"""

from __future__ import annotations

from . import main
from .main import (  # noqa: F401 -- re-exported as this module's public API
    CategoryCheckReport,
    FIELD_OPTION_LISTERS,
    FIELD_VALIDATORS,
    NatureLoginError,
    Variant,
    convert_scraped_categories,
    list_nature_categories,
)


class NatureBiotechVenue(main.EJPVenue):
    """The Nature Biotechnology venue: an eJP deployment sharing Nature's flow."""

    slug = "nature_biotech"
    variant = main.VARIANTS["nature_biotech"]


VENUE = NatureBiotechVenue()


def login_nature_biotech(headless: bool = False, new_session: bool = False) -> None:
    """Open Nature Biotechnology, sign in, and leave the browser open (see :func:`main.login_nature`)."""
    main.login_nature(headless=headless, new_session=new_session, cfg=VENUE.variant)


def verify_nature_categories(page, update: bool = True, settle_ms: int = 1000) -> bool:
    """Scrape Nature Biotechnology's live subject categories (see :func:`main.verify_nature_categories`)."""
    return main.verify_nature_categories(page, update=update, settle_ms=settle_ms, cfg=VENUE.variant)


def check_categories(headless: bool = True, manuscript: str | None = None, discard: bool = True):
    """Refresh the stored subject-category tree (see :func:`main.check_categories`)."""
    return main.check_categories(headless=headless, manuscript=manuscript, discard=discard, cfg=VENUE.variant)
