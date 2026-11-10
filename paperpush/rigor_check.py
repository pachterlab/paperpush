"""Rigor and reproducibility: research resources, study design, and statistics.

The checks follow what SciScore and the MDAR, ARRIVE 2.0, and NIH rigor
guidelines ask a methods section to report. Each applies only when the
manuscript has what it is about, so a theory or software paper sees them as
not applicable.

* ``rigor.rrids`` -- antibodies, cell lines, and organism strains named in the
  text carry a Research Resource Identifier (RRID). Software tools are held
  to it only when the manuscript gives RRIDs for other resources. The RRIDs
  given are looked up in the SciCrunch resolver (network).
* ``rigor.cell_lines`` -- the cell lines named are not known to be
  misidentified or contaminated (Cellosaurus, which carries the ICLAC
  register; network), and the manuscript says how they were authenticated and
  tested for mycoplasma.
* ``rigor.design`` -- a study of animals or human participants says how it
  handled blinding, randomization, sample size, exclusions, and sex as a
  biological variable (saying it did not do one counts as reporting it).
* ``rigor.statistics`` -- each test statistic reported with its degrees of
  freedom and p value (``t(28) = 2.20, p = .036``; F, r, chi-square, z) is
  recomputed, as statcheck does. A p value the statistic cannot produce is a
  warning; one that flips significance at .05 is called a decision error.
* ``rigor.trial_registration`` -- a manuscript that reports a clinical trial
  gives a registry ID (NCT, ISRCTN, ACTRN, ...), wherever in the text it is.
  Whether a paper is a trial comes from the .sub (a trial question or article
  type) or how the manuscript describes itself. ``ai.rigor.trial_registration``
  has a reader settle what wording cannot: whether it is a trial under the
  ICMJE definition, and whether the ID is this trial's own.
* ``ai.rigor.reporting_guideline`` -- a reader matches the study design to its
  reporting guideline (PRISMA for systematic reviews, CONSORT for trials, ...)
  and checks the manuscript follows it. Narrative reviews have none.

All findings are warnings: they rest on reading free text.
"""

from __future__ import annotations

import json
import logging
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .ai_validate import AiTask
from .database import Venue
from .requirements import get_requirements, resolve
from .validate import WARNING, Checks, Issue, NotApplicable, Outcome, Unchecked

logger = logging.getLogger(__name__)

CATEGORY = "Rigor and reproducibility"
_USER_AGENT = "paperpush-validate (+https://github.com/pachterlab/paperpush)"
_TIMEOUT = 15
MAX_LOOKUPS = 40

# ---------------------------------------------------------------------------
# text


def _texts(venue: Venue, values: dict[str, str], roles: tuple[str, ...] = ("manuscript", "supplementary")) -> list[tuple[str, str, str]]:
    """``(field id, file name, text)`` of the files in ``roles`` that can be read."""
    from . import document
    from .requirements_check import _existing, _fields_for, _paths

    out: list[tuple[str, str, str]] = []
    for role in roles:
        for field in _fields_for(venue, role):
            for path in _existing(_paths(field, values)):
                if path.suffix.lower() not in (".pdf", ".docx", ".tex", ".zip", ".md", ".markdown", ".txt"):
                    continue
                text = document.get(path).text
                if text:
                    out.append((field.id, path.name, re.sub(r"-\n(?=[a-z])", "", text)))
    return out


def _no_text(venue: Venue, values: dict[str, str], roles: tuple[str, ...], what: str) -> Exception:
    """What to raise when ``_texts`` found nothing: not checked if a file is attached (a scanned PDF), else not applicable."""
    from .requirements_check import _existing, _fields_for, _paths

    names = [p.name for role in roles for field in _fields_for(venue, role) for p in _existing(_paths(field, values))]
    if names:
        return Unchecked(f"could not read any text from {', '.join(dict.fromkeys(names))}")
    return NotApplicable(f"no {what} to read")


def _text_or_skip(venue: Venue, values: dict[str, str]) -> list[tuple[str, str, str]]:
    texts = _texts(venue, values)
    if not texts:
        raise _no_text(venue, values, ("manuscript", "supplementary"), "manuscript or supplementary text")
    return texts


def _body_texts(texts: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """``texts`` without their reference lists: a cited paper's HeLa cells are not this study's."""
    from .manuscript import without_reference_list

    return [(field_id, name, without_reference_list(text)) for field_id, name, text in texts]


# ---------------------------------------------------------------------------
# research resources

_VENDORS = r"(?:Abcam|Cell\s+Signal(?:l)?ing(?:\s+Technology)?|CST|Thermo(?:\s*Fisher)?|Invitrogen|BioLegend|Santa\s+Cruz|Sigma(?:-Aldrich)?|Merck|Millipore|R&D\s+Systems|BD\s+(?:Biosciences|Pharmingen)|Proteintech|Novus|Bio-Rad|Jackson\s+ImmunoResearch|eBioscience|Miltenyi|Dako|Agilent|Abnova|GeneTex|Synaptic\s+Systems|Bethyl|Active\s+Motif|Diagenode|Leica|Vector\s+Laboratories|Rockland|Tonbo|Developmental\s+Studies\s+Hybridoma\s+Bank|DSHB|Antibodies-online|Atlas\s+Antibodies|Addgene)"
_CATALOG = r"(?:cat(?:alog(?:ue)?)?\.?\s*(?:no\.?|#|number)|clone\s+[A-Z0-9][\w.-]*|#\s*\w*\d)"
# "anti-GFP" and "antiGFP", not the words "antibiotics", "antifade", "anti-inflammatory".
_NOT_ANTIBODY = r"(?!(?i:bod|biotic|fade|inflammat|cancer|tumou?r|oxid|viral|retroviral|bacterial|microbial|fungal|malarial|coagul|depress|psychotic|hypertens|diabet|apopto|aging|ageing|parallel|sense|correl|diuretic|convuls|epilept|thrombotic|proliferat|metasta|angiogen|fouling|ser(?:um|a)\b|gen\b))"
_ANTIBODY = re.compile(rf"(?:(?i:\banti-)|(?i:\banti)(?=[A-Z0-9])){_NOT_ANTIBODY}[A-Za-z0-9][\w/-]*\b(?=[^.\n]{{0,160}}(?:{_VENDORS}|(?i:{_CATALOG})))|(?i:\bantibod(?:y|ies)\b)(?=[^.\n]{{0,160}}(?:{_VENDORS}|(?i:{_CATALOG})))")
_CELL_LINES = (
    "HEK293T", "HEK293", "HEK-293", "293T", "HeLa", "A549", "MCF-7", "MCF7", "HepG2", "Hep G2", "Jurkat", "K562", "K-562", "U2OS", "U-2 OS",
    "HCT116", "HCT 116", "SH-SY5Y", "NIH/3T3", "NIH3T3", "NIH 3T3", "PC-3", "PC3", "LNCaP", "MDA-MB-231", "MDA-MB-468", "Caco-2", "THP-1",
    "RAW 264.7", "RAW264.7", "CHO-K1", "COS-7", "Vero", "HUVEC", "HAP1", "U-87 MG", "U87", "U251", "SW480", "SW620", "HT-29", "HT29",
    "DU145", "T47D", "T-47D", "BT-474", "SK-N-SH", "SK-N-BE(2)", "HL-60", "Huh7", "Huh-7", "Hep3B", "Neuro-2a", "N2a", "C2C12", "3T3-L1",
    "PANC-1", "MIA PaCa-2", "OVCAR-3", "SKOV3", "SK-OV-3", "A431", "HaCaT", "BEAS-2B", "H1299", "NCI-H1299", "NCI-H460", "Calu-3", "MOLT-4",
    "U937", "U-937", "Raji", "Ramos", "Daudi", "KG-1", "MV4-11", "MOLM-13", "OCI-AML3", "HCT-8", "LoVo", "RKO", "SK-BR-3", "ZR-75-1",
    "MDCK", "HMEC-1", "WI-38", "IMR-90", "MRC-5", "BJ", "RPE-1", "hTERT RPE-1", "ARPE-19", "B16-F10", "B16F10", "4T1", "CT26", "LLC1",
    "EL4", "P815", "Hepa1-6", "KB", "Chang liver", "WISH", "Hep-2", "HEp-2", "INT 407", "SF295", "PC-12", "PC12", "S2", "Sf9", "Sf21",
)
_CELL_LINE = re.compile(r"(?<![\w-])(" + "|".join(sorted((re.escape(c) for c in _CELL_LINES), key=len, reverse=True)) + r")(?![\w-])(?=\s*(?:cells?|cell\s+lines?|\(|,|;|\.|and|or|were|was|\)))")
_GENERIC_CELL_LINE = re.compile(r"\b([A-Z][\w/-]*\d[\w/-]*)\s+cell\s+lines?\b")
_STRAIN = re.compile(r"\b(C57BL/6[JN]?(?:J|N|NCrl)?|BALB/c(?:J|ByJ)?|129S\w*|FVB/N\w*|DBA/2\w*|NOD[/.]?\w*SCID\w*|NSG|Sprague[- ]Dawley|Wistar(?:\s+Kyoto)?|Long[- ]Evans|C3H/He\w*|CD-1|ICR|Swiss\s+Webster)\b")
_SOFTWARE = re.compile(r"\b(ImageJ|Fiji|GraphPad\s+Prism|Prism\s+\d+|MATLAB|SPSS|FlowJo|CellProfiler|Imaris|Seurat|Scanpy|DESeq2|edgeR|limma|HISAT2|Bowtie2?|BWA|Salmon|kallisto|GATK|SAMtools|Cell\s?Ranger|FastQC|Trimmomatic|MaxQuant|Perseus|PyMOL|UCSF\s+Chimera|ChimeraX|Cytoscape|MetaboAnalyst|Skyline|STAR\s+aligner|featureCounts|MACS2|Picard)\b")
RRID = re.compile(r"\bRRID\s*:?\s*((?:AB|CVCL|SCR|IMSR_[A-Z]+|BDSC|ZFIN_[\w-]+|ZDB-[\w-]+|MGI|RGD|WB-STRAIN|Addgene|NXR|BCBC|MMRRC|SAMN|DGGR|FlyBase|SciRes|nlx|nif|OMICS|CGC|TSC|AGSC|MSR)_?[\w:.-]*\w)", re.IGNORECASE)


# Blank lines, table rows, and sentence ends. A single line break is usually a PDF's line wrap, and "Cat. No. ab290" is one sentence.
_UNIT_BREAK = re.compile(r"\n\s*\n|\|[ \t]*\n|\n(?=[^\n]*\t)|(?<=[.!?])(?<!(?i:\bcat\.))(?<!(?i:\bno\.))(?<!(?i:\bnos\.))(?<!(?i:\binc\.))(?<!(?i:\bltd\.))(?<!(?i:\bco\.))(?<!(?i:\bcorp\.))(?<!(?i:\bref\.))\s+(?=[A-Z(])")

# Cell-line names that are also a supplementary figure ("Figure S2"), a principal
# component ("PC3"), a unit ("KB"), initials, or a surname: named only when the
# text calls them cells.
_AMBIGUOUS_CELL_LINES = frozenset({"S2", "PC3", "KB", "BJ", "Ramos", "Raji", "Daudi", "WISH", "Vero"})
_CALLED_CELLS = re.compile(r"(?:(?:,\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+|/)[\w()/-]+){0,4}\s*(?:cells?\b|cell\s+lines?\b)|\s*\(\s*(?:ATCC|RRID|CVCL|DSMZ|ECACC)", re.IGNORECASE)
_CELLS_BEFORE = re.compile(r"\bcell\s+lines?\b[^.]{0,80}$|\bcells?\s*\(?$", re.IGNORECASE)
_LIST_JOINT = re.compile(r"\s*(?:,\s*(?:and\s+|or\s+)?|and\s+|or\s+|/)\s*")
_FIGURE_BEFORE = re.compile(r"(?:\bfig(?:ure)?s?\.?|\btables?)\s*$", re.IGNORECASE)


def _cell_line_matches(text: str) -> list[re.Match[str]]:
    """The listed cell lines named in ``text``, leaving out names that are something else here."""
    out = []
    matches = list(_CELL_LINE.finditer(text))
    for i, m in enumerate(matches):
        if m.group(1) in _AMBIGUOUS_CELL_LINES:
            before = text[max(0, m.start() - 120) : m.start()]
            # "DU145, PC3, and LNCaP": listed beside a name that can only be a cell line.
            neighbours = [(matches[j], text[min(m.end(), matches[j].end()) : max(m.start(), matches[j].start())]) for j in (i - 1, i + 1) if 0 <= j < len(matches)]
            listed = any(n.group(1) not in _AMBIGUOUS_CELL_LINES and _LIST_JOINT.fullmatch(gap) for n, gap in neighbours)
            if _FIGURE_BEFORE.search(before) or not (listed or _CALLED_CELLS.match(text, m.end()) or _CELLS_BEFORE.search(before)):
                continue
        out.append(m)
    return out


def _resource_spans(text: str) -> list[tuple[str, str, int, int]]:
    """``(kind, name, start, end)`` of every research resource named in ``text``, in order."""
    spans: list[tuple[str, str, int, int]] = []
    last_anti = -10
    for m in _ANTIBODY.finditer(text):
        if m.group(0).lower().startswith("antibod") and m.start() - last_anti <= 2:
            continue  # the "antibody" of "anti-GFP antibody"
        last_anti = m.end()
        # Name the antibody by its own words ("anti-GFP"), or the words just before "antibody".
        name = m.group(0) if not m.group(0).lower().startswith("antibod") else " ".join(text[max(0, m.start() - 25) : m.end()].split("\n")[-1].split()[-3:])
        spans.append(("antibody", name, m.start(), m.end()))
    for matches, kind in ((_cell_line_matches(text), "cell line"), (_GENERIC_CELL_LINE.finditer(text), "cell line"), (_STRAIN.finditer(text), "organism"), (_SOFTWARE.finditer(text), "software")):
        for m in matches:
            spans.append((kind, m.group(1), m.start(), m.end()))
    spans.sort(key=lambda s: s[2])
    # Drop a span inside another (a listed cell line also caught by the generic pattern).
    out: list[tuple[str, str, int, int]] = []
    for span in spans:
        if out and span[2] < out[-1][3]:
            continue
        out.append(span)
    return out


def _resource_mentions(text: str) -> dict[str, list[tuple[str, bool]]]:
    """``{kind: [(name, has an RRID)]}`` for the resources named in ``text``.

    A resource's RRID is one given after it and before the next resource or
    the end of its sentence or table row ("anti-GFP (Abcam, Cat# ab290;
    RRID:AB_303395)").
    """
    found: dict[str, dict[str, bool]] = {"antibody": {}, "cell line": {}, "organism": {}, "software": {}}
    spans = _resource_spans(text)
    for i, (kind, name, start, end) in enumerate(spans):
        stop = spans[i + 1][2] if i + 1 < len(spans) else len(text)
        brk = _UNIT_BREAK.search(text, end, stop)
        window = text[end : brk.start() if brk else stop]
        name = " ".join(name.split())
        found[kind][name] = found[kind].get(name, False) or bool(RRID.search(window))
    return {kind: list(names.items()) for kind, names in found.items()}


def _http_json(url: str) -> tuple[int, object | None]:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # https literal below  # nosec B310
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, None


def resolve_rrid(rrid: str) -> bool | None:
    """Whether the SciCrunch resolver knows ``rrid`` (``AB_123``); None when it could not be asked."""
    try:
        status, data = _http_json("https://scicrunch.org/resolver/RRID:" + urllib.parse.quote(rrid, safe="_:-.") + ".json")
    except (OSError, ValueError) as exc:
        logger.debug("RRID lookup failed for %s: %s", rrid, exc)
        return None
    if status == 404:
        return False
    if status != 200 or not isinstance(data, dict):
        return None
    return bool((data.get("hits") or {}).get("total"))


def _plural(kind: str, n: int) -> str:
    if n == 1:
        return kind
    return {"antibody": "antibodies", "software": "software tools"}.get(kind, kind + "s")


def _rrids_check(venue: Venue, values: dict[str, str], online: bool) -> Outcome:
    texts = _body_texts(_text_or_skip(venue, values))
    issues: list[Issue] = []
    named: dict[str, dict[str, bool]] = {}
    given: dict[str, str] = {}
    field_of: dict[str, str] = {}
    for field_id, _, text in texts:
        for kind, items in _resource_mentions(text).items():
            for name, has in items:
                named.setdefault(kind, {})
                named[kind][name] = named[kind].get(name, False) or has
                field_of.setdefault(kind, field_id)
        for m in RRID.finditer(text):
            given.setdefault(m.group(1).upper().replace("RRID:", ""), field_id)
    uses_rrids = bool(given)
    kinds = ["antibody", "cell line", "organism"] + (["software"] if uses_rrids else [])
    if not any(named.get(k) for k in kinds) and not given:
        raise NotApplicable("no antibodies, cell lines, organism strains, or RRIDs named")
    counted = []
    for kind in kinds:
        items = named.get(kind) or {}
        if not items:
            continue
        missing = [n for n, has in items.items() if not has]
        counted.append(f"{len(items) - len(missing)}/{len(items)} {_plural(kind, len(items))}")
        if missing:
            shown = ", ".join(missing[:6]) + (f", and {len(missing) - 6} more" if len(missing) > 6 else "")
            issues.append(Issue(WARNING, field_of.get(kind, ""), f"no RRID found next to {len(missing)} {_plural(kind, len(missing))}: {shown}; give each its RRID (scicrunch.org/resources) or say none exists"))
    unchecked = 0
    if online and given:
        for rrid, field_id in list(given.items())[:MAX_LOOKUPS]:
            known = resolve_rrid(rrid)
            if known is False:
                issues.append(Issue(WARNING, field_id, f"RRID:{rrid} is not known to the SciCrunch resolver; check it for a typo"))
            elif known is None:
                unchecked += 1
    detail = "with RRIDs: " + "; ".join(counted) if counted else ""
    if given:
        detail += ("; " if detail else "") + f"{len(given)} RRID(s) given" + ("" if online else " (not looked up: registry checks are switched off)") + (f", {unchecked} could not be looked up" if unchecked else "")
    return Outcome(issues, detail)


# ---------------------------------------------------------------------------
# cell lines

_AUTHENTICATION = re.compile(r"(?i)\bauthenticat\w*|\bSTR\s+(?:profil|analys|typ)\w*|\bshort\s+tandem\s+repeat|\bcell\s+line\s+(?:identity|identification)|\bverified\s+by\s+(?:STR|genotyp)")
_MYCOPLASMA = re.compile(r"(?i)\bmycoplasma\b")


def cellosaurus_problem(name: str) -> tuple[str, str] | None | bool:
    """``(accession, problem)`` when Cellosaurus flags the cell line ``name``; False when it does not; None when it could not be asked."""
    query = urllib.parse.urlencode({"q": f'id:"{name}" OR sy:"{name}"', "fields": "id,sy,ac,problematic", "format": "tsv", "rows": "10"})
    req = urllib.request.Request("https://api.cellosaurus.org/search/cell-line?" + query, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # https literal  # nosec B310
            body = resp.read().decode("utf-8", "replace")
    except (OSError, ValueError) as exc:
        logger.debug("Cellosaurus lookup failed for %s: %s", name, exc)
        return None
    want = re.sub(r"[\s-]", "", name).lower()
    rows = [line.split("\t") for line in body.splitlines() if line and not line.startswith("#")]
    header = rows[0] if rows else []
    for row in rows[1:]:
        rec = dict(zip(header, row))
        names = [rec.get("id", "")] + [s.strip() for s in rec.get("sy", "").split(";")]
        if any(re.sub(r"[\s-]", "", n).lower() == want for n in names if n):
            problem = (rec.get("problematic") or "").strip()
            return (rec.get("ac", ""), problem) if problem else False
    return False


def _cell_lines_check(venue: Venue, values: dict[str, str], online: bool) -> Outcome:
    texts = _body_texts(_text_or_skip(venue, values))
    lines: dict[str, str] = {}
    whole = "\n".join(t for _, _, t in texts)
    for field_id, _, text in texts:
        for m in _cell_line_matches(text):
            lines.setdefault(" ".join(m.group(1).split()), field_id)
        for m in _GENERIC_CELL_LINE.finditer(text):
            lines.setdefault(m.group(1), field_id)
    if not lines:
        raise NotApplicable("no cell lines named")
    first_field = next(iter(lines.values()))
    issues: list[Issue] = []
    if not _AUTHENTICATION.search(whole):
        issues.append(Issue(WARNING, first_field, f"the manuscript names cell lines ({', '.join(list(lines)[:4])}) but does not say how they were authenticated (e.g., STR profiling)"))
    if not _MYCOPLASMA.search(whole):
        issues.append(Issue(WARNING, first_field, "the manuscript does not say whether the cell lines were tested for mycoplasma contamination"))
    unasked = 0
    if online:
        for name, field_id in list(lines.items())[:MAX_LOOKUPS]:
            got = cellosaurus_problem(name)
            if got is None:
                unasked += 1
            elif got:
                accession, problem = got
                issues.append(Issue(WARNING, field_id, f"{name} ({accession}) is listed as problematic in Cellosaurus: {problem[:200]}"))
    detail = f"{len(lines)} cell line(s) named" + ("" if online else "; not looked up in Cellosaurus (registry checks are switched off)") + (f"; {unasked} could not be looked up" if unasked else "")
    return Outcome(issues, detail)


# ---------------------------------------------------------------------------
# study design

_ANIMALS = re.compile(r"(?i)\b(?:mice|mouse|rats?|zebrafish|primates?|macaques?|rabbits?|pigs?|piglets|dogs|sheep|ferrets|hamsters|guinea\s+pigs|animals)\b")
_HUMANS = re.compile(r"(?i)\b(?:participants|patients|volunteers|human\s+subjects|respondents|cohort|enrolled)\b")
# The host of an antibody ("rabbit anti-LC3", "goat anti-mouse", "mouse monoclonal") is not a study animal.
_HOSTS = r"(?:mouse|mice|rats?|rabbits?|pigs?|guinea\s+pigs?|hamsters?|sheep|dogs|goats?|donkeys?|chickens?)"
_ANTIBODY_HOST = re.compile(rf"(?i)\b{_HOSTS}(?=[\s-]+(?:anti\b|anti-|monoclonal|polyclonal|IgG|IgM|IgA|secondary|primary|antibod))|\banti-{_HOSTS}\b|(?<=\()\s*{_HOSTS}\s*(?=[,;])")
# Telling a study of animals or people from a mention of them.
_ANIMAL_STUDY = re.compile(r"(?i)\b(?:mice|mouse|rats?|zebrafish|primates?|macaques?|rabbits?|pigs?|piglets|dogs|sheep|ferrets|hamsters|guinea\s+pigs|animals)\s+(?:were|was|received|underwent|aged|weighing|per\s+(?:group|cage))\b|\bIACUC\b|\banimal\s+(?:care|ethics|welfare|experiments?|stud(?:y|ies)|procedures?|protocols?)\b|\beuthani[sz]|\bsacrific|\bhoused\b")
_HUMAN_STUDY = re.compile(r"(?i)\b(?:enrolled|(?:participants?|patients?|subjects?|individuals?|volunteers?|people|adults|children|women|men|respondents|controls)\b[^.]{0,80}\brecruited|(?:we|they|investigators?|researchers?)\s+recruited|recruit(?:ed|ment\s+of|ing)\s+(?:\w+\s+){0,3}?(?:participants?|patients?|subjects?|individuals?|volunteers?|people|adults|children|women|men|respondents|controls)|(?:participant|patient|subject|study)\s+recruitment|(?:informed\s+)?consent(?:ed)?|(?:participants|patients|subjects|individuals)\s+(?:were|who)|cohort\s+(?:of|study|included|comprised)|volunteers|respondents|ethics\s+(?:committee|board|approval)|institutional\s+review\s+board|IRB)\b")
_DESIGN: dict[str, re.Pattern[str]] = {
    "blinding": re.compile(r"(?i)\bblind(?:ed|ing|ly)?\b|\bmask(?:ed|ing)\b(?=[^.]{0,60}\b(?:group|treatment|allocation|condition|genotype|outcome|assessor|investigator))|\b(?:un)?aware\s+of\s+(?:the\s+)?(?:group|treatment|genotype|allocation)|\bopen[- ]label\b"),
    "randomization": re.compile(r"(?i)\brandomi[sz](?:ed|ation|ing)\b|\brandom(?:ly)?\s+(?:assign|allocat)\w*|\ballocat\w+\s+(?:at\s+)?random\w*|\brandom\s+allocation\b"),
    "sample size": re.compile(r"(?i)\bpower\s+(?:analys[ie]s|calculations?)\b|\bsample\s+sizes?\b[^.]{0,80}\b(?:calculat|determin|estimat|justif|chosen|based\s+on|predetermin|similar\s+to)\w*|\bno\s+statistical\s+methods?\s+(?:was|were)\s+used\s+to\s+(?:pre-?)?determine\s+sample\s+size"),
    "exclusions": re.compile(r"(?i)\b(?:inclusion|exclusion|eligibility)\s+criteri(?:a|on)\b|\bwere\s+excluded\b|\bno\s+(?:data|animals|samples|participants|subjects|mice)\s+(?:points?\s+)?were\s+excluded\b|\bexcluded\s+from\s+(?:the\s+)?analys\w+"),
    "sex": re.compile(r"(?i)\bsex(?:es)?\s+as\s+a\s+biological\s+variable\b|\bboth\s+sexes\b|\b(?:male|female)\s+and\s+(?:male|female)\b|\bmales?\s+and\s+females?\b|\bfemales?\s+and\s+males?\b|\b(?:male|female)\s+(?:mice|rats|animals|zebrafish|participants|patients|subjects|donors)\b|\bsex\b[^.]{0,60}\b(?:recorded|reported|balanced|considered|self-reported|matched|stratified|covariate|not\s+considered)\b|\bmen\s+and\s+women\b|\bwomen\s+and\s+men\b|\b\d+(?:\.\d+)?\s*%\s+(?:were\s+)?(?:female|male|women|men)\b"),
}
_DESIGN_HINTS = {
    "blinding": "whether investigators were blinded to group allocation during the experiments and outcome assessment",
    "randomization": "whether and how subjects were randomized to groups",
    "sample size": "how the sample size was chosen (a power analysis, or that none was done)",
    "exclusions": "the inclusion and exclusion criteria, and whether any data were excluded",
    "sex": "the sex of the animals or participants, and whether sex was considered in the analysis",
}


_REVIEW_TYPE = re.compile(r"(?i)\breview\b|\bmeta-?analys[ie]s\b")
_REVIEW_TITLE = re.compile(r"(?i)\b(?:systematic|scoping|narrative|umbrella|literature|rapid|mapping)\s+review\b|\bmeta-?analys[ie]s\b")


def _design_check(venue: Venue, values: dict[str, str]) -> Outcome:
    if _REVIEW_TYPE.search(values.get("article_type", "")) or _REVIEW_TITLE.search(values.get("title", "")):
        raise NotApplicable("a review reports no experiment or participants of its own")
    texts = _text_or_skip(venue, values)
    body = "\n".join(t for _, _, t in _body_texts(texts))
    without_hosts = _ANTIBODY_HOST.sub(" ", body)
    animals = len(_ANIMALS.findall(without_hosts)) if _ANIMAL_STUDY.search(without_hosts) else 0
    humans = len(_HUMANS.findall(body)) if _HUMAN_STUDY.search(body) else 0
    # A passing mention ("as in mouse studies") is not a study of animals or people.
    if animals < 3 and humans < 3:
        raise NotApplicable("no study of animals or human participants described")
    subject = "animals" if animals >= humans else "human participants"
    found = {name: bool(p.search(body)) for name, p in _DESIGN.items()}
    if subject == "human participants":
        # Blinding and randomization are trial terms; an observational cohort need not report them.
        trial = re.search(r"(?i)\b(?:trial|intervention|treatment\s+arm|placebo|allocated)\b", body)
        if not trial:
            found.pop("blinding")
            found.pop("randomization")
    missing = [name for name, ok in found.items() if not ok]
    score = f"{len(found) - len(missing)}/{len(found)} design items reported for a study of {subject}"
    issues = []
    if missing:
        what = "; ".join(_DESIGN_HINTS[m] for m in missing)
        issues.append(Issue(WARNING, texts[0][0], f"the methods do not say {what} (MDAR/ARRIVE ask for these, including when they were not done)"))
    return Outcome(issues, score)


# ---------------------------------------------------------------------------
# statistics (statcheck)


def _betacf(a: float, b: float, x: float) -> float:
    tiny, qab, qap, qam = 1e-300, a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(ln) * _betacf(a, b, x) / a
    return 1.0 - math.exp(ln) * _betacf(b, a, 1.0 - x) / b


def gammaincc(a: float, x: float) -> float:
    """The regularized upper incomplete gamma function Q(a, x)."""
    if x <= 0.0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1.0:
        ap, total, delta = a, 1.0 / a, 1.0 / a
        for _ in range(1000):
            ap += 1.0
            delta *= x / ap
            total += delta
            if abs(delta) < abs(total) * 1e-14:
                break
        return 1.0 - total * math.exp(-x + a * math.log(x) - gln)
    tiny = 1e-300
    b, c, d = x + 1.0 - a, 1.0 / tiny, 1.0 / (x + 1.0 - a)
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = b + an / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


def p_value(test: str, stat: float, df1: float | None = None, df2: float | None = None) -> float:
    """Two-tailed p value for a t, z, or r statistic; upper-tail for F and chi-square."""
    if test == "z":
        return math.erfc(abs(stat) / math.sqrt(2.0))
    if test == "t":
        return betainc(df1 / 2.0, 0.5, df1 / (df1 + stat * stat))
    if test == "r":
        if abs(stat) >= 1.0:
            return 0.0
        t = stat * math.sqrt(df1 / (1.0 - stat * stat))
        return p_value("t", t, df1)
    if test == "F":
        return betainc(df2 / 2.0, df1 / 2.0, df2 / (df2 + df1 * stat)) if stat > 0 else 1.0
    if test == "chi2":
        return gammaincc(df1 / 2.0, stat / 2.0)
    raise ValueError(test)


_NUM = r"[-−–]?\s?(?:\d+(?:\.\d+)?|\.\d+)"
# Scientific notation first, so the "1" of "1.5 × 10-5" is not read as p = 1. The exponent may be "10-5", "10^-5", "10^{-5}", "10⁻⁵", or what is left of LaTeX's "\times 10^{-5}".
_P = r"p\s*([<>=≤≥])\s*(\d(?:\.\d+)?\s*(?:[x×]|\\times)?\s*10\s*\^?\s*\{?\s*[-−–⁻]\s*[\d⁰¹²³⁴⁵⁶⁷⁸⁹]+|\d(?:\.\d+)?e[-−–]\d+|0?\.\d+|1(?:\.0+)?(?!\d|\.\d))"
_STAT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("t", re.compile(rf"\bt\s*\(\s*(\d+(?:\.\d+)?)\s*\)\s*=\s*({_NUM})\s*[,;]\s*{_P}", re.IGNORECASE)),
    ("F", re.compile(rf"\bF\s*\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)\s*=\s*({_NUM})\s*[,;]\s*{_P}", re.IGNORECASE)),
    ("r", re.compile(rf"\br\s*\(\s*(\d+)\s*\)\s*=\s*({_NUM})\s*[,;]\s*{_P}", re.IGNORECASE)),
    ("chi2", re.compile(rf"(?:χ\s*2|χ²|\bchi[- ]?squared?|\bX2)\s*\(\s*(\d+)(?:\s*,\s*N\s*=\s*\d+)?\s*\)\s*=\s*({_NUM})\s*[,;]\s*{_P}", re.IGNORECASE)),
    ("z", re.compile(rf"\bz\s*=\s*({_NUM})\s*[,;]\s*{_P}", re.IGNORECASE)),
]


_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
_SCIENTIFIC = re.compile(r"10\s*\^?\s*\{?\s*[-−–⁻]|e[-−–]", re.IGNORECASE)
_ONE_TAILED = re.compile(r"(?i)\b(?:one|1)[- ]?(?:tailed|sided)\b|(?<![\w-])directional\b")
_SENTENCE_BREAK = re.compile(r"[.!?]\s+(?=[A-Z(])")


def _num(text: str) -> float:
    text = re.sub(r"[\s^{}]", "", text.translate(_SUPERSCRIPTS).replace("−", "-").replace("–", "-").replace("\\times", "x"))
    m = re.fullmatch(r"(\d(?:\.\d+)?)[xX×]?10-(\d+)", text)
    if m:
        return float(f"{m.group(1)}e-{m.group(2)}")
    return float(text)


def _decimals(text: str) -> int:
    text = text.strip().lstrip("-−– ")
    return len(text.split(".", 1)[1]) if "." in text and not _SCIENTIFIC.search(text) else 0


def _p_unit(text: str, value: float) -> float:
    """Half a unit in the last place of a reported p value, the error its rounding allows."""
    if not _SCIENTIFIC.search(text):
        decimals = _decimals(text)
        return 0.5 * 10**-decimals if decimals else 0.0
    mantissa = re.match(r"\d(?:\.(\d+))?", text.strip())
    return 0.5 * 10 ** (math.floor(math.log10(value)) - len(mantissa.group(1) or "")) if value > 0 and mantissa else 0.0


def reported_results(text: str) -> list[dict]:
    """Every APA-style test result in ``text``, recomputed."""
    out: list[dict] = []
    for test, pattern in _STAT_PATTERNS:
        for m in pattern.finditer(text):
            groups = m.groups()
            if test == "F":
                df1, df2, stat_s, comp, p_s = float(groups[0]), float(groups[1]), groups[2], groups[3], groups[4]
            elif test == "z":
                df1, df2, stat_s, comp, p_s = None, None, groups[0], groups[1], groups[2]
            else:
                df1, df2, stat_s, comp, p_s = float(groups[0]), None, groups[1], groups[2], groups[3]
            try:
                stat, reported = _num(stat_s), _num(p_s)
            except ValueError:
                continue
            if test == "F" and stat < 0 or test == "chi2" and stat < 0:
                continue
            half = 0.5 * 10 ** -_decimals(stat_s) if _decimals(stat_s) else 0.5
            mag = abs(stat)
            lo_stat, hi_stat = max(mag - half, 0.0), mag + half
            if test == "r":
                hi_stat = min(hi_stat, 0.999999)
            p_hi, p_lo = p_value(test, lo_stat, df1, df2), p_value(test, hi_stat, df1, df2)
            computed = p_value(test, mag, df1, df2)
            head = max(0, m.start() - 600)
            breaks = list(_SENTENCE_BREAK.finditer(text, head, m.start()))
            sentence_start = breaks[-1].end() if breaks else head
            one_tailed = bool(_ONE_TAILED.search(text[sentence_start : m.end() + 40]))
            out.append({"test": test, "text": " ".join(m.group(0).split()), "comparison": comp, "reported": reported, "p_decimals": _decimals(p_s), "p_unit": _p_unit(p_s, reported), "computed": computed, "range": (p_lo, p_hi), "one_tailed": one_tailed})
    return out


def _consistent(r: dict, lo: float, hi: float) -> bool:
    comp, rep = r["comparison"], r["reported"]
    if comp in ("<", "≤"):
        return lo < rep or (comp == "≤" and lo <= rep)
    if comp in (">", "≥"):
        return hi > rep or (comp == "≥" and hi >= rep)
    return lo - r["p_unit"] <= rep <= hi + r["p_unit"]


def judge(r: dict) -> str:
    """``"ok"``, ``"inconsistent"``, or ``"decision error"`` for one recomputed result."""
    lo, hi = r["range"]
    if _consistent(r, lo, hi) or (r["one_tailed"] and _consistent(r, lo / 2, hi / 2)):
        return "ok"
    alpha = 0.05
    rep, comp = r["reported"], r["comparison"]
    reported_sig = rep < alpha or (rep == alpha and comp in ("<", "≤", "="))  # p = .05 counts as significant, as in statcheck
    computed_sig = (lo / 2 if r["one_tailed"] else lo) < alpha
    return "decision error" if reported_sig != computed_sig else "inconsistent"


def _statistics_check(venue: Venue, values: dict[str, str]) -> Outcome:
    texts = _text_or_skip(venue, values)
    issues: list[Issue] = []
    total = 0
    for field_id, name, text in texts:
        for r in reported_results(text):
            total += 1
            verdict = judge(r)
            if verdict == "ok":
                continue
            what = "a decision error: the recomputed p value is on the other side of .05" if verdict == "decision error" else "the statistic and its p value do not agree"
            issues.append(Issue(WARNING, field_id, f"{name}: \"{r['text']}\" -- recomputed p = {r['computed']:.3g}; {what}"))
    if not total:
        raise NotApplicable("no test statistics reported with degrees of freedom and a p value (APA style)")
    return Outcome(issues, f"recomputed {total} reported test result(s)")


# ---------------------------------------------------------------------------
# trial registration

# ClinicalTrials.gov and the WHO primary registries. PROSPERO's CRD numbers
# register reviews, not trials, so they do not count.
TRIAL_ID = re.compile(
    r"\b(?:(?i:NCT)[\s-]?\d{8}|(?i:ISRCTN)[\s-]?\d{8}|ACTRN\s?\d{14}|ChiCTR-?(?:[A-Z]{2,4}-?)?\d{8,}|CTRI/\d{4}/\d{2,3}/\d{6}|DRKS\s?\d{8}|NTR\d{3,5}|NL-OMON\d+|IRCT\d{8,}N?\d*|UMIN\d{9}|jRCTs?\d{9,}|JPRN-[A-Za-z]+\d+|KCT\d{7}|PACTR\d{15}|RBR-[0-9a-z]{6,}|SLCTR/\d{4}/\d+|TCTR\d{11}|RPCEC\d{11}|LBCTR\d{10}|20\d{2}-5\d{5}-\d{2}-\d{2})\b"
    r"|(?i:EudraCT)[^.\n]{0,40}?\b20\d{2}-\d{6}-\d{2}\b"
)
_REGISTRY_NAMED = re.compile(r"(?i)\b(?:clinicaltrials\.gov|isrctn|anzctr|eudract|chictr|ictrp|trial\s+registr(?:y|ation)|prospectively\s+registered)\b")
# "Trial" alone is everyday wording in psychology and machine learning, so a
# manuscript counts only when it calls itself a trial of a clinical kind.
_TRIAL_KIND = r"(?:clinical|randomi[sz]ed|controlled|placebo[- ]controlled|double[- ]blind(?:ed)?|single[- ]blind(?:ed)?|open[- ]label|phase\s+(?:[1-4]|IV|I{1,3})(?:/(?:[1-4]|IV|I{1,3}))?[ab]?|cross-?over|multi-?cent(?:er|re)|pragmatic)"
_WORDS = r"(?:[\w-]+,?\s+)"
_SELF_TRIAL = re.compile(
    rf"(?i)\b(?:this|our|the\s+present|the\s+current)\s+{_WORDS}{{0,4}}?{_TRIAL_KIND},?\s+{_WORDS}{{0,3}}?trial\b"
    rf"|\bwe\s+(?:conducted|performed|carried\s+out|ran|undertook|initiated|report(?:ed)?|present|describe)\s+(?:the\s+(?:results|findings|outcomes)\s+(?:of|from)\s+)?(?:a|an|the)\s+{_WORDS}{{0,5}}?{_TRIAL_KIND},?\s+{_WORDS}{{0,3}}?trial\b"
    # People assigned to a health intervention; "participants were randomly
    # assigned to conditions" in a behavioural experiment does not count.
    r"|\b(?:patients|participants|subjects|volunteers)\b[^.]{0,60}\b(?:randomly\s+(?:assigned|allocated)|randomi[sz]ed)\b[^.]{0,120}\b(?:placebo|treatment|therapy|drug|dose|intervention|standard\s+of\s+care|usual\s+care|vaccine)\b"
)
# Not an "institutional review board", "ethics review", or "chart review".
_REVIEW_KIND = r"(?:systematic\s+review|meta-?analysis|scoping\s+review|narrative\s+review|(?<!institutional\s)(?<!ethics\s)(?<!ethical\s)(?<!peer\s)(?<!peer-)(?<!chart\s)(?<!safety\s)(?<!internal\s)review(?!\s+(?:boards?|committees?|panels?)\b))"
_SELF_REVIEW = re.compile(rf"(?i)\b(?:this|our|the\s+present|the\s+current)\s+{_WORDS}{{0,3}}?{_REVIEW_KIND}\b|\bwe\s+(?:conducted|performed|carried\s+out|undertook|present|report)\s+(?:a|an)\s+{_WORDS}{{0,3}}?{_REVIEW_KIND}\b")
_REVIEW_TITLE = re.compile(r"(?i)\b(?:systematic\s+review|meta-?analysis|scoping\s+review)\b")
_NOT_A_STUDY = re.compile(r"(?i)\b(?:review|editorial|commentary|comment|perspective|viewpoint|opinion|essay)\b")


def _trial_reason(venue: Venue, values: dict[str, str], body: str) -> Optional[str]:
    """Why this manuscript counts as reporting a clinical trial, or None when it does not."""
    # What the author declared in the .sub beats anything read from the text.
    declared = values.get("clinical_trial", "").strip().lower()
    if declared == "yes":
        return "the submission form says this manuscript reports a clinical trial"
    if declared == "no":
        return None
    base = get_requirements(venue.slug)
    article_type = values.get((base.article_type_field if base else "") or "article_type", "").strip()
    if re.search(r"(?i)\btrial\b", article_type):
        return f"the article type is '{article_type}'"
    if _NOT_A_STUDY.search(article_type):
        return None
    # A review describes other people's trials in the words a trial uses.
    if _SELF_REVIEW.search(body) or _REVIEW_TITLE.search(body[:300]):
        return None
    match = _SELF_TRIAL.search(body)
    return f"the manuscript appears to report a clinical trial (\"{' '.join(match.group(0).split())}\")" if match else None


def _trial_registration_check(venue: Venue, values: dict[str, str]) -> Outcome:
    from .manuscript import without_reference_list

    texts = _texts(venue, values, roles=("manuscript", "title_page"))
    if not texts:
        raise _no_text(venue, values, ("manuscript", "title_page"), "manuscript text")
    body = "\n".join(without_reference_list(text) for _, _, text in texts)
    found = list(dict.fromkeys(" ".join(m.group(0).split()) for m in TRIAL_ID.finditer(body)))
    if found:
        return Outcome([], f"trial registry ID(s) given: {', '.join(found[:5])}")
    reason = _trial_reason(venue, values, body)
    if reason is None:
        raise NotApplicable("the manuscript does not report a clinical trial")
    gap = "it mentions trial registration but gives no registry ID" if _REGISTRY_NAMED.search(body) else "no trial registry ID was found"
    return Outcome([Issue(WARNING, texts[0][0], f"{reason}, but {gap} (for example NCT01234567 or ISRCTN12345678). ICMJE journals require trials to be registered and the number given in the manuscript; ignore this if the manuscript does not report a trial")], "no registry ID found")


# ---------------------------------------------------------------------------
# tasks for a reader

_TRIAL_TASK = (
    "Decide whether this manuscript reports a clinical trial under the ICMJE definition: a study that prospectively assigns people or groups of people to a health-related intervention, with or without a comparison group, to evaluate its effect on a health outcome. "
    "If it does not, return not_applicable; that covers observational studies, behavioural experiments with no health outcome, reviews, and animal or computational work. "
    "If it does, find the registration number of this trial and name its registry. A secondary analysis counts and must give the parent trial's number. The number of another trial the manuscript cites does not count. "
    "Fail if the number is missing, quoting any sentence that claims registration without one. If the manuscript gives both the registration date and the date enrollment began and registration came after, fail and state both dates unless the manuscript explains why."
)
_GUIDELINE_TASK = (
    "Identify the study design and the reporting guideline it calls for: systematic review or meta-analysis, PRISMA 2020 (scoping review, PRISMA-ScR; network meta-analysis, PRISMA-NMA; meta-analysis of observational studies, MOOSE or PRISMA); randomized trial, CONSORT; trial protocol, SPIRIT; cohort, case-control, or cross-sectional study, STROBE; diagnostic accuracy, STARD; clinical prediction model, TRIPOD or TRIPOD+AI; case report, CARE; qualitative research, SRQR or COREQ; animal research, ARRIVE; health economic evaluation, CHEERS; quality improvement, SQUIRE. "
    "A review is systematic only if it reports a reproducible search and predefined inclusion criteria. Return not_applicable when no guideline applies: a narrative or invited review, perspective, editorial, theory, software, methods, in vitro, or purely computational paper. Never apply PRISMA to a narrative review. "
    "When a guideline applies, name the design and the guideline, then check that the manuscript says it followed that guideline and has what the guideline asks to be submitted: for PRISMA, a flow diagram of records identified, screened, and included, a completed checklist among the files or a statement that one is provided, and whether the protocol was registered (PROSPERO, for example) or a statement that it was not; for CONSORT, a participant flow diagram and a completed checklist. "
    "Fail naming each missing item. Accept an equivalent statement in any section, and the guideline's extension where one fits. Follow the named venue's requirements and the selected article type when they are supplied."
)


def _queue_reader_tasks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    from .requirements_check import _existing, _fields_for, _paths

    def files(*roles: str) -> list[str]:
        return [str(p) for role in roles for field in _fields_for(venue, role) for p in _existing(_paths(field, values))]

    if not files("manuscript"):
        return
    checks.needs_ai(AiTask(id="ai.rigor.trial_registration", title="A clinical trial gives its own registration number", category=CATEGORY, instructions=_TRIAL_TASK, files=files("manuscript", "title_page")))
    base = get_requirements(venue.slug)
    required = (resolve(base, values).statements.required or []) if base else []
    note = " This venue requires a reporting-guideline statement." if "reporting_guidelines" in required else ""
    checks.needs_ai(AiTask(id="ai.rigor.reporting_guideline", title="Follows the reporting guideline for its study design (PRISMA for systematic reviews, CONSORT for trials, ...)", category=CATEGORY, instructions=_GUIDELINE_TASK + note, files=files("manuscript", "supplementary", "figures")))


# ---------------------------------------------------------------------------


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    online = bool(checks.options.get("check_registries", True))
    checks.run("rigor.rrids", "Antibodies, cell lines, and organisms carry RRIDs that resolve", CATEGORY, _rrids_check, venue, values, online)
    checks.run("rigor.cell_lines", "Cell lines authenticated, tested for mycoplasma, and not known to be misidentified", CATEGORY, _cell_lines_check, venue, values, online)
    checks.run("rigor.design", "Blinding, randomization, sample size, exclusions, and sex reported for animal and human studies", CATEGORY, _design_check, venue, values)
    checks.run("rigor.statistics", "Reported test statistics agree with their p values (statcheck)", CATEGORY, _statistics_check, venue, values)
    checks.run("rigor.trial_registration", "A clinical trial gives a trial registry ID", CATEGORY, _trial_registration_check, venue, values)
    _queue_reader_tasks(checks, venue, values)
