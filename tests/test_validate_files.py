"""``paperpush validate`` on files named on the command line, with or without a .sub or venue."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperpush import mcp_server
from paperpush.cli import main
from paperpush.database import GENERIC_SLUG, default_form, generic_venue, get_venue
from paperpush.file_inputs import apply_files, validate_command
from paperpush.subfile import SubFile
from paperpush.validate import validate_report

OFFLINE = ["--dont-check-links", "--dont-check-references"]


def _status(report, check_id: str) -> str:
    return next(r.status for r in report.results if r.id == check_id)


# --- the generic venue and the title-page upload ---------------------------------


def test_generic_venue_has_every_upload_role():
    ids = {f.id for f in generic_venue().fields}
    assert {"manuscript_file", "title_page_file", "cover_letter", "figure_files", "table_files", "supplementary_files"} <= ids
    assert generic_venue().is_generic and not get_venue("nature").is_generic


def test_default_form_has_a_title_page_upload_only_when_the_guidelines_ask():
    assert "title_page_file" in {f["id"] for f in default_form("radiology")["fields"]}
    assert "title_page_file" not in {f["id"] for f in default_form("ieee_tmi")["fields"]}


# --- placing the files -------------------------------------------------------------


def test_apply_files_fills_the_role_fields_with_absolute_paths(tmp_path):
    placed = apply_files(generic_venue(), {}, {"manuscript": "paper.pdf", "figures": ["f1.png", "f2.png"], "title_page": "tp.pdf"}, base=tmp_path)
    assert placed.values["manuscript_file"] == str(tmp_path / "paper.pdf")
    assert placed.values["figure_files"] == f"{tmp_path / 'f1.png'}\n{tmp_path / 'f2.png'}"
    assert placed.values["title_page_file"] == str(tmp_path / "tp.pdf")
    assert placed.notes == []


def test_apply_files_adds_an_upload_the_venue_lacks_and_says_so(tmp_path):
    venue = get_venue("nature")
    assert "title_page_file" not in {f.id for f in venue.fields}
    placed = apply_files(venue, {}, {"title_page": "tp.pdf"}, base=tmp_path)
    assert "title_page_file" in {f.id for f in placed.venue.fields}
    assert placed.values["title_page_file"] == str(tmp_path / "tp.pdf")
    assert any("no title page upload" in n for n in placed.notes)


def test_apply_files_replaces_the_sub_value_and_notes_it(tmp_path):
    placed = apply_files(generic_venue(), {"manuscript_file": "/old.pdf"}, {"manuscript": "new.pdf"}, base=tmp_path)
    assert placed.values["manuscript_file"] == str(tmp_path / "new.pdf")
    assert placed.notes == ["--manuscript replaces the .sub's manuscript_file"]


def test_apply_files_refuses_several_files_for_a_single_upload(tmp_path):
    with pytest.raises(ValueError, match="--manuscript takes one file"):
        apply_files(generic_venue(), {}, {"manuscript": ["a.pdf", "b.pdf"]}, base=tmp_path)


def test_validate_command_rebuilds_the_flags():
    assert validate_command("nature", {"manuscript": "m.pdf", "figures": ["a b.png"]}) == "paperpush validate --venue nature --manuscript m.pdf --figures 'a b.png'"
    assert validate_command(None, {"cover_letter": "c.pdf"}) == "paperpush validate --cover-letter c.pdf"


# --- the report ----------------------------------------------------------------------


def _files_report(venue, files, base):
    placed = apply_files(venue, {}, files, base=base)
    return validate_report(SubFile(venue=venue.slug, values=placed.values), placed.venue, check_links=False, check_references=False, files_only=True, notes=placed.notes)


def test_no_venue_runs_only_the_general_checks(manuscript_pdf, tmp_path):
    report = _files_report(generic_venue(), {"manuscript": str(manuscript_pdf)}, tmp_path)
    assert report.scope == "general"
    assert report.files == [str(manuscript_pdf)]
    # No .sub: empty title/abstract/authors are not errors.
    assert _status(report, "form") == "not_applicable"
    assert not any(r.id.startswith("form.") for r in report.results)
    assert _status(report, "files.uploads") == "passed"
    guidelines = next(r for r in report.results if r.id == "guidelines")
    assert guidelines.status == "not_applicable" and "--venue" in guidelines.detail
    # The general checks still run.
    assert _status(report, "integrity.hidden_text") == "passed"
    assert any(r.id.startswith("prose.") and r.status != "not_applicable" for r in report.results)


def test_no_venue_does_not_check_which_venue_a_cover_letter_names(cover_letter_pdf, tmp_path):
    report = _files_report(generic_venue(), {"cover_letter": str(cover_letter_pdf)}, tmp_path)
    assert _status(report, "cover_letter.venue") == "not_applicable"


def test_files_for_a_venue_read_its_guidelines_but_not_its_form(manuscript_pdf, tmp_path):
    report = _files_report(get_venue("radiology"), {"manuscript": str(manuscript_pdf)}, tmp_path)
    assert report.scope == "files"
    assert _status(report, "form") == "not_applicable"
    # Radiology asks for a separate title page, and none was named.
    separate = next(r for r in report.results if r.id == "guidelines.title_page_file")
    assert separate.status == "warning"


def test_a_separate_title_page_satisfies_the_venue(manuscript_pdf, title_page_pdf, tmp_path):
    report = _files_report(get_venue("radiology"), {"manuscript": str(manuscript_pdf), "title_page": str(title_page_pdf)}, tmp_path)
    assert _status(report, "guidelines.title_page_file") == "passed"


# --- the CLI -------------------------------------------------------------------------


def test_cli_validates_files_without_a_sub_or_venue(manuscript_pdf, capsys):
    assert main(["validate", "--manuscript", str(manuscript_pdf), *OFFLINE]) == 0
    out = capsys.readouterr().out
    assert "Venue: none (general checks only)" in out
    assert "passed the general checks" in out


def test_cli_needs_a_sub_or_files(capsys):
    assert main(["validate"]) == 1
    assert "give a .sub file, or the files to check" in capsys.readouterr().err


def test_cli_rejects_a_venue_that_contradicts_the_sub(tmp_path, capsys):
    sub = tmp_path / "nature.sub.txt"
    assert main(["subfile", "nature", "-o", str(sub)]) == 0
    assert main(["validate", str(sub), "--venue", "biorxiv"]) == 1
    assert "is for nature, not biorxiv" in capsys.readouterr().err


def test_cli_takes_back_a_sub_swallowed_by_a_file_list(tmp_path, fig1_png, monkeypatch):
    seen = {}

    def fake_report(subfile, venue_def, subfile_path, **kwargs):
        seen.update(path=subfile_path, figures=subfile.values.get("figure_files"), files_only=kwargs["files_only"])
        raise SystemExit(0)

    sub = tmp_path / "nature.sub.txt"
    assert main(["subfile", "nature", "-o", str(sub)]) == 0
    monkeypatch.setattr("paperpush.cli.validate_report_for", fake_report)
    with pytest.raises(SystemExit):
        main(["validate", "--figures", str(fig1_png), str(sub)])
    assert seen == {"path": str(sub), "figures": str(fig1_png), "files_only": False}


def test_cli_saves_the_report_next_to_the_manuscript(manuscript_pdf, capsys):
    assert main(["validate", "--manuscript", str(manuscript_pdf), "-o", "", *OFFLINE]) == 0
    assert (manuscript_pdf.parent / f"{manuscript_pdf.stem}_validation.pdf").is_file()


def test_cli_web_needs_a_sub(manuscript_pdf, capsys):
    assert main(["validate", "--manuscript", str(manuscript_pdf), "--web"]) == 1
    assert "--web needs a .sub file" in capsys.readouterr().err


# --- the MCP tool --------------------------------------------------------------------


def test_mcp_validate_takes_files_relative_to_the_manuscript_dir(manuscript_pdf):
    result = mcp_server.validate_subfile(manuscript_dir=str(manuscript_pdf.parent), manuscript=manuscript_pdf.name, check_links=False, check_references=False)
    assert result["scope"] == "general" and result["venue"] == GENERIC_SLUG
    assert result["files"] == [str(manuscript_pdf)]


def test_mcp_validate_needs_a_sub_or_files():
    with pytest.raises(ValueError, match="pass a .sub file, or the files to check"):
        mcp_server.validate_subfile()


def test_mcp_ai_tasks_for_files_name_the_command_to_merge_them(manuscript_pdf):
    payload = mcp_server.validation_ai_tasks(venue="nature", manuscript=str(manuscript_pdf))
    assert f"paperpush validate --venue nature --manuscript {manuscript_pdf}" in payload["instructions"]
