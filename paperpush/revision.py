"""Spot a revised manuscript, so ``submit`` can warn that it is for initial submissions.

A revision goes back through the venue's own "submit revision" link, not
through a new submission: ``paperpush submit`` would open a second, duplicate
submission. The paperpush-respond-to-reviewers skill prepares revisions in a
``revisionN/`` directory beside (or inside) the manuscript directory and writes
a response-to-reviewers document there; either is a sign the files belong to a
revision.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

REVISION_DIR = re.compile(r"^revision[_-]?\d+$", re.IGNORECASE)
# File stems of a response-to-reviewers / rebuttal document.
_RESPONSE_STEM = re.compile(r"(response|reply|responses|rebuttal)[\s_-]*(to)?[\s_-]*(the)?[\s_-]*(reviewer|referee|review|editor)|point[\s_-]*by[\s_-]*point|^rebuttal", re.IGNORECASE)
_DOC_SUFFIXES = {".pdf", ".docx", ".doc", ".tex", ".md", ".txt", ".odt", ".rtf"}


def is_response_document(path: Path) -> bool:
    """Whether ``path``'s name looks like a response to reviewers."""
    return path.suffix.lower() in _DOC_SUFFIXES and bool(_RESPONSE_STEM.search(path.stem))


def revision_signals(paths: Iterable[str | Path]) -> list[str]:
    """Reasons to think these files (a .sub and the files it names) are a revision.

    Looks for a ``revisionN`` directory among each path's parents and for a
    response-to-reviewers document among the paths or beside them. Returns one
    human-readable reason per sign found; an empty list means none.
    """
    reasons: list[str] = []
    seen_dirs: set[Path] = set()
    for raw in paths:
        if not raw:
            continue
        path = Path(raw).expanduser()
        try:
            path = path.resolve()
        except OSError:
            continue
        for parent in (path, *path.parents) if path.is_dir() else path.parents:
            if REVISION_DIR.match(parent.name):
                reason = f"{parent} is a revision directory"
                if reason not in reasons:
                    reasons.append(reason)
                break
        if path.is_file() and is_response_document(path):
            reason = f"{path} looks like a response to reviewers"
            if reason not in reasons:
                reasons.append(reason)
        folder = path if path.is_dir() else path.parent
        if folder in seen_dirs or not folder.is_dir():
            continue
        seen_dirs.add(folder)
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and is_response_document(entry):
                reason = f"{entry} looks like a response to reviewers"
                if reason not in reasons:
                    reasons.append(reason)
    return reasons


def subfile_revision_signals(subfile_path: str | Path, venue, values: dict[str, str]) -> list[str]:
    """:func:`revision_signals` for a .sub file and every file it names."""
    from .requirements_check import _paths

    paths: list[str | Path] = [subfile_path]
    for field in getattr(venue, "fields", []) or []:
        paths += _paths(field, values)
    return revision_signals(paths)
