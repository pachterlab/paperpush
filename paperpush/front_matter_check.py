"""Check the abstract and the contact details on the title page.

Three slips editors return a manuscript for, checked for every venue:

* ``front_matter.abstract_citations`` -- the abstract cites references. An
  abstract is indexed and read on its own, away from the reference list, so a
  citation in it points at nothing; most journals forbid them outright. Both
  the ``.sub``'s abstract and the manuscript's own are read: in LaTeX source a
  ``\\cite`` (or ``\\citep``, ``\\parencite``, ...); in text a bracketed
  number ("[3]", "[2, 5-7]") or an author-year citation ("(Smith et al.,
  2020)", "Smith et al. (2020)").
* ``front_matter.structured_abstract`` -- when the venue's author guidelines
  ask for a structured abstract (``abstract.structured`` in
  ``manuscript_requirements.json``), each required heading must appear *as a
  label* -- at the start of the abstract or of a sentence or line, followed
  by a colon, full stop, dash, or line break ("Background: ...",
  "Results. ..."). A heading word used in the prose ("the results show")
  does not count. Singular and plural forms are interchangeable, and a
  heading with alternatives ("Intervention(s) or Exposure(s)") is satisfied
  by any of them.
* ``front_matter.contact_email`` -- the manuscript gives at least one e-mail
  address, so a reader can reach the corresponding author. Looked for on the
  title page (a separate title-page upload, else the manuscript's first
  pages), then anywhere in the manuscript. Not applicable at a double-blind
  venue, where the manuscript must not identify its authors; deferred to
  ``guidelines.title_page`` when the venue's guidelines already require the
  corresponding author's e-mail there.

Every finding is a WARNING: the abstract is located heuristically in extracted
text.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from . import document
from .database import Field, Venue
from .validate import WARNING, Checks, Issue, NotApplicable, Outcome, Unchecked, unchecked

logger = logging.getLogger(__name__)

FRONT_MATTER = "Abstract and front matter"
MAX_EXAMPLES = 3

# --- citations -------------------------------------------------------------------

# A LaTeX citation command (natbib, biblatex, plain), up to its opening brace.
TEX_CITE = re.compile(
    r"\\(?:[Cc]ite[a-z]*|[Pp]arencite|[Tt]extcite|[Aa]utocite|[Ff]ootcite|[Ss]upercite|[Ff]ullcite|[Ss]martcite)\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^}]*)\}"
)
# "[3]", "[2, 5-7]", "[12–14]": whole numbers from 1, no decimals (so "[0, 1]" is an interval, not a citation).
_BRACKET = re.compile(r"\[\s*([1-9]\d{0,3}(?:\s*[,\u2013\u2014-]\s*[1-9]\d{0,3})*)\s*\]")
# A surname: capitalised, with a lower-case letter early on (so "NIPS 2017" or "WMT 2014" is not one).
_NOT_A_NAME = r"(?!(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec|Spring|Summer|Autumn|Fall|Winter)\b)"
_NAME = _NOT_A_NAME + r"[A-Z](?:['\u2019][A-Z]|[a-z]?[A-Z])?[a-z\u00e0-\u017f][A-Za-z'\u2019\u00c0-\u017f-]*"
_YEAR = r"(?:19|20)\d{2}[a-z]?"
# "(Smith et al., 2020)", "(Smith and Jones 2019; Lee 2021)", "Smith et al. (2020)", "Smith et al., 2020".
_AUTHOR_YEAR = re.compile(
    rf"\(\s*(?:e\.g\.,?\s*|see\s+(?:also\s+)?)?{_NAME}(?:,\s*{_NAME})*(?:,?\s+(?:et\s+al\.?|and\s+{_NAME}|&\s*{_NAME}))?,?\s+{_YEAR}(?:[^()]{{0,60}}?\))?"
    rf"|\b{_NAME}\s+et\s+al\.?,?\s*\(?{_YEAR}"
    rf"|\b{_NAME}\s+(?:and|&)\s+{_NAME}\s+\({_YEAR}\)"
    # "Smith (2020) showed", not "the Global Burden of Disease (2019)" or "the UK Census (2020)".
    rf"|(?<!\bof\s)(?<!\bthe\s)(?<!\bin\s)(?<![A-Z]\s)\b{_NAME}\s+\({_YEAR}\)"
)
# Pandoc's "[@smith2020; @lee2021]".
_PANDOC_CITE = re.compile(r"\[\s*(?:see\s+)?-?@[\w:.#$%&+?<>~/-]+(?:\s*;\s*-?@[\w:.#$%&+?<>~/-]+)*\s*\]")
# Before a bracket: "95% CI [8, 16]". After it: "[3,3]-sigmatropic", "[1,2,4]triazole".
_INTERVAL_BEFORE = re.compile(r"(?:\b(?:CI|CrI|IQR|intervals?|ranges?|between)|%)\s*[:=,]?\s*$", re.IGNORECASE)
_NOMENCLATURE_AFTER = re.compile(r"-?[A-Za-z]")


def citations_in(text: str, *, tex: bool = False) -> list[str]:
    """The citations written in ``text``, as they appear (each once)."""
    found: list[str] = []
    if tex:
        found += [m.group(0) for m in TEX_CITE.finditer(text)]
    found += [m.group(0) for m in _BRACKET.finditer(text) if not (_INTERVAL_BEFORE.search(text[max(0, m.start() - 20) : m.start()]) or _NOMENCLATURE_AFTER.match(text, m.end()))]
    found += [m.group(0) for m in _PANDOC_CITE.finditer(text)]
    found += [m.group(0) for m in _AUTHOR_YEAR.finditer(text)]
    return list(dict.fromkeys(f.strip() for f in found))


# --- the manuscript's abstract -------------------------------------------------------

_TEX_ABSTRACT = re.compile(r"\\begin\s*\{\s*abstract\s*\}(.*?)\\end\s*\{\s*abstract\s*\}|\\abstract\s*\{((?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*)\}", re.DOTALL)
_ABSTRACT_HEADING = re.compile(r"^[ \t]*(?:\d+\.?\s+)?(?:abstract|summary(?![ \t]+(?:statement|points?|box)\b))[ \t]*[:.\u2014-]?[ \t]*(?:\n|(?=(?-i:[A-Z])))", re.IGNORECASE | re.MULTILINE)
# What ends the abstract in extracted text: the next part of the front matter or the main text.
_ABSTRACT_END = re.compile(
    r"^[ \t]*(?:\d+\.?[ \t]+|[IVX]+\.[ \t]+)?(?:introduction|key\s*words|keywords|index\s+terms|main(?:\s+text)?|significance(?:\s+statement)?|author\s+summary|"
    r"graphical\s+abstract|highlights|background\s+and\s+summary|one[- ]sentence\s+summary|teaser)\b[ \t]*[:.\u2014-]?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
# A line of its own naming a part of a structured abstract or of the paper: the two share "Methods" and "Results".
_ABSTRACT_LABEL = re.compile(
    r"^[ \t]*(?:\d+\.?[ \t]+|[IVX]+\.[ \t]+)?(materials\s+and\s+methods|methods\s+and\s+materials|results\s+and\s+discussion|methods?|results?|discussions?|conclusions?(?:\s+and\s+relevance)?|"
    r"background|context|importance|objectives?|purpose|aims?|rationale|design|setting|participants|interventions?|exposures?|measurements|main\s+outcomes?(?:\s+and\s+measures?)?|findings|interpretation|relevance|limitations)\b[ \t]*[:.\u2014-]?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_BODY_SECTIONS = frozenset({"methods", "results", "discussion", "conclusion"})
# Fewer words than this before the first label is not prose: a structured abstract opens with its first label.
_PROSE_LEAD_WORDS = 4
# Longest stretch read as the abstract when nothing marks its end.
_ABSTRACT_MAX_CHARS = 4000


def _canonical_label(label: str) -> str:
    """"Materials and Methods" and "Method" -> "methods"; "Results and discussion" -> "results"; "Conclusions" -> "conclusion"."""
    words = re.sub(r"\s+", " ", label.lower())
    for name in ("results", "methods", "discussion", "conclusion"):
        if name.rstrip("s") in words:
            return name
    return words


def _end_at_label(rest: str) -> int | None:
    """Where a section label in ``rest`` ends the abstract, or None.

    Prose before the first label means an unstructured abstract, so the first
    section of the paper ends it. A structured abstract opens with a label, and
    ends at a label it has already used or a section of the paper after its
    conclusions.
    """
    labels = [(_canonical_label(m.group(1)), m.start()) for m in _ABSTRACT_LABEL.finditer(rest)]
    if not labels:
        return None
    if len(rest[: labels[0][1]].split()) >= _PROSE_LEAD_WORDS:
        return next((pos for name, pos in labels if name in _BODY_SECTIONS), None)
    seen: set[str] = set()
    for name, pos in labels:
        if name in seen or (name in _BODY_SECTIONS and "conclusion" in seen):
            return pos
        seen.add(name)
    return None


def _end_at_marked_heading(rest: str, doc: document.Document) -> int | None:
    """Where the next heading the file marks at the abstract's level or above starts in ``rest``, or None.

    Section labels are left to :func:`_end_at_label`, since a structured
    abstract's may be marked at the same level as "Abstract".
    """
    from .manuscript import cased_headings, normalize_heading

    if not (doc.is_markdown or doc.is_docx):
        return None
    marked = [(int(level[1:]), normalize_heading(text)) for level, text in cased_headings(doc.path) or [] if re.fullmatch(r"h\d", level)]
    start = next((i for i, (_, text) in enumerate(marked) if text in ("abstract", "summary")), None)
    if start is None:
        return None
    following = next((text for level, text in marked[start + 1 :] if level <= marked[start][0]), None)
    if following is None or _ABSTRACT_LABEL.fullmatch(following):
        return None
    return next((m.start() for m in re.finditer(r"^.*$", rest, re.MULTILINE) if normalize_heading(m.group(0)) == following), None)


def _end_without_introduction(rest: str, doc: document.Document, sub_abstract: str) -> int:
    """Where the abstract ends in ``rest``, the text after its heading, when no Introduction or similar heading ends it."""
    ends = [_end_at_label(rest), _end_at_marked_heading(rest, doc)]
    words = re.findall(r"[A-Za-z0-9]+", sub_abstract)
    if len(words) >= 8:
        last = re.search(r"\W+".join(re.escape(w) for w in words[-6:]) + r"\S*", rest, re.IGNORECASE)
        ends.append(last.end() if last else None)
    return min((end for end in ends if end is not None), default=len(rest))


def manuscript_abstract(doc: document.Document, sub_abstract: str = "") -> tuple[str, bool] | None:
    """The manuscript's abstract and whether it is LaTeX source, or None if it cannot be found.

    LaTeX source: the ``abstract`` environment (or ``\\abstract{...}``), kept
    raw so ``\\cite`` can be seen. Otherwise the text after an "Abstract"
    heading, up to the Introduction, the keywords line, or a similar heading
    or, with none of those, the end :func:`_end_without_introduction` finds;
    failing a heading, the passage the ``.sub``'s abstract opens and closes with.
    """
    if doc.is_tex and doc.tex_source:
        m = _TEX_ABSTRACT.search(doc.tex_source)
        if m:
            return (m.group(1) or m.group(2) or ""), True
    text = doc.text
    if text is None:
        return None
    heading = _ABSTRACT_HEADING.search(text)
    if heading is not None:
        rest = text[heading.end() : heading.end() + _ABSTRACT_MAX_CHARS]
        end = _ABSTRACT_END.search(rest)
        return rest[: end.start() if end else _end_without_introduction(rest, doc, sub_abstract)], False
    words = re.findall(r"[A-Za-z0-9]+", sub_abstract)
    if len(words) >= 8:
        start = re.search(r"\W+".join(re.escape(w) for w in words[:6]), text, re.IGNORECASE)
        if start is not None:
            end = re.search(r"\W+".join(re.escape(w) for w in words[-6:]) + r"\S*", text[start.start() :], re.IGNORECASE)
            if end is not None:
                return text[start.start() : start.start() + end.end()], False
    return None


def _manuscript(venue: Venue, values: dict[str, str]) -> tuple[Field, Path, document.Document] | None:
    from .requirements_check import manuscript_files

    files = manuscript_files(venue, values)
    if not files:
        return None
    field, path = files[0]
    return field, path, document.get(path)


def _abstracts(venue: Venue, values: dict[str, str]) -> tuple[list[tuple[str, str, str, bool]], list[Issue]]:
    """``(field id, where, text, is_tex)`` for the .sub's abstract and the manuscript's, plus unchecked notes."""
    out: list[tuple[str, str, str, bool]] = []
    notes: list[Issue] = []
    sub = values.get("abstract", "").strip()
    if sub:
        out.append(("abstract", "the .sub's abstract", sub, False))
    found = _manuscript(venue, values)
    if found is not None:
        field, path, doc = found
        abstract = manuscript_abstract(doc, sub)
        if abstract is None:
            notes.append(unchecked(field.id, f"could not find the abstract in {path.name}; only the .sub's abstract was read" if sub else f"could not find the abstract in {path.name}"))
        elif abstract[0].strip():
            out.append((field.id, f"the abstract in {path.name}", abstract[0], abstract[1]))
    return out, notes


# --- structured abstracts --------------------------------------------------------------


def _heading_variants(heading: str) -> list[str]:
    """The wordings that satisfy one required heading: alternatives, (s) plurals, singular/plural."""
    parts = re.split(r"\s+or\s+|\s*/\s*", heading.strip())
    out: list[str] = []
    for part in parts:
        bare = re.sub(r"\(s\)", "", part).strip()
        for v in (bare, re.sub(r"\(s\)", "s", part).strip()):
            for w in (v, v[:-1] if v.endswith("s") and not v.endswith("ss") else v + "s"):
                if w and w not in out:
                    out.append(w)
    return out


def labelled(text: str, heading: str) -> bool:
    """Whether ``text`` carries ``heading`` as a label rather than a word in its prose."""
    for variant in _heading_variants(heading):
        body = r"\s+".join(re.escape(w) for w in variant.split())
        # At the start of the abstract, a line, or a sentence; then a colon, stop, dash, or line break.
        if re.search(rf"(?:^|\n|[.;!?]\s+|\\(?:textbf|textit|emph|paragraph|subsection\*?|section\*?)\s*\{{)\s*{body}\s*\}}?\s*(?:[:.\u2014\u2013-]|\n)", text.strip(), re.IGNORECASE):
            return True
        # A Markdown or LaTeX bold label: **Background** / \textbf{Background}
        if re.search(rf"\*\*\s*{body}\s*[:.]?\s*\*\*|\\textbf\s*\{{\s*{body}\s*[:.]?\s*\}}", text, re.IGNORECASE):
            return True
    return False


# --- e-mail addresses --------------------------------------------------------------------

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# \email{...} in LaTeX source, and an address with its @ spelled out ("jdoe at uni dot edu").
_TEX_EMAIL = re.compile(r"\\(?:email|emailAdd|ead)\s*(?:\[[^\]]*\])?\s*\{\s*[^}\s]+\s*\}")
# A bare " at " needs a lower-case host with unspaced dots: "done at UCLA. UK Biobank" is prose.
_SPELLED_EMAIL = re.compile(
    r"\b[A-Za-z0-9._%+-]+\s*(?:\[at\]|\(at\)|\{at\})\s*[A-Za-z0-9-]+(?:\s*(?:\.|\[dot\]|\(dot\)|\s+dot\s+)\s*[A-Za-z0-9-]+)+\b"
    r"|\b[A-Za-z0-9._%+-]+\s+at\s+[a-z0-9-]+(?:(?:\.|\s*\[dot\]\s*|\s*\(dot\)\s*|\s+dot\s+)[a-z0-9-]+)+\b"
)


def emails_in(text: str) -> list[str]:
    """E-mail addresses written in ``text`` (as LaTeX ``\\email{}``, plain, or spelled out)."""
    found = EMAIL.findall(text) + _TEX_EMAIL.findall(text)
    if not found:
        found = [m for m in _SPELLED_EMAIL.findall(text) if re.search(r"\b(?:edu|org|com|ac|gov|net|de|uk|fr|cn|jp|ca)\b", m, re.IGNORECASE)]
    return list(dict.fromkeys(found))


# --- the checks ---------------------------------------------------------------------------


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    checks.run("front_matter.abstract_citations", "Abstract cites no references", FRONT_MATTER, _abstract_citations_check, venue, values)
    checks.run("front_matter.structured_abstract", "Abstract structured with the required headings", FRONT_MATTER, _structured_check, venue, values)
    checks.run("front_matter.contact_email", "Manuscript gives a contact e-mail address", FRONT_MATTER, _email_check, venue, values)
    checks.run("front_matter.keywords", "Keywords given in the .sub or the manuscript", FRONT_MATTER, _keywords_check, venue, values)


# A "Keywords" label (at a line's start, or anywhere when followed by a colon or dash), or LaTeX's \\keywords{} and keyword environments.
_KEYWORDS_LABEL = re.compile(
    r"^[\s*_#>]*(?:key[\s-]*words?|key\s+terms|index\s+terms)\b|\b(?:key[\s-]*words?|index\s+terms)\s*[:\u2014\u2013-]|\\(?:begin\s*\{\s*)?(?:IEEE)?keywords?\b",
    re.IGNORECASE | re.MULTILINE,
)


def _keywords_check(venue: Venue, values: dict[str, str]) -> Outcome:
    from .requirements_check import _existing, _fields_for, _paths

    fields = _fields_for(venue, "keywords")
    if any(str(values.get(f.id, "")).strip() for f in fields):
        return Outcome([], "keywords given in the .sub")
    files = [(f, p) for role in ("manuscript", "title_page") for f in _fields_for(venue, role) for p in _existing(_paths(f, values))]
    if not files:
        raise NotApplicable("no keywords in the .sub and no manuscript file to read")
    read = []
    for _, path in files:
        doc = document.get(path)
        whole = "\n".join(t for t in (doc.tex_source, doc.text) if t)
        if not whole:
            continue
        if _KEYWORDS_LABEL.search(whole):
            return Outcome([], f"keywords listed in {path.name}")
        read.append(path.name)
    if not read:
        raise Unchecked("could not read " + ", ".join(p.name for _, p in files))
    field_id = fields[0].id if fields else files[0][0].id
    return Outcome([Issue(WARNING, field_id, f"no keywords in the .sub or in {', '.join(read)}; most venues and indexing services ask for them, so add 3-6 terms that say what the paper is about")])


def _rules(venue: Venue, values: dict[str, str]):
    from .requirements import get_requirements, resolve

    base = get_requirements(venue.slug)
    return None if base is None else resolve(base, values)


def _abstract_citations_check(venue: Venue, values: dict[str, str]) -> Outcome:
    abstracts, notes = _abstracts(venue, values)
    if not abstracts:
        if notes:
            raise Unchecked(notes[0].message)
        raise NotApplicable("no abstract in the .sub and no manuscript file")
    reqs = _rules(venue, values)
    rule = " (the author guidelines do not allow them)" if reqs is not None and reqs.abstract.no_references else ""
    issues: list[Issue] = []
    for field_id, where, text, tex in abstracts:
        found = citations_in(text, tex=tex)
        if found:
            shown = ", ".join(f"'{c}'" for c in found[:MAX_EXAMPLES])
            more = f" and {len(found) - MAX_EXAMPLES} more" if len(found) > MAX_EXAMPLES else ""
            issues.append(Issue(WARNING, field_id, f"{where} cites references ({shown}{more}){rule}; an abstract is read apart from the reference list, so state the point without the citation"))
    read = " and ".join(w for _, w, _, _ in abstracts)
    return Outcome(issues, "" if issues else f"no citations in {read}" + "".join(f" ({n.message})" for n in notes))


def _structured_check(venue: Venue, values: dict[str, str]) -> Outcome:
    reqs = _rules(venue, values)
    if reqs is None or not reqs.abstract.structured:
        raise NotApplicable("the author guidelines do not ask for a structured abstract")
    headings = reqs.abstract.structured_headings or []
    abstracts, notes = _abstracts(venue, values)
    if not abstracts:
        if notes:
            raise Unchecked(notes[0].message)
        raise NotApplicable("no abstract in the .sub and no manuscript file")
    issues: list[Issue] = []
    for field_id, where, text, tex in abstracts:
        if tex:
            from .manuscript import _tex_to_text

            text = re.sub(r"\\(?:textbf|textit|emph|paragraph|subsection\*?|section\*?)\s*\{([^{}]*)\}", r"\n\1:", text)
            text = _tex_to_text(text)
        if headings:
            missing = [h for h in headings if not labelled(text, h)]
            if missing:
                issues.append(Issue(WARNING, field_id, f"{where} is missing the structured-abstract heading(s) {', '.join(missing)}; the author guidelines ask for the headings {', '.join(headings)}, each as a label ('{headings[0]}: ...')"))
        elif not re.search(r"(?:^|\n|[.;]\s+)\s*[A-Z][A-Za-z ]{2,30}\s*:\s", text.strip()):
            issues.append(Issue(WARNING, field_id, f"{where} has no labelled sections; the author guidelines ask for a structured abstract (e.g. 'Background: ... Methods: ... Results: ... Conclusions: ...')"))
    if issues:
        return Outcome(issues)
    found = f"{'the required headings label' if headings else 'labelled sections in'} {' and '.join(w for _, w, _, _ in abstracts)}"
    return Outcome(issues, found + "".join(f" ({n.message})" for n in notes))


def _email_check(venue: Venue, values: dict[str, str]) -> Outcome:
    if venue.anonymous:
        raise NotApplicable("the venue is double-blind: the manuscript must not identify its authors")
    reqs = _rules(venue, values)
    if reqs is not None and "corresponding_email" in (reqs.title_page.required_items or []):
        raise NotApplicable("the author guidelines require the corresponding author's e-mail on the title page; see guidelines.title_page")
    from .consistency import _title_page_source

    try:
        field, path, front = _title_page_source(venue, values)
    except Unchecked:
        # No first pages to read (Markdown, plain text): the whole file is searched below.
        found = _manuscript(venue, values)
        if found is None:
            raise NotApplicable("no manuscript file to read") from None
        field, path, _ = found
        front = ""
    doc = document.get(path)
    raw_front = front
    if doc.is_tex and doc.tex_source:
        from .manuscript import title_page_text

        raw_front = (title_page_text(path) or "") + "\n" + front
    found = emails_in(raw_front)
    if found:
        return Outcome([], f"{found[0]} on the title page of {path.name}")
    whole = "\n".join(t for t in (doc.tex_source, doc.text) if t)
    if not whole:
        raise Unchecked(f"could not read {path.name} as text")
    found = emails_in(whole)
    if found:
        return Outcome([], f"{found[0]} in {path.name} (not on its first pages)")
    sub_emails = EMAIL.findall("\n".join(str(v) for v in values.values()))
    hint = f" (the .sub gives {sub_emails[0]})" if sub_emails else ""
    return Outcome([Issue(WARNING, field.id, f"{path.name} gives no e-mail address; add the corresponding author's e-mail to the title page{hint}")])


__all__ = ["TEX_CITE", "citations_in", "emails_in", "labelled", "manuscript_abstract", "run_checks"]
