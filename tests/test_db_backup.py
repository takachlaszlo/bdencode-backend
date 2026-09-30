from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import bdencode.db_backup as db_backup
from bdencode.db import MIGRATIONS, SCHEMA_VERSION, Database, PersistenceError
from bdencode.db_backup import (
    BackupError,
    create_backup,
    ensure_scheduled_backup,
    list_backups,
    prune_backups,
    restore_backup,
    retention_group,
    verify_backup,
)
from bdencode.models import JobCreate
from bdencode.queue import JobQueue


def make_database(path: Path, *jobs: str) -> Database:
    database = Database(path)
    database.initialize()
    queue = JobQueue(database)
    for name in jobs:
        queue.enqueue(JobCreate(source_path=f"/storage/{name}", name=name))
    return database


def job_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM jobs")}


def schema_one_fixture(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
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
                'QUEUED', 0, '{}', NULL, NULL, NULL, NULL, NULL, NULL,
                1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', NULL, NULL
            );
            """
        )
        connection.commit()


# -- creating and reading backups ----------------------------------------------------


def test_backup_is_a_self_contained_verified_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "state" / "encoder.sqlite3"
    database = make_database(source, "Alpha", "Beta")

    info = create_backup(source, tmp_path / "backups", label="manual")

    assert info.verified and info.schema_version == SCHEMA_VERSION and info.jobs == 2
    assert info.path.parent == tmp_path / "backups"
    assert len(info.sha256 or "") == 64 and info.size_bytes == info.path.stat().st_size
    assert job_names(info.path) == {"Alpha", "Beta"}
    assert not Path(f"{info.path}-wal").exists() and not Path(f"{info.path}-shm").exists()
    with closing(sqlite3.connect(info.path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    manifest = info.path.with_name(info.name + ".json")
    assert manifest.is_file()
    assert not list((tmp_path / "backups").glob(".*partial"))
    database.close()


def test_backup_rejects_bad_labels_and_missing_databases(tmp_path: Path) -> None:
    source = tmp_path / "encoder.sqlite3"
    make_database(source)
    for label in ("", "Upper", "has space", "../escape", "x" * 41):
        with pytest.raises(BackupError):
            create_backup(source, tmp_path / "b", label=label)
    with pytest.raises(BackupError, match="does not exist"):
        create_backup(tmp_path / "missing.sqlite3", tmp_path / "b")


def test_a_failed_copy_leaves_no_file_under_a_valid_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "encoder.sqlite3"
    make_database(source, "Alpha")
    real = db_backup.os.replace

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(db_backup.os, "replace", broken)
    with pytest.raises(BackupError, match="disk full"):
        create_backup(source, tmp_path / "backups")
    monkeypatch.setattr(db_backup.os, "replace", real)
    assert list((tmp_path / "backups").iterdir()) == []


def test_list_backups_is_newest_first_and_ignores_strays(tmp_path: Path) -> None:
    source = tmp_path / "encoder.sqlite3"
    make_database(source)
    directory = tmp_path / "backups"
    base = datetime(2026, 5, 1, 12, tzinfo=UTC)
    names = [
        create_backup(source, directory, label="scheduled", now=base + timedelta(hours=n)).name
        for n in range(3)
    ]
    (directory / "notes.txt").write_text("hello", encoding="utf-8")
    (directory / "encoder-20260101T000000Z-manual-deadbeef.sqlite3").write_bytes(b"x")  # no manifest
    orphan = create_backup(source, directory, label="manual", now=base)
    (directory / (orphan.name + ".json")).write_text("{not json", encoding="utf-8")

    listed = [item.name for item in list_backups(directory)]
    assert listed == list(reversed(names))
    assert list_backups(tmp_path / "does-not-exist") == []


# -- verification ---------------------------------------------------------------------------


def test_verify_detects_tampering_corruption_and_unsupported_schemas(tmp_path: Path) -> None:
    source = tmp_path / "encoder.sqlite3"
    make_database(source, "Alpha")
    info = create_backup(source, tmp_path / "backups")
    assert verify_backup(info.path).sha256 == info.sha256

    tampered = info.path.read_bytes()
    info.path.write_bytes(tampered[:-1] + bytes([tampered[-1] ^ 0xFF]))
    with pytest.raises(BackupError, match="checksum"):
        verify_backup(info.path)

    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"this is not a database" * 100)
    with pytest.raises(BackupError):
        verify_backup(garbage)

    future = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(future)) as connection:
        connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO schema_meta VALUES ('schema_version', '99')")
        connection.commit()
    with pytest.raises(BackupError, match="unsupported schema 99"):
        verify_backup(future)
    with pytest.raises(BackupError, match="does not exist"):
        verify_backup(tmp_path / "nope.sqlite3")


def test_an_external_copy_without_a_manifest_can_still_be_verified(tmp_path: Path) -> None:
    source = tmp_path / "encoder.sqlite3"
    make_database(source, "Alpha")
    info = create_backup(source, tmp_path / "backups")
    copy = tmp_path / "moved-by-hand.sqlite3"
    copy.write_bytes(info.path.read_bytes())
    checked = verify_backup(copy)
    assert checked.label == "external" and checked.jobs == 1


# -- retention -----------------------------------------------------------------------------------


def test_retention_is_per_kind_and_newest_survive(tmp_path: Path) -> None:
    source = tmp_path / "encoder.sqlite3"
    make_database(source)
    directory = tmp_path / "backups"
    base = datetime(2026, 5, 1, tzinfo=UTC)
    created = {
        label: [
            create_backup(source, directory, label=label, now=base + timedelta(hours=n))
            for n in range(6)
        ]
        for label in ("scheduled", "pre-migration-v1")
    }
    removed = prune_backups(directory, keep={"scheduled": 4, "pre-migration": 2})
    assert len(removed) == 6  # two scheduled + four pre-migration
    kept = {item.name for item in list_backups(directory)}
    assert kept == {
        *(item.name for item in created["scheduled"][2:]),
        *(item.name for item in created["pre-migration-v1"][4:]),
    }
    for name in removed:
        assert not (directory / name).exists()
        assert not (directory / (name + ".json")).exists()
    assert retention_group("pre-migration-v2") == "pre-migration"
    assert retention_group("scheduled") == "scheduled"


def test_scheduled_backup_respects_its_interval_and_prunes(tmp_path: Path) -> None:
    source = tmp_path / "encoder.sqlite3"
    make_database(source)
    directory = tmp_path / "backups"
    start = datetime(2026, 6, 1, 3, tzinfo=UTC)
    interval = timedelta(hours=24)

    first = ensure_scheduled_backup(source, directory, interval=interval, now=start)
    assert first is not None and first.label == "scheduled"
    assert ensure_scheduled_backup(source, directory, interval=interval, now=start + timedelta(hours=23)) is None
    second = ensure_scheduled_backup(source, directory, interval=interval, now=start + timedelta(hours=25))
    assert second is not None and second.name != first.name

    for day in range(2, 8):
        ensure_scheduled_backup(
            source, directory, interval=interval, keep={"scheduled": 3}, now=start + timedelta(days=day)
        )
    assert len([i for i in list_backups(directory) if i.label == "scheduled"]) == 3


# -- restore ---------------------------------------------------------------------------------------


def test_restore_replaces_the_database_and_keeps_a_safety_copy(tmp_path: Path) -> None:
    source = tmp_path / "state" / "encoder.sqlite3"
    database = make_database(source, "Alpha")
    backup = create_backup(source, tmp_path / "state" / "backups", label="manual")
    JobQueue(database).enqueue(JobCreate(source_path="/storage/Beta", name="Beta"))
    database.close()
    assert job_names(source) == {"Alpha", "Beta"}
    Path(f"{source}-wal").write_bytes(b"stale wal that must not be replayed")

    outcome = restore_backup(backup.path, source, safety_directory=tmp_path / "state" / "backups")

    assert outcome["restored"] == backup.name and outcome["jobs"] == 1
    assert job_names(source) == {"Alpha"}
    assert not Path(f"{source}-wal").exists() and not Path(f"{source}-shm").exists()
    safety = next(i for i in list_backups(tmp_path / "state" / "backups") if i.label == "pre-restore")
    assert outcome["safety_backup"] == safety.name
    assert job_names(safety.path) == {"Alpha", "Beta"}
    # The restored file is a normal database the application can open.
    reopened = Database(source)
    reopened.initialize()
    assert reopened.integrity_check() == ["ok"]


def test_restore_refuses_a_bad_backup_and_does_not_touch_the_database(tmp_path: Path) -> None:
    source = tmp_path / "encoder.sqlite3"
    make_database(source, "Alpha")
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"nope" * 200)
    with pytest.raises(BackupError):
        restore_backup(bad, source, safety_directory=tmp_path / "backups")
    assert job_names(source) == {"Alpha"}
    assert not (tmp_path / "backups").exists()


def test_restoring_an_older_schema_is_migrated_on_the_next_start(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.sqlite3"
    schema_one_fixture(legacy)
    info = create_backup(legacy, tmp_path / "old-backups")
    assert info.schema_version == 1

    target = tmp_path / "state" / "encoder.sqlite3"
    restore_backup(info.path, target, safety_directory=tmp_path / "state" / "backups")
    database = Database(target)
    database.initialize()

    assert database.schema_version() == SCHEMA_VERSION
    assert database.get_job("legacy").name == "legacy"
    assert any(i.label.startswith("pre-migration") for i in list_backups(tmp_path / "state" / "backups"))


# -- migration bookkeeping ----------------------------------------------------------------------------


def test_fresh_databases_record_their_creation_once(tmp_path: Path) -> None:
    path = tmp_path / "state" / "encoder.sqlite3"
    database = make_database(path)
    database.initialize()
    Database(path).initialize()
    history = database.migration_history()
    assert len(history) == 1
    assert history[0]["kind"] == "create" and history[0]["from_version"] is None
    assert history[0]["to_version"] == SCHEMA_VERSION and history[0]["backup_name"] is None
    assert not (tmp_path / "state" / "backups").exists(), "a new database needs no backup"


def test_migration_takes_a_backup_first_and_records_it(tmp_path: Path) -> None:
    path = tmp_path / "state" / "encoder.sqlite3"
    path.parent.mkdir()
    schema_one_fixture(path)

    database = Database(path)
    database.initialize()

    (record,) = database.migration_history()
    assert (record["kind"], record["from_version"], record["to_version"]) == ("migrate", 1, 2)
    backup = path.parent / "backups" / record["backup_name"]
    assert record["backup_name"].startswith("encoder-") and "pre-migration-v1" in record["backup_name"]
    assert verify_backup(backup).schema_version == 1, "the backup must be the pre-migration state"
    assert job_names(backup) == {"legacy"}
    Database(path).initialize()
    assert len(database.migration_history()) == 1


def test_a_failing_backup_does_not_block_the_atomic_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state" / "encoder.sqlite3"
    path.parent.mkdir()
    schema_one_fixture(path)

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise BackupError("backups directory is read-only")

    monkeypatch.setattr(db_backup, "create_backup", unavailable)
    database = Database(path)
    database.initialize()

    assert database.schema_version() == SCHEMA_VERSION
    (record,) = database.migration_history()
    assert record["backup_name"] is None and record["kind"] == "migrate"


def test_unknown_schema_versions_are_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "encoder.sqlite3"
    make_database(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE schema_meta SET value = '7' WHERE key = 'schema_version'")
        connection.commit()
    with pytest.raises(PersistenceError, match="unsupported database schema 7; expected 1 or 2"):
        Database(path).initialize()
    assert set(MIGRATIONS) == {1} and SCHEMA_VERSION == 2


def test_database_backup_helper_applies_retention_and_rejects_memory(tmp_path: Path) -> None:
    database = make_database(tmp_path / "state" / "encoder.sqlite3", "Alpha")
    first = database.backup("manual")
    assert first.path.parent == tmp_path / "state" / "backups"
    for _ in range(12):
        database.backup("manual")
    assert len([i for i in list_backups(database.backup_directory) if i.label == "manual"]) == 10
    with pytest.raises(PersistenceError, match="in-memory"):
        Database(":memory:").backup()
    assert database.integrity_check() == ["ok"]
