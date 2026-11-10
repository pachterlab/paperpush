"""Regression tests for the reference and citation checks of ``paperpush validate``.

Every registry is mocked, so these run offline.
"""

from __future__ import annotations

import zipfile
from datetime import date
from types import SimpleNamespace

import pytest

import paperpush.bibliography_check as bibliography
import paperpush.citation_check as citations
import paperpush.feature_check as feature
import paperpush.reference_review as review
import paperpush.reference_search as search
import paperpush.references as refs
from paperpush import report
from paperpush.validate import Checks, _reference_issues, _retraction_issues, _unmatched_reference_issues, _ReferenceContext

VENUE = SimpleNamespace(slug="zz_none", name="None", fields=[])
FIELD = SimpleNamespace(id="manuscript_file")


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _record(title="The same title of a paper here", family="Smith", year=2020, **extra):
    return {"title": title, "author": [{"family": family}], "issued": {"date-parts": [[year]]}, **extra}


def _entry(title="The same title of a paper here", author="Smith, J", year="2020", **kwargs):
    return refs.Entry(key="k", where="x.bib", raw_doi="10.1234/a", doi="10.1234/a", title=title, author=author, year=year, **kwargs)


def _uploads(monkeypatch, *paths):
    validate = __import__("sys").modules["paperpush.validate"]
    monkeypatch.setattr(validate, "_upload_paths_or_skip", lambda venue, values: list(paths))


def _uncited(monkeypatch, path):
    monkeypatch.setattr(bibliography, "_manuscripts", lambda venue, values: [(FIELD, path)])
    return Checks().run("bibliography.uncited", "t", "R", bibliography._uncited_check, VENUE, {})


# --- 1. a registry that does not answer is not a pass ---------------------------------


def test_unreachable_registry_is_unchecked_not_passed(tmp_path, monkeypatch):
    bib = _write(tmp_path, "a.bib", "@article{x,title={A real title about things},author={Smith, J},year={2020},doi={10.1038/abc}}\n")
    _uploads(monkeypatch, bib)
    monkeypatch.setattr(refs, "_resolve_doi", lambda doi, **k: (refs.DOI_UNKNOWN, None))
    checks, retracted, coverage = Checks(), [], {}
    dois = checks.run("integrity.dois", "t", "I", _reference_issues, VENUE, {}, retracted, {}, coverage)
    assert dois.status == report.UNCHECKED
    assert "not checked" in dois.detail
    gone = checks.run("integrity.retractions", "t", "I", _retraction_issues, VENUE, {}, None if dois.status == report.UNCHECKED else retracted, coverage)
    assert gone.status == report.UNCHECKED


def test_partly_answered_and_over_budget_dois_are_noted_in_both_checks(tmp_path, monkeypatch):
    bib = _write(tmp_path, "l.bib", "".join(f"@article{{k{i},title={{Paper number {i} about something}},author={{Smith, J}},year=2020,doi={{10.1234/n{i}}}}}\n" for i in range(130)))
    _uploads(monkeypatch, bib)

    monkeypatch.setattr(refs, "_resolve_doi", lambda doi, **k: (refs.DOI_FOUND, _record(f"Paper number {doi.rsplit('n', 1)[1]} about something")))
    checks, retracted, coverage = Checks(), [], {}
    dois = checks.run("integrity.dois", "t", "I", _reference_issues, VENUE, {}, retracted, {}, coverage)
    assert coverage == {"total": 130, "unknown": 0, "unbudgeted": 30}
    assert [i.unchecked for i in dois.issues] == [True]
    assert "30 of 130" in dois.issues[0].message
    gone = checks.run("integrity.retractions", "t", "I", _retraction_issues, VENUE, {}, retracted, coverage)
    assert gone.status != report.PASSED
    assert "30 of 130" in gone.issues[0].message


# --- 2. DOIs with parentheses --------------------------------------------------------


def test_doi_with_parentheses_is_read_whole(tmp_path):
    text = _write(tmp_path, "b.txt", "Text.\n\nReferences\n\n1. Zhu N. A novel coronavirus. Lancet 395 (2020). doi:10.1016/S0140-6736(20)30183-5\n2. Foo B. Title. Cell 2000. (https://doi.org/10.1016/S0092-8674(00)81683-9).\n3. Sici A. Old. 10.1002/(SICI)1096-8644(199601)99:1<1::AID-AJPA1>3.0.CO;2-1.\n")
    assert [c.doi for c in refs.collect_citations([text])] == ["10.1016/s0140-6736(20)30183-5", "10.1016/s0092-8674(00)81683-9", "10.1002/(sici)1096-8644(199601)99:1<1::aid-ajpa1>3.0.co;2-1"]


# --- 3. a miss needs both registries ---------------------------------------------------


def _indexed():
    return search.Reference(label="bibliography entry 'n'", where="x.bib", title="Attention based networks for sequence modeling tasks", author="Vaswani", year="2017", kind="inproceedings")


UNRELATED = {"title": ["Totally unrelated item of work"], "DOI": "10.1234/z", "type": "journal-article"}


def test_crossref_miss_with_openalex_down_is_not_an_accusation(monkeypatch):
    svc = search._services()
    svc.openalex.down = True
    monkeypatch.setattr(search, "crossref_search", lambda q, s: [UNRELATED])
    result = search.search_references([_indexed()], services=svc)
    assert [lk.status for lk in result.lookups] == [search.UNKNOWN]
    assert result.findings == []


def test_openalex_miss_with_crossref_down_is_not_an_accusation(monkeypatch):
    monkeypatch.setattr(search, "crossref_search", lambda q, s: None)
    monkeypatch.setattr(search, "openalex_search", lambda t, s: [])
    result = search.search_references([_indexed()], services=search._services())
    assert [lk.status for lk in result.lookups] == [search.UNKNOWN]
    assert result.findings == []


# --- 4. retracted works ------------------------------------------------------------------

TITLE = "Ileal-lymphoid-nodular hyperplasia, non-specific colitis, and pervasive developmental disorder in children"
RETRACTED = {"title": ["RETRACTED: " + TITLE], "DOI": "10.1016/S0140-6736(97)11096-0", "type": "journal-article", "author": [{"family": "Wakefield"}], "issued": {"date-parts": [[1998]]}, "updated-by": [{"type": "retraction"}]}


def _wakefield():
    return search.Reference(label="bibliography entry 'w'", where="x.bib", title=TITLE, author="Wakefield", year="1998", kind="article")


def test_retracted_title_is_found_and_reported_as_retracted(monkeypatch):
    monkeypatch.setattr(search, "crossref_search", lambda q, s: [RETRACTED])
    result = search.search_references([_wakefield()], services=search._services())
    assert [lk.status for lk in result.lookups] == [search.MATCHED]
    assert [f.category for f in result.findings] == [refs.RETRACTED]


def test_the_retraction_notice_is_not_the_work():
    notice = dict(RETRACTED, **{"title": ["Retracted: " + TITLE], "update-to": [{"type": "retraction", "DOI": "10.1234/x"}]})
    del notice["updated-by"]
    assert search.match_entry(_wakefield(), [notice]).status == search.NOT_FOUND


def test_short_retracted_title_stays_a_retraction():
    record = _record("RETRACTED: Stem cells in mice", **{"updated-by": [{"type": "retraction", "DOI": "10.1234/n"}]})
    found = refs._entry_findings(_entry("Stem cells in mice"), refs.DOI_FOUND, record)
    assert [f.category for f in found] == [refs.RETRACTED]


def test_a_retraction_found_by_search_moves_to_the_retraction_check(tmp_path, monkeypatch):
    bib = _write(tmp_path, "w.bib", "@article{w,title={" + TITLE + "},author={Wakefield, A},year=1998}\n")
    _uploads(monkeypatch, bib)
    monkeypatch.setattr(search, "crossref_search", lambda q, s: [RETRACTED])
    monkeypatch.setattr("paperpush.requirements_check.manuscript_files", lambda venue, values: [])
    retracted, ctx = [], _ReferenceContext()
    outcome = _unmatched_reference_issues(VENUE, {}, ctx, retracted)
    assert [f.category for f in retracted] == [refs.RETRACTED]
    assert not any("retracted" in i.message for i in outcome.issues)


# --- 5. arXiv's silence ---------------------------------------------------------------------

_EMPTY = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title></feed>'


def test_old_style_arxiv_id_drops_its_subject_class():
    entry = refs.parse_bibtex("@misc{p,title={Entropy},eprint={math.DG/0211159},archivePrefix={arXiv}}")[0]
    assert search.arxiv_id_of(entry) == ("math/0211159", None)


def test_empty_arxiv_batch_is_unknown_not_an_accusation():
    svc = search._services()
    calls = []
    svc.arxiv.get = lambda url, accept=None: calls.append(url) or _EMPTY
    two = [search.Reference(label=f"entry {i}", where="x.bib", title="Attention is all you need", arxiv_id=f"1706.0376{i}", kind="misc") for i in range(2)]
    result = search.search_references(two, services=svc)
    assert len(calls) == 2
    assert [lk.status for lk in result.lookups] == [search.UNKNOWN, search.UNKNOWN]
    assert result.findings == []


# --- 6. false mismatches -------------------------------------------------------------------


@pytest.mark.parametrize(
    "author, record",
    [
        ("Mart{\\'\\i}nez, Ana", _record(family="Martínez")),
        ("Mart{\\'{\\i}}nez, Ana", _record(family="Martínez")),
        ("{\\AA}berg, K", _record(family="Åberg")),
        ("Johannes van der Waals", _record(family="van der Waals")),
        ("van der Waals, J", _record(family="Waals")),
        ("{ENCODE Project Consortium}", {"title": "The same title of a paper here", "author": [{"name": "ENCODE Project Consortium"}]}),
        ("Smith J and Lee K", _record()),
        ("van der Waals JD", _record(family="van der Waals")),
    ],
)
def test_same_author_written_differently_is_no_mismatch(author, record):
    assert refs._mismatches(_entry(author=author), record) == []


def test_a_different_author_is_still_a_mismatch():
    assert "first author" in refs._mismatches(_entry(author="Jones, J"), _record())[0]


@pytest.mark.parametrize(
    "title, registered",
    [
        ("Deep learning for genomics", "Deep learning for genomics: a comprehensive survey of methods and applications"),
        ("CRISPR Cas9 in E. coli", "<i>CRISPR</i>-<i>Cas9</i> in <i>E. coli</i>"),
        ("Search for $H\\to\\gamma\\gamma$", 'Search for <mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML"><mml:mi>H</mml:mi></mml:math>'),
    ],
)
def test_subtitle_and_markup_are_no_mismatch(title, registered):
    assert refs._mismatches(_entry(title=title), _record(registered)) == []


def test_string_macros_and_concatenation_are_expanded():
    text = '@string{pre = "A long descriptive"}\n@string{auth = "Smith, John"}\n@article{c, title = pre # " title of a paper", author=auth, year=2020, doi={10.1234/a}}\n'
    (entry,) = refs.parse_bibtex(text)
    assert entry.title == "A long descriptive title of a paper"
    assert refs._mismatches(entry, _record("A long descriptive title of a paper")) == []


def test_undefined_macro_is_not_compared():
    (entry,) = refs.parse_bibtex("@article{c, title = elsewhere, author = alsoelsewhere, year = 2020, doi = {10.1234/a}}")
    assert refs._mismatches(entry, _record()) == []


@pytest.mark.parametrize("family, context", [("O'Neil", "o neil j science 12 1 2020"), ("van der Waals", "van der waals j phys rev 12 1"), ("Smith-Jones", "smith jones a nat 1 2 2020")])
def test_multiword_family_name_supports_a_titleless_reference(family, context):
    citation = refs.Citation("10.1234/a", "w", context, True)
    assert refs._context_supports(citation, {"title": "An unrelated long title about many different things", "author": [{"family": family}]})


# --- 7. offline DOI false positives ---------------------------------------------------------


@pytest.mark.parametrize("text", ["[1] Smith, J. Title. Nature (2020). doi:10.1038/abc. https://doi.org/10.1038/abc\n"])
def test_doi_printed_twice_in_one_entry_is_not_a_duplicate(tmp_path, text):
    path = _write(tmp_path, "g.txt", "Body\n\nReferences\n\n" + text)
    assert refs.scan_references([path], check_registry=False) == []


def test_doi_in_two_entries_is_still_a_duplicate(tmp_path):
    path = _write(tmp_path, "g.txt", "Body\n\nReferences\n\n[1] Smith, J. Title. doi:10.1038/abc\n[2] Lee, K. Other. doi:10.1038/abc\n")
    assert [f.category for f in refs.scan_references([path], check_registry=False)] == ["duplicate DOI"]


@pytest.mark.parametrize("raw", ["https://www.doi.org/10.1038/abc", "dx.doi.org/10.1038/abc", "DOI 10.1038/abc", "doi:https://doi.org/10.1038/abc", "\\url{https://doi.org/10.1038/abc}", "http://doi.org/10.1038/ABC."])
def test_doi_prefixes_are_removed(raw):
    assert refs.normalize_doi(raw) == "10.1038/abc"


# --- 8. entries BibTeX never prints ----------------------------------------------------------


def _shared_bib(tmp_path, cite):
    tex = _write(tmp_path, "m.tex", "\\documentclass{article}\\begin{document}" + cite + "\\bibliography{refs}\\end{document}")
    bib = _write(tmp_path, "refs.bib", "@article{good,title={A good paper title here},author={Smith, J},year=2020,doi={10.1234/good}}\n@article{unused,title={A retracted paper title here},author={Liar, J},year=2020,doi={10.1234/bad}}\n")
    return tex, bib


def _answers(doi, **kwargs):
    bad = "bad" in doi
    extra = {"updated-by": [{"type": "retraction", "DOI": "10.1234/n"}]} if bad else {}
    return refs.DOI_FOUND, _record("A retracted paper title here" if bad else "A good paper title here", "Liar" if bad else "Smith", **extra)


def test_uncited_bib_entry_is_not_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(refs, "_resolve_doi", _answers)
    assert refs.scan_references(list(_shared_bib(tmp_path, "See \\cite{good}."))) == []


def test_nocite_star_keeps_every_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(refs, "_resolve_doi", _answers)
    found = refs.scan_references(list(_shared_bib(tmp_path, "See \\cite{good}\\nocite{*}.")))
    assert [f.category for f in found] == [refs.RETRACTED]


# --- 9. eprint beside the journal DOI ---------------------------------------------------------


def test_journal_doi_and_eprint_on_one_entry_is_one_work():
    doi = "10.1103/physrevlett.1.1"
    ref = search.Reference(label="bibliography entry 'ads'", where="x.bib", title="T", doi=doi, arxiv_id="1602.03837", kind="article", venue="PRL")
    records = {"1602.03837": {"journal_doi": doi, "latest_version": 2, "journal_ref": "PRL", "title": "T"}}
    assert search.version_findings([], [ref], {doi}, services=search._services(), arxiv_records=records, arxiv_checked=["1602.03837"]) == []


def test_preprint_only_entry_is_still_told_to_cite_the_article():
    ref = search.Reference(label="bibliography entry 'p'", where="x.bib", title="T", arxiv_id="1602.03837", kind="misc")
    records = {"1602.03837": {"journal_doi": "10.1103/x", "latest_version": 2, "journal_ref": "", "title": "T"}}
    found = search.version_findings([], [ref], set(), services=search._services(), arxiv_records=records, arxiv_checked=["1602.03837"])
    assert [f.category for f in found] == ["preprint has been published"]


# --- 10. brackets that are not citations -----------------------------------------------------

_FOUR = "".join(f"[{i}] Smith, A. Paper {i} title goes here. Nature {i}, 1 (2020).\n" for i in range(1, 5))


@pytest.mark.parametrize("sentence", ["We normalise values to the interval [0, 1].", "BMI in the range [18-25] was used."])
def test_intervals_are_not_citations(tmp_path, monkeypatch, sentence):
    result = _uncited(monkeypatch, _write(tmp_path, "m.txt", f"Body cites [1-4]. {sentence}\n\nReferences\n\n{_FOUR}"))
    assert result.status == report.PASSED


def test_superscript_paper_with_an_interval_is_unchecked(tmp_path, monkeypatch):
    numbered = "".join(f"{i}. Smith, A. Paper {i} title goes here. Nature {i}, 1 (2020).\n" for i in range(1, 7))
    result = _uncited(monkeypatch, _write(tmp_path, "m.txt", f"Superscript text1,2 and more3-6. Values in [0, 1] only.\n\nReferences\n\n{numbered}"))
    assert result.status == report.UNCHECKED


def test_semicolon_separated_citations_are_read(tmp_path, monkeypatch):
    assert _uncited(monkeypatch, _write(tmp_path, "m.txt", f"Body [1; 2] and [3] [4].\n\nReferences\n\n{_FOUR}")).status == report.PASSED


def test_lone_citation_past_the_list_is_still_reported(tmp_path, monkeypatch):
    result = _uncited(monkeypatch, _write(tmp_path, "m.txt", f"Body [1-4] and [45].\n\nReferences\n\n{_FOUR}"))
    assert "[45]" in result.issues[0].message


# --- 11. author-year citations ---------------------------------------------------------------


def test_author_year_citation_can_wrap_a_line(tmp_path, monkeypatch):
    text = "We follow Smith et al.\n(2020) and Jones\n(2019) here.\n\nReferences\n\nSmith, A., Jones, B. (2020). A long enough title of paper. Nature, 1, 1-2.\nJones, C. (2019). Another long title goes here for this. Cell, 1, 1-2.\n"
    assert _uncited(monkeypatch, _write(tmp_path, "m.txt", text)).status == report.PASSED


def test_accented_first_surname_is_the_entrys_surname(tmp_path, monkeypatch):
    text = "We follow (Müller et al., 2020) and (García, 2019).\n\nReferences\n\nMüller, A., Schmidt, B. (2020). A long enough title of paper. Nature, 1, 1-2.\nGarcía, C., Lopez, D. (2019). Another long title goes here for this. Cell, 1, 1-2.\n"
    assert _uncited(monkeypatch, _write(tmp_path, "m.txt", text)).status == report.PASSED


# --- 12. figure lists and panels --------------------------------------------------------------


def _problems(text, series="figure"):
    return citations.order_problems(citations.mentions(text), series)[0]


@pytest.mark.parametrize("sentence", ["We see this (Figs. 1a-c and 2).", "Results in Figs. 1a,b and 2."])
def test_item_after_lettered_panels_is_cited(sentence):
    assert _problems(f"{sentence} Done.\nFigure 1. Cap.\nFigure 2. Cap.\n") == []


def test_count_after_a_figure_is_not_a_figure():
    assert _problems("As in Figure 1, 15 patients were enrolled. See Figure 2.\nFigure 1. Cap.\nFigure 2. Cap.\n") == []


def test_plural_list_keeps_its_last_item():
    assert _problems("Figures 1, 2 show this.\nFigure 1. Cap.\nFigure 2. Cap.\n") == []


@pytest.mark.parametrize("sentence", ["Reported before (Fig. 3 of ref. 12).", "As in Figure 5 of Smith et al. (2019)."])
def test_another_papers_figure_is_not_cited_here(sentence):
    assert _problems(f"{sentence} Ours are in Fig. 1 and Fig. 2.\nFigure 1. Cap.\nFigure 2. Cap.\n") == []


def test_count_after_in_is_not_a_reference():
    assert [m.number for m in citations.mentions("See Table 2 in 12 patients.") if not m.caption] == [2]


def test_panel_list_still_reads_forward_letters():
    assert [c.letter for c in citations.panel_mentions("See Fig. 2a, b, d.")] == ["a", "b", "d"]


def test_word_after_panel_is_dropped():
    assert [c.letter for c in citations.panel_mentions("(Fig. 2c, a marked increase)")] == ["c"]
    assert [c.letter for c in citations.panel_mentions("In Fig. 1b, B cells expand.")] == ["b"]


# --- 13. captions ---------------------------------------------------------------------------------


@pytest.mark.parametrize("caption", ["Table 1\nBaseline characteristics of patients\n", "Table 1 Baseline characteristics of patients\n"])
def test_captions_without_punctuation_are_captions(caption):
    found = citations.mentions("Text with no citation at all of it.\n" + caption)
    assert [(m.number, m.caption) for m in found] == [(1, True)]
    assert "never cited" in citations.order_problems(found, "table")[0][0]


def test_wrapped_sentence_in_a_two_column_page_is_not_a_caption():
    text = "Body of the column text as seen in the results\nFig. 2. We continue on from there with\nthe text describing the Fig. 1 result here.\nFigure 1. Cap.\nFigure 2. Cap.\n"
    assert [m.caption for m in citations.mentions(text) if m.number == 2] == [False, True]


# --- 14. numbered reference order ------------------------------------------------------------------


def _order(tmp_path, monkeypatch, text):
    path = _write(tmp_path, "q.txt", text)
    monkeypatch.setattr(feature, "_files", lambda venue, values, role: [path])
    monkeypatch.setattr(feature, "get_requirements", lambda slug: None)
    return Checks().run("details.reference_order", "t", "R", feature._reference_order, VENUE, {}, False, False)


def test_alphabetical_numbered_list_passes_with_no_rule(tmp_path, monkeypatch):
    text = "We cite [3] first and [1] and then [2].\n\nReferences\n\n[1] Adams, A. Paper title one here. Nature 1 (2020).\n[2] Brown, B. Paper title two here. Nature 2 (2020).\n[3] Clark, C. Paper title three here. Nature 3 (2020).\n"
    assert _order(tmp_path, monkeypatch, text).status == report.PASSED


def test_list_in_neither_order_is_reported(tmp_path, monkeypatch):
    text = "We cite [3] first and [1] and then [2].\n\nReferences\n\n[1] Clark, A. Paper title one here. Nature 1 (2020).\n[2] Adams, B. Paper title two here. Nature 2 (2020).\n[3] Brown, C. Paper title three here. Nature 3 (2020).\n"
    assert "first citation" in _order(tmp_path, monkeypatch, text).issues[0].message


# --- 15. reference age ---------------------------------------------------------------------------------


def _age(tmp_path, monkeypatch, text):
    path = _write(tmp_path, "p.txt", text)
    monkeypatch.setattr(review, "_source", lambda venue, values: (FIELD, path, bibliography.read_bibliography(path)))
    return Checks().run("bibliography.age", "t", "R", review._age, VENUE, {})


def test_one_old_reference_is_a_note_not_a_warning(tmp_path, monkeypatch):
    year = date.today().year
    lines = [f"[{i}] Smith, A. Paper {i}. Nature {i}, 1 ({year - 1}).\n" for i in range(1, 100)] + [f"[100] Old, B. Classic paper title here. Nature 1, 1 ({year - 12}).\n"]
    result = _age(tmp_path, monkeypatch, "Body [1-50], [51-100].\n\nReferences\n\n" + "".join(lines))
    assert result.status == report.PASSED
    assert "1 of 100" in result.detail


def test_year_is_not_read_from_a_title_or_page_range(tmp_path, monkeypatch):
    year = date.today().year - 1
    text = f"Body [1-3].\n\nReferences\n\n[1] Smith, A. The 1918 influenza pandemic revisited. Nature 1, 1 ({year}).\n[2] Jones, B. Title of paper. J. Biol. Chem. 275, 2001-2010 ({year}).\n[3] Lee, C. Title. J Biol Chem. {year};275:2001-2010. doi:10.1016/j.cell.2001.01.001\n"
    result = _age(tmp_path, monkeypatch, text)
    assert result.status == report.PASSED
    assert "0 of 3" in result.detail


# --- over the cap ---------------------------------------------------------------------------------------


def test_one_bib_shipped_loose_and_zipped_has_no_duplicate_keys(tmp_path):
    bib = _write(tmp_path, "refs.bib", "@article{a,title={T one},author={S, J},year=2020}\n@article{b,title={T two},author={S, J},year=2020}\n")
    bundle = tmp_path / "source.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.write(bib, "refs.bib")
    findings, total = refs.bib_structure_findings([bib, bundle])
    assert findings == [] and total == 2


def test_two_different_bibs_sharing_a_key_are_still_reported(tmp_path):
    one = _write(tmp_path, "one.bib", "@article{a,title={T one},author={S, J},year=2020}\n")
    two = _write(tmp_path, "two.bib", "@article{a,title={T two},author={S, J},year=2020}\n")
    assert [f.category for f in refs.bib_structure_findings([one, two])[0]] == ["duplicate citation key"]


def test_a_citation_macro_is_read_through(tmp_path, monkeypatch):
    tex = _write(tmp_path, "m.tex", "\\documentclass{article}\\newcommand{\\mycite}[1]{\\cite{#1}}\\begin{document}See \\mycite{a}.\\begin{thebibliography}{9}\n\\bibitem{a} Smith. A title.\n\\end{thebibliography}\\end{document}")
    assert bibliography.read_bibliography(tex).cited == {"a"}
    assert _uncited(monkeypatch, tex).status == report.PASSED


@pytest.mark.parametrize("field, author", [('M{\\"u}ller, Kai and Lee, J', ("Kai Müller", "Muller", "K")), ("{Rich}, Joseph and Lee, J", ("Joseph Rich", "Rich", "J"))])
def test_self_citation_reads_latex_in_author_fields(field, author):
    assert bibliography.is_self_citation(bibliography.Ref("k", "k", field, "2020"), [author])
