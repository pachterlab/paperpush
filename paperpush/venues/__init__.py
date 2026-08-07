"""Per-venue submission runners, grouped by submission portal.

Each supported venue has a module under the portal subpackage that drives its
submission wizard with Playwright. A portal shared by more than one venue keeps
its engine in that subpackage's ``main`` module (e.g.
:mod:`paperpush.venues.editorialmanager.main`,
:mod:`paperpush.venues.nature.main`); each venue is a thin binding that
selects its deployment. Code common to every portal lives in
:mod:`paperpush.venues.common`.

:data:`SLUG_TO_MODULE` maps a venue slug to the module implementing its runner
and is the single source of truth for where a venue lives: :func:`get_runner`,
:func:`get_field_validators`, and the login check in
:mod:`paperpush.venues.common` all resolve through it. It is not hand-written
but discovered from the subpackage layout by :func:`_discover_slug_to_module`
(each venue leaf is ``<portal>/<slug>.py`` exposing a module-level ``VENUE``), so
the layout is literally the only place a module location is declared -- adding a
venue means adding its file, nothing else. The one exception is the ``template/``
subpackage (see :data:`_SCAFFOLD_PORTALS`): it is a copy-me scaffold, not a real
portal, so its leaf is skipped even though it defines a ``VENUE``.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Callable

# Portal subpackages that are scaffolding, not real venues. ``template/`` ships a
# leaf that defines a module-level ``VENUE`` (so it reads as a fully worked
# example an author can copy), but it must never register as a live slug. Skipped
# by name in discovery so the exclusion holds even once the directory is a proper
# package -- rather than relying on it lacking an ``__init__.py``.
_SCAFFOLD_PORTALS = frozenset({"template"})


def _discover_slug_to_module() -> dict[str, str]:
    """Build the slug -> module registry by scanning the portal subpackages.

    Each venue leaf module is named ``<slug>.py`` and lives under its portal
    subpackage (``<portal>/<slug>.py``), so the slug is the filename stem and the
    module path is ``<portal>.<slug>``. A leaf counts as a venue -- rather than a
    shared ``main`` engine or a helper -- exactly when it assigns a module-level
    ``VENUE`` (the instance :func:`get_venue_impl` returns). That marker is read
    from the source with :mod:`ast`, so discovery never imports the module and
    thus never needs Playwright; a half-finished leaf that has not defined its
    ``VENUE`` yet is simply not registered.

    Raises :class:`RuntimeError` if two portals ship a leaf with the same stem,
    since a slug must resolve to a single module.
    """
    registry: dict[str, str] = {}
    origin: dict[str, str] = {}
    venues_dir = Path(__file__).parent
    for module_file in sorted(venues_dir.glob("*/*.py")):
        stem, portal = module_file.stem, module_file.parent.name
        if stem == "__init__" or portal in _SCAFFOLD_PORTALS or not (module_file.parent / "__init__.py").exists():
            continue
        tree = ast.parse(module_file.read_text(encoding="utf-8"), filename=str(module_file))
        defines_venue = any(isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VENUE" for t in node.targets) for node in tree.body)
        if not defines_venue:
            continue
        if stem in registry:
            raise RuntimeError(f"Duplicate venue slug {stem!r} in portals {origin[stem]!r} and {portal!r}; " "a slug must map to a single module.")
        registry[stem] = f"{portal}.{stem}"
        origin[stem] = portal
    return registry


# Venue slug -> module (dotted, relative to this package) implementing its runner,
# discovered from the portal subpackage layout. The subpackage is the submission
# portal; the leaf module ``<slug>.py`` is the venue, marked by its module-level
# ``VENUE``. A portal with a single code-level venue (arxiv, science) is just
# ``<portal>/<venue>.py``; a shared portal has a ``main`` engine (no ``VENUE``, so
# it is not itself a venue) plus one thin module per venue. To add a venue, drop
# its ``<slug>.py`` into the portal subpackage -- there is nothing to register.
SLUG_TO_MODULE = _discover_slug_to_module()

# Venues that submit *through* another venue's portal -- they share that base
# venue's runner, stored credentials, and saved browser session. This is a
# code-level fact about which submission implementation exists, kept here next to
# the registry rather than in ``venues.json``: the data file's ``inherits`` key
# describes only the field *schema* a venue derives from, which is independent.
# A venue may share another's portal without inheriting its schema, inherit its
# schema without sharing its portal (the Nature family: nature_biotech and
# nature_methods inherit nature's schema but each has its own eJP deployment, so
# they are deliberately absent here), or both (the AAAS family below, which all
# submit through Science's CTS wizard, the differing publication carried in the
# ``.sub`` rather than the code).
_SUBMISSION_BASE = {
    "science_advances": "science",
    "science_immunology": "science",
    "science_robotics": "science",
    "science_signaling": "science",
    "science_translational_medicine": "science",
}


def submission_base(slug: str) -> str:
    """Slug whose runner, credentials, and saved session ``slug`` submits through.

    Returns the base venue for one that submits through another's portal (see
    :data:`_SUBMISSION_BASE`), or the venue's own (lowercased) slug otherwise.
    Callers that dispatch a runner or look up a stored credential or session file
    should key on this so a whole family (e.g. all AAAS venues) signs in once as
    the base, while a venue with its own portal (e.g. nature_methods) keeps its
    own login even when it inherits another venue's field schema.
    """
    key = slug.lower()
    return _SUBMISSION_BASE.get(key, key)


def submission_aliases(slug: str) -> list[str]:
    """Slugs that submit through ``slug``'s portal, sharing its one login.

    Returns the other venues whose :func:`submission_base` is ``slug`` (e.g. the
    AAAS siblings that all sign in as ``science``), sorted, excluding ``slug``
    itself. Empty for a venue that no others submit through. Callers that report a
    stored credential can list these alongside it so a whole family reads as
    separately-logged-in journals even though the credential is stored once.
    """
    key = slug.lower()
    return sorted(alias for alias, base in _SUBMISSION_BASE.items() if base == key and alias != key)


# Venues whose submission system has no "Sign in with ORCID" control, so
# ``paperpush login --orcid`` is not offered for them. Conference portals are
# excluded by venue type in :func:`orcid_login_offered`; this names the journals
# and preprint servers that authenticate only with their own accounts.
_NO_ORCID_LOGIN = frozenset({"discrete_mathematics"})


def orcid_login_offered(slug: str) -> bool:
    """Whether ``paperpush login --orcid`` is accepted for ``slug``.

    Two ways to qualify, because offering the option and being able to drive it
    are different facts:

    * the venue's implementation declares ``supports_orcid_login`` -- its ORCID
      flow has been recorded, so the option demonstrably works; or
    * it is a journal or preprint server not listed in :data:`_NO_ORCID_LOGIN`.
      Those portals carry an ORCID button even where paperpush cannot drive it
      yet, so the option is accepted and the venue reports that the flow is not
      implemented (rather than pretending ORCID is not a way in at all).

    Conference portals are excluded: they run on their own accounts. Resolved
    through :func:`submission_base` so a venue that submits through another's
    portal answers for the portal it actually signs in to. An unknown slug is
    simply not offered ORCID rather than raising, since callers reach here only
    after the slug has been checked against the database.
    """
    from ..database import get_venue

    base = submission_base(slug)
    impl = try_get_venue_impl(base)
    if impl is not None and not impl.requires_login:
        return False
    if impl is not None and impl.supports_orcid_login:
        return True
    if base in _NO_ORCID_LOGIN:
        return False
    try:
        return get_venue(base).venue_type in {"journal", "preprint"}
    except KeyError:
        return False


def login_required(slug: str) -> bool:
    """Whether submitting to ``slug`` requires a stored or manual sign-in.

    Unknown or temporarily unimportable venues conservatively return ``True``.
    Known loginless implementations (currently EditFlow) declare
    :attr:`~paperpush.venues.base.Venue.requires_login` as ``False`` so the CLI
    and end-to-end pipeline skip credential collection entirely.
    """
    impl = try_get_venue_impl(submission_base(slug))
    return True if impl is None else impl.requires_login


def import_venue(slug: str):
    """Import and return the module implementing ``slug``'s runner.

    Resolves through :data:`SLUG_TO_MODULE`. Raises :class:`KeyError` if no module
    is registered for the slug, or propagates :class:`ImportError` if the module
    exists but a dependency (e.g. Playwright) is missing.
    """
    path = SLUG_TO_MODULE[slug.lower()]
    return importlib.import_module(f".{path}", __package__)


def try_import_venue(slug: str):
    """Return the module implementing ``slug``'s runner, or ``None``.

    Like :func:`import_venue` but swallows an unknown slug or an unimportable
    module (e.g. an optional dependency is absent), so callers that merely probe a
    venue's capabilities can degrade gracefully rather than raising.
    """
    path = SLUG_TO_MODULE.get(slug.lower())
    if path is None:
        return None
    try:
        return importlib.import_module(f".{path}", __package__)
    except Exception:  # noqa: BLE001 -- an unimportable module just means "no module"
        return None


def get_venue_impl(slug: str):
    """Return the :class:`~paperpush.venues.base.Venue` for ``slug``.

    Resolves through :data:`SLUG_TO_MODULE` to the venue module, then returns its
    single ``VENUE`` instance -- the object exposing ``submit``/``login``/
    ``is_logged_in`` that the dispatch, login check, and validation layer share.

    Raises :class:`KeyError` for an unknown slug, or :class:`ImportError` if the
    module exists but a dependency is missing. Does not apply
    :func:`submission_base`: callers that want a family to resolve to its base
    venue's implementation apply it themselves (see :func:`get_runner`).
    """
    return import_venue(slug).VENUE


def try_get_venue_impl(slug: str):
    """Return the :class:`~paperpush.venues.base.Venue` for ``slug``, or ``None``.

    Like :func:`get_venue_impl` but swallows an unknown slug or an unimportable
    module, for callers that merely probe a venue's capabilities.
    """
    module = try_import_venue(slug)
    if module is None:
        return None
    return getattr(module, "VENUE", None)


def get_runner(slug: str) -> Callable:
    """Return the ``submit`` callable for ``slug`` (case-insensitive).

    A venue that submits through another's portal (e.g. the AAAS family that all
    submit through Science's CTS wizard) resolves to that base venue's runner
    via :func:`submission_base`; the differing publication is carried in the
    ``.sub`` rather than the code, so one runner serves the whole family.

    Raises :class:`KeyError` if no runner is registered for the venue.
    """
    return get_venue_impl(submission_base(slug)).submit


def get_field_validators(slug: str) -> dict[str, Callable[[str], str]]:
    """Return the venue module's custom per-field validators, or ``{}``.

    A venue module may expose ``FIELD_VALIDATORS``, a mapping of field id to a
    callable that takes the field's raw ``.sub`` string value and returns it
    unchanged, or raises :class:`ValueError` with a clear message. The validation
    layer (:func:`paperpush.validate._model_for`) layers these onto the
    generic pydantic schema so a venue can enforce rules its field metadata
    cannot express -- for example that Nature's subject paths name real branches
    of the category tree and number between two and five.

    Returns an empty mapping when the venue has no module or declares no custom
    validators (including when the module cannot be imported, e.g. an optional
    dependency is missing), so validation degrades gracefully rather than failing.
    """
    impl = try_get_venue_impl(slug)
    if impl is None:
        return {}
    return dict(getattr(impl, "field_validators", {}) or {})


def get_field_option_listers(slug: str) -> dict[str, Callable[..., list[str]]]:
    """Return the venue module's custom per-field option listers, or ``{}``.

    A venue whose allowed values for a field are not a flat list in the field
    metadata -- Nature's variable-depth subject tree, browsed one level at a
    time -- exposes ``field_option_listers``, a mapping of field id to a callable
    that takes the drill-down path (zero or more level names) and returns the
    option strings one level deeper. ``paperpush options VENUE.FIELD [PATH...]``
    resolves through this, falling back to a field's flat ``options`` when no
    lister is registered.

    Returns an empty mapping when the venue has no module or declares no listers
    (including when the module cannot be imported, e.g. an optional dependency is
    missing), so the ``options`` command degrades gracefully rather than failing.
    """
    impl = try_get_venue_impl(slug)
    if impl is None:
        return {}
    return dict(getattr(impl, "field_option_listers", {}) or {})
