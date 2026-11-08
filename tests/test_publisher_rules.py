"""Publisher house rules backing a venue's own (manuscript_requirements.json ``publisher:<slug>`` entries)."""

from __future__ import annotations

import copy

import pytest

from paperpush import requirements as req_mod
from paperpush import report
from paperpush.database import get_venue
from paperpush.requirements import get_publisher, get_requirements, list_requirements, own_rules_only, publisher_note_tag, resolve
from paperpush.requirements_check import _text_fields_check
from paperpush.validate import ERROR, WARNING, Checks, Issue

HOUSE = {
    "source_urls": ["https://example.org/springer-nature-house-rules"],
    "retrieved": "2026-11-01",
    "abstract": {"max_words": 50},
    "keywords": {"max": 3},
    "notes": ["Data availability statements are mandatory."],
}


@pytest.fixture
def house_rules(monkeypatch):
    """The bundled database plus Springer Nature house rules."""
    stored = copy.deepcopy(req_mod._load_raw())
    stored["$publishers"]["springer_nature"]["rules"] = copy.deepcopy(HOUSE)
    raw = req_mod._expand_publishers(stored)
    monkeypatch.setattr(req_mod, "_load_raw", lambda: raw)
    return raw


def test_venues_name_known_publishers():
    known = {p.slug for p in req_mod.list_publishers()}
    from paperpush.database import list_venues

    named = {v.publisher for v in list_venues() if v.publisher}
    assert named and named <= known
    assert get_venue("nature").publisher == "springer_nature"
    assert get_venue("cell").publisher == "cell_press"


def test_get_publisher_matches_slug_name_and_crossref_name():
    assert get_publisher("springer_nature").slug == "springer_nature"
    assert get_publisher("Springer Nature").slug == "springer_nature"
    assert get_publisher("Springer Science and Business Media LLC").slug == "springer_nature"
    assert get_publisher("publisher:elsevier").slug == "elsevier"
    assert get_publisher("no such press") is None


def test_no_house_rules_leaves_venue_unchanged(monkeypatch):
    stored = {k: v for k, v in copy.deepcopy(req_mod._load_raw()).items() if not k.startswith("publisher:")}
    for entry in stored["$publishers"].values():
        entry.pop("rules", None)
    monkeypatch.setattr(req_mod, "_load_raw", lambda: req_mod._expand_publishers(stored))
    reqs = get_requirements("nature")
    assert reqs.publisher == "" and not reqs.from_publisher


def test_venue_rules_win_and_gaps_fall_back(house_rules):
    reqs = get_requirements("nature")
    assert reqs.publisher == "springer_nature"
    # Nature states its own abstract limit; it has no keyword limit, so the house rule fills it.
    assert reqs.abstract.max_words == 200
    assert reqs.keywords.max == 3
    assert "keywords.max" in reqs.from_publisher
    assert "abstract.max_words" not in reqs.from_publisher
    tag = publisher_note_tag("springer_nature")
    assert tag + "Data availability statements are mandatory." in reqs.notes
    # The venue's own sources stay its own.
    assert "https://example.org/springer-nature-house-rules" not in reqs.source_urls


def test_without_publisher_rules_is_the_venue_alone(house_rules):
    own = get_requirements("nature").without_publisher_rules()
    assert own.keywords.max is None
    assert own.abstract.max_words == 200
    assert not any(n.startswith("[") for n in own.notes)
    with own_rules_only():
        assert get_requirements("nature").keywords.max is None


def test_null_in_venue_drops_house_rule(house_rules):
    house_rules["nature"] = {**house_rules["nature"], "keywords": {"max": None}}
    reqs = get_requirements("nature")
    assert reqs.keywords.max is None
    assert "keywords.max" not in reqs.from_publisher


def test_publisher_entries_are_not_venues(house_rules):
    assert all(not r.slug.startswith("publisher:") for r in list_requirements())


def test_publisher_venue_for_unlisted_journal(house_rules):
    venue = get_venue("publisher:springer_nature")
    assert venue.slug == "publisher:springer_nature" and not venue.has_form
    reqs = get_requirements(venue.slug)
    assert reqs.publisher_rules_only
    assert {"abstract.max_words", "keywords.max"} <= reqs.from_publisher
    assert reqs.without_publisher_rules().abstract.max_words is None
    assert get_venue("publisher:Springer Nature").slug == "publisher:springer_nature"


def test_publisher_venue_needs_house_rules():
    with pytest.raises(KeyError):
        get_venue("publisher:wiley")


def test_resolve_keeps_provenance(house_rules):
    reqs = get_requirements("nature")
    assert resolve(reqs, {}).from_publisher == reqs.from_publisher


def _keyword_values(venue, n: int) -> dict[str, str]:
    field = next(f for f in venue.fields if f.id == "keywords")
    sep = "\n" if field.type in ("multichoice", "list") else ", "
    return {"title": "A study", "keywords": sep.join(f"kw{i}" for i in range(n)), "abstract": "word " * 120}


def test_validate_reports_house_rule_as_warning(house_rules):
    venue = get_venue("publisher:springer_nature")
    values = _keyword_values(venue, 5)
    checks = Checks()
    checks.use_publisher_fallback(venue, values)
    reqs = resolve(get_requirements(venue.slug), values)
    result = checks.run("guidelines.text_fields", "limits", "Guidelines", _text_fields_check, venue, reqs, values)
    assert result.status == report.WARNING
    assert result.issues and all(i.level == WARNING and "Springer Nature house rule" in i.message for i in result.issues)
    # The re-run without the house rules left nothing behind.
    assert len(checks.results) == 1 and not checks.ai_tasks


def test_validate_keeps_venue_rule_as_error(house_rules):
    venue = get_venue("publisher:springer_nature")
    values = _keyword_values(venue, 1)
    checks = Checks()
    checks.use_publisher_fallback(venue, values)

    def check(reqs):
        own = Issue(ERROR, "abstract", "the venue's own rule")
        return [own, Issue(ERROR, "keywords", "a house rule")] if reqs.keywords.max else [own]

    result = checks.run("x", "x", "Guidelines", check, resolve(get_requirements(venue.slug), values))
    levels = {i.field: i.level for i in result.issues}
    assert levels == {"abstract": ERROR, "keywords": WARNING}
    assert result.status == report.FAILED


def test_no_fallback_without_house_rules():
    venue = get_venue("plos_one")  # PLOS has no house rules recorded
    checks = Checks()
    checks.use_publisher_fallback(venue, {})
    assert checks.publisher_fallback is None


def test_published_publishers_shape_is_checked():
    assert req_mod._publishers_problem({"elsevier": {"name": "Elsevier"}}) is None
    assert "does not know" in req_mod._publishers_problem({"elsevier": {"nmae": "Elsevier"}})
    assert "slug" in req_mod._publishers_problem({"Bad Slug": {}})


def test_crossref_name_matching():
    from paperpush.publisher_lookup import match_publisher, normalize_issn

    assert match_publisher("Elsevier BV").slug == "elsevier"
    assert match_publisher("Springer Science and Business Media LLC").slug == "springer_nature"
    assert match_publisher("Wiley-VCH Verlag") is None or match_publisher("Wiley-VCH Verlag").slug == "wiley"
    assert match_publisher("Some Small Press") is None
    assert normalize_issn("0028 0836".replace(" ", "")) == "0028-0836"
    with pytest.raises(ValueError):
        normalize_issn("12345")


def test_lookup_by_issn(monkeypatch, house_rules):
    from paperpush import publisher_lookup

    monkeypatch.setattr(publisher_lookup, "_get", lambda url: {"message": {"title": "Some Journal", "publisher": "Springer Science and Business Media LLC"}})
    found = publisher_lookup.lookup(issn="1234-5678")
    assert found.venue_slug == "publisher:springer_nature" and not found.problem

    monkeypatch.setattr(publisher_lookup, "_get", lambda url: {"message": {"title": "Other", "publisher": "Wiley"}})
    found = publisher_lookup.lookup(issn="1234-5678")
    assert found.venue_slug == "" and "no house rules" in found.problem

    monkeypatch.setattr(publisher_lookup, "_get", lambda url: None)
    assert "could not reach" in publisher_lookup.lookup(issn="1234-5678").problem


def test_house_rules_venue_filename(house_rules):
    from paperpush.subfile import default_filename

    assert default_filename(get_venue("publisher:springer_nature")) == "publisher_springer_nature.sub.txt"


def test_house_rules_venue_named_after_journal(house_rules):
    from paperpush.database import publisher_venue

    named = publisher_venue("springer_nature", journal="Journal of Things")
    assert named.name == "Journal of Things" and not named.journal_unnamed
    assert publisher_venue("springer_nature").journal_unnamed


def test_resolve_target(monkeypatch, house_rules):
    from paperpush import publisher_lookup

    assert publisher_lookup.resolve_target("general") is None
    assert publisher_lookup.resolve_target("", publisher="Springer Nature").venue.slug == "publisher:springer_nature"
    with pytest.raises(ValueError, match="is listed"):
        publisher_lookup.resolve_target("nature", publisher="springer_nature")
    with pytest.raises(publisher_lookup.UnknownPublisher):
        publisher_lookup.resolve_target("", publisher="no such press")
    with pytest.raises(ValueError, match="no house rules"):
        publisher_lookup.resolve_target("", publisher="wiley")
    # A title paperpush lists gets that venue, without asking Crossref.
    monkeypatch.setattr(publisher_lookup, "_get", lambda url: pytest.fail("no lookup expected"))
    assert publisher_lookup.resolve_target("", journal="Nature Methods").venue.slug == "nature_methods"


def test_cli_publishers_and_validate_flags(house_rules, capsys):
    from paperpush.cli import main

    assert main(["publishers"]) == 0
    out = capsys.readouterr().out
    assert "springer_nature" in out and "house rules" in out
    assert main(["validate", "--manuscript", "missing.pdf", "--venue", "nature", "--publisher", "springer_nature"]) == 1
    assert "is listed" in capsys.readouterr().err
    assert main(["validate", "--manuscript", "missing.pdf", "--publisher", "nobody"]) == 2


def test_web_picker_lists_house_rules(house_rules):
    pytest.importorskip("fastapi")
    from paperpush.web.app import venue_list

    slugs = [v["slug"] for v in venue_list()]
    assert "publisher:springer_nature" in slugs and "publisher:wiley" not in slugs


def test_house_rules_stored_where_older_versions_ignore_them(house_rules):
    # Older versions read every top-level key that is not "$..." as a venue.
    stored = {k: v for k, v in house_rules.items() if not k.startswith("publisher:")}
    assert "publisher:springer_nature" not in stored
    assert stored["$publishers"]["springer_nature"]["rules"] == HOUSE
    assert req_mod._expand_publishers(stored)["publisher:springer_nature"] == HOUSE


def test_publisher_rules_are_checked_when_published():
    assert "rule(s) this version does not know" in req_mod._publishers_problem({"elsevier": {"rules": {"abstract": {"max_wrods": 5}}}})
    assert "not another publisher" in req_mod._publishers_problem({"cell_press": {"rules": {"inherits": "publisher:elsevier"}}})
    ok = {"elsevier": {"rules": {"abstract": {"max_words": 5}}}, "cell_press": {"rules": {"inherits": "publisher:elsevier"}}}
    assert req_mod._publishers_problem(ok) is None
    assert req_mod._expand_publishers({"$publishers": ok})["publisher:cell_press"]["inherits"] == "publisher:elsevier"


def test_venue_may_not_inherit_publisher_rules():
    assert "venues.json" in req_mod._entry_problem("nature", {"inherits": "publisher:springer_nature"})
