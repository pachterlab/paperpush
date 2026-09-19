"""Venue data published separately from releases (paperpush.venue_data).

Covers the three layers that let a ``venues.json`` fix reach installed copies
without a PyPI release, with the network replaced by a fake that serves a
directory built by ``scripts/build_venue_data.py``:

1. **merging** -- a published venue entry replaces the bundled one only when the
   installed runner can drive it (same field ids and types); new venues, new or
   retyped fields, and venues inheriting from a held-back base keep the bundled
   entry. A published ``manuscript_requirements.json`` entry is used only when
   this version's ``validate`` can apply it (known rule keys and value types).
2. **refreshing** -- the cache is filled from the manifest, verified by hash and
   by loading it, updated incrementally, and left untouched by a bad download;
   an incompatible data format is reported rather than used.
3. **choosing a source** -- auto/remote/bundled/override modes, offline use,
   failed checks, and caches written by another paperpush version.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import time
from pathlib import Path

import pytest

from paperpush import __version__, database, requirements, venue_data
from paperpush.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_build_script():
    spec = importlib.util.spec_from_file_location("build_venue_data", REPO_ROOT / "scripts" / "build_venue_data.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_venue_data = _load_build_script()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _rehash(root: Path) -> None:
    """Rewrite ``root``'s manifest hashes after a test edits its files."""
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["files"] = {p.relative_to(root).as_posix(): venue_data.file_sha256(p) for p in sorted(root.rglob("*")) if p.is_file() and p.name != "manifest.json"}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))


def _edit_json(root: Path, name: str, edit) -> None:
    path = root / name
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data, indent=2))
    _rehash(root)


def _edit_venues(root: Path, edit) -> None:
    _edit_json(root, "venues.json", edit)


def _edit_requirements(root: Path, edit) -> None:
    _edit_json(root, "manuscript_requirements.json", edit)


class FakeServer:
    """Serves a published directory in place of the real HTTPS host."""

    def __init__(self, root: Path):
        self.root = root
        self.requests: list[str] = []
        self.fail = False

    def get(self, url: str, etag=None):
        rel = url[len(venue_data.data_url()) :]
        self.requests.append(rel)
        if self.fail:
            raise OSError("network is unreachable")
        body = (self.root / rel).read_bytes()
        tag = '"' + hashlib.sha256(body).hexdigest() + '"'
        if etag and etag == tag:
            return 304, b"", etag
        return 200, body, tag


@pytest.fixture
def published(tmp_path) -> Path:
    """A freshly built copy of the checkout's venue data, as the workflow would publish it."""
    root = tmp_path / "published"
    build_venue_data.build(root)
    return root


@pytest.fixture
def server(published, tmp_path, monkeypatch):
    """Point paperpush at ``published`` through a fake server, with a private cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("PAPERPUSH_VENUE_DATA", "remote")
    monkeypatch.delenv("PAPERPUSH_OFFLINE", raising=False)
    fake = FakeServer(published)
    monkeypatch.setattr(venue_data, "_http_get", fake.get)
    database.reload()
    yield fake
    database.reload()


# ---------------------------------------------------------------------------
# 1. Merging a published database onto the bundled one
# ---------------------------------------------------------------------------


@pytest.fixture
def bundled() -> dict:
    return json.loads(database.DATABASE_PATH.read_text())


def test_data_only_change_is_taken(bundled):
    published = copy.deepcopy(bundled)
    published["biorxiv"]["fields"][0]["help"] = "Updated help."
    published["biorxiv"]["max_upload_mb"] = 99
    merged = database.merge_published(bundled, published)
    assert merged["biorxiv"]["fields"][0]["help"] == "Updated help."
    assert merged["biorxiv"]["max_upload_mb"] == 99


def test_new_field_holds_back_the_venue_and_its_inheritors(bundled):
    published = copy.deepcopy(bundled)
    published["science"]["fields"].append({"id": "brand_new", "type": "text", "label": "New"})
    published["science_advances"]["description"] = "changed"
    merged = database.merge_published(bundled, published)
    assert merged["science"] == bundled["science"]
    # Its own shape is unchanged, but it would resolve against a held-back base.
    assert merged["science_advances"] == bundled["science_advances"]


def test_retyped_field_is_held_back(bundled):
    published = copy.deepcopy(bundled)
    published["biorxiv"]["fields"][0]["type"] = "text"
    assert database.merge_published(bundled, published)["biorxiv"] == bundled["biorxiv"]


def test_new_venue_needs_a_release(bundled):
    published = copy.deepcopy(bundled)
    published["brand_new_venue"] = copy.deepcopy(bundled["biorxiv"])
    merged = database.merge_published(bundled, published)
    assert "brand_new_venue" not in merged


def test_venue_missing_from_published_keeps_bundled(bundled):
    published = copy.deepcopy(bundled)
    del published["arxiv"]
    assert database.merge_published(bundled, published)["arxiv"] == bundled["arxiv"]


# --- manuscript_requirements.json ------------------------------------------


@pytest.fixture
def bundled_reqs() -> dict:
    return json.loads(requirements.REQUIREMENTS_PATH.read_text())


def test_every_bundled_requirements_entry_is_readable(bundled_reqs):
    """The compatibility check never rejects data this version itself ships."""
    problems = {slug: requirements._entry_problem(slug, entry) for slug, entry in bundled_reqs.items()}
    assert {slug: p for slug, p in problems.items() if p} == {}
    assert requirements.merge_published(bundled_reqs, copy.deepcopy(bundled_reqs)) == bundled_reqs


def test_requirements_rule_change_is_taken(bundled_reqs):
    published = copy.deepcopy(bundled_reqs)
    published["biorxiv"].setdefault("figures", {})["min_dpi"] = 450
    merged = requirements.merge_published(bundled_reqs, published)
    assert merged["biorxiv"]["figures"]["min_dpi"] == 450


def test_requirements_entries_can_be_added_and_dropped(bundled_reqs):
    # No runner depends on these rules, so the published copy decides which
    # entries exist -- unlike venues.json, a new entry needs no release.
    published = copy.deepcopy(bundled_reqs)
    published["future_venue"] = {"figures": {"min_dpi": 300}}
    del published["arxiv"]
    merged = requirements.merge_published(bundled_reqs, published)
    assert merged["future_venue"] == {"figures": {"min_dpi": 300}}
    assert "arxiv" not in merged


def test_null_still_drops_an_inherited_rule(bundled_reqs):
    published = copy.deepcopy(bundled_reqs)
    published["medrxiv"] = {"inherits": "biorxiv", "figures": {"min_dpi": None}}
    merged = requirements.merge_published(bundled_reqs, published)
    assert merged["medrxiv"] == published["medrxiv"]
    resolved = requirements.ManuscriptRequirements.from_dict("medrxiv", requirements._resolve_entry("medrxiv", merged))
    assert resolved.figures.min_dpi is None


@pytest.mark.parametrize(
    "edit, reason",
    [
        (lambda e: e.setdefault("figures", {}).update(max_panels=6), "rule(s) this version does not know: max_panels"),
        (lambda e: e.setdefault("figures", {}).update(min_dpi={"photo": 300}), "does not match this version's rule types"),
        (lambda e: e.update(equations={"numbered": True}), "key(s) this version does not know: equations"),
        (lambda e: e.update(source_urls="https://example.org"), "does not match this version's entry types"),
        (lambda e: e.setdefault("article_types", {}).update({"Note": {"figures": {"max_panels": 2}}}), "rule(s) this version does not know: max_panels"),
    ],
    ids=["new-rule", "retyped-rule", "new-section", "retyped-top-level", "new-rule-in-article-type"],
)
def test_requirements_this_version_cannot_apply_are_held_back(bundled_reqs, caplog, edit, reason):
    published = copy.deepcopy(bundled_reqs)
    edit(published["biorxiv"])
    published["medrxiv"].setdefault("figures", {})["min_dpi"] = 451  # a readable change alongside
    with caplog.at_level("INFO", logger="paperpush.requirements"):
        merged = requirements.merge_published(bundled_reqs, published)
    assert merged["biorxiv"] == bundled_reqs["biorxiv"]
    assert merged["medrxiv"]["figures"]["min_dpi"] == 451
    assert reason in caplog.text
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_requirements_inheriting_from_a_held_back_base_are_held_back(bundled_reqs):
    published = copy.deepcopy(bundled_reqs)
    published["biorxiv"].setdefault("figures", {})["max_panels"] = 6
    published["medrxiv"] = {"inherits": "biorxiv", "notes": ["changed"]}
    merged = requirements.merge_published(bundled_reqs, published)
    assert merged["biorxiv"] == bundled_reqs["biorxiv"]
    assert merged["medrxiv"] == bundled_reqs["medrxiv"]


def test_requirements_inheriting_from_a_missing_base_are_dropped(bundled_reqs):
    published = copy.deepcopy(bundled_reqs)
    published["future_venue"] = {"inherits": "no_such_venue"}
    assert "future_venue" not in requirements.merge_published(bundled_reqs, published)


def test_malformed_aliases_are_held_back(bundled_reqs):
    published = copy.deepcopy(bundled_reqs)
    published["$aliases"] = {"sections": ["methods"]}
    assert requirements.merge_published(bundled_reqs, published)["$aliases"] == bundled_reqs["$aliases"]


# ---------------------------------------------------------------------------
# 2. Refreshing the cache
# ---------------------------------------------------------------------------


def test_build_ships_data_schemas_and_assets(published):
    manifest = json.loads((published / "manifest.json").read_text())
    assert manifest["data_format"] == venue_data.DATA_FORMAT
    for name in venue_data.DATA_FILES:
        assert name in manifest["files"]
        assert manifest["files"][name] == venue_data.file_sha256(database.DATABASE_PATH.parent / name)
    assert "_assets/arxiv_categories.txt" in manifest["files"]


def test_build_rejects_data_that_breaks_its_schema(tmp_path, monkeypatch):
    bad = tmp_path / "pkg"
    (bad / "venues" / "_assets").mkdir(parents=True)
    for name in venue_data.DATA_FILES:
        (bad / name).write_bytes((database.DATABASE_PATH.parent / name).read_bytes())
    data = json.loads((bad / "venues.json").read_text())
    data["biorxiv"]["fields"][0]["type"] = "not-a-type"
    (bad / "venues.json").write_text(json.dumps(data))
    monkeypatch.setattr(build_venue_data, "PACKAGE_DIR", bad)
    monkeypatch.setattr(build_venue_data, "ASSETS_DIR", bad / "venues" / "_assets")
    with pytest.raises(SystemExit, match="does not match its schema"):
        build_venue_data.build(tmp_path / "out")


def test_refresh_fills_the_cache_and_is_used(server, published):
    _edit_venues(published, lambda d: d["biorxiv"].update(max_upload_mb=77))
    result = venue_data.refresh()
    assert result.status == "updated"
    cache = venue_data.cache_dir()
    for name in venue_data.DATA_FILES:
        assert (cache / name).is_file()

    database.reload()
    source = venue_data.active_source()
    assert source.kind == "remote"
    assert source.path("venues.schema.json") == cache / "venues.schema.json"
    assert database.get_venue("biorxiv").max_upload_mb == 77


def test_refresh_is_conditional_and_incremental(server, published):
    venue_data.refresh()
    server.requests.clear()
    # Same manifest: a 304, no file downloads.
    assert venue_data.refresh().status == "current"
    assert server.requests == ["manifest.json"]

    _edit_venues(published, lambda d: d["arxiv"].update(max_upload_mb=5))
    server.requests.clear()
    assert venue_data.refresh().status == "updated"
    assert sorted(server.requests) == ["manifest.json", "venues.json"]


def test_hash_mismatch_leaves_the_cache_untouched(server, published):
    venue_data.refresh()
    before = (venue_data.cache_dir() / "venues.json").read_bytes()
    _edit_venues(published, lambda d: d["biorxiv"].update(max_upload_mb=1))
    (published / "venues.json").write_text("{}")  # tampered after hashing
    with pytest.raises(ValueError, match="does not match its manifest hash"):
        venue_data.refresh(force=True)
    assert (venue_data.cache_dir() / "venues.json").read_bytes() == before


def test_data_that_does_not_load_is_rejected(server, published):
    _edit_venues(published, lambda d: d["science_advances"].update(inherits="no_such_venue"))
    with pytest.raises(KeyError, match="no_such_venue"):
        venue_data.refresh()
    assert not (venue_data.cache_dir() / "venues.json").exists()


@pytest.mark.parametrize("path", ["../escape.json", "_assets/../../x", "other.json"])
def test_manifest_paths_are_restricted(server, published, path):
    manifest = json.loads((published / "manifest.json").read_text())
    manifest["files"][path] = "0" * 64
    (published / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="unexpected path"):
        venue_data.refresh()


def test_incompatible_format_is_reported_not_used(server, published):
    venue_data.refresh()
    manifest = json.loads((published / "manifest.json").read_text())
    manifest["data_format"] = venue_data.DATA_FORMAT + 1
    (published / "manifest.json").write_text(json.dumps(manifest))
    result = venue_data.refresh(force=True)
    assert result.status == "incompatible"
    assert "pip install -U paperpush" in result.message
    # The previously verified cache is still the one in use.
    database.reload()
    assert venue_data.active_source().manifest["data_format"] == venue_data.DATA_FORMAT


def test_published_options_file_is_used(server, published):
    (published / "_assets" / "arxiv_categories.txt").write_text("cs.AI\nq-bio.GN\n")
    _rehash(published)
    venue_data.refresh()
    database.reload()
    field = next(f for f in database.get_venue("arxiv").fields if f.options_file == "arxiv_categories.txt")
    assert field.options == ["cs.AI", "q-bio.GN"]


def test_published_requirements_are_used(server, published, capsys):
    _edit_requirements(published, lambda d: d["biorxiv"].setdefault("upload", {}).update(notes=["published note"]))
    venue_data.refresh()
    database.reload()
    assert "published note" in requirements.get_requirements("biorxiv").upload.notes

    assert main(["requirements", "biorxiv"]) == 0
    out = capsys.readouterr().out
    assert "published note" in out
    assert "venue data: published" in out


def test_requirements_only_change_downloads_only_that_file(server, published):
    venue_data.refresh()
    server.requests.clear()
    _edit_requirements(published, lambda d: d["biorxiv"].setdefault("figures", {}).update(min_dpi=450))
    assert venue_data.refresh().status == "updated"
    assert sorted(server.requests) == ["manifest.json", "manuscript_requirements.json"]
    database.reload()
    assert requirements.get_requirements("biorxiv").figures.min_dpi == 450


def test_published_requirements_the_version_cannot_apply_fall_back(server, published):
    def edit(d):
        d["biorxiv"].setdefault("figures", {})["max_panels"] = 6
        d["medrxiv"].setdefault("figures", {})["min_dpi"] = 451

    _edit_requirements(published, edit)
    assert venue_data.refresh().status == "updated"
    database.reload()
    bundled_biorxiv = json.loads(requirements.REQUIREMENTS_PATH.read_text())["biorxiv"]
    assert requirements.get_requirements("biorxiv").figures.min_dpi == (bundled_biorxiv.get("figures") or {}).get("min_dpi")
    assert requirements.get_requirements("medrxiv").figures.min_dpi == 451


def test_requirements_that_do_not_load_are_rejected(server, published):
    (published / "manuscript_requirements.json").write_text("[]")
    _rehash(published)
    with pytest.raises(ValueError, match="not a JSON object"):
        venue_data.refresh()
    assert not (venue_data.cache_dir() / "manuscript_requirements.json").exists()


# ---------------------------------------------------------------------------
# 3. Choosing a source
# ---------------------------------------------------------------------------


def test_auto_refreshes_once_then_uses_the_cache(server):
    assert venue_data.active_source().kind == "remote"
    assert "manifest.json" in server.requests
    server.requests.clear()
    database.reload()
    assert venue_data.active_source().kind == "remote"
    assert server.requests == []  # not due again for a day


def test_failed_check_falls_back_and_is_not_retried_every_command(server):
    server.fail = True
    assert venue_data.active_source().kind == "bundled"
    server.requests.clear()
    database.reload()
    venue_data.active_source()
    assert server.requests == []


def test_offline_never_fetches(server, monkeypatch):
    monkeypatch.setenv("PAPERPUSH_OFFLINE", "1")
    assert venue_data.active_source().kind == "bundled"
    assert server.requests == []


def test_cache_from_another_version_is_not_trusted(server, monkeypatch):
    venue_data.refresh()
    state_path = venue_data.cache_dir() / ".state.json"
    state = json.loads(state_path.read_text())
    state.update(fetched_by="0.0.1", checked_at=time.time())
    state_path.write_text(json.dumps(state))
    monkeypatch.setenv("PAPERPUSH_OFFLINE", "1")
    database.reload()
    assert venue_data.active_source().kind == "bundled"


def test_bundled_mode_ignores_the_cache(server, monkeypatch):
    venue_data.refresh()
    monkeypatch.setenv("PAPERPUSH_VENUE_DATA", "bundled")
    database.reload()
    assert venue_data.active_source().kind == "bundled"


def test_source_checkout_defaults_to_bundled(server, monkeypatch):
    monkeypatch.setenv("PAPERPUSH_VENUE_DATA", "auto")
    monkeypatch.setattr(venue_data, "_in_source_checkout", lambda: True)
    assert venue_data.active_source().kind == "bundled"
    assert server.requests == []


def test_override_directory(tmp_path, monkeypatch, bundled):
    override = tmp_path / "override"
    override.mkdir()
    data = copy.deepcopy(bundled)
    data["biorxiv"]["max_upload_mb"] = 3
    (override / "venues.json").write_text(json.dumps(data))
    monkeypatch.setenv("PAPERPUSH_VENUE_DATA", str(override))
    database.reload()
    try:
        source = venue_data.active_source()
        assert source.kind == "override"
        assert database.get_venue("biorxiv").max_upload_mb == 3
        # Files the override lacks come from the bundled copy.
        assert source.path("venues.schema.json") == venue_data.PACKAGE_DIR / "venues.schema.json"
    finally:
        monkeypatch.undo()
        database.reload()


def test_non_https_url_is_refused():
    with pytest.raises(ValueError, match="non-HTTPS"):
        venue_data._http_get("http://example.com/manifest.json")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_update_venues_command(server, capsys):
    assert main(["update-venues"]) == 0
    out = capsys.readouterr().out
    assert "venue data updated" in out
    assert "In use: published" in out

    assert main(["update-venues", "--clear"]) == 0
    assert not venue_data.cache_dir().exists()
    assert f"bundled with paperpush {__version__}" in capsys.readouterr().out


def test_update_venues_reports_network_failure(server, capsys):
    server.fail = True
    assert main(["update-venues"]) == 1
    assert "could not fetch" in capsys.readouterr().err


def test_venues_listing_names_the_data_source(capsys):
    assert main(["--venues"]) == 0
    assert "Venue data: bundled with paperpush" in capsys.readouterr().out
