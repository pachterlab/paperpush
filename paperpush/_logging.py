"""Logging configuration for the paperpush command-line interface.

Library modules obtain a logger with ``logging.getLogger(__name__)`` and never
install handlers themselves. Following the standard-library convention, the
package's top-level logger carries a :class:`logging.NullHandler` (attached in
:mod:`paperpush.__init__`), so importing paperpush as a library stays
silent unless the embedding application configures logging.

The command-line entry point calls :func:`configure_logging` once to attach a
single stderr handler whose level reflects the ``-v``/``-q`` flags, or the
``PAPERPUSH_LOG_LEVEL`` environment variable when set. Diagnostics go to
stderr so they never mix with command output on stdout.
"""

from __future__ import annotations

import logging
import os
import sys

PACKAGE_LOGGER = "paperpush"

# Each additional ``-v`` raises the verbosity one step from the WARNING default:
# -v -> INFO, -vv -> DEBUG.
_VERBOSITY_LEVELS = [logging.WARNING, logging.INFO, logging.DEBUG]

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"

# Marks the handler this module installs so a later call can replace it rather
# than stack a second one.
_HANDLER_FLAG = "_paperpush_cli_handler"


def _level_from_env() -> int | None:
    """Return the level named by ``PAPERPUSH_LOG_LEVEL``, or None if unset/bad."""
    name = os.environ.get("PAPERPUSH_LOG_LEVEL")
    if not name:
        return None
    resolved = logging.getLevelName(name.strip().upper())
    # getLevelName returns the int for a known name, or a "Level X" string for
    # an unknown one; only accept a real level.
    return resolved if isinstance(resolved, int) else None


def resolve_level(verbosity: int = 0, quiet: bool = False) -> int:
    """Pick a logging level from the CLI flags and the environment.

    ``PAPERPUSH_LOG_LEVEL`` (e.g. ``DEBUG``) wins when set to a valid level;
    otherwise ``quiet`` forces ``ERROR`` and each ``-v`` raises the verbosity one
    step from the ``WARNING`` default.
    """
    env_level = _level_from_env()
    if env_level is not None:
        return env_level
    if quiet:
        return logging.ERROR
    index = min(max(verbosity, 0), len(_VERBOSITY_LEVELS) - 1)
    return _VERBOSITY_LEVELS[index]


def configure_logging(verbosity: int = 0, quiet: bool = False) -> logging.Logger:
    """Attach a single stderr handler to the package logger and set its level.

    Safe to call more than once: a handler installed by an earlier call is
    replaced rather than stacked, so repeated CLI invocations within one process
    (notably the test suite) do not emit duplicate lines.
    """
    level = resolve_level(verbosity, quiet)
    logger = logging.getLogger(PACKAGE_LOGGER)

    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_FLAG, False):
            logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    setattr(handler, _HANDLER_FLAG, True)
    logger.addHandler(handler)
    logger.setLevel(level)
    # Diagnostics flow only through our handler, not whatever the root logger has.
    logger.propagate = False
    logger.debug("Logging configured at level %s", logging.getLevelName(level))
    return logger
