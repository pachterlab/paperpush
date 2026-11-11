"""``paperpush convert``: move a manuscript between Markdown, Word, and LaTeX.

Readers parse a manuscript into one document model
(:mod:`~paperpush.convert.model`); writers produce Word or LaTeX from it.
Equations, citations, and cross-references stay live in both directions, and
every conversion re-reads its own output and reports what did not survive.

The original is never modified. The result goes to a new directory together
with ``CONVERSION.md``.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .model import Cite, Document, Equation, Figure, Heading, Log, Math, Note, Ref, Table, all_blocks, all_inlines, plain_text

DOCX = "docx"
TEX = "tex"
MARKDOWN = "md"
TARGETS = (DOCX, TEX)
SUPPORTED = {(MARKDOWN, DOCX), (MARKDOWN, TEX), (DOCX, TEX), (TEX, DOCX)}
TRACKED_CHOICES = ("accept", "reject")


class ConvertError(Exception):
    """The manuscript cannot be converted (unsupported direction, unreadable file, ...)."""


@dataclass
class ConvertResult:
    manuscript: Path
    output: Path
    source_format: str
    target_format: str
    report: Path | None = None
    pdf: Path | None = None
    upload: Path | None = None
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, tuple[int, int]] = field(default_factory=dict)  # name -> (source, output)
    restored_blocks: int = 0
    generated_blocks: int = 0
    compiled: bool | None = None  # None when no compile was attempted

    @property
    def clean(self) -> bool:
        return not self.warnings and all(a == b for name, (a, b) in self.counts.items() if name != "words") and self.compiled is not False

    def to_dict(self) -> dict:
        return {
            "manuscript": str(self.manuscript),
            "output": str(self.output),
            "upload": str(self.upload or self.output),
            "source_format": self.source_format,
            "target_format": self.target_format,
            "clean": self.clean,
            "counts": {name: {"source": a, "output": b} for name, (a, b) in self.counts.items()},
            "changes": self.changes,
            "warnings": self.warnings,
            "restored_blocks": self.restored_blocks,
            "generated_blocks": self.generated_blocks,
            "compiled": self.compiled,
            "pdf": str(self.pdf) if self.pdf else None,
            "report": str(self.report) if self.report else None,
        }


def format_of(path: Path) -> str:
    """The format of a manuscript file, by its extension."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return DOCX
    if suffix in (".tex", ".zip"):
        return TEX
    if suffix in (".md", ".markdown"):
        return MARKDOWN
    if suffix == ".pdf":
        raise ConvertError(f"{path.name} is a PDF, which cannot be converted; give its Word, LaTeX, or Markdown source")
    if suffix == ".doc":
        raise ConvertError(f"{path.name} is a legacy .doc file; save it as .docx in Word and convert that")
    raise ConvertError(f"{path.name}: paperpush converts .md, .docx, .tex, and .zip (LaTeX) files")


def _read(path: Path, kind: str, log: Log, work: Path, *, bib: Path | None, tracked: str, use_manifest: bool = True) -> tuple[Document, Path]:
    """Parse ``path``; returns the model and the directory its relative files live in."""
    from .read_docx import DocxReader
    from .read_md import MarkdownReader
    from .read_tex import TexReader

    if kind == DOCX:
        return DocxReader(path, log, work / "docx_images", tracked=tracked, use_manifest=use_manifest).read(), path.parent
    if kind == MARKDOWN:
        return MarkdownReader(path, log, bib=bib).read(), path.parent
    main = path
    if path.suffix.lower() == ".zip":
        from ..manuscript import _main_tex
        from ..templates import _safe_extract

        target = work / "bundle"
        target.mkdir(parents=True, exist_ok=True)
        _safe_extract(path, target)
        found = _main_tex(sorted(target.rglob("*.tex")))
        if found is None:
            raise ConvertError(f"{path.name} holds no .tex file with \\documentclass")
        main = found
    return TexReader(main, log, bib=bib).read(), main.parent


def _counts(doc: Document) -> dict[str, int]:
    from .model import number_labels

    blocks = list(all_blocks(doc.blocks))
    inlines = list(all_inlines(doc))
    known = number_labels(doc)
    words = sum(len(plain_text(b.children).split()) for b in blocks if hasattr(b, "children"))
    cites = [n for n in inlines if isinstance(n, Cite)]
    return {
        "headings": sum(isinstance(b, Heading) for b in blocks),
        "words": words,
        "inline equations": sum(isinstance(n, Math) for n in inlines),
        "display equations": sum(isinstance(b, Equation) for b in blocks),
        "figures": sum(isinstance(b, Figure) for b in blocks),
        "tables": sum(isinstance(b, Table) for b in blocks),
        "citations": len(cites),
        "cited references": len({k for c in cites for k in c.keys}),
        "cross-references": sum(isinstance(n, Ref) and n.label in known and n.label not in doc.extra_labels for n in inlines),
        "footnotes": sum(isinstance(n, Note) for n in inlines),
    }


def _check_links(doc: Document, log: Log) -> None:
    """Dangling citations and cross-references, reported once, before writing."""
    from .model import number_labels

    known = number_labels(doc)
    missing_refs = sorted({n.label for n in all_inlines(doc) if isinstance(n, Ref) and n.label not in known})
    for label in missing_refs:
        log.warn(f"cross-reference to '{label}' has no matching label in the source")
    missing_cites = sorted({k for n in all_inlines(doc) if isinstance(n, Cite) for k in n.keys if k not in doc.bib})
    if missing_cites:
        shown = ", ".join(missing_cites[:10]) + (f", and {len(missing_cites) - 10} more" if len(missing_cites) > 10 else "")
        log.warn(f"{len(missing_cites)} citation key(s) have no bibliography entry: {shown}. Pass the .bib with --bib")


def _compile(main: Path, out_dir: Path, log: Log) -> tuple[Path | None, bool | None]:
    from .. import manuscript

    if manuscript.tex_toolchain() is None:
        log.warn("no TeX installation found, so the LaTeX output was not compiled")
        return None, None
    build = out_dir / ".build"
    build.mkdir(exist_ok=True)
    pdf = manuscript._compile(main, build)
    errors: list[str] = []
    logs = sorted(build.glob("*.log"))
    if logs:
        errors = [ln for ln in logs[0].read_text("utf-8", "replace").splitlines() if ln.startswith("!")][:3]
    final = None
    if pdf is not None:
        final = out_dir / (main.stem + ".pdf")
        shutil.copy2(pdf, final)
    shutil.rmtree(build, ignore_errors=True)
    if errors or pdf is None:
        log.warn(f"the LaTeX output did not compile cleanly: {' / '.join(errors) or 'no PDF was produced'}")
        return final, False
    return final, True


def convert_manuscript(manuscript: Path, to: str, *, out_dir: Path | None = None, bib: Path | None = None, assets: Path | None = None, tracked: str = "accept", compile_pdf: bool = True, citation_style: str | None = None, embed_source: bool = True) -> ConvertResult:
    """Convert ``manuscript`` to ``to`` (``docx`` or ``tex``); see the module docstring.

    ``assets`` is where the original figures and the venue's style files are,
    for a Word file going back to LaTeX away from its project. ``embed_source``
    False leaves the original LaTeX out of a .docx, for one that goes to a
    journal rather than back to the authors.
    """
    manuscript = manuscript.expanduser().resolve()
    if not manuscript.is_file():
        raise ConvertError(f"{manuscript} does not exist")
    if to not in TARGETS:
        raise ConvertError(f"cannot convert to '{to}': choose one of {', '.join(TARGETS)}")
    if tracked not in TRACKED_CHOICES:
        raise ConvertError(f"--tracked-changes must be one of {', '.join(TRACKED_CHOICES)}")
    source = format_of(manuscript)
    if source == to:
        raise ConvertError(f"{manuscript.name} is already {to}; use 'paperpush format' to restyle it for a venue")
    if (source, to) not in SUPPORTED:
        raise ConvertError(f"paperpush does not convert {source} to {to}")
    out_dir = (out_dir or manuscript.parent / f"converted_{to}").expanduser().resolve()
    if out_dir == manuscript.parent:
        raise ConvertError("the output directory must not be the manuscript's own directory (the original is never overwritten)")
    out_dir.mkdir(parents=True, exist_ok=True)
    if bib is not None:
        bib = bib.expanduser().resolve()
        if not bib.is_file():
            raise ConvertError(f"{bib} does not exist")

    log = Log()
    result = ConvertResult(manuscript=manuscript, output=out_dir, source_format=source, target_format=to)
    with tempfile.TemporaryDirectory(prefix="paperpush-convert-") as tmp:
        work = Path(tmp)
        try:
            doc, root = _read(manuscript, source, log, work, bib=bib, tracked=tracked)
        except (ValueError, OSError) as exc:
            raise ConvertError(f"could not read {manuscript.name}: {exc}") from exc
        if citation_style:
            doc.citation_style = citation_style
        _check_links(doc, log)
        if to == DOCX:
            output = _write_docx(doc, log, out_dir, manuscript, root, work, embed_source)
        else:
            output, restored, generated = _write_tex(doc, log, out_dir, manuscript, assets.expanduser().resolve() if assets else root)
            result.restored_blocks, result.generated_blocks = restored, generated
            if compile_pdf:
                result.pdf, result.compiled = _compile(output, out_dir, log)
        before = _counts(doc)
        _audit(output, to, before, result, log, work, bib, tracked)
    log.flush()
    result.output = output
    result.upload = output
    result.changes, result.warnings = log.changes, log.warnings
    result.report = _write_report(result, out_dir)
    return result


def _write_docx(doc: Document, log: Log, out_dir: Path, manuscript: Path, root: Path, work: Path, embed_source: bool) -> Path:
    from .write_docx import DocxWriter

    output = out_dir / (manuscript.stem + ".docx")
    writer = DocxWriter(doc, log, work / "images", root)
    writer.write(output, title=plain_text(doc.title), embed_source=embed_source)
    return output


def _write_tex(doc: Document, log: Log, out_dir: Path, manuscript: Path, root: Path) -> tuple[Path, int, int]:
    from .write_tex import TexWriter

    output = out_dir / (manuscript.stem + ".tex")
    writer = TexWriter(doc, log, out_dir, source_dir=root)
    writer.write(output)
    return output, writer.restored, writer.generated


def _audit(output: Path, to: str, before: dict[str, int], result: ConvertResult, log: Log, work: Path, bib: Path | None, tracked: str) -> None:
    """Re-read the output and compare what is in it against the source."""
    scratch = Log()
    try:
        again, _ = _read(output, to, scratch, work / "audit", bib=None, tracked=tracked)
    except (ValueError, OSError) as exc:
        log.warn(f"could not re-read the output to check it: {exc}")
        return
    after = _counts(again)
    result.counts = {name: (before[name], after.get(name, 0)) for name in before}
    for name, (a, b) in result.counts.items():
        if name == "words":
            if a and abs(a - b) > max(5, 0.02 * a):
                log.warn(f"word count changed from {a} to {b}: compare the two files")
        elif a != b:
            log.warn(f"{name}: {a} in the source, {b} in the output")


def _write_report(result: ConvertResult, out_dir: Path) -> Path:
    lines = [f"# {result.manuscript.name} converted to {result.target_format}", "", f"Output: `{result.output}`"]
    if result.pdf:
        lines.append(f"PDF: `{result.pdf}`")
    lines += ["", "**Result: clean.**" if result.clean else "**Result: needs review. See below.**", ""]
    if result.counts:
        lines += ["## What came through", "", "| | Source | Output |", "| --- | ---: | ---: |"]
        lines += [f"| {name} | {a} | {b} |" for name, (a, b) in result.counts.items()]
        lines.append("")
    if result.restored_blocks or result.generated_blocks:
        lines += [f"{result.restored_blocks} block(s) restored as their original LaTeX, {result.generated_blocks} generated (new or edited in Word).", ""]
    if result.warnings:
        lines += ["## Check these", ""] + [f"- {w}" for w in result.warnings] + [""]
    if result.changes:
        lines += ["## Normalized", ""] + [f"- {c}" for c in result.changes] + [""]
    lines.append("The original file was not modified.")
    path = out_dir / "CONVERSION.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


__all__ = ["ConvertError", "ConvertResult", "convert_manuscript", "format_of", "SUPPORTED", "TARGETS", "TRACKED_CHOICES"]
