"""paperpush: one-click manuscript submission to academic venues."""

import logging

__version__ = "0.1.4"
__url__ = "https://github.com/pachterlab/paperpush"

# Standard-library convention: a library should not configure logging output.
# The NullHandler keeps importing paperpush silent until the embedding
# application (or our own CLI, via paperpush._logging.configure_logging)
# installs a real handler.
#
# Import stdlib logging under its own name -- NOT ``as _logging`` -- so it does
# not shadow the ``paperpush._logging`` submodule as a package attribute, which
# would make ``from paperpush import _logging`` hand back the stdlib module.
logging.getLogger(__name__).addHandler(logging.NullHandler())

# Public API. Kept deliberately small: the two data models (with their
# accessors), the subfile toolkit, and the two workflow operations that have a
# library-level entry point. The login and submit steps are exposed only as CLI
# commands (`paperpush login` / `paperpush submit`), not as importable
# functions. Lower-level helpers (autofill extraction internals, credential
# storage, logging setup) remain importable from their submodules
# (e.g. `from paperpush import credentials`) but are not part of the public API.
from .database import Field, Venue, get_venue, list_venues
from .subfile import SubFile, load, parse, render_template, write_template
from .autofill import autofill
from .validate import validate

__all__ = [
    "__version__",
    "__url__",
    # Data models
    "Field",
    "Venue",
    "get_venue",
    "list_venues",
    # Submission files
    "SubFile",
    "load",
    "parse",
    "render_template",
    "write_template",
    # Workflow operations
    "autofill",
    "validate",
]
