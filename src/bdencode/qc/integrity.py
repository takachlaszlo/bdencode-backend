"""Source decode and encoded-video integrity quality-control primitives.

The packet-size probe deliberately measures elementary packet payload bytes
instead of container file sizes.  That keeps the source/encode comparison
stable across Matroska metadata, attachments, indexes, and muxer versions.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Mapping


DEFAULT_MINIMUM_SAVINGS_RATIO = Decimal("0.001")
DEFAULT_VIDEO_COMPLETENESS_TOLERANCE_FRAMES = 2
DEFAULT_VIDEO_CADENCE_TOLERANCE_MS = Decimal("1")
_STREAM_HASH_RE = re.compile(
    r"^\s*\d+\s*,\s*([vas])\s*,\s*SHA256=([0-9a-fA-F]{64})\s*$"
)


@dataclass(frozen=True, slots=True)
class VideoPacketSummary:
    """Exact aggregate of the selected video's compressed packet payloads."""

    packet_count: int
    total_bytes: int
    smallest_packet_bytes: int
    largest_packet_bytes: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PacketTimelineEntry:
    """One packet's normalized millisecond timing and payload size."""

    pts_ms: int | None
    dts_ms: int | None
    duration_ms: int | None
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PacketTimelineFingerprint:
    """Canonical packet-timeline evidence with uniform start offset removed."""

    entries: tuple[PacketTimelineEntry, ...]
    canonical_sha256: str
    presentation_span_ms: int | None

    @property
    def packet_count(self) -> int:
        return len(self.entries)

    @property
    def missing_pts_count(self) -> int:
        return sum(entry.pts_ms is None for entry in self.entries)

    @property
    def missing_dts_count(self) -> int:
        return sum(entry.dts_ms is None for entry in self.entries)

    @property
    def missing_duration_count(self) -> int:
        return sum(entry.duration_ms is None for entry in self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_count": self.packet_count,
            "canonical_sha256": self.canonical_sha256,
            "presentation_span_ms": self.presentation_span_ms,
            "missing_pts_count": self.missing_pts_count,
            "missing_dts_count": self.missing_dts_count,
            "missing_duration_count": self.missing_duration_count,
        }


@dataclass(frozen=True, slots=True)
class PacketTimelineVerdict:
    """Tolerance-aware sidecar/final packet timeline comparison."""

    packet_count_matches: bool
    mismatch_count: int
    first_mismatch_indexes: tuple[int, ...]
    tolerance_ms: int
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["first_mismatch_indexes"] = list(self.first_mismatch_indexes)
        return value


@dataclass(frozen=True, slots=True)
class VideoCadenceVerdict:
    """CFR grid validation for every final video packet PTS/duration."""

    reference_frame_count: int
    packet_count: int
    fps_numerator: int
    fps_denominator: int
    tolerance_ms: Decimal
    maximum_pts_error_ms: Decimal | None
    maximum_duration_error_ms: Decimal | None
    packet_count_matches: bool
    all_pts_present: bool
    all_durations_present: bool
    pts_are_unique: bool
    passed: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for name in (
            "tolerance_ms",
            "maximum_pts_error_ms",
            "maximum_duration_error_ms",
        ):
            item = value[name]
            value[name] = str(item) if item is not None else None
        value["reasons"] = list(self.reasons)
        return value


@dataclass(frozen=True, slots=True)
class VideoEfficiencyVerdict:
    """Result of the elementary-stream size policy for a lossy encode."""

    source_bytes: int
    encoded_bytes: int
    encoded_is_lossy: bool
    check_applied: bool
    passed: bool
    saved_bytes: int
    encoded_to_source_ratio: Decimal
    minimum_savings_ratio: Decimal
    required_savings_bytes: int
    maximum_encoded_bytes: int | None
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["encoded_to_source_ratio"] = str(self.encoded_to_source_ratio)
        value["minimum_savings_ratio"] = str(self.minimum_savings_ratio)
        return value


@dataclass(frozen=True, slots=True)
class VideoCompletenessVerdict:
    """Frame-count and duration verdict for a completed Matroska video track."""

    reference_frame_count: int
    encoded_packet_count: int
    fps_numerator: int
    fps_denominator: int
    tolerance_frames: int
    frame_duration_seconds: Decimal
    reference_duration_seconds: Decimal
    title_duration_seconds: Decimal
    final_video_duration_seconds: Decimal
    duration_delta_seconds: Decimal
    final_duration_delta_seconds: Decimal
    maximum_duration_delta_seconds: Decimal
    packet_count_matches: bool
    duration_matches: bool
    final_duration_matches: bool
    passed: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready, precision-preserving representation."""

        return {
            "reference_frame_count": self.reference_frame_count,
            "encoded_packet_count": self.encoded_packet_count,
            "fps_numerator": self.fps_numerator,
            "fps_denominator": self.fps_denominator,
            "tolerance_frames": self.tolerance_frames,
            "frame_duration_seconds": str(self.frame_duration_seconds),
            "reference_duration_seconds": str(self.reference_duration_seconds),
            "title_duration_seconds": str(self.title_duration_seconds),
            "final_video_duration_seconds": str(
                self.final_video_duration_seconds
            ),
            "duration_delta_seconds": str(self.duration_delta_seconds),
            "final_duration_delta_seconds": str(
                self.final_duration_delta_seconds
            ),
            "maximum_duration_delta_seconds": str(
                self.maximum_duration_delta_seconds
            ),
            "packet_count_matches": self.packet_count_matches,
            "duration_matches": self.duration_matches,
            "passed": self.passed,
            "reasons": list(self.reasons),
        }


class VideoEfficiencyError(ValueError):
    """Raised when a lossy encode does not satisfy the size-efficiency gate."""


class VideoCompletenessError(ValueError):
    """Raised when the final video is incomplete or its evidence is invalid."""


def _strict_positive_integer(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _strict_nonnegative_integer(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _positive_decimal(
    value: Decimal | int | float | str,
    *,
    name: str,
) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive finite number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return parsed


def source_video_integrity_command(
    path: Path,
    stream: int = 0,
    *,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Build a fail-fast, full pixel-decode scan of one video stream.

    FFmpeg's stderr remains the durable diagnostic log.  Machine-readable
    progress is emitted on stdout, which lets a caller supervise a long scan
    without weakening ``-xerror``/``explode`` error handling.  No copy codec is
    selected: every source frame must pass through the decoder before the null
    muxer accepts it.
    """

    if stream < 0:
        raise ValueError("video stream ordinal must not be negative")
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "repeat+level+info",
        "-nostats",
        "-stats_period",
        "1",
        "-progress",
        "pipe:1",
        "-xerror",
        "-err_detect",
        "explode",
        "-i",
        str(path),
        "-map",
        f"0:v:{stream}",
        "-map_metadata",
        "-1",
        "-f",
        "null",
        "-",
    ]


def video_packet_size_command(
    path: Path,
    stream: int = 0,
    *,
    ffprobe: str = "ffprobe",
) -> list[str]:
    """Build an exact, compact packet-payload size probe for one video stream."""

    if stream < 0:
        raise ValueError("video stream ordinal must not be negative")
    return [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        f"v:{stream}",
        "-show_packets",
        "-show_entries",
        "packet=size",
        "-of",
        "csv=p=0",
        str(path),
    ]


def packet_timeline_probe_command(
    path: Path,
    *,
    stream_type: Literal["v", "a", "s"],
    stream: int = 0,
    ffprobe: str = "ffprobe",
) -> list[str]:
    """Build a complete type-ordinal packet timestamp probe."""

    if stream_type not in {"v", "a", "s"}:
        raise ValueError("stream type must be v, a or s")
    if isinstance(stream, bool) or not isinstance(stream, int) or stream < 0:
        raise ValueError("stream ordinal must be a non-negative integer")
    return [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        f"{stream_type}:{stream}",
        "-show_packets",
        "-show_entries",
        "packet=pts_time,dts_time,duration_time,size",
        "-of",
        "json=compact=1",
        str(path),
    ]


def _optional_packet_time(value: Any, *, field: str) -> Decimal | None:
    if value in (None, "", "N/A"):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"packet {field} is not numeric") from exc
    if not parsed.is_finite():
        raise ValueError(f"packet {field} must be finite")
    return parsed


def _milliseconds(value: Decimal) -> int:
    return int((value * Decimal(1000)).to_integral_value(rounding=ROUND_HALF_UP))


def parse_packet_timeline(
    document: Mapping[str, Any] | str | bytes,
) -> PacketTimelineFingerprint:
    """Parse and normalize a complete ffprobe packet timeline."""

    if isinstance(document, bytes):
        document = document.decode("utf-8")
    if isinstance(document, str):
        try:
            raw = json.loads(document)
        except json.JSONDecodeError as exc:
            raise ValueError("packet timeline probe is not valid JSON") from exc
    else:
        raw = document
    if not isinstance(raw, Mapping) or not isinstance(raw.get("packets"), list):
        raise ValueError("packet timeline probe has no packets array")
    packets = raw["packets"]
    if not packets:
        raise ValueError("packet timeline probe contains no packets")

    parsed: list[tuple[Decimal | None, Decimal | None, Decimal | None, int]] = []
    for index, packet in enumerate(packets, start=1):
        if not isinstance(packet, Mapping):
            raise ValueError(f"packet timeline entry {index} is invalid")
        pts = _optional_packet_time(packet.get("pts_time"), field="PTS")
        dts = _optional_packet_time(packet.get("dts_time"), field="DTS")
        duration = _optional_packet_time(
            packet.get("duration_time"), field="duration"
        )
        if pts is None and dts is None:
            raise ValueError(f"packet timeline entry {index} has no PTS or DTS")
        if duration is not None and duration < 0:
            raise ValueError(f"packet timeline entry {index} has negative duration")
        size = packet.get("size")
        if isinstance(size, bool):
            raise ValueError(f"packet timeline entry {index} size is invalid")
        try:
            parsed_size = int(str(size))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"packet timeline entry {index} size is invalid"
            ) from exc
        if parsed_size <= 0:
            raise ValueError(
                f"packet timeline entry {index} size must be positive"
            )
        parsed.append((pts, dts, duration, parsed_size))

    pts_values = tuple(item[0] for item in parsed if item[0] is not None)
    dts_values = tuple(item[1] for item in parsed if item[1] is not None)
    # One shared origin removes only the legitimate mux-wide --sync offset.
    # Normalizing PTS and DTS independently would hide a changed composition /
    # decoder delay relationship even though packet order still looked intact.
    timeline_origin = (
        min(pts_values)
        if pts_values
        else min(dts_values)
        if dts_values
        else None
    )
    entries = tuple(
        PacketTimelineEntry(
            pts_ms=(
                _milliseconds(pts - timeline_origin)
                if pts is not None and timeline_origin is not None
                else None
            ),
            dts_ms=(
                _milliseconds(dts - timeline_origin)
                if dts is not None and timeline_origin is not None
                else None
            ),
            duration_ms=(
                _milliseconds(duration) if duration is not None else None
            ),
            size_bytes=size,
        )
        for pts, dts, duration, size in parsed
    )
    canonical = json.dumps(
        [asdict(entry) for entry in entries],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    presentation_ends = (
        tuple(
            entry.pts_ms + entry.duration_ms
            for entry in entries
            if entry.pts_ms is not None and entry.duration_ms is not None
        )
        if all(
            entry.pts_ms is not None and entry.duration_ms is not None
            for entry in entries
        )
        else ()
    )
    return PacketTimelineFingerprint(
        entries=entries,
        canonical_sha256=hashlib.sha256(canonical).hexdigest(),
        presentation_span_ms=(max(presentation_ends) if presentation_ends else None),
    )


def compare_packet_timelines(
    source: PacketTimelineFingerprint,
    final: PacketTimelineFingerprint,
    *,
    tolerance_ms: int = 1,
) -> PacketTimelineVerdict:
    """Compare normalized packet timing while allowing Matroska ms rounding."""

    if (
        isinstance(tolerance_ms, bool)
        or not isinstance(tolerance_ms, int)
        or tolerance_ms < 0
    ):
        raise ValueError("packet timeline tolerance must be non-negative")
    mismatches: list[int] = []
    for index, (left, right) in enumerate(
        zip(source.entries, final.entries, strict=False)
    ):
        timing_matches = True
        for field in ("pts_ms", "dts_ms", "duration_ms"):
            left_value = getattr(left, field)
            right_value = getattr(right, field)
            if (left_value is None) != (right_value is None) or (
                left_value is not None
                and right_value is not None
                and abs(left_value - right_value) > tolerance_ms
            ):
                timing_matches = False
        if left.size_bytes != right.size_bytes or not timing_matches:
            mismatches.append(index)
    packet_count_matches = source.packet_count == final.packet_count
    if not packet_count_matches:
        mismatches.extend(
            range(
                min(source.packet_count, final.packet_count),
                max(source.packet_count, final.packet_count),
            )
        )
    return PacketTimelineVerdict(
        packet_count_matches=packet_count_matches,
        mismatch_count=len(mismatches),
        first_mismatch_indexes=tuple(mismatches[:10]),
        tolerance_ms=tolerance_ms,
        passed=packet_count_matches and not mismatches,
    )


def evaluate_video_cadence(
    timeline: PacketTimelineFingerprint,
    reference_frame_count: int,
    *,
    fps_numerator: int,
    fps_denominator: int,
    tolerance_ms: Decimal | int | float | str = DEFAULT_VIDEO_CADENCE_TOLERANCE_MS,
) -> VideoCadenceVerdict:
    """Validate every final video packet against its exact CFR time grid."""

    if not isinstance(timeline, PacketTimelineFingerprint):
        raise ValueError("video packet timeline evidence is invalid")
    reference_frames = _strict_positive_integer(
        reference_frame_count, name="reference frame count"
    )
    numerator = _strict_positive_integer(fps_numerator, name="FPS numerator")
    denominator = _strict_positive_integer(fps_denominator, name="FPS denominator")
    if isinstance(tolerance_ms, bool):
        raise ValueError("video cadence tolerance must be a finite non-negative number")
    try:
        tolerance = Decimal(str(tolerance_ms))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            "video cadence tolerance must be a finite non-negative number"
        ) from exc
    if not tolerance.is_finite() or tolerance < 0:
        raise ValueError(
            "video cadence tolerance must be a finite non-negative number"
        )

    packet_count_matches = timeline.packet_count == reference_frames
    all_pts_present = timeline.missing_pts_count == 0
    all_durations_present = timeline.missing_duration_count == 0
    pts_values = sorted(
        entry.pts_ms for entry in timeline.entries if entry.pts_ms is not None
    )
    pts_are_unique = len(pts_values) == len(set(pts_values))
    frame_duration_ms = Decimal(1000) * Decimal(denominator) / Decimal(numerator)
    pts_errors = tuple(
        abs(Decimal(value) - Decimal(index) * frame_duration_ms)
        for index, value in enumerate(pts_values)
    )
    duration_errors = tuple(
        abs(Decimal(entry.duration_ms) - frame_duration_ms)
        for entry in timeline.entries
        if entry.duration_ms is not None
    )
    maximum_pts_error = max(pts_errors) if pts_errors else None
    maximum_duration_error = max(duration_errors) if duration_errors else None

    reasons: list[str] = []
    if not packet_count_matches:
        reasons.append(
            "final video packet-timeline count does not match the reference frame "
            f"count: packets={timeline.packet_count}, frames={reference_frames}"
        )
    if not all_pts_present:
        reasons.append("final video packet timeline has missing presentation timestamps")
    if not all_durations_present:
        reasons.append("final video packet timeline has missing packet durations")
    if not pts_are_unique:
        reasons.append("final video presentation timestamps are not unique")
    if maximum_pts_error is not None and maximum_pts_error > tolerance:
        reasons.append(
            "final video presentation timestamps deviate from the CFR grid: "
            f"maximum_error_ms={maximum_pts_error}, tolerance_ms={tolerance}"
        )
    if maximum_duration_error is not None and maximum_duration_error > tolerance:
        reasons.append(
            "final video packet durations deviate from the CFR grid: "
            f"maximum_error_ms={maximum_duration_error}, tolerance_ms={tolerance}"
        )

    return VideoCadenceVerdict(
        reference_frame_count=reference_frames,
        packet_count=timeline.packet_count,
        fps_numerator=numerator,
        fps_denominator=denominator,
        tolerance_ms=tolerance,
        maximum_pts_error_ms=maximum_pts_error,
        maximum_duration_error_ms=maximum_duration_error,
        packet_count_matches=packet_count_matches,
        all_pts_present=all_pts_present,
        all_durations_present=all_durations_present,
        pts_are_unique=pts_are_unique,
        passed=not reasons,
        reasons=tuple(reasons),
    )


def require_video_cadence(
    timeline: PacketTimelineFingerprint,
    reference_frame_count: int,
    *,
    fps_numerator: int,
    fps_denominator: int,
    tolerance_ms: Decimal | int | float | str = DEFAULT_VIDEO_CADENCE_TOLERANCE_MS,
) -> VideoCadenceVerdict:
    """Return a passing CFR cadence verdict or fail closed for review."""

    try:
        verdict = evaluate_video_cadence(
            timeline,
            reference_frame_count,
            fps_numerator=fps_numerator,
            fps_denominator=fps_denominator,
            tolerance_ms=tolerance_ms,
        )
    except ValueError as exc:
        raise VideoCompletenessError(
            f"invalid video-cadence evidence: {exc}"
        ) from exc
    if not verdict.passed:
        raise VideoCompletenessError("; ".join(verdict.reasons))
    return verdict


def video_stream_hash_command(
    path: Path,
    stream: int = 0,
    *,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Hash one compressed video stream's packet payload through stream copy."""

    return stream_payload_hash_command(
        path,
        stream_type="v",
        stream=stream,
        ffmpeg=ffmpeg,
    )


def stream_payload_hash_command(
    path: Path,
    *,
    stream_type: Literal["v", "a", "s"],
    stream: int = 0,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Hash one selected compressed stream exactly as it crosses the mux."""

    if stream_type not in {"v", "a", "s"}:
        raise ValueError("stream type must be v, a or s")
    if isinstance(stream, bool) or not isinstance(stream, int) or stream < 0:
        raise ValueError("stream ordinal must be a non-negative integer")
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-err_detect",
        "explode",
        "-i",
        str(path),
        "-map",
        f"0:{stream_type}:{stream}",
        "-c",
        "copy",
        "-f",
        "streamhash",
        "-hash",
        "sha256",
        "-",
    ]


def parse_video_stream_hash(report: str | bytes) -> str:
    """Parse exactly one FFmpeg streamhash SHA-256 for a video stream."""

    return parse_stream_payload_hash(report, expected_stream_type="v")


def parse_stream_payload_hash(
    report: str | bytes,
    *,
    expected_stream_type: Literal["v", "a", "s"],
) -> str:
    """Parse exactly one type-checked FFmpeg streamhash SHA-256."""

    if expected_stream_type not in {"v", "a", "s"}:
        raise ValueError("expected stream type must be v, a or s")
    if isinstance(report, bytes):
        report = report.decode("utf-8")
    matches = [
        (match.group(1), match.group(2).lower())
        for line in report.splitlines()
        if (match := _STREAM_HASH_RE.fullmatch(line)) is not None
    ]
    if len(matches) != 1:
        raise ValueError("streamhash report must contain exactly one SHA-256")
    stream_type, digest = matches[0]
    if stream_type != expected_stream_type:
        raise ValueError(
            "streamhash report type differs from the selected stream type"
        )
    return digest


def parse_video_packet_sizes(report: str | bytes) -> VideoPacketSummary:
    """Parse ``video_packet_size_command`` output and sum payload bytes exactly."""

    if isinstance(report, bytes):
        report = report.decode("utf-8")
    packet_count = 0
    total_bytes = 0
    smallest_packet_bytes: int | None = None
    largest_packet_bytes: int | None = None
    for line_number, row in enumerate(csv.reader(io.StringIO(report)), start=1):
        values = [value.strip() for value in row if value.strip()]
        if not values:
            continue
        if len(values) != 1:
            raise ValueError(
                f"video packet-size report line {line_number} has "
                f"{len(values)} fields instead of one"
            )
        try:
            size = int(values[0], 10)
        except ValueError as exc:
            raise ValueError(
                f"video packet-size report line {line_number} is not an integer"
            ) from exc
        if size < 0:
            raise ValueError(
                f"video packet-size report line {line_number} is negative"
            )
        packet_count += 1
        total_bytes += size
        smallest_packet_bytes = (
            size
            if smallest_packet_bytes is None
            else min(smallest_packet_bytes, size)
        )
        largest_packet_bytes = (
            size if largest_packet_bytes is None else max(largest_packet_bytes, size)
        )
    if packet_count == 0:
        raise ValueError("video packet-size report contains no packets")
    if total_bytes <= 0:
        raise ValueError("video packet-size report has no payload bytes")
    assert smallest_packet_bytes is not None
    assert largest_packet_bytes is not None
    return VideoPacketSummary(
        packet_count=packet_count,
        total_bytes=total_bytes,
        smallest_packet_bytes=smallest_packet_bytes,
        largest_packet_bytes=largest_packet_bytes,
    )


def evaluate_video_completeness(
    reference_frame_count: int,
    encoded_packet_count: int,
    *,
    fps_numerator: int,
    fps_denominator: int,
    title_duration_seconds: Decimal | int | float | str,
    final_video_duration_seconds: Decimal | int | float | str,
    tolerance_frames: int = DEFAULT_VIDEO_COMPLETENESS_TOLERANCE_FRAMES,
) -> VideoCompletenessVerdict:
    """Evaluate whether the encoded Matroska video covers the complete title.

    Matroska carries one H.264/HEVC access unit per video packet, so an encoded
    packet count different from the reference frame count fails unconditionally.
    Independent duration checks derive the reference duration from its exact
    frame count and rational FPS, then require both the reviewed playlist and
    the final Matroska packet timeline to remain within ``tolerance_frames``.

    Invalid or non-finite evidence raises instead of producing a passing verdict.
    Use :func:`require_video_completeness` for a single fail-closed gate.
    """

    reference_frames = _strict_positive_integer(
        reference_frame_count, name="reference frame count"
    )
    encoded_packets = _strict_positive_integer(
        encoded_packet_count, name="encoded packet count"
    )
    numerator = _strict_positive_integer(fps_numerator, name="FPS numerator")
    denominator = _strict_positive_integer(fps_denominator, name="FPS denominator")
    tolerance = _strict_nonnegative_integer(
        tolerance_frames, name="video completeness tolerance frames"
    )
    title_duration = _positive_decimal(
        title_duration_seconds, name="title duration"
    )
    final_video_duration = _positive_decimal(
        final_video_duration_seconds, name="final video duration"
    )

    frame_duration = Decimal(denominator) / Decimal(numerator)
    reference_duration = Decimal(reference_frames) * frame_duration
    duration_delta = abs(reference_duration - title_duration)
    final_duration_delta = abs(reference_duration - final_video_duration)
    maximum_duration_delta = Decimal(tolerance) * frame_duration
    packet_count_matches = encoded_packets == reference_frames
    duration_matches = duration_delta <= maximum_duration_delta
    final_duration_matches = final_duration_delta <= maximum_duration_delta

    reasons: list[str] = []
    if not packet_count_matches:
        reasons.append(
            "encoded Matroska video packet count does not match reference frame "
            f"count: encoded_packets={encoded_packets}, "
            f"reference_frames={reference_frames}"
        )
    if not duration_matches:
        reasons.append(
            "reference frame-derived duration does not match playlist/title "
            f"duration within {tolerance} frame(s): "
            f"reference_duration_seconds={reference_duration}, "
            f"title_duration_seconds={title_duration}, "
            f"delta_seconds={duration_delta}, "
            f"maximum_delta_seconds={maximum_duration_delta}"
        )
    if not final_duration_matches:
        reasons.append(
            "final Matroska packet-timeline duration does not match the "
            f"reference within {tolerance} frame(s): "
            f"reference_duration_seconds={reference_duration}, "
            f"final_video_duration_seconds={final_video_duration}, "
            f"delta_seconds={final_duration_delta}, "
            f"maximum_delta_seconds={maximum_duration_delta}"
        )

    return VideoCompletenessVerdict(
        reference_frame_count=reference_frames,
        encoded_packet_count=encoded_packets,
        fps_numerator=numerator,
        fps_denominator=denominator,
        tolerance_frames=tolerance,
        frame_duration_seconds=frame_duration,
        reference_duration_seconds=reference_duration,
        title_duration_seconds=title_duration,
        final_video_duration_seconds=final_video_duration,
        duration_delta_seconds=duration_delta,
        final_duration_delta_seconds=final_duration_delta,
        maximum_duration_delta_seconds=maximum_duration_delta,
        packet_count_matches=packet_count_matches,
        duration_matches=duration_matches,
        final_duration_matches=final_duration_matches,
        passed=(
            packet_count_matches and duration_matches and final_duration_matches
        ),
        reasons=tuple(reasons),
    )


def require_video_completeness(
    reference_frame_count: int,
    encoded_packet_count: int,
    *,
    fps_numerator: int,
    fps_denominator: int,
    title_duration_seconds: Decimal | int | float | str,
    final_video_duration_seconds: Decimal | int | float | str,
    tolerance_frames: int = DEFAULT_VIDEO_COMPLETENESS_TOLERANCE_FRAMES,
) -> VideoCompletenessVerdict:
    """Return a passing completeness verdict or raise for manual review."""

    try:
        verdict = evaluate_video_completeness(
            reference_frame_count,
            encoded_packet_count,
            fps_numerator=fps_numerator,
            fps_denominator=fps_denominator,
            title_duration_seconds=title_duration_seconds,
            final_video_duration_seconds=final_video_duration_seconds,
            tolerance_frames=tolerance_frames,
        )
    except ValueError as exc:
        raise VideoCompletenessError(
            f"invalid video-completeness evidence: {exc}"
        ) from exc
    if not verdict.passed:
        raise VideoCompletenessError("; ".join(verdict.reasons))
    return verdict


def evaluate_video_efficiency(
    source_bytes: int,
    encoded_bytes: int,
    *,
    encoded_is_lossy: bool = True,
    minimum_savings_ratio: Decimal = DEFAULT_MINIMUM_SAVINGS_RATIO,
) -> VideoEfficiencyVerdict:
    """Evaluate whether a lossy encode is materially smaller than its source.

    The default 0.1% savings margin is deliberately small, but it ensures that
    an equal-size or larger lossy output can never pass due to integer rounding.
    Set the margin to zero to require only one byte of savings.
    """

    if source_bytes <= 0:
        raise ValueError("source video payload size must be positive")
    if encoded_bytes <= 0:
        raise ValueError("encoded video payload size must be positive")
    minimum_savings_ratio = Decimal(minimum_savings_ratio)
    if not Decimal(0) <= minimum_savings_ratio < Decimal(1):
        raise ValueError("minimum savings ratio must be at least zero and below one")

    ratio = Decimal(encoded_bytes) / Decimal(source_bytes)
    saved_bytes = source_bytes - encoded_bytes
    check_applied = bool(encoded_is_lossy)
    required_savings_bytes = 0
    maximum_encoded_bytes = None
    if check_applied:
        required_savings_bytes = max(
            1,
            int(
                (Decimal(source_bytes) * minimum_savings_ratio).to_integral_value(
                    rounding=ROUND_CEILING
                )
            ),
        )
        maximum_encoded_bytes = source_bytes - required_savings_bytes
    passed = not check_applied or (
        maximum_encoded_bytes is not None
        and encoded_bytes <= maximum_encoded_bytes
    )
    reason = None
    if not passed:
        reason = (
            "lossy encoded video is not smaller than the source by the required "
            f"margin: encoded={encoded_bytes} bytes, source={source_bytes} bytes, "
            f"ratio={ratio:.6f}, required_savings={required_savings_bytes} bytes"
        )
    return VideoEfficiencyVerdict(
        source_bytes=source_bytes,
        encoded_bytes=encoded_bytes,
        encoded_is_lossy=encoded_is_lossy,
        check_applied=check_applied,
        passed=passed,
        saved_bytes=saved_bytes,
        encoded_to_source_ratio=ratio,
        minimum_savings_ratio=minimum_savings_ratio,
        required_savings_bytes=required_savings_bytes,
        maximum_encoded_bytes=maximum_encoded_bytes,
        reason=reason,
    )


def require_video_efficiency(
    source_bytes: int,
    encoded_bytes: int,
    *,
    encoded_is_lossy: bool = True,
    minimum_savings_ratio: Decimal = DEFAULT_MINIMUM_SAVINGS_RATIO,
) -> VideoEfficiencyVerdict:
    """Return a passing verdict or raise with a review-ready explanation."""

    verdict = evaluate_video_efficiency(
        source_bytes,
        encoded_bytes,
        encoded_is_lossy=encoded_is_lossy,
        minimum_savings_ratio=minimum_savings_ratio,
    )
    if not verdict.passed:
        assert verdict.reason is not None
        raise VideoEfficiencyError(verdict.reason)
    return verdict


__all__ = [
    "DEFAULT_MINIMUM_SAVINGS_RATIO",
    "DEFAULT_VIDEO_CADENCE_TOLERANCE_MS",
    "DEFAULT_VIDEO_COMPLETENESS_TOLERANCE_FRAMES",
    "VideoCompletenessError",
    "VideoCompletenessVerdict",
    "VideoCadenceVerdict",
    "VideoEfficiencyError",
    "VideoEfficiencyVerdict",
    "VideoPacketSummary",
    "PacketTimelineEntry",
    "PacketTimelineFingerprint",
    "PacketTimelineVerdict",
    "compare_packet_timelines",
    "evaluate_video_completeness",
    "evaluate_video_cadence",
    "evaluate_video_efficiency",
    "parse_video_packet_sizes",
    "parse_stream_payload_hash",
    "parse_packet_timeline",
    "parse_video_stream_hash",
    "require_video_completeness",
    "require_video_cadence",
    "require_video_efficiency",
    "source_video_integrity_command",
    "packet_timeline_probe_command",
    "stream_payload_hash_command",
    "video_packet_size_command",
    "video_stream_hash_command",
]
