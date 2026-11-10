"""paperpush validate --disable-NAME: turning checks off by ID, family, or group."""

from __future__ import annotations

import importlib

import pytest

from paperpush import ai_validate, check_ids
from paperpush.cli import _expand_check_flags, main
from paperpush.database import Field, Venue
from paperpush.subfile import SubFile

validate_mod = importlib.import_module("paperpush.validate")
OFFLINE = dict(check_links=False, check_references=False, check_sensitive=False, check_hidden_text=False, check_openreview=False)


@pytest.fixture(autouse=True)
def _clear_model_cache():
    validate_mod._MODELS.clear()
    yield
    validate_mod._MODELS.clear()


def _venue(slug="test_disable"):
    return Venue(slug=slug, name="Test", fields=[Field(id="title", label="Title", type="text", required=True), Field(id="manuscript_file", label="Manuscript", type="file")])


def _report(values, **flags):
    return validate_mod.validate_report(SubFile(venue="test_disable", values=values), _venue(), **{**OFFLINE, "check_manuscript": False, **flags})


def _result(report, check_id):
    return next(r for r in report.results if r.id == check_id)


@pytest.mark.parametrize(
    "given, expected",
    [
        (["bibliography.age"], ["bibliography.age"]),
        (["prose"], ["prose"]),
        (["ai.notes"], ["ai.notes"]),
        (["cover-letter.date"], ["cover_letter.date"]),
        (["ai.reference_source.*"], ["ai.reference_source"]),
        (["references", "manuscript", "references"], ["references", "manuscript"]),
    ],
)
def test_normalize_accepts_ids_families_and_groups(given, expected):
    assert check_ids.normalize(given) == expected


@pytest.mark.parametrize("name", ["bibliography.agee", "ai.not", "nonsense", "not_implemented"])
def test_normalize_rejects_what_names_no_check(name):
    with pytest.raises(ValueError, match="unknown check"):
        check_ids.normalize([name])


def test_normalize_suggests_the_closest_id():
    with pytest.raises(ValueError, match="did you mean bibliography.age"):
        check_ids.normalize(["bibliography.agee"])


def test_matching_stops_at_a_dot():
    assert check_ids.matching("ai.notes.general", ["ai.notes"]) == "ai.notes"
    assert check_ids.matching("ai.reference_source.3", ["ai.reference_source"]) == "ai.reference_source"
    assert check_ids.matching("ai.notes_extra", ["ai.notes"]) == ""


def test_a_disabled_check_does_not_run_and_says_why():
    ran = []
    checks = validate_mod.Checks(disabled=["prose"])
    checks.run("prose.spelling", "Spelling", "Prose", lambda: ran.append(1))
    assert not ran
    assert checks.results[0].status == "unchecked" and checks.results[0].detail == "switched off (--disable-prose)"


def test_a_disabled_checks_errors_are_dropped():
    on = _report({})
    assert any(i.field == "title" for i in on.errors)
    off = _report({}, disable=["form.required"])
    assert not any(i.field == "title" for i in off.errors)
    assert _result(off, "form.required").status == "unchecked"


def test_not_applicable_stays_not_applicable():
    checks = validate_mod.Checks(disabled=["prose"])
    checks.record("prose.x", "X", "Prose", "not_applicable", detail="nothing to read")
    assert checks.results[0].status == "not_applicable" and checks.results[0].detail == "nothing to read"


def test_unknown_name_raises_from_the_api():
    with pytest.raises(ValueError, match="unknown check"):
        _report({}, disable=["nope"])


def test_disabled_ai_tasks_are_not_sent(tmp_path, monkeypatch):
    path = tmp_path / "m.txt"
    path.write_text("x", encoding="utf-8")
    checks = validate_mod.Checks(disabled=["ai.notes"])
    checks.needs_ai(ai_validate.AiTask("ai.notes.general", "G", "x", "i", [str(path)]))
    checks.needs_ai(ai_validate.AiTask("ai.prose", "P", "x", "i", [str(path)]))
    sent = {}
    monkeypatch.setattr(ai_validate, "run_api", lambda tasks, *a, **k: sent.update({t.id: 1 for t in tasks}) or {})
    tasks = ai_validate.resolve(checks, Venue(slug="j", name="J", fields=[]), {}, ai_validate.AiOptions.api("anthropic", "m"))
    assert list(sent) == ["ai.prose"] and [t.id for t in tasks] == ["ai.prose"]
    assert next(r for r in checks.results if r.id == "ai.notes.general").status == "unchecked"


def test_a_check_others_read_keeps_feeding_them():
    checks = validate_mod.Checks(disabled=["integrity.dois"])
    ran = []
    real = checks.run("integrity.dois", "DOIs", "Integrity", lambda: ran.append(1) or [])
    assert ran and real.status == "passed"
    assert checks.results[0].status == "unchecked"


def test_every_reference_check_disabled_makes_no_lookups(monkeypatch, tmp_path):
    manuscript = tmp_path / "m.txt"
    manuscript.write_text("text", encoding="utf-8")
    monkeypatch.setattr(validate_mod, "_reference_issues", lambda *a, **k: pytest.fail("looked up references"))
    report = _report({"title": "T", "manuscript_file": str(manuscript)}, check_references=True, disable=["integrity"])
    assert "--disable-integrity" in _result(report, "integrity.dois").detail


def test_groups_set_the_broad_switches():
    report = _report({"title": "T"}, check_manuscript=True, disable=["manuscript"])
    assert _result(report, "guidelines").detail == "switched off (--disable-manuscript)"


def test_flags_are_expanded_only_for_validate(capsys):
    assert _expand_check_flags(["validate", "x.sub", "--disable-prose"]) == ["validate", "x.sub", "--disable=prose"]
    assert _expand_check_flags(["subfile", "--disable-prose"]) == ["subfile", "--disable-prose"]
    assert _expand_check_flags(["validate", "--dont-check-links"]) == ["validate", "--disable=integrity.links"]
    assert "--dont-check-links is now --disable-integrity.links" in capsys.readouterr().err


def _sub(tmp_path, monkeypatch, body="title: A study\n"):
    import paperpush.cli as cli

    monkeypatch.setattr(cli, "get_venue", lambda slug: _venue())
    sub = tmp_path / "p.sub"
    sub.write_text("@venue: test_disable\n" + body, encoding="utf-8")
    return str(sub)


OFF = ["--disable-references", "--disable-integrity.links", "--disable-manuscript"]


def test_cli_disabling_an_error_lets_it_pass(tmp_path, monkeypatch):
    sub = _sub(tmp_path, monkeypatch, body="")
    assert main(["validate", sub, *OFF]) == 1
    assert main(["validate", sub, *OFF, "--disable-form.required"]) == 0


def test_cli_unknown_check_exits_2(tmp_path, monkeypatch, capsys):
    sub = _sub(tmp_path, monkeypatch)
    assert main(["validate", sub, "--disable-prosee"]) == 2
    assert "did you mean prose" in capsys.readouterr().err


def test_cli_disable_is_refused_with_web(tmp_path, monkeypatch, capsys):
    sub = _sub(tmp_path, monkeypatch)
    assert main(["validate", sub, "--disable-prose", "--web"]) == 1
    assert "--web" in capsys.readouterr().err


def test_cli_rerun_command_keeps_the_disabled_checks(tmp_path, monkeypatch, capsys):
    sub = _sub(tmp_path, monkeypatch)
    tasks = tmp_path / "tasks.json"
    main(["validate", sub, "--disable-integrity.links", "--disable-references", "--disable-prose", "--ai-tasks", str(tasks)])
    assert "--disable-prose" in capsys.readouterr().out


def test_mcp_takes_the_same_names(tmp_path, monkeypatch):
    from paperpush import mcp_server

    sub = _sub(tmp_path, monkeypatch, body="")
    monkeypatch.setattr(mcp_server, "get_venue", lambda slug: _venue(), raising=False)
    out = mcp_server.validate_subfile(sub, check_links=False, check_references=False, check_manuscript=False, disable=["form.required"])
    assert next(c for c in out["checks"] if c["id"] == "form.required")["status"] == "unchecked"


# --enable-NAME, and lists of names


def test_flags_expand_enable_but_keep_the_network_flag():
    assert _expand_check_flags(["validate", "x.sub", "--enable-prose,citations"]) == ["validate", "x.sub", "--enable=prose,citations"]
    assert _expand_check_flags(["submit", "x.sub", "--enable-network-checks"]) == ["submit", "x.sub", "--enable-network-checks"]


def test_normalize_splits_commas():
    assert check_ids.normalize(["prose, bibliography.age", "citations"]) == ["prose", "bibliography.age", "citations"]


def test_normalize_reads_a_text_file(tmp_path):
    listing = tmp_path / "checks.txt"
    listing.write_text("# ours\nprose  # spelling and all\nbibliography.age, cover-letter.date\n\nai.notes citations\n", encoding="utf-8")
    assert check_ids.normalize([str(listing)]) == ["prose", "bibliography.age", "cover_letter.date", "ai.notes", "citations"]


def test_normalize_reports_a_bad_name_in_a_file(tmp_path):
    listing = tmp_path / "checks.txt"
    listing.write_text("prosee\n", encoding="utf-8")
    with pytest.raises(ValueError, match="did you mean prose"):
        check_ids.normalize([str(listing)])


def test_normalize_rejects_a_missing_file():
    with pytest.raises(ValueError, match="no such file"):
        check_ids.normalize(["does/not/exist.txt"])


@pytest.mark.parametrize("given", [",", " , "])
def test_normalize_rejects_a_list_with_no_names(given):
    with pytest.raises(ValueError, match="no check names"):
        check_ids.normalize([given])


def test_an_empty_file_is_an_error_not_every_check(tmp_path):
    listing = tmp_path / "checks.txt"
    listing.write_text("# nothing yet\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no check names"):
        check_ids.normalize([str(listing)])


def test_a_valid_name_is_not_read_as_a_file(tmp_path, monkeypatch):
    (tmp_path / "manuscript").write_text("prose\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert check_ids.normalize(["manuscript"]) == ["manuscript"]


@pytest.mark.parametrize(
    "disable, enable, check_id, runs",
    [
        ([], ["prose"], "prose.spelling", True),
        ([], ["prose"], "citations.order", False),
        ([], ["prose.spelling"], "prose.numbers", False),
        (["prose.spelling"], ["prose"], "prose.spelling", False),
        (["prose.spelling"], ["prose"], "prose.numbers", True),
        (["prose"], ["prose.spelling"], "prose.spelling", True),
        (["prose"], ["prose.spelling"], "prose.numbers", False),
    ],
)
def test_more_specific_name_wins(disable, enable, check_id, runs):
    assert (check_ids.off_reason(check_id, disable, enable) == "") is runs


@pytest.mark.parametrize(
    "disable, enable",
    [(["prose"], ["prose"]), (["manuscript"], ["prose"]), (["references"], ["integrity.dois"]), (["manuscript"], ["manuscript"])],
)
def test_contradictions_are_errors(disable, enable):
    with pytest.raises(ValueError, match="contradict|cannot be combined"):
        check_ids.select(disable, enable)


def test_a_group_can_be_narrowed():
    assert check_ids.select(["references"], ["integrity"]) == (["references"], ["integrity"])


def test_group_names_expand_for_matching():
    enable = check_ids.expand_enabled(["manuscript", "references"])
    assert "prose" in enable and "form" not in enable and "integrity.dois" in enable and "integrity.links" not in enable


def test_a_check_not_enabled_does_not_run_and_says_why():
    ran = []
    checks = validate_mod.Checks(enabled=["citations"])
    checks.run("prose.spelling", "Spelling", "Prose", lambda: ran.append(1))
    assert not ran
    assert checks.results[0].status == "unchecked" and "--enable" in checks.results[0].detail


def test_enabling_ai_keeps_the_manuscript_checks_that_queue_tasks():
    ran = []
    checks = validate_mod.Checks(enabled=["ai.template"])
    checks.run("layout.template", "Template", "Layout", lambda: ran.append(1) or [])
    assert ran and checks.results[0].status == "unchecked"


def test_enabling_ai_does_not_override_a_disable():
    ran = []
    checks = validate_mod.Checks(disabled=["layout"], enabled=["ai.template"])
    checks.run("layout.template", "Template", "Layout", lambda: ran.append(1) or [])
    assert not ran


def test_enable_keeps_errors_of_other_checks_out(tmp_path):
    report = _report({}, enable=["form.options"])
    assert not any(i.field == "title" for i in report.errors)
    assert _result(report, "form.required").status == "unchecked"


def test_enabling_only_the_form_skips_the_manuscript_stage(monkeypatch):
    monkeypatch.setattr(validate_mod, "_run_manuscript_stages", lambda *a, **k: pytest.fail("read the manuscript"))
    report = _report({"title": "T"}, check_manuscript=True, enable=["form"])
    assert "--enable" in _result(report, "guidelines").detail


def test_enable_list_without_reference_checks_makes_no_lookups(monkeypatch, tmp_path):
    manuscript = tmp_path / "m.txt"
    manuscript.write_text("text", encoding="utf-8")
    monkeypatch.setattr(validate_mod, "_reference_issues", lambda *a, **k: pytest.fail("looked up references"))
    report = _report({"title": "T", "manuscript_file": str(manuscript)}, check_references=True, enable=["form"])
    assert "--enable" in _result(report, "integrity.dois").detail


def test_enabling_the_unverified_references_task_keeps_the_lookups(monkeypatch, tmp_path):
    manuscript = tmp_path / "m.txt"
    manuscript.write_text("text", encoding="utf-8")
    called = []
    monkeypatch.setattr(validate_mod, "_reference_issues", lambda *a, **k: called.append(1) or [])
    _report({"title": "T", "manuscript_file": str(manuscript)}, check_references=True, enable=["ai.unverified_references"])
    assert called


def test_cli_enable_ignores_other_failures(tmp_path, monkeypatch):
    sub = _sub(tmp_path, monkeypatch, body="")
    assert main(["validate", sub, *OFF]) == 1
    assert main(["validate", sub, "--enable-form.options"]) == 0


def test_cli_takes_commas_and_files(tmp_path, monkeypatch):
    sub = _sub(tmp_path, monkeypatch, body="")
    listing = tmp_path / "checks.txt"
    listing.write_text("form.required\n", encoding="utf-8")
    assert main(["validate", sub, *OFF, "--disable-form.schema,form.required"]) == 0
    assert main(["validate", sub, *OFF, f"--disable={listing}"]) == 0
    assert main(["validate", sub, *OFF, "--disable", str(listing)]) == 0
    assert main(["validate", sub, f"--enable-{listing}"]) == 1


def test_cli_enable_conflict_exits_2(tmp_path, monkeypatch, capsys):
    sub = _sub(tmp_path, monkeypatch)
    assert main(["validate", sub, "--enable-prose", "--disable-prose"]) == 2
    assert "contradict" in capsys.readouterr().err


def test_cli_enable_is_refused_with_web(tmp_path, monkeypatch, capsys):
    sub = _sub(tmp_path, monkeypatch)
    assert main(["validate", sub, "--enable-prose", "--web"]) == 1
    assert "--web" in capsys.readouterr().err


def test_cli_ai_needs_a_manuscript_check_in_the_enable_list(tmp_path, monkeypatch, capsys):
    sub = _sub(tmp_path, monkeypatch)
    assert main(["validate", sub, "--enable-form", "--ai-only"]) == 1
    assert "--enable" in capsys.readouterr().err


def test_mcp_takes_enable(tmp_path, monkeypatch):
    from paperpush import mcp_server

    sub = _sub(tmp_path, monkeypatch, body="")
    monkeypatch.setattr(mcp_server, "get_venue", lambda slug: _venue(), raising=False)
    out = mcp_server.validate_subfile(sub, check_links=False, check_references=False, check_manuscript=False, enable=["form.options"])
    assert next(c for c in out["checks"] if c["id"] == "form.required")["status"] == "unchecked"


def test_cli_does_not_call_a_partial_run_ready_to_submit(tmp_path, monkeypatch, capsys):
    sub = _sub(tmp_path, monkeypatch)
    assert main(["validate", sub, "--enable-form.options"]) == 0
    captured = capsys.readouterr()
    assert "ready to submit." not in (captured.out + captured.err).replace("does not mean it is ready to submit.", "")
    assert "did not run" in captured.out + captured.err

