"""medRxiv submission runner (Playwright).

Thin binding for the medRxiv venue on the openRxiv platform: it selects the
medRxiv :class:`~paperpush.venues.openrxiv.main.Variant` and exposes the
uniform ``VENUE`` object plus the ``check_medrxiv`` interface checker. medRxiv
keeps its own portal host, credentials, and session. See ``main.py`` for the
implementation.
"""

from __future__ import annotations

from . import main
from .main import (  # noqa: F401 -- re-exported as this module's public API
    CheckReport,
    OpenRxivLoginError as MedrxivLoginError,
    StepResult,
    Variant,
)


class MedrxivVenue(main.OpenRxivVenue):
    """The medRxiv venue: the openRxiv deployment with its own portal host."""

    slug = "medrxiv"
    variant = main.VARIANTS["medrxiv"]


VENUE = MedrxivVenue()


def check_medrxiv(headless: bool = True, timeout_ms: int = 8000, discard: bool = False) -> CheckReport:
    """Walk the medRxiv wizard and report missing selectors (see :func:`main.check_biorxiv`)."""
    return main.check_biorxiv(headless=headless, timeout_ms=timeout_ms, discard=discard, cfg=VENUE.variant)
