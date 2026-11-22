#!/usr/bin/env python
"""Score `paperpush validate` against real papers whose findings were reviewed by hand.

Each benchmark file (tests/validate_benchmark/*.json) pins a set of papers (an arXiv
id with its version, so the PDF never changes under the benchmark) and lists the
warnings and errors a person confirmed are real for each one. The script downloads
the papers into a cache, runs validate on each with the network checks turned off
(so the score depends on paperpush, not on registries being up), and compares:

* a reported warning or error that no expected entry matches is a **false alarm**;
* an expected entry that nothing reported matches is a **miss**;
* a finding a paper's optional "tolerated" list matches (a known borderline case, kept
  visible with its "why") counts as neither.

It exits 1 when there are more false alarms than ``--max-false-alarms`` (default 0)
or more misses than ``--max-misses`` (default 0), so CI fails when a change brings
back a false alarm fixed earlier or loses a finding. Suggestions, unchecked notes,
and the AI checks are not scored.

    python scripts/validate_benchmark.py                      # every benchmark file
    python scripts/validate_benchmark.py tests/validate_benchmark/arxiv_cs_ai.json -v
    python scripts/validate_benchmark.py --cache ~/.cache/paperpush-benchmark --json out.json

When a paper gets a new finding that is real, add it to the paper's "expected" list
(with a "why" saying what in the paper makes it right); when it is a false alarm, fix
paperpush and add a unit test for it (see tests/test_arxiv_cs_ai_false_positives.py).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCHMARKS = ROOT / "tests" / "validate_benchmark"
# Checks that ask a registry, a search service, or a web server: left out so the score is reproducible.
NETWORK_CHECKS = [
    "integrity.links",
    "integrity.dois",
    "integrity.unmatched_references",
    "integrity.reference_versions",
    "integrity.isbns",
    "integrity.reference_details",
    "integrity.journal_indexing",
    "integrity.retractions",
    "integrity.openreview",
    "integrity.plagiarism",
    "rigor.rrids",
]
SCORED_LEVELS = {"error", "likely_error", "warning"}
USER_AGENT = "paperpush-validate-benchmark (https://github.com/pachterlab/paperpush)"


def _download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 -- fixed https URLs
                data = response.read()
            break
        except OSError:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    if not data.startswith(b"%PDF"):
        raise RuntimeError(f"{url} did not return a PDF")
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    time.sleep(3)  # arXiv asks automated clients to pace their requests
    return dest


def _pdf(paper: dict, cache: Path) -> Path:
    arxiv_id = paper["arxiv"]
    return _download(f"https://arxiv.org/pdf/{arxiv_id}", cache / f"{arxiv_id.replace('/', '_')}.pdf")


def _europepmc_docx(paper: dict, cache: Path) -> Path:
    """A preprint's Europe PMC full text (JATS) as a Word file: bioRxiv refuses scripted PDF downloads."""
    ppr = paper["europepmc"]
    out = cache / f"{ppr}.docx"
    if out.exists() and out.stat().st_size > 0:
        return out
    xml = cache / f"{ppr}.xml"
    if not xml.exists():
        cache.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{ppr}/fullTextXML", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 -- fixed https URL
            data = response.read()
        if b"<article" not in data[:2000]:
            raise RuntimeError(f"Europe PMC returned no full text for {ppr}")
        xml.write_bytes(data)
        time.sleep(1)
    subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "jats_to_docx.py"), str(xml), str(out)], check=True, timeout=300)
    return out


def _manuscript(paper: dict, cache: Path) -> Path:
    return _europepmc_docx(paper, cache) if "europepmc" in paper else _pdf(paper, cache)


def _id(paper: dict) -> str:
    return paper.get("arxiv") or paper["europepmc"]


def _validate(manuscript: Path, venue: str, timeout: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.json"
        cmd = [sys.executable, "-m", "paperpush.cli", "validate", "--venue", venue, "--manuscript", str(manuscript), "--no-report", "--json", str(out), "--hide-suggestions"]
        cmd += [f"--disable-{check}" for check in NETWORK_CHECKS]
        env = dict(os.environ, PAPERPUSH_VENUE_DATA=os.environ.get("PAPERPUSH_VENUE_DATA", "bundled"))
        subprocess.run(cmd, cwd=tmp, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout, check=False)
        if not out.exists():
            raise RuntimeError(f"validate wrote no report for {manuscript.name}")
        return json.loads(out.read_text("utf-8"))


def _reported(report: dict) -> list[tuple[str, str]]:
    """``(check id, message)`` for each scored issue in a validate JSON report."""
    out = []
    for check in report.get("checks", []):
        if check["id"].startswith("ai."):
            continue
        for issue in check.get("issues", []):
            if issue.get("level") in SCORED_LEVELS and not issue.get("unchecked"):
                out.append((check["id"], issue["message"]))
    return out


def _matches(rule: dict, check: str, message: str) -> bool:
    return check == rule["check"] and all(part in message for part in ([rule["contains"]] if isinstance(rule.get("contains"), str) else rule.get("contains", [])))


def score(paper: dict, report: dict) -> dict:
    reported = _reported(report)
    expected = paper.get("expected", [])
    # Known borderline findings (a variant spelling codespell lists): neither a hit nor a false alarm.
    tolerated = paper.get("tolerated", [])
    unmatched = [(c, m) for c, m in reported if not any(_matches(r, c, m) for r in expected)]
    false_alarms = [f"{c}: {m}" for c, m in unmatched if not any(_matches(r, c, m) for r in tolerated)]
    misses = [f"{r['check']}: {r.get('contains', '')}" for r in expected if not any(_matches(r, c, m) for c, m in reported)]
    return {"paper": _id(paper), "reported": len(reported), "tolerated": len(unmatched) - len(false_alarms), "false_alarms": false_alarms, "misses": misses}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("benchmarks", nargs="*", type=Path, help="benchmark files (default: every tests/validate_benchmark/*.json)")
    ap.add_argument("--cache", type=Path, default=Path(os.environ.get("PAPERPUSH_BENCHMARK_CACHE", Path.home() / ".cache" / "paperpush-benchmark")), help="where downloaded papers are kept")
    ap.add_argument("--max-false-alarms", type=int, default=0)
    ap.add_argument("--max-misses", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=900, help="seconds per validate run")
    ap.add_argument("--json", type=Path, help="write the per-paper results here")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every false alarm and miss")
    args = ap.parse_args(argv)

    files = args.benchmarks or sorted(BENCHMARKS.glob("*.json"))
    results = []
    for path in files:
        bench = json.loads(path.read_text("utf-8"))
        for paper in bench["papers"]:
            try:
                report = _validate(_manuscript(paper, args.cache), bench.get("venue", "arxiv"), args.timeout)
            except Exception as exc:  # noqa: BLE001 -- one unreachable paper should not hide the others' scores
                print(f"{_id(paper)}: could not be scored ({exc})", file=sys.stderr)
                results.append({"paper": _id(paper), "error": str(exc)})
                continue
            result = score(paper, report)
            results.append(result)
            print(f"{_id(paper)}: {result['reported']} reported, {len(result['false_alarms'])} false alarm(s), {len(result['misses'])} miss(es)")
            if args.verbose:
                for line in result["false_alarms"]:
                    print(f"  FALSE ALARM {line[:300]}")
                for line in result["misses"]:
                    print(f"  MISS        {line}")
    scored = [r for r in results if "error" not in r]
    false_alarms = sum(len(r["false_alarms"]) for r in scored)
    misses = sum(len(r["misses"]) for r in scored)
    found = sum(r["reported"] - r["tolerated"] for r in scored) - false_alarms
    precision = found / (found + false_alarms) if found + false_alarms else 1.0
    print(f"\n{len(scored)} paper(s) scored, {len(results) - len(scored)} skipped: {false_alarms} false alarm(s), {misses} miss(es), precision {precision:.0%}")
    if args.json:
        args.json.write_text(json.dumps({"precision": precision, "false_alarms": false_alarms, "misses": misses, "papers": results}, indent=2), "utf-8")
    if not scored:
        return 2
    return 1 if false_alarms > args.max_false_alarms or misses > args.max_misses else 0


if __name__ == "__main__":
    sys.exit(main())
