"""Small, process-safe SQLite persistence layer for bdencode.

There is intentionally no ORM here.  Every mutating operation uses
``BEGIN IMMEDIATE`` and the database also has a partial unique index for the
single-active-job invariant, so separate API and worker processes cannot race
past the queue guard.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence
from uuid import uuid4

from .models import (
    BLOCKING_STATES,
    RETRYABLE_FAILED_STAGES,
    TERMINAL_STATES,
    Artifact,
    ArtifactCreate,
    Event,
    EventCreate,
    Job,
    JobCreate,
    JobState,
    Scan,
    ScanCreate,
    ScanState,
    ScanUpdate,
    validate_transition,
)


SCHEMA_VERSION = 1


class PersistenceError(RuntimeError):
    """Base class for persistence/domain conflicts."""


class NotFoundError(PersistenceError):
    pass


class StateConflictError(PersistenceError):
    def __init__(self, message: str, *, current: JobState | None = None) -> None:
        super().__init__(message)
        self.current = current


class QueueBlockedError(PersistenceError):
    def __init__(self, active_job: Job | None = None) -> None:
        message = "another job owns the active pipeline"
        if active_job is not None:
            message += f" ({active_job.id}, {active_job.state.value})"
        super().__init__(message)
        self.active_job = active_job


def utc_now() -> str:
    # SQLite stores RFC3339 text because it remains readable and sortable.
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="microseconds")


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_load(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


class Database:
    """Injectable database handle; connections are short-lived and fork-safe."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 30_000) -> None:
        self.path = str(path)
        self._connection_target = self.path
        self._connection_is_uri = False
        self._keeper: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self._connection_target = (
                f"file:bdencode-{uuid4().hex}?mode=memory&cache=shared"
            )
            self._connection_is_uri = True
        self.busy_timeout_ms = busy_timeout_ms
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @property
    def display_path(self) -> str:
        return self.path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._connection_target,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            uri=self._connection_is_uri,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        return connection

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            if self.path != ":memory:":
                Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                blocking = ",".join(f"'{state.value}'" for state in BLOCKING_STATES)
                job_states = ",".join(f"'{state.value}'" for state in JobState)
                scan_states = ",".join(f"'{state.value}'" for state in ScanState)
                connection.executescript(
                    f"""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        source_path TEXT NOT NULL,
                        work_path TEXT,
                        output_path TEXT,
                        disc_type TEXT NOT NULL,
                        content_type TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ({job_states})),
                        priority INTEGER NOT NULL DEFAULT 0,
                        settings_json TEXT NOT NULL DEFAULT '{{}}',
                        selection_json TEXT,
                        requested_by TEXT,
                        progress REAL CHECK (progress IS NULL OR (progress >= 0 AND progress <= 1)),
                        status_message TEXT,
                        error TEXT,
                        resume_state TEXT CHECK (resume_state IS NULL OR resume_state IN ({job_states})),
                        version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS ix_jobs_queue
                        ON jobs (priority DESC, created_at ASC)
                        WHERE state = 'QUEUED';
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_single_active
                        ON jobs ((1)) WHERE state IN ({blocking});

                    CREATE TABLE IF NOT EXISTS scans (
                        id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        source_path TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ({scan_states})),
                        result_json TEXT NOT NULL DEFAULT '{{}}',
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS ix_scans_job ON scans (job_id, created_at);

                    CREATE TABLE IF NOT EXISTS artifacts (
                        id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        scan_id TEXT REFERENCES scans(id) ON DELETE SET NULL,
                        kind TEXT NOT NULL,
                        name TEXT NOT NULL,
                        path TEXT NOT NULL,
                        mime_type TEXT,
                        sha256 TEXT,
                        size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
                        metadata_json TEXT NOT NULL DEFAULT '{{}}',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS ix_artifacts_job
                        ON artifacts (job_id, created_at);

                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE,
                        scan_id TEXT REFERENCES scans(id) ON DELETE SET NULL,
                        kind TEXT NOT NULL,
                        state_from TEXT,
                        state_to TEXT,
                        message TEXT,
                        payload_json TEXT NOT NULL DEFAULT '{{}}',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS ix_events_job_id ON events (job_id, id);
                    INSERT INTO schema_meta(key, value) VALUES ('schema_version', '{SCHEMA_VERSION}')
                        ON CONFLICT(key) DO NOTHING;
                    COMMIT;
                    """
                )
                version_row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                actual_version = int(version_row["value"]) if version_row else 0
                if actual_version != SCHEMA_VERSION:
                    raise PersistenceError(
                        f"unsupported database schema {actual_version}; expected {SCHEMA_VERSION}"
                    )
                self._initialized = True
                if self.path == ":memory:":
                    # A named shared-memory database lives until its final
                    # connection closes.  Keep one idle handle for this object.
                    self._keeper = connection
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                if connection is not self._keeper:
                    connection.close()

    def close(self) -> None:
        keeper, self._keeper = self._keeper, None
        if keeper is not None:
            keeper.close()

    def schema_version(self) -> int:
        with self._read() as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
        return int(row["value"]) if row else 0

    # -- Jobs -----------------------------------------------------------------

    def create_job(self, request: JobCreate) -> Job:
        now = utc_now()
        job_id = str(uuid4())
        source_leaf = (
            request.source_path.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
        )
        name = request.name or source_leaf or job_id
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, name, source_path, work_path, output_path, disc_type,
                    content_type, state, priority, settings_json, requested_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    name,
                    request.source_path,
                    request.work_path,
                    request.output_path,
                    request.disc_type.value,
                    request.content_type.value,
                    JobState.QUEUED.value,
                    request.priority,
                    _json_dump(request.settings),
                    request.requested_by,
                    now,
                    now,
                ),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                kind="job.created",
                state_to=JobState.QUEUED,
                payload={"priority": request.priority},
            )
            row = self._job_row(connection, job_id)
        return self._decode_job(row)

    def get_job(self, job_id: str) -> Job:
        with self._read() as connection:
            row = self._job_row(connection, job_id)
        return self._decode_job(row)

    def list_jobs(
        self,
        *,
        states: Sequence[JobState] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Job]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            parameters.extend(state.value for state in states)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, offset))
        with self._read() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs {where}
                ORDER BY
                    CASE WHEN state = 'QUEUED' THEN 0 ELSE 1 END,
                    priority DESC, created_at ASC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [self._decode_job(row) for row in rows]

    def count_jobs(self, *, states: Sequence[JobState] | None = None) -> int:
        if states:
            placeholders = ",".join("?" for _ in states)
            query = (
                f"SELECT COUNT(*) AS count FROM jobs WHERE state IN ({placeholders})"
            )
            parameters: Sequence[Any] = [state.value for state in states]
        else:
            query = "SELECT COUNT(*) AS count FROM jobs"
            parameters = []
        with self._read() as connection:
            row = connection.execute(query, parameters).fetchone()
        return int(row["count"])

    def active_job(self) -> Job | None:
        placeholders = ",".join("?" for _ in BLOCKING_STATES)
        with self._read() as connection:
            row = connection.execute(
                f"SELECT * FROM jobs WHERE state IN ({placeholders}) LIMIT 1",
                [state.value for state in BLOCKING_STATES],
            ).fetchone()
        return self._decode_job(row) if row else None

    def claim_next_job(self) -> Job | None:
        """Atomically claim the oldest highest-priority job for scanning."""

        try:
            with self._write() as connection:
                placeholders = ",".join("?" for _ in BLOCKING_STATES)
                active = connection.execute(
                    f"SELECT id FROM jobs WHERE state IN ({placeholders}) LIMIT 1",
                    [state.value for state in BLOCKING_STATES],
                ).fetchone()
                if active:
                    return None
                row = connection.execute(
                    """
                    SELECT * FROM jobs WHERE state = 'QUEUED'
                    ORDER BY priority DESC, created_at ASC LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return None
                return self._transition_in_connection(
                    connection,
                    row,
                    JobState.SCANNING,
                    message="claimed by worker",
                    details={},
                )
        except sqlite3.IntegrityError as exc:
            raise QueueBlockedError(self.active_job()) from exc

    def transition_job(
        self,
        job_id: str,
        target: JobState,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> Job:
        try:
            with self._write() as connection:
                row = self._job_row(connection, job_id)
                if expected_version is not None and row["version"] != expected_version:
                    raise StateConflictError(
                        f"job version is {row['version']}, expected {expected_version}",
                        current=JobState(row["state"]),
                    )
                return self._transition_in_connection(
                    connection,
                    row,
                    target,
                    message=message,
                    details=details or {},
                )
        except sqlite3.IntegrityError as exc:
            raise QueueBlockedError(self.active_job()) from exc

    def retry_failed_job(
        self,
        job_id: str,
        *,
        message: str | None = None,
        expected_version: int | None = None,
    ) -> Job:
        """Transactionally restore a safely replayable FAILED worker stage.

        FAILED remains terminal in the general state machine.  This dedicated
        operation requires durable transition provenance and re-enters only a
        narrow set of marker-guarded stages while the single-active invariant
        is held by the same SQLite write transaction.
        """

        try:
            with self._write() as connection:
                row = self._job_row(connection, job_id)
                current = JobState(row["state"])
                if current is not JobState.FAILED:
                    raise StateConflictError(
                        "only FAILED jobs can retry a worker stage",
                        current=current,
                    )
                if expected_version is not None and row["version"] != expected_version:
                    raise StateConflictError(
                        f"job version is {row['version']}, expected {expected_version}",
                        current=current,
                    )

                failure_event = connection.execute(
                    """
                    SELECT id, kind, state_from, state_to
                    FROM events
                    WHERE job_id = ?
                      AND (state_from IS NOT NULL OR state_to IS NOT NULL)
                    ORDER BY id DESC LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
                if (
                    failure_event is None
                    or failure_event["kind"] != "job.state"
                    or failure_event["state_to"] != JobState.FAILED.value
                    or failure_event["state_from"] is None
                ):
                    raise StateConflictError(
                        "FAILED job has no valid latest failure transition provenance",
                        current=current,
                    )
                try:
                    retry_stage = JobState(failure_event["state_from"])
                except ValueError as exc:
                    raise StateConflictError(
                        "FAILED job has invalid failure-stage provenance",
                        current=current,
                    ) from exc

                stored_resume = row["resume_state"]
                if stored_resume is not None:
                    try:
                        resume_stage = JobState(stored_resume)
                    except ValueError as exc:
                        raise StateConflictError(
                            "FAILED job has invalid stored retry provenance",
                            current=current,
                        ) from exc
                    if resume_stage is not retry_stage:
                        raise StateConflictError(
                            "FAILED job retry provenance does not match its failure event",
                            current=current,
                        )
                if retry_stage not in RETRYABLE_FAILED_STAGES:
                    raise StateConflictError(
                        f"FAILED stage {retry_stage.value} is not safely retryable",
                        current=current,
                    )

                placeholders = ",".join("?" for _ in BLOCKING_STATES)
                blocker = connection.execute(
                    f"""
                    SELECT * FROM jobs
                    WHERE id != ? AND state IN ({placeholders})
                    LIMIT 1
                    """,
                    (job_id, *(state.value for state in BLOCKING_STATES)),
                ).fetchone()
                if blocker is not None:
                    raise QueueBlockedError(self._decode_job(blocker))

                now = utc_now()
                retry_message = message or f"retrying failed {retry_stage.value} stage"
                cursor = connection.execute(
                    """
                    UPDATE jobs SET state = ?, status_message = ?, error = NULL,
                        resume_state = NULL, progress = NULL, finished_at = NULL,
                        updated_at = ?, version = version + 1
                    WHERE id = ? AND state = ? AND version = ?
                    """,
                    (
                        retry_stage.value,
                        retry_message,
                        now,
                        job_id,
                        JobState.FAILED.value,
                        row["version"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise StateConflictError(
                        "job changed concurrently", current=current
                    )
                self._insert_event(
                    connection,
                    job_id=job_id,
                    kind="job.retry",
                    state_from=JobState.FAILED,
                    state_to=retry_stage,
                    message=retry_message,
                    payload={
                        "failure_event_id": int(failure_event["id"]),
                        "retry_stage": retry_stage.value,
                        "previous_version": int(row["version"]),
                        "new_version": int(row["version"]) + 1,
                    },
                )
                return self._decode_job(self._job_row(connection, job_id))
        except sqlite3.IntegrityError as exc:
            if (
                getattr(exc, "sqlite_errorcode", None)
                == sqlite3.SQLITE_CONSTRAINT_UNIQUE
            ):
                blocker = self.active_job()
                if blocker is not None:
                    raise QueueBlockedError(blocker) from exc
            raise

    def set_selection(
        self,
        job_id: str,
        selection: dict[str, Any],
        *,
        message: str | None = None,
        expected_version: int | None = None,
    ) -> Job:
        with self._write() as connection:
            row = self._job_row(connection, job_id)
            current = JobState(row["state"])
            if current not in {JobState.AWAITING_SELECTION, JobState.NEEDS_REVIEW}:
                raise StateConflictError(
                    "selection is only accepted while awaiting selection or review",
                    current=current,
                )
            if expected_version is not None and row["version"] != expected_version:
                raise StateConflictError(
                    f"job version is {row['version']}, expected {expected_version}",
                    current=current,
                )
            connection.execute(
                "UPDATE jobs SET selection_json = ?, updated_at = ? WHERE id = ?",
                (_json_dump(selection), utc_now(), job_id),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                kind="job.selection",
                message=message,
                payload={"selection": selection},
            )
            scan_row = connection.execute(
                """
                SELECT * FROM scans
                WHERE job_id = ? AND status = 'AWAITING_SELECTION'
                ORDER BY created_at DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            if scan_row is not None:
                completed_at = utc_now()
                connection.execute(
                    """
                    UPDATE scans SET status = 'COMPLETED', completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (completed_at, completed_at, scan_row["id"]),
                )
                self._insert_event(
                    connection,
                    job_id=job_id,
                    scan_id=scan_row["id"],
                    kind="scan.state",
                    message="selection accepted",
                    payload={
                        "from": ScanState.AWAITING_SELECTION.value,
                        "to": ScanState.COMPLETED.value,
                    },
                )
            row = self._job_row(connection, job_id)
            return self._transition_in_connection(
                connection,
                row,
                JobState.READY,
                message=message
                or (
                    "selection revised after review"
                    if current is JobState.NEEDS_REVIEW
                    else "playlist and tracks selected"
                ),
                details={},
            )

    def record_progress(
        self,
        job_id: str,
        progress: float,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> Job:
        if not 0 <= progress <= 1:
            raise ValueError("progress must be between 0 and 1")
        with self._write() as connection:
            row = self._job_row(connection, job_id)
            current = JobState(row["state"])
            if current in TERMINAL_STATES:
                raise StateConflictError(
                    "terminal jobs cannot report progress", current=current
                )
            now = utc_now()
            connection.execute(
                """
                UPDATE jobs SET progress = ?, status_message = ?, updated_at = ?,
                    version = version + 1 WHERE id = ?
                """,
                (progress, message, now, job_id),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                kind="job.progress",
                message=message,
                payload={"progress": progress, **(details or {})},
            )
            return self._decode_job(self._job_row(connection, job_id))

    def _transition_in_connection(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        target: JobState,
        *,
        message: str | None,
        details: dict[str, Any],
    ) -> Job:
        current = JobState(row["state"])
        resume = JobState(row["resume_state"]) if row["resume_state"] else None
        try:
            validate_transition(current, target, resume_state=resume)
        except ValueError as exc:
            raise StateConflictError(str(exc), current=current) from exc

        now = utc_now()
        started_at = row["started_at"]
        finished_at = row["finished_at"]
        progress = row["progress"]
        error = row["error"]
        next_resume: str | None = row["resume_state"]
        if current is JobState.QUEUED and target is JobState.SCANNING:
            started_at = now
            progress = 0.0
        if target is JobState.NEEDS_REVIEW:
            next_resume = current.value
        elif current is JobState.NEEDS_REVIEW:
            next_resume = None
        if target in TERMINAL_STATES:
            finished_at = now
            next_resume = None
            if target is JobState.COMPLETED:
                progress = 1.0
        if target is JobState.FAILED:
            error = message or "job failed"
            next_resume = current.value

        cursor = connection.execute(
            """
            UPDATE jobs SET state = ?, status_message = ?, error = ?, resume_state = ?,
                progress = ?, started_at = ?, finished_at = ?, updated_at = ?,
                version = version + 1
            WHERE id = ? AND version = ?
            """,
            (
                target.value,
                message,
                error,
                next_resume,
                progress,
                started_at,
                finished_at,
                now,
                row["id"],
                row["version"],
            ),
        )
        if cursor.rowcount != 1:
            raise StateConflictError("job changed concurrently", current=current)
        self._insert_event(
            connection,
            job_id=row["id"],
            kind="job.state",
            state_from=current,
            state_to=target,
            message=message,
            payload=details,
        )
        return self._decode_job(self._job_row(connection, row["id"]))

    @staticmethod
    def _job_row(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"job not found: {job_id}")
        return row

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> Job:
        values = dict(row)
        values.pop("settings_json", None)
        values.pop("selection_json", None)
        return Job.model_validate(
            {
                **values,
                "settings": _json_load(row["settings_json"], {}),
                "selection": _json_load(row["selection_json"], None),
            }
        )

    # -- Scans ----------------------------------------------------------------

    def create_scan(self, request: ScanCreate) -> Scan:
        scan_id = str(uuid4())
        now = utc_now()
        with self._write() as connection:
            job_row = self._job_row(connection, request.job_id)
            job_state = JobState(job_row["state"])
            if job_state is not JobState.SCANNING:
                raise StateConflictError(
                    "scans may only be created while the job is SCANNING",
                    current=job_state,
                )
            if request.status not in {ScanState.PENDING, ScanState.RUNNING}:
                raise StateConflictError("new scans must be PENDING or RUNNING")
            source_path = request.source_path or job_row["source_path"]
            connection.execute(
                """
                INSERT INTO scans (
                    id, job_id, source_path, status, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id,
                    request.job_id,
                    source_path,
                    request.status.value,
                    _json_dump(request.result),
                    now,
                    now,
                ),
            )
            self._insert_event(
                connection,
                job_id=request.job_id,
                scan_id=scan_id,
                kind="scan.created",
                payload={"status": request.status.value},
            )
            row = self._scan_row(connection, scan_id)
        return self._decode_scan(row)

    def get_scan(self, scan_id: str) -> Scan:
        with self._read() as connection:
            row = self._scan_row(connection, scan_id)
        return self._decode_scan(row)

    def list_scans(
        self,
        *,
        job_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Scan]:
        where = "WHERE job_id = ?" if job_id else ""
        parameters: list[Any] = [job_id] if job_id else []
        parameters.extend((limit, offset))
        with self._read() as connection:
            rows = connection.execute(
                f"SELECT * FROM scans {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
        return [self._decode_scan(row) for row in rows]

    def update_scan(self, scan_id: str, request: ScanUpdate) -> Scan:
        """Update a scan and move its job at the scan/selection boundary atomically."""

        allowed: dict[ScanState, set[ScanState]] = {
            ScanState.PENDING: {ScanState.RUNNING, ScanState.FAILED},
            ScanState.RUNNING: {
                ScanState.AWAITING_SELECTION,
                ScanState.COMPLETED,
                ScanState.FAILED,
            },
            ScanState.AWAITING_SELECTION: {ScanState.COMPLETED, ScanState.FAILED},
            ScanState.COMPLETED: set(),
            ScanState.FAILED: set(),
        }
        with self._write() as connection:
            row = self._scan_row(connection, scan_id)
            current = ScanState(row["status"])
            if request.status is not current and request.status not in allowed[current]:
                raise StateConflictError(
                    f"illegal scan transition {current.value} -> {request.status.value}"
                )
            result = (
                request.result
                if request.result is not None
                else _json_load(row["result_json"], {})
            )
            completed_at = (
                utc_now()
                if request.status in {ScanState.COMPLETED, ScanState.FAILED}
                else row["completed_at"]
            )
            now = utc_now()
            connection.execute(
                """
                UPDATE scans SET status = ?, result_json = ?, error = ?,
                    completed_at = ?, updated_at = ? WHERE id = ?
                """,
                (
                    request.status.value,
                    _json_dump(result),
                    request.error,
                    completed_at,
                    now,
                    scan_id,
                ),
            )
            self._insert_event(
                connection,
                job_id=row["job_id"],
                scan_id=scan_id,
                kind="scan.state",
                message=request.message,
                payload={"from": current.value, "to": request.status.value},
            )

            job_row = self._job_row(connection, row["job_id"])
            job_state = JobState(job_row["state"])
            if job_state is JobState.SCANNING:
                if request.status is ScanState.AWAITING_SELECTION:
                    self._transition_in_connection(
                        connection,
                        job_row,
                        JobState.AWAITING_SELECTION,
                        message=request.message
                        or "scan requires playlist/track selection",
                        details={"scan_id": scan_id},
                    )
                elif request.status is ScanState.COMPLETED:
                    self._transition_in_connection(
                        connection,
                        job_row,
                        JobState.READY,
                        message=request.message or "scan completed",
                        details={"scan_id": scan_id},
                    )
                elif request.status is ScanState.FAILED:
                    self._transition_in_connection(
                        connection,
                        job_row,
                        JobState.FAILED,
                        message=request.error or request.message or "scan failed",
                        details={"scan_id": scan_id},
                    )
            return self._decode_scan(self._scan_row(connection, scan_id))

    @staticmethod
    def _scan_row(connection: sqlite3.Connection, scan_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"scan not found: {scan_id}")
        return row

    @staticmethod
    def _decode_scan(row: sqlite3.Row) -> Scan:
        values = dict(row)
        values.pop("result_json", None)
        return Scan.model_validate(
            {**values, "result": _json_load(row["result_json"], {})}
        )

    # -- Artifacts ------------------------------------------------------------

    def create_artifact(self, request: ArtifactCreate) -> Artifact:
        now = utc_now()
        with self._write() as connection:
            self._job_row(connection, request.job_id)
            if request.scan_id is not None:
                scan = self._scan_row(connection, request.scan_id)
                if scan["job_id"] != request.job_id:
                    raise StateConflictError("artifact scan belongs to a different job")
            existing = connection.execute(
                """
                SELECT id FROM artifacts
                WHERE job_id = ? AND path = ?
                ORDER BY created_at DESC
                """,
                (request.job_id, request.path),
            ).fetchall()
            if existing:
                artifact_id = existing[0]["id"]
                connection.execute(
                    """
                    UPDATE artifacts SET scan_id = ?, kind = ?, name = ?,
                        mime_type = ?, sha256 = ?, size_bytes = ?,
                        metadata_json = ?, created_at = ?
                    WHERE id = ?
                    """,
                    (
                        request.scan_id,
                        request.kind.value,
                        request.name,
                        request.mime_type,
                        request.sha256.lower() if request.sha256 else None,
                        request.size_bytes,
                        _json_dump(request.metadata),
                        now,
                        artifact_id,
                    ),
                )
                stale_ids = [row["id"] for row in existing[1:]]
                if stale_ids:
                    connection.executemany(
                        "DELETE FROM artifacts WHERE id = ?",
                        ((item,) for item in stale_ids),
                    )
                event_kind = "artifact.updated"
            else:
                artifact_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO artifacts (
                        id, job_id, scan_id, kind, name, path, mime_type, sha256,
                        size_bytes, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        request.job_id,
                        request.scan_id,
                        request.kind.value,
                        request.name,
                        request.path,
                        request.mime_type,
                        request.sha256.lower() if request.sha256 else None,
                        request.size_bytes,
                        _json_dump(request.metadata),
                        now,
                    ),
                )
                event_kind = "artifact.created"
            self._insert_event(
                connection,
                job_id=request.job_id,
                scan_id=request.scan_id,
                kind=event_kind,
                payload={"artifact_id": artifact_id, "kind": request.kind.value},
            )
            row = self._artifact_row(connection, artifact_id)
        return self._decode_artifact(row)

    def get_artifact(self, artifact_id: str) -> Artifact:
        with self._read() as connection:
            row = self._artifact_row(connection, artifact_id)
        return self._decode_artifact(row)

    def list_artifacts(
        self,
        *,
        job_id: str | None = None,
        scan_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Artifact]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if job_id:
            clauses.append("job_id = ?")
            parameters.append(job_id)
        if scan_id:
            clauses.append("scan_id = ?")
            parameters.append(scan_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, offset))
        with self._read() as connection:
            rows = connection.execute(
                f"SELECT * FROM artifacts {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
        return [self._decode_artifact(row) for row in rows]

    @staticmethod
    def _artifact_row(connection: sqlite3.Connection, artifact_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"artifact not found: {artifact_id}")
        return row

    @staticmethod
    def _decode_artifact(row: sqlite3.Row) -> Artifact:
        values = dict(row)
        values.pop("metadata_json", None)
        return Artifact.model_validate(
            {**values, "metadata": _json_load(row["metadata_json"], {})}
        )

    # -- Events ---------------------------------------------------------------

    def add_event(self, request: EventCreate) -> Event:
        with self._write() as connection:
            event_job_id = request.job_id
            if request.job_id:
                self._job_row(connection, request.job_id)
            if request.scan_id:
                scan = self._scan_row(connection, request.scan_id)
                if request.job_id and scan["job_id"] != request.job_id:
                    raise StateConflictError("event scan belongs to a different job")
                event_job_id = event_job_id or scan["job_id"]
            event_id = self._insert_event(
                connection,
                job_id=event_job_id,
                scan_id=request.scan_id,
                kind=request.kind,
                message=request.message,
                payload=request.payload,
            )
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        return self._decode_event(row)

    def list_events(
        self,
        *,
        job_id: str | None = None,
        scan_id: str | None = None,
        after_id: int = 0,
        limit: int = 200,
    ) -> list[Event]:
        clauses = ["id > ?"]
        parameters: list[Any] = [after_id]
        if job_id:
            clauses.append("job_id = ?")
            parameters.append(job_id)
        if scan_id:
            clauses.append("scan_id = ?")
            parameters.append(scan_id)
        parameters.append(limit)
        with self._read() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM events WHERE {" AND ".join(clauses)}
                ORDER BY id ASC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._decode_event(row) for row in rows]

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        kind: str,
        job_id: str | None = None,
        scan_id: str | None = None,
        state_from: JobState | None = None,
        state_to: JobState | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO events (
                job_id, scan_id, kind, state_from, state_to, message,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                scan_id,
                kind,
                state_from.value if state_from else None,
                state_to.value if state_to else None,
                message,
                _json_dump(payload or {}),
                utc_now(),
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _decode_event(row: sqlite3.Row) -> Event:
        values = dict(row)
        values.pop("payload_json", None)
        return Event.model_validate(
            {**values, "payload": _json_load(row["payload_json"], {})}
        )
