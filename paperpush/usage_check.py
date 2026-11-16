"""Usage and typography in the manuscript's prose: commas, dashes, number formats, agreement.

Like :mod:`paperpush.style_check`, each check looks for slips a pattern can
find with few false alarms, and reports a mistake as a WARNING and a matter of
consistency or house style as a SUGGESTION (``--strict-style`` reports those
as warnings). The prose is read by :func:`paperpush.prose_check.read`, so math,
code, citations, URLs, and the reference list are already left out.

* ``prose.serial_comma`` -- lists of three or more short items written both
  with the serial (Oxford) comma ("A, B, and C") and without it ("A, B and
  C"). When the venue's ``manuscript.serial_comma`` says which, lists written
  the other way are warnings.
* ``prose.dashes`` -- a hyphen in a number range ("10-20" for "10–20"), a
  spaced hyphen or a double hyphen used as a dash ("the cells - which" for an
  em dash), dashes set both spaced and closed up ("a—b" and "a — b"), and,
  in LaTeX source, straight double quotes ("word"), which LaTeX prints as two
  closing quotes (write ``word'').
* ``prose.number_format`` -- spacing around ``=``, ``<``, and ``>`` used two
  ways ("n = 12" and "n=12") or on one side only ("n =12"); P values with and
  without a leading zero ("P < 0.05" and "P < .05") or with "P" and "p" both
  used; and units and percent signs set both with and without a space ("5 mg"
  and "5mg"). Spacing is not compared in a PDF, whose extracted text loses
  thin spaces.
* ``prose.usage`` -- agreement slips a pattern can see ("these data is",
  "a criteria", "the results shows", "there is several", "each sample were"),
  "data" used as both singular and plural, contractions ("don't", "it's"),
  and sentences of more than 50 words.
* ``prose.spelling_variant`` -- when the guidelines name no English variant
  (otherwise ``layout.language`` checks it): American and British spellings
  mixed, or spelling that is the opposite of where the corresponding author
  is (British spelling from a US affiliation, or the other way round).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Optional

from . import document
from .database import Venue
from .validate import SUGGESTION, WARNING, Issue, NotApplicable, Outcome

logger = logging.getLogger(__name__)

MAX_EXAMPLES = 5
# Sentences longer than this many words are reported.
LONG_SENTENCE_WORDS = 50


def _snip(p: str, start: int, end: int) -> str:
    from .prose_check import _mark, _snippet

    return _snippet(p, start, end, _mark)


def _list(hits: list[str]) -> str:
    shown = "; ".join(hits[:MAX_EXAMPLES])
    return shown + (f" (and {len(hits) - MAX_EXAMPLES} more)" if len(hits) > MAX_EXAMPLES else "")


def _two_ways(forms: Counter, examples: dict[str, list[str]], what: str) -> str | None:
    """``what`` written two (or more) ways, quoting the less-used forms; None when written one way."""
    if len([f for f in forms if forms[f]]) < 2:
        return None
    (major, n), *rest = forms.most_common()
    others = "; ".join(f"{form} {count}x (e.g. {examples[form][0]})" for form, count in rest if count)
    return f"{what} written two ways: {major} {n}x, but {others}; pick one"


def _rules(venue: Venue, values: dict[str, str]):
    from .requirements import get_requirements, resolve

    base = get_requirements(venue.slug)
    return None if base is None else resolve(base, values).manuscript


def _files(venue: Venue, values: dict[str, str]):
    """``(field, text, body paragraphs, kind)`` per manuscript file; kind is 'pdf', 'tex', or 'text'."""
    from .prose_check import PDF_CHECKS, _manuscripts
    from .style_check import _CAPTION_START, _body_paragraphs

    for fld, text in _manuscripts(venue, values):
        if text.why:
            yield fld, text, None, ""
            continue
        doc = document.get(text.path)
        kind = "pdf" if text.checks == PDF_CHECKS else "tex" if doc.is_tex else "text"
        body = [p for p in _body_paragraphs(text, prose_only=True) if not _CAPTION_START.match(p)]
        yield fld, text, body, kind


def _run(venue: Venue, values: dict[str, str], fn) -> Outcome:
    """Run ``fn(body, kind, path) -> (issues as (level, message), detail)`` on each manuscript file."""
    issues: list[Issue] = []
    details: list[str] = []
    files = list(_files(venue, values))
    for fld, text, body, kind in files:
        if body is None:
            issues.append(Issue(WARNING, fld.id, text.why, unchecked=True))
            continue
        found, detail = fn(body, kind, text.path)
        issues += [Issue(level, fld.id, f"{text.path.name}: {message}") for level, message in found]
        if detail and not found:
            details.append(detail if len(files) == 1 else f"{text.path.name}: {detail}")
    return Outcome(issues, "; ".join(details))


# --- serial comma ---------------------------------------------------------------------------

# Words that make a stretch a clause rather than a list item.
_CLAUSE_WORDS = frozenset(
    """we i you he she it they there this these those that which who whom whose is are was were be been being am has have had
    do does did not can could will would shall should may might must when while if although because since where whereas then
    than so but yet however thus therefore and or nor each every such including especially particularly either neither
    whether how what why""".split()
)
# Words that open an introductory phrase rather than the first item of a list ("In 2020, ...", "However, ...").
_INTRO_WORDS = frozenset(
    """in on at by for to from with without after before during within across under over since as using unlike like despite
    upon through among between here next finally first second third then thus therefore however moreover furthermore
    additionally notably importantly briefly together overall specifically consequently similarly instead meanwhile also
    when while if although because though once whereas indeed nevertheless nonetheless hence accordingly alternatively
    otherwise still yet so of compared consistent given based taken collectively altogether following prior""".split()
)
_SUBJECT_OR_VERB = frozenset("we i you he she it they there is are was were has have had".split())
_NOT_ADVERBS = frozenset("italy july family sicily emily lily assembly supply apply reply anomaly monopoly".split())


def _intro(words: list[str]) -> bool:
    """Whether a sentence's first comma-separated stretch is an introductory phrase ("In 2020", "Briefly", "Compared with X")."""
    w = words[0].lower()
    if len(words) == 1 and w.endswith("ly") and w not in _NOT_ADVERBS:
        return True
    # "Then we kept cats, dogs and mice" opens with a clause, whose last word starts the list.
    return w in _INTRO_WORDS and not any(x.lower() in _SUBJECT_OR_VERB for x in words)


# A word of a list item: no dash ("attestation---", "a\u2014b") and no trailing hyphen ("Lang-" split across lines).
_ITEM_WORD = re.compile(r"^(?!.*(?:--|-$))[\w\u2019'\-/.%\u00b0]+$")
_ITEM_MAX_WORDS = 4


def _item(words: list[str]) -> bool:
    """Whether ``words`` reads as a short list item: a few words, none of them a clause word."""
    return 0 < len(words) <= _ITEM_MAX_WORDS and all(_ITEM_WORD.match(w) and w.lower() not in _CLAUSE_WORDS for w in words)


def _middle_item(segment: str) -> bool:
    """Whether a segment between two commas is a list item, not an aside ("trained on ImageNet", "in mice")."""
    words = segment.split()
    if not _item(words):
        return False
    w = words[0]
    if len(words) > 1 and words[-1].islower() and words[-1].endswith("ed"):
        return False  # "the system created": a subject and its verb
    return not (len(words) > 1 and w.islower() and (w.endswith(("ed", "ing")) or w in _INTRO_WORDS))


def _trim(text: str, end: int) -> int:
    """``end`` moved back over closing punctuation, so a marked list ends at its last word."""
    while end > 0 and text[end - 1] in ".!?\"')\u201d":
        end -= 1
    return end


def serial_lists(paragraph: str) -> list[tuple[bool, str]]:
    """``(serial, snippet)`` for each list of three or more short items in ``paragraph``."""
    from .prose_check import _is_modifier, _sentences

    found: list[tuple[bool, str]] = []
    for sentence in _sentences(paragraph):
        offset = paragraph.find(sentence)
        # Comma-separated segments with their start offsets.
        parts = [(m.start(), m.group(0)) for m in re.finditer(r"[^,;:()]+", sentence)]
        # ", " between segments (each segment keeps its leading space).
        seps = [sentence[a + len(s) : b] + (" " if t.startswith(" ") else "") for (a, s), (b, t) in zip(parts, parts[1:])]
        i = 0
        while i < len(parts) - 1:
            if seps[i] != ", " or (i == 0 and _intro(parts[0][1].split() or ["x"])):
                i += 1  # "In 2020, ..." opens the sentence; a list may still follow
                continue
            # A run of segments joined by ", ": parts[i..j].
            j = i
            while j < len(parts) - 1 and seps[j] == ", ":
                j += 1
            run = [s.strip() for _, s in parts[i : j + 1]]
            first, last = run[0].split(), run[-1].split()
            ok_first = bool(first) and _item(first[-1:])
            ok_middle = all(_middle_item(seg) for seg in run[1:-1])
            # "PAPER2WEB, a benchmark dataset and ...": an item opening with "a"/"an" after a first item with no article is an appositive.
            second = run[1].split()
            if second and second[0].lower() in ("a", "an") and not any(w.lower() in ("a", "an", "the") for w in first[-_ITEM_MAX_WORDS:]):
                ok_first = False
            # The list is marked from the first item's last word.
            start = offset + parts[i][0] + parts[i][1].rstrip().rfind(" ") + 1
            if last and last[0].lower() in ("and", "or") and len(run) >= 3:
                # "A, B, and C": the middle items are short, and C does not open a clause.
                if ok_first and ok_middle and len(last) > 1 and last[1].lower() not in _CLAUSE_WORDS:
                    end = re.match(r"\s*\S+\s+(?:(?:a|an|the)\s+)?\S+", parts[j][1], re.I).end()
                    found.append((True, _snip(paragraph, start, _trim(paragraph, offset + parts[j][0] + end))))
            else:
                # "A, B and C": the last segment opens "B and C".
                m = re.match(r"\s*((?:\S+\s+){0,%d}?\S+)\s+(and|or)\s+((?:(?:a|an|the)\s+)?\S+)" % (_ITEM_MAX_WORDS - 1), parts[j][1], re.I)
                # "automatic, machine-to-machine publishing and rapid ...": with one comma, an adjective before a
                # longer phrase modifies it (coordinate adjectives) rather than being a list item.
                adjectives = bool(m) and j == i + 1 and len(m.group(1).split()) > 1 and _is_modifier(first[-1])
                if m and not adjectives and ok_first and ok_middle and _middle_item(m.group(1)) and m.group(3).lower() not in _CLAUSE_WORDS:
                    found.append((False, _snip(paragraph, start, _trim(paragraph, offset + parts[j][0] + m.end()))))
            i = j
    return found


def _serial_comma(body: list[str], kind: str, path, rule: Optional[bool]):
    serial = [s for p in body for ok, s in serial_lists(p) if ok]
    plain = [s for p in body for ok, s in serial_lists(p) if not ok]
    if rule is True and plain:
        return [(WARNING, f"{len(plain)} list(s) without the serial comma before the last item: {_list(plain)}; the venue's guidelines use the serial (Oxford) comma ('A, B, and C')")], ""
    if rule is False and serial:
        return [(WARNING, f"{len(serial)} list(s) with a serial comma before the last item: {_list(serial)}; the venue's guidelines leave it out ('A, B and C')")], ""
    if serial and plain:
        forms = Counter({"with the serial comma ('A, B, and C')": len(serial), "without it ('A, B and C')": len(plain)})
        examples = {"with the serial comma ('A, B, and C')": serial, "without it ('A, B and C')": plain}
        return [(SUGGESTION, _two_ways(forms, examples, "Lists"))], ""
    n = len(serial) + len(plain)
    if not n:
        return [], "no lists of three or more items found"
    return [], f"{n} list(s), all {'with' if serial else 'without'} the serial comma"


def _serial_comma_check(venue: Venue, values: dict[str, str]) -> Outcome:
    rules = _rules(venue, values)
    rule = getattr(rules, "serial_comma", None) if rules is not None else None
    return _run(venue, values, lambda body, kind, path: _serial_comma(body, kind, path, rule))


# --- dashes and quotes ----------------------------------------------------------------------

# "10-20", "2019-2020", "3.5-4.0": not dates (2020-01-31), IDs ("GSE12-34"), coordinates ("chr1:10-20"), or minus signs.
_HYPHEN_RANGE = re.compile(r"(?<![\w.\-/:\u2212])(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)(?![\w\-/:]|\.\d)")
_SPACED_HYPHEN = re.compile(r"(?<=[A-Za-z,)]) - (?=[A-Za-z(])")
_DOUBLE_HYPHEN = re.compile(r"(?<=\w)\s?--\s?(?=\w)")
_EM_CLOSED = re.compile(r"(?<=\w)\u2014(?=\w)")
_EM_SPACED = re.compile(r"(?<=\w) \u2014 (?=\w)")
_EN_SPACED = re.compile(r"(?<=[A-Za-z,)]) \u2013 (?=[A-Za-z(])")
_TEX_STRAIGHT_QUOTE = re.compile(r"(?<![\w\"\\])\"(?=[A-Za-z])")


def _dashes(body: list[str], kind: str, path):
    tex = kind == "tex"
    if tex:
        # LaTeX writes the en dash "--" and the em dash "---".
        body = [p.replace("---", "\u2014").replace("--", "\u2013") for p in body]
    found: list[tuple[str, str]] = []
    ranges = [_snip(p, m.start(), m.end()) for p in body for m in _HYPHEN_RANGE.finditer(p) if not _is_date_or_id(p, m)]
    if ranges:
        en = "--" if tex else "\u2013"
        found.append((SUGGESTION, f"{len(ranges)} number range(s) with a hyphen: {_list(ranges)}; use an en dash ({en}) for a range ('10\u201320')"))
    spaced = [_snip(p, m.start(), m.end()) for p in body for m in _SPACED_HYPHEN.finditer(p)]
    if not tex:
        spaced += [_snip(p, m.start(), m.end()) for p in body for m in _DOUBLE_HYPHEN.finditer(p)]
    if spaced:
        em = "---" if tex else "\u2014"
        found.append((SUGGESTION, f"{len(spaced)} hyphen(s) used as a dash: {_list(spaced)}; a hyphen joins words, so set a break in the sentence with an em dash ({em})"))
    forms: Counter = Counter()
    examples: dict[str, list[str]] = {}
    for name, pattern in (("closed-up em dashes ('a\u2014b')", _EM_CLOSED), ("spaced em dashes ('a \u2014 b')", _EM_SPACED), ("spaced en dashes ('a \u2013 b')", _EN_SPACED)):
        # CRediT roles are named with a spaced en dash ("Writing \u2013 original draft").
        hits = [_snip(p, m.start(), m.end()) for p in body for m in pattern.finditer(p) if not re.search(r"writing$", p[max(0, m.start() - 8) : m.start()], re.I)]
        if hits:
            forms[name] = len(hits)
            examples[name] = hits
    mixed = _two_ways(forms, examples, "Dashes that break a sentence")
    if mixed:
        found.append((SUGGESTION, mixed))
    if tex:
        quotes = [_snip(p, m.start(), m.end()) for p in body for m in _TEX_STRAIGHT_QUOTE.finditer(p)]
        if quotes:
            found.append((WARNING, f"{len(quotes)} straight double quote(s) opening a quotation: {_list(quotes)}; LaTeX prints \" as a closing quote, so write ``word'' (or \\enquote{{word}})"))
    return found, "hyphens, en dashes, and em dashes used as expected"


_STAMP = re.compile(r"\u00a9|\(c\)\s*\d{4}|\bProceedings\s+of\b|\bCopyright\b", re.I)


def _is_date_or_id(p: str, m: re.Match) -> bool:
    a, b = m.group(1), m.group(2)
    if len(a) == 4 and len(b) == 2 and "." not in a + b and re.match(r"-\d{2}\b", p[m.end() :]):
        return True  # 2020-01-31
    # A publisher's stamp ("Proceedings of ..., pages 626-635, July 2-7, 2026 ©2026 ..."), not the author's text.
    if _STAMP.search(p[max(0, m.start() - 100) : m.end() + 100]):
        return True
    before = p[max(0, m.start() - 12) : m.start()].lower()
    return bool(re.search(r"(?:accession|id|no\.|number|isbn|issn|doi|tel\.?|phone|grant|award)\s*:?\s*$", before))


def _dashes_check(venue: Venue, values: dict[str, str]) -> Outcome:
    return _run(venue, values, _dashes)


# --- number formats -------------------------------------------------------------------------

# "n = 12", "P<0.05", "r =.4": a symbol (one to three letters) compared with a number.
_COMPARISON = re.compile(r"(?<![\w=<>!])([A-Za-z]{1,3}|R\u00b2|r\u00b2)(\s*)([=<>\u2264\u2265])(\s*)(?=[-\u2212]?\.?\d)")
# A P value: "P < 0.05", "p=.001".
_P_VALUE = re.compile(r"(?<![\w-])([Pp])(?:[- ]?value)?\s*([=<>\u2264\u2265])\s*(0?)\.(\d+)")
# Units that take a space after the number (SI), with no single letters (which could be anything).
_UNIT = r"(?:[kmµμnp]?g|[mµμn][lL]|[kcmµμn]m|[mµμnp]M|k?Da|[kMG]b|bp|nt|[kMG]?Hz|ms|min|[KMGT]B|\u00b0C|rpm|mmHg|kcal|kJ|mV|mA|kV|mW|ppm)"
_NUMBER_UNIT = re.compile(rf"(?<![\w.,\-])(\d+(?:\.\d+)?)( ?)({_UNIT})(?![\w/\u00b2\u00b3])")
_PERCENT = re.compile(r"(?<![\w.,\-])(\d+(?:\.\d+)?)( ?)%")


def _number_format(body: list[str], kind: str, path):
    found: list[tuple[str, str]] = []
    spacing = kind != "pdf"
    if spacing:
        forms: Counter = Counter()
        examples: dict[str, list[str]] = {}
        lopsided: list[str] = []
        for p in body:
            for m in _COMPARISON.finditer(p):
                before, after = m.group(2), m.group(4)
                snip = _snip(p, m.start(), m.end() + 1)
                if bool(before) != bool(after):
                    lopsided.append(snip)
                    continue
                key = "spaced ('n = 12')" if before else "closed up ('n=12')"
                forms[key] += 1
                examples.setdefault(key, []).append(snip)
        if lopsided:
            found.append((SUGGESTION, f"{len(lopsided)} '=', '<', or '>' spaced on one side only: {_list(lopsided)}; space both sides or neither"))
        mixed = _two_ways(forms, examples, "'=', '<', and '>'")
        if mixed:
            found.append((SUGGESTION, mixed))
        for what, pattern in (("Units after a number", _NUMBER_UNIT), ("Percent signs", _PERCENT)):
            forms, examples = Counter(), {}
            for p in body:
                for m in pattern.finditer(p):
                    key = "with a space ('5 mg')" if m.group(2) else "closed up ('5mg')"
                    if pattern is _PERCENT:
                        key = key.replace("mg", "%")
                    forms[key] += 1
                    examples.setdefault(key, []).append(_snip(p, m.start(), m.end()))
            mixed = _two_ways(forms, examples, what)
            if mixed:
                found.append((SUGGESTION, mixed))
    zero: Counter = Counter()
    zero_ex: dict[str, list[str]] = {}
    case: Counter = Counter()
    case_ex: dict[str, list[str]] = {}
    for p in body:
        for m in _P_VALUE.finditer(p):
            snip = _snip(p, m.start(), m.end())
            key = "with a leading zero ('P < 0.05')" if m.group(3) else "without one ('P < .05')"
            zero[key] += 1
            zero_ex.setdefault(key, []).append(snip)
            key = f"'{m.group(1)}'"
            case[key] += 1
            case_ex.setdefault(key, []).append(snip)
    for what, forms, examples in (("P values", zero, zero_ex), ("The P of P values", case, case_ex)):
        mixed = _two_ways(forms, examples, what)
        if mixed:
            found.append((SUGGESTION, mixed))
    checked = ["P values"] + (["spacing around =, <, >", "units", "percent signs"] if spacing else [])
    return found, "consistent (" + ", ".join(checked) + ")" + ("" if spacing else "; spacing not compared in a PDF")


def _number_format_check(venue: Venue, values: dict[str, str]) -> Outcome:
    return _run(venue, values, _number_format)


# --- usage ----------------------------------------------------------------------------------

_APOS = "['\u2019]"
_CONTRACTION = re.compile(
    rf"\b(?:(?:do|does|did|is|are|was|were|has|have|had|could|would|should|must|need)n{_APOS}t|can{_APOS}t|won{_APOS}t|shan{_APOS}t"
    rf"|(?:it|that|there|here|what|who|let){_APOS}s|(?:we|they|you){_APOS}(?:re|ve|ll|d)|i{_APOS}(?:m|ve|ll|d)|(?:it|that|there){_APOS}(?:ll|d))\b",
    re.IGNORECASE,
)
_PLURALS = {"criteria": "criterion", "phenomena": "phenomenon", "bacteria": "bacterium", "strata": "stratum", "spectra": "spectrum", "hypotheses": "hypothesis", "analyses": "analysis", "matrices": "matrix", "vertices": "vertex", "loci": "locus"}  # fmt: skip
_SINGULARS = {v: k for k, v in _PLURALS.items()} | {"datum": "data"}
_PLURAL_RE = "|".join(_PLURALS)
_SINGULAR_RE = "|".join(_SINGULARS)
# (pattern, fix, whether a preceding "of"/"in" phrase makes the noun an object: "the quality of these data is").
_AGREEMENT = [
    (re.compile(r"\b(these|those)\s+data\s+(is|was|has)\b", re.I), "'data' is plural here: use a plural verb", True),
    (re.compile(r"\b(this|that)\s+data\s+(are|were|have)\b", re.I), "a singular determiner with a plural verb", True),
    (re.compile(rf"\b(a|an|this|that|one|each|every|another)\s+({_PLURAL_RE})\b", re.I), "the singular is '{s}'", False),
    (re.compile(rf"\b({_PLURAL_RE})\s+(is|was|has|does)\b", re.I), "'{p}' is plural", True),
    (re.compile(rf"\b(these|those|many|several|both|various|multiple)\s+({_SINGULAR_RE})(?=\s*(?:[.,;:)]|$)|\s+(?:is|are|was|were|has|have|show|showed|suggest|of|in|for|that|which|and|or)\b)", re.I), "the plural is '{pl}'", False),
    (re.compile(r"\b(the|these|those|our|their)\s+(results|findings)\s+(is|was|has|shows|suggests|indicates|demonstrates|confirms|reveals)\b", re.I), "plural subject, singular verb", True),
    (re.compile(r"\bthere\s+(is|was|exists)\s+(many|several|multiple|numerous|various|few|two|three|four|five|both)\b", re.I), "write 'there are' or 'there were'", False),
    (re.compile(r"\b(each|every)\s+([a-z]+)\s+(are|were|have)\b"), "'{0} {1}' takes a singular verb", False),
    (re.compile(r"\bits['\u2019](?!\w)", re.I), "write 'its'", False),
]
_OBJECT_OF = re.compile(r"\b(?:of|in|on|for|from|with|between|among)\s+(?:\w+\s+)?$", re.I)
_DATA_SINGULAR = re.compile(r"\b(?:(this|that)\s+data\b|data\s+(?:is|was|has|shows|suggests|indicates|demonstrates|reveals|supports|confirms)\b)", re.I)
_DATA_PLURAL = re.compile(r"\b(?:(these|those)\s+data\b|data\s+(?:are|were|have|show|suggest|indicate|demonstrate|reveal|support|confirm)\b)", re.I)


def agreement(paragraph: str) -> list[str]:
    """Agreement slips a pattern can see, each as a snippet and the fix."""
    out = []
    taken: list[tuple[int, int]] = []
    for pattern, fix, after_of in _AGREEMENT:
        for m in pattern.finditer(paragraph):
            if any(m.start() < b and a < m.end() for a, b in taken):
                continue  # "a criteria was" is one slip, not two
            if after_of and _OBJECT_OF.search(paragraph[max(0, m.start() - 30) : m.start()]):
                continue  # "the number of criteria is", "one of the results is"
            words = [g for g in m.groups() if g]
            noun = next((w.lower() for w in words if w.lower() in _PLURALS or w.lower() in _SINGULARS), "")
            note = fix.format(*words, s=_PLURALS.get(noun, ""), p=noun, pl=_SINGULARS.get(noun, ""))
            taken.append(m.span())
            out.append(f"{_snip(paragraph, m.start(), m.end())} ({note})")
    return out


def long_sentences(paragraph: str, limit: int = LONG_SENTENCE_WORDS) -> list[str]:
    """Sentences of ``paragraph`` with more than ``limit`` words, each as its opening words and length."""
    from .prose_check import _sentences

    out = []
    for sentence in _sentences(paragraph):
        words = re.findall(r"[A-Za-z0-9][\w'\u2019\-]*", sentence)
        if len(words) > limit:
            out.append(f"'{' '.join(sentence.split()[:8])} ...' ({len(words)} words)")
    return out


def _usage(body: list[str], kind: str, path):
    found: list[tuple[str, str]] = []
    slips = [hit for p in body for hit in agreement(p)]
    if slips:
        found.append((WARNING, f"{len(slips)} agreement slip(s): {_list(slips)}"))
    singular = [_snip(p, m.start(), m.end()) for p in body for m in _DATA_SINGULAR.finditer(p)]
    plural = [_snip(p, m.start(), m.end()) for p in body for m in _DATA_PLURAL.finditer(p)]
    if singular and plural:
        forms = Counter({"plural ('data are')": len(plural), "singular ('data is')": len(singular)})
        mixed = _two_ways(forms, {"plural ('data are')": plural, "singular ('data is')": singular}, "'Data'")
        found.append((SUGGESTION, mixed.replace("pick one", "pick one (most journals treat 'data' as plural)")))
    contractions = Counter(m.group(0).lower().replace("\u2019", "'") for p in body for m in _CONTRACTION.finditer(p))
    if contractions:
        shown = ", ".join(f"'{c}'" + (f" {n}x" if n > 1 else "") for c, n in contractions.most_common())
        found.append((SUGGESTION, f"{sum(contractions.values())} contraction(s): {shown}; formal prose spells them out ('do not', 'it is')"))
    if kind != "pdf":
        long = [hit for p in body for hit in long_sentences(p)]
        if long:
            found.append((SUGGESTION, f"{len(long)} sentence(s) of more than {LONG_SENTENCE_WORDS} words: {_list(long)}; consider splitting them"))
    return found, "no agreement slips, contractions" + (", or very long sentences" if kind != "pdf" else " (sentence length not measured in a PDF)")


def _usage_check(venue: Venue, values: dict[str, str]) -> Outcome:
    return _run(venue, values, _usage)


# --- spelling variant when the guidelines name none ---------------------------------------------

_US_STATES = """Alabama Alaska Arizona Arkansas California Colorado Connecticut Delaware Florida Hawaii Idaho Illinois Indiana Iowa
Kansas Kentucky Louisiana Maine Maryland Massachusetts Michigan Minnesota Mississippi Missouri Montana Nebraska Nevada
New\\s+Hampshire New\\s+Jersey New\\s+Mexico New\\s+York North\\s+Carolina North\\s+Dakota Ohio Oklahoma Oregon Pennsylvania
Rhode\\s+Island South\\s+Carolina South\\s+Dakota Tennessee Texas Utah Vermont Virginia Washington West\\s+Virginia Wisconsin
Wyoming""".split()
_STATE_CODES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
_AMERICAN_PLACE = re.compile(
    r"\bUSA\b|\bU\.S\.(?:A\.)?|\bUnited\s+States\b|\b(?:" + "|".join(_US_STATES) + r")\b|\b(?:" + "|".join(_STATE_CODES) + r")\s+\d{5}(?:-\d{4})?\b"
)
_BRITISH_PLACE = re.compile(r"\bUK\b|\bU\.K\.|\bUnited\s+Kingdom\b|\bGreat\s+Britain\b|\bEngland\b|\bScotland\b|\bWales\b|\bNorthern\s+Ireland\b|\bIreland\b|\bAustralia\b|\bNew\s+Zealand\b|\bSouth\s+Africa\b")
_NOT_PLACE_COLUMNS = {"name", "email", "orcid", "corresponding", "first_name", "last_name", "middle_name", "prefix", "suffix", "openreview_id"}


def author_variant(venue: Venue, values: dict[str, str]) -> tuple[Optional[str], str]:
    """``(variant, who)``: 'American' or 'British' from the corresponding author's affiliation, else ``(None, "")``."""
    from .validate import parse_authors

    for fld in venue.fields:
        if fld.type != "authorlist" or "author" not in fld.id.lower() or not values.get(fld.id):
            continue
        authors = parse_authors(values[fld.id], fld.fields)
        if not authors:
            continue
        author = next((a for a in authors if a.get("corresponding") is True), authors[0])
        place = " ".join(str(v) for k, v in author.items() if k not in _NOT_PLACE_COLUMNS and isinstance(v, str))
        us, uk = bool(_AMERICAN_PLACE.search(place)), bool(_BRITISH_PLACE.search(place))
        who = "the corresponding author" if author.get("corresponding") is True else "the first author"
        if us != uk:
            return ("American" if us else "British"), who
        return None, ""
    return None, ""


def _spelling_variant_check(venue: Venue, values: dict[str, str]) -> Outcome:
    from .layout_check import spelling_counts
    from .manuscript import truncate_at_references
    from .requirements_check import manuscript_files

    files = manuscript_files(venue, values)
    if not files:
        raise NotApplicable("no manuscript file to read")
    expected, who = author_variant(venue, values)
    issues: list[Issue] = []
    details: list[str] = []
    sample = lambda c: ", ".join(w for w, _ in c.most_common(4))  # noqa: E731
    for fld, path in files:
        text = document.get(path).text
        if text is None:
            issues.append(Issue(WARNING, fld.id, f"could not read {path.name} as text; its spelling variant was not checked", unchecked=True))
            continue
        us, uk, _, ise = spelling_counts(truncate_at_references(text))
        british = uk + ise
        n_us, n_uk = sum(us.values()), sum(british.values())
        if us and british:
            issues.append(Issue(SUGGESTION, fld.id, f"{path.name} mixes American ({sample(us)}) and British ({sample(british)}) spellings; use one variant throughout"))
        elif expected == "American" and n_uk >= 2:
            issues.append(Issue(SUGGESTION, fld.id, f"{path.name} uses British spellings ({sample(british)}), but {who}'s affiliation is in the US; the guidelines name no variant, so British English may be kept; use one variant consistently"))
        elif expected == "British" and n_us >= 2:
            issues.append(Issue(SUGGESTION, fld.id, f"{path.name} uses American spellings ({sample(us)}), but {who}'s affiliation uses British English; the guidelines name no variant, so American English may be kept; use one variant consistently"))
        else:
            variant = "American" if n_us > n_uk else "British" if n_uk else "no variant-specific"
            note = f"; {who} is in a{'n' if expected == 'American' else ''} {expected} English region" if expected else ""
            details.append(f"{variant} spelling ({n_us} American, {n_uk} British forms){note}")
    return Outcome(issues, "; ".join(details))


# --- registration -------------------------------------------------------------------------------


def run_checks(checks, venue: Venue, values: dict[str, str]) -> None:
    from .prose_check import PROSE

    checks.run("prose.serial_comma", "Lists use the serial (Oxford) comma one way", PROSE, _serial_comma_check, venue, values)
    checks.run("prose.dashes", "Hyphens, en dashes, em dashes, and quotes used as expected", PROSE, _dashes_check, venue, values)
    checks.run("prose.number_format", "Statistics, units, and percent signs written one way", PROSE, _number_format_check, venue, values)
    checks.run("prose.usage", "Agreement, contractions, and very long sentences", PROSE, _usage_check, venue, values)
    title = "Spelling uses one English variant (US or UK)"
    rules = _rules(venue, values)
    language = ((rules.language if rules is not None else None) or "").strip()
    if language and language.lower() != "english":
        from .report import NOT_APPLICABLE

        checks.record("prose.spelling_variant", title, PROSE, NOT_APPLICABLE, detail=f"the guidelines ask for {language}; checked by layout.language")
    else:
        checks.run("prose.spelling_variant", title, PROSE, _spelling_variant_check, venue, values)
