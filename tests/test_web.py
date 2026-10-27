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
from paperpush.subfile import parse, render_template, replace_block, replace_scalar
from paperpush.validate import validate_report
from paperpush.web.app import LIVE_CHECKS, Settings, create_app, live_rules, localize_values, safe_name

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
    for asset in ("app.js", "subfile.js", "checks.js", "style.css"):
        assert client.get(f"/static/{asset}").status_code == 200


def test_venue_list_matches_the_database(client):
    assert [v["slug"] for v in client.get("/api/venues").json()] == [v.slug for v in list_venues()]


def test_venue_detail_carries_template_and_field_definitions(client):
    detail = client.get(f"/api/venues/{VENUE}").json()
    assert detail["template"] == render_template(get_venue(VENUE), fill_defaults=True)
    authors = next(f for f in detail["fields"] if f["id"] == "authors")
    assert authors["fields"] and authors["role"]
    assert detail["subfile_name"] == f"{VENUE}.sub"


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
    assert by_id["integrity.links"]["group"] == "validate"
    assert all(c["group"] == "ai" for c in res["checks"] if c["id"].startswith("ai."))


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
        res = client.post(
            f"/api/workspaces/{ws}/validate",
            json={"text": _filled_template(), "ai": True},
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
        assert sorted(zf.namelist()) == ["biorxiv.sub", "manuscript.pdf"]
        assert zf.read("biorxiv.sub").decode() == text


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
    texts += [p.read_text(encoding="utf-8") for p in sorted((Path(__file__).parent / "sub_files").rglob("*.sub"))]
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
  const res = checks.run(c.fields, c.values, c.rules);
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
    return cases


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_checks_js_matches_validate_py():
    import dataclasses

    from paperpush.subfile import SubFile

    payload, expected = [], []
    for venue, values in _live_cases():
        payload.append(
            {
                "fields": [dataclasses.asdict(f) for f in venue.fields],
                "values": values,
                "rules": live_rules(venue, values),
            }
        )
        report = validate_report(
            SubFile(venue=venue.slug, values=values),
            venue,
            check_links=False,
            check_references=False,
            check_openreview=False,
            check_sensitive=False,
            check_hidden_text=False,
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
