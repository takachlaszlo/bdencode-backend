"""Timeline-safe video frame selection and comparison command plans."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


FRAME_TYPES = ("I", "P", "B")


class FrameSelectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FrameRecord:
    presentation_index: int
    pts_seconds: Decimal
    pict_type: str | None
    key_frame: bool = False
    coded_picture_number: int | None = None

    def __post_init__(self) -> None:
        if self.presentation_index < 0:
            raise ValueError("presentation_index cannot be negative")
        if self.pict_type is not None and self.pict_type not in FRAME_TYPES:
            raise ValueError(f"unsupported frame type: {self.pict_type}")


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


def parse_vspipe_info(text: str) -> VapourSynthInfo:
    """Parse the stable ``vspipe --info`` frame-count/FPS fields."""

    frames = re.search(r"(?im)^Frames:\s*(\d+)\s*$", text)
    fps = re.search(r"(?im)^FPS:\s*(\d+)\s*/\s*(\d+)(?:\s|$)", text)
    if not frames or not fps:
        raise FrameSelectionError("vspipe --info did not report frame count and FPS")
    return VapourSynthInfo(int(frames.group(1)), int(fps.group(1)), int(fps.group(2)))


def vspipe_info_command(script: Path, *, vspipe: str = "vspipe") -> list[str]:
    return [vspipe, "--info", str(script), "-"]


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


def select_frame_pairs(
    encoded: Sequence[FrameRecord],
    reference: Sequence[FrameRecord],
    *,
    per_type: int = 4,
    pts_tolerance: Decimal = Decimal("0.001"),
    dual_type_match: bool = False,
) -> list[FramePair]:
    """Select identical presentation frames, categorized by final encode type.

    The reference frame is found by presentation index first. The PTS check prevents
    accidentally comparing shifted content. ``dual_type_match`` only filters already
    aligned pairs; it never substitutes a different frame to force a type match.
    """
    if per_type < 1:
        raise ValueError("per_type must be positive")
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

    pairs: list[FramePair] = []
    for category in FRAME_TYPES:
        try:
            selected = _evenly_spaced(candidates[category], per_type)
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
        "-vsync",
        "0",
        "-frames:v",
        "1",
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
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    filters = "[0:v]settb=AVTB,setpts=PTS-STARTPTS[ref];[1:v]settb=AVTB,setpts=PTS-STARTPTS[enc];"
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
