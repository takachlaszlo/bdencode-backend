from __future__ import annotations

import json
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from bdencode.encode import (
    PcmBlurayAudio,
    ReferenceRemuxPlan,
    audio_track_command,
    encode_pipeline_commands,
    reference_remux_command,
    subtitle_track_command,
)
from bdencode.media.profiles import VideoEncoder, recommended_profile
from bdencode.mux import (
    CommonTimelinePlan,
    FinalTrackPolicy,
    FinalVideoPolicy,
    MuxTrack,
    inspection_commands,
    mkvmerge_command,
    parse_stream_start_times,
    plan_common_zero_timeline,
    stream_start_probe_command,
    validate_ffprobe_stream_policy,
    validate_hdr10_side_data,
    validate_mkvmerge_identification,
    validate_mux_track_policy,
    validate_stream_start_times,
)
from bdencode.vapoursynth import (
    Crop,
    ReferenceScriptPlan,
    TemporalFilter,
    render_reference_script,
)


def test_reference_remux_uses_libbluray_without_shell() -> None:
    plan = ReferenceRemuxPlan(Path("/storage/disc"), "00800", Path("reference.mkv"), 2)
    command = reference_remux_command(plan)
    assert command[command.index("-playlist") + 1] == "800"
    assert "bluray:/storage/disc" in command
    assert "-c" in command and "copy" in command


def test_reference_remux_converts_only_bluray_pcm_for_matroska() -> None:
    plan = ReferenceRemuxPlan(
        Path("/storage/disc"),
        "00800",
        Path("reference.mkv"),
        pcm_bluray_audio=(PcmBlurayAudio(0, 16), PcmBlurayAudio(2, 24)),
    )

    command = reference_remux_command(plan)

    assert command[command.index("-c") + 1] == "copy"
    assert command[command.index("-c:a:0") + 1] == "pcm_s16le"
    assert command[command.index("-c:a:2") + 1] == "pcm_s24le"
    assert "-c:a:1" not in command


@pytest.mark.parametrize(
    "streams",
    [
        (PcmBlurayAudio(1, 16), PcmBlurayAudio(0, 24)),
        (PcmBlurayAudio(0, 16), PcmBlurayAudio(0, 24)),
    ],
)
def test_reference_remux_rejects_invalid_audio_ordinals(
    streams: tuple[PcmBlurayAudio, ...],
) -> None:
    with pytest.raises(ValueError, match="audio stream ordinals"):
        ReferenceRemuxPlan(
            Path("/storage/disc"),
            "00800",
            Path("reference.mkv"),
            pcm_bluray_audio=streams,
        )


def test_bluray_pcm_requires_a_valid_ordinal_and_reviewed_bit_depth() -> None:
    with pytest.raises(ValueError, match="ordinal"):
        PcmBlurayAudio(-1, 16)
    with pytest.raises(ValueError, match="bit depth"):
        PcmBlurayAudio(0, 32)


def test_vapoursynth_script_is_frame_server_and_crop_auditable(tmp_path: Path) -> None:
    plan = ReferenceScriptPlan(
        Path("source.mkv"),
        tmp_path / "index",
        tmp_path / "source.vpy",
        temporal_filter=TemporalFilter.IVTC_TFF,
        crop=Crop(2, 4, 2, 4),
    )
    script = render_reference_script(plan)
    assert "core.bs.VideoSource" in script
    assert "core.vivtc.VFM" in script
    assert "core.vivtc.VDecimate" in script
    assert "CropRel" in script
    assert "exporttimestamps" not in script


def test_encode_pipeline_has_no_shell_syntax() -> None:
    settings = recommended_profile(VideoEncoder.X264)
    commands = encode_pipeline_commands(Path("source.vpy"), Path("video.mkv"), settings)
    assert len(commands) == 2
    assert commands[0][-1] == "-"
    assert "pipe:0" in commands[1]
    assert commands[1][commands[1].index("-progress") + 1] == "pipe:2"
    assert commands[1][commands[1].index("-stats_period") + 1] == "2"
    assert "-nostats" in commands[1]
    assert all("|" not in argument for command in commands for argument in command)


def test_subtitle_sidecar_forces_matroska_muxer() -> None:
    command = subtitle_track_command(
        Path("reference.mkv"), 3, Path("track-03-subtitle.mks")
    )

    assert command[command.index("-c:s") + 1] == "copy"
    assert "-copyts" in command
    assert command[-4:] == [
        "-f",
        "matroska",
        "-y",
        "track-03-subtitle.mks",
    ]


def test_ffmpeg_can_write_mks_sidecar_with_explicit_matroska_muxer(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg and ffprobe are required for the subtitle muxer smoke test")

    subtitle = tmp_path / "subtitle.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nBDEncode smoke test\n",
        encoding="utf-8",
    )
    reference = tmp_path / "reference.mkv"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(subtitle),
            "-map",
            "0:s:0",
            "-c:s",
            "copy",
            "-f",
            "matroska",
            "-y",
            str(reference),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    sidecar = tmp_path / "track-01-subtitle.mks"
    subprocess.run(
        subtitle_track_command(reference, 0, sidecar, ffmpeg=ffmpeg),
        check=True,
        capture_output=True,
        text=True,
    )
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "json",
            str(sidecar),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(probe.stdout)["streams"] == [
        {"codec_name": "subrip", "codec_type": "subtitle"}
    ]


def test_full_decode_treats_decoder_errors_as_fatal() -> None:
    commands = inspection_commands(Path("final.mkv"), Path("reports"))
    command = next(
        argv for argv, report in commands if report.name == "full-decode.log"
    )
    assert "-xerror" in command
    assert command[command.index("-err_detect") + 1] == "explode"
    assert "0:v?" in command
    assert "0:a?" in command
    assert command.count("0:v?") == 1
    assert command.count("0:a?") == 1
    assert "0" not in (
        command[index + 1] for index, item in enumerate(command) if item == "-map"
    )


def test_mux_never_embeds_logs_or_internal_metadata() -> None:
    command = mkvmerge_command(
        Path("final.mkv"),
        Path("video.mkv"),
        audio_tracks=[MuxTrack(Path("audio.flac"), "hu", "Hungarian", True)],
    )
    assert "--attach-file" not in command
    assert "--global-tags" not in command
    assert "--no-global-tags" in command
    assert "--no-attachments" in command
    assert "0:hu" in command
    assert "--no-chapters" in command
    assert "--no-global-tags" in command
    assert "--no-attachments" in command
    video_input_index = command.index("video.mkv")
    assert command[video_input_index - 2 : video_input_index] != [
        "--default-track-flag",
        "0:no",
    ]
    assert command[command.index("--default-track-flag") + 1] == "0:yes"


def test_common_timeline_rebases_earliest_av_start_without_losing_offsets() -> None:
    plan = plan_common_zero_timeline(
        Decimal("0.042"),
        [Decimal("0.037"), Decimal("12.500")],
    )

    assert plan == CommonTimelinePlan(
        origin_seconds=Decimal("0.037"),
        expected_start_seconds=(
            Decimal("0.005"),
            Decimal("0.000"),
            Decimal("12.463"),
        ),
        video_sync_offset_ms=Decimal("5.000"),
        track_sync_offsets_ms=(Decimal("-37.000"), Decimal("-37.000")),
    )

    command = mkvmerge_command(
        Path("final.mkv"),
        Path("video.mkv"),
        video_sync_offset_ms=plan.video_sync_offset_ms,
        audio_tracks=[
            MuxTrack(
                Path("audio.mka"),
                "en",
                default=True,
                sync_offset_ms=plan.track_sync_offsets_ms[0],
            )
        ],
        subtitle_tracks=[
            MuxTrack(
                Path("subtitle.mks"),
                "en",
                sync_offset_ms=plan.track_sync_offsets_ms[1],
            )
        ],
    )
    sync_values = [
        command[index + 1] for index, value in enumerate(command) if value == "--sync"
    ]
    assert sync_values == ["0:5", "0:-37", "0:-37"]


def test_common_timeline_compensates_for_audio_encoder_priming() -> None:
    plan = plan_common_zero_timeline(
        Decimal("0.042"),
        [Decimal("0.042")],
        encoded_video_start=Decimal("0"),
        sidecar_start_times=[Decimal("0.037")],
    )

    assert plan.expected_start_seconds == (Decimal("0"), Decimal("0"))
    assert plan.video_sync_offset_ms == 0
    assert plan.track_sync_offsets_ms == (Decimal("-37"),)


def test_reference_stream_start_probe_is_indexed_and_strict() -> None:
    command = stream_start_probe_command(Path("reference.mkv"))
    assert command[command.index("-show_entries") + 1] == (
        "stream=index,codec_type,start_time"
    )
    assert parse_stream_start_times(
        {
            "streams": [
                {"index": 0, "codec_type": "video", "start_time": "0.042"},
                {"index": 2, "codec_type": "audio", "start_time": "0.037"},
            ]
        }
    ) == {0: Decimal("0.042"), 2: Decimal("0.037")}

    with pytest.raises(ValueError, match="no presentation start_time"):
        parse_stream_start_times({"streams": [{"index": 0, "start_time": "N/A"}]})


def test_mux_requires_an_audio_default_and_reviewed_forced_subtitle() -> None:
    no_default = [MuxTrack(Path("original.mka"), "ja")]
    assert validate_mux_track_policy(audio_tracks=no_default, subtitle_tracks=[]) == (
        "exactly one retained audio track must be default",
    )
    with pytest.raises(ValueError, match="audio track must be default"):
        mkvmerge_command(Path("final.mkv"), Path("video.mkv"), audio_tracks=no_default)

    two_defaults = [
        MuxTrack(Path("original.mka"), "ja", default=True),
        MuxTrack(Path("dub.mka"), "en", default=True),
    ]
    assert validate_mux_track_policy(audio_tracks=two_defaults, subtitle_tracks=[]) == (
        "exactly one retained audio track must be default",
    )

    unreviewed = MuxTrack(Path("subtitle.mks"), "en", forced=True)
    full = MuxTrack(Path("full.mks"), "en", forced=True, subtitle_kind="full")
    reviewed = MuxTrack(Path("forced.mks"), "en", forced=True, subtitle_kind="forced")
    reviewed_without_flag = MuxTrack(
        Path("forced-without-flag.mks"),
        "en",
        forced=False,
        subtitle_kind="forced",
    )
    assert validate_mux_track_policy(audio_tracks=[], subtitle_tracks=[unreviewed])
    assert validate_mux_track_policy(audio_tracks=[], subtitle_tracks=[full])
    assert validate_mux_track_policy(audio_tracks=[], subtitle_tracks=[reviewed]) == ()
    assert validate_mux_track_policy(
        audio_tracks=[], subtitle_tracks=[reviewed_without_flag]
    ) == ("reviewed forced subtitle track 1 must set the forced flag",)


def test_final_stream_starts_use_common_zero_and_preserve_relative_timing() -> None:
    document = {
        "streams": [
            {"codec_type": "video", "start_time": "0.005"},
            {"codec_type": "audio", "start_time": "0.000"},
            {"codec_type": "subtitle", "start_time": "12.463"},
            {"codec_type": "attachment", "start_time": "N/A"},
        ]
    }
    expected = [Decimal("0.005"), Decimal("0"), Decimal("12.463")]

    assert validate_stream_start_times(document, expected_start_times=expected) == ()

    broken = {
        "streams": [
            {"codec_type": "video", "start_time": "0.000"},
            {"codec_type": "audio", "start_time": "0.037"},
            {"codec_type": "subtitle", "start_time": "12.500"},
        ]
    }
    errors = validate_stream_start_times(broken, expected_start_times=expected)
    assert any("stream 1 relative start differs" in item for item in errors)


def test_audio_sidecar_preserves_16_bit_flac_and_reference_timestamps() -> None:
    command = audio_track_command(
        Path("reference.mkv"),
        2,
        Path("audio.mka"),
        action="flac",
        source_codec="pcm_s16le",
        source_channels=2,
        source_sample_rate=48_000,
        source_bit_depth=16,
    )

    assert "-copyts" in command
    assert command[command.index("-sample_fmt") + 1] == "s16"


@pytest.mark.parametrize("source_codec", ("ac3", "AC-3", "E-AC-3 secondary"))
def test_audio_sidecar_disables_ac3_family_decoder_drc(source_codec: str) -> None:
    command = audio_track_command(
        Path("reference.mkv"),
        0,
        Path("audio.mka"),
        action="flac",
        source_codec=source_codec,
        source_channels=6,
        source_sample_rate=48_000,
    )

    option = command.index("-drc_scale")
    assert command[option + 1] == "0"
    assert option < command.index("-i")


def test_audio_sidecar_does_not_apply_ac3_drc_to_truehd() -> None:
    command = audio_track_command(
        Path("reference.mkv"),
        0,
        Path("audio.mka"),
        action="flac",
        source_codec="truehd",
        source_channels=8,
        source_sample_rate=48_000,
    )

    assert "-drc_scale" not in command


def test_final_mkv_topology_matches_reviewed_track_flags() -> None:
    audio = MuxTrack(Path("audio.mka"), "hu", "Hungarian FLAC", True, False)
    subtitle = MuxTrack(
        Path("subtitle.mks"),
        "en",
        "English",
        False,
        True,
        subtitle_kind="forced",
    )
    document = {
        "container": {"properties": {"title": "Movie.Encode"}},
        "tracks": [
            {
                "id": 0,
                "type": "video",
                "properties": {"default_track": True, "forced_track": False},
            },
            {
                "id": 1,
                "type": "audio",
                "properties": {
                    "language_ietf": "hu",
                    "track_name": "Hungarian FLAC",
                    "default_track": True,
                    "forced_track": False,
                },
            },
            {
                "id": 2,
                "type": "subtitles",
                "properties": {
                    "language_ietf": "en",
                    "track_name": "English",
                    "default_track": False,
                    "forced_track": True,
                },
            },
        ],
        "attachments": [],
    }
    assert (
        validate_mkvmerge_identification(
            document,
            audio_tracks=[audio],
            subtitle_tracks=[subtitle],
            title="Movie.Encode",
        )
        == ()
    )


def test_final_stream_and_hdr10_policy_is_exact() -> None:
    streams = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "profile": "Main 10",
                "width": 3840,
                "height": 2160,
                "pix_fmt": "yuv420p10le",
                "color_range": "tv",
                "color_space": "bt2020nc",
                "color_transfer": "smpte2084",
                "color_primaries": "bt2020",
                "chroma_location": "left",
            },
            {"codec_type": "audio", "codec_name": "flac"},
            {
                "codec_type": "attachment",
                "codec_name": "unknown",
                "tags": {"filename": "encode.log"},
            },
        ]
    }
    policy = FinalVideoPolicy(
        "hevc",
        "Main 10",
        3840,
        2160,
        "yuv420p10le",
        "tv",
        "bt2020nc",
        "smpte2084",
        "bt2020",
        "left",
    )
    assert (
        validate_ffprobe_stream_policy(
            streams,
            video=policy,
            media_tracks=[FinalTrackPolicy("audio", "flac")],
        )
        == ()
    )
    frames = {
        "frames": [
            {
                "side_data_list": [
                    {
                        "side_data_type": "Mastering display metadata",
                        "green_x": "8500/50000",
                        "green_y": "39850/50000",
                        "blue_x": "6550/50000",
                        "blue_y": "2300/50000",
                        "red_x": "35400/50000",
                        "red_y": "14600/50000",
                        "white_point_x": "15635/50000",
                        "white_point_y": "16450/50000",
                        "max_luminance": "10000000/10000",
                        "min_luminance": "1/10000",
                    },
                    {
                        "side_data_type": "Content light level metadata",
                        "max_content": 1000,
                        "max_average": 400,
                    },
                ]
            }
        ]
    }
    mastering = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)"
    assert (
        validate_hdr10_side_data(
            streams,
            frames,
            enabled=True,
            mastering_display=mastering,
            max_cll=1000,
            max_fall=400,
        )
        == ()
    )
