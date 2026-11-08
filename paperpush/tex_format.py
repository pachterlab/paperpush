"""Reformat a LaTeX manuscript: a venue's class or style files, or its layout rules.

The manuscript's sources are copied into a new directory and only the copy of
the main ``.tex`` is edited. With a template, the document class becomes the
template's (with the options its sample paper uses), the template's own style
packages are loaded, its bibliography style replaces the manuscript's, and the
class/style/bibliography files are copied in beside the manuscript. Packages
that fight a venue's class over the page (``geometry``, ``fullpage``, ...) are
commented out, never deleted. Without a template, the preamble gains the page
size and margins (``geometry``), double spacing (``setspace``), line numbers
(``lineno``), body font, and font size the guidelines ask for.

Front matter is not restructured: a class that wants ``\\begin{frontmatter}``
or ``\\affiliation`` is reported, with the template's sample to copy from.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .templates import Template

_DOCUMENTCLASS = re.compile(r"\\documentclass\s*(\[[^\]]*\])?\s*\{([^}]*)\}")
_USEPACKAGE = re.compile(r"^[ \t]*\\usepackage\s*(\[[^\]]*\])?\s*\{([^}]*)\}[^\n]*$", re.MULTILINE)
_BIBSTYLE = re.compile(r"\\bibliographystyle\s*\{([^}]*)\}")
# Packages that set the page or line layout a venue's class owns.
_LAYOUT_PACKAGES = {"geometry", "fullpage", "a4wide", "vmargin", "anysize", "savetrees", "typearea"}
_RULE_PACKAGES = {"geometry", "fullpage", "a4wide", "vmargin", "anysize", "savetrees", "setspace", "doublespace", "lineno", "mathptmx", "times", "newtxtext", "helvet", "anyfontsize"}
_STANDARD_CLASSES = {"article", "report", "book", "amsart", "amsbook", "scrartcl", "scrreprt", "memoir", "extarticle"}
# Front-matter forms a venue class may want: (pattern, how to name it).
_FRONTMATTER_FORMS = [
    (re.compile(r"\\begin\{frontmatter\}"), "\\begin{frontmatter}"),
    (re.compile(r"\\affiliation\b"), "\\affiliation"),
    (re.compile(r"\\affil\b"), "\\affil"),
    (re.compile(r"\\address\b"), "\\address"),
    (re.compile(r"\\corres\b|\\cortext\b"), "\\cortext (corresponding author)"),
    (re.compile(r"\\icmlauthor\b"), "\\icmlauthor"),
    (re.compile(r"\\author\[[^\]]*\]\{"), "\\author[label]{...}"),
]


def copy_sources(manuscript: Path, dest: Path) -> Path:
    """Copy the manuscript's sources into ``dest``; returns the copy of the main ``.tex``."""
    from .manuscript import _main_tex
    from .templates import _safe_extract

    dest.mkdir(parents=True, exist_ok=True)
    if manuscript.suffix.lower() == ".zip":
        _safe_extract(manuscript, dest)
        main = _main_tex(sorted(dest.rglob("*.tex")))
        if main is None:
            raise ValueError(f"{manuscript.name} holds no .tex file with \\documentclass")
        return main
    src = manuscript.parent
    for item in src.iterdir():
        if item.name.startswith(".") or item.resolve() == dest.resolve() or dest.resolve() in item.resolve().parents or item.resolve() in dest.resolve().parents:
            continue
        if item.is_dir():
            if not any(item.rglob("*.tex")) and not any(p.suffix.lower() in (".png", ".pdf", ".jpg", ".jpeg", ".eps", ".bib", ".sty", ".cls", ".bst", ".tikz", ".pgf") for p in item.rglob("*")):
                continue
            shutil.copytree(item, dest / item.name, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".*", "*.aux", "*.log", "*.synctex.gz", "*.fdb_latexmk", "*.fls"))
        elif item.suffix.lower() not in (".aux", ".log", ".out", ".fls", ".fdb_latexmk", ".synctex", ".gz") or item.name.endswith(".tar.gz"):
            if item.stat().st_size <= 200 * 1024 * 1024:
                shutil.copy2(item, dest / item.name)
    return dest / manuscript.name


def _comment_out(source: str, packages: set[str], why: str) -> tuple[str, list[str]]:
    """Comment out ``\\usepackage`` lines that load any of ``packages`` (and ``\\geometry{}`` calls)."""
    removed: list[str] = []

    def fix(m: re.Match) -> str:
        names = {n.strip() for n in m.group(2).split(",")}
        hit = names & packages
        if not hit:
            return m.group(0)
        removed.extend(sorted(hit))
        line = "% " + m.group(0).lstrip() + f"  % commented out by paperpush format: {why}"
        rest = [n.strip() for n in m.group(2).split(",") if n.strip() and n.strip() not in packages]
        # Keep the other packages a shared \usepackage{a,b} line loads.
        return ("\\usepackage{" + ",".join(rest) + "}\n" + line) if rest else line

    source = _USEPACKAGE.sub(fix, source)
    if "geometry" in packages:
        source = re.sub(r"^([ \t]*)(\\geometry\s*\{[^}]*\})", lambda m: m.group(1) + "% " + m.group(2) + f"  % commented out by paperpush format: {why}", source, flags=re.MULTILINE)
    return source, removed


def _preamble_insert(source: str, lines: list[str]) -> str:
    """Insert ``lines`` right after the ``\\documentclass`` line."""
    m = _DOCUMENTCLASS.search(source)
    if not m:
        return source
    end = source.find("\n", m.end())
    end = len(source) if end == -1 else end
    return source[:end] + "\n" + "\n".join(lines) + source[end:]


def apply_template(main: Path, template: Template) -> tuple[list[str], list[str]]:
    """Switch the manuscript at ``main`` to the template. Returns ``(changes, things left to the author)``."""
    source = main.read_text("utf-8", "replace")
    done: list[str] = []
    todo: list[str] = []
    sample = template.main_tex.read_text("utf-8", "replace") if template.main_tex else ""
    # 1. Copy the class/style/bibliography files in.
    copied = []
    for f in template.files:
        target = main.parent / f.name
        if target.exists() and target.read_bytes() == f.read_bytes():
            continue
        shutil.copy2(f, target)
        copied.append(f.name)
    if copied:
        done.append("copied the template's files: " + ", ".join(sorted(copied)))
    local_styles = {f.stem for f in template.files if f.suffix.lower() == ".sty"}
    local_classes = {f.stem for f in template.files if f.suffix.lower() == ".cls"}
    local_bst = {f.stem for f in template.files if f.suffix.lower() == ".bst"}
    # 2. The document class.
    t_class = _DOCUMENTCLASS.search(sample) if sample else None
    if t_class:
        new_class = t_class.group(0)
    elif template.class_name or local_classes:
        new_class = "\\documentclass{" + (template.class_name or sorted(local_classes)[0]) + "}"
    else:
        new_class = None
    m_class = _DOCUMENTCLASS.search(source)
    if not m_class:
        raise ValueError(f"{main.name} has no \\documentclass")
    if new_class and m_class.group(0) != new_class:
        source = source[: m_class.start()] + new_class + source[m_class.end() :]
        done.append(f"document class changed from \\documentclass{m_class.group(1) or ''}{{{m_class.group(2)}}} to {new_class}")
    # 3. The template's own style packages, as its sample loads them.
    wanted = []
    for m in _USEPACKAGE.finditer(sample):
        names = {n.strip() for n in m.group(2).split(",")}
        if names & local_styles:
            wanted.append(m.group(0).strip().split("%")[0].strip())
    if not sample and local_styles and not local_classes:
        wanted = ["\\usepackage{" + s + "}" for s in sorted(local_styles)]
    present = {n.strip() for m in _USEPACKAGE.finditer(source) for n in m.group(2).split(",")}
    add = [line for line in wanted if not ({n.strip() for n in _USEPACKAGE.match(line).group(2).split(",")} <= present)]
    if add:
        source = _preamble_insert(source, ["% Added by paperpush format: the venue's style package(s)"] + add)
        done.append("loaded the template's style package(s): " + "; ".join(add))
    # 4. Packages that would override the class's page layout.
    source, removed = _comment_out(source, _LAYOUT_PACKAGES, "the venue's template sets the page layout")
    if removed:
        done.append("commented out the page-layout package(s) the template replaces: " + ", ".join(sorted(set(removed))))
    # 5. Bibliography style.
    t_bst = _BIBSTYLE.search(sample) if sample else None
    style = t_bst.group(1) if t_bst else (sorted(local_bst)[0] if len(local_bst) == 1 else None)
    m_bst = _BIBSTYLE.search(source)
    if style and m_bst and m_bst.group(1) != style:
        source = source[: m_bst.start()] + "\\bibliographystyle{" + style + "}" + source[m_bst.end() :]
        done.append(f"bibliography style changed from {m_bst.group(1)} to {style}")
    elif style and not m_bst and re.search(r"\\usepackage(?:\[[^\]]*\])?\{biblatex\}", source):
        todo.append(f"the manuscript uses biblatex; the template expects BibTeX with \\bibliographystyle{{{style}}}")
    # 6. Authors: a class of its own that never defines \and (elsarticle takes one \author per author)
    #    cannot read "A \and B"; a class or style that defines \and or \And can.
    classes = [f for f in template.files if f.suffix.lower() == ".cls"]
    defines_and = any(re.search(r"\\(?:and|And|AND)\b", f.read_text("utf-8", "replace")) for f in template.files if f.suffix.lower() in (".cls", ".sty"))
    if classes and not defines_and and not re.search(r"\\(?:and|And|AND)\b", sample):
        source, n = _split_authors(source)
        if n:
            done.append(f"split \\author{{... \\and ...}} into {n} \\author commands, one per author, as the class expects")
    # 7. Front matter the class may want differently.
    if sample:
        missing = [label for pattern, label in _FRONTMATTER_FORMS if pattern.search(sample) and not pattern.search(source)]
        if missing:
            todo.append(f"the template's front matter uses {', '.join(missing)}; move the title, authors, and affiliations into that form (see {template.main_tex.name} in the template)")
    main.write_text(source, encoding="utf-8")
    return done, todo


def _split_authors(source: str) -> tuple[str, int]:
    """``\\author{A \\and B}`` as ``\\author{A}\n\\author{B}``; returns the new source and how many authors."""
    m = re.search(r"\\author\s*\{", source)
    if not m:
        return source, 0
    depth, i = 1, m.end()
    while i < len(source) and depth:
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        i += 1
    body = source[m.end() : i - 1]
    parts = [p.strip() for p in re.split(r"\\and\b", body) if p.strip()]
    if len(parts) < 2:
        return source, 0
    return source[: m.start()] + "\n".join("\\author{" + p + "}" for p in parts) + source[i:], len(parts)


def apply_rules(main: Path, reqs) -> tuple[list[str], list[str]]:
    """Apply the venue's layout rules to the preamble of ``main``. Returns ``(changes, rules not applied)``."""
    m = reqs.manuscript
    source = main.read_text("utf-8", "replace")
    done: list[str] = []
    skipped: list[str] = []
    cls = _DOCUMENTCLASS.search(source)
    if not cls:
        raise ValueError(f"{main.name} has no \\documentclass")
    wanted: set[str] = set()
    lines: list[str] = []
    if m.page_size or m.margins_mm:
        opts = []
        size = (m.page_size or "").strip().lower()
        if size in ("a4",):
            opts.append("a4paper")
        elif size in ("letter", "us letter", "us-letter"):
            opts.append("letterpaper")
        elif size:
            skipped.append(f"page size '{m.page_size}' is not one paperpush knows; set it by hand")
        if m.margins_mm:
            opts.append(f"margin={m.margins_mm:g}mm")
        if opts:
            lines.append(f"\\usepackage[{','.join(opts)}]{{geometry}}")
            wanted.add("geometry")
            done.append("page " + ", ".join(o.replace("paper", " paper").replace("margin=", "margins ") for o in opts))
    if m.double_spacing:
        lines.append("\\usepackage{setspace}\n\\doublespacing")
        wanted |= {"setspace", "doublespace"}
        done.append("double spacing (setspace)")
    if m.line_numbers:
        lines.append("\\usepackage{lineno}\n\\linenumbers")
        wanted.add("lineno")
        done.append("continuous line numbers (lineno)")
    if m.font:
        from .docx_format import body_font

        font = body_font(m.font) or ""
        if font in ("Times New Roman", "Times"):
            lines.append("\\usepackage{mathptmx}")
            wanted |= {"mathptmx", "times", "newtxtext"}
            done.append("Times body font (mathptmx)")
        elif font in ("Arial", "Helvetica"):
            lines.append("\\usepackage[scaled]{helvet}\n\\renewcommand{\\familydefault}{\\sfdefault}")
            wanted.add("helvet")
            done.append("Helvetica (Arial-like) sans-serif body font")
        else:
            skipped.append(f"body font '{m.font}': LaTeX has no standard package for it; set it with fontspec (XeLaTeX/LuaLaTeX)")
    if m.font_size_pt:
        size = f"{m.font_size_pt:g}pt"
        if cls.group(2).strip() in _STANDARD_CLASSES and size in ("10pt", "11pt", "12pt"):
            opts = [o.strip() for o in (cls.group(1) or "[]")[1:-1].split(",") if o.strip() and not re.fullmatch(r"\d+pt", o.strip())]
            new = "\\documentclass[" + ",".join([size] + opts) + "]{" + cls.group(2) + "}"
            if new != cls.group(0):
                source = source[: cls.start()] + new + source[cls.end() :]
                done.append(f"body text size {size} (class option)")
        else:
            lines.append(f"\\AtBeginDocument{{\\fontsize{{{m.font_size_pt:g}}}{{{m.font_size_pt * 1.2:.1f}}}\\selectfont}}")
            done.append(f"body text size {size} (\\fontsize)")
    if m.page_numbers and re.search(r"\\pagestyle\{empty\}|\\thispagestyle\{empty\}", source):
        source = re.sub(r"\\pagestyle\{empty\}", r"\\pagestyle{plain}", source)
        done.append("page numbers turned back on (\\pagestyle{plain})")
    if wanted:
        source, removed = _comment_out(source, wanted, "replaced by the venue's layout settings below the \\documentclass line")
        if removed:
            done.append("commented out the manuscript's own " + ", ".join(sorted(set(removed))) + " setup")
    if lines:
        source = _preamble_insert(source, ["% Added by paperpush format: the venue's layout rules"] + lines)
    main.write_text(source, encoding="utf-8")
    return done, skipped
