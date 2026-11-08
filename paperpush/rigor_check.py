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

from .database import Venue
from .validate import WARNING, Checks, Issue, NotApplicable, Outcome

logger = logging.getLogger(__name__)

CATEGORY = "Rigor and reproducibility"
_USER_AGENT = "paperpush-validate (+https://github.com/pachterlab/paperpush)"
_TIMEOUT = 15
MAX_LOOKUPS = 40

# ---------------------------------------------------------------------------
# text


def _texts(venue: Venue, values: dict[str, str]) -> list[tuple[str, str, str]]:
    """``(field id, file name, text)`` of the manuscript and supplementary files that can be read."""
    from . import document
    from .requirements_check import _existing, _fields_for, _paths

    out: list[tuple[str, str, str]] = []
    for role in ("manuscript", "supplementary"):
        for field in _fields_for(venue, role):
            for path in _existing(_paths(field, values)):
                if path.suffix.lower() not in (".pdf", ".docx", ".tex", ".zip", ".md", ".markdown", ".txt"):
                    continue
                text = document.get(path).text
                if text:
                    out.append((field.id, path.name, re.sub(r"-\n(?=[a-z])", "", text)))
    return out


def _text_or_skip(venue: Venue, values: dict[str, str]) -> list[tuple[str, str, str]]:
    texts = _texts(venue, values)
    if not texts:
        raise NotApplicable("no manuscript or supplementary text to read")
    return texts


# ---------------------------------------------------------------------------
# research resources

_VENDORS = r"(?:Abcam|Cell\s+Signal(?:l)?ing(?:\s+Technology)?|CST|Thermo(?:\s*Fisher)?|Invitrogen|BioLegend|Santa\s+Cruz|Sigma(?:-Aldrich)?|Merck|Millipore|R&D\s+Systems|BD\s+(?:Biosciences|Pharmingen)|Proteintech|Novus|Bio-Rad|Jackson\s+ImmunoResearch|eBioscience|Miltenyi|Dako|Agilent|Abnova|GeneTex|Synaptic\s+Systems|Bethyl|Active\s+Motif|Diagenode|Leica|Vector\s+Laboratories|Rockland|Tonbo|Developmental\s+Studies\s+Hybridoma\s+Bank|DSHB|Antibodies-online|Atlas\s+Antibodies|Addgene)"
_CATALOG = r"(?:cat(?:alog(?:ue)?)?\.?\s*(?:no\.?|#|number)|clone\s+[A-Z0-9][\w.-]*|#\s*\w*\d)"
_ANTIBODY = re.compile(rf"(?i:\banti-?[A-Za-z0-9][\w/-]*\b|\bantibod(?:y|ies)\b)(?=[^.\n]{{0,160}}(?:{_VENDORS}|(?i:{_CATALOG})))")
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


_UNIT_BREAK = re.compile(r"\n|(?<=[.!?])\s+(?=[A-Z(])")


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
    for pattern, kind in ((_CELL_LINE, "cell line"), (_GENERIC_CELL_LINE, "cell line"), (_STRAIN, "organism"), (_SOFTWARE, "software")):
        for m in pattern.finditer(text):
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
    texts = _text_or_skip(venue, values)
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
    texts = _text_or_skip(venue, values)
    lines: dict[str, str] = {}
    whole = "\n".join(t for _, _, t in texts)
    for field_id, _, text in texts:
        for m in _CELL_LINE.finditer(text):
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
_HUMANS = re.compile(r"(?i)\b(?:participants|patients|volunteers|human\s+subjects|respondents|cohort|enrolled|recruited)\b")
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


def _design_check(venue: Venue, values: dict[str, str]) -> Outcome:
    texts = _text_or_skip(venue, values)
    whole = "\n".join(t for _, _, t in texts)
    from .manuscript import truncate_at_references

    body = truncate_at_references(whole)
    animals = len(_ANIMALS.findall(body))
    humans = len(_HUMANS.findall(body))
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
_P = r"p\s*([<>=≤≥])\s*(0?\.\d+|1(?:\.0+)?|\d(?:\.\d+)?\s*[x×]\s*10\s*[-−–]\s*\d+|\d(?:\.\d+)?e[-−–]\d+)"
_STAT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("t", re.compile(rf"\bt\s*\(\s*(\d+(?:\.\d+)?)\s*\)\s*=\s*({_NUM})\s*[,;]\s*{_P}", re.IGNORECASE)),
    ("F", re.compile(rf"\bF\s*\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)\s*=\s*({_NUM})\s*[,;]\s*{_P}", re.IGNORECASE)),
    ("r", re.compile(rf"\br\s*\(\s*(\d+)\s*\)\s*=\s*({_NUM})\s*[,;]\s*{_P}", re.IGNORECASE)),
    ("chi2", re.compile(rf"(?:χ\s*2|χ²|\bchi[- ]?squared?|\bX2)\s*\(\s*(\d+)(?:\s*,\s*N\s*=\s*\d+)?\s*\)\s*=\s*({_NUM})\s*[,;]\s*{_P}", re.IGNORECASE)),
    ("z", re.compile(rf"\bz\s*=\s*({_NUM})\s*[,;]\s*{_P}", re.IGNORECASE)),
]


def _num(text: str) -> float:
    text = text.replace("−", "-").replace("–", "-").replace(" ", "")
    m = re.match(r"(\d(?:\.\d+)?)[x×]10-(\d+)", text)
    if m:
        return float(m.group(1)) * 10 ** -int(m.group(2))
    return float(text)


def _decimals(text: str) -> int:
    text = text.strip().lstrip("-−– ")
    return len(text.split(".", 1)[1]) if "." in text and "e" not in text.lower() and "x" not in text.lower() and "×" not in text else 0


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
            sentence_start = text.rfind(".", 0, m.start())
            one_tailed = bool(re.search(r"(?i)one[- ](?:tailed|sided)", text[max(0, sentence_start) : m.end() + 40]))
            out.append({"test": test, "text": " ".join(m.group(0).split()), "comparison": comp, "reported": reported, "p_decimals": _decimals(p_s), "computed": computed, "range": (p_lo, p_hi), "one_tailed": one_tailed})
    return out


def _consistent(r: dict, lo: float, hi: float) -> bool:
    comp, rep = r["comparison"], r["reported"]
    if comp in ("<", "≤"):
        return lo < rep or (comp == "≤" and lo <= rep)
    if comp in (">", "≥"):
        return hi > rep or (comp == "≥" and hi >= rep)
    unit = 0.5 * 10 ** -r["p_decimals"] if r["p_decimals"] else 0.0
    return lo - unit <= rep <= hi + unit


def judge(r: dict) -> str:
    """``"ok"``, ``"inconsistent"``, or ``"decision error"`` for one recomputed result."""
    lo, hi = r["range"]
    if _consistent(r, lo, hi) or (r["one_tailed"] and _consistent(r, lo / 2, hi / 2)):
        return "ok"
    alpha = 0.05
    rep, comp = r["reported"], r["comparison"]
    reported_sig = rep < alpha or (rep == alpha and comp in ("<", "≤"))
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


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    online = bool(checks.options.get("check_registries", True))
    checks.run("rigor.rrids", "Antibodies, cell lines, and organisms carry RRIDs that resolve", CATEGORY, _rrids_check, venue, values, online)
    checks.run("rigor.cell_lines", "Cell lines authenticated, tested for mycoplasma, and not known to be misidentified", CATEGORY, _cell_lines_check, venue, values, online)
    checks.run("rigor.design", "Blinding, randomization, sample size, exclusions, and sex reported for animal and human studies", CATEGORY, _design_check, venue, values)
    checks.run("rigor.statistics", "Reported test statistics agree with their p values (statcheck)", CATEGORY, _statistics_check, venue, values)
