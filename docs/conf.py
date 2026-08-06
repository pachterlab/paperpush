# Configuration file for the Sphinx documentation builder.
#
# Build locally with:
#   pip install -r docs/requirements.txt
#   sphinx-build -b html docs docs/_build/html
# or, from inside docs/:  make html

import os
import sys
from datetime import date

# Make the package importable so ``sphinx.ext.autodoc`` and ``__version__``
# resolve against the checked-out source rather than an installed copy.
sys.path.insert(0, os.path.abspath(".."))

try:
    from paperpush import __version__ as _version
except Exception:  # pragma: no cover - docs should build even if import fails
    _version = "0.1.4"

# -- Project information ------------------------------------------------------

project = "PaperPush"
author = "Joseph Rich"
copyright = f"{date.today().year}, {author}"

version = _version
release = _version

# -- General configuration ---------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "myst_parser",
]

# Parse both reStructuredText and Markdown (the latter via MyST).
source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

# MyST niceties: allow ``key: value`` field lists, definition lists, and
# GitHub-style admonitions inside the Markdown pages we reuse from the repo.
myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "fieldlist",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- HTML output -------------------------------------------------------------

# Prefer the Read the Docs theme; fall back to the built-in "alabaster" so the
# docs still build in an environment without the theme installed.
try:
    import sphinx_rtd_theme  # noqa: F401

    html_theme = "sphinx_rtd_theme"
except ImportError:  # pragma: no cover
    html_theme = "alabaster"

html_title = f"PaperPush {release}"
html_static_path = ["_static"]

# -- Autodoc / Napoleon ------------------------------------------------------

autodoc_typehints = "description"
autodoc_member_order = "bysource"
napoleon_google_docstring = True
napoleon_numpy_docstring = True

# Heavy or optional third-party dependencies that need not be importable for
# autodoc to read our docstrings. Mocking them keeps the Read the Docs build
# light (no Playwright browsers, no Anthropic SDK required).
autodoc_mock_imports = [
    "playwright",
    "anthropic",
]

# -- Intersphinx -------------------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}
