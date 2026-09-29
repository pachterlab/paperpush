#!/usr/bin/env python3
"""Measure a compiled venue template for ``manuscript.template_layout``.

Compile the venue's official template exactly as distributed (submission /
review mode, nothing edited), then point this at the PDF. It prints the
``template_layout`` object to paste into the venue's ``manuscript`` section of
``paperpush/manuscript_requirements.json``:

    python scripts/measure_template.py path/to/template.pdf \\
        --running-head "Under review as a conference paper at ICLR 2027"

The numbers come from the same measurement ``paperpush validate`` applies to a
submission (:func:`paperpush.template_check.measure`), so a paper built from
the unmodified template measures clean by construction. Check the output by
eye: ``text_top_pt``/``text_bottom_pt`` need a template with full pages of
body text, which the sample paper in most style bundles has.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from paperpush.template_check import measure  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", type=Path, help="the compiled, unmodified template")
    parser.add_argument("--running-head", default="", help="the running head the submission version prints on page 1")
    args = parser.parse_args(argv)

    m = measure(args.pdf, args.running_head)
    if m is None:
        print(f"error: could not read glyphs from {args.pdf}", file=sys.stderr)
        return 1
    if not m.top_by_page:
        print("error: no page has enough body text to locate the text block; compile a longer sample", file=sys.stderr)
        return 1
    layout = {
        "page_width_pt": round(m.page_width, 1),
        "page_height_pt": round(m.page_height, 1),
        "text_left_pt": m.left,
        "text_right_pt": m.right,
        "text_top_pt": max(m.top_by_page),
        "text_bottom_pt": min(m.bottom_by_page),
        "body_font_pt": m.body_font,
        "baseline_skip_pt": m.baseline_skip,
        "running_head": args.running_head or None,
        "margin_line_numbers": m.margin_line_numbers,
        "tolerance_pt": 3,
    }
    print(json.dumps({k: v for k, v in layout.items() if v is not None}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
