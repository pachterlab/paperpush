"""Check that figures and tables are cited, in order, and that their legends fit the rules.

Every venue numbers figures and tables in the order the text first cites them,
and expects each one to be cited. This module reads the manuscript text (a
PDF's, or a LaTeX source compiled to one; a Word file's paragraphs) and finds:

* **citations** -- "Fig. 2", "Figures 3-5", "Table 1", "Supplementary Fig. 4",
  "Fig. S2", "Extended Data Fig. 1", and so on, in six series (main,
  supplementary, and Extended Data figures and tables);
* **captions** -- a line that *starts* with "Figure 2." / "Fig. 2 |" / "Table
  1:", which defines that item rather than citing it.

For each series it reports an item first cited before a lower-numbered one, an
item with a caption (or an attached figure file) that the text never cites,
and a citation of an item that does not exist. A LaTeX source that cannot be
compiled is read through its ``\\label``/``\\ref`` pairs instead: the order of
the ``figure``/``table`` environments numbers the items, and the ``\\ref``
calls outside them are the citations.

The legends are checked against ``figures.legend_max_words`` and
``figures.legends_location`` (at the end of the manuscript, after the
references, or with each figure).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import document
from .database import Venue
from .requirements import get_requirements, resolve
from .validate import WARNING, Checks, Issue, NotApplicable, Outcome, Unchecked

logger = logging.getLogger(__name__)

CITATIONS = "Citations"

# (series id, readable name, check title)
SERIES = [
    ("figure", "Figure", "Main figures cited in order"),
    ("table", "Table", "Main tables cited in order"),
    ("supp_figure", "Supplementary Figure", "Supplementary figures cited in order"),
    ("supp_table", "Supplementary Table", "Supplementary tables cited in order"),
    ("ed_figure", "Extended Data Fig.", "Extended Data figures cited in order"),
    ("ed_table", "Extended Data Table", "Extended Data tables cited in order"),
]
_NAMES = {key: name for key, name, _ in SERIES}

_ITEM = r"S?\d{1,3}[a-zA-Z]?"
_MENTION = re.compile(
    r"(?P<prefix>\b(?:Supplementary|Supplemental|Suppl\.?|Supp\.?|Extended\s+Data|Appendix)\s+(?:Information\s+)?)?" r"\b(?P<word>Fig(?:ure)?s?\.?|Tables?)\s*" rf"(?P<list>{_ITEM}(?:\s*(?:,|;|and|&|[\u2013-]|to)\s*{_ITEM}(?![\w]))*)",
    re.IGNORECASE,
)
# A caption: at the start of a line, the item number then '.', ':', '|', or two spaces.
_CAPTION_TAIL = re.compile(r"\s*(?:[.:|\u2014-]|\s{2,}|$)")


@dataclass(frozen=True)
class Mention:
    series: str
    number: int
    pos: int
    caption: bool
    text: str


def _series(prefix: str, word: str, item: str) -> str | None:
    table = word.lower().startswith("tab")
    prefix = (prefix or "").lower()
    if "extended" in prefix:
        return "ed_table" if table else "ed_figure"
    if "appendix" in prefix:
        return None
    if prefix or item.upper().startswith("S"):
        return "supp_table" if table else "supp_figure"
    return "table" if table else "figure"


def _expand(spec: str) -> list[str]:
    """'1, 3-5 and S2' -> ['1', '3', '4', '5', 'S2'] (panel letters dropped)."""
    items: list[str] = []
    parts = re.split(r"\s*(?:,|;|and|&)\s*", spec)
    for part in parts:
        bounds = re.split(r"\s*(?:[\u2013-]|to)\s*", part)
        if len(bounds) == 2:
            a, b = (re.match(r"(S?)(\d+)", x, re.IGNORECASE) for x in bounds)
            if a and b and int(b.group(2)) >= int(a.group(2)) and int(b.group(2)) - int(a.group(2)) <= 30:
                items += [f"{a.group(1)}{n}" for n in range(int(a.group(2)), int(b.group(2)) + 1)]
                continue
        for x in bounds:
            m = re.match(r"(S?)(\d+)", x, re.IGNORECASE)
            if m:
                items.append(m.group(1) + m.group(2))
    return items


def mentions(text: str) -> list[Mention]:
    """Every figure/table citation and caption in ``text``, in order.

    Citations inside the reference list -- from its heading to the first
    caption after it, where collected legends usually start -- are left out.
    """
    from .manuscript import _REFERENCE_HEADING

    out: list[Mention] = []
    for m in _MENTION.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        at_line_start = not text[line_start : m.start()].strip(" \t(")
        single = len(_expand(m.group("list"))) == 1
        caption = False
        if at_line_start and single:
            before = text[:line_start].rstrip()
            previous = before[before.rfind("\n") + 1 :]
            # The line before a caption ends a paragraph: in punctuation, a page
            # number, or short of the full measure. A citation wrapped onto a new
            # line ("as shown in / Fig. 1.") follows a full, unpunctuated line.
            caption = bool(_CAPTION_TAIL.match(text, m.end())) and (not before or before[-1] in ".!?:)]\"'" or before[-1].isdigit() or len(previous) < 50)
        for item in _expand(m.group("list")):
            series = _series(m.group("prefix") or "", m.group("word"), item)
            if series is not None:
                out.append(Mention(series, int(re.sub(r"\D", "", item)), m.start(), caption, m.group(0)))
    refs = _REFERENCE_HEADING.search(text)
    if refs:
        end = next((x.pos for x in out if x.caption and x.pos > refs.end()), len(text))
        out = [x for x in out if x.caption or not refs.start() <= x.pos < end]
    return out


# --- LaTeX labels ---------------------------------------------------------------------

_ENV = re.compile(r"\\begin\s*\{\s*(figure|table|SCfigure|wrapfigure|sidewaysfigure|sidewaystable)\*?\s*\}(.*?)\\end\s*\{\s*\1\*?\s*\}", re.DOTALL)
_LABEL = re.compile(r"\\label\s*\{([^}]*)\}")
_REF = re.compile(r"\\(?:ref|cref|Cref|autoref|subref|figref|tabref|vref)\*?\s*\{([^}]*)\}")
_SUPP_SWITCH = re.compile(r"\\renewcommand\s*\{?\s*\\the(figure|table)\s*\}?\s*\{\s*S|\\beginsupplement\b|\\appendix\b")


def tex_mentions(source: str) -> list[Mention]:
    """Citations and definitions from a LaTeX source's labels and references."""
    switches = [(m.start(), m.group(1)) for m in _SUPP_SWITCH.finditer(source)]
    counters: dict[str, int] = {}
    labels: dict[str, tuple[str, int]] = {}
    spans: list[tuple[int, int]] = []
    out: list[Mention] = []
    for env in _ENV.finditer(source):
        kind = "table" if "table" in env.group(1) else "figure"
        supp = any(pos < env.start() and (what is None or what == kind) for pos, what in switches)
        series = ("supp_" if supp else "") + kind
        counters[series] = counters.get(series, 0) + 1
        spans.append((env.start(), env.end()))
        for label in _LABEL.findall(env.group(2)):
            labels[label.strip()] = (series, counters[series])
        out.append(Mention(series, counters[series], env.start(), True, env.group(1)))
    for ref in _REF.finditer(source):
        if any(a <= ref.start() < b for a, b in spans):
            continue
        for label in ref.group(1).split(","):
            target = labels.get(label.strip())
            if target:
                out.append(Mention(target[0], target[1], ref.start(), False, ref.group(0)))
    return sorted(out, key=lambda m: m.pos)


# --- order ------------------------------------------------------------------------------


def order_problems(found: list[Mention], series: str, defined_extra: set[int] | None = None) -> tuple[list[str], str]:
    """Out-of-order, uncited, and undefined items of one series, as sentences."""
    name = _NAMES[series]
    first: dict[int, int] = {}
    defined: set[int] = set(defined_extra or ())
    for m in found:
        if m.series != series:
            continue
        if m.caption:
            defined.add(m.number)
        elif m.number not in first:
            first[m.number] = m.pos
    problems: list[str] = []
    cited_order = sorted(first, key=first.get)
    seen: set[int] = set()
    known = defined | set(first)
    for number in cited_order:
        earlier = [n for n in known if n < number and n not in seen]
        if earlier:
            problems.append(f"{name} {number} is cited before {name} {min(earlier)}; number {name.lower()}s in the order the text first cites them")
            break
        seen.add(number)
    if defined:
        uncited = sorted(defined - set(first))
        if uncited:
            problems.append(f"{name} {', '.join(map(str, uncited))} {'is' if len(uncited) == 1 else 'are'} never cited in the text")
        missing = sorted(n for n in first if n not in defined)
        if missing:
            problems.append(f"the text cites {name} {', '.join(map(str, missing))}, but no such {name.lower()} was found (captions or files: {', '.join(map(str, sorted(defined)))})")
    detail = f"{len(first)} cited in order ({', '.join(map(str, cited_order[:12]))}{', ...' if len(cited_order) > 12 else ''})" if first and not problems else ""
    return problems, detail


def _attached_count(venue: Venue, values: dict[str, str], role: str) -> int:
    from .requirements_check import _fields_for, _paths

    return sum(len(_paths(f, values)) for f in _fields_for(venue, role))


def _supplement_captions(venue: Venue, values: dict[str, str]) -> list[Mention]:
    """Captions found in the supplementary files (to know which supplementary items exist)."""
    from .requirements_check import _existing, _fields_for, _paths

    out: list[Mention] = []
    for f in _fields_for(venue, "supplementary"):
        for path in _existing(_paths(f, values)):
            if path.suffix.lower() not in (".pdf", ".docx", ".tex"):
                continue
            text = document.get(path).text
            if not text:
                continue
            for m in mentions(text):
                if not m.caption:
                    continue
                # In the supplement, "Figure 3." captions supplementary figure 3.
                series = m.series if m.series.startswith(("supp_", "ed_")) else "supp_" + m.series
                out.append(Mention(series, m.number, m.pos, True, m.text))
    return out


def _manuscript_mentions(venue: Venue, values: dict[str, str]) -> tuple[list[Mention], str]:
    from .requirements_check import manuscript_files

    files = manuscript_files(venue, values)
    if not files:
        raise NotApplicable("no manuscript file to read")
    field, path = files[0]
    doc = document.get(path)
    if doc.is_tex and doc.pdf is None:
        if doc.tex_source is None:
            raise Unchecked(f"could not read the LaTeX source of {path.name}")
        return tex_mentions(doc.tex_source), field.id
    text = doc.text
    if text is None:
        raise Unchecked(f"could not read {path.name} as text")
    return mentions(text), field.id


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    from .report import NOT_APPLICABLE, UNCHECKED

    try:
        found, field_id = _manuscript_mentions(venue, values)
    except (NotApplicable, Unchecked) as exc:
        status = NOT_APPLICABLE if isinstance(exc, NotApplicable) else UNCHECKED
        for key, _name, title in SERIES[:4]:
            checks.record(f"citations.{key}", title, CITATIONS, status, detail=str(exc))
        _legend_checks(checks, venue, values, None, "")
        return
    found = found + _supplement_captions(venue, values)
    attached = {"figure": _attached_count(venue, values, "figures"), "table": _attached_count(venue, values, "tables")}
    for key, name, title in SERIES:
        present = any(m.series == key for m in found)
        if not present and key.startswith("ed_"):
            continue  # Extended Data is a Nature-family convention; silent elsewhere
        if not present and not attached.get(key):
            checks.record(f"citations.{key}", title, CITATIONS, NOT_APPLICABLE, detail=f"no {name.lower()}s cited or captioned")
            continue
        extra = set(range(1, attached[key] + 1)) if attached.get(key) and not any(m.series == key and m.caption for m in found) else None
        checks.run(f"citations.{key}", title, CITATIONS, _order_check, found, key, extra, field_id)
    _legend_checks(checks, venue, values, found, field_id)


def _order_check(found: list[Mention], series: str, extra: set[int] | None, field_id: str) -> Outcome:
    problems, detail = order_problems(found, series, extra)
    return Outcome([Issue(WARNING, field_id, p) for p in problems], detail)


# --- legends -------------------------------------------------------------------------------


def legends(text: str) -> list[tuple[int, int, str]]:
    """``(figure number, position, legend text)`` for each main-figure caption in ``text``."""
    out = []
    lines = text.split("\n")
    lengths = sorted(len(l) for l in lines if len(l) > 20)
    typical = lengths[len(lengths) // 2] if lengths else 80
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    starts = {m.pos: m for m in mentions(text) if m.caption and m.series == "figure"}
    for i, line in enumerate(lines):
        start = offsets[i]
        mention = next((m for p, m in starts.items() if start <= p < start + len(line) + 1), None)
        if mention is None:
            continue
        body = [line]
        for following in lines[i + 1 : i + 60]:
            if not following.strip() or _MENTION.match(following.strip()) and _CAPTION_TAIL.match(following.strip(), _MENTION.match(following.strip()).end()):
                break
            body.append(following)
            if following.rstrip().endswith(".") and len(following) < 0.6 * typical:
                break  # a short last line: the paragraph ends here
        out.append((mention.number, start, " ".join(s.strip() for s in body)))
    return out


def tex_legends(source: str) -> list[tuple[int, int, str]]:
    out = []
    number = 0
    for env in _ENV.finditer(source):
        if "table" in env.group(1):
            continue
        number += 1
        caption = re.search(r"\\caption\s*(?:\[[^\]]*\])?\s*\{", env.group(2))
        if not caption:
            continue
        depth, i, body = 1, caption.end(), env.group(2)
        while i < len(body) and depth:
            depth += {"{": 1, "}": -1}.get(body[i], 0)
            i += 1
        from .manuscript import _tex_to_text

        out.append((number, env.start(), _tex_to_text(body[caption.end() : i - 1])))
    return out


def _legend_checks(checks: Checks, venue: Venue, values: dict[str, str], found, field_id: str) -> None:
    from .report import NOT_APPLICABLE

    base = get_requirements(venue.slug)
    rules = resolve(base, values).figures if base is not None else None
    if rules is None or (not rules.legend_max_words and not rules.legends_location):
        checks.record("figures.legends", "Figure legends (length and location)", "Figures", NOT_APPLICABLE, detail="the guidelines set no legend length or location")
        return
    checks.run("figures.legends", "Figure legends (length and location)", "Figures", _legend_check, venue, values, rules)


def _legend_check(venue: Venue, values: dict[str, str], rules) -> Outcome:
    from .manuscript import _REFERENCE_HEADING
    from .requirements_check import manuscript_files

    files = manuscript_files(venue, values)
    if not files:
        raise NotApplicable("no manuscript file to read")
    field, path = files[0]
    doc = document.get(path)
    issues: list[Issue] = []
    if doc.is_tex and doc.pdf is None:
        source = doc.tex_source or ""
        found = tex_legends(source)
        refs = re.search(r"\\bibliography\b|\\begin\s*\{\s*thebibliography|\\printbibliography", source)
        refs_pos = refs.start() if refs else None
        at_end = "endfloat" in source
    else:
        text = doc.text
        if text is None:
            raise Unchecked(f"could not read {path.name} as text")
        found = legends(text)
        refs = _REFERENCE_HEADING.search(text)
        refs_pos = refs.start() if refs else None
        at_end = None
    if not found:
        raise Unchecked(f"found no figure legends ('Figure 1. ...') in {path.name}")
    if rules.legend_max_words:
        long = [(n, len(t.split())) for n, _, t in found if len(t.split()) > rules.legend_max_words]
        for number, words in long:
            issues.append(Issue(WARNING, field.id, f"the legend of Figure {number} is about {words} words; the author guidelines allow {rules.legend_max_words}"))
    location = rules.legends_location
    if location in ("end_of_manuscript", "after_references"):
        if at_end:
            pass
        elif refs_pos is None:
            issues.append(Issue(WARNING, field.id, f"could not find the reference list in {path.name}, so the legends' position was not checked", unchecked=True))
        else:
            early = [n for n, pos, _ in found if pos < refs_pos]
            if early:
                where = "after the references" if location == "after_references" else "at the end of the manuscript"
                issues.append(Issue(WARNING, field.id, f"the legend of Figure {early[0]} comes before the references in {path.name}; the author guidelines put figure legends {where}"))
    elif location == "with_figure" and refs_pos is not None and all(pos > refs_pos for _, pos, _ in found) and not (doc.is_tex and doc.pdf is None):
        issues.append(Issue(WARNING, field.id, f"all figure legends in {path.name} are collected after the references; the author guidelines put each legend with its figure"))
    longest = max(len(t.split()) for _, _, t in found)
    return Outcome(issues, f"{len(found)} legend(s), longest about {longest} words")
