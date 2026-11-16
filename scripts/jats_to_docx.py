"""Europe PMC JATS full text -> a Word manuscript, as an author would submit it.

pandoc's JATS reader drops the title, authors, figures, and the reference list, so they are
put back as ordinary paragraphs first: title and authors at the top, each figure's legend
("Figure 1. Title. Text") where the figure floats, table captions above their tables, and a
"References" section typeset from the structured <element-citation>/<mixed-citation> records
(Vancouver-style initials; numbered only when the text cites by number). Back matter
(acknowledgements, declarations) goes before the references, and pandoc's own title block is
dropped. Inline math pandoc cannot render is lost, so prose checks around formulas are not
reliable on the result. Used by scripts/validate_benchmark.py for bioRxiv preprints, whose PDFs
cannot be downloaded by scripts. Needs pandoc.

    python -I scripts/jats_to_docx.py in.xml out.docx
"""

import copy
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

XLINK = "{http://www.w3.org/1999/xlink}"


def text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def para(s: str, bold_label: str = "") -> ET.Element:
    p = ET.Element("p")
    if bold_label:
        b = ET.SubElement(p, "bold")
        b.text = bold_label
        b.tail = " " + s
    else:
        p.text = s
    return p


def reference(ref) -> str:
    cit = ref.find("element-citation") or ref.find("mixed-citation") or ref.find("citation")
    if cit is None:
        return text(ref)
    if cit.tag == "mixed-citation" and not cit.findall(".//name"):
        return text(cit)
    names = []
    for n in cit.iter("name"):
        sur, giv = text(n.find("surname")), text(n.find("given-names"))
        if re.search(r"[a-z]", giv):  # a full given name: Vancouver initials
            giv = "".join(w[0] for w in re.split(r"[\s.-]+", giv) if w)
        names.append(f"{sur} {giv}".strip())
    collab = [text(c) for c in cit.iter("collab")]
    authors = ", ".join(names + collab)
    if cit.find(".//etal") is not None:
        authors += ", et al."
    year = text(cit.find("year"))
    title = text(cit.find("article-title")) or text(cit.find("chapter-title"))
    source = text(cit.find("source"))
    vol, issue = text(cit.find("volume")), text(cit.find("issue"))
    fpage, lpage = text(cit.find("fpage")), text(cit.find("lpage"))
    elocation = text(cit.find("elocation-id"))
    doi = next((text(p) for p in cit.iter("pub-id") if p.get("pub-id-type") == "doi"), "")
    parts = [f"{authors} ({year})." if year else f"{authors}."]
    if title:
        parts.append(title.rstrip(".") + ".")
    where = source
    if vol:
        where += f" {vol}" + (f"({issue})" if issue else "")
    if fpage:
        where += f":{fpage}" + (f"-{lpage}" if lpage else "")
    elif elocation:
        where += f":{elocation}"
    if where.strip():
        parts.append(where.strip() + ".")
    if doi:
        parts.append(f"doi:{doi}")
    return " ".join(parts)


def main(src: str, out: str) -> None:
    tree = ET.parse(src)
    root = tree.getroot()
    body = root.find("body")
    meta = root.find("front/article-meta")
    parent = {c: p for p in root.iter() for c in p}

    # Figures and tables: their legends become paragraphs where they float.
    for fig in list(root.iter("fig")):
        label = text(fig.find("label")) or "Figure"
        cap = fig.find("caption")
        title = text(cap.find("title")) if cap is not None else ""
        rest = " ".join(text(p) for p in cap.findall("p")) if cap is not None else ""
        legend = para((title + " " + rest).strip(), label.rstrip(".") + ".")
        host = parent[fig]
        if host.tag == "p":  # a figure inside a paragraph: its legend follows the paragraph
            grand = parent[host]
            grand.insert(list(grand).index(host) + 1, legend)
            host.remove(fig)
        else:
            host.insert(list(host).index(fig), legend)
            host.remove(fig)
    for tw in list(root.iter("table-wrap")):
        label = text(tw.find("label"))
        cap = tw.find("caption")
        if label:
            host = parent[tw]
            cap_text = text(cap) if cap is not None else ""
            target, at = (parent[host], list(parent[host]).index(host)) if host.tag == "p" else (host, list(host).index(tw))
            target.insert(at, para(cap_text, label.rstrip(".") + "."))
        if cap is not None:
            tw.remove(cap)

    # Front matter: title, authors with their affiliations, then the body.
    front = [para(text(meta.find("title-group/article-title")))]
    authors = []
    for c in meta.iter("contrib"):
        if c.get("contrib-type") == "author":
            n = c.find("name")
            if n is not None:
                authors.append(f"{text(n.find('given-names'))} {text(n.find('surname'))}".strip())
            elif c.find("collab") is not None:
                authors.append(text(c.find("collab")))
    if authors:
        front.append(para(", ".join(authors)))
    for note in meta.iter("corresp"):
        front.append(para(text(note)))
    if not any(e for n in meta.iter("corresp") for e in n.iter("email")):
        emails = [text(e) for e in meta.iter("email")]
        if emails:
            front.append(para("Correspondence: " + ", ".join(emails)))
    for aff in meta.iter("aff"):
        aff = copy.deepcopy(aff)
        for holder in list(aff.iter()):
            for child in list(holder):
                if child.tag == "institution-id":  # a ROR link, not printed on the title page
                    tail = child.tail
                    holder.remove(child)
                    if tail:
                        holder.text = (holder.text or "") + tail
        front.append(para(text(aff)))
    abstract = meta.find("abstract")
    if abstract is not None:
        sec = ET.Element("sec")
        ET.SubElement(sec, "title").text = "Abstract"
        for p in abstract.iter("p"):
            sec.append(para(text(p)))
        front.append(sec)
        meta.remove(abstract)
    for extra in meta.findall("trans-abstract") + meta.findall("abstract"):
        meta.remove(extra)
    # pandoc writes these as a title block of its own; the front matter above replaces it.
    for tag in ("title-group", "contrib-group", "pub-date", "author-notes", "aff"):
        for el in meta.findall(tag):
            meta.remove(el)
    for i, el in enumerate(front):
        body.insert(i, el)

    # Back matter an author writes into the manuscript (acknowledgements, declarations, author
    # notes): pandoc drops <back>, so it goes at the end of the body, before the references.
    back = root.find("back")
    if back is not None:
        for el in list(back):
            if el.tag in ("ack", "sec", "notes", "fn-group", "app-group"):
                if el.tag == "fn-group":
                    for fn in el.iter("fn"):
                        for p in fn.findall("p"):
                            body.append(para(text(p)))
                else:
                    el.tag = "sec" if el.tag != "app-group" else el.tag
                    if el.tag == "sec" and el.find("title") is None and el.tag != "app-group":
                        t = ET.Element("title")
                        t.text = "Acknowledgements"
                        el.insert(0, t)
                    if el.tag == "app-group":
                        for app in el.iter("app"):
                            app.tag = "sec"
                            body.append(app)
                    else:
                        body.append(el)
                back.remove(el)

    # The reference list, as a section of its own after the body.
    refs = back.findall(".//ref") if back is not None else []
    if refs:
        sec = ET.SubElement(body, "sec")
        ET.SubElement(sec, "title").text = "References"
        cites = [text(x) for x in root.iter("xref") if x.get("ref-type") == "bibr"]
        numbered = cites and sum(bool(re.fullmatch(r"[\d,\s\u2013-]+", c)) for c in cites) > len(cites) / 2
        for n, ref in enumerate(refs, 1):
            label = text(ref.find("label")).rstrip(".")
            label = label if label.isdigit() else str(n)
            sec.append(para((f"{label}. " if numbered else "") + reference(ref)))
        for rl in back.findall(".//ref-list"):
            parent[rl].remove(rl)

    with tempfile.NamedTemporaryFile("wb", suffix=".xml", delete=False) as tmp:
        tree.write(tmp, encoding="utf-8", xml_declaration=True)
    subprocess.run(["pandoc", "-f", "jats", "-t", "docx", "-o", out, tmp.name], check=True, stderr=subprocess.DEVNULL)
    Path(tmp.name).unlink()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
