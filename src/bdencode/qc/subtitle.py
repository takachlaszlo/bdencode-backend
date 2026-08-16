"""Fail-closed subtitle sidecar inspection and classification checks.

The encoder extracts each retained subtitle stream into a one-stream Matroska
sidecar before the final mux.  This module deliberately operates on that
sidecar, so stream selection is unambiguous and the result can be archived as
ordinary ffprobe JSON.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Mapping


DEFAULT_FORCED_PACKET_LIMIT = 250
DEFAULT_FORCED_COVERAGE_LIMIT = Decimal("0.50")
_UNKNOWN_DISPLAY_TIME = 2**32 - 1


class SubtitleProbeError(ValueError):
    """Raised when a subtitle probe cannot be interpreted safely."""


class SubtitleDecodeError(ValueError):
    """Raised when final-subtitle decode evidence cannot pass safely."""


@dataclass(frozen=True, slots=True)
class SubtitleDecodeEvent:
    """Timing evidence emitted for one actually decoded subtitle event."""

    timestamp: Decimal | None
    duration: Decimal | None
    num_rects: int

    def __post_init__(self) -> None:
        if self.timestamp is not None and not self.timestamp.is_finite():
            raise ValueError("decoded subtitle timestamp must be finite")
        if self.duration is not None:
            if not self.duration.is_finite():
                raise ValueError("decoded subtitle duration must be finite")
            if self.duration < 0:
                raise ValueError("decoded subtitle duration must not be negative")
        if (
            isinstance(self.num_rects, bool)
            or not isinstance(self.num_rects, int)
            or self.num_rects < 0
        ):
            raise ValueError("decoded subtitle rectangle count must be non-negative")


@dataclass(frozen=True, slots=True)
class SubtitleDecodeProbe:
    """Payload-decode evidence for one type-ordinal subtitle stream."""

    codec_name: str
    stream_index: int
    events: tuple[SubtitleDecodeEvent, ...]

    def __post_init__(self) -> None:
        if not self.codec_name:
            raise ValueError("decoded subtitle codec name must not be empty")
        if self.stream_index < 0:
            raise ValueError("decoded subtitle stream index must not be negative")

    @property
    def event_count(self) -> int:
        """Return the number of subtitle events produced by the decoder."""

        return len(self.events)


@dataclass(frozen=True, slots=True)
class SubtitleDecodeVerdict:
    """Fail-closed verdict for a final subtitle payload-decode pass."""

    codec_name: str
    stream_index: int
    decoded_event_count: int
    visible_event_count: int
    missing_timestamp_count: int
    missing_duration_count: int
    non_monotonic_timestamp_count: int
    first_timestamp: Decimal | None
    last_timestamp: Decimal | None
    last_end_timestamp: Decimal | None
    passed: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return compact, precision-preserving JSON-ready evidence."""

        value = asdict(self)
        value["first_timestamp"] = (
            None if self.first_timestamp is None else str(self.first_timestamp)
        )
        value["last_end_timestamp"] = (
            None
            if self.last_end_timestamp is None
            else str(self.last_end_timestamp)
        )
        value["last_timestamp"] = (
            None if self.last_timestamp is None else str(self.last_timestamp)
        )
        value["reasons"] = list(self.reasons)
        return value


@dataclass(frozen=True, slots=True)
class SubtitleProbe:
    """Timing and event-density evidence for one subtitle sidecar.

    ffprobe exposes subtitle events as packets.  For the one-stream sidecars
    produced by the encoder, ``packet_count`` is therefore also the available
    event count.  Empty or damaged streams may not expose timing values; those
    remain ``None`` so the forced-track validator can reject them fail-closed.
    """

    codec_name: str
    packet_count: int | None
    start_time: Decimal | None
    duration: Decimal | None

    def __post_init__(self) -> None:
        if not self.codec_name:
            raise ValueError("subtitle codec name must not be empty")
        if self.packet_count is not None and self.packet_count < 0:
            raise ValueError("subtitle packet count must not be negative")
        if self.start_time is not None and not self.start_time.is_finite():
            raise ValueError("subtitle start time must be finite")
        if self.duration is not None:
            if not self.duration.is_finite():
                raise ValueError("subtitle duration must be finite")
            if self.duration < 0:
                raise ValueError("subtitle duration must not be negative")

    @property
    def event_count(self) -> int | None:
        """Return ffprobe's packet count under its subtitle-event meaning."""

        return self.packet_count

    def coverage_fraction(self, title_duration_seconds: Decimal) -> Decimal | None:
        """Return the sidecar span as a fraction of the reviewed title."""

        if self.start_time is None or self.duration is None:
            return None
        title_duration = _required_positive_decimal(
            title_duration_seconds, name="title duration"
        )
        # Measure only the intersection with the reviewed presentation.  This
        # uses both requested timing fields and prevents an offset or malformed
        # sidecar from inflating coverage beyond the title boundary.
        covered_start = max(self.start_time, Decimal(0))
        covered_end = min(self.start_time + self.duration, title_duration)
        covered_duration = max(covered_end - covered_start, Decimal(0))
        return covered_duration / title_duration


def subtitle_probe_command(
    path: Path, *, ffprobe: str = "ffprobe"
) -> list[str]:
    """Build a bounded-metadata ffprobe command for one subtitle sidecar."""

    return [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "s:0",
        "-count_packets",
        "-show_entries",
        (
            "stream=codec_type,codec_name,start_time,duration,nb_read_packets:"
            "format=start_time,duration"
        ),
        "-of",
        "json",
        str(path),
    ]


def subtitle_decode_probe_command(
    path: Path,
    subtitle_ordinal: int = 0,
    *,
    ffprobe: str = "ffprobe",
) -> list[str]:
    """Build a strict full-payload decode probe for a final subtitle stream.

    ``-show_frames`` is intentional: unlike packet counting or remuxing, it
    sends every selected PGS/text packet through FFmpeg's subtitle decoder.
    The type-relative ``s:N`` selector remains correct when unrelated streams
    occur before the subtitle in the container.  Decoder errors are promoted
    with ``explode`` and the successful machine-readable output is compact
    JSON on stdout.
    """

    if (
        isinstance(subtitle_ordinal, bool)
        or not isinstance(subtitle_ordinal, int)
        or subtitle_ordinal < 0
    ):
        raise ValueError("subtitle ordinal must be a non-negative integer")
    return [
        ffprobe,
        "-v",
        "error",
        "-err_detect",
        "explode",
        "-select_streams",
        f"s:{subtitle_ordinal}",
        "-show_frames",
        "-show_entries",
        (
            "stream=index,codec_type,codec_name:"
            "subtitle=media_type,pts_time,start_display_time,end_display_time,"
            "num_rects"
        ),
        "-of",
        "json=compact=1",
        str(path),
    ]


def parse_subtitle_decode_probe(
    document: Mapping[str, Any] | str | bytes,
) -> SubtitleDecodeProbe:
    """Parse decoded-frame JSON while preserving absent timing as evidence.

    Syntactically invalid or structurally ambiguous reports raise immediately.
    Empty reports and missing per-event timing remain explicit in the returned
    probe so :func:`evaluate_subtitle_decode` can produce an auditable failing
    verdict rather than inventing timestamps from container metadata.
    """

    if isinstance(document, bytes):
        try:
            document = document.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SubtitleDecodeError(
                "subtitle decode probe is not valid UTF-8 JSON"
            ) from exc
    if isinstance(document, str):
        try:
            raw = json.loads(document)
        except json.JSONDecodeError as exc:
            raise SubtitleDecodeError(
                "subtitle decode probe is not valid JSON"
            ) from exc
    else:
        raw = document
    if not isinstance(raw, Mapping):
        raise SubtitleDecodeError("subtitle decode probe root must be an object")

    streams = raw.get("streams")
    if not isinstance(streams, list):
        raise SubtitleDecodeError("subtitle decode probe has no streams array")
    if len(streams) != 1 or not isinstance(streams[0], Mapping):
        raise SubtitleDecodeError(
            "subtitle decode probe must describe exactly one selected stream"
        )
    stream = streams[0]
    if str(stream.get("codec_type", "")).lower() != "subtitle":
        raise SubtitleDecodeError("decoded stream is not a subtitle")
    codec_name = str(stream.get("codec_name") or "").strip().lower()
    if not codec_name:
        raise SubtitleDecodeError("subtitle decode probe is missing codec_name")
    stream_index = _required_nonnegative_int(
        stream.get("index"), name="decoded subtitle stream index"
    )

    frames = raw.get("frames")
    if not isinstance(frames, list):
        raise SubtitleDecodeError("subtitle decode probe has no frames array")
    events: list[SubtitleDecodeEvent] = []
    for position, frame in enumerate(frames, start=1):
        if not isinstance(frame, Mapping):
            raise SubtitleDecodeError(
                f"decoded subtitle frame {position} must be an object"
            )
        if str(frame.get("media_type", "")).lower() != "subtitle":
            raise SubtitleDecodeError(
                f"decoded frame {position} is not a subtitle event"
            )
        pts_time = _optional_decimal(
            frame.get("pts_time"),
            name=f"decoded subtitle frame {position} timestamp",
            error_type=SubtitleDecodeError,
        )
        display_start_ms = _optional_decode_nonnegative_int(
            frame.get("start_display_time"),
            name=f"decoded subtitle frame {position} display start",
        )
        display_end_ms = _optional_decode_nonnegative_int(
            frame.get("end_display_time"),
            name=f"decoded subtitle frame {position} display end",
        )
        if (
            codec_name == "hdmv_pgs_subtitle"
            and display_end_ms == _UNKNOWN_DISPLAY_TIME
        ):
            display_end_ms = None
        if (
            display_start_ms is not None
            and display_end_ms is not None
            and display_end_ms < display_start_ms
        ):
            raise SubtitleDecodeError(
                f"decoded subtitle frame {position} display end precedes its start"
            )
        timestamp = (
            pts_time + Decimal(display_start_ms) / Decimal(1000)
            if pts_time is not None and display_start_ms is not None
            else None
        )
        duration = (
            Decimal(display_end_ms - display_start_ms) / Decimal(1000)
            if display_start_ms is not None and display_end_ms is not None
            else None
        )
        num_rects = _required_nonnegative_int(
            frame.get("num_rects"),
            name=f"decoded subtitle frame {position} rectangle count",
        )
        events.append(
            SubtitleDecodeEvent(
                timestamp=timestamp,
                duration=duration,
                num_rects=num_rects,
            )
        )
    return SubtitleDecodeProbe(
        codec_name=codec_name,
        stream_index=stream_index,
        events=tuple(events),
    )


def evaluate_subtitle_decode(probe: SubtitleDecodeProbe) -> SubtitleDecodeVerdict:
    """Evaluate decoded subtitle events without accepting missing evidence."""

    missing_timestamp_count = sum(
        event.timestamp is None for event in probe.events
    )
    missing_duration_count = sum(event.duration is None for event in probe.events)
    timestamps = tuple(
        event.timestamp for event in probe.events if event.timestamp is not None
    )
    non_monotonic_timestamp_count = sum(
        current < previous
        for previous, current in zip(timestamps, timestamps[1:])
    )
    pgs = probe.codec_name == "hdmv_pgs_subtitle"
    visible_events = tuple(
        event
        for event in probe.events
        if event.num_rects > 0
        and event.timestamp is not None
        and (
            pgs
            or (event.duration is not None and event.duration > 0)
        )
    )
    invalid_visible_span_count = sum(
        event.num_rects > 0
        and event.duration is not None
        and event.duration <= 0
        for event in probe.events
    )
    complete_events = tuple(
        event
        for event in probe.events
        if event.timestamp is not None and event.duration is not None
    )
    first_timestamp = min(timestamps) if timestamps else None
    last_timestamp = max(timestamps) if timestamps else None
    last_end_timestamp = (
        max(event.timestamp + event.duration for event in complete_events)
        if complete_events
        else None
    )

    reasons: list[str] = []
    if probe.event_count == 0:
        reasons.append("subtitle decoder produced zero frames/events")
    if missing_timestamp_count:
        reasons.append(
            f"{missing_timestamp_count} decoded subtitle event(s) lack a timestamp"
        )
    if missing_duration_count and not pgs:
        reasons.append(
            f"{missing_duration_count} decoded subtitle event(s) lack a duration"
        )
    if invalid_visible_span_count:
        reasons.append(
            f"{invalid_visible_span_count} visible subtitle event(s) have no positive display span"
        )
    if not visible_events:
        reasons.append("subtitle decoder produced no visible timed event")
    if non_monotonic_timestamp_count:
        reasons.append(
            f"{non_monotonic_timestamp_count} decoded subtitle timestamp(s) are out of order"
        )
    return SubtitleDecodeVerdict(
        codec_name=probe.codec_name,
        stream_index=probe.stream_index,
        decoded_event_count=probe.event_count,
        visible_event_count=len(visible_events),
        missing_timestamp_count=missing_timestamp_count,
        missing_duration_count=missing_duration_count,
        non_monotonic_timestamp_count=non_monotonic_timestamp_count,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        last_end_timestamp=last_end_timestamp,
        passed=not reasons,
        reasons=tuple(reasons),
    )


def require_subtitle_decode(
    document: SubtitleDecodeProbe | Mapping[str, Any] | str | bytes,
) -> SubtitleDecodeVerdict:
    """Return a passing final-subtitle verdict or raise for manual review."""

    probe = (
        document
        if isinstance(document, SubtitleDecodeProbe)
        else parse_subtitle_decode_probe(document)
    )
    verdict = evaluate_subtitle_decode(probe)
    if not verdict.passed:
        raise SubtitleDecodeError("; ".join(verdict.reasons))
    return verdict


def parse_subtitle_probe(
    document: Mapping[str, Any] | str | bytes,
) -> SubtitleProbe:
    """Parse one-sidecar ffprobe JSON without inventing missing evidence."""

    if isinstance(document, (str, bytes)):
        try:
            raw = json.loads(document)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SubtitleProbeError("subtitle probe is not valid JSON") from exc
    else:
        raw = document
    if not isinstance(raw, Mapping):
        raise SubtitleProbeError("subtitle probe root must be an object")

    streams = raw.get("streams")
    if not isinstance(streams, list):
        raise SubtitleProbeError("subtitle probe has no streams array")
    if len(streams) != 1 or not isinstance(streams[0], Mapping):
        raise SubtitleProbeError(
            "subtitle sidecar must contain exactly one selected subtitle stream"
        )
    stream = streams[0]
    if str(stream.get("codec_type", "")).lower() != "subtitle":
        raise SubtitleProbeError("selected sidecar stream is not a subtitle")

    codec_name = str(stream.get("codec_name") or "").strip().lower()
    if not codec_name:
        raise SubtitleProbeError("subtitle probe is missing codec_name")

    format_data = raw.get("format")
    if not isinstance(format_data, Mapping):
        format_data = {}
    packet_count = _optional_nonnegative_int(
        stream.get("nb_read_packets"), name="subtitle packet count"
    )
    start_time = _optional_decimal(
        _prefer_available(stream.get("start_time"), format_data.get("start_time")),
        name="subtitle start time",
    )
    duration = _optional_decimal(
        _prefer_available(stream.get("duration"), format_data.get("duration")),
        name="subtitle duration",
    )
    if duration is not None and duration < 0:
        raise SubtitleProbeError("subtitle duration must not be negative")
    return SubtitleProbe(
        codec_name=codec_name,
        packet_count=packet_count,
        start_time=start_time,
        duration=duration,
    )


def validate_subtitle_classification(
    probe: SubtitleProbe,
    *,
    subtitle_kind: str,
    title_duration_seconds: Decimal | str | int | float,
    forced_packet_limit: int = DEFAULT_FORCED_PACKET_LIMIT,
    forced_coverage_limit: Decimal | str | int | float = (
        DEFAULT_FORCED_COVERAGE_LIMIT
    ),
) -> tuple[str, ...]:
    """Validate an explicit ``full``/``forced`` classification.

    Missing/empty timing evidence is invalid for either classification.  A
    forced track is additionally suspicious when both its event count and
    temporal span look like a full dialogue track; an unreadable or sparse
    probe must never silently enable a Matroska forced flag.
    """

    if subtitle_kind not in {"full", "forced"}:
        return (
            "subtitle must be explicitly classified as full or forced before muxing",
        )
    if (
        isinstance(forced_packet_limit, bool)
        or not isinstance(forced_packet_limit, int)
        or forced_packet_limit < 1
    ):
        raise ValueError("forced packet limit must be a positive integer")
    title_duration = _required_positive_decimal(
        title_duration_seconds, name="title duration"
    )
    coverage_limit = _required_fraction(
        forced_coverage_limit, name="forced coverage limit"
    )

    missing: list[str] = []
    if probe.packet_count is None:
        missing.append("packet/event count")
    if probe.start_time is None:
        missing.append("start time")
    if probe.duration is None:
        missing.append("duration")
    if missing:
        return (
            f"{subtitle_kind} subtitle classification lacks required probe evidence: "
            + ", ".join(missing),
        )

    assert probe.packet_count is not None
    assert probe.duration is not None
    coverage = probe.coverage_fraction(title_duration)
    assert coverage is not None
    if probe.packet_count == 0 or probe.duration <= 0 or coverage <= 0:
        return (
            f"{subtitle_kind} subtitle sidecar is empty or has no in-title timing coverage",
        )
    if subtitle_kind == "full":
        return ()
    if probe.packet_count > forced_packet_limit and coverage > coverage_limit:
        return (
            "subtitle classified as forced has full-track density: "
            f"{probe.packet_count} packets/events and "
            f"{coverage * Decimal(100):.2f}% title coverage; "
            "review it as full or supply a verified forced-only track",
        )
    return ()


def _optional_nonnegative_int(value: Any, *, name: str) -> int | None:
    if value in (None, "", "N/A"):
        return None
    if isinstance(value, bool):
        raise SubtitleProbeError(f"{name} is not an integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise SubtitleProbeError(f"{name} is not an integer") from exc
    if parsed < 0:
        raise SubtitleProbeError(f"{name} must not be negative")
    return parsed


def _required_nonnegative_int(value: Any, *, name: str) -> int:
    if value in (None, "", "N/A"):
        raise SubtitleDecodeError(f"{name} is missing")
    if isinstance(value, bool):
        raise SubtitleDecodeError(f"{name} is not an integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise SubtitleDecodeError(f"{name} is not an integer") from exc
    if parsed < 0:
        raise SubtitleDecodeError(f"{name} must not be negative")
    return parsed


def _optional_decode_nonnegative_int(value: Any, *, name: str) -> int | None:
    if value in (None, "", "N/A"):
        return None
    if isinstance(value, bool):
        raise SubtitleDecodeError(f"{name} is not an integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise SubtitleDecodeError(f"{name} is not an integer") from exc
    if parsed < 0:
        raise SubtitleDecodeError(f"{name} must not be negative")
    return parsed


def _prefer_available(primary: Any, fallback: Any) -> Any:
    return fallback if primary in (None, "", "N/A") else primary


def _optional_decimal(
    value: Any,
    *,
    name: str,
    error_type: type[ValueError] = SubtitleProbeError,
) -> Decimal | None:
    if value in (None, "", "N/A"):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise error_type(f"{name} is not numeric") from exc
    if not parsed.is_finite():
        raise error_type(f"{name} must be finite")
    return parsed


def _required_positive_decimal(value: Any, *, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return parsed


def _required_fraction(value: Any, *, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not parsed.is_finite() or not Decimal(0) < parsed < Decimal(1):
        raise ValueError(f"{name} must be between zero and one")
    return parsed


__all__ = [
    "DEFAULT_FORCED_COVERAGE_LIMIT",
    "DEFAULT_FORCED_PACKET_LIMIT",
    "SubtitleDecodeError",
    "SubtitleDecodeEvent",
    "SubtitleDecodeProbe",
    "SubtitleDecodeVerdict",
    "SubtitleProbe",
    "SubtitleProbeError",
    "evaluate_subtitle_decode",
    "parse_subtitle_decode_probe",
    "parse_subtitle_probe",
    "require_subtitle_decode",
    "subtitle_decode_probe_command",
    "subtitle_probe_command",
    "validate_subtitle_classification",
]
