from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from bdencode.qc import (
    SubtitleDecodeError,
    evaluate_subtitle_decode,
    parse_subtitle_decode_probe,
    require_subtitle_decode,
    subtitle_decode_probe_command,
)


def _decode_document(
    *, codec_name: str = "hdmv_pgs_subtitle"
) -> dict[str, object]:
    return {
        "frames": [
            {
                "media_type": "subtitle",
                "pts_time": "12.000000",
                "start_display_time": 50,
                "end_display_time": 2050,
                "num_rects": 1,
            },
            {
                "media_type": "subtitle",
                "pts_time": "27.500000",
                "start_display_time": 0,
                "end_display_time": 1750,
                "num_rects": 1,
            },
        ],
        "streams": [
            {
                "index": 5,
                "codec_type": "subtitle",
                "codec_name": codec_name,
            }
        ],
    }


def test_decode_command_selects_type_ordinal_and_decodes_every_event() -> None:
    command = subtitle_decode_probe_command(
        Path("final.mkv"), 3, ffprobe="/opt/ffmpeg/bin/ffprobe"
    )

    assert command[0] == "/opt/ffmpeg/bin/ffprobe"
    assert command[command.index("-select_streams") + 1] == "s:3"
    assert "-show_frames" in command
    assert command[command.index("-err_detect") + 1] == "explode"
    assert command[command.index("-of") + 1] == "json=compact=1"
    assert "subtitle=" in command[command.index("-show_entries") + 1]
    assert "-show_packets" not in command
    assert "-c:s" not in command
    assert "copy" not in command


@pytest.mark.parametrize("ordinal", [-1, True, 1.5])
def test_decode_command_rejects_invalid_type_ordinals(ordinal: object) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        subtitle_decode_probe_command(
            Path("final.mkv"), ordinal  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("codec_name", ["hdmv_pgs_subtitle", "ass", "subrip"])
def test_parse_and_require_decode_accept_pgs_and_text_events(
    codec_name: str,
) -> None:
    probe = parse_subtitle_decode_probe(_decode_document(codec_name=codec_name))

    assert probe.codec_name == codec_name
    assert probe.stream_index == 5
    assert probe.event_count == 2
    assert probe.events[0].timestamp == Decimal("12.05")
    assert probe.events[0].duration == Decimal("2")

    verdict = require_subtitle_decode(probe)
    assert verdict.passed
    assert verdict.decoded_event_count == 2
    assert verdict.visible_event_count == 2
    assert verdict.first_timestamp == Decimal("12.05")
    assert verdict.last_end_timestamp == Decimal("29.25")
    assert verdict.to_dict()["first_timestamp"] == "12.050000"


def test_empty_decode_report_cannot_pass() -> None:
    document = _decode_document()
    document["frames"] = []
    probe = parse_subtitle_decode_probe(document)

    verdict = evaluate_subtitle_decode(probe)
    assert not verdict.passed
    assert verdict.decoded_event_count == 0
    assert "subtitle decoder produced zero frames/events" in verdict.reasons
    assert "subtitle decoder produced no visible timed event" in verdict.reasons
    with pytest.raises(SubtitleDecodeError, match="zero frames/events"):
        require_subtitle_decode(probe)


@pytest.mark.parametrize(
    "document",
    [
        "{not-json",
        b"\xff",
        [],
        {"streams": [], "frames": []},
    ],
)
def test_malformed_decode_reports_fail_closed(document: object) -> None:
    with pytest.raises(SubtitleDecodeError):
        parse_subtitle_decode_probe(document)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "missing_field, reason",
    [
        ("pts_time", "timestamp"),
        ("start_display_time", "timestamp"),
        ("end_display_time", "duration"),
    ],
)
def test_missing_decoded_event_timing_cannot_pass(
    missing_field: str, reason: str
) -> None:
    document = _decode_document(
        codec_name="subrip"
        if missing_field == "end_display_time"
        else "hdmv_pgs_subtitle"
    )
    frames = document["frames"]
    assert isinstance(frames, list)
    assert isinstance(frames[0], dict)
    del frames[0][missing_field]

    probe = parse_subtitle_decode_probe(document)
    verdict = evaluate_subtitle_decode(probe)

    assert not verdict.passed
    assert any(reason in item for item in verdict.reasons)
    with pytest.raises(SubtitleDecodeError, match=reason):
        require_subtitle_decode(probe)


def test_invalid_decoded_event_timing_is_rejected_during_parse() -> None:
    document = _decode_document()
    frames = document["frames"]
    assert isinstance(frames, list)
    assert isinstance(frames[0], dict)
    frames[0]["end_display_time"] = 40

    with pytest.raises(SubtitleDecodeError, match="precedes"):
        parse_subtitle_decode_probe(document)


def test_missing_rectangle_count_is_rejected_during_parse() -> None:
    document = _decode_document()
    frames = document["frames"]
    assert isinstance(frames, list)
    assert isinstance(frames[0], dict)
    del frames[0]["num_rects"]

    with pytest.raises(SubtitleDecodeError, match="rectangle count is missing"):
        parse_subtitle_decode_probe(document)


def test_clear_or_zero_span_events_alone_cannot_prove_visible_subtitles() -> None:
    document = _decode_document()
    frames = document["frames"]
    assert isinstance(frames, list)
    assert all(isinstance(frame, dict) for frame in frames)
    for frame in frames:
        assert isinstance(frame, dict)
        frame["num_rects"] = 0
        frame["end_display_time"] = frame["start_display_time"]

    verdict = evaluate_subtitle_decode(parse_subtitle_decode_probe(document))

    assert not verdict.passed
    assert verdict.visible_event_count == 0
    assert "subtitle decoder produced no visible timed event" in verdict.reasons


def test_pgs_unknown_end_sentinel_is_not_treated_as_a_multiweek_duration() -> None:
    document = _decode_document(codec_name="hdmv_pgs_subtitle")
    frames = document["frames"]
    assert isinstance(frames, list)
    assert all(isinstance(frame, dict) for frame in frames)
    for index, frame in enumerate(frames):
        assert isinstance(frame, dict)
        frame["end_display_time"] = 2**32 - 1
        frame["num_rects"] = 1 if index == 0 else 0

    verdict = require_subtitle_decode(parse_subtitle_decode_probe(document))

    assert verdict.passed
    assert verdict.visible_event_count == 1
    assert verdict.missing_duration_count == 2
    assert verdict.last_end_timestamp is None


def test_non_monotonic_subtitle_timestamps_fail_closed() -> None:
    document = _decode_document()
    frames = document["frames"]
    assert isinstance(frames, list)
    assert isinstance(frames[0], dict)
    assert isinstance(frames[1], dict)
    frames[0]["pts_time"] = "30.000"
    frames[1]["pts_time"] = "20.000"

    verdict = evaluate_subtitle_decode(parse_subtitle_decode_probe(document))

    assert not verdict.passed
    assert verdict.non_monotonic_timestamp_count == 1
    assert any("out of order" in reason for reason in verdict.reasons)
