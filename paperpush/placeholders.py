"""Leftovers from drafting: placeholders, to-dos, comments, and highlighting.

``details.placeholders`` reads the manuscript, title page, cover letter, and
supplementary files for the marks a draft carries and a submission should not:

* placeholder text in the prose -- ``XXX``/``xxx``, ``XX%``, ``n = X``, ``[ref]``,
  ``[citation needed]``, ``TBD``, ``Figure X``, ``(Smith et al., 20XX)``,
  ``[insert ...]``, ``Dear [Editor]``, *lorem ipsum*;
* to-do notes typeset into the document (``\\todo{}``, ``\\fixme{}``,
  ``\\missingfigure``, ``TODO:``/``FIXME:`` in the text);
* in a Word file, comments left in it and highlighted text.

LaTeX ``%`` comments are not read here: they never reach the PDF, and
``integrity.sensitive`` already reports the ones that carry notes. Unresolved
cross-references (``??``) are ``citations.cross_references``.
"""

from __future__ import annotations

import re
from pathlib import Path

from .database import Venue
from .validate import WARNING, Issue, NotApplicable, Outcome, Unchecked, unchecked

_UNITS = r"(?:%|‰|-?fold|mg|µg|μg|ng|kg|g|ml|mL|µl|μl|µL|μL|mM|µM|μM|nM|nm|µm|μm|mm|cm|kb|bp|°C|h|hr|hrs|min|s|days?|weeks?|months?|years?|participants|patients|subjects|samples|cells|mice|rats)"
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Numbers not filled in. "XX" alone is a karyotype, so it counts only beside a unit or an equals sign.
    # "XXX"/"xxx" standing alone (not in an e-mail address, URL, or word); "Xxx" is a gene name.
    ("placeholder text", re.compile(r"(?<![\w.@/-])(?<!\d,)(?<!Vol\. )(?<!Volume )(?:XXX+|xxx+)(?![\w@/]|-\d|\s+(?:International|Annual|Congress|Conference|Symposium|Meeting|Workshop|Colloquium|General\s+Assembly|syndrome|karyotype)\b)")),
    ("number placeholder", re.compile(rf"(?<![\w.])(?:[XY]\.[XY]+(?=\s*{_UNITS}\b|\s*[%)±]|\s*$)|(?:(?<=[=<>~]\s)|(?<=[=<>~])|(?<=was\s)|(?<=were\s)|(?<=is\s)|(?<=are\s)|(?<=mean\s)|(?<=median\s))[XY]\.[XY]+|XX(?=\s*{_UNITS}\b|\s*%))|(?<![\w])(?:[nNpP]|OR|HR|RR|CI)\s*[=<>]\s*(?:X+|\?+|#+)(?![\w])|\b(?:19|20)XX\b|\(\s*\?{2,}\s*\)")),
    ("citation placeholder", re.compile(r"\[\s*(?:refs?|references?|citations?(?: needed)?|cite|CITE|REF|REFS|\?+|#+)\s*\]|\((?:refs?|REF|cite|citation needed)\)|\bcitation needed\b", re.IGNORECASE)),
    ("figure or table placeholder", re.compile(r"\b(?:Supplementary\s+|Extended\s+Data\s+)?(?:Fig(?:ure)?s?\.?|Tables?)\s+(?:S?X{1,2}|#+|TBD)(?![\w])")),
    ("to-do note", re.compile(r"\b(?-i:TODO|FIXME|TBD|TBC(?!\s+(?:domains?|family|proteins?))|TKTK)\b|\b(?:todo|fixme)\s*:|\[\s*(?:insert|add|update|fill in|to be (?:added|completed|confirmed))\b[^\]]{0,60}\]|<\s*(?:insert|add)\b[^>]{0,60}>|\binsert (?:text|figure|table|reference|citation|name|date|here)\b", re.IGNORECASE)),
    ("template placeholder", re.compile(r"\[\s*(?:Editor(?:'s)? name|Journal name|Name of (?:the )?(?:editor|journal)|Author names?|Your name|Date|Title|Affiliation|Institution|Manuscript title|First Last|Name)\s*\]|\blorem ipsum\b", re.IGNORECASE)),
]
# "XX mice" beside "XY mice" is the karyotype, not a number left to fill in.
_XX_NOUN = re.compile(r"\s*(?:participants|patients|subjects|samples|cells|mice|rats)\b")
_KARYOTYPE = re.compile(r"(?<![\w.])XY(?![\w.])|karyotyp|sex\s+chromosome|\b4[5-9],\s*X", re.IGNORECASE)
_TEX_TODO = re.compile(r"\\(?:todo|fixme|missingfigure|TODO|FIXME)\b\s*(?:\[[^\]]*\])?\s*\{?")
_TEX_HIGHLIGHT = re.compile(r"\\(?:hl|colorbox\s*\{\s*yellow\s*\})\s*\{")
_TEX_TODO_PACKAGES = re.compile(r"\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^}]*\b(?:todonotes|fixme)\b")


def _files(venue: Venue, values: dict[str, str]) -> list[tuple[str, Path]]:
    from .requirements_check import _existing, _fields_for, _paths

    out: list[tuple[str, Path]] = []
    for role in ("manuscript", "title_page", "cover_letter", "supplementary"):
        for field in _fields_for(venue, role):
            for path in _existing(_paths(field, values)):
                if path.suffix.lower() in (".pdf", ".docx", ".tex", ".zip", ".md", ".txt", ".markdown") and (field.id, path) not in out:
                    out.append((field.id, path))
    return out


def _excerpt(text: str, start: int, end: int) -> str:
    left = text.rfind("\n", 0, start)
    a = max(left + 1, start - 40)
    right = text.find("\n", end)
    b = min(right if right != -1 else len(text), end + 40)
    return " ".join(text[a:b].split())


def _not_a_placeholder(text: str, m: re.Match[str]) -> bool:
    """Whether a hit is ordinary science: a karyotype, or an acronym the paper defines ("tick-borne disease (TBD)")."""
    if m.group(0) == "XX":
        return bool(_XX_NOUN.match(text, m.end()) and _KARYOTYPE.search(text))
    if m.group(0) in ("TBD", "TBC"):
        from .style_check import _expansion

        return any(_expansion(text[max(0, d.start() - 80) : d.start()], m.group(0)) for d in re.finditer(rf"\(\s*{m.group(0)}s?\s*\)", text))
    return False


def scan_text(text: str) -> list[tuple[str, str]]:
    """``(kind, description)`` for each kind of placeholder in ``text``: the first two hits and a count."""
    found: list[tuple[str, str]] = []
    for kind, pattern in _PATTERNS:
        hits = [m for m in pattern.finditer(text) if not _not_a_placeholder(text, m)]
        if not hits:
            continue
        shown = "; ".join(f"\"{' '.join(m.group(0).split())}\" in \"...{_excerpt(text, m.start(), m.end())}...\"" for m in hits[:2])
        found.append((kind, shown + (f" (and {len(hits) - 2} more)" if len(hits) > 2 else "")))
    return found


def _word_extras(doc) -> list[str]:
    """Comments and highlighting left in a Word file."""
    out: list[str] = []
    comments = doc.docx_part("word/comments.xml") or ""
    count = len(re.findall(r"<w:comment\b", comments))
    if count:
        first = re.search(r"<w:comment\b[^>]*>(.*?)</w:comment>", comments, re.DOTALL)
        said = " ".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", first.group(1))) if first else ""
        out.append(f"{count} comment(s) left in the file" + (f' (first: "{said[:60]}")' if said else ""))
    body = doc.docx_part("word/document.xml") or ""
    marks = re.findall(r"<w:r\b(?:(?!</w:r>).)*?<w:(?:highlight\s+w:val=\"(?!none)\w+\"|shd\b[^>]*w:fill=\"(?!auto|FFFFFF|ffffff)[0-9A-Fa-f]{6}\")(?:(?!</w:r>).)*?</w:r>", body, re.DOTALL)
    if marks:
        sample = " ".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", marks[0]))
        out.append(f"{len(marks)} highlighted run(s) of text" + (f' (first: "{sample[:60]}")' if sample.strip() else ""))
    return out


def check(venue: Venue, values: dict[str, str]) -> Outcome:
    from . import document

    files = _files(venue, values)
    if not files:
        raise NotApplicable("no manuscript, title page, cover letter, or supplementary text file to read")
    issues: list[Issue] = []
    unread: list[tuple[str, str]] = []
    for field_id, path in files:
        doc = document.get(path)
        found: list[tuple[str, str]] = []
        if doc.is_tex and doc.tex_source is not None:
            source = doc.tex_source
            if _TEX_TODO_PACKAGES.search(source):
                found.append(("to-do note", "the todonotes/fixme package is loaded"))
            for kind, pattern in (("to-do note", _TEX_TODO), ("highlighted text", _TEX_HIGHLIGHT)):
                hits = list(pattern.finditer(source))
                if hits:
                    shown = "; ".join(f'"{_excerpt(source, m.start(), m.end() + 50)}"' for m in hits[:2])
                    found.append((kind, shown + (f" (and {len(hits) - 2} more)" if len(hits) > 2 else "")))
        text = doc.text
        extras = _word_extras(doc) if doc.is_docx else []
        if not (text or "").strip() and not found and not extras:  # unreadable, or a scanned PDF with no text
            unread.append((field_id, path.name))
            continue
        found.extend(scan_text(text or ""))
        found.extend(("Word leftovers", extra) for extra in extras)
        for kind, excerpt in found:
            issues.append(Issue(WARNING, field_id, f"{path.name}: {kind}: {excerpt}" if kind != "Word leftovers" else f"{path.name}: {excerpt}; remove before submission"))
    names = ", ".join(name for _, name in unread)
    if len(unread) == len(files):
        raise Unchecked(f"could not read any text from {names}")
    issues.extend(unchecked(field_id, f"could not read any text from {name}; its placeholders were not checked") for field_id, name in unread)
    detail = f"read {len(files) - len(unread)} file(s)" + (f"; could not read {names}" if unread else "")
    return Outcome(issues, detail)
