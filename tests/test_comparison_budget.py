from __future__ import annotations

import pytest

from bdencode.worker import (
    COMPARISON_DEADLINE_SECONDS,
    COMPARISON_FRAME_PROBE_TIMEOUT_SECONDS,
    MAX_COMPARISON_BUDGET_SCALE,
    comparison_budget_scale,
)


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (720, 480, 1),
        (1280, 720, 1),
        (1920, 1080, 1),
        (3840, 1636, 3),  # a cropped UHD picture still has to be decoded from the uncropped source
        (3840, 2160, 4),
        (7680, 4320, MAX_COMPARISON_BUDGET_SCALE),  # never an unbounded budget
        (None, None, 1),
        (0, 0, 1),
        (3840, None, 1),
    ],
)
def test_the_comparison_budget_grows_with_the_pixels_to_decode(width, height, expected) -> None:
    assert comparison_budget_scale(width, height) == expected


def test_a_uhd_title_gets_the_uhd_sized_limits() -> None:
    scale = comparison_budget_scale(3840, 2160)
    assert COMPARISON_FRAME_PROBE_TIMEOUT_SECONDS * scale == 1200
    assert COMPARISON_DEADLINE_SECONDS * scale == 7200
    # two probes at the per-command limit always fit into the stage deadline
    assert 2 * COMPARISON_FRAME_PROBE_TIMEOUT_SECONDS * scale < COMPARISON_DEADLINE_SECONDS * scale
