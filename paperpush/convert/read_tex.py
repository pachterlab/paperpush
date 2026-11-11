"""Read a LaTeX manuscript into the document model."""

from __future__ import annotations

import re
from pathlib import Path

from . import bib as biblib
from .mathconv import SYMBOLS, latex_to_omml, signature_of_xml
from .model import Appendix, Author, BibEntry, Bibliography, Break, Cell, Cite, Code, Document, Equation, Figure, Heading, Image, Link, ListBlock, Log, Math, Note, Para, Quote, Raw, Ref, Style, Table, Text, merge_text

_CMD = re.compile(r"\\([a-zA-Z@]+\*?|.)", re.S)
_SECTIONS = {"part": 1, "chapter": 1, "section": 1, "subsection": 2, "subsubsection": 3, "paragraph": 4, "subparagraph": 5}
_VERBATIM = {"verbatim", "verbatim*", "lstlisting", "minted", "Verbatim", "comment"}
_MATH_ENVS = {"equation", "equation*", "displaymath", "align", "align*", "gather", "gather*", "multline", "multline*", "eqnarray", "eqnarray*", "flalign", "flalign*", "alignat", "alignat*"}
_ROW_ENVS = {"align", "gather", "multline", "eqnarray", "flalign", "alignat"}
_TRANSPARENT = {"center", "flushleft", "flushright", "minipage", "landscape", "sloppypar", "small", "footnotesize", "scriptsize", "large", "Large", "singlespace", "doublespace", "onehalfspace", "spacing", "adjustbox", "resizebox", "multicols", "frontmatter", "document", "linenomath", "linenumbers", "samepage", "widetext", "appendices", "subappendices"}
_FIGURES = {"figure", "figure*", "sidewaysfigure", "wrapfigure", "SCfigure"}
_TABLES = {"table", "table*", "sidewaystable", "wraptable"}
_TABULARS = {"tabular", "tabular*", "tabularx", "tabulary", "longtable", "longtable*", "supertabular", "xltabular", "tblr"}
_RENDERED = {"tikzpicture", "pgfpicture", "picture", "forest", "tikzcd"}
_LAYOUT_NOARG = {"noindent", "indent", "centering", "raggedright", "raggedleft", "clearpage", "newpage", "cleardoublepage", "pagebreak", "nopagebreak", "bigskip", "medskip", "smallskip", "vfill", "hfill", "hrulefill", "par", "null", "small", "footnotesize", "scriptsize", "tiny", "normalsize", "large", "Large", "LARGE", "huge", "Huge", "normalfont", "rmfamily", "sffamily", "ttfamily", "mdseries", "upshape", "protect", "sloppy", "fussy", "linebreak", "nolinebreak", "xspace", "selectfont", "boldmath", "unboldmath", "ignorespaces", "unskip", "strut", "leavevmode", "balance", "onecolumn", "twocolumn", "doublespacing", "singlespacing", "onehalfspacing", "linenumbers", "nolinenumbers", "maketitle", "tableofcontents", "listoffigures", "listoftables", "frenchspacing", "nonfrenchspacing", "FloatBarrier", "sectionmark", "nobreak", "allowbreak", "selectlanguage", "bfseries", "itshape", "scshape", "em", "bf", "it", "sc", "tt", "rm", "sf", "sl"}
_LAYOUT_ARGS = {"vspace": 1, "vspace*": 1, "hspace": 1, "hspace*": 1, "pagestyle": 1, "thispagestyle": 1, "pagenumbering": 1, "setcounter": 2, "addtocounter": 2, "setlength": 2, "addtolength": 2, "color": 1, "captionsetup": 1, "bibliographystyle": 1, "addcontentsline": 3, "markboth": 2, "markright": 1, "fontsize": 2, "linespread": 1, "theoremstyle": 1, "newtheorem": 2, "newtheorem*": 2, "DeclareMathOperator": 2, "DeclareMathOperator*": 2, "graphicspath": 1, "input": 1, "phantomsection": 0, "nocite": 1, "hypersetup": 1, "definecolor": 3, "needspace": 1, "enlargethispage": 1, "enlargethispage*": 1, "Alph": 1, "alph": 1, "arabic": 1, "roman": 1, "Roman": 1, "newcolumntype": 2, "rule": 2, "specialrule": 3, "arrayrulecolor": 1, "cellcolor": 1, "extracolsep": 1}
_LITERALS = {"onedot": ".", "LaTeX": "LaTeX", "TeX": "TeX", "ldots": "…", "dots": "…", "textellipsis": "…", "textdegree": "°", "textmu": "µ", "textbackslash": "\\", "textasciitilde": "~", "textasciicircum": "^", "textbar": "|", "textless": "<", "textgreater": ">", "textbullet": "•", "textperiodcentered": "·", "texttimes": "×", "textpm": "±", "textregistered": "®", "texttrademark": "™", "textcopyright": "©", "textsection": "§", "S": "§", "P": "¶", "dag": "†", "ddag": "‡", "textdagger": "†", "textdaggerdbl": "‡", "pounds": "£", "euro": "€", "textendash": "–", "textemdash": "—", "textquoteleft": "‘", "textquoteright": "’", "textquotedblleft": "“", "textquotedblright": "”", "quad": "\u2003", "qquad": "\u2003\u2003", "enspace": "\u2002", "thinspace": "\u2009", "nobreakspace": "\u00a0", "slash": "/", "ss": "ß", "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ", "aa": "å", "AA": "Å", "o": "ø", "O": "Ø", "l": "ł", "L": "Ł", "i": "ı", "j": "ȷ"}
_STYLES = {"textbf": "strong", "textit": "emph", "emph": "emph", "textsl": "emph", "texttt": "code", "textsc": "smallcaps", "underline": "underline", "uline": "underline", "textsuperscript": "sup", "textsubscript": "sub"}
_GROUP_STYLES = {"bf": "strong", "bfseries": "strong", "it": "emph", "itshape": "emph", "em": "emph", "sl": "emph", "sc": "smallcaps", "scshape": "smallcaps", "tt": "code", "ttfamily": "code"}
_TRANSPARENT_ARG = {"textrm", "textsf", "textnormal", "textmd", "textup", "mbox", "hbox", "text", "fbox", "textls", "foreignlanguage", "makebox", "NoCaseChange", "protect", "acrshort", "gls"}
_CITE = re.compile(r"(?:newcite|shortcite|citeN|citeA|[cC]ite(?:t|p|alp|alt|num|author|year|yearpar|url)?\*?|[pP]arencite\*?|[tT]extcite\*?|[aA]utocite\*?|[fF]ootcite\*?|[sS]martcite\*?|supercite)$")
_REFS = {"ref": "ref", "eqref": "eqref", "pageref": "page", "nameref": "ref", "vref": "ref", "autoref": "auto", "cref": "auto", "Cref": "auto", "labelcref": "ref", "subref": "ref"}
_REF_NAMES = {"fig": "Figure", "figure": "Figure", "tab": "Table", "tbl": "Table", "table": "Table", "eq": "Equation", "eqn": "Equation", "equation": "Equation", "sec": "Section", "section": "Section", "ssec": "Section", "subsec": "Section", "chap": "Chapter", "app": "Appendix", "appendix": "Appendix", "thm": "Theorem", "lem": "Lemma", "alg": "Algorithm", "prop": "Proposition", "cor": "Corollary", "def": "Definition"}
_DEFAULT_THEOREMS = {"proof": "Proof"}
_BLOCK_IN_GROUP = re.compile(r"\\(?:begin\{|section\b|subsection\b|bibliography\b|printbibliography\b)|\n[ \t]*\n")
_AUTHOR_YEAR_BST = re.compile(r"(?i)plainnat|abbrvnat|unsrtnat$|apalike|apa|chicago|harvard|agsm|dcu|authordate|elsarticle-harv|ama-harv|natbib-harv|cell|genome")


def read_braced(src: str, i: int) -> tuple[str, int] | None:
    """The contents of the ``{...}`` at or after ``i`` and the index past it."""
    while i < len(src) and src[i] in " \t\n":
        i += 1
    if i >= len(src) or src[i] != "{":
        return None
    depth, j = 0, i
    while j < len(src):
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1 : j], j + 1
        j += 1
    return None


def read_optional(src: str, i: int) -> tuple[str | None, int]:
    """The contents of a ``[...]`` at or after ``i`` (or None) and the index past it."""
    j = i
    while j < len(src) and src[j] in " \t\n":
        j += 1
    if j >= len(src) or src[j] != "[":
        return None, i
    depth, k, brace = 0, j, 0
    while k < len(src):
        ch = src[k]
        if ch == "\\":
            k += 2
            continue
        brace += (ch == "{") - (ch == "}")
        if brace == 0:
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return src[j + 1 : k], k + 1
        k += 1
    return None, i


def strip_comments(src: str, log: Log | None = None) -> str:
    """Remove ``%`` comments as TeX does: the line end goes too, and the next line's indent."""
    out: list[str] = []
    verbatim = None
    skip_indent = False
    dropped = 0
    for line in src.split("\n"):
        if skip_indent:
            line = line.lstrip(" \t")
            skip_indent = False
        if verbatim:
            out.append(line + "\n")
            if f"\\end{{{verbatim}}}" in line:
                verbatim = None
            continue
        begin = re.search(r"\\begin\{(" + "|".join(re.escape(v) for v in _VERBATIM) + r")\}", line)
        if begin and f"\\end{{{begin.group(1)}}}" not in line:
            verbatim = begin.group(1)
            out.append(line + "\n")
            continue
        masked = re.sub(r"\\verb(.).*?\1|\\(?:url|href)\{[^{}]*\}|\\\\", lambda m: "x" * len(m.group(0)), line)
        m = re.search(r"(?<!\\)%", masked)
        if m:
            kept = line[: m.start()]
            if line[m.start() + 1 :].strip():
                dropped += 1
            if kept.strip():
                out.append(kept)
            elif out and not out[-1].endswith("\n"):
                pass
            skip_indent = True
        else:
            out.append(line + "\n")
    if log is not None and dropped:
        log.change(f"dropped {dropped} LaTeX comment(s), which have no place in the output")
    result = "".join(out)
    # split() saw one more line than the text has; a stray newline after \\input would break a paragraph.
    return result[:-1] if result.endswith("\n") else result


def find_end(src: str, env: str, i: int) -> tuple[int, int]:
    """``(start of \\end{env}, index past it)`` for the environment whose body starts at ``i``."""
    begin, end = f"\\begin{{{env}}}", f"\\end{{{env}}}"
    depth = 1
    pat = re.compile(re.escape(begin) + "|" + re.escape(end))
    for m in pat.finditer(src, i):
        depth += 1 if m.group(0) == begin else -1
        if depth == 0:
            return m.start(), m.end()
    return len(src), len(src)


def split_top(src: str, sep_re: str) -> list[str]:
    """Split ``src`` on a pattern, ignoring matches inside braces, math, or environments."""
    pat = re.compile(sep_re)
    out, depth, env, start, i = [], 0, 0, 0, 0
    while i < len(src):
        ch = src[i]
        if depth == 0 and env == 0:
            m = pat.match(src, i)
            if m and m.end() > i:
                out.append(src[start:i])
                i = start = m.end()
                continue
        if ch == "\\":
            if src.startswith("\\begin{", i):
                env += 1
            elif src.startswith("\\end{", i):
                env -= 1
            i += 2
            continue
        depth += (ch == "{") - (ch == "}")
        i += 1
    out.append(src[start:])
    return out


class TexReader:
    def __init__(self, main: Path, log: Log, *, bib: Path | None = None) -> None:
        self.main = main
        self.root = main.parent
        self.log = log
        self.bib_path = bib
        self.macros: dict[str, tuple[int, str | None, str]] = {}
        self.theorems: dict[str, str] = dict(_DEFAULT_THEOREMS)
        self.theorem_counts: dict[str, int] = {}
        self.theorem_counter: dict[str, str] = {}
        self.extra_labels: dict[str, str] = {}
        self.env_counts: dict[str, int] = {}
        self.graphics_dirs: list[str] = [""]
        self.unknown: dict[str, int] = {}
        self.sub_labels: dict[str, tuple[str, str]] = {}
        self.user_envs: set[str] = set()
        self.body_definitions: list[str] = []
        self.preamble = ""

    # -- loading ---------------------------------------------------------------

    def load(self, path: Path, seen: set[Path], log: Log | None = None) -> str:
        path = path.resolve()
        if path in seen:
            return ""
        seen.add(path)
        text = strip_comments(path.read_text(encoding="utf-8", errors="replace"), log)
        text = re.sub(r"\\iffalse\b.*?\\fi\b", "", text, flags=re.S)

        def include(m: re.Match) -> str:
            name = m.group(3).strip()
            base = path.parent / (m.group(2) or "")
            for candidate in (base / name, base / (name + ".tex"), self.root / (m.group(2) or "") / name, self.root / (m.group(2) or "") / (name + ".tex")):
                if candidate.is_file():
                    return self.load(candidate, seen, log)
            self.log.warn(f"could not find the included file '{name}'")
            return ""

        return re.sub(r"\\(input|include|subfile|import|subimport)(?:\{([^{}]*/)\})?\{([^{}]+)\}", include, text)

    def collect_macros(self, src: str) -> None:
        for m in re.finditer(r"\\(?:re)?newcommand\*?|\\providecommand\*?|\\DeclareRobustCommand\*?", src):
            i = m.end()
            named = read_braced(src, i)
            if named:
                name, i = named[0].strip(), named[1]
            else:
                n = re.compile(r"\s*(\\[a-zA-Z@]+)").match(src, i)
                if not n:
                    continue
                name, i = n.group(1), n.end()
            count, i = read_optional(src, i)
            default, i = read_optional(src, i)
            body = read_braced(src, i)
            if body and name.startswith("\\"):
                self.macros[name[1:]] = (int(count or 0), default, body[0])
        for m in re.finditer(r"\\def\s*\\([a-zA-Z@]+)((?:#\d)*)\s*(?=\{)", src):
            body = read_braced(src, m.end())
            if body:
                self.macros[m.group(1)] = (m.group(2).count("#"), None, body[0])
        for m in re.finditer(r"\\DeclareMathOperator(\*?)\s*\{\\([a-zA-Z]+)\}\s*\{((?:[^{}]|\{[^{}]*\})*)\}", src):
            self.macros[m.group(2)] = (0, None, f"\\operatorname{m.group(1)}{{{m.group(3)}}}")
        for m in re.finditer(r"\\newtheorem\*?\s*\{([^{}]+)\}\s*(?:\[([^\]]*)\])?\s*\{([^{}]+)\}", src):
            self.theorems[m.group(1)] = m.group(3)
            if m.group(2):
                self.theorem_counter[m.group(1)] = m.group(2)
        for m in re.finditer(r"\\newenvironment\*?\s*\{([^{}]+)\}", src):
            self.user_envs.add(m.group(1))
        g = re.search(r"\\graphicspath\s*\{((?:\s*\{[^{}]*\})+)\s*\}", src)
        if g:
            self.graphics_dirs += re.findall(r"\{([^{}]*)\}", g.group(1))

    def collect_package_macros(self, preamble: str) -> None:
        """Simple macros from a venue's own .sty or .cls beside the manuscript (\\\\eg in CVPR's)."""
        names = re.findall(r"\\(?:usepackage|documentclass)(?:\[[^\]]*\])?\{([^{}]+)\}", preamble)
        own, self.macros = self.macros, {}
        for group in names:
            for name in group.split(","):
                for ext in (".sty", ".cls"):
                    path = self.root / (name.strip() + ext)
                    if path.is_file():
                        self.collect_macros(strip_comments(path.read_text(encoding="utf-8", errors="replace")))
        reserved = set(_SECTIONS) | set(_STYLES) | set(_REFS) | set(_LITERALS) | {"abstract", "caption", "cite", "label", "item", "footnote", "title", "author", "maketitle", "bibliography", "appendix", "keywords", "thanks", "and", "url", "href"}
        kept = {k: v for k, v in self.macros.items() if "@" not in k and "@" not in v[2] and k not in reserved}
        self.macros = {**kept, **own}

    def expand(self, src: str, depth: int = 0) -> str:
        """Expand the document's own macros in ``src``."""
        if not self.macros or depth > 12:
            return src
        out: list[str] = []
        i = 0
        changed = False
        while i < len(src):
            if src[i] != "\\":
                j = src.find("\\", i)
                j = len(src) if j < 0 else j
                out.append(src[i:j])
                i = j
                continue
            m = _CMD.match(src, i)
            if not m:
                out.append(src[i])
                i += 1
                continue
            name = m.group(1)
            macro = self.macros.get(name)
            if macro is None:
                out.append(m.group(0))
                i = m.end()
                continue
            count, default, body = macro
            j = m.end()
            args: list[str] = []
            if default is not None:
                value, j = read_optional(src, j)
                args.append(default if value is None else value)
            ok = True
            while len(args) < count:
                got = read_braced(src, j)
                if got is not None:
                    args.append(got[0])
                    j = got[1]
                    continue
                k = j
                while k < len(src) and src[k] in " \t\n":
                    k += 1
                if k >= len(src):
                    ok = False
                    break
                tok = _CMD.match(src, k)
                args.append(tok.group(0) if tok else src[k])
                j = tok.end() if tok else k + 1
            if not ok:
                out.append(m.group(0))
                i = m.end()
                continue
            for n, value in enumerate(args, 1):
                body = body.replace(f"#{n}", value)
            # A macro name ending in a letter swallows the spaces after it.
            if count == 0 and src[m.end() : m.end() + 1] == "{" and src[m.end() : m.end() + 2] == "{}":
                j = m.end() + 2
            out.append(body)
            i = j
            changed = True
        result = "".join(out)
        return self.expand(result, depth + 1) if changed else result

    # -- the document ----------------------------------------------------------

    def read(self) -> Document:
        text = self.load(self.main, set(), self.log)
        split = re.search(r"\\begin\{document\}", text)
        if not split:
            raise ValueError(f"{self.main.name} has no \\begin{{document}}")
        preamble = text[: split.start()]
        end = re.search(r"\\end\{document\}", text)
        body = text[split.end() : end.start() if end else len(text)]
        self.preamble = preamble
        self.collect_package_macros(preamble)
        self.collect_macros(preamble)
        self.collect_macros(body)
        doc = Document(preamble=preamble, macros=dict(self.macros))
        body = self.front_matter(doc, preamble, body)
        doc.blocks = self.merge_bibliographies(self.blocks(body))
        doc.extra_labels = self.extra_labels
        if self.body_definitions:
            doc.preamble = doc.preamble.rstrip() + "\n" + "\n".join(self.body_definitions) + "\n"
        self.resolve_sub_labels(doc)
        self.load_bibliography(doc, text)
        self.detect_citation_style(doc, text)
        for name, n in sorted(self.unknown.items()):
            self.log.warn(f"unknown LaTeX command \\{name} ({n} use(s)): its arguments were kept as text. Define it with \\newcommand in the preamble, or check the result")
        return doc

    def merge_bibliographies(self, blocks: list) -> list:
        """\\bibliographystyle and \\bibliography can land apart; a paper has one reference list."""
        found = [b for b in blocks if isinstance(b, Bibliography)]
        if len(found) < 2:
            return blocks
        found[-1].source = "\n".join(b.source for b in found)
        return [b for b in blocks if not isinstance(b, Bibliography) or b is found[-1]]

    def front_matter(self, doc: Document, preamble: str, body: str) -> str:
        """Fill in the title block, and return the body without it."""
        both = preamble + "\n" + body
        m = re.search(r"\\title\b", both)
        if m:
            _, i = read_optional(both, m.end())
            got = read_braced(both, i)
            if got:
                doc.title = self.inlines(re.sub(r"\\thanks\{[^{}]*\}|\\\\", " ", got[0]))
        self.read_authors(doc, both)
        last = 0

        def consume(match_end: int) -> None:
            nonlocal last
            last = max(last, match_end)

        m = re.search(r"\\begin\{abstract\}", body)
        if m:
            end, after = find_end(body, "abstract", m.end())
            doc.abstract = self.blocks(body[m.end() : end])
            consume(after)
        else:
            m = re.search(r"\\abstract\s*\{", both)
            if m:
                got = read_braced(both, m.end() - 1)
                if got:
                    doc.abstract = self.blocks(got[0])
                    start_in_body = m.start() - len(preamble) - 1
                    if start_in_body >= 0:
                        consume(got[1] - len(preamble) - 1)
        for pat, env in ((r"\\begin\{keywords\}", "keywords"), (r"\\begin\{IEEEkeywords\}", "IEEEkeywords"), (r"\\begin\{keyword\}", "keyword")):
            m = re.search(pat, body)
            if m:
                end, after = find_end(body, env, m.end())
                doc.keywords = self.split_keywords(body[m.end() : end])
                consume(after)
                break
        else:
            m = re.search(r"\\(?:keywords|keyword|kwd)\s*\{", both)
            if m:
                got = read_braced(both, m.end() - 1)
                if got:
                    doc.keywords = self.split_keywords(got[0])
                    if m.start() > len(preamble):
                        consume(got[1] - len(preamble) - 1)
        for m in re.finditer(r"\\maketitle\b", body):
            consume(m.end())
        for name in ("title", "author", "affil", "affiliation", "address", "email", "date", "corres"):
            for m in re.finditer(rf"\\{name}\b", body):
                section = re.search(r"\\(?:section|chapter)\b", body)
                if section and m.start() > section.start():
                    break
                _, i = read_optional(body, m.end())
                got = read_braced(body, i)
                if got:
                    consume(got[1])
        doc.front_source = body[:last].strip()
        return body[last:]

    def split_keywords(self, text: str) -> list[str]:
        text = re.sub(r"\\sep\b|\\and\b|\\kwd\b|\\texttt|\\emph", ",", text)
        words = re.split(r"\s*[,;·•]\s*|\s*\\\\\s*", biblib.clean(self.expand(text)))
        return [w.strip().rstrip(".") for w in words if w.strip()]

    def read_authors(self, doc: Document, both: str) -> None:
        labelled: dict[str, int] = {}
        for m in re.finditer(r"\\(affil|affiliation|address|institute)\b", both):
            label, i = read_optional(both, m.end())
            got = read_braced(both, i)
            if not got:
                continue
            text = biblib.clean(re.sub(r"\\(?:\\|and)|\\(?:orgdiv|orgname|orgaddress|street|city|state|postcode|country)\b", ", ", self.expand(got[0])))
            text = re.sub(r"(?:,\s*)+", ", ", text).strip(", ")
            if text and text not in doc.affiliations:
                doc.affiliations.append(text)
            if label is not None:
                labelled[label.strip()] = doc.affiliations.index(text) + 1 if text else 0
        for m in re.finditer(r"\\author\b", both):
            label, i = read_optional(both, m.end())
            got = read_braced(both, i)
            if not got:
                continue
            raw = self.expand(got[0])
            for piece in re.split(r"\\and\b|\\And\b|\\AND\b", raw):
                self.add_author(doc, piece, label, labelled)
        emails = re.findall(r"\\(?:email|ead)(?:\[[^\]]*\])?\s*\{([^{}]+)\}", both)
        for author, email in zip([a for a in doc.authors if not a.email], emails):
            author.email = email.strip()

    def add_author(self, doc: Document, piece: str, label: str | None, labelled: dict[str, int]) -> None:
        email = re.search(r"([\w.+-]+@[\w.-]+\w)", piece)
        piece = re.sub(r"\\(?:thanks|footnote|orcid|orcidlink|inst|corref|fnref)\s*(?:\[[^\]]*\])?\s*\{[^{}]*\}", "", piece)
        lines = [line.strip() for line in re.split(r"\\\\(?:\[[^\]]*\])?|\\newline\b", piece) if line.strip()]
        if not lines:
            return
        name_line = lines[0]
        sup = re.findall(r"\\textsuperscript\{([^{}]*)\}|\$\^\{?([^${}]*)\}?\$", name_line)
        name_line = re.sub(r"\\textsuperscript\{[^{}]*\}|\$\^\{?[^${}]*\}?\$", "", name_line)
        names = [n.strip() for n in re.split(r",\s*|\s+and\s+", biblib.clean(name_line)) if n.strip()]
        affils: list[int] = []
        for line in lines[1:]:
            text = biblib.clean(re.sub(r"\\(?:texttt|emph|textit)\{[^{}]*@[^{}]*\}|\S+@\S+", "", line)).strip(", ")
            if text:
                if text not in doc.affiliations:
                    doc.affiliations.append(text)
                affils.append(doc.affiliations.index(text) + 1)
        for tag in re.split(r"\s*,\s*", label or ""):
            if tag.strip() in labelled and labelled[tag.strip()]:
                affils.append(labelled[tag.strip()])
            elif tag.strip().isdigit() and int(tag) <= len(doc.affiliations):
                affils.append(int(tag))
        for mark in sup:
            for tag in re.split(r"\s*,\s*", mark[0] or mark[1]):
                if tag.strip().isdigit():
                    affils.append(int(tag))
        for name in names:
            doc.authors.append(Author(name=name, affiliations=sorted(set(affils)), email=email.group(1) if email and len(names) == 1 else ""))

    # -- bibliography ----------------------------------------------------------

    def load_bibliography(self, doc: Document, text: str) -> None:
        names: list[str] = []
        for m in re.finditer(r"\\bibliography\s*\{([^{}]+)\}", text):
            names += [n.strip() for n in m.group(1).split(",") if n.strip()]
        names += [m.group(1).strip() for m in re.finditer(r"\\addbibresource(?:\[[^\]]*\])?\s*\{([^{}]+)\}", text)]
        paths: list[Path] = []
        missing: list[str] = []
        if self.bib_path is not None:
            paths.append(self.bib_path)
        for name in names:
            for candidate in (self.root / name, self.root / (name + ".bib")):
                if candidate.is_file():
                    paths.append(candidate)
                    break
            else:
                missing.append(name)
        for path in paths:
            contents = path.read_text(encoding="utf-8", errors="replace")
            doc.bib_source[path.name] = contents
            doc.bib.update(biblib.parse_bib(contents))
        m = re.search(r"\\begin\{thebibliography\}", text)
        if m:
            end, _ = find_end(text, "thebibliography", m.end())
            doc.bib.update(self.parse_bibitems(text[m.end() : end]))
        if not doc.bib:
            bbl = self.main.with_suffix(".bbl")
            if bbl.is_file():
                raw = bbl.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"\\begin\{thebibliography\}", raw)
                if m:
                    end, _ = find_end(raw, "thebibliography", m.end())
                    doc.bib.update(self.parse_bibitems(raw[m.end() : end]))
                    self.log.change(f"took the references from {bbl.name}")
        if not doc.bib:
            for name in missing:
                self.log.warn(f"could not find the bibliography '{name}'")

    def parse_bibitems(self, body: str) -> dict[str, BibEntry]:
        out: dict[str, BibEntry] = {}
        for piece in re.split(r"(?=\\bibitem\b)", body):
            m = re.match(r"\\bibitem\s*", piece)
            if not m:
                continue
            _, i = read_optional(piece, m.end())
            got = read_braced(piece, i)
            if not got:
                continue
            text = re.sub(r"\\(?:newblock|bibinfo\{[^{}]*\}|bibfield\{[^{}]*\})", " ", piece[got[1] :])
            out[got[0].strip()] = BibEntry(key=got[0].strip(), text=biblib.clean(self.expand(text)))
        return out

    def detect_citation_style(self, doc: Document, text: str) -> None:
        m = re.search(r"\\bibliographystyle\s*\{([^{}]+)\}", text)
        natbib = re.search(r"\\usepackage(?:\[([^\]]*)\])?\{natbib\}", text)
        biblatex = re.search(r"\\usepackage\[([^\]]*)\]\{biblatex\}", text)
        style = "numeric"
        if biblatex and re.search(r"style\s*=\s*(?:authoryear|apa|chicago)", biblatex.group(1)):
            style = "author-year"
        elif natbib and natbib.group(1) and "numbers" in natbib.group(1):
            style = "numeric"
        elif natbib and natbib.group(1) and re.search(r"authoryear|round", natbib.group(1)):
            style = "author-year"
        elif m and _AUTHOR_YEAR_BST.search(m.group(1)) and not re.search(r"unsrt", m.group(1)):
            style = "author-year"
        doc.citation_style = style

    # -- blocks ----------------------------------------------------------------

    def blocks(self, src: str) -> list:
        out: list = []
        i, n, depth = 0, len(src), 0
        start = 0

        def flush(end: int) -> None:
            nonlocal start
            chunk = src[start:end]
            if chunk.strip():
                children = self.inlines(chunk)
                if any(not (isinstance(c, Text) and not c.text.strip()) for c in children):
                    out.append(Para(children=trim(children), source=chunk.strip()))
            start = end

        while i < n:
            ch = src[i]
            if ch == "\n" and depth == 0 and re.compile(r"\n[ \t]*\n").match(src, i):
                flush(i)
                m = re.compile(r"(?:[ \t]*\n)+").match(src, i)
                i = start = m.end()
                continue
            if ch == "{":
                if depth == 0 and not src[start:i].strip():
                    group = read_braced(src, i)
                    if group and _BLOCK_IN_GROUP.search(group[0]):  # {\\small \\bibliography{...}}
                        out.extend(self.blocks(group[0]))
                        i = start = group[1]
                        continue
                depth += 1
            elif ch == "}":
                depth = max(0, depth - 1)
            elif ch == "$" and src.startswith("$$", i) and depth == 0:
                end = src.find("$$", i + 2)
                end = n if end < 0 else end
                flush(i)
                out.append(self.equation("displaymath", src[i + 2 : end], src[i : end + 2]))
                i = start = min(n, end + 2)
                continue
            elif ch == "\\":
                m = _CMD.match(src, i)
                if not m:
                    i += 1
                    continue
                name = m.group(1)
                if name == "verb" or name == "verb*":
                    delim = src[m.end()] if m.end() < n else ""
                    end = src.find(delim, m.end() + 1)
                    i = (end + 1) if end > 0 else m.end()
                    continue
                if depth == 0:
                    handled = self.block_command(name, m, src, out, flush)
                    if handled is not None:
                        i = start = handled
                        continue
                i = m.end()
                continue
            i += 1
        flush(n)
        return out

    def block_command(self, name: str, m: re.Match, src: str, out: list, flush) -> int | None:
        i = m.start()
        base = name.rstrip("*")
        if base in _DEFINITIONS:  # a definition in the body belongs with the preamble
            end = _definition_end(base, src, m.end())
            if end is not None:
                flush(i)
                self.body_definitions.append(src[i:end])
                return end
        if base in _SECTIONS:
            flush(i)
            _, j = read_optional(src, m.end())
            got = read_braced(src, j)
            if not got:
                return m.end()
            end = got[1]
            label = re.compile(r"\s*\\label\s*\{([^{}]+)\}").match(src, end)
            title = got[0]
            inner = re.search(r"\\label\s*\{([^{}]+)\}", title)
            tag = inner.group(1) if inner else ""
            title = re.sub(r"\\label\s*\{[^{}]+\}", "", title)
            if label:
                tag = label.group(1)
                end = label.end()
            level = _SECTIONS[base]
            if "\\chapter" in self.preamble or re.search(r"\\documentclass(?:\[[^\]]*\])?\{(?:book|report|memoir)\}", self.preamble):
                level = {"part": 1, "chapter": 1, "section": 2, "subsection": 3, "subsubsection": 4, "paragraph": 5, "subparagraph": 6}[base]
            out.append(Heading(level=level, children=trim(self.inlines(title)), label=tag, numbered=not name.endswith("*") and base not in ("paragraph", "subparagraph"), source=src[i:end]))
            return end
        if name == "appendix":
            flush(i)
            out.append(Appendix(source="\\appendix"))
            return m.end()
        if name == "[":
            end = src.find("\\]", m.end())
            end = len(src) if end < 0 else end
            flush(i)
            out.append(self.equation("displaymath", src[m.end() : end], src[i : end + 2]))
            return min(len(src), end + 2)
        if name in ("bibliography", "printbibliography", "bibliographystyle"):
            flush(i)
            end = m.end()
            if name == "bibliographystyle":
                got = read_braced(src, end)
                end = got[1] if got else end
                nxt = re.compile(r"\s*\\bibliography\s*\{[^{}]*\}").match(src, end)
                if not nxt:
                    return end if any(isinstance(b, Bibliography) for b in out) else self._bib_block(out, src[i:end], end)
                end = nxt.end()
            elif name == "bibliography":
                got = read_braced(src, end)
                end = got[1] if got else end
                if out and isinstance(out[-1], Bibliography):
                    out[-1].source += "\n" + src[i:end]
                    return end
            else:
                opt, end = read_optional(src, end)
            return self._bib_block(out, src[i:end], end)
        if name == "begin":
            got = read_braced(src, m.end())
            if not got:
                return None
            env = got[0]
            if env in _VERBATIM:
                flush(i)
                end_at = src.find(f"\\end{{{env}}}", got[1])
                end_at = len(src) if end_at < 0 else end_at
                after = min(len(src), end_at + len(env) + 6)
                if env != "comment":
                    out.append(self.code(env, src[got[1] : end_at], src[i:after]))
                return after
            flush(i)
            end_at, after = find_end(src, env, got[1])
            out.extend(self.environment(env, src[got[1] : end_at], src[i:after]))
            return after
        return None

    def _bib_block(self, out: list, source: str, end: int) -> int:
        out.append(Bibliography(source=source))
        return end

    def code(self, env: str, body: str, source: str) -> Code:
        lang = ""
        text = body
        opt, j = read_optional(body, 0)
        if env == "lstlisting" and opt is not None:
            m = re.search(r"language\s*=\s*(\w+)", opt)
            lang = m.group(1) if m else ""
            text = body[j:]
        if env == "minted":
            got = read_braced(body, j)
            if got:
                lang, text = got[0], body[got[1] :]
        return Code(text=text.strip("\n"), lang=lang, source=source)

    def equation(self, env: str, body: str, source: str) -> Equation:
        base = env.rstrip("*")
        expanded = self.expand(body)
        tagged = bool(re.search(r"\\tag\*?\s*\{", expanded))  # \\tag numbers a row of a starred environment
        starred = (env.endswith("*") or env == "displaymath") and not tagged
        rows_env = base in _ROW_ENVS
        if base == "alignat":
            got = read_braced(expanded, 0)
            expanded = expanded[got[1] :] if got else expanded
        labels: list[str] = []
        if rows_env:
            rows = split_top(expanded, r"\\\\(?:\[[^\]]*\])?")
            rows = [r for r in rows if r.strip()]
            clean_rows = []
            carried = ""  # a \label on a \nonumber row names the next number shown
            for row in rows:
                label = re.search(r"\\label\s*\{([^{}]+)\}", row)
                numbered = not re.search(r"\\(?:nonumber|notag)\b", row) and (not tagged or bool(re.search(r"\\tag\b", row)))
                row = re.sub(r"\\label\s*\{[^{}]+\}|\\(?:nonumber|notag)\b", "", row).strip()
                clean_rows.append(row)
                if not starred and numbered:
                    labels.append(label.group(1) if label else carried)
                    carried = ""
                elif label:
                    carried = label.group(1)
            if carried and labels and not labels[-1]:
                labels[-1] = carried
            latex = " \\\\\n".join(clean_rows)
            if base == "multline":
                labels = labels[-1:] if labels else [""]
        else:
            label = re.search(r"\\label\s*\{([^{}]+)\}", expanded)
            latex = re.sub(r"\\label\s*\{[^{}]+\}", "", expanded).strip()
            if not starred:
                labels = [label.group(1) if label else ""]
            if re.search(r"\\(?:nonumber|notag)\b", latex):
                labels = []
        omml, unsupported = latex_to_omml(latex, rows=rows_env)
        self.note_unsupported(unsupported)
        return Equation(latex=latex, env="align" if base in ("align", "eqnarray", "flalign", "alignat") else (base if rows_env else "equation"), row_labels=labels, numbered=bool(labels), sig=signature_of_xml(omml), source=source)

    def note_unsupported(self, unsupported: set[str]) -> None:
        for cmd in sorted(unsupported):
            self.log.warn(f"the math command {cmd} has no Word equivalent: it appears as literal text in the equation")

    def environment(self, env: str, body: str, source: str) -> list:
        if env in _MATH_ENVS:
            return [self.equation(env, body, source)]
        if env in ("itemize", "enumerate", "description", "compactitem", "compactenum", "inparaenum", "tasks"):
            return [self.list_block(env, body, source)]
        if env in ("quote", "quotation", "verse", "displayquote"):
            return [Quote(blocks=self.blocks(body), source=source)]
        if env in _FIGURES:
            return self.figure(body, source)
        if env in _TABLES:
            return self.table(body, source)
        if env in _TABULARS:
            return self.table(source, source)
        if env in ("threeparttable", "ThreePartTable"):
            return self.table(body, source)
        if env in _RENDERED:
            return [Figure(images=[Image(path="", tikz=source)], numbered=False, source=source)]
        if env == "thebibliography":
            return [Bibliography(source=source)]
        base = env.rstrip("*")
        if base in self.theorems:
            return self.theorem(base, env, body, source)
        if env in _TRANSPARENT or env in self.user_envs:
            if env == "appendices":
                return [Appendix(source="\\appendix")] + self.blocks(body)
            _, i = read_optional(body, 0)
            if env in ("minipage", "spacing", "multicols", "adjustbox", "resizebox"):
                got = read_braced(body, i)
                i = got[1] if got else i
            return self.blocks(body[i:])
        self.log.warn(f"no Word equivalent for the environment {{{env}}}: kept as LaTeX source, and shown as a placeholder in Word")
        labels = re.findall(r"\\label\s*\{([^{}]+)\}", source)
        if labels:
            self.env_counts[env] = self.env_counts.get(env, 0) + 1
            for label in labels:
                self.extra_labels[label] = str(self.env_counts[env])
        return [Raw(tex=source, note=env, source=source)]

    def theorem(self, base: str, env: str, body: str, source: str) -> list:
        title = self.theorems[base]
        note, i = read_optional(body, 0)
        rest = body[i:]
        own = re.match(r"\s*\\label\s*\{([^{}]+)\}", rest)
        if own:
            rest = rest[own.end() :]
        if base == "proof":
            lead = Style("emph", [Text(f"{title}. ")])
        else:
            counter = self.theorem_counter.get(base, base)
            if counter in self.theorems or counter == base:
                self.theorem_counts[counter] = self.theorem_counts.get(counter, 0) + 1
            shown = self.theorem_counts.get(counter, 1)
            number = "" if env.endswith("*") else f" {shown}"
            if own:
                self.extra_labels[own.group(1)] = str(shown)
            lead = Style("strong", [Text(f"{title}{number}" + (f" ({biblib.clean(note)})" if note else "") + ". ")])
        blocks = self.blocks(rest)
        if blocks and isinstance(blocks[0], Para):
            blocks[0].children = [lead] + blocks[0].children
            blocks[0].source = ""
        else:
            blocks.insert(0, Para(children=[lead]))
        blocks[0].source = source
        blocks[0].group = len(blocks)  # restored as one environment, or not at all
        for block in blocks[1:]:
            block.source = ""
        return blocks

    def list_block(self, env: str, body: str, source: str) -> ListBlock:
        pieces = split_top(body, r"\\item\b")
        items = []
        for piece in pieces[1:]:
            label, i = read_optional(piece, 0)
            blocks = self.blocks(piece[i:]) or [Para(children=[])]
            if label is not None and isinstance(blocks[0], Para):
                blocks[0].children = [Style("strong", self.inlines(label)), Text(" ")] + blocks[0].children
                blocks[0].source = ""
            for block in blocks:
                block.source = ""
            items.append(blocks)
        return ListBlock(ordered=env in ("enumerate", "compactenum", "inparaenum", "tasks"), items=items, source=source)

    def caption_and_label(self, body: str) -> tuple[list, str]:
        caption: list = []
        m = re.search(r"\\caption\*?\b", body)
        label = ""
        if m:
            _, i = read_optional(body, m.end())
            got = read_braced(body, i)
            if got:
                text = got[0]
                inner = re.search(r"\\label\s*\{([^{}]+)\}", text)
                if inner:
                    label = inner.group(1)
                    text = re.sub(r"\\label\s*\{[^{}]+\}", "", text)
                caption = trim(self.inlines(text))
        if not label:
            # \label after the last sub-environment belongs to the float itself.
            stripped = re.sub(r"\\begin\{subfigure\}.*?\\end\{subfigure\}|\\begin\{subtable\}.*?\\end\{subtable\}", "", body, flags=re.S)
            found = re.search(r"\\label\s*\{([^{}]+)\}", stripped)
            label = found.group(1) if found else ""
        return caption, label

    def find_image(self, name: str) -> Path | None:
        name = name.strip()
        for folder in self.graphics_dirs:
            base = self.root / folder / name
            if base.is_file():
                return base
            for ext in (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".tif", ".tiff", ".svg"):
                candidate = base.with_name(base.name + ext)
                if candidate.is_file():
                    return candidate
        return None

    def images(self, body: str) -> list[Image]:
        out: list[Image] = []
        for m in re.finditer(r"\\includegraphics\*?\b", self.expand(body)):
            text = self.expand(body)
            opts, i = read_optional(text, m.end())
            got = read_braced(text, i)
            if not got:
                continue
            path = self.find_image(got[0])
            if path is None:
                self.log.warn(f"could not find the figure file '{got[0].strip()}'")
            out.append(Image(path=str(path) if path else "", width_in=_width(opts or ""), original=got[0].strip()))
        return out

    def split_floats(self, env: str, body: str, content: str) -> list[str] | None:
        """One piece per caption, when an environment holds several floats."""
        masked = re.sub(r"\\begin\{sub(?:figure|table)\}.*?\\end\{sub(?:figure|table)\}", lambda m: "x" * len(m.group(0)), body, flags=re.S)
        marks = list(re.finditer(r"\\caption\*?\b", masked))
        if len(marks) < 2:
            return None
        ends = []
        for m in marks:
            _, i = read_optional(body, m.end())
            got = read_braced(body, i)
            if not got:
                return None
            label = re.compile(r"\s*(?:\\vspace\*?\s*\{[^{}]*\}\s*)*\\label\s*\{[^{}]*\}").match(body, got[1])
            ends.append((m.start(), label.end() if label else got[1]))
        caption_last = re.search(content, body[: ends[0][0]]) is not None
        cuts = [end for _, end in ends[:-1]] if caption_last else [start for start, _ in ends[1:]]
        pieces, last = [], 0
        for cut in cuts + [len(body)]:
            pieces.append(body[last:cut])
            last = cut
        return pieces

    def figure(self, body: str, source: str) -> list:
        pieces = self.split_floats("figure", body, r"\\includegraphics|\\begin\{tikzpicture\}")
        if pieces:
            return [b for piece in pieces for b in self.figure(piece, f"\\begin{{figure}}{piece}\\end{{figure}}")]
        images = self.images(body)
        expanded = self.expand(body)
        for env in _RENDERED:
            for m in re.finditer(rf"\\begin\{{{env}\}}", expanded):
                _, after = find_end(expanded, env, m.end())
                images.append(Image(path="", tikz=expanded[m.start() : after]))
        caption, label = self.caption_and_label(re.sub(r"\\begin\{subfigure\}.*?\\end\{subfigure\}", "", body, flags=re.S))
        subs = []
        for n, m in enumerate(re.finditer(r"\\begin\{subfigure\}(.*?)\\end\{subfigure\}|\\subfloat\s*(?:\[([^\]]*)\])", body, re.S)):
            letter = chr(ord("a") + n)
            inner = m.group(1) or ""
            sub_caption, sub_label = self.caption_and_label(inner) if inner else (self.inlines(m.group(2) or ""), "")
            if sub_caption:
                subs += [Text(f" ({letter}) ")] + sub_caption
            if sub_label:
                self.sub_labels[sub_label] = (label, letter)
        if subs:
            caption = merge_text(caption + subs)
        return [Figure(images=images, caption=caption, label=label, numbered=bool(caption) or bool(label), source=source)]

    def table(self, body: str, source: str) -> list:
        pieces = self.split_floats("table", body, r"\\begin\{(?:tabular|longtable)")
        if pieces:
            return [b for piece in pieces for b in self.table(piece, f"\\begin{{table}}{piece}\\end{{table}}")]
        caption, label = self.caption_and_label(body)
        expanded = self.expand(body)
        m = None
        env = ""
        for candidate in sorted(_TABULARS, key=len, reverse=True):
            m = re.search(rf"\\begin\{{{re.escape(candidate)}\}}", expanded)
            if m:
                env = candidate
                break
        if not m:
            self.log.warn("a table environment holds no tabular: kept as LaTeX source")
            return [Raw(tex=source, note="table", source=source)]
        end, _ = find_end(expanded, env, m.end())
        inner = expanded[m.end() : end]
        i = 0
        if env in ("tabular*", "tabularx", "tabulary", "xltabular"):
            got = read_braced(inner, i)
            i = got[1] if got else i
        _, i = read_optional(inner, i)
        spec = read_braced(inner, i)
        aligns: list[str] = []
        if spec:
            aligns = _column_aligns(spec[0])
            i = spec[1]
        table = self.parse_tabular(inner[i:], aligns)
        table.caption, table.label, table.source = caption, label, source
        table.numbered = bool(caption) or bool(label)
        out: list = [table]
        notes = re.search(r"\\begin\{tablenotes\}(.*?)\\end\{tablenotes\}", expanded, re.S)
        if notes:
            for piece in split_top(re.sub(r"^\s*(?:\[[^\]]*\])?", "", notes.group(1)), r"\\item\b"):
                label_text, j = read_optional(piece, 0)
                if piece[j:].strip():
                    prefix = [Style("sup", [Text(biblib.clean(label_text))]), Text(" ")] if label_text else []
                    out.append(Para(children=prefix + trim(self.inlines(piece[j:]))))
        table.group = len(out)  # the notes are part of the table's source
        return out

    def parse_tabular(self, body: str, aligns: list[str]) -> Table:
        rows: list[list[Cell]] = []
        header_rows = 0
        pending: dict[int, int] = {}
        for raw in split_top(body, r"\\\\\*?(?:\[[^\]]*\])?|\\tabularnewline\b"):
            rules = re.findall(r"\\(?:midrule|hline|endhead|endfirsthead)\b", raw)
            text = re.sub(r"\\(?:toprule|midrule|bottomrule|hline|endhead|endfirsthead|endfoot|endlastfoot|morecmidrules|addlinespace(?:\[[^\]]*\])?)\b", "", raw)
            text = re.sub(r"\\(?:cline|cmidrule)(?:\([^)]*\))?(?:\[[^\]]*\])?\{[^{}]*\}|\\specialrule\s*\{[^{}]*\}\s*\{[^{}]*\}\s*\{[^{}]*\}", "", text)
            text = re.sub(r"\\rowcolor(?:\[[^\]]*\])?\{[^{}]*\}", "", text)
            if rows and rules and not header_rows and "\\midrule" in raw or (rows and "\\hline" in raw and not header_rows):
                header_rows = len(rows)
            if not text.strip():
                continue
            row: list[Cell] = []
            col = 0
            for cell_src in split_top(text, r"&"):
                if pending.get(col, 0) > 0:  # under a \\multirow: LaTeX still has an empty cell here
                    pending[col] -= 1
                    col += 1
                    continue
                cell = self.cell(cell_src, aligns, col)
                if cell is None:
                    continue
                if cell.rowspan > 1:
                    for c in range(col, col + cell.colspan):
                        pending[c] = cell.rowspan - 1
                row.append(cell)
                col += cell.colspan
            rows.append(row)
        return Table(rows=rows, header_rows=header_rows or (1 if len(rows) > 1 else 0), aligns=aligns)

    def cell(self, src: str, aligns: list[str], col: int) -> Cell | None:
        src = src.strip()
        colspan = rowspan = 1
        align = aligns[col] if col < len(aligns) else ""
        m = re.match(r"\\multicolumn\s*\{(\d+)\}\s*", src)
        if m:
            spec = read_braced(src, m.end())
            body = read_braced(src, spec[1]) if spec else None
            if spec and body:
                colspan = int(m.group(1))
                align = (_column_aligns(spec[0]) or [align])[0]
                src = body[0].strip()
        m = re.match(r"\\multirow\s*(?:\[[^\]]*\])?\s*\{(-?\d+)\}\s*", src)
        if m:
            width = read_braced(src, m.end())
            body = read_braced(src, width[1]) if width else None
            if width and body:
                rowspan = max(1, int(m.group(1)))
                src = body[0].strip()
        return Cell(children=trim(self.inlines(src)), colspan=colspan, rowspan=rowspan, align=align)

    def resolve_sub_labels(self, doc: Document) -> None:
        if not self.sub_labels:
            return

        def fix(inlines: list) -> list:
            out = []
            for node in inlines:
                if isinstance(node, Ref) and node.label in self.sub_labels:
                    parent, letter = self.sub_labels[node.label]
                    out += [Ref(parent, node.kind), Text(letter)]
                    continue
                if isinstance(node, (Style, Link)):
                    node.children = fix(node.children)
                out.append(node)
            return out

        from .model import all_blocks

        for block in all_blocks(doc.blocks):
            if isinstance(block, (Para, Heading)):
                block.children = fix(block.children)

    # -- inlines ---------------------------------------------------------------

    def inlines(self, src: str) -> list:
        return merge_text(self._inlines(self.expand(src)))

    def _inlines(self, src: str) -> list:
        out: list = []
        buf: list[str] = []
        i, n = 0, len(src)

        def flush() -> None:
            if buf:
                out.append(Text("".join(buf)))
                buf.clear()

        while i < n:
            ch = src[i]
            if ch == "\\":
                m = _CMD.match(src, i)
                if not m:
                    i += 1
                    continue
                i = self.inline_command(m, src, buf, out, flush)
                continue
            if ch == "$":
                end = i + 1
                braces = 0
                while end < n and not (src[end] == "$" and src[end - 1] != "\\" and braces == 0):
                    if src[end - 1] != "\\":  # \text{$x$} inside math has dollars of its own
                        braces += (src[end] == "{") - (src[end] == "}")
                    end += 1
                flush()
                out.append(self.math(src[i + 1 : end]))
                i = end + 1
                continue
            if ch == "{":
                got = read_braced(src, i)
                if got is None:
                    i += 1
                    continue
                flush()
                out += self.group(got[0])
                i = got[1]
                continue
            if ch == "}":
                i += 1
                continue
            if ch == "~":
                buf.append("\u00a0")
            elif src.startswith("---", i):
                buf.append("—")
                i += 3
                continue
            elif src.startswith("--", i):
                buf.append("–")
                i += 2
                continue
            elif src.startswith("``", i):
                buf.append("“")
                i += 2
                continue
            elif src.startswith("''", i):
                buf.append("”")
                i += 2
                continue
            elif ch == "`":
                buf.append("‘")
            elif ch == "'":
                buf.append("’")
            elif ch in "\n\t":
                buf.append(" ")
            else:
                buf.append(ch)
            i += 1
        flush()
        return out

    def group(self, inner: str) -> list:
        m = re.match(r"\s*\\([a-zA-Z]+)\b\s*", inner)
        if m and m.group(1) in _GROUP_STYLES:
            return [Style(_GROUP_STYLES[m.group(1)], self._inlines(inner[m.end() :]))]
        return self._inlines(inner)

    def math(self, latex: str) -> Math:
        latex = latex.strip()
        omml, unsupported = latex_to_omml(latex)
        self.note_unsupported(unsupported)
        return Math(latex=latex, sig=signature_of_xml(omml))

    def inline_command(self, m: re.Match, src: str, buf: list[str], out: list, flush) -> int:
        name = m.group(1)
        end = m.end()
        if len(name) == 1 and not name.isalpha():
            return self.symbol_command(name, m, src, buf, out, flush)
        # A control word swallows the spaces after it, unless {} ends it.
        after_space = end
        while after_space < len(src) and src[after_space] in " \t\n":
            after_space += 1
        if name in _LITERALS:
            buf.append(_LITERALS[name])
            if src.startswith("{}", end):
                return end + 2
            return after_space
        if name in _STYLES:
            got = read_braced(src, end)
            if got:
                flush()
                out.append(Style(_STYLES[name], self._inlines(got[0])))
                return got[1]
            return end
        if name in _TRANSPARENT_ARG:
            _, j = read_optional(src, end)
            got = read_braced(src, j)
            if got and name == "foreignlanguage":
                got = read_braced(src, got[1])
            if got:
                flush()
                out += self._inlines(got[0])
                return got[1]
            return end
        if _CITE.match(name):
            return self.cite(name, m, src, out, flush)
        if name in _REFS:
            return self.ref(name, m, src, buf, out, flush)
        if name == "label":
            got = read_braced(src, end)
            return got[1] if got else end
        if name == "footnote":
            _, j = read_optional(src, end)
            got = read_braced(src, j)
            if got:
                flush()
                blocks = self.blocks(got[0])
                for block in blocks:
                    block.source = ""
                out.append(Note(blocks=blocks))
                return got[1]
            return end
        if name in ("footnotemark", "footnotetext"):
            self.log.warn(f"\\{name} is split from its text and is not carried over: use \\footnote")
            _, j = read_optional(src, end)
            got = read_braced(src, j) if name == "footnotetext" else None
            return got[1] if got else j
        if name == "url":
            got = read_braced(src, end)
            if got:
                flush()
                url = _unescape_url(got[0])
                out.append(Link(url=url, children=[Text(url)]))
                return got[1]
            return end
        if name == "href":
            got = read_braced(src, end)
            text = read_braced(src, got[1]) if got else None
            if got and text:
                flush()
                out.append(Link(url=_unescape_url(got[0]), children=self._inlines(text[0])))
                return text[1]
            return end
        if name in ("verb", "verb*", "lstinline"):
            _, j = read_optional(src, end)
            if j >= len(src):
                return j
            if src[j] == "{":
                got = read_braced(src, j)
                flush()
                out.append(Style("code", [Text(got[0] if got else "")]))
                return got[1] if got else j + 1
            close = src.find(src[j], j + 1)
            close = len(src) if close < 0 else close
            flush()
            out.append(Style("code", [Text(src[j + 1 : close])]))
            return close + 1
        if name == "ensuremath":
            got = read_braced(src, end)
            if got:
                symbol = got[0].strip()
                if symbol.startswith("\\") and symbol[1:] in SYMBOLS:  # one symbol is a character, not an equation
                    buf.append(SYMBOLS[symbol[1:]])
                    return got[1]
                if symbol == "-":
                    buf.append("−")
                    return got[1]
                flush()
                out.append(self.math(got[0]))
                return got[1]
            return end
        if name == "textcolor":
            color = read_braced(src, end)
            body = read_braced(src, color[1]) if color else None
            if body:
                flush()
                self.log.count("removed text color")
                out += self._inlines(body[0])
                return body[1]
            return end
        if name in ("hl", "sout", "st", "colorbox", "highlight"):
            if name == "colorbox":
                color = read_braced(src, end)
                end = color[1] if color else end
            got = read_braced(src, end)
            if got:
                flush()
                self.log.count(f"removed \\{name} markup, kept its text")
                out += self._inlines(got[0])
                return got[1]
            return end
        if name in ("todo", "fixme", "note", "comment"):
            _, j = read_optional(src, end)
            got = read_braced(src, j)
            if got:
                self.log.warn(f"a \\{name} note was left out of the output: {biblib.clean(got[0])[:80]}")
                return got[1]
            return end
        if name in ("SI", "qty", "SIrange"):
            value = read_braced(src, end)
            unit = read_braced(src, value[1]) if value else None
            if value and unit:
                buf.append(f"{value[0]}\u00a0{_unit(unit[0])}")
                return unit[1]
            return end
        if name in ("si", "unit"):
            got = read_braced(src, end)
            if got:
                buf.append(_unit(got[0]))
                return got[1]
            return end
        if name == "num":
            got = read_braced(src, end)
            if got:
                buf.append(got[0])
                return got[1]
            return end
        if name in ("parbox", "rotatebox", "scalebox", "raisebox", "resizebox", "resizebox*"):
            j = end
            for _ in range(2 if name == "resizebox" or name == "resizebox*" else 1):
                _, j = read_optional(src, j)
                got = read_braced(src, j)
                j = got[1] if got else j
            _, j = read_optional(src, j)
            body = read_braced(src, j)
            if body:
                flush()
                out += self._inlines(body[0])
                return body[1]
            return end
        if name in ("newcommand", "renewcommand", "providecommand"):
            word = re.compile(r"\*?\s*\\[a-zA-Z@]+").match(src, end)
            j = word.end() if word else end
            if not word:
                got = read_braced(src, j)
                j = got[1] if got else j
            _, j = read_optional(src, j)
            _, j = read_optional(src, j)
            got = read_braced(src, j)
            return got[1] if got else j
        if name == "multirow":
            _, j = read_optional(src, end)
            rows = read_braced(src, j)
            width = read_braced(src, rows[1]) if rows else None
            body = read_braced(src, width[1]) if width else None
            if body:
                flush()
                out += self._inlines(body[0])
                return body[1]
            return end
        if name == "newline":
            flush()
            out.append(Break())
            return after_space
        if name == "today":
            return after_space
        if name == "begin":
            got = read_braced(src, end)
            if got and got[0] == "math":
                close, after = find_end(src, "math", got[1])
                flush()
                out.append(self.math(src[got[1] : close]))
                return after
            return got[1] if got else end
        if name == "end":
            got = read_braced(src, end)
            return got[1] if got else end
        if name in _LAYOUT_NOARG:
            self.log.count("removed layout commands, which do not carry over") if name not in ("protect", "noindent", "par", "maketitle") else None
            return after_space
        base_args = _LAYOUT_ARGS.get(name)
        if base_args is not None:
            j = end
            for _ in range(base_args):
                _, j = read_optional(src, j)
                got = read_braced(src, j)
                j = got[1] if got else j
            return j
        if name in ("thanks", "item"):
            got = read_braced(src, end)
            return got[1] if got else end
        if name in ("includegraphics", "includegraphics*"):
            _, j = read_optional(src, end)
            got = read_braced(src, j)
            self.log.warn("a figure outside a figure environment was left out; put it in a figure environment")
            return got[1] if got else j
        if len(name) == 1 and name in "uvHckr" and (src[end : end + 1] == "{" or src[end : end + 1].isspace()):
            got = read_braced(src, end)
            if got:
                buf.append(biblib.decode_accents(f"\\{name}{{{got[0]}}}"))
                return got[1]
        self.unknown[name] = self.unknown.get(name, 0) + 1
        return after_space

    def symbol_command(self, name: str, m: re.Match, src: str, buf: list[str], out: list, flush) -> int:
        end = m.end()
        if name in "%$&#_{}":
            buf.append(name)
            return end
        if name == "\\":
            _, j = read_optional(src, end)
            if src[end : end + 1] == "*":
                j = end + 1
            flush()
            out.append(Break())
            return j
        if name == "(":
            close = src.find("\\)", end)
            close = len(src) if close < 0 else close
            flush()
            out.append(self.math(src[end:close]))
            return close + 2
        if name in " \n\t":
            buf.append(" ")
            return end
        if name == ",":
            buf.append("\u2009")
            return end
        if name in ";:":
            buf.append(" ")
            return end
        if name in "/-@!":
            return end
        if name in "'`^\"~=.":
            piece = re.compile(r"\s*\{?\\?[a-zA-Z]\}?").match(src, end)
            if piece:
                buf.append(biblib.decode_accents(f"\\{name}{piece.group(0).strip()}"))
                return piece.end()
            return end
        return end

    def cite(self, name: str, m: re.Match, src: str, out: list, flush) -> int:
        first, j = read_optional(src, m.end())
        second, j = read_optional(src, j)
        got = read_braced(src, j)
        if not got:
            return m.end()
        keys = [k.strip() for k in got[0].split(",") if k.strip()]
        pre, post = ("", first or "") if second is None else (first or "", second)
        lowered = name.lower().rstrip("*")
        flush()
        mode = "text" if lowered in ("citet", "textcite", "citealt", "citeauthor", "newcite") else "paren"
        out.append(Cite(keys=keys, mode=mode, cmd=name, pre=pre, post=post))
        return got[1]

    def ref(self, name: str, m: re.Match, src: str, buf: list[str], out: list, flush) -> int:
        got = read_braced(src, m.end())
        if not got:
            return m.end()
        labels = [x.strip() for x in got[0].split(",") if x.strip()]
        kind = _REFS[name]
        flush()
        for n, label in enumerate(labels):
            if n:
                out.append(Text(", "))
            if kind == "auto":
                prefix = label.split(":", 1)[0].lower() if ":" in label else ""
                word = _REF_NAMES.get(prefix, "")
                if word and n == 0:
                    out.append(Text(word + "\u00a0"))
                out.append(Ref(label, "ref"))
            else:
                out.append(Ref(label, kind))
        return got[1]


_UNITS = {"milli": "m", "micro": "µ", "nano": "n", "pico": "p", "kilo": "k", "mega": "M", "giga": "G", "centi": "c", "deci": "d", "metre": "m", "meter": "m", "gram": "g", "second": "s", "litre": "L", "liter": "L", "mole": "mol", "molar": "M", "hertz": "Hz", "newton": "N", "pascal": "Pa", "joule": "J", "watt": "W", "volt": "V", "ampere": "A", "ohm": "Ω", "kelvin": "K", "celsius": "°C", "degreeCelsius": "°C", "percent": "%", "hour": "h", "minute": "min", "day": "d", "dalton": "Da", "base": "bp"}


def _unit(spec: str) -> str:
    """The display form of a siunitx unit: ``\\milli\\metre`` as ``mm``."""
    if "\\" not in spec:
        return spec.replace(".", "·")
    text = re.sub(r"\\per\s*\\([a-zA-Z]+(?:\\[a-zA-Z]+)*)", lambda m: "/" + _unit("\\" + m.group(1)), spec)
    text = re.sub(r"\\squared", "²", text)
    text = re.sub(r"\\cubed", "³", text)
    return re.sub(r"\\([a-zA-Z]+)", lambda m: _UNITS.get(m.group(1), m.group(1)), text).replace(" ", "")


_DEFINITIONS = {"newcommand", "renewcommand", "providecommand", "DeclareRobustCommand", "def", "gdef", "newtheorem", "newenvironment", "renewenvironment", "DeclareMathOperator", "newcolumntype"}


def _definition_end(name: str, src: str, i: int) -> int | None:
    """Where the definition starting just before ``i`` ends, or None if it does not parse."""
    if name in ("def", "gdef"):
        m = re.compile(r"\s*\\[a-zA-Z@]+(?:#\d)*\s*").match(src, i)
        got = read_braced(src, m.end()) if m else None
        return got[1] if got else None
    got = read_braced(src, i)
    if got:
        j = got[1]
    else:
        m = re.compile(r"\s*\\[a-zA-Z@]+").match(src, i)
        if not m:
            return None
        j = m.end()
    if name == "DeclareMathOperator":
        got = read_braced(src, j)
        return got[1] if got else None
    if name == "newtheorem":
        _, j = read_optional(src, j)
        got = read_braced(src, j)
        if not got:
            return None
        _, j = read_optional(src, got[1])
        return j
    _, j = read_optional(src, j)
    _, j = read_optional(src, j)
    got = read_braced(src, j)
    if not got:
        return None
    if name in ("newenvironment", "renewenvironment"):
        got = read_braced(src, got[1])
        if not got:
            return None
    return got[1]


def _unescape_url(url: str) -> str:
    return re.sub(r"\\([%#&_~$])", r"\1", url).strip()


def _column_aligns(spec: str) -> list[str]:
    spec = re.sub(r"\*\{(\d+)\}\{([^{}]*)\}", lambda m: m.group(2) * int(m.group(1)), spec)
    spec = re.sub(r"[@!>< ]\{(?:[^{}]|\{[^{}]*\})*\}", "", spec)
    out: list[str] = []
    i = 0
    while i < len(spec):
        ch = spec[i]
        if ch in "lcr":
            out.append({"l": "left", "c": "center", "r": "right"}[ch])
        elif ch in "pmbXLCRSJ":
            if ch in "pmb" and spec[i + 1 : i + 2] == "{":
                got = read_braced(spec, i + 1)
                if got:
                    i = got[1] - 1
            out.append({"C": "center", "R": "right", "S": "right"}.get(ch, "left"))
        i += 1
    return out


def _width(opts: str) -> float | None:
    """A width option in inches, against a 6.5 inch text block."""
    m = re.search(r"\bwidth\s*=\s*([\d.]*)\s*\\(?:textwidth|linewidth|columnwidth|hsize)", opts)
    if m:
        return 6.5 * float(m.group(1) or 1)
    m = re.search(r"\bwidth\s*=\s*([\d.]+)\s*(cm|mm|in|pt)", opts)
    if m:
        return float(m.group(1)) * {"cm": 1 / 2.54, "mm": 1 / 25.4, "in": 1.0, "pt": 1 / 72.27}[m.group(2)]
    return None


def trim(children: list) -> list:
    """``children`` without leading and trailing white space."""
    children = list(children)
    while children and isinstance(children[0], Text):
        stripped = children[0].text.lstrip(" \n\t")
        if stripped:
            children[0] = Text(stripped)
            break
        children.pop(0)
    while children and isinstance(children[-1], Text):
        stripped = children[-1].text.rstrip(" \n\t")
        if stripped:
            children[-1] = Text(stripped)
            break
        children.pop()
    return children
