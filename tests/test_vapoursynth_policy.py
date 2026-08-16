from __future__ import annotations

from pathlib import Path

import pytest

from bdencode.vapoursynth import Crop, ReferenceScriptPlan, render_reference_script


def test_detected_crop_is_even_inward_and_dimension_safe() -> None:
    crop = Crop.from_detected_borders(
        left=221,
        right=221,
        top=3,
        bottom=3,
    )

    assert crop == Crop(left=220, right=220, top=2, bottom=2)
    assert crop.output_dimensions(1920, 1080) == (1480, 1076)

    with pytest.raises(ValueError, match="at least 16"):
        Crop(left=954, right=954).output_dimensions(1920, 1080)


def test_reference_script_fails_before_an_unsafe_runtime_crop(tmp_path: Path) -> None:
    plan = ReferenceScriptPlan(
        source=tmp_path / "source.mkv",
        cache_path=tmp_path / "cache",
        script_path=tmp_path / "source.vpy",
        crop=Crop(top=138, bottom=138),
    )

    script = render_reference_script(plan)

    assert "if src.width % 2 or src.height % 2:" in script
    assert "if src.width - 0 < 16 or src.height - 276 < 16:" in script
    assert "core.std.CropRel" in script
