"""Tests for ``paperpush subfile <venue>`` and the committed sample files.

This file covers the ``subfile`` command end to end and the ground-truth sample
``.sub`` files it produces:

* the ``subfile`` command for every venue in the database -- the generated
  template parses back to that venue with a slot for every field, fills
  defaults unless told not to, respects ``-o/--output`` and ``--overwrite``
  (and its old name ``--force``), and rejects an
  unknown venue;
* the ``paperpush venues`` discovery listing, which is what points a user at the
  ``subfile`` command in the first place;
* the committed samples in ``tests/sub_files/`` -- each is byte-for-byte what the
  subfile renderer (and therefore the ``subfile`` command) produces today, parses
  with every field present, has its required fields filled, references input
  files that exist, and is reproducible with no orphans.

Regenerate the committed sample files after changing the renderer or the venue
database with ``python tests/sample_subfiles.py``.

Credential/config isolation is handled by the autouse ``_isolate_user_state``
fixture in ``conftest.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import replace

import pytest
import sample_subfiles
from sample_subfiles import REPO_ROOT, scenarios

from paperpush import subfile, venues
from paperpush.cli import main
from paperpush.database import SECTION_TITLES, Field, get_venue, list_venues

VENUES = list_venues()
SLUGS = [j.slug for j in VENUES]


def _has_runner(slug: str) -> bool:
    """True when a venue has a submission runner (is actually submittable).

    A venue may live in the database before its portal automation exists -- e.g.
    a conference stub carrying only metadata. Such a venue has no runner in
    ``SLUG_TO_MODULE`` and cannot be submitted, so it is exempt from the
    sample-.sub coverage guard below.
    """
    try:
        venues.get_runner(slug)
        return True
    except KeyError:
        return False


SCENARIOS = scenarios()
IDS = [s.filename for s in SCENARIOS]

# Field types whose value(s) name a path on disk.
_FILE_TYPES = {"file", "filelist"}


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    """Run with the cwd set to a temp dir so default-named .sub files land there."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- the subfile command, across every venue ------------------------------


@pytest.mark.parametrize("venue", VENUES, ids=SLUGS)
def test_subfile_creates_parseable_template(venue, in_tmp, capsys):
    rc = main(["subfile", venue.slug])
    out = capsys.readouterr().out

    assert rc == 0
    out_path = in_tmp / f"{venue.slug}.sub.txt"
    assert out_path.exists()
    assert f"Created {venue.slug}.sub.txt" in out

    parsed = subfile.load(out_path)
    assert parsed.venue == venue.slug
    # Every field in the venue definition gets a slot in the template.
    for field in venue.fields:
        assert field.id in parsed.values


@pytest.mark.parametrize("venue", VENUES, ids=SLUGS)
def test_subfile_fill_defaults_populates_defaults(venue, in_tmp):
    rc = main(["subfile", venue.slug])
    assert rc == 0

    parsed = subfile.load(in_tmp / f"{venue.slug}.sub.txt")
    for field in venue.fields:
        if field.default is not None and field.type != "boolean":
            assert parsed.values[field.id] == str(field.default)


@pytest.mark.parametrize("venue", VENUES, ids=SLUGS)
def test_subfile_custom_output_and_overwrite(venue, in_tmp):
    target = in_tmp / "custom.sub"
    assert main(["subfile", venue.slug, "-o", str(target)]) == 0
    assert target.exists()

    # Without --overwrite a second write refuses and leaves the file in place.
    assert main(["subfile", venue.slug, "-o", str(target)]) == 1
    # With --overwrite (or its old name --force) it overwrites cleanly.
    assert main(["subfile", venue.slug, "-o", str(target), "--overwrite"]) == 0
    assert main(["subfile", venue.slug, "-o", str(target), "--force"]) == 0
    assert subfile.load(target).venue == venue.slug


def test_subfile_unknown_venue_errors(in_tmp, capsys):
    rc = main(["subfile", "not-a-real-venue"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown venue" in err


def test_venues_lists_supported_venues(capsys):
    rc = main(["venues"])
    out = capsys.readouterr().out

    assert rc == 0
    # One table with a column per attribute; venue types appear in the Type column.
    header = out.splitlines()[0].split()
    assert header == ["Slug", "Venue", "Keywords", "Type", "Link", "Validate", "Submit"]
    assert "Preprint" in out and "Journal" in out
    # Every configured venue slug shows up in the listing.
    for venue in list_venues():
        assert venue.slug in out.lower()


# --- field order: shared sections vs. --venue-order -------------------------


def _ids_in_text(text: str) -> list[str]:
    """Field ids in the order their value slots appear in rendered .sub text."""
    ids = []
    for line in text.splitlines():
        if line and not line.startswith((" ", "#", "@")) and ":" in line:
            ids.append(line.split(":", 1)[0])
    return ids


@pytest.mark.parametrize("venue", VENUES, ids=SLUGS)
def test_template_groups_fields_by_section_in_canonical_order(venue):
    ordered = subfile.ordered_fields(venue)
    assert sorted(f.id for _, f in ordered) == sorted(f.id for f in venue.fields)

    # Sections appear in SECTION_TITLES order, each as one contiguous run.
    runs = [s for i, (s, _) in enumerate(ordered) if i == 0 or ordered[i - 1][0] != s]
    rank = list(SECTION_TITLES)
    assert runs == sorted(runs, key=rank.index)
    assert len(runs) == len(set(runs))

    # The rendered text follows the same order, with one heading per section.
    text = subfile.render_template(venue)
    assert _ids_in_text(text) == [f.id for _, f in ordered]
    for section in runs:
        assert text.count(f"# {SECTION_TITLES[section]}\n") == 1


@pytest.mark.parametrize("venue", VENUES, ids=SLUGS)
def test_venue_order_keeps_the_portal_order(venue):
    text = subfile.render_template(venue, venue_order=True)
    assert _ids_in_text(text) == [f.id for f in venue.fields]
    for title in SECTION_TITLES.values():
        assert f"# {title}\n" not in text


@pytest.mark.parametrize("venue", VENUES, ids=SLUGS)
def test_conditional_field_never_precedes_its_trigger(venue):
    position = {f.id: i for i, (_, f) in enumerate(subfile.ordered_fields(venue))}
    for field in venue.fields:
        if field.required_if in position:
            assert position[field.required_if] < position[field.id], field.id


def test_common_fields_rank_within_a_section():
    # bioRxiv's portal asks for the result type first, but the shared layout
    # leads with files after the type questions and puts the title before the
    # abstract, ahead of the authors.
    ids = [f.id for _, f in subfile.ordered_fields(get_venue("biorxiv"))]
    assert ids.index("result_type") < ids.index("manuscript_file") < ids.index("title") < ids.index("abstract") < ids.index("authors")


def test_explicit_section_overrides_common_fields():
    field = Field(id="title", label="Title", type="text", section="additional")
    assert subfile.field_section(field) == "additional"
    assert subfile.field_section(Field(id="title", label="Title", type="text")) == "metadata"
    assert subfile.field_section(Field(id="no_such_field", label="X", type="text")) == subfile.FALLBACK_SECTION


def test_dependent_moves_after_its_trigger():
    # A dependent ranked into an earlier section than its trigger follows it.
    trigger = Field(id="has_extra", label="Extra?", type="boolean", section="additional")
    dependent = Field(id="extra_detail", label="Detail", type="text", section="type", required_if="has_extra")
    venue = replace(get_venue("biorxiv"), fields=[dependent, Field(id="title", label="Title", type="text"), trigger])
    ordered = subfile.ordered_fields(venue)
    assert [f.id for _, f in ordered] == ["title", "has_extra", "extra_detail"]
    assert ordered[-1][0] == "additional"


def test_common_fields_file_is_well_formed():
    raw = json.loads(subfile.COMMON_FIELDS_PATH.read_text(encoding="utf-8"))
    sections = [k for k in raw if not k.startswith("$")]
    assert set(sections) <= set(SECTION_TITLES)
    # Every ranked id is used by at least one venue, so a typo cannot hide.
    used = {f.id for v in VENUES for f in v.fields}
    assert set(subfile.common_fields()) <= used


def test_every_venue_field_is_placed_deliberately():
    # Fields neither tagged nor ranked silently land in "additional"; keep that
    # deliberate by requiring every venue field to be placed one way or the other.
    unplaced = sorted({f"{v.slug}.{f.id}" for v in VENUES for f in v.fields if f.section is None and f.id not in subfile.common_fields()})
    assert not unplaced, "give these a 'section' in submission_forms.json or rank them in common_fields.json"


def test_subfile_venue_order_flag(in_tmp):
    venue = get_venue("biorxiv")
    assert main(["subfile", "biorxiv"]) == 0
    assert main(["subfile", "biorxiv", "-o", "portal.sub", "--venue-order"]) == 0
    canonical = (in_tmp / "biorxiv.sub.txt").read_text(encoding="utf-8")
    portal = (in_tmp / "portal.sub").read_text(encoding="utf-8")
    assert _ids_in_text(portal) == [f.id for f in venue.fields]
    assert _ids_in_text(canonical) != _ids_in_text(portal)
    # Both orders parse to the same values.
    assert subfile.parse(canonical).values == subfile.parse(portal).values


def test_check_venue_data_rejects_unknown_section(tmp_path):
    from paperpush import database, venue_data

    for name in (venue_data.VENUES_FILE, venue_data.FORMS_FILE, venue_data.REQUIREMENTS_FILE):
        (tmp_path / name).write_text((database.DATABASE_PATH.parent / name).read_text(encoding="utf-8"), encoding="utf-8")
    forms_path = tmp_path / venue_data.FORMS_FILE
    forms = json.loads(forms_path.read_text(encoding="utf-8"))
    forms["biorxiv"]["fields"][0]["section"] = "nonsense"
    forms_path.write_text(json.dumps(forms), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown section"):
        database.check_venue_data(tmp_path)


# --- the options command ---------------------------------------------------


def test_options_lists_a_fields_choices(capsys):
    rc = main(["options", "arxiv.crosslist_categories"])
    out = capsys.readouterr().out

    assert rc == 0
    # One option per line, drawn from the field's options_file.
    lines = out.splitlines()
    field = next(f for f in get_venue("arxiv").fields if f.id == "crosslist_categories")
    assert lines == field.options
    assert "cs.LG (Machine Learning)" in lines


def test_options_requires_venue_dot_field(capsys):
    rc = main(["options", "arxiv"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "VENUE.FIELD" in err


def test_options_unknown_venue_errors(capsys):
    rc = main(["options", "not-a-real-venue.field"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown venue" in err


def test_options_unknown_field_errors(capsys):
    rc = main(["options", "arxiv.not-a-field"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "no field" in err


def test_options_field_without_options_errors(capsys):
    rc = main(["options", "arxiv.title"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no options" in err


def test_template_points_file_backed_options_at_the_command():
    """A field whose options come from a file gets a command hint, not the list."""
    venue = get_venue("arxiv")
    text = subfile.render_template(venue)

    # crosslist_categories is file-backed: hint instead of the (huge) list.
    assert "run 'paperpush options arxiv.crosslist_categories' to list" in text
    # crosslist_archives has inline options: those are still spelled out.
    assert "options: astro-ph | cond-mat | cs" in text


# --- ground-truth checks on the committed sample .sub files -----------------


def _referenced_paths(venue, values: dict[str, str]) -> list[str]:
    """Collect every file path named by file/filelist fields in ``values``."""
    by_id = {f.id: f for f in venue.fields}
    paths: list[str] = []
    for fid, value in values.items():
        field = by_id.get(fid)
        if field is None or field.type not in _FILE_TYPES or not value.strip():
            continue
        if field.type == "file":
            paths.append(value.strip())
        else:  # filelist: "path | ..." per line
            for line in value.splitlines():
                line = line.strip()
                if line:
                    paths.append(line.split("|")[0].strip())
    return paths


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_committed_file_matches_generator(scenario):
    assert scenario.path.exists(), f"{scenario.filename} is missing; regenerate it with " "'python tests/sample_subfiles.py'"
    committed = scenario.path.read_text(encoding="utf-8")
    assert committed == scenario.render(), f"{scenario.filename} is out of date with the subfile renderer; " "regenerate it with 'python tests/sample_subfiles.py'"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_sample_parses_with_every_field(scenario):
    parsed = subfile.load(scenario.path)
    venue = get_venue(scenario.venue)
    assert parsed.venue == scenario.venue
    for field in venue.fields:
        assert field.id in parsed.values, f"missing slot for {field.id}"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_required_fields_are_filled(scenario):
    parsed = subfile.load(scenario.path)
    venue = get_venue(scenario.venue)
    empty = [f.id for f in venue.fields if f.required and not parsed.values.get(f.id, "").strip()]
    assert not empty, f"required field(s) left empty: {empty}"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_referenced_input_files_exist(scenario):
    parsed = subfile.load(scenario.path)
    venue = get_venue(scenario.venue)
    for rel in _referenced_paths(venue, parsed.values):
        path = REPO_ROOT / rel
        assert path.exists(), f"referenced input file is missing: {rel}"
        assert path.stat().st_size > 0, f"referenced input file is empty: {rel}"


def test_no_orphan_sample_files():
    """The samples directory holds exactly the files the generator produces.

    Guards against stale ``.sub`` files left behind when a variant is removed:
    every committed sample must correspond to a current scenario, and no extra
    sample files may linger.
    """
    expected = {s.path.resolve() for s in SCENARIOS}
    on_disk = {p.resolve() for p in sample_subfiles.SAMPLES_DIR.rglob("*.sub.txt")}
    orphans = sorted(str(p.relative_to(sample_subfiles.SAMPLES_DIR)) for p in on_disk - expected)
    assert not orphans, f"orphan sample .sub file(s) not produced by the generator: {orphans}; " "remove them or add a matching scenario"


def test_generator_is_reproducible(tmp_path):
    """Regenerating into a clean directory reproduces the committed files."""
    for path in sample_subfiles.write_all(tmp_path):
        rel = path.relative_to(tmp_path)
        committed = (sample_subfiles.SAMPLES_DIR / rel).read_text("utf-8")
        assert path.read_text("utf-8") == committed


def test_each_variant_differs_from_its_base():
    """Every variant changes at least one field from its base sample.

    A variant either fills an optional field the base leaves empty (e.g.
    bioRxiv's figures/funding) or substitutes an alternative value for a field
    the base already sets (e.g. arXiv's LaTeX source bundle in place of the PDF
    manuscript). Either way it must differ, or it is a pointless duplicate of the
    base.
    """
    base_values = {s.venue: s.values for s in SCENARIOS if not s.variant}
    variants = [s for s in SCENARIOS if s.variant]
    assert variants, "expected at least one variant scenario"
    for s in variants:
        base = base_values[s.venue]
        changed = [k for k, v in s.values.items() if v.strip() != base.get(k, "").strip()]
        assert changed, f"{s.filename} adds nothing over the base sample"


def test_every_venue_has_a_sample_subfile():
    """Guard: each *submittable* venue has at least one committed sample.

    Ties the venue list to the sample scenarios so a venue added to the
    database without a sample .sub (and thus no downstream validate/submit
    coverage) is caught here. Venues with no submission runner yet (e.g. a
    conference stub carrying only metadata) are exempt -- they cannot be
    submitted, so there is nothing for a sample to cover -- see
    :func:`_has_runner`.
    """
    submittable = {slug for slug in SLUGS if _has_runner(slug)}
    venues_with_samples = {s.venue for s in SCENARIOS}
    missing = sorted(submittable - venues_with_samples)
    assert not missing, f"venues with no sample .sub: {missing}; " "add them to tests/sample_subfiles.json"


def test_sample_subfiles_reference_real_scenarios():
    """Sanity check that scenarios resolve to committed files on disk."""
    assert SCENARIOS, "no sample scenarios discovered"
    for scenario in SCENARIOS:
        assert scenario.path.exists(), f"missing committed sample {scenario.relpath}; regenerate with " "'python tests/sample_subfiles.py'"
    # SAMPLES_DIR is referenced so a relocation of the samples root is noticed.
    assert sample_subfiles.SAMPLES_DIR.is_dir()


# ---------------------------------------------------------------------------
# load(): relative upload paths and quoted scalars
# ---------------------------------------------------------------------------


def test_parse_strips_yaml_quotes():
    from paperpush.subfile import parse

    sub = parse("@venue: biorxiv\ncover_letter: 'Cover letter.txt'\ntitle: \"A: B\"\n")
    assert sub.values["cover_letter"] == "Cover letter.txt"
    assert sub.values["title"] == "A: B"


def test_load_resolves_upload_paths_against_sub_folder(tmp_path, monkeypatch):
    from paperpush.subfile import load

    ms = tmp_path / "ms"
    ms.mkdir()
    (ms / "main.pdf").write_bytes(b"%PDF-1.4\n")
    (ms / "Cover letter.txt").write_text("hi", encoding="utf-8")
    sub_path = ms / "nucleic_acids_research.sub"
    sub_path.write_text(
        "@venue: nucleic_acids_research\n"
        "manuscript_file: main.pdf\n"
        "cover_letter: 'Cover letter.txt'\n"
        "figures: |\n  fig1.png\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    values = load(sub_path).values
    assert values["manuscript_file"] == str(ms.resolve() / "main.pdf")
    assert values["cover_letter"] == str(ms.resolve() / "Cover letter.txt")
    assert values["figures"] == str(ms.resolve() / "fig1.png")


def test_load_keeps_cwd_relative_path_that_only_exists_there(tmp_path, monkeypatch):
    from paperpush.subfile import load

    ms = tmp_path / "ms"
    ms.mkdir()
    sub_path = ms / "nucleic_acids_research.sub"
    sub_path.write_text("@venue: nucleic_acids_research\nmanuscript_file: main.pdf\n", encoding="utf-8")
    (tmp_path / "main.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.chdir(tmp_path)
    assert load(sub_path).values["manuscript_file"] == "main.pdf"


def test_load_prefers_the_sub_folder_over_cwd_when_both_have_the_file(tmp_path, monkeypatch):
    from paperpush.subfile import load

    ms = tmp_path / "ms"
    ms.mkdir()
    (ms / "main.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "main.pdf").write_bytes(b"%PDF-1.4\n")
    sub_path = ms / "nucleic_acids_research.sub"
    sub_path.write_text("@venue: nucleic_acids_research\nmanuscript_file: main.pdf\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load(sub_path).values["manuscript_file"] == str(ms.resolve() / "main.pdf")


# --- file suffix: .sub.txt is written, bare .sub is still read -------------


@pytest.mark.parametrize(
    "name, is_sub, stem",
    [
        ("biorxiv.sub.txt", True, "biorxiv"),
        ("biorxiv.sub", True, "biorxiv"),
        ("dir/BIORXIV.SUB.TXT", True, "BIORXIV"),
        ("biorxiv_figures.sub.txt", True, "biorxiv_figures"),
        ("notes.txt", False, "notes"),
        ("paper.pdf", False, "paper"),
    ],
)
def test_subfile_suffix_helpers(name, is_sub, stem):
    assert subfile.is_subfile_name(name) is is_sub
    assert subfile.subfile_stem(name) == stem


def test_with_subfile_suffix_keeps_either_suffix_and_appends_the_new_one():
    assert subfile.with_subfile_suffix("out") == Path("out.sub.txt")
    assert subfile.with_subfile_suffix("out.sub.txt") == Path("out.sub.txt")
    assert subfile.with_subfile_suffix("out.sub") == Path("out.sub")


def test_subfile_writes_sub_txt_and_legacy_sub_still_validates(in_tmp):
    assert main(["subfile", "biorxiv"]) == 0
    assert (in_tmp / "biorxiv.sub.txt").is_file()
    assert not (in_tmp / "biorxiv.sub").exists()
    assert main(["subfile", "biorxiv", "-o", "mine"]) == 0
    assert (in_tmp / "mine.sub.txt").is_file()
    # A file under the old bare .sub suffix is read like any other, without a warning.
    (in_tmp / "biorxiv.sub.txt").rename(in_tmp / "biorxiv.sub")
    assert subfile.load(in_tmp / "biorxiv.sub").venue == "biorxiv"


# --- .sub and .sub.txt are interchangeable ------------------------------------


@pytest.mark.parametrize(("given", "on_disk"), [("biorxiv.sub", "biorxiv.sub.txt"), ("biorxiv.sub.txt", "biorxiv.sub")])
def test_find_subfile_uses_the_other_suffix_when_only_it_exists(tmp_path, given, on_disk):
    from paperpush.subfile import find_subfile

    (tmp_path / on_disk).write_text("@venue: biorxiv\n", encoding="utf-8")
    assert find_subfile(tmp_path / given) == tmp_path / on_disk


def test_find_subfile_prefers_the_exact_name_when_both_exist(tmp_path):
    from paperpush.subfile import find_subfile

    for name in ("biorxiv.sub", "biorxiv.sub.txt"):
        (tmp_path / name).write_text("@venue: biorxiv\n", encoding="utf-8")
        assert find_subfile(tmp_path / name) == tmp_path / name


def test_find_subfile_leaves_missing_and_non_sub_paths_alone(tmp_path):
    from paperpush.subfile import find_subfile

    assert find_subfile(tmp_path / "biorxiv.sub") == tmp_path / "biorxiv.sub"
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    assert find_subfile(tmp_path / "notes") == tmp_path / "notes"


def test_validate_accepts_the_other_suffix(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["subfile", "biorxiv"]) == 0
    assert (tmp_path / "biorxiv.sub.txt").is_file()
    main(["validate", "biorxiv.sub"])
    err = capsys.readouterr().err
    assert "cannot read" not in err


def test_autofill_fills_the_existing_twin_instead_of_creating_a_new_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["subfile", "biorxiv"]) == 0
    ms = tmp_path / "ms"
    ms.mkdir()
    values = tmp_path / "values.json"
    values.write_text(json.dumps({"fields": {}}), encoding="utf-8")
    main(["autofill", "biorxiv.sub", "-p", str(ms), "--engine", "manual", "--values", str(values)])
    assert not (tmp_path / "biorxiv.sub").exists()
