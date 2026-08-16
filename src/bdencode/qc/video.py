"""Timeline-safe video frame selection and comparison command plans."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence


FRAME_TYPES = ("I", "P", "B")
MIN_STRONG_COMPARISON_PAIRS = 20
DEFAULT_COMPARISON_PAIRS = 24
MAX_STRONG_COMPARISON_PAIRS = 50
DEFAULT_KEYFRAME_PREROLL_SECONDS = Decimal("12")
COMPARISON_FONT_FILE = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


class FrameSelectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CropMargins:
    """Active-picture crop margins, expressed in decoded luma pixels."""

    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    def __post_init__(self) -> None:
        if min(self.left, self.top, self.right, self.bottom) < 0:
            raise ValueError("crop margins cannot be negative")

    @property
    def applied(self) -> bool:
        return any((self.left, self.top, self.right, self.bottom))

    def validate_subsampled(
        self, *, chroma_width: int = 2, chroma_height: int = 2
    ) -> None:
        """Reject margins which would shift a subsampled chroma plane."""

        if chroma_width < 1 or chroma_height < 1:
            raise ValueError("chroma subsampling factors must be positive")
        if self.left % chroma_width or self.right % chroma_width:
            raise ValueError("horizontal crop margins are not chroma aligned")
        if self.top % chroma_height or self.bottom % chroma_height:
            raise ValueError("vertical crop margins are not chroma aligned")

    def ffmpeg_filter(self) -> str:
        return (
            f"crop=iw-{self.left + self.right}:ih-{self.top + self.bottom}:"
            f"{self.left}:{self.top}"
        )

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FrameRecord:
    presentation_index: int
    pts_seconds: Decimal
    pict_type: str | None
    key_frame: bool = False
    coded_picture_number: int | None = None
    # A bounded ffprobe sample can begin at a non-zero container timestamp.
    # ``pts_seconds`` remains normalized to the VapourSynth timeline so the
    # existing alignment/selection code keeps its meaning; this field retains
    # the real timestamp needed for an accurate input seek.
    container_pts_seconds: Decimal | None = None

    def __post_init__(self) -> None:
        if self.presentation_index < 0:
            raise ValueError("presentation_index cannot be negative")
        if self.pict_type is not None and self.pict_type not in FRAME_TYPES:
            raise ValueError(f"unsupported frame type: {self.pict_type}")

    @property
    def seek_pts_seconds(self) -> Decimal:
        return (
            self.pts_seconds
            if self.container_pts_seconds is None
            else self.container_pts_seconds
        )


@dataclass(frozen=True, slots=True)
class FramePair:
    category: str
    presentation_index: int
    encoded_pts_seconds: Decimal
    reference_pts_seconds: Decimal
    encoded_pict_type: str
    source_pict_type: str | None
    dual_type_match: bool

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["encoded_pts_seconds"] = str(self.encoded_pts_seconds)
        result["reference_pts_seconds"] = str(self.reference_pts_seconds)
        return result


@dataclass(frozen=True, slots=True)
class VapourSynthInfo:
    frames: int
    fps_numerator: int
    fps_denominator: int

    def __post_init__(self) -> None:
        if self.frames < 1 or self.fps_numerator < 1 or self.fps_denominator < 1:
            raise ValueError("invalid VapourSynth clip information")

    def pts_for_frame(self, presentation_index: int) -> Decimal:
        if not 0 <= presentation_index < self.frames:
            raise FrameSelectionError(
                f"reference frame {presentation_index} is outside the VapourSynth clip"
            )
        return (
            Decimal(presentation_index)
            * Decimal(self.fps_denominator)
            / Decimal(self.fps_numerator)
        )

    @property
    def duration_seconds(self) -> Decimal:
        return (
            Decimal(self.frames)
            * Decimal(self.fps_denominator)
            / Decimal(self.fps_numerator)
        )


@dataclass(frozen=True, slots=True)
class FrameProbeInterval:
    """One bounded ffprobe interval on the normalized clip timeline."""

    start_seconds: Decimal
    duration_seconds: Decimal

    def __post_init__(self) -> None:
        if self.start_seconds < 0:
            raise ValueError("sample interval start cannot be negative")
        if self.duration_seconds <= 0:
            raise ValueError("sample interval duration must be positive")

    @property
    def end_seconds(self) -> Decimal:
        return self.start_seconds + self.duration_seconds

    def ffprobe_value(
        self,
        pts_origin: Decimal,
        *,
        decode_preroll_seconds: Decimal = Decimal(0),
    ) -> str:
        """Render an absolute interval with optional decoder-safe GOP preroll.

        FFprobe seeks each ``read_intervals`` entry independently. Starting
        exactly at a requested sample can therefore expose an open-GOP B-frame
        without its references. Seeking at least one maximum GOP earlier lets
        the demuxer land on an earlier keyframe and warms the decoder before
        the actual evidence window.
        """

        if not pts_origin.is_finite():
            raise ValueError("sample PTS origin must be finite")
        if not decode_preroll_seconds.is_finite() or decode_preroll_seconds < 0:
            raise ValueError("decode preroll must be finite and non-negative")
        normalized_start = max(Decimal(0), self.start_seconds - decode_preroll_seconds)
        start = pts_origin + normalized_start
        end = pts_origin + self.end_seconds
        return f"{_format_decimal(start)}%{_format_decimal(end)}"


def parse_vspipe_info(text: str) -> VapourSynthInfo:
    """Parse the stable ``vspipe --info`` frame-count/FPS fields."""

    frames = re.search(r"(?im)^Frames:\s*(\d+)\s*$", text)
    fps = re.search(r"(?im)^FPS:\s*(\d+)\s*/\s*(\d+)(?:\s|$)", text)
    if not frames or not fps:
        raise FrameSelectionError("vspipe --info did not report frame count and FPS")
    return VapourSynthInfo(int(frames.group(1)), int(fps.group(1)), int(fps.group(2)))


def vspipe_info_command(script: Path, *, vspipe: str = "vspipe") -> list[str]:
    return [vspipe, "--info", str(script), "-"]


def ffprobe_frame_origin_command(path: Path, *, ffprobe: str = "ffprobe") -> list[str]:
    """Decode only the opening second to establish the real video PTS origin."""

    return [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-read_intervals",
        "%+1",
        "-show_frames",
        "-show_entries",
        "frame=media_type,best_effort_timestamp_time,pts_time",
        "-of",
        "json",
        str(path),
    ]


def parse_ffprobe_frame_origin(
    document: str | bytes | Mapping[str, Any],
) -> Decimal:
    """Return the first decoded video PTS from a bounded opening probe."""

    if isinstance(document, (str, bytes)):
        raw = json.loads(document)
    else:
        raw = document
    origins: list[Decimal] = []
    for item in raw.get("frames", []):
        if not isinstance(item, Mapping) or item.get("media_type") not in (
            None,
            "video",
        ):
            continue
        value = item.get("best_effort_timestamp_time", item.get("pts_time"))
        if value is None:
            continue
        try:
            parsed = Decimal(str(value))
        except InvalidOperation as exc:
            raise FrameSelectionError(f"invalid opening frame PTS: {value}") from exc
        if parsed.is_finite():
            origins.append(parsed)
    if not origins:
        raise FrameSelectionError("opening ffprobe sample contains no video frame")
    return min(origins)


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def plan_sample_intervals(
    info: VapourSynthInfo,
    *,
    distributed_windows: int = DEFAULT_COMPARISON_PAIRS,
    window_seconds: Decimal = Decimal("2"),
    opening_seconds: Decimal = Decimal("6"),
) -> tuple[FrameProbeInterval, ...]:
    """Plan a short opening sample plus bounded samples across the title.

    The default provides 24 independent regions for a release-grade 20--50
    frame comparison instead of clustering adjacent P/B frames. The union of
    returned intervals is never longer than
    ``opening_seconds + distributed_windows * window_seconds``. Overlapping
    windows on short clips are merged, so they do not decode the same region
    twice. The opening sample is important: it gives sampled frame parsing a
    real PTS origin instead of guessing an index from a mid-title seek.
    """

    if distributed_windows < 0:
        raise ValueError("distributed_windows cannot be negative")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    if opening_seconds <= 0:
        raise ValueError("opening_seconds must be positive")

    clip_duration = info.duration_seconds
    raw: list[FrameProbeInterval] = [
        FrameProbeInterval(Decimal(0), min(opening_seconds, clip_duration))
    ]
    if clip_duration > opening_seconds and distributed_windows:
        effective_window = min(window_seconds, clip_duration)
        half_window = effective_window / 2
        latest_start = clip_duration - effective_window
        for position in range(1, distributed_windows + 1):
            center = (
                clip_duration * Decimal(position) / Decimal(distributed_windows + 1)
            )
            start = max(Decimal(0), min(center - half_window, latest_start))
            raw.append(FrameProbeInterval(start, effective_window))

    merged: list[FrameProbeInterval] = []
    for interval in sorted(raw, key=lambda item: item.start_seconds):
        if not merged or interval.start_seconds > merged[-1].end_seconds:
            merged.append(interval)
            continue
        prior = merged[-1]
        end = max(prior.end_seconds, interval.end_seconds)
        merged[-1] = FrameProbeInterval(
            prior.start_seconds, min(end, clip_duration) - prior.start_seconds
        )
    return tuple(merged)


def recommended_comparison_pair_count(requested: int = DEFAULT_COMPARISON_PAIRS) -> int:
    """Validate a release-grade visual sample count.

    The helper is deliberately independent of title length so configuration
    validation is cheap and deterministic. Very short clips which cannot
    supply the requested unique frames still fail explicitly at selection.
    """

    if not MIN_STRONG_COMPARISON_PAIRS <= requested <= MAX_STRONG_COMPARISON_PAIRS:
        raise ValueError(
            "comparison pair count must be between "
            f"{MIN_STRONG_COMPARISON_PAIRS} and {MAX_STRONG_COMPARISON_PAIRS}"
        )
    return requested


def parse_ffprobe_frames(
    document: str | bytes | Mapping[str, Any],
) -> list[FrameRecord]:
    if isinstance(document, (str, bytes)):
        raw = json.loads(document)
    else:
        raw = document
    frames: list[FrameRecord] = []
    presentation_index = 0
    for item in raw.get("frames", []):
        if item.get("media_type") not in (None, "video"):
            continue
        value = item.get("best_effort_timestamp_time", item.get("pts_time"))
        if value is None:
            continue
        try:
            pts = Decimal(str(value))
        except InvalidOperation as exc:
            raise FrameSelectionError(f"invalid frame PTS: {value}") from exc
        pict_type = item.get("pict_type")
        frames.append(
            FrameRecord(
                presentation_index=presentation_index,
                pts_seconds=pts,
                pict_type=pict_type if pict_type in FRAME_TYPES else None,
                key_frame=bool(int(item.get("key_frame", 0))),
                coded_picture_number=_optional_int(item.get("coded_picture_number")),
            )
        )
        presentation_index += 1
    return frames


def parse_sampled_ffprobe_frames(
    document: str | bytes | Mapping[str, Any],
    info: VapourSynthInfo,
    *,
    pts_origin: Decimal,
    pts_tolerance: Decimal = Decimal("0.001"),
) -> list[FrameRecord]:
    """Map disjoint ffprobe samples back to global presentation indexes.

    ``pts_origin`` comes from a separate bounded opening probe, preventing a
    missing opening interval from silently rebasing a mid-title frame to index
    zero. Matroska commonly rounds CFR timestamps to milliseconds;
    ``pts_tolerance`` permits that rounding while refusing shifted frames.
    Samples can contain duplicate frames due to keyframe seeking, which are
    deduplicated by the recovered global index.
    """

    if not pts_origin.is_finite():
        raise ValueError("sample PTS origin must be finite")
    if pts_tolerance < 0:
        raise ValueError("PTS tolerance cannot be negative")
    if isinstance(document, (str, bytes)):
        raw = json.loads(document)
    else:
        raw = document

    values: list[tuple[Decimal, Mapping[str, Any]]] = []
    for item in raw.get("frames", []):
        if not isinstance(item, Mapping) or item.get("media_type") not in (
            None,
            "video",
        ):
            continue
        value = item.get("best_effort_timestamp_time", item.get("pts_time"))
        if value is None:
            continue
        try:
            pts = Decimal(str(value))
        except InvalidOperation as exc:
            raise FrameSelectionError(f"invalid sampled frame PTS: {value}") from exc
        if not pts.is_finite():
            raise FrameSelectionError(f"invalid sampled frame PTS: {value}")
        values.append((pts, item))
    if not values:
        raise FrameSelectionError("sampled ffprobe document contains no video frames")

    by_index: dict[int, tuple[FrameRecord, Decimal]] = {}
    for container_pts, item in values:
        frame_position = (
            (container_pts - pts_origin)
            * Decimal(info.fps_numerator)
            / Decimal(info.fps_denominator)
        )
        presentation_index = int(
            frame_position.to_integral_value(rounding=ROUND_HALF_UP)
        )
        if not 0 <= presentation_index < info.frames:
            raise FrameSelectionError(
                "sampled frame maps outside the VapourSynth timeline: "
                f"{presentation_index}"
            )
        normalized_pts = info.pts_for_frame(presentation_index)
        expected_container_pts = pts_origin + normalized_pts
        alignment_error = abs(container_pts - expected_container_pts)
        if alignment_error > pts_tolerance:
            raise FrameSelectionError(
                "sampled frame PTS does not align with the VapourSynth timeline: "
                f"index={presentation_index}, error={alignment_error}"
            )
        pict_type = item.get("pict_type")
        candidate = FrameRecord(
            presentation_index=presentation_index,
            pts_seconds=normalized_pts,
            pict_type=pict_type if pict_type in FRAME_TYPES else None,
            key_frame=bool(int(item.get("key_frame", 0))),
            coded_picture_number=_optional_int(item.get("coded_picture_number")),
            container_pts_seconds=container_pts,
        )
        prior = by_index.get(presentation_index)
        if prior is None:
            by_index[presentation_index] = (candidate, alignment_error)
            continue
        prior_frame, prior_error = prior
        if (
            prior_frame.pict_type is not None
            and candidate.pict_type is not None
            and prior_frame.pict_type != candidate.pict_type
        ):
            raise FrameSelectionError(
                "duplicate sampled frame has conflicting picture types: "
                f"index={presentation_index}"
            )
        # Retain the timestamp closest to the ideal CFR position. If both are
        # equally close, prefer the record that contains a known picture type.
        if alignment_error < prior_error or (
            alignment_error == prior_error
            and prior_frame.pict_type is None
            and candidate.pict_type is not None
        ):
            by_index[presentation_index] = (candidate, alignment_error)

    if 0 not in by_index:
        raise FrameSelectionError(
            "sampled ffprobe document does not contain the opening video frame"
        )
    return [by_index[index][0] for index in sorted(by_index)]


def _optional_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _evenly_spaced(items: Sequence[FrameRecord], count: int) -> list[FrameRecord]:
    if len(items) < count:
        raise FrameSelectionError(
            f"need {count} frames but only {len(items)} are available"
        )
    if count == 1:
        return [items[len(items) // 2]]
    indexes = [
        round(position * (len(items) - 1) / (count - 1)) for position in range(count)
    ]
    return [items[index] for index in indexes]


def _evenly_spaced_on_timeline(
    items: Sequence[FrameRecord], count: int, timeline_frames: int
) -> list[FrameRecord]:
    if len(items) < count:
        raise FrameSelectionError(
            f"need {count} frames but only {len(items)} are available"
        )
    if timeline_frames < 1:
        raise ValueError("timeline_frames must be positive")
    if count == 1:
        targets = [(timeline_frames - 1) / 2]
    else:
        # Avoid choosing only the opening/credits while keeping samples spread
        # across the useful interior of the title.
        targets = [
            (position + 1) * (timeline_frames - 1) / (count + 1)
            for position in range(count)
        ]
    remaining = list(items)
    selected: list[FrameRecord] = []
    for target in targets:
        chosen = min(
            remaining,
            key=lambda item: (
                abs(item.presentation_index - target),
                item.presentation_index,
            ),
        )
        selected.append(chosen)
        remaining.remove(chosen)
    return sorted(selected, key=lambda item: item.presentation_index)


def _balanced_type_counts(total: int) -> dict[str, int]:
    """Allocate evidence evenly while retaining the historical 1I/2P/2B split."""

    base, remainder = divmod(total, len(FRAME_TYPES))
    requested = {name: base for name in FRAME_TYPES}
    # Predicted frames remain slightly more useful when the total is not a
    # multiple of three, but large plans no longer collapse to a single I-frame.
    for category in ("P", "B", "I")[:remainder]:
        requested[category] += 1
    return requested


def _category_schedule(requested: Mapping[str, int]) -> list[str]:
    remaining = dict(requested)
    schedule: list[str] = []
    while any(remaining.values()):
        for category in ("P", "B", "I"):
            if remaining[category]:
                schedule.append(category)
                remaining[category] -= 1
    return schedule


def _globally_distributed_frames(
    candidates: Mapping[str, Sequence[FrameRecord]],
    requested: Mapping[str, int],
    *,
    timeline_frames: int,
    minimum_separation_frames: int,
) -> list[FrameRecord]:
    """Select one type at each global target instead of clustered type groups."""

    schedule = _category_schedule(requested)
    selected: list[FrameRecord] = []
    used_indexes: set[int] = set()
    for position, category in enumerate(schedule, start=1):
        target = position * (timeline_frames - 1) / (len(schedule) + 1)
        eligible = [
            item
            for item in candidates[category]
            if item.presentation_index not in used_indexes
            and all(
                abs(item.presentation_index - prior.presentation_index)
                >= minimum_separation_frames
                for prior in selected
            )
        ]
        if not eligible:
            raise FrameSelectionError(
                f"need a unique separated {category} frame near timeline target "
                f"{position}/{len(schedule)}"
            )
        chosen = min(
            eligible,
            key=lambda item: (
                abs(item.presentation_index - target),
                item.presentation_index,
            ),
        )
        selected.append(chosen)
        used_indexes.add(chosen.presentation_index)
    return sorted(selected, key=lambda item: item.presentation_index)


def select_frame_pairs(
    encoded: Sequence[FrameRecord],
    reference: Sequence[FrameRecord],
    *,
    per_type: int = 4,
    pts_tolerance: Decimal = Decimal("0.001"),
    dual_type_match: bool = False,
    total_pairs: int | None = None,
    timeline_frames: int | None = None,
    minimum_separation_frames: int | None = None,
) -> list[FramePair]:
    """Select identical presentation frames, categorized by final encode type.

    The reference frame is found by presentation index first. The PTS check prevents
    accidentally comparing shifted content. ``dual_type_match`` only filters already
    aligned pairs; it never substitutes a different frame to force a type match.
    """
    if per_type < 1:
        raise ValueError("per_type must be positive")
    if total_pairs is not None and total_pairs < len(FRAME_TYPES):
        raise ValueError("total_pairs must leave room for mandatory I/P/B frames")
    if timeline_frames is not None and timeline_frames < 1:
        raise ValueError("timeline_frames must be positive")
    if minimum_separation_frames is not None and minimum_separation_frames < 1:
        raise ValueError("minimum frame separation must be positive")
    reference_by_index = {frame.presentation_index: frame for frame in reference}
    if len(reference_by_index) != len(reference):
        raise FrameSelectionError("reference presentation indexes are not unique")

    candidates: dict[str, list[FrameRecord]] = {name: [] for name in FRAME_TYPES}
    for frame in encoded:
        if frame.pict_type not in candidates:
            continue
        source = reference_by_index.get(frame.presentation_index)
        if source is None:
            continue
        if abs(frame.pts_seconds - source.pts_seconds) > pts_tolerance:
            continue
        if dual_type_match and source.pict_type != frame.pict_type:
            continue
        candidates[frame.pict_type].append(frame)

    if total_pairs is None:
        requested = {name: per_type for name in FRAME_TYPES}
    else:
        requested = _balanced_type_counts(total_pairs)

    if timeline_frames is not None:
        pair_total = sum(requested.values())
        separation = minimum_separation_frames or max(
            1, timeline_frames // (pair_total * 4)
        )
        try:
            selected_frames = _globally_distributed_frames(
                candidates,
                requested,
                timeline_frames=timeline_frames,
                minimum_separation_frames=separation,
            )
        except FrameSelectionError as exc:
            qualifier = "aligned dual-type" if dual_type_match else "aligned"
            raise FrameSelectionError(
                f"missing mandatory {qualifier} comparison frames: {exc}"
            ) from exc
        pairs = []
        for frame in selected_frames:
            category = frame.pict_type
            assert category in FRAME_TYPES
            source = reference_by_index[frame.presentation_index]
            pairs.append(
                FramePair(
                    category=category,
                    presentation_index=frame.presentation_index,
                    encoded_pts_seconds=frame.pts_seconds,
                    reference_pts_seconds=source.pts_seconds,
                    encoded_pict_type=category,
                    source_pict_type=source.pict_type,
                    dual_type_match=source.pict_type == category,
                )
            )
        return pairs

    pairs: list[FramePair] = []
    for category in FRAME_TYPES:
        try:
            count = requested[category]
            selected = _evenly_spaced(candidates[category], count)
        except FrameSelectionError as exc:
            qualifier = " aligned dual-type" if dual_type_match else " aligned"
            raise FrameSelectionError(
                f"missing mandatory {category}{qualifier} comparison frames: {exc}"
            ) from exc
        for frame in selected:
            source = reference_by_index[frame.presentation_index]
            pairs.append(
                FramePair(
                    category=category,
                    presentation_index=frame.presentation_index,
                    encoded_pts_seconds=frame.pts_seconds,
                    reference_pts_seconds=source.pts_seconds,
                    encoded_pict_type=category,
                    source_pict_type=source.pict_type,
                    dual_type_match=source.pict_type == category,
                )
            )
    return pairs


def ffprobe_frame_command(path: Path, *, ffprobe: str = "ffprobe") -> list[str]:
    return [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_frames",
        "-show_entries",
        "frame=media_type,best_effort_timestamp_time,pts_time,pict_type,key_frame,coded_picture_number",
        "-of",
        "json",
        str(path),
    ]


def ffprobe_sampled_frame_command(
    path: Path,
    intervals: Sequence[FrameProbeInterval],
    *,
    pts_origin: Decimal,
    decode_preroll_seconds: Decimal = DEFAULT_KEYFRAME_PREROLL_SECONDS,
    ffprobe: str = "ffprobe",
) -> list[str]:
    """Probe bounded windows after warming the decoder from an earlier GOP."""

    if not intervals:
        raise ValueError("at least one frame probe interval is required")
    ordered = sorted(intervals, key=lambda item: item.start_seconds)
    for left, right in zip(ordered, ordered[1:]):
        if left.end_seconds > right.start_seconds:
            raise ValueError("frame probe intervals must not overlap")
    if not decode_preroll_seconds.is_finite() or decode_preroll_seconds < 0:
        raise ValueError("decode preroll must be finite and non-negative")
    return [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-read_intervals",
        ",".join(
            item.ffprobe_value(
                pts_origin, decode_preroll_seconds=decode_preroll_seconds
            )
            for item in ordered
        ),
        "-show_frames",
        "-show_entries",
        "frame=media_type,best_effort_timestamp_time,pts_time,pict_type,key_frame,coded_picture_number",
        "-of",
        "json",
        str(path),
    ]


def png_filter_chain(
    *,
    hdr_native: bool,
    source_hdr10: bool,
    color_primaries: str,
    color_transfer: str,
    color_matrix: str,
    color_range: str,
) -> list[str]:
    input_range = "full" if color_range == "full" else "tv"
    if source_hdr10 and not hdr_native:
        # Fixed, recorded proof transform. Native-PQ images remain the metric evidence.
        return [
            f"zscale=pin={color_primaries}:tin={color_transfer}:min={color_matrix}:"
            f"rin={input_range}:p=bt2020:t=linear:m=bt2020nc:r=tv:npl=100",
            "format=gbrpf32le",
            "tonemap=mobius:param=0.3:desat=0",
            "zscale=p=bt709:t=bt709:m=bt709:r=tv",
            "format=rgb48be",
        ]
    return [
        f"zscale=pin={color_primaries}:tin={color_transfer}:min={color_matrix}:"
        f"rin={input_range}:p={color_primaries}:t={color_transfer}:"
        f"m={color_matrix}:r=full",
        "format=gbrp16le",
    ]


def extract_png_command(
    input_path: Path,
    presentation_index: int,
    output_path: Path,
    *,
    hdr_native: bool,
    source_hdr10: bool = False,
    color_primaries: str | None = None,
    color_transfer: str | None = None,
    color_matrix: str | None = None,
    color_range: str = "limited",
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    if presentation_index < 0:
        raise ValueError("presentation_index cannot be negative")
    color_primaries = color_primaries or ("bt2020" if source_hdr10 else "bt709")
    color_transfer = color_transfer or ("smpte2084" if source_hdr10 else "bt709")
    color_matrix = color_matrix or ("bt2020nc" if source_hdr10 else "bt709")
    select = f"select=eq(n\\,{presentation_index})"
    filters = [select]
    filters.extend(
        png_filter_chain(
            hdr_native=hdr_native,
            source_hdr10=source_hdr10,
            color_primaries=color_primaries,
            color_transfer=color_transfer,
            color_matrix=color_matrix,
            color_range=color_range,
        )
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "warning",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-vf",
        ",".join(filters),
        "-fps_mode",
        "passthrough",
        "-frames:v",
        "1",
        "-update",
        "1",
        "-compression_level",
        "6",
        "-y",
        str(output_path),
    ]


def extract_png_at_timestamp_command(
    input_path: Path,
    pts_seconds: Decimal,
    output_path: Path,
    *,
    hdr_native: bool,
    source_hdr10: bool = False,
    color_primaries: str | None = None,
    color_transfer: str | None = None,
    color_matrix: str | None = None,
    color_range: str = "limited",
    decode_start_seconds: Decimal | None = None,
    decode_preroll_seconds: Decimal = DEFAULT_KEYFRAME_PREROLL_SECONDS,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Seek to and decode one known sampled presentation timestamp.

    A coarse input seek is placed one maximum GOP before the target, then an
    accurate output seek decodes/discards the preroll. This prevents open-GOP
    recovery warnings from contaminating the selected frame without decoding
    from frame zero for every comparison image. ``decode_start_seconds`` lets a
    caller supply an exact earlier keyframe timestamp when one is known.
    """

    if not pts_seconds.is_finite():
        raise ValueError("PNG seek timestamp must be finite")
    if not decode_preroll_seconds.is_finite() or decode_preroll_seconds < 0:
        raise ValueError("decode preroll must be finite and non-negative")
    if decode_start_seconds is not None:
        if not decode_start_seconds.is_finite():
            raise ValueError("PNG decode start timestamp must be finite")
        if decode_start_seconds > pts_seconds:
            raise ValueError("PNG decode start cannot follow the target timestamp")
        seek_start = decode_start_seconds
    elif pts_seconds > 0:
        seek_start = max(Decimal(0), pts_seconds - decode_preroll_seconds)
    else:
        seek_start = None
    discard_seconds = (
        Decimal(0) if seek_start is None else max(Decimal(0), pts_seconds - seek_start)
    )
    color_primaries = color_primaries or ("bt2020" if source_hdr10 else "bt709")
    color_transfer = color_transfer or ("smpte2084" if source_hdr10 else "bt709")
    color_matrix = color_matrix or ("bt2020nc" if source_hdr10 else "bt709")
    filters = png_filter_chain(
        hdr_native=hdr_native,
        source_hdr10=source_hdr10,
        color_primaries=color_primaries,
        color_transfer=color_transfer,
        color_matrix=color_matrix,
        color_range=color_range,
    )
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "warning",
    ]
    if seek_start is not None:
        command.extend(
            [
                "-seek_timestamp",
                "1",
                "-ss",
                _format_decimal(seek_start),
                "-accurate_seek",
            ]
        )
    command.extend(["-i", str(input_path)])
    if discard_seconds:
        command.extend(["-ss", _format_decimal(discard_seconds)])
    command.extend(
        [
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            ",".join(filters),
            "-fps_mode",
            "passthrough",
            "-frames:v",
            "1",
            "-update",
            "1",
            "-compression_level",
            "6",
            "-y",
            str(output_path),
        ]
    )
    return command


def annotate_comparison_png_command(
    input_path: Path,
    output_path: Path,
    *,
    image_role: Literal["SOURCE", "ENCODE"],
    presentation_index: int,
    pict_type: str,
    matched_to_type: bool = False,
    ffmpeg: str = "ffmpeg",
    font_file: Path = COMPARISON_FONT_FILE,
) -> list[str]:
    """Add a lossless header without covering any source-frame pixels.

    The comparison metrics intentionally use the unannotated input PNG.  This
    command produces the operator/BBCode image by extending the canvas upward,
    so the label neither hides picture detail nor changes the measured pixels.
    """

    if image_role not in {"SOURCE", "ENCODE"}:
        raise ValueError("image_role must be SOURCE or ENCODE")
    if presentation_index < 0:
        raise ValueError("presentation_index cannot be negative")
    if pict_type not in FRAME_TYPES:
        raise ValueError(f"unsupported frame type: {pict_type}")
    if matched_to_type and image_role != "SOURCE":
        raise ValueError("matched_to_type is valid only for SOURCE images")
    if input_path == output_path:
        raise ValueError("annotated PNG output must differ from its input")

    font = _escape_filter_path(font_file)
    type_text = (
        f"MATCHED TO {pict_type}-FRAME" if matched_to_type else f"{pict_type}-FRAME"
    )
    text = f"{image_role} | 0-BASED INDEX {presentation_index:09d} | {type_text}"
    filters = (
        "pad=iw:ih+max(40\\,ih/16):0:max(40\\,ih/16):color=black,"
        f"drawtext=fontfile='{font}':text='{text}':expansion=none:"
        "fontcolor=white:fontsize=max(10\\,min(h/34\\,w/44)):"
        "x=max(10\\,w/80):y=(max(40\\,h/17)-text_h)/2,"
        "format=rgb48be"
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "warning",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        filters,
        "-frames:v",
        "1",
        "-fps_mode",
        "passthrough",
        "-update",
        "1",
        "-c:v",
        "png",
        "-pix_fmt",
        "rgb48be",
        "-compression_level",
        "6",
        "-y",
        str(output_path),
    ]


def metric_command(
    reference_path: Path,
    encoded_path: Path,
    output_json: Path,
    *,
    include_vmaf: bool,
    crop: CropMargins | None = None,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    active_filter = f",{crop.ffmpeg_filter()}" if crop and crop.applied else ""
    filters = (
        f"[0:v]settb=AVTB,setpts=PTS-STARTPTS{active_filter}[ref];"
        f"[1:v]settb=AVTB,setpts=PTS-STARTPTS{active_filter}[enc];"
    )
    if include_vmaf:
        filters += f"[enc][ref]libvmaf=log_fmt=json:log_path={_escape_filter_path(output_json)}"
    else:
        stats = _escape_filter_path(output_json.with_suffix(".ssim.log"))
        filters += f"[enc][ref]ssim=stats_file={stats};[enc][ref]psnr"
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-i",
        str(reference_path),
        "-i",
        str(encoded_path),
        "-lavfi",
        filters,
        "-f",
        "null",
        "-",
    ]


def extract_y4m_at_timestamp_command(
    input_path: Path,
    pts_seconds: Decimal,
    output_path: Path,
    *,
    crop: CropMargins | None = None,
    decode_start_seconds: Decimal | None = None,
    decode_preroll_seconds: Decimal = DEFAULT_KEYFRAME_PREROLL_SECONDS,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Decode one native-YUV evidence frame with open-GOP-safe preroll."""

    if not pts_seconds.is_finite():
        raise ValueError("Y4M seek timestamp must be finite")
    if not decode_preroll_seconds.is_finite() or decode_preroll_seconds < 0:
        raise ValueError("decode preroll must be finite and non-negative")
    if decode_start_seconds is not None:
        if not decode_start_seconds.is_finite():
            raise ValueError("Y4M decode start timestamp must be finite")
        if decode_start_seconds > pts_seconds:
            raise ValueError("Y4M decode start cannot follow the target timestamp")
        seek_start = decode_start_seconds
    elif pts_seconds > 0:
        seek_start = max(Decimal(0), pts_seconds - decode_preroll_seconds)
    else:
        seek_start = None
    discard_seconds = (
        Decimal(0) if seek_start is None else max(Decimal(0), pts_seconds - seek_start)
    )
    command = [ffmpeg, "-hide_banner", "-nostdin", "-v", "warning"]
    if seek_start is not None:
        command.extend(
            [
                "-seek_timestamp",
                "1",
                "-ss",
                _format_decimal(seek_start),
                "-accurate_seek",
            ]
        )
    command.extend(["-i", str(input_path)])
    if discard_seconds:
        command.extend(["-ss", _format_decimal(discard_seconds)])
    command.extend(["-map", "0:v:0", "-an", "-sn", "-dn"])
    if crop and crop.applied:
        crop.validate_subsampled()
        command.extend(["-vf", crop.ffmpeg_filter()])
    command.extend(
        [
            "-frames:v",
            "1",
            "-f",
            "yuv4mpegpipe",
            "-y",
            str(output_path),
        ]
    )
    return command


def native_yuv_metric_command(
    reference_y4m: Path,
    encoded_y4m: Path,
    ssim_output: Path,
    psnr_output: Path,
    *,
    crop: CropMargins | None = None,
    pixel_format: str | None = None,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Measure SSIM/PSNR on native Y/U/V planes and write per-plane stats.

    No RGB transform is inserted. ``pixel_format`` is optional and should only
    be supplied when both inputs must be normalized to the known source format.
    """

    if pixel_format is not None and not re.fullmatch(r"[a-z0-9_]+", pixel_format):
        raise ValueError("invalid FFmpeg pixel format")
    if crop and crop.applied:
        crop.validate_subsampled()
    active_filter = f",{crop.ffmpeg_filter()}" if crop and crop.applied else ""
    format_filter = f",format={pixel_format}" if pixel_format else ""
    ssim_stats = _escape_filter_path(ssim_output)
    psnr_stats = _escape_filter_path(psnr_output)
    filters = (
        f"[0:v]settb=AVTB,setpts=PTS-STARTPTS{active_filter}{format_filter},"
        "split=2[ref_ssim][ref_psnr];"
        f"[1:v]settb=AVTB,setpts=PTS-STARTPTS{active_filter}{format_filter},"
        "split=2[enc_ssim][enc_psnr];"
        f"[enc_ssim][ref_ssim]ssim=stats_file={ssim_stats}[ssimout];"
        f"[enc_psnr][ref_psnr]psnr=stats_file={psnr_stats}[psnrout]"
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-i",
        str(reference_y4m),
        "-i",
        str(encoded_y4m),
        "-filter_complex",
        filters,
        "-map",
        "[ssimout]",
        "-map",
        "[psnrout]",
        "-f",
        "null",
        "-",
    ]


def parse_ffmpeg_metric_stats(text: str) -> dict[str, float | str]:
    """Parse the final FFmpeg SSIM/PSNR stats row, including Y/U/V fields."""

    parsed: dict[str, float | str] = {}
    for line in text.splitlines():
        values: dict[str, float | str] = {}
        for name, raw in re.findall(r"(?:^|\s)([A-Za-z][A-Za-z0-9_]*):([^\s]+)", line):
            normalized = raw.strip("()")
            try:
                numeric = float(normalized)
            except ValueError:
                values[name] = normalized
            else:
                values[name] = numeric if numeric == numeric else normalized
        if values:
            parsed = values
    if not parsed:
        raise ValueError("FFmpeg metric stats contain no key/value row")
    return parsed


_CROPDETECT_PATTERN = re.compile(
    r"(?:^|\s)crop=(?P<width>\d+):(?P<height>\d+):(?P<x>\d+):(?P<y>\d+)"
)


def parse_cropdetect(
    text: str,
    *,
    source_width: int,
    source_height: int,
    minimum_observations: int = 3,
) -> CropMargins:
    """Return the modal active-picture crop from a distributed cropdetect log."""

    if source_width < 1 or source_height < 1:
        raise ValueError("source dimensions must be positive")
    if minimum_observations < 1:
        raise ValueError("minimum crop observations must be positive")
    counts: dict[tuple[int, int, int, int], int] = {}
    for match in _CROPDETECT_PATTERN.finditer(text):
        value = tuple(int(match.group(name)) for name in ("width", "height", "x", "y"))
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        raise ValueError("cropdetect log contains no crop observations")
    (width, height, x, y), observations = max(
        counts.items(), key=lambda item: (item[1], item[0][0] * item[0][1])
    )
    if observations < minimum_observations:
        raise ValueError("cropdetect result is not supported by enough observations")
    right = source_width - width - x
    bottom = source_height - height - y
    if min(right, bottom) < 0:
        raise ValueError("cropdetect result exceeds the source canvas")
    result = CropMargins(left=x, top=y, right=right, bottom=bottom)
    result.validate_subsampled()
    return result


def standalone_vmaf_command(
    reference_y4m: Path,
    encoded_y4m: Path,
    output_json: Path,
    *,
    threads: int = 0,
    vmaf: str = "vmaf",
    model: str = "vmaf_v0.6.1",
) -> list[str]:
    """Build the official libvmaf CLI command for files or named pipes."""
    if threads < 0:
        raise ValueError("VMAF thread count cannot be negative")
    return [
        vmaf,
        "--reference",
        str(reference_y4m),
        "--distorted",
        str(encoded_y4m),
        "--model",
        f"version={model}",
        "--feature",
        "psnr",
        "--feature",
        "float_ssim",
        "--feature",
        "float_ms_ssim",
        "--threads",
        str(threads),
        "--output",
        str(output_json),
        "--json",
    ]


def _escape_filter_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def comparison_manifest(pairs: Iterable[FramePair]) -> dict[str, Any]:
    values = [pair.to_dict() for pair in pairs]
    counts = {
        name: sum(item["category"] == name for item in values) for name in FRAME_TYPES
    }
    return {
        "schema_version": 1,
        "categorization": "final_encode_picture_type",
        "alignment": "same_presentation_index_and_pts",
        "pairs": values,
        "counts": counts,
    }
