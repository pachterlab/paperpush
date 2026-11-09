"""Copy-editing conventions in the manuscript's prose: numbers, abbreviations, acronyms.

Like :mod:`paperpush.prose_check`, each check looks for slips a pattern can
find with few false alarms, and every finding is a WARNING. The prose is read
by :func:`paperpush.prose_check.read`, so math, code, citations, URLs, and the
reference list are already left out.

* ``prose.numbers`` -- a number of 10 or more spelled out ("twenty-five
  patients" for "25 patients"), except at the start of a sentence, where a
  number is spelled out; a sentence that starts with digits ("15 patients
  were ..."); and thousands grouped inconsistently: a number of 10,000 or more
  with no separator ("12000 cells"), four-digit numbers written both "1,500"
  and "2000", or commas in some numbers and spaces ("12 000") in others.
  When the venue's guidelines set ``manuscript.thousands_separator`` or
  ``manuscript.group_four_digit_numbers``, those rules are enforced instead of
  consistency. Years (1900-2099), labels ("Fig. 1000", "accession 123456"), and IDs
  ("GSE12345", "chr1:12345") are not counted.
* ``prose.conventions`` -- abbreviations written two ways, or wrongly: "i.e." and
  "e.g." with and without the comma, "ie"/"i.e"/"i. e."; "et. al", and "et al"
  with no full stop (unless the venue's ``manuscript.et_al_period`` is false,
  as in AMA and RSNA house style); "vs" and "vs."; "Fig." and "Figure" both used mid-sentence;
  and a word written both hyphenated and closed up ("pre-processing" and
  "preprocessing").
* ``prose.acronyms`` -- acronyms not introduced as "full term (ACR)" at first
  use: used before the definition, spelled out a second time, defined in the
  abstract but not again in the main text (or the other way round), used
  three or more times without ever being defined, or defined and then never
  used again (write the full term instead), or spelled out with two different
  meanings. Headings, captions, and the front matter before the abstract are
  not counted as first use.
* ``prose.heading_case`` -- headings of the same level capitalized differently
  ("Materials and methods" and "Data Availability"). Words that are capitalized
  in the running text too (names), acronyms, and one-word headings say nothing
  about the style, so they are not counted.

The checks that need a reader -- spelling and typos beyond codespell's list,
grammar, and tense -- are queued as one AI task, ``ai.prose``.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from .database import Venue
from .validate import WARNING, Issue, NotApplicable, Outcome, Unchecked

logger = logging.getLogger(__name__)

MAX_EXAMPLES = 5

# --- numbers ------------------------------------------------------------------------------

_TENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 10**6, "billion": 10**9}
_NUMBER_WORD = "|".join(sorted(list(_TENS) + list(_UNITS) + list(_SCALES), key=len, reverse=True))
# A run of number words: "twenty-five", "one hundred and twelve", "two thousand".
_NUMBER_PHRASE = re.compile(rf"(?<![\w-])(?:{_NUMBER_WORD})(?:(?:-|\s+|\s+and\s+)(?:{_NUMBER_WORD}))*(?![\w])", re.IGNORECASE)
# Words that, after the phrase, make it something other than a count: "twenty-first", "ten-fold" is fine as "10-fold".
_ORDINAL_AFTER = re.compile(r"-(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)\b", re.IGNORECASE)
# Abbreviations whose full stop does not end a sentence.
_NOT_SENTENCE_END = {"al", "fig", "figs", "eq", "eqs", "ref", "refs", "no", "nos", "vs", "approx", "resp", "cf", "ca", "sec", "ch", "vol", "pp", "dr", "st", "mr", "ms", "suppl", "supp", "e.g", "i.e", "et"}
# Digits opening a sentence: "... ended. 15 patients were ...".
_DIGIT_START = re.compile(r"(\S+?)([.!?])([\"')\]\u201d]*)\s+(\d[\d,.]*)\s+(?=[a-z])")


def words_to_number(phrase: str) -> int | None:
    """``"twenty-five"`` -> 25, ``"one hundred and twelve"`` -> 112; None if it does not parse."""
    total = current = 0
    for word in re.split(r"[-\s]+", phrase.lower()):
        if word in ("and", ""):
            continue
        if word in _UNITS:
            current += _UNITS[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word in _SCALES:
            current = (current or 1) * _SCALES[word]
            if _SCALES[word] >= 1000:
                total += current
                current = 0
        else:
            return None
    return total + current


def _digits(value: int) -> str:
    if value >= 10**9 and value % 10**9 == 0:
        return f"{value // 10**9:,} billion"
    if value >= 10**6 and value % 10**6 == 0:
        return f"{value // 10**6:,} million"
    return f"{value:,}"


def _phrases(paragraph: str):
    """``(start, end, phrase)`` for each spelled-out number; "and" joins only after a scale ("one hundred and five", not "two and ten")."""
    for m in _NUMBER_PHRASE.finditer(paragraph):
        text, start, pieces = m.group(0), 0, []
        for joint in re.finditer(r"\s+and\s+", text):
            if not re.search(r"(?:hundred|thousand|million|billion)$", text[: joint.start()], re.IGNORECASE):
                pieces.append((start, joint.start()))
                start = joint.end()
        pieces.append((start, len(text)))
        for lo, hi in pieces:
            yield m.start() + lo, m.start() + hi, text[lo:hi]


def _sentence_start(paragraph: str, start: int) -> bool:
    before = paragraph[:start].rstrip(" \t\n\"'(\u201c\u2018[")
    if not before:
        return True
    if before[-1] not in ".!?":
        return False
    word = re.search(r"([A-Za-z.]+)\.$", before)
    return not (word and word.group(1).lower().rstrip(".") in _NOT_SENTENCE_END)


def spelled_numbers(paragraph: str) -> list[str]:
    """Numbers of 10 or more written in words, outside the start of a sentence."""
    from .prose_check import _mark, _snippet

    found = []
    for start, end, phrase in _phrases(paragraph):
        if _ORDINAL_AFTER.match(paragraph, end) or phrase.lower() in _SCALES:
            continue  # "twenty-first"; a lone "hundred" or "million" ("a million cells") reads fine
        value = words_to_number(phrase)
        if value is None or value < 10 or _sentence_start(paragraph, start):
            continue
        found.append(f"{_snippet(paragraph, start, end, _mark)} (write {_digits(value)})")
    return found


def digit_sentence_starts(paragraph: str) -> list[str]:
    """Sentences inside a paragraph that open with digits ("... ended. 15 patients were ...")."""
    from .prose_check import _mark, _snippet

    found = []
    for m in _DIGIT_START.finditer(paragraph):
        token, stop, closers = m.group(1), m.group(2), m.group(3)
        if stop == "." and not closers and not token.endswith((")", "]")):
            word = token.lower().strip("(\"'[").rstrip(".")
            if word in _NOT_SENTENCE_END or (len(word) == 1 and word.isalpha()) or re.fullmatch(r"[\d.,]+", word):
                continue  # "Fig. 3", an initial, "Section 2. 3 ..." in a numbered list
        found.append(f"{_snippet(paragraph, m.start(4), m.end(4), _mark)} (spell it out or rephrase so the sentence does not open with digits)")
    return found


# A number in the prose, with its thousands grouped by commas ("12,000"), by
# spaces ("12 000", "12 000"), or not at all ("12000"); a decimal part may
# follow. Numbers glued to letters, digits, or ID punctuation ("GSE12345",
# "chr1:12345", "v2.1000", "10.1000/xyz") are not quantities and are skipped.
_THIN = "   "
_NUMBER = re.compile(
    rf"(?<![\w.,:/#\-–−{_THIN}])"
    rf"(?P<int>\d{{1,3}}(?:,\d{{3}})+|\d{{1,3}}(?:[ {_THIN}]\d{{3}})+|\d+)"
    rf"(?P<dec>\.\d+)?"
    rf"(?![\w/]|[.,:]\d|[ {_THIN}]\d)"
)
# A word right before the number that makes it a label, not a quantity: "Fig. 1000", "accession 123456".
_LABEL_BEFORE = re.compile(
    r"(?:\b(?:figs?|figures?|tables?|eqs?|equations?|refs?|sections?|pp?|pages?|no|nos|numbers?|ids?|accession|"
    r"zip|pmid|pmcid|doi|isbn|issn|codes?|versions?|v|chr|chromosome|positions?|pos|grant|award|protocol|lot|catalog|cat)\.?"
    r"|[#\[])\s*$",
    re.IGNORECASE,
)


@dataclass
class _Num:
    snippet: str
    digits: int  # digits before the decimal point
    style: str  # "comma", "space", or "none"


def _grouping_style(text: str) -> str:
    if "," in text:
        return "comma"
    return "space" if re.search(rf"[ {_THIN}]", text) else "none"


def numbers_in(paragraph: str) -> list[_Num]:
    """Numbers of 1000 or more written as quantities, with how their thousands are grouped.

    Year-like four-digit numbers (1900-2099) and numbers with a leading zero
    are left out; so is a space-grouped number of four digits ("Fig. 2 100
    cells" is not 2,100), unless the space is a thin or no-break one.
    """
    from .prose_check import _mark, _snippet

    found = []
    for m in _NUMBER.finditer(paragraph):
        whole = m.group("int")
        digits = re.sub(r"\D", "", whole)
        if len(digits) < 4 or digits.startswith("0"):
            continue
        style = _grouping_style(whole)
        if style == "space" and len(digits) < 5 and not re.search(f"[{_THIN}]", whole):
            continue
        if style == "none" and len(digits) == 4 and 1900 <= int(digits) <= 2099:
            continue  # a year
        if _LABEL_BEFORE.search(paragraph[max(0, m.start() - 15) : m.start()]):
            continue
        found.append(_Num(_snippet(paragraph, m.start(), m.end(), _mark), len(digits), style))
    return found


_STYLE_NAME = {"comma": "commas (1,000)", "space": "spaces (1 000)", "none": "no separator (1000)"}


def thousands_grouping(paragraphs: list[str], separator: Optional[str] = None, group_four: Optional[bool] = None) -> list[tuple[str, list[str]]]:
    """Problems with how the thousands are grouped, as ``(description, examples)``.

    ``separator`` (``"comma"`` or ``"space"``) and ``group_four`` are the
    venue's rules (``manuscript.thousands_separator`` and
    ``manuscript.group_four_digit_numbers``); where the guidelines say nothing
    the manuscript is only checked against itself:

    * a number of five or more digits with no separator ("12000 cells");
    * numbers grouped with the wrong separator, or (no rule) with commas in some
      and spaces in others, the less-used way quoted;
    * four-digit numbers grouped when the venue writes "1000", ungrouped when it
      writes "1,000", or (no rule) written both ways, the less-used way quoted
      (when tied, the ungrouped ones).
    """
    nums = [n for p in paragraphs for n in numbers_in(p)]
    grouped = Counter(n.style for n in nums if n.style != "none")
    use = separator or ("comma" if grouped["comma"] >= grouped["space"] else "space")
    rule = " (the venue's guidelines)" if separator else ""
    problems: list[tuple[str, list[str]]] = []

    long_plain = [n.snippet for n in nums if n.style == "none" and n.digits >= 5]
    if long_plain:
        problems.append((f"number(s) of 10,000 or more with no thousands separator; group the digits with {_STYLE_NAME[use]}{rule}", long_plain))

    if separator:
        other = "space" if separator == "comma" else "comma"
        wrong = [n.snippet for n in nums if n.style == other]
        if wrong:
            problems.append((f"number(s) grouped with {_STYLE_NAME[other]}; the venue's guidelines group thousands with {_STYLE_NAME[separator]}", wrong))
    elif grouped["comma"] and grouped["space"]:
        minority = "space" if grouped["space"] <= grouped["comma"] else "comma"
        other = "comma" if minority == "space" else "space"
        hits = [n.snippet for n in nums if n.style == minority]
        problems.append((f"number(s) grouped with {_STYLE_NAME[minority]}, while {grouped[other]} other(s) use {_STYLE_NAME[other]}; use one separator", hits))

    four = [n for n in nums if n.digits == 4]
    if group_four is True:
        hits = [n.snippet for n in four if n.style == "none"]
        if hits:
            problems.append((f"four-digit number(s) with no separator; the venue's guidelines write {'1,000' if use == 'comma' else '1 000'}", hits))
    elif group_four is False:
        hits = [n.snippet for n in four if n.style != "none"]
        if hits:
            problems.append(("four-digit number(s) grouped; the venue's guidelines write 1000, with a separator only from 10,000", hits))
    else:
        four_styles = Counter("none" if n.style == "none" else "grouped" for n in four)
        if four_styles["none"] and four_styles["grouped"]:
            minority = "grouped" if four_styles["grouped"] < four_styles["none"] else "none"
            hits = [n.snippet for n in four if (n.style == "none") == (minority == "none")]
            if minority == "none":
                desc = f"four-digit number(s) with no separator, while {four_styles['grouped']} other(s) are grouped (1,000); write them one way"
            else:
                desc = f"four-digit number(s) grouped, while {four_styles['none']} other(s) have no separator (1000); write them one way"
            problems.append((desc, hits))
    return problems


# --- conventions --------------------------------------------------------------------------

# Every spelling of "i.e."/"e.g.", well-formed or not, and what follows it.
_IE_EG = re.compile(r"(?<![\w.])(i\.?\s?e|e\.?\s?g)(\.?)(?![\w])(,?)")
_ET_AL_BAD = re.compile(r"\bet\.\s?al\b\.?|\bet al(?![.\w])|\bet\.al\b\.?")
# The same, for a venue that writes "et al" with no full stop: only the misplaced stops.
_ET_AL_BAD_NO_PERIOD = re.compile(r"\bet\.\s?al\b\.?|\bet\.al\b\.?")
_VS = re.compile(r"(?<![\w.])vs(\.?)(?![\w])")
# "Fig. 2" / "Figure 2" / "Figs. 2-3" / "Figures 2 and 3", not prefixed by Supplementary (that series has its own form).
_FIG_WORD = re.compile(r"(?<![\w.])(Figs?\.|Figures?)(?=\s*S?\d)")
_HYPHENATED = re.compile(r"(?<![\w-])([a-z]{2,})-([a-z]{2,})(?![\w-])")


@dataclass
class Conventions:
    """What :func:`conventions` found, one sentence per problem."""

    problems: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)


def _minority(forms: Counter, examples: dict[str, list[str]], what: str) -> str | None:
    """The less-used forms of ``what``, when ``forms`` has more than one."""
    if len(forms) < 2:
        return None
    (major, n), *rest = forms.most_common()
    others = "; ".join(f"'{form}' {count}x (e.g. {examples[form][0]})" for form, count in rest)
    return f"{what} written two ways: '{major}' {n}x, but {others}; pick one"


def conventions(paragraphs: list[str], *, hyphens: bool = True, et_al_period: Optional[bool] = None) -> Conventions:
    """Abbreviations and compounds written inconsistently or wrongly across ``paragraphs``.

    ``et_al_period`` is the venue's ``manuscript.et_al_period``: when false,
    "et al" without a full stop is the house style and is not reported.
    """
    from .prose_check import _mark, _snippet

    out = Conventions()
    ie_forms: Counter = Counter()
    ie_examples: dict[str, list[str]] = {}
    malformed: list[str] = []
    et_al_bad: list[str] = []
    vs_forms: Counter = Counter()
    vs_examples: dict[str, list[str]] = {}
    fig_forms: Counter = Counter()
    fig_examples: dict[str, list[str]] = {}
    hyphenated: dict[str, tuple[str, str]] = {}
    closed: Counter = Counter()

    for p in paragraphs:
        for m in _IE_EG.finditer(p):
            core, dot, comma = m.group(1), m.group(2), m.group(3)
            proper = core in ("i.e", "e.g") and dot == "."
            snippet = _snippet(p, m.start(), m.end(), _mark)
            if not proper:
                # A bare "ie"/"eg" counts only as a word of its own in running text: "(ie a model)".
                if core.isalpha() and (m.start() == 0 or p[m.start() - 1] not in " (\u201c" or p[m.end() : m.end() + 1] not in (" ", ")", ",")):
                    continue
                malformed.append(f"{snippet} (write '{'i.e.' if core[0] == 'i' else 'e.g.'}')")
                continue
            key = "with a comma" if comma else "without a comma"
            ie_forms[key] += 1
            ie_examples.setdefault(key, []).append(snippet)
        for m in (_ET_AL_BAD_NO_PERIOD if et_al_period is False else _ET_AL_BAD).finditer(p):
            et_al_bad.append(f"{_snippet(p, m.start(), m.end(), _mark)} (write '{'et al' if et_al_period is False else 'et al.'}')")
        for m in _VS.finditer(p):
            key = "vs." if m.group(1) else "vs"
            vs_forms[key] += 1
            vs_examples.setdefault(key, []).append(_snippet(p, m.start(), m.end(), _mark))
        for m in _FIG_WORD.finditer(p):
            if re.search(r"(?:Supplementary|Supplemental|Extended\s+Data|Appendix)\s+$", p[: m.start()]):
                continue
            if _sentence_start(p, m.start()):
                continue  # "Figure 2 shows" opens a sentence in either style
            key = "Fig." if m.group(1).startswith("Fig") and m.group(1).endswith(".") else "Figure"
            fig_forms[key] += 1
            fig_examples.setdefault(key, []).append(_snippet(p, m.start(), m.end(), _mark))
        if hyphens:
            for m in _HYPHENATED.finditer(p):
                hyphenated.setdefault(m.group(1) + m.group(2), (m.group(0), _snippet(p, m.start(), m.end(), _mark)))
            closed.update(w.lower() for w in re.findall(r"(?<![\w-])[A-Za-z]{5,}(?![\w-])", p))

    if malformed:
        out.problems.append(f"{len(malformed)} malformed 'i.e.'/'e.g.': " + "; ".join(malformed[:MAX_EXAMPLES]))
    ie = _minority(ie_forms, ie_examples, "'i.e.'/'e.g.'")
    if ie:
        out.problems.append(ie)
    if et_al_bad:
        out.problems.append(f"{len(et_al_bad)} malformed '{'et al' if et_al_period is False else 'et al.'}': " + "; ".join(et_al_bad[:MAX_EXAMPLES]))
    for problem in (_minority(vs_forms, vs_examples, "'vs.'"), _minority(fig_forms, fig_examples, "Figure citations mid-sentence")):
        if problem:
            out.problems.append(problem)
    both = [(form, snippet, closed[key]) for key, (form, snippet) in hyphenated.items() if closed[key]]
    if both:
        shown = "; ".join(f"'{form}' (e.g. {snippet}) and '{form.replace('-', '')}' {n}x" for form, snippet, n in both[:MAX_EXAMPLES])
        more = f" (and {len(both) - MAX_EXAMPLES} more)" if len(both) > MAX_EXAMPLES else ""
        out.problems.append(f"{len(both)} word(s) both hyphenated and closed up: {shown}{more}; pick one form")
    out.checked = ["i.e./e.g.", "et al" if et_al_period is False else "et al.", "vs."] + (["Fig./Figure"] if fig_forms else []) + (["hyphenation"] if hyphens else [])
    return out


# --- acronyms -----------------------------------------------------------------------------

# Two or more capitals (an optional plural s), no digits: "CNN", "CNNs", "scRNA" is left alone.
_ACRONYM = re.compile(r"(?<![\w\-/.\\@])([A-Z][A-Z&]*[A-Z])(s?)(?![\w\-/@])")
# "full term (ACR)", "(ACRs)", "(ACR; ref. 3)", "(ACR, ...)".
_DEFINITION = re.compile(r"\(\s*([A-Z][A-Z&]*[A-Z])s?\s*(?=[);,])")
# Known to every reader of a paper, or not an acronym at all.
_KNOWN = frozenset(
    """DNA RNA USA UK EU US UN PDF HTML URL CSV JSON XML GPU GPUs CPU RAM IEEE ACM ISBN ISSN DOI ORCID PI II III IV VI VII VIII IX XI XII
    AM PM UTC GMT OK ID AI IT TV GB MB KB TB GHz HD LED USB NA NAN SD SE SEM CI IQR ANOVA TODO FIXME NOTE COVID SARS HIV AIDS PCR MRI CT
    PET EEG ECG EKG FDA NIH NSF ERC WHO CDC MIT UCLA USC MD PHD PhD MS MSc BS BSc BA MA ET AL FIG EQ VS IE EG CF NB PS AND OR THE OF IN ON
    AT TO FOR BY WITH IS ARE NOT ABSTRACT INTRODUCTION METHODS RESULTS DISCUSSION CONCLUSION CONCLUSIONS REFERENCES ACKNOWLEDGMENTS
    ACKNOWLEDGEMENTS SUPPLEMENTARY APPENDIX BACKGROUND MATERIALS DATA CODE AVAILABILITY FUNDING TABLE FIGURE NY CA MA TX UK LLC INC
    MATLAB ATP GTP NADH NADPH""".split()
)
_ROMAN = re.compile(r"[IVXLC]+")
# Words an acronym's letters skip ("Department of Energy (DE)").
_ACRONYM_LINK_WORDS = frozenset("a an and the of for in on to with by".split())
# Uses of an acronym that is never defined before it is reported.
_UNDEFINED_MIN_USES = 3
# A paragraph this short with no final full stop is a heading.
_HEADING_MAX_WORDS = 12
_CAPTION_START = re.compile(r"^\s*(?:\(?(?:Supplementary|Extended\s+Data)\s+)?(?:Fig(?:ure)?s?\.?|Table)\s*S?\d+[a-zA-Z]?\s*(?:[.:|\u2014-]|\s{2,})", re.IGNORECASE)


def _is_heading(paragraph: str) -> bool:
    words = paragraph.split()
    return len(words) <= _HEADING_MAX_WORDS and not paragraph.rstrip().endswith((".", ":", ";", "?", "!"))


def _counts(paragraph: str) -> bool:
    """Whether ``paragraph`` is body text, where an acronym's first use should define it."""
    if _is_heading(paragraph) or _CAPTION_START.match(paragraph):
        return False
    letters = [c for c in paragraph if c.isalpha()]
    return not letters or sum(c.isupper() for c in letters) < 0.5 * len(letters)


@dataclass
class _Acro:
    first_use: tuple[int, int] | None = None  # (paragraph, offset)
    definitions: list[tuple[int, int]] = field(default_factory=list)
    uses: int = 0
    snippet: str = ""
    expansions: list[str] = field(default_factory=list)


def _expansion(before: str, acronym: str) -> str | None:
    """The words before "(ACR)" whose initials spell it, lower-cased; None if they do not ("electroencephalogram (EEG)")."""
    words = re.findall(r"[A-Za-z0-9']+", before)
    picked: list[str] = []
    i = len(words) - 1
    for letter in reversed(acronym.replace("&", "").lower()):
        while i >= 0 and words[i].lower() in _ACRONYM_LINK_WORDS and words[i][0].lower() != letter:
            i -= 1
        if i < 0 or words[i][0].lower() != letter:
            return None
        picked.append(words[i].lower())
        i -= 1
    picked[0] = picked[0][:-1] if len(picked[0]) > 3 and picked[0].endswith("s") else picked[0]  # the last word: singular
    return " ".join(reversed(picked))


def _scan_acronyms(paragraphs: list[str]) -> dict[str, _Acro]:
    from .prose_check import _mark, _snippet

    found: dict[str, _Acro] = {}
    for i, p in enumerate(paragraphs):
        if not _counts(p):
            continue
        defined_here = {(m.group(1), m.start(1)) for m in _DEFINITION.finditer(p)}
        for acr, pos in sorted(defined_here, key=lambda x: x[1]):
            entry = found.setdefault(acr, _Acro())
            entry.definitions.append((i, pos))
            expansion = _expansion(p[: p.rfind("(", 0, pos)], acr)
            if expansion and expansion not in entry.expansions:
                entry.expansions.append(expansion)
        for m in _ACRONYM.finditer(p):
            acr = m.group(1)
            if (acr, m.start(1)) in defined_here or acr in _KNOWN or _ROMAN.fullmatch(acr) or len(acr) > 8:
                continue
            a = found.setdefault(acr, _Acro())
            a.uses += 1
            if a.first_use is None:
                a.first_use = (i, m.start())
                a.snippet = _snippet(p, m.start(), m.end(), _mark)
    return found


@dataclass
class Acronyms:
    before_definition: list[str] = field(default_factory=list)
    redefined: list[str] = field(default_factory=list)
    undefined: list[str] = field(default_factory=list)
    rarely_used: list[str] = field(default_factory=list)
    conflicting: list[str] = field(default_factory=list)
    abstract_only: list[str] = field(default_factory=list)
    abstract_undefined: list[str] = field(default_factory=list)
    defined: int = 0


def acronyms(body: list[str], abstract: list[str] | None = None) -> Acronyms:
    """Acronyms in ``body`` (the main text) and ``abstract`` not introduced at first use."""
    out = Acronyms()
    main = _scan_acronyms(body)
    abs_found = _scan_acronyms(abstract or [])
    out.defined = sum(1 for a in main.values() if a.definitions)
    for acr, a in main.items():
        if a.definitions:
            if a.first_use is not None and a.first_use < a.definitions[0]:
                out.before_definition.append(f"'{acr}' first appears in {a.snippet}, before it is spelled out")
            meanings = list(dict.fromkeys(a.expansions + (abs_found[acr].expansions if acr in abs_found else [])))
            if len(meanings) > 1:
                out.conflicting.append(f"'{acr}' (" + ", ".join(f"'{m}'" for m in meanings) + ")")
            elif len(a.definitions) > 1:
                out.redefined.append(f"'{acr}' ({len(a.definitions)} times)")
            if a.uses == 0:  # only the definition: the acronym is never used
                out.rarely_used.append(f"'{acr}'")
        elif acr in abs_found and abs_found[acr].definitions and a.uses:
            out.abstract_only.append(f"'{acr}' (first used in {a.snippet})")
        elif a.uses >= _UNDEFINED_MIN_USES:
            out.undefined.append(f"'{acr}' ({a.uses}x, first in {a.snippet})")
    for acr, a in abs_found.items():
        if a.uses and not a.definitions and acr in main and main[acr].definitions:
            out.abstract_undefined.append(f"'{acr}'")
    return out


_ACRONYM_KINDS = (
    ("before_definition", "acronym(s) used before they are spelled out", "define each at its first use in the main text"),
    ("abstract_only", "acronym(s) spelled out only in the abstract", "the main text should define them again at first use"),
    ("abstract_undefined", "acronym(s) the abstract uses without spelling out (the main text does)", "an abstract is read on its own, so define them there too"),
    ("redefined", "acronym(s) spelled out more than once in the main text", "define once, then use the acronym"),
    ("undefined", "acronym(s) used but never spelled out", "define them at first use, unless every reader knows them (a gene, a unit, a name)"),
    ("rarely_used", "acronym(s) spelled out but never used again", "write the full term and drop the acronym"),
    ("conflicting", "acronym(s) spelled out with different meanings", "give each acronym one meaning"),
)


# --- splitting the manuscript ---------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def split_abstract(paragraphs: list[str], abstract_text: str | None) -> tuple[list[str], list[str]]:
    """``(abstract paragraphs, main-text paragraphs)``: the front matter before the abstract is dropped."""
    if not abstract_text:
        return [], paragraphs
    whole = _norm(abstract_text)
    inside = []
    for i, p in enumerate(paragraphs):
        start = re.sub(r"^(?:abstract|summary)\s+", "", _norm(p))[:80]
        if not _is_heading(p) and len(start) > 20 and start in whole:
            inside.append(i)
    if not inside:
        return [], paragraphs
    first, last = inside[0], inside[-1]
    return paragraphs[first : last + 1], paragraphs[last + 1 :]


def _tex_without_floats(source: str) -> str:
    """LaTeX source with its figure and table environments removed (captions are not first use)."""
    return re.sub(r"\\begin\s*\{\s*(figure|table|SCfigure|wrapfigure|sidewaysfigure|sidewaystable)\*?\s*\}.*?\\end\s*\{\s*\1\*?\s*\}", " ", source, flags=re.DOTALL)


def _pdf_without_captions(page: str) -> str:
    """A PDF page's text with figure and table legends (from a 'Figure 2.' line to the paragraph's end) removed."""
    lines = page.split("\n")
    keep, in_caption = [], False
    for line in lines:
        if _CAPTION_START.match(line):
            in_caption = True
        elif in_caption and (not line.strip() or (line.rstrip().endswith(".") and len(line) < 50)):
            in_caption = False
            continue
        if not in_caption:
            keep.append(line)
    return "\n".join(keep)


# --- the checks -------------------------------------------------------------------------------


def _report(hits: list[str], noun: str, field_id: str, name: str, advice: str = "") -> Issue:
    shown = "; ".join(hits[:MAX_EXAMPLES])
    more = f" (and {len(hits) - MAX_EXAMPLES} more)" if len(hits) > MAX_EXAMPLES else ""
    return Issue(WARNING, field_id, f"{name}: {len(hits)} {noun}: {shown}{more}" + (f"; {advice}" if advice else ""))


def _number_rules(venue: Venue, values: dict[str, str]) -> tuple[Optional[str], Optional[bool]]:
    """The venue's ``manuscript.thousands_separator`` and ``group_four_digit_numbers`` for the selected article type."""
    from .requirements import get_requirements, resolve

    base = get_requirements(venue.slug)
    if base is None:
        return None, None
    rules = resolve(base, values).manuscript
    return rules.thousands_separator, rules.group_four_digit_numbers


def _et_al_period(venue: Venue, values: dict[str, str]) -> Optional[bool]:
    """The venue's ``manuscript.et_al_period`` for the selected article type."""
    from .requirements import get_requirements, resolve

    base = get_requirements(venue.slug)
    return None if base is None else resolve(base, values).manuscript.et_al_period


def _numbers_check(venue: Venue, values: dict[str, str]) -> Outcome:
    from .prose_check import _main_paragraphs, _manuscripts

    issues: list[Issue] = []
    details: list[str] = []
    separator, group_four = _number_rules(venue, values)
    for fld, text in _manuscripts(venue, values):
        if text.why:
            issues.append(Issue(WARNING, fld.id, text.why, unchecked=True))
            continue
        paragraphs = [p for p in _main_paragraphs(text.paragraphs) if not _is_heading(p)]
        spelled = [hit for p in paragraphs for hit in spelled_numbers(p)]
        opening = [hit for p in paragraphs for hit in digit_sentence_starts(p)]
        if spelled:
            issues.append(_report(spelled, "number(s) of 10 or more spelled out", fld.id, text.path.name, "write numbers of 10 and over in digits"))
        if opening:
            issues.append(_report(opening, "sentence(s) opening with digits", fld.id, text.path.name))
        grouping = thousands_grouping(paragraphs, separator, group_four)
        for desc, hits in grouping:
            issues.append(_report(hits, desc, fld.id, text.path.name))
        if not spelled and not opening and not grouping:
            details.append(f"none in {text.path.name}")
    return Outcome(issues, "; ".join(details))


def _conventions_check(venue: Venue, values: dict[str, str]) -> Outcome:
    from .prose_check import PDF_CHECKS, _main_paragraphs, _manuscripts

    issues: list[Issue] = []
    details: list[str] = []
    for fld, text in _manuscripts(venue, values):
        if text.why:
            issues.append(Issue(WARNING, fld.id, text.why, unchecked=True))
            continue
        # Text extracted from a PDF rejoins words hyphenated across lines, so hyphenation is not compared there.
        # Legends are left out: a venue may write "Fig." in legends and "Figure" in the text.
        body = [p for p in _body_paragraphs(text) if not _CAPTION_START.match(p)]
        found = conventions(body, hyphens=text.checks != PDF_CHECKS, et_al_period=_et_al_period(venue, values))
        issues += [Issue(WARNING, fld.id, f"{text.path.name}: {p}") for p in found.problems]
        if not found.problems:
            details.append(f"{text.path.name}: consistent ({', '.join(found.checked)})")
    return Outcome(issues, "; ".join(details))


def _body_paragraphs(text) -> list[str]:
    """The main text's paragraphs, with LaTeX floats and a PDF's legends left out (Word and Markdown legends are paragraphs of their own)."""
    from . import document
    from .prose_check import PDF_CHECKS, _main_paragraphs, _tex_paragraphs

    doc = document.get(text.path)
    if doc.is_tex and doc.tex_source is not None:
        return _main_paragraphs(_tex_paragraphs(_tex_without_floats(doc.tex_source)))
    if text.checks == PDF_CHECKS and doc.page_texts is not None:
        from .manuscript import main_text_end

        whole = "\n\n\f".join(_pdf_without_captions(page) for page in doc.page_texts)
        end = main_text_end(whole)
        if end:
            whole = whole[: end.start()]
        # A PDF page has no paragraphs of its own; blank lines (or the page) separate them.
        return [re.sub(r"\s*\n\s*", " ", b).strip() for page in whole.split("\f") for b in re.split(r"\n\s*\n", page) if b.strip()]
    return _main_paragraphs(text.paragraphs)


def _acronym_check(venue: Venue, values: dict[str, str]) -> Outcome:
    from . import document
    from .front_matter_check import manuscript_abstract
    from .prose_check import _manuscripts

    issues: list[Issue] = []
    details: list[str] = []
    for fld, text in _manuscripts(venue, values):
        if text.why:
            issues.append(Issue(WARNING, fld.id, text.why, unchecked=True))
            continue
        paragraphs = _body_paragraphs(text)
        if not paragraphs:
            raise Unchecked(f"no prose read from {text.path.name}")
        own = manuscript_abstract(document.get(text.path), values.get("abstract", ""))
        abstract_text = own[0] if own else ""
        if own and own[1]:
            from .prose_check import _tex_paragraphs

            abstract_text = " ".join(_tex_paragraphs("\\begin{document}" + abstract_text))
        abstract, body = split_abstract(paragraphs, abstract_text)
        found = acronyms(body, abstract)
        for kind, noun, advice in _ACRONYM_KINDS:
            hits = getattr(found, kind)
            if hits:
                issues.append(_report(hits, noun, fld.id, text.path.name, advice))
        if not any(getattr(found, k) for k, _, _ in _ACRONYM_KINDS):
            details.append(f"{text.path.name}: {found.defined} acronym(s) defined at first use" + ("" if abstract else " (abstract not found, so it was read as main text)"))
    return Outcome(issues, "; ".join(details))


# --- heading capitalization -----------------------------------------------------------------

_HEADING_WORD = re.compile(r"(?<![\w'\u2019-])[^\W\d_][\w'\u2019-]*")
# Lower-case in Title Case too, so they say nothing about the style.
_HEADING_SMALL = frozenset("a an and as at but by for from in into nor of on onto or over per the to under via vs versus with within without between among".split())
_CASE_NAMES = {"upper": "ALL CAPS", "title": "Title Case", "sentence": "Sentence case", "lower": "lower case"}
# Fewer letters than this in capitals may be an acronym rather than a style ("RNA").
_UPPER_MIN_LETTERS = 5


def _style_neutral(word: str) -> bool:
    """A word whose capitals are its own: an acronym or mixed-case name ("RNA", "scRNA", "mTOR", "H3K27ac")."""
    return any(c.isdigit() for c in word) or any(c.isupper() for part in word.split("-") for c in part[1:])


def heading_case(text: str, proper: frozenset[str] | set[str] = frozenset()) -> str | None:
    """A heading's style ("upper", "title", "sentence", "lower"), or None when its words do not show one."""
    words = _HEADING_WORD.findall(text)
    if not words:
        return None
    letters = [c for c in text if c.isalpha()]
    if not any(c.islower() for c in letters):
        return "upper" if len(letters) >= _UPPER_MIN_LETTERS else None
    first = words[0]
    if first.islower() and not _style_neutral(first) and len(first.split("-")[0]) > 1:
        return "lower"
    capitalized = lowered = 0
    for word in words[1:]:
        if word.lower() in _HEADING_SMALL or word.lower() in proper or _style_neutral(word):
            continue
        if word[0].isupper():
            capitalized += 1
        else:
            lowered += 1
    if capitalized and lowered:
        return None
    return "title" if capitalized else "sentence" if lowered else None


def inconsistent_heading_case(headings: list[tuple[str, str]], proper: frozenset[str] | set[str] = frozenset()) -> tuple[list[str], int]:
    """Headings styled differently from most others of their level, and how many headings showed a style."""
    levels: dict[str, list[tuple[str, str]]] = {}
    for level, text in headings:
        style = heading_case(text, proper)
        if style:
            levels.setdefault(level, []).append((text, style))
    hits: list[str] = []
    for found in levels.values():
        counts = Counter(style for _, style in found)
        if len(counts) > 1:
            usual = counts.most_common(1)[0][0]
            hits += [f"'{text}' is {_CASE_NAMES[style]}, others are {_CASE_NAMES[usual]}" for text, style in found if style != usual]
    return hits, sum(len(found) for found in levels.values())


def _proper_words(paragraphs: list[str]) -> set[str]:
    """Lower-cased words the running text capitalizes mid-sentence and never writes in lower case: names."""
    body = [p for p in paragraphs if not _is_heading(p)]
    capitalized = {w.lower() for p in body for w in re.findall(r"(?<=[a-z,;] )[A-Z][a-z]+\b", p)}
    return capitalized - {w for p in body for w in re.findall(r"\b[a-z]+\b", p)}


def _heading_case_check(venue: Venue, values: dict[str, str]) -> Outcome:
    from .manuscript import cased_headings
    from .prose_check import _manuscripts

    title = _norm(values.get("title", ""))
    issues: list[Issue] = []
    details: list[str] = []
    for fld, text in _manuscripts(venue, values):
        if text.why:
            issues.append(Issue(WARNING, fld.id, text.why, unchecked=True))
            continue
        headings = cased_headings(text.path)
        if headings is None:
            issues.append(Issue(WARNING, fld.id, f"could not read the headings of {text.path.name}", unchecked=True))
            continue
        hits, styled = inconsistent_heading_case([h for h in headings if _norm(h[1]) != title], _proper_words(text.paragraphs))
        if hits:
            issues.append(_report(hits, "heading(s) capitalized differently from the others at their level", fld.id, text.path.name, "use one style for headings of the same level"))
        elif styled:
            details.append(f"{text.path.name}: {styled} heading(s) capitalized consistently")
    if not issues and not details:
        raise NotApplicable("found no headings whose capitalization shows a style")
    return Outcome(issues, "; ".join(details))


# --- the AI task ----------------------------------------------------------------------------

AI_PROSE_ID = "ai.prose"
AI_PROSE_TITLE = "Spelling, grammar, and tense (read by AI)"
_AI_PROSE = (
    "Proofread the manuscript's prose (not the reference list, math, code, or tables of numbers). Look for:\n"
    "1. Spelling mistakes and typos, including real words in the wrong place ('form' for 'from', 'than' for 'then') and {spelling}. "
    "Gene, protein, software, and other proper names are not misspellings.\n"
    "2. Grammar: subject-verb agreement, missing or doubled words, wrong articles (a/an/the) or prepositions, dangling modifiers, run-on sentences, comma splices, and sentences that do not parse.\n"
    "3. Tense: past tense for what this study did and found (Methods, Results, and the abstract's findings); present tense for established facts, "
    "for what a figure or table shows ('Figure 2 shows'), and for interpretation and conclusions (Introduction, Discussion). Flag tense that drifts within a paragraph, "
    "and a section that breaks these conventions inconsistently. A section that follows another convention consistently (for example Methods in the present tense, common in computer science) is not a problem.\n"
    "Do not report style preferences, word choice that is correct, or anything the author could defensibly keep. "
    "Report 'fail' listing each problem (up to 25, the clearest first) as: the section or page, a short quote, and the correction; else 'pass'."
)


def ai_prose_task(venue: Venue, values: dict[str, str]):
    """The AI task for what a pattern cannot judge: typos beyond a list, grammar, and tense."""
    from .ai_validate import AiTask
    from .requirements import get_requirements, resolve
    from .requirements_check import manuscript_files

    files = [str(p) for _, p in manuscript_files(venue, values)]
    if not files:
        return None
    base = get_requirements(venue.slug)
    language = (resolve(base, values).manuscript.language or "").strip() if base is not None else ""
    if language and language.lower() != "english":
        spelling = f"spellings that do not follow the guidelines' '{language}' variant"
    else:
        spelling = "a mix of American and British spellings ('color' and 'colour')"
    return AiTask(id=AI_PROSE_ID, title=AI_PROSE_TITLE, category="Prose", instructions=_AI_PROSE.format(spelling=spelling), files=files)
