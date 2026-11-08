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
