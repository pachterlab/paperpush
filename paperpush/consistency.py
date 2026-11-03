"""Check that what the ``.sub`` says matches what the manuscript says.

Portals ask again for things the manuscript already contains -- the title, the
abstract, the authors, the keywords, the data-availability and competing-
interest statements -- and a copy that drifted from the paper (an abstract
pasted from an earlier draft, an author added to the paper but not the form)
is a common reason for an editor to send a submission back. For each such
``.sub`` value this module finds the matching passage in the manuscript and
compares them word for word, ignoring case, punctuation, and line breaks:

* **title** -- against the manuscript's front matter (and its running title);
* **abstract** and **statements** -- located anywhere in the manuscript text
  by their opening words, then aligned with :class:`difflib.SequenceMatcher`;
* **authors** -- each name's surname (and first name or initial) must appear
  in the front matter, in the same order as the ``.sub`` lists them; e-mail
  addresses and ORCID iDs the front matter prints must agree;
* **keywords** -- the manuscript's "Keywords:" line (or ``\\keywords``)
  against the ``.sub``'s list, as sets;
* **stated counts** -- a word count on the title page against the main text
  (and an abstract word count against the ``.sub``'s abstract), and a stated
  number of figures, tables, or display items against those the manuscript
  cites (see :func:`stated_counts` and :func:`main_text_scopes`).

An identical value passes; a near match is a WARNING listing the first few
differing words; a value the manuscript does not contain at all is a WARNING
too. The semantic version of this check, for fields like a cover-letter
summary, is the ``ai.consistency`` task (see :mod:`paperpush.ai_validate`).
"""

from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path

from . import document
from .database import Field, Venue
from .validate import WARNING, Checks, Issue, NotApplicable, Outcome, Unchecked, unchecked

logger = logging.getLogger(__name__)

CONSISTENCY = "Consistency"
# Below this similarity a .sub value counts as absent from the manuscript.
MIN_SIMILARITY = 0.85
# Shortest free-text value worth comparing, in words.
MIN_STATEMENT_WORDS = 6
# Free-text .sub fields that restate a manuscript section, by id pattern.
_STATEMENT_FIELDS = re.compile(r"data_availability(?!_links)|code_availability|competing_interest|conflict_of_interest|declaration_of_interest|author_contribution|acknowledg|funding_statement|ethics_statement|significance|author_summary|lay_summary")
_KEYWORDS = re.compile(r"(?:^|\n)\s*(?:key\s*words|keywords|index\s+terms)\s*[:.\u2014-]?\s*(.+(?:\n(?![A-Z][a-z]+\s*\n).+)?)", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ORCID = re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b")


def _manuscript(venue: Venue, values: dict[str, str]) -> tuple[Field, Path, document.Document]:
    from .requirements_check import manuscript_files

    files = manuscript_files(venue, values)
    if not files:
        raise NotApplicable("no manuscript file to compare with")
    field, path = files[0]
    return field, path, document.get(path)


def _text(doc: document.Document) -> str:
    text = doc.text
    if text is None:
        raise Unchecked(f"could not read {doc.path.name} as text")
    return text


def _front(doc: document.Document) -> str:
    """The front matter as plain text (LaTeX markup removed)."""
    from .manuscript import _tex_to_text, title_page_text

    front = title_page_text(doc.pdf if doc.is_tex and doc.pdf is not None else doc.path)
    if front is None:
        raise Unchecked(f"could not read the first pages of {doc.path.name}")
    if doc.is_tex and doc.pdf is None:
        front = _tex_to_text(front)
    return document.normalize(front)


def locate(needle: list[str], hay: list[str]) -> tuple[float, int | None, int]:
    """Best match of token list ``needle`` in ``hay``: ``(similarity, start, end)``.

    Candidate starts come from any 5-word run of the needle found in the hay
    (so a changed first word does not hide the passage); each candidate window
    is scored with :class:`difflib.SequenceMatcher`.
    """
    if not needle or not hay:
        return 0.0, None, 0
    # Short values (a title) anchor on 3-word runs at every offset, so two
    # changed words cannot hide them; long ones on every fifth 5-word run.
    k = min(3 if len(needle) < 30 else 5, len(needle))
    step = 1 if len(needle) < 60 else k
    index: dict[tuple[str, ...], list[int]] = {}
    for i in range(len(hay) - k + 1):
        index.setdefault(tuple(hay[i : i + k]), []).append(i)
    starts: set[int] = set()
    for offset in range(0, max(1, len(needle) - k + 1), step):
        for pos in index.get(tuple(needle[offset : offset + k]), [])[:20]:
            starts.add(max(0, pos - offset))

    def score(start: int, end: int) -> float:
        # Matched words over the longer of the two, so a window that drops a
        # differing word scores no better than one that keeps it.
        window = hay[start:end]
        matched = sum(b.size for b in difflib.SequenceMatcher(None, needle, window, autojunk=False).get_matching_blocks())
        return matched / max(len(needle), len(window))

    # Score each start at the needle's own length, then fit the end of the best
    # few: the manuscript's version may be a few words longer or shorter.
    ranked = sorted(((score(s, s + len(needle)), s) for s in sorted(starts)[:200]), reverse=True)[:3]
    best: tuple[float, int | None, int] = (0.0, None, 0)
    slack = max(3, len(needle) // 10)
    for _, start in ranked:
        # Nearest-length windows first, so a tie keeps the needle's own length.
        ends = sorted(range(max(start + 1, start + len(needle) - slack), min(len(hay), start + len(needle) + slack) + 1), key=lambda e: abs(e - start - len(needle)))
        for end in ends:
            ratio = score(start, end)
            if ratio > best[0]:
                best = (ratio, start, end)
    return best


def differences(needle: list[str], window: list[str], limit: int = 3) -> list[str]:
    """The first few differing word spans, as "'.sub: X' vs 'manuscript: Y'"."""
    out = []
    for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(None, needle, window, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        sub = " ".join(needle[a0:a1][:8]) or "(nothing)"
        ms = " ".join(window[b0:b1][:8]) or "(nothing)"
        out.append(f"'{sub}' in the .sub vs '{ms}' in the manuscript")
        if len(out) >= limit:
            break
    return out


def compare_passage(label: str, field_id: str, value: str, hay: list[str], where: str, min_similarity: float = MIN_SIMILARITY) -> tuple[list[Issue], str]:
    needle = document.tokens(value)
    ratio, start, end = locate(needle, hay)
    if start is not None and ratio >= 0.999:
        return [], f"{label} matches {where}"
    if start is None or ratio < min_similarity:
        closest = f" (closest passage {100 * ratio:.0f}% similar)" if start is not None else ""
        return [Issue(WARNING, field_id, f"{label} in the .sub was not found in {where}{closest}; paste the manuscript's text, or update the manuscript")], ""
    diffs = differences(needle, hay[start:end])
    return [Issue(WARNING, field_id, f"{label} in the .sub differs from {where} ({100 * ratio:.0f}% the same): {'; '.join(diffs)}")], ""


# --- the checks ----------------------------------------------------------------------


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    by_id = {f.id: f for f in venue.fields}
    checks.run("consistency.title", "Title in the .sub matches the manuscript", CONSISTENCY, _title_check, venue, values, by_id)
    checks.run("consistency.abstract", "Abstract in the .sub matches the manuscript", CONSISTENCY, _abstract_check, venue, values, by_id)
    checks.run("consistency.authors", "Authors in the .sub match the manuscript (names, order, emails, ORCIDs)", CONSISTENCY, _authors_check, venue, values)
    checks.run("consistency.keywords", "Keywords in the .sub match the manuscript", CONSISTENCY, _keywords_check, venue, values, by_id)
    checks.run("consistency.statements", "Statements in the .sub match the manuscript", CONSISTENCY, _statements_check, venue, values)
    checks.run("consistency.word_counts", "Word counts stated on the title page match the manuscript", CONSISTENCY, _word_counts_check, venue, values)
    checks.run("consistency.display_counts", "Figure and table counts stated on the title page match the manuscript", CONSISTENCY, _display_counts_check, venue, values)


def _title_check(venue: Venue, values: dict[str, str], by_id: dict[str, Field]) -> Outcome:
    title = values.get("title", "").strip()
    if not title:
        raise NotApplicable("no title in the .sub")
    field, path, doc = _manuscript(venue, values)
    front = document.tokens(_front(doc))
    issues, detail = compare_passage("the title", "title", title, front, f"the first pages of {path.name}", min_similarity=0.5)
    found = manuscript_title(doc)
    if issues and found and document.tokens(found) != document.tokens(title):
        issues[0] = Issue(WARNING, "title", issues[0].message + f"; the manuscript's title reads '{found}'")
    for fid in ("running_title", "short_title"):
        short = values.get(fid, "").strip()
        if short and fid in by_id:
            match = re.search(r"(?:running|short)\s+(?:title|head)\s*[:.\u2014-]\s*(.+)", _front(doc), re.IGNORECASE)
            if match and document.tokens(match.group(1)) != document.tokens(short):
                issues.append(Issue(WARNING, fid, f"{by_id[fid].label} in the .sub ('{short}') differs from the manuscript's ('{match.group(1).strip()}')"))
    return Outcome(issues, detail)


def manuscript_title(doc: document.Document) -> str | None:
    """The manuscript's own title, when it can be picked out of the file."""
    from .manuscript import _tex_to_text

    if doc.tex_source:
        m = re.search(r"\\title\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{[^{}]*\})*)\}", doc.tex_source)
        if m:
            return re.sub(r"\s+", " ", _tex_to_text(m.group(1))).strip()
    if doc.is_docx:
        xml = doc.docx_part("word/document.xml") or ""
        m = re.search(r'<w:pStyle w:val="Title"/>.*?</w:p>', xml, re.DOTALL)
        if m:
            return re.sub(r"<[^>]+>", "", m.group(0)).strip() or None
    pages = doc.glyph_pages
    if pages:
        glyphs = [g for g in pages[0].glyphs if g.text.strip() and not g.in_figure]
        if glyphs:
            from collections import Counter

            body = Counter(round(g.size, 1) for g in glyphs).most_common(1)[0][0]
            big = max(g.size for g in glyphs)
            if big < 1.15 * body:
                return None  # no display-size title on page 1
            rows: dict[float, list] = {}
            for g in glyphs:
                if g.size >= big - 0.5:
                    rows.setdefault(round(g.y0), []).append(g)
            from .pdf_layout import join_glyphs

            lines = [join_glyphs(sorted(row, key=lambda g: g.x0)) for _, row in sorted(rows.items(), reverse=True)]
            text = " ".join(lines).strip()
            return text if 2 <= len(text.split()) <= 40 else None
    return None


def _abstract_check(venue: Venue, values: dict[str, str], by_id: dict[str, Field]) -> Outcome:
    fields = [fid for fid in ("abstract",) if values.get(fid, "").strip()]
    if not fields:
        raise NotApplicable("no abstract in the .sub")
    _, path, doc = _manuscript(venue, values)
    text = _text(doc)
    if doc.is_tex and doc.pdf is None and doc.tex_source:
        from .manuscript import _tex_to_text

        m = re.search(r"\\begin\s*\{\s*abstract\s*\}(.*?)\\end\s*\{\s*abstract\s*\}", doc.tex_source, re.DOTALL)
        if m:
            text = _tex_to_text(m.group(1))
    issues, detail = compare_passage("the abstract", "abstract", values["abstract"], document.tokens(text), path.name)
    return Outcome(issues, detail)


def _authors_check(venue: Venue, values: dict[str, str]) -> Outcome:
    from .validate import _author_name, parse_authors

    field = next((f for f in venue.fields if f.type == "authorlist" and values.get(f.id, "").strip()), None)
    if field is None:
        raise NotApplicable("no author list in the .sub")
    authors = parse_authors(values[field.id], field.fields)
    names = [(a, _author_name(a)) for a in authors if _author_name(a)]
    if not names:
        raise NotApplicable("the .sub's authors carry no names (OpenReview ids only)")
    _, path, doc = _manuscript(venue, values)
    if venue.anonymous:
        raise NotApplicable("the venue is double-blind: the manuscript must not name its authors")
    front_raw = _front(doc)
    front = re.sub(r"\s+", " ", document.fold(front_raw))
    issues: list[Issue] = []
    positions: list[tuple[str, int]] = []
    missing: list[str] = []
    for author, name in names:
        parts = [p for p in re.split(r"\s+", document.fold(name).replace(".", ". ")) if p]
        surname = parts[-1]
        given = parts[0] if len(parts) > 1 else ""
        found = None
        for m in re.finditer(rf"(?<![a-z]){re.escape(surname)}(?![a-z])", front):
            before = front[max(0, m.start() - 45) : m.start()]
            after = front[m.end() : m.end() + 30]
            if not given or re.search(rf"(?<![a-z]){re.escape(given[0])}", before + " " + after):
                found = m.start()
                break
        if found is None:
            missing.append(name)
        else:
            positions.append((name, found))
    if missing:
        issues.append(Issue(WARNING, field.id, f"author(s) {', '.join(missing)} in the .sub not found on the first pages of {path.name}"))
    for (first, i), (second, j) in zip(positions, positions[1:]):
        if j < i:
            issues.append(Issue(WARNING, field.id, f"the .sub lists {first} before {second}, but the manuscript lists {second} first; keep the author order the same"))
            break
    emails = {e.lower() for e in _EMAIL.findall(front_raw)}
    orcids = set(_ORCID.findall(front_raw))
    for author, name in names:
        email = str(author.get("email", "")).strip().lower()
        if email and emails and author.get("corresponding") and email not in emails:
            issues.append(Issue(WARNING, field.id, f"the corresponding author {name}'s e-mail in the .sub ({email}) is not among those in the manuscript ({', '.join(sorted(emails)[:3])})"))
        orcid = re.sub(r"^https?://orcid\.org/", "", str(author.get("orcid", "")).strip())
        if orcid and orcids and orcid not in orcids:
            issues.append(Issue(WARNING, field.id, f"{name}'s ORCID iD in the .sub ({orcid}) does not appear in the manuscript, which lists {', '.join(sorted(orcids)[:3])}"))
    return Outcome(issues, f"{len(positions)} of {len(names)} authors found, in order" if not issues else "")


def manuscript_keywords(doc: document.Document) -> list[str] | None:
    source = doc.tex_source or ""
    m = re.search(r"\\keywords\s*\{([^}]*)\}|\\begin\s*\{\s*(?:keyword|keywords|IEEEkeywords)\s*\}(.*?)\\end", source, re.DOTALL)
    if m:
        raw = m.group(1) or m.group(2) or ""
        return [k for k in (re.sub(r"\\[a-zA-Z]+", "", x).strip() for x in re.split(r"\\sep|[,;]", raw)) if k]
    text = doc.text
    if not text:
        return None
    m = _KEYWORDS.search(text)
    if not m:
        return None
    line = m.group(1).split("\n")[0] if "\n" in m.group(1) and not re.search(r"[,;\u00b7\u2022]\s*$", m.group(1).split("\n")[0]) else m.group(1)
    return [k.strip(" .") for k in re.split(r"[,;\u00b7\u2022|]", line.replace("\n", " ")) if k.strip(" .")]


def _keywords_check(venue: Venue, values: dict[str, str], by_id: dict[str, Field]) -> Outcome:
    fid = next((f for f in ("keywords",) if values.get(f, "").strip()), None)
    if fid is None:
        raise NotApplicable("no keywords in the .sub")
    _, path, doc = _manuscript(venue, values)
    found = manuscript_keywords(doc)
    if not found:
        raise NotApplicable(f"{path.name} prints no keyword list to compare with")
    sub = [k.strip() for k in re.split(r"[,;\n]", values[fid]) if k.strip()]
    norm = lambda k: " ".join(document.tokens(k))  # noqa: E731
    sub_set, ms_set = {norm(k) for k in sub}, {norm(k) for k in found}
    issues = []
    only_sub = [k for k in sub if norm(k) not in ms_set]
    only_ms = [k for k in found if norm(k) not in sub_set]
    if only_sub or only_ms:
        parts = []
        if only_sub:
            parts.append(f"only in the .sub: {', '.join(only_sub)}")
        if only_ms:
            parts.append(f"only in the manuscript: {', '.join(only_ms)}")
        issues.append(Issue(WARNING, fid, f"keywords differ between the .sub and {path.name} ({'; '.join(parts)})"))
    return Outcome(issues, f"{len(sub)} keywords match")


def _statements_check(venue: Venue, values: dict[str, str]) -> Outcome:
    fields = [f for f in venue.fields if f.type in ("text", "textarea") and not f.fields and _STATEMENT_FIELDS.search(f.id) and len(values.get(f.id, "").split()) >= MIN_STATEMENT_WORDS]
    if not fields:
        raise NotApplicable("no statement in the .sub long enough to compare")
    _, path, doc = _manuscript(venue, values)
    hay = document.tokens(_text(doc))
    issues: list[Issue] = []
    matched = []
    for f in fields:
        found, detail = compare_passage(f.label, f.id, values[f.id], hay, path.name)
        issues.extend(found)
        if detail:
            matched.append(f.label)
    return Outcome(issues, f"{len(matched)} statement(s) match: {', '.join(matched)}" if matched else "")


# --- counts stated on the title page ---------------------------------------------------
#
# Title pages often state the manuscript's word count and its number of figures
# and tables, and those numbers go stale with every revision. Each stated count
# is compared with the manuscript itself.
#
# Word counts are measured from where the main text starts (the Introduction,
# a venue's ``main_text_start_headings``, or else the end of the abstract) to
# each place the venue's count could plausibly stop: the first back-matter
# heading (acknowledgements, declarations, figure legends), a Methods section
# placed after the results, or the reference list. Venues and word processors
# count differently, so a stated count passes when any of those scopes, or the
# whole document, is within WORD_COUNT_TOLERANCE of it (and at least
# WORD_COUNT_SLACK words).

WORD_COUNT_TOLERANCE = 0.05
WORD_COUNT_SLACK = 50
ABSTRACT_COUNT_SLACK = 10

_NUMBER_WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
_COUNT = re.compile(
    rf"(?<![\w.,])(\d{{1,3}}(?:,\d{{3}})+|\d+|{'|'.join(_NUMBER_WORDS)})(?![\w]|[.,]\d)",
    re.IGNORECASE,
)
# A stated limit or a range ("Word limit: 3,000", "150-250 words") is not a count.
_LIMIT_WORDS = re.compile(
    r"\blimit|\bmax(?:imum)?\b|\bup\s+to\b|\bat\s+most\b|\bno\s+more\s+than\b|\bnot\s+exceed|[<\u2264]",
    re.IGNORECASE,
)
_RANGE = re.compile(r"^\s*(?:[\u2013-]|to)\s*\d")
# Word counts of parts this check does not measure.
_UNMEASURED_WORD_COUNT = re.compile(
    r"legend|caption|method|reference|title|character|summary|significance|highlight|keyword|supplement",
    re.IGNORECASE,
)
_SEGMENT_SPLIT = re.compile(r"\n|;|\||\u00b7|\u2022")
_PARENTHETICAL = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")
_DISPLAY_NOUNS = (
    ("display_items", r"display\s+items?"),
    ("figures", r"(?:main(?:\s+text)?\s+)?(?:figures?|figs?\.?)"),
    ("tables", r"(?:main(?:\s+text)?\s+)?tables?"),
)
_START_HEADINGS = ("Introduction", "Background", "Main")
_BACK_MATTER_HEADINGS = (
    "Acknowledgements", "Acknowledgments", "Acknowledgement", "Acknowledgment", "Author contributions", "Contributions",
    "Competing interests", "Competing interest", "Conflicts of interest", "Conflict of interest", "Declaration of interests",
    "Declarations", "Data availability", "Code availability", "Data and code availability", "Funding", "Figure legends",
    "Figure captions", "Figure titles and legends", "Supplementary information", "Supplementary material", "Supplementary materials",
    "Ethics statement", "Additional information", "Disclosures", "Author information",
)  # fmt: skip
_METHODS_HEADINGS = (
    "Methods",
    "Materials and methods",
    "Online methods",
    "Experimental procedures",
    "STAR Methods",
    "Method details",
)
_RESULTS_HEADINGS = (
    "Results",
    "Discussion",
    "Results and discussion",
    "Conclusion",
    "Conclusions",
)


def _count_value(token: str) -> int:
    token = token.lower()
    return _NUMBER_WORDS[token] if token in _NUMBER_WORDS else int(token.replace(",", ""))


def _tidy(quote: str) -> str:
    return re.sub(r"\s+([:=])", r"\1", re.sub(r"\s+", " ", quote)).strip()


def stated_counts(front: str) -> list[tuple[str, int, str]]:
    """Counts the title page states, as ``(kind, number, text)``.

    ``kind`` is ``"abstract"`` or ``"text"`` for a word count, ``"figures"``,
    ``"tables"``, or ``"display_items"`` for a count of display items. Each
    number is labelled by the words before it on its line (since the previous
    number), with parenthetical remarks set aside: "Word count (excluding
    Methods): 3,500" is a main-text count. Stated limits ("word limit: 3000"),
    ranges, and counts of parts not measured here (legends, methods,
    references, the title, supplementary items) are left out.
    """
    out: list[tuple[str, int, str]] = []
    for segment in _SEGMENT_SPLIT.split(front):
        text = re.sub(r"\s+", " ", segment).strip()
        bare = _PARENTHETICAL.sub(" ", text)
        if not bare or len(bare) > 200:
            continue
        previous = 0
        for m in _COUNT.finditer(bare):
            left, right = bare[previous : m.start()], bare[m.end() :]
            previous = m.end()
            label_end = re.split(r"[,.]\s", left)[-1]
            if _LIMIT_WORDS.search(label_end) or _RANGE.match(right) or re.search(r"(?:[\u2013-]|\bto)\s*$", left):
                continue
            number = _count_value(m.group(1))
            words_after = re.match(r"\s*words?\b", right, re.IGNORECASE)
            if words_after or re.search(r"\bwords?\b|word\s*count", label_end, re.IGNORECASE) and re.search(r"[:=]\s*$", left):
                quote = _tidy(label_end + m.group(0) + (words_after.group(0) if words_after else ""))
                if re.search(r"\babstract\b", label_end, re.IGNORECASE):
                    out.append(("abstract", number, quote))
                elif not _UNMEASURED_WORD_COUNT.search(label_end):
                    out.append(("text", number, quote))
                continue
            for kind, noun in _DISPLAY_NOUNS:
                before = re.search(rf"(?:number\s+of\s+)?\b{noun}\s*[:=]\s*$", left, re.IGNORECASE)
                after = re.match(rf"\s+{noun}(?!\s*\d)", right, re.IGNORECASE)
                if before or after:
                    if re.search(
                        r"supplement|extended\s+data|appendix",
                        label_end + (right[: after.end()] if after else ""),
                        re.IGNORECASE,
                    ):
                        break
                    out.append(
                        (
                            kind,
                            number,
                            _tidy(label_end + m.group(0) + (after.group(0) if after else "")),
                        )
                    )
                    break
    return out


def _heading_positions(text: str, headings: tuple[str, ...]) -> list[int]:
    from .manuscript import _extra_heading_pattern

    pattern = _extra_heading_pattern(headings)
    return [] if pattern is None else [m.start() for m in pattern.finditer(text)]


def _abstract_end(text: str, abstract: str) -> int | None:
    """Where the ``.sub``'s abstract ends in ``text`` (by its last few words), or None."""
    words = re.findall(r"[A-Za-z0-9]+", abstract)[-8:]
    if len(words) < 4:
        return None
    match = re.search(r"\W+".join(re.escape(w) for w in words) + r"\S*", text, re.IGNORECASE)
    return match.end() if match else None


def main_text_scopes(
    text: str,
    start_headings: tuple[str, ...],
    end_headings: tuple[str, ...],
    abstract: str = "",
) -> dict[str, int] | None:
    """Word counts of the main text under each plausible scope, by description.

    None when the start of the main text cannot be found.
    """
    from .manuscript import _REFERENCE_HEADING, main_text_end

    starts = _heading_positions(text, start_headings or _START_HEADINGS)
    start_label = "the Introduction" if not start_headings else f"'{start_headings[0]}'"
    if starts:
        start = starts[0]
    else:
        start = _abstract_end(text, abstract) if abstract else None
        start_label = "the end of the abstract"
        if start is None:
            return None
    body = text[start:]
    ends: dict[str, int] = {}
    venue_end = main_text_end(body, end_headings)
    if venue_end is not None:
        ends[("the references" if _REFERENCE_HEADING.match(venue_end.group(0)) else f"'{venue_end.group(0).strip()}'")] = venue_end.start()
    back = _heading_positions(body, _BACK_MATTER_HEADINGS)
    if back:
        ends["the back matter (acknowledgements, declarations, legends)"] = back[0]
    results = _heading_positions(body, _RESULTS_HEADINGS)
    methods = [m for m in _heading_positions(body, _METHODS_HEADINGS) if results and m > results[0]]
    if methods:
        ends["Methods"] = methods[0]
    if not ends:
        ends["the end of the document"] = len(body)
    return {f"from {start_label} to {where}": len(body[:end].split()) for where, end in ends.items()}


def _title_page_source(venue: Venue, values: dict[str, str]) -> tuple[Field, Path, str]:
    """The separate title page if one is attached, else the manuscript's front matter."""
    from .requirements_check import _existing, _paths, manuscript_files

    for f in venue.fields:
        if "title_page" in f.id and f.type in ("file", "filelist"):
            for path in _existing(_paths(f, values)):
                text = document.get(path).text
                if text is not None:
                    return f, path, document.normalize(text)
    files = manuscript_files(venue, values)
    if not files:
        raise NotApplicable("no manuscript or title-page file to read")
    field, path = files[0]
    return field, path, _front(document.get(path))


def _word_counts_check(venue: Venue, values: dict[str, str]) -> Outcome:
    from .requirements import get_requirements, resolve
    from .requirements_check import _fields_for, manuscript_files

    tp_field, tp_path, front = _title_page_source(venue, values)
    stated = [(kind, n, text) for kind, n, text in stated_counts(front) if kind in ("text", "abstract")]
    if not stated:
        raise NotApplicable(f"no word count stated on the title page of {tp_path.name}")
    issues: list[Issue] = []
    details: list[str] = []
    abstract = next(
        (values.get(f.id, "").strip() for f in _fields_for(venue, "abstract") if values.get(f.id, "").strip()),
        "",
    )
    for kind, n, text in stated:
        if kind != "abstract":
            continue
        if not abstract:
            issues.append(
                unchecked(
                    tp_field.id,
                    f"the title page states '{text}', but the .sub has no abstract to count",
                )
            )
            continue
        words = len(abstract.split())
        if abs(words - n) > max(WORD_COUNT_TOLERANCE * n, ABSTRACT_COUNT_SLACK):
            issues.append(
                Issue(
                    WARNING,
                    tp_field.id,
                    f"the title page of {tp_path.name} states '{text}', but the abstract in the .sub is {words} words; update the stated count",
                )
            )
        else:
            details.append(f"abstract: {n} stated, {words} counted")
    text_counts = [(n, text) for kind, n, text in stated if kind == "text"]
    if text_counts:
        files = manuscript_files(venue, values)
        if not files:
            raise NotApplicable("no manuscript file to count")
        _, path = files[0]
        full = _text(document.get(path))
        base = get_requirements(venue.slug)
        rules = resolve(base, values).manuscript if base is not None else None
        start_headings = tuple((rules.main_text_start_headings if rules else None) or ())
        end_headings = tuple((rules.main_text_end_headings if rules else None) or ())
        scopes = main_text_scopes(full, start_headings, end_headings, abstract)
        if scopes is None:
            issues.append(
                unchecked(
                    tp_field.id,
                    f"could not find where the main text of {path.name} starts (no Introduction heading, and the .sub's abstract was not found); the stated word count was not checked",
                )
            )
        else:
            scopes = {**scopes, "in the whole document": len(full.split())}
            for n, text in text_counts:
                where, words = min(scopes.items(), key=lambda kv: abs(kv[1] - n))
                if abs(words - n) <= max(WORD_COUNT_TOLERANCE * n, WORD_COUNT_SLACK):
                    details.append(f"main text: {n} stated, about {words} counted {where}")
                    continue
                main_where, main_words = next(iter(scopes.items()))
                shown = ", ".join(f"{w} {d}" for d, w in list(scopes.items())[:3])
                issues.append(
                    Issue(
                        WARNING,
                        tp_field.id,
                        f"the title page of {tp_path.name} states '{text}', but {path.name} has about {main_words} words {main_where} ({100 * (main_words - n) / max(n, 1):+.0f}%; counted: {shown}); update the stated count",
                    )
                )
    return Outcome(issues, "; ".join(details))


def _display_counts_check(venue: Venue, values: dict[str, str]) -> Outcome:
    from .citation_check import _attached_count, _manuscript_mentions

    tp_field, tp_path, front = _title_page_source(venue, values)
    stated = [(kind, n, text) for kind, n, text in stated_counts(front) if kind in ("figures", "tables", "display_items")]
    if not stated:
        raise NotApplicable(f"no figure or table count stated on the title page of {tp_path.name}")
    found, _ = _manuscript_mentions(venue, values)
    actual: dict[str, set[int]] = {}
    for kind, series, role in (
        ("figures", "figure", "figures"),
        ("tables", "table", "tables"),
    ):
        cited = {m.number for m in found if m.series == series}
        counts = {max(cited)} if cited else set()
        attached = _attached_count(venue, values, role)
        if attached:
            counts.add(attached)
        if not counts and not found:
            raise Unchecked(f"found no figure or table citations in the manuscript to count")
        actual[kind] = counts or {0}
    actual["display_items"] = {f + t for f in actual["figures"] for t in actual["tables"]}
    issues: list[Issue] = []
    details: list[str] = []
    names = {
        "figures": "main figures",
        "tables": "main tables",
        "display_items": "display items (figures and tables)",
    }
    for kind, n, text in stated:
        if n in actual[kind]:
            details.append(f"{n} {names[kind]}")
            continue
        counted = " or ".join(str(c) for c in sorted(actual[kind]))
        issues.append(
            Issue(
                WARNING,
                tp_field.id,
                f"the title page of {tp_path.name} states '{text}', but the manuscript has {counted} {names[kind]}; update the stated count",
            )
        )
    return Outcome(
        issues,
        (("stated counts match: " + ", ".join(details)) if details and not issues else ""),
    )
