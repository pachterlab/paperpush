"""Combinatorica submission runner on MSP's EditFlow portal."""

from __future__ import annotations

import re

from ...database import get_venue
from .main import EditFlowVenue

_MSC_RE = re.compile(r"^\d{2}[A-Z]\d{2}$")


def validate_msc_classification(value: str) -> str:
    """Require one five-character Mathematics Subject Classification code."""
    code = value.strip().upper()
    if not _MSC_RE.fullmatch(code):
        raise ValueError("use one five-character MSC code such as 05C05")
    return code


class CombinatoricaVenue(EditFlowVenue):
    """Combinatorica's EditFlow deployment."""

    slug = "combinatorica"
    _VENUE = get_venue(slug)
    field_validators = {"msc_classification": validate_msc_classification}


VENUE = CombinatoricaVenue()
