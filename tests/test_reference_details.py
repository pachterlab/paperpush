"""The whole-record reference comparison, journal indexing, and the uncited-claims AI task.

Every registry is mocked, so these run offline.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import paperpush.references as refs
from paperpush import reference_details as rd
from paperpush import reference_review, report
from paperpush.database import Field, Venue
from paperpush.validate import Checks, _journal_indexing_issues, _reference_detail_issues, _reference_issues, _ReferenceContext

SCIPY = {
    "type": "journal-article",
    "DOI": "10.1038/s41592-019-0686-2",
    "title": "SciPy 1.0: fundamental algorithms for scientific computing in Python",
    "container-title": "Nature Methods",
    "container-title-short": "Nat Methods",
    "volume": "17",
    "issue": "3",
    "page": "261-272",
    "ISSN": ["1548-7091", "1548-7105"],
    "issued": {"date-parts": [[2020, 2, 3]]},
    "author": [{"given": "Pauli", "family": "Virtanen"}, {"given": "Ralf", "family": "Gommers"}, {"given": "Travis E.", "family": "Oliphant"}],
}
TITLE = "SciPy 1.0: fundamental algorithms for scientific computing in Python"


def _text(text: str, record=SCIPY, doi="10.1038/s41592-019-0686-2"):
    return rd.detail_findings([rd.Matched("reference [1]", "paper.pdf", record, text=text, doi=doi)])


def _bib(bibtex: str, record=SCIPY):
    entry = refs.parse_bibtex(bibtex, "refs.bib")[0]
    return rd.detail_findings([rd.Matched(f"bibliography entry '{entry.key}'", "refs.bib", record, entry=entry, doi="10.1038/s41592-019-0686-2")])


# --- the same reference in every common style matches -------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"[1] Virtanen P, Gommers R, Oliphant TE. {TITLE}. Nat Methods. 2020;17(3):261-72. doi:10.1038/s41592-019-0686-2",
        f"Virtanen, P., Gommers, R., & Oliphant, T. E. (2020). {TITLE}. Nature Methods, 17(3), 261–272.",
        f"1. Virtanen, P., Gommers, R. & Oliphant, T. E. {TITLE}. Nature Methods 17, 261–272 (2020).",
        f"Pauli Virtanen, Ralf Gommers, and Travis E. Oliphant. {TITLE}. Nature Methods, 17(3):261–272, 2020.",
        f"P. Virtanen, R. Gommers, and T. E. Oliphant, “{TITLE},” Nat. Methods, vol. 17, no. 3, pp. 261–272, 2020.",
        f"Virtanen, P. et al. {TITLE}. Nat. Methods 17, 261–272 (2020).",
    ],
)
def test_a_correct_reference_in_any_style_passes(text):
    assert _text(text) == []


def test_a_correct_bib_entry_passes():
    assert _bib("@article{a, author={Virtanen, Pauli and Gommers, Ralf and Oliphant, Travis E.}, title={SciPy 1.0}, journal={Nat. Methods}, volume={17}, number={3}, pages={261--272}, year={2020}}") == []


@pytest.mark.parametrize("cited", ["PNAS", "Proc Natl Acad Sci USA", "Proc. Natl Acad. Sci. U.S.A.", "Proceedings of the National Academy of Sciences of the United States of America"])
def test_journal_abbreviations_and_acronyms_match(cited):
    pnas = {"container-title": "Proceedings of the National Academy of Sciences", "container-title-short": "Proc. Natl. Acad. Sci. U.S.A."}
    assert rd.same_journal(cited, pnas) is True


@pytest.mark.parametrize("cited,registered", [("J. Am. Chem. Soc.", "Journal of the American Chemical Society"), ("N Engl J Med", "New England Journal of Medicine"), ("Lancet", "The Lancet"), ("Bioinformatics", "Bioinformatics (Oxford, England)"), ("PLoS Comput Biol", "PLOS Computational Biology")])
def test_common_abbreviations_match(cited, registered):
    assert rd.same_journal(cited, {"container-title": registered}) is True


# --- what is wrong is reported -------------------------------------------------------------


def test_wrong_journal_name_is_reported():
    # Paperpal's demo reference 3: a real DOI, but the entry names another journal.
    found = _text(f"Virtanen, P., Gommers, R., & Oliphant, T. E. (2020). {TITLE}. Frontiers in Bioscience and Biomedical Sciences, 17(3), 261–272.")
    assert [f.category for f in found] == [rd.DETAILS]
    assert "does not name the work's journal, Nature Methods" in found[0].detail
    assert found[0].level == "warning"


def test_wrong_volume_issue_and_pages_are_reported():
    found = _text(f"Virtanen, P., Gommers, R., & Oliphant, T. E. (2020). {TITLE}. Nature Methods, 18(4), 300–310.")
    detail = found[0].detail
    assert "volume is 18 but the work is in volume 17" in detail
    assert "issue is 4 but the work is in issue 3" in detail
    assert "pages start at 300 but the work is on pages 261-272" in detail


def test_wrong_year_with_a_doi_is_reported():
    found = _text(f"Virtanen, P., Gommers, R., & Oliphant, T. E. (2016). {TITLE}. Nature Methods, 17(3), 261–272.")
    assert "year is 2016 but the work is dated 2020" in found[0].detail


def test_a_whole_wrong_author_list_with_the_right_doi_is_a_likely_error():
    # Paperpal's demo reference 2: the DOI and title are right, the authors are someone else's.
    found = _text(f"Smith, A., Jones, B., & Brown, C. (2020). {TITLE}. Nature Methods, 17(3), 261–272.")
    assert [f.category for f in found] == [rd.WRONG_AUTHORS]
    assert found[0].level == "likely_error"


def test_wrong_author_list_sharing_one_coauthors_name_is_still_a_likely_error():
    # "Smith" is one of the work's later authors, but the list is not the work's.
    record = {**SCIPY, "author": [{"given": g, "family": f} for g, f in (("Pauli", "Virtanen"), ("Ralf", "Gommers"), ("Travis E.", "Oliphant"), ("Matt", "Haberland"), ("Nathaniel J.", "Smith"))]}
    found = _text(f"Smith, A., Jones, B., & Brown, C. (2020). {TITLE}. Nature Methods, 17(3), 261–272.", record=record)
    assert [(f.category, f.level) for f in found] == [(rd.WRONG_AUTHORS, "likely_error")]


def test_journal_given_as_the_author_is_incomplete():
    # Paperpal's demo reference 5.
    found = _text(f"Nature Methods. (2020). {TITLE}. Nature Methods, 17(3), 261–272.")
    assert [f.category for f in found] == [rd.INCOMPLETE]
    assert "its author is the journal (Nature Methods)" in found[0].detail
    assert "Pauli Virtanen" in found[0].detail


def test_missing_volume_and_pages_are_a_suggestion():
    found = _text(f"Virtanen, P., Gommers, R., & Oliphant, T. E. (2020). {TITLE}. Nature Methods.")
    assert [(f.category, f.level) for f in found] == [(rd.MISSING_NUMBERS, "suggestion")]
    assert "volume 17, pages 261-272" in found[0].detail


def test_book_chapter_without_its_book_or_editors():
    # Paperpal's demo reference 10.
    chapter = {
        "type": "book-chapter",
        "title": "Artificial intelligence in higher education",
        "container-title": "Handbook of Educational Technology",
        "author": [{"given": "A", "family": "Pahuja"}],
        "editor": [{"given": "Maria", "family": "Lopez"}],
        "issued": {"date-parts": [[2024]]},
    }
    found = _text("Pahuja, A. (2024). Artificial intelligence in higher education. Springer.", record=chapter)
    by = {f.category: f for f in found}
    assert "does not name the book, Handbook of Educational Technology" in by[rd.INCOMPLETE].detail
    assert "editors Maria Lopez" in by[rd.MISSING_NUMBERS].detail
    assert _text("Pahuja, A. (2024). Artificial intelligence in higher education. In M. Lopez (Ed.), Handbook of Educational Technology (pp. 1-20). Springer.", record=chapter) == []


def test_bib_entry_field_by_field():
    found = _bib("@article{b, author={Virtanen, Pauli and Oliphant, Travis E. and Gommers, Ralf}, title={SciPy 1.0}, journal={Nature Biotechnology}, volume={18}, pages={1--2}, year={2020}}")
    detail = found[0].detail
    assert "journal is Nature Biotechnology but the work was published in Nature Methods" in detail
    assert "volume is 18" in detail and "pages start at 1" in detail
    assert "different order" in detail


def test_bib_entry_missing_and_extra_coauthors():
    found = _bib("@article{c, author={Virtanen, Pauli and Smith, John}, title={SciPy 1.0}, journal={Nature Methods}, volume={17}, pages={261--272}, year={2020}}")
    detail = found[0].detail
    assert "it lists Smith, John, who is not among the work's authors" in detail
    assert "leaves out registered authors Ralf Gommers, Travis E. Oliphant" in detail


def test_bib_and_others_is_not_missing_authors():
    assert _bib("@article{d, author={Virtanen, Pauli and others}, title={SciPy 1.0}, journal={Nature methods}, volume={17}, pages={261--272}, year={2020}}") == []


def test_bib_article_without_journal():
    found = _bib("@article{e, author={Virtanen, Pauli and Gommers, Ralf and Oliphant, Travis E.}, title={SciPy 1.0}, year={2020}}")
    assert any(f.category == rd.INCOMPLETE and "names no journal" in f.detail for f in found)


def test_a_preprint_entry_matched_to_its_journal_version_is_not_a_wrong_journal():
    # Without a DOI the search may match the journal version of a cited preprint; the version check covers that.
    found = rd.detail_findings([rd.Matched("reference [1]", "p.pdf", SCIPY, text=f"Virtanen, P. et al. {TITLE}. arXiv preprint arXiv:1907.10121 (2019).", check_authors=False)])
    assert found == []


# --- through validate -----------------------------------------------------------------------

VENUE = SimpleNamespace(slug="zz_none", name="None", fields=[])


def test_detail_check_reads_the_records_the_doi_check_fetched(tmp_path, monkeypatch):
    bib = tmp_path / "r.bib"
    bib.write_text("@article{x, author={Virtanen, Pauli and Gommers, Ralf and Oliphant, Travis E.}, title={" + TITLE + "}, journal={Nature Biotechnology}, volume={17}, pages={261--272}, year={2020}, doi={10.1038/s41592-019-0686-2}}\n", encoding="utf-8")
    validate = __import__("sys").modules["paperpush.validate"]
    monkeypatch.setattr(validate, "_upload_paths_or_skip", lambda venue, values: [bib])
    monkeypatch.setattr(refs, "_resolve_doi", lambda doi, **k: (refs.DOI_FOUND, SCIPY))
    ctx = _ReferenceContext()
    checks = Checks()
    checks.run("integrity.dois", "t", "I", _reference_issues, VENUE, {}, [], ctx.resolved, ctx.coverage, ctx.flagged)
    result = checks.run("integrity.reference_details", "t", "I", _reference_detail_issues, VENUE, {}, ctx)
    assert result.status == report.WARNING
    assert "journal is Nature Biotechnology" in result.issues[0].message
    assert "compared 1 reference" in result.detail


def test_detail_check_with_nothing_matched_is_not_applicable(tmp_path, monkeypatch):
    bib = tmp_path / "r.bib"
    bib.write_text("@article{x, author={Smith, J}, title={A paper}, year={2020}}\n", encoding="utf-8")
    validate = __import__("sys").modules["paperpush.validate"]
    monkeypatch.setattr(validate, "_upload_paths_or_skip", lambda venue, values: [bib])
    result = Checks().run("integrity.reference_details", "t", "I", _reference_detail_issues, VENUE, {}, _ReferenceContext())
    assert result.status == report.NOT_APPLICABLE


# --- journal indexing -----------------------------------------------------------------------


class _FakeService:
    def __init__(self, answers):
        self.answers = answers
        self.urls = []

    def get_json(self, url):
        self.urls.append(url)
        return self.answers.pop(0) if self.answers else None


def test_journal_indexing_flags_journals_in_no_index():
    matched = [
        rd.Matched("reference [1]", "p.pdf", SCIPY),
        rd.Matched("reference [2]", "p.pdf", {**SCIPY, "container-title": "International Journal of Pedagogics", "ISSN": ["2181-1717"]}),
        rd.Matched("reference [3]", "p.pdf", {**SCIPY, "container-title": "Unknown Journal", "ISSN": ["9999-9999"]}),
        rd.Matched("reference [4]", "p.pdf", {**SCIPY, "type": "proceedings-article", "ISSN": ["1111-1111"]}),
    ]
    journals = rd.cited_journals(matched)
    assert [j.name for j in journals] == ["Nature Methods", "International Journal of Pedagogics", "Unknown Journal"]
    service = _FakeService(
        [
            {
                "results": [
                    {"display_name": "Nature Methods", "issn": ["1548-7091", "1548-7105"], "type": "journal", "is_core": True, "listed_in": ["cwts-core", "medline"]},
                    {"display_name": "International Journal of Pedagogics", "issn": ["2181-1717"], "type": "journal", "is_core": False, "is_in_doaj": False, "listed_in": [], "host_organization_name": "Oak Publishers"},
                ]
            }
        ]
    )
    sources, complete = rd.lookup_sources([i for j in journals for i in j.issns], service)
    assert complete and "filter=issn%3A1548-7091" in service.urls[0]
    findings, found = rd.journal_indexing_findings(journals, sources)
    assert found == 2
    by = {f.category: f.detail for f in findings}
    assert "reference [2] cites International Journal of Pedagogics (Oak Publishers), which is in none of the indexes" in by[rd.UNINDEXED]
    assert "reference [3] cites Unknown Journal (ISSN 9999-9999), which OpenAlex does not list" in by[rd.UNKNOWN_JOURNAL]


def test_journal_on_a_national_list_only_is_still_flagged():
    assert not rd.is_indexed({"is_core": False, "listed_in": ["poland-20"]})
    assert rd.is_indexed({"is_core": False, "listed_in": ["norway-1"]})
    assert rd.is_indexed({"is_in_doaj": True})


def test_journal_indexing_unanswered_is_unchecked(monkeypatch):
    ctx = _ReferenceContext()
    validate = __import__("sys").modules["paperpush.validate"]
    monkeypatch.setattr(validate, "_matched_references", lambda venue, values, c: [rd.Matched("reference [1]", "p.pdf", SCIPY)])
    monkeypatch.setattr(rd, "lookup_sources", lambda issns, service: ({}, False))
    result = Checks().run("integrity.journal_indexing", "t", "I", _journal_indexing_issues, VENUE, {}, ctx)
    assert result.status == report.UNCHECKED


# --- uncited claims -------------------------------------------------------------------------


def test_uncited_claims_task_is_queued_by_default(tmp_path):
    path = tmp_path / "paper.md"
    path.write_text("Introduction\n\nA study at the University of Bremen found students scored 6.71 points lower.\n\nReferences\n\n1. Doe, J. Cells. Journal of Cells (2024).\n", encoding="utf-8")
    venue = Venue(slug="review_test", name="Review Test", fields=[Field(id="manuscript_file", label="Manuscript", type="file")])
    checks = Checks()
    reference_review.run_checks(checks, venue, {"manuscript_file": str(path)})
    task = next(t for t in checks.ai_tasks if t.id == "ai.uncited_claims")
    assert task.files == [str(path)]
    assert "University of Bremen" in task.instructions
