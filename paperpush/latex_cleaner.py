"""Preview what ``arxiv_latex_cleaner`` would change in a submission's LaTeX.

Backs the ``integrity.latex_cleaner`` check. arXiv publishes the uploaded
source as-is, so comments, unused files, and build junk go public with it.
This module copies every LaTeX upload -- a ``.tex`` file, or a ``.zip`` /
``.tar`` / ``.tar.gz`` / ``.tgz`` bundle containing one -- into a temporary
directory, runs Google's
`arxiv_latex_cleaner <https://github.com/google-research/arxiv-latex-cleaner>`_
on the copy, and reports how the result differs from the original.

``validate`` never modifies the author's files, so nothing here writes outside
the temporary directory. The ``paperpush-fix`` skill is what applies the
cleaner, after reviewing its output: the tool can drop a figure whose path a
macro builds, and can cut a ``%`` inside ``verbatim``.

arxiv_latex_cleaner is optional (``pip install 'paperpush[validate]'``) and is
run in a subprocess (``python -m arxiv_latex_cleaner``), so its CLI -- the
stable interface across its releases -- is all this relies on.
"""

from __future__ import annotations

import difflib
import importlib.util
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

INSTALL_HINT = "pip install 'paperpush[validate]'"

# Source-bundle suffixes, longest first so ".tar.gz" beats ".gz".
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")

# This check runs by default, so a pathological source must not hang validate
# or fill the disk.
_TIMEOUT_SECONDS = 120
_MAX_BUNDLE_BYTES = 1024**3

_MAX_FILES_LISTED = 5
_MAX_RANGES_LISTED = 8


class CleanerError(RuntimeError):
    """arxiv_latex_cleaner could not produce a cleaned copy of a source."""


@dataclass
class Preview:
    """What the cleaner would do to one upload."""

    source: Path
    # File (relative to the source) -> the original line numbers it would change.
    changed: dict[str, list[int]] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.changed or self.removed)

    def describe(self) -> str:
        """One sentence of counts, file names, and line numbers (never the comment text)."""
        parts = []
        if self.changed:
            lines = sum(len(numbers) for numbers in self.changed.values())
            where = [f"{name}: {_line_ranges(numbers)}" for name, numbers in sorted(self.changed.items())]
            parts.append(f"change {lines} line(s) in {len(self.changed)} file(s) ({_listed(where)})")
        if self.removed:
            parts.append(f"remove {len(self.removed)} file(s) ({_listed(self.removed)})")
        return f"{self.source.name}: arxiv_latex_cleaner would {' and '.join(parts)}"


def _listed(items: list[str]) -> str:
    shown = "; ".join(items[:_MAX_FILES_LISTED])
    extra = len(items) - _MAX_FILES_LISTED
    return f"{shown}; and {extra} more" if extra > 0 else shown


def _line_ranges(numbers: list[int]) -> str:
    """``[4, 6, 10, 11, 12]`` as ``lines 4, 6, 10-12``."""
    ranges: list[tuple[int, int]] = []
    for number in numbers:
        if ranges and number == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], number)
        else:
            ranges.append((number, number))
    shown = ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges[:_MAX_RANGES_LISTED])
    if len(ranges) > _MAX_RANGES_LISTED:
        shown += ", ..."
    return f"line {shown}" if len(numbers) == 1 else f"lines {shown}"


def available() -> bool:
    """True when the optional arxiv_latex_cleaner package is importable."""
    return importlib.util.find_spec("arxiv_latex_cleaner") is not None


def _archive_suffix(path: Path) -> str | None:
    name = path.name.lower()
    return next((s for s in _ARCHIVE_SUFFIXES if name.endswith(s)), None)


def _archive_has_tex(path: Path) -> bool:
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                return any(n.lower().endswith(".tex") for n in zf.namelist())
        if tarfile.is_tarfile(path):
            with tarfile.open(path) as tf:
                return any(m.isfile() and m.name.lower().endswith(".tex") for m in tf.getmembers())
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        logger.debug("could not list %s", path, exc_info=True)
    return False


def is_latex_source(path: Path) -> bool:
    """True for a ``.tex`` file or a zip/tar bundle that contains one."""
    if not path.is_file():
        return False
    if path.suffix.lower() == ".tex":
        return True
    return _archive_suffix(path) is not None and _archive_has_tex(path)


def _run_cleaner(folder: Path) -> Path:
    """Run the cleaner on ``folder``; return its ``<folder>_arXiv`` output.

    ``--keep_bib``: the reference checks read the ``.bib``, and a source without
    a compiled ``.bbl`` needs it to build. The working directory is empty
    because ``python -m`` imports from it, and bundles are the author's files.
    """
    cmd = [sys.executable, "-m", "arxiv_latex_cleaner", str(folder), "--keep_bib"]
    logger.info("Running %s", " ".join(cmd))
    try:
        with tempfile.TemporaryDirectory(prefix="paperpush-alc-cwd-") as empty:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=_TIMEOUT_SECONDS, cwd=empty)  # nosec B603 -- fixed argv, no shell
    except subprocess.TimeoutExpired as exc:
        raise CleanerError(f"arxiv_latex_cleaner did not finish within {_TIMEOUT_SECONDS} seconds") from exc
    out = Path(str(folder) + "_arXiv")
    if proc.returncode != 0 or not out.is_dir():
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise CleanerError(detail[-1] if detail else f"arxiv_latex_cleaner exited with status {proc.returncode}")
    return out


def _extract(archive: Path, dest: Path) -> None:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            if sum(i.file_size for i in zf.infolist()) > _MAX_BUNDLE_BYTES:
                raise CleanerError("the bundle is too large to clean for a preview")
            zf.extractall(dest)  # nosec B202 -- zipfile strips absolute paths and ".."
        return
    with tarfile.open(archive) as tf:
        if sum(m.size for m in tf.getmembers()) > _MAX_BUNDLE_BYTES:
            raise CleanerError("the bundle is too large to clean for a preview")
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")  # nosec B202 -- "data" filter rejects unsafe members
            return
        root = dest.resolve()
        members = [m for m in tf.getmembers() if (m.isfile() or m.isdir()) and (root / m.name).resolve().is_relative_to(root)]
        tf.extractall(dest, members=members)  # nosec B202 -- members vetted above


def _lines(data: bytes) -> list[str]:
    """Lines to compare: the cleaner rewrites line endings and the final newline even in a clean file."""
    lines = [line.rstrip() for line in data.decode("utf-8", errors="replace").splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _changed_lines(before: bytes, after: bytes) -> list[int]:
    """The 1-based line numbers of ``before`` that differ in ``after``."""
    a, b = _lines(before), _lines(after)
    if a == b:
        return []
    numbers: list[int] = []
    for tag, i1, i2, _, _ in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag != "equal":
            numbers.extend(range(i1 + 1, i2 + 1))
    return numbers


def _compare(source: Path, before: Path, after: Path) -> Preview:
    preview = Preview(source)
    for path in sorted(p for p in before.rglob("*") if p.is_file()):
        relative = path.relative_to(before)
        cleaned = after / relative
        if not cleaned.is_file():
            preview.removed.append(relative.as_posix())
            continue
        original, new = path.read_bytes(), cleaned.read_bytes()
        if original != new:
            numbers = _changed_lines(original, new)
            if numbers:
                preview.changed[relative.as_posix()] = numbers
    return preview


def _preview_bundle(archive: Path) -> Preview:
    with tempfile.TemporaryDirectory(prefix="paperpush-alc-") as tmp:
        before = Path(tmp) / "source"
        before.mkdir()
        _extract(archive, before)
        return _compare(archive, before, _run_cleaner(before))


def _preview_loose(texs: list[Path]) -> list[Preview]:
    """Preview loose ``.tex`` uploads, one :class:`Preview` for each that would change.

    Only the ``.tex`` uploads are copied, under one root so that a ``\\input``
    between them still resolves. Whatever else sits in their directory is not
    uploaded with them, so it is not the cleaner's business here: it would
    report a compiled ``paper.pdf`` beside the source as an unused file.
    """
    root = Path(os.path.commonpath([str(p.parent) for p in texs]))
    with tempfile.TemporaryDirectory(prefix="paperpush-alc-") as tmp:
        before = Path(tmp) / "source"
        for tex in texs:
            target = before / tex.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tex, target)
        whole = _compare(root, before, _run_cleaner(before))
    previews = []
    for tex in texs:
        name = tex.relative_to(root).as_posix()
        preview = Preview(tex, {tex.name: whole.changed[name]} if name in whole.changed else {}, [tex.name] if name in whole.removed else [])
        if preview:
            previews.append(preview)
    return previews


def preview(paths: Iterable[Path]) -> tuple[list[Preview], list[tuple[Path, str]]]:
    """Report what the cleaner would change in each LaTeX upload among ``paths``.

    Returns ``(previews, failures)``: a :class:`Preview` for each upload that
    would change, and ``(path, reason)`` for each the cleaner could not handle.
    Only temporary copies are cleaned; ``paths`` are left as they are.
    """
    unique = list(dict.fromkeys(Path(p).expanduser().resolve() for p in paths))
    sources = [p for p in unique if is_latex_source(p)]
    texs = [p for p in sources if p.suffix.lower() == ".tex"]
    previews: list[Preview] = []
    failures: list[tuple[Path, str]] = []
    errors = (CleanerError, OSError, tarfile.TarError, zipfile.BadZipFile)
    if texs:
        try:
            previews.extend(_preview_loose(texs))
        except errors as exc:
            logger.debug("previewing %s failed", texs, exc_info=True)
            failures.extend((tex, str(exc)) for tex in texs)
    for bundle in (p for p in sources if p.suffix.lower() != ".tex"):
        try:
            result = _preview_bundle(bundle)
        except errors as exc:
            logger.debug("previewing %s failed", bundle, exc_info=True)
            failures.append((bundle, str(exc)))
            continue
        if result:
            previews.append(result)
    return previews, failures
