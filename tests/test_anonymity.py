"""Unit tests for :mod:`paperpush.anonymity` and the ``anonymous`` venue flag.

Covers the identity terms read from the author list, each identifying-
information detector, the anonymous.4open.science repository scan (against a
fake API, so the suite stays offline), and how ``validate`` / ``paperpush
validate --anonymous`` switch the check on. Files are written into ``tmp_path``.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from PIL import Image

from paperpush import anonymity
from paperpush.cli import main
from paperpush.database import Field, Venue, get_venue, list_venues
from paperpush.subfile import SubFile
from paperpush.validate import validate

AUTHORS = Field(id="authors", label="Authors", type="authorlist")
CONFLICTS = Field(id="conflicts", label="Conflicts", type="authorlist")
PDF = Field(id="pdf_file", label="PDF", type="file")
SUPP = Field(id="supp", label="Supplement", type="file")
ABSTRACT = Field(id="abstract", label="Abstract", type="textarea")

AUTHOR_LINE = "Ada Lovelace | ada@example.org | Analytical Engine Institute | 0000-0001-2345-6789 | yes"
TERMS = [
    ("author name", "Ada Lovelace"),
    ("email", "ada@example.org"),
    ("affiliation", "Analytical Engine Institute"),
    ("ORCID iD", "0000-0001-2345-6789"),
]


def _venue(*fields: Field, anonymous: bool = False, slug: str = "testj") -> Venue:
    return Venue(slug=slug, name="Test", fields=list(fields), anonymous=anonymous)


def _details(findings) -> str:
    return "\n".join(f.detail for f in findings)


# --- the venue flag ----------------------------------------------------------


def test_anonymous_flag_set_for_double_blind_venues():
    assert get_venue("iclr_2027").anonymous is True
    assert get_venue("aaai_2027").anonymous is True
    assert get_venue("biorxiv").anonymous is False


def test_anonymous_flag_defaults_to_false_when_absent():
    assert Venue.from_dict("x", {"name": "X"}).anonymous is False
    assert Venue.from_dict("x", {"anonymous": True}).anonymous is True


def test_every_venue_declares_anonymous_explicitly():
    raw = json.loads((Path(anonymity.__file__).parent / "venues.json").read_text(encoding="utf-8"))
    missing = [slug for slug, entry in raw.items() if "anonymous" not in entry]
    assert missing == []
    assert {v.slug for v in list_venues(include_deprecated=True) if v.anonymous} == {"iclr_2027", "aaai_2027"}


# --- identity terms ------------------------------------------------------------


def test_identity_terms_from_author_list_only():
    venue = _venue(AUTHORS, CONFLICTS)
    values = {"authors": AUTHOR_LINE + "\nPlato | | MIT | | no", "conflicts": "Charles Babbage | cb@example.org | | | no"}
    terms = anonymity.identity_terms(venue, values)
    assert set(terms) == set(TERMS)  # single-word name and short affiliation skipped


def test_identity_terms_openreview_columns():
    field = Field(id="authors", label="Authors", type="authorlist", fields=["open_review_id", "name", "email_suffixes?", "reciprocal_reviewer?"])
    terms = anonymity.identity_terms(_venue(field), {"authors": "~Ada_Lovelace1 | Ada Lovelace | example.org | no"})
    assert ("OpenReview ID", "~Ada_Lovelace1") in terms
    assert ("author name", "Ada Lovelace") in terms


# --- text detectors ------------------------------------------------------------


def test_name_email_and_orcid_found_case_and_space_insensitive():
    text = "ADA   LOVELACE\nada@example.org\nhttps://orcid.org/0000-0001-2345-6789\n"
    details = _details(anonymity.scan_text("m.tex", text, TERMS))
    assert "author name 'Ada Lovelace'" in details
    assert "email 'ada@example.org'" in details
    assert "ORCID iD" in details


def test_name_only_in_reference_list_is_a_permitted_self_citation():
    text = "Introduction\nWe build on prior work [1].\nReferences\n[1] Ada Lovelace. Notes. 1843.\n"
    assert "Ada Lovelace" not in _details(anonymity.scan_text("m.pdf", text, TERMS))


def test_name_in_appendix_after_references_is_reported():
    text = "Intro\nReferences\n[1] Ada Lovelace. Notes.\nAppendix A\nThanks to Ada Lovelace.\n"
    assert "Ada Lovelace" in _details(anonymity.scan_text("m.pdf", text, TERMS))


def test_home_path_username_reported_but_generic_users_ignored():
    text = "saved to /Users/alovelace/proj/fig.png and /home/runner/work/x\n"
    details = _details(anonymity.scan_text("nb.ipynb", text, [], is_manuscript=False))
    assert "'alovelace'" in details
    assert "runner" not in details


def test_github_link_to_author_account_reported():
    text = "Code: https://github.com/adalovelace/engine and https://github.com/pytorch/pytorch"
    details = _details(anonymity.scan_text("m.tex", text, TERMS))
    assert "github.com/adalovelace/engine" in details
    assert "pytorch" not in details


def test_acknowledgments_and_camera_ready_latex_flagged():
    tex = "\\usepackage[final]{neurips_2026}\n\\section*{Acknowledgments}\nWe thank our funders.\n"
    details = _details(anonymity.scan_text("main.tex", tex, [], is_latex=True))
    assert "Acknowledgments" in details
    assert "camera-ready" in details


def test_commented_out_final_switch_is_ignored():
    tex = "% \\iclrfinalcopy\n\\usepackage{iclr2027_conference}\n"
    assert _details(anonymity.scan_text("main.tex", tex, [], is_latex=True)) == ""


def test_plain_text_acknowledgments_heading():
    assert "Acknowledgments" in _details(anonymity.scan_text("m.pdf", "5 Conclusion\nDone.\nAcknowledgements\nThanks.\n", []))


# --- file formats ----------------------------------------------------------------


def test_pdf_author_metadata_reported(tmp_path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Author": "Ada Lovelace"})
    pdf = tmp_path / "paper.pdf"
    with pdf.open("wb") as fh:
        writer.write(fh)
    assert "PDF metadata Author is 'Ada Lovelace'" in _details(anonymity.scan_file(pdf, []))


def test_docx_core_properties_reported(tmp_path):
    docx = tmp_path / "paper.docx"
    with zipfile.ZipFile(docx, "w") as zf:
        zf.writestr("docProps/core.xml", "<cp:coreProperties><dc:creator>Ada Lovelace</dc:creator><cp:lastModifiedBy>Anonymous</cp:lastModifiedBy></cp:coreProperties>")
        zf.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Body</w:t></w:r></w:p></w:body></w:document>")
    details = _details(anonymity.scan_file(docx, []))
    assert "author 'Ada Lovelace'" in details
    assert "last modified by" not in details


def test_image_exif_artist_reported(tmp_path):
    exif = Image.Exif()
    exif[0x013B] = "Ada Lovelace"
    img = tmp_path / "fig.jpg"
    Image.new("RGB", (4, 4)).save(img, exif=exif)
    assert "Artist is 'Ada Lovelace'" in _details(anonymity.scan_file(img, []))


def test_archive_members_scanned_but_bib_skipped(tmp_path):
    bundle = tmp_path / "supp.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("src/main.tex", "\\author{Ada Lovelace}\n")
        zf.writestr("src/refs.bib", "@article{a, author={Lovelace, Ada and Ada Lovelace}}")
    findings = anonymity.scan_file(bundle, TERMS)
    assert [f.where for f in findings] == ["supp.zip:src/main.tex"]


# --- anonymous.4open.science -----------------------------------------------------


def test_find_anonymous_repos_strips_trailing_punctuation():
    text = "Code at https://anonymous.4open.science/r/My-Repo-4C14. See also anonymous.4open.science/r/x (no scheme), https://anonymous.4open.science/r/abc/README.md"
    assert anonymity.find_anonymous_repos([text]) == ["My-Repo-4C14", "abc"]


def _fake_api(repo: str, tree: dict[str, list[dict]], files: dict[str, bytes], error: str | None = None):
    base = f"{anonymity.ANONYMOUS_GITHUB}/api/repo/{repo}/"

    def get(url, timeout=None):
        if error is not None:
            return 404, json.dumps({"error": error}).encode()
        assert url.startswith(base), url
        rest = url[len(base) :]
        if rest.startswith("files/?path="):
            from urllib.parse import unquote

            return 200, json.dumps(tree[unquote(rest[len("files/?path=") :])]).encode()
        if rest.startswith("file/"):
            from urllib.parse import unquote

            return 200, files[unquote(rest[len("file/") :])]
        raise AssertionError(url)

    return get


def test_anonymous_repo_scanned_recursively(monkeypatch):
    tree = {
        "": [{"name": "LICENSE", "path": "", "size": 60, "sha": "1"}, {"name": "src", "path": ""}, {"name": "w.pt", "path": "", "size": 9, "sha": "3"}],
        "src": [{"name": "run.py", "path": "src", "size": 50, "sha": "2"}],
    }
    files = {
        "LICENSE": b"MIT License\n\nCopyright (c) 2026 Ada Lovelace\n",
        "src/run.py": b"DATA = '/Users/alovelace/data'\n# contact ada@example.org\n",
    }
    monkeypatch.setattr(anonymity, "_http_get", _fake_api("R1", tree, files))
    details = _details(anonymity.scan_anonymous_repo("R1", TERMS))
    assert "copyright line names 'Ada Lovelace'" in details
    assert "email 'ada@example.org'" in details
    assert "'alovelace'" in details


def test_anonymized_license_is_quiet(monkeypatch):
    tree = {"": [{"name": "LICENSE", "path": "", "size": 60, "sha": "1"}]}
    files = {"LICENSE": b"MIT License\n\nCopyright (c) 2026 Anonymous Authors\nCopyright 2026 XXXX\n"}
    monkeypatch.setattr(anonymity, "_http_get", _fake_api("R2", tree, files))
    assert anonymity.scan_anonymous_repo("R2", TERMS) == []


def test_expired_anonymous_repo_reported(monkeypatch):
    monkeypatch.setattr(anonymity, "_http_get", _fake_api("R3", {}, {}, error="repository_expired"))
    assert "has expired" in _details(anonymity.scan_anonymous_repo("R3", TERMS))


# --- validate wiring -------------------------------------------------------------


def _leaky_pdf(tmp_path) -> Path:
    tex = tmp_path / "main.tex"
    tex.write_text("\\title{A paper}\n\\author{Ada Lovelace}\nCode: https://anonymous.4open.science/r/R1\n", encoding="utf-8")
    return tex


def _anonymity_warnings(issues) -> list[str]:
    return [i.message for i in issues if i.message.startswith("anonymity:")]


def test_validate_runs_anonymity_check_for_anonymous_venue(tmp_path):
    venue = _venue(AUTHORS, PDF, anonymous=True)
    sub = SubFile(venue="testj", values={"authors": AUTHOR_LINE, "pdf_file": str(_leaky_pdf(tmp_path))})
    warnings = _anonymity_warnings(validate(sub, venue, check_links=False, check_references=False, check_manuscript=False, check_sensitive=False))
    assert any("Ada Lovelace" in w for w in warnings)
    # The linked mirror was looked up (offline stub -> reported as uncheckable).
    assert any("anonymous.4open.science/r/R1" in w for w in warnings)


def test_validate_skips_anonymity_for_regular_venue_unless_asked(tmp_path):
    venue = _venue(AUTHORS, PDF)
    sub = SubFile(venue="testj", values={"authors": AUTHOR_LINE, "pdf_file": str(_leaky_pdf(tmp_path))})
    kwargs = dict(check_links=False, check_references=False, check_manuscript=False, check_sensitive=False)
    assert _anonymity_warnings(validate(sub, venue, **kwargs)) == []
    assert _anonymity_warnings(validate(sub, venue, check_anonymous=True, **kwargs))


def test_validate_scans_sub_text_fields(tmp_path):
    venue = _venue(AUTHORS, ABSTRACT, anonymous=True)
    sub = SubFile(venue="testj", values={"authors": AUTHOR_LINE, "abstract": "In our lab at Analytical Engine Institute we..."})
    warnings = _anonymity_warnings(validate(sub, venue, check_links=False, check_references=False, check_manuscript=False, check_sensitive=False))
    assert any("affiliation 'Analytical Engine Institute'" in w for w in warnings)


def test_cli_validate_anonymous_flag(tmp_path, monkeypatch, capsys):
    import paperpush.cli as cli

    # A slug of its own: validate caches its schema model per slug, so reusing a
    # real venue's slug here would leak this two-field schema into later tests.
    venue = _venue(AUTHORS, PDF, slug="test_anonymity_cli")
    monkeypatch.setattr(cli, "get_venue", lambda slug: venue)
    sub = tmp_path / "paper.sub"
    sub.write_text(f"@venue: test_anonymity_cli\nauthors: {AUTHOR_LINE}\npdf_file: {_leaky_pdf(tmp_path)}\n", encoding="utf-8")

    assert main(["validate", str(sub), "--dont-check-links", "--dont-check-references", "--dont-check-manuscript"]) == 0
    assert "anonymity:" not in capsys.readouterr().err

    assert main(["validate", str(sub), "--anonymous", "--dont-check-links", "--dont-check-references", "--dont-check-manuscript"]) == 0
    err = capsys.readouterr().err
    assert "anonymity: author name 'Ada Lovelace'" in err
