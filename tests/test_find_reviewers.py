"""Tests for ``paperpush find-reviewers`` and :mod:`paperpush.reviewer_finder`.

OpenAlex is faked (see tests/test_openalex.py): a handful of similar works,
the manuscript's two authors, a recent co-author, a colleague at the same
institution, an opposed reviewer, and two good candidates.
"""

from __future__ import annotations

import json

import pytest

import paperpush.openalex as openalex
from paperpush.database import get_venue
from paperpush.reviewer_finder import Candidate, find_reviewers, gather_inputs, name_key, parse_author, reviewer_line, same_name, write_to_subfile

TITLE = "Pseudoalignment for single-cell RNA-seq preprocessing"
ABSTRACT = "We preprocess single-cell RNA-seq reads with pseudoalignment and the BUS format, which is fast and memory efficient."
ADA_ORCID = "0000-0001-0000-0001"


def _inst(iid, name):
    return {"id": f"https://openalex.org/{iid}", "display_name": name}


def _person(aid, name, orcid=""):
    return {"id": f"https://openalex.org/{aid}", "display_name": name, "orcid": f"https://orcid.org/{orcid}" if orcid else None}


def _authorship(aid, name, position="middle", inst=None, orcid=""):
    return {"author_position": position, "author": _person(aid, name, orcid), "institutions": [inst] if inst else []}


LONDON = _inst("I1", "University of London")
CAMBRIDGE = _inst("I2", "University of Cambridge")
MIT = _inst("I3", "Massachusetts Institute of Technology")
ETH = _inst("I4", "ETH Zurich")


def _work(wid, title, year, abstract, authorships):
    return {"id": f"https://openalex.org/{wid}", "doi": f"https://doi.org/10.1/{wid}", "display_name": title, "publication_year": year, "relevance_score": 10, "primary_location": {"source": {"id": "https://openalex.org/S1", "display_name": "J", "type": "journal"}}, "authorships": authorships, "abstract_inverted_index": {w: [i] for i, w in enumerate(abstract.split())}}


SIMILAR = [
    _work("W1", "Benchmarking single-cell RNA-seq preprocessing with pseudoalignment", 2025, "single-cell RNA-seq preprocessing pseudoalignment benchmark", [_authorship("A5", "Grace Hopper", "first", MIT), _authorship("A3", "Carl Coauthor", "middle", ETH), _authorship("A6", "Otto Other", "last", ETH)]),
    _work("W2", "The BUS format for single-cell RNA-seq", 2024, "BUS format single-cell RNA-seq pseudoalignment", [_authorship("A4", "Cora Colleague", "first", LONDON), _authorship("A7", "Olga Opposed", "last", ETH)]),
    _work("W3", "Pseudoalignment of single-cell RNA-seq reads", 2023, "pseudoalignment single-cell RNA-seq reads memory", [_authorship("A1", "Ada Lovelace", "first", LONDON, ADA_ORCID), _authorship("A5", "Grace Hopper", "last", MIT)]),
]
AUTHORS = {
    "A1": {"id": "https://openalex.org/A1", "display_name": "Ada Lovelace", "orcid": f"https://orcid.org/{ADA_ORCID}", "last_known_institutions": [LONDON], "works_count": 40},
    "A2": {"id": "https://openalex.org/A2", "display_name": "Charles Babbage", "orcid": None, "last_known_institutions": [CAMBRIDGE], "works_count": 90},
    "A3": {"id": "https://openalex.org/A3", "display_name": "Carl Coauthor", "last_known_institutions": [ETH], "works_count": 10},
    "A5": {"id": "https://openalex.org/A5", "display_name": "Grace Hopper", "orcid": "https://orcid.org/0000-0003-0000-0005", "last_known_institutions": [MIT], "works_count": 120, "summary_stats": {"h_index": 30}},
    "A6": {"id": "https://openalex.org/A6", "display_name": "Otto Other", "last_known_institutions": [ETH], "works_count": 15, "summary_stats": {"h_index": 5}},
}
# Babbage and Carl wrote a paper together last year: Carl is conflicted.
AUTHOR_WORKS = [{"id": "W9", "publication_year": 2025, "authorships": [_authorship("A2", "Charles Babbage"), _authorship("A3", "Carl Coauthor")]}]


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        flt = params.get("filter", "")
        if path == "/works" and "authorships.author.id:" in flt:
            ids = set(flt.split("authorships.author.id:")[1].split(",")[0].split("|"))
            return {"results": [w for w in AUTHOR_WORKS if ids & {openalex.short_id(a["author"]["id"]) for a in w["authorships"]}]}
        if path == "/works":
            return {"results": SIMILAR}
        if path == "/authors" and flt.startswith("openalex_id:"):
            return {"results": [AUTHORS[i] for i in flt.split(":", 1)[1].split("|") if i in AUTHORS]}
        if path == "/authors" and flt.startswith("orcid:"):
            wanted = set(flt.split(":", 1)[1].split("|"))
            return {"results": [a for a in AUTHORS.values() if openalex.bare_orcid(a.get("orcid") or "") in wanted]}
        if path == "/authors":
            surname = openalex.fold(params["search"]).split()[-1]
            return {"results": [a for a in AUTHORS.values() if openalex.fold(a["display_name"]).split()[-1] == surname]}
        raise AssertionError(f"unexpected call {path} {params}")


@pytest.fixture
def fake(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(openalex, "get_json", fake)
    return fake


AUTHORS_GIVEN = [parse_author(f"Ada Lovelace | University of London {ADA_ORCID}"), parse_author("Charles Babbage | University of Cambridge")]


def test_parse_author_picks_out_affiliation_and_orcid():
    a = parse_author(f"Ada Lovelace | University of London | {ADA_ORCID}")
    assert (a.name, a.affiliation, a.orcid) == ("Ada Lovelace", "University of London", ADA_ORCID)
    assert parse_author("Grace Hopper") == parse_author("Grace Hopper |")


def test_names_match_on_surname_and_first_initial():
    assert name_key("Páll Melsted") == ("melsted", "p")
    assert same_name("Lior S. Pachter", "Lior Pachter")
    assert not same_name("Lior Pachter", "Ann Pachter")


def test_conflicts_are_screened_out(fake):
    found = find_reviewers(TITLE, ABSTRACT, authors=AUTHORS_GIVEN, exclude=["Olga Opposed"], this_year=2026)
    assert [c.name for c in found.candidates] == ["Grace Hopper", "Otto Other"]
    reasons = {e.name: e.reason for e in found.excluded}
    assert reasons["Ada Lovelace"] == "a manuscript author"
    assert reasons["Carl Coauthor"] == "co-authored with Charles Babbage (2025)"
    assert reasons["Cora Colleague"].startswith("shares an institution with Ada Lovelace")
    assert reasons["Olga Opposed"] == "excluded (Olga Opposed)"
    assert found.authors_found == ["Ada Lovelace", "Charles Babbage"] and not found.authors_missing


def test_candidates_carry_details_and_reasons(fake):
    grace = find_reviewers(TITLE, ABSTRACT, authors=AUTHORS_GIVEN, this_year=2026).candidates[0]
    assert (grace.institution, grace.orcid, grace.openalex_id, grace.h_index) == ("Massachusetts Institute of Technology", "0000-0003-0000-0005", "A5", 30)
    # Her similar works, the closest match first.
    assert sorted(t for t, _, _ in grace.papers()) == ["Benchmarking single-cell RNA-seq preprocessing with pseudoalignment", "Pseudoalignment of single-cell RNA-seq reads"]
    weights = [w for w, *_ in sorted(grace.works, key=lambda w: -w[0])]
    assert weights == sorted(weights, reverse=True)


def test_without_authors_nothing_is_screened_and_a_note_says_so(fake):
    found = find_reviewers(TITLE, ABSTRACT, this_year=2026)
    assert "Ada Lovelace" in [c.name for c in found.candidates]
    assert any("No author list" in n for n in found.notes)
    assert any("e-mail" in n for n in found.notes)


def test_limit_is_honored(fake):
    assert len(find_reviewers(TITLE, ABSTRACT, limit=1, this_year=2026).candidates) == 1


def test_orcid_lookup_is_batched_and_names_searched_individually(fake):
    find_reviewers(TITLE, ABSTRACT, authors=AUTHORS_GIVEN, this_year=2026)
    author_calls = [p for path, p in fake.calls if path == "/authors"]
    assert any(p.get("filter", "").startswith("orcid:") for p in author_calls)
    assert [p["search"] for p in author_calls if "search" in p] == ["Charles Babbage"]


def test_reviewer_line_follows_the_fields_columns():
    c = Candidate("A5", "Grace Hopper", institution="MIT", works=[(1.0, "A paper", 2025, "")])
    assert reviewer_line(c, ["name", "email?", "institution?", "stance?", "reason?"]) == "Grace Hopper |  | MIT | suggested | Published similar work: A paper (2025)"
    assert reviewer_line(c, ["first_name", "last_name", "email", "institution?", "department?"]) == "Grace | Hopper |  | MIT"
    assert reviewer_line(c, ["name", "email", "stance", "reason?"], "stance must be 'recommended' or 'opposed'").split(" | ")[2] == "recommended"


def test_write_to_subfile_appends_and_skips_listed_names():
    venue = get_venue("cell")
    text = "@venue: cell\ntitle: T\nreviewers: |\n  Grace Hopper | g@x.org\n  Olga Opposed | o@x.org | | opposed\n"
    new, added = write_to_subfile(text, venue, [Candidate("A5", "Grace Hopper"), Candidate("A6", "Otto Other", institution="ETH Zurich")])
    assert added == 1
    assert new.endswith("reviewers: |\n  Grace Hopper | g@x.org\n  Olga Opposed | o@x.org | | opposed\n  Otto Other |  | ETH Zurich | suggested\n")


def test_write_to_subfile_turns_an_empty_value_into_a_block():
    new, added = write_to_subfile("@venue: cell\nreviewers:\ntitle: T\n", get_venue("cell"), [Candidate("A6", "Otto Other")])
    assert added == 1 and new == "@venue: cell\nreviewers: |\n  Otto Other |  |  | suggested\ntitle: T\n"


def test_write_to_subfile_needs_a_reviewer_field():
    with pytest.raises(ValueError, match="no suggested-reviewer field"):
        write_to_subfile("@venue: biorxiv\n", get_venue("biorxiv"), [Candidate("A6", "Otto Other")])


def _cell_sub(tmp_path):
    path = tmp_path / "cell.sub.txt"
    path.write_text(
        "@venue: cell\n"
        f"title: {TITLE}\n"
        "abstract: |\n"
        f"  {ABSTRACT}\n"
        "keywords: single-cell RNA-seq; pseudoalignment\n"
        "authors: |\n"
        "  Ada Lovelace | ada@london.ac.uk | University of London | UK | yes\n"
        "  Charles Babbage | cb@cam.ac.uk | University of Cambridge | UK | no\n"
        "reviewers: |\n"
        "  Olga Opposed | o@x.org | | opposed\n",
        encoding="utf-8",
    )
    return path


def test_gather_inputs_reads_a_sub(tmp_path):
    got = gather_inputs(_cell_sub(tmp_path), authors=["Extra Person"], exclude=["Someone Else"])
    assert got.title == TITLE and got.abstract == ABSTRACT
    assert got.keywords == ["single-cell RNA-seq", "pseudoalignment"]
    assert [a.name for a in got.authors] == ["Extra Person", "Ada Lovelace", "Charles Babbage"]
    assert got.authors[1].affiliation == "University of London"
    assert got.exclude == ["Someone Else", "Olga Opposed"]
    assert got.venue.slug == "cell"


def test_cli_find_reviewers_from_a_sub_and_write(fake, tmp_path, capsys):
    from paperpush.cli import main

    path = _cell_sub(tmp_path)
    assert main(["find-reviewers", str(path), "--write"]) == 0
    out = capsys.readouterr().out
    assert "1. Grace Hopper -- Massachusetts Institute of Technology; ORCID 0000-0003-0000-0005; OpenAlex A5; h-index 30" in out
    assert "co-authored with Charles Babbage (2025)" in out
    assert "OpenAlex has no e-mail addresses" in out
    assert "Added 2 reviewer(s)" in out
    assert "  Grace Hopper |  | Massachusetts Institute of Technology | suggested | Published similar work:" in path.read_text()


def test_cli_find_reviewers_json(fake, capsys):
    from paperpush.cli import main

    assert main(["find-reviewers", "--json", "--title", TITLE, "--abstract", ABSTRACT, "--author", f"Ada Lovelace | University of London {ADA_ORCID}", "-n", "1"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in data["reviewers"]] == ["Grace Hopper"]
    assert data["reviewers"][0]["email"] == "" and data["reviewers"][0]["score"] == 100
    assert {"name": "Ada Lovelace", "reason": "a manuscript author", "openalex_id": "A1"} in data["excluded"]


def test_cli_find_reviewers_usage_errors(capsys):
    from paperpush.cli import main

    assert main(["find-reviewers"]) == 2
    assert main(["find-reviewers", "--title", TITLE, "--write"]) == 2
    assert "--write needs a .sub" in capsys.readouterr().err


def test_cli_find_reviewers_when_openalex_is_unreachable(capsys):
    from paperpush.cli import main

    assert main(["find-reviewers", "--title", TITLE]) == 1
    assert "offline" in capsys.readouterr().err


def test_mcp_find_reviewers(fake, tmp_path):
    from paperpush import mcp_server

    path = _cell_sub(tmp_path)
    result = mcp_server.find_reviewers(subfile=str(path), limit=1, write=True)
    assert [r["name"] for r in result["reviewers"]] == ["Grace Hopper"]
    assert result["written"] == {"subfile": str(path), "added": 1}
    assert result["manuscript"]["authors"] == ["Ada Lovelace", "Charles Babbage"]
    with pytest.raises(ValueError):
        mcp_server.find_reviewers(title=TITLE, write=True)


def test_one_reviewer_per_group(fake, monkeypatch):
    import copy
    import sys

    module = sys.modules[__name__]
    works = copy.deepcopy(SIMILAR)
    works[0]["authorships"].insert(1, _authorship("A8", "Mia Labmate", "middle", MIT))
    monkeypatch.setattr(module, "SIMILAR", works)
    monkeypatch.setitem(AUTHORS, "A8", {"id": "https://openalex.org/A8", "display_name": "Mia Labmate", "last_known_institutions": [MIT], "summary_stats": {"h_index": 40}})
    found = find_reviewers(TITLE, ABSTRACT, authors=AUTHORS_GIVEN, this_year=2026)
    assert "Mia Labmate" not in [c.name for c in found.candidates]
    assert {e.name: e.reason for e in found.excluded}["Mia Labmate"] == "same group as Grace Hopper, who is suggested"
