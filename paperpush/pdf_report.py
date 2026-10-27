"""Write a validation report as a PDF, keeping its colours.

A deliberately small PDF writer, so saving a report needs no third-party
package: US Letter pages, the standard Helvetica fonts (which every PDF viewer
has, so nothing is embedded), word-wrapped coloured text, and a page footer.
Text is encoded as WinAnsi (cp1252); characters outside it are replaced, which
is why the status markers here are ASCII (``[PASS]``, ``[FAIL]``, ...).

:func:`render_pdf` takes the line layout :mod:`paperpush.report` builds for
every output format and returns the PDF bytes.
"""

from __future__ import annotations

import zlib
from datetime import date

PAGE_WIDTH = 612.0
PAGE_HEIGHT = 792.0
MARGIN = 54.0
INDENT = 16.0

# Advance widths (1/1000 em) of the printable ASCII characters 32..126, from
# the Adobe Font Metrics of the standard 14 fonts. Oblique shares Helvetica's.
_HELVETICA = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]  # fmt: skip
_HELVETICA_BOLD = [
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
]  # fmt: skip
_FONTS = {"regular": ("F1", "Helvetica", _HELVETICA), "bold": ("F2", "Helvetica-Bold", _HELVETICA_BOLD), "italic": ("F3", "Helvetica-Oblique", _HELVETICA)}

# Common typography outside cp1252's reach, mapped to something it has.
_REPLACE = {"✔": "+", "✖": "x", "▲": "!", "→": "->", "≥": ">=", "≤": "<=", "×": "x", "−": "-", "µ": "u"}


def _clean(text: str) -> str:
    for src, dst in _REPLACE.items():
        text = text.replace(src, dst)
    return text.encode("cp1252", "replace").decode("cp1252")


def _width(text: str, style: str, size: float) -> float:
    table = _FONTS[style][2]
    total = 0
    for ch in text:
        code = ord(ch)
        total += table[code - 32] if 32 <= code <= 126 else 556
    return total * size / 1000.0


def _rgb(color: str) -> tuple[float, float, float]:
    color = (color or "#000000").lstrip("#")
    return tuple(int(color[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _escape(text: str) -> bytes:
    raw = _clean(text).encode("cp1252", "replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


class _Writer:
    """Lays runs of styled text onto pages, wrapping and breaking pages as needed."""

    def __init__(self) -> None:
        self.pages: list[list[bytes]] = []
        self.y = 0.0
        self._new_page()

    def _new_page(self) -> None:
        self.pages.append([])
        self.y = PAGE_HEIGHT - MARGIN

    def space(self, points: float) -> None:
        self.y -= points

    def _ensure(self, height: float) -> None:
        if self.y - height < MARGIN + 14:
            self._new_page()

    def _show(self, x: float, text: str, style: str, size: float, color: str) -> None:
        font = _FONTS[style][0]
        r, g, b = _rgb(color)
        self.pages[-1].append(b"BT /" + font.encode() + f" {size:g} Tf {r:.3f} {g:.3f} {b:.3f} rg {x:.2f} {self.y:.2f} Td (".encode() + _escape(text) + b") Tj ET\n")

    def rule(self, color: str, width: float = 0.8) -> None:
        r, g, b = _rgb(color)
        self.pages[-1].append(f"{r:.3f} {g:.3f} {b:.3f} RG {width:g} w {MARGIN:.2f} {self.y:.2f} m {PAGE_WIDTH - MARGIN:.2f} {self.y:.2f} l S\n".encode())

    def paragraph(self, runs: list[tuple[str, str, str]], size: float, indent: float = 0.0, hanging: float = 0.0, leading: float | None = None) -> None:
        """Write ``runs`` of ``(text, style, color)`` as one wrapped paragraph.

        Continuation lines start ``hanging`` points further in than the first.
        """
        leading = leading or size * 1.32
        left = MARGIN + indent
        limit = PAGE_WIDTH - MARGIN
        # Split every run into words, keeping each word's style and colour.
        words: list[tuple[str, str, str]] = []
        for text, style, color in runs:
            parts = _clean(text).split(" ")
            for i, part in enumerate(parts):
                words.append((part + (" " if i < len(parts) - 1 else ""), style, color))
        lines: list[list[tuple[str, str, str]]] = [[]]
        x = left
        for word, style, color in words:
            if lines[-1] and x + _width(word.rstrip(" "), style, size) > limit:
                lines.append([])
                x = left + hanging
            # A single word longer than the line (a long path or URL) is hard-broken.
            while not lines[-1] and len(word) > 1 and x + _width(word.rstrip(" "), style, size) > limit:
                cut = len(word)
                while cut > 1 and x + _width(word[:cut], style, size) > limit:
                    cut -= 1
                lines[-1].append((word[:cut], style, color))
                lines.append([])
                x = left + hanging
                word = word[cut:]
            lines[-1].append((word, style, color))
            x += _width(word, style, size)
        for number, line in enumerate(lines):
            self._ensure(leading)
            self.y -= leading
            x = left + (hanging if number else 0)
            for text, style, color in line:
                if text:
                    self._show(x, text, style, size, color)
                    x += _width(text, style, size)


def render_pdf(lines, summary, palette, marks) -> bytes:
    """The PDF for a report laid out by :func:`paperpush.report._layout`."""
    w = _Writer()
    black, grey = "#1f2328", "#59636e"
    for line in lines:
        color = palette.for_status(line.status) if line.status else black
        indent = INDENT * line.indent
        if line.role == "title":
            w.paragraph([(line.text, "bold", black)], 16)
            w.space(4)
        elif line.role == "meta":
            w.paragraph([(line.text, "regular", grey)], 9)
        elif line.role == "summary":
            w.space(8)
            w.paragraph([(line.text, "bold", color)], 12)
            runs = []
            for i, (status, text) in enumerate(summary):
                runs.append((("    " if i else "") + f"{marks[status]} {text}", "bold", palette.for_status(status)))
            w.paragraph(runs, 9.5)
        elif line.role == "heading":
            w.space(12)
            w.paragraph([(line.text.upper(), "bold", color)], 11.5)
            w.space(3)
            w.rule(color)
            w.space(2)
        elif line.role == "check":
            mark = marks.get(line.status, "")
            style = "bold" if line.status in ("failed", "warning") else "regular"
            w.space(3)
            w.paragraph([(f"{mark} ", "bold", color), (line.text, style, color)], 10, indent=indent - INDENT, hanging=_width(mark + " ", "bold", 10))
        elif line.role == "item":
            w.paragraph([("- ", "regular", color), (line.text, "regular", color)], 9, indent=indent - INDENT + 8, hanging=_width("- ", "regular", 9))
        else:
            w.paragraph([(line.text, "italic", color)], 9, indent=max(0.0, indent - INDENT + 8))
    return _assemble(w.pages)


def _assemble(pages: list[list[bytes]]) -> bytes:
    objects: list[bytes] = []
    n = len(pages)
    # 1 catalog, 2 pages, 3-5 fonts, 6 info, then (page, contents) pairs.
    kids = " ".join(f"{7 + 2 * i} 0 R" for i in range(n))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    for _, base, _w in _FONTS.values():
        objects.append(f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} /Encoding /WinAnsiEncoding >>".encode())
    objects.append(f"<< /Producer (paperpush) /Title (paperpush validation report) /CreationDate (D:{date.today():%Y%m%d}) >>".encode())
    fonts = " ".join(f"/{key} {3 + i} 0 R" for i, (key, _, _w) in enumerate(_FONTS.values()))
    for number, ops in enumerate(pages, start=1):
        footer = f"BT /F1 8 Tf 0.45 0.45 0.45 rg {MARGIN:.2f} {MARGIN - 18:.2f} Td (paperpush validate  -  page {number} of {n}) Tj ET\n".encode()
        stream = zlib.compress(b"".join(ops) + footer)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH:g} {PAGE_HEIGHT:g}] /Resources << /Font << {fonts} >> >> /Contents {8 + 2 * (number - 1)} 0 R >>".encode())
        objects.append(f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode() + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
