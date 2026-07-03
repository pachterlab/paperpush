"""PLOS Computational Biology submission runner (Playwright).

Thin binding for the PLOS Computational Biology venue on the Editorial Manager
wizard (implemented in :mod:`paperpush.venues.editorialmanager.main`): selects
the PLOS Variant and exposes the uniform ``VENUE`` object.
"""

from __future__ import annotations

from . import main
from .main import (  # noqa: F401 -- re-exported as this module's public API
    EditorialManagerLoginError, Variant)


class PlosCompbioVenue(main.EditorialManagerVenue):
    """The PLOS Computational Biology venue: an Editorial Manager deployment."""

    slug = "plos_compbio"
    variant = main.VARIANTS["plos_compbio"]


VENUE = PlosCompbioVenue()
