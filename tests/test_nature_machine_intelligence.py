"""Nature Machine Intelligence dispatch and isolation from flagship Nature."""

from unittest.mock import Mock

from paperpush.database import get_venue
from paperpush.requirements import get_requirements
from paperpush.venues import get_runner, get_venue_impl, submission_base
from paperpush.venues.nature import main

SLUG = "nature_machine_intelligence"


def test_nmi_has_own_portal_session_and_nature_form():
    venue = get_venue(SLUG)
    impl = get_venue_impl(SLUG)
    assert venue.inherits == "nature"
    assert impl.variant.portal_url == "https://mts-natmachintell.nature.com/cgi-bin/main.plex"
    assert submission_base(SLUG) == SLUG
    assert impl.session_path() != get_venue_impl("nature").session_path()
    fields = {f.id: f for f in venue.fields}
    assert fields["manuscript_type"].default == "Article"
    assert "Biological Sciences - Article" not in fields["manuscript_type"].options
    assert fields["authors"].fields == next(f for f in get_venue("nature").fields if f.id == "authors").fields
    assert get_requirements(SLUG).article_type_field == "manuscript_type"


def test_nmi_submit_dispatches_shared_engine_with_own_venue(monkeypatch):
    submit = Mock()
    monkeypatch.setattr(main, "submit_nature", submit)
    values = {"title": "Example", "manuscript_type": "Article"}
    get_runner(SLUG)(values, headless=True, keep_open_on_failure=False)
    assert submit.call_args.args == (values,)
    assert submit.call_args.kwargs["venue"] is get_venue_impl(SLUG)
    assert submit.call_args.kwargs["headless"] is True
    assert submit.call_args.kwargs["keep_open_on_failure"] is False


def test_metadata_uses_upload_ids_when_figures_have_no_color_controls():
    """NMI figures must not be dropped or shift the supplementary title."""
    page = Mock()
    controls = {}

    def locator(selector):
        control = controls.setdefault(selector, Mock())
        control.count.return_value = 0 if "color_" in selector else 1
        return control

    page.locator.side_effect = locator
    main._enter_file_metadata(
        page,
        {
            "101": ("paper.pdf", "Article File", "", ""),
            "102": ("figure.tif", "Figure", "Figure 1", "color"),
            "104": (
                "supp.pdf",
                "Supplementary Information",
                "Supplementary Information",
                "Methods",
            ),
        },
    )
    controls["input[name='object_title_102']"].fill.assert_called_once_with("Figure 1")
    controls["input[name='object_title_104']"].fill.assert_called_once_with("Supplementary Information")
    controls["textarea[name='object_desc_104']"].fill.assert_called_once_with("Methods")
    assert "input[name='object_title_101']" not in controls
    assert not any("[value=" in selector for selector in controls)


def test_metadata_keeps_nature_figure_color_choice():
    page = Mock()
    page.locator.return_value.count.return_value = 1
    main._enter_file_metadata(page, {"9": ("figure.tif", "Figure", "", "bw")})
    page.locator.assert_any_call("input[name='color_9'][value='bw']")
    page.locator.return_value.fill.assert_called_once_with("figure")


def test_nmi_review_stops_before_final_approval(monkeypatch):
    from unittest.mock import MagicMock

    page = MagicMock()
    final_button = Mock()
    links = MagicMock()
    links.count.return_value = 2
    refresh = Mock()
    pending = Mock()
    pending.count.side_effect = [1, 0]
    page.get_by_text.side_effect = lambda text, **kwargs: pending if text is main.PDF_PENDING_TEXT else Mock()
    roles = {"Approve Submission": final_button, "Validate PDF": links, "Check for completion of file conversion.": refresh}
    page.get_by_role.side_effect = lambda role, **kwargs: roles[kwargs["name"]]
    page.locator.return_value.count.return_value = 2
    click = Mock()
    monkeypatch.setattr(main, "_click_button", click)
    main._advance_to_submission_review(page)
    assert click.call_count == 2
    assert all(call.args == (page, "Save and Continue") for call in click.call_args_list)
    page.get_by_text.assert_any_call("No errors found.", exact=True)
    final_button.wait_for.assert_called_once_with(state="visible")
    final_button.click.assert_not_called()
    assert page.expect_popup.call_count == 2
    refresh.click.assert_called_once()
    page.wait_for_timeout.assert_any_call(main.CONVERSION_POLL_MS)
    page.expect_navigation.assert_called_once_with(wait_until="load")


def test_upload_ids_follow_upload_order_not_dom_order(monkeypatch):
    page = Mock()
    page.locator.return_value.evaluate_all.return_value = [
        "object_file_type_105",
        "object_file_type_101",
    ]
    monkeypatch.setattr(main, "_click_button", Mock())
    files = [
        ("paper.pdf", "Article File", "", ""),
        ("figure.tif", "Figure", "Figure 1", ""),
    ]
    assert main._upload_files(page, files) == {"101": files[0], "105": files[1]}


def test_nmi_variant_matches_live_dashboard_and_declarations():
    cfg = main.VARIANTS[SLUG]
    assert cfg.submit_new_link == "Submit Manuscript"
    assert cfg.validate_before_review
    assert not cfg.has_open_access
    assert not cfg.has_external_consultation
    assert not main.VARIANTS["nature"].validate_before_review
