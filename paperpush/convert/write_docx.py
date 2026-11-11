"""Write the document model as a Word (.docx) package.

Everything is native Word: equations are Office Math, figure and table numbers
are SEQ fields, cross-references are REF fields to bookmarks, section numbers
come from a multilevel list. Citations are ADDIN fields, the mechanism Zotero
uses, so the citation keys stay with the text while authors edit around them.

A custom XML part carries a manifest of the LaTeX each block came from, which
:mod:`paperpush.convert.read_docx` uses to restore whatever nobody edited.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from . import bib as biblib
from . import images as imagelib
from .mathconv import latex_to_omml
from .model import Appendix, Bibliography, Break, Cite, Code, Comment, Document, Equation, Figure, Heading, Image, Link, ListBlock, Log, Math, Note, Para, Quote, Raw, Ref, Style, Table, Text, all_blocks, cited_keys, fingerprint, number_labels, plain_text

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MANIFEST_NS = "urn:paperpush:convert:1"
DOC_NS = f'xmlns:w="{W_NS}" xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" ' 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" ' 'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" ' 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" ' 'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
EMU = 914400
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
SEP_ID, CONT_ID = -1, 0


def _x(text: str) -> str:
    return escape(_INVALID_XML.sub("", text))


def bookmark_name(label: str) -> str:
    """A Word bookmark name for a label: hidden, at most 40 characters."""
    name = "_pp_" + re.sub(r"\W", "_", label, flags=re.A)
    if len(name) > 40:
        name = name[:31] + "_" + hashlib.sha256(label.encode()).hexdigest()[:8]
    return name


class DocxWriter:
    def __init__(self, doc: Document, log: Log, work: Path, root: Path) -> None:
        self.doc = doc
        self.log = log
        self.work = work
        self.root = root
        self.numbers = number_labels(doc)
        self.cited = cited_keys(doc)
        self.cite_numbers = self.order_citations()
        self.rels: list[tuple[str, str, str, str]] = []
        self.note_rels: list[tuple[str, str, str, str]] = []  # a footnote's links resolve against its own part
        self.rel_sink = self.rels
        self.next_rel = 100
        self.footnotes: list[str] = []
        self.media: list[tuple[str, bytes]] = []
        self.next_bookmark = 1
        self.next_shape = 1
        self.lists: list[list[bool]] = []
        self.labels: dict[str, str] = {}
        self.fig_n = self.tab_n = self.eq_n = 0
        self.tikz_n = 0
        self.raw: dict[str, str] = {}
        self.image_map: dict[str, str] = {}
        self.appendix = False

    def order_citations(self) -> dict[str, int]:
        keys = list(self.cited)
        if self.doc.citation_style == "author-year":
            keys.sort(key=lambda k: biblib.author_year(self.doc.bib[k]) if k in self.doc.bib else (k, ""))
        return {k: n for n, k in enumerate(keys, 1)}

    def rel(self, kind: str, target: str, external: bool = False) -> str:
        rid = f"rId{self.next_rel}"
        self.next_rel += 1
        self.rel_sink.append((rid, kind, target, "External" if external else ""))
        return rid

    # -- inlines ---------------------------------------------------------------

    def run(self, text: str, styles: tuple = ()) -> str:
        if not text:
            return ""
        return f'<w:r>{self.rpr(styles)}<w:t xml:space="preserve">{_x(text)}</w:t></w:r>'

    def rpr(self, styles: tuple) -> str:
        kinds = set(styles)
        parts = []
        if "link" in kinds:
            parts.append('<w:rStyle w:val="Hyperlink"/>')
        elif "code" in kinds:
            parts.append('<w:rStyle w:val="VerbatimChar"/>')
        if "strong" in kinds:
            parts.append("<w:b/>")
        if "emph" in kinds:
            parts.append("<w:i/>")
        if "smallcaps" in kinds:
            parts.append("<w:smallCaps/>")
        if "underline" in kinds:
            parts.append('<w:u w:val="single"/>')
        if "sup" in kinds:
            parts.append('<w:vertAlign w:val="superscript"/>')
        elif "sub" in kinds:
            parts.append('<w:vertAlign w:val="subscript"/>')
        return f"<w:rPr>{''.join(parts)}</w:rPr>" if parts else ""

    def field(self, instr: str, result_xml: str, styles: tuple = ()) -> str:
        rpr = self.rpr(styles)
        return f'<w:r>{rpr}<w:fldChar w:fldCharType="begin"/></w:r><w:r>{rpr}<w:instrText xml:space="preserve">{_x(instr)}</w:instrText></w:r><w:r>{rpr}<w:fldChar w:fldCharType="separate"/></w:r>{result_xml}<w:r>{rpr}<w:fldChar w:fldCharType="end"/></w:r>'

    def inlines(self, nodes: list, styles: tuple = ()) -> str:
        return "".join(self.inline(n, styles) for n in nodes)

    def inline(self, node, styles: tuple) -> str:
        if isinstance(node, Text):
            return self.run(node.text, styles)
        if isinstance(node, Style):
            return self.inlines(node.children, styles + (node.kind,))
        if isinstance(node, Math):
            omml, _ = latex_to_omml(node.latex)
            return f"<m:oMath>{omml}</m:oMath>"
        if isinstance(node, Link):
            rid = self.rel(f"{REL}/hyperlink", node.url, external=True)
            return f'<w:hyperlink r:id="{rid}">{self.inlines(node.children, styles + ("link",))}</w:hyperlink>'
        if isinstance(node, Cite):
            return self.cite(node, styles)
        if isinstance(node, Ref):
            return self.ref(node, styles)
        if isinstance(node, Note):
            return self.note(node)
        if isinstance(node, Break):
            return "<w:r><w:br/></w:r>"
        if isinstance(node, Comment):
            return ""
        return ""

    def cite_text(self, node: Cite) -> str:
        known = [k for k in node.keys if k in self.doc.bib]
        for key in node.keys:
            if key not in self.doc.bib:
                self.log.warn(f"citation '{key}' has no entry in the bibliography: shown as [?]")
        pre = node.pre + " " if node.pre else ""
        post = ", " + node.post if node.post else ""
        cmd = node.cmd.lower().rstrip("*")
        if not known:
            return "[?]"
        if cmd == "citeauthor":
            return "; ".join(biblib.author_year(self.doc.bib[k])[0] for k in known)
        if cmd == "citeyear" or cmd == "citeyearpar":
            return ", ".join(biblib.author_year(self.doc.bib[k])[1] for k in known)
        if self.doc.citation_style == "author-year":
            pairs = [biblib.author_year(self.doc.bib[k]) for k in known]
            if node.mode == "text":
                return "; ".join(f"{a} ({pre}{y}{post})" for a, y in pairs)
            return "(" + pre + "; ".join(f"{a}, {y}" for a, y in pairs) + post + ")"
        numbers = sorted(self.cite_numbers[k] for k in known)
        body = "[" + pre + _compress(numbers) + post + "]"
        if node.mode == "text":
            return f"{biblib.author_year(self.doc.bib[known[0]])[0]} {body}"
        return body

    def cite(self, node: Cite, styles: tuple) -> str:
        payload = {"keys": node.keys, "mode": node.mode}
        if node.cmd:
            payload["cmd"] = node.cmd
        if node.pre:
            payload["pre"] = node.pre
        if node.post:
            payload["post"] = node.post
        instr = f" ADDIN PAPERPUSH_CITE {json.dumps(payload, ensure_ascii=False)} "
        return self.field(instr, self.run(self.cite_text(node), styles), styles)

    def ref(self, node: Ref, styles: tuple) -> str:
        number = self.numbers.get(node.label)
        if number is None:  # reported once, by _check_links; the field keeps the label for the way back
            payload = json.dumps({"label": node.label, "kind": node.kind}, ensure_ascii=False)
            return self.field(f" ADDIN PAPERPUSH_REF {payload} ", self.run("??", styles), styles)
        if node.label in self.doc.extra_labels:  # no bookmark to point at: a static field keeps the label
            payload = json.dumps({"label": node.label, "kind": node.kind}, ensure_ascii=False)
            return self.field(f" ADDIN PAPERPUSH_REF {payload} ", self.run(number, styles), styles)
        name = bookmark_name(node.label)
        self.labels[name] = node.label
        is_section = self.is_section(node.label)
        number_switch = "\\r " if is_section and node.kind != "page" else ""
        instr = f" {'PAGEREF' if node.kind == 'page' else 'REF'} {name} {number_switch}\\h "
        field = self.field(instr, self.run("1" if node.kind == "page" else number, styles), styles)
        return self.run("(", styles) + field + self.run(")", styles) if node.kind == "eqref" else field

    def is_section(self, label: str) -> bool:
        return any(isinstance(b, Heading) and b.label == label for b in self.doc.blocks)

    def note(self, node: Note) -> str:
        fid = len(self.footnotes) + 1
        body = []
        self.rel_sink = self.note_rels
        for n, block in enumerate(node.blocks):
            children = block.children if isinstance(block, Para) else [Text(plain_text(getattr(block, "children", [])))]
            lead = '<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteRef/></w:r><w:r><w:t xml:space="preserve"> </w:t></w:r>' if n == 0 else ""
            body.append(f'<w:p><w:pPr><w:pStyle w:val="FootnoteText"/></w:pPr>{lead}{self.inlines(children)}</w:p>')
        self.rel_sink = self.rels
        if not body:
            body.append('<w:p><w:pPr><w:pStyle w:val="FootnoteText"/></w:pPr></w:p>')
        self.footnotes.append(f'<w:footnote w:id="{fid}">{"".join(body)}</w:footnote>')
        return f'<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteReference w:id="{fid}"/></w:r>'

    def bookmarked(self, label: str, inner: str) -> str:
        if not label:
            return inner
        name = bookmark_name(label)
        self.labels[name] = label
        bid = self.next_bookmark
        self.next_bookmark += 1
        return f'<w:bookmarkStart w:id="{bid}" w:name="{name}"/>{inner}<w:bookmarkEnd w:id="{bid}"/>'

    # -- blocks ----------------------------------------------------------------

    def para(self, content: str, style: str = "", extra: str = "") -> str:
        ppr = (f'<w:pStyle w:val="{style}"/>' if style else "") + extra
        return f"<w:p>{'<w:pPr>' + ppr + '</w:pPr>' if ppr else ''}{content}</w:p>"

    def front(self) -> list[str]:
        doc = self.doc
        out = []
        if doc.title:
            out.append(self.para(self.inlines(doc.title), "Title"))
        if doc.authors:
            parts = []
            for n, author in enumerate(doc.authors):
                if n:
                    parts.append(self.run(", "))
                parts.append(self.run(author.name))
                marks = ",".join(str(a) for a in author.affiliations) + ("*" if author.corresponding else "")
                if marks:
                    parts.append(self.run(marks, ("sup",)))
            out.append(self.para("".join(parts), "Author"))
        for n, affiliation in enumerate(doc.affiliations, 1):
            out.append(self.para(self.run(str(n), ("sup",)) + self.run(" " + affiliation), "Affiliation"))
        emails = [f"{a.name}: {a.email}" if len(doc.authors) > 1 else a.email for a in doc.authors if a.email]
        if emails:
            out.append(self.para(self.run("Correspondence: ", ("strong",)) + self.run("; ".join(emails)), "Affiliation"))
        if doc.abstract:
            out.append(self.para(self.run("Abstract"), "AbstractHeading"))
            for block in doc.abstract:
                if isinstance(block, Para):
                    out.append(self.para(self.inlines(block.children), "Abstract"))
                else:
                    out += self.block(block)
        if doc.keywords:
            out.append(self.para(self.run("Keywords: ", ("strong",)) + self.run(", ".join(doc.keywords)), "Keywords"))
        return out

    def block(self, block) -> list[str]:
        if isinstance(block, Heading):
            return [self.heading(block)]
        if isinstance(block, Para):
            return [self.para(self.inlines(block.children))]
        if isinstance(block, ListBlock):
            return self.list_block(block)
        if isinstance(block, Quote):
            return [self.para(self.inlines(b.children), "Quote") for b in all_blocks(block.blocks) if isinstance(b, Para)]
        if isinstance(block, Code):
            lines = block.text.split("\n")
            runs = "<w:r><w:br/></w:r>".join(f'<w:r><w:t xml:space="preserve">{_x(line)}</w:t></w:r>' for line in lines)
            return [self.para(runs, "SourceCode")]
        if isinstance(block, Equation):
            return self.equation(block)
        if isinstance(block, Figure):
            return self.figure(block)
        if isinstance(block, Table):
            return self.table(block)
        if isinstance(block, Raw):
            token = f"LaTeX block {len(self.raw) + 1} ({block.note})"
            self.raw[token] = block.source or block.tex
            mark = self.field(f" ADDIN PAPERPUSH_RAW {json.dumps({'token': token}, ensure_ascii=False)} ", "")
            self.tikz_n += 1
            rendered = imagelib.render_tikz(block.source or block.tex, self.doc.preamble, self.root, self.work, self.tikz_n, self.log, quiet=True)
            if rendered is not None:
                return [self.picture(Image(path=str(rendered), original=""), style="LatexBlock", lead=mark)]
            self.log.warn(f"{block.note}: could not be rendered for Word, so a placeholder stands in for it")
            return [self.para(mark + self.run(f"[{token}: not representable in Word, restored when converted back to LaTeX]"), "LatexBlock")]
        if isinstance(block, Appendix):
            self.appendix = True
            return []
        if isinstance(block, Bibliography):
            return self.bibliography()
        return []

    def heading(self, block: Heading) -> str:
        level = min(block.level, 6)
        num_id = 0
        if block.numbered:
            num_id = 2 if self.appendix else 1
        extra = f'<w:numPr><w:ilvl w:val="{level - 1}"/><w:numId w:val="{num_id}"/></w:numPr>'
        return self.para(self.bookmarked(block.label, self.inlines(block.children)), f"Heading{level}", extra)

    def list_block(self, block: ListBlock) -> list[str]:
        kinds: list[bool] = []

        def visit(lst: ListBlock, depth: int) -> None:
            if depth >= len(kinds):
                kinds.append(lst.ordered)
            for item in lst.items:
                for sub in item:
                    if isinstance(sub, ListBlock):
                        visit(sub, depth + 1)

        visit(block, 0)
        self.lists.append(kinds)
        num_id = 2 + len(self.lists)
        return self.list_items(block, num_id, 0)

    def list_items(self, block: ListBlock, num_id: int, depth: int) -> list[str]:
        out: list[str] = []
        for item in block.items:
            first = True
            for sub in item:
                if isinstance(sub, ListBlock):
                    out += self.list_items(sub, num_id, depth + 1)
                elif isinstance(sub, Para):
                    if first:
                        out.append(self.para(self.inlines(sub.children), "ListParagraph", f'<w:numPr><w:ilvl w:val="{depth}"/><w:numId w:val="{num_id}"/></w:numPr>'))
                    else:
                        out.append(self.para(self.inlines(sub.children), "ListContinue", f'<w:ind w:left="{720 * (depth + 1)}"/>'))
                    first = False
                else:
                    out += self.block(sub)
        return out

    def equation(self, block: Equation) -> list[str]:
        rows = block.env in ("align", "gather", "multline")
        omml, _ = latex_to_omml(block.latex, rows=rows)
        para_math = f'<m:oMathPara><m:oMathParaPr><m:jc m:val="center"/></m:oMathParaPr><m:oMath>{omml}</m:oMath></m:oMathPara>'
        if not block.numbered:
            return [self.para(para_math, "DisplayEquation")]
        numbers = []
        for label in block.row_labels:
            self.eq_n += 1
            field = self.field(" SEQ Equation \\* ARABIC ", self.run(str(self.eq_n)))
            numbers.append(self.para(self.run("(") + self.bookmarked(label, field) + self.run(")"), "EquationNumber"))
        widths = (720, 7920, 720)
        grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)

        def cell(width: int, content: str) -> str:
            return f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/><w:vAlign w:val="center"/></w:tcPr>{content}</w:tc>'

        table = f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/><w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid><w:tr>{cell(widths[0], "<w:p/>")}{cell(widths[1], self.para(para_math, "DisplayEquation"))}{cell(widths[2], "".join(numbers))}</w:tr></w:tbl>'
        return [table, '<w:p><w:pPr><w:pStyle w:val="EquationSpacer"/></w:pPr></w:p>']

    def picture(self, image: Image, style: str = "FigureBody", lead: str = "") -> str:
        path = Path(image.path) if image.path else None
        if image.tikz and path is None:
            self.tikz_n += 1
            path = imagelib.render_tikz(image.tikz, self.doc.preamble, self.root, self.work, self.tikz_n, self.log)
        ready = imagelib.to_raster(path, self.work, self.log) if path is not None else None
        if ready is None:
            return self.para(self.run(f"[Figure not available: {image.original or 'TikZ picture'}]"), "FigureBody")
        width, height = imagelib.size_inches(ready, image.width_in)
        data = ready.read_bytes()
        digest = hashlib.sha256(data).hexdigest()[:20]
        self.image_map[digest] = image.original
        suffix = ".jpeg" if ready.suffix.lower() in (".jpg", ".jpeg") else ".png"
        name = f"image{len(self.media) + 1}{suffix}"
        self.media.append((name, data))
        rid = self.rel(f"{REL}/image", f"media/{name}")
        shape = self.next_shape
        self.next_shape += 1
        cx, cy = int(width * EMU), int(height * EMU)
        drawing = f'<w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{shape}" name="Figure {shape}"/><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="{shape}" name={quoteattr(name)}/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing>'
        return self.para(f"{lead}<w:r>{drawing}</w:r>", style)

    def caption(self, word: str, number: int, label: str, nodes: list) -> str:
        field = self.field(f" SEQ {word} \\* ARABIC ", self.run(str(number)))
        separator = ". " if nodes else ""
        return self.para(self.run(word + " ") + self.bookmarked(label, field) + self.run(separator) + self.inlines(nodes), "Caption", "<w:keepLines/>" if word == "Figure" else "<w:keepNext/>")

    def figure(self, block: Figure) -> list[str]:
        out = [self.picture(image) for image in block.images]
        if not out:
            out.append(self.para(self.run("[Figure not available]"), "FigureBody"))
        if block.numbered:
            self.fig_n += 1
            out.append(self.caption("Figure", self.fig_n, block.label, block.caption))
        return out

    def table(self, block: Table) -> list[str]:
        out = []
        if block.numbered:
            self.tab_n += 1
            out.append(self.caption("Table", self.tab_n, block.label, block.caption))
        cols = max((sum(c.colspan for c in row) for row in block.rows), default=1)
        # Cells a \multirow covers take up columns in later rows.
        grid_cols = cols
        active: dict[int, list[int]] = {}
        rows_xml = []
        for r, row in enumerate(block.rows):
            cells = list(row)
            col = 0
            parts = []
            is_head = r < block.header_rows
            bottom = r == len(block.rows) - 1
            rule_below = r == block.header_rows - 1
            while col < grid_cols + 8:
                if col in active and active[col][0] > 0:
                    span = active[col][1]
                    active[col][0] -= 1
                    parts.append(self.table_cell(None, span, "continue", is_head, r == 0, bottom or rule_below, grid_cols))
                    col += span
                    continue
                if not cells:
                    break
                cell = cells.pop(0)
                merge = ""
                if cell.rowspan > 1:
                    active[col] = [cell.rowspan - 1, cell.colspan]
                    merge = "restart"
                parts.append(self.table_cell(cell, cell.colspan, merge, is_head, r == 0, bottom or rule_below, grid_cols))
                col += cell.colspan
            grid_cols = max(grid_cols, col)
            trpr = "<w:trPr><w:cantSplit/><w:tblHeader/></w:trPr>" if is_head else "<w:trPr><w:cantSplit/></w:trPr>"
            rows_xml.append(f"<w:tr>{trpr}{''.join(parts)}</w:tr>")
        width = 9360 // max(grid_cols, 1)
        grid = "".join(f'<w:gridCol w:w="{width}"/>' for _ in range(grid_cols))
        out.append(f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/><w:tblLook w:val="0420" w:firstRow="1" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" w:noHBand="0" w:noVBand="1"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>{"".join(rows_xml)}</w:tbl>')
        out.append('<w:p><w:pPr><w:pStyle w:val="TableSpacer"/></w:pPr></w:p>')
        return out

    def table_cell(self, cell, span: int, merge: str, head: bool, top: bool, bottom: bool, cols: int) -> str:
        width = 9360 // max(cols, 1) * span
        props = [f'<w:tcW w:w="{width}" w:type="dxa"/>']
        if span > 1:
            props.append(f'<w:gridSpan w:val="{span}"/>')
        if merge == "restart":
            props.append('<w:vMerge w:val="restart"/>')
        elif merge == "continue":
            props.append("<w:vMerge/>")
        borders = ('<w:top w:val="single" w:sz="8" w:space="0" w:color="000000"/>' if top else "") + ('<w:bottom w:val="single" w:sz="8" w:space="0" w:color="000000"/>' if bottom else "")
        if borders:
            props.append(f"<w:tcBorders>{borders}</w:tcBorders>")
        align = cell.align if cell is not None else ""
        jc = f'<w:jc w:val="{align}"/>' if align in ("left", "center", "right") else ""
        content = self.inlines(cell.children, ("strong",) if head else ()) if cell is not None else ""
        return f"<w:tc><w:tcPr>{''.join(props)}</w:tcPr>{self.para(content, 'TableText', jc)}</w:tc>"

    def bibliography(self) -> list[str]:
        out = [self.para(self.run("References"), "ReferencesHeading")]
        keys = sorted(self.cite_numbers, key=self.cite_numbers.get)
        for key in keys:
            entry = self.doc.bib.get(key)
            if entry is None:
                continue
            payload = json.dumps({"key": key}, ensure_ascii=False)
            mark = f"[{self.cite_numbers[key]}]" if self.doc.citation_style == "numeric" else ""
            field = self.field(f" ADDIN PAPERPUSH_BIBITEM {payload} ", self.run(mark))
            lead = self.run(" ") if mark else ""
            out.append(self.para(field + lead + self.run(biblib.format_reference(entry)), "Bibliography"))
        uncited = [k for k in self.doc.bib if k not in self.cite_numbers]
        if uncited:
            self.log.change(f"left {len(uncited)} uncited bibliography entries out of the reference list")
        return out

    # -- the package -----------------------------------------------------------

    def body(self) -> str:
        parts = self.front()
        placed = any(isinstance(b, Bibliography) for b in self.doc.blocks)
        for block in self.doc.blocks:
            parts += self.block(block)
        if self.cited and not placed:
            parts += self.bibliography()
        sect = '<w:sectPr><w:footnotePr><w:numFmt w:val="decimal"/></w:footnotePr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
        return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {DOC_NS}><w:body>{"".join(parts)}{sect}</w:body></w:document>'

    def bare_manifest(self) -> str:
        data = {"version": 1, "labels": self.labels, "citation_style": self.doc.citation_style}
        packed = base64.b64encode(zlib.compress(json.dumps(data, ensure_ascii=False).encode("utf-8"), 9)).decode("ascii")
        return f'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n<pp:manifest xmlns:pp="{MANIFEST_NS}">{packed}</pp:manifest>'

    def manifest(self, extra: dict) -> str:
        blocks: dict[str, list[str]] = {}
        math: dict[str, str] = {}
        groups: dict[str, list[str]] = {}
        top = self.doc.blocks
        for n, block in enumerate(top):
            size = getattr(block, "group", 0)
            if size > 1:
                groups[fingerprint(block)] = [fingerprint(b) for b in top[n + 1 : n + size]]
        for block in all_blocks(self.doc.blocks):
            if getattr(block, "source", "") and not isinstance(block, Raw):
                blocks.setdefault(fingerprint(block), []).append(block.source)
            if isinstance(block, Equation) and block.sig:
                math[block.sig] = block.latex
        from .model import all_inlines

        for node in all_inlines(self.doc):
            if isinstance(node, Math) and node.sig:
                math[node.sig] = node.latex
        data = {"version": 1, "labels": self.labels, "blocks": blocks, "groups": groups, "math": math, "raw": self.raw, "images": self.image_map, "preamble": self.doc.preamble, "front": self.doc.front_source, "front_fp": front_fingerprint(self.doc), "bib_source": self.doc.bib_source, "citation_style": self.doc.citation_style, "bib": {k: {"kind": e.kind, "fields": e.fields, "text": e.text} for k, e in self.doc.bib.items()}, **extra}
        packed = base64.b64encode(zlib.compress(json.dumps(data, ensure_ascii=False).encode("utf-8"), 9)).decode("ascii")
        return f'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n<pp:manifest xmlns:pp="{MANIFEST_NS}">{packed}</pp:manifest>'

    def write(self, path: Path, *, title: str = "", extra: dict | None = None, embed_source: bool = True) -> None:
        document = self.body()
        # Without the source: labels and citation data only, nothing a reader can't already see.
        manifest = self.manifest(extra or {}) if embed_source else self.bare_manifest()
        self.rel(f"{REL}/styles", "styles.xml")
        self.rel(f"{REL}/numbering", "numbering.xml")
        self.rel(f"{REL}/settings", "settings.xml")
        self.rel(f"{REL}/footnotes", "footnotes.xml")
        self.rel(f"{REL}/customXml", "../customXml/item1.xml")
        rels = "".join(f'<Relationship Id="{rid}" Type="{kind}" Target={quoteattr(target)}{" TargetMode=" + quoteattr(mode) if mode else ""}/>' for rid, kind, target, mode in self.rels)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        item_id = "{" + str(uuid.uuid4()).upper() + "}"
        content_types = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>' '<Default Extension="png" ContentType="image/png"/><Default Extension="jpeg" ContentType="image/jpeg"/>' '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>' '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>' '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>' '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>' '<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>' '<Override PartName="/customXml/itemProps1.xml" ContentType="application/vnd.openxmlformats-officedocument.customXmlProperties+xml"/>' '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>' '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>'
        package_rels = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="{REL}/extended-properties" Target="docProps/app.xml"/></Relationships>'
        core = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>{_x(title)}</dc:title><dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>'
        app = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>paperpush</Application></Properties>'
        footnotes = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:footnotes {DOC_NS}><w:footnote w:type="separator" w:id="{SEP_ID}"><w:p><w:r><w:separator/></w:r></w:p></w:footnote><w:footnote w:type="continuationSeparator" w:id="{CONT_ID}"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>{"".join(self.footnotes)}</w:footnotes>'
        settings = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:settings xmlns:w="{W_NS}" xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><w:defaultTabStop w:val="720"/><w:footnotePr><w:footnote w:id="{SEP_ID}"/><w:footnote w:id="{CONT_ID}"/></w:footnotePr><w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat><m:mathPr><m:mathFont m:val="Cambria Math"/><m:defJc m:val="centerGroup"/></m:mathPr></w:settings>'
        props = f'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n<ds:datastoreItem ds:itemID="{item_id}" xmlns:ds="http://schemas.openxmlformats.org/officeDocument/2006/customXml"><ds:schemaRefs><ds:schemaRef ds:uri="{MANIFEST_NS}"/></ds:schemaRefs></ds:datastoreItem>'
        item_rels = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}/customXmlProps" Target="itemProps1.xml"/></Relationships>'
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types)
            zf.writestr("_rels/.rels", package_rels)
            zf.writestr("docProps/core.xml", core)
            zf.writestr("docProps/app.xml", app)
            zf.writestr("word/document.xml", document)
            zf.writestr("word/_rels/document.xml.rels", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
            zf.writestr("word/styles.xml", _styles())
            zf.writestr("word/numbering.xml", self.numbering())
            zf.writestr("word/settings.xml", settings)
            zf.writestr("word/footnotes.xml", footnotes)
            if self.note_rels:
                note_rels = "".join(f'<Relationship Id="{rid}" Type="{kind}" Target={quoteattr(target)}{" TargetMode=" + quoteattr(mode) if mode else ""}/>' for rid, kind, target, mode in self.note_rels)
                zf.writestr("word/_rels/footnotes.xml.rels", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{note_rels}</Relationships>')
            zf.writestr("customXml/item1.xml", manifest)
            zf.writestr("customXml/itemProps1.xml", props)
            zf.writestr("customXml/_rels/item1.xml.rels", item_rels)
            for name, data in self.media:
                zf.writestr(f"word/media/{name}", data)

    def numbering(self) -> str:
        def heading_levels(first_fmt: str) -> str:
            levels = []
            for n in range(6):
                fmt = first_fmt if n == 0 else "decimal"
                text = ".".join(f"%{k + 1}" for k in range(n + 1))
                levels.append(f'<w:lvl w:ilvl="{n}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/><w:suff w:val="space"/><w:lvlText w:val="{text}"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="0" w:firstLine="0"/></w:pPr></w:lvl>')
            return "".join(levels)

        abstracts = [f'<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="multilevel"/>{heading_levels("decimal")}</w:abstractNum>', f'<w:abstractNum w:abstractNumId="2"><w:multiLevelType w:val="multilevel"/>{heading_levels("upperLetter")}</w:abstractNum>']
        nums = ['<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>', '<w:num w:numId="2"><w:abstractNumId w:val="2"/></w:num>']
        bullets = ["•", "◦", "▪"]
        for n, kinds in enumerate(self.lists, 3):
            levels = []
            for depth in range(max(len(kinds), 1)):
                ordered = kinds[depth]
                fmt, text = ("decimal", f"%{depth + 1}.") if ordered else ("bullet", bullets[depth % 3])
                levels.append(f'<w:lvl w:ilvl="{depth}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/><w:lvlText w:val="{text}"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="{720 * (depth + 1)}" w:hanging="360"/></w:pPr></w:lvl>')
            abstracts.append(f'<w:abstractNum w:abstractNumId="{n}"><w:multiLevelType w:val="hybridMultilevel"/>{"".join(levels)}</w:abstractNum>')
            nums.append(f'<w:num w:numId="{n}"><w:abstractNumId w:val="{n}"/></w:num>')
        return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:numbering xmlns:w="{W_NS}">{"".join(abstracts)}{"".join(nums)}</w:numbering>'


def front_fingerprint(doc: Document) -> str:
    """A hash of the title block as a reader sees it."""
    parts = [plain_text(doc.title), "|".join(a.name for a in doc.authors), "|".join(fingerprint(b) for b in doc.abstract), "|".join(doc.keywords)]
    return hashlib.sha256("\n".join(re.sub(r"\s+", " ", p.replace(" ", " ")).strip() for p in parts).encode("utf-8")).hexdigest()[:20]


def _compress(numbers: list[int]) -> str:
    """``[1, 2, 3, 7]`` as ``1–3, 7``."""
    out: list[str] = []
    i = 0
    while i < len(numbers):
        j = i
        while j + 1 < len(numbers) and numbers[j + 1] == numbers[j] + 1:
            j += 1
        if j - i >= 2:
            out.append(f"{numbers[i]}–{numbers[j]}")
        else:
            out += [str(n) for n in numbers[i : j + 1]]
        i = j + 1
    return ", ".join(out)


def _style(style_id: str, name: str, *, based: str = "Normal", nxt: str = "", ppr: str = "", rpr: str = "", kind: str = "paragraph", default: bool = False) -> str:
    based_xml = f'<w:basedOn w:val="{based}"/>' if based else ""
    next_xml = f'<w:next w:val="{nxt}"/>' if nxt else ""
    default_xml = ' w:default="1"' if default else ""
    return f'<w:style w:type="{kind}"{default_xml} w:styleId="{style_id}"><w:name w:val="{name}"/>{based_xml}{next_xml}<w:qFormat/>{"<w:pPr>" + ppr + "</w:pPr>" if ppr else ""}{"<w:rPr>" + rpr + "</w:rPr>" if rpr else ""}</w:style>'


def _styles() -> str:
    font = '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="Times New Roman" w:cs="Times New Roman"/>'
    mono = '<w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:cs="Courier New"/>'
    items = [
        _style("Normal", "Normal", based="", ppr='<w:spacing w:after="120" w:line="276" w:lineRule="auto"/>', default=True),
        _style("Title", "Title", nxt="Author", ppr='<w:spacing w:after="240"/><w:jc w:val="center"/>', rpr='<w:b/><w:sz w:val="36"/><w:szCs w:val="36"/>'),
        _style("Author", "Author", nxt="Affiliation", ppr='<w:spacing w:after="120"/><w:jc w:val="center"/>'),
        _style("Affiliation", "Affiliation", ppr='<w:spacing w:after="60"/><w:jc w:val="center"/>', rpr='<w:sz w:val="20"/><w:szCs w:val="20"/>'),
        _style("AbstractHeading", "Abstract Heading", nxt="Abstract", ppr='<w:keepNext/><w:spacing w:before="240" w:after="60"/>', rpr="<w:b/>"),
        _style("Abstract", "Abstract", ppr='<w:spacing w:after="120"/>'),
        _style("Keywords", "Keywords", ppr='<w:spacing w:after="240"/>'),
        _style("ReferencesHeading", "References Heading", nxt="Bibliography", ppr='<w:keepNext/><w:spacing w:before="360" w:after="120"/>', rpr='<w:b/><w:sz w:val="28"/><w:szCs w:val="28"/>'),
        _style("Bibliography", "Bibliography", ppr='<w:spacing w:after="80"/><w:ind w:left="480" w:hanging="480"/>'),
        _style("Caption", "Caption", ppr='<w:spacing w:before="60" w:after="200"/>', rpr='<w:sz w:val="20"/><w:szCs w:val="20"/>'),
        _style("FigureBody", "Figure Body", nxt="Caption", ppr='<w:keepNext/><w:spacing w:before="120" w:after="60"/><w:jc w:val="center"/>'),
        _style("DisplayEquation", "Display Equation", ppr='<w:spacing w:before="60" w:after="60"/><w:jc w:val="center"/>'),
        _style("EquationNumber", "Equation Number", ppr='<w:spacing w:after="0"/><w:jc w:val="right"/>'),
        _style("EquationSpacer", "Equation Spacer", ppr='<w:spacing w:after="0" w:line="120" w:lineRule="exact"/>'),
        _style("TableText", "Table Text", ppr='<w:spacing w:before="20" w:after="20" w:line="240" w:lineRule="auto"/>', rpr='<w:sz w:val="20"/><w:szCs w:val="20"/>'),
        _style("TableSpacer", "Table Spacer", ppr='<w:spacing w:after="120"/>'),
        _style("Quote", "Quote", ppr='<w:ind w:left="720" w:right="720"/>'),
        _style("ListParagraph", "List Paragraph", ppr='<w:spacing w:after="60"/>'),
        _style("ListContinue", "List Continue", ppr='<w:spacing w:after="60"/>'),
        _style("SourceCode", "Source Code", ppr='<w:spacing w:after="120" w:line="240" w:lineRule="auto"/>', rpr=f'{mono}<w:sz w:val="18"/><w:szCs w:val="18"/>'),
        _style("LatexBlock", "LaTeX Block", ppr='<w:shd w:val="clear" w:color="auto" w:fill="EEEEEE"/>', rpr='<w:i/><w:sz w:val="20"/><w:szCs w:val="20"/>'),
        _style("FootnoteText", "footnote text", ppr='<w:spacing w:after="0" w:line="240" w:lineRule="auto"/>', rpr='<w:sz w:val="20"/><w:szCs w:val="20"/>'),
        _style("DefaultParagraphFont", "Default Paragraph Font", based="", kind="character", default=True),
        _style("FootnoteReference", "footnote reference", based="DefaultParagraphFont", kind="character", rpr='<w:vertAlign w:val="superscript"/>'),
        _style("Hyperlink", "Hyperlink", based="DefaultParagraphFont", kind="character", rpr='<w:color w:val="0563C1"/><w:u w:val="single"/>'),
        _style("VerbatimChar", "Verbatim Char", based="DefaultParagraphFont", kind="character", rpr=mono),
    ]
    sizes = {1: 28, 2: 24, 3: 22, 4: 22, 5: 22, 6: 22}
    for level, size in sizes.items():
        italic = "<w:i/>" if level >= 4 else ""
        items.append(_style(f"Heading{level}", f"heading {level}", nxt="Normal", ppr=f'<w:keepNext/><w:keepLines/><w:spacing w:before="{320 - 40 * min(level, 3)}" w:after="100"/><w:outlineLvl w:val="{level - 1}"/>', rpr=f'<w:b/>{italic}<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'))
    defaults = f'<w:docDefaults><w:rPrDefault><w:rPr>{font}<w:sz w:val="24"/><w:szCs w:val="24"/><w:lang w:val="en-US"/></w:rPr></w:rPrDefault><w:pPrDefault/></w:docDefaults>'
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:styles xmlns:w="{W_NS}">{defaults}{"".join(items)}</w:styles>'
