"""paperpush validate --json: the report as JSON on stdout, or written to a file."""

from __future__ import annotations

import importlib
import json

import pytest

from paperpush.cli import main
from paperpush.database import Field, Venue

validate_mod = importlib.import_module("paperpush.validate")
FLAGS = ["--dont-check-links", "--dont-check-references", "--dont-check-manuscript", "--dont-check-for-sensitive-info"]


@pytest.fixture(autouse=True)
def _clear_model_cache():
    validate_mod._MODELS.clear()
    yield
    validate_mod._MODELS.clear()


def test_cli_json_flag(tmp_path, monkeypatch, capsys):
    import paperpush.cli as cli

    # A slug of its own: validate caches its schema model per slug.
    venue = Venue(slug="test_validate_json_cli", name="Test Venue", fields=[Field(id="abstract", label="Abstract", type="textarea", word_count=3)])
    monkeypatch.setattr(cli, "get_venue", lambda slug: venue)
    sub = tmp_path / "paper.sub"
    sub.write_text("@venue: test_validate_json_cli\nabstract: one two three four\n", encoding="utf-8")

    assert main(["validate", str(sub), "--json", *FLAGS]) == 1
    data = json.loads(capsys.readouterr().out)  # stdout is only the JSON
    assert data["ok"] is False
    assert any("4 words" in i["message"] for c in data["checks"] for i in c.get("issues", []))

    out = tmp_path / "report.json"
    assert main(["validate", str(sub), "--json", str(out), *FLAGS]) == 1
    assert json.loads(out.read_text())["venue"] == "test_validate_json_cli"
    assert "Wrote the report as JSON" in capsys.readouterr().out
