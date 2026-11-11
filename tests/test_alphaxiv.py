"""alphaXiv dispatch, form invariants, and Edit-step helpers."""

from unittest.mock import MagicMock, Mock

import pytest

from paperpush.database import get_venue
from paperpush.venues import get_runner, get_venue_impl
from paperpush.venues.alphaxiv import alphaxiv
from paperpush.venues.recovery_channel import is_final_submit_label

SLUG = "alphaxiv"


def _rows(count):
    rows = MagicMock()
    state = {"count": count}
    rows.count.side_effect = lambda: state["count"]
    return rows, state


def test_dispatch_and_form_invariants():
    venue = get_venue(SLUG)
    impl = get_venue_impl(SLUG)
    assert get_runner(SLUG).__self__ is impl
    assert impl.supports_recovery
    fields = {f.id: f for f in venue.fields}
    assert fields["manuscript_file"].accept == [".pdf"]
    assert fields["manuscript_file"].max_file_size_mb == 50
    assert fields["agree_terms"].confirm and fields["agree_terms"].autofill == "never"
    assert "Computer Science" in fields["categories"].options
    assert "cs.AI" in fields["subcategories"].options


@pytest.mark.parametrize("label", ["Submit", "Submit paper for review", "Publish anyway"])
def test_send_controls_are_refused_to_recovery(label):
    assert is_final_submit_label(label, get_venue_impl(SLUG).final_submit_names)


def test_headless_refused_before_opening_a_browser(monkeypatch):
    launcher = Mock()
    monkeypatch.setattr(alphaxiv, "sync_playwright", launcher)
    with pytest.raises(alphaxiv.AlphaXivError, match="visible browser"):
        get_venue_impl(SLUG).submit({"authors": "A"}, headless=True)
    launcher.assert_not_called()


def test_set_rows_reuses_extracted_row_adds_and_blurs():
    page = MagicMock()
    rows, state = _rows(1)
    page.get_by_placeholder.return_value = rows
    add = MagicMock()
    add.click.side_effect = lambda: state.update(count=state["count"] + 1)
    page.get_by_role.return_value = add
    alphaxiv._set_rows(page, ["Ada", "Grace"], "Author name", "Add author", "Remove author")
    assert add.click.call_count == 1
    assert [c.args[0] for c in rows.nth.call_args_list] == [0, 1]
    assert [c.args[0] for c in rows.nth.return_value.fill.call_args_list] == ["Ada", "Grace"]
    assert rows.nth.return_value.blur.call_count == 2


def test_set_rows_removes_extra_extracted_rows():
    page = MagicMock()
    rows, state = _rows(3)
    page.get_by_placeholder.return_value = rows
    remove = MagicMock()
    remove.last.click.side_effect = lambda: state.update(count=state["count"] - 1)
    page.get_by_role.return_value = remove
    alphaxiv._set_rows(page, ["Ada"], "Author name", "Add author", "Remove author")
    assert remove.last.click.call_count == 2


def _picker(existing):
    page = MagicMock()
    box = page.locator.return_value
    field = MagicMock()
    chips = MagicMock()
    state = {"count": existing}
    chips.count.side_effect = lambda: state["count"]
    box.locator.side_effect = lambda sel: field if sel == "input" else chips
    return page, field, state


def test_set_picker_clears_then_selects():
    page, field, state = _picker(2)

    def press(key):
        state["count"] += -1 if key == "Backspace" else 1

    field.press.side_effect = press
    alphaxiv._set_picker(page, "Subcategories", ["cs.AI", "cs.LG"])
    assert [c.args[0] for c in field.press.call_args_list] == ["Backspace", "Backspace", "Enter", "Enter"]
    assert [c.args[0] for c in field.fill.call_args_list] == ["cs.AI", "cs.LG"]


def test_set_picker_reports_unmatched_item():
    page, field, _ = _picker(0)
    with pytest.raises(alphaxiv.AlphaXivError, match="no match"):
        alphaxiv._set_picker(page, "Categories", ["Alchemy"])


def test_publication_date_validator():
    assert alphaxiv.FIELD_VALIDATORS["publication_date"]("2026-11-10") == "2026-11-10"
    with pytest.raises(ValueError):
        alphaxiv.FIELD_VALIDATORS["publication_date"]("11/10/2026")
