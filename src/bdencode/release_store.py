"""Durable release-preparation records kept beside the encode queue."""

from __future__ import annotations

from datetime import datetime
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import Database, NotFoundError, PersistenceError, StateConflictError, utc_now
from .models import JobState
from .release.models import ReleaseMetadata, ReleasePreparationState


_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_COMPLETED_RELEASE_DELETED_EVENT = "job.completed-release-deleted"


CleanupRollback = Callable[[], None]
CleanupCallback = Callable[[], CleanupRollback | None]
RecoveryCleanupCallback = Callable[["ReleasePreparation"], CleanupRollback | None]


class ReleasePreparation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    job_id: str
    state: ReleasePreparationState
    profile_id: str
    profile_digest: str
    metadata: ReleaseMetadata
    payload_name: str
    payload_path: str
    payload_size: int = Field(ge=1)
    payload_sha256: str
    kit_path: str | None = None
    manifest_sha256: str | None = None
    torrent_infohash: str | None = None
    torrent_sha256: str | None = None
    dupe_receipt: dict[str, Any] | None = None
    qbittorrent_receipt: dict[str, Any] | None = None
    publication_receipt: dict[str, Any] | None = None
    error: str | None = None
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "profile_digest", "payload_sha256", "manifest_sha256", "torrent_sha256"
    )
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is not None and not _HEX_64.fullmatch(value):
            raise ValueError("expected a lowercase SHA-256 digest")
        return value

    @field_validator("torrent_infohash")
    @classmethod
    def validate_infohash(cls, value: str | None) -> str | None:
        if value is not None and not _HEX_40.fullmatch(value):
            raise ValueError("expected a lowercase SHA-1 infohash")
        return value


_TRANSITIONS: dict[ReleasePreparationState, frozenset[ReleasePreparationState]] = {
    ReleasePreparationState.NOT_PREPARED: frozenset(
        {
            ReleasePreparationState.PREPARING,
            ReleasePreparationState.FAILED,
        }
    ),
    ReleasePreparationState.PREPARING: frozenset(
        {
            ReleasePreparationState.READY,
            ReleasePreparationState.NEEDS_REVIEW,
            ReleasePreparationState.FAILED,
        }
    ),
    ReleasePreparationState.NEEDS_REVIEW: frozenset(
        {
            ReleasePreparationState.PREPARING,
            ReleasePreparationState.SEEDING_CHECK,
            ReleasePreparationState.FAILED,
        }
    ),
    ReleasePreparationState.READY: frozenset(
        {
            ReleasePreparationState.SEEDING_CHECK,
            ReleasePreparationState.SEEDING,
            ReleasePreparationState.READY_TO_PUBLISH,
            ReleasePreparationState.FAILED,
        }
    ),
    ReleasePreparationState.SEEDING_CHECK: frozenset(
        {
            ReleasePreparationState.READY_TO_PUBLISH,
            ReleasePreparationState.NEEDS_REVIEW,
            ReleasePreparationState.UNKNOWN,
            ReleasePreparationState.FAILED,
        }
    ),
    ReleasePreparationState.READY_TO_PUBLISH: frozenset(
        {
            ReleasePreparationState.SEEDING,
            ReleasePreparationState.PUBLISHING,
            ReleasePreparationState.NEEDS_REVIEW,
            ReleasePreparationState.UNKNOWN,
            ReleasePreparationState.FAILED,
        }
    ),
    ReleasePreparationState.SEEDING: frozenset(
        {
            ReleasePreparationState.READY,
            ReleasePreparationState.READY_TO_PUBLISH,
            ReleasePreparationState.UNKNOWN,
        }
    ),
    ReleasePreparationState.PUBLISHING: frozenset(
        {
            ReleasePreparationState.PUBLISHED,
            ReleasePreparationState.NEEDS_REVIEW,
            ReleasePreparationState.UNKNOWN,
            ReleasePreparationState.FAILED,
        }
    ),
    ReleasePreparationState.FAILED: frozenset({ReleasePreparationState.PREPARING}),
    ReleasePreparationState.UNKNOWN: frozenset(),
    ReleasePreparationState.PUBLISHED: frozenset(),
}


class ReleaseStore:
    """Small transactional store that shares ``Database`` locking semantics."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        # Database.initialize owns the schema-version migration.  This
        # idempotent v2 extension is separate so a pre-existing v2 deployment
        # receives the release tables without destructive rebuilds.
        with self.database._write() as connection:
            connection.executescript(
                """
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
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_release_events_preparation
                    ON release_preparation_events(preparation_id, id);
                """
            )

    @staticmethod
    def _decode(row: sqlite3.Row) -> ReleasePreparation:
        return ReleasePreparation(
            id=row["id"],
            job_id=row["job_id"],
            state=ReleasePreparationState(row["state"]),
            profile_id=row["profile_id"],
            profile_digest=row["profile_digest"],
            metadata=ReleaseMetadata.model_validate_json(row["metadata_json"]),
            payload_name=row["payload_name"],
            payload_path=row["payload_path"],
            payload_size=int(row["payload_size"]),
            payload_sha256=row["payload_sha256"],
            kit_path=row["kit_path"],
            manifest_sha256=row["manifest_sha256"],
            torrent_infohash=row["torrent_infohash"],
            torrent_sha256=row["torrent_sha256"],
            dupe_receipt=(
                json.loads(row["dupe_receipt_json"])
                if row["dupe_receipt_json"]
                else None
            ),
            qbittorrent_receipt=(
                json.loads(row["qbittorrent_receipt_json"])
                if row["qbittorrent_receipt_json"]
                else None
            ),
            publication_receipt=(
                json.loads(row["publication_receipt_json"])
                if row["publication_receipt_json"]
                else None
            ),
            error=row["error"],
            version=int(row["version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create(
        self,
        *,
        job_id: str,
        profile_id: str,
        profile_digest: str,
        metadata: ReleaseMetadata,
        payload_name: str,
        payload_path: str,
        payload_size: int,
        payload_sha256: str,
    ) -> ReleasePreparation:
        preparation_id = uuid4().hex
        now = utc_now()
        # Let the strict record validate every digest and metadata value before
        # anything is persisted.
        candidate = ReleasePreparation(
            id=preparation_id,
            job_id=job_id,
            state=ReleasePreparationState.NOT_PREPARED,
            profile_id=profile_id,
            profile_digest=profile_digest,
            metadata=metadata,
            payload_name=payload_name,
            payload_path=payload_path,
            payload_size=payload_size,
            payload_sha256=payload_sha256,
            version=1,
            created_at=now,
            updated_at=now,
        )
        with self.database._write() as connection:
            job = connection.execute(
                "SELECT state FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise NotFoundError(f"job not found: {job_id}")
            current = JobState(job["state"])
            if current is not JobState.COMPLETED:
                raise StateConflictError(
                    "release preparation requires a completed job",
                    current=current,
                )
            self._assert_no_destructive_maintenance(
                connection,
                job_id=job_id,
            )
            tombstone = connection.execute(
                "SELECT 1 FROM events WHERE job_id = ? AND kind = ? LIMIT 1",
                (job_id, _COMPLETED_RELEASE_DELETED_EVENT),
            ).fetchone()
            if tombstone is not None:
                raise StateConflictError(
                    "the completed public release was explicitly deleted",
                    current=current,
                )
            connection.execute(
                """
                INSERT INTO release_preparations(
                    id, job_id, state, profile_id, profile_digest, metadata_json,
                    payload_name, payload_path, payload_size, payload_sha256,
                    version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    candidate.id,
                    candidate.job_id,
                    candidate.state.value,
                    candidate.profile_id,
                    candidate.profile_digest,
                    candidate.metadata.model_dump_json(),
                    candidate.payload_name,
                    candidate.payload_path,
                    candidate.payload_size,
                    candidate.payload_sha256,
                    now,
                    now,
                ),
            )
            self._event(
                connection,
                candidate.id,
                "release.preparation.created",
                candidate.state,
                payload={"profile_id": profile_id},
            )
        return self.get(preparation_id)

    def get(self, preparation_id: str) -> ReleasePreparation:
        with self.database._read() as connection:
            row = connection.execute(
                "SELECT * FROM release_preparations WHERE id = ?",
                (preparation_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"release preparation not found: {preparation_id}")
        return self._decode(row)

    def list_for_job(self, job_id: str) -> tuple[ReleasePreparation, ...]:
        with self.database._read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM release_preparations
                WHERE job_id = ? ORDER BY created_at DESC
                """,
                (job_id,),
            ).fetchall()
        return tuple(self._decode(row) for row in rows)

    def list_interrupted(self) -> tuple[ReleasePreparation, ...]:
        active = self._active_states()
        placeholders = ",".join("?" for _ in active)
        with self.database._read() as connection:
            rows = connection.execute(
                f"SELECT * FROM release_preparations "
                f"WHERE state IN ({placeholders}) ORDER BY created_at, id",
                tuple(state.value for state in active),
            ).fetchall()
        return tuple(self._decode(row) for row in rows)

    def transition(
        self,
        preparation_id: str,
        target: ReleasePreparationState,
        *,
        expected_version: int,
        message: str | None = None,
        values: dict[str, Any] | None = None,
    ) -> ReleasePreparation:
        allowed_columns = {
            "kit_path",
            "manifest_sha256",
            "torrent_infohash",
            "torrent_sha256",
            "dupe_receipt_json",
            "qbittorrent_receipt_json",
            "publication_receipt_json",
            "error",
        }
        updates = dict(values or {})
        unknown = set(updates) - allowed_columns
        if unknown:
            raise ValueError(f"unsupported release update columns: {sorted(unknown)}")
        with self.database._write() as connection:
            row = connection.execute(
                "SELECT * FROM release_preparations WHERE id = ?",
                (preparation_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"release preparation not found: {preparation_id}")
            current = ReleasePreparationState(row["state"])
            self._assert_no_destructive_maintenance(
                connection,
                job_id=str(row["job_id"]),
                preparation_id=preparation_id,
            )
            if int(row["version"]) != expected_version:
                raise StateConflictError(
                    f"release preparation version is {row['version']}, "
                    f"expected {expected_version}"
                )
            if target is not current and target not in _TRANSITIONS[current]:
                raise StateConflictError(
                    f"release preparation cannot transition from {current.value} "
                    f"to {target.value}"
                )
            assignments = ["state = ?", "version = version + 1", "updated_at = ?"]
            parameters: list[Any] = [target.value, utc_now()]
            for key, value in updates.items():
                assignments.append(f"{key} = ?")
                parameters.append(
                    json.dumps(value, separators=(",", ":"), sort_keys=True)
                    if key.endswith("_json") and value is not None
                    else value
                )
            parameters.extend((preparation_id, expected_version))
            cursor = connection.execute(
                f"""
                UPDATE release_preparations SET {", ".join(assignments)}
                WHERE id = ? AND version = ?
                """,
                parameters,
            )
            if cursor.rowcount != 1:
                raise StateConflictError("release preparation changed concurrently")
            self._event(
                connection,
                preparation_id,
                "release.preparation.transitioned",
                target,
                message=message,
                payload={"from": current.value, "to": target.value},
            )
            updated = connection.execute(
                "SELECT * FROM release_preparations WHERE id = ?",
                (preparation_id,),
            ).fetchone()
        assert updated is not None
        return self._decode(updated)

    def claim_seeding(
        self,
        preparation_id: str,
        *,
        expected_version: int,
        expected_profile_digest: str,
        expected_payload_sha256: str,
        expected_infohash: str,
    ) -> ReleasePreparation:
        """Acquire one qBittorrent add lease for an equivalent torrent."""

        allowed_states = {
            ReleasePreparationState.READY,
            ReleasePreparationState.READY_TO_PUBLISH,
        }
        with self.database._write() as connection:
            row = connection.execute(
                "SELECT * FROM release_preparations WHERE id = ?",
                (preparation_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"release preparation not found: {preparation_id}")
            current = ReleasePreparationState(row["state"])
            self._assert_no_destructive_maintenance(
                connection,
                job_id=str(row["job_id"]),
                preparation_id=preparation_id,
            )
            if int(row["version"]) != expected_version:
                raise StateConflictError(
                    f"release preparation version is {row['version']}, "
                    f"expected {expected_version}"
                )
            if current not in allowed_states:
                raise StateConflictError("seeding requires a verified release kit")
            if (
                row["profile_digest"] != expected_profile_digest
                or row["payload_sha256"] != expected_payload_sha256
                or row["torrent_infohash"] != expected_infohash
            ):
                raise StateConflictError(
                    "release bindings changed while seeding was prepared"
                )

            candidates = connection.execute(
                """
                SELECT id, state, qbittorrent_receipt_json
                FROM release_preparations
                WHERE job_id = ? AND profile_id = ? AND torrent_infohash = ?
                """,
                (row["job_id"], row["profile_id"], expected_infohash),
            ).fetchall()
            for candidate in candidates:
                encoded_receipt = candidate["qbittorrent_receipt_json"]
                outcome: str | None = None
                if encoded_receipt:
                    try:
                        receipt = json.loads(encoded_receipt)
                        outcome = (
                            receipt.get("outcome")
                            if isinstance(receipt, dict)
                            else None
                        )
                    except (TypeError, ValueError):
                        outcome = None
                candidate_state = ReleasePreparationState(candidate["state"])
                is_current = candidate["id"] == preparation_id
                if (
                    candidate_state is ReleasePreparationState.SEEDING
                    or (
                        not is_current
                        and candidate_state is ReleasePreparationState.UNKNOWN
                    )
                    or outcome not in {None, "REJECTED"}
                    or (encoded_receipt is not None and outcome is None)
                ):
                    raise StateConflictError(
                        "an equivalent torrent already has an active, successful, "
                        "or uncertain qBittorrent outcome"
                    )

            cursor = connection.execute(
                """
                UPDATE release_preparations
                SET state = ?, version = version + 1, updated_at = ?, error = NULL
                WHERE id = ? AND state = ? AND version = ?
                """,
                (
                    ReleasePreparationState.SEEDING.value,
                    utc_now(),
                    preparation_id,
                    current.value,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(
                    "release preparation changed while seeding was claimed"
                )
            self._event(
                connection,
                preparation_id,
                "release.preparation.transitioned",
                ReleasePreparationState.SEEDING,
                message="exclusive qBittorrent add lease acquired",
                payload={
                    "from": current.value,
                    "to": ReleasePreparationState.SEEDING.value,
                },
            )
            updated = connection.execute(
                "SELECT * FROM release_preparations WHERE id = ?",
                (preparation_id,),
            ).fetchone()
        assert updated is not None
        return self._decode(updated)

    def fail_build(
        self,
        preparation_id: str,
        *,
        expected_version: int,
        error: str,
        maintenance_operation_id: str | None = None,
    ) -> ReleasePreparation:
        """Fail a PREPARING build and commit its orphan-kit detach atomically."""

        with self.database._write() as connection:
            row = connection.execute(
                "SELECT * FROM release_preparations WHERE id = ?",
                (preparation_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"release preparation not found: {preparation_id}")
            current = ReleasePreparationState(row["state"])
            self._assert_no_destructive_maintenance(
                connection,
                job_id=str(row["job_id"]),
                preparation_id=preparation_id,
            )
            if int(row["version"]) != expected_version:
                raise StateConflictError(
                    f"release preparation version is {row['version']}, "
                    f"expected {expected_version}"
                )
            if current is not ReleasePreparationState.PREPARING:
                raise StateConflictError("only a PREPARING build can be failed")
            cursor = connection.execute(
                """
                UPDATE release_preparations
                SET state = ?, version = version + 1, updated_at = ?, error = ?
                WHERE id = ? AND state = ? AND version = ?
                """,
                (
                    ReleasePreparationState.FAILED.value,
                    utc_now(),
                    error,
                    preparation_id,
                    ReleasePreparationState.PREPARING.value,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(
                    "release preparation changed while its build was failed"
                )
            self._event(
                connection,
                preparation_id,
                "release.preparation.transitioned",
                ReleasePreparationState.FAILED,
                message="release build failed safely",
                payload={
                    "from": ReleasePreparationState.PREPARING.value,
                    "to": ReleasePreparationState.FAILED.value,
                },
            )
            if maintenance_operation_id is not None:
                self.database._mark_maintenance_committed(
                    connection,
                    maintenance_operation_id,
                    kind="failed-release-build-cleanup",
                    subject_id=preparation_id,
                )
            updated = connection.execute(
                "SELECT * FROM release_preparations WHERE id = ?",
                (preparation_id,),
            ).fetchone()
        assert updated is not None
        return self._decode(updated)

    def claim_publication(
        self,
        preparation_id: str,
        *,
        expected_version: int,
        expected_profile_digest: str,
        expected_manifest_sha256: str,
        expected_payload_sha256: str,
        dupe_receipt: Mapping[str, Any],
    ) -> ReleasePreparation:
        """Acquire the tracker-publication lease across equivalent preparations.

        A job may have more than one draft preparation, but only one preparation
        for the same tracker profile and payload may have an active, successful,
        or uncertain publication outcome.  The sibling check and the current
        preparation CAS deliberately share one ``BEGIN IMMEDIATE`` transaction.
        """

        guarded_states = {
            ReleasePreparationState.PUBLISHING,
            ReleasePreparationState.PUBLISHED,
            ReleasePreparationState.UNKNOWN,
        }
        with self.database._write() as connection:
            row = connection.execute(
                "SELECT * FROM release_preparations WHERE id = ?",
                (preparation_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"release preparation not found: {preparation_id}")
            current = ReleasePreparationState(row["state"])
            if int(row["version"]) != expected_version:
                raise StateConflictError(
                    f"release preparation version is {row['version']}, "
                    f"expected {expected_version}"
                )
            if current is not ReleasePreparationState.READY_TO_PUBLISH:
                raise StateConflictError(
                    "publication requires a READY_TO_PUBLISH preparation"
                )
            if (
                row["profile_digest"] != expected_profile_digest
                or row["manifest_sha256"] != expected_manifest_sha256
                or row["payload_sha256"] != expected_payload_sha256
            ):
                raise StateConflictError(
                    "release bindings changed while publication was prepared"
                )

            siblings = connection.execute(
                """
                SELECT id, state, publication_receipt_json
                FROM release_preparations
                WHERE job_id = ? AND profile_id = ? AND id <> ?
                """,
                (
                    row["job_id"],
                    row["profile_id"],
                    preparation_id,
                ),
            ).fetchall()
            for sibling in siblings:
                sibling_state = ReleasePreparationState(sibling["state"])
                blocked = sibling_state in guarded_states
                encoded_receipt = sibling["publication_receipt_json"]
                if encoded_receipt:
                    try:
                        receipt = json.loads(encoded_receipt)
                        outcome = (
                            receipt.get("outcome")
                            if isinstance(receipt, dict)
                            else None
                        )
                    except (TypeError, ValueError):
                        outcome = None
                    blocked = blocked or outcome != "REJECTED"
                if blocked:
                    raise StateConflictError(
                        "an equivalent release preparation already has an active, "
                        "published, or uncertain tracker outcome"
                    )

            encoded_current_receipt = row["publication_receipt_json"]
            if encoded_current_receipt:
                try:
                    current_receipt = json.loads(encoded_current_receipt)
                    current_outcome = (
                        current_receipt.get("outcome")
                        if isinstance(current_receipt, dict)
                        else None
                    )
                except (TypeError, ValueError):
                    current_outcome = None
                if current_outcome != "REJECTED":
                    raise StateConflictError(
                        "release preparation already has a tracker outcome"
                    )

            cursor = connection.execute(
                """
                UPDATE release_preparations SET
                    state = ?, version = version + 1, updated_at = ?, error = NULL,
                    dupe_receipt_json = ?
                WHERE id = ? AND state = ? AND version = ?
                """,
                (
                    ReleasePreparationState.PUBLISHING.value,
                    utc_now(),
                    json.dumps(
                        dict(dupe_receipt), separators=(",", ":"), sort_keys=True
                    ),
                    preparation_id,
                    ReleasePreparationState.READY_TO_PUBLISH.value,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(
                    "release preparation changed while publication was claimed"
                )
            self._event(
                connection,
                preparation_id,
                "release.preparation.transitioned",
                ReleasePreparationState.PUBLISHING,
                message="exclusive tracker publication lease acquired",
                payload={
                    "from": current.value,
                    "to": ReleasePreparationState.PUBLISHING.value,
                },
            )
            updated = connection.execute(
                "SELECT * FROM release_preparations WHERE id = ?",
                (preparation_id,),
            ).fetchone()
        assert updated is not None
        return self._decode(updated)

    def update_receipt(
        self,
        preparation_id: str,
        *,
        expected_version: int,
        receipt_column: str,
        receipt: dict[str, Any],
        message: str,
    ) -> ReleasePreparation:
        if receipt_column not in {
            "dupe_receipt_json",
            "qbittorrent_receipt_json",
            "publication_receipt_json",
        }:
            raise ValueError("invalid release receipt column")
        current = self.get(preparation_id)
        return self.transition(
            preparation_id,
            current.state,
            expected_version=expected_version,
            message=message,
            values={receipt_column: receipt},
        )

    @staticmethod
    def _run_rollbacks(rollbacks: list[CleanupRollback]) -> None:
        failures: list[BaseException] = []
        for rollback in reversed(rollbacks):
            try:
                rollback()
            except BaseException as exc:  # pragma: no cover - catastrophic FS failure
                failures.append(exc)
        if failures:
            raise PersistenceError("filesystem cleanup rollback failed") from failures[
                0
            ]

    @staticmethod
    def _active_states() -> frozenset[ReleasePreparationState]:
        return frozenset(
            {
                ReleasePreparationState.PREPARING,
                ReleasePreparationState.SEEDING_CHECK,
                ReleasePreparationState.SEEDING,
                ReleasePreparationState.PUBLISHING,
            }
        )

    @staticmethod
    def _assert_no_destructive_maintenance(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        preparation_id: str | None = None,
    ) -> None:
        clauses = [
            "(kind IN ('terminal-job-purge', 'completed-release-delete') "
            "AND subject_id = ?)"
        ]
        parameters: list[str] = [job_id]
        if preparation_id is not None:
            clauses.append("(kind = 'release-preparation-delete' AND subject_id = ?)")
            parameters.append(preparation_id)
        row = connection.execute(
            "SELECT 1 FROM maintenance_operations "
            "WHERE phase NOT IN ('FINALIZED', 'ROLLED_BACK') AND ("
            + " OR ".join(clauses)
            + ") LIMIT 1",
            parameters,
        ).fetchone()
        if row is not None:
            raise StateConflictError(
                "destructive maintenance intent blocks release mutation"
            )

    def delete(
        self,
        preparation_id: str,
        *,
        expected_version: int,
        cleanup: CleanupCallback | None = None,
        maintenance_operation_id: str | None = None,
    ) -> None:
        """Delete one inactive preparation with its FS detach inside the CAS.

        ``cleanup`` must only perform a short, atomic quarantine operation.  It
        may return a rollback callback, which is invoked if the SQLite write or
        commit fails after the quarantine succeeds.
        """

        rollbacks: list[CleanupRollback] = []
        try:
            with self.database._write() as connection:
                row = connection.execute(
                    "SELECT state, version FROM release_preparations WHERE id = ?",
                    (preparation_id,),
                ).fetchone()
                if row is None:
                    raise NotFoundError(
                        f"release preparation not found: {preparation_id}"
                    )
                if int(row["version"]) != expected_version:
                    raise StateConflictError(
                        f"release preparation version is {row['version']}, "
                        f"expected {expected_version}"
                    )
                if ReleasePreparationState(row["state"]) in self._active_states():
                    raise StateConflictError(
                        "active release preparation cannot be deleted"
                    )
                if cleanup is not None and maintenance_operation_id is not None:
                    raise ValueError(
                        "cleanup and maintenance_operation_id are mutually exclusive"
                    )
                if cleanup is not None:
                    rollback = cleanup()
                    if rollback is not None:
                        rollbacks.append(rollback)
                cursor = connection.execute(
                    "DELETE FROM release_preparations WHERE id = ? AND version = ?",
                    (preparation_id, expected_version),
                )
                if cursor.rowcount != 1:
                    raise StateConflictError("release preparation changed concurrently")
                if maintenance_operation_id is not None:
                    self.database._mark_maintenance_committed(
                        connection,
                        maintenance_operation_id,
                        kind="release-preparation-delete",
                        subject_id=preparation_id,
                    )
        except BaseException:
            self._run_rollbacks(rollbacks)
            raise

    def delete_completed_release(
        self,
        job_id: str,
        *,
        expected_versions: Mapping[str, int],
        cleanup: CleanupCallback | None = None,
        maintenance_operation_id: str | None = None,
        message: str = "completed public release was explicitly deleted",
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Atomically tombstone a completed release and all preparations.

        The supplied ``{preparation_id: version}`` map must exactly match the
        current set.  This makes a preparation created or changed after the UI
        snapshot a hard conflict before any filesystem quarantine is touched.
        """

        if any(
            not isinstance(key, str) or type(value) is not int or value < 1
            for key, value in expected_versions.items()
        ):
            raise ValueError("invalid release preparation version snapshot")
        snapshot = dict(expected_versions)
        if len(snapshot) != len(expected_versions):
            raise ValueError("invalid release preparation version snapshot")
        rollbacks: list[CleanupRollback] = []
        try:
            with self.database._write() as connection:
                job = connection.execute(
                    "SELECT state FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if job is None:
                    raise NotFoundError(f"job not found: {job_id}")
                current_job_state = JobState(job["state"])
                if current_job_state is not JobState.COMPLETED:
                    raise StateConflictError(
                        "only a completed job can delete its public release",
                        current=current_job_state,
                    )
                if (
                    connection.execute(
                        "SELECT 1 FROM events WHERE job_id = ? AND kind = ? LIMIT 1",
                        (job_id, _COMPLETED_RELEASE_DELETED_EVENT),
                    ).fetchone()
                    is not None
                ):
                    raise StateConflictError(
                        "the completed public release was already deleted",
                        current=current_job_state,
                    )
                rows = connection.execute(
                    "SELECT id, state, version FROM release_preparations WHERE job_id = ?",
                    (job_id,),
                ).fetchall()
                actual = {str(row["id"]): int(row["version"]) for row in rows}
                if actual != snapshot:
                    raise StateConflictError(
                        "release preparation snapshot changed concurrently",
                        current=current_job_state,
                    )
                if any(
                    ReleasePreparationState(row["state"]) in self._active_states()
                    for row in rows
                ):
                    raise StateConflictError(
                        "active release preparation prevents deletion",
                        current=current_job_state,
                    )
                if cleanup is not None and maintenance_operation_id is not None:
                    raise ValueError(
                        "cleanup and maintenance_operation_id are mutually exclusive"
                    )
                if cleanup is not None:
                    rollback = cleanup()
                    if rollback is not None:
                        rollbacks.append(rollback)
                cursor = connection.execute(
                    "DELETE FROM release_preparations WHERE job_id = ?", (job_id,)
                )
                if cursor.rowcount != len(snapshot):
                    raise StateConflictError(
                        "release preparations changed concurrently",
                        current=current_job_state,
                    )
                self.database._insert_event(
                    connection,
                    job_id=job_id,
                    kind=_COMPLETED_RELEASE_DELETED_EVENT,
                    message=message,
                    payload=payload,
                )
                if maintenance_operation_id is not None:
                    self.database._mark_maintenance_committed(
                        connection,
                        maintenance_operation_id,
                        kind="completed-release-delete",
                        subject_id=job_id,
                    )
        except BaseException:
            self._run_rollbacks(rollbacks)
            raise

    def recover_interrupted(
        self,
        *,
        cleanup_preparing: RecoveryCleanupCallback | None = None,
        maintenance_operation_id: str | None = None,
    ) -> tuple[ReleasePreparation, ...]:
        """Resolve operation leases left behind by a previous service process."""

        recoverable = {
            ReleasePreparationState.PREPARING: ReleasePreparationState.FAILED,
            ReleasePreparationState.SEEDING_CHECK: ReleasePreparationState.UNKNOWN,
            ReleasePreparationState.SEEDING: ReleasePreparationState.UNKNOWN,
            ReleasePreparationState.PUBLISHING: ReleasePreparationState.UNKNOWN,
        }
        rollbacks: list[CleanupRollback] = []
        recovered_ids: list[str] = []
        try:
            with self.database._write() as connection:
                placeholders = ",".join("?" for _ in recoverable)
                rows = connection.execute(
                    f"SELECT * FROM release_preparations WHERE state IN ({placeholders}) "
                    "ORDER BY created_at, id",
                    tuple(state.value for state in recoverable),
                ).fetchall()
                for row in rows:
                    record = self._decode(row)
                    if (
                        cleanup_preparing is not None
                        and maintenance_operation_id is not None
                    ):
                        raise ValueError(
                            "cleanup_preparing and maintenance_operation_id are "
                            "mutually exclusive"
                        )
                    if (
                        record.state is ReleasePreparationState.PREPARING
                        and cleanup_preparing is not None
                    ):
                        rollback = cleanup_preparing(record)
                        if rollback is not None:
                            rollbacks.append(rollback)
                    target = recoverable[record.state]
                    cursor = connection.execute(
                        """
                        UPDATE release_preparations
                        SET state = ?, version = version + 1, updated_at = ?, error = ?
                        WHERE id = ? AND state = ? AND version = ?
                        """,
                        (
                            target.value,
                            utc_now(),
                            "interrupted release operation recovered at service startup",
                            record.id,
                            record.state.value,
                            record.version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise StateConflictError(
                            "release preparation changed during startup recovery"
                        )
                    self._event(
                        connection,
                        record.id,
                        "release.preparation.interrupted-recovered",
                        target,
                        message="interrupted operation resolved without automatic retry",
                        payload={
                            "from": record.state.value,
                            "to": target.value,
                        },
                    )
                    recovered_ids.append(record.id)
                recovered_rows = [
                    connection.execute(
                        "SELECT * FROM release_preparations WHERE id = ?", (identifier,)
                    ).fetchone()
                    for identifier in recovered_ids
                ]
                if maintenance_operation_id is not None:
                    self.database._mark_maintenance_committed(
                        connection,
                        maintenance_operation_id,
                        kind="interrupted-release-cleanup",
                        subject_id="startup",
                    )
        except BaseException:
            self._run_rollbacks(rollbacks)
            raise
        return tuple(self._decode(row) for row in recovered_rows if row is not None)

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        preparation_id: str,
        kind: str,
        state: ReleasePreparationState,
        *,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO release_preparation_events(
                preparation_id, kind, state, message, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                preparation_id,
                kind,
                state.value,
                message,
                json.dumps(payload or {}, separators=(",", ":"), sort_keys=True),
                utc_now(),
            ),
        )


__all__ = ["ReleasePreparation", "ReleaseStore"]
