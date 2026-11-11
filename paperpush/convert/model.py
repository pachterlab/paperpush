"""The document model every reader produces and every writer consumes.

A manuscript is a small tree: front matter, a list of blocks (headings,
paragraphs, lists, equations, figures, tables), and a bibliography. Citations
and cross-references are nodes rather than text, so "[3]" in Word and
``\\cite{smith2020}`` in LaTeX are the same thing here.

:func:`fingerprint` reduces a block to what a reader can see of it. A LaTeX
block and the Word block it became have the same fingerprint until somebody
edits it, which is how an unedited paragraph gets its original source back.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Union

# --- inlines ------------------------------------------------------------------


@dataclass
class Text:
    text: str


@dataclass
class Style:
    kind: str  # emph, strong, underline, smallcaps, sup, sub, code
    children: list["Inline"] = field(default_factory=list)


@dataclass
class Math:
    latex: str
    sig: str = ""  # signature of the Word equation it corresponds to


@dataclass
class Link:
    url: str
    children: list["Inline"] = field(default_factory=list)


@dataclass
class Cite:
    keys: list[str]
    mode: str = "paren"  # paren, text
    cmd: str = ""  # the LaTeX command it came from, when it came from one
    pre: str = ""
    post: str = ""


@dataclass
class Ref:
    label: str
    kind: str = "ref"  # ref, eqref, page


@dataclass
class Note:
    blocks: list["Block"] = field(default_factory=list)


@dataclass
class Break:
    pass


@dataclass
class Comment:
    author: str
    text: str


Inline = Union[Text, Style, Math, Link, Cite, Ref, Note, Break, Comment]

# --- blocks -------------------------------------------------------------------


@dataclass
class Heading:
    level: int
    children: list[Inline]
    label: str = ""
    numbered: bool = True
    source: str = ""


@dataclass
class Para:
    children: list[Inline]
    source: str = ""


@dataclass
class ListBlock:
    ordered: bool
    items: list[list["Block"]]
    source: str = ""


@dataclass
class Quote:
    blocks: list["Block"]
    source: str = ""


@dataclass
class Code:
    text: str
    lang: str = ""
    source: str = ""


@dataclass
class Equation:
    latex: str  # the body; rows separated by \\ when there are several
    env: str = "equation"  # equation, align, gather, multline
    row_labels: list[str] = field(default_factory=list)  # one per number shown
    numbered: bool = True
    sig: str = ""
    source: str = ""

    @property
    def label(self) -> str:
        return next((x for x in self.row_labels if x), "")


@dataclass
class Image:
    path: str  # file on disk, as found or rendered
    width_in: float | None = None
    original: str = ""  # the path as the source wrote it
    tikz: str = ""  # LaTeX to render, when there is no file


@dataclass
class Figure:
    images: list[Image]
    caption: list[Inline] = field(default_factory=list)
    label: str = ""
    numbered: bool = True
    source: str = ""


@dataclass
class Cell:
    children: list[Inline]
    colspan: int = 1
    rowspan: int = 1
    align: str = ""


@dataclass
class Table:
    rows: list[list[Cell]]
    caption: list[Inline] = field(default_factory=list)
    label: str = ""
    header_rows: int = 1
    aligns: list[str] = field(default_factory=list)
    numbered: bool = True
    source: str = ""


@dataclass
class Raw:
    """Source the model cannot represent: kept for LaTeX, reported for Word."""

    tex: str
    note: str = ""
    source: str = ""


@dataclass
class Appendix:
    source: str = ""


@dataclass
class Bibliography:
    """Where the reference list goes."""

    source: str = ""


Block = Union[Heading, Para, ListBlock, Quote, Code, Equation, Figure, Table, Raw, Appendix, Bibliography]

# --- the document -------------------------------------------------------------


@dataclass
class Author:
    name: str
    affiliations: list[int] = field(default_factory=list)  # 1-based
    email: str = ""
    corresponding: bool = False


@dataclass
class BibEntry:
    key: str
    kind: str = "misc"
    fields: dict[str, str] = field(default_factory=dict)
    text: str = ""  # the formatted reference, when that is all there is

    @property
    def structured(self) -> bool:
        return bool(self.fields)


@dataclass
class Document:
    title: list[Inline] = field(default_factory=list)
    authors: list[Author] = field(default_factory=list)
    affiliations: list[str] = field(default_factory=list)
    abstract: list[Block] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    bib: dict[str, BibEntry] = field(default_factory=dict)
    citation_style: str = "numeric"  # numeric, author-year
    numbered_sections: bool = True
    preamble: str = ""  # LaTeX only
    front_source: str = ""  # LaTeX only: everything from \begin{document} to the first section
    bib_source: dict[str, str] = field(default_factory=dict)  # name -> contents of each .bib
    macros: dict = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)  # Word only: what paperpush embedded on the way in
    extra_labels: dict[str, str] = field(default_factory=dict)  # labels on things the model has no node for (theorems, algorithms)


class Log:
    """What a conversion normalized (``changes``) and what the author must look at (``warnings``)."""

    def __init__(self) -> None:
        self.changes: list[str] = []
        self.warnings: list[str] = []
        self._counts: dict[str, int] = {}

    def change(self, text: str) -> None:
        if text not in self.changes:
            self.changes.append(text)

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)

    def count(self, what: str, n: int = 1) -> None:
        """Tally a repeated normalization; :meth:`flush` writes one line for it."""
        self._counts[what] = self._counts.get(what, 0) + n

    def flush(self) -> None:
        for what, n in self._counts.items():
            self.change(f"{what} ({n})")
        self._counts.clear()


# --- walking ------------------------------------------------------------------


def walk_inlines(inlines: list[Inline]):
    """Every inline in ``inlines``, depth first, including those in footnotes."""
    for node in inlines:
        yield node
        if isinstance(node, (Style, Link)):
            yield from walk_inlines(node.children)
        elif isinstance(node, Note):
            for block in node.blocks:
                yield from block_inlines(block)


def block_inlines(block: Block):
    """Every inline inside ``block``."""
    if isinstance(block, (Heading, Para)):
        yield from walk_inlines(block.children)
    elif isinstance(block, ListBlock):
        for item in block.items:
            for sub in item:
                yield from block_inlines(sub)
    elif isinstance(block, Quote):
        for sub in block.blocks:
            yield from block_inlines(sub)
    elif isinstance(block, Figure):
        yield from walk_inlines(block.caption)
    elif isinstance(block, Table):
        yield from walk_inlines(block.caption)
        for row in block.rows:
            for cell in row:
                yield from walk_inlines(cell.children)


def all_inlines(doc: Document):
    """Every inline in the title, abstract, and body."""
    yield from walk_inlines(doc.title)
    for block in list(doc.abstract) + list(doc.blocks):
        yield from block_inlines(block)


def all_blocks(blocks: list[Block]):
    """Every block, descending into lists and quotes."""
    for block in blocks:
        yield block
        if isinstance(block, ListBlock):
            for item in block.items:
                yield from all_blocks(item)
        elif isinstance(block, Quote):
            yield from all_blocks(block.blocks)


def plain_text(inlines: list[Inline]) -> str:
    """The visible words of ``inlines``, without math, citations, references, or notes."""
    out: list[str] = []
    for node in inlines:
        if isinstance(node, Text):
            out.append(node.text)
        elif isinstance(node, (Style, Link)):
            out.append(plain_text(node.children))
        elif isinstance(node, Break):
            out.append(" ")
    return "".join(out)


# --- numbering ----------------------------------------------------------------


def number_labels(doc: Document) -> dict[str, str]:
    """What each label displays as: ``{"fig:a": "1", "eq:b": "3", "sec:c": "2.1"}``."""
    out: dict[str, str] = dict(doc.extra_labels)
    counters = [0] * 6
    fig = tab = eq = 0
    appendix = False
    for block in doc.blocks:
        if isinstance(block, Appendix):
            appendix = True
            counters = [0] * 6
        elif isinstance(block, Heading) and block.numbered:
            level = min(max(block.level, 1), 6) - 1
            counters[level] += 1
            counters[level + 1 :] = [0] * (5 - level)
            first = chr(ord("A") + counters[0] - 1) if appendix and counters[0] else str(counters[0])
            number = ".".join([first] + [str(c) for c in counters[1 : level + 1]])
            if block.label:
                out[block.label] = number
    for block in all_blocks(doc.blocks):
        if isinstance(block, Figure) and block.numbered:
            fig += 1
            if block.label:
                out[block.label] = str(fig)
        elif isinstance(block, Table) and block.numbered:
            tab += 1
            if block.label:
                out[block.label] = str(tab)
        elif isinstance(block, Equation) and block.numbered:
            for label in block.row_labels or [""]:
                eq += 1
                if label:
                    out[label] = str(eq)
    return out


def cited_keys(doc: Document) -> list[str]:
    """Citation keys in order of first citation."""
    seen: dict[str, None] = {}
    for node in all_inlines(doc):
        if isinstance(node, Cite):
            for key in node.keys:
                seen.setdefault(key)
    return list(seen)


# --- fingerprints -------------------------------------------------------------


def _chunks(inlines: list[Inline], styles: frozenset, out: list) -> None:
    """Flatten to ``(styles, text)`` pairs, so nesting order and what a style wraps stop mattering."""
    for node in inlines:
        if isinstance(node, Text):
            out.append((styles if node.text.strip() else None, node.text))
        elif isinstance(node, Style):
            _chunks(node.children, styles | {node.kind}, out)
        elif isinstance(node, Math):
            out.append((None, f"\x01m\x02{node.sig or node.latex}\x03"))
        elif isinstance(node, Link):
            out.append((None, f"\x01l:{node.url}\x02{_flat(node.children)}\x03"))
        elif isinstance(node, Cite):
            out.append((None, f"\x01c:{','.join(node.keys)}:{node.mode}\x03"))
        elif isinstance(node, Ref):
            ref = f"\x01r:{node.label}\x03"
            out.append((None, f"({ref})" if node.kind == "eqref" else ref))
        elif isinstance(node, Note):
            out.append((None, "\x01n\x02" + "|".join(_fp_text(b) for b in node.blocks) + "\x03"))
        elif isinstance(node, Break):
            out.append((None, "\n"))


def _flat(inlines: list[Inline]) -> str:
    chunks: list = []
    _chunks(inlines, frozenset(), chunks)
    out: list[str] = []
    n = 0
    while n < len(chunks):
        styles, text = chunks[n]
        n += 1
        # White space between two runs of one style belongs to the run.
        while styles and n < len(chunks) and (chunks[n][0] == styles or (chunks[n][0] is None and not chunks[n][1].strip() and n + 1 < len(chunks) and chunks[n + 1][0] == styles)):
            text += chunks[n][1]
            n += 1
        out.append(f"\x01{','.join(sorted(styles))}\x02{text}\x03" if styles else text)
    return "".join(out)


def _unbold(children: list[Inline]) -> list[Inline]:
    out: list[Inline] = []
    for node in children:
        if isinstance(node, Style) and node.kind == "strong":
            out += _unbold(node.children)
        elif isinstance(node, Style):
            out.append(Style(node.kind, _unbold(node.children)))
        else:
            out.append(node)
    return out


def _squash(text: str) -> str:
    text = text.replace(" ", " ").replace("​", "")
    text = re.sub(r"\s+", " ", text).strip()
    # A style boundary may fall on either side of a space.
    return re.sub(r" ?([\x01\x03]) ?", r"\1", text)


def _fp_text(block: Block) -> str:
    if isinstance(block, Heading):
        return f"H{block.level}:{_squash(_flat(block.children))}"
    if isinstance(block, Para):
        return "P:" + _squash(_flat(block.children))
    if isinstance(block, ListBlock):
        return f"L{int(block.ordered)}:" + "||".join("|".join(_fp_text(b) for b in item) for item in block.items)
    if isinstance(block, Quote):
        return "Q:" + "|".join(_fp_text(b) for b in block.blocks)
    if isinstance(block, Code):
        return "C:" + block.text.strip()
    if isinstance(block, Equation):
        return f"E:{block.sig or block.latex}:{','.join(x or '' for x in block.row_labels)}"
    if isinstance(block, Figure):
        return f"F:{_squash(_flat(block.caption))}:{block.label}"
    if isinstance(block, Table):
        # A header row's bold is how it looks in Word, not emphasis.
        cells = "#".join("&".join(_squash(_flat(_unbold(c.children) if r < block.header_rows else c.children)) for c in row) for r, row in enumerate(block.rows))
        return f"T:{_squash(_flat(block.caption))}:{block.label}:{cells}"
    if isinstance(block, Raw):
        return "R:" + block.tex.strip()
    return type(block).__name__


def fingerprint(block: Block) -> str:
    """A short hash of what a reader can see of ``block``."""
    return hashlib.sha256(_fp_text(block).encode("utf-8")).hexdigest()[:20]


def merge_text(inlines: list[Inline]) -> list[Inline]:
    """Join adjacent text nodes and adjacent runs of the same style, and drop empty ones."""
    out: list[Inline] = []
    for node in inlines:
        if isinstance(node, Style):
            node = Style(node.kind, merge_text(node.children))
            if not node.children:
                continue
            if out and isinstance(out[-1], Style) and out[-1].kind == node.kind:
                out[-1] = Style(node.kind, merge_text(out[-1].children + node.children))
                continue
        elif isinstance(node, Text):
            if not node.text:
                continue
            if out and isinstance(out[-1], Text):
                out[-1] = Text(out[-1].text + node.text)
                continue
        out.append(node)
    return out
