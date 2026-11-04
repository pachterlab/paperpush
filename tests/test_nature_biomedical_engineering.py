"""Nature Biomedical Engineering dispatch, Variant, and validation-tab wait."""

from unittest.mock import MagicMock, Mock

import pytest
from playwright.sync_api import TimeoutError as PWTimeout

from paperpush.database import get_venue
from paperpush.requirements import get_requirements
from paperpush.venues import get_runner, get_venue_impl, submission_base
from paperpush.venues.nature import main

SLUG = "nature_biomedical_engineering"


def test_nbe_has_own_portal_session_and_nature_form():
    venue = get_venue(SLUG)
    impl = get_venue_impl(SLUG)
    assert venue.inherits == "nature"
    assert impl.variant.portal_url == "https://mts-natbiomedeng.nature.com/cgi-bin/main.plex"
    assert submission_base(SLUG) == SLUG
    assert impl.session_path() != get_venue_impl("nature").session_path()
    fields = {f.id: f for f in venue.fields}
    assert fields["manuscript_type"].default == "Article"
    # The seven types on the live manuscript-type page (2026-11-04).
    assert fields["manuscript_type"].options == [
        "Article",
        "Review Article",
        "Perspective",
        "Comment",
        "Correspondence",
        "Matters Arising",
        "Down to Business",
    ]
    assert fields["authors"].fields == next(f for f in get_venue("nature").fields if f.id == "authors").fields


def test_nbe_requirements_key_article_types_on_the_form_field():
    reqs = get_requirements(SLUG)
    options = {f.id: f for f in get_venue(SLUG).fields}["manuscript_type"].options
    assert reqs.article_type_field == "manuscript_type"
    assert set(reqs.article_types) <= set(options)


def test_nbe_variant_matches_live_portal():
    """Observed on the live portal: dashboard link, four default author panes with
    'More Authors' (no num_authors box), and NMI's Validate tabs before the
    author-only 'Approve Submission' button."""
    cfg = main.VARIANTS[SLUG]
    assert cfg.submit_new_link == "Submit Manuscript"
    assert cfg.author_count_mode == "additional"
    assert cfg.validate_before_review
    assert not cfg.has_open_access
    assert not cfg.has_external_consultation


def test_nbe_submit_dispatches_shared_engine_with_own_venue(monkeypatch):
    submit = Mock()
    monkeypatch.setattr(main, "submit_nature", submit)
    values = {"title": "Example", "manuscript_type": "Article"}
    get_runner(SLUG)(values, headless=True, keep_open_on_failure=False)
    assert submit.call_args.args == (values,)
    assert submit.call_args.kwargs["venue"] is get_venue_impl(SLUG)


def _conversion_page(pending_counts):
    page = MagicMock()
    pending = Mock()
    pending.count.side_effect = pending_counts
    page.get_by_text.return_value = pending
    refresh = Mock()
    page.get_by_role.return_value = refresh
    return page, refresh


def test_conversion_wait_waits_for_each_refresh_navigation():
    """Regression: the refresh link navigates after its click returns, so reading
    the page straight away saw the old page and ended the wait early. Each click
    must sit inside expect_navigation, and the loop keys on the pending rows,
    not on the refresh link (NBE keeps that link after conversion finishes)."""
    page, refresh = _conversion_page([5, 5, 0])
    main._wait_for_pdf_conversion(page, main.VARIANTS[SLUG])
    page.get_by_text.assert_called_once_with(main.PDF_PENDING_TEXT)
    assert refresh.click.call_count == 2
    assert page.expect_navigation.call_count == 2
    page.expect_navigation.assert_called_with(wait_until="load")
    refresh.is_visible.assert_not_called()


def test_conversion_wait_is_bounded(monkeypatch):
    page, refresh = _conversion_page(lambda: 3)  # never finishes converting
    clock = iter([0.0, main.CONVERSION_TIMEOUT_S + 1])
    monkeypatch.setattr(main.time, "monotonic", lambda: next(clock))
    with pytest.raises(PWTimeout, match="3 uploaded file"):
        main._wait_for_pdf_conversion(page, main.VARIANTS[SLUG])
    refresh.click.assert_not_called()


def test_pending_text_skips_the_instruction_bullet():
    assert main.PDF_PENDING_TEXT.search("PDF Not Yet Converted Upload PDF")
    assert not main.PDF_PENDING_TEXT.search("If you see “PDF Not Yet Converted”, you may upload your own PDF")
