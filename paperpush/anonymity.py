"""Check a double-blind submission for information that identifies its authors.

Anonymous-review venues (ICLR, AAAI, ...; ``"anonymous": true`` in
``venues.json``) desk-reject a paper whose files reveal who wrote it. This module
is the back end for that check in ``paperpush validate``, which runs it for those
venues automatically and for any other venue with ``--anonymous``.

Two sources are scanned:

* **The attached files** (every ``file``/``filelist`` upload, archive members
  included) and the ``.sub``'s own free-text values (title, abstract, TL;DR,
  ...), which reviewers also see.
* **Every anonymous.4open.science repository they link.** Anonymous GitHub only
  redacts the terms the author listed when creating the mirror, so a forgotten
  name in a LICENSE, a notebook's ``/Users/<name>/`` output path, or a
  ``github.com/<author>`` URL slips through. The mirror's files are fetched
  through the service's public API and scanned the same way; an expired or
  missing mirror is reported too, since reviewers will not be able to open it.

What counts as identifying:

* the authors' own details, taken from the ``.sub``'s author list -- full names,
  emails, ORCID iDs, OpenReview IDs, affiliations. A name that appears only in
  the manuscript's reference list is not reported: citing one's own work in the
  third person is allowed.
* author metadata: a PDF's ``/Author``, an Office file's creator/last-modified-by,
  an image's EXIF Artist/Copyright.
* absolute home-directory paths (``/Users/<name>/``, ``/home/<name>/``), which
  carry a username.
* an Acknowledgments section, and LaTeX set to camera-ready mode
  (``\\iclrfinalcopy``, ``\\usepackage[final]{neurips_...}``), which prints the
  author block.
* in a linked anonymous repository, a LICENSE copyright line naming a holder.

Every finding is advisory (the check is heuristic), reported through the same
:class:`~paperpush.sensitive.Finding` shape as the other scans.
"""

from __future__ import annotations

import io
import json
import logging
import re
import urllib.parse
import zipfile
from pathlib import Path
from typing import Iterable, Iterator

from PIL import Image

from .sensitive import (
    ARCHIVE_EXTS,
    IMAGE_EXTS,
    LATEX_EXTS,
    MAX_ARCHIVE_MEMBERS,
    MAX_TEXT_BYTES,
    Finding,
    _decode,
    _dedup_paths,
    _find_github_repos,
    _iter_archive_members,
    _latex_comment,
    _pdf_metadata_text,
    _pdf_text,
)

logger = logging.getLogger(__name__)

# --- identity terms ----------------------------------------------------------

# Author-list columns that identify a person, mapped to the label used in a
# finding. ``name`` is assembled separately (it may be split into first/last).
_IDENTITY_COLUMNS = {
    "email": "email",
    "orcid": "ORCID iD",
    "open_review_id": "OpenReview ID",
    "affiliation": "affiliation",
    "institution": "affiliation",
}
# Shorter than this, an affiliation ("MIT", "UW") is too ambiguous to search for.
MIN_AFFILIATION_CHARS = 5


def identity_terms(venue, values: dict[str, str]) -> list[tuple[str, str]]:
    """``(label, term)`` pairs identifying the submission's authors.

    Read from the venue's author-list field(s) -- every ``authorlist`` whose id
    mentions "author", so a conflicts-of-interest list (people who are *not* the
    authors) is left out. Names need at least two words to be searched for: a
    lone surname would match ordinary prose.
    """
    from .validate import parse_authors

    terms: dict[str, str] = {}
    for field in venue.fields:
        if field.type != "authorlist" or "author" not in field.id.lower():
            continue
        for author in parse_authors(values.get(field.id, ""), field.fields):
            name = (author.get("name") or " ".join(p for p in (author.get("first_name", ""), author.get("last_name", "")) if p)).strip()
            if len(name.split()) >= 2:
                terms.setdefault(name, "author name")
            for column, label in _IDENTITY_COLUMNS.items():
                value = str(author.get(column, "") or "").strip()
                if not value:
                    continue
                if label == "affiliation" and len(value) < MIN_AFFILIATION_CHARS:
                    continue
                terms.setdefault(value, label)
    return [(label, term) for term, label in terms.items()]


def _term_pattern(term: str) -> re.Pattern[str]:
    """Case-insensitive, whitespace-flexible, word-bounded matcher for ``term``."""
    body = r"\s+".join(re.escape(part) for part in term.split())
    return re.compile(rf"(?<![\w@.]){body}(?![\w@])", re.IGNORECASE)


# --- generic identifying patterns --------------------------------------------

# A home directory in an absolute path carries the username that owns it.
_HOME_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\+Users\\+)([A-Za-z0-9._\-]+)")
# Account names that identify nobody (CI runners, containers, placeholders).
_GENERIC_USERS = {
    "user",
    "users",
    "username",
    "runner",
    "ubuntu",
    "root",
    "admin",
    "shared",
    "public",
    "guest",
    "anonymous",
    "anon",
    "me",
    "you",
    "xxx",
    "xxxx",
    "name",
    "your_name",
    "yourname",
    "jovyan",
    "vscode",
    "codespace",
    "runneradmin",
    "default",
}
_ACK_HEADING = re.compile(
    r"^\s*(?:\d+\.?\s+|[A-Z]\.?\s+)?acknowledge?ments?\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_TEX_ACK = re.compile(r"\\(?:section|subsection|paragraph)\*?\s*\{\s*acknowledge?ments?\s*\}|\\begin\s*\{\s*ack\s*\}|\\acks\b", re.IGNORECASE)
# Camera-ready switches of the ML conference styles; each prints the author block.
_TEX_FINAL = re.compile(
    r"\\(?:iclrfinalcopy|colmfinalcopy)\b" r"|\\usepackage\s*\[[^\]]*\b(?:final|camera-?ready|preprint)\b[^\]]*\]\s*\{\s*(?:iclr|neurips|nips|icml|aaai|colm|corl|tmlr)[^}]*\}",
    re.IGNORECASE,
)
# Where the reference list starts / the appendix after it resumes, in plain text
# and in LaTeX source. A name inside that span is a (permitted) self-citation.
_REFERENCE_START = re.compile(
    r"^\s*(?:\d+\.?\s+|[ivxlcIVXLC]+\.?\s+)?(?:references|bibliography|literature\s+cited)\s*:?\s*$" r"|\\begin\s*\{\s*thebibliography\s*\}|\\bibliography\s*\{|\\printbibliography\b",
    re.IGNORECASE | re.MULTILINE,
)
_REFERENCE_END = re.compile(
    r"^\s*(?:[A-Z]\.?\s+)?(?:appendix|appendices|supplementary\s+material)\b[^\n]{0,60}$" r"|\\end\s*\{\s*thebibliography\s*\}|\\appendix\b",
    re.IGNORECASE | re.MULTILINE,
)
# "Copyright (c) 2026 Holder" -- the line Anonymous GitHub most often leaves
# un-redacted, in a LICENSE file.
_COPYRIGHT = re.compile(r"(?im)^\s*copyright\s+(?:\(c\)\s*|©\s*)?(?:\d{4}(?:\s*[-–,]\s*\d{4})*\s+)?(?P<holder>[^\n]{2,80})$")
_ANONYMOUS_HOLDER = re.compile(r"(?i)anonym|x{3,}|\bauthors?\b|<[^>]*>|\[[^\]]*\]|\{[^}]*\}")
# PDF /Info keys and Office core-properties that name a person.
_PDF_AUTHOR_KEYS = {"/Author"}
_OFFICE_EXTS = {".docx", ".pptx", ".xlsx", ".docm", ".pptm", ".xlsm"}
_OFFICE_PEOPLE = re.compile(r"<(dc:creator|cp:lastModifiedBy)>([^<]+)</\1>")
# EXIF tags naming a person: Artist, Copyright, XPAuthor.
_EXIF_PEOPLE = {0x013B: "Artist", 0x8298: "Copyright", 0x9C9D: "XPAuthor"}


def _is_anonymous_value(value: str) -> bool:
    return not value.strip() or bool(re.search(r"(?i)anonym|^x+$|^unknown$", value.strip()))


def _reference_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of ``text`` holding a reference list."""
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        start = _REFERENCE_START.search(text, pos)
        if start is None:
            return spans
        end = _REFERENCE_END.search(text, start.end())
        stop = end.start() if end else len(text)
        spans.append((start.start(), stop))
        if end is None:
            return spans
        pos = end.end()


def _strip_latex_comments(text: str) -> str:
    """LaTeX source with ``%`` comments removed (for the directive checks only)."""
    lines = []
    for line in text.splitlines():
        comment = _latex_comment(line)
        lines.append(line if comment is None else line[: len(line) - len(comment) - 1])
    return "\n".join(lines)


def scan_text(
    where: str,
    text: str,
    terms: list[tuple[str, str]],
    *,
    is_latex: bool = False,
    is_manuscript: bool = True,
) -> Iterator[Finding]:
    """Yield identifying-information findings for one blob of text.

    ``is_manuscript`` turns on the paper-shaped checks (acknowledgments, and
    skipping names found in the reference list); it is off for a repository
    file, where a name anywhere is a leak.
    """
    ref_spans = _reference_spans(text) if is_manuscript else []
    for label, term in terms:
        for match in _term_pattern(term).finditer(text):
            if label == "author name" and any(a <= match.start() < b for a, b in ref_spans):
                continue
            yield Finding(where, "identifying information", f"anonymity: {label} '{term}' appears")
            break
    # github.com/<owner> where the owner is one of the authors' handles.
    handles = _author_handles(terms)
    for owner, repo in dict.fromkeys(_find_github_repos(text)):
        if owner.lower() in handles:
            yield Finding(where, "identifying information", f"anonymity: GitHub link github.com/{owner}/{repo} names an author's account; link an anonymous.4open.science mirror instead")
    for user in dict.fromkeys(m.group(1) for m in _HOME_PATH.finditer(text)):
        if user.lower() not in _GENERIC_USERS:
            yield Finding(where, "identifying information", f"anonymity: absolute path contains the username '{user}' (e.g. /Users/{user}/...)")
    if not is_manuscript:
        return
    body = _strip_latex_comments(text) if is_latex else text
    if (_TEX_ACK if is_latex else _ACK_HEADING).search(body):
        yield Finding(where, "identifying information", "anonymity: has an Acknowledgments section; remove it for anonymous review (it usually names funders, colleagues, and grants)")
    if is_latex:
        final = _TEX_FINAL.search(body)
        if final:
            yield Finding(where, "identifying information", f"anonymity: LaTeX is in camera-ready mode ({final.group(0).strip()}), which prints the author names; use the submission/anonymous option")


def _author_handles(terms: list[tuple[str, str]]) -> set[str]:
    """Lowercase account-name guesses for the authors (email local parts, OpenReview IDs, names)."""
    handles: set[str] = set()
    for label, term in terms:
        if label == "email":
            handles.add(term.split("@", 1)[0].lower())
        elif label == "OpenReview ID":
            handles.add(re.sub(r"\d+$", "", term.lstrip("~")).replace("_", "").lower())
        elif label == "author name":
            parts = [p.lower() for p in re.findall(r"[A-Za-z]+", term)]
            if len(parts) >= 2:
                handles.update({"".join(parts), parts[0] + parts[-1], parts[0][0] + parts[-1], "-".join(parts)})
    return {h for h in handles if len(h) >= 3}


# --- attached files ------------------------------------------------------------


def _scan_image_people(where: str, data: bytes) -> Iterator[Finding]:
    try:
        with Image.open(io.BytesIO(data)) as img:
            exif = img.getexif()
    except Exception:
        logger.debug("could not read EXIF from %s; skipping", where, exc_info=True)
        return
    for tag, label in _EXIF_PEOPLE.items():
        value = exif.get(tag)
        if isinstance(value, bytes):
            value = value.decode("utf-16-le" if tag == 0x9C9D else "utf-8", errors="replace").strip("\x00")
        if value and not _is_anonymous_value(str(value)):
            yield Finding(where, "identifying information", f"anonymity: image metadata {label} is '{str(value).strip()}'")


def _scan_office_people(where: str, data: bytes) -> Iterator[Finding]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            core = zf.read("docProps/core.xml").decode("utf-8", errors="replace")
    except Exception:
        return
    for tag, value in _OFFICE_PEOPLE.findall(core):
        if not _is_anonymous_value(value):
            label = "author" if tag == "dc:creator" else "last modified by"
            yield Finding(where, "identifying information", f"anonymity: document properties list {label} '{value.strip()}'")


def _scan_blob(where: str, name: str, data: bytes, terms: list[tuple[str, str]]) -> Iterator[Finding]:
    """Scan one archive member (or a small top-level file) by its extension."""
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_EXTS:
        yield from _scan_image_people(where, data)
        return
    if suffix in _OFFICE_EXTS:
        yield from _scan_office_people(where, data)
        return
    if suffix == ".bib" or len(data) > MAX_TEXT_BYTES:
        # A .bib holds the works cited, where the authors' own papers belong.
        return
    text = _decode(data)
    if text is not None:
        yield from scan_text(where, text, terms, is_latex=suffix in LATEX_EXTS)


def scan_file(path: Path, terms: list[tuple[str, str]]) -> list[Finding]:
    """Every identifying-information finding in one attached file."""
    findings: list[Finding] = []
    name = path.name
    suffix = path.suffix.lower()
    double = "".join(path.suffixes[-2:]).lower()
    try:
        if suffix in _OFFICE_EXTS:
            data = path.read_bytes()
            findings.extend(_scan_office_people(name, data))
            from .manuscript import docx_to_text

            text = docx_to_text(path) if suffix == ".docx" else None
            if text:
                findings.extend(scan_text(name, text, terms))
        elif suffix in ARCHIVE_EXTS or double in ARCHIVE_EXTS:
            for count, (member, data) in enumerate(_iter_archive_members(path)):
                if count >= MAX_ARCHIVE_MEMBERS:
                    break
                where = f"{name}:{member}"
                findings.extend(scan_text(where, member, terms, is_manuscript=False))
                if data:
                    findings.extend(_scan_blob(where, member, data, terms))
        elif suffix == ".pdf":
            text = _pdf_text(path)
            if text:
                findings.extend(scan_text(name, text, terms))
            for key, value in _pdf_metadata_text(path).items():
                if key in _PDF_AUTHOR_KEYS and not _is_anonymous_value(value):
                    findings.append(Finding(name, "identifying information", f"anonymity: PDF metadata {key.lstrip('/')} is '{value.strip()}'; clear it (e.g. \\hypersetup{{pdfauthor={{}}}})"))
                else:
                    findings.extend(scan_text(f"{name} [metadata {key}]", value, terms, is_manuscript=False))
        else:
            findings.extend(_scan_blob(name, name, path.read_bytes(), terms))
    except Exception:
        logger.debug("could not scan %s for identifying information", path, exc_info=True)
    return findings


# --- anonymous.4open.science ---------------------------------------------------

ANONYMOUS_GITHUB = "https://anonymous.4open.science"
_ANON_REPO_URL = re.compile(r"https?://anonymous\.4open\.science/r/([A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*)", re.IGNORECASE)
# Caps so a huge mirror cannot turn validation into a crawl; hitting one is reported.
MAX_REPO_FILES = 300
MAX_REPO_FILE_BYTES = 1024 * 1024
REPO_FETCH_TIMEOUT = 15.0
REPO_FETCH_WORKERS = 8
# Files whose bytes are never text worth scanning; skipped without a download.
_BINARY_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tgz",
    ".tar",
    ".bz2",
    ".xz",
    ".7z",
    ".npy",
    ".npz",
    ".pt",
    ".pth",
    ".ckpt",
    ".bin",
    ".safetensors",
    ".h5",
    ".hdf5",
    ".pkl",
    ".pickle",
    ".parquet",
    ".feather",
    ".mat",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".whl",
    ".mp4",
    ".mov",
    ".mp3",
    ".wav",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
}
_REPO_ERRORS = {
    "repository_expired": "has expired, so reviewers cannot open it; refresh it on anonymous.4open.science (or extend its expiration) before submitting",
    "repo_not_found": "does not exist on anonymous.4open.science; check the link",
    "repository_not_ready": "is still being anonymized; check again once it is ready",
    "repository_removed": "has been removed; recreate the anonymous mirror",
}


def find_anonymous_repos(texts: Iterable[str]) -> list[str]:
    """Distinct anonymous.4open.science repository ids linked from ``texts``, in order."""
    repos: dict[str, None] = {}
    for text in texts:
        for match in _ANON_REPO_URL.finditer(text):
            repos.setdefault(match.group(1).rstrip("."), None)
    return list(repos)


def _http_get(url: str, timeout: float = REPO_FETCH_TIMEOUT) -> tuple[int, bytes]:
    """``(status, body)`` for a GET of ``url``; ``(0, b"")`` on a transport failure."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "paperpush-validate"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # fixed https host  # nosec B310
            return resp.status, resp.read(MAX_REPO_FILE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        return exc.code, body
    except Exception:
        logger.debug("could not fetch %s", url, exc_info=True)
        return 0, b""


def _api(repo: str, suffix: str) -> str:
    return f"{ANONYMOUS_GITHUB}/api/repo/{urllib.parse.quote(repo, safe='')}/{suffix}"


def _api_error(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return ""
    return str(data.get("error", "")) if isinstance(data, dict) else ""


def _list_repo_files(repo: str) -> tuple[list[tuple[str, int]], str | None, bool]:
    """Walk a mirror's tree: ``(files, error, truncated)``.

    ``files`` is ``(path, size)`` per file. Directory entries come back from the
    API without a ``size``; their children are listed with ``?path=<dir>``.
    ``error`` is the service's error code (or a short description) when the root
    listing fails.
    """
    files: list[tuple[str, int]] = []
    pending = [""]
    truncated = False
    first = True
    while pending:
        directory = pending.pop(0)
        status, body = _http_get(_api(repo, "files/?path=" + urllib.parse.quote(directory)))
        if status != 200:
            if first:
                return [], _api_error(body) or (f"HTTP {status}" if status else "unreachable"), False
            logger.debug("could not list %s in anonymous repo %s (HTTP %s)", directory, repo, status)
            continue
        first = False
        try:
            entries = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        for entry in entries if isinstance(entries, list) else []:
            name = str(entry.get("name", ""))
            if not name:
                continue
            path = f"{entry.get('path')}/{name}" if entry.get("path") else name
            if "size" in entry:
                if len(files) >= MAX_REPO_FILES:
                    truncated = True
                    continue
                files.append((path, int(entry.get("size") or 0)))
            else:
                pending.append(path)
    return files, None, truncated


def scan_anonymous_repo(repo: str, terms: list[tuple[str, str]]) -> list[Finding]:
    """Fetch an anonymous.4open.science mirror and scan its files for identity leaks."""
    where = f"anonymous.4open.science/r/{repo}"
    files, error, truncated = _list_repo_files(repo)
    if error is not None:
        reason = _REPO_ERRORS.get(error, f"could not be checked ({error})")
        # Located at the submission level: the message already names the link.
        return [Finding("submission", "anonymous repository", f"anonymity: linked anonymous repository {where} {reason}")]

    findings: list[Finding] = []
    for path, _ in files:
        findings.extend(scan_text(f"{where}/{path}", path, terms, is_manuscript=False))
    wanted = [(p, s) for p, s in files if Path(p).suffix.lower() not in _BINARY_EXTS and s <= MAX_REPO_FILE_BYTES]

    def fetch(item: tuple[str, int]) -> tuple[str, bytes | None]:
        path = item[0]
        status, body = _http_get(_api(repo, "file/" + urllib.parse.quote(path)))
        return path, body if status == 200 else None

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=REPO_FETCH_WORKERS, thread_name_prefix="paperpush-anonrepo") as pool:
        fetched = list(pool.map(fetch, wanted)) if wanted else []

    for path, body in fetched:
        if body is None:
            continue
        text = _decode(body)
        if text is None:
            continue
        file_where = f"{where}/{path}"
        findings.extend(scan_text(file_where, text, terms, is_manuscript=False))
        if re.match(r"(?i)(licen[cs]e|copying)(\.|$)", Path(path).name):
            for match in _COPYRIGHT.finditer(text):
                holder = match.group("holder").strip()
                if not _ANONYMOUS_HOLDER.search(holder):
                    findings.append(Finding(file_where, "identifying information", f"anonymity: copyright line names '{holder}'; add it to the terms Anonymous GitHub redacts"))
    if truncated:
        findings.append(Finding("submission", "anonymous repository", f"anonymity: only the first {MAX_REPO_FILES} files of {where} were checked; inspect the rest manually"))
    return findings


# --- entry point -----------------------------------------------------------------


def _text_blobs(paths: list[Path]) -> Iterator[str]:
    """Plain text of each attachment, for finding the repository links they cite."""
    from .sensitive import _iter_text_blobs

    for path in paths:
        for _, text in _iter_text_blobs(path):
            yield text
        if path.suffix.lower() == ".docx":
            from .manuscript import docx_to_text

            text = docx_to_text(path)
            if text:
                yield text


def scan_submission(
    paths: Iterable[Path],
    terms: list[tuple[str, str]],
    field_texts: dict[str, str],
    *,
    check_repos: bool = True,
) -> list[Finding]:
    """Scan a submission for anything that identifies its authors.

    ``paths`` are the attached files; ``terms`` the authors' identity terms (see
    :func:`identity_terms`); ``field_texts`` the ``.sub``'s free-text values by
    field label, which reviewers also see. With ``check_repos`` (the default)
    every anonymous.4open.science mirror linked from either is fetched and
    scanned too, which needs network access.
    """
    paths = _dedup_paths(paths)
    findings: list[Finding] = []
    if not terms:
        findings.append(
            Finding(
                "submission",
                "anonymity",
                "anonymity: no author details in the .sub's author list, so only generic checks ran (metadata, home paths, acknowledgments); fill in the authors to also search for their names and emails",
            )
        )
    for label, text in field_texts.items():
        findings.extend(scan_text(f".sub field '{label}'", text, terms, is_manuscript=False))
    for path in paths:
        findings.extend(scan_file(path, terms))
    if check_repos:
        repos = find_anonymous_repos([*field_texts.values(), *_text_blobs(paths)])
        logger.info("Checking %d linked anonymous.4open.science repositor%s", len(repos), "y" if len(repos) == 1 else "ies")
        for repo in repos:
            findings.extend(scan_anonymous_repo(repo, terms))
    # One finding per (location, message): a name on every page is one problem.
    return list(dict.fromkeys(findings))
