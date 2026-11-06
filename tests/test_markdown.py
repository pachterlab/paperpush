"""Markdown manuscripts: headings, sections, and the reference list are found as in PDF/Word."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperpush import document, manuscript
from paperpush.bibliography_check import read_bibliography
from paperpush.manuscript import headings, main_text_end, markdown_headings, markdown_to_text, normalize_heading

PAPER = """---
title: "Fast single-cell preprocessing"
author:
  - name: Ada Lovelace
abstract: |
  We present a fast method.
---

# Introduction {#sec:intro}

Single-cell RNA-seq is *widespread* [1], see [the docs](https://example.org/docs).

## 2. Materials and Methods ##

We used `kallisto` [2].

```python
# a comment, not a heading
x = 1
```

**Results**

It is fast.

Discussion
----------

It works.

**Data availability.** All data are public.

---

## References

1. Bray NL, et al. Near-optimal probabilistic RNA-seq quantification. *Nat Biotechnol*. 2016. <https://doi.org/10.1038/nbt.3519>
2. Melsted P, et al. Modular and efficient pre-processing of single-cell RNA-seq. bioRxiv. 2019.
"""


@pytest.fixture
def paper(tmp_path: Path) -> Path:
    path = tmp_path / "paper.md"
    path.write_text(PAPER, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "line",
    ["## References", "**References**", "# 7. References #", "References {#refs}", "### Bibliography:", "__Literature cited__"],
)
def test_reference_heading_matches_markdown_markup(line):
    assert main_text_end(f"Body text.\n\n{line}\n\n1. A ref.") is not None


@pytest.mark.parametrize(
    "line, expected",
    [
        ("## 2. Materials and Methods ##", "materials and methods"),
        ("**Materials and methods**", "materials and methods"),
        ("# Introduction {#sec:intro}", "introduction"),
        ("### Methods:", "methods"),
        ("MATERIALS AND METHODS", "materials and methods"),
    ],
)
def test_normalize_heading_drops_markdown_markup(line, expected):
    assert normalize_heading(line) == expected


def test_a_venue_heading_matches_with_markdown_markup():
    assert main_text_end("Body.\n\n## Appendix A\n\nMore.", ("Appendix",)) is not None


def test_markdown_headings_in_order_skipping_code():
    assert markdown_headings(PAPER) == ["abstract", "introduction", "materials and methods", "results", "discussion", "data availability", "references"]


def test_markdown_to_text_puts_headings_on_lines_of_their_own():
    text = markdown_to_text(PAPER)
    lines = text.splitlines()
    for heading in ("Introduction", "2. Materials and Methods", "Results", "Discussion", "References", "Abstract"):
        assert heading in lines, heading
    assert "#" not in text.replace("# a comment", "")  # the fenced block is gone too
    assert "a comment" not in text and "x = 1" not in text
    assert "widespread [1], see the docs (https://example.org/docs)" in text
    assert "We used kallisto [2]." in text
    assert "Fast single-cell preprocessing" in lines and "Ada Lovelace" in lines
    assert "1. Bray NL, et al. Near-optimal probabilistic RNA-seq quantification. Nat Biotechnol. 2016. https://doi.org/10.1038/nbt.3519" in lines


def test_a_link_whose_text_is_its_target_is_not_doubled():
    assert markdown_to_text("[https://doi.org/10.1/x](https://doi.org/10.1/x)").strip() == "https://doi.org/10.1/x"


def test_underscores_inside_words_are_not_emphasis():
    assert "snake_case_name" in markdown_to_text("a snake_case_name here")


def test_table_rules_are_dropped_and_rows_kept():
    text = markdown_to_text("| a | b |\n|---|:---:|\n| 1 | 2 |\n")
    assert "---" not in text and "| 1 | 2 |" in text


def test_headings_reads_the_marked_headings(paper):
    assert headings(paper) == markdown_headings(PAPER)


def test_headings_of_unmarked_markdown_fall_back_to_short_lines(tmp_path):
    path = tmp_path / "plain.markdown"
    path.write_text("Introduction\n\nSome text that is a long sentence of prose here.\n\nMethods\n\nMore.\n", encoding="utf-8")
    assert {"introduction", "methods"} <= set(headings(path))


def test_manuscript_text_and_document_text_read_markdown(paper):
    text = manuscript.manuscript_text(paper)
    assert text is not None and "## " not in text
    assert document.get(paper).text is not None and "Discussion" in document.get(paper).text.splitlines()
    # The main text ends at the reference heading.
    assert manuscript.words_before_references(paper) < len(text.split())


def test_reference_list_of_a_markdown_manuscript_is_read(paper):
    bib = read_bibliography(paper)
    assert bib is not None and bib.mode == "numbered"
    assert [r.key for r in bib.refs] == ["1", "2"]
    assert bib.cited == {"1", "2"}
