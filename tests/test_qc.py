from __future__ import annotations

import struct
import zlib
from decimal import Decimal
from pathlib import Path

import pytest

from bdencode.audio import effective_audio_policy
from bdencode.qc.artifacts import inspect_png
from bdencode.qc.audio import (
    AudioProbe,
    analysis_command,
    audio_probe_command,
    compare_audio_probes,
    parse_audio_probe,
    pcm_hash_command,
    plan_spectrum_windows,
    spectrum_command,
    spectrum_stitch_command,
    verify_audio_output,
)
from bdencode.qc.video import (
    CropMargins,
    FrameProbeInterval,
    FrameRecord,
    FrameSelectionError,
    VapourSynthInfo,
    annotate_comparison_png_command,
    extract_png_command,
    extract_png_at_timestamp_command,
    extract_y4m_at_timestamp_command,
    ffprobe_frame_origin_command,
    ffprobe_sampled_frame_command,
    native_yuv_metric_command,
    parse_cropdetect,
    parse_ffmpeg_metric_stats,
    parse_ffprobe_frame_origin,
    parse_sampled_ffprobe_frames,
    parse_vspipe_info,
    plan_sample_intervals,
    recommended_comparison_pair_count,
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


def test_total_pair_selection_keeps_mandatory_ipb_and_spends_extras_on_pb() -> None:
    encoded = _frames("IPBBPBBIPBBPBBIPBBPBBIPBBPBB")
    pairs = select_frame_pairs(
        encoded,
        encoded,
        total_pairs=5,
        timeline_frames=len(encoded),
        dual_type_match=True,
    )
    assert len(pairs) == 5
    assert {
        category: sum(pair.category == category for pair in pairs) for category in "IPB"
    } == {
        "I": 1,
        "P": 2,
        "B": 2,
    }
    assert all(pair.dual_type_match for pair in pairs)
    assert len({pair.presentation_index for pair in pairs}) == 5


def test_sample_interval_plan_is_distributed_and_hard_bounded() -> None:
    info = VapourSynthInfo(frames=158_750, fps_numerator=24_000, fps_denominator=1001)
    intervals = plan_sample_intervals(info)
    assert intervals[0].start_seconds == 0
    assert len(intervals) == 25
    assert sum(item.duration_seconds for item in intervals) <= Decimal("54")
    assert all(item.end_seconds <= info.duration_seconds for item in intervals)
    assert any(
        item.start_seconds > info.duration_seconds * Decimal("0.8")
        for item in intervals
    )
    assert all(
        left.end_seconds <= right.start_seconds
        for left, right in zip(intervals, intervals[1:])
    )


def test_short_sample_interval_plan_merges_overlaps_without_exceeding_clip() -> None:
    info = VapourSynthInfo(frames=200, fps_numerator=25, fps_denominator=1)
    intervals = plan_sample_intervals(info)
    assert intervals == (FrameProbeInterval(Decimal(0), Decimal(8)),)


def test_sampled_ffprobe_command_never_requests_an_unbounded_scan() -> None:
    intervals = (
        FrameProbeInterval(Decimal(0), Decimal(6)),
        FrameProbeInterval(Decimal("120.5"), Decimal(3)),
    )
    command = ffprobe_sampled_frame_command(
        Path("encode.mkv"), intervals, pts_origin=Decimal("7")
    )
    assert command[command.index("-read_intervals") + 1] == "7%13,115.5%130.5"
    assert "-show_frames" in command
    assert command[-1] == "encode.mkv"


def test_opening_origin_probe_is_bounded_and_accepts_nonzero_or_negative_pts() -> None:
    command = ffprobe_frame_origin_command(Path("encode.mkv"))
    assert command[command.index("-read_intervals") + 1] == "%+1"
    assert parse_ffprobe_frame_origin(
        {
            "frames": [
                {"media_type": "audio", "best_effort_timestamp_time": "-1"},
                {"media_type": "video", "best_effort_timestamp_time": "0.040"},
                {"media_type": "video", "best_effort_timestamp_time": "0.082"},
            ]
        }
    ) == Decimal("0.040")
    assert parse_ffprobe_frame_origin(
        {"frames": [{"media_type": "video", "pts_time": "-0.250"}]}
    ) == Decimal("-0.250")


def test_sampled_frame_parser_recovers_global_indexes_and_deduplicates() -> None:
    info = VapourSynthInfo(frames=1000, fps_numerator=25, fps_denominator=1)
    document = {
        "frames": [
            {
                "media_type": "video",
                "best_effort_timestamp_time": "7.000",
                "pict_type": "I",
                "key_frame": 1,
            },
            {
                "media_type": "video",
                "best_effort_timestamp_time": "7.040",
                "pict_type": "P",
            },
            {
                "media_type": "video",
                "best_effort_timestamp_time": "11.000",
                "pict_type": "B",
            },
            # Seeking adjacent intervals may report the same decoded frame.
            {
                "media_type": "video",
                "pts_time": "11.000",
                "pict_type": "B",
            },
        ]
    }
    frames = parse_sampled_ffprobe_frames(document, info, pts_origin=Decimal("7.000"))
    assert [item.presentation_index for item in frames] == [0, 1, 100]
    assert [item.pts_seconds for item in frames] == [
        Decimal(0),
        Decimal("0.04"),
        Decimal(4),
    ]
    assert frames[-1].seek_pts_seconds == Decimal("11.000")


def test_sampled_frame_parser_rejects_non_cfr_alignment() -> None:
    info = VapourSynthInfo(frames=1000, fps_numerator=25, fps_denominator=1)
    document = {
        "frames": [
            {"best_effort_timestamp_time": "7.000", "pict_type": "I"},
            {"best_effort_timestamp_time": "11.015", "pict_type": "P"},
        ]
    }
    with pytest.raises(FrameSelectionError, match="does not align"):
        parse_sampled_ffprobe_frames(document, info, pts_origin=Decimal("7.000"))


def test_sampled_frame_parser_refuses_to_rebase_a_missing_opening_sample() -> None:
    info = VapourSynthInfo(frames=1000, fps_numerator=25, fps_denominator=1)
    document = {
        "frames": [
            {"best_effort_timestamp_time": "11.000", "pict_type": "P"},
        ]
    }
    with pytest.raises(FrameSelectionError, match="opening video frame"):
        parse_sampled_ffprobe_frames(document, info, pts_origin=Decimal("7.000"))


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


def _packet_audio_probe_document(
    duration: Decimal, *, container_duration: Decimal = Decimal("100")
) -> dict[str, object]:
    packet_duration = Decimal("0.032")
    return {
        "streams": [
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "ac3",
                "sample_rate": "48000",
                "channels": 6,
                "channel_layout": "5.1(side)",
                "start_time": "0",
                "duration": str(duration),
                "nb_read_packets": "2",
            }
        ],
        "packets": [
            {
                "stream_index": 1,
                "pts_time": "0",
                "duration_time": str(packet_duration),
            },
            {
                "stream_index": 1,
                "pts_time": str(duration - packet_duration),
                "duration_time": str(packet_duration),
            },
        ],
        "format": {"duration": str(container_duration)},
    }


def test_audio_probe_uses_complete_track_packet_tail_not_container_duration() -> None:
    source = parse_audio_probe(_packet_audio_probe_document(Decimal("100")))
    truncated = parse_audio_probe(_packet_audio_probe_document(Decimal("90")))

    assert source.duration == Decimal("100.000")
    assert truncated.duration == Decimal("90.000")
    assert truncated.duration_evidence == "complete_packet_tail"
    assert truncated.packet_count == 2
    policy = effective_audio_policy(
        "copy",
        source_codec="ac3",
        source_channels=6,
        source_sample_rate=48_000,
    )
    verification = verify_audio_output(
        source, truncated, policy, decoded_pcm_sha256_match=True
    )
    assert verification.duration_within_tolerance is False
    assert verification.passed is False


def test_audio_probe_accepts_matching_full_track_packet_tail() -> None:
    source = parse_audio_probe(_packet_audio_probe_document(Decimal("100")))
    encode = parse_audio_probe(_packet_audio_probe_document(Decimal("100")))
    policy = effective_audio_policy(
        "copy",
        source_codec="ac3",
        source_channels=6,
        source_sample_rate=48_000,
    )

    verification = verify_audio_output(
        source, encode, policy, decoded_pcm_sha256_match=True
    )

    assert verification.duration_within_tolerance is True
    assert verification.passed is True


def test_audio_probe_missing_track_duration_evidence_fails_closed() -> None:
    missing = parse_audio_probe(
        {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "sample_rate": "48000",
                    "channels": 6,
                    "channel_layout": "5.1(side)",
                    "start_time": "0",
                }
            ],
            "format": {"duration": "100"},
        }
    )
    complete = parse_audio_probe(_packet_audio_probe_document(Decimal("100")))
    policy = effective_audio_policy(
        "copy",
        source_codec="ac3",
        source_channels=6,
        source_sample_rate=48_000,
    )

    verification = verify_audio_output(
        complete, missing, policy, decoded_pcm_sha256_match=True
    )

    assert missing.duration is None
    assert missing.duration_evidence is None
    assert verification.duration_within_tolerance is False
    assert verification.passed is False


def test_audio_probe_rejects_incomplete_packet_tail_evidence() -> None:
    document = _packet_audio_probe_document(Decimal("100"))
    streams = document["streams"]
    assert isinstance(streams, list)
    stream = streams[0]
    assert isinstance(stream, dict)
    stream["nb_read_packets"] = "3"

    with pytest.raises(ValueError, match="packet count mismatch"):
        parse_audio_probe(document)


def test_audio_probe_command_collects_track_specific_packet_tail() -> None:
    command = audio_probe_command(Path("input.mkv"))
    entries = command[command.index("-show_entries") + 1]
    assert "-count_packets" in command
    assert "-show_packets" in command
    assert "nb_read_packets" in entries
    assert "packet=stream_index,pts_time,dts_time,duration_time" in entries
    assert ":format=" not in entries


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
    assert (
        max(window.height for window in windows)
        - min(window.height for window in windows)
        <= 1
    )
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


def test_ac3_decode_policy_is_applied_before_input_for_all_pcm_qc_commands() -> None:
    commands = (
        pcm_hash_command(Path("input.ac3"), 0, input_codec="AC-3"),
        analysis_command(
            Path("input.ec3"),
            0,
            Path("analysis.log"),
            input_codec="E-AC-3",
        ),
        spectrum_command(
            Path("input.ec3"),
            0,
            Path("spectrum.png"),
            start_seconds=Decimal(0),
            duration_seconds=Decimal(10),
            input_codec="eac3 secondary",
        ),
    )

    for command in commands:
        option = command.index("-drc_scale")
        assert command[option + 1] == "0"
        assert option < command.index("-i")


def test_non_ac3_pcm_qc_inputs_do_not_receive_decoder_drc_option() -> None:
    commands = (
        pcm_hash_command(Path("input.flac"), input_codec="flac"),
        analysis_command(
            Path("input.thd"), 0, Path("analysis.log"), input_codec="truehd"
        ),
        spectrum_command(
            Path("input.dts"),
            0,
            Path("spectrum.png"),
            start_seconds=Decimal(0),
            duration_seconds=Decimal(10),
            input_codec="dts",
        ),
    )

    assert all("-drc_scale" not in command for command in commands)


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


def test_timestamp_png_extraction_seeks_before_input_without_global_frame_scan() -> (
    None
):
    command = extract_png_at_timestamp_command(
        Path("encode.mkv"),
        Decimal("3301.256"),
        Path("frame.png"),
        hdr_native=True,
    )
    assert command.index("-ss") < command.index("-i")
    assert command.index("-seek_timestamp") < command.index("-ss")
    assert command[command.index("-seek_timestamp") + 1] == "1"
    assert command[command.index("-ss") + 1] == "3289.256"
    assert "-accurate_seek" in command
    assert command[command.index("-i") + 2 : command.index("-map")] == ["-ss", "12"]
    filters = command[command.index("-vf") + 1]
    assert "select=" not in filters
    assert "format=gbrp16le" in filters
    assert command[command.index("-frames:v") + 1] == "1"
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    assert command[command.index("-update") + 1] == "1"
    assert "-vsync" not in command
    assert command[-1] == "frame.png"


def test_release_grade_pair_plan_is_balanced_distributed_and_not_clustered() -> None:
    frames = _frames("IPB" * 800)
    pairs = select_frame_pairs(
        frames,
        frames,
        total_pairs=recommended_comparison_pair_count(24),
        timeline_frames=len(frames),
        dual_type_match=True,
    )
    assert len(pairs) == 24
    assert {
        category: sum(pair.category == category for pair in pairs) for category in "IPB"
    } == {"I": 8, "P": 8, "B": 8}
    indexes = [pair.presentation_index for pair in pairs]
    assert indexes == sorted(indexes)
    assert all(right - left >= 25 for left, right in zip(indexes, indexes[1:]))
    assert indexes[0] > len(frames) * 0.02
    assert indexes[-1] < len(frames) * 0.98


def test_release_grade_pair_count_has_explicit_bounds() -> None:
    with pytest.raises(ValueError, match="between 20 and 50"):
        recommended_comparison_pair_count(5)
    with pytest.raises(ValueError, match="between 20 and 50"):
        recommended_comparison_pair_count(51)


def test_native_yuv_metrics_crop_active_picture_and_keep_per_plane_stats() -> None:
    crop = CropMargins(left=220, right=220)
    command = native_yuv_metric_command(
        Path("reference.y4m"),
        Path("encode.y4m"),
        Path("sample.ssim.log"),
        Path("sample.psnr.log"),
        crop=crop,
    )
    filters = command[command.index("-filter_complex") + 1]
    assert filters.count("crop=iw-440:ih-0:220:0") == 2
    assert "gbr" not in filters and "rgb" not in filters
    assert "ssim=stats_file=" in filters and "psnr=stats_file=" in filters

    values = parse_ffmpeg_metric_stats("n:1 Y:0.991 U:0.998 V:0.997 All:0.993 (20.0)\n")
    assert values["Y"] == pytest.approx(0.991)
    assert values["U"] == pytest.approx(0.998)
    assert values["V"] == pytest.approx(0.997)
    assert values["All"] == pytest.approx(0.993)


def test_cropdetect_modal_result_and_native_y4m_extraction() -> None:
    crop = parse_cropdetect(
        "\n".join(
            [
                "frame=1 crop=1480:1080:220:0",
                "frame=2 crop=1478:1080:222:0",
                "frame=3 crop=1480:1080:220:0",
                "frame=4 crop=1480:1080:220:0",
            ]
        ),
        source_width=1920,
        source_height=1080,
    )
    assert crop == CropMargins(left=220, top=0, right=220, bottom=0)
    command = extract_y4m_at_timestamp_command(
        Path("encode.mkv"),
        Decimal("120"),
        Path("frame.y4m"),
        crop=crop,
    )
    assert command[command.index("-ss") + 1] == "108"
    assert command[command.index("-vf") + 1] == "crop=iw-440:ih-0:220:0"
    assert command[command.index("-f") + 1] == "yuv4mpegpipe"
    assert command[-1] == "frame.y4m"


def test_all_single_png_commands_use_non_sequence_image2_options() -> None:
    commands = (
        extract_png_command(Path("input.mkv"), 1, Path("native.png"), hdr_native=True),
        annotate_comparison_png_command(
            Path("native.png"),
            Path("proof.png"),
            image_role="ENCODE",
            presentation_index=1,
            pict_type="P",
        ),
    )
    for command in commands:
        assert "-vsync" not in command
        assert command[command.index("-fps_mode") + 1] == "passthrough"
        assert command[command.index("-update") + 1] == "1"


@pytest.mark.parametrize(
    ("image_role", "pict_type"),
    [("SOURCE", "I"), ("ENCODE", "P"), ("ENCODE", "B")],
)
def test_comparison_png_annotation_has_lossless_external_metadata_header(
    image_role: str, pict_type: str
) -> None:
    command = annotate_comparison_png_command(
        Path("clean.png"),
        Path("comparison.png"),
        image_role=image_role,  # type: ignore[arg-type]
        presentation_index=1234,
        pict_type=pict_type,
    )

    filters = command[command.index("-vf") + 1]
    assert "pad=iw:ih+max(40\\,ih/16)" in filters
    assert "fontsize=max(10\\,min(h/34\\,w/44))" in filters
    assert (
        f"text='{image_role} | 0-BASED INDEX 000001234 | {pict_type}-FRAME'" in filters
    )
    assert "format=rgb48be" in filters
    assert command[command.index("-pix_fmt") + 1] == "rgb48be"
    assert command[command.index("-c:v") + 1] == "png"
    assert command[-1] == "comparison.png"


def test_transformed_source_annotation_does_not_claim_a_native_picture_type() -> None:
    command = annotate_comparison_png_command(
        Path("clean.png"),
        Path("comparison.png"),
        image_role="SOURCE",
        presentation_index=42,
        pict_type="B",
        matched_to_type=True,
    )

    filters = command[command.index("-vf") + 1]
    assert "SOURCE | 0-BASED INDEX 000000042 | MATCHED TO B-FRAME" in filters

    with pytest.raises(ValueError, match="only for SOURCE"):
        annotate_comparison_png_command(
            Path("clean.png"),
            Path("comparison.png"),
            image_role="ENCODE",
            presentation_index=42,
            pict_type="B",
            matched_to_type=True,
        )


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
