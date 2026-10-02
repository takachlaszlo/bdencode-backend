from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from bdencode import worker

SCRIPT = Path(__file__).parents[1] / "tools" / "e2e" / "synthetic_disc.py"


def test_the_synthetic_disc_names_its_output_the_way_the_worker_requires() -> None:
    """A picture size is not part of the name: the worker ties the name to the disc type."""

    names = re.findall(r'"output_name":\s*"([^"]+)"', SCRIPT.read_text(encoding="utf-8"))
    assert names, "the end-to-end tool no longer sets an output name"
    for name in names:
        assert worker._X265_RELEASE_RE.fullmatch(name) and "_" not in name, name


def test_the_tool_validates_its_picture_options_before_touching_any_tool() -> None:
    for options in (["--size", "huge"], ["--size", "3840x2160", "--scenes", "1"]):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *options], capture_output=True, text=True, check=False
        )
        assert result.returncode == 2, result.stderr
        assert "--size must look like" in result.stderr
