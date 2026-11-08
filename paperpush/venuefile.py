"""Venue files: one plain-text file describing a venue, merged into the databases.

``paperpush add-venue --template`` writes a venue file (``VENUE.txt``) for an
author (or an AI agent) to fill in, and ``paperpush add-venue VENUE.txt``
checks it and merges it into the three venue databases:

* ``[venue]`` -> ``venues.json`` (the master list: name, type, homepage, ...);
* ``[form]`` and its ``[field <id>]`` blocks -> ``submission_forms.json`` (the
  fields the submission portal asks for);
* ``[requirements]``, ``[requirements.<section>]`` and ``[article_type <name>]``
  blocks -> ``manuscript_requirements.json`` (what the author guidelines
  require of the manuscript).

The syntax is the ``.sub`` file's: ``key: value`` on one line, or ``key: |``
followed by lines indented two spaces. Lines starting with ``#`` are comments.
Lists are comma-separated on one line or one item per line in a block, booleans
are ``yes``/``no``, and an empty value means "not set". The literal ``null``
drops an inherited value (inheriting forms and requirements only).

The keys a block accepts, their types and their descriptions are read off the
dataclasses that define the databases (:class:`~paperpush.database.VenueInfo`,
:class:`~paperpush.database.SubmissionForm`, :class:`~paperpush.database.Field`,
and the section classes in :mod:`paperpush.requirements`), the same source the
JSON schemas are generated from, so a key added there appears here without
editing this module.

The parsed file is a :class:`VenueDoc`: plain dicts shaped exactly like the
database entries. The web page's "Add venue" editor works on the same dicts
(:func:`schema`, :func:`doc_to_json`, :func:`doc_from_json`).
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as _dt
import json
import re
import textwrap
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, Union, get_args, get_origin, get_type_hints

from . import venue_data
from .database import SECTION_TITLES, Field, SubmissionForm, VenueInfo
from .requirements import SECTION_TYPES, ManuscriptRequirements, StatementId, TitlePageItem

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
BLOCK_INDENT = "  "
WRAP = 88

# Keys of a requirements entry's top level that the [requirements] block holds
# (the sections and article types have blocks of their own).
_REQ_TOP_KEYS = ("source_urls", "retrieved", "inherits", "article_type_field", "notes")

# Form-level keys shown in [form]. ``removed_fields`` is a loader directive of
# an inheriting form, not a SubmissionForm attribute.
_FORM_EXTRA = {"removed_fields": ("list", "Ids of inherited fields to drop (inheriting forms only).")}

# Field keys written first, in this order, then the rest in dataclass order.
_FIELD_HEAD = ("label", "type", "required", "section", "help", "options")

# String keys whose values are usually long, edited in a text box.
LONG_TEXT = frozenset({"help", "description", "initial_submission_any_format"})


# --------------------------------------------------------------------------
# Key specs, read off the dataclasses


@dataclass(frozen=True)
class KeySpec:
    """How one key is written, parsed and edited."""

    name: str
    kind: str  # str | bool | int | float | choice | list | json | any
    description: str = ""
    choices: tuple[str, ...] = ()  # for choice, or the allowed items of a list

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "kind": self.kind, "description": self.description}
        if self.choices:
            out["choices"] = list(self.choices)
        return out


def _unwrap(tp: Any) -> tuple[Any, str]:
    description = ""
    if get_origin(tp) is Annotated:
        args = get_args(tp)
        tp = args[0]
        for extra in args[1:]:
            if getattr(extra, "description", None):
                description = extra.description
                break
    if get_origin(tp) is Union:
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            tp = args[0]
    return tp, description


def _spec(name: str, annotation: Any) -> KeySpec:
    tp, description = _unwrap(annotation)
    origin = get_origin(tp)
    if origin is Literal:
        return KeySpec(name, "choice", description, tuple(str(a) for a in get_args(tp) if a != ""))
    if tp is bool:
        return KeySpec(name, "bool", description)
    if tp is int:
        return KeySpec(name, "int", description)
    if tp is float:
        return KeySpec(name, "float", description)
    if tp is str:
        return KeySpec(name, "str", description)
    if origin is list:
        (item,) = get_args(tp) or (str,)
        item, _ = _unwrap(item)
        choices = tuple(str(a) for a in get_args(item)) if get_origin(item) is Literal else ()
        if item in (str,) or choices:
            return KeySpec(name, "list", description, choices)
    if tp is Any:
        return KeySpec(name, "any", description)
    return KeySpec(name, "json", description)


def _specs(cls: type, skip: tuple[str, ...] = ()) -> dict[str, KeySpec]:
    hints = get_type_hints(cls, include_extras=True)
    return {f.name: _spec(f.name, hints[f.name]) for f in dataclasses.fields(cls) if f.name not in skip}


def venue_specs() -> dict[str, KeySpec]:
    """Keys of the [venue] block: ``slug`` then the master-list keys."""
    out = {"slug": KeySpec("slug", "str", "The venue's id: lowercase letters, digits and underscores (e.g. 'nature_genetics'). Used as `paperpush subfile SLUG`.")}
    out.update(_specs(VenueInfo, skip=("slug",)))
    return out


def form_specs() -> dict[str, KeySpec]:
    """Keys of the [form] block (fields have blocks of their own)."""
    out = {"include": KeySpec("include", "bool", "no = a validate-only venue: no submission form is written, and the [field] blocks are ignored.")}
    out.update(_specs(SubmissionForm, skip=("fields",)))
    for name, (kind, description) in _FORM_EXTRA.items():
        out[name] = KeySpec(name, kind, description)
    return out


def field_specs() -> dict[str, KeySpec]:
    """Keys of a [field <id>] block, in the order they are written."""
    specs = _specs(Field, skip=("id",))
    specs["default"] = dataclasses.replace(specs["default"], description="Default value, written like the field's own value (yes/no for a boolean).")
    ordered = {k: specs[k] for k in _FIELD_HEAD}
    ordered.update({k: v for k, v in specs.items() if k not in ordered})
    return ordered


def requirements_specs() -> dict[str, KeySpec]:
    """Keys of the [requirements] block itself."""
    specs = _specs(ManuscriptRequirements, skip=("slug",))
    out = {"include": KeySpec("include", "bool", "no = skip the author guidelines: no manuscript_requirements.json entry is written.")}
    out.update({k: specs[k] for k in _REQ_TOP_KEYS})
    out["retrieved"] = dataclasses.replace(out["retrieved"], description="Date (YYYY-MM-DD) you read the guideline pages.")
    out["article_type_field"] = dataclasses.replace(
        out["article_type_field"],
        description="Id of the form field that selects the article type (usually article_type). Its options name the [article_type ...] blocks below.",
    )
    return out


def section_specs() -> dict[str, dict[str, KeySpec]]:
    """Keys of each [requirements.<section>] block."""
    return {name: _specs(typ) for name, typ in SECTION_TYPES.items()}


SECTION_DOCS = {name: (typ.__doc__ or "").strip().splitlines()[0] for name, typ in SECTION_TYPES.items()}


# --------------------------------------------------------------------------
# The starter form: the fields most venues' portals share


def _today() -> str:
    return _dt.date.today().isoformat()


# The field ids most submission forms share (counted over submission_forms.json
# when this was written: title, abstract and authors in every form; the
# manuscript in all but two; a cover letter, article type, figures, funding and
# comments to the editor in a third or more). Their ids are the ones
# common_fields.json already places, so a new venue's .sub lines up with the
# others. A venue file starts with these; delete the ones its portal does not ask for.
PRESET_FIELDS: list[dict[str, Any]] = [
    {"id": "article_type", "label": "Article type", "type": "choice", "required": True, "autofill": "classify", "help": "The kind of article being submitted."},
    {"id": "manuscript_file", "label": "Manuscript file", "type": "file", "required": True, "autofill": "filemap", "help": "Path to the manuscript.", "accept": [".pdf", ".docx"]},
    {"id": "figure_files", "label": "Figure files", "type": "filelist", "autofill": "filemap", "help": "Optional. One figure file per line as 'path | label' (label optional)."},
    {"id": "supplementary_files", "label": "Supplementary files", "type": "filelist", "autofill": "filemap", "help": "Optional. One supplementary file per line as 'path | label' (label optional)."},
    {"id": "cover_letter", "label": "Cover letter", "type": "file", "autofill": "filemap", "help": "Path to the cover letter.", "accept": [".pdf", ".docx"]},
    {"id": "title", "label": "Title", "type": "text", "required": True, "autofill": "extract", "help": "The full title of the manuscript."},
    {"id": "abstract", "label": "Abstract", "type": "textarea", "required": True, "autofill": "extract", "help": "The abstract as it appears in the manuscript."},
    {"id": "keywords", "label": "Keywords", "type": "text", "autofill": "extract", "help": "Comma-separated keywords."},
    {
        "id": "authors",
        "label": "Authors",
        "type": "authorlist",
        "required": True,
        "autofill": "extract",
        "help": "One author per line, in order. Format: Name | email | affiliation | ORCID | corresponding(yes/no). Mark exactly one author as the corresponding author.",
        "fields": ["name", "email", "affiliation?", "orcid?", "corresponding"],
    },
    {"id": "suggested_reviewers", "label": "Suggested reviewers", "type": "textarea", "autofill": "never", "help": "Optional. One reviewer per line as 'Name | email | institution'.", "fields": ["name", "email", "institution?"]},
    {"id": "excluded_reviewers", "label": "Excluded reviewers", "type": "textarea", "autofill": "never", "help": "Optional. One person per line as 'Name | email | institution'.", "fields": ["name", "email?", "institution?"]},
    {"id": "funding", "label": "Funding statement", "type": "textarea", "autofill": "extract", "help": "The funding statement as it appears in the manuscript."},
    {"id": "competing_interests", "label": "Competing interests", "type": "textarea", "autofill": "extract", "help": "The competing interests statement as it appears in the manuscript."},
    {"id": "data_availability", "label": "Data availability statement", "type": "textarea", "autofill": "extract", "help": "The data availability statement as it appears in the manuscript."},
    {"id": "comments_to_editor", "label": "Comments to the editor", "type": "textarea", "autofill": "never", "help": "Optional. Anything the editor should know."},
]


def preset_field(field_id: str) -> Optional[dict[str, Any]]:
    """A copy of the starter definition of ``field_id``, or None."""
    for f in PRESET_FIELDS:
        if f["id"] == field_id:
            return copy.deepcopy(f)
    return None


# --------------------------------------------------------------------------
# The document


@dataclass
class VenueDoc:
    """A venue file's contents, as the database entries it becomes.

    ``form`` is None for a validate-only venue (no submission_forms.json entry)
    and ``requirements`` is None when the file records no guidelines.
    """

    slug: str = ""
    venue: dict[str, Any] = dataclass_field(default_factory=dict)
    form: Optional[dict[str, Any]] = None
    requirements: Optional[dict[str, Any]] = None


def starter_doc(slug: str = "") -> VenueDoc:
    """A new venue: no metadata yet, the common form fields, empty guidelines."""
    return VenueDoc(
        slug=slug,
        venue={"venue_type": "journal", "anonymous": False},
        form={"fields": copy.deepcopy(PRESET_FIELDS)},
        requirements={"retrieved": _today()},
    )


def _raw_databases(data_dir: Optional[Path] = None) -> tuple[dict, dict, dict]:
    """The raw venues, forms and requirements databases (unresolved).

    A file still missing from ``data_dir`` is read from the active copy, the
    one :func:`merge` seeds it with, so :func:`check` sees what merge will.
    """
    paths = database_paths(data_dir)
    source = venue_data.active_source() if data_dir else None

    def read(path: Path) -> dict:
        if not path.is_file() and source is not None:
            path = source.path(path.name)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    return tuple(read(paths[k]) for k in ("venues", "forms", "requirements"))  # type: ignore[return-value]


def doc_for(slug: str, data_dir: Optional[Path] = None) -> VenueDoc:
    """The venue file of an existing venue, as its database entries stand.

    Entries are taken as written (an inheriting form keeps its ``inherits`` and
    partial overrides), so merging the file back changes nothing. Raises
    ``KeyError`` for an unknown slug.
    """
    info, forms, reqs = _raw_databases(data_dir)
    key = next((k for k in info if k.lower() == slug.lower()), None)
    if key is None:
        raise KeyError(slug)
    return VenueDoc(
        slug=key,
        venue=copy.deepcopy(info[key]),
        form=copy.deepcopy(forms[key]) if key in forms else None,
        requirements=copy.deepcopy(reqs[key]) if key in reqs else None,
    )


# --------------------------------------------------------------------------
# Values: text <-> Python


_TRUE = {"yes", "y", "true", "1"}
_FALSE = {"no", "n", "false", "0"}


class VenueFileError(ValueError):
    """A venue file that cannot be read; ``problems`` lists every issue."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


def _join_text(lines: list[str]) -> str:
    """Block lines as one string: wrapped lines rejoin, blank lines keep paragraphs."""
    paragraphs, current = [], []
    for line in lines:
        if line.strip():
            current.append(line.strip())
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n".join(paragraphs)


def _split_list(raw: str, block: bool) -> list[str]:
    if block:
        return [line.strip() for line in raw.splitlines() if line.strip()]
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_value(spec: KeySpec, raw: str, block: bool, *, field_type: str = "") -> Any:
    """Convert a key's raw text into its value; raises ValueError when it does not fit."""
    # Literals for what an override needs to say explicitly: drop the inherited
    # value, or replace it with an empty list or string.
    if raw.strip() == "null":
        return None
    if raw.strip() == "[]" and spec.kind in ("list", "any"):
        return []
    if raw.strip() == '""' and spec.kind in ("str", "any"):
        return ""
    if spec.kind == "list":
        items = _split_list(raw, block)
        bad = [i for i in items if spec.choices and i not in spec.choices]
        if bad:
            raise ValueError(f"{', '.join(repr(b) for b in bad)} not allowed; choose from: {', '.join(spec.choices)}")
        return items
    text = _join_text(raw.splitlines()) if block else raw.strip()
    if spec.kind == "bool":
        if text.lower() in _TRUE:
            return True
        if text.lower() in _FALSE:
            return False
        raise ValueError("expected yes or no")
    if spec.kind == "int":
        try:
            return int(text)
        except ValueError:
            raise ValueError("expected a whole number") from None
    if spec.kind == "float":
        try:
            value = float(text)
        except ValueError:
            raise ValueError("expected a number") from None
        return int(value) if value.is_integer() and "." not in text else value
    if spec.kind == "choice":
        if text not in spec.choices:
            raise ValueError(f"expected one of: {', '.join(spec.choices)}")
        return text
    if spec.kind == "json":
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"expected JSON ({exc.msg})") from None
    if spec.kind == "any":
        # A field's default: typed by the field it belongs to.
        if field_type == "boolean":
            return parse_value(KeySpec(spec.name, "bool"), raw, block)
        if field_type == "int":
            return parse_value(KeySpec(spec.name, "int"), raw, block)
        if field_type in ("multichoice", "filelist"):
            return _split_list(raw, block)
        return text
    return text


def _fmt_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        return str(value)
    return str(value)


def format_value(spec: KeySpec, value: Any, key: str) -> list[str]:
    """The line(s) that write ``key: value``."""
    if value is None:
        return [f"{key}: null"]
    if value == "":
        return [f'{key}: ""']
    if spec.kind == "list" or (spec.kind == "any" and isinstance(value, list)):
        items = [str(v) for v in value]
        inline = ", ".join(items)
        if not items:
            return [f"{key}: []"]
        if all("," not in i and "\n" not in i for i in items) and len(inline) <= 60 and spec.name != "notes":
            return [f"{key}: {inline}"]
        return [f"{key}: |"] + [BLOCK_INDENT + i for i in items]
    if spec.kind == "json" or (spec.kind == "any" and isinstance(value, (dict, list))):
        return [f"{key}: {json.dumps(value, ensure_ascii=False)}"]
    text = _fmt_scalar(value)
    if "\n" in text or len(text) > WRAP - len(key) - 2:
        lines = [f"{key}: |"]
        for para in text.split("\n"):
            lines += [BLOCK_INDENT + w for w in textwrap.wrap(para, WRAP - 2, break_long_words=False, break_on_hyphens=False)] or [""]
            lines.append("")
        while lines[-1] == "":
            lines.pop()
        return lines
    return [f"{key}: {text}".rstrip()]


# --------------------------------------------------------------------------
# Reading a venue file


@dataclass
class _Block:
    header: str
    line: int
    items: dict[str, tuple[str, bool, int]] = dataclass_field(default_factory=dict)


_HEADER_RE = re.compile(r"^\[\s*(.+?)\s*\]\s*$")


def _blocks(text: str, problems: list[str]) -> list[_Block]:
    blocks: list[_Block] = []
    current: Optional[_Block] = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        lineno = i + 1
        i += 1
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        header = _HEADER_RE.match(stripped) if not raw.startswith(" ") else None
        if header:
            current = _Block(header.group(1), lineno)
            blocks.append(current)
            continue
        key, sep, rest = raw.partition(":")
        if not sep or raw.startswith(" ") or not key.strip():
            problems.append(f"line {lineno}: expected 'key: value', a [block] header, or a comment; got {stripped!r}")
            continue
        key = key.strip()
        if current is None:
            problems.append(f"line {lineno}: {key!r} comes before the first [block] header")
            continue
        rest = rest.strip()
        if rest == "|":
            body: list[str] = []
            while i < len(lines):
                nxt = lines[i]
                if nxt.startswith(BLOCK_INDENT):
                    body.append(nxt[len(BLOCK_INDENT) :])
                elif nxt.strip() == "":
                    body.append("")
                else:
                    break
                i += 1
            value, block = "\n".join(body).strip("\n"), True
        else:
            value, block = rest, False
        if key in current.items:
            problems.append(f"line {lineno}: [{current.header}] sets {key!r} twice")
        current.items[key] = (value, block, lineno)
    return blocks


def _read_items(block: _Block, specs: dict[str, KeySpec], problems: list[str], *, field_type: str = "", allow_dotted: bool = False) -> dict[str, Any]:
    """The block's set keys as values; empty values are skipped."""
    out: dict[str, Any] = {}
    sections = section_specs() if allow_dotted else {}
    for key, (raw, is_block, lineno) in block.items.items():
        if not raw.strip():
            continue
        spec = specs.get(key)
        target, name = out, key
        if spec is None and allow_dotted and "." in key:
            section, _, name = key.partition(".")
            spec = sections.get(section, {}).get(name)
            if spec is not None:
                target = out.setdefault(section, {})
        if spec is None:
            if allow_dotted:
                hint = " (write `section.key`, e.g. `abstract.max_words`, with a key of a [requirements.SECTION] block, or `notes`)"
            else:
                hint = f" (keys: {', '.join(specs)})" if len(specs) < 40 else ""
            problems.append(f"line {lineno}: [{block.header}] has no key {key!r}{hint}")
            continue
        try:
            target[name] = parse_value(spec, raw, is_block, field_type=field_type)
        except ValueError as exc:
            problems.append(f"line {lineno}: {key}: {exc}")
    return out


def parse(text: str) -> VenueDoc:
    """Read a venue file. Raises :class:`VenueFileError` listing every problem."""
    problems: list[str] = []
    blocks = _blocks(text, problems)
    doc = VenueDoc()
    form: dict[str, Any] = {}
    form_included: Optional[bool] = None
    fields: list[dict[str, Any]] = []
    reqs: dict[str, Any] = {}
    reqs_included: Optional[bool] = None
    article_types: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    sections = section_specs()

    for block in blocks:
        head = block.header
        kind, _, name = head.partition(" ")
        name = name.strip()
        if head in seen and not head.startswith("field ") and not head.startswith("article_type "):
            problems.append(f"line {block.line}: [{head}] appears twice")
        seen.add(head)
        if head == "venue":
            values = _read_items(block, venue_specs(), problems)
            doc.slug = str(values.pop("slug", "") or "")
            doc.venue = values
        elif head == "form":
            values = _read_items(block, form_specs(), problems)
            form_included = values.pop("include", None)
            form.update(values)
        elif kind == "field":
            if not name:
                problems.append(f"line {block.line}: [field] needs the field's id, e.g. [field title]")
                continue
            if any(f["id"] == name for f in fields):
                problems.append(f"line {block.line}: field {name!r} is defined twice")
                continue
            type_raw = block.items.get("type", ("", False, 0))[0].strip()
            values = _read_items(block, field_specs(), problems, field_type=type_raw)
            fields.append({"id": name, **values})
        elif head == "requirements":
            values = _read_items(block, requirements_specs(), problems)
            reqs_included = values.pop("include", None)
            reqs.update(values)
        elif head.startswith("requirements."):
            section = head.split(".", 1)[1].strip()
            if section not in sections:
                problems.append(f"line {block.line}: unknown block [{head}]; the requirement sections are: {', '.join(sections)}")
                continue
            values = _read_items(block, sections[section], problems)
            if values:
                reqs[section] = values
        elif kind == "article_type":
            if not name:
                problems.append(f"line {block.line}: [article_type] needs the type's name, e.g. [article_type Research Article]")
                continue
            values = _read_items(block, {"notes": requirements_specs()["notes"]}, problems, allow_dotted=True)
            if name in article_types:
                problems.append(f"line {block.line}: article type {name!r} is defined twice")
            article_types[name] = values
        else:
            problems.append(f"line {block.line}: unknown block [{head}]; expected [venue], [form], [field ID], [requirements], [requirements.SECTION] or [article_type NAME]")

    if problems:
        line_of = lambda p: int(m.group(1)) if (m := re.match(r"line (\d+):", p)) else 0  # noqa: E731
        raise VenueFileError(sorted(problems, key=line_of))

    if form_included is not False:
        if fields:
            form["fields"] = fields
        doc.form = form if (form_included or fields or form.get("inherits")) else None
    if article_types:
        reqs["article_types"] = article_types
    if reqs_included is not False and any(k not in ("retrieved",) for k in reqs):
        doc.requirements = reqs
    return doc


def load(path: Path | str) -> VenueDoc:
    return parse(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Writing a venue file


def _comment(text: str, *, wrap: bool = True) -> list[str]:
    """``text`` as comment lines; a paragraph's continuation lines hang under its indent."""
    out = []
    for para in text.split("\n"):
        if not wrap or not para.strip():
            out.append(f"# {para}".rstrip())
            continue
        lead = " " * (len(para) - len(para.lstrip()))
        sub = lead + ("    " if lead else "")
        out += [f"# {line}" for line in textwrap.wrap(para.strip(), WRAP - 2, initial_indent=lead, subsequent_indent=sub, break_long_words=False, break_on_hyphens=False)]
    return out


def _kind_note(spec: KeySpec) -> str:
    if spec.kind == "bool":
        return "yes/no"
    if spec.kind == "choice":
        return "one of: " + " | ".join(spec.choices)
    if spec.kind == "list" and spec.choices:
        return "list of: " + ", ".join(spec.choices)
    if spec.kind in ("list", "int", "float"):
        return {"list": "list", "int": "whole number", "float": "number"}[spec.kind]
    if spec.kind == "json":
        return "JSON"
    return ""


def _key_lines(spec: KeySpec, value: Any, *, describe: bool, key: Optional[str] = None) -> list[str]:
    lines: list[str] = []
    if describe:
        note = _kind_note(spec)
        lines += _comment(spec.description + (f" ({note})" if note else ""))
    if value is None and key is None:
        lines.append(f"{spec.name}:")
    else:
        lines += format_value(spec, value, key or spec.name)
    return lines


def _rule(title: str, subtitle: str) -> list[str]:
    bar = "# " + "=" * (WRAP - 2)
    return ["", bar, f"# {title}", *(_comment(subtitle) if subtitle else []), bar]


_HEADER = """\
paperpush venue file  -*- mode: yaml -*-

Describes one venue (journal, preprint server, or conference) for paperpush.
Fill it in, then add it with:   paperpush add-venue {filename}

  [venue]                       -> venues.json: the list of venues
  [form], [field ID]            -> submission_forms.json: what the portal asks for
  [requirements],
  [requirements.SECTION],
  [article_type NAME]           -> manuscript_requirements.json: what the author
                                   guidelines require of the manuscript

Syntax (the same as a .sub file): `key: value` on one line, or `key: |` followed by
lines indented two spaces. Lists: comma-separated on one line, or one item per line
in a `|` block. Booleans: yes / no. Leave a value empty when the venue does not say;
empty keys are ignored. Lines starting with '#' are comments.

Record only what the venue's own pages state. To fill it in with an AI agent: "Fill
in {filename} for <venue> from its submission portal and author guidelines."
"""


def render(doc: VenueDoc, filename: str = "VENUE.txt") -> str:
    """The venue file for ``doc``, every key described in a comment."""
    out: list[str] = _comment(_HEADER.format(filename=filename).rstrip(), wrap=False)

    # [venue]
    out += _rule("[venue]  ->  venues.json", "General information about the venue.")
    out.append("[venue]")
    for name, spec in venue_specs().items():
        value = doc.slug if name == "slug" else doc.venue.get(name)
        out += [""] + _key_lines(spec, value, describe=True)

    # [form] and its fields
    out += _rule(
        "[form] and [field ID]  ->  submission_forms.json",
        "The submission portal's form, for `paperpush subfile` / `submit`. List the fields in the " "order the portal asks for them. Delete the [field] blocks the portal does not have, edit " "the rest, and copy one to add a field. Submitting also needs a runner script (see " "CONTRIBUTING.md); without one, the form still gives `subfile` and `validate` the venue's fields.",
    )
    out.append("[form]")
    form = doc.form
    for name, spec in form_specs().items():
        value = (form is not None) if name == "include" else (form or {}).get(name)
        if name in _FORM_EXTRA and not value:
            continue
        if name == "inherits" and not value:
            out += [""] + _comment(spec.description + " An inheriting form's [field] blocks only list the keys they change.")
            out.append("inherits:")
            continue
        out += [""] + _key_lines(spec, value, describe=True)

    out += [""] + _comment(_field_reference())
    fspecs = field_specs()
    for f in (form or {}).get("fields", []) if form is not None else []:
        out += ["", f"[field {f['id']}]"]
        ftype = f.get("type", "")
        for name, spec in fspecs.items():
            if name in f:
                out += _key_lines(spec, f[name], describe=False, key=name)
            elif name == "options" and ftype in ("choice", "multichoice") and "options_file" not in f:
                out.append("options: |")
        for name in f:
            if name != "id" and name not in fspecs:
                out.append(f"{name}: {json.dumps(f[name], ensure_ascii=False)}")

    # [requirements]
    reqs = doc.requirements
    out += _rule(
        "[requirements] and [requirements.SECTION]  ->  manuscript_requirements.json",
        "What the author guidelines require of the manuscript itself. `paperpush validate` measures " "the manuscript against these. Record only what the guidelines state, leaving the rest " "empty. Rules with no key of their own go in a section's `notes`, one sentence per line. " "These are the rules for a research article. Put another article type's differences in an " "[article_type NAME] block at the end.",
    )
    out.append("[requirements]")
    for name, spec in requirements_specs().items():
        value = (reqs is not None) if name == "include" else (reqs or {}).get(name)
        if name == "retrieved" and value is None and reqs is not None:
            value = _today()
        out += [""] + _key_lines(spec, value, describe=True)

    for section, specs in section_specs().items():
        values = (reqs or {}).get(section) or {}
        out += ["", "# " + "-" * (WRAP - 2), *_comment(SECTION_DOCS[section]), f"[requirements.{section}]"]
        for name, spec in specs.items():
            if name == "template_layout" and name not in values:
                continue  # measured with scripts/measure_template.py; see DEVELOPMENT.md
            out += _key_lines(spec, values.get(name), describe=True, key=name if name in values else None)

    # [article_type ...]
    out += _rule(
        "[article_type NAME]  ->  manuscript_requirements.json article_types",
        "One block per article type whose rules differ from the research article's. NAME is the " "type's option string exactly as in the form's article-type field (article_type_field). Set " "only what differs, as `section.key: value` (e.g. `manuscript.max_words_before_refs: 3000`), " "plus `notes`.",
    )
    sections = section_specs()
    types = (reqs or {}).get("article_types") or {}
    if not types:
        out += ["", *_comment("[article_type Brief Communication]"), *_comment("manuscript.max_words_before_refs: 1500"), *_comment("abstract.max_words: 100")]
    for name, rules in types.items():
        out += ["", f"[article_type {name}]"]
        for section, values in (rules or {}).items():
            if section == "notes":
                out += format_value(requirements_specs()["notes"], values, "notes")
                continue
            if values is None:
                out.append(f"{section}: null")
                continue
            for key, value in values.items():
                spec = sections.get(section, {}).get(key, KeySpec(key, "json"))
                out += format_value(spec, value, f"{section}.{key}")
    return "\n".join(out).rstrip() + "\n"


def _field_reference() -> str:
    lines = ["Field keys ([field ID] blocks; ID is the key used in the .sub file):"]
    for name, spec in field_specs().items():
        note = _kind_note(spec)
        desc = re.split(r"\.\s+(?=[A-Z])", spec.description)[0].rstrip(".")
        lines.append(f"  {name}: {desc}" + (f" ({note})" if note and spec.kind != "choice" else ""))
        if name == "type":
            lines[-1] += ": " + " | ".join(spec.choices)
        if name == "section":
            lines[-1] = "  section: .sub template section the field is listed under, for an id paperpush does not already place: " + " | ".join(SECTION_TITLES)
    lines.append("Reuse a common id where one fits (" + ", ".join(sorted(_common_ids())[:24]) + ", ...): it is placed in the .sub automatically.")
    return "\n".join(lines)


def _common_ids() -> set[str]:
    from .subfile import common_fields

    return set(common_fields())


def template(slug: str = "", *, from_slug: Optional[str] = None, filename: str = "VENUE.txt") -> str:
    """A venue file to fill in: blank (with the common form fields), or an existing venue's."""
    if from_slug:
        doc = doc_for(from_slug)
        if slug:
            doc.slug = slug
    else:
        doc = starter_doc(slug)
    return render(doc, filename)


# --------------------------------------------------------------------------
# Checking


@dataclass
class CheckResult:
    errors: list[str] = dataclass_field(default_factory=list)
    warnings: list[str] = dataclass_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check(doc: VenueDoc, *, force: bool = False, data_dir: Optional[Path] = None) -> CheckResult:
    """Every problem that would stop ``doc`` from loading once merged."""
    from .database import _info_problem
    from .requirements import _entry_problem
    from .requirements import _resolve_entry as _resolve_requirements

    res = CheckResult()
    info, forms, reqs_db = _raw_databases(data_dir)
    slug = doc.slug

    if not slug:
        res.errors.append("[venue] slug is empty: give the venue an id such as 'nature_genetics'.")
        return res
    if not SLUG_RE.match(slug):
        res.errors.append(f"[venue] slug {slug!r}: use lowercase letters, digits and underscores, starting with a letter.")
    if slug in info and not force:
        res.errors.append(f"{slug!r} is already a venue. Use --overwrite to replace its entries (`paperpush add-venue --template --from {slug}` writes its current file).")
    if not doc.venue.get("name"):
        res.warnings.append("[venue] name is empty; the slug is shown instead.")
    problem = _info_problem(slug, doc.venue)
    if problem:
        res.errors.append(f"[venue] {problem}")
    publisher = str(doc.venue.get("publisher") or "").strip().lower()
    known_publishers = reqs_db.get("$publishers") or {}
    if publisher and publisher not in known_publishers:
        res.errors.append(f"[venue] publisher {publisher!r} is not listed under $publishers in manuscript_requirements.json; use one of {', '.join(sorted(known_publishers))}, or leave it empty")

    if doc.form is None and doc.requirements is None:
        res.errors.append("The file has neither a submission form nor requirements, so paperpush could do nothing with this venue. Fill in [form] or [requirements].")

    if doc.form is not None:
        _check_form(slug, doc.form, forms, res)

    if doc.requirements is not None:
        entry = doc.requirements
        if not entry.get("source_urls"):
            res.warnings.append("[requirements] source_urls is empty: list the guideline pages the rules come from.")
        problem = _entry_problem(slug, entry)
        if problem:
            res.errors.append(f"[requirements] {problem}")
        else:
            candidate = {**reqs_db, slug: entry}
            try:
                ManuscriptRequirements.from_dict(slug, _resolve_requirements(slug, candidate))
            except (KeyError, TypeError, ValueError) as exc:
                res.errors.append(f"[requirements] {exc}")
        _check_article_types(slug, doc, forms, res)
        if doc.form is not None:
            res.warnings += _limit_mismatches(slug, doc, forms)

    if doc.form is not None:
        from .venues import SLUG_TO_MODULE, submission_base

        base = slug
        try:
            base = submission_base(slug)
        except Exception:  # pragma: no cover - only a lookup
            pass
        if base not in SLUG_TO_MODULE:
            res.warnings.append(f"No submission runner for {slug!r} yet, so `paperpush submit` will refuse it (subfile, autofill and validate work). " f"Add one at paperpush/venues/<portal>/{slug}.py; see CONTRIBUTING.md.")
    return res


def _check_form(slug: str, form: dict[str, Any], forms: dict[str, Any], res: CheckResult) -> None:
    from .database import FORM_KEYS, Venue, _resolve_entry

    fields = form.get("fields", [])
    inheriting = bool(form.get("inherits"))
    unknown = sorted(set(form) - FORM_KEYS)
    if unknown:
        res.errors.append(f"[form] unknown key(s): {', '.join(unknown)}")
    if inheriting and form["inherits"] not in forms:
        res.errors.append(f"[form] inherits {form['inherits']!r}, which has no submission form. Venues with one: {', '.join(sorted(forms))}.")
        return
    if not fields and not inheriting:
        res.errors.append("[form] has no [field] blocks. Add the portal's fields, or set `include: no` for a validate-only venue.")
        return

    common = _common_ids()
    base_ids = {f["id"] for f in _resolve_entry(form["inherits"], forms).get("fields", [])} if inheriting else set()
    for f in fields:
        where = f"[field {f['id']}]"
        if f["id"] in base_ids:
            continue  # a partial override of an inherited field
        for key in ("label", "type"):
            if not f.get(key):
                res.errors.append(f"{where} needs a {key}.")
        if f.get("type") in ("choice", "multichoice") and not f.get("options") and not f.get("options_file"):
            res.errors.append(f"{where} is a {f.get('type')} with no options: list them, one per line.")
        if not f.get("section") and f["id"] not in common:
            res.warnings.append(f"{where} has no section, so the .sub lists it under 'Additional information'. Set section to one of {', '.join(SECTION_TITLES)} to place it ({f['id']!r} is not an id paperpush already places).")
        if f.get("options_file"):
            assets = venue_data.active_source().assets
            from .database import ASSETS_DIR

            if not (assets / f["options_file"]).is_file() and not (ASSETS_DIR / f["options_file"]).is_file():
                res.errors.append(f"{where} options_file {f['options_file']!r} is not in paperpush/venues/_assets/.")
    if res.errors:
        return

    candidate = {**forms, slug: form}
    try:
        resolved = _resolve_entry(slug, candidate)
    except KeyError as exc:
        res.errors.append(f"[form] {exc.args[0]}")
        return
    ids = [f.get("id") for f in resolved.get("fields", [])]
    for f in resolved.get("fields", []):
        target = f.get("required_if")
        if target and target not in ids:
            res.errors.append(f"[field {f['id']}] required_if names {target!r}, which is not a field of this form.")
        try:
            Field.from_dict(f)
        except (KeyError, TypeError, ValueError) as exc:
            res.errors.append(f"[field {f.get('id')}] {exc}")
    try:
        Venue.from_dict(slug, {**resolved, "fields": []})
    except (KeyError, TypeError, ValueError) as exc:
        res.errors.append(f"[form] {exc}")


def _form_fields(slug: str, doc: VenueDoc, forms: dict[str, Any]) -> dict[str, dict[str, Any]]:
    from .database import _resolve_entry

    if doc.form is None:
        return {}
    try:
        return {f["id"]: f for f in _resolve_entry(slug, {**forms, slug: doc.form}).get("fields", [])}
    except KeyError:
        return {f["id"]: f for f in doc.form.get("fields", [])}


def _check_article_types(slug: str, doc: VenueDoc, forms: dict[str, Any], res: CheckResult) -> None:
    reqs = doc.requirements or {}
    types = reqs.get("article_types") or {}
    field_id = reqs.get("article_type_field")
    if types and not field_id:
        res.errors.append("[requirements] article_type_field is empty, but there are [article_type] blocks: name the form field that selects the type (usually article_type).")
        return
    if not field_id:
        return
    fields = _form_fields(slug, doc, forms)
    if doc.form is None:
        if field_id != "article_type" and types:
            res.warnings.append(f"A validate-only venue's form names its article-type field after article_type_field; {field_id!r} is fine but article_type is usual.")
        return
    field = fields.get(field_id)
    if field is None:
        res.errors.append(f"[requirements] article_type_field {field_id!r} is not a field of the form.")
        return
    options = field.get("options") or []
    missing = [t for t in types if options and t not in options]
    if missing:
        res.errors.append(f"[article_type] block(s) {', '.join(repr(m) for m in missing)} are not options of the {field_id!r} field ({', '.join(options)}). Use the option strings exactly.")


def _limit_mismatches(slug: str, doc: VenueDoc, forms: dict[str, Any]) -> list[str]:
    """Limits the form and the guidelines both record, with different values."""
    fields = _form_fields(slug, doc, forms)
    reqs = doc.requirements or {}
    pairs = [
        ("abstract", "word_count", "abstract", "max_words"),
        ("abstract", "character_count", "abstract", "max_characters"),
        ("title", "character_count", "title_page", "title_max_characters"),
        ("manuscript_file", "max_words_before_refs", "manuscript", "max_words_before_refs"),
        ("manuscript_file", "max_pages", "manuscript", "max_pages"),
        ("manuscript_file", "max_file_size_mb", "manuscript", "max_file_size_mb"),
    ]
    out = []
    for fid, fkey, section, rkey in pairs:
        a = (fields.get(fid) or {}).get(fkey)
        b = (reqs.get(section) or {}).get(rkey)
        if a is not None and b is not None and a != b:
            out.append(f"[field {fid}] {fkey} is {a} but [requirements.{section}] {rkey} is {b}. Fine if the portal and the guidelines really disagree (validate enforces the portal's); otherwise make them match.")
    return out


# --------------------------------------------------------------------------
# Merging into the databases


def database_paths(data_dir: Optional[Path] = None) -> dict[str, Path]:
    """The venues, forms and requirements files to read and write.

    A file missing from a local override directory falls back to the package's.
    """
    if data_dir:
        root = Path(data_dir)
    else:
        # A local override directory (PAPERPUSH_VENUE_DATA=DIR) is the author's
        # own copy, so it is the one to read and extend; a published (remote)
        # copy is never written, so otherwise the package's own files are.
        source = venue_data.active_source()
        root = source.root if source.kind == "override" else venue_data.PACKAGE_DIR
    pick = lambda name: root / name if (root / name).is_file() or data_dir else venue_data.PACKAGE_DIR / name  # noqa: E731
    return {
        "venues": pick(venue_data.VENUES_FILE),
        "forms": pick(venue_data.FORMS_FILE),
        "requirements": pick(venue_data.REQUIREMENTS_FILE),
    }


@dataclass
class MergeResult:
    written: list[str] = dataclass_field(default_factory=list)
    replaced: list[str] = dataclass_field(default_factory=list)
    kept: list[str] = dataclass_field(default_factory=list)


def _put(db: dict[str, Any], slug: str, entry: dict[str, Any]) -> bool:
    """Set ``db[slug]`` (in place when it exists, else at the end); True if it replaced one."""
    existed = slug in db
    db[slug] = entry
    return existed


def merge(doc: VenueDoc, *, data_dir: Optional[Path] = None, force: bool = False) -> MergeResult:
    """Write ``doc`` into the databases. Run :func:`check` first.

    A section the file leaves out (``include: no``) leaves the venue's existing
    entry in that database untouched. With ``data_dir``, the three files are
    read from and written to that directory, starting from the active copies
    when it lacks them (for ``PAPERPUSH_VENUE_DATA=<dir>``).
    """
    paths = database_paths(data_dir)
    if data_dir:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        source = venue_data.active_source()
        for name, path in zip((venue_data.VENUES_FILE, venue_data.FORMS_FILE, venue_data.REQUIREMENTS_FILE), paths.values()):
            if not path.exists():
                path.write_text(source.path(name).read_text(encoding="utf-8"), encoding="utf-8")
    info, forms, reqs = _raw_databases(data_dir)
    if doc.slug in info and not force:
        raise ValueError(f"{doc.slug!r} is already a venue (use force=True to replace it)")

    # Into a local override directory even for a file it lacks (read from the package's).
    source = venue_data.active_source()
    if not data_dir and source.kind == "override":
        paths = {key: source.root / path.name for key, path in paths.items()}

    result = MergeResult()
    plan = [("venues", info, doc.venue), ("forms", forms, doc.form), ("requirements", reqs, doc.requirements)]
    for key, db, entry in plan:
        path = paths[key]
        if entry is None:
            if doc.slug in db:
                result.kept.append(path.name)
            continue
        (result.replaced if _put(db, doc.slug, entry) else result.written).append(path.name)
        path.write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    from .database import reload

    reload()
    return result


# --------------------------------------------------------------------------
# For the web page's editor


def schema() -> dict[str, Any]:
    """Everything the "Add venue" page needs to build its editor."""
    return {
        "venue": [s.to_dict() for s in venue_specs().values()],
        "form": [s.to_dict() for s in form_specs().values()],
        "field": [s.to_dict() for s in field_specs().values()],
        "requirements": [s.to_dict() for s in requirements_specs().values()],
        "sections": {name: {"doc": SECTION_DOCS[name], "keys": [s.to_dict() for s in specs.values()]} for name, specs in section_specs().items()},
        "field_sections": SECTION_TITLES,
        "common_fields": sorted(_common_ids()),
        "presets": copy.deepcopy(PRESET_FIELDS),
        "long_text": sorted(LONG_TEXT),
        "title_page_items": list(get_args(TitlePageItem)),
        "statement_ids": list(get_args(StatementId)),
    }


def doc_to_json(doc: VenueDoc) -> dict[str, Any]:
    return {"slug": doc.slug, "venue": doc.venue, "form": doc.form, "requirements": doc.requirements}


def _prune(value: Any) -> Any:
    """Drop empty strings, lists and dicts (what the editor sends for unset keys)."""
    if isinstance(value, dict):
        out = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in ("", [], {})}
    if isinstance(value, list):
        return [_prune(v) for v in value if v not in ("", None)]
    return value


def doc_from_json(data: dict[str, Any]) -> VenueDoc:
    """A :class:`VenueDoc` from the editor's JSON, with unset keys dropped."""
    form = data.get("form")
    reqs = data.get("requirements")
    if form is not None:
        fields = [{"id": f.get("id", ""), **_prune({k: v for k, v in f.items() if k != "id"})} for f in form.get("fields") or []]
        form = {**_prune({k: v for k, v in form.items() if k != "fields"}), **({"fields": fields} if fields else {})}
    if reqs is not None:
        types = {str(k): _prune(v or {}) for k, v in (reqs.get("article_types") or {}).items() if str(k).strip()}
        reqs = _prune({k: v for k, v in reqs.items() if k != "article_types"})
        if types:
            reqs["article_types"] = types
    return VenueDoc(slug=str(data.get("slug") or "").strip(), venue=_prune(dict(data.get("venue") or {})), form=form, requirements=reqs)
