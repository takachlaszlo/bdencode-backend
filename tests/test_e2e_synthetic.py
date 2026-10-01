from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "tools" / "e2e" / "synthetic_disc.py"

pytestmark = pytest.mark.skipif(
    os.environ.get("BDENCODE_E2E") != "1",
    reason="opt-in: set BDENCODE_E2E=1 (needs ffmpeg+x265, mkvmerge, vspipe/BestSource and vmaf; ~8 min)",
)


def test_synthetic_hdr10_disc_runs_to_completed(tmp_path: Path) -> None:
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--work", str(tmp_path / "e2e")],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=3600,
    )
    if result.returncode == 77:
        pytest.skip(result.stderr.strip())
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "FINAL COMPLETED" in result.stdout
