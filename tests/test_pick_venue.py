"""Tests for ``paperpush pick-venue`` and :mod:`paperpush.venue_select`.

Ranking and filtering are pure functions over venue metadata, so most tests use
hand-built :class:`~paperpush.database.Venue` objects through the ``venues=``
parameter of :func:`~paperpush.venue_select.pick_venues` -- deterministic no
matter how ``venues.json`` evolves. A smaller set of tests runs against the
shipped database, the ``pick-venue`` CLI end to end via
:func:`paperpush.cli.main`, and the MCP tool.
"""

from __future__ import annotations

import json

import pytest

from paperpush.database import Venue
from paperpush.venue_select import (
    ManuscriptQuery,
    extract_manuscript,
    format_hits,
    infer_subjects,
    pick_venues,
    portal_of,
    query_from_text,
    rank_via_api,
    select,
    subjects_named,
)


def _venue(slug, name="", description="", venue_type="journal", full_name="", keywords=()):
    return Venue(slug=slug, name=name or slug, description=description, venue_type=venue_type, full_name=full_name, keywords=list(keywords))


# ---------------------------------------------------------------------------
# Venue-name search (--venue-name), on a fixed corpus
# ---------------------------------------------------------------------------

_FAKE_VENUES = [
    _venue("prex", "Preprint X", "an open archive for preprints", "preprint", "Preprint X Full"),
    _venue("jx", "Journal X", "a journal about widgets", "journal"),
    _venue("jx_methods", "Journal X Methods", "methods for widgets", "journal"),
    _venue("cx27", "Conf X 2027", "conference on widgets", "conference"),
]


def test_exact_slug_ranks_first():
    hits = pick_venues("jx", venues=_FAKE_VENUES)
    assert hits[0].venue.slug == "jx"
    assert "slug" in hits[0].reasons
    # A slug the search only begins still matches, lower.
    assert hits[1].venue.slug == "jx_methods"
    assert hits[1].reasons == ["full-name-prefix", "slug-prefix"]


def test_multi_word_name_matches_whole():
    hits = pick_venues("journal x methods", venues=_FAKE_VENUES)
    assert hits[0].venue.slug == "jx_methods"
    assert hits[0].reasons[0] == "full-name"


def test_word_inside_a_name_matches():
    hits = pick_venues("methods", venues=_FAKE_VENUES)
    assert [hit.venue.slug for hit in hits] == ["jx_methods"]
    assert hits[0].reasons == ["name-word"]


def test_name_search_ignores_descriptions_types_and_function_words():
    assert pick_venues("widgets", venues=_FAKE_VENUES) == []
    assert pick_venues("conference", venues=_FAKE_VENUES) == []
    assert pick_venues("the of", venues=_FAKE_VENUES) == []


def test_name_search_is_case_insensitive():
    assert pick_venues("JX", venues=_FAKE_VENUES)[0].venue.slug == "jx"


def test_ties_break_by_slug():
    hits = pick_venues("x", venues=[_venue("bx", "Q X"), _venue("ax", "R X")])
    assert [hit.venue.slug for hit in hits] == ["ax", "bx"]
    assert hits[0].score == hits[1].score


def test_nothing_matching_returns_empty():
    assert pick_venues("flurb-no-such-term", venues=_FAKE_VENUES) == []


def test_no_input_keeps_everything_sorted_by_slug():
    hits = pick_venues("", venues=_FAKE_VENUES)
    assert [hit.venue.slug for hit in hits] == ["cx27", "jx", "jx_methods", "prex"]
    assert all(hit.score == 0 for hit in hits)


def test_type_filter_narrows_before_scoring():
    hits = pick_venues("", venue_types=("preprint",), venues=_FAKE_VENUES)
    assert [hit.venue.slug for hit in hits] == ["prex"]
    assert pick_venues("jx", venue_types=("preprint",), venues=_FAKE_VENUES) == []


def test_format_hits_pads_slugs_and_truncates_descriptions():
    long = "x" * 200
    hits = pick_venues("", venues=[_venue("ab", "A B", long), _venue("cdef", "C D", "short")])
    lines = format_hits(hits)
    assert len(lines) == 2
    assert lines[0].startswith("ab    A B (journal) -- ")
    assert lines[0].endswith("…")
    assert lines[1].startswith("cdef  C D (journal) -- short")


def test_portal_of_resolves_family_members():
    assert portal_of("biorxiv") == "openrxiv"
    assert portal_of("cell") == "editorialmanager"
    # AAAS siblings submit through the shared science runner.
    assert portal_of("science_advances") == "science"
    assert portal_of("no-such-venue") == ""


def test_live_name_search():
    assert pick_venues("nature_methods")[0].venue.slug == "nature_methods"
    assert pick_venues("nature methods")[0].venue.slug == "nature_methods"
    assert pick_venues("nature neuro")[0].venue.slug == "nature_neuroscience"


def test_live_portal_filter_matches():
    slugs = {hit.venue.slug for hit in pick_venues("", portals=("openrxiv",))}
    assert {"biorxiv", "medrxiv"} <= slugs
    assert "arxiv" not in slugs


# ---------------------------------------------------------------------------
# Subjects: venue keywords, keywords, titles, abstracts, manuscripts
# ---------------------------------------------------------------------------

_SUBJECT_VENUES = [
    _venue("immj", "Imm J", "immunology research", keywords=["immunology", "biology"]),
    _venue("bigj", "Big J", "everything in the life sciences", keywords=["biology", "genetics", "neuroscience", "immunology", "bioinformatics"]),
    _venue("genj", "General J", "multidisciplinary", keywords=["general", "biology", "physics"]),
    _venue("mlj", "ML J", "machine learning", keywords=["machine learning"]),
]

_TCELL_TITLE = "Single-cell RNA-seq reveals T cell exhaustion programs in tumor-infiltrating lymphocytes"
_TCELL_ABSTRACT = "We profiled CD8+ T cells from melanoma patients with single-cell RNA sequencing and identified transcriptional programs of exhaustion linked to response to checkpoint immunotherapy."


def test_subject_specialist_outranks_broad_venue():
    hits = pick_venues(subjects=["immunology"], venues=_SUBJECT_VENUES)
    # The specialist first, then the broad venue that lists the subject, then
    # the general venue (which covers any subject, at half weight).
    assert [hit.venue.slug for hit in hits] == ["immj", "bigj", "genj"]
    assert hits[0].reasons == ["keywords: immunology"]
    assert hits[2].reasons == ["keywords: general"]


def test_general_venue_covers_any_subject_at_reduced_weight():
    hits = pick_venues(subjects=["physics", "math"], venues=_SUBJECT_VENUES)
    assert [hit.venue.slug for hit in hits] == ["genj"]
    # A venue with only unrelated subjects is left out entirely.
    assert "mlj" not in {hit.venue.slug for hit in pick_venues(subjects=["immunology"], venues=_SUBJECT_VENUES)}


def test_subjects_named_in_keywords():
    assert subjects_named(["Machine   Learning for genomics", "immunology"]) == ["immunology", "machine learning"]
    assert subjects_named(["hippocampus"]) == []


def test_query_from_text_splits_title_keywords_and_abstract():
    q = query_from_text("A short title\nAbstract: Body of the abstract.\nKeywords: alpha; beta, gamma")
    assert q.title == "A short title"
    assert q.abstract == "Body of the abstract."
    assert q.keywords == ("alpha", "beta", "gamma")


def test_infer_subjects_from_an_abstract():
    subjects = infer_subjects(ManuscriptQuery(title=_TCELL_TITLE, abstract=_TCELL_ABSTRACT))
    assert subjects[0].subject == "immunology"
    assert subjects[0].weight == 1.0
    assert "bioinformatics" in [s.subject for s in subjects]
    # "T cell" and "single-cell" claim their words, so biology's "cell" is not counted on top.
    assert "cell" not in next((s.terms for s in subjects if s.subject == "biology"), ())


def test_infer_subjects_short_terms_need_whole_words():
    # "pet" (PET imaging) must not fire on "petri"; "cells" still counts as "cell".
    assert "radiology" not in [s.subject for s in infer_subjects("Bacteria grown in petri dishes")]
    assert "radiology" in [s.subject for s in infer_subjects("Tumours were staged with PET and CT")]
    assert "biology" in [s.subject for s in infer_subjects("We cultured stem cells and measured their cells")]


def test_infer_subjects_nothing_to_go_on():
    assert infer_subjects("Lorem ipsum dolor sit amet.") == []


def test_title_counts_more_than_abstract():
    # The same words weigh three times as much in the title as in the abstract.
    as_title = infer_subjects(ManuscriptQuery(title="hippocampal memory", abstract="a quantum lattice"))
    as_abstract = infer_subjects(ManuscriptQuery(title="a quantum lattice", abstract="hippocampal memory"))
    assert as_title[0].subject == "neuroscience"
    assert as_abstract[0].subject == "physics"


def test_topic_keyword_finds_venues():
    selection = select(keywords=["hippocampus"])
    assert selection.inferred[0].subject == "neuroscience"
    assert selection.hits[0].venue.slug in {"neuron", "nature_neuroscience"}


def test_vocabulary_keyword_matches_venues_directly():
    selection = select(keywords=["Immunology"])
    assert selection.subjects["immunology"] == 1.0
    assert selection.hits[0].venue.keywords[0] == "immunology"


def test_keywords_split_on_commas():
    selection = select(keywords=["single cell RNA-seq, T cell exhaustion"])
    assert selection.manuscript.keywords == ("single cell RNA-seq", "T cell exhaustion")
    assert {"bioinformatics", "immunology"} <= set(selection.subjects)


def _write_tex(tmp_path):
    path = tmp_path / "paper.tex"
    path.write_text(
        r"""\documentclass{article}
\title{Hippocampal place cells remap during \emph{spatial} learning}
\begin{document}
\maketitle
\begin{abstract}
Using two-photon imaging in mice, we tracked CA1 neurons across days and found that synaptic
plasticity drives remapping of place fields, linking memory formation to cortical input.
\end{abstract}
\begin{keywords}
hippocampus, place cells, memory
\end{keywords}
\section{Introduction}
Text.
\end{document}
""",
        encoding="utf-8",
    )
    return path


def test_extract_manuscript_reads_title_abstract_keywords(tmp_path):
    q = extract_manuscript(_write_tex(tmp_path))
    assert q.title == "Hippocampal place cells remap during spatial learning"
    assert q.abstract.startswith("Using two-photon imaging")
    assert q.keywords == ("hippocampus", "place cells", "memory")
    assert infer_subjects(q)[0].subject == "neuroscience"


def test_extract_manuscript_missing_file(tmp_path):
    with pytest.raises(ValueError, match="no such file"):
        extract_manuscript(tmp_path / "nope.pdf")


def test_keywords_add_to_the_manuscripts(tmp_path):
    selection = select(manuscript=_write_tex(tmp_path), keywords=["two-photon imaging, memory", "CA1"])
    assert selection.manuscript.keywords == ("hippocampus", "place cells", "memory", "two-photon imaging", "CA1")


def test_title_and_abstract_override_the_manuscripts(tmp_path):
    selection = select(manuscript=_write_tex(tmp_path), title="Deep learning for chest X-ray triage", abstract="A convolutional network reads radiographs.")
    assert selection.manuscript.title == "Deep learning for chest X-ray triage"
    assert selection.manuscript.abstract == "A convolutional network reads radiographs."
    # The manuscript's own keywords (hippocampus, memory) still count alongside.
    assert "radiology" in [s.subject for s in selection.inferred]


def test_name_and_keywords_add_up():
    selection = select(name="nature", keywords=["hippocampus"])
    # Nature itself (the exact name) first; the keyword lifts Nature Neuroscience above the rest of the family.
    assert [h.venue.slug for h in selection.hits[:2]] == ["nature", "nature_neuroscience"]


def test_live_manuscript_ranks_neuroscience_venues(tmp_path):
    hits = select(manuscript=_write_tex(tmp_path)).hits
    assert hits[0].venue.slug in {"neuron", "nature_neuroscience"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_positional_keywords(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "-n", "3", "hippocampus"]) == 0
    out = capsys.readouterr().out
    assert "Subjects inferred: neuroscience" in out
    assert "neuron" in out and "paperpush subfile" in out


def test_cli_positional_words_form_one_keyword_until_a_comma(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--json", "single", "cell", "RNA-seq,", "T", "cell", "exhaustion"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["manuscript"]["keywords"] == ["single cell RNA-seq", "T cell exhaustion"]


def test_cli_k_adds_keywords(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--json", "hippocampus", "-k", "memory", "-k", "immunology"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["manuscript"]["keywords"] == ["hippocampus", "memory", "immunology"]


def test_cli_venue_name_and_aliases(capsys):
    from paperpush.cli import main

    for flag in ("--venue-name", "--journal-name", "--conference-name"):
        assert main(["pick-venue", "-n", "1", flag, "nature methods"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("nature_methods")


def test_cli_json_is_machine_readable(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--json", "--venue-name", "cell"]) == 0
    data = json.loads(capsys.readouterr().out)
    matches = data["matches"]
    assert matches[0]["slug"] == "cell"
    assert matches[0]["venue_type"] == "journal"
    assert {"slug", "name", "venue_type", "portal", "keywords", "score", "reasons"} <= set(matches[0])
    assert data["venue_name"] == "cell" and data["manuscript"] is None


def test_cli_type_filter(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--all", "-t", "preprint"]) == 0
    out = capsys.readouterr().out
    venue_lines = [line for line in out.splitlines() if line and not line.startswith("Use ")]
    assert len(venue_lines) == 4
    assert all("(preprint)" in line for line in venue_lines)


def test_cli_limit_and_hint(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "-n", "1", "genetics"]) == 0
    out = capsys.readouterr().out
    listed = [line for line in out.splitlines() if " (journal)" in line or " (preprint)" in line or " (conference)" in line]
    assert len(listed) == 1
    # Even a single displayed row keeps the next-step hint, pointing at the top-ranked hit.
    assert f"paperpush subfile {select(keywords=['genetics']).hits[0].venue.slug}" in out


def test_cli_zero_limit_keeps_the_next_step_hint(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "-n", "0", "genetics"]) == 0
    out = capsys.readouterr().out
    assert f"paperpush subfile {select(keywords=['genetics']).hits[0].venue.slug}" in out


def test_cli_rejects_missing_input(capsys):
    from paperpush.cli import main

    assert main(["pick-venue"]) == 2
    assert "needs keywords" in capsys.readouterr().err


def test_cli_keyword_naming_no_subject_fails(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "astrology"]) == 1
    assert "could not tell the subject" in capsys.readouterr().err


def test_cli_rejects_a_venue_name_with_no_match(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--venue-name", "zzz-no-such-venue"]) == 1
    assert "no venues matched the venue name" in capsys.readouterr().err


def test_cli_rejects_filters_with_no_match(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--portal", "no-such-portal"]) == 1
    assert "no venues matched" in capsys.readouterr().err


def test_cli_title_and_abstract(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--title", _TCELL_TITLE, "--abstract", _TCELL_ABSTRACT]) == 0
    out = capsys.readouterr().out
    assert "Subjects inferred: immunology" in out
    assert "science_immunology" in out
    assert "--ai" in out


def test_cli_manuscript_as_positional_file(tmp_path, capsys):
    from paperpush.cli import main

    assert main(["pick-venue", str(_write_tex(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "Read the title, abstract, and keywords" in out
    assert "Title: Hippocampal place cells" in out


def test_cli_abstract_with_no_subject_fails(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--abstract", "Lorem ipsum dolor sit amet."]) == 1
    assert "could not tell the subject" in capsys.readouterr().err


def test_cli_json_with_manuscript(tmp_path, capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--json", "--manuscript", str(_write_tex(tmp_path))]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["manuscript"]["keywords"] == ["hippocampus", "place cells", "memory"]
    assert data["inferred_subjects"][0]["subject"] == "neuroscience"
    assert data["ai"] is None
    assert len(data["matches"]) <= 5 <= data["total_matches"]


# ---------------------------------------------------------------------------
# --ai, with the LLM call stubbed
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_llm(monkeypatch):
    import importlib

    # The package exports an ``autofill`` function that shadows the module name.
    autofill = importlib.import_module("paperpush.autofill")
    calls = []

    def call(model, documents, instructions, schema, max_tokens, system="", **kwargs):
        calls.append({"model": model, "documents": documents, "instructions": instructions, "schema": schema, "system": system})
        return json.dumps(
            {
                "subjects": ["immunology", "bioinformatics"],
                "venues": [
                    {"slug": "science_immunology", "fit": "strong", "reason": "T cell exhaustion is core immunology."},
                    {"slug": "not_a_venue", "fit": "good", "reason": "ignored"},
                    {"slug": "biorxiv", "fit": "good", "reason": "Fast preprint."},
                ],
            }
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setitem(autofill._CALLERS, "anthropic", call)
    return calls


def test_rank_via_api_keeps_only_known_slugs(fake_llm):
    from paperpush.database import get_venue

    candidates = [get_venue("science_immunology"), get_venue("biorxiv")]
    ranking = rank_via_api(ManuscriptQuery(title=_TCELL_TITLE, abstract=_TCELL_ABSTRACT), candidates, provider="anthropic", limit=5)
    assert [p.slug for p in ranking.picks] == ["science_immunology", "biorxiv"]
    assert ranking.subjects == ["immunology", "bioinformatics"]
    assert fake_llm[0]["documents"] == []
    assert "science_immunology" in fake_llm[0]["instructions"]
    assert fake_llm[0]["schema"]["properties"]["venues"]["items"]["properties"]["slug"]["enum"] == ["science_immunology", "biorxiv"]


def test_cli_ai_prints_the_models_ranking(fake_llm, capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--ai", "--provider", "anthropic", "--title", _TCELL_TITLE, "--abstract", _TCELL_ABSTRACT]) == 0
    out = capsys.readouterr().out
    assert "Subjects (anthropic" in out
    lines = [line for line in out.splitlines() if line.startswith(("science_immunology", "biorxiv"))]
    assert lines[0].startswith("science_immunology") and "strong: T cell exhaustion" in lines[0]
    assert "paperpush subfile science_immunology" in out


def test_cli_ai_needs_manuscript_input(capsys):
    from paperpush.cli import main

    assert main(["pick-venue", "--ai", "--venue-name", "nature"]) == 2
    assert "--ai needs something to judge" in capsys.readouterr().err


def test_cli_ai_without_a_key_fails_cleanly(monkeypatch, capsys):
    from paperpush.cli import main

    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert main(["pick-venue", "--ai", "--abstract", _TCELL_ABSTRACT]) == 1
    assert "no API key" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------


def test_mcp_pick_venue_with_subject_keywords():
    from paperpush.mcp_server import pick_venue

    result = pick_venue(keywords=["radiology", "machine learning"], limit=3)
    assert len(result["matches"]) == 3
    assert result["matches"][0]["slug"] in {"ieee_tmi", "medical_image_analysis", "radiology_ai"}
    assert "radiology" in result["subject_vocabulary"] and "general" not in result["subject_vocabulary"]


def test_mcp_pick_venue_by_name():
    from paperpush.mcp_server import pick_venue

    assert pick_venue(venue_name="nature methods", limit=1)["matches"][0]["slug"] == "nature_methods"


def test_mcp_pick_venue_manuscript_with_no_subject_has_a_note(tmp_path):
    from paperpush.mcp_server import pick_venue

    path = tmp_path / "abstract.txt"
    path.write_text("Lorem ipsum dolor sit amet.", encoding="utf-8")
    result = pick_venue(manuscript=str(path))
    assert result["matches"] == []
    assert "as `keywords`" in result["note"]


def test_mcp_pick_venue_needs_input():
    from paperpush.mcp_server import pick_venue

    with pytest.raises(ValueError):
        pick_venue()
