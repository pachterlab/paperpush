"""Zenodo authentication tested in a browser against an offline login form."""

import json
from urllib.parse import parse_qs

import pytest
from playwright.sync_api import sync_playwright

from paperpush.venues import get_runner, get_venue_impl
from paperpush.venues.invenio import zenodo
from paperpush.venues.login import VenueLoginError, _login_supported

# Form attributes checked against https://zenodo.org/login/ (2026-11-15).
LOGIN_FORM = """
<form name="login_user_form" action="/login/" method="POST">
  <input type="email" name="email" placeholder="Email Address" required>
  <input type="password" name="password" placeholder="Password" required>
  <button type="submit">Log in</button>
</form>
"""
GUEST_MENU = '<nav id="invenio-menu"><a href="/me/uploads">My dashboard</a></nav>'
SIGNED_IN_MENU = '<nav id="invenio-menu"><div hidden><a href="/logout/">Log out</a></div></nav>'


@pytest.fixture
def page():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(1000)
        page.route("https://zenodo.org/**", lambda route: route.fulfill(content_type="text/html", body=GUEST_MENU))
        page.goto("https://zenodo.org/")
        yield page
        context.close()
        browser.close()


def test_login_and_submit_dispatch():
    assert get_venue_impl("zenodo") is zenodo.VENUE
    assert _login_supported("zenodo") is zenodo.VENUE
    assert zenodo.VENUE.login_url == zenodo.LOGIN_URL
    assert zenodo.VENUE.requires_login
    assert "Publish" in zenodo.VENUE.final_submit_names
    assert get_runner("zenodo") == zenodo.VENUE.submit
    assert zenodo.VENUE.supports_recovery


def test_guest_dashboard_is_not_a_signed_in_session(page):
    page.set_content(GUEST_MENU)
    assert not zenodo.VENUE.is_logged_in(page, timeout_ms=50)


def test_hidden_logout_control_is_a_signed_in_session(page):
    page.set_content(SIGNED_IN_MENU)
    assert zenodo.VENUE.is_logged_in(page, timeout_ms=500)


@pytest.mark.parametrize("accepted", [True, False], ids=["success", "rejected"])
def test_login_checks_authentication_after_post(page, accepted):
    submitted = []

    def respond(route):
        if route.request.method == "POST":
            submitted.append(parse_qs(route.request.post_data))
            body = SIGNED_IN_MENU if accepted else GUEST_MENU + LOGIN_FORM
        else:
            body = GUEST_MENU + LOGIN_FORM
        route.fulfill(status=200, content_type="text/html", body=body)

    page.route("https://zenodo.org/**", respond)
    if accepted:
        zenodo.VENUE.login(page, "author@example.org", "test-password", timeout_ms=200)
    else:
        with pytest.raises(VenueLoginError, match="Check your email and password"):
            zenodo.VENUE.login(page, "author@example.org", "test-password", timeout_ms=200)
    assert submitted == [{"email": ["author@example.org"], "password": ["test-password"]}]


def test_changed_login_form_reports_a_login_error(page):
    page.route("https://zenodo.org/**", lambda route: route.fulfill(status=200, content_type="text/html", body="<h1>Additional sign-in step</h1>"))
    with pytest.raises(VenueLoginError, match="could not find the username/password fields"):
        zenodo.VENUE.login(page, "author@example.org", "test-password", timeout_ms=50)


@pytest.mark.parametrize("provider", ["orcid", "github"])
@pytest.mark.parametrize("result", ["success", "mfa", "rejected", "session"])
def test_provider_callback_and_interactive_steps(page, provider, result):
    cfg = zenodo.OAUTH_PROVIDERS[provider]
    submitted = []
    visited = []
    callback = f"https://zenodo.org/oauth/authorized/{provider}/?state=test-state"
    form = f"""<form action="/session" method="POST">
      <input id="{cfg['identity'][1:]}" name="identity">
      <input id="password" name="password" type="password">
      <button type="submit">{cfg['submit']}</button>
    </form>"""

    def respond(route):
        url = route.request.url
        visited.append(url)
        if url == zenodo.LOGIN_URL:
            body = f'<a href="/oauth/login/{provider}/">Sign in with {cfg["name"]}</a>'
        elif url == f"https://zenodo.org/oauth/login/{provider}/":
            target = callback if result == "session" else f"https://{cfg['host']}/signin"
            # Use a fresh navigation so Playwright routes every hop offline;
            # HTTP redirects inside route.fulfill bypass subsequent routing.
            body = f"<script>location.replace({json.dumps(target)})</script>"
        elif url == callback:
            body = SIGNED_IN_MENU
        elif route.request.method == "POST":
            submitted.append(parse_qs(route.request.post_data))
            if result == "success":
                body = f"<script>location.replace({json.dumps(callback)})</script>"
            else:
                body = '<button>Authorize Zenodo</button><input name="otp" aria-label="Two-factor code">' if result == "mfa" else form
        else:
            body = form
        route.fulfill(content_type="text/html", body=body)

    page.route("**/*", respond)
    kwargs = {provider: True, "timeout_ms": 1000}
    if result in {"success", "session"}:
        zenodo.VENUE.login(page, "provider-user", "test-password", **kwargs)
        assert page.url == callback
    else:
        with pytest.raises(VenueLoginError, match="provider has not returned"):
            zenodo.VENUE.login(page, "provider-user", "test-password", **kwargs)
        assert page.url == f"https://{cfg['host']}/session"
        assert callback not in visited  # Never bypass consent/MFA with a reload.
    assert submitted == ([] if result == "session" else [{"identity": ["provider-user"], "password": ["test-password"]}])
    assert visited.count(zenodo.LOGIN_URL) == 1


@pytest.mark.parametrize("provider", ["orcid", "github"])
def test_provider_credentials_are_not_entered_on_an_unexpected_host(page, provider):
    cfg = zenodo.OAUTH_PROVIDERS[provider]

    def respond(route):
        if route.request.url == zenodo.LOGIN_URL:
            body = f'<a href="https://unexpected.example/signin">Sign in with {cfg["name"]}</a>'
        else:
            body = f'<input id="{cfg["identity"][1:]}"><input id="password" type="password">'
        route.fulfill(content_type="text/html", body=body)

    page.route("**/*", respond)
    with pytest.raises(VenueLoginError, match="unexpected host"):
        zenodo.VENUE.login(page, "provider-user", "test-password", **{provider: True, "timeout_ms": 200})
    assert page.locator(cfg["identity"]).input_value() == ""
    assert page.locator("#password").input_value() == ""


def test_foreign_logout_control_is_not_a_zenodo_session(page):
    page.route("https://github.com/**", lambda route: route.fulfill(content_type="text/html", body=SIGNED_IN_MENU))
    page.goto("https://github.com/")
    assert not zenodo.VENUE.is_logged_in(page, timeout_ms=50)


@pytest.mark.parametrize("method,flags", [("password", []), ("orcid", ["--orcid"]), ("github", ["--github"])])
def test_cli_stores_checks_and_reuses_the_selected_provider(monkeypatch, capsys, method, flags):
    from unittest.mock import Mock

    from paperpush import credentials
    from paperpush.cli import main
    from paperpush.venues import login as controls

    identity = "0000-0002-1825-0097" if method == "orcid" else "author@example.org"
    monkeypatch.setenv("PAPERPUSH_USERNAME", identity)
    monkeypatch.setenv("PAPERPUSH_ORCID_ID", identity)
    monkeypatch.setenv("PAPERPUSH_PASSWORD", "test-password")
    verify = Mock()
    monkeypatch.setattr(controls, "verify_login", verify)
    assert main(["login", "zenodo", *flags]) == 0
    assert verify.call_args.kwargs.get("method", "password") == method
    cred = credentials.get_credential("zenodo")
    assert cred.method == method
    assert main(["login", "zenodo", "--check"]) == 0
    assert verify.call_args.kwargs["method"] == method
    assert verify.call_args.kwargs["headless"] is True
    login = Mock()
    monkeypatch.setattr(zenodo.VENUE, "login", login)
    assert zenodo.VENUE._sign_in_with_credential(object(), cred)
    assert login.call_args.args[1:] == (identity, "test-password")
    assert login.call_args.kwargs == ({} if method == "password" else {method: True})
    assert main(["login", "zenodo", "--status"]) == 0
    if method != "password":
        assert ("GitHub" if method == "github" else "ORCID") in capsys.readouterr().out


@pytest.mark.parametrize("flags", [["--github", "--orcid"], ["--github", "--orcid-id", "0000-0002-1825-0097"]])
def test_cli_rejects_conflicting_provider_flags_before_prompting(monkeypatch, flags):
    from paperpush.cli import main

    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("must reject before requesting credentials"))
    assert main(["login", "zenodo", *flags]) == 2


def test_cli_refuses_github_on_a_venue_without_that_provider():
    from paperpush.cli import main

    assert main(["login", "biorxiv", "--github", "--no-verify"]) == 2


def test_cli_does_not_store_a_rejected_github_login(monkeypatch):
    from paperpush import credentials
    from paperpush.cli import main
    from paperpush.venues import login as controls

    monkeypatch.setenv("PAPERPUSH_USERNAME", "test-user")
    monkeypatch.setenv("PAPERPUSH_PASSWORD", "test-password")

    def reject(*args, **kwargs):
        raise controls.LoginVerificationError("two-factor authentication required")

    monkeypatch.setattr(controls, "verify_login", reject)
    assert main(["login", "zenodo", "--github"]) == 1
    assert credentials.get_credential("zenodo") is None


@pytest.mark.parametrize("written,expected", [("Figure", "Image / Figure"), ("Journal article", "Publication / Journal article"), ("Computational notebook", "Software / Computational notebook"), ("Other", "Other"), ("Image / Other", "Image / Other"), ("Publication / Other", "Publication / Other"), ("Dataset", "Dataset")])
def test_resource_type_preserves_groups_and_older_subfiles(written, expected):
    assert zenodo._resource_label(written) == expected


def test_resource_dropdown_selects_the_exact_group(page):
    page.set_content("""<div role="combobox" name="metadata.resource_type"><input>
      <div role="option" onclick="window.picked='generic'">Other</div>
      <div role="option" onclick="window.picked='image'">Image / Other</div>
      <div role="option" onclick="window.picked='publication'">Publication / Other</div>
    </div>""")
    zenodo._choose(zenodo._combo(page, "metadata.resource_type"), "Publication / Other")
    assert page.evaluate("window.picked") == "publication"


@pytest.fixture
def file_values(tmp_path):
    path = tmp_path / "synthetic.txt"
    path.write_text("Synthetic test file; do not publish.")
    return {"data_files": str(path), "resource_type": "Dataset", "title": "PAPERPUSH TEST - DO NOT PUBLISH", "publication_date": "2026-11-15", "authors": "Test, Paperpush", "license": zenodo.DEFAULT_LICENSE}


@pytest.mark.parametrize("changes,reason", [({"data_files": ""}, "data_files"), ({"data_files": "/missing/synthetic.png"}, "does not exist"), ({"embargo_date": "2035-01-01", "access_right": "Public"}, "Restricted"), ({"embargo_reason": "Test"}, "embargo_date"), ({"communities": "one\ntwo"}, "one community"), ({"communities": 'bad"]slug'}, "identifier"), ({"related_identifiers": "10.1234/test"}, "relation"), ({"related_identifiers": "123 | Cites"}, "scheme")])
def test_invalid_values_fail_before_browser_launch(file_values, changes, reason, monkeypatch):
    from unittest.mock import Mock

    launch = Mock()
    monkeypatch.setattr(zenodo, "sync_playwright", launch)
    with pytest.raises(ValueError, match=reason):
        zenodo.VENUE.submit({**file_values, **changes})
    launch.assert_not_called()


def test_files_keep_order_strip_labels_and_reject_duplicate_names(file_values, tmp_path):
    second = tmp_path / "second.txt"
    second.touch()
    assert zenodo._check_values({**file_values, "data_files": f'{second} | Second\n{file_values["data_files"]} | First'}) == [str(second), file_values["data_files"]]
    with pytest.raises(ValueError, match="unique filenames"):
        zenodo._check_values({**file_values, "data_files": f"{second}\n{tmp_path}/other/second.txt"})
    with pytest.raises(ValueError, match="100 files"):
        zenodo._check_values({**file_values, "data_files": "\n".join(f"file{i}.txt" for i in range(101))})


def test_people_preserve_family_names_roles_organizations_and_order(monkeypatch):
    from unittest.mock import MagicMock, Mock, call

    page = MagicMock()
    inputs, combos = {}, {}
    monkeypatch.setattr(zenodo, "_input", lambda root, path: inputs.setdefault(path, Mock()))
    monkeypatch.setattr(zenodo, "_combo", lambda root, path: combos.setdefault(path, Mock()))
    tag, choose = Mock(), Mock()
    monkeypatch.setattr(zenodo, "_tag", tag)
    monkeypatch.setattr(zenodo, "_choose", choose)
    zenodo._add_people(page, "van Example, Alex | Lab One; Lab Two | 0000-0002-1825-0097 | Editor | Person\nExample Collaboration | Lab Three | | | Organization")
    inputs["person_or_org.family_name"].fill.assert_called_once_with("van Example")
    inputs["person_or_org.given_name"].fill.assert_called_once_with("Alex")
    inputs["person_or_org.name"].fill.assert_called_once_with("Example Collaboration")
    assert [c.args[1] for c in tag.call_args_list] == ["0000-0002-1825-0097", "Lab One", "Lab Two", "Lab Three"]
    assert choose.call_args.args[1] == "Editor"
    assert page.get_by_role.call_args_list == [call("button", name="Add author", exact=True)] * 2


def test_contributor_column_order_includes_required_role(monkeypatch):
    from unittest.mock import MagicMock, Mock

    inputs, combos = {}, {}
    monkeypatch.setattr(zenodo, "_input", lambda root, path: inputs.setdefault(path, Mock()))
    monkeypatch.setattr(zenodo, "_combo", lambda root, path: combos.setdefault(path, Mock()))
    tag, choose = Mock(), Mock()
    monkeypatch.setattr(zenodo, "_tag", tag)
    monkeypatch.setattr(zenodo, "_choose", choose)
    zenodo._add_people(MagicMock(), "Reviewer, Pat | Data curator | Example Institute | 0000-0002-1825-0097", contributors=True)
    choose.assert_called_once_with(combos["role"], "Data curator")
    assert [c.args[1] for c in tag.call_args_list] == ["0000-0002-1825-0097", "Example Institute"]


@pytest.mark.parametrize("identifier,expected", [("10.1234/test", "DOI"), ("https://doi.org/10.1234/test", "DOI"), ("https://example.org/data", "URL")])
def test_related_identifier_scheme_inference(identifier, expected):
    assert zenodo._scheme(identifier, "") == expected
    assert zenodo._scheme("123", "PMID") == "PMID"


def _deposit_mock(monkeypatch):
    from unittest.mock import MagicMock, Mock

    page = MagicMock()
    controls = {}
    page.get_by_role.side_effect = lambda role, **kwargs: controls.setdefault((role, str(kwargs.get("name"))), Mock())
    page.locator.return_value.get_by_role.return_value.filter.return_value.get_by_role.return_value = Mock()
    response = page.expect_response.return_value.__enter__.return_value.value
    response.ok = True
    response.json.return_value = {"errors": []}
    for name in ("_choose", "_add_people", "_fill_license", "_fill_funding", "_fill_identifiers"):
        monkeypatch.setattr(zenodo, name, Mock())
    return page, controls, response


def test_deposit_uploads_automatically_saves_and_never_publishes(file_values, monkeypatch):
    page, controls, _ = _deposit_mock(monkeypatch)
    zenodo.VENUE._fill_deposit(page, file_values)
    page.locator.assert_any_call(zenodo.FILE_INPUT)
    page.locator.return_value.set_input_files.assert_called_once_with([file_values["data_files"]])
    controls[("button", "Save draft")].click.assert_called_once()
    # Upload starts on selection. Neither DOI reservation nor publishing is a
    # runner action, and the final controls are only checked for visibility.
    assert not any(name.startswith("Upload ") or name == "Get a DOI now!" for role, name in controls)
    for (role, name), control in controls.items():
        if "Publish" in name or name == "Preview":
            control.wait_for.assert_called_once_with(state="visible")
            control.click.assert_not_called()
    assert page.locator.call_args_list[1].args[0] == zenodo.DOI_RADIO.format(value="managed")


@pytest.mark.parametrize("ok,errors", [(False, []), (True, [{"field": "metadata.title", "messages": ["Required"]}])])
def test_failed_or_invalid_save_cannot_report_ready(file_values, monkeypatch, ok, errors):
    page, controls, response = _deposit_mock(monkeypatch)
    response.ok = ok
    response.status = 400
    response.json.return_value = {"errors": errors}
    with pytest.raises(ValueError, match="Zenodo"):
        zenodo.VENUE._fill_deposit(page, file_values)
    assert ("button", "Preview") not in controls


def test_existing_doi_uses_unmanaged_mode(file_values, monkeypatch):
    page, controls, _ = _deposit_mock(monkeypatch)
    zenodo.VENUE._fill_deposit(page, {**file_values, "doi": "10.1234/existing"})
    page.locator.assert_any_call(zenodo.DOI_RADIO.format(value="unmanaged"))
    controls[("textbox", "Copy/paste your existing DOI here...")].fill.assert_called_once_with("10.1234/existing")


def test_related_identifier_rows_start_empty(monkeypatch):
    from unittest.mock import MagicMock, Mock

    page = MagicMock()
    choose = Mock()
    monkeypatch.setattr(zenodo, "_choose", choose)
    zenodo._fill_identifiers(page, "10.1234/test | Cites\nhttps://example.org/test | Is supplement to | URL | Dataset", related=True)
    assert page.get_by_role.call_count == 2
    assert [c.args[1] for c in choose.call_args_list] == ["Cites", "DOI", "Is supplement to", "URL", "Dataset"]


def test_community_selector_requires_exact_identifier_not_first_result(page):
    page.set_content("""<button>Select a community</button>
      <div role="dialog" aria-label="Select a community">
        <input aria-label="Search in all communities"><button>Search</button>
        <div class="community-item"><a href="https://zenodo.org/communities/wrong/">Target name</a><button onclick="window.picked='wrong'">Select</button></div>
        <div class="community-item"><a href="https://zenodo.org/communities/target/">Target name</a><button onclick="window.picked='target';this.closest('[role=dialog]').remove()">Select</button></div>
      </div>""")
    zenodo._select_community(page, "target")
    assert page.evaluate("window.picked") == "target"


def test_modal_vocabulary_selection_does_not_tab_into_or_dismiss_other_controls():
    from unittest.mock import Mock

    combo = Mock()
    zenodo._choose(combo, "English")
    combo.get_by_role.assert_called_once_with("option", name="English", exact=True)
    combo.locator.return_value.press.assert_not_called()
    combo.reset_mock()
    zenodo._tag(combo, "synthetic keyword")
    combo.locator.return_value.press.assert_not_called()
    combo.get_by_role.assert_any_call("option", name="Add synthetic keyword", exact=True)


def test_publishing_fields_target_native_paths(file_values, monkeypatch):
    from unittest.mock import Mock

    page, _, _ = _deposit_mock(monkeypatch)
    inputs = {}
    monkeypatch.setattr(zenodo, "_input", lambda root, path: inputs.setdefault(path, Mock()))
    zenodo.VENUE._fill_deposit(page, {**file_values, **{key: f"test-{key}" for key in zenodo.CUSTOM_TEXT_FIELDS}})
    for key, path in zenodo.CUSTOM_TEXT_FIELDS.items():
        inputs[path].fill.assert_called_once_with(f"test-{key}")


def test_upload_quota_is_checked_before_browser(file_values, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(zenodo.Path, "stat", lambda self: SimpleNamespace(st_size=50_000_000_001))
    monkeypatch.setattr(zenodo.Path, "is_file", lambda self: True)
    with pytest.raises(ValueError, match="50 GB"):
        zenodo._check_values(file_values)


def test_keywords_wait_for_async_addition_and_keep_every_tag(page):
    page.set_content("""<div role="combobox" name="metadata.subjects"><input oninput="search(this)"><div role="listbox"></div></div>
      <script>
      function search(input) {
        const root = input.parentElement, list = root.querySelector('[role=listbox]');
        list.replaceChildren();
        const value = input.value;
        setTimeout(() => {
          if (input.value !== value) return;
          const option = document.createElement('div');
          option.setAttribute('role', 'option');
          option.textContent = 'Add ' + value;
          option.onclick = () => {
            const chip = document.createElement('a');
            chip.className = 'ui label'; chip.textContent = value;
            root.prepend(chip); input.value = ''; list.replaceChildren();
          };
          list.append(option);
        }, 30);
      }
      </script>""")
    combo = zenodo._combo(page, "metadata.subjects")
    zenodo._tag(combo, "synthetic data")
    zenodo._tag(combo, "automation test")
    assert combo.locator("a.ui.label").all_text_contents() == ["automation test", "synthetic data"]
