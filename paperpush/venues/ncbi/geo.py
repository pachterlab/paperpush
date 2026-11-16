"""GEO (Gene Expression Omnibus) sign-in through NCBI's account gateway.

Login only: GEO's submission flow has no runner yet, so ``LOGIN_ONLY`` keeps
GEO out of the submit registry.
"""

from __future__ import annotations

from .main import NCBIVenue

LOGIN_ONLY = True


class GEO(NCBIVenue):
    slug = "geo"
    signed_out_selector = "#sign_in"
    header_selector = ".myncbi"


VENUE = GEO()
