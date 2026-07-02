"""Structural fingerprints of a portal page, for change detection.

The per-venue runners (:mod:`paperpush.venues.biorxiv.main`,
:mod:`paperpush.venues.scholarone.bioinformatics`) bet on a fixed set of
selectors. A
contract check (does each selector our code uses still resolve?) catches a break
in something we already click, but it is blind to a portal *adding* a required
field we do not yet handle. A fingerprint closes that gap: it captures the
stable structure of a page -- the form-control names, button labels, field
labels, and headings -- so the drift test can diff the live page against a
committed baseline and flag anything that appeared or disappeared.

Why not diff the raw HTML? These portals (ScholarOne especially) embed
per-session ``PARAMS=`` tokens, CSRF fields, and timestamps that change on every
load, so a byte/HTML comparison reports a change every single time. The
fingerprint deliberately drops those volatile parts: hidden inputs are excluded
(that is where the tokens live) and only human-meaningful, structural text is
kept. What is left is stable across loads but moves when the portal actually
restyles a page.

The functions here are pure (no browser launch, no disk IO): :func:`fingerprint`
takes an already-open Playwright ``page`` and returns a plain dict, and
:func:`diff` compares two such dicts. Baseline storage lives with the test that
owns the baselines, not here.
"""

from __future__ import annotations

import re

# Categories collected for each page, in a stable order. Each maps to a sorted,
# de-duplicated list of strings, so two fingerprints of the same page compare
# equal regardless of DOM order.
CATEGORIES = ("fields", "buttons", "labels", "headings")

# Form-control names/ids that carry a per-session token rather than identifying a
# real field. Matched case-insensitively as a substring and dropped from the
# ``fields`` category so a fresh token never reads as a page change. Hidden
# inputs are already excluded wholesale (see the script below); this also catches
# visible-but-volatile controls.
_VOLATILE_FIELD = re.compile(r"csrf|xsrf|token|nonce|viewstate|params|session|captcha|__RequestVerification", re.IGNORECASE)

# Collected entirely in the page so each category is one round-trip rather than
# one per element. Returns the four lists; Python normalizes/sorts them.
#
#   fields   -- visible input/select/textarea, keyed by `name` (else `id`).
#               type=hidden is skipped: that is where CSRF/session tokens live.
#   buttons  -- <button>, input[type=submit|button|reset], and role=button,
#               by their visible text (or value).
#   labels   -- <label> text; the questions a form asks, which is exactly what a
#               portal changes when it adds or renames a field.
#   headings -- h1..h3 text; the page's section structure.
_EXTRACT_JS = r"""
() => {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const visible = (el) => {
    if (!el) return false;
    if (el.type === "hidden") return false;
    const s = window.getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden") return false;
    return true;
  };
  const fields = [];
  for (const el of document.querySelectorAll("input, select, textarea")) {
    if (!visible(el)) continue;
    const key = el.getAttribute("name") || el.getAttribute("id");
    if (key) fields.push(key);
  }
  const buttons = [];
  for (const el of document.querySelectorAll(
      "button, input[type=submit], input[type=button], input[type=reset], [role=button]")) {
    if (!visible(el)) continue;
    const text = clean(el.innerText || el.textContent || el.value || el.getAttribute("aria-label"));
    if (text) buttons.push(text);
  }
  const labels = [];
  for (const el of document.querySelectorAll("label")) {
    if (!visible(el)) continue;
    const text = clean(el.innerText || el.textContent);
    if (text) labels.push(text);
  }
  const headings = [];
  for (const el of document.querySelectorAll("h1, h2, h3")) {
    if (!visible(el)) continue;
    const text = clean(el.innerText || el.textContent);
    if (text) headings.push(text);
  }
  return { fields, buttons, labels, headings };
}
"""


def _normalize(values: list[str], *, drop_volatile: bool = False) -> list[str]:
    """Collapse whitespace, drop empties (and volatile field keys), sort, dedup."""
    out = set()
    for raw in values:
        text = " ".join((raw or "").split())
        if not text:
            continue
        if drop_volatile and _VOLATILE_FIELD.search(text):
            continue
        out.add(text)
    return sorted(out)


def fingerprint(page) -> dict[str, list[str]]:
    """Return a structural fingerprint of the currently-loaded ``page``.

    The result is a JSON-serializable dict with the keys in :data:`CATEGORIES`,
    each a sorted, de-duplicated list of strings. Stable across reloads of an
    unchanged page (volatile tokens are excluded), so a later run can diff
    against it with :func:`diff`.
    """
    raw = page.evaluate(_EXTRACT_JS)
    return {
        "fields": _normalize(raw.get("fields", []), drop_volatile=True),
        "buttons": _normalize(raw.get("buttons", [])),
        "labels": _normalize(raw.get("labels", [])),
        "headings": _normalize(raw.get("headings", [])),
    }


def diff(baseline: dict[str, list[str]], current: dict[str, list[str]]) -> dict[str, dict[str, list[str]]]:
    """Compare two fingerprints, returning what each category gained/lost.

    The result maps each changed category to ``{"added": [...], "removed":
    [...]}`` (entries present only in ``current`` / only in ``baseline``).
    Unchanged categories are omitted, so an empty dict means the page matches its
    baseline.
    """
    out: dict[str, dict[str, list[str]]] = {}
    for cat in CATEGORIES:
        base = set(baseline.get(cat, []))
        cur = set(current.get(cat, []))
        added = sorted(cur - base)
        removed = sorted(base - cur)
        if added or removed:
            out[cat] = {"added": added, "removed": removed}
    return out


def format_diff(page_diff: dict[str, dict[str, list[str]]]) -> str:
    """Render a :func:`diff` result as a short, human-readable report."""
    lines: list[str] = []
    for cat, change in page_diff.items():
        for item in change.get("removed", []):
            lines.append(f"  - [{cat}] removed: {item!r}")
        for item in change.get("added", []):
            lines.append(f"  + [{cat}] added:   {item!r}")
    return "\n".join(lines)
