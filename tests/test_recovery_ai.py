"""Tests for paperpush.venues.recovery_ai: an LLM fixing a failed step from inside the run.

The handler runs against a real headless Chromium. The model is scripted: a
fake session returns a fixed sequence of tool calls, so the tests check what
the handler does with them (and what it feeds back) without an API key. The
provider sessions are tested separately against fake SDK clients, to pin down
the request shapes each API gets.
"""

from __future__ import annotations

import argparse
import json
import sys
import types
from types import SimpleNamespace as NS

import pytest
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from paperpush import cli
from paperpush.venues import recovery_ai as ai
from paperpush.venues.recovery import FallbackHandler, Resolution, guard, step
from paperpush.venues.recovery_ai import ApiAgentHandler, ModelError, ToolCall
from paperpush.venues.recovery_channel import PageTools, read_log

FORM = """
<button onclick="document.getElementById('out').textContent='next'">Next</button>
<button onclick="document.getElementById('out').textContent='SENT'">Submit</button>
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


class ScriptedModel:
    """A fake ModelSession: each turn returns the next list of tool calls; records what it was sent."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.opening = None
        self.image = None
        self.replies: list[list[tuple[ToolCall, str]]] = []

    def start(self, text, image_png):
        self.opening, self.image = text, image_png
        return self._next()

    def reply(self, results):
        self.replies.append(results)
        return self._next()

    def _next(self):
        turn = self.turns.pop(0) if self.turns else []
        if isinstance(turn, Exception):
            raise turn
        return turn


def call(name, **args):
    return ToolCall(id=f"c-{name}-{len(args)}", name=name, args=args)


def handler_for(tmp_path, model, **kw):
    return ApiAgentHandler(PageTools(tmp_path), provider="anthropic", model="test-model", session_factory=lambda: model, output=lambda _m: None, **kw)


def test_model_fixes_a_renamed_button_and_the_run_continues(page, tmp_path):
    model = ScriptedModel(
        [call("look")],
        [call("act", action="click", target={"role": "button", "name": "Next"})],
        [call("finish", resolution="resolved", note="Save / Continue renamed to Next")],
    )
    guarded = guard(page, handler_for(tmp_path, model))
    step("Title page")
    guarded.get_by_role("button", name="Save / Continue").click()

    assert page.locator("#out").text_content() == "next"
    assert "Step: Title page" in model.opening and "get_by_role('button', name='Save / Continue')" in model.opening
    assert 'button "Next"' in model.opening
    assert model.image and model.image.startswith(b"\x89PNG")
    look = json.loads(model.replies[0][0][1])
    assert "aria_snapshot" in look and "screenshot" not in look
    assert json.loads(model.replies[1][0][1])["ok"] is True
    [entry] = read_log(tmp_path)
    assert entry["handler"] == "ai" and entry["model"] == "test-model"
    assert entry["note"] == "Save / Continue renamed to Next"
    assert entry["agent_actions"] == [{"op": "click", "target": {"role": "button", "name": "Next"}}]


def test_final_submit_is_refused_and_the_refusal_is_fed_back(page, tmp_path):
    model = ScriptedModel(
        [call("act", action="click", target={"role": "button", "name": "Submit"})],
        [call("finish", resolution="abort", note="only Submit left")],
    )
    guarded = guard(page, handler_for(tmp_path, model))
    with pytest.raises(PWTimeout):
        guarded.locator("#missing").click()
    assert page.locator("#out").text_content() == ""
    assert "final submit" in json.loads(model.replies[0][0][1])["error"]


def test_bad_resolution_and_unknown_tool_get_an_error_back(page, tmp_path):
    model = ScriptedModel(
        [call("finish", resolution="maybe", note=""), call("teleport")],
        [call("finish", resolution="retry", note="dialog closed")],
    )
    handler = handler_for(tmp_path, model)
    assert handler.recover(page, _failure()) is Resolution.RETRY
    errors = [json.loads(text)["error"] for _, text in model.replies[0]]
    assert "resolution must be" in errors[0] and "unknown tool" in errors[1]


def test_turn_budget(page, tmp_path):
    model = ScriptedModel(*[[call("look")] for _ in range(10)])
    handler = handler_for(tmp_path, model, max_turns=3)
    assert handler.recover(page, _failure()) is Resolution.ABORT
    assert "no resolution after 3" in read_log(tmp_path)[0]["note"]


def test_recovery_budget(page, tmp_path):
    handler = handler_for(tmp_path, None, max_recoveries=0)
    assert handler.recover(page, _failure()) is Resolution.ABORT


def test_a_model_that_stops_talking_is_nudged_once_then_aborted(page, tmp_path):
    model = ScriptedModel([], [])
    assert handler_for(tmp_path, model).recover(page, _failure()) is Resolution.ABORT
    assert model.replies[0][0][1].startswith("Call `finish`")


def test_model_errors_abort(page, tmp_path):
    model = ScriptedModel(ModelError("the API request failed: 529 overloaded"))
    assert handler_for(tmp_path, model).recover(page, _failure()) is Resolution.ABORT
    assert "overloaded" in read_log(tmp_path)[0]["note"]


def test_fallback_hands_an_aborted_step_to_the_next_handler(page, tmp_path):
    model = ScriptedModel([call("finish", resolution="abort", note="captcha")])

    class Person:
        def recover(self, page, failure):
            page.get_by_role("button", name="Next").click()
            return Resolution.RESOLVED

    guarded = guard(page, FallbackHandler(handler_for(tmp_path, model), Person()))
    guarded.locator("#missing").click()
    assert page.locator("#out").text_content() == "next"


def _failure():
    from paperpush.venues.recovery import FailedAction

    return FailedAction(action="click()", target="locator('#missing')", error=PWTimeout("Timeout 300ms exceeded."), step="Files page", url="about:blank")


# --- provider sessions against fake SDKs -----------------------------------


def test_anthropic_session_request_shapes(monkeypatch):
    sent = []

    class Messages:
        def create(self, **kw):
            sent.append(json.loads(json.dumps(kw, default=lambda o: o.__dict__)))
            content = [NS(type="tool_use", id="tu1", name="act", input={"action": "wait", "value": "100"})]
            return NS(stop_reason="tool_use", content=content, usage=NS(input_tokens=1, output_tokens=1))

    fake = types.ModuleType("anthropic")
    fake.APIError = type("APIError", (Exception,), {})
    fake.Anthropic = lambda **kw: NS(messages=Messages())
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    session = ai.make_session("anthropic", "claude-x")
    [first] = session.start("hello", b"\x89PNG")
    assert first == ToolCall(id="tu1", name="act", args={"action": "wait", "value": "100"})
    session.reply([(first, '{"ok": true}')])
    assert sent[0]["system"] == ai.SYSTEM and sent[0]["model"] == "claude-x"
    assert {t["name"] for t in sent[0]["tools"]} == {"look", "act", "finish"}
    assert sent[0]["messages"][0]["content"][1]["type"] == "image"
    last = sent[1]["messages"][-1]
    assert last == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": '{"ok": true}'}]}


def test_anthropic_refusal_raises(monkeypatch):
    fake = types.ModuleType("anthropic")
    fake.APIError = type("APIError", (Exception,), {})
    fake.Anthropic = lambda **kw: NS(messages=NS(create=lambda **k: NS(stop_reason="refusal", content=[], usage=NS(input_tokens=0, output_tokens=0))))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    with pytest.raises(ModelError, match="declined"):
        ai.make_session("anthropic", "m").start("x", None)


def test_openai_session_chains_by_response_id(monkeypatch):
    sent = []

    def create(**kw):
        sent.append(kw)
        output = [NS(type="function_call", call_id=f"call{len(sent)}", name="look", arguments="{}")]
        return NS(id=f"resp{len(sent)}", output=output)

    fake = types.ModuleType("openai")
    fake.APIError = type("APIError", (Exception,), {})
    fake.OpenAI = lambda **kw: NS(responses=NS(create=create))
    monkeypatch.setitem(sys.modules, "openai", fake)

    session = ai.make_session("openai", "gpt-x")
    [first] = session.start("hello", b"png")
    session.reply([(first, "result")])
    assert "previous_response_id" not in sent[0]
    assert sent[0]["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,")
    assert sent[0]["tools"][0]["type"] == "function"
    assert sent[1]["previous_response_id"] == "resp1"
    assert sent[1]["input"] == [{"type": "function_call_output", "call_id": "call1", "output": "result"}]


def test_google_session_chains_by_interaction_id(monkeypatch):
    sent = []

    def create(**kw):
        sent.append(kw)
        steps = [NS(type="user_input"), NS(type="function_call", id=f"fc{len(sent)}", name="finish", arguments={"resolution": "abort", "note": "n"})]
        return NS(id=f"int{len(sent)}", status="requires_action", steps=steps)

    google = types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    errors = types.ModuleType("google.genai.errors")
    errors.APIError = type("APIError", (Exception,), {})
    genai.Client = lambda **kw: NS(interactions=NS(create=create))
    genai.errors = errors
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setitem(sys.modules, "google.genai.errors", errors)

    session = ai.make_session("google", "gemini-x")
    [first] = session.start("hello", b"png")
    assert first.args == {"resolution": "abort", "note": "n"}
    session.reply([(first, "r")])
    assert sent[0]["input"][1]["type"] == "image"
    assert sent[1]["previous_interaction_id"] == "int1"
    assert sent[1]["input"] == [{"type": "function_result", "call_id": "fc1", "name": "finish", "result": "r"}]


# --- the CLI flag ----------------------------------------------------------


def _args(**kw):
    base = {"recover": None, "ai": False, "provider": None, "model": None, "recover_dir": None, "cdp_port": None, "headless": False}
    return argparse.Namespace(**{**base, **kw})


@pytest.fixture
def no_keys(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_cli_ai_needs_an_api_key(no_keys):
    with pytest.raises(ValueError, match="no API key"):
        cli._submit_recovery_setup(_args(ai=True), "biorxiv", "biorxiv", {})


def test_cli_ai_needs_the_sdk(no_keys, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    with pytest.raises(ValueError, match=r"pip install paperpush\[autofill-openai\]"):
        cli._submit_recovery_setup(_args(ai=True), "biorxiv", "biorxiv", {})


def test_cli_ai_builds_the_handler(no_keys, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    handler, port = cli._submit_recovery_setup(_args(ai=True, model="claude-test", recover_dir=str(tmp_path)), "biorxiv", "biorxiv", {})
    # pytest's stdin is not a terminal, so there is no person to fall back to.
    assert isinstance(handler, ApiAgentHandler) and port is None
    assert (handler.provider, handler.model) == ("anthropic", "claude-test")
    assert handler.tools.directory == tmp_path
    assert "screenshot" in capsys.readouterr().out


def test_cli_ai_conflicts(no_keys):
    with pytest.raises(ValueError, match="pick one"):
        cli._submit_recovery_setup(_args(ai=True, recover="by-hand"), "biorxiv", "biorxiv", {})
    with pytest.raises(ValueError, match="only apply with --ai"):
        cli._submit_recovery_setup(_args(model="m"), "biorxiv", "biorxiv", {})
