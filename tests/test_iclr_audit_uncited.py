"""bibliography.uncited false alarms from the precision audit of accepted ICLR 2026 papers.

A hand audit (manuscript/iclr_study/data/precision_audit_*.jsonl) found 1 of 10 "reference never
cited" findings correct. The rest were parsing errors, each mirrored here with short synthetic text:

- two entries merged, so the year read was the other entry's (Kang 2024 read as 2014, Grimmett
  2001 as 2025, Eberhardt 2005 as 1960);
- non-entries read as entries: an appendix heading "A P ROOF OF THEOREM 1" after the list, and a
  venue/volume line ("Bio Systems, 37(1-2):147-166, 1996.") or an ISBN line split off its entry;
- compressed year chains ("Liu et al. (2023b; 2022b;a)" cites 2022a);
- a yearless citation ("LoRA (Hu et al.)");
- a surname glued to the word before it ("Chain of ThoughtWei et al. (2022)", "InequalityRinaldo (2018)").

The audit's true positive (an entry that appears only in the reference list) must still fire.
"""

from pathlib import Path

import pytest

from paperpush import bibliography_check as B
from paperpush.validate import Unchecked


class _Doc:
    page_texts = None
    is_docx = False
    is_tex = False
    tex_source = None

    def __init__(self, text: str):
        self.text = text


def _bib(body: str, tail: str, after: str = ""):
    return B._text_bibliography(_Doc(body + "\nREFERENCES\n" + tail + after), Path("x.pdf"))


def _uncited(bib) -> list[str]:
    return [r.label for r in bib.refs if r.key not in bib.cited]


def test_merged_entries_accept_a_citation_to_either_year():
    # Each pair is wrapped so that the first entry ends "... title. YYYY." on its own line.
    tail = (
        "Feiyang Kang, Yifan Sun, and Ruoxi Jia.\n"
        "AutoScale: Automatic prediction of compute-optimal data compositions for training llms. 2024.\n"
        "Diederik P Kingma and Jimmy Ba. Adam: A method for stochastic optimization.arXiv preprint arXiv:1412.6980, 2014.\n"
        "Geoffrey Grimmett and David Stirzaker.Probability and Random Processes. 2001.\n"
        "Ju He, Qihang Yu, and Liang-Chieh Chen. Flowtok: Flowing seamlessly across text and image tokens.CoRR, abs/2503.10772, 2025.\n"
    )
    body = "AutoScale (Kang et al., 2024) with Adam (Kingma & Ba, 2014); total variance (Grimmett & Stirzaker, 2001); FlowTok (He et al., 2025)."
    bib = _bib(body, tail)
    assert _uncited(bib) == []
    assert {r.year for r in bib.refs} >= {"2024", "2014"}  # Kang and Kingma are entries of their own


def test_a_merged_entry_still_matches_on_its_second_year():
    # Whatever the split, a citation to any year an entry holds counts.
    ref = "Frederick Eberhardt, Clark Glymour, and Richard Scheines. On the number of experiments. In UAI, pp. 178-184. AUAI Press, 2005. Paul Erdos and Alfred Renyi. On the evolution of random graphs.Publ. Math., 5:17-61, 1960."
    assert [y.group(0) for y in B.entry_years(ref)][:2] == ["1960", "2005"]


def test_numbered_appendix_heading_ends_the_reference_list():
    tail = (
        "Ulysses Marteau-Ferey, Dmitrii Ostrovskii, and Francis Bach. Beyond least-squares. In COLT, 2019.\n"
        "Werner Zellinger, Stefan Kindermann, and Sergei Pereverzev. Adaptive learning. In NeurIPS, 2023.\n"
    )
    appendix = (
        "A P ROOF OF THEOREM 1\n"
        "Sketch of the proof: First we bound the excess risk using results from Marteau-Ferey et al. (2019).\n"
        "Proof. It has been shown in the proof of Zellinger et al. (2023) that the bound holds.\n"
    )
    bib = _bib("Our setting follows prior work.", tail, appendix)
    assert len(bib.refs) == 2 and _uncited(bib) == []


def test_heading_like_and_proof_lines_are_not_entries():
    assert B._heading_like("A P ROOF OF THEOREM 1 Sketch of the proof: we bound (Smith, 2019).")
    assert B._heading_like("Proof. It has been shown in the proof of Zellinger et al. (2023, 2019)")
    assert not B._heading_like("Anthropic. System card: Claude opus 4 & claude sonnet 4. 2025.")
    assert not B._heading_like("Geoffrey Hinton and Ruslan Salakhutdinov. Reducing dimensionality. Science, 2006.")


def test_venue_volume_and_isbn_lines_stay_with_their_entry():
    tail = (
        "T. Sandholm and R. Crites. Multiagent reinforcement learning in the iterated prisoner's dilemma.\n"
        "Bio Systems, 37(1-2):147-166, 1996.\n"
        "J. Schulman, F. Wolski, and O. Klimov. Proximal policy optimization algorithms. arXiv preprint arXiv:1707.06347, 2017.\n"
        "Amir Emad Ghassami, Saber Salehkaleybar, and Kun Zhang. Learning causal structures using regression invariance. In NeurIPS, volume 30.\n"
        "Curran Associates Inc. ISBN 9781510860964.\n"
        "AmirEmad Ghassami, Negar Kiyavash, and Biwei Huang. Counting and sampling from markov equivalent dags. In AAAI, 2018.\n"
    )
    body = "As in Sandholm & Crites (1996) and Schulman et al. (2017); see also Ghassami et al. (2018)."
    bib = _bib(body, tail)
    labels = [r.label for r in bib.refs]
    assert not any(l.startswith(("Systems", "Inc", "Curran")) for l in labels), labels
    assert "Sandholm (1996)" in labels and _uncited(bib) == []
    assert B._venue_line("Science, 211(4481):453-458, 1981.")
    assert B._venue_line("ISSN 2835-8856. URL https://openreview.net/forum?id=KcmWZSk53y.")
    assert not B._venue_line("Smith, J. and Lee, K. (2015). A title. Journal, 3(2):1-9.")


def test_compressed_suffix_chains_are_expanded():
    tail = (
        "Jiyuan Liu, Xinwang Liu, and Siwei Wang. Late fusion multiple kernel clustering. In AAAI, 2022a.\n"
        "Jiyuan Liu, Xinwang Liu, and En Zhu. Simplemkkm: Simple multiple kernel k-means. TPAMI, 2022b.\n"
        "Suyuan Liu, Siwei Wang, and Xinwang Liu. Contrastive multi-view clustering. In ICML, 2023b.\n"
        "Yalan Qin, Guorui Feng, and Xinpeng Zhang. Block-diagonal clustering. TNNLS, 2023a.\n"
        "Yalan Qin, Nan Pu, and Nicu Sebe. Elastic multi-view clustering. TCSVT, 2025c.\n"
    )
    body = "Multi-view clustering Qin et al. (2021; 2023d;a; 2024b; 2025a;c); Liu et al. (2023b;\n2022b;a). With progress ..."
    bib = _bib(body, tail)
    assert _uncited(bib) == []


def test_yearless_citation_counts_only_for_a_unique_first_author():
    tail = (
        "Edward J. Hu, Yelong Shen, and Weizhu Chen. Lora: Low-rank adaptation of large language models. In ICLR, 2022.\n"
        "Hu Ye, Jun Zhang, and Wei Yang. Ip-adapter: Text compatible image prompt adapter. arXiv preprint arXiv:2308.06721, 2023.\n"
        "Mingyuan Wang, Ali Farhadi, and Ross Girshick. First wang paper. In CVPR, 2020.\n"
        "Xiaolong Wang, Kaiming He, and Abhinav Gupta. Second wang paper. In ICCV, 2021.\n"
    )
    body = "Techniques like LoRA (Hu et al.) and IP-Adapter (Ye et al., 2023). Others build on Wang et al. and more."
    bib = _bib(body, tail)
    # Hu leads one entry (Hu Ye's surname is Ye), so "Hu et al." with no year cites it. Wang leads
    # two, so a yearless "Wang et al." cannot say which, and both stay uncited.
    assert sorted(_uncited(bib)) == ["Wang (2020)", "Wang (2021)"]


def test_surname_glued_to_the_word_before_it():
    tail = (
        "Jason Wei, Xuezhi Wang, and Denny Zhou. Chain-of-thought prompting elicits reasoning. In NeurIPS, 2022.\n"
        "Alessandro Rinaldo. Advanced statistical theory. https://www.stat.cmu.edu/ ~arinaldo/Sep19.pdf, 2018.\n"
        "FıratÖncel, Matthias Bethge, and Cem Subakan. Adaptation odyssey in llms. arXiv preprint arXiv:2406.03036, 2024.\n"
    )
    body = ("allowing the model to do Chain of ThoughtWei et al. (2022), and\n"
            "Lemma 6.(Maximal InequalityRinaldo (2018)) Suppose X1 are sub-Gaussian; and (Ibrahim et al., 2024; ¨Oncel et al., 2024).")
    bib = _bib(body, tail)
    assert _uncited(bib) == []


def test_reference_list_only_entry_is_still_reported():
    # ICLR ZgQnIPG4uV: AdamW appears only in the reference list. Nothing else may hide it.
    tail = (
        "Ilya Loshchilov and Frank Hutter. Decoupled weight decay regularization. In ICLR, 2019.\n"
        "Nathan Srebro and Adi Shraibman. Rank, trace-norm and max-norm. In COLT, 2005.\n"
        "Kaiyang Zhou, Jingkang Yang, and Ziwei Liu. Learning to prompt for vision-language models. IJCV, 2022.\n"
    )
    body = "Shared knowledge (Srebro & Shraibman, 2005) and prompts (Zhou et al., 2022). We train with a weight decay of 0.01."
    bib = _bib(body, tail)
    assert _uncited(bib) == ["Loshchilov (2019)"]


def test_yearless_match_needs_et_al_or_a_coauthor():
    # A bare surname that is also a word or a name in the text is not a citation.
    tail = "Ilya Loshchilov and Frank Hutter. Decoupled weight decay regularization. In ICLR, 2019.\nAda Lovelace. Notes. Journal, 2020.\n"
    bib = _bib("We thank Loshchilov for comments. Lovelace (2020) wrote notes.", tail)
    assert _uncited(bib) == ["Loshchilov (2019)"]


def test_mostly_unmatched_author_year_list_is_unchecked(monkeypatch, tmp_path):
    tail = "".join(f"Author{chr(65 + i)} Surname{chr(65 + i)}x and Coauthor Name. Title number {i}. Journal, 20{10 + i}.\n" for i in range(12))
    body = "Only one citation here (SurnameAx & Name, 2010)."
    bib = _bib(body, tail)
    assert len(bib.refs) == 12 and len(_uncited(bib)) == 11

    class _Field:
        id = "manuscript"

    monkeypatch.setattr(B, "_manuscripts", lambda venue, values: [(_Field(), tmp_path / "x.pdf")])
    monkeypatch.setattr(B, "read_bibliography", lambda path: bib)
    with pytest.raises(Unchecked):
        B._uncited_check(None, {})
    # A healthy list with one uncited entry is still reported.
    few = _bib("(SurnameAx & Name, 2010) and (SurnameBx & Name, 2011).", "".join(tail.splitlines(keepends=True)[:3]))
    monkeypatch.setattr(B, "read_bibliography", lambda path: few)
    outcome = B._uncited_check(None, {})
    assert "1 of 3 entries" in outcome.issues[0].message


def test_citation_across_a_page_break_with_blank_lines():
    # ICLR 3KqASrNJGK: "Ren et al." / page number / blank lines / running head / "(2025)".
    tail = "Richard Ren, Arunim Agarwal, and Dan Hendrycks. The mask benchmark. arXiv preprint arXiv:2503.03750, 2025.\n"
    body = "sycophantic behavior across leading models. Ren et al.\n2\n" + "\n" * 40 + "(2025) find that most honesty benchmarks capture accuracy."
    assert _uncited(_bib(body, tail)) == []


def test_publisher_domain_line_is_not_an_entry():
    # ICLR 90tCp2KszA: "OpenReview.net, 2025. URL ..." on a line of its own.
    tail = (
        "Jane Doe and John Roe. A paper. In The Thirteenth International Conference on Learning Representations, ICLR 2025, Singapore, April 24-28, 2025.\n"
        "OpenReview.net, 2025. URLhttps://openreview.net/forum?id=cRR0oDFEBC.\n"
        "Thomas Moe and Ann Poe. Another paper. In NeurIPS, 2024.\n"
    )
    bib = _bib("See Doe & Roe (2025) and Moe & Poe (2024).", tail)
    assert [r.label for r in bib.refs] == ["Doe (2025)", "Moe (2024)"] and _uncited(bib) == []


def test_given_name_and_middle_initial_label_by_surname():
    # "Edward J. Hu" and "George A Miller" are given-name first; "Erickson AG, ..." (Vancouver) is surname first.
    for entry, surname in [("George A Miller. Wordnet. CACM, 1995.", "Miller"), ("Edward J. Hu, Yelong Shen. Lora. In ICLR, 2022.", "Hu"),
                           ("Erickson AG, Smith J. A title. J, 2020.", "Erickson"), ("Jacob Devlin, Ming-Wei Chang. Bert. 2019.", "Devlin")]:
        assert B._likely_surname(entry, B._surname_candidates(entry)) == surname


def test_capitalized_venue_lines_are_not_entries():
    # ICLR BtpXep9qxC and 5llEo7Hx1k: venue lines that read like a list of capitalized names.
    tail = (
        "Wei Lin and Bo Chen. Edge offloading for mobile inference.\n"
        "IEEE Trans. Mob. Comput., 24(5):4053-4071, 2025.\n"
        "Ann Poe and Tom Moe. Neural rendering. In SIGGRAPH, 2017.\n"
    )
    bib = _bib("As in Lin & Chen (2025) and Poe & Moe (2017).", tail)
    assert [r.label for r in bib.refs] == ["Lin (2025)", "Poe (2017)"] and _uncited(bib) == []
    assert B._VENUE_OPENING.search("Graph.36, 6, Article 176 (Nov. 2017), 14 pages.")


def test_model_name_glued_to_citation_publisher_line_and_lone_parenthesized_author():
    # ICLR 9U51rOnGko ("Llama-3-8BDubey et al. (2024)"), P8RzsaEN2s ("American Mathematical Soc., 2004."),
    # CVqYCYpq75 ("architectures from (Chen)").
    tail = (
        "Abhimanyu Dubey, Abhinav Jauhri, and Abhinav Pandey. The llama 3 herd of models. arXiv preprint arXiv:2407.21783, 2024.\n"
        "Bennett Chow and Dan Knopf. The Ricci Flow: An Introduction, volume 1.\n"
        "American Mathematical Soc., 2004.\n"
        "Yaofo Chen. Pytorch cifar models. https://github.com/chenyaofo/pytorch-cifar-models, 2021.\n"
    )
    body = "zero-shot inference withLlama-3-8BDubey et al. (2024); Ricci flow (Chow & Knopf, 2004); architectures from (Chen): ResNet-20."
    bib = _bib(body, tail)
    # The yearless Chow line merges with the entry before (author_year_starts); the publisher line
    # must join them rather than stand as "Soc (2004)", and the merged entry is cited by either year.
    assert not any(r.label.startswith(("Soc", "American")) for r in bib.refs) and _uncited(bib) == []
    short = _bib("the TWu et al. (2020) baseline.", "Xia Wu and Bo Li. Short names. In ICML, 2020.\n")
    assert _uncited(short) == ["Wu (2020)"]  # a two-letter surname after a capital may be an acronym's tail
