"""Check the manuscript for text that matches published sources, through a plagiarism API.

Opt-in only (``paperpush validate --plagiarism``): the check sends the
manuscript's text to a third-party service, which costs credits on the
author's account. Two services are supported (Winston AI is the default):

* Winston AI -- ``POST https://api.gowinston.ai/v2/plagiarism`` with a Bearer
  key (``WINSTON_API_KEY``); 2 credits per word.
* Originality.ai -- ``POST https://api.originality.ai/api/v3/scan`` with the key
  in ``X-OAI-API-KEY`` (``ORIGINALITY_API_KEY``). The scan is not stored on
  their side (``storeScan: false``).

The text scanned is the manuscript's own (its reference list cut out, since
references always match the works they cite), split into chunks under each
service's size limit. The result is always shown: the similarity score and the
matching sources, so the author can see that a match is their own preprint.
``accepted_urls`` lists sources that are fine to match (the author's preprint,
an earlier conference version): they are still listed, marked accepted, and do
not count toward the threshold. Similarity at or over ``threshold`` percent is
reported at the *similarity* level (pink in the report), the level kept for the
plagiarism and AI-text scans: never an error, and not counted with the
warnings, since methods sections, standard phrases, and quotations always
match something, so a score needs a reader. Characters that hide text from the
scan are a likely error.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PROVIDERS = ("winston", "originality")  # the first is the default
PROVIDER_NAMES = {"originality": "Originality.ai", "winston": "Winston AI"}
# The environment variables each provider's key is read from, in order.
KEY_ENV = {"originality": ("ORIGINALITY_API_KEY", "ORIGINALITY_AI_API_KEY"), "winston": ("WINSTON_API_KEY", "WINSTON_AI_API_KEY", "GOWINSTON_API_KEY")}
URLS = {"originality": "https://api.originality.ai/api/v3/scan", "winston": "https://api.gowinston.ai/v2/plagiarism"}
# Characters per request. Winston takes 100 to 120,000; Originality.ai suggests
# splitting long text, as a plagiarism scan can take a minute.
CHUNK_CHARS = {"originality": 40_000, "winston": 110_000}
MIN_CHARS = 100
TIMEOUT = 300.0
DEFAULT_THRESHOLD = 20.0
# Sources listed in the check's note; every unaccepted source at or over
# SOURCE_ISSUE_PERCENT also gets a line of its own when the check warns.
SHOWN_SOURCES = 8
SOURCE_ISSUE_PERCENT = 1.0


class PlagiarismError(Exception):
    """The service could not be asked, or refused (the message says why)."""


@dataclass(frozen=True)
class PlagiarismOptions:
    provider: str
    api_key: str
    accepted_urls: tuple[str, ...] = ()
    threshold: float = DEFAULT_THRESHOLD

    @property
    def name(self) -> str:
        return PROVIDER_NAMES[self.provider]


@dataclass
class Source:
    url: str
    title: str = ""
    percent: float = 0.0  # share of the scanned words matching this source
    words: int = 0
    passages: list[str] = field(default_factory=list)
    accepted: bool = False


@dataclass
class ScanResult:
    provider: str
    score: float  # percent of the text matching a source, accepted sources included
    words: int
    sources: list[Source]
    credits: int | None = None
    flags: list[str] = field(default_factory=list)  # e.g. hidden characters the service found

    @property
    def unaccepted_score(self) -> float:
        """The score without the accepted sources (see :func:`_score_without`)."""
        return _score_without(self)


def options(provider: str | None = None, api_key: str | None = None, accepted_urls=(), threshold: float | None = None) -> PlagiarismOptions:
    """The options for a scan; ValueError when no key can be found.

    ``provider`` defaults to the first of :data:`PROVIDERS` whose key is in the
    environment, and ``api_key`` to that environment variable. ``accepted_urls``
    may hold comma-separated URLs.
    """
    provider = (provider or "").strip().lower() or None
    if provider is not None and provider not in PROVIDERS:
        raise ValueError(f"unknown plagiarism provider {provider!r}: use one of {', '.join(PROVIDERS)}")
    api_key = (api_key or "").strip() or None
    if api_key is None:
        for candidate in [provider] if provider else PROVIDERS:
            api_key = next((os.environ[v].strip() for v in KEY_ENV[candidate] if os.environ.get(v, "").strip()), None)
            if api_key:
                provider = candidate
                break
    if api_key is None:
        wanted = KEY_ENV[provider][0] if provider else " or ".join(KEY_ENV[p][0] for p in PROVIDERS)
        raise ValueError(f"the plagiarism check needs an API key: set {wanted} (or pass --plagiarism-api-key)")
    if provider is None:
        provider = PROVIDERS[0]
    urls = tuple(u.strip() for raw in accepted_urls or () for u in str(raw).split(",") if u.strip())
    if threshold is not None and not 0 <= threshold <= 100:
        raise ValueError("the plagiarism threshold is a percentage, 0 to 100")
    return PlagiarismOptions(provider=provider, api_key=api_key, accepted_urls=urls, threshold=DEFAULT_THRESHOLD if threshold is None else float(threshold))


# --- accepted sources -------------------------------------------------------


def _url_key(url: str) -> tuple[str, str]:
    """``(host, path)`` of ``url``, lower-cased, without ``www.``, a scheme, or a trailing slash.

    A bare DOI (``10.1101/...``) is read as its doi.org URL.
    """
    url = url.strip()
    if re.match(r"^10\.\d{4,9}/", url):
        url = "https://doi.org/" + url
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    return host, parts.path.rstrip("/").lower()


def is_accepted(url: str, accepted: tuple[str, ...]) -> bool:
    """Whether ``url`` is one of ``accepted``: a domain covers its subdomains, a URL the pages under it.

    ``biorxiv.org`` accepts every bioRxiv page; ``biorxiv.org/content/10.1101/2025.01.01.123456``
    accepts that preprint in each of its versions and formats, and so does the bare DOI.
    """
    host, path = _url_key(url)
    for raw in accepted:
        a_host, a_path = _url_key(raw)
        # A DOI also accepts any page carrying it (biorxiv.org/content/<doi>v2.full).
        if a_host in ("doi.org", "dx.doi.org") and a_path.startswith("/10."):
            doi = re.sub(r"v\d+$", "", a_path[1:])
            if re.search(re.escape(doi) + r"(v\d+)?([./?#]|$)", urllib.parse.unquote(path)):
                return True
        if not a_host or not (host == a_host or host.endswith("." + a_host)):
            continue
        if not a_path:
            return True
        # Any version (v2) and format (.full, .full.pdf, /abstract) of the page.
        base = re.sub(r"v\d+$", "", a_path)
        if re.fullmatch(re.escape(base) + r"(v\d+)?([./].*)?", path):
            return True
    return False


def _score_without(result: ScanResult) -> float:
    """The score with the accepted sources taken out.

    When nothing is accepted, the service's own score. Otherwise the score is
    scaled by the share of matched words that come from unaccepted sources, the
    best measure the services' answers allow (matches can overlap). For Winston
    the accepted URLs are also sent as ``excluded_sources``, so its score has
    them out already.
    """
    if not any(s.accepted for s in result.sources):
        return result.score
    if result.provider == "winston":
        return result.score
    total = sum(s.words or len(" ".join(s.passages).split()) for s in result.sources)
    kept = sum(s.words or len(" ".join(s.passages).split()) for s in result.sources if not s.accepted)
    return 0.0 if total == 0 else round(result.score * kept / total, 1)


# --- the services -----------------------------------------------------------


def _post(url: str, body: dict, headers: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "paperpush-validate", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # fixed https hosts  # nosec B310
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            payload = ""
        reason = {401: "the API key was refused", 402: "the account is out of credits", 403: "the API key was refused", 429: "too many requests; try again later"}.get(exc.code, f"HTTP {exc.code}")
        raise PlagiarismError(f"{reason}{': ' + payload if payload and exc.code not in (401, 403) else ''}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PlagiarismError(f"could not reach the service ({exc})") from None
    except json.JSONDecodeError:
        raise PlagiarismError("the service's answer was not JSON") from None


def _scan_winston(text: str, opts: PlagiarismOptions) -> ScanResult:
    body: dict = {"text": text, "language": "auto"}
    if opts.accepted_urls:
        body["excluded_sources"] = list(opts.accepted_urls)
    data = _post(URLS["winston"], body, {"Authorization": f"Bearer {opts.api_key}"})
    result = data.get("result") or {}
    words = int(result.get("textWordCounts") or len(text.split()))
    sources = []
    for s in data.get("sources") or []:
        url = s.get("url") or ""
        passages = [p.get("sequence") for p in s.get("plagiarismFound") or [] if p.get("sequence")]
        sources.append(Source(url=url, title=s.get("title") or "", percent=float(s.get("score") or 0), words=int(s.get("plagiarismWords") or 0), passages=passages, accepted=bool(s.get("is_excluded")) or is_accepted(url, opts.accepted_urls)))
    attack = data.get("attackDetected") or {}
    flags = [label for key, label in (("zero_width_space", "zero-width spaces"), ("homoglyph_attack", "look-alike (homoglyph) characters")) if attack.get(key)]
    return ScanResult(provider="winston", score=float(result.get("score") or 0), words=words, sources=sources, credits=data.get("credits_used"), flags=flags)


def _scan_originality(text: str, opts: PlagiarismOptions) -> ScanResult:
    body = {
        "title": "paperpush validate",
        "content": text,
        "check_ai": False,
        "check_plagiarism": True,
        "check_facts": False,
        "check_readability": False,
        "check_grammar": False,
        "storeScan": False,
        "aiModelVersion": "model-5.0",
    }
    data = _post(URLS["originality"], body, {"X-OAI-API-KEY": opts.api_key})
    results = data.get("results") or data
    plag = results.get("plagiarism") or {}
    words = len(text.split())
    by_url: dict[str, Source] = {}
    for match in plag.get("results") or []:
        phrase = match.get("phrase") or ""
        for hit in match.get("results") or []:
            url = hit.get("link") or ""
            src = by_url.get(url)
            if src is None:
                src = by_url[url] = Source(url=url, title=hit.get("title") or "", accepted=is_accepted(url, opts.accepted_urls))
            if phrase and phrase not in src.passages:
                src.passages.append(phrase)
    for src in by_url.values():
        src.words = sum(len(p.split()) for p in src.passages)
        src.percent = round(100 * src.words / words, 1) if words else 0.0
    credits = (results.get("credits") or {}).get("used")
    return ScanResult(provider="originality", score=float(plag.get("score") or 0), words=words, sources=list(by_url.values()), credits=credits)


_SCANNERS = {"originality": _scan_originality, "winston": _scan_winston}


def chunks(text: str, size: int) -> list[str]:
    """``text`` in pieces of at most ``size`` characters, split between paragraphs where possible."""
    text = text.strip()
    out: list[str] = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = text.rfind(" ", 0, size)
        if cut <= 0:
            cut = size
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


def scan(text: str, opts: PlagiarismOptions) -> ScanResult:
    """Scan ``text`` with ``opts.provider``, in chunks, and merge the answers.

    The merged score is the chunks' scores weighted by their word counts; a
    source found in several chunks is listed once with its words added up.
    """
    pieces = [p for p in chunks(text, CHUNK_CHARS[opts.provider]) if len(p) >= MIN_CHARS]
    if not pieces:
        raise PlagiarismError(f"the manuscript has under {MIN_CHARS} characters of text to scan")
    parts = [_SCANNERS[opts.provider](piece, opts) for piece in pieces]
    if len(parts) == 1:
        return parts[0]
    words = sum(p.words for p in parts)
    merged: dict[str, Source] = {}
    for part in parts:
        for s in part.sources:
            m = merged.setdefault(s.url, Source(url=s.url, title=s.title, accepted=s.accepted))
            m.words += s.words or round(s.percent * part.words / 100)
            m.passages += [p for p in s.passages if p not in m.passages]
    for m in merged.values():
        m.percent = round(100 * m.words / words, 1) if words else 0.0
    score = round(sum(p.score * p.words for p in parts) / words, 1) if words else 0.0
    credits = sum(p.credits for p in parts if p.credits is not None) if any(p.credits is not None for p in parts) else None
    flags = sorted({f for p in parts for f in p.flags})
    return ScanResult(provider=opts.provider, score=score, words=words, sources=list(merged.values()), credits=credits, flags=flags)


# --- the check --------------------------------------------------------------


def _snippet(text: str, n: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def summary(result: ScanResult, opts: PlagiarismOptions) -> str:
    """One line for the report: the score, what was scanned, and the top sources."""
    score = result.unaccepted_score
    parts = [f"{opts.name}: {result.score:g}% of {result.words:,} words match other sources"]
    if score != result.score:
        parts[0] += f" ({score:g}% without the accepted sources)"
    parts[0] += f"; warns at {opts.threshold:g}%"
    if result.credits is not None:
        parts[0] += f"; {result.credits:,} credits used"
    ranked = sorted(result.sources, key=lambda s: -s.percent)
    if ranked:
        shown = [f"{s.url} {s.percent:g}%{' (accepted)' if s.accepted else ''}" for s in ranked[:SHOWN_SOURCES]]
        more = len(ranked) - SHOWN_SOURCES
        parts.append("sources: " + "; ".join(shown) + (f"; and {more} more" if more > 0 else ""))
    else:
        parts.append("no matching sources")
    return ". ".join(parts) + "."


def check_text(text: str, opts: PlagiarismOptions, field_id: str = "manuscript"):
    """``(issues, detail)`` for ``text``: SIMILARITY findings when the unaccepted similarity reaches the threshold (the score, then each matching source), and a likely error for text that hides matches."""
    from .validate import LIKELY_ERROR, SIMILARITY, Issue

    result = scan(text, opts)
    issues: list = []
    score = result.unaccepted_score
    if score >= opts.threshold:
        issues.append(Issue(SIMILARITY, field_id, f"plagiarism: {score:g}% of the text matches other sources ({opts.name}); check that each match below is quoted and cited, or is your own earlier version (accept it with --plagiarism-accept URL)"))
        for s in sorted(result.sources, key=lambda s: -s.percent):
            if s.accepted or s.percent < SOURCE_ISSUE_PERCENT:
                continue
            example = f', e.g. "{_snippet(s.passages[0])}"' if s.passages else ""
            issues.append(Issue(SIMILARITY, field_id, f"plagiarism: {s.percent:g}% matches {s.url}{' (' + s.title + ')' if s.title else ''}{example}"))
    for flag in result.flags:
        issues.append(Issue(LIKELY_ERROR, field_id, f"plagiarism: {opts.name} found {flag} in the text, which can hide matching text from similarity checks"))
    return issues, summary(result, opts)
