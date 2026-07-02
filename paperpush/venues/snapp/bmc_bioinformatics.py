"""BMC Bioinformatics submission runner (Playwright).

Thin binding for the Springer Nature "Snapp" platform implemented in
:mod:`paperpush.venues.snapp.main`: selects the BMC Bioinformatics
:class:`~paperpush.venues.snapp.main.Variant` (sign-in via a captured IDP
gateway URL) and exposes the uniform ``VENUE`` object plus the
``login_bmc_bioinformatics`` sign-in-and-hold entry point.
"""

from __future__ import annotations

from . import main
from .main import (  # noqa: F401 -- re-exported as this module's public API
    SnappLoginError,
    Variant,
)


class BmcBioinformaticsVenue(main.SnappVenue):
    """The BMC Bioinformatics venue: a Springer Nature Snapp deployment."""

    slug = "bmc_bioinformatics"
    variant = main.VARIANTS["bmc_bioinformatics"]


VENUE = BmcBioinformaticsVenue()


def login_bmc_bioinformatics(headless: bool = False, new_session: bool = False) -> None:
    """Open BMC Bioinformatics, sign in, and leave the browser open (see :func:`main.login`)."""
    main.login(VENUE, headless=headless, new_session=new_session)
