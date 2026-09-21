#!/usr/bin/env python3
"""Detect changes to the author-guideline pages behind manuscript_requirements.json.

Every entry in ``paperpush/manuscript_requirements.json`` names the pages it was
read from (``source_urls``). This script re-reads those pages and says which
ones changed since the entry was written, so an agent (or a person) only has to
re-read the venues whose guidelines actually moved. It never calls an LLM.

    python scripts/check_guidelines.py                 # check every page
    python scripts/check_guidelines.py --venue nature  # one venue
    python scripts/check_guidelines.py --record-new    # baseline pages that have none
    python scripts/check_guidelines.py --accept nature # requirements updated: advance baseline

How a page is compared. Each page is rendered in a real browser (several
publishers refuse plain HTTP clients), reduced to the text of its main content
(navigation, header, footer, cookie banners and scripts removed), normalized,
and hashed. PDF and .docx sources are downloaded and their text extracted. A
fetch that fails, hits a bot wall, or yields almost no text is *unreadable*
rather than changed, so a transient block never registers as a guideline change.

Where state lives.

- ``scripts/guideline_fingerprints.json`` (committed) maps each URL to the
  sha256 of the text the current requirements entry reflects. It only moves via
  ``--record-new`` / ``--accept``, so committing it together with the matching
  edit to ``manuscript_requirements.json`` is what marks a change as handled; a
  change that was never merged is simply detected again on the next run.
- ``.guideline_cache/`` (gitignored) holds the page texts, keyed by sha256, so a
  change can be shown as a diff, plus per-URL bookkeeping (``state.json``) and
  the last run's ``report.json``. It can be deleted at any time; the only loss
  is the diff for the next change (the full new text is reported instead).
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from xml.etree import ElementTree  # parses .docx parts fetched from publisher sites  # nosec B405

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_FILE = REPO_ROOT / "paperpush" / "manuscript_requirements.json"
FINGERPRINTS_FILE = REPO_ROOT / "scripts" / "guideline_fingerprints.json"
CACHE_DIR = REPO_ROOT / ".guideline_cache"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
NAV_TIMEOUT_MS = 45_000
# How long to wait for a bot-check interstitial to clear by itself.
CHALLENGE_TIMEOUT_S = 25
# Seconds between two requests to the same host.
DEFAULT_DELAY_S = 5.0
# A page with less main-content text than this did not render (JavaScript shell,
# consent wall, soft block) and is treated as unreadable.
MIN_CHARS = 200
# A venue with an unreadable page and an entry older than this is reported as
# stale: nothing cheap can vouch for it, so it is due for a full re-read.
DEFAULT_STALE_DAYS = 180

DOCUMENT_SUFFIXES = (".pdf", ".docx")
# Statuses that mean the page no longer exists (a stable state that needs a new
# URL), as opposed to a block or outage that may clear by the next run.
GONE_STATUSES = (404, 410)
# A live countdown ("00 weeks 04 days 17:12:24") differs on every load. Units are
# only masked when they lead up to a running clock, so "within 14 days" is kept.
_COUNTDOWN = re.compile(r"(?:\d+\s*(?:weeks?|days?|hours?|hrs?|minutes?|mins?|seconds?|secs?)\s*,?\s*)*\b\d{1,3}:\d{2}:\d{2}\b", re.IGNORECASE)
_CHALLENGE_TITLE = re.compile(r"just a moment|attention required|access denied|are you a robot|security check|verif(y|ying) you are human", re.IGNORECASE)

# Runs in the page: picks the main content (falling back to <body>), removes the
# boilerplate inside it, and returns its rendered text.
_EXTRACT_JS = """
() => {
  const drop = [
    'script', 'style', 'noscript', 'template', 'svg', 'iframe', 'nav', 'header', 'footer', 'aside', 'form',
    '[role=navigation]', '[role=banner]', '[role=contentinfo]', '[role=complementary]', '[role=search]',
    '[aria-hidden=true]', '[hidden]',
    '[id*=cookie i]', '[class*=cookie i]', '[id*=consent i]', '[class*=consent i]', '[id*=onetrust i]',
    '[class*=advert i]', '[class*=banner i]', '[class*=breadcrumb i]', '[class*=social i]', '[class*=share i]',
  ];
  const roots = ['main', '[role=main]', 'article', '#main-content', '#content', '.main-content'];
  let best = null;
  for (const sel of roots) {
    for (const el of document.querySelectorAll(sel)) {
      const n = (el.innerText || '').length;
      if (!best || n > best.n) best = {el, n};
    }
    if (best && best.n > 0) break;
  }
  const root = (best && best.n > 0) ? best.el : document.body;
  const total = (root.innerText || '').length;
  for (const el of root.querySelectorAll(drop.join(','))) {
    // A loose class match on a page wrapper (<div class="has-banner">) must not
    // take the guidelines with it.
    if (el.isConnected && (el.innerText || '').length <= total / 2) el.remove();
  }
  return root.innerText || '';
}
"""


# --------------------------------------------------------------------------- #
# Pure helpers (no browser, no network): covered by tests/test_check_guidelines.py
# --------------------------------------------------------------------------- #


def collect_urls(requirements: dict) -> dict[str, list[str]]:
    """``{url: [venue slugs that cite it]}`` for every entry, in file order."""
    out: dict[str, list[str]] = {}
    for slug, entry in requirements.items():
        if slug.startswith("$") or not isinstance(entry, dict):
            continue
        for url in entry.get("source_urls") or []:
            out.setdefault(url, [])
            if slug not in out[url]:
                out[url].append(slug)
    return out


def normalize(text: str) -> str:
    """Canonical form of a page's text: what is hashed and what is diffed.

    Unicode-normalized, whitespace collapsed within each line, blank lines
    dropped. Lines are kept (rather than joining everything) so that a change
    shows up as a readable line diff.
    """
    text = unicodedata.normalize("NFKC", text).replace("\u00ad", "").replace("\u200b", "")  # soft hyphen, zero-width space
    text = _COUNTDOWN.sub("<countdown>", text)
    lines = (re.sub(r"\s+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_document(url: str) -> bool:
    return urlparse(url).path.lower().endswith(DOCUMENT_SUFFIXES)


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def docx_text(data: bytes) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        root = ElementTree.fromstring(zf.read("word/document.xml"))  # nosec B314
    return "\n".join("".join(t.text or "" for t in para.iter(f"{ns}t")) for para in root.iter(f"{ns}p"))


def document_text(url: str, data: bytes) -> str:
    return docx_text(data) if urlparse(url).path.lower().endswith(".docx") else pdf_text(data)


def fetch_order(urls: list[str]) -> list[str]:
    """Round-robin across hosts, so the per-host delay rarely has to be slept.

    Within a host, documents go last: a PDF behind a bot wall is fetched with
    the cookies the host's HTML pages earned.
    """
    by_host: dict[str, list[str]] = {}
    for url in urls:
        by_host.setdefault(urlparse(url).netloc, []).append(url)
    queues = [sorted(group, key=is_document) for group in by_host.values()]
    out = []
    while any(queues):
        for queue in queues:
            if queue:
                out.append(queue.pop(0))
    return out


@dataclass
class Fetched:
    """One fetch: ``text`` is the normalized page text, or ``None`` with ``error`` set."""

    text: Optional[str] = None
    error: Optional[str] = None
    gone: bool = False


def classify(fetched: Fetched, baseline_sha: Optional[str]) -> str:
    """``unchanged`` / ``changed`` / ``new`` (no baseline yet) / ``unreadable`` / ``gone``."""
    if fetched.text is None:
        return "gone" if fetched.gone else "unreadable"
    if baseline_sha is None:
        return "new"
    return "unchanged" if sha256(fetched.text) == baseline_sha else "changed"


def unified_diff(old: str, new: str, url: str) -> str:
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), f"recorded {url}", f"current {url}", n=2, lineterm=""))


def venue_report(slug: str, entry: dict, url_results: dict[str, dict], today: date, stale_days: int) -> dict:
    """Roll the per-URL results of one venue up into what a caller acts on."""
    out: dict = {"retrieved": entry.get("retrieved", "")}
    for status in ("changed", "new", "unreadable", "gone"):
        urls = [u for u in entry.get("source_urls") or [] if url_results.get(u, {}).get("status") == status]
        if urls:
            out[status] = urls
    try:
        age = (today - date.fromisoformat(out["retrieved"])).days
    except ValueError:
        age = None
    # Unreadable pages cannot be vouched for; past the age limit they need a full re-read.
    out["stale"] = bool(out.get("unreadable")) and (age is None or age > stale_days)
    out["needs_review"] = bool(out.get("changed") or out.get("gone") or out["stale"])
    return out


# --------------------------------------------------------------------------- #
# State on disk
# --------------------------------------------------------------------------- #


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _text_path(sha: str) -> Path:
    return CACHE_DIR / "text" / f"{sha}.txt"


def cached_text(sha: Optional[str]) -> Optional[str]:
    path = _text_path(sha) if sha else None
    return path.read_text(encoding="utf-8") if path and path.exists() else None


def cache_text(text: str) -> str:
    sha = sha256(text)
    path = _text_path(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return sha


def prune_cache(keep: set[str]) -> None:
    for path in (CACHE_DIR / "text").glob("*.txt"):
        if path.stem not in keep:
            path.unlink()


def promote(fingerprints: dict, state: dict, urls: list[str], today: date) -> list[str]:
    """Make the text seen in the last check of each of ``urls`` its baseline; returns the URLs moved.

    A page whose last check failed is left alone: the text on record for it is
    from an earlier run, not the one the requirements were just updated from.
    """
    moved = []
    for url in urls:
        seen = None if state.get(url, {}).get("failures") else state.get(url, {}).get("sha256")
        if seen and fingerprints.get(url, {}).get("sha256") != seen:
            fingerprints[url] = {"sha256": seen, "chars": state[url].get("chars", 0), "recorded": today.isoformat()}
            moved.append(url)
    return moved


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #


class Browser:
    """One Chromium context for the whole run, with a per-host request delay."""

    def __init__(self, headless: bool, delay: float):
        from playwright.sync_api import sync_playwright

        self._delay = delay
        self._last: dict[str, float] = {}
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
        self._ctx = self._browser.new_context(user_agent=USER_AGENT, locale="en-US", viewport={"width": 1366, "height": 900}, accept_downloads=True)

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()

    def _wait_turn(self, url: str) -> None:
        host = urlparse(url).netloc
        wait = self._last.get(host, 0) + self._delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def fetch(self, url: str) -> Fetched:
        self._wait_turn(url)
        try:
            fetched = self._fetch_document(url) if is_document(url) else self._fetch_page(url)
        except Exception as exc:  # any network/browser failure is "unreadable", never "changed"
            return Fetched(error=f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
        if fetched.text is not None and len(fetched.text) < MIN_CHARS:
            return Fetched(error=f"only {len(fetched.text)} characters of text")
        return fetched

    def _fetch_document(self, url: str) -> Fetched:
        resp = self._ctx.request.get(url, timeout=NAV_TIMEOUT_MS)
        if resp.status >= 400:
            return Fetched(error=f"HTTP {resp.status}", gone=resp.status in GONE_STATUSES)
        return Fetched(text=normalize(document_text(url, resp.body())))

    def _fetch_page(self, url: str) -> Fetched:
        page = self._ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            deadline = time.monotonic() + CHALLENGE_TIMEOUT_S
            while _CHALLENGE_TITLE.search(page.title()) and time.monotonic() < deadline:
                page.wait_for_timeout(1000)
            if _CHALLENGE_TITLE.search(page.title()):
                return Fetched(error=f"bot check ({page.title()!r})")
            status = resp.status if resp else 0
            # A cleared challenge leaves the original 403 on `resp`; only trust an
            # error status when the page that ended up rendered is still an error page.
            if status in GONE_STATUSES:
                return Fetched(error=f"HTTP {status}", gone=True)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # pages with long-polling never go idle; the text is there anyway  # nosec B110
                pass
            # innerText applies CSS text-transform, so a heading reads "Methods" or
            # "METHODS" depending on whether the stylesheet had arrived yet.
            page.add_style_tag(content="* { text-transform: none !important; }")
            text = normalize(page.evaluate(_EXTRACT_JS))
            if status >= 400 and len(text) < MIN_CHARS:
                return Fetched(error=f"HTTP {status}")
            return Fetched(text=text)
        finally:
            page.close()


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _select(url_venues: dict[str, list[str]], venues: list[str], urls: list[str]) -> list[str]:
    unknown = [v for v in venues if not any(v in slugs for slugs in url_venues.values())] + [u for u in urls if u not in url_venues]
    if unknown:
        sys.exit(f"not in {REQUIREMENTS_FILE.name}: {', '.join(unknown)}")
    if not venues and not urls:
        return list(url_venues)
    return [u for u, slugs in url_venues.items() if u in urls or any(v in slugs for v in venues)]


def run_accept(targets: list[str], requirements: dict, url_venues: dict[str, list[str]], today: date) -> int:
    venues = [t for t in targets if not t.startswith(("http://", "https://"))]
    selected = _select(url_venues, venues, [t for t in targets if t not in venues])
    fingerprints, state = _load_json(FINGERPRINTS_FILE), _load_json(CACHE_DIR / "state.json")
    moved = promote(fingerprints, state, selected, today)
    never_read = [u for u in selected if state.get(u, {}).get("failures") or not state.get(u, {}).get("sha256")]
    _write_json(FINGERPRINTS_FILE, {u: fp for u, fp in fingerprints.items() if u in url_venues})
    print(f"baseline advanced for {len(moved)} of {len(selected)} page(s)")
    for url in never_read:
        print(f"  not readable in the last check, left as is: {url}")
    return 0


def run_check(args, requirements: dict, url_venues: dict[str, list[str]], today: date) -> int:
    selected = _select(url_venues, args.venue, args.url)
    fingerprints, state = _load_json(FINGERPRINTS_FILE), _load_json(CACHE_DIR / "state.json")
    results: dict[str, dict] = {}

    browser = Browser(headless=args.headless, delay=args.delay)
    try:
        for i, url in enumerate(fetch_order(selected), 1):
            fetched = browser.fetch(url)
            baseline = fingerprints.get(url, {}).get("sha256")
            status = classify(fetched, baseline)
            result: dict = {"status": status, "venues": url_venues[url]}
            seen = state.setdefault(url, {})
            seen["checked"] = today.isoformat()
            if fetched.text is None:
                result["error"] = fetched.error
                seen["failures"] = seen.get("failures", 0) + 1
                seen["error"] = fetched.error
            else:
                seen.update(sha256=cache_text(fetched.text), chars=len(fetched.text), failures=0)
                seen.pop("error", None)
                result.update(sha256=seen["sha256"], chars=len(fetched.text))
                if status == "changed":
                    old = cached_text(baseline)
                    # Without the recorded text (fresh clone, cleared cache) there is
                    # nothing to diff against; the caller reads text_file in full.
                    result["diff"] = unified_diff(old, fetched.text, url) if old is not None else None
                    result["text_file"] = str(_text_path(seen["sha256"]).relative_to(REPO_ROOT))
            results[url] = result
            print(f"[{i}/{len(selected)}] {status:10s} {url}" + (f"  ({result['error']})" if "error" in result else ""), flush=True)
    finally:
        browser.close()

    if args.record_new:
        new = [u for u, r in results.items() if r["status"] == "new"]
        for url in promote(fingerprints, state, new, today):
            results[url]["status"] = "unchanged"
        _write_json(FINGERPRINTS_FILE, {u: fp for u, fp in fingerprints.items() if u in url_venues})
    _write_json(CACHE_DIR / "state.json", {u: s for u, s in state.items() if u in url_venues})
    prune_cache({fp["sha256"] for fp in fingerprints.values()} | {s["sha256"] for s in state.values() if s.get("sha256")})

    slugs = [s for s in requirements if not s.startswith("$") and any(s in url_venues[u] for u in selected)]
    report = {
        "checked": today.isoformat(),
        "venues": {s: venue_report(s, requirements[s], results, today, args.stale_days) for s in slugs},
        "urls": results,
    }
    report_path = Path(args.report) if args.report else CACHE_DIR / "report.json"
    _write_json(report_path, report)

    counts = {s: sum(r["status"] == s for r in results.values()) for s in ("unchanged", "changed", "new", "unreadable", "gone")}
    print("\n" + ", ".join(f"{n} {s}" for s, n in counts.items() if n))
    for slug, v in report["venues"].items():
        if v["needs_review"]:
            why = [f"{len(v[k])} {k}" for k in ("changed", "gone") if k in v] + (["stale"] if v["stale"] else [])
            print(f"  needs review: {slug} ({', '.join(why)})")
    if counts["new"]:
        print("  pages without a baseline: rerun with --record-new to record them")
    if args.diff:
        for url, r in results.items():
            if r.get("diff"):
                print("\n" + r["diff"])
    print(f"report: {report_path}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--venue", action="append", default=[], metavar="SLUG", help="check only this venue's pages (repeatable)")
    parser.add_argument("--url", action="append", default=[], help="check only this page (repeatable)")
    parser.add_argument("--record-new", action="store_true", help="record a baseline for every readable page that has none")
    parser.add_argument("--accept", nargs="+", metavar="SLUG_OR_URL", help="no fetch: make the text last seen for these venues/pages their baseline " "(run after updating their requirements entry)")
    parser.add_argument("--diff", action="store_true", help="print the diff of every changed page")
    parser.add_argument("--report", metavar="PATH", help="where to write the JSON report (default: .guideline_cache/report.json)")
    parser.add_argument("--headless", action="store_true", help="run the browser headless (more pages are refused as a bot); " "without a display the default headed browser is run under xvfb-run")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_S, help="seconds between two requests to the same host")
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS, help="age of `retrieved` past which a venue with unreadable pages is reported stale")
    args = parser.parse_args(argv)

    requirements = json.loads(REQUIREMENTS_FILE.read_text(encoding="utf-8"))
    url_venues = collect_urls(requirements)
    today = date.today()
    if args.accept:
        return run_accept(args.accept, requirements, url_venues, today)

    if not args.headless and not os.environ.get("DISPLAY") and sys.platform.startswith("linux"):
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            os.execv(xvfb, [xvfb, "-a", sys.executable, *sys.argv])  # nosec B606
        print("no display and no xvfb-run: falling back to --headless", file=sys.stderr)
        args.headless = True
    return run_check(args, requirements, url_venues, today)


if __name__ == "__main__":
    sys.exit(main())
