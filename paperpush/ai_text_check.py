"""Estimate how much of the manuscript's text reads as AI-generated, through an AI-text detector.

Opt-in only (``paperpush validate --ai-detection``): the check sends the
manuscript's text to a third-party service, which costs credits on the
author's account. One service is supported:

* Pangram -- ``POST https://text.external-api.pangram.com/task`` with the key in
  ``x-api-key`` (``PANGRAM_API_KEY``), then ``GET .../task/{task_id}`` until
  the task's ``stage`` is ``STAGE_SUCCESS`` or ``STAGE_FAILED``.

The text scanned is the manuscript's own, with its reference list cut out (the
same text the plagiarism scan sends). The result is always shown: the shares
of AI-generated, AI-assisted, and human text. A share of AI-generated text at
or over ``threshold`` percent is reported at the *similarity* level (pink in
the report), the level kept for the plagiarism and AI-text scans: a
probability from a classifier, never an error and never a warning. Detectors
misjudge some human writing (non-native English among it), and many venues
allow AI-assisted writing when it is disclosed, so the finding says whether
the manuscript has a statement on AI use, and the author decides.

Nothing in paperpush rewrites text to lower this score: the remedy for AI use a
venue allows is the disclosure its guidelines ask for.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PROVIDERS = ("pangram",)  # the first is the default
PROVIDER_NAMES = {"pangram": "Pangram"}
# The environment variables each provider's key is read from, in order.
KEY_ENV = {"pangram": ("PANGRAM_API_KEY", "PANGRAM_LABS_API_KEY")}
TASK_URL = "https://text.external-api.pangram.com/task"
# Characters per task. Pangram documents no limit; long text is split between
# paragraphs so no single task is huge, and the shares are merged by word count.
CHUNK_CHARS = 100_000
MIN_CHARS = 100
TIMEOUT = 300.0  # seconds to wait for all of a task's polls together
POLL_SECONDS = 2.0
DEFAULT_THRESHOLD = 20.0
# AI-generated passages listed one per line under the finding; the rest are counted.
SHOWN_PASSAGES = 8
AI_LABELS = ("ai-generated", "ai generated")

# A sentence that says AI tools were used (or not) in preparing the manuscript.
_AI_TOOL = r"(?:chat\s?gpt|gpt-?\d|claude|gemini|copilot|grammarly|deepl write|large language models?|llms?|generative (?:ai|artificial intelligence)|ai[- ](?:assisted|based|powered|generated)|ai (?:tools?|models?|assistants?|systems?|technolog(?:y|ies))|artificial intelligence (?:tools?|models?|assistants?|systems?|technolog(?:y|ies)))"
_AI_USE = r"(?:used|use of|using|assist|declar|disclos|edit|writ|draft|polish|prepar|proofread|did not|no )"
_DISCLOSURE = re.compile(rf"[^.\n]*\b{_AI_TOOL}\b[^.\n]*\b{_AI_USE}[^.\n]*|[^.\n]*\b{_AI_USE}[^.\n]*\b{_AI_TOOL}\b[^.\n]*", re.IGNORECASE)
_DISCLOSURE_HEADING = re.compile(r"^\s*(?:declaration of )?(?:generative )?(?:ai|artificial intelligence)[- ](?:use|usage|disclosure|declaration|statement)", re.IGNORECASE | re.MULTILINE)


class AiTextError(Exception):
    """The service could not be asked, or refused (the message says why)."""


@dataclass(frozen=True)
class AiTextOptions:
    provider: str
    api_key: str
    threshold: float = DEFAULT_THRESHOLD
    model: str | None = None  # Pangram's model selector; None leaves it to the account's default

    @property
    def name(self) -> str:
        return PROVIDER_NAMES[self.provider]


@dataclass
class Passage:
    text: str
    label: str
    score: float = 0.0  # the service's 0-1 AI-assistance score
    confidence: str = ""
    words: int = 0
    humanized: bool = False

    @property
    def is_ai(self) -> bool:
        return self.label.strip().lower() in AI_LABELS


@dataclass
class ScanResult:
    provider: str
    fraction_ai: float  # 0-1, AI-generated
    fraction_ai_assisted: float
    fraction_human: float
    words: int
    headline: str = ""
    version: str = ""
    passages: list[Passage] = field(default_factory=list)

    @property
    def percent_ai(self) -> float:
        return round(100 * self.fraction_ai, 1)

    @property
    def percent_ai_assisted(self) -> float:
        return round(100 * self.fraction_ai_assisted, 1)

    @property
    def percent_human(self) -> float:
        return round(100 * self.fraction_human, 1)


def options(provider: str | None = None, api_key: str | None = None, threshold: float | None = None, model: str | None = None) -> AiTextOptions:
    """The options for a scan; ValueError when no key can be found."""
    provider = (provider or "").strip().lower() or PROVIDERS[0]
    if provider not in PROVIDERS:
        raise ValueError(f"unknown AI-detection provider {provider!r}: use one of {', '.join(PROVIDERS)}")
    api_key = (api_key or "").strip() or next((os.environ[v].strip() for v in KEY_ENV[provider] if os.environ.get(v, "").strip()), None)
    if not api_key:
        raise ValueError(f"the AI-text detection needs an API key: set {KEY_ENV[provider][0]} (or pass --ai-detection-api-key)")
    if threshold is not None and not 0 <= threshold <= 100:
        raise ValueError("the AI-detection threshold is a percentage, 0 to 100")
    return AiTextOptions(provider=provider, api_key=api_key, threshold=DEFAULT_THRESHOLD if threshold is None else float(threshold), model=(model or "").strip() or None)


# --- the service ------------------------------------------------------------


def _request(url: str, headers: dict, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST", headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "paperpush-validate", **headers})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # fixed https host  # nosec B310
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            payload = ""
        reason = {
            401: "the API key was refused, or the account is out of credits",
            402: "the account is out of credits",
            403: "the API key was refused, or the model is not enabled for it",
            422: "the service refused the model selector",
            429: "too many requests; try again later",
            503: "the service is temporarily unavailable",
        }.get(exc.code, f"HTTP {exc.code}")
        raise AiTextError(f"{reason}{': ' + payload if payload and exc.code not in (401, 403) else ''}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AiTextError(f"could not reach the service ({exc})") from None
    except json.JSONDecodeError:
        raise AiTextError("the service's answer was not JSON") from None


def _scan_pangram(text: str, opts: AiTextOptions, *, sleep=time.sleep, clock=time.monotonic) -> ScanResult:
    headers = {"x-api-key": opts.api_key}
    body: dict = {"text": text}
    if opts.model:
        body["model"] = opts.model
    task_id = _request(TASK_URL, headers, body).get("task_id")
    if not task_id:
        raise AiTextError("the service did not start a task")
    deadline = clock() + TIMEOUT
    while True:
        data = _request(f"{TASK_URL}/{task_id}", headers)
        stage = str(data.get("stage") or "")
        if stage == "STAGE_SUCCESS":
            break
        if stage == "STAGE_FAILED":
            raise AiTextError(f"the scan failed: {data.get('headline') or 'no reason given'}")
        if clock() >= deadline:
            raise AiTextError(f"no answer after {TIMEOUT:g} seconds")
        sleep(POLL_SECONDS)
    scanned = data.get("text") or text
    passages = []
    for w in data.get("windows") or []:
        start, end = w.get("start_index"), w.get("end_index")
        passage = w.get("text") or (scanned[start:end] if isinstance(start, int) and isinstance(end, int) else "")
        passages.append(
            Passage(
                text=passage,
                label=str(w.get("label") or ""),
                score=float(w.get("ai_assistance_score") or 0),
                confidence=str(w.get("confidence") or ""),
                words=int(w.get("word_count") or len(passage.split())),
                humanized=bool(w.get("is_humanized")),
            )
        )
    return ScanResult(
        provider="pangram",
        fraction_ai=float(data.get("fraction_ai") or 0),
        fraction_ai_assisted=float(data.get("fraction_ai_assisted") or 0),
        fraction_human=float(data.get("fraction_human") or 0),
        words=len(text.split()),
        headline=str(data.get("headline") or ""),
        version=str(data.get("version") or ""),
        passages=passages,
    )


_SCANNERS = {"pangram": _scan_pangram}


def chunks(text: str, size: int) -> list[str]:
    """``text`` in pieces of at most ``size`` characters, split between paragraphs where possible."""
    from .plagiarism_check import chunks as _chunks

    return _chunks(text, size)


def scan(text: str, opts: AiTextOptions) -> ScanResult:
    """Scan ``text`` with ``opts.provider``, in chunks, and merge the shares by word count."""
    pieces = [p for p in chunks(text, CHUNK_CHARS) if len(p) >= MIN_CHARS]
    if not pieces:
        raise AiTextError(f"the manuscript has under {MIN_CHARS} characters of text to scan")
    parts = [_SCANNERS[opts.provider](piece, opts) for piece in pieces]
    if len(parts) == 1:
        return parts[0]
    words = sum(p.words for p in parts) or 1

    def mean(attr: str) -> float:
        return sum(getattr(p, attr) * p.words for p in parts) / words

    return ScanResult(
        provider=opts.provider,
        fraction_ai=mean("fraction_ai"),
        fraction_ai_assisted=mean("fraction_ai_assisted"),
        fraction_human=mean("fraction_human"),
        words=sum(p.words for p in parts),
        headline="; ".join(dict.fromkeys(p.headline for p in parts if p.headline)),
        version=parts[0].version,
        passages=[w for p in parts for w in p.passages],
    )


# --- the check --------------------------------------------------------------


def disclosure(text: str) -> str:
    """The manuscript's statement on AI use (a heading or a sentence naming an AI tool and its use), or ""."""
    heading = _DISCLOSURE_HEADING.search(text)
    if heading:
        return _snippet(text[heading.start() : heading.start() + 300])
    match = _DISCLOSURE.search(text)
    return _snippet(match.group(0)) if match else ""


def _snippet(text: str, n: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def summary(result: ScanResult, opts: AiTextOptions) -> str:
    """One line for the report: the shares, what was scanned, and the threshold."""
    model = f" (model {result.version})" if result.version else ""
    line = f"{opts.name}{model}: {result.percent_ai:g}% AI-generated, {result.percent_ai_assisted:g}% AI-assisted, {result.percent_human:g}% human, over {result.words:,} words"
    if result.headline:
        line += f' ("{result.headline}")'
    line += f"; flags at {opts.threshold:g}% AI-generated. A classifier's estimate, not proof: detectors misjudge some human writing."
    return line


def check_text(text: str, opts: AiTextOptions, field_id: str = "manuscript", *, full_text: str | None = None):
    """``(issues, detail)`` for ``text``: SIMILARITY findings when the AI-generated share reaches the threshold.

    ``full_text`` (the manuscript with its reference list) is searched for a
    statement on AI use; it defaults to ``text``.
    """
    from .validate import SIMILARITY, Issue

    result = scan(text, opts)
    issues: list = []
    if result.percent_ai >= opts.threshold:
        statement = disclosure(full_text if full_text is not None else text)
        if statement:
            advice = f'The manuscript has a statement on AI use ("{statement}"): check that it covers this text and says what the venue\'s guidelines ask.'
        else:
            advice = "The manuscript has no statement on AI use: if AI tools helped write it, add the disclosure the venue's guidelines ask for; if not, no change is needed."
        issues.append(Issue(SIMILARITY, field_id, f"AI text: {opts.name} estimates {result.percent_ai:g}% of the text is AI-generated ({result.percent_ai_assisted:g}% AI-assisted). This is a probability, not proof. {advice}"))
        flagged = [p for p in result.passages if p.is_ai and p.confidence.lower() != "low"]
        for p in flagged[:SHOWN_PASSAGES]:
            conf = f", {p.confidence.lower()} confidence" if p.confidence else ""
            issues.append(Issue(SIMILARITY, field_id, f'AI text: passage read as AI-generated{conf}: "{_snippet(p.text)}"'))
        if len(flagged) > SHOWN_PASSAGES:
            issues.append(Issue(SIMILARITY, field_id, f"AI text: and {len(flagged) - SHOWN_PASSAGES} more passage(s) read as AI-generated"))
    humanized = [p for p in result.passages if p.humanized]
    if humanized:
        issues.append(Issue(SIMILARITY, field_id, f'AI text: {opts.name} reads {len(humanized)} passage(s) as AI text reworded by a "humanizer" tool, e.g. "{_snippet(humanized[0].text)}"'))
    return issues, summary(result, opts)
