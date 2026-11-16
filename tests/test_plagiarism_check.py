"""paperpush validate --plagiarism: the opt-in scan through Originality.ai or Winston AI (no network: the API is faked)."""

from __future__ import annotations

import importlib

import pytest

from paperpush import plagiarism_check as pc
from paperpush.cli import main
from paperpush.database import Field, Venue
from paperpush.subfile import SubFile

validate_mod = importlib.import_module("paperpush.validate")
OFFLINE = dict(check_links=False, check_references=False, check_sensitive=False, check_hidden_text=False, check_openreview=False, check_manuscript=False)
TEXT = "Introduction\n" + "We measured the expression of many genes in single cells across tissues. " * 40 + "\nReferences\n1. Someone et al. A cited paper that would always match. Nature 2020.\n"
PREPRINT = "https://www.biorxiv.org/content/10.1101/2025.01.01.123456v2.full"


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for names in pc.KEY_ENV.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    validate_mod._MODELS.clear()
    yield
    validate_mod._MODELS.clear()


def _winston_answer(score=35, sources=None):
    sources = (
        sources
        if sources is not None
        else [
            {"url": PREPRINT, "title": "Our preprint", "score": 33, "plagiarismWords": 160, "plagiarismFound": [{"startIndex": 0, "endIndex": 50, "sequence": "We measured the expression of many genes"}], "is_excluded": False},
            {"url": "https://example.org/other-paper", "title": "Other paper", "score": 2, "plagiarismWords": 10, "plagiarismFound": [{"sequence": "single cells across tissues"}], "is_excluded": False},
        ]
    )
    return {"status": 200, "result": {"score": score, "textWordCounts": 480}, "sources": sources, "attackDetected": {"zero_width_space": False, "homoglyph_attack": False}, "credits_used": 960}


def _originality_answer():
    return {
        "results": {
            "plagiarism": {
                "score": 30,
                "results": [
                    {"phrase": "We measured the expression of many genes in single cells across tissues.", "results": [{"link": PREPRINT, "title": "Our preprint"}]},
                    {"phrase": "across tissues", "results": [{"link": "https://example.org/other-paper", "title": "Other"}]},
                ],
            },
            "credits": {"used": 5},
        }
    }


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple[str, dict, dict]] = []
    answers: dict[str, dict] = {}

    def fake_post(url, body, headers):
        seen.append((url, body, headers))
        return answers["winston" if "gowinston" in url else "originality"]

    monkeypatch.setattr(pc, "_post", fake_post)
    seen_answers = answers
    seen_answers.update(winston=_winston_answer(), originality=_originality_answer())
    return seen, answers


def test_options_need_a_key():
    with pytest.raises(ValueError, match="WINSTON_API_KEY or ORIGINALITY_API_KEY"):
        pc.options()
    with pytest.raises(ValueError, match="WINSTON_API_KEY"):
        pc.options("winston")


def test_options_pick_the_provider_whose_key_is_set(monkeypatch):
    monkeypatch.setenv("WINSTON_API_KEY", "w-key")
    opts = pc.options(accepted_urls=["biorxiv.org, arxiv.org", "10.1101/x"])
    assert (opts.provider, opts.api_key, opts.threshold) == ("winston", "w-key", pc.DEFAULT_THRESHOLD)
    assert opts.accepted_urls == ("biorxiv.org", "arxiv.org", "10.1101/x")
    assert pc.options("originality", "explicit").provider == "originality"
    assert pc.options(api_key="explicit").provider == "winston"  # the default


def test_winston_is_preferred_when_both_keys_are_set(monkeypatch):
    monkeypatch.setenv("ORIGINALITY_API_KEY", "o-key")
    monkeypatch.setenv("WINSTON_API_KEY", "w-key")
    assert (pc.options().provider, pc.options().api_key) == ("winston", "w-key")


@pytest.mark.parametrize(
    "url, accepted, expected",
    [
        (PREPRINT, ["biorxiv.org"], True),
        (PREPRINT, ["https://biorxiv.org/content/10.1101/2025.01.01.123456"], True),
        (PREPRINT, ["biorxiv.org/content/10.1101/2025.01.01.123456v1"], True),
        (PREPRINT, ["10.1101/2025.01.01.123456"], True),
        (PREPRINT, ["https://doi.org/10.1101/2025.01.01.123456"], True),
        (PREPRINT, ["10.1101/2025.01.01.12345"], False),
        (PREPRINT, ["biorxiv.org/content/10.1101/2025.01.01.999999"], False),
        ("https://example.org/a", ["biorxiv.org"], False),
        ("https://sub.example.org/abc", ["example.org/a"], False),
    ],
)
def test_is_accepted(url, accepted, expected):
    assert pc.is_accepted(url, tuple(accepted)) is expected


def test_winston_warns_over_threshold_and_lists_every_source(calls):
    seen, _ = calls
    issues, detail = pc.check_text(TEXT, pc.options("winston", "k"))
    url, body, headers = seen[0]
    assert url == pc.URLS["winston"] and headers["Authorization"] == "Bearer k" and "excluded_sources" not in body
    # The score over the threshold and each matching source under it are similarity findings (pink).
    assert [i.level for i in issues] == ["similarity", "similarity", "similarity"]
    assert "35% of the text" in issues[0].message and PREPRINT in issues[1].message
    assert PREPRINT in detail and "example.org/other-paper" in detail and "960 credits" in detail


def test_accepted_preprint_is_listed_but_does_not_warn(calls):
    seen, answers = calls
    # Winston takes the accepted URLs out of its score itself.
    answers["winston"] = _winston_answer(score=2, sources=[{**_winston_answer()["sources"][0], "is_excluded": True}, _winston_answer()["sources"][1]])
    issues, detail = pc.check_text(TEXT, pc.options("winston", "k", ["biorxiv.org"]))
    assert seen[0][1]["excluded_sources"] == ["biorxiv.org"]
    assert issues == []
    assert f"{PREPRINT} 33% (accepted)" in detail


def test_originality_score_drops_accepted_sources(calls):
    seen, _ = calls
    opts = pc.options("originality", "k", [PREPRINT])
    issues, detail = pc.check_text(TEXT, opts)
    url, body, headers = seen[0]
    assert url == pc.URLS["originality"] and headers["X-OAI-API-KEY"] == "k"
    assert body["check_plagiarism"] is True and body["storeScan"] is False and body["check_ai"] is False
    assert issues == []
    assert "30% of" in detail and "without the accepted sources" in detail and "(accepted)" in detail


def test_originality_warns_without_accepted_urls(calls):
    issues, _ = pc.check_text(TEXT, pc.options("originality", "k"))
    assert issues and "30% of the text" in issues[0].message


def test_hidden_characters_are_a_warning(calls):
    _, answers = calls
    answers["winston"] = {**_winston_answer(score=0, sources=[]), "attackDetected": {"homoglyph_attack": True}}
    issues, detail = pc.check_text(TEXT, pc.options("winston", "k"))
    assert len(issues) == 1 and "homoglyph" in issues[0].message and "no matching sources" in detail


def test_long_text_is_scanned_in_chunks(calls, monkeypatch):
    seen, _ = calls
    monkeypatch.setitem(pc.CHUNK_CHARS, "winston", 1000)
    pc.check_text(TEXT * 3, pc.options("winston", "k"))
    assert len(seen) > 1 and all(len(body["text"]) <= 1000 for _, body, _ in seen)


def test_chunks_split_between_paragraphs():
    text = "a" * 60 + "\n" + "b" * 60
    assert pc.chunks(text, 100) == ["a" * 60, "b" * 60]


def _venue():
    return Venue(slug="test_plag", name="Test", fields=[Field(id="title", label="Title", type="text"), Field(id="manuscript_file", label="Manuscript", type="file")])


def _check(report):
    return next(r for r in report.results if r.id == "integrity.plagiarism")


def test_validate_leaves_it_off_by_default():
    report = validate_mod.validate_report(SubFile(venue="test_plag", values={"title": "T"}), _venue(), **OFFLINE)
    result = _check(report)
    assert result.status == "unchecked" and "--plagiarism" in result.detail


def test_validate_scans_the_manuscript_without_its_references(calls, tmp_path):
    seen, _ = calls
    manuscript = tmp_path / "manuscript.txt"
    manuscript.write_text(TEXT, encoding="utf-8")
    sub = SubFile(venue="test_plag", values={"title": "T", "manuscript_file": str(manuscript)})
    report = validate_mod.validate_report(sub, _venue(), plagiarism=pc.options("winston", "k"), **OFFLINE)
    result = _check(report)
    assert result.status == "similarity" and PREPRINT in result.detail
    assert "would always match" not in seen[0][1]["text"]


def test_validate_reports_a_service_failure_as_not_checked(monkeypatch, tmp_path):
    def refuse(url, body, headers):
        raise pc.PlagiarismError("the account is out of credits")

    monkeypatch.setattr(pc, "_post", refuse)
    manuscript = tmp_path / "manuscript.txt"
    manuscript.write_text(TEXT, encoding="utf-8")
    sub = SubFile(venue="test_plag", values={"title": "T", "manuscript_file": str(manuscript)})
    result = _check(validate_mod.validate_report(sub, _venue(), plagiarism=pc.options("originality", "k"), **OFFLINE))
    assert result.status == "unchecked" and "out of credits" in result.detail


def test_cli_without_a_key_stops(capsys, tmp_path):
    assert main(["validate", str(tmp_path / "x.sub.txt"), "--plagiarism"]) == 2
    assert "needs an API key" in capsys.readouterr().err
