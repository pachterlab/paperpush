"""``--disable-network-checks``: validate makes no network request, from the CLI, the web page, and MCP.

``urllib.request.urlopen`` and the anonymity fetcher are replaced with functions
that fail the test if called, so any request that slips through is caught.
"""

from __future__ import annotations

import urllib.request

import pytest

from paperpush import anonymity, report
from paperpush.cli import main
from paperpush.database import Field, Venue
from paperpush.subfile import SubFile
from paperpush.validate import validate_report

AUTHORS = Field(id="authors", label="Authors", type="authorlist")
PDF = Field(id="pdf_file", label="PDF", type="file")
NETWORK_IDS = ("integrity.links", "integrity.dois", "integrity.unmatched_references", "integrity.retractions")


@pytest.fixture
def no_requests(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError(f"network request with the network checks disabled: {a[:1]}")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(anonymity, "_http_get", refuse)


def _sub(tmp_path) -> tuple[SubFile, Venue]:
    tex = tmp_path / "main.tex"
    tex.write_text(
        "\\title{A paper}\nCode: https://github.com/adalovelace/engine. Data: https://doi.org/10.5281/zenodo.111.\n"
        "Project page: https://engine.example.org/.\n\\begin{thebibliography}{1}\n\\bibitem{a} A. Author. A title. doi:10.1234/abc.\n\\end{thebibliography}\n",
        encoding="utf-8",
    )
    venue = Venue(slug="testj", name="Test", fields=[AUTHORS, PDF], anonymous=True)
    return SubFile(venue="testj", values={"authors": "Ada Lovelace | ada@example.org | Engine Institute |  | yes", "pdf_file": str(tex)}), venue


def test_validate_without_network_makes_no_request_and_says_so(tmp_path, no_requests):
    sub, venue = _sub(tmp_path)
    result = validate_report(sub, venue, network=False, check_manuscript=False)
    by_id = {c.id: c for c in result.results}
    for check_id in NETWORK_IDS:
        assert by_id[check_id].status == report.UNCHECKED, check_id
        assert "network checks disabled (--disable-network-checks)" in by_id[check_id].detail
    # The offline part of the anonymity check still runs.
    anon = " ".join(i.message for i in by_id["integrity.anonymity"].issues)
    assert "adalovelace" in anon
    assert "zenodo" not in anon.lower()


def test_validate_without_network_refuses_the_outside_scans(tmp_path):
    sub, venue = _sub(tmp_path)
    with pytest.raises(ValueError, match="network checks disabled"):
        validate_report(sub, venue, network=False, plagiarism=object())


def test_cli_flag_passes_network_false(tmp_path, monkeypatch):
    seen = {}

    def fake_report(subfile, venue_def, subfile_path, **kwargs):
        seen["network"] = kwargs.get("network")
        raise SystemExit(0)

    sub = tmp_path / "nature.sub.txt"
    assert main(["subfile", "nature", "-o", str(sub)]) == 0
    monkeypatch.setattr("paperpush.cli.validate_report_for", fake_report)
    with pytest.raises(SystemExit):
        main(["validate", str(sub), "--disable-network-checks"])
    assert seen == {"network": False}
    with pytest.raises(SystemExit):
        main(["validate", str(sub)])
    assert seen == {"network": True}


@pytest.mark.parametrize("extra", [["--plagiarism", "--plagiarism-api-key", "k"], ["--issn", "1234-5678"], ["--reproduce-figures"]])
def test_cli_flag_refuses_options_that_need_the_network(tmp_path, capsys, extra):
    sub = tmp_path / "nature.sub.txt"
    assert main(["subfile", "nature", "-o", str(sub)]) == 0
    assert main(["validate", str(sub), "--disable-network-checks", *extra]) == 1
    assert "cannot be combined with --disable-network-checks" in capsys.readouterr().err


def test_cli_flag_is_not_read_as_a_check_name(tmp_path, monkeypatch):
    # --disable-NAME flags become --disable=NAME; this one must stay itself.
    from paperpush.cli import _expand_check_flags

    assert _expand_check_flags(["validate", "x.sub.txt", "--disable-network-checks"]) == ["validate", "x.sub.txt", "--disable-network-checks"]


def test_web_box_turns_the_network_off(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from paperpush.web import app as web_app
    from paperpush.web.app import Settings, create_app

    seen = {}
    real = web_app.validate_report

    def spy(*a, **k):
        seen.update(network=k.get("network"), check_links=k.get("check_links"), check_references=k.get("check_references"))
        return real(*a, **{**k, "network": False})

    monkeypatch.setattr(web_app, "validate_report", spy)
    with TestClient(create_app(Settings(data_dir=tmp_path / "web", network_checks=True))) as client:
        ws = client.post("/api/workspaces").json()["id"]
        text = "@venue: biorxiv\n"
        res = client.post(f"/api/workspaces/{ws}/validate", json={"text": text, "network": False})
        assert res.status_code == 200, res.text
        assert res.json()["network_checks"] is False
        assert seen == {"network": False, "check_links": False, "check_references": False}
        res = client.post(f"/api/workspaces/{ws}/validate", json={"text": text, "network": False, "plagiarism": True}, headers={"X-Plagiarism-Key": "k"})
        assert res.status_code == 400 and "disable network checks" in res.json()["detail"]


def test_web_page_has_the_box():
    from pathlib import Path

    static = Path(__file__).parents[1] / "paperpush" / "web" / "static"
    assert 'id="no-network-check"' in (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert "network: !state.noNetwork" in js and "wireNoNetwork();" in js
