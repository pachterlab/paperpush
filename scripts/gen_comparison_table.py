#!/usr/bin/env python3
"""Generate the "Comparison with other tools" table as a PDF and a PNG.

The table compares PaperPush with other manuscript-checking tools, feature by
feature. Green cells mean the tool has the feature, red cells mean it does not,
amber cells mean partial support, and grey cells mean we could not find out.

The tool data lives in ``TOOLS`` below; each entry lists the sources it came
from. Edit the data there and re-run:

    python scripts/gen_comparison_table.py

Outputs (by default):
    docs/_static/comparison.pdf
    docs/_static/comparison.png   (embedded in README.md)

The PaperPush column is computed from the repository (venue counts, skill
count, validation-check count), so it stays current.

Rendering uses Playwright's Chromium, which is already a PaperPush dependency.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "_static"

YES, NO, PARTIAL, UNKNOWN = "yes", "no", "partial", "unknown"

# Row key -> row label. Order is the order in the table.
FEATURES = [
    ("price", "Price"),
    ("open_source", "Open source"),
    ("tailored_checks", "Venue-tailored checks"),
    ("num_checks", "Number of checks"),
    ("requirement_db", "Venue requirement database"),
    ("metadata_file", "Submission metadata file"),
    ("shareable", "Shareable (plain-text, versionable)"),
    ("cli", "Command-line support"),
    ("ai_agent", "AI agent integration (MCP)"),
    ("skills", "Agent skills"),
    ("transfer", "Venue transfer support"),
    ("portal_filling", "Submission portal filling"),
    ("offline", "Offline support"),
]


# --------------------------------------------------------------------------
# PaperPush, computed from the repository
# --------------------------------------------------------------------------


def _readme_venue_counts() -> tuple[int, int]:
    """Return (validate venues, submit venues) from the README venue table."""
    text = (ROOT / "README.md").read_text()
    m = re.search(
        r"<!-- BEGIN SUPPORTED VENUES -->(.*?)<!-- END SUPPORTED VENUES -->",
        text,
        re.S,
    )
    block = m.group(1) if m else ""
    rows = [r for r in block.splitlines() if r.startswith("| [")]
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    validate = sum(1 for c in cells if len(c) >= 4 and c[2] == "✅")
    submit = sum(1 for c in cells if len(c) >= 4 and c[3] == "✅")
    return validate, submit


def _check_count() -> int | None:
    text = (ROOT / "README.md").read_text()
    m = re.search(r"(\d+) distinct validation check types", text)
    return int(m.group(1)) if m else None


def _skill_count() -> int:
    skills = ROOT / "paperpush" / "_skills"
    return sum(1 for p in skills.iterdir() if (p / "SKILL.md").exists())


def paperpush_column() -> dict:
    validate_venues, submit_venues = _readme_venue_counts()
    checks = _check_count()
    skills = _skill_count()
    return {
        "name": "PaperPush",
        "price": (YES, "Free"),
        "open_source": (YES, "BSD-2"),
        "tailored_checks": (YES, f"{validate_venues} venues", "(+ any venue, via online guideline scraping)"),
        "num_checks": (YES, f"{checks}" if checks else "yes"),
        "requirement_db": (YES, "Open JSON"),
        "metadata_file": (YES, ".sub file"),
        "shareable": (YES, "Yes"),
        "cli": (YES, "Yes"),
        "ai_agent": (YES, "MCP server"),
        "skills": (YES, f"{skills}"),
        "transfer": (YES, "Yes"),
        "portal_filling": (YES, f"{submit_venues} venues"),
        "offline": (YES, "Yes"),
        "sources": ["This repository"],
    }


# --------------------------------------------------------------------------
# Other tools. Researched 2026-11-07 from the vendors' sites and listings.
# Cells are (status, short text). Leave the text empty to use the default
# label for the status.
# --------------------------------------------------------------------------

_CLOSED = (NO, "No")

TOOLS = [
    {
        "name": "Paperpal",
        "price": (NO, "Free tier; $25/mo"),
        "open_source": _CLOSED,
        "tailored_checks": (PARTIAL, "Publisher Preflight only"),
        "num_checks": (YES, "30+"),
        "requirement_db": (NO, ""),
        "metadata_file": (NO, ""),
        "shareable": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "sources": [
            "https://paperpal.com/manuscript-check",
            "https://www.capterra.com/p/10003076/Paperpal/",
        ],
    },
    {
        "name": "Penelope.ai",
        "price": (NO, "Journals pay (from $750/mo)"),
        "open_source": _CLOSED,
        "tailored_checks": (YES, "Licensing journals"),
        "num_checks": (YES, "30+"),
        "requirement_db": (PARTIAL, "Per-journal configs, private"),
        "metadata_file": (NO, ""),
        "shareable": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "sources": [
            "https://www.penelope.ai",
            "https://www.capterra.com/p/255629/Penelopeai/",
        ],
    },
    {
        "name": "Trinka AI",
        "price": (NO, "Free tier; $80/yr"),
        "open_source": _CLOSED,
        "tailored_checks": (NO, "Generic checks"),
        "num_checks": (YES, "25+"),
        "requirement_db": (NO, ""),
        "metadata_file": (NO, ""),
        "shareable": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "sources": ["https://trinka.ai/features", "https://trinka.ai/pricing"],
    },
    {
        "name": "Research Eagle",
        "price": (NO, "Free tier; $12/paper"),
        "open_source": _CLOSED,
        "tailored_checks": (YES, "18,960 journals"),
        "num_checks": (PARTIAL, "Not stated"),
        "requirement_db": (PARTIAL, "18,960 journals, private"),
        "metadata_file": (NO, ""),
        "shareable": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "sources": ["https://researcheagle.org/", "https://researcheagle.org/pricing"],
    },
    {
        "name": "Articlo",
        "price": (NO, "$19/paper or $19/mo"),
        "open_source": _CLOSED,
        "tailored_checks": (YES, "2,875 journals"),
        "num_checks": (PARTIAL, "Formatting checklist"),
        "requirement_db": (PARTIAL, "2,875 journals, private"),
        "metadata_file": (NO, ""),
        "shareable": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (YES, "Re-targeting"),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "sources": ["https://getarticlo.com/", "https://getarticlo.com/journals"],
    },
    {
        "name": "CheckMyManuscript",
        "price": (NO, "$50/report"),
        "open_source": _CLOSED,
        "tailored_checks": (YES, "100+ publishers"),
        "num_checks": (YES, "95"),
        "requirement_db": (PARTIAL, "Private"),
        "metadata_file": (NO, ""),
        "shareable": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "sources": ["https://checkmymanuscript.com/"],
    },
    {
        "name": "SciScore",
        "price": (NO, "Paid; free via some journals"),
        "open_source": _CLOSED,
        "tailored_checks": (NO, "Rigor criteria only"),
        "num_checks": (PARTIAL, "Rigor score"),
        "requirement_db": (NO, ""),
        "metadata_file": (NO, ""),
        "shareable": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "sources": [
            "https://sciscore.com/",
            "https://pmc.ncbi.nlm.nih.gov/articles/PMC7644557/",
        ],
    },
    {
        "name": "Writefull",
        "price": (NO, "Free tier; ~$15/mo"),
        "open_source": _CLOSED,
        "tailored_checks": (NO, "Language only"),
        "num_checks": (NO, "Language only"),
        "requirement_db": (NO, ""),
        "metadata_file": (NO, ""),
        "shareable": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "sources": [
            "https://www.writefull.com",
            "https://www.capterra.com/p/236309/Writefull/pricing/",
        ],
    },
    {
        "name": "SciSpace",
        "price": (NO, "Free tier; $12–20/mo"),
        "open_source": _CLOSED,
        "tailored_checks": (PARTIAL, "Journal checklist agent"),
        "num_checks": (UNKNOWN, "?"),
        "requirement_db": (PARTIAL, "Journal templates"),
        "metadata_file": (NO, ""),
        "shareable": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "sources": [
            "https://libguides.library.arizona.edu/ai-researchers/scispace",
            "https://www.capterra.com/p/178127/Scispace/pricing/",
        ],
    },
]

DEFAULT_TEXT = {YES: "Yes", NO: "No", PARTIAL: "Partial", UNKNOWN: "?"}

CSS = """
@page { size: 17in 8.5in; margin: 0.35in; }
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: #ffffff; color: #1f2328;
       font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; }
p.sub { margin: 0 0 12px; color: #57606a; font-size: 12px; }
table { border-collapse: separate; border-spacing: 3px; width: 100%; table-layout: fixed; }
th, td { font-size: 11.5px; padding: 6px 5px; text-align: center; border-radius: 4px;
         line-height: 1.25; }
th.tool { background: #f6f8fa; font-weight: 600; vertical-align: bottom; }
th.tool.us { background: #0969da; color: #fff; }
td small { display: block; font-weight: 400; font-size: 9.5px; margin-top: 2px; }
th.tool small { display: block; font-weight: 400; font-size: 10px; color: #6e7781; }
th.feature { text-align: left; background: #f6f8fa; font-weight: 600; width: 190px; }
td.yes { background: #c9f0d1; color: #0a5c22; }
td.no { background: #ffd6d6; color: #8a1010; }
td.partial { background: #ffe9b0; color: #6b4800; }
td.unknown { background: #e5e7ea; color: #57606a; }
td.us { font-weight: 700; outline: 2px solid #1a7f37; }
.legend { margin-top: 10px; font-size: 11px; color: #57606a; }
.legend span { display: inline-block; padding: 2px 8px; border-radius: 4px; margin-right: 6px; }
"""


def cell(tool: dict, key: str) -> tuple[str, str, str]:
    """Return (status, text, note). A cell may carry an optional third item, a note shown below the text."""
    status, text, *rest = tool.get(key, (UNKNOWN, ""))
    return status, text or DEFAULT_TEXT[status], rest[0] if rest else ""


def build_html(columns: list[dict]) -> str:
    head = ['<th class="feature">Feature</th>']
    for i, t in enumerate(columns):
        note = f"<small>{html.escape(t['note'])}</small>" if t.get("note") else ""
        cls = "tool us" if i == 0 else "tool"
        head.append(f'<th class="{cls}">{html.escape(t["name"])}{note}</th>')
    rows = []
    for key, label in FEATURES:
        tds = [f'<th class="feature">{html.escape(label)}</th>']
        for i, t in enumerate(columns):
            status, text, note = cell(t, key)
            cls = f"{status} us" if i == 0 else status
            note_html = f"<small>{html.escape(note)}</small>" if note else ""
            tds.append(f'<td class="{cls}">{html.escape(text)}{note_html}</td>')
        rows.append("<tr>" + "".join(tds) + "</tr>")
    today = date.today().isoformat()
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>PaperPush comparison</title><style>{CSS}</style></head><body>
<h1>PaperPush compared with other manuscript tools</h1>
<p class="sub">Feature support as advertised by each tool, checked {today}.
Prices are list prices for individual authors where available.</p>
<table><thead><tr>{''.join(head)}</tr></thead><tbody>{''.join(rows)}</tbody></table>
<div class="legend"><span class="yes" style="background:#c9f0d1">Supported</span>
<span style="background:#ffe9b0">Partial</span>
<span style="background:#ffd6d6">Not supported</span>
<span style="background:#e5e7ea">Unknown</span></div>
</body></html>"""


def render(html_text: str, pdf: Path, png: Path | None) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 400}, device_scale_factor=2)
        page.set_content(html_text, wait_until="load")
        page.pdf(path=str(pdf), prefer_css_page_size=True, print_background=True)
        if png is not None:
            page.screenshot(path=str(png), full_page=True)
        browser.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--no-png", action="store_true", help="Write only the PDF.")
    ap.add_argument("--html", action="store_true", help="Also write the HTML source.")
    ap.add_argument("--json", action="store_true", help="Print the table data as JSON and exit.")
    args = ap.parse_args(argv)

    columns = [paperpush_column(), *TOOLS]
    if args.json:
        print(json.dumps(columns, indent=2, ensure_ascii=False))
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    text = build_html(columns)
    if args.html:
        (args.out_dir / "comparison.html").write_text(text)
    pdf = args.out_dir / "comparison.pdf"
    png = None if args.no_png else args.out_dir / "comparison.png"
    render(text, pdf, png)
    print(f"Wrote {pdf}" + (f" and {png}" if png else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
