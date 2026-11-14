"""Regressions found by validating ICLR 2026 desk-rejected submissions (manuscript/iclr_study).

1. natbib author-year reference lists (ICLR, NeurIPS, ICML, *ACL), where the year
   closes the entry, were folded into a handful of entries, so most references were
   never looked up and a fabricated one went unreported.
2. Running heads inside the reference list became entries of their own.
3. A venue's main_text_end_headings missed small-caps headings ("E THICS S TATEMENT")
   and run-in ones ("Ethics Statement: ..."), and a reference list starting at the top
   of a page counted that page as main text for a venue without such headings.
"""

from pathlib import Path

from paperpush import bibliography_check as B
from paperpush import manuscript as M
from paperpush import reference_search as _rs

# conftest stubs _Service.get out for every test; this one needs the real method.
_REAL_SERVICE_GET = _rs._Service.get

from pdf_builder import text_line, write_pdf

HEAD = "Under review as a conference paper at ICLR 2026"


def _page(lines: list[str], head: str = HEAD) -> str:
    out = text_line(108, 740, head)
    y = 700
    for line in lines:
        out += text_line(108, y, line)
        y -= 12
    return out


NATBIB = [
    "Yuan Gong, Yu-An Chung, and James Glass. Ast: Audio spectrogram transformer. In Proc.",
    "Interspeech, pp. 571-575, 2021.",
    "Jinhua Liang, Huy Phan, and Emmanouil Benetos. Learning from taxonomy: Multi-label fewshot",
    "classification for everyday sound recognition. In ICASSP 2024-2024 IEEE International",
    "Conference on Acoustics, Speech and Signal Processing (ICASSP), pp. 771-775. IEEE, 2024.",
    "Tsung-Yi Lin, Priya Goyal, Ross Girshick, Kaiming He, and Piotr Dollar. Focal loss for dense",
    "object detection. In Proceedings of the IEEE international conference on computer vision, pp.",
    "2980-2988, 2017.",
    "Yupeng Lu, Yingqiang Ge, John Hershey, Abdelrahman Mohamed, and Chao Wang. Spectrogramgraph:",
    "Spectrogram-based graph neural network for weakly supervised audio event detection.",
    "In IEEE International Conference on Acoustics, Speech and Signal Processing (ICASSP), pp.",
    "911-915, 2022.",
    "Jake Snell, Kevin Swersky, and Richard Zemel. Prototypical networks for few-shot learning. In",
    "Advances in Neural Information Processing Systems, 2017.",
]


def test_natbib_entries_are_split_one_per_reference(tmp_path: Path):
    body = ["1 INTRODUCTION", "Prior work (Gong et al., 2021; Liang et al., 2024; Lin et al., 2017) and",
            "Lu et al. (2022) and Snell et al. (2017) study this."]
    pdf = write_pdf(tmp_path / "p.pdf", [_page(body), _page(body), _page(["REFERENCES"] + NATBIB[:8]), _page(NATBIB[8:])])
    bib = B.read_bibliography(pdf)
    assert bib is not None
    labels = [r.label for r in bib.refs]
    assert labels == ["Gong (2021)", "Liang (2024)", "Lin (2017)", "Lu (2022)", "Snell (2017)"], labels
    # The running head on the last page is not an entry, nor glued onto one.
    assert not any("Under review" in r.authors for r in bib.refs)


def test_author_list_head():
    assert B._author_list_head("Jort F. Gemmeke, Daniel P. W. Ellis, and Marvin Ritter. Audioset: an ontology, 2017.")
    assert B._author_list_head("Martha White. Unifying task specification in reinforcement learning. In ICML, 2017.")
    assert not B._author_list_head("Conference on Acoustics, Speech and Signal Processing (ICASSP), pp. 771-775. IEEE, 2024.")
    assert B._runs_on("Conference on Acoustics, Speech and Signal Processing (ICASSP), pp. 771-775. IEEE, 2024.")
    assert not B._runs_on("Jake Snell, Kevin Swersky, and Richard Zemel. Prototypical networks, 2017.")


def test_venue_headings_match_small_caps_and_run_in():
    pat = M._extra_heading_pattern(("Ethics Statement", "The Use of Large Language Models (LLMs)"))
    assert pat.search("E THICS S TATEMENT")
    assert pat.search("ETHICSSTATEMENT")
    assert pat.search("F T HE USE OF LARGE LANGUAGE MODELS (LLM S)")
    assert pat.search("Ethics Statement: Discussion on limitations is in Appendix E.")
    assert not pat.search("We thank the Ethics Statement board for comments.")
    assert not pat.search("Ethics Statement of the board was approved")


def test_reference_list_at_top_of_page_does_not_count_that_page(tmp_path: Path):
    prose = ["Main text " * 8] * 20
    pages = [_page(prose) for _ in range(9)] + [_page(["REFERENCES"] + NATBIB)]
    pdf = write_pdf(tmp_path / "nine.pdf", pages)
    assert M.pages_before_references(pdf, ("Ethics Statement",)) == 9
    run_in = [_page(prose) for _ in range(9)] + [_page(["Ethics Statement: Limitations are discussed in Appendix E.", "REFERENCES"] + NATBIB)]
    pdf = write_pdf(tmp_path / "run_in.pdf", run_in)
    assert M.pages_before_references(pdf, ("Ethics Statement",)) == 9


def test_iclr_entries_name_their_excluded_sections():
    import json

    reqs = json.loads((Path(M.__file__).parent / "manuscript_requirements.json").read_text())
    for slug in ("iclr_2026", "iclr_2027"):
        headings = reqs[slug]["manuscript"].get("main_text_end_headings") or []
        assert "Ethics Statement" in headings and "Reproducibility Statement" in headings, slug


def test_surname_guess_skips_et_al_and_single_authors():
    assert B._surname_candidates("Maximilian Ankner et al. Ensembling llm-judges for reference-free evaluation, 2024.")[-1] == "Ankner"
    assert B._surname_candidates("Abraham H Maslow. A theory of human motivation. Psychological review, 50(4):370-396, 1943.")[-1] == "Maslow"
    assert B._surname_candidates("Smith, J. (2015). A title. Journal.") == ["Smith"]


def test_wrapped_identifier_lines_do_not_start_entries():
    tail = ("Jake Snell, Kevin Swersky, and Richard Zemel. Prototypical networks for few-shot learning. SIAM J., 2017. doi:\n"
            "S0895479803436652.\n"
            "Yuan Gong, Yu-An Chung, and James Glass. Ast: Audio spectrogram transformer, 2021.\n"
            "URLhttps://example.org/ast.\n"
            "Jinhua Liang and Huy Phan. Learning from taxonomy. In ICASSP, 2024.\n")
    starts = M.author_year_starts(tail)
    assert [tail[i:i + 4] for i in starts] == ["Jake", "Yuan", "Jinh"]


def test_capitalized_lettered_appendix_heading_ends_the_reference_list():
    assert M._AFTER_REFERENCES.search("A EXPERIMENTAL SETUP")
    assert not M._AFTER_REFERENCES.search("REFERENCES")
    assert not M._AFTER_REFERENCES.search("Smith J. Title of the work")
    assert not M._AFTER_REFERENCES.search("A Survey of methods, 2020.")


def test_run_in_heading_with_a_period():
    pat = M._extra_heading_pattern(("Ethics Statement", "Reproducibility"))
    assert pat.search("Ethics Statement.Our work concerns public data.")
    assert pat.search("Reproducibility. We release all code.")
    assert not pat.search("Reproducibility of results is hard to guarantee.")


def test_rate_limited_lookups_are_waited_out_not_given_up(monkeypatch):
    """A batch of 429s from Crossref took the service down for the rest of a run, so most
    references went unsearched ("the registries stopped answering")."""
    import io
    import urllib.error

    from paperpush import reference_search as rs

    calls = {"n": 0}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "0"}, None)
        return Resp(b'{"ok": true}')

    monkeypatch.setattr(rs._Service, "get", _REAL_SERVICE_GET)
    monkeypatch.setattr(rs.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(rs.time, "sleep", lambda s: None)
    svc = rs._Service("Crossref", budget=10)
    assert svc.get_json("https://api.crossref.org/works?query=x") == {"ok": True}
    assert svc.available and svc.failures == 0


_RESNET = {"DOI": "10.1109/cvpr.2016.90", "type": "proceedings-article", "title": ["Deep Residual Learning for Image Recognition"],
           "author": [{"family": "He", "given": "Kaiming"}, {"family": "Zhang", "given": "Xiangyu"}, {"family": "Ren", "given": "Shaoqing"}, {"family": "Sun", "given": "Jian"}],
           "issued": {"date-parts": [[2016]]}}
_SUBSET = {"DOI": "10.1/x", "type": "proceedings-article", "title": ["On the Difficulty of Membership Inference Attacks"],
           "author": [{"family": "Rezaei", "given": "S"}, {"family": "Liu", "given": "X"}], "issued": {"date-parts": [[2021]]}}


def _ref(text):
    return _rs.Reference(label="reference 'x'", where="p.pdf", text=text)


def test_a_real_title_under_invented_authors_is_reported_outright():
    """ICLR 2026 desk-rejected papers cited real titles under fabricated author lists."""
    lk = _rs.match_text(_ref("Alice Smith and Robert Jones. Deep residual learning for image recognition. In CVPR, pp. 770-778, 2016."), [_RESNET])
    assert lk.status == _rs.MATCHED and lk.problems[0].startswith(_rs.WRONG_AUTHORS)


def test_a_correct_entry_has_no_problems():
    lk = _rs.match_text(_ref("Kaiming He, Xiangyu Zhang, Shaoqing Ren, and Jian Sun. Deep residual learning for image recognition. In CVPR, 2016."), [_RESNET])
    assert lk.status == _rs.MATCHED and lk.problems == []
    # "et al." holds the entry to its first author only.
    lk = _rs.match_text(_ref("Kaiming He et al. Deep residual learning for image recognition. In CVPR, 2016."), [_RESNET])
    assert lk.problems == []


def test_a_wrong_year_on_a_matching_entry_is_a_problem():
    lk = _rs.match_text(_ref("Kaiming He, Xiangyu Zhang, Shaoqing Ren, and Jian Sun. Deep residual learning for image recognition. In CVPR, 2011."), [_RESNET])
    assert any("year" in p for p in lk.problems)


def test_a_shorter_title_contained_in_the_entry_is_not_its_work():
    text = "Lauren Watson, Chuan Guo, Graham Cormode, and Alexandre Sablayrolles. On the importance of difficulty calibration in membership inference attacks. In ICLR, 2022."
    lk = _rs.match_text(_ref(text), [_SUBSET])
    assert not any(p.startswith(_rs.WRONG_AUTHORS) for p in lk.problems)


def test_a_title_one_key_word_apart_is_not_a_match():
    """'On the Weaknesses of RL for NMT' (cited under the wrong authors and year) was taken for 'A Study of RL for NMT'."""
    study = {"DOI": "10.18653/v1/d18-1397", "type": "proceedings-article", "title": ["A Study of Reinforcement Learning for Neural Machine Translation"],
             "author": [{"family": "Wu", "given": "Lijun"}, {"family": "Tian", "given": "Fei"}, {"family": "Qin", "given": "Tao"}], "issued": {"date-parts": [[2018]]}}
    text = "Lijun Wu, Fei Tian, Tao Qin, Jianhuang Lai, and Tie-Yan Liu. On the weaknesses of reinforcement learning for neural machine translation. In EMNLP, 2018."
    assert _rs.match_text(_ref(text), [study]).status != _rs.MATCHED
    same = "Lijun Wu, Fei Tian, Tao Qin, Jianhuang Lai, and Tie-Yan Liu. A study of reinforcement learning for neural machine translation. In EMNLP, 2018."
    assert _rs.match_text(_ref(same), [study]).status == _rs.MATCHED


def test_an_arxiv_paper_retitled_since_is_not_a_wrong_identifier():
    record = {"title": "Erasing More Than Intended? How Concept Erasure Degrades the Generation of Non-Target Concepts",
              "author": [{"family": "Amara", "literal": "Ibtihel Amara"}, {"family": "Humayun", "literal": "Ahmed Imtiaz Humayun"}, {"family": "Kajic", "literal": "Ivana Kajic"}],
              "issued": {"date-parts": [[2025]]}}
    text = "Ibtihel Amara, Ahmed Imtiaz Humayun, Ivana Kajic, et al. Erasebench: Understanding the ripple effects of concept erasure techniques. arXiv preprint arXiv:2501.09833, 2025."
    ref = _rs.Reference(label="x", where="p.pdf", text=text, arxiv_id="2501.09833")
    assert _rs.match_arxiv(ref, record).problems == []
    other = _rs.Reference(label="x", where="p.pdf", text="Alice Smith and Bob Jones. Erasebench: concept erasure. arXiv:2501.09833, 2025.", arxiv_id="2501.09833")
    assert _rs.match_arxiv(other, record).problems


def test_et_al_ends_an_author_list():
    assert B._author_list_head("Zhichao Wang, Bin Bi, Shiva Kumar Pentyala, et al. A comprehensive survey of llm alignment techniques. arXiv, 2024.")
    assert _rs._prose_title("Wei Chen et al. Instantid: Fast and accurate identity preservation. arXiv, 2023.").startswith("Instantid")


def test_right_first_author_with_other_coauthors_is_only_a_near_miss():
    horton = {"DOI": "10.3386/w31122", "type": "report", "title": ["Large Language Models as Simulated Economic Agents: What Can We Learn from Homo Silicus?"],
              "author": [{"family": "Horton"}, {"family": "Filippas"}, {"family": "Manning"}], "issued": {"date-parts": [[2023]]}}
    text = "John J Horton. Large language models as simulated economic agents: What can we learn from homo silicus? Technical report, National Bureau of Economic Research, 2023."
    lk = _rs.match_text(_ref(text), [horton])
    assert lk.problems and not any(p.startswith(_rs.WRONG_AUTHORS) for p in lk.problems)


def test_split_accents_are_rejoined():
    assert B.join_split_accents("Stefan Schl ¨uter and Piotr Doll ´ar") == "Stefan Schlüter and Piotr Dollár"


def test_a_reference_heading_in_capitals_does_not_end_its_own_list():
    """A capitals-only appendix rule matched "REFERENCES" itself, so no DOI counted as in the list."""
    from paperpush import references as R

    text = "Main text.\nREFERENCES\nAlice Smith. A title. Journal, 2020. doi: 10.1000/xyz123.\nA EXPERIMENTAL SETUP\nMore."
    start = R._reference_list_start(text)
    assert "10.1000/xyz123" in text[start:R._reference_list_end(text, start)]


def test_a_question_in_a_title_does_not_end_it():
    assert _rs._prose_title("Yifei Yuan and Lei Li. Do llms overcome shortcut learning? an evaluation of shortcut challenges. In EMNLP, 2024.").startswith("Do llms overcome shortcut learning? an evaluation")


def test_an_enumerated_list_in_an_appendix_is_not_a_numbered_reference_list():
    names = ["Alice", "Brian", "Carla", "David", "Erin", "Frank", "Grace", "Henry", "Irene", "James", "Karen", "Louis"]
    tail = "".join(f"{n} Smith and Bob Jones. A title about topic {n.lower()} in learning. In Proc. Conf., 20{10 + i}.\n" for i, n in enumerate(names))
    tail += "A.1 Statistics\n1. Overall accuracies are higher.\n2. The models that perform well keep dominating.\n"
    assert M._AFTER_REFERENCES.search("A.1 SemBench Statistics")
    assert not M._AFTER_REFERENCES.search("Smith, A.1 et al")
    from paperpush import document

    class Doc:
        text = "Body text cites Smith (2010).\nREFERENCES\n" + tail
        page_texts = None
        is_docx = False

    bib = B._text_bibliography(Doc(), Path("x.pdf"))
    assert bib.mode == "author-year" and len(bib.refs) >= 10


def test_listed_authors_are_checked_even_with_et_al():
    recitation = {"DOI": "10.1/r", "type": "proceedings-article", "title": ["Recitation-Augmented Language Models"],
                  "author": [{"family": f} for f in ("Sun", "Wang", "Tay", "Yang", "Zhou")], "issued": {"date-parts": [[2023]]}}
    fake = "David Sun, Jasmine Jiang, Terry Nguyen, James Tsai, Yizhe Xia, Kyunghyun Cho, et al. Recitation-augmented language models. In ICLR, 2023."
    real = "Zhiqing Sun, Xuezhi Wang, Yi Tay, et al. Recitation-augmented language models. In ICLR, 2023."
    assert _rs.match_text(_ref(fake), [recitation]).problems
    assert _rs.match_text(_ref(real), [recitation]).problems == []


def test_a_short_title_matches_when_the_entry_has_that_title():
    attention = {"DOI": None, "type": "preprint", "title": ["Attention Is All You Need"],
                 "author": [{"family": f} for f in ("Vaswani", "Shazeer", "Parmar")], "issued": {"date-parts": [[2017]]}}
    text = "Ashish Vaswani, Noam Shazeer, and Niki Parmar. Attention is all you need. In NeurIPS, 2017."
    assert _rs.match_text(_ref(text), [attention]).status == _rs.MATCHED


def test_a_prose_entry_found_nowhere_is_reported_when_it_claims_a_journal(monkeypatch):
    monkeypatch.setattr(_rs, "crossref_search", lambda q, s: [])
    monkeypatch.setattr(_rs, "openalex_search", lambda q, s: [])
    svc = _rs._Services(*(_rs._Service(n, 10) for n in ("Crossref", "OpenAlex", "arXiv", "bioRxiv")))
    entry = "Alice Smith and Bob Jones. A wholly invented study of nothing at all. IEEE Transactions on Imaginary Systems, 2023."
    res = _rs.search_references([_ref(entry)], services=svc)
    assert res.findings and "matches no work" in res.findings[0].detail
    software = "Alice Smith. A wholly invented toolkit for nothing at all. GitHub repository, 2023."
    assert not _rs.search_references([_ref(software)], services=svc).findings


def test_entry_year_skips_arxiv_ids_page_ranges_and_dois():
    assert B.entry_year("Tom Brown et al. Language models are few-shot learners. NeurIPS, 33:1877-1901, 2020.").group(0) == "2020"
    assert B.entry_year("Alexey Dosovitskiy et al. An image is worth 16x16 words. arXiv preprint arXiv:2010.11929, 2020.").group(0) == "2020"
    assert B.entry_year("Smith, J. (2015). A title from 2012. Journal, 3:1999-2001.").group(0) == "2015"
