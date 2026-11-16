"""citations.supplementary_attachments false alarms from the ICLR 2026 camera-ready audit.

Each case mirrors an audited paper in short synthetic text: a "supplementary"
phrase that means the PDF's own appendix, a heading, or ordinary prose is not a
missing upload; a zip of code, numbered supplementary items, or Supplementary
Data still are.
"""

import pytest

from paperpush import report
from tests.test_supplementary_attachments import _run

REFS = "\nReferences\nA. Author. A paper title. In Proceedings, 2020.\nB. Author. Another paper. Journal, 2021.\n"
APPENDIX = "\nA RELATED WORK\nPrior work is reviewed here.\nB NOTATIONS\nWe define the notation.\n"


def _check(tmp_path, text):
    path = tmp_path / "manuscript.txt"
    path.write_text(text)
    return _run(path)[1]


@pytest.mark.parametrize("text", [
    # UQZ0Y0dbP9: the pointer names the appendix in the same sentence.
    "Appendix\nThe appendix is organized as follows. We provide supplementary information on the methodology in Appendix B, and more results in Appendix D.\n",
    # OswqOlTYR2: "supplementary results" used adjectivally inside the appendix.
    "C.3 Additional Results\nWe further provide supplementary results on consistency generation. Figure 12 presents examples.\n",
    # d3ABURcHk3: ordinary prose, even with "videos" in the sentence.
    "Videos provide supplementary information about characters, scenes, and plot progression, while audio conveys tone.\n",
    # OWkkFaq1IZ: the neighboring sentence points at the appendix.
    "Training curves are shown in the Appendix K and L. In addition, results and visualizations are included in the supplementary materials. We will release our code.\n",
    # Pa6ak2B9jJ: the paper's own appendix heading.
    "We clarify our setting in Section 3.\n\nTECHNICAL APPENDICES AND SUPPLEMENTARY MATERIAL\nA Target Model Used\nWe primarily consider open-source models.\n",
    # avgMb57IP5: a generic mention in a PDF with its own lettered appendix after the references.
    "Introduction\nThe detailed hyperparameter setting and the full results are reported in the supplementary materials.\n" + REFS + APPENDIX,
])
def test_appendix_heading_or_prose_is_not_a_missing_supplement(tmp_path, text):
    assert _check(tmp_path, text).status == report.NOT_APPLICABLE


@pytest.mark.parametrize("text", [
    # ta8BKRa1bl: a zip of code, even though the paper has an appendix and cites Section F.1 just before.
    "Section F.1 discusses the computational resources. As supplementary material, we provide a zip folder that allows reproducing our analysis.\n" + REFS + APPENDIX,
    # Fn2rSOnpNf: source code in a separate supplement.
    "Reproducibility statement\nWe provide supplementary material containing all source code for our implementation.\n" + REFS + APPENDIX,
    # Journal and bioRxiv cases: numbered items and named files are uploads, appendix or not.
    "Raw counts are in Supplementary Data 1.\n" + REFS + APPENDIX,
    "See Supplementary Table 2 and Fig. S3 for the full results.\n" + REFS + APPENDIX,
    "Example rollouts are shown in the supplementary videos.\n" + REFS + APPENDIX,
    # No appendix in the file: a generic mention still needs an upload.
    "The full results are reported in the supplementary materials.\n",
    "Detailed protocols are given in the Supplementary Information.\n",
])
def test_real_supplement_references_still_fire(tmp_path, text):
    check = _check(tmp_path, text)
    assert check.status == report.LIKELY_ERROR
