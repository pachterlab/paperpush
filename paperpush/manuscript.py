"""Measure the length of a manuscript file before its reference list.

Venues cap the main text of a manuscript -- the part before the references --
either as a word count or a page count. This module extracts that measure from
the formats venues accept for the main manuscript:

* ``.tex`` (and a ``.zip`` LaTeX source bundle): the source is read directly,
  comments and the bibliography are removed, and LaTeX markup is stripped to
  leave countable words.
* ``.docx``: the Word document's ``document.xml`` is unzipped and its paragraph
  text extracted (no third-party dependency). A total page count, when needed,
  comes from Word's cached statistic in ``docProps/app.xml`` -- a best-effort
  hint, since a Word file has no intrinsic pagination.
* ``.pdf``: text and page boundaries are read with :mod:`pypdf` when it is
  installed, falling back to a dependency-free reader that decodes the page
  content streams (including ``FlateDecode``-compressed ones).

The public helpers return the measure of the text *before the references*, or
``None`` when the format cannot yield it -- a word count of a ``.doc`` binary,
or the page count of a ``.tex`` source, which is only fixed once rendered. The
caller (``paperpush.validate``) turns a ``None`` into an advisory warning
rather than a hard error.

The counts are deliberately approximate: a venue's word/page limit is itself
a soft target, and the aim here is to catch a manuscript that is clearly over,
not to reproduce a word processor's exact tally.
"""

from __future__ import annotations

import logging
import re
import zipfile
import zlib
from pathlib import Path

logger = logging.getLogger(__name__)

# A line that is, on its own, the heading that starts the reference list. Allows
# an optional leading section number (Arabic or Roman). Matched per line, so a
# passing mention of "references" inside a sentence does not trigger it.
_REFERENCE_HEADING = re.compile(
    r"^\s*(?:\d+\.?\s+|[ivxlcIVXLC]+\.?\s+)?" r"(references(?:\s+and\s+notes)?|bibliography|literature\s+cited|works\s+cited)" r"\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# LaTeX ways of starting the bibliography, checked against the raw source.
_TEX_BIBLIOGRAPHY = re.compile(
    r"\\(?:begin\s*\{\s*thebibliography\s*\}|bibliography\b|printbibliography\b)",
    re.IGNORECASE,
)
_TEX_REFERENCE_SECTION = re.compile(
    r"\\(?:section|chapter)\*?\s*\{[^}]*\b(?:references|bibliography)\b[^}]*\}",
    re.IGNORECASE,
)


def truncate_at_references(text: str) -> str:
    """Return ``text`` up to the start of its reference list.

    Looks for a standalone references/bibliography heading; if none is found the
    text is returned whole (the whole document then counts toward the limit).
    """
    match = _REFERENCE_HEADING.search(text)
    return text[: match.start()] if match else text


def _word_count(text: str) -> int:
    return len(text.split())


# --- LaTeX -----------------------------------------------------------------


def _strip_tex_comments(raw: str) -> str:
    """Drop LaTeX comments (an unescaped ``%`` to end of line)."""
    out_lines: list[str] = []
    for line in raw.splitlines():
        cut = len(line)
        for i, ch in enumerate(line):
            if ch == "%" and (i == 0 or line[i - 1] != "\\"):
                cut = i
                break
        out_lines.append(line[:cut])
    return "\n".join(out_lines)


def _tex_to_text(raw: str) -> str:
    """Reduce LaTeX source to countable words, dropping the bibliography.

    Comments are removed, the source is cut at the first bibliography marker
    (``\\bibliography``, ``thebibliography``, or a References section), and the
    remaining control words, control symbols, and math/grouping punctuation are
    replaced with spaces so only ordinary words remain.
    """
    text = _strip_tex_comments(raw)
    cut = len(text)
    for pattern in (_TEX_BIBLIOGRAPHY, _TEX_REFERENCE_SECTION):
        match = pattern.search(text)
        if match:
            cut = min(cut, match.start())
    text = text[:cut]
    # Structural commands whose braced argument is a name, not prose, so the name
    # (e.g. the "article" of \documentclass{article}) is not miscounted as a word.
    text = re.sub(
        r"\\(?:documentclass|usepackage|begin|end)\s*(?:\[[^\]]*\])?\s*\{[^}]*\}",
        " ",
        text,
    )
    # Control words (\section, \textbf, ...) and control symbols (\\, \&, \{).
    text = re.sub(r"\\[a-zA-Z@]+\*?", " ", text)
    text = re.sub(r"\\[^a-zA-Z]", " ", text)
    # Remaining grouping/math punctuation carries no words of its own.
    text = re.sub(r"[{}\[\]$&#~^_]", " ", text)
    return text


def _zip_tex_text(path: Path) -> str | None:
    """Concatenate the LaTeX sources inside a ``.zip`` bundle, or None.

    arXiv and some venues accept a zipped LaTeX source tree in place of a PDF.
    The ``.tex`` members are concatenated (main file first when one is obvious)
    and reduced like a single source. Returns None when the archive holds no
    ``.tex`` file.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            tex_names = [n for n in zf.namelist() if n.lower().endswith(".tex")]
            if not tex_names:
                return None
            # Read a likely main file first so a references section near its end
            # truncates the rest; "main"/"manuscript"/"ms" are common names.
            tex_names.sort(
                key=lambda n: (
                    not re.search(r"\b(main|manuscript|ms|paper)\b", n, re.IGNORECASE),
                    n,
                )
            )
            sources = []
            for name in tex_names:
                try:
                    sources.append(zf.read(name).decode("utf-8", "replace"))
                except KeyError:
                    continue
    except (zipfile.BadZipFile, OSError) as exc:
        logger.debug("Could not read LaTeX bundle %s: %s", path, exc)
        return None
    return _tex_to_text("\n".join(sources))


# --- DOCX ------------------------------------------------------------------

_W_TAB = re.compile(r"<w:tab\b[^>]*/>")
_W_BREAK = re.compile(r"<w:(?:br|cr)\b[^>]*/>")
_XML_TAG = re.compile(r"<[^>]+>")
_XML_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'"}


def _unescape_xml(text: str) -> str:
    for entity, char in _XML_ENTITIES.items():
        text = text.replace(entity, char)
    return text


def docx_to_text(path: Path) -> str | None:
    """Extract paragraph text from a ``.docx`` file with the standard library.

    A ``.docx`` is a zip whose ``word/document.xml`` holds the body. Each
    paragraph (``</w:p>``) becomes a line and each run of text (``<w:t>``) its
    content, so a references heading on its own paragraph stays on its own line.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        logger.debug("Could not read DOCX %s: %s", path, exc)
        return None
    xml = xml.replace("</w:p>", "\n")
    xml = _W_TAB.sub(" ", xml)
    xml = _W_BREAK.sub("\n", xml)
    # Stripping the remaining tags leaves the run text plus the newlines above.
    text = _XML_TAG.sub("", xml)
    return _unescape_xml(text)


# --- PDF -------------------------------------------------------------------


def _pdf_pages_pypdf(path: Path) -> list[str] | None:
    """Per-page text via pypdf, if it is installed. None if unavailable."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(str(path))
        return [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # pypdf raises a variety of parse errors
        logger.debug("pypdf failed to read %s: %s", path, exc)
        return None


_OBJ = re.compile(rb"(\d+)\s+0\s+obj\b(.*?)\bendobj", re.DOTALL)
_PAGE_TYPE = re.compile(rb"/Type\s*/Page\b")
_KIDS = re.compile(rb"/Kids\s*\[(.*?)\]", re.DOTALL)
_REF = re.compile(rb"(\d+)\s+0\s+R")
_CONTENTS_ONE = re.compile(rb"/Contents\s+(\d+)\s+0\s+R")
_CONTENTS_MANY = re.compile(rb"/Contents\s*\[(.*?)\]", re.DOTALL)
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_PDF_STRING = re.compile(rb"\((?:\\.|[^\\()])*\)", re.DOTALL)


def _pdf_objects(data: bytes) -> dict[int, bytes]:
    return {int(m.group(1)): m.group(2) for m in _OBJ.finditer(data)}


def _page_order(objs: dict[int, bytes]) -> list[int]:
    """Page-object numbers in reading order.

    Prefers the ``/Kids`` order from the page-tree node; falls back to every
    ``/Type /Page`` object sorted by object number when the tree cannot be read.
    """
    page_nums = {num for num, body in objs.items() if _PAGE_TYPE.search(body)}
    for body in objs.values():
        if b"/Pages" in body:
            kids_match = _KIDS.search(body)
            if kids_match:
                ordered = [int(n) for n in _REF.findall(kids_match.group(1))]
                ordered = [n for n in ordered if n in page_nums]
                if ordered:
                    return ordered
    return sorted(page_nums)


def _content_refs(body: bytes) -> list[int]:
    one = _CONTENTS_ONE.search(body)
    if one:
        return [int(one.group(1))]
    many = _CONTENTS_MANY.search(body)
    if many:
        return [int(n) for n in _REF.findall(many.group(1))]
    return []


def _stream_bytes(obj_body: bytes) -> bytes | None:
    match = _STREAM.search(obj_body)
    if not match:
        return None
    raw = match.group(1)
    if b"/FlateDecode" in obj_body:
        for candidate in (raw, raw.rstrip(b"\r\n")):
            try:
                return zlib.decompress(candidate)
            except zlib.error:
                continue
        return None
    return raw


def _decode_pdf_string(body: bytes) -> str:
    body = re.sub(rb"\\([()\\])", rb"\1", body)
    body = body.replace(rb"\n", b"\n").replace(rb"\r", b"\n").replace(rb"\t", b"\t")
    return body.decode("latin-1", "replace")


def _extract_stream_text(stream: bytes) -> str:
    """Join the literal strings drawn by a content stream, one per line.

    Both ``(text) Tj`` and the strings inside a ``[...] TJ`` array are literal
    PDF strings; pulling them all out and joining with newlines keeps each drawn
    line roughly on its own line, which is what the references-heading match
    needs.
    """
    return "\n".join(_decode_pdf_string(m.group(0)[1:-1]) for m in _PDF_STRING.finditer(stream))


def _pdf_pages_stdlib(path: Path) -> list[str] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(b"%PDF-"):
        return None
    objs = _pdf_objects(data)
    if not objs:
        return None
    pages: list[str] = []
    for page_num in _page_order(objs):
        parts: list[str] = []
        for content_num in _content_refs(objs.get(page_num, b"")):
            stream = _stream_bytes(objs.get(content_num, b""))
            if stream is not None:
                parts.append(_extract_stream_text(stream))
        pages.append("\n".join(parts))
    return pages or None


def _pdf_pages(path: Path) -> list[str] | None:
    """Per-page text of a PDF, preferring pypdf and falling back to stdlib."""
    return _pdf_pages_pypdf(path) or _pdf_pages_stdlib(path)


# --- public API ------------------------------------------------------------


def manuscript_text(path: Path) -> str | None:
    """Plain text of a manuscript file, or None if the format is unsupported."""
    suffix = path.suffix.lower()
    if suffix == ".tex":
        try:
            return _tex_to_text(path.read_text("utf-8", "replace"))
        except OSError:
            return None
    if suffix == ".zip":
        return _zip_tex_text(path)
    if suffix == ".docx":
        return docx_to_text(path)
    if suffix == ".pdf":
        pages = _pdf_pages(path)
        return None if pages is None else "\n".join(pages)
    return None


def words_before_references(path: Path) -> int | None:
    """Word count of the manuscript before its reference list, or None.

    None means the format could not be measured (e.g. a ``.doc`` binary or a PDF
    whose text could not be extracted), so the caller should not treat the limit
    as violated.
    """
    text = manuscript_text(path)
    if text is None:
        return None
    return _word_count(truncate_at_references(text))


def pages_before_references(path: Path) -> int | None:
    """Number of pages up to and including the page the references start on.

    Only a PDF carries fixed pages with a known layout, so this returns None for
    every other format -- a ``.docx`` records only a single cached total (see
    :func:`total_pages`), not where the references fall, and a ``.tex`` page
    count is only fixed once rendered. When no references heading is found, the
    document's full page count is returned.
    """
    if path.suffix.lower() != ".pdf":
        return None
    pages = _pdf_pages(path)
    if pages is None:
        return None
    for index, page_text in enumerate(pages):
        if _REFERENCE_HEADING.search(page_text or ""):
            return index + 1
    return len(pages)


def total_words(path: Path) -> int | None:
    """Word count of the whole manuscript, references included, or None.

    The counterpart to :func:`words_before_references` for venues that cap the
    entire document rather than only the main text. None means the format could
    not be measured, just as there.
    """
    text = manuscript_text(path)
    if text is None:
        return None
    return _word_count(text)


# Word records document statistics, including a page count, in ``docProps/app.xml``
# at save time. The element is absent in a docx no word processor has paginated.
_DOCX_PAGES = re.compile(r"<Pages>(\d+)</Pages>")


def _docx_page_count(path: Path) -> int | None:
    """Word's cached page count for a ``.docx``, or None when not recorded.

    A Word document has no intrinsic pagination -- the page count depends on the
    engine that renders it (fonts, margins, page size). Word does cache the count
    from its last save in ``docProps/app.xml``; that value is used when present,
    but it is absent in a docx written programmatically or never opened in Word,
    and can be stale after editing elsewhere. It is therefore a best-effort hint.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("docProps/app.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        logger.debug("No cached page count in %s: %s", path, exc)
        return None
    match = _DOCX_PAGES.search(xml)
    return int(match.group(1)) if match else None


def total_pages(path: Path) -> int | None:
    """Total number of pages in the manuscript, or None.

    A PDF carries fixed pages and is counted exactly. A ``.docx`` has no fixed
    pagination, so its count comes from Word's cached statistic and is a
    best-effort hint (see :func:`_docx_page_count`). Every other format -- a
    ``.tex`` source or a legacy ``.doc`` binary -- returns None.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pages = _pdf_pages(path)
        return None if pages is None else len(pages)
    if suffix == ".docx":
        return _docx_page_count(path)
    return None
