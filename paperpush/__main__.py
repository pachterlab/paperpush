"""Allow ``python -m paperpush`` as an alias for the ``paperpush`` script.

The console-script entry point (``pyproject.toml``) is the normal way in, but a
module entry point lets a caller invoke the CLI through a *specific*
interpreter -- ``sys.executable -m paperpush ...`` -- which is how
:mod:`paperpush.mcp_server` shells out to ``login``. That guarantees the
subprocess runs the same install as the caller, rather than whichever
``paperpush`` happens to be first on ``PATH``.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
