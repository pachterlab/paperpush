"""Code availability and figure reproducibility, in three tiers.

* ``rigor.code_availability`` (deterministic, on by default) -- a manuscript
  that reports computational work links the code behind it, does not offer it
  only "upon request", and archives it (a Zenodo, figshare, Software Heritage,
  Code Ocean, or OSF DOI, or at least a pinned release or commit) rather than
  pointing at a moving branch. With network access (switched off with the
  link check, ``--disable-integrity.links``) each GitHub repository given
  as the paper's code is opened through the GitHub API: it should carry a
  license, a README, an environment specification (``requirements.txt``,
  ``environment.yml``, ``renv.lock``, a Dockerfile, ...), and code.
* ``rigor.data_accessions`` (deterministic, network) -- the GEO series and
  BioProject accessions the manuscript gives are public. One still under
  embargo is reported so the author can give reviewers its access token.
* ``ai.rigor.figure_provenance`` (AI, on by default when code is linked or
  attached) -- a reader maps every main figure and panel to the script or
  notebook that draws it and the inputs it reads, and reports the panels no
  code produces and the inputs a reader cannot get. Read only: nothing is run.
* ``ai.figure_reproduction.<n>`` (opt-in, ``--reproduce-figures``) -- an agent
  builds a fresh environment from the repository's own dependency files, runs
  the code for figure ``n`` within a time and download budget, and compares
  what it draws with the submitted figure. These tasks need an agent that can
  run code, so they are refused with ``--ai`` (an API model cannot run them)
  and with ``--web``.

All findings are warnings: venues rarely require any of this, and the
deterministic checks read free text.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .ai_validate import AiTask
from .database import Venue
from .validate import WARNING, Checks, Issue, NotApplicable, Outcome, Unchecked

logger = logging.getLogger(__name__)

CATEGORY = "Rigor and reproducibility"
_USER_AGENT = "paperpush-validate (+https://github.com/pachterlab/paperpush)"
_TIMEOUT = 15
# Repositories opened through the GitHub API (two requests each; the anonymous limit is 60 an hour).
MAX_REPOS = 3
MAX_ACCESSIONS = 10
DEFAULT_REPRODUCE_MINUTES = 30
DEFAULT_REPRODUCE_DOWNLOAD_GB = 5.0

# ---------------------------------------------------------------------------
# what the manuscript says about its code and data

_STATEMENT_HEADING = re.compile(r"(?im)^[ \t]*(?:\d+(?:\.\d+)*\.?[ \t]+)?(?:#+[ \t]*)?" r"((?:data(?:,)?[ \t]+(?:and[ \t]+)?|materials(?:,)?[ \t]+(?:data[ \t]+)?and[ \t]+)?(?:code|software)(?:[ \t]+and[ \t]+(?:data|materials))?[ \t]+availability" r"|data[ \t]+(?:and[ \t]+materials[ \t]+)?availability" r"|availability[ \t]+of[ \t]+(?:data|code|software|materials)[^\n:]{0,40}" r"|(?:data|code|software)[ \t]+(?:sharing|deposition)" r")(?:[ \t]+statements?)?[ \t]*[:.\u2014-]?[ \t]*")
# Where a statement section ends: the next statement or back-matter heading.
_NEXT_HEADING = re.compile(r"(?im)^[ \t]*(?:\d+(?:\.\d+)*\.?[ \t]+)?(?:#+[ \t]*)?(?:acknowledge?ments?|author[ \t]+contributions?|competing[ \t]+interests?|conflicts?[ \t]+of[ \t]+interest|declarations?|funding|references|bibliography|ethics|supplementary[ \t]+information|additional[ \t]+information|correspondence|materials[ \t]+availability|lead[ \t]+contact)\b")
_STATEMENT_FIELDS = re.compile(r"(?i)^(?:code_availability|data_availability|data_availability_links|data_statement|data_repository_url|code_url|software_availability)$")
_CODE_WORD = re.compile(r"(?i)\b(?:code|software|scripts?|notebooks?|source)\b")

_UPON_REQUEST = re.compile(r"(?i)\b(?:(?:up)?on[ \t]+(?:reasonable[ \t]+|justified[ \t]+|written[ \t]+)?request|by[ \t]+request|(?:at[ \t]+the[ \t]+)?request[ \t]+(?:to|from|of)[ \t]+the[ \t]+(?:corresponding[ \t]+|lead[ \t]+)?authors?|contact(?:ing)?[ \t]+the[ \t]+(?:corresponding[ \t]+|lead[ \t]+)?authors?)\b")
# A reason a dataset legitimately cannot be posted publicly.
_RESTRICTED_REASON = re.compile(r"(?i)\b(?:privacy|confidential\w*|consent|GDPR|HIPAA|controlled[ \t-]+access|dbGaP|EGA\b|European[ \t]+Genome|proprietary|licen[cs]\w*|third[ \t-]+part\w+|data[ \t]+use[ \t]+agreement|identif\w+|sensitive|commercial)\b")

_COMPUTATIONAL = re.compile(r"(?i)\b(?:python|matplotlib|ggplot2?|seaborn|plotly|jupyter|notebooks?|bioconductor|scikit-learn|sklearn|pytorch|tensorflow|jax|matlab|julia|snakemake|nextflow|source[ \t]+code|custom[ \t]+(?:code|scripts?)|scripts?|simulations?|simulated|in[ \t]+silico|computational(?:ly)?|github|R[ \t]+package|numpy|scipy|pandas)\b")
# Mentions of computational work needed before a missing code link is reported.
_COMPUTATIONAL_MIN = 3

# Archives that give a deposited, citable snapshot of code.
_ARCHIVE = re.compile(r"(?i)(?:\b10\.5281/zenodo\.\d+|zenodo\.org/(?:records?|doi|badge)/\S+|\b10\.6084/m9\.figshare\.\S+|figshare\.com/\S+|\bswh:1:(?:dir|rev|rel|snp|cnt):[0-9a-f]{40}|softwareheritage\.org/\S+|\b10\.24433/CO\.\S+|codeocean\.com/capsule/\S+|\b10\.17605/OSF\.IO/\S+|osf\.io/[a-z0-9]{5}\b|\b10\.5061/dryad\.\S+|datadryad\.org/\S+)")
_PINNED_URL = re.compile(r"(?i)github\.com/[^/\s]+/[^/\s]+/(?:tree|blob|commit|releases/tag|archive)/[^\s)]+")
_PINNED_TEXT = re.compile(r"(?i)\b(?:commit|tag|release|version|v)[ \t:]*(?:[0-9a-f]{7,40}\b|v?\d+\.\d+(?:\.\d+)?\b)")

_CODE_SUFFIXES = {".py", ".r", ".rmd", ".qmd", ".ipynb", ".m", ".jl", ".sh", ".smk", ".nf", ".c", ".cpp", ".rs", ".go", ".java", ".js", ".ts", ".do", ".sas", ".pl"}
_ENV_FILES = {
    "requirements.txt",
    "environment.yml",
    "environment.yaml",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "pipfile",
    "pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "pixi.toml",
    "pixi.lock",
    "conda-lock.yml",
    "renv.lock",
    "description",
    "install.r",
    "packrat.lock",
    "dockerfile",
    "containerfile",
    "singularity",
    "apptainer.def",
    "project.toml",
    "manifest.toml",
    "cargo.toml",
    "go.mod",
    "package.json",
    "makefile",
    "cmakelists.txt",
    "configure.ac",
    "meson.build",
    "snakefile",
    "nextflow.config",
    "flake.nix",
    "default.nix",
    "shell.nix",
    "binder",
    "runtime.txt",
    "apt.txt",
    "install.sh",
}
_ENV_SUFFIXES = (".def", ".sif", ".lock")

_GEO = re.compile(r"\bGSE\d{3,8}\b")
_BIOPROJECT = re.compile(r"\bPRJ(?:NA|EB|DB)\d{3,9}\b")


@dataclass
class CodeSources:
    """Where the manuscript's own code is: repositories, archives, and attached code."""

    repos: list[str] = field(default_factory=list)  # https://github.com/owner/repo and other hosts
    github: list[tuple[str, str]] = field(default_factory=list)
    archives: list[str] = field(default_factory=list)
    attached: list[Path] = field(default_factory=list)  # uploads that carry code
    pinned: bool = False
    statements: list[tuple[str, str]] = field(default_factory=list)  # (field id, text)
    computational: int = 0
    field_id: str = ""

    def any(self) -> bool:
        return bool(self.repos or self.archives or self.attached)

    def describe(self) -> str:
        parts = []
        if self.repos:
            parts.append("repository " + ", ".join(self.repos))
        if self.archives:
            parts.append("archive " + ", ".join(self.archives))
        if self.attached:
            parts.append("attached " + ", ".join(p.name for p in self.attached))
        return "; ".join(parts)


def _role_paths(venue: Venue, values: dict[str, str], *roles: str) -> list[Path]:
    from .requirements_check import _existing, _fields_for, _paths

    return list(dict.fromkeys(p for role in roles for f in _fields_for(venue, role) for p in _existing(_paths(f, values))))


def statements(text: str) -> list[str]:
    """The code and data availability statements in ``text``."""
    out = []
    for m in _STATEMENT_HEADING.finditer(text):
        rest = text[m.end() : m.end() + 2500]
        # The section ends at the next back-matter heading or the next availability statement.
        ends = [e.start() for e in (_NEXT_HEADING.search(rest, 1), _STATEMENT_HEADING.search(rest, 1)) if e]
        body = rest[: min(ends)] if ends else rest
        # A statement is a paragraph or two; past that the section has ended.
        paragraphs = [p for p in re.split(r"\n[ \t]*\n", body) if p.strip()][:3]
        body = "\n\n".join(paragraphs).strip()
        if body:
            out.append(f"{m.group(1).strip()}: {body}")
    return out


def _clean_repo(url: str) -> str:
    return url.rstrip(".,;:)]}'\"")


def _code_in_archive(path: Path) -> bool:
    """Whether an uploaded archive holds source code."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()[:5000]
        elif tarfile.is_tarfile(path):
            with tarfile.open(path) as tf:
                names = [m.name for i, m in zip(range(5000), tf)]
        else:
            return False
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        return False
    # A LaTeX source archive is not the paper's code.
    return any(Path(n).suffix.lower() in _CODE_SUFFIXES for n in names)


def code_sources(venue: Venue, values: dict[str, str]) -> CodeSources:
    """The code this manuscript links or attaches, read from its main text (not its reference list)."""
    from . import sensitive
    from .manuscript import truncate_at_references
    from .rigor_check import _texts

    found = CodeSources()
    body_parts = []
    for field_id, _, text in _texts(venue, values, roles=("manuscript", "supplementary")):
        found.field_id = found.field_id or field_id
        body = truncate_at_references(text)
        body_parts.append(body)
        found.statements.extend((field_id, s) for s in statements(body))
    for key, value in values.items():
        if _STATEMENT_FIELDS.match(key) and value.strip():
            found.statements.append((key, value.strip()))
    body = "\n".join(body_parts)
    found.computational = len(_COMPUTATIONAL.findall(body))
    statement_text = "\n".join(s for _, s in found.statements)
    # The repositories a code statement names are the paper's; otherwise every
    # repository in the main text is a candidate (a cited tool's may be among them).
    for source in (statement_text, body) if statement_text else (body,):
        for owner, repo in sensitive._find_github_repos(source):
            if (owner, repo) not in found.github:
                found.github.append((owner, repo))
                found.repos.append(f"https://github.com/{owner}/{repo}")
        for m in sensitive._CODE_HOST_RE.finditer(source):
            url = "https://" + _clean_repo(m.group(0))
            if url not in found.repos:
                found.repos.append(url)
        for m in _ARCHIVE.finditer(source):
            archive = _clean_repo(m.group(0))
            if archive not in found.archives:
                found.archives.append(archive)
        if found.repos or found.archives:
            break
    found.pinned = bool(found.archives or _PINNED_URL.search(statement_text or body) or (statement_text and _PINNED_TEXT.search(statement_text)))
    found.attached = [p for p in _role_paths(venue, values, "supplementary") if p.suffix.lower() in _CODE_SUFFIXES or _code_in_archive(p)]
    return found


# ---------------------------------------------------------------------------
# network


def _get_json(url: str, headers: dict[str, str] | None = None) -> tuple[int, object | None]:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # https literals below  # nosec B310
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, None


def _github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


@dataclass
class RepoInfo:
    status: str  # "ok", "missing", "unknown"
    license: str | None = None
    paths: list[str] = field(default_factory=list)
    truncated: bool = False
    archived: bool = False


def github_repo(owner: str, repo: str) -> RepoInfo:
    """What the GitHub API says about ``owner/repo``: its license and file tree."""
    base = f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"
    try:
        status, data = _get_json(base, _github_headers())
        if status == 404:
            return RepoInfo("missing")
        if status != 200 or not isinstance(data, dict):
            return RepoInfo("unknown")
        lic = (data.get("license") or {}).get("spdx_id") if isinstance(data.get("license"), dict) else None
        info = RepoInfo("ok", license=lic if lic and lic != "NOASSERTION" else (lic and "unrecognized"), archived=bool(data.get("archived")))
        branch = urllib.parse.quote(str(data.get("default_branch") or "HEAD"), safe="")
        status, tree = _get_json(f"{base}/git/trees/{branch}?recursive=1", _github_headers())
        if status == 409:  # an empty repository
            return info
        if status != 200 or not isinstance(tree, dict):
            return RepoInfo("unknown", license=info.license)
        info.paths = [str(t.get("path", "")) for t in tree.get("tree", []) if t.get("type") == "blob"]
        info.truncated = bool(tree.get("truncated"))
        return info
    except (OSError, ValueError) as exc:
        logger.debug("GitHub lookup failed for %s/%s: %s", owner, repo, exc)
        return RepoInfo("unknown")


def _repo_problems(name: str, info: RepoInfo) -> list[str]:
    problems = []
    names = {Path(p).name.lower(): p for p in info.paths}
    if not info.license:
        problems.append(f"{name} has no license file, so others may not legally reuse or rerun the code (add a LICENSE, e.g. MIT, BSD-3-Clause, or GPL-3.0)")
    if not info.paths and not info.truncated:
        problems.append(f"{name} is empty")
        return problems
    if not any(n.startswith("readme") for n in names):
        problems.append(f"{name} has no README saying how to run the code and which script makes which figure")
    if not any(n in _ENV_FILES or n.endswith(_ENV_SUFFIXES) for n in names) and not any(p.lower().startswith(("binder/", ".devcontainer/")) for p in info.paths):
        problems.append(f"{name} declares no software environment (requirements.txt, environment.yml, renv.lock, a Dockerfile, ...), so its dependencies and their versions cannot be rebuilt")
    if not info.truncated and not any(Path(p).suffix.lower() in _CODE_SUFFIXES for p in info.paths):
        problems.append(f"{name} holds no source code or notebooks")
    return problems


def accession_public(accession: str) -> bool | None:
    """Whether NCBI lists ``accession`` publicly (GEO series or BioProject); None when it could not be asked."""
    db, term = ("gds", f"{accession}[ACCN]") if accession.startswith("GSE") else ("bioproject", f"{accession}[PRJA]")
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode({"db": db, "term": term, "retmode": "json"})
    try:
        status, data = _get_json(url)
    except (OSError, ValueError) as exc:
        logger.debug("accession lookup failed for %s: %s", accession, exc)
        return None
    if status != 200 or not isinstance(data, dict):
        return None
    count = (data.get("esearchresult") or {}).get("count")
    return None if count is None else int(count) > 0


# ---------------------------------------------------------------------------
# tier 1: deterministic checks


def _code_availability_check(venue: Venue, values: dict[str, str], online: bool) -> Outcome:
    from .rigor_check import _no_text, _texts

    if not _texts(venue, values, roles=("manuscript", "supplementary")):
        raise _no_text(venue, values, ("manuscript", "supplementary"), "manuscript text")
    src = code_sources(venue, values)
    code_statements = [(f, s) for f, s in src.statements if _CODE_WORD.search(s.split(":", 1)[0]) or _CODE_WORD.search(s)]
    if not src.any() and not code_statements and src.computational < _COMPUTATIONAL_MIN:
        raise NotApplicable("the manuscript names no code and does not describe computational work")
    where = src.field_id
    issues: list[Issue] = []
    for field_id, text in code_statements:
        if _UPON_REQUEST.search(text) and _CODE_WORD.search(text) and not src.any():
            issues.append(Issue(WARNING, field_id, f'the code is offered only on request ("{_quote(text, _UPON_REQUEST)}"); deposit it in a public repository and archive a release (Zenodo, Software Heritage) so readers can rerun the analysis'))
            break
    for field_id, text in src.statements:
        if not re.search(r"(?i)\bdata\b", text.split(":", 1)[0]) or not _UPON_REQUEST.search(text) or _RESTRICTED_REASON.search(text):
            continue
        issues.append(Issue(WARNING, field_id, f'the data are offered only on request, with no reason given ("{_quote(text, _UPON_REQUEST)}"); deposit them in a public repository, or say why they cannot be shared (privacy, consent, licence) and how access is granted'))
        break
    if not src.any():
        if not any("on request" in i.message for i in issues):
            issues.append(Issue(WARNING, where, f"the manuscript describes computational work ({src.computational} mentions of code, scripts, or analysis software) but links no code repository or archive and attaches no code; add a code availability statement with a public repository"))
        return Outcome(issues, "no code repository, archive, or attached code found")
    if src.repos and not src.pinned:
        issues.append(Issue(WARNING, where, f"the code is linked only as a repository ({', '.join(src.repos[:3])}), which can change after publication; archive the release the paper used (GitHub's Zenodo integration gives it a DOI) or cite the exact commit or tag"))
    checked = []
    if online and src.github:
        unknown = []
        for owner, repo in src.github[:MAX_REPOS]:
            name = f"github.com/{owner}/{repo}"
            info = github_repo(owner, repo)
            if info.status == "missing":
                continue  # integrity.links reports a private or removed repository
            if info.status == "unknown":
                unknown.append(name)
                continue
            checked.append(name)
            issues.extend(Issue(WARNING, where, p) for p in _repo_problems(name, info))
        if unknown:
            from .validate import unchecked

            issues.append(unchecked(where, f"GitHub did not answer for {', '.join(unknown)} (rate limit or no network; set GITHUB_TOKEN to raise the limit), so its license, README, and environment files were not checked"))
    detail = f"code: {src.describe()}" + (f"; inspected {', '.join(checked)}" if checked else "")
    return Outcome(issues, detail)


def _quote(text: str, pattern: re.Pattern) -> str:
    m = pattern.search(text)
    if not m:
        return ""
    start = max(text.rfind(".", 0, m.start()) + 1, m.start() - 120)
    end = text.find(".", m.end())
    end = m.end() + 60 if end < 0 else min(end + 1, m.end() + 60)
    return " ".join(text[start:end].split())


def _data_accessions_check(venue: Venue, values: dict[str, str], online: bool) -> Outcome:
    from .manuscript import truncate_at_references
    from .rigor_check import _texts

    texts = _texts(venue, values, roles=("manuscript", "supplementary"))
    body = "\n".join(truncate_at_references(t) for _, _, t in texts) + "\n" + "\n".join(v for k, v in values.items() if _STATEMENT_FIELDS.match(k))
    accessions = list(dict.fromkeys(m.group(0) for pattern in (_GEO, _BIOPROJECT) for m in pattern.finditer(body)))
    if not accessions:
        raise NotApplicable("the manuscript gives no GEO series or BioProject accession")
    if not online:
        raise Unchecked(f"found {len(accessions)} accession(s) ({', '.join(accessions[:5])}) but network checks are off")
    where = texts[0][0] if texts else ""
    issues, unknown = [], []
    for accession in accessions[:MAX_ACCESSIONS]:
        public = accession_public(accession)
        if public is None:
            unknown.append(accession)
        elif not public:
            issues.append(Issue(WARNING, where, f"{accession} is not public in NCBI {'GEO' if accession.startswith('GSE') else 'BioProject'}; if it is under embargo until publication, give reviewers its access token in the data availability statement, and check the accession is right"))
    if unknown and len(unknown) == len(accessions[:MAX_ACCESSIONS]):
        raise Unchecked("NCBI did not answer")
    if unknown:
        from .validate import unchecked

        issues.append(unchecked(where, f"NCBI did not answer for {', '.join(unknown)}"))
    more = f" (the first {MAX_ACCESSIONS} of {len(accessions)})" if len(accessions) > MAX_ACCESSIONS else ""
    return Outcome(issues, f"looked up {', '.join(accessions[:MAX_ACCESSIONS])}{more}")


# ---------------------------------------------------------------------------
# tiers 2 and 3: tasks for a reader and for an agent that runs code


def _where_code_is(src: CodeSources) -> str:
    lines = []
    if src.repos:
        lines.append("Repositories the manuscript gives for its code: " + ", ".join(src.repos) + ".")
    if src.archives:
        lines.append("Archived copies: " + ", ".join(src.archives) + ".")
    if src.attached:
        lines.append("Code attached to the submission: " + ", ".join(str(p) for p in src.attached) + ".")
    if src.statements:
        lines.append("Availability statements: " + " | ".join(" ".join(s.split())[:600] for _, s in src.statements[:4]))
    return "\n".join(lines)


_PROVENANCE_TASK = "Map each main figure, and each of its panels, to the code that draws it. Do not run anything: read the repository (clone it, preferring the archived release or the commit the manuscript cites over the default branch), " "its README, and the code attached to the submission. For every figure and panel name the script or notebook (and the function or cell, where the file draws several) that produces it, and the input files or datasets it reads. " "Check that each input can be had by a reader: a file in the repository, the output of another script in the repository, a public accession or archive DOI the manuscript gives, or a download the code performs. " "Report fail naming each panel with no code that produces it, each input that cannot be had (an absolute local path such as /Users/... or C:\\..., a file that is neither in the repository nor downloaded, data 'available on request'), " "and any figure whose code is hard-coded to a machine. A schematic, photograph, or micrograph is not drawn by code: list it as not computational, and report not_applicable if no figure is. " "Report pass only after you have found code for every computational panel, and put the mapping in details as one line per panel ('Fig. 2b: notebooks/fig2.ipynb, cell 7; reads data/counts.h5ad (Zenodo 10.5281/zenodo.123)'). " "If the repository cannot be opened, report unclear."


def _figure_numbers(venue: Venue, values: dict[str, str]) -> list[tuple[int, str, str]]:
    """``(number, legend, file)`` for each main figure the manuscript or the uploads have."""
    from . import document
    from .citation_check import _FILE_NUMBER, legends, tex_legends
    from .requirements_check import manuscript_files

    found: dict[int, list[str]] = {}
    for _, path in manuscript_files(venue, values):
        doc = document.get(path)
        for number, _, legend in legends(doc.text or "", paragraphs=doc.is_docx):
            found.setdefault(number, [legend, ""])
        if doc.is_tex and doc.tex_source:
            for number, _, legend in tex_legends(doc.tex_source):
                found.setdefault(number, [legend, ""])
    for path in _role_paths(venue, values, "figures"):
        m = _FILE_NUMBER.search(path.stem)
        if m and m.group(0).lower().startswith("f"):
            found.setdefault(int(m.group(1)), ["", ""])[1] = str(path)
    return [(n, legend, path) for n, (legend, path) in sorted(found.items())]


def _reproduction_instructions(minutes: int, download_gb: float) -> str:
    return "Rerun the code for this figure and compare what it draws with the submitted figure. " f"Budget: {minutes} minutes of run time and {download_gb:g} GB of downloads for this figure, and no GPU unless the machine has one. " "Work in a new scratch directory outside the manuscript directory, and never change the manuscript's files. Get the code at the release or commit the manuscript cites " "(else the default branch, and say so). Build a fresh, isolated environment only from what the repository declares (its requirements.txt, environment.yml, renv.lock, " "Dockerfile, ...): a new venv, conda environment, renv library, or container, never your global environment, and do not add packages it does not declare (if you must, say " "which: that is a finding). Use no credentials of your own. Follow the README; find the script for this figure from the README, the figure provenance mapping " "(ai.rigor.figure_provenance) if you made one, or the file names. Run it with a timeout, then look at the output beside the submitted figure, panel by panel. " "Same result means the panels show the same thing and support the same claims (axes, groups, trends, and reported values agree); small differences from random seeds, " "stochastic embeddings such as UMAP or t-SNE, plotting styles, or numerical libraries are not failures, but say what differed. " "Begin details with one word, then explain: 'reproduced' (report pass); 'differs' (report fail, naming each panel and what differs); " "'failed' (report fail: no code draws the figure, an undeclared dependency, a missing input file, an error in the code, with the last lines of the error); " "'not attempted' (report unclear: the data are controlled-access or behind a login, the run would exceed the budget, or it needs hardware this machine lacks; say which). " "A figure that code does not draw (a schematic, photograph, or micrograph) is not_applicable. Delete the scratch directory and environment when done unless the author asks to keep them."


def _queue_tasks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    reproduce = bool(checks.options.get("reproduce_figures"))
    try:
        src = code_sources(venue, values)
    except Exception as exc:  # noqa: BLE001 -- the tasks are optional; never break validation
        logger.debug("could not read code sources: %s", exc)
        return
    manuscript = [str(p) for p in _role_paths(venue, values, "manuscript")]
    if not manuscript:
        return
    files = manuscript + [str(p) for p in _role_paths(venue, values, "figures", "supplementary")]
    from . import report

    if not src.any():
        if reproduce:
            checks.record("ai.figure_reproduction.all", "Figures reproduced by rerunning the authors' code (--reproduce-figures)", CATEGORY, report.UNCHECKED, detail="no code repository, archive, or attached code was found, so no figure could be rerun")
        return
    where = _where_code_is(src)
    checks.needs_ai(AiTask(id="ai.rigor.figure_provenance", title="Each figure panel is traced to the code and public data that draw it", category=CATEGORY, instructions=where + "\n" + _PROVENANCE_TASK, files=list(dict.fromkeys(files))))
    if not reproduce:
        return
    minutes = int(checks.options.get("reproduce_minutes") or DEFAULT_REPRODUCE_MINUTES)
    download_gb = float(checks.options.get("reproduce_download_gb") or DEFAULT_REPRODUCE_DOWNLOAD_GB)
    how = _reproduction_instructions(minutes, download_gb)
    figures = _figure_numbers(venue, values)
    if not figures:
        checks.needs_ai(AiTask(id="ai.figure_reproduction.all", title="Figures reproduced by rerunning the authors' code", category=CATEGORY, instructions=f"{where}\nNo figure legends could be read, so treat every main figure in the manuscript as one task and report on each in details. {how}", files=list(dict.fromkeys(files))))
        return
    for number, legend, path in figures:
        legend_line = f"Legend of Figure {number}: {' '.join(legend.split())[:800]}\n" if legend else ""
        own = [path] if path else []
        checks.needs_ai(AiTask(id=f"ai.figure_reproduction.{number}", title=f"Figure {number} reproduced by rerunning the authors' code", category=CATEGORY, instructions=f"Figure {number}{' (file ' + Path(path).name + ')' if path else ' (embedded in the manuscript)'}.\n{legend_line}{where}\n{how}", files=list(dict.fromkeys(own + manuscript))))


def run_checks(checks: Checks, venue: Venue, values: dict[str, str]) -> None:
    online = bool(checks.options.get("check_repositories", True))
    checks.run("rigor.code_availability", "Code behind the analysis is public, archived, licensed, and declares its environment", CATEGORY, _code_availability_check, venue, values, online)
    checks.run("rigor.data_accessions", "GEO and BioProject accessions given in the manuscript are public", CATEGORY, _data_accessions_check, venue, values, online)
    _queue_tasks(checks, venue, values)
