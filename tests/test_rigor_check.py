"""Rigor and reproducibility checks: RRIDs, cell lines, study design, and statcheck."""

from __future__ import annotations

import pytest

from paperpush import rigor_check as rc
from paperpush.database import Field, Venue
from paperpush.validate import NotApplicable

VENUE = Venue(slug="testj", name="Test", fields=[Field(id="manuscript_file", label="Manuscript", type="file")])


def _values(tmp_path, text: str) -> dict[str, str]:
    path = tmp_path / "paper.md"
    path.write_text(text, encoding="utf-8")
    return {"manuscript_file": str(path)}


# --- p values -----------------------------------------------------------------


@pytest.mark.parametrize(
    "test, stat, df1, df2, expected",
    [("t", 2.20, 28, None, 0.0362), ("F", 3.16, 2, 57, 0.0499), ("chi2", 3.84, 1, None, 0.0500), ("r", 0.30, 48, None, 0.0343), ("z", 1.96, None, None, 0.0500)],
)
def test_p_values_match_reference_tables(test, stat, df1, df2, expected):
    assert rc.p_value(test, stat, df1, df2) == pytest.approx(expected, abs=5e-4)


@pytest.mark.parametrize(
    "text, verdict",
    [
        ("t(28) = 2.20, p = .036", "ok"),
        ("t(28) = 2.20, p = .046", "inconsistent"),
        ("t(8) = 1.10, p < .05", "decision error"),
        ("F(2, 57) = 3.16, p = .05", "ok"),
        ("χ2(1) = 10.5, p = .30", "decision error"),
        ("r(48) = .30, p = .034", "ok"),
        ("z = 2.5, p = 0.012", "ok"),
        ("t(100) = 8.5, p < 0.001", "ok"),
        ("A one-tailed test gave t(28) = 1.80, p = .041", "ok"),
    ],
)
def test_statcheck_verdicts(text, verdict):
    (result,) = rc.reported_results(text)
    assert rc.judge(result) == verdict


def test_statistics_not_applicable_without_results(tmp_path):
    with pytest.raises(NotApplicable):
        rc._statistics_check(VENUE, _values(tmp_path, "We describe an algorithm."))


# --- RRIDs ----------------------------------------------------------------------

_METHODS = (
    "Methods\n\nHeLa cells (RRID:CVCL_0030) and HEK293T cells were cultured. "
    "Rabbit anti-GFP (Abcam, Cat# ab290; RRID:AB_303395) and mouse anti-beta-actin (Sigma, clone AC-15) were used. "
    "C57BL/6J mice (Jackson Laboratory) were used. Images were analysed in Fiji (RRID:SCR_002285) and GraphPad Prism. T cells were sorted."
)


def test_resource_mentions_pair_each_rrid_with_its_resource():
    found = rc._resource_mentions(_METHODS)
    assert found["antibody"] == [("anti-GFP", True), ("anti-beta-actin", False)]
    assert found["cell line"] == [("HeLa", True), ("HEK293T", False)]
    assert found["organism"] == [("C57BL/6J", False)]
    assert found["software"] == [("Fiji", True), ("GraphPad Prism", False)]


def test_rrids_check_reports_missing_and_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "resolve_rrid", lambda rrid: rrid != "AB_303395")
    out = rc._rrids_check(VENUE, _values(tmp_path, _METHODS), True)
    messages = [i.message for i in out.issues]
    assert any("anti-beta-actin" in m for m in messages)
    assert any("HEK293T" in m for m in messages)
    assert any("GraphPad Prism" in m for m in messages)  # software counts once RRIDs are in use
    assert any("RRID:AB_303395 is not known" in m for m in messages)


def test_rrids_offline_skips_lookups(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "resolve_rrid", lambda rrid: pytest.fail("looked up while offline"))
    out = rc._rrids_check(VENUE, _values(tmp_path, _METHODS), False)
    assert "not looked up" in out.detail


def test_rrids_not_applicable_for_a_theory_paper(tmp_path):
    with pytest.raises(NotApplicable):
        rc._rrids_check(VENUE, _values(tmp_path, "We prove a theorem about graphs using Python."), True)


# --- cell lines -----------------------------------------------------------------


def test_cell_lines_problematic_and_unauthenticated(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "cellosaurus_problem", lambda name: ("CVCL_0372", "Contaminated. Shown to be a HeLa derivative.") if name == "KB" else False)
    out = rc._cell_lines_check(VENUE, _values(tmp_path, "The KB cell line and HeLa cells were used."), True)
    messages = [i.message for i in out.issues]
    assert any("authenticated" in m for m in messages)
    assert any("mycoplasma" in m for m in messages)
    assert any("KB (CVCL_0372)" in m and "HeLa derivative" in m for m in messages)


def test_cell_lines_authenticated(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "cellosaurus_problem", lambda name: False)
    text = "HeLa cells were authenticated by STR profiling and tested negative for mycoplasma."
    assert not rc._cell_lines_check(VENUE, _values(tmp_path, text), True).issues


# --- design -------------------------------------------------------------------


def test_design_for_an_animal_study(tmp_path):
    text = "Methods\n\nMale and female mice were used. Mice were randomly assigned to groups. The mice were housed in pairs. Investigators were blinded to genotype."
    out = rc._design_check(VENUE, _values(tmp_path, text))
    assert "3/5" in out.detail
    assert "sample size" in out.issues[0].message and "exclusion" in out.issues[0].message


def test_design_observational_cohort_skips_trial_items(tmp_path):
    text = "Patients were recruited from two clinics. Participants (54% female) were enrolled after consent. Patients with prior surgery were excluded. The sample size was based on a power analysis."
    out = rc._design_check(VENUE, _values(tmp_path, text))
    assert not out.issues and "3/3" in out.detail


def test_design_not_applicable_without_subjects(tmp_path):
    with pytest.raises(NotApplicable):
        rc._design_check(VENUE, _values(tmp_path, "We benchmark sorting algorithms on synthetic data."))


# --- trial registration -----------------------------------------------------------

_TRIAL = "Methods\n\nThis double-blind, placebo-controlled trial enrolled adults with asthma. "


@pytest.mark.parametrize(
    "registry_id",
    ["NCT01234567", "NCT 01234567", "ISRCTN12345678", "ACTRN12618000123456", "ChiCTR2000029308", "ChiCTR-IOR-17012345", "CTRI/2020/03/023456", "DRKS00012345", "IRCT20190101012345N1", "jRCT1234567890", "UMIN000012345", "PACTR201901234567890", "RBR-8fdnvw", "2022-500014-26-00", "EudraCT number 2015-001234-56"],
)
def test_trial_registry_id_formats_pass(tmp_path, registry_id):
    out = rc._trial_registration_check(VENUE, _values(tmp_path, f"{_TRIAL}It was registered as {registry_id}."))
    assert not out.issues and registry_id in out.detail


def test_trial_without_a_registry_id_warns(tmp_path):
    out = rc._trial_registration_check(VENUE, _values(tmp_path, _TRIAL))
    assert "no trial registry ID was found" in out.issues[0].message


def test_trial_claiming_registration_without_a_number_warns(tmp_path):
    out = rc._trial_registration_check(VENUE, _values(tmp_path, f"{_TRIAL}The trial was registered at ClinicalTrials.gov."))
    assert "gives no registry ID" in out.issues[0].message


def test_a_prospero_number_is_not_a_trial_registration(tmp_path):
    out = rc._trial_registration_check(VENUE, _values(tmp_path, f"{_TRIAL}The protocol is CRD42020123456."))
    assert out.issues


def test_registry_id_only_in_the_reference_list_does_not_count(tmp_path):
    out = rc._trial_registration_check(VENUE, _values(tmp_path, f"{_TRIAL}\n\nReferences\n\n1. Smith J. A trial. NCT01234567.\n"))
    assert out.issues


@pytest.mark.parametrize(
    "text",
    [
        "We benchmark sorting algorithms on synthetic data.",
        "Patients were recruited from two clinics and followed for two years. Earlier clinical trials reported mixed results.",
        "On this trial, participants pressed a key. Participants were randomly assigned to conditions.",
        "Mice were randomly assigned to treatment or vehicle.",
        "A systematic review of asthma therapy\n\nPatients were randomized to placebo or drug in 12 of the included studies.",
        "We conducted a narrative review of asthma. In one study, patients were randomized to placebo or drug.",
    ],
)
def test_trial_registration_not_applicable_to_non_trials(tmp_path, text):
    with pytest.raises(NotApplicable):
        rc._trial_registration_check(VENUE, _values(tmp_path, text))


@pytest.mark.parametrize("text", ["Patients were randomly assigned 1:1 to receive placebo or the study drug.", "We conducted a multicenter, open-label phase III trial of drug X.", "Our randomised controlled trial enrolled 200 adults."])
def test_trial_recognized_from_the_text(tmp_path, text):
    assert rc._trial_registration_check(VENUE, _values(tmp_path, text)).issues


def test_author_declarations_beat_the_text(tmp_path):
    no = {**_values(tmp_path, _TRIAL), "clinical_trial": "No"}
    with pytest.raises(NotApplicable):
        rc._trial_registration_check(VENUE, no)
    yes = {**_values(tmp_path, "We measured lung function."), "clinical_trial": "Yes"}
    assert "submission form" in rc._trial_registration_check(VENUE, yes).issues[0].message


def test_article_type_decides_when_set(tmp_path):
    trial = {**_values(tmp_path, "We measured lung function."), "article_type": "Clinical Trial"}
    assert "article type" in rc._trial_registration_check(VENUE, trial).issues[0].message
    review = {**_values(tmp_path, _TRIAL), "article_type": "Review"}
    with pytest.raises(NotApplicable):
        rc._trial_registration_check(VENUE, review)


def test_required_trial_statement_no_longer_warns_on_non_trials(tmp_path):
    from dataclasses import replace

    from paperpush.requirements import ManuscriptRequirements, StatementRules
    from paperpush.requirements_check import _heading_issues

    path = tmp_path / "paper.md"
    path.write_text("# Introduction\n\nA cohort study.\n\n# Methods\n\nWe followed patients.\n", encoding="utf-8")
    reqs = replace(ManuscriptRequirements(slug="testj"), statements=StatementRules(required=["clinical_trial_registration"]))
    assert _heading_issues(VENUE.fields[0], path, reqs) == []


# --- reader tasks -----------------------------------------------------------------


class _Queue:
    def __init__(self):
        self.tasks = []

    def needs_ai(self, task):
        self.tasks.append(task)


def test_reader_tasks_queued_for_a_manuscript(tmp_path):
    queue = _Queue()
    rc._queue_reader_tasks(queue, VENUE, _values(tmp_path, "Some text."))
    by_id = {t.id: t for t in queue.tasks}
    assert set(by_id) == {"ai.rigor.trial_registration", "ai.rigor.reporting_guideline"}
    assert "PRISMA 2020" in by_id["ai.rigor.reporting_guideline"].instructions
    assert "Never apply PRISMA to a narrative review" in by_id["ai.rigor.reporting_guideline"].instructions
    assert "ICMJE" in by_id["ai.rigor.trial_registration"].instructions


def test_no_reader_tasks_without_a_manuscript():
    queue = _Queue()
    rc._queue_reader_tasks(queue, VENUE, {})
    assert queue.tasks == []
