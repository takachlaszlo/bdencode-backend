"""Fail-closed workspace accounting and quarantine primitives.

The HTTP layer deliberately uses these helpers outside long-running SQLite
write transactions.  A destructive operation first moves its exact target to
an owned quarantine directory on the same filesystem.  Recursive deletion can
then be retried after a process crash without exposing source or completed
media to a broad ``rmtree`` target.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import time
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence
from uuid import uuid4

if TYPE_CHECKING:
    from .config import Settings
    from .db import Database


WORKSPACE_CATEGORIES = (
    "work",
    "logs",
    "analysis",
    "comparison",
    "stages",
)


class MaintenanceSafetyError(RuntimeError):
    """A maintenance target cannot be proven to be locally owned and safe."""


class MaintenanceLeaseBusyError(MaintenanceSafetyError):
    """Another live process owns the durable maintenance operation lease."""


class MaintenancePhase(StrEnum):
    INTENT = "INTENT"
    DETACHED = "DETACHED"
    COMMITTED = "COMMITTED"
    FINALIZED = "FINALIZED"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass(frozen=True, slots=True)
class StorageCategory:
    name: str
    bytes: int
    file_count: int
    reclaimable: bool
    present: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class JobStorageReport:
    workspace_bytes: int
    reclaimable_bytes: int
    completed_release_bytes: int
    categories: tuple[StorageCategory, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_bytes": self.workspace_bytes,
            "reclaimable_bytes": self.reclaimable_bytes,
            "completed_release_bytes": self.completed_release_bytes,
            "categories": [item.to_dict() for item in self.categories],
        }


@dataclass(frozen=True, slots=True)
class QuarantineReceipt:
    operation_id: str
    original_path: Path
    quarantine_path: Path
    bytes_moved: int
    file_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "original_path": str(self.original_path),
            "quarantine_path": str(self.quarantine_path),
            "bytes_moved": self.bytes_moved,
            "file_count": self.file_count,
        }


@dataclass(frozen=True, slots=True)
class MaintenanceTargetSpec:
    """One exact, existing child that an operation intends to remove."""

    target: Path
    root: Path
    label: str = "maintenance target"


@dataclass(frozen=True, slots=True)
class MaintenanceDomainGuard:
    """Database snapshot that must still hold when destructive intent commits."""

    job_id: str | None = None
    expected_job_version: int | None = None
    allowed_job_states: tuple[str, ...] = ()
    expected_preparation_versions: Mapping[str, int] | None = None
    preparation_id: str | None = None
    expected_preparation_version: int | None = None
    allowed_preparation_states: tuple[str, ...] = ()
    forbid_active_preparations: bool = False


@dataclass(frozen=True, slots=True)
class MaintenanceOperation:
    id: str
    kind: str
    subject_id: str
    phase: MaintenancePhase
    targets: tuple[dict[str, Any], ...]
    created_at: str
    updated_at: str


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return (
        path.is_symlink()
        or bool(callable(is_junction) and is_junction())
        or bool(reparse_flag and attributes & reparse_flag)
    )


def _require_real_directory(path: Path, *, description: str) -> Path:
    if not os.path.lexists(path):
        raise MaintenanceSafetyError(f"{description} does not exist")
    if _is_link_or_junction(path) or not path.is_dir():
        raise MaintenanceSafetyError(
            f"{description} must be a real directory, not a link or junction"
        )
    return path.resolve(strict=True)


def _require_direct_child(
    path: Path,
    root: Path,
    *,
    description: str,
    must_exist: bool = True,
) -> Path:
    resolved_root = _require_real_directory(root, description=f"{description} root")
    if must_exist:
        if not os.path.lexists(path):
            raise MaintenanceSafetyError(f"{description} does not exist")
        if _is_link_or_junction(path):
            raise MaintenanceSafetyError(
                f"{description} cannot be a symbolic link or junction"
            )
        resolved = path.resolve(strict=True)
        if resolved.lstat().st_dev != resolved_root.stat().st_dev:
            raise MaintenanceSafetyError(f"{description} crosses a filesystem boundary")
    else:
        if os.path.lexists(path):
            raise MaintenanceSafetyError(f"{description} already exists")
        parent = path.parent.resolve(strict=True)
        resolved = parent / path.name
    if resolved.parent != resolved_root or not resolved.name:
        raise MaintenanceSafetyError(f"{description} escaped its configured root")
    return resolved


def _parse_mountinfo(document: str) -> frozenset[str]:
    points: set[str] = set()
    escapes = {
        "\\040": " ",
        "\\011": "\t",
        "\\012": "\n",
        "\\134": "\\",
    }
    for line in document.splitlines():
        fields = line.split(" ")
        if len(fields) < 6 or "-" not in fields:
            raise MaintenanceSafetyError("Linux mount boundary data is malformed")
        value = fields[4]
        for encoded, decoded in escapes.items():
            value = value.replace(encoded, decoded)
        points.add(os.path.normcase(os.path.abspath(value)))
    return frozenset(points)


def _linux_mount_points() -> frozenset[str]:
    if os.name != "posix":
        return frozenset()
    try:
        document = Path("/proc/self/mountinfo").read_text(
            encoding="utf-8", errors="strict"
        )
    except (OSError, UnicodeError):
        # Linux exposes mountinfo for every normal process. If it disappears,
        # destructive traversal cannot prove that same-device bind mounts are
        # absent and therefore fails closed.
        if Path("/proc").exists():
            raise MaintenanceSafetyError("Linux mount boundaries are unavailable")
        return frozenset()
    return _parse_mountinfo(document)


def _scandir_entry_stat(entry: os.DirEntry[str]) -> os.stat_result:
    # Windows' DirEntry.stat currently reports st_dev=0 even when Path.lstat
    # carries the volume identity. Use the exact non-following path call on all
    # platforms so the cross-device invariant is portable.
    return Path(entry.path).lstat()


def _sync_directory(path: Path) -> None:
    """Persist a POSIX directory entry update before advancing the journal.

    Windows does not expose a supported directory ``fsync`` equivalent.  Its
    rename path below uses ``MoveFileExW(MOVEFILE_WRITE_THROUGH)`` instead;
    creating an empty ``.trash`` before the intent is harmless if lost.
    """

    if os.name == "nt":
        return
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise MaintenanceSafetyError(
            f"maintenance directory could not be durably synchronized: {path.name}"
        ) from exc


def _durable_replace_posix(source: Path, destination: Path) -> None:
    os.replace(source, destination)
    _sync_directory(source.parent)
    if destination.parent != source.parent:
        _sync_directory(destination.parent)


def _durable_replace_windows(
    source: Path, destination: Path, kernel32: Any | None = None
) -> None:
    import ctypes
    from ctypes import wintypes

    api = kernel32
    if api is None:
        api = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = api.MoveFileExW
    move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_file.restype = wintypes.BOOL
    # REPLACE_EXISTING is defensive; the caller has already required the exact
    # destination not to exist. WRITE_THROUGH waits for the move to reach disk.
    if not move_file(str(source), str(destination), 0x1 | 0x8):
        raise ctypes.WinError(ctypes.get_last_error())


def _durable_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        _durable_replace_windows(source, destination)
    else:
        _durable_replace_posix(source, destination)


def _tree_usage(path: Path) -> tuple[int, int]:
    """Count bytes without following any link or directory junction."""

    if not os.path.lexists(path):
        return 0, 0
    if _is_link_or_junction(path):
        raise MaintenanceSafetyError(f"storage target is an unsafe link: {path.name}")
    mount_points = _linux_mount_points()
    root_key = os.path.normcase(os.path.abspath(path))
    if root_key in mount_points:
        raise MaintenanceSafetyError("storage target is a mounted filesystem")
    if path.is_file():
        stat = path.stat()
        return stat.st_size, 1
    if not path.is_dir():
        raise MaintenanceSafetyError(
            "storage target is not a regular file or directory"
        )

    root_device = int(path.lstat().st_dev)
    total = 0
    files = 0
    pending = [path]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                candidate = Path(entry.path)
                if entry.is_symlink() or _is_link_or_junction(candidate):
                    raise MaintenanceSafetyError(
                        f"storage tree contains an unsafe link: {candidate.name}"
                    )
                entry_stat = _scandir_entry_stat(entry)
                if int(entry_stat.st_dev) != root_device:
                    raise MaintenanceSafetyError(
                        f"storage tree crosses a filesystem boundary: {candidate.name}"
                    )
                if os.path.normcase(os.path.abspath(candidate)) in mount_points:
                    raise MaintenanceSafetyError(
                        f"storage tree contains a mounted path: {candidate.name}"
                    )
                if entry.is_dir(follow_symlinks=False):
                    pending.append(candidate)
                elif entry.is_file(follow_symlinks=False):
                    total += entry_stat.st_size
                    files += 1
                else:
                    raise MaintenanceSafetyError(
                        f"storage tree contains an unsupported filesystem entry: "
                        f"{candidate.name}"
                    )
    return total, files


def safe_tree_usage(path: Path) -> tuple[int, int]:
    """Validate a removal tree and return its byte/file accounting."""

    return _tree_usage(path)


def inspect_job_storage(
    job_root: Path,
    *,
    jobs_root: Path,
    completed_release: Path | None = None,
    completed_root: Path | None = None,
) -> JobStorageReport:
    if os.path.lexists(job_root):
        resolved_job = _require_direct_child(
            job_root, jobs_root, description="job workspace"
        )
    else:
        resolved_jobs = _require_real_directory(jobs_root, description="jobs root")
        if job_root.parent.resolve(strict=True) != resolved_jobs or not job_root.name:
            raise MaintenanceSafetyError("job workspace escaped its configured root")
        resolved_job = resolved_jobs / job_root.name
    categories: list[StorageCategory] = []
    workspace_bytes = 0
    reclaimable_bytes = 0
    for name in WORKSPACE_CATEGORIES:
        target = resolved_job / name
        size, count = _tree_usage(target)
        reclaimable = name == "work"
        workspace_bytes += size
        if reclaimable:
            reclaimable_bytes += size
        categories.append(
            StorageCategory(
                name=name,
                bytes=size,
                file_count=count,
                reclaimable=reclaimable,
                present=os.path.lexists(target),
            )
        )

    completed_bytes = 0
    if completed_release is not None:
        if completed_root is None:
            raise ValueError("completed_root is required with completed_release")
        resolved_release = _require_direct_child(
            completed_release,
            completed_root,
            description="completed release",
        )
        completed_bytes, _ = _tree_usage(resolved_release)

    return JobStorageReport(
        workspace_bytes=workspace_bytes,
        reclaimable_bytes=reclaimable_bytes,
        completed_release_bytes=completed_bytes,
        categories=tuple(categories),
    )


def _ensure_quarantine_root(root: Path) -> Path:
    resolved_root = _require_real_directory(root, description="maintenance root")
    candidate = resolved_root / ".trash"
    if os.path.lexists(candidate):
        if _is_link_or_junction(candidate) or not candidate.is_dir():
            raise MaintenanceSafetyError(
                "maintenance quarantine must be a real directory"
            )
    else:
        candidate.mkdir(mode=0o700)
        _sync_directory(resolved_root)
        _sync_directory(candidate)
    resolved = candidate.resolve(strict=True)
    if resolved.parent != resolved_root or resolved.name != ".trash":
        raise MaintenanceSafetyError("maintenance quarantine escaped its root")
    return resolved


def quarantine_direct_child(
    target: Path,
    *,
    root: Path,
    operation_id: str | None = None,
    label: str = "workspace",
) -> QuarantineReceipt:
    """Atomically detach one exact direct child into ``root/.trash``."""

    resolved = _require_direct_child(target, root, description=label)
    size, count = _tree_usage(resolved)
    identifier = operation_id or uuid4().hex
    if not identifier or any(
        char not in "abcdefghijklmnopqrstuvwxyz0123456789-"
        for char in identifier.lower()
    ):
        raise ValueError("operation_id must contain only letters, digits, and hyphens")
    quarantine = _ensure_quarantine_root(root)
    destination = quarantine / f"{resolved.name}-{identifier}"
    _require_direct_child(
        destination,
        quarantine,
        description="quarantine destination",
        must_exist=False,
    )
    _durable_replace(resolved, destination)
    return QuarantineReceipt(
        operation_id=identifier,
        original_path=resolved,
        quarantine_path=destination.resolve(strict=True),
        bytes_moved=size,
        file_count=count,
    )


def quarantine_temporary_work(
    job_root: Path,
    *,
    jobs_root: Path,
    operation_id: str | None = None,
) -> QuarantineReceipt | None:
    resolved_job = _require_direct_child(
        job_root, jobs_root, description="job workspace"
    )
    work = resolved_job / "work"
    if not os.path.lexists(work):
        return None
    return quarantine_direct_child(
        work,
        root=resolved_job,
        operation_id=operation_id,
        label="temporary work directory",
    )


def delete_quarantined(receipt: QuarantineReceipt, *, root: Path) -> None:
    """Permanently remove exactly the quarantined path recorded by a receipt."""

    quarantine = _ensure_quarantine_root(root)
    resolved = _require_direct_child(
        receipt.quarantine_path,
        quarantine,
        description="quarantined maintenance target",
    )
    if resolved != receipt.quarantine_path.resolve(strict=True):
        raise MaintenanceSafetyError(
            "quarantine receipt no longer identifies its target"
        )
    size, count = _tree_usage(resolved)
    if (size, count) != (receipt.bytes_moved, receipt.file_count):
        raise MaintenanceSafetyError(
            "quarantined maintenance contents changed before removal"
        )
    if resolved.is_dir():
        shutil.rmtree(resolved)
    elif resolved.is_file():
        resolved.unlink()
    else:
        raise MaintenanceSafetyError("quarantined target has an unsupported type")
    _sync_directory(quarantine)


def restore_quarantined(receipt: QuarantineReceipt, *, root: Path) -> None:
    """Restore a detached target when its matching database mutation lost a race."""

    quarantine = _ensure_quarantine_root(root)
    resolved = _require_direct_child(
        receipt.quarantine_path,
        quarantine,
        description="quarantined maintenance target",
    )
    original_root = _require_real_directory(
        receipt.original_path.parent,
        description="original maintenance root",
    )
    destination = original_root / receipt.original_path.name
    _require_direct_child(
        destination,
        original_root,
        description="restored maintenance target",
        must_exist=False,
    )
    _durable_replace(resolved, destination)


def list_quarantine(root: Path) -> tuple[Path, ...]:
    quarantine = _ensure_quarantine_root(root)
    entries: list[Path] = []
    for candidate in quarantine.iterdir():
        if _is_link_or_junction(candidate):
            raise MaintenanceSafetyError("quarantine contains an unsafe link")
        resolved = candidate.resolve(strict=True)
        if resolved.parent != quarantine:
            raise MaintenanceSafetyError("quarantine entry escaped its root")
        entries.append(resolved)
    return tuple(sorted(entries))


class MaintenanceJournal:
    """Crash-safe two-phase filesystem detach coordinator.

    The intent and deterministic quarantine destinations are committed before
    the first rename.  A domain transaction then changes ``DETACHED`` to
    ``COMMITTED`` atomically with its own mutation/event.  Recovery can
    therefore restore every uncommitted detach and reap every committed one,
    including a crash between ``os.replace`` and receipt persistence.
    """

    _TERMINAL_PHASES = frozenset(
        {MaintenancePhase.FINALIZED, MaintenancePhase.ROLLED_BACK}
    )

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        lease_seconds: float = 300.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("maintenance lease_seconds must be positive")
        self.database = database
        self.settings = settings
        self.lease_seconds = float(lease_seconds)
        self.owner = uuid4().hex
        self.pid = os.getpid()
        self.host = socket.gethostname()
        self.process_token = self._process_start_token(self.pid) or self.owner

    @staticmethod
    def _now_text() -> str:
        return datetime.now(UTC).isoformat(timespec="microseconds")

    @staticmethod
    def _dump_targets(targets: Sequence[dict[str, Any]]) -> str:
        return json.dumps(
            targets,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(path))

    @staticmethod
    def _paths_overlap(left: str, right: str) -> bool:
        try:
            common = os.path.normcase(os.path.commonpath((left, right)))
        except ValueError:
            return False
        return common in {left, right}

    def _trusted_roots(self) -> tuple[Path, Path, Path, Path]:
        data = _require_real_directory(
            self.settings.data_root, description="maintenance data root"
        )
        configured = (
            (self.settings.jobs_root, "jobs"),
            (self.settings.completed_root, "completed"),
            (self.settings.release_kits_root, "release-kits"),
        )
        resolved: list[Path] = []
        for candidate, expected_name in configured:
            root = _require_real_directory(
                candidate, description=f"maintenance {expected_name} root"
            )
            if root.parent != data or root.name != expected_name:
                raise MaintenanceSafetyError(
                    f"maintenance {expected_name} root escaped data root"
                )
            resolved.append(root)
        return data, resolved[0], resolved[1], resolved[2]

    def _authorize_root(self, root: Path) -> Path:
        _data, jobs, completed, release_kits = self._trusted_roots()
        resolved = _require_real_directory(root, description="maintenance target root")
        if resolved in {jobs, completed, release_kits}:
            return resolved
        if resolved.parent == jobs and resolved.name not in {"", ".trash"}:
            return resolved
        raise MaintenanceSafetyError(
            "maintenance target root is outside configured owned roots"
        )

    @staticmethod
    def _identity(path: Path) -> dict[str, int]:
        stat = path.lstat()
        return {
            "device": int(stat.st_dev),
            "inode": int(stat.st_ino),
            "mode": int(stat.st_mode),
        }

    @classmethod
    def _require_identity(cls, path: Path, expected: object) -> None:
        if not isinstance(expected, dict) or any(
            type(expected.get(name)) is not int for name in ("device", "inode", "mode")
        ):
            raise MaintenanceSafetyError("maintenance target identity is invalid")
        if cls._identity(path) != expected:
            raise MaintenanceSafetyError(
                "maintenance target was replaced after intent persistence"
            )

    def _target_paths(self, target: dict[str, Any]) -> tuple[Path, Path, Path]:
        try:
            root_value = target["root"]
            original_value = target["original_path"]
            quarantine_value = target["quarantine_path"]
        except KeyError as exc:
            raise MaintenanceSafetyError(
                "maintenance target journal is incomplete"
            ) from exc
        if not all(
            isinstance(value, str) and value
            for value in (root_value, original_value, quarantine_value)
        ):
            raise MaintenanceSafetyError("maintenance target paths are invalid")
        root = self._authorize_root(Path(root_value))
        original = root / Path(original_value).name
        if not self._same_path(original, Path(original_value)):
            raise MaintenanceSafetyError("maintenance original path escaped its root")
        if target.get("original_path_key") != self._path_key(original):
            raise MaintenanceSafetyError(
                "maintenance target canonical binding does not match"
            )
        quarantine = _ensure_quarantine_root(root)
        destination = quarantine / Path(quarantine_value).name
        if not self._same_path(destination, Path(quarantine_value)):
            raise MaintenanceSafetyError("maintenance quarantine path escaped its root")
        return root, original, destination

    def _decode_operation(self, row: Any) -> MaintenanceOperation:
        try:
            raw_targets = json.loads(row["targets_json"])
            phase = MaintenancePhase(row["phase"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MaintenanceSafetyError(
                "maintenance operation journal is invalid"
            ) from exc
        if not isinstance(raw_targets, list) or any(
            not isinstance(target, dict) for target in raw_targets
        ):
            raise MaintenanceSafetyError("maintenance operation targets are invalid")
        return MaintenanceOperation(
            id=str(row["id"]),
            kind=str(row["kind"]),
            subject_id=str(row["subject_id"]),
            phase=phase,
            targets=tuple(dict(target) for target in raw_targets),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def operation(self, operation_id: str) -> MaintenanceOperation:
        with self.database._read() as connection:
            row = connection.execute(
                "SELECT * FROM maintenance_operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise MaintenanceSafetyError(
                f"maintenance operation not found: {operation_id}"
            )
        return self._decode_operation(row)

    @staticmethod
    def _windows_process_start_token(
        pid: int, kernel32: Any | None = None
    ) -> str | None:
        import ctypes
        from ctypes import wintypes

        api = kernel32
        if api is None:
            api = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = api.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = api.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        get_process_times = api.GetProcessTimes
        get_process_times.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        get_process_times.restype = wintypes.BOOL
        close_handle = api.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        process = open_process(0x1000, False, pid)
        if not process:
            return None
        try:
            exit_code = wintypes.DWORD()
            if (
                not get_exit_code(process, ctypes.byref(exit_code))
                or exit_code.value != 259
            ):
                return None
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not get_process_times(
                process,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            value = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            return f"windows-filetime:{value}"
        finally:
            close_handle(process)

    @classmethod
    def _process_start_token(cls, pid: int) -> str | None:
        if pid <= 0:
            return None
        if os.name == "nt":
            try:
                return cls._windows_process_start_token(pid)
            except (AttributeError, OSError):
                return None
        stat_path = Path(f"/proc/{pid}/stat")
        try:
            value = stat_path.read_text(encoding="ascii")
            suffix = value[value.rfind(")") + 2 :].split()
            # /proc stat fields after ``comm`` begin with field 3.  Process
            # starttime is field 22, hence offset 19 in this suffix.
            return f"proc-start-ticks:{suffix[19]}"
        except (IndexError, OSError, UnicodeError):
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            return f"alive-unknown-start:{pid}"
        return f"alive-unknown-start:{pid}"

    def _claim(self, operation_id: str) -> MaintenanceOperation:
        now = time.time()
        with self.database._write() as connection:
            row = connection.execute(
                "SELECT * FROM maintenance_operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise MaintenanceSafetyError(
                    f"maintenance operation not found: {operation_id}"
                )
            current_owner = str(row["lease_owner"])
            same_host = str(row["lease_host"]) == self.host
            live_token = (
                self._process_start_token(int(row["lease_pid"])) if same_host else None
            )
            owner_alive = (
                same_host
                and live_token is not None
                and live_token == str(row["lease_process_token"])
            )
            expired = float(row["lease_expires_at"]) <= now
            if current_owner != self.owner and owner_alive:
                raise MaintenanceLeaseBusyError(
                    "maintenance operation is leased by a live process"
                )
            if current_owner != self.owner and not same_host and not expired:
                raise MaintenanceLeaseBusyError(
                    "maintenance operation has an unexpired remote lease"
                )
            connection.execute(
                "UPDATE maintenance_operations SET lease_owner = ?, lease_pid = ?, "
                "lease_host = ?, lease_process_token = ?, lease_expires_at = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    self.owner,
                    self.pid,
                    self.host,
                    self.process_token,
                    now + self.lease_seconds,
                    self._now_text(),
                    operation_id,
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM maintenance_operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
        assert claimed is not None
        return self._decode_operation(claimed)

    def begin(
        self,
        kind: str,
        subject_id: str,
        targets: Sequence[MaintenanceTargetSpec],
        *,
        guard: MaintenanceDomainGuard | None = None,
    ) -> MaintenanceOperation:
        if (
            not kind
            or not subject_id
            or len(kind) > 80
            or len(subject_id) > 256
            or any(ord(char) < 0x20 for char in kind + subject_id)
        ):
            raise ValueError("invalid maintenance operation binding")
        if not targets or len(targets) > 1024:
            raise ValueError("maintenance operation must have 1 to 1024 targets")
        operation_id = uuid4().hex
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, spec in enumerate(targets):
            root = self._authorize_root(spec.root)
            original = _require_direct_child(
                spec.target,
                root,
                description=spec.label,
            )
            if original.name == ".trash":
                raise MaintenanceSafetyError(
                    "maintenance quarantine root cannot be a target"
                )
            key = os.path.normcase(str(original))
            if key in seen:
                raise MaintenanceSafetyError("duplicate maintenance target")
            seen.add(key)
            size, count = _tree_usage(original)
            quarantine_root = _ensure_quarantine_root(root)
            destination = quarantine_root / f"{original.name}-{operation_id}-{index}"
            _require_direct_child(
                destination,
                quarantine_root,
                description="journal quarantine destination",
                must_exist=False,
            )
            records.append(
                {
                    "index": index,
                    "label": spec.label,
                    "root": str(root),
                    "original_path": str(original),
                    "original_path_key": self._path_key(original),
                    "quarantine_path": str(destination),
                    "bytes_moved": size,
                    "file_count": count,
                    "identity": self._identity(original),
                    "state": "PLANNED",
                }
            )

        now_text = self._now_text()
        now_epoch = time.time()
        with self.database._write() as connection:
            if guard is not None:
                self._validate_domain_guard(connection, guard)
            unfinished = connection.execute(
                "SELECT targets_json FROM maintenance_operations "
                "WHERE phase NOT IN ('FINALIZED', 'ROLLED_BACK')"
            ).fetchall()
            intended_paths = {str(record["original_path_key"]) for record in records}
            for row in unfinished:
                try:
                    existing = json.loads(row["targets_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise MaintenanceSafetyError(
                        "existing maintenance journal is invalid"
                    ) from exc
                if not isinstance(existing, list):
                    raise MaintenanceSafetyError(
                        "existing maintenance journal targets are invalid"
                    )
                existing_paths = {
                    item.get("original_path_key")
                    for item in existing
                    if isinstance(item, dict)
                    and isinstance(item.get("original_path_key"), str)
                }
                if any(
                    self._paths_overlap(intended, current)
                    for intended in intended_paths
                    for current in existing_paths
                ):
                    raise MaintenanceLeaseBusyError(
                        "maintenance target hierarchy already has an unfinished "
                        "operation"
                    )
            connection.execute(
                "INSERT INTO maintenance_operations("
                "id, kind, subject_id, phase, targets_json, lease_owner, "
                "lease_pid, lease_host, lease_process_token, lease_expires_at, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, 'INTENT', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operation_id,
                    kind,
                    subject_id,
                    self._dump_targets(records),
                    self.owner,
                    self.pid,
                    self.host,
                    self.process_token,
                    now_epoch + self.lease_seconds,
                    now_text,
                    now_text,
                ),
            )
            try:
                connection.executemany(
                    "INSERT INTO maintenance_target_claims("
                    "original_path_key, operation_id) VALUES (?, ?)",
                    ((record["original_path_key"], operation_id) for record in records),
                )
            except sqlite3.IntegrityError as exc:
                raise MaintenanceLeaseBusyError(
                    "maintenance target already has an unfinished operation"
                ) from exc
        return self.operation(operation_id)

    @staticmethod
    def _validate_domain_guard(
        connection: sqlite3.Connection,
        guard: MaintenanceDomainGuard,
    ) -> None:
        active_states = {
            "PREPARING",
            "SEEDING_CHECK",
            "SEEDING",
            "PUBLISHING",
        }
        if guard.job_id is not None:
            job = connection.execute(
                "SELECT state, version FROM jobs WHERE id = ?",
                (guard.job_id,),
            ).fetchone()
            if job is None:
                raise MaintenanceSafetyError("guarded maintenance job disappeared")
            if (
                guard.expected_job_version is not None
                and int(job["version"]) != guard.expected_job_version
            ):
                raise MaintenanceLeaseBusyError(
                    "guarded maintenance job changed before intent"
                )
            if (
                guard.allowed_job_states
                and str(job["state"]) not in guard.allowed_job_states
            ):
                raise MaintenanceLeaseBusyError(
                    "guarded maintenance job state changed before intent"
                )
        if guard.preparation_id is not None:
            preparation = connection.execute(
                "SELECT job_id, state, version FROM release_preparations WHERE id = ?",
                (guard.preparation_id,),
            ).fetchone()
            if preparation is None:
                raise MaintenanceSafetyError("guarded release preparation disappeared")
            if guard.job_id is not None and str(preparation["job_id"]) != guard.job_id:
                raise MaintenanceSafetyError(
                    "guarded release preparation changed ownership"
                )
            if (
                guard.expected_preparation_version is not None
                and int(preparation["version"]) != guard.expected_preparation_version
            ):
                raise MaintenanceLeaseBusyError(
                    "guarded release preparation changed before intent"
                )
            if (
                guard.allowed_preparation_states
                and str(preparation["state"]) not in guard.allowed_preparation_states
            ):
                raise MaintenanceLeaseBusyError(
                    "guarded release preparation state changed before intent"
                )
        if guard.expected_preparation_versions is not None:
            if guard.job_id is None:
                raise ValueError("exact preparation snapshot guard requires a job_id")
            expected = dict(guard.expected_preparation_versions)
            if any(
                not isinstance(identifier, str)
                or type(version) is not int
                or version < 1
                for identifier, version in expected.items()
            ):
                raise ValueError("invalid guarded preparation version snapshot")
            rows = connection.execute(
                "SELECT id, state, version FROM release_preparations WHERE job_id = ?",
                (guard.job_id,),
            ).fetchall()
            actual = {str(row["id"]): int(row["version"]) for row in rows}
            if actual != expected:
                raise MaintenanceLeaseBusyError(
                    "release preparation snapshot changed before destructive intent"
                )
            if guard.forbid_active_preparations and any(
                str(row["state"]) in active_states for row in rows
            ):
                raise MaintenanceLeaseBusyError(
                    "active release preparation blocks destructive intent"
                )

    def _save_targets(
        self,
        operation_id: str,
        targets: Sequence[dict[str, Any]],
        *,
        phase: MaintenancePhase | None = None,
    ) -> None:
        assignments = [
            "targets_json = ?",
            "lease_expires_at = ?",
            "updated_at = ?",
        ]
        parameters: list[Any] = [
            self._dump_targets(targets),
            time.time() + self.lease_seconds,
            self._now_text(),
        ]
        if phase is not None:
            assignments.append("phase = ?")
            parameters.append(phase.value)
        parameters.extend((operation_id, self.owner))
        with self.database._write() as connection:
            cursor = connection.execute(
                f"UPDATE maintenance_operations SET {', '.join(assignments)} "
                "WHERE id = ? AND lease_owner = ?",
                parameters,
            )
            if cursor.rowcount != 1:
                raise MaintenanceLeaseBusyError(
                    "maintenance operation lease changed concurrently"
                )
            if phase in self._TERMINAL_PHASES:
                connection.execute(
                    "DELETE FROM maintenance_target_claims WHERE operation_id = ?",
                    (operation_id,),
                )

    def stage(self, operation_id: str) -> tuple[QuarantineReceipt, ...]:
        operation = self._claim(operation_id)
        if operation.phase is MaintenancePhase.DETACHED:
            return self._receipts(operation)
        if operation.phase is not MaintenancePhase.INTENT:
            raise MaintenanceSafetyError(
                f"cannot detach maintenance operation in {operation.phase.value}"
            )
        targets = [dict(target) for target in operation.targets]
        for target in targets:
            root, original, destination = self._target_paths(target)
            original_exists = os.path.lexists(original)
            destination_exists = os.path.lexists(destination)
            if original_exists and destination_exists:
                raise MaintenanceSafetyError(
                    "maintenance target exists in both original and quarantine"
                )
            if original_exists:
                resolved = _require_direct_child(
                    original,
                    root,
                    description=str(target.get("label", "maintenance target")),
                )
                self._require_identity(resolved, target.get("identity"))
                size, count = _tree_usage(resolved)
                if (size, count) != (
                    target.get("bytes_moved"),
                    target.get("file_count"),
                ):
                    raise MaintenanceSafetyError(
                        "maintenance target contents changed after intent persistence"
                    )
                _durable_replace(resolved, destination)
            elif not destination_exists:
                raise MaintenanceSafetyError(
                    "maintenance target disappeared outside its journal"
                )
            quarantined = _require_direct_child(
                destination,
                destination.parent,
                description="journaled quarantine target",
            )
            self._require_identity(quarantined, target.get("identity"))
            size, count = _tree_usage(quarantined)
            if (size, count) != (
                target.get("bytes_moved"),
                target.get("file_count"),
            ):
                raise MaintenanceSafetyError(
                    "quarantined maintenance contents changed after detach"
                )
            target["state"] = "DETACHED"
            self._save_targets(operation_id, targets)
        self._save_targets(
            operation_id,
            targets,
            phase=MaintenancePhase.DETACHED,
        )
        return self._receipts(self.operation(operation_id))

    def _receipts(
        self, operation: MaintenanceOperation
    ) -> tuple[QuarantineReceipt, ...]:
        receipts: list[QuarantineReceipt] = []
        for target in operation.targets:
            if target.get("state") != "DETACHED":
                raise MaintenanceSafetyError(
                    "maintenance target receipt is not detached"
                )
            receipts.append(
                QuarantineReceipt(
                    operation_id=operation.id,
                    original_path=Path(str(target["original_path"])),
                    quarantine_path=Path(str(target["quarantine_path"])),
                    bytes_moved=int(target["bytes_moved"]),
                    file_count=int(target["file_count"]),
                )
            )
        return tuple(receipts)

    def rollback(self, operation_id: str) -> MaintenanceOperation:
        operation = self._claim(operation_id)
        if operation.phase is MaintenancePhase.ROLLED_BACK:
            return operation
        if operation.phase is MaintenancePhase.FINALIZED:
            raise MaintenanceSafetyError("finalized maintenance cannot be restored")
        if operation.phase is MaintenancePhase.COMMITTED:
            raise MaintenanceSafetyError("committed maintenance cannot be restored")
        targets = [dict(target) for target in operation.targets]
        for target in reversed(targets):
            root, original, destination = self._target_paths(target)
            original_exists = os.path.lexists(original)
            destination_exists = os.path.lexists(destination)
            if original_exists and destination_exists:
                raise MaintenanceSafetyError(
                    "maintenance target exists in both original and quarantine"
                )
            if destination_exists:
                quarantined = _require_direct_child(
                    destination,
                    destination.parent,
                    description="journaled quarantine target",
                )
                self._require_identity(quarantined, target.get("identity"))
                size, count = _tree_usage(quarantined)
                if (size, count) != (
                    target.get("bytes_moved"),
                    target.get("file_count"),
                ):
                    raise MaintenanceSafetyError(
                        "quarantined maintenance contents changed before restore"
                    )
                _require_direct_child(
                    original,
                    root,
                    description="restored journal target",
                    must_exist=False,
                )
                _durable_replace(quarantined, original)
            elif not original_exists:
                raise MaintenanceSafetyError(
                    "uncommitted maintenance target disappeared"
                )
            restored = _require_direct_child(
                original,
                root,
                description="restored journal target",
            )
            self._require_identity(restored, target.get("identity"))
            target["state"] = "RESTORED"
            self._save_targets(operation_id, targets)
        self._save_targets(
            operation_id,
            targets,
            phase=MaintenancePhase.ROLLED_BACK,
        )
        return self.operation(operation_id)

    def finalize(self, operation_id: str) -> MaintenanceOperation:
        operation = self._claim(operation_id)
        if operation.phase is MaintenancePhase.FINALIZED:
            return operation
        if operation.phase is not MaintenancePhase.COMMITTED:
            raise MaintenanceSafetyError("only committed maintenance can be finalized")
        targets = [dict(target) for target in operation.targets]
        for target in targets:
            root, original, destination = self._target_paths(target)
            original_exists = os.path.lexists(original)
            destination_exists = os.path.lexists(destination)
            if original_exists:
                raise MaintenanceSafetyError(
                    "committed maintenance target unexpectedly exists at origin"
                )
            if destination_exists:
                quarantined = _require_direct_child(
                    destination,
                    destination.parent,
                    description="journaled quarantine target",
                )
                self._require_identity(quarantined, target.get("identity"))
                size, count = _tree_usage(quarantined)
                if (size, count) != (
                    target.get("bytes_moved"),
                    target.get("file_count"),
                ):
                    raise MaintenanceSafetyError(
                        "quarantined maintenance contents changed before deletion"
                    )
                receipt = QuarantineReceipt(
                    operation_id=operation.id,
                    original_path=original,
                    quarantine_path=quarantined,
                    bytes_moved=int(target["bytes_moved"]),
                    file_count=int(target["file_count"]),
                )
                delete_quarantined(receipt, root=root)
            target["state"] = "DELETED"
            self._save_targets(operation_id, targets)
        self._save_targets(
            operation_id,
            targets,
            phase=MaintenancePhase.FINALIZED,
        )
        return self.operation(operation_id)

    def recover(self) -> tuple[MaintenanceOperation, ...]:
        """Reconcile every abandoned intent; live process leases are skipped."""

        with self.database._read() as connection:
            rows = connection.execute(
                "SELECT id FROM maintenance_operations "
                "WHERE phase NOT IN ('FINALIZED', 'ROLLED_BACK') "
                "ORDER BY created_at, id"
            ).fetchall()
        recovered: list[MaintenanceOperation] = []
        for row in rows:
            operation_id = str(row["id"])
            try:
                operation = self._claim(operation_id)
            except MaintenanceLeaseBusyError:
                continue
            if operation.phase is MaintenancePhase.COMMITTED:
                recovered.append(self.finalize(operation_id))
            else:
                recovered.append(self.rollback(operation_id))
        return tuple(recovered)


def total_usage(paths: Iterable[Path]) -> tuple[int, int]:
    total_bytes = 0
    total_files = 0
    for path in paths:
        size, count = _tree_usage(path)
        total_bytes += size
        total_files += count
    return total_bytes, total_files
