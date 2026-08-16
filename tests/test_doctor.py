from __future__ import annotations

import os
from dataclasses import fields
from pathlib import Path

import pytest

from bdencode import doctor
from bdencode.cli import main
from bdencode.config import Settings
from bdencode.db import Database
from bdencode.models import BLOCKING_STATES, JobCreate, JobState
from bdencode.queue import JobQueue


ROOT = Path(__file__).parents[1]


def queue_config(tmp_path: Path) -> tuple[Path, Database, JobQueue]:
    config = tmp_path / "config.toml"
    source = tmp_path / "storage"
    source.mkdir()
    data = tmp_path / "encode"
    config.write_text(
        "[bdencode]\n"
        f'data_root = "{data.as_posix()}"\n'
        f'source_roots = ["{source.as_posix()}"]\n',
        encoding="utf-8",
    )
    database = Database(data / "state" / "encoder.sqlite3")
    database.initialize()
    return config, database, JobQueue(database)


def job_in_state(queue: JobQueue, target: JobState) -> None:
    job = queue.enqueue(JobCreate(source_path="/storage/Film", name="Film"))
    claimed = queue.claim_next()
    assert claimed is not None and claimed.id == job.id
    paths = {
        JobState.SCANNING: (),
        JobState.AWAITING_SELECTION: (JobState.AWAITING_SELECTION,),
        JobState.READY: (JobState.READY,),
        JobState.ENCODING: (JobState.READY, JobState.ENCODING),
        JobState.MUXING: (JobState.READY, JobState.ENCODING, JobState.MUXING),
        JobState.QC: (
            JobState.READY,
            JobState.ENCODING,
            JobState.MUXING,
            JobState.QC,
        ),
        JobState.COMPARISON: (
            JobState.READY,
            JobState.ENCODING,
            JobState.MUXING,
            JobState.QC,
            JobState.COMPARISON,
        ),
        JobState.UPLOADING: (
            JobState.READY,
            JobState.ENCODING,
            JobState.MUXING,
            JobState.QC,
            JobState.COMPARISON,
            JobState.UPLOADING,
        ),
        JobState.NEEDS_REVIEW: (JobState.NEEDS_REVIEW,),
        JobState.UPLOAD_FAILED: (
            JobState.READY,
            JobState.ENCODING,
            JobState.MUXING,
            JobState.QC,
            JobState.COMPARISON,
            JobState.UPLOADING,
            JobState.UPLOAD_FAILED,
        ),
    }
    for state in paths[target]:
        queue.advance(job.id, state)
    assert queue.database.get_job(job.id).state is target


def test_queue_idle_exit_status(tmp_path: Path) -> None:
    config, _database, _queue = queue_config(tmp_path)
    assert main(["--config", str(config), "init-db"]) == 0
    assert main(["--config", str(config), "queue-idle"]) == 0
    assert main(["--config", str(config), "queue-idle", "--allow-review"]) == 0
    assert (
        main(
            [
                "--config",
                str(config),
                "queue-idle",
                "--allow-install-safe-pause",
            ]
        )
        == 0
    )


def test_queue_idle_treats_awaiting_selection_as_safe_pause(
    tmp_path: Path,
) -> None:
    config, _database, queue = queue_config(tmp_path)
    job_in_state(queue, JobState.AWAITING_SELECTION)

    assert main(["--config", str(config), "queue-idle"]) == 0
    assert main(["--config", str(config), "queue-idle", "--allow-review"]) == 0


def test_queue_idle_rejects_an_active_scan(tmp_path: Path) -> None:
    config, _database, queue = queue_config(tmp_path)
    job_in_state(queue, JobState.SCANNING)

    assert main(["--config", str(config), "queue-idle"]) == 3


def test_queue_idle_install_safe_pause_accepts_upload_failed(tmp_path: Path) -> None:
    config, _database, queue = queue_config(tmp_path)
    job_in_state(queue, JobState.UPLOAD_FAILED)

    assert main(["--config", str(config), "queue-idle"]) == 3
    assert main(["--config", str(config), "queue-idle", "--allow-review"]) == 3
    assert (
        main(
            [
                "--config",
                str(config),
                "queue-idle",
                "--allow-install-safe-pause",
            ]
        )
        == 0
    )


@pytest.mark.parametrize(
    "state",
    sorted(
        BLOCKING_STATES - {JobState.AWAITING_SELECTION},
        key=lambda item: item.value,
    ),
)
def test_queue_idle_allow_review_remains_fail_closed_for_other_blockers(
    tmp_path: Path, state: JobState
) -> None:
    config, _database, queue = queue_config(tmp_path)
    job_in_state(queue, state)

    assert main(["--config", str(config), "queue-idle", "--allow-review"]) == 3


def test_installer_uses_narrow_review_pause_gate() -> None:
    installer = (ROOT / "install" / "install.sh").read_text(encoding="utf-8")
    start = installer.index("queue_is_install_safe() {")
    end = installer.index("\n}\ntrap finish EXIT", start)
    gate = installer[start:end]

    assert "queue_args+=(--allow-install-safe-pause)" in gate
    assert "AWAITING_SELECTION" in gate
    assert "UPLOAD_FAILED" in gate
    assert "NEEDS_REVIEW" not in gate


def test_installer_loads_only_fixed_encrypted_image_host_credentials() -> None:
    installer = (ROOT / "install" / "install.sh").read_text(encoding="utf-8")
    for name in ("imgbb-api-key", "catbox-userhash", "freeimage-api-key"):
        assert name in installer
    assert "systemd-creds decrypt" in installer
    assert 'stat -c %u -- "$credential_path"' in installer
    assert 'stat -c %a -- "$credential_path"' in installer
    assert '"$credential_name" "$credential_path"' in installer
    assert "LoadCredentialEncrypted=%s:%s" in installer
    assert (
        'assert_path_components_without_symlinks "$credential_directory"' in installer
    )
    assert 'stat -c %u -- "$credential_directory"' in installer
    assert 'stat -c %a -- "$credential_directory"' in installer


def test_installer_and_doctor_require_comparison_annotation_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = (ROOT / "install" / "install.sh").read_text(encoding="utf-8")
    assert "fonts-dejavu-core" in installer
    assert {"drawtext", "pad"} <= doctor.MANDATORY_FFMPEG_FILTERS

    font = tmp_path / "DejaVuSans-Bold.ttf"
    monkeypatch.setattr(doctor, "COMPARISON_FONT_FILE", font)
    assert doctor._comparison_font_status()["ok"] is False

    font.write_bytes(b"test-font")
    status = doctor._comparison_font_status()
    assert status["present"] is True
    assert status["regular"] is True
    assert status["readable"] is True
    assert status["ok"] is True


def test_doctor_data_contract_includes_private_release_kits(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    settings = Settings(
        data_root=tmp_path / "data",
        source_roots=(source,),
    ).validate()
    settings.create_directories()

    status = doctor._data_path_check(settings)

    release_kits = status["required_writable_paths"]["release_kits"]
    assert release_kits["path"] == str(settings.release_kits_root)
    assert release_kits["ok"] is True


def test_doctor_parses_release_profiles_without_echoing_invalid_input(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    profiles = tmp_path / "release-profiles.json"
    settings = Settings(
        data_root=tmp_path / "data",
        source_roots=(source,),
        release_profiles_path=profiles,
    ).validate()
    profiles.write_text(
        '{"schema_version":1,"profiles":[]}',
        encoding="utf-8",
    )

    valid = doctor._release_profiles_status(settings)
    assert valid == {
        "path": str(profiles),
        "present": True,
        "valid": True,
        "configured": False,
        "profile_count": 0,
        "error_code": None,
    }

    profiles.write_text(
        '{"schema_version":1,"profiles":[],"secret":"do-not-echo"}',
        encoding="utf-8",
    )
    invalid = doctor._release_profiles_status(settings)
    assert invalid["present"] is True
    assert invalid["valid"] is False
    assert invalid["configured"] is False
    assert invalid["error_code"] == "release_profile_configuration_invalid"
    assert "do-not-echo" not in repr(invalid)


def test_runtime_credential_status_requires_regular_private_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "credentials"
    runtime.mkdir()
    credential = runtime / "imgbb-api-key"
    credential.write_text("encrypted-runtime-value", encoding="utf-8")
    credential.chmod(0o600)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(runtime))
    # Windows exposes ACL-backed files as 0666 regardless of chmod. Exercise
    # the production POSIX metadata contract without making this test host-only.
    monkeypatch.setattr(doctor.stat, "S_IMODE", lambda _mode: 0o600)

    status = doctor._credential_status("imgbb-api-key")

    assert status["configured"] is True
    assert status["runtime_loaded"] is True
    assert status["encrypted_at_rest"] is False
    assert status["runtime_plaintext"] is True
    assert status["metadata_ok"] is True

    credential.unlink()
    credential.mkdir()
    status = doctor._credential_status("imgbb-api-key")
    assert status["present"] is True
    assert status["configured"] is False
    assert status["metadata_ok"] is False


@pytest.mark.skipif(os.name == "nt", reason="reliable symlink metadata requires POSIX")
def test_runtime_credential_status_rejects_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "credentials"
    runtime.mkdir()
    target = runtime / "target"
    target.write_text("value", encoding="utf-8")
    (runtime / "catbox-userhash").symlink_to(target)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(runtime))

    status = doctor._credential_status("catbox-userhash")

    assert status["present"] is True
    assert status["configured"] is False
    assert status["metadata_ok"] is False


def test_installer_isolates_tests_from_live_runtime_configuration() -> None:
    installer = (ROOT / "install" / "install.sh").read_text(encoding="utf-8")
    start = installer.index("# The installer itself accepts BDENCODE_DATA_ROOT")
    end = installer.index("# Current VapourSynth", start)
    test_command = installer[start:end]

    runtime_variables = {f"BDENCODE_{field.name.upper()}" for field in fields(Settings)}
    runtime_variables.update(
        {
            "BDENCODE_CONFIG",
            "BDENCODE_DB_PATH",
            "BDENCODE_SOURCE_ROOT",
            "BDENCODE_CPU_PERCENT",
        }
    )
    for variable in runtime_variables:
        assert f"-u {variable}" in test_command
    assert '"$release_root/venv/bin/python" -m pytest' in test_command


def test_settings_cpu_quota_is_total_machine_fraction(tmp_path: Path) -> None:
    source = tmp_path / "storage"
    source.mkdir()
    settings = Settings(
        data_root=tmp_path / "encode", source_roots=(source,), cpu_limit_percent=80
    ).validate()
    assert settings.cpu_limit_percent == 80


def test_data_path_uses_required_children_for_runtime_writability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "encode"
    source = tmp_path / "storage"
    data.mkdir()
    source.mkdir()
    settings = Settings(data_root=data, source_roots=(source,)).validate()
    required = (
        settings.state_root,
        settings.jobs_root,
        settings.completed_root,
        settings.release_kits_root,
        settings.cache_root,
        settings.updates_root,
    )
    for path in required:
        path.mkdir()

    def access(path: Path, mode: int) -> bool:
        if mode == doctor.os.W_OK:
            return Path(path) != data
        return True

    monkeypatch.setattr(doctor.os, "access", access)

    report = doctor._data_path_check(settings)

    assert report["path"] == str(data)
    assert report["readable"] is True
    assert report["root_writable"] is False
    assert report["writable"] is True
    assert report["ok"] is True
    assert report["free_bytes"] > 0
    assert report["total_bytes"] > 0
    assert all(item["ok"] for item in report["required_writable_paths"].values())


@pytest.mark.parametrize("failure_mode", ["missing", "unwritable"])
def test_data_path_fails_when_a_required_child_is_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    data = tmp_path / "encode"
    source = tmp_path / "storage"
    data.mkdir()
    source.mkdir()
    settings = Settings(data_root=data, source_roots=(source,)).validate()
    required = {
        "state": settings.state_root,
        "jobs": settings.jobs_root,
        "completed": settings.completed_root,
        "cache": settings.cache_root,
        "updates": settings.updates_root,
    }
    failed_path = required["cache"]
    for path in required.values():
        if failure_mode != "missing" or path != failed_path:
            path.mkdir()

    def access(path: Path, mode: int) -> bool:
        if mode == doctor.os.W_OK:
            return Path(path) not in {data, failed_path}
        return True

    monkeypatch.setattr(doctor.os, "access", access)

    report = doctor._data_path_check(settings)

    assert report["root_writable"] is False
    assert report["writable"] is False
    assert report["ok"] is False
    assert report["required_writable_paths"]["cache"]["ok"] is False
