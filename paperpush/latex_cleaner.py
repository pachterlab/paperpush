"""Run ``arxiv_latex_cleaner`` over a submission's LaTeX source.

Backs ``paperpush validate --arxiv-latex-cleaner``. arXiv (and any venue that
takes LaTeX source) publishes the uploaded source as-is, so comments, unused
files, and build junk go public with it. This module finds every ``file`` /
``filelist`` value that is LaTeX source -- a ``.tex`` file, or a ``.zip`` /
``.tar`` / ``.tar.gz`` / ``.tgz`` bundle containing one -- runs Google's
`arxiv_latex_cleaner <https://github.com/google-research/arxiv-latex-cleaner>`_
on it, and returns the values rewritten to point at the cleaned copies, so the
rest of ``validate`` checks what would actually be uploaded.

The originals are never modified. Cleaned copies are written next to them,
following the cleaner's own naming:

* ``paper/main.tex``  -> ``paper_arXiv/main.tex`` (the cleaner runs on the
  ``.tex`` file's whole directory);
* ``source.zip``      -> ``source_arXiv.zip`` (likewise ``.tar``, ``.tar.gz``,
  ``.tgz``, repacked in the original format).

An existing ``*_arXiv`` output is replaced, as the cleaner itself does.

arxiv_latex_cleaner is optional (``pip install 'paperpush[validate]'``) and is
run in a subprocess (``python -m arxiv_latex_cleaner``), so its CLI -- the
stable interface across its releases -- is all this relies on.
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .schema_models import Venue

logger = logging.getLogger(__name__)

INSTALL_HINT = "pip install 'paperpush[validate]'"

# Source-bundle suffixes the cleaner's output can be repacked into, mapped to
# the shutil.make_archive format. Longest first so ".tar.gz" beats ".gz".
_ARCHIVE_FORMATS = {".tar.gz": "gztar", ".tgz": "gztar", ".tar": "tar", ".zip": "zip"}


class CleanerError(RuntimeError):
    """arxiv_latex_cleaner could not produce a cleaned copy of a source."""


@dataclass
class Cleaned:
    """One upload replaced by its cleaned copy."""

    field_id: str
    original: Path
    cleaned: Path


def available() -> bool:
    """True when the optional arxiv_latex_cleaner package is importable."""
    return importlib.util.find_spec("arxiv_latex_cleaner") is not None


def _archive_suffix(path: Path) -> str | None:
    name = path.name.lower()
    return next((s for s in _ARCHIVE_FORMATS if name.endswith(s)), None)


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
    """Run the cleaner on ``folder``; return its ``<folder>_arXiv`` output."""
    cmd = [sys.executable, "-m", "arxiv_latex_cleaner", str(folder)]
    logger.info("Running %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)  # nosec B603 -- fixed argv, no shell
    out = Path(str(folder.resolve()) + "_arXiv")
    if proc.returncode != 0 or not out.is_dir():
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise CleanerError(detail[-1] if detail else f"arxiv_latex_cleaner exited with status {proc.returncode}")
    return out


def _extract(archive: Path, dest: Path) -> None:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)  # nosec B202 -- zipfile strips absolute paths and ".."
        return
    with tarfile.open(archive) as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")  # nosec B202 -- "data" filter rejects unsafe members
            return
        root = dest.resolve()
        members = [m for m in tf.getmembers() if (m.isfile() or m.isdir()) and (root / m.name).resolve().is_relative_to(root)]
        tf.extractall(dest, members=members)  # nosec B202 -- members vetted above


def _clean_archive(archive: Path) -> Path:
    suffix = _archive_suffix(archive)
    assert suffix is not None
    stem = archive.name[: -len(suffix)]
    target = archive.with_name(f"{stem}_arXiv{suffix}")
    with tempfile.TemporaryDirectory(prefix="paperpush-alc-") as tmp:
        src = Path(tmp) / stem
        src.mkdir()
        _extract(archive, src)
        out = _run_cleaner(src)
        packed = shutil.make_archive(str(Path(tmp) / "packed"), _ARCHIVE_FORMATS[suffix], root_dir=out)
        shutil.move(packed, target)
    return target


def _clean_tex(tex: Path, done: dict[Path, Path]) -> Path:
    folder = tex.parent.resolve()
    if folder not in done:
        done[folder] = _run_cleaner(folder)
    cleaned = done[folder] / tex.name
    if not cleaned.is_file():
        raise CleanerError(f"the cleaner's output {done[folder]} has no {tex.name}")
    return cleaned


def clean_values(venue: Venue, values: dict[str, str]) -> tuple[dict[str, str], list[Cleaned], list[tuple[str, Path, str]]]:
    """Clean every LaTeX upload named in ``values``.

    Returns ``(new_values, cleaned, failures)``: ``values`` with each cleaned
    path swapped for its copy (a ``filelist`` line keeps its ``| ...``
    subfields), the substitutions made, and ``(field_id, path, reason)`` for
    each source the cleaner could not handle (left pointing at the original).
    """
    new_values = dict(values)
    cleaned: list[Cleaned] = []
    failures: list[tuple[str, Path, str]] = []
    done_dirs: dict[Path, Path] = {}
    done_archives: dict[Path, Path] = {}

    def swap(field_id: str, raw_path: str) -> str:
        path = Path(raw_path).expanduser()
        if not is_latex_source(path):
            return raw_path
        try:
            if path.suffix.lower() == ".tex":
                out = _clean_tex(path, done_dirs)
            else:
                key = path.resolve()
                if key not in done_archives:
                    done_archives[key] = _clean_archive(path)
                out = done_archives[key]
        except (CleanerError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            logger.debug("cleaning %s failed", path, exc_info=True)
            failures.append((field_id, path, str(exc)))
            return raw_path
        cleaned.append(Cleaned(field_id, path, out))
        return str(out)

    for field in venue.fields:
        raw = values.get(field.id, "")
        if not raw.strip():
            continue
        if field.type == "file":
            new_values[field.id] = swap(field.id, raw.strip())
        elif field.type == "filelist":
            lines = []
            for line in raw.splitlines():
                path_part, sep, rest = line.partition("|")
                if path_part.strip():
                    new_path = swap(field.id, path_part.strip())
                    line = f"{new_path} {sep}{rest}" if sep else new_path
                lines.append(line)
            new_values[field.id] = "\n".join(lines)
    return new_values, cleaned, failures
