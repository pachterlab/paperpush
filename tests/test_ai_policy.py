"""The generative-AI policy check (``guidelines.ai_policy``) and its sentence classifier."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperpush import ai_policy
from paperpush import requirements as r
from paperpush.database import Field, Venue
from paperpush.requirements_check import STATEMENT_IN_TEXT
from paperpush.validate import ERROR, WARNING, NotApplicable

_ALIASES = {
    "sections": {"Introduction": [], "Results": [], "Methods": ["Materials and Methods", "Online Methods"], "Acknowledgements": ["Acknowledgments"]},
    "statements": {"ai_use": ["Declaration of generative AI", "Use of AI"], "acknowledgements": ["Acknowledgments", "Acknowledgement"], "funding": ["Funding"]},
}

MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
COVER = Field(id="cover_letter", label="Cover letter", type="file")
AUTHORS = Field(id="authors", label="Authors", type="authorlist", fields=["name", "email?", "corresponding"])
AI_FIELD = Field(id="ai_disclosure", label="Generative AI use disclosure", type="textarea")


@pytest.fixture(autouse=True)
def _aliases(monkeypatch):
    monkeypatch.setattr(r, "_load_raw", lambda: {"$aliases": _ALIASES})


def _venue(*fields: Field) -> Venue:
    return Venue(slug="testj", name="Test", fields=[MANUSCRIPT, *fields])


def _reqs(**statements) -> r.ManuscriptRequirements:
    return r.ManuscriptRequirements(slug="testj", statements=r.StatementRules(**statements))


def _write(tmp_path: Path, lines: list[str], name: str = "paper.md") -> str:
    path = tmp_path / name
    path.write_text("\n\n".join(lines), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    "sentence, expected",
    [
        ("ChatGPT (OpenAI) was used to improve the readability and language of the manuscript.", (True, False, True)),
        ("During the preparation of this work the authors used Claude to draft code for the figures.", (True, False, False)),
        ("No generative AI tools were used in the preparation of this manuscript.", (True, True, False)),
        ("The authors did not use AI-assisted technologies in writing this paper.", (True, True, False)),
        ("GPT-4 achieved 85% accuracy on the benchmark.", (False, False, False)),
        ("We used LLMs to generate synthetic training data.", (False, False, False)),
        ("Claude Bernard proposed the concept of the milieu interieur.", (False, False, False)),
        ("The text was edited for clarity.", None),
    ],
)
def test_classify(sentence, expected):
    assert ai_policy.classify(sentence) == expected


def test_in_text_statement_pattern():
    assert STATEMENT_IN_TEXT["ai_use"].search("Methods text. We used ChatGPT to edit the manuscript for grammar.")
    assert not STATEMENT_IN_TEXT["ai_use"].search("We benchmarked GPT-4 and Claude on the task.")


def test_not_applicable_without_a_policy(tmp_path):
    with pytest.raises(NotApplicable):
        ai_policy.check(_venue(), _reqs(), {"manuscript_file": _write(tmp_path, ["Introduction", "Text."])})


def test_disclosure_in_the_wrong_section(tmp_path):
    path = _write(tmp_path, ["Introduction", "Text.", "Methods", "We did experiments.", "Acknowledgements", "We used ChatGPT to draft parts of the text."])
    out = ai_policy.check(_venue(), _reqs(ai_use_if_used=True, ai_use_locations=["methods"]), {"manuscript_file": path})
    assert [i.level for i in out.issues] == [WARNING]
    assert "Acknowledgements" in out.issues[0].message and "Methods" in out.issues[0].message


def test_disclosure_in_an_allowed_section_passes(tmp_path):
    path = _write(tmp_path, ["Introduction", "Text.", "Materials and Methods", "Cell culture", "We used Claude to write analysis code."])
    out = ai_policy.check(_venue(), _reqs(ai_use_if_used=True, ai_use_locations=["methods"]), {"manuscript_file": path})
    assert not out.issues
    assert "disclosure in Methods" in out.detail


def test_copyediting_exempt(tmp_path):
    path = _write(tmp_path, ["Introduction", "Text.", "Acknowledgements", "Grammarly was used to check spelling."])
    reqs = _reqs(ai_use_if_used=True, ai_use_locations=["statement"], ai_copyediting_exempt=True)
    assert not ai_policy.check(_venue(), reqs, {"manuscript_file": path}).issues


def test_no_mention_passes_with_a_reminder(tmp_path):
    path = _write(tmp_path, ["Introduction", "Text.", "Methods", "We did experiments."])
    out = ai_policy.check(_venue(), _reqs(ai_use_if_used=True, ai_use_locations=["methods"]), {"manuscript_file": path})
    assert not out.issues and "mentions no use of generative AI" in out.detail


def test_sub_says_used_but_manuscript_is_silent(tmp_path):
    path = _write(tmp_path, ["Introduction", "Text."])
    values = {"manuscript_file": path, "ai_disclosure": "ChatGPT was used to edit the abstract."}
    out = ai_policy.check(_venue(AI_FIELD), _reqs(ai_use_if_used=True), {**values})
    assert len(out.issues) == 1 and out.issues[0].field == "ai_disclosure"
    values["ai_disclosure"] = "None"
    assert not ai_policy.check(_venue(AI_FIELD), _reqs(ai_use_if_used=True), values).issues


def test_ai_author_is_an_error(tmp_path):
    path = _write(tmp_path, ["Introduction", "Text."])
    values = {"manuscript_file": path, "authors": "Jane Doe | jane@uni.edu | yes\nChatGPT | | no"}
    out = ai_policy.check(_venue(AUTHORS), _reqs(ai_authorship_prohibited=True), values)
    assert [i.level for i in out.issues] == [ERROR] and "ChatGPT" in out.issues[0].message


def test_cover_letter_must_disclose_too(tmp_path):
    path = _write(tmp_path, ["Introduction", "Text.", "Methods", "We used ChatGPT to translate the questionnaire text."])
    letter = _write(tmp_path, ["Dear Editor, please consider our manuscript."], "letter.md")
    reqs = _reqs(ai_use_if_used=True, ai_use_locations=["methods"], ai_use_cover_letter=True)
    out = ai_policy.check(_venue(COVER), reqs, {"manuscript_file": path, "cover_letter": letter})
    assert len(out.issues) == 1 and out.issues[0].field == "cover_letter"


def test_latex_statement_section(tmp_path):
    tex = tmp_path / "ms.tex"
    tex.write_text(
        "\\documentclass{article}\\begin{document}\\section{Introduction} Text.\n"
        "\\section*{Declaration of generative AI} The authors used ChatGPT to polish the language.\n"
        "\\end{document}",
        encoding="utf-8",
    )
    out = ai_policy.check(_venue(), _reqs(ai_use_if_used=True, ai_use_locations=["statement"]), {"manuscript_file": str(tex)})
    assert not out.issues, out.issues

