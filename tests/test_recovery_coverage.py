"""Every runner that claims step recovery really wires it up, and vice versa.

``Venue.supports_recovery`` is what ``submit --recover`` / ``--ai`` and the MCP
server's agent recovery read to decide whether to turn recovery on. It is a
promise about the runner's code: that ``submit`` wraps its page with
:func:`paperpush.venues.recovery.guard` and launches the browser with
:func:`paperpush.venues.recovery_channel.browser_launch_args`. These tests
check the promise against the source, so a runner can't claim recovery without
the guard, or guard its page without saying so.
"""

from __future__ import annotations

import inspect
import sys

import pytest

from paperpush import venues
from paperpush.database import list_venues


def _runner_impls():
    seen = {}
    for venue in list_venues():
        if not venue.has_form:
            continue
        try:
            impl = venues.get_venue_impl(venues.submission_base(venue.slug))
        except (KeyError, ImportError):
            continue
        seen[type(impl)] = impl
    return sorted(seen.values(), key=lambda impl: impl.slug)


def _runner_source(impl) -> str:
    """The source of the module that defines ``impl``'s submit (where the runner lives)."""
    submit = type(impl).submit
    module = sys.modules[submit.__module__]
    text = inspect.getsource(module)
    # A thin per-venue subclass delegates to a shared engine module; include
    # the module of the class itself too.
    own = sys.modules[type(impl).__module__]
    if own is not module:
        text += inspect.getsource(own)
    return text


IMPLS = _runner_impls()


@pytest.mark.parametrize("impl", IMPLS, ids=lambda impl: impl.slug)
def test_supports_recovery_matches_the_runner_code(impl):
    source = _runner_source(impl)
    guarded = "guard(page" in source
    assert impl.supports_recovery == guarded, f"{impl.slug}: supports_recovery={impl.supports_recovery} but the runner {'does' if guarded else 'does not'} call guard(page)"
    if impl.supports_recovery:
        assert "browser_launch_args()" in source, f"{impl.slug}: guarded runner does not launch with browser_launch_args(), so --cdp-port is ignored"


def test_final_submit_names_are_strings():
    for impl in IMPLS:
        assert isinstance(impl.final_submit_names, tuple)
        assert all(isinstance(name, str) and name.strip() for name in impl.final_submit_names), impl.slug
