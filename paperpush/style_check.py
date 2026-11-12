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
  meanings. The abstract and the main text are separate scopes, so an acronym
  is defined once in each. Each figure and table legend is read on its own:
  spelling an acronym out there neither counts as a second definition nor
  defines it for the main text, and a legend that uses an acronym without
  spelling it out is not reported (venues differ on that). Headings, captions,
  and the front matter before the abstract are not counted as first use.
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
# "twenty first century"; not "ten first-year students" or a "thirty second interval".
_ORDINAL_SPACED = re.compile(r"\s+(?:first|second(?=\s+centur)|third|fourth|fifth|sixth|seventh|eighth|ninth)\b", re.IGNORECASE)
# Abbreviations whose full stop does not end a sentence.
_NOT_SENTENCE_END = {"jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "max", "avg", "al", "fig", "figs", "eq", "eqs", "ref", "refs", "no", "nos", "vs", "approx", "resp", "cf", "ca", "sec", "ch", "vol", "pp", "dr", "st", "mr", "ms", "suppl", "supp", "e.g", "i.e", "et"}
# Digits opening a sentence: "... ended. 15 patients were ...".
_DIGIT_START = re.compile(r"(\S+?)([.!?])([\"')\]\u201d]*)\s+(\d[\d,.]*)\s+(?=[a-z])")


def words_to_number(phrase: str) -> int | None:
    """``"twenty-five"`` -> 25, ``"one hundred and twelve"`` -> 112; None if it does not parse or is not one number ("ten-eleven", "fifty-fifty")."""
    total = current = 0
    for word in re.split(r"[-\s]+", phrase.lower()):
        if word in ("and", ""):
            continue
        if word in _UNITS:
            if current % 10:
                return None
            current += _UNITS[word]
        elif word in _TENS:
            if current % 100:
                return None
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
        if value >= 20 and value % 10 == 0 and _ORDINAL_SPACED.match(paragraph, end):
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


# The name right before a number that belongs to it: "Hoechst 33342", "ATCC 25922", "NovaSeq 6000", "CA 90089".
_NAME_BEFORE = re.compile(r"(?<![\w-])([A-Z][\w-]*)(,?)[ \t\u00a0]+$")
_PRODUCTS = frozenset("Hoechst Tween Triton Lipofectamine Trizol Brij".split())
# "At week 12 150 patients" is two numbers, not 12,150.
_TIMEPOINT_BEFORE = re.compile(r"\b(?:weeks?|days?|months?|years?|hours?|visits?|cycles?|groups?|arms?|cohorts?|passages?|stages?|lanes?|batch(?:es)?|n)\s*=?\s*$", re.IGNORECASE)


def _named_number(paragraph: str, start: int) -> bool:
    """Whether the unseparated number at ``start`` is part of a product, strain, or postal name."""
    lo = max(0, start - 30)
    m = _NAME_BEFORE.search(paragraph[lo:start])
    if m is None or (m.group(2) and not re.fullmatch(r"[A-Z]{2}", m.group(1))):
        return False
    word = m.group(1)
    return word in _PRODUCTS or any(c.isupper() or c.isdigit() for c in word[1:]) or not _sentence_start(paragraph, lo + m.start(1))


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
        if style == "none" and _named_number(paragraph, m.start()):
            continue
        if style == "space" and not re.search(f"[{_THIN}]", whole) and _TIMEPOINT_BEFORE.search(paragraph[max(0, m.start() - 15) : m.start()]):
            continue
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
_ET_AL_BAD = re.compile(r"\bet\.\s?al\b\.?|\bet al(?![.\w]|[*_]+\.)|\bet\.al\b\.?")
# The same, for a venue that writes "et al" with no full stop: the misplaced stops, and a stop that does not end the sentence.
_ET_AL_BAD_NO_PERIOD = re.compile(r"\bet\.\s?al\b\.?|\bet\.al\b\.?|\bet al\.(?=\s*[,;:\[(]|\s+[a-z\d])")
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
            if " " in core and "." not in core:
                continue  # two letters ("variables i e"), not "i. e."
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
# A compound with a lower-case word counts ("BPE-inspired", "LLM-based"); "IL-6" and "HLA-DR" do not.
_ACRONYM = re.compile(r"(?<![\w\-/.\\@])([A-Z][A-Z&]*[A-Z])(s?)(?![\w/@])(?!-(?![a-z]))")
# "full term (ACR)", "(ACRs)", "(ACR; ref. 3)", "(ACR, ...)".
_DEFINITION = re.compile(r"\(\s*([A-Z][A-Z&]*[A-Z])s?\s*(?=[);,])")
# A mixed-case coinage in parentheses, "t-Distributed Neural Networks (TDistNNs)": an acronym only
# when the words before it spell its capitals, so "(PyTorch)" or "(ImageNet)" is not one.
# A legend's glossary entry, "FA: false alarms" or "FA = false alarms", at the start of a sentence: a definition in a legend only.
_LEGEND_DEFINITION = re.compile(r"(?:^|(?<=[.;]\s))\s*([A-Z][A-Z&]*[A-Z])s?\s*[:=]\s+(?=[a-z])")
_MIXED_DEFINITION = re.compile(r"\(\s*([A-Z](?=[A-Za-z]*[a-z])(?=[A-Za-z]*[A-Z][A-Za-z]*[A-Z])[A-Za-z]{2,10}?)s?\s*(?=[);,])")
# Known to every reader of a paper, or not an acronym at all.
_KNOWN = frozenset(
    """DNA RNA USA UK EU US UN PDF HTML URL CSV JSON XML GPU GPUs CPU RAM IEEE ACM ISBN ISSN DOI ORCID PI II III IV VI VII VIII IX XI XII
    AM PM UTC GMT OK ID AI IT TV GB MB KB TB GHz HD LED USB NA NAN SD SE SEM CI IQR ANOVA TODO FIXME NOTE COVID SARS HIV AIDS PCR MRI CT
    PET EEG ECG EKG FDA NIH NSF ERC WHO CDC MIT UCLA USC MD PHD PhD MS MSc BS BSc BA MA ET AL FIG EQ VS IE EG CF NB PS AND OR THE OF IN ON
    AT TO FOR BY WITH IS ARE NOT ABSTRACT INTRODUCTION METHODS RESULTS DISCUSSION CONCLUSION CONCLUSIONS REFERENCES ACKNOWLEDGMENTS
    ACKNOWLEDGEMENTS SUPPLEMENTARY APPENDIX BACKGROUND MATERIALS DATA CODE AVAILABILITY FUNDING TABLE FIGURE NY CA MA TX UK LLC INC
    MATLAB ATP GTP NADH NADPH TEX LATEX BIBTEX""".split()
)
_ROMAN = re.compile(r"[IVXLC]+")
# Words an acronym's letters skip ("Department of Energy (DE)").
_ACRONYM_LINK_WORDS = frozenset("a an and the of for in on to with by".split())
# Uses of an acronym that is never defined before it is reported (one use is often a name in passing).
_UNDEFINED_MIN_USES = 2
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
    other_uses: int = 0  # in legends and tables
    legend_defined: bool = False  # spelled out in a legend
    snippet: str = ""
    expansions: list[str] = field(default_factory=list)


def _expansion(before: str, acronym: str) -> str | None:
    """The words before "(ACR)" whose initials spell it, lower-cased; None if they do not ("electroencephalogram (EEG)").

    A mixed-case acronym is spelled by its capitals: "t-Distributed Neural Networks (TDistNN)".
    """
    words = re.findall(r"[A-Za-z0-9']+", before)
    letters = acronym.replace("&", "")
    if not letters.isupper():
        letters = letters[0] + "".join(c for c in letters[1:] if c.isupper())
    picked: list[str] = []
    i = len(words) - 1
    for letter in reversed(letters.lower()):
        while i >= 0 and words[i].lower() in _ACRONYM_LINK_WORDS and words[i][0].lower() != letter:
            i -= 1
        if i < 0 or words[i][0].lower() != letter:
            return None
        picked.append(words[i].lower())
        i -= 1
    picked[0] = picked[0][:-1] if len(picked[0]) > 3 and picked[0].endswith("s") else picked[0]  # the last word: singular
    return " ".join(reversed(picked))


def _skipped(acr: str) -> bool:
    return acr in _KNOWN or bool(_ROMAN.fullmatch(acr)) or len(acr) > 8


def mixed_case_acronyms(paragraphs: list[str]) -> set[str]:
    """Mixed-case acronyms ("TDistNN") a paragraph spells out: "t-Distributed Neural Networks (TDistNNs)"."""
    found: set[str] = set()
    for p in paragraphs:
        for m in _MIXED_DEFINITION.finditer(p):
            if _expansion(p[: p.rfind("(", 0, m.start(1))], m.group(1)):
                found.add(m.group(1))
    return found


def _acronym_matches(p: str, pattern: re.Pattern, mixed: re.Pattern | None):
    yield from pattern.finditer(p)
    if mixed is not None:
        yield from mixed.finditer(p)


def _scan_acronyms(paragraphs: list[str], mixed: set[str] | frozenset[str] = frozenset()) -> dict[str, _Acro]:
    """Each acronym's definitions and uses in the body text of ``paragraphs``.

    Uses in a legend or a table (a paragraph :func:`_counts` leaves out, other
    than a heading) are counted in ``other_uses``: they are not a first use,
    but an acronym used only there still has to be spelled out somewhere.
    """
    from .prose_check import _mark, _snippet

    names = "|".join(re.escape(m) for m in sorted(mixed, key=len, reverse=True))
    mixed_use = re.compile(rf"(?<![\w\-/.\\@])({names})(s?)(?![\w/@])(?!-(?![a-z]))") if names else None
    mixed_def = re.compile(rf"\(\s*({names})s?\s*(?=[);,])") if names else None
    found: dict[str, _Acro] = {}
    for i, p in enumerate(paragraphs):
        if not _counts(p):
            if not _is_heading(p) or _CAPTION_START.match(p):
                defs = {(m.group(1), m.start(1)) for m in _acronym_matches(p, _DEFINITION, mixed_def)}
                if _CAPTION_START.match(p):
                    defs |= {(m.group(1), m.start(1)) for m in _LEGEND_DEFINITION.finditer(p)}
                for m in _acronym_matches(p, _ACRONYM, mixed_use):
                    if not _skipped(m.group(1)):
                        entry = found.setdefault(m.group(1), _Acro())
                        if (m.group(1), m.start(1)) in defs:
                            entry.legend_defined = True
                        else:
                            entry.other_uses += 1
            continue
        defined_here = {(m.group(1), m.start(1)) for m in _acronym_matches(p, _DEFINITION, mixed_def)}
        for acr, pos in sorted(defined_here, key=lambda x: x[1]):
            if _skipped(acr):
                continue  # its uses are not counted either: "(USA)", "(III)"
            entry = found.setdefault(acr, _Acro())
            entry.definitions.append((i, pos))
            expansion = _expansion(p[: p.rfind("(", 0, pos)], acr)
            if expansion and expansion not in entry.expansions:
                entry.expansions.append(expansion)
        for m in _acronym_matches(p, _ACRONYM, mixed_use):
            acr = m.group(1)
            if (acr, m.start(1)) in defined_here or _skipped(acr):
                continue
            a = found.setdefault(acr, _Acro())
            a.uses += 1
            if a.first_use is None:
                a.first_use = (i, m.start())
                a.snippet = _snippet(p, m.start(), m.end(), _mark)
    return found


def _float_acronyms(paragraphs: list[str], mixed: set[str] | frozenset[str]) -> tuple[Counter, set[str]]:
    """Acronym uses in legend and table text, and the acronyms it spells out ("... (ACR)")."""
    names = "|".join(re.escape(m) for m in sorted(mixed, key=len, reverse=True))
    mixed_use = re.compile(rf"(?<![\w\-/.\\@])({names})(s?)(?![\w/@])(?!-(?![a-z]))") if names else None
    mixed_def = re.compile(rf"\(\s*({names})s?\s*(?=[);,])") if names else None
    uses: Counter = Counter()
    defined: set[str] = set()
    for p in paragraphs:
        defs = {(m.group(1), m.start(1)) for m in _acronym_matches(p, _DEFINITION, mixed_def)}
        defs |= {(m.group(1), m.start(1)) for m in _LEGEND_DEFINITION.finditer(p)}
        defined |= {acr for acr, _ in defs}
        uses.update(m.group(1) for m in _acronym_matches(p, _ACRONYM, mixed_use) if (m.group(1), m.start(1)) not in defs and not _skipped(m.group(1)))
    return uses, defined


@dataclass
class Acronyms:
    before_definition: list[str] = field(default_factory=list)
    redefined: list[str] = field(default_factory=list)
    undefined: list[str] = field(default_factory=list)
    rarely_used: list[str] = field(default_factory=list)
    conflicting: list[str] = field(default_factory=list)
    abstract_only: list[str] = field(default_factory=list)
    abstract_undefined: list[str] = field(default_factory=list)
    legend_only: list[str] = field(default_factory=list)
    defined: int = 0


def acronyms(body: list[str], abstract: list[str] | None = None, floats: list[str] | None = None, tables: list[str] | None = None) -> Acronyms:
    """Acronyms in ``body`` (the main text) and ``abstract`` not introduced at first use.

    ``floats`` is the legend text of the main text's figures and tables that
    ``body`` leaves out: an acronym used there counts toward its uses, and one
    spelled out there counts as spelled out. ``tables`` is table-cell text: it
    only shows that an acronym is used (a column of method names is not where
    acronyms are introduced, so cells alone do not make one "undefined").
    """
    out = Acronyms()
    mixed = mixed_case_acronyms(body + (abstract or []) + (floats or []))
    main = _scan_acronyms(body, mixed)
    abs_found = _scan_acronyms(abstract or [], mixed)
    float_uses, float_defined = _float_acronyms(floats or [], mixed)
    for acr, n in float_uses.items():
        main.setdefault(acr, _Acro()).other_uses += n
    table_uses, table_defined = _float_acronyms(tables or [], mixed)
    float_defined |= table_defined
    out.defined = sum(1 for a in main.values() if a.definitions)
    for acr, a in main.items():
        if a.definitions:
            if a.first_use is not None and a.first_use < a.definitions[0]:
                out.before_definition.append(f"'{acr}' first appears in {a.snippet}, before it is spelled out")
            meanings = list(dict.fromkeys(a.expansions + (abs_found[acr].expansions if acr in abs_found else [])))
            if len(meanings) > 1:
                out.conflicting.append(f"'{acr}' (" + ", ".join(f"'{m}'" for m in meanings) + ")")
            elif len(a.definitions) > 1 and a.expansions:
                out.redefined.append(f"'{acr}' ({len(a.definitions)} times)")
            if a.uses == 0 and a.other_uses == 0 and not table_uses[acr] and a.expansions:  # only the definition: the acronym is never used
                out.rarely_used.append(f"'{acr}'")
        elif acr in abs_found and abs_found[acr].definitions and a.uses:
            out.abstract_only.append(f"'{acr}' (first used in {a.snippet})")
        elif acr in float_defined or a.legend_defined:
            # A legend is read on its own, so spelling an acronym out there does not define it for the main text.
            if a.uses:
                out.legend_only.append(f"'{acr}' (first used in {a.snippet})")
        elif a.uses + a.other_uses >= _UNDEFINED_MIN_USES:
            where = f"first in {a.snippet}" if a.uses else "only in figure legends or tables"
            out.undefined.append(f"'{acr}' ({a.uses + a.other_uses}x, {where})")
    for acr, a in abs_found.items():
        if a.uses and not a.definitions and acr in main and main[acr].definitions:
            out.abstract_undefined.append(f"'{acr}'")
    return out


_ACRONYM_KINDS = (
    ("before_definition", "acronym(s) used before they are spelled out", "define each at its first use in the main text"),
    ("abstract_only", "acronym(s) spelled out only in the abstract", "the main text should define them again at first use"),
    ("abstract_undefined", "acronym(s) the abstract uses without spelling out (the main text does)", "an abstract is read on its own, so define them there too"),
    ("legend_only", "acronym(s) spelled out only in a figure or table legend", "a legend is read on its own, so define them at first use in the main text too"),
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
        return _split_abstract_inline(paragraphs, abstract_text)
    first, last = inside[0], inside[-1]
    return paragraphs[first : last + 1], paragraphs[last + 1 :]


def _words_pattern(words: list[str]) -> re.Pattern:
    # Any run of non-letters (spaces, line breaks, punctuation, a hyphen at a line break) between the words.
    # A word may also be broken across lines with a hyphen ("manu-\nscripts").
    word = lambda w: r"(?:-\s+)?".join(re.escape(c) for c in w)
    return re.compile(r"[\W_]*".join(word(w) for w in words), re.IGNORECASE)


_ABSTRACT_ANCHOR_WORDS = 6


def _split_abstract_inline(paragraphs: list[str], abstract_text: str) -> tuple[list[str], list[str]]:
    """The abstract found inside a paragraph rather than as paragraphs of its own.

    A two-column PDF's extracted text often has no blank line between the
    title, the abstract, and the introduction, so all three arrive as one
    paragraph. Find the abstract's first and last words in the text and cut
    there; ``([], paragraphs)`` when they are not found.
    """
    words = re.findall(r"[a-z0-9]+", abstract_text.lower())
    if len(words) < 2 * _ABSTRACT_ANCHOR_WORDS:
        return [], paragraphs
    head = _words_pattern(words[:_ABSTRACT_ANCHOR_WORDS])
    tail = _words_pattern(words[-_ABSTRACT_ANCHOR_WORDS:])
    for i, p in enumerate(paragraphs):
        start = head.search(p)
        if not start:
            continue
        for j in range(i, len(paragraphs)):
            end = tail.search(paragraphs[j], start.end() if j == i else 0)
            if end:
                if j == i:
                    abstract = [p[start.start() : end.end()]]
                else:
                    abstract = [p[start.start() :], *paragraphs[i + 1 : j], paragraphs[j][: end.end()]]
                rest = paragraphs[j][end.end() :].lstrip(" .;:\n")
                return abstract, ([rest] if rest.strip() else []) + paragraphs[j + 1 :]
        return [], paragraphs
    return [], paragraphs


_TEX_FLOAT = re.compile(r"\\begin\s*\{\s*(figure|table|SCfigure|wrapfigure|sidewaysfigure|sidewaystable)\*?\s*\}.*?\\end\s*\{\s*\1\*?\s*\}", re.DOTALL)
# Where the main text of a LaTeX source ends: the reference list or the appendices.
_TEX_MAIN_END = re.compile(r"\\(?:bibliography\s*\{|printbibliography\b|begin\s*\{\s*thebibliography\s*\}|appendix\b)")


_TEX_TABULAR = re.compile(r"\\(?:begin\s*\{\s*(?:tabular[xy*]?|longtable|tblr)\s*\}(?:\s*\[[^\]]*\])?(?:\s*\{(?:[^{}]|\{[^{}]*\})*\}){1,2}|end\s*\{\s*(?:tabular[xy*]?|longtable|tblr)\s*\})")

_TEX_TABULAR_ENV = re.compile(r"\\begin\s*\{\s*(tabular[xy*]?|longtable|tblr)\s*\}.*?\\end\s*\{\s*\1\s*\}", re.DOTALL)


def _tex_without_floats(source: str) -> str:
    """LaTeX source with its figure and table environments removed (captions are not first use)."""
    return _TEX_FLOAT.sub(" ", source)


def _tex_main_floats(source: str) -> tuple[list[str], list[str]]:
    """The legends (and other text) of the figures and tables of a LaTeX source's main text, and its table cells, as paragraphs."""
    from .prose_check import _tex_paragraphs

    begin = re.search(r"\\begin\s*\{\s*document\s*\}", source)
    start = begin.end() if begin else 0
    end = _TEX_MAIN_END.search(source, start)
    floats = [m.group(0) for m in _TEX_FLOAT.finditer(source, start, end.start() if end else len(source))]
    if not floats:
        return [], []
    text = "\n\n".join(floats)
    legends = _tex_paragraphs("\\begin{document}\n" + text + "\n\\end{document}")
    # Table cells: unwrap each tabular (which prose reading masks) and split its cells.
    tables = "\n\n".join(m.group(0) for m in _TEX_TABULAR_ENV.finditer(text))
    tables = re.sub(r"(?<!\\)&", " , ", _TEX_TABULAR.sub("\n\n", tables))
    cells = _tex_paragraphs("\\begin{document}\n" + tables + "\n\\end{document}") if tables else []
    return legends, cells


def _pdf_split_captions(page: str) -> tuple[str, list[str]]:
    """A PDF page's text without its figure and table legends (from a 'Figure 2.' line to the paragraph's end), and the legends."""
    lines = page.split("\n")
    keep, legends, in_caption = [], [], False
    for line in lines:
        if _CAPTION_START.match(line):
            in_caption = True
            legends.append([])
        elif in_caption and (not line.strip() or (line.rstrip().endswith(".") and len(line) < 50)):
            in_caption = False
            legends[-1].append(line)
            continue
        if in_caption:
            legends[-1].append(line)
        else:
            keep.append(line)
    return "\n".join(keep), [re.sub(r"\s*\n\s*", " ", "\n".join(legend)).strip() for legend in legends]


def _pdf_without_captions(page: str) -> str:
    """A PDF page's text with figure and table legends (from a 'Figure 2.' line to the paragraph's end) removed."""
    return _pdf_split_captions(page)[0]


def _pdf_main_captions(doc) -> list[str]:
    """The legends of a PDF's main text (before the reference list)."""
    from .manuscript import main_text_end

    pages = doc.page_texts or []
    whole = "\n\n\f".join(pages)
    end = main_text_end(whole)
    if end:
        whole = whole[: end.start()]
    return [legend for page in whole.split("\f") for legend in _pdf_split_captions(page)[1] if legend]


# --- the checks -------------------------------------------------------------------------------


def _report(hits: list[str], noun: str, field_id: str, name: str, advice: str = "", named: bool = False) -> Issue:
    """One warning listing ``hits``; past the first few, only their names when ``named`` (each hit opens with its quoted name)."""
    shown = "; ".join(hits[:MAX_EXAMPLES])
    rest = hits[MAX_EXAMPLES:]
    if rest and named:
        more = f" (and {len(rest)} more: " + ", ".join(h.split(" ", 1)[0] for h in rest) + ")"
    else:
        more = f" (and {len(rest)} more)" if rest else ""
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


# A line of code or a code listing in a PDF's text: a "#" comment or a "snake_case:" / "@key:" key.
_PDF_CODE_LINE = re.compile(r"^\s*(?:#|@?[A-Za-z][\w.-]*_[\w.-]*:\s|@[A-Za-z][\w.-]*:\s)")


def _pdf_table_or_code_line(line: str) -> bool:
    """Whether a line of a PDF's text is code or a table row rather than prose.

    A table's header or row has no word in lower case and no sentence end
    ("System Detected % Partial FA Min $"); a line of prose that wraps
    almost always has one, and a parenthesis may be a definition, so a line
    with one is kept.
    """
    if _PDF_CODE_LINE.match(line):
        return True
    tokens = line.split()
    if len(tokens) < 4 or "(" in line or line.rstrip().endswith((".", ":", ";", "?", "!", ",")):
        return False
    return not any(t[0].islower() for t in tokens) and any(c.isalpha() for c in line)


def _body_paragraphs(text, prose_only: bool = False) -> list[str]:
    """The main text's paragraphs, with LaTeX floats and a PDF's legends left out (Word and Markdown legends are paragraphs of their own).

    ``prose_only`` also drops a PDF's code lines and table rows, which its page text keeps.
    """
    from . import document
    from .prose_check import PDF_CHECKS, _main_paragraphs, _tex_paragraphs

    doc = document.get(text.path)
    if doc.is_tex and doc.tex_source is not None:
        return _main_paragraphs(_tex_paragraphs(_tex_without_floats(doc.tex_source)))
    if text.checks == PDF_CHECKS and doc.page_texts is not None:
        from .manuscript import main_text_end

        pages = [_pdf_without_captions(page) for page in doc.page_texts]
        if prose_only:
            pages = ["\n".join(line for line in page.split("\n") if not _pdf_table_or_code_line(line)) for page in pages]
        whole = "\n\n\f".join(pages)
        end = main_text_end(whole)
        if end:
            whole = whole[: end.start()]
        # A PDF page has no paragraphs of its own; blank lines (or the page) separate them.
        return [re.sub(r"\s*\n\s*", " ", b).strip() for page in whole.split("\f") for b in re.split(r"\n\s*\n", page) if b.strip()]
    return _main_paragraphs(text.paragraphs)


def _acronym_check(venue: Venue, values: dict[str, str]) -> Outcome:
    from . import document
    from .front_matter_check import manuscript_abstract
    from .prose_check import PDF_CHECKS, _manuscripts

    issues: list[Issue] = []
    details: list[str] = []
    for fld, text in _manuscripts(venue, values):
        if text.why:
            issues.append(Issue(WARNING, fld.id, text.why, unchecked=True))
            continue
        paragraphs = _body_paragraphs(text, prose_only=True)
        if not paragraphs:
            raise Unchecked(f"no prose read from {text.path.name}")
        own = manuscript_abstract(document.get(text.path), values.get("abstract", ""))
        abstract_text = own[0] if own else ""
        if own and own[1]:
            from .prose_check import _tex_paragraphs

            abstract_text = " ".join(_tex_paragraphs("\\begin{document}" + abstract_text))
        abstract, body = split_abstract(paragraphs, abstract_text)
        doc = document.get(text.path)
        floats: list[str] = []
        tables: list[str] = []
        if doc.is_tex and doc.tex_source:
            floats, tables = _tex_main_floats(doc.tex_source)
        elif text.checks == PDF_CHECKS and doc.page_texts is not None:
            floats = _pdf_main_captions(doc)  # a PDF's tables are in its page text
        found = acronyms(body, abstract, floats, tables)
        for kind, noun, advice in _ACRONYM_KINDS:
            hits = getattr(found, kind)
            if hits:
                issues.append(_report(hits, noun, fld.id, text.path.name, advice, named=True))
        if not any(getattr(found, k) for k, _, _ in _ACRONYM_KINDS):
            details.append(f"{text.path.name}: {found.defined} acronym(s) defined at first use" + ("" if abstract else " (abstract not found, so it was read as main text)"))
    return Outcome(issues, "; ".join(details))


# --- heading capitalization -----------------------------------------------------------------

_HEADING_WORD = re.compile(r"(?<![\w'\u2019-])[^\W\d_][\w'\u2019-]*")
# Lower-case in Title Case too, so they say nothing about the style.
_HEADING_SMALL = frozenset("a an and as at but by for from in into nor of on onto or over per the to under via vs versus with within without between among".split())
_CASE_NAMES = {"upper": "ALL CAPS", "title": "Title Case", "sentence": "Sentence case", "lower": "lower case"}
# Short verbs, pronouns, and longer prepositions that some Title Case styles leave in lower case: a heading
# that lower-cases only these is not mixing styles ("When are Models Better", "Results using VDU").
_TITLE_OPTIONAL_LOWER = frozenset(
    """is are was were be been am its it his her their our we us using about above across after against along around before behind below
    beneath beside besides beyond despite during except inside like near off out outside since than through toward towards until upon""".split()
)
# Cross-references are capitalized in any style ("seen in the Figures").
_HEADING_REFERENCE_WORDS = frozenset("figure figures fig table tables section sections appendix appendices supplementary equation equations".split())
# Fewer letters than this in capitals may be an acronym rather than a style ("RNA").
_UPPER_MIN_LETTERS = 5


def _style_neutral(word: str) -> bool:
    """A word whose capitals are its own: an acronym or mixed-case name ("RNA", "scRNA", "mTOR", "H3K27ac")."""
    return any(c.isdigit() for c in word) or any(c.isupper() for part in word.split("-") for c in part[1:])


def _heading_evidence(text: str, proper: frozenset[str] | set[str] = frozenset()) -> tuple[str | None, list[str], list[str]]:
    """``(style, capitalized words, lower-case words)`` of a heading, the words after its first.

    The style is "upper", "lower", "title", "sentence", "mixed" (both
    capitalized and lower-case content words: "Code generation model
    Pre-Training"), or None when its words do not show one. Small words,
    names (``proper``), acronyms, and the word after a colon say nothing.
    """
    text = re.sub(r"\$[^$]*\$", " ", text)  # math: "$k$-dimensional"
    words = [m.group(0) for m in _HEADING_WORD.finditer(text)]
    if not words:
        return None, [], []
    letters = [c for c in text if c.isalpha()]
    if not any(c.islower() for c in letters):
        return ("upper" if len(letters) >= _UPPER_MIN_LETTERS else None), [], []
    first = words[0]
    if first.islower() and not _style_neutral(first) and len(first.split("-")[0]) > 1:
        return "lower", [], []
    after_colon = {m.end() for m in re.finditer(r"[:?!]\s+", text)}
    capitalized: list[str] = []
    lowered: list[str] = []
    for m in list(_HEADING_WORD.finditer(text))[1:]:
        word = m.group(0)
        lower = word.lower()
        head = lower.split("-")[0]
        if lower in _HEADING_SMALL or lower in proper or head in proper or lower in _HEADING_REFERENCE_WORDS or _style_neutral(word) or m.start() in after_colon:
            continue
        if len(head) == 1 and "-" in word:
            continue  # a variable, "k-dimensional", "t-Distributed"
        if word[0].isupper():
            capitalized.append(word)
        elif lower not in _TITLE_OPTIONAL_LOWER:
            lowered.append(word)
    if capitalized and lowered:
        return "mixed", capitalized, lowered
    return ("title" if capitalized else "sentence" if lowered else None), capitalized, lowered


def heading_case(text: str, proper: frozenset[str] | set[str] = frozenset()) -> str | None:
    """A heading's style ("upper", "title", "sentence", "lower"), or None when its words do not show one."""
    style = _heading_evidence(text, proper)[0]
    return None if style == "mixed" else style


def _usual_style(counts: Counter) -> str | None:
    """The style most headings in ``counts`` have, or None on a tie."""
    ranked = counts.most_common(2)
    if not ranked or (len(ranked) == 2 and ranked[0][1] == ranked[1][1]):
        return None
    return ranked[0][0]


def inconsistent_heading_case(headings: list[tuple[str, str]], proper: frozenset[str] | set[str] = frozenset(), parts: list[str] | None = None) -> tuple[list[str], int]:
    """Headings styled differently from most others of their level, and how many headings showed a style.

    Headings are compared with the others of their level in the same part
    (``parts`` gives each one's part, e.g. main text or appendix). When a
    level is split evenly, the style most headings of the document have
    decides. A heading that mixes the two styles is named, with the words
    that depart from its level's style.
    """
    groups: dict[tuple[str, str], list[tuple[str, str, list[str], list[str]]]] = {}
    for i, (level, text) in enumerate(headings):
        style, caps, lows = _heading_evidence(text, proper)
        if style:
            groups.setdefault((parts[i] if parts else "", level), []).append((text, style, caps, lows))
    overall = Counter(style for found in groups.values() for _, style, _, _ in found if style != "mixed")
    hits: list[str] = []
    for found in groups.values():
        counts = Counter(style for _, style, _, _ in found if style != "mixed")
        if len(counts) < 2 and not any(style == "mixed" for _, style, _, _ in found):
            continue
        usual = _usual_style(counts) if counts else None
        if usual is None:
            usual = _usual_style(overall)
            if usual is None or (counts and usual not in counts):
                continue
        for text, style, caps, lows in found:
            if style == usual:
                continue
            if style != "mixed":
                hits.append(f"'{text}' is {_CASE_NAMES[style]}, others are {_CASE_NAMES[usual]}")
            elif usual == "title":
                hits.append(f"'{text}' has {', '.join(repr(w) for w in lows)} in lower case, others are {_CASE_NAMES[usual]}")
            elif usual == "sentence":
                hits.append(f"'{text}' capitalizes {', '.join(repr(w) for w in caps)}, others are {_CASE_NAMES[usual]}")
    return hits, sum(1 for found in groups.values() for _, style, _, _ in found if style != "mixed")


def _proper_words(paragraphs: list[str]) -> set[str]:
    """Lower-cased words the running text capitalizes mid-sentence and never writes in lower case: names."""
    body = [p for p in paragraphs if not _is_heading(p)]
    capitalized = {w.lower() for p in body for w in re.findall(r"(?<=[a-z,;] )[A-Z][a-z]+\b", p)}
    return capitalized - {w for p in body for w in re.findall(r"\b[a-z]+\b", p)}


def _tex_heading_parts(path, count: int) -> list[str] | None:
    """"main" or "appendix" for each heading of a LaTeX source, in :func:`cased_headings` order (None for other files)."""
    from . import document
    from .manuscript import _TEX_SECTION_LEVEL

    doc = document.get(path)
    if not doc.is_tex or not doc.tex_source:
        return None
    source = doc.tex_source
    appendix = re.search(r"\\appendix\b|\\begin\s*\{\s*appendices\s*\}", source)
    parts = ["appendix" if appendix and m.start() > appendix.start() else "main" for m in _TEX_SECTION_LEVEL.finditer(source)]
    return parts if len(parts) == count else None


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
        parts = _tex_heading_parts(text.path, len(headings))
        keep = [i for i, h in enumerate(headings) if _norm(h[1]) != title]
        hits, styled = inconsistent_heading_case([headings[i] for i in keep], _proper_words(text.paragraphs), [parts[i] for i in keep] if parts else None)
        if hits:
            issues.append(Issue(WARNING, fld.id, f"{text.path.name}: {len(hits)} heading(s) capitalized differently from the others at their level: {'; '.join(hits)}; use one style for headings of the same level"))
        elif styled:
            details.append(f"{text.path.name}: {styled} heading(s) capitalized consistently")
    if not issues and not details:
        raise NotApplicable("found no headings whose capitalization shows a style")
    return Outcome(issues, "; ".join(details))


# --- the AI task ----------------------------------------------------------------------------

AI_PROSE_ID = "ai.prose"
AI_PROSE_TITLE = "Spelling, grammar, and tense (read by AI)"
_AI_PROSE = (
    "Proofread the prose of these files: the manuscript and, when listed, the title page and cover letter (not the reference list, math, code, or tables of numbers). Look for:\n"
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
    from .requirements_check import _existing, _fields_for, _paths, manuscript_files

    files = [str(p) for _, p in manuscript_files(venue, values)]
    if not files:
        return None
    files += [str(p) for role in ("title_page", "cover_letter") for f in _fields_for(venue, role) for p in _existing(_paths(f, values)) if p.suffix.lower() in (".docx", ".pdf", ".tex", ".txt", ".md")]
    files = list(dict.fromkeys(files))
    base = get_requirements(venue.slug)
    language = (resolve(base, values).manuscript.language or "").strip() if base is not None else ""
    if language and language.lower() != "english":
        spelling = f"spellings that do not follow the guidelines' '{language}' variant"
    else:
        spelling = "a mix of American and British spellings ('color' and 'colour')"
    return AiTask(id=AI_PROSE_ID, title=AI_PROSE_TITLE, category="Prose", instructions=_AI_PROSE.format(spelling=spelling), files=files)
