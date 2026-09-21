"""Guideline change detection (scripts/check_guidelines.py), without a browser.

The fetch itself needs real publisher pages, so what is covered here is
everything around it: which pages are checked, how a fetch is classified against
the recorded baseline, how a venue's pages roll up into "needs review", and how
the baseline only advances through ``promote`` (``--record-new`` / ``--accept``).
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location("check_guidelines", REPO_ROOT / "scripts" / "check_guidelines.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


cg = _load_script()

TODAY = date(2026, 9, 21)
REQUIREMENTS = {
    "$schema": "./manuscript_requirements.schema.json",
    "$aliases": ["sections"],
    "alpha": {"source_urls": ["https://a.org/guide", "https://shared.org/figures"], "retrieved": "2026-09-10"},
    "beta": {"source_urls": ["https://shared.org/figures", "https://b.org/guide.pdf"], "retrieved": "2025-01-01"},
    "gamma": {"retrieved": "2026-09-10"},
}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cg, "CACHE_DIR", tmp_path / "cache")
    return tmp_path / "cache"


def test_collect_urls_skips_metadata_and_merges_shared_pages():
    assert cg.collect_urls(REQUIREMENTS) == {
        "https://a.org/guide": ["alpha"],
        "https://shared.org/figures": ["alpha", "beta"],
        "https://b.org/guide.pdf": ["beta"],
    }


def test_every_bundled_entry_names_its_pages():
    data = json.loads((REPO_ROOT / "paperpush" / "manuscript_requirements.json").read_text(encoding="utf-8"))
    cited = {slug for slugs in cg.collect_urls(data).values() for slug in slugs}
    assert cited == {slug for slug in data if not slug.startswith("$")}


def test_normalize_ignores_layout_but_not_wording():
    a = cg.normalize("Word limit:\u00a0 5,000   words\n\n\n  Figures: 6\r\n")
    assert a == "Word limit: 5,000 words\nFigures: 6"
    assert cg.sha256(a) == cg.sha256(cg.normalize("Word limit: 5,000 words\nFigures: 6"))
    assert cg.sha256(a) != cg.sha256(cg.normalize("Word limit: 4,000 words\nFigures: 6"))


def test_normalize_masks_running_countdowns_only():
    a = cg.normalize("Paper deadline Sep 25, 2026 11:59 PM AOE or 00 weeks 04 days 17:12:24 .")
    b = cg.normalize("Paper deadline Sep 25, 2026 11:59 PM AOE or 00 weeks 04 days 16:58:44 .")
    assert a == b == "Paper deadline Sep 25, 2026 11:59 PM AOE or <countdown> ."
    assert cg.normalize("Revisions are due within 14 days.") == "Revisions are due within 14 days."
    assert cg.normalize("Sep 26, 2026 11:59 PM") != cg.normalize("Sep 25, 2026 11:59 PM")


def test_fetch_order_alternates_hosts_and_puts_documents_last():
    urls = ["https://a.org/x.pdf", "https://a.org/1", "https://a.org/2", "https://b.org/1"]
    assert cg.fetch_order(urls) == ["https://a.org/1", "https://b.org/1", "https://a.org/2", "https://a.org/x.pdf"]


@pytest.mark.parametrize(
    "fetched, baseline, expected",
    [
        (cg.Fetched(text="same"), cg.sha256("same"), "unchanged"),
        (cg.Fetched(text="edited"), cg.sha256("same"), "changed"),
        (cg.Fetched(text="same"), None, "new"),
        # A block or outage is never a change, whatever the baseline.
        (cg.Fetched(error="HTTP 403"), cg.sha256("same"), "unreadable"),
        (cg.Fetched(error="HTTP 404", gone=True), cg.sha256("same"), "gone"),
    ],
)
def test_classify(fetched, baseline, expected):
    assert cg.classify(fetched, baseline) == expected


def test_unified_diff_shows_the_changed_line():
    diff = cg.unified_diff("Title\nMax 5,000 words\nEnd", "Title\nMax 4,000 words\nEnd", "https://a.org/guide")
    assert "-Max 5,000 words" in diff and "+Max 4,000 words" in diff


def test_docx_text_reads_paragraphs():
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xml = f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:t>Final </w:t></w:r><w:r><w:t>checklist</w:t></w:r></w:p><w:p><w:r><w:t>Figures</w:t></w:r></w:p></w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    assert cg.document_text("https://a.org/Final%20Checklist.docx", buf.getvalue()) == "Final checklist\nFigures"


def test_venue_report_flags_changes_and_dead_pages():
    results = {"https://a.org/guide": {"status": "changed"}, "https://shared.org/figures": {"status": "unchanged"}}
    report = cg.venue_report("alpha", REQUIREMENTS["alpha"], results, TODAY, 180)
    assert report["changed"] == ["https://a.org/guide"] and report["needs_review"] and not report["stale"]

    results = {"https://a.org/guide": {"status": "gone"}, "https://shared.org/figures": {"status": "unchanged"}}
    assert cg.venue_report("alpha", REQUIREMENTS["alpha"], results, TODAY, 180)["needs_review"]


def test_venue_report_unreadable_page_needs_review_only_once_the_entry_is_old():
    results = {"https://a.org/guide": {"status": "unreadable"}, "https://shared.org/figures": {"status": "unreadable"}, "https://b.org/guide.pdf": {"status": "unchanged"}}
    recent = cg.venue_report("alpha", REQUIREMENTS["alpha"], results, TODAY, 180)
    assert not recent["stale"] and not recent["needs_review"]
    old = cg.venue_report("beta", REQUIREMENTS["beta"], results, TODAY, 180)
    assert old["stale"] and old["needs_review"]


def test_promote_advances_only_pages_that_were_read():
    url, unread = "https://a.org/guide", "https://b.org/guide.pdf"
    fingerprints = {url: {"sha256": "old", "chars": 1, "recorded": "2026-01-01"}}
    state = {url: {"sha256": "new", "chars": 42}, unread: {"failures": 3, "error": "HTTP 403"}}
    assert cg.promote(fingerprints, state, [url, unread], TODAY) == [url]
    assert fingerprints == {url: {"sha256": "new", "chars": 42, "recorded": "2026-09-21"}}
    # Read earlier but not in the last check: that text is not what was just reviewed.
    state[url] = {"sha256": "newer", "chars": 50, "failures": 1}
    assert cg.promote(fingerprints, state, [url], TODAY) == []
    state[url] = {"sha256": "new", "chars": 42, "failures": 0}
    # Already current: nothing moves, and the recorded date is kept.
    assert cg.promote(fingerprints, state, [url], date(2027, 1, 1)) == []
    assert fingerprints[url]["recorded"] == "2026-09-21"


def test_text_cache_round_trip_and_prune(cache):
    kept, dropped = cg.cache_text("recorded text"), cg.cache_text("superseded text")
    assert cg.cached_text(kept) == "recorded text"
    cg.prune_cache({kept})
    assert cg.cached_text(kept) == "recorded text" and cg.cached_text(dropped) is None
    assert cg.cached_text(None) is None


def test_accept_writes_the_baseline_and_drops_pages_no_longer_cited(cache, tmp_path, monkeypatch, capsys):
    fingerprints_file = tmp_path / "fingerprints.json"
    monkeypatch.setattr(cg, "FINGERPRINTS_FILE", fingerprints_file)
    fingerprints_file.write_text(json.dumps({"https://removed.org/old": {"sha256": "x", "chars": 1, "recorded": "2026-01-01"}}))
    cache.mkdir()
    (cache / "state.json").write_text(json.dumps({"https://a.org/guide": {"sha256": "abc", "chars": 900}}))

    assert cg.run_accept(["alpha"], REQUIREMENTS, cg.collect_urls(REQUIREMENTS), TODAY) == 0
    assert json.loads(fingerprints_file.read_text()) == {"https://a.org/guide": {"sha256": "abc", "chars": 900, "recorded": "2026-09-21"}}
    assert "https://shared.org/figures" in capsys.readouterr().out  # reported: never read, so not advanced


def test_select_rejects_unknown_venue():
    with pytest.raises(SystemExit):
        cg._select(cg.collect_urls(REQUIREMENTS), ["no_such_venue"], [])
