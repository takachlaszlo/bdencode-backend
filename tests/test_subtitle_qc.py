from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from bdencode.qc.subtitle import (
    SubtitleProbe,
    SubtitleProbeError,
    parse_subtitle_probe,
    subtitle_probe_command,
    validate_subtitle_classification,
)


def test_subtitle_probe_command_counts_one_sidecar_stream() -> None:
    command = subtitle_probe_command(
        Path("track-03-subtitle.mks"), ffprobe="/usr/bin/ffprobe"
    )

    assert command[0] == "/usr/bin/ffprobe"
    assert command[command.index("-select_streams") + 1] == "s:0"
    assert "-count_packets" in command
    entries = command[command.index("-show_entries") + 1]
    assert "nb_read_packets" in entries
    assert "start_time" in entries
    assert "duration" in entries
    assert command[-1] == "track-03-subtitle.mks"


def test_parse_subtitle_probe_returns_packet_event_count_and_timing() -> None:
    probe = parse_subtitle_probe(
        {
            "streams": [
                {
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "nb_read_packets": "1248",
                    "start_time": "12.463000",
                    "duration": "6811.250000",
                }
            ],
            "format": {"start_time": "0.000000", "duration": "6820.000000"},
        }
    )

    assert probe.codec_name == "hdmv_pgs_subtitle"
    assert probe.packet_count == 1248
    assert probe.event_count == 1248
    assert probe.start_time == Decimal("12.463000")
    assert probe.duration == Decimal("6811.250000")


def test_parse_subtitle_probe_uses_container_timing_fallbacks() -> None:
    probe = parse_subtitle_probe(
        """{
          "streams": [{
            "codec_type": "subtitle",
            "codec_name": "subrip",
            "nb_read_packets": "42",
            "start_time": "N/A",
            "duration": "N/A"
          }],
          "format": {"start_time": "3.500", "duration": "90.250"}
        }"""
    )

    assert probe.start_time == Decimal("3.500")
    assert probe.duration == Decimal("90.250")


@pytest.mark.parametrize(
    "document, message",
    [
        ({"streams": []}, "exactly one"),
        (
            {
                "streams": [
                    {
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "nb_read_packets": "2",
                    }
                ]
            },
            "not a subtitle",
        ),
        (
            {
                "streams": [
                    {
                        "codec_type": "subtitle",
                        "codec_name": "subrip",
                        "nb_read_packets": "broken",
                    }
                ]
            },
            "not an integer",
        ),
    ],
)
def test_parse_subtitle_probe_rejects_ambiguous_or_invalid_evidence(
    document: dict[str, object], message: str
) -> None:
    with pytest.raises(SubtitleProbeError, match=message):
        parse_subtitle_probe(document)


def test_full_pgs_misclassified_as_forced_is_rejected() -> None:
    full_pgs = SubtitleProbe(
        codec_name="hdmv_pgs_subtitle",
        packet_count=1248,
        start_time=Decimal("12.463"),
        duration=Decimal("6811.25"),
    )

    errors = validate_subtitle_classification(
        full_pgs,
        subtitle_kind="forced",
        title_duration_seconds=Decimal("6820"),
    )

    assert len(errors) == 1
    assert "full-track density" in errors[0]
    assert "1248 packets/events" in errors[0]


def test_full_classification_accepts_a_dense_pgs_track() -> None:
    full_pgs = SubtitleProbe(
        codec_name="hdmv_pgs_subtitle",
        packet_count=1248,
        start_time=Decimal("12.463"),
        duration=Decimal("6811.25"),
    )

    assert not validate_subtitle_classification(
        full_pgs,
        subtitle_kind="full",
        title_duration_seconds=Decimal("6820"),
    )


def test_coverage_is_clipped_to_the_reviewed_title_timeline() -> None:
    probe = SubtitleProbe(
        codec_name="hdmv_pgs_subtitle",
        packet_count=20,
        start_time=Decimal("90"),
        duration=Decimal("50"),
    )

    assert probe.coverage_fraction(Decimal("100")) == Decimal("0.1")


@pytest.mark.parametrize(
    ("packet_count", "duration"),
    [
        (250, Decimal("4000")),
        (251, Decimal("3000")),
        (40, Decimal("5900")),
    ],
)
def test_forced_density_requires_both_limits_to_be_exceeded(
    packet_count: int, duration: Decimal
) -> None:
    probe = SubtitleProbe(
        codec_name="hdmv_pgs_subtitle",
        packet_count=packet_count,
        start_time=Decimal(0),
        duration=duration,
    )

    assert not validate_subtitle_classification(
        probe,
        subtitle_kind="forced",
        title_duration_seconds=Decimal("6000"),
    )


def test_forced_classification_fails_closed_when_probe_evidence_is_missing() -> None:
    incomplete = SubtitleProbe(
        codec_name="hdmv_pgs_subtitle",
        packet_count=None,
        start_time=None,
        duration=None,
    )

    errors = validate_subtitle_classification(
        incomplete,
        subtitle_kind="forced",
        title_duration_seconds=Decimal("6000"),
    )

    assert errors == (
        "forced subtitle classification lacks required probe evidence: "
        "packet/event count, start time, duration",
    )


@pytest.mark.parametrize("subtitle_kind", ["full", "forced"])
def test_empty_subtitle_sidecar_is_rejected_for_every_kind(subtitle_kind: str) -> None:
    empty = SubtitleProbe(
        codec_name="hdmv_pgs_subtitle",
        packet_count=0,
        start_time=Decimal(0),
        duration=Decimal(0),
    )

    errors = validate_subtitle_classification(
        empty,
        subtitle_kind=subtitle_kind,
        title_duration_seconds=Decimal("6000"),
    )

    assert errors and "empty" in errors[0]


def test_sparse_full_span_subtitle_cannot_be_marked_forced() -> None:
    sparse_full = SubtitleProbe(
        codec_name="hdmv_pgs_subtitle",
        packet_count=320,
        start_time=Decimal(0),
        duration=Decimal(5800),
    )

    errors = validate_subtitle_classification(
        sparse_full,
        subtitle_kind="forced",
        title_duration_seconds=Decimal("6000"),
    )

    assert errors and "full-track density" in errors[0]


def test_unreviewed_subtitle_kind_fails_closed() -> None:
    probe = SubtitleProbe(
        codec_name="subrip",
        packet_count=12,
        start_time=Decimal(0),
        duration=Decimal(90),
    )

    errors = validate_subtitle_classification(
        probe,
        subtitle_kind="unknown",
        title_duration_seconds=Decimal(90),
    )

    assert errors and "explicitly classified" in errors[0]
