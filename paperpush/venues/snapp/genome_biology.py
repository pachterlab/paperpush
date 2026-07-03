"""Genome Biology submission runner (Playwright).

Thin binding for the Springer Nature "Snapp" platform implemented in
:mod:`paperpush.venues.snapp.main`: selects the Genome Biology
:class:`~paperpush.venues.snapp.main.Variant` (sign-in lets the portal redirect
to the identity provider) and exposes the uniform ``VENUE`` object plus the
``login_genome_biology`` sign-in-and-hold entry point.
"""

from __future__ import annotations

from . import main
from .main import (  # noqa: F401 -- re-exported as this module's public API
    SnappLoginError, Variant)


class GenomeBiologyVenue(main.SnappVenue):
    """The Genome Biology venue: a Springer Nature Snapp deployment."""

    slug = "genome_biology"
    variant = main.VARIANTS["genome_biology"]


VENUE = GenomeBiologyVenue()


def login_genome_biology(headless: bool = False, new_session: bool = False) -> None:
    """Open Genome Biology, sign in, and leave the browser open (see :func:`main.login`)."""
    main.login(VENUE, headless=headless, new_session=new_session)
