"""One cached view of an uploaded document, shared by the manuscript checks.

The layout, figure, citation, and consistency checks all read the same
manuscript in the same few ways: its text, its per-page text, its positioned
glyphs and images (for a PDF, or a LaTeX source compiled to one), its LaTeX
source, or the XML parts of a Word file. :func:`get` returns a
:class:`Document` that computes each of these once, on first use, and is
itself cached per file (path, size, modification time) for the process.

:func:`normalize` puts extracted text into a comparable form: ligatures
expanded, typographic dashes and quotes made plain, words hyphenated across a
line break rejoined.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import zipfile
from functools import cached_property, lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

TEX_SUFFIXES = (".tex", ".zip")

_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
_PUNCT = {"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-", "‘": "'", "’": "'", "“": '"', "”": '"', " ": " ", " ": " ", " ": " ", "­": ""}


def normalize(text: str) -> str:
    """Extracted text made comparable: ligatures, dashes, quotes, and line-break hyphens undone."""
    for src, dst in _LIGATURES.items():
        text = text.replace(src, dst)
    for src, dst in _PUNCT.items():
        text = text.replace(src, dst)
    # "experi-\nment" -> "experiment" (a lowercase letter on both sides).
    text = re.sub(r"([a-z])-\s*\n\s*([a-z])", r"\1\2", text)
    return text


def fold(text: str) -> str:
    """Lower-cased, accent-free text: for matching names and words."""
    text = unicodedata.normalize("NFKD", normalize(text))
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def tokens(text: str) -> list[str]:
    """The words of ``text``, folded, with punctuation dropped."""
    return re.findall(r"[a-z0-9]+", fold(text))


class Document:
    """Lazy, cached access to the ways the checks read one file."""

    def __init__(self, path: Path):
        self.path = path
        self.suffix = path.suffix.lower()

    @property
    def is_tex(self) -> bool:
        return self.suffix in TEX_SUFFIXES

    @property
    def is_pdf(self) -> bool:
        return self.suffix == ".pdf"

    @property
    def is_docx(self) -> bool:
        return self.suffix == ".docx"

    @cached_property
    def pdf(self) -> Path | None:
        """The PDF to measure: the file itself, a compiled LaTeX source, or None."""
        from . import manuscript

        return manuscript._as_pdf(self.path)

    @cached_property
    def page_texts(self) -> list[str] | None:
        from . import manuscript

        if self.pdf is None:
            return None
        pages = manuscript._pdf_pages(self.pdf)
        return None if pages is None else [normalize(p) for p in pages]

    @cached_property
    def text(self) -> str | None:
        """The whole document's text, references and legends included.

        A PDF (or LaTeX compiled to one) is read page by page; a Word file
        paragraph by paragraph; LaTeX that could not be compiled is reduced to
        its words (which drops what follows the bibliography).
        """
        from . import manuscript

        if self.page_texts is not None:
            return "\n".join(self.page_texts)
        if self.is_docx:
            text = manuscript.docx_to_text(self.path)
            return None if text is None else normalize(text)
        if self.is_tex and self.tex_source is not None:
            return normalize(manuscript._tex_to_text(self.tex_source))
        if self.suffix in (".txt", ".md"):
            try:
                return normalize(self.path.read_text("utf-8", "replace"))
            except OSError:
                return None
        return None

    @cached_property
    def tex_source(self) -> str | None:
        """Comment-stripped LaTeX source, main file first, for a .tex or .zip bundle."""
        from . import manuscript

        if not self.is_tex:
            return None
        return manuscript._raw_tex_source(self.path)

    @cached_property
    def glyph_pages(self):
        """Positioned glyphs and images of the PDF (pdfminer.six), or None."""
        from . import pdf_layout

        if self.pdf is None or not pdf_layout.available():
            return None
        return pdf_layout.read_pages(self.pdf)

    def docx_part(self, name: str) -> str | None:
        """The XML of one part of a Word file (``word/document.xml``, ...), or None."""
        return self._docx_parts.get(name)

    def docx_media(self, name: str) -> bytes | None:
        if not self.is_docx:
            return None
        try:
            with zipfile.ZipFile(self.path) as zf:
                return zf.read(name)
        except (KeyError, OSError, zipfile.BadZipFile):
            return None

    @cached_property
    def docx_names(self) -> list[str]:
        if not self.is_docx:
            return []
        try:
            with zipfile.ZipFile(self.path) as zf:
                return zf.namelist()
        except (OSError, zipfile.BadZipFile):
            return []

    @cached_property
    def _docx_parts(self) -> dict[str, str]:
        if not self.is_docx:
            return {}
        parts: dict[str, str] = {}
        try:
            with zipfile.ZipFile(self.path) as zf:
                for name in zf.namelist():
                    if name.endswith((".xml", ".rels")):
                        parts[name] = zf.read(name).decode("utf-8", "replace")
        except (OSError, zipfile.BadZipFile) as exc:
            logger.debug("could not read %s as a Word file: %s", self.path, exc)
        return parts


@lru_cache(maxsize=32)
def _cached(path: str, size: int, mtime_ns: int) -> Document:
    return Document(Path(path))


def get(path: Path) -> Document:
    """The (cached) :class:`Document` for ``path``."""
    try:
        stat = path.stat()
        return _cached(str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return Document(path)
