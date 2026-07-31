"""Nucleic Acids Research submission runner (Playwright).

NAR (Oxford University Press) submits through the same ScholarOne family as
Bioinformatics but at its own portal (``mc.manuscriptcentral.com/nar``) with a
different wizard. It reuses the shared ScholarOne engine (sign-in, author,
keyword, reviewer, and parse helpers) and supplies its own
:func:`submit_nucleic_acids_research` for the pages that differ, most notably the
declarations page. NAR keeps its own credentials and saved session under the
``nucleic_acids_research`` slug (its own portal).

Like the Bioinformatics runner this is close to the raw ``playwright codegen``
recording: declarations-page controls use ``CUSTOM_FIELD_RESPONSE_ID<id>`` names.
Two steps are best-effort (flagged below): the page-1 "Key Points" box and the
declarations-page "Add Funder" modal.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from ...database import get_venue
from ...manuscript import manuscript_text
from ...validate import parse_authors
from ..common import (DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts,
                      hold_open, hold_open_on_failure, open_run_context)
from ..common import session_path as _session_path
# Reuse the shared ScholarOne platform engine (author/keyword/reviewer/parse
# helpers). Sign-in is inherited from ScholarOneVenue.
from . import main as scholarone
from .main import (  # noqa: F401 -- re-exported as this module's public API
    ScholarOneLoginError, _add_authors, _add_keywords, _add_reviewers,
    _answer_sanctions, _split_figures, _split_keywords, _split_reviewers, _yes)

logger = logging.getLogger(__name__)

_SLUG = "nucleic_acids_research"
_VENUE = get_venue(_SLUG)

AUTHOR_LINK = scholarone.AUTHOR_LINK

# Labels offered by NAR's per-file "Designation" dropdown, read from venues.json;
# _upload_files asserts the labels it assigns are members of this set.
FILE_TYPE_OPTIONS = _VENUE.file_type_options or []

# The specific designation labels this runner assigns (each must be in
# FILE_TYPE_OPTIONS; the assertion catches drift at import time).
TYPE_MAIN_DOCUMENT = "Manuscript file - clean"
TYPE_FIGURE = "Figure"
TYPE_TABLE = "Table"
TYPE_SUPPLEMENTARY = "Supplementary data - for compiled PDF"
assert set((TYPE_MAIN_DOCUMENT, TYPE_FIGURE, TYPE_TABLE, TYPE_SUPPLEMENTARY)).issubset(FILE_TYPE_OPTIONS), "nucleic_acids_research file_type_options in venues.json is missing a designation label this runner uses"

# NAR requires at least 6 recommended (suggested) referees and allows at most 3
# opposed referees. ScholarOne labels the referee controls "Add Referee" /
# " Add New Referee " here rather than Bioinformatics' "Add Reviewer".
MIN_RECOMMENDED_REFEREES = 6
MAX_OPPOSED_REFEREES = 3
REFEREE_ADD_BUTTON = "Add Referee"
REFEREE_SUBMIT_BUTTON = " Add New Reviewer "

# Declarations-page custom-field ids (addressed by name as
# ``CUSTOM_FIELD_RESPONSE_ID<id>``; re-record if the portal regenerates them).
CF_RESUBMISSION = "7993"
CF_RESUBMISSION_ID = "27461"
CF_COI = "8001"
CF_COI_DETAILS = "27471"
CF_AUTHOR_LIST_ALTERED = "813005"  # <select>: Yes / No / Not Applicable
CF_AUTHOR_LIST_DETAILS = "1287973"
CF_PUBLICATION_CHARGE_FUNDING = "474374"
CF_BACK_TO_BACK = "61754"
CF_BACK_TO_BACK_AUTHORS = "720438"
CF_CATEGORY = "61750"  # <select>: the NAR manuscript categories
CF_NOVEL_METHOD = "61751"
CF_NOVEL_METHOD_DESC = "564150"
CF_SUPPLEMENTARY_ONLINE = "61758"
CF_DATA_AVAILABILITY = "1521496"

# Author-confirmation checkboxes (all required consents): checked unconditionally.
CF_CONFIRM_CHECKBOXES = (
    "61761",  # all authors in complete agreement with the contents
    "61762",  # all authors prepared to abide by the policies of this venue
    "693534",  # study conforms to all OUP Ethical Requirements
    "61763",  # manuscript not currently being submitted elsewhere
    "133964",  # permissions obtained for cited personal communications
    "251424",  # corresponding-author email-retention consent
    "959391",  # raw data saved, kept available, and available on request
    "1307206",  # all intellectual-property matters fully resolved
)

# Data-policy and open-access consent checkboxes (required): checked unconditionally.
CF_DATA_POLICY_CHECKBOXES = (
    "1055244",  # read the Venue Research Data Policy
    "1055245",  # read the data-reporting requirements; statement included
)
CF_OPEN_ACCESS_CHECKBOX = "1068912"  # read the open access charges and waiver policy

# The page-6 "data deposition" matrix: one Yes/No group per data type. A label
# selected in the .sub ``data_deposition_types`` answers its row "Yes", rest "No".
# Labels must match the venues.json ``data_deposition_types`` options exactly.
DATA_DEPOSITION_ROWS = (
    ("New genome expression or sequencing data (ChIP-seq, RNA-seq)", "1286390"),
    ("New genome-wide binding/interaction data or genome-wide mutation data", "1286391"),
    ("Novel nucleic acid sequences", "949365"),
    ("Illumina-type genome sequencing data", "949366"),
    ("Novel protein sequences", "949367"),
    ("Novel molecular structures (X-ray, NMR, CryoEM/EM)", "949368"),
    ("Novel molecular models (SAXS, computational modeling)", "949369"),
    ("Molecular behaviour studies (biological NMR spectroscopy data)", "949407"),
    ("Novel nucleic acids structure", "949408"),
    ("Structures of nucleosides, nucleotides, other small molecules", "949409"),
    ("Mass spectrometry proteomics", "949410"),
    ("Microarray data", "949411"),
    ("Quantitative PCR", "949412"),
    ("Synthetic nucleic acid oligonucleotides including siRNAs or shRNAs", "949413"),
    ("Software and source codes", "1286400"),
    ("Gel images, micrographs, graphs, and tables", "949416"),
)

# Map the .sub author-list-change choice and institution-verification choice onto
# the page controls.
_AUTHOR_LIST_ALTERED_OPTIONS = {"Yes", "No", "Not Applicable"}
_INSTITUTION_VERIFICATION_LABELS = {
    "verified": "radiobutton  I confirm that I have verified my institution",
    "not in list": "radiobutton  I have an institution, but it is not available in the list",
    "N/A entered": "radiobutton  I don't have an institution, and have entered 'N/A'",
}

# Cover-letter values that mean "no conflict" and so answer the COI question "No".
_NONE_DECLARED = {"", "none", "none declared", "none declared.", "n/a", "na", "no conflict of interest", "the authors declare no conflict of interest.", "no conflicts of interest", "no competing interests"}


def session_path():
    """Path to the saved Nucleic Acids Research session (Playwright storage_state)."""
    return _session_path(_SLUG)


def _check_yes_no(page, field_id: str, yes: bool) -> None:
    """Check the Yes/No radio of a custom-field group (Yes first, No second)."""
    radios = page.locator(f'input[name="CUSTOM_FIELD_RESPONSE_ID{field_id}"]')
    radios.nth(0 if yes else 1).check()


def _fill_custom_textarea(page, field_id: str, value: str) -> None:
    """Fill a ScholarOne custom-field textarea/text input addressed by its id."""
    box = page.locator(f'textarea[name="CUSTOM_FIELD_RESPONSE_ID{field_id}"], input[name="CUSTOM_FIELD_RESPONSE_ID{field_id}"]').first
    box.click()
    box.fill(value)


def _check_custom_checkbox(page, field_id: str) -> None:
    """Check a single ScholarOne custom-field checkbox addressed by its id."""
    page.locator(f'input[name="CUSTOM_FIELD_RESPONSE_ID{field_id}"]').first.check()


def _cover_letter_text(values: dict) -> str:
    """Return the cover-letter body to paste into the AUTHOR_COMMENTS box.

    NAR pastes the cover letter as text, so the ``.sub`` ``cover_letter`` file is
    read via :func:`manuscript_text` (falling back to a plain read). Returns "" when
    no path is given or its text cannot be extracted.
    """
    raw = values.get("cover_letter", "").strip()
    if not raw:
        return ""
    path = Path(raw)
    text = manuscript_text(path)
    if text:
        return text.strip()
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        logger.warning("Could not read the cover-letter file %s (%s); leaving the box empty", path, exc)
        return ""


def _parse_funders(value: str) -> list[tuple[str, str]]:
    """Parse the ``funding`` block into ``(funder, grant_id)`` tuples.

    Each non-empty line is ``funder | grant id`` (the grant id optional).
    """
    out: list[tuple[str, str]] = []
    for line in (value or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        funder = parts[0]
        grant_id = parts[1] if len(parts) > 1 else ""
        if funder:
            out.append((funder, grant_id))
    return out


def _upload_files(page, files: list[tuple[str, str]]) -> None:
    """Attach each ``(path, designation)`` on the NAR upload step and set its type.

    Chooser triggers are numbered "Select File N", each row with a "Designation"
    dropdown. Every ``designation`` must be in FILE_TYPE_OPTIONS (checked first).
    """
    for _, designation in files:
        if designation not in FILE_TYPE_OPTIONS:
            raise ValueError(f"Unknown NAR designation {designation!r}; expected one of {FILE_TYPE_OPTIONS}")

    for index, (path, designation) in enumerate(files):
        cell = page.get_by_role("cell", name=re.compile(rf"FILE_TO_UPLOAD.*Select File {index + 1}"))
        cell.get_by_label("FILE_TO_UPLOAD").set_input_files(path)
        page.locator(f'select[file-index="{index}"]').select_option(label=designation)


def _select_institution_verification(page, choice: str) -> None:
    """Check the institution-verification radio matching the ``.sub`` choice.

    One of three radios; ``institution_verification`` selects which (defaults to
    "verified" for an unknown value).
    """
    name = _INSTITUTION_VERIFICATION_LABELS.get(choice.strip().lower(), _INSTITUTION_VERIFICATION_LABELS["verified"])
    page.get_by_role("cell", name=name, exact=True).get_by_label("radiobutton").check()


def _answer_declarations(page, values: dict) -> None:
    """Drive the NAR final declarations page from the ``.sub`` values.

    Controls are addressed by their ScholarOne ``CUSTOM_FIELD_RESPONSE_ID<id>``
    names (see the CF_* constants). Yes/No radio groups are 0 = Yes, 1 = No. The
    consent checkboxes are checked unconditionally; the data-bearing answers come
    from the ``.sub``.
    """
    # Cover letter (pasted as text).
    cover_letter = _cover_letter_text(values)
    if cover_letter:
        box = page.get_by_role("textbox", name="AUTHOR_COMMENTS")
        box.click()
        box.fill(cover_letter)

    # Funding: toggle the report radio, then add each funder via the modal.
    funders = _parse_funders(values.get("funding", ""))
    _add_funders(page, funders)

    # Source of funding for publication charges (required free text).
    charge_funding = values.get("publication_charge_funding", "").strip()
    if charge_funding:
        _fill_custom_textarea(page, CF_PUBLICATION_CHARGE_FUNDING, charge_funding)

    # Resubmission of previous work.
    is_resubmission = _yes(values.get("is_resubmission", "no"))
    _check_yes_no(page, CF_RESUBMISSION, is_resubmission)
    resubmission_id = values.get("resubmission_id", "").strip()
    if is_resubmission and resubmission_id:
        _fill_custom_textarea(page, CF_RESUBMISSION_ID, resubmission_id)

    # Conflict of interest: any non-"none" statement answers Yes and is provided.
    coi = values.get("conflict_of_interest", "").strip()
    has_coi = coi.lower() not in _NONE_DECLARED
    _check_yes_no(page, CF_COI, has_coi)
    if has_coi:
        _fill_custom_textarea(page, CF_COI_DETAILS, coi)

    # Author-list change (resubmission/revision): a Yes / No / Not Applicable select.
    altered = values.get("author_list_altered", "Not Applicable").strip() or "Not Applicable"
    page.locator(f'select[name="CUSTOM_FIELD_RESPONSE_ID{CF_AUTHOR_LIST_ALTERED}"]').select_option(label=altered)
    altered_details = values.get("author_list_change_details", "").strip()
    if altered == "Yes" and altered_details:
        _fill_custom_textarea(page, CF_AUTHOR_LIST_DETAILS, altered_details)

    # Back-to-back publication.
    back_to_back = _yes(values.get("back_to_back", "no"))
    _check_yes_no(page, CF_BACK_TO_BACK, back_to_back)
    btb_authors = values.get("back_to_back_authors", "").strip()
    if back_to_back and btb_authors:
        _fill_custom_textarea(page, CF_BACK_TO_BACK_AUTHORS, btb_authors)

    # Manuscript category (a <select> chosen by label).
    category = values.get("category", "").strip()
    if category:
        page.locator(f'select[name="CUSTOM_FIELD_RESPONSE_ID{CF_CATEGORY}"]').select_option(label=category)

    # Novel method.
    novel_method = _yes(values.get("novel_method", "no"))
    _check_yes_no(page, CF_NOVEL_METHOD, novel_method)
    novel_desc = values.get("novel_method_description", "").strip()
    if novel_method and novel_desc:
        _fill_custom_textarea(page, CF_NOVEL_METHOD_DESC, novel_desc)

    # Supplementary material for online-only publication.
    _check_yes_no(page, CF_SUPPLEMENTARY_ONLINE, _yes(values.get("supplementary_online", "no")))

    # Author confirmations (required consents).
    for field_id in CF_CONFIRM_CHECKBOXES:
        _check_custom_checkbox(page, field_id)

    # Data-deposition matrix: Yes for each selected type, No for the rest.
    selected = {item.strip().lower() for item in (values.get("data_deposition_types", "") or "").split(",") if item.strip()}
    for label, field_id in DATA_DEPOSITION_ROWS:
        _check_yes_no(page, field_id, label.lower() in selected)

    # Data availability statement (required).
    data_availability = values.get("data_availability", "").strip()
    if data_availability:
        _fill_custom_textarea(page, CF_DATA_AVAILABILITY, data_availability)

    # Data-policy and open-access consents (required).
    for field_id in CF_DATA_POLICY_CHECKBOXES:
        _check_custom_checkbox(page, field_id)
    _check_custom_checkbox(page, CF_OPEN_ACCESS_CHECKBOX)


def _add_funders(page, funders: list[tuple[str, str]]) -> None:
    """Answer the funding-report radio and add each funder via the Add Funder modal.

    Best-effort (the recording did not capture the modal): the funder is searched
    in ``SUB_FUNDER_ID_SELECT`` (first match) and the grant id typed into
    ``FUNDREF_GRANT_NO_0``. A funder that can't be added is logged and skipped.
    """
    report_radios = page.locator('input[name="FUND_REPORT_RADIO"]')
    if not funders:
        # No funding to report: the first radio is "yes" and the second "no".
        report_radios.nth(1).check()
        return
    report_radios.nth(0).check()
    for funder, grant_id in funders:
        try:
            page.get_by_role("button", name="Add Funder").click()
            page.wait_for_timeout(1000)
            select = page.locator("#SUB_FUNDER_ID_SELECT")
            select.click()
            select.fill(funder)
            page.wait_for_timeout(1000)
            page.locator("li.ui-menu-item, li.x-boundlist-item").first.click()
            if grant_id:
                grant_box = page.locator("#FUNDREF_GRANT_NO_0")
                grant_box.click()
                grant_box.fill(grant_id)
            page.get_by_role("button", name="Save").click()
            page.wait_for_timeout(1000)
        except Exception as exc:  # noqa: BLE001 -- best-effort optional modal step
            logger.warning("nucleic_acids_research: could not add funder %r via the modal (%s); " "leaving it to be finished by hand", funder, exc)


def submit_nucleic_acids_research(values: dict, headless: bool = False, debug: bool = False, new_session: bool = False, timeout: float = DEFAULT_TIMEOUT_SECONDS, *, venue) -> None:
    """Open Nucleic Acids Research (ScholarOne) and drive the submission wizard.

    ``values`` is the parsed ``.sub`` field map. The wizard runs across six pages:
    (1) article type, title, abstract, Key Points; (2) file upload; (3) keywords;
    (4) authors + institution verification; (5) referees; (6) declarations.

    Sign-in is handled by the inherited ``venue.ensure_signed_in``;
    ``new_session=True`` discards any saved session first and ``debug=True`` opens
    the Inspector. The run stops on the declarations page and leaves the browser
    open via :func:`hold_open` (it never clicks the final submit).
    """
    # The Inspector needs a visible browser; debugging headless makes no sense.
    if debug:
        headless = False

    manuscript_file = values["manuscript_file"].strip()

    # Upload files in attach order: manuscript, figures, tables, then optional
    # supplementary. Each pairs a path with its Designation label.
    upload_files: list[tuple[str, str]] = [(manuscript_file, TYPE_MAIN_DOCUMENT)]
    upload_files += [(figure, TYPE_FIGURE) for figure in _split_figures(values.get("figures", ""))]
    upload_files += [(table, TYPE_TABLE) for table in _split_figures(values.get("tables", ""))]
    supplementary_file = values.get("supplementary_file", "").strip()
    if supplementary_file:
        upload_files.append((supplementary_file, TYPE_SUPPLEMENTARY))

    # Authors, parsed with the inherited Bioinformatics column order declared in
    # venues.json (email | prefix | name | institution | country | city | corresponding).
    author_field = next((f for f in _VENUE.fields if f.type == "authorlist"), None)
    authors = parse_authors(values.get("authors", ""), author_field.fields if author_field else None)

    logger.info("Starting Nucleic Acids Research (ScholarOne) submission run " "(headless=%s, debug=%s, manuscript=%s, %d file(s) to upload, %d author(s))", headless, debug, manuscript_file, len(upload_files), len(authors))

    with sync_playwright() as playwright, hold_open_on_failure(headless=headless):
        browser = playwright.chromium.launch(headless=headless)
        context = open_run_context(browser, venue.session_path(), new_session=new_session)
        apply_default_timeouts(context, timeout)
        page = context.new_page()

        venue.ensure_signed_in(page, context, debug=debug)

        if debug:
            page.pause()

        logger.debug("Signed in; starting a new submission")
        page.wait_for_timeout(3000)
        try:
            page.get_by_role("button", name="Reject All").click(timeout=2000)
        except Exception as exc:  # noqa: BLE001 -- best-effort; button may not be present
            pass
        page.get_by_role("link", name=AUTHOR_LINK).click()
        page.get_by_role("link", name=" Start New Submission").click()
        page.get_by_role("link", name="Begin Submission").click()
        page.get_by_role("link", name="or continue without pre-").click()

        # Page 1: article type, title, abstract, key points. NAR has no subject
        # category, NGS, or machine-learning questions here.
        article_type = values.get("article_type", "Standard Article").strip()
        page.get_by_role("radio", name=f"Choice {article_type}").check()
        page.get_by_role("textbox", name="DOCUMENT_TITLE").click()
        page.get_by_role("textbox", name="DOCUMENT_TITLE").fill(values["title"].strip())
        page.get_by_role("textbox", name="DOCUMENT_ABSTRACT").click()
        page.get_by_role("textbox", name="DOCUMENT_ABSTRACT").fill(values["abstract"].strip())
        # Key Points box: best-effort selector (the recording did not capture it).
        # NAR asks for 3 bullet points (<=100 chars each) after the abstract; they
        # are stored one per line and pasted as a block.
        key_points = values.get("key_points", "").strip()
        if key_points:
            try:
                page.locator('textarea[name="CUSTOM_FIELD_RESPONSE_ID1181588"]').fill(key_points)
            except Exception as exc:  # noqa: BLE001 -- best-effort; selector may need re-recording
                logger.warning("nucleic_acids_research: could not fill the Key Points box (%s); " "fill it by hand in the open browser", exc)
        page.get_by_role("button", name="Save & Continue ").click()

        # Page 2: upload files with their Designation.
        _upload_files(page, upload_files)
        page.get_by_role("link", name="Upload Selected Files").click()
        page.get_by_role("button", name="Save & Continue ").click()

        # Page 3: free keywords (required, 3-5); NAR has no portal-keyword list.
        _add_keywords(page, _split_keywords(values.get("keywords", "")), portal=False, keywords_box_name="Key Words")
        page.get_by_role("button", name="Save & Continue ").click()

        # Page 4: sanctions (no-op; NAR carries no sanctions fields), authors, the
        # institution-verification radio, then the page checkbox.
        _answer_sanctions(page, values)
        _add_authors(page, authors, venue_label=_SLUG)
        _select_institution_verification(page, values.get("institution_verification", "verified"))
        page.get_by_role("checkbox", name="checkbox").check()
        page.get_by_role("button", name="Save & Continue ").click()

        # Page 5: referees (>=6 recommended, <=3 opposed, reason required).
        _add_reviewers(
            page,
            _split_reviewers(values.get("reviewers", "")),
            min_recommended=MIN_RECOMMENDED_REFEREES,
            max_opposed=MAX_OPPOSED_REFEREES,
            add_button=REFEREE_ADD_BUTTON,
            submit_button=REFEREE_SUBMIT_BUTTON,
            venue_label=_SLUG,
        )
        page.get_by_role("checkbox", name="checkbox").check()
        page.get_by_role("button", name="Save & Continue ").click()

        # Page 6: declarations (entirely NAR-specific).
        _answer_declarations(page, values)
        page.get_by_role("button", name="Save & Continue ").click()

        logger.info("Completed the recorded Nucleic Acids Research steps; " "leaving the browser open for review (the final submit is left to you)")
        hold_open()


def _validate_reviewers(value: str) -> str:
    """Custom validator: enforce NAR's referee stance counts at validate time.

    Requires >= :data:`MIN_RECOMMENDED_REFEREES` recommended and
    <= :data:`MAX_OPPOSED_REFEREES` opposed referees (opposed do not count toward
    the minimum), plus a reason per line. Returns the value or raises.
    """
    reviewers = _split_reviewers(value)
    recommended = sum(1 for r in reviewers if r["stance"] == "recommended")
    opposed = sum(1 for r in reviewers if r["stance"] == "opposed")
    if recommended < MIN_RECOMMENDED_REFEREES:
        raise ValueError(f"at least {MIN_RECOMMENDED_REFEREES} recommended referees are required " f"(opposed referees do not count); found {recommended}")
    if opposed > MAX_OPPOSED_REFEREES:
        raise ValueError(f"at most {MAX_OPPOSED_REFEREES} opposed referees are allowed; found {opposed}")
    if any(not r.get("reason", "").strip() for r in reviewers):
        raise ValueError("every referee requires a reason (the fourth | column)")
    return value


def _validate_key_points(value: str) -> str:
    """Custom validator: each Key Point must be at most 100 characters."""
    for line in (value or "").splitlines():
        line = line.strip()
        if line and len(line) > 100:
            raise ValueError(f"each Key Point must be at most 100 characters; this one is {len(line)}: {line[:60]!r}...")
    return value


FIELD_VALIDATORS = {
    "reviewers": _validate_reviewers,
    "key_points": _validate_key_points,
}


class NucleicAcidsResearchVenue(scholarone.ScholarOneVenue):
    """The Nucleic Acids Research venue: a ScholarOne deployment with NAR validators."""

    slug = "nucleic_acids_research"
    field_validators = FIELD_VALIDATORS

    def submit(
        self,
        values: dict,
        *,
        headless: bool = False,
        debug: bool = False,
        new_session: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        submit_nucleic_acids_research(
            values,
            headless=headless,
            debug=debug,
            new_session=new_session,
            timeout=timeout,
            venue=self,
        )


VENUE = NucleicAcidsResearchVenue()
