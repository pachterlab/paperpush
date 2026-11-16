"""SRA (Sequence Read Archive) Submission Portal: sign in through NCBI, then fill the wizard.

The wizard (https://submit.ncbi.nlm.nih.gov/subs/sra/) has eight steps, each its
own URL under ``/subs/sra/SUB#######/``: submitter, general, bp_info (only for a
new BioProject), sample_type and attributes (only for new BioSamples), metadata,
files, and overview (Review & Submit). Steps are skipped depending on the
answers on the General Info page, so :meth:`SRA.submit` dispatches on the URL
the portal lands on after each Continue instead of assuming a fixed order.

The portal validates the uploaded BioSample attributes and SRA metadata files
server-side and re-renders the page with a ``.message.error`` block; the runner
raises with that message rather than retrying. It stops on the Review & Submit
page and never clicks its ``Submit`` button, and it leaves the Files page's
"Autofinish submission" box unticked, since that box sends the submission by
itself once the files pass their checks.

Selectors were read from the live portal on 2026-11-15.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import tempfile
import time
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

from ... import biosample
from ..common import DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts, hold_open, hold_open_on_failure, open_run_context, parse_pipe_file_list
from ..recovery import guard, step, unrecovered
from ..recovery_channel import browser_launch_args
from .main import NCBIVenue

logger = logging.getLogger(__name__)

PORTAL_URL = "https://submit.ncbi.nlm.nih.gov/subs/sra/"
NEW_SUBMISSION = "#id_sub_new"
# "You have at least one unfinished submission below ..." confirmation dialog.
START_ANYWAY = "#create-new-submission-anyway"
CONTINUE = "#id_sub_continue"
FINAL_SUBMIT = "#id_sub_submit"
ERROR_MESSAGE = ".message.error:visible"
# Qualtrics "Please tell us about your visit!" survey; it can cover the form at any step.
SURVEY_DECLINE = "No Thanks"
SURVEY_OVERLAY = ".QSIWebResponsive"
SURVEY_REMOVE_JS = "() => document.querySelectorAll('.QSIWebResponsive').forEach(e => e.remove())"

SUBMITTER_INPUTS = {
    "submitter_first_name": "#id_submitter-first_name",
    "submitter_middle_name": "#id_submitter-middle_name",
    "submitter_last_name": "#id_submitter-last_name",
    "submitter_email": "#id_submitter-email",
    "submitter_secondary_email": "#id_submitter-sec_email",
    "submitting_organization": "#id_contact-institution",
    "submitting_organization_url": "#id_contact-institution_url",
    "department": "#id_contact-department",
    "phone": "#id_contact-phone",
    "fax": "#id_contact-fax",
    "street": "#id_contact-street",
    "city": "#id_contact-city",
    "state": "#id_contact-sub",
    "postal_code": "#id_contact-postal_code",
}
COUNTRY_INPUT = "#id_contact-country"
UPDATE_PROFILE = "id_submitter-update_profile"

BIOPROJECT_RADIO = {True: "id_select_existing_bioproject-select_existing_bioproject_0", False: "id_select_existing_bioproject-select_existing_bioproject_1"}
BIOPROJECT_INPUT = "#id_bioproject-project"
BIOSAMPLE_RADIO = {True: "id_select_existing_biosample-select_existing_biosample_0", False: "id_select_existing_biosample-select_existing_biosample_1"}
RELEASE_RADIO = {"immediate": "id_release-choice_0", "date": "id_release-choice_1"}
RELEASE_DATE_INPUT = "#id_release-release_date"
# The release datepicker's own limits (minDate '+1w', maxDate '+1461d').
RELEASE_MIN_DAYS, RELEASE_MAX_DAYS = 7, 1461

PROJECT_TITLE = "#id_relevance-project_title"
PROJECT_DESCRIPTION = "#id_relevance-public_description"
RELEVANCE = "#id_relevance-relevance"
RELEVANCE_DESCRIPTION = "#id_relevance-relevance_description"
CONSORTIUM_NAME = "#id_consortium-consortium_name"
CONSORTIUM_URL = "#id_consortium-url"
# Django formsets: row N's inputs are named <prefix>-N-<column>; "Add another ..." appends a row.
LINK_ROW = {"prefix": "link", "add": "Add another link", "columns": ("description", "url")}
GRANT_ROW = {"prefix": "custom_grant", "add": "Add another grant", "columns": ("grant_id", "title", "agency", "abbr")}
PROVIDER_ROW = {"prefix": "center", "add": "Add another data provider", "columns": ("data_provider", "url")}
ADD_GRANTS = "Add grants"
ENTER_GRANTS_MANUALLY = "#enter-manually-button"

PACKAGE_RADIO = 'input[type="radio"][name^="package-"][value="{value}"]'
PACKAGE_GROUPS = 'input[type="radio"][id^="id-package-g-"]'
ATTRIBUTES_FROM_FILE = "id_attributes_source-attributes_source_1"
ATTRIBUTES_FILE = "#id_attr_file-attr_file"
METADATA_FROM_FILE = "id_data_table_source-data_table_source_1"
METADATA_FILE = "#id_metadata-metadata_file"

UPLOAD_RADIO = {"web": "id_files-upload_type_1", "preload": "id_files-upload_type_0"}
DATA_FILES_INPUT = "#id_files-files"
SELECT_PRELOAD_FOLDER = "#open_folder_selection_dialog"
PRELOAD_DIALOG = ".ui-dialog:visible"
PRELOAD_USE_FOLDER = "#save-button"
AUTOFINISH = "id_autofinish-autofinish"
# An uploaded file's row gains its "Created" timestamp once the upload is stored:
# a table row on Files, the file's block on the single-file attributes and metadata pages.
UPLOADED_ROW = "tr"
UPLOADED_BLOCK = "div"
UPLOADED_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}")
# The portal sends larger browser uploads through the Aspera Connect plugin instead.
WEB_UPLOAD_MAX_BYTES = 2 * 1024**3

METADATA_REQUIRED_COLUMNS = ("library_ID", "title", "library_strategy", "library_source", "library_selection", "library_layout", "platform", "instrument_model", "design_description", "filetype", "filename")
# Files are checked after upload (format, pairing) before the Review page renders.
FILES_PROCESSING_TIMEOUT_S = 1800
PAGE_ADVANCE_TIMEOUT_S = 120
TASK_RELOAD_S = 15


# --- values ----------------------------------------------------------------


def _yes(value) -> bool:
    return str(value or "").strip().lower() in {"yes", "y", "true", "1", "on"}


def _lines(value: str) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _upload_method(values: dict) -> str:
    method = str(values.get("upload_method") or "Web browser upload").strip().lower()
    if method.startswith("web"):
        return "web"
    if "preload" in method or method.startswith(("ftp", "aspera")):
        return "preload"
    raise ValueError(f"upload_method: expected 'Web browser upload' or 'FTP or Aspera preload', not {values.get('upload_method')!r}")


def _release_date(values: dict, today: _dt.date | None = None) -> str:
    """'' for release immediately after processing, else the YYYY-MM-DD date."""
    value = str(values.get("release_date") or "").strip()
    if not value or value.lower().startswith(("immediate", "release immediately")):
        return ""
    try:
        date = _dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"release_date: give a date as YYYY-MM-DD (or leave it blank to release immediately), not {value!r}") from None
    today = today or _dt.date.today()
    if not today + _dt.timedelta(days=RELEASE_MIN_DAYS) <= date <= today + _dt.timedelta(days=RELEASE_MAX_DAYS):
        raise ValueError(f"release_date: the SRA portal accepts dates from one week to four years ahead ({today + _dt.timedelta(days=RELEASE_MIN_DAYS)} to {today + _dt.timedelta(days=RELEASE_MAX_DAYS)}), not {date}")
    return date.isoformat()


# The sheet of NCBI's SRA_metadata.xlsx that holds the experiments.
METADATA_SHEET = "SRA_data"


def _sheet(path: Path, sheet: str) -> str:
    return sheet if path.suffix.lower() == ".xlsx" else ""


def _metadata_rows(path: Path) -> tuple[list[str], list[dict]]:
    header, rows = biosample.read_table(path, _sheet(path, METADATA_SHEET))
    if not header:
        raise ValueError(f"sra_metadata_file is empty: {path}")
    return header, rows


def _as_tsv(path: str, sheet: str, workdir: Path) -> str:
    """The file to upload: ``path`` itself, or an ``.xlsx`` workbook's sheet saved as tab-delimited text."""
    source = Path(path).expanduser()
    if source.suffix.lower() != ".xlsx":
        return str(source)
    return str(biosample.write_tsv(source, workdir / f"{source.stem}.tsv", _sheet(source, sheet)))


def _metadata_filenames(header: list[str], rows: list[dict]) -> list[str]:
    columns = [col for col in header if re.fullmatch(r"filename\d*|fasta_file", col)]
    return [row[col] for row in rows for col in columns if row.get(col)]


def _check_values(values: dict, today: _dt.date | None = None) -> list[str]:
    """Check what the portal would reject before opening a browser; return the files to upload."""
    metadata = Path(str(values.get("sra_metadata_file") or "")).expanduser()
    if not str(values.get("sra_metadata_file") or "").strip():
        raise ValueError("sra_metadata_file is required: the SRA_data sheet of NCBI's SRA metadata template, saved as tab-delimited text")
    if not metadata.is_file():
        raise ValueError(f"sra_metadata_file: file does not exist: {metadata}")
    if metadata.suffix.lower() == ".xls":
        raise ValueError("sra_metadata_file: save the old .xls workbook as .xlsx or tab-delimited text first")
    header, rows = _metadata_rows(metadata)
    if not rows:
        raise ValueError("sra_metadata_file has a header row but no experiments")
    missing = [col for col in METADATA_REQUIRED_COLUMNS if col not in header]
    if not ({"sample_name", "biosample_accession"} & set(header)):
        missing.insert(0, "sample_name (or biosample_accession)")
    if missing:
        raise ValueError(f"sra_metadata_file is missing the column(s) {', '.join(missing)}; start from NCBI's SRA_metadata.xlsx template")
    if len(rows) > 1000:
        raise ValueError("SRA takes at most 1000 samples per submission; split them across several submissions that cite the same BioProject")
    library_ids = [row.get("library_ID", "") for row in rows]
    if len(set(library_ids)) != len(library_ids):
        raise ValueError("sra_metadata_file: every row needs a unique library_ID")
    named = _metadata_filenames(header, rows)
    if not named:
        raise ValueError("sra_metadata_file names no files in its filename column(s)")
    if len(set(named)) != len(named):
        raise ValueError("sra_metadata_file: each file may be listed only once (file names must be unique)")

    package = str(values.get("biosample_package") or "").strip()
    attributes = str(values.get("biosample_attributes_file") or "").strip()
    if bool(package) != bool(attributes):
        raise ValueError("biosample_package and biosample_attributes_file go together: give both to register new BioSamples, or neither when the samples are already registered")
    if attributes and not Path(attributes).expanduser().is_file():
        raise ValueError(f"biosample_attributes_file: file does not exist: {attributes}")
    if attributes and Path(attributes).suffix.lower() == ".xls":
        raise ValueError("biosample_attributes_file: save the old .xls workbook as .xlsx or tab-delimited text first")

    if not str(values.get("bioproject") or "").strip():
        title, abstract = str(values.get("title") or "").strip(), str(values.get("abstract") or "").strip()
        if not 20 <= len(title) <= 250:
            raise ValueError("title: a new BioProject needs a project title of 20-250 characters (or give an existing bioproject accession)")
        if not 20 <= len(abstract) <= 4000:
            raise ValueError("abstract: a new BioProject needs a public description of 20-4000 characters")
        if str(values.get("relevance") or "").strip() == "Other" and not str(values.get("relevance_description") or "").strip():
            raise ValueError("relevance_description is required when relevance is 'Other'")
    _release_date(values, today)

    if _upload_method(values) == "preload":
        if not str(values.get("preload_folder") or "").strip():
            raise ValueError("preload_folder: name the folder you uploaded this submission's files to by FTP or Aspera")
        return []
    files = [str(Path(line.split("|")[0].strip()).expanduser()) for line in _lines(values.get("data_files", ""))]
    if not files:
        raise ValueError("data_files is empty; list the sequence files to upload (or use upload_method: FTP or Aspera preload)")
    names = [Path(path).name for path in files]
    if len(set(names)) != len(names):
        raise ValueError("data_files must have unique file names")
    for path in files:
        if not Path(path).is_file():
            raise ValueError(f"data_files: file does not exist: {path}")
        if Path(path).suffix.lower() == ".zip":
            raise ValueError(f"data_files: SRA does not accept zip archives; use gzip or bzip2: {path}")
        if Path(path).stat().st_size > WEB_UPLOAD_MAX_BYTES:
            raise ValueError(f"data_files: {Path(path).name} is over 2 GB, too large for the portal's web upload; upload by FTP or Aspera to a preload folder and set upload_method: FTP or Aspera preload")
    unlisted = sorted(set(names) - set(named))
    unuploaded = sorted(set(named) - set(names))
    if unlisted:
        raise ValueError(f"data_files: not named in sra_metadata_file: {', '.join(unlisted)}")
    if unuploaded:
        raise ValueError(f"sra_metadata_file names files missing from data_files: {', '.join(unuploaded)}")
    return files


# --- page helpers ----------------------------------------------------------


def _segment(url: str) -> str:
    """The wizard step in a portal URL: 'submitter', 'general', ..., 'overview'."""
    match = re.search(r"/subs/sra/SUB\d+/([a-z_]+)", url)
    return match.group(1) if match else ""


def _label(page, input_id: str):
    # The USWDS radios and checkboxes are drawn by their labels; the inputs sit off-screen.
    return page.locator(f'label[for="{input_id}"]')


def _set_checkbox(page, input_id: str, wanted: bool) -> None:
    if page.locator(f"#{input_id}").is_checked() != wanted:
        _label(page, input_id).click()


def _page_error(page) -> str:
    errors = page.locator(ERROR_MESSAGE)
    with unrecovered():
        texts = errors.all_inner_texts() if errors.count() else []
    return "\n".join(text.strip() for text in texts if text.strip())


def _remove_survey(raw) -> None:
    # Clicking "No Thanks" does not always close the overlay (it was seen staying
    # up and intercepting clicks), so drop Qualtrics' nodes outright.
    try:
        raw.get_by_role("button", name=SURVEY_DECLINE, exact=True).first.click(timeout=1000)
    except PWError:
        pass
    raw.evaluate(SURVEY_REMOVE_JS)


def _decline_survey(page) -> None:
    raw = page.unwrap() if hasattr(page, "unwrap") else page
    if hasattr(raw, "add_locator_handler"):
        raw.add_locator_handler(raw.locator(SURVEY_OVERLAY).first, lambda: _remove_survey(raw), no_wait_after=True)


def _continue(page, current: str, *, timeout_s: float = PAGE_ADVANCE_TIMEOUT_S) -> str:
    """Click Continue and wait for the next step; raise with the portal's message if it refuses."""
    # The POST behind Continue can take longer than an action timeout (uploads are
    # checked server-side); the loop below waits for the next step itself.
    page.locator(CONTINUE).click(no_wait_after=True)
    deadline = time.monotonic() + timeout_s
    next_reload = time.monotonic() + TASK_RELOAD_S
    while True:
        with unrecovered():
            try:
                page.wait_for_load_state()
            except PWError:
                pass  # The document can be replaced while we wait.
            url = page.url
            if "task_id=" in url and time.monotonic() >= next_reload:
                # The upload steps pass through a ?task_id= processing page that
                # normally moves on by itself; one was seen stuck there although the
                # server had finished, and a reload reaches the next step.
                next_reload = time.monotonic() + TASK_RELOAD_S
                try:
                    page.reload()
                except PWError:
                    pass
                url = page.url
            if "task_id=" not in url:
                try:
                    error = _page_error(page)
                except PWError:
                    error = ""  # mid-navigation; look again on the next pass
                    url = ""
                if error:
                    raise ValueError(f"The SRA portal rejected the {current} step:\n{error}")
                if url and _segment(url) != current:
                    try:
                        page.wait_for_load_state()  # fill the next step only once it has loaded
                    except PWError:
                        pass
                    return _segment(url)
        if time.monotonic() >= deadline:
            raise ValueError(f"The SRA portal stayed on the {current} step after Continue; check the browser for a message")
        page.wait_for_timeout(1000)


def _add_link(page, name: str):
    """A portal "Add ..." link. Its accessible name starts with a Font Awesome glyph
    (the ``<i class="fa fa-plus-circle">`` lacks aria-hidden), so match its ``<span>`` text."""
    return page.locator("a", has=page.get_by_text(name, exact=True))


def _fill_rows(page, spec: dict, rows: list[list[str]]) -> None:
    prefix, columns = spec["prefix"], spec["columns"]
    for index, row in enumerate(rows):
        first = page.locator(f'[name="{prefix}-{index}-{columns[0]}"]')
        if not first.count():
            _add_link(page, spec["add"]).click()
        for column, value in zip(columns, row):
            if value:
                page.locator(f'[name="{prefix}-{index}-{column}"]').fill(value)


def _stored(page, name: str, row: str):
    """The portal's entry for an uploaded ``name`` once it is stored (it has its "Created" timestamp)."""
    form = page.locator(CONTINUE).locator("xpath=ancestor::form[1]")
    return form.locator(row).filter(has=page.get_by_text(name, exact=True)).filter(has_text=UPLOADED_STAMP).first


def _upload(page, selector: str, paths: list[str], *, timeout_ms: int, row: str = UPLOADED_BLOCK) -> None:
    """Attach ``paths`` and wait until the portal lists each one as stored.

    A file's name shows as soon as it is chosen, while it is still uploading;
    only a finished upload gets its "Created" timestamp, and leaving the page
    before then drops the file. Files the page already lists as stored (a step
    the portal sent the run back to) are not uploaded again.
    """
    with unrecovered():
        pending = [path for path in paths if not _stored(page, Path(path).name, row).is_visible()]
    if not pending:
        return
    page.locator(selector).set_input_files(pending)
    for path in pending:
        _stored(page, Path(path).name, row).wait_for(state="visible", timeout=timeout_ms)
    if row == UPLOADED_BLOCK:
        # The single-file widgets then swap their file input for a hidden one that
        # carries the stored file; Continue before that posts no file.
        page.locator(f'{selector}[type="hidden"]:not([value=""])').wait_for(state="attached", timeout=timeout_ms)


# --- wizard steps ----------------------------------------------------------


def _fill_submitter(page, values: dict) -> None:
    for key, selector in SUBMITTER_INPUTS.items():
        value = str(values.get(key) or "").strip()
        if value:
            page.locator(selector).fill(value)
    country = str(values.get("country") or "").strip()
    if country and page.locator(COUNTRY_INPUT).input_value().strip() != country:
        page.locator(COUNTRY_INPUT).fill(country)
        page.get_by_role("menuitem", name=country).first.click()
    # The portal ticks "Update my contact information in profile" by default.
    _set_checkbox(page, UPDATE_PROFILE, _yes(values.get("update_profile")))


def _fill_general(page, values: dict) -> None:
    bioproject = str(values.get("bioproject") or "").strip()
    _label(page, BIOPROJECT_RADIO[bool(bioproject)]).click()
    if bioproject:
        page.locator(BIOPROJECT_INPUT).fill(bioproject)
    _label(page, BIOSAMPLE_RADIO[not str(values.get("biosample_package") or "").strip()]).click()
    date = _release_date(values)
    _label(page, RELEASE_RADIO["date" if date else "immediate"]).click()
    if date:
        page.locator(RELEASE_DATE_INPUT).fill(date)
        page.keyboard.press("Escape")  # close the datepicker


def _fill_project(page, values: dict) -> None:
    page.locator(PROJECT_TITLE).fill(values["title"].strip())
    page.locator(PROJECT_DESCRIPTION).fill(values["abstract"].strip())
    relevance = str(values.get("relevance") or "").strip()
    if relevance:
        page.locator(RELEVANCE).select_option(label=relevance)
        if values.get("relevance_description"):
            page.locator(RELEVANCE_DESCRIPTION).fill(values["relevance_description"].strip())
    _fill_rows(page, LINK_ROW, [[r["description"], r["url"]] for r in parse_pipe_file_list(values.get("external_links", ""), ["description", "url"])])
    grants = parse_pipe_file_list(values.get("grants", ""), ["grant_id", "title", "agency", "abbr"])
    if grants:
        _add_link(page, ADD_GRANTS).click()
        page.locator(ENTER_GRANTS_MANUALLY).click()
        _fill_rows(page, GRANT_ROW, [[g["grant_id"], g["title"], g["agency"], g["abbr"]] for g in grants])
    if values.get("consortium_name"):
        page.locator(CONSORTIUM_NAME).fill(values["consortium_name"].strip())
    if values.get("consortium_url"):
        page.locator(CONSORTIUM_URL).fill(values["consortium_url"].strip())
    _fill_rows(page, PROVIDER_ROW, [[p["name"], p["url"]] for p in parse_pipe_file_list(values.get("data_providers", ""), ["name", "url"])])


def _package_group(package: str, groups: list[str]) -> str:
    """The group radio (Pathogen, MIGS.ba, ...) that reveals ``package``, or '' for a top-level package."""
    matches = [group for group in groups if package.startswith(group + ".")]
    return max(matches, key=len) if matches else ""


def _choose_package(page, package: str) -> None:
    radio = page.locator(PACKAGE_RADIO.format(value=package))
    # The package list is built by the page's script after load.
    page.locator(PACKAGE_GROUPS).first.wait_for(state="attached")
    with unrecovered():
        try:
            radio.wait_for(state="attached", timeout=3000)
        except PWError:
            raise ValueError(f"biosample_package: the SRA portal offers no package {package!r}") from None
    if not radio.is_visible():
        groups = page.locator(PACKAGE_GROUPS).evaluate_all("els => els.map(e => e.value)")
        group = _package_group(package, groups)
        if group:
            group_id = page.locator(f'{PACKAGE_GROUPS}[value="{group}"]').get_attribute("id")
            _label(page, group_id).click()
    _label(page, radio.get_attribute("id")).click()


def _fill_files(page, values: dict, files: list[str], *, timeout_ms: int) -> None:
    if _upload_method(values) == "web":
        _label(page, UPLOAD_RADIO["web"]).click()
        page.attempt(f"upload {len(files)} sequence file(s)", lambda p: _upload(p, DATA_FILES_INPUT, files, timeout_ms=timeout_ms, row=UPLOADED_ROW))
    else:
        folder = values["preload_folder"].strip()
        _label(page, UPLOAD_RADIO["preload"]).click()
        page.locator(SELECT_PRELOAD_FOLDER).click()
        dialog = page.locator(PRELOAD_DIALOG)
        dialog.get_by_text(folder, exact=True).first.click()
        page.locator(PRELOAD_USE_FOLDER).click()
        dialog.wait_for(state="hidden")
    # Autofinish would submit by itself once the files pass their checks.
    _set_checkbox(page, AUTOFINISH, False)


class SRA(NCBIVenue):
    slug = "sra"
    signed_out_selector = "#account_login"
    header_selector = ".ncbi-header__account"
    supports_recovery = True
    final_submit_names = ("Submit",)

    def submit(
        self,
        values: dict,
        *,
        headless: bool = False,
        debug: bool = False,
        new_session: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        keep_open_on_failure: bool = True,
    ) -> None:
        """Start a new SRA submission and fill every wizard step up to Review & Submit."""
        files = _check_values(values)
        if debug:
            headless = False
        with sync_playwright() as playwright, hold_open_on_failure(headless=headless, keep_open=keep_open_on_failure):
            browser = playwright.chromium.launch(headless=headless, args=browser_launch_args())
            context = open_run_context(browser, self.session_path(), new_session=new_session)
            apply_default_timeouts(context, timeout)
            page = context.new_page()
            page.set_viewport_size({"width": 1400, "height": 1200})
            self.ensure_signed_in(page, context, debug=debug)
            page = guard(page)
            _decline_survey(page)
            if debug:
                page.pause()
            # An .xlsx attributes or metadata file is uploaded as TSV, written here.
            with tempfile.TemporaryDirectory(prefix="paperpush-sra-") as workdir:
                self._fill_wizard(page, values, files, timeout_ms=int(timeout * 1000), workdir=Path(workdir))
            hold_open()

    def _fill_wizard(self, page, values: dict, files: list[str], *, timeout_ms: int, workdir: Path) -> None:
        step("Start a new SRA submission")
        if not page.url.startswith(PORTAL_URL):
            page.goto(PORTAL_URL)
        page.locator(NEW_SUBMISSION).click()
        start = page.locator(START_ANYWAY)
        with unrecovered():
            try:
                start.wait_for(state="visible", timeout=3000)
                start.click()
            except PWError:
                pass  # No unfinished submissions, so no confirmation.
        page.wait_for_url(re.compile(r"/subs/sra/SUB\d+/submitter"))
        submission = re.search(r"SUB\d+", page.url).group(0)
        logger.info("Started SRA submission %s", submission)

        current = "submitter"
        visits: dict[str, int] = {}
        while current != "overview":
            # A step can come back once (the portal re-renders it when a value did
            # not take); fill it again, but stop rather than loop.
            visits[current] = visits.get(current, 0) + 1
            if visits[current] > 2:
                raise ValueError(f"The SRA portal keeps returning to the {current} step; check the browser for a message")
            logger.info("SRA wizard step: %s", current)
            if current == "submitter":
                step("Submitter")
                _fill_submitter(page, values)
            elif current == "general":
                step("General Info")
                _fill_general(page, values)
            elif current == "bp_info":
                step("Project Info")
                _fill_project(page, values)
            elif current == "sample_type":
                step("BioSample type")
                _choose_package(page, values["biosample_package"].strip())
            elif current == "attributes":
                step("BioSample attributes")
                _label(page, ATTRIBUTES_FROM_FILE).click()
                path = _as_tsv(values["biosample_attributes_file"], "", workdir)
                page.attempt("upload the BioSample attributes file", lambda p: _upload(p, ATTRIBUTES_FILE, [path], timeout_ms=timeout_ms))
            elif current == "metadata":
                step("SRA metadata")
                _label(page, METADATA_FROM_FILE).click()
                path = _as_tsv(values["sra_metadata_file"], METADATA_SHEET, workdir)
                page.attempt("upload the SRA metadata file", lambda p: _upload(p, METADATA_FILE, [path], timeout_ms=timeout_ms))
            elif current == "files":
                step("Files")
                _fill_files(page, values, files, timeout_ms=timeout_ms)
            else:
                raise ValueError(f"The SRA portal opened a step paperpush does not know ({current or page.url}); finish it in the browser")
            current = _continue(page, current, timeout_s=FILES_PROCESSING_TIMEOUT_S if current == "files" else PAGE_ADVANCE_TIMEOUT_S)

        step("Review & Submit")
        page.locator(FINAL_SUBMIT).wait_for(state="visible")
        error = _page_error(page)
        if error:
            logger.warning("The SRA Review page reports: %s", error)
        logger.info("SRA submission %s is filled in and waiting on Review & Submit. Check it in the browser and click Submit yourself.", submission)


VENUE = SRA()
