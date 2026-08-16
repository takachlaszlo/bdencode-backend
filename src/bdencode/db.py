"""Small, process-safe SQLite persistence layer for bdencode.

There is intentionally no ORM here.  Every mutating operation uses
``BEGIN IMMEDIATE`` and the database also has separate partial unique indexes
for the one scan lane and the one encode lane, so separate processes cannot
race past either queue guard.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import uuid4

from .models import (
    BLOCKING_STATES,
    PREPARATION_ACTIVE_STATES,
    RETRYABLE_FAILED_STAGES,
    TERMINAL_STATES,
    Artifact,
    ArtifactCreate,
    Event,
    EventCreate,
    Job,
    JobControlState,
    JobCreate,
    JobState,
    Scan,
    ScanCreate,
    ScanState,
    ScanUpdate,
    validate_transition,
)
from .progress import pipeline_progress_baseline


SCHEMA_VERSION = 2

_INITIALIZE_MAX_ATTEMPTS = 8
_INITIALIZE_BUSY_TIMEOUT_MS = 500
_INITIALIZE_BACKOFF_INITIAL_SECONDS = 0.01
_INITIALIZE_BACKOFF_MAX_SECONDS = 0.25


# Only these states can have an executing worker operation.  Pausing any other
# state is acknowledged in the API transaction because no process can still be
# mutating its workspace.
CONTROL_ACTIVE_STATES = frozenset(
    {
        JobState.SCANNING,
        JobState.ENCODING,
        JobState.MUXING,
        JobState.QC,
        JobState.COMPARISON,
        JobState.UPLOADING,
    }
)


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


def _is_sqlite_busy(error: sqlite3.OperationalError) -> bool:
    message = str(error).casefold()
    return "busy" in message or "locked" in message


def _execute_sql_statements(connection: sqlite3.Connection, script: str) -> None:
    """Execute a schema script without sqlite3.executescript's implicit commit."""

    pending = ""
    for line in script.splitlines():
        pending += f"{line}\n"
        if sqlite3.complete_statement(pending):
            connection.execute(pending)
            pending = ""
    if pending.strip():
        raise PersistenceError("incomplete database schema statement")


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

    def _connect(self, *, busy_timeout_ms: int | None = None) -> sqlite3.Connection:
        effective_timeout_ms = (
            self.busy_timeout_ms
            if busy_timeout_ms is None
            else max(0, int(busy_timeout_ms))
        )
        connection = sqlite3.connect(
            self._connection_target,
            timeout=effective_timeout_ms / 1000,
            isolation_level=None,
            uri=self._connection_is_uri,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {effective_timeout_ms}")
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
        """Initialize or migrate the database with bounded lock retries."""

        if self._initialized:
            return
        last_busy_error: sqlite3.OperationalError | None = None
        for attempt in range(_INITIALIZE_MAX_ATTEMPTS):
            try:
                self._initialize_once()
                return
            except sqlite3.OperationalError as error:
                if not _is_sqlite_busy(error):
                    raise
                last_busy_error = error
                if attempt + 1 == _INITIALIZE_MAX_ATTEMPTS:
                    break
                delay = min(
                    _INITIALIZE_BACKOFF_INITIAL_SECONDS * (2**attempt),
                    _INITIALIZE_BACKOFF_MAX_SECONDS,
                )
                time.sleep(delay)
        raise PersistenceError(
            "database initialization remained busy after bounded retries"
        ) from last_busy_error

    def _initialize_once(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            if self.path != ":memory:":
                Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect(
                busy_timeout_ms=min(
                    max(0, int(self.busy_timeout_ms)),
                    _INITIALIZE_BUSY_TIMEOUT_MS,
                )
            )
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                # The version and column inventory are deliberately read only
                # after acquiring the cross-process write lock.  Otherwise two
                # v1 initializers can both plan the same ALTER TABLE migration.
                connection.execute("BEGIN IMMEDIATE")
                existing_meta = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
                ).fetchone()
                existing_version: int | None = None
                if existing_meta is not None:
                    version_row = connection.execute(
                        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                    ).fetchone()
                    existing_version = int(version_row["value"]) if version_row else 0
                    if existing_version not in {1, SCHEMA_VERSION}:
                        raise PersistenceError(
                            f"unsupported database schema {existing_version}; "
                            f"expected 1 or {SCHEMA_VERSION}"
                        )
                # v1 -> v2 is deliberately a small, atomic column migration.
                # The idempotent schema block below remains the single place
                # where new v2 tables and indexes are declared.
                if existing_version == 1:
                    columns = {
                        str(row["name"])
                        for row in connection.execute(
                            "PRAGMA table_info(jobs)"
                        ).fetchall()
                    }
                    if "control_state" not in columns:
                        connection.execute(
                            "ALTER TABLE jobs ADD COLUMN control_state TEXT "
                            "NOT NULL DEFAULT 'RUNNING' CHECK (control_state IN "
                            "('RUNNING','PAUSE_REQUESTED','PAUSED','CANCEL_REQUESTED'))"
                        )
                    if "control_revision" not in columns:
                        connection.execute(
                            "ALTER TABLE jobs ADD COLUMN control_revision INTEGER "
                            "NOT NULL DEFAULT 1 CHECK (control_revision >= 1)"
                        )
                    if "control_requested_at" not in columns:
                        connection.execute(
                            "ALTER TABLE jobs ADD COLUMN control_requested_at TEXT"
                        )
                    if "control_message" not in columns:
                        connection.execute(
                            "ALTER TABLE jobs ADD COLUMN control_message TEXT"
                        )
                    connection.execute(
                        "UPDATE schema_meta SET value = '2' "
                        "WHERE key = 'schema_version'"
                    )
                blocking = ",".join(f"'{state.value}'" for state in BLOCKING_STATES)
                job_states = ",".join(f"'{state.value}'" for state in JobState)
                control_states = ",".join(
                    f"'{state.value}'" for state in JobControlState
                )
                scan_states = ",".join(f"'{state.value}'" for state in ScanState)
                _execute_sql_statements(
                    connection,
                    f"""
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
                        control_state TEXT NOT NULL DEFAULT 'RUNNING'
                            CHECK (control_state IN ({control_states})),
                        control_revision INTEGER NOT NULL DEFAULT 1
                            CHECK (control_revision >= 1),
                        control_requested_at TEXT,
                        control_message TEXT,
                        version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT
                    );
                    DROP INDEX IF EXISTS ix_jobs_queue;
                    CREATE INDEX ix_jobs_queue
                        ON jobs (priority DESC, created_at ASC)
                        WHERE state = 'QUEUED' AND control_state = 'RUNNING';
                    DROP INDEX IF EXISTS ix_jobs_ready;
                    CREATE INDEX ix_jobs_ready
                        ON jobs (priority DESC, created_at ASC)
                        WHERE state = 'READY' AND control_state = 'RUNNING';
                    DROP INDEX IF EXISTS ux_jobs_single_active;
                    DROP INDEX IF EXISTS ux_jobs_single_scanning;
                    CREATE UNIQUE INDEX ux_jobs_single_scanning
                        ON jobs ((1))
                        WHERE state = 'SCANNING' AND control_state != 'PAUSED';
                    DROP INDEX IF EXISTS ux_jobs_single_encoding;
                    CREATE UNIQUE INDEX ux_jobs_single_encoding
                        ON jobs ((1))
                        WHERE state IN ({blocking}) AND control_state != 'PAUSED';

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

                    CREATE TABLE IF NOT EXISTS release_preparations (
                        id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        state TEXT NOT NULL,
                        profile_id TEXT NOT NULL,
                        profile_digest TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        payload_name TEXT NOT NULL,
                        payload_path TEXT NOT NULL,
                        payload_size INTEGER NOT NULL CHECK (payload_size >= 1),
                        payload_sha256 TEXT NOT NULL,
                        kit_path TEXT,
                        manifest_sha256 TEXT,
                        torrent_infohash TEXT,
                        torrent_sha256 TEXT,
                        dupe_receipt_json TEXT,
                        qbittorrent_receipt_json TEXT,
                        publication_receipt_json TEXT,
                        error TEXT,
                        version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS ix_release_preparations_job
                        ON release_preparations(job_id, created_at DESC);
                    CREATE TABLE IF NOT EXISTS release_preparation_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        preparation_id TEXT NOT NULL
                            REFERENCES release_preparations(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        state TEXT NOT NULL,
                        message TEXT,
                        payload_json TEXT NOT NULL DEFAULT '{{}}',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS ix_release_events_preparation
                        ON release_preparation_events(preparation_id, id);
                    CREATE TABLE IF NOT EXISTS maintenance_operations (
                        id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        subject_id TEXT NOT NULL,
                        phase TEXT NOT NULL CHECK (
                            phase IN (
                                'INTENT', 'DETACHED', 'COMMITTED',
                                'FINALIZED', 'ROLLED_BACK'
                            )
                        ),
                        targets_json TEXT NOT NULL,
                        lease_owner TEXT NOT NULL,
                        lease_pid INTEGER NOT NULL CHECK (lease_pid >= 0),
                        lease_host TEXT NOT NULL,
                        lease_process_token TEXT NOT NULL,
                        lease_expires_at REAL NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS ix_maintenance_operations_phase
                        ON maintenance_operations(phase, created_at);
                    CREATE TABLE IF NOT EXISTS maintenance_target_claims (
                        original_path_key TEXT PRIMARY KEY,
                        operation_id TEXT NOT NULL
                            REFERENCES maintenance_operations(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS ix_maintenance_target_claims_operation
                        ON maintenance_target_claims(operation_id);
                    INSERT INTO schema_meta(key, value) VALUES ('schema_version', '{SCHEMA_VERSION}')
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                    """,
                )
                version_row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                actual_version = int(version_row["value"]) if version_row else 0
                if actual_version != SCHEMA_VERSION:
                    raise PersistenceError(
                        f"unsupported database schema {actual_version}; expected {SCHEMA_VERSION}"
                    )
                connection.commit()
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
        """Return any running worker job, preferring the serial encode lane."""

        encoding = self.encoding_job()
        return encoding if encoding is not None else self.preparing_job()

    def encoding_job(self) -> Job | None:
        placeholders = ",".join("?" for _ in BLOCKING_STATES)
        with self._read() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE state IN ({placeholders}) AND control_state != 'PAUSED'
                LIMIT 1
                """,
                [state.value for state in BLOCKING_STATES],
            ).fetchone()
        return self._decode_job(row) if row else None

    def preparing_job(self) -> Job | None:
        placeholders = ",".join("?" for _ in PREPARATION_ACTIVE_STATES)
        with self._read() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE state IN ({placeholders}) AND control_state != 'PAUSED'
                LIMIT 1
                """,
                [state.value for state in PREPARATION_ACTIVE_STATES],
            ).fetchone()
        return self._decode_job(row) if row else None

    def claim_next_job(self) -> Job | None:
        """Atomically claim one queued job for the independent scan lane."""

        try:
            with self._write() as connection:
                placeholders = ",".join("?" for _ in PREPARATION_ACTIVE_STATES)
                active = connection.execute(
                    f"""
                    SELECT id FROM jobs
                    WHERE state IN ({placeholders}) AND control_state != 'PAUSED'
                    LIMIT 1
                    """,
                    [state.value for state in PREPARATION_ACTIVE_STATES],
                ).fetchone()
                if active:
                    return None
                row = connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE state = 'QUEUED' AND control_state = 'RUNNING'
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
            raise QueueBlockedError(self.preparing_job()) from exc

    def claim_next_ready_job(self) -> Job | None:
        """Atomically move the first configured job into the serial encode lane."""

        try:
            with self._write() as connection:
                placeholders = ",".join("?" for _ in BLOCKING_STATES)
                active = connection.execute(
                    f"""
                    SELECT id FROM jobs
                    WHERE state IN ({placeholders}) AND control_state != 'PAUSED'
                    LIMIT 1
                    """,
                    [state.value for state in BLOCKING_STATES],
                ).fetchone()
                if active:
                    return None
                row = connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE state = 'READY' AND control_state = 'RUNNING'
                    ORDER BY priority DESC, created_at ASC LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return None
                return self._transition_in_connection(
                    connection,
                    row,
                    JobState.ENCODING,
                    message="claimed for serial encode; preparing reference timeline",
                    details={"queue_lane": "encode"},
                )
        except sqlite3.IntegrityError as exc:
            raise QueueBlockedError(self.encoding_job()) from exc

    def transition_job(
        self,
        job_id: str,
        target: JobState,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> Job:
        if target is JobState.CANCELLED:
            # Keep the legacy generic transition endpoint safe: an executing
            # stage must not be declared CANCELLED until its worker acks.
            if expected_version is not None:
                observed = self.get_job(job_id)
                if observed.version != expected_version:
                    raise StateConflictError(
                        f"job version is {observed.version}, expected {expected_version}",
                        current=observed.state,
                    )
            return self.request_cancel(job_id, message=message or "cancelled")
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

    def request_pause(
        self,
        job_id: str,
        *,
        message: str = "pause requested",
        expected_control_revision: int | None = None,
    ) -> Job:
        """Durably request a pause, acknowledging idle jobs immediately."""

        with self._write() as connection:
            row = self._job_row(connection, job_id)
            current = JobState(row["state"])
            control = JobControlState(row["control_state"])
            self._check_control_revision(row, expected_control_revision)
            if current in TERMINAL_STATES:
                raise StateConflictError(
                    "terminal jobs cannot be paused", current=current
                )
            if control in {
                JobControlState.PAUSE_REQUESTED,
                JobControlState.PAUSED,
            }:
                return self._decode_job(row)
            if control is JobControlState.CANCEL_REQUESTED:
                raise StateConflictError(
                    "cancellation is already requested", current=current
                )
            target = (
                JobControlState.PAUSE_REQUESTED
                if current in CONTROL_ACTIVE_STATES
                else JobControlState.PAUSED
            )
            return self._set_control_in_connection(
                connection,
                row,
                target,
                message=message,
                requested_at=utc_now(),
                kind="job.control.pause-requested",
                payload={"acknowledged": target is JobControlState.PAUSED},
            )

    def request_cancel(
        self,
        job_id: str,
        *,
        message: str = "cancellation requested",
        expected_control_revision: int | None = None,
    ) -> Job:
        """Request cancellation without declaring an active process stopped.

        Idle and already-paused jobs have no in-flight worker effects, so their
        request and acknowledgement are committed atomically.
        """

        with self._write() as connection:
            row = self._job_row(connection, job_id)
            current = JobState(row["state"])
            control = JobControlState(row["control_state"])
            self._check_control_revision(row, expected_control_revision)
            if current is JobState.CANCELLED:
                return self._decode_job(row)
            if current in {JobState.COMPLETED, JobState.FAILED}:
                raise StateConflictError(
                    "completed or failed jobs cannot be cancelled", current=current
                )
            if control is JobControlState.CANCEL_REQUESTED:
                return self._decode_job(row)
            active = (
                current in CONTROL_ACTIVE_STATES
                and control is not JobControlState.PAUSED
            )
            if active:
                return self._set_control_in_connection(
                    connection,
                    row,
                    JobControlState.CANCEL_REQUESTED,
                    message=message,
                    requested_at=utc_now(),
                    kind="job.control.cancel-requested",
                    payload={"acknowledged": False},
                )

            # There is no worker/process to acknowledge this request.  Commit
            # the ordinary terminal transition and control revision together.
            transitioned = self._transition_in_connection(
                connection,
                row,
                JobState.CANCELLED,
                message=message,
                details={"control_acknowledged": True},
            )
            terminal_row = self._job_row(connection, job_id)
            return self._set_control_in_connection(
                connection,
                terminal_row,
                JobControlState.RUNNING,
                message=None,
                requested_at=None,
                kind="job.control.cancelled",
                payload={
                    "acknowledged": True,
                    "state_version": transitioned.version,
                },
            )

    def acknowledge_pause(
        self,
        job_id: str,
        *,
        expected_control_revision: int | None = None,
        message: str | None = None,
    ) -> Job:
        """Worker acknowledgement after all stage processes have stopped."""

        with self._write() as connection:
            row = self._job_row(connection, job_id)
            current = JobState(row["state"])
            self._check_control_revision(row, expected_control_revision)
            if JobControlState(row["control_state"]) is JobControlState.PAUSED:
                return self._decode_job(row)
            if (
                JobControlState(row["control_state"])
                is not JobControlState.PAUSE_REQUESTED
            ):
                raise StateConflictError(
                    "no pause is awaiting acknowledgement", current=current
                )
            return self._set_control_in_connection(
                connection,
                row,
                JobControlState.PAUSED,
                message=message or row["control_message"] or "paused",
                requested_at=row["control_requested_at"],
                kind="job.control.paused",
                payload={"acknowledged": True, "stage": current.value},
            )

    def acknowledge_cancel(
        self,
        job_id: str,
        *,
        expected_control_revision: int | None = None,
        message: str | None = None,
    ) -> Job:
        """Worker acknowledgement after termination and partial cleanup."""

        with self._write() as connection:
            row = self._job_row(connection, job_id)
            current = JobState(row["state"])
            self._check_control_revision(row, expected_control_revision)
            if current is JobState.CANCELLED:
                return self._decode_job(row)
            if (
                JobControlState(row["control_state"])
                is not JobControlState.CANCEL_REQUESTED
            ):
                raise StateConflictError(
                    "no cancellation is awaiting acknowledgement", current=current
                )
            cancel_message = message or row["control_message"] or "cancelled"
            self._transition_in_connection(
                connection,
                row,
                JobState.CANCELLED,
                message=cancel_message,
                details={"control_acknowledged": True},
            )
            return self._set_control_in_connection(
                connection,
                self._job_row(connection, job_id),
                JobControlState.RUNNING,
                message=None,
                requested_at=None,
                kind="job.control.cancelled",
                payload={"acknowledged": True, "stage": current.value},
            )

    def resume_paused_job(
        self,
        job_id: str,
        *,
        message: str = "resumed",
        expected_control_revision: int | None = None,
    ) -> Job:
        """Release a durable pause without changing its pipeline/checkpoints."""

        try:
            with self._write() as connection:
                row = self._job_row(connection, job_id)
                current = JobState(row["state"])
                self._check_control_revision(row, expected_control_revision)
                if JobControlState(row["control_state"]) is not JobControlState.PAUSED:
                    raise StateConflictError("job is not paused", current=current)
                if current in TERMINAL_STATES:
                    raise StateConflictError(
                        "terminal jobs cannot be resumed", current=current
                    )
                return self._set_control_in_connection(
                    connection,
                    row,
                    JobControlState.RUNNING,
                    message=None,
                    requested_at=None,
                    kind="job.control.resumed",
                    payload={"message": message, "stage": current.value},
                )
        except sqlite3.IntegrityError as exc:
            if current in PREPARATION_ACTIVE_STATES:
                raise QueueBlockedError(self.preparing_job()) from exc
            if current in BLOCKING_STATES:
                raise QueueBlockedError(self.encoding_job()) from exc
            raise

    def get_control(self, job_id: str) -> tuple[JobControlState, int]:
        """Cheap polling read used by process runners."""

        with self._read() as connection:
            row = connection.execute(
                "SELECT control_state, control_revision FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"job not found: {job_id}")
        return JobControlState(row["control_state"]), int(row["control_revision"])

    @staticmethod
    def _check_control_revision(
        row: sqlite3.Row, expected_control_revision: int | None
    ) -> None:
        if (
            expected_control_revision is not None
            and int(row["control_revision"]) != expected_control_revision
        ):
            raise StateConflictError(
                "job control revision is "
                f"{row['control_revision']}, expected {expected_control_revision}",
                current=JobState(row["state"]),
            )

    def _set_control_in_connection(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        target: JobControlState,
        *,
        message: str | None,
        requested_at: str | None,
        kind: str,
        payload: dict[str, Any],
    ) -> Job:
        previous = JobControlState(row["control_state"])
        now = utc_now()
        cursor = connection.execute(
            """
            UPDATE jobs SET control_state = ?, control_revision = control_revision + 1,
                control_requested_at = ?, control_message = ?, updated_at = ?
            WHERE id = ? AND control_revision = ?
            """,
            (
                target.value,
                requested_at,
                message,
                now,
                row["id"],
                row["control_revision"],
            ),
        )
        if cursor.rowcount != 1:
            raise StateConflictError(
                "job control changed concurrently", current=JobState(row["state"])
            )
        self._insert_event(
            connection,
            job_id=row["id"],
            kind=kind,
            message=message,
            payload={
                **payload,
                "control_from": previous.value,
                "control_to": target.value,
                "previous_control_revision": int(row["control_revision"]),
                "new_control_revision": int(row["control_revision"]) + 1,
            },
        )
        return self._decode_job(self._job_row(connection, row["id"]))

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

                if retry_stage is not JobState.READY:
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
                retry_progress = pipeline_progress_baseline(retry_stage)
                if retry_progress is None:
                    raise StateConflictError(
                        f"FAILED stage {retry_stage.value} has no progress baseline",
                        current=current,
                    )
                cursor = connection.execute(
                    """
                    UPDATE jobs SET state = ?, status_message = ?, error = NULL,
                        resume_state = NULL, progress = ?, finished_at = NULL,
                        updated_at = ?, version = version + 1
                    WHERE id = ? AND state = ? AND version = ?
                    """,
                    (
                        retry_stage.value,
                        retry_message,
                        retry_progress,
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
                blocker = self.encoding_job()
                if blocker is not None:
                    raise QueueBlockedError(blocker) from exc
            raise

    def restart_cancelled_job(
        self,
        job_id: str,
        *,
        message: str | None = None,
        expected_version: int | None = None,
    ) -> Job:
        """Restore a cancelled job to the safest non-running queue boundary.

        A configured job returns to READY and can reuse its validated stage
        markers.  A successfully scanned but unconfigured job returns to the
        selection screen.  Earlier cancellations return to QUEUED for a fresh
        scan attempt.  None of these targets takes ownership of the serial
        encode lane inside this request.
        """

        with self._write() as connection:
            row = self._job_row(connection, job_id)
            current = JobState(row["state"])
            if current is not JobState.CANCELLED:
                raise StateConflictError(
                    "only CANCELLED jobs can be restarted",
                    current=current,
                )
            if expected_version is not None and row["version"] != expected_version:
                raise StateConflictError(
                    f"job version is {row['version']}, expected {expected_version}",
                    current=current,
                )

            successful_scan = connection.execute(
                """
                SELECT id, status FROM scans
                WHERE job_id = ? AND status IN ('AWAITING_SELECTION', 'COMPLETED')
                ORDER BY created_at DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            has_selection = row["selection_json"] not in {None, "", "null"}
            if successful_scan is not None and has_selection:
                target = JobState.READY
                default_message = "cancelled job restored to the configured queue"
            elif successful_scan is not None:
                target = JobState.AWAITING_SELECTION
                default_message = "cancelled job restored for operator selection"
            else:
                target = JobState.QUEUED
                default_message = "cancelled job queued for a new scan attempt"

            now = utc_now()
            restart_message = message or default_message
            progress = pipeline_progress_baseline(target)
            cursor = connection.execute(
                """
                UPDATE jobs SET state = ?, status_message = ?, error = NULL,
                    resume_state = NULL, progress = ?, finished_at = NULL,
                    started_at = CASE WHEN ? = 'QUEUED' THEN NULL ELSE started_at END,
                    updated_at = ?, version = version + 1
                WHERE id = ? AND state = ? AND version = ?
                """,
                (
                    target.value,
                    restart_message,
                    progress,
                    target.value,
                    now,
                    job_id,
                    JobState.CANCELLED.value,
                    row["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflictError("job changed concurrently", current=current)
            self._insert_event(
                connection,
                job_id=job_id,
                kind="job.restart",
                state_from=current,
                state_to=target,
                message=restart_message,
                payload={
                    "restart_target": target.value,
                    "reused_successful_scan": successful_scan is not None,
                    "reused_selection": has_selection and successful_scan is not None,
                    "previous_version": int(row["version"]),
                    "new_version": int(row["version"]) + 1,
                },
            )
            return self._decode_job(self._job_row(connection, job_id))

    def delete_terminal_job(
        self,
        job_id: str,
        *,
        expected_version: int | None = None,
        cleanup: Callable[[], None] | None = None,
        allow_completed: bool = False,
        expected_release_versions: Mapping[str, int] | None = None,
        maintenance_operation_id: str | None = None,
    ) -> None:
        """Delete a terminal job after its workspace cleanup succeeds.

        Completed records are retained by default for compatibility.  The
        maintenance API opts in only after independently proving that the
        public completed release will be preserved.
        """

        with self._write() as connection:
            row = self._job_row(connection, job_id)
            current = JobState(row["state"])
            deletable = {JobState.FAILED, JobState.CANCELLED}
            if allow_completed:
                deletable.add(JobState.COMPLETED)
            if current not in deletable:
                allowed = (
                    "FAILED, CANCELLED, or COMPLETED"
                    if allow_completed
                    else "FAILED or CANCELLED"
                )
                raise StateConflictError(
                    f"only {allowed} jobs can be deleted",
                    current=current,
                )
            if expected_version is not None and row["version"] != expected_version:
                raise StateConflictError(
                    f"job version is {row['version']}, expected {expected_version}",
                    current=current,
                )
            if expected_release_versions is not None:
                if any(
                    not isinstance(identifier, str)
                    or type(version) is not int
                    or version < 1
                    for identifier, version in expected_release_versions.items()
                ):
                    raise ValueError("invalid release preparation version snapshot")
                expected = {
                    identifier: version
                    for identifier, version in expected_release_versions.items()
                }
                if len(expected) != len(expected_release_versions):
                    raise ValueError("invalid release preparation version snapshot")
                preparations = connection.execute(
                    "SELECT id, state, version FROM release_preparations "
                    "WHERE job_id = ?",
                    (job_id,),
                ).fetchall()
                actual = {
                    str(preparation["id"]): int(preparation["version"])
                    for preparation in preparations
                }
                if actual != expected:
                    raise StateConflictError(
                        "release preparations changed concurrently",
                        current=current,
                    )
                active_release_states = {
                    "PREPARING",
                    "SEEDING_CHECK",
                    "SEEDING",
                    "PUBLISHING",
                }
                if any(
                    str(preparation["state"]) in active_release_states
                    for preparation in preparations
                ):
                    raise StateConflictError(
                        "active release preparation prevents job deletion",
                        current=current,
                    )
            if cleanup is not None and maintenance_operation_id is not None:
                raise ValueError(
                    "cleanup and maintenance_operation_id are mutually exclusive"
                )
            if cleanup is not None:
                cleanup()
            cursor = connection.execute(
                "DELETE FROM jobs WHERE id = ? AND state = ? AND version = ?",
                (job_id, current.value, row["version"]),
            )
            if cursor.rowcount != 1:
                raise StateConflictError("job changed concurrently", current=current)
            if maintenance_operation_id is not None:
                self._mark_maintenance_committed(
                    connection,
                    maintenance_operation_id,
                    kind="terminal-job-purge",
                    subject_id=job_id,
                )

    def record_completed_cleanup(
        self,
        job_id: str,
        *,
        expected_version: int | None,
        cleanup: Callable[[], None] | None,
        payload: Mapping[str, Any],
        maintenance_operation_id: str | None = None,
    ) -> Job:
        """Bind a short workspace quarantine to the completed-job snapshot."""

        with self._write() as connection:
            row = self._job_row(connection, job_id)
            current = JobState(row["state"])
            if current is not JobState.COMPLETED:
                raise StateConflictError(
                    "temporary cleanup is limited to completed jobs",
                    current=current,
                )
            if expected_version is not None and row["version"] != expected_version:
                raise StateConflictError(
                    f"job version is {row['version']}, expected {expected_version}",
                    current=current,
                )
            if maintenance_operation_id is not None and cleanup is not None:
                raise ValueError(
                    "cleanup and maintenance_operation_id are mutually exclusive"
                )
            if cleanup is not None:
                cleanup()
            self._insert_event(
                connection,
                job_id=job_id,
                kind="job.workspace-cleaned",
                message="temporary workspace cleanup completed",
                payload=payload,
            )
            if maintenance_operation_id is not None:
                self._mark_maintenance_committed(
                    connection,
                    maintenance_operation_id,
                    kind="completed-workspace-cleanup",
                    subject_id=job_id,
                )
            return self._decode_job(self._job_row(connection, job_id))

    @staticmethod
    def _mark_maintenance_committed(
        connection: sqlite3.Connection,
        operation_id: str,
        *,
        kind: str,
        subject_id: str,
    ) -> None:
        """Commit a filesystem detach lease in the domain mutation transaction."""

        row = connection.execute(
            "SELECT kind, subject_id, phase, targets_json "
            "FROM maintenance_operations WHERE id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise PersistenceError(f"maintenance operation not found: {operation_id}")
        if row["kind"] != kind or row["subject_id"] != subject_id:
            raise PersistenceError("maintenance operation binding does not match")
        if row["phase"] != "DETACHED":
            raise PersistenceError(
                "maintenance operation must be fully detached before commit"
            )
        targets = _json_load(row["targets_json"], None)
        if not isinstance(targets, list) or any(
            not isinstance(target, dict) or target.get("state") != "DETACHED"
            for target in targets
        ):
            raise PersistenceError("maintenance target receipts are incomplete")
        cursor = connection.execute(
            "UPDATE maintenance_operations "
            "SET phase = 'COMMITTED', updated_at = ? "
            "WHERE id = ? AND phase = 'DETACHED'",
            (utc_now(), operation_id),
        )
        if cursor.rowcount != 1:
            raise PersistenceError("maintenance operation changed concurrently")

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
        expected_state: JobState | None = None,
        emit_event: bool = True,
    ) -> Job:
        if not 0 <= progress <= 1:
            raise ValueError("progress must be between 0 and 1")
        with self._write() as connection:
            row = self._job_row(connection, job_id)
            current = JobState(row["state"])
            if expected_state is not None and current is not expected_state:
                raise StateConflictError(
                    f"job state is {current.value}, expected {expected_state.value}",
                    current=current,
                )
            if current in TERMINAL_STATES:
                raise StateConflictError(
                    "terminal jobs cannot report progress", current=current
                )
            if JobControlState(row["control_state"]) is JobControlState.PAUSED:
                raise StateConflictError(
                    "paused jobs cannot report progress", current=current
                )
            previous = float(row["progress"]) if row["progress"] is not None else 0.0
            effective_progress = max(previous, progress)
            now = utc_now()
            connection.execute(
                """
                UPDATE jobs SET progress = ?, status_message = ?, updated_at = ?,
                    version = version + 1 WHERE id = ?
                """,
                (effective_progress, message, now, job_id),
            )
            if emit_event:
                self._insert_event(
                    connection,
                    job_id=job_id,
                    kind="job.progress",
                    message=message,
                    payload={**(details or {}), "progress": effective_progress},
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
        control = JobControlState(row["control_state"])
        if control is not JobControlState.RUNNING and target is not JobState.CANCELLED:
            raise StateConflictError(
                f"job control is {control.value}; worker must acknowledge it first",
                current=current,
            )
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
        baseline = pipeline_progress_baseline(target)
        if current is JobState.NEEDS_REVIEW and target is JobState.READY:
            # A revised material selection invalidates downstream checkpoints,
            # so the complete-pipeline meter restarts at READY deliberately.
            progress = baseline
        elif baseline is not None:
            progress = max(float(progress or 0.0), baseline)
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
