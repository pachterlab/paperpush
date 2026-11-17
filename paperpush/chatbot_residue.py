"""Text left over from a chatbot: ``integrity.chatbot_residue``.

Some published papers carry a chatbot's reply pasted along with the text
it was asked for: "Certainly, here is a possible introduction for your
topic", "As an AI language model", "Regenerate response". Editors desk-reject
such manuscripts and journals have retracted papers over them. This check
looks for those phrases only: words that have no place in a manuscript
except as the chatbot's own voice.

It does not judge style. Words often called AI tells ("delve", "intricate",
"pivotal") are written by people too, so they are left to the opt-in detector
(``integrity.ai_text``). Unfilled slots ("[Insert citation]", "[Your name]")
are ``details.placeholders``.

False positives cost more here than misses, so the check reads less than the
whole paper:

* the manuscript, title page, and cover letter, not the supplement (where a
  paper on language models prints transcripts);
* in LaTeX, not the verbatim, code, quotation, box, figure, or table
  environments, nor anything after ``\\appendix``;
* in PDF/Word/Markdown text, not the reference list or an appendix;
* not text in double quotation marks;
* not a phrase introduced as an example ("responses such as", "e.g.",
  "starting with") or after a speaker label ("Model: Sure! Here is");
* not text from a figure (a column of short lines, as a PDF's diagram
  text comes out).

What it finds is a LIKELY_ERROR: critical if real, but for the author to
confirm. Using AI is not the issue: the fix is to delete the leftover and to
disclose AI use where the venue asks for it.
"""

from __future__ import annotations

import re
from pathlib import Path

from .database import Venue
from .validate import LIKELY_ERROR, Issue, NotApplicable, Outcome

# (what it is, pattern). Each pattern is narrow on purpose; see the module docstring.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # The ChatGPT interface's button, pasted with the reply. Capitalized and on its own.
    ("the chatbot's 'Regenerate response' button", re.compile(r"(?:^|(?<=[.!?:])\s+|\n)[ \t]*Regenerate response\b(?![ \t]+[a-z])", re.MULTILINE)),
    # Code-block chrome and speaker labels from a copied chat, each a line of its own.
    ("a chat interface label", re.compile(r"^[ \t]*(?:(?:python|bash|latex|r|sql|plaintext|markdown)[ \t]+)?Copy code[ \t]*$|^[ \t]*ChatGPT said:", re.MULTILINE | re.IGNORECASE)),
    # The model speaking of itself.
    # Followed by the comma the reply goes on after ("As an AI language model, I cannot..."), so a
    # phrase like "frames the attack as an AI Assistant Performance Benchmark" is not one.
    ("a chatbot speaking of itself", re.compile(r"\b(?:(?i:as an AI language model|as a large language model)|[Aa]s an AI (?:assistant|developed by \w+|trained by \w+))\s*,|\bI(?:'m| am) an AI (?:language model|assistant)\b")),
    ("a chatbot's knowledge cutoff", re.compile(r"\b(?:as of my (?:last |latest )?(?:knowledge |training )?(?:update|cut-?off)|my knowledge cut-?off|my training (?:data )?(?:only )?(?:goes|extends|runs) (?:up )?(?:to|until))\b", re.IGNORECASE)),
    ("a chatbot's refusal", re.compile(r"\bI(?:'m| am) (?:very |really |so )?sorry,? but (?:as an AI|I (?:can(?:'|no)t|don't|do not|am unable|am not able|don't have))|\bI (?:don't|do not) have (?:access to )?real-time (?:data|information|access)|\bI (?:can(?:'|no)t|am unable to|don't have the ability to) (?:browse|access) the (?:internet|web)\b", re.IGNORECASE)),
    # The reply's opening, addressed to the person who asked.
    ("a chatbot's reply opening", re.compile(
        r"\b(?:Certainly|Sure|Absolutely)\s*[!,.]\s+(?:here(?:'s| is| are)|below is|I(?:'d| would) be (?:happy|glad))\b"
        r"|\bhere(?:'s| is) (?:a |an |the )?(?:possible |revised |rewritten |polished |improved |refined |more concise |shorter |condensed |updated |suggested |draft )*(?:version|introduction|abstract|draft|paragraph|summary|rewrite|conclusion|cover letter)\b[^.\n]{0,40}?\b(?:for your|of your|you (?:requested|asked for|provided))\b",
        re.IGNORECASE,
    )),
    # The reply's sign-off, offering to keep going.
    ("a chatbot's reply sign-off", re.compile(r"\b(?:let me know if you(?:'d| would) like me to|would you like me to (?:revise|expand|shorten|adjust|elaborate|rewrite|add|make)|feel free to (?:adjust|modify|tweak|customi[sz]e) (?:it|this|the (?:text|wording|paragraph|draft|tone)) (?:to|as|further)|I hope this helps)\b", re.IGNORECASE)),
]

# A phrase introduced as an example: "refusals such as As an AI language model, ...".
# A speaker label ("Fine-tuned Model: Sure! Here is ...") counts too: the paper is showing a reply.
_EXAMPLE_LEAD = re.compile(r":\s*$|\b(?:such as|e\.g\.|i\.e\.|for example|for instance|like|phrases?|responses?|outputs?|refus\w*|strings?|templates?|patterns?|prompts?|begin(?:s|ning)? with|start(?:s|ing)? with|contain(?:s|ing)?|the words?)\W*$", re.IGNORECASE)

# Text in double quotation marks, within a paragraph.
_QUOTED = re.compile(r"“[^”\n]{0,600}”|\"[^\"\n]{0,600}\"|``(?:(?!'').){0,600}''", re.DOTALL)

# LaTeX environments whose content may be quoted model output.
_TEX_SKIP_ENVS = r"(?:verbatim|Verbatim|BVerbatim|LVerbatim|lstlisting|minted|alltt|comment|quote|quotation|displayquote|tcolorbox|mdframed|framed|shaded|figure|table|tabular|tabularx|longtable|prompt\w*|chat\w*|dialog\w*|conversation\w*|response\w*|example\w*|transcript\w*|box\w*|\w*box)\*?"
_TEX_ENV = re.compile(r"\\begin\s*\{(" + _TEX_SKIP_ENVS + r")\}.*?\\end\s*\{\1\}", re.DOTALL)
_TEX_INLINE = re.compile(r"\\(?:verb|lstinline)\*?(.).*?\1|\\(?:texttt|enquote|mintinline\s*\{[^}]*\})\s*\{(?:[^{}]|\{[^{}]*\})*\}")
_TEX_APPENDIX = re.compile(r"\\appendix\b|\\begin\s*\{(?:appendix|appendices)\}|\\(?:section|chapter)\*?\s*\{\s*(?:Appendix|Appendices)\b")

# An appendix heading in extracted text: "Appendix", "Appendix A: ...", "A EXPERIMENTAL DETAILS", "A.1 Setup".
_TEXT_APPENDIX = re.compile(
    r"^[ \t#*]*(?:(?:Appendix|Appendices)\b[^\n.]{0,60}|(?-i:[A-Z](?:\.\d+)?\.?[ \t]+[A-Z][A-Z0-9 \t&:,()-]{3,60})|(?-i:[A-Z]\.\d+[ \t]+[A-Z][^\n.]{2,80}))[ \t*]*$",
    re.MULTILINE | re.IGNORECASE,
)


def _files(venue: Venue, values: dict[str, str]) -> list[tuple[str, Path]]:
    from .requirements_check import _existing, _fields_for, _paths

    out: list[tuple[str, Path]] = []
    for role in ("manuscript", "title_page", "cover_letter"):
        for field in _fields_for(venue, role):
            for path in _existing(_paths(field, values)):
                if path.suffix.lower() in (".pdf", ".docx", ".tex", ".zip", ".md", ".markdown", ".txt") and (field.id, path) not in out:
                    out.append((field.id, path))
    return out


def tex_scan_text(source: str) -> str:
    """The prose of a (comment-stripped) LaTeX source this check reads: no quoted, boxed, or code content, no appendix."""
    from .manuscript import _tex_to_text

    appendix = _TEX_APPENDIX.search(source)
    if appendix:
        source = source[: appendix.start()] + "\n\\end{document}\n"
    previous = None
    while previous != source:  # nested environments
        previous, source = source, _TEX_ENV.sub("\n\n", source)
    source = _TEX_INLINE.sub(" ", source)
    return _tex_to_text(source)


def plain_scan_text(text: str) -> str:
    """The text of a PDF/Word/Markdown file this check reads: no reference list, no appendix."""
    from .manuscript import reference_list_span

    span = reference_list_span(text)
    if span is not None:
        text = text[: span[0]] + "\n" + text[span[1] :]
    appendix = _TEXT_APPENDIX.search(text)
    return text[: appendix.start()] if appendix else text


def _excerpt(text: str, start: int, end: int) -> str:
    a = max(text.rfind("\n", 0, start) + 1, start - 50)
    right = text.find("\n", end)
    b = min(right if right != -1 else len(text), end + 50)
    return " ".join(text[a:b].split())


# Text extracted from a figure in a PDF comes out as a column of short lines (the labels and
# boxes of a diagram); a figure showing a model's replies is not residue.
_SHORT_LINE = 35
_NEIGHBOR_LINES = 3


def _in_figure_text(text: str, start: int) -> bool:
    """Whether the lines around ``start`` are mostly short, as a figure's text is when extracted from a PDF."""
    before = [ln for ln in text[:start].split("\n")[-_NEIGHBOR_LINES - 1 :] if ln.strip()]
    after = [ln for ln in text[start:].split("\n")[1 : _NEIGHBOR_LINES + 1] if ln.strip()]
    around = before + after
    if len(around) < 4:
        return False
    return sum(len(ln.strip()) < _SHORT_LINE for ln in around) / len(around) >= 0.75


def scan_text(text: str) -> list[tuple[str, str]]:
    """``(what, excerpt)`` for each chatbot phrase in ``text``, quoted text and examples left out."""
    text = _QUOTED.sub(lambda m: " " * len(m.group(0)), text)
    hits: list[tuple[str, str]] = []
    for what, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            if _EXAMPLE_LEAD.search(text[max(0, m.start() - 60) : m.start()]) or _in_figure_text(text, m.start()):
                continue
            hits.append((what, _excerpt(text, m.start(), m.end())))
    return hits


def _file_text(path: Path) -> str | None:
    from . import document
    from .manuscript import manuscript_text

    if path.suffix.lower() in (".tex", ".zip"):
        source = document.get(path).tex_source
        return None if source is None else tex_scan_text(source)
    text = manuscript_text(path)
    return None if text is None else plain_scan_text(text)


_ADVICE = "Delete it if it was pasted from a chatbot's reply; if an AI tool helped write the text, disclose that where the venue's guidelines ask."
_MAX_PER_SOURCE = 3


def _issues(field: str, where: str, hits: list[tuple[str, str]]) -> list[Issue]:
    issues = [Issue(LIKELY_ERROR, field, f"{where}: looks like {what} left in the text: \"{excerpt}\". {_ADVICE}") for what, excerpt in hits[:_MAX_PER_SOURCE]]
    if len(hits) > _MAX_PER_SOURCE:
        issues.append(Issue(LIKELY_ERROR, field, f"{where}: {len(hits) - _MAX_PER_SOURCE} more passage(s) like these"))
    return issues


def check(venue: Venue, values: dict[str, str]) -> Outcome:
    """Look for a chatbot's own words in the manuscript, title page, cover letter, and the .sub's text fields."""
    files = _files(venue, values)
    fields = [f for f in venue.fields if f.type in ("text", "textarea") and values.get(f.id, "").strip()]
    if not files and not fields:
        raise NotApplicable("no manuscript, title page, cover letter, or text field to read")
    issues: list[Issue] = []
    unread: list[str] = []
    for field_id, path in files:
        text = _file_text(path)
        if text is None:
            unread.append(path.name)
            continue
        issues.extend(_issues(field_id, path.name, scan_text(text)))
    for f in fields:
        issues.extend(_issues(f.id, f.label or f.id, scan_text(values[f.id])))
    read = len(files) - len(unread)
    detail = f"read {read} file(s) and {len(fields)} text field(s)"
    if unread:
        detail += f"; could not read the text of {', '.join(unread)}"
    return Outcome(issues, detail)
