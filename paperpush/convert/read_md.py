"""Read a Markdown manuscript into the document model.

Follows the Pandoc conventions academic Markdown is written in: a YAML title
block, ``[@key]`` citations, ``{#fig:x}`` labels, ``@fig:x`` cross-references
(the pandoc-crossref form), ``$...$`` math, and pipe tables with a ``: caption``
line.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import bib as biblib
from .mathconv import latex_to_omml, signature_of_xml
from .model import Author, Bibliography, Break, Cell, Cite, Code, Document, Equation, Figure, Heading, Image, Link, ListBlock, Log, Math, Note, Para, Quote, Ref, Style, Table, Text, merge_text

_LABEL_PREFIXES = ("fig", "tbl", "tab", "eq", "sec", "lst")
_ATTRS = re.compile(r"\s*\{([^{}]*)\}\s*$")
_FENCE = re.compile(r"^(```+|~~~+)\s*\{?\.?([\w+-]*)")
_LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_IMAGE_ONLY = re.compile(r"^!\[(.*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)\s*(\{[^{}]*\})?\s*$", re.S)
_CITE_KEY = r"[A-Za-z0-9_][\w:.#$%&+?<>~/-]*"
_BRACKET_CITE = re.compile(r"\[((?:[^\[\]]*?-?@" + _CITE_KEY + r"[^\[\]]*?)(?:;[^\[\]]*)*)\]")
_BARE_CITE = re.compile(r"(?<![\w@.])@(" + _CITE_KEY + r")")


def parse_front(text: str) -> tuple[dict, str]:
    """The YAML title block, as a small dict, and the text after it."""
    m = re.match(r"\A---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", text, re.S)
    if not m:
        return {}, text
    return _yaml(m.group(1)), text[m.end() :]


def _scalar(value: str):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        return [_scalar(v) for v in re.split(r",\s*(?![^\[]*\])", value[1:-1]) if v.strip()]
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def _yaml(block: str) -> dict:
    """The YAML subset title blocks use: scalars, ``|``/``>`` text, lists, and lists of maps."""
    lines = block.split("\n")
    out, _ = _yaml_map(lines, 0, 0)
    return out


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _yaml_map(lines: list[str], i: int, indent: int) -> tuple[dict, int]:
    out: dict = {}
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if _indent(line) < indent:
            break
        m = re.match(r"\s*([\w-]+):\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, value = m.group(1), m.group(2).strip()
        i += 1
        if value in ("|", ">", "|-", ">-"):
            block: list[str] = []
            while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > _indent(line)):
                block.append(lines[i].strip())
                i += 1
            out[key] = ("\n" if value.startswith("|") else " ").join(block).strip()
        elif value:
            out[key] = _scalar(value)
        else:
            nxt = next((x for x in lines[i:] if x.strip()), "")
            if nxt.lstrip().startswith("- "):
                out[key], i = _yaml_list(lines, i, _indent(nxt))
            elif nxt and _indent(nxt) > _indent(line):
                out[key], i = _yaml_map(lines, i, _indent(nxt))
            else:
                out[key] = ""
    return out, i


def _yaml_list(lines: list[str], i: int, indent: int) -> tuple[list, int]:
    out: list = []
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if _indent(line) != indent or not line.lstrip().startswith("- "):
            break
        rest = line.lstrip()[2:]
        if re.match(r"[\w-]+:(\s|$)", rest):
            inner = indent + 2
            sub = [" " * inner + rest]
            i += 1
            while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) >= inner):
                sub.append(lines[i])
                i += 1
            out.append(_yaml_map(sub, 0, inner)[0])
        else:
            out.append(_scalar(rest))
            i += 1
    return out, i


class MarkdownReader:
    def __init__(self, path: Path, log: Log, *, bib: Path | None = None) -> None:
        self.path = path
        self.root = path.parent
        self.log = log
        self.bib_path = bib
        self.notes: dict[str, str] = {}
        self.labels: set[str] = set()

    def read(self) -> Document:
        raw = self.path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        front, body = parse_front(raw)
        doc = Document()
        self.apply_front(doc, front)
        body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
        body = self.collect_notes(body)
        self.labels = set(re.findall(r"\{#((?:" + "|".join(_LABEL_PREFIXES) + r"):[^\s{}]+)", body))
        doc.blocks = self.blocks(body.split("\n"))
        self.split_abstract(doc)
        doc.numbered_sections = bool(front.get("numbersections") or front.get("number-sections"))
        for block in doc.blocks:
            if isinstance(block, Heading):
                block.numbered = doc.numbered_sections
        self.load_bib(doc, front)
        if str(front.get("citation-style", "")).lower().find("author") >= 0 or front.get("citation_style") == "author-year":
            doc.citation_style = "author-year"
        if doc.bib and not any(isinstance(b, Bibliography) for b in doc.blocks):
            doc.blocks.append(Bibliography())
        return doc

    def apply_front(self, doc: Document, front: dict) -> None:
        if front.get("title"):
            doc.title = self.inlines(str(front["title"]))
        authors = front.get("author") or front.get("authors") or []
        if isinstance(authors, str):
            authors = [a.strip() for a in re.split(r"\s+and\s+|;", authors) if a.strip()]
        for item in authors:
            if isinstance(item, str):
                doc.authors.append(Author(name=item))
                continue
            if len(item) == 1 and isinstance(next(iter(item.values())), dict):  # "- Name: {affiliation: ...}"
                name, info = next(iter(item.items()))
                item = {"name": name, **info}
            author = Author(name=str(item.get("name", "")), email=str(item.get("email", "")), corresponding=bool(item.get("corresponding")))
            affil = item.get("affiliation") or item.get("affiliations") or []
            for value in affil if isinstance(affil, list) else [affil]:
                value = str(value)
                if value and value not in doc.affiliations:
                    doc.affiliations.append(value)
                if value:
                    author.affiliations.append(doc.affiliations.index(value) + 1)
            doc.authors.append(author)
        if front.get("abstract"):
            doc.abstract = [Para(children=self.inlines(p)) for p in re.split(r"\n\s*\n", str(front["abstract"])) if p.strip()]
        keywords = front.get("keywords") or []
        doc.keywords = [str(k) for k in (keywords if isinstance(keywords, list) else re.split(r"[,;]\s*", str(keywords)))]

    def load_bib(self, doc: Document, front: dict) -> None:
        wanted = front.get("bibliography") or []
        paths: list[Path] = [self.bib_path] if self.bib_path else []
        for name in wanted if isinstance(wanted, list) else [wanted]:
            candidate = self.root / str(name)
            if candidate.is_file():
                paths.append(candidate)
            else:
                self.log.warn(f"could not find the bibliography '{name}'")
        if not paths:
            sibling = self.path.with_suffix(".bib")
            found = [sibling] if sibling.is_file() else sorted(self.root.glob("*.bib"))
            if len(found) == 1:
                paths = found
                self.log.change(f"used {found[0].name}, the only .bib next to the manuscript")
        for path in paths:
            contents = path.read_text(encoding="utf-8", errors="replace")
            doc.bib_source[path.name] = contents
            doc.bib.update(biblib.parse_bib(contents))

    def split_abstract(self, doc: Document) -> None:
        """An ``# Abstract`` section at the top becomes the abstract."""
        if doc.abstract or not doc.blocks:
            return
        for n, block in enumerate(doc.blocks[:3]):
            if isinstance(block, Heading) and re.fullmatch(r"(?i)\s*abstract[.:]?\s*", "".join(c.text for c in block.children if isinstance(c, Text))):
                end = n + 1
                while end < len(doc.blocks) and not isinstance(doc.blocks[end], Heading):
                    end += 1
                doc.abstract = doc.blocks[n + 1 : end]
                doc.blocks = doc.blocks[:n] + doc.blocks[end:]
                return

    def collect_notes(self, body: str) -> str:
        out: list[str] = []
        lines = body.split("\n")
        i = 0
        while i < len(lines):
            m = re.match(r"^\[\^([^\]]+)\]:\s*(.*)$", lines[i])
            if m:
                text = [m.group(2)]
                i += 1
                while i < len(lines) and (lines[i].startswith(("    ", "\t")) or (not lines[i].strip() and i + 1 < len(lines) and lines[i + 1].startswith(("    ", "\t")))):
                    text.append(lines[i].strip())
                    i += 1
                self.notes[m.group(1)] = "\n".join(text)
                continue
            out.append(lines[i])
            i += 1
        return "\n".join(out)

    # -- blocks ----------------------------------------------------------------

    def blocks(self, lines: list[str]) -> list:
        out: list = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            fence = _FENCE.match(line)
            if fence:
                marker = fence.group(1)
                j = i + 1
                while j < len(lines) and not lines[j].startswith(marker):
                    j += 1
                out.append(Code(text="\n".join(lines[i + 1 : j]), lang=fence.group(2)))
                i = j + 1
                continue
            heading = _HEADING.match(line)
            if heading:
                out.append(self.heading(len(heading.group(1)), heading.group(2)))
                i += 1
                continue
            if i + 1 < len(lines) and line.strip() and re.match(r"^(=+|-+)\s*$", lines[i + 1]) and not line.lstrip().startswith(("|", "-", "*", ">")):
                out.append(self.heading(1 if lines[i + 1].startswith("=") else 2, line.strip()))
                i += 2
                continue
            if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
                i += 1
                continue
            if line.lstrip().startswith("$$"):
                i = self.display_math(lines, i, out)
                continue
            if line.lstrip().startswith(">"):
                j = i
                inner: list[str] = []
                while j < len(lines) and lines[j].lstrip().startswith(">"):
                    inner.append(re.sub(r"^\s*>\s?", "", lines[j]))
                    j += 1
                out.append(Quote(blocks=self.blocks(inner)))
                i = j
                continue
            if _LIST_ITEM.match(line):
                i = self.list_block(lines, i, out)
                continue
            if "|" in line and i + 1 < len(lines) and _TABLE_RULE.match(lines[i + 1]):
                i = self.table(lines, i, out)
                continue
            if re.match(r"^:\s", line) or re.match(r"^(?:Table|Tbl\.?):\s", line):
                j = i
                while j < len(lines) and lines[j].strip():
                    j += 1
                following = j
                while following < len(lines) and not lines[following].strip():
                    following += 1
                if following + 1 < len(lines) and "|" in lines[following] and _TABLE_RULE.match(lines[following + 1]):
                    caption = " ".join(lines[i:j])
                    k = self.table(lines, following, out)
                    self.apply_caption(out[-1], caption)
                    i = k
                    continue
            j = i
            para: list[str] = []
            while j < len(lines) and lines[j].strip():
                if j > i and (_HEADING.match(lines[j]) or _FENCE.match(lines[j]) or lines[j].lstrip().startswith(("$$", ">"))):
                    break
                if j > i and _LIST_ITEM.match(lines[j]) and not re.match(r"^\s*\d+[.)]", lines[j]):
                    break
                para.append(lines[j])
                j += 1
            out.append(self.paragraph("\n".join(para)))
            i = j
        return out

    def heading(self, level: int, text: str) -> Heading:
        label, numbered = "", True
        m = _ATTRS.search(text)
        if m:
            attrs = m.group(1)
            text = text[: m.start()]
            ident = re.search(r"#([^\s}]+)", attrs)
            label = ident.group(1) if ident else ""
            numbered = ".unnumbered" not in attrs
        return Heading(level=level, children=self.inlines(text.strip()), label=label, numbered=numbered)

    def display_math(self, lines: list[str], i: int, out: list) -> int:
        text = lines[i].lstrip()[2:]
        j = i
        while "$$" not in text and j + 1 < len(lines):
            j += 1
            text += "\n" + lines[j]
        body, _, rest = text.partition("$$")
        label = ""
        m = re.search(r"\{#([^\s{}]+)", rest)
        if m:
            label = m.group(1)
        body = body.strip()
        tagged = re.search(r"\\label\{([^{}]+)\}", body)
        if tagged:
            label = label or tagged.group(1)
            body = body.replace(tagged.group(0), "").strip()
        env_match = re.fullmatch(r"\\begin\{(align|gather|multline)\*?\}(.*)\\end\{\1\*?\}", body, re.S)
        rows = bool(env_match)
        env = env_match.group(1) if env_match else "equation"
        latex = env_match.group(2).strip() if env_match else body
        omml, unsupported = latex_to_omml(latex, rows=rows)
        for cmd in sorted(unsupported):
            self.log.warn(f"the math command {cmd} has no Word equivalent: it appears as literal text in the equation")
        out.append(Equation(latex=latex, env=env, row_labels=[label] if label else [], numbered=bool(label), sig=signature_of_xml(omml)))
        return j + 1

    def list_block(self, lines: list[str], i: int, out: list) -> int:
        first = _LIST_ITEM.match(lines[i])
        base = len(first.group(1))
        ordered = first.group(2)[0].isdigit()
        items: list[list[str]] = []
        while i < len(lines):
            line = lines[i]
            m = _LIST_ITEM.match(line)
            if m and len(m.group(1)) == base:
                if m.group(2)[0].isdigit() != ordered:
                    break
                items.append([m.group(3)])
                i += 1
                continue
            if not line.strip():
                nxt = next((x for x in lines[i + 1 :] if x.strip()), "")
                if nxt and (_indent(nxt) > base or (_LIST_ITEM.match(nxt) and _indent(nxt) == base)):
                    if items:
                        items[-1].append("")
                    i += 1
                    continue
                break
            if _indent(line) > base or not (_HEADING.match(line) or line.lstrip().startswith(("$$", ">"))):
                if items:
                    cut = min(_indent(line), base + 2)
                    items[-1].append(line[cut:])
                i += 1
                continue
            break
        out.append(ListBlock(ordered=ordered, items=[self.blocks(item) or [Para(children=[])] for item in items]))
        return i

    def table(self, lines: list[str], i: int, out: list) -> int:
        def cells(line: str) -> list[str]:
            line = line.strip()
            if line.startswith("|"):
                line = line[1:]
            if line.endswith("|") and not line.endswith("\\|"):
                line = line[:-1]
            return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]

        aligns = []
        for spec in cells(lines[i + 1]):
            left, right = spec.startswith(":"), spec.endswith(":")
            aligns.append("center" if left and right else "right" if right else "left")
        rows = [[Cell(children=self.inlines(c), align=aligns[n] if n < len(aligns) else "") for n, c in enumerate(cells(lines[i]))]]
        j = i + 2
        while j < len(lines) and lines[j].strip() and "|" in lines[j]:
            rows.append([Cell(children=self.inlines(c), align=aligns[n] if n < len(aligns) else "") for n, c in enumerate(cells(lines[j]))])
            j += 1
        table = Table(rows=rows, aligns=aligns, numbered=False)
        out.append(table)
        k = j
        while k < len(lines) and not lines[k].strip():
            k += 1
        if k < len(lines) and re.match(r"^(?::|Table:|Tbl\.?:)\s", lines[k]):
            end = k
            while end < len(lines) and lines[end].strip():
                end += 1
            self.apply_caption(table, " ".join(lines[k:end]))
            return end
        return j

    def apply_caption(self, table: Table, line: str) -> None:
        text = re.sub(r"^(?::|Table:|Tbl\.?:)\s*", "", line).strip()
        m = _ATTRS.search(text)
        if m:
            ident = re.search(r"#([^\s}]+)", m.group(1))
            table.label = ident.group(1) if ident else ""
            text = text[: m.start()].strip()
        table.caption = self.inlines(text)
        table.numbered = True

    def paragraph(self, text: str):
        m = _IMAGE_ONLY.match(text.strip())
        if m:
            attrs = m.group(3) or ""
            ident = re.search(r"#([^\s}]+)", attrs)
            width = re.search(r"width\s*=\s*\"?([\d.]+)(%|in|cm|px)?", attrs)
            width_in = None
            if width:
                value, unit = float(width.group(1)), width.group(2) or "px"
                width_in = {"%": value / 100 * 6.5, "in": value, "cm": value / 2.54, "px": value / 96}[unit]
            path = self.root / m.group(2)
            if not path.is_file():
                self.log.warn(f"could not find the figure file '{m.group(2)}'")
            caption = self.inlines(m.group(1))
            return Figure(images=[Image(path=str(path) if path.is_file() else "", width_in=width_in, original=m.group(2))], caption=caption, label=ident.group(1) if ident else "", numbered=bool(caption) or bool(ident))
        return Para(children=self.inlines(re.sub(r"[ \t]*\n[ \t]*", lambda _: "\n", text.strip())))

    # -- inlines ---------------------------------------------------------------

    def inlines(self, text: str) -> list:
        return merge_text(self._inlines(text))

    def _inlines(self, text: str) -> list:
        out: list = []
        buf: list[str] = []
        i, n = 0, len(text)

        def flush() -> None:
            if buf:
                out.append(Text("".join(buf)))
                buf.clear()

        while i < n:
            ch = text[i]
            if ch == "\\" and i + 1 < n:
                if text[i + 1] == "\n":
                    flush()
                    out.append(Break())
                    i += 2
                    continue
                m = re.compile(r"\\(ref|eqref|cite[tp]?)\{([^{}]+)\}").match(text, i)
                if m:
                    flush()
                    if m.group(1).startswith("cite"):
                        out.append(Cite(keys=[k.strip() for k in m.group(2).split(",")], mode="text" if m.group(1) == "citet" else "paren", cmd=m.group(1)))
                    else:
                        out.append(Ref(m.group(2), m.group(1)))
                    i = m.end()
                    continue
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == "`":
                m = re.compile(r"(`+)(.+?)\1(?!`)", re.S).match(text, i)
                if m:
                    flush()
                    out.append(Style("code", [Text(m.group(2).strip())]))
                    i = m.end()
                    continue
            if ch == "$" and i + 1 < n and not text[i + 1].isspace():
                m = re.compile(r"\$((?:[^$\\]|\\.)+?)\$(?!\d)", re.S).match(text, i)
                if m and not m.group(1).endswith(" "):
                    flush()
                    out.append(self.math(m.group(1)))
                    i = m.end()
                    continue
            if ch == "!" and text.startswith("![", i):
                m = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)(?:\{[^{}]*\})?").match(text, i)
                if m:
                    self.log.warn(f"an image inside a paragraph ('{m.group(2)}') was left out; put it in a paragraph of its own")
                    i = m.end()
                    continue
            if ch == "^" and text.startswith("^[", i):
                end = _closing(text, i + 1, "[", "]")
                if end > 0:
                    flush()
                    out.append(Note(blocks=[Para(children=self.inlines(text[i + 2 : end]))]))
                    i = end + 1
                    continue
            if ch == "[":
                advanced = self.bracket(text, i, out, flush)
                if advanced is not None:
                    i = advanced
                    continue
            if ch == "@":
                m = _BARE_CITE.match(text, i) if (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "@.")) else None
                if m:
                    key = m.group(1).rstrip(".,;:")
                    flush()
                    out += self.at_reference(key, "text")
                    i = m.start() + 1 + len(key)
                    continue
            if ch in "*_":
                advanced = self.emphasis(text, i, out, flush)
                if advanced is not None:
                    i = advanced
                    continue
            if ch in "~^":
                if ch == "~" and text.startswith("~~", i):
                    m = re.compile(r"~~(.+?)~~", re.S).match(text, i)
                    if m:
                        self.log.count("removed strikethrough markup, kept its text")
                        flush()
                        out += self._inlines(m.group(1))
                        i = m.end()
                        continue
                m = re.compile(re.escape(ch) + r"([^\s" + re.escape(ch) + r"]+)" + re.escape(ch)).match(text, i)
                if m:
                    flush()
                    out.append(Style("sup" if ch == "^" else "sub", [Text(m.group(1))]))
                    i = m.end()
                    continue
            if ch == "<":
                m = re.compile(r"<(sup|sub|em|i|b|strong|u)>(.*?)</\1>", re.S).match(text, i)
                if m:
                    flush()
                    kind = {"sup": "sup", "sub": "sub", "em": "emph", "i": "emph", "b": "strong", "strong": "strong", "u": "underline"}[m.group(1)]
                    out.append(Style(kind, self._inlines(m.group(2))))
                    i = m.end()
                    continue
                m = re.compile(r"<(https?://[^\s>]+)>").match(text, i)
                if m:
                    flush()
                    out.append(Link(url=m.group(1), children=[Text(m.group(1))]))
                    i = m.end()
                    continue
                m = re.compile(r"<br\s*/?>").match(text, i)
                if m:
                    flush()
                    out.append(Break())
                    i = m.end()
                    continue
            if ch == " " and text.startswith("  \n", i):
                flush()
                out.append(Break())
                i += 3
                continue
            if ch == "\n":
                buf.append(" ")
            else:
                buf.append(ch)
            i += 1
        flush()
        return self.smart(out)

    def smart(self, inlines: list) -> list:
        """Typographic quotes and dashes, as Pandoc's ``smart`` does."""
        for n, node in enumerate(inlines):
            if isinstance(node, Text):
                text = node.text.replace("---", "—").replace("--", "–")
                text = re.sub(r'(^|[\s(\[])"', "\\1“", text).replace('"', "”")
                text = re.sub(r"(^|[\s(\[])'", "\\1‘", text).replace("'", "’")
                inlines[n] = Text(text)
        return inlines

    def math(self, latex: str) -> Math:
        omml, unsupported = latex_to_omml(latex)
        for cmd in sorted(unsupported):
            self.log.warn(f"the math command {cmd} has no Word equivalent: it appears as literal text in the equation")
        return Math(latex=latex, sig=signature_of_xml(omml))

    def at_reference(self, key: str, mode: str) -> list:
        if key.split(":", 1)[0] in _LABEL_PREFIXES and ":" in key:
            prefix = key.split(":", 1)[0]
            word = {"fig": "Figure", "tbl": "Table", "tab": "Table", "eq": "Equation", "sec": "Section", "lst": "Listing"}[prefix]
            if prefix == "eq":
                return [Text(word + "\u00a0"), Ref(key, "eqref")]
            return [Text(word + "\u00a0"), Ref(key, "ref")]
        return [Cite(keys=[key], mode=mode)]

    def bracket(self, text: str, i: int, out: list, flush) -> int | None:
        m = re.compile(r"\[\^([^\]]+)\]").match(text, i)
        if m and m.group(1) in self.notes:
            flush()
            out.append(Note(blocks=[Para(children=self.inlines(p)) for p in re.split(r"\n\s*\n", self.notes[m.group(1)]) if p.strip()]))
            return m.end()
        end = _closing(text, i, "[", "]")
        if end < 0:
            return None
        inner = text[i + 1 : end]
        if text[end + 1 : end + 2] == "(":
            close = _closing(text, end + 1, "(", ")")
            if close > 0:
                flush()
                url = text[end + 2 : close].split()[0] if text[end + 2 : close].split() else ""
                out.append(Link(url=url, children=self._inlines(inner)))
                return close + 1
        m = _BRACKET_CITE.fullmatch(text[i : end + 1])
        if m and "@" in inner:
            parts = [p.strip() for p in inner.split(";") if p.strip()]
            keys: list[str] = []
            refs: list = []
            pre = post = ""
            for part in parts:
                found = re.search(r"-?@(" + _CITE_KEY + r")", part)
                if not found:
                    return None
                key = found.group(1).rstrip(".,;:")
                if key.split(":", 1)[0] in _LABEL_PREFIXES and ":" in key:
                    refs += self.at_reference(key, "paren")
                    continue
                keys.append(key)
                if len(parts) == 1:
                    pre = part[: found.start()].strip()
                    post = part[found.end() :].lstrip(", ").strip()
            flush()
            out += refs
            if keys:
                out.append(Cite(keys=keys, mode="paren", pre=pre, post=post))
            return end + 1
        return None

    def emphasis(self, text: str, i: int, out: list, flush) -> int | None:
        ch = text[i]
        for marker, kinds in ((ch * 3, ("strong", "emph")), (ch * 2, ("strong",)), (ch, ("emph",))):
            if not text.startswith(marker, i):
                continue
            if ch == "_" and i > 0 and text[i - 1].isalnum():
                return None
            start = i + len(marker)
            if start >= len(text) or text[start].isspace():
                continue
            end = self.closing_emphasis(text, start, marker)
            if end < 0:
                continue
            flush()
            inner = self._inlines(text[start:end])
            for kind in reversed(kinds):
                inner = [Style(kind, inner)]
            out += inner
            return end + len(marker)
        return None

    def closing_emphasis(self, text: str, start: int, marker: str) -> int:
        i = start
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == "`":
                close = text.find("`", i + 1)
                if close > 0:
                    i = close + 1
                    continue
            if text[i] == "$":
                close = text.find("$", i + 1)
                if close > 0:
                    i = close + 1
                    continue
            if text.startswith(marker, i) and not text[i - 1].isspace():
                after = text[i + len(marker) : i + len(marker) + 1]
                if marker[0] == "_" and after.isalnum():
                    i += 1
                    continue
                if len(marker) == 1 and after == marker:
                    i += 2
                    continue
                return i
            i += 1
        return -1


def _closing(text: str, i: int, opener: str, closer: str) -> int:
    depth = 0
    j = i
    while j < len(text):
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1
