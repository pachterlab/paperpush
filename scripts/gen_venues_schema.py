#!/usr/bin/env python3
"""Regenerate the JSON schemas from the pydantic models.

Two schemas are maintained: ``paperpush/venues.schema.json`` (for
``venues.json``, the submission-form database) and
``paperpush/manuscript_requirements.schema.json`` (for
``manuscript_requirements.json``, the author-guideline rules ``validate``
measures the uploads against). Both are editor-facing artifacts that power
autocomplete and inline validation (wired up in ``.vscode/settings.json``).
Their single source of truth is :mod:`paperpush.schema_models`; run this after
changing those models or the dataclasses they read:

    python scripts/gen_venues_schema.py        # rewrite the schema in place
    python scripts/gen_venues_schema.py --check # exit 1 if out of date

The ``--check`` form is suitable for CI or a pre-commit hook.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Import the package directly from the repo without requiring an install.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from paperpush.schema_models import build_requirements_schema, build_schema  # noqa: E402

SCHEMA_PATH = REPO_ROOT / "paperpush" / "venues.schema.json"
REQUIREMENTS_SCHEMA_PATH = REPO_ROOT / "paperpush" / "manuscript_requirements.schema.json"

# (path, builder) for every generated schema.
SCHEMAS = (
    (SCHEMA_PATH, build_schema),
    (REQUIREMENTS_SCHEMA_PATH, build_requirements_schema),
)


def render(build=build_schema) -> str:
    """Return a schema as the exact text its committed file should contain."""
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the schema file is out of date instead of " "rewriting it",
    )
    args = parser.parse_args()

    status = 0
    for path, build in SCHEMAS:
        updated = render(build)
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        name = path.name
        if args.check:
            if current != updated:
                print(f"{name} is out of date. Run: python scripts/gen_venues_schema.py", file=sys.stderr)
                status = 1
            continue
        if current != updated:
            path.write_text(updated, encoding="utf-8")
            print(f"Updated {path.relative_to(REPO_ROOT)}")
        else:
            print(f"{name} already up to date")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
