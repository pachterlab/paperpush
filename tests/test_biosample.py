"""Offline tests for the NCBI BioSample attributes check (paperpush/biosample.py).

PACKAGE_XML is a trimmed copy of
https://www.ncbi.nlm.nih.gov/biosample/docs/packages/Model.organism.animal.1.0/?format=xml
as read on 2026-11-15.
"""

from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path

import pytest

from paperpush import biosample
from paperpush.database import get_venue
from paperpush.validate import ERROR, WARNING, _biosample_attributes_issues

REPO = Path(__file__).resolve().parent.parent
PACKAGE = "Model.organism.animal.1.0"
TODAY = dt.date(2026, 11, 15)
PACKAGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<BioSamplePackages>
  <Package>
    <Name>Model.organism.animal.1.0</Name>
    <DisplayName>Model organism or animal; version 1.0</DisplayName>
    <Attribute use="mandatory"><Name>sample name</Name><HarmonizedName>sample_name</HarmonizedName><Description/><Format><Description/></Format></Attribute>
    <Attribute use="optional"><Name>bioproject accession</Name><HarmonizedName>bioproject_accession</HarmonizedName><Description/><Format><Description/></Format></Attribute>
    <Attribute use="mandatory"><Name>organism</Name><HarmonizedName>organism</HarmonizedName><Description/><Format><Description/></Format></Attribute>
    <Attribute use="either_one_mandatory" group_name="Organism"><Name>strain</Name><HarmonizedName>strain</HarmonizedName><Format><Description/></Format></Attribute>
    <Attribute use="either_one_mandatory" group_name="Organism"><Name>isolate</Name><HarmonizedName>isolate</HarmonizedName><Format><Description/></Format></Attribute>
    <Attribute use="either_one_mandatory" group_name="Age/stage"><Name>age</Name><HarmonizedName>age</HarmonizedName><Format><Description>{float} {unit}</Description></Format></Attribute>
    <Attribute use="either_one_mandatory" group_name="Age/stage"><Name>development stage</Name><HarmonizedName>dev_stage</HarmonizedName><Format><Description>None</Description></Format></Attribute>
    <Attribute use="mandatory"><Name>collection date</Name><HarmonizedName>collection_date</HarmonizedName><Format><Description>{timestamp}</Description></Format></Attribute>
    <Attribute use="mandatory"><Name>geographic location</Name><HarmonizedName>geo_loc_name</HarmonizedName><Format><Description>{term}:{term}:{text}</Description></Format></Attribute>
    <Attribute use="mandatory"><Name>sex</Name><HarmonizedName>sex</HarmonizedName><Format type="select"><Description>male | female</Description><Choice/><Choice>male</Choice><Choice>female</Choice><Choice>pooled male and female</Choice><Choice>not determined</Choice><Choice>missing</Choice><Choice>not applicable</Choice><Choice>not collected</Choice></Format></Attribute>
    <Attribute use="mandatory"><Name>tissue</Name><HarmonizedName>tissue</HarmonizedName><Format><Description/></Format></Attribute>
    <Attribute use="optional"><Name>latitude and longitude</Name><HarmonizedName>lat_lon</HarmonizedName><Format><Description>{float} {float}</Description></Format></Attribute>
  </Package>
</BioSamplePackages>
"""
HEADER = ["*sample_name", "sample_title", "*organism", "strain", "isolate", "age", "dev_stage", "*collection_date", "*geo_loc_name", "*sex", "*tissue", "lat_lon"]
GOOD = [
    ["s1", "Liver 1", "Mus musculus", "C57BL/6J", "", "8 weeks", "", "2025-01-11", "USA: California", "male", "liver", "38.98 N 77.11 W"],
    ["s2", "Brain 2", "Mus musculus", "C57BL/6J", "", "8 weeks", "", "Oct-2024/15-Feb-2025", "Germany", "female", "brain", ""],
]


@pytest.fixture
def package(monkeypatch):
    parsed = biosample.parse_package_xml(PACKAGE_XML, PACKAGE)
    monkeypatch.setattr(biosample, "fetch_package", lambda name: parsed)
    return parsed


def _tsv(tmp_path: Path, rows: list[list[str]], header: list[str] = HEADER, comments: bool = True) -> Path:
    lines = ["# This is a submission template for batch deposit ...", "# Fields with an asterisk (*) are mandatory."] if comments else []
    path = tmp_path / "attributes.tsv"
    path.write_text("\n".join(lines + ["\t".join(r) for r in [header, *rows]]) + "\n", encoding="utf-8")
    return path


def _xlsx(path: Path, rows: list[list], sheet: str = "Sheet1") -> Path:
    """A minimal .xlsx: strings inline, numbers as numbers (as Excel stores a date it converted)."""

    def cell(ref, value):
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"><v>{value}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'

    body = "".join(f'<row r="{i}">' + "".join(cell(f"{chr(65 + j)}{i}", v) for j, v in enumerate(row) if v != "") + "</row>" for i, row in enumerate(rows, 1))
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets><sheet name="Notes" sheetId="1" r:id="rId1"/><sheet name="{sheet}" sheetId="2" r:id="rId2"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        zf.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{main}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>notes</t></is></c></row></sheetData></worksheet>')
        zf.writestr("xl/worksheets/sheet2.xml", f'<worksheet xmlns="{main}"><sheetData>{body}</sheetData></worksheet>')
    return path


def _messages(issues, level=None) -> str:
    return "\n".join(i.message for i in issues if level is None or i.level == level)


# --- links and definitions --------------------------------------------------


def test_links_are_built_from_the_package():
    assert biosample.definition_url(PACKAGE) == "https://submit.ncbi.nlm.nih.gov/biosample/template/?package-0=Model.organism.animal.1.0&action=definition"
    assert biosample.tsv_template_url(PACKAGE) == "https://submit.ncbi.nlm.nih.gov/biosample/template/?package-0=Model.organism.animal.1.0&action=download_tsv"
    assert biosample.excel_template_url(PACKAGE) == "https://www.ncbi.nlm.nih.gov/biosample/docs/templates/packages/Model.organism.animal.1.0.xlsx"
    links = biosample.template_links(PACKAGE)
    assert "action=definition" in links and "download_tsv" in links and ".xlsx" in links


def test_parse_package_xml():
    pkg = biosample.parse_package_xml(PACKAGE_XML, PACKAGE)
    assert [a.name for a in pkg.mandatory] == ["sample_name", "organism", "collection_date", "geo_loc_name", "sex", "tissue"]
    assert {g: [a.name for a in v] for g, v in pkg.groups.items()} == {"Organism": ["strain", "isolate"], "Age/stage": ["age", "dev_stage"]}
    assert pkg.get("sex").choices[:2] == ("male", "female")
    assert pkg.get("dev_stage").format == ""
    # NCBI also accepts the display name as the column header.
    assert pkg.get("collection date").name == "collection_date"
    assert biosample.parse_package_xml("<BioSamplePackages/>", "Nope.1.0") is None


def test_fetch_package_caches_and_falls_back(monkeypatch):
    calls = []
    monkeypatch.setattr(biosample, "_download", lambda url: calls.append(url) or PACKAGE_XML.encode())
    assert biosample.fetch_package(PACKAGE).name == PACKAGE
    assert biosample.fetch_package(PACKAGE).name == PACKAGE
    assert calls == [biosample.package_xml_url(PACKAGE)]  # the second came from the cache

    def offline(url):
        raise OSError("offline")

    monkeypatch.setattr(biosample, "_download", offline)
    monkeypatch.setattr(biosample, "MAX_AGE_SECONDS", -1)  # stale: the download is tried, the cache used
    assert biosample.fetch_package(PACKAGE).name == PACKAGE
    with pytest.raises(OSError):
        biosample.fetch_package("Human.1.0")
    monkeypatch.setattr(biosample, "_download", lambda url: b"<BioSamplePackages/>")
    with pytest.raises(ValueError, match="no BioSample package"):
        biosample.fetch_package("Nope.1.0")
    with pytest.raises(ValueError, match="not a BioSample package id"):
        biosample.fetch_package("../etc")


# --- the rules ------------------------------------------------------------


def test_a_good_file_passes(tmp_path, package):
    issues, pkg = biosample.check_attributes(_tsv(tmp_path, GOOD), PACKAGE, metadata_samples=["s1", "s2"], today=TODAY)
    assert issues == [] and pkg is package


def test_mandatory_columns_and_values(tmp_path, package):
    header = [h for h in HEADER if h != "*tissue"]
    rows = [[v for h, v in zip(HEADER, row) if h != "*tissue"] for row in GOOD]
    rows[1][header.index("*sex")] = ""
    issues, _ = biosample.check_attributes(_tsv(tmp_path, rows, header), PACKAGE, today=TODAY)
    text = _messages(issues, ERROR)
    assert "mandatory column(s) tissue" in text and "download_tsv" in text
    assert "mandatory attribute sex is empty in sample row 2" in text and "not collected" in text


def test_missing_value_terms_are_accepted(tmp_path, package):
    rows = [list(r) for r in GOOD]
    rows[0][HEADER.index("*collection_date")] = "not collected"
    rows[0][HEADER.index("*geo_loc_name")] = "missing: control sample"
    rows[0][HEADER.index("*sex")] = "not applicable"
    issues, _ = biosample.check_attributes(_tsv(tmp_path, rows), PACKAGE, today=TODAY)
    assert issues == []


def test_either_one_groups(tmp_path, package):
    rows = [list(r) for r in GOOD]
    rows[1][HEADER.index("strain")] = ""
    issues, _ = biosample.check_attributes(_tsv(tmp_path, rows), PACKAGE, today=TODAY)
    assert "sample row 2 has no value for any of strain, isolate (the Organism group" in _messages(issues, ERROR)
    header = [h for h in HEADER if h not in ("age", "dev_stage")]
    rows = [[v for h, v in zip(HEADER, row) if h not in ("age", "dev_stage")] for row in GOOD]
    issues, _ = biosample.check_attributes(_tsv(tmp_path, rows, header), PACKAGE, today=TODAY)
    assert "needs at least one of the Age/stage columns (age, dev_stage)" in _messages(issues, ERROR)


@pytest.mark.parametrize(
    ("column", "value", "level", "expected"),
    [
        ("*sex", "boy", ERROR, "sex in sample row 1 ('boy') must be one of: male | female"),
        ("*sex", "Male", None, ""),
        ("*collection_date", "45678", ERROR, "Excel turned into a serial number"),
        ("*collection_date", "2024/03/15", ERROR, "needs a date as YYYY-MM-DD"),
        ("*collection_date", "30-Feb-2024", ERROR, "needs a date"),
        ("*collection_date", "2030-01-01", ERROR, "is in the future"),
        ("*collection_date", "2015-10-11T17:53:03Z", None, ""),
        ("*collection_date", "21-Oct-1952/15-Feb-1953", None, ""),
        ("*geo_loc_name", "United States: Ohio", ERROR, "country or ocean from the INSDC list"),
        ("*geo_loc_name", "usa: Ohio", ERROR, "did you mean 'USA'?"),
        ("*geo_loc_name", "Burma: Yangon", WARNING, "historical name"),
        ("*geo_loc_name", "Cote d’Ivoire", None, ""),
        ("*geo_loc_name", "Pacific Ocean: Hawaii: Oahu", None, ""),
        ("lat_lon", "38.98, -77.11", ERROR, "decimal degrees"),
        ("age", "eight weeks", WARNING, "a number and a unit"),
        ("age", "6-8 weeks", None, ""),
    ],
)
def test_value_formats(tmp_path, package, column, value, level, expected):
    rows = [list(r) for r in GOOD]
    rows[0][HEADER.index(column)] = value
    issues, _ = biosample.check_attributes(_tsv(tmp_path, rows), PACKAGE, today=TODAY)
    if level is None:
        assert issues == []
    else:
        assert expected in _messages(issues, level), _messages(issues)


def test_unique_names_and_distinct_samples(tmp_path, package):
    rows = [list(GOOD[0]), list(GOOD[0]), list(GOOD[0])]
    rows[1][0], rows[1][1] = "s1", "another title"
    rows[2][0], rows[2][1] = "s3", "a third title"
    issues, _ = biosample.check_attributes(_tsv(tmp_path, rows), PACKAGE, today=TODAY)
    text = _messages(issues, ERROR)
    assert "sample_name 's1' appears in sample rows 1, 2" in text
    assert "samples 's1', 's1', 's3' have identical attributes" in text


def test_sra_metadata_samples_must_be_registered(tmp_path, package):
    issues, _ = biosample.check_attributes(_tsv(tmp_path, GOOD), PACKAGE, metadata_samples=["s1", "s9"], today=TODAY)
    assert "no row for the sample_name(s) s9" in _messages(issues, ERROR)
    assert "registers sample(s) s2 that no row of sra_metadata_file uses" in _messages(issues, WARNING)


def test_unloadable_definitions_still_run_the_generic_checks(tmp_path):
    # The suite-wide fixture makes NCBI unreachable.
    rows = [list(GOOD[0]), list(GOOD[0])]
    issues, pkg = biosample.check_attributes(_tsv(tmp_path, rows), PACKAGE, today=TODAY)
    assert pkg is None
    assert "appears in sample rows 1, 2" in _messages(issues, ERROR)
    assert any(i.unchecked and "action=definition" in i.message for i in issues)


# --- reading files ----------------------------------------------------------


def test_xlsx_is_read_like_the_tsv(tmp_path, package):
    rows = [["# This is a submission template ..."], HEADER, *GOOD]
    path = _xlsx(tmp_path / "attributes.xlsx", rows)
    assert biosample.read_table(path, "Notes") == (["notes"], [])
    # With no sheet named, the one with a sample_name column, not the first.
    header, data = biosample.read_table(path)
    assert header[0] == "sample_name" and data[0]["strain"] == "C57BL/6J" and data[1]["lat_lon"] == ""


def test_xlsx_serial_date_and_tsv_export(tmp_path, package):
    rows = [list(r) for r in GOOD]
    rows[0][HEADER.index("*collection_date")] = 45678
    path = _xlsx(tmp_path / "attributes.xlsx", [HEADER, *rows], sheet="Notes2")
    out = biosample.write_tsv(path, tmp_path / "out.tsv")
    lines = out.read_text().splitlines()
    assert lines[0].startswith("*sample_name\tsample_title") and lines[1].split("\t")[HEADER.index("*collection_date")] == "45678"
    issues, _ = biosample.check_attributes(out, PACKAGE, today=TODAY)
    assert "serial number" in _messages(issues, ERROR)


# --- validate ---------------------------------------------------------------


def test_validate_check(tmp_path, package):
    venue = get_venue("sra")
    good = _tsv(tmp_path, GOOD)
    out = _biosample_attributes_issues(venue, {"biosample_package": PACKAGE, "biosample_attributes_file": str(good)})
    assert out.issues == [] and "action=definition" in out.detail and "download_tsv" in out.detail and ".xlsx" in out.detail
    out = _biosample_attributes_issues(venue, {"biosample_package": PACKAGE, "biosample_attributes_file": ""})
    assert "is empty" in out.issues[0].message and "download_tsv" in out.detail
    metadata = tmp_path / "sra_metadata.tsv"
    metadata.write_text("sample_name\tlibrary_ID\ns1\tlib1\ns3\tlib3\n")
    out = _biosample_attributes_issues(venue, {"biosample_package": PACKAGE, "biosample_attributes_file": str(good), "sra_metadata_file": str(metadata)})
    assert "no row for the sample_name(s) s3" in _messages(out.issues, ERROR)


def test_validate_report_runs_the_check(tmp_path, package):
    from paperpush.subfile import parse
    from paperpush.validate import validate_report

    subfile = parse((REPO / "tests/sub_files/sra/sra.sub.txt").read_text(encoding="utf-8"))
    subfile.values["biosample_attributes_file"] = str(_tsv(tmp_path, [list(GOOD[0]), list(GOOD[0])]))
    result = validate_report(subfile, get_venue("sra"), check_manuscript=False, check_links=False, check_references=False)
    check = next(r for r in result.results if r.id == "files.biosample_attributes")
    assert check.status == "failed" and "appears in sample rows 1, 2" in _messages(check.issues)
