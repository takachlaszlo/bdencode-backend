"""Distributed, fail-closed active-picture crop detection policy.

The functions in this module deliberately do not depend on the worker.  A
caller runs the bounded FFmpeg commands, concatenates their stderr, parses the
result into stable evidence and validates the operator-selected crop.  Policy
failures use :class:`CropPolicyError`, which the worker can translate to its
``ReviewRequired`` state without coupling this module to orchestration code.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Literal

from .video import CropMargins, parse_cropdetect


DEFAULT_CROP_SAMPLES = 12
DEFAULT_CROP_SAMPLE_SECONDS = Decimal("2")
DEFAULT_MINIMUM_OBSERVATIONS = 24
DEFAULT_MINIMUM_SUPPORT_RATIO = Decimal("0.70")
DEFAULT_BORDER_JITTER_PIXELS = 2
DEFAULT_SUBSTANTIAL_BORDER_PIXELS = 8
DEFAULT_SAFETY_MARGIN_PIXELS = 2
DEFAULT_DECODE_PREROLL_SECONDS = Decimal("12")
DEFAULT_FULL_TITLE_SAMPLE_FPS = Decimal("1")


_CROP_OBSERVATION_PATTERN = re.compile(
    r"(?:^|\s)crop=(?P<width>\d+):(?P<height>\d+):(?P<x>\d+):(?P<y>\d+)"
)
_TIMED_CROP_PATTERN = re.compile(
    r"(?:\bt:(?P<time>-?\d+(?:\.\d+)?)\b.*?)?"
    r"(?:^|\s)crop=(?P<width>\d+):(?P<height>\d+):(?P<x>\d+):(?P<y>\d+)"
)
DEFAULT_ASPECT_WARMUP_FRACTION = 0.05
DEFAULT_ASPECT_WARMUP_MAX_SECONDS = 600.0
DEFAULT_ASPECT_MIN_CHANGE_PIXELS = 8
MAX_REPORTED_EXPANSIONS = 20


class CropPolicyError(ValueError):
    """A crop decision needs explicit operator review.

    ``code`` is stable and suitable for manifests or API error handling; the
    human-readable message is intentionally specific enough for the UI.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CropDetectInterval:
    """One bounded crop-detection window on the title timeline."""

    start_seconds: Decimal
    duration_seconds: Decimal

    def __post_init__(self) -> None:
        if not self.start_seconds.is_finite() or self.start_seconds < 0:
            raise ValueError("crop sample start must be finite and non-negative")
        if not self.duration_seconds.is_finite() or self.duration_seconds <= 0:
            raise ValueError("crop sample duration must be finite and positive")

    @property
    def end_seconds(self) -> Decimal:
        return self.start_seconds + self.duration_seconds


@dataclass(frozen=True, slots=True)
class CropDetectionEvidence:
    """Stable modal active-picture borders derived from cropdetect logs."""

    crop: CropMargins
    source_width: int
    source_height: int
    observations: int
    supporting_observations: int
    support_ratio: Decimal
    jitter_pixels: int
    # Largest active canvas seen across the sampled windows.  This can be more
    # conservative than the modal recommendation for variable-aspect titles.
    safe_crop: CropMargins | None = None
    # True when the widest envelope was accepted because the title changes
    # aspect ratio, instead of because one modal border dominated the log.
    variable_aspect: bool = False

    def __post_init__(self) -> None:
        if self.source_width < 1 or self.source_height < 1:
            raise ValueError("source dimensions must be positive")
        if self.observations < 1:
            raise ValueError("crop evidence needs observations")
        if not 0 <= self.supporting_observations <= self.observations:
            raise ValueError("invalid crop support count")
        if not self.support_ratio.is_finite() or not 0 <= self.support_ratio <= 1:
            raise ValueError("crop support ratio must be between zero and one")
        if self.jitter_pixels < 0:
            raise ValueError("crop jitter cannot be negative")
        if self.safe_crop is None:
            object.__setattr__(self, "safe_crop", self.crop)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "crop": self.crop.to_dict(),
            "source_width": self.source_width,
            "source_height": self.source_height,
            "observations": self.observations,
            "supporting_observations": self.supporting_observations,
            "support_ratio": str(self.support_ratio),
            "jitter_pixels": self.jitter_pixels,
            "safe_crop": self.safe_crop.to_dict() if self.safe_crop else None,
        }
        if self.variable_aspect:
            result["variable_aspect"] = True
        return result


@dataclass(frozen=True, slots=True)
class CropPolicyDecision:
    """Accepted crop decision plus the exact residual-border calculation."""

    status: Literal["accepted"]
    detected: CropMargins
    release_safe: CropMargins
    requested: CropMargins
    residual: CropMargins
    substantial_border_pixels: int
    safety_margin_pixels: int

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["detected"] = self.detected.to_dict()
        result["release_safe"] = self.release_safe.to_dict()
        result["requested"] = self.requested.to_dict()
        result["residual"] = self.residual.to_dict()
        return result


def _decimal(value: Decimal | int | float | str, *, name: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def plan_cropdetect_intervals(
    duration_seconds: Decimal | int | float | str,
    *,
    samples: int = DEFAULT_CROP_SAMPLES,
    sample_seconds: Decimal | int | float | str = DEFAULT_CROP_SAMPLE_SECONDS,
) -> tuple[CropDetectInterval, ...]:
    """Spread non-overlapping bounded samples uniformly across a title.

    Samples are centred in equal timeline segments, which avoids bias toward
    an opening logo or end credits.  Short clips automatically use fewer
    windows rather than repeatedly decoding the same frames.
    """

    duration = _decimal(duration_seconds, name="title duration")
    requested_window = _decimal(sample_seconds, name="crop sample duration")
    if duration <= 0:
        raise ValueError("title duration must be positive")
    if requested_window <= 0:
        raise ValueError("crop sample duration must be positive")
    if samples < 1:
        raise ValueError("crop sample count must be positive")

    window = min(duration, requested_window)
    possible_windows = max(1, int(duration // window))
    count = min(samples, possible_windows)
    segment = duration / Decimal(count)
    intervals: list[CropDetectInterval] = []
    for index in range(count):
        center = segment * (Decimal(index) + Decimal("0.5"))
        start = max(Decimal(0), min(center - window / 2, duration - window))
        intervals.append(CropDetectInterval(start, window))
    return tuple(intervals)


def cropdetect_commands(
    input_path: Path,
    intervals: Iterable[CropDetectInterval],
    *,
    ffmpeg: str = "ffmpeg",
    limit: Decimal | int | float | str = Decimal("0.094"),
    round_to: int = 2,
    decode_preroll_seconds: Decimal | int | float | str = (
        DEFAULT_DECODE_PREROLL_SECONDS
    ),
) -> tuple[list[str], ...]:
    """Build independently bounded FFmpeg cropdetect commands.

    Input-side seeks keep total decoded work proportional to the sample plan;
    no command has to decode the gaps between title regions.
    """

    parsed_limit = _decimal(limit, name="cropdetect limit")
    if not 0 <= parsed_limit <= 1:
        raise ValueError("cropdetect limit must be between zero and one")
    if round_to < 1:
        raise ValueError("cropdetect round value must be positive")
    preroll = _decimal(decode_preroll_seconds, name="cropdetect decode preroll")
    if preroll < 0:
        raise ValueError("cropdetect decode preroll must be non-negative")
    planned = tuple(intervals)
    if not planned:
        raise ValueError("at least one cropdetect interval is required")

    crop_filter = (
        f"cropdetect=limit={_format_decimal(parsed_limit)}:"
        f"round={round_to}:reset=0"
    )
    commands: list[list[str]] = []
    for interval in planned:
        decode_start = max(Decimal(0), interval.start_seconds - preroll)
        discard = interval.start_seconds - decode_start
        command = [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-v",
            "info",
            "-ss",
            _format_decimal(decode_start),
            "-i",
            str(input_path),
        ]
        if discard:
            command.extend(("-ss", _format_decimal(discard)))
        command.extend(
            [
                "-t", _format_decimal(interval.duration_seconds),
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                crop_filter,
                "-f",
                "null",
                "-",
            ]
        )
        commands.append(command)
    return tuple(commands)


def distributed_cropdetect_commands(
    input_path: Path,
    duration_seconds: Decimal | int | float | str,
    *,
    samples: int = DEFAULT_CROP_SAMPLES,
    sample_seconds: Decimal | int | float | str = DEFAULT_CROP_SAMPLE_SECONDS,
    ffmpeg: str = "ffmpeg",
    limit: Decimal | int | float | str = Decimal("0.094"),
    round_to: int = 2,
    decode_preroll_seconds: Decimal | int | float | str = (
        DEFAULT_DECODE_PREROLL_SECONDS
    ),
) -> tuple[list[str], ...]:
    """Convenience API combining the distributed plan and command builders."""

    intervals = plan_cropdetect_intervals(
        duration_seconds, samples=samples, sample_seconds=sample_seconds
    )
    return cropdetect_commands(
        input_path,
        intervals,
        ffmpeg=ffmpeg,
        limit=limit,
        round_to=round_to,
        decode_preroll_seconds=decode_preroll_seconds,
    )


def full_title_cropdetect_command(
    input_path: Path,
    *,
    ffmpeg: str = "ffmpeg",
    sample_fps: Decimal | int | float | str = DEFAULT_FULL_TITLE_SAMPLE_FPS,
    limit: Decimal | int | float | str = Decimal("0.094"),
    round_to: int = 2,
    hwaccel: str | None = None,
) -> list[str]:
    """Build a sequential full-title crop scan with sparse observations.

    ``cropdetect`` deliberately runs before the output-rate limiter, so every
    decoded frame can widen the conservative ``reset=0`` envelope.  The
    trailing ``fps`` filter only limits frames sent to the null muxer; even a
    sub-second variable-aspect/full-frame insert remains visible to the gate.

    ``hwaccel="cuda"`` decodes on the GPU (NVDEC); the frames are copied back
    to system memory, so ``cropdetect`` sees the same pictures as a CPU decode.
    """

    parsed_fps = _decimal(sample_fps, name="full-title crop sample FPS")
    parsed_limit = _decimal(limit, name="cropdetect limit")
    if parsed_fps <= 0:
        raise ValueError("full-title crop sample FPS must be positive")
    if not 0 <= parsed_limit <= 1:
        raise ValueError("cropdetect limit must be between zero and one")
    if round_to < 1:
        raise ValueError("cropdetect round value must be positive")
    if hwaccel not in (None, "cuda"):
        raise ValueError(f"unsupported crop decode acceleration: {hwaccel}")
    filters = (
        f"cropdetect=limit={_format_decimal(parsed_limit)}:"
        f"round={round_to}:reset=0,"
        f"fps={_format_decimal(parsed_fps)}"
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
        *(("-hwaccel", hwaccel) if hwaccel else ()),
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        filters,
        "-f",
        "null",
        "-",
    ]


def _observed_margins(
    text: str, *, source_width: int, source_height: int
) -> tuple[CropMargins, ...]:
    values: list[CropMargins] = []
    for match in _CROP_OBSERVATION_PATTERN.finditer(text):
        width, height, x, y = (
            int(match.group(name)) for name in ("width", "height", "x", "y")
        )
        right = source_width - width - x
        bottom = source_height - height - y
        if min(x, y, right, bottom) < 0:
            raise CropPolicyError(
                "invalid_detection",
                "cropdetect reported an active picture outside the source canvas",
            )
        try:
            crop = CropMargins(left=x, top=y, right=right, bottom=bottom)
            crop.validate_subsampled()
        except ValueError as exc:
            raise CropPolicyError(
                "unaligned_detection",
                f"cropdetect reported chroma-unaligned borders: {exc}",
            ) from exc
        values.append(crop)
    return tuple(values)


def parse_stable_cropdetect(
    logs: str | Iterable[str],
    *,
    source_width: int,
    source_height: int,
    minimum_observations: int = DEFAULT_MINIMUM_OBSERVATIONS,
    minimum_support_ratio: Decimal | int | float | str = (
        DEFAULT_MINIMUM_SUPPORT_RATIO
    ),
    jitter_pixels: int = DEFAULT_BORDER_JITTER_PIXELS,
) -> CropDetectionEvidence:
    """Parse distributed logs and require strong support for the modal crop."""

    if source_width < 1 or source_height < 1:
        raise ValueError("source dimensions must be positive")
    if minimum_observations < 1:
        raise ValueError("minimum crop observations must be positive")
    ratio_required = _decimal(
        minimum_support_ratio, name="minimum crop support ratio"
    )
    if not 0 < ratio_required <= 1:
        raise ValueError("minimum crop support ratio must be above zero and at most one")
    if jitter_pixels < 0:
        raise ValueError("crop border jitter cannot be negative")

    window_logs = (logs,) if isinstance(logs, str) else tuple(logs)
    text = "\n".join(window_logs)
    window_observations = tuple(
        _observed_margins(
            window_log, source_width=source_width, source_height=source_height
        )
        for window_log in window_logs
    )
    if len(window_logs) > 1 and any(not values for values in window_observations):
        raise CropPolicyError(
            "incomplete_windows",
            "at least one distributed cropdetect window produced no observation",
        )
    observations = tuple(
        value for values in window_observations for value in values
    )
    if len(observations) < minimum_observations:
        raise CropPolicyError(
            "insufficient_detection",
            "cropdetect produced too few observations for a release-safe decision "
            f"({len(observations)}/{minimum_observations})",
        )
    try:
        modal = parse_cropdetect(
            text,
            source_width=source_width,
            source_height=source_height,
            minimum_observations=1,
        )
    except ValueError as exc:
        raise CropPolicyError("invalid_detection", str(exc)) from exc

    def supports(value: CropMargins) -> bool:
        return all(
            abs(getattr(value, edge) - getattr(modal, edge)) <= jitter_pixels
            for edge in ("left", "top", "right", "bottom")
        )

    supporting = sum(supports(value) for value in observations)
    support_ratio = Decimal(supporting) / Decimal(len(observations))
    if support_ratio < ratio_required:
        raise CropPolicyError(
            "unstable_detection",
            "cropdetect modal borders are not stable enough for automatic approval "
            f"({supporting}/{len(observations)}, required {ratio_required})",
        )
    # ``reset=0`` makes the last observation of each independently decoded
    # window its conservative largest-active-picture result.  If callers only
    # provide one concatenated string, use every observation rather than risk
    # overlooking a variable-aspect segment.
    safe_observations = (
        observations
        if len(window_logs) == 1
        else tuple(values[-1] for values in window_observations)
    )
    safe_crop = CropMargins(
        **{
            edge: min(getattr(value, edge) for value in safe_observations)
            for edge in ("left", "top", "right", "bottom")
        }
    )
    return CropDetectionEvidence(
        crop=modal,
        source_width=source_width,
        source_height=source_height,
        observations=len(observations),
        supporting_observations=supporting,
        support_ratio=support_ratio,
        jitter_pixels=jitter_pixels,
        safe_crop=safe_crop,
    )


@dataclass(frozen=True, slots=True)
class AspectExpansion:
    """One moment at which the running active-picture envelope grew."""

    time_seconds: float
    width: int
    height: int

    @property
    def aspect_ratio(self) -> float:
        return round(self.width / self.height, 4)

    def to_dict(self) -> dict[str, object]:
        return {
            "time_seconds": round(self.time_seconds, 3),
            "width": self.width,
            "height": self.height,
            "aspect_ratio": self.aspect_ratio,
        }


@dataclass(frozen=True, slots=True)
class AspectProfile:
    """Evidence that the picture changes aspect ratio during the title.

    ``cropdetect reset=0`` reports a running maximum, so the envelope can only
    grow.  A material growth *after* the opening minutes therefore marks the
    first frame of a taller or wider scene (typically IMAX or full-frame
    inserts in a scope film).  The crop that protects such a title is the
    widest envelope, which :func:`automatic_crop` already selects; this profile
    records why, and when the picture first changes.
    """

    variable: bool
    warmup_seconds: float
    baseline_width: int
    baseline_height: int
    widest_width: int
    widest_height: int
    expansions: tuple[AspectExpansion, ...]
    timing: str

    def to_dict(self) -> dict[str, object]:
        return {
            "variable": self.variable,
            "warmup_seconds": round(self.warmup_seconds, 3),
            "baseline": {
                "width": self.baseline_width,
                "height": self.baseline_height,
                "aspect_ratio": round(self.baseline_width / self.baseline_height, 4),
            },
            "widest": {
                "width": self.widest_width,
                "height": self.widest_height,
                "aspect_ratio": round(self.widest_width / self.widest_height, 4),
            },
            "expansions": [item.to_dict() for item in self.expansions],
            "timing": self.timing,
        }

    def summary(self) -> str:
        if not self.variable:
            return "constant aspect ratio"
        first = self.expansions[0]
        return (
            f"variable aspect ratio: {self.baseline_width}x{self.baseline_height} "
            f"({self.baseline_width / self.baseline_height:.2f}:1) grows to "
            f"{self.widest_width}x{self.widest_height} "
            f"({self.widest_width / self.widest_height:.2f}:1), first at "
            f"{first.time_seconds:.0f}s; the widest canvas is kept"
        )


def parse_aspect_profile(
    log: str,
    *,
    source_width: int,
    source_height: int,
    duration_seconds: float,
    warmup_fraction: float = DEFAULT_ASPECT_WARMUP_FRACTION,
    warmup_max_seconds: float = DEFAULT_ASPECT_WARMUP_MAX_SECONDS,
    min_change_pixels: int = DEFAULT_ASPECT_MIN_CHANGE_PIXELS,
) -> AspectProfile | None:
    """Derive an :class:`AspectProfile` from a full-title cropdetect log.

    Returns ``None`` when the log carries no crop observations.  Timestamps come
    from FFmpeg's ``t:`` field; a log without them falls back to the relative
    position of each observation, which is reported as ``timing=approximate``.
    """

    if source_width < 1 or source_height < 1:
        raise ValueError("source dimensions must be positive")
    if duration_seconds <= 0:
        raise ValueError("title duration must be positive")
    if not 0 <= warmup_fraction < 1 or warmup_max_seconds < 0:
        raise ValueError("warm-up policy is invalid")
    if min_change_pixels < 1:
        raise ValueError("minimum aspect change must be positive")

    observations: list[tuple[float | None, int, int]] = []
    for line in log.splitlines():
        match = _TIMED_CROP_PATTERN.search(line)
        if match is None:
            continue
        width, height = int(match["width"]), int(match["height"])
        if width < 1 or height < 1 or width > source_width or height > source_height:
            continue
        observations.append(
            (None if match["time"] is None else float(match["time"]), width, height)
        )
    if not observations:
        return None

    timed = all(item[0] is not None for item in observations)
    total = len(observations)
    stamped = [
        (
            item[0] if timed else duration_seconds * index / max(total - 1, 1),
            item[1],
            item[2],
        )
        for index, item in enumerate(observations)
    ]
    warmup = min(duration_seconds * warmup_fraction, warmup_max_seconds)

    # Running envelope: the log is already cumulative, but a defensive maximum
    # keeps the profile correct for logs produced with reset=1 as well.
    events: list[AspectExpansion] = []
    envelope_w = envelope_h = 0
    baseline: tuple[int, int] | None = None
    for moment, width, height in stamped:
        if moment > warmup and baseline is None:
            baseline = (envelope_w, envelope_h)
        grown_w, grown_h = max(envelope_w, width), max(envelope_h, height)
        changed = (
            grown_w - envelope_w >= min_change_pixels
            or grown_h - envelope_h >= min_change_pixels
        )
        if changed and envelope_w and moment > warmup:
            events.append(AspectExpansion(moment, grown_w, grown_h))
        envelope_w, envelope_h = grown_w, grown_h
    if baseline is None or baseline[0] == 0:
        baseline = (envelope_w, envelope_h)

    return AspectProfile(
        variable=bool(events),
        warmup_seconds=warmup,
        baseline_width=baseline[0],
        baseline_height=baseline[1],
        widest_width=envelope_w,
        widest_height=envelope_h,
        expansions=tuple(events[:MAX_REPORTED_EXPANSIONS]),
        timing="exact" if timed else "approximate",
    )


def variable_aspect_evidence(
    log: str,
    *,
    source_width: int,
    source_height: int,
    duration_seconds: float,
    minimum_observations: int = DEFAULT_MINIMUM_OBSERVATIONS,
    maximum_expansions: int = 3,
    minimum_widest_observations: int = 240,
    jitter_pixels: int = DEFAULT_BORDER_JITTER_PIXELS,
) -> CropDetectionEvidence | None:
    """Explain an otherwise "unstable" log as a variable-aspect title.

    A title that switches between, say, 2.40:1 and full-frame IMAX scenes has no
    single dominant border, so :func:`parse_stable_cropdetect` refuses it.  Its
    running-envelope log is nevertheless a clean step function: a few material
    expansions, each to a larger canvas, that never shrinks.  For such a log the
    widest canvas is a safe crop by construction because it removes no picture;
    the cost of being wrong is some encoded black bars, never lost image.

    Returns ``None`` (leaving the original review requirement in place) when the
    log does not look like a small number of aspect changes, or when the widest
    canvas is supported by too few observations to be more than a flash.
    """

    profile = parse_aspect_profile(
        log,
        source_width=source_width,
        source_height=source_height,
        duration_seconds=duration_seconds,
    )
    if (
        profile is None
        or not profile.variable
        or len(profile.expansions) > maximum_expansions
    ):
        return None
    observations = _observed_margins(
        log, source_width=source_width, source_height=source_height
    )
    if len(observations) < minimum_observations:
        return None
    widest = CropMargins(
        **{
            edge: min(getattr(value, edge) for value in observations)
            for edge in ("left", "top", "right", "bottom")
        }
    )

    def at_widest(value: CropMargins) -> bool:
        return all(
            abs(getattr(value, edge) - getattr(widest, edge)) <= jitter_pixels
            for edge in ("left", "top", "right", "bottom")
        )

    supporting = sum(at_widest(value) for value in observations)
    if supporting < minimum_widest_observations:
        return None
    return CropDetectionEvidence(
        crop=widest,
        source_width=source_width,
        source_height=source_height,
        observations=len(observations),
        supporting_observations=supporting,
        support_ratio=Decimal(supporting) / Decimal(len(observations)),
        jitter_pixels=jitter_pixels,
        safe_crop=widest,
        variable_aspect=True,
    )


def validate_operator_crop(
    requested: CropMargins | None,
    evidence: CropDetectionEvidence,
    *,
    substantial_border_pixels: int = DEFAULT_SUBSTANTIAL_BORDER_PIXELS,
    safety_margin_pixels: int = DEFAULT_SAFETY_MARGIN_PIXELS,
) -> CropPolicyDecision:
    """Accept a safe crop or raise a coded review-required policy error.

    A conservative under-crop is allowed while the residual black border is
    smaller than ``substantial_border_pixels``.  Cropping beyond the detected
    active-picture edge is much riskier and is limited to the small explicit
    safety margin.
    """

    if substantial_border_pixels < 1:
        raise ValueError("substantial crop border must be positive")
    if safety_margin_pixels < 0:
        raise ValueError("crop safety margin cannot be negative")
    selected = requested or CropMargins()
    try:
        selected.validate_subsampled()
    except ValueError as exc:
        raise CropPolicyError("unaligned_request", str(exc)) from exc
    if (
        selected.left + selected.right >= evidence.source_width
        or selected.top + selected.bottom >= evidence.source_height
    ):
        raise CropPolicyError(
            "invalid_request", "requested crop removes the entire source picture"
        )

    release_safe = evidence.safe_crop or evidence.crop
    residual_values: dict[str, int] = {}
    for edge in ("left", "top", "right", "bottom"):
        detected = getattr(release_safe, edge)
        modal = getattr(evidence.crop, edge)
        operator = getattr(selected, edge)
        residual = max(0, detected - operator)
        residual_values[edge] = residual
        if operator - detected > safety_margin_pixels:
            variable_aspect = modal - detected > safety_margin_pixels
            raise CropPolicyError(
                "variable_aspect_ratio" if variable_aspect else "over_crop",
                f"requested {edge} crop ({operator}px) exceeds the release-safe "
                f"sampled border ({detected}px) by more than the "
                f"{safety_margin_pixels}px safety margin"
                + (
                    f"; the modal {modal}px border is not present in every window"
                    if variable_aspect
                    else ""
                ),
            )
        if detected >= substantial_border_pixels and residual >= substantial_border_pixels:
            raise CropPolicyError(
                "under_crop",
                f"requested {edge} crop leaves a stable {residual}px black border; "
                f"the review threshold is {substantial_border_pixels}px",
            )

    return CropPolicyDecision(
        status="accepted",
        detected=evidence.crop,
        release_safe=release_safe,
        requested=selected,
        residual=CropMargins(**residual_values),
        substantial_border_pixels=substantial_border_pixels,
        safety_margin_pixels=safety_margin_pixels,
    )


def automatic_crop(
    evidence: CropDetectionEvidence,
    *,
    substantial_border_pixels: int = DEFAULT_SUBSTANTIAL_BORDER_PIXELS,
) -> CropMargins:
    """Choose the conservative sampled crop without trimming thin edge noise.

    ``safe_crop`` is the largest active canvas observed across the sampled
    title, so it protects variable-aspect material.  Borders below the policy
    threshold remain untouched because removing a few dark edge pixels is
    riskier than preserving them.
    """

    if substantial_border_pixels < 1:
        raise ValueError("substantial crop border must be positive")
    release_safe = evidence.safe_crop or evidence.crop
    selected = CropMargins(
        **{
            edge: (
                getattr(release_safe, edge)
                if getattr(release_safe, edge) >= substantial_border_pixels
                else 0
            )
            for edge in ("left", "top", "right", "bottom")
        }
    )
    selected.validate_subsampled()
    return selected


__all__ = [
    "AspectExpansion",
    "AspectProfile",
    "CropDetectInterval",
    "CropDetectionEvidence",
    "CropPolicyDecision",
    "CropPolicyError",
    "automatic_crop",
    "cropdetect_commands",
    "distributed_cropdetect_commands",
    "full_title_cropdetect_command",
    "parse_aspect_profile",
    "parse_stable_cropdetect",
    "plan_cropdetect_intervals",
    "validate_operator_crop",
    "variable_aspect_evidence",
]
