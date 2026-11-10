"""Code availability, data accessions, figure provenance, and opt-in figure reproduction."""

from __future__ import annotations

import zipfile

import pytest

from paperpush import report
from paperpush import reproducibility_check as rc
from paperpush.database import Field, Venue
from paperpush.subfile import SubFile
from paperpush.validate import Checks, validate_report

VENUE = Venue(
    slug="repro_test",
    name="Repro Test",
    fields=[Field(id="manuscript_file", label="Manuscript", type="file"), Field(id="supplementary_files", label="Supplementary", type="filelist"), Field(id="figure_files", label="Figures", type="filelist")],
)

_PAPER = """A study

Introduction

We wrote Python scripts and ran simulations in Jupyter notebooks (Fig. 1a, Fig. 2b).

Figure 1. Overview of the simulations. a, the model.

Figure 2. UMAP of the cells. b, expression of GENE1.

{statements}

References

1. Doe J. A tool we used. https://github.com/someone/their-tool (2020).
"""


def _values(tmp_path, statements: str = "", **extra) -> dict[str, str]:
    path = tmp_path / "paper.md"
    path.write_text(_PAPER.format(statements=statements), encoding="utf-8")
    return {"manuscript_file": str(path), **extra}


def _run(values, **options):
    checks = Checks(options={"check_repositories": False, **options})
    rc.run_checks(checks, VENUE, values)
    return {r.id: r for r in checks.results}, checks.ai_tasks


def _messages(result) -> str:
    return " | ".join(i.message for i in result.issues)


# --- reading the statements -----------------------------------------------------


def test_statements_stop_at_the_next_heading():
    text = "Code availability\nThe code is at github.com/a/b.\n\nData availability\nIn GEO.\n\nAcknowledgements\nThanks."
    assert rc.statements(text) == ["Code availability: The code is at github.com/a/b.", "Data availability: In GEO."]


def test_repositories_in_the_reference_list_are_not_the_papers_code(tmp_path):
    src = rc.code_sources(VENUE, _values(tmp_path))
    assert src.repos == [] and not src.any()


def test_the_code_statement_names_the_papers_repository(tmp_path):
    body = "Methods\nReads were aligned with https://github.com/other/tool.\n\nCode availability\nCode: https://github.com/lab/paper-code."
    src = rc.code_sources(VENUE, _values(tmp_path, body))
    assert src.github == [("lab", "paper-code")]


# --- tier 1 ---------------------------------------------------------------------


def test_computational_paper_without_code_is_warned(tmp_path):
    results, tasks = _run(_values(tmp_path))
    result = results["rigor.code_availability"]
    assert result.status == report.WARNING
    assert "links no code repository" in _messages(result)
    assert tasks == []


def test_code_only_on_request_is_warned(tmp_path):
    results, _ = _run(_values(tmp_path, "Code availability\nThe analysis code is available from the corresponding author upon reasonable request."))
    assert "offered only on request" in _messages(results["rigor.code_availability"])


def test_data_on_request_with_a_reason_is_accepted(tmp_path):
    body = "Code availability\nhttps://doi.org/10.5281/zenodo.1234567\n\nData availability\nPatient data are available on request because of consent restrictions."
    results, _ = _run(_values(tmp_path, body))
    assert results["rigor.code_availability"].status == report.PASSED


def test_data_on_request_without_a_reason_is_warned(tmp_path):
    body = "Code availability\nhttps://doi.org/10.5281/zenodo.1234567\n\nData availability\nData are available upon request."
    results, _ = _run(_values(tmp_path, body))
    assert "data are offered only on request" in _messages(results["rigor.code_availability"])


def test_unarchived_repository_is_warned_and_pinned_one_is_not(tmp_path):
    results, _ = _run(_values(tmp_path, "Code availability\nhttps://github.com/lab/paper-code"))
    assert "can change after publication" in _messages(results["rigor.code_availability"])
    results, _ = _run(_values(tmp_path, "Code availability\nhttps://github.com/lab/paper-code/releases/tag/v1.0.2"))
    assert results["rigor.code_availability"].status == report.PASSED


def test_non_computational_paper_is_not_applicable(tmp_path):
    path = tmp_path / "essay.md"
    path.write_text("An essay\n\nIntroduction\n\nWe discuss the history of the field.\n", encoding="utf-8")
    results, _ = _run({"manuscript_file": str(path)})
    assert results["rigor.code_availability"].status == report.NOT_APPLICABLE
    assert results["rigor.data_accessions"].status == report.NOT_APPLICABLE


def test_attached_code_archive_counts_as_code(tmp_path):
    archive = tmp_path / "code.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("analysis/fig1.py", "print('hi')\n")
    latex = tmp_path / "source.zip"
    with zipfile.ZipFile(latex, "w") as zf:
        zf.writestr("main.tex", "\\documentclass{article}")
    src = rc.code_sources(VENUE, _values(tmp_path, supplementary_files=f"{archive}\n{latex}"))
    assert src.attached == [archive]


def test_github_repository_contents_are_inspected(tmp_path, monkeypatch):
    def fake(url, headers=None):
        if url.endswith("/repos/lab/paper-code"):
            return 200, {"license": None, "default_branch": "main"}
        if "/git/trees/main" in url:
            return 200, {"tree": [{"type": "blob", "path": "fig1.py"}], "truncated": False}
        return 404, None

    monkeypatch.setattr(rc, "_get_json", fake)
    results, _ = _run(_values(tmp_path, "Code availability\nhttps://github.com/lab/paper-code (archived at 10.5281/zenodo.42)"), check_repositories=True)
    messages = _messages(results["rigor.code_availability"])
    assert "no license" in messages and "no README" in messages and "declares no software environment" in messages
    assert "holds no source code" not in messages


def test_github_rate_limit_is_unchecked_not_passed(tmp_path):
    results, _ = _run(_values(tmp_path, "Code availability\nhttps://github.com/lab/paper-code (10.5281/zenodo.42)"), check_repositories=True)
    result = results["rigor.code_availability"]
    assert any(i.unchecked for i in result.issues) and "GITHUB_TOKEN" in _messages(result)


@pytest.mark.parametrize("count, expected", [("1", report.PASSED), ("0", report.WARNING)])
def test_geo_accessions_are_looked_up(tmp_path, monkeypatch, count, expected):
    monkeypatch.setattr(rc, "_get_json", lambda url, headers=None: (200, {"esearchresult": {"count": count}}))
    results, _ = _run(_values(tmp_path, "Data availability\nReads are in GEO under GSE123456."), check_repositories=True)
    assert results["rigor.data_accessions"].status == expected
    if expected == report.WARNING:
        assert "access token" in _messages(results["rigor.data_accessions"])


# --- tiers 2 and 3 ----------------------------------------------------------------


def test_provenance_task_is_queued_by_default_and_reproduction_is_not(tmp_path):
    _, tasks = _run(_values(tmp_path, "Code availability\nhttps://github.com/lab/paper-code"))
    assert [t.id for t in tasks] == ["ai.rigor.figure_provenance"]
    assert "github.com/lab/paper-code" in tasks[0].instructions and "Do not run anything" in tasks[0].instructions


def test_reproduce_figures_queues_one_task_per_figure(tmp_path):
    figure = tmp_path / "Figure2.png"
    figure.write_bytes(b"\x89PNG")
    values = _values(tmp_path, "Code availability\nhttps://github.com/lab/paper-code", figure_files=str(figure))
    _, tasks = _run(values, reproduce_figures=True, reproduce_minutes=10, reproduce_download_gb=2)
    by_id = {t.id: t for t in tasks}
    assert set(by_id) == {"ai.rigor.figure_provenance", "ai.figure_reproduction.1", "ai.figure_reproduction.2"}
    two = by_id["ai.figure_reproduction.2"]
    assert str(figure) in two.files and "UMAP of the cells" in two.instructions
    assert "10 minutes" in two.instructions and "2 GB" in two.instructions
    assert "'not attempted' (report unclear" in two.instructions


def test_reproduce_figures_without_code_says_why(tmp_path):
    results, tasks = _run(_values(tmp_path), reproduce_figures=True)
    assert tasks == []
    assert results["ai.figure_reproduction.all"].status == report.UNCHECKED


def test_reproduce_flag_reaches_validate_report_and_refuses_the_api(tmp_path):
    from paperpush.ai_validate import AiOptions

    sub = SubFile(venue=VENUE.slug, values=_values(tmp_path, "Code availability\nhttps://github.com/lab/paper-code"))
    flags = dict(check_links=False, check_references=False, check_sensitive=False, check_openreview=False)
    result = validate_report(sub, VENUE, reproduce_figures=True, **flags)
    assert {"ai.figure_reproduction.1", "ai.figure_reproduction.2"} <= {t.id for t in result.ai_tasks}
    only = validate_report(sub, VENUE, reproduce_figures=True, ai_only=True, **flags)
    assert "ai.figure_reproduction.1" in {t.id for t in only.ai_tasks}
    with pytest.raises(ValueError, match="cannot"):
        validate_report(sub, VENUE, reproduce_figures=True, ai=AiOptions.api(), **flags)
