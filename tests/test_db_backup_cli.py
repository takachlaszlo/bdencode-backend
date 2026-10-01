from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from bdencode.cli import main
from bdencode.config import Settings
from bdencode.db import Database
from bdencode.models import JobCreate
from bdencode.queue import JobQueue


@pytest.fixture
def config(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "storage"
    source.mkdir()
    data = tmp_path / "encode"
    path = tmp_path / "config.toml"
    path.write_text(
        "[bdencode]\n"
        f'data_root = "{data.as_posix()}"\n'
        f'source_roots = ["{source.as_posix()}"]\n',
        encoding="utf-8",
    )
    database = Database(data / "state" / "encoder.sqlite3")
    database.initialize()
    queue = JobQueue(database)
    queue.enqueue(JobCreate(source_path="/storage/Alpha", name="Alpha"))
    database.close()
    return path, data / "state"


def run(capsys: pytest.CaptureFixture[str], path: Path, *arguments: str) -> tuple[int, str, str]:
    code = main(["--config", str(path), *arguments])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def job_names(state: Path) -> set[str]:
    with closing(sqlite3.connect(state / "encoder.sqlite3")) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM jobs")}


def test_status_reports_schema_integrity_history_and_backups(config, capsys) -> None:
    path, _state = config
    code, out, _err = run(capsys, path, "db-status")
    assert code == 0
    report = json.loads(out)
    assert report["schema_version"] == 2 and report["integrity"] == ["ok"]
    assert report["migrations"][0]["kind"] == "create"
    assert report["backups"] == 0 and report["latest_backup"] is None
    assert report["size_bytes"] > 0


def test_backup_then_list_and_verify(config, capsys) -> None:
    path, state = config
    code, out, _ = run(capsys, path, "db-backup", "--label", "before-upgrade")
    assert code == 0
    created = json.loads(out)
    assert created["label"] == "before-upgrade" and created["verified"] and created["jobs"] == 1
    assert (state / "backups" / created["name"]).is_file()

    code, out, _ = run(capsys, path, "db-backups", "--verify")
    assert code == 0
    listing = json.loads(out)
    assert [item["name"] for item in listing["backups"]] == [created["name"]]
    assert listing["backups"][0]["verified"] is True

    (state / "backups" / created["name"]).write_bytes(b"corrupted")
    code, out, _ = run(capsys, path, "db-backups", "--verify")
    assert code == 1
    entry = json.loads(out)["backups"][0]
    assert entry["verified"] is False and "checksum" in entry["error"]

    code, _out, err = run(capsys, path, "db-backup", "--label", "Bad Label")
    assert code == 1 and "label" in err


def test_restore_needs_confirmation_and_an_idle_queue(config, capsys) -> None:
    path, state = config
    _, out, _ = run(capsys, path, "db-backup")
    name = json.loads(out)["name"]

    code, _out, err = run(capsys, path, "db-restore", name)
    assert code == 2 and "--yes" in err

    database = Database(state / "encoder.sqlite3")
    queue = JobQueue(database)
    queue.claim_next()  # the queued job now owns the pipeline
    database.close()
    code, _out, err = run(capsys, path, "db-restore", name, "--yes")
    assert code == 3 and "still owns the pipeline" in err


def test_restore_brings_back_the_backed_up_state_and_keeps_a_safety_copy(config, capsys) -> None:
    path, state = config
    _, out, _ = run(capsys, path, "db-backup")
    name = json.loads(out)["name"]

    database = Database(state / "encoder.sqlite3")
    JobQueue(database).enqueue(JobCreate(source_path="/storage/Beta", name="Beta"))
    database.close()
    assert job_names(state) == {"Alpha", "Beta"}

    # A job that owns the pipeline blocks the restore unless --force is given.
    database = Database(state / "encoder.sqlite3")
    JobQueue(database).claim_next()
    database.close()
    code, _out, _err = run(capsys, path, "db-restore", name, "--yes", "--force")

    assert code == 0
    outcome = json.loads(_out)
    assert outcome["restored"] == name and outcome["jobs"] == 1
    assert job_names(state) == {"Alpha"}
    safety = state / "backups" / outcome["safety_backup"]
    assert safety.is_file() and job_names.__call__(state) == {"Alpha"}
    with closing(sqlite3.connect(safety)) as connection:
        assert {row[0] for row in connection.execute("SELECT name FROM jobs")} == {"Alpha", "Beta"}
    # The restored database opens normally.
    code, out, _ = run(capsys, path, "db-status")
    assert code == 0 and json.loads(out)["integrity"] == ["ok"]


def test_restore_rejects_an_unknown_or_damaged_backup(config, capsys, tmp_path: Path) -> None:
    path, state = config
    code, _out, err = run(capsys, path, "db-restore", "encoder-does-not-exist.sqlite3", "--yes")
    assert code == 1 and "does not exist" in err
    damaged = tmp_path / "damaged.sqlite3"
    damaged.write_bytes(b"not a database" * 50)
    code, _out, err = run(capsys, path, "db-restore", str(damaged), "--yes")
    assert code == 1
    assert job_names(state) == {"Alpha"}, "a refused restore must not touch the database"


def test_backup_settings_are_validated_and_loaded(tmp_path: Path) -> None:
    source = tmp_path / "s"
    source.mkdir()
    settings = Settings(data_root=tmp_path / "d", source_roots=(source,)).validate()
    assert settings.backup_interval_hours == 24 and settings.backup_keep_scheduled == 14
    from bdencode.config import ConfigurationError, load_settings

    for field, value in (("backup_interval_hours", 999), ("backup_interval_hours", -1), ("backup_keep_scheduled", 0), ("backup_keep_scheduled", 61)):
        with pytest.raises(ConfigurationError):
            Settings(data_root=tmp_path / "d", source_roots=(source,), **{field: value}).validate()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[bdencode]\ndata_root = "{(tmp_path / "d").as_posix()}"\nsource_roots = ["{source.as_posix()}"]\n'
        "backup_interval_hours = 6\nbackup_keep_scheduled = 3\n",
        encoding="utf-8",
    )
    loaded = load_settings(config, env={})
    assert (loaded.backup_interval_hours, loaded.backup_keep_scheduled) == (6, 3)
    override = load_settings(config, env={"BDENCODE_BACKUP_INTERVAL_HOURS": "0"})
    assert override.backup_interval_hours == 0
