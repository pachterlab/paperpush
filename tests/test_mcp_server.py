"""Tests for the MCP server's tool functions.

The tools are plain functions (see :mod:`paperpush.mcp_server`), so these call
them directly -- no MCP client, and no ``mcp`` install needed for anything but
the one registration test at the bottom.

The autouse ``_isolate_user_state`` fixture in ``conftest`` already points
credential storage at ``tmp_path`` with the keyring disabled, so the login tests
here never touch the developer's real keychain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paperpush import credentials, mcp_server
from paperpush.database import list_venues

VENUE = "biorxiv"


# --- helpers ---------------------------------------------------------------


def test_resolve_leaves_absolute_paths_alone(tmp_path):
    assert mcp_server._resolve(str(tmp_path / "a.sub")) == tmp_path / "a.sub"


def test_resolve_uses_base_for_relative_paths(tmp_path):
    assert mcp_server._resolve("a.sub", str(tmp_path)) == tmp_path / "a.sub"


def test_slug_from_reference_accepts_slug_and_filename():
    assert mcp_server._slug_from_reference(VENUE) == VENUE
    assert mcp_server._slug_from_reference(f"{VENUE}.sub") == VENUE
    assert mcp_server._slug_from_reference(f"/tmp/nowhere/{VENUE}.sub") == VENUE


def test_slug_from_reference_prefers_the_venue_recorded_in_the_file(tmp_path):
    """The file is the authority on which venue it targets, not its name."""
    path = tmp_path / "renamed.sub"
    mcp_server.create_subfile(VENUE, str(path))
    assert mcp_server._slug_from_reference(str(path)) == VENUE


def test_slug_from_reference_rejects_blank():
    with pytest.raises(ValueError, match="required"):
        mcp_server._slug_from_reference("   ")


# --- venue catalogue -------------------------------------------------------


def test_list_supported_venues_matches_the_database():
    listed = mcp_server.list_supported_venues()
    assert [v["slug"] for v in listed] == [v.slug for v in list_venues()]
    assert all(v["field_count"] > 0 for v in listed)


def test_describe_venue_carries_fields_and_roles():
    described = mcp_server.describe_venue(VENUE)
    assert described["slug"] == VENUE
    assert described["default_subfile_name"] == f"{VENUE}.sub"
    roles = {f["id"]: f["role"] for f in described["fields"]}
    assert roles["title"] == "extract"
    assert roles["manuscript_file"] == "filemap"
    assert roles["license"] == "never"


def test_unknown_venue_error_lists_the_supported_ones():
    with pytest.raises(ValueError, match="unknown venue 'nope'") as exc:
        mcp_server.describe_venue("nope")
    assert VENUE in str(exc.value)


def test_field_options_lists_a_flat_choice_field():
    result = mcp_server.field_options(VENUE, "license")
    assert result["hierarchical"] is False
    assert result["options"]


def test_field_options_rejects_an_unknown_field():
    with pytest.raises(ValueError, match="has no field 'nope'"):
        mcp_server.field_options(VENUE, "nope")


def test_field_options_rejects_a_path_on_a_flat_field():
    with pytest.raises(ValueError, match="does not take a path"):
        mcp_server.field_options(VENUE, "license", ["Biological sciences"])


# --- .sub files ------------------------------------------------------------


def test_create_subfile_writes_a_template(tmp_path):
    path = tmp_path / "out.sub"
    result = mcp_server.create_subfile(VENUE, str(path))
    assert result["path"] == str(path)
    assert result["venue"] == VENUE
    assert "title:" in path.read_text(encoding="utf-8")


def test_create_subfile_will_not_clobber_without_overwrite(tmp_path):
    path = tmp_path / "out.sub"
    mcp_server.create_subfile(VENUE, str(path))
    with pytest.raises(ValueError, match="already exists"):
        mcp_server.create_subfile(VENUE, str(path))
    # ...but says so, and obliges when asked explicitly.
    assert mcp_server.create_subfile(VENUE, str(path), overwrite=True)["path"] == str(path)


def test_read_subfile_reports_the_required_fields_still_empty(tmp_path):
    path = tmp_path / "out.sub"
    mcp_server.create_subfile(VENUE, str(path))
    result = mcp_server.read_subfile(str(path))
    assert result["venue"] == VENUE
    assert "title" in result["empty_required_fields"]


def test_read_subfile_resolves_relative_paths_against_the_manuscript_dir(tmp_path):
    """A relative path must not be read against the *server's* cwd."""
    mcp_server.create_subfile(VENUE, str(tmp_path / f"{VENUE}.sub"))
    result = mcp_server.read_subfile(f"{VENUE}.sub", manuscript_dir=str(tmp_path))
    assert result["path"] == str(tmp_path / f"{VENUE}.sub")


def test_read_subfile_rejects_a_missing_file(tmp_path):
    with pytest.raises(ValueError, match="no .sub file at"):
        mcp_server.read_subfile(str(tmp_path / "absent.sub"))


def test_validate_subfile_flags_an_unfilled_template(tmp_path):
    path = tmp_path / "out.sub"
    mcp_server.create_subfile(VENUE, str(path))
    result = mcp_server.validate_subfile(str(path), check_links=False, check_sensitive=False)
    assert result["ok"] is False
    assert any(issue["field"] == "title" for issue in result["errors"])


# --- autofill --------------------------------------------------------------


def _values(**fields) -> dict:
    return {"fields": [{"id": k, "value": v, "confidence": "high", "source": "test"} for k, v in fields.items()]}


def test_autofill_writes_extracted_values(tmp_path, manuscript_pdf):
    path = tmp_path / f"{VENUE}.sub"
    mcp_server.create_subfile(VENUE, str(path))
    result = mcp_server.autofill_subfile(
        subfile=str(path),
        manuscript_dir=str(tmp_path),
        values=_values(title="A Study of Studies"),
    )
    assert result["written"] is True
    assert [o["id"] for o in result["filled"]] == ["title"]
    assert "A Study of Studies" in path.read_text(encoding="utf-8")


def test_autofill_resolves_file_paths_against_the_manuscript_dir(tmp_path, manuscript_pdf):
    path = tmp_path / f"{VENUE}.sub"
    mcp_server.create_subfile(VENUE, str(path))
    result = mcp_server.autofill_subfile(
        subfile=str(path),
        manuscript_dir=str(tmp_path),
        values=_values(manuscript_file=manuscript_pdf.name),
    )
    written = next(o for o in result["filled"] + result["needs_review"] if o["id"] == "manuscript_file")
    assert written["value"] == str(manuscript_pdf)


def test_autofill_never_touches_a_policy_field(tmp_path, manuscript_pdf):
    """`never`-role fields are the author's to set; the gate is not overridable."""
    path = tmp_path / f"{VENUE}.sub"
    mcp_server.create_subfile(VENUE, str(path))
    result = mcp_server.autofill_subfile(
        subfile=str(path),
        manuscript_dir=str(tmp_path),
        values=_values(license="CC0 1.0 Universal (CC0 1.0) Public Domain Dedication"),
    )
    assert result["filled"] == []
    assert [o["id"] for o in result["skipped"]] == ["license"]


def test_autofill_respects_the_confidence_floor(tmp_path, manuscript_pdf):
    path = tmp_path / f"{VENUE}.sub"
    mcp_server.create_subfile(VENUE, str(path))
    result = mcp_server.autofill_subfile(
        subfile=str(path),
        manuscript_dir=str(tmp_path),
        values={"fields": [{"id": "title", "value": "Maybe", "confidence": "low"}]},
        min_confidence="high",
    )
    assert result["filled"] == []
    assert [o["id"] for o in result["skipped"]] == ["title"]
    assert "Maybe" not in path.read_text(encoding="utf-8")


def test_autofill_dry_run_leaves_the_file_alone(tmp_path, manuscript_pdf):
    path = tmp_path / f"{VENUE}.sub"
    mcp_server.create_subfile(VENUE, str(path))
    before = path.read_text(encoding="utf-8")
    result = mcp_server.autofill_subfile(
        subfile=str(path),
        manuscript_dir=str(tmp_path),
        values=_values(title="Not Written"),
        dry_run=True,
    )
    assert result["written"] is False
    assert path.read_text(encoding="utf-8") == before


def test_autofill_starts_from_a_fresh_template_when_the_sub_is_missing(tmp_path, manuscript_pdf):
    result = mcp_server.autofill_subfile(
        subfile=f"{VENUE}.sub",  # relative -> resolved against manuscript_dir
        manuscript_dir=str(tmp_path),
        values=_values(title="Brand New"),
    )
    assert result["started_from_fresh_template"] is True
    assert (tmp_path / f"{VENUE}.sub").exists()


def test_autofill_rejects_a_missing_manuscript_dir(tmp_path):
    with pytest.raises(ValueError, match="manuscript directory not found"):
        mcp_server.autofill_subfile(subfile=f"{VENUE}.sub", manuscript_dir=str(tmp_path / "absent"), values=_values(title="x"))


# --- credentials -----------------------------------------------------------


def test_login_status_reports_nothing_stored():
    assert mcp_server.login_status()["logged_in_venues"] == []


def test_login_status_lists_a_stored_login():
    credentials.save_credential(VENUE, "ada@example.com", "hunter2")
    status = mcp_server.login_status()
    assert VENUE in status["logged_in_venues"]
    assert status["logins"][0]["identity"] == "ada@example.com"


def test_login_status_never_returns_the_password():
    credentials.save_credential(VENUE, "ada@example.com", "hunter2")
    assert "hunter2" not in repr(mcp_server.login_status())
    assert "hunter2" not in repr(mcp_server.login_status(VENUE))


def test_login_status_for_one_venue_gives_the_command_when_signed_out():
    status = mcp_server.login_status(VENUE)
    assert status["logged_in"] is False
    assert status["login_command"] == f"paperpush login {VENUE}"


def test_login_status_for_one_venue_when_signed_in():
    credentials.save_credential(VENUE, "ada@example.com", "hunter2")
    status = mcp_server.login_status(VENUE)
    assert status["logged_in"] is True
    assert status["identity"] == "ada@example.com"


def test_login_status_accepts_a_subfile_reference(tmp_path):
    path = tmp_path / f"{VENUE}.sub"
    mcp_server.create_subfile(VENUE, str(path))
    assert mcp_server.login_status(str(path))["venue"] == VENUE


def test_login_short_circuits_when_already_logged_in(monkeypatch):
    credentials.save_credential(VENUE, "ada@example.com", "hunter2")

    def _fail(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("login must not shell out when credentials exist")

    monkeypatch.setattr(mcp_server.subprocess, "run", _fail)
    result = mcp_server.login(VENUE)
    assert result["status"] == "already_logged_in"
    assert result["identity"] == "ada@example.com"


def test_login_hands_back_the_command_when_it_cannot_prompt(monkeypatch):
    """With no credentials in the environment there is nothing to prompt on."""
    monkeypatch.delenv("PAPERPUSH_USERNAME", raising=False)
    monkeypatch.delenv("PAPERPUSH_PASSWORD", raising=False)

    def _fail(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("login must not shell out without credentials to use")

    monkeypatch.setattr(mcp_server.subprocess, "run", _fail)
    result = mcp_server.login(VENUE)
    assert result["status"] == "action_required"
    assert result["command"] == f"paperpush login {VENUE}"


def test_login_takes_no_password_argument():
    """A credential passed as a tool argument would land in the transcript."""
    import inspect

    params = set(inspect.signature(mcp_server.login).parameters)
    assert not params & {"username", "password", "user", "secret"}


def test_login_runs_the_cli_when_the_environment_supplies_credentials(monkeypatch):
    monkeypatch.setenv("PAPERPUSH_USERNAME", "ada@example.com")
    monkeypatch.setenv("PAPERPUSH_PASSWORD", "hunter2")
    calls = []

    class _Completed:
        returncode = 0
        stdout = "Stored credentials for biorxiv."
        stderr = ""

    def _run(command, **kwargs):
        calls.append((command, kwargs))
        # Stand in for the real sign-in having stored something.
        credentials.save_credential(VENUE, "ada@example.com", "hunter2")
        return _Completed()

    monkeypatch.setattr(mcp_server.subprocess, "run", _run)
    result = mcp_server.login(VENUE, verify=False)
    assert result["status"] == "logged_in"
    command, kwargs = calls[0]
    assert command[:4] == [mcp_server.sys.executable, "-m", "paperpush", "login"]
    assert "--no-verify" in command
    # No terminal to prompt on: an unexpected prompt must EOF, not hang.
    assert kwargs["stdin"] is mcp_server.subprocess.DEVNULL


def test_login_reports_a_timeout_as_a_run_it_yourself(monkeypatch):
    monkeypatch.setenv("PAPERPUSH_USERNAME", "ada@example.com")
    monkeypatch.setenv("PAPERPUSH_PASSWORD", "hunter2")

    def _timeout(command, **kwargs):
        raise mcp_server.subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(mcp_server.subprocess, "run", _timeout)
    result = mcp_server.login(VENUE, timeout_seconds=1)
    assert result["status"] == "timed_out"
    assert result["command"].startswith("paperpush login")


def test_login_reports_a_failed_sign_in(monkeypatch):
    monkeypatch.setenv("PAPERPUSH_USERNAME", "ada@example.com")
    monkeypatch.setenv("PAPERPUSH_PASSWORD", "wrong")

    class _Completed:
        returncode = 1
        stdout = ""
        stderr = "error: sign-in check failed"

    monkeypatch.setattr(mcp_server.subprocess, "run", lambda *a, **k: _Completed())
    result = mcp_server.login(VENUE)
    assert result["status"] == "failed"
    assert "sign-in check failed" in result["output"]


# --- submitting ------------------------------------------------------------

# A committed, fully-filled sample that passes validation, so the submit tests
# exercise the launch path rather than tripping the preflight.
VALID_SUB = Path(__file__).resolve().parent / "sub_files" / VENUE / f"{VENUE}.sub"


class FakePopen:
    """Stand-in for a launched `paperpush submit`, recording how it was started."""

    instances: list["FakePopen"] = []

    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.pid = 4242 + len(FakePopen.instances)
        self.stdin = None
        self.terminated = False
        self.killed = False
        self._exit_code = None
        FakePopen.instances.append(self)

    def poll(self):
        return self._exit_code

    def terminate(self):
        self.terminated = True
        self._exit_code = -15

    def kill(self):  # pragma: no cover - only on a stuck child
        self.killed = True
        self._exit_code = -9

    def wait(self, timeout=None):
        return self._exit_code


@pytest.fixture
def fake_launch(monkeypatch):
    """Patch out the real Popen and keep the run registry clean."""
    FakePopen.instances = []
    monkeypatch.setattr(mcp_server.subprocess, "Popen", FakePopen)
    mcp_server._RUNS.clear()
    yield FakePopen
    mcp_server._RUNS.clear()


@pytest.fixture
def no_launch(monkeypatch):
    """Assert nothing is launched at all."""

    def _fail(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("submit must not open a browser on the preflight path")

    monkeypatch.setattr(mcp_server.subprocess, "Popen", _fail)
    mcp_server._RUNS.clear()
    yield
    mcp_server._RUNS.clear()


def test_submit_refuses_a_subfile_that_does_not_validate(tmp_path, no_launch):
    path = tmp_path / f"{VENUE}.sub"
    mcp_server.create_subfile(VENUE, str(path))  # a blank template: required fields empty
    result = mcp_server.submit(str(path))
    assert result["status"] == "blocked"
    assert result["errors"]


def test_submit_refuses_without_a_stored_login(no_launch):
    """Launching would stall on a password prompt with no terminal to answer it."""
    result = mcp_server.submit(str(VALID_SUB))
    assert result["status"] == "action_required"
    assert result["command"] == f"paperpush login {VENUE}"


def test_submit_launches_and_returns_a_handle(fake_launch):
    credentials.save_credential(VENUE, "ada@example.com", "hunter2")
    result = mcp_server.submit(str(VALID_SUB))
    assert result["status"] == "running"
    assert result["pid"] in mcp_server._RUNS
    assert Path(result["log_path"]).exists()
    command = fake_launch.instances[0].command
    assert command[:4] == [mcp_server.sys.executable, "-m", "paperpush", "submit"]
    assert command[4] == str(VALID_SUB)


def test_submit_holds_the_childs_stdin_open(fake_launch):
    """The whole design hinges on this.

    stdin cannot be inherited (this process's stdin is the client's JSON-RPC
    stream) and cannot be DEVNULL (hold_open's `input()` would EOF, the process
    would exit, and Playwright would close the browser). It must be a pipe.
    """
    credentials.save_credential(VENUE, "ada@example.com", "hunter2")
    mcp_server.submit(str(VALID_SUB))
    kwargs = fake_launch.instances[0].kwargs
    assert kwargs["stdin"] is mcp_server.subprocess.PIPE
    assert kwargs["stdin"] is not mcp_server.subprocess.DEVNULL


def test_submit_detaches_from_the_servers_process_group(fake_launch):
    """Quitting the client must not take the author's browser down mid-review."""
    credentials.save_credential(VENUE, "ada@example.com", "hunter2")
    mcp_server.submit(str(VALID_SUB))
    assert fake_launch.instances[0].kwargs["start_new_session"] is True


def test_submit_passes_the_optional_flags_through(fake_launch):
    credentials.save_credential(VENUE, "ada@example.com", "hunter2")
    mcp_server.submit(str(VALID_SUB), headless=True, new_session=True, close_on_failure=True)
    command = fake_launch.instances[0].command
    assert {"--headless", "--new-session", "--close-on-failure"} <= set(command)


def test_submit_defaults_to_a_visible_browser_it_leaves_open(fake_launch):
    credentials.save_credential(VENUE, "ada@example.com", "hunter2")
    mcp_server.submit(str(VALID_SUB))
    command = fake_launch.instances[0].command
    assert "--headless" not in command
    assert "--close-on-failure" not in command


def test_submit_status_reports_a_running_submission(fake_launch):
    credentials.save_credential(VENUE, "ada@example.com", "hunter2")
    pid = mcp_server.submit(str(VALID_SUB))["pid"]
    status = mcp_server.submit_status(pid)
    assert status["running"] is True
    assert status["exit_code"] is None
    assert status["venue"] == VENUE


def test_submit_status_surfaces_the_log_tail(fake_launch):
    credentials.save_credential(VENUE, "ada@example.com", "hunter2")
    result = mcp_server.submit(str(VALID_SUB))
    Path(result["log_path"]).write_text("Opening biorxiv…\nStep 3 of 7\n", encoding="utf-8")
    assert mcp_server.submit_status(result["pid"])["log_tail"] == ["Opening biorxiv…", "Step 3 of 7"]


def test_submit_status_with_no_pid_lists_every_run(fake_launch):
    credentials.save_credential(VENUE, "ada@example.com", "hunter2")
    mcp_server.submit(str(VALID_SUB))
    mcp_server.submit(str(VALID_SUB))
    assert len(mcp_server.submit_status()["submissions"]) == 2


def test_submit_status_rejects_an_unknown_pid(fake_launch):
    with pytest.raises(ValueError, match="no submission with pid 999"):
        mcp_server.submit_status(999)


def test_submit_close_ends_the_run(fake_launch):
    credentials.save_credential(VENUE, "ada@example.com", "hunter2")
    pid = mcp_server.submit(str(VALID_SUB))["pid"]
    result = mcp_server.submit_close(pid)
    assert result["status"] == "closed"
    assert fake_launch.instances[0].terminated is True
    assert pid not in mcp_server._RUNS


def test_submit_close_is_honest_about_an_already_finished_run(fake_launch):
    credentials.save_credential(VENUE, "ada@example.com", "hunter2")
    pid = mcp_server.submit(str(VALID_SUB))["pid"]
    fake_launch.instances[0]._exit_code = 1  # the run fell over on its own
    result = mcp_server.submit_close(pid)
    assert result["status"] == "already_finished"
    assert result["exit_code"] == 1
    assert fake_launch.instances[0].terminated is False


def test_submit_close_rejects_an_unknown_pid(fake_launch):
    with pytest.raises(ValueError, match="no submission with pid 999"):
        mcp_server.submit_close(999)


# --- resources and server assembly ------------------------------------------


def test_resources_are_json():
    import json

    assert isinstance(json.loads(mcp_server.venues_resource()), list)
    assert json.loads(mcp_server.venue_resource(VENUE))["slug"] == VENUE


def _registered_tools():
    """The built server's tool list, across both MCP SDK generations.

    ``list_tools`` is a coroutine on MCP SDK 1.x and a plain method on 2.0.
    """
    pytest.importorskip("mcp", reason="the MCP server needs the optional 'mcp' package")
    import inspect

    import anyio

    server = mcp_server.build_server()
    if inspect.iscoroutinefunction(server.list_tools):
        return anyio.run(server.list_tools)
    return server.list_tools()


def test_build_server_registers_every_tool():
    assert {tool.name for tool in _registered_tools()} == {fn.__name__ for fn in mcp_server.TOOLS}


def test_build_server_describes_every_tool():
    """The SDK derives a tool's description from its docstring -- so it needs one."""
    for tool in _registered_tools():
        assert tool.description, f"{tool.name} has no description"


def test_build_server_exposes_typed_arguments():
    """A tool the model can call correctly needs a real input schema."""
    by_name = {tool.name: tool for tool in _registered_tools()}
    tool = by_name["validate_subfile"]
    # Renamed from inputSchema to input_schema in MCP SDK 2.0.
    schema = getattr(tool, "input_schema", None) or tool.inputSchema
    assert set(schema["properties"]) == {"subfile", "manuscript_dir", "check_links", "check_sensitive"}
    assert schema["required"] == ["subfile"]
