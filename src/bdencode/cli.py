"""Synchronous console entry point used by systemd and administrators."""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from .config import ConfigurationError, Settings, load_settings
from .db import Database
from .models import JobState


INSTALL_SAFE_OPERATOR_PAUSES = frozenset(
    {JobState.AWAITING_SELECTION, JobState.UPLOAD_FAILED}
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bdencode", description="BDEncode backend")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="TOML configuration path (or BDENCODE_CONFIG)",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="override the SQLite state database",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    api = commands.add_parser("api", help="run the localhost HTTP API")
    api.add_argument("--host", default=None)
    api.add_argument("--port", type=int, default=None)
    api.add_argument("--log-level", default=None)

    worker = commands.add_parser("worker", help="run the serial worker")
    worker.add_argument("--once", action="store_true", help="process at most one job")
    worker.add_argument("--poll-interval", type=float, default=None)

    commands.add_parser("init-db", help="create or migrate the state database")
    commands.add_parser(
        "db-status",
        help="schema version, integrity check, migration history and backups",
    )
    db_backup_command = commands.add_parser(
        "db-backup", help="write a verified online backup of the state database"
    )
    db_backup_command.add_argument("--label", default="manual")
    db_backup_command.add_argument(
        "--output-dir", type=Path, default=None, help="default: <state>/backups"
    )
    db_backups_command = commands.add_parser(
        "db-backups", help="list the database backups"
    )
    db_backups_command.add_argument(
        "--verify",
        action="store_true",
        help="re-hash every backup and re-run its integrity check",
    )
    db_restore = commands.add_parser(
        "db-restore",
        help="replace the state database with a verified backup (services stopped)",
    )
    db_restore.add_argument("backup", type=Path, help="backup file or its name")
    db_restore.add_argument(
        "--yes", action="store_true", help="confirm that the services are stopped"
    )
    db_restore.add_argument(
        "--force",
        action="store_true",
        help="restore even though a job still owns the pipeline",
    )
    doctor = commands.add_parser(
        "doctor", help="check the database and core media tools"
    )
    doctor.add_argument(
        "--json", action="store_true", help="retained for scripting compatibility"
    )
    queue_idle = commands.add_parser(
        "queue-idle", help="succeed only when no job blocks the pipeline"
    )
    queue_idle.add_argument(
        "--allow-review",
        action="store_true",
        help=(
            "also succeed only for AWAITING_SELECTION; NEEDS_REVIEW and all "
            "runnable pipeline states remain busy"
        ),
    )
    queue_idle.add_argument(
        "--allow-install-safe-pause",
        action="store_true",
        help=(
            "also succeed for durable AWAITING_SELECTION or UPLOAD_FAILED "
            "operator pauses; runnable and review states remain busy"
        ),
    )
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    settings = load_settings(args.config)
    if args.database is not None:
        settings = replace(
            settings, database_path=args.database.expanduser()
        ).validate()
    return settings


def _database(args: argparse.Namespace, settings: Settings | None = None) -> Database:
    selected = settings or _settings(args)
    return Database(selected.resolved_database_path)


def _call_with_supported_keywords(function, **kwargs):
    signature = inspect.signature(function)
    accepts_all = any(
        item.kind is inspect.Parameter.VAR_KEYWORD
        for item in signature.parameters.values()
    )
    selected = (
        kwargs
        if accepts_all
        else {
            key: value for key, value in kwargs.items() if key in signature.parameters
        }
    )
    return function(**selected)


def _run_api(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is not installed; install the backend API dependencies",
            file=sys.stderr,
        )
        return 2
    from .api import create_app

    settings = _settings(args)
    overrides: dict[str, object] = {}
    if args.host:
        overrides["bind_host"] = args.host
    if args.port is not None:
        overrides["bind_port"] = args.port
    if overrides:
        # Command-line overrides must satisfy the same loopback-only and port
        # invariants as configured values; the API relies on a trusted proxy.
        try:
            settings = replace(settings, **overrides).validate()
        except ConfigurationError as exc:
            print(f"bdencode: {exc}", file=sys.stderr)
            return 2
    uvicorn.run(
        create_app(_database(args, settings), settings=settings),
        host=settings.bind_host,
        port=settings.bind_port,
        log_level=(args.log_level or settings.log_level).lower(),
        root_path=settings.api_root_path,
    )
    return 0


def _run_worker(args: argparse.Namespace) -> int:
    """Lazy adapter so the persistence package works before worker installation."""

    try:
        module = importlib.import_module("bdencode.worker")
    except ImportError as exc:
        # Only treat the missing worker module itself as an optional component;
        # a missing dependency inside an installed worker remains actionable.
        if exc.name == "bdencode.worker":
            print("worker component is not installed yet", file=sys.stderr)
            return 2
        raise

    settings = _settings(args)
    run_worker = getattr(module, "run_worker", None)
    if callable(run_worker):
        result = _call_with_supported_keywords(
            run_worker,
            database=_database(args, settings),
            settings=settings,
            once=args.once,
            poll_interval=args.poll_interval or settings.worker_poll_seconds,
        )
        return int(result or 0)

    worker_main = getattr(module, "main", None)
    if callable(worker_main):
        forwarded = ["--database", str(settings.resolved_database_path)]
        if args.once:
            forwarded.append("--once")
        forwarded.extend(
            ["--poll-interval", str(args.poll_interval or settings.worker_poll_seconds)]
        )
        return int(worker_main(forwarded) or 0)

    print("bdencode.worker has no run_worker() or main() entry point", file=sys.stderr)
    return 2


def _init_db(args: argparse.Namespace) -> int:
    settings = _settings(args)
    database = _database(args, settings)
    database.initialize()
    print(f"initialized schema {database.schema_version()} at {database.display_path}")
    return 0


def _print_json(document: object) -> None:
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))


def _db_status(args: argparse.Namespace) -> int:
    from .db_backup import list_backups

    settings = _settings(args)
    database = _database(args, settings)
    database.initialize()
    path = Path(database.display_path).expanduser()
    backups = list_backups(database.backup_directory)
    integrity = database.integrity_check()
    _print_json(
        {
            "path": str(path),
            "schema_version": database.schema_version(),
            "size_bytes": path.stat().st_size if path.is_file() else None,
            "integrity": integrity,
            "migrations": database.migration_history(),
            "backup_directory": str(database.backup_directory),
            "backups": len(backups),
            "latest_backup": backups[0].to_dict() if backups else None,
        }
    )
    return 0 if integrity == ["ok"] else 1


def _db_backup(args: argparse.Namespace) -> int:
    from .db_backup import BackupError

    database = _database(args)
    try:
        info = database.backup(args.label, args.output_dir)
    except (BackupError, ValueError) as exc:
        print(f"bdencode: {exc}", file=sys.stderr)
        return 1
    _print_json({**info.to_dict(), "path": str(info.path)})
    return 0


def _db_backups(args: argparse.Namespace) -> int:
    from .db_backup import BackupError, list_backups, verify_backup

    database = _database(args)
    exit_code = 0
    listing = []
    for item in list_backups(database.backup_directory):
        entry = item.to_dict()
        if args.verify:
            try:
                entry["verified"] = verify_backup(item.path).verified
            except BackupError as exc:
                entry.update(verified=False, error=str(exc))
                exit_code = 1
        listing.append(entry)
    _print_json({"directory": str(database.backup_directory), "backups": listing})
    return exit_code


def _db_restore(args: argparse.Namespace) -> int:
    from .db_backup import BackupError, restore_backup

    database = _database(args)
    if not args.yes:
        print(
            "bdencode: stop bdencode-api and bdencode-worker first, then repeat "
            "with --yes",
            file=sys.stderr,
        )
        return 2
    candidate = args.backup
    if not candidate.is_absolute() and not candidate.exists():
        candidate = database.backup_directory / candidate.name
    path = Path(database.display_path).expanduser()
    if path.is_file() and not args.force:
        active = database.active_job()
        if active is not None:
            print(
                f"bdencode: {active.id} ({active.state.value}) still owns the "
                "pipeline; finish or stop it, or pass --force",
                file=sys.stderr,
            )
            return 3
    try:
        outcome = restore_backup(
            candidate, path, safety_directory=database.backup_directory
        )
    except BackupError as exc:
        print(f"bdencode: {exc}", file=sys.stderr)
        return 1
    _print_json(outcome)
    return 0


def _doctor(args: argparse.Namespace) -> int:
    try:
        module = importlib.import_module("bdencode.doctor")
    except ImportError as exc:
        if exc.name != "bdencode.doctor":
            raise
    else:
        run_doctor = getattr(module, "run_doctor", None)
        if callable(run_doctor):
            settings = _settings(args)
            return int(
                _call_with_supported_keywords(
                    run_doctor,
                    database=_database(args, settings),
                    settings=settings,
                )
                or 0
            )

    settings = _settings(args)
    database = _database(args, settings)
    database.initialize()
    mandatory = ("ffmpeg", "ffprobe", "mkvmerge", "mediainfo")
    tools = {name: shutil.which(name) for name in mandatory}
    report = {
        "database": {
            "path": database.display_path,
            "schema_version": database.schema_version(),
            "status": "ok",
        },
        "tools": tools,
        "status": "ok" if all(tools.values()) else "missing-tools",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


def _queue_idle(args: argparse.Namespace) -> int:
    settings = _settings(args)
    database = _database(args, settings)
    database.initialize()
    active = database.active_job()
    if active is not None:
        allow_pause = (
            args.allow_install_safe_pause
            and active.state in INSTALL_SAFE_OPERATOR_PAUSES
        ) or (
            args.allow_review and active.state is JobState.AWAITING_SELECTION
        )
        if allow_pause:
            print(f"operator-pause: {active.id} {active.state.value}")
            return 0
        print(f"busy: {active.id} {active.state.value}", file=sys.stderr)
        # A distinct code lets maintenance scripts distinguish an expected
        # busy queue from an unreadable database or invalid configuration.
        return 3
    print("idle")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "api": _run_api,
        "worker": _run_worker,
        "init-db": _init_db,
        "db-status": _db_status,
        "db-backup": _db_backup,
        "db-backups": _db_backups,
        "db-restore": _db_restore,
        "doctor": _doctor,
        "queue-idle": _queue_idle,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
