"""Tests for the OpenAlex client and ``pick-venue``'s any-journal search.

OpenAlex is never contacted: ``openalex.get_json`` is replaced by a fake that
answers from canned records (the suite-wide fixture in conftest.py makes it
"unreachable" otherwise).
"""

from __future__ import annotations

import json

import pytest

import paperpush.openalex as openalex
from paperpush.database import Venue
from paperpush.venue_select import ManuscriptQuery, find_journals, format_journals

TITLE = "Pseudoalignment for single-cell RNA-seq preprocessing"
ABSTRACT = "We preprocess single-cell RNA-seq reads with pseudoalignment and the BUS format, which is fast and memory efficient."


def _work(wid, title, year, source, abstract="", authors=(), relevance=10.0):
    return {
        "id": f"https://openalex.org/{wid}",
        "doi": f"https://doi.org/10.1/{wid}",
        "display_name": title,
        "publication_year": year,
        "relevance_score": relevance,
        "primary_location": {"source": source},
        "authorships": list(authors),
        "abstract_inverted_index": {w: [i] for i, w in enumerate(abstract.split())},
    }


def _source(sid, name, stype="journal"):
    return {"id": f"https://openalex.org/{sid}", "display_name": name, "type": stype}


GENOME_BIO = _source("S1", "Genome biology")
BIOINF = _source("S2", "Bioinformatics")
NUMBER_THEORY = _source("S3", "Communications in Number Theory")
BIORXIV = _source("S4", "bioRxiv (Cold Spring Harbor Laboratory)", "repository")
PMC = _source("S5", "Europe PMC (PubMed Central)", "repository")

WORKS = [
    _work("W0", TITLE, 2024, BIORXIV, ABSTRACT),  # the manuscript itself
    _work("W1", "Benchmarking single-cell RNA-seq preprocessing workflows", 2023, GENOME_BIO, "single-cell RNA-seq preprocessing pseudoalignment benchmark"),
    _work("W2", "Pseudoalignment of single-cell RNA-seq with the BUS format", 2021, BIOINF, "BUS format pseudoalignment single-cell RNA-seq memory efficient"),
    _work("W3", "Single-cell RNA-seq preprocessing at scale", 2025, GENOME_BIO, "single-cell RNA-seq preprocessing fast"),
    _work("W4", "Modular forms and quantum geometry", 2024, NUMBER_THEORY, "modular forms quantum geometry"),
    _work("W5", "The BUS format for single-cell RNA-seq", 2020, PMC, "BUS format single-cell RNA-seq"),
    _work("W6", "Fast pseudoalignment of bulk reads", 2024, BIORXIV, "pseudoalignment of bulk reads"),
]

SOURCE_DETAILS = {
    "S1": {"id": "https://openalex.org/S1", "display_name": "Genome Biology", "issn_l": "1474-760X", "host_organization_name": "BioMed Central", "is_oa": True, "is_in_doaj": True, "apc_usd": 5490, "summary_stats": {"h_index": 392}, "type": "journal", "homepage_url": "https://genomebiology.biomedcentral.com"},
    "S2": {"id": "https://openalex.org/S2", "display_name": "Bioinformatics", "issn_l": "1367-4803", "host_organization_name": "Oxford University Press", "is_oa": True, "is_in_doaj": True, "apc_usd": 3989, "summary_stats": {"h_index": 563}, "type": "journal"},
    "S4": {"id": "https://openalex.org/S4", "display_name": "bioRxiv (Cold Spring Harbor Laboratory)", "host_organization_name": "Cold Spring Harbor Laboratory", "type": "repository", "summary_stats": {"h_index": 136}},
}


class FakeOpenAlex:
    """Answers ``get_json`` from canned works, sources, and authors, recording each call."""

    def __init__(self, works=WORKS, sources=SOURCE_DETAILS, authors=None, author_works=None):
        self.works, self.sources, self.authors, self.author_works = works, sources, authors or {}, author_works or []
        self.calls = []

    def __call__(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        flt = params.get("filter", "")
        if path == "/works" and "authorships.author.id:" in flt:
            ids = set(flt.split("authorships.author.id:")[1].split(",")[0].split("|"))
            return {"results": [w for w in self.author_works if ids & {openalex.short_id(a["author"]["id"]) for a in w["authorships"]}]}
        if path == "/works":
            return {"results": list(self.works)}
        if path == "/sources":
            ids = flt.split("openalex_id:")[1].split("|")
            return {"results": [self.sources[i] for i in ids if i in self.sources]}
        if path == "/authors" and flt.startswith("openalex_id:"):
            ids = flt.split("openalex_id:")[1].split("|")
            return {"results": [self.authors[i] for i in ids if i in self.authors]}
        if path == "/authors" and flt.startswith("orcid:"):
            orcids = set(flt.split("orcid:")[1].split("|"))
            return {"results": [a for a in self.authors.values() if openalex.bare_orcid(a.get("orcid") or "") in orcids]}
        if path == "/authors":
            name = params.get("search", "")
            return {"results": [a for a in self.authors.values() if openalex.fold(a["display_name"]).split()[-1] == openalex.fold(name).split()[-1]]}
        raise AssertionError(f"unexpected OpenAlex call {path} {params}")


@pytest.fixture
def fake(monkeypatch):
    fake = FakeOpenAlex()
    monkeypatch.setattr(openalex, "get_json", fake)
    return fake


# ---------------------------------------------------------------------------
# The client's pure helpers
# ---------------------------------------------------------------------------


def test_search_terms_or_together_keywords_title_and_frequent_abstract_words():
    q = openalex.search_terms("A method for single-cell RNA-seq preprocessing", "Pseudoalignment pseudoalignment of reads. We use reads.", ["BUS format"])
    terms = q.split(" OR ")
    assert terms[0] == '"BUS format"'
    assert '"single-cell"' in terms and '"RNA-seq"' in terms and "preprocessing" in terms
    assert "pseudoalignment" in terms
    # Function words and filler never become terms.
    assert not {"A", "method", "for", "we", "use"} & set(terms)


def test_search_terms_is_capped():
    assert len(openalex.search_terms(" ".join(f"word{i}" for i in range(50))).split(" OR ")) == openalex.MAX_TERMS


def test_work_abstract_rebuilds_the_inverted_index():
    assert openalex.work_abstract({"abstract_inverted_index": {"world": [1], "hello": [0, 2]}}) == "hello world hello"
    assert openalex.work_abstract({}) == ""


def test_rerank_drops_the_manuscript_itself_and_unrelated_work():
    ranked = openalex.rerank(WORKS, TITLE, ABSTRACT)
    titles = [w["display_name"] for w in ranked]
    assert TITLE not in titles
    assert "Modular forms and quantum geometry" not in titles
    assert ranked[0]["similarity"] == 1.0 and all(0 < w["similarity"] <= 1 for w in ranked)


def test_ids_and_orcids():
    assert openalex.short_id("https://openalex.org/S123") == "S123"
    assert openalex.short_id("A5") == "A5"
    assert openalex.bare_orcid("https://orcid.org/0000-0002-1825-009X") == "0000-0002-1825-009X"
    assert openalex.bare_orcid("none") == ""


def _fresh_client():
    """A separate copy of the client module, whose get_json the conftest fixture has not replaced."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_openalex_under_test", openalex.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_get_json_adds_the_api_key_and_a_generic_user_agent(monkeypatch):
    real = _fresh_client()
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"results": []}'

    def urlopen(req, timeout):
        seen["url"], seen["agent"] = req.full_url, req.get_header("User-agent")
        return Resp()

    monkeypatch.setenv("OPENALEX_API_KEY", "k123")
    monkeypatch.setattr(real.urllib.request, "urlopen", urlopen)
    assert real.get_json("/works", {"search": "x y", "filter": "type:article|preprint"}) == {"results": []}
    assert "api_key=k123" in seen["url"] and "type:article|preprint" in seen["url"]
    assert "@" not in seen["agent"]


def test_get_json_raises_when_unreachable(monkeypatch):
    import urllib.error

    real = _fresh_client()
    monkeypatch.setattr(real, "_RETRY_DELAY", 0)

    def urlopen(req, timeout):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(real.urllib.request, "urlopen", urlopen)
    with pytest.raises(real.OpenAlexError, match="could not reach OpenAlex"):
        real.get_json("/works")


# ---------------------------------------------------------------------------
# find_journals
# ---------------------------------------------------------------------------

_SUPPORTED = [Venue(slug="genome_biology", name="Genome Biology", venue_type="journal", full_name="Genome Biology"), Venue(slug="biorxiv", name="bioRxiv", venue_type="preprint", full_name="bioRxiv - The Preprint Server for Biology")]


def test_find_journals_groups_similar_work_by_source(fake):
    found = find_journals(ManuscriptQuery(title=TITLE, abstract=ABSTRACT), venues=_SUPPORTED, this_year=2026)
    names = [h.name for h in found.hits]
    assert names[0] == "Genome Biology"  # two recent, close matches
    assert set(names) == {"Genome Biology", "Bioinformatics", "bioRxiv (Cold Spring Harbor Laboratory)"}
    top = found.hits[0]
    assert top.score == 100 and top.works == 2 and top.apc_usd == 5490 and top.h_index == 392 and top.publisher == "BioMed Central"
    # Supported venues are matched by name, across "the"/case/parentheticals.
    assert top.slug == "genome_biology"
    assert {h.name: h.slug for h in found.hits}["bioRxiv (Cold Spring Harbor Laboratory)"] == "biorxiv"
    assert {h.name: h.slug for h in found.hits}["Bioinformatics"] == ""
    assert found.since_year == 2026 - 8
    # One batched sources call after the works search.
    assert [c[0] for c in fake.calls] == ["/works", "/sources"]


def test_find_journals_type_filter_keeps_only_preprint_servers(fake):
    found = find_journals(ManuscriptQuery(title=TITLE, abstract=ABSTRACT), venue_types=("preprint",), venues=_SUPPORTED, this_year=2026)
    # Europe PMC is a repository but not a preprint server.
    assert [h.name for h in found.hits] == ["bioRxiv (Cold Spring Harbor Laboratory)"]


def test_format_journals_marks_supported_venues(fake):
    lines = format_journals(find_journals(ManuscriptQuery(title=TITLE, abstract=ABSTRACT), venues=_SUPPORTED, this_year=2026).hits)
    assert lines[0].startswith("1. Genome Biology -- BioMed Central; 2 similar works; h-index 392; open access (DOAJ); APC $5,490")
    assert "[paperpush: genome_biology, " in lines[0]
    assert lines[1].strip().startswith("e.g. ")


def test_find_journals_with_nothing_to_search_raises():
    with pytest.raises(ValueError):
        find_journals(ManuscriptQuery(title="the of and"))


# ---------------------------------------------------------------------------
# pick-venue CLI and MCP tool
# ---------------------------------------------------------------------------


def test_cli_pick_venue_lists_any_journal_by_default(fake, capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--title", TITLE, "--abstract", ABSTRACT]) == 0
    out = capsys.readouterr().out
    assert "Journals publishing similar work (OpenAlex:" in out
    assert "Bioinformatics -- Oxford University Press" in out
    assert "Use 'paperpush subfile genome_biology'" in out


def test_cli_pick_venue_supported_only_skips_openalex(fake, capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--paperpush-supported-only", "--title", TITLE, "--abstract", ABSTRACT]) == 0
    out = capsys.readouterr().out
    assert fake.calls == []
    assert "Journals publishing similar work" not in out
    assert "Subjects inferred:" in out


def test_cli_pick_venue_venue_name_search_stays_offline(fake, capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--venue-name", "nature methods", "-n", "1"]) == 0
    assert fake.calls == []


def test_cli_pick_venue_json_has_both_rankings(fake, capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--json", "-n", "2", "--title", TITLE, "--abstract", ABSTRACT]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["openalex"]["source"] == "OpenAlex"
    assert len(data["openalex"]["journals"]) == 2 and data["openalex"]["total_journals"] == 3
    assert data["openalex"]["journals"][0]["paperpush_slug"] == "genome_biology"
    assert "matches" in data  # the supported-venue ranking is still there


def test_cli_pick_venue_falls_back_when_openalex_is_unreachable(capsys):
    from paperpush.cli import main

    # conftest makes OpenAlex unreachable.
    assert main(["pick-venue", "--title", TITLE, "--abstract", ABSTRACT]) == 0
    captured = capsys.readouterr()
    assert "ranking only the venues paperpush supports" in captured.err
    assert "Subjects inferred:" in captured.out


def test_cli_pick_venue_json_reports_the_openalex_error(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--json", "--title", TITLE, "--abstract", ABSTRACT]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "offline" in data["openalex"]["error"]
    assert data["matches"]


def test_mcp_pick_venue_any_journal_and_supported_only(fake):
    from paperpush import mcp_server

    result = mcp_server.pick_venue(title=TITLE, abstract=ABSTRACT, limit=1)
    assert [j["name"] for j in result["openalex"]["journals"]] == ["Genome Biology"]
    fake.calls.clear()
    result = mcp_server.pick_venue(title=TITLE, abstract=ABSTRACT, paperpush_supported_only=True)
    assert result["openalex"] is None and fake.calls == []
