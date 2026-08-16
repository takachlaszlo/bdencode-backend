from __future__ import annotations

import importlib.util
from contextlib import closing
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).parents[1] / "install" / "install_transaction.py"
INSTALLER_PATH = Path(__file__).parents[1] / "install" / "install.sh"
SPEC = importlib.util.spec_from_file_location("bdencode_install_transaction", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
install_transaction = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = install_transaction
SPEC.loader.exec_module(install_transaction)

TRANSACTION_ID = "20260802T123456Z-42"


def test_system_target_allowlist_covers_apt_recovery_dropins() -> None:
    targets = {path.as_posix() for path in install_transaction.SYSTEM_TARGETS}
    assert (
        "/etc/systemd/system/apt-daily.service.d/bdencode-recovery.conf"
        in targets
    )
    assert (
        "/etc/systemd/system/apt-daily-upgrade.service.d/bdencode-recovery.conf"
        in targets
    )
    assert "/var/www/bdencode/current" in targets


def test_installer_prepares_database_rollback_before_live_migration() -> None:
    installer = INSTALLER_PATH.read_text(encoding="utf-8")
    begin = installer.index("bdencode-install-transaction begin")
    worker_stopped = installer.index('worker_state="$(sudo systemctl show', begin)
    prepare = installer.index("bdencode-install-transaction prepare", worker_stopped)
    migration = installer.index('"$app_root/current/venv/bin/bdencode" init-db', prepare)

    assert '--database-path "$database_path"' in installer[begin:worker_stopped]
    assert begin < worker_stopped < prepare < migration


def test_stable_recovery_bootstrap_is_outside_rollback_allowlist() -> None:
    targets = {path.as_posix() for path in install_transaction.SYSTEM_TARGETS}
    assert "/usr/local/libexec/bdencode-update-runtime" not in targets
    assert "/usr/local/libexec/bdencode-apt-transaction" not in targets
    assert "/usr/local/libexec/bdencode-install-transaction" not in targets
    assert (
        "/etc/systemd/system/bdencode-api.service.d/bdencode-recovery.conf"
        not in targets
    )


def helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[object, Path, Path, Path]:
    monkeypatch.setenv("BDENCODE_INSTALL_TESTING", "1")
    state_root = tmp_path / "journal"
    state_root.mkdir(mode=0o700)
    app_root = tmp_path / "data" / "app"
    (app_root / "tools").mkdir(parents=True)
    existing = tmp_path / "system" / "existing.conf"
    absent = tmp_path / "system" / "created-by-installer.conf"
    existing.parent.mkdir()
    existing.write_bytes(b"old configuration\n")
    os.chmod(existing, 0o640)
    transaction = install_transaction.InstallTransaction(
        state_root, fixed_targets=(existing, absent)
    )
    transaction.initialize()
    return transaction, app_root, existing, absent


def create_v1_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_meta VALUES ('schema_version', '1')"
        )
        connection.execute(
            "CREATE TABLE payload (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO payload(value) VALUES ('before-upgrade')")
        connection.commit()


def test_transaction_id_is_strictly_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, _app_root, _existing, _absent = helper(tmp_path, monkeypatch)
    assert transaction.transaction_dir(TRANSACTION_ID) == tmp_path / "journal" / TRANSACTION_ID
    with pytest.raises(
        install_transaction.InstallTransactionError, match="Invalid installer transaction id"
    ):
        transaction.transaction_dir("../../etc")


def test_recovery_restores_files_and_removes_new_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, existing, absent = helper(tmp_path, monkeypatch)
    old_pointer = app_root / "current"
    old_pointer.write_bytes(b"old application pointer")
    os.chmod(old_pointer, 0o600)

    transaction.begin(TRANSACTION_ID, app_root)
    transaction.prepare_mutation()
    existing.write_bytes(b"new configuration\n")
    os.chmod(existing, 0o666)
    absent.write_bytes(b"new file\n")
    old_pointer.write_bytes(b"new application pointer")
    tools_pointer = app_root / "tools" / "current"
    tools_pointer.write_bytes(b"new tools pointer")

    assert transaction.recover() is True
    assert existing.read_bytes() == b"old configuration\n"
    if os.name == "posix":
        assert stat.S_IMODE(existing.stat().st_mode) == 0o640
    assert not absent.exists()
    assert old_pointer.read_bytes() == b"old application pointer"
    if os.name == "posix":
        assert stat.S_IMODE(old_pointer.stat().st_mode) == 0o600
    assert not tools_pointer.exists()
    assert not transaction.active_file.exists()
    assert transaction.state(transaction.transaction_dir(TRANSACTION_ID)) == "RESTORED"


def test_recovery_accepts_legacy_manifest_without_database_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, _existing, _absent = helper(tmp_path, monkeypatch)
    transaction.begin(TRANSACTION_ID, app_root)
    transaction_dir = transaction.transaction_dir(TRANSACTION_ID)
    manifest = transaction.load_manifest(transaction_dir)
    manifest["schema"] = 1
    manifest.pop("database")
    transaction.write_manifest(transaction_dir, manifest)

    assert transaction.recover() is True
    assert not transaction.active_file.exists()
    assert transaction.state(transaction_dir) == "RESTORED"


def test_recovery_restores_database_before_old_release_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, existing, _absent = helper(tmp_path, monkeypatch)
    database = app_root.parent / "state" / "encoder.sqlite3"
    create_v1_database(database)
    old_pointer = app_root / "current"
    old_pointer.write_text("old release", encoding="utf-8")

    transaction.begin(
        TRANSACTION_ID,
        app_root,
        database_path=database,
    )
    transaction.prepare_mutation()
    transaction_dir = transaction.transaction_dir(TRANSACTION_ID)
    manifest = transaction.load_manifest(transaction_dir)
    assert manifest["database"]["kind"] == "file"
    assert manifest["database"]["schema_version"] == 1

    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE schema_meta SET value = '2' WHERE key = 'schema_version'"
        )
        connection.execute("UPDATE payload SET value = 'after-upgrade'")
        connection.execute("CREATE TABLE release_preparations (id TEXT PRIMARY KEY)")
        connection.commit()
    Path(f"{database}-wal").write_bytes(b"stale migrated WAL")
    Path(f"{database}-shm").write_bytes(b"stale migrated SHM")
    existing.write_text("candidate config", encoding="utf-8")
    old_pointer.write_text("new release", encoding="utf-8")

    observations: list[tuple[int, str]] = []
    restore_targets = transaction.restore

    def observe_database_first(
        directory: Path, current_manifest: dict[str, object]
    ) -> None:
        with closing(
            sqlite3.connect(transaction.sqlite_readonly_uri(database), uri=True)
        ) as connection:
            schema = int(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
            )
            value = str(connection.execute("SELECT value FROM payload").fetchone()[0])
        observations.append((schema, value))
        restore_targets(directory, current_manifest)

    monkeypatch.setattr(transaction, "restore", observe_database_first)

    assert transaction.recover() is True
    assert observations == [(1, "before-upgrade")]
    assert old_pointer.read_text(encoding="utf-8") == "old release"
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    assert transaction.recover() is False


def test_database_snapshot_includes_committed_wal_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, _existing, _absent = helper(tmp_path, monkeypatch)
    database = app_root.parent / "state" / "encoder.sqlite3"
    database.parent.mkdir()
    child = """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA journal_mode = WAL")
connection.execute("PRAGMA wal_autocheckpoint = 0")
connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
connection.execute("INSERT INTO schema_meta VALUES ('schema_version', '1')")
connection.execute("CREATE TABLE payload (value TEXT NOT NULL)")
connection.execute("INSERT INTO payload VALUES ('committed-in-wal')")
connection.commit()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", child, str(database)], check=True)
    assert Path(f"{database}-wal").is_file()

    transaction.begin(
        TRANSACTION_ID,
        app_root,
        database_path=database,
    )
    transaction.prepare_mutation()
    manifest = transaction.load_manifest(transaction.transaction_dir(TRANSACTION_ID))
    backup = transaction.transaction_dir(TRANSACTION_ID) / manifest["database"][
        "backup"
    ]

    with closing(
        sqlite3.connect(transaction.sqlite_readonly_uri(backup), uri=True)
    ) as connection:
        value = connection.execute("SELECT value FROM payload").fetchone()[0]
    assert value == "committed-in-wal"


def test_first_install_recovery_removes_new_database_and_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, _existing, _absent = helper(tmp_path, monkeypatch)
    state_root = app_root.parent / "state"
    state_root.mkdir()
    database = state_root / "encoder.sqlite3"

    transaction.begin(
        TRANSACTION_ID,
        app_root,
        database_path=database,
    )
    transaction.prepare_mutation()
    manifest = transaction.load_manifest(transaction.transaction_dir(TRANSACTION_ID))
    assert manifest["database"] == {"kind": "absent", "path": str(database)}

    create_v1_database(database)
    Path(f"{database}-wal").write_bytes(b"new WAL")
    Path(f"{database}-shm").write_bytes(b"new SHM")

    assert transaction.recover() is True
    assert not database.exists()
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_corrupt_database_backup_does_not_remove_live_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, _existing, _absent = helper(tmp_path, monkeypatch)
    database = app_root.parent / "state" / "encoder.sqlite3"
    create_v1_database(database)
    transaction.begin(
        TRANSACTION_ID,
        app_root,
        database_path=database,
    )
    transaction.prepare_mutation()
    transaction_dir = transaction.transaction_dir(TRANSACTION_ID)
    manifest = transaction.load_manifest(transaction_dir)
    backup = transaction_dir / manifest["database"]["backup"]
    backup.write_bytes(b"corrupt backup")
    live_wal = Path(f"{database}-wal")
    live_wal.write_bytes(b"keep until backup validation succeeds")

    with pytest.raises(
        install_transaction.InstallTransactionError,
        match="integrity failure",
    ):
        transaction.recover()

    assert live_wal.read_bytes() == b"keep until backup validation succeeds"
    assert transaction.state(transaction_dir) == "RECOVERY_REQUIRED"


def test_rollback_clears_mutation_marker_before_runtime_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, existing, _absent = helper(tmp_path, monkeypatch)
    transaction.begin(TRANSACTION_ID, app_root)
    transaction.prepare_mutation()
    existing.write_text("candidate\n", encoding="utf-8")
    observations: list[tuple[str | None, str | None]] = []

    def observe_runtime_restore(_manifest: object) -> None:
        observations.append(
            (transaction.active_id(), transaction.pending_services_id())
        )

    monkeypatch.setattr(transaction, "restore_active_unit_states", observe_runtime_restore)
    transaction.recover()

    assert observations == [(None, TRANSACTION_ID)]
    assert transaction.active_id() is None
    assert transaction.pending_services_id() is None


def test_interrupted_service_restore_is_durably_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, _existing, _absent = helper(tmp_path, monkeypatch)
    transaction.begin(TRANSACTION_ID, app_root)
    transaction.prepare_mutation()

    def interrupted(_manifest: object) -> None:
        raise install_transaction.InstallTransactionError("simulated service crash")

    monkeypatch.setattr(transaction, "restore_active_unit_states", interrupted)
    with pytest.raises(
        install_transaction.InstallTransactionError, match="simulated service crash"
    ):
        transaction.recover()

    assert transaction.active_id() is None
    assert transaction.pending_services_id() == TRANSACTION_ID
    status = transaction.pending_services_dir()
    assert status is not None
    assert transaction.state(status) == "RESTORED"

    restored: list[bool] = []
    monkeypatch.setattr(
        transaction,
        "restore_active_unit_states",
        lambda _manifest: restored.append(True),
    )
    assert transaction.recover() is True
    assert restored == [True]
    assert transaction.pending_services_id() is None


def test_observation_recovery_never_stops_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, _existing, _absent = helper(tmp_path, monkeypatch)
    transaction.begin(TRANSACTION_ID, app_root)
    restored: list[bool] = []

    monkeypatch.setattr(
        transaction,
        "stop_runtime",
        lambda: pytest.fail("observation rollback stopped the runtime"),
    )
    monkeypatch.setattr(
        transaction,
        "restore_active_unit_states",
        lambda _manifest: restored.append(True),
    )

    assert transaction.recover() is True
    assert restored == [True]
    assert transaction.state(transaction.transaction_dir(TRANSACTION_ID)) == "RESTORED"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symlinks")
def test_recovery_restores_release_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, _existing, _absent = helper(tmp_path, monkeypatch)
    old_app = app_root / "releases" / "old"
    new_app = app_root / "releases" / "new"
    old_tools = app_root / "tools" / "releases" / "old"
    new_tools = app_root / "tools" / "releases" / "new"
    for release in (old_app, new_app, old_tools, new_tools):
        release.mkdir(parents=True)
    app_pointer = app_root / "current"
    tools_pointer = app_root / "tools" / "current"
    app_pointer.symlink_to(old_app)
    tools_pointer.symlink_to(old_tools)

    transaction.begin(TRANSACTION_ID, app_root)
    transaction.prepare_mutation()
    app_pointer.unlink()
    app_pointer.symlink_to(new_app)
    tools_pointer.unlink()
    tools_pointer.symlink_to(new_tools)

    transaction.recover()
    assert app_pointer.is_symlink()
    assert os.readlink(app_pointer) == str(old_app)
    assert tools_pointer.is_symlink()
    assert os.readlink(tools_pointer) == str(old_tools)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symlinks")
def test_recovery_restores_frontend_release_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BDENCODE_INSTALL_TESTING", "1")
    state_root = tmp_path / "journal"
    state_root.mkdir(mode=0o700)
    app_root = tmp_path / "data" / "app"
    (app_root / "tools").mkdir(parents=True)
    web_releases = tmp_path / "var" / "www" / "bdencode" / "releases"
    old_web = web_releases / "old"
    new_web = web_releases / "new"
    old_web.mkdir(parents=True)
    new_web.mkdir()
    web_pointer = web_releases.parent / "current"
    web_pointer.symlink_to(old_web)
    transaction = install_transaction.InstallTransaction(
        state_root, fixed_targets=(web_pointer,)
    )
    transaction.initialize()

    transaction.begin(TRANSACTION_ID, app_root)
    transaction.prepare_mutation()
    web_pointer.unlink()
    web_pointer.symlink_to(new_web)

    transaction.recover()
    assert web_pointer.is_symlink()
    assert os.readlink(web_pointer) == str(old_web)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symlinks")
def test_first_install_recovery_removes_frontend_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BDENCODE_INSTALL_TESTING", "1")
    state_root = tmp_path / "journal"
    state_root.mkdir(mode=0o700)
    app_root = tmp_path / "data" / "app"
    (app_root / "tools").mkdir(parents=True)
    web_release = tmp_path / "var" / "www" / "bdencode" / "releases" / "new"
    web_release.mkdir(parents=True)
    web_pointer = web_release.parents[1] / "current"
    transaction = install_transaction.InstallTransaction(
        state_root, fixed_targets=(web_pointer,)
    )
    transaction.initialize()

    transaction.begin(TRANSACTION_ID, app_root)
    transaction.prepare_mutation()
    web_pointer.symlink_to(web_release)

    transaction.recover()
    assert not web_pointer.exists()
    assert not web_pointer.is_symlink()


def test_commit_keeps_new_files_and_clears_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, existing, absent = helper(tmp_path, monkeypatch)
    transaction.begin(TRANSACTION_ID, app_root)
    transaction.prepare_mutation()
    existing.write_text("committed\n", encoding="utf-8")
    absent.write_text("committed new file\n", encoding="utf-8")

    transaction.healthy()
    transaction.commit()
    assert existing.read_text(encoding="utf-8") == "committed\n"
    assert absent.read_text(encoding="utf-8") == "committed new file\n"
    assert not transaction.active_file.exists()
    assert transaction.state(transaction.transaction_dir(TRANSACTION_ID)) == "COMMITTED"
    assert transaction.recover() is False


def test_healthy_interruption_finalizes_candidate_without_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, existing, _absent = helper(tmp_path, monkeypatch)
    transaction.begin(TRANSACTION_ID, app_root)
    transaction.prepare_mutation()
    existing.write_text("validated candidate\n", encoding="utf-8")
    transaction.healthy()

    assert transaction.recover() is False
    assert existing.read_text(encoding="utf-8") == "validated candidate\n"
    assert transaction.state(transaction.transaction_dir(TRANSACTION_ID)) == "COMMITTED"
    assert not transaction.active_file.exists()


def test_healthy_start_happens_after_commit_and_active_marker_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, _existing, _absent = helper(tmp_path, monkeypatch)
    transaction.begin(TRANSACTION_ID, app_root)
    transaction.prepare_mutation()
    transaction.healthy()
    observations: list[tuple[str, str | None, str | None]] = []

    monkeypatch.setattr(transaction, "stop_runtime", lambda: None)
    monkeypatch.setattr(
        transaction, "finalize_healthy_enablement", lambda _manifest: None
    )

    def observe_start(_manifest: object) -> None:
        directory = transaction.transaction_dir(TRANSACTION_ID)
        observations.append(
            (
                transaction.state(directory),
                transaction.active_id(),
                transaction.pending_services_id(),
            )
        )

    monkeypatch.setattr(transaction, "start_healthy_units", observe_start)
    assert transaction.recover() is False

    assert observations == [("COMMITTED", None, TRANSACTION_ID)]
    assert transaction.pending_services_id() is None


def test_corrupt_backup_leaves_retryable_recovery_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, existing, _absent = helper(tmp_path, monkeypatch)
    transaction.begin(TRANSACTION_ID, app_root)
    transaction.prepare_mutation()
    transaction_dir = transaction.transaction_dir(TRANSACTION_ID)
    manifest = json.loads((transaction_dir / "manifest.json").read_text(encoding="utf-8"))
    entry = next(item for item in manifest["targets"] if item["path"] == str(existing))
    (transaction_dir / entry["backup"]).write_bytes(b"tampered")
    existing.write_bytes(b"new configuration")

    with pytest.raises(
        install_transaction.InstallTransactionError, match="integrity failure"
    ):
        transaction.recover()
    assert transaction.active_file.exists()
    assert transaction.state(transaction_dir) == "RECOVERY_REQUIRED"


def test_directory_target_fails_before_active_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, _existing, absent = helper(tmp_path, monkeypatch)
    absent.mkdir()

    with pytest.raises(
        install_transaction.InstallTransactionError, match="not a regular file or symlink"
    ):
        transaction.begin(TRANSACTION_ID, app_root)
    assert not transaction.active_file.exists()


def test_second_begin_requires_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, app_root, _existing, _absent = helper(tmp_path, monkeypatch)
    transaction.begin(TRANSACTION_ID, app_root)

    with pytest.raises(
        install_transaction.InstallTransactionError, match="unfinished installer transaction"
    ):
        transaction.begin("20260802T123457Z-43", app_root)


def test_interrupted_package_configuration_is_repaired_before_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, _app_root, _existing, _absent = helper(tmp_path, monkeypatch)
    commands: list[tuple[str, ...]] = []
    audit_calls = 0

    def fake_run(command, *, check=True, capture=False):
        nonlocal audit_calls
        normalized = tuple(command)
        commands.append(normalized)
        if normalized == ("dpkg", "--audit"):
            audit_calls += 1
            output = "package is unpacked\n" if audit_calls == 1 else ""
            return SimpleNamespace(returncode=0, stdout=output)
        if normalized == ("apt-get", "check"):
            return SimpleNamespace(returncode=100 if audit_calls == 1 else 0, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(transaction, "run", fake_run)
    transaction.ensure_package_integrity()

    assert any(command[:2] == ("dpkg", "--force-confdef") for command in commands)
    assert any(
        command[0] == "apt-get" and "-f" in command and "install" in command
        for command in commands
    )
    assert commands[-1] == ("apt-get", "check")


def test_inconsistent_package_state_keeps_recovery_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, _app_root, _existing, _absent = helper(tmp_path, monkeypatch)

    def fake_run(command, *, check=True, capture=False):
        normalized = tuple(command)
        if normalized == ("dpkg", "--audit"):
            return SimpleNamespace(returncode=0, stdout="still broken\n")
        if normalized == ("apt-get", "check"):
            return SimpleNamespace(returncode=100, stdout="")
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(transaction, "run", fake_run)
    with pytest.raises(
        install_transaction.InstallTransactionError,
        match="Package state is inconsistent",
    ):
        transaction.ensure_package_integrity()


def test_observing_recovery_never_repairs_preexisting_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction, _app_root, _existing, _absent = helper(tmp_path, monkeypatch)
    commands: list[tuple[str, ...]] = []

    def fake_run(command, *, check=True, capture=False):
        normalized = tuple(command)
        commands.append(normalized)
        if normalized == ("dpkg", "--audit"):
            return SimpleNamespace(returncode=0, stdout="pre-existing problem\n")
        if normalized == ("apt-get", "check"):
            return SimpleNamespace(returncode=100, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(transaction, "run", fake_run)
    transaction.ensure_package_integrity(repair=False)

    assert commands == [("dpkg", "--audit"), ("apt-get", "check")]
