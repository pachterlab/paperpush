"""Tests for the full validation report and the checks added with it.

Covers :func:`paperpush.validate.validate_report` (one status per check), the
report renderers (terminal, PDF, HTML, Markdown, text; the colour-blind
palette), the CLI's ``-o`` / ``--colorblind`` / ``--ai*`` flags, the AI task
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
    # Nothing filled in and no files: the checks that read them have nothing to look at.
    for check_id in ("integrity.links", "integrity.dois", "form.urls", "form.schema"):
        assert _result(rep, check_id).status == report.NOT_APPLICABLE, check_id
    assert not rep.ok


def test_switched_off_file_checks_are_unchecked_when_files_exist(_use, tmp_path):
    _use({})
    manuscript = tmp_path / "manuscript.pdf"
    manuscript.write_bytes(b"%PDF-1.4\n")
    rep = _report(_venue(TITLE, MANUSCRIPT), {"title": "A title", "manuscript_file": str(manuscript)})
    assert _result(rep, "form.schema").status == report.PASSED
    for check_id in ("integrity.links", "integrity.dois"):
        assert _result(rep, check_id).status == report.UNCHECKED, check_id
    assert "--dont-check-links" in _result(rep, "integrity.links").detail


def test_file_scans_not_applicable_without_attachments(_use):
    _use({"testj": {"figures": {}}})
    rep = _report(_venue(TITLE, MANUSCRIPT, FIGURES), {}, check_links=True, check_references=True, check_sensitive=True, check_hidden_text=True, check_anonymous=True)
    for check_id in ("integrity.links", "integrity.dois", "integrity.sensitive", "integrity.hidden_text", "integrity.anonymity", "figures.lettering"):
        assert _result(rep, check_id).status == report.NOT_APPLICABLE, check_id


def test_ai_checks_not_applicable_without_attachments(_use):
    _use({"testj": {"notes": ["Use Arial throughout."], "references": {"style": "Vancouver"}}})
    rep = _report(_venue(TITLE, MANUSCRIPT), {})
    assert rep.ai_tasks == []
    for check_id in ("ai.notes.general", "ai.reference_format"):
        assert _result(rep, check_id).status == report.NOT_APPLICABLE, check_id


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
            report.CheckResult("g", "Irrelevant", "Cat", report.NOT_APPLICABLE, detail="no such rule"),
            report.CheckResult("h", "Also irrelevant", "Other", report.NOT_APPLICABLE),
        ],
    )


def test_text_and_markdown_list_every_section():
    rep = _sample_report()
    text = report.to_text(rep)
    for marker in ("[PASS] [Cat] Passes", "[FAIL] [Cat] Breaks", "[WARN]", "[AI] [Cat] Judgement", "[TODO] [Cat] Rule", "[SKIP] [Cat] Off", "error: [title] title is required"):
        assert marker in text
    # Not-applicable checks get a line each, without their reasons.
    assert "  [NA] [Cat] Irrelevant\n  [NA] [Other] Also irrelevant\n" in text and "no such rule" not in text
    assert text.index("FAILED") < text.index("PASSED")
    md = report.to_markdown(rep)
    assert "## ✖ Failed (1)" in md and "## ✔ Passed (1)" in md


def test_errors_only_shows_just_the_failed_section():
    rep = _sample_report()
    text = report.to_text(rep, hide=report.hidden_statuses(errors_only=True))
    # The summary still counts every check, and the report says what it left out.
    assert "[WARN] 1 with warnings" in text and "Not shown: Warnings (1); " in text
    body = text.split("Not shown:", 1)[1]
    assert "[FAIL] [Cat] Breaks" in body and "error: [title] title is required" in body
    for gone in ("PASSED (", "WARNINGS (", "[AI] [", "[TODO] [", "[SKIP] [", "[NA] ["):
        assert gone not in body


def test_hide_warnings_and_hide_unchecked_drop_their_sections():
    rep = _sample_report()
    no_warn = report.to_text(rep, hide=report.hidden_statuses(hide_warnings=True))
    assert "WARNINGS (" not in no_warn and "looks single-spaced" not in no_warn
    assert "[FAIL] [" in no_warn and "[SKIP] [" in no_warn and "[PASS] [" in no_warn
    no_unchecked = report.to_text(rep, hide=report.hidden_statuses(hide_unchecked=True))
    for gone in ("[AI] [", "[TODO] [", "[SKIP] ["):
        assert gone not in no_unchecked
    assert "[WARN] [Cat] Advisory" in no_unchecked and "[PASS] [" in no_unchecked
    assert "Not shown: Not checked: could not run (1); Not checked: needs AI (1); Not checked: not implemented (1)." in no_unchecked
    assert report.hidden_statuses() == frozenset() and "Not shown" not in report.to_text(rep)


def test_hide_warnings_drops_warning_items_under_a_failed_check():
    rep = report.ValidationReport(
        venue="testj",
        subfile="paper.sub",
        results=[report.CheckResult("b", "Breaks", "Cat", report.FAILED, issues=[Issue("error", "", "too long"), Issue(WARNING, "", "also odd")])],
    )
    text = report.to_text(rep, hide=report.hidden_statuses(hide_warnings=True))
    assert "too long" in text and "also odd" not in text


def test_html_uses_the_status_colours_and_the_alt_palette():
    rep = _sample_report()
    html = report.to_html(rep)
    assert report.DEFAULT_PALETTE.passed in html and report.DEFAULT_PALETTE.failed in html and report.DEFAULT_PALETTE.unchecked in html
    alt = report.to_html(rep, alt_color=True)
    assert report.ALT_PALETTE.passed in alt and report.DEFAULT_PALETTE.passed not in alt


def test_each_status_has_its_own_colour():
    """Green / red / amber / purple / grey: needs AI is purple, and the two
    "not checked" states that are not AI share grey rather than a new hue."""
    for pal in (report.DEFAULT_PALETTE, report.ALT_PALETTE):
        verdicts = {pal.for_status(s) for s in (report.PASSED, report.FAILED, report.WARNING, report.NEEDS_AI)}
        assert len(verdicts) == 4
        assert pal.for_status(report.NEEDS_AI) != pal.for_status(report.UNCHECKED)
    assert report.DEFAULT_PALETTE.needs_ai == "#8250df"
    assert report.ALT_PALETTE.needs_ai == "#aa3377"


def test_web_and_report_palettes_agree():
    """style.css's status colours are report.py's, in both palettes."""
    css = (Path(report.__file__).parent / "web" / "static" / "style.css").read_text()
    root, alt = css.split(":root {", 1)[1].split("}", 1)[0], css.split('html[data-palette="alt"] {', 1)[1].split("}", 1)[0]
    for block, pal in ((root, report.DEFAULT_PALETTE), (alt, report.ALT_PALETTE)):
        for var, colour in (("--st-passed", pal.passed), ("--st-failed", pal.failed), ("--st-warning", pal.warning), ("--st-ai", pal.needs_ai), ("--st-unchecked", pal.unchecked)):
            assert f"{var}: {colour};" in block, (var, colour)


def test_colorblind_flag_opens_the_web_page_in_the_same_palette(tmp_path, monkeypatch):
    from paperpush import cli
    from paperpush.web.app import local_url

    assert local_url("127.0.0.1", 1, "t", "biorxiv", colorblind=True) == "http://127.0.0.1:1/?local=t&palette=alt#/v/biorxiv"
    assert "palette" not in local_url("127.0.0.1", 1, "t", "biorxiv")
    seen = {}
    monkeypatch.setattr(cli, "_open_web", lambda path, **kw: seen.update(kw) or 0)
    sub = tmp_path / "paper.sub"
    sub.write_text("venue: biorxiv\n")
    assert main(["validate", str(sub), "--web", "--colorblind"]) == 0
    assert seen["colorblind"] is True
    assert main(["validate", str(sub), "--web", "--alt-color"]) == 0 and seen["colorblind"] is True


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
    assert main(["validate", str(sub), "--dont-check-links", "--dont-check-references", "-o", str(tmp_path / "r.html"), "--colorblind"]) == 0
    assert report.ALT_PALETTE.passed in (tmp_path / "r.html").read_text()
    out = capsys.readouterr().out
    assert "PASSED" in out and "Saved the report to" in out


def test_cli_display_flags_filter_the_terminal_and_saved_report(_use, tmp_path, monkeypatch, capsys):
    _use({})
    _cli_venue(monkeypatch, TITLE)
    sub = _sub(tmp_path, "title: A study\n")
    out_md = tmp_path / "r.md"
    assert main(["validate", str(sub), "--dont-check-links", "--dont-check-references", "--show-errors-only", "-o", str(out_md)]) == 0
    out = capsys.readouterr().out
    assert "PASSED (" not in out and "Not shown:" in out
    assert "## ✔ Passed" not in out_md.read_text() and "Passed (" in out_md.read_text()
    assert main(["validate", str(sub), "--dont-check-links", "--dont-check-references", "--hide-warnings", "--hide-unchecked"]) == 0
    assert "PASSED (" in capsys.readouterr().out


def test_cli_refuses_to_replace_a_saved_report_without_overwrite(_use, tmp_path, monkeypatch, capsys):
    _use({})
    _cli_venue(monkeypatch, TITLE)
    sub = _sub(tmp_path, "title: A study\n")
    target = tmp_path / "r.md"
    target.write_text("old", encoding="utf-8")
    args = ["validate", str(sub), "--dont-check-links", "--dont-check-references", "-o", str(target)]
    assert main(args) == 1
    assert "use --overwrite" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "old"
    assert main(args + ["--overwrite"]) == 0
    assert target.read_text(encoding="utf-8") != "old"
    target.write_text("old", encoding="utf-8")
    assert main(args + ["--force"]) == 0  # old name for --overwrite
    assert target.read_text(encoding="utf-8") != "old"


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


def test_cli_ai_only_reports_just_the_ai_checks(_use, tmp_path, monkeypatch, capsys):
    pdf = _manuscript_with_notes(_use, tmp_path)
    _cli_venue(monkeypatch, TITLE, MANUSCRIPT)
    # An empty title would be a blocking error in a full run; --ai-only never checks it.
    sub = _sub(tmp_path, f"manuscript_file: {pdf}\n")
    tasks = tmp_path / "tasks.json"
    assert main(["validate", str(sub), "--ai-only", "--ai-tasks", str(tasks)]) == 0
    out = capsys.readouterr().out
    assert "Title is required" not in out and "NEEDS AI" in out and "AI checks only (--ai-only)" in out
    assert "--ai-only --ai-results" in out
    assert "ai.notes.general" in {t["id"] for t in json.loads(tasks.read_text())["tasks"]}

    results = tmp_path / "results.json"
    results.write_text(json.dumps({"results": [{"id": "ai.notes.general", "status": "fail", "details": "pages are not numbered"}]}))
    assert main(["validate", str(sub), "--ai-only", "--ai-results", str(results)]) == 0
    out = capsys.readouterr().out
    assert "pages are not numbered" in out and "Ran only the AI checks" in out


def test_ai_only_drops_every_deterministic_result(_use, tmp_path):
    pdf = _manuscript_with_notes(_use, tmp_path)
    rep = _report(_venue(TITLE, MANUSCRIPT), {"manuscript_file": str(pdf)}, ai_only=True)
    assert rep.ai_only and rep.results
    assert {r.id for r in rep.results} == {t.id for t in rep.ai_tasks}
    assert rep.to_dict()["ai_only"] is True


def test_cli_ai_only_needs_the_manuscript_checks(_use, tmp_path, monkeypatch, capsys):
    _use({})
    _cli_venue(monkeypatch, TITLE)
    assert main(["validate", str(_sub(tmp_path, "title: A study\n")), "--ai-only", "--dont-check-manuscript"]) == 1
    assert "--ai-only cannot be combined with --dont-check-manuscript" in capsys.readouterr().err


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
    only = mcp_server.validate_subfile(str(sub), ai_only=True)
    assert only["ai_only"] and {c["id"] for c in only["checks"]} == {t["id"] for t in only["ai_tasks"]}


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


def test_panel_mentions_parse_lists_ranges_and_skip_other_papers():
    cites = citation_check.panel_mentions("See Figs. 2a and 3b\u2013d, then Fig. 2a,c. Fig. 4a and a control. Fig. 5a\u20135c.")
    assert [(c.figure, c.letter) for c in cites] == [(2, "a"), (3, "b"), (3, "c"), (3, "d"), (2, "a"), (2, "c"), (4, "a"), (5, "a"), (5, "b"), (5, "c")]
    others = "As in Fig. 3b of ref. 12, Fig. 1c in Smith et al. 2020, (ref. 4, Fig. 5a) and Appendix Fig. 2b."
    assert citation_check.panel_mentions(others) == []
    in_refs = "Intro cites Fig. 1a.\nReferences\n1. A. Author. Fig. 9a of a book. 2020.\n"
    assert [c.figure for c in citation_check.panel_mentions(in_refs)] == [1]


def test_panel_mentions_cover_supplementary_extended_data_and_tables():
    text = "Supplementary Figs. 2a and 3b, Fig. S4a\u2013S4c, Extended Data Fig. 1b, Table 2a, b and Supplementary Table 1c. Figs. 1b and S5a."
    cites = [(c.series, c.number, c.letter) for c in citation_check.panel_mentions(text)]
    assert cites == [
        ("supp_figure", 2, "a"), ("supp_figure", 3, "b"), ("supp_figure", 4, "a"), ("supp_figure", 4, "b"), ("supp_figure", 4, "c"),
        ("ed_figure", 1, "b"), ("table", 2, "a"), ("table", 2, "b"), ("supp_table", 1, "c"), ("figure", 1, "b"), ("supp_figure", 5, "a"),
    ]
    assert [c.number for c in citation_check.panel_mentions(text, "figure")] == [1]


def test_panel_order_is_checked_in_every_series():
    text = "Supplementary Fig. 2c, then Supplementary Fig. 2b. Extended Data Fig. 1b before Extended Data Fig. 1a. Table 3b, then Table 3a. Fig. 1a, b."
    problems, _ = citation_check.panel_order_problems(citation_check.panel_mentions(text))
    assert [p.split(" is first")[0] for p in problems] == ["Table 3b", "Supplementary Fig. 2c", "Extended Data Fig. 1b"]


def test_panel_order_uses_first_citations_only():
    problems, _ = citation_check.panel_order_problems(citation_check.panel_mentions("Fig. 1a. Then Fig. 1c. Then Fig. 1b."))
    assert problems == [problems[0]] and "Fig. 1c is first cited before Fig. 1b" in problems[0]
    problems, detail = citation_check.panel_order_problems(citation_check.panel_mentions("Fig. 1a, b, then Fig. 1a again, and Fig. 1c. Fig. 2A-C."))
    assert not problems and "2 figure(s)" in detail


def test_panel_order_check_runs_on_the_manuscript(_use, tmp_path):
    _use({"testj": {}})
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["Intro", "See Fig. 1a and Fig. 1c, then Fig. 1b.", "Figure 1. A legend."])
    rep = _report(_venue(MANUSCRIPT), {"manuscript_file": str(doc)})
    result = _result(rep, "citations.panels")
    assert "Fig. 1c is first cited before Fig. 1b" in result.issues[0].message


def test_caption_panels_reads_the_letters_a_legend_names():
    assert citation_check.caption_panels("Figure 1: Overview. (a) One. (b) Two. (c) Three.") == ["a", "b", "c"]
    assert citation_check.caption_panels("Figure 2 | Setup. a, The rig. b, Its output.") == ["a", "b"]
    assert citation_check.caption_panels("Figure 3. **A** One. **B** Two.") == ["a", "b"]
    # Initials and a lone "(a)" are not panels.
    assert citation_check.caption_panels("Figure 4. A, B and C were measured (a) once.") == []


def test_uncited_panels_are_reported_only_for_figures_cited_by_panel():
    cites = citation_check.panel_mentions("See Fig. 1a and b, and Fig. 2.")
    problems = citation_check.uncited_panel_problems(cites, {1: ["a", "b", "c"], 2: ["a", "b"]})
    assert problems == ["Fig. 1 has panel c in its legend that the text never cites (cited: a, b)"]


def test_tex_panels_from_labels_and_subfigures():
    source = (
        "\\begin{document}\nSee \\ref{fig:one}a and \\ref{fig:one}(b), and \\ref{fig:two-a}.\n"
        "\\begin{figure}\\includegraphics{x}\\caption{\\textbf{a} One. \\textbf{b} Two. \\textbf{c} Three.}\\label{fig:one}\\end{figure}\n"
        "\\begin{figure}\\begin{subfigure}{.5\\textwidth}\\caption{A}\\label{fig:two-a}\\end{subfigure}"
        "\\begin{subfigure}{.5\\textwidth}\\caption{B}\\label{fig:two-b}\\end{subfigure}\\caption{Two panels.}\\label{fig:two}\\end{figure}\n\\end{document}"
    )
    cites = citation_check.tex_panel_mentions(source)
    assert [(c.number, c.letter) for c in cites] == [(1, "a"), (1, "b"), (2, "a")]
    assert citation_check.tex_caption_panels(source) == {1: ["a", "b", "c"], 2: ["a", "b"]}
    problems = citation_check.uncited_panel_problems(cites, citation_check.tex_caption_panels(source))
    assert [p.split(" in its")[0] for p in problems] == ["Fig. 1 has panel c", "Fig. 2 has panel b"]


def test_uncited_panel_check_runs_on_the_manuscript(_use, tmp_path):
    _use({"testj": {}})
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["Intro", "See Fig. 1a and Fig. 1b.", "Figure 1. A legend. (a) One. (b) Two. (c) Three."])
    rep = _report(_venue(MANUSCRIPT), {"manuscript_file": str(doc)})
    result = _result(rep, "citations.panels")
    assert result.status == report.WARNING
    assert "Fig. 1 has panel c in its legend that the text never cites" in result.issues[0].message


def test_tex_undefined_refs_finds_keys_no_label_defines():
    source = (
        r"\label{fig:a} \label[figure]{fig:typed} See \ref{fig:a}, \cref{fig:a,fig:b} and \eqref{eq:1}."
        r" \newcommand{\figref}[1]{Fig.~\ref{fig:#1}} \crefrange{fig:a}{fig:z} \hyperref[sec:x]{X}"
        r" \begin{lstlisting}[label=lst:1] \end{lstlisting} \ref{lst:1} \autoref{fig:typed}"
    )
    assert citation_check.tex_undefined_refs(source) == ["fig:b", "eq:1", "fig:z", "sec:x"]
    # Labels made by a macro: keys with its prefix may exist.
    assert citation_check.tex_undefined_refs(r"\newcommand{\fl}[1]{\label{fig:#1}} \ref{fig:one} \ref{tab:two}") == ["tab:two"]
    # xr: the labels live in another document.
    assert citation_check.tex_undefined_refs(r"\externaldocument{supp} \ref{fig:s1}") is None


def test_placeholder_problems_find_query_marks_and_word_errors():
    pages = ["See Figure ?? and Section ??. Really?? We cite [?] and [?, ?].", "As in Error! Reference source not found. above."]
    problems = citation_check.placeholder_problems(pages, citations=True, word=True)
    assert problems[0].startswith("2 × “??”") and "page 1:" in problems[0]
    assert problems[1].startswith("2 × “[?]”")
    assert problems[2].startswith("1 × a broken Word cross-reference field") and "page 2:" in problems[2]
    assert citation_check.placeholder_problems(["What?? No."], citations=True, word=True) == []
    # Compiled LaTeX: undefined citations are bibliography.uncited's to report.
    assert len(citation_check.placeholder_problems(["Table ?? and [?]"], citations=False, word=False)) == 1


def test_cross_reference_check_reports_latex_source(_use, tmp_path):
    _use({"testj": {}})
    bundle = tmp_path / "src.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("main.tex", "\\documentclass{article}\\begin{document}\\input{body}\\end{document}")
        zf.writestr("body.tex", "\\section{Intro}\\label{sec:intro} See Section~\\ref{sec:intro} and Figure~\\ref{fig:missing}.")
    rep = _report(_venue(MANUSCRIPT), {"manuscript_file": str(bundle)})
    result = _result(rep, "citations.cross_references")
    assert result.status == report.WARNING
    assert "fig:missing" in result.issues[0].message and "sec:intro" not in result.issues[0].message


def test_cross_reference_check_reports_word_field_errors(_use, tmp_path):
    _use({"testj": {}})
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["Intro", "As shown in Error! Reference source not found., the effect is large."])
    result = _result(_report(_venue(MANUSCRIPT), {"manuscript_file": str(doc)}), "citations.cross_references")
    assert result.status == report.WARNING and "Error! Reference source not found" in result.issues[0].message


def test_cross_reference_check_passes_clean_text(_use, tmp_path):
    _use({"testj": {}})
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["Intro", "As shown in Figure 1, the effect is large [3]."])
    result = _result(_report(_venue(MANUSCRIPT), {"manuscript_file": str(doc)}), "citations.cross_references")
    assert result.status == report.PASSED
    assert _result(_report(_venue(MANUSCRIPT), {}), "citations.cross_references").status == report.NOT_APPLICABLE


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
    assert "never cited" not in lettering


def test_labelled_panels_the_text_never_cites(_use, tmp_path):
    _pdfminer()
    _use({"testj": {"figures": {"min_font_size_pt": 5}}})
    fig = _figure_pdf(tmp_path / "fig2.pdf", labels=("a", "b", "c", "x"))
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["See Fig. 2a and Fig. 2c, and Figure 2 as a whole."])
    rep = _report(_venue(MANUSCRIPT, FIGURES), {"manuscript_file": str(doc), "figure_files": f"{fig} | Figure 2"})
    lettering = " ".join(i.message for i in _result(rep, "figures.lettering").issues)
    assert "panel(s) b of Figure 2 is never cited" in lettering
    assert " x " not in lettering  # a stray letter outside the a, b, c run


def test_raster_panels_cited_goes_to_an_ai_task(_use, tmp_path):
    from PIL import Image

    _use({"testj": {"figures": {"min_font_size_pt": 5}}})
    png = tmp_path / "fig1.png"
    Image.new("RGB", (600, 400), "white").save(png, dpi=(300, 300))
    doc = tmp_path / "ms.docx"
    _build_docx(doc, ["See Fig. 1a and Fig. 1b."])
    rep = _report(_venue(MANUSCRIPT, FIGURES), {"manuscript_file": str(doc), "figure_files": f"{png} | Figure 1"})
    task = next(t for t in rep.ai_tasks if t.id == "ai.figures.raster_panels_cited")
    assert "cites panel(s) a, b" in task.instructions and str(png) in task.files


def test_figure_content_match_goes_to_an_ai_task(_use, tmp_path):
    from PIL import Image

    _use({"testj": {"figures": {"min_font_size_pt": 5}}})
    pngs = []
    for n in (1, 2):
        png = tmp_path / f"fig{n}.png"
        Image.new("RGB", (600, 400), "white").save(png, dpi=(300, 300))
        pngs.append(png)
    doc = tmp_path / "ms.docx"
    _build_docx(doc, [
        "Survival fell in treated mice (Fig. 1b). Expression rose over time (Fig. 1a).",
        "The pathway is summarized in Figure 2.",
        "Figure 1. Treatment response. a, Expression over time. b, Kaplan-Meier survival curve.",
    ])
    rep = _report(_venue(MANUSCRIPT, FIGURES), {"manuscript_file": str(doc), "figure_files": f"{pngs[0]} | Figure 1\n{pngs[1]} | Figure 2"})
    task = next(t for t in rep.ai_tasks if t.id == "ai.figures.content_matches_text")
    assert set(task.files) == {str(p) for p in pngs}
    text = task.instructions
    assert "Figure 1: fig1.png" in text and "Figure 2: fig2.png" in text
    assert "Legend: Figure 1. Treatment response." in text
    assert 'on panel(s) b: "Survival fell in treated mice (Fig. 1b)."' in text
    assert 'on panel(s) a: "Expression rose over time (Fig. 1a)."' in text
    assert 'on the whole figure: "The pathway is summarized in Figure 2."' in text


def test_figure_descriptions_keep_sentences_whole_across_abbreviations():
    from paperpush.figure_check import figure_descriptions

    text = "As Smith et al. found, the effect held (Figs. 3a, c). Next we looked elsewhere."
    _, sentences = figure_descriptions(text)
    assert sentences[3] == [("As Smith et al. found, the effect held (Figs. 3a, c).", {"a", "c"})]


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


# ===========================================================================
# initial submission in any format (--initial-submission)
# ===========================================================================

_ANY_FORMAT = {"testj": {"manuscript": {"initial_submission_any_format": "Initial submissions need no special formatting."}}}


def test_any_format_rule_is_on_the_report_without_the_flag(_use):
    _use(_ANY_FORMAT)
    rep = _report(_venue(TITLE), {})
    assert rep.initial_submission_rule == "Initial submissions need no special formatting."
    assert not rep.initial_submission and not rep.ok
    assert _result(rep, "form.required").status == report.FAILED
    text = report.to_text(rep)
    assert "Format for initial submission: Initial submissions need no special formatting." in text
    assert "--initial-submission" in text
    # The note sits at the top, before the verdict.
    assert text.index("Format for initial submission") < text.index("Not ready")


def test_initial_submission_turns_errors_into_warnings(_use):
    _use(_ANY_FORMAT)
    rep = _report(_venue(TITLE), {}, initial_submission=True)
    required = _result(rep, "form.required")
    assert required.status == report.WARNING
    assert required.issues and all(i.level == WARNING for i in required.issues)
    assert rep.ok and not rep.errors and rep.downgraded == 1
    assert not rep.by_status(report.FAILED)
    text = report.to_text(rep)
    assert "Ready to submit as an initial submission: 1 error(s) shown as warnings" in text
    for render in (report.to_markdown, report.to_html):
        assert "Format for initial submission" in render(rep)


def test_initial_submission_without_a_rule_says_the_venue_has_none(_use):
    _use({})
    rep = _report(_venue(TITLE), {}, initial_submission=True)
    assert rep.ok and rep.initial_submission_rule == ""
    assert "do not say that an initial submission may be in any format" in report.to_text(rep)
    assert "Format for initial submission" not in report.to_text(rep)


def test_any_format_rule_follows_the_article_type(_use):
    _use({"testj": {"article_type_field": "kind", "article_types": {"Letter": {"manuscript": {"initial_submission_any_format": "Letters may be in any format."}}}}})
    assert r.any_format_initial_rule("testj") == ""
    assert r.any_format_initial_rule("testj", {"kind": "Letter"}) == "Letters may be in any format."
    # Informational: never listed as a guideline rule with no check.
    venue = _venue(Field(id="kind", label="Kind", type="text"))
    rep = _report(venue, {"kind": "Letter"})
    assert rep.initial_submission_rule == "Letters may be in any format."
    assert not any("initial_submission_any_format" in res.id for res in rep.results)


def test_cli_initial_submission_flag_passes_with_warnings(_use, tmp_path, monkeypatch, capsys):
    _use(_ANY_FORMAT)
    _cli_venue(monkeypatch, TITLE)
    sub = _sub(tmp_path, "")
    args = ["validate", str(sub), "--dont-check-links", "--dont-check-references"]
    assert main(args) == 1
    out = capsys.readouterr().out
    assert "Format for initial submission" in out
    assert main(args + ["--initial-submission", "-o", str(tmp_path / "r.pdf")]) == 0
    out = capsys.readouterr().out
    assert "as an initial submission (1 error(s) shown as warnings)" in out
    assert (tmp_path / "r.pdf").read_bytes().startswith(b"%PDF-")


def test_real_any_format_venues_carry_the_rule():
    for slug in ("nature", "cell", "plos_compbio", "pnas", "bioinformatics"):
        assert r.any_format_initial_rule(slug), slug
    for slug in ("biorxiv", "arxiv", "science"):
        assert not r.any_format_initial_rule(slug), slug


def test_progress_reports_every_stage_in_order_then_done():
    events = []
    _report(_venue(TITLE), {"title": "A paper"}, progress=lambda done, total, label: events.append((done, total, label)))
    totals = {total for _, total, _ in events}
    assert len(totals) == 1
    total = totals.pop()
    assert [done for done, _, _ in events] == list(range(total)) + [total]
    labels = [label for _, _, label in events]
    assert labels[0] == "Submission form"
    assert labels[-2:] == ["AI checks", "done"]
    # Switched-off checks are not stages.
    assert "Links" not in labels and "Reference DOIs" not in labels


def test_progress_callback_errors_do_not_break_validation():
    def boom(*_):
        raise RuntimeError("display broke")

    rep = _report(_venue(TITLE), {"title": "A paper"}, progress=boom)
    assert _result(rep, "form.required").status == report.PASSED


def test_progress_in_ai_only_mode():
    events = []
    _report(_venue(TITLE), {"title": "A paper"}, ai_only=True, progress=lambda *e: events.append(e))
    assert events[-1][0] == events[-1][1] and events[-1][2] == "done"


# ===========================================================================
# reference search and versions (paperpush.reference_search)
# ===========================================================================

_BIB_FIELD = Field(id="bibliography", label="Bibliography", type="file")


def _bib_report(_use, tmp_path, bib: str, **kwargs):
    _use({})
    path = tmp_path / "refs.bib"
    path.write_text(bib, encoding="utf-8")
    manuscript = tmp_path / "manuscript.tex"
    manuscript.write_text("\\documentclass{article}\\begin{document}Text \\cite{a}.\\bibliography{refs}\\end{document}\n", encoding="utf-8")
    venue = _venue(TITLE, MANUSCRIPT, _BIB_FIELD)
    return _report(venue, {"title": "A title", "manuscript_file": str(manuscript), "bibliography": str(path)}, **{"check_references": True, **kwargs})


def test_reference_search_checks_are_switched_off_with_the_doi_check(_use, tmp_path):
    rep = _bib_report(_use, tmp_path, "@article{a, title={T}, author={A}}", check_references=False)
    for check_id in ("integrity.unmatched_references", "integrity.reference_versions"):
        assert _result(rep, check_id).status == report.UNCHECKED
        assert "--dont-check-references" in _result(rep, check_id).detail


def test_a_fabricated_reference_is_reported_and_an_unindexed_one_goes_to_the_ai(_use, tmp_path, monkeypatch):
    from paperpush import reference_search as rs

    monkeypatch.setattr(rs, "crossref_search", lambda q, s: [])
    monkeypatch.setattr(rs, "openalex_search", lambda t, s: [])
    rep = _bib_report(
        _use,
        tmp_path,
        "@article{a, title={Quantum entanglement governs sourdough microbiomes}, author={Smith, J}, journal={Nature}, year={2021}}\n" "@book{b, title={A Laboratory Manual of Something}, author={Doe, J}, year={2001}}\n",
    )
    res = _result(rep, "integrity.unmatched_references")
    assert res.status == report.WARNING
    messages = " ".join(i.message for i in res.issues)
    assert "bibliography entry 'a'" in messages and "may be fabricated" in messages
    assert any(i.unchecked and "bibliography entry 'b'" in i.message for i in res.issues)
    (task,) = [t for t in rep.ai_tasks if t.id == "ai.unverified_references"]
    assert "bibliography entry 'b'" in task.instructions and "bibliography entry 'a'" not in task.instructions
    assert _result(rep, "ai.unverified_references").status == report.NEEDS_AI


def test_registries_that_do_not_answer_leave_the_search_unchecked(_use, tmp_path):
    # The conftest makes every request fail.
    rep = _bib_report(_use, tmp_path, "@article{a, title={A perfectly real paper title}, author={Doe, J}, journal={J}, year={2020}}")
    assert _result(rep, "integrity.unmatched_references").status == report.UNCHECKED


def test_every_reference_with_a_doi_passes_the_search(_use, tmp_path):
    rep = _bib_report(_use, tmp_path, "@article{a, title={A real paper}, author={Doe, J}, doi={10.1/x}}")
    assert _result(rep, "integrity.unmatched_references").status == report.PASSED


def test_a_published_preprint_doi_is_reported_by_the_version_check(_use, tmp_path, monkeypatch):
    from paperpush import references

    record = {"title": "Modular and efficient pre-processing", "author": [{"family": "Melsted"}], "issued": {"date-parts": [[2019]]}, "type": "posted-content", "relation": {"is-preprint-of": [{"id-type": "doi", "id": "10.1038/s41587-021-00870-2"}]}}
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (references.DOI_FOUND, record))
    rep = _bib_report(_use, tmp_path, "@article{pre, title={Modular and efficient pre-processing}, author={Melsted, P}, year={2019}, doi={10.1101/673285}}")
    res = _result(rep, "integrity.reference_versions")
    assert res.status == report.WARNING and "10.1038/s41587-021-00870-2" in res.issues[0].message


def test_bib_structure_check_runs_with_the_manuscript_checks(_use, tmp_path):
    rep = _bib_report(_use, tmp_path, "@article{a, title={One}, author={A}}\n@article{a, title={Two}, author={B}}\n")
    res = _result(rep, "bibliography.entries")
    assert res.status == report.WARNING and "citation key 'a'" in res.issues[0].message
