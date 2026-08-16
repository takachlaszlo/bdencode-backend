"""Parse FFmpeg's machine progress protocol and report encode progress safely."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping

from .models import JobState


LOG = logging.getLogger(__name__)

# These values describe progress through the complete job, rather than merely
# the expensive video encode. Paused and terminal error states intentionally
# have no baseline: their last durable progress is preserved.
PIPELINE_PROGRESS_BASELINES: Mapping[JobState, float] = {
    JobState.QUEUED: 0.0,
    JobState.SCANNING: 0.02,
    JobState.AWAITING_SELECTION: 0.10,
    JobState.READY: 0.12,
    JobState.ENCODING: 0.15,
    JobState.MUXING: 0.78,
    JobState.QC: 0.85,
    JobState.COMPARISON: 0.92,
    JobState.UPLOADING: 0.98,
    JobState.COMPLETED: 1.0,
}

_ENCODE_START = PIPELINE_PROGRESS_BASELINES[JobState.ENCODING]
_ENCODE_END = PIPELINE_PROGRESS_BASELINES[JobState.MUXING]
_SPEED_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)x$")
_MILESTONES = (0.0, 0.10, 0.25, 0.50, 0.75, 0.90, 1.0)


def pipeline_progress_baseline(state: JobState) -> float | None:
    """Return the whole-pipeline baseline for a normal durable state."""

    return PIPELINE_PROGRESS_BASELINES.get(state)


def encoding_overall_progress(stage_fraction: float) -> float:
    """Map video-only progress into its share of the complete pipeline."""

    fraction = min(1.0, max(0.0, stage_fraction))
    return _ENCODE_START + ((_ENCODE_END - _ENCODE_START) * fraction)


def _finite_float(value: str | None) -> float | None:
    if not value or value.casefold() in {"n/a", "nan", "inf", "+inf", "-inf"}:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _nonnegative_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _clock_seconds(value: str | None) -> float | None:
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
    except ValueError:
        return None
    result = (hours * 3600) + (minutes * 60) + seconds
    return result if result >= 0 and math.isfinite(result) else None


@dataclass(frozen=True, slots=True)
class EncodeProgress:
    stage_fraction: float
    out_time_seconds: float
    duration_seconds: float
    frame: int | None
    fps: float | None
    speed: float | None
    eta_seconds: float | None
    protocol_status: str

    @property
    def overall_progress(self) -> float:
        return encoding_overall_progress(self.stage_fraction)

    def details(self) -> dict[str, object]:
        value = asdict(self)
        value["overall_progress"] = self.overall_progress
        return value


class FFmpegProgressParser:
    """Incrementally parse ``-progress`` key/value blocks from FFmpeg stderr."""

    _KNOWN_KEYS = frozenset(
        {
            "frame",
            "fps",
            "out_time_us",
            "out_time_ms",
            "out_time",
            "speed",
            "progress",
        }
    )

    def __init__(self, duration_seconds: float) -> None:
        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise ValueError("duration_seconds must be a positive finite number")
        self.duration_seconds = duration_seconds
        self._fields: dict[str, str] = {}
        self._last_stage_fraction = 0.0

    def feed_line(self, line: str) -> EncodeProgress | None:
        text = line.strip()
        if "=" not in text:
            return None
        key, value = text.split("=", 1)
        if key not in self._KNOWN_KEYS:
            return None
        self._fields[key] = value.strip()
        if key != "progress":
            return None

        status = self._fields.get("progress", "continue").casefold()
        out_time = self._out_time_seconds()
        if status == "end":
            stage_fraction = 1.0
            out_time = max(out_time, self.duration_seconds)
        else:
            stage_fraction = min(1.0, max(0.0, out_time / self.duration_seconds))
        stage_fraction = max(self._last_stage_fraction, stage_fraction)
        self._last_stage_fraction = stage_fraction

        fps = _finite_float(self._fields.get("fps"))
        speed_value = self._fields.get("speed", "")
        speed_match = _SPEED_RE.fullmatch(speed_value)
        speed = _finite_float(speed_match.group(1)) if speed_match else None
        remaining = max(0.0, self.duration_seconds - out_time)
        eta = remaining / speed if speed is not None and speed > 0 else None
        snapshot = EncodeProgress(
            stage_fraction=stage_fraction,
            out_time_seconds=max(0.0, out_time),
            duration_seconds=self.duration_seconds,
            frame=_nonnegative_int(self._fields.get("frame")),
            fps=fps if fps is None or fps >= 0 else None,
            speed=speed,
            eta_seconds=eta,
            protocol_status=status,
        )
        self._fields.clear()
        return snapshot

    def _out_time_seconds(self) -> float:
        # Despite its historical name, FFmpeg documents out_time_ms in the
        # progress protocol as microseconds too. Prefer the unambiguous key.
        for key in ("out_time_us", "out_time_ms"):
            value = _finite_float(self._fields.get(key))
            if value is not None:
                return max(0.0, value / 1_000_000)
        return _clock_seconds(self._fields.get("out_time")) or 0.0


def _format_duration(seconds: float) -> str:
    rounded = max(0, int(round(seconds)))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def encode_status_message(progress: EncodeProgress) -> str:
    parts = [f"Videó kódolása: {progress.stage_fraction * 100:.1f}%"]
    if progress.fps is not None:
        parts.append(f"{progress.fps:.1f} fps")
    if progress.speed is not None:
        parts.append(f"{progress.speed:.2f}x")
    if progress.eta_seconds is not None:
        parts.append(f"ETA {_format_duration(progress.eta_seconds)}")
    return " · ".join(parts)


class EncodeProgressReporter:
    """Persist parsed progress without allowing telemetry to fail an encode."""

    def __init__(
        self,
        duration_seconds: float,
        jsonl_path: Path,
        record_progress: Callable[[float, str, dict[str, object]], None],
        *,
        minimum_db_interval: float = 10.0,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if minimum_db_interval < 0:
            raise ValueError("minimum_db_interval cannot be negative")
        self.parser = FFmpegProgressParser(duration_seconds)
        self.jsonl_path = jsonl_path
        self.record_progress = record_progress
        self.minimum_db_interval = minimum_db_interval
        self.monotonic_clock = monotonic_clock
        self._last_db_at: float | None = None
        self._emitted_milestones: set[float] = set()

    def start(self) -> None:
        recorded = self._record(
            encoding_overall_progress(0.0),
            "Videó kódolása: 0.0% · ETA számítása…",
            {
                "stage": JobState.ENCODING.value,
                "stage_fraction": 0.0,
                "duration_seconds": self.parser.duration_seconds,
                "milestone_percent": 0,
            },
        )
        if recorded:
            self._emitted_milestones.add(0.0)
            self._last_db_at = self.monotonic_clock()

    def handle_line(self, line: str) -> None:
        try:
            snapshot = self.parser.feed_line(line)
            if snapshot is None:
                return
            self._append_jsonl(snapshot)
            now = self.monotonic_clock()
            crossed = [
                milestone
                for milestone in _MILESTONES
                if milestone not in self._emitted_milestones
                and snapshot.stage_fraction >= milestone
            ]
            due = (
                self._last_db_at is None
                or now - self._last_db_at >= self.minimum_db_interval
                or bool(crossed)
                or snapshot.stage_fraction >= 1.0
            )
            if due:
                details: dict[str, object] = {
                    "stage": JobState.ENCODING.value,
                    **snapshot.details(),
                }
                if crossed:
                    details["milestone_percent"] = int(max(crossed) * 100)
                recorded = self._record(
                    snapshot.overall_progress,
                    encode_status_message(snapshot),
                    details,
                )
                if recorded:
                    self._emitted_milestones.update(crossed)
                    self._last_db_at = now
        except Exception:
            LOG.exception("video progress observation failed; encode continues")

    def complete(self) -> None:
        snapshot = EncodeProgress(
            stage_fraction=1.0,
            out_time_seconds=self.parser.duration_seconds,
            duration_seconds=self.parser.duration_seconds,
            frame=None,
            fps=None,
            speed=None,
            eta_seconds=0.0,
            protocol_status="end",
        )
        try:
            self._append_jsonl(snapshot)
            details: dict[str, object] = {
                "stage": JobState.ENCODING.value,
                **snapshot.details(),
            }
            if 1.0 not in self._emitted_milestones:
                details["milestone_percent"] = 100
            recorded = self._record(
                snapshot.overall_progress,
                encode_status_message(snapshot),
                details,
            )
            if recorded:
                self._emitted_milestones.add(1.0)
        except Exception:
            LOG.exception("final video progress observation failed; encode continues")

    def _record(
        self, progress: float, message: str, details: dict[str, object]
    ) -> bool:
        try:
            self.record_progress(progress, message, details)
            return True
        except Exception:
            LOG.exception("durable video progress update failed; encode continues")
            return False

    def _append_jsonl(self, progress: EncodeProgress) -> None:
        self.jsonl_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if os.path.lexists(self.jsonl_path):
            is_junction = getattr(self.jsonl_path, "is_junction", None)
            if self.jsonl_path.is_symlink() or (
                callable(is_junction) and is_junction()
            ):
                raise ValueError(
                    "progress JSONL path cannot be a symbolic link or junction"
                )
        record = {
            "timestamp": datetime.now(UTC).isoformat(timespec="microseconds"),
            **progress.details(),
        }
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
