#!/usr/bin/env python3
"""Regenerate ``paperpush/venues.schema.json`` from the pydantic models.

The schema is the editor-facing artifact that powers autocomplete and inline
validation for ``venues.json`` (wired up in ``.vscode/settings.json``). Its
single source of truth is :mod:`paperpush.schema_models`; run this after
changing those models:

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

from paperpush.schema_models import build_schema  # noqa: E402

SCHEMA_PATH = REPO_ROOT / "paperpush" / "venues.schema.json"


def render() -> str:
    """Return the schema as the exact text the committed file should contain."""
    return json.dumps(build_schema(), indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the schema file is out of date instead of " "rewriting it",
    )
    args = parser.parse_args()

    updated = render()
    current = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""

    if args.check:
        if current != updated:
            print(
                "venues.schema.json is out of date. " "Run: python scripts/gen_venues_schema.py",
                file=sys.stderr,
            )
            return 1
        return 0

    if current != updated:
        SCHEMA_PATH.write_text(updated, encoding="utf-8")
        print(f"Updated {SCHEMA_PATH.relative_to(REPO_ROOT)}")
    else:
        print("venues.schema.json already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
