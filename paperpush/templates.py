"""Find and download a venue's Word or LaTeX manuscript template.

``paperpush format`` prefers the venue's own template to reformatting a
manuscript itself, so it first looks for one:

1. ``manuscript.template_url`` in ``manuscript_requirements.json``, when it is
   recorded (a file, an archive, a GitHub repository, or a page that links one);
2. otherwise every author-guideline page recorded for the venue
   (``source_urls``), scanned for links that look like a template: a ``.docx``
   / ``.dotx`` / ``.zip`` / ``.cls`` / ``.sty`` file, an Overleaf template, or a
   GitHub style-file repository, named as a template, style file, or author kit.
   Pages those links lead to are scanned one level further.

Pages are read over plain HTTPS first and, when a publisher's bot check turns
that away, once more in a headless browser (Playwright, already a paperpush
dependency). Downloads are cached under ``~/.cache/paperpush/templates``; an
archive is unpacked into a directory of its own and its files are only read,
never run.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

logger = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; paperpush-format; +https://github.com/pachterlab/paperpush)"
_TIMEOUT = 25
MAX_PAGES = 8
MAX_DOWNLOAD_MB = 60

WORD_SUFFIXES = (".docx", ".dotx", ".dot", ".doc", ".docm", ".dotm")
LATEX_SUFFIXES = (".cls", ".sty", ".tex", ".bst")
ARCHIVE_SUFFIXES = (".zip",)

_TEMPLATE_WORDS = re.compile(r"(?i)\btemplates?\b|\bstyle[- ]?files?\b|\bclass[- ]files?\b|\bauthor[- ]?kit\b|\bpaper[- ]?pack\b|\bstyle\s+guide\s+files?\b|\blatex\s+(?:package|class|files?)\b|\bword\s+(?:file|document)\s+for\s+authors\b|\bsample\s+(?:manuscript|paper|file)\b")
_KIND_WORDS = re.compile(r"(?i)\b(?:la)?tex\b|\bword\b|\bms[- ]word\b|\.docx?\b|\boverleaf\b|\bcls\b|\bsty\b|\bbibtex\b")
_NOT_TEMPLATE = re.compile(r"(?i)\b(?:forms?|checklists?|declarations?|licen[cs]e|copyright|agreement|reporting\s+summary|cover\s+letter|permission|invoice|waiver|transfer|consent|response\s+to\s+reviewers|figure\s+guide|poster|slides?|presentation|reviewer|referee|supplement(?:ary|al)?|chemdraw|monographs?|contributed\s+volume|books?|chapters?|thesis|proceedings\s+volume|graphical\s+abstract|highlights)\b|cover\s*-?\s*letter")
_BOT_WALL = re.compile(r"(?i)just a moment\.\.\.|attention required|verify you are (?:a )?human|enable javascript and cookies|cf-browser-verification|access denied|captcha")


@dataclass
class TemplateLink:
    """A link that looks like a manuscript template."""

    url: str
    text: str
    kind: str  # "word", "latex", "archive" (contents unknown yet), "overleaf", "github", or "page"
    score: int
    found_on: str

    def describe(self) -> str:
        label = {"word": "Word template", "latex": "LaTeX template", "archive": "template archive", "overleaf": "Overleaf template", "github": "LaTeX template (GitHub)", "page": "template page"}[self.kind]
        text = f" ({self.text})" if self.text and self.text.lower() not in self.url.lower() else ""
        return f"{label}: {self.url}{text}"


@dataclass
class Template:
    """A downloaded template, ready to apply."""

    kind: str  # "word" or "latex"
    source: str  # the URL it came from (or the local path given)
    path: Path  # the .docx/.dotx for Word; the directory holding the files for LaTeX
    main_tex: Path | None = None  # LaTeX: the sample manuscript that shows how to use the class
    files: list[Path] = field(default_factory=list)  # LaTeX: the class/style/bibliography files
    class_name: str = ""  # LaTeX: a class already installed in TeX (no files to copy)


@dataclass
class Search:
    """What the template search looked at and found."""

    links: list[TemplateLink] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)  # pages read
    refused: list[str] = field(default_factory=list)  # pages that could not be read, with why


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "paperpush" / "templates"


# ---------------------------------------------------------------------------
# fetching


def _http_get(url: str) -> tuple[int, bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # https URLs from the venue data  # nosec B310
            data = resp.read(MAX_DOWNLOAD_MB * 1024 * 1024 + 1)
            return resp.status, data, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, b"", ""


def _browser_html(url: str) -> str | None:
    """The page as a headless browser renders it, or None when there is no browser to use."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            try:
                page = browser.new_context(locale="en-US").new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=_TIMEOUT * 1000)
                page.wait_for_timeout(2500)
                return page.content()
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 -- no browser installed, a timeout, ...
        logger.debug("headless fetch of %s failed: %s", url, exc)
        return None


def fetch_page(url: str, *, browser: bool = True) -> tuple[str | None, str]:
    """``(html, "")`` for a page, or ``(None, why)`` when it could not be read."""
    try:
        status, data, _ = _http_get(url)
    except (OSError, ValueError) as exc:
        status, data = 0, b""
        why = str(exc)
    else:
        why = f"HTTP {status}"
    html = data.decode("utf-8", "replace") if data else ""
    if status == 200 and html and not _BOT_WALL.search(html[:20000]):
        return html, ""
    if browser:
        rendered = _browser_html(url)
        if rendered and not _BOT_WALL.search(rendered[:20000]):
            return rendered, ""
        why = "a bot check refused the page" if rendered else why
    elif html and _BOT_WALL.search(html[:20000]):
        why = "a bot check refused the page"
    return None, why


# ---------------------------------------------------------------------------
# finding


def _suffix(url: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    for suffix in WORD_SUFFIXES + LATEX_SUFFIXES + ARCHIVE_SUFFIXES + (".tar.gz", ".tgz"):
        if path.endswith(suffix):
            return suffix
    return ""


def classify_link(url: str, text: str, found_on: str) -> TemplateLink | None:
    """A :class:`TemplateLink` when ``url`` (with anchor ``text``) looks like a manuscript template."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    both = f"{text} {urllib.parse.unquote(parsed.path)}"
    suffix = _suffix(url)
    host = parsed.netloc.lower()
    score = 0
    if _TEMPLATE_WORDS.search(both) or re.search(r"(?i)template|stylefile|style[-_]files|author[-_]?kit|paperpack", parsed.path):
        score += 3
    if _KIND_WORDS.search(both):
        score += 1
    if _NOT_TEMPLATE.search(text) or re.search(r"(?i)form|checklist|declaration|licen[cs]e|copyright|summary|supplement|chemdraw|cover-?letter|monograph|poster", parsed.path):
        score -= 5
    if suffix in WORD_SUFFIXES:
        kind = "word"
    elif suffix in (".cls", ".sty", ".tex", ".bst"):
        kind, score = "latex", score + 1
    elif suffix in ARCHIVE_SUFFIXES + (".tar.gz", ".tgz"):
        kind = "archive"
    elif "overleaf.com" in host and "/latex/templates/" in parsed.path:
        kind, score = "overleaf", score + 2
    elif host == "github.com" and len([p for p in parsed.path.split("/") if p]) >= 2:
        kind = "github"
        if not re.search(r"(?i)latex|style|template|class|kit|format", both):
            return None
    elif re.search(r"(?i)\bdownload\b", text) and (_TEMPLATE_WORDS.search(text) or re.search(r"(?i)\bpackage\b|\.zip\b|\bzip\b", text)):
        kind = "archive"  # "Download the journal article template package": a file behind a URL with no extension
        score += 2
    else:
        kind = "page"
        # A page is only worth following when it is clearly about the template.
        if not (_TEMPLATE_WORDS.search(text) and _KIND_WORDS.search(both)):
            return None
    if score < 3 and kind != "latex":
        return None
    return TemplateLink(url=url.split("#", 1)[0], text=text[:120], kind=kind, score=score, found_on=found_on)


def links_on(html: str, base: str) -> list[TemplateLink]:
    parser = _Links()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 -- malformed HTML: use what was parsed
        pass
    out: dict[str, TemplateLink] = {}
    for href, text in parser.links:
        url = urllib.parse.urljoin(base, href.strip())
        link = classify_link(url, text, base)
        if link and (link.url not in out or out[link.url].score < link.score):
            out[link.url] = link
    return list(out.values())


def find_templates(urls: list[str], template_url: str = "", *, browser: bool = True, fetch=fetch_page) -> Search:
    """Search ``template_url`` and the guideline pages ``urls`` for template links."""
    search = Search()
    found: dict[str, TemplateLink] = {}
    queue: list[tuple[str, int]] = []
    if template_url:
        direct = classify_link(template_url, "template", "manuscript.template_url")
        if direct and direct.kind != "page":
            direct.score += 10
            found[direct.url] = direct
        else:
            queue.append((template_url, 0))
    queue += [(u, 0) for u in urls if u and u != template_url]
    seen: set[str] = set()
    while queue and len(search.pages) + len(search.refused) < MAX_PAGES:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if _suffix(url) == ".pdf" or url.lower().endswith(".pdf"):
            continue
        html, why = fetch(url, browser=browser)
        if html is None:
            search.refused.append(f"{url} ({why})")
            continue
        search.pages.append(url)
        for link in links_on(html, url):
            if url == template_url:
                link.score += 5
            if link.kind == "page":
                if depth == 0:
                    queue.append((link.url, 1))
                continue
            if link.url not in found or found[link.url].score < link.score:
                found[link.url] = link
    search.links = sorted(found.values(), key=lambda link: -link.score)
    return search


def find_for_venue(reqs, *, browser: bool = True, fetch=fetch_page) -> Search:
    """Search the venue's recorded template URL and author-guideline pages."""
    return find_templates(list(reqs.source_urls or []), reqs.manuscript.template_url or "", browser=browser, fetch=fetch)


# ---------------------------------------------------------------------------
# downloading


def _download(url: str) -> tuple[Path, str]:
    """``(path, content type)`` of ``url`` in the cache, downloading it if needed."""
    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    folder = cache_dir() / key
    name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name or "download"
    target = folder / name
    meta = folder / ".content-type"
    if target.is_file():
        return target, meta.read_text() if meta.exists() else ""
    status, data, ctype = _http_get(url)
    if status != 200 or not data:
        raise OSError(f"HTTP {status} downloading {url}")
    if "html" in (ctype or "").lower() or data[:200].lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise OSError(f"{url} is a web page, not a template file")
    if len(data) > MAX_DOWNLOAD_MB * 1024 * 1024:
        raise OSError(f"{url} is larger than {MAX_DOWNLOAD_MB} MB")
    folder.mkdir(parents=True, exist_ok=True)
    if not Path(name).suffix:
        if data[:2] == b"PK":
            name += ".zip"
        target = folder / name
    target.write_bytes(data)
    meta.write_text(ctype)
    return target, ctype


def _safe_extract(archive: Path, dest: Path) -> None:
    """Unpack ``archive`` into ``dest``, refusing members that would land outside it."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            if member.is_dir() or member.filename.startswith("__MACOSX/") or Path(member.filename).name.startswith("._"):
                continue
            target = (dest / member.filename).resolve()
            if root not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def _latex_template(folder: Path, source: str, prefer: str = "") -> Template | None:
    tex = [p for p in folder.rglob("*.tex") if p.is_file()]
    support = [p for p in folder.rglob("*") if p.suffix.lower() in (".cls", ".sty", ".bst", ".clo", ".bbx", ".cbx") and p.is_file()]
    if not support and not tex:
        return None

    def rank(p: Path) -> tuple:
        text = p.read_text("utf-8", "replace")
        has_class = "\\documentclass" in text
        named = bool(re.search(r"(?i)template|sample|example|main|paper|demo|guide|submission", p.stem))
        # Among several samples (elsarticle-template-num / -harv), the one matching the venue's reference style.
        tokens = [tok for tok in re.split(r"[-_]", p.stem.lower()) if len(tok) >= 3 and tok not in ("template", "sample", "example", "elsarticle")]
        hits = sum(tok in prefer for tok in tokens) if prefer else 0
        extra = len(tokens) - hits if prefer else 0
        return (not has_class, -hits, extra, not named, len(p.relative_to(folder).parts), p.name)

    main = sorted(tex, key=rank)[0] if tex else None
    if main is not None and "\\documentclass" not in main.read_text("utf-8", "replace"):
        main = None
    return Template(kind="latex", source=source, path=(main.parent if main else support[0].parent), main_tex=main, files=support)


def _word_template(folder: Path, source: str) -> Template | None:
    docs = [p for p in folder.rglob("*") if p.suffix.lower() in (".docx", ".dotx", ".docm", ".dotm") and p.is_file()]
    if not docs:
        return None
    docs.sort(key=lambda p: (not re.search(r"(?i)template", p.name), p.suffix.lower() not in (".dotx", ".docx"), len(p.parts)))
    return Template(kind="word", source=source, path=docs[0])


def download(link: TemplateLink | str, prefer: str = "") -> Template:
    """Download (or open, for a local path) a template and say what it holds."""
    url = link.url if isinstance(link, TemplateLink) else str(link)
    local = Path(url).expanduser()
    if not re.match(r"https?://", url) and local.exists():
        path, source = local, str(local)
    else:
        if isinstance(link, TemplateLink) and link.kind == "overleaf":
            raise OSError(f"Overleaf templates cannot be downloaded without signing in; open {url}, choose 'Open as Template', download the project as a zip, and pass it with --template")
        if "github.com" in urllib.parse.urlparse(url).netloc and not _suffix(url):
            parts = [p for p in urllib.parse.urlparse(url).path.split("/") if p]
            url = f"https://codeload.github.com/{parts[0]}/{parts[1]}/zip/HEAD"
        path, _ = _download(url)
        source = link.url if isinstance(link, TemplateLink) else url
    suffix = path.suffix.lower()
    if path.is_dir():
        found = _word_template(path, source) or _latex_template(path, source, prefer)
    elif suffix in (".docx", ".dotx", ".docm", ".dotm"):
        found = Template(kind="word", source=source, path=path)
    elif suffix == ".doc" or suffix == ".dot":
        raise OSError(f"{path.name} is a legacy binary Word file; open it in Word, save it as .docx or .dotx, and pass that with --template")
    elif suffix in (".cls", ".sty"):
        found = Template(kind="latex", source=source, path=path.parent, files=[path])
    elif suffix == ".tex":
        found = _latex_template(path.parent, source, prefer)
    elif zipfile.is_zipfile(path):
        unpacked = path.parent / (path.stem + "_unpacked")
        if not unpacked.exists():
            _safe_extract(path, unpacked)
            _generate_classes(unpacked)
        found = _latex_template(unpacked, source, prefer) if list(unpacked.rglob("*.cls")) or list(unpacked.rglob("*.sty")) else None
        found = found or _word_template(unpacked, source) or _latex_template(unpacked, source, prefer)
    else:
        found = None
    if found is None:
        raise OSError(f"{source} holds no Word template (.docx/.dotx) or LaTeX class/style file")
    return found


def installed_class(name: str) -> Template | None:
    """The LaTeX class ``name`` when the local TeX installation already has it."""
    import subprocess  # nosec B404 -- fixed argv, no shell

    exe = shutil.which("kpsewhich")
    if not exe or not re.fullmatch(r"[A-Za-z0-9_-]+", name or ""):
        return None
    try:
        out = subprocess.run([exe, f"{name}.cls"], capture_output=True, text=True, timeout=20, check=False).stdout.strip()  # nosec B603
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    return Template(kind="latex", source=f"the {name} class in your TeX installation", path=Path(out).parent, class_name=name)


def ctan_link(name: str) -> TemplateLink | None:
    """A download link for the CTAN package ``name`` (e.g. elsarticle), or None if CTAN does not have it."""
    import json

    if not re.fullmatch(r"[A-Za-z0-9_-]+", name or ""):
        return None
    try:
        status, data, _ = _http_get(f"https://www.ctan.org/json/2.0/pkg/{name}")
        info = json.loads(data.decode("utf-8")) if status == 200 else {}
    except (OSError, ValueError):
        return None
    path = (info.get("ctan") or {}).get("path") if isinstance(info, dict) else None
    if not path:
        return None
    return TemplateLink(url=f"https://mirrors.ctan.org{path}.zip", text=f"{name} on CTAN", kind="archive", score=1, found_on="CTAN")


def _generate_classes(folder: Path) -> None:
    """Run each ``.ins`` installer in ``folder`` (TeX without shell escape) when it ships no ``.cls``."""
    import subprocess  # nosec B404 -- fixed argv, no shell

    exe = shutil.which("latex") or shutil.which("pdflatex")
    if not exe or list(folder.rglob("*.cls")):
        return
    for ins in folder.rglob("*.ins"):
        try:
            subprocess.run([exe, "-interaction=batchmode", "-no-shell-escape", ins.name], cwd=ins.parent, capture_output=True, timeout=120, check=False)  # nosec B603
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("could not run %s: %s", ins, exc)


def pick(search: Search, want: str) -> list[TemplateLink]:
    """The links worth trying for a manuscript of kind ``want`` ("word" or "latex"), best first."""
    ok = {"word": ("word", "archive"), "latex": ("latex", "archive", "github", "overleaf")}[want]
    return [link for link in search.links if link.kind in ok and not (want == "latex" and link.kind == "archive" and re.search(r"(?i)\bword\b", link.text))]


def describe_zip(data: bytes) -> list[str]:
    """Member names of an in-memory zip (for tests and reports)."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.namelist()
