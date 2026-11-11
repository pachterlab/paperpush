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
* ``.md`` / ``.markdown``: reduced to the plain text a Word or PDF extraction
  would give (see :func:`markdown_to_text`), so headings written as ``## Methods``
  are found like any other; ``.txt`` is read as it is.
* ``.pdf``: text and page boundaries are read with :mod:`pypdf` when it is
  installed, falling back to a dependency-free reader that decodes the page
  content streams (including ``FlateDecode``-compressed ones).

The public helpers return the measure of the text *before the references*, or
``None`` when the format cannot yield it -- a word count of a ``.doc`` binary,
or the page count of a ``.tex`` source when no TeX toolchain is installed. A
page count of LaTeX source is only fixed once rendered, so when ``latexmk`` or
``pdflatex`` is available the source is compiled to a scratch PDF first (see
:func:`build_pdf`) and that PDF is measured. The caller
(``paperpush.validate``) turns a ``None`` into an advisory warning rather than
a hard error.

The counts are deliberately approximate: a venue's word/page limit is itself
a soft target, and the aim here is to catch a manuscript that is clearly over,
not to reproduce a word processor's exact tally.
"""

from __future__ import annotations

import atexit
import html
import logging
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# A line that is, on its own, the heading that starts the reference list. Allows
# an optional leading section number (Arabic or Roman). Matched per line, so a
# passing mention of "references" inside a sentence does not trigger it.
# Markdown heading markup around a heading line ("## References", "**References**",
# "References {#refs}"), so the matchers below also work on a raw Markdown file's
# text; :func:`markdown_to_text` removes it from text the checks read.
_MD_HEADING_OPEN = r"(?:#{1,6}[ \t]+)?(?:\*\*|__)?"
_MD_HEADING_CLOSE = r"(?:\*\*|__)?(?:[ \t]+#+)?(?:[ \t]*\{[#.][^{}\n]*\})?"

_REFERENCE_HEADING = re.compile(
    r"^\s*" + _MD_HEADING_OPEN + r"(?:\d+\.?\s+|[ivxlcIVXLC]+\.?\s+)?" r"(references(?:\s+and\s+notes|\s+cited)?|reference\s+list|cited\s+references|bibliography|literature\s+cited|works\s+cited)" r"\s*:?" + _MD_HEADING_CLOSE + r"\s*$",
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


# Words of leading text on a page that still count as page furniture -- a running
# head ("Published as a conference paper at ICLR 2027"), a page number, an
# extracted margin note -- rather than main text. Used to decide whether a
# heading starts its page. Set well above any real running head and far below a
# page of prose (several hundred words): erring high costs at most a sentence or
# two of overflow going unflagged, while erring low would reject a manuscript
# that is exactly at its limit, which is the common case for a hard page cap.
_PAGE_FURNITURE_WORDS = 25


def _extra_heading_pattern(headings: tuple[str, ...]) -> re.Pattern[str] | None:
    """A standalone-heading matcher for a venue's ``main_text_end_headings``.

    Each entry is a literal phrase, so ``submission_forms.json`` lists heading wordings
    ("Ethics Statement", "Appendix") rather than regular expressions. The shape
    mirrors :data:`_REFERENCE_HEADING` -- the phrase alone on its line, with an
    optional leading section designator -- plus an optional trailing designator,
    so both ``A Appendix`` and ``Appendix A`` are recognised. ``None`` when the
    venue names no extra headings.
    """
    phrases = [h.strip() for h in headings if h and h.strip()]
    if not phrases:
        return None
    alternatives = "|".join(re.escape(phrase) for phrase in phrases)
    return re.compile(
        r"^\s*" + _MD_HEADING_OPEN + r"(?:\d+\.?\s+|[A-Za-z]\.?\s+|[ivxlcIVXLC]+\.?\s+)?" rf"(?:{alternatives})" r"(?:\s+[A-Za-z0-9]{1,3})?" r"\s*:?" + _MD_HEADING_CLOSE + r"\s*$",
        re.IGNORECASE | re.MULTILINE,
    )


def main_text_end(text: str, headings: tuple[str, ...] = ()) -> re.Match[str] | None:
    """The earliest heading in ``text`` that ends the main text, or ``None``.

    The reference list always ends it; ``headings`` names the further sections a
    venue excludes from its before-refs limits (see ``main_text_end_headings`` in
    ``submission_forms.json``), such as an appendix or a required statement. Whichever
    comes first wins.
    """
    pattern = _extra_heading_pattern(headings)
    found = [m for m in (_REFERENCE_HEADING.search(text), pattern.search(text) if pattern else None) if m is not None]
    return min(found, key=lambda m: m.start()) if found else None


def truncate_at_references(text: str, headings: tuple[str, ...] = ()) -> str:
    """Return ``text`` up to the end of its main text.

    Looks for a standalone references/bibliography heading, or one of the venue's
    extra ``headings``; if none is found the text is returned whole (the whole
    document then counts toward the limit).
    """
    match = main_text_end(text, headings)
    return text[: match.start()] if match else text


def reference_list_span(text: str) -> tuple[int, int] | None:
    """Where the reference list runs in extracted text: its heading to whatever follows it, or None."""
    heading = _REFERENCE_HEADING.search(text)
    if heading is None:
        return None
    after = _AFTER_REFERENCES.search(text, heading.end())
    return heading.start(), after.start() if after else len(text)


def without_reference_list(text: str) -> str:
    """``text`` with its reference list cut out; the back matter after it stays.

    For the checks that read everything the authors wrote: a code-availability
    statement or a trial number can come after the references.
    """
    span = reference_list_span(text)
    return text if span is None else text[: span[0]] + "\n" + text[span[1] :]


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


_TEX_ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303", "=": "\u0304", ".": "\u0307", "u": "\u0306", "v": "\u030c", "H": "\u030b", "c": "\u0327", "k": "\u0328", "r": "\u030a", "d": "\u0323", "b": "\u0331"}
_TEX_ACCENT_BASE = r"(?:\\[ij](?![a-zA-Z])|[A-Za-z])"
_TEX_ACCENT_SYMBOL = re.compile(r"(?<!\\)\\(['`^\"~=.])\s*(?:\{\s*(" + _TEX_ACCENT_BASE + r")\s*\}|(" + _TEX_ACCENT_BASE + r"))")
_TEX_ACCENT_LETTER = re.compile(r"(?<!\\)\\([uvHckrdb])(?:\s*\{\s*(" + _TEX_ACCENT_BASE + r")\s*\}|\s+(" + _TEX_ACCENT_BASE + r"))")
_TEX_LETTERS = {"o": "\u00f8", "O": "\u00d8", "l": "\u0142", "L": "\u0141", "ss": "\u00df", "aa": "\u00e5", "AA": "\u00c5", "ae": "\u00e6", "AE": "\u00c6", "oe": "\u0153", "OE": "\u0152"}
_TEX_LETTER = re.compile(r"(?<!\\)\\(ss|aa|AA|ae|AE|oe|OE|o|O|l|L)(?![a-zA-Z])(?:\{\}|[ \t]+)?")
_TEX_BRACED_LETTER = re.compile(r"\{([^\x00-\x7f])\}")


def _tex_accent(match: re.Match[str]) -> str:
    base = (match.group(2) or match.group(3)).lstrip("\\")
    return unicodedata.normalize("NFC", base + _TEX_ACCENTS[match.group(1)])


def decode_tex_accents(text: str) -> str:
    """LaTeX accent and letter macros as the characters they print.

    ``Jos\\'e`` becomes ``Jos\u00e9``, ``M\\"{u}ller`` ``M\u00fcller``, ``S{\\o}rensen``
    ``S\u00f8rensen``; a discretionary hyphen (``\\-``) is removed.
    """
    text = _TEX_ACCENT_SYMBOL.sub(_tex_accent, text)
    text = _TEX_ACCENT_LETTER.sub(_tex_accent, text)
    text = _TEX_LETTER.sub(lambda m: _TEX_LETTERS[m.group(1)], text)
    text = re.sub(r"(?<!\\)\\-", "", text)
    return _TEX_BRACED_LETTER.sub(r"\1", text)


# Commands whose braced argument names something (a key, a file) instead of being prose.
_TEX_NON_PROSE = re.compile(r"\\(?:[a-zA-Z]*cite[a-zA-Z]*\*?|(?:eq|auto|page|name|c|C|v)?ref|label|url|href|includegraphics\*?|input|include|bibliography|addbibresource|bibliographystyle|bibitem|[vh]space\*?)\s*(?:\[[^\]]*\]\s*)*\{[^{}]*\}")
# What the preamble says that a reader sees.
_TEX_PREAMBLE_TEXT = re.compile(r"\\(?:title|author)\s*(?:\[[^\]]*\])?\s*\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}")
_TEX_BEGIN_DOCUMENT = re.compile(r"\\begin\s*\{\s*document\s*\}")


def _tex_to_text(raw: str, keep_bibliography: bool = False) -> str:
    """Reduce LaTeX source to countable words, dropping the bibliography.

    Comments are removed, accents decoded, the preamble reduced to its title and
    authors, the source cut at the first bibliography marker (``\\bibliography``,
    ``thebibliography``, or a References section) unless ``keep_bibliography``,
    and the remaining control words, control symbols, and math/grouping
    punctuation replaced with spaces so only ordinary words remain.
    """
    text = decode_tex_accents(_strip_tex_comments(raw))
    begin = _TEX_BEGIN_DOCUMENT.search(text)
    if begin:
        text = "\n".join(m.group(0) for m in _TEX_PREAMBLE_TEXT.finditer(text[: begin.start()])) + "\n" + text[begin.end() :]
    if not keep_bibliography:
        cut = len(text)
        for pattern in (_TEX_BIBLIOGRAPHY, _TEX_REFERENCE_SECTION):
            match = pattern.search(text)
            if match:
                cut = min(cut, match.start())
        text = text[:cut]
    text = _TEX_NON_PROSE.sub(" ", text)
    # Structural commands whose braced argument is a name, not prose, so the name
    # (e.g. the "article" of \\documentclass{article}) is not miscounted as a word.
    text = re.sub(
        r"\\(?:documentclass|usepackage|begin|end)\s*(?:\[[^\]]*\])?\s*\{[^}]*\}",
        " ",
        text,
    )
    # Control words (\\section, \\textbf, ...) and control symbols (\\\\, \\&, \\{).
    text = re.sub(r"\\[a-zA-Z@]+\*?", " ", text)
    text = re.sub(r"\\[^a-zA-Z]", " ", text)
    # Remaining grouping/math punctuation carries no words of its own.
    text = re.sub(r"[{}\[\]$&#~^_]", " ", text)
    return text


def _zip_tex_text(path: Path) -> str | None:
    """Concatenate the LaTeX sources inside a ``.zip`` bundle, or None.

    arXiv and some venues accept a zipped LaTeX source tree in place of a PDF.
    The root ``.tex`` is read with its ``\\input`` files in place (see
    :func:`_raw_tex_source`) and reduced like a single source. Returns None when
    the archive holds no ``.tex`` file.
    """
    source = _raw_tex_source(path)
    return _tex_to_text(source) if source else None


# --- DOCX ------------------------------------------------------------------

_W_TAB = re.compile(r"<w:tab\b[^>]*/>")
_W_BREAK = re.compile(r"<w:(?:br|cr)\b[^>]*/>")
_XML_TAG = re.compile(r"<[^>]+>")
# Parts of document.xml Word never shows as text: field codes (EndNote's and
# Zotero's run to pages), tracked deletions and moves, and the fallback copy of a text box.
_W_HIDDEN = re.compile(r"<w:(instrText|delInstrText|delText)\b[^>]*>.*?</w:\1>|<w:moveFrom\b[^>]*>.*?</w:moveFrom>|<mc:Fallback\b[^>]*>.*?</mc:Fallback>", re.DOTALL)
_W_TABLE = re.compile(r"<w:tbl>.*?</w:tbl>", re.DOTALL)
_XML_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'"}


def _unescape_xml(text: str) -> str:
    for entity, char in _XML_ENTITIES.items():
        text = text.replace(entity, char)
    return text


_W_PPR = re.compile(r"<w:pPr\b[^>]*>(.*?)</w:pPr>", re.DOTALL)
_W_NUM_PR = re.compile(r"<w:numPr\b[^>]*>(.*?)</w:numPr>", re.DOTALL)
_W_ILVL = re.compile(r'<w:ilvl\b[^>]*w:val="(\d+)"')
_W_NUM_ID = re.compile(r'<w:numId\b[^>]*w:val="(\d+)"')
_W_ABSTRACT = re.compile(r'<w:abstractNum\b[^>]*w:abstractNumId="(\d+)"[^>]*>(.*?)</w:abstractNum>', re.DOTALL)
_W_NUM = re.compile(r'<w:num\b[^>]*w:numId="(\d+)"[^>]*>(.*?)</w:num>', re.DOTALL)
_W_LVL = re.compile(r'<w:lvl\b[^>]*w:ilvl="(\d+)"[^>]*>(.*?)</w:lvl>', re.DOTALL)
_W_OVERRIDE = re.compile(r'<w:lvlOverride\b[^>]*w:ilvl="(\d+)"[^>]*>(.*?)</w:lvlOverride>', re.DOTALL)


def _docx_val(tag: str, xml: str) -> str | None:
    m = re.search(rf'<w:{tag}\b[^>]*w:val="([^"]*)"', xml)
    return m.group(1) if m else None


def _docx_numbering(numbering_xml: str) -> dict[str, dict[int, tuple[str, str, int]]]:
    """``numId -> {level: (numFmt, lvlText, start)}`` from ``word/numbering.xml``."""
    abstract: dict[str, dict[int, tuple[str, str, int]]] = {}
    for aid, body in _W_ABSTRACT.findall(numbering_xml):
        levels = {}
        for ilvl, lvl in _W_LVL.findall(body):
            levels[int(ilvl)] = (_docx_val("numFmt", lvl) or "decimal", _docx_val("lvlText", lvl) or "", int(_docx_val("start", lvl) or 1))
        abstract[aid] = levels
    out: dict[str, dict[int, tuple[str, str, int]]] = {}
    for num_id, body in _W_NUM.findall(numbering_xml):
        aid = _docx_val("abstractNumId", body)
        levels = dict(abstract.get(aid or "", {}))
        for ilvl, override in _W_OVERRIDE.findall(body):
            start = _docx_val("startOverride", override)
            if start is not None and int(ilvl) in levels:
                fmt, text, _ = levels[int(ilvl)]
                levels[int(ilvl)] = (fmt, text, int(start))
        out[num_id] = levels
    return out


def _number_list_paragraphs(xml: str, numbering_xml: str | None) -> str:
    """``xml`` with the number Word draws before each numbered-list paragraph written out.

    Word stores "1." of an automatically numbered list in ``numbering.xml``, not
    in the paragraph's text, so a reference list typed as a numbered list would
    otherwise lose its numbers. Only decimal lists are written out ("1.",
    "[1]", whatever the level's ``lvlText`` says); bullets are left alone.
    """
    if not numbering_xml or "<w:numPr" not in xml:
        return xml
    numbering = _docx_numbering(numbering_xml)
    counters: dict[str, dict[int, int]] = {}

    def number(m: re.Match[str]) -> str:
        num_pr = _W_NUM_PR.search(m.group(1))
        if num_pr is None:
            return m.group(0)
        num_id = _W_NUM_ID.search(num_pr.group(1))
        ilvl = _W_ILVL.search(num_pr.group(1))
        level = int(ilvl.group(1)) if ilvl else 0
        levels = numbering.get(num_id.group(1)) if num_id else None
        if not levels or level not in levels:
            return m.group(0)
        seen = counters.setdefault(num_id.group(1), {})
        fmt, text, start = levels[level]
        seen[level] = seen[level] + 1 if level in seen else start
        for deeper in [k for k in seen if k > level]:
            del seen[deeper]
        if fmt != "decimal":
            return m.group(0)
        label = re.sub(r"%(\d)", lambda g: str(seen.get(int(g.group(1)) - 1, levels.get(int(g.group(1)) - 1, ("", "", 1))[2])), text).strip()
        return m.group(0) + (f"<w:t>{label} </w:t>" if label else "")

    return _W_PPR.sub(number, xml)


def docx_to_text(path: Path, drop_tables: bool = False) -> str | None:
    """Extract paragraph text from a ``.docx`` file with the standard library.

    A ``.docx`` is a zip whose ``word/document.xml`` holds the body. Each
    paragraph (``</w:p>``) becomes a line and each run of text (``<w:t>``) its
    content, so a references heading on its own paragraph stays on its own line.
    A paragraph of an automatically numbered list starts with its number
    ("1. "), as Word shows it. Field codes and tracked deletions are left out;
    so are tables, with ``drop_tables``.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
            try:
                numbering = zf.read("word/numbering.xml").decode("utf-8", "replace")
            except KeyError:
                numbering = None
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        logger.debug("Could not read DOCX %s: %s", path, exc)
        return None
    xml = _W_HIDDEN.sub("", xml)
    if drop_tables:
        xml = _W_TABLE.sub("", xml)
    xml = _number_list_paragraphs(xml, numbering)
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


_LINE_TAIL = re.compile(r"\s*(\d{1,6})\s*$")


def _line_number_chain(tails: list[re.Match[str] | None], start: int, value: int, step: int) -> dict[int, str]:
    """``{line index: number}`` of the numbers continuing ``value`` by ``step`` from line ``start``."""
    picked = {start: str(value)}
    for index in range(start + 1, len(tails)):
        tail = tails[index]
        if tail is not None and tail.group(1).endswith(str(value + step)):
            value += step
            picked[index] = str(value)
    return picked


def _strip_line_numbers(page: str) -> str:
    """A page's text without the review line numbers extraction appends to its lines.

    pypdf reads a line-numbered manuscript as "1 Introduction10", "References64",
    which no heading matcher recognises. A number is a line number only as part
    of a run climbing by 1 (or by 5, for every-fifth-line numbering) down the page.
    """
    lines = page.split("\n")
    tails = [_LINE_TAIL.search(line) for line in lines]
    written = sum(1 for line in lines if line.strip())
    if sum(1 for t in tails if t) < 4:
        return page
    best: dict[int, str] = {}
    for start, tail in list(enumerate(tails))[:20]:
        if tail is None:
            continue
        digits = tail.group(1)
        # Longest reading first: "212" and its suffix "12" climb alike, and a tie keeps the first.
        for value in dict.fromkeys(int(digits[k:]) for k in range(len(digits))):
            for step, needed in ((1, max(8, written // 2)), (5, max(4, written // 10))):
                chain = _line_number_chain(tails, start, value, step)
                indices = sorted(chain)
                if len(chain) >= needed and len(chain) > len(best) and (step == 1 or all(3 <= b - a <= 8 for a, b in zip(indices, indices[1:]))):
                    best = chain
    for index, number in best.items():
        tail = tails[index]
        lines[index] = lines[index][: tail.end(1) - len(number)].rstrip()
    return "\n".join(lines)


def _pdf_pages(path: Path) -> list[str] | None:
    """Per-page text of a PDF, preferring pypdf and falling back to stdlib."""
    pages = _pdf_pages_pypdf(path) or _pdf_pages_stdlib(path)
    return None if pages is None else [_strip_line_numbers(page) for page in pages]


# --- Markdown --------------------------------------------------------------

MARKDOWN_SUFFIXES = (".md", ".markdown")

# Markdown markup a heading line can carry, and the pattern shared by every
# heading matcher in the package: "## 2. Methods ##", "**Methods**", "Methods {#sec:methods}".
_MD_ATX = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$", re.MULTILINE)
_MD_ATX_EMPTY = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*$", re.MULTILINE)
_MD_SETEXT = re.compile(r"^([ \t]{0,3}\S[^\n]*)\n[ \t]{0,3}(?:=+|-+)[ \t]*$", re.MULTILINE)
_MD_RULE = re.compile(r"^[ \t]{0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$", re.MULTILINE)
_MD_HEADING_ATTRS = re.compile(r"[ \t]*\{[#.][^{}\n]*\}[ \t]*$", re.MULTILINE)
_MD_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n.*?^[ \t]{0,3}\1[ \t]*$", re.MULTILINE | re.DOTALL)
_MD_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.DOTALL)
_MD_IMAGE = re.compile(r"!\[([^\]\n]*)\]\([^)\n]*\)")
_MD_LINK = re.compile(r"(?<!\\)\[([^\]\n]+)\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)")
_MD_REF_LINK = re.compile(r"(?<!\\)\[([^\]\n]+)\]\[[^\]\n]*\]")
_MD_LINK_DEF = re.compile(r"^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*\S+.*$", re.MULTILINE)
_MD_AUTOLINK = re.compile(r"<((?:https?|ftp|mailto):[^>\s]+)>")
_MD_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_MD_EM = re.compile(r"(?<![\w*\\])([*_])(?=\S)([^*_\n]+?)(?<=\S)\1(?![\w*])")
_MD_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_MD_CODE = re.compile(r"(`+)(.+?)\1")
_MD_QUOTE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.MULTILINE)
_MD_TABLE_RULE = re.compile(r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*(?:\n|\Z)", re.MULTILINE)
_MD_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")


def _front_matter_lines(block: str) -> list[str]:
    """The text a YAML front-matter block carries: its title, authors, abstract, and so on, as lines."""
    lines: list[str] = []
    for raw in block.splitlines():
        m = re.match(r"^\s*(?:-\s+)?(?:(title|subtitle|author|authors|name|date|abstract|keywords|affiliation|institute)\s*:)?\s*(.*)$", raw, re.IGNORECASE)
        if not m:
            continue
        key, value = (m.group(1) or "").lower(), m.group(2).strip().strip("'\"")
        if value in ("|", ">", "|-", ">-"):
            value = ""
        if key == "abstract":
            lines += ["", "Abstract"]
        if value and (key or raw.startswith((" ", "\t"))):
            lines.append(value)
    return lines


def _link_text(match: re.Match[str]) -> str:
    """A link as a reader sees it: its text, plus the target when the text does not already show it."""
    text, url = match.group(1), match.group(2)
    if not url or url.startswith("#") or url.lower() in text.lower() or text.lower() in url.lower():
        return text
    return f"{text} ({url})"


def markdown_to_text(source: str) -> str:
    """Markdown reduced to the plain text a Word or PDF extraction would give.

    Every heading -- ATX (``## Methods``, ``## 2. Methods ##``), setext (a line
    underlined with ``===`` or ``---``), or a paragraph that is all bold
    (``**Materials and methods**``) -- becomes a line of its own with the markup
    and any Pandoc attributes (``{#sec:methods}``) removed, which is the shape
    the heading matchers (references, sections, statements, abstract) expect.
    Emphasis and code marks are dropped, links and images become their text
    (a link keeps its target in brackets when the text does not show it, so a
    DOI link still carries its DOI), and fenced code, HTML comments, tags, and
    table rules are removed. YAML front matter is kept as its values (title,
    authors, abstract under an "Abstract" heading). List markers stay, so a
    numbered reference list still reads as one.
    """
    text = source.replace("\r\n", "\n").replace("\r", "\n")
    front = _MD_FRONT_MATTER.match(text)
    head = ""
    if front:
        head = "\n".join(_front_matter_lines(front.group(1))) + "\n\n"
        text = text[front.end() :]
    text = _MD_FENCE.sub("\n", text)
    text = _MD_HTML_COMMENT.sub(" ", text)
    text = _MD_LINK_DEF.sub("", text)
    text = _MD_TABLE_RULE.sub("", text)
    # Setext before rules: "Methods\n-------" is a heading, a lone "---" a rule.
    text = _MD_SETEXT.sub(lambda m: "\n" + m.group(1).strip() + "\n", text)
    text = _MD_RULE.sub("", text)
    text = _MD_ATX.sub(lambda m: "\n" + m.group(1).strip() + "\n", text)
    text = _MD_ATX_EMPTY.sub("", text)
    text = _MD_HEADING_ATTRS.sub("", text)
    text = _MD_QUOTE.sub("", text)
    text = _MD_IMAGE.sub(lambda m: m.group(1), text)
    text = _MD_LINK.sub(_link_text, text)
    text = _MD_REF_LINK.sub(lambda m: m.group(1), text)
    text = _MD_AUTOLINK.sub(lambda m: m.group(1), text)
    text = _MD_CODE.sub(lambda m: m.group(2).strip(), text)
    text = _MD_STRIKE.sub(lambda m: m.group(1), text)
    text = _MD_STRONG.sub(lambda m: m.group(2), text)
    text = _MD_EM.sub(lambda m: m.group(2), text)
    text = _MD_HTML_TAG.sub("", text)
    text = _MD_ESCAPE.sub(r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", head + text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


_MD_BOLD_LINE = re.compile(r"^[ \t]{0,3}(?:\*\*|__)([^*_\n]{2,80}?)(?:\*\*|__)[ \t]*:?[ \t]*$", re.MULTILINE)
_MD_RUN_IN = re.compile(r"^[ \t]{0,3}(?:\*\*|__)([^*_\n]{2,60}?)[.:]?(?:\*\*|__)[.:]?[ \t]+\S", re.MULTILINE)


def markdown_headings(source: str) -> list[str]:
    """The headings a Markdown source marks as headings, in order, normalized.

    ATX (``## Methods``) and setext headings, paragraphs that are all bold
    (``**Methods**``), and run-in bold labels that open a paragraph
    (``**Data availability.** The data are ...``), which is how statements are
    often written. Fenced code is skipped, so a ``# comment`` in a code block is
    not a heading.
    """
    text = _MD_FENCE.sub("\n", source.replace("\r\n", "\n"))
    text = _MD_HTML_COMMENT.sub(" ", text)
    front = _MD_FRONT_MATTER.match(text)
    found: list[tuple[int, str]] = []
    if front:
        if re.search(r"^abstract\s*:", front.group(1), re.IGNORECASE | re.MULTILINE):
            found.append((0, "abstract"))
        text = "\n" * front.end() + text[front.end() :]
    for pattern in (_MD_ATX, _MD_SETEXT, _MD_BOLD_LINE, _MD_RUN_IN):
        for m in pattern.finditer(text):
            if pattern is _MD_SETEXT and _MD_RULE.fullmatch(m.group(1).strip()):
                continue
            found.append((m.start(), m.group(1)))
    out = []
    for _, raw in sorted(found):
        plain = _MD_EM.sub(lambda m: m.group(2), _MD_STRONG.sub(lambda m: m.group(2), _MD_CODE.sub(lambda m: m.group(2), raw)))
        heading = normalize_heading(plain)
        if heading:
            out.append(heading)
    return out


def read_markdown(path: Path) -> str | None:
    """The plain text of a Markdown file (see :func:`markdown_to_text`), or None if unreadable."""
    try:
        return markdown_to_text(path.read_text("utf-8", "replace"))
    except OSError:
        return None


# --- public API ------------------------------------------------------------


def manuscript_text(path: Path) -> str | None:
    """Plain text of a manuscript file, or None if the format is unsupported."""
    suffix = path.suffix.lower()
    if suffix in MARKDOWN_SUFFIXES:
        return read_markdown(path)
    if suffix == ".txt":
        try:
            return path.read_text("utf-8", "replace")
        except OSError:
            return None
    if suffix == ".tex":
        source = _raw_tex_source(path)
        return None if source is None else _tex_to_text(source)
    if suffix == ".zip":
        return _zip_tex_text(path)
    if suffix == ".docx":
        return docx_to_text(path)
    if suffix == ".pdf":
        pages = _pdf_pages(path)
        # A scanned PDF has pages but no text: it is unreadable, not empty.
        return None if pages is None or not any(p.strip() for p in pages) else "\n".join(pages)
    return None


def words_before_references(path: Path, headings: tuple[str, ...] = ()) -> int | None:
    """Word count of the manuscript's main text, or None.

    The main text ends at the reference list, or earlier at one of the venue's
    ``headings`` (see :func:`main_text_end`). None means the format could not be
    measured (e.g. a ``.doc`` binary or a PDF whose text could not be extracted),
    so the caller should not treat the limit as violated.
    """
    text = manuscript_text(path)
    if text is None:
        return None
    return _word_count(truncate_at_references(text, headings))


def pages_before_references(path: Path, headings: tuple[str, ...] = ()) -> int | None:
    """Number of pages the manuscript's main text occupies, or None.

    Only a PDF carries fixed pages with a known layout, so this returns None for
    every other format -- a ``.docx`` records only a single cached total (see
    :func:`total_pages`), not where the references fall. A ``.tex`` source (or a
    ``.zip`` bundle of one) is first compiled to a scratch PDF when a TeX
    toolchain is installed (see :func:`build_pdf`); without one it returns None.
    When nothing ends the main text, the document's full page count is
    returned.

    Without ``headings`` this counts up to *and including* the page the
    references start on. A venue that names its own ``main_text_end_headings``
    is measuring the main text proper against a hard limit, where that extra page
    is the difference between passing and failing, so for those the page holding
    the heading is counted only when real text precedes it: a heading with
    nothing but a running head or page number ahead of it starts its page, and
    the main text ended on the one before.
    """
    pdf = _as_pdf(path)
    if pdf is None:
        return None
    pages = _pdf_pages(pdf)
    if pages is None:
        return None
    for index, page_text in enumerate(pages):
        match = main_text_end(page_text or "", headings)
        if match is None:
            continue
        if not headings:
            return index + 1
        preceding = (page_text or "")[: match.start()]
        return index if len(preceding.split()) <= _PAGE_FURNITURE_WORDS else index + 1
    return len(pages)


def total_words(path: Path) -> int | None:
    """Word count of the whole manuscript, references included, or None.

    The counterpart to :func:`words_before_references` for venues that cap the
    entire document rather than only the main text. None means the format could
    not be measured, just as there.
    """
    if path.suffix.lower() in (".tex", ".zip"):
        source = _raw_tex_source(path)
        if not source:
            return None
        bib = _cited_bib_entries(path, source) or []
        return _word_count(_tex_to_text(source, keep_bibliography=True)) + sum(_word_count(_BIB_MARKUP.sub(" ", entry)) for entry in bib)
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
    best-effort hint (see :func:`_docx_page_count`). A ``.tex`` source (or a
    ``.zip`` bundle of one) is compiled to a scratch PDF first when a TeX
    toolchain is installed (see :func:`build_pdf`). Every other format -- a
    legacy ``.doc`` binary, or LaTeX with no toolchain -- returns None.
    """
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _docx_page_count(path)
    pdf = _as_pdf(path)
    if pdf is None:
        return None
    pages = _pdf_pages(pdf)
    return None if pages is None else len(pages)


# --- LaTeX -> PDF ----------------------------------------------------------
#
# A page limit can only be checked on rendered pages, so a LaTeX manuscript is
# compiled to a scratch PDF before counting. The build runs in a temporary
# output directory (the source tree is never written to) and is cached per
# source file for the life of the process, so the validate pass that counts
# pages before the references and the one that counts the total share one
# compile.

# Seconds a single compile may take before it is abandoned.
BUILD_TIMEOUT_S = 300


@dataclass(frozen=True)
class TexBuild:
    """What compiling a LaTeX manuscript produced.

    ``pdf`` is the scratch PDF, or None when none was produced. ``errors`` are
    the errors in the LaTeX log, in order. ``has_root`` is False for a ``.zip``
    with no ``.tex`` holding ``\\documentclass``, which there is nothing to compile.
    """

    pdf: Path | None = None
    errors: tuple[str, ...] = ()
    has_root: bool = True


_BUILD_CACHE: dict[tuple[str, int, int], TexBuild] = {}
_BUILD_DIRS: list[str] = []


def _cleanup_build_dirs() -> None:
    for d in _BUILD_DIRS:
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup_build_dirs)


def _new_build_dir() -> Path:
    d = tempfile.mkdtemp(prefix="paperpush-build-")
    _BUILD_DIRS.append(d)
    return Path(d)


_DOCUMENTCLASS = re.compile(r"^\s*\\documentclass", re.MULTILINE)


def _main_tex(tex_files: list[Path]) -> Path | None:
    """Pick the root ``.tex`` of a source tree: the one with ``\\documentclass``.

    When several qualify, a conventional name (``main``, ``manuscript``, ``ms``,
    ``paper``) wins, then the shortest path (a root file over one nested in a
    subdirectory).
    """
    roots = []
    for tex in tex_files:
        try:
            if _DOCUMENTCLASS.search(_strip_tex_comments(tex.read_text("utf-8", "replace"))):
                roots.append(tex)
        except OSError:
            continue
    if not roots:
        return None
    roots.sort(key=lambda p: (not re.search(r"\b(main|manuscript|ms|paper)\b", p.stem, re.IGNORECASE), len(p.parts), str(p)))
    return roots[0]


def tex_toolchain() -> str | None:
    """Name of the available TeX build tool (``latexmk`` or ``pdflatex``), or None."""
    for tool in ("latexmk", "pdflatex"):
        if shutil.which(tool):
            return tool
    return None


def _run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float) -> bool:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("%s failed to run: %s", cmd[0], exc)
        return False
    if proc.returncode != 0:
        tail = proc.stdout.decode("utf-8", "replace")[-2000:]
        logger.debug("%s exited %d:\n%s", cmd[0], proc.returncode, tail)
    return proc.returncode == 0


def _compile(main: Path, outdir: Path) -> Path | None:
    """Compile ``main`` into ``outdir`` and return the PDF path, or None.

    Prefers ``latexmk`` (which reruns pdflatex/bibtex/biber as needed); falls
    back to pdflatex twice around a bibtex pass when only pdflatex is present.
    Neither stops at the first LaTeX error, and a non-zero exit still yields the
    PDF when one was produced -- a stray error does not change the page count
    enough to matter, and a partial render beats no measurement at all.
    """
    env = dict(os.environ)
    # Let \input/\include and \bibliography find the source tree from the
    # output directory, whichever tool runs.
    src = str(main.parent)
    for var in ("TEXINPUTS", "BIBINPUTS", "BSTINPUTS"):
        env[var] = src + os.pathsep + env.get(var, "") + os.pathsep
    env.setdefault("max_print_line", "1000")
    pdf = outdir / (main.stem + ".pdf")
    tool = tex_toolchain()
    if tool == "latexmk":
        _run(["latexmk", "-pdf", "-interaction=nonstopmode", "-f", f"-outdir={outdir}", main.name], main.parent, env, BUILD_TIMEOUT_S)
    elif tool == "pdflatex":
        pdflatex = ["pdflatex", "-interaction=nonstopmode", f"-output-directory={outdir}", main.name]
        _run(pdflatex, main.parent, env, BUILD_TIMEOUT_S / 3)
        if shutil.which("bibtex") and (outdir / (main.stem + ".aux")).exists():
            _run(["bibtex", main.stem], outdir, env, 60)
        _run(pdflatex, main.parent, env, BUILD_TIMEOUT_S / 3)
        _run(pdflatex, main.parent, env, BUILD_TIMEOUT_S / 3)
    else:
        return None
    return pdf if pdf.is_file() and pdf.stat().st_size > 0 else None


def _log_errors(log: Path) -> tuple[str, ...]:
    """The errors in a LaTeX log, each with the source line LaTeX points at."""
    try:
        lines = log.read_text("utf-8", "replace").splitlines()
    except OSError:
        return ()
    errors: list[str] = []
    stopped = False
    for index, line in enumerate(lines):
        stopped = stopped or line.startswith("! Emergency stop")
        if not line.startswith("! ") or "==> Fatal error occurred" in line or line.startswith("! Emergency stop"):
            continue
        message = line[2:].strip()
        for following in lines[index + 1 : index + 15]:
            if following.startswith("! "):
                break
            if re.match(r"l\.\d+", following):
                message += f" ({following.strip()})"
                break
        errors.append(message)
    if stopped and not errors:
        errors.append("LaTeX stopped early, often because the file has no \\end{document}")
    return tuple(dict.fromkeys(errors))


def tex_build(path: Path) -> TexBuild:
    """Compile a ``.tex`` file or ``.zip`` LaTeX bundle to a scratch PDF and report how it went.

    The result is cached per (path, size, mtime) for the process, so repeated
    measurements of one manuscript compile once. The source directory is never
    modified: all build products go to the scratch directory, which is removed
    at exit.
    """
    try:
        stat = path.stat()
    except OSError:
        return TexBuild()
    key = (str(path.resolve()), stat.st_size, int(stat.st_mtime))
    if key in _BUILD_CACHE:
        return _BUILD_CACHE[key]
    result = TexBuild()
    if tex_toolchain() is None:
        logger.info("No TeX toolchain (latexmk/pdflatex) found; cannot render %s for a page count", path.name)
    else:
        outdir = _new_build_dir()
        suffix = path.suffix.lower()
        try:
            if suffix == ".tex":
                logger.info("Compiling %s to a scratch PDF for page counting", path.name)
                pdf = _compile(path, outdir)
                result = TexBuild(pdf, _log_errors(outdir / (path.stem + ".log")))
            elif suffix == ".zip":
                srcdir = outdir / "src"
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(srcdir)
                main = _main_tex(sorted(srcdir.rglob("*.tex")))
                if main is None:
                    logger.info("%s holds no .tex with \\documentclass; cannot render it", path.name)
                    result = TexBuild(has_root=False)
                else:
                    logger.info("Compiling %s (from %s) to a scratch PDF for page counting", main.name, path.name)
                    (outdir / "out").mkdir(exist_ok=True)
                    pdf = _compile(main, outdir / "out")
                    result = TexBuild(pdf, _log_errors(outdir / "out" / (main.stem + ".log")))
        except (zipfile.BadZipFile, OSError) as exc:
            logger.debug("Could not build %s: %s", path, exc)
            result = TexBuild()
        if result.pdf is None:
            logger.warning("Could not render %s to PDF; its page count was not checked", path.name)
    _BUILD_CACHE[key] = result
    return result


def build_pdf(path: Path) -> Path | None:
    """The scratch PDF compiled from a ``.tex`` file or ``.zip`` LaTeX bundle (see :func:`tex_build`).

    None when no TeX toolchain is installed, the bundle has no root ``.tex``, or
    the build produced no PDF.
    """
    return tex_build(path).pdf


def _as_pdf(path: Path) -> Path | None:
    """``path`` itself for a PDF, a scratch build for LaTeX, None otherwise."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return path
    if suffix in (".tex", ".zip"):
        return build_pdf(path)
    return None


# --- structure: headings, references, title page ---------------------------

# A LaTeX sectioning command and its title argument (one level of nested braces).
_TEX_SECTION = re.compile(r"\\(?:part|chapter|section|subsection|subsubsection|paragraph|bmhead)\*?\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{[^{}]*\})*)\}")
# Leading section designators a heading line may carry: "2.", "II.", "A.", "2.1".
_HEADING_PREFIX = re.compile(r"^\s*(?:#{1,6}\s+)?(?:(?:\d+(?:\.\d+)*\.?|[ivxlcIVXLC]+\.|[A-Z]\.)\s+)?")
# Longest line that can still be a heading, in words.
_HEADING_MAX_WORDS = 8


def normalize_heading(line: str) -> str:
    """A heading line reduced to its lower-cased wording.

    Drops a leading section designator, trailing punctuation, and (for LaTeX)
    residual markup, so ``"2. Materials and Methods:"`` and ``"MATERIALS AND
    METHODS"`` both become ``"materials and methods"``.
    """
    return heading_text(line).lower().replace(" & ", " and ")


def heading_text(line: str) -> str:
    """A heading line's wording with its case kept: no section number, markup, or trailing punctuation."""
    text = _MD_HEADING_ATTRS.sub("", line.strip())
    text = re.sub(r"^(\*\*|__)(.*)\1$", r"\2", text.strip())
    text = re.sub(r"[ \t]+#+$", "", _HEADING_PREFIX.sub("", text, count=1))
    text = re.sub(r"\\label\s*\{[^{}]*\}", "", text).replace("\\&", "&")
    text = re.sub(r"\\[a-zA-Z@]+\*?", " ", text)
    text = re.sub(r"[{}]", "", text)
    text = text.rstrip(" .:;-\u2013\u2014")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


_TEX_INPUT = re.compile(r"\\(?:input|include|subfile)\s*(?:\{\s*([^{}]+?)\s*\}|[ \t]+([^\s{}\\]+))")


def _expand_tex(source: str, read, seen: tuple[str, ...] = ()) -> str:
    """Comment-stripped ``source`` with each ``\\input``/``\\include`` replaced by the file it names."""

    def expand(match: re.Match[str]) -> str:
        name = (match.group(1) or match.group(2)).strip()
        for candidate in (name, name + ".tex"):
            text = None if candidate in seen or len(seen) > 32 else read(candidate)
            if text is not None:
                return _expand_tex(text, read, seen + (candidate,))
        return match.group(0)

    return _TEX_INPUT.sub(expand, _strip_tex_comments(source))


def _raw_tex_source(path: Path) -> str | None:
    """The comment-stripped LaTeX source of a ``.tex`` file or ``.zip`` bundle, accents decoded.

    ``\\input`` and ``\\include`` are followed from the root file, so the source
    reads as the compiled document does. A bundle with no ``\\documentclass`` is
    its ``.tex`` files joined.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".tex":

            def read_file(name: str) -> str | None:
                target = path.parent / name
                return target.read_text("utf-8", "replace") if target.is_file() else None

            return decode_tex_accents(_expand_tex(path.read_text("utf-8", "replace"), read_file))
        if suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".tex")]
                names.sort(key=lambda n: (not re.search(r"\b(main|manuscript|ms|paper)\b", n, re.IGNORECASE), n.count("/"), n))
                members = set(zf.namelist())
                texts = {n: zf.read(n).decode("utf-8", "replace") for n in names}
                root = next((n for n in names if _DOCUMENTCLASS.search(_strip_tex_comments(texts[n]))), None)
                if root is None:
                    return decode_tex_accents("\n".join(_strip_tex_comments(texts[n]) for n in names))
                base = posixpath.dirname(root)

                def read_member(name: str) -> str | None:
                    member = posixpath.normpath(posixpath.join(base, name))
                    return zf.read(member).decode("utf-8", "replace") if member in members else None

                return decode_tex_accents(_expand_tex(texts[root], read_member))
    except (OSError, zipfile.BadZipFile) as exc:
        logger.debug("Could not read LaTeX source %s: %s", path, exc)
    return None


def headings(path: Path) -> list[str] | None:
    """Candidate section headings of a manuscript, normalised, in order.

    For LaTeX source these are the sectioning commands' titles; for Markdown,
    the lines it marks as headings (see :func:`markdown_headings`). For a PDF or
    Word file -- where a heading is just a short line of its own -- every line
    of at most :data:`_HEADING_MAX_WORDS` words is a candidate; the caller
    matches them against the wordings it is looking for, so the over-inclusion
    is harmless. Returns None when the file cannot be read as text.
    """
    if path.suffix.lower() in (".tex", ".zip"):
        source = _raw_tex_source(path)
        if source is None:
            return None
        marked = [(m.start(), normalize_heading(m.group(1))) for m in _TEX_SECTION.finditer(source)]
        # Statements are often set as \paragraph{} or as bold run-in text
        # (\textbf{Data availability.}) rather than sections; include those.
        for m in re.finditer(r"\\(?:textbf|textit|emph|noindent\s*\\textbf)\s*\{([^{}]{3,60})\}", source):
            marked.append((m.start(), normalize_heading(m.group(1))))
        # Parts a class sets with an environment or macro of their own.
        for pattern, name in _TEX_IMPLICIT_HEADINGS:
            marked += [(m.start(), name) for m in pattern.finditer(source)]
        return [h for _, h in sorted(marked, key=lambda item: item[0]) if h]
    if path.suffix.lower() in MARKDOWN_SUFFIXES:
        try:
            marked = markdown_headings(path.read_text("utf-8", "replace"))
        except OSError:
            return None
        # A Markdown file that marks no headings at all is read like plain text.
        if marked:
            return marked
    text = manuscript_text(path)
    if text is None:
        return None
    found = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        head = normalize_heading(stripped) if len(stripped.split()) <= _HEADING_MAX_WORDS else ""
        if head:
            found.append(head)
        # A sentence ending in a full stop is prose, not a heading -- but a
        # run-in heading ("Data availability. The data are...", "Funding: NIH")
        # is caught by taking the text before the first break too.
        run_in = _run_in_heading(stripped)
        if run_in:
            first = normalize_heading(run_in[0])
            if first and first != head:
                found.append(first)
    return found


# The label of a run-in heading, and where a heading matcher should look for it.
_RUN_IN_BREAK = re.compile(r"(.+?)[.:](?=\s|$)")


def _run_in_heading(line: str) -> tuple[str, int] | None:
    """``(label, offset just past it)`` when ``line`` opens with a short label ending in ``.`` or ``:``."""
    body = _HEADING_PREFIX.sub("", line, count=1)
    match = _RUN_IN_BREAK.match(body)
    if match is None or len(match.group(1).split()) > _HEADING_MAX_WORDS:
        return None
    # A label is capitalized; "results. We..." is a sentence wrapped at that word.
    if not next((c.isupper() for c in match.group(1) if c.isalpha()), False):
        return None
    return match.group(1), len(line) - len(body) + match.end()


_TEX_IMPLICIT_HEADINGS = (
    (re.compile(r"\\begin\s*\{\s*abstract\s*\}|\\abstract\s*\{"), "abstract"),
    (re.compile(r"\\begin\s*\{\s*(?:acknowledge?ments?|acks)\s*\}|\\acknowledge?ments?\b|\\acknow\s*\{"), "acknowledgements"),
    (_TEX_BIBLIOGRAPHY, "references"),
)



# Multi-word section names, recognizable where a file does not mark its headings (a PDF, plain text, an unstyled Word file).
_STANDARD_HEADINGS = frozenset(
    (
        "materials and methods|methods and materials|results and discussion|discussion and conclusions|data availability|data availability statement|code availability|"
        "data and code availability|author contributions|competing interests|conflict of interest|conflicts of interest|declaration of interests|ethics statement|ethics approval|"
        "supplementary material|supplementary materials|supplementary information|statistical analysis|study design|funding sources|financial disclosure"
    ).split("|")
)
_TEX_SECTION_LEVEL = re.compile(r"\\(part|chapter|section|subsection|subsubsection|paragraph)\*?\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{[^{}]*\})*)\}")
_DOCX_PARAGRAPH = re.compile(r"<w:p[ >].*?</w:p>", re.DOTALL)
_DOCX_HEADING_STYLE = re.compile(r'<w:pStyle\s+w:val="[^"]*?heading\s*(\d)[^"]*"', re.IGNORECASE)
_DOCX_TEXT = re.compile(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>")


def _markdown_cased_headings(source: str) -> list[tuple[str, str]]:
    text = _MD_FENCE.sub("\n", source.replace("\r\n", "\n"))
    text = _MD_HTML_COMMENT.sub(" ", text)
    front = _MD_FRONT_MATTER.match(text)
    if front:
        text = text[front.end() :]
    found: list[tuple[int, str, str]] = []
    for m in _MD_ATX.finditer(text):
        marks = m.group(0).strip()
        found.append((m.start(), f"h{len(marks) - len(marks.lstrip('#'))}", m.group(1)))
    for m in _MD_SETEXT.finditer(text):
        if not _MD_RULE.fullmatch(m.group(1).strip()):
            found.append((m.start(), "h1" if m.group(0).rstrip().endswith("=") else "h2", m.group(1)))
    for pattern, level in ((_MD_BOLD_LINE, "bold"), (_MD_RUN_IN, "run-in")):
        found += [(m.start(), level, m.group(1)) for m in pattern.finditer(text)]
    out = []
    for _, level, raw in sorted(found):
        plain = _MD_EM.sub(lambda m: m.group(2), _MD_STRONG.sub(lambda m: m.group(2), _MD_CODE.sub(lambda m: m.group(2), raw)))
        out.append((level, heading_text(plain)))
    return out


def cased_headings(path: Path) -> list[tuple[str, str]] | None:
    """``(level, heading as written)`` for each heading a manuscript marks, in order.

    LaTeX sectioning commands, Markdown headings, and Word's Heading styles
    carry their level, so headings can be compared with others of their rank.
    A file that marks no headings has only its standard multi-word section
    names read, all at one level ("standard"): any other short line of a PDF
    might not be a heading. Returns None when the file cannot be read.
    """
    from . import document

    doc = document.get(path)
    found: list[tuple[str, str]] = []
    if doc.is_tex:
        if doc.tex_source is None:
            return None
        return [(m.group(1), heading_text(m.group(2))) for m in _TEX_SECTION_LEVEL.finditer(doc.tex_source)]
    if doc.is_markdown:
        try:
            found = _markdown_cased_headings(path.read_text("utf-8", "replace"))
        except OSError:
            return None
    elif doc.is_docx:
        xml = doc.docx_part("word/document.xml")
        if xml is None:
            return None
        for paragraph in _DOCX_PARAGRAPH.findall(xml):
            style = _DOCX_HEADING_STYLE.search(paragraph)
            if style:
                found.append((f"h{style.group(1)}", heading_text(html.unescape("".join(_DOCX_TEXT.findall(paragraph))))))
    found = [(level, text) for level, text in found if text]
    if found:
        return found
    text = doc.text
    if text is None:
        return None
    return [("standard", heading_text(line)) for line in text.splitlines() if normalize_heading(line) in _STANDARD_HEADINGS]


def section_word_counts(path: Path, wordings: dict[str, str]) -> dict[str, int] | None:
    """Words under each known section heading of a manuscript's main text.

    ``wordings`` maps each heading wording (lower-cased, as
    :func:`normalize_heading` leaves it) to the canonical section it stands for.
    Only those headings divide the text, so a section runs from its heading to
    the next known heading or the reference list, subsections included. When a
    section's heading appears more than once (a structured abstract's
    "Results" before the body's), the last one is the section. Returns None
    when the file cannot be read as text.
    """
    marks: list[tuple[int, int, str]]  # (heading start, body start, canonical)
    if path.suffix.lower() in (".tex", ".zip"):
        source = _raw_tex_source(path)
        if source is None:
            return None
        cut = min((m.start() for p in (_TEX_BIBLIOGRAPHY, _TEX_REFERENCE_SECTION) if (m := p.search(source))), default=len(source))
        source = source[:cut]
        marks = _tex_section_marks(source, wordings)
        count = lambda a, b: _word_count(_tex_to_text(source[a:b]))  # noqa: E731
    else:
        text = manuscript_text(path)
        if text is None:
            return None
        source = truncate_at_references(text)
        marks = _text_section_marks(source, wordings)
        count = lambda a, b: _word_count(source[a:b])  # noqa: E731
    counts: dict[str, int] = {}
    for i, (_, body, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(source)
        counts[name] = count(body, end)
    return counts


def _canonical_heading(heading: str, wordings: dict[str, str]) -> str | None:
    return wordings.get(re.sub(r"^\d+\W*", "", heading))


def _tex_section_marks(source: str, wordings: dict[str, str]) -> list[tuple[int, int, str]]:
    """``(heading start, body start, canonical)`` of each known section heading of LaTeX source."""
    marks = []
    for m in _TEX_SECTION.finditer(source):
        name = _canonical_heading(normalize_heading(m.group(1)), wordings)
        if name:
            marks.append((m.start(), m.end(), name))
    return marks


def _text_section_marks(text: str, wordings: dict[str, str]) -> list[tuple[int, int, str]]:
    """``(heading start, body start, canonical)`` of each known heading of extracted text, run-in labels included."""
    marks = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        name = None
        if stripped and len(stripped.split()) <= _HEADING_MAX_WORDS:
            name = _canonical_heading(normalize_heading(stripped), wordings)
        if name:
            marks.append((offset, offset + len(line), name))
        elif stripped:
            run_in = _run_in_heading(stripped)
            name = _canonical_heading(normalize_heading(run_in[0]), wordings) if run_in else None
            if name:
                start = offset + len(line) - len(line.lstrip())
                marks.append((start, start + run_in[1], name))
        offset += len(line)
    return marks


# An abstract is at most a few hundred words; a longer run of "labels" is the paper itself.
_MAX_STRUCTURED_ABSTRACT_CHARS = 5000


def structured_abstract_labels(names: list[str | None], starts: list[int] | None = None) -> set[int]:
    """Which of ``names`` (canonical headings in order, None for other lines) are a structured abstract's labels.

    A structured abstract repeats the body's headings ("Background", "Results"),
    and its labels come straight after the Abstract heading. They run until the
    first section seen a second time, which starts the body. There are at least
    two of them, and with ``starts`` (each heading's offset) they span no more
    than an abstract can: a supplement repeating "Methods" does not make the
    paper before it an abstract.
    """
    start = next((i for i, name in enumerate(names) if name == "Abstract"), None)
    if start is None or not any(names[start + 1 : start + 3]):
        return set()
    seen: set[str] = set()
    labels: set[int] = set()
    for index in range(start + 1, len(names)):
        name = names[index]
        if not name:
            continue
        if name in seen:
            too_long = starts is not None and starts[index] - starts[start] > _MAX_STRUCTURED_ABSTRACT_CHARS
            return labels if len(labels) >= 2 and not too_long else set()
        seen.add(name)
        labels.add(index)
    return set()


# Headings that hold legends or tables, and the captions that stand in for them.
_EXCLUSION_HEADINGS = {"figure legends": "@legends", "figure legend": "@legends", "figure captions": "@legends", "legends": "@legends", "legends to figures": "@legends", "legends for figures": "@legends", "table legends": "@legends", "tables": "@tables"}
_CAPTION_LINE = re.compile(r"^[ \t]*(?:(?:Supplementary|Extended\s+Data)\s+)?(?:Fig(?:ure)?\.?|Table)\s+S?\d+[a-z]?\s*[.:|]", re.IGNORECASE | re.MULTILINE)
_TABLE_MENTION = re.compile(r"\bTables?\s+S?\d", re.IGNORECASE)
_DISPLAY_MENTION = re.compile(r"\b(?:Tables?|Fig(?:ure)?s?\.?)\s+S?\d", re.IGNORECASE)
_MD_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.MULTILINE)
_TEX_ENV_ABSTRACT = re.compile(r"\\begin\s*\{\s*abstract\s*\}.*?\\end\s*\{\s*abstract\s*\}", re.DOTALL)
_TEX_ENV_TABLE = re.compile(r"\\begin\s*\{\s*table\*?\s*\}.*?\\end\s*\{\s*table\*?\s*\}", re.DOTALL)
_TEX_CAPTION = re.compile(r"\\caption\*?\s*(?:\[[^\]]*\])?\s*\{")
_TEX_ABSTRACT_MACRO = re.compile(r"\\abstract\s*\{")
_TEX_ENV_BIBLIOGRAPHY = re.compile(r"\\begin\s*\{\s*thebibliography\s*\}.*?\\end\s*\{\s*thebibliography\s*\}", re.DOTALL)


def _tex_group_end(source: str, opening: int) -> int:
    """The offset just past the brace group opening at ``opening``."""
    depth = 0
    for index in range(opening, len(source)):
        char = source[index]
        if char == "{" and source[index - 1] != "\\":
            depth += 1
        elif char == "}" and source[index - 1] != "\\":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(source)


def _section_spans(marks: list[tuple[int, int, str]], limit: int) -> dict[str, list[tuple[int, int]]]:
    """The ``(start, end)`` of each known section, by canonical name; an abstract keeps its labels."""
    labels = structured_abstract_labels([name for _, _, name in marks], [start for start, _, _ in marks])
    spans: dict[str, list[tuple[int, int]]] = {}
    for index, (start, _, name) in enumerate(marks):
        if index in labels:
            continue
        following = index + 1
        while following in labels and name == "Abstract":
            following += 1
        spans.setdefault(name, []).append((start, marks[following][0] if following < len(marks) else limit))
    return spans


# Canonical headings of the body of a paper, as opposed to its front matter.
_BODY_SECTIONS = frozenset({"Background", "Methods", "Results", "Discussion", "Conclusions"})


def words_excluding(path: Path, wordings: dict[str, str], parts: dict[str, str], endings: tuple[str, ...] = (), whole: bool = False) -> tuple[int, list[str]] | None:
    """The word count with the venue's excluded parts left out, and the parts that could not be found.

    ``parts`` maps each excluded part, as the guidelines name it, to a canonical
    section in ``wordings`` or to a kind of content: ``@references``, ``@front``
    (title and authors), ``@titles`` (headings), ``@legends``, ``@tables``,
    ``@figures``, or ``@supplement``. A part mapped to None cannot be found. The
    count runs to the end of the main text, or of the file with ``whole``. Returns
    None when the file cannot be read as text.
    """
    wordings = {**_EXCLUSION_HEADINGS, **wordings}
    kinds = set(parts.values())
    is_tex = path.suffix.lower() in (".tex", ".zip")
    is_pdf = path.suffix.lower() == ".pdf"
    has_tables = has_captions = False
    spans: list[tuple[int, int]] = []
    if is_tex:
        source = _raw_tex_source(path)
        if not source:
            return None
        marks = _tex_section_marks(source, wordings)
        end = len(source)
        has_references = bool(_TEX_BIBLIOGRAPHY.search(source) or _TEX_REFERENCE_SECTION.search(source))
        abstract = [m.span() for m in _TEX_ENV_ABSTRACT.finditer(source)] + [(m.start(), _tex_group_end(source, m.end() - 1)) for m in _TEX_ABSTRACT_MACRO.finditer(source)]
        has_captions = has_tables = True
        if "@tables" in kinds:
            spans += [m.span() for m in _TEX_ENV_TABLE.finditer(source)]
        if "@legends" in kinds or "@figures" in kinds:
            spans += [(m.start(), _tex_group_end(source, m.end() - 1)) for m in _TEX_CAPTION.finditer(source)]
    else:
        text = docx_to_text(path, drop_tables=True) if path.suffix.lower() == ".docx" and "@tables" in kinds else manuscript_text(path)
        if text is None:
            return None
        source = text
        marks = _text_section_marks(source, wordings)
        ending = main_text_end(source, endings)
        # A venue's own end heading can come first; the reference list is still left out with what follows it.
        has_references = bool(_REFERENCE_HEADING.search(source))
        end = len(source) if whole or ending is None else ending.start()
        abstract = []
        if not is_pdf:
            # A Word caption is one paragraph, a line; Markdown and text paragraphs end at a blank line.
            stop = "\n" if path.suffix.lower() == ".docx" else "\n\n"
            for m in _CAPTION_LINE.finditer(source):
                has_captions = True
                if "@legends" in kinds or "@figures" in kinds:
                    close = source.find(stop, m.start())
                    spans.append((m.start(), len(source) if close < 0 else close))
        has_tables = "@tables" in kinds and path.suffix.lower() == ".docx" and len(source) != len(manuscript_text(path) or "")
        for m in _MD_TABLE_ROW.finditer(source) if "@tables" in kinds else ():
            has_tables = True
            spans.append(m.span())
    by_name = _section_spans(marks, end if is_tex else len(source))
    if abstract:
        by_name.setdefault("Abstract", []).extend(abstract)
    missing: list[str] = []
    for part, kind in parts.items():
        if kind is None:
            missing.append(part)
        elif kind == "@references":
            if not has_references:
                missing.append(part)
            elif whole and is_tex:
                # A whole-document count runs past the bibliography, so it has to be cut out.
                spans += [m.span() for m in _TEX_ENV_BIBLIOGRAPHY.finditer(source)]
            elif whole:
                spans.append(reference_list_span(source))
        elif kind == "@front":
            if marks or abstract:
                spans.append((0, min([marks[0][0]] * bool(marks) + [a for a, _ in abstract])))
            else:
                missing.append(part)
        elif kind == "@titles":
            spans += [(start, body) for start, body, _ in marks]
        elif kind == "@legends":
            spans += by_name.get("@legends", [])
            if not (is_tex or has_captions or "@legends" in by_name or not _DISPLAY_MENTION.search(source)):
                missing.append(part)
        elif kind == "@tables":
            spans += by_name.get("@tables", [])
            if not (is_tex or has_tables or "@tables" in by_name or not _TABLE_MENTION.search(source)):
                missing.append(part)
        elif kind == "@supplement":
            spans += by_name.get("Supplementary information", [])
        elif kind != "@figures":
            if kind in by_name:
                spans += by_name[kind]
                # Leaving the abstract out of a body count leaves out the title block above it too.
                if kind == "Abstract" and not is_tex:
                    first = next(m for m in marks if m[2] == "Abstract")
                    if not any(m[0] < first[0] and m[2] in _BODY_SECTIONS for m in marks):
                        spans.append((0, first[0]))
            else:
                missing.append(part)
    kept: list[str] = []
    position = 0
    for start, stop in sorted(spans):
        if start > position:
            kept.append(source[position:min(start, end)])
        position = max(position, stop)
    kept.append(source[position:end] if position < end else "")
    remainder = "\n".join(kept)
    if is_tex:
        return _word_count(truncate_at_references(_tex_to_text(remainder, keep_bibliography=whole), () if whole else endings)), missing
    return _word_count(remainder), missing


# One reference entry in an extracted reference list: "1. ", "[1] ", "1) ".
_NUMBERED_REF = re.compile(r"^\s*(?:\[(\d{1,4})\]|(\d{1,4})[.)])\s+\S", re.MULTILINE)
# An author-year entry: starts with a capitalised surname and holds a year.
# "Smith J, 2021.", "Smith J (2021). Title", Springer's "Smith J (2021) Title"; not a
# wrapped line that opens with a journal and its volume ("Nature 12, 1-9 (2021).").
_AUTHOR_YEAR_REF = re.compile(r"^(?![A-Z][\w.&]*(?:[ \t]+[A-Z][\w.&]*){0,4}[ \t]+\d{1,4}[ \t]*[,:(;])[A-Z][^\n]{3,200}?(?:\((?:19|20)\d{2}[a-z]?\)[.,:;]?(?=\s)|(?:19|20)\d{2}[a-z]?\)?[.,:])", re.MULTILINE)
_BIB_ENTRY = re.compile(r"^\s*@(?!string\b|comment\b|preamble\b)[A-Za-z]+\s*[{(]", re.MULTILINE | re.IGNORECASE)


_TEX_CITATION = re.compile(r"\\[a-zA-Z]*cite[a-zA-Z]*\*?\s*(?:\[[^\]]*\]\s*)*\{([^{}]*)\}")
_BIB_KEY = re.compile(r"@[A-Za-z]+\s*[{(]\s*([^,\s]+)")
_BIB_MARKUP = re.compile(r"@[A-Za-z]+\s*[{(]\s*[^,\s]+\s*,|\b[A-Za-z_-]+\s*=|[{}\"]")


def _cited_bib_entries(main_tex: Path, source: str) -> list[str] | None:
    """The entries of the ``.bib`` files a LaTeX source names that it actually cites.

    Every entry under ``\\nocite{*}`` or when no citation command is found at all
    (nothing tells the used entries apart). None when no ``.bib`` file is found, or
    the source's citations match nothing a ``.bib`` file holds, so the caller does
    not take that for an empty list.
    """
    names = [n.strip() for m in re.finditer(r"\\(?:bibliography|addbibresource)\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", source) for n in m.group(1).split(",")]
    entries: list[str] = []
    found = False
    for name in names:
        candidate = main_tex.parent / (name if name.lower().endswith(".bib") else name + ".bib")
        try:
            text = candidate.read_text("utf-8", "replace")
        except OSError:
            continue
        found = True
        entries += [e.strip() for e in re.split(r"\n\s*(?=@[A-Za-z]+\s*[{(])", text) if _BIB_ENTRY.match(e)]
    if not found:
        return None
    cited = {key.strip() for m in _TEX_CITATION.finditer(source) for key in m.group(1).split(",")}
    if not cited or "*" in cited:
        return entries
    used = []
    for entry in entries:
        key = _BIB_KEY.match(entry)
        if key and key.group(1) in cited:
            used.append(entry)
    return used or None


# A wrapped line opening with a journal and its volume ("Nature 12, 1-9"), not an entry.
_JOURNAL_VOLUME_LINE = re.compile(r"[A-Z][\w.&]*(?:[ \t]+[A-Z][\w.&]*){0,4}[ \t]+\d{1,4}[ \t]*[,:(;]")
_ENTRY_YEAR = re.compile(r"\b(?:19|20)\d{2}[a-z]?\b")
_ENTRY_CLOSED = re.compile(r"(?:[.)\]]|\d|https?://\S+|doi\S*)\s*$", re.IGNORECASE)


def author_year_starts(tail: str) -> list[int]:
    """Where each entry of an author-year reference list starts in ``tail``; [] when none has a year.

    Extraction wraps entries over lines, and the year can be on any of them, so
    an entry ends only once it holds a year and a line closes it. A line then
    starts the next entry if it opens with a capital and is not a journal and
    volume. A piece with no year of its own goes back to the entry before.
    """
    starts: list[int] = []
    offset = 0
    has_year = closed = False
    for line in tail.splitlines(keepends=True):
        stripped = line.strip()
        if stripped:
            upper = stripped[0].isupper() and not _JOURNAL_VOLUME_LINE.match(stripped)
            if upper and (not starts or (has_year and (closed or _AUTHOR_YEAR_REF.match(stripped)))):
                starts.append(offset + len(line) - len(line.lstrip()))
                has_year = False
            has_year = has_year or bool(_ENTRY_YEAR.search(stripped))
            closed = bool(_ENTRY_CLOSED.search(stripped))
        offset += len(line)
    bounds = starts + [len(tail)]
    kept = [a for i, (a, b) in enumerate(zip(bounds, bounds[1:])) if i == 0 or _ENTRY_YEAR.search(tail[a:b])]
    return kept if _ENTRY_YEAR.search(tail) else []


def _numbered_starts(tail: str) -> list[re.Match[str]]:
    """The numbered-entry matches of a reference list, or [] when it is not a numbered list.

    A wrapped author-year entry can leave "2021." at the start of a line, so a
    list counts as numbered only with two or more entries starting near 1.
    """
    matches = list(_NUMBERED_REF.finditer(tail))
    numbers = [int(m.group(1) or m.group(2)) for m in matches]
    return matches if len(numbers) >= 2 and min(numbers) <= 5 else []


def reference_count(path: Path) -> int | None:
    """Approximate number of entries in the manuscript's reference list, or None.

    LaTeX: the ``\\bibitem`` entries of an inline bibliography, else the
    entries of the ``.bib`` file(s) the source cites. PDF/Word: the numbered entries after the references
    heading, else the author-year entries. None when no reference list is
    found, so the caller does not treat "unmeasured" as "within limit".
    """
    if path.suffix.lower() in (".tex", ".zip"):
        source = _raw_tex_source(path)
        if source is None:
            return None
        items = len(re.findall(r"\\bibitem\b", source))
        if items:
            return items
        if path.suffix.lower() == ".tex":
            entries = _cited_bib_entries(path, source)
            return None if entries is None else len(entries)
        return None
    text = manuscript_text(path)
    if text is None:
        return None
    match = _REFERENCE_HEADING.search(text)
    if match is None:
        return None
    tail = text[match.end() :]
    end = _AFTER_REFERENCES.search(tail)
    if end:
        tail = tail[: end.start()]
    numbered = _numbered_starts(tail)
    if numbered:
        # Use the highest number rather than the match count: a line-wrapped
        # entry can hide its number from the line-anchored pattern.
        highest = max(int(m.group(1) or m.group(2)) for m in numbered)
        return max(highest, len(numbered)) if highest <= len(numbered) * 3 else len(numbered)
    return len(author_year_starts(tail)) or None


# Where a reference list ends in extracted text: a figure/table caption or a
# heading that commonly follows it.
_AFTER_REFERENCES = re.compile(r"^\s*" + _MD_HEADING_OPEN + r"(?:(?:Supplementary\s+|Extended\s+Data\s+)?(?:Fig(?:ure)?\.?|Table)\s+S?\d+\s*[.:|]|Figure\s+legends?\b|Tables?\s*$|Supplementary\s+(?:information|materials?)\b|Appendix\b|(?:Online\s+|Materials\s+and\s+)?Methods\s*$|(?:(?:Data|Code|Software|Materials?)(?:(?:\s*,\s*|\s+and\s+)\w+)*\s+availability|Acknowledge?ments?|Author\s+contributions?|Funding|Competing\s+interests?|Conflicts?\s+of\s+interest|(?:Clinical\s+)?Trial\s+registration)\b\s*(?:\*\*|__)?\s*(?:[:.](?:\s|$)|$)|(?:Ethics(?:\s+statement)?|Declarations?|Additional\s+information)\s*$)", re.IGNORECASE | re.MULTILINE)
_BIB_ITEM = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{[^}]*\}(.*?)(?=\\bibitem|\\end\s*\{\s*thebibliography|$)", re.DOTALL)


def reference_entries(path: Path) -> list[str] | None:
    """The entries of the manuscript's reference list, as text, or None if none is found.

    LaTeX: the ``\\bibitem`` entries, else the ``.bib`` entries the source names.
    PDF/Word: the text after the references heading, up to the first caption
    or closing heading, split into numbered entries ("1.", "[1]") or, failing
    that, into lines that start like an author-year entry.
    """
    from . import document

    doc = document.get(path)
    if doc.is_tex and doc.pdf is None:
        source = doc.tex_source
        if source is None:
            return None
        items = [re.sub(r"\s+", " ", m.group(1)).strip() for m in _BIB_ITEM.finditer(source)]
        if items:
            return items
        if path.suffix.lower() == ".tex":
            return _cited_bib_entries(path, source)
        return None
    text = doc.text
    if text is None:
        return None
    match = _REFERENCE_HEADING.search(text)
    if match is None:
        return None
    tail = text[match.end() :]
    end = _AFTER_REFERENCES.search(tail)
    if end:
        tail = tail[: end.start()]
    starts = [m.start() for m in _numbered_starts(tail)] or author_year_starts(tail)
    if not starts:
        return None
    bounds = starts + [len(tail)]
    entries: list[str] = []
    for a, b in zip(bounds, bounds[1:]):
        entry = re.sub(r"\s+", " ", tail[a:b]).strip()
        # A short fragment ("1843. 1": a year then a page number) belongs to the entry before.
        if entries and len(entry) < 25:
            entries[-1] += " " + entry
        elif entry:
            entries.append(entry)
    return entries


# Lines of extracted text that make up the "title page" of a PDF/Word manuscript.
_TITLE_PAGE_LINES = 80


def title_page_text(path: Path) -> str | None:
    """The front matter of a manuscript, where title-page items live.

    For a PDF this is the first two pages' text; for a Word file the first
    lines; for LaTeX the source before the first sectioning command (the
    ``\\title``/``\\author``/``\\affil``/``\\email`` block plus the abstract),
    kept raw so command names like ``\\orcid`` can be matched too. None when
    the file cannot be read as text.
    """
    suffix = path.suffix.lower()
    if suffix in (".tex", ".zip"):
        source = _raw_tex_source(path)
        if source is None:
            return None
        cut = _TEX_SECTION.search(source)
        return source[: cut.start()] if cut else source
    if suffix == ".pdf":
        pages = _pdf_pages(path)
        if pages is None:
            return None
        return "\n".join(pages[:2]) if any(p.strip() for p in pages[:2]) else None
    text = manuscript_text(path)
    if text is None:
        return None
    return "\n".join(text.splitlines()[:_TITLE_PAGE_LINES])
