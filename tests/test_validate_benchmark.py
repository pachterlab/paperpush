"""The validate benchmark (scripts/validate_benchmark.py): real papers with hand-reviewed findings.

The offline tests check the benchmark files and the scoring. The benchmark itself downloads
the papers and runs validate on each, so it runs only when asked:

    PAPERPUSH_RUN_BENCHMARK=1 pytest tests/test_validate_benchmark.py

(also run monthly by .github/workflows/validate-benchmark.yml).
"""

import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BENCHMARKS = sorted((ROOT / "tests" / "validate_benchmark").glob("*.json"))
_spec = importlib.util.spec_from_file_location("validate_benchmark", ROOT / "scripts" / "validate_benchmark.py")
V = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(V)
CHECK_IDS = set(json.loads((ROOT / "paperpush" / "validation_check_ids.json").read_text("utf-8")))


@pytest.mark.parametrize("path", BENCHMARKS, ids=lambda p: p.stem)
def test_benchmark_files_are_well_formed(path):
    bench = json.loads(path.read_text("utf-8"))
    ids = [p.get("arxiv") or p.get("europepmc") for p in bench["papers"]]
    assert ids and all(ids) and len(ids) == len(set(ids))
    for paper in bench["papers"]:
        if "arxiv" in paper:
            # A pinned version, so the PDF cannot change under the expected findings.
            assert re.search(r"v\d+$", paper["arxiv"]), paper["arxiv"]
        else:
            assert re.fullmatch(r"PPR\d+", paper["europepmc"]), paper["europepmc"]
        for rule in paper["expected"] + paper.get("tolerated", []):
            assert rule["check"] in CHECK_IDS, rule["check"]
            assert rule["check"] not in V.NETWORK_CHECKS, f"{rule['check']} is turned off in the benchmark"
            assert rule.get("contains") and rule.get("why"), rule


def test_scoring_counts_false_alarms_and_misses():
    paper = {"arxiv": "x", "expected": [{"check": "citations.table", "contains": "Table 4"}, {"check": "prose.acronyms", "contains": ["'LLM'", "never"]}]}
    report = {
        "checks": [
            {"id": "citations.table", "issues": [{"level": "warning", "message": "Table 4 is never cited"}]},
            {"id": "prose.spelling", "issues": [{"level": "warning", "message": "'singl'"}, {"level": "warning", "message": "x", "unchecked": True}]},
            {"id": "prose.dashes", "issues": [{"level": "suggestion", "message": "use an en dash"}]},
            {"id": "ai.legends", "issues": [{"level": "warning", "message": "queued"}]},
        ]
    }
    result = V.score(paper, report)
    assert result["reported"] == 2
    assert result["false_alarms"] == ["prose.spelling: 'singl'"]
    assert result["misses"] == ["prose.acronyms: [\"'LLM'\", 'never']"]


@pytest.mark.skipif(os.environ.get("PAPERPUSH_RUN_BENCHMARK") != "1", reason="downloads papers; set PAPERPUSH_RUN_BENCHMARK=1")
def test_validate_benchmark():
    assert V.main([str(p) for p in BENCHMARKS] + ["-v"]) == 0
