"""Conformance tests for the venue interface.

Every submittable venue exposes a single :class:`~paperpush.venues.base.Venue`
named ``VENUE`` -- the object the dispatch, login check, and validation layer
resolve by slug. These tests pin that contract so a new venue that forgets its
``VENUE`` (or ships one missing ``submit`` / ``login``) fails here rather than deep
in a submission run.

``paperpush.venues.SLUG_TO_MODULE`` is the registry of venues that have a
submission implementation; ``_SUBMISSION_BASE`` lists venues that submit through
another's portal (e.g. the AAAS family through Science). Both are covered.
"""

from __future__ import annotations

import abc
from pathlib import Path

import pytest

from paperpush import venues
from paperpush.venues import _SUBMISSION_BASE
from paperpush.venues.base import Venue

# Every venue with its own submission implementation, and every alias that
# submits through one -- the full set of slugs a `submit` can dispatch.
SUBMITTABLE = sorted(set(venues.SLUG_TO_MODULE) | set(_SUBMISSION_BASE))

# A baseline of venues that must always be discovered -- one per portal, so a
# regression in discovery that silently drops a whole subpackage is caught even
# though the registry itself is generated rather than hand-listed.
_EXPECTED_BASELINE = {"arxiv", "biorxiv", "medrxiv", "cell", "bmc_bioinformatics", "bioinformatics", "nature", "science", "combinatorica"}


def test_slug_to_module_is_discovered_from_filenames():
    """The registry is derived from ``<portal>/<slug>.py`` -- key == filename stem.

    This pins the convention the discovery relies on: a venue's slug is exactly
    its leaf module's filename, and the module path is ``<portal>.<slug>``. If a
    leaf were misnamed (or its stem drifted from its slug) it would show up here.
    """
    venues_dir = Path(venues.__file__).parent
    for slug, dotted in venues.SLUG_TO_MODULE.items():
        portal, _, stem = dotted.partition(".")
        assert stem == slug, f"{slug!r} maps to leaf {stem!r}; the module must be named <slug>.py"
        assert (venues_dir / portal / f"{slug}.py").is_file(), f"no file for {dotted!r}"


def test_discovery_finds_every_portal():
    """A representative venue from each portal is discovered (no silent drop)."""
    missing = _EXPECTED_BASELINE - set(venues.SLUG_TO_MODULE)
    assert not missing, f"discovery lost venues: {sorted(missing)}"


@pytest.mark.parametrize("slug", sorted(venues.SLUG_TO_MODULE), ids=sorted(venues.SLUG_TO_MODULE))
def test_module_exposes_a_venue_object(slug):
    """Each registered module exposes one ``VENUE`` that is a :class:`Venue`."""
    module = venues.import_venue(slug)
    impl = getattr(module, "VENUE", None)
    assert impl is not None, f"{slug} module defines no VENUE object"
    assert isinstance(impl, Venue), f"{slug} VENUE is {type(impl)!r}, not a Venue"
    assert impl.slug == slug, f"{slug} VENUE.slug is {impl.slug!r}"


@pytest.mark.parametrize("slug", SUBMITTABLE, ids=SUBMITTABLE)
def test_venue_implements_the_required_operations(slug):
    """Every submittable venue -- base or alias -- has submit/login/is_logged_in.

    ``submit`` and ``login`` are the venue-specific abstract methods; ``is_logged_in``
    is supplied by the base class from the ``logged_in_*`` attributes, so it is
    always present too.
    """
    impl = venues.get_venue_impl(venues.submission_base(slug))
    assert isinstance(impl, Venue)
    for op in ("submit", "login", "is_logged_in"):
        assert callable(getattr(impl, op)), f"{slug}.{op} is not callable"


@pytest.mark.parametrize("slug", SUBMITTABLE, ids=SUBMITTABLE)
def test_get_runner_returns_the_venue_run(slug):
    """`submit`'s dispatch resolves to the venue's own ``submit`` (aliases included)."""
    impl = venues.get_venue_impl(venues.submission_base(slug))
    assert venues.get_runner(slug) == impl.submit


def test_field_validators_come_from_the_venue():
    """A venue's declared ``field_validators`` are what the validation layer sees."""
    for slug in venues.SLUG_TO_MODULE:
        impl = venues.get_venue_impl(slug)
        assert venues.get_field_validators(slug) == dict(impl.field_validators or {})
    # At least one venue actually declares validators, so this is a real check.
    assert venues.get_field_validators("nucleic_acids_research")


def test_incomplete_subclass_cannot_be_instantiated():
    """The ABC enforces the contract: omitting an operation fails at construction.

    This is what lets the rest of the package treat every ``VENUE`` as complete
    instead of probing for loosely-agreed function names. ``submit`` and ``login`` are
    the abstract methods; a subclass that defines only ``submit`` (here) is missing
    ``login`` and cannot be instantiated.
    """

    class MissingLogin(Venue):
        slug = "broken"
        logged_in_names = ("Sign out",)

        def submit(self, values, **kwargs): ...

    with pytest.raises(TypeError):
        MissingLogin()

    # Sanity: the base class really is abstract.
    assert isinstance(Venue, abc.ABCMeta)


def test_loginless_venue_opens_the_public_form_without_a_session():
    impl = venues.get_venue_impl("combinatorica")

    class Page:
        urls = []

        def goto(self, url):
            self.urls.append(url)

    page = Page()
    assert impl.requires_login is False
    assert impl.ensure_signed_in(page, object()) is True
    assert page.urls == [impl.portal_url]

    with pytest.raises(NotImplementedError, match="does not require"):
        impl.save_session()
