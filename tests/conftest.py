"""Shared pytest fixtures for paperpush.

The main job here is to manufacture realistic sample submission files in a
temporary directory so tests can exercise the validation and submission paths
without shipping binary fixtures in the repository.

Builders are deterministic (no timestamps, no randomness) so the same inputs
always produce byte-identical files.

Available file types:

* manuscript / supplement documents -- ``.pdf`` (default) or ``.docx``
* figures -- ``.png`` (default), ``.jpg``/``.jpeg``, ``.tif``/``.tiff``,
  ``.pdf``, or ``.svg``

Use the :func:`samples` fixture for a factory bound to ``tmp_path``, or the
named convenience fixtures (``manuscript_pdf``, ``fig1_png``, ...) for the
common cases.
"""

from __future__ import annotations

import os
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from paperpush.database import list_venues

# --- opt-in real-portal tests ----------------------------------------------
#
# Only tests that hit a REAL submission portal (marked ``@pytest.mark.portal``)
# are skipped by default; everything else -- including submit logic that does
# not touch a live portal -- runs as usual. So a bare ``pytest`` exercises all
# offline behavior and skips only the network/portal tests. Opt in with
# ``pytest --run-portal`` or by setting ``PAPERPUSH_RUN_PORTAL=1`` (the
# monthly CI job does the latter).
#
# Mark a test with @pytest.mark.portal ONLY if it talks to a real portal.
# Offline submit tests should stay unmarked so they run automatically.


def pytest_addoption(parser):
    parser.addoption(
        "--run-portal",
        action="store_true",
        default=False,
        help="also run tests marked @pytest.mark.portal (these hit a real " "submission portal and are skipped by default)",
    )
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="rewrite the committed portal-page fingerprints in " "tests/portal_snapshots from the live pages instead of comparing " "against them (used with --run-portal after an intended portal change)",
    )
    parser.addoption(
        "--venue",
        action="append",
        default=None,
        metavar="SLUG",
        help="restrict the submit walkthrough to the named venue slug " "(exact match, repeatable). Unlike ``-k`` this does not " "substring-match, so ``--venue bioinformatics`` runs only " "bioinformatics and never bmc_bioinformatics.",
    )
    parser.addoption(
        "--subfile",
        action="append",
        default=None,
        metavar="PATH",
        help="run the submit walkthrough against these specific .sub files " "instead of each venue's canonical <slug>/<slug>.sub (repeatable). " "Each file's venue is read from the file itself, so --venue still " "filters and the walkthrough records success under the right venue " "(e.g. --subfile tests/sub_files/arxiv/arxiv_tex.sub).",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "portal: test hits a real submission portal; skipped unless " "--run-portal (or PAPERPUSH_RUN_PORTAL=1) is given",
    )


def _requested_venues(config) -> set[str] | None:
    """The exact set of venue slugs named by ``--venue``, or None if unset.

    Validates the slugs against the database so a typo fails loudly rather than
    silently deselecting every test. ``--venue`` is exact-match (unlike ``-k``,
    which is substring), so ``--venue bioinformatics`` never also selects
    ``bmc_bioinformatics``.
    """
    selected = config.getoption("--venue")
    if not selected:
        return None
    wanted = list(dict.fromkeys(selected))  # de-dupe, preserve order
    known = {j.slug for j in list_venues()}
    unknown = [s for s in wanted if s not in known]
    if unknown:
        raise pytest.UsageError(f"--venue: unknown venue slug(s) {unknown}; " f"choose from {sorted(known)}")
    return set(wanted)


def _item_venue(item) -> str | None:
    """The venue slug a collected test is parametrized for, or None.

    Covers every way a test names a venue in this suite: a ``slug`` string
    (drift, submit walkthrough), a ``venue`` :class:`Venue` object (subfile,
    login loops), or a ``scenario`` object carrying ``.venue`` (validate/submit
    sample sweeps). A test with no venue parameter returns None and is left
    alone by the filter.
    """
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return None
    params = callspec.params
    if "slug" in params:
        return params["slug"]
    if "venue" in params:
        return getattr(params["venue"], "slug", None)
    if "scenario" in params:
        return getattr(params["scenario"], "venue", None)
    return None


def pytest_collection_modifyitems(config, items):
    # Restrict venue-parametrized tests to those named by --venue (exact
    # match), before the portal opt-in skip below so a filtered-out portal test
    # is deselected rather than collected-then-skipped.
    wanted = _requested_venues(config)
    if wanted is not None:
        kept, deselected = [], []
        for item in items:
            venue = _item_venue(item)
            if venue is None or venue in wanted:
                kept.append(item)
            else:
                deselected.append(item)
        if deselected:
            config.hook.pytest_deselected(items=deselected)
            items[:] = kept

    opted_in = config.getoption("--run-portal") or os.environ.get("PAPERPUSH_RUN_PORTAL") == "1"
    if opted_in:
        return
    skip = pytest.mark.skip(reason="real-portal test: pass --run-portal or set " "PAPERPUSH_RUN_PORTAL=1 to run")
    for item in items:
        if "portal" in item.keywords:
            item.add_marker(skip)


# --- shared test-account credentials ---------------------------------------
#
# The real-portal sign-in uses a shared test account. Its username/password are
# read from the environment rather than committed to the repository, so no live
# credential lives in source control:
#
#   PAPERPUSH_TEST_USERNAME   the test account's email/login
#   PAPERPUSH_TEST_PASSWORD   its password
#
# Set them locally before running portal tests, e.g.
#   export PAPERPUSH_TEST_USERNAME=you@example.com
#   export PAPERPUSH_TEST_PASSWORD=...
# In CI add them as repository secrets (Settings -> Secrets and variables ->
# Actions) and pass them through as `env:` on the job that signs in to the
# portal. A test that needs them takes the `test_username` / `test_password`
# fixtures, which skip the test when the variables are unset.

TEST_USERNAME_ENV = "PAPERPUSH_TEST_USERNAME"
TEST_PASSWORD_ENV = "PAPERPUSH_TEST_PASSWORD"


@pytest.fixture
def update_snapshots(request) -> bool:
    """True when ``--update-snapshots`` (or ``PAPERPUSH_UPDATE_SNAPSHOTS=1``)
    is set, telling the portal-drift tests to rewrite their baselines rather than
    assert against them."""
    return bool(request.config.getoption("--update-snapshots")) or os.environ.get("PAPERPUSH_UPDATE_SNAPSHOTS") == "1"


@pytest.fixture
def test_username() -> str:
    """The shared test account's username, from ``PAPERPUSH_TEST_USERNAME``.

    Skips the requesting test when the variable is unset, so portal sign-in
    tests stay green on a checkout without the secret configured.
    """
    value = os.environ.get(TEST_USERNAME_ENV)
    if not value:
        pytest.skip(f"set {TEST_USERNAME_ENV} to run tests that sign in to the " "real submission portal")
    return value


@pytest.fixture
def test_password() -> str:
    """The shared test account's password, from ``PAPERPUSH_TEST_PASSWORD``.

    Skips the requesting test when the variable is unset (see
    :func:`test_username`).
    """
    value = os.environ.get(TEST_PASSWORD_ENV)
    if not value:
        pytest.skip(f"set {TEST_PASSWORD_ENV} to run tests that sign in to the " "real submission portal")
    return value


# Extensions each kind of file is allowed to take.
DOCUMENT_EXTS = {".pdf", ".docx"}
FIGURE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".pdf", ".svg"}

_RASTER_FORMAT = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".pdf": "PDF",
}


def _norm_ext(ext: str) -> str:
    """Normalize ``png`` / ``.PNG`` -> ``.png``."""
    ext = ext.lower()
    return ext if ext.startswith(".") else "." + ext


# --- PDF -------------------------------------------------------------------


def _build_pdf(*, pages: int = 1, title: str = "Sample", body_lines: list[str] | None = None) -> bytes:
    """Return the bytes of a small but genuinely valid PDF.

    The document has a proper object table and cross-reference table, one or
    more ``/Type /Page`` objects, and is comfortably larger than the 1 KB that
    :mod:`paperpush.validate` treats as suspiciously empty.

    ``body_lines``, when given, are drawn on the first page (each on its own
    line) before the size-padding text; pass them to render specific content
    such as a title page's title, authors, and affiliations.
    """
    n = max(1, pages)
    page_nums = [4 + 2 * i for i in range(n)]
    content_nums = [5 + 2 * i for i in range(n)]

    bodies: dict[int, bytes] = {}
    bodies[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = b" ".join(f"{p} 0 R".encode() for p in page_nums)
    bodies[2] = b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(n).encode() + b" >>"
    bodies[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    for i in range(n):
        pn, cn = page_nums[i], content_nums[i]
        bodies[pn] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] " f"/Resources << /Font << /F1 3 0 R >> >> /Contents {cn} 0 R >>").encode()

        lines = [f"{title} -- page {i + 1} of {n}"]
        if i == 0:
            if body_lines:
                lines += list(body_lines)
            # Pad the first page so the whole file clears the 1 KB sanity floor.
            lines += [f"Lorem ipsum dolor sit amet, line {k:02d}." for k in range(40)]
        ops = [b"BT /F1 16 Tf 72 720 Td 14 TL"]
        for line in lines:
            safe = line.replace("(", "[").replace(")", "]")
            ops.append(f"({safe}) Tj T*".encode())
        ops.append(b"ET")
        stream = b"\n".join(ops)
        bodies[cn] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    max_obj = 3 + 2 * n
    for num in range(1, max_obj + 1):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + bodies[num] + b"\nendobj\n"

    xref_pos = len(out)
    out += f"xref\n0 {max_obj + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, max_obj + 1):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += b"trailer\n"
    out += f"<< /Size {max_obj + 1} /Root 1 0 R >>\n".encode()
    out += b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF\n"
    return bytes(out)


# --- title page ------------------------------------------------------------

# Defaults used when a caller does not supply their own; constant so the
# generated file stays byte-identical across runs.
_DEFAULT_TITLE = "Sample Manuscript"
_DEFAULT_AUTHORS = ("Ada Lovelace", "Alan Turing")
_DEFAULT_AFFILIATIONS = ("Department of Computing, Example University",)


def _title_page_lines(title: str, authors, affiliations) -> list[str]:
    """Compose the text lines of a title page.

    Layout: the title, a blank line, the comma-joined author list, then each
    affiliation on its own numbered line. Empty author/affiliation inputs are
    simply omitted so the result stays well-formed.
    """
    lines = [title, ""]
    authors = [a for a in authors if a]
    if authors:
        lines.append(", ".join(authors))
    for i, aff in enumerate((a for a in affiliations if a), start=1):
        lines.append(f"{i}. {aff}")
    return lines


# --- DOCX ------------------------------------------------------------------

_CONTENT_TYPES = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' '<Default Extension="rels" ContentType="application/vnd.openxmlformats-' 'package.relationships+xml"/>' '<Default Extension="xml" ContentType="application/xml"/>' '<Override PartName="/word/document.xml" ContentType="application/vnd.' 'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>' "</Types>"

_RELS = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/' 'relationships">' '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/' 'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>' "</Relationships>"


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_docx(path: Path, paragraphs: list[str]) -> None:
    """Write a minimal but valid Office Open XML (.docx) file."""
    paras = "".join(f'<w:p><w:r><w:t xml:space="preserve">{_xml_escape(p)}</w:t></w:r></w:p>' for p in paragraphs)
    document = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' '<w:document xmlns:w="http://schemas.openxmlformats.org/' 'wordprocessingml/2006/main"><w:body>' f"{paras}" "<w:sectPr/></w:body></w:document>"
    # Write each member with a fixed timestamp and permission bits (as
    # _build_zip does) so identical inputs yield byte-identical output;
    # plain writestr(name, data) would stamp the current time and make the
    # committed .docx drift on every regeneration. The OOXML member order is
    # preserved ([Content_Types].xml first).
    members = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _RELS,
        "word/document.xml": document,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, data in members.items():
            info = zipfile.ZipInfo(arcname, date_time=_ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16  # -rw-r--r--
            z.writestr(info, data)


# --- figures ---------------------------------------------------------------

# Distinct, easily-distinguishable fill colors keyed by figure number, so a
# glance at the pixels tells you which figure you are looking at.
_FIG_COLORS = [
    (214, 40, 40),  # red
    (33, 102, 204),  # blue
    (38, 153, 71),  # green
    (235, 149, 18),  # orange
    (146, 56, 196),  # purple
    (10, 160, 160),  # teal
]


def _figure_tag(label: str) -> str:
    """``"fig1"`` -> ``"F1"``; fall back to an upper-cased label."""
    digits = "".join(ch for ch in label if ch.isdigit())
    return f"F{digits}" if digits else label.upper()


def _figure_color(label: str) -> tuple[int, int, int]:
    digits = "".join(ch for ch in label if ch.isdigit())
    idx = (int(digits) - 1) if digits else 0
    return _FIG_COLORS[idx % len(_FIG_COLORS)]


def _svg_text(size: tuple[int, int], label: str) -> str:
    w, h = size
    tag = _figure_tag(label)
    r, g, b = _figure_color(label)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" ' f'viewBox="0 0 {w} {h}">\n' f'  <rect width="{w}" height="{h}" fill="rgb({r},{g},{b})"/>\n' f'  <text x="{w / 2}" y="{h / 2}" font-size="{h // 2}" ' 'font-family="sans-serif" font-weight="bold" fill="white" ' f'text-anchor="middle" dominant-baseline="central">' f"{_xml_escape(tag)}</text>\n" "</svg>\n"


def _figure_payload(ext: str, *, size: tuple[int, int], dpi: int, label: str) -> bytes:
    """Return the bytes of a sample figure in ``ext`` format.

    Splitting the rendering out from disk I/O lets a figure be embedded in an
    in-memory archive (the LaTeX source bundle) as well as written to a file.
    """
    if ext == ".svg":
        return _svg_text(size, label).encode("utf-8")

    tag = _figure_tag(label)
    img = Image.new("RGB", size, _figure_color(label))
    draw = ImageDraw.Draw(img)
    font_size = max(min(size) // 2, 12)
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:  # Pillow < 10 has no sized default font
        font = ImageFont.load_default()
    left, top, right, bottom = draw.textbbox((0, 0), tag, font=font)
    pos = ((size[0] - (right - left)) / 2 - left, (size[1] - (bottom - top)) / 2 - top)
    draw.text(pos, tag, fill=(255, 255, 255), font=font)

    fmt = _RASTER_FORMAT[ext]
    kwargs: dict = {}
    if fmt in {"PNG", "TIFF", "JPEG"}:
        kwargs["dpi"] = (dpi, dpi)
    if fmt == "JPEG":
        kwargs["quality"] = 95
    if fmt == "PDF":
        kwargs["resolution"] = float(dpi)
    buf = BytesIO()
    img.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def _build_image(path: Path, ext: str, *, size: tuple[int, int], dpi: int, label: str) -> None:
    path.write_bytes(_figure_payload(ext, size=size, dpi=dpi, label=label))


# --- LaTeX source bundle ---------------------------------------------------
#
# arXiv prefers a LaTeX *source* bundle (a .zip of the .tex, its document class
# and bibliography style, the figures, and the compiled .bbl) over a finished
# PDF. The helpers below assemble such a bundle so a sample submission can point
# at one. As with every other builder here the output is deterministic.

# A fixed timestamp for every archive member, so a regenerated .zip is
# byte-identical (a zip records each member's modification time).
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)

_DEFAULT_ABSTRACT = "A short sample abstract describing an unremarkable but " "entirely valid manuscript, suitable as submission-system test input."


def _build_zip(members: dict[str, bytes]) -> bytes:
    """Return the bytes of a deterministic ZIP archive of ``members``.

    Members are written in sorted name order, each with a fixed timestamp and
    permission bits, so identical inputs always yield byte-identical output.
    """
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname in sorted(members):
            info = zipfile.ZipInfo(arcname, date_time=_ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16  # -rw-r--r--
            zf.writestr(info, members[arcname])
    return buf.getvalue()


def _latex_sources(*, title: str, authors, abstract: str, figures) -> dict[str, bytes]:
    """The member files of a small but realistic LaTeX source bundle.

    The returned mapping is keyed by archive name and is internally consistent:
    ``main.tex`` uses the bundled ``paperpush`` document class and bibliography
    style, cites the entry in ``ref.bib``, includes each figure in ``figures``,
    and carries a pre-compiled ``main.bbl`` so the bundle needs no BibTeX run.

    Figures are embedded as PNGs: unlike Pillow's PDF output (which stamps a
    wall-clock ``/CreationDate``), PNG carries no timestamp, keeping the whole
    archive byte-identical across runs.
    """
    author_tex = " \\and ".join(a for a in authors if a)
    fig_blocks = "".join("\\begin{figure}[t]\n" "  \\centering\n" f"  \\includegraphics[width=0.6\\linewidth]{{figures/{fig}.png}}\n" f"  \\caption{{Sample figure {i}.}}\n" f"  \\label{{fig:{fig}}}\n" "\\end{figure}\n" for i, fig in enumerate(figures, start=1))
    main_tex = "\\documentclass{paperpush}\n" "\\usepackage{graphicx}\n" f"\\title{{{title}}}\n" f"\\author{{{author_tex}}}\n" "\\begin{document}\n" "\\maketitle\n" "\\begin{abstract}\n" f"{abstract}\n" "\\end{abstract}\n" "\\section{Introduction}\n" "This sample manuscript exists to exercise the LaTeX source bundle " "path~\\cite{lovelace1843}.\n" f"{fig_blocks}" "\\bibliographystyle{paperpush}\n" "\\bibliography{ref}\n" "\\end{document}\n"
    ref_bib = "@article{lovelace1843,\n" "  author  = {Ada Lovelace},\n" "  title   = {Notes on the Analytical Engine},\n" "  venue = {Taylor's Scientific Memoirs},\n" "  year    = {1843},\n" "}\n"
    cls = "\\NeedsTeXFormat{LaTeX2e}\n" "\\ProvidesClass{paperpush}[1980/01/01 v1.0 Sample document class]\n" "\\DeclareOption*{\\PassOptionsToClass{\\CurrentOption}{article}}\n" "\\ProcessOptions\\relax\n" "\\LoadClass{article}\n"
    bst = "% paperpush.bst -- minimal BibTeX style stub bundled so the source\n" "% archive carries a .bst file; based on the standard 'plain' style.\n" "ENTRY\n" "  { author title venue year }\n" "  {}\n" "  { label }\n" "FUNCTION {default.type} { }\n" "READ\n" "ITERATE {call.type$}\n"
    bbl = "\\begin{thebibliography}{1}\n" "\\bibitem{lovelace1843}\n" "Ada Lovelace.\n" "\\newblock Notes on the analytical engine.\n" "\\newblock {\\em Taylor's Scientific Memoirs}, 1843.\n" "\\end{thebibliography}\n"

    members: dict[str, bytes] = {
        "main.tex": main_tex.encode("utf-8"),
        "ref.bib": ref_bib.encode("utf-8"),
        "paperpush.cls": cls.encode("utf-8"),
        "paperpush.bst": bst.encode("utf-8"),
        "main.bbl": bbl.encode("utf-8"),
    }
    for fig in figures:
        members[f"figures/{fig}.png"] = _figure_payload(".png", size=(1200, 900), dpi=300, label=fig)
    return members


# --- factory ---------------------------------------------------------------


class SampleFiles:
    """Factory that writes sample submission files into a base directory."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def document(self, name: str, ext: str = ".pdf", *, text: str | None = None, pages: int = 1) -> Path:
        ext = _norm_ext(ext)
        if ext not in DOCUMENT_EXTS:
            raise ValueError(f"unsupported document extension {ext!r}; " f"choose from {sorted(DOCUMENT_EXTS)}")
        path = self.root / f"{name}{ext}"
        if ext == ".pdf":
            path.write_bytes(_build_pdf(pages=pages, title=text or name))
        else:  # .docx
            body = text or f"{name} body text."
            _build_docx(path, [body] + [f"Paragraph {i}." for i in range(pages)])
        return path

    def manuscript(self, ext: str = ".pdf", *, name: str = "manuscript", pages: int = 3) -> Path:
        return self.document(name, ext, text="Sample Manuscript", pages=pages)

    def supplement(self, ext: str = ".pdf", *, name: str = "supplement_text", pages: int = 1) -> Path:
        return self.document(name, ext, text="Supplementary Text", pages=pages)

    def title_page(self, ext: str = ".pdf", *, name: str = "title_page", title: str = _DEFAULT_TITLE, authors=_DEFAULT_AUTHORS, affiliations=_DEFAULT_AFFILIATIONS) -> Path:
        """A standalone title page rendering ``title``, ``authors``, and
        ``affiliations``.

        Some venues (e.g. bioRxiv) require the title page as a separate file
        from the main manuscript; others (e.g. Bioinformatics) fold it into the
        manuscript and need no separate file.
        """
        ext = _norm_ext(ext)
        if ext not in DOCUMENT_EXTS:
            raise ValueError(f"unsupported document extension {ext!r}; " f"choose from {sorted(DOCUMENT_EXTS)}")
        path = self.root / f"{name}{ext}"
        body = _title_page_lines(title, authors, affiliations)
        if ext == ".pdf":
            path.write_bytes(_build_pdf(pages=1, title="Title Page", body_lines=body))
        else:  # .docx
            _build_docx(path, body)
        return path

    def cover_letter(self, ext: str = ".pdf", *, name: str = "cover_letter", title: str = _DEFAULT_TITLE, authors=_DEFAULT_AUTHORS) -> Path:
        """A cover letter addressed to the editor.

        Several venues (e.g. Bioinformatics) require a cover letter as its own
        file. The body is a short, realistic letter that references ``title``
        and is signed by the first of ``authors``; it stays constant so the
        generated file is byte-identical across runs.
        """
        ext = _norm_ext(ext)
        if ext not in DOCUMENT_EXTS:
            raise ValueError(f"unsupported document extension {ext!r}; " f"choose from {sorted(DOCUMENT_EXTS)}")
        path = self.root / f"{name}{ext}"
        signer = next((a for a in authors if a), "The Authors")
        body = [
            "Dear Editor,",
            "",
            f'Please find enclosed our manuscript, "{title}", which we submit ' "for your consideration.",
            "",
            "The work is original, has not been published elsewhere, and is " "not under consideration by another venue. No related preprints " "or manuscripts overlap with this submission.",
            "",
            "The authors declare no conflicts of interest and report no use of " "generative AI or large language model tools in this work.",
            "",
            "Sincerely,",
            signer,
        ]
        if ext == ".pdf":
            path.write_bytes(_build_pdf(pages=1, title="Cover Letter", body_lines=body))
        else:  # .docx
            _build_docx(path, body)
        return path

    def figure(self, name: str = "fig1", ext: str = ".png", *, size: tuple[int, int] = (1200, 900), dpi: int = 300) -> Path:
        ext = _norm_ext(ext)
        if ext not in FIGURE_EXTS:
            raise ValueError(f"unsupported figure extension {ext!r}; " f"choose from {sorted(FIGURE_EXTS)}")
        path = self.root / f"{name}{ext}"
        _build_image(path, ext, size=size, dpi=dpi, label=name)
        return path

    def latex_bundle(self, name: str = "manuscript_tex_bundle", ext: str = ".zip", *, title: str = _DEFAULT_TITLE, authors=_DEFAULT_AUTHORS, abstract: str = _DEFAULT_ABSTRACT, figures=("fig1", "fig2")) -> Path:
        """Write a LaTeX source bundle as a single ``.zip`` and return its path.

        The archive holds a self-consistent set of LaTeX sources -- ``main.tex``,
        the ``paperpush`` document class (``.cls``) and bibliography style
        (``.bst``), ``ref.bib``, the compiled ``main.bbl``, and a ``figures/``
        directory with one PDF per entry in ``figures``. arXiv accepts such a
        bundle in place of a finished PDF.
        """
        ext = _norm_ext(ext)
        if ext != ".zip":
            raise ValueError(f"unsupported bundle extension {ext!r}; only '.zip' is supported")
        members = _latex_sources(title=title, authors=authors, abstract=abstract, figures=figures)
        path = self.root / f"{name}{ext}"
        path.write_bytes(_build_zip(members))
        return path


# --- fixtures --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_user_state(tmp_path, monkeypatch):
    """Keep tests off the real keyring and user config directory.

    Credential storage falls back to a JSON file under ``XDG_CONFIG_HOME``;
    pointing that at ``tmp_path`` and disabling the keyring means ``login``
    tests never touch the developer's machine keychain or config.
    """
    monkeypatch.setenv("PAPERPUSH_KEYRING", "0")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.fixture
def samples(tmp_path) -> SampleFiles:
    """A :class:`SampleFiles` factory bound to the test's ``tmp_path``."""
    return SampleFiles(tmp_path)


@pytest.fixture
def manuscript_pdf(samples) -> Path:
    return samples.manuscript(".pdf")


@pytest.fixture
def manuscript_docx(samples) -> Path:
    return samples.manuscript(".docx")


@pytest.fixture
def supplement_pdf(samples) -> Path:
    return samples.supplement(".pdf")


@pytest.fixture
def title_page_pdf(samples) -> Path:
    return samples.title_page(".pdf")


@pytest.fixture
def cover_letter_pdf(samples) -> Path:
    return samples.cover_letter(".pdf")


@pytest.fixture
def supplement_docx(samples) -> Path:
    return samples.supplement(".docx")


@pytest.fixture
def fig1_png(samples) -> Path:
    return samples.figure("fig1", ".png")


@pytest.fixture
def fig2_png(samples) -> Path:
    return samples.figure("fig2", ".png")
