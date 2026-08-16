"""Command construction for reference remux and x264/x265 encoding."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from bdencode.audio import audio_decode_input_args, audio_encode_args
from bdencode.media.profiles import EncoderSettings


@dataclass(frozen=True, slots=True)
class PcmBlurayAudio:
    """One Blu-ray PCM stream that needs a Matroska-compatible representation."""

    ordinal: int
    bit_depth: int

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise ValueError("audio stream ordinal must be non-negative")
        if self.bit_depth not in {16, 20, 24}:
            raise ValueError("Blu-ray PCM bit depth must be 16, 20 or 24")

    @property
    def ffmpeg_codec(self) -> str:
        # FFmpeg has no packed 20-bit PCM encoder. pcm_s24le preserves all 20
        # significant bits without loss; 16-bit material must not be padded.
        return "pcm_s16le" if self.bit_depth == 16 else "pcm_s24le"


@dataclass(frozen=True, slots=True)
class ReferenceRemuxPlan:
    disc_root: Path
    playlist_id: str
    output_path: Path
    angle: int = 1
    pcm_bluray_audio: tuple[PcmBlurayAudio, ...] = ()

    def __post_init__(self) -> None:
        if not self.playlist_id.isdigit():
            raise ValueError("playlist_id must be numeric")
        if self.angle < 1:
            raise ValueError("angle must be at least one")
        ordinals = tuple(item.ordinal for item in self.pcm_bluray_audio)
        if tuple(sorted(set(ordinals))) != ordinals:
            raise ValueError("audio stream ordinals must be sorted and unique")


def reference_remux_command(
    plan: ReferenceRemuxPlan, *, ffmpeg: str = "ffmpeg"
) -> list[str]:
    """Materialize the selected libbluray timeline without changing media data."""
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-playlist",
        str(int(plan.playlist_id)),
        "-angle",
        str(plan.angle),
        "-i",
        f"bluray:{plan.disc_root.as_posix()}",
        "-ignore_unknown",
        "-map",
        "0",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "0",
        "-c",
        "copy",
    ]
    # Matroska cannot carry FFmpeg's pcm_bluray codec directly. Convert only
    # those audio streams to an equivalent lossless, container-native PCM
    # representation; video, subtitles, and other audio codecs stay bit-exact.
    for stream in plan.pcm_bluray_audio:
        command.extend((f"-c:a:{stream.ordinal}", stream.ffmpeg_codec))
    command.extend(
        [
            "-avoid_negative_ts",
            "make_zero",
            "-max_interleave_delta",
            "0",
            "-y",
            str(plan.output_path),
        ]
    )
    return command


def encode_pipeline_commands(
    script_path: Path,
    output_path: Path,
    settings: EncoderSettings,
    *,
    metadata: Mapping[str, Any] | None = None,
    vspipe: str = "vspipe",
    ffmpeg: str = "ffmpeg",
) -> list[list[str]]:
    if settings.bframes < 1:
        raise ValueError("B-frames are mandatory because I/P/B comparison is required")
    vs_command = [vspipe, "--container", "y4m", str(script_path), "-"]
    encode_command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-stats_period",
        "2",
        "-progress",
        "pipe:2",
        "-v",
        "info",
        "-f",
        "yuv4mpegpipe",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-an",
        *settings.ffmpeg_video_args(),
        "-map_metadata",
        "-1",
    ]
    if metadata:
        for key, value in sorted(metadata.items()):
            if not key.replace("_", "").isalnum():
                raise ValueError(f"unsafe metadata key: {key}")
            encode_command.extend(
                (
                    "-metadata",
                    f"{key}={json.dumps(value, sort_keys=True) if not isinstance(value, str) else value}",
                )
            )
    encode_command.extend(("-y", str(output_path)))
    return [vs_command, encode_command]


def audio_track_command(
    reference_path: Path,
    stream_ordinal: int,
    output_path: Path,
    *,
    action: str,
    source_codec: str = "unknown",
    source_profile: str | None = None,
    source_channels: int | None = None,
    source_sample_rate: int | None = None,
    source_bit_depth: int | None = None,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    if stream_ordinal < 0:
        raise ValueError("stream_ordinal cannot be negative")
    decoder_args = audio_decode_input_args(source_codec)
    codec_args = audio_encode_args(
        action,
        source_codec=source_codec,
        source_profile=source_profile,
        source_channels=source_channels,
        source_sample_rate=source_sample_rate,
        source_bit_depth=source_bit_depth,
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-xerror",
        "-err_detect",
        "explode",
        "-copyts",
        *decoder_args,
        "-i",
        str(reference_path),
        "-map",
        f"0:a:{stream_ordinal}",
        "-vn",
        "-sn",
        "-dn",
        *codec_args,
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-f",
        "matroska",
        "-y",
        str(output_path),
    ]


def subtitle_track_command(
    reference_path: Path,
    stream_ordinal: int,
    output_path: Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    if stream_ordinal < 0:
        raise ValueError("stream_ordinal cannot be negative")
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-xerror",
        "-err_detect",
        "explode",
        "-copyts",
        "-i",
        str(reference_path),
        "-map",
        f"0:s:{stream_ordinal}",
        "-vn",
        "-an",
        "-dn",
        "-c:s",
        "copy",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-f",
        "matroska",
        "-y",
        str(output_path),
    ]
