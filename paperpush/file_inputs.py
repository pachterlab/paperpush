"""Files named on the command line for ``paperpush validate``.

``validate`` normally reads the uploads from a ``.sub`` file. It can instead be
given them directly (``--manuscript``, ``--title-page``, ``--cover-letter``,
``--figures``, ``--tables``, ``--supplement``), with or without a ``.sub``:

* with a ``.sub``, each flag replaces the path the ``.sub`` gives for that file;
* with ``--venue`` and no ``.sub``, the files are checked against that venue's
  author guidelines, and the checks that read the ``.sub``'s form fields are
  reported as not applicable;
* with neither, the files are checked against the placeholder generic venue
  (:func:`paperpush.database.generic_venue`): only the checks that apply to any
  manuscript run (links, references, hidden text, citation order, prose, ...).

Each flag names a *role* (see :data:`paperpush.requirements_check._ROLE_FIELD_IDS`)
and goes in the venue's upload field for that role. When the venue's form has
no such field (a cover letter for a portal without a cover-letter upload, say),
a field from :func:`paperpush.database.default_form` is added so the checks
still read the file, and a note says the portal has nowhere to upload it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Optional, Sequence, Union

from .database import GENERIC_SLUG, Field, Venue, default_form

# (key, role, takes several files). ``key`` is the CLI flag's dest and the MCP
# tool's argument name; ``role`` is the requirements_check role it fills.
FILE_ROLES: tuple[tuple[str, str, bool], ...] = (
    ("manuscript", "manuscript", False),
    ("title_page", "title_page", False),
    ("cover_letter", "cover_letter", False),
    ("figures", "figures", True),
    ("tables", "tables", True),
    ("supplement", "supplementary", True),
)

_FLAGS = {key: "--" + key.replace("_", "-") for key, _, _ in FILE_ROLES}


@dataclass
class FileInputs:
    """The venue and values to validate once the named files are put in place.

    ``venue`` may have upload fields added (see the module docstring); ``notes``
    are the messages to show the author about how the files were placed.
    """

    venue: Venue
    values: dict[str, str]
    notes: list[str] = field(default_factory=list)


Paths = Union[str, Sequence[str], None]


def given(files: Mapping[str, Paths]) -> bool:
    """Whether any file role in ``files`` names at least one path."""
    return any(_as_list(files.get(key)) for key, _, _ in FILE_ROLES)


def validate_command(venue: Optional[str], files: Mapping[str, Paths]) -> str:
    """The ``paperpush validate`` command that checks ``files`` (no .sub) for ``venue``."""
    import shlex

    parts = ["paperpush", "validate"]
    if venue:
        parts += ["--venue", venue]
    for key, _, _ in FILE_ROLES:
        paths = _as_list(files.get(key))
        if paths:
            parts += [_FLAGS[key], *paths]
    return shlex.join(parts)


def _as_list(value: Paths) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [v for v in value if v and str(v).strip()]


def _fallback_field(role: str) -> Field:
    """The :func:`default_form` upload field for ``role``."""
    from .requirements_check import upload_role_ids

    ids = upload_role_ids(role)
    for spec in default_form(GENERIC_SLUG)["fields"]:
        if spec["id"] in ids:
            return Field.from_dict(spec)
    raise KeyError(role)


def apply_files(venue: Venue, values: Mapping[str, str], files: Mapping[str, Paths], *, base: Optional[Path] = None) -> FileInputs:
    """Put the files named in ``files`` (keyed as in :data:`FILE_ROLES`) into ``values``.

    Relative paths are resolved against ``base`` (default: the working
    directory), so the values hold absolute paths whatever reads them next.
    Raises ``ValueError`` when several files are given for a role the venue
    takes as a single upload.
    """
    from .requirements_check import _fields_for

    base = Path.cwd() if base is None else base
    out = dict(values)
    notes: list[str] = []
    fields = list(venue.fields)
    for key, role, multiple in FILE_ROLES:
        paths = [str((base / Path(p).expanduser()).resolve()) for p in _as_list(files.get(key))]
        if not paths:
            continue
        if len(paths) > 1 and not multiple:
            raise ValueError(f"{_FLAGS[key]} takes one file (got {len(paths)})")
        targets = [f for f in _fields_for(replace(venue, fields=fields), role) if f.type in ("file", "filelist")]
        if targets:
            target = targets[0]
        else:
            target = _fallback_field(role)
            fields.append(target)
            if not venue.is_generic:
                names = ", ".join(Path(p).name for p in paths)
                notes.append(f"{venue.name or venue.slug}'s form has no {target.label.lower()} upload; {names} is checked, but the portal has nowhere to upload it")
        if target.type == "file" and len(paths) > 1:
            raise ValueError(f"{venue.name or venue.slug} takes a single {target.label.lower()} file, but {_FLAGS[key]} named {len(paths)}")
        if out.get(target.id, "").strip():
            notes.append(f"{_FLAGS[key]} replaces the .sub's {target.id}")
        out[target.id] = "\n".join(paths)
    if len(fields) != len(venue.fields):
        venue = replace(venue, fields=fields)
    return FileInputs(venue=venue, values=out, notes=notes)
