"""False alarms found by validating accepted ICLR 2026 papers (manuscript/iclr_study, Figure 3d).

Warnings that fired on nearly every accepted paper:

1. bibliography.uncited: most "never cited" entries were parsing errors. An entry
   merged with the next one (a one-word author like "Anthropic.", a long author list,
   a title glued to the authors' period, a stray "Online." line, a URL wrapped after
   '/'), a year read from an ISSN, a DOI, a URL, or an access date, or a citation the
   body text breaks (split accents "R¨ottger", "Akhound-" / "Sadegh", "Wang et al.,
   2018; 2019", "Tong et al., 2024a;b", numbered citations in parentheses "(1; 13)").
2. prose.acronyms: the running head ("... at ICLR 2026") put ICLR on every page.
3. citations.figure/table: "Tab. 1" was not read as a citation, "Table 1:Deep" (no space
   after the colon) not as a caption, "Appendix Fig. 8" did not count as citing Figure 8,
   and a main-text pointer to an appendix figure was reported as misnumbering.
4. integrity.unmatched_references: "add the DOI" advice for references that were found
   is a suggestion; venues that require DOIs have references.doi_required.
"""

from pathlib import Path

from paperpush import bibliography_check as B
from paperpush import citation_check as C
from paperpush import manuscript as M


class _Doc:
    page_texts = None
    is_docx = False

    def __init__(self, text: str):
        self.text = text


def _bib(body: str, tail: str):
    return B._text_bibliography(_Doc(body + "\nREFERENCES\n" + tail), Path("x.pdf"))


def _uncited(bib) -> list[str]:
    return [r.label for r in bib.refs if r.key not in bib.cited]


def test_one_word_author_long_author_list_and_glued_title_are_entries_of_their_own():
    many = ", ".join(f"Name{chr(65 + i % 26)} Surname{chr(65 + i // 26)}{chr(97 + i % 26)}" for i in range(60))
    tail = (
        "Maksym Andriushchenko and Nicolas Flammarion. Jailbreaking aligned models. arXiv preprint arXiv:2404.02151, 2024.\n"
        "Anthropic. System card: Claude opus 4 & claude sonnet 4. Technical report, Anthropic PBC, May 2025.\n"
        f"Mark Chen, {many}, and Wojciech Zaremba. Evaluating large language models trained on code, 2021.\n"
        "Daan Frenkel and Berend Smit.Understanding Molecular Simulation. Elsevier, 2023.\n"
    )
    body = "As in Andriushchenko & Flammarion (2024), Anthropic (2025), Chen et al. (2021), and Frenkel & Smit (2023)."
    bib = _bib(body, tail)
    assert len(bib.refs) == 4 and _uncited(bib) == []


def test_stray_line_and_url_wrapped_after_a_slash_stay_with_their_entry():
    tail = (
        "Rob Knight and Thomas Wolf. Standard open arms, 2024. URL https://github.com/TheRobotStudio/SO-ARM100.\n"
        "Online.\n"
        "Eric Kolve, Roozbeh Mottaghi, and Winson Han. Ai2-thor: An interactive 3d environment. arXiv preprint arXiv:1712.05474, 2017.\n"
        "Rodrigo de Lazcano and Jordan Terry. Gymnasium robotics, 2024. URL http://github.com/Farama-Foundation/\n"
        "Gymnasium-Robotics.\n"
        "Ben Deverett and Joel Z Leibo. Interval timing in deep reinforcement learning agents. NeurIPS, 32, 2019.\n"
    )
    starts = M.author_year_starts(tail)
    assert [tail[i:i + 4] for i in starts] == ["Rob ", "Eric", "Rodr", "Ben "]


def test_a_line_ending_mid_phrase_is_continued_not_a_new_entry():
    tail = (
        "Harry H. Jiang and Timnit Gebru. Ai art and its impact on artists. In Proceedings of\n"
        "the 2023 AAAI/ACM Conference on AI, Ethics, and Society, pp. 363-374, New York,\n"
        "NY , USA, 2023. Association for Computing Machinery.\n"
        "Ziyuan Zhang and Chao Zhang. Dcdiff: Enhancing jpeg compression. In 2025 62nd ACM/IEEE Design\n"
        "Automation Conference (DAC), pp. 1-7. IEEE, 2025b.\n"
        "Yue Zhao and Philipp Krähenbühl. Image and video tokenization. arXiv preprint arXiv:2406.07548, 2024.\n"
    )
    starts = M.author_year_starts(tail)
    assert [tail[i:i + 4] for i in starts] == ["Harr", "Ziyu", "Yue "]


def test_entry_year_skips_issn_dois_urls_access_dates_and_future_numbers():
    assert B.entry_year("Maria Christoforaki and Oya Beyan. AI Ethics. Appl. Sci., 12(9):4130, 2022. ISSN 2076-3417.").group(0) == "2022"
    assert B.entry_year("Viktor Stein. Synthetic protein switches. Trends, 33(2):101-110, 2015. doi: https://doi.org/10.1016/j. tibtech.2014.11.010.").group(0) == "2015"
    assert B.entry_year("Cohere. Wikipedia embeddings dataset. https://huggingface.co/x, 2023. Accessed: 2025-09-13.").group(0) == "2023"
    assert B.entry_year("Mikko Lauri. A survey. IEEE T-RO, 39(1):21-40, 2023. URL http://example.org/2018/paper.pdf.").group(0) == "2023"


def test_citations_with_split_accents_hyphen_breaks_and_year_chains_are_found():
    tail = (
        "Paul R¨ottger and Dirk Hovy. Xstest: A test suite. In NAACL, 2024.\n"
        "Johannes Ball ´e and Nick Johnston. Variational image compression. arXiv preprint arXiv:1802.01436, 2018.\n"
        "Tara Akhound-Sadegh and Alexander Tong. Iterated denoising energy matching. In ICML, 2024.\n"
        "Yue Wang and Justin Solomon. Dynamic graph cnn. ACM TOG, 38(5):1-12, 2019.\n"
        "Alexander Tong and Yoshua Bengio. Simulation-free schrödinger bridges. In AISTATS, 2024b.\n"
        "Mar López-Pelegr´ın and Joan Arolas. Multiple stable conformations. Journal, 2013.\n"
        "Kathy Y . Wei and David Baker. Computational design. Journal, 2020.\n"
    )
    body = ("XSTest (R¨ottger et al., 2024) and a hyperprior (Ball´e et al., 2018). Unlike (Akhound-\n"
            "Sadegh et al., 2024), others (Wang et al., 2018; 2019; Tong et al., 2024a;b). See López-Pelegrín et al. (2013) and Wei et al. (2020).")
    bib = _bib(body, tail)
    assert len(bib.refs) == 7 and _uncited(bib) == []


def test_numbered_citations_in_parentheses():
    tail = "".join(f"[{i}] Author{i} Name. A title number {i}. Journal, 2020.\n" for i in range(1, 7))
    bib = _bib("Prior work (1; 3; 2) and later (4, 5). Also (6).", tail)
    assert bib.mode == "numbered" and _uncited(bib) == []


def test_tab_abbreviation_colon_captions_and_appendix_prefix():
    text = (
        "Our method wins, as shown in Tab. 1 and Figure 1.\n"
        "A long line of text drawn inside the figure that does not end a sentence <|header|>\n"
        "Figure 1:Left: an overview of everything.\n"
        "Table 1:Deep-prefill robustness of every model.\n"
        "The appendix has more (Appendix Fig. 2).\n"
        "REFERENCES\n"
        "Alice Smith. A title. Journal, 2020.\n"
        "A MORE RESULTS\n"
        "Figure 2: More results.\n"
    )
    found = C.mentions(text)
    for series in ("figure", "table"):
        assert C.order_problems(found, series)[0] == [], series


def test_main_text_pointer_to_an_appendix_figure_is_not_misnumbering():
    text = (
        "We show the method in Figure 1 and failure cases in Figure 3.\n"
        "Figure 1: The method.\n"
        "Results are in Figure 2.\n"
        "Figure 2: Results.\n"
        "REFERENCES\n"
        "Alice Smith. A title. Journal, 2020.\n"
        "A FAILURES\n"
        "Figure 3 shows failures.\n"
        "Figure 3: Failures.\n"
    )
    assert C.order_problems(C.mentions(text), "figure")[0] == []
    # Main-text figures out of order are still reported.
    swapped = text.replace("We show the method in Figure 1", "We show results in Figure 2 and the method in Figure 1")
    assert any("cited before" in p for p in C.order_problems(C.mentions(swapped), "figure")[0])


def test_running_heads_are_not_prose_for_the_acronym_check(tmp_path):
    from pdf_builder import text_line, write_pdf

    from paperpush import style_check as S
    from paperpush.prose_check import PDF_CHECKS

    head = "Published as a conference paper at ICLR 2026"
    pages = []
    for n in range(4):
        content = text_line(108, 740, head)
        content += text_line(108, 700, f"Page {n} prose about large language models (LLMs) and how LLMs are used here.")
        pages.append(content)
    pdf = tmp_path / "paper.pdf"
    write_pdf(pdf, pages)

    class Text:
        path = pdf
        checks = PDF_CHECKS
        paragraphs: list[str] = []

    body = S._body_paragraphs(Text(), prose_only=True)
    assert body and not any("ICLR" in p for p in body)
