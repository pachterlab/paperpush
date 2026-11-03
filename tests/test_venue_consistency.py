"""scripts/check_venue_consistency.py: limits the forms and requirements files disagree on."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from paperpush.database import Venue
from paperpush.requirements import ManuscriptRequirements
from paperpush.requirements_check import _ROLE_FIELD_IDS

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location("check_venue_consistency", REPO_ROOT / "scripts" / "check_venue_consistency.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


cvc = _load_script()

ARTICLE_TYPE = {
    "id": "article_type",
    "label": "Article type",
    "type": "choice",
    "options": ["Article", "Review"],
}


def _venue(*fields: dict, **extra) -> Venue:
    return Venue.from_dict("demo", {"fields": [ARTICLE_TYPE, *fields], **extra})


def _reqs(**data) -> ManuscriptRequirements:
    return ManuscriptRequirements.from_dict("demo", data)


def _abstract(**limits) -> dict:
    return {"id": "abstract", "label": "Abstract", "type": "textarea", **limits}


def test_role_field_ids_match_validate():
    assert cvc.ROLE_FIELD_IDS == _ROLE_FIELD_IDS


def test_matching_limits_are_not_reported():
    venue = _venue(_abstract(word_count=150))
    assert cvc._scalar_discrepancies(venue, _reqs(abstract={"max_words": 150})) == []


def test_differing_abstract_limit_is_reported_with_both_paths():
    venue = _venue(_abstract(word_count=1000))
    (d,) = cvc._scalar_discrepancies(venue, _reqs(abstract={"max_words": 998}))
    assert (d.form_path, d.form_value) == ("demo.abstract.word_count", 1000)
    assert (d.req_path, d.req_value) == ("demo.abstract.max_words", 998)
    text = cvc.report([d])
    assert "submission_forms.json: demo.abstract.word_count = 1000" in text
    assert "manuscript_requirements.json: demo.abstract.max_words = 998" in text


def test_limit_recorded_in_one_file_only_is_not_reported():
    assert cvc._scalar_discrepancies(_venue(_abstract(word_count=150)), _reqs()) == []
    assert cvc._scalar_discrepancies(_venue(_abstract()), _reqs(abstract={"max_words": 150})) == []


def test_stricter_article_type_is_expected_but_looser_one_is_reported():
    venue = _venue(_abstract(word_count=150))
    stricter = _reqs(
        article_type_field="article_type",
        abstract={"max_words": 150},
        article_types={"Review": {"abstract": {"max_words": 75}}},
    )
    assert cvc._scalar_discrepancies(venue, stricter) == []
    looser = _reqs(
        article_type_field="article_type",
        abstract={"max_words": 150},
        article_types={"Review": {"abstract": {"max_words": 300}}},
    )
    (d,) = cvc._scalar_discrepancies(venue, looser)
    assert d.req_path == 'demo.article_types["Review"].abstract.max_words'


def test_conditional_form_limit_is_compared_per_article_type():
    manuscript = {
        "id": "manuscript_file",
        "label": "Manuscript",
        "type": "file",
        "max_pages_by": {
            "field": "article_type",
            "values": {"Article": 7, "Review": 8},
        },
    }
    reqs = _reqs(
        article_type_field="article_type",
        article_types={
            "Article": {"manuscript": {"max_pages": 7}},
            "Review": {"manuscript": {"max_pages": 10}},
        },
    )
    (d,) = cvc._conditional_discrepancies(_venue(manuscript), reqs)
    assert d.form_path == 'demo.manuscript_file.max_pages_by["Review"]'
    assert (d.form_value, d.req_value) == (8, 10)


def test_conditional_limit_ignores_a_type_the_guidelines_set_no_limit_for():
    # Review states no page limit, so the top-level 7 (Article's) is not its rule.
    manuscript = {"id": "manuscript_file", "label": "Manuscript", "type": "file", "max_pages_by": {"field": "article_type", "values": {"Review": 2}}}
    reqs = _reqs(article_type_field="article_type", manuscript={"max_pages": 7}, article_types={"Article": {}, "Review": {"manuscript": {"notes": ["No explicit limit."]}}})
    assert cvc._conditional_discrepancies(_venue(manuscript), reqs) == []


def test_formats_report_only_what_the_portal_rejects_by_default():
    manuscript = {
        "id": "manuscript_file",
        "label": "Manuscript",
        "type": "file",
        "accept": [".pdf", ".zip", ".jpeg"],
    }
    venue = _venue(manuscript)
    # The portal accepting more (.zip) is normal; .jpg and .jpeg are one format.
    assert cvc._format_discrepancies(venue, _reqs(manuscript={"formats": [".pdf", ".jpg"]})) == []
    assert len(cvc._format_discrepancies(venue, _reqs(manuscript={"formats": [".pdf"]}), all_formats=True)) == 1
    (d,) = cvc._format_discrepancies(venue, _reqs(manuscript={"formats": [".pdf", ".tex"]}))
    assert ".tex" in d.note


def test_upload_cap_and_cover_letter_requirement():
    cover = {
        "id": "cover_letter",
        "label": "Cover letter",
        "type": "file",
        "required": False,
    }
    found = cvc._venue_level_discrepancies(
        _venue(cover, max_upload_mb=40),
        _reqs(upload={"max_total_mb": 50}, cover_letter={"required": True}),
    )
    assert {d.req_path for d in found} == {
        "demo.upload.max_total_mb",
        "demo.cover_letter.required",
    }


def test_article_types_must_match_the_forms_options():
    reqs = _reqs(article_type_field="article_type", article_types={"Article": {}, "Letter": {}})
    (d,) = cvc._reference_discrepancies(_venue(), reqs)
    assert d.req_value == ["Letter"]
    (d,) = cvc._reference_discrepancies(
        _venue(),
        _reqs(article_type_field="manuscript_type", article_types={"Article": {}}),
    )
    assert "does not have" in d.note


def test_main_warns_without_failing_unless_strict(monkeypatch, capsys):
    monkeypatch.setenv("PAPERPUSH_VENUE_DATA", "bundled")  # main() sets it; undone after the test
    monkeypatch.setattr(
        cvc,
        "find_discrepancies",
        lambda slugs=None, all_formats=False: [cvc.Discrepancy("demo", "a", 1, "b", 2)],
    )
    assert cvc.main([]) == 0
    assert "1 value(s) differ" in capsys.readouterr().err
    assert cvc.main(["--strict"]) == 1


def test_runs_on_the_checked_in_data(monkeypatch):
    monkeypatch.setenv("PAPERPUSH_VENUE_DATA", "bundled")
    assert cvc.main([]) == 0
