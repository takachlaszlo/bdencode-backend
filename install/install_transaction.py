#!/usr/bin/env python3
"""Durable fixed-target rollback journal for BDEncode installations.

The installer invokes ``begin`` before changing application pointers or host
configuration, ``commit`` after all health checks, and ``recover`` on entry and
from its EXIT trap.  Recovery is idempotent, so a second interruption merely
continues restoring the same root-owned snapshot.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

try:  # pragma: no cover - production is Linux; pure state logic is tested on Windows.
    import fcntl
except ModuleNotFoundError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


DEFAULT_STATE_ROOT = Path("/var/lib/bdencode/install-transactions")
INSTALLER_APT_LOCK = Path("/run/lock/bdencode-installer-apt.lock")
NATIVE_APT_LOCKS = (
    Path("/var/lib/dpkg/lock-frontend"),
    Path("/var/lib/dpkg/lock"),
    Path("/var/cache/apt/archives/lock"),
    Path("/var/lib/apt/lists/lock"),
)
TRANSACTION_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9]+$")
RECOVERABLE_STATES = {"PREPARED", "RESTORING", "RECOVERY_REQUIRED"}
FINAL_STATES = {"COMMITTED", "RESTORED"}
VALID_STATES = RECOVERABLE_STATES | FINAL_STATES | {"OBSERVING", "HEALTHY"}
APPLICATION_UNITS = (
    "bdencode-api.service",
    "bdencode-worker.service",
    "bdencode-update.timer",
)
APT_TIMER_UNITS = ("apt-daily.timer", "apt-daily-upgrade.timer")
MANAGED_UNITS = APPLICATION_UNITS + APT_TIMER_UNITS

# Mutable host files restored by an installer rollback. Stable recovery
# helpers/units/drop-ins are deliberately installed before this snapshot and
# retained so they can understand and finish the published journal.
SYSTEM_TARGETS = (
    Path("/etc/bdencode/config.toml"),
    Path("/etc/bdencode/media-apt.sources.list"),
    Path("/etc/apt/preferences.d/bdencode-media"),
    Path("/etc/systemd/system/bdencode-api.service"),
    Path("/etc/systemd/system/bdencode-worker.service"),
    Path("/etc/systemd/system/bdencode-update.service"),
    Path("/etc/systemd/system/bdencode-update.timer"),
    Path("/etc/systemd/system/bdencode-worker.service.d/credential.conf"),
    Path("/etc/systemd/system/apt-daily.service.d/bdencode-recovery.conf"),
    Path("/etc/systemd/system/apt-daily-upgrade.service.d/bdencode-recovery.conf"),
    Path("/usr/local/libexec/bdencode-daily-update"),
    Path("/etc/nginx/apps/bdencode.conf"),
    Path("/var/www/bdencode/current"),
)


class InstallTransactionError(RuntimeError):
    """A fail-closed installer snapshot or recovery error."""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"{utc_now()} {message}", flush=True)


def fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        fsync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def root_path(path: Path, description: str) -> Path:
    if not path.is_absolute() or path == Path(path.anchor):
        raise InstallTransactionError(f"Unsafe {description}: {path}")
    return path


class InstallTransaction:
    def __init__(
        self,
        state_root: Path = DEFAULT_STATE_ROOT,
        fixed_targets: Sequence[Path] = SYSTEM_TARGETS,
    ) -> None:
        self.state_root = state_root
        self.active_file = state_root / "active"
        self.pending_services_file = state_root / "services-pending"
        self.fixed_targets = tuple(root_path(Path(item), "managed target") for item in fixed_targets)
        if len(set(self.fixed_targets)) != len(self.fixed_targets):
            raise InstallTransactionError("Duplicate managed installer target")

    def initialize(self) -> None:
        getuid = getattr(os, "geteuid", None)
        testing = os.environ.get("BDENCODE_INSTALL_TESTING") == "1"
        if getuid is not None and getuid() != 0 and not testing:
            raise InstallTransactionError("Installer transaction commands must run as root")
        root_path(self.state_root, "installer transaction root")
        details = safe_lstat(self.state_root)
        if details is None:
            self.state_root.mkdir(parents=True, mode=0o700)
        else:
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                raise InstallTransactionError(
                    f"Unsafe installer transaction root: {self.state_root}"
                )
            if getuid is not None and not testing and details.st_uid != 0:
                raise InstallTransactionError(
                    f"Installer transaction root is not root-owned: {self.state_root}"
                )
            if not testing and details.st_mode & 0o022:
                raise InstallTransactionError(
                    f"Installer transaction root is group/world writable: {self.state_root}"
                )
        # Execute-only traversal lets runtime services stat the fixed active
        # marker without reading root-only transaction contents.
        os.chmod(self.state_root, 0o711)

    def transaction_dir(self, transaction_id: str) -> Path:
        if not TRANSACTION_ID_RE.fullmatch(transaction_id):
            raise InstallTransactionError(
                f"Invalid installer transaction id: {transaction_id!r}"
            )
        return self.state_root / transaction_id

    def marker_id(self, marker: Path, description: str) -> str | None:
        details = safe_lstat(marker)
        if details is None:
            return None
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise InstallTransactionError(f"Unsafe installer {description} marker")
        transaction_id = marker.read_text(encoding="ascii").strip()
        self.transaction_dir(transaction_id)
        return transaction_id

    def active_id(self) -> str | None:
        return self.marker_id(self.active_file, "active")

    def pending_services_id(self) -> str | None:
        return self.marker_id(self.pending_services_file, "services-pending")

    def transaction_from_marker(self, marker_id: str | None, description: str) -> Path | None:
        if marker_id is None:
            return None
        transaction = self.transaction_dir(marker_id)
        details = safe_lstat(transaction)
        if details is None or stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise InstallTransactionError(
                f"Missing or unsafe installer {description} transaction: {transaction}"
            )
        return transaction

    def active_dir(self) -> Path | None:
        return self.transaction_from_marker(self.active_id(), "active")

    def pending_services_dir(self) -> Path | None:
        return self.transaction_from_marker(
            self.pending_services_id(), "services-pending"
        )

    @staticmethod
    def state(transaction: Path) -> str:
        state_path = transaction / "state"
        details = safe_lstat(state_path)
        if details is None or stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise InstallTransactionError(f"Missing or unsafe transaction state: {state_path}")
        value = state_path.read_text(encoding="ascii").strip()
        if value not in VALID_STATES:
            raise InstallTransactionError(f"Unknown installer transaction state: {value}")
        return value

    @staticmethod
    def write_state(transaction: Path, state: str) -> None:
        if state not in VALID_STATES:
            raise InstallTransactionError(f"Invalid installer state: {state}")
        atomic_write(transaction / "state", f"{state}\n".encode("ascii"))
        log(f"installer transaction {transaction.name}: {state}")

    @staticmethod
    def app_root(path: Path) -> Path:
        root_path(path, "application root")
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise InstallTransactionError(f"Application root does not exist: {path}") from error
        if resolved == Path(resolved.anchor) or not resolved.is_dir():
            raise InstallTransactionError(f"Unsafe application root: {resolved}")
        tools_root = resolved / "tools"
        if not tools_root.is_dir() or tools_root.is_symlink():
            raise InstallTransactionError(f"Missing application tools root: {tools_root}")
        return resolved

    def managed_targets(self, app_root: Path) -> tuple[Path, ...]:
        resolved = self.app_root(app_root)
        targets = (*self.fixed_targets, resolved / "current", resolved / "tools" / "current")
        if len(set(targets)) != len(targets):
            raise InstallTransactionError("Managed installer targets overlap")
        return targets

    @staticmethod
    def snapshot_target(target: Path, backup_root: Path, index: int) -> dict[str, Any]:
        details = safe_lstat(target)
        entry: dict[str, Any] = {"path": str(target)}
        if details is None:
            entry["kind"] = "absent"
            return entry
        if stat.S_ISLNK(details.st_mode):
            entry.update(
                {
                    "kind": "symlink",
                    "link_target": os.readlink(target),
                    "uid": details.st_uid,
                    "gid": details.st_gid,
                }
            )
            return entry
        if not stat.S_ISREG(details.st_mode):
            raise InstallTransactionError(
                f"Managed target is not a regular file or symlink: {target}"
            )

        relative = Path("files") / f"{index:04d}.bin"
        backup = backup_root / relative
        backup.parent.mkdir(mode=0o700, exist_ok=True)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (details.st_dev, details.st_ino):
                raise InstallTransactionError(f"Managed target changed during backup: {target}")
            with os.fdopen(os.dup(descriptor), "rb") as source, backup.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
        finally:
            os.close(descriptor)
        os.chmod(backup, 0o600)
        with backup.open("r+b") as stream:
            os.fsync(stream.fileno())
        entry.update(
            {
                "kind": "file",
                "backup": str(relative),
                "sha256": sha256_file(backup),
                "size": opened.st_size,
                "mode": stat.S_IMODE(opened.st_mode),
                "uid": opened.st_uid,
                "gid": opened.st_gid,
                "atime_ns": opened.st_atime_ns,
                "mtime_ns": opened.st_mtime_ns,
            }
        )
        return entry

    def begin(
        self,
        transaction_id: str,
        app_root: Path,
        unit_states: dict[str, dict[str, bool]] | None = None,
    ) -> None:
        if self.active_id() is not None or self.pending_services_id() is not None:
            raise InstallTransactionError(
                "An unfinished installer transaction already exists; recover it first"
            )
        transaction = self.transaction_dir(transaction_id)
        if transaction.exists() or transaction.is_symlink():
            raise InstallTransactionError(f"Installer transaction already exists: {transaction}")
        resolved_app = self.app_root(app_root)
        targets = self.managed_targets(resolved_app)
        if unit_states is None:
            unit_states = {
                unit: {"active": False, "enabled": False}
                for unit in MANAGED_UNITS
            }
        if set(unit_states) != set(MANAGED_UNITS) or any(
            set(state) != {"active", "enabled"}
            or not all(isinstance(value, bool) for value in state.values())
            for state in unit_states.values()
        ):
            raise InstallTransactionError("Invalid managed unit-state snapshot")
        building = Path(
            tempfile.mkdtemp(prefix=f".building-{transaction_id}-", dir=self.state_root)
        )
        os.chmod(building, 0o700)
        try:
            entries = [
                self.snapshot_target(target, building, index)
                for index, target in enumerate(targets)
            ]
            manifest = {
                "schema": 1,
                "transaction_id": transaction_id,
                "created_at": utc_now(),
                "app_root": str(resolved_app),
                "unit_states": unit_states,
                "targets": entries,
            }
            atomic_write(
                building / "manifest.json",
                (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            # No mutable host/app target has changed yet. During OBSERVING the
            # installer may stop only the API/timer to inspect the queue; a
            # rollback must never interrupt the still-running worker.
            self.write_state(building, "OBSERVING")
            if (building / "files").is_dir():
                fsync_directory(building / "files")
            fsync_directory(building)
            os.replace(building, transaction)
            fsync_directory(self.state_root)
            atomic_write(self.active_file, f"{transaction_id}\n".encode("ascii"))
        except Exception:
            shutil.rmtree(building, ignore_errors=True)
            raise
        log(f"installer rollback snapshot prepared for {len(entries)} target(s)")

    def load_manifest(self, transaction: Path) -> dict[str, Any]:
        manifest_path = transaction / "manifest.json"
        details = safe_lstat(manifest_path)
        if details is None or stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise InstallTransactionError(f"Missing or unsafe installer manifest: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise InstallTransactionError(f"Invalid installer manifest: {error}") from error
        if manifest.get("schema") != 1 or manifest.get("transaction_id") != transaction.name:
            raise InstallTransactionError("Installer manifest identity mismatch")
        app_root = self.app_root(Path(manifest.get("app_root", "")))
        allowed = set(self.managed_targets(app_root))
        entries = manifest.get("targets")
        if not isinstance(entries, list) or not entries:
            raise InstallTransactionError("Installer manifest has no target entries")
        seen: set[Path] = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise InstallTransactionError("Malformed installer target entry")
            target = Path(entry["path"])
            if target not in allowed or target in seen:
                raise InstallTransactionError(f"Unmanaged or duplicate installer target: {target}")
            if entry.get("kind") not in {"absent", "file", "symlink"}:
                raise InstallTransactionError(f"Unknown installer target kind: {target}")
            seen.add(target)
        dynamic = {app_root / "current", app_root / "tools" / "current"}
        if not dynamic.issubset(seen):
            raise InstallTransactionError("Installer manifest is missing release pointers")
        unit_states = manifest.get("unit_states")
        if not isinstance(unit_states, dict) or set(unit_states) != set(MANAGED_UNITS):
            raise InstallTransactionError("Installer manifest has invalid unit states")
        for unit, state in unit_states.items():
            if (
                not isinstance(state, dict)
                or set(state) != {"active", "enabled"}
                or not all(isinstance(value, bool) for value in state.values())
            ):
                raise InstallTransactionError(
                    f"Installer manifest has invalid state for {unit}"
                )
        return manifest

    @staticmethod
    def run(
        command: Sequence[str], *, check: bool = True, capture: bool = False
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            list(command),
            text=True,
            check=False,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
        if check and completed.returncode:
            raise InstallTransactionError(
                f"Command failed ({completed.returncode}): {' '.join(command[:3])}"
            )
        return completed

    @staticmethod
    def production_runtime() -> bool:
        return os.environ.get("BDENCODE_INSTALL_TESTING") != "1"

    def stop_runtime(self) -> None:
        if not self.production_runtime():
            return
        for unit in (
            "bdencode-update.timer",
            "bdencode-worker.service",
            "bdencode-api.service",
            *APT_TIMER_UNITS,
        ):
            self.run(["systemctl", "stop", unit], check=False)
            state = self.run(
                ["systemctl", "show", "--property=ActiveState", "--value", unit],
                check=False,
                capture=True,
            ).stdout.strip()
            if state not in {"", "inactive", "failed"}:
                raise InstallTransactionError(
                    f"Refusing installer recovery while {unit} is {state}"
                )

    @staticmethod
    def quiesce_apt_services() -> None:
        for unit in APT_TIMER_UNITS:
            InstallTransaction.run(["systemctl", "stop", unit], check=False)
        for unit in ("apt-daily.service", "apt-daily-upgrade.service"):
            for _attempt in range(120):
                observed = InstallTransaction.run(
                    ["systemctl", "show", "--property=ActiveState", "--value", unit],
                    check=False,
                    capture=True,
                )
                state = (observed.stdout or "").strip()
                if state in {"", "inactive", "failed"}:
                    break
                time.sleep(1)
            else:
                raise InstallTransactionError(
                    f"Timed out waiting for package service {unit}"
                )

    @staticmethod
    def open_lock(path: Path) -> int:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o640)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != 0
            or details.st_mode & 0o022
        ):
            os.close(descriptor)
            raise InstallTransactionError(f"Unsafe package-manager lock: {path}")
        return descriptor

    @staticmethod
    def acquire_native_apt_locks(timeout_seconds: int = 1800) -> list[int]:
        if fcntl is None:
            raise InstallTransactionError("fcntl is required for package recovery")
        deadline = time.monotonic() + timeout_seconds
        while True:
            descriptors: list[int] = []
            try:
                for path in NATIVE_APT_LOCKS:
                    descriptor = InstallTransaction.open_lock(path)
                    try:
                        fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        os.close(descriptor)
                        raise
                    descriptors.append(descriptor)
                return descriptors
            except BlockingIOError:
                for descriptor in reversed(descriptors):
                    fcntl.lockf(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
                if time.monotonic() >= deadline:
                    raise InstallTransactionError(
                        "Timed out waiting for apt/dpkg locks; recovery remains pending"
                    )
                time.sleep(1)
            except Exception:
                for descriptor in reversed(descriptors):
                    fcntl.lockf(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
                raise

    @staticmethod
    def release_native_apt_locks(descriptors: Sequence[int]) -> None:
        if fcntl is None:
            return
        for descriptor in reversed(descriptors):
            fcntl.lockf(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def ensure_package_integrity(self, *, repair: bool = True) -> None:
        audit = self.run(["dpkg", "--audit"], check=False, capture=True)
        dependency_check = self.run(
            ["apt-get", "check"], check=False, capture=True
        )
        audit_output = (audit.stdout or "").strip()
        if audit.returncode or audit_output or dependency_check.returncode:
            if not repair:
                log(
                    "pre-existing package inconsistency observed before installer mutation; "
                    "leaving it untouched"
                )
                return
            log("finishing an interrupted installer package configuration")
            self.run(
                [
                    "dpkg",
                    "--force-confdef",
                    "--force-confold",
                    "--configure",
                    "--pending",
                ],
                check=False,
            )
            self.run(
                [
                    "apt-get",
                    "-y",
                    "--no-remove",
                    "--no-install-recommends",
                    "-o",
                    "Dpkg::Options::=--force-confdef",
                    "-o",
                    "Dpkg::Options::=--force-confold",
                    "-f",
                    "install",
                ],
                check=False,
            )
        audit = self.run(["dpkg", "--audit"], check=False, capture=True)
        dependency_check = self.run(
            ["apt-get", "check"], check=False, capture=True
        )
        if (
            audit.returncode
            or (audit.stdout or "").strip()
            or dependency_check.returncode
        ):
            raise InstallTransactionError(
                "Package state is inconsistent; installer recovery remains pending"
            )

    @contextmanager
    def package_manager_quiesced(
        self, *, repair_packages: bool = True
    ) -> Iterator[None]:
        if not self.production_runtime():
            yield
            return
        if fcntl is None:
            raise InstallTransactionError("fcntl is required for package recovery")
        installer_lock = self.open_lock(INSTALLER_APT_LOCK)
        native_locks: list[int] = []
        try:
            # The installer's `flock ... apt-get` child retains this lock even
            # if its parent shell is SIGKILLed. Never recover beside that child.
            fcntl.flock(installer_lock, fcntl.LOCK_EX)
            self.quiesce_apt_services()
            native_locks = self.acquire_native_apt_locks()
            self.release_native_apt_locks(native_locks)
            native_locks = []
            self.ensure_package_integrity(repair=repair_packages)
            native_locks = self.acquire_native_apt_locks()
            yield
        finally:
            self.release_native_apt_locks(native_locks)
            fcntl.flock(installer_lock, fcntl.LOCK_UN)
            os.close(installer_lock)

    def restore_unit_enablement(self, manifest: dict[str, Any]) -> None:
        if not self.production_runtime():
            return
        self.run(["systemctl", "daemon-reload"])
        states = manifest["unit_states"]
        for unit in MANAGED_UNITS:
            action = "enable" if states[unit]["enabled"] else "disable"
            self.run(["systemctl", action, unit], check=states[unit]["enabled"])
            enabled = self.run(
                ["systemctl", "is-enabled", "--quiet", unit], check=False
            ).returncode == 0
            if enabled != states[unit]["enabled"]:
                raise InstallTransactionError(
                    f"Could not restore {unit} enablement"
                )

    def restore_active_unit_states(self, manifest: dict[str, Any]) -> None:
        if not self.production_runtime():
            return
        states = manifest["unit_states"]
        for unit in (*APPLICATION_UNITS, *APT_TIMER_UNITS):
            if states[unit]["active"]:
                # --no-block is required when recovery itself is ordered
                # Before these units at boot. File restoration has already
                # committed and the active mutation marker is now absent.
                self.run(["systemctl", "--no-block", "start", unit])
            else:
                self.run(["systemctl", "stop", unit], check=False)

    def finalize_healthy_enablement(self, manifest: dict[str, Any]) -> None:
        if not self.production_runtime():
            return
        self.run(["systemctl", "daemon-reload"])
        for unit in APPLICATION_UNITS:
            self.run(["systemctl", "enable", unit])
        states = manifest["unit_states"]
        for unit in APT_TIMER_UNITS:
            action = "enable" if states[unit]["enabled"] else "disable"
            self.run(["systemctl", action, unit], check=states[unit]["enabled"])
            enabled = self.run(
                ["systemctl", "is-enabled", "--quiet", unit], check=False
            ).returncode == 0
            if enabled != states[unit]["enabled"]:
                raise InstallTransactionError(
                    f"Could not preserve {unit} enablement"
                )

    def start_healthy_units(self, manifest: dict[str, Any]) -> None:
        if not self.production_runtime():
            return
        for unit in APPLICATION_UNITS:
            self.run(["systemctl", "--no-block", "start", unit])
        states = manifest["unit_states"]
        for unit in APT_TIMER_UNITS:
            if states[unit]["active"]:
                self.run(["systemctl", "--no-block", "start", unit])
            else:
                self.run(["systemctl", "stop", unit], check=False)

    @staticmethod
    def validate_backup(transaction: Path, entry: dict[str, Any]) -> Path:
        relative = Path(entry.get("backup", ""))
        if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != "files":
            raise InstallTransactionError("Unsafe installer backup path")
        unresolved = transaction / relative
        if unresolved.is_symlink():
            raise InstallTransactionError(f"Installer backup is a symlink: {unresolved}")
        backup = unresolved.resolve(strict=True)
        files_root = (transaction / "files").resolve(strict=True)
        if backup.parent != files_root or not backup.is_file():
            raise InstallTransactionError(f"Unsafe installer backup: {backup}")
        if backup.stat().st_size != entry.get("size") or sha256_file(backup) != entry.get("sha256"):
            raise InstallTransactionError(f"Installer backup integrity failure: {backup}")
        return backup

    @staticmethod
    def prepare_destination(target: Path) -> None:
        parent = target.parent
        details = safe_lstat(parent)
        if details is None or stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise InstallTransactionError(f"Unsafe or missing target directory: {parent}")
        current = safe_lstat(target)
        if current is not None and not (
            stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode)
        ):
            raise InstallTransactionError(f"Refusing to replace non-file target: {target}")

    @staticmethod
    def restore_absent(target: Path) -> None:
        current = safe_lstat(target)
        if current is None:
            return
        if not (stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode)):
            raise InstallTransactionError(f"Refusing to remove non-file target: {target}")
        target.unlink()
        fsync_directory(target.parent)

    @staticmethod
    def restore_file(target: Path, backup: Path, entry: dict[str, Any]) -> None:
        InstallTransaction.prepare_destination(target)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.restore-", dir=target.parent)
        temporary_path = Path(temporary)
        try:
            with backup.open("rb") as source, os.fdopen(descriptor, "wb", closefd=False) as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if hasattr(os, "fchown"):
                os.fchown(descriptor, int(entry["uid"]), int(entry["gid"]))
            os.fchmod(descriptor, int(entry["mode"]))
            os.close(descriptor)
            descriptor = -1
            timestamps = (int(entry["atime_ns"]), int(entry["mtime_ns"]))
            try:
                os.utime(temporary_path, ns=timestamps, follow_symlinks=False)
            except NotImplementedError:  # Windows test hosts lack this safe spelling.
                os.utime(temporary_path, ns=timestamps)
            with temporary_path.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary_path, target)
            fsync_directory(target.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def restore_symlink(target: Path, entry: dict[str, Any]) -> None:
        InstallTransaction.prepare_destination(target)
        temporary = target.parent / (
            f".{target.name}.restore-{os.getpid()}-{secrets.token_hex(8)}"
        )
        created = False
        try:
            os.symlink(entry["link_target"], temporary)
            created = True
            if hasattr(os, "lchown"):
                os.lchown(temporary, int(entry["uid"]), int(entry["gid"]))
            os.replace(temporary, target)
            fsync_directory(target.parent)
        finally:
            if created:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def verify_restored(target: Path, entry: dict[str, Any]) -> None:
        details = safe_lstat(target)
        kind = entry["kind"]
        if kind == "absent":
            if details is not None:
                raise InstallTransactionError(f"Target should be absent after recovery: {target}")
            return
        if details is None:
            raise InstallTransactionError(f"Target is missing after recovery: {target}")
        if kind == "symlink":
            if not stat.S_ISLNK(details.st_mode) or os.readlink(target) != entry["link_target"]:
                raise InstallTransactionError(f"Symlink recovery verification failed: {target}")
            return
        if not stat.S_ISREG(details.st_mode):
            raise InstallTransactionError(f"File recovery verification failed: {target}")
        if (
            details.st_size != entry["size"]
            or stat.S_IMODE(details.st_mode) != entry["mode"]
            or sha256_file(target) != entry["sha256"]
        ):
            raise InstallTransactionError(f"File recovery verification failed: {target}")
        if os.name == "posix" and (
            details.st_uid != entry["uid"] or details.st_gid != entry["gid"]
        ):
            raise InstallTransactionError(f"File ownership recovery failed: {target}")

    def restore(self, transaction: Path, manifest: dict[str, Any]) -> None:
        for entry in manifest["targets"]:
            target = Path(entry["path"])
            kind = entry["kind"]
            if kind == "absent":
                self.restore_absent(target)
            elif kind == "file":
                backup = self.validate_backup(transaction, entry)
                self.restore_file(target, backup, entry)
            else:
                self.restore_symlink(target, entry)
            self.verify_restored(target, entry)

    def clear_active(self, expected: str) -> None:
        if self.active_id() != expected:
            raise InstallTransactionError("Installer active marker changed unexpectedly")
        self.active_file.unlink()
        fsync_directory(self.state_root)

    def publish_pending_services(self, expected: str) -> None:
        current = self.pending_services_id()
        if current is not None and current != expected:
            raise InstallTransactionError(
                "Another installer service-restoration marker already exists"
            )
        if current is None:
            atomic_write(
                self.pending_services_file, f"{expected}\n".encode("ascii")
            )

    def clear_pending_services(self, expected: str) -> None:
        if self.pending_services_id() != expected:
            raise InstallTransactionError(
                "Installer service-restoration marker changed unexpectedly"
            )
        self.pending_services_file.unlink()
        fsync_directory(self.state_root)

    def finish_restored_services(
        self, transaction: Path, manifest: dict[str, Any]
    ) -> None:
        self.publish_pending_services(transaction.name)
        if self.active_id() == transaction.name:
            self.clear_active(transaction.name)
        self.restore_active_unit_states(manifest)
        self.clear_pending_services(transaction.name)
        log("installer runtime service state restored")

    def finish_healthy_services(self, transaction: Path) -> None:
        manifest = self.load_manifest(transaction)
        self.publish_pending_services(transaction.name)
        if self.active_id() == transaction.name:
            self.clear_active(transaction.name)
        self.start_healthy_units(manifest)
        self.clear_pending_services(transaction.name)
        self.prune_nonfatal(keep=3)
        log("healthy installer runtime started")

    def recover(self) -> bool:
        transaction = self.active_dir()
        pending_services = False
        if transaction is None:
            transaction = self.pending_services_dir()
            pending_services = transaction is not None
        if transaction is None:
            log("installer recovery: no unfinished transaction")
            return False
        repair_packages = pending_services or self.state(transaction) != "OBSERVING"
        with self.package_manager_quiesced(repair_packages=repair_packages):
            return self.recover_quiesced(transaction, pending_services)

    def recover_quiesced(
        self, transaction: Path, pending_services: bool
    ) -> bool:
        if pending_services:
            pending_state = self.state(transaction)
            if pending_state not in {"RESTORED", "COMMITTED"}:
                raise InstallTransactionError(
                    "Installer service restoration references an invalid transaction"
                )
            if pending_state == "RESTORED":
                manifest = self.load_manifest(transaction)
                self.finish_restored_services(transaction, manifest)
                return True
            self.load_manifest(transaction)
            self.finish_healthy_services(transaction)
            return False
        state = self.state(transaction)
        if state == "COMMITTED":
            self.load_manifest(transaction)
            self.finish_healthy_services(transaction)
            return False
        if state == "RESTORED":
            manifest = self.load_manifest(transaction)
            self.finish_restored_services(transaction, manifest)
            return True
        if state == "OBSERVING":
            manifest = self.load_manifest(transaction)
            self.write_state(transaction, "RESTORED")
            self.finish_restored_services(transaction, manifest)
            log("untouched installer observation finalized")
            return True
        if state == "HEALTHY":
            self.stop_runtime()
            manifest = self.load_manifest(transaction)
            self.finalize_healthy_enablement(manifest)
            self.write_state(transaction, "COMMITTED")
            self.finish_healthy_services(transaction)
            log("healthy installer transaction finalized after interruption")
            return False
        if state not in RECOVERABLE_STATES:
            raise InstallTransactionError(f"Cannot recover installer transaction from {state}")
        manifest = self.load_manifest(transaction)
        try:
            self.write_state(transaction, "RESTORING")
            self.stop_runtime()
            self.restore(transaction, manifest)
            self.restore_unit_enablement(manifest)
        except Exception:
            try:
                self.write_state(transaction, "RECOVERY_REQUIRED")
            except Exception as state_error:
                log(f"could not persist RECOVERY_REQUIRED: {state_error}")
            raise
        self.write_state(transaction, "RESTORED")
        self.finish_restored_services(transaction, manifest)
        log("installer system files and release pointers restored")
        return True

    def healthy(self) -> None:
        transaction = self.active_dir()
        if transaction is None or self.state(transaction) != "PREPARED":
            raise InstallTransactionError("No prepared installer transaction")
        self.load_manifest(transaction)
        self.write_state(transaction, "HEALTHY")

    def prepare_mutation(self) -> None:
        transaction = self.active_dir()
        if transaction is None or self.state(transaction) != "OBSERVING":
            raise InstallTransactionError("No observing installer transaction")
        self.load_manifest(transaction)
        self.write_state(transaction, "PREPARED")

    def commit(self) -> None:
        transaction = self.active_dir()
        if transaction is None:
            raise InstallTransactionError("No active installer transaction to commit")
        if self.state(transaction) != "HEALTHY":
            raise InstallTransactionError(
                f"Cannot commit installer transaction from {self.state(transaction)}"
            )
        self.stop_runtime()
        manifest = self.load_manifest(transaction)
        self.finalize_healthy_enablement(manifest)
        self.write_state(transaction, "COMMITTED")
        self.finish_healthy_services(transaction)
        log("installer transaction committed")

    def prune_nonfatal(self, keep: int) -> None:
        try:
            self.prune(keep=keep)
        except Exception as error:
            log(f"non-fatal installer snapshot retention warning: {error}")

    def prune(self, keep: int) -> None:
        finished: list[Path] = []
        for child in self.state_root.iterdir():
            if (
                not child.is_dir()
                or child.is_symlink()
                or not TRANSACTION_ID_RE.fullmatch(child.name)
            ):
                continue
            try:
                if self.state(child) in FINAL_STATES:
                    finished.append(child)
            except InstallTransactionError:
                continue
        finished.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for obsolete in finished[keep:]:
            if obsolete.parent != self.state_root:
                raise InstallTransactionError(f"Refusing to prune unsafe path: {obsolete}")
            shutil.rmtree(obsolete)

    def show_status(self) -> None:
        transaction = self.active_dir()
        marker = "active"
        if transaction is None:
            transaction = self.pending_services_dir()
            marker = "services-pending"
        print(
            json.dumps(
                {
                    "active": transaction is not None,
                    "transaction_id": transaction.name if transaction else None,
                    "state": self.state(transaction) if transaction else None,
                    "marker": marker if transaction else None,
                },
                sort_keys=True,
            )
        )


def bool_value(value: str) -> bool:
    if value not in {"0", "1"}:
        raise argparse.ArgumentTypeError("expected 0 or 1")
    return value == "1"


def require(value: Any, name: str) -> Any:
    if value is None:
        raise InstallTransactionError(f"{name} is required")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "command",
        choices=("begin", "prepare", "healthy", "recover", "commit", "status"),
    )
    result.add_argument("--transaction-id")
    result.add_argument("--app-root", type=Path)
    for name in (
        "api-active",
        "api-enabled",
        "worker-active",
        "worker-enabled",
        "timer-active",
        "timer-enabled",
        "apt-daily-active",
        "apt-daily-enabled",
        "apt-upgrade-active",
        "apt-upgrade-enabled",
    ):
        result.add_argument(f"--{name}", type=bool_value)
    result.add_argument(
        "--state-root",
        type=Path,
        default=Path(os.environ.get("BDENCODE_INSTALL_STATE_ROOT", DEFAULT_STATE_ROOT)),
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    transaction = InstallTransaction(args.state_root)
    try:
        transaction.initialize()
        if args.command == "begin":
            if not args.transaction_id or args.app_root is None:
                raise InstallTransactionError("begin requires --transaction-id and --app-root")
            state_values = {
                "bdencode-api.service": {
                    "active": require(args.api_active, "--api-active"),
                    "enabled": require(args.api_enabled, "--api-enabled"),
                },
                "bdencode-worker.service": {
                    "active": require(args.worker_active, "--worker-active"),
                    "enabled": require(args.worker_enabled, "--worker-enabled"),
                },
                "bdencode-update.timer": {
                    "active": require(args.timer_active, "--timer-active"),
                    "enabled": require(args.timer_enabled, "--timer-enabled"),
                },
                "apt-daily.timer": {
                    "active": require(args.apt_daily_active, "--apt-daily-active"),
                    "enabled": require(args.apt_daily_enabled, "--apt-daily-enabled"),
                },
                "apt-daily-upgrade.timer": {
                    "active": require(args.apt_upgrade_active, "--apt-upgrade-active"),
                    "enabled": require(args.apt_upgrade_enabled, "--apt-upgrade-enabled"),
                },
            }
            transaction.begin(args.transaction_id, args.app_root, state_values)
        elif args.command == "prepare":
            transaction.prepare_mutation()
        elif args.command == "healthy":
            transaction.healthy()
        elif args.command == "recover":
            transaction.recover()
        elif args.command == "commit":
            transaction.commit()
        elif args.command == "status":
            transaction.show_status()
        return 0
    except (OSError, InstallTransactionError) as error:
        print(f"bdencode installer transaction error: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
