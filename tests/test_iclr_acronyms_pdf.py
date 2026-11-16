"""Acronym check on bare PDFs: fixes from a fresh precision sample of ICLR 2026 submissions.

On 30 accepted and rejected papers outside the first audit, 25 were flagged and 4 of 25 sampled
findings were right. The fixes: a PDF legend no longer runs on into the main text (taking its
definitions along), a parenthesis that only names an acronym is not its definition, adjective and
noun forms are one meaning, and "never spelled out" is a suggestion for a PDF.
"""

from __future__ import annotations

from types import SimpleNamespace

from paperpush import style_check as sc
from paperpush.validate import SUGGESTION, WARNING

FULL = "x" * 96  # a full line of the page, so the page's line width is known


def _page(*lines: str) -> str:
    return "\n".join([FULL, FULL, *lines, FULL])


# --- PDF legends end where the main text resumes ---------------------------------------------


def test_one_line_legend_ends_before_a_new_main_text_sentence():
    # "Figure 1: The Mario case ... field." then the introduction's "To capture ... (CPFSMs)".
    page = _page(
        "Figure 1: The Mario case to introduce finite-state machines into the role-playing field.",
        "To capture more nuanced dynamics, we introduce Codified Probabilistic Finite-State Machines",
        "(CPFSMs), which represent states as probability distributions, enabling multinomial transitions.",
    )
    body, legends = sc._pdf_split_captions(page)
    assert "(CPFSMs)" in body
    assert legends == ["Figure 1: The Mario case to introduce finite-state machines into the role-playing field."]


def test_legend_ends_when_the_main_text_resumes_mid_sentence():
    # A full-width last legend line, then the sentence the figure interrupted ("neural networks (CNN) ...").
    page = _page(
        "Figure 1: Challenges in VPS: (a) and (b) the high camouflage of polyps, (c) position variation, (d)",
        "shape variation, (e) size variation. Note that (a)-(e) represent two strictly adjacent frames.",
        "neural networks (CNN) (Wang et al., 2022a) or harnessed the global context modeling of transformers",
    )
    body, legends = sc._pdf_split_captions(page)
    assert "neural networks (CNN)" in body
    assert legends[0].endswith("two strictly adjacent frames.")


def test_legend_does_not_end_at_an_abbreviation_or_a_table_row():
    page = _page(
        "Figure 2: Results of the baseline from Wang et al.",
        "on three benchmarks, with the variance across seeds shown as shaded bands around the mean curve.",
        "Table 1: Translation results on the held-out split, averaged over three seeds and both directions.",
        "Task Model FID Male (CelebA) Female (CelebA) Young Old 12.3 14.5 18.2 19.0 13.3 15.8",
    )
    body, legends = sc._pdf_split_captions(page)
    assert "on three benchmarks" in legends[0]
    assert "Task Model FID" not in body


def test_long_multi_sentence_legend_is_kept_whole():
    page = _page(
        "Figure 3: Accuracy of every method across the five benchmarks used throughout the evaluation section.",
        "Each bar is the mean of three seeds, and the error bars show the standard deviation across seeds.",
        "",
        "We now turn to the ablations.",
    )
    body, legends = sc._pdf_split_captions(page)
    assert "standard deviation" in legends[0]
    assert "We now turn to the ablations." in body


# --- a parenthesis that names an acronym is not its definition --------------------------------


def test_listed_or_cited_acronym_in_parentheses_is_not_a_definition():
    body = [
        "We report background PSNR as a proxy for structural consistency.",
        "We compute pixel-wise metrics (PSNR, MSE) and perceptual metrics.",
        "Accuracy on the 2024 AIME competition improves. We test on AIME25 (AIME, 2025) as well.",
    ]
    assert sc.acronyms(body).before_definition == []


def test_definition_followed_by_its_citation_still_counts():
    body = [
        "We use GAE for the advantages.",
        "Generalized Advantage Estimation (GAE, Schulman et al., 2016) lowers the variance.",
    ]
    assert [h.split("'")[1] for h in sc.acronyms(body).before_definition] == ["GAE"]


def test_adjective_and_noun_forms_are_one_meaning():
    body = [
        "Each client trains a variational transposed convolutional (VTC) network.",
        "Federated learning with variational transposed convolution (VTC) shares the VTC weights.",
    ]
    assert sc.acronyms(body).conflicting == []
    body[1] = "A variable token cache (VTC) stores the VTC states."
    assert len(sc.acronyms(body).conflicting) == 1


# --- severity: "never spelled out" in a PDF is a suggestion -----------------------------------


def _run(monkeypatch, tmp_path, *, pdf: bool):
    from paperpush import document, front_matter_check, prose_check

    path = tmp_path / ("paper.pdf" if pdf else "paper.md")
    path.write_text("x")
    text = SimpleNamespace(path=path, why="", checks=prose_check.PDF_CHECKS if pdf else object(), paragraphs=[])
    fld = SimpleNamespace(id="manuscript_file", label="Manuscript")
    doc = SimpleNamespace(is_tex=False, tex_source=None, page_texts=None)
    monkeypatch.setattr(prose_check, "_manuscripts", lambda v, vals: [(fld, text)])
    monkeypatch.setattr(document, "get", lambda p: doc)
    monkeypatch.setattr(front_matter_check, "manuscript_abstract", lambda d, a: None)
    monkeypatch.setattr(sc, "_body_paragraphs", lambda t, prose_only=False: ["The HMM ran. The HMM won. The HMM stopped."])
    outcome = sc._acronym_check(None, {})
    return next(i for i in outcome.issues if "never spelled out" in i.message)


def test_undefined_acronym_is_a_suggestion_in_a_pdf(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, pdf=True).level == SUGGESTION


def test_undefined_acronym_stays_a_warning_outside_a_pdf(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, pdf=False).level == WARNING
