"""Generator for the test ``.sub`` files in ``tests/sub_files/``.

The data lives in ``tests/sample_subfiles.json``; this module is only the code
that turns it into files. Keeping the two apart means contributors extend the
fixtures by editing plain JSON, never this module (see ``CONTRIBUTING.md``).

Each supported venue gets a subdirectory ``sub_files/<venue>/`` holding one or
more ready-to-submit sample submission files (``<venue>.sub`` and, optionally,
``<venue>_<variant>.sub``). They serve two purposes:

* committed ground-truth fixtures that ``paperpush submit <file>`` can run
  end to end, and
* a regression guard: the files are *generated* from the JSON by the very same
  ``paperpush.subfile`` renderer the ``subfile`` command uses, so if that
  command's template format changes the committed files can simply be
  regenerated (and a test flags the drift until they are).

``sample_subfiles.json`` maps each sample's filename to its ``journal`` (the
venue slug) and its ``fields`` (the fully filled-in value map). Every field is
written out in full, so each entry is self-contained: there is no base/overlay
inheritance to trace. Two invariants are enforced when the JSON is loaded (see
:func:`_load_definitions`): every venue has at least one sample, and no two
samples for the same venue carry identical fields.

The input assets the samples point at (manuscript, supplement, figures, ...)
are not hand-maintained either: :func:`ensure_assets` reads the file paths the
samples reference and builds each one from the shared ``conftest.SampleFiles``
factory via :data:`_ASSET_BUILDERS`, so there are no per-venue generator
modules to keep in sync.

Regenerate the committed files after changing the JSON or the renderer::

    python tests/sample_subfiles.py

The browser submission for every venue uses a shared test account whose
credentials are read from the environment, not committed here: see the
``PAPERPUSH_TEST_USERNAME`` / ``PAPERPUSH_TEST_PASSWORD`` variables and
the ``test_username`` / ``test_password`` fixtures in ``conftest.py``
(``paperpush login <venue>``). Note that bioRxiv has no way to delete an
in-progress submission, so the tests here never actually submit; they only
check the files statically.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from paperpush.database import get_venue
from paperpush.subfile import render_template

# The tests/ directory holds the input assets; the committed sample .sub files
# live in tests/sub_files/. REPO_ROOT is where repo-relative input paths resolve.
TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SAMPLES_DIR = TESTS_DIR / "sub_files"
DEFINITIONS_FILE = TESTS_DIR / "sample_subfiles.json"

# Each venue's sample .sub points at the input assets in that venue's own bundle
# under tests/manuscript_files/<venue>/, written as repo-relative paths so a
# generated file can be submitted from the repository root with
# `paperpush submit tests/sub_files/<venue>/<f>.sub`. The bundles are built
# by ensure_assets() from the paths the samples reference (see _ASSET_BUILDERS).
MANUSCRIPT_FILES = "tests/manuscript_files"

# File-typed database fields whose value(s) name input files a sample references.
_FILE_FIELD_TYPES = frozenset({"file", "filelist"})

# The manuscript-bundle assets every venue points at are built from the shared
# conftest.SampleFiles factory (the "master" builders). This maps a referenced
# file's stem (its name without extension) to the factory call that writes it;
# figures -- any path under a ``figures/`` directory -- are handled separately by
# SampleFiles.figure. A sample references only the subset of these its fields ask
# for, and ensure_assets() builds exactly those, so adding a sample needs no new
# generator code -- only, when a sample references a new kind of file, a new
# entry here.
_ASSET_BUILDERS = {
    "manuscript": lambda f, ext: f.manuscript(ext, name="manuscript"),
    "supplement": lambda f, ext: f.supplement(ext, name="supplement"),
    "cover_letter": lambda f, ext: f.cover_letter(ext, name="cover_letter"),
    "declaration_of_interest": lambda f, ext: f.supplement(ext, name="declaration_of_interest"),
    "manuscript_tex_bundle": lambda f, ext: f.latex_bundle(name="manuscript_tex_bundle"),
}


@dataclass(frozen=True)
class Scenario:
    """One sample submission file: a venue plus a set of filled-in values."""

    venue: str
    filename: str
    values: dict[str, str]

    @property
    def variant(self) -> str:
        """The variant tag: ``""`` for ``<venue>.sub``, else the ``_<tag>`` suffix."""
        stem = self.filename[: -len(".sub")]
        return "" if stem == self.venue else stem[len(self.venue) + 1 :]

    @property
    def relpath(self) -> Path:
        """Location of this sample relative to the samples root: ``<venue>/<file>``."""
        return Path(self.venue) / self.filename

    @property
    def path(self) -> Path:
        return SAMPLES_DIR / self.relpath

    def render(self) -> str:
        """Render this scenario to ``.sub`` text via the subfile renderer."""
        venue = get_venue(self.venue)
        return render_template(venue, values=self.values)


def _load_definitions() -> list[Scenario]:
    """Parse ``sample_subfiles.json`` into scenarios, validating its invariants.

    The JSON maps ``<filename>.sub`` -> ``{"journal": <slug>, "fields": {...}}``.
    Raises ``ValueError`` if a filename is not prefixed with its journal slug, if
    a venue has no sample, or if two samples for the same venue are identical --
    the guarantees CONTRIBUTING.md asks contributors to uphold.
    """
    raw = json.loads(DEFINITIONS_FILE.read_text(encoding="utf-8"))
    scenarios: list[Scenario] = []
    seen_per_venue: dict[str, dict[str, str]] = {}
    for filename, entry in raw.items():
        if not filename.endswith(".sub"):
            raise ValueError(f"{filename!r}: sample filenames must end in '.sub'")
        venue = entry["journal"]
        stem = filename[: -len(".sub")]
        if stem != venue and not stem.startswith(f"{venue}_"):
            raise ValueError(f"{filename!r}: filename must be '{venue}.sub' or '{venue}_<variant>.sub'")
        values = entry["fields"]
        fingerprint = json.dumps(values, sort_keys=True)
        prior = seen_per_venue.setdefault(venue, {})
        if fingerprint in prior:
            raise ValueError(f"{filename!r} is identical to {prior[fingerprint]!r}; every sample for a venue must differ")
        prior[fingerprint] = filename
        scenarios.append(Scenario(venue, filename, values))
    if not scenarios:
        raise ValueError(f"{DEFINITIONS_FILE.name} defines no samples")
    return scenarios


_SCENARIOS = _load_definitions()


def scenarios() -> list[Scenario]:
    """Every sample scenario across all venues, in the JSON's declared order."""
    return [Scenario(s.venue, s.filename, dict(s.values)) for s in _SCENARIOS]


def write_all(directory: Path | None = None) -> list[Path]:
    """(Re)generate every sample ``.sub`` file on disk and return the paths."""
    target_dir = Path(directory) if directory is not None else SAMPLES_DIR
    written: list[Path] = []
    for scenario in scenarios():
        path = target_dir / scenario.relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(scenario.render(), encoding="utf-8")
        written.append(path)
    return written


def _sample_files_factory(root: Path):
    """A :class:`conftest.SampleFiles` bound to ``root`` (the master builders).

    Imported from the rootdir ``tests/conftest.py`` lazily so this module can be
    imported (for ``scenarios()``/``write_all()``) without pulling in the test
    fixtures; only asset generation needs it.
    """
    if str(TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(TESTS_DIR))
    from conftest import SampleFiles  # the rootdir conftest, not a venue one

    return SampleFiles(root)


def _referenced_paths(values: dict[str, str], venue) -> list[str]:
    """Every repo-relative path named by ``values``' file/filelist fields."""
    by_id = {f.id: f for f in venue.fields}
    paths: list[str] = []
    for fid, value in values.items():
        field = by_id.get(fid)
        if field is None or field.type not in _FILE_FIELD_TYPES or not value.strip():
            continue
        if field.type == "file":
            paths.append(value.strip())
        else:  # filelist: "path | ..." per line
            for line in value.splitlines():
                line = line.strip()
                if line:
                    paths.append(line.split("|")[0].strip())
    return paths


def ensure_assets(root: Path | None = None) -> list[Path]:
    """Create the real input assets every sample ``.sub`` references.

    The samples point at manuscript-bundle files under
    ``tests/manuscript_files/<venue>/`` (manuscript, supplement, cover letter,
    figures, ...), written as repo-relative paths. This collects every such path
    across all samples and builds each one from the shared ``SampleFiles``
    factory via :data:`_ASSET_BUILDERS`, resolving the paths against ``root``
    (the repository root by default). The builders are deterministic, so
    regenerating produces byte-identical files and a generated sample is actually
    submittable rather than pointing at empty placeholders.

    Only the files the samples actually reference are built -- a sample that
    points at ``figures/fig1.png`` gets that file, not every figure format. A
    referenced file whose stem has no builder raises ``ValueError`` so a new kind
    of input fails loudly with a pointer to :data:`_ASSET_BUILDERS`.
    """
    root = Path(root) if root is not None else REPO_ROOT
    wanted: set[str] = set()
    for scenario in _SCENARIOS:
        wanted.update(_referenced_paths(scenario.values, get_venue(scenario.venue)))
    written: list[Path] = []
    for rel in sorted(wanted):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        factory = _sample_files_factory(target.parent)
        if target.parent.name == "figures":
            written.append(factory.figure(name=target.stem, ext=target.suffix))
            continue
        builder = _ASSET_BUILDERS.get(target.stem)
        if builder is None:
            raise ValueError(f"no asset builder for {rel!r}; add {target.stem!r} to _ASSET_BUILDERS in tests/sample_subfiles.py")
        written.append(builder(factory, target.suffix))
    return written


if __name__ == "__main__":
    assets = ensure_assets()
    print(f"Ensured {len(assets)} input asset(s):")
    for p in assets:
        print(f"  {p}")
    paths = write_all()
    print(f"Regenerated {len(paths)} sample .sub file(s):")
    for p in paths:
        print(f"  {p}")
