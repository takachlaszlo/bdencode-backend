from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from bdencode.media.bluray import (
    BluRayScanner,
    CaptureResult,
    ToolCapabilities,
    _hdr_static_from_frame_payload,
)


def _frame_payload() -> dict[str, Any]:
    return {
        "frames": [
            {
                "side_data_list": [
                    {
                        "side_data_type": "Mastering display metadata",
                        "red_x": "34000/50000",
                        "red_y": "16000/50000",
                        "green_x": "13250/50000",
                        "green_y": "34500/50000",
                        "blue_x": "7500/50000",
                        "blue_y": "3000/50000",
                        "white_point_x": "15635/50000",
                        "white_point_y": "16450/50000",
                        "max_luminance": "10000000/10000",
                        "min_luminance": "50/10000",
                    },
                    {
                        "side_data_type": "Content light level metadata",
                        "max_content": 1000,
                        "max_average": 427,
                    },
                ]
            },
            {
                "side_data_list": [
                    {
                        "side_data_type": "Mastering display metadata",
                        "red_x": "34000/50000",
                        "red_y": "16000/50000",
                        "green_x": "13250/50000",
                        "green_y": "34500/50000",
                        "blue_x": "7500/50000",
                        "blue_y": "3000/50000",
                        "white_point_x": "15635/50000",
                        "white_point_y": "16450/50000",
                        "max_luminance": "10000000/10000",
                        "min_luminance": "50/10000",
                    },
                    {
                        "side_data_type": "Content light level metadata",
                        "max_content": 1000,
                        "max_average": 427,
                    },
                ]
            },
        ]
    }


class FakeRunner:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, ...]] = []

    def capture(
        self,
        argv: Sequence[str | Path],
        *,
        timeout: float = 30,
        check: bool = True,
    ) -> CaptureResult:
        self.calls.append(tuple(str(item) for item in argv))
        return CaptureResult(
            returncode=0,
            stdout=json.dumps(self.payload),
            stderr="",
        )


def test_frame_payload_is_converted_to_x265_hdr10_metadata() -> None:
    metadata = _hdr_static_from_frame_payload(_frame_payload())

    assert metadata.complete
    assert metadata.mastering_display == (
        "G(13250,34500)"
        "B(7500,3000)"
        "R(34000,16000)"
        "WP(15635,16450)"
        "L(10000000,50)"
    )
    assert metadata.max_cll == 1000
    assert metadata.max_fall == 427


def test_conflicting_cll_pairs_fail_closed() -> None:
    payload = _frame_payload()
    payload["frames"].append(
        {
            "side_data_list": [
                {
                    "side_data_type": "Content light level metadata",
                    "max_content": 4000,
                    "max_average": 1000,
                }
            ]
        }
    )

    metadata = _hdr_static_from_frame_payload(payload)

    assert metadata.mastering_display is not None
    assert metadata.max_cll is None
    assert metadata.max_fall is None
    assert not metadata.complete


def test_scanner_enriches_hdr10_from_representative_clip(
    tmp_path: Path,
) -> None:
    root = tmp_path / "disc"
    stream_directory = root / "BDMV" / "STREAM"
    stream_directory.mkdir(parents=True)
    clip = stream_directory / "00278.m2ts"
    clip.write_bytes(b"")

    runner = FakeRunner(_frame_payload())
    scanner = BluRayScanner(
        runner=runner,
        capabilities=ToolCapabilities(
            ffprobe="/usr/bin/ffprobe",
            ffprobe_bluray=True,
        ),
        source_root=tmp_path,
    )

    probe = {
        "streams": [
            {
                "index": 0,
                "id": "0x1011",
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 3840,
                "height": 2160,
                "pix_fmt": "yuv420p10le",
                "bits_per_raw_sample": "10",
                "color_primaries": "bt2020",
                "color_transfer": "smpte2084",
                "color_space": "bt2020nc",
                "avg_frame_rate": "24000/1001",
                "field_order": "progressive",
            }
        ],
        "format": {"duration": "6120.489356"},
    }

    native = {
        "duration": 6120.489356,
        "segments": [
            {
                "clip_id": "00278",
                "in_time": 0,
                "out_time": 6120.489356,
                "duration": 6120.489356,
                "packet_count": 1,
            }
        ],
        "streams": [
            {
                "pid": 4113,
                "codec_type": "video",
                "codec": "hevc",
            }
        ],
    }

    enriched = scanner._with_hdr_static_metadata(
        root,
        probe,
        native,
    )

    video_stream = enriched["streams"][0]

    assert video_stream["mastering_display"] == (
        "G(13250,34500)"
        "B(7500,3000)"
        "R(34000,16000)"
        "WP(15635,16450)"
        "L(10000000,50)"
    )
    assert video_stream["max_cll"] == 1000
    assert video_stream["max_fall"] == 427

    playlist = scanner._playlist_from_payload(
        "00803",
        enriched,
        native,
    )
    video = playlist.video_streams[0].video

    assert video is not None
    assert video.hdr10
    assert video.hdr10_static.complete
    assert video.hdr10_static.max_cll == 1000
    assert video.hdr10_static.max_fall == 427

    scanner._with_hdr_static_metadata(root, probe, native)

    assert len(runner.calls) == 1
    assert "-show_frames" in runner.calls[0]
    assert str(clip) in runner.calls[0]



def test_truehd_and_embedded_ac3_core_receive_unique_ids(
    tmp_path: Path,
) -> None:
    scanner = BluRayScanner(
        capabilities=ToolCapabilities(),
        source_root=tmp_path,
    )

    probe = {
        "streams": [
            {
                "index": 2,
                "id": "0x1100",
                "codec_type": "audio",
                "codec_name": "truehd",
                "channels": 8,
                "sample_rate": "48000",
                "tags": {"language": "eng"},
            },
            {
                "index": 3,
                "id": "0x1100",
                "codec_type": "audio",
                "codec_name": "ac3",
                "channels": 6,
                "sample_rate": "48000",
                "tags": {"language": "eng"},
            },
            {
                "index": 4,
                "id": "0x1101",
                "codec_type": "audio",
                "codec_name": "ac3",
                "channels": 6,
                "sample_rate": "48000",
                "tags": {"language": "spa"},
            },
        ],
        "format": {"duration": "6120.489356"},
    }

    playlist = scanner._playlist_from_payload(
        "00803",
        probe,
        {
            "duration": 6120.489356,
            "segments": [],
        },
    )

    assert [stream.id for stream in playlist.audio_streams] == [
        "audio:4352:truehd",
        "audio:4352:ac3",
        "audio:4353",
    ]

    assert len(
        {stream.id for stream in playlist.audio_streams}
    ) == len(playlist.audio_streams)
