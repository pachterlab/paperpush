"""Read and write ``.sub`` submission files.

A ``.sub`` file is a human-editable, line-based description of one manuscript
submission to one venue. It is generated from a venue's field list and is
meant to be opened in a text editor and filled in by the author. The format is
deliberately simple so that ``submit`` can round-trip it:

    # comment lines start with '#'
    @venue: biorxiv          # the target venue slug

    key: value                 # single-line field
    key: |                     # block field; indented lines that follow
      first line               # are the value, with two-space indentation
      second line

Block syntax (``key: |``) is used for any multi-line value (abstracts, author
lists, file lists). Everything else is a single ``key: value`` line.

Fields are grouped into the same sections, in the same order, for every venue
(manuscript type, files, details, authors, reviewers, declarations, additional;
see :func:`ordered_fields`), so ``.sub`` files for different venues line up.
``venue_order=True`` lists them in the order the venue's portal asks instead.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .database import SECTION_TITLES, Field, Venue, options_command

logger = logging.getLogger(__name__)

BLOCK_INDENT = "  "
_MULTILINE_TYPES = {"textarea", "authorlist", "filelist"}


@dataclass
class SubFile:
    """Parsed contents of a ``.sub`` file."""

    venue: str
    values: dict[str, str]


# The hosted paperpush web page; a .sub dropped onto it opens filled in.
WEB_URL = "https://paperpush.onrender.com"

# Ranks the field ids many venues share and gives each its section; see the
# file's own "$comment" and ordered_fields below.
COMMON_FIELDS_PATH = Path(__file__).with_name("common_fields.json")

# Section a field lands in when neither it nor common_fields.json names one.
FALLBACK_SECTION = "additional"


@lru_cache(maxsize=1)
def common_fields() -> dict[str, tuple[str, int]]:
    """Map each field id in ``common_fields.json`` to its (section, rank).

    Raises ``ValueError`` when the file names an unknown section or lists an id
    twice, since either would make the layout ambiguous.
    """
    data = json.loads(COMMON_FIELDS_PATH.read_text(encoding="utf-8"))
    ranks: dict[str, tuple[str, int]] = {}
    for section, ids in data.items():
        if section.startswith("$"):
            continue
        if section not in SECTION_TITLES:
            raise ValueError(f"{COMMON_FIELDS_PATH.name}: unknown section {section!r}")
        for rank, field_id in enumerate(ids):
            if field_id in ranks:
                raise ValueError(f"{COMMON_FIELDS_PATH.name}: {field_id!r} is listed twice")
            ranks[field_id] = (section, rank)
    return ranks


def field_section(field: Field) -> str:
    """The section of a ``.sub`` template that ``field`` is listed under.

    The field's own ``section`` wins; otherwise the section
    ``common_fields.json`` gives its id; otherwise :data:`FALLBACK_SECTION`. An
    unknown ``section`` value (e.g. from a newer published database) falls
    through rather than failing.
    """
    if field.section in SECTION_TITLES:
        return field.section
    common = common_fields().get(field.id)
    return common[0] if common else FALLBACK_SECTION


def ordered_fields(venue: Venue, venue_order: bool = False) -> list[tuple[Optional[str], Field]]:
    """Return ``venue``'s fields as (section, field) pairs in template order.

    With ``venue_order`` the fields keep the order the venue defines (its
    portal's order) and every section is None, so no headings are printed.

    Otherwise fields are grouped by :func:`field_section`, sections in
    :data:`~paperpush.database.SECTION_TITLES` order. Within a section, fields
    ``common_fields.json`` ranks for that section come first, in rank order,
    then the rest in venue order. Finally a field whose ``required_if`` target
    would come after it is moved to just after that target (taking the target's
    section), so a conditional question is never asked before the answer it
    depends on.
    """
    if venue_order:
        return [(None, f) for f in venue.fields]

    section_rank = {name: i for i, name in enumerate(SECTION_TITLES)}
    ranks = common_fields()

    def key(item: tuple[int, Field]) -> tuple[int, int, int]:
        index, field = item
        section = field_section(field)
        common = ranks.get(field.id)
        if common and common[0] == section:
            return (section_rank[section], 0, common[1])
        return (section_rank[section], 1, index)

    ordered = [(field_section(f), f) for _, f in sorted(enumerate(venue.fields), key=key)]

    # Move each dependent after its target. A pass can only move a field later,
    # and a chain of n dependents settles in at most n passes.
    for _ in range(len(ordered)):
        moved = False
        position = {f.id: i for i, (_, f) in enumerate(ordered)}
        for i, (_, field) in enumerate(ordered):
            target = position.get(field.required_if or "")
            if target is not None and target > i:
                ordered.pop(i)
                # The target shifted one place left when the dependent was popped.
                ordered.insert(target, (ordered[target - 1][0], field))
                moved = True
                break
        if not moved:
            break
    return ordered


def default_filename(venue: Venue) -> str:
    """Conventional ``.sub`` filename for a venue (e.g. ``biorxiv.sub``)."""
    return f"{venue.slug}.sub"


def _format_default(field: Field) -> str:
    """Render a field's default value as text for a fresh template."""
    if field.type == "boolean":
        return "yes" if field.default else "no"
    if field.default is None:
        return ""
    return str(field.default)


def render_template(
    venue: Venue,
    fill_defaults: bool = False,
    values: dict[str, str] | None = None,
    venue_order: bool = False,
) -> str:
    """Build the text of a ``.sub`` template for ``venue``.

    Fields are grouped into the shared sections of :func:`ordered_fields`, each
    under a comment heading; ``venue_order=True`` lists them in the venue's own
    (portal) order with no headings instead.

    When ``values`` is given, a field whose id appears there is pre-populated
    with that value (this is how ``autofill`` carries extracted values into the
    template). Otherwise, when ``fill_defaults`` is True, fields with
    a declared default are pre-populated; failing both, value slots are left
    empty. ``values`` takes precedence over ``fill_defaults`` for any field it
    names.
    """
    # The leading "-*- mode: yaml -*-" is an Emacs file-local mode line and the
    # trailing "# vim: set filetype=yaml :" (added below) is a Vim modeline.
    # Both make editors color the .sub as YAML -- which the format already
    # resembles -- so highlighting works without any plugin or per-user config.
    lines: list[str] = [
        f"# paperpush submission file for {venue.slug}  -*- mode: yaml -*-",
        f"# {venue.full_name or venue.slug}",
        "#",
        "# Edit the values below, then run:  paperpush submit " f"{default_filename(venue)}",
        "# Lines starting with '#' are comments and are ignored.",
        "#",
        "# To fill this in through a form instead, open it in the paperpush web page:",
        f"#   - run:  paperpush web {default_filename(venue)}   (use this file's path)",
        f"#   - or visit {WEB_URL} and drag this file onto the page",
        "",
        f"@venue: {venue.slug}",
    ]
    if venue.submission_guide:
        lines.append(f"# submission guide: {venue.submission_guide}")
    if venue_order:
        lines.append("# Fields are listed in the order the venue's submission form asks for them.")
    else:
        lines.append("# Fields are grouped by section. 'paperpush subfile --venue-order' lists them")
        lines.append("# in the order the venue's submission form asks for them instead.")
    lines.append("")

    current_section: Optional[str] = None
    for section, field in ordered_fields(venue, venue_order=venue_order):
        if section is not None and section != current_section:
            current_section = section
            rule = "# " + "-" * 70
            lines += [rule, f"# {SECTION_TITLES[section]}", rule, ""]
        flag = "REQUIRED" if field.required else "optional"
        lines.append(f"# {field.label} ({field.type}, {flag})")
        if field.help:
            lines.append(f"#   {field.help}")
        if field.min_count is not None or field.max_count is not None:
            lo = field.min_count if field.min_count is not None else 0
            hi = "any" if field.max_count is None else field.max_count
            if field.type == "multichoice":
                lines.append(f"#   select {lo}-{hi}, comma-separated")
            else:
                lines.append(f"#   provide {lo}-{hi} item(s), one per line")
        if field.word_count is not None:
            lines.append(f"#   at most {field.word_count} words")
        if field.character_count is not None:
            lines.append(f"#   at most {field.character_count} characters")
        if field.min_character_count is not None:
            lines.append(f"#   at least {field.min_character_count} characters")
        if field.require_url:
            lines.append("#   each non-blank line must be a valid http/https URL")
        # A venue that names its own main_text_end_headings excludes more than the
        # references from these limits, so the hint says "main text" rather than
        # naming the one section it stops at.
        before = "in the main text" if field.main_text_end_headings else "before the references"
        if field.max_words_before_refs is not None:
            lines.append(f"#   at most {field.max_words_before_refs} words {before}")
        if field.max_pages_before_refs is not None:
            lines.append(f"#   at most {field.max_pages_before_refs} pages {before}")
        if field.type == "int" and (field.min_value is not None or field.max_value is not None):
            lo = "any" if field.min_value is None else field.min_value
            hi = "any" if field.max_value is None else field.max_value
            lines.append(f"#   whole number, range {lo}-{hi} inclusive")
        if field.max_file_size_mb is not None:
            lines.append(f"#   each file at most {field.max_file_size_mb:g} MB")
        if field.options_file:
            # The options live in a shared vocabulary file that can run to
            # hundreds of entries (e.g. the arXiv categories); point at the
            # command that prints them rather than inlining the whole list.
            lines.append(f"#   options: run '{options_command(venue.slug, field.id)}' to list the choices")
        elif field.options:
            lines.append("#   options: " + " | ".join(field.options))
        if field.type_options:
            lines.append("#   file types: " + " | ".join(field.type_options))

        if values is not None and field.id in values:
            value = values[field.id]
        elif fill_defaults:
            value = _format_default(field)
        else:
            value = ""

        if field.type in _MULTILINE_TYPES:
            lines.append(f"{field.id}: |")
            if value:
                for v in value.splitlines() or [value]:
                    lines.append(f"{BLOCK_INDENT}{v}")
            else:
                lines.append(f"{BLOCK_INDENT}")
        else:
            lines.append(f"{field.id}: {value}")
        lines.append("")

    lines.append("# vim: set filetype=yaml :")
    return "\n".join(lines).rstrip() + "\n"


def write_template(
    venue: Venue,
    path: str | Path | None = None,
    fill_defaults: bool = False,
    overwrite: bool = False,
    venue_order: bool = False,
) -> Path:
    """Write a ``.sub`` template for ``venue`` and return its path.

    ``venue_order`` is passed to :func:`render_template`.
    """
    target = Path(path) if path is not None else Path(default_filename(venue))
    if target.exists() and not overwrite:
        logger.debug("write_template: %s exists and overwrite=False", target)
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_template(venue, fill_defaults=fill_defaults, venue_order=venue_order), encoding="utf-8")
    logger.debug(
        "write_template: wrote %s template to %s (fill_defaults=%s, venue_order=%s)",
        venue.slug,
        target,
        fill_defaults,
        venue_order,
    )
    return target


def parse(text: str) -> SubFile:
    """Parse the text of a ``.sub`` file into a :class:`SubFile`."""
    venue = ""
    values: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        i += 1

        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("@venue:"):
            venue = stripped.split(":", 1)[1].strip()
            continue

        if ":" not in raw:
            continue

        key, _, rest = raw.partition(":")
        key = key.strip()
        rest = rest.strip()

        if rest == "|":
            # Block value: consume following indented lines.
            block: list[str] = []
            while i < len(lines):
                nxt = lines[i]
                if nxt.startswith(BLOCK_INDENT):
                    block.append(nxt[len(BLOCK_INDENT) :])
                    i += 1
                elif nxt.strip() == "":
                    block.append("")
                    i += 1
                else:
                    break
            values[key] = "\n".join(block).strip("\n")
        else:
            values[key] = rest

    logger.debug("Parsed .sub: venue=%r, %d field value(s)", venue, len(values))
    return SubFile(venue=venue, values=values)


def load(path: str | Path) -> SubFile:
    """Read and parse a ``.sub`` file from disk."""
    logger.debug("Loading .sub file %s", path)
    return parse(Path(path).read_text(encoding="utf-8"))


def find_block(text: str, key: str) -> str | None:
    """Return the value of block field ``key`` (``key: |``) from raw text.

    The result matches what :func:`parse` stores for that field: the indented
    body with the common two-space indentation removed and surrounding blank
    lines trimmed. Returns None if ``key`` is not present as a block field.
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        head, sep, rest = lines[i].partition(":")
        i += 1
        if not sep or head.strip() != key or rest.strip() != "|":
            continue
        block: list[str] = []
        while i < len(lines):
            nxt = lines[i]
            if nxt.startswith(BLOCK_INDENT):
                block.append(nxt[len(BLOCK_INDENT) :])
                i += 1
            elif nxt.strip() == "":
                block.append("")
                i += 1
            else:
                break
        return "\n".join(block).strip("\n")
    return None


def replace_scalar(text: str, key: str, new_value: str) -> str:
    """Return ``text`` with single-line field ``key``'s value replaced.

    Rewrites only the value on a ``key: value`` line, preserving the key,
    comments, and every other line. Block fields (``key: |``) are left
    untouched -- use :func:`replace_block` for those. If ``key`` is not present
    as a scalar field the text is returned unchanged. ``new_value`` is written
    verbatim, so it must not contain a newline.
    """
    lines = text.splitlines()
    out: list[str] = []
    replaced = False
    for raw in lines:
        if not replaced:
            head, sep, rest = raw.partition(":")
            if sep and head.strip() == key and rest.strip() != "|":
                out.append(f"{head}: {new_value}")
                replaced = True
                continue
        out.append(raw)
    result = "\n".join(out)
    if text.endswith("\n"):
        result += "\n"
    return result


def replace_block(text: str, key: str, new_value: str) -> str:
    """Return ``text`` with block field ``key``'s body replaced by ``new_value``.

    Only the indented body following ``key: |`` is rewritten; comments, other
    fields, and blank-line spacing are preserved. If ``key`` is not a block
    field in ``text`` the text is returned unchanged.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        raw = lines[i]
        head, sep, rest = raw.partition(":")
        if not replaced and sep and head.strip() == key and rest.strip() == "|":
            out.append(raw)
            i += 1
            body: list[str] = []
            while i < len(lines) and (lines[i].startswith(BLOCK_INDENT) or lines[i].strip() == ""):
                body.append(lines[i])
                i += 1
            # Preserve blank lines that separate this block from what follows.
            trailing_blanks = 0
            while body and body[-1].strip() == "":
                trailing_blanks += 1
                body.pop()
            for value_line in new_value.splitlines() or [""]:
                out.append(f"{BLOCK_INDENT}{value_line}" if value_line else BLOCK_INDENT)
            out.extend([""] * trailing_blanks)
            replaced = True
            continue
        out.append(raw)
        i += 1
    result = "\n".join(out)
    if text.endswith("\n"):
        result += "\n"
    return result
