"""Loosely matched references go to the AI check instead of counting as confirmed.

ICLR 2026 desk rejections for fabricated references: in most papers where the AI check missed a
reference the program chairs named, the reference had been matched to a registered work with no
problem reported, although the work plainly differed. The entry cited a real arXiv id under other
authors ("Steffen Schneider et al." for Kvinge et al.'s paper) or another title, or gave a longer
title that merely starts with the registered one ("How do vision transformers work? Insights from
spatial masking" for "How Do Vision Transformers Work?").
"""
from __future__ import annotations

from paperpush import reference_search as rs

_FRAMES = {"title": "Internal Representations of Vision Models Through the Lens of Frames on Data Manifolds",
           "author": [{"family": "Kvinge", "given": "Henry"}, {"family": "Jorgenson", "given": "Grayson"}, {"family": "Brown", "given": "Davis"}],
           "issued": {"date-parts": [[2022]]}}
_VIT = {"DOI": "10.48550/arxiv.2202.06709", "type": "posted-content", "title": ["How Do Vision Transformers Work?"],
        "author": [{"family": "Park", "given": "Namuk"}, {"family": "Kim", "given": "Songkuk"}], "issued": {"date-parts": [[2022]]}}
_RESNET = {"DOI": "10.1109/cvpr.2016.90", "type": "proceedings-article", "title": ["Deep Residual Learning for Image Recognition"],
           "author": [{"family": "He", "given": "Kaiming"}, {"family": "Zhang", "given": "Xiangyu"}, {"family": "Ren", "given": "Shaoqing"}, {"family": "Sun", "given": "Jian"}],
           "issued": {"date-parts": [[2016]]}}


def _ref(text, arxiv_id=""):
    return rs.Reference(label="reference 'x (2022)'", where="p.pdf", text=text, arxiv_id=arxiv_id)


def test_a_real_arxiv_id_under_other_authors_is_loose():
    text = "Steffen Schneider, Alice Lee, and Bob Stone. Internal representations of vision models through the lens of frames on data manifolds. arXiv:2211.10558, 2022."
    lk = rs.match_arxiv(_ref(text, "2211.10558"), _FRAMES)
    assert lk.status == rs.MATCHED and not lk.problems
    assert lk.loose == rs.LOOSE_AUTHORS


def test_a_real_arxiv_id_under_another_title_is_loose():
    text = "Henry Kvinge, Grayson Jorgenson, and Davis Brown. Frames on data manifolds for robust vision models. arXiv:2211.10558, 2022."
    lk = rs.match_arxiv(_ref(text, "2211.10558"), _FRAMES)
    assert not lk.problems and rs.LOOSE_TITLE in lk.loose


def test_a_correct_arxiv_entry_is_not_loose():
    text = "Henry Kvinge, Grayson Jorgenson, and Davis Brown. Internal representations of vision models through the lens of frames on data manifolds. arXiv:2211.10558, 2022."
    assert rs.match_arxiv(_ref(text, "2211.10558"), _FRAMES).loose == ""


def test_a_longer_title_starting_with_the_registered_one_is_loose():
    text = "Namuk Park and Songkuk Kim. How do vision transformers work? insights from spatial masking. In ICLR, 2022."
    lk = rs.match_text(_ref(text), [_VIT])
    assert lk.status == rs.MATCHED and not lk.problems and lk.loose == rs.LOOSE_TITLE


def test_a_title_run_into_its_venue_is_not_loose():
    text = "Namuk Park and Songkuk Kim. How do vision transformers work?In International Conference on Learning Representations, 2022."
    lk = rs.match_text(_ref(text), [_VIT])
    assert lk.status == rs.MATCHED and lk.loose == ""


def test_a_correct_prose_entry_is_not_loose():
    text = "Kaiming He, Xiangyu Zhang, Shaoqing Ren, and Jian Sun. Deep residual learning for image recognition. In CVPR, 2016."
    lk = rs.match_text(_ref(text), [_RESNET])
    assert lk.status == rs.MATCHED and lk.loose == ""


def test_loose_matches_go_to_the_ai_task_and_get_no_doi_suggestion():
    text = "Namuk Park and Songkuk Kim. How do vision transformers work? insights from spatial masking. In ICLR, 2022."
    lk = rs.match_text(_ref(text), [_VIT])
    result = rs.SearchResult(lookups=[lk])
    assert result.loose == [lk] and result.suggestions == [] and result.unverified == []
    task = rs.ai_task_instructions([], [], result.loose)
    assert "reference 'x (2022)'" in task and "Matched only loosely" in task and "How Do Vision Transformers Work?" in task


def test_a_registry_title_cut_at_the_subtitle_is_not_loose():
    usad = {"DOI": "10.1145/3394486.3403392", "type": "proceedings-article", "title": ["USAD"],
            "author": [{"family": "Audibert", "given": "Julien"}, {"family": "Michiardi", "given": "Pietro"}], "issued": {"date-parts": [[2020]]}}
    text = "Julien Audibert, Pietro Michiardi, Frederic Guyard, et al. Usad: Unsupervised anomaly detection on multivariate time series. In KDD, 2020."
    lk = rs.match_text(_ref(text), [usad])
    assert lk.status == rs.MATCHED and lk.loose == ""


def test_a_work_cited_under_its_team_is_not_loose_for_its_authors():
    llama = {"title": "The Llama 3 Herd of Models", "author": [{"family": "Grattafiori", "given": "Aaron"}, {"family": "Dubey", "given": "Abhimanyu"}], "issued": {"date-parts": [[2024]]}}
    lk = rs.match_arxiv(_ref("Llama Team. The llama 3 herd of models. arXiv:2407.21783, 2024.", "2407.21783"), llama)
    assert rs.LOOSE_AUTHORS not in lk.loose
