"""Consistent SQLite backups, verification, retention and offline restore.

Backups use SQLite's online backup API rather than copying the ``.sqlite3``,
``-wal`` and ``-shm`` files.  The API reads a transactionally consistent
snapshot (including committed WAL pages) while the API and worker keep running,
and the copy is converted to ``journal_mode=DELETE`` so every backup is one
self-contained file.

Each backup ``encoder-<UTC stamp>-<label>-<token>.sqlite3`` gets a JSON
manifest next to it (size, SHA-256, schema version, table counts).  A backup is
only ever published after ``PRAGMA integrity_check`` passed on the copy, by
atomic rename, so a crash never leaves a half-written file under a valid name.

This module deliberately does not import :mod:`bdencode.db` at import time:
the database layer calls it *before* a schema migration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
NAME_RE = re.compile(
    r"^encoder-(?P<stamp>\d{8}T\d{6}Z)-(?P<label>[a-z0-9][a-z0-9-]{0,39})"
    r"-(?P<token>[0-9a-f]{8})\.sqlite3$"
)
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
MANIFEST_SCHEMA = 1

# How many backups of each kind are kept.  Pre-migration and pre-restore copies
# are the ones that protect against a bad upgrade, so they outlive the routine
# scheduled copies only by count, never by exemption.
DEFAULT_KEEP: Mapping[str, int] = {
    "scheduled": 14,
    "manual": 10,
    "pre-migration": 5,
    "pre-restore": 3,
}
_FALLBACK_KEEP = 10


class BackupError(RuntimeError):
    """A backup could not be created, verified or restored."""


@dataclass(frozen=True, slots=True)
class BackupInfo:
    name: str
    path: Path
    label: str
    created_at: str
    size_bytes: int
    sha256: str | None
    schema_version: int | None
    jobs: int | None
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "created_at": self.created_at,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "schema_version": self.schema_version,
            "jobs": self.jobs,
            "verified": self.verified,
        }


def retention_group(label: str) -> str:
    """``pre-migration-v1`` and ``pre-migration-v2`` share one retention pool."""

    return re.sub(r"-v\d+$", "", label)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_version(connection: sqlite3.Connection) -> int | None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
    ).fetchone()
    if exists is None:
        return None
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    return int(row[0]) if row is not None else None


def _job_count(connection: sqlite3.Connection) -> int | None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).fetchone()
    if exists is None:
        return None
    return int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.partial")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def create_backup(
    source: Path,
    directory: Path,
    *,
    label: str = "manual",
    now: datetime | None = None,
) -> BackupInfo:
    """Write a verified, self-contained backup of ``source`` into ``directory``."""

    if not LABEL_RE.fullmatch(label):
        raise BackupError("backup label must be lowercase letters, digits and dashes")
    source = Path(source).expanduser()
    if not source.is_file():
        raise BackupError(f"database does not exist: {source}")
    directory = Path(directory).expanduser()
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)

    moment = now or datetime.now(UTC)
    name = (
        f"encoder-{moment.strftime(STAMP_FORMAT)}-{label}-{secrets.token_hex(4)}.sqlite3"
    )
    final = directory / name
    temporary = directory / f".{name}.partial"
    try:
        with closing(sqlite3.connect(str(source), timeout=30)) as origin:
            with closing(sqlite3.connect(str(temporary))) as copy:
                origin.backup(copy)
                copy.execute("PRAGMA journal_mode = DELETE")
                if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise BackupError("the backup copy failed its integrity check")
                schema_version = _schema_version(copy)
                jobs = _job_count(copy)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, final)
        _fsync_directory(directory)
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(f"database backup failed: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)

    try:
        os.chmod(final, 0o600)
    except OSError:
        pass
    digest = _sha256(final)
    created_at = moment.astimezone(UTC).isoformat(timespec="seconds")
    size = final.stat().st_size
    manifest = {
        "manifest_schema": MANIFEST_SCHEMA,
        "name": name,
        "label": label,
        "created_at": created_at,
        "size_bytes": size,
        "sha256": digest,
        "schema_version": schema_version,
        "jobs": jobs,
        "source_name": source.name,
    }
    _atomic_write(
        final.with_name(name + ".json"),
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return BackupInfo(
        name=name,
        path=final,
        label=label,
        created_at=created_at,
        size_bytes=size,
        sha256=digest,
        schema_version=schema_version,
        jobs=jobs,
        verified=True,
    )


def _read_manifest(data_file: Path) -> dict[str, Any] | None:
    manifest = data_file.with_name(data_file.name + ".json")
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(document, dict)
        or document.get("manifest_schema") != MANIFEST_SCHEMA
        or document.get("name") != data_file.name
    ):
        return None
    return document


def list_backups(directory: Path) -> list[BackupInfo]:
    """Backups that have a valid manifest, newest first (no hashing)."""

    directory = Path(directory).expanduser()
    if not directory.is_dir():
        return []
    result: list[BackupInfo] = []
    for candidate in directory.iterdir():
        match = NAME_RE.fullmatch(candidate.name)
        if match is None or not candidate.is_file():
            continue
        document = _read_manifest(candidate)
        if document is None:
            continue
        try:
            result.append(
                BackupInfo(
                    name=candidate.name,
                    path=candidate,
                    label=str(document["label"]),
                    created_at=str(document["created_at"]),
                    size_bytes=int(document["size_bytes"]),
                    sha256=str(document["sha256"]),
                    schema_version=(
                        None
                        if document.get("schema_version") is None
                        else int(document["schema_version"])
                    ),
                    jobs=None if document.get("jobs") is None else int(document["jobs"]),
                    verified=False,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(result, key=lambda item: (item.created_at, item.name), reverse=True)


def verify_backup(
    path: Path, *, supported_versions: tuple[int, ...] | None = None
) -> BackupInfo:
    """Re-check a backup file: manifest hash (if any), integrity, schema."""

    path = Path(path).expanduser()
    if not path.is_file():
        raise BackupError(f"backup does not exist: {path}")
    if supported_versions is None:
        from .db import MIGRATIONS, SCHEMA_VERSION

        supported_versions = (*MIGRATIONS, SCHEMA_VERSION)
    document = _read_manifest(path)
    digest = _sha256(path)
    if document is not None and document.get("sha256") != digest:
        raise BackupError("backup checksum does not match its manifest")
    try:
        with closing(
            sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        ) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BackupError("backup failed its integrity check")
            version = _schema_version(connection)
            jobs = _job_count(connection)
    except sqlite3.Error as exc:
        raise BackupError(f"backup is not a readable database: {exc}") from exc
    if version not in supported_versions:
        raise BackupError(
            f"backup has unsupported schema {version}; supported: "
            f"{', '.join(map(str, supported_versions))}"
        )
    match = NAME_RE.fullmatch(path.name)
    return BackupInfo(
        name=path.name,
        path=path,
        label=match["label"] if match else "external",
        created_at=(
            str(document["created_at"])
            if document
            else datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(
                timespec="seconds"
            )
        ),
        size_bytes=path.stat().st_size,
        sha256=digest,
        schema_version=version,
        jobs=jobs,
        verified=True,
    )


def prune_backups(
    directory: Path, *, keep: Mapping[str, int] | None = None
) -> list[str]:
    """Delete backups beyond the per-kind retention; returns the removed names."""

    limits = {**DEFAULT_KEEP, **(dict(keep) if keep else {})}
    seen: dict[str, int] = {}
    removed: list[str] = []
    for item in list_backups(directory):  # newest first
        group = retention_group(item.label)
        seen[group] = seen.get(group, 0) + 1
        if seen[group] <= limits.get(group, _FALLBACK_KEEP):
            continue
        item.path.unlink(missing_ok=True)
        item.path.with_name(item.name + ".json").unlink(missing_ok=True)
        removed.append(item.name)
    return removed


def ensure_scheduled_backup(
    source: Path,
    directory: Path,
    *,
    interval: timedelta,
    keep: Mapping[str, int] | None = None,
    now: datetime | None = None,
) -> BackupInfo | None:
    """Create a ``scheduled`` backup when the newest one is older than ``interval``."""

    moment = now or datetime.now(UTC)
    newest = next(
        (item for item in list_backups(directory) if item.label == "scheduled"), None
    )
    if newest is not None:
        try:
            age = moment - datetime.fromisoformat(newest.created_at)
        except ValueError:
            age = interval
        if age < interval:
            return None
    created = create_backup(source, directory, label="scheduled", now=moment)
    prune_backups(directory, keep=keep)
    return created


def restore_backup(
    backup: Path,
    database_path: Path,
    *,
    safety_directory: Path,
) -> dict[str, Any]:
    """Replace ``database_path`` with a verified backup (services must be stopped).

    The current database is first copied to a ``pre-restore`` backup, and the
    stale ``-wal``/``-shm`` files are removed so SQLite cannot replay them over
    the restored content.  A backup of an older schema is fine: the normal
    initialization migrates it (with its own pre-migration backup) at startup.
    """

    info = verify_backup(backup)
    database_path = Path(database_path).expanduser()
    safety: BackupInfo | None = None
    if database_path.is_file():
        safety = create_backup(database_path, safety_directory, label="pre-restore")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = database_path.with_name(f".{database_path.name}.restore")
    try:
        with closing(sqlite3.connect(str(info.path))) as origin:
            with closing(sqlite3.connect(str(temporary))) as copy:
                origin.backup(copy)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        for suffix in ("-wal", "-shm"):
            Path(f"{database_path}{suffix}").unlink(missing_ok=True)
        os.replace(temporary, database_path)
        _fsync_directory(database_path.parent)
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(f"restore failed: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "restored": info.name,
        "schema_version": info.schema_version,
        "jobs": info.jobs,
        "safety_backup": safety.name if safety else None,
    }
