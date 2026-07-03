"""paperpush: one-click manuscript submission to academic venues."""

import logging as _logging

__version__ = "0.1.0"
__url__ = "https://github.com/pachterlab/paperpush"

# Standard-library convention: a library should not configure logging output.
# The NullHandler keeps importing paperpush silent until the embedding
# application (or our own CLI, via configure_logging) installs a real handler.
_logging.getLogger(__name__).addHandler(_logging.NullHandler())

from ._logging import configure_logging
from .autofill import (AutofillApiError, AutofillResult, DocumentInput,
                       Extraction, Proposal, autofill, effective_role,
                       extract_via_api, field_schema, parse_extraction)
from .credentials import (Credential, delete_credential, get_credential,
                          save_credential)
from .database import Field, Venue, get_venue, list_venues
from .subfile import SubFile, load, parse, render_template, write_template

__all__ = [
    "__version__",
    "__url__",
    "configure_logging",
    "Field",
    "Venue",
    "get_venue",
    "list_venues",
    "SubFile",
    "load",
    "parse",
    "render_template",
    "write_template",
    "autofill",
    "AutofillResult",
    "Extraction",
    "Proposal",
    "parse_extraction",
    "effective_role",
    "field_schema",
    "extract_via_api",
    "AutofillApiError",
    "DocumentInput",
    "Credential",
    "save_credential",
    "get_credential",
    "delete_credential",
]
