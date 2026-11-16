"""Zenodo's InvenioRDM login and deposit form; save drafts, never publish."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from ...validate import parse_authors
from ..base import Venue
from ..common import DEFAULT_TIMEOUT_SECONDS, apply_default_timeouts, hold_open, hold_open_on_failure, open_run_context
from ..login import ORCID_COOKIE_NAME, VenueLoginError, fill_login_form
from ..recovery import guard, step
from ..recovery_channel import browser_launch_args

logger = logging.getLogger(__name__)
UPLOAD_URL = "https://zenodo.org/uploads/new"
MODAL = ".ui.modal.visible.active"
FILE_INPUT = '#files-section input[type="file"]'
EDITOR_FRAME = 'iframe[title="Rich Text Area"]'
DEFAULT_LICENSE = "Creative Commons Attribution 4.0 International"
FUNDER_OPTION = '[role="option"][name={name}], [role="option"][pid={identifier}]'
CUSTOM_TEXT_FIELDS = {"code_url": "custom_fields.code:codeRepository", "journal_title": "custom_fields.journal:journal.title", "journal_issn": "custom_fields.journal:journal.issn", "journal_volume": "custom_fields.journal:journal.volume", "journal_issue": "custom_fields.journal:journal.issue", "journal_pages": "custom_fields.journal:journal.pages", "imprint_title": "custom_fields.imprint:imprint.title", "imprint_isbn": "custom_fields.imprint:imprint.isbn", "imprint_place": "custom_fields.imprint:imprint.place", "imprint_pages": "custom_fields.imprint:imprint.pages", "imprint_edition": "custom_fields.imprint:imprint.edition", "thesis_university": "custom_fields.thesis:thesis.university", "thesis_department": "custom_fields.thesis:thesis.department", "thesis_type": "custom_fields.thesis:thesis.type", "thesis_date_submitted": "custom_fields.thesis:thesis.date_submitted", "thesis_date_defended": "custom_fields.thesis:thesis.date_defended", "conference_title": "custom_fields.meeting:meeting.title", "conference_acronym": "custom_fields.meeting:meeting.acronym", "conference_place": "custom_fields.meeting:meeting.place", "conference_dates": "custom_fields.meeting:meeting.dates", "conference_session": "custom_fields.meeting:meeting.session", "conference_session_part": "custom_fields.meeting:meeting.session_part", "conference_url": "custom_fields.meeting:meeting.url"}
DOI_RADIO = 'input[name="radioGroup"][value="{value}"]'

LOGIN_URL = "https://zenodo.org/login/"
LOGIN_FORM = 'form[name="login_user_form"]'
EMAIL_SELECTOR = f'{LOGIN_FORM} input[name="email"]'
PASSWORD_SELECTOR = f'{LOGIN_FORM} input[name="password"]'
LOGIN_BUTTON_SELECTOR = f'{LOGIN_FORM} button[type="submit"]'
SIGNED_IN_SELECTOR = '#invenio-menu a[href="/logout/"], #invenio-menu a[href="/logout"]'
OAUTH_PROVIDERS = {
    "orcid": {"name": "ORCID", "host": "orcid.org", "identity": "#username-input", "password": "#password", "submit": "Sign in to ORCID"},  # nosec B105 - CSS selector, not a secret
    "github": {"name": "GitHub", "host": "github.com", "identity": "#login_field", "password": "#password", "submit": "Sign in"},  # nosec B105 - CSS selector, not a secret
}


def _check_host(page, hostname: str) -> None:
    url = urlsplit(page.url)
    if url.scheme != "https" or url.hostname != hostname or url.username or url.password:
        raise VenueLoginError("Sign-in redirected to an unexpected host; finish signing in manually in the browser. No credentials were entered on that host.")


def _rows(value: str) -> list[list[str]]:
    return [[part.strip() for part in line.split("|")] for line in value.splitlines() if line.strip()]


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
    # Wait for the actual addition/suggestion rather than pressing Enter while
    # remote search results are still replacing the dropdown's options.
    combo.locator("input").fill(value)
    additional = combo.get_by_role("option", name="Add " + value, exact=True)
    exact = combo.get_by_role("option", name=value, exact=True)
    named = combo.locator(f'[role="option"][name={json.dumps(value)}]')
    additional.or_(exact).or_(named).first.click()
    combo.locator("a.ui.label").filter(has_text=re.compile("^" + re.escape(value) + "$", re.I)).wait_for(state="visible")


def _person_name(name: str) -> tuple[str, str]:
    if "," in name:
        family, given = name.split(",", 1)
        return family.strip(), given.strip()
    parts = name.rsplit(None, 1)
    return (parts[-1], parts[0] if len(parts) > 1 else "")


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


def _resource_label(value: str) -> str:
    # Preserve older .sub files, which used ungrouped resource names.
    if value in {"Diagram", "Drawing", "Figure", "Photo", "Plot"}:
        return "Image / " + value
    if value == "Computational notebook":
        return "Software / " + value
    publications = {"Annotation collection", "Book", "Book chapter", "Conference paper", "Conference proceeding", "Data paper", "Journal", "Journal article", "Output management plan", "Patent", "Peer review", "Preprint", "Project deliverable", "Project milestone", "Proposal", "Report", "Software documentation", "Standard", "Taxonomic treatment", "Technical note", "Thesis", "Working paper"}
    return "Publication / " + value if value in publications else value


def _add_people(page, value: str, *, contributors: bool = False) -> None:
    columns = ["name", "role", "affiliation?", "orcid?", "type?"] if contributors else ["name", "affiliation?", "orcid?", "role?", "type?"]
    for person in parse_authors(value, columns):
        page.get_by_role("button", name="Add contributor" if contributors else "Add author", exact=True).click()
        modal = page.locator(MODAL)
        if person.get("type", "").lower() == "organization":
            modal.get_by_text("Organization", exact=True).click()
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


def _fill_license(page, name: str) -> None:
    if not name:
        return
    section = page.locator("#basic-information-section")
    if section.get_by_role("listitem").filter(has_text=name).count():
        return
    section.get_by_role("listitem").filter(has_text=DEFAULT_LICENSE).get_by_role("button", name="Edit", exact=True).click()
    modal = page.locator(MODAL)
    modal.get_by_role("textbox", name="Search for licenses", exact=True).fill(name)
    modal.get_by_text(name, exact=True).click()
    modal.get_by_role("button", name="Change license", exact=True).click()
    modal.wait_for(state="hidden")


def _fill_identifiers(page, value: str, *, related: bool = False) -> None:
    for index, row in enumerate(_rows(value)):
        # Unlike CaltechDATA, Zenodo starts with no identifier rows.
        page.get_by_role("button", name="Add related work" if related else "Add identifier", exact=True).click()
        prefix = f"metadata.{'related_identifiers' if related else 'identifiers'}.{index}"
        _input(page, f"{prefix}.identifier").fill(row[0])
        if related:
            _, relation, scheme, resource = (row + ["", "", ""])[:4]
            _choose(_combo(page, f"{prefix}.relation_type"), relation)
            _choose(_combo(page, f"{prefix}.scheme"), _scheme(row[0], scheme))
            if resource:
                _choose(_combo(page, f"{prefix}.resource_type"), _resource_label(resource))
        else:
            _choose(_combo(page, f"{prefix}.scheme"), row[1])


def _select_community(page, slug: str) -> None:
    page.get_by_role("button", name="Select a community", exact=True).click()
    modal = page.get_by_role("dialog", name="Select a community", exact=True)
    modal.get_by_role("textbox", name="Search in all communities", exact=True).fill(slug)
    modal.get_by_role("button", name="Search", exact=True).click()
    # Match the actual community identifier, never the first search result.
    result = modal.locator(".community-item:visible").filter(has=page.locator(f'a[href$="/communities/{slug}/"], a[href$="/communities/{slug}"]'))
    result.get_by_role("button", name=re.compile(r"^Select(?: |$)")).click()
    modal.wait_for(state="hidden")


def _check_values(values: dict) -> list[str]:
    files = [str(Path(row[0]).expanduser().resolve()) for row in _rows(values.get("data_files", ""))]
    if not files:
        raise ValueError("data_files is empty; Zenodo's upload form requires at least one file")
    if len(files) > 100:
        raise ValueError("Zenodo accepts at most 100 files per record; zip the collection first")
    if len({Path(path).name for path in files}) != len(files):
        raise ValueError("data_files must have unique filenames")
    for path in files:
        if not Path(path).is_file():
            raise ValueError(f"data_files: file does not exist: {path}")
    if sum(Path(path).stat().st_size for path in files) > 50_000_000_000:
        raise ValueError("data_files exceeds Zenodo's standard 50 GB quota")
    if values.get("embargo_date") and values.get("access_right") != "Restricted":
        raise ValueError("embargo_date requires access_right: Restricted")
    if values.get("embargo_reason") and not values.get("embargo_date"):
        raise ValueError("embargo_reason requires embargo_date")
    communities = [line.strip() for line in values.get("communities", "").splitlines() if line.strip()]
    if len(communities) > 1:
        raise ValueError("Zenodo's new-upload form selects one community; add further communities after publication")
    if communities and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", communities[0]):
        raise ValueError("communities: use the community identifier from its URL")
    for row in _rows(values.get("related_identifiers", "")):
        if len(row) < 2 or not row[1]:
            raise ValueError("related_identifiers requires identifier | relation")
        _scheme(row[0], row[2] if len(row) > 2 else "")
    return files


class ZenodoVenue(Venue):
    slug = "zenodo"
    logged_in_names = ("Log out",)
    supports_recovery = True
    supports_orcid_login = True
    supports_github_login = True
    final_submit_names = ("Publish", "Publish record", "Submit for review")

    @property
    def login_url(self) -> str:
        return LOGIN_URL

    def is_logged_in(self, page, *, timeout_ms: int | None = None) -> bool:
        # Guests also see "My dashboard". The logout control is specific to
        # authenticated users and may be inside a collapsed account menu.
        try:
            page.locator(SIGNED_IN_SELECTOR).first.wait_for(state="attached", timeout=timeout_ms if timeout_ms is not None else self.logged_in_timeout_ms)
            _check_host(page, "zenodo.org")
            return True
        except (PWTimeout, VenueLoginError):
            return False

    def login(self, page, username: str, password: str, *, orcid: bool = False, github: bool = False, timeout_ms: int = 15000) -> None:
        if orcid and github:
            raise ValueError("choose one login method: email, ORCID, or GitHub")
        try:
            page.goto(LOGIN_URL)
            if orcid or github:
                self._login_oauth(page, username, password, provider="orcid" if orcid else "github", timeout_ms=timeout_ms)
            else:
                _check_host(page, "zenodo.org")
                fill_login_form(page, username, password, userid_sel=EMAIL_SELECTOR, password_sel=PASSWORD_SELECTOR, submit_sel=LOGIN_BUTTON_SELECTOR, timeout_ms=timeout_ms)
            if not self.is_logged_in(page, timeout_ms=timeout_ms):
                if orcid or github:
                    raise VenueLoginError("The provider has not returned a signed-in Zenodo session. Check your provider credentials and complete any two-factor, authorization, or account-linking prompts in the browser.")
                raise VenueLoginError("Zenodo sign-in did not complete. Check your email and password, confirm your email address, and complete any additional sign-in prompts in the browser.")
        except PWTimeout as exc:
            raise VenueLoginError("Zenodo sign-in timed out. Check the browser for additional sign-in prompts or changes to the login form.") from exc

    def _login_oauth(self, page, username: str, password: str, *, provider: str, timeout_ms: int) -> None:
        cfg = OAUTH_PROVIDERS[provider]
        # Zenodo's provider links navigate in this tab. Preserve the OAuth
        # callback and state rather than reloading Zenodo after the form POST.
        page.get_by_role("link", name=f"Sign in with {cfg['name']}", exact=True).click(timeout=timeout_ms)
        if self.is_logged_in(page, timeout_ms=100):
            return  # An existing provider session completed the round trip.
        identity = page.locator(cfg["identity"])
        try:
            identity.wait_for(state="visible", timeout=timeout_ms)
        except PWTimeout:
            if self.is_logged_in(page, timeout_ms=100):
                return
            raise
        _check_host(page, cfg["host"])
        if provider == "orcid":
            cookie = page.get_by_role("button", name=ORCID_COOKIE_NAME, exact=True)
            if cookie.is_visible():
                cookie.click(timeout=timeout_ms)
        _check_host(page, cfg["host"])
        identity.fill(username, timeout=timeout_ms)
        secret = page.locator(cfg["password"])
        secret.wait_for(state="visible", timeout=timeout_ms)
        _check_host(page, cfg["host"])
        secret.fill(password, timeout=timeout_ms)
        page.get_by_role("button", name=cfg["submit"], exact=True).click(timeout=timeout_ms)
        # MFA, provider consent, and linking a Zenodo account remain on this
        # page for verify_login's existing interactive fallback to the author.

    def submit(self, values: dict, *, headless: bool = False, debug: bool = False, new_session: bool = False, timeout: float = DEFAULT_TIMEOUT_SECONDS, keep_open_on_failure: bool = True) -> None:
        _check_values(values)
        if debug:
            headless = False
        with sync_playwright() as playwright, hold_open_on_failure(headless=headless, keep_open=keep_open_on_failure):
            browser = playwright.chromium.launch(headless=headless, args=browser_launch_args())
            context = open_run_context(browser, self.session_path(), new_session=new_session)
            apply_default_timeouts(context, timeout)
            page = context.new_page()
            page.set_viewport_size({"width": 1500, "height": 1500})
            self.ensure_signed_in(page, context, debug=debug)
            page = guard(page)
            if debug:
                page.pause()
            step("New Zenodo upload")
            page.goto(UPLOAD_URL)
            self._fill_deposit(page, values)
            hold_open()

    def _fill_deposit(self, page, values: dict) -> None:
        files = _check_values(values)
        _input(page, "metadata.title").wait_for(state="visible")
        cookie = page.get_by_role("button", name="Accept only essential cookies", exact=True)
        if cookie.is_visible():
            cookie.click()
        if values.get("communities", "").strip():
            _select_community(page, values["communities"].strip())

        step("Basic information")
        # The initial form has neither DOI mode selected. Choose managed without
        # reserving a DOI; registration happens only when the author publishes.
        page.locator(DOI_RADIO.format(value="unmanaged" if values.get("doi") else "managed")).locator("..").locator("label").click()
        if values.get("doi"):
            page.get_by_role("textbox", name="Copy/paste your existing DOI here...", exact=True).fill(values["doi"])
        _choose(_combo(page, "metadata.resource_type"), _resource_label(values["resource_type"]))
        _input(page, "metadata.title").fill(values["title"])
        _fill_titles(page, values.get("additional_titles", ""))
        _input(page, "metadata.publication_date").fill(values["publication_date"])
        _add_people(page, values["authors"])
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
                _combo(page, "metadata.subjects").locator("input").press("Escape")
        for language in values.get("languages", "").split(","):
            if language.strip():
                _choose(_combo(page, "metadata.languages"), language.strip())
                _combo(page, "metadata.languages").locator("input").press("Escape")
        for index, row in enumerate(_rows(values.get("dates", ""))):
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
        for index, reference in enumerate(line.strip() for line in values.get("references", "").splitlines() if line.strip()):
            page.get_by_role("button", name="Add reference", exact=True).click()
            _input(page, f"metadata.references.{index}.reference").fill(reference)

        step("Software and publishing information")
        for key, path in CUSTOM_TEXT_FIELDS.items():
            if values.get(key):
                _input(page, path).fill(values[key])
        for language in values.get("programming_languages", "").split(","):
            if language.strip():
                _choose(_combo(page, "custom_fields.code:programmingLanguage"), language.strip())
                _combo(page, "custom_fields.code:programmingLanguage").locator("input").press("Escape")
        if values.get("development_status"):
            status = page.locator('[name="custom_fields.code:developmentStatus"]')
            status.click()
            status.get_by_role("option", name=values["development_status"], exact=True).click()

        step("Files and visibility")
        if values.get("access_right"):
            page.locator("#visibility-section").get_by_role("button", name=values["access_right"], exact=True).click()
        if values.get("embargo_date"):
            page.locator('label[for="access.embargo.active"]').last.click()
            _input(page, "access.embargo.until").fill(values["embargo_date"])
            if values.get("embargo_reason"):
                _input(page, "access.embargo.reason").fill(values["embargo_reason"])
        page.locator(FILE_INPUT).set_input_files(files)
        for path in files:
            row = page.locator("#files-section").get_by_role("row").filter(has=page.get_by_role("link", name=Path(path).name, exact=True))
            row.get_by_role("cell", name="100%", exact=True).wait_for(state="visible")

        step("Save Zenodo draft for review")
        with page.expect_response(lambda response: "/api/records" in response.url and "/files" not in response.url and response.request.method in {"POST", "PUT"}) as saved:
            page.get_by_role("button", name="Save draft", exact=True).click()
        response = saved.value
        if not response.ok:
            raise ValueError(f"Zenodo could not save the draft (HTTP {response.status}): {response.text()}")
        record = response.json()
        if record.get("errors"):
            raise ValueError(f"Zenodo draft still has validation errors: {record['errors']}")
        page.get_by_role("button", name="Preview", exact=True).wait_for(state="visible")
        page.get_by_role("button", name=re.compile(r"^(Publish|Submit for review)$")).wait_for(state="visible")
        logger.info("Zenodo draft saved at %s. Review the metadata, files, license and access policy before publishing it yourself.", page.url)


VENUE = ZenodoVenue()
