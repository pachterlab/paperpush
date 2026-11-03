"""``paperpush web [FILE.sub]``: serve the web page on this computer.

The servers themselves (``serve_app``, ``serve_local``) are replaced by stubs:
these tests check what the command hands them and when it refuses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from paperpush.cli import main  # noqa: E402
from paperpush.web import app as web_app  # noqa: E402

SUB = Path(__file__).parent / "sub_files" / "nucleic_acids_research" / "nucleic_acids_research.sub"


@pytest.fixture
def served(monkeypatch):
    calls: list[tuple[str, tuple, dict]] = []
    monkeypatch.setattr(web_app, "serve_app", lambda *a, **k: calls.append(("app", a, k)) or 0)
    monkeypatch.setattr(web_app, "serve_local", lambda *a, **k: calls.append(("local", a, k)) or 0)
    return calls


def test_web_without_a_file_serves_the_plain_page(served):
    assert main(["web", "--no-browser", "--port", "9123"]) == 0
    assert served == [("app", (), {"port": 9123, "open_browser": False})]


def test_web_opens_by_default_on_any_free_port(served):
    assert main(["web"]) == 0
    assert served == [("app", (), {"port": 0, "open_browser": True})]


def test_web_with_a_sub_file_opens_it_filled_in(served, tmp_path):
    sub = tmp_path / "nar.sub"
    sub.write_text(SUB.read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["web", str(sub), "-d", str(tmp_path), "--no-browser"]) == 0
    [(kind, args, kwargs)] = served
    assert kind == "local"
    assert args == (sub,)
    assert kwargs == {"manuscript_dirs": (tmp_path,), "port": 0, "open_browser": False}


def test_web_refuses_a_missing_file(served, tmp_path, capsys):
    assert main(["web", str(tmp_path / "nope.sub")]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert served == []


def test_web_refuses_a_file_without_a_venue(served, tmp_path, capsys):
    sub = tmp_path / "notes.sub"
    sub.write_text("title: hello\n", encoding="utf-8")
    assert main(["web", str(sub)]) == 1
    assert "@venue" in capsys.readouterr().err
    assert served == []


def test_web_directory_needs_a_sub_file(served, tmp_path, capsys):
    assert main(["web", "-d", str(tmp_path)]) == 2
    assert "needs a .sub file" in capsys.readouterr().err
    assert served == []


def test_web_refuses_a_missing_directory(served, tmp_path, capsys):
    sub = tmp_path / "nar.sub"
    sub.write_text(SUB.read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["web", str(sub), "-d", str(tmp_path / "missing")]) == 1
    assert "not a directory" in capsys.readouterr().err
    assert served == []
