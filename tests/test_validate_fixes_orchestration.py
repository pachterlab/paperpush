"""Regression tests for the orchestration review of ``paperpush validate``.

One test per finding: the form and file checks, what the report says when a
check could not look, how AI verdicts are merged, and the front ends agreeing.
Everything runs offline; network scanners are mocked.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from paperpush import ai_validate, file_inputs, filematch, mcp_server, sensitive
from paperpush.cli import main
from paperpush.database import Field, Venue, get_venue
from paperpush.report import ValidationReport, CheckResult, _layout, output_path
from paperpush.subfile import SubFile, load, resolve_relative_path
from paperpush.validate import Issue, unchecked, validate_report
from tests.pdf_builder import text_line, write_pdf

validate_mod = importlib.import_module("paperpush.validate")
OFF = dict(check_links=False, check_references=False, check_sensitive=False, check_hidden_text=False, check_openreview=False, check_manuscript=False)


@pytest.fixture(autouse=True)
def bundled(monkeypatch):
    monkeypatch.setenv("PAPERPUSH_VENUE_DATA", "bundled")


def _run(venue, values, **flags):
    venue = get_venue(venue) if isinstance(venue, str) else venue
    return validate_report(SubFile(venue=venue.slug, values=values), venue, **{**OFF, **flags})


def _check(report, check_id):
    return next(r for r in report.results if r.id == check_id)


def _pdf(tmp_path, name="paper.pdf"):
    return write_pdf(tmp_path / name, [text_line(72, 700, "Hello world manuscript text.")])


# --- 1: a malformed value does not end the run ---


def test_malformed_url_is_an_error_not_a_crash():
    report = _run("biorxiv", {"data_availability_links": "http://[foo"})
    assert _check(report, "form.urls").status == "failed"


@pytest.mark.parametrize("value", ["~nosuchuser999/a.pdf", "x" * 5000 + ".pdf"])
def test_unusable_path_is_an_error_not_a_crash(value):
    report = _run("biorxiv", {"manuscript_file": value}, check_manuscript=True)
    assert _check(report, "files.uploads").status == "failed"


def test_unusable_path_is_kept_by_the_sub_path_resolver(tmp_path):
    assert resolve_relative_path("~nosuchuser999/a.pdf", tmp_path) == "~nosuchuser999/a.pdf"


def test_a_field_check_that_breaks_is_recorded_as_unchecked(monkeypatch):
    def boom(field, raw):
        raise KeyError("x")

    monkeypatch.setattr(validate_mod, "_check_authors", boom)
    report = _run("biorxiv", {"authors": "A | a@b.co | MIT | | yes"})
    required = _check(report, "form.required")
    assert any(i.unchecked and i.field == "authors" for i in required.issues)


# --- 2: options that hold commas ---


def test_multichoice_option_with_commas_is_one_option():
    value = "New genome expression or sequencing data (ChIP-seq, RNA-seq)"
    assert _check(_run("nucleic_acids_research", {"data_deposition_types": value}), "form.options").status == "passed"


def test_multichoice_count_treats_a_comma_option_as_one():
    field = next(f for f in get_venue("bioinformatics").fields if f.id == "portal_keywords")
    value = ", ".join([o for o in field.options if "," not in o][:5] + ["Association study (GWAS, EWAS)"])
    report = _run("bioinformatics", {"portal_keywords": value})
    assert _check(report, "form.counts").status == "passed"
    assert _check(report, "form.options").status == "passed"


def test_multichoice_coded_option_with_commas_and_bare_codes():
    value = "cs.CE (Computational Engineering, Finance, and Science), q-bio.GN"
    assert _check(_run("arxiv", {"crosslist_categories": value}), "form.options").status == "passed"


# --- 3: hidden text without a PDF reader ---


def test_hidden_text_is_unchecked_without_pdfminer(tmp_path, monkeypatch):
    from paperpush import hidden_text, pdf_layout

    monkeypatch.setattr(pdf_layout, "available", lambda: False)
    monkeypatch.setattr(pdf_layout, "read_pages", lambda path: None)
    monkeypatch.setattr(hidden_text, "scan_paths", lambda paths: [])
    report = _run("biorxiv", {"manuscript_file": str(_pdf(tmp_path))}, check_hidden_text=True)
    result = _check(report, "integrity.hidden_text")
    assert result.status == "unchecked" and "pdfminer" in result.detail


@pytest.mark.parametrize("name, data", [("bad.pdf", b"%PDF-1.4 garbage" * 200), ("bad.docx", b"garbage" * 200)])
def test_unreadable_attachment_is_unchecked_for_hidden_text_and_links(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    report = _run("biorxiv", {"manuscript_file": str(path)}, check_hidden_text=True, check_links=True)
    assert _check(report, "integrity.hidden_text").status == "unchecked"
    assert _check(report, "integrity.links").status == "unchecked"


# --- 4: links that got no answer ---


def _with_links(tmp_path):
    path = tmp_path / "links.txt"
    path.write_text("See https://example.org/a and https://example.org/b .\n")
    return {"supplementary_files": str(path)}


def test_links_are_unchecked_when_none_answered(tmp_path, monkeypatch):
    monkeypatch.setattr(sensitive, "_url_is_reachable", lambda url: None)
    assert _check(_run("biorxiv", _with_links(tmp_path), check_links=True), "integrity.links").status == "unchecked"


def test_links_partly_answered_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(sensitive, "_url_is_reachable", lambda url: None if url.endswith("/b") else True)
    result = _check(_run("biorxiv", _with_links(tmp_path), check_links=True), "integrity.links")
    assert result.status == "unchecked"
    assert "1 of 2" in result.issues[0].message


def test_links_all_answered_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(sensitive, "_url_is_reachable", lambda url: True)
    assert _check(_run("biorxiv", _with_links(tmp_path), check_links=True), "integrity.links").status == "passed"


# --- 5: no publisher re-run of the integrity checks ---


def test_integrity_checks_are_not_rerun_for_publisher_rules(tmp_path, monkeypatch):
    from paperpush import references

    calls = []

    def scan(paths, **kw):
        calls.append(1)
        return [sensitive.Finding("x.pdf", references.RETRACTED, "10.1/x is retracted")]

    monkeypatch.setattr(references, "scan_references", scan)
    monkeypatch.setattr(validate_mod, "_unmatched_reference_issues", lambda *a: [])
    monkeypatch.setattr(validate_mod, "_reference_version_issues", lambda *a: [])
    report = _run("bmc_biology", {"manuscript_file": str(_pdf(tmp_path))}, check_references=True)
    assert len(_check(report, "integrity.retractions").issues) == 1
    assert len(calls) == 1


def test_link_finding_is_not_labelled_a_house_rule(tmp_path, monkeypatch):
    finding = sensitive.Finding("x.pdf", "broken link", "https://a is gone")
    monkeypatch.setattr(sensitive, "scan_links", lambda paths, stats=None: [finding])
    result = _check(_run("bmc_biology", {"manuscript_file": str(_pdf(tmp_path))}, check_links=True), "integrity.links")
    assert "house rule" not in result.issues[0].message


# --- 6: a blank first or last name ---


@pytest.mark.parametrize("line", [" | Ada |  | a@b.co | MIT | USA |  | yes", " |  | Lovelace | a@b.co | MIT | USA |  | yes"])
def test_blank_first_or_last_name_is_an_error(line):
    assert _check(_run("nature", {"authors": line}), "form.authors").status == "failed"


# --- 7: the model cache follows the fields ---


def test_schema_model_is_not_shared_between_field_lists(tmp_path):
    venue = get_venue("biorxiv")
    placed = file_inputs.apply_files(venue, {}, {"title_page": str(_pdf(tmp_path))})
    assert _check(_run(venue, {"title": "x"}), "form.schema").status == "passed"
    assert _check(_run(placed.venue, {"title": "x", **placed.values}), "form.schema").status == "passed"
    assert _check(_run(venue, {"title": "x", "title_page_file": "stray"}), "form.schema").status == "failed"


# --- 8: a .sub with no venue line ---


def test_sub_without_venue_is_general_whatever_its_name(tmp_path):
    # A venue's name in the file name does not stand in for the @venue: line.
    (tmp_path / "nature.sub.txt").write_text("title: x\n")
    assert load(tmp_path / "nature.sub.txt").venue == "general"


def test_cli_validates_a_sub_without_venue_against_the_general_checks(tmp_path, capsys):
    sub = tmp_path / "zzzjournal.sub.txt"
    sub.write_text("title: x\n")
    code = main(["validate", str(sub), "--dont-check-links", "--dont-check-references", "--dont-check-manuscript"])
    captured = capsys.readouterr()
    assert code != 2 and "unknown venue" not in captured.err
    assert "general" in captured.out.lower()


def test_submit_refuses_a_sub_without_venue(tmp_path, capsys):
    sub = tmp_path / "nature.sub.txt"
    sub.write_text("title: x\n")
    assert main(["submit", str(sub), "--skip-validate"]) != 0
    assert "general .sub" in capsys.readouterr().err


def test_mcp_resolves_a_sub_without_venue_to_general(tmp_path):
    from paperpush import mcp_server

    sub = tmp_path / "nature.sub.txt"
    sub.write_text("title: x\n")
    assert mcp_server._load_subfile(str(sub))[2].is_general


def test_web_resolves_a_sub_without_venue_to_general(tmp_path):
    pytest.importorskip("fastapi")
    from paperpush.web import app as web_app

    _, venue = web_app._localized_sub("title: x\n", tmp_path)
    assert venue.is_general


def _no_venue_validate(sub, *extra):
    return main(["validate", str(sub), "--disable-integrity.links", "--disable-references", "--disable-manuscript", *extra])


def test_venue_flag_fills_in_a_missing_venue_line(tmp_path, capsys):
    sub = tmp_path / "draft.sub.txt"
    sub.write_text("title: x\n")
    assert load(sub, default_venue="nature").venue == "nature"
    _no_venue_validate(sub, "--venue", "nature")
    captured = capsys.readouterr()
    assert "Venue: nature" in captured.out and "is for" not in captured.err


def test_venue_flag_cannot_change_a_stated_venue(tmp_path, capsys):
    sub = tmp_path / "draft.sub.txt"
    sub.write_text("@venue: biorxiv\ntitle: x\n")
    assert load(sub, default_venue="nature").venue == "biorxiv"
    assert _no_venue_validate(sub, "--venue", "nature") == 1
    assert "is for biorxiv, not nature" in capsys.readouterr().err


def test_venue_flag_stays_in_the_ai_merge_command_for_a_sub_without_venue(tmp_path, capsys):
    sub = tmp_path / "draft.sub.txt"
    sub.write_text("title: x\n")
    main(["validate", str(sub), "--disable-integrity.links", "--disable-references", "--venue", "nature", "--ai-tasks", str(tmp_path / "tasks.json")])
    captured = capsys.readouterr()
    assert "--venue nature" in captured.out + captured.err


def test_mcp_venue_fills_in_a_missing_venue_line(tmp_path):
    from paperpush import mcp_server

    sub = tmp_path / "draft.sub.txt"
    sub.write_text("title: x\n")
    assert mcp_server._load_subfile(str(sub), default_venue="nature")[2].slug == "nature"
    assert mcp_server._load_subfile(str(sub))[2].is_general


# --- 9: closed lists on a textarea ---


def test_textarea_options_are_checked_per_line():
    report = _run("plos_compbio", {"classifications": "Not a class\nGenomics"})
    result = _check(report, "form.options")
    assert result.status == "failed"
    assert len(result.issues) == 1 and "Not a class" in result.issues[0].message


# --- 10: the merge command keeps the named files ---


def test_validate_command_keeps_named_files_with_a_sub():
    command = file_inputs.validate_command(None, {"manuscript": "b.pdf"}, subfile="r.sub.txt")
    assert command == "paperpush validate r.sub.txt --manuscript b.pdf"


def test_cli_ai_tasks_merge_command_keeps_named_files(tmp_path):
    a, b = _pdf(tmp_path, "a.pdf"), _pdf(tmp_path, "b.pdf")
    sub = tmp_path / "r.sub.txt"
    sub.write_text(f"@venue: biorxiv\nmanuscript_file: {a}\n")
    tasks = tmp_path / "t.json"
    main(["validate", str(sub), "--manuscript", str(b), "--dont-check-links", "--dont-check-references", "--dont-check-for-sensitive-info", "--dont-check-hidden-text", "--ai-tasks", str(tasks)])
    assert f"--manuscript {b}" in json.loads(tasks.read_text())["instructions"]


# --- 11: nothing measured is not a pass ---


def test_manuscript_length_with_a_missing_file_is_unchecked(tmp_path):
    report = _run("bioinformatics", {"manuscript_file": str(tmp_path / "nope.pdf"), "article_type": "Original Paper"})
    assert _check(report, "files.manuscript_length").status == "unchecked"


def test_manuscript_length_with_no_resolved_limit_is_not_applicable(tmp_path):
    report = _run("bioinformatics", {"manuscript_file": str(_pdf(tmp_path))})
    assert _check(report, "files.manuscript_length").status == "not_applicable"


def test_total_size_with_nothing_attached_is_not_applicable():
    assert _check(_run("arxiv", {}), "files.total_size").status == "not_applicable"


def test_total_size_with_only_missing_files_is_unchecked(tmp_path):
    assert _check(_run("arxiv", {"manuscript_file": str(tmp_path / "nope.pdf")}), "files.total_size").status == "unchecked"


def test_total_size_with_some_missing_files_is_unchecked(tmp_path):
    venue = Venue(slug="sizej", name="T", max_upload_mb=5, fields=[Field(id="a", label="A", type="file"), Field(id="b", label="B", type="file")])
    assert _check(_run(venue, {"a": str(_pdf(tmp_path)), "b": str(tmp_path / "nope.pdf")}), "files.total_size").status == "unchecked"


# --- 12: guideline notes reach the right files ---


def test_title_page_notes_read_the_title_page_upload(tmp_path):
    manuscript, title_page = _pdf(tmp_path, "m.pdf"), _pdf(tmp_path, "t.pdf")
    tasks = ai_validate.guideline_tasks(get_venue("radiology"), {"manuscript_file": str(manuscript), "title_page_file": str(title_page)})
    assert str(title_page) in next(t for t in tasks if t.id == "ai.notes.title_page").files


def test_upload_notes_become_a_task(tmp_path):
    manuscript = _pdf(tmp_path, "m.pdf")
    tasks = ai_validate.guideline_tasks(get_venue("jama"), {"manuscript_file": str(manuscript)})
    assert next(t for t in tasks if t.id == "ai.notes.upload").files == [str(manuscript)]


# --- 13: a PDF with no pages ---


def test_zero_page_pdf_is_flagged(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_metadata({"/Title": "x" * 3000})
    path = tmp_path / "zero.pdf"
    writer.write(str(path))
    result = _check(_run("biorxiv", {"manuscript_file": str(path)}), "files.uploads")
    assert any("could not detect any pages" in i.message for i in result.issues)


def test_pdf_with_pages_is_not_flagged(tmp_path):
    result = _check(_run("biorxiv", {"manuscript_file": str(_pdf(tmp_path))}), "files.uploads")
    assert not any("pages" in i.message for i in result.issues)


# --- 14: multi-part extensions ---


def test_tar_gz_is_an_accepted_extension(tmp_path):
    path = tmp_path / "src.tar.gz"
    path.write_bytes(b"\x1f\x8b" * 100)
    assert _check(_run("arxiv", {"manuscript_file": str(path)}), "files.uploads").status == "passed"


def test_filematch_places_a_tar_gz():
    assert filematch.match_files(get_venue("arxiv"), ["paper.tar.gz"]).assignments == {"manuscript_file": "paper.tar.gz"}


# --- 15: file names ---


@pytest.mark.parametrize("name, kind", [("FigS1.tif", "supplement"), ("Figure_S2.png", "supplement"), ("S3_Fig.tif", "supplement"), ("Table_S1.xlsx", "supp_table"), ("Figure1.tif", "figure"), ("Figures1.pdf", "figure")])
def test_numbered_supplementary_items(name, kind):
    assert filematch.kinds_of(name)[0] == kind


@pytest.mark.parametrize("name", ["better_results.pdf", "stable_data.xlsx"])
def test_everyday_words_are_not_fuzzy_matches(name):
    assert filematch.kinds_of(name) == []


def test_title_page_is_not_taken_for_the_manuscript():
    assert "manuscript_file" not in filematch.match_files(get_venue("nature"), ["title_page.docx", "Figure1.tif"]).assignments


def test_title_page_goes_in_a_title_page_upload():
    a = filematch.match_files(get_venue("radiology"), ["paper.docx", "title_page.docx"]).assignments
    assert a["title_page_file"] == "title_page.docx" and a["manuscript_file"] == "paper.docx"


# --- the lower-severity report and AI-merge defects ---


def test_ai_results_without_manuscript_checks_are_refused(tmp_path):
    with pytest.raises(ValueError, match="check_manuscript"):
        _run("biorxiv", {}, ai=ai_validate.AiOptions.from_results({"results": []}, "r.json"))


def test_cli_refuses_ai_results_without_manuscript_checks(tmp_path, capsys):
    sub = tmp_path / "biorxiv.sub.txt"
    sub.write_text("@venue: biorxiv\n")
    results = tmp_path / "r.json"
    results.write_text('{"results": []}')
    assert main(["validate", str(sub), "--dont-check-manuscript", "--ai-results", str(results)]) == 1
    assert "--disable-manuscript" in capsys.readouterr().err


def test_files_only_initial_submission_is_no_readiness_verdict(tmp_path):
    report = _run("biorxiv", {"manuscript_file": str(_pdf(tmp_path))}, files_only=True, initial_submission=True)
    verdict = next(line.text for line in _layout(report) if line.role == "summary")
    assert "Ready to submit" not in verdict


def test_json_keeps_the_unchecked_marker():
    result = CheckResult("a", "A", "x", "warning", issues=[Issue("warning", "", "real"), unchecked("f", "could not read")])
    assert result.to_dict()["issues"] == [{"level": "warning", "field": "", "message": "real"}, {"level": "warning", "field": "f", "message": "could not read", "unchecked": True}]


def test_mcp_issue_dicts_keep_the_unchecked_marker():
    assert mcp_server._issue_dicts([unchecked("f", "m")]) == [{"level": "warning", "field": "f", "message": "m", "unchecked": True}]


def test_ai_only_report_gives_no_verdict_in_json():
    assert ValidationReport(venue="v", ai_only=True).to_dict()["ok"] is None
    assert ValidationReport(venue="v").to_dict()["ok"] is True


def test_api_answer_missing_a_task_says_so(tmp_path, monkeypatch):
    checks = validate_mod.Checks()
    path = str(_pdf(tmp_path))
    checks.needs_ai(ai_validate.AiTask("ai.a", "A", "x", "i", [path]))
    checks.needs_ai(ai_validate.AiTask("ai.b", "B", "x", "i", [path]))
    monkeypatch.setattr(ai_validate, "run_api", lambda *a, **k: {"ai.a": {"status": "pass", "details": "ok"}})
    ai_validate.resolve(checks, Venue(slug="j", name="J", fields=[]), {}, ai_validate.AiOptions.api("anthropic", "m"))
    result = next(r for r in checks.results if r.id == "ai.b")
    assert result.status == "needs_ai" and "no verdict" in result.detail and "--ai" not in result.detail


def test_api_with_no_tasks_is_not_reported_as_off():
    ai = ai_validate.AiOptions.api("anthropic", "m")
    ai_validate.resolve(validate_mod.Checks(), Venue(slug="j", name="J", fields=[]), {}, ai)
    assert ai.description


def _fake_provider(monkeypatch, answer):
    autofill = importlib.import_module("paperpush.autofill")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    seen = {}

    def fake(model, documents, instructions, schema, max_tokens, system="", api_key=None):
        seen["documents"] = [d.path.name for d in documents]
        seen["instructions"] = instructions
        return json.dumps(answer)

    monkeypatch.setitem(autofill._CALLERS, "anthropic", fake)
    return seen


def test_archive_is_not_sent_to_the_model(tmp_path, monkeypatch):
    archive = tmp_path / "src.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("main.tex", "hello")
    seen = _fake_provider(monkeypatch, {"results": []})
    pdf = str(_pdf(tmp_path))
    tasks = [ai_validate.AiTask("ai.zip", "Z", "x", "i", [str(archive)]), ai_validate.AiTask("ai.both", "B", "x", "i", [pdf, str(archive)])]
    verdicts = ai_validate.run_api(tasks, get_venue("biorxiv"), ai_validate.AiOptions.api("anthropic", "m"))
    assert verdicts["ai.zip"]["status"] == "unclear"
    assert seen["documents"] == ["paper.pdf"]
    assert "src.zip" in seen["instructions"]


def test_only_archives_makes_no_api_call(tmp_path, monkeypatch):
    archive = tmp_path / "src.zip"
    archive.write_bytes(b"PK\x03\x04\x00")
    seen = _fake_provider(monkeypatch, {"results": []})
    verdicts = ai_validate.run_api([ai_validate.AiTask("ai.zip", "Z", "x", "i", [str(archive)])], get_venue("biorxiv"), ai_validate.AiOptions.api("anthropic", "m"))
    assert verdicts["ai.zip"]["status"] == "unclear" and not seen


@pytest.mark.parametrize("bad", [{"foo": [{"id": "x", "status": "pass"}]}, [1, 2], {"x": ["pass"]}, {"results": [{"status": "pass"}]}])
def test_malformed_results_are_refused(bad):
    with pytest.raises(ValueError):
        ai_validate.parse_results(bad)


def test_empty_results_are_allowed():
    assert ai_validate.parse_results({"results": []}) == {}


def test_output_path_with_no_subfile():
    assert output_path("", "").name == "paperpush_validation.pdf"


def test_url_with_a_space_is_rejected():
    assert _check(_run("biorxiv", {"data_availability_links": "https://exa mple.com/x"}), "form.urls").status == "failed"


def test_mcp_ai_tasks_can_include_the_reference_lookup(tmp_path, monkeypatch):
    seen = {}
    pdf = _pdf(tmp_path)

    def fake(sub, venue, **flags):
        seen.update(flags)
        return ValidationReport(venue=venue.slug)

    monkeypatch.setattr(mcp_server, "_run_validate_report", fake)
    mcp_server.validation_ai_tasks(venue="biorxiv", manuscript=str(pdf))
    assert seen["check_references"] is False
    mcp_server.validation_ai_tasks(venue="biorxiv", manuscript=str(pdf), check_references=True)
    assert seen["check_references"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_web_counts_a_comma_option_as_one():
    checks_js = Path(file_inputs.__file__).parent / "web" / "static" / "checks.js"
    script = "const c = require(process.argv[1]); console.log(c.countItems({type: 'multichoice', options: ['A (b, c)', 'D']}, 'A (b, c), D, E'), c.isHttpUrl('https://exa mple.com/x'));"
    out = subprocess.run(["node", "-e", script, str(checks_js)], capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["3", "false"]


def test_submit_runners_keep_options_that_contain_commas():
    """The portal runners read multichoice values the way validate does."""
    from paperpush.database import get_venue
    from paperpush.validate import _multichoice_items
    from paperpush.venues.scholarone import bioinformatics, nucleic_acids_research

    for module in (bioinformatics, nucleic_acids_research):
        assert module._multichoice_items is _multichoice_items
    field = next(f for f in get_venue("bioinformatics").fields if f.id == "portal_keywords")
    option = next(o for o in field.options if "," in o)
    assert _multichoice_items(field, f"{option}, not a listed option") == [option, "not a listed option"]
