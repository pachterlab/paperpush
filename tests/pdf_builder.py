"""Write tiny PDFs from raw content streams, for tests of glyph-level checks.

The hidden-text and template checks look at how each character is painted
(colour, render mode, size, position), which needs real PDF content streams.
This builds them by hand -- one Helvetica font resource, US Letter pages -- so
the tests need no TeX installation.
"""

from __future__ import annotations

from pathlib import Path


def text_line(x: float, y: float, text: str, *, size: float = 10, gray: float = 0.0, render: int = 0) -> str:
    """Content-stream operators that show ``text`` with its baseline at (x, y)."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {size:g} Tf {gray:g} g {render} Tr {x:g} {y:g} Td ({escaped}) Tj ET\n"


def filled_rect(x: float, y: float, w: float, h: float, gray: float = 0.0) -> str:
    return f"{gray:g} g {x:g} {y:g} {w:g} {h:g} re f\n"


def write_pdf(path: Path, pages: list[str], *, width: float = 612, height: float = 792) -> Path:
    """Write ``pages`` (one content stream each) as a PDF at ``path``."""
    objects: list[bytes] = []
    n_pages = len(pages)
    # 1: catalog, 2: pages tree, 3: font, then (page, content) pairs.
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n_pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for i, stream in enumerate(pages):
        content = stream.encode("latin-1")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width:g} {height:g}] /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode())
        objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path
