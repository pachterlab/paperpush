"""Build ``venues.schema.json`` from the runtime dataclasses.

The :class:`~paperpush.database.Field` and
:class:`~paperpush.database.Venue` dataclasses are the single source of
truth for the venue database's shape. Their attribute types and the
``Annotated[..., pydantic.Field(description=...)]`` descriptions on them are read
here with :class:`pydantic.TypeAdapter` to generate the JSON Schema that powers
editor autocomplete and validation (wired up in ``.vscode/settings.json``).

This module only *reshapes* what the dataclasses already declare into the parts
JSON Schema needs that a flat dataclass cannot express on its own:

* the database is an object keyed by venue slug (the slug is the key, not a
  value inside an entry, so it is dropped from the venue schema);
* a venue's fields are either full definitions (standalone venue) or partial
  overrides (inheriting venue), selected with an ``if/then/else`` on
  ``inherits``;
* an inheriting venue may carry ``removed_fields`` (an instruction consumed by
  the loader, not stored on the resolved :class:`Venue`).

Run ``python scripts/gen_venues_schema.py`` to regenerate the committed schema;
CI drift-checks it with ``--check``.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import TypeAdapter

from paperpush.database import Field, Venue

_SCHEMA_ID = "https://paperpush.org/schemas/venues.schema.json"
_TITLE = "paperpush venue database"
_DESCRIPTION = "Schema for venues.json. The top-level object maps a venue slug to a " "venue definition. Generated from the Field/Venue dataclasses in " "paperpush/database.py; run scripts/gen_venues_schema.py to regenerate."


def _clean(node: Any, in_named_map: bool = False) -> Any:
    """Drop pydantic's ``title`` annotations and ``default`` keywords so the
    committed schema stays minimal and stable.

    ``in_named_map`` marks the children of a ``properties``/``$defs`` object,
    whose keys are arbitrary names (e.g. a field literally named ``default``)
    and must be preserved -- only schema *keywords* are stripped.
    """
    if isinstance(node, dict):
        if in_named_map:
            return {key: _clean(value) for key, value in node.items()}
        out = {}
        for key, value in node.items():
            if key in ("title", "default"):
                continue
            out[key] = _clean(value, in_named_map=key in ("properties", "$defs"))
        return out
    if isinstance(node, list):
        return [_clean(v) for v in node]
    return node


def _nullable(prop: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a property schema that also permits ``null``.

    In an inheriting venue an override may set any inherited key to ``null`` to
    delete it (see :func:`paperpush.database._merge_fields`), so every
    overridable property must accept null. Already-nullable properties (the
    ``Optional[...]`` ones) are returned unchanged; an "any" schema already
    admits null.
    """
    p = copy.deepcopy(prop)
    if "anyOf" in p:
        if {"type": "null"} not in p["anyOf"]:
            p["anyOf"].append({"type": "null"})
        return p
    if "type" in p:
        description = p.pop("description", None)
        wrapped: dict[str, Any] = {"anyOf": [p, {"type": "null"}]}
        if description is not None:
            wrapped["description"] = description
        return wrapped
    return p


def _strip_model_description(schema: dict[str, Any]) -> dict[str, Any]:
    """Drop the model-level ``description`` pydantic derives from a class docstring.

    Whether a dataclass docstring is propagated to the top-level ``description``
    of its JSON Schema varies by pydantic version (older releases omit it, newer
    ones emit it), which would make the committed schema drift purely on the
    pydantic version CI happens to install. Those class docstrings document the
    Python implementation, not the ``venues.json`` shape, so we drop them to keep
    the generated schema deterministic. The per-attribute ``Field(description=...)``
    descriptions -- the useful editor docs -- live under ``properties`` and are
    left untouched.
    """
    schema.pop("description", None)
    return schema


def _field_schema() -> dict[str, Any]:
    """Full field definition (standalone venues): id, label, type required."""
    schema = _strip_model_description(_clean(TypeAdapter(Field).json_schema()))
    schema["additionalProperties"] = False
    return schema


def _field_override_schema(field_schema: dict[str, Any]) -> dict[str, Any]:
    """Partial field override (inheriting venues): only ``id`` required, every
    other key optional and nullable (null deletes the inherited key)."""
    schema = copy.deepcopy(field_schema)
    schema["required"] = ["id"]
    schema["properties"] = {name: prop if name == "id" else _nullable(prop) for name, prop in schema["properties"].items()}
    return schema


def _venue_common_schema() -> dict[str, Any]:
    """Venue-level keys, shared by standalone and inheriting venues.

    Derived from the :class:`Venue` dataclass, minus ``slug`` (the database
    key, not an entry value) and plus ``removed_fields`` (a loader directive that
    is not stored on the resolved venue). ``fields`` is pointed at the override
    item schema by default; the standalone case tightens it below.
    """
    schema = _strip_model_description(_clean(TypeAdapter(Venue).json_schema()))
    # TypeAdapter inlines the nested Field model under $defs and points
    # `fields.items` at it; we supply our own field/fieldOverride defs instead.
    schema.pop("$defs", None)

    properties = schema["properties"]
    properties.pop("slug", None)
    if "required" in schema:
        schema["required"] = [r for r in schema["required"] if r != "slug"]
        if not schema["required"]:
            schema.pop("required")

    properties["fields"] = {
        "type": "array",
        "items": {"$ref": "#/$defs/fieldOverride"},
    }
    properties["removed_fields"] = {
        "type": "array",
        "items": {"type": "string"},
        "description": "Inherited field ids to drop. Only meaningful on an " "inheriting venue.",
    }
    schema["additionalProperties"] = False
    return schema


def build_schema() -> dict[str, Any]:
    """Assemble the full JSON Schema (draft 2020-12) for ``venues.json``."""
    field = _field_schema()
    field_override = _field_override_schema(field)
    venue_common = _venue_common_schema()

    # TypeAdapter(Field) inlines nested models (the ConditionalLimit used by the
    # `*_by` limits) under the field schema's own ``$defs`` and refers to them
    # with a root pointer (``#/$defs/ConditionalLimit``). Hoist those defs to the
    # top level so the references resolve, and drop the per-field duplicates (the
    # override is a deep copy of the field, so it carries the same ones).
    nested_defs = field.pop("$defs", {})
    for nested in nested_defs.values():
        _strip_model_description(nested)
    field_override.pop("$defs", None)

    venue = {
        "allOf": [
            {"$ref": "#/$defs/venueCommon"},
            {
                "if": {
                    "required": ["inherits"],
                    "properties": {"inherits": {"type": "string", "minLength": 1}},
                },
                "then": {
                    "description": "Inheriting venue: field entries are partial " "overrides; removed_fields is allowed.",
                    "properties": {"fields": {"items": {"$ref": "#/$defs/fieldOverride"}}},
                },
                "else": {
                    "description": "Standalone venue: every field must be a full " "definition and removed_fields is not applicable.",
                    "properties": {
                        "fields": {"items": {"$ref": "#/$defs/field"}},
                        "removed_fields": False,
                    },
                },
            },
        ]
    }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": _SCHEMA_ID,
        "title": _TITLE,
        "description": _DESCRIPTION,
        "type": "object",
        "propertyNames": {
            "description": "Venue slug. The canonical identifier is the " "lowercase slug; case variants collapse to one venue at load time.",
            "pattern": "^[A-Za-z0-9_]+$",
        },
        "additionalProperties": {"$ref": "#/$defs/venue"},
        "$defs": {
            "fieldOverride": field_override,
            "field": field,
            "venueCommon": venue_common,
            "venue": venue,
            **nested_defs,
        },
    }
