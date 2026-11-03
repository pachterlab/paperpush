#!/usr/bin/env python3
"""Report limits that submission_forms.json and manuscript_requirements.json disagree on.

Several limits are recorded twice, once on a portal field in
``submission_forms.json`` and once in the author guidelines in
``manuscript_requirements.json``. Examples are the abstract's word limit
(``abstract.word_count`` against ``abstract.max_words``), keyword counts,
per-file size caps and accepted file formats. When both files give a value
they should normally agree. This script lists every pair that does not.

A mismatch is a warning, not an error: a venue's own pages sometimes disagree
(the guidelines say 1000 characters, the portal stops at 998), and the portal
value is what ``validate`` enforces when both exist. So the script exits 0 and
only prints the list, unless ``--strict`` is given.

Both files are compared after inheritance (``inherits``) is applied. A
requirement that an ``article_types`` override changes is compared for each
article type too, but only reported when the portal's single limit is the
stricter one (a type the guidelines hold to a tighter limit is expected). A
form limit that depends on article type (``max_pages_by`` and the other
``*_by`` limits) is compared type by type.

For file formats, only formats the guidelines allow but the portal rejects are
reported by default. Portals routinely accept more (a .zip of LaTeX source,
legacy .doc); ``--all-formats`` reports that direction too.

The script also checks how the two files refer to each other. Each
``article_type_field`` must name a field in the venue's form, and each
``article_types`` key must be one of that field's options.

    python scripts/check_venue_consistency.py            # print warnings, exit 0
    python scripts/check_venue_consistency.py --strict   # exit 1 if there are any
    python scripts/check_venue_consistency.py --all-formats  # both format directions
    python scripts/check_venue_consistency.py nature cell  # only these venues
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from paperpush.database import Field, Venue, list_venues  # noqa: E402
from paperpush.requirements import (
    ManuscriptRequirements,
    get_requirements,
    resolve,
)  # noqa: E402

FORMS = "submission_forms.json"
REQS = "manuscript_requirements.json"

# The form field ids that play each role. Kept in step with
# paperpush.requirements_check._ROLE_FIELD_IDS, which validate uses to decide
# which guideline rule applies to which portal field.
ROLE_FIELD_IDS: dict[str, tuple[str, ...]] = {
    "manuscript": ("manuscript_file", "pdf_file", "combined_pdf", "manuscript"),
    "figures": ("figure_files", "figures"),
    "tables": ("table_files", "tables"),
    "supplementary": (
        "supplementary_files",
        "supplementary_file",
        "supplementary_material",
        "supplementary_tables",
        "supplement",
        "technical_supplement",
    ),
    "cover_letter": ("cover_letter",),
    "title": ("title",),
    "abstract": ("abstract",),
    "keywords": ("keywords",),
    "running_title": ("running_title", "short_title"),
}

# (role, form field attribute, requirements section, requirements key, bound).
# Each pair records the same limit in both files. ``bound`` says whether the
# limit is an upper ("max") or lower ("min") bound, which decides when an
# article type's own value conflicts with the portal's single value.
SCALAR_PAIRS: tuple[tuple[str, str, str, str, str], ...] = (
    ("abstract", "word_count", "abstract", "max_words", "max"),
    ("abstract", "character_count", "abstract", "max_characters", "max"),
    ("title", "character_count", "title_page", "title_max_characters", "max"),
    ("title", "word_count", "title_page", "title_max_words", "max"),
    (
        "running_title",
        "character_count",
        "title_page",
        "running_title_max_characters",
        "max",
    ),
    ("keywords", "min_count", "keywords", "min", "min"),
    ("keywords", "max_count", "keywords", "max", "max"),
    ("manuscript", "max_file_size_mb", "manuscript", "max_file_size_mb", "max"),
    ("manuscript", "max_words", "manuscript", "max_words", "max"),
    (
        "manuscript",
        "max_words_before_refs",
        "manuscript",
        "max_words_before_refs",
        "max",
    ),
    ("manuscript", "max_pages", "manuscript", "max_pages", "max"),
    (
        "manuscript",
        "max_pages_before_refs",
        "manuscript",
        "max_pages_before_refs",
        "max",
    ),
    ("figures", "max_file_size_mb", "figures", "max_file_size_mb", "max"),
    ("figures", "max_count", "figures", "max_count", "max"),
    ("tables", "max_count", "tables", "max_count", "max"),
    ("supplementary", "max_file_size_mb", "supplementary", "max_file_size_mb", "max"),
    ("supplementary", "max_count", "supplementary", "max_count", "max"),
)

# Manuscript length limits a form can make depend on a sibling field (``*_by``).
CONDITIONAL_ATTRS = (
    "max_words",
    "max_words_before_refs",
    "max_pages",
    "max_pages_before_refs",
)

# (role, requirements section) pairs whose accepted file formats are compared.
FORMAT_PAIRS: tuple[tuple[str, str], ...] = (
    ("manuscript", "manuscript"),
    ("figures", "figures"),
    ("tables", "tables"),
    ("supplementary", "supplementary"),
    ("cover_letter", "cover_letter"),
)

# Extensions that name the same format, so a .jpg/.jpeg difference is not reported.
_SAME_FORMAT = {".jpeg": ".jpg", ".tif": ".tiff", ".htm": ".html", ".tgz": ".tar.gz"}


@dataclass(frozen=True)
class Discrepancy:
    venue: str
    form_path: str
    form_value: Any
    req_path: str
    req_value: Any
    note: str = ""

    def lines(self) -> list[str]:
        out = [
            f"  {FORMS}: {self.form_path} = {_show(self.form_value)}",
            f"  {REQS}: {self.req_path} = {_show(self.req_value)}",
        ]
        if self.note:
            out.append(f"    ({self.note})")
        return out


def _show(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, tuple, set, frozenset)):
        return "[" + ", ".join(sorted(str(v) for v in value)) + "]"
    return str(value)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b


# Roles a venue fills with a single field. As in validate, the first id present
# wins; every present id counts for the other (multi-file) roles.
MULTI_FIELD_ROLES = frozenset({"figures", "tables", "supplementary"})


def _fields_for(venue: Venue, role: str) -> list[Field]:
    by_id = {f.id: f for f in venue.fields}
    present = [by_id[i] for i in ROLE_FIELD_IDS[role] if i in by_id]
    return present if role in MULTI_FIELD_ROLES else present[:1]


def _formats(values: Iterable[str]) -> frozenset[str]:
    out = set()
    for v in values:
        ext = v.strip().lower()
        if not ext.startswith("."):
            ext = "." + ext
        out.add(_SAME_FORMAT.get(ext, ext))
    return frozenset(out)


def _article_types(reqs: ManuscriptRequirements) -> list[Optional[str]]:
    """None (the top-level rules) followed by each article type name."""
    return [None, *(reqs.article_types or {})]


def _rules_for(reqs: ManuscriptRequirements, article_type: Optional[str]) -> ManuscriptRequirements:
    if article_type is None:
        return reqs
    return resolve(reqs, {reqs.article_type_field: article_type})


def _req_path(slug: str, article_type: Optional[str], section: str, key: str) -> str:
    if article_type is None:
        return f"{slug}.{section}.{key}"
    return f'{slug}.article_types["{article_type}"].{section}.{key}'


def _req_value(reqs: ManuscriptRequirements, section: str, key: str) -> Any:
    return getattr(getattr(reqs, section), key)


def _scalar_discrepancies(venue: Venue, reqs: ManuscriptRequirements) -> list[Discrepancy]:
    out: list[Discrepancy] = []
    for role, attr, section, key, bound in SCALAR_PAIRS:
        for field in _fields_for(venue, role):
            form_value = getattr(field, attr)
            if form_value is None:
                continue
            top = _req_value(reqs, section, key)
            for article_type in _article_types(reqs):
                req_value = _req_value(_rules_for(reqs, article_type), section, key)
                if req_value is None or _same(form_value, req_value):
                    continue
                note = ""
                if article_type is not None:
                    # Same value as the top level: already reported there.
                    if top is not None and _same(req_value, top):
                        continue
                    # The portal has one limit for every article type, so a
                    # type the guidelines hold to a stricter limit is expected.
                    # Only a looser one conflicts: the portal would block a
                    # manuscript the guidelines allow.
                    looser = req_value > form_value if bound == "max" else req_value < form_value
                    if not looser:
                        continue
                    note = "the portal's single limit is stricter than this article type's"
                out.append(
                    Discrepancy(
                        venue.slug,
                        f"{venue.slug}.{field.id}.{attr}",
                        form_value,
                        _req_path(venue.slug, article_type, section, key),
                        req_value,
                        note,
                    )
                )
    return out


def _conditional_discrepancies(venue: Venue, reqs: ManuscriptRequirements) -> list[Discrepancy]:
    """Compare a form's per-article-type length limits type by type."""
    out: list[Discrepancy] = []
    for field in _fields_for(venue, "manuscript"):
        for attr in CONDITIONAL_ATTRS:
            by = getattr(field, f"{attr}_by")
            if by is None:
                continue
            if by.field != reqs.article_type_field:
                continue
            for name, form_value in by.values.items():
                req_type = next(
                    (t for t in reqs.article_types if t.strip().lower() == name.strip().lower()),
                    None,
                )
                if req_type is None:
                    continue
                # Compare only a limit the guidelines state for this type. A
                # type that sets none falls back to the top-level value, which
                # is often another type's limit (Bioinformatics records its
                # Original Paper page cap there) rather than a rule for this one.
                req_value = ((reqs.article_types[req_type] or {}).get("manuscript") or {}).get(attr)
                if req_value is None or _same(form_value, req_value):
                    continue
                out.append(
                    Discrepancy(
                        venue.slug,
                        f'{venue.slug}.{field.id}.{attr}_by["{name}"]',
                        form_value,
                        _req_path(venue.slug, req_type, "manuscript", attr),
                        req_value,
                    )
                )
    return out


def _format_discrepancies(venue: Venue, reqs: ManuscriptRequirements, all_formats: bool = False) -> list[Discrepancy]:
    """Formats the guidelines allow but the portal's upload rejects.

    Portals routinely accept more than the guidelines list (a .zip of LaTeX
    source, legacy .doc), so that direction is only reported with
    ``all_formats``.
    """
    out: list[Discrepancy] = []
    for role, section in FORMAT_PAIRS:
        req_formats = getattr(reqs, section).formats
        if not req_formats:
            continue
        for field in _fields_for(venue, role):
            if not field.accept:
                continue
            form_set, req_set = _formats(field.accept), _formats(req_formats)
            if form_set == req_set or (not all_formats and req_set <= form_set):
                continue
            parts = []
            if all_formats and form_set - req_set:
                parts.append("portal also accepts " + ", ".join(sorted(form_set - req_set)))
            if req_set - form_set:
                parts.append("the guidelines allow " + ", ".join(sorted(req_set - form_set)) + ", which the portal does not accept")
            out.append(
                Discrepancy(
                    venue.slug,
                    f"{venue.slug}.{field.id}.accept",
                    field.accept,
                    f"{venue.slug}.{section}.formats",
                    req_formats,
                    "; ".join(parts),
                )
            )
    return out


def _venue_level_discrepancies(venue: Venue, reqs: ManuscriptRequirements) -> list[Discrepancy]:
    out: list[Discrepancy] = []
    if venue.max_upload_mb and reqs.upload.max_total_mb and not _same(venue.max_upload_mb, reqs.upload.max_total_mb):
        out.append(
            Discrepancy(
                venue.slug,
                f"{venue.slug}.max_upload_mb",
                venue.max_upload_mb,
                f"{venue.slug}.upload.max_total_mb",
                reqs.upload.max_total_mb,
            )
        )
    for field in _fields_for(venue, "cover_letter"):
        required = reqs.cover_letter.required
        if required is not None and bool(field.required) != bool(required):
            out.append(
                Discrepancy(
                    venue.slug,
                    f"{venue.slug}.{field.id}.required",
                    field.required,
                    f"{venue.slug}.cover_letter.required",
                    required,
                )
            )
    return out


def _reference_discrepancies(venue: Venue, reqs: ManuscriptRequirements) -> list[Discrepancy]:
    """article_type_field and article_types must point at the form's own field and options."""
    if not reqs.article_type_field:
        return []
    by_id = {f.id: f for f in venue.fields}
    field = by_id.get(reqs.article_type_field)
    path = f"{venue.slug}.article_type_field"
    if field is None:
        return [
            Discrepancy(
                venue.slug,
                f"{venue.slug} (no such field)",
                "-",
                path,
                reqs.article_type_field,
                "article_type_field names a field the form does not have, so per-type rules never apply",
            )
        ]
    options = field.options or []
    if not options or field.options_recommended:
        return []
    lowered = {o.strip().lower() for o in options}
    missing = [t for t in (reqs.article_types or {}) if t.strip().lower() not in lowered]
    if not missing:
        return []
    return [
        Discrepancy(
            venue.slug,
            f"{venue.slug}.{field.id}.options",
            options,
            f"{venue.slug}.article_types keys",
            missing,
            "these article types match no option, so their rules never apply",
        )
    ]


def find_discrepancies(slugs: Optional[Iterable[str]] = None, all_formats: bool = False) -> list[Discrepancy]:
    """Every disagreement between the two files, for venues that have a form and requirements."""
    wanted = {s.lower() for s in slugs} if slugs else None
    out: list[Discrepancy] = []
    for venue in list_venues(include_deprecated=True):
        if wanted is not None and venue.slug.lower() not in wanted:
            continue
        if not venue.supports_validate or not venue.has_form:
            continue
        reqs = get_requirements(venue.slug)
        if reqs is None:
            continue
        out.extend(_reference_discrepancies(venue, reqs))
        out.extend(_scalar_discrepancies(venue, reqs))
        out.extend(_conditional_discrepancies(venue, reqs))
        out.extend(_format_discrepancies(venue, reqs, all_formats))
        out.extend(_venue_level_discrepancies(venue, reqs))
    return out


def report(discrepancies: list[Discrepancy]) -> str:
    if not discrepancies:
        return f"{FORMS} and {REQS} agree on every limit they both record."
    venues = sorted({d.venue for d in discrepancies})
    lines = [
        f"{len(discrepancies)} value(s) differ between {FORMS} and {REQS} " f"across {len(venues)} venue(s):",
        "",
    ]
    for slug in venues:
        lines.append(f"{slug}:")
        for d in (d for d in discrepancies if d.venue == slug):
            lines.extend(d.lines())
        lines.append("")
    lines.append("These are warnings. Reconcile a value if one file is wrong. If the venue's own pages")
    lines.append("disagree, leave both as recorded: validate enforces the portal value when both exist.")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("venues", nargs="*", help="limit the check to these venue slugs")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any discrepancy is found")
    parser.add_argument(
        "--all-formats",
        action="store_true",
        help="also report formats the portal accepts but the guidelines do not list",
    )
    args = parser.parse_args(argv)
    # Compare the checked-in files, never a downloaded or overridden copy.
    os.environ["PAPERPUSH_VENUE_DATA"] = "bundled"
    found = find_discrepancies(args.venues or None, all_formats=args.all_formats)
    print(report(found), file=sys.stderr if found else sys.stdout)
    return 1 if (found and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
