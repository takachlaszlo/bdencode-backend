from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import sys
import threading
import time

import pytest

from bdencode.config import Settings
from bdencode.db import Database, QueueBlockedError, StateConflictError
from bdencode.models import JobControlState, JobCreate, JobOperation, JobState
from bdencode.process import CommandRunner, ProcessInterrupted
from bdencode.queue import JobQueue
from bdencode.worker import JobPaths, PipelineWorker


@pytest.fixture
def database(tmp_path):
    value = Database(tmp_path / "control.sqlite3")
    value.initialize()
    return value


def enqueue(queue: JobQueue, name: str):
    return queue.enqueue(JobCreate(source_path=f"/storage/{name}", name=name))


def test_progress_version_churn_does_not_conflict_with_control_revision(database):
    queue = JobQueue(database)
    job = enqueue(queue, "progress-race")
    original_control_revision = job.control_revision

    progressed = database.record_progress(job.id, 0.1, message="observed")
    assert progressed.version > job.version
    assert progressed.control_revision == original_control_revision

    paused = queue.pause(
        job.id, expected_control_revision=original_control_revision
    )
    assert paused.state is JobState.QUEUED
    assert paused.control_state is JobControlState.PAUSED
    assert paused.control_revision == original_control_revision + 1
    assert JobOperation.RESUME in paused.allowed_operations
    assert queue.claim_next() is None


def test_active_pause_is_request_then_ack_and_resume_preserves_stage(database):
    queue = JobQueue(database)
    job = enqueue(queue, "durable-stage")
    active = queue.claim_next()
    assert active is not None and active.id == job.id

    requested = queue.request_pause(
        job.id, expected_control_revision=active.control_revision
    )
    assert requested.state is JobState.SCANNING
    assert requested.control_state is JobControlState.PAUSE_REQUESTED
    assert database.preparing_job() is not None

    paused = queue.acknowledge_pause(
        job.id, expected_control_revision=requested.control_revision
    )
    assert paused.state is JobState.SCANNING
    assert paused.control_state is JobControlState.PAUSED
    assert database.preparing_job() is None

    resumed = queue.resume(
        job.id, expected_control_revision=paused.control_revision
    )
    assert resumed.state is JobState.SCANNING
    assert resumed.progress == paused.progress
    assert resumed.control_state is JobControlState.RUNNING
    assert database.preparing_job().id == job.id


def test_paused_lane_resume_is_transactionally_blocked(database):
    queue = JobQueue(database)
    first = enqueue(queue, "first")
    second = enqueue(queue, "second")
    first_active = queue.claim_next()
    assert first_active is not None and first_active.id == first.id
    requested = queue.pause(first.id)
    paused = queue.acknowledge_pause(
        first.id, expected_control_revision=requested.control_revision
    )
    second_active = queue.claim_next()
    assert second_active is not None and second_active.id == second.id

    with pytest.raises(QueueBlockedError) as raised:
        queue.resume(
            first.id, expected_control_revision=paused.control_revision
        )
    assert raised.value.active_job is not None
    assert raised.value.active_job.id == second.id
    assert database.get_job(first.id).control_state is JobControlState.PAUSED


def test_pause_can_be_atomically_escalated_to_cancel(database):
    queue = JobQueue(database)
    job = enqueue(queue, "escalate")
    active = queue.claim_next()
    assert active is not None
    pause_request = queue.pause(job.id)
    cancel_request = queue.request_cancel(
        job.id, expected_control_revision=pause_request.control_revision
    )
    assert cancel_request.state is JobState.SCANNING
    assert cancel_request.control_state is JobControlState.CANCEL_REQUESTED

    with pytest.raises(StateConflictError, match="control revision"):
        queue.acknowledge_pause(
            job.id, expected_control_revision=pause_request.control_revision
        )
    cancelled = queue.acknowledge_cancel(
        job.id, expected_control_revision=cancel_request.control_revision
    )
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.control_state is JobControlState.RUNNING
    assert cancelled.control_requested_at is None


def test_concurrent_control_writers_have_one_revision_winner(database):
    queue = JobQueue(database)
    job = enqueue(queue, "concurrent")
    barrier = threading.Barrier(2)

    def request(action: str):
        barrier.wait()
        if action == "pause":
            return queue.pause(
                job.id, expected_control_revision=job.control_revision
            )
        return queue.request_cancel(
            job.id, expected_control_revision=job.control_revision
        )

    outcomes: list[object] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request, action) for action in ("pause", "cancel")]
        for future in futures:
            try:
                outcomes.append(future.result())
            except StateConflictError as exc:
                outcomes.append(exc)
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, StateConflictError) for item in outcomes) == 1


def _create_schema_one_fixture(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('schema_version', '1');
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, source_path TEXT NOT NULL,
                work_path TEXT, output_path TEXT, disc_type TEXT NOT NULL,
                content_type TEXT NOT NULL, state TEXT NOT NULL, priority INTEGER NOT NULL,
                settings_json TEXT NOT NULL, selection_json TEXT, requested_by TEXT,
                progress REAL, status_message TEXT, error TEXT, resume_state TEXT,
                version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT
            );
            INSERT INTO jobs VALUES (
                'legacy', 'legacy', '/storage/legacy', NULL, NULL, 'AUTO', 'FILM',
                'ENCODING', 0, '{}', '{}', NULL, 0.4, 'encoding', NULL, NULL,
                7, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00', NULL
            );
            """
        )


def test_schema_one_without_control_columns_migrates_in_place(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    _create_schema_one_fixture(path)

    migrated = Database(path)
    migrated.initialize()
    job = migrated.get_job("legacy")
    assert migrated.schema_version() == 2
    assert job.state is JobState.ENCODING
    assert job.control_state is JobControlState.RUNNING
    assert job.control_revision == 1
    assert job.version == 7


def test_parallel_initializers_migrate_schema_one_atomically(tmp_path):
    path = tmp_path / "parallel-legacy.sqlite3"
    _create_schema_one_fixture(path)
    initializer_count = 64
    start = threading.Barrier(initializer_count)

    def initialize(_: int) -> int:
        database = Database(path, busy_timeout_ms=25)
        start.wait(timeout=20)
        database.initialize()
        version = database.schema_version()
        database.close()
        return version

    with ThreadPoolExecutor(max_workers=initializer_count) as pool:
        versions = tuple(pool.map(initialize, range(initializer_count)))

    assert versions == (2,) * initializer_count
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        columns = [
            row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
        ]
        objects = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table', 'index')"
            )
        }
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]

    assert version == "2"
    assert integrity == "ok"
    for column in (
        "control_state",
        "control_revision",
        "control_requested_at",
        "control_message",
    ):
        assert columns.count(column) == 1
    assert {
        "ux_jobs_single_scanning",
        "ux_jobs_single_encoding",
        "release_preparations",
        "release_preparation_events",
    } <= objects


def test_initializer_lock_budget_is_independent_of_runtime_timeout(
    tmp_path, monkeypatch
):
    observed_timeouts = []
    original_connect = Database._connect

    def tracked_connect(database, *, busy_timeout_ms=None):
        observed_timeouts.append(busy_timeout_ms)
        return original_connect(database, busy_timeout_ms=busy_timeout_ms)

    monkeypatch.setattr(Database, "_connect", tracked_connect)
    database = Database(tmp_path / "dedicated-init-timeout.sqlite3", busy_timeout_ms=25)

    database.initialize()

    assert observed_timeouts == [500]


def test_single_command_interrupt_terminates_before_return(tmp_path):
    started = threading.Event()
    interrupt = threading.Event()
    output = tmp_path / "never.txt"
    command = [
        sys.executable,
        "-c",
        (
            "import pathlib,time; "
            f"pathlib.Path({str(output)!r}).write_text('started'); "
            "time.sleep(30)"
        ),
    ]
    runner = CommandRunner(
        interrupt_requested=interrupt.is_set,
        terminate_grace_seconds=0.2,
    )

    def execute():
        started.set()
        return runner.run(command, poll_interval=0.02)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute)
        assert started.wait(timeout=2)
        deadline = time.monotonic() + 2
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        interrupt.set()
        with pytest.raises(ProcessInterrupted):
            future.result(timeout=5)


def test_interrupted_partial_cleanup_never_traverses_workspace_links(tmp_path):
    source_root = tmp_path / "storage"
    source_root.mkdir()
    settings = Settings(
        data_root=tmp_path / "encode",
        source_roots=(source_root,),
    ).validate()
    settings.create_directories()
    paths = JobPaths.create(settings, "partial-cleanup")

    owned = paths.work / "video-encoded.partial.mkv"
    owned.write_bytes(b"partial")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "private.partial.mkv"
    sentinel.write_bytes(b"keep")
    linked = paths.work / "linked"
    try:
        linked.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    PipelineWorker._remove_interrupted_partials(paths)

    assert not owned.exists()
    assert sentinel.read_bytes() == b"keep"
    assert linked.is_symlink()


@pytest.mark.parametrize(
    ("request_name", "expected_state", "expected_control"),
    [
        ("pause", JobState.SCANNING, JobControlState.PAUSED),
        ("cancel", JobState.CANCELLED, JobControlState.RUNNING),
    ],
)
def test_restarted_worker_acknowledges_orphaned_request_before_stage(
    tmp_path, request_name, expected_state, expected_control
):
    source_root = tmp_path / "storage"
    source_root.mkdir()
    database = Database(tmp_path / "orphan.sqlite3")
    queue = JobQueue(database)
    job = queue.enqueue(
        JobCreate(source_path=str(source_root / "disc"), name="orphan")
    )
    active = queue.claim_next()
    assert active is not None
    requested = (
        queue.pause(job.id)
        if request_name == "pause"
        else queue.request_cancel(job.id)
    )

    # This is a fresh worker instance, as after a service crash/reboot.  It
    # must acknowledge the durable request without entering the scanner.
    worker = PipelineWorker(
        database,
        Settings(data_root=tmp_path / "encode", source_roots=(source_root,)),
        scanner_factory=lambda _settings: pytest.fail("scanner was started"),
    )
    result = worker.process_job(requested)
    assert result.state is expected_state
    assert result.control_state is expected_control


def test_worker_runner_observes_durable_pause_before_acknowledgement(tmp_path):
    source_root = tmp_path / "storage"
    source_root.mkdir()
    settings = Settings(data_root=tmp_path / "encode", source_roots=(source_root,))
    database = Database(tmp_path / "runner-control.sqlite3")
    queue = JobQueue(database)
    job = queue.enqueue(JobCreate(source_path=str(source_root), name="runner"))
    active = queue.claim_next()
    assert active is not None
    worker = PipelineWorker(database, settings)
    paths = JobPaths.create(settings.validate(), job.id)
    child_started = tmp_path / "child-started"
    command = [
        sys.executable,
        "-c",
        (
            "import pathlib,time; "
            f"pathlib.Path({str(child_started)!r}).write_text('yes'); "
            "time.sleep(30)"
        ),
    ]

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker._runner(paths).run, command, poll_interval=0.02)
        deadline = time.monotonic() + 3
        while not child_started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_started.exists()
        requested = queue.pause(job.id)
        assert requested.state is JobState.SCANNING
        assert requested.control_state is JobControlState.PAUSE_REQUESTED
        with pytest.raises(ProcessInterrupted):
            future.result(timeout=5)

    # The subprocess is gone, but only the worker boundary may acknowledge it.
    assert database.get_job(job.id).control_state is JobControlState.PAUSE_REQUESTED
    paused = worker.process_job(database.get_job(job.id))
    assert paused.state is JobState.SCANNING
    assert paused.control_state is JobControlState.PAUSED
