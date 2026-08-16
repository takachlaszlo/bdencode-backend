from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from bdencode.audio import effective_audio_policy
from bdencode.qc.audio import (
    audio_frame_continuity_probe_command,
    compare_audio_frame_continuity,
    parse_audio_frame_continuity,
)


def _frame_document(
    pts_values: tuple[str, ...] = ("10.000", "10.032", "10.064", "10.096"),
    *,
    samples_per_frame: int = 1536,
    counted_frames: int | None = None,
) -> dict[str, object]:
    return {
        "streams": [
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "eac3",
                "sample_rate": "48000",
                "time_base": "1/1000",
                "nb_read_frames": str(
                    len(pts_values) if counted_frames is None else counted_frames
                ),
            }
        ],
        "frames": [
            {
                "media_type": "audio",
                "stream_index": 1,
                "pts_time": pts,
                "nb_samples": samples_per_frame,
                "sample_rate": "48000",
            }
            for pts in pts_values
        ],
    }


def test_audio_frame_probe_is_full_decode_counted_and_codec_aware() -> None:
    command = audio_frame_continuity_probe_command(
        Path("input.eac3"), input_codec="E-AC-3"
    )

    assert command[:6] == [
        "ffprobe",
        "-v",
        "error",
        "-drc_scale",
        "0",
        "-select_streams",
    ]
    assert "-count_frames" in command
    assert "-show_frames" in command
    assert "-show_streams" in command
    entries = command[command.index("-show_entries") + 1]
    assert "nb_read_frames" in entries
    assert "pts_time" in entries
    assert "nb_samples" in entries
    assert "sample_rate" in entries
    assert command[-1] == "input.eac3"


def test_audio_frame_parser_builds_continuous_normalized_sample_cursor() -> None:
    evidence = parse_audio_frame_continuity(_frame_document())

    assert evidence.continuous
    assert evidence.frame_count == 4
    assert evidence.first_pts_seconds == Decimal("10.000")
    assert evidence.normalized_end_seconds == Decimal("0.128")
    assert evidence.total_samples == 6144
    assert evidence.timestamp_tolerance_samples == 48
    assert evidence.discontinuity_frame_indexes == ()


def test_audio_frame_parser_rejects_truncated_counted_evidence() -> None:
    with pytest.raises(ValueError, match="frame count mismatch"):
        parse_audio_frame_continuity(_frame_document(counted_frames=5))


def test_internal_gap_and_overlap_fail_with_same_endpoint_and_total_samples() -> None:
    source = parse_audio_frame_continuity(_frame_document())
    # Frame 1 moves forward by exactly one E-AC-3 frame, then frame 2 returns
    # to the original timeline.  The track endpoint and payload sample count
    # are unchanged, but there is one internal gap followed by one overlap.
    broken = parse_audio_frame_continuity(
        _frame_document(("10.000", "10.064", "10.064", "10.096"))
    )
    policy = effective_audio_policy(
        "eac3",
        source_codec="truehd",
        source_channels=8,
        source_sample_rate=48_000,
    )

    verdict = compare_audio_frame_continuity(source, broken, policy)

    assert broken.gap_count == 1
    assert broken.overlap_count == 1
    assert broken.total_samples == source.total_samples
    assert broken.normalized_end_seconds == source.normalized_end_seconds
    assert verdict.total_sample_delta == 0
    assert verdict.normalized_end_delta_seconds == 0
    assert verdict.total_samples_within_tolerance
    assert verdict.normalized_end_within_tolerance
    assert not verdict.encoded_continuous
    assert not verdict.passed


def test_lossy_total_sample_padding_is_bounded_by_codec_policy() -> None:
    source = parse_audio_frame_continuity(_frame_document())
    one_frame_padded = parse_audio_frame_continuity(
        _frame_document(("10.000", "10.032", "10.064", "10.096", "10.128"))
    )
    policy = effective_audio_policy(
        "eac3",
        source_codec="truehd",
        source_channels=8,
        source_sample_rate=48_000,
    )

    verdict = compare_audio_frame_continuity(source, one_frame_padded, policy)

    assert verdict.tolerance_samples == 3072
    assert verdict.total_sample_delta == 1536
    assert verdict.passed


def test_each_decoded_frame_must_declare_its_sample_rate() -> None:
    document = _frame_document()
    frames = document["frames"]
    assert isinstance(frames, list)
    assert isinstance(frames[1], dict)
    del frames[1]["sample_rate"]

    with pytest.raises(ValueError, match="sample rate changed or is missing"):
        parse_audio_frame_continuity(document)


@pytest.mark.parametrize("invalid_count", [True, 1536.0])
def test_audio_frame_integer_evidence_is_strict(invalid_count: object) -> None:
    document = _frame_document()
    frames = document["frames"]
    assert isinstance(frames, list)
    assert isinstance(frames[1], dict)
    frames[1]["nb_samples"] = invalid_count

    with pytest.raises(ValueError, match="integer audio-frame evidence|malformed"):
        parse_audio_frame_continuity(document)
