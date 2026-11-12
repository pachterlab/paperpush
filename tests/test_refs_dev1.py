"""Regression tests for the reference-metadata fixes found on the benchmark's dev split.

* A Mendeley export's ``{\\\\\\}National Academy of Sciences}`` (an escaped
  backslash, then an escaped brace) must not leave the entry open and swallow
  every entry after it: BibTeX counts every brace.
* A DOI braced by its reference manager (``10.1162/netn{_}a{_}00033``) is the
  plain DOI.
* A Zenodo software DOI is titled and dated by its release.
* An arXiv preprint whose arXiv record names no journal DOI is searched for its
  published version (U-Net, ResNet, BERT), and every common way of writing the
  arXiv id is read.

Every registry query is stubbed, so these run offline.
"""

from __future__ import annotations

import pytest

from paperpush import reference_search as rs
from paperpush import references
from paperpush.reference_search import Reference


@pytest.fixture(autouse=True)
def _empty_cache(monkeypatch):
    monkeypatch.setattr(rs, "_PUBLISHED_CACHE", {})


# --- BibTeX parsing ----------------------------------------------------------------------


def test_escaped_brace_does_not_swallow_the_following_entries():
    text = (
        "@article{newman, title={{Modularity}}, publisher = {\t{\\\\\\}National Academy of Sciences}, doi = {10.1073/PNAS.0601602103}}\n\n"
        "@article{next, title={{Robust detection}}, doi={10.1063/1.4790830}}\n"
    )
    entries = references.parse_bibtex(text)
    assert [e.key for e in entries] == ["newman", "next"]
    assert entries[1].doi == "10.1063/1.4790830"


def test_balanced_escaped_braces_still_parse():
    (entry,) = references.parse_bibtex("@article{k, title={The \\{x\\} set}, doi={10.1/abc}}")
    assert entry.key == "k" and entry.doi == "10.1/abc"


@pytest.mark.parametrize("raw", ["10.1162/netn{_}a{_}00033", "10.1162/netn{\\_}a{\\_}00033", "{10.1162/netn_a_00033}"])
def test_braced_doi_is_the_plain_doi(raw):
    assert references.normalize_doi(raw) == "10.1162/netn_a_00033"


# --- software DOIs ----------------------------------------------------------------------


def _entry(**fields):
    body = ", ".join(f"{k}={{{v}}}" for k, v in fields.items())
    (entry,) = references.parse_bibtex("@misc{k, " + body + "}")
    return entry


def test_a_software_release_title_and_year_are_not_a_mismatch():
    entry = _entry(title="Violin Plots for Matlab, Github Project", author="Bechtold, Bastian", year="2016", doi="10.5281/zenodo.4559847")
    record = {"type": "software", "title": "bastibe/Violinplot-Matlab: A Good Starting Point", "author": [{"family": "Bechtold"}], "issued": {"date-parts": [[2021]]}}
    assert references._mismatches(entry, record) == []


def test_a_software_doi_of_another_author_is_still_a_mismatch():
    entry = _entry(title="Violin Plots for Matlab", author="Bechtold, Bastian", year="2016", doi="10.5281/zenodo.1")
    record = {"type": "software", "title": "Something else", "author": [{"family": "Smith"}], "issued": {"date-parts": [[2021]]}}
    assert len(references._mismatches(entry, record)) == 3


def test_a_wrong_doi_on_an_article_is_still_a_mismatch():
    entry = _entry(title="Robust detection of dynamic community structure in networks", author="Bassett, Danielle", year="2013", doi="10.1038/nature14539")
    record = {"type": "journal-article", "title": "Deep learning", "author": [{"family": "LeCun"}], "issued": {"date-parts": [[2015]]}}
    assert len(references._mismatches(entry, record)) == 3


# --- reading arXiv ids --------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields, expected",
    [
        ("journal={ArXiv}, volume={abs/1810.04805}", "1810.04805"),
        ("journal={CoRR}, volume={abs/1505.04597}", "1505.04597"),
        ("journal={J}, arxivId={1206.4358}", "1206.4358"),
        ("journal={Nature}, volume={abs/1810.04805}", ""),
    ],
)
def test_more_ways_of_writing_an_arxiv_id(fields, expected):
    (entry,) = references.parse_bibtex("@article{k, title={T}, " + fields + "}")
    assert rs.arxiv_id_of(entry)[0] == expected


# --- published versions of arXiv preprints ----------------------------------------------------


UNET = {"title": "U-Net: Convolutional Networks for Biomedical Image Segmentation", "author": [{"family": "Ronneberger", "literal": "Olaf Ronneberger"}], "issued": {"date-parts": [[2015]]}, "latest_version": 1, "journal_doi": "", "journal_ref": ""}
UNET_TALK = {"DOI": "10.1007/978-3-662-54345-0_3", "type": "book-chapter", "title": ["Invited Talk: U-Net Convolutional Networks for Biomedical Image Segmentation"], "author": [{"family": "Ronneberger"}], "issued": {"date-parts": [[2017]]}, "container-title": ["Informatik aktuell"]}
UNET_MICCAI = {"DOI": "10.1007/978-3-319-24574-4_28", "type": "book-chapter", "title": ["U-Net: Convolutional Networks for Biomedical Image Segmentation"], "author": [{"family": "Ronneberger"}], "issued": {"date-parts": [[2015]]}, "container-title": ["Lecture Notes in Computer Science"]}
UNET_COPY = {"DOI": "10.2139/ssrn.1", "type": "posted-content", "title": ["U-Net: Convolutional Networks for Biomedical Image Segmentation"], "author": [{"family": "Ronneberger"}], "issued": {"date-parts": [[2015]]}}
OTHER_AUTHOR = {"DOI": "10.1109/x", "type": "proceedings-article", "title": ["U-Net: Convolutional Networks for Biomedical Image Segmentation"], "author": [{"family": "Smith"}], "issued": {"date-parts": [[2016]]}}


@pytest.fixture
def crossref(monkeypatch):
    answer = {"items": [UNET_TALK, UNET_COPY, OTHER_AUTHOR, UNET_MICCAI]}
    calls = []

    def search(query, service):
        calls.append(query)
        return answer["items"]

    monkeypatch.setattr(rs, "crossref_search", search)
    monkeypatch.setattr(rs, "arxiv_lookup", lambda ids, service: {i: UNET for i in ids})
    return answer, calls


def _unet_ref(**kw):
    base = dict(label="bibliography entry 'ronneberger2015unet'", where="custom.bib", title="U-Net: Convolutional Networks for Biomedical Image Segmentation", author="Ronneberger", year="2015", kind="article", venue="arXiv preprint arXiv:1505.04597", arxiv_id="1505.04597")
    base.update(kw)
    return Reference(**base)


def test_an_arxiv_preprint_with_a_published_version_is_reported(crossref):
    (finding,) = rs.version_findings([], [_unet_ref()], set(), arxiv_records={"1505.04597": UNET}, arxiv_checked={"1505.04597"})
    assert finding.category == "preprint has been published"
    assert "10.1007/978-3-319-24574-4_28" in finding.detail and "Lecture Notes in Computer Science" in finding.detail


def test_the_published_version_cited_too_is_a_duplicate(crossref):
    (finding,) = rs.version_findings([], [_unet_ref()], {"10.1007/978-3-319-24574-4_28"}, arxiv_records={"1505.04597": UNET}, arxiv_checked={"1505.04597"})
    assert finding.category == "preprint and article both cited"


def test_an_entry_that_names_its_venue_is_not_searched(crossref):
    _, calls = crossref
    ref = _unet_ref(venue="Medical Image Computing and Computer-Assisted Intervention (MICCAI)", kind="inproceedings")
    assert rs.version_findings([], [ref], set(), arxiv_records={"1505.04597": UNET}, arxiv_checked={"1505.04597"}) == []
    assert calls == []


def test_no_published_version_without_the_same_first_author_or_type(crossref):
    answer, _ = crossref
    answer["items"] = [UNET_COPY, OTHER_AUTHOR]
    assert rs.version_findings([], [_unet_ref()], set(), arxiv_records={"1505.04597": UNET}, arxiv_checked={"1505.04597"}) == []


def test_a_registry_that_does_not_answer_is_silent_and_not_cached(crossref):
    answer, _ = crossref
    answer["items"] = None
    assert rs.version_findings([], [_unet_ref()], set(), arxiv_records={"1505.04597": UNET}, arxiv_checked={"1505.04597"}) == []
    assert "1505.04597" not in rs._PUBLISHED_CACHE


def test_one_search_per_arxiv_id(crossref):
    _, calls = crossref
    refs = [_unet_ref(), _unet_ref(label="bibliography entry 'unet2'")]
    assert len(rs.version_findings([], refs, set(), arxiv_records={"1505.04597": UNET}, arxiv_checked={"1505.04597"})) == 2
    assert len(calls) == 1


def test_searches_are_capped(crossref, monkeypatch):
    _, calls = crossref
    monkeypatch.setattr(rs, "MAX_PUBLISHED_SEARCHES", 2)
    refs = [_unet_ref(label=f"e{i}", arxiv_id=f"1505.0459{i}") for i in range(5)]
    rs.version_findings([], refs, set(), arxiv_records={r.arxiv_id: UNET for r in refs}, arxiv_checked={r.arxiv_id for r in refs})
    assert len(calls) == 2


def test_a_reference_list_entry_naming_the_venue_is_not_reported(crossref):
    text = "O. Ronneberger, P. Fischer, T. Brox. U-Net: Convolutional networks for biomedical image segmentation. Lecture Notes in Computer Science, 2015. arXiv preprint arXiv:1505.04597"
    ref = Reference(label="reference [3]", where="main.pdf", text=text, arxiv_id="1505.04597")
    assert rs.version_findings([], [ref], set(), arxiv_records={"1505.04597": UNET}, arxiv_checked={"1505.04597"}) == []
    bare = Reference(label="reference [3]", where="main.pdf", text="O. Ronneberger, P. Fischer, T. Brox. U-Net: Convolutional networks for biomedical image segmentation. arXiv preprint arXiv:1505.04597, 2015", arxiv_id="1505.04597")
    (finding,) = rs.version_findings([], [bare], set(), arxiv_records={"1505.04597": UNET}, arxiv_checked={"1505.04597"})
    assert finding.category == "preprint has been published"


def test_an_entry_with_its_journal_doi_is_not_searched(crossref):
    _, calls = crossref
    ref = _unet_ref(doi="10.1007/978-3-319-24574-4_28")
    assert rs.version_findings([], [ref], set(), arxiv_records={"1505.04597": UNET}, arxiv_checked={"1505.04597"}) == []
    assert calls == []


def test_an_id_arxiv_does_not_know_is_left_to_the_search(crossref):
    _, calls = crossref
    assert rs.version_findings([], [_unet_ref()], set(), arxiv_records={}, arxiv_checked={"1505.04597"}) == []
    assert calls == []
