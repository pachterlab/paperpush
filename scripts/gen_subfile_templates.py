#!/usr/bin/env python3
"""Regenerate the checked-in ``.sub`` templates from venues.json.

For every supported venue, ``paperpush/subfile_templates/<slug>.sub``
holds an empty submission template -- the same file ``paperpush subfile
<slug> --dont-fill-defaults`` would produce -- so contributors can see each
venue's fields at a glance without running the tool. These are generated
artifacts; edit ``paperpush/venues.json`` (and the field schema) instead
and re-run this script:

    python scripts/gen_subfile_templates.py         # rewrite templates in place
    python scripts/gen_subfile_templates.py --check  # exit 1 if any is out of date

The ``--check`` form is suitable for CI or a pre-commit hook. Templates are
"empty" (fields present but values left blank, ``fill_defaults=False``) so the
committed file is a stable, defaults-free starting point.

Every supported venue is regenerated on each run rather than only the ones
whose entry changed: venues.json inheritance means one base venue's edit can
change several inheriting venues' fields, so regenerating the whole set is both
simpler and always correct. Stale templates (for a venue removed from the
database) are deleted.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Import the package directly from the repo without requiring an install.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from paperpush.database import list_venues  # noqa: E402
from paperpush.subfile import default_filename, render_template  # noqa: E402

TEMPLATES_DIR = REPO_ROOT / "paperpush" / "subfile_templates"


def _desired() -> dict[str, str]:
    """Map each supported venue's template filename to its rendered text."""
    return {default_filename(j): render_template(j, fill_defaults=False) for j in list_venues()}


def _existing() -> dict[str, str]:
    """Map each currently checked-in ``*.sub`` filename to its text."""
    if not TEMPLATES_DIR.is_dir():
        return {}
    return {p.name: p.read_text(encoding="utf-8") for p in TEMPLATES_DIR.glob("*.sub")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if any template is out of date instead of rewriting it",
    )
    args = parser.parse_args()

    desired = _desired()
    existing = _existing()

    stale = sorted(set(existing) - set(desired))
    changed = sorted(name for name, text in desired.items() if existing.get(name) != text)

    if args.check:
        if not stale and not changed:
            return 0
        for name in changed:
            print(f"{name} is out of date.", file=sys.stderr)
        for name in stale:
            print(f"{name} is stale (venue no longer in the database).", file=sys.stderr)
        print(
            "Run: python scripts/gen_subfile_templates.py",
            file=sys.stderr,
        )
        return 1

    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    for name in changed:
        (TEMPLATES_DIR / name).write_text(desired[name], encoding="utf-8")
        print(f"Updated {name}")
    for name in stale:
        (TEMPLATES_DIR / name).unlink()
        print(f"Removed {name}")
    if not changed and not stale:
        print(f"All {len(desired)} subfile templates already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
