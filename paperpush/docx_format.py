"""Reformat a Word (.docx) manuscript: a venue's Word template, or its layout rules.

The edits are made on the package's XML as text, element by element, so the
rest of the file (namespaces, compatibility settings, fields, equations,
comments) is left byte for byte as Word wrote it. Children are inserted in the
order the OOXML schema prescribes, which Word insists on.

:class:`DocxFile` is the editable package. The two entry points are
:func:`apply_template`, which takes a template's styles, theme, page setup,
and headers and footers, and :func:`apply_rules`, which sets the page size,
margins, body font and size, double spacing, line numbers, and page numbers a
venue's guidelines ask for.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_HEADER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header"
REL_FOOTER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer"
CT_HEADER = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
CT_FOOTER = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
CT_STYLES = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
REL_STYLES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"

# Child order inside the elements edited here (ECMA-376 Part 1, the CT_* sequences).
PPR_ORDER = ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection", "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr", "pPrChange"]
RPR_ORDER = ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline", "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing", "w", "kern", "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath"]
SECT_ORDER = ["headerReference", "footerReference", "footnotePr", "endnotePr", "type", "pgSz", "pgMar", "paperSrc", "pgBorders", "lnNumType", "pgNumType", "cols", "formProt", "vAlign", "noEndnote", "titlePg", "textDirection", "bidi", "rtlGutter", "docGrid", "printerSettings", "sectPrChange"]
STYLE_ORDER = ["name", "aliases", "basedOn", "next", "link", "autoRedefine", "hidden", "uiPriority", "semiHidden", "unhideWhenUsed", "qFormat", "locked", "personal", "personalCompose", "personalReply", "rsid", "pPr", "rPr", "tblPr", "trPr", "tcPr", "tblStylePr"]
DOCDEFAULTS_ORDER = ["rPrDefault", "pPrDefault"]

PAGE_SIZES_TWIPS = {"a4": (11906, 16838), "letter": (12240, 15840), "us letter": (12240, 15840), "us-letter": (12240, 15840), "legal": (12240, 20160), "a5": (8391, 11906)}
TWIPS_PER_MM = 1440 / 25.4
# Fonts that carry symbols rather than text; a run set in one keeps it.
_SYMBOL_FONTS = re.compile(r"(?i)symbol|wingdings|webdings|math|mt extra|zapf|dingbat|marlett")

_TAG = re.compile(r"<(/?)([A-Za-z_][\w.-]*:[\w.-]+|[A-Za-z_][\w.-]*)((?:\s+[^\s=>/]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)\s*(/?)>")


# ---------------------------------------------------------------------------
# element-level helpers


def children(inner: str) -> list[tuple[str, str]]:
    """The top-level elements of an element's inner XML, as ``(local tag, xml)``; text between them is dropped."""
    out: list[tuple[str, str]] = []
    depth, start, tag = 0, 0, ""
    for m in _TAG.finditer(inner):
        closing, name, _, selfclose = m.group(1), m.group(2), m.group(3), m.group(4)
        if name.startswith("?") or name.startswith("!"):
            continue
        if not closing:
            if depth == 0:
                start, tag = m.start(), name.split(":")[-1]
            if selfclose:
                if depth == 0:
                    out.append((tag, inner[start : m.end()]))
            else:
                depth += 1
        else:
            depth -= 1
            if depth == 0:
                out.append((tag, inner[start : m.end()]))
    return out


def split(elem: str) -> tuple[str, str, str]:
    """``(open tag, inner, close tag)`` of one element; a self-closing one gets an empty inner and a close tag."""
    m = _TAG.match(elem.strip())
    if not m:
        raise ValueError(f"not an element: {elem[:60]}")
    name = m.group(2)
    if m.group(4):
        return f"<{name}{m.group(3)}>", "", f"</{name}>"
    close = elem.rstrip()[elem.rstrip().rfind("</") :]
    return elem[: m.end()], elem[m.end() : len(elem.rstrip()) - len(close)], close


def set_child(elem: str, child: str | None, order: list[str], tag: str | None = None) -> str:
    """``elem`` with its child ``tag`` replaced by ``child`` (removed when ``child`` is None), in schema order."""
    open_, inner, close = split(elem)
    tag = tag or (_TAG.match(child).group(2).split(":")[-1] if child else "")
    kids = [(t, x) for t, x in children(inner) if t != tag]
    if child is not None:
        rank = order.index(tag) if tag in order else len(order)
        at = next((i for i, (t, _) in enumerate(kids) if (order.index(t) if t in order else len(order)) > rank), len(kids))
        kids.insert(at, (tag, child))
    return open_ + "".join(x for _, x in kids) + close


def get_child(elem: str, tag: str) -> str | None:
    _, inner, _ = split(elem)
    return next((x for t, x in children(inner) if t == tag), None)


def attr(elem: str, name: str) -> str | None:
    m = re.search(rf'\s{re.escape(name)}="([^"]*)"', _TAG.match(elem.strip()).group(0))
    return m.group(1) if m else None


def with_attrs(elem: str, **attrs: str | None) -> str:
    """``elem`` with the ``w:``-prefixed attributes given set (None removes one)."""
    m = _TAG.match(elem)
    head = m.group(0)
    for key, value in attrs.items():
        name = "w:" + key
        head_wo = re.sub(rf'\s{re.escape(name)}="[^"]*"', "", head)
        if value is None:
            head = head_wo
        else:
            head = re.sub(r"\s*(/?)>$", lambda mm: f' {name}="{value}"' + ("/>" if mm.group(1) else ">"), head_wo)
    return head + elem[m.end() :]


# ---------------------------------------------------------------------------
# the package


@dataclass
class DocxFile:
    """A .docx (or .dotx) read into memory, its parts editable as text."""

    parts: dict[str, bytes]
    order: list[str] = field(default_factory=list)

    @classmethod
    def open(cls, path: Path) -> "DocxFile":
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            return cls(parts={n: zf.read(n) for n in names}, order=names)

    def text(self, name: str) -> str | None:
        data = self.parts.get(name)
        return None if data is None else data.decode("utf-8")

    def put(self, name: str, text: str | bytes) -> None:
        if name not in self.parts:
            self.order.append(name)
        self.parts[name] = text.encode("utf-8") if isinstance(text, str) else text

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            # [Content_Types].xml first, as Word writes it.
            for name in sorted(self.order, key=lambda n: n != "[Content_Types].xml"):
                zf.writestr(name, self.parts[name])
        tmp.replace(path)

    @property
    def main(self) -> str:
        return "word/document.xml"

    # --- relationships and content types ----------------------------------------

    def rels_name(self, part: str) -> str:
        folder, _, name = part.rpartition("/")
        return f"{folder}/_rels/{name}.rels"

    def add_rel(self, part: str, rel_type: str, target: str) -> str:
        name = self.rels_name(part)
        xml = self.text(name) or '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>'
        used = set(re.findall(r'Id="([^"]+)"', xml))
        n = 1
        while f"rIdPP{n}" in used:
            n += 1
        rid = f"rIdPP{n}"
        xml = xml.replace("</Relationships>", f'<Relationship Id="{rid}" Type="{rel_type}" Target="{target}"/></Relationships>')
        self.put(name, xml)
        return rid

    def rel_targets(self, part: str) -> dict[str, tuple[str, str]]:
        """``{rId: (type, target)}`` of a part's relationships."""
        xml = self.text(self.rels_name(part)) or ""
        out = {}
        for m in re.finditer(r"<Relationship\b([^>]*)/?>", xml):
            a = m.group(1)
            rid = re.search(r'Id="([^"]+)"', a)
            typ = re.search(r'Type="([^"]+)"', a)
            tgt = re.search(r'Target="([^"]+)"', a)
            if rid and typ and tgt and 'TargetMode="External"' not in a:
                out[rid.group(1)] = (typ.group(1), tgt.group(1))
        return out

    def add_override(self, part: str, content_type: str) -> None:
        xml = self.text("[Content_Types].xml") or ""
        if f'PartName="/{part}"' not in xml:
            xml = xml.replace("</Types>", f'<Override PartName="/{part}" ContentType="{content_type}"/></Types>')
            self.put("[Content_Types].xml", xml)

    def add_default(self, extension: str, content_type: str) -> None:
        xml = self.text("[Content_Types].xml") or ""
        if not re.search(rf'<Default\s+Extension="{re.escape(extension)}"', xml, re.IGNORECASE):
            xml = xml.replace("</Types>", f'<Default Extension="{extension}" ContentType="{content_type}"/></Types>')
            self.put("[Content_Types].xml", xml)

    def unused_name(self, stem: str, suffix: str) -> str:
        n = 1
        while f"word/{stem}{n}{suffix}" in self.parts:
            n += 1
        return f"word/{stem}{n}{suffix}"

    # --- sections -------------------------------------------------------------------

    def edit_sections(self, fn) -> int:
        """Apply ``fn(sectPr) -> sectPr`` to every section of the body; returns how many there are."""
        xml = self.text(self.main)
        count = 0

        def one(m: re.Match) -> str:
            nonlocal count
            count += 1
            return fn(m.group(0))

        xml = re.sub(r"<w:sectPr\b[^>]*/>|<w:sectPr\b[^>]*>.*?</w:sectPr>", one, xml, flags=re.DOTALL)
        if count == 0:
            xml = xml.replace("</w:body>", fn("<w:sectPr></w:sectPr>") + "</w:body>")
            count = 1
        self.put(self.main, xml)
        return count

    def final_section(self) -> str | None:
        xml = self.text(self.main) or ""
        found = re.findall(r"<w:sectPr\b[^>]*/>|<w:sectPr\b[^>]*>.*?</w:sectPr>", xml, flags=re.DOTALL)
        return found[-1] if found else None


# ---------------------------------------------------------------------------
# styles


def _style_blocks(styles: str) -> list[tuple[str, str, str, str]]:
    """``(styleId, name, type, xml)`` of each style in a styles part."""
    out = []
    for m in re.finditer(r"<w:style\b[^>]*>.*?</w:style>", styles, flags=re.DOTALL):
        block = m.group(0)
        sid = re.search(r'w:styleId="([^"]*)"', block[: block.find(">")])
        typ = re.search(r'w:type="([^"]*)"', block[: block.find(">")])
        name = re.search(r'<w:name\s+w:val="([^"]*)"', block)
        out.append((sid.group(1) if sid else "", (name.group(1) if name else "").strip().lower(), typ.group(1) if typ else "paragraph", block))
    return out


def _merge_root_namespaces(target: str, source: str) -> str:
    """``target``'s root element gains the namespace declarations and mc:Ignorable prefixes ``source``'s root has."""
    t_root = re.search(r"<w:styles\b[^>]*>|<w:document\b[^>]*>", target)
    s_root = re.search(r"<w:styles\b[^>]*>|<w:document\b[^>]*>", source)
    if not t_root or not s_root:
        return target
    head = t_root.group(0)
    for prefix, uri in re.findall(r'xmlns:([\w-]+)="([^"]*)"', s_root.group(0)):
        if f"xmlns:{prefix}=" not in head:
            head = head[:-1] + f' xmlns:{prefix}="{uri}">'
    s_ign = re.search(r'mc:Ignorable="([^"]*)"', s_root.group(0))
    if s_ign:
        t_ign = re.search(r'mc:Ignorable="([^"]*)"', head)
        have = set(t_ign.group(1).split()) if t_ign else set()
        merged = " ".join(sorted(have | set(s_ign.group(1).split())))
        declared = set(re.findall(r"xmlns:([\w-]+)=", head))
        merged = " ".join(p for p in merged.split() if p in declared)
        head = re.sub(r'\smc:Ignorable="[^"]*"', "", head)
        if merged and "xmlns:mc=" in head:
            head = head[:-1] + f' mc:Ignorable="{merged}">'
    return target[: t_root.start()] + head + target[t_root.end() :]


def merge_styles(doc: DocxFile, template: DocxFile) -> tuple[dict[str, str], int]:
    """Make the template's styles the document's, keeping the document's own styles the template lacks.

    Returns the style-id renames applied to the document and how many of the
    document's styles were matched to a template style by name.
    """
    theirs = template.text("word/styles.xml")
    if not theirs:
        return {}, 0
    ensure_styles(doc)
    mine = doc.text("word/styles.xml") or ""
    t_blocks = _style_blocks(theirs)
    t_by_name = {(n, t): sid for sid, n, t, _ in t_blocks if n}
    t_ids = {sid for sid, _, _, _ in t_blocks}
    renames: dict[str, str] = {}
    keep: list[tuple[str, str]] = []
    matched = 0
    for sid, name, typ, block in _style_blocks(mine):
        if (name, typ) in t_by_name:
            if t_by_name[(name, typ)] != sid:
                renames[sid] = t_by_name[(name, typ)]
            matched += 1
            continue
        new_id = sid
        if sid in t_ids:  # same id, different style: keep both
            new_id = sid + "PP"
            renames[sid] = new_id
        keep.append((new_id, block))
    added = []
    for new_id, block in keep:
        block = re.sub(r'(w:styleId=")[^"]*(")', lambda m: m.group(1) + new_id + m.group(2), block, count=1)
        block = _rename_refs(block, renames)
        added.append(block)
    merged = _merge_root_namespaces(theirs, mine)
    merged = merged.replace("</w:styles>", "".join(added) + "</w:styles>")
    doc.put("word/styles.xml", merged)
    if renames:
        for part in [n for n in doc.parts if n.startswith("word/") and n.endswith(".xml") and n != "word/styles.xml"]:
            text = doc.text(part)
            if text and re.search(r"<w:(?:pStyle|rStyle|tblStyle|basedOn|next|link|styleLink|numStyleLink)\b", text):
                doc.put(part, _rename_refs(text, renames))
    return renames, matched


def _rename_refs(xml: str, renames: dict[str, str]) -> str:
    if not renames:
        return xml
    return re.sub(r'(<w:(?:pStyle|rStyle|tblStyle|basedOn|next|link|styleLink|numStyleLink)\s+w:val=")([^"]*)(")', lambda m: m.group(1) + renames.get(m.group(2), m.group(2)) + m.group(3), xml)


# ---------------------------------------------------------------------------
# headers and footers


def _copy_part_with_media(doc: DocxFile, template: DocxFile, part: str, stem: str, content_type: str) -> str:
    """Copy a header/footer part (and the images it uses) from the template; returns its new name in ``doc``."""
    new = doc.unused_name(stem, ".xml")
    xml = template.text(part) or ""
    for rid, (typ, target) in template.rel_targets(part).items():
        src = "word/" + target if not target.startswith("/") else target.lstrip("/")
        if src not in template.parts:
            continue
        ext = Path(src).suffix.lower().lstrip(".")
        dest = doc.unused_name("media/pp_image", "." + ext) if "/media/" in src else None
        if dest is None:
            continue
        doc.put(dest, template.parts[src])
        doc.add_default(ext, {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "gif": "image/gif", "emf": "image/x-emf", "wmf": "image/x-wmf", "svg": "image/svg+xml"}.get(ext, "application/octet-stream"))
        new_rid = doc.add_rel(new, typ, dest[len("word/") :])
        xml = re.sub(rf'(r:(?:embed|id|link)=")({re.escape(rid)})(")', lambda m: m.group(1) + new_rid + m.group(3), xml)
    doc.put(new, xml)
    doc.add_override(new, content_type)
    return new


def copy_headers_footers(doc: DocxFile, template: DocxFile) -> list[str]:
    """Give every section of ``doc`` the template's headers and footers; returns what was copied."""
    t_sect = template.final_section()
    if not t_sect:
        return []
    refs = re.findall(r"<w:(header|footer)Reference\b[^>]*/>", t_sect)
    if not refs:
        return []
    t_rels = template.rel_targets(template.main)
    new_refs: list[str] = []
    copied: list[str] = []
    for m in re.finditer(r"<w:(header|footer)Reference\b([^>]*)/>", t_sect):
        kind, attrs = m.group(1), m.group(2)
        rid = re.search(r'r:id="([^"]+)"', attrs)
        typ = re.search(r'w:type="([^"]+)"', attrs)
        if not rid or rid.group(1) not in t_rels:
            continue
        part = "word/" + t_rels[rid.group(1)][1]
        if part not in template.parts:
            continue
        new = _copy_part_with_media(doc, template, part, kind, CT_HEADER if kind == "header" else CT_FOOTER)
        new_rid = doc.add_rel(doc.main, REL_HEADER if kind == "header" else REL_FOOTER, new[len("word/") :])
        new_refs.append(f'<w:{kind}Reference w:type="{typ.group(1) if typ else "default"}" r:id="{new_rid}"/>')
        copied.append(f"{kind} ({typ.group(1) if typ else 'default'})")

    def fn(sect: str) -> str:
        open_, inner, close = split(sect)
        kids = [x for t, x in children(inner) if t not in ("headerReference", "footerReference")]
        return open_ + "".join(new_refs) + "".join(kids) + close

    _ensure_r_namespace(doc)
    doc.edit_sections(fn)
    if "<w:titlePg" in t_sect:
        doc.edit_sections(lambda s: set_child(s, "<w:titlePg/>", SECT_ORDER))
    return copied


def _ensure_r_namespace(doc: DocxFile) -> None:
    xml = doc.text(doc.main) or ""
    root = re.search(r"<w:document\b[^>]*>", xml)
    if root and "xmlns:r=" not in root.group(0):
        xml = xml[: root.end() - 1] + f' xmlns:r="{R_NS}">' + xml[root.end() :]
        doc.put(doc.main, xml)


# ---------------------------------------------------------------------------
# page numbers


def has_page_numbers(doc: DocxFile) -> bool:
    for name, data in doc.parts.items():
        if re.match(r"word/(?:footer|header)\d*\w*\.xml$", name):
            text = data.decode("utf-8", "replace")
            if re.search(r"instrText[^>]*>\s*PAGE\b|w:instr=\"\s*PAGE\b", text):
                return True
    return False


def add_page_number_footer(doc: DocxFile) -> None:
    """A centered PAGE field in a new default footer on every section that has no default footer."""
    footer = doc.unused_name("footer", ".xml")
    doc.put(
        footer,
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:ftr xmlns:w="{W_NS}" xmlns:r="{R_NS}"><w:p><w:pPr><w:jc w:val="center"/></w:pPr>' '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>' '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p></w:ftr>',
    )
    doc.add_override(footer, CT_FOOTER)
    rid = doc.add_rel(doc.main, REL_FOOTER, footer[len("word/") :])
    _ensure_r_namespace(doc)

    def fn(sect: str) -> str:
        if re.search(r'<w:footerReference\b[^>]*w:type="default"', sect):
            return sect
        open_, inner, close = split(sect)
        kids = children(inner)
        at = next((i for i, (t, _) in enumerate(kids) if t not in ("headerReference", "footerReference")), len(kids))
        kids.insert(at, ("footerReference", f'<w:footerReference w:type="default" r:id="{rid}"/>'))
        return open_ + "".join(x for _, x in kids) + close

    doc.edit_sections(fn)


# ---------------------------------------------------------------------------
# direct formatting


def strip_direct_formatting(doc: DocxFile, *, fonts: bool, sizes: bool, spacing: bool) -> int:
    """Remove the run fonts, sizes, and paragraph line spacing set directly in the text, so the styles show.

    Tables keep their spacing, and runs set in a symbol font keep it.
    Returns how many settings were removed.
    """
    xml = doc.text(doc.main) or ""
    removed = 0

    def strip_runs(segment: str) -> str:
        nonlocal removed
        if fonts:

            def drop_font(m: re.Match) -> str:
                nonlocal removed
                if _SYMBOL_FONTS.search(m.group(0)):
                    return m.group(0)
                removed += 1
                return ""

            segment = re.sub(r"<w:rFonts\b[^>]*/>", drop_font, segment)
        if sizes:

            def drop_size(m: re.Match) -> str:
                nonlocal removed
                value = re.search(r'w:val="(\d+)"', m.group(0))
                # Larger lettering (a heading set by hand) keeps its size; body-sized text loses it.
                if value and body_size and int(value.group(1)) > body_size + 4:
                    return m.group(0)
                removed += 1
                return ""

            segment = re.sub(r"<w:(?:sz|szCs)\b[^>]*/>", drop_size, segment)
        return segment

    def strip_spacing(segment: str) -> str:
        nonlocal removed

        def fix(m: re.Match) -> str:
            nonlocal removed
            tag = m.group(0)
            if "w:line=" not in tag and "w:lineRule=" not in tag:
                return tag
            removed += 1
            tag = re.sub(r'\sw:line(?:Rule)?="[^"]*"', "", tag)
            return "" if re.fullmatch(r"<w:spacing\s*/>", tag) else tag

        return re.sub(r"<w:spacing\b[^>]*/>", fix, segment)

    body = re.search(r"<w:body>(.*)</w:body>", xml, flags=re.DOTALL)
    if not body:
        return 0
    from collections import Counter

    seen = Counter(int(v) for v in re.findall(r'<w:sz\s+w:val="(\d+)"', body.group(1)))
    body_size = seen.most_common(1)[0][0] if seen else 0
    inner = body.group(1)
    # Keep each table's own spacing; strip everywhere else.
    pieces = re.split(r"(<w:tbl>.*?</w:tbl>)", inner, flags=re.DOTALL)
    out = []
    for piece in pieces:
        piece = strip_runs(piece)
        if spacing and not piece.startswith("<w:tbl>"):
            # Only paragraph spacing: run-level <w:spacing> (character spacing) sits inside <w:rPr>.
            piece = re.sub(r"<w:pPr>.*?</w:pPr>", lambda m: re.sub(r"(<w:rPr>.*?</w:rPr>)|(<w:spacing\b[^>]*/>)", lambda mm: mm.group(1) or strip_spacing(mm.group(2)), m.group(0), flags=re.DOTALL), piece, flags=re.DOTALL)
        out.append(piece)
    doc.put(doc.main, xml[: body.start(1)] + "".join(out) + xml[body.end(1) :])
    return removed


# ---------------------------------------------------------------------------
# rules


def _rfonts(font: str) -> str:
    return f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:eastAsia="{font}" w:cs="{font}"/>'


def _set_run_defaults(rpr: str, font: str | None, half_points: int | None) -> str:
    if font:
        rpr = set_child(rpr, _rfonts(font), RPR_ORDER)
    if half_points:
        rpr = set_child(rpr, f'<w:sz w:val="{half_points}"/>', RPR_ORDER)
        rpr = set_child(rpr, f'<w:szCs w:val="{half_points}"/>', RPR_ORDER)
    return rpr


def _set_double_spacing(ppr: str) -> str:
    old = get_child(ppr, "spacing")
    new = with_attrs(old or "<w:spacing/>", line="480", lineRule="auto")
    return set_child(ppr, new, PPR_ORDER)


def ensure_styles(doc: DocxFile) -> None:
    """Give a package without a styles part (some generated files) a minimal one: defaults and Normal."""
    if "word/styles.xml" in doc.parts:
        return
    doc.put(
        "word/styles.xml",
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:styles xmlns:w="{W_NS}"><w:docDefaults><w:rPrDefault><w:rPr></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr></w:pPr></w:pPrDefault></w:docDefaults>' '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style></w:styles>',
    )
    doc.add_override("word/styles.xml", CT_STYLES)
    if "styles.xml" not in (doc.text(doc.rels_name(doc.main)) or ""):
        doc.add_rel(doc.main, REL_STYLES, "styles.xml")


def set_body_text(doc: DocxFile, *, font: str | None = None, size_pt: float | None = None, double: bool = False) -> list[str]:
    """Set the body font, size, and double spacing in the document defaults and the Normal style."""
    if not (font or size_pt or double):
        return []
    ensure_styles(doc)
    styles = doc.text("word/styles.xml")
    half = int(round(size_pt * 2)) if size_pt else None
    done: list[str] = []
    # Document defaults.
    dd = re.search(r"<w:docDefaults\b[^>]*/>|<w:docDefaults\b[^>]*>.*?</w:docDefaults>", styles, flags=re.DOTALL)
    defaults = dd.group(0) if dd else "<w:docDefaults></w:docDefaults>"
    rpr_default = get_child(defaults, "rPrDefault") or "<w:rPrDefault></w:rPrDefault>"
    rpr = get_child(rpr_default, "rPr") or "<w:rPr></w:rPr>"
    rpr_default = set_child(rpr_default, _set_run_defaults(rpr, font, half), ["rPr"])
    defaults = set_child(defaults, rpr_default, DOCDEFAULTS_ORDER)
    if double:
        ppr_default = get_child(defaults, "pPrDefault") or "<w:pPrDefault></w:pPrDefault>"
        ppr = get_child(ppr_default, "pPr") or "<w:pPr></w:pPr>"
        ppr_default = set_child(ppr_default, _set_double_spacing(ppr), ["pPr"])
        defaults = set_child(defaults, ppr_default, DOCDEFAULTS_ORDER)
    if dd:
        styles = styles[: dd.start()] + defaults + styles[dd.end() :]
    else:
        styles = re.sub(r"(<w:styles\b[^>]*>)", lambda m: m.group(1) + defaults, styles, count=1)

    # The Normal (default paragraph) style, which usually overrides the defaults.
    def normal(m: re.Match) -> str:
        block = m.group(0)
        if double:
            ppr = get_child(block, "pPr") or "<w:pPr></w:pPr>"
            block = set_child(block, _set_double_spacing(ppr), STYLE_ORDER)
        rpr = get_child(block, "rPr") or "<w:rPr></w:rPr>"
        return set_child(block, _set_run_defaults(rpr, font, half), STYLE_ORDER)

    styles, n = re.subn(r'<w:style\b(?=[^>]*w:type="paragraph")(?=[^>]*w:default="1")[^>]*>.*?</w:style>', normal, styles, count=1, flags=re.DOTALL)
    doc.put("word/styles.xml", styles)
    if font:
        done.append(f"body font set to {font}")
    if size_pt:
        done.append(f"body text size set to {size_pt:g} pt")
    if double:
        done.append("body text double-spaced")
    return done


def set_page(doc: DocxFile, *, page_size: str | None = None, margin_mm: float | None = None, line_numbers: bool = False) -> list[str]:
    done: list[str] = []
    size = PAGE_SIZES_TWIPS.get((page_size or "").strip().lower())
    margin = int(round(margin_mm * TWIPS_PER_MM)) if margin_mm else None

    def fn(sect: str) -> str:
        if size:
            old = get_child(sect, "pgSz")
            landscape = old is not None and attr(old, "w:orient") == "landscape"
            w, h = (size[1], size[0]) if landscape else size
            sect = set_child(sect, with_attrs(old or "<w:pgSz/>", w=str(w), h=str(h)), SECT_ORDER)
        if margin:
            old = get_child(sect, "pgMar") or '<w:pgMar w:header="708" w:footer="708" w:gutter="0"/>'
            new = with_attrs(old, top=str(margin), right=str(margin), bottom=str(margin), left=str(margin))
            for edge in ("header", "footer"):
                value = attr(new, f"w:{edge}")
                if value is None or int(value) >= margin:
                    new = with_attrs(new, **{edge: str(margin // 2)})
            sect = set_child(sect, new, SECT_ORDER)
        if line_numbers:
            sect = set_child(sect, '<w:lnNumType w:countBy="1" w:restart="continuous"/>', SECT_ORDER)
        return sect

    if size or margin or line_numbers:
        n = doc.edit_sections(fn)
        if size:
            done.append(f"page size set to {page_size} in {n} section(s)")
        if margin:
            done.append(f"margins set to {margin_mm:g} mm")
        if line_numbers:
            done.append("continuous line numbers turned on")
    return done


def copy_page_setup(doc: DocxFile, template: DocxFile) -> list[str]:
    """The template's page size, margins, columns, grid, and line numbering, in every section."""
    t_sect = template.final_section()
    if not t_sect:
        return []
    taken = [(tag, get_child(t_sect, tag)) for tag in ("pgSz", "pgMar", "lnNumType", "pgNumType", "cols", "docGrid")]
    taken = [(tag, x) for tag, x in taken if x]
    if not taken:
        return []

    def fn(sect: str) -> str:
        for tag, x in taken:
            sect = set_child(sect, x, SECT_ORDER)
        return sect

    doc.edit_sections(fn)
    return ["page setup (" + ", ".join({"pgSz": "page size", "pgMar": "margins", "lnNumType": "line numbering", "pgNumType": "page numbering", "cols": "columns", "docGrid": "text grid"}[t] for t, _ in taken) + ") taken from the template"]


# ---------------------------------------------------------------------------
# entry points


def apply_template(doc: DocxFile, template: DocxFile, *, keep_direct: bool = False) -> list[str]:
    done: list[str] = []
    renames, matched = merge_styles(doc, template)
    if template.text("word/styles.xml"):
        done.append(f"styles taken from the template ({matched} of the manuscript's styles matched by name" + (f", {len(renames)} renamed" if renames else "") + ")")
    theme = "word/theme/theme1.xml"
    if theme in template.parts and theme in doc.parts:
        doc.put(theme, template.parts[theme])
        done.append("theme (theme fonts and colors) taken from the template")
    done += copy_page_setup(doc, template)
    copied = copy_headers_footers(doc, template)
    if copied:
        done.append("headers and footers taken from the template: " + ", ".join(copied))
    if not keep_direct:
        n = strip_direct_formatting(doc, fonts=True, sizes=True, spacing=True)
        if n:
            done.append(f"{n} directly set font, size, or line-spacing setting(s) removed so the template's styles apply")
    return done


_WORD_NAMES = {"Times": "Times New Roman", "Times New Roman": "Times New Roman", "Helvetica": "Arial", "Arial": "Arial"}


def body_font(rule_text: str) -> str | None:
    """The font a guideline's font rule names first, as Word calls it (Times -> Times New Roman)."""
    from .layout_check import parse_font_rule

    rule = parse_font_rule(rule_text)
    lower = rule_text.lower()
    named = sorted(rule.families, key=lambda f: (lower.find(f.lower()) if f.lower() in lower else 10**6, f))
    if named:
        return _WORD_NAMES.get(named[0], named[0])
    if "serif" in rule.generics:
        return "Times New Roman"
    if "sans-serif" in rule.generics:
        return "Arial"
    return None


def apply_rules(doc: DocxFile, reqs, *, keep_direct: bool = False) -> tuple[list[str], list[str]]:
    """Apply the venue's layout rules. Returns ``(changes, rules that could not be applied)``."""
    m = reqs.manuscript
    done: list[str] = []
    skipped: list[str] = []
    font = body_font(m.font) if m.font else None
    done += set_body_text(doc, font=font, size_pt=m.font_size_pt, double=bool(m.double_spacing))
    done += set_page(doc, page_size=m.page_size, margin_mm=m.margins_mm, line_numbers=bool(m.line_numbers))
    if m.page_numbers and not has_page_numbers(doc):
        add_page_number_footer(doc)
        done.append("page numbers added in the footer")
    if not keep_direct and (font or m.font_size_pt or m.double_spacing):
        n = strip_direct_formatting(doc, fonts=bool(font), sizes=bool(m.font_size_pt), spacing=bool(m.double_spacing))
        if n:
            done.append(f"{n} directly set font, size, or line-spacing setting(s) removed so the new body text settings apply")
    if m.page_size and m.page_size.strip().lower() not in PAGE_SIZES_TWIPS:
        skipped.append(f"page size '{m.page_size}' is not one paperpush knows; set it by hand")
    return done, skipped
