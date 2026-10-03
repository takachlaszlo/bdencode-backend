"""Deterministic VapourSynth reference scripts for headless operation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class SourceFilter(StrEnum):
    # L-SMASH indexes the container's packets (about 90 s for a 60 GB UHD
    # reference); BestSource decodes every frame for its index (about 45 min).
    # Both deliver bit-identical pictures for the same frame numbers.
    LSMAS = "lsmas"
    BESTSOURCE = "bestsource"


class TemporalFilter(StrEnum):
    PROGRESSIVE = "progressive"
    IVTC_TFF = "ivtc_tff"
    IVTC_BFF = "ivtc_bff"
    BWDIF_TFF = "bwdif_tff"
    BWDIF_BFF = "bwdif_bff"
    HYBRID_SAFE_BOB_TFF = "hybrid_safe_bob_tff"
    HYBRID_SAFE_BOB_BFF = "hybrid_safe_bob_bff"


@dataclass(frozen=True, slots=True)
class Crop:
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    def __post_init__(self) -> None:
        values = tuple(asdict(self).values())
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise ValueError("4:2:0 crop values must be integers")
        if any(value < 0 or value % 2 for value in values):
            raise ValueError("4:2:0 crop values must be non-negative even integers")

    @classmethod
    def from_detected_borders(
        cls,
        *,
        left: int = 0,
        top: int = 0,
        right: int = 0,
        bottom: int = 0,
        safety: int = 0,
    ) -> Crop:
        """Round measured borders inward to a safe 4:2:0 crop."""

        values = (left, top, right, bottom, safety)
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise ValueError("detected borders and safety must be integers")
        if any(value < 0 for value in values):
            raise ValueError("detected borders and safety cannot be negative")

        def safe(value: int) -> int:
            return (max(0, value - safety) // 2) * 2

        return cls(safe(left), safe(top), safe(right), safe(bottom))

    @property
    def enabled(self) -> bool:
        return any(asdict(self).values())

    def output_dimensions(self, width: int, height: int) -> tuple[int, int]:
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            raise ValueError("source dimensions must be positive integers")
        if width % 2 or height % 2:
            raise ValueError("4:2:0 source dimensions must be even")
        output_width = width - self.left - self.right
        output_height = height - self.top - self.bottom
        if output_width < 16 or output_height < 16:
            raise ValueError("crop must leave even dimensions of at least 16 pixels")
        if output_width % 2 or output_height % 2:
            raise ValueError("4:2:0 cropped dimensions must be even")
        return output_width, output_height


@dataclass(frozen=True, slots=True)
class ReferenceScriptPlan:
    source: Path
    cache_path: Path
    script_path: Path
    track: int = 0
    temporal_filter: TemporalFilter = TemporalFilter.PROGRESSIVE
    crop: Crop = Crop()
    output_width: int | None = None
    output_height: int | None = None
    decoder_threads: int = 0
    # Optional (start_frame, frame_count) windows on the finished timeline.  A
    # sample script splices them after every other filter, so a CRF probe sees
    # exactly the picture the release encode will receive.
    sample_windows: tuple[tuple[int, int], ...] = ()
    source_filter: SourceFilter = SourceFilter.LSMAS

    def __post_init__(self) -> None:
        if self.track < 0:
            raise ValueError("video track cannot be negative")
        previous_end = 0
        for window in self.sample_windows:
            if (
                len(window) != 2
                or any(type(value) is not int for value in window)
                or window[0] < previous_end
                or window[1] < 1
            ):
                raise ValueError(
                    "sample windows must be ordered, non-overlapping "
                    "(start, count) integer pairs"
                )
            previous_end = window[0] + window[1]
        if (self.output_width is None) != (self.output_height is None):
            raise ValueError("output width and height must be supplied together")
        if self.output_width is not None and (
            self.output_width < 16
            or self.output_height is None
            or self.output_height < 16
            or self.output_width % 2
            or self.output_height % 2
        ):
            raise ValueError("output dimensions must be even and at least 16")


def _source_lines(plan: ReferenceScriptPlan) -> list[str]:
    source = json.dumps(str(plan.source.resolve(strict=False)))
    cache = str(plan.cache_path.resolve(strict=False))
    if plan.source_filter is SourceFilter.LSMAS:
        return [
            "src = core.lsmas.LWLibavSource(",
            f"    source={source}, stream_index={plan.track}, threads={plan.decoder_threads},",
            f"    cachefile={json.dumps(cache + '.lwi')},",
            ")",
            # Downstream Y4M consumers and encoders require one stable format.
            # A Blu-ray title is constant-format; fail rather than silently
            # accepting a malformed source whose format changes mid-playlist.
            "if src.format is None:",
            '    raise ValueError("the source changes its format mid-stream")',
        ]
    return [
        "src = core.bs.VideoSource(",
        f"    source={source}, track={plan.track}, threads={plan.decoder_threads},",
        f"    cachemode=4, cachepath={json.dumps(cache)}, maxdecoders=1, showprogress=True,",
        # Downstream Y4M consumers and encoders require one stable format.  A
        # Blu-ray title is constant-format; fail rather than silently accepting
        # a malformed source whose format changes mid-playlist.
        # The encode is CFR and its full output timeline is checked against
        # vspipe --info. BestSource's exporttimestamps=True changes the return
        # value into a multi-key VSMap and materializes an otherwise unused
        # array for every frame.
        "    variableformat=0,",
        ")",
    ]


def render_reference_script(plan: ReferenceScriptPlan) -> str:
    lines = [
        "# Generated by BDEncode. Edits make the recorded hash invalid.",
        "from vapoursynth import core",
        "",
        *_source_lines(plan),
    ]
    temporal = plan.temporal_filter
    if temporal is TemporalFilter.IVTC_TFF:
        lines.extend(
            ["src = core.vivtc.VFM(src, order=1)", "src = core.vivtc.VDecimate(src)"]
        )
    elif temporal is TemporalFilter.IVTC_BFF:
        lines.extend(
            ["src = core.vivtc.VFM(src, order=0)", "src = core.vivtc.VDecimate(src)"]
        )
    elif temporal is TemporalFilter.BWDIF_TFF:
        lines.append("src = core.bwdif.Bwdif(src, field=1)")
    elif temporal is TemporalFilter.BWDIF_BFF:
        lines.append("src = core.bwdif.Bwdif(src, field=0)")
    elif temporal is TemporalFilter.HYBRID_SAFE_BOB_TFF:
        lines.append("src = core.bwdif.Bwdif(src, field=3)")
    elif temporal is TemporalFilter.HYBRID_SAFE_BOB_BFF:
        lines.append("src = core.bwdif.Bwdif(src, field=2)")

    lines.extend(
        [
            "if src.width % 2 or src.height % 2:",
            '    raise ValueError("4:2:0 source dimensions must be even")',
        ]
    )
    if plan.crop.enabled:
        horizontal = plan.crop.left + plan.crop.right
        vertical = plan.crop.top + plan.crop.bottom
        lines.extend(
            [
                f"if src.width - {horizontal} < 16 or src.height - {vertical} < 16:",
                '    raise ValueError("crop must leave at least 16 pixels per dimension")',
            ]
        )
        lines.append(
            "src = core.std.CropRel(src, "
            f"left={plan.crop.left}, top={plan.crop.top}, "
            f"right={plan.crop.right}, bottom={plan.crop.bottom})"
        )
    if plan.output_width is not None and plan.output_height is not None:
        lines.append(
            f"src = core.resize.Spline36(src, width={plan.output_width}, height={plan.output_height})"
        )
    if plan.sample_windows:
        pieces = ", ".join(
            f"src[{start}:{start + count}]" for start, count in plan.sample_windows
        )
        lines.append(
            f"src = {pieces}"
            if len(plan.sample_windows) == 1
            else f"src = core.std.Splice([{pieces}], mismatch=False)"
        )
    lines.extend(["", "src.set_output()", ""])
    return "\n".join(lines)


def script_record(plan: ReferenceScriptPlan, content: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": 1,
        "source": str(plan.source),
        "cache_path": str(plan.cache_path),
        "track": plan.track,
        "temporal_filter": plan.temporal_filter.value,
        "crop": asdict(plan.crop),
        "output_width": plan.output_width,
        "output_height": plan.output_height,
        "decoder_threads": plan.decoder_threads,
        "script_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    if plan.source_filter is not SourceFilter.BESTSOURCE:
        # BestSource records stay byte-identical to those of earlier releases.
        record["source_filter"] = plan.source_filter.value
    if plan.sample_windows:
        # Present only for sample scripts, so the digest of every ordinary
        # reference-script checkpoint stays byte-identical across upgrades.
        record["sample_windows"] = [list(window) for window in plan.sample_windows]
    return record
