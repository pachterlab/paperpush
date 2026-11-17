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
* author metadata: a PDF's ``/Author`` or XMP creator, an Office file's
  creator/last-modified-by/company/manager and its comment and tracked-change
  authors, an image's EXIF Artist/Copyright.
* absolute home-directory paths (``/Users/<name>/``, ``/home/<name>/``), which
  carry a username.
* an Acknowledgments section, and LaTeX set to camera-ready mode
  (``\\iclrfinalcopy``, ``\\usepackage[final]{neurips_...}``), which prints the
  author block.
* any non-anonymized ``github.com/<owner>/<repo>`` link outside the reference
  list, since its owner (often a lab or organization account) identifies the
  authors; a link whose owner or repository name contains an author's handle
  or surname (``pachterlab`` for Pachter) is worded more firmly.
* in a linked anonymous repository, a LICENSE copyright line naming a holder.

Findings never block on a guess: LaTeX in camera-ready mode and an author named in a
file's metadata are errors; an author's name, email, ORCID or OpenReview ID in the text,
a GitHub link carrying an author's handle, image/comment metadata naming someone, and a
copyright holder in a linked repository are likely errors (serious if real, but the
check is heuristic); the rest are warnings. All are reported through the same
:class:`~paperpush.sensitive.Finding` shape as the other scans. A file that could
not be read is reported too, so that a scan that did not happen is not a pass.
Files only the editors see (a title page, a cover letter) are left out: they
are where the authors' names belong.
"""

from __future__ import annotations

import html
import io
import json
import logging
import re
import urllib.parse
import zipfile
from pathlib import Path
from typing import Iterable, Iterator

from PIL import Image

from . import document
from .sensitive import (
    LEVEL_ERROR,
    LEVEL_LIKELY,
    LEVEL_WARNING,
    ARCHIVE_EXTS,
    IMAGE_EXTS,
    LATEX_EXTS,
    MAX_ARCHIVE_MEMBERS,
    MAX_BLOB_BYTES,
    MAX_TEXT_BYTES,
    NOT_FETCHED,
    OFFICE_EXTS,
    OPAQUE_EXTS,
    UNREAD,
    Finding,
    _decode,
    _dedup_paths,
    _GITHUB_RE,
    _GITHUB_RESERVED_OWNERS,
    _find_github_repos,
    _iter_archive_members,
    _latex_comment,
    _member_file,
    _not_scanned,
    _office_parts,
    _pdf_metadata_text,
    _read_capped,
    _read_pdf,
    _unread_summary,
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
# Hits on these identity terms are likely errors; the rest (affiliations) are warnings.
_LIKELY_LABELS = {"author name", "email", "ORCID iD", "OpenReview ID"}
# Shorter than this, an affiliation ("MIT", "UW") is too ambiguous to search for.
MIN_AFFILIATION_CHARS = 5
# Upload fields only the editors see, where the authors' names belong.
EDITOR_ONLY_FIELDS = re.compile(r"title_page|cover_letter")
_ANON_PREFIX = "anonymity: "
# A comma-separated part of an affiliation that names an institution, and one too generic to search for.
_INSTITUTION_WORD = re.compile(r"(?i)universit|institut|college|hospital|school|laborator|\blabs?\b|cent(?:er|re)\b|academy|clinic|\b(?:inc|corp|ltd|llc|gmbh)\b|research")
_GENERIC_UNIT = re.compile(r"(?i)^\s*(?:department|dept\.?|school|faculty|division|college|institute|center|centre)\s+(?:of|for)\b")


def _affiliation_terms(value: str) -> list[str]:
    """``value``, and the parts of it that name an institution on their own."""
    terms = [value]
    parts = [p.strip() for p in re.split(r"[,;]", value) if len(p.strip()) >= MIN_AFFILIATION_CHARS]
    if len(parts) > 1 or (parts and parts[0] != value):
        specific = [p for p in parts if not _GENERIC_UNIT.search(p)]
        terms += [p for p in specific if _INSTITUTION_WORD.search(p)] or specific[:1]
    return terms


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
                if label == "affiliation":
                    if len(value) >= MIN_AFFILIATION_CHARS:
                        for term in _affiliation_terms(value):
                            terms.setdefault(term, label)
                    continue
                if label == "ORCID iD":
                    value = re.sub(r"(?i)^(?:https?://)?(?:www\.)?orcid\.org/", "", value)
                terms.setdefault(value, label)
    return [(label, term) for term, label in terms.items()]


_NAME_SUFFIX = re.compile(r"^(?:jr|sr|ii|iii|iv)\.?$")
_NAME_PARTICLE = {"van", "von", "de", "der", "den", "del", "della", "di", "da", "dos", "du", "la", "le", "ter", "ten", "bin", "ibn", "al", "el"}


def _flex(words: list[str]) -> str:
    return r"\s+".join(re.escape(word) for word in words)


def _name_variants(folded: str) -> list[str]:
    """Regex alternatives for a folded full name: as given, with middle initials, by initials, inverted."""
    words = folded.replace(",", " ").split()
    full = _flex(words)
    while len(words) > 2 and _NAME_SUFFIX.match(words[-1]):
        words = words[:-1]
    start = next((i for i, word in enumerate(words) if i and word in _NAME_PARTICLE), len(words) - 1)
    given, surname = re.escape(words[0]), _flex(words[start:])
    variants = [full, rf"{given}(?:\s+[a-z]\.?){{0,3}}\s+{surname}"]
    if len("".join(words[start:])) >= MIN_SURNAME_CHARS:
        initials = re.escape(words[0][0]) + r"\.(?:[\s-]*[a-z]\.)*"
        variants += [rf"{initials}\s*{surname}", rf"{surname},\s*{given}", rf"{surname},\s*{initials}"]
    return variants


def _term_pattern(term: str, label: str = "") -> re.Pattern[str]:
    """Whitespace-flexible, word-bounded matcher for ``term``, to run on :func:`document.fold`-ed text.

    Names and affiliations may touch a digit: extracted text runs an
    affiliation mark into them ("John Smith1,2", "1Stanford University").
    """
    folded = document.fold(term)
    body = "|".join(_name_variants(folded)) if label == "author name" else _flex(folded.split())
    if label in ("author name", "affiliation"):
        return re.compile(rf"(?<![^\W\d_])(?:{body})(?![^\W\d_])")
    return re.compile(rf"(?<![\w@.])(?:{body})(?![\w@])")


# LaTeX spellings of what a name or address looks like once typeset.
_TEX_GROUPED_ACCENT = re.compile(r"\{(\\[`'\"^~=.]\s*\{?\\?[A-Za-z]\}?)\}")
_TEX_SYMBOL_ACCENT = re.compile(r"\\[`'\"^~=.]\s*\{?\\?([A-Za-z])\}?")
_TEX_LETTER_ACCENT = re.compile(r"\\[uvHckrdbt]\s*\{\\?([A-Za-z])\}")
_TEX_SPECIAL_LETTER = re.compile(r"\\(ss|aa|AA|ae|AE|oe|OE|[oOlLij])(?![A-Za-z])(?:\{\}|\s+(?=[a-z]))?")
_TEX_LONE_LETTER = re.compile(r"\{([A-Za-z])\}")


def _detex(text: str) -> str:
    """LaTeX source with accent macros decoded and ties and escapes made plain, for searching."""
    text = _TEX_SYMBOL_ACCENT.sub(r"\1", _TEX_GROUPED_ACCENT.sub(r"\1", text))
    text = _TEX_LETTER_ACCENT.sub(r"\1", text)
    text = _TEX_SPECIAL_LETTER.sub(lambda m: {"aa": "a", "AA": "A"}.get(m.group(1), m.group(1)), text)
    text = _TEX_LONE_LETTER.sub(r"\1", text)
    return re.sub(r"\\([_&%#.])", r"\1", text.replace("~", " ").replace("\\ ", " "))


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
# On a line of its own, or run into the paragraph ("Acknowledgments: We thank ...", or a
# run-in bold heading with no punctuation: "Acknowledgments This work used ...").
_ACK_HEADING = re.compile(
    r"^\s*(?:\d+\.?\s+|[A-Z]\.?\s+)?acknowledge?ments?(?:\s+(?:and|&)\s+[a-z ]{3,40})?\s*(?:[:.\u2014\u2013]|$|(?-i:\s+(?:This|We|The|Our|Funding|Research|Part|[A-Z][a-z]+\s+(?:was|is|were|are|thanks?|acknowledges?|gratefully))\b))",
    re.IGNORECASE | re.MULTILINE,
)
# A funding statement with no Acknowledgments heading ("This work was supported in part by
# NSF grant 123"): it names the authors' funders, so it identifies them as surely as one.
_FUNDING_STATEMENT = re.compile(
    r"(?i)(?:(?:\b(?:was|is|were|are|been|being|and)|,)\s+(?:\w+ly\s+)?(?:supported|funded|sponsored|financed)\s+(?:in\s+part\s+|partly\s+|partially\s+)?by|\b(?:we|authors?)\s+(?:gratefully\s+)?acknowledge\s+(?:the\s+)?(?:support|funding|financial))"
    r"(?:[^.]|(?<=\b[A-Z])\.){0,200}?\b(?:grants?|awards?|foundation|fellowships?|scholarships?|NSF|NIH|DARPA|ERC|NSFC|DFG|EPSRC|JSPS|NSERC|(?:research|science|funding)\s+(?:program(?:me)?|council|agency)|contract\s+(?:no|number)|project\s+(?:no|number|id))\b"
)
# The camera-ready (non-anonymous) running head of the ICLR/TMLR styles on a paper under review.
_CAMERA_READY_HEAD = re.compile(r"(?i)^\s*published\s+as\s+a\s+conference\s+paper\s+at\b")
# An e-mail address, as an author block prints it ("{a,b}@uni.edu" included).
_EMAIL = re.compile(r"(?:\{[^}\n]{1,120}\}|[\w.+-]{1,64})\s?@\s?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# A title-block line naming an institution (short, not a sentence).
_AFFILIATION_LINE = re.compile(
    r"\b(?:Universit(?:y|\u00e9|\u00e4|\u00e0|at|e|ies)|Institut(?:e|o)?|College|Laborator(?:y|ies)|School\s+of|Department\s+of|Dept\.|Faculty\s+of|Academy\s+of|Polytechnic|Politecnico|Inc\.|Ltd\.?|GmbH|Corporation|Google|DeepMind|Microsoft|NVIDIA|Amazon|Tencent|Alibaba|ByteDance|Huawei|Baidu|Meta\s+(?:AI|FAIR)|FAIR|Shanghai\s+AI\s+Lab(?:oratory)?|AI\s+Lab|Research\s+(?:Lab|Center|Centre))\b"
)
# A title-block line listing people ("Zulun Zhu, Haoyu Liu, Mengke He, Siqiang Luo"): two or
# more given-family names separated by commas or "and" (a title in capitals does not fit).
_PERSON = r"[A-Z][a-z\u00c0-\u017f]+(?:-[A-Za-z\u00c0-\u017f]+)?(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-z\u00c0-\u017f'-]+){1,2}"
_NAME_LIST_LINE = re.compile(rf"^{_PERSON}(?:\s*(?:,|,?\s+and|&)\s*{_PERSON})+\s*$")
# The end of a paper's title block.
_ABSTRACT_HEADING = re.compile(r"(?im)^\s*(?:a\s?bstract|abstract)\b")
# Other code forges whose URLs name their owner, like github.com/<owner>/<repo>.
_FORGE_RE = re.compile(r"(?i)\b(gitlab\.com|gitee\.com|bitbucket\.org|codeberg\.org)/([A-Za-z0-9][\w.-]{0,38})(?:/([\w.-]+))?")
_FORGE_RESERVED = {"explore", "users", "groups", "help", "about", "docs", "dashboard", "blog", "login", "signup", "search"}
# A GitHub Pages site, <owner>.github.io: a personal or lab page.
_GITHUB_PAGES_RE = re.compile(r"(?i)\b([a-z0-9](?:[a-z0-9-]{0,38}))\.github\.io\b")
# A URL broken across lines after the host ("https://github.com/" / "owner/repo").
_WRAPPED_HOST = re.compile(r"(?i)((?:github\.com|gitlab\.com|gitee\.com|bitbucket\.org|codeberg\.org)/(?:[\w.-]+/)?)[ \t]*\n[ \t]*(?=[\w.-])")
_TEX_ACK = re.compile(
    r"\\(?:(?:sub)*section|(?:sub)?paragraph)\*?\s*(?:\[[^\]]*\])?\s*\{\s*acknowledge?ments?(?:\s+(?:and|\\&)\s[^}]*)?\s*\.?\s*\}" r"|\\begin\s*\{\s*(?:acks?|acknowledge?ments?)\s*\}|\\acks\b|\\acknowledge?ments?\b",
    re.IGNORECASE,
)
# Camera-ready switches of the ML conference styles; each prints the author block.
_TEX_FINAL = re.compile(
    r"\\(?:iclr|colm|cvpr|iccv|wacv|acl|naacl|eacl|emnlp)finalcopy\b" r"|\\usepackage\s*\[[^\]]*\b(?:final|camera-?ready|preprint|accepted)\b[^\]]*\]\s*\{\s*(?:iclr|neurips|nips|icml|aaai|colm|corl|tmlr|aistats|cvpr|acl)[^}]*\}",
    re.IGNORECASE,
)
# Where the reference list starts / the appendix after it resumes, in plain text
# and in LaTeX source. A name inside that span is a (permitted) self-citation.
_REFERENCE_START = re.compile(
    r"^\s*(?:\d+\.?\s+|[ivxlcIVXLC]+\.?\s+)?(?:references|bibliography|literature\s+cited)\s*:?\s*$" r"|\\begin\s*\{\s*thebibliography\s*\}|\\bibliography\s*\{|\\printbibliography\b" r"|\\(?:sub)*section\*?\s*\{\s*(?:references|bibliography)\s*\}",
    re.IGNORECASE | re.MULTILINE,
)
_REFERENCE_END = re.compile(
    r"^\s*(?:[A-Z]\.?\s+)?(?:appendix|appendices|supplementary\s+material)\b[^\n]{0,60}$" r"|^\s*(?:\d+\.?\s+)?(?:author\s+contributions?|contributions|acknowledge?ments?|funding|competing\s+interests?|conflicts?\s+of\s+interest|data\s+availability|code\s+availability|declarations?|ethics(?:\s+statement)?|supplementary\s+information)\s*:?\s*$" r"|\\end\s*\{\s*thebibliography\s*\}|\\appendix\b|\\(?:sub)*section\*?\s*(?:\[[^\]]*\])?\s*\{",
    re.IGNORECASE | re.MULTILINE,
)
# A lettered appendix heading ("A Implementation details"). The text may be case-folded, so
# a wrapped reference title fits this too; a reference has a year close by (_YEAR_NEARBY).
_LETTERED_APPENDIX = re.compile(r"^[ \t]*a\.?[ \t]+[a-z]+(?:[ \t]+[a-z][a-z-]+){0,7}[ \t]*$", re.IGNORECASE | re.MULTILINE)
_YEAR_NEARBY = re.compile(r"\b(?:19|20)\d{2}\b")
# "Copyright (c) 2026 Holder" -- the line Anonymous GitHub most often leaves
# un-redacted, in a LICENSE file.
_COPYRIGHT = re.compile(r"(?im)^\s*copyright\s+(?:\(c\)\s*|©\s*)?(?:\d{4}(?:\s*[-–,]\s*\d{4})*\s+)?(?P<holder>[^\n]{2,80})$")
_ANONYMOUS_HOLDER = re.compile(r"(?i)anonym|x{3,}|\bauthors?\b|<[^>]*>|\[[^\]]*\]|\{[^}]*\}")
# PDF /Info keys and Office core-properties that name a person.
_PDF_AUTHOR_KEYS = {"/Author", "/XMP dc:creator"}
_OFFICE_PEOPLE = re.compile(r"<(dc:creator|cp:lastModifiedBy|meta:initial-creator)(?:\s[^>]*)?>([^<]+)</\1>")
_OFFICE_APP_PEOPLE = re.compile(r"<(Company|Manager)>([^<]+)</\1>")
# Who wrote a comment or tracked change, in Word, PowerPoint, and Excel.
_OFFICE_REVIEWER = re.compile(r'\bw:author="([^"]+)"|<p:cmAuthor\b[^>]*\bname="([^"]+)"|<author>([^<]+)</author>')
# The body of a document, where the manuscript-shaped checks apply.
_OFFICE_BODY_PARTS = {"word/document.xml", "content.xml"}
# What follows a one-word company name when the product, not the employer, is meant.
_PRODUCT_NAME = re.compile(r"\s+(?:scholar|translate|colab|drive|docs|sheets|forms|cloud|maps|earth|search|trends|gemini|bard|mechanical\s+turk|web\s+services|sagemaker|bedrock|coco|excel|word|powerpoint|office|azure|teams|bing|copilot|corenlp|cars|dogs)\b")
# EXIF tags naming a person: Artist, Copyright, XPAuthor.
_EXIF_PEOPLE = {0x013B: "Artist", 0x8298: "Copyright", 0x9C9D: "XPAuthor"}


def _is_anonymous_value(value: str) -> bool:
    return not value.strip() or bool(re.search(r"(?i)anonym|^x+$|^(?:unknown|authors?|user|word user|microsoft office user)$", value.strip()))


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
        appendix = next((m for m in _LETTERED_APPENDIX.finditer(text, start.end(), stop) if not _YEAR_NEARBY.search(text, m.end(), m.end() + 160)), None)
        if appendix is not None:
            end, stop = appendix, appendix.start()
        spans.append((start.start(), stop))
        if end is None:
            return spans
        pos = end.end()


def _blank_latex_comments(text: str) -> str:
    """LaTeX source with each ``%`` comment overwritten by spaces, so positions keep."""
    lines = []
    for line in text.split("\n"):
        comment = _latex_comment(line)
        lines.append(line if comment is None else line[: len(line) - len(comment)] + " " * len(comment))
    return "\n".join(lines)


def _home_path_findings(where: str, text: str) -> Iterator[Finding]:
    users: dict[str, None] = {}
    for match in _HOME_PATH.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        if not re.search(r"(?i)https?://\S*$", text[line_start : match.start()]):  # a web page's path
            users.setdefault(match.group(1), None)
    for user in users:
        if user.lower() not in _GENERIC_USERS:
            yield Finding(where, "identifying information", f"anonymity: absolute path contains the username '{user}' (e.g. /Users/{user}/...)")


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
    # A commented-out \\bibliography must not read as the start of a reference list.
    uncommented = _blank_latex_comments(text) if is_latex else text
    ref_spans = _reference_spans(uncommented) if is_manuscript else []
    searchable = document.fold(_detex(uncommented) if is_latex else text)
    searchable_refs = _reference_spans(searchable) if is_manuscript else []
    comments = document.fold(_detex("\n".join(c for c in map(_latex_comment, text.splitlines()) if c))) if is_latex else ""
    for label, term in terms:
        pattern = _term_pattern(term, label)
        product_ok = label == "affiliation" and len(term.split()) == 1
        hits = (m for m in pattern.finditer(searchable) if not (product_ok and _PRODUCT_NAME.match(searchable, m.end())))
        found = any(not (label == "author name" and any(a <= m.start() < b for a, b in searchable_refs)) for m in hits)
        if found or pattern.search(comments):
            level = LEVEL_LIKELY if label in _LIKELY_LABELS else LEVEL_WARNING
            yield Finding(where, "identifying information", f"anonymity: {label} '{term}' appears", level)
    # Any github.com/<owner>/<repo> link shows its owner, and the owner is
    # often a lab or organization account, so every one is reported -- except
    # in the reference list, where (as with names) citing a repository is a
    # permitted third-person citation. A link whose owner or repository name
    # contains an author's handle or surname gets the firmer wording; the rest
    # may be third-party code being cited.
    handles = _author_handles(terms)
    surnames = _author_surnames(terms)
    seen: set[tuple[str, str]] = set()
    # Rejoin URLs broken after the host ("https://github.com/" / "owner/repo"). Joining only
    # removes characters after each match's start, so positions shift by a few characters at most.
    link_text = _WRAPPED_HOST.sub(r"\1", text)
    for match in _GITHUB_RE.finditer(link_text):
        if any(a <= match.start() < b for a, b in ref_spans):
            continue
        link = next(_find_github_repos(match.group(0)), None)
        if link is None or link in seen:
            continue
        seen.add(link)
        owner, repo = link
        hit = _identifying_part(owner, repo, handles, surnames)
        if hit:
            yield Finding(where, "identifying information", f"anonymity: GitHub link github.com/{owner}/{repo} contains an author's name or handle ('{hit}'); link an anonymous.4open.science mirror instead", LEVEL_LIKELY)
        elif _presented_as_own(link_text, match.start(), match.end()):
            yield Finding(
                where,
                "identifying information",
                f"anonymity: non-anonymized GitHub link github.com/{owner}/{repo} shows its owner ('{owner}'), and the text presents it as your code; link an anonymous.4open.science mirror instead",
            )
        else:
            yield Finding(
                where,
                "identifying information",
                f"anonymity: GitHub link github.com/{owner}/{repo} shows its owner ('{owner}'); nothing around it says it is your code, so it is probably third-party software you cite, but if it is yours or your lab's, link an anonymous.4open.science mirror instead",
                _LEVEL_SUGGESTION,
            )
    # github.com/<owner> with no repository: a person's or lab's profile page.
    for match in _GITHUB_PROFILE_RE.finditer(link_text):
        if any(a <= match.start() < b for a, b in ref_spans):
            continue
        owner = match.group(1)
        if owner.lower() in _GITHUB_RESERVED_OWNERS or any(o == owner for o, _ in seen):
            continue
        seen.add((owner, ""))
        yield Finding(where, "identifying information", f"anonymity: link to the GitHub account github.com/{owner} identifies its owner; remove it for anonymous review or link an anonymous.4open.science mirror")
    # GitLab, Gitee, Bitbucket, Codeberg: the same owner-in-the-URL problem as GitHub.
    for match in _FORGE_RE.finditer(link_text):
        if any(a <= match.start() < b for a, b in ref_spans):
            continue
        host, owner, repo = match.group(1).lower(), match.group(2), (match.group(3) or "").rstrip(".")
        if owner.lower() in _FORGE_RESERVED or (host, owner.lower()) in seen:
            continue
        seen.add((host, owner.lower()))
        url = f"{host}/{owner}" + (f"/{repo}" if repo else "")
        yield Finding(where, "identifying information", f"anonymity: non-anonymized link {url} shows its owner ('{owner}'); if it is your or your lab's code, link an anonymous mirror instead (ignore this if it is third-party software you cite)")
    # <owner>.github.io: a personal or lab web page (a project page usually lives there).
    for match in _GITHUB_PAGES_RE.finditer(link_text):
        if any(a <= match.start() < b for a, b in ref_spans):
            continue
        owner = match.group(1)
        if ("pages", owner.lower()) in seen:
            continue
        seen.add(("pages", owner.lower()))
        yield Finding(where, "identifying information", f"anonymity: link to the GitHub Pages site {owner}.github.io names its owner ('{owner}'); if it is your or your lab's page, remove it or use an anonymous host (ignore this if it is a third-party page)")
    yield from _home_path_findings(where, text)
    if not is_manuscript:
        return
    body = _strip_latex_comments(text) if is_latex else text
    plain = _detex(body) if is_latex else body
    for match in _SELF_REFERENCE_RE.finditer(plain):
        if any(a <= match.start() < b for a, b in _reference_spans(plain)):
            continue
        snippet = " ".join(plain[match.start():match.end() + 60].split())
        yield Finding(where, "identifying information", f"anonymity: first-person reference to the authors' own earlier work ('{snippet}...'); refer to it in the third person for anonymous review")
        break
    has_ack = bool((_TEX_ACK if is_latex else _ACK_HEADING).search(body))
    if has_ack:
        yield Finding(where, "identifying information", "anonymity: has an Acknowledgments section; remove it for anonymous review (it usually names funders, colleagues, and grants)")
    else:
        spans = _reference_spans(plain)
        funding = next((m for m in _FUNDING_STATEMENT.finditer(plain) if not any(a <= m.start() < b for a, b in spans)), None)
        if funding is not None:
            snippet = " ".join(plain[funding.start():funding.end()].split())[:160]
            yield Finding(where, "identifying information", f"anonymity: funding statement ('{snippet}'); funders and grant numbers identify the authors, so remove it for anonymous review", LEVEL_LIKELY)
    if is_latex:
        final = _TEX_FINAL.search(body)
        if final:
            yield Finding(where, "identifying information", f"anonymity: LaTeX is in camera-ready mode ({final.group(0).strip()}), which prints the author names; use the submission/anonymous option", LEVEL_ERROR)


# A sentence that presents a linked repository as the authors' own ("Our code is available at",
# "we release the benchmark at", "Code: github.com/..."). Without one, a github.com/<owner>/<repo>
# link that names no author is most often third-party software being cited (github.com/
# facebookresearch/moco, EleutherAI/lm-evaluation-harness): on ICLR 2026 papers that passed
# review, such links were the main source of anonymity warnings, so they are suggestions.
_OWN_CODE_RE = re.compile(
    r"(?i)\bour\b[^.]{0,60}?\b(?:codes?|implementations?|repositor(?:y|ies)|repo|data\s*sets?|benchmarks?|models?|weights|checkpoints?|project\s+page|demos?|website|tool(?:kit)?|library|package|software|framework|source)\b"
    r"|\bwe\b[^.]{0,40}?\b(?:release[sd]?|open[- ]?sourced?|publish(?:ed)?|make\s+(?:\w+\s+){0,3}(?:publicly\s+)?available|provide\s+(?:the\s+|our\s+)?(?:code|implementation))\b"
    r"|\b(?:codes?|implementation|repository|project\s+page|source\s+code)\s+(?:is|are|will\s+be|has\s+been|can\s+be)\s+(?:publicly\s+|made\s+|openly\s+)*(?:(?:available|released|accessible|found)\s+)?(?:at|on|in|here|via)\b"
)
# A label right before the link, opening its line or sentence ("Code: https://github.com/...").
_CODE_LABEL_RE = re.compile(r"(?i)(?:^|[\n.])\s*(?:codes?|project\s+page|repository|github|source\s+code)(?:\s+and\s+[\w ]{1,20})?\s*:\s*(?:https?://)?(?:www\.)?$")
_LEVEL_SUGGESTION = "suggestion"


def _presented_as_own(text: str, start: int, end: int) -> bool:
    """Whether the sentence around a link (back to the previous full stop, at most 300 characters, and on to the next) presents it as the authors' own."""
    before = text[max(0, start - 300) : start]
    if _CODE_LABEL_RE.search(before):
        return True
    stop = max(before.rfind(". "), before.rfind(".\n"), before.rfind("\n\n"))
    after = text[end : end + 150]
    nxt = re.search(r"\.(?:\s|$)", after)
    return bool(_OWN_CODE_RE.search(before[stop + 1 :] + text[start:end] + (after[: nxt.start()] if nxt else after)))


# A GitHub account link (no repository after the owner).
_GITHUB_PROFILE_RE = re.compile(r"(?i)github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/?(?=[\s}{)\]\"'<>,;]|\.(?:\s|$)|$)")
# "our previous work", "our earlier system developed at ...", "in our prior paper": self-identifying in a blind paper.
# Up to three words may name it ("extends our previous MOS prediction model", "than our
# original Perch model"), and "our <noun> (Surname et al., 2024)" cites it outright.
_SELF_REFERENCE_RE = re.compile(
    r"(?i)\b(?:extends?|extending|extension\s+of|builds?\s+(?:up)?on|building\s+(?:up)?on|in|as\s+in|from|following|unlike|than|of|improves?\s+(?:on|upon)?|improving)\s+our\s+(?:own\s+)?(?:previous|prior|earlier|recent|past|original|conference|workshop)\s+(?:[\w-]+\s+){0,3}?(?:works?|papers?|study|studies|system|method|models?|approach|framework|tool|publications?|results?|version|benchmark|dataset|algorithm|architecture)\b"
    r"|\bour\s+(?:own\s+)?(?:previous|prior|earlier|recent|past|original|preliminary|conference|workshop)\s+(?:[\w-]+\s+){0,3}?(?:works?|papers?|models?|methods?|systems?|frameworks?|study|studies|publications?|versions?)\s*\((?:[A-Z][\w'À-ſ-]+\s+et\s+al\.?|[A-Z][\w'À-ſ-]+\s+(?:and|&)\s+[A-Z][\w'À-ſ-]+|\d)"
)


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


# Shorter than this, a surname ("Li", "Wu") would match inside unrelated
# GitHub account and repository names.
MIN_SURNAME_CHARS = 4


def _author_surnames(terms: list[tuple[str, str]]) -> set[str]:
    """Lowercase surnames of the authors, for substring matching in GitHub links."""
    surnames: set[str] = set()
    for label, term in terms:
        if label == "author name":
            parts = re.findall(r"[A-Za-z]+", term)
            if len(parts) >= 2 and len(parts[-1]) >= MIN_SURNAME_CHARS:
                surnames.add(parts[-1].lower())
    return surnames


def _identifying_part(owner: str, repo: str, handles: set[str], surnames: set[str]) -> str | None:
    """The author handle or surname a GitHub owner/repo name contains, if any.

    Handles must equal the owner; surnames may appear anywhere in the owner or
    repository name, with separators ignored (``pachterlab``, ``rich-lab``).
    """
    if owner.lower() in handles:
        return owner
    for name in (owner, repo):
        flat = re.sub(r"[^a-z]", "", name.lower())
        for surname in sorted(surnames):
            if surname in flat:
                return surname
    return None


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
            yield Finding(where, "identifying information", f"anonymity: image metadata {label} is '{str(value).strip()}'", LEVEL_LIKELY)


def _scan_office_people(where: str, data: bytes) -> Iterator[Finding]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            parts = {n: zf.read(n).decode("utf-8", errors="replace") for n in zf.namelist() if n.lower().endswith(".xml") and "theme/" not in n}
    except Exception:
        return
    for tag, value in _OFFICE_PEOPLE.findall(parts.get("docProps/core.xml", "") + parts.get("meta.xml", "")):
        if not _is_anonymous_value(value):
            label = "last modified by" if tag == "cp:lastModifiedBy" else "author"
            yield Finding(where, "identifying information", f"anonymity: document properties list {label} '{html.unescape(value).strip()}'", LEVEL_ERROR)
    for tag, value in _OFFICE_APP_PEOPLE.findall(parts.get("docProps/app.xml", "")):
        if not _is_anonymous_value(value):
            yield Finding(where, "identifying information", f"anonymity: document properties list {tag.lower()} '{html.unescape(value).strip()}'")
    reviewers = {html.unescape(next(g for g in groups if g)).strip() for xml in parts.values() for groups in _OFFICE_REVIEWER.findall(xml)}
    for reviewer in sorted(r for r in reviewers if not _is_anonymous_value(r)):
        yield Finding(where, "identifying information", f"anonymity: a comment or tracked change is by '{reviewer}'; remove personal information from the document", LEVEL_LIKELY)


def _scan_office(where: str, data: bytes, terms: list[tuple[str, str]]) -> Iterator[Finding]:
    """The people named in an Office file's properties, and its text: body, headers, footers, notes, comments."""
    yield from _scan_office_people(where, data)
    parts = _office_parts(data)
    if parts is None:
        yield _not_scanned(where, "not a readable Office file", _ANON_PREFIX)
        return
    for part, text in parts:
        if not part.startswith("docProps/") and part != "meta.xml":
            yield from scan_text(where, text, terms, is_manuscript=part in _OFFICE_BODY_PARTS)


def _pdf_pages_and_links(path: Path) -> tuple[list[str], list[tuple[int, str]]]:
    """Each page's text and the (page index, URI) of every link annotation (empty on failure)."""
    try:
        from pypdf import PdfReader

        from .manuscript import pdf_raw_pages

        reader = PdfReader(str(path))
        raw = pdf_raw_pages(path)
        shared = raw[0] if raw is not None and len(raw[0]) == len(reader.pages) else None
        pages, links = [], []
        for i, page in enumerate(reader.pages):
            if shared is not None:
                pages.append(shared[i])
            else:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:
                    pages.append("")
            for annot in page.get("/Annots") or []:
                try:
                    uri = (annot.get_object().get("/A") or {}).get("/URI")
                except Exception:
                    continue
                if uri:
                    links.append((i, str(uri)))
        return pages, links
    except Exception:
        logger.debug("could not read pages/links of %s", path, exc_info=True)
        return [], []


def title_block_findings(where: str, first_page: str) -> Iterator[Finding]:
    """Author names printed on page 1 of a PDF under anonymous review.

    Reads the title block (page 1 above the abstract). A block that says "Anonymous" is
    the anonymized one; otherwise an e-mail address, two or more lines naming
    institutions, or the camera-ready running head ("Published as a conference paper at")
    mean the author block is printed. Works without the authors' names in the .sub.
    """
    if not first_page.strip():
        return
    head = first_page[:300]
    if _CAMERA_READY_HEAD.search(head):
        yield Finding(where, "identifying information", "anonymity: page 1 carries the camera-ready running head ('Published as a conference paper at ...'), the non-anonymous style option, which prints the author names; use the submission/anonymous option", LEVEL_LIKELY)
        return
    end = _ABSTRACT_HEADING.search(first_page)
    block = first_page[: end.start()] if end and end.start() < 2500 else first_page[:1200]
    if re.search(r"(?i)\banonymous\b|double[- ]blind|under\s+(?:double[- ]blind\s+)?review\b(?!\s+as\s+a)", block):
        return
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    emails = [m.group(0) for m in _EMAIL.finditer(block)]
    affiliations = [ln for ln in lines if len(ln) <= 160 and _AFFILIATION_LINE.search(ln) and not re.search(r"[.?!]\s+[a-z]", ln)]
    if emails:
        yield Finding(where, "identifying information", f"anonymity: page 1 prints an e-mail address above the abstract ('{emails[0]}'), so the author block is not anonymized", LEVEL_LIKELY)
    elif len(affiliations) >= 2 or (affiliations and (re.search(r"[A-Z][a-z]+\s+[A-Z][a-z]+(?:\d|\s*[*†‡∗])", block) or any(_NAME_LIST_LINE.match(ln) for ln in lines))):
        yield Finding(where, "identifying information", f"anonymity: page 1 names institutions above the abstract ('{affiliations[0][:80]}'), so the author block looks not anonymized", LEVEL_LIKELY)


def _scan_pdf(where: str, path: Path, terms: list[tuple[str, str]]) -> Iterator[Finding]:
    read = _read_pdf(path)
    if read.problem:
        yield _not_scanned(where, read.problem, _ANON_PREFIX)
    elif not read.text.strip() and read.pages > 1:  # one such page is usually a figure
        yield _not_scanned(where, "no text layer, so the pages could not be searched", _ANON_PREFIX)
    if read.text:
        yield from scan_text(where, read.text, terms)
    pages, links = _pdf_pages_and_links(path)
    if pages:
        yield from title_block_findings(where, pages[0])
    # Links that exist only as hyperlinks ("available at GMN_ESN_Examples"), on pages before
    # the reference list, where a repository link is the authors' own.
    ref_page = next((i for i, p in enumerate(pages) if _REFERENCE_START.search(p)), len(pages))
    body_links = sorted({uri for i, uri in links if i < ref_page})
    if body_links:
        yield from scan_text(where, "\n".join(body_links), terms, is_manuscript=False)
    for key, value in _pdf_metadata_text(path).items():
        if key in _PDF_AUTHOR_KEYS and not _is_anonymous_value(value):
            yield Finding(where, "identifying information", f"anonymity: PDF metadata {key.lstrip('/')} is '{value.strip()}'; clear it (e.g. \\hypersetup{{pdfauthor={{}}}})", LEVEL_ERROR)
        else:
            yield from scan_text(f"{where} [metadata {key}]", value, terms, is_manuscript=False)


def _scan_blob(where: str, name: str, data: bytes, terms: list[tuple[str, str]]) -> Iterator[Finding]:
    """Scan one archive member (or a small top-level file) by its extension."""
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_EXTS:
        yield from _scan_image_people(where, data)
        return
    if suffix in OFFICE_EXTS:
        yield from _scan_office(where, data, terms)
        return
    if suffix in OPAQUE_EXTS:
        yield _not_scanned(where, f"{suffix} files cannot be read", _ANON_PREFIX)
        return
    if suffix == ".pdf" and data:
        with _member_file(name, data) as pdf:
            yield from _scan_pdf(where, pdf, terms)
        return
    if len(data) > MAX_TEXT_BYTES:
        yield _not_scanned(where, f"larger than {MAX_TEXT_BYTES // (1024 * 1024)} MB", _ANON_PREFIX)
        return
    if suffix == ".bib":
        # A .bib holds the works cited, where the authors' own papers belong, but
        # reference managers leave the paths of their PDF files in it.
        text = _decode(data)
        if text is not None:
            yield from _home_path_findings(where, text)
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
        if suffix in ARCHIVE_EXTS or double in ARCHIVE_EXTS:
            unread: list[str] = []
            for count, (member, data) in enumerate(_iter_archive_members(path)):
                if count >= MAX_ARCHIVE_MEMBERS:
                    findings.append(_not_scanned(name, f"only the first {MAX_ARCHIVE_MEMBERS} archive entries were read", _ANON_PREFIX))
                    break
                where = f"{name}:{member}"
                findings.extend(scan_text(where, member, terms, is_manuscript=False))
                if data is UNREAD:
                    unread.append(member)
                elif data:
                    findings.extend(_scan_blob(where, member, data, terms))
            if unread:
                findings.append(_not_scanned(name, _unread_summary(unread), _ANON_PREFIX))
        elif suffix == ".pdf":
            findings.extend(_scan_pdf(name, path, terms))
        else:
            limit = MAX_BLOB_BYTES if suffix in IMAGE_EXTS or suffix in OFFICE_EXTS else MAX_TEXT_BYTES
            data = _read_capped(path, limit)
            if data is UNREAD:
                findings.append(_not_scanned(name, f"larger than {limit // (1024 * 1024)} MB", _ANON_PREFIX))
            else:
                findings.extend(_scan_blob(name, name, data, terms))
    except Exception as exc:
        logger.debug("could not scan %s for identifying information", path, exc_info=True)
        findings.append(_not_scanned(name, type(exc).__name__, _ANON_PREFIX))
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


def _list_repo_files(repo: str) -> tuple[list[tuple[str, int]], str | None, bool, list[str]]:
    """Walk a mirror's tree: ``(files, error, truncated, unlisted folders)``.

    ``files`` is ``(path, size)`` per file. Directory entries come back from the
    API without a ``size``; their children are listed with ``?path=<dir>``.
    ``error`` is the service's error code (or a short description) when the root
    listing fails.
    """
    files: list[tuple[str, int]] = []
    unlisted: list[str] = []
    pending = [""]
    truncated = False
    first = True
    while pending:
        directory = pending.pop(0)
        status, body = _http_get(_api(repo, "files/?path=" + urllib.parse.quote(directory)))
        if status != 200:
            if first:
                return [], _api_error(body) or (f"HTTP {status}" if status else "unreachable"), False, []
            logger.debug("could not list %s in anonymous repo %s (HTTP %s)", directory, repo, status)
            unlisted.append(directory)
            continue
        try:
            entries = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            entries = None
        if not isinstance(entries, list):
            if first:
                return [], "an unexpected response", False, []
            unlisted.append(directory)
            continue
        first = False
        for entry in entries:
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
    return files, None, truncated, unlisted


def scan_anonymous_repo(repo: str, terms: list[tuple[str, str]]) -> list[Finding]:
    """Fetch an anonymous.4open.science mirror and scan its files for identity leaks."""
    where = f"anonymous.4open.science/r/{repo}"
    files, error, truncated, unlisted = _list_repo_files(repo)
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

    missed = unlisted + [p for p, s in files if Path(p).suffix.lower() not in _BINARY_EXTS and s > MAX_REPO_FILE_BYTES] + [p for p, body in fetched if body is None]
    if missed:
        shown = ", ".join(missed[:3]) + (", ..." if len(missed) > 3 else "")
        findings.append(Finding("submission", NOT_FETCHED, f"anonymity: {len(missed)} item(s) of {where} could not be fetched and were not checked ({shown}); inspect them manually"))
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
                    findings.append(Finding(file_where, "identifying information", f"anonymity: copyright line names '{holder}'; add it to the terms Anonymous GitHub redacts", LEVEL_LIKELY))
    if truncated:
        findings.append(Finding("submission", NOT_FETCHED, f"anonymity: only the first {MAX_REPO_FILES} files of {where} were checked; inspect the rest manually"))
    return findings


# --- entry point -----------------------------------------------------------------


def _text_blobs(paths: list[Path]) -> Iterator[str]:
    """Plain text of each attachment, for finding the repository links they cite."""
    from .sensitive import _iter_text_blobs

    for path in paths:
        for _, text in _iter_text_blobs(path):
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
