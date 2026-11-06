"""Tests for ``paperpush codegen``: the script model, its rendering, and the recorder in a real page."""

from __future__ import annotations

import ast

import pytest

from paperpush.cli import build_parser
from paperpush.codegen.model import (
    Node,
    Script,
    choose_path,
    render,
    render_locator,
    suggest_branch_name,
    suggest_variable,
    valid_variable_name,
)

ROLE_NEXT = [["get_by_role", ["button"], {"name": "Next"}]]
TITLE_BOX = [["get_by_label", ["Title"], {"exact": True}]]


def body(script: Script, target: str = "paperpush") -> list[str]:
    """The rendered lines after the paperpush header and declarations."""
    lines = render(script, target).lines
    return (
        lines[lines.index("") + 1 :]
        if target == "paperpush" and not script.variables()
        else lines
    )


def compiles(code: str) -> None:
    ast.parse(code)


# --------------------------------------------------------------------- model


def test_typing_into_one_field_is_one_fill_and_one_undo_step():
    s = Script()
    for text in ("D", "De", "Deep"):
        s.add(Node("fill", locator=TITLE_BOX, value=text))
    assert [n.value for n in s.nodes] == ["Deep"]
    assert s.undo() and s.nodes == []


def test_second_click_becomes_dblclick():
    s = Script()
    s.add(Node("click", locator=ROLE_NEXT, value=1))
    s.add(Node("click", locator=ROLE_NEXT, value=2))
    assert [n.kind for n in s.nodes] == ["dblclick"]


def test_chooser_upload_takes_over_the_click_that_opened_it():
    s = Script()
    s.add(Node("click", locator=ROLE_NEXT, value=1, t=10))
    s.add(
        Node(
            "chooser_upload",
            locator=[["locator", ["#f"], {}]],
            value=["/x/ms.pdf"],
            t=20,
        )
    )
    assert len(s.nodes) == 1
    node = s.nodes[0]
    assert (
        node.kind == "chooser_upload"
        and node.locator == ROLE_NEXT
        and node.value == ["/x/ms.pdf"]
    )


def test_chooser_upload_without_a_click_sets_the_input_files():
    s = Script()
    s.add(Node("fill", locator=TITLE_BOX, value="x"))
    s.add(
        Node("chooser_upload", locator=[["locator", ["#f"], {}]], value=["/x/ms.pdf"])
    )
    assert s.nodes[-1].kind == "upload"
    assert 'page.locator("#f").set_input_files("/x/ms.pdf")' in render(s).lines


def test_undo_redo_round_trip():
    s = Script()
    s.add(Node("click", locator=ROLE_NEXT, value=1))
    s.add_step("Authors")
    assert s.undo() and [n.kind for n in s.nodes] == ["click"]
    assert s.redo() and [n.kind for n in s.nodes] == ["click", "step"]
    assert not s.redo()
    s.undo()
    s.add(Node("press", value="Tab"))
    assert not s.can_redo  # a new edit drops the redo history


RADIOS = [("#diagnostic", "Diagnostic tools"), ("#epi", "Epidemiology"), ("#bio", "Biological research")]


def record_branch(s: Script, radios=RADIOS) -> str:
    name = s.enter_branch()
    for n, (sel, label) in enumerate(radios):
        if n:
            s.next_path()
        s.add(Node("check", locator=[["locator", [sel], {}]], label=label))
    s.exit_branch()
    s.add(Node("click", locator=[["get_by_role", ["button"], {"name": "Continue"}]], value=1))
    return name


def test_branch_is_a_match_with_one_case_per_path():
    s = Script()
    assert record_branch(s) == "branch_1"
    code = render(s).code
    compiles(code)
    assert 'branch_1 = values.get("branch_1", "")' in code
    assert (
        "match branch_1:\n"
        '    case "Diagnostic tools":\n'
        '        page.locator("#diagnostic").check()\n'
        '    case "Epidemiology":\n'
        '        page.locator("#epi").check()\n'
        '    case "Biological research":\n'
        '        page.locator("#bio").check()\n'
        'page.get_by_role("button", name="Continue").click()\n'
    ) in code
    # The python target records the path taken last as the branch's value.
    py = render(s, "python").code
    compiles(py)
    assert '"branch_1": "Biological research",' in py


def test_branch_names_count_up_and_cannot_nest():
    s = Script()
    record_branch(s)
    assert s.enter_branch() == "branch_2"
    with pytest.raises(ValueError):
        s.enter_branch()


def test_empty_or_open_branch_renders_valid_code():
    s = Script()
    s.enter_branch()
    compiles(render(s).code)
    assert "# branch_1: no paths recorded yet" in render(s).lines
    s.add(Node("click", locator=ROLE_NEXT, value=1, label="Next"))
    compiles(render(s).code)
    compiles(render(s, "python").code)


def test_duplicate_path_labels_get_numbered_and_keys_fall_back_to_locator():
    s = Script()
    s.enter_branch()
    s.add(Node("click", locator=ROLE_NEXT, value=1, label="Next"))
    s.next_path()
    s.add(Node("click", locator=[["locator", ["#n2"], {}]], value=1, label="Next"))
    s.next_path()
    s.add(Node("check", locator=[["locator", ["#bio"], {}]]))
    assert [s.nodes[i].option for i in s.branch_paths(0)] == ["Next", "Next 2", "bio"]


def test_a_path_holds_every_action_until_next():
    s = Script()
    s.add(Node("goto", value="https://example.org"))
    s.enter_branch()
    assert not s.next_path()  # nothing recorded for the path yet
    s.add(Node("check", locator=[["locator", ["#a"], {}]], label="A"))
    s.add(Node("fill", locator=[["locator", ["#a-detail"], {}]], value="details"))
    assert s.next_path() and not s.path_open()
    assert not s.next_path()  # no empty paths
    s.add(Node("check", locator=[["locator", ["#b"], {}]], label="B"))
    s.exit_branch()
    s.add(Node("click", locator=ROLE_NEXT, value=1))
    assert [s.nodes[i].option for i in s.branch_paths(1)] == ["A", "B"]
    code = render(s).code
    compiles(code)
    assert (
        'page.goto("https://example.org")\n'
        "match branch_1:\n"
        '    case "A":\n'
        '        page.locator("#a").check()\n'
        '        page.locator("#a-detail").fill("details")\n'
        '    case "B":\n'
        '        page.locator("#b").check()\n'
        'page.get_by_role("button", name="Next").click()\n'
    ) in code
    compiles(render(s, "python").code)
    assert s.undo() and s.undo() and s.undo() and s.path_open() is False  # back past End, B and Next


def test_replay_runs_the_whole_chosen_path():
    from paperpush.codegen.session import CodegenSession

    session = CodegenSession(sub_values={"branch_1": "A"})
    s = session.script
    s.enter_branch()
    s.add(Node("check", locator=[["locator", ["#a"], {}]], label="A"))
    s.add(Node("fill", locator=[["locator", ["#a-detail"], {}]], value="details"))
    s.next_path()
    s.add(Node("check", locator=[["locator", ["#b"], {}]], label="B"))
    s.add(Node("press", value="Tab"))
    s.exit_branch()
    s.add(Node("click", locator=ROLE_NEXT, value=1))
    assert session.replay_plan() == [1, 2, 7]
    session.sub_values = {"branch_1": "B"}
    assert session.replay_plan() == [4, 5, 7]


def test_editing_branch_lines_renames_branch_and_paths():
    s = Script()
    record_branch(s)
    rendered = render(s)
    header = rendered.lines.index("match branch_1:")
    assert rendered.edits[header] == "branch_name"
    s.edit_line("branch_name", rendered.owners[header], "match manuscript_type:")
    case = rendered.lines.index('    case "Epidemiology":')
    assert rendered.edits[case] == "option"
    s.edit_line("option", rendered.owners[case], 'case "Epidemiological study":')
    code = render(s).code
    assert "match manuscript_type:" in code and 'case "Epidemiological study":' in code
    assert s.undo() and 'case "Epidemiology":' in render(s).code
    with pytest.raises(ValueError):
        s.edit_line("branch_name", 0, "match 1abc:")
    with pytest.raises(ValueError):
        s.edit_line("option", 3, 'case "Diagnostic tools":')  # already a path


def test_variable_on_a_path_renames_its_branch():
    s = Script()
    record_branch(s)
    assert s.variable_target(3) == ("branch", 0)
    s.set_variable(3, "server_suitability")
    assert s.nodes[0].value == "server_suitability" and s.nodes[0].var_locked


def test_suggest_branch_name_from_field_options():
    options = {"server_suitability": ["Diagnostic tools", "Epidemiology", "Biological research", "Clinical"], "license": ["CC-BY"]}
    assert suggest_branch_name(["Diagnostic tools", "biological  research"], options, {}) == "server_suitability"
    assert suggest_branch_name(["Yes", "No"], options, {}) is None
    assert suggest_branch_name(["Yes", "No"], {}, {"coi": "No"}) == "coi"


def test_choose_path_follows_the_value_else_the_last_path():
    s = Script()
    record_branch(s)
    assert choose_path(s, 0, "epidemiology") == 3
    assert choose_path(s, 0, None) == 5
    assert choose_path(s, 0, "unknown") == 5


def test_variable_replaces_value_and_matching_literals():
    s = Script()
    s.add(Node("fill", locator=TITLE_BOX, value="Deep learning"))
    s.add(Node("click", locator=ROLE_NEXT, value=1))
    s.add(Node("fill", locator=[["locator", ["#again"], {}]], value="Deep learning"))
    changed = s.set_variable(0, "title")
    assert changed == [0, 2]
    lines = render(s).lines
    assert 'title = values.get("title", "")' in lines
    assert 'page.get_by_label("Title", exact=True).fill(title)' in lines
    s.set_variable(0, "")
    assert s.nodes[0].var is None and s.nodes[2].var == "title"


@pytest.mark.parametrize("name", ["class", "page", "page1", "values", "1abc", "a-b"])
def test_unusable_variable_names(name):
    assert not valid_variable_name(name)
    s = Script()
    s.add(Node("fill", locator=TITLE_BOX, value="x"))
    with pytest.raises(ValueError):
        s.set_variable(0, name)


def test_navigation_to_the_url_just_opened_is_not_recorded_twice():
    s = Script()
    s.add(Node("goto", value="https://x.org/q", t=0))
    s.add(Node("goto", value="https://x.org/q", t=60))
    assert len(s.nodes) == 1


def test_variable_on_a_line_without_value_is_refused():
    s = Script()
    s.add(Node("click", locator=ROLE_NEXT, value=1))
    with pytest.raises(ValueError):
        s.set_variable(0, "title")


def test_paperpush_target_wraps_chooser_in_attempt_and_uses_step():
    s = Script()
    s.add_step("Files")
    s.add(Node("click", locator=ROLE_NEXT, value=1, t=1))
    s.add(
        Node(
            "chooser_upload",
            locator=[],
            value=["/x/ms.pdf"],
            var="manuscript_file",
            t=2,
        )
    )
    code = render(s, file_fields=["manuscript_file"]).code
    compiles(code)
    assert 'manuscript_file = values.get("manuscript_file", "").strip()' in code
    assert 'step("Files")' in code
    assert (
        "def _attach_1(page):\n    with page.expect_file_chooser() as fc_info:" in code
    )
    assert 'page.attempt("attach manuscript_file", _attach_1)' in code


def test_python_target_is_a_runnable_script_without_secrets():
    s = Script()
    s.add(Node("goto", value="https://example.org"))
    s.add(
        Node("fill", locator=TITLE_BOX, value="hunter2", sensitive=True, var="password")
    )
    s.add(Node("click", locator=ROLE_NEXT, value=1, popup="page1"))
    s.add(Node("click", page="page1", locator=ROLE_NEXT, value=1, dialog="accept"))
    code = render(s, "python").code
    compiles(code)
    assert "hunter2" not in code
    assert '"password": "",  # not recorded: fill in' in code
    assert "with page.expect_popup() as page1_info:" in code
    assert 'page1.once("dialog", lambda dialog: dialog.accept())' in code
    assert "with page.expect_file_chooser" not in code


def test_render_locator_chain():
    chain = [
        ["frame_locator", ["iframe[name='x']"], {}],
        ["get_by_role", ["button"], {"name": "Go", "exact": True}],
        ["nth", [0], {}],
    ]
    assert (
        render_locator(chain)
        == 'page.frame_locator("iframe[name=\'x\']").get_by_role("button", name="Go", exact=True).first'
    )


def test_suggest_variable_matches_sub_values():
    sub = {
        "title": "Deep  learning",
        "manuscript_file": "/a/ms.pdf",
        "figures": "/a/f1.png | Figure\n/a/f2.png | Figure",
    }
    files = ["manuscript_file", "figures"]
    assert suggest_variable("deep learning", "fill", sub, files) == "title"
    assert suggest_variable("other", "fill", sub, files) is None
    assert (
        suggest_variable(["/elsewhere/ms.pdf"], "upload", sub, files)
        == "manuscript_file"
    )
    assert suggest_variable(["f2.png"], "chooser_upload", sub, files) == "figures"


def test_cli_registers_codegen():
    args = build_parser().parse_args(
        ["codegen", "https://example.org", "--target", "python", "-o", "x.py"]
    )
    assert (
        args.url == "https://example.org"
        and args.target == "python"
        and args.output == "x.py"
    )


# ------------------------------------------------------------------ recorder

# The spacer keeps the controls clear of the toolbar (top right), which would intercept clicks.
FORM = """<!doctype html><title>Wizard</title><body style="max-width: 420px">
<label for="t">Manuscript title</label><input id="t">
<input type="password" placeholder="Password">
<select aria-label="Article type"><option>Research</option><option>Review</option></select>
<label><input type="checkbox" id="agree"> I agree</label>
<input type="file" id="f" style="display:none">
<button id="up" onclick="document.getElementById('f').click()"><span>Upload file</span></button>
<button onclick="if (confirm('Sure?')) document.body.dataset.ok = '1'">Next</button>
<iframe name="inner" srcdoc="<button>Inside</button>"></iframe>
<fieldset><legend>Is your manuscript better suited for medRxiv?</legend>
<label><input type="radio" name="s" id="diagnostic"> Diagnostic tools or medical equipment tested in humans</label>
<label><input type="radio" name="s" id="epi"> Epidemiological and/or public health study in humans</label>
<label><input type="radio" name="s" id="bio"> Biological research</label></fieldset>
<button onclick="document.body.dataset.continued = '1'">Continue</button>
<div role="listbox" aria-label="Subject"><div role="option">Biochemistry</div><div role="option">Genomics</div></div>
"""


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # browsers not installed
            pytest.skip(f"chromium not available: {exc}")
        yield browser
        browser.close()


@pytest.fixture
def recorder(browser, tmp_path):
    from paperpush.codegen.session import CodegenSession

    manuscript = tmp_path / "ms.pdf"
    manuscript.write_text("pdf")
    copied: list[str] = []
    session = CodegenSession(
        sub_values={"title": "Deep learning", "manuscript_file": str(manuscript)},
        file_fields=["manuscript_file"],
        field_options={"server_suitability": ["Diagnostic tools or medical equipment tested in humans", "Epidemiological and/or public health study in humans", "Biological research"]},
        output=tmp_path / "out.py",
        clipboard=lambda text: copied.append(text) or True,
    )
    # Wide enough that the toolbar (top right) never covers the 420px-wide form.
    context = browser.new_context(viewport={"width": 1700, "height": 900})
    session.context = context
    session.attach(context)
    page = context.new_page()
    session._register_page(page, "page")
    (tmp_path / "form.html").write_text(FORM)
    page.goto((tmp_path / "form.html").as_uri())

    def settle(ms: int = 150) -> None:
        page.wait_for_timeout(ms)
        session.drain()

    settle(300)
    session.script = Script()  # drop the goto the fixture's own navigation recorded
    session._dirty = True
    session.drain()
    yield session, page, settle, manuscript, copied
    context.close()


def test_recorder_records_actions_with_good_locators(recorder):
    session, page, settle, manuscript, _ = recorder
    page.get_by_label("Manuscript title").press_sequentially("Deep learning")
    settle()
    page.get_by_placeholder("Password").fill("hunter2")
    settle()
    page.get_by_label("Article type").select_option("Review")
    page.get_by_text("I agree").click()
    settle()
    with page.expect_file_chooser() as fc:
        page.get_by_role("button", name="Upload file").click()
    fc.value.set_files(str(manuscript))
    settle()
    page.get_by_role("button", name="Next").click()
    settle(300)
    page.frame_locator("iframe").get_by_role("button", name="Inside").click()
    settle()

    lines = session.rendered().lines
    code = "\n".join(lines)
    compiles(code)
    assert 'page.get_by_role("textbox", name="Manuscript title").fill(title)' in lines
    assert 'page.get_by_placeholder("Password", exact=True).fill(password)' in lines
    assert (
        'page.get_by_role("combobox", name="Article type").select_option(label="Review")'
        in lines
    )
    assert 'page.get_by_role("checkbox", name="I agree").check()' in lines
    assert '        page.get_by_role("button", name="Upload file").click()' in lines
    assert "    fc_info.value.set_files(manuscript_file)" in lines
    assert 'page.once("dialog", lambda dialog: dialog.accept())' in lines
    assert page.evaluate("document.body.dataset.ok") == "1"
    assert (
        'page.frame_locator("iframe[name=\'inner\']").get_by_role("button", name="Inside").click()'
        in lines
    )
    assert "hunter2" not in code
    assert session.output.read_text() == session.rendered().code


def test_recorder_upload_path_comes_from_devtools(recorder):
    session, page, settle, manuscript, _ = recorder
    page.locator("#f").set_input_files(str(manuscript))
    settle()
    node = session.script.nodes[-1]
    assert node.kind in ("upload", "chooser_upload") and node.value == [str(manuscript)]


def test_toolbar_commands_and_paused_recording(recorder):
    session, page, settle, _, copied = recorder

    def toolbar(cmd: str) -> None:
        point = page.evaluate("c => window.__ppToolbarRect(c)", cmd)
        assert point, cmd
        page.mouse.click(point["x"], point["y"])
        settle()

    toolbar("record")
    assert not session.recording
    page.get_by_role("button", name="Upload file").evaluate("b => b.onclick = null")
    page.get_by_role("button", name="Upload file").click()
    settle()
    assert not any(n.kind == "click" for n in session.script.nodes)
    toolbar("record")
    page.get_by_role("button", name="Upload file").click()
    settle()
    assert session.script.nodes[-1].kind == "click"
    toolbar("undo")
    assert not session.script.nodes
    toolbar("redo")
    assert session.script.nodes[-1].kind == "click"
    toolbar("copy")
    assert "Upload file" in copied[-1]
    toolbar("copyhtml")
    assert 'id="agree"' in copied[-1] and "paperpush-codegen" not in copied[-1]
    # Picking for a branch does not reach the page.
    toolbar("pickhtml")
    page.locator("#agree").click()
    settle()
    assert copied[-1].startswith("<input") and not page.locator("#agree").is_checked()


def test_replay_runs_the_script(recorder):
    session, page, settle, _, _ = recorder
    page.get_by_label("Manuscript title").fill("Deep learning")
    page.get_by_text("I agree").click()
    settle()
    session.script.nodes.insert(0, Node("goto", value=page.url))
    session.replay()
    assert session.message == "Replay finished."
    replayed = [p for p in session.context.pages if p in session.replay_pages][0]
    assert replayed.locator("#t").input_value() == "Deep learning"
    assert replayed.locator("#agree").is_checked()


def toolbar_click(page, settle, cmd: str) -> None:
    point = page.evaluate("c => window.__ppToolbarRect(c)", cmd)
    assert point, cmd
    page.mouse.click(point["x"], point["y"])
    settle()


def test_toolbar_is_one_row_with_code_shown(recorder):
    _, page, settle, _, _ = recorder
    first = page.evaluate("() => window.__ppToolbarRect('record')")
    last = page.evaluate("() => window.__ppToolbarRect('copy')")
    assert abs(first["y"] - last["y"]) < 2
    assert page.evaluate("() => window.__ppToolbarRect('.code')") is not None
    order = page.evaluate("() => ['endbranch', 'optional', 'pickoptions', 'variable', 'pickhtml', 'copyhtml', 'panel', 'copy', 'save'].map(c => window.__ppToolbarRect(c).x)")
    assert order == sorted(order)
    assert page.evaluate("() => window.__ppToolbarRect('step')") is None


def test_branch_records_each_option_as_a_path(recorder):
    session, page, settle, _, _ = recorder
    toolbar_click(page, settle, "branch")
    for n, sel in enumerate(("#diagnostic", "#epi", "#bio")):
        if n:
            toolbar_click(page, settle, "nextpath")
        page.locator(sel).check()
        settle()
    toolbar_click(page, settle, "endbranch")
    page.get_by_role("button", name="Continue").click()
    settle()
    lines = session.rendered().lines
    compiles("\n".join(lines))
    # Named after the venue field whose options the paths are.
    assert 'server_suitability = values.get("server_suitability", "")' in lines
    assert "match server_suitability:" in lines
    assert '    case "Epidemiological and/or public health study in humans":' in lines
    assert lines[-1] == 'page.get_by_role("button", name="Continue").click()'

    # Retype the branch's name in the code panel.
    point = page.evaluate("() => window.__ppToolbarRect('.ln.edit[data-owner=\"0\"]')")
    page.mouse.click(point["x"], point["y"])
    page.keyboard.press("Meta+a" if __import__("sys").platform == "darwin" else "Control+a")
    page.keyboard.type("match manuscript_type:")
    page.keyboard.press("Enter")
    settle()
    assert "match manuscript_type:" in session.rendered().lines
    assert session.script.nodes[0].var_locked

    # Variable on a path line renames the branch too.
    path_line = page.evaluate("() => window.__ppToolbarRect('.ln.own[data-owner=\"3\"]:not(.edit)')")
    page.mouse.click(path_line["x"], path_line["y"])
    settle()
    toolbar_click(page, settle, "variable")
    page.keyboard.press("Meta+a" if __import__("sys").platform == "darwin" else "Control+a")
    page.keyboard.type("scope")
    page.keyboard.press("Enter")
    settle()
    assert "match scope:" in session.rendered().lines and "Branch renamed to scope." == session.message


def test_dragging_over_code_lines_keeps_the_selection(recorder):
    session, page, settle, _, _ = recorder
    for _ in range(2):
        page.get_by_role("button", name="Continue").click()
        settle()
    lines = page.evaluate("() => [0, 1].map(i => window.__ppToolbarRect(`.ln.own[data-owner=\"${i}\"]`))")
    first, last = lines
    page.mouse.move(first["x"] - 40, first["y"])
    page.mouse.down()
    page.mouse.move(last["x"] + 40, last["y"], steps=5)
    page.mouse.up()
    settle()
    session._dirty = True  # another state push, as recording would cause
    settle()
    assert session.selected is None  # the drag did not select a line
    assert "click()" in page.evaluate("() => String(window.getSelection())")


def test_variable_explains_a_line_without_value(recorder):
    session, page, settle, _, _ = recorder
    toolbar_click(page, settle, "variable")
    assert session.message.startswith("Nothing to name yet")
    page.get_by_role("button", name="Continue").click()
    settle()
    toolbar_click(page, settle, '.ln.own[data-owner="0"]')  # select the click line
    toolbar_click(page, settle, "variable")
    assert "has no value to replace" in session.message


def test_replay_takes_the_path_the_sub_names(tmp_path):
    from paperpush.codegen.session import CodegenSession

    session = CodegenSession(sub_values={"branch_1": "epidemiology"})
    record_branch(session.script)
    assert session.replay_plan() == [3, 7]  # the Epidemiology path, then Continue
    session.sub_values = {}
    assert session.replay_plan() == [5, 7]  # no value: the path recorded last


def test_options_comment_lists_a_dropdowns_choices(recorder):
    session, page, settle, _, _ = recorder
    cases = (
        (page.get_by_label("Article type"), ['# Options of "Article type" (2):', "#   - Research", "#   - Review"]),
        (page.locator("#epi"), ['# Options of "Is your manuscript better suited for medRxiv?" (3):', "#   - Biological research"]),
        (page.get_by_role("option", name="Genomics"), ['# Options of "Subject" (2):', "#   - Genomics"]),
    )
    for target, expected in cases:
        toolbar_click(page, settle, "pickoptions")
        target.click()
        settle()
        lines = session.rendered().lines
        for line in expected:
            assert line in lines
    # Picking does not act on the page.
    assert not page.locator("#epi").is_checked()
    assert not any(n.kind == "check" for n in session.script.nodes)
    toolbar_click(page, settle, "pickoptions")
    page.get_by_role("button", name="Continue").click()
    settle()
    assert "no options" in session.message
    assert page.evaluate("document.body.dataset.continued") is None


def test_options_comment_inside_a_branch_compiles():
    s = Script()
    s.enter_branch()
    s.add_comment("Kind", ["A", "B"])
    s.add(Node("check", locator=[["locator", ["#a"], {}]], label="A"))
    s.next_path()
    s.add(Node("check", locator=[["locator", ["#b"], {}]], label="B"))
    s.add_comment("Detail", ["x"])
    s.exit_branch()
    code = render(s).code
    compiles(code)
    assert '    # Options of "Kind" (2):\n    #   - A\n' in code
    assert '        # Options of "Detail" (1):\n' in code
    assert [s.nodes[i].option for i in s.branch_paths(0)] == ["A", "B"]


def test_save_code_writes_the_file(tmp_path):
    from paperpush.codegen.session import CodegenSession

    session = CodegenSession()
    session.script.add(Node("goto", value="https://example.org"))
    session.save(str(tmp_path / "out" / "steps.py"))
    assert (tmp_path / "out" / "steps.py").read_text() == session.rendered().code
    assert session.save_path.endswith("steps.py") and session.message.startswith("Saved the code to")
    session.save("")
    assert session.message.startswith("Give a file name")


def test_optional_toggle_wraps_section_in_comments():
    s = Script()
    s.add(Node("click", locator=[["get_by_role", ["button"], {"name": "A"}]], label="A"))
    assert s.toggle_optional() is True and s.optional_open()
    s.add(Node("click", locator=[["get_by_role", ["button"], {"name": "B"}]], label="B"))
    assert s.toggle_optional() is False and not s.optional_open()
    lines = [ln.strip() for ln in render(s).code.splitlines()]
    start = lines.index("# ---OPTIONAL---")
    assert "name=\"B\"" in lines[start + 1] and lines[start + 2] == "# ---END OPTIONAL---"
    assert s.undo() and s.optional_open()


def test_codegen_sub_accepts_the_other_suffix(tmp_path, monkeypatch):
    """`codegen --sub biorxiv.sub` reads biorxiv.sub.txt when only that exists."""
    from types import SimpleNamespace

    from paperpush.cli import main
    from paperpush.codegen import session as session_mod

    monkeypatch.chdir(tmp_path)
    assert main(["subfile", "biorxiv"]) == 0
    seen = {}

    class FakeSession:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def run(self):
            return SimpleNamespace(code="")

    monkeypatch.setattr(session_mod, "CodegenSession", FakeSession)
    assert main(["codegen", "--sub", "biorxiv.sub", "--new-session"]) == 0
    assert "biorxiv" in seen["header"]
    assert seen["fields"]
