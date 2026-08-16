"""MKV assembly, tags, and post-mux inspection commands."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
    sync_offset_ms: Decimal = Decimal(0)
    subtitle_kind: str | None = None

    def __post_init__(self) -> None:
        if not self.language or len(self.language) > 35:
            raise ValueError("invalid BCP 47 language tag")
        if not isinstance(self.default, bool) or not isinstance(self.forced, bool):
            raise ValueError("track default and forced flags must be boolean")
        sync_offset = _decimal(self.sync_offset_ms, name="track sync offset")
        object.__setattr__(self, "sync_offset_ms", sync_offset)
        if self.subtitle_kind not in {None, "unknown", "full", "forced"}:
            raise ValueError(
                "subtitle_kind must be unknown, full, forced or omitted"
            )


@dataclass(frozen=True, slots=True)
class CommonTimelinePlan:
    """Mux offsets and expected starts on one normalized presentation timeline."""

    origin_seconds: Decimal
    expected_start_seconds: tuple[Decimal, ...]
    video_sync_offset_ms: Decimal
    track_sync_offsets_ms: tuple[Decimal, ...]


def plan_common_zero_timeline(
    reference_video_start: Decimal | str | int | float,
    retained_track_starts: Sequence[Decimal | str | int | float],
    *,
    encoded_video_start: Decimal | str | int | float = Decimal(0),
    sidecar_start_times: Sequence[Decimal | str | int | float] | None = None,
) -> CommonTimelinePlan:
    """Preserve relative stream timing while rebasing the earliest stream to zero.

    The encoded video produced from Y4M starts at zero, while audio and subtitle
    sidecars normally retain the reference-remux timestamps.  The optional
    encoded/sidecar starts account for codec priming or container rounding, so
    the calculated mux offsets restore the reviewed source relationship rather
    than assuming every intermediate starts exactly where its source did.
    """

    starts = (
        _decimal(reference_video_start, name="reference video start"),
        *(
            _decimal(value, name=f"retained track {index} start")
            for index, value in enumerate(retained_track_starts, start=1)
        ),
    )
    origin = min(starts)
    expected = tuple(value - origin for value in starts)
    encoded_start = _decimal(encoded_video_start, name="encoded video start")
    if sidecar_start_times is None:
        sidecar_starts = starts[1:]
    else:
        sidecar_starts = tuple(
            _decimal(value, name=f"sidecar {index} start")
            for index, value in enumerate(sidecar_start_times, start=1)
        )
        if len(sidecar_starts) != len(retained_track_starts):
            raise ValueError("sidecar starts must match retained track count")
    return CommonTimelinePlan(
        origin_seconds=origin,
        expected_start_seconds=expected,
        video_sync_offset_ms=(expected[0] - encoded_start) * Decimal(1000),
        track_sync_offsets_ms=tuple(
            (desired - sidecar_start) * Decimal(1000)
            for desired, sidecar_start in zip(
                expected[1:], sidecar_starts, strict=True
            )
        ),
    )


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
    level: int | None = None


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
    title: str | None = None,
    video_sync_offset_ms: Decimal | str | int | float = Decimal(0),
    mkvmerge: str = "mkvmerge",
) -> list[str]:
    policy_errors = validate_mux_track_policy(
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
    )
    if policy_errors:
        raise ValueError("; ".join(policy_errors))
    video_sync = _decimal(video_sync_offset_ms, name="video sync offset")
    command = [mkvmerge, "--output", str(output_path)]
    if title:
        command.extend(("--title", title))
    if chapters_path:
        command.extend(("--chapters", str(chapters_path)))
    command.extend(
        (
            "--default-track-flag",
            "0:yes",
            "--forced-display-flag",
            "0:no",
            *(_sync_option(video_sync)),
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
    return command


def _track_options(item: MuxTrack) -> tuple[str, ...]:
    values = [
        "--language",
        f"0:{item.language}",
        "--default-track-flag",
        f"0:{'yes' if item.default else 'no'}",
        "--forced-display-flag",
        f"0:{'yes' if item.forced else 'no'}",
        *_sync_option(item.sync_offset_ms),
    ]
    if item.name:
        values.extend(("--track-name", f"0:{item.name}"))
    return tuple(values)


def _decimal(value: object, *, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite number")
    return result


def _decimal_token(value: Decimal) -> str:
    if value == 0:
        return "0"
    token = format(value, "f")
    if "." in token:
        token = token.rstrip("0").rstrip(".")
    return token


def _sync_option(offset_ms: Decimal) -> tuple[str, ...]:
    if offset_ms == 0:
        return ()
    return ("--sync", f"0:{_decimal_token(offset_ms)}")


def validate_mux_track_policy(
    *,
    audio_tracks: Sequence[MuxTrack],
    subtitle_tracks: Sequence[MuxTrack],
) -> tuple[str, ...]:
    """Reject ambiguous defaults and unreviewed forced-subtitle declarations."""

    errors: list[str] = []
    if audio_tracks and sum(item.default for item in audio_tracks) != 1:
        errors.append("exactly one retained audio track must be default")
    for index, item in enumerate(audio_tracks, start=1):
        if item.forced:
            errors.append(f"audio track {index} cannot be forced")
        if item.subtitle_kind is not None:
            errors.append(f"audio track {index} cannot declare subtitle_kind")
    for index, item in enumerate(subtitle_tracks, start=1):
        if item.forced:
            if item.subtitle_kind == "full":
                errors.append(f"full subtitle track {index} cannot be forced")
            elif item.subtitle_kind != "forced":
                errors.append(
                    f"subtitle track {index} cannot be forced without an explicit "
                    "reviewed-forced classification"
                )
        elif item.subtitle_kind == "forced":
            errors.append(
                f"reviewed forced subtitle track {index} must set the forced flag"
            )
    return tuple(errors)


def stream_start_probe_command(
    input_path: Path, *, ffprobe: str = "ffprobe"
) -> list[str]:
    """Probe the absolute presentation start of every reference-remux stream."""

    return [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type,start_time",
        "-of",
        "json",
        str(input_path),
    ]


def parse_stream_start_times(document: Mapping[str, Any]) -> dict[int, Decimal]:
    """Read a stream-index to absolute-start mapping without guessing gaps."""

    streams = document.get("streams")
    if not isinstance(streams, list):
        raise ValueError("ffprobe stream-start report has no streams array")
    result: dict[int, Decimal] = {}
    for position, stream in enumerate(streams):
        if not isinstance(stream, Mapping):
            raise ValueError(f"stream-start entry {position} is not an object")
        raw_index = stream.get("index")
        if isinstance(raw_index, bool):
            raise ValueError(f"stream-start entry {position} has no numeric index")
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"stream-start entry {position} has no numeric index"
            ) from exc
        if index in result:
            raise ValueError(f"duplicate stream index in start report: {index}")
        raw_start = stream.get("start_time")
        if raw_start is None or raw_start == "N/A":
            raise ValueError(f"stream {index} has no presentation start_time")
        result[index] = _decimal(raw_start, name=f"stream {index} start")
    if not result:
        raise ValueError("ffprobe stream-start report has no streams")
    return result


def parse_stream_start_times_by_type(
    document: Mapping[str, Any],
) -> dict[tuple[str, int], Decimal]:
    """Resolve starts by FFmpeg type ordinal, independent of absolute indexes."""

    streams = document.get("streams")
    if not isinstance(streams, list):
        raise ValueError("ffprobe stream-start report has no streams array")
    ordinals: dict[str, int] = {}
    result: dict[tuple[str, int], Decimal] = {}
    for position, stream in enumerate(streams):
        if not isinstance(stream, Mapping):
            raise ValueError(f"stream-start entry {position} is not an object")
        codec_type = stream.get("codec_type")
        if not isinstance(codec_type, str) or not codec_type:
            raise ValueError(f"stream-start entry {position} has no codec_type")
        ordinal = ordinals.get(codec_type, 0)
        ordinals[codec_type] = ordinal + 1
        raw_start = stream.get("start_time")
        if raw_start is None or raw_start == "N/A":
            raise ValueError(
                f"{codec_type} stream ordinal {ordinal} has no presentation start_time"
            )
        result[(codec_type, ordinal)] = _decimal(
            raw_start, name=f"{codec_type} stream ordinal {ordinal} start"
        )
    if not result:
        raise ValueError("ffprobe stream-start report has no streams")
    return result


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
                "stream=index,codec_name,profile,level,codec_type,start_time,duration,width,height,pix_fmt,color_range,color_space,color_transfer,color_primaries,chroma_location,bits_per_raw_sample:stream_disposition:stream_side_data",
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
    errors.extend(
        validate_mux_track_policy(
            audio_tracks=audio_tracks,
            subtitle_tracks=subtitle_tracks,
        )
    )
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

    actual_video = next(
        (
            item
            for item in tracks
            if isinstance(item, Mapping) and item.get("type") == "video"
        ),
        None,
    )
    video_properties = (
        actual_video.get("properties", {})
        if isinstance(actual_video, Mapping)
        else {}
    )
    if not isinstance(video_properties, Mapping) or not bool(
        video_properties.get("default_track", False)
    ):
        errors.append("the sole video track is not default")
    if isinstance(video_properties, Mapping) and bool(
        video_properties.get("forced_track", False)
    ):
        errors.append("the video track is unexpectedly forced")

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
    if names:
        errors.append(
            f"attachment policy differs: public releases must have none, got {names}"
        )

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
    # Attachments are never emitted by v2.  Ignore one here only so the media
    # topology diagnostic can report playable-stream differences independently;
    # the MKVToolNix topology validator rejects attachments separately.
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
    if video.level is not None:
        expected_video["level"] = video.level
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


def validate_stream_start_times(
    document: Mapping[str, Any],
    *,
    expected_start_times: Sequence[Decimal | str | int | float],
    tolerances: Sequence[Decimal | str | int | float] | None = None,
    zero_tolerance: Decimal | str | int | float = Decimal("0.001"),
) -> tuple[str, ...]:
    """Validate final media starts and their offsets on a common zero timeline.

    ``expected_start_times`` uses final playable-stream order (video, audios,
    subtitles).  Both expected and actual values are compared relative to
    their earliest stream, while the final container is separately required
    to place that earliest stream at zero.  Attachments are ignored.
    """

    raw_streams = document.get("streams")
    if not isinstance(raw_streams, list):
        return ("ffprobe stream report has no streams array",)
    streams = [
        item
        for item in raw_streams
        if isinstance(item, Mapping) and item.get("codec_type") != "attachment"
    ]
    expected = tuple(
        _decimal(value, name=f"expected stream {index} start")
        for index, value in enumerate(expected_start_times)
    )
    if len(streams) != len(expected):
        return (
            "stream-start topology differs: "
            f"expected {len(expected)} media streams, got {len(streams)}",
        )
    if not expected:
        return ("stream-start policy has no media streams",)
    if tolerances is None:
        tolerance_values = (Decimal("0.001"),) * len(expected)
    else:
        tolerance_values = tuple(
            _decimal(value, name=f"stream {index} start tolerance")
            for index, value in enumerate(tolerances)
        )
        if len(tolerance_values) != len(expected):
            raise ValueError("start tolerances must match expected stream count")
    if any(value < 0 for value in tolerance_values):
        raise ValueError("stream start tolerances cannot be negative")
    allowed_zero_delta = _decimal(zero_tolerance, name="zero tolerance")
    if allowed_zero_delta < 0:
        raise ValueError("zero tolerance cannot be negative")

    actual: list[Decimal] = []
    errors: list[str] = []
    for index, stream in enumerate(streams):
        raw_start = stream.get("start_time")
        if raw_start is None or raw_start == "N/A":
            errors.append(f"stream {index} has no start_time")
            continue
        try:
            actual.append(_decimal(raw_start, name=f"stream {index} start"))
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        return tuple(errors)

    actual_origin = min(actual)
    expected_origin = min(expected)
    if abs(actual_origin) > allowed_zero_delta:
        errors.append(
            "final media timeline does not start at zero: "
            f"earliest stream starts at {_decimal_token(actual_origin)}s"
        )
    normalized_actual = tuple(value - actual_origin for value in actual)
    normalized_expected = tuple(value - expected_origin for value in expected)
    for index, (actual_start, expected_start, tolerance) in enumerate(
        zip(
            normalized_actual,
            normalized_expected,
            tolerance_values,
            strict=True,
        )
    ):
        delta = actual_start - expected_start
        if abs(delta) > tolerance:
            errors.append(
                f"stream {index} relative start differs by "
                f"{_decimal_token(delta)}s (allowed {_decimal_token(tolerance)}s)"
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
