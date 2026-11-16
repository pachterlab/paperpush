"""False alarms found by validating recent arXiv cs.AI papers (2611.17338-17501, Nov 2026).

Each test reproduces, with a short excerpt of the extracted text, one reading error that
put a warning or a likely error on a paper that did not deserve it:

1. bibliography.uncited (author-year PDFs): an ACL entry's title line after "... Qiu. 2023."
   started a new entry and swallowed the next one; a long yearless venue line did the same;
   ICML's "Surname, I." lists ending in an initial ("Kiela, D. Retrieval ...") were not read
   as author lists; a page number split an entry; "V ." kerning hid an initial; and a section
   after the references ("AI USAGE DISCLOSURE") ran into the last entry.
2. integrity.dois: "doi: 10.1109/" + page number "4" + running head + "TBDATA..." was read as
   the DOI 10.1109/4; a Crossref record titled "LEAP" (the rest in its subtitle) was reported
   as not matching "LEAP: Learning to Prescribe ...".
3. integrity.hidden_text: 1.4 pt tick labels in a grid of heatmaps were "hidden text".
4. citations.missing_captions / citations.table: captions right after a running head, and
   "Table 1.Main results" with no space, were not read as captions.
5. front_matter.contact_email: "{a.smith, b.jones}@lboro.ac.uk" was not an e-mail address.
6. prose.spelling / prose.repeated_words: "R dout×r", "singl e", a column-broken "ment",
   and axis labels ("anli anli anli arc arc ...", "lb lb = 1") were typos.
7. prose.acronyms: NVIDIA/AMD were undefined acronyms; "signalto-noise" (a lost hyphen) was a
   second meaning of QSNR.
8. LaTeX source: \\twocolumn[ (ICML/MLSys title block), a \\footnote inside parentheses, and
   \\tikzset styles broke the bracket and punctuation checks; [disable]{todonotes} was a to-do;
   \\href escapes ("\\%27", "\\#") made a live link 404; "55$\\times$" differed from "55x".
"""

from pathlib import Path

from paperpush import bibliography_check as B
from paperpush import citation_check as C
from paperpush import document
from paperpush import prose_check as P
from paperpush import references as R
from paperpush.figure_check import _merge_drawings
from paperpush.front_matter_check import emails_in
from paperpush.manuscript import _AFTER_REFERENCES, author_year_starts
from paperpush.placeholders import _TEX_TODO_PACKAGES
from paperpush.sensitive import _find_urls
from paperpush.style_check import _meaning_key
from paperpush.validate import WARNING, Issue


class _Doc:
    page_texts = None
    is_docx = False

    def __init__(self, text: str):
        self.text = text


def _bib(body: str, tail: str):
    return B._text_bibliography(_Doc(body + "\nReferences\n" + tail), Path("x.pdf"))


def _uncited(bib) -> list[str]:
    return [r.label for r in bib.refs if r.key not in bib.cited]


# --- 1. reference lists ----------------------------------------------------------------------


def test_acl_title_line_after_the_year_does_not_start_an_entry():
    tail = (
        "Huiqiang Jiang, Qianhui Wu, Xufang Luo, Dongsheng\n"
        "Li, Chin-Yew Lin, Yuqing Yang, and Lili Qiu. 2023.\n"
        "Longllmlingua: Accelerating and enhancing llms\n"
        "in long context scenarios via prompt compression.\n"
        "arXiv preprint arXiv:2310.06839.\n"
        "Kai Li, Xuanqing Yu, Ziyi Ni, Yi Zeng, Yao Xu, Zheqing Zhang, Xin Li, Jitao Sang, Xiaogang Duan,\n"
        "Xuelei Wang, Chengbao Liu, and Jie Tan. 2026.\n"
        "Timem: Temporal-hierarchical memory consolidation. arXiv\n"
        "preprint arXiv:2601.02845.\n"
    )
    starts = author_year_starts(tail)
    assert [tail[a:a + 8] for a in starts] == ["Huiqiang", "Kai Li, "]
    bib = _bib("LongLLMLingua (Jiang et al., 2023) and TiMem (Li et al., 2026).", tail)
    assert [r.label for r in bib.refs] == ["Jiang (2023)", "Li (2026)"] and _uncited(bib) == []


def test_long_venue_line_does_not_keep_the_next_authors_in_its_entry():
    tail = (
        "Gabriel Ilharco, Marco Tulio Ribeiro, Mitchell Wortsman, Ludwig Schmidt, Hannaneh Hajishirzi, and Ali\n"
        "Farhadi. 2023. Editing Models with Task Arithmetic.\n"
        "InInternational Conference on Learning Representations.\n"
        "Ganesh Jawahar, Benoît Sagot, and Djamé Seddah.\n"
        "2019. What does BERT learn about the structure of\n"
        "language? In Proceedings of ACL.\n"
    )
    bib = _bib("Task arithmetic (Ilharco et al., 2023) and probing (Jawahar et al., 2019).", tail)
    assert [r.label for r in bib.refs] == ["Ilharco (2023)", "Jawahar (2019)"] and _uncited(bib) == []


def test_icml_author_list_ending_in_an_initial_page_numbers_and_kerned_initials():
    tail = (
        "Johnson, J., Douze, M., and Jégou, H. Billion-scale\n"
        "similarity search with GPUs.IEEE Transactions\n"
        "on Big Data, 7(3):535-547, 2021. doi: 10.1109/\n"
        "4\n"
        "TBDATA.2019.2921572.\n"
        "Karpukhin, V ., Oguz, B., Min, S., Lewis, P., Wu, L., Edunov,\n"
        "S., Chen, D., and Yih, W.-t. Dense passage retrieval for\n"
        "open-domain question answering. In Proceedings of EMNLP, 2020.\n"
        "Lewis, P., Perez, E., Piktus, A., Petroni, F., Karpukhin, V.,\n"
        "Goyal, N., Küttler, H., Lewis, M., Yih, W.-t., Rocktäschel,\n"
        "T., Riedel, S., and Kiela, D. Retrieval-augmented generation for knowledge-intensive NLP tasks. In Advances in\n"
        "Neural Information Processing Systems, volume 33, pp. 9459-9474, 2020.\n"
        "Wu, R., Qiu, Z., Jiang, J., Qi, G., and Wu, X. Conditional generation net for medication recommendation. In\n"
        "Proceedings of the ACM Web Conference 2022, pp. 935-945. ACM, 2022.\n"
    )
    body = "FAISS (Johnson et al., 2021), DPR (Karpukhin et al., 2020), RAG (Lewis et al., 2020), COGNet (Wu et al., 2022)."
    bib = _bib(body, tail)
    assert [r.label for r in bib.refs] == ["Johnson (2021)", "Karpukhin (2020)", "Lewis (2020)", "Wu (2022)"]
    assert _uncited(bib) == []


def test_a_capitalized_section_after_the_references_ends_the_list():
    tail = "Zhao, X. and Long, B. AutoDim. In Proceedings of the Web Conference 2021, 2021.\nAI USAGEDISCLOSURE\nAI tools contributed (Liao et al., 2026).\n"
    assert _AFTER_REFERENCES.search(tail).group(0).strip() == "AI USAGEDISCLOSURE"
    assert _AFTER_REFERENCES.search("Smith, J. 2020. A paper.\nAI Usage Disclosure\nWe used...\n")
    # A reference entry in capitals that runs on is not a heading.
    assert not _AFTER_REFERENCES.search("Smith, J. 2020. A paper. IEEE TRANSACTIONS ON X, 3.\n")


# --- 2. DOIs ---------------------------------------------------------------------------------


def test_doi_split_by_a_page_number_and_running_head_is_rejoined():
    text = "2021. doi: 10.1109/\n4\nTabular Population Priors for EHR Medication Set Prediction\nTBDATA.2019.2921572. URL https://doi.org/\n10.1109"
    assert R._doi_continuation(text, text.index("/") + 1).group(1) == "TBDATA.2019.2921572."
    # A suffix wrapped mid-way keeps its digits ("sdata.2016." / "18").
    text = "doi:10.1038/sdata.2016.\n18\n"
    assert R._doi_continuation(text, text.index("2016.") + 5).group(1) == "18"


def test_a_registered_title_with_its_rest_in_the_subtitle_matches():
    record = {"title": ["LEAP"], "subtitle": ["Learning to Prescribe Effective and Safe Treatment Combinations for Multimorbidity"]}
    title = R._title_of(record)
    assert title.startswith("LEAP: Learning")
    assert R.title_similarity("{LEAP}: Learning to Prescribe Effective and Safe Treatment Combinations for Multimorbidity", title) == 1.0
    assert R.title_similarity("LEAP", "LEAP: Learning to Prescribe Effective") == 1.0
    assert R.title_similarity("LEAP", "LEAN") < 1.0


# --- 3-5. hidden text, captions, e-mail --------------------------------------------------------


def test_running_head_and_unspaced_label_open_captions():
    head = "MXFP6 Inference for Recommendation Models"
    text = "\n".join(
        [
            "Intro. As shown in Table 1, it works.",
            head,
            "Table 1. Dense peak tensor throughput of each precision, by GPU.",
            "Text continues here with more words in a long line of running prose that fills it.",
            head,
            "More text with many words in a long running line of prose that fills the line up.",
            head,
            "results where each example corresponds to an inpatient",
            "Table 2.Main results on 1,004 held-out MIMIC-IV admissions.",
        ]
    )
    captions = [(m.series, m.number) for m in C.mentions(text) if m.caption]
    assert captions == [("table", 1), ("table", 2)]


def test_grouped_email_addresses():
    found = emails_in("Andrea Soltoggio\n{I.M.D-Souza, T.W.Jackson, V.M.Q.Castro}@lboro.ac.uk\nLoughborough University")
    assert found == ["{I.M.D-Souza, T.W.Jackson, V.M.Q.Castro}@lboro.ac.uk"]
    assert emails_in("see (a, b) @ noon") == []


def test_identical_small_text_warnings_on_one_page_are_merged():
    rest = "97 text item(s) print smaller than 4 pt (e.g. '0.0' 1.4 pt)"
    issues = [Issue(WARNING, "manuscript_file", f"x.pdf page 22, drawing {n}: {rest}") for n in (1, 2, 3)]
    issues.append(Issue(WARNING, "manuscript_file", "x.pdf page 7, drawing 2: 86 text item(s) print smaller"))
    merged = _merge_drawings(issues)
    assert [i.message for i in merged] == [f"x.pdf page 22, drawings 1, 2, 3: {rest}", "x.pdf page 7, drawing 2: 86 text item(s) print smaller"]


# --- 6. prose ----------------------------------------------------------------------------------


def test_spelling_skips_math_split_words_and_column_broken_suffixes():
    table = P.misspellings()
    if table is None:
        return  # codespell not installed
    assert P.spelling("whereAa,ℓ∈R r×din,Ba,ℓ∈R dout×r, and", table) == []
    assert P.spelling("the sampling cost on a singl e consumer GPU", table) == []
    assert P.spelling("store the output tile\nment is then one fully coalesced load", table) == []
    assert P.spelling("so it is\nteh best", table) == ["'teh' (did you mean 'the'?)"]
    assert P.spelling("we recieve it", table)


def test_axis_labels_and_legend_keys_are_not_doubled_words():
    assert P.repeated_words("task number anli anli anli arc arc bool cb common copa cosmos_qa dart definite drop e2e glue glue hellaswag", pdf=True) == []
    assert P.repeated_words("accuracy Boundary lb lb = 1 lb = 8 lb = 16", pdf=True) == []
    assert P.repeated_words("We ran the the experiment on all of the data and it worked.", pdf=True)


# --- 7. acronyms -------------------------------------------------------------------------------


def test_lost_hyphen_does_not_give_an_acronym_a_second_meaning():
    assert _meaning_key("quantization signal noise ratio") == _meaning_key("quantization signalto noise ratio")
    assert _meaning_key("Regions of Interest") == _meaning_key("region of interest")
    assert _meaning_key("false discovery rate") != _meaning_key("false positive rate")


# --- 8. LaTeX source ---------------------------------------------------------------------------


def test_tex_title_block_footnotes_and_tikz_styles_do_not_break_brackets():
    src = r"""\begin{document}
\twocolumn[
\icmltitle{Tabular Population Priors}
]
\tikzset{softflow/.style={line width=0.9pt , ->}, panel/.style={fill=PanelGray , rounded corners}}
That is a thing. (Normal training methods like fine-tuning aren't included.\footnote{Unless designed for it.

See below.} We are concerned here.)
\end{document}"""
    paragraphs = P._tex_paragraphs(src)
    brackets = P._CHECKS[P.BRACKETS][2]
    assert all(brackets(p) == [] and P.punctuation_spacing(p) == [] for p in paragraphs)
    assert any("Unless designed for it." in p for p in paragraphs)


def test_todo_package_switched_off_is_not_a_placeholder():
    assert not _TEX_TODO_PACKAGES.search(r"\usepackage[disable,textsize=tiny]{todonotes}")
    assert not _TEX_TODO_PACKAGES.search(r"\usepackage[final]{fixme}")
    assert _TEX_TODO_PACKAGES.search(r"\usepackage[textsize=tiny]{todonotes}")


def test_href_escapes_are_undone_before_a_link_is_probed():
    urls = list(_find_urls(r"\href{https://en.wikipedia.org/wiki/Sinkhorn\%27s_theorem\#Sinkhorn\%E2\%80\%93Knopp_algorithm}{here}"))
    assert urls == ["https://en.wikipedia.org/wiki/Sinkhorn%27s_theorem#Sinkhorn%E2%80%93Knopp_algorithm"]


def test_times_sign_after_a_number_is_ignored_when_comparing_text():
    assert document.tokens("a 55x speedup, 1.9× faster") == document.tokens("a 55 speedup, 1.9 faster")
    assert document.tokens("x-ray 0x1F 3x3") == ["x", "ray", "0x1f", "3x3"]


def test_title_case_first_appendix_ends_the_reference_list():
    # ICML: the appendix after the references is headed "A. Population Prior Implementation Details";
    # left in the reference list, its "Table 3 reports ..." was not counted as citing Table 3.
    assert _AFTER_REFERENCES.search("Zhao, X. MedRAG. 2025.\nA. Population Prior Implementation Details\nWe build...\n")
    assert not _AFTER_REFERENCES.search("Zhao, X. 2025.\nA Survey of Large Language Models\n")
    assert not _AFTER_REFERENCES.search("Zhao, X. 2025.\nA. Smith and B. Jones\n")


# --- held-out papers (2611.17515-17539) ---------------------------------------------------------


def test_undated_entry_and_ogonek_keep_entries_apart():
    tail = (
        "Guan, Melody Y., Miles Wang, and Micah Carroll. 2025.\n"
        "“Monitoring Monitorability.” OpenAI, December 18.\n"
        "In re London Silver Fixing, Ltd., Antitrust Litigation. n.d. “Third Amended\n"
        "Complaint.”\n"
        "Kahneman, Daniel, and Amos Tversky. 1979. “Prospect Theory.” Econometrica 47 (2): 263–91.\n"
        "Thaler, Richard H., and Eric J. Johnson. 1990. “Gambling with the House Money.”\n"
        "Management Science 36 (6): 643–60.\n"
        "Tr˛ebacz, Maja, Sam Arnesen, and Joe Gershenson. 2026.\n"
        "“Auto-review of Agent Actions.” OpenAI Alignment Research Blog.\n"
    )
    body = "Guan et al. (2025), Kahneman and Tversky (1979), Thaler and Johnson (1990), Trębacz et al. (2026)."
    bib = _bib(body, tail)
    labels = [r.label for r in bib.refs]
    assert {"Guan (2025)", "Kahneman (1979)", "Thaler (1990)", "Trębacz (2026)"} <= set(labels)
    assert _uncited(bib) == []


def test_yes_no_table_row_is_not_a_doubled_word():
    assert P.repeated_words("(7.5) Position controls no no yes no yes *p<0.05", pdf=True) == []
    assert P.repeated_words("there is no no reason to", pdf=True)


def test_kerned_table_label_and_caption_after_figure_lettering():
    text = "∆ from RS +0.155 +0.079 +0.376\nT able 1: Results of applying the detector.\nText. As in Table 1, it works."
    assert [(m.series, m.number, m.caption) for m in C.mentions(text)] == [("table", 1, True), ("table", 1, False)]
    text = (
        "V. RESULTS\nTable I reports the accuracy of every configuration,\nFig. 2 plots it against the amount of labelled data.\n"
        "Model annealing Cross-entropy Label smoothing SGDR Snapshot KD\nFig. 2. Test top-1 accuracy against the amount of labelled data.\n"
    )
    assert [(m.number, m.caption) for m in C.mentions(text) if m.series == "figure"] == [(2, False), (2, True)]
    # A sentence wrapped after "in" that ends at the citation is still a citation.
    text = "unlike the tuned speedups in\nFig. 3.\nThe next part.\nFig. 3. Speedups per layer.\n"
    assert [m.caption for m in C.mentions(text)] == [False, True]


def test_spelling_skips_a_word_glued_to_a_math_symbol_or_an_italic_phrase():
    table = P.misspellings()
    if table is None:
        return
    assert P.spelling("instead of the one-hot\nvectore y. Its entropy", table) == []
    assert P.spelling("both at the network levelin vivoand computationally", table) == []
    assert P.spelling("the maximum displacement is\nbelows tol.", table)


def test_parenthesized_panel_letters_are_panel_citations():
    found = [(c.number, c.letter) for c in C.panel_mentions("Verifier-gated continuation.Figure 3(b) starts. Figure 3(a-c) show it. Fig. 2a-c and 3b, d.")]
    assert found == [(3, "b"), (3, "a"), (3, "b"), (3, "c"), (2, "a"), (2, "b"), (2, "c"), (3, "b"), (3, "d")]


def test_alphabetical_order_reads_given_name_first_entries_by_surname():
    assert B._sort_surname("Noushin Ahmadpour, Meher Kantroo, and Jillian L Stobart. Extracellular calcium") == "Ahmadpour"
    assert B._sort_surname("Nicola J Allen and Cagla Eroglu. Cell biology") == "Allen"
    assert B._sort_surname("Dettmers, T., and Zettlemoyer, L. 2023. The Case") == "Dettmers"
    assert B._sort_surname("J. Smith and K. Lee. Title") == "Smith"
