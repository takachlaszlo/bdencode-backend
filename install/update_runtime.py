#!/usr/bin/env python3
"""Durable runtime activation journal and recovery for BDEncode updates."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence


DEFAULT_STATE_ROOT = Path("/var/lib/bdencode/update-runtime")
DEFAULT_APT_STATE_ROOT = Path("/var/lib/bdencode/apt-transactions")
DEFAULT_APT_HELPER = Path("/usr/local/libexec/bdencode-apt-transaction")
VALID_STATES = {
    "BEGUN",
    "APT_PREPARED",
    "STAGING",
    "ACTIVATING",
    "HEALTHY",
    "COMMITTED",
}


class RuntimeError_(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"{utc_now()} {message}", flush=True)


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary_path.unlink(missing_ok=True)


def run(
    command: Sequence[str], *, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {"LANG": "C", "LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive"}
    )
    completed = subprocess.run(
        list(command),
        text=True,
        check=False,
        env=environment,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and completed.returncode:
        raise RuntimeError_(
            f"Command failed ({completed.returncode}): {' '.join(command[:3])}"
        )
    return completed


class RuntimeJournal:
    def __init__(
        self, state_root: Path, apt_state_root: Path, apt_helper: Path
    ) -> None:
        self.state_root = state_root
        self.journal_path = state_root / "active.json"
        self.apt_state_root = apt_state_root
        self.apt_helper = apt_helper

    def initialize(self) -> None:
        if os.geteuid() != 0:
            raise RuntimeError_("Runtime journal commands must run as root")
        if self.state_root.exists() or self.state_root.is_symlink():
            details = os.lstat(self.state_root)
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                raise RuntimeError_(f"Unsafe runtime state root: {self.state_root}")
            if details.st_uid != 0 or details.st_mode & 0o022:
                raise RuntimeError_(
                    f"Runtime state root must be root-owned: {self.state_root}"
                )
        else:
            self.state_root.mkdir(parents=True, mode=0o711)
        # Execute-only traversal lets the unprivileged worker stat the fixed
        # active marker without exposing the root-only journal contents.
        os.chmod(self.state_root, 0o711)

    def load(self, *, required: bool = True) -> dict[str, Any] | None:
        if not self.journal_path.exists():
            if required:
                raise RuntimeError_("No active runtime update journal")
            return None
        if self.journal_path.is_symlink() or not self.journal_path.is_file():
            raise RuntimeError_("Unsafe runtime update journal")
        try:
            journal = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError_(f"Invalid runtime update journal: {error}") from error
        if journal.get("schema") != 1 or journal.get("state") not in VALID_STATES:
            raise RuntimeError_("Unknown runtime update journal schema/state")
        return journal

    def save(self, journal: dict[str, Any]) -> None:
        journal["updated_at"] = utc_now()
        atomic_write(
            self.journal_path,
            (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        log(f"runtime update {journal['release_id']}: {journal['state']}")

    @staticmethod
    def release_path(app_root: Path, candidate: Path) -> Path:
        resolved = candidate.resolve(strict=True)
        releases = (app_root / "tools" / "releases").resolve(strict=True)
        if resolved.parent != releases or not resolved.is_dir():
            raise RuntimeError_(
                f"Tool release is outside the versioned release root: {resolved}"
            )
        return resolved

    def begin(
        self,
        *,
        release_id: str,
        data_root: Path,
        task_user: str,
        previous_tools: Path,
        api_active: bool,
        worker_active: bool,
        apt_daily_active: bool,
        apt_upgrade_active: bool,
    ) -> None:
        if self.load(required=False) is not None:
            raise RuntimeError_("An unfinished runtime update journal already exists")
        app_root = (data_root / "app").resolve(strict=True)
        previous = self.release_path(app_root, previous_tools)
        try:
            import pwd
        except ImportError as error:
            raise RuntimeError_("Runtime user lookup requires a POSIX host") from error
        user = pwd.getpwnam(task_user)
        if user.pw_dir == "/" or user.pw_uid == 0:
            raise RuntimeError_("Unsafe BDEncode runtime user")
        journal: dict[str, Any] = {
            "schema": 1,
            "release_id": release_id,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "state": "BEGUN",
            "data_root": str(data_root.resolve(strict=True)),
            "app_root": str(app_root),
            "task_user": task_user,
            "previous_tools": str(previous),
            "candidate_tools": None,
            "tools_activated": False,
            "apt_changed": None,
            "api_was_active": api_active,
            "worker_was_active": worker_active,
            "apt_daily_was_active": apt_daily_active,
            "apt_upgrade_was_active": apt_upgrade_active,
        }
        self.save(journal)

    def apt_prepared(self) -> None:
        journal = self.load()
        assert journal is not None
        active_id = self.apt_active_id()
        if active_id is not None and active_id != journal["release_id"]:
            raise RuntimeError_("APT/runtime transaction identity mismatch")
        journal["apt_changed"] = active_id is not None
        journal["state"] = "APT_PREPARED"
        self.save(journal)

    def activating(self, candidate_tools: Path) -> None:
        journal = self.load()
        assert journal is not None
        app_root = Path(journal["app_root"])
        candidate = self.release_path(app_root, candidate_tools)
        journal["candidate_tools"] = str(candidate)
        journal["tools_activated"] = True
        journal["state"] = "ACTIVATING"
        self.save(journal)

    def staging(self, candidate_tools: Path) -> None:
        journal = self.load()
        assert journal is not None
        app_root = Path(journal["app_root"])
        releases = (app_root / "tools" / "releases").resolve(strict=True)
        candidate = candidate_tools.absolute()
        if (
            candidate.parent.resolve(strict=True) != releases
            or candidate.name != journal["release_id"]
        ):
            raise RuntimeError_(f"Unsafe staged tool release path: {candidate}")
        journal["candidate_tools"] = str(candidate)
        journal["tools_activated"] = False
        journal["state"] = "STAGING"
        self.save(journal)

    def candidate_discarded(self) -> None:
        journal = self.load()
        assert journal is not None
        if journal["tools_activated"]:
            raise RuntimeError_("Cannot discard a tool release after activation")
        journal["candidate_tools"] = None
        journal["state"] = "APT_PREPARED"
        self.save(journal)

    def healthy(self) -> None:
        journal = self.load()
        assert journal is not None
        if journal["state"] not in {"APT_PREPARED", "ACTIVATING"}:
            raise RuntimeError_(f"Cannot mark services healthy from {journal['state']}")
        journal["state"] = "HEALTHY"
        self.save(journal)

    def commit(self) -> None:
        journal = self.load()
        assert journal is not None
        if journal["state"] != "HEALTHY":
            raise RuntimeError_(f"Cannot commit runtime from {journal['state']}")
        journal["state"] = "COMMITTED"
        self.save(journal)

    def apt_active_id(self) -> str | None:
        active = self.apt_state_root / "active"
        if not active.exists():
            return None
        if active.is_symlink() or not active.is_file():
            raise RuntimeError_("Unsafe APT active marker")
        return active.read_text(encoding="ascii").strip()

    def apt_transaction_state(self, release_id: str) -> str | None:
        state = self.apt_state_root / release_id / "state"
        if not state.exists():
            return None
        if state.is_symlink() or not state.is_file():
            raise RuntimeError_("Unsafe APT transaction state")
        return state.read_text(encoding="ascii").strip()

    def clear(self) -> None:
        self.journal_path.unlink()
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(self.state_root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    @staticmethod
    def switch_tools(journal: dict[str, Any], target: Path) -> None:
        app_root = Path(journal["app_root"])
        resolved = RuntimeJournal.release_path(app_root, target)
        tools_root = app_root / "tools"
        current = tools_root / "current"
        temporary = tools_root / f".current-recovery-{os.getpid()}"
        temporary.unlink(missing_ok=True)
        os.symlink(resolved, temporary)
        os.replace(temporary, current)
        directory = os.open(tools_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        log(f"runtime tools pointer now references {resolved}")

    @staticmethod
    def remove_staged_candidate(journal: dict[str, Any]) -> None:
        value = journal.get("candidate_tools")
        if value is None:
            return
        candidate = Path(value)
        app_root = Path(journal["app_root"])
        releases = (app_root / "tools" / "releases").resolve(strict=True)
        if candidate.parent.resolve(strict=True) != releases:
            raise RuntimeError_(f"Refusing to remove unsafe staged path: {candidate}")
        current = (app_root / "tools" / "current").resolve(strict=True)
        if candidate.exists() and candidate.resolve(strict=True) != current:
            if candidate.is_symlink() or not candidate.is_dir():
                raise RuntimeError_(
                    f"Refusing to remove unsafe staged release: {candidate}"
                )
            shutil.rmtree(candidate)
            log(f"removed uncommitted tool candidate {candidate}")

    @staticmethod
    def stop_services() -> None:
        units = ("bdencode-worker.service", "bdencode-api.service")
        for unit in units:
            observed = run(
                ["systemctl", "show", "--property=ActiveState", "--value", unit],
                check=False,
                capture=True,
            )
            state = (observed.stdout or "").strip()
            if state in {"active", "reloading", "activating", "deactivating"}:
                run(["systemctl", "stop", unit])
            elif state not in {"", "inactive", "failed"}:
                raise RuntimeError_(f"Unknown {unit} ActiveState: {state}")
            final = run(
                ["systemctl", "show", "--property=ActiveState", "--value", unit],
                check=False,
                capture=True,
            )
            final_state = (final.stdout or "").strip()
            if final_state not in {"", "inactive", "failed"}:
                raise RuntimeError_(
                    f"Refusing recovery while {unit} is {final_state}"
                )

    @staticmethod
    def wait_for_api() -> None:
        for _attempt in range(20):
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:8796/api/v1/health", timeout=2
                ) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
            time.sleep(1)
        raise RuntimeError_("BDEncode API health check failed during recovery")

    @staticmethod
    def run_doctor(journal: dict[str, Any]) -> None:
        app_root = Path(journal["app_root"])
        data_root = Path(journal["data_root"])
        task_user = journal["task_user"]
        backend = app_root / "current" / "venv" / "bin" / "bdencode"
        tools = app_root / "tools" / "current"
        if not backend.is_file():
            return
        environment = (
            f"PATH={tools}/bin:{app_root}/current/venv/bin:/usr/local/bin:/usr/bin:/bin"
        )
        run(
            [
                "runuser",
                "-u",
                task_user,
                "--",
                "env",
                "BDENCODE_CONFIG=/etc/bdencode/config.toml",
                environment,
                f"XDG_CACHE_HOME={data_root}/cache",
                f"XDG_CONFIG_HOME={tools}/config",
                str(backend),
                "doctor",
                "--json",
            ]
        )

    @staticmethod
    def restore_timer_state(journal: dict[str, Any]) -> None:
        timer_states = (
            ("apt-daily.timer", journal["apt_daily_was_active"]),
            ("apt-daily-upgrade.timer", journal["apt_upgrade_was_active"]),
        )
        for unit, was_active in timer_states:
            action = "start" if was_active else "stop"
            run(["systemctl", action, unit])
            result = run(
                ["systemctl", "is-active", "--quiet", unit], check=False
            )
            if (result.returncode == 0) != was_active:
                raise RuntimeError_(
                    f"Could not restore {unit} to "
                    f"{'active' if was_active else 'inactive'}"
                )

    @staticmethod
    def restore_runtime_state(journal: dict[str, Any]) -> None:
        RuntimeJournal.restore_timer_state(journal)
        if journal["api_was_active"]:
            run(["systemctl", "start", "bdencode-api.service"])
            RuntimeJournal.wait_for_api()
        else:
            run(["systemctl", "stop", "bdencode-api.service"])
        if journal["worker_was_active"]:
            # The worker unit is Type=notify, so a successful start means its
            # database/config initialization and singleton lock are ready.
            run(["systemctl", "start", "bdencode-worker.service"])
            result = run(
                ["systemctl", "is-active", "--quiet", "bdencode-worker.service"],
                check=False,
            )
            if result.returncode:
                raise RuntimeError_("BDEncode worker did not start during recovery")
        else:
            run(["systemctl", "stop", "bdencode-worker.service"])

    def recover(self, *, restore_runtime: bool, restore_timers: bool = False) -> None:
        journal = self.load(required=False)
        apt_active = self.apt_active_id()
        if journal is None and apt_active is None:
            log("runtime recovery: no unfinished update")
            return

        if journal is not None and journal["state"] == "BEGUN" and apt_active is None:
            if restore_runtime:
                self.restore_runtime_state(journal)
            elif restore_timers:
                self.restore_timer_state(journal)
            self.clear()
            log("untouched runtime journal finalized")
            return

        if (
            journal is not None
            and journal["state"] == "COMMITTED"
            and apt_active is None
        ):
            if restore_runtime:
                self.restore_runtime_state(journal)
            elif restore_timers:
                self.restore_timer_state(journal)
            self.clear()
            log("committed runtime journal finalized")
            return

        self.stop_services()
        keep_candidate = False
        if journal is not None:
            release_id = journal["release_id"]
            apt_state = self.apt_transaction_state(release_id)
            apt_changed = bool(journal["apt_changed"]) or apt_active == release_id
            package_commit_exists = apt_changed and apt_state == "COMMITTED"
            keep_candidate = journal["state"] in {"HEALTHY", "COMMITTED"} and (
                not apt_changed or package_commit_exists
            )
            if not keep_candidate:
                self.switch_tools(journal, Path(journal["previous_tools"]))

        run([str(self.apt_helper), "recover"])

        if journal is not None:
            if keep_candidate and journal["candidate_tools"] is not None:
                self.switch_tools(journal, Path(journal["candidate_tools"]))
            elif keep_candidate and journal["candidate_tools"] is None:
                self.switch_tools(journal, Path(journal["previous_tools"]))
            self.run_doctor(journal)
            if not keep_candidate:
                self.remove_staged_candidate(journal)
            if restore_runtime:
                self.restore_runtime_state(journal)
            elif restore_timers:
                self.restore_timer_state(journal)
            self.clear()
            log("runtime update journal finalized")

    def show_status(self) -> None:
        journal = self.load(required=False)
        print(
            json.dumps(
                {"active": journal is not None, "journal": journal}, sort_keys=True
            )
        )


def bool_value(value: str) -> bool:
    if value not in {"0", "1"}:
        raise argparse.ArgumentTypeError("expected 0 or 1")
    return value == "1"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "command",
        choices=(
            "begin",
            "apt-prepared",
            "staging",
            "activating",
            "candidate-discarded",
            "healthy",
            "commit",
            "recover",
            "status",
        ),
    )
    result.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    result.add_argument("--apt-state-root", type=Path, default=DEFAULT_APT_STATE_ROOT)
    result.add_argument("--apt-helper", type=Path, default=DEFAULT_APT_HELPER)
    result.add_argument("--release-id")
    result.add_argument("--data-root", type=Path)
    result.add_argument("--task-user")
    result.add_argument("--previous-tools", type=Path)
    result.add_argument("--candidate-tools", type=Path)
    result.add_argument("--api-active", type=bool_value)
    result.add_argument("--worker-active", type=bool_value)
    result.add_argument("--apt-daily-active", type=bool_value)
    result.add_argument("--apt-upgrade-active", type=bool_value)
    result.add_argument("--restore-runtime", action="store_true")
    result.add_argument("--restore-timers", action="store_true")
    return result


def require(value: Any, name: str) -> Any:
    if value is None:
        raise RuntimeError_(f"{name} is required")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    journal = RuntimeJournal(args.state_root, args.apt_state_root, args.apt_helper)
    try:
        journal.initialize()
        if args.command == "begin":
            journal.begin(
                release_id=require(args.release_id, "--release-id"),
                data_root=require(args.data_root, "--data-root"),
                task_user=require(args.task_user, "--task-user"),
                previous_tools=require(args.previous_tools, "--previous-tools"),
                api_active=require(args.api_active, "--api-active"),
                worker_active=require(args.worker_active, "--worker-active"),
                apt_daily_active=require(args.apt_daily_active, "--apt-daily-active"),
                apt_upgrade_active=require(
                    args.apt_upgrade_active, "--apt-upgrade-active"
                ),
            )
        elif args.command == "apt-prepared":
            journal.apt_prepared()
        elif args.command == "staging":
            journal.staging(require(args.candidate_tools, "--candidate-tools"))
        elif args.command == "activating":
            journal.activating(require(args.candidate_tools, "--candidate-tools"))
        elif args.command == "candidate-discarded":
            journal.candidate_discarded()
        elif args.command == "healthy":
            journal.healthy()
        elif args.command == "commit":
            journal.commit()
        elif args.command == "recover":
            if args.restore_runtime and args.restore_timers:
                raise RuntimeError_(
                    "Choose either --restore-runtime or --restore-timers"
                )
            journal.recover(
                restore_runtime=args.restore_runtime,
                restore_timers=args.restore_timers,
            )
        elif args.command == "status":
            journal.show_status()
        return 0
    except (OSError, RuntimeError_, subprocess.SubprocessError) as error:
        print(f"bdencode runtime recovery error: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
