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
    assert kwargs == {"manuscript_dirs": (tmp_path,), "port": 0, "open_browser": False, "token": None, "idle_timeout": None}


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


# -- the localhost link 'subfile' and 'autofill' print -----------------------


@pytest.fixture
def spawned(monkeypatch):
    """Pretend stdout is a terminal and record the preview server instead of starting it."""
    import subprocess

    calls: list[tuple[list, dict]] = []
    monkeypatch.delenv("PAPERPUSH_NO_WEB_LINK", raising=False)
    monkeypatch.setattr("paperpush.cli._stdout_is_terminal", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    return calls


def test_subfile_prints_a_link_and_serves_the_file_in_the_background(spawned, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["subfile", "biorxiv"]) == 0
    out = capsys.readouterr().out
    [(cmd, kw)] = spawned
    token = kw["env"]["PAPERPUSH_WEB_TOKEN"]
    port = cmd[cmd.index("--port") + 1]
    assert f"http://127.0.0.1:{port}/?local={token}#/v/biorxiv" in out
    assert cmd[cmd.index("web") + 1] == str((tmp_path / "biorxiv.sub").resolve())
    assert "--idle-timeout" in cmd and kw["start_new_session"]


def test_no_link_when_turned_off(spawned, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PAPERPUSH_NO_WEB_LINK", "1")
    assert main(["subfile", "biorxiv"]) == 0
    assert not spawned and "127.0.0.1" not in capsys.readouterr().out


def test_web_passes_the_token_and_idle_timeout_on(served, monkeypatch):
    monkeypatch.setenv("PAPERPUSH_WEB_TOKEN", "tok")
    assert main(["web", str(SUB), "--no-browser", "--idle-timeout", "60"]) == 0
    [(_, _, kw)] = served
    assert kw["token"] == "tok" and kw["idle_timeout"] == 60
