"""Command construction for reference remux and x264/x265 encoding."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from bdencode.media.profiles import EncoderSettings


@dataclass(frozen=True, slots=True)
class ReferenceRemuxPlan:
    disc_root: Path
    playlist_id: str
    output_path: Path
    angle: int = 1

    def __post_init__(self) -> None:
        if not self.playlist_id.isdigit():
            raise ValueError("playlist_id must be numeric")
        if self.angle < 1:
            raise ValueError("angle must be at least one")


def reference_remux_command(
    plan: ReferenceRemuxPlan, *, ffmpeg: str = "ffmpeg"
) -> list[str]:
    """Materialize the selected libbluray timeline without changing media data."""
    return [
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
        "-map",
        "0",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-max_interleave_delta",
        "0",
        "-y",
        str(plan.output_path),
    ]


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
    stream_index: int,
    output_path: Path,
    *,
    action: str,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    if stream_index < 0:
        raise ValueError("stream_index cannot be negative")
    normalized = action.lower()
    if normalized not in {"copy", "flac"}:
        raise ValueError("audio action must be copy or flac")
    codec_args = (
        ["-c:a", "copy"]
        if normalized == "copy"
        else ["-c:a", "flac", "-compression_level", "8"]
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-copyts",
        "-i",
        str(reference_path),
        "-map",
        f"0:{stream_index}",
        "-vn",
        "-sn",
        "-dn",
        *codec_args,
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-y",
        str(output_path),
    ]


def subtitle_track_command(
    reference_path: Path,
    stream_index: int,
    output_path: Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    if stream_index < 0:
        raise ValueError("stream_index cannot be negative")
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-copyts",
        "-i",
        str(reference_path),
        "-map",
        f"0:{stream_index}",
        "-vn",
        "-an",
        "-dn",
        "-c:s",
        "copy",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-y",
        str(output_path),
    ]
