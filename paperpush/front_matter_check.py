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
* ``front_matter.institutional_email`` -- the authors' e-mail addresses (in
  the ``.sub``'s author list and on the manuscript's title page) are
  institutional, not personal webmail (Gmail, Outlook, Yahoo, QQ, ...).
  Editors and readers trust, and can verify, an institutional address, and
  many journals ask for one. A warning, since an author with no institution
  may have only a personal address. At a double-blind venue only the ``.sub``
  is read.
* ``front_matter.orcid`` -- at least one author's ORCID iD appears in the
  ``.sub`` or the manuscript (or title page). ORCID iDs are optional at most
  venues, so finding none is only a suggestion. Not applicable when the
  venue already requires them (an ORCID on the title page in the guidelines,
  or a required ORCID column in the author list), since those checks report it.
  At a double-blind venue the suggestion points at the submission form only.

Abstract findings are WARNINGs: the abstract is located heuristically in extracted
text.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from . import document
from .database import Field, Venue
from .validate import DEFAULT_AUTHOR_FIELDS, SUGGESTION, WARNING, Checks, Issue, NotApplicable, Outcome, Unchecked, unchecked

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


def _small_caps(words: str) -> str:
    """``words`` as a pattern that also matches small-caps headings split after their first letter.

    pdftotext renders a small-caps heading's large initial apart from the rest,
    so ICLR's "1 Introduction" comes out as "1 I NTRODUCTION" and "Abstract" as
    "A BSTRACT"; without this the abstract ran on into the Introduction.
    """
    return r"\s+".join(w[0] + r" ?" + w[1:] if len(w) > 1 and w[0].isalpha() else w for w in words.split())


_ABSTRACT_HEADING = re.compile(
    rf"^[ \t]*(?:\d+\.?\s+)?(?:{_small_caps('abstract')}|summary(?![ \t]+(?:statement|points?|box)\b))[ \t]*[:.\u2014-]?[ \t]*(?:\n|(?=(?-i:[A-Z])))", re.IGNORECASE | re.MULTILINE
)
# What ends the abstract in extracted text: the next part of the front matter or the main text.
_END_WORDS = (
    "introduction", "key words", "keywords", "index terms", "main", "main text", "significance", "significance statement", "author summary",
    "graphical abstract", "highlights", "background and summary", "one-sentence summary", "one sentence summary", "teaser",
    # ACM and IEEE front matter after the abstract.
    "ccs concepts", "acm reference format", "general terms", "additional key words and phrases",
)  # fmt: skip
_ABSTRACT_END = re.compile(
    r"^[ \t]*(?:\d+\.?[ \t]+|[IVX]+\.[ \t]+)?(?:" + "|".join(_small_caps(w) for w in sorted(_END_WORDS, key=len, reverse=True)) + r")\b[ \t]*[:.\u2014-]?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
# The first numbered section of the paper, whatever it is called: "1 Background", "1. Problem Setup",
# "1 M OTIVATION" (split small caps), "I. RELATED WORK". A line of its own, of capitalised words, with no
# full stop, so a wrapped abstract line that happens to start with "1" ("1 million cells were ...") is not one.
# A split small-caps word ("I NTRODUCTION") reads as two capitalised words.
_CAP_WORD = r"(?:[A-Z][A-Za-z0-9'\u2019-]*|of|and|for|the|in|on|to|with|a|an|via|vs\.?|&)"
_FIRST_SECTION = re.compile(rf"^[ \t]*(?:1\.?|I\.)[ \t]+(?=[A-Z]){_CAP_WORD}(?:[ \t]+{_CAP_WORD}){{0,7}}[ \t]*$", re.MULTILINE)
# A heading after a paragraph break: a blank line, then a short line of capitalised words with no full stop,
# then more text ("\n\nRelated Work\nPrior methods ..."). Structured-abstract labels are left to _end_at_label.
_HEADING_AFTER_BREAK = re.compile(rf"\n[ \t]*\n[ \t]*((?=[A-Z]){_CAP_WORD}(?:[ \t]+{_CAP_WORD}){{0,7}})[ \t]*\n(?=[ \t]*\S)")
# A figure or table caption at the start of a line: no abstract contains one.
_CAPTION = re.compile(r"^[ \t]*(?:Figure|Fig\.|Table)[ \t]*\d+[ \t]*[:.|]", re.MULTILINE)
# Words inside a figure, as pdftotext gives them: eight or more lines in a row of three words or fewer.
# Fewer is not enough: inline math in an abstract also breaks into a handful of short lines.
_FIGURE_DEBRIS = re.compile(r"^(?:[ \t]*(?:\S+[ \t]+){0,2}\S+[ \t]*\n){8,}", re.MULTILINE)
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


def _end_at_heading(rest: str, *, pdf: bool = False) -> int | None:
    """Where the first heading after the abstract starts in ``rest``, or None.

    The Introduction, keywords, and similar named parts; the paper's first
    numbered section under any name; or a heading after a paragraph break.
    Structured-abstract labels ("Methods", "Results") are not headings here.
    In a PDF, a figure's caption or the text inside it (a run of very short
    lines) also ends it, since pdftotext can put a figure that sits above the
    Introduction (ICLR's teaser figures) between it and the abstract.
    """
    ends = [m.start() for m in (_ABSTRACT_END.search(rest), _FIRST_SECTION.search(rest), _CAPTION.search(rest)) if m]
    if pdf:
        debris = _FIGURE_DEBRIS.search(rest)
        if debris:
            ends.append(debris.start())
    for m in _HEADING_AFTER_BREAK.finditer(rest):
        if not _ABSTRACT_LABEL.fullmatch(m.group(1)) and re.search(r"[A-Za-z]{3}", m.group(1)):
            ends.append(m.start(1))
            break
    return min(ends, default=None)


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
        end = _end_at_heading(rest, pdf=doc.is_pdf)
        return rest[: end if end is not None else _end_without_introduction(rest, doc, sub_abstract)], False
    words = re.findall(r"[A-Za-z0-9]+", sub_abstract)
    if len(words) >= 8:
        start = re.search(r"\W+".join(re.escape(w) for w in words[:6]), text, re.IGNORECASE)
        if start is not None:
            end = re.search(r"\W+".join(re.escape(w) for w in words[-6:]) + r"\S*", text[start.start() :], re.IGNORECASE)
            if end is not None:
                return text[start.start() : start.start() + end.end()], False
    return None


# An "abstract" longer than this means its end was not found (the ICLR audit's runaways read 530-660
# words, Introduction included); real abstracts run 150-300 words, rarely past 400.
_ABSTRACT_MAX_WORDS = 450


def _checkable_abstract(text: str) -> str | None:
    """``text`` if it is plausibly the whole abstract and nothing more, else its first paragraph, else None.

    Reporting citations from past the abstract's end is worse than missing
    one in it, so an over-long stretch is cut to its first paragraph and, with
    no paragraph break to cut at, not checked at all.
    """
    if len(text.split()) <= _ABSTRACT_MAX_WORDS:
        return text
    first = re.split(r"\n[ \t]*\n", text.strip(), maxsplit=1)[0]
    return first if len(first.split()) <= _ABSTRACT_MAX_WORDS else None


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


# Webmail and consumer-ISP domains: an address at one of these is personal, not institutional.
PERSONAL_EMAIL_DOMAINS = frozenset(
    "gmail.com googlemail.com outlook.com hotmail.com live.com msn.com aol.com icloud.com me.com mac.com "
    "ymail.com rocketmail.com protonmail.com protonmail.ch proton.me pm.me tutanota.com tuta.io fastmail.com "
    "zoho.com mail.com email.com gmx.com gmx.net gmx.de web.de t-online.de freenet.de mail.ru inbox.ru bk.ru "
    "list.ru rambler.ru yandex.com yandex.ru ya.ru qq.com foxmail.com 163.com 126.com yeah.net sina.com sina.cn "
    "sohu.com 139.com aliyun.com naver.com hanmail.net daum.net kakao.com rediffmail.com libero.it virgilio.it "
    "tiscali.it alice.it orange.fr wanadoo.fr free.fr laposte.net sfr.fr btinternet.com sky.com comcast.net "
    "verizon.net att.net sbcglobal.net bellsouth.net cox.net charter.net earthlink.net optonline.net "
    "shaw.ca rogers.com sympatico.ca bigpond.com optusnet.com.au seznam.cz wp.pl o2.pl onet.pl interia.pl "
    "uol.com.br bol.com.br terra.com.br hush.com".split()
)
# Webmail brands that register the same name under many country domains (yahoo.co.uk, hotmail.fr).
_PERSONAL_EMAIL_BRANDS = re.compile(r"^(?:yahoo|hotmail|outlook|live|gmx|yandex|aol|msn)\.(?:[a-z]{2,3}\.)?[a-z]{2,3}$")


def is_personal_email(address: str) -> bool:
    """Whether ``address`` is at a webmail or consumer-ISP domain rather than an institution's."""
    domain = address.rsplit("@", 1)[-1].strip().strip(".>").lower()
    return domain in PERSONAL_EMAIL_DOMAINS or bool(_PERSONAL_EMAIL_BRANDS.match(domain))


# Several authors' addresses on one domain: "{a.smith, b.jones}@uni.ac.uk" (or "(...)@").
_GROUPED_EMAIL = re.compile(r"[{(][\w.+\-,;\s]{1,300}[})]\s?@[\w-]+(?:\.[\w-]+)+")


def emails_in(text: str) -> list[str]:
    """E-mail addresses written in ``text`` (as LaTeX ``\\email{}``, plain, or spelled out)."""
    found = EMAIL.findall(text) + _TEX_EMAIL.findall(text) + _GROUPED_EMAIL.findall(text)
    if not found:
        found = [m for m in _SPELLED_EMAIL.findall(text) if re.search(r"\b(?:edu|org|com|ac|gov|net|de|uk|fr|cn|jp|ca)\b", m, re.IGNORECASE)]
    return list(dict.fromkeys(found))


# --- the checks ---------------------------------------------------------------------------


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    checks.run("front_matter.abstract_citations", "Abstract cites no references", FRONT_MATTER, _abstract_citations_check, venue, values)
    checks.run("front_matter.structured_abstract", "Abstract structured with the required headings", FRONT_MATTER, _structured_check, venue, values)
    checks.run("front_matter.contact_email", "Manuscript gives a contact e-mail address", FRONT_MATTER, _email_check, venue, values)
    checks.run("front_matter.institutional_email", "Author e-mail addresses are institutional, not personal (Gmail, Outlook, ...)", FRONT_MATTER, _institutional_email_check, venue, values)
    checks.run("front_matter.keywords", "Keywords given in the .sub or the manuscript", FRONT_MATTER, _keywords_check, venue, values)
    checks.run("front_matter.orcid", "ORCID iDs given in the .sub or the manuscript", FRONT_MATTER, _orcid_check, venue, values)


# An ORCID iD (0000-0002-1825-0097, checksum X allowed), an orcid.org link, or LaTeX's \orcid / \orcidlink.
ORCID = re.compile(r"(?<![\d-])\d{4}-\d{4}-\d{4}-\d{3}[\dX](?![\d-])|orcid\.org/\d|\\orcid(?:link|id)?\b", re.IGNORECASE)


def _orcid_check(venue: Venue, values: dict[str, str]) -> Outcome:
    from .requirements_check import _existing, _fields_for, _paths
    from .validate import _subfield_specs

    reqs = _rules(venue, values)
    if reqs is not None and "orcid" in (reqs.title_page.required_items or []):
        raise NotApplicable("the author guidelines require ORCID iDs on the title page; see guidelines.title_page")
    author_fields = [f for f in venue.fields if f.type == "authorlist" and "author" in f.id.lower()]
    orcid_columns = [f for f in author_fields if any(name == "orcid" for name, _ in _subfield_specs(f.fields or DEFAULT_AUTHOR_FIELDS))]
    if any(required for f in orcid_columns for name, required in _subfield_specs(f.fields or DEFAULT_AUTHOR_FIELDS) if name == "orcid"):
        raise NotApplicable("the submission form requires each author's ORCID iD; see form.authors")
    if ORCID.search("\n".join(str(v) for v in values.values())):
        return Outcome([], "ORCID iD(s) given in the .sub")
    files = [(f, p) for role in ("manuscript", "title_page") for f in _fields_for(venue, role) for p in _existing(_paths(f, values))]
    authors_given = any(str(values.get(f.id, "")).strip() for f in author_fields)
    if not files and not authors_given:
        raise NotApplicable("no authors in the .sub and no manuscript file to read")
    read = []
    for _, path in files:
        doc = document.get(path)
        whole = "\n".join(t for t in (doc.tex_source, doc.text) if t)
        if not whole:
            continue
        if ORCID.search(whole):
            return Outcome([], f"ORCID iD(s) in {path.name}")
        read.append(path.name)
    if files and not read and not authors_given:
        raise Unchecked("could not read " + ", ".join(p.name for _, p in files))
    where = " or ".join(["the .sub"] + ([", ".join(read)] if read else []))
    field_id = (orcid_columns or author_fields or [f for f, _ in files])[0].id
    if orcid_columns:
        place = "the ORCID column of the .sub's author list"
    else:
        place = "the authors' profiles in the submission portal"
    if not venue.anonymous:
        place += " and the manuscript's title page (next to each author's name)"
    advice = f"consider adding ORCID iDs (at least the corresponding author's) to {place}. Many journals and funders ask for them, and they link the paper to the right author record"
    if venue.anonymous:
        advice += " (keep them out of the manuscript, which must stay anonymous)"
    return Outcome([Issue(SUGGESTION, field_id, f"no ORCID iDs in {where}; {advice}")])


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
    rules = _rules(venue, values)
    required = any(f.required or (f.min_count or 0) > 0 for f in fields) or bool(rules and (rules.keywords.min or 0) > 0)
    level = WARNING if required else SUGGESTION
    advice = "the venue requires keywords" if required else "consider adding keywords to help indexing and discovery"
    return Outcome([Issue(level, field_id, f"no keywords in the .sub or in {', '.join(read)}; {advice}")])


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
    read: list[str] = []
    for field_id, where, text, tex in abstracts:
        if field_id != "abstract" and not tex:
            # Found heuristically in extracted text: check it only when its end is believable.
            checkable = _checkable_abstract(text)
            if checkable is None:
                notes.append(unchecked(field_id, f"could not tell where {where} ends, so it was not read for citations"))
                continue
            text = checkable
        read.append(where)
        found = citations_in(text, tex=tex)
        if found:
            shown = ", ".join(f"'{c}'" for c in found[:MAX_EXAMPLES])
            more = f" and {len(found) - MAX_EXAMPLES} more" if len(found) > MAX_EXAMPLES else ""
            issues.append(Issue(WARNING, field_id, f"{where} cites references ({shown}{more}){rule}; an abstract is read apart from the reference list, so state the point without the citation"))
    if not read:
        raise Unchecked(notes[0].message)
    return Outcome(issues, "" if issues else f"no citations in {' and '.join(read)}" + "".join(f" ({n.message})" for n in notes))


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


def _title_page_emails(venue: Venue, values: dict[str, str]) -> tuple[Field, Path, list[str]] | None:
    """The plain e-mail addresses on the manuscript's title page (or its first pages), or None with no manuscript."""
    from .consistency import _title_page_source
    from .manuscript import truncate_at_references

    try:
        field, path, front = _title_page_source(venue, values)
    except Unchecked:
        found = _manuscript(venue, values)
        if found is None:
            return None
        field, path, doc = found
        # No first pages to read (Markdown, plain text): the text before the references.
        front = truncate_at_references(doc.text or "")
    doc = document.get(path)
    if doc.is_tex and doc.tex_source:
        from .manuscript import title_page_text

        front = (title_page_text(path) or "") + "\n" + front
    return field, path, list(dict.fromkeys(EMAIL.findall(front)))


def _institutional_email_check(venue: Venue, values: dict[str, str]) -> Outcome:
    author_fields = [f for f in venue.fields if f.type == "authorlist"]
    sources: list[tuple[str, str, list[str]]] = []  # (field id, where, addresses)
    for f in author_fields:
        found = list(dict.fromkeys(EMAIL.findall(str(values.get(f.id, "")))))
        if found:
            sources.append((f.id, "the .sub's author list", found))
    if not venue.anonymous:
        page = _title_page_emails(venue, values)
        if page is not None and page[2]:
            sources.append((page[0].id, f"the title page of {page[1].name}", page[2]))
    if not sources:
        raise NotApplicable("no author e-mail addresses in the .sub or on the title page")
    issues: list[Issue] = []
    for field_id, where, found in sources:
        personal = [a for a in found if is_personal_email(a)]
        if personal:
            issues.append(Issue(SUGGESTION, field_id, f"{where} gives personal e-mail address(es): {', '.join(personal)}; use institutional addresses where the authors have them, especially the corresponding author's"))
    n = len({a.lower() for _, _, found in sources for a in found})
    return Outcome(issues, "" if issues else f"{n} e-mail address{'es' if n != 1 else ''}, all institutional")


__all__ = ["ORCID", "TEX_CITE", "citations_in", "emails_in", "is_personal_email", "labelled", "manuscript_abstract", "run_checks"]
