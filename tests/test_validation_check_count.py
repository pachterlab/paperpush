"""The public check count must track the validation code and venue data."""

import subprocess
import sys
from pathlib import Path


def test_documented_validation_check_count_is_current():
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "gen_validation_check_count.py"), "--check"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
