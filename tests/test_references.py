"""Unit tests for :mod:`paperpush.references`.

These exercise the bibliography check in three layers: the BibTeX parser (entry
shapes, value delimiters, non-entry blocks), the offline rules (malformed and
duplicated DOIs), and the registry comparison (title / first author / year
against a CSL JSON record). Every network call goes through ``_resolve_doi``,
which the tests patch, so nothing here touches doi.org.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from paperpush import references
from paperpush.references import DOI_FOUND, DOI_MISSING, DOI_UNKNOWN, Entry, collect_citations, collect_entries, normalize_doi, parse_bibtex, scan_references

# Captured at import time, before conftest's autouse fixture stubs resolution
# out to keep the suite offline; the transport tests at the end exercise the
# real request logic against a patched ``urlopen``.
_real_resolve_doi = references._resolve_doi


def _categories(findings) -> set[str]:
    return {f.category for f in findings}


def _bib(tmp_path: Path, text: str, name: str = "ref.bib") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _entry(**kwargs) -> Entry:
    """An :class:`Entry` with blank defaults, overridden by ``kwargs``."""
    fields = {"key": "k", "where": "ref.bib", "raw_doi": "", "doi": "", "title": "", "author": "", "year": ""}
    fields.update(kwargs)
    return Entry(**fields)


def _record(title="A Paper", family="Lovelace", year=1843) -> dict:
    """A minimal CSL JSON record of the shape doi.org returns."""
    return {"title": title, "author": [{"given": "Ada", "family": family}], "issued": {"date-parts": [[year, 1, 1]]}}


# --- DOI normalization -----------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "10.1234/abc",
        " 10.1234/abc ",
        "https://doi.org/10.1234/abc",
        "http://dx.doi.org/10.1234/abc",
        "doi:10.1234/abc",
        "{10.1234/abc}",
        "10.1234/abc.",
        "10.1234/ABC",
    ],
)
def test_normalize_doi_strips_prefixes_case_and_punctuation(raw):
    assert normalize_doi(raw) == "10.1234/abc"


def test_normalize_doi_unescapes_latex_punctuation():
    assert normalize_doi(r"10.1234/a\_b") == "10.1234/a_b"


# --- BibTeX parsing --------------------------------------------------------


def test_parse_bibtex_reads_the_fields_the_check_needs():
    text = "@article{lovelace1843,\n  author = {Lovelace, Ada},\n  title = {Notes on the Engine},\n  year = {1843},\n  doi = {10.1234/abc},\n}\n"
    (entry,) = parse_bibtex(text, "ref.bib")
    assert entry.key == "lovelace1843"
    assert entry.where == "ref.bib"
    assert entry.title == "Notes on the Engine"
    assert entry.author == "Lovelace, Ada"
    assert entry.year == "1843"
    assert entry.doi == "10.1234/abc"


def test_parse_bibtex_handles_quoted_and_bare_values():
    text = '@inproceedings{t50,\n  author = "Alan M. Turing",\n  year = 1950,\n  DOI = {10.1093/mind/LIX.236.433}\n}\n'
    (entry,) = parse_bibtex(text)
    assert entry.author == "Alan M. Turing"
    assert entry.year == "1950"
    # Field names are case-folded, so DOI and doi land in the same slot.
    assert entry.doi == "10.1093/mind/lix.236.433"


def test_parse_bibtex_keeps_nested_braces_in_a_title():
    text = "@article{k, title = {The {C. elegans} Genome}, doi = {10.1/x}}\n"
    (entry,) = parse_bibtex(text)
    assert entry.title == "The {C. elegans} Genome"


def test_parse_bibtex_skips_string_preamble_and_comment_blocks():
    text = '@string{tsm = "Taylor\'s Memoirs"}\n@preamble{"\\newcommand{\\x}{}"}\n@comment{@article{nope, doi = {10.9/9}}}\n@article{real, doi = {10.1/x}}\n'
    keys = [e.key for e in parse_bibtex(text)]
    assert keys == ["real"]


def test_parse_bibtex_reads_an_unclosed_final_entry():
    # A truncated file still yields the entries that came before it.
    text = "@article{first, doi = {10.1/a}}\n@article{second, doi = {10.1/b}\n"
    assert [e.key for e in parse_bibtex(text)] == ["first", "second"]


def test_parse_bibtex_leaves_an_unexpanded_macro_as_literal_text():
    # @string macros are not expanded; the value is read literally, which at
    # worst leaves a field unchecked -- it must never invent a mismatch.
    text = "@article{k, journal = tsm, title = {T}, doi = {10.1/x}}\n"
    (entry,) = parse_bibtex(text)
    assert entry.title == "T"


def test_parse_bibtex_returns_nothing_for_a_file_with_no_entries():
    assert parse_bibtex("% just a comment\n") == []


# --- collecting bibliographies off the submission --------------------------


def test_collect_entries_reads_a_bib_file(tmp_path):
    path = _bib(tmp_path, "@article{k, title = {T}, doi = {10.1/x}}\n")
    (entry,) = collect_entries([path])
    assert entry.key == "k"
    assert entry.where == "ref.bib"


def test_collect_entries_reads_a_bib_inside_a_source_archive(tmp_path):
    bundle = tmp_path / "source.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("main.tex", "\\bibliography{ref}\n")
        zf.writestr("ref.bib", "@article{k, title = {T}, doi = {10.1/x}}\n")
    (entry,) = collect_entries([bundle])
    assert entry.key == "k"
    assert entry.where == "source.zip:ref.bib"


def test_collect_entries_ignores_non_bibliography_files(tmp_path):
    tex = tmp_path / "main.tex"
    tex.write_text("See doi 10.1234/abc and @article{notreally, doi={10.9/9}}\n", encoding="utf-8")
    assert collect_entries([tex]) == []


def test_collect_entries_swallows_an_unreadable_file(tmp_path):
    assert collect_entries([tmp_path / "absent.bib"]) == []


# --- offline rules ---------------------------------------------------------


def test_malformed_doi_flagged(tmp_path):
    path = _bib(tmp_path, "@article{k, title = {T}, doi = {not-a-doi}}\n")
    (finding,) = scan_references([path], check_registry=False)
    assert finding.category == "malformed DOI"
    assert "not-a-doi" in finding.detail
    assert finding.where == "ref.bib"


def test_duplicate_doi_across_entries_flagged(tmp_path):
    path = _bib(tmp_path, "@article{a, doi = {10.1234/abc}}\n@article{b, doi = {10.1234/ABC}}\n")
    (finding,) = scan_references([path], check_registry=False)
    assert finding.category == "duplicate DOI"
    # Both keys are named so the author can find the pair.
    assert "'b'" in finding.detail and "'a'" in finding.detail


def test_entries_without_a_doi_are_left_alone(tmp_path):
    path = _bib(tmp_path, "@book{k, title = {A Book}, author = {Someone}, year = {1999}}\n")
    assert scan_references([path], check_registry=False) == []


def test_check_registry_false_makes_no_lookups(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: calls.append(doi) or (DOI_UNKNOWN, None))
    path = _bib(tmp_path, "@article{k, title = {T}, doi = {10.1234/abc}}\n")
    assert scan_references([path], check_registry=False) == []
    assert calls == []


# --- registry comparison ---------------------------------------------------


def test_unregistered_doi_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_MISSING, None))
    path = _bib(tmp_path, "@article{k, title = {T}, doi = {10.1234/abc}}\n")
    (finding,) = scan_references([path])
    assert finding.category == "unresolvable DOI"
    assert "10.1234/abc" in finding.detail


def test_matching_record_produces_no_finding(tmp_path, monkeypatch):
    record = _record(title="Notes on the Analytical Engine", family="Lovelace", year=1843)
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_FOUND, record))
    path = _bib(tmp_path, "@article{k, author = {Ada Lovelace}, title = {Notes on the Analytical Engine}, year = {1843}, doi = {10.1234/abc}}\n")
    assert scan_references([path]) == []


def test_wrong_doi_reports_every_mismatched_field(tmp_path, monkeypatch):
    record = _record(title="The Genome of C. elegans", family="Brenner", year=1974)
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_FOUND, record))
    path = _bib(tmp_path, "@article{k, author = {Ada Lovelace}, title = {Notes on the Analytical Engine}, year = {1843}, doi = {10.1234/abc}}\n")
    (finding,) = scan_references([path])
    assert finding.category == "DOI metadata mismatch"
    # One finding carries all three disagreements rather than three findings.
    assert "title is" in finding.detail
    assert "first author is Lovelace" in finding.detail
    assert "year is 1843" in finding.detail
    assert "'k'" in finding.detail


def test_unknown_resolution_stays_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_UNKNOWN, None))
    path = _bib(tmp_path, "@article{k, title = {T}, year = {1999}, doi = {10.1234/abc}}\n")
    assert scan_references([path]) == []


def test_malformed_doi_is_never_looked_up(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: calls.append(doi) or (DOI_UNKNOWN, None))
    path = _bib(tmp_path, "@article{k, doi = {nonsense}}\n")
    assert _categories(scan_references([path])) == {"malformed DOI"}
    assert calls == []


def test_a_shared_doi_is_resolved_once_but_reported_per_entry(tmp_path, monkeypatch):
    calls = []
    record = _record(title="Something Else Entirely", family="Brenner", year=1974)
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: calls.append(doi) or (DOI_FOUND, record))
    path = _bib(tmp_path, "@article{a, title = {Notes on the Engine}, doi = {10.1234/abc}}\n@article{b, title = {Notes on the Engine}, doi = {10.1234/abc}}\n")
    findings = scan_references([path])
    assert calls == ["10.1234/abc"]  # one lookup for the distinct DOI
    assert [f.category for f in findings] == ["duplicate DOI", "DOI metadata mismatch", "DOI metadata mismatch"]


def test_lookups_are_capped_and_the_truncation_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "MAX_DOI_CHECKS", 3)
    calls = []
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: calls.append(doi) or (DOI_UNKNOWN, None))
    text = "".join(f"@article{{k{i}, title = {{T}}, doi = {{10.1234/e{i}}}}}\n" for i in range(6))
    findings = scan_references([_bib(tmp_path, text)])
    assert len(calls) == 3
    (finding,) = findings
    assert finding.category == "DOI checks truncated"
    assert "first 3 of 6" in finding.detail


def test_lookups_run_concurrently(tmp_path, monkeypatch):
    import threading

    barrier = threading.Barrier(references.DOI_CHECK_WORKERS, timeout=5)

    def resolve(doi, **kwargs):
        # Deadlocks (and fails the test) unless the pool really runs this many
        # lookups at once.
        barrier.wait()
        return DOI_UNKNOWN, None

    monkeypatch.setattr(references, "_resolve_doi", resolve)
    text = "".join(f"@article{{k{i}, doi = {{10.1234/e{i}}}}}\n" for i in range(references.DOI_CHECK_WORKERS))
    assert scan_references([_bib(tmp_path, text)]) == []


# --- reading DOIs out of the manuscript itself -----------------------------

REFS = "Body text here.\n\nReferences\n\n"


def _doc(tmp_path: Path, text: str, name: str = "manuscript.txt") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _turing(**over) -> dict:
    record = {"title": "Computing Machinery and Intelligence", "author": [{"family": "Turing"}], "issued": {"date-parts": [[1950]]}}
    record.update(over)
    return record


def test_collect_citations_reads_dois_from_prose(tmp_path):
    doc = _doc(tmp_path, REFS + "1. Turing, A. M. Mind 59, 433 (1950). doi:10.1093/mind/LIX.236.433\n")
    (citation,) = collect_citations([doc])
    assert citation.doi == "10.1093/mind/lix.236.433"
    assert citation.where == "manuscript.txt"
    assert citation.in_reference_list is True


@pytest.mark.parametrize(
    "written",
    [
        "doi:10.1093/mind/LIX.236.433",
        "https://doi.org/10.1093/mind/LIX.236.433",
        "DOI 10.1093/mind/LIX.236.433.",
        "(10.1093/mind/LIX.236.433)",
        "10.1093/mind/LIX.236.433,",
    ],
)
def test_citation_dois_recognised_however_they_are_written(tmp_path, written):
    (citation,) = collect_citations([_doc(tmp_path, REFS + f"1. Turing. Mind (1950). {written}\n")])
    assert citation.doi == "10.1093/mind/lix.236.433"


def test_a_doi_before_the_reference_list_is_not_in_it(tmp_path):
    doc = _doc(tmp_path, "Data are at https://doi.org/10.5281/zenodo.123456\n\nReferences\n\n1. Turing. doi:10.1093/mind/LIX.236.433\n")
    dataset, reference = collect_citations([doc])
    assert (dataset.doi, dataset.in_reference_list) == ("10.5281/zenodo.123456", False)
    assert (reference.doi, reference.in_reference_list) == ("10.1093/mind/lix.236.433", True)


def test_a_document_with_no_reference_heading_has_no_reference_list(tmp_path):
    doc = _doc(tmp_path, "Data are at https://doi.org/10.5281/zenodo.123456\n")
    (citation,) = collect_citations([doc])
    assert citation.in_reference_list is False


@pytest.mark.parametrize(
    "heading",
    ["References", "REFERENCES", "3. References", "Bibliography", "Literature Cited", "\\section{References}", "\\begin{thebibliography}{10}"],
)
def test_reference_list_start_recognises_each_heading_style(heading):
    text = f"Body text.\n\n{heading}\n\n1. An entry.\n"
    assert references._reference_list_start(text) is not None


def test_a_passing_mention_of_references_is_not_the_heading():
    assert references._reference_list_start("We compare our references to theirs.\n") is None


def test_each_reference_entry_is_kept_separately(tmp_path):
    # The second entry reusing a DOI is exactly the error being looked for, so
    # it must not be collapsed into the first.
    doc = _doc(tmp_path, REFS + "1. Turing. doi:10.1093/mind/LIX.236.433\n\n2. Franklin. doi:10.1093/mind/LIX.236.433\n")
    citations = collect_citations([doc])
    assert [c.doi for c in citations] == ["10.1093/mind/lix.236.433"] * 2
    assert "turing" in citations[0].context and "turing" not in citations[1].context


def test_a_body_doi_repeated_is_kept_once(tmp_path):
    doc = _doc(tmp_path, "Data at 10.5281/zenodo.123456 and again at 10.5281/zenodo.123456.\n")
    assert len(collect_citations([doc])) == 1


def test_entry_context_stops_at_the_neighbouring_reference(tmp_path):
    # The neighbouring entry is where a wrongly-pasted DOI came from, so its
    # text must never be what confirms the citation.
    doc = _doc(tmp_path, REFS + "1. Turing, A. M. Computing machinery and intelligence. Mind (1950).\n\n2. Franklin, R. Acta Cryst (1953). doi:10.1093/mind/LIX.236.433\n")
    (citation,) = collect_citations([doc])
    assert "franklin" in citation.context
    assert "turing" not in citation.context
    assert "machinery" not in citation.context


@pytest.mark.parametrize("marker", ["[2]", "2.", "(2)", "\\bibitem{franklin}"])
def test_entry_context_stops_at_each_entry_marker_style(tmp_path, marker):
    doc = _doc(tmp_path, REFS + f"[1] Turing, A. M. Computing machinery and intelligence. Mind (1950).\n{marker} Franklin, R. Acta Cryst (1953). doi:10.1093/mind/LIX.236.433\n")
    (citation,) = collect_citations([doc])
    assert "franklin" in citation.context
    assert "machinery" not in citation.context


def test_entry_context_never_reaches_above_the_reference_heading(tmp_path):
    doc = _doc(tmp_path, "We study computing machinery and intelligence at length.\n\nReferences\n\n1. Franklin. doi:10.1093/mind/LIX.236.433\n")
    (citation,) = collect_citations([doc])
    assert "machinery" not in citation.context


def test_hyphenated_line_break_is_rejoined_in_the_context(tmp_path):
    doc = _doc(tmp_path, REFS + "1. Turing. Computing machinery and intelli-\ngence. Mind (1950). doi:10.1093/mind/LIX.236.433\n")
    (citation,) = collect_citations([doc])
    assert "intelligence" in citation.context


# --- matching a citation against its record --------------------------------


def test_a_reference_matching_its_doi_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_FOUND, _turing()))
    doc = _doc(tmp_path, REFS + "1. Turing, A. M. Computing machinery and intelligence. Mind 59, 433 (1950). doi:10.1093/mind/LIX.236.433\n")
    assert scan_references([doc]) == []


def test_a_reference_matching_only_on_author_is_silent(tmp_path, monkeypatch):
    # Numeric styles (Nature, Science) print no article title at all; the
    # author's name is the only signal there is, and it has to be enough.
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_FOUND, _turing()))
    doc = _doc(tmp_path, REFS + "1. Turing, A. M. Mind 59, 433-460 (1950). doi:10.1093/mind/LIX.236.433\n")
    assert scan_references([doc]) == []


def test_a_reference_matching_neither_title_nor_author_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_FOUND, _turing()))
    doc = _doc(tmp_path, REFS + "1. Franklin, R. Molecular configuration in sodium thymonucleate. Acta Cryst (1953). doi:10.1093/mind/LIX.236.433\n")
    (finding,) = scan_references([doc])
    assert finding.category == "DOI metadata mismatch"
    assert "Computing Machinery and Intelligence" in finding.detail
    assert "Turing" in finding.detail
    assert "1950" in finding.detail


def test_a_doi_outside_the_reference_list_is_not_compared(tmp_path, monkeypatch):
    # A dataset DOI in a data-availability sentence has no bibliographic text
    # beside it, so comparing it would flag every correctly cited dataset.
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_FOUND, _turing()))
    doc = _doc(tmp_path, "Our sequencing data are deposited at https://doi.org/10.5281/zenodo.123456.\n")
    assert scan_references([doc]) == []


def test_a_dead_doi_outside_the_reference_list_is_still_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_MISSING, None))
    doc = _doc(tmp_path, "Our data are deposited at https://doi.org/10.5281/zenodo.123456.\n")
    (finding,) = scan_references([doc])
    assert finding.category == "unresolvable DOI"
    assert "in the manuscript text" in finding.detail


def test_a_dead_doi_in_the_reference_list_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_MISSING, None))
    doc = _doc(tmp_path, REFS + "1. Turing. doi:10.1093/mind/LIX.236.433\n")
    (finding,) = scan_references([doc])
    assert "in the reference list" in finding.detail


def test_a_record_with_nothing_to_match_on_stays_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_FOUND, {}))
    doc = _doc(tmp_path, REFS + "1. Some entry. doi:10.1093/mind/LIX.236.433\n")
    assert scan_references([doc]) == []


def test_a_title_matching_partially_is_enough(tmp_path, monkeypatch):
    # PDF extraction drops and mangles words, so the overlap is a fraction, not
    # an exact match. Two of the three significant words carry it.
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_FOUND, _turing(author=[])))
    doc = _doc(tmp_path, REFS + "1. Computing machinery and intelli. Mind (1950). doi:10.1093/mind/LIX.236.433\n")
    assert scan_references([doc]) == []


def test_two_reference_entries_sharing_a_doi_are_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_UNKNOWN, None))
    doc = _doc(tmp_path, REFS + "1. Turing. doi:10.1093/mind/LIX.236.433\n\n2. Franklin. doi:10.1093/mind/LIX.236.433\n")
    (finding,) = scan_references([doc])
    assert finding.category == "duplicate DOI"
    assert "2 entries" in finding.detail


def test_a_doi_in_both_body_and_references_is_reported_once(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_MISSING, None))
    doc = _doc(tmp_path, "As shown previously (doi:10.1093/mind/LIX.236.433).\n\nReferences\n\n1. Turing. doi:10.1093/mind/LIX.236.433\n")
    (finding,) = scan_references([doc])
    assert "in the reference list" in finding.detail


def test_a_bib_entry_takes_precedence_over_the_same_doi_in_prose(tmp_path, monkeypatch):
    # The field-by-field comparison is the more precise of the two, so the
    # manuscript's copy of the same DOI must not produce a second finding.
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_MISSING, None))
    bib = _bib(tmp_path, "@article{turing1950, title = {T}, doi = {10.1093/mind/LIX.236.433}}\n")
    doc = _doc(tmp_path, REFS + "1. Turing. doi:10.1093/mind/LIX.236.433\n")
    (finding,) = scan_references([bib, doc])
    assert "turing1950" in finding.detail


# --- DOIs broken across a line ---------------------------------------------


def test_a_doi_wrapped_mid_suffix_records_its_continuation(tmp_path):
    doc = _doc(tmp_path, REFS + "1. Wilkinson et al. Sci Data (2016). doi:10.1038/sdata.2016.\n18\n")
    (citation,) = collect_citations([doc])
    assert citation.doi == "10.1038/sdata.2016"
    # Rebuilt from the raw text, so the '.' the break fell after is kept.
    assert citation.continuation == "10.1038/sdata.2016.18"


def test_a_doi_wrapped_at_the_slash_is_rebuilt_whole(tmp_path):
    doc = _doc(tmp_path, REFS + "1. Watson & Crick. Nature (1953). https://doi.org/10.1038/\n171737a0\n")
    (citation,) = collect_citations([doc])
    assert citation.doi == "10.1038/171737a0"


def test_a_wrapped_doi_is_not_reported_dead_when_its_whole_form_resolves(tmp_path, monkeypatch):
    record = {"title": "The FAIR Guiding Principles", "author": [{"family": "Wilkinson"}]}

    def resolve(doi, **kwargs):
        return (DOI_FOUND, record) if doi == "10.1038/sdata.2016.18" else (DOI_MISSING, None)

    monkeypatch.setattr(references, "_resolve_doi", resolve)
    doc = _doc(tmp_path, REFS + "1. Wilkinson et al. Sci Data (2016). doi:10.1038/sdata.2016.\n18\n")
    assert scan_references([doc]) == []


def test_a_truly_dead_doi_is_still_reported_when_the_rebuild_also_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "_resolve_doi", lambda doi, **k: (DOI_MISSING, None))
    doc = _doc(tmp_path, REFS + "1. Nobody. doi:10.1038/sdata.2016.\n18\n")
    (finding,) = scan_references([doc])
    assert finding.category == "unresolvable DOI"
    # The form the author wrote is the one reported, not the speculative rebuild.
    assert "DOI 10.1038/sdata.2016," in finding.detail


def test_the_rebuild_is_only_tried_when_the_plain_form_is_missing(monkeypatch):
    calls = []

    def resolve(doi, **kwargs):
        calls.append(doi)
        return DOI_FOUND, {}

    monkeypatch.setattr(references, "_resolve_doi", resolve)
    references._resolve_with_fallback(("10.1/x", "10.1/xy"))
    assert calls == ["10.1/x"]


# --- comparison tolerance --------------------------------------------------


@pytest.mark.parametrize(
    "entry_title,registered_title",
    [
        ("The {C. elegans} Genome", "The C. elegans genome"),
        ("A Study of \\emph{Drosophila} Wings", "A study of Drosophila wings"),
        ("Gr{\\\"u}nbaum's Conjecture", "Grunbaum's conjecture"),
        ("Deep learning: a review", "Deep Learning -- A Review"),
    ],
)
def test_markup_and_punctuation_differences_do_not_count(entry_title, registered_title):
    entry = _entry(title=entry_title, doi="10.1/x")
    assert references._mismatches(entry, {"title": registered_title}) == []


def test_a_year_off_by_one_is_tolerated():
    # Online-first publication routinely straddles a year boundary.
    entry = _entry(year="2019", doi="10.1/x")
    assert references._mismatches(entry, _record(title="", year=2020)) == []


def test_a_year_off_by_two_is_reported():
    entry = _entry(year="2018", doi="10.1/x")
    (problem,) = references._mismatches(entry, _record(title="", year=2020))
    assert "year is 2018" in problem


def test_a_blank_field_is_not_compared():
    # An entry missing its title/author/year is incomplete, not wrong.
    entry = _entry(doi="10.1/x")
    assert references._mismatches(entry, _record()) == []


@pytest.mark.parametrize(
    "author_field,expected",
    [
        ("Lovelace, Ada", "Lovelace"),
        ("Ada Lovelace", "Lovelace"),
        ("Ada Lovelace and Charles Babbage", "Lovelace"),
        ("Lovelace, Ada and Babbage, Charles", "Lovelace"),
        ("Ludwig van Beethoven", "Beethoven"),
        ("{The LIGO Collaboration}", "Collaboration"),
        ("", ""),
    ],
)
def test_first_author_family_name_read_from_either_name_order(author_field, expected):
    assert references._entry_first_author(author_field) == expected


def test_accented_author_names_compare_equal():
    entry = _entry(author='Gr{\\"u}nbaum, Branko', doi="10.1/x")
    assert references._mismatches(entry, _record(title="", family="Gr\u00fcnbaum")) == []


# --- reading a CSL JSON record ---------------------------------------------


def test_title_given_as_a_list_is_read():
    assert references._title_of({"title": ["Computing Machinery and Intelligence"]}) == "Computing Machinery and Intelligence"


def test_year_falls_back_to_the_online_publication_date():
    assert references._year_of({"published-online": {"date-parts": [[2021, 4]]}}) == "2021"


def test_organization_author_read_from_a_literal_name():
    assert references._first_author_of({"author": [{"literal": "The LIGO Collaboration"}]}) == "The LIGO Collaboration"


def test_record_without_author_or_date_yields_blanks():
    assert references._first_author_of({}) == ""
    assert references._year_of({}) == ""


# --- resolution transport --------------------------------------------------


def test_resolve_doi_returns_missing_only_for_404(monkeypatch):
    import urllib.error
    import urllib.request

    def raise_http(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", raise_http)
    assert _real_resolve_doi("10.1234/abc") == (DOI_MISSING, None)


def test_resolve_doi_treats_other_errors_as_unknown(monkeypatch):
    import urllib.error
    import urllib.request

    def raise_http(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", raise_http)
    assert _real_resolve_doi("10.1234/abc") == (DOI_UNKNOWN, None)


def test_resolve_doi_treats_a_transport_failure_as_unknown(monkeypatch):
    import urllib.request

    def boom(req, timeout=None):
        raise OSError("network is unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert _real_resolve_doi("10.1234/abc") == (DOI_UNKNOWN, None)


def test_resolve_doi_returns_the_parsed_record_and_asks_for_csl_json(monkeypatch):
    import contextlib
    import urllib.request

    seen = {}

    @contextlib.contextmanager
    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["accept"] = req.get_header("Accept")

        class _Resp:
            def read(self, _n=None):
                return b'{"title": "A Paper"}'

        yield _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    status, record = _real_resolve_doi("10.1234/abc")
    assert (status, record) == (DOI_FOUND, {"title": "A Paper"})
    assert seen["url"] == "https://doi.org/10.1234/abc"
    assert seen["accept"] == "application/vnd.citationstyles.csl+json"


def test_resolve_doi_treats_non_json_as_unknown(monkeypatch):
    import contextlib
    import urllib.request

    @contextlib.contextmanager
    def fake_urlopen(req, timeout=None):
        class _Resp:
            def read(self, _n=None):
                return b"<html>a landing page</html>"

        yield _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _real_resolve_doi("10.1234/abc") == (DOI_UNKNOWN, None)
