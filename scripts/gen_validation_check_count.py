#!/usr/bin/env python3
"""Keep the documented validation-check count in sync with the validator.

Count distinct check IDs in reports for every bundled venue, using both an
empty submission and a representative submission with files. Network checks
are disabled; they still appear in the report as unchecked. Rules explicitly
marked ``not_implemented`` are not counted as checks.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["PAPERPUSH_VENUE_DATA"] = "bundled"

from paperpush.database import list_venues  # noqa: E402
from paperpush.requirements_check import _fields_for  # noqa: E402
from paperpush.subfile import SubFile  # noqa: E402
from paperpush.validate import validate_report  # noqa: E402
from tests.pdf_builder import text_line, write_pdf  # noqa: E402

README = ROOT / "README.md"
DOCS = ROOT / "docs" / "validation-checks.rst"
START = "<!-- BEGIN VALIDATION CHECK COUNT -->"
END = "<!-- END VALIDATION CHECK COUNT -->"
DOCS_START = ".. BEGIN VALIDATION CHECK COUNT"
DOCS_END = ".. END VALIDATION CHECK COUNT"
LIST_START = ".. BEGIN VALIDATION CHECK LIST"
LIST_END = ".. END VALIDATION CHECK LIST"


def check_catalog() -> dict[str, str]:
    catalog: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as directory:
        manuscript = Path(directory) / "manuscript.txt"
        manuscript.write_text("A sample title\nAbstract\nIntroduction\nSample text.\nReferences\n", encoding="utf-8")
        pdf = write_pdf(Path(directory) / "manuscript.pdf", [text_line(72, 720, "A sample title") + text_line(72, 680, "Abstract") + text_line(72, 660, "Sample text")])
        figure = Path(directory) / "figure.png"
        # A minimal PNG is enough to exercise the figure-check registration.
        import base64

        figure.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=="))
        files = {"manuscript": pdf, "figures": figure, "tables": manuscript,
                 "supplementary": manuscript, "cover_letter": manuscript, "title_page": pdf}
        for venue in list_venues():
            for filled in (False, True):
                values: dict[str, str] = {}
                if filled:
                    for role, path in files.items():
                        for field in _fields_for(venue, role):
                            if field.type in ("file", "filelist"):
                                values[field.id] = str(path)
                    for field in _fields_for(venue, "title"):
                        values[field.id] = "A sample title"
                result = validate_report(
                    SubFile(venue=venue.slug, values=values), venue,
                    check_links=False, check_references=False,
                    check_sensitive=False, check_hidden_text=False,
                    check_openreview=False, check_anonymous=False,
                )
                catalog.update((item.id, item.title) for item in result.results if not item.id.startswith("not_implemented."))
    # Some checks are registered only after an optional dependency is missing
    # or a bibliography search has an ambiguous result. Include those literal
    # declarations without making network calls or changing the environment.
    for source in (ROOT / "paperpush").glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "checks" and node.func.attr in ("run", "record"):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    check_id = node.args[0].value
                    title = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str) else None
                    catalog.setdefault(check_id, title or check_id.replace(".", ": ").replace("_", " ").capitalize())
            elif (isinstance(node.func, ast.Name) and node.func.id == "AiTask") or (isinstance(node.func, ast.Attribute) and node.func.attr == "AiTask"):
                check_id = None
                title = None
                for keyword in node.keywords:
                    if keyword.arg == "id" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                        check_id = keyword.value.value
                    if keyword.arg == "title" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                        title = keyword.value.value
                if check_id:
                    catalog.setdefault(check_id, title or check_id.replace(".", ": ").replace("_", " ").capitalize())
    catalog.update({
        "ai.cover_letter_venue": "Cover letter addressed to the target venue (when the automatic check can't decide)",
        "cover_letter.venue": "Cover letter addressed to the target venue",
        "ai.reference_source.*": "Each cited source supports the claims that cite it (full reference check)",
        "ai.reference_journal.*": "Each cited journal is correctly identified and indexed (full reference check)",
    })
    return {check_id: title for check_id, title in catalog.items() if not check_id.startswith("not_implemented.")}


def check_ids() -> set[str]:
    return set(check_catalog())


def check_list(catalog: dict[str, str]) -> str:
    groups = {
        "form": "Submission form", "files": "Files", "guidelines": "Venue guidelines",
        "layout": "Page layout", "figures": "Figures", "citations": "Figure and table citations",
        "consistency": "Submission and manuscript consistency", "cover_letter": "Cover letter",
        "front_matter": "Abstract and front matter", "bibliography": "Bibliography",
        "prose": "Prose", "integrity": "Integrity and external sources",
        "details": "Additional manuscript details", "ai": "Checks requiring a reader",
    }
    by_group: dict[str, list[str]] = {}
    for check_id in sorted(catalog):
        by_group.setdefault(check_id.split(".")[0], []).append(check_id)
    number = 0
    lines = ["All validation check types", "--------------------------", "",
             "Each item below is a distinct report check ID. A check may be not applicable,",
             "unable to run, or require a reader; which ones run depends on the venue, files,",
             "and options. The two ``.*`` entries are families with one numbered check per",
             "cited reference when ``--reference-check-full`` is used. Venue rules marked",
             "*not implemented* are excluded.", ""]
    for prefix, ids in by_group.items():
        lines.extend([f"**{groups.get(prefix, prefix.replace('_', ' ').title())}**", ""])
        for check_id in ids:
            number += 1
            # Report titles may quote venue wording; keep generated docs in US English.
            title = re.sub(
                r"\bcolour(s)?\b",
                lambda match: ("Color" if match.group()[0].isupper() else "color") + (match.group(1) or ""),
                catalog[check_id],
                flags=re.I,
            )
            lines.append(f"{number}. ``{check_id}`` — {title}")
        lines.append("")
    return "\n".join(lines).rstrip()


def replace_block(path: Path, content: str, *, check: bool, start_marker: str = START, end_marker: str = END) -> bool:
    original = path.read_text(encoding="utf-8")
    start = original.index(start_marker) + len(start_marker)
    end = original.index(end_marker, start)
    separator = "\n\n" if start_marker in (DOCS_START, LIST_START) else "\n"
    updated = original[:start] + separator + content + separator + original[end:]
    if updated == original:
        return True
    if not check:
        path.write_text(updated, encoding="utf-8")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the documented count is stale")
    args = parser.parse_args()
    catalog = check_catalog()
    count = len(catalog)
    readme = f"- Extensive venue-specific manuscript guideline checking including section lengths, reference hallucination, figure resolution, link validation, and anonymization — {count} distinct validation check types. The checks in a report depend on the venue and files. [See the full list](https://paperpush.readthedocs.io/en/latest/validation-checks.html)."
    docs = f"PaperPush currently has **{count} distinct validation check types** across its bundled venues. This count is generated from check IDs in validation reports for empty and representative file submissions, plus declarations for conditional paths; it excludes rules marked *not implemented*. The checks in any one report depend on the venue, supplied files, and selected options. Regenerate it with ``python scripts/gen_validation_check_count.py``."
    current = [replace_block(README, readme, check=args.check), replace_block(DOCS, docs, check=args.check, start_marker=DOCS_START, end_marker=DOCS_END),
               replace_block(DOCS, check_list(catalog), check=args.check, start_marker=LIST_START, end_marker=LIST_END)]
    print(f"{count} distinct validation check types")
    if args.check and not all(current):
        print("Documented check count is stale; run python scripts/gen_validation_check_count.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
