"""Tests for paperpush.venues.recovery_channel: an outside agent fixing a paused run.

The runner side (AgentHandler) blocks on the main thread with a real headless
Chromium, the same as in a real ``submit --recover agent`` run. The agent side
(send_command, which is what the MCP tools call) runs in a second thread and
talks to it only through the recovery directory, as the MCP server process does.
"""

from __future__ import annotations

import argparse
import json
import threading
import time

import pytest
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from paperpush import cli
from paperpush.venues import recovery_channel as ch
from paperpush.venues.recovery import HumanHandler, Resolution, guard, step

FORM = """
<form onsubmit="event.preventDefault(); document.getElementById('out').textContent='form sent'">
  <button type="button" id="next" onclick="document.getElementById('out').textContent='next'">Next</button>
  <button type="button" onclick="document.getElementById('out').textContent='SENT'">Approve Submission</button>
  <input id="name" aria-label="Name">
  <input id="file" type="file" aria-label="Manuscript">
</form>
<div id="out"></div>
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    context.set_default_timeout(300)
    page = context.new_page()
    page.set_content(FORM)
    yield page
    context.close()


def agent(directory, script):
    """Run ``script(send)`` in a thread once the run in ``directory`` pauses; return its replies."""
    replies: list[dict] = []
    errors: list[BaseException] = []

    def send(op, **kw):
        reply = ch.send_command(directory, {"op": op, **kw}, timeout=20)
        replies.append(reply)
        return reply

    def body():
        try:
            deadline = time.monotonic() + 20
            while ch.read_paused(directory) is None:
                if time.monotonic() > deadline:
                    raise AssertionError("the run never paused")
                time.sleep(0.05)
            script(send)
        except BaseException as exc:  # noqa: BLE001 -- re-raised in the test thread
            errors.append(exc)

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread, replies, errors


def finish(thread, errors):
    thread.join(timeout=30)
    assert not thread.is_alive()
    if errors:
        raise errors[0]


def test_agent_fixes_a_renamed_button_and_the_script_continues(page, tmp_path):
    handler = ch.AgentHandler(tmp_path, output=lambda _m: None)
    guarded = guard(page, handler)
    step("Title page")

    def script(send):
        snap = send("snapshot")
        assert snap["failure"]["target"] == "get_by_role('button', name='Save / Continue')"
        assert snap["step"] == "Title page"
        assert 'button "Next"' in snap["aria_snapshot"]
        assert snap["screenshot"] and (tmp_path / snap["screenshot"].rsplit("/", 1)[-1]).exists()
        assert send("click", target={"role": "button", "name": "Next"})["ok"]
        send("resume", resolution="resolved", note="button renamed to Next")

    thread, replies, errors = agent(tmp_path, script)
    guarded.get_by_role("button", name="Save / Continue").click()
    guarded.get_by_role("textbox", name="Name").fill("after")  # the next line runs
    finish(thread, errors)

    assert page.locator("#out").text_content() == "next"
    assert page.locator("#name").input_value() == "after"
    assert ch.read_paused(tmp_path) is None
    [entry] = ch.read_log(tmp_path)
    assert entry["resolution"] == "resolved"
    assert entry["note"] == "button renamed to Next"
    assert entry["agent_actions"] == [{"op": "click", "target": {"role": "button", "name": "Next"}}]


def test_agent_abort_raises_the_original_error(page, tmp_path):
    guarded = guard(page, ch.AgentHandler(tmp_path, output=lambda _m: None))
    thread, _, errors = agent(tmp_path, lambda send: send("resume", resolution="abort"))
    with pytest.raises(PWTimeout):
        guarded.locator("#missing").click()
    finish(thread, errors)


def test_guardrails_refuse_final_submit_enter_and_unlisted_uploads(page, tmp_path):
    allowed = tmp_path / "ms.pdf"
    allowed.write_bytes(b"%PDF-1.4")
    other = tmp_path / "secret.txt"
    other.write_text("no")
    handler = ch.AgentHandler(tmp_path / "run", allowed_uploads=[str(allowed)], output=lambda _m: None)
    guarded = guard(page, handler)

    def script(send):
        refused = send("click", target={"role": "button", "name": "Approve Submission"})
        assert not refused["ok"] and "final submit" in refused["error"]
        by_css = send("click", target={"css": "form button:nth-of-type(2)"})  # same control, no name given
        assert not by_css["ok"] and "final submit" in by_css["error"]
        enter = send("press", target={"label": "Name"}, value="Enter")
        assert not enter["ok"] and "Enter" in enter["error"]
        bad = send("upload", target={"label": "Manuscript"}, value=str(other))
        assert not bad["ok"] and "not one of the files" in bad["error"]
        assert send("upload", target={"label": "Manuscript"}, value=str(allowed))["ok"]
        assert not send("goto", target={"css": "body"})["ok"]  # unknown op
        assert not send("click", target={"css": "#a", "role": "button"})["ok"]  # ambiguous target
        send("resume", resolution="resolved")

    thread, _, errors = agent(tmp_path / "run", script)
    guarded.locator("#missing").click()
    finish(thread, errors)
    assert page.locator("#out").text_content() == ""  # nothing was sent
    assert page.locator("#file").evaluate("el => el.files[0].name") == "ms.pdf"


def test_a_run_nobody_resumes_gives_up(page, tmp_path):
    guarded = guard(page, ch.AgentHandler(tmp_path, pause_timeout=0.5, output=lambda _m: None))
    with pytest.raises(PWTimeout):
        guarded.locator("#missing").click()
    assert ch.read_paused(tmp_path) is None


def test_send_command_refuses_a_run_that_is_not_paused(tmp_path):
    with pytest.raises(ch.ChannelError, match="not paused"):
        ch.send_command(tmp_path, {"op": "snapshot"})


def test_send_command_notices_a_dead_run(tmp_path):
    (tmp_path / ch.PAUSED_FILE).write_text("{}")
    with pytest.raises(ch.ChannelError, match="exited"):
        ch.send_command(tmp_path, {"op": "snapshot"}, timeout=5, is_alive=lambda: False)


@pytest.mark.parametrize(
    "label, final",
    [("Submit", True), ("  approve   submission ", True), ("Finalize", True), ("Submit a new manuscript", False), ("Save / Continue", False), ("Upload Files", False), ("", False)],
)
def test_final_submit_labels(label, final):
    assert ch.is_final_submit_label(label) is final


def test_venue_specific_final_submit_names():
    assert ch.is_final_submit_label("Post Preprint", ["Post preprint"])


def test_allowed_upload_paths_reads_file_fields(tmp_path):
    ms = tmp_path / "ms.pdf"
    fig = tmp_path / "fig1.png"
    ms.write_bytes(b"x")
    fig.write_bytes(b"x")
    values = {"manuscript_file": str(ms), "figure_files": f"{fig} | Figure 1\n{tmp_path / 'gone.png'} | Figure 2", "title": "A title"}
    assert ch.allowed_upload_paths(values) == {str(ms.resolve()), str(fig.resolve())}


def test_cdp_args_only_when_asked():
    assert ch.browser_launch_args() == []
    with ch.use_cdp_port(9333):
        assert "--remote-debugging-port=9333" in ch.browser_launch_args()
    assert ch.browser_launch_args() == []


# --- the CLI flags -------------------------------------------------------


def _args(**kw):
    base = {"recover": "off", "recover_dir": None, "cdp_port": None, "headless": False}
    return argparse.Namespace(**{**base, **kw})


def test_cli_recover_off_sets_nothing():
    assert cli._submit_recovery_setup(_args(), "biorxiv", "biorxiv", {}) == (None, None)


def test_cli_recover_by_hand(capsys):
    handler, port = cli._submit_recovery_setup(_args(recover="by-hand"), "biorxiv", "biorxiv", {})
    assert isinstance(handler, HumanHandler) and port is None
    # Logs to a fresh run directory, created only once something fails.
    assert handler.log_dir is not None and not handler.log_dir.exists()
    assert ch.recovery_dir_of(handler) == handler.log_dir


def test_cli_recover_by_hand_logs_to_recover_dir(tmp_path):
    handler, _ = cli._submit_recovery_setup(_args(recover="by-hand", recover_dir=str(tmp_path / "run")), "biorxiv", "biorxiv", {})
    assert handler.log_dir == tmp_path / "run"


def test_cli_recover_by_hand_needs_a_window():
    with pytest.raises(ValueError, match="visible browser"):
        cli._submit_recovery_setup(_args(recover="by-hand", headless=True), "biorxiv", "biorxiv", {})


def test_cli_recover_agent_and_cdp(tmp_path, capsys):
    handler, port = cli._submit_recovery_setup(_args(recover="agent", recover_dir=str(tmp_path), cdp_port=0), "biorxiv", "biorxiv", {})
    assert isinstance(handler, ch.RememberingHandler) and isinstance(handler.inner, ch.AgentHandler)
    assert handler.inner.directory == tmp_path and ch.recovery_dir_of(handler) == tmp_path
    assert json.loads((tmp_path / "run.json").read_text())["base"] == "biorxiv"
    assert 0 < port < 65536
    assert f"playwright-cli attach --cdp=http://127.0.0.1:{port}" in capsys.readouterr().out


def test_cli_recover_is_a_no_op_for_unconverted_venues(capsys, monkeypatch):
    from paperpush import venues

    monkeypatch.setattr(type(venues.get_venue_impl("nature")), "supports_recovery", False)
    assert cli._submit_recovery_setup(_args(recover="by-hand"), "nature", "nature", {}) == (None, None)
    assert "does not support recovery" in capsys.readouterr().err


def test_cli_recover_dir_needs_a_recovery_mode():
    with pytest.raises(ValueError, match="--recover agent, by-hand"):
        cli._submit_recovery_setup(_args(recover_dir="/tmp/x", cdp_port=0), "biorxiv", "biorxiv", {})


# ---------------------------------------------------------------------------
# RememberingHandler: reusing a fix within one run
# ---------------------------------------------------------------------------

from paperpush.venues.recovery import FailedAction, FallbackHandler  # noqa: E402


class FakeInner:
    """Stands in for the agent: logs a fix through the tools, like AgentHandler does."""

    def __init__(self, tools, actions, resolution=Resolution.RETRY):
        self.tools, self.actions, self.resolution = tools, actions, resolution
        self.calls: list[tuple[FailedAction, dict | None]] = []

    def recover(self, page, failure):
        self.calls.append((failure, self.tools.hint))
        self.tools.log(failure, self.resolution, list(self.actions), "fixed it", handler="agent")
        return self.resolution


def _failure(target="get_by_role('button', name='Save')", action="click()", step_label="Page 2", attempt=1):
    return FailedAction(action=action, target=target, error=PWTimeout("Timeout 10000ms exceeded."), step=step_label, url="https://portal/x", attempt=attempt)


@pytest.fixture
def remembering(tmp_path, monkeypatch):
    tools = ch.PageTools(tmp_path)
    done: list[tuple] = []

    def act(page, op, target=None, value=None):
        done.append((op, target, value))
        return {"ok": True}

    monkeypatch.setattr(tools, "act", act)
    return tools, done


def test_a_wait_and_click_fix_is_replayed_for_the_same_failure(remembering):
    tools, done = remembering
    fix = [{"op": "wait", "value": 5000}, {"op": "click", "target": {"role": "button", "name": "OK"}}]
    inner = FakeInner(tools, fix)
    handler = ch.RememberingHandler(inner, tools, output=lambda *_: None)
    assert handler.recover(None, _failure()) is Resolution.RETRY
    assert handler.recover(None, _failure()) is Resolution.RETRY
    assert len(inner.calls) == 1  # the second one never paused
    assert done == [("wait", None, 5000), ("click", {"role": "button", "name": "OK"}, None)]
    assert [e["handler"] for e in ch.read_log(tools.directory)] == ["agent", "replay"]


@pytest.mark.parametrize(
    "fix",
    [
        [{"op": "fill", "target": {"label": "Name"}, "value": "Funder One"}],
        [{"op": "click", "target": {"text": "Funder One"}}],
        [{"op": "upload", "target": {"label": "File"}, "value": "/a.pdf"}],
        [],  # fixed by hand: nothing to repeat, and never skip a step silently
    ],
)
def test_fixes_that_carry_data_are_not_replayed(remembering, fix):
    tools, done = remembering
    inner = FakeInner(tools, fix)
    handler = ch.RememberingHandler(inner, tools, output=lambda *_: None)
    handler.recover(None, _failure())
    handler.recover(None, _failure())
    assert len(inner.calls) == 2 and done == []
    # ...but the agent is told how it was fixed last time.
    assert inner.calls[1][1]["actions"] == fix


def test_a_retried_failure_goes_to_the_agent(remembering):
    tools, done = remembering
    inner = FakeInner(tools, [{"op": "wait", "value": 1000}])
    handler = ch.RememberingHandler(inner, tools, output=lambda *_: None)
    handler.recover(None, _failure())
    handler.recover(None, _failure(attempt=2))  # the replayed fix did not hold
    assert len(inner.calls) == 2


def test_a_similar_failure_gets_the_earlier_fix_as_a_hint(remembering):
    tools, _ = remembering
    fix = [{"op": "upload", "target": {"label": "FILE_TO_UPLOAD", "nth": 0}, "value": "/m.pdf"}]
    inner = FakeInner(tools, fix, Resolution.RESOLVED)
    handler = ch.RememberingHandler(inner, tools, output=lambda *_: None)
    handler.recover(None, _failure(target="get_by_role('cell', name='Select File 1')", action="set_input_files('/m.pdf')"))
    handler.recover(None, _failure(target="get_by_role('cell', name='Select File 2')", action="set_input_files('/s.pdf')"))
    first_hint, second_hint = inner.calls[0][1], inner.calls[1][1]
    assert first_hint is None
    assert second_hint["actions"] == fix and second_hint["note"] == "fixed it"
    assert tools.hint is None  # cleared once the agent is done


def test_a_replay_that_no_longer_works_hands_over(remembering, monkeypatch):
    tools, _ = remembering
    inner = FakeInner(tools, [{"op": "click", "target": {"role": "button", "name": "OK"}}])
    handler = ch.RememberingHandler(inner, tools, output=lambda *_: None)
    handler.recover(None, _failure())
    monkeypatch.setattr(tools, "act", lambda *a, **k: {"ok": False, "error": "gone"})
    handler.recover(None, _failure())
    assert len(inner.calls) == 2


def test_aborts_are_not_remembered(remembering):
    tools, done = remembering
    inner = FakeInner(tools, [{"op": "wait", "value": 1}], Resolution.ABORT)
    handler = ch.RememberingHandler(inner, tools, output=lambda *_: None)
    handler.recover(None, _failure())
    handler.recover(None, _failure())
    assert len(inner.calls) == 2 and done == []


def test_earlier_runs_in_a_reused_directory_are_not_remembered(remembering):
    tools, done = remembering
    tools.log(_failure(), Resolution.RETRY, [{"op": "wait", "value": 1}], "old run", handler="agent")
    inner = FakeInner(tools, [{"op": "fill", "target": {"label": "x"}, "value": "y"}])
    handler = ch.RememberingHandler(inner, tools, output=lambda *_: None)
    handler.recover(None, _failure())
    handler.recover(None, _failure())
    assert done == []  # it remembered this run's fill fix, not the old wait


def test_hint_reaches_paused_json_and_snapshots(page, tmp_path):
    handler = ch.AgentHandler(tmp_path, pause_timeout=10)
    handler.tools.hint = {"actions": [{"op": "wait", "value": 1}], "note": "n"}

    def script(send):
        assert ch.read_paused(tmp_path)["earlier_fix"]["note"] == "n"
        assert send("snapshot")["earlier_fix"]["note"] == "n"
        send("resume", resolution="abort")

    thread, _, errors = agent(tmp_path, script)
    assert handler.recover(page, _failure()) is Resolution.ABORT
    finish(thread, errors)


def test_recovery_dir_of_looks_through_wrappers(tmp_path):
    tools = ch.PageTools(tmp_path)
    wrapped = FallbackHandler(ch.RememberingHandler(FakeInner(tools, []), tools), HumanHandler())
    assert ch.recovery_dir_of(wrapped) == tmp_path
    assert ch.recovery_dir_of(HumanHandler()) is None
    assert ch.recovery_dir_of(None) is None
