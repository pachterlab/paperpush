"""Bibliographies: read BibTeX and CSL records, format references, write BibTeX."""

from __future__ import annotations

import re
import unicodedata

from .model import BibEntry

_ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303", "=": "\u0304", ".": "\u0307", "u": "\u0306", "v": "\u030c", "H": "\u030b", "c": "\u0327", "k": "\u0328", "r": "\u030a"}
_LETTERS = {"ss": "ß", "o": "ø", "O": "Ø", "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ", "aa": "å", "AA": "Å", "l": "ł", "L": "Ł", "i": "ı", "j": "ȷ"}
_ACCENT_RE = re.compile(r"""\\(['`^"~=.])\s*\{?\\?([a-zA-Z])\}?|\\([uvHckr])(?:\s+|\s*\{)\\?([a-zA-Z])\}?""")
_LETTER_RE = re.compile(r"\\(ss|ae|AE|oe|OE|aa|AA|o|O|l|L|i|j)\b(?:\{\})?\s?")


def decode_accents(text: str) -> str:
    """Turn LaTeX accent commands (``\\"o``, ``\\c{c}``, ``\\ss``) into Unicode."""

    def accent(m: re.Match) -> str:
        mark, letter = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        return unicodedata.normalize("NFC", letter + _ACCENTS[mark])

    text = _ACCENT_RE.sub(accent, text)
    return _LETTER_RE.sub(lambda m: _LETTERS[m.group(1)], text)


def clean(value: str) -> str:
    """A BibTeX field as plain text."""
    value = decode_accents(value)
    value = re.sub(r"\\(?:emph|textit|textbf|textsc|texttt|mathrm|url|href|natexlab|doi)\{([^{}]*)\}", r"\1", value)
    value = re.sub(r"\\(?:em|it|bf|sc|rm|tt|sl|sf|newblock|penalty0|relax|protect)\b\s*", "", value)
    value = re.sub(r"\\([%&$#_{}])", lambda m: "\x00" + m.group(1), value)
    value = value.replace("{", "").replace("}", "").replace("\x00", "")
    value = value.replace("---", "—").replace("--", "–").replace("~", " ")
    return re.sub(r"\s+", " ", value).strip()


def _balanced(text: str, start: int, opener: str, closer: str) -> int:
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _fields(body: str, strings: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    i = 0
    while i < len(body):
        m = re.compile(r"\s*,?\s*([\w:.-]+)\s*=\s*").match(body, i)
        if not m:
            break
        name = m.group(1).lower()
        i = m.end()
        parts: list[str] = []
        while i < len(body):
            ch = body[i]
            if ch == "{":
                end = _balanced(body, i, "{", "}")
                if end < 0:
                    return out
                parts.append(body[i + 1 : end])
                i = end + 1
            elif ch == '"':
                end = i + 1
                depth = 0
                while end < len(body) and not (body[end] == '"' and depth == 0):
                    depth += body[end] == "{"
                    depth -= body[end] == "}"
                    end += 1
                parts.append(body[i + 1 : end])
                i = end + 1
            else:
                word = re.compile(r"[\w:.-]+").match(body, i)
                if not word:
                    break
                parts.append(strings.get(word.group(0).lower(), word.group(0)))
                i = word.end()
            spaced = re.compile(r"\s*#\s*").match(body, i)
            if not spaced:
                break
            i = spaced.end()
        out[name] = "".join(parts).strip()
    return out


def parse_bib(text: str) -> dict[str, BibEntry]:
    """Every entry in a ``.bib`` file, by key."""
    out: dict[str, BibEntry] = {}
    strings: dict[str, str] = {"jan": "January", "feb": "February", "mar": "March", "apr": "April", "may": "May", "jun": "June", "jul": "July", "aug": "August", "sep": "September", "oct": "October", "nov": "November", "dec": "December"}
    for m in re.finditer(r"@(\w+)\s*([{(])", text):
        kind = m.group(1).lower()
        opener = m.group(2)
        end = _balanced(text, m.end() - 1, opener, "}" if opener == "{" else ")")
        if end < 0:
            continue
        body = text[m.end() : end]
        if kind == "comment" or kind == "preamble":
            continue
        if kind == "string":
            for name, value in _fields(body, strings).items():
                strings[name] = value
            continue
        key, _, rest = body.partition(",")
        out[key.strip()] = BibEntry(key=key.strip(), kind=kind, fields=_fields(rest, strings))
    return out


def split_authors(value: str) -> list[tuple[str, str]]:
    """``(family, given)`` for each name in a BibTeX author field."""
    names = re.split(r"\s+and\s+", clean(value))
    out: list[tuple[str, str]] = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        if name.lower() == "others":
            out.append(("et al.", ""))
        elif "," in name:
            family, given = name.split(",", 1)
            out.append((family.strip(), given.strip()))
        else:
            parts = name.split()
            out.append((parts[-1], " ".join(parts[:-1])))
    return out


def _initials(given: str) -> str:
    return "".join(p[0].upper() for p in re.split(r"[\s.-]+", given) if p)


def format_reference(entry: BibEntry) -> str:
    """One formatted reference, in a plain Vancouver-like style."""
    if not entry.fields:
        return entry.text or entry.key
    f = {k: clean(v) for k, v in entry.fields.items()}
    authors = split_authors(entry.fields.get("author") or entry.fields.get("editor") or "")
    names = [f"{family} {_initials(given)}".strip() for family, given in authors[:6]]
    if len(authors) > 6:
        names.append("et al.")
    parts = [", ".join(names) + "." if names else ""]
    title = f.get("title", "").rstrip(".")
    if title:
        parts.append(title + ".")
    venue = f.get("journal") or f.get("journaltitle") or f.get("booktitle") or f.get("publisher") or f.get("howpublished") or ""
    year = f.get("year") or f.get("date", "")[:4]
    tail = ""
    if year:
        tail += year
    if f.get("volume"):
        tail += (";" if tail else "") + f["volume"]
    if f.get("number"):
        tail += f"({f['number']})"
    if f.get("pages"):
        tail += (":" if tail else "") + f["pages"].replace("–", "-")
    if venue:
        parts.append(venue + ("." if tail else ""))
    if tail:
        parts.append(tail + ".")
    if f.get("doi"):
        parts.append("doi:" + f["doi"])
    elif f.get("url"):
        parts.append(f["url"])
    return " ".join(p for p in parts if p).strip()


def author_year(entry: BibEntry) -> tuple[str, str]:
    """``(names, year)`` for an author-year citation."""
    if not entry.fields:
        m = re.search(r"\b(1[89]\d\d|20\d\d)[a-z]?\b", entry.text)
        family = re.match(r"\s*([^\s,.(]+)", entry.text)
        return (family.group(1) if family else entry.key), (m.group(0) if m else "n.d.")
    authors = split_authors(entry.fields.get("author") or entry.fields.get("editor") or "")
    year = clean(entry.fields.get("year") or entry.fields.get("date", ""))[:4] or "n.d."
    if not authors:
        return entry.key, year
    if len(authors) == 1:
        return authors[0][0], year
    if len(authors) == 2 and authors[1][0] != "et al.":
        return f"{authors[0][0]} and {authors[1][0]}", year
    return f"{authors[0][0]} et al.", year


_ESCAPE_BIB = re.compile(r"(?<!\\)([%&#_])")


def to_bibtex(entries: list[BibEntry]) -> str:
    """BibTeX text for the structured entries among ``entries``."""
    out: list[str] = []
    for entry in entries:
        if not entry.fields:
            continue
        lines = [f"@{entry.kind}{{{entry.key},"]
        for name, value in entry.fields.items():
            if name not in ("url", "doi"):
                value = _ESCAPE_BIB.sub(r"\\\1", value)
            lines.append(f"  {name} = {{{value}}},")
        lines.append("}")
        out.append("\n".join(lines))
    return "\n\n".join(out) + ("\n" if out else "")


_CSL_TYPES = {"article-journal": "article", "paper-conference": "inproceedings", "chapter": "incollection", "book": "book", "thesis": "phdthesis", "report": "techreport", "webpage": "misc", "article": "misc", "post-weblog": "misc"}


def key_for(family: str, year: str, taken) -> str:
    """A citation key like ``smith2020``, with a letter added if it is taken."""
    base = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", family).encode("ascii", "ignore").decode().lower()) or "ref"
    base += re.sub(r"\D", "", year)[:4]
    key, n = base, 0
    while key in taken:
        n += 1
        key = base + chr(ord("a") + (n - 1) % 26) + ("" if n <= 26 else str(n // 26))
    return key


def from_csl(item: dict, taken) -> BibEntry:
    """A BibTeX-shaped entry from a CSL-JSON record (what Zotero and Mendeley embed in Word)."""
    authors = item.get("author") or item.get("editor") or []
    names = []
    for a in authors:
        if a.get("family"):
            names.append(f"{a['family']}, {a.get('given', '')}".strip().rstrip(","))
        elif a.get("literal"):
            names.append("{" + a["literal"] + "}")
    issued = item.get("issued") or {}
    year = ""
    if issued.get("date-parts") and issued["date-parts"][0]:
        year = str(issued["date-parts"][0][0])
    elif issued.get("raw"):
        year = str(issued["raw"])[:4]
    fields = {}
    if names:
        fields["author"] = " and ".join(names)
    if item.get("title"):
        fields["title"] = "{" + str(item["title"]) + "}"
    if year:
        fields["year"] = year
    container = item.get("container-title")
    kind = _CSL_TYPES.get(item.get("type", ""), "misc")
    if container:
        fields["booktitle" if kind in ("inproceedings", "incollection") else "journal"] = str(container)
    for csl, bib in (("volume", "volume"), ("issue", "number"), ("page", "pages"), ("DOI", "doi"), ("URL", "url"), ("publisher", "publisher")):
        if item.get(csl):
            fields[bib] = str(item[csl])
    family = (authors[0].get("family") or authors[0].get("literal") or "ref") if authors else "ref"
    return BibEntry(key=key_for(family, year, taken), kind=kind, fields=fields)
