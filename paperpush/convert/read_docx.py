"""Read a Word (.docx) manuscript into the document model.

Two kinds of document come through here. One paperpush wrote carries field
codes and a manifest, and reads back exactly. One a person typed has none of
that, so structure is recovered from what Word does record (styles, list
numbering, Zotero/Mendeley/EndNote fields, native captions and cross-references)
and, failing that, from conventions: a bold line that is a heading, "Figure 2."
at the start of a caption, "[3]" or "(Smith, 2020)" against the reference list.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import xml.etree.ElementTree as ET  # nosec B405 -- input is screened for DOCTYPE by _parse
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from . import bib as biblib
from .mathconv import M_NS, omml_rows_to_latex, omml_to_latex, signature
from .model import Author, BibEntry, Bibliography, Break, Cell, Cite, Code, Comment, Document, Equation, Figure, Heading, Image, Link, ListBlock, Log, Math, Note, Para, Quote, Raw, Ref, Style, Table, Text, all_blocks, fingerprint, merge_text, plain_text
from .write_docx import MANIFEST_NS

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
V = "urn:schemas-microsoft-com:vml"
O = "urn:schemas-microsoft-com:office:office"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
_MONO = re.compile(r"(?i)courier|consolas|menlo|monaco|mono\b|lucida console")
_FIG_CAPTION = re.compile(r"^\s*((?:Supplementary\s+|Extended\s+Data\s+)?(?:Figure|Fig\.?))\s*(S?\d+)\s*[.:|–—-]?\s*", re.I)
_TAB_CAPTION = re.compile(r"^\s*((?:Supplementary\s+|Extended\s+Data\s+)?Table)\s*(S?\d+)\s*[.:|–—-]?\s*", re.I)
_REF_HEADING = re.compile(r"(?i)^\s*(?:\d+[.)]?\s+)?(references?|bibliography|literature cited|works cited)\s*:?\s*$")
_ABSTRACT = re.compile(r"(?i)^\s*(abstract|summary)\s*[.:—-]?\s*$")
_ABSTRACT_RUNIN = re.compile(r"(?i)^\s*abstract\s*[.:—-]\s*")
_KEYWORDS = re.compile(r"(?i)^\s*key\s?words?\s*[.:—-]\s*")
_NUMBER_PREFIX = re.compile(r"^\s*(\d+(?:\.\d+)*)[.)]?\s+(?=\S)")
_ENTRY_NUMBER = re.compile(r"^\s*(?:\[(\d+)\]|(\d+)[.)])\s*")
_NUMERIC_CITE = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]")
_AUTHOR_YEAR_GROUP = re.compile(r"\(((?:[^()]*?\b(?:1[89]|20)\d\d[a-z]?)(?:\s*;[^()]*?\b(?:1[89]|20)\d\d[a-z]?)*)\)")
_NARRATIVE = re.compile(r"\b([A-Z][\w'’-]+(?:\s+(?:et al\.?|and\s+[A-Z][\w'’-]+|&\s+[A-Z][\w'’-]+))?)\s+\(((?:1[89]|20)\d\d[a-z]?)\)")
_TYPED_REF = re.compile(r"\b(Figures?|Figs?\.?|Tables?|Tabs?\.?|Equations?|Eqs?\.?)(\s|\u00a0)+(\(?)(\d+)(\)?)")
_DANGEROUS = re.compile(r"\\(?:write18|immediate|openout|openin|read\b|directlua|latelua|catcode|ShellEscape|input\s*\{?\s*[/~|])|\\include\s*\{?\s*[/~]|\|\s*\"")


def w(name: str) -> str:
    return f"{{{W}}}{name}"


def _m(name: str) -> str:
    return f"{{{M_NS}}}{name}"


def _local(el) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _ns(el) -> str:
    return el.tag[1:].split("}", 1)[0] if isinstance(el.tag, str) and el.tag.startswith("{") else ""


def _parse(data: bytes):
    """Parse a package part. Word never writes a DOCTYPE, so one means a crafted file."""
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:4096]:
        raise ValueError("the document contains an XML DOCTYPE, which Word never writes; refusing to read it")
    return ET.fromstring(data)  # nosec B314 -- DOCTYPE refused above


def _on(el) -> bool:
    if el is None:
        return False
    return el.get(w("val"), "1").lower() not in ("0", "false", "off", "none")


@dataclass
class Item:
    """One body paragraph or table, before blocks are assembled."""

    kind: str  # p, tbl
    style: str = ""
    children: list = field(default_factory=list)
    images: list[Image] = field(default_factory=list)
    display_math: list = field(default_factory=list)  # <m:oMath> elements shown on their own line
    bookmarks: list[str] = field(default_factory=list)
    num: tuple[int, int] | None = None  # (numId, ilvl)
    bibkey: str = ""
    has_seq: str = ""
    bold_all: bool = False
    size: int = 0
    table: object = None
    centered: bool = False
    bib_marker: bool = False
    used: bool = False
    raw_token: str = ""


class DocxReader:
    def __init__(self, path: Path, log: Log, images_dir: Path, *, tracked: str = "accept", use_manifest: bool = True) -> None:
        self.path = path
        self.log = log
        self.images_dir = images_dir
        self.tracked = tracked
        self.use_manifest = use_manifest
        self.styles: dict[str, dict] = {}
        self.numbering: dict[int, dict[int, str]] = {}
        self.rels: dict[str, tuple[str, str]] = {}
        self.footnotes: dict[str, object] = {}
        self.endnotes: dict[str, object] = {}
        self.comments: dict[str, tuple[str, str]] = {}
        self.zip: zipfile.ZipFile | None = None
        self.manifest: dict = {}
        self.csl_keys: dict[str, str] = {}
        self.bib: dict[str, BibEntry] = {}
        self.bib_field = False
        self.image_n = 0
        self.tally: dict[str, int] = {}
        self.bookmark_label: dict[str, str] = {}
        self.styled_headings = False
        self.free_labels: dict[str, str] = {}
        self.note_rels: dict[str, dict[str, tuple[str, str]]] = {}

    # -- the package -----------------------------------------------------------

    def read(self) -> Document:
        try:
            self.zip = zipfile.ZipFile(self.path)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"{self.path.name} is not a valid Word file") from exc
        with self.zip:
            if sum(i.file_size for i in self.zip.infolist()) > MAX_PACKAGE_BYTES:
                raise ValueError(f"{self.path.name} unpacks to more than 1 GB; refusing to read it")
            names = set(self.zip.namelist())
            if "word/document.xml" not in names:
                raise ValueError(f"{self.path.name} has no word/document.xml")
            self.load_parts(names)
            root = _parse(self.zip.read("word/document.xml"))
            body = root.find(w("body"))
            if body is None:
                raise ValueError(f"{self.path.name} has no document body")
            items = self.body_items(body)
            doc = self.assemble(items)
        self.report_tally()
        return doc

    def load_parts(self, names: set[str]) -> None:
        if "word/styles.xml" in names:
            self.load_styles(_parse(self.zip.read("word/styles.xml")))
        if "word/numbering.xml" in names:
            self.load_numbering(_parse(self.zip.read("word/numbering.xml")))
        if "word/_rels/document.xml.rels" in names:
            for rel in _parse(self.zip.read("word/_rels/document.xml.rels")):
                self.rels[rel.get("Id")] = (rel.get("Type", "").rsplit("/", 1)[-1], rel.get("Target", ""))
        for part, store, tag in (("word/footnotes.xml", self.footnotes, "footnote"), ("word/endnotes.xml", self.endnotes, "endnote")):
            if part in names:
                for note in _parse(self.zip.read(part)).findall(w(tag)):
                    if note.get(w("type")) in (None, "normal"):
                        store[note.get(w("id"))] = note
        for part, key in (("word/_rels/footnotes.xml.rels", "footnote"), ("word/_rels/endnotes.xml.rels", "endnote")):
            if part in names:
                self.note_rels[key] = {rel.get("Id"): (rel.get("Type", "").rsplit("/", 1)[-1], rel.get("Target", "")) for rel in _parse(self.zip.read(part))}
        if "word/comments.xml" in names:
            for comment in _parse(self.zip.read("word/comments.xml")).findall(w("comment")):
                text = " ".join("".join(t.text or "" for t in p.iter(w("t"))) for p in comment.iter(w("p")))
                self.comments[comment.get(w("id"))] = (comment.get(w("author"), "unknown"), text.strip())
        if self.use_manifest:
            self.load_manifest(names)

    def load_manifest(self, names: set[str]) -> None:
        for name in sorted(names):
            if not re.fullmatch(r"customXml/item\d+\.xml", name):
                continue
            data = self.zip.read(name)
            if MANIFEST_NS.encode() not in data:
                continue
            try:
                root = _parse(data)
                decoded = zlib.decompressobj().decompress(base64.b64decode(root.text or ""), 64 * 1024 * 1024)
                manifest = json.loads(decoded.decode("utf-8"))
            except (ValueError, binascii.Error, zlib.error, ET.ParseError):
                self.log.warn("the embedded paperpush manifest could not be read, so the original LaTeX was not restored")
                return
            if self.unsafe(manifest):
                self.log.warn("the embedded paperpush manifest contains LaTeX that can run commands or read files, so it was ignored. Check where this file came from")
                return
            self.manifest = manifest
            return

    def unsafe(self, manifest: dict) -> bool:
        strings: list[str] = [str(manifest.get("preamble", "")), str(manifest.get("front", ""))]
        for sources in (manifest.get("blocks") or {}).values():
            strings += [str(s) for s in sources]
        strings += [str(s) for s in (manifest.get("raw") or {}).values()]
        strings += [str(s) for s in (manifest.get("math") or {}).values()]
        return any(_DANGEROUS.search(s) for s in strings)

    def load_styles(self, root) -> None:
        for style in root.findall(w("style")):
            sid = style.get(w("styleId"))
            name = style.find(w("name"))
            based = style.find(w("basedOn"))
            ppr = style.find(w("pPr"))
            rpr = style.find(w("rPr"))
            outline = ppr.find(w("outlineLvl")) if ppr is not None else None
            num = ppr.find(w("numPr")) if ppr is not None else None
            size = rpr.find(w("sz")) if rpr is not None else None
            self.styles[sid] = {
                "name": (name.get(w("val")) if name is not None else sid or "").lower(),
                "based": based.get(w("val")) if based is not None else None,
                "outline": int(outline.get(w("val"), "9")) if outline is not None else None,
                "num": num,
                "bold": _on(rpr.find(w("b"))) if rpr is not None and rpr.find(w("b")) is not None else None,
                "italic": _on(rpr.find(w("i"))) if rpr is not None and rpr.find(w("i")) is not None else None,
                "size": int(size.get(w("val"), "0")) if size is not None else 0,
                "font": (rpr.find(w("rFonts")).get(w("ascii"), "") if rpr is not None and rpr.find(w("rFonts")) is not None else ""),
            }

    def load_numbering(self, root) -> None:
        abstracts: dict[str, dict[int, str]] = {}
        for abstract in root.findall(w("abstractNum")):
            levels = {}
            for lvl in abstract.findall(w("lvl")):
                fmt = lvl.find(w("numFmt"))
                levels[int(lvl.get(w("ilvl"), "0"))] = fmt.get(w("val"), "decimal") if fmt is not None else "decimal"
            abstracts[abstract.get(w("abstractNumId"))] = levels
        for num in root.findall(w("num")):
            ref = num.find(w("abstractNumId"))
            if ref is not None:
                self.numbering[int(num.get(w("numId")))] = abstracts.get(ref.get(w("val")), {})

    def style_chain(self, sid: str):
        seen = set()
        while sid and sid in self.styles and sid not in seen:
            seen.add(sid)
            yield self.styles[sid]
            sid = self.styles[sid]["based"]

    def count(self, what: str, n: int = 1) -> None:
        self.tally[what] = self.tally.get(what, 0) + n

    def report_tally(self) -> None:
        verb = "accepted" if self.tracked == "accept" else "rejected"
        n = self.tally.get("tracked", 0)
        if n:
            self.log.warn(f"{n} tracked change(s) were {verb} in the output. Use --tracked-changes to choose; the Word file still holds them")
        n = self.tally.get("comment", 0)
        if n:
            self.log.warn(f"{n} Word comment(s) were carried over as LaTeX comments, which do not print")
        n = self.tally.get("hidden", 0)
        if n:
            self.log.warn(f"{n} run(s) of hidden text were left out. Check that nothing there was meant to stay")
        n = self.tally.get("ole_equation", 0)
        if n:
            self.log.warn(f"{n} MathType/Equation Editor object(s) cannot be read and appear as '[equation]'. Convert them to Word equations first (in MathType: Convert Equations)")
        n = self.tally.get("chart", 0)
        if n:
            self.log.warn(f"{n} native Word chart(s) or SmartArt graphic(s) cannot be converted: export each as an image and insert that")
        n = self.tally.get("textbox", 0)
        if n:
            self.log.warn(f"{n} text box(es) were moved into the main text; check their position")
        n = self.tally.get("highlight", 0)
        if n:
            self.log.change(f"removed highlighting and font color from {n} run(s)")

    # -- paragraphs ------------------------------------------------------------

    def body_items(self, parent) -> list[Item]:
        out: list[Item] = []
        for child in parent:
            tag = _local(child)
            if _ns(child) != W:
                continue
            if tag == "p":
                out += self.paragraph(child)
            elif tag == "tbl":
                out.append(Item(kind="tbl", table=child))
            elif tag == "sdt":
                content = child.find(w("sdtContent"))
                if content is not None:
                    out += self.body_items(content)
            elif tag in ("ins", "moveTo"):
                if self.tracked == "accept":
                    out += self.body_items(child)
                self.count("tracked")
            elif tag in ("del", "moveFrom"):
                if self.tracked == "reject":
                    out += self.body_items(child)
                self.count("tracked")
        return out

    def style_of(self, p) -> tuple[str, str]:
        ppr = p.find(w("pPr"))
        sid = ""
        if ppr is not None and ppr.find(w("pStyle")) is not None:
            sid = ppr.find(w("pStyle")).get(w("val"), "")
        name = self.styles.get(sid, {}).get("name", sid.lower())
        return sid, name

    def paragraph(self, p) -> list[Item]:
        ppr = p.find(w("pPr"))
        sid, name = self.style_of(p)
        item = Item(kind="p", style=name)
        extra: list[Item] = []
        state = _State(item=item, extra=extra)
        self.walk(p, state, ())
        state.finish()
        item.children = merge_text(state.out)
        if ppr is not None:
            numpr = ppr.find(w("numPr"))
            if numpr is not None:
                nid, ilvl = numpr.find(w("numId")), numpr.find(w("ilvl"))
                item.num = (int(nid.get(w("val"), "0")) if nid is not None else 0, int(ilvl.get(w("val"), "0")) if ilvl is not None else 0)
            jc = ppr.find(w("jc"))
            item.centered = jc is not None and jc.get(w("val")) == "center"
        if item.num is None:
            for style in self.style_chain(sid):
                if style["num"] is not None:
                    nid, ilvl = style["num"].find(w("numId")), style["num"].find(w("ilvl"))
                    if nid is not None:
                        item.num = (int(nid.get(w("val"), "0")), int(ilvl.get(w("val"), "0")) if ilvl is not None else 0)
                    break
        item.bold_all = state.text_chars > 0 and state.bold_chars == state.text_chars
        item.size = state.max_size or next((s["size"] for s in self.style_chain(sid) if s["size"]), 0)
        return [item] + extra

    def walk(self, parent, state: "_State", styles: tuple) -> None:
        for child in parent:
            tag, ns = _local(child), _ns(child)
            if ns == M_NS:
                if tag == "oMathPara":
                    for math in child.findall(_m("oMath")):
                        state.item.display_math.append(math)
                elif tag == "oMath":
                    latex = omml_to_latex(child)
                    state.emit(Math(latex=self.restore_math(child, latex), sig=signature(child)))
                continue
            if ns == MC and tag == "AlternateContent":
                choice = child.find(f"{{{MC}}}Choice")
                if choice is not None:
                    self.walk(choice, state, styles)
                continue
            if ns != W:
                continue
            if tag == "r":
                self.run(child, state, styles)
            elif tag == "hyperlink":
                self.hyperlink(child, state, styles)
            elif tag == "fldSimple":
                self.simple_field(child, state, styles)
            elif tag in ("ins", "moveTo"):
                self.count("tracked")
                if self.tracked == "accept":
                    self.walk(child, state, styles)
            elif tag in ("del", "moveFrom"):
                self.count("tracked")
                if self.tracked == "reject":
                    self.walk(child, state, styles)
            elif tag in ("smartTag", "customXml"):
                self.walk(child, state, styles)
            elif tag == "sdt":
                content = child.find(w("sdtContent"))
                if content is not None:
                    self.walk(content, state, styles)
            elif tag == "bookmarkStart":
                name = child.get(w("name"), "")
                if name and name != "_GoBack":
                    state.item.bookmarks.append(name)

    def run_styles(self, r) -> tuple[tuple, bool, int]:
        rpr = r.find(w("rPr"))
        kinds: list[str] = []
        if rpr is None:
            return (), False, 0
        sid_el = rpr.find(w("rStyle"))
        sid = sid_el.get(w("val"), "") if sid_el is not None else ""
        chain = list(self.style_chain(sid))
        name = chain[0]["name"] if chain else ""

        def flag(tag: str, key: str) -> bool:
            el = rpr.find(w(tag))
            if el is not None:
                return _on(el)
            return next((s[key] for s in chain if s[key] is not None), False)

        bold = flag("b", "bold") or name == "strong"
        if bold:
            kinds.append("strong")
        if flag("i", "italic") or name == "emphasis":
            kinds.append("emph")
        underline = rpr.find(w("u"))
        if underline is not None and underline.get(w("val"), "single") != "none" and name != "hyperlink":
            kinds.append("underline")
        if _on(rpr.find(w("smallCaps"))):
            kinds.append("smallcaps")
        va = rpr.find(w("vertAlign"))
        if va is not None and va.get(w("val")) == "superscript":
            kinds.append("sup")
        if va is not None and va.get(w("val")) == "subscript":
            kinds.append("sub")
        if name == "footnote reference":
            kinds = [k for k in kinds if k != "sup"]
        fonts = rpr.find(w("rFonts"))
        font = fonts.get(w("ascii"), "") if fonts is not None else next((s["font"] for s in chain if s["font"]), "")
        if _MONO.search(font or "") or name in ("verbatim char", "source code", "html code"):
            kinds.append("code")
        if rpr.find(w("highlight")) is not None or (rpr.find(w("color")) is not None and rpr.find(w("color")).get(w("val"), "auto") not in ("auto", "000000") and name != "hyperlink"):
            self.count("highlight")
        size = rpr.find(w("sz"))
        return tuple(kinds), bold, int(size.get(w("val"), "0")) if size is not None else 0

    def run(self, r, state: "_State", outer: tuple) -> None:
        rpr = r.find(w("rPr"))
        if rpr is not None and _on(rpr.find(w("vanish"))):
            if not state.in_instr():
                self.count("hidden")
            return
        kinds, bold, size = self.run_styles(r)
        kinds = tuple(dict.fromkeys(outer + kinds))
        for child in r:
            tag, ns = _local(child), _ns(child)
            if ns == M_NS and tag == "oMath":
                state.emit(Math(latex=self.restore_math(child, omml_to_latex(child)), sig=signature(child)))
                continue
            if ns == MC and tag == "AlternateContent":
                choice = child.find(f"{{{MC}}}Choice")
                if choice is not None:
                    self.objects(choice, state)
                continue
            if ns != W:
                continue
            if tag == "t":
                state.text(child.text or "", kinds, bold, size)
            elif tag == "delText" and self.tracked == "reject":
                state.text(child.text or "", kinds, bold, size)
            elif tag == "tab":
                state.text("\t", kinds, False, 0)
            elif tag in ("br", "cr"):
                if child.get(w("type")) not in ("page", "column"):
                    state.emit(Break())
            elif tag == "noBreakHyphen":
                state.text("\u2011", kinds, bold, size)
            elif tag == "softHyphen":
                pass
            elif tag == "sym":
                state.text(_symbol(child), kinds, bold, size)
            elif tag == "fldChar":
                self.field_char(child, state)
            elif tag == "instrText":
                state.instr(child.text or "")
            elif tag == "footnoteReference":
                state.emit(self.note(self.footnotes, child.get(w("id"))))
            elif tag == "endnoteReference":
                state.emit(self.note(self.endnotes, child.get(w("id"))))
            elif tag == "commentReference":
                author, text = self.comments.get(child.get(w("id")), ("unknown", ""))
                if text:
                    self.count("comment")
                    state.emit(Comment(author=author, text=text))
            elif tag in ("drawing", "pict", "object"):
                self.objects(child, state)

    def objects(self, el, state: "_State") -> None:
        """Pictures, text boxes, and embedded objects anywhere under ``el``."""
        ole = next((e for e in el.iter(f"{{{O}}}OLEObject")), None)
        if ole is not None and "equation" in ole.get("ProgID", "").lower():
            self.count("ole_equation")
            state.emit(Text("[equation]"))
            return
        for e in el.iter():
            tag = _local(e)
            if tag == "txbxContent" and _ns(e) == W:
                self.count("textbox")
                state.extra.extend(self.body_items(e))
                return
        found = False
        for blip in el.iter(f"{{{A}}}blip"):
            rid = blip.get(f"{{{R}}}embed")
            if rid:
                self.add_image(rid, state.item)
                found = True
        for data in el.iter(f"{{{V}}}imagedata"):
            rid = data.get(f"{{{R}}}id")
            if rid and not found:
                self.add_image(rid, state.item)
                found = True
        if not found and any(_local(e) in ("chart", "relIds") for e in el.iter()):
            self.count("chart")

    def add_image(self, rid: str, item: Item) -> None:
        kind, target = self.rels.get(rid, ("", ""))
        if kind != "image" or not target:
            return
        name = "word/" + target.lstrip("/") if not target.startswith("/word/") else target.lstrip("/")
        name = re.sub(r"word/\.\./", "", name)
        try:
            data = self.zip.read(name)
        except KeyError:
            return
        self.image_n += 1
        suffix = Path(target).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
            suffix = ".png"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        path = self.images_dir / f"figure{self.image_n}{suffix}"
        path.write_bytes(data)
        import hashlib

        item.images.append(Image(path=str(path), original=hashlib.sha256(data).hexdigest()[:20]))

    def note(self, store: dict, nid: str):
        note = store.get(nid)
        if note is None:
            return Text("")
        blocks = []
        document_rels = self.rels
        self.rels = self.note_rels.get("footnote" if store is self.footnotes else "endnote", {})
        for p in note.findall(w("p")):
            item = self.paragraph(p)[0]
            children = _strip_note_lead(item.children)
            if children:
                blocks.append(Para(children=children))
        self.rels = document_rels
        return Note(blocks=blocks)

    def hyperlink(self, el, state: "_State", styles: tuple) -> None:
        rid = el.get(f"{{{R}}}id")
        inner = _State(item=state.item, extra=state.extra)
        self.walk(el, inner, styles)
        inner.finish()
        children = [_unlink(c) for c in merge_text(inner.out)]
        url = self.rels.get(rid, ("", ""))[1] if rid else ""
        if url:
            state.emit(Link(url=url, children=children))
        else:
            state.emit_many(children)

    def simple_field(self, el, state: "_State", styles: tuple) -> None:
        instr = el.get(w("instr"), "")
        inner = _State(item=state.item, extra=state.extra)
        self.walk(el, inner, styles)
        inner.finish()
        state.emit_many(self.field_result(instr, merge_text(inner.out), state.item))

    def field_char(self, el, state: "_State") -> None:
        kind = el.get(w("fldCharType"))
        if kind == "begin":
            state.stack.append({"instr": "", "result": [], "phase": "instr", "saved": state.out})
            state.out = []
        elif kind == "separate" and state.stack:
            state.stack[-1]["phase"] = "result"
            state.out = []
        elif kind == "end" and state.stack:
            frame = state.stack.pop()
            result = merge_text(state.out)
            state.out = frame["saved"]
            state.emit_many(self.field_result(frame["instr"], result, state.item))

    def field_result(self, instr: str, result: list, item: Item) -> list:
        code = instr.strip()
        upper = code.upper()
        if upper.startswith("ADDIN PAPERPUSH_CITE"):
            try:
                data = json.loads(code.split(None, 2)[2])
                return [Cite(keys=list(data["keys"]), mode=data.get("mode", "paren"), cmd=data.get("cmd", ""), pre=data.get("pre", ""), post=data.get("post", ""))]
            except (IndexError, KeyError, ValueError):
                return result
        if upper.startswith("ADDIN PAPERPUSH_BIBITEM"):
            try:
                item.bibkey = json.loads(code.split(None, 2)[2])["key"]
            except (IndexError, KeyError, ValueError):
                pass
            return []
        if upper.startswith("ADDIN ZOTERO_ITEM") or upper.startswith("ADDIN CSL_CITATION"):
            cite = self.csl_citation(code)
            return [cite] if cite is not None else result
        if upper.startswith("ADDIN ZOTERO_BIBL") or "CSL_BIBLIOGRAPHY" in upper or upper.startswith("ADDIN EN.REFLIST"):
            item.bib_marker = True
            self.bib_field = True
            return result
        if upper.startswith("ADDIN EN.CITE") and not upper.startswith("ADDIN EN.CITE.DATA"):
            cite = self.endnote_citation(code)
            return [cite] if cite is not None else result
        if upper.startswith("ADDIN PAPERPUSH_RAW"):
            try:
                item.raw_token = json.loads(code.split(None, 2)[2])["token"]
            except (IndexError, KeyError, ValueError):
                pass
            return []
        if upper.startswith("ADDIN PAPERPUSH_REF"):
            try:
                data = json.loads(code.split(None, 2)[2])
                shown = plain_text(result).strip()
                if shown != "??":  # a reference that was dangling in the source stays dangling
                    self.free_labels[data["label"]] = shown
                return [Ref(label=data["label"], kind=data.get("kind", "ref"))]
            except (IndexError, KeyError, ValueError):
                return result
        m = re.match(r"(REF|PAGEREF|NOTEREF)\s+(\S+)(.*)", code, re.I)
        if m:
            return self.ref_field(m.group(1).upper(), m.group(2), result)
        m = re.match(r"SEQ\s+(\S+)", code, re.I)
        if m:
            item.has_seq = m.group(1)
            return result
        m = re.match(r'HYPERLINK\s+"([^"]+)"', code, re.I)
        if m:
            return [Link(url=m.group(1), children=[_unlink(c) for c in result])]
        if upper.startswith(("TOC", "INDEX", "TA ")):
            self.log.change("dropped the table of contents field")
            return []
        return result

    def ref_field(self, kind: str, bookmark: str, result: list) -> list:
        text = plain_text(result).strip()
        ref = Ref(label=f"\x00{bookmark}", kind="page" if kind == "PAGEREF" else "ref")
        m = re.match(r"^(.*?)(\(?)((?:[A-Z]|[A-Z]?\d+)(?:\.\d+)*)(\)?)\s*$", text, re.S)
        if m and m.group(1).strip():
            return [Text(m.group(1)), ref]
        return [ref]

    def csl_citation(self, code: str):
        start = code.find("{")
        if start < 0:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(code[start:])
        except ValueError:
            return None
        keys = []
        for cite in data.get("citationItems", []):
            item = cite.get("itemData") or {}
            ident = str((cite.get("uris") or cite.get("uri") or [item.get("id", "")])[0] if (cite.get("uris") or cite.get("uri")) else item.get("id", ""))
            if ident not in self.csl_keys:
                entry = biblib.from_csl(item, self.bib)
                self.bib[entry.key] = entry
                self.csl_keys[ident] = entry.key
            keys.append(self.csl_keys[ident])
        return Cite(keys=keys) if keys else None

    def endnote_citation(self, code: str):
        keys = []
        for cite in re.findall(r"<Cite[ >].*?</Cite>", code, re.S):
            record = re.search(r"<rec-number>(\d+)</rec-number>", cite) or re.search(r"<RecNum>(\d+)</RecNum>", cite)
            ident = "en:" + (record.group(1) if record else cite[:60])
            if ident not in self.csl_keys:
                entry = _endnote_entry(cite, self.bib)
                if entry is None:
                    continue
                self.bib[entry.key] = entry
                self.csl_keys[ident] = entry.key
            keys.append(self.csl_keys[ident])
        return Cite(keys=keys) if keys else None

    def restore_math(self, el, latex: str) -> str:
        return (self.manifest.get("math") or {}).get(signature(el), latex)

    # -- tables ----------------------------------------------------------------

    def read_table(self, el) -> Table | list:
        rows: list[list[Cell]] = []
        header = 0
        open_cells: dict[int, Cell] = {}
        images: list[Image] = []
        math_cells: list = []
        number_cells: list[Item] = []
        for tr in el.findall(w("tr")):
            trpr = tr.find(w("trPr"))
            if trpr is not None and trpr.find(w("tblHeader")) is not None and len(rows) == header:
                header += 1
            row: list[Cell] = []
            col = 0
            for tc in _cells(tr):
                tcpr = tc.find(w("tcPr"))
                span = 1
                merge = None
                if tcpr is not None:
                    gs = tcpr.find(w("gridSpan"))
                    span = int(gs.get(w("val"), "1")) if gs is not None else 1
                    vm = tcpr.find(w("vMerge"))
                    if vm is not None:
                        merge = vm.get(w("val"), "continue")
                if merge == "continue":
                    if col in open_cells:
                        open_cells[col].rowspan += 1
                    col += span
                    continue
                items = self.body_items(tc)
                children: list = []
                align = ""
                for n, it in enumerate(items):
                    images += it.images
                    if it.display_math:
                        math_cells += it.display_math
                    elif re.fullmatch(r"\s*\(\s*\w+\s*\)\s*", plain_text(it.children) or "x") and plain_text(it.children).strip():
                        number_cells.append(it)
                    if n and children:
                        children.append(Break())
                    children += it.children
                    for math in it.display_math:
                        children.append(Math(latex=self.restore_math(math, omml_to_latex(math)), sig=signature(math)))
                cell = Cell(children=_strip_bold(merge_text(children), header > len(rows)), colspan=span, align=_cell_align(tc))
                if merge == "restart":
                    open_cells[col] = cell
                else:
                    open_cells.pop(col, None)
                row.append(cell)
                col += span
            rows.append(row)
        if math_cells and len(rows) == 1 and len(math_cells) == 1:
            return self.equation_from(math_cells[0], number_cells)
        if images and all(not plain_text(c.children).strip() for r in rows for c in r):
            self.log.warn("a layout table holding images was read as one figure; check that its panels are in order")
            return Figure(images=images, numbered=False)
        return Table(rows=rows, header_rows=header or (1 if len(rows) > 1 else 0), aligns=[])

    def equation_from(self, math, number_cells: list[Item]) -> Equation:
        body, has_rows = omml_rows_to_latex(math)
        sig = signature(math)
        restored = (self.manifest.get("math") or {}).get(sig)
        labels = []
        for it in number_cells:
            name = next((b for b in it.bookmarks if b), "")
            label = self.bookmark_label.get(name, "")
            if not label and not self.manifest:  # a file of ours labels what its source labelled
                number = re.search(r"\d+", plain_text(it.children))
                label = f"\x01eq:{number.group(0)}" if number else ""
                if name and label:
                    self.bookmark_label[name] = label
            labels.append(label)
        env = "align" if has_rows and len(labels) > 1 else "equation"
        if has_rows and len(labels) <= 1 and not restored:
            body = "\\begin{aligned}\n" + body + "\n\\end{aligned}"
        return Equation(latex=restored or body, env=env, row_labels=labels, numbered=bool(labels), sig=sig)

    # -- assembly --------------------------------------------------------------

    def assemble(self, items: list[Item]) -> Document:
        doc = Document()
        self.styled_headings = any(self.is_heading_style(it) for it in items)
        self.bookmark_label = dict(self.manifest.get("labels") or {})
        self.front_matter(doc, items)
        self.find_references(doc, items)
        doc.blocks = self.build_blocks([it for it in items if not it.used])
        self.finish_labels(doc)
        self.typed_citations(doc)
        self.typed_refs(doc)
        doc.bib.update(self.bib)
        doc.extra_labels = self.free_labels
        if self.manifest:
            self.apply_manifest(doc)
        if doc.bib and not any(isinstance(b, Bibliography) for b in doc.blocks):
            doc.blocks.append(Bibliography())
        doc.numbered_sections = any(isinstance(b, Heading) and b.numbered for b in doc.blocks)
        return doc

    def apply_manifest(self, doc: Document) -> None:
        doc.manifest = self.manifest
        for key, value in (self.manifest.get("bib") or {}).items():
            if key not in doc.bib or not doc.bib[key].fields:
                doc.bib[key] = BibEntry(key=key, kind=value.get("kind", "misc"), fields=value.get("fields") or {}, text=value.get("text", ""))
        doc.citation_style = self.manifest.get("citation_style", doc.citation_style)
        doc.preamble = self.manifest.get("preamble", "")
        doc.bib_source = self.manifest.get("bib_source") or {}

    def text_of(self, item: Item) -> str:
        return plain_text(item.children).strip()

    def is_heading_style(self, item: Item) -> int:
        m = re.fullmatch(r"heading\s*(\d)", item.style)
        return int(m.group(1)) if m else 0

    def front_matter(self, doc: Document, items: list[Item]) -> None:
        paragraphs = [it for it in items if it.kind == "p"]
        first_body = next((n for n, it in enumerate(items) if self.is_heading_style(it) and not _ABSTRACT.match(self.text_of(it))), len(items))
        title = next((it for it in items[:first_body] if it.kind == "p" and it.style == "title"), None)
        if title is None and paragraphs:
            candidate = next((it for it in items[:first_body] if it.kind == "p" and self.text_of(it)), None)
            if candidate is not None and not _ABSTRACT.match(self.text_of(candidate)) and not self.is_heading_style(candidate) and not candidate.images and not candidate.display_math and (candidate.bold_all or candidate.size >= 28 or candidate.centered) and len(self.text_of(candidate).split()) <= 60:
                title = candidate
                self.log.change("took the first bold or large line as the title")
        if title is not None:
            doc.title = _strip_bold(title.children, True)
            title.used = True
        start = items.index(title) + 1 if title is not None else 0
        abstract_at = next((n for n in range(start, min(first_body + 1, len(items))) if items[n].kind == "p" and (items[n].style in ("abstract heading", "abstract") or _ABSTRACT.match(self.text_of(items[n])) or _ABSTRACT_RUNIN.match(self.text_of(items[n])))), None)
        end_front = abstract_at if abstract_at is not None else first_body
        self.authors(doc, items[start:end_front], title is not None)
        if abstract_at is not None:
            self.abstract(doc, items, abstract_at, first_body)
        for it in items[start : first_body + 1 if first_body < len(items) else len(items)]:
            if it.kind == "p" and not it.used and (_KEYWORDS.match(self.text_of(it)) or it.style == "keywords"):
                text = _KEYWORDS.sub("", self.text_of(it))
                doc.keywords = [k.strip().rstrip(".") for k in re.split(r"[,;·•]\s*", text) if k.strip()]
                it.used = True

    def authors(self, doc: Document, items: list[Item], have_title: bool) -> None:
        if not have_title:
            return
        for it in items:
            if it.kind != "p" or it.used or it.images or it.display_math:
                continue
            text = self.text_of(it)
            if not text:
                if it.children:  # a line of only a citation or an equation is body text
                    break
                it.used = True
                continue
            if _KEYWORDS.match(text):
                continue
            if it.style == "author" or (not doc.authors and it.style != "affiliation" and _looks_like_authors(text)):
                doc.authors += _parse_authors(it.children)
                it.used = True
            elif it.style == "affiliation" or _looks_like_affiliation(text) or re.match(r"(?i)^\s*(correspond|\*)", text):
                self.affiliation(doc, it, text)
                it.used = True
            else:
                break  # the title block ends at the first line that is neither

    def affiliation(self, doc: Document, item: Item, text: str) -> None:
        m = re.match(r"(?i)^\s*\*?\s*correspond\w*(?:\s+authors?)?\s*:?\s*(.*)$", text)
        if m:
            found = re.findall(r"(?:([^;:,]+?)\s*:\s*)?([\w.+-]+@[\w.-]+\w)", m.group(1))
            for name, email in found:
                author = next((a for a in doc.authors if a.name == name.strip()), None)
                if author is None and len(doc.authors) >= 1 and not name:
                    author = next((a for a in doc.authors if a.corresponding), doc.authors[0])
                if author is not None:
                    author.email = email
            return
        cleaned = re.sub(r"^\s*(\d+|[a-z])\s*[.)]?\s*", "", text).strip()
        if cleaned:
            doc.affiliations.append(cleaned)

    def abstract(self, doc: Document, items: list[Item], at: int, first_body: int) -> None:
        heading = items[at]
        text = self.text_of(heading)
        n = at
        if _ABSTRACT.match(text) or heading.style == "abstract heading":
            heading.used = True
            n = at + 1
        elif _ABSTRACT_RUNIN.match(text):
            heading.children = _strip_prefix(heading.children, _ABSTRACT_RUNIN)
        while n < len(items) and n < first_body:
            it = items[n]
            if it.kind != "p" or _KEYWORDS.match(self.text_of(it)) or it.style == "keywords":
                break
            if it.images or it.display_math:
                break
            if it.style != "abstract" and doc.abstract and it.bold_all:
                break
            if self.text_of(it) or it.children:
                doc.abstract.append(Para(children=it.children))
            it.used = True
            n += 1

    def find_references(self, doc: Document, items: list[Item]) -> None:
        # The last match: a line reading "Reference" earlier on is not the reference list.
        matches = [n for n, it in enumerate(items) if it.kind == "p" and not it.used and (_REF_HEADING.match(self.text_of(it)) or it.style == "references heading")]
        styled = [n for n in matches if items[n].style == "references heading"]
        heading_at = (styled or matches or [None])[-1]
        if heading_at is None:
            first_bib = next((n for n, it in enumerate(items) if it.kind == "p" and (it.style == "bibliography" or it.bibkey or it.bib_marker)), None)
            if first_bib is None:
                return
            heading_at = first_bib - 1
        else:
            items[heading_at].used = True
            items[heading_at].kind = "bibmark"
        taken = self.bib
        n = heading_at + 1
        keyed = False
        self.entry_numbers: dict[int, str] = {}
        self.entries_by_name: list[tuple[str, str, str]] = []
        while n < len(items):
            it = items[n]
            if it.kind != "p" or self.is_heading_style(it) or (it.bold_all and len(self.text_of(it).split()) < 12 and not it.bibkey):
                break
            if it.images or it.display_math or it.style in ("caption", "figure body", "latex block") or it.has_seq:
                break  # a figure after the references is not a reference
            if keyed and not it.bibkey and self.text_of(it):
                break  # paperpush keys every entry, so this is body text after the list
            keyed = keyed or bool(it.bibkey)
            text = self.text_of(it)
            it.used = True
            n += 1
            if not text and not it.bibkey:
                continue
            m = _ENTRY_NUMBER.match(text)
            number = int(m.group(1) or m.group(2)) if m and not it.bibkey else None
            body = text[m.end() :] if m else text
            key = it.bibkey
            if not key:
                family = re.match(r"\s*([^\s,.(]+)", body)
                year = re.search(r"\b(1[89]\d\d|20\d\d)\b", body)
                key = biblib.key_for(family.group(1) if family else "ref", year.group(1) if year else "", taken)
            if key not in taken:
                taken[key] = BibEntry(key=key, text=body.strip())
            if number is not None:
                self.entry_numbers[number] = key
            family = re.match(r"\s*([^\s,.(]+)", body)
            year = re.search(r"\b(1[89]\d\d|20\d\d)([a-z]?)\b", body)
            if family and year and not it.bibkey:
                self.entries_by_name.append((family.group(1).lower(), year.group(0), key))
        if heading_at >= 0 and items[heading_at].kind == "bibmark":
            items[heading_at].kind = "bib"
            items[heading_at].used = False
        elif heading_at + 1 < len(items):
            marker = Item(kind="bib")
            items.insert(heading_at + 1, marker)

    def build_blocks(self, items: list[Item]) -> list:
        out: list = []
        i = 0
        appendix = False
        figure_seen = table_seen = 0
        while i < len(items):
            it = items[i]
            if it.kind == "bib":
                out.append(Bibliography())
                i += 1
                continue
            if it.kind == "tbl":
                result = self.read_table(it.table)
                if isinstance(result, Table):
                    caption_item = self.table_caption(items, i, out)
                    if caption_item is not None:
                        table_seen += 1
                        self.apply_caption(result, caption_item, _TAB_CAPTION, "tab", table_seen)
                    else:
                        result.numbered = False
                out.append(result)
                i += 1
                continue
            text = self.text_of(it)
            if it.style == "latex block":
                out.append(self.raw_block(it, text))
                i += 1
                continue
            if it.used:
                i += 1
                continue
            level = self.heading_level(it)
            if level:
                numbered, children = self.heading_number(it)
                fmt = self.numbering.get(it.num[0], {}).get(0, "") if it.num else ""
                if fmt == "upperLetter" and not appendix:
                    from .model import Appendix

                    out.append(Appendix())
                    appendix = True
                out.append(Heading(level=level, children=_strip_bold(children, True), numbered=numbered, label=self.heading_label(it, children)))
                i += 1
                continue
            if it.images and not text:
                i = self.figure(items, i, out)
                continue
            if it.display_math and not text:
                for math in it.display_math:
                    body, has_rows = omml_rows_to_latex(math)
                    restored = (self.manifest.get("math") or {}).get(signature(math))
                    if has_rows and not restored:
                        body = "\\begin{aligned}\n" + body + "\n\\end{aligned}"
                    out.append(Equation(latex=restored or body, row_labels=[], numbered=False, sig=signature(math)))
                i += 1
                continue
            legacy = self.legacy_equation(it)
            if legacy is not None:
                out.append(legacy)
                i += 1
                continue
            if it.style == "figure body" and text.startswith("[Figure not available"):
                i = self.figure(items, i, out)
                continue
            if self.is_caption(it, _FIG_CAPTION, "Figure") and (i + 1 >= len(items) or not items[i + 1].images):
                figure = Figure(images=[], numbered=True)
                figure_seen += 1
                self.apply_caption(figure, it, _FIG_CAPTION, "fig", figure_seen)
                self.log.warn(f"caption with no image next to it: {text[:60]}. Upload the figure separately or check its placement")
                out.append(figure)
                i += 1
                continue
            if self.is_caption(it, _TAB_CAPTION, "Table") and not it.used and i + 1 < len(items) and items[i + 1].kind == "tbl":
                i += 1  # read with the table that follows
                items[i - 1].used = False
                items[i - 1].kind = "caption"
                continue
            if it.kind == "caption":
                i += 1
                continue
            if self.is_list(it):
                i = self.list_block(items, i, out)
                continue
            if it.style in ("quote", "block text", "intense quote"):
                j = i
                blocks = []
                while j < len(items) and items[j].kind == "p" and items[j].style == it.style:
                    blocks.append(Para(children=items[j].children))
                    j += 1
                out.append(Quote(blocks=blocks))
                i = j
                continue
            if it.style in ("source code", "code", "verbatim", "html preformatted"):
                j = i
                lines = []
                while j < len(items) and items[j].kind == "p" and items[j].style == it.style:
                    lines.append(_code_text(items[j].children))
                    j += 1
                out.append(Code(text="\n".join(lines)))
                i = j
                continue
            if it.style in ("equation spacer", "table spacer") or (not text and not it.children):
                i += 1
                continue
            out.append(Para(children=_trim(it.children)))
            out += self.flush_images(it)
            i += 1
        self.check_caption_order(out)
        return out

    def flush_images(self, it: Item) -> list:
        if it.images:
            self.log.warn("an image sits inside a paragraph of text: it was moved after the paragraph")
            return [Figure(images=it.images, numbered=False)]
        return []

    def raw_block(self, it: Item, text: str):
        m = re.match(r"\[(LaTeX block \d+ \([^)]*\))", text)
        token = it.raw_token or (m.group(1) if m else "")
        source = (self.manifest.get("raw") or {}).get(token)
        if source is not None:
            return Raw(tex=source, note=token, source=source)
        if it.images:
            self.log.warn(f"{token or 'a LaTeX block'} is only an image in this Word file: it comes through as a picture, not as LaTeX")
            return Figure(images=it.images, numbered=False)
        self.log.warn(f"the LaTeX behind placeholder '{token or text[:40]}' is gone: its content is missing from the output")
        return Para(children=[Text(text)])

    def heading_level(self, it: Item) -> int:
        level = self.is_heading_style(it)
        if level:
            return level
        for style_id, style in self.styles.items():
            if style["name"] == it.style and style["outline"] is not None and style["outline"] < 6:
                return style["outline"] + 1
        text = self.text_of(it)
        # With real heading styles in use, a bold line is only a heading if it is numbered like one.
        if self.styled_headings and not _NUMBER_PREFIX.match(text):
            return 0
        if it.bold_all and text and len(text.split()) <= 12 and not text.endswith((".", ":", ";", ",")) and not it.images and not it.display_math and not it.num:
            self.log.count("treated a short all-bold line as a heading")
            return 2 if _NUMBER_PREFIX.match(text) and "." in _NUMBER_PREFIX.match(text).group(1) else 1
        return 0

    def heading_number(self, it: Item) -> tuple[bool, list]:
        numbered = bool(it.num and it.num[0] != 0)
        children = it.children
        text = self.text_of(it)
        if _NUMBER_PREFIX.match(text):
            children = _strip_prefix(children, _NUMBER_PREFIX)
            numbered = True
            self.log.count("removed typed section numbers (the output numbers sections itself)")
        return numbered, _trim(children)

    def heading_label(self, it: Item, children: list) -> str:
        for name in it.bookmarks:
            label = self.bookmark_label.get(name)
            if label:
                return label
        if it.bookmarks:
            slug = re.sub(r"[^a-z0-9]+", "-", plain_text(children).lower()).strip("-")[:30] or "section"
            label = f"\x01sec:{slug}"
            for name in it.bookmarks:
                self.bookmark_label[name] = label
            return label
        return ""

    def is_caption(self, it: Item, pattern: re.Pattern, word: str) -> bool:
        if it.kind != "p":
            return False
        text = self.text_of(it)
        return (it.has_seq.lower() == word.lower()) or (bool(pattern.match(text)) and (it.style == "caption" or len(text) > 0 and pattern.match(text) is not None and self.caption_like(text, pattern)))

    def caption_like(self, text: str, pattern: re.Pattern) -> bool:
        m = pattern.match(text)
        rest = text[m.end() :]
        # "Figure 2 shows ..." is a sentence; "Figure 2. Overview" or "Figure 2 | Overview" is a caption.
        return bool(re.search(r"[.:|–—-]\s*$", text[: m.end()])) or not rest or rest[:1].isupper() and not re.match(r"(shows|is|was|illustrates|depicts|presents)\b", rest, re.I)

    def apply_caption(self, block, it: Item, pattern: re.Pattern, prefix: str, seen: int) -> None:
        it.used = True
        text = self.text_of(it)
        m = pattern.match(text)
        block.caption = _trim(_strip_prefix(it.children, pattern)) if m else _trim(it.children)
        number = m.group(2) if m else str(seen)
        block.numbered = True
        block._typed_number = number  # type: ignore[attr-defined]
        if number.upper().startswith("S"):
            self.log.warn(f"caption numbered {number}: LaTeX numbers floats from 1, so set \\renewcommand{{\\the{'figure' if prefix == 'fig' else 'table'}}}{{S\\arabic{{{'figure' if prefix == 'fig' else 'table'}}}}} for a supplement")
        label = next((self.bookmark_label[b] for b in it.bookmarks if self.bookmark_label.get(b)), "")
        if not label and not self.manifest:  # a file of ours labels what its source labelled
            label = f"\x01{prefix}:{number.lower()}"
            for name in it.bookmarks:
                self.bookmark_label[name] = label
        block.label = label

    def table_caption(self, items: list[Item], i: int, out: list) -> Item | None:
        if i > 0 and items[i - 1].kind == "caption":
            items[i - 1].kind = "p"
            return items[i - 1]
        if i + 1 < len(items) and items[i + 1].kind == "p" and self.is_caption(items[i + 1], _TAB_CAPTION, "Table"):
            return items[i + 1]
        return None

    def figure(self, items: list[Item], i: int, out: list) -> int:
        images: list[Image] = []
        j = i
        while j < len(items) and items[j].kind == "p" and (items[j].images or items[j].style == "figure body") and not (self.text_of(items[j]) and not items[j].style == "figure body"):
            images += items[j].images
            j += 1
        figure = Figure(images=images, numbered=False)
        caption = None
        if j < len(items) and items[j].kind == "p" and self.is_caption(items[j], _FIG_CAPTION, "Figure"):
            caption = items[j]
            j += 1
        elif out and isinstance(out[-1], Para) and _FIG_CAPTION.match(plain_text(out[-1].children)):
            prior = out.pop()
            caption = Item(kind="p", children=prior.children)
        if caption is not None:
            n = sum(isinstance(b, Figure) and b.numbered for b in out) + 1
            self.apply_caption(figure, caption, _FIG_CAPTION, "fig", n)
        out.append(figure)
        return j

    def legacy_equation(self, it: Item) -> Equation | None:
        """A paragraph of only an equation and a typed ``(3)``."""
        maths = [c for c in it.children if isinstance(c, Math)]
        if len(maths) != 1:
            return None
        rest = "".join(c.text for c in it.children if isinstance(c, Text))
        m = re.fullmatch(r"\s*\(\s*(\d+)\s*\)\s*", rest)
        if not m:
            return None
        return Equation(latex=maths[0].latex, row_labels=[f"\x01eq:{m.group(1)}"], numbered=True, sig=maths[0].sig)

    def is_list(self, it: Item) -> bool:
        if it.num is None or it.num[0] == 0:
            return False
        return bool(self.numbering.get(it.num[0])) or it.style.startswith("list")

    def list_block(self, items: list[Item], i: int, out: list) -> int:
        num_id = items[i].num[0]

        def ordered(level: int) -> bool:
            return self.numbering.get(num_id, {}).get(level, "bullet") != "bullet"

        def build(j: int, level: int) -> tuple[ListBlock, int]:
            block = ListBlock(ordered=ordered(level), items=[])
            while j < len(items):
                it = items[j]
                if it.kind != "p":
                    break
                if it.style == "list continue" and block.items:
                    block.items[-1].append(Para(children=it.children))
                    j += 1
                    continue
                if it.num is None or it.num[0] != num_id:
                    break
                if it.num[1] < level:
                    break
                if it.num[1] > level:
                    if not block.items:
                        block.items.append([Para(children=[])])
                    sub, j = build(j, level + 1)
                    block.items[-1].append(sub)
                    continue
                block.items.append([Para(children=_trim(it.children))])
                j += 1
            return block, j

        block, j = build(i, items[i].num[1])
        out.append(block)
        return j

    def check_caption_order(self, blocks: list) -> None:
        for kind, word in ((Figure, "Figure"), (Table, "Table")):
            numbers = [getattr(b, "_typed_number", "") for b in blocks if isinstance(b, kind) and b.numbered]
            digits = [int(n) for n in numbers if n.isdigit()]
            if digits and digits != list(range(digits[0], digits[0] + len(digits))):
                self.log.warn(f"{word} captions are numbered {digits} in the Word file; the output numbers them in the order they appear")

    # -- recovering citations and references -----------------------------------

    def finish_labels(self, doc: Document) -> None:
        """Replace bookmark references with labels, and generated placeholders with final labels."""

        def clean(label: str) -> str:
            return label[1:] if label.startswith("\x01") else label

        for block in all_blocks(doc.blocks):
            if isinstance(block, (Heading, Figure, Table)) and block.label:
                block.label = clean(block.label)
            elif isinstance(block, Equation):
                block.row_labels = [clean(x) for x in block.row_labels]
        known = {clean(v) for v in self.bookmark_label.values()}
        dropped = 0
        for block in list(all_blocks(doc.blocks)) + doc.abstract:
            containers = _inline_lists(block)
            for container in containers:
                n = 0
                while n < len(container):
                    node = container[n]
                    if isinstance(node, Ref) and node.label.startswith("\x00"):
                        name = node.label[1:]
                        label = clean(self.bookmark_label.get(name, ""))
                        if label and label in known:
                            node.label = label
                            if self.is_paren_ref(container, n, label):
                                container[n - 1] = Text(container[n - 1].text[:-1])
                                container[n + 1] = Text(container[n + 1].text[1:])
                                node.kind = "eqref"
                        else:
                            dropped += 1
                            container[n] = Text("??")
                    n += 1
        if dropped:
            self.log.warn(f"{dropped} cross-reference field(s) point at a bookmark that no longer exists: shown as ??")

    def is_paren_ref(self, container: list, n: int, label: str) -> bool:
        return label.startswith("eq") and n > 0 and n + 1 < len(container) and isinstance(container[n - 1], Text) and container[n - 1].text.endswith("(") and isinstance(container[n + 1], Text) and container[n + 1].text.startswith(")")

    def typed_citations(self, doc: Document) -> None:
        numbers = getattr(self, "entry_numbers", {})
        names = getattr(self, "entries_by_name", [])
        if not numbers and not names:
            return
        converted = 0
        unmatched: set[str] = set()

        def numeric(match: re.Match):
            nonlocal converted
            wanted = _expand_numbers(match.group(1))
            if not wanted or not all(n in numbers for n in wanted):
                return None
            converted += 1
            return Cite(keys=[numbers[n] for n in wanted])

        def lookup(name: str, year: str):
            family = re.match(r"\s*([^\s,.]+)", name)
            if not family:
                return None
            target = family.group(1).lower()
            return next((key for fam, y, key in names if fam == target and y == year), None)

        def author_year(match: re.Match):
            nonlocal converted
            keys = []
            for part in match.group(1).split(";"):
                found = re.search(r"\b((?:1[89]|20)\d\d[a-z]?)\s*$", part.strip())
                if not found or not re.search(r"[A-Za-z]{2}", part[: found.start() + 1]):  # "(2019)" alone is not a citation
                    return None
                key = lookup(part, found.group(1))
                if key is None:
                    if re.match(r"[A-Z][\w'’-]+(?:\s+(?:et al\.?|and\s+\S+|&\s+\S+))?,?\s+\d{4}", part.strip()):
                        unmatched.add(part.strip())
                    return None
                keys.append(key)
            converted += 1
            return Cite(keys=keys)

        def narrative(match: re.Match):
            nonlocal converted
            key = lookup(match.group(1), match.group(2))
            if key is None:
                return None
            converted += 1
            return Cite(keys=[key], mode="text")

        patterns = []
        if numbers:
            patterns.append((_NUMERIC_CITE, numeric))
        if names:
            patterns += [(_NARRATIVE, narrative), (_AUTHOR_YEAR_GROUP, author_year)]
        for block in all_blocks(doc.blocks):
            for container in _inline_lists(block):
                if numbers:
                    self.superscript_citations(container, numbers)
                _rewrite_text(container, patterns)
        if not numbers and names and converted:
            doc.citation_style = "author-year"
        if converted:
            self.log.change(f"turned {converted} typed citation(s) into live citations by matching them to the reference list")
        for part in sorted(unmatched)[:5]:
            self.log.warn(f"typed citation '{part}' matches nothing in the reference list: left as text")

    def superscript_citations(self, container: list, numbers: dict[int, str]) -> None:
        for n, node in enumerate(container):
            if isinstance(node, Style) and node.kind == "sup":
                text = plain_text(node.children).strip()
                if re.fullmatch(r"\d+(?:\s*[,–-]\s*\d+)*", text) and not (n and isinstance(container[n - 1], Text) and container[n - 1].text[-1:].isdigit()):
                    wanted = _expand_numbers(text)
                    if wanted and all(k in numbers for k in wanted):
                        container[n] = Cite(keys=[numbers[k] for k in wanted])

    def typed_refs(self, doc: Document) -> None:
        figures = {getattr(b, "_typed_number", "").lower(): b.label for b in all_blocks(doc.blocks) if isinstance(b, Figure) and b.label}
        tables = {getattr(b, "_typed_number", "").lower(): b.label for b in all_blocks(doc.blocks) if isinstance(b, Table) and b.label}
        equations = {}
        for b in all_blocks(doc.blocks):
            if isinstance(b, Equation):
                for label in b.row_labels:
                    if label.startswith("eq:") and label[3:].isdigit():
                        equations[label[3:]] = label
        converted = 0

        def replace(match: re.Match):
            nonlocal converted
            word = match.group(1).lower()
            table = figures if word.startswith("fig") else tables if word.startswith("tab") else equations
            label = table.get(match.group(4).lower())
            if not label:
                return None
            converted += 1
            paren = match.group(3) == "(" and match.group(5) == ")"
            nodes: list = [Text(match.group(1) + "\u00a0"), Ref(label, "eqref" if paren else "ref")]
            if not paren:
                nodes = [Text(match.group(1) + "\u00a0" + match.group(3)), Ref(label, "ref"), Text(match.group(5))]
            return nodes

        untouched = self.manifest.get("blocks") or {}
        for block in all_blocks(doc.blocks):
            if isinstance(block, (Figure, Table)) or fingerprint(block) in untouched:
                continue
            for container in _inline_lists(block):
                _rewrite_text(container, [(_TYPED_REF, replace)])
        if converted:
            self.log.change(f"turned {converted} typed figure, table, and equation reference(s) into live cross-references")


class _State:
    """Inline output for one paragraph, with the Word field stack."""

    def __init__(self, item: Item, extra: list[Item]) -> None:
        self.item = item
        self.extra = extra
        self.out: list = []
        self.stack: list[dict] = []
        self.text_chars = 0
        self.bold_chars = 0
        self.max_size = 0

    def in_instr(self) -> bool:
        return bool(self.stack) and self.stack[-1]["phase"] == "instr"

    def instr(self, text: str) -> None:
        if self.stack and self.stack[-1]["phase"] == "instr":
            self.stack[-1]["instr"] += text

    def text(self, text: str, kinds: tuple, bold: bool, size: int) -> None:
        if self.in_instr():
            return
        stripped = len(text.strip())
        self.text_chars += stripped
        if bold:
            self.bold_chars += stripped
        self.max_size = max(self.max_size, size)
        node = Text(text.replace("\t", " "))
        for kind in reversed(_ordered(kinds)):
            node = Style(kind, [node])
        self.out.append(node)

    def emit(self, node) -> None:
        if self.in_instr():
            return
        self.out.append(node)

    def emit_many(self, nodes: list) -> None:
        for node in nodes:
            self.emit(node)

    def finish(self) -> None:
        while self.stack:
            frame = self.stack.pop()
            result = self.out
            self.out = frame["saved"] + result


def _ordered(kinds: tuple) -> list[str]:
    order = ["code", "strong", "emph", "smallcaps", "underline", "sup", "sub"]
    return sorted(set(kinds), key=order.index)


def _cells(tr) -> list:
    out = []
    for child in tr:
        if _ns(child) != W:
            continue
        if _local(child) == "tc":
            out.append(child)
        elif _local(child) == "sdt":
            content = child.find(w("sdtContent"))
            if content is not None:
                out += _cells(content)
    return out


def _cell_align(tc) -> str:
    p = tc.find(w("p"))
    if p is None or p.find(w("pPr")) is None:
        return ""
    jc = p.find(w("pPr")).find(w("jc"))
    return jc.get(w("val"), "") if jc is not None else ""


def _symbol(el) -> str:
    code = el.get(w("char"), "")
    try:
        value = int(code, 16)
    except ValueError:
        return ""
    if 0xF000 <= value <= 0xF0FF:
        value -= 0xF000
    font = el.get(w("font"), "").lower()
    if "symbol" in font:
        greek = {0x61: "α", 0x62: "β", 0x67: "γ", 0x64: "δ", 0x65: "ε", 0x6D: "μ", 0x70: "π", 0x73: "σ", 0x74: "τ", 0x77: "ω", 0xB1: "±", 0xA3: "≤", 0xB3: "≥", 0xB4: "×", 0xB0: "°", 0xAE: "→", 0xB9: "≠", 0xBB: "≈", 0x44: "Δ", 0x53: "Σ", 0x57: "Ω"}
        return greek.get(value, chr(value))
    return chr(value)


def _strip_note_lead(children: list) -> list:
    children = list(children)
    if children and isinstance(children[0], Text):
        children[0] = Text(children[0].text.lstrip())
    return merge_text(children)


def _unlink(node):
    """Remove the underline Word's Hyperlink style adds."""
    if isinstance(node, Style) and node.kind == "underline":
        return Text(plain_text(node.children))
    return node


def _strip_bold(children: list, on: bool) -> list:
    """Remove bold that comes from a heading's or header row's look rather than emphasis."""
    if not on:
        return children
    out: list = []
    for node in children:
        if isinstance(node, Style) and node.kind == "strong":
            out += node.children
        elif isinstance(node, Style):
            out.append(Style(node.kind, _strip_bold(node.children, True)))
        else:
            out.append(node)
    return merge_text(out)


def _trim(children: list) -> list:
    children = list(children)
    while children and isinstance(children[0], Text):
        text = children[0].text.lstrip()
        if text:
            children[0] = Text(text)
            break
        children.pop(0)
    while children and isinstance(children[-1], Text):
        text = children[-1].text.rstrip()
        if text:
            children[-1] = Text(text)
            break
        children.pop()
    return children


def _strip_prefix(children: list, pattern: re.Pattern) -> list:
    """``children`` with whatever ``pattern`` matches at the start of their text removed."""
    text = plain_text(children)
    m = pattern.match(text)
    if not m:
        return children
    remaining = m.end()
    out: list = []
    for node in children:
        if remaining <= 0:
            out.append(node)
            continue
        if isinstance(node, Text):
            take = min(len(node.text), remaining)
            remaining -= take
            if node.text[take:]:
                out.append(Text(node.text[take:]))
        elif isinstance(node, Style):
            length = len(plain_text(node.children))
            take = min(length, remaining)
            remaining -= take
            rest = _strip_chars(node.children, take)
            if rest:
                out.append(Style(node.kind, rest))
        elif isinstance(node, Ref):
            continue
        else:
            out.append(node)
    return merge_text(out)


def _strip_chars(children: list, n: int) -> list:
    out: list = []
    for node in children:
        if n <= 0:
            out.append(node)
        elif isinstance(node, Text):
            take = min(len(node.text), n)
            n -= take
            if node.text[take:]:
                out.append(Text(node.text[take:]))
        elif isinstance(node, Style):
            length = len(plain_text(node.children))
            take = min(length, n)
            n -= take
            rest = _strip_chars(node.children, take)
            if rest:
                out.append(Style(node.kind, rest))
        else:
            out.append(node)
    return out


def _code_text(children: list) -> str:
    out = []
    for node in children:
        if isinstance(node, Text):
            out.append(node.text)
        elif isinstance(node, Style):
            out.append(_code_text(node.children))
        elif isinstance(node, Break):
            out.append("\n")
    return "".join(out)


def _looks_like_authors(text: str) -> bool:
    """Whether a line reads as a list of names rather than a sentence."""
    if len(text) > 800 or re.search(r"[.!?]\s*$", text) or _looks_like_affiliation(text):
        return False
    names = [n.strip(" *†‡0-9") for n in re.split(r",|;|\band\b|&", re.sub(r"\d", "", text))]
    names = [n for n in names if n]
    if not names:
        return False
    particles = {"de", "van", "der", "von", "la", "le", "del", "da", "dos", "di", "bin", "ibn"}
    return all(1 <= len(n.split()) <= 5 and all(w[0].isupper() or w.lower() in particles for w in n.split()) for n in names)


def _looks_like_affiliation(text: str) -> bool:
    return bool(re.search(r"(?i)\b(university|universit[àéä]t|institute|department|dept\.|school of|college|hospital|laborator|centre|center for|faculty|inc\.|ltd|gmbh)\b", text)) or bool(re.match(r"^\s*\d+\s+[A-Z]", text))


def _parse_authors(children: list) -> list[Author]:
    """Authors from a line like ``Ada Lovelace1,2*, Alan Turing2``, superscripts and all."""
    pieces: list[tuple[str, str]] = []
    for node in children:
        if isinstance(node, Style) and node.kind == "sup":
            pieces.append(("sup", plain_text(node.children)))
        elif isinstance(node, Style):
            inner = _unstyled(node)
            pieces.append(("text", inner))
        elif isinstance(node, Text):
            pieces.append(("text", node.text))
    authors: list[Author] = []
    current = ""

    def close(marks: str = "") -> None:
        nonlocal current
        name = current.strip(" ,;")
        name = re.sub(r"^and\s+|^&\s*", "", name).strip()
        if name:
            affils = sorted({int(x) for x in re.findall(r"\d+", marks)})
            authors.append(Author(name=name, affiliations=affils, corresponding="*" in marks))
        current = ""

    for kind, text in pieces:
        if kind == "sup":
            close(text)
            continue
        parts = re.split(r",\s*|;\s*|\s+and\s+|\s*&\s*", text)
        for n, part in enumerate(parts):
            if n:
                close()
            current += part
    close()
    return authors


def _unstyled(node) -> str:
    return plain_text(node.children) if isinstance(node, Style) else ""


def _inline_lists(block) -> list[list]:
    """Every mutable list of inlines in ``block``."""
    out: list[list] = []

    def visit(children: list) -> None:
        out.append(children)
        for node in children:
            if isinstance(node, (Style, Link)):
                visit(node.children)
            elif isinstance(node, Note):
                for sub in node.blocks:
                    if isinstance(sub, Para):
                        visit(sub.children)

    if isinstance(block, (Heading, Para)):
        visit(block.children)
    elif isinstance(block, Figure):
        visit(block.caption)
    elif isinstance(block, Table):
        visit(block.caption)
        for row in block.rows:
            for cell in row:
                visit(cell.children)
    return out


def _expand_numbers(text: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"\s*,\s*", text.strip()):
        m = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", part)
        if m:
            low, high = int(m.group(1)), int(m.group(2))
            if high < low or high - low > 200:
                return []
            out += list(range(low, high + 1))
        elif part.isdigit():
            out.append(int(part))
        else:
            return []
    return out


def _rewrite_text(container: list, patterns: list) -> None:
    """Apply ``(pattern, replacer)`` pairs to the text nodes of ``container``, in place."""
    for pattern, replace in patterns:
        n = 0
        while n < len(container):
            node = container[n]
            if not isinstance(node, Text):
                n += 1
                continue
            pieces: list = []
            last = 0
            for m in pattern.finditer(node.text):
                result = replace(m)
                if result is None:
                    continue
                pieces.append(Text(node.text[last : m.start()]))
                pieces += result if isinstance(result, list) else [result]
                last = m.end()
            if not pieces:
                n += 1
                continue
            pieces.append(Text(node.text[last:]))
            pieces = [p for p in pieces if not (isinstance(p, Text) and not p.text)]
            container[n : n + 1] = pieces
            n += len(pieces)


def _endnote_entry(cite: str, taken) -> BibEntry | None:
    def tag(name: str) -> str:
        m = re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", cite, re.S)
        return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""

    authors = [re.sub(r"<[^>]+>", "", a).strip() for a in re.findall(r"<author\b[^>]*>(.*?)</author>", cite, re.S)]
    year = tag("year") or tag("Year")
    title = tag("title")
    if not authors and not title:
        return None
    fields = {}
    if authors:
        fields["author"] = " and ".join(authors)
    if title:
        fields["title"] = "{" + title + "}"
    if year:
        fields["year"] = year
    for source, target in (("secondary-title", "journal"), ("volume", "volume"), ("number", "number"), ("pages", "pages"), ("electronic-resource-num", "doi")):
        value = tag(source)
        if value:
            fields[target] = value
    family = authors[0].split(",")[0].split()[-1] if authors else (tag("Author") or "ref")
    return BibEntry(key=biblib.key_for(family, year, taken), kind="article", fields=fields)
