"""Offline tests for the SRA Submission Portal runner (paperpush/venues/ncbi/sra.py).

The HTML snippets mirror the live portal's markup as read on 2026-11-15: USWDS
radios and checkboxes drawn by their labels, Django formset rows named
``<prefix>-N-<column>``, BioSample packages grouped behind a parent radio.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from paperpush.database import get_venue
from paperpush.subfile import parse
from paperpush.venues import get_runner, get_venue_impl
from paperpush.venues.ncbi import sra

REPO = Path(__file__).resolve().parent.parent
SAMPLE = REPO / "tests/sub_files/sra/sra.sub.txt"
TODAY = dt.date(2026, 11, 15)


def _values(path: Path = SAMPLE) -> dict:
    values = dict(parse(path.read_text(encoding="utf-8")).values)
    for key in ("biosample_attributes_file", "sra_metadata_file"):
        if values.get(key):
            values[key] = str(REPO / values[key])
    values["data_files"] = "\n".join(str(REPO / line.strip()) for line in values.get("data_files", "").splitlines() if line.strip())
    return values


@pytest.fixture
def page():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(2000)
        yield page
        browser.close()


# --- registration and form -------------------------------------------------


def test_dispatch_and_guardrails():
    venue = get_venue_impl("sra")
    assert venue is sra.VENUE
    assert get_runner("sra") == venue.submit
    assert venue.supports_recovery
    assert "Submit" in venue.final_submit_names
    assert venue.requires_login
    assert get_venue("sra").supports_submit


def test_form_fields_match_the_runner():
    ids = {field.id for field in get_venue("sra").fields}
    assert set(sra.SUBMITTER_INPUTS) | {"country", "update_profile"} <= ids
    assert {"bioproject", "release_date", "title", "abstract", "relevance", "relevance_description", "external_links", "grants", "consortium_name", "consortium_url", "data_providers"} <= ids
    assert {"biosample_package", "biosample_attributes_file", "sra_metadata_file", "upload_method", "preload_folder", "data_files"} <= ids
    package = next(f for f in get_venue("sra").fields if f.id == "biosample_package")
    assert "Model.organism.animal.1.0" in package.options and "MIGS.ba.soil.6.0" in package.options
    upload = next(f for f in get_venue("sra").fields if f.id == "upload_method")
    assert {sra._upload_method({"upload_method": option}) for option in upload.options} == {"web", "preload"}


def test_sample_sub_passes_the_runner_checks():
    files = sra._check_values(_values(), today=TODAY)
    assert sorted(Path(path).name for path in files) == ["pp_test_1_R1.fastq.gz", "pp_test_1_R2.fastq.gz", "pp_test_2_R1.fastq.gz", "pp_test_2_R2.fastq.gz"]
    assert sra._check_values(_values(SAMPLE.with_name("sra_immediate.sub.txt")), today=TODAY)


# --- value checks ------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", ""), ("immediately", ""), ("Release immediately following processing", ""), ("2027-06-30", "2027-06-30"), ("2026-11-22", "2026-11-22")],
)
def test_release_date(value, expected):
    assert sra._release_date({"release_date": value}, today=TODAY) == expected


@pytest.mark.parametrize("value", ["2026-11-20", "2031-01-01", "30/06/2027", "next year"])
def test_release_date_outside_the_portal_range(value):
    with pytest.raises(ValueError, match="release_date"):
        sra._release_date({"release_date": value}, today=TODAY)


def _break(tmp_path, **changes) -> dict:
    values = _values()
    values.update(changes)
    return values


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sra_metadata_file": ""}, "sra_metadata_file is required"),
        ({"biosample_attributes_file": ""}, "go together"),
        ({"biosample_package": ""}, "go together"),
        ({"title": "too short"}, "20-250 characters"),
        ({"relevance_description": ""}, "relevance is 'Other'"),
        ({"data_files": ""}, "data_files is empty"),
        ({"upload_method": "FTP or Aspera preload"}, "preload_folder"),
        ({"upload_method": "Cloud"}, "upload_method"),
    ],
)
def test_check_values_refuses(tmp_path, changes, message):
    with pytest.raises(ValueError, match=message):
        sra._check_values(_break(tmp_path, **changes), today=TODAY)


def test_xlsx_metadata_and_attributes_are_read_and_uploaded_as_tsv(tmp_path):
    from tests.test_biosample import _xlsx

    values = _values()
    rows = [line.split("\t") for line in Path(values["sra_metadata_file"]).read_text().splitlines()]
    # NCBI's SRA_metadata.xlsx keeps the experiments on its SRA_data sheet, after an instructions sheet.
    metadata = _xlsx(tmp_path / "SRA_metadata.xlsx", rows, sheet="SRA_data")
    attributes = _xlsx(tmp_path / "attributes.xlsx", [line.split("\t") for line in Path(values["biosample_attributes_file"]).read_text().splitlines()])
    values.update(sra_metadata_file=str(metadata), biosample_attributes_file=str(attributes))
    assert sra._check_values(values, today=TODAY)
    uploaded = Path(sra._as_tsv(str(metadata), sra.METADATA_SHEET, tmp_path))
    assert uploaded.suffix == ".tsv" and uploaded.read_text().splitlines() == ["\t".join(r) for r in rows]
    assert Path(sra._as_tsv(str(attributes), "", tmp_path)).read_text().startswith("*sample_name\t")
    assert sra._as_tsv(str(tmp_path / "x.tsv"), "", tmp_path) == str(tmp_path / "x.tsv")


def test_existing_bioproject_needs_no_project_info():
    values = _values()
    values.update(bioproject="PRJNA000001", title="", abstract="", relevance="")
    assert sra._check_values(values, today=TODAY)


def test_preload_uploads_nothing_from_disk():
    values = _values()
    values.update(upload_method="FTP or Aspera preload", preload_folder="my_sra_upload", data_files="")
    assert sra._check_values(values, today=TODAY) == []


def test_data_files_must_match_the_metadata(tmp_path):
    values = _values()
    extra = tmp_path / "unlisted.fastq.gz"
    shutil.copy(values["data_files"].splitlines()[0], extra)
    with pytest.raises(ValueError, match="not named in sra_metadata_file: unlisted.fastq.gz"):
        sra._check_values({**values, "data_files": values["data_files"] + "\n" + str(extra)}, today=TODAY)
    with pytest.raises(ValueError, match="missing from data_files: pp_test_2_R2.fastq.gz"):
        sra._check_values({**values, "data_files": "\n".join(values["data_files"].splitlines()[:3])}, today=TODAY)


def test_metadata_needs_the_template_columns(tmp_path):
    metadata = tmp_path / "sra_metadata.tsv"
    metadata.write_text("sample_name\tlibrary_ID\tfilename\ns1\tlib1\ta.fastq.gz\n")
    with pytest.raises(ValueError, match="missing the column"):
        sra._check_values({**_values(), "sra_metadata_file": str(metadata)}, today=TODAY)


def test_metadata_library_ids_must_be_unique(tmp_path):
    source = Path(_values()["sra_metadata_file"]).read_text().splitlines()
    metadata = tmp_path / "sra_metadata.tsv"
    metadata.write_text("\n".join([source[0], source[1], source[2].replace("pp_lib_2", "pp_lib_1")]) + "\n")
    with pytest.raises(ValueError, match="unique library_ID"):
        sra._check_values({**_values(), "sra_metadata_file": str(metadata)}, today=TODAY)


def test_zip_and_large_files_are_refused(tmp_path, monkeypatch):
    values = _values()
    archive = tmp_path / "reads.zip"
    archive.write_bytes(b"PK")
    with pytest.raises(ValueError, match="zip"):
        sra._check_values({**values, "data_files": str(archive)}, today=TODAY)
    monkeypatch.setattr(sra, "WEB_UPLOAD_MAX_BYTES", 10)
    with pytest.raises(ValueError, match="over 2 GB"):
        sra._check_values(values, today=TODAY)


# --- page helpers ------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "segment"),
    [
        ("https://submit.ncbi.nlm.nih.gov/subs/sra/SUB16634978/submitter", "submitter"),
        ("https://submit.ncbi.nlm.nih.gov/subs/sra/SUB16634978/bp_info", "bp_info"),
        ("https://submit.ncbi.nlm.nih.gov/subs/sra/SUB16634978/files?task_id=abc", "files"),
        ("https://submit.ncbi.nlm.nih.gov/subs/sra/", ""),
    ],
)
def test_segment(url, segment):
    assert sra._segment(url) == segment


def test_package_group():
    groups = ["Pathogen", "MIGS.ba", "MIGS.eu", "MIMARKS.specimen", "MIMS.me"]
    assert sra._package_group("Pathogen.cl.1.0", groups) == "Pathogen"
    assert sra._package_group("MIGS.ba.soil.6.0", groups) == "MIGS.ba"
    assert sra._package_group("MIMS.me.water.6.0", groups) == "MIMS.me"
    assert sra._package_group("Model.organism.animal.1.0", groups) == ""


PACKAGES = """
<form>
<label for="id-package-7"><input type="radio" id="id-package-7" name="package-0" value="Model.organism.animal.1.0">Model organism or animal</label>
<label for="id-package-g-14"><input type="radio" id="id-package-g-14" name="package-0" value="MIGS.ba"
  onchange="document.getElementById('migs').hidden = false">MIGS Cultured Bacterial/Archaeal</label>
<div id="migs" hidden>
  <label for="id-package-34"><input type="radio" id="id-package-34" name="package-1" value="MIGS.ba.soil.6.0">soil</label>
</div>
</form>
"""


@pytest.mark.parametrize(("package", "radio"), [("Model.organism.animal.1.0", "#id-package-7"), ("MIGS.ba.soil.6.0", "#id-package-34")])
def test_choose_package_opens_its_group(page, package, radio):
    page.set_content(PACKAGES)
    sra._choose_package(page, package)
    assert page.locator(radio).is_checked()


def test_choose_unknown_package(page):
    page.set_content(PACKAGES)
    with pytest.raises(ValueError, match="no package"):
        sra._choose_package(page, "Generic")


SUBMITTER = """
<form>
<input id="id_submitter-first_name" value="Joseph"><input id="id_contact-department" value="DEPARTMENTHERE">
<input id="id_contact-city" value="city">
<input id="id_contact-country" value="USA"
  oninput="document.getElementById('menu').hidden = false">
<ul id="menu" hidden><li role="menuitem" onclick="document.getElementById('id_contact-country').value = this.textContent">United States of America (USA)</li></ul>
<label for="id_submitter-update_profile"><input type="checkbox" id="id_submitter-update_profile" checked>Update my contact information in profile</label>
</form>
"""


def test_fill_submitter_keeps_profile_values_and_leaves_the_profile_alone(page):
    page.set_content(SUBMITTER)
    sra._fill_submitter(page, {"department": "Biology", "country": "United States of America (USA)"})
    assert page.locator("#id_contact-department").input_value() == "Biology"
    assert page.locator("#id_contact-city").input_value() == "city"  # blank in the .sub: profile value kept
    assert page.locator("#id_contact-country").input_value() == "United States of America (USA)"
    assert not page.locator("#id_submitter-update_profile").is_checked()


def test_fill_submitter_can_update_the_profile(page):
    page.set_content(SUBMITTER.replace(" checked", ""))
    sra._fill_submitter(page, {"update_profile": "yes"})
    assert page.locator("#id_submitter-update_profile").is_checked()


GENERAL = """
<form>
<label for="id_select_existing_bioproject-select_existing_bioproject_0"><input type="radio" name="bp" id="id_select_existing_bioproject-select_existing_bioproject_0" checked>Yes</label>
<label for="id_select_existing_bioproject-select_existing_bioproject_1"><input type="radio" name="bp" id="id_select_existing_bioproject-select_existing_bioproject_1">No</label>
<input id="id_bioproject-project">
<label for="id_select_existing_biosample-select_existing_biosample_0"><input type="radio" name="bs" id="id_select_existing_biosample-select_existing_biosample_0" checked>Yes</label>
<label for="id_select_existing_biosample-select_existing_biosample_1"><input type="radio" name="bs" id="id_select_existing_biosample-select_existing_biosample_1">No</label>
<label for="id_release-choice_0"><input type="radio" name="r" id="id_release-choice_0">Release immediately following processing</label>
<label for="id_release-choice_1"><input type="radio" name="r" id="id_release-choice_1">Release on specified date</label>
<input id="id_release-release_date">
</form>
"""


def test_fill_general_new_project_new_samples_dated_release(page, monkeypatch):
    monkeypatch.setattr(sra, "_release_date", lambda values, today=None: values["release_date"])
    page.set_content(GENERAL)
    sra._fill_general(page, {"biosample_package": "Model.organism.animal.1.0", "release_date": "2027-06-30"})
    assert page.locator("#id_select_existing_bioproject-select_existing_bioproject_1").is_checked()
    assert page.locator("#id_select_existing_biosample-select_existing_biosample_1").is_checked()
    assert page.locator("#id_release-choice_1").is_checked()
    assert page.locator("#id_release-release_date").input_value() == "2027-06-30"


def test_fill_general_existing_project_and_samples(page):
    page.set_content(GENERAL)
    sra._fill_general(page, {"bioproject": "PRJNA000001", "release_date": ""})
    assert page.locator("#id_select_existing_bioproject-select_existing_bioproject_0").is_checked()
    assert page.locator("#id_bioproject-project").input_value() == "PRJNA000001"
    assert page.locator("#id_select_existing_biosample-select_existing_biosample_0").is_checked()
    assert page.locator("#id_release-choice_0").is_checked()


FORMSET = """
<form>
<input name="center-0-data_provider"><input name="center-0-url">
<a href="#" onclick="const i = document.querySelectorAll('[name$=-data_provider]').length;
  this.insertAdjacentHTML('beforebegin', `<input name='center-${i}-data_provider'><input name='center-${i}-url'>`); return false"><i class="fa fa-plus-circle"></i> <span>Add another data provider</span></a>
</form>
"""


def test_fill_rows_adds_formset_rows(page):
    page.set_content(FORMSET)
    sra._fill_rows(page, sra.PROVIDER_ROW, [["Center A", "https://a.example"], ["Center B", ""]])
    assert page.locator('[name="center-0-data_provider"]').input_value() == "Center A"
    assert page.locator('[name="center-0-url"]').input_value() == "https://a.example"
    assert page.locator('[name="center-1-data_provider"]').input_value() == "Center B"


FILES = """
<form>
<label for="id_files-upload_type_0"><input type="radio" name="u" id="id_files-upload_type_0">FTP or Aspera Command Line file preload</label>
<label for="id_files-upload_type_1"><input type="radio" name="u" id="id_files-upload_type_1">Web browser upload</label>
<input type="file" id="id_files-files" multiple onchange="
  const table = document.getElementById('table');
  table.innerHTML = [...this.files].map(f => `<tr><td><a href='#'>${f.name}</a></td><td class='created'></td></tr>`).join('');
  setTimeout(() => table.querySelectorAll('.created').forEach(td => td.textContent = '2026-11-15 14:03'), 300)">
<table id="table"></table>
<label for="id_autofinish-autofinish"><input type="checkbox" id="id_autofinish-autofinish" checked>Autofinish submission</label>
<input type="submit" id="id_sub_continue" value="Continue">
</form>
"""


def test_fill_files_uploads_and_never_autofinishes(page):
    from paperpush.venues.recovery import guard

    page.set_content(FILES)
    values = _values()
    files = sra._check_values(values, today=TODAY)
    sra._fill_files(guard(page), values, files, timeout_ms=2000)
    assert page.locator("#id_files-upload_type_1").is_checked()
    # Waited for the upload to finish (the Created stamp), not just the file name to show.
    assert page.locator("#table a").all_inner_texts() == [Path(path).name for path in files]
    assert page.locator("#table .created").all_inner_texts() == ["2026-11-15 14:03"] * len(files)
    assert not page.locator("#id_autofinish-autofinish").is_checked()


def test_continue_raises_with_the_portal_error(page):
    page.route("https://submit.ncbi.nlm.nih.gov/**", lambda route: route.fulfill(content_type="text/html", body=ATTRIBUTE_ERROR))
    page.goto("https://submit.ncbi.nlm.nih.gov/subs/sra/SUB1/attributes")
    with pytest.raises(ValueError, match="age, development stage"):
        sra._continue(page, "attributes", timeout_s=2)


def test_continue_returns_the_next_step(page):
    pages = {"attributes": '<form method="get" action="metadata"><input type="submit" id="id_sub_continue" value="Continue"></form>', "metadata": "<h2>SRA Metadata</h2>"}
    page.route("https://submit.ncbi.nlm.nih.gov/**", lambda route: route.fulfill(content_type="text/html", body=pages[route.request.url.split("/")[-1].split("?")[0]]))
    page.goto("https://submit.ncbi.nlm.nih.gov/subs/sra/SUB1/attributes")
    assert sra._continue(page, "attributes", timeout_s=5) == "metadata"


ATTRIBUTE_ERROR = """
<div class="message error fa-ul">Error: Sample has missing attribute(s).
<table><tr><td>pp_test_sample_1</td><td>age, development stage</td></tr></table></div>
<form><input type="button" id="id_sub_continue" value="Continue"></form>
"""


SURVEY = """
<button id="target" onclick="this.textContent = 'clicked'">Add grants</button>
<div class="QSIWebResponsive" style="position: fixed; inset: 0; background: rgba(0,0,0,.5)">
  <h1>Please tell us about your visit!</h1><button>Start Survey</button>
  <button onclick="/* the live survey sometimes ignores this */">No Thanks</button>
</div>
"""


def test_survey_overlay_is_removed_when_it_blocks_a_click(page):
    # Seen live on 2026-11-15: "No Thanks" left the Qualtrics overlay up over Project Info.
    page.set_content(SURVEY)
    sra._decline_survey(page)
    page.locator("#target").click(timeout=3000)
    assert page.locator("#target").inner_text() == "clicked"
    assert page.locator(".QSIWebResponsive").count() == 0


def test_continue_reloads_a_stalled_processing_page(page, monkeypatch):
    # Seen live on 2026-11-15: after Files, the tab sat on files?task_id=... although the
    # server had finished; only a reload reached the overview.
    monkeypatch.setattr(sra, "TASK_RELOAD_S", 0.5)
    state = {"loads": 0}

    def respond(route):
        url = route.request.url
        if "task_id" in url:
            state["loads"] += 1
            if state["loads"] > 1:
                route.fulfill(status=302, headers={"Location": "https://submit.ncbi.nlm.nih.gov/subs/sra/SUB1/overview"})
                return
            body = "<p>Processing files...</p>"
        elif url.endswith("/overview"):
            body = '<input type="submit" id="id_sub_submit" value="Submit">'
        else:
            body = '<form method="get" action="files"><input type="hidden" name="task_id" value="abc"><input type="submit" id="id_sub_continue" value="Continue"></form>'
        route.fulfill(content_type="text/html", body=body)

    page.route("https://submit.ncbi.nlm.nih.gov/**", respond)
    page.goto("https://submit.ncbi.nlm.nih.gov/subs/sra/SUB1/files")
    assert sra._continue(page, "files", timeout_s=10) == "overview"
    assert state["loads"] == 2


ATTRIBUTES = """
<form>
<div id="widget"><input type="file" id="id_attr_file-attr_file" onchange="
  const name = this.files[0].name, widget = document.getElementById('widget');
  widget.innerHTML = `<div><a href='#'>${name}</a> <span class='stamp'></span></div>`;
  setTimeout(() => widget.querySelector('.stamp').textContent = '461 bytes 2026-11-15 15:56', 200);
  setTimeout(() => widget.insertAdjacentHTML('beforeend', `<input type='hidden' id='id_attr_file-attr_file' value='abc/${name}'>`), 500)"></div>
<input type="submit" id="id_sub_continue" value="Continue">
</form>
"""


def test_single_file_upload_waits_for_the_stored_file(page):
    # Seen live on 2026-11-15: a Continue sent before the hidden field held the
    # stored file brought the run back to the attributes step.
    page.set_content(ATTRIBUTES)
    path = _values()["biosample_attributes_file"]
    sra._upload(page, sra.ATTRIBUTES_FILE, [path], timeout_ms=3000)
    assert page.locator('#id_attr_file-attr_file[type="hidden"]').get_attribute("value") == "abc/biosample_attributes.tsv"


def test_a_revisited_step_does_not_upload_a_stored_file_again(page):
    page.set_content('<form><div><a href="#">biosample_attributes.tsv</a> 461 bytes 2026-11-15 15:56</div>' '<input type="hidden" id="id_attr_file-attr_file" value="abc/biosample_attributes.tsv">' '<input type="submit" id="id_sub_continue" value="Continue"></form>')
    sra._upload(page, sra.ATTRIBUTES_FILE, [_values()["biosample_attributes_file"]], timeout_ms=1000)  # set_input_files on the hidden input would time out
