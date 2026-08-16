from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from bdencode.db import Database, StateConflictError
from bdencode.models import EventCreate, JobCreate
from bdencode.release.models import ReleaseMetadata, ReleasePreparationState
from bdencode.release_store import ReleaseStore


def _metadata() -> ReleaseMetadata:
    return ReleaseMetadata(
        release_name="Example.Movie.2026.1080p.BluRay.x264-GROUP",
        title="Example Movie",
        year=2026,
        category="Movie",
        source_media="BluRay",
        resolution="1080p",
        video_codec="x264",
        audio_codecs=("FLAC",),
        languages=("en",),
    )


def _store(tmp_path: Path) -> tuple[Database, ReleaseStore, str]:
    database = Database(tmp_path / "encoder.sqlite3")
    job = database.create_job(JobCreate(source_path=str(tmp_path / "source")))
    with database._write() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'COMPLETED' WHERE id = ?", (job.id,)
        )
    return database, ReleaseStore(database), job.id


def _create(store: ReleaseStore, job_id: str, *, name: str = "movie.mkv"):
    return store.create(
        job_id=job_id,
        profile_id="example",
        profile_digest="a" * 64,
        metadata=_metadata(),
        payload_name=name,
        payload_path=f"C:/completed/{name}",
        payload_size=123,
        payload_sha256="b" * 64,
    )


def test_release_preparation_round_trip_and_versioned_transition(
    tmp_path: Path,
) -> None:
    _database, store, job_id = _store(tmp_path)
    created = _create(
        store,
        job_id,
        name="Example.Movie.2026.1080p.BluRay.x264-GROUP.mkv",
    )

    preparing = store.transition(
        created.id,
        ReleasePreparationState.PREPARING,
        expected_version=created.version,
    )
    ready = store.transition(
        created.id,
        ReleasePreparationState.READY,
        expected_version=preparing.version,
        values={
            "manifest_sha256": "c" * 64,
            "torrent_infohash": "d" * 40,
            "torrent_sha256": "e" * 64,
        },
    )

    assert ready.version == 3
    assert ready.state is ReleasePreparationState.READY
    assert store.list_for_job(job_id) == (ready,)


def test_stale_release_version_and_illegal_transition_fail_closed(
    tmp_path: Path,
) -> None:
    _database, store, job_id = _store(tmp_path)
    created = _create(store, job_id)

    with pytest.raises(StateConflictError, match="cannot transition"):
        store.transition(
            created.id,
            ReleasePreparationState.PUBLISHED,
            expected_version=created.version,
        )
    store.transition(
        created.id,
        ReleasePreparationState.PREPARING,
        expected_version=created.version,
    )
    with pytest.raises(StateConflictError, match="version is"):
        store.transition(
            created.id,
            ReleasePreparationState.FAILED,
            expected_version=created.version,
        )


def test_release_records_cascade_when_job_is_deleted(tmp_path: Path) -> None:
    database, store, job_id = _store(tmp_path)
    created = _create(store, job_id)
    # The persistence primitive is enough here; API deletion performs the
    # filesystem quarantine before invoking it.
    database.delete_terminal_job(job_id, allow_completed=True)

    assert store.list_for_job(job_id) == ()
    with pytest.raises(Exception, match="not found"):
        store.get(created.id)


def test_create_checks_completed_state_and_release_deletion_tombstone(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "encoder.sqlite3")
    job = database.create_job(JobCreate(source_path=str(tmp_path / "source")))
    store = ReleaseStore(database)

    with pytest.raises(StateConflictError, match="completed job"):
        _create(store, job.id)

    with database._write() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'COMPLETED' WHERE id = ?", (job.id,)
        )
    database.add_event(
        EventCreate(job_id=job.id, kind="job.completed-release-deleted")
    )
    with pytest.raises(StateConflictError, match="explicitly deleted"):
        _create(store, job.id)


def test_delete_checks_version_and_active_state_before_cleanup(tmp_path: Path) -> None:
    _database, store, job_id = _store(tmp_path)
    created = _create(store, job_id)
    calls = 0

    def cleanup():
        nonlocal calls
        calls += 1
        return None

    with pytest.raises(StateConflictError, match="version is"):
        store.delete(created.id, expected_version=created.version + 1, cleanup=cleanup)
    assert calls == 0

    preparing = store.transition(
        created.id,
        ReleasePreparationState.PREPARING,
        expected_version=created.version,
    )
    with pytest.raises(StateConflictError, match="active"):
        store.delete(preparing.id, expected_version=preparing.version, cleanup=cleanup)
    assert calls == 0


def test_bulk_delete_requires_exact_snapshot_and_writes_tombstone(
    tmp_path: Path,
) -> None:
    database, store, job_id = _store(tmp_path)
    first = _create(store, job_id, name="one.mkv")
    second = _create(store, job_id, name="two.mkv")
    cleanup_calls = 0

    def cleanup():
        nonlocal cleanup_calls
        cleanup_calls += 1
        return None

    with pytest.raises(StateConflictError, match="snapshot changed"):
        store.delete_completed_release(
            job_id,
            expected_versions={first.id: first.version},
            cleanup=cleanup,
        )
    assert cleanup_calls == 0

    store.delete_completed_release(
        job_id,
        expected_versions={first.id: first.version, second.id: second.version},
        cleanup=cleanup,
        payload={"sha256": "b" * 64},
    )
    assert cleanup_calls == 1
    assert store.list_for_job(job_id) == ()
    assert [event.kind for event in database.list_events(job_id=job_id)].count(
        "job.completed-release-deleted"
    ) == 1
    with pytest.raises(StateConflictError, match="explicitly deleted"):
        _create(store, job_id)


def test_bulk_delete_rolls_back_cleanup_when_database_write_fails(
    tmp_path: Path,
) -> None:
    database, store, job_id = _store(tmp_path)
    created = _create(store, job_id)
    cleanup_calls = 0
    rollback_calls = 0

    def cleanup():
        nonlocal cleanup_calls
        cleanup_calls += 1

        def rollback() -> None:
            nonlocal rollback_calls
            rollback_calls += 1

        return rollback

    with database._write() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_bulk_release_delete
            BEFORE DELETE ON release_preparations
            BEGIN SELECT RAISE(ABORT, 'injected bulk delete failure'); END
            """
        )
    with pytest.raises(sqlite3.DatabaseError, match="injected bulk delete failure"):
        store.delete_completed_release(
            job_id,
            expected_versions={created.id: created.version},
            cleanup=cleanup,
        )

    assert cleanup_calls == 1
    assert rollback_calls == 1
    assert store.get(created.id) == created
    assert "job.completed-release-deleted" not in {
        event.kind for event in database.list_events(job_id=job_id)
    }


def test_startup_recovery_resolves_every_operation_lease(tmp_path: Path) -> None:
    database, store, job_id = _store(tmp_path)
    preparing = store.transition(
        _create(store, job_id, name="preparing.mkv").id,
        ReleasePreparationState.PREPARING,
        expected_version=1,
    )

    checking_base = store.transition(
        _create(store, job_id, name="checking.mkv").id,
        ReleasePreparationState.PREPARING,
        expected_version=1,
    )
    checking_ready = store.transition(
        checking_base.id,
        ReleasePreparationState.READY,
        expected_version=checking_base.version,
    )
    checking = store.transition(
        checking_ready.id,
        ReleasePreparationState.SEEDING_CHECK,
        expected_version=checking_ready.version,
    )

    seeding_base = store.transition(
        _create(store, job_id, name="seeding.mkv").id,
        ReleasePreparationState.PREPARING,
        expected_version=1,
    )
    seeding_ready = store.transition(
        seeding_base.id,
        ReleasePreparationState.READY,
        expected_version=seeding_base.version,
    )
    seeding = store.transition(
        seeding_ready.id,
        ReleasePreparationState.SEEDING,
        expected_version=seeding_ready.version,
    )

    publishing_base = store.transition(
        _create(store, job_id, name="publishing.mkv").id,
        ReleasePreparationState.PREPARING,
        expected_version=1,
    )
    publishing_ready = store.transition(
        publishing_base.id,
        ReleasePreparationState.READY,
        expected_version=publishing_base.version,
    )
    publishable = store.transition(
        publishing_ready.id,
        ReleasePreparationState.READY_TO_PUBLISH,
        expected_version=publishing_ready.version,
    )
    publishing = store.transition(
        publishable.id,
        ReleasePreparationState.PUBLISHING,
        expected_version=publishable.version,
    )

    cleaned: list[str] = []
    recovered = store.recover_interrupted(
        cleanup_preparing=lambda record: cleaned.append(record.id) or None
    )
    states = {item.id: item.state for item in recovered}

    assert cleaned == [preparing.id]
    assert states == {
        preparing.id: ReleasePreparationState.FAILED,
        checking.id: ReleasePreparationState.UNKNOWN,
        seeding.id: ReleasePreparationState.UNKNOWN,
        publishing.id: ReleasePreparationState.UNKNOWN,
    }
    with database._read() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM release_preparation_events "
            "WHERE kind = 'release.preparation.interrupted-recovered'"
        ).fetchone()["count"]
    assert count == 4
