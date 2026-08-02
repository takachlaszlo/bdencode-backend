"""High-level operations for the persistent, strictly serial job queue."""

from __future__ import annotations

from typing import Any

from .db import Database, StateConflictError
from .models import Job, JobCreate, JobState, QueueClaimResponse


class JobQueue:
    """Facade used by both API handlers and the worker.

    Enqueueing never disturbs the active job.  ``claim_next`` is the only entry
    into SCANNING and is atomic in SQLite, allowing the service to recover after
    a reboot without an in-memory lock.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def enqueue(self, request: JobCreate) -> Job:
        return self.database.create_job(request)

    def claim_next(self) -> Job | None:
        return self.database.claim_next_job()

    def claim_status(self) -> QueueClaimResponse:
        active = self.database.active_job()
        if active is not None:
            return QueueClaimResponse(job=None, blocked_by=active)
        claimed = self.database.claim_next_job()
        return QueueClaimResponse(job=claimed, blocked_by=None)

    def advance(
        self,
        job_id: str,
        target: JobState,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> Job:
        return self.database.transition_job(
            job_id,
            target,
            message=message,
            details=details,
            expected_version=expected_version,
        )

    def cancel(self, job_id: str, *, message: str = "cancelled") -> Job:
        return self.advance(job_id, JobState.CANCELLED, message=message)

    def fail(
        self,
        job_id: str,
        *,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> Job:
        return self.advance(
            job_id,
            JobState.FAILED,
            message=message,
            details=details,
        )

    def needs_review(
        self,
        job_id: str,
        *,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> Job:
        return self.advance(
            job_id,
            JobState.NEEDS_REVIEW,
            message=message,
            details=details,
        )

    def resume_review(self, job_id: str, *, message: str = "review accepted") -> Job:
        job = self.database.get_job(job_id)
        if job.state is not JobState.NEEDS_REVIEW or job.resume_state is None:
            raise StateConflictError(
                "job is not resumable from NEEDS_REVIEW", current=job.state
            )
        return self.advance(job_id, job.resume_state, message=message)

    def retry_upload(self, job_id: str, *, message: str = "retrying upload") -> Job:
        job = self.database.get_job(job_id)
        if job.state is not JobState.UPLOAD_FAILED:
            raise StateConflictError(
                "only UPLOAD_FAILED jobs can retry upload", current=job.state
            )
        return self.advance(job_id, JobState.UPLOADING, message=message)

    def blocker(self) -> Job | None:
        return self.database.active_job()

    def queued_count(self) -> int:
        return self.database.count_jobs(states=[JobState.QUEUED])


# A descriptive alias for callers that prefer the longer service name.
PersistentJobQueue = JobQueue
