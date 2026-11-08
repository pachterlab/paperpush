"""Revised manuscripts: detection, submit's warning, and the guidelines' revision rules."""

from __future__ import annotations

import pytest

from paperpush import revision
from paperpush.requirements import ManuscriptRequirements, RevisionRules


@pytest.mark.parametrize(
    "name",
    ["response_to_reviewers.docx", "Response to Reviewers.pdf", "reply-to-referees.tex", "rebuttal.pdf", "point_by_point.md", "responses_to_the_reviewers.docx", "response_to_editor.txt"],
)
def test_response_document_names(tmp_path, name):
    assert revision.is_response_document(tmp_path / name)


@pytest.mark.parametrize("name", ["manuscript.pdf", "cover_letter.docx", "cell_corresponding_second.sub.txt", "suggested_reviewers.txt", "response_to_reviewers.png"])
def test_other_names_are_not_response_documents(tmp_path, name):
    assert not revision.is_response_document(tmp_path / name)


def test_initial_submission_has_no_signals(tmp_path):
    (tmp_path / "manuscript.pdf").write_text("x")
    sub = tmp_path / "nature.sub.txt"
    sub.write_text("")
    assert revision.revision_signals([sub, tmp_path / "manuscript.pdf"]) == []


def test_revision_directory_is_a_signal(tmp_path):
    folder = tmp_path / "paper" / "revision2"
    folder.mkdir(parents=True)
    sub = folder / "nature.sub.txt"
    sub.write_text("")
    signals = revision.revision_signals([sub])
    assert len(signals) == 1 and "revision2" in signals[0]


def test_response_document_beside_the_files_is_a_signal(tmp_path):
    (tmp_path / "Response_to_Reviewers.docx").write_text("x")
    sub = tmp_path / "nature.sub.txt"
    sub.write_text("")
    signals = revision.revision_signals([sub])
    assert len(signals) == 1 and "Response_to_Reviewers.docx" in signals[0]


def test_revision_rules_round_trip():
    reqs = ManuscriptRequirements.from_dict("x", {"revision": {"marked_manuscript": "track_changes", "notes": ["Include line numbers."]}})
    assert reqs.revision == RevisionRules(marked_manuscript="track_changes", notes=["Include line numbers."])
    assert reqs.to_dict()["revision"] == {"marked_manuscript": "track_changes", "notes": ["Include line numbers."]}
    assert "revision" not in ManuscriptRequirements.from_dict("x", {}).to_dict()


# --- submit ----------------------------------------------------------------


def _stub_submit(monkeypatch):
    import importlib

    import paperpush.subfile as subfile
    import paperpush.venues as venues
    from paperpush.subfile import SubFile

    validate_mod = importlib.import_module("paperpush.validate")
    ran: list = []
    monkeypatch.setattr(subfile, "load", lambda path: SubFile(venue="biorxiv", values={}))
    monkeypatch.setattr(validate_mod, "validate", lambda sub, venue, **kwargs: [])
    monkeypatch.setattr(venues, "get_runner", lambda venue: (lambda *a, **k: ran.append((a, k))))
    monkeypatch.setattr(venues, "login_required", lambda slug: False)
    return ran


def _revision_sub(tmp_path):
    folder = tmp_path / "revision1"
    folder.mkdir()
    sub = folder / "biorxiv.sub"
    sub.write_text("")
    return sub


def test_submit_stops_on_a_revision_without_a_terminal(tmp_path, monkeypatch, capsys):
    from paperpush.cli import main

    ran = _stub_submit(monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    rc = main(["submit", str(_revision_sub(tmp_path))])
    err = capsys.readouterr().err
    assert rc == 1 and not ran
    assert "looks like a revised manuscript" in err and "initial submissions" in err


def test_submit_asks_and_honours_no(tmp_path, monkeypatch, capsys):
    from paperpush.cli import main

    ran = _stub_submit(monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert main(["submit", str(_revision_sub(tmp_path))]) == 1
    assert not ran


def test_submit_yes_goes_ahead(tmp_path, monkeypatch, capsys):
    from paperpush.cli import main

    ran = _stub_submit(monkeypatch)
    assert main(["submit", str(_revision_sub(tmp_path)), "--yes"]) == 0
    assert ran
    assert "looks like a revised manuscript" in capsys.readouterr().err
