"""Tests for the conference feeds (paperpush/conferences.py) and pick-venue's conference rows.

No feed is ever downloaded: ``conferences._download`` answers from the canned
feeds below (the suite-wide fixture in conftest.py makes it fail otherwise),
and OpenAlex is the fake from test_openalex.py.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date

import pytest

import paperpush.conferences as conferences
import paperpush.openalex as openalex
from paperpush.venue_select import (
    JournalHit,
    JournalSearch,
    ManuscriptQuery,
    VenueRow,
    archival_venue_types,
    combine_rows,
    limit_text,
    rows_from_conferences,
    rows_from_search,
    subject_fit,
)

TODAY = date(2026, 11, 12)

AGENT = {
    "generated": "2026-11-01",
    "conferences": [
        {
            "id": "recomb",
            "acronym": "RECOMB",
            "name": "Research in Computational Molecular Biology",
            "category": "biology, computer science",
            "subcategory": "genomics",
            "format": "abstract, paper, poster, oral",
            "size": "medium",
            "attendance": 400,
            "url": "https://recomb.org",
            "upcoming_abstract_deadline": "2099-10-28",
            "upcoming_paper_deadline": "2099-11-04",
            "paper_guidelines": "- Maximum 10 pages (Letter) including figures; bibliography does not count.",
        },
        {
            "id": "ashg",
            "acronym": "ASHG",
            "name": "American Society of Human Genetics Annual Meeting",
            "category": "biology",
            "subcategory": "genomics",
            "format": "abstract, poster, oral",
            "size": "massive",
            "attendance": 7500,
            "url": "https://www.ashg.org",
            "upcoming_abstract_deadline": "2099-06-01",
            "abstract_guidelines": "- Abstract body limited to 2,500 characters.",
        },
        {
            "id": "rsna",
            "acronym": "RSNA",
            "name": "Radiological Society of North America Annual Meeting",
            "category": "medicine",
            "subcategory": "radiology",
            "format": "abstract, poster, oral",
            "size": "massive",
            "url": "https://www.rsna.org",
            "prior_abstract_deadline": "2026-05-06",
        },
        {
            "id": "ats",
            "acronym": "ATS",
            "name": "American Thoracic Society International Conference",
            "category": "medicine",
            "subcategory": "pulmonology",
            "format": "abstract, poster, oral",
            "url": "https://conference.thoracic.org",
        },
    ],
}

CORE_CSV = (
    '1,"Research in Computational Molecular Biology",RECOMB,CORE2023,B,Yes,4601,,\n'
    '2,"Advances in Neural Information Processing Systems",NeurIPS,CORE2023,A*,Yes,4611,4602,\n'
    '3,"Association for Computational Linguistics",ACL,CORE2023,A*,Yes,4602,,\n'
    '4,"IEEE International Conference on Computer Vision",ICCV,CORE2023,A*,Yes,4603,,\n'
    '5,"Asian Test Symposium",ATS,CORE2023,C,Yes,4606,,\n'
    '6,"IEEE Conference on Computer Vision and Pattern Recognition",CVPR,CORE2023,A*,Yes,4603,,\n'
)

CCFDDL = [
    {
        "title": "NeurIPS",
        "description": "Conference on Neural Information Processing Systems",
        "sub": "AI",
        "rank": {"ccf": "A", "core": "A*"},
        "confs": [
            {"year": 2025, "link": "https://neurips.cc/2025", "timeline": [{"abstract_deadline": "2025-05-11 23:59:59", "deadline": "2025-05-15 23:59:59"}], "timezone": "AoE"},
            {"year": 2026, "link": "https://neurips.cc/2026", "timeline": [{"abstract_deadline": "2026-05-05 11:59:00", "deadline": "2026-05-07 11:59:59"}], "timezone": "UTC+0"},
        ],
    },
    {
        "title": "CVC",
        "description": "Computer Vision Conference",
        "sub": "AI",
        "rank": {"ccf": "N", "core": "N"},
        "confs": [{"year": 2027, "link": "https://cvc.example", "timeline": [{"deadline": "TBD"}]}],
    },
    {
        "title": "ICCV",
        "description": "IEEE International Conference on Computer Vision",
        "sub": "AI",
        "rank": {"ccf": "A", "core": "A*"},
        "confs": [{"year": 2027, "link": "https://iccv.thecvf.com", "timeline": [{"abstract_deadline": "2026-11-01 23:59:59", "deadline": "2027-03-07 23:59:59"}], "timezone": "UTC-12"}],
    },
]

CCFDDL_ACCEPT = [
    {
        "title": "NeurIPS",
        "accept_rates": [
            {"year": 2024, "submitted": 15671, "accepted": 4037, "rate": 0.2576},
            {"year": 2025, "submitted": 21575, "accepted": 5290, "rate": 0.2452},
        ],
    },
    {"title": "CVC", "accept_rates": []},
]

FEEDS = {
    conferences.CONFERENCE_AGENT_URL: json.dumps(AGENT).encode(),
    conferences.CORE_URL: CORE_CSV.encode(),
    conferences.CCFDDL_URL: json.dumps(CCFDDL).encode(),
    conferences.CCFDDL_ACCEPT_URL: json.dumps(CCFDDL_ACCEPT).encode(),
}


@pytest.fixture
def feeds(monkeypatch):
    """Serve the canned feeds; returns the list of URLs downloaded."""
    downloaded = []

    def download(url):
        downloaded.append(url)
        return FEEDS[url]

    monkeypatch.setattr(conferences, "_download", download)
    conferences.clear_cache()
    return downloaded


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------


def test_tokens_drop_noise_years_and_parentheses():
    assert conferences.tokens("Proceedings of the 38th AAAI Conference on Artificial Intelligence (AAAI 2024)") == {"aaai", "artificial", "intelligence"}
    # "A/Proceedings - A" names one venue twice; "IEEE/CVF ..." is one name.
    assert conferences.tokens("Computer Vision and Pattern Recognition/Proceedings - Computer Vision and Pattern Recognition") == {"computer", "vision", "pattern", "recognition"}
    assert conferences.tokens("IEEE/CVF Conference on Computer Vision and Pattern Recognition") == {"computer", "vision", "pattern", "recognition"}


def test_acronym_of_reads_a_parenthesized_acronym():
    assert conferences.acronym_of("Conference on Empirical Methods in Natural Language Processing (EMNLP)") == "EMNLP"
    assert conferences.acronym_of("Some Conference (held online)") == ""


def test_name_match_rules():
    t = conferences.tokens
    assert conferences.name_match(t("neural information processing systems"), t("Advances in Neural Information Processing Systems")) == 100
    assert conferences.name_match(t("Association for Computational Linguistics"), t("Annual Meeting of the Association for Computational Linguistics")) == 100
    # A satellite venue never matches its main conference.
    assert conferences.name_match(t("Findings of the Association for Computational Linguistics"), t("Association for Computational Linguistics")) == 0
    assert conferences.name_match(t("Workshop on Machine Learning in Computational Biology"), t("Machine Learning in Computational Biology")) == 0
    # Two words in common are not enough.
    assert conferences.name_match(t("Computer Vision Conference"), t("Computer Vision and Pattern Recognition")) == 0


def test_find_prefers_the_acronym_and_closer_name(feeds):
    cat = conferences.load_catalog(today=TODAY)
    assert cat.find("IEEE International Conference on Computer Vision (ICCV)").acronym == "ICCV"
    assert cat.find("International Conference on Computer Vision").acronym == "ICCV"
    assert cat.find("ICCV").core_rank == "A*"


def test_an_acronym_alone_does_not_join_unrelated_conferences(feeds):
    cat = conferences.load_catalog(today=TODAY)
    ats = next(c for c in cat.agent_conferences() if c.acronym == "ATS")
    assert ats.sources == ("conference-agent",) and ats.core_rank == ""


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------


def test_next_cycle_keeps_upcoming_dates_and_drops_a_passed_abstract():
    assert conferences.next_cycle(date(2026, 12, 1), date(2026, 12, 8), TODAY) == (date(2026, 12, 1), date(2026, 12, 8), False)
    assert conferences.next_cycle(date(2026, 11, 1), date(2026, 12, 8), TODAY) == (None, date(2026, 12, 8), False)


def test_next_cycle_moves_a_passed_cycle_forward_together():
    assert conferences.next_cycle(date(2026, 5, 5), date(2026, 5, 7), TODAY) == (date(2027, 5, 5), date(2027, 5, 7), True)
    assert conferences.next_cycle(None, None, TODAY) == (None, None, False)


def test_ccfddl_deadline_is_the_next_one_or_an_estimate(feeds):
    cat = conferences.load_catalog(today=TODAY)
    neurips = cat.find("neural information processing systems")
    assert (neurips.abstract_deadline, neurips.paper_deadline, neurips.deadline_estimated) == ("2027-05-05", "2027-05-07", True)
    assert neurips.deadlines_text() == "~abstract 2027-05-05, paper 2027-05-07 (est.)"
    iccv = cat.find("ICCV")
    assert (iccv.abstract_deadline, iccv.paper_deadline, iccv.deadline_estimated) == ("", "2027-03-07", False)


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------


def test_conference_agent_entries_carry_format_archival_and_limits(feeds):
    cat = conferences.load_catalog(today=TODAY)
    by = {c.acronym: c for c in cat.agent_conferences()}
    recomb, ashg, rsna = by["RECOMB"], by["ASHG"], by["RSNA"]
    assert (recomb.format, recomb.archival, recomb.limit, recomb.core_rank) == ("abstract + paper", "yes", "10 pages", "B")
    assert recomb.deadlines_text() == "abstract 2099-10-28, paper 2099-11-04"
    assert (ashg.format, ashg.archival, ashg.limit, ashg.size) == ("abstract", "no", "2,500 characters (abstract)", "massive")
    assert ashg.deadlines_text() == "abstract 2099-06-01"
    assert rsna.deadlines_text() == "~abstract 2027-05-06 (est.)"


def test_core_export_wins_over_ccfddls_copy_of_the_rank(feeds, monkeypatch):
    stale = [dict(CCFDDL[0], rank={"ccf": "A", "core": "B"})]
    monkeypatch.setitem(FEEDS, conferences.CCFDDL_URL, json.dumps(stale).encode())
    neurips = conferences.load_catalog(today=TODAY).find("Conference on Neural Information Processing Systems")
    assert (neurips.core_rank, neurips.ccf_rank) == ("A*", "A")
    assert neurips.sources == ("CORE", "ccfddl")


def test_acceptance_rate_is_the_latest_years(feeds):
    cat = conferences.load_catalog(today=TODAY)
    assert cat.find("NeurIPS").acceptance_rate == "24.5% (2025)"
    assert cat.find("Computer Vision Conference", acronym="CVC").acceptance_rate == ""
    assert cat.find("NeurIPS").to_dict()["acceptance_rate"] == "24.5% (2025)"


def test_feeds_are_cached_for_a_day(feeds):
    conferences.load_catalog(today=TODAY)
    assert len(feeds) == 4
    conferences.clear_cache()
    conferences.load_catalog(today=TODAY)
    assert len(feeds) == 4  # read from the cache
    for path in conferences.cache_dir().iterdir():
        old = time.time() - 2 * conferences.MAX_AGE_SECONDS
        os.utime(path, (old, old))
    conferences.clear_cache()
    conferences.load_catalog(today=TODAY)
    assert len(feeds) == 8


def test_a_stale_cache_stands_in_when_the_download_fails(feeds, monkeypatch):
    conferences.load_catalog(today=TODAY)
    for path in conferences.cache_dir().iterdir():
        old = time.time() - 2 * conferences.MAX_AGE_SECONDS
        os.utime(path, (old, old))

    def offline(url):
        raise OSError("no network")

    monkeypatch.setattr(conferences, "_download", offline)
    conferences.clear_cache()
    cat = conferences.load_catalog(today=TODAY)
    assert not cat.errors and len(cat.agent) == 4


def test_an_unreachable_feed_is_named_not_fatal():
    # conftest makes every download fail and the cache empty.
    cat = conferences.load_catalog(today=TODAY)
    assert set(cat.errors) == {"conference-agent", "core", "ccfddl", "ccfddl-acceptance"}
    assert cat.find("NeurIPS") is None


def test_offline_mode_reads_only_the_cache(feeds, monkeypatch):
    monkeypatch.setenv("PAPERPUSH_OFFLINE", "1")
    cat = conferences.load_catalog(today=TODAY)
    assert feeds == [] and "no cached copy" in cat.errors["core"]


# ---------------------------------------------------------------------------
# pick-venue rows
# ---------------------------------------------------------------------------

QUERY = ManuscriptQuery(title="Pseudoalignment for single-cell RNA-seq", abstract="We quantify single-cell RNA-seq reads with k-mer pseudoalignment for genomics.")


def _hit(sid, name, stype="conference", score=50, works=2, **kw):
    return JournalHit(source_id=sid, name=name, source_type=stype, score=score, works=works, **kw)


def test_rows_from_search_enriches_and_merges_conferences(feeds):
    cat = conferences.load_catalog(today=TODAY)
    search = JournalSearch(
        [
            _hit("S1", "Genome Biology", "journal", 100, 3, h_index=392, mean_citedness_2yr=9.9, topics=("Single-cell RNA-seq", "Gene expression", "Third")),
            _hit("S2", "Research in Computational Molecular Biology", score=70, works=2, h_index=40),
            _hit("S3", "Proceedings of RECOMB (RECOMB)", score=20, works=1, h_index=55),
            _hit("S4", "neural information processing systems", score=10, works=1),
        ],
        "q",
        10,
        2018,
    )
    rows = rows_from_search(search, cat)
    assert [r.name for r in rows] == ["Genome Biology", "Research in Computational Molecular Biology (RECOMB)", "Advances in Neural Information Processing Systems (NeurIPS)"]
    journal, recomb, neurips = rows
    assert journal.keywords == ("Single-cell RNA-seq", "Gene expression") and journal.archival == "" and journal.deadlines == ""
    # Two OpenAlex sources for one conference become one row.
    assert recomb.similar_works == 3 and recomb.h_index == 55
    assert (recomb.core_rank, recomb.format, recomb.archival, recomb.limit, recomb.link) == ("B", "abstract + paper", "yes", "10 pages", "https://recomb.org")
    assert neurips.core_rank == "A*" and neurips.ccf_rank == "A" and neurips.deadlines.startswith("~abstract")
    data = recomb.to_dict()
    assert data["archival"] is True and data["paper_deadline"] == "2099-11-04" and data["conference"]["sources"] == ["conference-agent", "CORE"]


def test_a_supported_conference_found_through_openalex_gets_its_slug(feeds):
    cat = conferences.load_catalog(today=TODAY)
    search = JournalSearch([_hit("S5", "IEEE Computer Society Conference on Computer Vision and Pattern Recognition/Proceedings - IEEE Computer Society Conference on Computer Vision and Pattern Recognition")], "q", 2, 2018)
    (row,) = rows_from_search(search, cat)
    assert row.slug == "cvpr_2027" and row.support and row.core_rank == "A*" and row.limit == limit_text("cvpr_2027")


def test_subject_fit_uses_subject_terms_and_stems():
    score, matched = subject_fit(QUERY, ["genomics", "radiology"])
    assert score > 0 and matched == ["genomics"]


def test_rows_from_conferences_scores_by_subject_and_skips_listed(feeds):
    cat = conferences.load_catalog(today=TODAY)
    rows = rows_from_conferences(QUERY, cat)
    assert [r.conference.acronym for r in rows] == ["ASHG", "RECOMB"] or [r.conference.acronym for r in rows] == ["RECOMB", "ASHG"]
    assert all(r.basis == "subject" and 0 < r.score <= 60 for r in rows)
    assert "RSNA" not in {r.conference.acronym for r in rows}
    assert [r.conference.acronym for r in rows_from_conferences(QUERY, cat, exclude={"recomb"})] == ["ASHG"]


def test_combine_rows_archival_modes(feeds):
    cat = conferences.load_catalog(today=TODAY)
    rows = rows_from_conferences(QUERY, cat)
    journal = VenueRow(name="Genome Biology", venue_type="journal", score=100, basis="similar works")
    acronyms = lambda kept: {r.conference.acronym for r in kept if r.conference}
    # Default: archival only.
    kept = combine_rows([journal], rows)
    assert [r.name for r in kept][0] == "Genome Biology" and acronyms(kept) == {"RECOMB"}
    # Non-archival only: no journals, no archival conferences.
    kept = combine_rows([journal], rows, archival="non-archival")
    assert [r.venue_type for r in kept] == ["conference"] and acronyms(kept) == {"ASHG"}
    # Either.
    kept = combine_rows([journal], rows, archival="all")
    assert "Genome Biology" in {r.name for r in kept} and acronyms(kept) == {"ASHG", "RECOMB"}
    assert [r.venue_type for r in combine_rows([journal], rows, venue_types=("conference",), archival="all")] == ["conference", "conference"]
    with pytest.raises(ValueError):
        combine_rows([journal], rows, archival="maybe")


def test_archival_venue_types():
    assert archival_venue_types("archival", ()) == ()
    assert archival_venue_types("all", ("journal",)) == ("journal",)
    assert archival_venue_types("non-archival", ()) == ("conference",)
    assert archival_venue_types("non-archival", ("conference", "journal")) == ("conference",)
    with pytest.raises(ValueError, match="journals and preprint servers are archival"):
        archival_venue_types("non-archival", ("journal",))


def test_limit_text_reads_the_venues_guidelines():
    assert limit_text("iclr_2027") == "9 pages"
    assert limit_text("nature") == "4,300 words"
    assert limit_text("no_such_venue") == ""


# ---------------------------------------------------------------------------
# CLI and MCP
# ---------------------------------------------------------------------------

TITLE = "Pseudoalignment for single-cell RNA-seq preprocessing"
ABSTRACT = "We preprocess single-cell RNA-seq reads with pseudoalignment and the BUS format for genomics, which is fast and memory efficient."


def _work(wid, title, year, source, abstract):
    return {"id": f"https://openalex.org/{wid}", "display_name": title, "publication_year": year, "primary_location": {"source": source}, "authorships": [], "abstract_inverted_index": {w: [i] for i, w in enumerate(abstract.split())}}


JOURNAL_SRC = {"id": "https://openalex.org/S1", "display_name": "Genome biology", "type": "journal"}
CONF_SRC = {"id": "https://openalex.org/S9", "display_name": "Research in Computational Molecular Biology", "type": "conference"}


class FakeOpenAlex:
    """Journal articles for the journal search, conference papers for the conference search."""

    def __init__(self):
        self.calls = []

    def __call__(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        flt = params.get("filter", "")
        if path == "/works" and "type:conference-paper" in flt:
            return {"results": [_work("W9", "Pseudoalignment of single-cell RNA-seq reads in linear time", 2024, CONF_SRC, "single-cell RNA-seq pseudoalignment fast memory efficient")]}
        if path == "/works":
            return {"results": [_work("W1", "Benchmarking single-cell RNA-seq preprocessing", 2023, JOURNAL_SRC, "single-cell RNA-seq preprocessing pseudoalignment benchmark")]}
        if path == "/sources":
            ids = flt.split("openalex_id:")[1].split("|")
            details = {"S1": {"id": "https://openalex.org/S1", "display_name": "Genome Biology", "type": "journal", "summary_stats": {"h_index": 392, "2yr_mean_citedness": 9.9}}, "S9": {"id": "https://openalex.org/S9", "display_name": "Research in Computational Molecular Biology", "type": "conference", "summary_stats": {"h_index": 40}}}
            return {"results": [details[i] for i in ids if i in details]}
        raise AssertionError(path)


@pytest.fixture
def fake_openalex(monkeypatch):
    fake = FakeOpenAlex()
    monkeypatch.setattr(openalex, "get_json", fake)
    return fake


def test_cli_conference_table(fake_openalex, feeds, capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--title", TITLE, "--abstract", ABSTRACT, "--venue-type", "conference", "--archival", "all"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Conferences publishing similar work")
    header = out.splitlines()[1]
    for column in ("Venue", "Fit", "CORE", "Next deadline", "Submit", "Limit", "Archival"):
        assert column in header
    assert "Research in Computational Molecular Biology (RECOMB)" in out and "10 pages" in out
    assert "American Society of Human Genetics" in out  # non-archival, asked for with "all"
    assert "Genome Biology" not in out
    # Only the conference-paper search ran.
    assert [p.get("filter", "").split(",")[0] for path, p in fake_openalex.calls if path == "/works"] == ["type:conference-paper"]


def test_cli_leaves_out_non_archival_meetings_by_default(fake_openalex, feeds, capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--json", "--title", TITLE, "--abstract", ABSTRACT]) == 0
    data = json.loads(capsys.readouterr().out)
    names = [v["name"] for v in data["venues"]]
    assert {"Genome Biology", "Research in Computational Molecular Biology (RECOMB)"} <= set(names)
    assert not any("Human Genetics" in n for n in names)
    recomb = next(v for v in data["venues"] if v["name"].startswith("Research"))
    assert recomb["core_rank"] == "B" and recomb["archival"] is True and recomb["format"] == "abstract + paper" and recomb["basis"] == "similar works"
    assert data["venue_type"] == "all" and data["archival"] == "archival" and data["conference_data"] == {"errors": {}}


def test_cli_non_archival_lists_only_non_archival_conferences(fake_openalex, feeds, capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--json", "--title", TITLE, "--abstract", ABSTRACT, "--non-archival"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["venues"] and all(v["venue_type"] == "conference" and v["archival"] is False for v in data["venues"])
    assert any("Human Genetics" in v["name"] for v in data["venues"]) and data["archival"] == "non-archival"
    # Journals are archival, so only the conference-paper search ran.
    assert [p.get("filter", "").split(",")[0] for path, p in fake_openalex.calls if path == "/works"] == ["type:conference-paper"]


def test_cli_non_archival_journals_is_an_error(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "genetics", "--venue-type", "journal", "--non-archival"]) == 2
    assert "journals and preprint servers are archival" in capsys.readouterr().err


def test_cli_archival_flags_are_exclusive(capsys):
    from paperpush.cli import main

    with pytest.raises(SystemExit):
        main(["pick-venue", "genetics", "--non-archival", "--archival", "all"])


def test_cli_journals_only_skips_the_conference_feeds(fake_openalex, feeds, capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--json", "--title", TITLE, "--abstract", ABSTRACT, "--venue-type", "journal"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {v["venue_type"] for v in data["venues"]} == {"journal"}
    assert feeds == [] and data["conference_data"] is None


def test_cli_rejects_an_unknown_column(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "genetics", "--columns", "venue,deadlines"]) == 2
    assert "did you mean 'deadline'" in capsys.readouterr().err


def test_cli_columns_choose_what_is_shown(fake_openalex, feeds, capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--title", TITLE, "--abstract", ABSTRACT, "--columns", "venue,core,archival"]) == 0
    header = capsys.readouterr().out.splitlines()[1].split()
    assert header == ["Venue", "CORE", "Archival"]


def test_mcp_pick_venue_conferences(fake_openalex, feeds):
    from paperpush import mcp_server

    result = mcp_server.pick_venue(title=TITLE, abstract=ABSTRACT, venue_type="conference", archival="all")
    assert {v["venue_type"] for v in result["venues"]} == {"conference"}
    by_name = {v["name"]: v for v in result["venues"]}
    ashg = by_name["American Society of Human Genetics Annual Meeting (ASHG)"]
    assert ashg["archival"] is False and ashg["basis"] == "subject" and ashg["format"] == "abstract"
    assert result["conference_data"] == {"errors": {}} and result["archival"] == "all"
    only = mcp_server.pick_venue(title=TITLE, abstract=ABSTRACT, archival="non-archival")
    assert only["venues"] and all(v["archival"] is False for v in only["venues"])
