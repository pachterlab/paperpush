"""Regressions from reviewing the prose, rigor, front-matter, cover-letter, and placeholder checks.

Each test holds both directions: the false alarm stays gone and the true finding still fires.
"""

from __future__ import annotations

import datetime
import zipfile

import pytest

from paperpush import cover_letter_check as cl
from paperpush import document, feature_check, placeholders
from paperpush import front_matter_check as fm
from paperpush import prose_check as pc
from paperpush import rigor_check as rc
from paperpush import style_check as sc
from paperpush.database import Field, Venue
from paperpush.validate import NotApplicable, Unchecked
from tests.pdf_builder import filled_rect, text_line, write_pdf

VENUE = Venue(slug="testj", name="Test", fields=[Field(id="manuscript_file", label="Manuscript", type="file")])
TARGET = Venue(slug="plos_biology", name="PLOS Biology", fields=[])
OTHERS = [Venue(slug=s, name=n, fields=[]) for s, n in (("cell", "Cell"), ("science", "Science"), ("nature", "Nature"), ("nature_methods", "Nature Methods"))]


def _values(tmp_path, text: str, name: str = "paper.md") -> dict[str, str]:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return {"manuscript_file": str(path)}


def _cell_lines(text: str) -> dict[str, bool]:
    return dict(rc._resource_mentions(text)["cell line"])


def _antibodies(text: str) -> dict[str, bool]:
    return dict(rc._resource_mentions(text)["antibody"])


def _verdict(text: str) -> str:
    results = rc.reported_results(text)
    assert len(results) == 1, text
    return rc.judge(results[0])


def test_figure_labels_and_surnames_are_not_cell_lines(tmp_path):
    for text in ("See Figure S2. Details are in Table S2, and Fig. S2 (right).", "Groups split along PC1, PC2 and PC3, then clustering.", "As described by Ramos and Daudi (2019). BJ and XY designed it.", "A 5 KB, insert."):
        assert _cell_lines(text) == {}, text
    with pytest.raises(NotApplicable):
        rc._cell_lines_check(VENUE, _values(tmp_path, "See Figure S2. Details in Table S2."), False)
    for text, name in (("We cultured S2 cells.", "S2"), ("Drosophila S2 and Sf9 cells were used.", "S2"), ("PC3 cells were grown.", "PC3"), ("The lines DU145, PC3, and LNCaP were used.", "PC3"), ("Ramos and Daudi cells were used.", "Ramos"), ("PC3 (ATCC CRL-1435) were grown.", "PC3")):
        assert name in _cell_lines(text), text
    assert "HeLa" in _cell_lines("HeLa cells, and U2OS.")


@pytest.mark.parametrize(
    "text, verdict, reported",
    [
        ("z = 5.20, p = 2.0 × 10-7", "ok", 2e-7),
        ("z = 5.20, p = 1.99e-7", "ok", 1.99e-7),
        ("z = 5.20, p = 2.0 × 10⁻⁷", "ok", 2e-7),
        ("z = 5.20, p = 1.99 \\times 10^{-7}", "ok", 1.99e-7),
        ("z = 5.20, p = 1.99   10  -7", "ok", 1.99e-7),
        ("z = 5.20, p = 1.5 × 10-5", "inconsistent", 1.5e-5),
        ("t(50) = 0.50, p < 1e-5", "decision error", 1e-5),
        ("t(50) = 0.50, p < 1 × 10-5", "decision error", 1e-5),
        ("t(28) = 0.00, p = 1", "ok", 1.0),
        ("t(28) = 0.00, p = 1.00.", "ok", 1.0),
    ],
)
def test_statistics_scientific_notation(text, verdict, reported):
    r = rc.reported_results(text)[0]
    assert r["reported"] == pytest.approx(reported)
    assert rc.judge(r) == verdict


@pytest.mark.parametrize(
    "text, verdict",
    [
        ("A 1-tailed test gave t(28) = 1.80, p = .041.", "ok"),
        ("A directional test gave t(28) = 1.80, p = .041.", "ok"),
        ("One-tailed tests at the .05 level gave t(28) = 1.80, p = .041.", "ok"),
        ("A non-directional test gave t(28) = 1.80, p = .041.", "decision error"),
        ("A two-tailed test gave t(28) = 1.80, p = .041.", "decision error"),
        ("It was one-tailed. Then a later test gave t(28) = 1.80, p = .041.", "decision error"),
        ("z = 4.512, p = 6.4e-6", "ok"),
        ("z = 4.512, p = 9.4e-6", "inconsistent"),
        ("F(1, 28) = 5.00, p = .05", "inconsistent"),
        ("F(1, 28) = 0.50, p = .05", "decision error"),
    ],
)
def test_statistics_tails_rounding_and_alpha(text, verdict):
    assert _verdict(text) == verdict


def test_acronyms_known_and_roman_are_not_reported_unused():
    found = sc.acronyms(
        [
            "Results are the mean with the confidence interval (CI) throughout this whole paper and analysis. The CI was wide.",
            "Magnetic resonance imaging (MRI) was used in this cohort of patients at baseline. MRI was repeated.",
            "Antibodies came from Abcam (UK) and reagents came from Sigma (USA), with kits from Thermo (USA) in all assays performed.",
            "Iron (III) chloride and copper (II) sulfate were dissolved in water and used without purification.",
            "Enzymes were bought from a supplier in Ipswich (NEB) and the rest came from the other supplier (NEB) today.",
        ]
    )
    assert found.rarely_used == [] and found.redefined == []
    found = sc.acronyms(["We trained a deep neural network (DNN) on these images and then moved on to other things."])
    assert found.rarely_used == ["'DNN'"]
    found = sc.acronyms(["We trained a deep neural network (DNN) on images. A deep neural network (DNN) was also used on DNN text."])
    assert found.redefined


def test_rrids_antibody_words_and_rrid_window():
    for text in ("kept in 1% antibiotics (Thermo Fisher) and ProLong antifade (Invitrogen).", "The anti-inflammatory drug dexamethasone (Sigma-Aldrich) and the anticoagulant heparin (Sigma).", "ANTIBIOTICS (Thermo Fisher)."):
        assert _antibodies(text) == {}, text
    assert _antibodies("anti-GFP (Abcam, Cat# ab290) was used.") == {"anti-GFP": False}
    assert "antiGFP" in _antibodies("antiGFP (Abcam) was used.")
    assert "anti-PD-1" in _antibodies("anti-PD-1 (BioLegend) was used.")
    assert any("antibody" in name for name in _antibodies("the primary antibody (Abcam) was used."))
    assert _antibodies("anti-GFP (Abcam, Cat. No. ab290; RRID:AB_303395) was used.") == {"anti-GFP": True}
    assert _cell_lines("HeLa cells (ATCC, Cat. No. CCL-2, RRID:CVCL_0030) were used.") == {"HeLa": True}
    assert _cell_lines("HeLa cells (ATCC CCL-2,\nRRID:CVCL_0030) were used.") == {"HeLa": True}
    # A later sentence or table row's RRID is still not this resource's.
    assert _cell_lines("HeLa cells were used. Mice had RRID:IMSR_JAX:000664 listed.") == {"HeLa": False}
    assert _cell_lines("| HeLa, | ATCC |\n| x | RRID:CVCL_0030 |\n") == {"HeLa": False}
    assert _cell_lines("HeLa cells,\n\nRRID:CVCL_0030 elsewhere") == {"HeLa": False}


def test_no_text_is_not_a_pass(tmp_path):
    pdf = write_pdf(tmp_path / "scan.pdf", [filled_rect(50, 50, 500, 700, 0.5)])
    values = {"manuscript_file": str(pdf)}
    for run in (lambda: pc._check("repeated_words", VENUE, values), lambda: pc._check("spelling", VENUE, values), lambda: pc._repetition_check(VENUE, values), lambda: sc._numbers_check(VENUE, values), lambda: sc._conventions_check(VENUE, values)):
        out = run()
        assert [i.unchecked for i in out.issues] == [True]
    for fn, args in ((placeholders.check, ()), (rc._design_check, ()), (rc._statistics_check, ()), (rc._rrids_check, (False,)), (rc._trial_registration_check, ())):
        with pytest.raises(Unchecked):
            fn(VENUE, values, *args)
    with pytest.raises(NotApplicable):
        rc._design_check(VENUE, {})
    # One unreadable file among readable ones is noted, and the readable one is still scanned.
    multi = Venue(slug="testj", name="Test", fields=[Field(id="manuscript_file", label="Manuscript", type="file"), Field(id="supplementary_file", label="Supplementary information", type="file")])
    sup = tmp_path / "s.md"
    sup.write_text("A value of XX% was found.", encoding="utf-8")
    values = {"manuscript_file": str(pdf), "supplementary_file": str(sup)}
    from paperpush.requirements_check import _fields_for

    if _fields_for(multi, "supplementary"):
        out = placeholders.check(multi, values)
        assert sorted(i.unchecked for i in out.issues) == [False, True]


@pytest.mark.parametrize(
    "text",
    ["Patients were randomly assigned to drug or placebo in this double-blind randomized trial. The protocol was approved by our institutional review board.", "Patients were randomly assigned to drug or placebo in this double-blind randomized trial. This study went to the current ethics review."],
)
def test_review_board_does_not_make_a_trial_a_review(tmp_path, text):
    assert rc._trial_registration_check(VENUE, _values(tmp_path, text)).issues


def test_real_review_is_still_not_a_trial(tmp_path):
    text = "In this systematic review, patients were randomly assigned to drug or placebo in the double-blind randomized trials we included. In this review we discuss them."
    with pytest.raises(NotApplicable):
        rc._trial_registration_check(VENUE, _values(tmp_path, text))


def test_antibody_hosts_are_not_study_animals(tmp_path):
    hosts = "HeLa cells were stained with mouse anti-GAPDH and rabbit anti-LC3 (rabbit, Abcam), then with goat anti-mouse and goat anti-rabbit secondary antibodies."
    with pytest.raises(NotApplicable):
        rc._design_check(VENUE, _values(tmp_path, hosts))
    simulated = "Cancer patients benefit. Many patients respond. We simulated patients using a differential equation model on synthetic data."
    with pytest.raises(NotApplicable):
        rc._design_check(VENUE, _values(tmp_path, simulated))
    study = "Mice were housed in groups. Mouse weight was recorded and mice were given the drug."
    assert "study of animals" in rc._design_check(VENUE, _values(tmp_path, study)).detail
    people = "Patients were enrolled at three sites. We recruited patients and the patients gave consent."
    assert "human participants" in rc._design_check(VENUE, _values(tmp_path, people)).detail


@pytest.mark.parametrize(
    "text",
    [
        "X.Y. and Z.W. performed the experiments; Y.X. wrote the paper.",
        "XX mice and XY mice were compared in the four core genotypes model.",
        "A 46,XX patients group and karyotype data; XX patients were followed.",
        "A 47,XXX karyotype was found.",
        "Proceedings of the XXX International Conference on Machine Learning",
        "We assayed the TBC domain of the RabGAP.",
        "Tick-borne disease (TBD) is prevalent, and TBD cases rose.",
        "Todo el mundo",
    ],
)
def test_placeholders_leave_science_alone(text):
    assert placeholders.scan_text(text) == []


@pytest.mark.parametrize(
    "text, kind",
    [
        ("The value was X.X mg.", "number placeholder"),
        ("A rise of X.X% was seen.", "number placeholder"),
        ("The mean was X.X. The SD was small.", "number placeholder"),
        ("OR = X.X", "number placeholder"),
        ("XX patients were enrolled.", "number placeholder"),
        ("A value of XX% was found.", "number placeholder"),
        ("Author XXX to confirm.", "placeholder text"),
        ("This is XXX here.", "placeholder text"),
        ("The sample size is TBD.", "to-do note"),
        ("The sample size (TBD) will follow.", "to-do note"),
        ("Dates TBC.", "to-do note"),
        ("todo: fix this", "to-do note"),
        ("TODO fix this", "to-do note"),
    ],
)
def test_placeholders_still_fire(text, kind):
    assert kind in [k for k, _ in placeholders.scan_text(text)]


def test_numbers_products_and_names():
    for text in ("nuclei were stained with Hoechst 33342 and imaged today.", "Hoechst 33342 was added and imaged today.", "strain ATCC 25922 was grown overnight.", "reads were sequenced on a NovaSeq 6000 with cycles.", "Los Angeles, CA 90089, USA and others.", "Los Angeles, CA, 90089 and others."):
        assert sc.numbers_in(text) == [], text
    assert [(n.digits, n.style) for n in sc.numbers_in("We profiled 12000 cells.")] == [(5, "none")]
    assert sc.numbers_in("In 12000 cells this held.")
    assert sc.numbers_in("centrifuged at 12000 g for 10 min.")
    assert sc.numbers_in("At week 12 150 patients completed.") == []
    assert [n.style for n in sc.numbers_in("We counted 12 150 patients.")] == ["space"]
    assert sc.spelled_numbers("the ten-eleven translocation enzymes") == []
    assert sc.spelled_numbers("a fifty-fifty split") == []
    assert sc.spelled_numbers("in the twenty first century") == []
    assert sc.spelled_numbers("in the twenty second century") == []
    for text, value in (("we saw twenty-five patients", "25"), ("we saw one hundred and twelve mice", "112"), ("we saw ten first-year students", "10"), ("after a thirty second interval", "30"), ("we saw two hundred thirty mice", "230")):
        assert any(f"write {value}" in hit for hit in sc.spelled_numbers(text)), text
    assert sc.words_to_number("two thousand five hundred") == 2500
    assert sc.digit_sentence_starts("Samples arrived on Jan. 15 and on Feb. 3 at the core.") == []
    assert sc.digit_sentence_starts("The dose was max. 30 mg per day.") == []
    assert sc.digit_sentence_starts("The study ended. 15 patients were lost.")
    assert sc.digit_sentence_starts("Samples were spun for 10 min. 15 patients were lost.")


def test_abstract_citations():
    for text in ("Mean change was 12 points (95% CI [8, 16]) over one year.", "A [3,3]-sigmatropic rearrangement and a [1,2,4]triazole were made.", "In the range [1, 100] it held.", "During the first wave (March 2020 to June 2020) and (Spring 2021) we saw this.", "the Global Burden of Disease (2019) study", "the UK Census (2020) data"):
        assert fm.citations_in(text) == [], text
    for text in ("as shown [3].", "as shown [2, 5-7].", "(Smith et al., 2020)", "(Smith 2020)", "(Smith and Jones 2019; Lee 2021)", "Smith et al. (2020)", "Smith (2020) showed this.", "as shown by Smith (2020).", "(Smith, Jones & Lee, 2020)", "(Smith, Jones, and Lee 2020)", "as shown [@smith2020; @lee2021].", "as shown [@smith2020]."):
        assert fm.citations_in(text), text


def test_summary_statistics_line_is_not_the_abstract_heading(tmp_path):
    ms = "# A GWAS of height\n\nOpening passage.\n\n## Introduction\n\nHeight.\n\n## Methods\n\nWe used public data, and\nsummary statistics were pooled across cohorts [12].\n"
    path = tmp_path / "ms.md"
    path.write_text(ms, encoding="utf-8")
    assert fm.manuscript_abstract(document.get(path)) is None
    for text in ("Abstract\nWe did it.\n\nIntroduction\n", "Abstract: We did it.\n\nIntroduction\n", "Summary We did it.\n\nIntroduction\n", "ABSTRACT The thing.\n\nINTRODUCTION\n"):
        path = tmp_path / "a.txt"
        path.write_text(text, encoding="utf-8")
        found = fm.manuscript_abstract(document.Document(path))
        assert found and ("did it" in found[0] or "thing" in found[0]), text


@pytest.mark.parametrize(
    "written, stale",
    [("12/01/2026", True), ("13/05/2026", True), ("1. November 2026", True), ("9. November 2026", False), ("11/09/2026", False), ("09/11/2026", False), ("November 2026", False), ("November 9, 2026", False), ("October 2026", True), ("5.11.2026", False)],
)
def test_cover_letter_dates(written, stale):
    today = datetime.date(2026, 11, 9)
    found, readings = cl.parse_dates(written)
    issues, _ = cl.date_issue("f", "L", found, readings, today, 7)
    assert bool(issues) == stale


def test_spelling_keeps_its_own_word_pattern():
    table = {"hte": ["the"], "adn": ["and"], "thier": ["their"], "wih": ["with"], "recieve": ["receive"]}
    assert pc.spelling("the hte1 and adn2 mutants, wih1 expression", table) == []
    assert pc.spelling("we used nto/thier arrays in data.thier", table) == []
    assert len(pc.spelling("we did recieve hte thier data", table)) == 3
    assert "'" in pc._REPETITION_WORD.pattern and pc._WORD.pattern != pc._REPETITION_WORD.pattern


@pytest.mark.parametrize(
    "letter, names",
    [
        ("Cell atlases are of broad interest to the readers of your journal.\n", []),
        ("We submit our Computer Science study to your journal.\n", []),
        ("We submit it to the Academy of Science editors for this journal.\n", []),
        ("This work has not been published elsewhere and is being submitted exclusively to Nature Methods.\n", ["Nature Methods"]),
        ("It has not been published previously and we submit it to Science.\n", ["Science"]),
        ("We submit this for consideration in Nature.\n", ["Nature"]),
        ("Dear Editors of Nature,\n", ["Nature"]),
        ("We hope the readers of Cell will enjoy it.\n", ["Cell"]),
        ("Our earlier work published in Nature is cited for your journal.\n", []),
    ],
)
def test_cover_letter_venue(letter, names):
    assert [n for n, _ in cl.other_venue_mentions(letter, TARGET, OTHERS)] == names


def test_repeated_words_in_pdf_tables(tmp_path):
    lines = ["Table 1 Baseline characteristics of the cohort", "Age NA NA 34", "Sex Female Female Male", "Status yes yes 12", "We used the the model here"]
    pdf = write_pdf(tmp_path / "t.pdf", ["".join(text_line(72, 700 - 14 * i, line) for i, line in enumerate(lines))])
    out = pc._check("repeated_words", VENUE, {"manuscript_file": str(pdf)})
    assert len(out.issues) == 1 and "1 repeated word(s)" in out.issues[0].message and "the the" in out.issues[0].message
    assert pc.repeated_words("a log log plot") == []
    assert pc.repeated_words("we used the the model")
    assert pc.repeated_words("The Female Female row")  # not a PDF


def test_punctuation_brackets_conventions():
    assert pc.punctuation_spacing("mixed at a ratio of 1 : 100 in PBS") == []
    assert pc.punctuation_spacing("see Smith , 2020")
    assert pc.punctuation_spacing("the group 5 : it was large")
    assert pc.punctuation_spacing("we used anndata.AnnData objects and torch.Tensor here") == []
    assert pc.punctuation_spacing("the end.Next sentence began")
    assert pc.unbalanced("x in the half-open interval [0, 1) and y in (0, 1]") == []
    assert pc.unbalanced('tubing of 3" length') == []
    assert pc.unbalanced("a value [0, 1 was left open")
    assert pc.unbalanced("the (value was left open")
    assert pc.unbalanced('he said "hello there')
    assert sc.conventions(["Smith *et al*. reported this."]).problems == []
    assert sc.conventions(["Smith *et al* reported this."]).problems
    assert sc.conventions(["Smith et al reported this."]).problems
    assert sc.conventions(["In the variables i e and the others."]).problems == []
    assert sc.conventions(["That is, i. e. the others."]).problems


def _docx(path, body: str):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="w"><w:body>{body}</w:body></w:document>')
    return path


def test_tracked_formatting_and_prose_email(tmp_path):
    fmt = _docx(tmp_path / "f.docx", '<w:p><w:r><w:rPr><w:rPrChange w:id="2" w:author="R"><w:rPr/></w:rPrChange></w:rPr><w:t>x</w:t></w:r></w:p>')
    assert feature_check._word_file_cleanliness(VENUE, {"manuscript_file": str(fmt)}).issues
    clean = _docx(tmp_path / "c.docx", "<w:p><w:r><w:instrText> PAGE </w:instrText><w:t>x</w:t></w:r></w:p>")
    assert not feature_check._word_file_cleanliness(VENUE, {"manuscript_file": str(clean)}).issues
    assert fm.emails_in("Imaging was at UCLA. UK Biobank data were used.") == []
    assert fm.emails_in("The sequencing was done at Stanford. De novo assembly was performed.") == []
    for text in ("jdoe at uni dot edu", "jdoe at uni.edu", "jane [at] uni.edu", "jane (at) Uni.ac.uk", "jane.doe@uni.ac.uk"):
        assert fm.emails_in(text), text
