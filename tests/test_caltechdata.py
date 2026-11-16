"""CaltechDATA dispatch and access.caltech authentication safeguards."""

from unittest.mock import MagicMock, Mock

import pytest
from playwright.sync_api import TimeoutError as PWTimeout

from paperpush.venues import get_runner, get_venue_impl
from paperpush.venues.invenio import caltechdata as module
from paperpush.venues.login import VenueLoginError


def test_dispatch_and_publication_guard():
    venue = get_venue_impl("caltechdata")
    assert get_runner("caltechdata") == venue.submit
    assert venue.requires_login
    assert venue.supports_recovery
    assert "Publish" in venue.final_submit_names


def test_guest_dashboard_link_is_not_a_login_marker():
    page = Mock()
    page.locator.return_value.first.wait_for.side_effect = PWTimeout("no logout")
    assert not module.VENUE.is_logged_in(page, timeout_ms=10)
    page.get_by_role.assert_not_called()


def test_collapsed_logout_menu_marks_authenticated_session():
    page = Mock()
    assert module.VENUE.is_logged_in(page, timeout_ms=10)
    page.locator.return_value.first.wait_for.assert_called_once_with(state="attached", timeout=10)


def test_login_leaves_two_factor_to_author(monkeypatch):
    page = Mock()
    monkeypatch.setattr(module, "first_present", lambda *args: None)
    monkeypatch.setattr(module.VENUE, "is_logged_in", lambda *args, **kwargs: False)
    with pytest.raises(VenueLoginError, match="two-factor"):
        module.VENUE.login(page, "example", "example-secret")
    page.get_by_label.return_value.fill.assert_called_once_with("example-secret")
    assert not any("Yes, this is my device" in str(call) for call in page.mock_calls)


@pytest.mark.parametrize("name,expected", [("van Example, Alex Taylor", ("van Example", "Alex Taylor")), ("Alex Example", ("Example", "Alex")), ("Mononym", ("Mononym", ""))])
def test_person_name_boundaries(name, expected):
    assert module._person_name(name) == expected


@pytest.mark.parametrize("values,reason", [({"metadata_only": "yes", "data_files": "image.png"}, "metadata_only"), ({}, "data_files"), ({"metadata_only": "yes", "existing_doi": "10.1234/test", "reserve_doi": "yes"}, "existing_doi"), ({"metadata_only": "yes", "embargo_date": "2027-01-01"}, "Restricted"), ({"metadata_only": "yes", "embargo_reason": "pending publication"}, "embargo_date")])
def test_invalid_cross_field_choices_stop_before_browser_launch(values, reason, monkeypatch):
    launch = Mock()
    monkeypatch.setattr(module, "sync_playwright", launch)
    with pytest.raises(ValueError, match=reason):
        module.VENUE.submit(values)
    launch.assert_not_called()


def test_files_preserve_order_and_use_only_path_column(tmp_path):
    a, b = tmp_path / "second.png", tmp_path / "first.png"
    for path in (a, b):
        path.touch()
    assert module._check_values({"data_files": f"{a} | Figure B\n{b} | Figure A"}) == [str(a), str(b)]


def test_duplicate_upload_names_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="unique filenames"):
        module._check_values({"data_files": f"{tmp_path}/one/file.png\n{tmp_path}/two/file.png"})


@pytest.mark.parametrize("identifier,expected", [("10.22002/example", "DOI"), ("https://doi.org/10.22002/example", "DOI"), ("https://example.org/data", "URL")])
def test_related_identifiers_infer_only_unambiguous_schemes(identifier, expected):
    assert module._scheme(identifier, "") == expected
    assert module._scheme("123", "PMID") == "PMID"
    with pytest.raises(ValueError, match="scheme"):
        module._scheme("123", "")


def test_people_keep_order_affiliations_and_organization_names(monkeypatch):
    page = MagicMock()
    inputs, combos = {}, {}
    monkeypatch.setattr(module, "_input", lambda root, path: inputs.setdefault(path, Mock()))
    monkeypatch.setattr(module, "_combo", lambda root, path: combos.setdefault(path, Mock()))
    tag = Mock()
    monkeypatch.setattr(module, "_tag", tag)
    module._add_people(page, "van Example, Alex | Lab One; Lab Two | 0000-0001-2345-6789 | Person\nExample Collaboration | Lab Three | | Organization")
    inputs["person_or_org.family_name"].fill.assert_called_once_with("van Example")
    inputs["person_or_org.given_name"].fill.assert_called_once_with("Alex")
    inputs["person_or_org.name"].fill.assert_called_once_with("Example Collaboration")
    assert [call.args[1] for call in tag.call_args_list] == ["0000-0001-2345-6789", "Lab One", "Lab Two", "Lab Three"]
    assert page.get_by_role.call_args_list[0].kwargs["name"] == "Add creator"


def test_draft_save_never_publishes_or_reserves_doi_by_default(monkeypatch):
    page = MagicMock()
    controls = {}

    def control(role, **kwargs):
        return controls.setdefault((role, str(kwargs.get("name"))), Mock())

    page.get_by_role.side_effect = control
    saved = page.expect_response.return_value.__enter__.return_value.value
    saved.ok = True
    saved.json.return_value = {"errors": []}
    monkeypatch.setattr(module, "_choose", Mock())
    module.VENUE._fill_deposit(page, {"metadata_only": "yes", "title": "Synthetic test"})
    buttons = [call for call in page.get_by_role.call_args_list if call.args == ("button",)]
    assert any(call.kwargs.get("name") == "Save draft" for call in buttons)
    assert not any(call.kwargs.get("name") == "Get a DOI now!" for call in buttons)
    controls[("button", "Save draft")].click.assert_called_once()
    for (role, name), button in controls.items():
        if "Publish" in name or "Preview" in name:
            button.wait_for.assert_called()
            button.click.assert_not_called()


def test_existing_doi_is_entered_without_reserving_another(monkeypatch):
    page = MagicMock()
    controls = {}
    page.get_by_role.side_effect = lambda role, **kwargs: controls.setdefault((role, str(kwargs.get("name"))), Mock())
    saved = page.expect_response.return_value.__enter__.return_value.value
    saved.ok = True
    saved.json.return_value = {"errors": []}
    monkeypatch.setattr(module, "_choose", Mock())
    module.VENUE._fill_deposit(page, {"metadata_only": "yes", "existing_doi": "10.1234/example", "title": "Synthetic test"})
    controls[("radio", "Yes, I already have one")].check.assert_called_once_with(force=True)
    controls[("textbox", "Copy/paste your existing DOI here...")].fill.assert_called_once_with("10.1234/example")
    assert ("button", "Get a DOI now!") not in controls


def test_exact_funder_name_does_not_select_a_prefix_match():
    """Exercise the real selectors against conflicting labels, without network."""
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Error as exc:
            pytest.skip(f"local Playwright browser unavailable: {exc}")
        page = browser.new_page()
        page.set_default_timeout(1500)
        page.set_content("""
          <section id="funding-section"><button onclick="document.querySelector('.modal').style.display='block'">Add custom</button></section>
          <div class="ui modal visible active" style="display:none">
            <div role="combobox"><input>
              <div role="option" name="National Science Foundation of Sri Lanka" pid="wrong" onclick="window.picked='wrong'">National Science Foundation of Sri Lanka, Sri Lanka</div>
              <div role="option" name="U.S. National Science Foundation" pid="021nxhr62" onclick="window.picked='right'">U.S. National Science Foundation, United States, 021nxhr62</div>
            </div>
            <label>Number<input></label><label>Title<input></label><label>URL<input></label>
            <button onclick="document.querySelector('.modal').style.display='none'">Add</button>
          </div>""")
        module._fill_funding(page, "U.S. National Science Foundation | TEST-123")
        assert page.evaluate("window.picked") == "right"
        page.evaluate("window.picked = null")
        module._fill_funding(page, "https://ror.org/021nxhr62")
        assert page.evaluate("window.picked") == "right"
        with pytest.raises(PWTimeout):
            module._fill_funding(page, "National Science Foundation")
        browser.close()


def test_form_matches_current_portal_fields():
    from paperpush.database import get_venue

    fields = {field.id: field for field in get_venue("caltechdata").fields}
    assert "contributor_email" not in fields  # Removed from the current portal.
    assert fields["data_files"].max_count == 50
    assert fields["resource_type"].options.count("Image") == 1
    assert "Map" in fields["resource_type"].options
    assert fields["existing_doi"].autofill == "never"
    assert fields["reserve_doi"].autofill == "never"
