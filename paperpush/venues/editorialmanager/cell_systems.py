"""Cell Systems submission runner (Playwright).

Thin binding for the Cell Systems venue on the Editorial Manager wizard
(implemented in :mod:`paperpush.venues.editorialmanager.main`): selects the
Cell Systems Variant and exposes the uniform ``VENUE`` object.
"""

from __future__ import annotations

from . import main
from .main import (  # noqa: F401 -- re-exported as this module's public API
    EditorialManagerLoginError, Variant)


class CellSystemsVenue(main.EditorialManagerVenue):
    """The Cell Systems venue: an Editorial Manager deployment."""

    slug = "cell_systems"
    variant = main.VARIANTS["cell_systems"]


VENUE = CellSystemsVenue()
