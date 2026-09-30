"""Automatic CRF selection from short, VMAF-scored sample encodes.

The search is deliberately independent of the worker: it plans the sample
windows, decides which CRF to probe next, parses libvmaf output and reports a
JSON-safe outcome.  The worker owns process execution and checkpoints.

Design constraints:

* the sample clip is a deterministic splice of evenly distributed windows from
  the *same* reference graph that feeds the real encode, so every probe sees
  exactly the crop/IVTC/deinterlace result the release will use;
* the probes use the same encoder settings as the release (optionally a faster
  preset), so the measured score describes the real rate-control behaviour;
* the search never extrapolates blindly: it returns the highest probed CRF that
  met the target and reports honestly when the target is unreachable.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from typing import Any, Mapping, Sequence

CRF_STEP = 0.25
SCORE_METRICS = ("mean", "harmonic_mean", "percentile_1")
DEFAULT_TARGET_VMAF = 95.0
DEFAULT_SLOPE = -1.5  # VMAF points per CRF unit near the high-quality plateau
_SLOPE_LIMITS = (-4.0, -0.3)
_EPSILON = 1e-9

# The first and last part of a title are dominated by logos, black frames and
# credits; sampling them would bias the CRF towards content nobody watches.
EDGE_SKIP_FRACTION = 0.03

STATUS_CONVERGED = "converged"
STATUS_BEST_EFFORT = "best_effort"
STATUS_MAX_CRF = "max_crf_reached"
STATUS_UNREACHABLE = "target_unreachable"


class CrfSearchError(ValueError):
    """The search cannot be planned or its evidence cannot be trusted."""


def _real(value: object, name: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise CrfSearchError(f"{name} must be a finite number")
    return float(value)


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise CrfSearchError(f"{name} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class AutoCrfConfig:
    """Operator-visible parameters of the automatic CRF search."""

    enabled: bool = False
    target_vmaf: float = DEFAULT_TARGET_VMAF
    min_crf: float = 12.0
    max_crf: float = 26.0
    samples: int = 12
    sample_seconds: float = 3.0
    max_iterations: int = 6
    tolerance: float = 0.5
    metric: str = "mean"
    probe_preset: str | None = None

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise CrfSearchError("auto_crf.enabled must be a boolean")
        for name in ("target_vmaf", "min_crf", "max_crf", "sample_seconds", "tolerance"):
            _real(getattr(self, name), f"auto_crf.{name}")
        for name in ("samples", "max_iterations"):
            _integer(getattr(self, name), f"auto_crf.{name}")
        if not 80.0 <= self.target_vmaf <= 99.5:
            raise CrfSearchError("auto_crf.target_vmaf must be between 80 and 99.5")
        if not 0.1 <= self.min_crf < self.max_crf <= 51:
            raise CrfSearchError(
                "auto_crf CRF bounds must satisfy 0.1 <= min_crf < max_crf <= 51"
            )
        if self.max_crf - self.min_crf < 2 * CRF_STEP:
            raise CrfSearchError("auto_crf CRF bounds are too close together")
        if not 4 <= self.samples <= 48:
            raise CrfSearchError("auto_crf.samples must be between 4 and 48")
        if not 1.0 <= self.sample_seconds <= 10.0:
            raise CrfSearchError("auto_crf.sample_seconds must be between 1 and 10")
        if not 3 <= self.max_iterations <= 10:
            raise CrfSearchError("auto_crf.max_iterations must be between 3 and 10")
        if not 0.1 <= self.tolerance <= 3.0:
            raise CrfSearchError("auto_crf.tolerance must be between 0.1 and 3")
        if self.metric not in SCORE_METRICS:
            raise CrfSearchError(
                "auto_crf.metric must be one of " + ", ".join(SCORE_METRICS)
            )
        if self.probe_preset is not None and (
            type(self.probe_preset) is not str or not self.probe_preset
        ):
            raise CrfSearchError("auto_crf.probe_preset must be a preset name")

    @classmethod
    def from_mapping(cls, raw: object) -> AutoCrfConfig:
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise CrfSearchError("video.auto_crf must be an object")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(raw) - allowed
        if unknown:
            raise CrfSearchError(
                "unknown auto_crf field(s): " + ", ".join(sorted(unknown))
            )
        return cls(**dict(raw))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SampleWindow:
    """A contiguous run of frames on the reference timeline."""

    start_frame: int
    frame_count: int

    def __post_init__(self) -> None:
        if self.start_frame < 0 or self.frame_count < 1:
            raise CrfSearchError("sample windows need a non-negative start and length")

    @property
    def end_frame(self) -> int:
        return self.start_frame + self.frame_count

    def to_dict(self) -> dict[str, int]:
        return {"start_frame": self.start_frame, "frame_count": self.frame_count}


def plan_sample_windows(
    total_frames: int,
    frame_rate: Fraction,
    *,
    samples: int,
    sample_seconds: float,
) -> tuple[SampleWindow, ...]:
    """Spread non-overlapping windows over the body of a title.

    Windows are centred in equal segments of the usable region, exactly like the
    crop-detection plan, so the result is deterministic for a given clip.
    """

    if total_frames < 1 or frame_rate <= 0:
        raise CrfSearchError("the clip has no frames to sample")
    skip = int(total_frames * EDGE_SKIP_FRACTION)
    usable = total_frames - 2 * skip
    window = max(2, round(float(frame_rate) * sample_seconds))
    if usable < window:
        raise CrfSearchError(
            "the title is too short for a CRF search "
            f"({total_frames} frames, one sample needs {window})"
        )
    count = min(samples, usable // window)
    if count < 2:
        raise CrfSearchError("the title is too short for at least two CRF samples")
    segment = usable / count
    windows: list[SampleWindow] = []
    for index in range(count):
        centre = skip + segment * (index + 0.5)
        start = int(centre - window / 2)
        start = max(skip, min(start, total_frames - skip - window))
        windows.append(SampleWindow(start, window))
    return tuple(windows)


def quantize_crf(value: float) -> float:
    return round(round(value / CRF_STEP) * CRF_STEP, 6)


@dataclass(frozen=True, slots=True)
class CrfProbe:
    crf: float
    score: float

    def to_dict(self) -> dict[str, float]:
        return {"crf": self.crf, "score": round(self.score, 4)}


@dataclass(frozen=True, slots=True)
class CrfSearchOutcome:
    status: str
    chosen_crf: float | None
    chosen_score: float | None
    probes: tuple[CrfProbe, ...]
    monotonic: bool
    target_vmaf: float

    @property
    def usable(self) -> bool:
        return self.chosen_crf is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "chosen_crf": self.chosen_crf,
            "chosen_score": (
                None if self.chosen_score is None else round(self.chosen_score, 4)
            ),
            "target_vmaf": self.target_vmaf,
            "monotonic": self.monotonic,
            "probes": [probe.to_dict() for probe in self.probes],
        }


@dataclass(slots=True)
class CrfSearch:
    """Bracketing search assuming VMAF falls as CRF rises."""

    config: AutoCrfConfig
    initial_crf: float
    probes: list[CrfProbe] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.initial_crf = self._clamp(self.initial_crf)

    # -- bookkeeping ---------------------------------------------------------
    def _clamp(self, value: float) -> float:
        low = quantize_crf(self.config.min_crf)
        high = quantize_crf(self.config.max_crf)
        return min(high, max(low, quantize_crf(value)))

    def record(self, crf: float, score: float) -> None:
        if not math.isfinite(score):
            raise CrfSearchError("a probe produced a non-finite VMAF score")
        if any(abs(item.crf - crf) < _EPSILON for item in self.probes):
            raise CrfSearchError(f"CRF {crf} was already probed")
        self.probes.append(CrfProbe(crf=crf, score=score))

    def _bracket(self) -> tuple[CrfProbe | None, CrfProbe | None]:
        target = self.config.target_vmaf
        failing = [item for item in self.probes if item.score < target]
        first_fail = min((item.crf for item in failing), default=math.inf)
        passing = [
            item
            for item in self.probes
            if item.score >= target and item.crf < first_fail
        ]
        low = max(passing, key=lambda item: item.crf, default=None)
        above = [
            item for item in failing if low is None or item.crf > low.crf
        ]
        high = min(above, key=lambda item: item.crf, default=None)
        return low, high

    def _slope(self) -> float:
        ordered = sorted(self.probes, key=lambda item: item.crf)
        best: float | None = None
        best_distance = math.inf
        for left, right in zip(ordered, ordered[1:]):
            if right.crf - left.crf < _EPSILON:
                continue
            slope = (right.score - left.score) / (right.crf - left.crf)
            centre = (left.score + right.score) / 2
            distance = abs(centre - self.config.target_vmaf)
            if distance < best_distance:
                best, best_distance = slope, distance
        if best is None or not math.isfinite(best):
            return DEFAULT_SLOPE
        return min(_SLOPE_LIMITS[1], max(_SLOPE_LIMITS[0], best))

    # -- decisions -----------------------------------------------------------
    def next_crf(self) -> float | None:
        """Return the next CRF to probe, or ``None`` when the search is over."""

        if len(self.probes) >= self.config.max_iterations:
            return None
        if not self.probes:
            return self.initial_crf
        target = self.config.target_vmaf
        low_bound = quantize_crf(self.config.min_crf)
        high_bound = quantize_crf(self.config.max_crf)
        low, high = self._bracket()
        if low is not None and low.score - target <= self.config.tolerance:
            return None
        if low is not None and high is not None:
            gap = high.crf - low.crf
            if gap <= CRF_STEP + _EPSILON:
                return None
            span = low.score - high.score
            fraction = (low.score - target) / span if span > _EPSILON else 0.5
            candidate = quantize_crf(low.crf + fraction * gap)
            return min(high.crf - CRF_STEP, max(low.crf + CRF_STEP, candidate))
        slope = -self._slope()
        if low is not None:
            if low.crf >= high_bound - _EPSILON:
                return None
            candidate = quantize_crf(low.crf + (low.score - target) / slope)
            return min(high_bound, max(low.crf + CRF_STEP, candidate))
        if high is not None:
            if high.crf <= low_bound + _EPSILON:
                return None
            candidate = quantize_crf(high.crf - (target - high.score) / slope)
            return max(low_bound, min(high.crf - CRF_STEP, candidate))
        return None

    def outcome(self) -> CrfSearchOutcome:
        target = self.config.target_vmaf
        low, _high = self._bracket()
        ordered = sorted(self.probes, key=lambda item: item.crf)
        monotonic = all(
            right.score <= left.score + 0.25 for left, right in zip(ordered, ordered[1:])
        )
        if low is None:
            return CrfSearchOutcome(
                STATUS_UNREACHABLE, None, None, tuple(ordered), monotonic, target
            )
        if low.score - target <= self.config.tolerance:
            status = STATUS_CONVERGED
        elif low.crf >= quantize_crf(self.config.max_crf) - _EPSILON:
            status = STATUS_MAX_CRF
        else:
            status = STATUS_BEST_EFFORT
        return CrfSearchOutcome(
            status, low.crf, low.score, tuple(ordered), monotonic, target
        )


# -- libvmaf JSON ------------------------------------------------------------


def _frame_scores(document: Mapping[str, Any]) -> list[float]:
    frames = document.get("frames")
    if not isinstance(frames, Sequence) or isinstance(frames, (str, bytes)):
        return []
    scores: list[float] = []
    for frame in frames:
        metrics = frame.get("metrics") if isinstance(frame, Mapping) else None
        value = metrics.get("vmaf") if isinstance(metrics, Mapping) else None
        if type(value) in {int, float} and math.isfinite(value):
            scores.append(float(value))
    return scores


def vmaf_score(document: Mapping[str, Any], metric: str = "mean") -> float:
    """Extract one pooled VMAF number from libvmaf's JSON report."""

    if metric not in SCORE_METRICS:
        raise CrfSearchError(f"unsupported VMAF metric: {metric}")
    if metric != "percentile_1":
        pooled = document.get("pooled_metrics")
        entry = pooled.get("vmaf") if isinstance(pooled, Mapping) else None
        value = entry.get(metric) if isinstance(entry, Mapping) else None
        if type(value) in {int, float} and math.isfinite(value):
            return float(value)
    scores = _frame_scores(document)
    if not scores:
        raise CrfSearchError("libvmaf JSON contains no VMAF frame scores")
    if metric == "mean":
        return sum(scores) / len(scores)
    if metric == "harmonic_mean":
        return len(scores) / sum(1.0 / max(score, 1e-6) for score in scores)
    ordered = sorted(scores)
    index = max(0, math.ceil(0.01 * len(ordered)) - 1)
    return ordered[index]


def sample_frame_total(windows: Sequence[SampleWindow]) -> int:
    return sum(window.frame_count for window in windows)


def vmaf_model_for_height(height: int | None) -> bool:
    """Return whether the 4K VMAF model should be used for this picture height."""

    return height is not None and height >= 1440
