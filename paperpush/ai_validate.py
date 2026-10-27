"""The validation checks only a reader can do, run by an LLM or an agent.

Some guideline rules cannot be measured from a file's bytes: how large the text
is inside a raster figure, whether its panels are lettered, whether a postal
address is on the title page, whether the abstract in the form says the same
as the one in the paper, or the dozens of free-text rules each venue's
guidelines carry (the ``notes`` in ``manuscript_requirements.json``). The
deterministic checks queue these as :class:`AiTask` objects; this module then
resolves them in one of three ways, chosen by :class:`AiOptions`:

* **API** (``paperpush validate --ai``): one request to an LLM provider --
  Anthropic, OpenAI, or Google, whichever key is set, through the same callers
  ``paperpush autofill --engine api`` uses -- with the manuscript, figures, and
  other files attached and every task listed; the model returns one verdict per
  task in a fixed JSON shape.
* **Agent results** (``--ai-results FILE``): an agent (Claude Code, Codex, ...)
  ran the tasks itself, from the task file ``--ai-tasks FILE`` writes (or the
  MCP ``validation_ai_tasks`` tool), and wrote its verdicts in that same shape.
  This uses the agent's own subscription; no API key is needed.
* **Neither**: each task is reported as *needs AI*, so the report says plainly
  what was not checked.

A verdict is ``pass``, ``fail``, ``unclear``, or ``not_applicable``. A failed
AI check is reported as a warning: it is a reader's judgement, not a
measurement, so it never blocks a submission on its own.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .database import Venue

logger = logging.getLogger(__name__)

VERDICTS = ("pass", "fail", "unclear", "not_applicable")

_SYSTEM = "You are a meticulous pre-submission checker for academic manuscripts. You are given " "the files of one submission and a list of checks. For each check, read the files it " "names and decide: 'pass' if the submission meets it, 'fail' if it does not, " "'not_applicable' if the check does not apply to this submission, or 'unclear' if the " "files do not let you decide. Judge only from the files; do not assume. For 'fail', " "'details' must say exactly what is wrong and where (file, page, figure, panel) so the " "author can fix it; for 'pass', say briefly what you verified. Quote short evidence."


@dataclass(frozen=True)
class AiTask:
    """One check for an LLM or agent: what to judge, and which files to read."""

    id: str
    title: str
    category: str
    instructions: str
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "category": self.category, "instructions": self.instructions, "files": list(self.files)}


@dataclass
class AiOptions:
    """How to resolve the AI checks: an API ``provider``/``model``, or a ``results`` file.

    ``mode`` is ``"api"`` or ``"results"``. ``error`` is set when the API call
    or results file fails, and is shown in the report.
    """

    mode: str
    provider: Optional[str] = None
    model: Optional[str] = None
    results: Optional[dict[str, dict]] = None
    results_path: str = ""
    error: str = ""
    description: str = ""
    # A key handed over directly (the website's visitors bring their own)
    # instead of read from the provider's environment variable.
    api_key: Optional[str] = field(default=None, repr=False)

    @classmethod
    def api(cls, provider: Optional[str] = None, model: Optional[str] = None, api_key: Optional[str] = None) -> "AiOptions":
        return cls(mode="api", provider=provider, model=model, api_key=api_key)

    @classmethod
    def from_results(cls, data: Any, path: str = "") -> "AiOptions":
        return cls(mode="results", results=parse_results(data), results_path=path, description=f"agent results from {path}" if path else "agent results")


def parse_results(data: Any) -> dict[str, dict]:
    """An agent's verdicts keyed by task id.

    Accepts ``{"results": [{"id", "status", "details"}, ...]}``, a bare list of
    such objects, or a mapping ``{task id: {"status", "details"}}``.
    """
    if isinstance(data, dict) and "results" in data:
        data = data["results"]
    out: dict[str, dict] = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                out[str(item["id"])] = item
    elif isinstance(data, dict):
        for key, item in data.items():
            if isinstance(item, dict):
                out[str(key)] = {"id": key, **item}
            elif isinstance(item, str):
                out[str(key)] = {"id": key, "status": item}
    else:
        raise ValueError("expected a JSON object with a 'results' list")
    for task_id, item in out.items():
        status = str(item.get("status", "")).strip().lower().replace(" ", "_").replace("-", "_")
        status = {"passed": "pass", "ok": "pass", "failed": "fail", "warning": "fail", "na": "not_applicable", "n/a": "not_applicable"}.get(status, status)
        if status not in VERDICTS:
            raise ValueError(f"result {task_id!r} has status {item.get('status')!r}; use one of {', '.join(VERDICTS)}")
        item["status"] = status
    return out


# --- tasks the guidelines themselves call for ----------------------------------

_NOTES_FILES = {
    "manuscript": ("manuscript",),
    "title_page": ("manuscript",),
    "abstract": ("manuscript",),
    "keywords": ("manuscript",),
    "sections": ("manuscript",),
    "statements": ("manuscript",),
    "figures": ("manuscript", "figures"),
    "tables": ("manuscript", "tables"),
    "supplementary": ("supplementary",),
    "references": ("manuscript",),
    "cover_letter": ("cover_letter",),
    "upload": (),
}
_NOTES_TITLES = {
    "": "General rules in the author guidelines",
    "manuscript": "Free-text manuscript rules in the author guidelines",
    "title_page": "Free-text title-page rules",
    "abstract": "Free-text abstract rules",
    "keywords": "Free-text keyword rules",
    "sections": "Free-text section rules",
    "statements": "Free-text rules for statements and declarations",
    "figures": "Free-text figure rules",
    "tables": "Free-text table rules",
    "supplementary": "Free-text supplementary-material rules",
    "references": "Free-text reference rules",
    "cover_letter": "Free-text cover-letter rules",
    "upload": "Free-text upload rules",
}
# Free-text sub fields compared against the manuscript in the consistency task.
_CONSISTENCY_SKIP_TYPES = {"file", "filelist", "boolean"}
_MAX_VALUE_CHARS = 3000


def _role_paths(venue: Venue, values: dict[str, str], role: str) -> list[str]:
    from .requirements_check import _existing, _fields_for, _paths

    return [str(p) for f in _fields_for(venue, role) for p in _existing(_paths(f, values))]


def guideline_tasks(venue: Venue, values: dict[str, str]) -> list[AiTask]:
    """Tasks for the venue's free-text rules and the .sub-versus-manuscript comparison."""
    from .requirements import SECTION_TYPES, get_requirements, resolve

    manuscript = _role_paths(venue, values, "manuscript")
    tasks: list[AiTask] = []
    base = get_requirements(venue.slug)
    if base is not None:
        reqs = resolve(base, values)
        groups: list[tuple[str, list[str]]] = [("", list(reqs.notes))]
        groups += [(key, list(getattr(reqs, key).notes)) for key in SECTION_TYPES]
        for section, notes in groups:
            if not notes:
                continue
            files: list[str] = []
            for role in _NOTES_FILES.get(section, ("manuscript",)):
                files += _role_paths(venue, values, role)
            if section in ("supplementary", "cover_letter", "upload") and not files:
                continue
            files = files or manuscript
            rules = "\n".join(f"  {i}. {note}" for i, note in enumerate(notes, start=1))
            tasks.append(
                AiTask(
                    id=f"ai.notes.{section or 'general'}",
                    title=_NOTES_TITLES.get(section, f"Free-text {section} rules"),
                    category="Guidelines",
                    instructions=(f"The author guidelines of {venue.full_name or venue.name} state these rules{f' about the {section.replace(chr(95), chr(32))}' if section else ''}:\n{rules}\n" "Check the submission files against every rule that can be judged from them. Rules about the portal, fees, or what happens after submission are not_applicable. " "Report 'fail' and list each rule (by number) the files break, or 'pass' if they meet all that apply."),
                    files=files,
                )
            )
        if reqs.tables.editable and any(Path(p).suffix.lower() == ".pdf" for p in manuscript):
            tasks.append(AiTask(id="ai.tables_editable", title="Tables are editable text, not images", category="Guidelines", instructions="The guidelines require tables to be editable text. Look at every table in the manuscript: report 'fail' naming each table that is a picture (a pasted screenshot or image) rather than typeset text, 'pass' if all are text, 'not_applicable' if there are no tables.", files=manuscript))
        refs = reqs.references
        if refs.style or refs.include_titles:
            wants = []
            if refs.style:
                wants.append(f"the reference style is: {refs.style}")
            if refs.include_titles:
                wants.append("every journal reference includes the article title")
            tasks.append(AiTask(id="ai.reference_format", title="Reference list follows the guidelines' style", category="Guidelines", instructions=f"Read the reference list. The guidelines say {'; and '.join(wants)}. Report 'fail' with examples of entries that do not follow it, else 'pass'.", files=manuscript))
    if manuscript:
        fields = []
        for f in venue.fields:
            value = values.get(f.id, "").strip()
            if not value or f.type in _CONSISTENCY_SKIP_TYPES or f.autofill == "never":
                continue
            if len(value) > _MAX_VALUE_CHARS:
                value = value[:_MAX_VALUE_CHARS] + " [...]"
            fields.append(f"--- {f.id} ({f.label}) ---\n{value}")
        if fields:
            files = manuscript + [p for f in venue.fields if "title_page" in f.id for p in _role_paths_for_field(f, values)]
            tasks.append(
                AiTask(
                    id="ai.consistency",
                    title="Every .sub field agrees with the manuscript",
                    category="Consistency",
                    instructions=("These are the values entered in the submission form. Compare each with what the manuscript says: the title and abstract must match it word for word; " "authors must be the same people in the same order, with the same affiliations, emails, and ORCID iDs where the manuscript gives them; keywords, statements " "(data/code availability, competing interests, funding, contributions), counts, and every other value must agree with the manuscript. Report 'fail' listing " "each field that differs and how, or 'pass'.\n\n" + "\n\n".join(fields)),
                    files=files,
                )
            )
    return tasks


def _role_paths_for_field(field, values: dict[str, str]) -> list[str]:
    from .requirements_check import _existing, _paths

    return [str(p) for p in _existing(_paths(field, values))]


def all_tasks(checks, venue: Venue, values: dict[str, str]) -> list[AiTask]:
    """Every AI task for this submission: the ones the checks queued, then the guideline ones."""
    seen: dict[str, AiTask] = {}
    for task in list(checks.ai_tasks) + guideline_tasks(venue, values):
        if task.id in seen:
            # Two checks queued the same task id: merge their files.
            old = seen[task.id]
            seen[task.id] = AiTask(old.id, old.title, old.category, old.instructions, list(dict.fromkeys(old.files + task.files)))
        else:
            seen[task.id] = task
    return list(seen.values())


# --- resolving the tasks --------------------------------------------------------


def resolve(checks, venue: Venue, values: dict[str, str], ai: Optional[AiOptions]) -> list[AiTask]:
    """Record a result for every AI task in ``checks``, according to ``ai``; return the tasks."""
    from . import report

    tasks = all_tasks(checks, venue, values)
    if not tasks:
        return tasks
    verdicts: dict[str, dict] = {}
    reason = "run with --ai (needs an API key) or have an agent do it with the paperpush-validate skill"
    if ai is not None and ai.mode == "results":
        verdicts = ai.results or {}
        reason = "the AI results file has no verdict for this check"
    elif ai is not None and ai.mode == "api":
        try:
            verdicts = run_api(tasks, venue, ai)
        except Exception as exc:  # noqa: BLE001 -- report it; the deterministic checks stand
            message = str(exc)
            if ai.api_key:
                # A provider may echo the key back; keep it out of the report and the log.
                message = message.replace(ai.api_key, "[API key]")
            ai.error = message
            logger.warning("AI checks could not run: %s", message)
            reason = f"the AI request failed: {message}"
    for task in tasks:
        verdict = verdicts.get(task.id)
        if verdict is None:
            checks.record(task.id, task.title, task.category, report.NEEDS_AI, detail=reason)
            continue
        details = str(verdict.get("details", "") or "").strip()
        evidence = str(verdict.get("evidence", "") or "").strip()
        message = details + (f" (evidence: {evidence})" if evidence else "")
        status = verdict["status"]
        if status == "pass":
            checks.record(task.id, task.title, task.category, report.PASSED, detail=message, source="ai")
        elif status == "fail":
            from .validate import WARNING, Issue

            checks.record(task.id, task.title, task.category, report.WARNING, issues=[Issue(WARNING, "", message or "the AI check failed (no details given)")], source="ai")
        elif status == "not_applicable":
            checks.record(task.id, task.title, task.category, report.NOT_APPLICABLE, detail=message, source="ai")
        else:
            checks.record(task.id, task.title, task.category, report.UNCHECKED, detail=f"the AI could not decide: {message}" if message else "the AI could not decide", source="ai")
    return tasks


def _schema(task_ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": task_ids},
                        "status": {"type": "string", "enum": list(VERDICTS)},
                        "details": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["id", "status", "details", "evidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def _task_text(tasks: list[AiTask], labels: dict[str, str]) -> str:
    parts = []
    for task in tasks:
        files = ", ".join(labels.get(f, Path(f).name) for f in task.files) or "all attached files"
        parts.append(f"### {task.id}: {task.title}\nFiles: {files}\n{task.instructions}")
    return "Run each of these checks and return one result per check id.\n\n" + "\n\n".join(parts)


def run_api(tasks: list[AiTask], venue: Venue, ai: AiOptions, max_tokens: int = 16000) -> dict[str, dict]:
    """Ask an LLM API to run ``tasks``; return its verdicts keyed by task id."""
    import importlib

    # The package exports an ``autofill`` function that shadows the module name.
    autofill = importlib.import_module("paperpush.autofill")

    provider = autofill.resolve_provider(ai.provider, alternative="run the checks with an agent: --ai-tasks / --ai-results", api_key=ai.api_key)
    model = ai.model or autofill.DEFAULT_MODELS[provider]
    ai.description = f"{provider} {model}"
    paths: list[str] = list(dict.fromkeys(f for task in tasks for f in task.files))
    documents = []
    labels: dict[str, str] = {}
    for number, path in enumerate(paths, start=1):
        label = f"file {number}: {Path(path).name}"
        labels[path] = label
        documents.append(autofill.DocumentInput(label=label, path=Path(path)))
    instructions = f"Venue: {venue.full_name or venue.name} ({venue.slug}).\n\n" + _task_text(tasks, labels)
    logger.info("validate ai: calling %s %s for %s (%d file(s), %d check(s))", provider, model, venue.slug, len(documents), len(tasks))
    text = autofill._CALLERS[provider](model, documents, instructions, _schema([t.id for t in tasks]), max_tokens, system=_SYSTEM, **autofill._client_kwargs(ai.api_key))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise autofill.AutofillApiError(f"the model returned output that was not valid JSON: {exc}") from exc
    return parse_results(data)


def tasks_payload(tasks: list[AiTask], venue: Venue, subfile: str = "") -> dict[str, Any]:
    """The task file for an agent (``paperpush validate --ai-tasks``)."""
    return {
        "venue": venue.slug,
        "subfile": subfile,
        "instructions": ("For each task, read the files it lists and judge it as the task says. " 'Write {"results": [{"id": <task id>, "status": "pass" | "fail" | "unclear" | "not_applicable", ' '"details": <what you found>, "evidence": <a short quote or location>}]} to a JSON file, then run ' f"`paperpush validate {subfile or '<venue>.sub'} --ai-results <that file>` to merge the verdicts into the report."),
        "system": _SYSTEM,
        "tasks": [t.to_dict() for t in tasks],
    }
