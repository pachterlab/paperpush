"""Tests for autofill: the deterministic core, the API extraction engine, and
the ``paperpush autofill`` CLI.

* Core (``paperpush.autofill``) -- the two gates that protect a ``.sub``
  file from an over-reaching extractor (the role gate, where ``never`` fields are
  never written, and the confidence gate), plus file-path resolution and the
  extraction-schema loader. No network and no model; proposals are supplied
  directly.
* API engine (the API section of ``paperpush.autofill``) -- the network path is exercised
  with a fake ``anthropic`` module injected into ``sys.modules``, and the
  document-reading / prompt-building helpers are tested directly.
* CLI (``paperpush autofill -d <dir> <subfile>``) -- the manual engine reads
  proposed field values from a ``--values`` JSON file (the same file the Claude
  skill produces), so these exercise the full CLI path without any model or
  network; the api engine is driven with a fake SDK.
"""

from __future__ import annotations

import base64
import importlib
import json
import sys
import types
from pathlib import Path

import pytest

from paperpush import subfile
from paperpush.cli import main
from paperpush.database import get_venue, list_venues

# The package exports an `autofill` function that shadows the `autofill` module
# attribute, so reach the module via importlib to test its internals
# unambiguously.
af = importlib.import_module("paperpush.autofill")
# The API extraction engine now lives in the same module (merged from the old
# ``autofill_api``); ``api`` is an alias kept so the engine tests read clearly.
api = af
from paperpush.subfile import parse, render_template, replace_scalar

BIORXIV = get_venue("biorxiv")


# ===========================================================================
# Core (paperpush.autofill)
# ===========================================================================


@pytest.fixture
def skeleton() -> str:
    """A fresh, defaults-filled bioRxiv template to autofill into."""
    return render_template(BIORXIV, fill_defaults=True)


def _extract(*fields, unfilled=None):
    return af.parse_extraction(
        {
            "fields": list(fields),
            "unfilled": unfilled or [],
        }
    )


# --- replace_scalar --------------------------------------------------------


def test_replace_scalar_rewrites_only_the_value(skeleton):
    out = replace_scalar(skeleton, "title", "A New Method")
    assert parse(out).values["title"] == "A New Method"
    # Other scalar fields are untouched.
    assert parse(out).values["previous_doi"] == parse(skeleton).values["previous_doi"]


def test_replace_scalar_leaves_block_fields_alone(skeleton):
    # 'abstract' is a textarea (block field); replace_scalar must not touch it.
    out = replace_scalar(skeleton, "abstract", "should not appear")
    assert "should not appear" not in out


def test_replace_scalar_missing_key_is_noop(skeleton):
    assert replace_scalar(skeleton, "no_such_field", "x") == skeleton


# --- role gate -------------------------------------------------------------


def test_effective_role_reads_explicit_annotation():
    by_id = {f.id: f for f in BIORXIV.fields}
    assert af.effective_role(by_id["title"]) == af.EXTRACT
    assert af.effective_role(by_id["subject_category"]) == af.CLASSIFY
    assert af.effective_role(by_id["manuscript_file"]) == af.FILEMAP
    assert af.effective_role(by_id["license"]) == af.NEVER
    assert af.effective_role(by_id["author_consent"]) == af.NEVER


def test_never_fields_are_left_at_their_default(skeleton):
    result = af.autofill(
        skeleton,
        BIORXIV,
        _extract(
            {"id": "license", "value": "CC-BY", "confidence": "high"},
            {"id": "author_consent", "value": "yes", "confidence": "high"},
        ),
    )
    values = parse(result.text).values
    # Proposals for never-fields are ignored; defaults stand.
    assert values["license"] == "CC-BY-NC-ND"
    assert values["author_consent"] == "no"
    actions = {o.id: o.action for o in result.outcomes}
    assert actions["license"] == af.SKIPPED_POLICY
    assert actions["author_consent"] == af.SKIPPED_POLICY


def test_extract_high_confidence_is_filled_not_flagged(skeleton):
    result = af.autofill(
        skeleton,
        BIORXIV,
        _extract(
            {"id": "title", "value": "A New Method", "confidence": "high"},
        ),
    )
    assert parse(result.text).values["title"] == "A New Method"
    assert [o.action for o in result.outcomes] == [af.FILLED]


def test_classify_is_always_flagged_for_review(skeleton):
    result = af.autofill(
        skeleton,
        BIORXIV,
        _extract(
            {"id": "subject_category", "value": "Bioinformatics", "confidence": "high"},
        ),
    )
    # Written, but flagged: a classification is a judgment call.
    assert parse(result.text).values["subject_category"] == "Bioinformatics"
    assert result.outcomes[0].action == af.REVIEW


def test_unknown_field_is_reported_not_written(skeleton):
    result = af.autofill(
        skeleton,
        BIORXIV,
        _extract(
            {"id": "not_a_field", "value": "x"},
        ),
    )
    assert result.text == skeleton
    assert result.outcomes[0].action == af.UNKNOWN


# --- confidence gate -------------------------------------------------------


def test_below_threshold_is_not_written(skeleton):
    result = af.autofill(
        skeleton,
        BIORXIV,
        _extract(
            {"id": "title", "value": "Tentative", "confidence": "low"},
        ),
        min_confidence="high",
    )
    assert parse(result.text).values["title"] == ""  # template slot stays empty
    assert result.outcomes[0].action == af.SKIPPED_LOW


def test_sub_high_confidence_is_written_but_flagged(skeleton):
    result = af.autofill(
        skeleton,
        BIORXIV,
        _extract(
            {"id": "title", "value": "Tentative", "confidence": "medium"},
        ),
        min_confidence="low",
    )
    assert parse(result.text).values["title"] == "Tentative"
    assert result.outcomes[0].action == af.REVIEW
    assert "medium" in result.outcomes[0].note


def test_empty_value_is_skipped(skeleton):
    result = af.autofill(
        skeleton,
        BIORXIV,
        _extract(
            {"id": "title", "value": "   ", "confidence": "high"},
        ),
    )
    assert result.outcomes[0].action == af.SKIPPED_EMPTY


# --- file path resolution --------------------------------------------------


def test_filemap_resolves_paths_against_manuscript_dir(skeleton, tmp_path):
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 2000)
    result = af.autofill(
        skeleton,
        BIORXIV,
        _extract(
            {"id": "manuscript_file", "value": "paper.pdf", "confidence": "high"},
        ),
        manuscript_dir=tmp_path,
    )
    # The bare name is rewritten to a path that exists from the cwd.
    written = parse(result.text).values["manuscript_file"]
    assert written == str(tmp_path / "paper.pdf")


def test_filemap_filelist_preserves_label_columns(skeleton, tmp_path):
    (tmp_path / "fig1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
    result = af.autofill(
        skeleton,
        BIORXIV,
        _extract(
            {"id": "figure_files", "value": "fig1.png | Figure 1", "confidence": "high"},
        ),
        manuscript_dir=tmp_path,
    )
    written = parse(result.text).values["figure_files"]
    assert written == f"{tmp_path / 'fig1.png'} | Figure 1"


def test_unresolved_path_is_left_for_validation(skeleton, tmp_path):
    result = af.autofill(
        skeleton,
        BIORXIV,
        _extract(
            {"id": "manuscript_file", "value": "missing.pdf", "confidence": "high"},
        ),
        manuscript_dir=tmp_path,
    )
    assert parse(result.text).values["manuscript_file"] == "missing.pdf"
    # validate() flags the missing file as an error.
    assert any(i.is_error and i.field == "manuscript_file" for i in result.issues)


# --- extraction schema loader ----------------------------------------------


def test_parse_extraction_defaults_and_unfilled():
    extraction = af.parse_extraction(
        {
            "fields": [{"id": "title", "value": "T"}],
            "unfilled": [{"id": "license", "reason": "policy"}],
        }
    )
    prop = extraction.fields[0]
    assert prop.confidence == af.DEFAULT_CONFIDENCE  # missing confidence -> default
    assert prop.source == ""
    assert extraction.unfilled == [("license", "policy")]


def test_autofill_does_not_mutate_input_text(skeleton):
    before = skeleton
    af.autofill(
        skeleton,
        BIORXIV,
        _extract(
            {"id": "title", "value": "X", "confidence": "high"},
        ),
    )
    assert skeleton == before


# ===========================================================================
# API engine (the API section of paperpush.autofill)
# ===========================================================================


# --- document reading ------------------------------------------------------


def test_field_brief_excludes_never_fields():
    brief, ids = api._field_brief(BIORXIV)
    assert "title" in ids and "subject_category" in ids
    assert "license" not in ids and "author_consent" not in ids
    # classify fields carry their options for the model to choose from.
    cat = next(e for e in brief if e["id"] == "subject_category")
    assert "Bioinformatics" in cat["options"]


def test_pdf_becomes_base64_document_block(tmp_path):
    pdf = tmp_path / "manuscript.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 1500)
    blocks = api._document_blocks([api.DocumentInput("manuscript", pdf)])
    assert blocks[0]["type"] == "document"
    assert blocks[0]["source"]["media_type"] == "application/pdf"
    # data round-trips back to the file bytes (and carries no newlines).
    decoded = base64.standard_b64decode(blocks[0]["source"]["data"])
    assert decoded == pdf.read_bytes()
    assert "\n" not in blocks[0]["source"]["data"]


def test_text_source_is_read_inline(tmp_path):
    tex = tmp_path / "main.tex"
    tex.write_text("\\title{A New Method}\n", encoding="utf-8")
    blocks = api._document_blocks([api.DocumentInput("manuscript", tex)])
    assert blocks[0]["type"] == "text"
    assert "A New Method" in blocks[0]["text"]


def test_docx_text_extraction(samples):
    docx = samples.document("title_page", ".docx", text="Ada Lovelace, Example University")
    text = api._docx_text(docx)
    assert "Ada Lovelace" in text


def test_missing_document_raises(tmp_path):
    with pytest.raises(api.AutofillApiError):
        api._document_blocks([api.DocumentInput("manuscript", tmp_path / "nope.pdf")])


# --- network path with a fake anthropic module -----------------------------


class _FakeMessages:
    def __init__(self, payload, stop_reason="end_turn"):
        self._payload = payload
        self._stop_reason = stop_reason
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        block = types.SimpleNamespace(type="text", text=json.dumps(self._payload))
        usage = types.SimpleNamespace(input_tokens=10, output_tokens=20)
        return types.SimpleNamespace(content=[block], stop_reason=self._stop_reason, stop_details=None, usage=usage)


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Install a fake ``anthropic`` module; tests set its canned response."""
    module = types.ModuleType("anthropic")
    holder = {}

    class APIError(Exception):
        pass

    class Anthropic:
        def __init__(self, *a, **k):
            self.messages = holder["messages"]

    module.APIError = APIError
    module.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return holder


def _docs(tmp_path):
    pdf = tmp_path / "manuscript.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 1500)
    return [api.DocumentInput("manuscript", pdf)]


def test_extract_via_api_returns_extraction(fake_anthropic, tmp_path):
    fake_anthropic["messages"] = _FakeMessages(
        {
            "fields": [
                {"id": "title", "value": "A New Method", "confidence": "high", "source": "p.1"},
                {"id": "subject_category", "value": "Bioinformatics", "confidence": "medium", "source": "abstract"},
            ],
            "unfilled": [{"id": "funding", "reason": "no statement found"}],
        }
    )
    extraction = api.extract_via_api(BIORXIV, _docs(tmp_path), ["manuscript.pdf"])
    ids = {p.id: p for p in extraction.fields}
    assert ids["title"].value == "A New Method"
    assert ids["subject_category"].confidence == "medium"
    assert extraction.unfilled == [("funding", "no statement found")]
    # The request forces JSON via output_config and uses the default model.
    kwargs = fake_anthropic["messages"].last_kwargs
    assert kwargs["model"] == "claude-opus-4-8"
    assert kwargs["output_config"]["format"]["type"] == "json_schema"


def test_extract_via_api_refusal_raises(fake_anthropic, tmp_path):
    fake_anthropic["messages"] = _FakeMessages({"fields": [], "unfilled": []}, stop_reason="refusal")
    with pytest.raises(api.AutofillApiError):
        api.extract_via_api(BIORXIV, _docs(tmp_path), ["manuscript.pdf"])


def test_missing_api_key_raises(fake_anthropic, monkeypatch, tmp_path):
    fake_anthropic["messages"] = _FakeMessages({"fields": [], "unfilled": []})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(api.AutofillApiError, match="API key"):
        api.extract_via_api(BIORXIV, _docs(tmp_path), ["manuscript.pdf"])


def test_missing_anthropic_dependency_raises(monkeypatch, tmp_path):
    # Ensure no anthropic module is importable for this test.
    monkeypatch.setitem(sys.modules, "anthropic", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with pytest.raises(api.AutofillApiError, match="anthropic"):
        api.extract_via_api(BIORXIV, _docs(tmp_path), ["manuscript.pdf"])


# ===========================================================================
# CLI (paperpush autofill / schema)
# ===========================================================================


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def manuscript_dir(in_tmp) -> Path:
    d = in_tmp / "ms"
    d.mkdir()
    (d / "paper.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 2000 + b"\n/Type /Page\n%%EOF")
    (d / "fig1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
    return d


def _write_values(path: Path, **extra) -> Path:
    payload = {
        "fields": [
            {"id": "title", "value": "A New Method", "confidence": "high", "source": "p.1"},
            {"id": "abstract", "value": "We present a method.", "confidence": "high"},
            {"id": "subject_category", "value": "Bioinformatics", "confidence": "medium"},
            {"id": "manuscript_file", "value": "paper.pdf", "confidence": "high"},
            {"id": "figure_files", "value": "fig1.png | Figure 1", "confidence": "high"},
            {"id": "license", "value": "CC-BY"},  # never -> ignored
        ],
        "unfilled": [{"id": "data_availability_links", "reason": "none found"}],
    }
    payload.update(extra)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --- manual engine ---------------------------------------------------------


def test_autofill_creates_and_fills_subfile(in_tmp, manuscript_dir):
    values = _write_values(in_tmp / "values.json")
    rc = main(["autofill", "biorxiv.sub", "-d", str(manuscript_dir), "--values", str(values)])
    assert rc == 0

    out = in_tmp / "biorxiv.sub"
    assert out.exists()
    parsed = subfile.load(out)
    assert parsed.venue == "biorxiv"
    assert parsed.values["title"] == "A New Method"
    assert parsed.values["subject_category"] == "Bioinformatics"
    # File path resolved against the manuscript directory.
    assert parsed.values["manuscript_file"] == str(manuscript_dir / "paper.pdf")
    # never-field proposal ignored; template default preserved.
    assert parsed.values["license"] == "CC-BY-NC-ND"
    assert parsed.values["author_consent"] == "no"


def test_autofill_summary_groups_outcomes(in_tmp, manuscript_dir, capsys):
    values = _write_values(in_tmp / "values.json")
    main(["autofill", "biorxiv.sub", "-d", str(manuscript_dir), "--values", str(values)])
    out = capsys.readouterr().out
    assert "Filled" in out
    assert "need your review" in out  # subject_category + authors confidence
    assert "Left for you to set" in out  # license + data_availability_links
    assert "license" in out


def test_autofill_dry_run_writes_nothing(in_tmp, manuscript_dir, capsys):
    values = _write_values(in_tmp / "values.json")
    rc = main(["autofill", "biorxiv.sub", "-d", str(manuscript_dir), "--values", str(values), "--dry-run"])
    assert rc == 0
    assert not (in_tmp / "biorxiv.sub").exists()
    assert "dry run" in capsys.readouterr().out


def test_autofill_fills_existing_subfile_in_place(in_tmp, manuscript_dir):
    # Pre-create a skeleton, then autofill into it.
    main(["subfile", "biorxiv", "--fill-defaults"])
    values = _write_values(in_tmp / "values.json")
    rc = main(["autofill", "biorxiv.sub", "-d", str(manuscript_dir), "--values", str(values)])
    assert rc == 0
    assert subfile.load(in_tmp / "biorxiv.sub").values["title"] == "A New Method"


def test_autofill_output_flag_and_force(in_tmp, manuscript_dir):
    values = _write_values(in_tmp / "values.json")
    target = in_tmp / "out.sub"
    assert main(["autofill", "biorxiv.sub", "-d", str(manuscript_dir), "--values", str(values), "-o", str(target)]) == 0
    assert target.exists()
    # Refuses to overwrite without --force.
    rc = main(["autofill", "biorxiv.sub", "-d", str(manuscript_dir), "--values", str(values), "-o", str(target)])
    assert rc == 1
    assert main(["autofill", "biorxiv.sub", "-d", str(manuscript_dir), "--values", str(values), "-o", str(target), "--force"]) == 0


def test_autofill_min_confidence_high_skips_medium(in_tmp, manuscript_dir):
    values = _write_values(in_tmp / "values.json")
    main(["autofill", "biorxiv.sub", "-d", str(manuscript_dir), "--values", str(values), "--min-confidence", "high"])
    parsed = subfile.load(in_tmp / "biorxiv.sub")
    # subject_category was medium confidence -> not written under a high floor.
    assert parsed.values["subject_category"] == ""
    # title was high confidence -> still written.
    assert parsed.values["title"] == "A New Method"


def test_autofill_missing_directory_errors(in_tmp):
    values = _write_values(in_tmp / "values.json")
    rc = main(["autofill", "biorxiv.sub", "-d", "no_such_dir", "--values", str(values)])
    assert rc == 1


def test_autofill_unknown_venue_errors(in_tmp, manuscript_dir):
    values = _write_values(in_tmp / "values.json")
    rc = main(["autofill", "nonsense.sub", "-d", str(manuscript_dir), "--values", str(values)])
    assert rc == 2


def test_autofill_manual_requires_values(in_tmp, manuscript_dir):
    rc = main(["autofill", "biorxiv.sub", "-d", str(manuscript_dir)])
    assert rc == 1


@pytest.mark.parametrize("venue", list_venues(), ids=[j.slug for j in list_venues()])
def test_autofill_command_for_each_venue(venue, in_tmp, manuscript_dir):
    """The manual engine runs for every venue, creating that venue's .sub.

    An empty proposals payload keeps this independent of each venue's field
    set: autofill deliberately leaves policy fields empty and reports residual
    validation as guidance, so it still exits 0 and writes the file.
    """
    values = in_tmp / "values.json"
    values.write_text(json.dumps({"fields": [], "unfilled": []}), encoding="utf-8")
    rc = main(["autofill", f"{venue.slug}.sub", "-d", str(manuscript_dir), "--values", str(values)])
    assert rc == 0
    out = in_tmp / f"{venue.slug}.sub"
    assert out.exists()
    assert subfile.load(out).venue == venue.slug


# --- api engine ------------------------------------------------------------


def _install_fake_anthropic(monkeypatch, payload):
    """Inject a fake anthropic SDK that returns ``payload`` as JSON."""
    module = types.ModuleType("anthropic")

    class APIError(Exception):
        pass

    class _Messages:
        def create(self, **kwargs):
            block = types.SimpleNamespace(type="text", text=json.dumps(payload))
            usage = types.SimpleNamespace(input_tokens=10, output_tokens=20)
            return types.SimpleNamespace(content=[block], stop_reason="end_turn", stop_details=None, usage=usage)

    class Anthropic:
        def __init__(self, *a, **k):
            self.messages = _Messages()

    module.APIError = APIError
    module.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def test_autofill_api_engine_end_to_end(in_tmp, manuscript_dir, monkeypatch):
    _install_fake_anthropic(
        monkeypatch,
        {
            "fields": [
                {"id": "title", "value": "A New Method", "confidence": "high", "source": "p.1"},
                {"id": "subject_category", "value": "Bioinformatics", "confidence": "medium", "source": "abstract"},
                {"id": "manuscript_file", "value": "paper.pdf", "confidence": "high", "source": "dir"},
                {"id": "license", "value": "CC-BY", "confidence": "high", "source": "guess"},
            ],
            "unfilled": [{"id": "funding", "reason": "none found"}],
        },
    )
    rc = main(["autofill", "biorxiv.sub", "-d", str(manuscript_dir), "--engine", "api", "--manuscript", str(manuscript_dir / "paper.pdf")])
    assert rc == 0
    parsed = subfile.load(in_tmp / "biorxiv.sub")
    assert parsed.values["title"] == "A New Method"
    # filemap path resolved against the manuscript directory.
    assert parsed.values["manuscript_file"] == str(manuscript_dir / "paper.pdf")
    # never-role field ignored even though the model proposed it.
    assert parsed.values["license"] == "CC-BY-NC-ND"


def test_autofill_api_missing_manuscript_errors(in_tmp, monkeypatch, tmp_path):
    _install_fake_anthropic(monkeypatch, {"fields": [], "unfilled": []})
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = main(["autofill", "biorxiv.sub", "-d", str(empty), "--engine", "api"])
    assert rc == 1


def test_autofill_api_without_sdk_errors(in_tmp, manuscript_dir, monkeypatch):
    # No anthropic module importable, but a manuscript is present.
    monkeypatch.setitem(sys.modules, "anthropic", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    rc = main(["autofill", "biorxiv.sub", "-d", str(manuscript_dir), "--engine", "api", "--manuscript", str(manuscript_dir / "paper.pdf")])
    assert rc == 1


# --- schema command --------------------------------------------------------


def test_schema_command_prints_roles(in_tmp, capsys):
    rc = main(["schema", "biorxiv"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["venue"] == "biorxiv"
    roles = {f["id"]: f["role"] for f in data["fields"]}
    assert roles["title"] == "extract"
    assert roles["subject_category"] == "classify"
    assert roles["manuscript_file"] == "filemap"
    assert roles["license"] == "never"
    # classify fields carry their option set for the extractor to choose from.
    cat = next(f for f in data["fields"] if f["id"] == "subject_category")
    assert "Bioinformatics" in cat["options"]


def test_schema_unknown_venue_errors(in_tmp):
    assert main(["schema", "nonsense"]) == 2


# ===========================================================================
# Generic per-venue engine coverage
#
# The tests above pin the gates to bioRxiv's concrete field set, which reads as
# a precise, human-checkable spec. These re-run the same gate logic against
# *every* venue's own fields, so a venue whose roles are misconfigured -- a
# policy field not marked ``never``, a choice field that does not ``classify`` --
# is caught without a bespoke test. Each test discovers a representative field of
# the role it needs from the venue's schema and skips the venue if it has none.
# Assertions are on the :class:`Outcome` action, which the gates compute from
# role + confidence independent of the field's widget, so they hold for any
# venue regardless of how a given field is written into the template.
# ===========================================================================


_ALL_VENUES = list_venues()
_VENUE_IDS = [v.slug for v in _ALL_VENUES]


def _fields_with_role(venue, role):
    return [f for f in venue.fields if af.effective_role(f) == role]


def _defaults_skeleton(venue) -> str:
    return render_template(venue, fill_defaults=True)


def _outcome_for(result, field_id):
    return next(o for o in result.outcomes if o.id == field_id)


@pytest.mark.parametrize("venue", _ALL_VENUES, ids=_VENUE_IDS)
def test_never_fields_are_never_written_for_any_venue(venue):
    """Every ``never``-role field is left at its default on any venue."""
    never = _fields_with_role(venue, af.NEVER)
    if not never:
        pytest.skip(f"{venue.slug} has no never-role fields")
    skeleton = _defaults_skeleton(venue)
    before = parse(skeleton).values
    result = af.autofill(
        skeleton,
        venue,
        _extract(*[{"id": f.id, "value": "PROPOSED", "confidence": "high"} for f in never]),
    )
    values = parse(result.text).values
    actions = {o.id: o.action for o in result.outcomes}
    for f in never:
        assert actions[f.id] == af.SKIPPED_POLICY
        # The proposal is ignored; the template default stands unchanged.
        assert values.get(f.id) == before.get(f.id)


@pytest.mark.parametrize("venue", _ALL_VENUES, ids=_VENUE_IDS)
def test_high_confidence_extract_is_filled_for_any_venue(venue):
    """A high-confidence ``extract`` proposal is filled (not flagged) on any venue."""
    extract = _fields_with_role(venue, af.EXTRACT)
    if not extract:
        pytest.skip(f"{venue.slug} has no extract-role fields")
    field = extract[0]
    result = af.autofill(
        _defaults_skeleton(venue),
        venue,
        _extract({"id": field.id, "value": "A New Value", "confidence": "high"}),
    )
    assert _outcome_for(result, field.id).action == af.FILLED


@pytest.mark.parametrize("venue", _ALL_VENUES, ids=_VENUE_IDS)
def test_classify_is_flagged_for_review_for_any_venue(venue):
    """A ``classify`` proposal is written but flagged for review on any venue."""
    classify = _fields_with_role(venue, af.CLASSIFY)
    if not classify:
        pytest.skip(f"{venue.slug} has no classify-role fields")
    field = classify[0]
    # Use a real option when the field has a closed set, so the write is valid.
    value = field.options[0] if field.options else "Some Category"
    result = af.autofill(
        _defaults_skeleton(venue),
        venue,
        _extract({"id": field.id, "value": value, "confidence": "high"}),
    )
    assert _outcome_for(result, field.id).action == af.REVIEW


@pytest.mark.parametrize("venue", _ALL_VENUES, ids=_VENUE_IDS)
def test_below_threshold_extract_is_not_written_for_any_venue(venue):
    """A proposal under the confidence floor is skipped, not written, on any venue."""
    extract = _fields_with_role(venue, af.EXTRACT)
    if not extract:
        pytest.skip(f"{venue.slug} has no extract-role fields")
    field = extract[0]
    result = af.autofill(
        _defaults_skeleton(venue),
        venue,
        _extract({"id": field.id, "value": "Tentative", "confidence": "low"}),
        min_confidence="high",
    )
    assert _outcome_for(result, field.id).action == af.SKIPPED_LOW


@pytest.mark.parametrize("venue", _ALL_VENUES, ids=_VENUE_IDS)
def test_field_brief_partitions_by_role_for_any_venue(venue):
    """The API brief offers exactly the non-``never`` fields on any venue.

    The brief is what an extractor is shown, so a ``never`` field leaking into
    it (or a fillable field missing from it) is a real bug regardless of venue.
    """
    _brief, ids = api._field_brief(venue)
    ids = set(ids)
    for f in venue.fields:
        if af.effective_role(f) == af.NEVER:
            assert f.id not in ids, f"never-field {f.id!r} leaked into {venue.slug} brief"
        else:
            assert f.id in ids, f"fillable field {f.id!r} missing from {venue.slug} brief"
