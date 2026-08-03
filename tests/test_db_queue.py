from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from bdencode.db import Database, QueueBlockedError, StateConflictError
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


def fail_at_muxing(queue: JobQueue, name: str):
    job = enqueue(queue, name)
    claimed = queue.claim_next()
    assert claimed is not None and claimed.id == job.id
    for target in (JobState.READY, JobState.ENCODING, JobState.MUXING):
        queue.advance(job.id, target)
    return queue.fail(job.id, message=f"{name} mux failed")


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


def test_failed_retry_restores_marker_guarded_stage_and_audits(database):
    queue = JobQueue(database)
    failed = fail_at_muxing(queue, "retry-success")

    assert failed.state is JobState.FAILED
    assert failed.resume_state is JobState.MUXING
    assert failed.error == "retry-success mux failed"
    assert failed.finished_at is not None
    failure_event = database.list_events(job_id=failed.id)[-1]

    with pytest.raises(StateConflictError, match="expected"):
        queue.retry_failed(failed.id, expected_version=failed.version + 1)
    assert database.get_job(failed.id).version == failed.version

    retried = queue.retry_failed(
        failed.id,
        message="operator requested safe mux retry",
        expected_version=failed.version,
    )

    assert retried.state is JobState.MUXING
    assert retried.resume_state is None
    assert retried.error is None
    assert retried.status_message == "operator requested safe mux retry"
    assert retried.progress is None
    assert retried.finished_at is None
    assert retried.version == failed.version + 1
    retry_event = database.list_events(job_id=failed.id)[-1]
    assert retry_event.kind == "job.retry"
    assert retry_event.state_from is JobState.FAILED
    assert retry_event.state_to is JobState.MUXING
    assert retry_event.payload == {
        "failure_event_id": failure_event.id,
        "retry_stage": "MUXING",
        "previous_version": failed.version,
        "new_version": retried.version,
    }


def test_failed_retry_supports_legacy_event_provenance_without_resume_state(database):
    queue = JobQueue(database)
    failed = fail_at_muxing(queue, "legacy-retry")
    with database._write() as connection:
        connection.execute(
            "UPDATE jobs SET resume_state = NULL WHERE id = ?", (failed.id,)
        )

    retried = queue.retry_failed(failed.id)

    assert retried.state is JobState.MUXING
    assert retried.error is None


@pytest.mark.parametrize(
    "retry_stage",
    (
        JobState.READY,
        JobState.ENCODING,
        JobState.MUXING,
        JobState.QC,
        JobState.COMPARISON,
    ),
)
def test_failed_retry_allowlist_matches_replayable_worker_stages(
    database, retry_stage: JobState
):
    queue = JobQueue(database)
    job = enqueue(queue, f"retry-{retry_stage.value.lower()}")
    claimed = queue.claim_next()
    assert claimed is not None
    for stage in (
        JobState.READY,
        JobState.ENCODING,
        JobState.MUXING,
        JobState.QC,
        JobState.COMPARISON,
        JobState.UPLOADING,
    ):
        if retry_stage is JobState.SCANNING:
            break
        queue.advance(job.id, stage)
        if stage is retry_stage:
            break
    failed = queue.fail(job.id, message="stage failed")

    retried = queue.retry_failed(job.id)

    assert failed.resume_state is retry_stage
    assert retried.state is retry_stage


def test_failed_retry_rejects_missing_mismatched_or_unsafe_provenance(database):
    queue = JobQueue(database)
    missing = fail_at_muxing(queue, "missing-provenance")
    with database._write() as connection:
        connection.execute(
            "DELETE FROM events WHERE job_id = ? AND state_to = ?",
            (missing.id, JobState.FAILED.value),
        )
    with pytest.raises(StateConflictError, match="provenance"):
        queue.retry_failed(missing.id)
    assert database.get_job(missing.id).version == missing.version

    mismatched = fail_at_muxing(queue, "mismatched-provenance")
    with database._write() as connection:
        connection.execute(
            "UPDATE jobs SET resume_state = ? WHERE id = ?",
            (JobState.QC.value, mismatched.id),
        )
    with pytest.raises(StateConflictError, match="does not match"):
        queue.retry_failed(mismatched.id)
    assert database.get_job(mismatched.id).state is JobState.FAILED

    unsafe = enqueue(queue, "unsafe-scan")
    queue.claim_next()
    unsafe = queue.fail(unsafe.id, message="scan failed")
    assert unsafe.resume_state is JobState.SCANNING
    with pytest.raises(StateConflictError, match="not safely retryable"):
        queue.retry_failed(unsafe.id)


def test_failed_retry_rejects_non_failed_terminal_jobs(database):
    queue = JobQueue(database)
    completed = enqueue(queue, "completed")
    queue.claim_next()
    finish(queue, completed.id)
    cancelled = enqueue(queue, "cancelled")
    queue.claim_next()
    queue.cancel(cancelled.id)

    for job_id, state in (
        (completed.id, JobState.COMPLETED),
        (cancelled.id, JobState.CANCELLED),
    ):
        with pytest.raises(StateConflictError, match="only FAILED") as error:
            queue.retry_failed(job_id)
        assert error.value.current is state


def test_concurrent_failed_retries_preserve_single_active_guard(database):
    queue = JobQueue(database)
    failed_jobs = (
        fail_at_muxing(queue, "concurrent-one"),
        fail_at_muxing(queue, "concurrent-two"),
    )
    retry_databases = (Database(database.path), Database(database.path))
    for retry_database in retry_databases:
        retry_database.initialize()
    barrier = Barrier(2)

    def retry(index: int):
        barrier.wait()
        try:
            job = JobQueue(retry_databases[index]).retry_failed(
                failed_jobs[index].id
            )
            return "retried", job.id
        except QueueBlockedError as exc:
            assert exc.active_job is not None
            return "blocked", exc.active_job.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(retry, range(2)))

    retried = [job_id for outcome, job_id in results if outcome == "retried"]
    blocked = [job_id for outcome, job_id in results if outcome == "blocked"]
    assert len(retried) == len(blocked) == 1
    assert blocked == retried
    assert database.active_job() is not None
    assert database.active_job().id == retried[0]
    loser = next(job for job in failed_jobs if job.id != retried[0])
    unchanged = database.get_job(loser.id)
    assert unchanged.state is JobState.FAILED
    assert unchanged.version == loser.version
