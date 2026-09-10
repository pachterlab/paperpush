"""Cell submission runner (Playwright).

Thin binding for the Cell venue on the Editorial Manager wizard (implemented in
:mod:`paperpush.venues.editorialmanager.main`): selects the Cell Variant (the
default deployment) and exposes the uniform ``VENUE`` object.
"""

from __future__ import annotations

from . import main
from .main import EditorialManagerLoginError, Variant  # noqa: F401 -- re-exported as this module's public API


class CellVenue(main.EditorialManagerVenue):
    """The Cell venue: the default Editorial Manager deployment."""

    slug = "cell"
    variant = main.VARIANTS["cell"]


VENUE = CellVenue()
