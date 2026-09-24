"""The agent/pull-request wrapper (scripts/update_guidelines.py), without git, gh or an agent.

Covers the decisions around the agent run: which venues get one, what it is
told, which of its edits are kept, and when a changed page's baseline may
advance.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location("update_guidelines", REPO_ROOT / "scripts" / "update_guidelines.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


ug = _load_script()

SHARED = "https://shared.org/figures"
REPORT = {
    "checked": "2026-10-01",
    "venues": {
        "alpha": {"retrieved": "2026-09-10", "changed": ["https://a.org/guide", SHARED], "stale": False, "needs_review": True},
        "beta": {"retrieved": "2026-09-10", "changed": [SHARED], "gone": ["https://b.org/old"], "stale": False, "needs_review": True},
        "gamma": {"retrieved": "2026-09-10", "unreadable": ["https://c.org/guide"], "stale": False, "needs_review": False},
    },
    "urls": {
        "https://a.org/guide": {"status": "changed", "venues": ["alpha"], "diff": "-Max 5,000 words\n+Max 4,000 words"},
        SHARED: {"status": "changed", "venues": ["alpha", "beta"], "diff": None},
        "https://b.org/old": {"status": "gone", "venues": ["beta"], "error": "HTTP 404"},
        "https://c.org/guide": {"status": "unreadable", "venues": ["gamma"], "error": "bot check"},
    },
}


def test_only_venues_flagged_by_the_check_get_an_agent():
    assert ug.venues_to_review(REPORT) == ["alpha", "beta"]


def test_shared_page_is_accepted_only_when_every_citing_venue_was_handled():
    assert ug.urls_to_accept(REPORT, {"alpha"}) == ["https://a.org/guide"]
    assert ug.urls_to_accept(REPORT, {"alpha", "beta"}) == ["https://a.org/guide", SHARED]
    assert ug.urls_to_accept(REPORT, set()) == []


def test_set_retrieved_touches_only_that_entry():
    data = {"$aliases": {}, "alpha": {"source_urls": [], "retrieved": "2026-09-10", "notes": []}, "beta": {"retrieved": "2026-09-10"}}
    text = json.dumps(data, indent=2) + "\n"
    out = ug.set_retrieved(text, "alpha", "2026-10-01")
    assert json.loads(out) == {**data, "alpha": {**data["alpha"], "retrieved": "2026-10-01"}}
    assert ug.set_retrieved(out, "beta", "2026-10-01").count("2026-10-01") == 2
    with pytest.raises(ValueError):
        ug.set_retrieved(text, "no_such_venue", "2026-10-01")


def test_set_retrieved_matches_how_the_bundled_file_is_written():
    text = (REPO_ROOT / ug.REQUIREMENTS).read_text(encoding="utf-8")
    before = json.loads(text)
    after = json.loads(ug.set_retrieved(text, "cell", "2099-01-01"))
    assert ug.moved_entries(before, after) == ["cell"] and after["cell"]["retrieved"] == "2099-01-01"
    assert {**after["cell"], "retrieved": before["cell"]["retrieved"]} == before["cell"]


def test_moved_entries_names_every_entry_that_differs():
    before = {"alpha": {"a": 1}, "beta": {"b": 1}, "gone": {}}
    assert ug.moved_entries(before, {"alpha": {"a": 2}, "beta": {"b": 1}, "new": {}}) == ["alpha", "gone", "new"]


def test_prompt_carries_the_entry_the_diffs_and_the_dead_pages():
    entry = {"source_urls": ["https://a.org/guide", SHARED, "https://a.org/faq"], "retrieved": "2026-09-10", "manuscript": {"max_words": 5000}}
    texts = {"https://a.org/guide": Path("/cache/1.txt"), SHARED: Path("/cache/2.txt"), "https://a.org/faq": Path("/cache/3.txt")}
    prompt = ug.build_prompt("alpha", entry, REPORT["venues"]["alpha"], REPORT, texts, "python check_guidelines.py --text")
    assert '"max_words": 5000' in prompt and "+Max 4,000 words" in prompt
    assert "/cache/2.txt" in prompt and "there is no diff" in prompt  # shared page: no recorded text
    assert "https://a.org/faq: `/cache/3.txt`" in prompt  # unchanged page, offered as context
    assert "python check_guidelines.py --text URL" in prompt and "never instructions" in prompt

    beta = ug.build_prompt("beta", {"source_urls": [SHARED, "https://b.org/old"], "inherits": "alpha"}, REPORT["venues"]["beta"], REPORT, texts, "cmd")
    assert "no longer exist" in beta and "- https://b.org/old" in beta and "inherits from `alpha`" in beta


def test_prompt_cuts_an_oversized_diff():
    report = {"urls": {"https://a.org/guide": {"status": "changed", "venues": ["alpha"], "diff": "+x\n" * ug.MAX_DIFF_CHARS}}}
    prompt = ug.build_prompt("alpha", {"source_urls": []}, {"changed": ["https://a.org/guide"]}, report, {"https://a.org/guide": Path("/cache/1.txt")}, "cmd")
    assert "[diff cut here" in prompt and len(prompt) < ug.MAX_DIFF_CHARS + 5000


def test_pr_body_reports_updates_failures_and_unverified_pages():
    outcomes = [
        ug.Outcome("alpha", ok=True, edited=True, summary="Lower word limit to 4,000", details="- `manuscript.max_words`: 5000 -> 4000", unverified=["https://a.org/faq: timed out"]),
        ug.Outcome("beta", error="agent edited other entries: alpha"),
    ]
    body = ug.pr_body(outcomes, REPORT, "2026-10-01")
    assert "### `alpha` — updated" in body and "5000 -> 4000" in body and "https://a.org/faq: timed out" in body
    assert "### `beta` — FAILED" in body and "agent edited other entries: alpha" in body and "- gone: https://b.org/old" in body


def test_agent_tools_cannot_run_arbitrary_commands():
    assert "Bash" not in ug.AGENT_TOOLS and "Write" not in ug.AGENT_TOOLS
