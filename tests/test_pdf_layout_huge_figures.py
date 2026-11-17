"""A huge embedded vector figure is laid out as an empty box, not tokenized (ICLR 2026 timeouts).

Two desk-rejected ICLR 2026 papers (qkaKLF9BgZ, xgdPfzy9Ww) embed scatter plots whose form
XObjects decode to 10-74 MB; pdfminer spent about a minute per page tokenizing them, and
validate timed out after 15 min. The layout reader now keeps such a figure's box and skips
its contents, so the text around it is still read.
"""

from __future__ import annotations

import pytest

from paperpush import pdf_layout

pytest.importorskip("pdfminer")


def _pdf_with_form(path):
    """One page with body text and a form XObject (a 'figure') holding its own text."""
    form = b"BT /F1 12 Tf 10 10 Td (INSIDEFIGURE) Tj ET"
    page = b"BT /F1 12 Tf 72 700 Td (BODYTEXT) Tj ET q 1 0 0 1 72 400 cm /Fm1 Do Q"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> /XObject << /Fm1 6 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(page) + page + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 200 100] /Resources << /Font << /F1 5 0 R >> >>"
        b" /Length %d >>\nstream\n" % len(form) + form + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def _text(pages):
    return "".join(g.text for p in pages for g in p.glyphs)


def test_small_figures_are_read(tmp_path):
    pages = pdf_layout.read_pages(_pdf_with_form(tmp_path / "small.pdf"))
    assert "BODYTEXT" in _text(pages) and "INSIDEFIGURE" in _text(pages)


def test_huge_figures_keep_their_box_but_not_their_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_layout, "MAX_FORM_BYTES", 10)  # the test form is ~40 bytes
    pdf_layout._cached.cache_clear()
    pages = pdf_layout.read_pages(_pdf_with_form(tmp_path / "huge.pdf"))
    assert "BODYTEXT" in _text(pages)
    assert "INSIDEFIGURE" not in _text(pages)
    assert pages[0].figure_boxes  # the figure is still there, as an empty box
