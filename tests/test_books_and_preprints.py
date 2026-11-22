"""bioRxiv/medRxiv versions, alphaXiv links, ISBNs, Open Library books, and the book and page details.

Every registry is stubbed (the conftest also makes any real request "fail"),
so these run offline.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import paperpush.references as refs
from paperpush import isbn, report
from paperpush import reference_details as rd
from paperpush import reference_search as rs
from paperpush.reference_search import MATCHED, NOT_FOUND, Cited, Reference
from paperpush.validate import Checks, _isbn_issues, _reference_detail_issues, _ReferenceContext

VENUE = SimpleNamespace(slug="zz_none", name="None", fields=[])


# --- bioRxiv / medRxiv versions ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, doi, version",
    [
        ("10.1101/2024.09.14.613029v2", "10.1101/2024.09.14.613029", 2),
        ("10.1101/2024.09.14.613029v2.full.pdf", "10.1101/2024.09.14.613029", 2),
        ("10.1101/2024.09.14.613029.full", "10.1101/2024.09.14.613029", None),
        ("doi:10.1101/673285v3.", "10.1101/673285", 3),
        ("10.1101/2024.09.14.613029", "10.1101/2024.09.14.613029", None),
        # Journals on the same prefix keep their DOIs whole.
        ("10.1101/gr.275569.121", "10.1101/gr.275569.121", None),
        ("10.1101/gad.1234v2", "10.1101/gad.1234v2", None),
    ],
)
def test_a_biorxiv_doi_loses_the_version_its_page_adds(raw, doi, version):
    assert refs.normalize_doi(raw) == doi
    assert refs.preprint_doi_version(raw) == version


def test_a_versioned_biorxiv_link_is_resolved_by_its_bare_doi(tmp_path, monkeypatch):
    """doi.org knows only the bare DOI; the versioned one used to be reported as unregistered."""
    path = tmp_path / "paper.txt"
    path.write_text("Intro.\n\nReferences\n\n1. Smith J. A preprint. bioRxiv (2024). https://www.biorxiv.org/content/10.1101/2024.09.14.613029v2\n", encoding="utf-8")
    asked = []

    def resolve(doi, **kwargs):
        asked.append(doi)
        return (refs.DOI_FOUND, {"title": ["A preprint"], "author": [{"family": "Smith"}], "issued": {"date-parts": [[2024]]}, "type": "posted-content"}) if doi == "10.1101/2024.09.14.613029" else (refs.DOI_MISSING, None)

    monkeypatch.setattr(refs, "_resolve_doi", resolve)
    findings = refs.scan_references([path])
    assert asked == ["10.1101/2024.09.14.613029"]
    assert not any(f.category == "unresolvable DOI" for f in findings)


def test_the_cited_preprint_version_is_read_from_a_bib_doi_or_url(tmp_path):
    bib = tmp_path / "r.bib"
    bib.write_text(
        "@article{a, title={One preprint here}, author={Smith, J}, year={2024}, doi={10.1101/2024.09.14.613029v1}}\n"
        "@misc{b, title={Two preprint here}, author={Lee, K}, year={2024}, url={https://www.biorxiv.org/content/10.1101/2024.01.02.000001v3.full}}\n"
        "@article{c, title={Three}, author={Lee, K}, year={2024}, doi={10.1101/2024.01.02.000002}}\n",
        encoding="utf-8",
    )
    got = {r.label: r.preprint_version for r in rs.references_from_entries(refs.collect_entries([bib]))}
    assert got == {"bibliography entry 'a'": 1, "bibliography entry 'b'": 3, "bibliography entry 'c'": None}


def _preprint():
    return {"title": ["A preprint"], "author": [{"family": "Smith"}], "issued": {"date-parts": [[2024]]}, "type": "posted-content"}


def test_an_old_biorxiv_version_is_reported(monkeypatch):
    monkeypatch.setattr(rs, "biorxiv_published_doi", lambda doi, service: "")
    monkeypatch.setattr(rs, "biorxiv_latest_version", lambda doi, service: 3)
    (finding,) = rs.version_findings([Cited("reference [1]", "p.pdf", "10.1101/2024.09.14.613029", _preprint(), preprint_version=1)], [], set())
    assert finding.category == "older preprint version"
    assert "version 1 of the preprint 10.1101/2024.09.14.613029, but the latest version is 3" in finding.detail


def test_the_latest_or_an_unpinned_biorxiv_version_is_not_reported(monkeypatch):
    asked = []
    monkeypatch.setattr(rs, "biorxiv_published_doi", lambda doi, service: "")
    monkeypatch.setattr(rs, "biorxiv_latest_version", lambda doi, service: asked.append(doi) or 3)
    assert rs.version_findings([Cited("r", "p.pdf", "10.1101/2024.09.14.613029", _preprint(), preprint_version=3)], [], set()) == []
    assert rs.version_findings([Cited("r", "p.pdf", "10.1101/2024.09.14.613029", _preprint())], [], set()) == []
    assert asked == ["10.1101/2024.09.14.613029"]


def test_a_published_biorxiv_preprint_is_reported_as_published_not_as_old(monkeypatch):
    monkeypatch.setattr(rs, "biorxiv_published_doi", lambda doi, service: "10.1038/s41587-021-00870-2")
    monkeypatch.setattr(rs, "biorxiv_latest_version", lambda doi, service: 3)
    (finding,) = rs.version_findings([Cited("r", "p.pdf", "10.1101/2024.09.14.613029", _preprint(), preprint_version=1)], [], set())
    assert finding.category == "preprint has been published"


class _Answers:
    def __init__(self, answers):
        self.answers = answers
        self.urls = []
        self.available = True

    def get_json(self, url):
        self.urls.append(url)
        return self.answers.pop(0) if self.answers else None


def test_biorxiv_latest_version_falls_back_to_medrxiv():
    service = _Answers([{"collection": []}, {"collection": [{"version": "1"}, {"version": "2"}]}])
    assert rs.biorxiv_latest_version("10.1101/2020.01.01.000001", service) == 2
    assert "/details/biorxiv/" in service.urls[0] and "/details/medrxiv/" in service.urls[1]
    assert rs.biorxiv_latest_version("10.1101/2020.01.01.000001", _Answers([])) is None


# --- alphaXiv ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://www.alphaxiv.org/abs/2101.00001", ("2101.00001", None)),
        ("alphaxiv.org/overview/2101.00001v2", ("2101.00001", "2")),
        ("https://arxiv.org/html/2101.00001v1", ("2101.00001", "1")),
    ],
)
def test_alphaxiv_and_arxiv_html_links_give_the_arxiv_id(text, expected):
    assert rs._ARXIV_IN_TEXT.search(text).groups() == expected


def test_an_alphaxiv_url_in_a_bib_entry_is_checked_as_arxiv(tmp_path):
    bib = tmp_path / "r.bib"
    bib.write_text("@misc{a, title={Some paper}, author={Smith, J}, year={2021}, url={https://www.alphaxiv.org/abs/2101.00001v2}}\n", encoding="utf-8")
    (ref,) = rs.references_from_entries(refs.collect_entries([bib]))
    assert (ref.arxiv_id, ref.arxiv_version) == ("2101.00001", 2)


# --- ISBNs ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, problem",
    [
        ("978-0-387-84857-0", ""),
        ("0387848576", ""),
        ("0-262-03561-8", ""),
        ("0-8044-2957-X", ""),
        ("9780387848571", "check digit"),
        ("026203561X", "check digit"),
        ("97803878485", "11 digits"),
        ("9770387848570", "starts with 978 or 979"),
        ("03878X8576", "only an ISBN-10's last character"),
    ],
)
def test_isbn_check_digit(value, problem):
    found = isbn.isbn_problem(value)
    assert (problem in found) if problem else found == ""


def test_an_isbn10_converts_to_its_isbn13():
    assert isbn.to_isbn13("0-387-84857-6") == "9780387848570"
    assert isbn.to_isbn13("9780387848570") == "9780387848570"
    assert isbn.to_isbn13("9780387848571") == ""


def test_isbns_are_read_only_where_labelled_and_without_the_year_after():
    assert isbn.isbns_in_text("Springer; 2009. ISBN 978-0-387-84857-0. 745 pp.") == ["978-0-387-84857-0"]
    assert isbn.isbns_in_text("Wiley. ISBN-10: 0-387-84857-6 2009") == ["0-387-84857-6"]
    assert isbn.isbns_in_text("A truncated one: ISBN 978-0-387-8485.") == ["978-0-387-8485"]
    assert isbn.isbns_in_text("Smith (2009), 9780387848570 copies sold") == []
    assert isbn.isbns_in_field("9780387848570, 0387848576") == ["9780387848570", "0387848576"]
    assert isbn.isbns_in_field("12-34") == ["12-34"]


def _book(title="The Elements of Statistical Learning: Data Mining, Inference, and Prediction", short="The Elements of Statistical Learning", family="Hastie", year=2009, publisher="Springer-Verlag New York", edition="", isbn13="9780387848570"):
    return {
        "title": [title],
        "short-title": [short],
        "author": [{"family": family, "given": "T"}],
        "issued": {"date-parts": [[year]]},
        "type": "book",
        "publisher": publisher,
        "publishers": [publisher],
        "edition": edition,
        "ISBN": [isbn13],
        "source": "Open Library",
    }


def _book_ref(title="The Elements of Statistical Learning", isbns=("978-0-387-84857-0",), label="bibliography entry 'esl'"):
    return Reference(label, "refs.bib", title=title, author="Hastie", year="2009", kind="book", isbns=list(isbns))


def test_isbn_findings(monkeypatch):
    asked = []
    monkeypatch.setattr(rs, "openlibrary_isbn_records", lambda isbns, service: asked.append(isbns) or {"9780387848570": _book()})
    good = _book_ref()
    wrong = _book_ref(title="Quantum Gardening for Beginners", label="bibliography entry 'qg'")
    invalid = _book_ref(isbns=("9780387848571",), label="bibliography entry 'bad'")
    unknown = _book_ref(isbns=("9780262035613",), label="bibliography entry 'dl'")
    result = rs.isbn_findings([good, wrong, invalid, unknown])
    assert asked == [["9780262035613", "9780387848570"]]
    by = {f.category: f for f in result.findings}
    assert set(by) == {rs.INVALID_ISBN, rs.WRONG_ISBN}
    assert "'bad' gives the ISBN 9780387848571, which is not a valid ISBN" in by[rs.INVALID_ISBN].detail and by[rs.INVALID_ISBN].level != "likely_error"
    assert by[rs.WRONG_ISBN].level == "likely_error" and "'qg'" in by[rs.WRONG_ISBN].detail and "Springer-Verlag New York" in by[rs.WRONG_ISBN].detail
    assert list(result.records) == ["bibliography entry 'esl'"]
    assert (result.read, result.valid, result.found) == (4, 3, 1)


def test_an_isbn_in_a_reference_list_entry_is_checked_against_its_text(monkeypatch):
    monkeypatch.setattr(rs, "openlibrary_isbn_records", lambda isbns, service: {"9780387848570": _book()})
    same = Reference("reference [1]", "p.pdf", text="Hastie T, Tibshirani R, Friedman J. The Elements of Statistical Learning. 2nd ed. Springer; 2009. ISBN 978-0-387-84857-0.", isbns=["978-0-387-84857-0"])
    other = Reference("reference [2]", "p.pdf", text="Smith J. Gardening with quantum computers at home. Acme Press; 2019. ISBN 978-0-387-84857-0.", isbns=["978-0-387-84857-0"])
    result = rs.isbn_findings([same, other])
    assert list(result.records) == ["reference [1]"]
    assert [f.category for f in result.findings] == [rs.WRONG_ISBN] and "reference [2]" in result.findings[0].detail


def test_open_library_not_answering_reports_nothing_but_the_checksums(monkeypatch):
    monkeypatch.setattr(rs, "openlibrary_isbn_records", lambda isbns, service: None)
    result = rs.isbn_findings([_book_ref(), _book_ref(isbns=("9780387848571",), label="b")])
    assert [f.category for f in result.findings] == [rs.INVALID_ISBN] and not result.answered


def test_open_library_records_are_read_into_the_csl_shape():
    service = _Answers(
        [
            {"ISBN:9780387848570": {"title": "The Elements of Statistical Learning", "subtitle": "Data Mining", "authors": [{"name": "Trevor Hastie"}], "publishers": [{"name": "Springer"}], "publish_date": "Feb 2009", "identifiers": {"isbn_13": ["9780387848570"]}}},
            {"ISBN:9780387848570": {"details": {"edition_name": "2nd ed."}}},
        ]
    )
    records = rs.openlibrary_isbn_records(["9780387848570", "9780262035613"], service)
    rec = records["9780387848570"]
    assert set(records) == {"9780387848570"}
    assert rec["title"] == ["The Elements of Statistical Learning: Data Mining"] and rec["author"][0]["family"] == "Hastie"
    assert rec["issued"] == {"date-parts": [[2009]]} and rec["publisher"] == "Springer" and rd.record_edition(rec) == 2
    assert rs.openlibrary_isbn_records(["9780387848570"], _Answers([])) is None


def test_isbn_check_through_validate(tmp_path, monkeypatch):
    bib = tmp_path / "r.bib"
    bib.write_text("@book{esl, title={The Elements of Statistical Learning}, author={Hastie, Trevor}, year={2009}, edition={1st}, publisher={Wiley}, isbn={978-0-387-84857-0}}\n", encoding="utf-8")
    validate = sys.modules["paperpush.validate"]
    monkeypatch.setattr(validate, "_upload_paths_or_skip", lambda venue, values: [bib])
    monkeypatch.setattr(rs, "openlibrary_isbn_records", lambda isbns, service: {"9780387848570": _book(edition="2nd ed.")})
    ctx = _ReferenceContext()
    checks = Checks()
    result = checks.run("integrity.isbns", "t", "I", _isbn_issues, VENUE, {}, ctx)
    assert result.status == report.PASSED and "1 found in Open Library" in result.detail
    details = checks.run("integrity.reference_details", "t", "I", _reference_detail_issues, VENUE, {}, ctx)
    messages = " ".join(i.message for i in details.issues)
    assert "cites the 1st edition but the work is the 2nd edition" in messages
    assert "its publisher is Wiley but the work's is Springer-Verlag New York" in messages


def test_isbn_check_without_isbns_is_not_applicable(tmp_path, monkeypatch):
    bib = tmp_path / "r.bib"
    bib.write_text("@book{b, title={A book with no ISBN}, author={Smith, J}, year={2009}}\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules["paperpush.validate"], "_upload_paths_or_skip", lambda venue, values: [bib])
    assert Checks().run("integrity.isbns", "t", "I", _isbn_issues, VENUE, {}, _ReferenceContext()).status == report.NOT_APPLICABLE


# --- Open Library title search ----------------------------------------------------------------


@pytest.fixture
def no_registry_match(monkeypatch):
    monkeypatch.setattr(rs, "crossref_search", lambda query, service: [])
    monkeypatch.setattr(rs, "openalex_search", lambda title, service: [])
    monkeypatch.setattr(rs, "datacite_arxiv_title_search", lambda title, service: [])
    monkeypatch.setattr(rs, "arxiv_title_search", lambda title, service: [])
    searched = []
    monkeypatch.setattr(rs, "openlibrary_search", lambda title, author, year, service: searched.append(title) or [_book()])
    return searched


def test_a_book_the_registries_miss_is_matched_in_open_library(no_registry_match):
    result = rs.search_references([Reference("bibliography entry 'esl'", "refs.bib", title="The Elements of Statistical Learning", author="Hastie", year="2009", kind="book")])
    (lookup,) = result.lookups
    assert lookup.status == MATCHED and lookup.source.endswith("Open Library")
    assert result.unverified == [] and result.findings == []
    assert no_registry_match == ["The Elements of Statistical Learning"]


def test_a_journal_article_is_not_searched_in_open_library(no_registry_match):
    result = rs.search_references([Reference("bibliography entry 'x'", "refs.bib", title="Near optimal probabilistic RNA seq quantification", author="Bray", year="2016", kind="article")])
    assert result.lookups[0].status == NOT_FOUND and no_registry_match == []


def test_a_book_open_library_does_not_match_stays_unverified(no_registry_match, monkeypatch):
    monkeypatch.setattr(rs, "openlibrary_search", lambda title, author, year, service: [_book(title="Something Else Entirely", short="Something Else Entirely")])
    result = rs.search_references([Reference("bibliography entry 'b'", "refs.bib", title="A Book Nobody Has Catalogued Yet", author="Smith", year="2001", kind="book")])
    assert [lk.ref.label for lk in result.unverified] == ["bibliography entry 'b'"] and result.findings == []


# --- last page --------------------------------------------------------------------------------

SCIPY = {
    "type": "journal-article",
    "title": "SciPy 1.0: fundamental algorithms for scientific computing in Python",
    "container-title": "Nature Methods",
    "volume": "17",
    "issue": "3",
    "page": "261-272",
    "issued": {"date-parts": [[2020]]},
    "author": [{"given": "Pauli", "family": "Virtanen"}],
}


def _bib(bibtex: str, record=SCIPY):
    entry = refs.parse_bibtex(bibtex, "refs.bib")[0]
    return rd.detail_findings([rd.Matched(f"bibliography entry '{entry.key}'", "refs.bib", record, entry=entry, doi="10.1038/x")])


def _text(text: str, record=SCIPY):
    return rd.detail_findings([rd.Matched("reference [1]", "p.pdf", record, text=text, doi="10.1038/x")])


def test_a_wrong_last_page_is_reported():
    (found,) = _bib("@article{a, author={Virtanen, Pauli}, title={SciPy}, journal={Nature Methods}, volume={17}, number={3}, pages={261--279}, year={2020}}")
    assert "pages end at 279 but the work ends on page 272" in found.detail
    (found,) = _text("Virtanen P. SciPy 1.0: fundamental algorithms for scientific computing in Python. Nature Methods 17(3):261-279 (2020).")
    assert "pages end at 279 but the work ends on page 272" in found.detail


@pytest.mark.parametrize("pages", ["261--272", "261--72", "261", "e261"])
def test_a_right_or_shortened_last_page_is_not_reported(pages):
    assert _bib(f"@article{{a, author={{Virtanen, Pauli}}, title={{SciPy}}, journal={{Nature Methods}}, volume={{17}}, number={{3}}, pages={{{pages}}}, year={{2020}}}}") == []


def test_a_shortened_registered_range_is_read_in_full():
    record = {**SCIPY, "page": "1234-9"}
    assert _bib("@article{a, author={Virtanen, Pauli}, title={SciPy}, journal={Nature Methods}, volume={17}, number={3}, pages={1234--1239}, year={2020}}", record) == []


# --- books: edition and publisher -------------------------------------------------------------

CROSSREF_BOOK = {"type": "book", "title": ["The Elements of Statistical Learning"], "author": [{"family": "Hastie", "given": "Trevor"}, {"family": "Tibshirani", "given": "Robert"}, {"family": "Friedman", "given": "Jerome"}], "issued": {"date-parts": [[2009]]}, "publisher": "Springer New York", "edition": "2"}


def test_book_edition_and_publisher_from_a_bib_entry():
    found = _bib("@book{esl, author={Hastie, Trevor and Tibshirani, Robert and Friedman, Jerome}, title={The Elements of Statistical Learning}, edition={Third}, publisher={Wiley}, year={2009}}", CROSSREF_BOOK)
    by = {f.category: f for f in found}
    assert "cites the 3rd edition but the work is the 2nd edition" in by[rd.DETAILS].detail
    assert by[rd.PUBLISHER].level == "suggestion" and "its publisher is Wiley but the work's is Springer New York" in by[rd.PUBLISHER].detail


@pytest.mark.parametrize("publisher", ["Springer", "Springer-Verlag", "Springer Science \\& Business Media"])
def test_another_form_of_the_same_publisher_is_not_reported(publisher):
    assert _bib(f"@book{{esl, author={{Hastie, Trevor and Tibshirani, Robert and Friedman, Jerome}}, title={{The Elements of Statistical Learning}}, edition={{2nd}}, publisher={{{publisher}}}, year={{2009}}}}", CROSSREF_BOOK) == []


def test_book_edition_and_publisher_from_a_reference_list_entry():
    found = _text("Hastie T, Tibshirani R, Friedman J. The Elements of Statistical Learning. 3rd edn. Wiley; 2009.", CROSSREF_BOOK)
    by = {f.category: f for f in found}
    assert "cites the 3rd edition but the work is the 2nd edition" in by[rd.DETAILS].detail
    assert "does not name the work's publisher, Springer New York" in by[rd.PUBLISHER].detail
    assert _text("Hastie T, Tibshirani R, Friedman J. The Elements of Statistical Learning. 2nd ed. New York: Springer; 2009.", CROSSREF_BOOK) == []


def test_open_library_authors_are_not_compared_name_by_name():
    """Open Library mixes series editors and translators into a book's authors."""
    record = {**_book(), "author": [{"family": "Goodfellow"}, {"family": "Bengio"}, {"family": "Courville"}, {"family": "Bach"}], "publisher": "MIT Press", "publishers": ["MIT Press"]}
    assert _bib("@book{dl, author={Goodfellow, Ian and Bengio, Yoshua and Courville, Aaron}, title={Deep Learning}, publisher={MIT Press}, year={2016}}", record) == []


@pytest.mark.parametrize(
    "cited, registered, same",
    [
        ("MIT Press", "Massachusetts Institute of Technology Press", True),
        ("Cambridge Univ. Press", "Cambridge University Press", True),
        ("CRC Press", "Chapman and Hall/CRC", True),
        ("Oxford University Press", "Cambridge University Press", False),
        ("Wiley", "Springer Science and Business Media LLC", False),
    ],
)
def test_same_publisher(cited, registered, same):
    assert rd.same_publisher(cited, {"publisher": registered}) is same


def test_edition_numbers():
    assert [rd.edition_number(v) for v in ("2", "2nd", "Second", "2nd ed.", "")] == [2, 2, 2, 2, None]
    assert rd.edition_in_text("Springer, 2nd edn, 2009") == 2 and rd.edition_in_text("Third revised edition. Wiley") == 3
    assert rd.edition_in_text("The second law of thermodynamics") is None


def test_open_library_fills_in_the_edition_and_publisher_a_crossref_book_lacks():
    from paperpush.validate import _with_book_details

    crossref = {k: v for k, v in CROSSREF_BOOK.items() if k not in ("publisher", "edition")}
    matched = [rd.Matched("bibliography entry 'esl'", "refs.bib", crossref), rd.Matched("other", "refs.bib", CROSSREF_BOOK)]
    out = _with_book_details(matched, {"bibliography entry 'esl'": _book(edition="2nd ed."), "other": _book(publisher="Wiley", edition="5th")})
    assert out[0].record["publisher"] == "Springer-Verlag New York" and rd.record_edition(out[0].record) == 2
    # A record that already has them keeps its own.
    assert out[1].record["publisher"] == "Springer New York" and rd.record_edition(out[1].record) == 2
    assert crossref.get("publisher") is None
