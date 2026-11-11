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
    docs/_static/comparison.json  (the table data, used by --check)

``--check`` exits 1 when the table data differs from comparison.json (CI runs
it); ``--if-stale`` re-renders only when it differs (the pre-commit hook runs
it), so the PaperPush column follows the README's check and venue counts.

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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "_static"

YES, NO, PARTIAL, UNKNOWN = "yes", "no", "partial", "unknown"

# (section, [(row key, row label), ...]). Order is the order in the table.
SECTIONS = [
    (
        "Format",
        [
            ("price", "Price"),
            ("open_source", "Open source"),
            ("privacy", "Privacy and data storage"),
        ],
    ),
    (
        "Features",
        [
            ("venue_picking", "Venue picking"),
            ("file_validation", "File validation"),
            ("reviewer_selection", "Reviewer selection"),
            ("portal_filling", "Submission portal filling"),
            ("respond_reviewers", "Responding to reviewers"),
            ("transfer", "Venue transfer reformatting"),
        ],
    ),
    (
        "Manuscript checks",
        [
            ("num_checks", "Number of checks"),
            ("tailored_checks", "Venue-tailored checks"),
            ("non_ai_checks", "Non-AI (rule-based) checks"),
            ("chk_references", "Reference verification (DOIs, retractions, hallucinations)"),
            ("chk_figures", "Figure quality (resolution, lettering, color)"),
            ("chk_limits", "Word, page, and section limits"),
            ("chk_layout", "Page layout (margins, fonts, spacing)"),
            ("chk_anonymity", "Anonymization for blind review"),
            ("chk_rigor", "Reporting and rigor (RRIDs, statistics, ethics)"),
            ("chk_language", "Language and spelling"),
            ("reformat", "Reformat to venue requirements"),
        ],
    ),
    (
        "Databases",
        [
            ("requirement_db", "Venue guidelines database"),
            ("portal_db", "Venue submission portal database"),
        ],
    ),
    (
        "Workflow",
        [
            ("metadata_file", "Submission metadata file"),
            ("skills", "AI agent skills"),
            ("ai_agent", "MCP server"),
            ("web", "Web interface"),
            ("cli", "Command-line support"),
            ("offline", "Offline support"),
        ],
    ),
]
FEATURES = [row for _, rows in SECTIONS for row in rows]


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


def _non_ai_check_count() -> int:
    """Checks that run without a model: every check ID outside the ``ai.`` namespace."""
    ids = json.loads((ROOT / "paperpush" / "validation_check_ids.json").read_text())
    return sum(1 for i in ids if not i.startswith("ai."))


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
        "privacy": (YES, "Runs locally; nothing stored"),
        "venue_picking": (YES, "Yes"),
        "file_validation": (YES, "Yes"),
        "reviewer_selection": (YES, "Yes"),
        "portal_filling": (YES, f"{submit_venues} venues"),
        "respond_reviewers": (YES, "Yes"),
        "transfer": (YES, "Yes"),
        "num_checks": (YES, f"{checks}" if checks else "yes"),
        "tailored_checks": (YES, f"{validate_venues} venues", "(+ any venue, via online guideline scraping)"),
        "non_ai_checks": (YES, f"{_non_ai_check_count()}"),
        "chk_references": (YES, "DOIs, retractions, preprint versions"),
        "chk_figures": (YES, "DPI, lettering, fonts, colorblind"),
        "chk_limits": (YES, "Word, page, section"),
        "chk_layout": (YES, "Margins, fonts, spacing, line nos."),
        "chk_anonymity": (YES, "Yes"),
        "chk_rigor": (YES, "RRIDs, statistics, cell lines"),
        "chk_language": (YES, "Spelling, prose, acronyms"),
        "reformat": (YES, "Yes"),
        "requirement_db": (YES, f"{validate_venues} venues, open JSON"),
        "portal_db": (YES, f"{submit_venues} venues, open JSON"),
        "metadata_file": (YES, ".sub file (plain text)"),
        "skills": (YES, f"{skills}"),
        "ai_agent": (YES, "Yes"),
        "web": (YES, "Yes"),
        "cli": (YES, "Yes"),
        "offline": (YES, "Yes"),
        "sources": ["This repository"],
    }


# Date the other tools' data was last researched (shown in the table).
RESEARCHED = "2026-11-11"

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
        "chk_references": (PARTIAL, "Hallucinated refs"),
        "chk_figures": (NO, ""),
        "chk_limits": (YES, "Word counts"),
        "chk_layout": (UNKNOWN, "?"),
        "chk_anonymity": (NO, ""),
        "chk_rigor": (PARTIAL, "Ethics statements"),
        "chk_language": (YES, ""),
        "reformat": (NO, ""),
        "requirement_db": (NO, ""),
        "metadata_file": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "privacy": (PARTIAL, "Not used for training"),
        "venue_picking": (NO, ""),
        "file_validation": (PARTIAL, "Submission readiness"),
        "reviewer_selection": (NO, ""),
        "respond_reviewers": (PARTIAL, "Drafts from reviewer notes"),
        "non_ai_checks": (UNKNOWN, "?"),
        "portal_db": (NO, ""),
        "web": (YES, ""),
        "sources": [
            "https://paperpal.com/privacy-policy",
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
        "chk_references": (PARTIAL, "Citation matching"),
        "chk_figures": (PARTIAL, "Citations, legends"),
        "chk_limits": (YES, "Word, item counts"),
        "chk_layout": (PARTIAL, "Line spacing"),
        "chk_anonymity": (NO, ""),
        "chk_rigor": (PARTIAL, "Ethics, PRISMA/CONSORT"),
        "chk_language": (NO, ""),
        "reformat": (NO, ""),
        "requirement_db": (PARTIAL, "Per-journal configs, private"),
        "metadata_file": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "privacy": (UNKNOWN, "?"),
        "venue_picking": (NO, ""),
        "file_validation": (YES, "Track changes, counts"),
        "reviewer_selection": (NO, ""),
        "respond_reviewers": (NO, ""),
        "non_ai_checks": (YES, "30+"),
        "portal_db": (PARTIAL, "Portal integrations"),
        "web": (YES, ""),
        "sources": [
            "https://www.penelope.ai/privacy",
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
        "chk_references": (YES, "Retracted, hallucinated"),
        "chk_figures": (UNKNOWN, "?"),
        "chk_limits": (UNKNOWN, "?"),
        "chk_layout": (UNKNOWN, "?"),
        "chk_anonymity": (UNKNOWN, "?"),
        "chk_rigor": (UNKNOWN, "?"),
        "chk_language": (YES, ""),
        "reformat": (PARTIAL, "Citations only"),
        "requirement_db": (NO, ""),
        "metadata_file": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "privacy": (PARTIAL, "Not stored on Confidential plan"),
        "venue_picking": (YES, "Journal Finder"),
        "file_validation": (PARTIAL, "Technical checks"),
        "reviewer_selection": (NO, ""),
        "respond_reviewers": (NO, ""),
        "non_ai_checks": (UNKNOWN, "?"),
        "portal_db": (NO, ""),
        "web": (YES, ""),
        "sources": ["https://trinka.ai/features", "https://trinka.ai/pricing", "https://www.trinka.ai/features"],
    },
    {
        "name": "Research Eagle",
        "price": (PARTIAL, "Checks free; AI from $12/paper"),
        "open_source": _CLOSED,
        "tailored_checks": (YES, "18,960 journals", "(of 30,269 indexed; general checks for the rest)"),
        "num_checks": (PARTIAL, "Not stated"),
        "chk_references": (NO, ""),
        "chk_figures": (PARTIAL, "DPI"),
        "chk_limits": (YES, "Word limits"),
        "chk_layout": (UNKNOWN, "?"),
        "chk_anonymity": (UNKNOWN, "?"),
        "chk_rigor": (UNKNOWN, "?"),
        "chk_language": (NO, ""),
        "reformat": (NO, ""),
        "requirement_db": (PARTIAL, "18,960 journals, private"),
        "metadata_file": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "privacy": (YES, "Deleted in same request; no training"),
        "venue_picking": (NO, ""),
        "file_validation": (PARTIAL, "DOCX/PDF, size limit"),
        "reviewer_selection": (NO, ""),
        "respond_reviewers": (NO, ""),
        "non_ai_checks": (YES, "Standard check uses no AI"),
        "portal_db": (NO, ""),
        "web": (YES, ""),
        "sources": ["https://researcheagle.org/", "https://researcheagle.org/pricing", "https://researcheagle.org/privacy-policy/"],
    },
    {
        "name": "Articlo",
        "price": (NO, "$19/paper or $19/mo"),
        "open_source": _CLOSED,
        "tailored_checks": (YES, "2,875 journals"),
        "num_checks": (PARTIAL, "Formatting checklist"),
        "chk_references": (NO, ""),
        "chk_figures": (UNKNOWN, "?"),
        "chk_limits": (UNKNOWN, "?"),
        "chk_layout": (YES, "Applies journal layout"),
        "chk_anonymity": (NO, ""),
        "chk_rigor": (UNKNOWN, "?"),
        "chk_language": (NO, ""),
        "reformat": (YES, "Word output"),
        "requirement_db": (PARTIAL, "2,875 journals, private"),
        "metadata_file": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (YES, "Re-targeting"),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "privacy": (PARTIAL, "Stored in account; never sent to AI"),
        "venue_picking": (NO, ""),
        "file_validation": (PARTIAL, "Flags separate source files"),
        "reviewer_selection": (NO, ""),
        "respond_reviewers": (NO, ""),
        "non_ai_checks": (YES, "Rule-based formatting"),
        "portal_db": (NO, ""),
        "web": (YES, ""),
        "sources": ["https://getarticlo.com/", "https://getarticlo.com/journals"],
    },
    {
        "name": "CheckMyManuscript",
        "price": (NO, "$50/report"),
        "open_source": _CLOSED,
        "tailored_checks": (YES, "100+ publishers"),
        "num_checks": (YES, "95"),
        "chk_references": (PARTIAL, "Citation matching"),
        "chk_figures": (PARTIAL, "Citations, captions"),
        "chk_limits": (YES, "Word limits"),
        "chk_layout": (UNKNOWN, "?"),
        "chk_anonymity": (NO, ""),
        "chk_rigor": (NO, ""),
        "chk_language": (YES, ""),
        "reformat": (NO, ""),
        "requirement_db": (PARTIAL, "Private"),
        "metadata_file": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "privacy": (YES, "Deleted after processing"),
        "venue_picking": (YES, "Journal Finder"),
        "file_validation": (PARTIAL, "Format checks"),
        "reviewer_selection": (YES, "Reviewer Finder"),
        "respond_reviewers": (NO, ""),
        "non_ai_checks": (UNKNOWN, "?"),
        "portal_db": (NO, ""),
        "web": (YES, ""),
        "sources": ["https://checkmymanuscript.com/", "https://checkmymanuscript.com"],
    },
    {
        "name": "SciScore",
        "price": (NO, "Paid; free via some journals"),
        "open_source": _CLOSED,
        "tailored_checks": (NO, "Rigor criteria only"),
        "num_checks": (PARTIAL, "Rigor score"),
        "chk_references": (NO, ""),
        "chk_figures": (NO, ""),
        "chk_limits": (NO, ""),
        "chk_layout": (NO, ""),
        "chk_anonymity": (NO, ""),
        "chk_rigor": (YES, "RRIDs, rigor criteria"),
        "chk_language": (NO, ""),
        "reformat": (NO, ""),
        "requirement_db": (NO, ""),
        "metadata_file": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "privacy": (UNKNOWN, "?"),
        "venue_picking": (NO, ""),
        "file_validation": (NO, ""),
        "reviewer_selection": (NO, ""),
        "respond_reviewers": (NO, ""),
        "non_ai_checks": (PARTIAL, "Regex + ML"),
        "portal_db": (NO, ""),
        "web": (YES, ""),
        "sources": [
            "https://jmir.org/2022/6/e37324",
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
        "chk_references": (UNKNOWN, "?"),
        "chk_figures": (NO, ""),
        "chk_limits": (NO, ""),
        "chk_layout": (NO, ""),
        "chk_anonymity": (NO, ""),
        "chk_rigor": (NO, ""),
        "chk_language": (YES, ""),
        "reformat": (NO, ""),
        "requirement_db": (NO, ""),
        "metadata_file": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "privacy": (PARTIAL, "Stored (EU); not used for training"),
        "venue_picking": (NO, ""),
        "file_validation": (NO, ""),
        "reviewer_selection": (NO, ""),
        "respond_reviewers": (NO, ""),
        "non_ai_checks": (NO, "Language models"),
        "portal_db": (NO, ""),
        "web": (YES, ""),
        "sources": [
            "https://writefull.com/privacy",
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
        "chk_references": (UNKNOWN, "?"),
        "chk_figures": (UNKNOWN, "?"),
        "chk_limits": (UNKNOWN, "?"),
        "chk_layout": (YES, "Via templates"),
        "chk_anonymity": (UNKNOWN, "?"),
        "chk_rigor": (UNKNOWN, "?"),
        "chk_language": (UNKNOWN, "?"),
        "reformat": (YES, "40,000+ templates"),
        "requirement_db": (PARTIAL, "Journal templates"),
        "metadata_file": (NO, ""),
        "cli": (NO, ""),
        "ai_agent": (NO, ""),
        "skills": (NO, "0"),
        "transfer": (NO, ""),
        "portal_filling": (NO, ""),
        "offline": (NO, ""),
        "privacy": (PARTIAL, "API: not used for training"),
        "venue_picking": (PARTIAL, "DOAJ journal finder"),
        "file_validation": (PARTIAL, "Guideline compliance"),
        "reviewer_selection": (UNKNOWN, "?"),
        "respond_reviewers": (UNKNOWN, "?"),
        "non_ai_checks": (UNKNOWN, "?"),
        "portal_db": (NO, ""),
        "web": (YES, ""),
        "sources": [
            "https://docs.api.scispace.com/platform/data-privacy",
            "https://scispace.com/agents/doaj-i6csev0m",
            "https://libguides.library.arizona.edu/ai-researchers/scispace",
            "https://scispace.com/formats",
            "https://www.capterra.com/p/178127/Scispace/pricing/",
        ],
    },
]

DEFAULT_TEXT = {YES: "Yes", NO: "No", PARTIAL: "Partial", UNKNOWN: "?"}

CSS = """
@page { size: 17in 15in; margin: 0.35in; }
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
tr.group th { text-align: left; font-size: 12.5px; font-weight: 700; padding: 12px 4px 3px;
              background: none; border-bottom: 1.5px solid #1f2328; border-radius: 0; }
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
    for section, features in SECTIONS:
        rows.append(f'<tr class="group"><th colspan="{len(columns) + 1}">{html.escape(section)}</th></tr>')
        for key, label in features:
            tds = [f'<th class="feature">{html.escape(label)}</th>']
            for i, t in enumerate(columns):
                status, text, note = cell(t, key)
                cls = f"{status} us" if i == 0 else status
                note_html = f"<small>{html.escape(note)}</small>" if note else ""
                tds.append(f'<td class="{cls}">{html.escape(text)}{note_html}</td>')
            rows.append("<tr>" + "".join(tds) + "</tr>")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>PaperPush comparison</title><style>{CSS}</style></head><body>
<h1>PaperPush compared with other manuscript tools</h1>
<p class="sub">Feature support as advertised by each tool, checked {RESEARCHED}.</p>
<table><thead><tr>{''.join(head)}</tr></thead><tbody>{''.join(rows)}</tbody></table>
<div class="legend"><span style="background:#c9f0d1">Supported</span>
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
    ap.add_argument("--check", action="store_true", help="Exit 1 if the rendered table is out of date.")
    ap.add_argument("--if-stale", action="store_true", help="Re-render only if the table is out of date.")
    args = ap.parse_args(argv)

    columns = [paperpush_column(), *TOOLS]
    data = json.dumps(columns, indent=2, ensure_ascii=False) + "\n"
    if args.json:
        print(data, end="")
        return 0

    data_path = args.out_dir / "comparison.json"
    stale = not data_path.exists() or data_path.read_text() != data
    if args.check:
        if stale:
            print("Comparison table is stale; run python scripts/gen_comparison_table.py", file=sys.stderr)
            return 1
        return 0
    if args.if_stale and not stale:
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    text = build_html(columns)
    if args.html:
        (args.out_dir / "comparison.html").write_text(text)
    pdf = args.out_dir / "comparison.pdf"
    png = None if args.no_png else args.out_dir / "comparison.png"
    render(text, pdf, png)
    data_path.write_text(data)
    print(f"Wrote {pdf}" + (f" and {png}" if png else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
