#!/usr/bin/env python3
"""Interactively add a new venue to ``paperpush/venues.json``.

The prompts are generated from the :class:`~paperpush.database.Field` and
:class:`~paperpush.database.Venue` dataclasses -- the same single source
that produces ``venues.schema.json`` -- so this tool always offers exactly the
keys those models define, with their descriptions, defaults, and (for ``type`` /
``autofill``) their allowed values.

Flow:

1. Enter the new venue's slug and its top-level keys (press Enter to accept a
   default / omit an optional key).
2. Choose standalone or inheriting (an inheriting venue derives from a base
   slug; its fields are partial overrides).
3. Add fields one at a time: each field asks for its ``type`` first, then walks
   every remaining key. Leave ``type`` empty to finish adding fields.
4. Review the assembled entry, which is validated against the schema before it
   is written into ``venues.json``.

Keys left empty are omitted entirely rather than written as empty values, so the
entry stays minimal (the loader fills defaults).

    python scripts/add_venue.py
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import (Annotated, Any, Literal, Union, get_args, get_origin,
                    get_type_hints)

# Import the package directly from the repo without requiring an install.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from paperpush.database import Field, Venue  # noqa: E402
from paperpush.schema_models import build_schema  # noqa: E402

DATABASE_PATH = REPO_ROOT / "paperpush" / "venues.json"

# Venue-level keys handled outside the generic top-level prompt loop: ``slug``
# is the database key (asked first), ``fields`` are gathered field-by-field, and
# ``inherits`` / ``removed_fields`` are driven by the standalone-vs-inheriting
# choice.
_VENUE_SPECIAL = {"slug", "fields", "inherits", "removed_fields"}

# The order a field's keys are asked in: type first (as the request specifies),
# then the identifying keys, then everything else in dataclass order.
_FIELD_KEY_ORDER_HEAD = ["type", "id", "label"]


class _Abort(Exception):
    """Raised to unwind to a clean exit (Ctrl-D / Ctrl-C / explicit quit)."""


@dataclasses.dataclass
class _Meta:
    """How to prompt for and parse one dataclass attribute."""

    name: str
    kind: str  # str | bool | int | float | list | choice | any | nested
    required: bool
    default: Any
    description: str
    choices: tuple[str, ...] = ()


def _unwrap_optional(tp: Any) -> tuple[Any, bool]:
    """Return (inner type, was_optional) for ``Optional[X]`` / ``X | None``."""
    if get_origin(tp) is Union:
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return tp, False


def _classify(name: str, annotated: Any, field: dataclasses.Field) -> _Meta:
    """Build prompt metadata for one dataclass attribute from its annotation."""
    description = ""
    base = annotated
    if get_origin(annotated) is Annotated:
        args = get_args(annotated)
        base = args[0]
        for extra in args[1:]:
            # The pydantic FieldInfo attached in database.py carries the text.
            if getattr(extra, "description", None):
                description = extra.description
                break

    base, _ = _unwrap_optional(base)

    has_default = field.default is not dataclasses.MISSING or field.default_factory is not dataclasses.MISSING  # type: ignore[misc]
    default = field.default if field.default is not dataclasses.MISSING else None

    origin = get_origin(base)
    if origin is Literal:
        kind, choices = "choice", tuple(str(a) for a in get_args(base))
    elif base is bool:
        kind, choices = "bool", ()
    elif base is int:
        kind, choices = "int", ()
    elif base is float:
        kind, choices = "float", ()
    elif origin in (list, list):
        kind, choices = "list", ()
    elif base is Any:
        kind, choices = "any", ()
    else:
        kind, choices = "str", ()

    return _Meta(
        name=name,
        kind=kind,
        required=not has_default,
        default=default,
        description=description,
        choices=choices,
    )


def _describe(cls: type) -> dict[str, _Meta]:
    """Map each attribute of a dataclass to its prompt metadata."""
    hints = get_type_hints(cls, include_extras=True)
    return {f.name: _classify(f.name, hints[f.name], f) for f in dataclasses.fields(cls)}


# --- input -----------------------------------------------------------------


def _read(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError as exc:  # Ctrl-D
        raise _Abort() from exc


def _parse(raw: str, meta: _Meta) -> Any:
    """Convert a non-empty raw string into the value for ``meta``'s kind."""
    raw = raw.strip()
    if meta.kind == "bool":
        if raw.lower() in {"y", "yes", "true", "1"}:
            return True
        if raw.lower() in {"n", "no", "false", "0"}:
            return False
        raise ValueError("enter yes or no")
    if meta.kind == "int":
        return int(raw)
    if meta.kind == "float":
        return float(raw)
    if meta.kind == "list":
        return [part.strip() for part in raw.split(",") if part.strip()]
    if meta.kind == "choice":
        if raw not in meta.choices:
            raise ValueError(f"choose one of: {', '.join(meta.choices)}")
        return raw
    if meta.kind == "any":
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _ask(meta: _Meta, *, required: bool) -> Any:
    """Prompt for one value; return ``_OMIT`` when left empty and optional."""
    bits = [meta.name]
    if meta.kind not in ("str", "any"):
        bits.append(f"({meta.kind})")
    if meta.choices:
        bits.append("[" + " | ".join(meta.choices) + "]")
    label = " ".join(bits)

    hint = "required" if required else "Enter to skip"
    if not required and meta.default not in (None, "", [], dataclasses.MISSING):
        hint = f"Enter for default {meta.default!r}"

    if meta.description:
        print(f"  {meta.description}")

    while True:
        raw = _read(f"{label} ({hint}): ").strip()
        if not raw:
            if required:
                print("    -> this key is required.")
                continue
            return _OMIT
        try:
            return _parse(raw, meta)
        except ValueError as err:
            print(f"    -> {err}")


_OMIT = object()


# --- assembly --------------------------------------------------------------


def _collect_venue_level(meta: dict[str, _Meta]) -> dict[str, Any]:
    """Prompt for the top-level venue keys (everything but slug/fields/
    inheritance)."""
    entry: dict[str, Any] = {}
    print("\n== Venue details (press Enter to skip an optional key) ==")
    for name, m in meta.items():
        if name in _VENUE_SPECIAL:
            continue
        value = _ask(m, required=m.required)
        if value is not _OMIT:
            entry[name] = value
    return entry


def _field_key_order(meta: dict[str, _Meta]) -> list[str]:
    rest = [n for n in meta if n not in _FIELD_KEY_ORDER_HEAD]
    return _FIELD_KEY_ORDER_HEAD + rest


def _collect_fields(meta: dict[str, _Meta], *, inheriting: bool) -> list[dict[str, Any]]:
    """Add fields one at a time until ``type`` is left empty.

    For an inheriting venue each field is a partial override, so only ``id`` is
    required; for a standalone venue ``id``, ``label``, and ``type`` are all
    required.
    """
    order = _field_key_order(meta)
    fields: list[dict[str, Any]] = []
    while True:
        print(f"\n-- Field #{len(fields) + 1} (leave type empty to finish) --")
        type_meta = meta["type"]
        type_val = _ask(type_meta, required=False)
        if type_val is _OMIT:
            break

        field: dict[str, Any] = {"type": type_val}
        for name in order:
            if name == "type":
                continue
            m = meta[name]
            if inheriting:
                required = name == "id"
            else:
                required = m.required
            value = _ask(m, required=required)
            if value is not _OMIT:
                field[name] = value
        fields.append(field)
    return fields


def _build_entry() -> tuple[str, dict[str, Any]]:
    field_meta = _describe(Field)
    venue_meta = _describe(Venue)

    print("== New venue ==")
    slug = ""
    while not slug:
        slug = _read("slug (the database key, e.g. 'nature_genetics'): ").strip()
        if not slug:
            print("    -> a slug is required.")

    # Ask about inheritance up front: it decides whether fields are full
    # definitions or partial overrides, and lets the rest of the prompts be
    # clicked through. An inheriting venue's base, and any inherited fields it
    # drops, are gathered here too.
    entry: dict[str, Any] = {}
    inheriting = _read("\nDoes this venue inherit another's submission format " "(e.g. an AAAS sibling of 'science')? [y/N]: ").strip().lower() in {"y", "yes"}
    if inheriting:
        entry["inherits"] = _ask(venue_meta["inherits"], required=True)
        # removed_fields is a loader directive (consumed by _merge_fields), not a
        # Venue dataclass attribute, so it has no introspected metadata.
        removed_meta = _Meta(
            name="removed_fields",
            kind="list",
            required=False,
            default=None,
            description="Comma-separated ids of inherited fields to drop.",
        )
        removed = _ask(removed_meta, required=False)
        if removed is not _OMIT:
            entry["removed_fields"] = removed

    entry.update(_collect_venue_level(venue_meta))

    fields = _collect_fields(field_meta, inheriting=inheriting)
    if fields:
        entry["fields"] = fields

    return slug, entry


# --- persistence -----------------------------------------------------------


def _load_db() -> dict[str, Any]:
    return json.loads(DATABASE_PATH.read_text(encoding="utf-8"))


def _validate(db: dict[str, Any]) -> list[str]:
    """Validate a candidate database against the generated schema."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        print(
            "  (jsonschema not installed; skipping validation -- " '`pip install -e ".[dev]"` to enable it)',
            file=sys.stderr,
        )
        return []
    validator = Draft202012Validator(build_schema())
    return [f"{'/'.join(map(str, e.path))}: {e.message}" for e in sorted(validator.iter_errors(db), key=lambda e: list(e.path))]


def _scaffold_module(slug: str) -> None:
    """Offer to write a starter Venue module for ``slug`` from the template.

    Copies ``paperpush/venues/template.py`` into a portal subpackage, renaming
    the class/error/slug so the author starts from a working skeleton and only has
    to fill in ``login`` and ``run``. Writes a brand-new file only (never edits an
    existing module or ``__init__.py``). Naming the file ``<slug>.py`` under the
    portal is the whole registration -- ``SLUG_TO_MODULE`` is discovered from the
    layout, so there is no list to edit.
    """
    venues_dir = REPO_ROOT / "paperpush" / "venues"
    template = venues_dir / "template.py"
    if not template.exists():
        return
    if _read("\nScaffold a starter Venue module from the template? [y/N]: ").strip().lower() not in {"y", "yes"}:
        return

    portal = _read(f"  Portal subpackage name [{slug}]: ").strip() or slug
    dest_dir = venues_dir / portal
    dest = dest_dir / f"{slug}.py"
    if dest.exists():
        print(f"  {dest.relative_to(REPO_ROOT)} already exists; leaving it untouched.", file=sys.stderr)
        return

    camel = "".join(part.capitalize() for part in slug.replace("-", "_").split("_"))
    text = template.read_text(encoding="utf-8")
    text = text.replace("TemplateLoginError", f"{camel}LoginError")
    text = text.replace("TemplateVenue", f"{camel}Venue")
    text = text.replace('slug = "template"', f'slug = "{slug}"')

    dest_dir.mkdir(parents=True, exist_ok=True)
    init = dest_dir / "__init__.py"
    if not init.exists():
        init.write_text("", encoding="utf-8")
    dest.write_text(text, encoding="utf-8")
    print(f"  Wrote starter module {dest.relative_to(REPO_ROOT)} (fill in `login` and `run`).")
    print("  It registers itself: SLUG_TO_MODULE is discovered from the file's " "location and its module-level VENUE -- nothing else to edit.")


def main() -> int:
    try:
        slug, entry = _build_entry()
    except (_Abort, KeyboardInterrupt):
        print("\nAborted; nothing written.", file=sys.stderr)
        return 1

    db = _load_db()
    if slug in db:
        overwrite = _read(f"\n{slug!r} already exists. Overwrite it? [y/N]: ").strip().lower() in {"y", "yes"}
        if not overwrite:
            print("Aborted; nothing written.", file=sys.stderr)
            return 1

    candidate = dict(db)
    candidate[slug] = entry

    print("\n== Entry preview ==")
    print(json.dumps({slug: entry}, indent=2, ensure_ascii=False))

    errors = _validate(candidate)
    if errors:
        print("\nThis entry does not satisfy the schema:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print("Nothing written. Re-run and adjust the flagged keys.", file=sys.stderr)
        return 1

    if _read("\nWrite this entry to venues.json? [y/N]: ").strip().lower() not in {
        "y",
        "yes",
    }:
        print("Aborted; nothing written.", file=sys.stderr)
        return 1

    DATABASE_PATH.write_text(json.dumps(candidate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        shown = DATABASE_PATH.relative_to(REPO_ROOT)
    except ValueError:
        shown = DATABASE_PATH
    print(f"Added {slug!r} to {shown}.")

    _scaffold_module(slug)

    print("\nNext: refresh the README table with " "`python scripts/gen_readme_venues.py`. If this venue needs portal " "automation and you skipped the scaffold above, copy " "paperpush/venues/template.py to paperpush/venues/<portal>/<slug>.py " "-- naming it after the slug is all the registration it needs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
