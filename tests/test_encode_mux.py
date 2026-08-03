from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bdencode.encode import (
    ReferenceRemuxPlan,
    encode_pipeline_commands,
    reference_remux_command,
    subtitle_track_command,
)
from bdencode.media.profiles import VideoEncoder, recommended_profile
from bdencode.mux import (
    FinalTrackPolicy,
    FinalVideoPolicy,
    MuxTrack,
    inspection_commands,
    mkvmerge_command,
    validate_ffprobe_stream_policy,
    validate_hdr10_side_data,
    validate_mkvmerge_identification,
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


def test_mux_attaches_only_sanitized_log() -> None:
    command = mkvmerge_command(
        Path("final.mkv"),
        Path("video.mkv"),
        audio_tracks=[MuxTrack(Path("audio.flac"), "hu", "Hungarian", True)],
        sanitized_log_path=Path("encode.log"),
    )
    assert "encode.log" in command
    assert not any(
        "comparison" in value.lower() and value.endswith(".json") for value in command
    )
    assert "0:hu" in command
    assert "--no-chapters" in command
    assert "--no-global-tags" in command
    assert "--no-attachments" in command


def test_final_mkv_topology_matches_reviewed_track_flags() -> None:
    audio = MuxTrack(Path("audio.mka"), "hu", "Hungarian FLAC", True, False)
    subtitle = MuxTrack(Path("subtitle.mks"), "en", "English", False, True)
    document = {
        "container": {"properties": {"title": "Movie.Encode"}},
        "tracks": [
            {"id": 0, "type": "video", "properties": {}},
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
        "attachments": [
            {"file_name": "encode.log", "content_type": "text/plain; charset=utf-8"}
        ],
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
