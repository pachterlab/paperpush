"""Venue names, abbreviations, and "did you mean" hints for unknown venues."""

import pytest

from paperpush import database


@pytest.mark.parametrize("name", ["acl_arr", "ACL Rolling Review", "arr", "ARR", "ACL Rolling Review (ARR)"])
def test_names_and_abbreviations_resolve(name):
    assert database.get_venue(name).slug == "acl_arr"


def test_ambiguous_abbreviation_does_not_resolve():
    with pytest.raises(KeyError):
        database.get_venue("plos")


@pytest.mark.parametrize("query,expected", [("Nat Methods", "nature_methods"), ("acl", "acl_arr"), ("cvpr", "cvpr_2027"), ("PLoS Comp Bio", "plos_compbio")])
def test_suggestions(query, expected):
    assert expected in database.suggest_venues(query)
    assert expected in database.unknown_venue_message(query)


def test_cli_suggests_a_slug(capsys, tmp_path):
    from paperpush.cli import main

    (tmp_path / "m.md").write_text("# Title\n\nText.\n")
    assert main(["validate", "--venue", "Nat Methods", "--manuscript", str(tmp_path / "m.md")]) != 0
    assert "Did you mean: nature_methods" in capsys.readouterr().err
