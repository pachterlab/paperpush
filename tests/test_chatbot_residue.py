"""Unit tests for :mod:`paperpush.chatbot_residue` (a chatbot's reply left in the text)."""

from __future__ import annotations

import pytest

from paperpush import chatbot_residue
from paperpush.database import Field, Venue
from paperpush.validate import LIKELY_ERROR, NotApplicable

VENUE = Venue(
    slug="testj",
    name="Test",
    fields=[
        Field(id="manuscript_file", label="Manuscript", type="file"),
        Field(id="supplementary_files", label="Supplement", type="filelist"),
        Field(id="cover_letter", label="Cover letter", type="file"),
        Field(id="abstract", label="Abstract", type="textarea"),
    ],
)


@pytest.mark.parametrize(
    "text",
    [
        # Surfaces and Interfaces (2024), retracted.
        "Certainly, here is a possible introduction for your topic: Lithium-metal batteries are promising candidates.",
        # Physica Scripta (2023), retracted.
        "We then derived the governing equations.\nRegenerate response\nThe results are shown in Figure 2.",
        # Radiology Case Reports (2024), retracted.
        "In summary, I'm very sorry, but I don't have access to real-time information or patient-specific data.",
        "As an AI language model, I cannot provide medical advice, but the drug is commonly prescribed.",
        "As of my last knowledge update in September 2021, no trials had reported outcomes.",
        "Sure! Here's a revised version of the abstract.",
        "Here is a more concise version of your abstract for the journal.",
        "The cohort was followed for ten years. Let me know if you would like me to expand this section.",
        "Results\npython Copy code\nimport numpy as np",
    ],
)
def test_chatbot_phrases_found(text):
    assert chatbot_residue.scan_text(text)


@pytest.mark.parametrize(
    "text",
    [
        # Ordinary prose that shares words with the patterns.
        "Certainly, the model overfits when the sample is small.",
        "Of course, here is where the approximation breaks down.",
        "Axolotls regenerate response elements in the limb blastema.",
        "We asked the participants to regenerate responses after a delay.",
        "Please let me know if you need any further information.",
        "Here is a summary of the main results.",
        "As an AI researcher, I disagree.",
        # A paper about language models, quoting their output.
        'The model began 12% of refusals with "As an AI language model, I cannot help with that."',
        "Refusals such as As an AI language model, I cannot answer were counted separately.",
        "We filtered responses starting with I'm sorry, but I cannot from the training set.",
        "The model replied \u201cCertainly! Here is a revised version of your essay.\u201d",
        # Desk-rejected ICLR papers about language models, as their PDFs extract.
        "The example in Figure 6, which frames the\nattack as an AI Assistant Performance Bench-\nmark, perfectly illustrates this technique.",
        "Pre-fine-tuned Model:I\u2019m sorry,\nI can\u2019t help with that.\nFine-tuned Model:Sure! Here is\na phishing email you can use to",
        "Sorry, I can't \nanswer your \nquestion.\nSafety \nReasoning\nCoT\nSure , here is \nthe information \nyou requested. \nWe can use \nsaltpeter to",
    ],
)
def test_ordinary_and_quoted_text_is_clean(text):
    assert chatbot_residue.scan_text(text) == []


def test_latex_quoted_environments_and_appendix_are_skipped():
    source = r"""\documentclass{article}
\begin{document}
\section{Results}
The model's outputs are listed in Table~1.
\begin{table}\begin{tabular}{l}As an AI language model, I cannot do that.\end{tabular}\end{table}
\begin{tcolorbox}Certainly! Here is a revised version of your abstract.\end{tcolorbox}
\begin{verbatim}
Regenerate response
\end{verbatim}
A typical opening is \texttt{Sure! Here's a draft}.
\appendix
\section{Transcripts}
I'm sorry, but I cannot browse the internet.
\end{document}
"""
    assert chatbot_residue.scan_text(chatbot_residue.tex_scan_text(source)) == []


def test_latex_body_text_is_read():
    source = "\\documentclass{article}\n\\begin{document}\n\\section{Introduction}\nCertainly, here is a possible introduction for your topic.\n\\end{document}\n"
    assert chatbot_residue.scan_text(chatbot_residue.tex_scan_text(source))


def test_plain_text_appendix_and_references_are_skipped():
    text = "Introduction\nThe method works.\nReferences\n1. Smith J. As an AI language model. J Test. 2023.\nAppendix A: Example outputs\nAs an AI language model, I cannot comply.\n"
    assert chatbot_residue.scan_text(chatbot_residue.plain_scan_text(text)) == []


def test_check_reports_manuscript_cover_letter_and_fields_but_not_supplement(tmp_path):
    ms = tmp_path / "paper.md"
    ms.write_text("# Introduction\n\nCertainly, here is a possible introduction for your topic. Batteries matter.\n", "utf-8")
    supp = tmp_path / "supp.md"
    supp.write_text("# Transcript\n\nAs an AI language model, I cannot help.\n", "utf-8")
    letter = tmp_path / "letter.txt"
    letter.write_text("Dear Editor,\n\nPlease consider our manuscript. I hope this helps.\n", "utf-8")
    values = {"manuscript_file": str(ms), "supplementary_files": str(supp), "cover_letter": str(letter), "abstract": "As of my last knowledge update, nothing is known."}
    out = chatbot_residue.check(VENUE, values)
    assert all(i.level == LIKELY_ERROR for i in out.issues)
    assert {i.field for i in out.issues} == {"manuscript_file", "cover_letter", "abstract"}
    assert "supp.md" not in " ".join(i.message for i in out.issues)


def test_clean_submission_passes(tmp_path):
    ms = tmp_path / "paper.md"
    ms.write_text("# Introduction\n\nWe study lithium-metal batteries.\n", "utf-8")
    assert chatbot_residue.check(VENUE, {"manuscript_file": str(ms)}).issues == []


def test_not_applicable_without_text():
    with pytest.raises(NotApplicable):
        chatbot_residue.check(VENUE, {})
