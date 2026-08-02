"""MKV assembly, tags, and post-mux inspection commands."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class MuxTrack:
    path: Path
    language: str = "und"
    name: str | None = None
    default: bool = False
    forced: bool = False

    def __post_init__(self) -> None:
        if not self.language or len(self.language) > 35:
            raise ValueError("invalid BCP 47 language tag")


@dataclass(frozen=True, slots=True)
class FinalVideoPolicy:
    codec_name: str
    profile: str
    width: int
    height: int
    pixel_format: str
    color_range: str
    color_space: str
    color_transfer: str
    color_primaries: str
    chroma_location: str


@dataclass(frozen=True, slots=True)
class FinalTrackPolicy:
    codec_type: str
    codec_name: str


def mkvmerge_command(
    output_path: Path,
    video_path: Path,
    *,
    audio_tracks: Sequence[MuxTrack] = (),
    subtitle_tracks: Sequence[MuxTrack] = (),
    chapters_path: Path | None = None,
    tags_path: Path | None = None,
    sanitized_log_path: Path | None = None,
    title: str | None = None,
    mkvmerge: str = "mkvmerge",
) -> list[str]:
    command = [mkvmerge, "--output", str(output_path)]
    if title:
        command.extend(("--title", title))
    if chapters_path:
        command.extend(("--chapters", str(chapters_path)))
    if tags_path:
        command.extend(("--global-tags", str(tags_path)))
    command.extend(
        (
            "--no-audio",
            "--no-subtitles",
            "--no-buttons",
            "--no-chapters",
            "--no-global-tags",
            "--no-attachments",
            str(video_path),
        )
    )
    for item in audio_tracks:
        command.extend(_track_options(item))
        command.extend(
            (
                "--no-video",
                "--no-subtitles",
                "--no-buttons",
                "--no-chapters",
                "--no-global-tags",
                "--no-attachments",
                str(item.path),
            )
        )
    for item in subtitle_tracks:
        command.extend(_track_options(item))
        command.extend(
            (
                "--no-video",
                "--no-audio",
                "--no-buttons",
                "--no-chapters",
                "--no-global-tags",
                "--no-attachments",
                str(item.path),
            )
        )
    if sanitized_log_path:
        command.extend(
            (
                "--attachment-mime-type",
                "text/plain; charset=utf-8",
                "--attachment-name",
                "encode.log",
                "--attachment-description",
                "Sanitized BDEncode log; comparison evidence remains external",
                "--attach-file",
                str(sanitized_log_path),
            )
        )
    return command


def _track_options(item: MuxTrack) -> tuple[str, ...]:
    values = [
        "--language",
        f"0:{item.language}",
        "--default-track-flag",
        f"0:{'yes' if item.default else 'no'}",
        "--forced-display-flag",
        f"0:{'yes' if item.forced else 'no'}",
    ]
    if item.name:
        values.extend(("--track-name", f"0:{item.name}"))
    return tuple(values)


def inspection_commands(
    output_path: Path,
    report_root: Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    mediainfo: str = "mediainfo",
    mkvmerge: str = "mkvmerge",
    mkvinfo: str = "mkvinfo",
) -> list[tuple[list[str], Path]]:
    return [
        ([mediainfo, "--Full", str(output_path)], report_root / "mediainfo.txt"),
        (
            [
                mkvmerge,
                "--identify",
                "--identification-format",
                "json",
                str(output_path),
            ],
            report_root / "mkvmerge-identify.json",
        ),
        ([mkvinfo, str(output_path)], report_root / "mkvinfo.txt"),
        (
            [
                ffprobe,
                "-v",
                "error",
                "-show_streams",
                "-show_entries",
                "stream=index,codec_name,profile,codec_type,width,height,pix_fmt,color_range,color_space,color_transfer,color_primaries,chroma_location,bits_per_raw_sample:stream_side_data",
                "-of",
                "json",
                str(output_path),
            ],
            report_root / "ffprobe-streams.json",
        ),
        (
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-read_intervals",
                "%+2",
                "-show_frames",
                "-show_entries",
                "frame=media_type:frame_side_data",
                "-of",
                "json",
                str(output_path),
            ],
            report_root / "ffprobe-video-side-data.json",
        ),
        (
            [
                ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-i",
                str(output_path),
                "-map",
                "0:v?",
                "-map",
                "0:a?",
                "-f",
                "null",
                "-",
            ],
            report_root / "full-decode.log",
        ),
    ]


def validate_mkvmerge_identification(
    document: Mapping[str, Any],
    *,
    audio_tracks: Sequence[MuxTrack],
    subtitle_tracks: Sequence[MuxTrack],
    title: str,
) -> tuple[str, ...]:
    """Compare final Matroska topology and flags with the reviewed mux plan."""

    errors: list[str] = []
    tracks = document.get("tracks")
    if not isinstance(tracks, list):
        return ("mkvmerge identification has no track array",)
    expected_types = [
        "video",
        *("audio" for _ in audio_tracks),
        *("subtitles" for _ in subtitle_tracks),
    ]
    actual_types = [item.get("type") for item in tracks if isinstance(item, Mapping)]
    if actual_types != expected_types:
        errors.append(
            f"track topology differs: expected {expected_types}, got {actual_types}"
        )

    expected_media = [*audio_tracks, *subtitle_tracks]
    actual_media = [
        item
        for item in tracks
        if isinstance(item, Mapping) and item.get("type") != "video"
    ]
    for index, (actual, expected) in enumerate(
        zip(actual_media, expected_media, strict=False), start=1
    ):
        properties = actual.get("properties", {})
        if not isinstance(properties, Mapping):
            errors.append(f"track {index} has no properties")
            continue
        actual_language = properties.get("language_ietf") or properties.get("language")
        if str(actual_language or "und").casefold() != expected.language.casefold():
            errors.append(
                f"track {index} language differs: expected {expected.language}, got {actual_language}"
            )
        if bool(properties.get("default_track", False)) is not expected.default:
            errors.append(f"track {index} default flag differs")
        if bool(properties.get("forced_track", False)) is not expected.forced:
            errors.append(f"track {index} forced flag differs")
        if expected.name is not None and properties.get("track_name") != expected.name:
            errors.append(f"track {index} name differs")

    attachments = document.get("attachments", [])
    if not isinstance(attachments, list):
        attachments = []
    names = [item.get("file_name") for item in attachments if isinstance(item, Mapping)]
    if names != ["encode.log"]:
        errors.append(
            f"attachment policy differs: expected only encode.log, got {names}"
        )
    elif not str(attachments[0].get("content_type", "")).startswith("text/plain"):
        errors.append("encode.log attachment MIME type is not text/plain")

    container = document.get("container", {})
    properties = (
        container.get("properties", {}) if isinstance(container, Mapping) else {}
    )
    if not isinstance(properties, Mapping) or properties.get("title") != title:
        errors.append("container title differs from the reviewed output name")
    return tuple(errors)


def _normalized_codec(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", str(value).casefold())
    aliases = {
        "dtshd": "dts",
        "dtshdma": "dts",
        "dtshdsecondary": "dts",
        "eac3secondary": "eac3",
        "h264avc": "h264",
        "h265hevc": "hevc",
    }
    return aliases.get(normalized, normalized)


def validate_ffprobe_stream_policy(
    document: Mapping[str, Any],
    *,
    video: FinalVideoPolicy,
    media_tracks: Sequence[FinalTrackPolicy],
) -> tuple[str, ...]:
    errors: list[str] = []
    raw_streams = document.get("streams")
    if not isinstance(raw_streams, list):
        return ("ffprobe stream report has no streams array",)
    # FFprobe represents the separately validated encode.log attachment as an
    # ``attachment`` stream.  MKVToolNix identification above enforces that
    # attachment exactly; this policy compares only playable media streams.
    streams = [
        item
        for item in raw_streams
        if isinstance(item, Mapping) and item.get("codec_type") != "attachment"
    ]
    expected_types = ["video", *(item.codec_type for item in media_tracks)]
    actual_types = [str(item.get("codec_type")) for item in streams]
    if actual_types != expected_types:
        errors.append(
            f"ffprobe stream topology differs: expected {expected_types}, got {actual_types}"
        )
        return tuple(errors)

    actual_video = streams[0]
    expected_video = {
        "codec_name": video.codec_name,
        "profile": video.profile,
        "width": video.width,
        "height": video.height,
        "pix_fmt": video.pixel_format,
        "color_range": video.color_range,
        "color_space": video.color_space,
        "color_transfer": video.color_transfer,
        "color_primaries": video.color_primaries,
        "chroma_location": video.chroma_location,
    }
    for key, expected in expected_video.items():
        actual = actual_video.get(key)
        if key == "codec_name":
            matches = _normalized_codec(actual) == _normalized_codec(expected)
        else:
            matches = actual == expected
        if not matches:
            errors.append(f"video {key} differs: expected {expected}, got {actual}")

    for index, (actual, expected) in enumerate(
        zip(streams[1:], media_tracks, strict=True), start=1
    ):
        if _normalized_codec(actual.get("codec_name")) != _normalized_codec(
            expected.codec_name
        ):
            errors.append(
                f"track {index} codec differs: expected {expected.codec_name}, "
                f"got {actual.get('codec_name')}"
            )
    return tuple(errors)


_MASTER_DISPLAY_RE = re.compile(
    r"^G\((\d+),(\d+)\)B\((\d+),(\d+)\)R\((\d+),(\d+)\)"
    r"WP\((\d+),(\d+)\)L\((\d+),(\d+)\)$"
)


def _side_data(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            if "side_data_type" in value:
                found.append(value)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(document)
    return found


def validate_hdr10_side_data(
    stream_document: Mapping[str, Any],
    frame_document: Mapping[str, Any],
    *,
    enabled: bool,
    mastering_display: str | None,
    max_cll: int | None,
    max_fall: int | None,
) -> tuple[str, ...]:
    errors: list[str] = []
    side_data = [*_side_data(stream_document), *_side_data(frame_document)]
    types = [str(item.get("side_data_type", "")) for item in side_data]
    lowered = "\n".join(types).casefold()
    for forbidden in ("dolby vision", "dovi", "hdr dynamic", "hdr10+"):
        if forbidden in lowered:
            errors.append(f"forbidden dynamic HDR side data is present: {forbidden}")

    mastering = [
        item
        for item in side_data
        if item.get("side_data_type") == "Mastering display metadata"
    ]
    content_light = [
        item
        for item in side_data
        if item.get("side_data_type") == "Content light level metadata"
    ]
    if not enabled:
        if mastering or content_light:
            errors.append("SDR output unexpectedly contains static HDR10 metadata")
        return tuple(errors)
    if not mastering or not content_light:
        errors.append(
            "HDR10 output is missing mastering display or content light metadata"
        )
        return tuple(errors)
    match = _MASTER_DISPLAY_RE.fullmatch(mastering_display or "")
    if match is None or max_cll is None or max_fall is None:
        return (*errors, "expected HDR10 metadata is incomplete")
    values = [int(item) for item in match.groups()]
    expected_mastering = {
        "green_x": Fraction(values[0], 50000),
        "green_y": Fraction(values[1], 50000),
        "blue_x": Fraction(values[2], 50000),
        "blue_y": Fraction(values[3], 50000),
        "red_x": Fraction(values[4], 50000),
        "red_y": Fraction(values[5], 50000),
        "white_point_x": Fraction(values[6], 50000),
        "white_point_y": Fraction(values[7], 50000),
        "max_luminance": Fraction(values[8], 10000),
        "min_luminance": Fraction(values[9], 10000),
    }
    for item in mastering:
        for key, expected in expected_mastering.items():
            try:
                actual = Fraction(str(item.get(key)))
            except (ValueError, ZeroDivisionError):
                errors.append(f"HDR10 mastering field {key} is missing or invalid")
                continue
            if actual != expected:
                errors.append(
                    f"HDR10 mastering field {key} differs: expected {expected}, got {actual}"
                )
    for item in content_light:
        if item.get("max_content") != max_cll or item.get("max_average") != max_fall:
            errors.append(
                "HDR10 MaxCLL/MaxFALL differs from the reviewed static metadata"
            )
    return tuple(dict.fromkeys(errors))
