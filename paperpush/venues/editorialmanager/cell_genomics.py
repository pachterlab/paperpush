"""Cell Genomics submission runner (Playwright).

Thin binding for the Cell Genomics venue on the Editorial Manager wizard
(implemented in :mod:`paperpush.venues.editorialmanager.main`): selects the
Cell Genomics Variant and exposes the uniform ``VENUE`` object.
"""

from __future__ import annotations

from . import main
from .main import (  # noqa: F401 -- re-exported as this module's public API
    EditorialManagerLoginError, Variant)


class CellGenomicsVenue(main.EditorialManagerVenue):
    """The Cell Genomics venue: an Editorial Manager deployment."""

    slug = "cell_genomics"
    variant = main.VARIANTS["cell_genomics"]


VENUE = CellGenomicsVenue()
