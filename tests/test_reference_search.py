"""Tests for paperpush.reference_search: references without a DOI, and publication versions.

Every registry query is stubbed (the conftest also makes any real request
"fail"), so these run offline. Several cases mirror the evals of the
``references`` skill: journal article over preprint, latest preprint version,
and a failed lookup that must not be read as a missing work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paperpush import reference_search as rs
from paperpush import references
from paperpush.reference_search import MATCHED, NEAR, NOT_FOUND, UNKNOWN, Cited, Reference


# Captured at import, before the conftest's autouse fixture stubs it.
_REAL_GET = rs._Service.get


def _write_bib(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "refs.bib"
    path.write_text(text, encoding="utf-8")
    return path


def _refs(tmp_path: Path, text: str) -> list[Reference]:
    return rs.references_from_entries(references.collect_entries([_write_bib(tmp_path, text)]))


def _record(title, family="Bray", year=2016, doi="10.1038/nbt.3519", kind="journal-article", **extra):
    return {"title": [title], "author": [{"family": family, "given": "N"}], "issued": {"date-parts": [[year]]}, "DOI": doi, "type": kind, **extra}


KALLISTO = _record("Near-optimal probabilistic RNA-seq quantification")


@pytest.fixture
def registry(monkeypatch):
    """Stub the four services; tests fill in what each one answers."""
    answers = {"crossref": [], "openalex": [], "arxiv": {}, "biorxiv": {}}
    calls = {"crossref": [], "openalex": [], "arxiv": [], "biorxiv": []}

    def crossref(query, service):
        calls["crossref"].append(query)
        return answers["crossref"]

    def openalex(title, service):
        calls["openalex"].append(title)
        return answers["openalex"]

    def arxiv(ids, service):
        calls["arxiv"].append(list(ids))
        return None if answers["arxiv"] is None else {i: answers["arxiv"][i] for i in ids if i in answers["arxiv"]}

    def biorxiv(doi, service):
        calls["biorxiv"].append(doi)
        return answers["biorxiv"].get(doi, "")

    monkeypatch.setattr(rs, "crossref_search", crossref)
    monkeypatch.setattr(rs, "openalex_search", openalex)
    monkeypatch.setattr(rs, "arxiv_lookup", arxiv)
    monkeypatch.setattr(rs, "biorxiv_published_doi", biorxiv)
    return answers, calls


# --- reading references ----------------------------------------------------------------


@pytest.mark.parametrize(
    "fields, expected",
    [
        ("eprint={1706.03762v1}, archivePrefix={arXiv}", ("1706.03762", 1)),
        ("eprint={1706.03762}", ("1706.03762", None)),
        ("journal={arXiv preprint arXiv:1706.03762}", ("1706.03762", None)),
        ("url={https://arxiv.org/abs/2101.00001v3}", ("2101.00001", 3)),
        ("doi={10.48550/arXiv.1412.6980}", ("1412.6980", None)),
        ("eprint={hep-th/9901001v2}, archivePrefix={arXiv}", ("hep-th/9901001", 2)),
        ("journal={Nature}", ("", None)),
        ("eprint={12345}, archivePrefix={PubMed}", ("", None)),
    ],
)
def test_arxiv_id_read_from_whichever_field_carries_it(fields, expected):
    (entry,) = references.parse_bibtex("@article{k, title={T}, " + fields + "}")
    assert rs.arxiv_id_of(entry) == expected


def test_references_from_entries_keeps_the_fields_the_search_needs(tmp_path):
    (ref,) = _refs(tmp_path, "@article{k, title={The {C. elegans} Genome}, author={Grün, Dominic and Lee, A}, year={2019}, journal={Cell}}")
    assert (ref.label, ref.title, ref.author, ref.year, ref.kind, ref.venue) == ("bibliography entry 'k'", "The C. elegans Genome", "Grün", "2019", "article", "Cell")
    assert ref.indexed


def test_books_and_software_are_not_indexed_kinds(tmp_path):
    refs = _refs(tmp_path, "@book{b, title={A Book Title Here}, author={A, B}}\n@software{s, title={Some Tool Name}, author={A, B}}")
    assert [r.indexed for r in refs] == [False, False]


# --- matching --------------------------------------------------------------------------


def test_title_similarity_ignores_case_markup_and_a_dropped_subtitle():
    assert rs.title_similarity("Near-optimal probabilistic {RNA}-seq quantification", "Near-Optimal Probabilistic RNA-Seq Quantification") == 1.0
    assert rs.title_similarity("Deep learning for single-cell genomics", "Deep learning for single-cell genomics: a review") == 1.0
    assert rs.title_similarity("Deep learning", "Unrelated title about soil") < rs.NEAR_TITLE_RATIO


def test_an_entry_matching_a_registered_work_has_no_problems():
    ref = Reference("e", "refs.bib", title="Near-optimal probabilistic RNA-seq quantification", author="Bray", year="2016", kind="article")
    lookup = rs.match_entry(ref, [KALLISTO])
    assert (lookup.status, lookup.problems) == (MATCHED, [])


def test_errata_and_datasets_are_never_the_match():
    ref = Reference("e", "refs.bib", title="Near-optimal probabilistic RNA-seq quantification", author="Bray", year="2016", kind="article")
    erratum = _record("Erratum: Near-optimal probabilistic RNA-seq quantification", doi="10.1038/nbt0816-888d")
    dataset = _record("Near-optimal probabilistic RNA-seq quantification", doi="10.5281/x", kind="dataset")
    assert rs.match_entry(ref, [erratum, dataset]).status == NOT_FOUND


def test_a_wrong_first_author_is_reported_against_the_matching_work():
    ref = Reference("e", "refs.bib", title="Near-optimal probabilistic RNA-seq quantification", author="Johnson", year="2016", kind="article")
    lookup = rs.match_entry(ref, [KALLISTO])
    assert lookup.status == MATCHED
    assert "Johnson is not among its authors" in lookup.problems[0]


def test_a_family_name_particle_is_not_a_different_author():
    ref = Reference("e", "refs.bib", title="Near-optimal probabilistic RNA-seq quantification", author="Beethoven", year="2016", kind="article")
    record = _record("Near-optimal probabilistic RNA-seq quantification", family="van Beethoven")
    assert rs.match_entry(ref, [record]).problems == []


def test_a_year_far_from_every_matching_work_is_reported():
    ref = Reference("e", "refs.bib", title="Near-optimal probabilistic RNA-seq quantification", author="Bray", year="2009", kind="article")
    assert "registered as 2016" in rs.match_entry(ref, [KALLISTO]).problems[0]


def test_of_two_same_title_works_the_nearer_year_is_matched():
    """A preprint and its article share a title; the entry's year picks which one it cites."""
    ref = Reference("e", "refs.bib", title="Modular and efficient pre-processing of single-cell RNA-seq", author="Melsted", year="2019", kind="article")
    article = _record("Modular and efficient pre-processing of single-cell RNA-seq", family="Melsted", year=2021, doi="10.1038/s41587-021-00870-2")
    preprint = _record("Modular and efficient pre-processing of single-cell RNA-seq", family="Melsted", year=2019, doi="10.1101/673285", kind="posted-content")
    lookup = rs.match_entry(ref, [article, preprint])
    assert lookup.record["DOI"] == "10.1101/673285" and lookup.problems == []


def test_a_similar_but_different_title_on_the_same_topic_is_not_a_near_miss():
    ref = Reference("e", "refs.bib", title="Quantum entanglement governs sourdough microbiome assembly in alpine bakeries", author="Smith", year="2021", kind="article")
    other = _record("A Quantum Entanglement Assembly Line", family="Schirber", year=2022, doi="10.1103/physics.15.s135")
    assert rs.match_entry(ref, [other]).status == NOT_FOUND


def test_a_garbled_title_is_a_near_miss():
    ref = Reference("e", "refs.bib", title="Near optimal probabilistic quantification of RNA-seq data", author="Bray", year="2016", kind="article")
    assert rs.match_entry(ref, [KALLISTO]).status == NEAR


def test_a_prose_entry_matches_when_its_text_has_the_title_and_first_author():
    ref = Reference("[3]", "manuscript.pdf", text="3. Bray NL, Pimentel H, Melsted P, Pachter L. Near-optimal probabilistic RNA-seq quantification. Nat Biotechnol. 2016;34:525.")
    assert rs.match_text(ref, [KALLISTO]).status == MATCHED
    elsewhere = Reference("[4]", "manuscript.pdf", text="4. Smith J. Some other paper on sourdough. J Bread. 2020;1:1.")
    assert rs.match_text(elsewhere, [KALLISTO]).status == NOT_FOUND


# --- search_references -----------------------------------------------------------------


def test_a_journal_entry_no_registry_knows_is_reported(tmp_path, registry):
    answers, calls = registry
    refs = _refs(tmp_path, "@article{fake, title={Quantum entanglement governs sourdough microbiome assembly}, author={Smith, John}, journal={Nature}, year={2021}}")
    result = rs.search_references(refs)
    (finding,) = result.findings
    assert finding.category == "no registered work"
    assert "bibliography entry 'fake'" in finding.detail and "Crossref and OpenAlex" in finding.detail
    assert calls["crossref"] and calls["openalex"]


def test_a_book_no_registry_knows_is_unverified_not_accused(tmp_path, registry):
    refs = _refs(tmp_path, "@book{b, title={Molecular Cloning: A Laboratory Manual}, author={Sambrook, Joseph}, year={2001}}")
    result = rs.search_references(refs)
    assert result.findings == []
    assert [lk.ref.label for lk in result.unverified] == ["bibliography entry 'b'"]


def test_a_book_matched_to_a_namesake_is_unverified_not_accused(tmp_path, registry):
    answers, _ = registry
    answers["crossref"] = [_record("Molecular Cloning: A Laboratory Manual", family="Maniatis", year=1985, doi="10.1002/abio.370050118")]
    refs = _refs(tmp_path, "@book{b, title={Molecular Cloning: A Laboratory Manual}, author={Sambrook, Joseph}, year={2001}}")
    result = rs.search_references(refs)
    assert result.findings == [] and result.unverified[0].status == NEAR


def test_a_matched_entry_without_a_doi_is_told_the_doi(tmp_path, registry):
    answers, calls = registry
    answers["crossref"] = [KALLISTO]
    refs = _refs(tmp_path, "@article{kallisto, title={Near-optimal probabilistic {RNA}-seq quantification}, author={Bray, Nicolas L}, journal={Nat Biotech}, year={2016}}")
    result = rs.search_references(refs)
    assert result.findings == []
    assert [lk.record["DOI"] for lk in result.suggestions] == ["10.1038/nbt.3519"]
    assert calls["openalex"] == []  # Crossref settled it


def test_entries_with_a_doi_or_too_short_a_title_are_not_searched(tmp_path, registry):
    _, calls = registry
    refs = _refs(tmp_path, "@article{a, title={Some long enough title}, doi={10.1/x}}\n@misc{r, title={R}, author={R Core Team}}")
    assert rs.search_references(refs).lookups == []
    assert calls["crossref"] == []


def test_a_failed_lookup_is_unknown_not_missing(tmp_path, registry):
    """The skill's unresolved-lookup case: a registry that does not answer is no evidence the work is missing."""
    answers, _ = registry
    answers["crossref"] = None
    answers["openalex"] = None
    refs = _refs(tmp_path, "@article{x, title={A perfectly real paper title}, author={Doe, Jane}, journal={J}, year={2020}}")
    result = rs.search_references(refs)
    assert result.findings == [] and [lk.status for lk in result.lookups] == [UNKNOWN]


def test_an_arxiv_id_arxiv_does_not_know_is_reported(tmp_path, registry):
    answers, calls = registry
    answers["arxiv"] = {}
    refs = _refs(tmp_path, "@article{a, title={Attention is all you need}, author={Vaswani, A}, eprint={2401.99999}, archivePrefix={arXiv}}")
    (finding,) = rs.search_references(refs).findings
    assert "arXiv has no record of" in finding.detail
    assert calls["crossref"] == []


def test_an_arxiv_id_belonging_to_another_paper_is_reported(tmp_path, registry):
    answers, _ = registry
    answers["arxiv"] = {"1706.03762": {"title": "Attention Is All You Need", "author": [], "issued": {}, "latest_version": 7, "journal_doi": "", "journal_ref": ""}}
    refs = _refs(tmp_path, "@article{a, title={Adam: a method for stochastic optimization}, author={Kingma, D}, eprint={1706.03762}}")
    (finding,) = rs.search_references(refs).findings
    assert 'that identifier is "Attention Is All You Need"' in finding.detail


def test_searches_stop_at_the_budget_and_the_rest_are_listed(tmp_path, registry, monkeypatch):
    monkeypatch.setattr(rs, "MAX_SEARCHES", 2)
    bib = "\n".join(f"@article{{k{i}, title={{A real enough title number {i}}}, author={{A, B}}, journal={{J}}}}" for i in range(5))
    result = rs.search_references(_refs(tmp_path, bib))
    assert len(result.lookups) == 2 and len(result.skipped) == 3
    assert "at most 2 references" in result.skipped_reason


def test_a_service_that_keeps_failing_is_given_up_on(monkeypatch):
    service = rs._Service("Test", 10)
    attempts = []

    def fail(*a, **k):
        attempts.append(1)
        raise OSError("down")

    # The conftest stubs _Service.get; the real one is under test here.
    monkeypatch.setattr(rs._Service, "get", _REAL_GET)
    monkeypatch.setattr(rs.urllib.request, "urlopen", fail)
    monkeypatch.setattr(rs, "RETRY_DELAY", 0)
    for _ in range(5):
        assert service.get("https://example.invalid") is None
    assert service.down and len(attempts) == rs.MAX_CONSECUTIVE_FAILURES * (1 + rs.RETRIES)


# --- versions --------------------------------------------------------------------------


def _preprint(doi="10.1101/673285", published=""):
    rec = _record("Modular and efficient pre-processing of single-cell RNA-seq", family="Melsted", year=2019, doi=doi, kind="posted-content")
    if published:
        rec["relation"] = {"is-preprint-of": [{"id-type": "doi", "id": published}]}
    return rec


def test_a_published_preprint_is_reported_with_its_article(registry):
    """The skill's journal-over-preprint case: cite the article once it exists."""
    cited = [Cited("bibliography entry 'pre'", "refs.bib", "10.1101/673285", _preprint(published="10.1038/s41587-021-00870-2"))]
    (finding,) = rs.version_findings(cited, [], {"10.1101/673285"})
    assert finding.category == "preprint has been published" and "10.1038/s41587-021-00870-2" in finding.detail


def test_a_preprint_cited_with_its_own_article_is_a_duplicate(registry):
    cited = [Cited("bibliography entry 'pre'", "refs.bib", "10.1101/673285", _preprint(published="10.1038/s41587-021-00870-2"))]
    (finding,) = rs.version_findings(cited, [], {"10.1101/673285", "10.1038/s41587-021-00870-2"})
    assert finding.category == "preprint and article both cited"


def test_biorxiv_is_asked_when_the_record_has_no_relation(registry):
    answers, calls = registry
    answers["biorxiv"] = {"10.1101/673285": "10.1038/s41587-021-00870-2"}
    (finding,) = rs.version_findings([Cited("p", "refs.bib", "10.1101/673285", _preprint())], [], set())
    assert "10.1038/s41587-021-00870-2" in finding.detail and calls["biorxiv"] == ["10.1101/673285"]


def test_a_journal_article_is_not_checked_for_a_newer_version(registry):
    _, calls = registry
    assert rs.version_findings([Cited("k", "refs.bib", "10.1038/nbt.3519", KALLISTO)], [], set()) == []
    assert calls["biorxiv"] == []


def _arxiv(latest=7, journal_doi="", journal_ref=""):
    return {"title": "Attention Is All You Need", "author": [], "issued": {}, "latest_version": latest, "journal_doi": journal_doi, "journal_ref": journal_ref}


def test_an_old_arxiv_version_is_reported(registry):
    """The skill's latest-preprint case."""
    ref = Reference("bibliography entry 'a'", "refs.bib", title="Attention is all you need", arxiv_id="1706.03762", arxiv_version=1)
    (finding,) = rs.version_findings([], [ref], set(), arxiv_records={"1706.03762": _arxiv()}, arxiv_checked={"1706.03762"})
    assert "v1, but the latest version is v7" in finding.detail


def test_an_unpinned_arxiv_id_is_already_the_latest(registry):
    ref = Reference("a", "refs.bib", title="Attention is all you need", arxiv_id="1706.03762")
    assert rs.version_findings([], [ref], set(), arxiv_records={"1706.03762": _arxiv()}, arxiv_checked={"1706.03762"}) == []


def test_an_arxiv_preprint_with_a_journal_doi_is_reported(registry):
    ref = Reference("a", "refs.bib", title="Attention is all you need", arxiv_id="1706.03762")
    (finding,) = rs.version_findings([], [ref], set(), arxiv_records={"1706.03762": _arxiv(journal_doi="10.5555/journal")}, arxiv_checked={"1706.03762"})
    assert finding.category == "preprint has been published"


def test_a_journal_ref_is_not_reported_when_the_entry_already_names_a_venue(registry):
    rec = {"1706.03762": _arxiv(journal_ref="NeurIPS 2017")}
    named = Reference("a", "refs.bib", title="Attention", arxiv_id="1706.03762", venue="Advances in Neural Information Processing Systems")
    bare = Reference("b", "refs.bib", title="Attention", arxiv_id="1706.03762", venue="arXiv preprint")
    assert rs.version_findings([], [named], set(), arxiv_records=rec, arxiv_checked=set(rec)) == []
    assert len(rs.version_findings([], [bare], set(), arxiv_records=rec, arxiv_checked=set(rec))) == 1


def test_arxiv_ids_the_search_already_asked_about_are_not_asked_again(registry):
    _, calls = registry
    ref = Reference("a", "refs.bib", arxiv_id="2401.99999")
    rs.version_findings([], [ref], set(), arxiv_records={}, arxiv_checked={"2401.99999"})
    assert calls["arxiv"] == []


# --- the AI task -----------------------------------------------------------------------


def test_ai_instructions_list_each_unverified_reference_with_its_closest_work():
    lk = rs.Lookup(Reference("bibliography entry 'b'", "refs.bib", title="Molecular Cloning", author="Sambrook", year="2001", kind="book"), NEAR, candidates=[_record("Molecular cloning", family="Maniatis", year=1985, doi="10.1/x")])
    text = rs.ai_task_instructions([lk], [Reference("reference [9]", "m.pdf", text="9. Some entry text that was never looked up.")])
    assert "bibliography entry 'b'" in text and "Maniatis" in text
    assert "reference [9]" in text and "Not looked up" in text
    assert "'unclear'" in text


# --- BibTeX structure ------------------------------------------------------------------


def test_bib_structure_findings(tmp_path):
    bib = _write_bib(
        tmp_path,
        "@article{dup, title={One}, author={A, B}}\n"
        "@article{DUP, title={Two}, author={A, B}}\n"
        "@article{, title={Keyless}, author={A, B}}\n"
        "@inproceedings{notitle, author={A, B}}\n"
        "@misc{web, howpublished={a url}}\n"
        "@article{x1, title={T}, author={A}, eprint={1706.03762}}\n"
        "@article{x2, title={T}, author={A}, journal={arXiv preprint arXiv:1706.03762v2}}\n"
        "@article{open, title={Unclosed}, author={A, B}\n",
    )
    findings, total = references.bib_structure_findings([bib])
    assert total == 8
    assert sorted(f.category for f in findings) == ["duplicate arXiv preprint", "duplicate citation key", "entry without a key", "incomplete entry", "unclosed entry"]


def test_a_clean_bib_has_no_structure_findings(tmp_path):
    bib = _write_bib(tmp_path, "@article{a, title={One}, author={A, B}, year={2020}}\n@misc{b, title={Two}}\n")
    assert references.bib_structure_findings([bib]) == ([], 2)


# --- scan_references hands its records on -----------------------------------------------


def test_scan_references_returns_the_records_that_check_out(tmp_path, monkeypatch):
    bib = _write_bib(tmp_path, "@article{k, title={Near-optimal probabilistic RNA-seq quantification}, author={Bray, N}, year={2016}, doi={10.1038/nbt.3519}}\n@article{w, title={Something else}, author={Z, Z}, year={1990}, doi={10.1/wrong}}")
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (references.DOI_FOUND, KALLISTO))
    resolved: dict = {}
    references.scan_references([bib], resolved_out=resolved)
    assert list(resolved) == ["10.1038/nbt.3519"]  # the mismatched one is not handed on
