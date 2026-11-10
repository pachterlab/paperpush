"""Sort a folder of submission files into a venue's upload fields, without AI.

Given the paths of the files (as they sat in the author's folder or zip, so a
``figures/`` directory still says something), :func:`match_files` decides
which upload field each belongs in: manuscript, cover letter, figures, tables,
supplement, and so on.

Every path is split into words (on punctuation, case changes, and digits:
``SuppFig1.tif`` -> ``supp``, ``fig``, ``1``), and each word -- and each pair
of neighbouring words run together, for ``cover_letter`` -> ``coverletter``
-- is compared with a standard list of names per kind of document, allowing
a small edit distance so ``suplementary`` or ``Manuscirpt`` still count.
Kinds are tried in a fixed order (a *supplementary figure* is a supplement,
not a figure), and a file goes to the first kind whose field exists in the
venue and accepts its extension. Images with no telling name are figures;
a lone unclaimed document in a format the manuscript field accepts is the
manuscript.

The result is a suggestion: the website fills only fields that are still
empty, and the author sees what went where.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional

from .database import Venue

# Standard names per kind of document, lower case, no separators.
KEYWORDS: dict[str, tuple[str, ...]] = {
    "title_page": ("titlepage",),
    "cover_letter": ("cover", "coverletter", "letter", "lettertoeditor", "editorletter"),
    "declaration": ("declaration", "declarations", "competing", "interests", "conflict", "conflicts", "coi", "disclosure", "disclosures", "declarationofinterest"),
    "supplement": ("supplement", "supplementary", "supplemental", "supp", "suppl", "supps", "si", "sm", "esm", "appendix", "appendices", "additional", "extended", "suppinfo", "supportinginformation"),
    "table": ("table", "tables", "tab", "tbl"),
    "figure": ("figure", "figures", "fig", "figs", "image", "images", "img"),
    "combined": ("combined", "merged", "complete", "full", "combinedpdf"),
    "source": ("source", "sources", "src", "latex", "latexsource"),
    "manuscript": ("manuscript", "main", "maintext", "paper", "article", "ms", "draft", "preprint", "mainmanuscript"),
}

# Precedence when a path matches several kinds.
ORDER = ("title_page", "cover_letter", "declaration", "supp_table", "supplement", "table", "figure", "combined", "source", "manuscript")

KIND_LABELS = {
    "title_page": "a title page",
    "cover_letter": "a cover letter",
    "declaration": "a declaration of interests",
    "supp_table": "a supplementary table",
    "supplement": "supplementary material",
    "table": "a table",
    "figure": "a figure",
    "combined": "a combined PDF",
    "source": "source files",
    "manuscript": "the manuscript",
}

# Upload fields per kind, in preference order.
TARGETS: dict[str, tuple[str, ...]] = {
    "title_page": ("title_page_file", "title_page"),
    "manuscript": ("manuscript_file", "pdf_file", "manuscript", "combined_pdf"),
    "combined": ("combined_pdf",),
    "cover_letter": ("cover_letter", "cover_letter_file"),
    "declaration": ("declaration_file",),
    "figure": ("figure_files", "figures"),
    "table": ("table_files", "tables"),
    "supp_table": ("supplementary_tables", "supplementary_files", "supplementary_material", "supplementary_file", "supplement", "additional_files"),
    "supplement": ("supplementary_files", "supplementary_material", "supplementary_file", "supplement", "additional_files"),
    "source": ("latex_source",),
}

IMAGE_EXTS = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".eps", ".svg", ".ai", ".psd", ".gif", ".bmp", ".cdx"}
DOCUMENT_EXTS = {".pdf", ".docx", ".doc", ".tex", ".odt", ".rtf"}

_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


_EXACT = {"letter", "table", "tables", "cover"}

# ``FigS1``, ``Table_S2``, ``S3_Fig``: a capital S and a number mean a supplementary item.
_SUPP_NUMBERED = re.compile(r"(?i:fig(?:ure)?s?|tab(?:le)?s?|tbl)[\s_.-]*S\d|(?<![A-Za-z])S\d+[\s_.-]*(?i:fig|tab)")


def edit_distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance between ``a`` and ``b``, or ``limit + 1`` once it exceeds ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def _tolerance(keyword: str) -> int:
    """Typos allowed: none for short names (``fig``, ``si``), one from 5 letters, two from 9.

    None either for the names one letter from everyday words (``better``, ``stable``, ``lover``).
    """
    if keyword in _EXACT:
        return 0
    return 0 if len(keyword) < 5 else 1 if len(keyword) < 9 else 2


def _part_words(part: str) -> list[str]:
    ws = [w.lower() for w in _WORD.findall(part) if not w.isdigit()]
    return ws + [a + b for a, b in zip(ws, ws[1:])] + [a + b + c for a, b, c in zip(ws, ws[1:], ws[2:])]


def _kinds_in(ws: list[str]) -> set[str]:
    found = set()
    for w in ws:
        for kind, names in KEYWORDS.items():
            if any(edit_distance(w, k, _tolerance(k)) <= _tolerance(k) for k in names):
                found.add(kind)
    if "title_page" in found:
        return {"title_page"}
    if "supplement" in found and "table" in found:
        found.add("supp_table")
    return found


# A folder named ``paper/`` or ``manuscript/`` usually holds everything, so
# these kinds are read from file names only.
_NAME_ONLY = {"manuscript", "combined"}


def kinds_of(path: str) -> list[str]:
    """Every kind ``path`` points to, most specific first (see :data:`ORDER`).

    The file name decides; only when it says nothing do the folders it sits
    in, innermost first (``figures/a.pdf`` is a figure).
    """
    p = PurePosixPath(path.replace("\\", "/"))
    found = _kinds_in(_part_words(p.stem))
    if _SUPP_NUMBERED.search(p.stem):
        found.add("supplement")
        if "table" in found:
            found.add("supp_table")
    if not found:
        for folder in reversed(p.parts[:-1]):
            found = _kinds_in(_part_words(folder)) - _NAME_ONLY
            if found:
                break
    return [k for k in ORDER if k in found]


def _natural(name: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


@dataclass
class Match:
    name: str  # bare file name, as stored and as written in the .sub
    path: str  # where it was in the folder / zip
    field: Optional[str] = None
    kind: Optional[str] = None
    reason: str = ""


@dataclass
class MatchResult:
    assignments: dict[str, str] = field(default_factory=dict)  # field id -> .sub value
    matches: list[Match] = field(default_factory=list)

    def to_dict(self, venue: Venue) -> dict:
        labels = {f.id: f.label for f in venue.fields}
        return {
            "assignments": self.assignments,
            "matched": [{"name": m.name, "path": m.path, "field": m.field, "label": labels.get(m.field or "", ""), "kind": m.kind} for m in self.matches if m.field],
            "unmatched": [{"name": m.name, "path": m.path, "reason": m.reason} for m in self.matches if not m.field],
        }


def _accepts(f, name: str) -> bool:
    return not f.accept or name.lower().endswith(tuple(a.lower() for a in f.accept))


def match_files(venue: Venue, paths: list[str]) -> MatchResult:
    """Assign each of ``paths`` to one of ``venue``'s upload fields, where its name says it belongs."""
    upload = {f.id: f for f in venue.fields if f.type in ("file", "filelist")}
    taken: dict[str, list[Match]] = {}
    result = MatchResult()
    leftovers: list[Match] = []

    def place(m: Match, kinds: list[str]) -> bool:
        for kind in kinds:
            for fid in TARGETS.get(kind, ()):
                f = upload.get(fid)
                if f is None or not _accepts(f, m.name):
                    continue
                if f.type == "file" and taken.get(fid):
                    continue
                m.field, m.kind = fid, kind
                taken.setdefault(fid, []).append(m)
                return True
        return False

    for path in sorted(paths, key=_natural):
        name = PurePosixPath(path.replace("\\", "/")).name
        m = Match(name=name, path=path)
        result.matches.append(m)
        kinds = kinds_of(path)
        ext = PurePosixPath(name).suffix.lower()
        if not kinds and ext in IMAGE_EXTS:
            kinds = ["figure"]
        if kinds and place(m, kinds):
            continue
        if kinds:
            m.reason = f"looks like {KIND_LABELS[kinds[0]]}, but no {venue.name} upload field for it is free and takes {ext or 'files without an extension'}"
        else:
            m.reason = "the name does not say what it is"
            if ext in DOCUMENT_EXTS:
                leftovers.append(m)

    # A lone unnamed document is the manuscript, if that field is still free.
    if len(leftovers) == 1 and place(leftovers[0], ["manuscript"]):
        leftovers[0].reason = ""

    for fid, ms in taken.items():
        names = [m.name for m in ms]
        result.assignments[fid] = "\n".join(names) if upload[fid].type == "filelist" else names[0]
    return result
