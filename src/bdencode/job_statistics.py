"""Per-file and aggregate encode statistics for completed jobs.

Everything here is derived from durable evidence the pipeline already keeps
under the job root after completion (manifest, comparison metrics, CRF search
report, the recorded source size) plus the database event log.  Missing
evidence yields ``None`` fields instead of guesses, so the numbers are never
invented for jobs finished by an older release.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import Artifact, ArtifactKind, Event, Job, JobState

GIB = 1024**3
_FRAMES_RE = re.compile(r"(?im)^Frames:\s*(\d+)\s*$")
_FPS_RE = re.compile(r"(?im)^FPS:\s*(\d+)\s*/\s*(\d+)")


def _json(path: Path) -> Mapping[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, Mapping) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def encode_seconds(events: Sequence[Event]) -> float | None:
    """Wall time spent in ``ENCODING``, excluding operator pauses.

    A job that was retried or restarted enters ``ENCODING`` more than once; every
    interval counts, because the CPU time was really spent.  ``None`` means the
    log never reached ``MUXING``/``FAILED``/etc. after entering ``ENCODING``.
    """

    ordered = sorted(events, key=lambda item: (item.created_at, item.id))
    intervals: list[tuple[datetime, datetime]] = []
    entered: datetime | None = None
    pauses: list[tuple[datetime, datetime]] = []
    paused_at: datetime | None = None
    for event in ordered:
        if event.kind == "job.control.paused":
            paused_at = event.created_at
        elif event.kind == "job.control.resumed" and paused_at is not None:
            pauses.append((paused_at, event.created_at))
            paused_at = None
        if event.kind != "job.state" or event.state_to is None:
            continue
        if entered is not None:
            intervals.append((entered, event.created_at))
            entered = None
        if event.state_to is JobState.ENCODING:
            entered = event.created_at
    if not intervals:
        return None
    total = timedelta()
    for start, end in intervals:
        span = end - start
        for pause_start, pause_end in pauses:
            overlap = min(end, pause_end) - max(start, pause_start)
            if overlap > timedelta():
                span -= overlap
        total += max(span, timedelta())
    return total.total_seconds()


def _reference_timeline(job_root: Path | None) -> tuple[int, float] | None:
    if job_root is None:
        return None
    try:
        text = (job_root / "comparison" / "reference-vapoursynth-info.txt").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    frames = _FRAMES_RE.search(text)
    rate = _FPS_RE.search(text)
    if frames is None or rate is None or int(rate.group(1)) == 0:
        return None
    frame_count = int(frames.group(1))
    return frame_count, frame_count * int(rate.group(2)) / int(rate.group(1))


def _output_bytes(job: Job, artifacts: Sequence[Artifact]) -> int | None:
    for artifact in artifacts:
        if artifact.kind is ArtifactKind.OUTPUT and artifact.size_bytes is not None:
            return int(artifact.size_bytes)
    if job.output_path:
        try:
            return Path(job.output_path).stat().st_size
        except OSError:
            return None
    return None


def compute_job_statistics(
    job: Job,
    events: Sequence[Event],
    artifacts: Sequence[Artifact],
    job_root: Path | None,
) -> dict[str, Any]:
    manifest = _json(job_root / "manifest.json") if job_root else None
    crf_report = _json(job_root / "analysis" / "crf-search.json") if job_root else None
    metrics = _json(job_root / "comparison" / "video-metrics.json") if job_root else None
    source_record = _json(job_root / "analysis" / "source-size.json") if job_root else None

    settings = (manifest or {}).get("encoder_settings")
    settings = settings if isinstance(settings, Mapping) else {}
    if not settings and isinstance(job.selection, Mapping):
        video = job.selection.get("video")
        overrides = video.get("settings") if isinstance(video, Mapping) else None
        settings = overrides if isinstance(overrides, Mapping) else {}

    source_bytes = (
        int(source_record["total_bytes"])
        if source_record and isinstance(source_record.get("total_bytes"), int)
        else None
    )
    output_bytes = _output_bytes(job, artifacts)
    timeline = _reference_timeline(job_root)
    frames, media_seconds = timeline if timeline else (None, None)
    spent = encode_seconds(events)

    saved = (
        source_bytes - output_bytes
        if source_bytes is not None and output_bytes is not None
        else None
    )
    aggregate = (metrics or {}).get("aggregate")
    aggregate = aggregate if isinstance(aggregate, Mapping) else {}
    vmaf_sample = _number((crf_report or {}).get("chosen_score"))
    started = job.started_at or job.created_at
    total_seconds = (
        (job.finished_at - started).total_seconds() if job.finished_at else None
    )
    return {
        "job_id": job.id,
        "name": job.name,
        "state": job.state.value,
        "disc_type": job.disc_type.value,
        "content_type": job.content_type.value,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "encoder": settings.get("encoder"),
        "crf": _number(settings.get("crf")),
        "preset": settings.get("preset"),
        "source_bytes": source_bytes,
        "output_bytes": output_bytes,
        "saved_bytes": saved,
        "saved_percent": (
            _round(saved / source_bytes * 100, 2)
            if saved is not None and source_bytes
            else None
        ),
        "media_seconds": _round(media_seconds, 1),
        "frames": frames,
        "bitrate_kbps": (
            _round(output_bytes * 8 / media_seconds / 1000, 1)
            if output_bytes is not None and media_seconds
            else None
        ),
        "encode_seconds": _round(spent, 1),
        "encode_fps": (
            _round(frames / spent, 2) if frames is not None and spent else None
        ),
        "realtime_factor": (
            _round(media_seconds / spent, 3) if media_seconds and spent else None
        ),
        "total_seconds": _round(total_seconds, 1),
        "quality": {
            "vmaf_sample": _round(vmaf_sample, 2),
            "vmaf_target": _number(
                (crf_report or {}).get("target_vmaf")
                or ((crf_report or {}).get("config") or {}).get("target_vmaf")
            ),
            "vmaf_scope": "auto-crf sample encodes" if vmaf_sample is not None else None,
            "ssim_mean": _round(_number(aggregate.get("ssim_all_mean")), 5),
            "psnr_mean_db": _round(_number(aggregate.get("psnr_average_db_mean")), 2),
            "comparison_samples": (metrics or {}).get("sample_count"),
        },
        "auto_crf": (
            {
                "status": crf_report.get("status"),
                "chosen_crf": _number(crf_report.get("chosen_crf")),
                "probes": len(crf_report.get("probes") or []),
            }
            if crf_report
            else None
        ),
    }


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Totals and averages over per-file statistics (skipping missing values)."""

    def column(key: str) -> list[float]:
        return [float(row[key]) for row in rows if row.get(key) is not None]

    def quality(key: str) -> list[float]:
        return [
            float(row["quality"][key])
            for row in rows
            if row.get("quality") and row["quality"].get(key) is not None
        ]

    with_both = [
        row
        for row in rows
        if row.get("source_bytes") is not None and row.get("output_bytes") is not None
    ]
    source_total = sum(int(row["source_bytes"]) for row in with_both)
    output_total = sum(int(row["output_bytes"]) for row in with_both)
    timed = [
        row
        for row in rows
        if row.get("frames") is not None and row.get("encode_seconds")
    ]
    timed_frames = sum(int(row["frames"]) for row in timed)
    timed_seconds = sum(float(row["encode_seconds"]) for row in timed)
    encoders: dict[str, int] = {}
    for row in rows:
        if row.get("encoder"):
            encoders[str(row["encoder"])] = encoders.get(str(row["encoder"]), 0) + 1
    return {
        "jobs": len(rows),
        "jobs_with_size_evidence": len(with_both),
        "source_gib": round(source_total / GIB, 2),
        "output_gib": round(output_total / GIB, 2),
        "saved_gib": round((source_total - output_total) / GIB, 2),
        "saved_percent": (
            round((source_total - output_total) / source_total * 100, 2)
            if source_total
            else None
        ),
        "total_output_gib": round(
            sum(int(row["output_bytes"]) for row in rows if row.get("output_bytes") is not None)
            / GIB,
            2,
        ),
        "average_vmaf_sample": _mean(quality("vmaf_sample")),
        "average_ssim": _mean(quality("ssim_mean")),
        "average_psnr_db": _mean(quality("psnr_mean_db")),
        "average_crf": _mean(column("crf")),
        "average_bitrate_kbps": _mean(column("bitrate_kbps")),
        "encode_hours": round(sum(column("encode_seconds")) / 3600, 2),
        "average_encode_fps": (
            round(timed_frames / timed_seconds, 2) if timed_seconds else None
        ),
        "average_realtime_factor": _mean(column("realtime_factor")),
        "encoders": encoders,
    }


def completed_statistics(
    jobs: Sequence[Job],
    load_events: Any,
    load_artifacts: Any,
    job_root_for: Any,
) -> dict[str, Any]:
    """Statistics for ``jobs`` (only ``COMPLETED`` ones are included)."""

    rows = [
        compute_job_statistics(
            job, load_events(job.id), load_artifacts(job.id), job_root_for(job.id)
        )
        for job in jobs
        if job.state is JobState.COMPLETED
    ]
    return {"summary": summarize(rows), "jobs": rows}
