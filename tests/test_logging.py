"""Behavioural guards for the CLI logging configuration.

:mod:`paperpush._logging` is the single place that turns the ``-v``/``-q`` flags
and the ``PAPERPUSH_LOG_LEVEL`` environment variable into a concrete level and a
lone stderr handler on the package logger. These tests pin that mapping, the
environment override, and the install-once handler behaviour so a change to the
diagnostics plumbing can't silently drop, duplicate, or mis-level output.

The package logger sets ``propagate = False`` once configured, so pytest's
``caplog`` (which listens on the root logger) never sees ``paperpush`` records.
The end-to-end tests therefore capture real ``stderr`` via ``capsys``: the
handler binds to ``sys.stderr`` at construction, and ``capsys`` has already
swapped it, so emitted lines land in the captured buffer.
"""

from __future__ import annotations

import logging
import sys

import pytest

# This must resolve to the paperpush submodule, not the stdlib ``logging``
# module. It regresses if ``paperpush/__init__.py`` ever re-aliases stdlib
# logging as ``_logging`` and shadows this package attribute again.
from paperpush import _logging


@pytest.fixture
def restore_package_logger():
    """Snapshot the package logger and restore it after the test.

    ``configure_logging`` mutates the global ``paperpush`` logger (its handlers,
    level, and ``propagate`` flag). Saving and restoring that state keeps a
    logging test from leaking its handler or level into the rest of the suite.
    """
    logger = logging.getLogger(_logging.PACKAGE_LOGGER)
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    try:
        yield logger
    finally:
        logger.handlers[:] = saved_handlers
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


@pytest.fixture
def clear_log_env(monkeypatch):
    """Ensure ``PAPERPUSH_LOG_LEVEL`` is unset so flag-based tests are hermetic."""
    monkeypatch.delenv("PAPERPUSH_LOG_LEVEL", raising=False)


def _our_handlers(logger):
    """The handlers this module's ``configure_logging`` installed (flagged)."""
    return [h for h in logger.handlers if getattr(h, _logging._HANDLER_FLAG, False)]


# --- resolve_level ---------------------------------------------------------


@pytest.mark.parametrize(
    "verbosity, quiet, expected",
    [
        (0, False, logging.WARNING),  # default
        (1, False, logging.INFO),  # -v
        (2, False, logging.DEBUG),  # -vv
        (5, False, logging.DEBUG),  # clamps rather than indexing out of range
        (0, True, logging.ERROR),  # -q
        (2, True, logging.ERROR),  # -q wins over -vv
    ],
)
def test_resolve_level_from_flags(clear_log_env, verbosity, quiet, expected):
    assert _logging.resolve_level(verbosity, quiet) == expected


def test_env_var_overrides_flags(clear_log_env, monkeypatch):
    monkeypatch.setenv("PAPERPUSH_LOG_LEVEL", "DEBUG")
    # -q would otherwise force ERROR; a valid environment level wins outright.
    assert _logging.resolve_level(verbosity=0, quiet=True) == logging.DEBUG


def test_env_var_is_case_insensitive_and_trimmed(clear_log_env, monkeypatch):
    monkeypatch.setenv("PAPERPUSH_LOG_LEVEL", "  info ")
    assert _logging.resolve_level() == logging.INFO


def test_unknown_env_var_falls_back_to_flags(clear_log_env, monkeypatch):
    monkeypatch.setenv("PAPERPUSH_LOG_LEVEL", "LOUD")  # not a real level name
    assert _logging.resolve_level(verbosity=1) == logging.INFO


# --- configure_logging -----------------------------------------------------


def test_configure_installs_single_stderr_handler(clear_log_env, restore_package_logger):
    logger = _logging.configure_logging(verbosity=1)

    handlers = _our_handlers(logger)
    assert len(handlers) == 1
    handler = handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stderr
    assert logger.level == logging.INFO
    assert logger.propagate is False


def test_configure_is_idempotent(clear_log_env, restore_package_logger):
    _logging.configure_logging(verbosity=0)
    _logging.configure_logging(verbosity=2)

    logger = logging.getLogger(_logging.PACKAGE_LOGGER)
    # A second call replaces the handler rather than stacking a duplicate...
    assert len(_our_handlers(logger)) == 1
    # ...and the level reflects the most recent call.
    assert logger.level == logging.DEBUG


def test_verbose_emits_info_to_stderr(clear_log_env, restore_package_logger, capsys):
    _logging.configure_logging(verbosity=1)  # INFO

    logging.getLogger("paperpush.test").info("hello-info")

    err = capsys.readouterr().err
    assert "hello-info" in err


def test_default_level_suppresses_info_but_keeps_warnings(clear_log_env, restore_package_logger, capsys):
    _logging.configure_logging(verbosity=0)  # WARNING default

    log = logging.getLogger("paperpush.test")
    log.info("quiet-info")
    log.warning("loud-warning")

    err = capsys.readouterr().err
    assert "quiet-info" not in err
    assert "loud-warning" in err


def test_quiet_suppresses_warnings(clear_log_env, restore_package_logger, capsys):
    _logging.configure_logging(quiet=True)  # ERROR

    log = logging.getLogger("paperpush.test")
    log.warning("dropped-warning")
    log.error("kept-error")

    err = capsys.readouterr().err
    assert "dropped-warning" not in err
    assert "kept-error" in err
