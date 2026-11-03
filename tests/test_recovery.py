"""Tests for paperpush.venues.recovery: resuming a run after a failed action.

These drive a real headless Chromium on inline HTML, not a mock. The wrapper's
job is to sit between real Playwright errors and the script, so a mock would
only test the mock. Each action's timeout is short so a missing element fails
fast.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from paperpush.venues import recovery
from paperpush.venues.common import _try
from paperpush.venues.openrxiv.main import _attach_file
from paperpush.venues.recovery import FailedAction, GuardedLocator, HumanHandler, RaiseHandler, Resolution, guard, step, use_handler

FORM = """
<button id="go" onclick="document.getElementById('out').textContent='clicked'">Go</button>
<input id="name" aria-label="Name">
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


class Scripted:
    """A handler that records each failure and answers from a list (running an optional fix first)."""

    def __init__(self, *answers, fix=None):
        self.answers = list(answers)
        self.fix = fix
        self.failures: list[FailedAction] = []

    def recover(self, page, failure):
        self.failures.append(failure)
        if self.fix:
            self.fix(page)
        return self.answers.pop(0)


def test_default_handler_raises_the_original_error(page):
    guarded = guard(page)
    assert isinstance(guarded._state.handler, RaiseHandler)
    with pytest.raises(PWTimeout):
        guarded.get_by_role("button", name="Missing").click()
    assert [e.resolution for e in guarded.events] == [Resolution.ABORT]


def test_actions_that_succeed_never_reach_the_handler(page):
    handler = Scripted()
    guarded = guard(page, handler)
    guarded.get_by_role("button", name="Go").click()
    guarded.get_by_role("textbox", name="Name").fill("Jane")
    assert page.locator("#out").text_content() == "clicked"
    assert page.locator("#name").input_value() == "Jane"
    assert handler.failures == []


def test_resolved_skips_the_action_and_the_script_continues(page):
    # The handler does the step itself on the real page, as a person or agent would.
    handler = Scripted(Resolution.RESOLVED, fix=lambda p: p.locator("#go").click())
    guarded = guard(page, handler)
    step("Title page")
    guarded.get_by_role("button", name="Save / Continue").click()  # renamed control
    guarded.get_by_role("textbox", name="Name").fill("after")  # the next line still runs

    assert page.locator("#out").text_content() == "clicked"
    assert page.locator("#name").input_value() == "after"
    [failure] = handler.failures
    assert failure.step == "Title page"
    assert failure.action == "click()"
    assert failure.target == "get_by_role('button', name='Save / Continue')"
    assert "Save / Continue" in failure.describe()


def test_retry_runs_the_action_again(page):
    add_button = "document.body.insertAdjacentHTML('beforeend', '<button>Later</button>')"
    handler = Scripted(Resolution.RETRY, fix=lambda p: p.evaluate(add_button))
    guarded = guard(page, handler)
    guarded.get_by_role("button", name="Later").click()
    [failure] = handler.failures
    assert failure.attempt == 1
    assert [e.resolution for e in guarded.events] == [Resolution.RETRY]


def test_retries_stop_at_max_attempts(page):
    handler = Scripted(Resolution.RETRY, Resolution.RETRY, Resolution.RETRY)
    guarded = guard(page, handler, max_attempts=2)
    with pytest.raises(PWTimeout):
        guarded.locator("#missing").click()
    assert [f.attempt for f in handler.failures] == [1, 2]


def test_queries_pass_through_without_recovery(page):
    handler = Scripted()
    guarded = guard(page, handler)
    assert guarded.locator("#missing").count() == 0
    assert guarded.get_by_role("button", name="Go").first.is_visible()
    assert handler.failures == []


def test_locator_chains_stay_guarded_and_describe_themselves(page):
    handler = Scripted(Resolution.ABORT)
    guarded = guard(page, handler)
    chained = guarded.locator("body").get_by_role("link", name="Select Files").first
    assert isinstance(chained, GuardedLocator)
    with pytest.raises(PWTimeout):
        chained.click()
    assert handler.failures[0].target == "locator('body').get_by_role('link', name='Select Files').first"


def test_attempt_reports_a_unit_as_one_failure(page):
    handler = Scripted(Resolution.ABORT)
    guarded = guard(page, handler)

    def unit(p):
        p.get_by_role("button", name="Go").click()  # succeeds
        p.locator("#missing").click()  # fails inside the unit

    with pytest.raises(PWTimeout):
        guarded.attempt("do two things", unit)
    [failure] = handler.failures
    assert failure.action == "do two things"
    assert failure.target == ""


def test_attach_file_is_one_unit_on_a_guarded_page(page, tmp_path):
    handler = Scripted(Resolution.ABORT)
    guarded = guard(page, handler)
    with pytest.raises(PWTimeout):
        _attach_file(guarded, "#dd-dropzone-manuscript", str(tmp_path / "ms.pdf"))
    [failure] = handler.failures
    assert failure.action.startswith("attach ")
    assert "#dd-dropzone-manuscript" in failure.action


def test_optional_steps_are_not_sent_to_the_handler(page):
    handler = Scripted()
    guarded = guard(page, handler)
    assert _try(lambda: guarded.locator("#missing").click(), "optional thing") is False
    assert handler.failures == []


def test_use_handler_supplies_the_default_and_a_broken_handler_aborts(page):
    class Broken:
        def recover(self, page, failure):
            raise RuntimeError("handler bug")

    with use_handler(Broken()):
        guarded = guard(page)
    with pytest.raises(PWTimeout):  # the real failure, not the handler's bug
        guarded.locator("#missing").click()
    assert recovery.current_handler().__class__ is RaiseHandler


def test_guard_resets_the_step_label(page):
    step("left over from a previous run")
    guard(page)
    assert recovery.current_step() is None


@pytest.mark.parametrize(
    "typed, expected",
    [("", Resolution.RESOLVED), ("c", Resolution.RESOLVED), ("r", Resolution.RETRY), ("A", Resolution.ABORT), ("huh\nr", Resolution.RETRY)],
)
def test_human_handler_answers(typed, expected):
    answers = iter(typed.split("\n"))
    printed: list[str] = []
    handler = HumanHandler(input_fn=lambda _prompt: next(answers), output=printed.append)
    failure = FailedAction(action="click()", target="locator('#x')", error=PWTimeout("Timeout 300ms exceeded."), step="Authors page", url="about:blank")
    assert handler.recover(None, failure) is expected
    assert "Authors page" in printed[0]


def test_human_handler_aborts_without_a_terminal():
    def no_stdin(_prompt):
        raise EOFError

    failure = FailedAction(action="click()", target="", error=PWTimeout("x"), step=None, url="about:blank")
    assert HumanHandler(input_fn=no_stdin, output=lambda _m: None).recover(None, failure) is Resolution.ABORT


def test_frame_locators_and_content_frame_stay_guarded(browser):
    context = browser.new_context()
    context.set_default_timeout(300)
    page = context.new_page()
    page.set_content('<iframe name="content" srcdoc="<button>Inside</button>"></iframe>')
    page.frame_locator('iframe[name="content"]').get_by_role("button", name="Inside").wait_for()
    handler = Scripted(Resolution.ABORT)
    guarded = guard(page, handler)
    guarded.frame_locator('iframe[name="content"]').get_by_role("button", name="Inside").click()
    via_content = guarded.locator('iframe[name="content"]').content_frame.get_by_role("button", name="Missing")
    assert isinstance(via_content, GuardedLocator)
    with pytest.raises(PWTimeout):
        via_content.click()
    assert handler.failures[0].target == "locator('iframe[name=\"content\"]').content_frame.get_by_role('button', name='Missing')"
    context.close()


def test_shared_probes_never_reach_the_handler(page):
    from paperpush.venues.login import first_visible

    handler = Scripted()
    guarded = guard(page, handler)
    assert first_visible(guarded, "link", ("Not here", "Nor here"), 600) is None
    assert first_visible(guarded, "button", ("Go",), 600) is not None
    assert handler.failures == []
