"""paperpush.front_matter_check: citations in the abstract, structured abstracts, a contact e-mail."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperpush import front_matter_check as fm
from paperpush import report
from paperpush import requirements as r
from paperpush.database import Field, Venue
from paperpush.validate import Checks
from tests.conftest import _build_docx

MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
ABSTRACT = Field(id="abstract", label="Abstract", type="textarea")
VENUE = Venue(slug="testj", name="Test Venue", fields=[MANUSCRIPT, ABSTRACT])


@pytest.fixture
def rules(monkeypatch):
    """Install an in-memory requirements database for ``testj``."""

    def install(entry: dict | None) -> None:
        data = {"$aliases": {}, **({"testj": entry} if entry is not None else {})}
        monkeypatch.setattr(r, "_load_raw", lambda: data)

    install(None)
    return install


def _run(values: dict[str, str], venue: Venue = VENUE) -> dict[str, tuple[str, str, str]]:
    checks = Checks()
    fm.run_checks(checks, venue, values)
    return {res.id: (res.status, " | ".join(i.message for i in res.issues), res.detail) for res in checks.results}


def _docx(tmp_path: Path, paragraphs: list[str]) -> str:
    path = tmp_path / "ms.docx"
    _build_docx(path, paragraphs)
    return str(path)


# --- citations ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Cells grow [1].",
        "Cells grow [2, 5-7].",
        "Cells grow (Smith et al., 2020).",
        "Cells grow (Smith and Jones 2019; Lee 2021).",
        "As Smith et al. (2020) showed, cells grow.",
        "Cells grow (O'Brien, 2018).",
    ],
)
def test_citations_found(text):
    assert fm.citations_in(text)


@pytest.mark.parametrize(
    "text",
    [
        "Values lie in [0, 1].",
        "Presented at (NIPS 2017).",
        "In 2020 we found that cells grow.",
        "We used the WMT 2014 data (Table 2).",
    ],
)
def test_non_citations_ignored(text):
    assert fm.citations_in(text) == []


def test_tex_cite_commands():
    assert fm.citations_in(r"Cells grow \citep[see][]{smith2020}.", tex=True)
    assert fm.citations_in(r"Cells grow \parencite{a,b}.", tex=True)
    assert fm.citations_in("Cells grow.", tex=True) == []


def test_abstract_citations_in_sub_and_manuscript(tmp_path):
    ms = _docx(tmp_path, ["Title", "Abstract", "Cells grow (Smith et al., 2020).", "Introduction", "Body [1]."])
    status, message, _ = _run({"manuscript_file": ms, "abstract": "Cells grow [3]."})["front_matter.abstract_citations"]
    assert status == report.WARNING
    assert "the .sub's abstract cites references ('[3]')" in message
    assert "the abstract in ms.docx cites references" in message
    assert "[1]" not in message  # the Introduction is not the abstract


def test_clean_abstract_passes(tmp_path):
    ms = _docx(tmp_path, ["Title", "Abstract", "Cells grow fast.", "Introduction", "Body [1]."])
    status, _, detail = _run({"manuscript_file": ms, "abstract": "Cells grow fast."})["front_matter.abstract_citations"]
    assert status == report.PASSED
    assert "the abstract in ms.docx" in detail


def test_guideline_rule_named(tmp_path, rules):
    rules({"abstract": {"no_references": True}})
    _, message, _ = _run({"abstract": "Cells grow [3]."})["front_matter.abstract_citations"]
    assert "the author guidelines do not allow them" in message


def test_tex_abstract_read_raw(tmp_path):
    tex = tmp_path / "ms.tex"
    tex.write_text("\\documentclass{article}\\begin{document}\\begin{abstract}Cells grow \\cite{x}.\\end{abstract}\\section{Intro}Body \\cite{y}.\\end{document}")
    status, message, _ = _run({"manuscript_file": str(tex)})["front_matter.abstract_citations"]
    assert status == report.WARNING and "\\cite{x}" in message and "\\cite{y}" not in message


# --- structured abstracts --------------------------------------------------------------------


def test_labelled():
    assert fm.labelled("Background: x. Results: y.", "Background")
    assert fm.labelled("Background: x. Results. y.", "Results")
    assert fm.labelled("x.\nConclusion\ny", "Conclusions")  # singular satisfies plural
    assert fm.labelled("Intervention: x", "Intervention(s) or Exposure(s)")
    assert fm.labelled("**Methods** we did", "Methods")
    assert not fm.labelled("The results show growth.", "Results")


def test_structured_not_applicable_without_rule(rules):
    status, _, _ = _run({"abstract": "Cells grow."})["front_matter.structured_abstract"]
    assert status == report.NOT_APPLICABLE


def test_structured_missing_headings(tmp_path, rules):
    rules({"abstract": {"structured": True, "structured_headings": ["Background", "Results", "Conclusions"]}})
    res = _run({"abstract": "Background: cells. The results show growth. Conclusions: good."})
    status, message, _ = res["front_matter.structured_abstract"]
    assert status == report.WARNING
    assert "missing the structured-abstract heading(s) Results;" in message


def test_structured_passes(rules):
    rules({"abstract": {"structured": True, "structured_headings": ["Background", "Results"]}})
    status, _, _ = _run({"abstract": "Background: cells.\nResults: growth."})["front_matter.structured_abstract"]
    assert status == report.PASSED


def test_structured_without_listed_headings(rules):
    rules({"abstract": {"structured": True}})
    status, message, _ = _run({"abstract": "Cells grow and divide."})["front_matter.structured_abstract"]
    assert status == report.WARNING and "no labelled sections" in message
    status, _, _ = _run({"abstract": "Aim: grow cells. Findings: they grew."})["front_matter.structured_abstract"]
    assert status == report.PASSED


# --- e-mail -------------------------------------------------------------------------------------


def test_email_found_on_title_page(tmp_path):
    ms = _docx(tmp_path, ["Title", "Jane Doe", "Correspondence: jane@uni.edu", "Abstract", "Text."])
    status, _, detail = _run({"manuscript_file": ms})["front_matter.contact_email"]
    assert status == report.PASSED and "jane@uni.edu" in detail


def test_email_missing(tmp_path):
    ms = _docx(tmp_path, ["Title", "Jane Doe", "Abstract", "Text."])
    authors = Field(id="authors", label="Authors", type="authorlist")
    venue = Venue(slug="testj", name="Test Venue", fields=[MANUSCRIPT, authors])
    status, message, _ = _run({"manuscript_file": ms, "authors": "Jane Doe | jane@uni.edu | Uni | | yes"}, venue)["front_matter.contact_email"]
    assert status == report.WARNING
    assert "gives no e-mail address" in message and "the .sub gives jane@uni.edu" in message


def test_email_spelled_out_and_tex(tmp_path):
    assert fm.emails_in("Contact: jdoe at caltech dot edu")
    assert fm.emails_in(r"\author{Jane}\email{jane@uni.edu}")
    assert not fm.emails_in("we looked at data dot plots")


def test_email_not_applicable_double_blind(tmp_path):
    ms = _docx(tmp_path, ["Title", "Abstract", "Text."])
    venue = Venue(slug="testj", name="Test Venue", fields=[MANUSCRIPT], anonymous=True)
    status, _, _ = _run({"manuscript_file": ms}, venue)["front_matter.contact_email"]
    assert status == report.NOT_APPLICABLE


def test_email_deferred_to_title_page_rule(tmp_path, rules):
    rules({"title_page": {"required_items": ["corresponding_email"]}})
    ms = _docx(tmp_path, ["Title", "Abstract", "Text."])
    status, detail_or_msg, detail = _run({"manuscript_file": ms})["front_matter.contact_email"]
    assert status == report.NOT_APPLICABLE and "guidelines.title_page" in detail


# --- keywords -----------------------------------------------------------------------------------

KEYWORDS = Field(id="keywords", label="Keywords", type="text")
KEYWORD_VENUE = Venue(slug="testj", name="Test Venue", fields=[MANUSCRIPT, KEYWORDS])


def _text(tmp_path: Path, content: str, name: str = "ms.txt") -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_keywords_warned_when_absent_even_without_venue_rules(tmp_path):
    status, message, _ = _run({"manuscript_file": _text(tmp_path, "A title\n\nAbstract\n\nWe did it.\n")}, KEYWORD_VENUE)["front_matter.keywords"]
    assert status == report.WARNING and "no keywords" in message and "ms.txt" in message


def test_keywords_in_the_sub_pass(tmp_path):
    values = {"manuscript_file": _text(tmp_path, "A title\n"), "keywords": "mice; genomics"}
    status, _, detail = _run(values, KEYWORD_VENUE)["front_matter.keywords"]
    assert status == report.PASSED and "the .sub" in detail


@pytest.mark.parametrize(
    "name, content",
    [
        ("ms.txt", "A title\n\nKeywords: mice, genomics\n"),
        ("ms.txt", "A title\n\nKey words\nmice\n"),
        ("ms.txt", "We did it. Keywords: mice\n"),
        ("ms.txt", "Index Terms\u2014mice\n"),
        ("ms.md", "# A title\n\n**Keywords:** mice\n"),
        ("ms.tex", "\\documentclass{article}\n\\begin{document}\n\\keywords{mice}\nText.\n\\end{document}\n"),
        ("ms.tex", "\\documentclass{article}\n\\begin{document}\n\\begin{IEEEkeywords}mice\\end{IEEEkeywords}\n\\end{document}\n"),
    ],
)
def test_keywords_in_the_manuscript_pass(tmp_path, name, content):
    status, _, detail = _run({"manuscript_file": _text(tmp_path, content, name)}, KEYWORD_VENUE)["front_matter.keywords"]
    assert status == report.PASSED and name in detail


def test_the_word_keywords_in_prose_does_not_count(tmp_path):
    status, _, _ = _run({"manuscript_file": _text(tmp_path, "A title\n\nWe searched the keywords in the corpus.\n")}, KEYWORD_VENUE)["front_matter.keywords"]
    assert status == report.WARNING


def test_keywords_not_applicable_without_a_file():
    assert _run({}, KEYWORD_VENUE)["front_matter.keywords"][0] == report.NOT_APPLICABLE


# --- where the abstract ends when there is no Introduction --------------------------------------

from paperpush import document  # noqa: E402

PROSE_ABSTRACT = "We trained a model on mice and it worked well."
BODY = "The body of the paper starts here."


def _abstract(tmp_path: Path, content: str, name: str = "ms.txt", sub: str = "") -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return fm.manuscript_abstract(document.get(path), sub)[0]


@pytest.mark.parametrize("heading", ["Materials and methods", "Methods", "Results", "2. Results and Discussion", "METHODS", "Discussion:"])
def test_prose_abstract_ends_at_the_first_body_section(tmp_path, heading):
    found = _abstract(tmp_path, f"Abstract\n\n{PROSE_ABSTRACT}\n\n{heading}\n\n{BODY}\n")
    assert PROSE_ABSTRACT in found and BODY not in found


def test_short_prose_abstract_in_markdown(tmp_path):
    found = _abstract(tmp_path, f"# Title\n\n## Abstract\n\n{PROSE_ABSTRACT}\n\n## Materials and methods\n\n{BODY}\n", "ms.md")
    assert PROSE_ABSTRACT in found and BODY not in found


def test_any_marked_heading_of_the_same_level_ends_the_abstract(tmp_path):
    found = _abstract(tmp_path, f"# Title\n\n## Abstract\n\n{PROSE_ABSTRACT}\n\n## Mouse husbandry\n\n{BODY}\n", "ms.md")
    assert PROSE_ABSTRACT in found and BODY not in found


def test_a_lower_level_heading_does_not_end_the_abstract(tmp_path):
    found = _abstract(tmp_path, f"# Title\n\n## Abstract\n\n{PROSE_ABSTRACT}\n\n### Some note\n\nMore of it.\n\n## Mouse husbandry\n\n{BODY}\n", "ms.md")
    assert "More of it." in found and BODY not in found


def test_word_heading_styles_end_the_abstract(tmp_path):
    from zipfile import ZipFile

    def para(style: str, text: str) -> str:
        props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        return f"<w:p>{props}<w:r><w:t>{text}</w:t></w:r></w:p>"

    path = tmp_path / "ms.docx"
    body = para("Heading1", "Abstract") + para("", PROSE_ABSTRACT) + para("Heading1", "Mouse husbandry") + para("", BODY)
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", f"<w:document><w:body>{body}</w:body></w:document>")
    found = fm.manuscript_abstract(document.get(path))[0]
    assert PROSE_ABSTRACT in found and BODY not in found


def test_structured_abstract_keeps_its_labels_and_ends_at_the_repeat(tmp_path):
    text = f"Abstract\n\nBackground\n\nWe asked.\n\nMethods\n\nWe did.\n\nResults\n\nWe found.\n\nConclusions\n\nIt works.\n\nMethods\n\n{BODY}\n"
    found = _abstract(tmp_path, text)
    assert "It works." in found and BODY not in found


def test_structured_abstract_with_other_labels_is_not_cut_short(tmp_path):
    text = "Abstract\n\nContext\n\nA long history of work on this.\n\nObjective\n\nTo test it.\n\nDesign\n\nA cohort.\n\nResults\n\nWe found it.\n\nConclusions\n\nIt works.\n"
    assert "It works." in _abstract(tmp_path, text)


def test_structured_abstract_ends_at_a_body_section_after_its_conclusions(tmp_path):
    text = f"Abstract\n\nBackground\n\nWe asked.\n\nFindings\n\nWe found.\n\nConclusions\n\nIt works.\n\nDiscussion\n\n{BODY}\n"
    found = _abstract(tmp_path, text)
    assert "It works." in found and BODY not in found


def test_sub_abstract_marks_the_end_of_an_unmarked_one(tmp_path):
    found = _abstract(tmp_path, f"Abstract\n\n{PROSE_ABSTRACT}\n\nMouse husbandry\n\n{BODY}\n", sub=PROSE_ABSTRACT)
    assert PROSE_ABSTRACT in found and BODY not in found


def test_introduction_still_ends_the_abstract_first(tmp_path):
    text = f"Abstract\n\nBackground\n\nWe asked.\n\nResults\n\nWe found.\n\nIntroduction\n\n{BODY}\n"
    found = _abstract(tmp_path, text)
    assert "We found." in found and BODY not in found
