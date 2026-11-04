"""Tests for venue files: ``paperpush add-venue`` and the web page's "Add venue" editor."""

from __future__ import annotations

import json
import re

import pytest

from paperpush import venuefile
from paperpush.cli import main
from paperpush.database import list_venues

SLUGS = [v.slug for v in list_venues(include_deprecated=True)]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A scratch copy of the venue databases, read by paperpush for the test."""
    root = tmp_path / "data"
    monkeypatch.setenv("PAPERPUSH_VENUE_DATA", str(root))
    for path in venuefile.database_paths().values():
        root.mkdir(exist_ok=True)
        (root / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    from paperpush.database import reload

    reload()
    yield root
    monkeypatch.delenv("PAPERPUSH_VENUE_DATA")
    reload()


def _filled(slug: str = "my_journal") -> str:
    """The template with the minimum a venue needs, as an author would fill it."""
    text = venuefile.template(slug)
    text = text.replace("options: |\n", "options: |\n  Research Article\n  Brief Communication\n", 1)
    text = text.replace("\nname:\n", "\nname: My Journal\n", 1)
    text = text.replace("\nsource_urls:\n", "\nsource_urls: https://example.org/guide\n", 1)
    text = text.replace("\narticle_type_field:\n", "\narticle_type_field: article_type\n", 1)
    text = re.sub(r"(\[requirements\.abstract\]\n(?:#.*\n)*max_words:)", r"\1 250", text)
    return text + "\n[article_type Brief Communication]\nabstract.max_words: 100\nnotes: Short.\n"


@pytest.mark.parametrize("slug", SLUGS)
def test_every_venue_round_trips(slug):
    doc = venuefile.doc_for(slug)
    back = venuefile.parse(venuefile.render(doc))
    assert (back.slug, back.venue, back.form, back.requirements) == (doc.slug, doc.venue, doc.form, doc.requirements)
    assert venuefile.check(back, force=True).errors == []


def test_template_lists_every_requirement_key():
    text = venuefile.template("x")
    for section, specs in venuefile.section_specs().items():
        assert f"[requirements.{section}]" in text
        for name in specs:
            if name != "template_layout":
                assert re.search(rf"^{name}:", text, re.M), (section, name)


def test_unfilled_template_needs_its_choices():
    result = venuefile.check(venuefile.parse(venuefile.template("my_journal")))
    assert any("article_type" in e and "no options" in e for e in result.errors)


def test_filled_template_parses_to_entries():
    doc = venuefile.parse(_filled())
    assert doc.slug == "my_journal" and doc.venue["name"] == "My Journal"
    assert [f["id"] for f in doc.form["fields"]][:2] == ["article_type", "manuscript_file"]
    assert doc.requirements["abstract"] == {"max_words": 250}
    assert doc.requirements["article_types"] == {"Brief Communication": {"abstract": {"max_words": 100}, "notes": ["Short."]}}
    assert venuefile.check(doc).errors == []


def test_parse_reports_every_problem_with_its_line():
    text = "[venue]\nslug: x\nbogus: 1\n[field a]\ntype: wat\n[requirements.foo]\n[article_type X]\nabc.d: 1\n"
    with pytest.raises(venuefile.VenueFileError) as info:
        venuefile.parse(text)
    problems = info.value.problems
    assert [p.split(":")[0] for p in problems] == ["line 3", "line 5", "line 6", "line 8"]


def test_check_catches_cross_file_mistakes():
    doc = venuefile.parse(_filled().replace("[article_type Brief Communication]", "[article_type Letter]"))
    assert any("'Letter'" in e for e in venuefile.check(doc).errors)
    assert any("already a venue" in e for e in venuefile.check(venuefile.doc_for("nature")).errors)


def test_validate_only_venue_has_no_form():
    text = _filled().replace("include: yes", "include: no", 1)
    doc = venuefile.parse(text)
    assert doc.form is None and doc.requirements is not None


def test_add_venue_cli_merges_into_the_databases(tmp_path, data_dir, capsys):
    path = tmp_path / "my_journal.txt"
    path.write_text(_filled(), encoding="utf-8")
    assert main(["add-venue", str(path), "--dry-run"]) == 0
    assert "my_journal" not in json.loads((data_dir / "venues.json").read_text())

    assert main(["add-venue", str(path), "--data-dir", str(data_dir)]) == 0
    for name in ("venues.json", "submission_forms.json", "manuscript_requirements.json"):
        assert "my_journal" in json.loads((data_dir / name).read_text()), name
    from paperpush.database import get_venue
    from paperpush.requirements import get_requirements

    assert get_venue("my_journal").name == "My Journal"
    assert get_requirements("my_journal").abstract.max_words == 250

    # A second add needs --overwrite (old name: --force), and a venue file from --from round-trips.
    assert main(["add-venue", str(path), "--data-dir", str(data_dir)]) == 1
    out = tmp_path / "again.txt"
    assert main(["add-venue", "--template", "--from", "my_journal", "-o", str(out)]) == 0
    assert main(["add-venue", str(out), "--data-dir", str(data_dir), "--overwrite"]) == 0
    assert main(["add-venue", str(out), "--data-dir", str(data_dir), "--force"]) == 0


def test_add_venue_template_cli(tmp_path, capsys):
    out = tmp_path / "x.txt"
    assert main(["add-venue", "--template", "new_venue", "-o", str(out)]) == 0
    assert "slug: new_venue" in out.read_text()
    assert main(["add-venue", "--template", "new_venue", "-o", str(out)]) == 1  # exists
    assert main(["add-venue", "--template", "--from", "no_such_venue", "-o", str(tmp_path / "y.txt")]) == 2
    assert main(["add-venue", "--template", "Bad Slug"]) == 2


def test_editor_json_round_trips_through_the_file():
    for slug in ("nature", "science_advances", "bioinformatics", "iclr_2027"):
        doc = venuefile.doc_for(slug)
        again = venuefile.doc_from_json(json.loads(json.dumps(venuefile.doc_to_json(doc))))
        assert venuefile.parse(venuefile.render(again)).requirements == doc.requirements


def test_web_endpoints(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from paperpush.web.app import Settings, create_app

    with TestClient(create_app(Settings(data_dir=tmp_path / "web", network_checks=False))) as client:
        assert client.get("/add-venue").status_code == 200
        schema = client.get("/api/venue-file/schema").json()
        assert {s["name"] for s in schema["venue"]} >= {"slug", "name", "venue_type"}
        assert "manuscript" in schema["sections"]

        start = client.get("/api/venue-file/start").json()
        assert start["form"]["fields"] and start["slug"] == ""
        assert client.get("/api/venue-file/start", params={"from": "no_such"}).status_code == 404

        nature = client.get("/api/venue-file/start", params={"from": "nature"}).json()
        res = client.post("/api/venue-file/render", json={"doc": nature, "filename": "nature.txt", "replacing": True}).json()
        assert res["errors"] == [] and "[venue]" in res["text"]
        assert client.post("/api/venue-file/render", json={"doc": nature}).json()["errors"]  # already a venue

        parsed = client.post("/api/venue-file/parse", json={"text": res["text"]}).json()
        assert parsed["doc"]["slug"] == "nature" and parsed["problems"] == []
        bad = client.post("/api/venue-file/parse", json={"text": "[nope]\n"}).json()
        assert bad["doc"] is None and bad["problems"]
