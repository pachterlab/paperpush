"""Scan submission files for information that was never meant to be shared.

Preprint servers and many journals publish an author's *source* files -- LaTeX,
figures, supplementary data -- alongside the compiled PDF. Authors overwhelmingly
think about the PDF and forget that the source is public too, so a large fraction
of submissions ship hidden information: API keys and passwords pasted into a
config, private SSH keys, GPS coordinates baked into figure photos (which can
pinpoint a home address), editable Google-Docs links to internal review notes,
and LaTeX ``%`` comments arguing with a co-author or listing a paper's known
weaknesses. (See Pennekamp et al., *"Hidden Secrets in the arXiv: Discovering,
Analyzing, and Preventing Unintentional Information Disclosure in Source Files of
Scientific Preprints"*, IEEE S&P 2026, arXiv:2604.20927, and arXiv's recommended
sanitiser ``arxiv_latex_cleaner``.)

This module is the sensitive-info back end for ``paperpush validate`` (which
runs it by default; ``--disable-integrity.sensitive`` opts out).
It is deliberately venue-agnostic: the secret / EXIF / metadata / editable-link
detectors apply to *any* file a journal accepts, and the LaTeX-specific checks
(source comments, junk/dangling auxiliary files) simply switch on when the
submission actually references LaTeX source -- directly or inside a ``.zip`` /
``.tar.gz`` source bundle. Every finding is advisory: the point is to show the
author what would become public so they can decide, not to block a submission.

The scanner never prints a secret it finds. A matched credential is masked
(:func:`_mask`) before it reaches a message, so running the check does not itself
leak the value into a terminal, a log file, or CI output.
"""

from __future__ import annotations

import bz2
import gzip
import html
import io
import logging
import lzma
import re
import tarfile
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple
from PIL import Image

logger = logging.getLogger(__name__)

# Don't try to read text out of a member larger than this; a giant data file is
# almost never where a pasted credential hides, and decoding it is wasteful.
MAX_TEXT_BYTES = 5 * 1024 * 1024
# Cap how many archive members we inspect, so a pathological bundle can't hang
# the check. Reported when hit so the truncation is never silent.
MAX_ARCHIVE_MEMBERS = 2000
# How deep an archive inside an archive is followed.
MAX_ARCHIVE_DEPTH = 3

# Text scanning is not gated on an extension allow-list: any file that is not an
# image, archive, or PDF is decoded as candidate text and dropped by _decode() if
# it looks binary. This deliberately catches odd/unknown extensions (a ``.env``,
# a config with no suffix) where a pasted credential is just as likely to hide.

# Extensions PIL can read EXIF from (GPS, camera make/model).
IMAGE_EXTS = {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".webp", ".heic"}
# Source archives an author might attach (e.g. an arXiv LaTeX bundle).
ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".tar.gz", ".bz2", ".tbz2", ".xz", ".txz"}
# LaTeX source extensions -- the presence of any of these turns on the
# comment scan and makes the junk-file check meaningful.
LATEX_EXTS = {".tex", ".ltx", ".cls", ".sty", ".bib"}
# Zip-of-XML documents: their text is read out of the XML parts.
OFFICE_EXTS = {".docx", ".pptx", ".xlsx", ".docm", ".pptm", ".xlsm", ".odt", ".ods", ".odp"}
# Formats that may hold text but that this module cannot open; reported, not skipped.
OPAQUE_EXTS = {".doc", ".ppt", ".xls", ".7z", ".rar"}
# Category of a finding that says a file went unread, so the scan is not a pass.
NOT_SCANNED = "not scanned"

# Auxiliary / editor / VCS files that do not help compile a paper but routinely
# ride along inside a source bundle and leak drafts, build paths, or history.
# (This is the lightweight, dependency-free counterpart to what
# ``arxiv_latex_cleaner`` / ALC-NG remove.)
_JUNK_SUFFIXES = {
    ".aux",
    ".log",
    ".out",
    ".toc",
    ".lof",
    ".lot",
    ".synctex",
    ".synctex.gz",
    ".bak",
    ".orig",
    ".swp",
    ".tmp",
    ".fls",
    ".fdb_latexmk",
    ".blg",
    ".spl",
}
_JUNK_NAMES = {".ds_store", "thumbs.db"}
_JUNK_DIR_MARKERS = ("/.git/", "/.svn/", "/.hg/", "/__macosx/", "/.ipynb_checkpoints/")


@dataclass(frozen=True)
class Finding:
    """One problem found in a submission file.

    ``where`` locates it: the file's display name, plus an archive-member path
    when the hit is inside a bundle (``figures.zip:notes.tex``), or the literal
    ``"submission"`` for a finding about the submission as a whole. ``category``
    is a short human phrase (e.g. ``"private key"``); ``detail`` is an
    already-masked, safe-to-print description.

    Shared with :mod:`paperpush.references`, whose bibliography checks report in
    the same shape, so :func:`paperpush.validate._findings_to_issues` renders
    every advisory pass identically.
    """

    where: str
    category: str
    detail: str


# --- secret / credential patterns -----------------------------------------
#
# (label, compiled pattern, group-index-to-mask). Each pattern is anchored
# enough to keep false positives low: high-signal token shapes (AWS, Google,
# GitHub, Slack, private-key headers) plus a couple of assignment forms that
# only fire on a plausible-looking value. The masked group is the secret itself,
# so the report can point at "an AWS key" without echoing it.

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"), 0),
    ("AWS access key", re.compile(r"\b((?:AKIA|ASIA)[0-9A-Z]{16})\b"), 1),
    ("Google API key", re.compile(r"\b(AIza[0-9A-Za-z_\-]{35})\b"), 1),
    ("GitHub token", re.compile(r"\b(gh[pousr]_[0-9A-Za-z]{36,255}|github_pat_[0-9A-Za-z_]{22,255})\b"), 1),
    ("GitLab token", re.compile(r"\b(glpat-[0-9A-Za-z_\-]{20,})"), 1),
    ("Hugging Face token", re.compile(r"\b(hf_[0-9A-Za-z]{30,})\b"), 1),
    ("bearer token", re.compile(r"(?i)\bbearer\s+([0-9A-Za-z._~+/\-]{20,}=*)"), 1),
    ("Slack token", re.compile(r"\b(xox[baprs]-[0-9A-Za-z-]{10,})\b"), 1),
    ("Stripe key", re.compile(r"\b((?:sk|rk)_live_[0-9A-Za-z]{16,})\b"), 1),
    ("OpenAI/Anthropic key", re.compile(r"\b(sk-(?!learn\b)(?:ant-)?[0-9A-Za-z_\-]{20,})\b"), 1),
    ("JSON Web Token", re.compile(r"\b(eyJ[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,})\b"), 1),
    # Generic "secret = value" / "password: value". Requires quotes or an
    # 8+-char value so a prose sentence ("the password requirements...") or an
    # empty template field ("password:") does not trip it.
    (
        "hardcoded credential",
        re.compile(r"""(?ix)
            (?<![a-z0-9])                   # "_" may precede: OPENAI_API_KEY, DB_PASSWORD
            (?:pass(?:word|wd|phrase)?|secret|api(?:\\?[_-])?key|access(?:\\?[_-])?key|
               (?:auth|access|api|hf|github)(?:\\?[_-])?token|client(?:\\?[_-])?secret|private(?:\\?[_-])?token)\b
            [ \t]*[:=][ \t]*                # separator, no line break -- the
                                            # value must sit on the same line
            (?P<val>"[^"\n]{6,}"|'[^'\n]{6,}'|[^\s"'#,;]{8,})
            """),
        0,  # mask the whole match; the value alone can be ambiguous to slice
    ),
    # A URL with an inline password: scheme://user:pass@host
    ("URL with embedded password", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:([^\s:/@]{3,})@"), 1),
]

# Editable web documents linked for "internal coordination" -- Google Docs /
# Sheets / Slides / Forms in edit mode expose review notes, rebuttals, minutes,
# and sometimes participant data to anyone who clicks.
_EDITABLE_LINK = re.compile(r"https?://docs\.google\.com/(?:document|spreadsheets|presentation|forms)/d/[\w\-]+[^\s)\]}>\"']*")

# LaTeX note markers -- the comments most likely to embarrass (to-dos, jabs at
# reviewers/competitors, admissions of weakness).
_LATEX_NOTE = re.compile(r"(?i)\b(TODO|FIXME|XXX|HACK|WTF|REVIEWER|CONFIDENTIAL|DO NOT|REMOVE THIS|placeholder)\b")


# A value on the right of "password =" that is code or a template, not a credential.
_PLACEHOLDER_VALUE = re.compile(r"(?i)^(?:<.*>|\$.*|%.*|\{\{?.*\}?\}|none|null|true|false)$|your[_-]|change[_-]?me|x{4,}|placeholder|redacted")
_CODE_VALUE = re.compile(r"[(\[]|^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")


# "Forward pass: activations are cached": the shorter keywords also end ordinary labels.
_PROSE_LABEL = re.compile(r"(?i)(?:pass|secret)[ \t]*:[ \t]*[a-z]+$")


def _is_placeholder_credential(value: str) -> bool:
    quoted = value[:1] in "\"'"
    bare = value.strip("\"'")
    return bool(_PLACEHOLDER_VALUE.search(bare)) or (not quoted and bool(_CODE_VALUE.search(bare)))


def _mask(secret: str) -> str:
    """Redact a matched secret to a short, non-reversible hint.

    Long values keep their first four and last two characters
    (``AKIA…xz``); short ones collapse to ``****``. Never returns enough of the
    value to be usable, so a finding can be printed and logged safely.
    """
    secret = secret.strip().strip("\"'")
    # Collapse a multi-line match (e.g. a key header) to its first line.
    secret = secret.splitlines()[0]
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}…{secret[-2:]}"


_EDITABLE_ID = re.compile(r"(/d/)([\w\-]+)")
_URL_USERINFO = re.compile(r"(?<=://)[^/\s@]+@")


def _redact(text: str) -> str:
    """``text`` with every credential in it masked, for quoting it in a finding."""
    for _, pattern, group in _SECRET_PATTERNS:

        def mask(match: re.Match[str], group: int = group) -> str:
            raw = match.group(group) or match.group(0)
            return match.group(0).replace(raw, _mask(raw))

        text = pattern.sub(mask, text)
    return text


def _strip_userinfo(url: str) -> str:
    """``url`` without a ``user:password@`` (or ``token@``) part."""
    return _URL_USERINFO.sub("", url)


def _not_scanned(where: str, why: str, prefix: str = "") -> Finding:
    return Finding(where, NOT_SCANNED, f"{prefix}could not be scanned ({why}); inspect it by hand before submitting")


# Identifiers of a study participant or patient written into the text. Each needs
# its label next to it, so a bare number or date never matches.
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = rf"(?:\d{{1,2}}[ ./-]{_MONTH}[ ./-]\d{{4}}|\d{{1,2}}\s+{_MONTH}\s+\d{{4}}|{_MONTH}\s+\d{{1,2}},?\s+\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}})"
_PERSONAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("medical record / patient number", re.compile(r"(?i)\b(?:medical record (?:number|no\.?)|MRN|patient (?:id|identifier|number|no\.?)|hospital number)\s*[:#]?\s*([A-Z]?\d{5,})\b")),
    ("date of birth", re.compile(rf"(?i)\b(?:date of birth|d\.?o\.?b\.?|born(?: on)?)\s*[:,]?\s*({_DATE})")),
    ("social security number", re.compile(r"(?i)\b(?:SSN|social security(?: number| no\.?)?)\s*[:#]?\s*(\d{3}-\d{2}-\d{4})\b")),
    ("phone number", re.compile(r"(?i)\b(?:tel\.?|phone|mobile|cell)\s*[:.]?\s*(\+?\d[\d ()./-]{7,}\d)")),
]


def _iter_personal_findings(where: str, text: str) -> Iterator[Finding]:
    """Yield a finding for each labeled personal identifier (patient number, date of birth, SSN, phone)."""
    seen: set[str] = set()
    for label, pattern in _PERSONAL_PATTERNS:
        for match in pattern.finditer(text):
            if label == "phone number" and re.search(r"(?i)\b(?:corresponding|correspondence|e-?mail|fax)\b", text[max(0, match.start() - 120):match.start()]):
                continue  # an author's contact line, not a participant's data
            raw = match.group(1)
            if raw in seen:
                continue
            seen.add(raw)
            yield Finding(where, "personal data", f"possible personal data: {label} ({_mask(raw)}); remove identifying details of study participants before submitting")


def _iter_secret_findings(where: str, text: str) -> Iterator[Finding]:
    """Yield credential / editable-link findings for one blob of text."""
    yield from _iter_personal_findings(where, text)
    seen: set[tuple[str, str]] = set()
    for label, pattern, group in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if label == "hardcoded credential" and (_is_placeholder_credential(match.group("val")) or _PROSE_LABEL.match(match.group(0))):
                continue
            raw = match.group(group) or match.group(0)
            key = (label, raw)
            if key in seen:
                continue
            seen.add(key)
            yield Finding(where, label, f"looks like {label} ({_mask(raw)})")
    for match in _EDITABLE_LINK.finditer(text):
        link = match.group(0)
        # An /edit or default sharing link grants access; a /d/ID without a mode
        # is still worth flagging since arXiv's study found many were editable.
        # The document id is the capability, so it is masked like a key.
        shown = _EDITABLE_ID.sub(lambda m: m.group(1) + _mask(m.group(2)), link)
        yield Finding(where, "editable document link", f"editable web-document link exposed: {shown}")


def _iter_latex_findings(where: str, text: str) -> Iterator[Finding]:
    """Yield LaTeX-comment findings for a ``.tex``/``.sty``/... source blob.

    Reports two things: any comment carrying a note marker (TODO, a jab at a
    reviewer, "remove this"), quoted so the author sees exactly what leaks; and,
    once, a tally of *all* comment lines with a nudge to run a sanitiser, because
    the arXiv study showed even ordinary comments (co-author arguments, admitted
    weaknesses) routinely embarrass.
    """
    total = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        comment = _latex_comment(line)
        if comment is None:
            continue
        total += 1
        if _LATEX_NOTE.search(comment):
            snippet = _redact(comment.strip())
            if len(snippet) > 80:
                snippet = snippet[:77] + "..."
            yield Finding(where, "LaTeX note comment", f"line {lineno}: % {snippet}")
    if total:
        yield Finding(
            where,
            "LaTeX comments",
            f"{total} source comment line(s) will be published with the LaTeX source; " "strip them (e.g. `arxiv_latex_cleaner /path/to/latex`) before submitting",
        )


def _latex_comment(line: str) -> str | None:
    """Return the comment text after an unescaped ``%``, or None if the line has none.

    A ``%`` preceded by a backslash is a literal percent sign, not a comment
    start, so ``50\\% faster`` is not a comment while ``x = 1 % debug`` is.
    """
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\":
            i += 2  # skip the escaped character (covers \%)
            continue
        if ch == "%":
            return line[i + 1 :]
        i += 1
    return None


def _decode(data: bytes) -> str | None:
    """Best-effort text decode of a blob, or None if it looks binary.

    A NUL byte in the first chunk is a strong binary signal (images, compiled
    objects); such blobs are left to the EXIF/PDF paths instead of being scanned
    as text.
    """
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:1024]:
        return None
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - decode with replace shouldn't raise
        return None


# --- per-format scanners ---------------------------------------------------


def _office_parts(data: bytes) -> list[tuple[str, str]] | None:
    """``(part name, plain text)`` of each XML part of an Office file, or None if it cannot be opened."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            parts: list[tuple[str, str]] = []
            for info in zf.infolist():
                name = info.filename
                if info.file_size > MAX_TEXT_BYTES or not name.lower().endswith((".xml", ".rels")) or "theme/" in name:
                    continue
                xml = zf.read(info).decode("utf-8", errors="replace")
                if name.endswith(".rels"):
                    # Hyperlink targets, which appear nowhere in the document text.
                    parts.append((name, "\n".join(_OFFICE_TARGET.findall(xml))))
                    continue
                text = _OFFICE_BREAK.sub("\n", _OFFICE_SPACE.sub(" ", xml))
                parts.append((name, html.unescape(_OFFICE_TAG.sub("", text))))
            return parts
    except Exception:
        logger.debug("could not open an Office file", exc_info=True)
        return None


_OFFICE_TARGET = re.compile(r'Target="([^"]+)"')
_OFFICE_SPACE = re.compile(r"<w:(?:tab|br|cr)\b[^>]*/>")
_OFFICE_BREAK = re.compile(r"</(?:w:p|a:p|text:p|text:h|si|c|row)>")
_OFFICE_TAG = re.compile(r"<[^>]+>")


def _scan_office(where: str, data: bytes) -> Iterator[Finding]:
    parts = _office_parts(data)
    if parts is None:
        yield _not_scanned(where, "not a readable Office file")
        return
    yield from _iter_secret_findings(where, "\n".join(text for _, text in parts))


@contextmanager
def _member_file(name: str, data: bytes) -> Iterator[Path]:
    """``data`` as a temporary file that has the extension(s) of ``name``."""
    with tempfile.TemporaryDirectory(prefix="paperpush-scan-") as folder:
        path = Path(folder) / ("member" + "".join(Path(name).suffixes[-2:]))
        path.write_bytes(data)
        yield path


def _scan_text_blob(where: str, data: bytes, *, is_latex: bool) -> Iterator[Finding]:
    if len(data) > MAX_TEXT_BYTES:
        yield _not_scanned(where, f"larger than {MAX_TEXT_BYTES // (1024 * 1024)} MB")
        return
    text = _decode(data)
    if text is None:
        return
    yield from _iter_secret_findings(where, text)
    # A .bib's "%" is not a LaTeX comment (BibTeX has none; inside a field it is
    # literal text, e.g. in a URL), so only real LaTeX files get the comment pass.
    if is_latex and not where.lower().endswith(".bib"):
        yield from _iter_latex_findings(where, text)


def _scan_image_blob(where: str, data: bytes) -> Iterator[Finding]:
    """Report GPS coordinates (and, softly, camera identity) from image EXIF."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            exif = img.getexif()
    except Exception:
        logger.debug("could not read EXIF from %s; skipping", where, exc_info=True)
        return
    if not exif:
        return
    gps = _gps_from_exif(exif)
    if gps is not None:
        lat, lon = gps
        yield Finding(
            where,
            "GPS location",
            f"image embeds GPS coordinates ({lat:.5f}, {lon:.5f}); this can reveal a home/work address",
        )


def _gps_from_exif(exif) -> tuple[float, float] | None:
    """Decode (lat, lon) in signed decimal degrees from an EXIF block, or None.

    GPS lives in a sub-IFD (tag ``0x8825``); latitude/longitude are three
    rationals (deg, min, sec) with a N/S / E/W reference that sets the sign.
    """
    try:
        gps_ifd = exif.get_ifd(0x8825)
    except Exception:
        logger.debug("could not read GPS IFD from EXIF; skipping", exc_info=True)
        return None
    if not gps_ifd:
        return None
    # GPS tag ids: 1 LatRef, 2 Lat, 3 LonRef, 4 Lon.
    lat = _dms_to_deg(gps_ifd.get(2))
    lon = _dms_to_deg(gps_ifd.get(4))
    if lat is None or lon is None:
        return None
    if str(gps_ifd.get(1, "N")).upper().startswith("S"):
        lat = -lat
    if str(gps_ifd.get(3, "E")).upper().startswith("W"):
        lon = -lon
    return lat, lon


def _dms_to_deg(dms) -> float | None:
    try:
        d, m, s = (float(x) for x in dms)
    except (TypeError, ValueError):
        return None
    return d + m / 60.0 + s / 3600.0


class PdfRead(NamedTuple):
    """A PDF's text layer, why it could not be read (or None), and its page count."""

    text: str
    problem: str | None
    pages: int


def _read_pdf(path: Path) -> PdfRead:
    """Extract the concatenated text layer of a PDF, saying so when it cannot be read."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import FileNotDecryptedError
    except Exception:
        logger.debug("pypdf not available; skipping PDF text extraction of %s", path)
        return PdfRead("", "pypdf is not installed", 0)
    try:
        reader = PdfReader(str(path))
        pages = list(reader.pages)
    except FileNotDecryptedError:
        return PdfRead("", "password-protected", 0)
    except Exception:
        logger.debug("could not parse PDF %s for text extraction; skipping", path, exc_info=True)
        return PdfRead("", "not a readable PDF", 0)
    parts: list[str] = []
    failed = 0
    for page in pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            logger.debug("could not extract text from a page of %s; skipping page", path, exc_info=True)
            failed += 1
    return PdfRead("\n".join(parts), f"{failed} of {len(pages)} pages could not be read" if failed else None, len(pages))


def _pdf_text(path: Path) -> str:
    """The concatenated text layer of a PDF (``""`` on any failure).

    Shared by the secret scan and the GitHub-link scan so a PDF is parsed the
    same way for both.
    """
    return _read_pdf(path).text


def _pdf_metadata_text(path: Path) -> dict[str, str]:
    """The PDF's ``/Info`` dictionary and XMP creators as ``{key: str(value)}`` (``{}`` on failure)."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        out = {str(k): str(v) for k, v in dict(reader.metadata or {}).items() if v}
    except Exception:
        logger.debug("could not read PDF metadata from %s; skipping", path, exc_info=True)
        return {}
    try:
        xmp = reader.xmp_metadata
        creators = [str(c) for c in (xmp.dc_creator or []) if c] if xmp is not None else []
    except Exception:
        logger.debug("could not read XMP metadata from %s; skipping", path, exc_info=True)
        creators = []
    if creators:
        out["/XMP dc:creator"] = ", ".join(creators)
    return out


def _scan_pdf(where: str, path: Path) -> Iterator[Finding]:
    """Extract text and document metadata from a PDF and scan both.

    A PDF is a compiled artifact, but pasted credentials survive into its text
    layer, and its ``/Info`` metadata often carries author names, the LaTeX
    source path, or the producing tool -- worth a look on the same pass.
    """
    read = _read_pdf(path)
    if read.problem:
        yield _not_scanned(where, read.problem)
    if read.text:
        yield from _iter_secret_findings(where, read.text)
    for key, value in _pdf_metadata_text(path).items():
        yield from _iter_secret_findings(f"{where} [metadata {key}]", value)


# --- archive expansion -----------------------------------------------------


def _scan_archive(where: str, path: Path) -> Iterator[Finding]:
    """Scan the members of a ``.zip`` / ``.tar*`` source bundle.

    Text/LaTeX members get the secret + comment scan and image members the EXIF
    scan; on top of that, member *names* are checked for junk/VCS files that do
    not belong in a published source bundle.
    """
    count = 0
    truncated = False
    unread: list[str] = []
    try:
        for name, data in _iter_archive_members(path):
            count += 1
            if count > MAX_ARCHIVE_MEMBERS:
                truncated = True
                break
            if data is UNREAD:
                unread.append(name)
            else:
                yield from _scan_member(f"{where}:{name}", name, data)
            yield from _junk_member_finding(where, name)
    except Exception as exc:
        logger.debug("could not read archive %s", path, exc_info=True)
        yield _not_scanned(where, f"the archive could not be read to its end: {type(exc).__name__}")
    if unread:
        yield _not_scanned(where, _unread_summary(unread))
    if truncated:
        yield Finding(
            where,
            "archive too large",
            f"only the first {MAX_ARCHIVE_MEMBERS} archive entries were scanned; inspect the rest manually",
        )


def _unread_summary(names: list[str]) -> str:
    shown = ", ".join(names[:3]) + (", ..." if len(names) > 3 else "")
    return f"{len(names)} archive entr{'y' if len(names) == 1 else 'ies'} too large or unreadable: {shown}"


def _scan_member(where: str, name: str, data: bytes) -> Iterator[Finding]:
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_EXTS:
        yield from _scan_image_blob(where, data)
    elif suffix in OFFICE_EXTS:
        yield from _scan_office(where, data)
    elif suffix in OPAQUE_EXTS:
        yield _not_scanned(where, f"{suffix} files cannot be read")
    elif suffix == ".pdf" and data:
        with _member_file(name, data) as pdf:
            yield from _scan_pdf(where, pdf)
    else:
        # Treat anything not obviously an image as candidate text; _decode drops
        # binary blobs. is_latex keys the comment scan off the member extension.
        yield from _scan_text_blob(where, data, is_latex=suffix in LATEX_EXTS)


def _junk_member_finding(where: str, name: str) -> Iterator[Finding]:
    lowered = "/" + name.lower().replace("\\", "/")
    base = Path(name).name.lower()
    if any(marker in lowered for marker in _JUNK_DIR_MARKERS):
        yield Finding(where, "unnecessary file", f"version-control/editor artifact in bundle: {name}")
        return
    if base in _JUNK_NAMES or base.endswith(tuple(_JUNK_SUFFIXES)) or base.endswith("~"):
        yield Finding(where, "unnecessary file", f"build/auxiliary file in bundle: {name}")


class _Unread(bytes):
    """Empty contents of an archive member that was too large or could not be read."""


UNREAD = _Unread()


def _expanded(name: str, data: bytes, depth: int) -> Iterator[tuple[str, bytes]]:
    """One archive member and, for an archive inside it, the members of that (``outer!inner``)."""
    if data is UNREAD or Path(name).suffix.lower() not in ARCHIVE_EXTS:
        yield name, data
        return
    if depth >= MAX_ARCHIVE_DEPTH:
        yield name, UNREAD
        return
    yield name, b""  # name still checked for junk
    if not data:
        return
    try:
        with _member_file(name, data) as nested:
            for inner, inner_data in _iter_archive_members(nested, depth + 1):
                yield f"{name}!{inner}", inner_data
    except Exception:
        logger.debug("could not read nested archive %s", name, exc_info=True)
        yield name, UNREAD


def _iter_archive_members(path: Path, depth: int = 0) -> Iterator[tuple[str, bytes]]:
    """Yield ``(member_name, bytes)`` for an archive, following archives inside it.

    A zip, a tar (compressed or not), or one gzip/bzip2/xz-compressed file. A
    member too large to read, or unreadable, comes back as :data:`UNREAD`.
    """
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    yield info.filename, b""
                    continue
                if info.file_size > MAX_TEXT_BYTES:
                    yield info.filename, UNREAD  # name still checked for junk
                    continue
                try:
                    data = zf.read(info)
                except Exception:
                    logger.debug("could not read zip member %s from %s; scanning name only", info.filename, path, exc_info=True)
                    data = UNREAD
                yield from _expanded(info.filename, data, depth)
        return
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as tf:
            for member in tf:
                if not member.isfile():
                    yield member.name, b""
                    continue
                if member.size > MAX_TEXT_BYTES:
                    yield member.name, UNREAD
                    continue
                try:
                    fh = tf.extractfile(member)
                    data = fh.read() if fh else b""
                except Exception:
                    logger.debug("could not read tar member %s from %s; scanning name only", member.name, path, exc_info=True)
                    data = UNREAD
                yield from _expanded(member.name, data, depth)
        return
    opener = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}.get(path.suffix.lower())
    if opener is None:
        raise ValueError(f"not a supported archive: {path}")
    with opener(path, "rb") as fh:
        data = fh.read(MAX_TEXT_BYTES + 1)
    yield from _expanded(path.stem, UNREAD if len(data) > MAX_TEXT_BYTES else data, depth)


# --- optional bandit pass on Python sources --------------------------------


@lru_cache(maxsize=1)
def _bandit_available() -> bool:
    """Whether the (optional) ``bandit`` package can be imported.

    bandit is not a hard dependency -- it was dropped from the install to keep
    the base footprint small -- so the Python pass is best-effort. Cached because
    the answer can't change within a process, and used both to gate the pass and
    to decide whether to nudge the user to ``pip install bandit`` (see
    :func:`scan_paths`).
    """
    try:
        from bandit.core import config as _  # noqa: F401
        from bandit.core import manager as _m  # noqa: F401
    except Exception:
        return False
    return True


def _scan_python_with_bandit(where: str, path: Path) -> Iterator[Finding]:
    """Run bandit over a ``.py`` file, surfacing hardcoded-secret findings.

    Supplementary code is a common attachment, and bandit's B105-B107 checks
    catch hardcoded passwords the generic regex might miss. The whole pass is
    best-effort: if bandit is absent or its API shifts, the check is skipped
    (the regex scan already ran on the same file as text) and :func:`scan_paths`
    surfaces a one-time ``pip install bandit`` nudge instead.
    """
    if not _bandit_available():
        return
    from bandit.core import config as b_config
    from bandit.core import manager as b_manager

    try:
        mgr = b_manager.BanditManager(b_config.BanditConfig(), "file")
        mgr.discover_files([str(path)])
        mgr.run_tests()
        results = mgr.get_issue_list()
    except Exception:
        logger.debug("bandit failed on %s", where, exc_info=True)
        return
    for issue in results:
        try:
            test_id = issue.test_id
            text = issue.text
            lineno = issue.lineno
        except Exception:
            logger.debug("skipping a bandit issue on %s with an unexpected shape", where, exc_info=True)
            continue
        # Focus on the credential-relevant checks so bandit doesn't flood the
        # report with generic lint (asserts, subprocess use, etc.).
        if test_id in {"B105", "B106", "B107", "B108", "B324", "B501"}:
            # B105-B107 quote the password they found.
            text = re.sub(r"'[^']*'", "'****'", text) if test_id in {"B105", "B106", "B107"} else _redact(text)
            yield Finding(where, "bandit", f"line {lineno}: {text} [{test_id}]")


# --- public entry points ---------------------------------------------------


def scan_file(path: Path) -> list[Finding]:
    """Return every :class:`Finding` in a single submission file.

    Dispatches on the file's extension: images get the EXIF scan, PDFs the
    text+metadata scan, archives are expanded and their members scanned, and
    everything text-like gets the secret / editable-link scan plus (for LaTeX
    source) the comment scan. A file that cannot be read is reported as a
    :data:`NOT_SCANNED` finding, so one unreadable attachment neither aborts the
    scan nor passes as clean.
    """
    findings: list[Finding] = []
    try:
        suffix = path.suffix.lower()
        name = path.name
        # Two-part archive suffix (.tar.gz / .tar.bz2) that .suffix misses.
        double = "".join(path.suffixes[-2:]).lower()
        if suffix in ARCHIVE_EXTS or double in ARCHIVE_EXTS:
            findings.extend(_scan_archive(name, path))
        elif suffix == ".pdf":
            findings.extend(_scan_pdf(name, path))
        elif suffix in IMAGE_EXTS:
            findings.extend(_scan_image_blob(name, path.read_bytes()))
        elif suffix in OFFICE_EXTS:
            findings.extend(_scan_office(name, path.read_bytes()))
        elif suffix in OPAQUE_EXTS:
            findings.append(_not_scanned(name, f"{suffix} files cannot be read"))
        else:
            data = path.read_bytes()
            findings.extend(_scan_text_blob(name, data, is_latex=suffix in LATEX_EXTS))
            if suffix == ".py":
                findings.extend(_scan_python_with_bandit(name, path))
    except FileNotFoundError:
        logger.debug("%s vanished before the sensitive-info scan", path)  # the file checks report it
    except Exception as exc:
        logger.debug("could not read %s for sensitive-info scan", path, exc_info=True)
        findings.append(_not_scanned(path.name, type(exc).__name__))
    return findings


def _dedup_paths(paths: Iterable[Path]) -> list[Path]:
    """De-duplicate a path iterable by resolved location, preserving first order.

    A file referenced by more than one field (or the same path twice) should be
    inspected once. Shared by :func:`scan_paths` and :func:`scan_github` so both
    passes agree on the set of files.
    """
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def scan_paths(paths: Iterable[Path]) -> list[Finding]:
    """Scan an iterable of files for sensitive information, de-duplicating repeats."""
    findings: list[Finding] = []
    python_files = 0
    for path in _dedup_paths(paths):
        if path.suffix.lower() == ".py":
            python_files += 1
        findings.extend(scan_file(path))
    # bandit is optional and off by default; when a submission actually ships
    # Python source but bandit isn't installed, say so once (rather than silently
    # skipping the deeper pass) and point at the one-line fix.
    if python_files and not _bandit_available():
        noun = "file" if python_files == 1 else "files"
        findings.append(
            Finding(
                "submission",
                "bandit not installed",
                f"{python_files} Python {noun} scanned with built-in checks only; " "install bandit (`pip install bandit`) for deeper hardcoded-secret " "detection (bandit B105-B107)",
            )
        )
    return findings


# --- link checking ---------------------------------------------------------
#
# Two independent concerns about the URLs a manuscript cites:
#   * reachability -- does the link actually resolve for an anonymous reader?
#     A dead DOI, a moved page, or a still-private repository all read as broken.
#     This is the default ``scan_links`` pass.
#   * code availability -- does a paper with code link a public repository at
#     all? A separate nudge under the sensitive-info scan (``scan_missing_code_link``).

# Any http(s) URL. The trailing-char class stops the match before common
# sentence/markup punctuation; a further rstrip below trims the rest.
_URL_RE = re.compile(r"https?://[^\s<>\"'`)\]}]+", re.IGNORECASE)
_URL_TRAILING = ".,);:]}'\"”’>"

# github.com/owner/repo, for the code-availability nudge and for wording a
# broken github link as a private/removed repo rather than a generic dead link.
# Other places a paper's code is linked from, including the anonymized mirror double-blind venues use.
_CODE_HOST_RE = re.compile(r"(?i)\b(?:gitlab\.com|bitbucket\.org|codeberg\.org|anonymous\.4open\.science/r)/\S+")
_GITHUB_RE = re.compile(r"(?i)github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)/([A-Za-z0-9._\-]+)")
# github.com/<x> paths that are site sections, not user/org accounts.
_GITHUB_RESERVED_OWNERS = {
    "about",
    "features",
    "pricing",
    "marketplace",
    "sponsors",
    "settings",
    "topics",
    "collections",
    "trending",
    "orgs",
    "enterprise",
    "login",
    "join",
    "new",
    "notifications",
    "search",
    "apps",
    "contact",
    "site",
    "security",
    "readme",
    "explore",
    "customer-stories",
    "account",
}
# Cap the number of distinct URLs we hit the network for, so a link-heavy
# bibliography can't turn validation into a crawl.
MAX_LINK_CHECKS = 50
# Probes are pure socket waits, so they overlap well; this many run at once.
LINK_CHECK_WORKERS = 10
# Per-request budget. Kept short because an unreachable-but-not-404 host tells
# us nothing anyway -- waiting longer only slows validation down.
LINK_CHECK_TIMEOUT = 3.0
# 404/410 are the only codes we treat as definitively broken. Others (403 bot
# blocks, 405 no-HEAD, 5xx blips, timeouts, DNS) are inconclusive -> stay quiet.
_BROKEN_STATUS = {404, 410}
# Codes that mean "this server dislikes HEAD", not "this URL is missing" -- the
# only case worth spending a second request on.
_HEAD_REJECTED_STATUS = {400, 403, 405, 406, 501}
# LaTeX support files the link scan skips. Their URLs aren't the submission's
# claims: a .bib holds the works being cited, and a .cls/.sty/.bst is a journal
# or template author's boilerplate. Left in, a bibliography's citation URLs also
# crowd out the manuscript's own links under the MAX_LINK_CHECKS budget.
LINK_SCAN_SKIP_EXTS = {".bib", ".bst", ".cls", ".sty"}


def _find_urls(text: str) -> Iterator[str]:
    """Yield each cleaned http(s) URL in ``text`` (trailing punctuation trimmed).

    A "URL" whose host has no letters ("https://477", a line number after a
    URL scheme cut at a line break) is not a link and is skipped.
    """
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(_URL_TRAILING)
        host = re.sub(r"(?i)^https?://", "", url).split("/")[0]
        if url and re.search(r"[A-Za-z]", host) and "." in host:
            yield url


def _find_github_repos(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(owner, repo)`` for each GitHub repository URL in ``text``."""
    for match in _GITHUB_RE.finditer(text):
        owner = match.group(1)
        repo = match.group(2).rstrip(".,);:]}'\"")
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
        if not repo or repo == "." or owner.lower() in _GITHUB_RESERVED_OWNERS:
            continue
        yield owner, repo


def _url_is_reachable(url: str, *, timeout: float = LINK_CHECK_TIMEOUT) -> bool | None:
    """Whether an anonymous visitor can reach ``url``.

    ``True`` if it resolves, ``False`` only on a 404/410 (missing/gone -- which
    also covers a private GitHub repo, invisible to an anonymous reader), and
    ``None`` when inconclusive (offline, timeout, 403 bot-block, a server that
    rejects HEAD, a 5xx blip) so the caller stays silent rather than crying wolf.

    Sends a HEAD, and retries with GET only when the server answered with a code
    that means it dislikes HEAD. A transport-level failure (timeout, DNS,
    refused connection) ends the probe there: the host is unreachable for this
    run either way, and a second request would just double the wait.
    """
    import urllib.error
    import urllib.request

    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": "paperpush-validate"})
        try:
            with urllib.request.urlopen(req, timeout=timeout):  # scheme constrained to http(s) by _URL_RE  # nosec B310
                return True
        except urllib.error.HTTPError as exc:
            if exc.code in _BROKEN_STATUS:
                return False
            if method == "HEAD" and exc.code in _HEAD_REJECTED_STATUS:
                continue  # server rejects HEAD; try GET before giving up
            return None
        except Exception:
            logger.debug("could not check reachability of %s", url, exc_info=True)
            return None
    return None


def _broken_link_finding(where: str, url: str) -> Finding:
    """Phrase a broken link, specialising the message for a GitHub repo URL."""
    if any(_find_github_repos(url)):
        return Finding(
            where,
            "private repository",
            f"GitHub link {_redact(url)} is not publicly accessible (private, renamed, or removed); " "make the repository public before submitting so reviewers and readers can reach it",
        )
    return Finding(where, "broken link", f"link {_redact(url)} is not reachable (returned 404/gone); check the URL before submitting")


def scan_links(paths: Iterable[Path], *, check_access: bool = True, stats: dict | None = None) -> list[Finding]:
    """Report URLs in the manuscript files that an anonymous reader can't reach.

    Collects every http(s) URL from the text of the submission files
    (LaTeX/source, PDF text, archive members), de-duplicates them, and probes
    each for reachability, flagging only the definitively broken ones (404/gone,
    including still-private GitHub repos). ``check_access=False`` collects the
    URLs but skips the network probes (used by tests and offline callers).

    The probes are pure network waits, so they run on a small thread pool --
    a link-heavy bibliography would otherwise dominate validation's runtime.
    Findings stay in the order the URLs were encountered.

    ``stats``, if given, is filled with ``probed`` (how many URLs were probed)
    and ``unanswered`` (those with no definite answer), so a caller can tell a
    clean scan from one that could not look.
    """
    urls: dict[str, str] = {}
    for path in _dedup_paths(paths):
        # A PDF's link annotations hold the exact targets. URLs read from its
        # text can be mangled: a review copy's margin line numbers glued on
        # ("github.com/org/repo.043"), or a URL cut at a line break.
        annotated = _pdf_uris(path) if path.suffix.lower() == ".pdf" else []
        for url in annotated:
            urls.setdefault(_strip_userinfo(url), path.name)
        for where, text in _iter_text_blobs(path):
            for url in _find_urls(text):
                if annotated and _covered_by_annotation(url, annotated):
                    continue
                # Credentials in a URL are never sent anywhere or printed.
                urls.setdefault(_strip_userinfo(url), where)

    if not check_access:
        return []

    checked = list(urls.items())[:MAX_LINK_CHECKS]
    if not checked:
        return []

    from concurrent.futures import ThreadPoolExecutor

    workers = min(LINK_CHECK_WORKERS, len(checked))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="paperpush-linkcheck") as pool:
        # map() keeps results aligned with `checked`, so findings stay ordered.
        reachable = list(pool.map(lambda item: _url_is_reachable(item[0]), checked))

    if stats is not None:
        stats["probed"] = len(checked)
        stats["unanswered"] = [_redact(url) for (url, _), ok in zip(checked, reachable) if ok is None]
    return [_broken_link_finding(where, url) for (url, where), ok in zip(checked, reachable) if ok is False]


def _pdf_uris(path: Path) -> list[str]:
    """The http(s) targets of a PDF's link annotations, in page order."""
    try:
        from pypdf import PdfReader

        out: list[str] = []
        for page in PdfReader(str(path)).pages:
            for annot in page.get("/Annots") or []:
                try:
                    obj = annot.get_object()
                    uri = (obj.get("/A") or {}).get("/URI")
                except Exception:  # noqa: BLE001 -- a malformed annotation
                    continue
                if isinstance(uri, bytes):
                    uri = uri.decode("latin-1", "replace")
                if uri and re.match(r"(?i)https?://", str(uri)) and str(uri) not in out:
                    out.append(str(uri).strip())
        return out
    except Exception:  # noqa: BLE001 -- unreadable PDF: fall back to the text alone
        logger.debug("could not read link annotations of %s", path, exc_info=True)
        return []


def _covered_by_annotation(url: str, annotated: list[str]) -> bool:
    """Whether a URL read from PDF text is a mangled copy of an annotated link.

    True when it is a prefix of one (cut at a line break), or one plus a short
    tail of digits and punctuation (line numbers glued on in a review copy).
    """
    u = url.rstrip("/").lower()
    for a in annotated:
        t = a.rstrip("/").lower()
        if u == t or t.startswith(u):
            return True
        if u.startswith(t) and re.fullmatch(r"[/.\d]{1,8}", u[len(t):]):
            return True
        # cut at a line break, then the next line's number glued on
        head = re.sub(r"[/.]?\d{1,4}$", "", u)
        if head != u and len(head) > len("https://x.y") and t.startswith(head):
            return True
    return False


def scan_missing_code_link(paths: Iterable[Path]) -> list[Finding]:
    """Warn when the manuscript names no GitHub repository at all.

    A code-availability nudge (part of the sensitive-info scan): if any text was
    read from the submission files but none of it references a GitHub repo, the
    paper may have forgotten to link its code. Stays silent when no text could be
    read (e.g. an image-only PDF), so a link-less-looking scan isn't a false alarm.
    """
    scanned_any_text = False
    for path in _dedup_paths(paths):
        for _, text in _iter_text_blobs(path):
            scanned_any_text = True
            if any(_find_github_repos(text)) or _CODE_HOST_RE.search(text):
                return []
    if not scanned_any_text:
        return []
    return [
        Finding(
            "submission",
            "missing code link",
            "no code repository link found in the manuscript files; if the paper has " "associated code, add a link to its public repository",
        )
    ]


def _iter_text_blobs(path: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(where, text)`` for every text-decodable blob in a file.

    Mirrors :func:`scan_file`'s dispatch but only for text: a plain source/text
    file, a PDF's or Office file's text, and the same for the members of an
    archive. Images and binary blobs yield nothing. Used by the GitHub-link scan, which cares about
    URLs in prose rather than the format-specific secret/EXIF passes.

    Bibliographies and LaTeX class/style/bibstyle files (``LINK_SCAN_SKIP_EXTS``)
    are skipped, at the top level and inside archives alike: their URLs belong to
    the cited works or the template's author, not to this submission. The
    secret/EXIF passes in :func:`scan_file` still read those files.
    """
    try:
        suffix = path.suffix.lower()
        name = path.name
        double = "".join(path.suffixes[-2:]).lower()
        if suffix in ARCHIVE_EXTS or double in ARCHIVE_EXTS:
            try:
                members = _iter_archive_members(path)
            except Exception:
                logger.debug("could not open archive %s for link scan; skipping", path, exc_info=True)
                return
            for count, (mname, data) in enumerate(members):
                if count >= MAX_ARCHIVE_MEMBERS:
                    break
                msuffix = Path(mname).suffix.lower()
                if msuffix in IMAGE_EXTS or msuffix in LINK_SCAN_SKIP_EXTS or len(data) > MAX_TEXT_BYTES:
                    continue
                if msuffix == ".pdf" and data:
                    with _member_file(mname, data) as pdf:
                        text = _pdf_text(pdf)
                    if text:
                        yield f"{name}:{mname}", text
                    continue
                text = _office_blob_text(data) if msuffix in OFFICE_EXTS else _decode(data)
                if text is not None:
                    yield f"{name}:{mname}", text
        elif suffix == ".pdf":
            text = _pdf_text(path)
            if text:
                yield name, text
        elif suffix in OFFICE_EXTS:
            text = _office_blob_text(path.read_bytes())
            if text is not None:
                yield name, text
        elif suffix not in IMAGE_EXTS and suffix not in LINK_SCAN_SKIP_EXTS:
            data = path.read_bytes()
            if len(data) <= MAX_TEXT_BYTES:
                text = _decode(data)
                if text is not None:
                    yield name, text
    except Exception:
        logger.debug("could not read %s for link scan", path, exc_info=True)


def _office_blob_text(data: bytes) -> str | None:
    parts = _office_parts(data)
    if parts is None:
        return None
    # The document body last: nothing else can then read as following its reference list.
    return "\n".join(text for _, text in sorted(parts, key=lambda part: part[0] in ("word/document.xml", "content.xml")))
