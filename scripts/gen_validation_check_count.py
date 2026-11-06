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


def check_ids() -> set[str]:
    ids: set[str] = set()
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
                ids.update(item.id for item in result.results if not item.id.startswith("not_implemented."))
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
                    ids.add(node.args[0].value)
            elif isinstance(node.func, ast.Name) and node.func.id == "AiTask":
                for keyword in node.keywords:
                    if keyword.arg == "id" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                        ids.add(keyword.value.value)
    return {check_id for check_id in ids if not check_id.startswith("not_implemented.")}


def replace_block(path: Path, content: str, *, check: bool, start_marker: str = START, end_marker: str = END) -> bool:
    original = path.read_text(encoding="utf-8")
    start = original.index(start_marker) + len(start_marker)
    end = original.index(end_marker, start)
    separator = "\n\n" if start_marker == DOCS_START else "\n"
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
    count = len(check_ids())
    readme = f"- Extensive venue-specific manuscript guideline checking including section lengths, reference hallucination, figure resolution, link validation, and anonymization — {count} distinct validation check types. The checks in a report depend on the venue and files."
    docs = f"PaperPush currently has **{count} distinct validation check types** across its bundled venues. This count is generated from check IDs in validation reports for empty and representative file submissions, plus declarations for conditional paths; it excludes rules marked *not implemented*. The checks in any one report depend on the venue, supplied files, and selected options. Regenerate it with ``python scripts/gen_validation_check_count.py``."
    current = [replace_block(README, readme, check=args.check), replace_block(DOCS, docs, check=args.check, start_marker=DOCS_START, end_marker=DOCS_END)]
    print(f"{count} distinct validation check types")
    if args.check and not all(current):
        print("Documented check count is stale; run python scripts/gen_validation_check_count.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
