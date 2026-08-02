from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).parents[1] / "install" / "update_runtime.py"
DAILY_UPDATE_PATH = Path(__file__).parents[1] / "install" / "daily-update.sh"
INSTALL_PATH = Path(__file__).parents[1] / "install" / "install.sh"
SPEC = importlib.util.spec_from_file_location("bdencode_update_runtime", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
update_runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = update_runtime
SPEC.loader.exec_module(update_runtime)


def test_daily_update_compares_uv_managed_environments_with_uv() -> None:
    script = DAILY_UPDATE_PATH.read_text(encoding="utf-8")

    assert 'UV_PYTHON_INSTALL_DIR="$tool_release/.python"' in script
    assert (
        '"$current_backend/venv/bin/uv" python install --no-bin --upgrade 3.12'
        in script
    )
    assert "venv --allow-existing --managed-python" in script
    assert '--no-python-downloads --python 3.12 "$tool_release"' in script
    assert 'UV_PYTHON_INSTALL_DIR="$app_root/tools/python"' not in script
    assert script.count('"$current_backend/venv/bin/uv" pip freeze') == 2
    assert '"$current_tools/bin/python" -m pip freeze' not in script
    assert '"$tool_release/bin/python" -m pip freeze' not in script
    assert "<(runuser" not in script
    assert '>"$current_freeze"' in script
    assert '>"$candidate_freeze"' in script
    assert 'realpath --relative-to="$current_tools_resolved"' in script
    assert 'realpath --relative-to="$candidate_tools_resolved"' in script
    assert '"$candidate_tools_resolved"/.python/*' in script
    assert '"$current_tools/bin/python" -VV' in script
    assert '"$tool_release/bin/python" -VV' in script
    assert 'cmp -s "$current_tools/bin/python" "$tool_release/bin/python"' in script


def test_installer_uses_release_local_managed_python() -> None:
    script = INSTALL_PATH.read_text(encoding="utf-8")

    assert 'UV_PYTHON_INSTALL_DIR="$tool_release/.python"' in script
    assert 'python install --no-bin --upgrade 3.12' in script
    assert "venv --allow-existing --managed-python" in script
    assert '--no-python-downloads --python 3.12 "$tool_release"' in script
    assert 'UV_PYTHON_INSTALL_DIR="$app_root/tools/python"' not in script


def test_bool_value_is_fail_closed() -> None:
    assert update_runtime.bool_value("1") is True
    assert update_runtime.bool_value("0") is False
    with pytest.raises(Exception):
        update_runtime.bool_value("yes")


def test_atomic_runtime_journal_round_trip(tmp_path: Path) -> None:
    journal = update_runtime.RuntimeJournal(
        tmp_path, tmp_path / "apt", tmp_path / "apt-helper"
    )
    payload = {
        "schema": 1,
        "release_id": "20260802T123456Z-42",
        "state": "BEGUN",
    }
    journal.save(payload)
    assert journal.load() == {
        **payload,
        "updated_at": payload["updated_at"],
    }
    if os.name == "posix":
        assert stat_mode(journal.journal_path) == 0o600


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_load_rejects_unknown_state(tmp_path: Path) -> None:
    journal = update_runtime.RuntimeJournal(
        tmp_path, tmp_path / "apt", tmp_path / "apt-helper"
    )
    tmp_path.mkdir(exist_ok=True)
    journal.journal_path.write_text(
        json.dumps({"schema": 1, "state": "BROKEN"}), encoding="utf-8"
    )
    with pytest.raises(update_runtime.RuntimeError_, match="schema/state"):
        journal.load()


def test_release_path_rejects_escape(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    releases = app_root / "tools" / "releases"
    valid = releases / "20260802T123456Z-42"
    valid.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    assert (
        update_runtime.RuntimeJournal.release_path(app_root, valid) == valid.resolve()
    )
    with pytest.raises(update_runtime.RuntimeError_, match="outside"):
        update_runtime.RuntimeJournal.release_path(app_root, outside)


def runtime_payload(
    tmp_path: Path, state: str, *, apt_changed: bool
) -> dict[str, object]:
    return {
        "schema": 1,
        "release_id": "20260802T123456Z-42",
        "state": state,
        "data_root": str(tmp_path / "data"),
        "app_root": str(tmp_path / "data" / "app"),
        "task_user": "encoder",
        "previous_tools": str(tmp_path / "previous"),
        "candidate_tools": str(tmp_path / "candidate"),
        "tools_activated": state in {"ACTIVATING", "HEALTHY", "COMMITTED"},
        "apt_changed": apt_changed,
        "api_was_active": True,
        "worker_was_active": True,
        "apt_daily_was_active": True,
        "apt_upgrade_was_active": True,
    }


def test_recovery_rolls_tools_back_before_uncommitted_apt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apt_root = tmp_path / "apt"
    transaction = apt_root / "20260802T123456Z-42"
    transaction.mkdir(parents=True)
    (transaction / "state").write_text("VALIDATING\n", encoding="ascii")
    (apt_root / "active").write_text("20260802T123456Z-42\n", encoding="ascii")
    journal = update_runtime.RuntimeJournal(
        tmp_path / "runtime", apt_root, Path("/apt-helper")
    )
    journal.state_root.mkdir()
    journal.save(runtime_payload(tmp_path, "ACTIVATING", apt_changed=True))
    events: list[object] = []
    monkeypatch.setattr(journal, "stop_services", lambda: events.append("stop"))
    monkeypatch.setattr(
        journal, "switch_tools", lambda _journal, target: events.append(target)
    )
    monkeypatch.setattr(journal, "run_doctor", lambda _journal: events.append("doctor"))
    monkeypatch.setattr(
        journal, "restore_runtime_state", lambda _journal: events.append("restore")
    )
    monkeypatch.setattr(
        journal, "remove_staged_candidate", lambda _journal: events.append("remove")
    )
    monkeypatch.setattr(
        update_runtime,
        "run",
        lambda command, check=True, capture=False: events.append(tuple(command))
        or SimpleNamespace(returncode=0),
    )

    journal.recover(restore_runtime=True)

    assert events[:3] == [
        "stop",
        Path(tmp_path / "previous"),
        (str(Path("/apt-helper")), "recover"),
    ]
    assert events[-3:] == ["doctor", "remove", "restore"]
    assert not journal.journal_path.exists()


def test_recovery_keeps_healthy_candidate_after_apt_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apt_root = tmp_path / "apt"
    transaction = apt_root / "20260802T123456Z-42"
    transaction.mkdir(parents=True)
    (transaction / "state").write_text("COMMITTED\n", encoding="ascii")
    journal = update_runtime.RuntimeJournal(
        tmp_path / "runtime", apt_root, Path("/apt-helper")
    )
    journal.state_root.mkdir()
    journal.save(runtime_payload(tmp_path, "HEALTHY", apt_changed=True))
    targets: list[Path] = []
    monkeypatch.setattr(journal, "stop_services", lambda: None)
    monkeypatch.setattr(
        journal, "switch_tools", lambda _journal, target: targets.append(target)
    )
    monkeypatch.setattr(journal, "run_doctor", lambda _journal: None)
    monkeypatch.setattr(journal, "restore_runtime_state", lambda _journal: None)
    monkeypatch.setattr(
        update_runtime,
        "run",
        lambda command, check=True, capture=False: SimpleNamespace(returncode=0),
    )

    journal.recover(restore_runtime=True)

    assert targets == [Path(tmp_path / "candidate")]


def test_untouched_journal_does_not_stop_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = update_runtime.RuntimeJournal(
        tmp_path / "runtime", tmp_path / "apt", Path("/apt")
    )
    journal.state_root.mkdir()
    journal.save(runtime_payload(tmp_path, "BEGUN", apt_changed=False))
    events: list[str] = []
    monkeypatch.setattr(journal, "stop_services", lambda: events.append("stop"))
    monkeypatch.setattr(
        journal, "restore_runtime_state", lambda _journal: events.append("restore")
    )

    journal.recover(restore_runtime=True)

    assert events == ["restore"]


def test_boot_recovery_can_restore_only_apt_timers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = update_runtime.RuntimeJournal(
        tmp_path / "runtime", tmp_path / "apt", Path("/apt")
    )
    journal.state_root.mkdir()
    journal.save(runtime_payload(tmp_path, "BEGUN", apt_changed=False))
    events: list[str] = []
    monkeypatch.setattr(
        journal, "restore_timer_state", lambda _journal: events.append("timers")
    )
    monkeypatch.setattr(
        journal,
        "restore_runtime_state",
        lambda _journal: pytest.fail("boot recovery started the runtime"),
    )

    journal.recover(restore_runtime=False, restore_timers=True)

    assert events == ["timers"]
    assert not journal.journal_path.exists()


def test_recovery_refuses_to_mutate_while_a_service_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_run(command, check=True, capture=False):
        normalized = tuple(command)
        commands.append(normalized)
        state = (
            "active\n"
            if normalized[-1] == "bdencode-worker.service"
            else "inactive\n"
        )
        return SimpleNamespace(returncode=0, stdout=state)

    monkeypatch.setattr(update_runtime, "run", fake_run)
    with pytest.raises(update_runtime.RuntimeError_, match="Refusing recovery"):
        update_runtime.RuntimeJournal.stop_services()
    assert commands[0][:2] == ("systemctl", "show")


def test_timer_restore_is_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command, check=True, capture=False):
        normalized = tuple(command)
        if normalized[:3] == ("systemctl", "is-active", "--quiet"):
            return SimpleNamespace(returncode=3)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(update_runtime, "run", fake_run)
    journal = {
        "apt_daily_was_active": True,
        "apt_upgrade_was_active": False,
        "api_was_active": False,
        "worker_was_active": False,
    }
    with pytest.raises(update_runtime.RuntimeError_, match="apt-daily.timer"):
        update_runtime.RuntimeJournal.restore_runtime_state(journal)
