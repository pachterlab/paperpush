"""Sorting a folder of submission files into a venue's upload fields by name."""

from __future__ import annotations

import pytest

from paperpush.database import get_venue
from paperpush.filematch import edit_distance, kinds_of, match_files

FOLDER = [
    "proj/manuscript.docx",
    "proj/Cover_Letter.pdf",
    "proj/figures/Fig2.pdf",
    "proj/figures/Fig10.pdf",
    "proj/figures/fig1.pdf",
    "proj/supp/SuppTable1.xlsx",
    "proj/supp/supplementary_info.pdf",
    "proj/table1.xlsx",
    "proj/README.md",
]


@pytest.mark.parametrize(
    "path, kind",
    [
        ("main.pdf", "manuscript"),
        ("Manuscirpt.docx", "manuscript"),  # a typo
        ("coverletter.pdf", "cover_letter"),
        ("letter-to-editor.docx", "cover_letter"),
        ("SuppFig1.tif", "supplement"),  # a supplementary figure is a supplement
        ("Suplementary_Table_2.xlsx", "supp_table"),
        ("Table1.xlsx", "table"),
        ("Fig 3.TIF", "figure"),
        ("figures/a.png", "figure"),  # the folder says so
        ("competing_interests.pdf", "declaration"),
    ],
)
def test_kinds_from_names_and_folders(path, kind):
    assert kinds_of(path)[0] == kind


def test_a_folder_named_paper_does_not_make_everything_the_manuscript():
    assert kinds_of("my_paper/notes.pdf") == []
    assert kinds_of("paper/cover.pdf") == ["cover_letter"]


def test_short_names_must_match_exactly():
    assert kinds_of("tag.pdf") == []  # not "tab"
    assert edit_distance("supplementry", "supplementary", 2) == 1


def test_nature_gets_every_kind_in_its_own_field():
    a = match_files(get_venue("nature"), FOLDER).assignments
    assert a["manuscript_file"] == "manuscript.docx"
    assert a["cover_letter"] == "Cover_Letter.pdf"
    assert a["figure_files"] == "fig1.pdf\nFig2.pdf\nFig10.pdf"  # natural order
    assert a["table_files"] == "table1.xlsx"
    assert a["supplementary_tables"] == "SuppTable1.xlsx"
    assert a["supplementary_files"] == "supplementary_info.pdf"


def test_files_a_venue_has_no_field_for_are_reported_not_forced():
    result = match_files(get_venue("biorxiv"), FOLDER).to_dict(get_venue("biorxiv"))
    unmatched = {m["name"]: m["reason"] for m in result["unmatched"]}
    assert "README.md" in unmatched
    assert "cover letter" in unmatched["Cover_Letter.pdf"]
    # bioRxiv has no table field for a supplementary table, so it is a supplement.
    assert "SuppTable1.xlsx" in result["assignments"]["supplementary_files"]


def test_a_lone_unnamed_document_is_the_manuscript():
    a = match_files(get_venue("biorxiv"), ["x/rich_et_al_2026.pdf", "x/fig1.png"]).assignments
    assert a["manuscript_file"] == "rich_et_al_2026.pdf"


def test_a_single_file_field_takes_only_one():
    a = match_files(get_venue("nature"), ["cover.pdf", "cover_letter_v2.pdf"]).assignments
    assert a["cover_letter"] == "cover.pdf"
