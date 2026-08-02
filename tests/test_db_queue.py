from __future__ import annotations

import pytest

from bdencode.db import Database, StateConflictError
from bdencode.models import (
    ArtifactCreate,
    ArtifactKind,
    JobCreate,
    JobState,
    ScanCreate,
    ScanState,
    ScanUpdate,
)
from bdencode.queue import JobQueue


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "encoder.sqlite3")
    db.initialize()
    return db


def enqueue(queue: JobQueue, name: str, *, priority: int = 0):
    return queue.enqueue(
        JobCreate(source_path=f"/storage/{name}/BDMV", name=name, priority=priority)
    )


def finish(queue: JobQueue, job_id: str) -> None:
    for target in (
        JobState.READY,
        JobState.ENCODING,
        JobState.MUXING,
        JobState.QC,
        JobState.COMPARISON,
        JobState.UPLOADING,
        JobState.COMPLETED,
    ):
        queue.advance(job_id, target)


def test_serial_queue_allows_backlog_but_only_one_active(database):
    queue = JobQueue(database)
    low = enqueue(queue, "low", priority=0)
    high = enqueue(queue, "high", priority=10)

    claimed = queue.claim_next()
    assert claimed is not None
    assert claimed.id == high.id
    assert claimed.state is JobState.SCANNING
    assert queue.claim_next() is None
    assert database.get_job(low.id).state is JobState.QUEUED

    finish(queue, high.id)
    second = queue.claim_next()
    assert second is not None
    assert second.id == low.id


def test_state_machine_rejects_skips_and_uses_optimistic_version(database):
    queue = JobQueue(database)
    job = enqueue(queue, "film")
    claimed = queue.claim_next()

    with pytest.raises(StateConflictError, match="illegal job transition"):
        queue.advance(job.id, JobState.ENCODING)

    with pytest.raises(StateConflictError, match="expected"):
        queue.advance(
            job.id,
            JobState.READY,
            expected_version=claimed.version + 10,
        )

    ready = queue.advance(job.id, JobState.READY, expected_version=claimed.version)
    assert ready.version == claimed.version + 1


def test_needs_review_and_upload_failure_both_block_queue(database):
    queue = JobQueue(database)
    first = enqueue(queue, "first")
    enqueue(queue, "second")
    queue.claim_next()
    queue.advance(first.id, JobState.READY)
    queue.advance(first.id, JobState.ENCODING)

    review = queue.needs_review(first.id, message="ambiguous cadence")
    assert review.state is JobState.NEEDS_REVIEW
    assert review.resume_state is JobState.ENCODING
    assert queue.claim_next() is None
    resumed = queue.resume_review(first.id)
    assert resumed.state is JobState.ENCODING

    for state in (
        JobState.MUXING,
        JobState.QC,
        JobState.COMPARISON,
        JobState.UPLOADING,
        JobState.UPLOAD_FAILED,
    ):
        queue.advance(first.id, state)
    assert queue.claim_next() is None
    assert queue.retry_upload(first.id).state is JobState.UPLOADING
    queue.advance(first.id, JobState.COMPLETED)
    assert queue.claim_next() is not None


def test_review_selection_can_be_corrected_and_resumes_original_stage(database):
    queue = JobQueue(database)
    job = enqueue(queue, "language-review")
    queue.claim_next()
    queue.advance(job.id, JobState.READY)
    review = queue.needs_review(job.id, message="language needs an override")
    assert review.resume_state is JobState.READY

    corrected = database.set_selection(
        job.id,
        {"tracks": [{"stream_id": "audio:4352", "language": "hun"}]},
    )

    assert corrected.state is JobState.READY
    assert corrected.resume_state is None
    assert corrected.selection["tracks"][0]["language"] == "hun"


def test_scan_selection_artifacts_events_and_reopen(database):
    queue = JobQueue(database)
    job = enqueue(queue, "branching")
    queue.claim_next()
    scan = database.create_scan(ScanCreate(job_id=job.id))
    database.update_scan(
        scan.id,
        ScanUpdate(
            status=ScanState.AWAITING_SELECTION,
            result={"playlists": [{"id": "00800.mpls"}, {"id": "00801.mpls"}]},
        ),
    )
    assert database.get_job(job.id).state is JobState.AWAITING_SELECTION

    ready = database.set_selection(
        job.id, {"playlist": "00800.mpls", "audio_tracks": [1, 2]}
    )
    assert ready.state is JobState.READY
    assert ready.selection["playlist"] == "00800.mpls"

    artifact = database.create_artifact(
        ArtifactCreate(
            job_id=job.id,
            scan_id=scan.id,
            kind=ArtifactKind.LOG,
            name="encode.log",
            path="/home/accofil/encode/jobs/id/logs/encode.log",
            sha256="a" * 64,
            size_bytes=12,
        )
    )
    assert database.get_artifact(artifact.id).sha256 == "a" * 64
    updated = database.create_artifact(
        ArtifactCreate(
            job_id=job.id,
            scan_id=scan.id,
            kind=ArtifactKind.LOG,
            name="encode.log",
            path="/home/accofil/encode/jobs/id/logs/encode.log",
            sha256="b" * 64,
            size_bytes=24,
        )
    )
    assert updated.id == artifact.id
    assert updated.sha256 == "b" * 64
    assert len(database.list_artifacts(job_id=job.id)) == 1
    assert any(
        event.kind == "artifact.created"
        for event in database.list_events(job_id=job.id)
    )

    reopened = Database(database.path)
    assert reopened.get_job(job.id).selection["playlist"] == "00800.mpls"
    assert reopened.schema_version() == 1


def test_in_memory_database_stays_available_across_short_connections():
    database = Database(":memory:")
    queue = JobQueue(database)
    job = enqueue(queue, "memory")
    assert database.get_job(job.id).state is JobState.QUEUED
    database.close()
