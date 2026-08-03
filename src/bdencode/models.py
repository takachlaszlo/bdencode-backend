"""Domain models and the durable job state machine.

The worker and the HTTP API deliberately share this module.  Keeping the
transition table in one place prevents an API request and a restarted worker
from interpreting a job differently.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


JsonObject = dict[str, Any]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class JobState(StrEnum):
    QUEUED = "QUEUED"
    SCANNING = "SCANNING"
    AWAITING_SELECTION = "AWAITING_SELECTION"
    READY = "READY"
    ENCODING = "ENCODING"
    MUXING = "MUXING"
    QC = "QC"
    COMPARISON = "COMPARISON"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UPLOAD_FAILED = "UPLOAD_FAILED"


TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED})

# A FAILED job may only be restored into local media stages guarded by durable,
# content-validated markers. Scanning is excluded because its marker does not
# fingerprint replaceable source contents; uploading is excluded because a
# remote success can precede its local checkpoint and is therefore at-least-once.
RETRYABLE_FAILED_STAGES = frozenset(
    {
        JobState.READY,
        JobState.ENCODING,
        JobState.MUXING,
        JobState.QC,
        JobState.COMPARISON,
    }
)

# QUEUED is intentionally not blocking: any number of jobs may wait, while the
# partial unique index in db.py permits exactly one state from this set.
BLOCKING_STATES = frozenset(set(JobState) - set(TERMINAL_STATES) - {JobState.QUEUED})

PIPELINE_STATES = (
    JobState.QUEUED,
    JobState.SCANNING,
    JobState.AWAITING_SELECTION,
    JobState.READY,
    JobState.ENCODING,
    JobState.MUXING,
    JobState.QC,
    JobState.COMPARISON,
    JobState.UPLOADING,
    JobState.COMPLETED,
)


_NORMAL_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.SCANNING}),
    JobState.SCANNING: frozenset(
        {JobState.AWAITING_SELECTION, JobState.READY, JobState.NEEDS_REVIEW}
    ),
    JobState.AWAITING_SELECTION: frozenset({JobState.READY}),
    JobState.READY: frozenset({JobState.ENCODING, JobState.NEEDS_REVIEW}),
    JobState.ENCODING: frozenset({JobState.MUXING, JobState.NEEDS_REVIEW}),
    JobState.MUXING: frozenset({JobState.QC, JobState.NEEDS_REVIEW}),
    JobState.QC: frozenset({JobState.COMPARISON, JobState.NEEDS_REVIEW}),
    JobState.COMPARISON: frozenset({JobState.UPLOADING, JobState.NEEDS_REVIEW}),
    JobState.UPLOADING: frozenset(
        {JobState.COMPLETED, JobState.UPLOAD_FAILED, JobState.NEEDS_REVIEW}
    ),
    JobState.UPLOAD_FAILED: frozenset({JobState.UPLOADING, JobState.NEEDS_REVIEW}),
    JobState.NEEDS_REVIEW: frozenset(),  # resume target is checked dynamically
    JobState.COMPLETED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


def allowed_transitions(
    state: JobState, *, resume_state: JobState | None = None
) -> frozenset[JobState]:
    """Return legal targets, including failure/cancellation and review resume."""

    if state in TERMINAL_STATES:
        return frozenset()
    targets = set(_NORMAL_TRANSITIONS[state])
    targets.update({JobState.FAILED, JobState.CANCELLED})
    if state is JobState.NEEDS_REVIEW and resume_state is not None:
        targets.add(resume_state)
        # Replacing material operator choices must replay dependency checks and
        # invalidate downstream markers from READY. A plain review acknowledgement
        # may still resume directly at ``resume_state``.
        targets.add(JobState.READY)
    return frozenset(targets)


def validate_transition(
    current: JobState,
    target: JobState,
    *,
    resume_state: JobState | None = None,
) -> None:
    if target not in allowed_transitions(current, resume_state=resume_state):
        legal = (
            ", ".join(
                sorted(
                    item.value
                    for item in allowed_transitions(current, resume_state=resume_state)
                )
            )
            or "none"
        )
        raise ValueError(
            f"illegal job transition {current.value} -> {target.value}; allowed: {legal}"
        )


class DiscType(StrEnum):
    AUTO = "AUTO"
    BD = "BD"
    UHD = "UHD"


class ContentType(StrEnum):
    FILM = "FILM"
    CONCERT = "CONCERT"
    ANIME = "ANIME"
    SERIES = "SERIES"


class ScanState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_SELECTION = "AWAITING_SELECTION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class JobCreate(StrictModel):
    source_path: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    work_path: str | None = None
    output_path: str | None = None
    disc_type: DiscType = DiscType.AUTO
    content_type: ContentType = ContentType.FILM
    priority: int = Field(default=0, ge=-1000, le=1000)
    settings: JsonObject = Field(default_factory=dict)
    requested_by: str | None = Field(default=None, max_length=255)

    @field_validator("source_path", "work_path", "output_path")
    @classmethod
    def reject_nul(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("paths may not contain NUL bytes")
        return value


class Job(StrictModel):
    id: str
    name: str
    source_path: str
    work_path: str | None
    output_path: str | None
    disc_type: DiscType
    content_type: ContentType
    state: JobState
    priority: int
    settings: JsonObject
    selection: JsonObject | None
    requested_by: str | None
    progress: float | None = Field(default=None, ge=0, le=1)
    status_message: str | None
    error: str | None
    resume_state: JobState | None
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobTransitionRequest(StrictModel):
    state: JobState
    message: str | None = Field(default=None, max_length=4000)
    details: JsonObject = Field(default_factory=dict)
    expected_version: int | None = Field(default=None, ge=1)


class JobRetryRequest(StrictModel):
    message: str | None = Field(default=None, max_length=4000)
    expected_version: int | None = Field(default=None, ge=1)


class JobSelectionRequest(StrictModel):
    selection: JsonObject
    message: str | None = Field(default=None, max_length=4000)
    expected_version: int | None = Field(default=None, ge=1)


class NormalizedCrop(StrictModel):
    left: int = Field(ge=0)
    top: int = Field(ge=0)
    right: int = Field(ge=0)
    bottom: int = Field(ge=0)


class SelectionValidationResponse(StrictModel):
    valid: bool
    playlist_id: str
    encoder: str
    settings: JsonObject
    ffmpeg_video_args: list[str]
    crop: NormalizedCrop
    temporal_filter: str
    advisory_warnings: list[str]


class JobProgressRequest(StrictModel):
    progress: float = Field(ge=0, le=1)
    message: str | None = Field(default=None, max_length=4000)
    details: JsonObject = Field(default_factory=dict)


class ScanCreate(StrictModel):
    job_id: str
    source_path: str | None = None
    status: ScanState = ScanState.RUNNING
    result: JsonObject = Field(default_factory=dict)


class ScanUpdate(StrictModel):
    status: ScanState
    result: JsonObject | None = None
    error: str | None = Field(default=None, max_length=16000)
    message: str | None = Field(default=None, max_length=4000)


class Scan(StrictModel):
    id: str
    job_id: str
    source_path: str
    status: ScanState
    result: JsonObject
    error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class ArtifactKind(StrEnum):
    LOG = "LOG"
    MANIFEST = "MANIFEST"
    MEDIAINFO = "MEDIAINFO"
    MKVINFO = "MKVINFO"
    VIDEO_COMPARISON = "VIDEO_COMPARISON"
    AUDIO_COMPARISON = "AUDIO_COMPARISON"
    SPECTROGRAM = "SPECTROGRAM"
    REPORT = "REPORT"
    BBCODE = "BBCODE"
    OUTPUT = "OUTPUT"
    OTHER = "OTHER"


class ArtifactCreate(StrictModel):
    job_id: str
    scan_id: str | None = None
    kind: ArtifactKind = ArtifactKind.OTHER
    name: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1)
    mime_type: str | None = Field(default=None, max_length=255)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)
    metadata: JsonObject = Field(default_factory=dict)


class Artifact(ArtifactCreate):
    id: str
    created_at: datetime


class EventCreate(StrictModel):
    job_id: str | None = None
    scan_id: str | None = None
    kind: str = Field(min_length=1, max_length=100)
    message: str | None = Field(default=None, max_length=4000)
    payload: JsonObject = Field(default_factory=dict)


class Event(StrictModel):
    id: int
    job_id: str | None
    scan_id: str | None
    kind: str
    state_from: JobState | None
    state_to: JobState | None
    message: str | None
    payload: JsonObject
    created_at: datetime


class HealthResponse(StrictModel):
    status: str
    database: str
    schema_version: int
    active_job_id: str | None
    blocking_state: JobState | None
    queued_jobs: int


class CapabilitiesResponse(StrictModel):
    api_version: str
    job_states: list[JobState]
    terminal_states: list[JobState]
    blocking_states: list[JobState]
    transitions: dict[str, list[JobState]]
    input_video_codecs: list[str]
    output_video_codecs: list[str]
    disc_types: list[DiscType]
    content_types: list[ContentType]
    detail_levels: list[str]
    audio_actions: list[str]
    constraints: JsonObject


class QueueClaimResponse(StrictModel):
    job: Job | None
    blocked_by: Job | None


class ListMeta(StrictModel):
    limit: int
    offset: int
    count: int


class JobList(StrictModel):
    items: list[Job]
    meta: ListMeta


class ScanList(StrictModel):
    items: list[Scan]
    meta: ListMeta


class ArtifactList(StrictModel):
    items: list[Artifact]
    meta: ListMeta


class EventList(StrictModel):
    items: list[Event]
    after_id: int
