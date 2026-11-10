"""Checks requiring a closer reading of manuscript and title-page files."""

from __future__ import annotations

import re
import unicodedata
import zipfile
from pathlib import Path

from . import placeholders
from .ai_validate import AiTask
from .bibliography_check import read_bibliography
from .database import Venue
from .requirements import get_requirements, resolve
from .validate import Checks, Issue, NotApplicable, Outcome, Unchecked, WARNING


CATEGORY = "Manuscript details"


def _files(venue: Venue, values: dict[str, str], role: str) -> list[Path]:
    from .requirements_check import _existing, _fields_for, _paths

    return [p for field in _fields_for(venue, role) for p in _existing(_paths(field, values))]


def _fold(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.lower()) if not unicodedata.combining(c))


def _reference_order(venue: Venue, values: dict[str, str], alphabetical: bool, citation_order: bool) -> Outcome:
    paths = _files(venue, values, "manuscript")
    if not paths:
        raise NotApplicable("no manuscript file to read")
    path = paths[0]
    bib = read_bibliography(path)
    if bib is None or len(bib.refs) < 2:
        raise Unchecked(f"could not read at least two reference entries in {path.name}")
    base = get_requirements(venue.slug)
    rules = resolve(base, values).references if base else None
    style = (rules.style or "").lower() if rules else ""
    specified_alpha = bool(rules and ("alphabet" in style or re.search(r"\b(?:APA|Harvard)\b", rules.style or "", re.IGNORECASE)))
    specified_citation = bool(re.search(r"order of (first )?citation|order cited|citation order", style))
    venue_other_order = bool(style and not specified_alpha and not specified_citation) or bool(rules and rules.numbered is True)
    if specified_alpha or specified_citation:
        alphabetical, citation_order = specified_alpha, specified_citation
    elif venue_other_order:
        alphabetical, citation_order = False, False
    from . import document
    from .bibliography_check import _split

    doc = document.get(path)
    parts = _split(doc.text or "")
    if parts is None:
        raise Unchecked(f"could not isolate the reference list in {path.name}")
    from .bibliography_check import _unwrap

    body = _unwrap(_fold(parts[0]))
    if bib.mode == "numbered":
        if venue_other_order and not (rules and rules.numbered is True):
            raise NotApplicable("the venue specifies another reference ordering")
        from .bibliography_check import _first_surname, _numbers, citation_groups

        issues: list[Issue] = []
        surnames = [_fold(_first_surname(ref.authors)) for ref in bib.refs]
        alpha_ok = all(surnames) and surnames == sorted(surnames)
        if alphabetical:
            if not alpha_ok:
                issues.append(Issue(WARNING, "", f"{path.name}: numbered references are not alphabetized by first author's surname"))
        first_seen: list[str] = []
        for group in citation_groups(body, max(int(ref.key) for ref in bib.refs)):
            for number in _numbers(group):
                key = str(number)
                if key not in first_seen:
                    first_seen.append(key)
        if len(first_seen) < 2:
            if alphabetical and not citation_order:
                return Outcome(issues, "numbered citation order could not be measured; alphabetical order checked")
            raise Unchecked(f"could not read at least two numbered citations in {path.name} to determine first-citation order")
        listed = [ref.key for ref in bib.refs if ref.key in first_seen]
        # With no rule either way, an alphabetical numbered list (BibTeX's plain style) is fine.
        if (citation_order or (not alphabetical and not alpha_ok)) and first_seen != listed:
            issues.append(Issue(WARNING, "", f"{path.name}: numbered references are not listed in order of first citation"))
        return Outcome(issues, "numbered reference order checked")
    if bib.mode != "author-year":
        raise Unchecked(f"{path.name} uses {bib.mode} references; ordering could not be assessed reliably")

    names = [_fold(ref.key.rsplit(" ", 1)[0]) for ref in bib.refs]
    alpha_ok = names == sorted(names)
    positions: list[int] = []
    for ref in bib.refs:
        surname = _fold(ref.key.rsplit(" ", 1)[0])
        year = ref.year[:4]
        match = re.search(rf"\b{re.escape(surname)}\b[^\n]{{0,100}}?\b{re.escape(year)}\b", body)
        if match:
            positions.append(match.start())
    cite_ok = len(positions) >= 2 and positions == sorted(positions)
    if not positions or len(positions) < 2:
        if not alpha_ok:
            raise Unchecked("reference list is not alphabetical, but citation order could not be measured reliably")
        cite_ok = False

    issues: list[Issue] = []
    if alphabetical and not alpha_ok:
        issues.append(Issue(WARNING, "", f"{path.name}: author-year references are not alphabetized"))
    if citation_order and not cite_ok:
        issues.append(Issue(WARNING, "", f"{path.name}: author-year references are not ordered by first citation"))
    if not (alphabetical or citation_order or venue_other_order) and not (alpha_ok or cite_ok):
        issues.append(Issue(WARNING, "", f"{path.name}: author-year references are neither alphabetical nor ordered by first citation"))
    return Outcome(issues, f"alphabetical: {'yes' if alpha_ok else 'no'}; first-citation order: {'yes' if cite_ok else 'no'}")


def _word_file_cleanliness(venue: Venue, values: dict[str, str]) -> Outcome:
    paths = _files(venue, values, "manuscript") + _files(venue, values, "title_page")
    word_files = [p for p in paths if p.suffix.lower() == ".docx"]
    if not word_files:
        raise NotApplicable("no Word (.docx) manuscript or title page")
    issues: list[Issue] = []
    for path in word_files:
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                xml = "\n".join(archive.read(name).decode("utf-8", "replace") for name in names if name.startswith("word/") and name.endswith(".xml"))
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            raise Unchecked(f"could not inspect {path.name}: {exc}") from exc
        if re.search(r"<w:(?:ins|del|moveFrom|moveTo|delText|cellIns|cellDel|rPrChange|pPrChange|sectPrChange|tblPrChange|tcPrChange|trPrChange)\b", xml):
            issues.append(Issue(WARNING, "", f"{path.name} contains tracked changes; accept or reject them before submission if the venue requires a clean file"))
        if re.search(r"(?:ADDIN\s+EN\.CITE|ADDIN\s+ENDNOTE|EndNote\.Cite|\{[^{}]{1,100}\s+#\d+\})", xml, re.IGNORECASE):
            issues.append(Issue(WARNING, "", f"{path.name} contains live or temporary EndNote citations; check whether the venue requires plain-text citations"))
    return Outcome(issues, f"inspected {len(word_files)} Word file(s) for tracked changes and EndNote fields")


def _author_names(venue: Venue, values: dict[str, str]) -> list[str]:
    """Full names (two words or more) from the .sub's author-list fields."""
    from .validate import parse_authors

    names: list[str] = []
    for field in venue.fields:
        if field.type != "authorlist" or "author" not in field.id.lower():
            continue
        for author in parse_authors(values.get(field.id, ""), field.fields):
            name = (author.get("name") or " ".join(p for p in (author.get("first_name", ""), author.get("last_name", "")) if p)).strip()
            if len(name.split()) >= 2 and name not in names:
                names.append(name)
    return names


def _name_then_digit(name: str) -> re.Pattern[str]:
    """A name immediately followed (no space) by a digit; group 1 is the digit."""
    body = r"\s+".join(re.escape(part) for part in name.split())
    return re.compile(rf"(?<!\w){body}(\d)", re.IGNORECASE)


_W_PARA = re.compile(r"<w:p\b.*?</w:p>", re.DOTALL)
_W_RUN = re.compile(r"<w:r\b.*?</w:r>", re.DOTALL)
_W_TEXT = re.compile(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>")


# Where the front matter (title, authors, affiliations) ends on a manuscript's first page.
_FRONT_END = re.compile(r"^\s*(?:#+\s*)?(?:\d+\.?\s*)?(?:abstract|summary|introduction|background|main)\b\s*:?\s*$", re.IGNORECASE | re.MULTILINE)
_TEX_FRONT_END = re.compile(r"\\begin\{abstract\}|\\maketitle|\\section\*?\{|\\abstract\{")


def _front(text: str, latex: bool = False) -> str:
    """The part of a manuscript's source before its abstract or first section."""
    m = (_TEX_FRONT_END if latex else _FRONT_END).search(text)
    return text[: m.start()] if m else text


def _docx_inline(xml: str, patterns: dict[str, re.Pattern[str]], first_page: bool) -> list[str]:
    hits: list[str] = []
    for para in _W_PARA.findall(xml):
        if first_page and _FRONT_END.fullmatch("".join(_W_TEXT.findall(para))):
            break
        text, raised = "", []
        for run in _W_RUN.findall(para):
            piece = "".join(_W_TEXT.findall(run))
            text += piece
            raised += [bool(re.search(r'<w:vertAlign w:val="superscript"', run))] * len(piece)
        for name, pattern in patterns.items():
            hits += [name for m in pattern.finditer(text) if not raised[m.start(1)]]
        if first_page and re.search(r'<w:br [^>]*w:type="page"|<w:lastRenderedPageBreak/>|<w:pageBreakBefore/>', para):
            break
    return hits


def _pdf_inline(pages, patterns: dict[str, re.Pattern[str]], first_page: bool) -> list[str]:
    hits: list[str] = []
    for page in pages[:1] if first_page else pages:
        text, index = "", []
        prev = None
        for g in page.glyphs:
            if g.in_figure or not g.text:
                continue
            if prev is not None and not g.text.isspace() and not text.endswith(" "):
                new_line = abs(g.y0 - prev.y0) > prev.size and g.x0 < prev.x0
                if new_line or g.x0 - prev.x1 > 0.2 * prev.size:
                    text, index = text + " ", index + [None]
            text, index = text + g.text, index + [g] * len(g.text)
            prev = g
        for name, pattern in patterns.items():
            for m in pattern.finditer(text):
                digit, before = index[m.start(1)], index[m.start(1) - 1]
                if digit is None or before is None:
                    continue
                if digit.size >= 0.9 * before.size and abs(digit.y0 - before.y0) < 0.15 * before.size:
                    hits.append(name)
    return hits


def _affiliation_superscripts(venue: Venue, values: dict[str, str]) -> Outcome:
    """Flag an author name followed directly by an affiliation number that is not superscripted ("Joe Rich2")."""
    from . import document

    names = _author_names(venue, values)
    if not names:
        raise NotApplicable("no author names in the .sub")
    # The manuscript's first page (under the title) and the whole title page.
    title_pages = _files(venue, values, "title_page")
    paths = {p: False for p in title_pages}
    for p in _files(venue, values, "manuscript"):
        paths.setdefault(p, True)
    if not paths:
        raise NotApplicable("no manuscript or title page")
    patterns = {name: _name_then_digit(name) for name in names}
    issues: list[Issue] = []
    read: list[str] = []
    for path, first_page in paths.items():
        doc = document.get(path)
        if doc.is_docx:
            xml = doc.docx_part("word/document.xml")
            hits = None if xml is None else _docx_inline(xml, patterns, first_page)
        elif doc.is_tex:
            source = doc.tex_source
            if source is not None and first_page:
                source = _front(source, latex=True)
            hits = None if source is None else [n for n, p in patterns.items() if p.search(source)]
        elif doc.is_markdown or doc.suffix == ".txt":
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                source = None
            if source is not None and first_page:
                source = _front(source)
            hits = None if source is None else [n for n, p in patterns.items() if p.search(source)]
        elif doc.is_pdf:
            pages = doc.glyph_pages
            hits = None if pages is None else _pdf_inline(pages, patterns, first_page)
        else:
            hits = None
        if hits is None:
            continue
        read.append(path.name)
        for name in dict.fromkeys(hits):
            issues.append(Issue(WARNING, "", f"{path.name}: the affiliation number after '{name}' is not superscripted (e.g. '{name}1' should be '{name}¹')"))
    if not read:
        raise Unchecked("could not read the author line of any manuscript or title page")
    return Outcome(issues, f"checked {len(names)} author name(s) in {', '.join(read)}")


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    checks.run("details.reference_order", "Reference list is alphabetical or follows first citation", CATEGORY, _reference_order, venue, values, bool(checks.options.get("references_alphabetical")), bool(checks.options.get("references_citation_order")))
    checks.run("details.word_cleanliness", "Word files have no tracked changes or EndNote fields", CATEGORY, _word_file_cleanliness, venue, values)
    checks.run("details.placeholders", "No placeholders, to-do notes, comments, or highlighting left in the files", CATEGORY, placeholders.check, venue, values)
    checks.run("details.affiliation_superscripts", "Affiliation numbers after author names are superscripted", CATEGORY, _affiliation_superscripts, venue, values)

    manuscripts = _files(venue, values, "manuscript")
    if not manuscripts:
        return
    title_pages = _files(venue, values, "title_page")
    files = [str(p) for p in manuscripts + title_pages]
    base = get_requirements(venue.slug)
    reqs = resolve(base, values) if base else None
    guideline = "Follow the named venue's requirements and the selected article type when they are supplied. Otherwise, judge whether the item is present and flag only a clear omission or inconsistency."
    tasks = [
        ("ethics", "Ethics approval and consent", "Check whether research involving humans or animals needs ethics approval and informed consent. If applicable, verify that the manuscript states approval or exemption, names the review board, and explains informed consent in the ethics section or Methods. Check a dedicated section's title and position against the venue rules. Do not infer approval from a heading alone."),
        ("declarations", "Required declarations (conflicts, data, contributions, funding) present and in the right place", "Check conflict of interest, data sharing or availability, author contributions, and funding statements. For each required item, verify the statement's substance, section title, and location against venue rules. A statement may correctly say none or not applicable. Identify each missing or misplaced item."),
        ("author_details", "Title page lists authors, affiliations, corresponding author, and contact emails", "Extract and report the title as printed on the title page. Check that all authors, affiliations, corresponding author designation, and contact email addresses are present where required. State whether each email domain is institutional for its stated affiliation; flag a public address as noncompliant only when the venue requires an institutional one. Check word count, keywords, and running head where required."),
        ("abstract_reporting", "Abstract has the required form and subheadings (plus PRISMA/CONSORT items for reviews and trials)", "Check that the manuscript has an abstract and that its structured or unstructured form and exact subheadings meet venue rules. If it is a systematic review or clinical trial, check the abstract against applicable PRISMA or CONSORT abstract reporting items, naming missing items; do not apply either checklist to unrelated article types."),
        ("supplementary", "Every mention of supplementary material has a matching uploaded item", "Check whether the manuscript refers to any uploaded supplementary material or appendix, and whether every such mention has a corresponding item. Treat a manuscript with no supplement and no appendix mention as not applicable."),
        ("trademarks", "Trademarked product names and ™/® symbols flagged", "Identify explicit trademark symbols and named commercial products likely to be registered marks. Report the name and location; do not assume every product name is a registered trademark, and return unclear if registration cannot be verified from the supplied files."),
        ("misplaced_content", "Content is in the section it belongs to", "Read the manuscript section by section and find content in the wrong section: findings or data reported in the Introduction or Methods, procedures described in the Results that the Methods never gives, new results that first appear in the Discussion or Conclusions, and interpretation that belongs in the Discussion but makes up a Results paragraph. Quote each passage and name the section it belongs in. A sentence of motivation or a brief recap of an earlier section is fine, and so is a combined Results and Discussion or a venue's own section order. Flag only clear cases."),
        ("heading_content", "Section headings match the text under them", "Compare each section and subsection heading with the text under it, down to the next heading. Flag a heading that names a topic the text does not cover (a 'Statistical analysis' section that describes only sample preparation), a section that also carries a major topic its heading does not suggest, and a descriptive Results heading that claims a finding the text under it does not report. Name each heading and say what the text covers instead. Do not flag headings for style or wording alone."),
        ("nih_grant_numbers", "NIH funding statements give grant numbers", "Find the funding statement or acknowledgments. If they name the NIH, National Institutes of Health, or an NIH institute or center (NCI, NHLBI, NIAID, NIGMS, NHGRI, NIMH, NIA, NINDS, NIDDK, NLM, and so on) as a funder, check that each such award has a grant number, for example R01GM123456 or U54 CA209891. Report each NIH funder named without a number, quoting the sentence. If the manuscript names no NIH funding, the check passes; do not apply it to other funders. Do not report an NIH employee or facility mentioned without funding."),
    ]
    for suffix, title, instructions in tasks:
        checks.needs_ai(AiTask(id=f"ai.details.{suffix}", title=title, category=CATEGORY, instructions=f"{instructions} {guideline}", files=files))
