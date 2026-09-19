#!/usr/bin/env python3
"""Assemble the venue data that is published separately from PyPI releases.

Installed copies of paperpush fetch this set at runtime (see
:mod:`paperpush.venue_data`), so a fix to ``venues.json`` reaches users without
a release. The ``venue-data`` workflow runs this on every push to ``main`` that
touches the data and commits the output to the ``venue-data`` branch:

    python scripts/build_venue_data.py OUT_DIR

``OUT_DIR`` receives ``venues.json``, ``manuscript_requirements.json``, their
two JSON schemas, ``_assets/*`` and a ``manifest.json`` recording the data
format, the source commit and the sha256 of every file. Nothing is written
unless the data validates against the schemas and loads with the package's own
loader -- the same check a client runs before accepting a download.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # nosec B404 -- fixed git invocations only
import sys
from pathlib import Path

# Import the package directly from the repo without requiring an install.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from jsonschema import Draft202012Validator  # noqa: E402

from paperpush import venue_data  # noqa: E402
from paperpush.database import check_venue_data  # noqa: E402

PACKAGE_DIR = REPO_ROOT / "paperpush"
ASSETS_DIR = PACKAGE_DIR / "venues" / venue_data.ASSETS_SUBDIR

# (data file, the schema it is validated against).
SCHEMA_PAIRS = (
    (venue_data.VENUES_FILE, venue_data.VENUES_SCHEMA_FILE),
    (venue_data.REQUIREMENTS_FILE, venue_data.REQUIREMENTS_SCHEMA_FILE),
)


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.strip()  # nosec B603 B607
    except (OSError, subprocess.CalledProcessError):
        return ""


def validate(root: Path) -> list[str]:
    """Schema errors for the data in ``root``, as ``file: path: message`` lines."""
    errors = []
    for data_name, schema_name in SCHEMA_PAIRS:
        schema = json.loads((root / schema_name).read_text(encoding="utf-8"))
        data = json.loads((root / data_name).read_text(encoding="utf-8"))
        for err in Draft202012Validator(schema).iter_errors(data):
            where = "/".join(str(p) for p in err.absolute_path) or "(root)"
            errors.append(f"{data_name}: {where}: {err.message}")
    return errors


def build(out: Path) -> dict:
    """Copy the data set into ``out`` and write its manifest; return the manifest."""
    if out.exists():
        shutil.rmtree(out)
    (out / venue_data.ASSETS_SUBDIR).mkdir(parents=True)

    for name in venue_data.DATA_FILES:
        shutil.copyfile(PACKAGE_DIR / name, out / name)
    for asset in sorted(ASSETS_DIR.iterdir()):
        if asset.is_file() and not asset.name.startswith("."):
            shutil.copyfile(asset, out / venue_data.ASSETS_SUBDIR / asset.name)

    errors = validate(out)
    if errors:
        raise SystemExit("venue data does not match its schema:\n  " + "\n  ".join(errors))
    check_venue_data(out)

    files = {path.relative_to(out).as_posix(): venue_data.file_sha256(path) for path in sorted(out.rglob("*")) if path.is_file()}
    manifest = {
        "data_format": venue_data.DATA_FORMAT,
        "commit": _git("rev-parse", "HEAD"),
        # The commit time, not the build time, so rebuilding the same commit
        # yields the same manifest.
        "published_at": _git("log", "-1", "--format=%cI"),
        "files": files,
    }
    (out / venue_data.MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", type=Path, help="directory to write the data set to (replaced if it exists)")
    args = parser.parse_args()
    manifest = build(args.out)
    print(f"Built venue data (format {manifest['data_format']}, {len(manifest['files'])} files) in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
