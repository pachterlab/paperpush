"""LaTeX math to Office Math (OMML) and back.

:func:`latex_to_omml` parses LaTeX math and writes the children of an
``<m:oMath>``; :func:`omml_to_latex` reads an ``<m:oMath>`` element back.
:func:`signature` reduces an equation to its structure, ignoring the run
splitting and properties Word rewrites when it saves, so an unedited equation
can be recognized after a trip through Word.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET  # nosec B405 -- parses XML this module generated, or a package read_docx screened
from xml.sax.saxutils import escape, quoteattr

M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_DECL = f'xmlns:m="{M_NS}" xmlns:w="{W_NS}"'

SYMBOLS = {
    # Greek
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ", "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "ϕ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    # relations
    "leq": "≤", "geq": "≥", "neq": "≠", "approx": "≈", "equiv": "≡", "sim": "∼", "simeq": "≃", "cong": "≅", "propto": "∝", "ll": "≪", "gg": "≫", "prec": "≺", "succ": "≻", "preceq": "⪯", "succeq": "⪰", "subset": "⊂", "supset": "⊃", "subseteq": "⊆", "supseteq": "⊇", "in": "∈", "ni": "∋", "notin": "∉", "perp": "⊥", "parallel": "∥", "mid": "∣", "triangleq": "≜", "geqslant": "⩾", "leqslant": "⩽", "lesssim": "≲", "gtrsim": "≳", "nmid": "∤", "nsubseteq": "⊈", "varpropto": "∝", "lhd": "⊲", "rhd": "⊳", "unlhd": "⊴", "unrhd": "⊵", "ltimes": "⋉", "rtimes": "⋊", "rightsquigarrow": "⇝", "complement": "∁", "blacksquare": "∎", "checkmark": "✓", "bigstar": "★", "models": "⊨", "vdash": "⊢", "dashv": "⊣", "asymp": "≍", "doteq": "≐",
    # arrows
    "to": "→", "rightarrow": "→", "leftarrow": "←", "leftrightarrow": "↔", "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔", "mapsto": "↦", "uparrow": "↑", "downarrow": "↓", "longrightarrow": "⟶", "longleftarrow": "⟵", "Longrightarrow": "⟹", "Longleftrightarrow": "⟺", "hookrightarrow": "↪", "rightharpoonup": "⇀", "nearrow": "↗", "searrow": "↘",
    # binary operators
    "pm": "±", "mp": "∓", "times": "×", "div": "÷", "cdot": "⋅", "ast": "∗", "star": "⋆", "circ": "∘", "bullet": "∙", "oplus": "⊕", "ominus": "⊖", "otimes": "⊗", "odot": "⊙", "cup": "∪", "cap": "∩", "setminus": "∖", "wedge": "∧", "vee": "∨", "land": "∧", "lor": "∨", "dagger": "†", "ddagger": "‡", "wr": "≀", "sqcup": "⊔", "sqcap": "⊓",
    # miscellaneous
    "infty": "∞", "partial": "∂", "nabla": "∇", "forall": "∀", "exists": "∃", "nexists": "∄", "emptyset": "∅", "varnothing": "∅", "neg": "¬", "lnot": "¬", "angle": "∠", "ell": "ℓ", "hbar": "ℏ", "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ", "wp": "℘", "prime": "′", "degree": "°", "triangle": "△", "square": "□", "therefore": "∴", "because": "∵", "top": "⊤", "bot": "⊥",
    "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱", "dotsc": "…", "dotsb": "⋯",
    "langle": "⟨", "rangle": "⟩", "lceil": "⌈", "rceil": "⌉", "lfloor": "⌊", "rfloor": "⌋", "lbrace": "{", "rbrace": "}", "vert": "|", "Vert": "‖", "lvert": "|", "rvert": "|", "lVert": "‖", "rVert": "‖", "backslash": "\\",
    "%": "%", "$": "$", "#": "#", "&": "&", "_": "_", "{": "{", "}": "}",
}  # fmt: skip
_REVERSE_PREFER = {"→": "to", "…": "ldots", "⊥": "perp", "∧": "wedge", "∨": "vee", "¬": "neg", "∅": "emptyset", "|": "", "{": "{", "}": "}", "%": "%", "$": "$", "#": "#", "_": "_", "&": "&", "\\": "backslash"}
UNICODE_TO_CMD = {}
for _cmd, _ch in SYMBOLS.items():
    UNICODE_TO_CMD.setdefault(_ch, _cmd)
UNICODE_TO_CMD.update({k: v for k, v in _REVERSE_PREFER.items() if v})
UNICODE_TO_CMD.pop("|", None)
UNICODE_TO_CMD.update({"∣": "mid", "−": "", "·": "cdot", "≦": "leq", "≧": "geq", "ε": "varepsilon", "φ": "varphi", "∕": "", "ℎ": ""})

NARY = {"sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬", "iiint": "∭", "oint": "∮", "bigcup": "⋃", "bigcap": "⋂", "bigoplus": "⨁", "bigotimes": "⨂", "bigvee": "⋁", "bigwedge": "⋀", "bigsqcup": "⨆", "bigodot": "⨀"}
NARY_REV = {v: k for k, v in NARY.items()}
INTEGRALS = {"int", "iint", "iiint", "oint"}
FUNCS = {"sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh", "coth", "log", "ln", "lg", "exp", "det", "dim", "ker", "deg", "gcd", "hom", "arg", "Pr"}
LIMIT_FUNCS = {"lim", "limsup", "liminf", "max", "min", "sup", "inf", "argmax", "argmin"}
_FUNC_TEXT = {"limsup": "lim sup", "liminf": "lim inf", "argmax": "arg max", "argmin": "arg min"}
ACCENTS = {"hat": "\u0302", "widehat": "\u0302", "tilde": "\u0303", "widetilde": "\u0303", "bar": "\u0305", "vec": "\u20d7", "dot": "\u0307", "ddot": "\u0308", "check": "\u030c", "breve": "\u0306", "acute": "\u0301", "grave": "\u0300", "mathring": "\u030a"}
ACCENTS_REV = {"\u0302": "hat", "\u0303": "tilde", "\u0305": "bar", "\u0304": "bar", "\u20d7": "vec", "\u0307": "dot", "\u0308": "ddot", "\u030c": "check", "\u0306": "breve", "\u0301": "acute", "\u0300": "grave", "\u030a": "mathring"}
FONTS = {
    "mathbf": '<m:sty m:val="b"/>', "boldsymbol": '<m:sty m:val="bi"/>', "bm": '<m:sty m:val="bi"/>', "mathit": '<m:sty m:val="i"/>', "mathrm": '<m:sty m:val="p"/>', "mathnormal": "", "mathsf": '<m:scr m:val="sans-serif"/><m:sty m:val="p"/>', "mathtt": '<m:scr m:val="monospace"/><m:sty m:val="p"/>', "mathcal": '<m:scr m:val="script"/><m:sty m:val="p"/>', "mathscr": '<m:scr m:val="script"/><m:sty m:val="p"/>', "mathfrak": '<m:scr m:val="fraktur"/><m:sty m:val="p"/>', "mathbb": '<m:scr m:val="double-struck"/><m:sty m:val="p"/>',
}  # fmt: skip
SPACES = {"\\,": "\u2009", "\\:": "\u2005", "\\>": "\u2005", "\\;": "\u2004", "\\ ": "\u00a0", "\\quad": "\u2003", "\\qquad": "\u2003\u2003", "~": "\u00a0"}
SPACES_REV = {"\u2009": "\\,", "\u2005": "\\:", "\u2004": "\\;", "\u00a0": "\\ ", "\u2003": "\\quad ", "\u2002": "\\;", "\u2006": "\\,", "\u2007": "\\;", "\u2008": "\\,", "\u200a": "\\,", "\u205f": "\\:", "\u3000": "\\quad "}
_IGNORED = {"small", "footnotesize", "normalsize", "large", "Large", "tiny", "scriptsize", "normalfont", "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle", "limits", "nolimits", "nonumber", "notag", "!", "big", "Big", "bigg", "Bigg", "bigl", "Bigl", "biggl", "Biggl", "bigr", "Bigr", "biggr", "Biggr", "bigm", "Bigm", "biggm", "Biggm", "strut", "mathstrut", "allowbreak", "protect", "mathopen", "mathclose", "mathrel", "mathbin", "mathord", "mathpunct", "mathinner", "mathop"}
_MATRICES = {"matrix": ("", ""), "pmatrix": ("(", ")"), "bmatrix": ("[", "]"), "Bmatrix": ("{", "}"), "vmatrix": ("|", "|"), "Vmatrix": ("‖", "‖"), "smallmatrix": ("", ""), "array": ("", ""), "tabular": ("", "")}
_ROWS_ENVS = {"aligned", "align", "align*", "alignat", "alignat*", "split", "gathered", "gather", "gather*", "multline", "multline*", "eqnarray", "eqnarray*", "flalign", "flalign*"}
_RELATIONS = {"=", "<", ">", "+", "-", ",", ";", "\\leq", "\\le", "\\geq", "\\ge", "\\neq", "\\ne", "\\approx", "\\equiv", "\\sim", "\\to", "\\rightarrow", "\\Rightarrow", "\\in", "\\pm", "\\mp", "\\propto", "\\quad", "\\qquad"}
_ALIASES = {"dag": "dagger", "ddag": "ddagger", "le": "leq", "ge": "geq", "ne": "neq", "gets": "leftarrow", "iff": "Longleftrightarrow", "implies": "Longrightarrow", "impliedby": "Longleftarrow", "colon": ":", "lt": "<", "gt": ">"}
_ZWSP = "\u200b"
_TOKEN = re.compile(r"\\[a-zA-Z]+\*?|\\.|\s+|.", re.S)
_RFONTS = '<w:rPr><w:rFonts w:ascii="Cambria Math" w:hAnsi="Cambria Math"/></w:rPr>'


class _R:
    """A run of math text; adjacent runs with the same properties are written as one."""

    __slots__ = ("text", "rpr")

    def __init__(self, text: str, rpr: str = "") -> None:
        self.text, self.rpr = text, rpr


class _X:
    """Finished OMML, and whether scripts on it mean limits above and below."""

    __slots__ = ("xml", "limit")

    def __init__(self, xml: str, limit: bool = False) -> None:
        self.xml, self.limit = xml, limit


class _G(list):
    """A brace group: several atoms acting as one base."""


def _run(text: str, rpr: str = "") -> str:
    props = f"<m:rPr>{rpr}</m:rPr>" if rpr else ""
    return f'<m:r>{props}{_RFONTS}<m:t xml:space="preserve">{escape(text)}</m:t></m:r>'


def _emit(atoms) -> str:
    out: list[str] = []
    pending: _R | None = None
    flat: list = []

    def flatten(items) -> None:
        for a in items:
            if isinstance(a, _G):
                flatten(a)
            else:
                flat.append(a)

    flatten(atoms)
    for atom in flat:
        if isinstance(atom, _R):
            if pending is not None and pending.rpr == atom.rpr and "<m:aln/>" not in atom.rpr:
                pending = _R(pending.text + atom.text, pending.rpr)
            else:
                if pending is not None:
                    out.append(_run(pending.text, pending.rpr))
                pending = atom
        else:
            if pending is not None:
                out.append(_run(pending.text, pending.rpr))
                pending = None
            out.append(atom.xml)
    if pending is not None:
        out.append(_run(pending.text, pending.rpr))
    return "".join(out)


def _e(atoms, tag: str = "m:e") -> str:
    body = _emit(atoms) or _run(_ZWSP)
    return f"<{tag}>{body}</{tag}>"


class _Parser:
    def __init__(self, source: str, unsupported: set[str]) -> None:
        self.tokens = _TOKEN.findall(source)
        self.pos = 0
        self.rpr = ""
        self.unsupported = unsupported
        self.aligning = False
        self.pending_aln = False

    def peek(self, skip_space: bool = True):
        if skip_space:
            while self.pos < len(self.tokens) and self.tokens[self.pos].isspace():
                self.pos += 1
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self):
        tok = self.peek()
        if tok is not None:
            self.pos += 1
        return tok

    def raw_group(self) -> str:
        """The text of the next ``{...}`` group, spaces included."""
        if self.peek() != "{":
            tok = self.next()
            return tok or ""
        self.pos += 1
        depth, out = 1, []
        while self.pos < len(self.tokens):
            tok = self.tokens[self.pos]
            self.pos += 1
            if tok == "{":
                depth += 1
            elif tok == "}":
                depth -= 1
                if depth == 0:
                    break
            out.append(tok)
        return "".join(out)

    def optional(self) -> str | None:
        if self.peek() != "[":
            return None
        self.pos += 1
        depth, out = 1, []
        while self.pos < len(self.tokens):
            tok = self.tokens[self.pos]
            self.pos += 1
            if tok == "[":
                depth += 1
            elif tok == "]":
                depth -= 1
                if depth == 0:
                    break
            out.append(tok)
        return "".join(out)

    def arg(self):
        """One argument: a brace group or a single atom."""
        if self.peek() == "{":
            self.pos += 1
            atoms = self.seq({"}"})
            if self.peek() == "}":
                self.pos += 1
            return atoms
        tok = self.peek()
        if tok is None:
            return []
        return [self.atom(frozenset())]

    def seq(self, stops) -> list:
        out: list = []
        while True:
            tok = self.peek()
            if tok is None or tok in stops:
                return out
            if tok in ("^", "_", "'"):
                base = out.pop() if out else _R(_ZWSP)
                out.append(self.scripts(base))
                continue
            atom = self.atom(stops)
            if atom is None:
                continue
            if self.peek() in ("^", "_", "'"):
                atom = self.scripts(atom)
            out.append(atom)

    def scripts(self, base):
        sub = sup = None
        primes = 0
        while True:
            tok = self.peek()
            if tok == "'":
                self.pos += 1
                primes += 1
            elif tok == "^" and sup is None:
                self.pos += 1
                sup = self.arg()
            elif tok == "_" and sub is None:
                self.pos += 1
                sub = self.arg()
            else:
                break
        if primes:
            sup = [_R("′" * primes)] + (sup or [])
        if isinstance(base, _X) and base.limit:
            xml = base.xml
            if sub is not None:
                xml = f"<m:limLow><m:e>{xml}</m:e>{_e(sub, 'm:lim')}</m:limLow>"
            if sup is not None:
                xml = f"<m:limUpp><m:e>{xml}</m:e>{_e(sup, 'm:lim')}</m:limUpp>"
            return _X(xml)
        if sub is not None and sup is not None:
            return _X(f"<m:sSubSup>{_e([base])}{_e(sub, 'm:sub')}{_e(sup, 'm:sup')}</m:sSubSup>")
        if sub is not None:
            return _X(f"<m:sSub>{_e([base])}{_e(sub, 'm:sub')}</m:sSub>")
        return _X(f"<m:sSup>{_e([base])}{_e(sup, 'm:sup')}</m:sSup>")

    def run(self, text: str, rpr: str | None = None) -> _R:
        props = self.rpr if rpr is None else rpr
        if self.pending_aln:
            self.pending_aln = False
            return _R(text, props + "<m:aln/>")
        return _R(text, props)

    def atom(self, stops):
        tok = self.next()
        if tok == "{":
            saved = self.rpr
            atoms = self.seq({"}"})
            self.rpr = saved
            if self.peek() == "}":
                self.pos += 1
            return _G(atoms)
        if tok == "&":
            if not self.aligning:
                return self.run("&")
            if self.next_is_plain_run():
                self.pending_aln = True
                return None
            return _R(_ZWSP, "<m:aln/>")  # the alignment point cannot ride on a fraction or an integral
        if tok == "~":
            return self.run(SPACES["~"])
        if tok.startswith("\\"):
            return self.command(tok, stops)
        if tok.isdigit() or (tok == "." and (self.peek(False) or "").isdigit()):
            number = tok
            while self.pos < len(self.tokens) and (self.tokens[self.pos].isdigit() or (self.tokens[self.pos] == "." and self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1].isdigit())):
                number += self.tokens[self.pos]
                self.pos += 1
            # A script binds to the last digit in LaTeX, but looks the same on the whole number.
            return self.run(number)
        return self.run(tok)

    def next_is_plain_run(self) -> bool:
        tok = self.peek()
        if tok is None or tok in ("{", "}", "\\\\", "^", "_", "&") or (tok.startswith("\\") and tok[1:] not in SYMBOLS):
            return False
        following = self.pos + 1
        while following < len(self.tokens) and self.tokens[following].isspace():
            following += 1
        return following >= len(self.tokens) or self.tokens[following] not in ("^", "_", "'")

    def command(self, tok: str, stops):
        name = tok[1:]
        name = _ALIASES.get(name, name)
        if name in (":", "<", ">") and tok[1:] in _ALIASES:
            return self.run(name)
        if tok in SPACES:
            return self.run(SPACES[tok], "")
        if name in _IGNORED:
            return None
        if name in NARY:
            return self.nary(name, stops)
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            num, den = self.arg(), self.arg()
            return _X(f'<m:f><m:fPr><m:type m:val="bar"/></m:fPr>{_e(num, "m:num")}{_e(den, "m:den")}</m:f>')
        if name in ("binom", "dbinom", "tbinom"):
            top, bottom = self.arg(), self.arg()
            return _X(f'<m:d><m:dPr><m:begChr m:val="("/><m:endChr m:val=")"/></m:dPr><m:e><m:f><m:fPr><m:type m:val="noBar"/></m:fPr>{_e(top, "m:num")}{_e(bottom, "m:den")}</m:f></m:e></m:d>')
        if name == "sqrt":
            degree = self.optional()
            body = self.arg()
            if degree is None:
                return _X(f'<m:rad><m:radPr><m:degHide m:val="1"/></m:radPr><m:deg/>{_e(body)}</m:rad>')
            return _X(f"<m:rad><m:radPr/>{_e(_Parser(degree, self.unsupported).seq(frozenset()), 'm:deg')}{_e(body)}</m:rad>")
        if name in FUNCS:
            return self.run(name, '<m:sty m:val="p"/>')
        if name in LIMIT_FUNCS:
            return _X(_run(_FUNC_TEXT.get(name, name), '<m:sty m:val="p"/>'), limit=True)
        if name in ("operatorname", "operatorname*"):
            text = self.raw_group()
            xml = _run(re.sub(r"\\[,;: ]", " ", text).replace("\\", ""), '<m:sty m:val="p"/>')
            return _X(xml, limit=name.endswith("*"))
        if name in ACCENTS:
            return _X(f"<m:acc><m:accPr><m:chr m:val={quoteattr(ACCENTS[name])}/></m:accPr>{_e(self.arg())}</m:acc>")
        if name in ("overline", "underline"):
            pos = "top" if name == "overline" else "bot"
            return _X(f'<m:bar><m:barPr><m:pos m:val="{pos}"/></m:barPr>{_e(self.arg())}</m:bar>')
        if name in ("overbrace", "underbrace"):
            over = name == "overbrace"
            chr_, pos, jc = ("⏞", "top", "bot") if over else ("⏟", "bot", "top")
            return _X(f'<m:groupChr><m:groupChrPr><m:chr m:val="{chr_}"/><m:pos m:val="{pos}"/><m:vertJc m:val="{jc}"/></m:groupChrPr>{_e(self.arg())}</m:groupChr>', limit=True)
        if name in ("overset", "stackrel", "underset"):
            mark, body = self.arg(), self.arg()
            tag = "m:limLow" if name == "underset" else "m:limUpp"
            return _X(f"<{tag}>{_e(body)}{_e(mark, 'm:lim')}</{tag}>")
        if name in FONTS:
            saved, self.rpr = self.rpr, FONTS[name]
            atoms = self.arg()
            self.rpr = saved
            return _G(atoms)
        if name in ("rm", "bf", "it", "sf", "tt", "cal"):  # old switches: to the end of the group
            self.rpr = {"rm": FONTS["mathrm"], "bf": FONTS["mathbf"], "it": FONTS["mathit"], "sf": FONTS["mathsf"], "tt": FONTS["mathtt"], "cal": FONTS["mathcal"]}[name]
            return None
        if name in ("text", "textrm", "textnormal", "mbox", "hbox", "textbf", "textit", "textsf", "texttt", "textsc", "textsl", "textup", "textmd"):
            text = re.sub(r"\\([%$&#_{}])", r"\1", self.raw_group()).replace("~", "\u00a0")
            return _R(text, "<m:nor/>")
        if name == "left":
            return self.delimited()
        if name == "begin":
            return self.environment()
        if name in ("hspace", "hspace*", "vspace", "vspace*", "label", "tag", "tag*", "stepcounter", "refstepcounter"):
            self.raw_group()
            return None
        if name in ("addtocounter", "setcounter"):
            self.raw_group()
            self.raw_group()
            return None
        if name in ("phantom", "hphantom", "vphantom", "cancel", "boxed", "mathclap", "mathllap", "mathrlap", "ensuremath"):
            return _G(self.arg())
        if name == "pmod":
            body = self.arg()
            return _G([_R("("), _R("mod", '<m:sty m:val="p"/>'), _R(SPACES["\\,"]), _G(body), _R(")")])
        if name in ("bmod", "mod"):
            return self.run("mod", '<m:sty m:val="p"/>')
        if name == "not":
            nxt = self.atom(stops)
            if isinstance(nxt, _R) and len(nxt.text) == 1:
                return _R(unicodedata.normalize("NFC", nxt.text + "\u0338"), nxt.rpr)
            return nxt
        if name in SYMBOLS:
            return self.run(SYMBOLS[name])
        if len(name) == 1 and not name.isalpha():
            return self.run(name)
        if name in ("right", "middle", "end"):
            self.unsupported.add(tok)
            return None
        self.unsupported.add(tok)
        return _R(tok, "<m:nor/>")

    def nary(self, name: str, stops):
        sub = sup = None
        while True:
            tok = self.peek()
            if tok in ("\\limits", "\\nolimits"):
                self.pos += 1
            elif tok == "_" and sub is None:
                self.pos += 1
                sub = self.arg()
            elif tok == "^" and sup is None:
                self.pos += 1
                sup = self.arg()
            else:
                break
        body = self.seq(frozenset(stops) | _RELATIONS)
        loc = "subSup" if name in INTEGRALS else "undOvr"
        hide = ('<m:subHide m:val="1"/>' if sub is None else "") + ('<m:supHide m:val="1"/>' if sup is None else "")
        return _X(f'<m:nary><m:naryPr><m:chr m:val="{NARY[name]}"/><m:limLoc m:val="{loc}"/>{hide}</m:naryPr>{_e(sub or [], "m:sub") if sub is not None else "<m:sub/>"}{_e(sup or [], "m:sup") if sup is not None else "<m:sup/>"}{_e(body)}</m:nary>')

    def delim_char(self) -> str:
        tok = self.next()
        if tok is None or tok == ".":
            return ""
        if tok.startswith("\\"):
            name = _ALIASES.get(tok[1:], tok[1:])
            return SYMBOLS.get(name, name)
        return tok

    def delimited(self):
        begin = self.delim_char()
        parts = [self.seq({"\\right", "\\middle"})]
        sep = ""
        while self.peek() == "\\middle":
            self.pos += 1
            sep = self.delim_char()
            parts.append(self.seq({"\\right", "\\middle"}))
        end = ""
        if self.peek() == "\\right":
            self.pos += 1
            end = self.delim_char()
        sep_attr = f"<m:sepChr m:val={quoteattr(sep)}/>" if sep else ""
        return _X(f"<m:d><m:dPr><m:begChr m:val={quoteattr(begin)}/>{sep_attr}<m:endChr m:val={quoteattr(end)}/></m:dPr>{''.join(_e(p) for p in parts)}</m:d>")

    def environment(self):
        env = self.raw_group()
        if env == "array":
            self.raw_group()
        if env == "alignat" or env == "alignat*":
            self.raw_group()
        if env == "cases" or env == "dcases":
            rows = self.table_rows()
            body = _matrix(rows, "left")
            return _X(f'<m:d><m:dPr><m:begChr m:val="{{"/><m:endChr m:val=""/></m:dPr><m:e>{body}</m:e></m:d>')
        if env in _MATRICES:
            rows = self.table_rows()
            begin, end = _MATRICES[env]
            body = _matrix(rows, "center")
            if not begin:
                return _X(body)
            return _X(f"<m:d><m:dPr><m:begChr m:val={quoteattr(begin)}/><m:endChr m:val={quoteattr(end)}/></m:dPr><m:e>{body}</m:e></m:d>")
        if env in _ROWS_ENVS or env in ("equation", "equation*", "displaymath"):
            return _X(self.aligned_rows())
        self.unsupported.add(f"\\begin{{{env}}}")
        return _G(self.seq({"\\end"}) + [self.finish_env()])

    def finish_env(self):
        if self.peek() == "\\end":
            self.pos += 1
            self.raw_group()
        return _G()

    def table_rows(self) -> list[list[list]]:
        saved, self.aligning = self.aligning, False
        rows: list[list[list]] = []
        row: list[list] = []
        while True:
            cell = self.seq({"&", "\\\\", "\\end", "\\hline", "\\cr"})
            tok = self.peek()
            if tok == "&":
                self.pos += 1
                row.append(cell)
                continue
            if tok == "\\hline":
                self.pos += 1
                if cell:
                    row.append(cell)
                continue
            row.append(cell)
            if tok in ("\\\\", "\\cr"):
                self.pos += 1
                self.optional() if self.peek() == "[" else None
                rows.append(row)
                row = []
                continue
            if any(c for c in row):
                rows.append(row)
            self.finish_env()
            break
        self.aligning = saved
        return rows

    def aligned_rows(self, *, top: bool = False) -> str:
        saved, self.aligning = self.aligning, True
        rows: list[list] = []
        while True:
            row = self.seq({"\\\\", "\\end", "\\cr"})
            tok = self.peek()
            self.pending_aln = False
            if tok in ("\\\\", "\\cr"):
                self.pos += 1
                self.optional() if self.peek() == "[" else None
                rows.append(row)
                continue
            if row or not rows:
                rows.append(row)
            if not top:
                self.finish_env()
            break
        self.aligning = saved
        return "<m:eqArr>" + "".join(_e(r) for r in rows) + "</m:eqArr>"


def _matrix(rows: list[list[list]], jc: str) -> str:
    cols = max((len(r) for r in rows), default=1)
    props = f'<m:mPr><m:plcHide m:val="1"/><m:mcs><m:mc><m:mcPr><m:count m:val="{cols}"/><m:mcJc m:val="{jc}"/></m:mcPr></m:mc></m:mcs></m:mPr>'
    body = "".join("<m:mr>" + "".join(_e(c) for c in r + [[]] * (cols - len(r))) + "</m:mr>" for r in rows)
    return f"<m:m>{props}{body}</m:m>"


def latex_to_omml(latex: str, *, rows: bool = False) -> tuple[str, set[str]]:
    """The OMML children for ``latex``, and the commands it could not translate.

    ``rows`` treats ``latex`` as the body of an align-like environment: rows
    separated by ``\\\\`` with ``&`` alignment points.
    """
    unsupported: set[str] = set()
    parser = _Parser(latex, unsupported)
    if rows:
        return parser.aligned_rows(top=True), unsupported
    atoms = parser.seq(frozenset())
    while parser.peek() is not None:  # a stray closer: skip it rather than losing the rest
        unsupported.add(parser.next())
        atoms += parser.seq(frozenset())
    return _emit(atoms), unsupported


# --- OMML to LaTeX ------------------------------------------------------------


def _local(el) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _m(name: str) -> str:
    return f"{{{M_NS}}}{name}"


def _val(el, name: str, default: str | None = None) -> str | None:
    if el is None:
        return default
    child = el.find(_m(name))
    if child is None:
        return default
    return child.get(_m("val"), "1")


def _is_on(value: str | None) -> bool:
    return value not in (None, "0", "false", "off")


def demath(ch: str) -> tuple[str, str]:
    """A Mathematical Alphanumeric character as ``(plain character, font command)``."""
    if ord(ch) < 0x1D400 or ord(ch) > 0x1D7FF:
        if ch == "ℎ":
            return "h", ""
        return ch, ""
    name = unicodedata.name(ch, "")
    decomposed = unicodedata.normalize("NFKC", ch)
    if name.startswith("MATHEMATICAL DOUBLE-STRUCK"):
        return decomposed, "mathbb"
    if "FRAKTUR" in name:
        return decomposed, "mathfrak"
    if "SCRIPT" in name:
        return decomposed, "mathcal"
    if "MONOSPACE" in name:
        return decomposed, "mathtt"
    if "SANS-SERIF" in name:
        return decomposed, "mathsf"
    if "BOLD ITALIC" in name:
        return decomposed, "boldsymbol"
    if "BOLD" in name:
        return decomposed, "mathbf"
    return decomposed, ""


_CMD_TAIL = re.compile(r"\\[a-zA-Z]+$")


def _join(parts: list[str]) -> str:
    out = ""
    for part in parts:
        if not part:
            continue
        if _CMD_TAIL.search(out) and part[0].isalpha():
            out += " "
        out += part
    return out


def _char(ch: str) -> str:
    if ch in SPACES_REV:
        return SPACES_REV[ch]
    if ch == "−":
        return "-"
    if ch == "′":
        return "'"
    if ch in ("%", "$", "#", "_", "{", "}", "&"):
        return "\\" + ch
    if ch == "\\":
        return "\\backslash"
    cmd = UNICODE_TO_CMD.get(ch)
    if cmd:
        return "\\" + cmd
    parts = unicodedata.normalize("NFD", ch)
    if len(parts) == 2 and parts[1] == "\u0338":  # a negated relation
        return "\\not" + _char(parts[0]) if parts[0] != "=" else "\\neq"
    if ch == "\u0338":
        return "\\not"
    return ch


def _escape_text_mode(text: str) -> str:
    text = re.sub(r"([%$&#_{}])", r"\\\1", text.replace("\\", "\x00"))
    return text.replace("\x00", "\\textbackslash{}").replace("\u00a0", "~")


def _font_of(rpr) -> str:
    """The LaTeX font command for a run's properties, or ``""``."""
    if rpr is None:
        return ""
    scr, sty = _val(rpr, "scr"), _val(rpr, "sty")
    fonts = {"double-struck": "mathbb", "fraktur": "mathfrak", "script": "mathcal", "sans-serif": "mathsf", "monospace": "mathtt"}
    if scr in fonts:
        return fonts[scr]
    return {"b": "mathbf", "bi": "boldsymbol", "p": "mathrm"}.get(sty or "", "")


def _run_latex(el) -> str:
    rpr = el.find(_m("rPr"))
    text = "".join(t.text or "" for t in el.findall(_m("t"))).replace(_ZWSP, "")
    align = "& " if rpr is not None and rpr.find(_m("aln")) is not None else ""
    if not text:
        return align.strip()
    if rpr is not None and _is_on(_val(rpr, "nor")):
        return align + f"\\text{{{_escape_text_mode(text)}}}"
    font = _font_of(rpr)
    if font == "mathrm" and not any(c.isalnum() for c in text):
        font = ""  # an upright operator is just the operator
    if font == "mathrm":
        word = text.strip().replace(" ", "")
        if word in ("argmax", "argmin"):
            return align + f"\\operatorname*{{{word}}}"
        if word in FUNCS or word in LIMIT_FUNCS:
            return align + "\\" + word
        if len(word) > 1 and word.isalpha():
            return align + f"\\operatorname{{{word}}}"
    groups: list[tuple[str, str]] = []  # (font, latex)
    for ch in text:
        plain, implied = demath(ch)
        use = implied or font
        piece = _char(plain) if len(plain) == 1 else plain
        if groups and groups[-1][0] == use:
            groups[-1] = (use, _join([groups[-1][1], piece]))
        else:
            groups.append((use, piece))
    return align + _join([f"\\{f}{{{body}}}" if f else body for f, body in groups])


def _children(el) -> str:
    return _join([_node(c) for c in el if not _local(c).endswith("Pr") and _local(c) != "ctrlPr"])


def _arg(el, name: str) -> str:
    child = el.find(_m(name))
    return _children(child) if child is not None else ""


def _script(text: str) -> str:
    return text if len(text) == 1 else "{" + text + "}"


def _base(text: str) -> str:
    """A script base: compound material needs braces to keep its scope."""
    if len(text) <= 1 or re.fullmatch(r"\\?[A-Za-z]+|\d+|\\[a-zA-Z]+\{[^{}]*\}", text):
        return text
    return "{" + text + "}"


def _delim(ch: str | None, side: str) -> str:
    if ch is None or ch == "":
        return f"\\{side}."
    if ch in ("{", "}"):
        return f"\\{side}\\{ch}"
    if ch in ("∥", "‖"):
        return f"\\{side}\\Vert"
    if ch == "∣":
        return f"\\{side}|"
    cmd = UNICODE_TO_CMD.get(ch)
    if cmd and ch not in "()[]|":
        return f"\\{side}\\{cmd}"
    return f"\\{side}{ch}"


def _matrix_body(el) -> str:
    rows = []
    for row in el.findall(_m("mr")):
        rows.append(" & ".join(_children(cell) for cell in row.findall(_m("e"))))
    return " \\\\ ".join(rows)


def _node(el) -> str:
    tag = _local(el)
    if tag == "r":
        return _run_latex(el)
    if tag in ("oMath", "oMathPara", "e", "num", "den", "sub", "sup", "lim", "deg", "fName"):
        return _children(el)
    if tag == "f":
        kind = _val(el.find(_m("fPr")), "type", "bar")
        num, den = _arg(el, "num"), _arg(el, "den")
        if kind == "noBar":
            return f"\\genfrac{{}}{{}}{{0pt}}{{}}{{{num}}}{{{den}}}"
        if kind == "lin":
            return f"{_base(num)}/{_base(den)}"
        return f"\\frac{{{num}}}{{{den}}}"
    if tag == "sSup":
        sup = _arg(el, "sup")
        base = _base(_arg(el, "e"))
        if sup and set(sup) == {"'"}:
            return base + sup
        return f"{base}^{_script(sup)}"
    if tag == "sSub":
        return f"{_base(_arg(el, 'e'))}_{_script(_arg(el, 'sub'))}"
    if tag == "sSubSup":
        return f"{_base(_arg(el, 'e'))}_{_script(_arg(el, 'sub'))}^{_script(_arg(el, 'sup'))}"
    if tag == "sPre":
        return f"{{}}_{_script(_arg(el, 'sub'))}^{_script(_arg(el, 'sup'))}{_arg(el, 'e')}"
    if tag == "rad":
        deg, body = _arg(el, "deg"), _arg(el, "e")
        hidden = _is_on(_val(el.find(_m("radPr")), "degHide"))
        return f"\\sqrt{{{body}}}" if hidden or not deg else f"\\sqrt[{deg}]{{{body}}}"
    if tag == "nary":
        pr = el.find(_m("naryPr"))
        ch = _val(pr, "chr", "∫")
        cmd = "\\" + NARY_REV[ch] if ch in NARY_REV else ch
        sub, sup = _arg(el, "sub"), _arg(el, "sup")
        out = cmd
        if sub and not _is_on(_val(pr, "subHide")):
            out += f"_{_script(sub)}"
        if sup and not _is_on(_val(pr, "supHide")):
            out += f"^{_script(sup)}"
        return _join([out, " " + _arg(el, "e")])
    if tag == "func":
        name = _arg(el, "fName")
        return _join([name, " ", _arg(el, "e")])
    if tag == "limLow":
        base, lim = _arg(el, "e"), _arg(el, "lim")
        if re.fullmatch(r"\\[a-zA-Z]+|\\operatorname\*\{\w+\}", base):
            return f"{base}_{_script(lim)}"
        if base.startswith("\\underbrace"):
            return f"{base}_{_script(lim)}"
        return f"\\underset{{{lim}}}{{{base}}}"
    if tag == "limUpp":
        base, lim = _arg(el, "e"), _arg(el, "lim")
        if base.startswith("\\overbrace"):
            return f"{base}^{_script(lim)}"
        return f"\\overset{{{lim}}}{{{base}}}"
    if tag == "acc":
        ch = _val(el.find(_m("accPr")), "chr", "\u0302")
        name = ACCENTS_REV.get(ch, "hat")
        return f"\\{name}{{{_arg(el, 'e')}}}"
    if tag == "bar":
        pos = _val(el.find(_m("barPr")), "pos", "bot")
        return f"\\{'overline' if pos == 'top' else 'underline'}{{{_arg(el, 'e')}}}"
    if tag == "groupChr":
        pr = el.find(_m("groupChrPr"))
        ch = _val(pr, "chr", "⏟")
        return f"\\{'overbrace' if ch == '⏞' else 'underbrace'}{{{_arg(el, 'e')}}}"
    if tag in ("box", "borderBox", "phant"):
        return _arg(el, "e")
    if tag == "eqArr":
        return "\\begin{aligned} " + " \\\\ ".join(_children(row) for row in el.findall(_m("e"))) + " \\end{aligned}"
    if tag == "m":
        return "\\begin{matrix} " + _matrix_body(el) + " \\end{matrix}"
    if tag == "d":
        return _delimiter(el)
    return _children(el)


def _delimiter(el) -> str:
    pr = el.find(_m("dPr"))
    begin, end = _val(pr, "begChr", "("), _val(pr, "endChr", ")")
    sep = _val(pr, "sepChr", "|")
    parts = el.findall(_m("e"))
    if len(parts) == 1:
        inner = [c for c in parts[0] if not _local(c).endswith("Pr")]
        if len(inner) == 1:
            only = inner[0]
            if _local(only) == "f" and _val(only.find(_m("fPr")), "type", "bar") == "noBar" and (begin, end) == ("(", ")"):
                return f"\\binom{{{_arg(only, 'num')}}}{{{_arg(only, 'den')}}}"
            if _local(only) in ("m", "eqArr") and begin == "{" and end == "":
                if _local(only) == "m":
                    return "\\begin{cases} " + _matrix_body(only) + " \\end{cases}"
                return "\\begin{cases} " + " \\\\ ".join(_children(row) for row in only.findall(_m("e"))) + " \\end{cases}"
            if _local(only) == "m":
                env = {("(", ")"): "pmatrix", ("[", "]"): "bmatrix", ("{", "}"): "Bmatrix", ("|", "|"): "vmatrix", ("‖", "‖"): "Vmatrix"}.get((begin, end))
                if env:
                    return f"\\begin{{{env}}} " + _matrix_body(only) + f" \\end{{{env}}}"
    middle = f" \\middle{'|' if sep == '|' else _char(sep or '|')} "
    return _delim(begin, "left") + " " + middle.join(_children(p) for p in parts) + " " + _delim(end, "right")


def omml_to_latex(el) -> str:
    """LaTeX for an ``<m:oMath>`` element (or any OMML element)."""
    return re.sub(r"[ \t]+", " ", _node(el)).strip()


def omml_rows_to_latex(el) -> tuple[str, bool]:
    """``(body, has_rows)``: an equation that is one ``<m:eqArr>`` as align-style rows."""
    kids = [c for c in el if not _local(c).endswith("Pr")]
    if len(kids) == 1 and _local(kids[0]) == "eqArr":
        rows = [_children(row).strip() for row in kids[0].findall(_m("e"))]
        return " \\\\\n".join(rows), True
    return omml_to_latex(el), False


# --- signatures ---------------------------------------------------------------

_DEFAULTS = {"chr": {"nary": "∫", "acc": "\u0302", "groupChr": "⏟"}, "begChr": {"d": "("}, "endChr": {"d": ")"}, "type": {"f": "bar"}, "pos": {"bar": "bot", "groupChr": "bot"}}


def _canon(el, out: list[str]) -> None:
    tag = _local(el)
    if tag == "r":
        rpr = el.find(_m("rPr"))
        style = f"{_val(rpr, 'scr', '')}/{_val(rpr, 'sty', '')}/{int(_is_on(_val(rpr, 'nor')))}" if rpr is not None else "//0"
        text = "".join(t.text or "" for t in el.findall(_m("t"))).replace(_ZWSP, "")
        text = "".join(demath(c)[0] for c in text if not c.isspace()).replace("−", "-")  # Word rewrites spacing and minus signs
        if not text:
            return
        if out and out[-1].startswith(f"r:{style}:"):
            out[-1] += text
        else:
            out.append(f"r:{style}:{text}")
        return
    if tag.endswith("Pr") or tag == "ctrlPr":
        return
    props = el.find(_m(tag + "Pr"))
    extra = []
    for name in ("chr", "begChr", "endChr", "type", "pos"):
        value = _val(props, name)
        if value is None:
            value = _DEFAULTS.get(name, {}).get(tag)
        if value is not None:
            extra.append(f"{name}={value}")
    out.append(f"<{tag}{' '.join(extra)}>")
    for child in el:
        _canon(child, out)
    out.append(f"</{tag}>")


def signature(el) -> str:
    """A hash of an equation's structure, stable across a save in Word."""
    out: list[str] = []
    for child in el:
        _canon(child, out)
    return hashlib.sha256("".join(out).encode("utf-8")).hexdigest()[:16]


def signature_of_xml(inner: str) -> str:
    """:func:`signature` of OMML children written by :func:`latex_to_omml`."""
    root = ET.fromstring(f"<m:oMath {NS_DECL}>{inner}</m:oMath>")  # nosec B314 -- generated in this module
    return signature(root)
