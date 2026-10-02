"""Find the Dolby Vision layer of dual-layer UHD Blu-rays (profile 7).

On a profile 7 disc the Dolby Vision RPUs live in a secondary 1080p HEVC video stream (the enhancement
layer, EL); the 4K base layer (BL) carries none, and neither FFmpeg nor the libbluray scan flags the
disc. The scan therefore looks at the secondary HEVC streams of the main playlists: it copies the first
frames of each candidate, asks ``dovi_tool`` for the RPU it holds and, for a profile 7 RPU, marks the
base layer as Dolby Vision and records which stream holds the metadata. Everything fails soft: a
missing ``dovi_tool`` or a stream without an RPU leaves the scan as it was.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .hdr_dynamic import DOVI_TOOL, DynamicHdrError, dovi_summary_command
from .media.bluray import DiscKind, DiscScan, MediaStream, PlaylistCandidate, VideoCodec
from .process import ProcessFailure

LOG = logging.getLogger(__name__)

PROBE_FRAMES = 120
_PROFILE_RE = re.compile(r"(?im)^\s*Profile:\s*(\d+)(?:\s*\((MEL|FEL)\))?")
_FRAMES_RE = re.compile(r"(?im)^\s*Frames:\s*(\d+)\s*$")


@dataclass(frozen=True, slots=True)
class DolbyVisionLayer:
    profile: int
    el_type: str | None
    frames: int


def parse_layer_summary(text: str) -> DolbyVisionLayer:
    """Profile, enhancement-layer type (MEL/FEL) and frame count from ``dovi_tool info --summary``."""

    profile = _PROFILE_RE.search(text)
    frames = _FRAMES_RE.search(text)
    if profile is None or frames is None:
        raise DynamicHdrError("invalid_metadata", "dovi_tool did not report a profile and a frame count")
    return DolbyVisionLayer(int(profile.group(1)), profile.group(2), int(frames.group(1)))


def enhancement_layer_candidates(playlist: PlaylistCandidate) -> tuple[tuple[int, MediaStream], ...]:
    """Secondary HEVC video streams with the base layer's frame rate and at most its size.

    Returns ``(ordinal among the playlist's video streams, stream)``; the ordinal is also the stream's
    position in the reference remux, which keeps every stream in playlist order.
    """

    videos = playlist.video_streams
    if len(videos) < 2 or videos[0].video is None or videos[0].video.codec is not VideoCodec.HEVC:
        return ()
    base = videos[0].video
    found: list[tuple[int, MediaStream]] = []
    for ordinal, stream in enumerate(videos[1:], start=1):
        video = stream.video
        if video is None or video.codec is not VideoCodec.HEVC or video.frame_rate != base.frame_rate:
            continue
        if (video.width or 0) > (base.width or 0) or (video.height or 0) > (base.height or 0):
            continue
        found.append((ordinal, stream))
    return tuple(found)


def layer_probe_commands(
    disc_root: Path,
    playlist_id: str,
    ordinal: int,
    rpu: Path,
    *,
    frames: int = PROBE_FRAMES,
    ffmpeg: str = "ffmpeg",
    tool: str = DOVI_TOOL,
) -> list[list[str]]:
    """Copy the first frames of one video stream of the playlist and extract their RPUs."""

    return [
        [
            ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-playlist", str(int(playlist_id)),
            "-i", f"bluray:{disc_root.as_posix()}", "-map", f"0:v:{ordinal}", "-c", "copy",
            "-bsf:v", "hevc_mp4toannexb", "-frames:v", str(frames), "-f", "hevc", "-",
        ],
        [tool, "extract-rpu", "-o", str(rpu), "-"],
    ]


def with_dolby_vision_layer(
    playlist: PlaylistCandidate, ordinal: int, layer: DolbyVisionLayer
) -> PlaylistCandidate:
    """The playlist with its base-layer video marked Dolby Vision and pointed at the EL stream."""

    videos = playlist.video_streams
    base, enhancement = videos[0], videos[ordinal]
    if base.video is None:
        return playlist
    marked = replace(
        base,
        video=replace(
            base.video,
            dolby_vision=True,
            dolby_vision_profile=layer.profile,
            dolby_vision_el_stream_id=enhancement.id,
            dolby_vision_el_type=layer.el_type,
        ),
    )
    return replace(
        playlist,
        streams=tuple(marked if item.id == base.id else item for item in playlist.streams),
    )


def detect_dolby_vision_layers(
    scan: DiscScan,
    disc_root: Path,
    *,
    run_pipeline: Callable[..., Any],
    run: Callable[..., Any],
    read_text: Callable[[Path], str],
    work: Path,
    logs: Path,
    tool_available: bool,
) -> DiscScan:
    """Mark the playlists of a dual-layer Dolby Vision UHD disc; leave everything else unchanged.

    Only the main playlists are probed (the recommended one and any at least half as long), and only
    UHD discs: a few seconds of copying per candidate, not a second scan of the disc.
    """

    if scan.disc_kind is not DiscKind.UHD or not tool_available:
        return scan
    longest = max((item.duration_seconds for item in scan.playlists if item.recommended), default=0.0)
    if longest <= 0.0:
        longest = max((item.duration_seconds for item in scan.playlists), default=0.0)
    work.mkdir(mode=0o750, parents=True, exist_ok=True)
    updated: list[PlaylistCandidate] = []
    for playlist in scan.playlists:
        candidates = enhancement_layer_candidates(playlist)
        if not candidates or playlist.duration_seconds < longest * 0.5:
            updated.append(playlist)
            continue
        for ordinal, stream in candidates:
            label = f"{playlist.playlist_id}-{ordinal}"
            rpu = work / f"probe-{label}.rpu"
            summary = work / f"probe-{label}.txt"
            try:
                run_pipeline(
                    layer_probe_commands(disc_root, playlist.playlist_id, ordinal, rpu),
                    cwd=work,
                    stderr_paths=[logs / f"dolby-vision-probe-{label}-source.log",
                                  logs / f"dolby-vision-probe-{label}.log"],
                )
                run(dovi_summary_command(rpu), cwd=work, stdout_path=summary,
                    stderr_path=logs / f"dolby-vision-probe-{label}-summary.log")
                layer = parse_layer_summary(read_text(summary))
            except (ProcessFailure, DynamicHdrError, OSError, ValueError) as exc:
                # No RPU in this stream (an ordinary secondary video) or an unreadable one.
                LOG.info("playlist %s stream %s carries no Dolby Vision RPU: %s", playlist.playlist_id, stream.id, exc)
                continue
            finally:
                rpu.unlink(missing_ok=True)
            if layer.profile == 7:
                LOG.info("playlist %s: Dolby Vision profile 7 (%s) in %s", playlist.playlist_id, layer.el_type, stream.id)
                playlist = with_dolby_vision_layer(playlist, ordinal, layer)
                break
        updated.append(playlist)
    return replace(scan, playlists=tuple(updated))
