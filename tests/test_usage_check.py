"""paperpush.usage_check: serial comma, dashes, number formats, agreement, spelling variant."""

from __future__ import annotations

from pathlib import Path

from paperpush import report
from paperpush import usage_check as u
from paperpush.database import Field, Venue
from paperpush.prose_check import run_checks
from paperpush.validate import Checks

MANUSCRIPT = Field(id="manuscript_file", label="Manuscript", type="file")
AUTHORS = Field(id="authors", label="Authors", type="authorlist")
VENUE = Venue(slug="testj", name="Test Venue", fields=[MANUSCRIPT, AUTHORS])


def _run(path: Path, **values: str) -> dict[str, tuple[str, str, str]]:
    checks = Checks()
    run_checks(checks, VENUE, {"manuscript_file": str(path), **values})
    return {r.id: (r.status, " | ".join(i.message for i in r.issues), r.detail) for r in checks.results}


def _md(tmp_path: Path, body: str) -> Path:
    doc = tmp_path / "ms.md"
    doc.write_text("# Title\n\n## Introduction\n\n" + body + "\n")
    return doc


# --- serial comma ------------------------------------------------------------------------------


def test_serial_lists_tell_the_two_forms_apart():
    assert u.serial_lists("We measured height, weight, and age.") == [(True, "We measured »height, weight, and age«.")]
    [(serial, snippet)] = u.serial_lists("We measured height, weight and age in mice.")
    assert serial is False and "»height, weight and age«" in snippet
    assert u.serial_lists("Samples from Smith, Jones, or Lee were used.")[0][0] is True


def test_serial_lists_survive_an_empty_first_segment():
    # PDF text from an ICLR 2026 paper (Etu7q4KcZ7) left a bare ", " after a semicolon or colon;
    # the empty segment before it crashed the check (IndexError), so prose.serial_comma never ran.
    assert u.serial_lists("We compare two settings; , large language models and small ones.") == []
    assert u.serial_lists("Results: , deep neural networks and trees.") == []


def test_each_after_a_preposition_modifies_a_plural_subject():
    # ICLR 2026 false alarms: the subject is "scores"/"pipelines", not "each task".
    assert u.agreement("The scores for each task are computed by voting.") == []
    assert u.agreement("The pipelines for each modality are described in Appendix B.") == []
    # Still a slip, also at the start of a sentence.
    assert u.agreement("Each sample were measured twice.")
    assert u.agreement("In this study each sample were measured.")


def test_serial_lists_skip_introductory_phrases_and_asides():
    for sentence in (
        "In 2020, we sampled soil and water.",
        "However, cells and tissues were stained.",
        "Briefly, cells and media were removed.",
        "Compared with controls, IL-6 and TNF were higher.",
        "We found that, in mice, A and B increased.",
        "The cells were washed, and the medium was replaced.",
        "Our model, trained on ImageNet, and the baseline were compared.",
        "After 24 h, the cells, which had grown, and the media were collected.",
        "In this paper, we introduce PAPER2WEB, a benchmark dataset and evaluation framework.",
        "To create a document, the system created and saved a file.",
        "Protocols enable automatic, machine-to-machine publishing and rapid dissemination.",
    ):
        assert u.serial_lists(sentence) == [], sentence
    # A list can still follow the introductory phrase.
    assert u.serial_lists("To test this, we used X, Y and Z.")[0][0] is False
    assert u.serial_lists("Italy, Spain and France joined.")[0][0] is False
    assert u.serial_lists("The method is fast, cheap and robust.")[0][0] is False  # a list of adjectives is still a list


def test_serial_comma_check_reports_a_mix_as_a_suggestion(tmp_path):
    doc = _md(tmp_path, "We measured height, weight, and age. We also scored mood, sleep, and diet. Then we kept cats, dogs and mice.")
    status, message, _ = _run(doc)["prose.serial_comma"]
    assert status == report.SUGGESTION and "»cats, dogs and mice«" in message
    doc = _md(tmp_path, "We measured height, weight, and age.")
    status, _, detail = _run(doc)["prose.serial_comma"]
    assert status == report.PASSED and "all with the serial comma" in detail


def test_serial_comma_check_follows_the_venues_rule(monkeypatch, tmp_path):
    from paperpush import requirements as r

    base = r.ManuscriptRequirements(slug="testj", manuscript=r.ManuscriptRules(serial_comma=False))
    monkeypatch.setattr(r, "get_requirements", lambda slug: base)
    status, message, _ = _run(_md(tmp_path, "We measured height, weight, and age."))["prose.serial_comma"]
    assert status == report.WARNING and "leave it out" in message


# --- dashes and quotes -------------------------------------------------------------------------


def test_dashes():
    found = dict((m.split(":")[0], (lvl, m)) for lvl, m, *_ in u._dashes(["Ranges of 10-20 cells in 2019-2020 on 2020-01-31 in COVID-19 and chr1:10-20.", "The cells - which grew - died."], "text", None)[0])
    lvl, msg = found["2 number range(s) with a hyphen"]
    assert lvl == report.SUGGESTION and "»10-20«" in msg and "»2019-2020«" in msg and "»2020-01" not in msg
    assert "2 hyphen(s) used as a dash" in found
    stamp = "Proceedings of the 64th Annual Meeting (System Demonstrations), pages 626-635 July 2-7, 2026 \u00a92026 Association"
    assert u._dashes([stamp], "pdf", None)[0] == []


def test_dash_spacing_and_latex_quotes():
    found, _ = u._dashes(["One---two and three --- four.", 'He said "hello" to 10--20 people.'], "tex", None)
    messages = " | ".join(m for _, m, *_ in found)
    assert "Dashes that break a sentence written two ways" in messages
    assert "number range" not in messages  # "10--20" is an en dash in LaTeX
    [(level, quote)] = [(lvl, m) for lvl, m, *_ in found if "straight double quote" in m]
    assert level == report.WARNING and "»\"«hello" in quote


# --- number formats ----------------------------------------------------------------------------


def test_number_format():
    found, _ = u._number_format(["With n = 12 and n=3 and (n =4); P < 0.05 and p<.01; 5 mg, 3mg, 10 %, 5%."], "text", None)
    messages = " | ".join(m for _, m, *_ in found)
    assert "spaced on one side only" in messages and "»n =4«" in messages
    assert "'=', '<', and '>' written two ways" in messages
    assert "Units after a number written two ways" in messages and "Percent signs written two ways" in messages
    assert "P values written two ways" in messages and "The P of P values written two ways" in messages
    assert all(level == report.SUGGESTION for level, *_ in found)


def test_number_format_skips_spacing_in_a_pdf():
    found, detail = u._number_format(["n = 12 and n=3; 5 mg and 3mg; P < 0.05 and P < 0.01"], "pdf", None)
    assert found == [] and "not compared in a PDF" in detail


# --- usage -------------------------------------------------------------------------------------


def test_agreement_slips():
    for text, fix in (
        ("these data is noisy", "plural"),
        ("a criteria was used", "the singular is 'criterion'"),
        ("the phenomena was rare", "'phenomena' is plural"),
        ("we ran these analysis.", "the plural is 'analyses'"),
        ("there is several ways", "there are"),
        ("each sample were run", "takes a singular verb"),
        ("the results shows that", "singular verb"),
        ("its' value", "write 'its'"),
    ):
        [hit] = u.agreement(text)
        assert fix in hit, (text, hit)


def test_agreement_spares_objects_of_prepositions_and_noun_adjuncts():
    for text in ("The number of criteria is small.", "The quality of these data is high.", "One of the results is odd.", "These analysis pipelines ran.", "The data are clear."):
        assert u.agreement(text) == [], text


def test_usage_check(tmp_path):
    long = " ".join(["word"] * 55) + "."
    doc = _md(tmp_path, f"We don't know why. The data are clear, but this data is odd. These data is noisy.\n\n{long}")
    status, message, _ = _run(doc)["prose.usage"]
    assert status == report.WARNING
    assert "agreement slip" in message and "'Data' written two ways" in message
    assert "'don't'" in message and "more than 50 words" in message


# --- spelling variant --------------------------------------------------------------------------


def test_spelling_variant_mixed(tmp_path):
    doc = _md(tmp_path, "The colour of the tumor and the color of the tumour.")
    status, message, _ = _run(doc)["prose.spelling_variant"]
    assert status == report.SUGGESTION and "mixes American" in message


def test_spelling_variant_against_the_authors_location(tmp_path):
    doc = _md(tmp_path, "The colour of the tumours was analysed in the centre.")
    us = "Ada Lovelace | ada@example.edu | Stanford University, Stanford, CA 94305, USA |  | yes"
    status, message, _ = _run(doc, authors=us)["prose.spelling_variant"]
    assert status == report.SUGGESTION and "affiliation is in the US" in message
    uk = "Ada Lovelace | ada@example.ac.uk | University of Oxford, Oxford, UK |  | yes"
    assert _run(doc, authors=uk)["prose.spelling_variant"][0] == report.PASSED
    assert _run(doc)["prose.spelling_variant"][0] == report.PASSED  # no authors: nothing to compare with


def test_spelling_variant_defers_to_the_venues_rule(monkeypatch, tmp_path):
    from paperpush import requirements as r

    base = r.ManuscriptRequirements(slug="testj", manuscript=r.ManuscriptRules(language="British English"))
    monkeypatch.setattr(r, "get_requirements", lambda slug: base)
    status, _, detail = _run(_md(tmp_path, "The colour."))["prose.spelling_variant"]
    assert status == report.NOT_APPLICABLE and "layout.language" in detail


# --- "significant" ---------------------------------------------------------------------------


def test_significant_uses_sorts_statistical_plain_and_unclear():
    uses = u.significant_uses(
        [
            "Gene X plays a significant role and has attracted significant interest.",
            "Expression was significantly higher in mutants (P < 0.01, two-sided t-test).",
            "The difference was statistically significant.",
            "Our method yields a significant improvement over the baseline (Fig. 2).",
            "This approach is significantly more expensive than sequencing.",
            "We discuss the significance of these findings, which are of great significance.",
        ]
    )
    assert len(uses.statistical) == 2
    assert len(uses.plain) == 5
    assert uses.unclear == ["Our method yields a »significant« improvement over the..."]


def test_significant_skips_technical_senses_and_headings():
    uses = u.significant_uses(["Values are rounded to three significant figures.", "Significance Statement", "The least significant bits are dropped."])
    assert uses.statistical == uses.plain == uses.unclear == []


def test_significant_plain_use_is_a_warning_when_the_paper_reports_tests(tmp_path):
    doc = _md(tmp_path, "Gene X plays a significant role in growth.\n\nMutants grew faster (P < 0.05, t-test).")
    checks = Checks()
    run_checks(checks, VENUE, {"manuscript_file": str(doc)})
    [result] = [r for r in checks.results if r.id == "prose.significant"]
    assert result.status == report.WARNING and "a »significant« role" in result.issues[0].message
    assert not [t for t in checks.ai_tasks if t.id == u.AI_SIGNIFICANT_ID]


def test_significant_plain_use_is_a_suggestion_without_statistics(tmp_path):
    status, message, _ = _run(_md(tmp_path, "This library plays a significant role in the ecosystem."))["prose.significant"]
    assert status == report.SUGGESTION and "role" in message


def test_significant_unclear_uses_go_to_the_ai_check(tmp_path):
    doc = _md(tmp_path, "Our method gives a significant improvement over the baseline.\n\nGroups differed (P = 0.03, Wilcoxon test).")
    checks = Checks()
    run_checks(checks, VENUE, {"manuscript_file": str(doc)})
    [result] = [r for r in checks.results if r.id == "prose.significant"]
    assert result.status == report.PASSED and "ai.significant" in result.detail
    [task] = [t for t in checks.ai_tasks if t.id == u.AI_SIGNIFICANT_ID]
    assert "»significant« improvement" in task.instructions and "report 'suggest'" not in task.instructions


def test_significant_not_used(tmp_path):
    status, _, detail = _run(_md(tmp_path, "The cells grew quickly."))["prose.significant"]
    assert status == report.PASSED and "not used" in detail
