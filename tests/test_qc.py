from __future__ import annotations

import struct
import zlib
from decimal import Decimal
from pathlib import Path

import pytest

from bdencode.qc.artifacts import inspect_png
from bdencode.qc.audio import (
    AudioProbe,
    audio_probe_command,
    compare_audio_probes,
    parse_audio_probe,
    pcm_hash_command,
    plan_spectrum_windows,
    spectrum_command,
    spectrum_stitch_command,
)
from bdencode.qc.video import (
    FrameRecord,
    FrameSelectionError,
    extract_png_command,
    parse_vspipe_info,
    select_frame_pairs,
    standalone_vmaf_command,
)
from bdencode.vmaf_runner import streamed_vmaf_command


def _frames(types: str) -> list[FrameRecord]:
    return [
        FrameRecord(index, Decimal(index) / 24, kind)
        for index, kind in enumerate(types)
    ]


def test_frame_pairs_keep_identical_presentation_index() -> None:
    encoded = _frames("IPBBIPBBIPBBIPBB")
    reference = _frames("IIBPIIBPIIBPIIBP")
    pairs = select_frame_pairs(encoded, reference, per_type=2)
    assert {pair.category for pair in pairs} == {"I", "P", "B"}
    assert len(pairs) == 6
    assert all(pair.encoded_pts_seconds == pair.reference_pts_seconds for pair in pairs)
    assert any(not pair.dual_type_match for pair in pairs)


def test_dual_type_never_substitutes_a_different_frame() -> None:
    encoded = _frames("IPBBIPBBIPBB")
    reference = _frames("IIIIIIIIIIII")
    with pytest.raises(FrameSelectionError, match="mandatory P"):
        select_frame_pairs(encoded, reference, per_type=1, dual_type_match=True)


def test_missing_b_frames_is_hard_failure() -> None:
    frames = _frames("IPPPIPPP")
    with pytest.raises(FrameSelectionError, match="mandatory B"):
        select_frame_pairs(frames, frames, per_type=1)


def test_vapoursynth_reference_pts_comes_from_real_clip_info() -> None:
    info = parse_vspipe_info("Width: 1920\nFrames: 240\nFPS: 24000/1001 (23.976 fps)\n")
    assert info.frames == 240
    assert info.pts_for_frame(24) == Decimal("1.001")


def test_audio_structure_comparison() -> None:
    source = AudioProbe(
        "truehd", 48000, 8, "7.1", 480000, Decimal("0"), Decimal("10"), 24
    )
    encoded = AudioProbe(
        "flac", 48000, 8, "7.1", 480000, Decimal("0"), Decimal("10"), 24
    )
    result = compare_audio_probes(source, encoded)
    assert result.structurally_lossless
    command = spectrum_command(
        Path("input.flac"),
        0,
        Path("spectrum.png"),
        start_seconds=Decimal("300"),
        duration_seconds=Decimal("287.882"),
        height=94,
    )
    filter_value = command[command.index("-filter_complex") + 1]
    assert filter_value.startswith("[0:a:0]showspectrumpic=")
    assert filter_value.endswith(",format=rgb48be[spectrum]")
    assert "s=3840x94" in filter_value
    assert "legend=0" in filter_value
    assert "orientation=horizontal" in filter_value
    assert command[command.index("-map") + 1] == "[spectrum]"
    assert command[command.index("-c:v") + 1] == "png"
    assert command[command.index("-pix_fmt") + 1] == "rgb48be"
    assert command[command.index("-ss") + 1] == "300"
    assert command[command.index("-t") + 1] == "287.882"
    assert "-an" in command
    assert "-lavfi" not in command
    assert command[-1] == "spectrum.png"


def test_audio_probe_falls_back_to_container_duration() -> None:
    probe = parse_audio_probe(
        {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "dts",
                    "sample_rate": "48000",
                    "channels": 6,
                    "channel_layout": "5.1(side)",
                    "start_time": "0.040000",
                }
            ],
            "format": {"duration": "6621.288000"},
        }
    )
    assert probe.duration == Decimal("6621.288000")
    assert probe.sample_count is None
    command = audio_probe_command(Path("input.mkv"))
    assert ":format=duration" in command[command.index("-show_entries") + 1]


def test_spectrum_windows_cover_long_tracks_without_unbounded_buffers() -> None:
    duration = Decimal("6621.288")
    windows = plan_spectrum_windows(duration)
    assert len(windows) == 23
    assert windows[0].start_seconds == 0
    assert windows[-1].start_seconds + windows[-1].duration_seconds == duration
    assert all(window.duration_seconds <= 300 for window in windows)
    assert all(
        left.start_seconds + left.duration_seconds == right.start_seconds
        for left, right in zip(windows, windows[1:])
    )
    assert sum(window.height for window in windows) == 2160
    assert max(window.height for window in windows) - min(
        window.height for window in windows
    ) <= 1
    seconds_per_pixel = duration / Decimal(2160)
    assert all(
        abs((window.duration_seconds / Decimal(window.height)) - seconds_per_pixel)
        < Decimal("1e-20")
        for window in windows
    )


def test_spectrum_stitch_maps_only_the_composed_png() -> None:
    inputs = tuple(Path(f"window-{index:02d}.png") for index in range(3))
    command = spectrum_stitch_command(inputs, Path("spectrum.png"))
    filter_value = command[command.index("-filter_complex") + 1]
    assert filter_value.startswith("[0:v:0][1:v:0][2:v:0]vstack=inputs=3")
    assert command[command.index("-map") + 1] == "[spectrum]"
    assert command[command.index("-c:v") + 1] == "png"
    assert command[-1] == "spectrum.png"


def test_spectrum_command_refuses_an_unbounded_full_track() -> None:
    with pytest.raises(ValueError, match="between 0 and 300"):
        spectrum_command(
            Path("input.flac"),
            0,
            Path("spectrum.png"),
            start_seconds=Decimal(0),
            duration_seconds=Decimal("301"),
        )


def test_pcm_integrity_uses_payload_hash_not_frame_packetization() -> None:
    command = pcm_hash_command(Path("input.flac"), 0)
    assert command[command.index("-f") + 1] == "hash"
    assert command[command.index("-hash") + 1] == "sha256"
    assert "framemd5" not in command


def _png(path: Path, bit_depth: int) -> None:
    signature = b"\x89PNG\r\n\x1a\n"
    data = struct.pack(">IIBBBBB", 1, 1, bit_depth, 2, 0, 0, 0)
    chunk = (
        struct.pack(">I", len(data))
        + b"IHDR"
        + data
        + struct.pack(">I", zlib.crc32(b"IHDR" + data))
    )
    path.write_bytes(signature + chunk)


def test_png_high_bit_depth_policy(tmp_path: Path) -> None:
    path = tmp_path / "native.png"
    _png(path, 16)
    assert inspect_png(path, require_high_bit_depth=True).high_bit_depth
    _png(path, 8)
    with pytest.raises(ValueError, match="higher than 8-bit"):
        inspect_png(path, require_high_bit_depth=True)


@pytest.mark.parametrize(
    ("source_hdr10", "hdr_native", "expected_matrix"),
    [(True, True, "m=bt2020nc"), (False, True, "m=bt709")],
)
def test_native_png_conversion_keeps_yuv_matrix_until_rgb_format(
    source_hdr10: bool, hdr_native: bool, expected_matrix: str
) -> None:
    command = extract_png_command(
        Path("input.mkv"),
        10,
        Path("frame.png"),
        source_hdr10=source_hdr10,
        hdr_native=hdr_native,
    )
    filters = command[command.index("-vf") + 1]
    assert expected_matrix in filters
    assert "m=gbr" not in filters
    assert "format=gbrp16le" in filters


def test_official_vmaf_cli_plan_uses_y4m_sidecars() -> None:
    command = standalone_vmaf_command(
        Path("reference.y4m"), Path("encode.y4m"), Path("vmaf.json")
    )
    assert command[0] == "vmaf"
    assert "--json" in command
    assert "float_ms_ssim" in command


def test_streamed_vmaf_wrapper_records_hdr_mode() -> None:
    command = streamed_vmaf_command(
        Path("reference.vpy"),
        Path("encode.mkv"),
        Path("vmaf.json"),
        hdr10=True,
        model_4k=True,
    )
    assert command[0] == "bdencode-vmaf"
    assert "--hdr10" in command
    assert command[command.index("--model") + 1] == "vmaf_4k_v0.6.1"
