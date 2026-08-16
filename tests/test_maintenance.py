from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from bdencode.config import Settings
from bdencode.db import Database, StateConflictError
from bdencode.maintenance import (
    MaintenanceDomainGuard,
    MaintenanceJournal,
    MaintenanceLeaseBusyError,
    MaintenancePhase,
    MaintenanceSafetyError,
    MaintenanceTargetSpec,
    delete_quarantined,
    inspect_job_storage,
    list_quarantine,
    quarantine_direct_child,
    quarantine_temporary_work,
    restore_quarantined,
)
import bdencode.maintenance as maintenance_module
from bdencode.models import JobCreate
from bdencode.queue import JobQueue
from bdencode.release.models import (
    ReleaseMetadata,
    ReleasePreparationState,
)
from bdencode.release_store import ReleasePreparation, ReleaseStore


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    jobs = tmp_path / "jobs"
    job = jobs / "job-1"
    for name in ("work", "logs", "analysis", "comparison", "stages"):
        (job / name).mkdir(parents=True, exist_ok=True)
    (job / "work" / "video.partial").write_bytes(b"w" * 11)
    (job / "logs" / "worker.log").write_bytes(b"l" * 7)
    (job / "analysis" / "qc.json").write_bytes(b"a" * 5)
    return jobs, job


def _journal_context(tmp_path: Path) -> tuple[Settings, Database, MaintenanceJournal]:
    source = tmp_path / "source"
    source.mkdir()
    settings = Settings(
        data_root=tmp_path / "data",
        source_roots=(source,),
    ).validate()
    settings.create_directories()
    database = Database(settings.resolved_database_path)
    database.initialize()
    return settings, database, MaintenanceJournal(database, settings)


def _run_crash_script(
    script: str, *arguments: Path | str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(script),
            *(str(item) for item in arguments),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _release_record(
    settings: Settings, database: Database
) -> tuple[str, ReleasePreparation]:
    job = database.create_job(JobCreate(source_path="/source/BDMV", name="release"))
    with database._write() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'COMPLETED' WHERE id = ?", (job.id,)
        )
    record = ReleaseStore(database).create(
        job_id=job.id,
        profile_id="example",
        profile_digest="a" * 64,
        metadata=ReleaseMetadata(
            release_name="Example.2026.1080p.BluRay.x264-GROUP",
            title="Example",
            year=2026,
            category="Movie",
            source_media="BluRay",
            resolution="1080p",
            video_codec="x264",
            audio_codecs=("FLAC",),
            languages=("en",),
        ),
        payload_name="Example.2026.1080p.BluRay.x264-GROUP.mkv",
        payload_path=str(
            settings.completed_root
            / "Example.2026.1080p.BluRay.x264-GROUP"
            / "Example.2026.1080p.BluRay.x264-GROUP.mkv"
        ),
        payload_size=7,
        payload_sha256="b" * 64,
    )
    return job.id, record


def test_storage_report_separates_reclaimable_work_from_audit(tmp_path: Path) -> None:
    jobs, job = _workspace(tmp_path)
    completed_root = tmp_path / "completed"
    release = completed_root / "Movie.2026.1080p.BluRay.x264"
    release.mkdir(parents=True)
    (release / "Movie.2026.1080p.BluRay.x264.mkv").write_bytes(b"m" * 19)

    report = inspect_job_storage(
        job,
        jobs_root=jobs,
        completed_release=release,
        completed_root=completed_root,
    )

    assert report.workspace_bytes == 23
    assert report.reclaimable_bytes == 11
    assert report.completed_release_bytes == 19
    assert {item.name: item.bytes for item in report.categories} == {
        "work": 11,
        "logs": 7,
        "analysis": 5,
        "comparison": 0,
        "stages": 0,
    }


def test_temporary_cleanup_is_atomic_then_reapable(tmp_path: Path) -> None:
    jobs, job = _workspace(tmp_path)

    receipt = quarantine_temporary_work(
        job,
        jobs_root=jobs,
        operation_id="cleanup-01",
    )

    assert receipt is not None
    assert receipt.bytes_moved == 11
    assert not (job / "work").exists()
    assert receipt.quarantine_path in list_quarantine(job)
    delete_quarantined(receipt, root=job)
    assert list_quarantine(job) == ()


def test_quarantined_target_can_be_restored_after_database_conflict(
    tmp_path: Path,
) -> None:
    jobs, job = _workspace(tmp_path)
    receipt = quarantine_direct_child(
        job,
        root=jobs,
        operation_id="delete-race",
        label="job workspace",
    )

    restore_quarantined(receipt, root=jobs)

    assert (job / "work" / "video.partial").read_bytes() == b"w" * 11
    assert list_quarantine(jobs) == ()


def test_whole_job_can_be_detached_without_touching_completed_release(
    tmp_path: Path,
) -> None:
    jobs, job = _workspace(tmp_path)
    completed = tmp_path / "completed" / "Movie"
    completed.mkdir(parents=True)
    sentinel = completed / "Movie.mkv"
    sentinel.write_bytes(b"release")

    receipt = quarantine_direct_child(
        job,
        root=jobs,
        operation_id="delete-01",
        label="job workspace",
    )

    assert not job.exists()
    assert sentinel.read_bytes() == b"release"
    delete_quarantined(receipt, root=jobs)
    assert sentinel.read_bytes() == b"release"


def test_linked_job_or_nested_link_is_rejected(tmp_path: Path) -> None:
    jobs, job = _workspace(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    linked_job = jobs / "linked-job"
    try:
        linked_job.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links unavailable: {exc}")

    with pytest.raises(MaintenanceSafetyError, match="link or junction"):
        inspect_job_storage(linked_job, jobs_root=jobs)

    nested = job / "work" / "external"
    nested.symlink_to(external, target_is_directory=True)
    with pytest.raises(MaintenanceSafetyError, match="unsafe link"):
        inspect_job_storage(job, jobs_root=jobs)


def test_quarantine_destination_cannot_be_precreated_or_redirected(
    tmp_path: Path,
) -> None:
    jobs, job = _workspace(tmp_path)
    trash = jobs / ".trash"
    trash.mkdir()
    hostile = trash / "job-1-delete-01"
    hostile.mkdir()

    with pytest.raises(MaintenanceSafetyError, match="already exists"):
        quarantine_direct_child(
            job,
            root=jobs,
            operation_id="delete-01",
            label="job workspace",
        )
    assert job.is_dir()


def test_quarantine_root_link_is_rejected(tmp_path: Path) -> None:
    jobs, job = _workspace(tmp_path)
    external = tmp_path / "external-trash"
    external.mkdir()
    trash = jobs / ".trash"
    try:
        trash.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links unavailable: {exc}")

    with pytest.raises(MaintenanceSafetyError, match="real directory"):
        quarantine_direct_child(job, root=jobs, operation_id="delete-01")
    assert list(external.iterdir()) == []


def test_invalid_operation_id_never_becomes_a_path(tmp_path: Path) -> None:
    jobs, job = _workspace(tmp_path)
    with pytest.raises(ValueError, match="operation_id"):
        quarantine_direct_child(job, root=jobs, operation_id="../escape")
    assert os.path.isdir(job)


def test_same_device_nested_bind_mount_blocks_quarantine_before_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, job = _workspace(tmp_path)
    nested_mount = job / "work" / "bound-library"
    nested_mount.mkdir()
    sentinel = nested_mount / "external.mkv"
    sentinel.write_bytes(b"external")
    monkeypatch.setattr(
        maintenance_module,
        "_linux_mount_points",
        lambda: frozenset({os.path.normcase(os.path.abspath(nested_mount))}),
    )
    monkeypatch.setattr(
        maintenance_module.shutil,
        "rmtree",
        lambda _path: pytest.fail("unsafe tree must never reach rmtree"),
    )

    with pytest.raises(MaintenanceSafetyError, match="mounted path"):
        quarantine_direct_child(job, root=jobs)

    assert sentinel.read_bytes() == b"external"
    assert job.exists()


def test_cross_device_entry_blocks_quarantine_before_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, job = _workspace(tmp_path)
    original_stat = maintenance_module._scandir_entry_stat

    def foreign_device(entry):
        observed = original_stat(entry)
        return SimpleNamespace(
            st_dev=int(observed.st_dev) + 1,
            st_size=int(observed.st_size),
        )

    monkeypatch.setattr(maintenance_module, "_scandir_entry_stat", foreign_device)
    monkeypatch.setattr(
        maintenance_module.shutil,
        "rmtree",
        lambda _path: pytest.fail("unsafe tree must never reach rmtree"),
    )

    with pytest.raises(MaintenanceSafetyError, match="filesystem boundary"):
        quarantine_direct_child(job, root=jobs)

    assert (job / "work" / "video.partial").read_bytes() == b"w" * 11


def test_mountinfo_parser_decodes_kernel_path_escapes() -> None:
    parsed = maintenance_module._parse_mountinfo(
        "36 25 0:32 / /owned/bind\\040mount rw,relatime - ext4 /dev/sda rw\n"
    )

    assert os.path.normcase(os.path.abspath("/owned/bind mount")) in parsed


def test_journal_restores_uncommitted_detach_and_releases_target_claim(
    tmp_path: Path,
) -> None:
    settings, database, journal = _journal_context(tmp_path)
    target = settings.release_kits_root / "preparation-1"
    target.mkdir()
    (target / "private.torrent").write_bytes(b"private")

    operation = journal.begin(
        "release-preparation-delete",
        "preparation-1",
        [MaintenanceTargetSpec(target, settings.release_kits_root, "release kit")],
    )
    journal.stage(operation.id)
    assert not target.exists()

    restored = journal.rollback(operation.id)

    assert restored.phase is MaintenancePhase.ROLLED_BACK
    assert (target / "private.torrent").read_bytes() == b"private"
    replacement = journal.begin(
        "release-preparation-delete",
        "preparation-1",
        [MaintenanceTargetSpec(target, settings.release_kits_root, "release kit")],
    )
    assert replacement.phase is MaintenancePhase.INTENT
    database.close()


def test_journal_rejects_empty_or_unbounded_target_sets(tmp_path: Path) -> None:
    settings, database, journal = _journal_context(tmp_path)
    with pytest.raises(ValueError, match="1 to 1024"):
        journal.begin("terminal-job-purge", "job-1", [])
    target = settings.jobs_root / "job-1"
    target.mkdir()
    spec = MaintenanceTargetSpec(target, settings.jobs_root)
    with pytest.raises(ValueError, match="1 to 1024"):
        journal.begin("terminal-job-purge", "job-1", [spec] * 1025)
    database.close()


def test_journal_has_database_unique_active_target_guard(tmp_path: Path) -> None:
    settings, database, first = _journal_context(tmp_path)
    target = settings.jobs_root / "job-1"
    target.mkdir()
    first.begin(
        "terminal-job-purge",
        "job-1",
        [MaintenanceTargetSpec(target, settings.jobs_root, "job workspace")],
    )

    second = MaintenanceJournal(database, settings)
    with pytest.raises(MaintenanceLeaseBusyError, match="unfinished operation"):
        second.begin(
            "terminal-job-purge",
            "job-1",
            [MaintenanceTargetSpec(target, settings.jobs_root, "job workspace")],
        )
    database.close()


@pytest.mark.parametrize("whole_job_first", [False, True])
def test_journal_rejects_ancestor_descendant_target_claims(
    tmp_path: Path,
    whole_job_first: bool,
) -> None:
    settings, database, journal = _journal_context(tmp_path)
    job = settings.jobs_root / "job-1"
    work = job / "work"
    work.mkdir(parents=True)
    (work / "partial.mkv").write_bytes(b"partial")
    whole = MaintenanceTargetSpec(job, settings.jobs_root, "job workspace")
    nested = MaintenanceTargetSpec(work, job, "temporary work")
    first, second = (whole, nested) if whole_job_first else (nested, whole)

    journal.begin("first-maintenance", "job-1", [first])
    with pytest.raises(MaintenanceLeaseBusyError, match="hierarchy"):
        journal.begin("second-maintenance", "job-1", [second])

    assert (work / "partial.mkv").read_bytes() == b"partial"
    database.close()


def test_destructive_intent_and_remote_claim_block_both_interleavings(
    tmp_path: Path,
) -> None:
    settings, database, journal = _journal_context(tmp_path)
    job_id, created = _release_record(settings, database)
    store = ReleaseStore(database)
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
    release = settings.completed_root / ready.metadata.release_name
    release.mkdir()
    (release / ready.payload_name).write_bytes(b"payload")
    job = database.get_job(job_id)
    snapshot = {ready.id: ready.version}
    operation = journal.begin(
        "completed-release-delete",
        job_id,
        [MaintenanceTargetSpec(release, settings.completed_root)],
        guard=MaintenanceDomainGuard(
            job_id=job_id,
            expected_job_version=job.version,
            allowed_job_states=("COMPLETED",),
            expected_preparation_versions=snapshot,
            forbid_active_preparations=True,
        ),
    )

    with pytest.raises(StateConflictError, match="destructive maintenance intent"):
        store.transition(
            ready.id,
            ReleasePreparationState.SEEDING_CHECK,
            expected_version=ready.version,
        )
    assert (release / ready.payload_name).read_bytes() == b"payload"
    journal.rollback(operation.id)

    checking = store.transition(
        ready.id,
        ReleasePreparationState.SEEDING_CHECK,
        expected_version=ready.version,
    )
    with pytest.raises(MaintenanceLeaseBusyError, match="active release"):
        journal.begin(
            "completed-release-delete",
            job_id,
            [MaintenanceTargetSpec(release, settings.completed_root)],
            guard=MaintenanceDomainGuard(
                job_id=job_id,
                expected_job_version=job.version,
                allowed_job_states=("COMPLETED",),
                expected_preparation_versions={checking.id: checking.version},
                forbid_active_preparations=True,
            ),
        )
    assert (release / ready.payload_name).read_bytes() == b"payload"
    database.close()


def test_committed_journal_reaps_missing_quarantine_idempotently(
    tmp_path: Path,
) -> None:
    settings, database, journal = _journal_context(tmp_path)
    target = settings.jobs_root / "job-1"
    target.mkdir()
    operation = journal.begin(
        "terminal-job-purge",
        "job-1",
        [MaintenanceTargetSpec(target, settings.jobs_root, "job workspace")],
    )
    receipts = journal.stage(operation.id)
    with database._write() as connection:
        database._mark_maintenance_committed(
            connection,
            operation.id,
            kind="terminal-job-purge",
            subject_id="job-1",
        )
    delete_quarantined(receipts[0], root=settings.jobs_root)

    finalized = journal.finalize(operation.id)

    assert finalized.phase is MaintenancePhase.FINALIZED
    assert journal.finalize(operation.id).phase is MaintenancePhase.FINALIZED
    assert not target.exists()
    database.close()


def test_finalize_rejects_replaced_or_mutated_quarantine_target(
    tmp_path: Path,
) -> None:
    settings, database, journal = _journal_context(tmp_path)
    target = settings.jobs_root / "job-1"
    target.mkdir()
    (target / "partial.mkv").write_bytes(b"original")
    operation = journal.begin(
        "terminal-job-purge",
        "job-1",
        [MaintenanceTargetSpec(target, settings.jobs_root)],
    )
    receipt = journal.stage(operation.id)[0]
    with database._write() as connection:
        database._mark_maintenance_committed(
            connection,
            operation.id,
            kind="terminal-job-purge",
            subject_id="job-1",
        )
    displaced = receipt.quarantine_path.with_name("displaced-owned-target")
    receipt.quarantine_path.rename(displaced)
    receipt.quarantine_path.mkdir()
    (receipt.quarantine_path / "partial.mkv").write_bytes(b"replacement")

    with pytest.raises(MaintenanceSafetyError, match="replaced after intent"):
        journal.finalize(operation.id)

    assert journal.operation(operation.id).phase is MaintenancePhase.COMMITTED
    assert (receipt.quarantine_path / "partial.mkv").read_bytes() == b"replacement"
    assert (displaced / "partial.mkv").read_bytes() == b"original"
    database.close()


def test_rollback_rejects_mutated_quarantine_contents(tmp_path: Path) -> None:
    settings, database, journal = _journal_context(tmp_path)
    target = settings.jobs_root / "job-1"
    target.mkdir()
    (target / "partial.mkv").write_bytes(b"original")
    operation = journal.begin(
        "terminal-job-purge",
        "job-1",
        [MaintenanceTargetSpec(target, settings.jobs_root)],
    )
    receipt = journal.stage(operation.id)[0]
    (receipt.quarantine_path / "injected.bin").write_bytes(b"changed")

    with pytest.raises(MaintenanceSafetyError, match="changed before restore"):
        journal.rollback(operation.id)

    assert not target.exists()
    assert (receipt.quarantine_path / "injected.bin").read_bytes() == b"changed"
    database.close()


def test_windows_reparse_attribute_is_treated_as_unsafe_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStat:
        st_file_attributes = 0x400

    monkeypatch.setattr(
        maintenance_module.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, raising=False
    )
    monkeypatch.setattr(Path, "lstat", lambda _path: FakeStat())
    monkeypatch.setattr(Path, "is_symlink", lambda _path: False)

    assert maintenance_module._is_link_or_junction(Path("junction")) is True


def test_windows_process_token_uses_pointer_sized_handle_prototypes() -> None:
    import ctypes
    from ctypes import wintypes

    calls: dict[str, object] = {}

    class Function:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    large_handle = 0x1_0000_1234

    def open_process(_access, _inherit, _pid):
        return large_handle

    def get_exit_code(handle, output):
        calls["exit_handle"] = handle
        output._obj.value = 259
        return 1

    def get_process_times(handle, creation, _exit, _kernel, _user):
        calls["times_handle"] = handle
        creation._obj.dwHighDateTime = 2
        creation._obj.dwLowDateTime = 3
        return 1

    def close_handle(handle):
        calls["close_handle"] = handle
        return 1

    class Kernel32:
        OpenProcess = Function(open_process)
        GetExitCodeProcess = Function(get_exit_code)
        GetProcessTimes = Function(get_process_times)
        CloseHandle = Function(close_handle)

    kernel32 = Kernel32()
    token = MaintenanceJournal._windows_process_start_token(123, kernel32)

    assert token == f"windows-filetime:{(2 << 32) | 3}"
    assert kernel32.OpenProcess.restype is wintypes.HANDLE
    assert kernel32.GetExitCodeProcess.argtypes[0] is wintypes.HANDLE
    assert kernel32.GetProcessTimes.argtypes[0] is wintypes.HANDLE
    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]
    assert calls == {
        "exit_handle": large_handle,
        "times_handle": large_handle,
        "close_handle": large_handle,
    }
    assert kernel32.GetExitCodeProcess.argtypes[1] == ctypes.POINTER(wintypes.DWORD)


def test_posix_replace_is_followed_by_both_directory_fsyncs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("/owned/jobs/job-1")
    destination = Path("/owned/jobs/.trash/job-1-operation")
    calls: list[tuple[str, Path, Path | None]] = []

    monkeypatch.setattr(
        maintenance_module.os,
        "replace",
        lambda left, right: calls.append(("replace", left, right)),
    )
    monkeypatch.setattr(
        maintenance_module,
        "_sync_directory",
        lambda path: calls.append(("fsync", path, None)),
    )

    maintenance_module._durable_replace_posix(source, destination)

    assert calls == [
        ("replace", source, destination),
        ("fsync", source.parent, None),
        ("fsync", destination.parent, None),
    ]


def test_windows_replace_requests_write_through() -> None:
    from ctypes import wintypes

    calls: list[tuple[str, str, int]] = []

    class Function:
        argtypes = None
        restype = None

        def __call__(self, source, destination, flags):
            calls.append((source, destination, flags))
            return 1

    class Kernel32:
        MoveFileExW = Function()

    kernel32 = Kernel32()
    maintenance_module._durable_replace_windows(
        Path("C:/owned/job-1"),
        Path("C:/owned/.trash/job-1-operation"),
        kernel32,
    )

    assert calls == [
        (
            str(Path("C:/owned/job-1")),
            str(Path("C:/owned/.trash/job-1-operation")),
            0x9,
        )
    ]
    assert kernel32.MoveFileExW.argtypes == [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    assert kernel32.MoveFileExW.restype is wintypes.BOOL


def test_commit_hook_rejects_wrong_operation_binding(tmp_path: Path) -> None:
    settings, database, journal = _journal_context(tmp_path)
    target = settings.jobs_root / "job-1"
    target.mkdir()
    operation = journal.begin(
        "terminal-job-purge",
        "job-1",
        [MaintenanceTargetSpec(target, settings.jobs_root)],
    )
    journal.stage(operation.id)

    with pytest.raises(Exception, match="binding does not match"):
        with database._write() as connection:
            database._mark_maintenance_committed(
                connection,
                operation.id,
                kind="terminal-job-purge",
                subject_id="different-job",
            )
    journal.rollback(operation.id)
    database.close()


def test_subprocess_crash_after_preparation_rename_is_restored_on_startup(
    tmp_path: Path,
) -> None:
    settings, database, _journal = _journal_context(tmp_path)
    target = settings.release_kits_root / "prep-crash"
    target.mkdir()
    sentinel = target / "private.torrent"
    sentinel.write_bytes(b"private-passkey-material")
    operation_path = tmp_path / "operation-id"

    crashed = _run_crash_script(
        """
        import os
        from pathlib import Path
        import sys
        from bdencode.config import Settings
        from bdencode.db import Database
        from bdencode.maintenance import MaintenanceJournal, MaintenanceTargetSpec

        data, source, operation_file = map(Path, sys.argv[1:])
        settings = Settings(data_root=data, source_roots=(source,)).validate()
        database = Database(settings.resolved_database_path)
        journal = MaintenanceJournal(database, settings)
        target = settings.release_kits_root / "prep-crash"
        operation = journal.begin(
            "release-preparation-delete",
            "prep-crash",
            [MaintenanceTargetSpec(target, settings.release_kits_root)],
        )
        operation_file.write_text(operation.id, encoding="ascii")
        journal.stage(operation.id)
        os._exit(73)
        """,
        settings.data_root,
        settings.source_roots[0],
        operation_path,
    )
    assert crashed.returncode == 73, crashed.stderr
    assert not target.exists()

    recovered = MaintenanceJournal(database, settings).recover()

    assert [item.id for item in recovered] == [operation_path.read_text("ascii")]
    assert recovered[0].phase is MaintenancePhase.ROLLED_BACK
    assert sentinel.read_bytes() == b"private-passkey-material"
    database.close()


def test_subprocess_partial_multitarget_stage_is_fully_restored(
    tmp_path: Path,
) -> None:
    settings, database, _journal = _journal_context(tmp_path)
    kit = settings.release_kits_root / "prep-1"
    workspace = settings.jobs_root / "job-1"
    kit.mkdir()
    workspace.mkdir()
    (kit / "private.torrent").write_bytes(b"kit")
    (workspace / "worker.log").write_bytes(b"job")

    crashed = _run_crash_script(
        """
        import os
        from pathlib import Path
        import sys
        from bdencode.config import Settings
        from bdencode.db import Database
        from bdencode.maintenance import MaintenanceJournal, MaintenanceTargetSpec

        data, source = map(Path, sys.argv[1:])
        settings = Settings(data_root=data, source_roots=(source,)).validate()
        database = Database(settings.resolved_database_path)
        journal = MaintenanceJournal(database, settings)
        operation = journal.begin(
            "terminal-job-purge",
            "job-1",
            [
                MaintenanceTargetSpec(
                    settings.release_kits_root / "prep-1",
                    settings.release_kits_root,
                ),
                MaintenanceTargetSpec(
                    settings.jobs_root / "job-1", settings.jobs_root
                ),
            ],
        )
        first = operation.targets[0]
        os.replace(first["original_path"], first["quarantine_path"])
        os._exit(74)
        """,
        settings.data_root,
        settings.source_roots[0],
    )
    assert crashed.returncode == 74, crashed.stderr
    assert not kit.exists()
    assert workspace.exists()

    recovered = MaintenanceJournal(database, settings).recover()

    assert len(recovered) == 1
    assert recovered[0].phase is MaintenancePhase.ROLLED_BACK
    assert (kit / "private.torrent").read_bytes() == b"kit"
    assert (workspace / "worker.log").read_bytes() == b"job"
    database.close()


def test_subprocess_crash_after_job_database_commit_finalizes_on_startup(
    tmp_path: Path,
) -> None:
    settings, database, _journal = _journal_context(tmp_path)
    queue = JobQueue(database)
    job = queue.enqueue(JobCreate(source_path="/source/BDMV", name="crash-job"))
    cancelled = queue.cancel(job.id)
    workspace = settings.job_root(job.id)
    workspace.mkdir()
    (workspace / "partial.mkv").write_bytes(b"partial")

    crashed = _run_crash_script(
        """
        import os
        from pathlib import Path
        import sys
        from bdencode.config import Settings
        from bdencode.db import Database
        from bdencode.maintenance import MaintenanceJournal, MaintenanceTargetSpec

        data, source, job_id, version = sys.argv[1:]
        settings = Settings(
            data_root=Path(data), source_roots=(Path(source),)
        ).validate()
        database = Database(settings.resolved_database_path)
        journal = MaintenanceJournal(database, settings)
        operation = journal.begin(
            "terminal-job-purge",
            job_id,
            [MaintenanceTargetSpec(settings.job_root(job_id), settings.jobs_root)],
        )
        journal.stage(operation.id)
        database.delete_terminal_job(
            job_id,
            expected_version=int(version),
            maintenance_operation_id=operation.id,
        )
        os._exit(75)
        """,
        settings.data_root,
        settings.source_roots[0],
        job.id,
        str(cancelled.version),
    )
    assert crashed.returncode == 75, crashed.stderr
    assert not workspace.exists()

    recovered = MaintenanceJournal(database, settings).recover()

    assert len(recovered) == 1
    assert recovered[0].phase is MaintenancePhase.FINALIZED
    assert not workspace.exists()
    database.close()


def test_subprocess_crash_after_preparation_delete_commit_finalizes(
    tmp_path: Path,
) -> None:
    settings, database, _journal = _journal_context(tmp_path)
    job_id, preparation = _release_record(settings, database)
    kit = settings.release_kits_root / preparation.id
    kit.mkdir()
    (kit / "private.torrent").write_bytes(b"private")

    crashed = _run_crash_script(
        """
        import os
        from pathlib import Path
        import sys
        from bdencode.config import Settings
        from bdencode.db import Database
        from bdencode.maintenance import (
            MaintenanceDomainGuard, MaintenanceJournal, MaintenanceTargetSpec,
        )
        from bdencode.release_store import ReleaseStore

        data, source, job_id, preparation_id, version = sys.argv[1:]
        settings = Settings(
            data_root=Path(data), source_roots=(Path(source),)
        ).validate()
        database = Database(settings.resolved_database_path)
        journal = MaintenanceJournal(database, settings)
        operation = journal.begin(
            "release-preparation-delete",
            preparation_id,
            [
                MaintenanceTargetSpec(
                    settings.release_kits_root / preparation_id,
                    settings.release_kits_root,
                )
            ],
            guard=MaintenanceDomainGuard(
                job_id=job_id,
                preparation_id=preparation_id,
                expected_preparation_version=int(version),
                allowed_preparation_states=("NOT_PREPARED",),
            ),
        )
        journal.stage(operation.id)
        ReleaseStore(database).delete(
            preparation_id,
            expected_version=int(version),
            maintenance_operation_id=operation.id,
        )
        os._exit(76)
        """,
        settings.data_root,
        settings.source_roots[0],
        job_id,
        preparation.id,
        str(preparation.version),
    )
    assert crashed.returncode == 76, crashed.stderr

    recovered = MaintenanceJournal(database, settings).recover()

    assert recovered[-1].phase is MaintenancePhase.FINALIZED
    assert not kit.exists()
    assert ReleaseStore(database).list_for_job(job_id) == ()
    database.close()


def test_subprocess_crash_after_completed_release_delete_commit_finalizes(
    tmp_path: Path,
) -> None:
    settings, database, _journal = _journal_context(tmp_path)
    job_id, preparation = _release_record(settings, database)
    job = database.get_job(job_id)
    kit = settings.release_kits_root / preparation.id
    kit.mkdir()
    (kit / "private.torrent").write_bytes(b"private")
    completed = settings.completed_root / preparation.metadata.release_name
    completed.mkdir()
    (completed / preparation.payload_name).write_bytes(b"payload")

    crashed = _run_crash_script(
        """
        import os
        from pathlib import Path
        import sys
        from bdencode.config import Settings
        from bdencode.db import Database
        from bdencode.maintenance import (
            MaintenanceDomainGuard, MaintenanceJournal, MaintenanceTargetSpec,
        )
        from bdencode.release_store import ReleaseStore

        data, source, job_id, job_version, preparation_id, version, release_name = sys.argv[1:]
        settings = Settings(
            data_root=Path(data), source_roots=(Path(source),)
        ).validate()
        database = Database(settings.resolved_database_path)
        journal = MaintenanceJournal(database, settings)
        snapshot = {preparation_id: int(version)}
        operation = journal.begin(
            "completed-release-delete",
            job_id,
            [
                MaintenanceTargetSpec(
                    settings.release_kits_root / preparation_id,
                    settings.release_kits_root,
                ),
                MaintenanceTargetSpec(
                    settings.completed_root / release_name,
                    settings.completed_root,
                ),
            ],
            guard=MaintenanceDomainGuard(
                job_id=job_id,
                expected_job_version=int(job_version),
                allowed_job_states=("COMPLETED",),
                expected_preparation_versions=snapshot,
                forbid_active_preparations=True,
            ),
        )
        journal.stage(operation.id)
        ReleaseStore(database).delete_completed_release(
            job_id,
            expected_versions=snapshot,
            maintenance_operation_id=operation.id,
        )
        os._exit(77)
        """,
        settings.data_root,
        settings.source_roots[0],
        job_id,
        str(job.version),
        preparation.id,
        str(preparation.version),
        preparation.metadata.release_name,
    )
    assert crashed.returncode == 77, crashed.stderr

    recovered = MaintenanceJournal(database, settings).recover()

    assert recovered[-1].phase is MaintenancePhase.FINALIZED
    assert not kit.exists()
    assert not completed.exists()
    assert "job.completed-release-deleted" in {
        event.kind for event in database.list_events(job_id=job_id)
    }
    database.close()


def test_subprocess_crash_after_completed_cleanup_commit_finalizes(
    tmp_path: Path,
) -> None:
    settings, database, _journal = _journal_context(tmp_path)
    job = database.create_job(JobCreate(source_path="/source/BDMV", name="cleanup"))
    with database._write() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'COMPLETED' WHERE id = ?", (job.id,)
        )
    job = database.get_job(job.id)
    work = settings.job_root(job.id) / "work"
    work.mkdir(parents=True)
    (work / "partial.mkv").write_bytes(b"partial")

    crashed = _run_crash_script(
        """
        import os
        from pathlib import Path
        import sys
        from bdencode.config import Settings
        from bdencode.db import Database
        from bdencode.maintenance import (
            MaintenanceDomainGuard, MaintenanceJournal, MaintenanceTargetSpec,
        )

        data, source, job_id, version = sys.argv[1:]
        settings = Settings(
            data_root=Path(data), source_roots=(Path(source),)
        ).validate()
        database = Database(settings.resolved_database_path)
        journal = MaintenanceJournal(database, settings)
        work = settings.job_root(job_id) / "work"
        operation = journal.begin(
            "completed-workspace-cleanup",
            job_id,
            [MaintenanceTargetSpec(work, settings.job_root(job_id))],
            guard=MaintenanceDomainGuard(
                job_id=job_id,
                expected_job_version=int(version),
                allowed_job_states=("COMPLETED",),
            ),
        )
        journal.stage(operation.id)
        database.record_completed_cleanup(
            job_id,
            expected_version=int(version),
            cleanup=None,
            payload={"bytes_removed": 7},
            maintenance_operation_id=operation.id,
        )
        os._exit(78)
        """,
        settings.data_root,
        settings.source_roots[0],
        job.id,
        str(job.version),
    )
    assert crashed.returncode == 78, crashed.stderr

    recovered = MaintenanceJournal(database, settings).recover()

    assert recovered[-1].phase is MaintenancePhase.FINALIZED
    assert not work.exists()
    assert "job.workspace-cleaned" in {
        event.kind for event in database.list_events(job_id=job.id)
    }
    database.close()
