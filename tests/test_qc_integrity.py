from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from bdencode.qc.integrity import (
    VideoEfficiencyError,
    compare_packet_timelines,
    evaluate_video_cadence,
    evaluate_video_efficiency,
    packet_timeline_probe_command,
    parse_packet_timeline,
    parse_video_packet_sizes,
    parse_stream_payload_hash,
    parse_video_stream_hash,
    require_video_efficiency,
    require_video_cadence,
    source_video_integrity_command,
    stream_payload_hash_command,
    video_packet_size_command,
    video_stream_hash_command,
)


def test_source_integrity_command_is_fail_fast_full_decode_with_progress() -> None:
    command = source_video_integrity_command(
        Path("reference.mkv"), stream=2, ffmpeg="/tools/ffmpeg"
    )
    assert command[0] == "/tools/ffmpeg"
    assert command[command.index("-map") + 1] == "0:v:2"
    assert "-c:v" not in command
    assert "copy" not in command
    assert command[command.index("-err_detect") + 1] == "explode"
    assert "-xerror" in command
    assert command[command.index("-f") + 1] == "null"
    assert command[command.index("-progress") + 1] == "pipe:1"
    assert command[command.index("-stats_period") + 1] == "1"
    assert command[-1] == "-"


def test_packet_size_command_selects_one_stream_and_compact_csv() -> None:
    command = video_packet_size_command(
        Path("encode.mkv"), stream=1, ffprobe="/tools/ffprobe"
    )
    assert command == [
        "/tools/ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:1",
        "-show_packets",
        "-show_entries",
        "packet=size",
        "-of",
        "csv=p=0",
        "encode.mkv",
    ]


@pytest.mark.parametrize("stream_type", ["v", "a", "s"])
def test_packet_timeline_command_uses_type_ordinal(stream_type: str) -> None:
    command = packet_timeline_probe_command(
        Path("track.mkv"),
        stream_type=stream_type,  # type: ignore[arg-type]
        stream=2,
        ffprobe="/tools/ffprobe",
    )
    assert command[0] == "/tools/ffprobe"
    assert command[command.index("-select_streams") + 1] == f"{stream_type}:2"
    assert command[command.index("-show_entries") + 1] == (
        "packet=pts_time,dts_time,duration_time,size"
    )


def test_packet_timeline_normalizes_uniform_mux_sync_offset() -> None:
    source = parse_packet_timeline(
        {
            "packets": [
                {
                    "pts_time": "0.040",
                    "dts_time": "0.000",
                    "duration_time": "0.032",
                    "size": "100",
                },
                {
                    "pts_time": "0.072",
                    "dts_time": "0.032",
                    "duration_time": "0.032",
                    "size": "90",
                },
                {
                    "pts_time": "0.104",
                    "dts_time": "0.064",
                    "duration_time": "0.032",
                    "size": "80",
                },
            ]
        }
    )
    final = parse_packet_timeline(
        {
            "packets": [
                {
                    "pts_time": "0.000",
                    "dts_time": "-0.040",
                    "duration_time": "0.032",
                    "size": "100",
                },
                {
                    "pts_time": "0.032",
                    "dts_time": "-0.008",
                    "duration_time": "0.032",
                    "size": "90",
                },
                {
                    "pts_time": "0.064",
                    "dts_time": "0.024",
                    "duration_time": "0.032",
                    "size": "80",
                },
            ]
        }
    )

    verdict = compare_packet_timelines(source, final)
    assert verdict.passed
    assert source.presentation_span_ms == final.presentation_span_ms == 96


def test_packet_timeline_rejects_internal_timestamp_shift_with_same_endpoints() -> None:
    def timeline(middle: str) -> dict[str, object]:
        return {
            "packets": [
                {
                    "pts_time": "0.000",
                    "dts_time": "0.000",
                    "duration_time": "0.010",
                    "size": "100",
                },
                {
                    "pts_time": middle,
                    "dts_time": middle,
                    "duration_time": "0.010",
                    "size": "100",
                },
                {
                    "pts_time": "0.099",
                    "dts_time": "0.099",
                    "duration_time": "0.010",
                    "size": "100",
                },
            ]
        }

    verdict = compare_packet_timelines(
        parse_packet_timeline(timeline("0.010")),
        parse_packet_timeline(timeline("0.050")),
    )
    assert not verdict.passed
    assert verdict.first_mismatch_indexes == (1,)


def test_packet_timeline_preserves_pts_to_dts_relationship() -> None:
    def timeline(dts: tuple[str, str]) -> dict[str, object]:
        return {
            "packets": [
                {
                    "pts_time": "0.000",
                    "dts_time": dts[0],
                    "duration_time": "0.040",
                    "size": "100",
                },
                {
                    "pts_time": "0.040",
                    "dts_time": dts[1],
                    "duration_time": "0.040",
                    "size": "100",
                },
            ]
        }

    verdict = compare_packet_timelines(
        parse_packet_timeline(timeline(("-0.080", "-0.040"))),
        parse_packet_timeline(timeline(("-0.040", "0.000"))),
    )
    assert not verdict.passed


def test_packet_timeline_duration_is_unavailable_if_any_duration_is_missing() -> None:
    timeline = parse_packet_timeline(
        {
            "packets": [
                {
                    "pts_time": "0.000",
                    "dts_time": "0.000",
                    "duration_time": "0.040",
                    "size": "100",
                },
                {
                    "pts_time": "0.040",
                    "dts_time": "0.040",
                    "size": "100",
                },
            ]
        }
    )
    assert timeline.presentation_span_ms is None
    assert timeline.missing_duration_count == 1


def _cfr_timeline(*pts_ms: int):
    return parse_packet_timeline(
        {
            "packets": [
                {
                    "pts_time": str(Decimal(pts) / Decimal(1000)),
                    "dts_time": str(Decimal(pts) / Decimal(1000)),
                    "duration_time": "0.040",
                    "size": "100",
                }
                for pts in pts_ms
            ]
        }
    )


def test_video_cadence_accepts_exact_cfr_grid() -> None:
    verdict = require_video_cadence(
        _cfr_timeline(0, 40, 80, 120),
        4,
        fps_numerator=25,
        fps_denominator=1,
    )
    assert verdict.passed
    assert verdict.maximum_pts_error_ms == 0
    assert verdict.maximum_duration_error_ms == 0


def test_video_cadence_rejects_irregular_middle_pts_with_same_endpoints() -> None:
    verdict = evaluate_video_cadence(
        _cfr_timeline(0, 60, 80, 120),
        4,
        fps_numerator=25,
        fps_denominator=1,
    )
    assert not verdict.passed
    assert verdict.maximum_pts_error_ms == 20
    assert any("CFR grid" in reason for reason in verdict.reasons)


def test_video_cadence_rejects_missing_or_duplicate_packets() -> None:
    verdict = evaluate_video_cadence(
        _cfr_timeline(0, 40, 40),
        4,
        fps_numerator=25,
        fps_denominator=1,
    )
    assert not verdict.packet_count_matches
    assert not verdict.pts_are_unique
    with pytest.raises(ValueError, match="packet-timeline count"):
        require_video_cadence(
            _cfr_timeline(0, 40, 40),
            4,
            fps_numerator=25,
            fps_denominator=1,
        )


def test_video_stream_hash_command_copies_one_compressed_stream() -> None:
    command = video_stream_hash_command(
        Path("encode.mkv"), stream=1, ffmpeg="/tools/ffmpeg"
    )
    assert command[0] == "/tools/ffmpeg"
    assert command[command.index("-map") + 1] == "0:v:1"
    assert command[command.index("-c") + 1] == "copy"
    assert command[command.index("-f") + 1] == "streamhash"
    assert command[command.index("-hash") + 1] == "sha256"
    assert "-xerror" in command
    assert command[-1] == "-"


def test_video_stream_hash_parser_requires_exactly_one_sha256() -> None:
    digest = "A1" * 32
    assert parse_video_stream_hash(f"0,v,SHA256={digest}\n") == digest.lower()


@pytest.mark.parametrize("stream_type", ["v", "a", "s"])
def test_payload_hash_command_and_parser_are_stream_type_specific(
    stream_type: str,
) -> None:
    command = stream_payload_hash_command(
        Path("track.mkv"),
        stream_type=stream_type,  # type: ignore[arg-type]
        stream=2,
    )
    assert command[command.index("-map") + 1] == f"0:{stream_type}:2"
    digest = "bc" * 32
    assert (
        parse_stream_payload_hash(
            f"0,{stream_type},SHA256={digest}\n",
            expected_stream_type=stream_type,  # type: ignore[arg-type]
        )
        == digest
    )


def test_payload_hash_parser_rejects_a_different_stream_type() -> None:
    with pytest.raises(ValueError, match="type differs"):
        parse_stream_payload_hash(
            "0,a,SHA256=" + ("ab" * 32), expected_stream_type="s"
        )


@pytest.mark.parametrize(
    "report",
    [
        "",
        "0,v,MD5=" + "a" * 32,
        "0,a,SHA256=" + "a" * 64,
        "0,v,SHA256=" + "a" * 63,
        "0,v,SHA256=" + "a" * 64 + "\n1,v,SHA256=" + "b" * 64,
    ],
)
def test_video_stream_hash_parser_rejects_unusable_reports(report: str) -> None:
    with pytest.raises(ValueError):
        parse_video_stream_hash(report)


def test_packet_size_parser_sums_exact_payload_bytes() -> None:
    summary = parse_video_packet_sizes(b"1200\r\n800\n\n0\n1500\n")
    assert summary.packet_count == 4
    assert summary.total_bytes == 3500
    assert summary.smallest_packet_bytes == 0
    assert summary.largest_packet_bytes == 1500
    assert summary.to_dict() == {
        "packet_count": 4,
        "total_bytes": 3500,
        "smallest_packet_bytes": 0,
        "largest_packet_bytes": 1500,
    }


@pytest.mark.parametrize("report", ["", "\n", "garbage\n", "12,13\n", "-1\n"])
def test_packet_size_parser_rejects_unusable_reports(report: str) -> None:
    with pytest.raises(ValueError):
        parse_video_packet_sizes(report)


def test_lossy_efficiency_rejects_equal_or_larger_output() -> None:
    equal = evaluate_video_efficiency(
        1_000_000, 1_000_000, minimum_savings_ratio=Decimal(0)
    )
    larger = evaluate_video_efficiency(
        1_000_000, 1_100_000, minimum_savings_ratio=Decimal("0.001")
    )
    assert not equal.passed
    assert equal.maximum_encoded_bytes == 999_999
    assert not larger.passed
    assert larger.encoded_to_source_ratio == Decimal("1.1")


def test_lossy_efficiency_enforces_configurable_small_savings_margin() -> None:
    too_close = evaluate_video_efficiency(
        1_000_000,
        999_500,
        minimum_savings_ratio=Decimal("0.001"),
    )
    passing = evaluate_video_efficiency(
        1_000_000,
        999_000,
        minimum_savings_ratio=Decimal("0.001"),
    )
    assert not too_close.passed
    assert too_close.required_savings_bytes == 1000
    assert passing.passed
    assert passing.saved_bytes == 1000


def test_non_lossy_output_records_but_does_not_apply_efficiency_gate() -> None:
    verdict = evaluate_video_efficiency(
        1000,
        1500,
        encoded_is_lossy=False,
        minimum_savings_ratio=Decimal("0.01"),
    )
    assert not verdict.check_applied
    assert verdict.passed
    assert verdict.reason is None
    assert verdict.required_savings_bytes == 0
    assert verdict.maximum_encoded_bytes is None


def test_require_video_efficiency_raises_review_ready_error() -> None:
    with pytest.raises(VideoEfficiencyError, match=r"encoded=1000.*source=1000"):
        require_video_efficiency(1000, 1000)


@pytest.mark.parametrize(
    ("source", "encoded", "margin"),
    [
        (0, 1, Decimal(0)),
        (1, 0, Decimal(0)),
        (1, 1, Decimal("-0.1")),
        (1, 1, Decimal(1)),
    ],
)
def test_efficiency_policy_rejects_invalid_inputs(
    source: int, encoded: int, margin: Decimal
) -> None:
    with pytest.raises(ValueError):
        evaluate_video_efficiency(
            source, encoded, minimum_savings_ratio=margin
        )


def test_stream_ordinals_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="ordinal"):
        source_video_integrity_command(Path("source.m2ts"), stream=-1)
    with pytest.raises(ValueError, match="ordinal"):
        video_packet_size_command(Path("encode.mkv"), stream=-1)
    with pytest.raises(ValueError, match="ordinal"):
        video_stream_hash_command(Path("encode.mkv"), stream=-1)
    with pytest.raises(ValueError, match="ordinal"):
        stream_payload_hash_command(
            Path("track.mkv"), stream_type="a", stream=-1
        )
