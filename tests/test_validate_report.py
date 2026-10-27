"""Tests for the full validation report and the checks added with it.

Covers :func:`paperpush.validate.validate_report` (one status per check), the
report renderers (terminal, PDF, HTML, Markdown, text; the colour-blind
palette), the CLI's ``-o`` / ``--alt-color`` / ``--ai*`` flags, the AI task
plumbing (:mod:`paperpush.ai_validate`), and the deterministic checks for
layout, figures, citation order, and .sub-versus-manuscript consistency.
"""

from __future__ import annotations

import importlib
import json
import zipfile
from pathlib import Path

import pytest

from paperpush import ai_validate, citation_check, consistency, figure_check, layout_check, report
from paperpush import requirements as r
from paperpush.cli import main
from paperpush.database import Field, Venue
from paperpush.subfile import SubFile
from paperpush.validate import WARNING, Checks, Issue, validate, validate_report
from tests.conftest import _build_docx, _build_pdf
from tests.pdf_builder import text_line, write_pdf

validate_mod = importlib.import_module("paperpush.validate")

_ALIASES = {"sections": {"Introduction": [], "Results": [], "Methods": []}, "statements": {"competing_interests": ["Competing interests"]}}

MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
FIGURES = Field(id="figure_files", label="Figures", type="filelist")
TITLE = Field(id="title", label="Title", type="text", required=True)
ABSTRACT = Field(id="abstract", label="Abstract", type="textarea")
AUTHORS = Field(id="authors", label="Authors", type="authorlist")
KEYWORDS = Field(id="keywords", label="Keywords", type="textarea")
COMPETING = Field(id="competing_interests", label="Competing interests", type="textarea")


@pytest.fixture(autouse=True)
def _clear_model_cache():
    validate_mod._MODELS.clear()
    yield
    validate_mod._MODELS.clear()


@pytest.fixture
def _use(monkeypatch):
    """Inject an in-memory requirements database for the duration of a test."""

    def install(db: dict) -> None:
        monkeypatch.setattr(r, "_load_raw", lambda: {"$aliases": _ALIASES, **db})

    yield install


def _venue(*fields: Field, slug: str = "testj") -> Venue:
    return Venue(slug=slug, name="Test Venue", fields=list(fields))


def _report(venue: Venue, values: dict[str, str], **kwargs) -> report.ValidationReport:
    flags = {"check_links": False, "check_references": False, "check_sensitive": False, "check_openreview": False}
    flags.update(kwargs)
    return validate_report(SubFile(venue=venue.slug, values=values), venue, **flags)


def _result(rep: report.ValidationReport, check_id: str) -> report.CheckResult:
    return next(res for res in rep.results if res.id == check_id)


def _pdfminer():
    pytest.importorskip("pdfminer")


# ===========================================================================
# statuses
# ===========================================================================


def test_every_check_gets_a_status(_use):
    _use({})
    rep = _report(_venue(TITLE), {})
    assert _result(rep, "form.required").status == report.FAILED
    assert _result(rep, "integrity.links").status == report.UNCHECKED
    assert "--dont-check-links" in _result(rep, "integrity.links").detail
    assert _result(rep, "form.urls").status == report.NOT_APPLICABLE
    assert _result(rep, "form.schema").status == report.PASSED
    assert not rep.ok


def test_validate_is_the_reports_flat_issue_list(_use):
    _use({})
    venue = _venue(TITLE, ABSTRACT)
    sub = SubFile(venue="testj", values={"abstract": "x"})
    issues = validate(sub, venue, check_links=False, check_references=False)
    assert [i.message for i in issues] == [i.message for i in validate_report(sub, venue, check_links=False, check_references=False).issues()]
    assert all(i.check for i in issues)


def test_a_check_that_crashes_is_recorded_as_unchecked():
    checks = Checks()

    def broken():
        raise RuntimeError("boom")

    checks.run("x.broken", "Broken", "Test", broken)
    assert checks.results[0].status == report.UNCHECKED
    assert "boom" in checks.results[0].detail


def test_unchecked_issues_make_an_unchecked_status():
    checks = Checks()
    checks.run("x.y", "Y", "Test", lambda: [Issue(WARNING, "", "could not read", unchecked=True)])
    checks.run("x.z", "Z", "Test", lambda: [Issue(WARNING, "", "could not read", unchecked=True), Issue(WARNING, "", "a real problem")])
    assert [res.status for res in checks.results] == [report.UNCHECKED, report.WARNING]


def test_unhandled_guideline_rules_are_reported_not_implemented(_use, monkeypatch, tmp_path):
    from paperpush import requirements_check

    _use({"testj": {"figures": {"color_charge": True, "min_dpi": 300}}})
    monkeypatch.setattr(requirements_check, "INFORMATIONAL_RULES", frozenset())
    rep = _report(_venue(MANUSCRIPT), {})
    ids = {res.id for res in rep.by_status(report.NOT_IMPLEMENTED)}
    assert ids == {"not_implemented.figures.color_charge"}


# ===========================================================================
# rendering
# ===========================================================================


def _sample_report() -> report.ValidationReport:
    return report.ValidationReport(
        venue="testj",
        subfile="paper.sub",
        results=[
            report.CheckResult("a", "Passes", "Cat", report.PASSED, detail="300 dpi"),
            report.CheckResult("b", "Breaks", "Cat", report.FAILED, issues=[Issue("error", "title", "title is required")]),
            report.CheckResult("c", "Advisory", "Cat", report.WARNING, issues=[Issue(WARNING, "", "looks single-spaced")]),
            report.CheckResult("d", "Judgement", "Cat", report.NEEDS_AI, detail="run with --ai"),
            report.CheckResult("e", "Rule", "Cat", report.NOT_IMPLEMENTED, detail="check by hand"),
            report.CheckResult("f", "Off", "Cat", report.UNCHECKED, detail="switched off"),
            report.CheckResult("g", "Irrelevant", "Cat", report.NOT_APPLICABLE),
        ],
    )


def test_text_and_markdown_list_every_section():
    rep = _sample_report()
    text = report.to_text(rep)
    for marker in ("[PASS] [Cat] Passes", "[FAIL] [Cat] Breaks", "[WARN]", "[AI] [Cat] Judgement", "[TODO] [Cat] Rule", "[SKIP] [Cat] Off", "error: [title] title is required"):
        assert marker in text
    assert text.index("FAILED") < text.index("PASSED")
    md = report.to_markdown(rep)
    assert "## ✖ Failed (1)" in md and "## ✔ Passed (1)" in md


def test_html_uses_the_status_colours_and_the_alt_palette():
    rep = _sample_report()
    html = report.to_html(rep)
    assert report.DEFAULT_PALETTE.passed in html and report.DEFAULT_PALETTE.failed in html and report.DEFAULT_PALETTE.unchecked in html
    alt = report.to_html(rep, alt_color=True)
    assert report.ALT_PALETTE.passed in alt and report.DEFAULT_PALETTE.passed not in alt


def test_pdf_report_is_a_readable_pdf_with_colours(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    out = report.save(_sample_report(), tmp_path / "r.pdf")
    reader = pypdf.PdfReader(str(out))
    text = "".join(page.extract_text() for page in reader.pages)
    assert "[FAIL]" in text and "[PASS]" in text and "title is required" in text
    content = reader.pages[0].get_contents().get_data().decode("latin-1")
    r_, g_, b_ = (int(report.DEFAULT_PALETTE.failed[i : i + 2], 16) / 255 for i in (1, 3, 5))
    assert f"{r_:.3f} {g_:.3f} {b_:.3f} rg" in content


def test_pdf_report_wraps_long_lines_onto_more_pages(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    rep = report.ValidationReport(venue="v", results=[report.CheckResult(str(i), "x" * 30, "C", report.WARNING, issues=[Issue(WARNING, "", "word " * 120)]) for i in range(30)])
    reader = pypdf.PdfReader(str(report.save(rep, tmp_path / "long.pdf")))
    assert len(reader.pages) > 3


def test_output_path_defaults_to_pdf_next_to_the_sub(tmp_path):
    sub = str(tmp_path / "nature.sub")
    assert report.output_path("", sub) == tmp_path / "nature_validation.pdf"
    assert report.output_path(str(tmp_path / "out"), sub) == tmp_path / "out.pdf"
    assert report.output_path(str(tmp_path / "out.md"), sub) == tmp_path / "out.md"
    assert report.output_path(str(tmp_path), sub) == tmp_path / "nature_validation.pdf"


def test_terminal_report_is_coloured_by_status():
    from rich.console import Console

    console = Console(record=True, force_terminal=True, color_system="truecolor", width=120)
    report.print_report(_sample_report(), console=console)
    html = console.export_html()
    assert report.DEFAULT_PALETTE.passed in html and report.DEFAULT_PALETTE.failed in html
    console = Console(record=True, force_terminal=True, color_system="truecolor", width=120)
    report.print_report(_sample_report(), console=console, alt_color=True)
    assert report.ALT_PALETTE.passed in console.export_html()


# ===========================================================================
# CLI
# ===========================================================================


def _cli_venue(monkeypatch, *fields: Field):
    import paperpush.cli as cli

    venue = _venue(*fields)
    monkeypatch.setattr(cli, "get_venue", lambda slug: venue)
    return venue


def _sub(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "paper.sub"
    path.write_text("@venue: testj\n" + body, encoding="utf-8")
    return path


def test_cli_saves_the_report_in_the_requested_format(_use, tmp_path, monkeypatch, capsys):
    _use({})
    _cli_venue(monkeypatch, TITLE)
    sub = _sub(tmp_path, "title: A study\n")
    assert main(["validate", str(sub), "--dont-check-links", "--dont-check-references", "-o"]) == 0
    assert (tmp_path / "paper_validation.pdf").read_bytes().startswith(b"%PDF-")
    assert main(["validate", str(sub), "--dont-check-links", "--dont-check-references", "-o", str(tmp_path / "r.html"), "--alt-color"]) == 0
    assert report.ALT_PALETTE.passed in (tmp_path / "r.html").read_text()
    out = capsys.readouterr().out
    assert "PASSED" in out and "Saved the report to" in out


def test_cli_exit_code_follows_errors(_use, tmp_path, monkeypatch, capsys):
    _use({})
    _cli_venue(monkeypatch, TITLE)
    assert main(["validate", str(_sub(tmp_path, "")), "--dont-check-links", "--dont-check-references"]) == 1
    assert "Title is required but empty" in capsys.readouterr().out


def _manuscript_with_notes(_use, tmp_path):
    _use({"testj": {"notes": ["Number every page."]}})
    pdf = tmp_path / "ms.pdf"
    pdf.write_bytes(_build_pdf(pages=1, body_lines=["A study"]))
    return pdf


def test_cli_writes_ai_tasks_and_merges_agent_results(_use, tmp_path, monkeypatch, capsys):
    pdf = _manuscript_with_notes(_use, tmp_path)
    _cli_venue(monkeypatch, TITLE, MANUSCRIPT)
    sub = _sub(tmp_path, f"title: A study\nmanuscript_file: {pdf}\n")
    tasks = tmp_path / "tasks.json"
    assert main(["validate", str(sub), "--dont-check-links", "--dont-check-references", "--ai-tasks", str(tasks)]) == 0
    payload = json.loads(tasks.read_text())
    by_id = {t["id"]: t for t in payload["tasks"]}
    assert "Number every page." in by_id["ai.notes.general"]["instructions"]
    assert by_id["ai.notes.general"]["files"] == [str(pdf)]
    assert "NEEDS AI" in capsys.readouterr().out

    results = tmp_path / "results.json"
    results.write_text(json.dumps({"results": [{"id": "ai.notes.general", "status": "fail", "details": "pages are not numbered", "evidence": "p.1"}]}))
    assert main(["validate", str(sub), "--dont-check-links", "--dont-check-references", "--ai-results", str(results)]) == 0
    out = capsys.readouterr().out
    assert "General rules in the author guidelines (AI)" in out and "pages are not numbered" in out


def test_cli_ai_without_a_key_says_so_and_still_reports(_use, tmp_path, monkeypatch, capsys):
    pdf = _manuscript_with_notes(_use, tmp_path)
    _cli_venue(monkeypatch, TITLE, MANUSCRIPT)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    sub = _sub(tmp_path, f"title: A study\nmanuscript_file: {pdf}\n")
    assert main(["validate", str(sub), "--dont-check-links", "--dont-check-references", "--ai"]) == 0
    out = capsys.readouterr().out
    assert "AI checks could not run: no API key found" in out


# ===========================================================================
# AI tasks
# ===========================================================================


def test_parse_results_accepts_the_documented_shapes():
    assert ai_validate.parse_results({"results": [{"id": "a", "status": "PASS"}]})["a"]["status"] == "pass"
    assert ai_validate.parse_results([{"id": "a", "status": "failed"}])["a"]["status"] == "fail"
    assert ai_validate.parse_results({"a": "n/a"})["a"]["status"] == "not_applicable"
    with pytest.raises(ValueError):
        ai_validate.parse_results({"results": [{"id": "a", "status": "maybe"}]})


def test_api_verdicts_become_check_results(_use, tmp_path, monkeypatch):
    autofill = importlib.import_module("paperpush.autofill")
    pdf = _manuscript_with_notes(_use, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    seen = {}

    def fake(model, documents, instructions, schema, max_tokens, system=""):
        seen.update(model=model, files=[d.path for d in documents], instructions=instructions, system=system)
        ids = schema["properties"]["results"]["items"]["properties"]["id"]["enum"]
        return json.dumps({"results": [{"id": i, "status": "fail" if i == "ai.notes.general" else "pass", "details": f"{i} judged", "evidence": ""} for i in ids]})

    monkeypatch.setitem(autofill._CALLERS, "anthropic", fake)
    rep = _report(_venue(TITLE, MANUSCRIPT), {"title": "A study", "manuscript_file": str(pdf)}, ai=ai_validate.AiOptions.api(provider="anthropic"))
    notes = _result(rep, "ai.notes.general")
    assert notes.status == report.WARNING and notes.source == "ai"
    assert rep.ai == f"anthropic {autofill.DEFAULT_MODELS['anthropic']}"
    assert seen["files"] == [pdf] and "Number every page." in seen["instructions"] and "pre-submission checker" in seen["system"]
    assert not rep.by_status(report.NEEDS_AI)


def test_raster_figures_queue_an_ai_task_with_a_size_threshold(_use, tmp_path, samples):
    _use({"testj": {"figures": {"min_font_size_pt": 6, "max_width_mm": 180}}})
    fig = samples.figure("fig1", ".png", size=(1800, 900), dpi=254)
    rep = _report(_venue(FIGURES), {"figure_files": str(fig)})
    task = next(t for t in rep.ai_tasks if t.id == "ai.figures.raster_lettering")
    assert task.files == [str(fig)]
    assert "% of the image width tall" in task.instructions
    assert _result(rep, "ai.figures.raster_lettering").status == report.NEEDS_AI


def test_mcp_validate_returns_checks_and_ai_tasks(_use, tmp_path, monkeypatch):
    mcp_server = importlib.import_module("paperpush.mcp_server")
    pdf = _manuscript_with_notes(_use, tmp_path)
    monkeypatch.setattr(mcp_server, "_venue_or_error", lambda slug: _venue(TITLE, MANUSCRIPT))
    sub = _sub(tmp_path, f"title: A study\nmanuscript_file: {pdf}\n")
    out = mcp_server.validate_subfile(str(sub), check_links=False, check_references=False, output=str(tmp_path / "r.md"))
    assert {c["status"] for c in out["checks"]} >= {"passed", "needs_ai"}
    assert any(t["id"] == "ai.notes.general" for t in out["ai_tasks"])
    assert Path(out["report"]).read_text().startswith("# paperpush validation report")
    merged = mcp_server.validate_subfile(str(sub), check_links=False, check_references=False, ai_results=[{"id": "ai.notes.general", "status": "pass", "details": "ok"}])
    assert next(c for c in merged["checks"] if c["id"] == "ai.notes.general")["status"] == "passed"
    assert mcp_server.validation_ai_tasks(str(sub))["tasks"]


# ===========================================================================
# citation order and legends
# ===========================================================================

_TEXT = """Introduction
We show the overview (Fig. 2) before the details (Figs. 1a-c), with Table 1.
Supplementary Fig. S2 then Supplementary Fig. S1 and Extended Data Fig. 1.
References
1. A. Author. Fig. 9 of a book. 2020.
Figure 1. Details of the thing.
Figure 2 | The overview of everything we did in this study.
Figure 3. Never cited.
"""


def test_mentions_tell_citations_from_captions():
    found = citation_check.mentions(_TEXT)
    captions = {(m.series, m.number) for m in found if m.caption}
    assert captions == {("figure", 1), ("figure", 2), ("figure", 3)}
    cited = [(m.series, m.number) for m in found if not m.caption]
    assert cited[:4] == [("figure", 2), ("figure", 1), ("table", 1), ("supp_figure", 2)]
    assert ("figure", 9) not in cited  # inside the reference list


def test_order_problems_report_out_of_order_uncited_and_missing():
    found = citation_check.mentions(_TEXT + "\nAs Fig. 4 shows.")
    problems, _ = citation_check.order_problems(found, "figure")
    assert any("Figure 2 is cited before Figure 1" in p for p in problems)
    assert any("Figure 3 is never cited" in p for p in problems)
    assert any("cites Figure 4, but no such figure" in p for p in problems)
    problems, _ = citation_check.order_problems(found, "supp_figure")
    assert problems and "Supplementary Figure 2 is cited before Supplementary Figure 1" in problems[0]
    problems, detail = citation_check.order_problems(found, "table")
    assert not problems and "1 cited in order" in detail


def test_wrapped_citation_at_a_line_start_is_not_a_caption():
    text = "we measured the response to the stimulus in all of the cells that are shown in\nFig. 1. Then more text.\n"
    assert not any(m.caption for m in citation_check.mentions(text))


def test_tex_labels_and_refs_give_the_citation_order():
    source = r"""
    See Fig.~\ref{fig:b} and then Fig.~\ref{fig:a}.
    \begin{figure}\caption{A}\label{fig:a}\end{figure}
    \begin{figure}\caption{B}\label{fig:b}\end{figure}
    \renewcommand{\thefigure}{S\arabic{figure}}
    \begin{figure}\caption{S}\label{fig:s}\end{figure}
    """
    found = citation_check.tex_mentions(source)
    problems, _ = citation_check.order_problems(found, "figure")
    assert "Figure 2 is cited before Figure 1" in problems[0]
    problems, _ = citation_check.order_problems(found, "supp_figure")
    assert any("never cited" in p for p in problems)


def test_legend_length_and_location(_use, tmp_path):
    _use({"testj": {"figures": {"legend_max_words": 5, "legends_location": "after_references"}}})
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["Intro", "As Fig. 1 shows.", "Figure 1. A legend that has rather more than five words in it.", "References", "1. A ref."])
    rep = _report(_venue(MANUSCRIPT), {"manuscript_file": str(doc)})
    messages = [i.message for i in _result(rep, "figures.legends").issues]
    assert any("legend of Figure 1 is about" in m for m in messages)
    assert any("comes before the references" in m for m in messages)


# ===========================================================================
# figures
# ===========================================================================


def test_cited_panels_expand_ranges_and_lists():
    panels = figure_check.cited_panels("See Fig. 2a-c and Figure 3B, D and Supplementary Fig. 4a.")
    assert panels == {2: {"a", "b", "c"}, 3: {"B", "D"}}


def test_dpi_uses_the_line_art_threshold_for_flat_images(_use, tmp_path):
    from PIL import Image

    _use({"testj": {"figures": {"min_dpi": 300, "min_dpi_line_art": 1000}}})
    flat = tmp_path / "flat.png"
    Image.new("RGB", (1200, 900), "white").save(flat, dpi=(600, 600))
    rep = _report(_venue(FIGURES), {"figure_files": str(flat)})
    message = _result(rep, "figures.dpi").issues[0].message
    assert "600 dpi" in message and "1000 dpi for line art" in message


def test_svg_lettering_size_is_scaled_to_the_print_width(_use, tmp_path):
    _use({"testj": {"figures": {"min_font_size_pt": 6, "max_width_mm": 90}}})
    svg = tmp_path / "fig1.svg"
    # 180 mm wide, printed at 90 mm: the 10 px (7.5 pt) label prints at 3.75 pt.
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="180mm" height="90mm" viewBox="0 0 680 340"><g font-family="Arial"><text font-size="10" x="1" y="10">tiny</text><text style="font-size:40px" x="1" y="50">big</text></g></svg>')
    rep = _report(_venue(FIGURES), {"figure_files": str(svg)})
    issue = _result(rep, "figures.text_size").issues[0].message
    assert "'tiny'" in issue and "'big'" not in issue


def _figure_pdf(path: Path, labels=("a", "b"), small_size: float = 4, label_font_bold: bool = False) -> Path:
    ops = "".join(text_line(10 + 200 * i, 180, label, size=9) for i, label in enumerate(labels))
    ops += text_line(20, 20, "axis label", size=small_size) + text_line(220, 20, "another label", size=8)
    return write_pdf(path, [ops], width=400, height=200)


def test_vector_figure_lettering_panels_and_font(_use, tmp_path):
    _pdfminer()
    _use({"testj": {"figures": {"min_font_size_pt": 5, "font": "Arial or Helvetica", "multipanel_labels": "uppercase bold (A, B, C)"}}})
    fig = _figure_pdf(tmp_path / "fig2.pdf")
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["See Fig. 2a, b and Fig. 2c."])
    rep = _report(_venue(MANUSCRIPT, FIGURES), {"manuscript_file": str(doc), "figure_files": f"{fig} | Figure 2"})
    size = " ".join(i.message for i in _result(rep, "figures.text_size").issues)
    assert "'axis label' 4.0 pt" in size and "another" not in size
    assert _result(rep, "figures.font").status == report.PASSED
    lettering = " ".join(i.message for i in _result(rep, "figures.lettering").issues)
    assert "upper" in lettering  # cited and drawn in lower case
    assert "not bold" in lettering
    assert "panel(s) c of Figure 2" in lettering


def test_figures_are_found_in_the_manuscript_when_none_are_attached(_use, tmp_path):
    _pdfminer()
    _use({"testj": {"figures": {"min_font_size_pt": 5}}})
    # A Form XObject drawing with lettering, placed on a manuscript page.
    form = "BT /F1 3 Tf 10 10 Td (tiny tick) Tj ET BT /F1 9 Tf 10 60 Td (big label) Tj ET"
    body = "".join(text_line(72, 700 - 14 * i, f"Body text line number {i} of the manuscript.", size=10) for i in range(20))
    pdf = tmp_path / "ms.pdf"
    _write_pdf_with_form(pdf, body + "q 1 0 0 1 100 200 cm /Fm1 Do Q\n", form)
    rep = _report(_venue(MANUSCRIPT), {"manuscript_file": str(pdf)})
    issue = _result(rep, "figures.text_size").issues[0]
    assert "page 1, drawing 1" in issue.message and "'tiny tick' 3.0 pt" in issue.message


def _write_pdf_with_form(path: Path, page_ops: str, form_ops: str) -> None:
    form = form_ops.encode("latin-1")
    page = page_ops.encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> /XObject << /Fm1 6 0 R >> >> /Contents 5 0 R >>",
        f"<< /Length {len(page)} >>\nstream\n".encode() + page + b"\nendstream",
        f"<< /Type /XObject /Subtype /Form /BBox [0 0 200 100] /Resources << /Font << /F1 3 0 R >> >> /Length {len(form)} >>\nstream\n".encode() + form + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


# ===========================================================================
# layout
# ===========================================================================


def test_font_rules_are_read_from_their_wording():
    rule = layout_check.parse_font_rule("Arial or Helvetica (Courier permitted for monospaced text)")
    assert rule.allows("Helvetica", "sans-serif") and rule.allows("Arial", "sans-serif")
    assert not rule.allows("Times", "serif")
    loose = layout_check.parse_font_rule("Helvetica or a similar typeface")
    assert loose.allows("Calibri", "sans-serif")
    strict = layout_check.parse_font_rule("Times and Symbol fonts only")
    assert strict.only and strict.allows("Times New Roman", "serif")
    assert layout_check.font_family("ABCDEF+TimesNewRomanPSMT") == ("Times New Roman", "serif")
    assert layout_check.font_family("NimbusSanL-Regu")[0] == "Helvetica"


def test_spelling_variants_are_counted():
    us, uk, ize, ise = layout_check.spelling_counts("The colour of the centre was analysed; colors were normalized and organised in the laboratory literature.")
    assert set(us) == {"colors"} and set(uk) == {"colour", "centre", "analysed"}
    assert set(ize) == {"normalized"} and set(ise) == {"organised"}


def test_latex_source_layout_is_read_from_the_preamble():
    tex = layout_check.measure_tex(r"\documentclass[12pt,a4paper]{article}\usepackage[margin=2cm]{geometry}\usepackage{times,lineno}\usepackage{setspace}\doublespacing\linenumbers\begin{document}x\end{document}")
    assert tex.font_size == 12 and tex.font == "Times" and tex.paper == "a4paper"
    assert tex.double_spacing and tex.line_numbers and tex.page_numbers
    assert round(tex.margins["left"] / layout_check.PT_PER_MM) == 20


def _styled_docx(path: Path, *, font="Times New Roman", half_points=24, line=480, line_numbers=True, page_field=True, margin_twips=1440) -> Path:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    sect = f'<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="{margin_twips}" w:right="{margin_twips}" w:bottom="{margin_twips}" w:left="{margin_twips}"/>' + ('<w:lnNumType w:countBy="1" w:restart="continuous"/>' if line_numbers else "") + "</w:sectPr>"
    document = f'<?xml version="1.0"?><w:document {w}><w:body><w:p><w:r><w:t>Body text of the manuscript.</w:t></w:r></w:p>{sect}</w:body></w:document>'
    styles = f'<?xml version="1.0"?><w:styles {w}><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="{font}"/><w:sz w:val="{half_points}"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:line="{line}" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults></w:styles>'
    footer = f'<?xml version="1.0"?><w:ftr {w}><w:p><w:fldSimple w:instr=" PAGE "/></w:p></w:ftr>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
        if page_field:
            z.writestr("word/footer1.xml", footer)
    return path


_LAYOUT_RULES = {"testj": {"manuscript": {"font": "Times New Roman", "font_size_pt": 12, "double_spacing": True, "line_numbers": True, "page_numbers": True, "margins_mm": 25.4, "page_size": "US Letter"}}}
_LAYOUT_CHECKS = ("layout.font", "layout.font_size", "layout.line_spacing", "layout.line_numbers", "layout.page_numbers", "layout.margins", "layout.page_size")


def test_word_layout_that_follows_the_rules_passes(_use, tmp_path):
    _use(_LAYOUT_RULES)
    rep = _report(_venue(MANUSCRIPT), {"manuscript_file": str(_styled_docx(tmp_path / "ok.docx"))})
    assert {cid: _result(rep, cid).status for cid in _LAYOUT_CHECKS} == {cid: report.PASSED for cid in _LAYOUT_CHECKS}


def test_word_layout_that_breaks_the_rules_warns(_use, tmp_path):
    _use(_LAYOUT_RULES)
    docx = _styled_docx(tmp_path / "bad.docx", font="Calibri", half_points=22, line=240, line_numbers=False, page_field=False, margin_twips=720)
    rep = _report(_venue(MANUSCRIPT), {"manuscript_file": str(docx)})
    assert {cid: _result(rep, cid).status for cid in _LAYOUT_CHECKS} == {**{cid: report.WARNING for cid in _LAYOUT_CHECKS}, "layout.page_size": report.PASSED}


def _layout_pdf(path: Path, *, spacing: float, line_numbers: bool, page_numbers: bool, left: float = 72) -> Path:
    pages = []
    for page in range(2):
        ops = ""
        for i in range(24):
            y = 708 - spacing * i
            ops += text_line(left, y, f"The body text of the manuscript continues on this line {i} with more words.", size=12)
            if line_numbers:
                ops += text_line(left - 30, y, str(page * 24 + i + 1), size=8)
        if page_numbers:
            ops += text_line(300, 30, str(page + 1), size=10)
        pages.append(ops)
    return write_pdf(path, pages)


def test_pdf_layout_measures_spacing_and_numbering(_use, tmp_path):
    _pdfminer()
    _use(_LAYOUT_RULES)
    good = _layout_pdf(tmp_path / "good.pdf", spacing=24, line_numbers=True, page_numbers=True)
    rep = _report(_venue(MANUSCRIPT), {"manuscript_file": str(good)})
    for cid in ("layout.line_spacing", "layout.line_numbers", "layout.page_numbers", "layout.font_size", "layout.margins", "layout.page_size"):
        assert _result(rep, cid).status == report.PASSED, (cid, _result(rep, cid))
    assert _result(rep, "layout.font").status == report.WARNING  # Helvetica, not Times New Roman
    bad = _layout_pdf(tmp_path / "bad.pdf", spacing=14, line_numbers=False, page_numbers=False, left=36)
    rep = _report(_venue(MANUSCRIPT), {"manuscript_file": str(bad)})
    for cid in ("layout.line_spacing", "layout.line_numbers", "layout.page_numbers", "layout.margins"):
        assert _result(rep, cid).status == report.WARNING, cid


# ===========================================================================
# consistency between the .sub and the manuscript
# ===========================================================================

_MS = [
    "A Study of Analytical Engines",
    "Ada Lovelace, Charles Babbage",
    "ada@example.edu",
    "Abstract",
    "We study engines that compute. They work well on many problems and we show how in detail.",
    "Keywords: engines; computing; notes",
    "Introduction",
    "Text.",
    "Competing interests",
    "The authors declare no competing interests of any kind.",
]


def _consistency(tmp_path: Path, values: dict[str, str]) -> Checks:
    doc = tmp_path / "ms.docx"
    _build_docx(doc, _MS)
    checks = Checks()
    venue = _venue(TITLE, ABSTRACT, AUTHORS, KEYWORDS, COMPETING, MANUSCRIPT)
    consistency.run_checks(checks, venue, {"manuscript_file": str(doc), **values})
    return checks


def _status(checks: Checks, check_id: str) -> tuple[str, str]:
    res = next(x for x in checks.results if x.id == check_id)
    return res.status, " ".join(i.message for i in res.issues)


def test_matching_sub_values_pass(tmp_path):
    checks = _consistency(
        tmp_path,
        {
            "title": "A study of analytical engines",
            "abstract": "We study engines that compute.  They work well on many problems, and we show how in detail.",
            "authors": "Ada Lovelace | ada@example.edu | | | yes\nCharles Babbage | | | | no",
            "keywords": "engines, computing, notes",
            "competing_interests": "The authors declare no competing interests of any kind.",
        },
    )
    for cid in ("consistency.title", "consistency.abstract", "consistency.authors", "consistency.keywords", "consistency.statements"):
        assert _status(checks, cid)[0] == report.PASSED, (cid, _status(checks, cid))


def test_drifted_sub_values_are_reported_with_the_differences(tmp_path):
    checks = _consistency(
        tmp_path,
        {
            "title": "A study of analytical machines",
            "abstract": "We study engines that compute. They work well on several problems and we show how in detail.",
            "authors": "Charles Babbage | | | | no\nAda Lovelace | ada@other.org | | | yes\nGrace Hopper | | | | no",
            "keywords": "engines, computing, history",
            "competing_interests": "Something else entirely is declared in this form about the work.",
        },
    )
    assert "'machines' in the .sub vs 'engines' in the manuscript" in _status(checks, "consistency.title")[1]
    assert "'several' in the .sub vs 'many'" in _status(checks, "consistency.abstract")[1]
    authors = _status(checks, "consistency.authors")[1]
    assert "Grace Hopper" in authors and "lists Charles Babbage before Ada Lovelace" in authors and "ada@other.org" in authors
    assert "only in the .sub: history" in _status(checks, "consistency.keywords")[1]
    assert "was not found" in _status(checks, "consistency.statements")[1]


def test_locate_finds_a_passage_whose_first_words_changed():
    hay = "intro words then the quick brown fox jumps over the lazy dog and runs away fast".split()
    ratio, start, end = consistency.locate("a quick brown fox jumps over the lazy dog and runs away fast".split(), hay)
    assert start is not None and ratio > 0.9


# ===========================================================================
# guideline checks added with the report
# ===========================================================================


def test_numbered_references_must_be_cited_in_order(_use, tmp_path):
    _use({"testj": {"references": {"numbered": True, "style": "Vancouver"}}})
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["Text cites [2] first, then [1], [3] and [4-5].", "References", "1. A. Ref one.", "2. B. Ref two."])
    rep = _report(_venue(MANUSCRIPT), {"manuscript_file": str(doc)})
    assert "reference [2] is cited before reference [1]" in _result(rep, "guidelines.reference_style").issues[0].message


def test_reference_list_rules(_use, tmp_path):
    _use({"testj": {"references": {"doi_required": True, "preprints_allowed": False, "unpublished_allowed": False}}})
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["Body.", "References", "1. A. Author. A paper in a journal. 2020. doi:10.1/x", "2. B. Author. A preprint on bioRxiv about things. 2021.", "3. C. Author. Results in preparation for a later paper."])
    messages = " ".join(i.message for i in _result(_report(_venue(MANUSCRIPT), {"manuscript_file": str(doc)}), "guidelines.reference_list").issues)
    assert "2 of 3 references" in messages and "cite preprints" in messages and "unpublished work" in messages


def test_section_order_and_abstract_abbreviations(_use, tmp_path):
    _use({"testj": {"sections": {"order": ["Introduction", "Results", "Methods"]}, "abstract": {"no_abbreviations": True}}})
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["Introduction", "x", "Methods", "y", "Results", "z"])
    rep = _report(_venue(MANUSCRIPT, ABSTRACT), {"manuscript_file": str(doc), "abstract": "We used scRNA-seq and the Human Cell Atlas (HCA) on DNA from HCA donors with MLP models."})
    assert "'Methods' comes before 'Results'" in _result(rep, "guidelines.section_order").issues[0].message
    abbreviations = _result(rep, "guidelines.abstract_abbreviations").issues[0].message
    assert "scRNA-seq" in abbreviations and "MLP" in abbreviations and "HCA" not in abbreviations and "DNA" not in abbreviations
