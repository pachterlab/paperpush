"""CaltechDATA's InvenioRDM deposit form and access.caltech sign-in."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from ...validate import parse_authors
from ..base import Venue
from ..common import DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts, hold_open, hold_open_on_failure, open_run_context
from ..login import VenueLoginError, first_present
from ..recovery import guard, step
from ..recovery_channel import browser_launch_args

LOGIN_URL = "https://data.caltech.edu/login/"
UPLOAD_URL = "https://data.caltech.edu/uploads/new"
SSO_LINK = "Sign in with access.caltech"
SIGNED_IN_SELECTOR = '#invenio-menu a[href="/logout/"], #invenio-menu a[href="/logout"]'
MODAL = ".ui.modal.visible.active"
FILE_INPUT = 'input[type="file"]:not([webkitdirectory])'
EDITOR_FRAME = 'iframe[title="Rich Text Area"]'
COOKIE_CLOSE = "#cit-cookie-banner button"
FUNDER_OPTION = '[role="option"][name={name}], [role="option"][pid={identifier}]'
logger = logging.getLogger(__name__)


def _rows(value: str) -> list[list[str]]:
    return [[part.strip() for part in line.split("|")] for line in value.splitlines() if line.strip()]


def _yes(value: str) -> bool:
    return value.strip().lower() in {"yes", "true", "1"}


def _input(root, path: str):
    return root.locator(f'[name="{path}"]:not([role="combobox"])')


def _combo(root, path: str):
    return root.locator(f'[role="combobox"][name="{path}"]')


def _choose(combo, value: str) -> None:
    """Choose a named vocabulary value, waiting for remote search results."""
    combo.click()
    combo.locator("input").fill(value)
    combo.get_by_role("option", name=value, exact=True).click()


def _tag(combo, value: str) -> None:
    # Free-text tags use Enter to commit the typed value; this also works for
    # a selected affiliation suggestion without inventing an identifier.
    combo.locator("input").fill(value)
    combo.locator("input").press("Enter")


def _person_name(name: str) -> tuple[str, str]:
    if "," in name:
        family, given = name.split(",", 1)
        return family.strip(), given.strip()
    parts = name.rsplit(None, 1)
    return (parts[-1], parts[0] if len(parts) > 1 else "")


def _add_people(page, value: str, *, contributors: bool = False) -> None:
    columns = ["name", "role", "affiliation?", "orcid?", "type?"] if contributors else ["name", "affiliation?", "orcid?", "type?", "role?"]
    for person in parse_authors(value, columns):
        page.get_by_role("button", name="Add contributor" if contributors else "Add creator", exact=True).click()
        modal = page.locator(MODAL)
        if person.get("type", "").lower() == "organization":
            modal.locator('input[type="radio"]').nth(1).check(force=True)
            _input(modal, "person_or_org.name").fill(person["name"])
        else:
            family, given = _person_name(person["name"])
            _input(modal, "person_or_org.family_name").fill(family)
            _input(modal, "person_or_org.given_name").fill(given)
            if person.get("orcid"):
                _tag(_combo(modal, "person_or_org.identifiers"), person["orcid"])
        for affiliation in person.get("affiliation", "").split(";"):
            if affiliation.strip():
                _tag(_combo(modal, "affiliations"), affiliation.strip())
        if person.get("role"):
            _choose(_combo(modal, "role"), person["role"])
        modal.get_by_role("button", name="Save", exact=True).click()
        modal.wait_for(state="hidden")


def _fill_titles(page, value: str) -> None:
    for index, row in enumerate(_rows(value)):
        title, kind, language = (row + ["", ""])[:3]
        page.get_by_role("button", name="Add titles", exact=True).click()
        prefix = f"metadata.additional_titles.{index}"
        _input(page, f"{prefix}.title").fill(title)
        _choose(_combo(page, f"{prefix}.type"), kind or "Alternative title")
        if language:
            _choose(_combo(page, f"{prefix}.lang"), language)


def _fill_descriptions(page, value: str) -> None:
    for index, row in enumerate(_rows(value)):
        text, kind, language = (row + ["", ""])[:3]
        page.get_by_role("button", name="Add description", exact=True).click()
        page.locator(EDITOR_FRAME).nth(index + 1).content_frame.locator("body").fill(text)
        prefix = f"metadata.additional_descriptions.{index}"
        _choose(_combo(page, f"{prefix}.type"), kind)
        if language:
            _choose(_combo(page, f"{prefix}.lang"), language)


def _fill_funding(page, value: str) -> None:
    for row in _rows(value):
        funder, number, title, url = (row + ["", "", ""])[:4]
        page.locator("#funding-section").get_by_role("button", name="Add custom", exact=True).click()
        modal = page.locator(MODAL)
        combo = modal.get_by_role("combobox").first
        combo.click()
        combo.locator("input").fill(funder)
        # Accessible labels also contain country and ROR id. Match the actual
        # name/id attributes, never a prefix shared by unrelated organizations.
        identifier = funder.removeprefix("https://ror.org/")
        combo.locator(FUNDER_OPTION.format(name=json.dumps(funder), identifier=json.dumps(identifier))).click()
        for label, text in (("Number", number), ("Title", title), ("URL", url)):
            if text:
                modal.get_by_role("textbox", name=label, exact=True).fill(text)
        modal.get_by_role("button", name="Add", exact=True).click()
        modal.wait_for(state="hidden")


def _scheme(identifier: str, explicit: str) -> str:
    if explicit:
        return explicit
    if re.match(r"^(?:https?://(?:dx\.)?doi\.org/)?10\.\d{4,9}/", identifier, re.I):
        return "DOI"
    if identifier.startswith(("https://", "http://")):
        return "URL"
    raise ValueError("related_identifiers: specify the identifier scheme for " + identifier)


def _fill_identifiers(page, value: str, *, related: bool = False) -> None:
    for index, row in enumerate(_rows(value)):
        if index:
            page.get_by_role("button", name="Add related work" if related else "Add identifier", exact=True).click()
        prefix = f"metadata.{'related_identifiers' if related else 'identifiers'}.{index}"
        identifier = row[0]
        _input(page, f"{prefix}.identifier").fill(identifier)
        if related:
            _, relation, scheme, resource = (row + ["", "", ""])[:4]
            _choose(_combo(page, f"{prefix}.relation_type"), relation)
            _choose(_combo(page, f"{prefix}.scheme"), _scheme(identifier, scheme))
            if resource:
                _choose(_combo(page, f"{prefix}.resource_type"), resource)
        else:
            _choose(_combo(page, f"{prefix}.scheme"), row[1])


def _fill_license(page, license_name: str) -> None:
    if not license_name:
        return
    section = page.locator("#basic-information-section")
    existing = section.get_by_role("listitem").filter(has_text=license_name)
    if existing.count():
        return
    # Replace the default, rather than adding a second, conflicting license.
    section.get_by_role("listitem").filter(has_text="Creative Commons Zero v1.0 Universal").get_by_role("button", name="Edit", exact=True).click()
    modal = page.locator(MODAL)
    modal.get_by_role("textbox", name="Search for licenses", exact=True).fill(license_name)
    modal.get_by_text(license_name, exact=True).click()
    modal.get_by_role("button", name="Change license", exact=True).click()
    modal.wait_for(state="hidden")


def _fill_visibility(page, values: dict) -> None:
    section = page.locator("#visibility-section")
    for key, index in (("record_visibility", 0), ("files_visibility", 1)):
        if values.get(key):
            section.get_by_role("button", name=values[key], exact=True).nth(index).click()
    if values.get("embargo_date"):
        page.locator('label[for="access.embargo.active"]').last.click()
        _input(page, "access.embargo.until").fill(values["embargo_date"])
        if values.get("embargo_reason"):
            page.get_by_role("textbox", name="Embargo reason", exact=True).fill(values["embargo_reason"])


def _check_values(values: dict) -> list[str]:
    files = [str(Path(row[0]).expanduser().resolve()) for row in _rows(values.get("data_files", ""))]
    if _yes(values.get("metadata_only", "")) and files:
        raise ValueError("metadata_only cannot be combined with data_files")
    if not files and not _yes(values.get("metadata_only", "")):
        raise ValueError("data_files is empty; explicitly set metadata_only: yes for a record without files")
    if len(files) > 50:
        raise ValueError("The current CaltechDATA web uploader accepts at most 50 files; zip the collection first")
    if len({Path(path).name for path in files}) != len(files):
        raise ValueError("data_files must have unique filenames")
    for path in files:
        if not Path(path).is_file():
            raise ValueError(f"data_files: file does not exist: {path}")
    if values.get("existing_doi") and _yes(values.get("reserve_doi", "")):
        raise ValueError("existing_doi cannot be combined with reserve_doi")
    if values.get("embargo_date") and "Restricted" not in (values.get("record_visibility"), values.get("files_visibility")):
        raise ValueError("embargo_date requires explicitly Restricted record_visibility or files_visibility")
    if values.get("embargo_reason") and not values.get("embargo_date"):
        raise ValueError("embargo_reason requires embargo_date")
    for row in _rows(values.get("related_identifiers", "")):
        _scheme(row[0], row[2] if len(row) > 2 else "")
    return files


class CaltechDataVenue(Venue):
    slug = "caltechdata"
    logged_in_names = ("Log out",)
    supports_recovery = True
    final_submit_names = ("Publish", "Publish record", "Submit for review")

    @property
    def login_url(self) -> str:
        return LOGIN_URL

    def is_logged_in(self, page, *, timeout_ms: int | None = None) -> bool:
        # My dashboard is also present for guests. The logout item lives in a
        # collapsed account menu, so test attachment rather than visibility.
        try:
            page.locator(SIGNED_IN_SELECTOR).first.wait_for(state="attached", timeout=timeout_ms if timeout_ms is not None else self.logged_in_timeout_ms)
            return True
        except PWTimeout:
            return False

    def login(self, page, username: str, password: str, *, timeout_ms: int = 15000) -> None:
        page.goto(LOGIN_URL)
        try:
            page.get_by_role("link", name=SSO_LINK, exact=True).click(timeout=timeout_ms)
            page.get_by_role("textbox", name="Username", exact=True).fill(username, timeout=timeout_ms)
            page.get_by_label("Password", exact=True).fill(password)
            page.get_by_label("Password", exact=True).press("Enter")
            # Some access.caltech sessions show another Login control; Duo and
            # device trust are deliberately left to the author in the browser.
            control = first_present([page.get_by_role("button", name="Login", exact=True)], 2000)
            if control is not None:
                control.click()
            if not self.is_logged_in(page, timeout_ms=timeout_ms):
                raise VenueLoginError("Complete access.caltech's two-factor and device-trust prompts in the browser, then return to the terminal. The CaltechDATA dashboard has not loaded yet.")
        except PWTimeout as exc:
            raise VenueLoginError("The access.caltech sign-in did not complete. Check your credentials and finish any two-factor prompt in the browser.") from exc

    def submit(self, values: dict, *, headless: bool = False, debug: bool = False, new_session: bool = False, timeout: float = DEFAULT_TIMEOUT_SECONDS, keep_open_on_failure: bool = True) -> None:
        _check_values(values)
        if debug:
            headless = False
        with sync_playwright() as playwright, hold_open_on_failure(headless=headless, keep_open=keep_open_on_failure):
            browser = playwright.chromium.launch(headless=headless, args=browser_launch_args())
            context = open_run_context(browser, self.session_path(), new_session=new_session)
            # The sticky visibility sidebar extends below smaller viewports.
            # Keep its embargo controls reachable without forced clicks.
            apply_default_timeouts(context, timeout)
            page = context.new_page()
            page.set_viewport_size({"width": 1500, "height": 1500})
            self.ensure_signed_in(page, context, debug=debug)
            page = guard(page)
            if debug:
                page.pause()
            step("New CaltechDATA upload")
            page.goto(UPLOAD_URL)
            self._fill_deposit(page, values)
            hold_open()

    def _fill_deposit(self, page, values: dict) -> None:
        files = _check_values(values)
        _input(page, "metadata.title").wait_for(state="visible")
        cookie = page.locator(COOKIE_CLOSE)
        if cookie.is_visible():
            cookie.click()
        if values.get("community"):
            page.get_by_role("button", name="Select a community", exact=True).click()
            modal = page.get_by_role("dialog", name="Select a community", exact=True)
            modal.get_by_role("textbox", name="Search in all communities", exact=True).fill(values["community"])
            modal.get_by_role("button", name="Search", exact=True).click()
            modal.get_by_role("button", name="Select " + values["community"], exact=True).click()
            modal.wait_for(state="hidden")

        step("Basic information")
        _input(page, "metadata.title").fill(values.get("title", ""))
        if values.get("existing_doi"):
            page.get_by_role("radio", name="Yes, I already have one", exact=True).check(force=True)
            page.get_by_role("textbox", name="Copy/paste your existing DOI here...", exact=True).fill(values["existing_doi"])
        _choose(_combo(page, "metadata.resource_type"), values.get("resource_type") or "Dataset")
        _fill_titles(page, values.get("additional_titles", ""))
        if values.get("publication_date"):
            _input(page, "metadata.publication_date").fill(values["publication_date"])
        _add_people(page, values.get("authors", ""))
        if values.get("abstract"):
            page.locator(EDITOR_FRAME).first.content_frame.locator("body").fill(values["abstract"])
        _fill_descriptions(page, values.get("additional_descriptions", ""))
        _fill_license(page, values.get("license", ""))
        if values.get("copyright"):
            _input(page, "metadata.copyright").fill(values["copyright"])

        step("Recommended information")
        _add_people(page, values.get("contributors", ""), contributors=True)
        for keyword in values.get("keywords", "").split(","):
            if keyword.strip():
                _tag(_combo(page, "metadata.subjects"), keyword.strip())
        for language in values.get("languages", "").split(","):
            if language.strip():
                _choose(_combo(page, "metadata.languages"), language.strip())
        for index, row in enumerate(_rows(values.get("dates", ""))):
            if index:
                page.get_by_role("button", name="Add date", exact=True).click()
            date, kind, description = (row + [""])[:3]
            prefix = f"metadata.dates.{index}"
            _input(page, f"{prefix}.date").fill(date)
            _choose(_combo(page, f"{prefix}.type"), kind)
            if description:
                _input(page, f"{prefix}.description").fill(description)
        for key in ("version", "publisher"):
            if values.get(key):
                _input(page, "metadata." + key).fill(values[key])
        _fill_funding(page, values.get("funding", ""))
        _fill_identifiers(page, values.get("alternate_identifiers", ""))
        _fill_identifiers(page, values.get("related_identifiers", ""), related=True)
        for group in values.get("caltech_groups", "").split(","):
            if group.strip():
                _choose(page.get_by_role("combobox", name="Caltech groups", exact=True), group.strip())

        step("Visibility and embargo")
        _fill_visibility(page, values)
        step("Files")
        if files:
            page.locator(FILE_INPUT).set_input_files(files)
            page.get_by_role("button", name=re.compile(r"^Upload \d+ files?$"), exact=True).click()
            for path in files:
                row = page.locator("#files-section").get_by_role("row").filter(has=page.get_by_role("link", name=Path(path).name, exact=True))
                row.get_by_role("cell", name="100%", exact=True).wait_for(state="visible")
        else:
            page.locator('#files-section input[type="checkbox"]').check(force=True)

        step("Save draft for review")
        with page.expect_response(lambda response: "/api/records" in response.url and "/files" not in response.url and response.request.method in {"POST", "PUT"}) as saved:
            page.get_by_role("button", name="Save draft", exact=True).click()
        response = saved.value
        if not response.ok:
            raise ValueError(f"CaltechDATA could not save the draft (HTTP {response.status}): {response.text()}")
        record = response.json()
        if record.get("errors"):
            raise ValueError(f"CaltechDATA draft still has validation errors: {record['errors']}")
        if _yes(values.get("reserve_doi", "")):
            with page.expect_response(lambda response: "/draft/pids/doi" in response.url and response.request.method == "POST") as reserved:
                page.get_by_role("button", name="Get a DOI now!", exact=True).click()
            if not reserved.value.ok:
                raise ValueError(f"CaltechDATA could not reserve a DOI (HTTP {reserved.value.status})")
        page.get_by_role("button", name="Preview", exact=True).wait_for(state="visible")
        page.get_by_role("button", name=re.compile(r"^(Publish|Submit for review)$")).wait_for(state="visible")
        logger.info("CaltechDATA draft saved at %s. Review the metadata, files, license and access policy before publishing it yourself.", page.url)


VENUE = CaltechDataVenue()
