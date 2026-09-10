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
import logging
import os
import re
import shutil
import subprocess
import tempfile
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

    Each entry is a literal phrase, so ``venues.json`` lists heading wordings
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
        r"^\s*(?:\d+\.?\s+|[A-Za-z]\.?\s+|[ivxlcIVXLC]+\.?\s+)?" rf"(?:{alternatives})" r"(?:\s+[A-Za-z0-9]{1,3})?" r"\s*:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    )


def main_text_end(text: str, headings: tuple[str, ...] = ()) -> re.Match[str] | None:
    """The earliest heading in ``text`` that ends the main text, or ``None``.

    The reference list always ends it; ``headings`` names the further sections a
    venue excludes from its before-refs limits (see ``main_text_end_headings`` in
    ``venues.json``), such as an appendix or a required statement. Whichever
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

_BUILD_CACHE: dict[tuple[str, int, int], Path | None] = {}
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
    A non-zero exit still yields the PDF when one was produced -- a stray
    overfull box or a missing citation does not change the page count enough to
    matter, and a partial render beats no measurement at all.
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
        _run(["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-f", f"-outdir={outdir}", main.name], main.parent, env, BUILD_TIMEOUT_S)
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


def build_pdf(path: Path) -> Path | None:
    """Compile a ``.tex`` file or ``.zip`` LaTeX bundle to a scratch PDF.

    Returns the path of the rendered PDF in a temporary directory, or None when
    no TeX toolchain is installed, the bundle has no root ``.tex``, or the build
    produced no PDF. The result is cached per (path, size, mtime) for the
    process, so repeated measurements of one manuscript compile once. The
    source directory is never modified: all build products go to the scratch
    directory, which is removed at exit.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path.resolve()), stat.st_size, int(stat.st_mtime))
    if key in _BUILD_CACHE:
        return _BUILD_CACHE[key]
    result: Path | None = None
    if tex_toolchain() is None:
        logger.info("No TeX toolchain (latexmk/pdflatex) found; cannot render %s for a page count", path.name)
    else:
        outdir = _new_build_dir()
        suffix = path.suffix.lower()
        try:
            if suffix == ".tex":
                logger.info("Compiling %s to a scratch PDF for page counting", path.name)
                result = _compile(path, outdir)
            elif suffix == ".zip":
                srcdir = outdir / "src"
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(srcdir)
                main = _main_tex(sorted(srcdir.rglob("*.tex")))
                if main is None:
                    logger.info("%s holds no .tex with \\documentclass; cannot render it", path.name)
                else:
                    logger.info("Compiling %s (from %s) to a scratch PDF for page counting", main.name, path.name)
                    (outdir / "out").mkdir(exist_ok=True)
                    result = _compile(main, outdir / "out")
        except (zipfile.BadZipFile, OSError) as exc:
            logger.debug("Could not build %s: %s", path, exc)
            result = None
        if result is None:
            logger.warning("Could not render %s to PDF; its page count was not checked", path.name)
    _BUILD_CACHE[key] = result
    return result


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
_TEX_SECTION = re.compile(r"\\(?:part|chapter|section|subsection|subsubsection|paragraph)\*?\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{[^{}]*\})*)\}")
# Leading section designators a heading line may carry: "2.", "II.", "A.", "2.1".
_HEADING_PREFIX = re.compile(r"^\s*(?:(?:\d+(?:\.\d+)*\.?|[ivxlcIVXLC]+\.|[A-Z]\.)\s+)?")
# Longest line that can still be a heading, in words.
_HEADING_MAX_WORDS = 8


def normalize_heading(line: str) -> str:
    """A heading line reduced to its lower-cased wording.

    Drops a leading section designator, trailing punctuation, and (for LaTeX)
    residual markup, so ``"2. Materials and Methods:"`` and ``"MATERIALS AND
    METHODS"`` both become ``"materials and methods"``.
    """
    text = _HEADING_PREFIX.sub("", line.strip(), count=1)
    text = re.sub(r"\\[a-zA-Z@]+\*?", " ", text)
    text = re.sub(r"[{}]", "", text)
    text = text.rstrip(" .:;-\u2013\u2014")
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _raw_tex_source(path: Path) -> str | None:
    """The comment-stripped LaTeX source of a ``.tex`` file or ``.zip`` bundle."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".tex":
            return _strip_tex_comments(path.read_text("utf-8", "replace"))
        if suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".tex")]
                names.sort(key=lambda n: (not re.search(r"\b(main|manuscript|ms|paper)\b", n, re.IGNORECASE), n))
                return "\n".join(_strip_tex_comments(zf.read(n).decode("utf-8", "replace")) for n in names)
    except (OSError, zipfile.BadZipFile) as exc:
        logger.debug("Could not read LaTeX source %s: %s", path, exc)
    return None


def headings(path: Path) -> list[str] | None:
    """Candidate section headings of a manuscript, normalised, in order.

    For LaTeX source these are the sectioning commands' titles. For a PDF or
    Word file -- where a heading is just a short line of its own -- every line
    of at most :data:`_HEADING_MAX_WORDS` words is a candidate; the caller
    matches them against the wordings it is looking for, so the over-inclusion
    is harmless. Returns None when the file cannot be read as text.
    """
    if path.suffix.lower() in (".tex", ".zip"):
        source = _raw_tex_source(path)
        if source is None:
            return None
        found = [normalize_heading(m.group(1)) for m in _TEX_SECTION.finditer(source)]
        # Statements are often set as \paragraph{} or as bold run-in text
        # (\textbf{Data availability.}) rather than sections; include those.
        for m in re.finditer(r"\\(?:textbf|textit|emph|noindent\s*\\textbf)\s*\{([^{}]{3,60})\}", source):
            found.append(normalize_heading(m.group(1)))
        return [h for h in found if h]
    text = manuscript_text(path)
    if text is None:
        return None
    found = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or len(stripped.split()) > _HEADING_MAX_WORDS:
            continue
        # A sentence ending in a full stop is prose, not a heading -- but a
        # run-in heading ("Data availability. The data are...") is caught by
        # taking the text before the first sentence break too.
        head = normalize_heading(stripped)
        if head:
            found.append(head)
        if "." in stripped[:-1]:
            first = normalize_heading(stripped.split(".", 1)[0])
            if first and first != head:
                found.append(first)
    return found


# One reference entry in an extracted reference list: "1. ", "[1] ", "1) ".
_NUMBERED_REF = re.compile(r"^\s*(?:\[(\d{1,4})\]|(\d{1,4})[.)])\s+\S", re.MULTILINE)
# An author-year entry: starts with a capitalised surname and holds a year.
_AUTHOR_YEAR_REF = re.compile(r"^[A-Z][^\n]{3,200}?\(?(?:19|20)\d{2}[a-z]?\)?[.,:]", re.MULTILINE)
_BIB_ENTRY = re.compile(r"^\s*@(?!string\b|comment\b|preamble\b)[A-Za-z]+\s*[{(]", re.MULTILINE | re.IGNORECASE)


def _bib_entry_count(main_tex: Path, source: str) -> int | None:
    """Entries in the ``.bib`` files a LaTeX source names, or None if none found."""
    names: list[str] = []
    for m in re.finditer(r"\\(?:bibliography|addbibresource)\s*\{([^}]*)\}", source):
        names.extend(n.strip() for n in m.group(1).split(","))
    if not names:
        return None
    total = 0
    found = False
    for name in names:
        candidate = main_tex.parent / (name if name.lower().endswith(".bib") else name + ".bib")
        try:
            total += len(_BIB_ENTRY.findall(candidate.read_text("utf-8", "replace")))
            found = True
        except OSError:
            continue
    return total if found else None


def reference_count(path: Path) -> int | None:
    """Approximate number of entries in the manuscript's reference list, or None.

    LaTeX: the ``\\bibitem`` entries of an inline bibliography, else the
    entries of the ``.bib`` file(s) the source names (an upper bound: unused
    entries count too). PDF/Word: the numbered entries after the references
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
            return _bib_entry_count(path, source)
        return None
    text = manuscript_text(path)
    if text is None:
        return None
    match = _REFERENCE_HEADING.search(text)
    if match is None:
        return None
    tail = text[match.end() :]
    numbered = _NUMBERED_REF.findall(tail)
    if numbered:
        # Use the highest number rather than the match count: a line-wrapped
        # entry can hide its number from the line-anchored pattern.
        highest = max(int(a or b) for a, b in numbered)
        return max(highest, len(numbered)) if highest <= len(numbered) * 3 else len(numbered)
    author_year = len(_AUTHOR_YEAR_REF.findall(tail))
    return author_year or None


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
        return "\n".join(pages[:2])
    text = manuscript_text(path)
    if text is None:
        return None
    return "\n".join(text.splitlines()[:_TITLE_PAGE_LINES])
