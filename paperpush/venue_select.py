"""Rank the supported venues against a loose user query or a manuscript.

The submission pipeline is slug-driven -- ``subfile``, ``login``, and ``submit``
all take a venue slug, which presumes the user already knows the exact slug of
their target venue. This module inverts that: it turns free-form input into an
ordered shortlist of venues, so a human or an AI agent can go from "I want to
put a preprint somewhere" -- or from a title and abstract -- to
``paperpush subfile biorxiv`` without guessing at the slug first.

Two kinds of input, scored differently and added together:

**A venue name** ("nature methods", "neuro"): scored against each venue's slug,
name, and full name in ``venues.json`` (:func:`_score_name`).

**Subjects**: every venue carries ``keywords`` from the fixed vocabulary
:data:`~paperpush.database.VENUE_KEYWORDS` (biology, neuroscience, machine
learning, ...). A vocabulary term given as a keyword, and subjects inferred
from a manuscript, score venues by how much of the
manuscript's subject matter they cover and how focused they are on it.

A manuscript's title, abstract, and keywords -- given separately, or read from
the file by :func:`extract_manuscript` -- are mapped to subjects by
:func:`infer_subjects`, a built-in word list per subject in which the title and
keywords count three times as much as the abstract. That runs offline and is
deterministic, but it only reads words. :func:`rank_via_api` instead asks an
LLM (any provider ``paperpush autofill --engine api`` supports) to choose the
subjects and rank the venues with a reason for each; agents on the MCP server
read the manuscript themselves and pass the subjects as keywords.
:func:`select` takes all of these.

Either way this ranks thematic fit among the venues paperpush supports only --
not impact, scope details, or the likelihood of acceptance.

**Any journal** (:func:`find_journals`, the ``pick-venue`` default for a
manuscript): the works most similar to the manuscript are looked up in OpenAlex
(:mod:`paperpush.openalex`) and grouped by the journal (or preprint server)
they appeared in, ranked by how many similar works each published, how closely
they match, and how recent they are. Journals paperpush supports are marked
with their slug. ``--paperpush-supported-only`` keeps to the ranking above.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .database import VENUE_KEYWORDS, Venue, list_venues
from .venues import SLUG_TO_MODULE, submission_base

logger = logging.getLogger(__name__)

# Function words ignored in a venue-name search ("journal of ...").
_STOPWORDS = frozenset("a an and as at by for from in into is of on or the to with".split())

# Manuscript formats read through :mod:`paperpush.document`; any other file is
# read as plain text (a title and abstract pasted into a .txt, say).
MANUSCRIPT_SUFFIXES = (".tex", ".zip", ".docx", ".pdf")

# How much of a manuscript's body is used when no abstract can be found.
_FALLBACK_CHARS = 6000

# Words and stems that signal each subject in ``VENUE_KEYWORDS``. A term of five
# or more characters matches as a word prefix ("immun" -> "immunity",
# "immunology"); a shorter one must be the whole word, optionally plural ("mri",
# "cell"/"cells"), so "pet" does not match "petri". Phrases match with any
# whitespace or hyphen between their words. "general" has no terms: it is a
# venue's breadth, never a manuscript's subject.
SUBJECT_TERMS: dict[str, tuple[str, ...]] = {
    "biology": (
        "biolog", "cell", "cellular", "protein", "organism", "molecular", "tissue", "enzym", "metabol",
        "evolution", "species", "ecolog", "microb", "bacteri", "plant", "yeast", "mouse", "mice",
        "zebrafish", "drosophila", "elegans", "embryo", "developmental", "stem cell", "organoid",
        "signaling", "signalling", "receptor", "membrane", "mitochond", "physiolog", "biochem",
    ),
    "genetics": (
        "gene", "genom", "genetic", "allele", "variant", "gwas", "heritab", "mutation", "mutant",
        "chromosom", "chromatin", "polymorphism", "snp", "epigen", "methylation", "crispr",
        "transcriptom", "eqtl", "ancestry", "linkage", "haplotyp", "exome", "sequencing", "dna", "rna",
        "transcription factor", "enhancer", "splicing",
    ),
    "bioinformatics": (
        "bioinformatic", "computational biology", "single cell", "scrna", "rna seq", "sequencing data",
        "read alignment", "read mapping", "aligner", "genome assembly", "assembler", "phylogen",
        "pipeline", "software", "k mer", "kmer", "annotation", "spatial transcriptom", "multiome",
        "multi omic", "omics", "proteomic", "metagenom", "pseudoalign", "differential expression",
        "clustering", "database",
    ),
    "immunology": (
        "immun", "t cell", "b cell", "nk cell", "antibod", "antigen", "cytokin", "lymphocyt",
        "macrophag", "neutrophil", "inflammat", "vaccin", "autoimmun", "mhc", "hla", "interleukin",
        "dendritic cell", "tcr", "bcr", "infection", "pathogen", "viral", "virus",
    ),
    "neuroscience": (
        "neuro", "neuron", "neural circuit", "brain", "cortex", "cortical", "synap", "hippocamp",
        "cognit", "fmri", "eeg", "behavior", "behaviour", "axon", "glia", "astrocyt", "memory",
        "perception", "sensory", "motor", "spike", "spiking", "electrophysiolog", "optogenetic",
        "dopamin", "cerebell", "thalam",
    ),
    "medicine": (
        "patient", "clinical", "clinic", "trial", "disease", "therap", "treatment", "diagnos",
        "hospital", "cohort", "mortality", "morbidity", "cancer", "tumor", "tumour", "oncolog",
        "randomi", "placebo", "epidemiolog", "health", "drug", "symptom", "surger", "surgical",
        "prognos", "biomarker", "diabet", "cardiovascular", "covid", "sars cov", "pediatric",
        "paediatric", "public health", "medical", "medicine",
    ),
    "radiology": (
        "radiolog", "mri", "magnetic resonance", "ct", "computed tomograph", "x ray", "radiograph",
        "ultrasound", "sonograph", "pet", "positron emission", "mammogra", "imaging", "lesion",
        "scan", "nuclear medicine", "radiotracer", "contrast agent",
    ),
    "machine learning": (
        "machine learning", "deep learning", "neural network", "transformer", "reinforcement learning",
        "supervised", "unsupervised", "self supervised", "representation learning", "generalization",
        "gradient", "stochastic", "optimiz", "classifier", "benchmark", "large language model", "llm",
        "embedding", "diffusion model", "generative model", "graph neural", "attention", "fine tun",
        "pretrain", "pre train", "training data", "overfitting", "kernel method",
    ),
    "computer vision": (
        "computer vision", "image", "images", "vision", "visual", "object detection", "segmentation",
        "video", "camera", "pixel", "convolutional", "cnn", "recognition", "3d reconstruction",
        "pose estimation", "scene", "optical flow", "point cloud", "depth estimation", "rendering",
    ),
    "natural language processing": (
        "natural language", "nlp", "language model", "text", "token", "machine translation",
        "translation", "sentiment", "parsing", "corpus", "corpora", "linguistic", "question answering",
        "summariz", "dialogue", "dialog", "speech", "named entity", "llm", "prompt",
    ),
    "robotics": (
        "robot", "manipulat", "locomotion", "grasp", "autonomous", "actuator", "control policy",
        "navigation", "drone", "legged", "humanoid", "sim to real", "teleoperat", "trajectory",
        "motion planning", "slam",
    ),
    "math": (
        "theorem", "proof", "prove", "lemma", "conjecture", "corollary", "algebra", "topolog",
        "mathemat", "math", "combinator", "graph theory", "manifold", "prime", "polynomial", "inequalit", "group theory",
        "geometr", "number theory", "permutation", "hypergraph", "extremal", "asymptotic",
    ),
    "statistics": (
        "statistic", "regression", "bayesian", "inference", "estimator", "estimation", "likelihood",
        "hypothesis test", "p value", "confidence interval", "variance", "sampling", "causal",
        "bootstrap", "nonparametric", "posterior", "mixed model", "false discovery", "multiple testing",
        "survival analysis", "monte carlo", "markov chain",
    ),
    "physics": (
        "physics", "quantum", "particle", "photon", "laser", "optic", "thermodynam", "condensed matter",
        "superconduct", "magnetic field", "plasma", "cosmolog", "gravit", "relativit", "spin",
        "lattice", "semiconductor", "fluid dynamic", "turbulen", "electron", "phonon", "boson",
        "fermion", "astrophys",
    ),
    "computer science": (
        "computer science", "algorithm", "complexity", "computation", "computational", "software", "distributed system",
        "compiler", "network", "security", "cryptograph", "programming", "data structure",
        "np hard", "polynomial time", "approximation algorithm", "runtime", "scalab", "parallel",
    ),
}

# Where in a manuscript a term was found, and how much it counts there: the
# title and the author's own keywords say what the paper is about more surely
# than any one sentence of the abstract.
_SECTION_WEIGHTS = {"title": 3, "keywords": 3, "abstract": 1}

# Each term counts at most this many times per section, so one word repeated
# through an abstract does not swamp the rest.
_MAX_TERM_HITS = 3

# A subject is kept when it scores at least this much and at least this share
# of the strongest subject; at most ``_MAX_SUBJECTS`` are kept.
_MIN_SUBJECT_SCORE = 2
_MIN_SUBJECT_SHARE = 0.25
_MAX_SUBJECTS = 4


def _term_pattern(term: str) -> str:
    words = [re.escape(w) for w in re.split(r"[\s-]+", term) if w]
    body = r"[\s-]+".join(words)
    if len(term) >= 5:
        return rf"\b{body}"
    return rf"\b{body}(?:e?s)?\b"


_SUBJECT_PATTERNS: dict[str, list[tuple[str, re.Pattern[str]]]] = {subject: [(term, re.compile(_term_pattern(term), re.IGNORECASE)) for term in terms] for subject, terms in SUBJECT_TERMS.items()}


@dataclass(frozen=True)
class VenueHit:
    """A venue and how well it matched the query.

    ``score`` orders the shortlist (higher is better). ``reasons`` lists, in
    descending weight, the fields that matched a token -- e.g. ``slug`` beat
    ``name`` -- and ``keywords: ...`` for the venue subjects that matched, so a
    user can see why a venue ranked where it did.
    """

    venue: Venue
    score: int
    portal: str
    reasons: list[str]


@dataclass(frozen=True)
class ManuscriptQuery:
    """What a manuscript (or pasted text) says it is about.

    ``source`` names where it came from, for messages. ``body`` holds text used
    in place of an abstract when none could be found.
    """

    title: str = ""
    abstract: str = ""
    keywords: tuple[str, ...] = ()
    source: str = ""
    body: str = ""

    def sections(self) -> dict[str, str]:
        """Text per section, for :func:`infer_subjects`."""
        return {"title": self.title, "keywords": ", ".join(self.keywords), "abstract": self.abstract or self.body}

    def is_empty(self) -> bool:
        return not any(self.sections().values())

    def to_dict(self) -> dict:
        return {"source": self.source, "title": self.title, "abstract": self.abstract, "keywords": list(self.keywords), "abstract_found": bool(self.abstract)}

    def prompt_text(self) -> str:
        """The text shown to an LLM."""
        parts = []
        if self.title:
            parts.append(f"Title: {self.title}")
        if self.keywords:
            parts.append(f"Keywords: {', '.join(self.keywords)}")
        if self.abstract:
            parts.append(f"Abstract: {self.abstract}")
        elif self.body:
            parts.append(f"Text (no abstract found; the start of the manuscript):\n{self.body}")
        return "\n\n".join(parts)


@dataclass(frozen=True)
class SubjectMatch:
    """A subject inferred from text: its share of the evidence and the terms behind it."""

    subject: str
    score: int
    weight: float  # (score / strongest score) ** 2: the strongest subject is 1.0 and weaker ones fall off fast
    terms: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"subject": self.subject, "score": self.score, "weight": round(self.weight, 3), "terms": list(self.terms)}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


_KEYWORDS_LINE = re.compile(r"^\s*(?:key\s*words?|index terms)\s*[:.—-]\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_ABSTRACT_LABEL = re.compile(r"^\s*abstract\s*[:.—-]?\s*", re.IGNORECASE)


def query_from_text(text: str, source: str = "text") -> ManuscriptQuery:
    """A query from pasted text: a title and/or abstract, maybe a keywords line.

    A short first line followed by more text is taken as the title; a
    ``Keywords:`` line supplies keywords; the rest is the abstract.
    """
    text = text.strip()
    keywords: tuple[str, ...] = ()
    m = _KEYWORDS_LINE.search(text)
    if m:
        keywords = tuple(k.strip(" .") for k in re.split(r"[,;·•|]", m.group(1)) if k.strip(" ."))
        text = (text[: m.start()] + text[m.end() :]).strip()
    title = ""
    first, _, rest = text.partition("\n")
    if rest.strip() and len(first.split()) <= 30 and not _ABSTRACT_LABEL.match(first):
        title, text = first.strip(), rest.strip()
    abstract = _clean(_ABSTRACT_LABEL.sub("", text, count=1))
    return ManuscriptQuery(title=_clean(title), abstract=abstract, keywords=keywords, source=source)


def extract_manuscript(path: Path) -> ManuscriptQuery:
    """The title, abstract, and keywords of a manuscript file.

    ``.tex``/``.zip``/``.docx``/``.pdf`` go through the same readers ``paperpush
    validate`` uses; anything else is read as plain text (:func:`query_from_text`).
    When no abstract can be found, the start of the manuscript text stands in
    for it (``body``). Raises :class:`ValueError` if the file cannot be read.
    """
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"no such file: {path}")
    if path.suffix.lower() not in MANUSCRIPT_SUFFIXES:
        try:
            return query_from_text(path.read_text(encoding="utf-8", errors="replace"), source=str(path))
        except OSError as exc:
            raise ValueError(f"cannot read {path}: {exc}") from exc

    from . import document
    from .consistency import manuscript_keywords, manuscript_title
    from .front_matter_check import manuscript_abstract
    from .manuscript import _tex_to_text

    doc = document.get(path)
    title = abstract = ""
    keywords: list[str] = []
    try:
        title = manuscript_title(doc) or ""
    except Exception:  # noqa: BLE001 -- a best-effort read; any parser failure just means no title
        logger.debug("pick-venue: no title from %s", path, exc_info=True)
    try:
        found = manuscript_abstract(doc)
        if found:
            raw, is_tex = found
            abstract = _tex_to_text(raw) if is_tex else raw
    except Exception:  # noqa: BLE001
        logger.debug("pick-venue: no abstract from %s", path, exc_info=True)
    try:
        keywords = manuscript_keywords(doc) or []
    except Exception:  # noqa: BLE001
        logger.debug("pick-venue: no keywords from %s", path, exc_info=True)
    body = ""
    if not abstract:
        text = doc.text
        if not text and not title:
            raise ValueError(f"cannot read any text from {path}")
        body = _clean((text or "")[:_FALLBACK_CHARS])
    return ManuscriptQuery(title=_clean(title), abstract=_clean(abstract), keywords=tuple(_clean(k) for k in keywords if k.strip()), source=str(path), body=body)


def infer_subjects(query: ManuscriptQuery | str, max_subjects: int = _MAX_SUBJECTS) -> list[SubjectMatch]:
    """The venue subjects (``VENUE_KEYWORDS``) a text is about, strongest first.

    Counts :data:`SUBJECT_TERMS` hits, weighted by section (title and keywords
    over abstract) and capped per term, and keeps the subjects that score well
    against the strongest one. Returns ``[]`` when nothing matches.
    """
    if isinstance(query, str):
        query = query_from_text(query)
    totals: dict[str, int] = {}
    terms_seen: dict[str, list[str]] = {}
    for section, text in query.sections().items():
        if not text:
            continue
        weight = _SECTION_WEIGHTS[section]
        # Longest matches claim their words first, so "T cell" counts for
        # immunology and not also for biology's "cell", and "RNA-seq" for
        # bioinformatics and not also for genetics' "RNA".
        found = [(m.start(), m.end(), subject, term) for subject, patterns in _SUBJECT_PATTERNS.items() for term, pattern in patterns for m in pattern.finditer(text)]
        found.sort(key=lambda f: (-(f[1] - f[0]), f[0]))
        taken: list[tuple[int, int]] = []
        hits: dict[tuple[str, str], int] = {}
        for start, end, subject, term in found:
            if any(start < t_end and t_start < end for t_start, t_end in taken):
                continue
            taken.append((start, end))
            hits[(subject, term)] = hits.get((subject, term), 0) + 1
        for (subject, term), count in hits.items():
            totals[subject] = totals.get(subject, 0) + min(count, _MAX_TERM_HITS) * weight
            if term not in terms_seen.setdefault(subject, []):
                terms_seen[subject].append(term)
    if not totals:
        return []
    top = max(totals.values())
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], VENUE_KEYWORDS.index(kv[0])))
    return [SubjectMatch(subject, score, (score / top) ** 2, tuple(terms_seen[subject])) for subject, score in ranked if score >= _MIN_SUBJECT_SCORE and score >= _MIN_SUBJECT_SHARE * top][:max_subjects]


def subjects_named(keywords: Iterable[str]) -> list[str]:
    """The venue subjects written out in keywords ("immunology", "machine learning for genomics")."""
    text = " | ".join(" ".join(k.lower().split()) for k in keywords)
    return [kw for kw in VENUE_KEYWORDS if kw != "general" and re.search(rf"\b{re.escape(kw)}\b", text)]


def portal_of(slug: str) -> str:
    """The submission-portal name ``slug`` submits through, or ``""``.

    Resolves through :func:`submission_base` so a venue that shares a family
    portal (e.g. every AAAS journal signs in as ``science``) reports the portal
    its login actually runs on. Reads the module registry only -- it never
    imports a venue's Playwright code -- so it stays cheap and
    dependency-free.
    """
    module = SLUG_TO_MODULE.get(submission_base(slug.lower()))
    return "" if module is None else module.split(".", 1)[0]


def _score_name(venue: Venue, name: str) -> tuple[int, list[str]]:
    """Score a venue's slug, name, and full name against a venue-name search.

    The whole search matching a name exactly scores highest ("nature methods"),
    then a name it begins ("nature neuro"). Each word then adds through its
    strongest match: the slug itself, a slug prefix, the name itself, a name
    prefix, a full-name prefix, or the start of any word in the name or full
    name ("neuroscience" in "Nature Neuroscience"). Function words are ignored.
    """
    phrase = " ".join(name.lower().split())
    if not phrase:
        return 0, []
    score = 0
    reasons: list[str] = []
    slug = venue.slug.lower()
    short = venue.name.lower()
    full = venue.full_name.lower()
    names = [short, full, slug.replace("_", " ")]
    if phrase in names:
        score += 100
        reasons.append("full-name")
    elif any(n.startswith(phrase) for n in names if n):
        score += 50
        reasons.append("full-name-prefix")
    words = f"{short} {full} {slug.replace('_', ' ')}"
    for token in (t for t in phrase.split() if t not in _STOPWORDS):
        if token == slug:
            score, reason = score + 100, "slug"
        elif slug.startswith(token):
            score, reason = score + 60, "slug-prefix"
        elif token == short:
            score, reason = score + 80, "name"
        elif short.startswith(token):
            score, reason = score + 40, "name-prefix"
        elif full and full.startswith(token):
            score, reason = score + 35, "full-name-prefix"
        elif re.search(rf"\b{re.escape(token)}", words):
            score, reason = score + 20, "name-word"
        else:
            continue
        reasons.append(reason)
    return score, reasons


def _subject_score(venue: Venue, weights: Mapping[str, float]) -> tuple[int, list[str]]:
    """Score a venue's ``keywords`` against subject weights; also return the subjects it covers.

    Up to 60 points for coverage (the weighted share of the manuscript's subjects
    the venue carries; a ``general`` venue covers each missing subject at half
    weight) and up to 30 for focus (the share of the venue's own subjects that
    are relevant), so a specialist venue outranks a broad one that merely
    includes the subject. A venue covering none of the subjects scores 0.
    """
    keywords = set(venue.keywords)
    total = sum(weights.values())
    if not total or not keywords:
        return 0, []
    matched = [s for s in weights if s in keywords]
    covered = sum(weights[s] for s in matched)
    if "general" in keywords:
        covered += 0.5 * sum(w for s, w in weights.items() if s not in keywords)
    if covered <= 0:
        return 0, []
    specific = keywords - {"general"}
    focus = len(specific & set(weights)) / len(specific) if specific else 0.0
    return round(60 * covered / total + 30 * focus), matched or ["general"]


def _as_weights(subjects: Mapping[str, float] | Iterable[str] | Iterable[SubjectMatch]) -> dict[str, float]:
    if isinstance(subjects, Mapping):
        return {str(s): float(w) for s, w in subjects.items() if w > 0}
    weights: dict[str, float] = {}
    for s in subjects:
        if isinstance(s, SubjectMatch):
            weights[s.subject] = max(weights.get(s.subject, 0.0), s.weight)
        else:
            weights[str(s)] = 1.0
    return weights


def pick_venues(
    name: str = "",
    *,
    subjects: Mapping[str, float] | Iterable[str] | Iterable[SubjectMatch] = (),
    venue_types: tuple[str, ...] = (),
    portals: tuple[str, ...] = (),
    venues: list[Venue] | None = None,
) -> list[VenueHit]:
    """Rank the non-deprecated venues against a venue-name search and subjects.

    ``name`` is scored against each venue's slug, name, and full name
    (:func:`_score_name`). ``subjects`` are ``VENUE_KEYWORDS`` entries -- a list
    (equal weight), a ``{subject: weight}`` mapping, or :class:`SubjectMatch`
    objects from :func:`infer_subjects` -- and score each venue's ``keywords``
    (:func:`_subject_score`). The two add up.

    Only venues with a positive score are returned (with neither a name nor
    subjects, every candidate is kept, so filters alone can drive the
    shortlist). ``venue_types`` and ``portals`` narrow the field first: a venue
    is a candidate only if it passes both filters when either is given. Results
    are ordered by descending score and, on a tie, by slug.

    ``venues`` is the candidate list; it defaults to :func:`list_venues`. Unit
    tests may pass a fixed list instead, so this stays deterministic.
    """
    candidates = list_venues() if venues is None else list(venues)
    weights = _as_weights(subjects)
    hits: list[VenueHit] = []
    for venue in candidates:
        portal = portal_of(venue.slug)
        if venue_types and venue.venue_type not in venue_types:
            continue
        if portals and portal not in portals:
            continue
        score, reasons = _score_name(venue, name)
        if weights:
            subject_score, matched = _subject_score(venue, weights)
            if subject_score:
                score += subject_score
                reasons.append("keywords: " + ", ".join(matched))
        if (name.strip() or weights) and score <= 0:
            continue
        hits.append(VenueHit(venue, score, portal, reasons))

    return sorted(hits, key=lambda hit: (-hit.score, hit.venue.slug))


def hit_dict(hit: VenueHit) -> dict:
    """A hit as plain data (``--json`` and the MCP tool)."""
    return {"slug": hit.venue.slug, "name": hit.venue.name, "venue_type": hit.venue.venue_type, "portal": hit.portal, "keywords": list(hit.venue.keywords), "score": hit.score, "reasons": list(hit.reasons)}


@dataclass(frozen=True)
class Selection:
    """The result of :func:`select`: the ranked hits and what they were ranked on."""

    hits: list[VenueHit]
    name: str = ""
    manuscript: ManuscriptQuery | None = None
    inferred: list[SubjectMatch] = field(default_factory=list)
    subjects: dict[str, float] = field(default_factory=dict)

    def to_dict(self, limit: int | None = None) -> dict:
        hits = self.hits if limit is None else self.hits[: max(limit, 0)]
        return {
            "venue_name": self.name,
            "manuscript": self.manuscript.to_dict() if self.manuscript else None,
            "inferred_subjects": [s.to_dict() for s in self.inferred],
            "subjects": {s: round(w, 3) for s, w in self.subjects.items()},
            "matches": [hit_dict(h) for h in hits],
            "total_matches": len(self.hits),
        }


def _split_keywords(values: Iterable[str]) -> list[str]:
    """Keywords from ``-k`` values: each may itself be comma- or semicolon-separated."""
    out: list[str] = []
    for value in values:
        for k in re.split(r"[,;]", value):
            k = _clean(k)
            if k and k.lower() not in (o.lower() for o in out):
                out.append(k)
    return out


def select(
    *,
    keywords: Iterable[str] = (),
    title: str = "",
    abstract: str = "",
    manuscript: str | Path | None = None,
    name: str = "",
    venue_types: tuple[str, ...] = (),
    portals: tuple[str, ...] = (),
    venues: list[Venue] | None = None,
) -> Selection:
    """Rank venues for a manuscript's keywords, title, and abstract, and/or a venue name.

    ``manuscript`` is read with :func:`extract_manuscript`; ``title`` and
    ``abstract`` replace what it found and ``keywords`` (each may be
    comma-separated) are added to its keywords. Each part is weighted by
    section when mapped to subjects (:func:`infer_subjects`: title and keywords
    count more than abstract), and a keyword that is itself a venue subject
    ("immunology") also matches venue keywords directly. ``name`` searches
    venue names (:func:`_score_name`). Raises :class:`ValueError` for an
    unreadable manuscript.
    """
    keywords = _split_keywords(keywords)
    base = extract_manuscript(Path(manuscript)) if manuscript else ManuscriptQuery()
    mq: ManuscriptQuery | None = None
    if manuscript or title.strip() or abstract.strip() or keywords:
        merged_keywords = list(base.keywords) + [k for k in keywords if k.lower() not in (b.lower() for b in base.keywords)]
        mq = ManuscriptQuery(
            title=_clean(title) or base.title,
            abstract=_clean(abstract) or base.abstract,
            keywords=tuple(merged_keywords),
            source=base.source or "options",
            body="" if abstract.strip() else base.body,
        )

    inferred = infer_subjects(mq) if mq else []
    weights = _as_weights(inferred)
    for subject in subjects_named(keywords):
        weights[subject] = 1.0
    if mq is not None and not weights and not name.strip():
        # A manuscript that names no subject matches nothing; without this,
        # filters alone would keep every venue, as if nothing had been given.
        return Selection([], name=name, manuscript=mq, inferred=inferred, subjects=weights)
    hits = pick_venues(name, subjects=weights, venue_types=venue_types, portals=portals, venues=venues)
    return Selection(hits, name=name, manuscript=mq, inferred=inferred, subjects=weights)


def format_hits(hits: list[VenueHit], *, show_keywords: bool = False, reasons: Mapping[str, str] | None = None) -> list[str]:
    """Render hits as display lines, one per venue.

    Each line is ``slug  Name (type) -- description`` with the slug column
    padded to the widest slug in the list and the description truncated, so the
    output lines up whether there are two hits or twenty. ``show_keywords``
    puts the venue's subject keywords in place of the description; ``reasons``
    (slug -> text, e.g. an LLM's) replaces it with that text instead.
    """
    width = max((len(hit.venue.slug) for hit in hits), default=0)
    lines: list[str] = []
    for hit in hits:
        head = f"{hit.venue.slug:<{width}}  {hit.venue.name} ({hit.venue.venue_type})"
        if reasons is not None and reasons.get(hit.venue.slug):
            head += f" -- {reasons[hit.venue.slug]}"
        elif show_keywords and hit.venue.keywords:
            head += f" [{', '.join(hit.venue.keywords)}]"
        else:
            description = " ".join(hit.venue.description.split())
            if description:
                head += f" -- {description[:72]}" + ("…" if len(description) > 72 else "")
        lines.append(head)
    return lines


# ---------------------------------------------------------------------------
# LLM ranking (``pick-venue --ai``)
# ---------------------------------------------------------------------------

FITS = ("strong", "good", "possible")

_SYSTEM = (
    "You help researchers choose where to submit a manuscript. You are given what the manuscript "
    "says it is about (title, keywords, abstract) and a list of candidate venues, each with its "
    "type, subject keywords, and description. Choose the manuscript's subjects from the fixed "
    "vocabulary given, then rank the candidate venues that are a good thematic fit, best first. "
    "Judge thematic fit and scope only -- not prestige or the chance of acceptance. Prefer venues "
    "whose scope is centred on the manuscript's topic over broad venues that merely include it, "
    "but keep strong broad venues when the work would interest a wide readership. Only use slugs "
    "from the list. Give each venue a one-sentence reason grounded in the manuscript."
)


def _ai_schema(slugs: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "subjects": {"type": "array", "items": {"type": "string", "enum": [k for k in VENUE_KEYWORDS if k != "general"]}},
            "venues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"slug": {"type": "string", "enum": slugs}, "fit": {"type": "string", "enum": list(FITS)}, "reason": {"type": "string"}},
                    "required": ["slug", "fit", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["subjects", "venues"],
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class AiPick:
    slug: str
    fit: str
    reason: str


@dataclass(frozen=True)
class AiRanking:
    """An LLM's subjects for the manuscript and its ranked venues (best first)."""

    subjects: list[str]
    picks: list[AiPick]
    model: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"model": self.model, "subjects": list(self.subjects), "venues": [{"slug": p.slug, "fit": p.fit, "reason": p.reason} for p in self.picks]}


def rank_via_api(
    query: ManuscriptQuery,
    candidates: list[Venue],
    *,
    limit: int = 10,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    max_tokens: int = 4000,
) -> AiRanking:
    """Ask an LLM to choose ``query``'s subjects and rank ``candidates`` for it.

    Uses the provider and model handling of ``paperpush autofill --engine api``
    (:func:`paperpush.autofill.resolve_provider`); raises
    :class:`paperpush.autofill.AutofillApiError` when no API key is set, the SDK
    is missing, or the call fails.
    """
    import importlib

    # The package exports an ``autofill`` function that shadows the module name.
    autofill = importlib.import_module("paperpush.autofill")

    if not candidates:
        return AiRanking([], [])
    provider = autofill.resolve_provider(provider, alternative="run pick-venue without --ai", api_key=api_key)
    model = model or autofill.DEFAULT_MODELS[provider]
    listing = "\n".join(f"- {v.slug}: {v.full_name or v.name} ({v.venue_type}); subjects: {', '.join(v.keywords) or 'none listed'}; {_clean(v.description)}" for v in candidates)
    vocabulary = ", ".join(k for k in VENUE_KEYWORDS if k != "general")
    instructions = f"Manuscript:\n\n{query.prompt_text()}\n\nSubject vocabulary: {vocabulary}\n\nCandidate venues:\n{listing}\n\nReturn the manuscript's subjects (most central first, at most four) and up to {limit} venues, best fit first."
    logger.info("pick-venue ai: calling %s %s (%d candidate venue(s))", provider, model, len(candidates))
    text = autofill._CALLERS[provider](model, [], instructions, _ai_schema([v.slug for v in candidates]), max_tokens, system=_SYSTEM, **autofill._client_kwargs(api_key))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise autofill.AutofillApiError(f"the model returned output that was not valid JSON: {exc}") from exc
    known = {v.slug for v in candidates}
    picks: list[AiPick] = []
    for item in data.get("venues", []):
        slug = str(item.get("slug", ""))
        if slug in known and slug not in {p.slug for p in picks}:
            picks.append(AiPick(slug, str(item.get("fit", "")), _clean(str(item.get("reason", "")))))
    subjects = [s for s in data.get("subjects", []) if s in VENUE_KEYWORDS]
    return AiRanking(subjects, picks[:limit], model=f"{provider} {model}")


# ---------------------------------------------------------------------------
# Any journal, from OpenAlex (the ``pick-venue`` default for a manuscript)
# ---------------------------------------------------------------------------

# How far back similar works count, and how much older ones count.
JOURNAL_YEARS = 8
# Source types kept: journals and conference proceedings; a repository only
# when it is a preprint server (bioRxiv, arXiv, ...), not an aggregator such as
# PubMed Central or a review site.
_KEPT_SOURCE_TYPES = {"journal", "conference"}
_PREPRINT_SERVER = re.compile(r"rxiv\b|\barxiv", re.IGNORECASE)
# ``--type`` values and the OpenAlex source types they keep.
_TYPE_TO_SOURCE = {"journal": {"journal"}, "conference": {"conference"}, "preprint": {"repository"}}


def _recency(year: int | None, this_year: int) -> float:
    if not year:
        return 0.5
    age = this_year - int(year)
    return 1.0 if age <= 3 else 0.75 if age <= 6 else 0.5


def _name_key(name: str) -> str:
    """A journal name for matching across sources: no parenthetical, "the", or punctuation."""
    from .openalex import fold

    name = re.sub(r"\([^)]*\)", " ", name or "").replace("&", " and ")
    words = fold(name).split()
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def _supported_by_name(venues: list[Venue] | None = None) -> dict[str, Venue]:
    """The venues paperpush supports, keyed by every name they go by (see :func:`_name_key`)."""
    out: dict[str, Venue] = {}
    for venue in list_venues() if venues is None else venues:
        for name in (venue.name, venue.full_name):
            key = _name_key(name)
            if key and key not in out:
                out[key] = venue
    return out


def support_level(venue: Venue) -> str:
    """``"validate + submit"``, ``"validate"``, ``"submit"``, or ``"listed"`` for a supported venue."""
    parts = [p for p, ok in (("validate", venue.supports_validate), ("submit", venue.supports_submit)) if ok]
    return " + ".join(parts) or "listed"


@dataclass(frozen=True)
class JournalHit:
    """A journal (or other source) that published works similar to the manuscript."""

    source_id: str
    name: str
    source_type: str
    score: int  # 0-100, the best match is 100
    works: int  # similar works it published
    examples: tuple[tuple[str, int | None], ...] = ()  # (title, year) of its closest matches
    publisher: str = ""
    issn_l: str = ""
    homepage: str = ""
    is_oa: bool | None = None
    is_in_doaj: bool | None = None
    apc_usd: int | None = None
    h_index: int | None = None
    slug: str = ""  # the paperpush venue, when supported
    support: str = ""  # what paperpush supports for it (see support_level)


def journal_dict(hit: JournalHit) -> dict:
    """A journal hit as plain data (``--json`` and the MCP tool)."""
    return {
        "name": hit.name,
        "openalex_id": hit.source_id,
        "source_type": hit.source_type,
        "score": hit.score,
        "similar_works": hit.works,
        "examples": [{"title": t, "year": y} for t, y in hit.examples],
        "publisher": hit.publisher,
        "issn_l": hit.issn_l,
        "homepage": hit.homepage,
        "is_oa": hit.is_oa,
        "is_in_doaj": hit.is_in_doaj,
        "apc_usd": hit.apc_usd,
        "h_index": hit.h_index,
        "paperpush_slug": hit.slug or None,
        "paperpush_support": hit.support or None,
    }


@dataclass(frozen=True)
class JournalSearch:
    """The result of :func:`find_journals`."""

    hits: list[JournalHit]
    query: str
    works: int
    since_year: int

    def to_dict(self, limit: int | None = None) -> dict:
        hits = self.hits if limit is None else self.hits[: max(limit, 0)]
        return {"source": "OpenAlex", "search": self.query, "similar_works": self.works, "since_year": self.since_year, "journals": [journal_dict(h) for h in hits], "total_journals": len(self.hits)}


def find_journals(
    query: ManuscriptQuery,
    *,
    venue_types: tuple[str, ...] = (),
    years: int = JOURNAL_YEARS,
    venues: list[Venue] | None = None,
    this_year: int | None = None,
) -> JournalSearch:
    """Journals that publish work like ``query``'s, from OpenAlex, best first.

    The works most similar to the manuscript (:func:`paperpush.openalex.similar_works`)
    are grouped by the source they appeared in; each source scores the sum,
    over its works, of the work's similarity to the manuscript (relative to the
    best match) times a recency factor, scaled so the top source is 100. The manuscript itself,
    when OpenAlex already has it, is left out. ``venue_types`` (``journal``,
    ``conference``, ``preprint``) narrows the source types kept.

    Raises :class:`paperpush.openalex.OpenAlexError` when OpenAlex does not
    answer and :class:`ValueError` when ``query`` has nothing to search on.
    """
    from . import openalex

    this_year = this_year or date.today().year
    since = this_year - years
    abstract = query.abstract or query.body
    search, works = openalex.similar_works(query.title, abstract, query.keywords, since_year=since)
    kept_types = set().union(*(_TYPE_TO_SOURCE[t] for t in venue_types if t in _TYPE_TO_SOURCE)) if venue_types else None
    own_title = openalex.fold(query.title)
    scores: dict[str, float] = {}
    counts: dict[str, int] = {}
    examples: dict[str, list[tuple[str, int | None]]] = {}
    names: dict[str, tuple[str, str]] = {}
    for work in works:
        title = work.get("display_name") or ""
        if own_title and openalex.fold(title) == own_title:
            continue
        src = ((work.get("primary_location") or {}).get("source")) or {}
        sid = openalex.short_id(src.get("id") or "")
        stype = src.get("type") or ""
        if not sid:
            continue
        if kept_types is not None:
            if stype not in kept_types or (stype == "repository" and not _PREPRINT_SERVER.search(src.get("display_name") or "")):
                continue
        elif stype not in _KEPT_SOURCE_TYPES and not (stype == "repository" and _PREPRINT_SERVER.search(src.get("display_name") or "")):
            continue
        year = work.get("publication_year")
        scores[sid] = scores.get(sid, 0.0) + float(work.get("similarity") or 0) * _recency(year, this_year)
        counts[sid] = counts.get(sid, 0) + 1
        examples.setdefault(sid, []).append((title, year))
        names[sid] = (src.get("display_name") or sid, stype)
    if not scores:
        return JournalSearch([], search, len(works), since)
    ranked = sorted(scores, key=lambda s: (-scores[s], names[s][0].lower()))
    details = openalex.sources(ranked[: openalex.BATCH])
    supported = _supported_by_name(venues)
    best = scores[ranked[0]]
    hits: list[JournalHit] = []
    for sid in ranked:
        info = details.get(sid, {})
        name = info.get("display_name") or names[sid][0]
        venue = supported.get(_name_key(name)) or supported.get(_name_key(names[sid][0]))
        stats = info.get("summary_stats") or {}
        hits.append(
            JournalHit(
                source_id=sid,
                name=name,
                source_type=info.get("type") or names[sid][1],
                score=max(1, round(100 * scores[sid] / best)),
                works=counts[sid],
                examples=tuple(examples[sid][:3]),
                publisher=info.get("host_organization_name") or "",
                issn_l=info.get("issn_l") or "",
                homepage=info.get("homepage_url") or "",
                is_oa=info.get("is_oa"),
                is_in_doaj=info.get("is_in_doaj"),
                apc_usd=info.get("apc_usd"),
                h_index=stats.get("h_index"),
                slug=venue.slug if venue else "",
                support=support_level(venue) if venue else "",
            )
        )
    return JournalSearch(hits, search, len(works), since)


def format_journals(hits: list[JournalHit]) -> list[str]:
    """Render journal hits as numbered display lines, two per journal."""
    lines: list[str] = []
    width = len(str(len(hits)))
    for i, hit in enumerate(hits, 1):
        facts = [f"{hit.works} similar work{'s' if hit.works != 1 else ''}"]
        if hit.publisher:
            facts.insert(0, hit.publisher)
        if hit.h_index is not None:
            facts.append(f"h-index {hit.h_index}")
        if hit.is_oa:
            facts.append("open access" + (" (DOAJ)" if hit.is_in_doaj else ""))
        if hit.apc_usd:
            facts.append(f"APC ${hit.apc_usd:,}")
        if hit.source_type and hit.source_type != "journal":
            facts.append(hit.source_type)
        support = f"  [paperpush: {hit.slug}, {hit.support}]" if hit.slug else ""
        lines.append(f"{i:>{width}}. {hit.name} -- {'; '.join(facts)}{support}")
        if hit.examples:
            title, year = hit.examples[0]
            title = " ".join(title.split())
            lines.append(f"{'':>{width}}  e.g. {title[:90]}{'…' if len(title) > 90 else ''}" + (f" ({year})" if year else ""))
    return lines
