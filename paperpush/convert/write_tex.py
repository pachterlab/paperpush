"""Write the document model as LaTeX.

When the model came from a Word file paperpush itself wrote, each block nobody
edited is written back as its original LaTeX, and the preamble and title block
are restored. Everything else is generated.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import bib as biblib
from . import images as imagelib
from .mathconv import UNICODE_TO_CMD
from .model import Appendix, Bibliography, Break, Cite, Code, Comment, Document, Equation, Figure, Heading, Link, ListBlock, Log, Math, Note, Para, Quote, Raw, Ref, Style, Table, Text, cited_keys, fingerprint, plain_text, walk_inlines
from .write_docx import front_fingerprint

_SECTION = {1: "section", 2: "subsection", 3: "subsubsection", 4: "paragraph", 5: "subparagraph", 6: "subparagraph"}
_ESCAPES = {"\\": "\\textbackslash{}", "{": "\\{", "}": "\\}", "%": "\\%", "$": "\\$", "&": "\\&", "#": "\\#", "_": "\\_", "^": "\\textasciicircum{}", "~": "\\textasciitilde{}", "<": "\\textless{}", ">": "\\textgreater{}", "|": "\\textbar{}"}
_TEXT_CHARS = {"\u00a0": "~", "“": "``", "”": "''", "‘": "`", "’": "'", "–": "--", "—": "---", "…": "\\ldots{}", "\u2009": "\\,", "\u2003": "\\quad{}", "\u2002": "\\enspace{}", "\u2011": "-", "\u200b": "", "\ufeff": "", "°": "\\textdegree{}", "µ": "\\textmu{}", "©": "\\textcopyright{}", "®": "\\textregistered{}", "™": "\\texttrademark{}", "§": "\\S{}", "¶": "\\P{}", "†": "\\textdagger{}", "‡": "\\textdaggerdbl{}", "•": "\\textbullet{}", "£": "\\pounds{}", "€": "\\texteuro{}", "×": "\\ensuremath{\\times}", "±": "\\ensuremath{\\pm}", "÷": "\\ensuremath{\\div}", "·": "\\ensuremath{\\cdot}", "−": "\\ensuremath{-}", "\u2004": "\\;", "\u2005": "\\:", "\u2006": "\\,", "\u2007": "~", "\u2008": "\\,", "\u200a": "\\,", "\u202f": "\\,", "\u205f": "\\:", "\u3000": "\\quad{}"}
_MATH_OK = re.compile(r"[\u0370-\u03ff\u2190-\u23ff\u27c0-\u27ef\u2a00-\u2aff\u2100-\u214f]")
_REQUIRED = [("amsmath", r"\\begin\{(?:align|gather|multline|aligned|cases|pmatrix|bmatrix)|\\(?:text|operatorname|binom|eqref)\b"), ("amssymb", r"\\mathbb|\\varnothing"), ("graphicx", r"\\includegraphics"), ("booktabs", r"\\(?:toprule|midrule|bottomrule)"), ("multirow", r"\\multirow"), ("hyperref", r"\\(?:href|url)\b")]


def escape_text(text: str, log: Log | None = None) -> str:
    out: list[str] = []
    for ch in text:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ch in _TEXT_CHARS:
            out.append(_TEXT_CHARS[ch])
        elif ord(ch) > 127 and _MATH_OK.match(ch) and UNICODE_TO_CMD.get(ch):
            out.append(f"\\ensuremath{{\\{UNICODE_TO_CMD[ch]}}}")
        else:
            out.append(ch)
    # Straight quotes and dashes would otherwise become ligatures or the wrong glyph.
    return "".join(out)


def _escape_url(url: str) -> str:
    return re.sub(r"([%#])", r"\\\1", url)  # the only two that break \url's argument


class TexWriter:
    def __init__(self, doc: Document, log: Log, out_dir: Path, *, source_dir: Path) -> None:
        self.doc = doc
        self.log = log
        self.out_dir = out_dir
        self.source_dir = source_dir
        self.manifest = doc.manifest or {}
        self.pool: dict[str, list[str]] = {k: list(v) for k, v in (self.manifest.get("blocks") or {}).items()}
        self.restored = 0
        self.generated = 0
        self.figure_n = 0
        self.natbib = False
        self.comments: list[str] = []
        self.uses: set[str] = set()

    # -- inlines ---------------------------------------------------------------

    def inlines(self, nodes: list) -> str:
        out: list[str] = []
        for n, node in enumerate(nodes):
            if n and isinstance(node, Math) and isinstance(nodes[n - 1], Math):
                out.append("{}")  # $a$$b$ would open display math
            out.append(self.inline(node))
        return "".join(out)

    def inline(self, node) -> str:
        if isinstance(node, Text):
            return escape_text(node.text)
        if isinstance(node, Style):
            inner = self.inlines(node.children)
            return {"strong": f"\\textbf{{{inner}}}", "emph": f"\\emph{{{inner}}}", "underline": f"\\underline{{{inner}}}", "smallcaps": f"\\textsc{{{inner}}}", "sup": f"\\textsuperscript{{{inner}}}", "sub": f"\\textsubscript{{{inner}}}", "code": f"\\texttt{{{inner}}}"}.get(node.kind, inner)
        if isinstance(node, Math):
            return f"${node.latex}$"
        if isinstance(node, Link):
            if plain_text(node.children) == node.url:
                return f"\\url{{{_escape_url(node.url)}}}"
            return f"\\href{{{_escape_url(node.url)}}}{{{self.inlines(node.children)}}}"
        if isinstance(node, Cite):
            return self.cite(node)
        if isinstance(node, Ref):
            return {"eqref": f"\\eqref{{{node.label}}}", "page": f"\\pageref{{{node.label}}}"}.get(node.kind, f"\\ref{{{node.label}}}")
        if isinstance(node, Note):
            return "\\footnote{" + "\n\n".join(self.inlines(b.children) for b in node.blocks if isinstance(b, Para)) + "}"
        if isinstance(node, Break):
            return "\\newline "
        if isinstance(node, Comment):
            self.comments.append(f"% Comment from {node.author}: " + re.sub(r"\s+", " ", node.text))
            return ""
        return ""

    def cite(self, node: Cite) -> str:
        cmd = node.cmd if node.cmd and (self.natbib or not re.match(r"cite[tp]|citealp|citealt|citeauthor|citeyear", node.cmd)) else ("citet" if node.mode == "text" and self.natbib else "citep" if self.natbib else "cite")
        opts = ""
        if node.pre and node.post:
            opts = f"[{node.pre}][{node.post}]"
        elif node.pre:
            opts = f"[{node.pre}][]"
        elif node.post:
            opts = f"[{node.post}]"
        return f"\\{cmd}{opts}{{{','.join(node.keys)}}}"

    # -- blocks ----------------------------------------------------------------

    def restore(self, block) -> str | None:
        """The original LaTeX for ``block``, when it came through Word unedited."""
        if not self.pool:
            return None
        sources = self.pool.get(fingerprint(block))
        if not sources:
            return None
        if isinstance(block, Figure) and not self.figure_files_ok(block, sources[0]):
            return None
        self.restored += 1
        return sources.pop(0) if len(sources) > 1 else sources[0]

    def figure_files_ok(self, block: Figure, source: str) -> bool:
        """A restored figure needs the original files, or it would point at nothing."""
        known = self.manifest.get("images") or {}
        for image in block.images:
            if image.original not in known:
                return False
        names = re.findall(r"\\includegraphics\*?(?:\[[^\]]*\])?\s*\{([^{}]+)\}", source)
        for name in names:
            found = self.find_original(name.strip())
            if found is None:
                return False
            target = (self.out_dir / found[1]).resolve()
            if self.out_dir.resolve() not in target.parents:
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(found[0], target)
        return True

    def find_original(self, name: str) -> tuple[Path, str] | None:
        for ext in ("", ".pdf", ".png", ".jpg", ".jpeg", ".eps"):
            candidate = self.source_dir / (name + ext)
            if candidate.is_file():
                return candidate, name + ext
        return None

    def top_blocks(self) -> list[str]:
        """The body. A block whose source spans several model blocks is restored whole or not at all."""
        groups = self.manifest.get("groups") or {}
        blocks = self.doc.blocks
        out: list[str] = []
        n = 0
        while n < len(blocks):
            block = blocks[n]
            followers = groups.get(fingerprint(block))
            if followers is None:
                out.append(self.block(block))
                n += 1
                continue
            following = [fingerprint(b) for b in blocks[n + 1 : n + 1 + len(followers)]]
            restored = self.restore(block) if following == followers else None
            if restored is not None:
                out.append(restored)
                n += 1 + len(followers)
                continue
            self.generated += 1
            out.append(self.generate(block))
            n += 1
        return out

    def block(self, block) -> str:
        restored = self.restore(block)
        if restored is not None:
            return restored
        if isinstance(block, Raw):
            self.restored += 1
            return block.source or block.tex
        if not isinstance(block, (Appendix, Bibliography)):
            self.generated += 1
        return self.generate(block)

    def generate(self, block) -> str:
        if isinstance(block, Heading):
            name = _SECTION[min(block.level, 6)]
            star = "" if block.numbered else "*"
            label = f"\\label{{{block.label}}}" if block.label else ""
            return f"\\{name}{star}{self.short_form(block.children)}{{{self.inlines(block.children)}}}{label}"
        if isinstance(block, Para):
            self.comments = []
            text = self.inlines(block.children)
            return "\n".join([text] + self.comments) if self.comments else text
        if isinstance(block, ListBlock):
            return self.list_block(block)
        if isinstance(block, Quote):
            return "\\begin{quote}\n" + "\n\n".join(self.block(b) for b in block.blocks) + "\n\\end{quote}"
        if isinstance(block, Code):
            return f"\\begin{{verbatim}}\n{block.text}\n\\end{{verbatim}}"
        if isinstance(block, Equation):
            return self.equation(block)
        if isinstance(block, Figure):
            return self.figure(block)
        if isinstance(block, Table):
            return self.table(block)
        if isinstance(block, Appendix):
            return "\\appendix"
        if isinstance(block, Bibliography):
            return self.bibliography(block)
        return ""

    def short_form(self, nodes: list) -> str:
        """``[plain text]`` for a caption or heading holding what cannot move to a list of figures."""
        if any(isinstance(n, (Note, Link)) for n in walk_inlines(nodes)):
            text = re.sub(r"[\[\]]", "", escape_text(plain_text(nodes)))
            return f"[{text[:120]}]"
        return ""

    def list_block(self, block: ListBlock) -> str:
        env = "enumerate" if block.ordered else "itemize"
        items = []
        for item in block.items:
            items.append("\\item " + "\n\n".join(self.generate(sub) for sub in item))
        return f"\\begin{{{env}}}\n" + "\n".join(items) + f"\n\\end{{{env}}}"

    def equation(self, block: Equation) -> str:
        rows = block.env in ("align", "gather", "multline")
        if not block.numbered:
            if rows:
                return f"\\begin{{{block.env}*}}\n{block.latex}\n\\end{{{block.env}*}}"
            return f"\\[\n{block.latex}\n\\]"
        if rows:
            parts = block.latex.split("\\\\")
            out = []
            for n, part in enumerate(parts):
                part = part.strip()
                label = block.row_labels[n] if n < len(block.row_labels) else ""
                out.append(part + (f" \\label{{{label}}}" if label else ""))
            return f"\\begin{{{block.env}}}\n" + " \\\\\n".join(out) + f"\n\\end{{{block.env}}}"
        label = f"\\label{{{block.label}}}\n" if block.label else ""
        return f"\\begin{{equation}}\n{label}{block.latex}\n\\end{{equation}}"

    def figure(self, block: Figure) -> str:
        lines = ["\\begin{figure}[htbp]", "\\centering"]
        for image in block.images:
            path = self.place_image(image)
            if path is None:
                continue
            width = min(image.width_in / 6.5, 1.0) if image.width_in else 1.0
            lines.append(f"\\includegraphics[width={width:.2f}\\linewidth]{{{path}}}")
        if not block.images:
            self.log.warn("a figure in the Word file has no image: the LaTeX holds a placeholder")
            lines.append("\\fbox{Figure not available}")
        if block.numbered:
            lines.append(f"\\caption{self.short_form(block.caption)}{{{self.inlines(block.caption)}}}")
            if block.label:
                lines.append(f"\\label{{{block.label}}}")
        lines.append("\\end{figure}")
        return "\n".join(lines)

    def place_image(self, image) -> str | None:
        if not image.path:
            self.log.warn(f"figure file missing: {image.original or 'unnamed'}")
            return None
        ready = imagelib.for_latex(Path(image.path), self.out_dir / ".images", self.log)
        if ready is None:
            return None
        folder = self.out_dir / "figures"
        folder.mkdir(parents=True, exist_ok=True)
        self.figure_n += 1
        name = re.sub(r"[^\w.-]", "_", ready.stem) + ready.suffix.lower()
        target = folder / name
        if target.exists() and imagelib.sha(target) != imagelib.sha(ready):
            target = folder / f"{Path(name).stem}-{self.figure_n}{Path(name).suffix}"
        shutil.copy2(ready, target)
        return f"figures/{target.name}"

    def table(self, block: Table) -> str:
        cols = max((sum(c.colspan for c in r) for r in block.rows), default=1)
        widest_row = cols
        active: dict[int, list[int]] = {}
        lines = []
        rows = []
        for r, row in enumerate(block.rows):
            cells = list(row)
            parts = []
            col = 0
            while col < widest_row + 8:
                if col in active and active[col][0] > 0:
                    active[col][0] -= 1
                    span = active[col][1]
                    parts.append(f"\\multicolumn{{{span}}}{{c}}{{}}" if span > 1 else "")
                    col += span
                    continue
                if not cells:
                    break
                cell = cells.pop(0)
                body = self.inlines(cell.children).replace("\\newline ", " ")
                if cell.rowspan > 1:
                    active[col] = [cell.rowspan - 1, cell.colspan]
                    body = f"\\multirow{{{cell.rowspan}}}{{*}}{{{body}}}"
                if cell.colspan > 1:
                    align = {"right": "r", "center": "c", "left": "l"}.get(cell.align, "c")
                    body = f"\\multicolumn{{{cell.colspan}}}{{{align}}}{{{body}}}"
                parts.append(body)
                col += cell.colspan
            widest_row = max(widest_row, col)
            rows.append(" & ".join(parts) + " \\\\")
            if r + 1 == block.header_rows:
                rows.append("\\midrule")
        aligns = block.aligns or [next((c.align for row in block.rows[block.header_rows :] for c in row[n : n + 1] if c.align), "") for n in range(widest_row)]
        spec = "".join({"right": "r", "center": "c", "left": "l"}.get(a, "l") for a in aligns) + "l" * max(0, widest_row - len(aligns))
        body = [f"\\begin{{tabular}}{{{spec}}}", "\\toprule"] + rows + ["\\bottomrule", "\\end{tabular}"]
        if not block.numbered:
            return "\\begin{center}\n" + "\n".join(body) + "\n\\end{center}"
        lines = ["\\begin{table}[htbp]", "\\centering", f"\\caption{self.short_form(block.caption)}{{{self.inlines(block.caption)}}}"]
        if block.label:
            lines.append(f"\\label{{{block.label}}}")
        return "\n".join(lines + body + ["\\end{table}"])

    def bibliography(self, block: Bibliography) -> str:
        restored_source = self.pool.get(fingerprint(block))
        if restored_source:
            self.restored += 1
            return restored_source[0]
        return self.generated_bibliography()

    def generated_bibliography(self) -> str:
        keys = cited_keys(self.doc)
        entries = [self.doc.bib[k] for k in keys if k in self.doc.bib]
        if not entries:
            return ""
        author_year = self.doc.citation_style == "author-year"
        if all(e.structured for e in entries):
            (self.out_dir / "references.bib").write_text(biblib.to_bibtex(entries), encoding="utf-8")
            return f"\\bibliographystyle{{{'plainnat' if author_year else 'unsrtnat'}}}\n\\bibliography{{references}}"
        lines = [f"\\begin{{thebibliography}}{{{len(entries)}}}"]
        for entry in entries:
            label = ""
            if author_year:
                names, year = biblib.author_year(entry)
                label = f"[{escape_text(names)}({escape_text(year)})]"
            lines.append(f"\\bibitem{label}{{{entry.key}}}\n{escape_text(biblib.format_reference(entry))}")
        lines.append("\\end{thebibliography}")
        return "\n\n".join(lines)

    # -- the document ----------------------------------------------------------

    def default_preamble(self, body: str) -> str:
        style = "round,authoryear" if self.doc.citation_style == "author-year" else "numbers,sort&compress"
        lines = ["\\documentclass[11pt]{article}", "\\usepackage[utf8]{inputenc}", "\\usepackage[T1]{fontenc}", "\\usepackage{textcomp}", "\\usepackage{lmodern}", "\\usepackage[margin=1in]{geometry}", "\\usepackage{amsmath,amssymb}", "\\usepackage{graphicx}", "\\usepackage{booktabs}", "\\usepackage{multirow}", "\\usepackage{authblk}", f"\\usepackage[{style}]{{natbib}}", "\\usepackage[hidelinks]{hyperref}", "\\newcommand{\\keywords}[1]{\\par\\medskip\\noindent\\textbf{Keywords:} #1}"]
        return "\n".join(lines) + "\n"

    def front(self) -> str:
        doc = self.doc
        lines = []
        if doc.title:
            lines.append(f"\\title{{{self.inlines(doc.title)}}}")
        for author in doc.authors:
            tags = ",".join(str(a) for a in author.affiliations)
            note = f"\\thanks{{{escape_text(author.email)}}}" if author.email else ""
            lines.append(f"\\author[{tags}]{{{escape_text(author.name)}{note}}}" if tags else f"\\author{{{escape_text(author.name)}{note}}}")
        for n, affiliation in enumerate(doc.affiliations, 1):
            lines.append(f"\\affil[{n}]{{{escape_text(affiliation)}}}")
        lines.append("\\date{}")
        return "\n".join(lines)

    def front_body(self) -> str:
        doc = self.doc
        parts = ["\\maketitle"] if doc.title or doc.authors else []
        if doc.abstract:
            parts.append("\\begin{abstract}\n" + "\n\n".join(self.generate(b) for b in doc.abstract) + "\n\\end{abstract}")
        if doc.keywords:
            parts.append("\\keywords{" + escape_text(", ".join(doc.keywords)) + "}")
        return "\n\n".join(parts)

    def write(self, path: Path) -> None:
        manifest = self.manifest
        has_preamble = bool(manifest.get("preamble"))
        self.natbib = bool(re.search(r"\{natbib\}", manifest.get("preamble", ""))) if has_preamble else True
        placed = any(isinstance(b, Bibliography) for b in self.doc.blocks)
        body_parts = self.top_blocks()
        if cited_keys(self.doc) and not placed:
            body_parts.append(self.generated_bibliography())
        body = self.settle_free_labels("\n\n".join(p for p in body_parts if p))
        front_same = has_preamble and manifest.get("front_fp") == front_fingerprint(self.doc)
        if has_preamble:
            self.copy_support_files(manifest["preamble"] + "\n" + body)
            for name, contents in (manifest.get("bib_source") or {}).items():
                safe = Path(name).name
                if safe.endswith(".bib"):
                    (self.out_dir / safe).write_text(contents, encoding="utf-8")
            preamble = self.add_missing_packages(manifest["preamble"], body)
            if front_same:
                front_text = manifest.get("front", "")
                head = preamble
            else:
                self.log.warn("the title block changed in Word, so it was regenerated: check that the document class still accepts it")
                head = _strip_front(preamble) + "\n" + self.front() + "\n"
                front_text = self.front_body()
        else:
            head = self.default_preamble(body) + self.front() + "\n"
            front_text = self.front_body()
        text = head.rstrip() + "\n\n\\begin{document}\n\n" + (front_text + "\n\n" if front_text else "") + body + "\n\n\\end{document}\n"
        path.write_text(text, encoding="utf-8")
        exotic = sorted({ch for ch in text if ord(ch) > 0x17F})
        if exotic and not has_preamble:
            shown = " ".join(f"U+{ord(ch):04X}" for ch in exotic[:10])
            self.log.warn(f"pdfLaTeX may reject these characters: {shown}. Compile with XeLaTeX or LuaLaTeX, or replace them")
        leftovers = self.out_dir / ".images"
        if leftovers.exists():
            shutil.rmtree(leftovers, ignore_errors=True)

    def settle_free_labels(self, body: str) -> str:
        """A reference to a theorem or algorithm whose environment is gone keeps its number, as text."""
        defined = set(re.findall(r"\\label\s*\{([^{}]+)\}", body))
        settled = 0
        for label, shown in self.doc.extra_labels.items():
            if label in defined:
                continue
            for command in ("ref", "eqref"):
                needle = f"\\{command}{{{label}}}"
                settled += body.count(needle)
                body = body.replace(needle, f"({shown})" if command == "eqref" else shown)
        if settled:
            self.log.change(f"{settled} reference(s) to theorems or algorithms became plain numbers, because Word has no such environments")
        return body

    def copy_support_files(self, text: str) -> None:
        """The venue's .cls, .sty, and .bst, which the restored preamble needs beside it."""
        names: set[tuple[str, str]] = set()
        for command, ext in (("usepackage", ".sty"), ("documentclass", ".cls"), ("bibliographystyle", ".bst")):
            for group in re.findall(rf"\\{command}(?:\[[^\]]*\])?\{{([^{{}}]+)\}}", text):
                names |= {(n.strip(), ext) for n in group.split(",")}
        for name, ext in names:
            source = self.source_dir / (name + ext)
            if "/" not in name and source.is_file():
                shutil.copy2(source, self.out_dir / source.name)

    def add_missing_packages(self, preamble: str, body: str) -> str:
        wanted = []
        for package, pattern in _REQUIRED:
            if re.search(pattern, body) and not re.search(r"\\(?:usepackage|RequirePackage)(?:\[[^\]]*\])?\{[^{}]*\b" + package + r"\b", preamble):
                wanted.append(f"\\usepackage{{{package}}}")
        return preamble.rstrip() + ("\n" + "\n".join(wanted) if wanted else "") + "\n"


def _strip_front(preamble: str) -> str:
    """The preamble without commands the regenerated title block replaces."""
    from .read_tex import read_braced, read_optional

    out = preamble
    for name in ("title", "author", "affil", "date"):
        while True:
            m = re.search(rf"\\{name}\b", out)
            if not m:
                break
            _, i = read_optional(out, m.end())
            got = read_braced(out, i)
            if not got:
                break
            out = out[: m.start()] + out[got[1] :]
    if "authblk" not in out:
        out = out.rstrip() + "\n\\usepackage{authblk}\n"
    if "\\keywords" not in out:
        out = out.rstrip() + "\n\\providecommand{\\keywords}[1]{\\par\\medskip\\noindent\\textbf{Keywords:} #1}\n"
    return out
