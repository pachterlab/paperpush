"""NCBI BioSample packages: template links, attribute definitions, and the attributes-file check.

Each BioSample package (``Model.organism.animal.1.0``, ``Human.1.0``,
``MIMS.me.soil.6.0``, ...) has its own attribute list. NCBI publishes it three ways:

- a page listing the attributes (:func:`definition_url`)
- downloadable TSV and Excel templates (:func:`tsv_template_url`, :func:`excel_template_url`)
- an XML version of the same definitions (:func:`package_xml_url`), which is what
  :func:`fetch_package` reads.

Each attribute is ``mandatory``, ``either_one_mandatory`` (at least one attribute
of its group, such as strain / isolate / breed / cultivar / ecotype, must have a
value), or ``optional``. Some have a fixed list of values (``sex``) or a value
format (``{timestamp}``, ``{float} {unit}``, ``d[d.dddd] N|S d[dd.dddd] W|E``).

:func:`check_attributes` holds a filled attributes file (TSV or Excel) to those
rules before the portal sees it. It also applies the checks that do not depend
on the package: sample names must be unique, and no two samples may have
identical attributes. The definitions are cached for a week under
``~/.cache/paperpush/biosample``; when they cannot be downloaded (and are not
cached), only the package-independent checks run, and the issue list says so.
"""

from __future__ import annotations

import csv
import datetime as _dt
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .validate import ERROR, WARNING, Issue, unchecked

logger = logging.getLogger(__name__)

PACKAGES_URL = "https://www.ncbi.nlm.nih.gov/biosample/docs/packages/"
ATTRIBUTES_URL = "https://www.ncbi.nlm.nih.gov/biosample/docs/attributes/"
GEO_LOC_NAMES_URL = "https://www.insdc.org/submitting-standards/geo_loc_name-qualifier-vocabulary/"
USER_AGENT = "paperpush-validate (+https://github.com/pachterlab/paperpush)"
TIMEOUT = 30
MAX_AGE_SECONDS = 7 * 24 * 3600

FIELD = "biosample_attributes_file"
# Values NCBI accepts in place of any attribute (a mandatory one included).
# INSDC also allows a reason after a colon, e.g. "missing: control sample".
MISSING_VALUES = ("not collected", "not applicable", "missing", "not provided", "restricted access")
# Columns that may differ between otherwise identical samples without making them distinct.
NOT_DISTINGUISHING = {"sample_name", "sample_title", "description", "bioproject_accession"}

# The INSDC geo_loc_name country/ocean vocabulary (revised 2024-07-08).
GEO_LOC_NAMES = frozenset("""Afghanistan|Albania|Algeria|American Samoa|Andorra|Angola|Anguilla|Antarctica|Antigua and Barbuda|Arctic Ocean|Argentina|Armenia|Aruba|Ashmore and Cartier Islands|Atlantic Ocean|Australia|Austria|Azerbaijan|Bahamas|Bahrain|Baltic Sea|Baker Island|Bangladesh|Barbados|Bassas da India|Belarus|Belgium|Belize|Benin|Bermuda|Bhutan|Bolivia|Borneo|Bosnia and Herzegovina|Botswana|Bouvet Island|Brazil|British Virgin Islands|Brunei|Bulgaria|Burkina Faso|Burundi|Cambodia|Cameroon|Canada|Cape Verde|Cayman Islands|Central African Republic|Chad|Chile|China|Christmas Island|Clipperton Island|Cocos Islands|Colombia|Comoros|Cook Islands|Coral Sea Islands|Costa Rica|Cote d'Ivoire|Croatia|Cuba|Curacao|Cyprus|Czechia|Democratic Republic of the Congo|Denmark|Djibouti|Dominica|Dominican Republic|Ecuador|Egypt|El Salvador|Equatorial Guinea|Eritrea|Estonia|Eswatini|Ethiopia|Europa Island|Falkland Islands (Islas Malvinas)|Faroe Islands|Fiji|Finland|France|French Guiana|French Polynesia|French Southern and Antarctic Lands|Gabon|Gambia|Gaza Strip|Georgia|Germany|Ghana|Gibraltar|Glorioso Islands|Greece|Greenland|Grenada|Guadeloupe|Guam|Guatemala|Guernsey|Guinea|Guinea-Bissau|Guyana|Haiti|Heard Island and McDonald Islands|Honduras|Hong Kong|Howland Island|Hungary|Iceland|India|Indian Ocean|Indonesia|Iran|Iraq|Ireland|Isle of Man|Israel|Italy|Jamaica|Jan Mayen|Japan|Jarvis Island|Jersey|Johnston Atoll|Jordan|Juan de Nova Island|Kazakhstan|Kenya|Kerguelen Archipelago|Kingman Reef|Kiribati|Kosovo|Kuwait|Kyrgyzstan|Laos|Latvia|Lebanon|Lesotho|Liberia|Libya|Liechtenstein|Line Islands|Lithuania|Luxembourg|Macau|Madagascar|Malawi|Malaysia|Maldives|Mali|Malta|Marshall Islands|Martinique|Mauritania|Mauritius|Mayotte|Mediterranean Sea|Mexico|Micronesia, Federated States of|Midway Islands|Moldova|Monaco|Mongolia|Montenegro|Montserrat|Morocco|Mozambique|Myanmar|Namibia|Nauru|Navassa Island|Nepal|Netherlands|New Caledonia|New Zealand|Nicaragua|Niger|Nigeria|Niue|Norfolk Island|North Korea|North Macedonia|North Sea|Northern Mariana Islands|Norway|Oman|Pacific Ocean|Pakistan|Palau|Palmyra Atoll|Panama|Papua New Guinea|Paracel Islands|Paraguay|Peru|Philippines|Pitcairn Islands|Poland|Portugal|Puerto Rico|Qatar|Republic of the Congo|Reunion|Romania|Ross Sea|Russia|Rwanda|Saint Barthelemy|Saint Helena|Saint Kitts and Nevis|Saint Lucia|Saint Martin|Saint Pierre and Miquelon|Saint Vincent and the Grenadines|Samoa|San Marino|Sao Tome and Principe|Saudi Arabia|Senegal|Serbia|Seychelles|Sierra Leone|Singapore|Sint Maarten|Slovakia|Slovenia|Solomon Islands|Somalia|South Africa|South Georgia and the South Sandwich Islands|South Korea|South Sudan|Southern Ocean|Spain|Spratly Islands|Sri Lanka|State of Palestine|Sudan|Suriname|Svalbard|Sweden|Switzerland|Syria|Taiwan|Tajikistan|Tanzania|Tasman Sea|Thailand|Timor-Leste|Togo|Tokelau|Tonga|Trinidad and Tobago|Tromelin Island|Tunisia|Turkey|Turkmenistan|Turks and Caicos Islands|Tuvalu|Uganda|Ukraine|United Arab Emirates|United Kingdom|Uruguay|USA|Uzbekistan|Vanuatu|Venezuela|Viet Nam|Virgin Islands|Wake Island|Wallis and Futuna|West Bank|Western Sahara|Yemen|Zambia|Zimbabwe""".split("|"))
# Names INSDC lists as historical: kept for old records, not for new samples.
HISTORICAL_GEO_LOC_NAMES = frozenset("Belgian Congo|British Guiana|Burma|Czechoslovakia|Czech Republic|East Timor|Korea|Macedonia|Micronesia|Netherlands Antilles|Serbia and Montenegro|Siam|Swaziland|The former Yugoslav Republic of Macedonia|USSR|Yugoslavia|Zaire".split("|"))


# --- links -----------------------------------------------------------------


def definition_url(package: str) -> str:
    """NCBI's page listing the package's attributes, what each means, and its value format."""
    return f"https://submit.ncbi.nlm.nih.gov/biosample/template/?package-0={urllib.parse.quote(package)}&action=definition"


def tsv_template_url(package: str) -> str:
    return f"https://submit.ncbi.nlm.nih.gov/biosample/template/?package-0={urllib.parse.quote(package)}&action=download_tsv"


def excel_template_url(package: str) -> str:
    return f"https://www.ncbi.nlm.nih.gov/biosample/docs/templates/packages/{urllib.parse.quote(package)}.xlsx"


def package_xml_url(package: str) -> str:
    return f"https://www.ncbi.nlm.nih.gov/biosample/docs/packages/{urllib.parse.quote(package)}/?format=xml"


def template_links(package: str) -> str:
    """One line naming the package's attribute definitions and its two templates."""
    return f"attribute definitions: {definition_url(package)}; templates: {tsv_template_url(package)} (TSV) or {excel_template_url(package)} (Excel)"


# --- package definitions ---------------------------------------------------


@dataclass(frozen=True)
class Attribute:
    name: str  # the harmonized name, used as the template's column header
    use: str  # "mandatory", "either_one_mandatory", or "optional"
    group: str = ""
    description: str = ""
    format: str = ""  # NCBI's format description, e.g. "{float} {unit}"
    choices: tuple[str, ...] = ()  # the allowed values of a select attribute
    synonyms: tuple[str, ...] = ()  # other column names NCBI maps to this attribute


@dataclass(frozen=True)
class Package:
    name: str
    display_name: str
    attributes: tuple[Attribute, ...]

    @property
    def mandatory(self) -> list[Attribute]:
        return [a for a in self.attributes if a.use == "mandatory"]

    @property
    def groups(self) -> dict[str, list[Attribute]]:
        """The either-one-mandatory groups: at least one attribute of each needs a value."""
        out: dict[str, list[Attribute]] = {}
        for attr in self.attributes:
            if attr.use == "either_one_mandatory":
                out.setdefault(attr.group or attr.name, []).append(attr)
        return out

    def get(self, column: str) -> Attribute | None:
        key = _column_key(column)
        for attr in self.attributes:
            if key == _column_key(attr.name) or key in {_column_key(s) for s in attr.synonyms}:
                return attr
        return None


def parse_package_xml(data: bytes | str, package: str = "") -> Package | None:
    """The package in an NCBI ``BioSamplePackages`` XML document, or None when it names none."""
    from .layout_check import safe_xml

    root = safe_xml(data)
    if root is None:
        return None
    for node in root.iter("Package"):
        name = (node.findtext("Name") or "").strip()
        if package and name != package:
            continue
        attrs = []
        for a in node.iter("Attribute"):
            fmt = a.find("Format")
            choices = tuple(c.text.strip() for c in fmt.iter("Choice") if c.text and c.text.strip()) if fmt is not None else ()
            fmt_text = (fmt.findtext("Description") or "").strip() if fmt is not None else ""
            attrs.append(
                Attribute(
                    name=(a.findtext("HarmonizedName") or a.findtext("Name") or "").strip(),
                    use=a.get("use", "optional"),
                    group=a.get("group_name", ""),
                    description=(a.findtext("Description") or "").strip(),
                    format="" if fmt_text == "None" else fmt_text,
                    choices=choices,
                    synonyms=tuple(s for s in ((a.findtext("Name") or "").strip(),) if s),
                )
            )
        if attrs:
            return Package(name=name, display_name=(node.findtext("DisplayName") or name).strip(), attributes=tuple(attrs))
    return None


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "paperpush" / "biosample"


def _offline() -> bool:
    return os.environ.get("PAPERPUSH_OFFLINE", "").lower() in {"1", "true", "yes"}


def _download(url: str) -> bytes:
    """GET ``url``. Tests patch this; the suite-wide fixture makes it fail."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # nosec B310 -- fixed https NCBI host
        return resp.read()


def fetch_package(package: str, *, offline: bool = False) -> Package:
    """The package's attribute definitions: the cached copy when under a week old, else downloaded.

    Falls back to a stale cached copy when the download fails. Raises
    :class:`OSError` when there is no copy at all, and :class:`ValueError` when
    NCBI does not know the package. ``offline`` (like ``PAPERPUSH_OFFLINE``)
    uses only the cached copy.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]+", package):
        raise ValueError(f"{package!r} is not a BioSample package id")
    path = cache_dir() / f"{package}.xml"
    data = None
    offline = offline or _offline()
    if path.exists() and (offline or time.time() - path.stat().st_mtime < MAX_AGE_SECONDS):
        data = path.read_bytes()
    elif offline:
        raise OSError(f"no cached definition of {package} (network checks are disabled)")
    else:
        try:
            data = _download(package_xml_url(package))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            if not path.exists():
                raise OSError(f"could not download {package_xml_url(package)} ({exc})") from exc
            logger.info("could not refresh the %s definition (%s); using the cached copy", package, exc)
            data = path.read_bytes()
        else:
            if parse_package_xml(data, package) is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                except OSError as exc:  # a read-only home still gets the data
                    logger.info("could not cache the %s definition: %s", package, exc)
    found = parse_package_xml(data, package)
    if found is None:
        raise ValueError(f"NCBI has no BioSample package {package!r} (see {PACKAGES_URL})")
    return found


# --- reading the file ------------------------------------------------------


def _column_key(name: str) -> str:
    return re.sub(r"[\s_]+", "_", name.strip().lstrip("*").strip().lower())


def _xlsx_column(ref: str) -> int:
    index = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        index = index * 26 + ord(ch) - 64
    return index - 1


def _xlsx_number(text: str) -> str:
    try:
        number = float(text)
    except ValueError:
        return text
    return str(int(number)) if number.is_integer() and abs(number) < 1e15 else text


def read_xlsx(path: Path, sheet: str = "") -> list[list[str]]:
    """The rows of one worksheet (``sheet``, else the first) as strings, without openpyxl.

    Numbers come back as written in the cell's stored value, so a date Excel
    converted to a serial number reads as that number (e.g. ``45678``).
    """
    from .layout_check import safe_xml

    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{path.name} is not a readable .xlsx workbook") from exc
    with zf:
        names = set(zf.namelist())
        workbook = safe_xml(zf.read("xl/workbook.xml"))
        rels = safe_xml(zf.read("xl/_rels/workbook.xml.rels"))
        if workbook is None or rels is None:
            raise ValueError(f"{path.name} is not a readable .xlsx workbook")
        targets = {r.get("Id"): r.get("Target", "") for r in rels.iter(f"{rel_ns}Relationship")}
        sheets = [(s.get("name", ""), targets.get(s.get(f"{{{ns['r']}}}id"), "")) for s in workbook.iter(f"{{{ns['m']}}}sheet")]
        if not sheets:
            raise ValueError(f"{path.name} has no worksheets")
        chosen = next((t for n, t in sheets if n == sheet), None) if sheet else sheets[0][1]
        if chosen is None:
            raise ValueError(f"{path.name} has no sheet named {sheet!r} (it has {', '.join(n for n, _ in sheets)})")
        target = chosen.lstrip("/") if chosen.startswith("/") else f"xl/{chosen}"
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sst = safe_xml(zf.read("xl/sharedStrings.xml"))
            if sst is not None:
                shared = ["".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")) for si in sst.iter(f"{{{ns['m']}}}si")]
        data = safe_xml(zf.read(target))
    if data is None:
        raise ValueError(f"{path.name}: could not read the worksheet")
    rows: list[list[str]] = []
    for row in data.iter(f"{{{ns['m']}}}row"):
        cells: dict[int, str] = {}
        for pos, c in enumerate(row.iter(f"{{{ns['m']}}}c")):
            kind, value = c.get("t", "n"), c.findtext("m:v", default="", namespaces=ns)
            if kind == "s":
                text = shared[int(value)] if value.isdigit() and int(value) < len(shared) else ""
            elif kind == "inlineStr":
                text = "".join(t.text or "" for t in c.iter(f"{{{ns['m']}}}t"))
            elif kind == "b":
                text = "TRUE" if value == "1" else "FALSE"
            elif kind in ("str", "e"):
                text = value
            else:
                text = _xlsx_number(value)
            cells[_xlsx_column(c.get("r")) if c.get("r") else pos] = text
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows


def _table_rows(path: Path, sheet: str = "") -> list[list[str]]:
    """The non-blank, non-comment rows of a text or Excel template, cells stripped.

    For an ``.xlsx`` with no ``sheet`` named, the first sheet whose header has
    a sample_name column, else the first sheet.
    """
    if path.suffix.lower() == ".xls":
        raise ValueError(f"{path.name}: the old .xls format cannot be read; save it as .xlsx or tab-delimited text")

    def clean(raw: list[list[str]]) -> list[list[str]]:
        rows = [[cell.strip() for cell in row] for row in raw if any(cell.strip() for cell in row)]
        return [row for row in rows if not row[0].startswith("#")]

    if path.suffix.lower() != ".xlsx":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return clean(list(csv.reader(handle, delimiter="\t")))
    if sheet:
        return clean(read_xlsx(path, sheet))
    first = None
    for name in xlsx_sheets(path):
        rows = clean(read_xlsx(path, name))
        first = rows if first is None else first
        if rows and "sample_name" in {_column_key(c) for c in rows[0]}:
            return rows
    return first or []


def xlsx_sheets(path: Path) -> list[str]:
    """The worksheet names of an ``.xlsx``, in order."""
    from .layout_check import safe_xml

    with zipfile.ZipFile(path) as zf:
        workbook = safe_xml(zf.read("xl/workbook.xml"))
    if workbook is None:
        raise ValueError(f"{path.name} is not a readable .xlsx workbook")
    return [s.get("name", "") for s in workbook.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet")]


def read_table(path: Path, sheet: str = "") -> tuple[list[str], list[dict[str, str]]]:
    """The header and rows of a tab-delimited or Excel template, skipping its ``#`` comment lines.

    Header names keep their spelling except for the leading ``*`` that marks a
    mandatory column. Blank rows are dropped.
    """
    rows = _table_rows(path, sheet)
    if not rows:
        return [], []
    header = [cell.lstrip("*").strip() for cell in rows[0]]
    return header, [dict(zip(header, row + [""] * (len(header) - len(row)))) for row in rows[1:]]


def write_tsv(path: Path, dest: Path, sheet: str = "") -> Path:
    """Save a template (``.xlsx`` or text) as tab-delimited text at ``dest``, for a portal that takes TSV."""
    rows = _table_rows(path, sheet)
    # Excel stores no trailing empty cells: pad each row to the header's width.
    width = len(rows[0]) if rows else 0
    with dest.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, delimiter="\t", lineterminator="\n").writerows(row + [""] * (width - len(row)) for row in rows)
    return dest


# --- value formats ---------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_FLOAT = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def is_missing(value: str) -> bool:
    """Whether ``value`` is one of NCBI's missing-value terms (optionally with a reason after a colon)."""
    return value.strip().lower().split(":")[0].strip() in MISSING_VALUES


def _one_date(text: str) -> _dt.date | None:
    text = text.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:T\d{2}:\d{2}(?::\d{2})?Z?)?", text)
    if m:
        y, mo, d = map(int, m.groups())
    elif m := re.fullmatch(r"(\d{4})-(\d{2})", text):
        y, mo, d = int(m.group(1)), int(m.group(2)), 1
    elif m := re.fullmatch(r"(\d{4})", text):
        y, mo, d = int(text), 1, 1
    elif m := re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", text):
        if m.group(2).lower() not in _MONTHS:
            return None
        y, mo, d = int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))
    elif m := re.fullmatch(r"([A-Za-z]{3})-(\d{4})", text):
        if m.group(1).lower() not in _MONTHS:
            return None
        y, mo, d = int(m.group(2)), _MONTHS[m.group(1).lower()], 1
    else:
        return None
    try:
        return _dt.date(y, mo, d)
    except ValueError:
        return None


def parse_collection_date(value: str) -> list[_dt.date] | None:
    """The date(s) in a ``{timestamp}`` value (two for a ``start/end`` range), or None when unparseable."""
    parts = value.split("/")
    if len(parts) > 2:
        return None
    dates = [_one_date(p) for p in parts]
    return None if any(d is None for d in dates) else dates


def value_problem(attr: Attribute, value: str, today: _dt.date | None = None) -> tuple[str, str] | None:
    """``(level, reason)`` when ``value`` breaks the attribute's choices or format, else None."""
    if not value or is_missing(value):
        return None
    fmt = attr.format
    if attr.choices:
        if value.lower() not in {c.lower() for c in attr.choices}:
            return ERROR, f"must be one of: {' | '.join(attr.choices)}"
        return None
    if attr.name == "bioproject_accession":
        return None if re.fullmatch(r"PRJ[NED][A-Z]{0,2}\d+", value) else (ERROR, "must be a BioProject accession like PRJNA12345")
    if fmt == "{timestamp}" or attr.name == "collection_date":
        if re.fullmatch(r"\d{5}(?:\.\d+)?", value):
            return ERROR, "looks like a date Excel turned into a serial number; format the column as Text and type the date again (e.g. 2024-03-15)"
        dates = parse_collection_date(value)
        if dates is None:
            return ERROR, 'needs a date as YYYY-MM-DD, YYYY-MM, YYYY, DD-Mmm-YYYY or Mmm-YYYY (a range as two dates joined by "/"), e.g. 2024-03-15'
        if any(d > (today or _dt.date.today()) for d in dates):
            return ERROR, "is in the future"
        return None
    if attr.name == "geo_loc_name" or fmt.startswith("{term}:{term}"):
        country = value.split(":")[0].strip().replace("’", "'")
        if country in GEO_LOC_NAMES:
            return None
        if country in HISTORICAL_GEO_LOC_NAMES:
            return WARNING, f"starts with {country!r}, a historical name INSDC keeps only for old records; use the current name ({GEO_LOC_NAMES_URL})"
        close = [c for c in GEO_LOC_NAMES if c.lower() == country.lower()]
        hint = f" (did you mean {close[0]!r}?)" if close else ""
        return ERROR, f'must start with a country or ocean from the INSDC list{hint}, then optionally ": region", e.g. "USA: California" ({GEO_LOC_NAMES_URL})'
    if attr.name == "lat_lon":
        ok = re.fullmatch(rf"\d{{1,2}}(?:\.\d+)? [NS] \d{{1,3}}(?:\.\d+)? [EW]", value)
        return None if ok else (ERROR, 'needs decimal degrees as "d[d.dddd] N|S d[dd.dddd] W|E", e.g. "38.98 N 77.11 W"')
    if fmt == "{float} {unit}":
        return None if re.fullmatch(rf"{_FLOAT}(?:\s*-\s*{_FLOAT})?\s*\S.*", value) else (WARNING, 'is expected as a number and a unit, e.g. "8 weeks"')
    if fmt == "{integer}":
        return None if re.fullmatch(r"[-+]?\d+", value) else (WARNING, "is expected to be a whole number")
    if fmt == "{float}":
        return None if re.fullmatch(_FLOAT, value) else (WARNING, "is expected to be a number")
    if fmt == "{boolean}":
        return None if value.lower() in {"true", "false", "yes", "no"} else (WARNING, "is expected to be yes or no")
    return None


# --- the check -------------------------------------------------------------


def _rows_label(numbers: list[int]) -> str:
    """Name sample rows by their 1-based position after the header (comment lines in the template do not count)."""
    shown = ", ".join(str(n) for n in numbers[:5])
    return f"sample row{'s' if len(numbers) > 1 else ''} {shown}{f' and {len(numbers) - 5} more' if len(numbers) > 5 else ''}"


def generic_issues(header: list[str], rows: list[dict[str, str]]) -> list[Issue]:
    """The rules every attributes file has, whatever the package."""
    issues: list[Issue] = []
    if "sample_name" not in {_column_key(h) for h in header}:
        issues.append(Issue(ERROR, FIELD, "has no sample_name column; start from the package's template, which names every attribute column"))
        return issues
    if not rows:
        issues.append(Issue(ERROR, FIELD, "has a header row but no samples"))
        return issues
    seen: dict[str, int] = {}
    for col in header:
        key = _column_key(col)
        if key in seen and key != "bioproject_accession":
            issues.append(Issue(ERROR, FIELD, f"names the column {col!r} twice"))
        seen[key] = 1
    name_col = next(h for h in header if _column_key(h) == "sample_name")
    names: dict[str, list[int]] = {}
    for n, row in enumerate(rows, 1):
        names.setdefault(row.get(name_col, ""), []).append(n)
    if "" in names:
        issues.append(Issue(ERROR, FIELD, f"sample_name is empty in {_rows_label(names.pop(''))}"))
    for name, at in names.items():
        if len(at) > 1:
            issues.append(Issue(ERROR, FIELD, f"sample_name {name!r} appears in {_rows_label(at)}; every sample needs its own name"))
    distinguishing = [h for h in header if _column_key(h) not in NOT_DISTINGUISHING]
    groups: dict[tuple, list[str]] = {}
    for row in rows:
        groups.setdefault(tuple(row.get(h, "").lower() for h in distinguishing), []).append(row.get(name_col, ""))
    for same in groups.values():
        if len(same) > 1:
            issues.append(Issue(ERROR, FIELD, f"samples {', '.join(repr(s) for s in same[:5])} have identical attributes (sample_title and description do not count); NCBI rejects them, so add an attribute that tells them apart (e.g. replicate, isolate, or a custom column)"))
    return issues


def package_issues(package: Package, header: list[str], rows: list[dict[str, str]], today: _dt.date | None = None) -> list[Issue]:
    """The package's rules: its mandatory columns and values, either-one groups, choices, and formats."""
    issues: list[Issue] = []
    columns: dict[str, str] = {}  # attribute name -> the file's column header
    for col in header:
        attr = package.get(col)
        if attr is not None:
            columns.setdefault(attr.name, col)
    absent = [a.name for a in package.mandatory if a.name not in columns]
    if absent:
        issues.append(Issue(ERROR, FIELD, f"is missing the mandatory column(s) {', '.join(absent)} of {package.name}; start from its template ({tsv_template_url(package.name)})"))
    for group, attrs in package.groups.items():
        if not any(a.name in columns for a in attrs):
            issues.append(Issue(ERROR, FIELD, f"needs at least one of the {group} columns ({', '.join(a.name for a in attrs)})"))
    for attr in package.mandatory:
        col = columns.get(attr.name)
        if col is None:
            continue
        empty = [n for n, row in enumerate(rows, 1) if not row.get(col, "")]
        if empty:
            issues.append(Issue(ERROR, FIELD, f"mandatory attribute {attr.name} is empty in {_rows_label(empty)}; write 'not collected', 'not applicable' or 'missing' when there is no value"))
    for group, attrs in package.groups.items():
        cols = [columns[a.name] for a in attrs if a.name in columns]
        if not cols:
            continue
        empty = [n for n, row in enumerate(rows, 1) if not any(row.get(c, "") for c in cols)]
        if empty:
            issues.append(Issue(ERROR, FIELD, f"{_rows_label(empty)} {'have' if len(empty) > 1 else 'has'} no value for any of {', '.join(a.name for a in attrs if a.name in columns)} (the {group} group needs one per sample)"))
    for name, col in columns.items():
        attr = package.get(name)
        bad: dict[tuple[str, str], list[int]] = {}
        for n, row in enumerate(rows, 1):
            problem = value_problem(attr, row.get(col, ""), today)
            if problem:
                bad.setdefault(problem, []).append(n)
        for (level, reason), at in bad.items():
            example = rows[at[0] - 1].get(col, "")
            issues.append(Issue(level, FIELD, f"{attr.name} in {_rows_label(at)} ({'e.g. ' if len(at) > 1 else ''}{example!r}) {reason}"))
    return issues


def check_attributes(path: Path, package_name: str, *, metadata_samples: list[str] | None = None, today: _dt.date | None = None, offline: bool = False) -> tuple[list[Issue], Package | None]:
    """Check a filled BioSample attributes file against its package. Returns the issues and the package (None when it could not be loaded).

    ``metadata_samples`` are the sample_name values of the SRA metadata, when
    known: each needs a row here. ``offline`` uses only a cached copy of the
    package's definitions.
    """
    header, rows = read_table(path)
    issues = generic_issues(header, rows)
    package = None
    try:
        package = fetch_package(package_name, offline=offline)
    except ValueError as exc:
        issues.append(Issue(ERROR, "biosample_package", str(exc)))
    except OSError as exc:
        issues.append(unchecked(FIELD, f"could not load the {package_name} attribute definitions ({exc}); its mandatory attributes and value formats were not checked. Check them against {definition_url(package_name)}"))
    if package is not None and header and rows:
        issues += package_issues(package, header, rows, today)
    if metadata_samples is not None and header:
        name_col = next((h for h in header if _column_key(h) == "sample_name"), None)
        if name_col:
            listed = {row.get(name_col, "") for row in rows}
            missing = sorted({s for s in metadata_samples if s} - listed)
            unused = sorted(listed - set(metadata_samples) - {""})
            if missing:
                issues.append(Issue(ERROR, FIELD, f"has no row for the sample_name(s) {', '.join(missing[:10])} that sra_metadata_file uses"))
            if unused:
                issues.append(Issue(WARNING, FIELD, f"registers sample(s) {', '.join(unused[:10])} that no row of sra_metadata_file uses; SRA needs data for each sample"))
    return issues, package
