from __future__ import annotations

import json
from decimal import Decimal

import pytest

from bdencode.qc import (
    DEFAULT_VIDEO_COMPLETENESS_TOLERANCE_FRAMES,
    VideoCompletenessError,
    evaluate_video_completeness,
    require_video_completeness,
)


def test_video_completeness_passes_matching_counts_and_duration() -> None:
    verdict = evaluate_video_completeness(
        1000,
        1000,
        fps_numerator=25,
        fps_denominator=1,
        title_duration_seconds=40,
        final_video_duration_seconds=40,
    )

    assert verdict.passed
    assert verdict.packet_count_matches
    assert verdict.duration_matches
    assert verdict.reasons == ()
    assert verdict.frame_duration_seconds == Decimal("0.04")
    assert verdict.reference_duration_seconds == Decimal("40.00")
    assert verdict.maximum_duration_delta_seconds == Decimal("0.08")
    assert verdict.tolerance_frames == DEFAULT_VIDEO_COMPLETENESS_TOLERANCE_FRAMES
    assert json.loads(json.dumps(verdict.to_dict()))["passed"] is True


def test_video_completeness_accepts_exact_two_frame_duration_boundary() -> None:
    verdict = require_video_completeness(
        1000,
        1000,
        fps_numerator=25,
        fps_denominator=1,
        title_duration_seconds=Decimal("40.08"),
        final_video_duration_seconds=Decimal("40.08"),
    )

    assert verdict.passed
    assert verdict.duration_delta_seconds == verdict.maximum_duration_delta_seconds


def test_video_completeness_rejects_duration_beyond_two_frame_boundary() -> None:
    verdict = evaluate_video_completeness(
        1000,
        1000,
        fps_numerator=25,
        fps_denominator=1,
        title_duration_seconds=Decimal("40.080000001"),
        final_video_duration_seconds=40,
    )

    assert not verdict.passed
    assert verdict.packet_count_matches
    assert not verdict.duration_matches
    assert "within 2 frame(s)" in verdict.reasons[0]


def test_video_completeness_rejects_truncated_packet_count_independently() -> None:
    verdict = evaluate_video_completeness(
        1000,
        999,
        fps_numerator=24000,
        fps_denominator=1001,
        title_duration_seconds=Decimal(1000) * Decimal(1001) / Decimal(24000),
        final_video_duration_seconds=(
            Decimal(1000) * Decimal(1001) / Decimal(24000)
        ),
    )

    assert not verdict.passed
    assert not verdict.packet_count_matches
    assert verdict.duration_matches
    assert verdict.reasons == (
        "encoded Matroska video packet count does not match reference frame count: "
        "encoded_packets=999, reference_frames=1000",
    )
    with pytest.raises(VideoCompletenessError, match=r"encoded_packets=999"):
        require_video_completeness(
            1000,
            999,
            fps_numerator=24000,
            fps_denominator=1001,
            title_duration_seconds=verdict.reference_duration_seconds,
            final_video_duration_seconds=verdict.reference_duration_seconds,
        )


def test_video_completeness_rejects_wrong_final_packet_timeline_duration() -> None:
    verdict = evaluate_video_completeness(
        1000,
        1000,
        fps_numerator=25,
        fps_denominator=1,
        title_duration_seconds=40,
        final_video_duration_seconds=20,
    )

    assert not verdict.passed
    assert verdict.duration_matches
    assert not verdict.final_duration_matches
    assert "final Matroska packet-timeline duration" in verdict.reasons[0]


@pytest.mark.parametrize(
    "overrides",
    [
        {"reference_frame_count": 0},
        {"encoded_packet_count": 0},
        {"fps_numerator": 0},
        {"fps_denominator": 0},
        {"title_duration_seconds": "NaN"},
        {"title_duration_seconds": "Infinity"},
        {"final_video_duration_seconds": "NaN"},
        {"final_video_duration_seconds": "Infinity"},
        {"tolerance_frames": -1},
        {"tolerance_frames": True},
    ],
)
def test_require_video_completeness_fails_closed_on_invalid_evidence(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "reference_frame_count": 1000,
        "encoded_packet_count": 1000,
        "fps_numerator": 25,
        "fps_denominator": 1,
        "title_duration_seconds": 40,
        "final_video_duration_seconds": 40,
        "tolerance_frames": 2,
    }
    values.update(overrides)

    with pytest.raises(VideoCompletenessError, match="invalid.*evidence"):
        require_video_completeness(**values)  # type: ignore[arg-type]
