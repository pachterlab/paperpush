"""``paperpush format``: reformat a manuscript for a venue.

The venue's own template comes first. :mod:`paperpush.templates` looks for one
(the recorded ``manuscript.template_url``, then the venue's author-guideline
pages and the pages they link; for LaTeX also the recorded
``manuscript.latex_class``, from the TeX installation or CTAN) and, when it
finds one that suits the manuscript (a Word template for a .docx, LaTeX files
for a .tex), applies it: :mod:`paperpush.docx_format` or
:mod:`paperpush.tex_format`. Without one, paperpush reformats the manuscript
itself from the layout rules recorded in ``manuscript_requirements.json``: page
size, margins, body font and size, double spacing, line numbers, and page
numbers.

The original files are never modified. The result is written to a new
directory with ``CHANGES.md``, which lists every change, what was left for the
author, and how the result measures against the venue's layout rules (the
same ``layout.*`` checks ``validate`` runs).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess  # nosec B404 -- pandoc with a fixed argv
from dataclasses import dataclass, field
from pathlib import Path

from .database import Venue
from .requirements import ManuscriptRequirements, get_requirements, resolve

logger = logging.getLogger(__name__)

WORD = "word"
LATEX = "latex"


class FormatError(Exception):
    """The manuscript cannot be reformatted (unsupported file, no rules or template, ...)."""


@dataclass
class FormatResult:
    venue: str
    manuscript: Path
    output: Path
    kind: str
    template: str = ""  # where the applied template came from ("" when none)
    template_note: str = ""  # why no template was applied, or what else was found
    searched: list[str] = field(default_factory=list)  # pages read in the template search
    refused: list[str] = field(default_factory=list)  # pages that could not be read
    other_templates: list[str] = field(default_factory=list)  # templates found that were not applied
    changes: list[str] = field(default_factory=list)
    left_to_author: list[str] = field(default_factory=list)
    checks: list[tuple[str, str, str, list[str]]] = field(default_factory=list)  # (id, title, status, messages)
    pdf: Path | None = None
    report: Path | None = None
    upload: Path | None = None  # the file to name in the .sub: the .docx, the main .tex, or the re-bundled .zip

    def to_dict(self) -> dict:
        return {
            "venue": self.venue,
            "manuscript": str(self.manuscript),
            "output": str(self.output),
            "kind": self.kind,
            "template": self.template,
            "template_note": self.template_note,
            "searched": self.searched,
            "refused": self.refused,
            "other_templates": self.other_templates,
            "changes": self.changes,
            "left_to_author": self.left_to_author,
            "layout_checks": [{"id": i, "title": t, "status": s, "issues": m} for i, t, s, m in self.checks],
            "pdf": str(self.pdf) if self.pdf else None,
            "upload": str(self.upload or self.output),
            "report": str(self.report) if self.report else None,
        }


def manuscript_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return WORD
    if suffix in (".tex", ".zip"):
        return LATEX
    if suffix in (".md", ".markdown", ".txt"):
        return "markdown"
    if suffix == ".pdf":
        raise FormatError(f"{path.name} is a PDF, which cannot be reformatted; give its Word (.docx) or LaTeX (.tex/.zip) source")
    if suffix == ".doc":
        raise FormatError(f"{path.name} is a legacy .doc file; save it as .docx in Word and format that")
    raise FormatError(f"{path.name}: paperpush formats .docx, .tex, .zip (LaTeX), and, with pandoc installed, .md files")


def _find_template(reqs: ManuscriptRequirements, kind: str, result: FormatResult, *, override: str | None, browser: bool):
    from . import templates

    if override:
        tpl = templates.download(override)
        if tpl.kind != kind:
            raise FormatError(f"--template {override} is a {tpl.kind} template, but the manuscript is a {kind} file")
        return tpl
    search = templates.find_for_venue(reqs, browser=browser)
    result.searched, result.refused = search.pages, search.refused
    found_other = [link for link in search.links if link not in templates.pick(search, kind)]
    errors: list[str] = []
    for link in templates.pick(search, kind):
        if link.kind == "overleaf":
            result.other_templates.append(link.describe() + " (Overleaf: download the project as a zip and pass it with --template)")
            continue
        try:
            tpl = templates.download(link, prefer=_style_hint(reqs))
        except OSError as exc:
            errors.append(f"{link.url}: {exc}")
            continue
        if tpl.kind == kind:
            return tpl
        result.other_templates.append(f"{link.describe()} (a {tpl.kind} template)")
    if kind == LATEX and reqs.manuscript.latex_class:
        name = reqs.manuscript.latex_class
        tpl = templates.installed_class(name)
        if tpl is None:
            link = templates.ctan_link(name)
            if link is not None:
                try:
                    tpl = templates.download(link, prefer=_style_hint(reqs))
                except OSError as exc:
                    errors.append(f"{link.url}: {exc}")
        if tpl is not None:
            return tpl
    result.other_templates += [link.describe() for link in found_other]
    notes = []
    if result.other_templates and not any(kind_word in t for t in result.other_templates for kind_word in ((" Word", "word template") if kind == WORD else ("LaTeX", "latex template"))):
        notes.append(f"the venue offers no {'Word' if kind == WORD else 'LaTeX'} template, only the ones listed below")
    elif errors:
        notes.append("could not use " + "; ".join(errors))
    if not search.links:
        notes.append("no template link found on the venue's author-guideline pages" + (f" ({len(search.refused)} page(s) could not be read)" if search.refused else ""))
    result.template_note = "; ".join(notes) or f"no {kind} template found"
    return None


def _style_hint(reqs: ManuscriptRequirements) -> str:
    """Words that pick the right sample among a template's several (numbered vs author-year references)."""
    style = (reqs.references.style or "").lower()
    numbered = reqs.references.numbered
    words = style + (" num numbered" if numbered or re.search(r"numbered|\bnum\b|vancouver|ieee", style) else "") + (" harv authoryear" if numbered is False or re.search(r"author[- ]?year|harvard|apa", style) else "")
    return words


def _markdown_to_docx(path: Path, out: Path, reference: Path | None) -> Path:
    exe = shutil.which("pandoc")
    if not exe:
        raise FormatError(f"{path.name} is Markdown/plain text; install pandoc (https://pandoc.org) to format it, or convert it to .docx or .tex first")
    target = out / (path.stem + ".docx")
    argv = [exe, str(path), "-o", str(target)]
    if reference is not None:
        argv += ["--reference-doc", str(reference)]
    run = subprocess.run(argv, capture_output=True, text=True, timeout=300, cwd=path.parent, check=False)  # nosec B603
    if run.returncode != 0 or not target.exists():
        raise FormatError(f"pandoc could not convert {path.name}: {run.stderr.strip()[:300]}")
    return target


def _verify(venue: Venue, values: dict[str, str], output: Path, result: FormatResult) -> None:
    """Measure the output with validate's page-layout checks."""
    from . import layout_check
    from .requirements_check import _fields_for
    from .validate import Checks

    fields = _fields_for(venue, "manuscript")
    if not fields:
        return
    checked = dict(values)
    for f in fields:
        checked.pop(f.id, None)
    checked[fields[0].id] = str(output)
    checks = Checks()
    try:
        layout_check.run_checks(checks, venue, checked)
    except Exception as exc:  # noqa: BLE001 -- verification must not lose the formatted file
        logger.warning("could not measure the formatted manuscript: %s", exc)
        return
    for r in checks.results:
        if r.id.startswith("layout."):
            result.checks.append((r.id, r.title, r.status, [i.message for i in r.issues]))


def _compile(main: Path, out_dir: Path) -> tuple[Path | None, str]:
    from . import manuscript

    if manuscript.tex_toolchain() is None:
        return None, "no TeX installation found, so the result was not compiled"
    build = out_dir / ".build"
    build.mkdir(exist_ok=True)
    pdf = manuscript._compile(main, build)
    if pdf is None:
        logs = sorted(build.rglob("*.log"))
        tail = ""
        if logs:
            lines = [ln for ln in logs[0].read_text("utf-8", "replace").splitlines() if ln.startswith("!")][:3]
            tail = ": " + " / ".join(lines) if lines else ""
            missing = re.search(r"File `([\w.-]+)\.(?:sty|cls)' not found", logs[0].read_text("utf-8", "replace"))
            if missing:
                return None, f"the formatted LaTeX needs {missing.group(1)}, which your TeX installation lacks (install it, e.g. 'tlmgr install {missing.group(1)}'), then compile {main.name}"
        return None, f"the formatted LaTeX did not compile{tail}; see {main.name} and fix it by hand"
    final = out_dir / (main.stem + ".pdf")
    shutil.copy2(pdf, final)
    shutil.rmtree(build, ignore_errors=True)
    return final, ""


def format_manuscript(manuscript: Path, venue: Venue, values: dict[str, str] | None = None, *, out_dir: Path | None = None, template: str | None = None, use_template: bool = True, browser: bool = True, keep_direct_formatting: bool = False, compile_pdf: bool = True) -> FormatResult:
    """Reformat ``manuscript`` for ``venue``; see the module docstring."""
    from . import docx_format, tex_format

    values = dict(values or {})
    manuscript = manuscript.expanduser().resolve()
    if not manuscript.is_file():
        raise FormatError(f"{manuscript} does not exist")
    base = get_requirements(venue.slug)
    if base is None:
        raise FormatError(f"paperpush has no author guidelines recorded for {venue.slug}; add them with the paperpush-add-venue-validate skill, or pass --template")
    reqs = resolve(base, values)
    kind = manuscript_kind(manuscript)
    out_dir = (out_dir or manuscript.parent / f"formatted_{venue.slug}").expanduser().resolve()
    if out_dir == manuscript.parent:
        raise FormatError("the output directory must not be the manuscript's own directory (the original is never overwritten)")
    out_dir.mkdir(parents=True, exist_ok=True)
    result = FormatResult(venue=venue.slug, manuscript=manuscript, output=out_dir, kind=kind)

    want = WORD if kind in (WORD, "markdown") else LATEX
    tpl = None
    if use_template or template:
        tpl = _find_template(reqs, want, result, override=template, browser=browser)
    elif not use_template:
        result.template_note = "template search skipped (--no-template)"

    if kind == "markdown":
        manuscript_docx = _markdown_to_docx(manuscript, out_dir, tpl.path if tpl else None)
        result.changes.append(f"converted {manuscript.name} to Word with pandoc" + (" using the template as the reference document" if tpl else ""))
        kind = result.kind = WORD
        source = manuscript_docx
    else:
        source = manuscript

    if kind == WORD:
        output = out_dir / f"{manuscript.stem}_{venue.slug}.docx"
        doc = docx_format.DocxFile.open(source)
        if tpl is not None:
            template_doc = docx_format.DocxFile.open(tpl.path)
            result.changes += docx_format.apply_template(doc, template_doc, keep_direct=keep_direct_formatting)
            result.template = tpl.source
        changes, skipped = docx_format.apply_rules(doc, reqs, keep_direct=keep_direct_formatting)
        result.changes += [c + (" (the guidelines' rule, on top of the template)" if tpl is not None else "") for c in changes]
        result.left_to_author += skipped
        doc.save(output)
        if source != manuscript and source.parent == out_dir:
            source.unlink(missing_ok=True)
    else:
        work = out_dir / f"{manuscript.stem}_{venue.slug}"
        if work.exists():
            shutil.rmtree(work)
        main = tex_format.copy_sources(manuscript, work)
        if tpl is not None:
            changes, todo = tex_format.apply_template(main, tpl)
            result.template = tpl.source
            result.changes += changes
            result.left_to_author += todo
        else:
            changes, skipped = tex_format.apply_rules(main, reqs)
            result.changes += changes
            result.left_to_author += skipped
        output = main
        if manuscript.suffix.lower() == ".zip":
            archive = shutil.make_archive(str(out_dir / f"{manuscript.stem}_{venue.slug}"), "zip", work)
            result.changes.append(f"bundled the formatted sources as {Path(archive).name}")
            result.upload = Path(archive)
        if compile_pdf:
            result.pdf, note = _compile(main, out_dir)
            if note:
                result.left_to_author.append(note)
    result.output = output
    result.upload = result.upload or output

    if not result.changes:
        result.left_to_author.append("nothing to change: the venue records no layout rules and no template was found")
    result.left_to_author += _not_handled(reqs)
    _verify(venue, values, result.pdf or output if kind == LATEX else output, result)
    result.report = _write_report(result, out_dir)
    return result


def _not_handled(reqs: ManuscriptRequirements) -> list[str]:
    """The venue's rules ``format`` does not apply, so the author knows what is left."""
    out = []
    if reqs.references.style:
        out.append(f"reference style ({reqs.references.style}): not converted; check with 'paperpush validate' and the /reference-checking skill")
    if reqs.manuscript.figures_placement or reqs.figures.placement:
        out.append(f"figure placement ({reqs.manuscript.figures_placement or reqs.figures.placement}): figures were not moved")
    if reqs.sections.order:
        out.append("section order: sections were not reordered (validate's guidelines.section_order reports it)")
    if reqs.title_page.separate_file:
        out.append("title page: the guidelines want it as a separate file; it was not split out")
    if reqs.manuscript.anonymized:
        out.append("anonymization: author names and affiliations were not removed (validate's integrity.anonymity reports them)")
    return out


def _write_report(result: FormatResult, out_dir: Path) -> Path:
    lines = [f"# {result.manuscript.name} formatted for {result.venue}", "", f"Output: `{result.output}`" + (f"  \nPDF: `{result.pdf}`" if result.pdf else ""), ""]
    lines.append("## Template")
    lines.append("")
    if result.template:
        lines.append(f"Applied the venue's template: {result.template}")
    else:
        lines.append(f"No template applied ({result.template_note or 'none found'}); the manuscript was reformatted from the venue's recorded layout rules.")
    if result.other_templates:
        lines += ["", "Other templates found:"] + [f"- {t}" for t in result.other_templates]
    if result.refused:
        lines += ["", "Guideline pages that could not be read:"] + [f"- {r}" for r in result.refused]
    lines += ["", "## Changes", ""] + ([f"- {c}" for c in result.changes] or ["- none"])
    if result.left_to_author:
        lines += ["", "## Left for the author", ""] + [f"- {c}" for c in result.left_to_author]
    if result.checks:
        lines += ["", "## Page layout of the result (validate's layout checks)", ""]
        for cid, title, status, messages in result.checks:
            lines.append(f"- **{status}** `{cid}`: {title}")
            lines += [f"  - {m}" for m in messages]
    lines += ["", "The original files were not modified. Run `paperpush validate` on the .sub after pointing it at the new file."]
    path = out_dir / "CHANGES.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def list_templates(venue: Venue, values: dict[str, str] | None = None, *, browser: bool = True):
    """The template search alone: every template link found for the venue."""
    from . import templates

    base = get_requirements(venue.slug)
    if base is None:
        raise FormatError(f"paperpush has no author guidelines recorded for {venue.slug}")
    return templates.find_for_venue(resolve(base, values or {}), browser=browser)
