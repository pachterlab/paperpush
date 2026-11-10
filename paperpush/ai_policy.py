"""The venue's policy on generative AI: disclosure, where it goes, and authorship.

Venues record their AI policy in the ``statements`` block of
``manuscript_requirements.json``:

* ``required: [..., "ai_use"]`` -- a statement on AI use is required whether or
  not AI was used (its presence is checked by ``guidelines.statements``);
* ``ai_use_if_used`` -- a disclosure is required when AI was used;
* ``ai_use_locations`` -- where in the manuscript the disclosure may go;
* ``ai_use_cover_letter`` -- the cover letter must disclose it too;
* ``ai_copyediting_exempt`` -- language editing alone need not be disclosed;
* ``ai_authorship_prohibited`` -- AI tools may not be authors.

The check is deterministic. A sentence counts as an AI-use disclosure when it
names an AI tool (ChatGPT, Claude, "large language model", "generative AI", ...)
together with what it was used for (writing, editing, code, figures, ...) or a
denial ("no generative AI was used"). A tool named only as the subject of the
research ("GPT-4 reached 85% accuracy") is not a disclosure. Each disclosure is
placed in the section it sits in (Methods, Acknowledgements, a statement of its
own, a figure legend), so the check can tell whether it is where the venue
asks for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .database import Venue
from .requirements import ManuscriptRequirements, heading_aliases
from .validate import ERROR, WARNING, Issue, NotApplicable, Outcome

# AI tools and the generic ways of naming them. Proper names that are also
# ordinary words or people's names (Claude, Gemini, Mistral, Copilot) only count
# with a capital letter, and only beside a use (see _DISCLOSURE).
_TOOL = (
    r"(?:\b(?i:chat\s?gpt|gpt-?(?:3(?:\.5)?|4(?:o|\.\d)?|4o-mini|5)|openai|anthropic|deepseek|perplexity(?:\.ai)?|grammarly|quillbot|deepl|writefull|paperpal|trinka|midjourney|dall[-·]?e(?:\s?\d)?|stable\s+diffusion|scispace|elicit)\b"
    r"|\b(?:Claude|Gemini|Bard|Copilot|LLaMA|Llama(?:\s?\d)?|Mistral|Qwen|Grok)\b"
    r"|\bLLMs?\b"
    r"|\b(?i:large\s+language\s+models?|generative\s+(?:ai|artificial\s+intelligence)|gen-?ai|(?:ai|artificial\s+intelligence)[- ](?:assisted|based|powered|driven)\s+(?:tools?|technolog(?:y|ies)|writing|language|editing|software|assistan(?:ts?|ce))|(?:ai|artificial\s+intelligence)\s+(?:tools?|chat\s?bots?|assistants?|writing\s+(?:tools?|assistants?)|language\s+models?)|chat\s?bots?)\b)"
)
_TOOL_RE = re.compile(_TOOL)
# What the tool was used for.
_PURPOSE = re.compile(
    r"\b(?i:writ\w*|draft\w*|edit\w*|copy-?edit\w*|proofread\w*|polish\w*|rephras\w*|paraphras\w*|translat\w*|grammar|grammatical|spelling|readability|language|wording|phrasing|clarity|manuscript|text|prose|code|coding|programming|scripts?|figures?|images?|illustrations?|graphics?|summar\w*|preparation)\b"
)
_USE = re.compile(r"\b(?i:used?|using|use\s+of|utili[sz]\w+|employ\w*|assist\w*|help\w*|aid(?:ed)?|support\w*|generat\w*|produc\w*|creat\w*|improv\w*|refin\w*|check\w*|enhanc\w*)\b")
_NEGATION = re.compile(r"\b(?i:no|not|none|never|neither|nor)\b|n't\b|\b(?i:without)\s+(?:\w+\s+){0,4}?" + _TOOL)
# A contrast between two clauses, each of which may say something different about the tool.
_CLAUSE_BREAK = re.compile(r"\b(?i:but|however|yet|although|though|while|whereas|except)\b|;")
_ANAPHOR = re.compile(r"\b(?i:it|they|them|this\s+tool|these\s+tools)\b|^\s*(?i:was|were|only)\b")
# Signs that the sentence is about the authors' own use, not the tool as a research subject.
_AUTHOR_USE = re.compile(
    r"\b(?i:we|our|authors?|manuscript|paper|article|submission|preparation|writing)\b|\bI\b"
    r"|\b(?i:(?:was|were|been|is|are)\s+(?:\w+\s+){0,2}?(?:used|utili[sz]ed|employed|applied)|(?:assisted|aided|supported|helped|edited|generated|produced|created|improved|checked|proofread|polished|drafted|written)\s+(?:\w+\s+){0,2}?(?:by|with|using)|using|use\s+of)\b"
)
# Uses limited to the language: what an ai_copyediting_exempt venue lets go undeclared.
_COPYEDIT = re.compile(r"\b(?i:grammar|grammatical|spelling|readability|language|wording|phrasing|clarity|proofread\w*|copy-?edit\w*|polish\w*|typos?|style)\b")
_SUBSTANTIVE = re.compile(r"\b(?i:draft\w*|generat\w*|writ(?:e|ing|ten)\s+(?:the\s+)?(?:code|text|sections?|first)|code|coding|programming|scripts?|figures?|images?|illustrations?|graphics?|analy[sz]\w*|data|summar\w*|literature|translat\w*)\b")
_FIGURE_LEGEND = re.compile(r"^\s*(?:Supplementary\s+|Extended\s+Data\s+)?(?:Fig(?:ure)?|Table)\.?\s*S?\d+[a-z]?\s*[.:|]", re.IGNORECASE)
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")

LOCATION_LABELS = {
    "methods": "Methods",
    "acknowledgements": "Acknowledgements",
    "statement": "an AI-use statement of its own",
    "supplementary": "the supplementary material",
    "figure_captions": "the figure legends",
}


@dataclass(frozen=True)
class Disclosure:
    """One sentence of a file that discloses (or denies) the use of AI."""

    sentence: str
    location: str  # a LOCATION_LABELS key, or "other"
    heading: str  # the heading it sits under, as written ("" at the top of the file)
    negative: bool  # says AI was *not* used
    copyedit_only: bool  # the only use named is language editing
    file: str

    def where(self) -> str:
        if self.location in LOCATION_LABELS and self.location != "statement":
            return LOCATION_LABELS[self.location]
        if self.location == "statement":
            return f"the '{self.heading}' statement" if self.heading else LOCATION_LABELS["statement"]
        return f"the '{self.heading}' section" if self.heading else "the front matter"


def _short(text: str, limit: int = 110) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _heading_table() -> dict[str, str]:
    """Every known section/statement heading wording, mapped to its AI-disclosure location ("other" if none)."""
    table: dict[str, str] = {}
    for kind in ("sections", "statements"):
        for canonical, wordings in heading_aliases(kind).items():
            location = {"ai_use": "statement", "acknowledgements": "acknowledgements", "Acknowledgements": "acknowledgements", "Methods": "methods"}.get(canonical, "other")
            for wording in wordings:
                if table.get(wording, "other") == "other":
                    table[wording] = location
    return table


def _location_for(heading: str, table: dict[str, str]) -> str | None:
    """The AI-disclosure location a normalized heading stands for, or None if it is not a known heading."""
    if not heading:
        return None
    bare = re.sub(r"^\d+(?:\.\d+)*\W*", "", heading)
    if bare in table:
        return table[bare]
    # An AI-use statement under a heading of its own wording ("Statement on LLM usage").
    if _TOOL_RE.search(heading) and re.search(r"(?i)\b(?:use|usage|statement|declaration|disclosure)\b", heading):
        return "statement"
    return None


def _chunks(path: Path) -> list[tuple[str, str, str]] | None:
    """The file as ``(heading, location, text)`` chunks, one per known heading, in order.

    Only the headings paperpush knows (the ``$aliases`` wordings of every section
    and statement, and AI-use headings) divide the text, so a Methods
    subsection stays in the Methods and a short line of running text is never
    taken for a heading. LaTeX source is divided at its sectioning commands
    and bold run-in headings. Returns None when the file cannot be read as text.
    """
    from . import document, manuscript

    table = _heading_table()
    doc = document.get(path)
    out: list[tuple[str, str, str]] = []
    if doc.is_tex and doc.tex_source is not None:
        source = doc.tex_source
        pattern = re.compile(manuscript._TEX_SECTION.pattern + r"|\\(?:textbf|noindent\s*\\textbf)\s*\{([^{}]{3,60})\}")
        last, heading, location = 0, "", "other"
        for m in pattern.finditer(source):
            name = manuscript.normalize_heading(m.group(1) or m.group(2) or "")
            found = _location_for(name, table)
            if m.group(2) is not None and found is None:
                continue  # bold text that is not a heading
            out.append((heading, location, _tex_plain(source[last : m.start()])))
            if found is None:
                # A subsection the alias table does not know ("Cell culture") stays in its section.
                found = location if re.match(r"\\(?:subsection|subsubsection|paragraph)", m.group(0)) else "other"
            heading, location, last = name, found, m.end()
        out.append((heading, location, _tex_plain(source[last:])))
        return [c for c in out if c[2].strip()]
    text = doc.text
    if text is None or not text.strip():
        return None
    heading, location, buf = "", "other", []
    for line in text.splitlines():
        stripped = line.strip()
        first = stripped.split(".", 1)[0] if "." in stripped[:-1] else ""
        for candidate, rest in ((stripped, ""), (first, stripped.split(".", 1)[1] if first else "")):
            if not candidate or len(candidate.split()) > manuscript._HEADING_MAX_WORDS:
                continue
            name = manuscript.normalize_heading(candidate)
            found = _location_for(name, table)
            if found is not None:
                out.append((heading, location, "\n".join(buf)))
                heading, location, buf = name, found, ([rest] if rest.strip() else [])
                break
        else:
            buf.append(line)
    out.append((heading, location, "\n".join(buf)))
    return [c for c in out if c[2].strip()]


def _tex_plain(source: str) -> str:
    """LaTeX source reduced to prose, keeping sentence punctuation and paragraph breaks."""
    text = re.sub(r"\\(?:cite\w*|ref|eqref|label|url|href)\s*(?:\[[^\]]*\])?\s*\{[^}]*\}", " ", source)
    text = re.sub(r"\\(?:begin|end)\s*\{[^}]*\}", "\n\n", text)
    text = re.sub(r"\\[a-zA-Z@]+\*?", " ", text)
    text = re.sub(r"\\[^a-zA-Z]", " ", text)
    return re.sub(r"[{}$&#~^_]", " ", text)


def _sentences(text: str) -> list[str]:
    """The sentences of ``text``; a figure legend's first line starts a new one."""
    text = re.sub(r"-\n(?=[a-z])", "", text)  # PDF hyphenation at a line break
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        flat = " ".join(para.split())
        if flat:
            out.extend(s for s in _SENTENCE_BREAK.split(flat) if s.strip())
    return out


def _affirms_use(sentence: str) -> bool:
    """Whether some clause says the tool was used: "used ChatGPT to edit, but not to write" does."""
    for clause in _CLAUSE_BREAK.split(sentence):
        use = _USE.search(clause)
        if use is None or not (_TOOL_RE.search(clause) or _ANAPHOR.search(clause)):
            continue
        denial = _NEGATION.search(clause)
        if denial is None or denial.start() > use.start():
            return True
    return False


def classify(sentence: str) -> tuple[bool, bool, bool] | None:
    """``(is_disclosure, negative, copyedit_only)`` for one sentence, or None if it names no AI tool."""
    tool = _TOOL_RE.search(sentence)
    if not tool:
        return None
    if not _AUTHOR_USE.search(sentence):
        return (False, False, False)
    negative = bool(_NEGATION.search(sentence)) and bool(_USE.search(sentence) or _PURPOSE.search(sentence)) and not _affirms_use(sentence)
    disclosure = negative or bool(_USE.search(sentence) and _PURPOSE.search(sentence))
    if not disclosure:
        return (False, False, False)
    # The purpose words outside the tool's own name ("AI writing assistant").
    rest = sentence[: tool.start()] + " " + sentence[tool.end() :]
    copyedit = bool(_COPYEDIT.search(rest)) and not _SUBSTANTIVE.search(rest)
    return (True, negative, copyedit)


def disclosures(path: Path, *, supplementary: bool = False) -> list[Disclosure] | None:
    """Every AI-use disclosure in ``path``, with where it sits; None if the file cannot be read."""
    chunks = _chunks(path)
    if chunks is None:
        return None
    found: list[Disclosure] = []
    for heading, section, text in chunks:
        location = "supplementary" if supplementary else section
        for sentence in _sentences(text):
            verdict = classify(sentence)
            in_statement = location == "statement"
            if verdict is None and not in_statement:
                continue
            is_disclosure, negative, copyedit = verdict or (in_statement, bool(_NEGATION.search(sentence)), False)
            if not is_disclosure and not in_statement:
                continue
            where = "figure_captions" if not supplementary and _FIGURE_LEGEND.match(sentence) else location
            found.append(Disclosure(sentence=sentence, location=where, heading=heading, negative=negative, copyedit_only=copyedit, file=path.name))
    return found


class _InText:
    """Adapter so :data:`paperpush.requirements_check.STATEMENT_IN_TEXT` can find an AI-use statement in running text."""

    def search(self, text: str) -> bool:
        return any((verdict := classify(s)) is not None and verdict[0] for s in _sentences(text))


IN_TEXT = _InText()


# .sub fields that carry the author's own answer about AI use.
_SUB_AI_FIELD = re.compile(r"(?:^|_)(?:ai|llm|genai|generative)(?:_|$)", re.IGNORECASE)
_SUB_NO = re.compile(r"^\s*(?:no|none|n/?a|not applicable|false|0)\b", re.IGNORECASE)


def _sub_ai_answer(venue: Venue, values: dict[str, str]) -> tuple[str, str] | None:
    """``(field id, value)`` of a filled .sub field about AI use, or None."""
    for field in venue.fields:
        if field.type in ("file", "filelist"):
            continue
        if _SUB_AI_FIELD.search(field.id) and (values.get(field.id) or "").strip():
            return field.id, values[field.id].strip()
    return None


# What may accompany a tool's name in a "name" that is really the tool's: "ChatGPT (OpenAI)", "Google Gemini 1.5".
_TOOL_WORDS = {"ai", "assistant", "bard", "bot", "chat", "google", "large", "language", "meta", "microsoft", "model", "pro", "the", "version"}


def _ai_authors(venue: Venue, values: dict[str, str]) -> list[str]:
    from .validate import parse_authors

    names: list[str] = []
    for field in venue.fields:
        if field.type != "authorlist" or "author" not in field.id.lower() or not values.get(field.id):
            continue
        for author in parse_authors(values[field.id], field.fields):
            name = (author.get("name") or " ".join(p for p in (author.get("first_name", ""), author.get("last_name", "")) if p)).strip()
            if name and _TOOL_RE.search(name) and len(name.split()) <= 4 and not set(re.findall(r"[a-z]+", _TOOL_RE.sub(" ", name).lower())) - _TOOL_WORDS:
                names.append(name)
    return names


def _files(venue: Venue, values: dict[str, str], role: str) -> list[tuple[str, Path]]:
    from .requirements_check import _existing, _fields_for, _paths

    return [(field.id, path) for field in _fields_for(venue, role) for path in _existing(_paths(field, values))]


def applies(reqs: ManuscriptRequirements) -> bool:
    st = reqs.statements
    return bool(st.ai_use_if_used or "ai_use" in (st.required or []) or st.ai_authorship_prohibited or st.ai_use_cover_letter or st.ai_use_locations)


def check(venue: Venue, reqs: ManuscriptRequirements, values: dict[str, str]) -> Outcome:
    """Issues with the manuscript's AI-use disclosure under the venue's policy."""
    st = reqs.statements
    if not applies(reqs):
        raise NotApplicable("the guidelines set no rule on the use of generative AI")
    issues: list[Issue] = []
    notes: list[str] = []

    if st.ai_authorship_prohibited:
        for name in _ai_authors(venue, values):
            issues.append(Issue(ERROR, "", f"'{name}' is listed as an author; the guidelines do not allow AI tools as authors (disclose the use instead)"))

    manuscripts = _files(venue, values, "manuscript")
    if not manuscripts and not issues:
        raise NotApplicable("no manuscript file to read")
    found: list[Disclosure] = []
    unread: list[str] = []
    for _, path in manuscripts:
        got = disclosures(path)
        if got is None:
            unread.append(path.name)
        else:
            found.extend(got)
    if "supplementary" in (st.ai_use_locations or []):
        for _, path in _files(venue, values, "supplementary"):
            found.extend(disclosures(path, supplementary=True) or [])
    if unread and not found:
        from .validate import Unchecked

        raise Unchecked(f"could not read {', '.join(unread)} as text")

    positive = [d for d in found if not d.negative]
    substantive = [d for d in positive if not (st.ai_copyediting_exempt and d.copyedit_only)]
    sub_answer = _sub_ai_answer(venue, values)
    sub_used = sub_answer is not None and not _SUB_NO.match(sub_answer[1])

    allowed = list(st.ai_use_locations or [])
    expected = " or ".join(LOCATION_LABELS[loc] for loc in allowed) if allowed else "the manuscript"

    if positive and allowed and substantive:
        placed = [d for d in substantive if d.location in allowed]
        if not placed:
            first = substantive[0]
            issues.append(Issue(WARNING, "", f"{first.file}: the AI-use disclosure (\"{_short(first.sentence)}\") is in {first.where()}; the guidelines ask for it in {expected}"))
    if sub_used and not positive:
        what = "" if sub_answer is None else f" ({sub_answer[0]}: {_short(sub_answer[1], 60)})"
        if st.ai_use_if_used or "ai_use" in (st.required or []):
            issues.append(Issue(WARNING, sub_answer[0], f"the .sub says AI was used{what}, but no AI-use disclosure was found in the manuscript; the guidelines ask for one in {expected}"))

    if st.ai_use_cover_letter and (substantive or sub_used):
        letters = _files(venue, values, "cover_letter")
        if not letters:
            notes.append("no cover letter attached to check for the disclosure")
        else:
            field_id, letter = letters[0]
            got = disclosures(letter)
            if got is not None and not [d for d in got if not d.negative]:
                issues.append(Issue(WARNING, field_id, f"{letter.name} does not mention the use of AI; the guidelines ask for the AI-use disclosure in the cover letter as well as the manuscript"))

    if found:
        kinds = []
        for d in found[:3]:
            kinds.append(f"{'no-use statement' if d.negative else 'language-editing disclosure' if d.copyedit_only else 'disclosure'} in {d.where()}")
        notes.insert(0, "found " + "; ".join(kinds) + (f" (+{len(found) - 3} more)" if len(found) > 3 else ""))
    elif st.ai_use_if_used:
        notes.insert(0, f"the manuscript mentions no use of generative AI; if any was used{', beyond language editing' if st.ai_copyediting_exempt else ''}, disclose it in {expected}")
    if st.ai_authorship_prohibited and not any(i.is_error for i in issues):
        notes.append("no AI tool listed as an author")
    return Outcome(issues, "; ".join(notes))
