"""High-level operations for the persistent preparation and encode queues."""

from __future__ import annotations

from typing import Any

from .db import Database, StateConflictError
from .models import Job, JobCreate, JobState, QueueClaimResponse


class JobQueue:
    """Facade used by both API handlers and the worker.

    Enqueueing never disturbs the active encode. ``claim_next`` atomically owns
    the one scan lane, while ``claim_next_ready`` atomically owns the one encode
    lane. Both recover from durable SQLite state after a reboot.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def enqueue(self, request: JobCreate) -> Job:
        return self.database.create_job(request)

    def claim_next(self) -> Job | None:
        return self.database.claim_next_job()

    def claim_next_ready(self) -> Job | None:
        return self.database.claim_next_ready_job()

    def claim_status(self) -> QueueClaimResponse:
        active = self.database.preparing_job()
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

    def retry_failed(
        self,
        job_id: str,
        *,
        message: str | None = None,
        expected_version: int | None = None,
    ) -> Job:
        return self.database.retry_failed_job(
            job_id,
            message=message,
            expected_version=expected_version,
        )

    def restart_cancelled(
        self,
        job_id: str,
        *,
        message: str | None = None,
        expected_version: int | None = None,
    ) -> Job:
        return self.database.restart_cancelled_job(
            job_id,
            message=message,
            expected_version=expected_version,
        )

    def blocker(self) -> Job | None:
        return self.database.encoding_job()

    def queued_count(self) -> int:
        return self.database.count_jobs(states=[JobState.QUEUED])

    def ready_count(self) -> int:
        return self.database.count_jobs(states=[JobState.READY])


# A descriptive alias for callers that prefer the longer service name.
PersistentJobQueue = JobQueue
