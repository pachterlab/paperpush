"""Unit tests for :mod:`paperpush.sensitive`.

These exercise each detector in isolation -- secrets, editable-document links,
LaTeX comments, EXIF GPS, PDF text/metadata, archive expansion, and junk-file
detection -- plus the ``scan_file`` / ``scan_paths`` dispatch. Files are written
into ``tmp_path`` so nothing depends on fixtures outside this module.
"""

from __future__ import annotations

import contextlib
import io
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from PIL import Image

import pytest

from paperpush import sensitive
from paperpush.sensitive import Finding, scan_file, scan_paths


# Captured at import time, before conftest's autouse fixture stubs the probe out
# to keep the suite offline; the tests below exercise the real request logic
# against a patched ``urlopen``.
_real_url_is_reachable = sensitive._url_is_reachable


def _categories(findings) -> set[str]:
    return {f.category for f in findings}


# --- secret / credential patterns ------------------------------------------


@pytest.mark.parametrize(
    "text,category",
    [
        ("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----", "private key"),
        ("aws = AKIAIOSFODNN7EXAMPLE", "AWS access key"),
        ("key: AIzaSyA1234567890123456789012345678901X", "Google API key"),  # gitleaks:allow -- fake fixture
        ("token ghp_" + "a" * 36, "GitHub token"),
        ("slack xoxb-1234567890-abcdefghij", "Slack token"),
        ("password = 'hunter2please'", "hardcoded credential"),
        ('api_key: "abcdef123456"', "hardcoded credential"),  # gitleaks:allow -- fake fixture
        ("db = postgres://user:s3cretpw@host/db", "URL with embedded password"),
    ],
)
def test_secret_patterns_detected(text, category):
    findings = list(sensitive._iter_secret_findings("f.txt", text))
    assert category in _categories(findings), findings


def test_secret_value_is_masked_not_echoed():
    secret = "AKIAIOSFODNN7EXAMPLE"
    (findings,) = list(sensitive._iter_secret_findings("f.txt", f"k = {secret}"))
    # The full secret must never appear in a message that could be logged/printed.
    assert secret not in findings.detail
    assert "AKIA" in findings.detail  # a short, non-reversible hint is fine


def test_empty_password_template_field_is_not_flagged():
    # A blank ".sub"/template field ("password:") must not read as a credential.
    assert list(sensitive._iter_secret_findings("f.txt", "password:\napi_key: \n")) == []


def test_prose_mentioning_password_is_not_flagged():
    text = "Users must choose a password with at least eight characters."
    cats = _categories(sensitive._iter_secret_findings("f.txt", text))
    assert "hardcoded credential" not in cats


def test_editable_google_doc_link_flagged():
    text = "notes: https://docs.google.com/document/d/1AbCdEf_gh/edit?usp=sharing"
    (finding,) = [f for f in sensitive._iter_secret_findings("f.txt", text) if f.category == "editable document link"]
    assert "docs.google.com" in finding.detail


# --- LaTeX comments --------------------------------------------------------


def test_latex_note_comment_flagged_and_counted():
    text = "% TODO fix this\nreal line\n  % just a normal comment\n"
    findings = list(sensitive._iter_latex_findings("main.tex", text))
    cats = _categories(findings)
    assert "LaTeX note comment" in cats
    assert "LaTeX comments" in cats
    tally = [f for f in findings if f.category == "LaTeX comments"][0]
    assert "2 source comment" in tally.detail


def test_escaped_percent_is_not_a_comment():
    # "50\% faster" is a literal percent, not a comment; no comment reported.
    assert list(sensitive._iter_latex_findings("main.tex", "The gain is 50\\% overall.\n")) == []


def test_percent_after_escaped_percent_still_detected():
    text = "value 50\\% better % actual comment here\n"
    comment = sensitive._latex_comment(text.splitlines()[0])
    assert comment is not None
    assert "actual comment" in comment


def test_latex_findings_skipped_for_non_latex_extension(tmp_path):
    # A .txt with a "%" line is not LaTeX source, so the comment scan is off.
    p = tmp_path / "notes.txt"
    p.write_text("% this is not a latex comment\n")
    assert "LaTeX comments" not in _categories(scan_file(p))


# --- EXIF GPS --------------------------------------------------------------


def _jpeg_with_gps(tmp_path: Path) -> Path:
    img = Image.new("RGB", (16, 16), (120, 120, 120))
    exif = img.getexif()
    exif[0x8825] = {1: "N", 2: (40.0, 44.0, 53.4), 3: "W", 4: (73.0, 59.0, 20.0)}
    path = tmp_path / "figure_photo.jpg"
    img.save(path, format="JPEG", exif=exif)
    return path


def test_gps_coordinates_detected_in_image(tmp_path):
    path = _jpeg_with_gps(tmp_path)
    findings = scan_file(path)
    gps = [f for f in findings if f.category == "GPS location"]
    assert gps, findings
    assert "40.7" in gps[0].detail and "-73.9" in gps[0].detail


def test_plain_image_has_no_gps_finding(tmp_path):
    path = tmp_path / "plain.png"
    Image.new("RGB", (8, 8), (0, 0, 0)).save(path)
    assert scan_file(path) == []


# --- PDF text + metadata ---------------------------------------------------


def test_pdf_metadata_secret_flagged(tmp_path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Author": "token ghp_" + "b" * 36})
    path = tmp_path / "manuscript.pdf"
    with path.open("wb") as fh:
        writer.write(fh)
    assert "GitHub token" in _categories(scan_file(path))


# --- archives --------------------------------------------------------------


def test_zip_members_and_junk_scanned(tmp_path):
    path = tmp_path / "source.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("paper.tex", "% FIXME cite this\nkey = AKIAIOSFODNN7EXAMPLE\n")
        z.writestr(".git/config", "[core]\n")
        z.writestr("paper.aux", "junk")
        z.writestr(".DS_Store", "junk")
    cats = _categories(scan_file(path))
    assert "AWS access key" in cats
    assert "LaTeX note comment" in cats
    assert "unnecessary file" in cats


def test_archive_member_where_names_the_member(tmp_path):
    path = tmp_path / "source.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("sub/secret.tex", "password = 'longenoughsecret'\n")
    findings = [f for f in scan_file(path) if f.category == "hardcoded credential"]
    assert findings and findings[0].where == "source.zip:sub/secret.tex"


# --- link reachability (all URLs) ------------------------------------------


def test_find_urls_extracts_and_trims():
    text = "See (https://example.com/a), and https://foo.org/b. Also http://x.io/y]\n"
    urls = set(sensitive._find_urls(text))
    assert "https://example.com/a" in urls  # trailing ) stripped
    assert "https://foo.org/b" in urls  # trailing . stripped
    assert "http://x.io/y" in urls  # trailing ] stripped


def test_scan_links_flags_broken_url(tmp_path, monkeypatch):
    p = tmp_path / "paper.tex"
    p.write_text("Data at https://example.com/dead-page and https://example.com/live\n")

    def reach(url, **k):
        return False if "dead-page" in url else True

    monkeypatch.setattr(sensitive, "_url_is_reachable", reach)
    findings = sensitive.scan_links([p], check_access=True)
    assert [f.category for f in findings] == ["broken link"]
    assert "dead-page" in findings[0].detail


def test_scan_links_broken_github_worded_as_private_repo(tmp_path, monkeypatch):
    p = tmp_path / "paper.tex"
    p.write_text("Code at https://github.com/acme/secret-repo\n")
    monkeypatch.setattr(sensitive, "_url_is_reachable", lambda url, **k: False)
    findings = sensitive.scan_links([p], check_access=True)
    assert [f.category for f in findings] == ["private repository"]
    assert "github.com/acme/secret-repo" in findings[0].detail


def test_scan_links_unknown_reachability_no_finding(tmp_path, monkeypatch):
    # Offline / rate-limited / bot-blocked -> None -> stay silent.
    p = tmp_path / "paper.tex"
    p.write_text("See https://example.com/maybe\n")
    monkeypatch.setattr(sensitive, "_url_is_reachable", lambda url, **k: None)
    assert sensitive.scan_links([p], check_access=True) == []


def test_scan_links_check_access_false_skips_probes(tmp_path, monkeypatch):
    p = tmp_path / "paper.tex"
    p.write_text("https://example.com/whatever\n")
    monkeypatch.setattr(sensitive, "_url_is_reachable", lambda url, **k: False)
    # check_access=False must not consult the probe at all.
    assert sensitive.scan_links([p], check_access=False) == []


def test_scan_links_probes_urls_concurrently(tmp_path, monkeypatch):
    # Each probe blocks until every worker has arrived, so this only finishes if
    # the probes really do overlap; a serial loop would deadlock and time out.
    workers = sensitive.LINK_CHECK_WORKERS
    p = tmp_path / "paper.tex"
    p.write_text("".join(f"https://example.com/p{i}\n" for i in range(workers)))

    barrier = threading.Barrier(workers, timeout=10)

    def reach(url, **k):
        barrier.wait()
        return False

    monkeypatch.setattr(sensitive, "_url_is_reachable", reach)
    findings = sensitive.scan_links([p], check_access=True)
    assert len(findings) == workers


def test_scan_links_findings_keep_source_order(tmp_path, monkeypatch):
    # Threading must not reorder the findings relative to the manuscript text.
    p = tmp_path / "paper.tex"
    p.write_text("".join(f"https://example.com/p{i}\n" for i in range(6)))
    monkeypatch.setattr(sensitive, "_url_is_reachable", lambda url, **k: False)
    findings = sensitive.scan_links([p], check_access=True)
    assert [f"https://example.com/p{i}" in f.detail for i, f in enumerate(findings)] == [True] * 6


@pytest.mark.parametrize("member", ["ref.bib", "plain.bst", "style.cls", "pkg.sty"])
def test_scan_links_skips_bibliography_and_style_members(tmp_path, monkeypatch, member):
    # Citation/template URLs aren't the submission's claims -- and left in, they
    # eat the MAX_LINK_CHECKS budget the manuscript's own links need.
    path = tmp_path / "source.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(member, "url = {https://example.com/cited-and-gone}\n")
        z.writestr("main.tex", "Code at https://example.com/ours\n")
    monkeypatch.setattr(sensitive, "_url_is_reachable", lambda url, **k: False)
    findings = sensitive.scan_links([path], check_access=True)
    assert len(findings) == 1
    assert "example.com/ours" in findings[0].detail


def test_scan_links_skips_bibliography_listed_directly(tmp_path, monkeypatch):
    # Same rule when the .bib is a top-level upload rather than an archive member.
    p = tmp_path / "ref.bib"
    p.write_text("url = {https://example.com/cited-and-gone}\n")
    monkeypatch.setattr(sensitive, "_url_is_reachable", lambda url, **k: False)
    assert sensitive.scan_links([p], check_access=True) == []


def test_secret_scan_still_reads_skipped_link_scan_files(tmp_path):
    # The link-scan skip must not blind the secret/LaTeX passes to the same file.
    p = tmp_path / "ref.bib"
    p.write_text("note = {key AKIAIOSFODNN7EXAMPLE}\n")
    assert "AWS access key" in _categories(scan_file(p))


def test_url_is_reachable_skips_get_retry_after_transport_failure(monkeypatch):
    # A timeout/DNS failure ends the probe: one request, not a HEAD-then-GET pair.
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.get_method())
        raise OSError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _real_url_is_reachable("https://example.com/x") is None
    assert calls == ["HEAD"]


def test_url_is_reachable_retries_with_get_when_head_rejected(monkeypatch):
    # 405 means "no HEAD here", not "missing" -- that retry is still worth it.
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.get_method())
        if req.get_method() == "HEAD":
            raise urllib.error.HTTPError("https://example.com/x", 405, "no", {}, None)
        return contextlib.nullcontext()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _real_url_is_reachable("https://example.com/x") is True
    assert calls == ["HEAD", "GET"]


def test_scan_links_finds_url_inside_archive_member(tmp_path, monkeypatch):
    path = tmp_path / "source.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("paper.tex", "Broken: https://example.com/gone\n")
    monkeypatch.setattr(sensitive, "_url_is_reachable", lambda url, **k: False)
    findings = sensitive.scan_links([path], check_access=True)
    assert len(findings) == 1 and "example.com/gone" in findings[0].detail


# --- missing code link (nudge) ---------------------------------------------


def test_find_github_repos_extracts_owner_repo_and_strips_suffix():
    text = "Code: https://github.com/pytorch/pytorch\n" "Clone https://github.com/octocat/Hello-World.git\n" "Ignore github.com/features/actions and github.com/settings/keys\n"
    repos = set(sensitive._find_github_repos(text))
    assert ("pytorch", "pytorch") in repos
    assert ("octocat", "Hello-World") in repos  # .git stripped
    assert not any(owner in {"features", "settings"} for owner, _ in repos)


def test_missing_code_link_warns_when_no_repo(tmp_path):
    p = tmp_path / "paper.tex"
    p.write_text("A manuscript with no repository link.\n")
    findings = sensitive.scan_missing_code_link([p])
    assert [f.category for f in findings] == ["missing code link"]


def test_missing_code_link_silent_when_repo_present(tmp_path):
    p = tmp_path / "paper.tex"
    p.write_text("Code at https://github.com/acme/bundled-repo\n")
    assert sensitive.scan_missing_code_link([p]) == []


def test_missing_code_link_silent_when_no_text_read(tmp_path):
    # An image-only submission has no readable text, so "no link" must NOT fire.
    img = tmp_path / "fig.png"
    Image.new("RGB", (8, 8), (1, 2, 3)).save(img)
    assert sensitive.scan_missing_code_link([img]) == []


def test_missing_code_link_finds_repo_inside_archive(tmp_path):
    path = tmp_path / "source.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("paper.tex", "See https://github.com/acme/bundled-repo for code.\n")
    assert sensitive.scan_missing_code_link([path]) == []


# --- dispatch / dedup ------------------------------------------------------


def test_scan_paths_dedupes_repeated_paths(tmp_path):
    p = tmp_path / "a.tex"
    p.write_text("key = AKIAIOSFODNN7EXAMPLE\n")
    once = scan_paths([p])
    twice = scan_paths([p, p])
    assert len(once) == len(twice)


def test_scan_file_swallows_unreadable_file(tmp_path):
    missing = tmp_path / "gone.tex"
    # No exception even though the file does not exist.
    assert scan_file(missing) == []


def test_scan_file_on_plain_text_no_findings(tmp_path):
    p = tmp_path / "clean.tex"
    p.write_text("\\documentclass{article}\n\\begin{document}Hi\\end{document}\n")
    assert scan_file(p) == []
