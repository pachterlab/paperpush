"""bioRxiv submission runner (Playwright).

Thin binding for the bioRxiv venue on the openRxiv platform: it selects the
default :class:`~paperpush.venues.openrxiv.main.Variant` and exposes the
uniform ``VENUE`` object plus the ``check_biorxiv`` interface checker. See
``main.py`` for the implementation.
"""

from __future__ import annotations

from . import main
from .main import (  # noqa: F401 -- re-exported as this module's public API
    CheckReport, OpenRxivLoginError, StepResult, Variant)


class BiorxivVenue(main.OpenRxivVenue):
    """The bioRxiv venue: the default openRxiv deployment."""

    slug = "biorxiv"
    variant = main.VARIANTS["biorxiv"]


VENUE = BiorxivVenue()


def check_biorxiv(headless: bool = True, timeout_ms: int = 8000, discard: bool = False) -> CheckReport:
    """Walk the bioRxiv wizard and report missing selectors (see :func:`main.check_biorxiv`)."""
    return main.check_biorxiv(headless=headless, timeout_ms=timeout_ms, discard=discard, cfg=VENUE.variant)
