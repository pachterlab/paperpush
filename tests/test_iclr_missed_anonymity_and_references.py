"""Regressions from the desk rejections validate missed in the ICLR 2026 study (manuscript/iclr_study, run v8).

Anonymity (no author list in the .sub, so only the generic checks ran):
1. Page 1 printed the author block (names, affiliations, e-mails) or the camera-ready
   running head, and nothing looked at the title block.
2. A run-in "Acknowledgments This work used ..." heading, and a funding statement with no
   heading at all, went unreported.
3. "extends our previous MOS prediction model (Elelu et al.)" and "our original Perch model"
   escaped the self-reference pattern, which wanted the noun right after "previous".
4. gitee/gitlab links, a GitHub URL wrapped after "github.com/", and <owner>.github.io pages.

References (looked up and accepted as verified):
5. Only family names were compared: "Ethan Perez" passed as Fabio Perez, "Shikun Liu" as Bo Liu.
6. A long fabricated author list full of common surnames counted as "the same people" as
   another paper's, so a wrong arXiv id passed (AnyBench cited with AppAgent's id).
7. An encyclopedia entry titled "Bonferroni, Carlo Emilio" stood in for Bonferroni's paper.
"""

from paperpush import anonymity as A
from paperpush import reference_search as R


def _details(text: str) -> list[str]:
    return [f.detail for f in A.scan_text("paper.pdf", text, [])]


# --- anonymity -------------------------------------------------------------------------

def test_title_block_with_names_and_affiliations_is_reported():
    page = "Under review as a conference paper at ICLR 2026\nRIGHT ANSWER AT THE RIGHT TIME\nZulun Zhu, Haoyu Liu, Siqiang Luo\nNanyang Technological University\nSchool of Computer Science\nABSTRACT\nQuestion answering ..."
    assert any("page 1 names institutions" in f.detail for f in A.title_block_findings("p.pdf", page))


def test_title_block_with_an_email_is_reported():
    page = "A TITLE\nIssei Sato\nThe University of Tokyo\nsato@g.ecc.u-tokyo.ac.jp\nABSTRACT\n..."
    assert any("e-mail address" in f.detail for f in A.title_block_findings("p.pdf", page))


def test_camera_ready_running_head_is_reported():
    page = "Published as a conference paper at ICLR 2026\nCONCEPT ATTRACTORS IN LLMS\n..."
    assert any("camera-ready running head" in f.detail for f in A.title_block_findings("p.pdf", page))


def test_anonymous_title_block_is_not_reported():
    page = "Under review as a conference paper at ICLR 2026\nA TITLE ABOUT THE UNIVERSITY ADMISSIONS PROBLEM\nAnonymous authors\nPaper under double-blind review\nABSTRACT\nWe study data from Stanford University and Google ..."
    assert list(A.title_block_findings("p.pdf", page)) == []


def test_run_in_acknowledgments_heading_is_reported():
    text = "Body.\nAcknowledgments This work used the Delta system at NCSA through allocation CIS250145.\nREFERENCES\n"
    assert any("Acknowledgments section" in d for d in _details(text))


def test_funding_statement_without_heading_is_reported():
    text = "Body text.\nThis work was facilitated by the Institute, supported in part by Alfred P. Sloan Foundation grant G-2017-9890.\nREFERENCES\n"
    assert any("funding statement" in d for d in _details(text))


def test_funding_in_the_reference_list_is_not_reported():
    text = "Body text.\nREFERENCES\nA. Author. A study supported in part by NSF grant 123. Journal, 2020.\n"
    assert not any("funding statement" in d for d in _details(text))


def test_self_reference_with_words_between_and_a_citation():
    assert any("first-person reference" in d for d in _details("we introduce X that extends our previous MOS prediction model (Elelu et al., a;b).\n"))
    assert any("first-person reference" in d for d in _details("this is larger than our original Perch model (which used EfficientNet).\n"))


def test_our_previous_section_is_not_a_self_reference():
    assert not any("first-person reference" in d for d in _details("As shown in our previous section, the loss drops. In our experiments we vary it.\n"))


def test_other_forges_wrapped_urls_and_github_pages():
    text = "Code is at https://gitee.com/dsdsdshe/qrl. We place the code at https://github.com/\nDeepScene-Generation/scene-cot. Project page: https://dingning-liu.github.io/bridge/.\nREFERENCES\nBlack Forest Labs. Flux. https://github.com/black-forest-labs/flux, 2024.\n"
    details = _details(text)
    assert any("gitee.com/dsdsdshe" in d for d in details)
    assert any("github.com/DeepScene-Generation/scene-cot" in d for d in details)
    assert any("dingning-liu.github.io" in d for d in details)
    assert not any("black-forest-labs" in d for d in details)  # cited in the reference list


# --- references ------------------------------------------------------------------------

def _rec(names, title="T"):
    return {"title": title, "author": [{"family": n.split()[-1], "literal": n} for n in names]}


def test_same_surname_different_first_name_is_wrong_author():
    p = R._lead_given_conflict("Ethan Perez, Marco Tulio Ribeiro, et al. Ignore previous prompt. 2022.", _rec(["Fábio Perez", "Ian Ribeiro"]))
    assert p.startswith(R.WRONG_AUTHORS)
    assert R._lead_given_conflict("Shikun Liu, Edward Johns, and Andrew J. Davison. Conflict-averse gradient descent.", _rec(["Bo Liu", "Xingchao Liu"]))


def test_initials_reversed_order_and_transliterations_agree():
    for text, names in [
        ("F. Perez and I. Ribeiro. Ignore previous prompt.", ["Fábio Perez"]),
        ("Perez, F. and Ribeiro, I. Ignore previous prompt.", ["Fábio Perez"]),
        ("Liu Bo and Xingchao Liu. Conflict-averse gradient descent.", ["Bo Liu"]),
        ("Jürgen Schmidhuber. Deep learning in neural networks.", ["Juergen Schmidhuber"]),
        ("Wen-tau Yih and Ming-Wei Chang. Semantic parsing.", ["Wen-tau Yih"]),
    ]:
        assert R._lead_given_conflict(text, _rec(names)) == "", text
    assert R._lead_given_conflict("Diederik P. Kingma and Jimmy Ba. Adam.", {"author": [{"given": "Diederik P.", "family": "Kingma"}]}) == ""


def test_arxiv_id_of_another_paper_with_overlapping_common_surnames():
    ref = R.Reference(label="reference 'Yang (2023)'", where="p.pdf", text="Tong Yang, Xiaodan Hu, Jingfeng Zhang, Zhilin Li, and Jason Liu. AnyBench: Language models evaluate anything. arXiv preprint arXiv:2312.13771, 2023.", arxiv_id="2312.13771")
    lk = R.match_arxiv(ref, _rec(["Chi Zhang", "Zhao Yang", "Jiaxuan Liu", "Yucheng Li"], "AppAgent: Multimodal Agents as Smartphone Users"))
    assert lk.problems and "AppAgent" in lk.problems[0]


def test_arxiv_match_with_another_first_name_is_reported():
    ref = R.Reference(label="reference 'Perez (2022)'", where="p.pdf", text="Ethan Perez, Marco Tulio Ribeiro, et al. Ignore previous prompt: Attack techniques for language models. arXiv preprint arXiv:2211.09527, 2022.", arxiv_id="2211.09527")
    lk = R.match_arxiv(ref, _rec(["Fábio Perez", "Ian Ribeiro"], "Ignore Previous Prompt: Attack Techniques For Language Models"))
    assert lk.problems and "Fabio Perez" in lk.problems[0]


def test_encyclopedia_entry_about_a_person_is_not_a_candidate():
    assert not R._is_candidate({"title": "Bonferroni, Carlo Emilio", "type": "reference-entry"})
    assert R._is_candidate({"title": "Bayes' theorem", "type": "reference-entry"})


def test_name_list_above_a_single_affiliation_is_reported():
    page = "Under review as a conference paper at ICLR 2026\nRIGHTANSWER AT THERIGHTTIME\nZulun Zhu, Haoyu Liu, Mengke He, Siqiang Luo\nNanyang Technological University\nABSTRACT\n..."
    assert any("page 1 names institutions" in f.detail for f in A.title_block_findings("p.pdf", page))
    title_only = "A STUDY OF GRAPHS\nGraph Neural Networks for University Admissions\nABSTRACT\n..."
    assert list(A.title_block_findings("p.pdf", title_only)) == []


def test_our_preliminary_study_is_a_pilot_not_a_self_reference():
    assert not any("first-person reference" in d for d in _details("In our preliminary study on CLIP-based MoE learners, a result emerged.\n"))


def test_preferred_given_name_and_merged_entries_are_not_wrong_authors():
    assert R._lead_given_conflict("Zhenyu Chen, Ronghang Hu, Xinlei Chen. UniT3D: A unified transformer.", {"author": [{"given": "Dave Zhenyu", "family": "Chen"}]}) == ""
    merged = "Haokun Lin, Haobo Xu, and Yichen Wu. Quantized LLMs. Ji Lin, Chuang Gan, and Song Han. Defensive Quantization, 2019. arXiv:1904.08444."
    assert R._lead_given_conflict(merged, _rec(["Ji Lin", "Chuang Gan", "Song Han"])) == ""


def test_authorless_records_and_other_titles_do_not_verify_a_prose_entry():
    ref = R.Reference(label="reference 'Zhang (2021)'", where="p.pdf", text="Zaixi Zhang, Jinyuan Zhang, Hang Wang, Zhaohan Liu, and Chaochao Zhou. Graphbackdoor: Backdoor attacks on graph neural networks. In Usenix Security Symposium, 2021.")
    assert R.match_text(ref, [{"title": "Usenix Security Symposium", "author": [], "type": "proceedings-article"}]).status != R.MATCHED
    ref2 = R.Reference(label="reference 'Agesen (1994)'", where="p.pdf", text="Omar Agesen. Type inference in the presence of first-class polymorphism and subtyping. In SAS / PLDI-era papers on constraint-based analysis, 1994.")
    other = {"title": "Constraint-based type inference and parametric polymorphism", "author": [{"family": "Agesen", "given": "Ole"}], "type": "book-chapter"}
    assert R.match_text(ref2, [other]).status != R.MATCHED
    real = R.Reference(label="reference 'Vaswani (2017)'", where="p.pdf", text="Ashish Vaswani, Noam Shazeer, Niki Parmar, and Jakob Uszkoreit. Attention is all you need. In Advances in Neural Information Processing Systems, 2017.")
    rec = {"title": "Attention is all you need", "author": [{"family": "Vaswani", "given": "Ashish"}, {"family": "Shazeer", "given": "Noam"}, {"family": "Parmar", "given": "Niki"}, {"family": "Uszkoreit", "given": "Jakob"}], "type": "proceedings-article"}
    assert R.match_text(real, [rec]).status == R.MATCHED


def test_a_title_run_into_the_venue_still_matches():
    ref = R.Reference(label="reference 'Liu (2023)'", where="p.pdf", text="Haotian Liu, Chunyuan Li, Qingyang Wu, and Yong Jae Lee. Visual instruction tuning.Advances in Neural Information Processing Systems, 36:34892-34916, 2023.")
    rec = {"title": "Visual Instruction Tuning", "author": [{"family": "Liu", "given": "Haotian"}, {"family": "Li", "given": "Chunyuan"}, {"family": "Wu", "given": "Qingyang"}, {"family": "Lee", "given": "Yong Jae"}], "type": "proceedings-article"}
    assert R.match_text(ref, [rec]).status == R.MATCHED
