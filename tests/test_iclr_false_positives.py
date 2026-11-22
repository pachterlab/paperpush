"""False alarms found by running ``validate`` on accepted ICLR 2026 papers.

Each test is a shape seen in a real reference list, as pypdf extracts it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paperpush import references as R


def _citations(tmp_path: Path, body: str) -> list[R.Citation]:
    f = tmp_path / "paper.txt"
    f.write_text("References\n" + body)
    return R.collect_citations([f])


@pytest.mark.parametrize(
    "text, doi",
    [
        # A kerning gap read as a space before the dot.
        ("CoRR, abs/2401.15884, 2024. doi: 10.48550/ARXIV .2401.15884. URL https://doi.org/x", "10.48550/arxiv.2401.15884"),
        ("Image Process., 2003. doi: 10.1109/TIP .2003.819861.\n", "10.1109/tip.2003.819861"),
        # An underscore drawn as a backslash and a space.
        ("Springer, 2002a. doi: 10.1007/3-540-36755-1\\ 29. URLhttps://doi.org/x", "10.1007/3-540-36755-1_29"),
        ("Neural Comput., 2017. doi: 10.1162/NECO \\ a\\ 00949. URL https://x", "10.1162/neco_a_00949"),
        # A ligature in the extracted text.
        ("ACL 2023. doi: 10.18653/v1/2023.ﬁndings-acl.618. URL\n", "10.18653/v1/2023.findings-acl.618"),
    ],
)
def test_doi_split_on_its_line_is_rejoined(tmp_path, text, doi):
    assert [c.doi for c in _citations(tmp_path, text)] == [doi]


def test_doi_cut_at_a_space_gets_fallback_readings(tmp_path):
    (c,) = _citations(tmp_path, "TACL, 2024. doi: 10.1162/tacl a 00718. URL\nhttps://aclanthology.org/x\n")
    assert c.doi == "10.1162/tacl"
    assert "10.1162/tacl_a_00718" in c.alternates
    (c,) = _citations(tmp_path, "WACV, 2023. doi: 10.1109/W ACV56688.2023.00466.\n")
    assert "10.1109/wacv56688.2023.00466" in c.alternates


def test_whole_doi_followed_by_its_link_gets_no_fallbacks(tmp_path):
    (c,) = _citations(tmp_path, "Nature, 2020. doi: 10.1038/s41586-020-2649-2 URL https://x\n")
    assert c.doi == "10.1038/s41586-020-2649-2"
    assert c.alternates == ()


def test_fallback_reading_is_used_when_the_cut_form_is_missing(monkeypatch):
    registry = {"10.1162/tacl_a_00718": {"title": "Holmes"}}
    monkeypatch.setattr(R, "_resolve_doi", lambda doi, **_: (R.DOI_FOUND, registry[doi]) if doi in registry else (R.DOI_MISSING, None))
    status, record, doi = R._resolve_with_fallback(("10.1162/tacl", "", ("10.1162/tacla", "10.1162/tacl_a", "10.1162/tacl_a_00718")))
    assert (status, doi, record["title"]) == (R.DOI_FOUND, "10.1162/tacl_a_00718", "Holmes")


def test_dois_cut_to_the_same_prefix_are_resolved_separately(tmp_path, monkeypatch):
    """Two TACL DOIs cut to "10.1162/tacl" each keep their own fallback readings."""
    registry = {"10.1162/tacl_a_00718": {"title": "Holmes"}, "10.1162/tacl_a_00634": {"title": "Lost in the middle"}}
    monkeypatch.setattr(R, "_resolve_doi", lambda doi, **_: (R.DOI_FOUND, registry[doi]) if doi in registry else (R.DOI_MISSING, None))
    f = tmp_path / "paper.txt"
    f.write_text(
        "References\n"
        "Andreas Waldis. Holmes a benchmark. TACL, 2024. doi: 10.1162/tacl a 00718. URL https://x\n\n"
        "Nelson Liu. Lost in the middle. TACL, 2024. doi: 10.1162/tacl a 00634. URL https://x\n"
    )
    findings = R.scan_references([f])
    assert not [x for x in findings if "not registered" in x.detail]
    assert sum(c.doi == "10.1162/tacl" for c in R.collect_citations([f])) == 2


def test_acm_placeholder_doi_is_a_warning():
    c = R.Citation(doi="10.5555/1390681.1390693", where="p.pdf", context="", in_reference_list=True)
    (f,) = R._citation_findings(c, R.DOI_MISSING, None, c.doi)
    assert f.level == R.LEVEL_WARNING
    assert "10.5555" in f.detail


# --- reference names and titles ---------------------------------------------------------

from paperpush import reference_search as rs  # noqa: E402


def _rec(names, title, year=2024):
    authors = []
    for n in names:
        parts = n.split()
        authors.append({"given": " ".join(parts[:-1]), "family": parts[-1]})
    return {"title": title, "author": authors, "issued": {"date-parts": [[year]]}, "type": "article"}


def test_middle_initial_does_not_end_the_lead_author():
    assert rs._entry_lead_author("Diederik P. Kingma and Jimmy Ba. Adam: A method for stochastic optimization. In ICLR, 2015.") == "Kingma"
    assert rs._entry_lead_author("Edward J. Hu, Yelong Shen, and Phillip Wallis. LoRA. In ICLR, 2022.") == "Hu"


def test_arxiv_record_with_added_authors_and_new_title_is_the_cited_paper():
    """REINFORCE++: v1 by Jian Hu alone, retitled and co-authored in its latest version."""
    record = _rec(["Jian Hu", "Jason Klein Liu", "Haotian Xu", "Wei Shen"], "REINFORCE++: Stabilizing Critic-Free Policy Optimization with Global Advantage Normalization", 2025)
    ref = rs.Reference(label="x", where="p.pdf", text="Jian Hu. Reinforce++: A simple and efficient approach for aligning large language models. arXiv preprint arXiv:2501.03262, 2025.", arxiv_id="2501.03262")
    assert rs.match_arxiv(ref, record).problems == []


def test_arxiv_id_of_a_paper_by_namesakes_is_still_wrong():
    record = _rec(["Chi Zhang", "Zhao Yang", "Jiaxuan Liu", "Yucheng Li"], "AppAgent: Multimodal Agents as Smartphone Users", 2023)
    ref = rs.Reference(label="x", where="p.pdf", text="Tong Yang, Xiaodan Hu, Jingfeng Zhang, Zhilin Li, and Jason Liu. AnyBench: Language models evaluate anything. arXiv preprint arXiv:2312.13771, 2023.", arxiv_id="2312.13771")
    assert rs.match_arxiv(ref, record).problems


def test_family_name_first_without_comma_is_the_same_people():
    record = _rec(["Siyu Chen", "Heejune Sheen", "Tianhao Wang", "Zhuoran Yang"], "Unveiling induction heads: Provable training dynamics and feature learning in transformers")
    text = "Chen Siyu, Sheen Heejune, Wang Tianhao, and Yang Zhuoran. Unveiling induction heads: Provable training dynamics and feature learning in transformers. In NeurIPS, 2024."
    lk = rs.match_text(rs.Reference(label="x", where="p.pdf", text=text), [record])
    assert not [p for p in lk.problems if p.startswith(rs.WRONG_AUTHORS)]


def test_same_title_under_other_people_in_another_year_is_still_reported():
    """A fabricated entry gives a real title made-up people and a made-up year, so a far year does not
    excuse an exact title under other people. (The price: a review carrying a book's exact title is
    reported too.)"""
    real = _rec(["Mary Smith", "John Doe"], "Adaptive gradient methods for sparse online learning", 2018)
    text = "Wei Di, Yang Yu, and Bo Liu. Adaptive gradient methods for sparse online learning. In ICML, 2025."
    lk = rs.match_text(rs.Reference(label="x", where="p.pdf", text=text), [real])
    assert [p for p in lk.problems if p.startswith(rs.WRONG_AUTHORS)]


def test_similar_title_under_other_people_is_not_reported_outright():
    review = _rec(["Mary Smith", "John Doe"], "Review of: Harry Potter and the Chamber of Secrets", 2002)
    text = "J. K. Rowling and Mary GrandPre. Harry Potter and the Chamber of Secrets. Scholastic, 1998."
    lk = rs.match_text(rs.Reference(label="x", where="p.pdf", text=text), [review])
    assert not [p for p in lk.problems if p.startswith(rs.WRONG_AUTHORS)]


def test_title_glued_to_the_venue_and_position_prefix():
    assert rs._prose_title("Ricky Chen and Yaron Lipman. Riemannian flow matching on general geometries.arXiv e-prints, pp. 1, 2023.") == "Riemannian flow matching on general geometries"
    assert rs._prose_title("Minyoung Huh and Brian Cheung. Position: The platonic representation hypothesis. In ICML, 2024.") == "The platonic representation hypothesis"
    assert rs._prose_title("Albert Q. Jiang and Alexandre Sablayrolles. Mistral 7b, 2023. URL https://x") == "Mistral 7b"
    assert rs._not_a_title("InInternational Conference on Representation Learning")


def test_arxiv_url_with_stray_spaces():
    m = rs._ARXIV_IN_TEXT.search("Mistral 7b, 2023. URL https://arxiv. org/abs/2310.06825.")
    assert m and m.group(1) == "2310.06825"


def test_empty_openalex_answer_is_not_cached():
    assert rs._empty_search_answer(b'{"meta": {"count": 0, "cost_usd": 0.0}, "results": []}')
    assert not rs._empty_search_answer(b'{"meta": {"count": 1}, "results": [{"id": "W1"}]}')


# --- DOI context ----------------------------------------------------------------------------


def test_wrapped_page_range_is_not_an_entry_marker(tmp_path):
    (c,) = _citations(
        tmp_path,
        "Joseph Bonilla and Sophia Zhang. Gaussian pancakes: geometrically-regularized 3D Gaussian splatting for realistic endoscopic reconstruction. In MICCAI, pp. 274-\n284. Springer, 2024. doi: 10.1007/978-3-031-72089-5_26.\n",
    )
    assert "pancakes" in c.context


def test_springer_chapter_number_split_off_is_rejoined(tmp_path):
    (c,) = _citations(tmp_path, "Bonilla. Gaussian pancakes. In MICCAI, 2024. doi: 10.1007/978-3-031-72089-5 26. URL https://x\n")
    assert c.doi == "10.1007/978-3-031-72089-5_26"


def test_unnumbered_entry_reaches_back_to_its_title(tmp_path):
    long_editors = "In Kristina Toutanova, Anna Rumshisky, Luke Zettlemoyer, Dilek Hakkani-Tur, Iz Beltagy, Steven Bethard, Ryan Cotterell, Tanmoy Chakraborty, and Yichao Zhou (eds.), Proceedings of the 2021 Conference of the North American Chapter of the Association for Computational Linguistics: Human Language Technologies, pp. 1-10, Online, June 2021. Association for Computational Linguistics. "
    (c,) = _citations(tmp_path, "Alice Writer and Bob Author. A remarkably specific benchmark title. " + long_editors + "doi: 10.18653/v1/2021.naacl-main.1.\n")
    assert "remarkably" in c.context


def test_dot_kerning_gap_across_a_line_break(tmp_path):
    (c,) = _citations(tmp_path, "Yan. Corrective RAG, 2023. doi: 10.48550/ARXIV .\n2303.08797. URL https://doi\n")
    assert c.doi == "10.48550/arxiv.2303.08797"


# --- template and length ----------------------------------------------------------------------

from paperpush import manuscript, template_check  # noqa: E402
from paperpush.requirements import TemplateLayout  # noqa: E402


def _measured(tops, bottoms):
    return template_check.LayoutMeasurement(pages=len(tops), page_width=612, page_height=792, body_font=10, baseline_skip=11, left=108, right=504, top_by_page=tuple(tops), bottom_by_page=tuple(bottoms), first_page_text="", margin_line_numbers=False)


def test_a_few_overfull_pages_are_a_minor_problem():
    layout = TemplateLayout(text_top_pt=711, text_bottom_pt=81, tolerance_pt=3)
    problems = list(template_check.layout_problems(_measured([711] * 18 + [720, 722], [81] * 20), layout))
    assert problems and all(template_check.is_minor_layout_problem(p) for p in problems)


def test_a_moved_bottom_margin_on_most_pages_is_not_minor():
    layout = TemplateLayout(text_top_pt=711, text_bottom_pt=81, tolerance_pt=3)
    problems = list(template_check.layout_problems(_measured([711] * 20, [60] * 16 + [81] * 4), layout))
    assert problems and not any(template_check.is_minor_layout_problem(p) for p in problems)


@pytest.mark.parametrize("line", ["Reproducibility statement We release all code.", "AcknowledgmentsThis work was supported by", "6E THICS STATEMENT", "Disclosure of LLM Usage"])
def test_back_matter_headings_end_the_main_text(line):
    pattern = manuscript._extra_heading_pattern(("Reproducibility Statement", "Acknowledgments", "Ethics Statement", "Disclosure of LLM Usage"))
    assert pattern.search(line)


def test_a_wrapped_sentence_is_not_a_heading():
    pattern = manuscript._extra_heading_pattern(("Acknowledgments", "Author Contributions"))
    assert not pattern.search("acknowledgments We thank the reviewers")


# --- second round (fp2 residuals) ---------------------------------------------------------------

from paperpush import sensitive  # noqa: E402


@pytest.mark.parametrize(
    "text, doi",
    [
        # Wrapped right after the prefix, then cut again on the new line.
        ("Bowman. BLiMP. TACL, 2020. doi: 10.1162/\ntacl a 00321. URL https://aclanthology.org/2020.tacl-1.25/.\n", "10.1162/tacl_a_00321"),
        ("MIT Press, 2021. doi: 10.1162/\nISAL\\_A\\_00369. URL https://doi.org/10.1162/isal_a_00369.\n", "10.1162/isal_a_00369"),
        ("CMA-ES/PYCMA on Github. zenodo, doi:\n10.5281/zenodo. 2559634.(feb. 2019), 2019.\n", "10.5281/zenodo.2559634"),
    ],
)
def test_doi_wrapped_at_the_slash_is_read_on_its_new_line(tmp_path, text, doi):
    readings = [{c.doi, *c.alternates} for c in _citations(tmp_path, text)]
    assert readings and all(doi in r for r in readings)


def test_publisher_link_with_article_id_is_the_same_doi(tmp_path):
    text = "Journal of the IGPL, 2024. doi: 10.1093/\njigpal/jzae062. URL https://academic.oup.com/jigpal/advance-article/\ndoi/10.1093/jigpal/jzae062/7689640.\n"
    assert [c.doi for c in _citations(tmp_path, text)] == ["10.1093/jigpal/jzae062"]


def test_a_digitless_doi_that_does_not_resolve_is_cut_off_not_dead():
    cit = R.Citation(doi="10.1109/w", where="paper.pdf", context="", in_reference_list=True)
    [finding] = R._citation_findings(cit, R.DOI_MISSING, None, "10.1109/w")
    assert finding.level == R.LEVEL_WARNING and "cut off" in finding.detail


def test_non_ascii_initial_does_not_end_the_author_list():
    text = "A. Vaswani, N. Shazeer, A. N. Gomez, Ł. Kaiser, and I. Polosukhin. Attention is all you need. In NeurIPS, 2017."
    assert rs._prose_title(text) == "Attention is all you need"


def test_bibtex_braces_glued_to_the_author_list():
    text = "Haojun Xia, Zhongzhu Zhou, et al.{Quant-LLM}: Accelerating the serving of large language models via{FP6-Centric}{Algorithm-System}{Co-Design}on modern {GPUs}. In2024 USENIX Annual Technical Conference, 2024."
    assert rs._prose_title(text).startswith("Quant-LLM: Accelerating the serving")


def test_line_break_hyphen_dropped_from_a_title():
    assert R.title_similarity("Whitening for selfsupervised representation learning", "Whitening for Self-Supervised Representation Learning") == 1.0
    assert rs._rehyphenate("Allin-one simulation-based inference") == "All-in-one simulation-based inference"
    assert rs._rehyphenate("Do pre-trained visionlanguage models encode object states?") == "Do pre-trained vision-language models encode object states?"


def test_missing_running_head_alone_is_minor():
    assert template_check.is_minor_layout_problem("the running head 'Under review as a conference paper at ICLR 2026' is missing from page 1 (wrong template year, camera-ready mode, or an edited style file)")
    assert template_check.is_minor_layout_problem("the review line numbers in the margin are missing (camera-ready mode or an edited style file)")
    assert not template_check.is_minor_layout_problem("text block is 6.50 in wide; the template's is 5.50 in")


def test_birth_date_of_a_public_figure_warns_and_table_decimals_are_no_phone():
    born = list(sensitive._iter_personal_findings("p.pdf", "Barack Obama was born on 4 August 1961 in Honolulu."))
    assert [f.level for f in born] == [sensitive.LEVEL_WARNING]
    dob = list(sensitive._iter_personal_findings("p.pdf", "Patient 3, DOB: 12/03/1950, presented with"))
    assert [f.level for f in dob] == [sensitive.LEVEL_LIKELY]
    assert not list(sensitive._iter_personal_findings("p.pdf", "Phone 7.5 12.25 30.1 41.0"))
    assert list(sensitive._iter_personal_findings("p.pdf", "participant phone: +1 (510) 642-1234"))


def test_margin_line_numbers_before_the_references_heading_are_furniture(monkeypatch):
    numbers = "\n".join(str(n) for n in range(541, 594))
    pages = ["body " * 400] * 10 + [numbers + "\nUnder review as a conference paper at ICLR 2026\nREFERENCES\nA. Author. A title. 2020."]
    monkeypatch.setattr(manuscript, "_as_pdf", lambda p: p)
    monkeypatch.setattr(manuscript, "_pdf_pages", lambda p: pages)
    assert manuscript.pages_before_references(Path("x.pdf"), ("Acknowledgments",)) == 10


def test_glued_title_words_count_and_an_umlaut_may_be_spelled_out():
    text = "Manuel Gloeckler, Michael Deistler, and Jakob H Macke. Allin-one simulation-based inference. In ICML. PMLR, 2024."
    rec = {"title": "All-in-one simulation-based inference", "author": [{"family": "Glöckler"}, {"family": "Deistler"}, {"family": "Macke"}], "type": "article"}
    assert rs.match_text(rs.Reference(label="r", where="x", text=text), [rec]).status == rs.MATCHED


def test_a_clean_record_of_the_same_work_beats_a_later_repost():
    text = "A. Vaswani, N. Shazeer, and N. Parmar. Attention is all you need. In NeurIPS, 2017."
    people = [{"family": "Vaswani"}, {"family": "Shazeer"}, {"family": "Parmar"}]
    repost = {"title": "Attention Is All You Need", "author": people, "type": "posted-content", "issued": {"date-parts": [[2025]]}}
    paper = {"title": "Attention Is All You Need", "author": people, "type": "article", "issued": {"date-parts": [[2017]]}}
    lookup = rs.match_text(rs.Reference(label="r", where="x", text=text), [repost, paper])
    assert lookup.status == rs.MATCHED and not lookup.problems and lookup.record is paper
