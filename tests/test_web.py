"""Tests for the website (:mod:`paperpush.web`).

The API tests drive the FastAPI app in-process with its TestClient. The
JavaScript tests run ``static/subfile.js`` and ``static/checks.js`` under Node
and compare them with :mod:`paperpush.subfile` and :mod:`paperpush.validate`,
since the page and the server must read a ``.sub`` -- and judge its form
fields -- the same way; they are skipped when Node is not installed.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from paperpush.database import get_venue, list_venues
from paperpush.subfile import default_filename, parse, render_template, replace_block, replace_scalar, subfile_stem
from paperpush.validate import validate_report
from paperpush.web.app import LIVE_CHECKS, Settings, create_app, live_rules, localize_values, safe_name, venue_schema_issues

VENUE = "biorxiv"
STATIC = Path(__file__).parents[1] / "paperpush" / "web" / "static"
SUBFILE_JS = STATIC / "subfile.js"
CHECKS_JS = STATIC / "checks.js"
MANUSCRIPT = Path(__file__).parent / "manuscript_files" / "biorxiv" / "manuscript.pdf"


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path / "web", network_checks=False)
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def ws(client):
    return client.post("/api/workspaces").json()["id"]


def _filled_template(**values: str) -> str:
    return render_template(get_venue(VENUE), fill_defaults=True, values=values)


# --- venues ----------------------------------------------------------------


def test_index_and_static_assets_are_served(client):
    assert "paperpush" in client.get("/").text
    for asset in ("app.js", "subfile.js", "checks.js", "viewer.js", "style.css"):
        assert client.get(f"/static/{asset}").status_code == 200


def test_venue_list_matches_the_database(client):
    assert [v["slug"] for v in client.get("/api/venues").json()] == [v.slug for v in list_venues()]


def test_venue_detail_carries_template_and_field_definitions(client):
    detail = client.get(f"/api/venues/{VENUE}").json()
    assert detail["template"] == render_template(get_venue(VENUE), fill_defaults=True)
    authors = next(f for f in detail["fields"] if f["id"] == "authors")
    assert authors["fields"] and authors["role"]
    assert detail["subfile_name"] == f"{VENUE}.sub.txt"


def test_unknown_venue_is_404(client):
    assert client.get("/api/venues/not-a-venue").status_code == 404


def test_hierarchical_options_are_browsable(client):
    top = client.get("/api/venues/nature/fields/subject_level/options").json()
    assert top["hierarchical"] and top["options"]
    child = client.get(
        "/api/venues/nature/fields/subject_level/options",
        params={"path": top["options"][0]},
    ).json()
    assert child["options"]


# --- workspaces ------------------------------------------------------------


def test_unknown_or_malformed_workspace_is_404(client):
    assert client.get("/api/workspaces/doesnotexist0000000000").status_code == 404
    assert client.get("/api/workspaces/..%2F..%2Fetc").status_code == 404


def test_upload_list_and_delete(client, ws):
    res = client.post(
        f"/api/workspaces/{ws}/files",
        files=[("files", ("paper.pdf", b"%PDF-1.4 x", "application/pdf"))],
    )
    assert res.json()["saved"] == ["paper.pdf"]
    assert [f["name"] for f in client.get(f"/api/workspaces/{ws}").json()["files"]] == ["paper.pdf"]
    assert client.delete(f"/api/workspaces/{ws}/files/paper.pdf").json()["files"] == []


def test_upload_names_cannot_escape_the_workspace(client, ws, tmp_path):
    res = client.post(
        f"/api/workspaces/{ws}/files",
        files=[("files", ("../../evil|x.pdf", b"x", "application/pdf"))],
    )
    assert res.json()["saved"] == ["evil_x.pdf"]
    assert not (tmp_path / "evil_x.pdf").exists()


def test_safe_name_rejects_names_with_nothing_left():
    assert safe_name("a/b\\c.pdf") == "c.pdf"
    with pytest.raises(Exception):
        safe_name("../..")


def test_upload_over_the_workspace_cap_is_refused(tmp_path):
    settings = Settings(data_dir=tmp_path / "web", max_workspace_mb=0.001, network_checks=False)
    with TestClient(create_app(settings)) as c:
        ws_id = c.post("/api/workspaces").json()["id"]
        res = c.post(
            f"/api/workspaces/{ws_id}/files",
            files=[("files", ("big.bin", b"x" * 5000, "application/octet-stream"))],
        )
        assert res.status_code == 413
        assert c.get(f"/api/workspaces/{ws_id}").json()["files"] == []


def test_stale_workspaces_are_swept(tmp_path):
    settings = Settings(data_dir=tmp_path / "web", ttl_hours=0, network_checks=False)
    with TestClient(create_app(settings)) as c:
        first = c.post("/api/workspaces").json()["id"]
        c.post("/api/workspaces")
        assert c.get(f"/api/workspaces/{first}").status_code == 404


# --- validation ------------------------------------------------------------


def test_localize_values_pins_every_upload_path_inside_the_workspace(tmp_path):
    venue = get_venue(VENUE)
    values = {
        "manuscript_file": "/etc/passwd",
        "figure_files": "../x/fig1.png | Figure 1\nfig2.png",
        "title": "/not/a/path",
    }
    out = localize_values(venue, values, tmp_path)
    assert out["manuscript_file"] == str(tmp_path / "passwd")
    assert out["figure_files"].splitlines() == [
        f"{tmp_path / 'fig1.png'} | Figure 1",
        str(tmp_path / "fig2.png"),
    ]
    assert out["title"] == "/not/a/path"


def _upload_manuscript(client, ws):
    client.post(
        f"/api/workspaces/{ws}/files",
        files=[("files", ("manuscript.pdf", MANUSCRIPT.read_bytes(), "application/pdf"))],
    )


def _checks(res) -> dict[str, dict]:
    return {c["id"]: c for c in res["checks"]}


def test_venue_detail_names_the_manuscript_upload_and_its_formats(client):
    detail = client.get(f"/api/venues/{VENUE}").json()
    assert detail["manuscript_field"] == "manuscript_file"
    assert detail["manuscript_formats"] == next(f.accept for f in get_venue(VENUE).fields if f.id == "manuscript_file")
    assert ".pdf" in detail["manuscript_formats"]
    assert {"has_form", "supports_submit", "login_required"} <= detail.keys()


def test_plan_groups_every_check_without_results(client, ws):
    res = client.post(f"/api/workspaces/{ws}/plan", json={"text": _filled_template()}).json()
    groups = {c["group"] for c in res["checks"]}
    assert groups <= {"live", "validate", "ai", "manual"}
    assert "status" not in res["checks"][0]
    by_id = _checks(res)
    assert by_id["form.required"]["group"] == "live"
    assert by_id["form.schema"]["group"] == "live"
    assert by_id["integrity.links"]["group"] == "validate"
    assert all(c["group"] == "ai" for c in res["checks"] if c["id"].startswith("ai."))


def test_validate_streams_progress_then_the_result(client, ws):
    text = _filled_template()
    plain = client.post(f"/api/workspaces/{ws}/validate", json={"text": text}).json()
    res = client.post(f"/api/workspaces/{ws}/validate", json={"text": text, "stream": True})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in res.text.splitlines() if line.strip()]
    progress = [e["progress"] for e in events[:-1]]
    assert progress and all(set(p) == {"done", "total", "label"} for p in progress)
    assert progress[-1]["label"] == "done" and progress[-1]["done"] == progress[-1]["total"]
    assert [p["done"] for p in progress] == sorted(p["done"] for p in progress)
    assert events[-1]["result"]["counts"] == plain["counts"]


def test_validate_resolves_uploads_and_hides_workspace_paths(client, ws):
    text = _filled_template(manuscript_file="manuscript.pdf")
    res = client.post(f"/api/workspaces/{ws}/validate", json={"text": text}).json()
    uploads = _checks(res)["files.uploads"]
    assert uploads["status"] == "failed"
    assert uploads["issues"][0]["message"] == "file not found: manuscript.pdf"

    _upload_manuscript(client, ws)
    res = client.post(f"/api/workspaces/{ws}/validate", json={"text": text}).json()
    by_id = _checks(res)
    assert by_id["files.uploads"]["status"] == "passed"
    assert ws not in json.dumps(res)
    assert {i["field"] for i in by_id["form.required"]["issues"]} >= {
        "title",
        "authors",
    }
    assert not res["ok"]
    # Network checks are off in these tests, and the note says so in the page's words.
    assert by_id["integrity.links"]["detail"] == "switched off on this server"
    # The text report behind "Download checks", with workspace paths hidden too.
    assert "file not found" not in res["report_text"]
    assert "title" in res["report_text"]
    assert ws not in res["report_text"]


def test_ai_checks_and_autofill_refuse_without_an_api_key(tmp_path, monkeypatch):
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app(Settings(data_dir=tmp_path / "web", network_checks=False))) as client:
        assert client.get("/api/info").json()["ai_provider"] is None
        ws = client.post("/api/workspaces").json()["id"]
        text = _filled_template()
        assert client.post(f"/api/workspaces/{ws}/validate", json={"text": text, "ai": True}).status_code == 400
        assert client.post(f"/api/workspaces/{ws}/autofill", json={"text": text}).status_code == 400


def test_ai_features_require_a_visitor_key_even_if_server_has_one(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "server-key")
    with TestClient(create_app(Settings(data_dir=tmp_path / "web", network_checks=False))) as client:
        ws = client.post("/api/workspaces").json()["id"]
        text = _filled_template()
        for path, payload in (
            ("validate", {"text": text, "ai": True}),
            ("autofill", {"text": text}),
        ):
            res = client.post(f"/api/workspaces/{ws}/{path}", json=payload)
            assert res.status_code == 400
            assert "add yours on the page" in res.json()["detail"]


def test_visitor_brings_their_own_api_key(tmp_path, monkeypatch, caplog):
    from fastapi import HTTPException

    from paperpush.web.app import visitor_llm

    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(data_dir=tmp_path / "web", network_checks=False)
    # The provider comes from the key's prefix unless the visitor names one.
    assert visitor_llm(settings, None, "sk-ant-abc", "autofill") == ("anthropic", "sk-ant-abc")
    assert visitor_llm(settings, None, " sk-abc ", "autofill") == ("openai", "sk-abc")
    assert visitor_llm(settings, "google", "xyz", "autofill") == ("google", "xyz")
    with pytest.raises(HTTPException):
        visitor_llm(settings, None, "xyz", "autofill")
    with pytest.raises(HTTPException):
        visitor_llm(settings, None, None, "autofill")
    with pytest.raises(HTTPException):
        visitor_llm(Settings(data_dir=tmp_path / "web", ai=False), None, "sk-ant-abc", "autofill")
    import importlib

    autofill = importlib.import_module("paperpush.autofill")
    seen = {}

    def fake(model, documents, instructions, schema, max_tokens, system="", api_key=None):
        seen["api_key"] = api_key
        # A provider echoing the key back must not show it on the page.
        raise autofill.AutofillApiError(f"the API request failed: bad key {api_key}")

    monkeypatch.setitem(autofill._CALLERS, "anthropic", fake)
    with TestClient(create_app(settings)) as client:
        info = client.get("/api/info").json()
        assert info["ai"] is True and info["ai_provider"] is None
        ws = client.post("/api/workspaces").json()["id"]
        # The AI checks only run on attached files.
        client.post(f"/api/workspaces/{ws}/files", files=[("files", ("manuscript.pdf", b"%PDF-1.4", "application/pdf"))])
        res = client.post(
            f"/api/workspaces/{ws}/validate",
            json={"text": _filled_template(manuscript_file="manuscript.pdf"), "ai": True},
            headers={"X-LLM-Key": "sk-ant-visitor"},
        ).json()
    assert seen["api_key"] == "sk-ant-visitor"
    assert "sk-ant-visitor" not in json.dumps(res)
    assert "[API key]" in res["ai_error"]
    assert "sk-ant-visitor" not in caplog.text


def test_ai_checks_pass_the_visitors_key_to_the_provider(monkeypatch):
    import importlib

    from paperpush import ai_validate

    autofill = importlib.import_module("paperpush.autofill")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    seen = {}

    def fake(model, documents, instructions, schema, max_tokens, system="", api_key=None):
        seen["api_key"] = api_key
        return json.dumps({"results": []})

    monkeypatch.setitem(autofill._CALLERS, "anthropic", fake)
    task = ai_validate.AiTask(id="t", title="T", category="c", instructions="i")
    ai = ai_validate.AiOptions.api(api_key="sk-ant-visitor")
    ai_validate.run_api([task], get_venue("biorxiv"), ai)
    assert seen["api_key"] == "sk-ant-visitor"
    assert "sk-ant-visitor" not in repr(ai)


def test_ai_can_be_switched_off_on_the_server(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with TestClient(create_app(Settings(data_dir=tmp_path / "web", ai=False))) as client:
        assert client.get("/api/info").json()["ai_provider"] is None


def test_live_rules_follow_the_article_type():
    venue = get_venue("genome_biology")
    assert live_rules(venue, {})["abstract"]["max_words"] == 250
    assert live_rules(venue, {"article_type": "methodology"})["abstract"]["max_words"] == 100


def test_live_rules_leave_limits_the_form_declares_to_the_form():
    for venue in list_venues():
        for fid, limits in live_rules(venue, {}).items():
            field = next(f for f in venue.fields if f.id == fid)
            assert not (field.word_count is not None and "max_words" in limits), venue.slug
            assert not (field.max_count is not None and "max_keywords" in limits), venue.slug


def test_validate_needs_a_venue_line(client, ws):
    assert client.post(f"/api/workspaces/{ws}/validate", json={"text": "title: x\n"}).status_code == 400


def test_bundle_holds_the_sub_and_every_upload(client, ws):
    client.post(
        f"/api/workspaces/{ws}/files",
        files=[("files", ("manuscript.pdf", b"%PDF-1.4", "application/pdf"))],
    )
    text = _filled_template(manuscript_file="manuscript.pdf")
    res = client.post(f"/api/workspaces/{ws}/bundle", json={"text": text})
    assert res.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        assert sorted(zf.namelist()) == ["biorxiv-submission/biorxiv.sub.txt", "biorxiv-submission/manuscript.pdf"]
        assert zf.read("biorxiv-submission/biorxiv.sub.txt").decode() == text


def test_bundle_puts_figures_in_a_folder_and_rewrites_the_sub(client, ws):
    client.post(
        f"/api/workspaces/{ws}/files",
        files=[("files", (n, b"x", "application/octet-stream")) for n in ("manuscript.pdf", "fig1.png", "notes.txt")],
    )
    text = _filled_template(manuscript_file="some/dir/manuscript.pdf", figure_files="fig1.png | Figure 1")
    res = client.post(f"/api/workspaces/{ws}/bundle", json={"text": text})
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        names = sorted(zf.namelist())
        sub = parse(zf.read("biorxiv-submission/biorxiv.sub.txt").decode()).values
    # Uploads the .sub does not name come along too.
    assert names == [f"biorxiv-submission/{n}" for n in ("biorxiv.sub.txt", "figures/fig1.png", "manuscript.pdf", "notes.txt")]
    assert sub["manuscript_file"] == "manuscript.pdf"
    assert sub["figure_files"].strip() == "figures/fig1.png | Figure 1"


def test_bundle_sorts_by_upload_box_not_by_file_name(client, ws):
    names = ("manuscript.pdf", "plot.png", "results.xlsx", "figure_legends.docx")
    client.post(f"/api/workspaces/{ws}/files", files=[("files", (n, b"x", "application/octet-stream")) for n in names])
    venue = get_venue("acl_arr")
    text = render_template(
        venue,
        fill_defaults=True,
        values={"manuscript_file": "manuscript.pdf", "figure_files": "plot.png", "table_files": "results.xlsx", "supplementary_files": "figure_legends.docx"},
    )
    res = client.post(f"/api/workspaces/{ws}/bundle", json={"text": text})
    assert res.status_code == 200, res.text
    folder = f"{subfile_stem(default_filename(venue))}-submission"
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        got = set(zf.namelist())
    assert {f"{folder}/figures/plot.png", f"{folder}/tables/results.xlsx", f"{folder}/supplementary/figure_legends.docx"} <= got


def test_bundle_refuses_when_a_named_file_is_missing(client, ws):
    text = _filled_template(manuscript_file="manuscript.pdf", figure_files="fig9.png")
    res = client.post(f"/api/workspaces/{ws}/bundle", json={"text": text})
    assert res.status_code == 400
    assert "manuscript.pdf" in res.json()["detail"] and "fig9.png" in res.json()["detail"]


# --- subfile.js matches subfile.py -----------------------------------------

_NODE_HARNESS = """
const sub = require(process.argv[1]);
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = cases.map(c => ({
  parsed: sub.parse(c.text),
  scalar: sub.replaceScalar(c.text, c.scalar_key, c.scalar_value),
  block: sub.replaceBlock(c.text, c.block_key, c.block_value),
}));
process.stdout.write(JSON.stringify(out));
"""


def _sample_texts() -> list[str]:
    texts = [render_template(v, fill_defaults=True) for v in list_venues()]
    texts += [p.read_text(encoding="utf-8") for p in sorted((Path(__file__).parent / "sub_files").rglob("*.sub.txt"))]
    texts.append("@venue: biorxiv\ntitle: a: b\nabstract: |\n  one\n\n  two\n\n\nauthors: |\n\nlicense: CC0")
    return texts


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_subfile_js_matches_python():
    cases = []
    for text in _sample_texts():
        parsed = parse(text)
        scalar_key = next((k for k, v in parsed.values.items() if "\n" not in v), "title")
        block_key = "abstract" if "abstract: |" in text else "authors"
        cases.append(
            {
                "text": text,
                "scalar_key": scalar_key,
                "scalar_value": "new | value",
                "block_key": block_key,
                "block_value": "line one\n\nline three",
            }
        )
    proc = subprocess.run(
        ["node", "-e", _NODE_HARNESS, str(SUBFILE_JS)],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        check=True,
    )
    for case, got in zip(cases, json.loads(proc.stdout)):
        expected = parse(case["text"])
        assert got["parsed"] == {"venue": expected.venue, "values": expected.values}
        assert got["scalar"] == replace_scalar(case["text"], case["scalar_key"], case["scalar_value"])
        assert got["block"] == replace_block(case["text"], case["block_key"], case["block_value"])


_SET_FIELD_HARNESS = """
const sub = require(process.argv[1]);
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(cases.map(c => sub.setField(c.text, c.field, c.value))));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_subfile_js_set_field_round_trips_through_the_python_parser():
    text = render_template(get_venue(VENUE), fill_defaults=True)
    cases = [
        {"text": text, "field": {"id": "title", "type": "text"}, "value": "A title"},
        {
            "text": text,
            "field": {"id": "authors", "type": "authorlist"},
            "value": "Ada | a@x.org | U |  | yes",
        },
        # A one-line field given several lines turns into a block.
        {"text": text, "field": {"id": "title", "type": "text"}, "value": "two\nlines"},
        # A field deleted from the text by hand is appended.
        {
            "text": "@venue: biorxiv\n",
            "field": {"id": "abstract", "type": "textarea"},
            "value": "p1\n\np2",
        },
    ]
    proc = subprocess.run(
        ["node", "-e", _SET_FIELD_HARNESS, str(SUBFILE_JS)],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        check=True,
    )
    for case, out in zip(cases, json.loads(proc.stdout)):
        values = parse(out).values
        assert values[case["field"]["id"]] == case["value"]
        # Every other field is untouched.
        before = parse(case["text"]).values
        assert {k: v for k, v in values.items() if k != case["field"]["id"]} == {k: v for k, v in before.items() if k != case["field"]["id"]}


# --- checks.js matches validate.py -----------------------------------------

_LIVE_HARNESS = """
const checks = require(process.argv[1]);
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = cases.map(c => {
  const res = checks.run(c.fields, c.values, c.rules, c.opts);
  return Object.fromEntries(res.checks.map(k => [k.id, {status: k.status, issues: k.issues.map(i => [i.level, i.field, i.message])}]));
});
process.stdout.write(JSON.stringify(out));
"""


def _live_cases():
    """(venue, values) pairs that exercise each live check, passing and failing."""
    cases = []
    for slug in ("biorxiv", "nature", "arxiv", "bioinformatics", "iclr_2027"):
        try:
            venue = get_venue(slug)
        except KeyError:
            continue
        cases.append((venue, {}))
        cases.append((venue, parse(render_template(venue, fill_defaults=True)).values))
        long_values = {}
        for f in venue.fields:
            if f.type in ("text", "textarea") and not f.fields:
                long_values[f.id] = "word " * 400 if f.type == "textarea" else "a title " * 40
            elif f.type == "authorlist":
                cols = len(f.fields or ["name", "email?", "affiliation?", "orcid?", "corresponding"])
                long_values[f.id] = "\n".join(
                    [
                        "Ada Lovelace" + " | " * (cols - 1) + "yes",
                        "Alan Turing" + " | " * (cols - 1) + "yes",
                        " | ".join([""] * cols),
                    ]
                )
            elif f.type == "multichoice" and f.options:
                long_values[f.id] = ", ".join(list(f.options)[:3] + ["Not an option"])
            elif f.type == "boolean":
                long_values[f.id] = "no"
        long_values["keywords"] = ", ".join(f"kw{i}" for i in range(20))
        cases.append((venue, long_values))
        # Values of the wrong type, for form.schema.
        bad = {"not_a_field": "x"}
        for f in venue.fields:
            if f.type == "choice" and f.options and not f.options_recommended:
                bad[f.id] = "Not an option"
            elif f.type == "boolean":
                bad[f.id] = "maybe"
            elif f.type == "int":
                bad[f.id] = "-5" if f.min_value is not None else "three"
            elif f.type == "filelist" and f.type_options:
                bad[f.id] = "a.pdf\nb.pdf | not a type"
            elif f.type == "textarea" and not f.fields:
                bad[f.id] = "Unknown > Subject"  # what a venue validator (Nature's subject tree) rejects
        cases.append((venue, bad))
    return cases


_SHARE_HARNESS = """
const sub = require(process.argv[1]);
const texts = JSON.parse(require('fs').readFileSync(0, 'utf8'));
(async () => {
  const out = [];
  for (const t of texts) {
    const code = await sub.toShareCode(t);
    out.push({ code, back: await sub.fromShareCode(code) });
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_subfile_js_share_code_round_trips():
    """A share link's code must give back the exact .sub, and stay URL-safe."""
    texts = [
        _filled_template(title="Ünïcødé — title", abstract="line 1\n\nline 2 with + / = signs"),
        "",
        "@venue: biorxiv\n" + "x" * 100_000,  # larger than one String.fromCharCode chunk
    ]
    proc = subprocess.run(
        ["node", "-e", _SHARE_HARNESS, str(SUBFILE_JS)],
        input=json.dumps(texts),
        capture_output=True,
        text=True,
        check=True,
    )
    for text, out in zip(texts, json.loads(proc.stdout)):
        assert out["back"] == text
        assert all(c.isalnum() or c in "-_" for c in out["code"])


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_checks_js_matches_validate_py():
    import dataclasses

    from paperpush.subfile import SubFile

    payload, expected = [], []
    for venue, values in _live_cases():
        report = validate_report(
            SubFile(venue=venue.slug, values=values),
            venue,
            check_links=False,
            check_references=False,
            check_openreview=False,
            check_sensitive=False,
            check_hidden_text=False,
        )
        payload.append(
            {
                "fields": [dataclasses.asdict(f) for f in venue.fields],
                "values": values,
                "rules": live_rules(venue, values),
                "opts": {"venue": venue.slug, "serverIssues": venue_schema_issues(venue, report)},
            }
        )
        got = {}
        for r in report.results:
            if r.id in LIVE_CHECKS:
                got[r.id] = {
                    "status": r.status,
                    "issues": [[i.level, i.field, i.message] for i in r.issues],
                }
        expected.append(got)
    out = subprocess.run(
        ["node", "-e", _LIVE_HARNESS, str(CHECKS_JS)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    for (venue, _), py, js in zip(_live_cases(), expected, json.loads(out.stdout)):
        for check_id, want in py.items():
            have = js[check_id]
            # validate names the valid options (or the command listing them) after the message.
            trim = lambda issues: [
                [
                    lvl,
                    fid,
                    msg.split(" (")[0] if "is not a valid option" in msg else msg,
                ]
                for lvl, fid, msg in issues
            ]  # noqa: E731
            # Same findings; validate visits the title, abstract, and keywords by role, the page in form order.
            assert sorted(trim(have["issues"])) == sorted(trim(want["issues"])), f"{venue.slug} {check_id}"
            # The page leaves a guideline check it has no limits for as not applicable; validate calls it passed.
            if not (check_id == "guidelines.text_fields" and have["status"] == "not_applicable"):
                assert have["status"] == want["status"], f"{venue.slug} {check_id}"


# --- opening a .sub or a .zip ---------------------------------------------


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_import_a_sub_returns_its_text_without_storing_it(client, ws):
    text = _filled_template()
    res = client.post(f"/api/workspaces/{ws}/import", files=[("files", ("biorxiv.sub", text.encode(), "text/plain"))]).json()
    assert res["subs"] == [{"name": "biorxiv.sub", "text": text}]
    assert res["files"] == []


def test_import_a_zip_unpacks_it_flat_and_returns_its_sub(client, ws, tmp_path):
    text = _filled_template(manuscript_file="paper/manuscript.pdf")
    data = _zip(
        {
            "paper/biorxiv.sub": text.encode(),
            "paper/manuscript.pdf": b"%PDF-1.4",
            "paper/figures/fig1.png": b"png",
            "paper/supp/fig1.png": b"other",  # same name: skipped
            "__MACOSX/paper/._manuscript.pdf": b"junk",
            "paper/.DS_Store": b"junk",
            "../../escape.pdf": b"x",  # '..' parts: dropped
        }
    )
    res = client.post(f"/api/workspaces/{ws}/import", files=[("files", ("paper.zip", data, "application/zip"))]).json()
    assert [s["text"] for s in res["subs"]] == [text]
    assert sorted(res["saved"]) == ["fig1.png", "manuscript.pdf"]
    assert "paper/figures/fig1.png" in res["paths"]
    assert [s["name"] for s in res["skipped"]] == ["paper/supp/fig1.png"]
    assert sorted(f["name"] for f in res["files"]) == ["fig1.png", "manuscript.pdf"]
    assert not list(tmp_path.rglob("escape.pdf"))
    # The .sub's folder paths resolve to the unpacked files.
    v = client.post(f"/api/workspaces/{ws}/validate", json={"text": text}).json()
    assert "manuscript.pdf" not in json.dumps([c for c in v["checks"] if c.get("status") == "failed" and "not found" in c.get("detail", "")])


def test_import_a_bundle_downloaded_here_round_trips(client, ws):
    client.post(f"/api/workspaces/{ws}/files", files=[("files", ("manuscript.pdf", b"%PDF-1.4", "application/pdf"))])
    text = _filled_template(manuscript_file="manuscript.pdf")
    bundle = client.post(f"/api/workspaces/{ws}/bundle", json={"text": text}).content
    other = client.post("/api/workspaces").json()["id"]
    res = client.post(f"/api/workspaces/{other}/import", files=[("files", ("b.zip", bundle, "application/zip"))]).json()
    assert res["subs"][0]["text"] == text
    assert res["saved"] == ["manuscript.pdf"]


def test_import_refuses_other_files_bad_zips_and_zips_over_the_cap(tmp_path):
    settings = Settings(data_dir=tmp_path / "web", max_workspace_mb=0.001, network_checks=False)
    with TestClient(create_app(settings)) as c:
        ws_id = c.post("/api/workspaces").json()["id"]
        url = f"/api/workspaces/{ws_id}/import"
        assert c.post(url, files=[("files", ("paper.pdf", b"x", "application/pdf"))]).status_code == 400
        assert c.post(url, files=[("files", ("bad.zip", b"not a zip", "application/zip"))]).status_code == 400
        # Small compressed, large unpacked: refused on the bytes actually unpacked.
        bomb = _zip({"big.bin": b"0" * 50_000})
        assert len(bomb) < 1024
        assert c.post(url, files=[("files", ("bomb.zip", bomb, "application/zip"))]).status_code == 413
        assert c.get(f"/api/workspaces/{ws_id}").json()["files"] == []


def test_match_places_files_by_name(client):
    res = client.post(
        "/api/venues/nature/match",
        json={"paths": ["x/manuscript.docx", "x/figures/Fig1.pdf", "x/cover_letter.pdf", "x/notes.md"]},
    ).json()
    assert res["assignments"] == {
        "manuscript_file": "manuscript.docx",
        "figure_files": "Fig1.pdf",
        "cover_letter": "cover_letter.pdf",
    }
    assert [m["name"] for m in res["unmatched"]] == ["notes.md"]
    assert client.post("/api/venues/nope/match", json={"paths": []}).status_code == 404


# --- local sessions (paperpush <command> --web) ------------------------------


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    from paperpush.web.app import LocalSession

    monkeypatch.chdir(tmp_path)
    paper = tmp_path / "paper"
    paper.mkdir()
    shutil.copy(MANUSCRIPT, paper / "manuscript.pdf")
    sub_path = tmp_path / "biorxiv.sub"
    sub_path.write_text(_filled_template(manuscript_file="paper/manuscript.pdf"), encoding="utf-8")
    local = LocalSession(sub_path=sub_path, root=tmp_path, manuscript_dirs=(paper,), action="validate")
    settings = Settings(data_dir=tmp_path / "web", network_checks=False, local=local)
    with TestClient(create_app(settings)) as c:
        c.headers["X-Paperpush-Token"] = local.token
        yield c, local


def test_local_endpoints_need_the_session_token(local_client, client):
    c, local = local_client
    assert c.get("/api/local", headers={"X-Paperpush-Token": "wrong"}).status_code == 403
    assert c.post("/api/workspaces/local/plan", json={"text": local.sub_path.read_text()}, headers={"X-Paperpush-Token": ""}).status_code == 403
    # The hosted site has no local session at all.
    assert client.get("/api/local", headers={"X-Paperpush-Token": "x"}).status_code == 404
    assert client.get("/api/workspaces/local").status_code == 404


def test_local_session_reads_and_saves_the_sub_file(local_client):
    c, local = local_client
    data = c.get("/api/local").json()
    assert data["path"] == "biorxiv.sub"
    assert data["venue"] == VENUE and data["action"] == "validate"
    assert data["text"] == local.sub_path.read_text()
    assert "paper/manuscript.pdf" in {f["name"] for f in data["files"]}
    assert c.get("/api/info").json()["local"] is True

    edited = replace_scalar(data["text"], "title", "A local title")
    assert c.put("/api/local", json={"text": edited}).json()["saved"]
    assert local.sub_path.read_text() == edited
    # Text that is not a .sub never overwrites the file.
    assert c.put("/api/local", json={"text": "nonsense"}).status_code == 400
    assert local.sub_path.read_text() == edited


def test_local_validate_resolves_paths_against_the_working_directory(local_client):
    c, local = local_client
    res = c.post("/api/workspaces/local/validate", json={"text": local.sub_path.read_text()}).json()
    blob = json.dumps(res)
    assert str(local.root) not in blob
    # paper/manuscript.pdf is relative to the working directory, as on the CLI, and found there.
    manuscript_issues = [i for chk in res["checks"] for i in chk.get("issues", []) if i.get("field") == "manuscript_file" and i.get("level") == "error"]
    assert not manuscript_issues, manuscript_issues


def test_local_uploads_land_beside_the_manuscript_and_nothing_is_deleted(local_client):
    c, local = local_client
    res = c.post("/api/workspaces/local/files", files=[("files", ("cover.pdf", b"%PDF-1.4", "application/pdf"))]).json()
    assert res["saved"] == ["paper/cover.pdf"]
    assert (local.root / "paper" / "cover.pdf").is_file()
    assert c.delete("/api/workspaces/local/files/cover.pdf").status_code == 403
    assert c.delete("/api/workspaces/local").json() == {"deleted": False}
    assert (local.root / "paper" / "cover.pdf").is_file()


def test_local_bundle_packs_the_files_from_this_computer(local_client):
    c, local = local_client
    (local.root / "figs").mkdir()
    (local.root / "figs" / "fig1.png").write_bytes(b"png")
    text = replace_block(local.sub_path.read_text(), "figure_files", "figs/fig1.png")
    res = c.post("/api/workspaces/local/bundle", json={"text": text})
    assert res.status_code == 200, res.text
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        assert sorted(zf.namelist()) == [f"biorxiv-submission/{n}" for n in ("biorxiv.sub.txt", "figures/fig1.png", "manuscript.pdf")]
        sub = parse(zf.read("biorxiv-submission/biorxiv.sub.txt").decode()).values
        assert zf.read("biorxiv-submission/manuscript.pdf") == (local.root / "paper" / "manuscript.pdf").read_bytes()
    assert sub["manuscript_file"] == "manuscript.pdf" and sub["figure_files"].strip() == "figures/fig1.png"
    # A path that is not on this computer is named, relative to where paperpush ran.
    missing = replace_block(text, "figure_files", "figs/gone.png")
    res = c.post("/api/workspaces/local/bundle", json={"text": missing})
    assert res.status_code == 400 and "figs/gone.png" in res.json()["detail"]
    assert str(local.root) not in res.json()["detail"]


def test_local_ai_falls_back_to_the_environment_key(local_client, monkeypatch):
    from paperpush.web.app import visitor_llm

    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(data_dir=Path("."))
    with pytest.raises(Exception):
        visitor_llm(settings, None, None, "autofill", local=True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert visitor_llm(settings, None, None, "autofill", local=True) == ("anthropic", None)
    # The hosted site still insists on the visitor's own key.
    with pytest.raises(Exception):
        visitor_llm(settings, None, None, "autofill")


@pytest.fixture
def served(monkeypatch):
    """Record what ``--web`` would serve instead of starting a server."""
    calls = []
    import paperpush.web.app as web_app

    monkeypatch.setattr(web_app, "serve_local", lambda sub_path, **kw: calls.append((Path(sub_path), kw)) or 0)
    return calls


def test_subfile_web_writes_the_template_then_opens_it(tmp_path, monkeypatch, served):
    from paperpush.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["subfile", VENUE, "--web"]) == 0
    assert (tmp_path / "biorxiv.sub.txt").is_file()
    assert served == [(Path("biorxiv.sub.txt"), {"manuscript_dirs": (), "action": None})]
    # Run again, the existing file is opened rather than refused.
    (tmp_path / "biorxiv.sub.txt").write_text("@venue: biorxiv\nmine\n")
    assert main(["subfile", VENUE, "--web"]) == 0
    assert (tmp_path / "biorxiv.sub.txt").read_text() == "@venue: biorxiv\nmine\n"
    assert len(served) == 2


def test_validate_web_opens_the_page_to_validate(tmp_path, monkeypatch, served):
    from paperpush.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "biorxiv.sub").write_text(_filled_template())
    assert main(["validate", "biorxiv.sub", "--web"]) == 0
    assert main(["validate", "biorxiv.sub", "--web", "--ai"]) == 0
    assert [kw["action"] for _, kw in served] == ["validate", "validate-ai"]


def test_autofill_web_without_values_lets_the_page_autofill(tmp_path, monkeypatch, served):
    from paperpush.cli import main

    monkeypatch.chdir(tmp_path)
    paper = tmp_path / "paper"
    paper.mkdir()
    shutil.copy(MANUSCRIPT, paper / "manuscript.pdf")
    assert main(["autofill", "-p", "paper", "biorxiv.sub", "--web"]) == 0
    sub = parse((tmp_path / "biorxiv.sub").read_text())
    assert sub.venue == VENUE
    assert sub.values["manuscript_file"] == "paper/manuscript.pdf"
    (path, kw), = served
    assert path == Path("biorxiv.sub") and kw["action"] == "autofill"
    assert kw["manuscript_dirs"] == (Path("paper"),)


def test_venue_detail_says_whether_the_initial_submission_may_be_in_any_format(client):
    assert client.get("/api/venues/nature").json()["initial_submission_rule"]
    assert client.get(f"/api/venues/{VENUE}").json()["initial_submission_rule"] == ""


def test_validate_as_initial_submission_turns_errors_into_warnings(client, ws):
    text = _filled_template(manuscript_file="manuscript.pdf")
    res = client.post(f"/api/workspaces/{ws}/validate", json={"text": text}).json()
    assert _checks(res)["files.uploads"]["status"] == "failed" and not res["ok"]
    assert res["initial_submission"] is False and res["downgraded"] == 0
    res = client.post(f"/api/workspaces/{ws}/validate", json={"text": text, "initial_submission": True}).json()
    uploads = _checks(res)["files.uploads"]
    assert uploads["status"] == "warning"
    assert {i["level"] for i in uploads["issues"]} == {"warning"}
    assert res["ok"] and res["counts"]["failed"] == 0 and res["downgraded"] >= 1


def test_validate_web_initial_submission_ticks_the_box(tmp_path, monkeypatch, served):
    from paperpush.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "biorxiv.sub").write_text(_filled_template())
    assert main(["validate", "biorxiv.sub", "--web", "--initial-submission"]) == 0
    assert main(["validate", "biorxiv.sub", "--web", "--ai", "--initial-submission"]) == 0
    assert [kw["action"] for _, kw in served] == ["validate+initial", "validate-ai+initial"]


# --- shared forms ------------------------------------------------------------

SHARED_JS = STATIC / "shared.js"
_TOKEN = "t" * 43
_HDR = {"X-Paperpush-Share-Token": _TOKEN}


def test_shared_form_create_get_update(client):
    assert client.get("/api/info").json()["shared"] == {"durable": False, "ttl_days": 90.0}
    made = client.post("/api/shared", json={"ciphertext": "AAAA", "token": _TOKEN}).json()
    assert made["version"] == 1
    path = f"/api/shared/{made['id']}"
    assert client.get(path).json()["ciphertext"] == "AAAA"
    # Asking with the version you already have returns no ciphertext.
    assert "ciphertext" not in client.get(path, params={"known": 1}).json()

    res = client.put(path, json={"ciphertext": "BBBB", "base_version": 1}, headers=_HDR)
    assert res.status_code == 200 and res.json()["version"] == 2
    assert client.get(path).json()["ciphertext"] == "BBBB"


def test_shared_form_stale_update_gets_409_with_current(client):
    made = client.post("/api/shared", json={"ciphertext": "AAAA", "token": _TOKEN}).json()
    path = f"/api/shared/{made['id']}"
    client.put(path, json={"ciphertext": "BBBB", "base_version": 1}, headers=_HDR)
    res = client.put(path, json={"ciphertext": "CCCC", "base_version": 1}, headers=_HDR)
    assert res.status_code == 409
    assert res.json()["current"]["version"] == 2
    assert res.json()["current"]["ciphertext"] == "BBBB"


def test_shared_form_needs_its_token_to_change_or_delete(client):
    made = client.post("/api/shared", json={"ciphertext": "AAAA", "token": _TOKEN}).json()
    path = f"/api/shared/{made['id']}"
    wrong = {"X-Paperpush-Share-Token": "w" * 43}
    assert client.put(path, json={"ciphertext": "BBBB", "base_version": 1}, headers=wrong).status_code == 403
    assert client.put(path, json={"ciphertext": "BBBB", "base_version": 1}).status_code == 403
    assert client.delete(path, headers=wrong).status_code == 403
    assert client.delete(path, headers=_HDR).status_code == 200
    assert client.get(path).status_code == 404


def test_shared_form_rejects_bad_input(client):
    assert client.post("/api/shared", json={"ciphertext": "not base64url!", "token": _TOKEN}).status_code == 400
    assert client.post("/api/shared", json={"ciphertext": "AAAA", "token": "short"}).status_code == 403
    assert client.get("/api/shared/../../etc").status_code == 404
    assert client.get("/api/shared/" + "x" * 24).status_code == 404


def test_shared_forms_expire(tmp_path):
    from paperpush.web.shared import SharedForms

    store = SharedForms(sqlite_path=tmp_path / "s.sqlite3", ttl_days=1)
    form = store.create("AAAA", _TOKEN)
    assert store.sweep() == 0
    with store._connect() as conn:
        conn.execute("UPDATE shared_forms SET updated_at = 0 WHERE id = ?", (form["id"],))
    assert store.sweep() == 1


def test_shared_forms_are_off_for_local_sessions_and_when_disabled(tmp_path):
    settings = Settings(data_dir=tmp_path / "web", network_checks=False, shared=False)
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/info").json()["shared"] is None
        assert c.post("/api/shared", json={"ciphertext": "AAAA", "token": _TOKEN}).status_code == 404


_MERGE_HARNESS = """
const sub = require(process.argv[1]);
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const fields = new Map([["title", {id: "title", type: "text"}], ["abstract", {id: "abstract", type: "textarea"}]]);
process.stdout.write(JSON.stringify(cases.map(c => sub.mergeFields(c.base, c.mine, c.theirs, fields))));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_subfile_js_merge_fields():
    base = _filled_template(title="T0", abstract="A0")
    mine_title = replace_scalar(base, "title", "T-mine")
    theirs_abs = replace_block(base, "abstract", "A-theirs\n\nsecond paragraph")
    theirs_title = replace_scalar(base, "title", "T-theirs")
    cases = [
        # Different fields: both kept.
        {"base": base, "mine": mine_title, "theirs": theirs_abs},
        # Same field, different values: a conflict, mine kept for now.
        {"base": base, "mine": mine_title, "theirs": theirs_title},
        # Nothing of mine: theirs as-is.
        {"base": base, "mine": base, "theirs": theirs_abs},
    ]
    proc = subprocess.run(["node", "-e", _MERGE_HARNESS, str(SUBFILE_JS)], input=json.dumps(cases), capture_output=True, text=True, check=True)
    both, clash, plain = json.loads(proc.stdout)

    assert parse(both["text"]).values["title"] == "T-mine"
    assert parse(both["text"]).values["abstract"] == "A-theirs\n\nsecond paragraph"
    assert both["conflicts"] == []

    assert parse(clash["text"]).values["title"] == "T-mine"
    assert clash["conflicts"] == [{"id": "title", "mine": "T-mine", "theirs": "T-theirs"}]

    assert plain["text"] == theirs_abs and plain["conflicts"] == []


_CRYPTO_HARNESS = """
const sc = require(process.argv[1]);
(async () => {
  const key = sc.newKey();
  const a = await sc.derive(key), b = await sc.derive(key), other = await sc.derive(sc.newKey());
  const text = "@venue: biorxiv\\ntitle: Ünïcødé " + "x".repeat(50000) + "\\n";
  const ct = await sc.encrypt(text, a.aes);
  let wrongKeyFails = false;
  try { await sc.decrypt(ct, other.aes); } catch { wrongKeyFails = true; }
  process.stdout.write(JSON.stringify({
    key, ct, back: (await sc.decrypt(ct, b.aes)) === text,
    sameToken: a.token === b.token, otherToken: a.token !== other.token, token: a.token,
    fresh: ct !== await sc.encrypt(text, a.aes), wrongKeyFails,
    leaks: ct.includes("biorxiv"),
  }));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_shared_js_encrypts_and_derives_tokens():
    out = json.loads(subprocess.run(["node", "-e", _CRYPTO_HARNESS, str(SHARED_JS)], capture_output=True, text=True, check=True).stdout)
    assert out["back"] and out["sameToken"] and out["otherToken"] and out["fresh"] and out["wrongKeyFails"]
    assert not out["leaks"]
    # What the page sends must pass the server's format checks.
    from paperpush.web.shared import check_ciphertext, check_token

    check_ciphertext(out["ct"])
    check_token(out["token"])


def test_local_session_resolves_against_the_sub_folder_then_cwd(tmp_path, monkeypatch):
    from paperpush.web.app import LocalSession

    ms = tmp_path / "ms"
    ms.mkdir()
    (ms / "beside.pdf").write_bytes(b"%PDF-1.4\n")
    (ms / "both.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "both.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "cwd_only.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.chdir(tmp_path)
    local = LocalSession(sub_path=ms / "x.sub", root=ms)
    assert local.resolve("beside.pdf") == ms / "beside.pdf"
    assert local.resolve("both.pdf") == ms / "both.pdf"
    assert local.resolve("cwd_only.pdf").resolve() == (tmp_path / "cwd_only.pdf").resolve()
    assert local.resolve("missing.pdf") == ms / "missing.pdf"
