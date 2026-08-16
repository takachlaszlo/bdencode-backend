"""Application service for preparing, seeding and publishing tracker releases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

from .config import ConfigurationError, Settings
from .db import Database, StateConflictError
from .maintenance import (
    MaintenanceDomainGuard,
    MaintenanceJournal,
    MaintenanceSafetyError,
    MaintenanceTargetSpec,
    safe_tree_usage,
)
from .models import ArtifactKind, JobState
from .process import CommandRunner
from .release import (
    DupeCheckOutcome,
    HttpDupeChecker,
    HttpTrackerPublisher,
    PublicationApproval,
    PublicationOutcome,
    PublicationReceipt,
    QBitTorrentClient,
    QBitTorrentOutcome,
    QBitTorrentReceipt,
    ReleaseMetadata,
    ReleasePreparationState,
    build_private_torrent,
    build_upload_kit,
    sanitize_release_text,
    verify_torrent,
    verify_upload_kit,
)
from .release_profiles import ConfiguredReleaseProfile, load_release_profiles
from .release_store import ReleasePreparation, ReleaseStore
from .utils import sha256_file


_STARTUP_RECOVERY_LOCK = threading.Lock()
_STARTUP_RECOVERED_DATABASES: set[str] = set()
_DUPE_RECEIPT_MAX_AGE = timedelta(minutes=10)


class ReleaseServiceError(RuntimeError):
    pass


class ReleasePreparationView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    job_id: str
    state: ReleasePreparationState
    profile_id: str
    profile_digest: str
    metadata: ReleaseMetadata
    payload_path: str
    payload_size: int
    payload_sha256: str
    kit_ready: bool
    manifest_sha256: str | None
    torrent_infohash: str | None
    torrent_sha256: str | None
    dupe_receipt: dict[str, Any] | None
    qbittorrent_receipt: dict[str, Any] | None
    publication_receipt: dict[str, Any] | None
    error: str | None
    version: int
    created_at: datetime
    updated_at: datetime


def _is_link_or_reparse(path: Path) -> bool:
    try:
        information = path.lstat()
    except FileNotFoundError:
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(
        getattr(information, "st_file_attributes", 0) & flag
    )


def _safe_message(exc: BaseException) -> str:
    # External tool and network exceptions may contain local paths or remote
    # response bodies.  The precise exception remains available to the caller's
    # private traceback; only this bounded classification is persisted/API-visible.
    return f"{type(exc).__name__}: release operation did not complete"


def _file_identity(details: os.stat_result) -> tuple[int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
    )


def _read_stable_bounded_file(path: Path, *, root: Path, maximum_bytes: int) -> bytes:
    """Read one direct-child file without following a replace/link race."""

    if maximum_bytes < 1:
        raise ValueError("maximum_bytes must be positive")
    path = Path(path)
    root = Path(root)
    try:
        if _is_link_or_reparse(root) or not root.is_dir():
            raise ReleaseServiceError("release-kit directory is unsafe")
        resolved_root = root.resolve(strict=True)
        before = path.lstat()
        reparse = getattr(before, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        )
        if stat.S_ISLNK(before.st_mode) or reparse or not stat.S_ISREG(before.st_mode):
            raise ReleaseServiceError("release torrent is not a regular file")
        resolved = path.resolve(strict=True)
        if resolved.parent != resolved_root:
            raise ReleaseServiceError("release torrent escaped its upload kit")
        if before.st_size > maximum_bytes:
            raise ReleaseServiceError("release torrent exceeds the size limit")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(before) or not stat.S_ISREG(
                opened.st_mode
            ):
                raise ReleaseServiceError("release torrent changed while it was opened")
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after_open = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = path.lstat()
        data = b"".join(chunks)
        if (
            _file_identity(after_open) != _file_identity(before)
            or _file_identity(after) != _file_identity(before)
            or len(data) != before.st_size
        ):
            raise ReleaseServiceError("release torrent changed while it was read")
        if len(data) > maximum_bytes:
            raise ReleaseServiceError("release torrent exceeds the size limit")
        return data
    except ReleaseServiceError:
        raise
    except OSError:
        raise ReleaseServiceError(
            "release torrent changed or disappeared while it was read"
        ) from None


def _stable_file_sha256(path: Path, *, root: Path) -> tuple[int, str]:
    """Hash one direct-child regular file through a pinned descriptor."""

    path = Path(path)
    root = Path(root)
    try:
        if _is_link_or_reparse(root) or not root.is_dir():
            raise ReleaseServiceError("release directory is unsafe")
        resolved_root = root.resolve(strict=True)
        before = path.lstat()
        reparse = getattr(before, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        )
        if stat.S_ISLNK(before.st_mode) or reparse or not stat.S_ISREG(before.st_mode):
            raise ReleaseServiceError("release payload is not a regular file")
        if path.resolve(strict=True).parent != resolved_root:
            raise ReleaseServiceError("release payload escaped its owned directory")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _file_identity(
                opened
            ) != _file_identity(before):
                raise ReleaseServiceError("release payload changed before hashing")
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after_open = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = path.lstat()
        if not (
            _file_identity(before)
            == _file_identity(opened)
            == _file_identity(after_open)
            == _file_identity(after)
        ):
            raise ReleaseServiceError("release payload changed while hashing")
        return opened.st_size, digest.hexdigest()
    except ReleaseServiceError:
        raise
    except OSError:
        raise ReleaseServiceError(
            "release payload changed or disappeared while it was hashed"
        ) from None


class ReleaseService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        store: ReleaseStore | None = None,
        maintenance_journal: MaintenanceJournal | None = None,
        runner: CommandRunner | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.store = store or ReleaseStore(database)
        self.maintenance = maintenance_journal or MaintenanceJournal(database, settings)
        self.runner = runner or CommandRunner()
        self._now = now or (lambda: datetime.now(UTC))
        self.maintenance.recover()
        self._recover_once_at_startup()

    def _startup_recovery_key(self) -> str:
        path = self.database.display_path
        if path == ":memory:":
            return f":memory:{id(self.database)}"
        return os.path.normcase(os.path.abspath(os.path.expanduser(path)))

    def _recover_once_at_startup(self) -> None:
        """Recover crash leases exactly once during a process singleton's life."""

        key = self._startup_recovery_key()
        with _STARTUP_RECOVERY_LOCK:
            if key in _STARTUP_RECOVERED_DATABASES:
                return
            self._recover_interrupted_operations()
            _STARTUP_RECOVERED_DATABASES.add(key)

    def _recover_interrupted_operations(self) -> None:
        root = self.settings.release_kits_root
        interrupted = self.store.list_interrupted()
        preparing_ids = {
            record.id
            for record in interrupted
            if record.state is ReleasePreparationState.PREPARING
        }
        if _is_link_or_reparse(root) or not root.is_dir():
            raise ReleaseServiceError("release-kit root is unsafe")
        resolved_root = root.resolve(strict=True)
        candidates = [
            child
            for child in resolved_root.iterdir()
            if child.name.startswith(".release-build-")
            or any(
                child.name == identifier or child.name.startswith(f".{identifier}.tmp-")
                for identifier in preparing_ids
            )
        ]
        operation = (
            self.maintenance.begin(
                "interrupted-release-cleanup",
                "startup",
                [
                    MaintenanceTargetSpec(
                        candidate,
                        resolved_root,
                        "orphan release build",
                    )
                    for candidate in candidates
                ],
            )
            if candidates
            else None
        )
        try:
            if operation is not None:
                self.maintenance.stage(operation.id)
            if interrupted:
                self.store.recover_interrupted(
                    maintenance_operation_id=(
                        operation.id if operation is not None else None
                    )
                )
            elif operation is not None:
                with self.database._write() as connection:
                    self.database._mark_maintenance_committed(
                        connection,
                        operation.id,
                        kind="interrupted-release-cleanup",
                        subject_id="startup",
                    )
        except BaseException:
            if operation is not None:
                self.maintenance.rollback(operation.id)
            raise
        if operation is not None:
            try:
                self.maintenance.finalize(operation.id)
            except (MaintenanceSafetyError, OSError):
                # The committed journal remains for a later fail-closed retry.
                pass

    def profiles(self) -> tuple[dict[str, object], ...]:
        document = load_release_profiles(self.settings.resolved_release_profiles_path)
        return tuple(item.public_dict() for item in document.profiles)

    def _profile(self, profile_id: str) -> ConfiguredReleaseProfile:
        return load_release_profiles(self.settings.resolved_release_profiles_path).get(
            profile_id
        )

    def _bound_profile(self, record: ReleasePreparation) -> ConfiguredReleaseProfile:
        profile = self._profile(record.profile_id)
        if profile.canonical_digest() != record.profile_digest:
            raise StateConflictError(
                "tracker profile changed after release preparation creation"
            )
        return profile

    @staticmethod
    def view(record: ReleasePreparation) -> ReleasePreparationView:
        return ReleasePreparationView(
            id=record.id,
            job_id=record.job_id,
            state=record.state,
            profile_id=record.profile_id,
            profile_digest=record.profile_digest,
            metadata=record.metadata,
            payload_path=f"{record.metadata.release_name}/{record.payload_name}",
            payload_size=record.payload_size,
            payload_sha256=record.payload_sha256,
            kit_ready=record.kit_path is not None,
            manifest_sha256=record.manifest_sha256,
            torrent_infohash=record.torrent_infohash,
            torrent_sha256=record.torrent_sha256,
            dupe_receipt=record.dupe_receipt,
            qbittorrent_receipt=record.qbittorrent_receipt,
            publication_receipt=record.publication_receipt,
            error=record.error,
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def list_for_job(self, job_id: str) -> tuple[ReleasePreparationView, ...]:
        self.database.get_job(job_id)
        return tuple(self.view(item) for item in self.store.list_for_job(job_id))

    def get(self, preparation_id: str) -> ReleasePreparationView:
        return self.view(self.store.get(preparation_id))

    def _payload_artifact(self, job_id: str) -> tuple[Path, int, str]:
        job = self.database.get_job(job_id)
        if job.state is not JobState.COMPLETED:
            raise StateConflictError(
                "release preparation requires a completed encode", current=job.state
            )
        outputs = tuple(
            item
            for item in self.database.list_artifacts(job_id=job_id, limit=1000)
            if item.kind is ArtifactKind.OUTPUT
        )
        if len(outputs) != 1:
            raise ReleaseServiceError(
                "completed job must have exactly one registered output"
            )
        artifact = outputs[0]
        path = Path(artifact.path)
        completed_root = self.settings.completed_root.resolve(strict=True)
        if _is_link_or_reparse(path) or not path.is_file():
            raise ReleaseServiceError("registered output is not a regular media file")
        resolved = path.resolve(strict=True)
        if (
            resolved.suffix.casefold() != ".mkv"
            or resolved.parent.parent != completed_root
            or resolved.parent.name != resolved.stem
        ):
            raise ReleaseServiceError(
                "registered output escaped the completed release tree"
            )
        owner = resolved.parent / ".bdencode-owner.json"
        if _is_link_or_reparse(owner) or not owner.is_file():
            raise ReleaseServiceError("completed release has no safe owner record")
        try:
            owner_bytes = _read_stable_bounded_file(
                owner,
                root=resolved.parent,
                maximum_bytes=64 * 1024,
            )
            ownership = json.loads(owner_bytes.decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ReleaseServiceError(
                "completed release owner record is invalid"
            ) from exc
        if set(ownership) != {"schema_version", "output_name", "mux_sha256"}:
            raise ReleaseServiceError(
                "completed release owner record has unknown fields"
            )
        size, digest = _stable_file_sha256(resolved, root=resolved.parent)
        if (
            ownership.get("schema_version") != 2
            or ownership.get("output_name") != resolved.stem
            or ownership.get("mux_sha256") != digest
            or artifact.sha256 is None
            or artifact.sha256.casefold() != digest
            or (artifact.size_bytes is not None and artifact.size_bytes != size)
        ):
            raise ReleaseServiceError(
                "completed output no longer matches its owner/artifact"
            )
        return resolved, size, digest

    def _bound_payload(self, record: ReleasePreparation) -> Path:
        payload, size, digest = self._payload_artifact(record.job_id)
        if (
            str(payload) != record.payload_path
            or payload.name != record.payload_name
            or size != record.payload_size
            or digest != record.payload_sha256
        ):
            raise StateConflictError(
                "completed output changed after release preparation creation"
            )
        return payload

    def create(
        self,
        job_id: str,
        *,
        profile_id: str,
        metadata: ReleaseMetadata,
    ) -> ReleasePreparationView:
        profile = self._profile(profile_id)
        payload, size, digest = self._payload_artifact(job_id)
        if metadata.release_name != payload.stem:
            raise ReleaseServiceError(
                "release_name must exactly match the completed MKV name"
            )
        record = self.store.create(
            job_id=job_id,
            profile_id=profile_id,
            profile_digest=profile.canonical_digest(),
            metadata=metadata,
            payload_name=payload.name,
            payload_path=str(payload),
            payload_size=size,
            payload_sha256=digest,
        )
        return self.view(record)

    def validate(self, preparation_id: str) -> dict[str, object]:
        record = self.store.get(preparation_id)
        profile = self._profile(record.profile_id)
        failures: list[str] = []
        try:
            payload, size, digest = self._payload_artifact(record.job_id)
        except ReleaseServiceError:
            payload = Path(record.payload_path)
            size = record.payload_size
            digest = record.payload_sha256
            failures.append("completed MKV changed after preparation creation")
        if profile.canonical_digest() != record.profile_digest:
            failures.append("tracker profile changed after preparation creation")
        if (
            str(payload) != record.payload_path
            or size != record.payload_size
            or digest != record.payload_sha256
        ):
            failures.append("completed MKV changed after preparation creation")
        screenshots: tuple[Path, ...] = ()
        try:
            screenshots = self._screenshots(payload, profile)
        except ReleaseServiceError as exc:
            failures.append(str(exc))
        if record.kit_path and record.manifest_sha256:
            try:
                verify_upload_kit(
                    Path(record.kit_path),
                    expected_manifest_sha256=record.manifest_sha256,
                )
            except (OSError, ValueError, RuntimeError):
                failures.append("upload kit no longer matches its approved manifest")
        return {
            "valid": not failures,
            "failures": failures,
            "payload": {
                "path": f"{record.metadata.release_name}/{record.payload_name}",
                "size": record.payload_size,
                "sha256": record.payload_sha256,
            },
            "screenshots": len(screenshots),
            "profile_digest": record.profile_digest,
            "manifest_sha256": record.manifest_sha256,
        }

    def _screenshots(
        self,
        payload: Path,
        profile: ConfiguredReleaseProfile,
    ) -> tuple[Path, ...]:
        root = payload.parent / "comparison"
        if _is_link_or_reparse(root) or not root.is_dir():
            raise ReleaseServiceError("completed comparison evidence is unavailable")
        resolved_root = root.resolve(strict=True)
        if resolved_root.parent != payload.parent:
            raise ReleaseServiceError(
                "comparison evidence escaped the completed release"
            )
        manifest_path = resolved_root / "video-comparison.json"
        if _is_link_or_reparse(manifest_path) or not manifest_path.is_file():
            raise ReleaseServiceError("video comparison manifest is unavailable")
        if manifest_path.stat().st_size > 4 * 1024 * 1024:
            raise ReleaseServiceError("video comparison manifest is too large")
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReleaseServiceError("video comparison manifest is invalid") from exc
        pairs = document.get("pairs") if isinstance(document, dict) else None
        if not isinstance(pairs, list):
            raise ReleaseServiceError("video comparison has no screenshot pairs")
        candidates: list[Path] = []
        for item in pairs:
            if not isinstance(item, dict):
                continue
            key = (
                "encode_sdr_png"
                if isinstance(item.get("encode_sdr_png"), str)
                else "encode_png"
            )
            name = item.get(key)
            if not isinstance(name, str) or Path(name).name != name:
                continue
            candidate = resolved_root / name
            if (
                candidate.suffix.casefold() != ".png"
                or _is_link_or_reparse(candidate)
                or not candidate.is_file()
                or candidate.resolve(strict=True).parent != resolved_root
            ):
                continue
            expected_hash = item.get(key.replace("png", "sha256"))
            if (
                isinstance(expected_hash, str)
                and sha256_file(candidate) != expected_hash
            ):
                continue
            candidates.append(candidate)
        maximum = profile.tracker.screenshot_maximum
        minimum = profile.tracker.screenshot_minimum
        if len(candidates) < minimum:
            raise ReleaseServiceError(
                "completed release has too few validated encode-only screenshots"
            )
        if len(candidates) <= maximum:
            return tuple(candidates)
        # Evenly spread the chosen images across the title-wide comparison set.
        if maximum == 1:
            return (candidates[len(candidates) // 2],)
        indexes = {
            round(index * (len(candidates) - 1) / (maximum - 1))
            for index in range(maximum)
        }
        return tuple(candidates[index] for index in sorted(indexes))

    def _release_text(self, record: ReleasePreparation) -> tuple[str, str, str]:
        payload = Path(record.payload_path)
        completed = self.runner.capture(
            ["mediainfo", str(payload)], timeout=120, check=True
        )
        mediainfo = sanitize_release_text(
            completed.stdout,
            payload_filename=record.payload_name,
        )
        metadata = record.metadata
        identifiers = "\n".join(
            value
            for value in (
                f"IMDb: {metadata.imdb_id}" if metadata.imdb_id else "",
                f"TMDb: {metadata.tmdb_id}" if metadata.tmdb_id else "",
            )
            if value
        )
        nfo = (
            f"{metadata.release_name}\n\n"
            f"Title: {metadata.title}\nYear: {metadata.year}\n"
            f"Source: {metadata.source_media}\nResolution: {metadata.resolution}\n"
            f"Video: {metadata.video_codec}\n"
            f"Audio: {', '.join(metadata.audio_codecs)}\n"
            f"Languages: {', '.join(metadata.languages)}\n"
            f"Size: {record.payload_size} bytes\n"
            f"SHA-256: {record.payload_sha256}\n{identifiers}\n"
        )
        description = (
            f"[b]{metadata.title} ({metadata.year})[/b]\n\n"
            f"[b]Release[/b]: {metadata.release_name}\n"
            f"[b]Source[/b]: {metadata.source_media}\n"
            f"[b]Video[/b]: {metadata.video_codec} / {metadata.resolution}\n"
            f"[b]Audio[/b]: {', '.join(metadata.audio_codecs)}\n"
            f"[b]Languages[/b]: {', '.join(metadata.languages)}\n"
            f"\n[spoiler=MediaInfo]\n{mediainfo}\n[/spoiler]\n"
        )
        return mediainfo, nfo, description

    def build(
        self, preparation_id: str, *, expected_version: int
    ) -> ReleasePreparationView:
        record = self.store.get(preparation_id)
        profile = self._bound_profile(record)
        preflight = self.validate(preparation_id)
        if not preflight["valid"]:
            raise ReleaseServiceError("release preflight failed")
        preparing = self.store.transition(
            preparation_id,
            ReleasePreparationState.PREPARING,
            expected_version=expected_version,
            values={"error": None},
        )
        release_root = self.settings.release_kits_root
        scratch: Path | None = None
        try:
            release_root.mkdir(mode=0o750, parents=True, exist_ok=True)
            if _is_link_or_reparse(release_root) or not release_root.is_dir():
                raise ReleaseServiceError("release-kit root is unsafe")
            scratch = Path(tempfile.mkdtemp(prefix=".release-build-", dir=release_root))
            payload = Path(preparing.payload_path)
            torrent_path = scratch / f"{preparing.metadata.release_name}.torrent"
            torrent = build_private_torrent(
                payload,
                torrent_path,
                release_name=preparing.metadata.release_name,
                profile=profile.tracker.torrent_profile(),
            )
            if torrent.sha256 != preparing.payload_sha256:
                raise ReleaseServiceError("torrent builder observed a changed MKV")
            screenshots = self._screenshots(payload, profile)
            mediainfo, nfo, description = self._release_text(preparing)
            kit = build_upload_kit(
                release_root / preparing.id,
                profile=profile.tracker,
                metadata=preparing.metadata,
                torrent_path=torrent_path,
                payload_path=payload,
                mediainfo=mediainfo,
                nfo=nfo,
                description_bbcode=description,
                screenshots=screenshots,
                screenshot_roots=(payload.parent / "comparison",),
                created_at=self._now(),
            )
            ready = self.store.transition(
                preparing.id,
                ReleasePreparationState.READY,
                expected_version=preparing.version,
                values={
                    "kit_path": str(kit.directory),
                    "manifest_sha256": kit.manifest_sha256,
                    "torrent_infohash": torrent.infohash,
                    "torrent_sha256": torrent.torrent_sha256,
                    "error": None,
                },
            )
            return self.view(ready)
        except Exception as exc:
            current = self.store.get(preparation_id)
            if current.state is ReleasePreparationState.PREPARING:
                final_kit = release_root / current.id
                operation = None
                try:
                    if os.path.lexists(final_kit):
                        operation = self.maintenance.begin(
                            "failed-release-build-cleanup",
                            current.id,
                            [
                                MaintenanceTargetSpec(
                                    final_kit,
                                    release_root,
                                    "failed private release kit",
                                )
                            ],
                            guard=MaintenanceDomainGuard(
                                job_id=current.job_id,
                                preparation_id=current.id,
                                expected_preparation_version=current.version,
                                allowed_preparation_states=(
                                    ReleasePreparationState.PREPARING.value,
                                ),
                            ),
                        )
                        self.maintenance.stage(operation.id)
                    self.store.fail_build(
                        preparation_id,
                        expected_version=current.version,
                        error=_safe_message(exc),
                        maintenance_operation_id=(
                            operation.id if operation is not None else None
                        ),
                    )
                except BaseException:
                    if operation is not None:
                        self.maintenance.rollback(operation.id)
                    raise ReleaseServiceError(
                        "release build failed and requires startup recovery"
                    ) from None
                if operation is not None:
                    try:
                        self.maintenance.finalize(operation.id)
                    except (MaintenanceSafetyError, OSError):
                        # The FAILED transition committed the durable detach.
                        pass
            raise ReleaseServiceError("release build failed safely") from None
        finally:
            if scratch is not None and os.path.lexists(scratch):
                try:
                    resolved_root = release_root.resolve(strict=True)
                    if (
                        _is_link_or_reparse(scratch)
                        or not scratch.is_dir()
                        or scratch.resolve(strict=True).parent != resolved_root
                        or not scratch.name.startswith(".release-build-")
                    ):
                        raise ReleaseServiceError(
                            "release scratch failed cleanup containment"
                        )
                    safe_tree_usage(scratch)
                    shutil.rmtree(scratch)
                except (OSError, RuntimeError):
                    # READY/FAILED is already durable. Startup recovery owns
                    # any retained .release-build-* directory.
                    pass

    def torrent_path(self, preparation_id: str) -> tuple[Path, str]:
        record = self.store.get(preparation_id)
        if not record.kit_path or not record.manifest_sha256:
            raise StateConflictError("release upload kit is not ready")
        manifest = verify_upload_kit(
            Path(record.kit_path),
            expected_manifest_sha256=record.manifest_sha256,
        )
        torrent_files = [
            item for item in manifest.files if item.role.value == "TORRENT"
        ]
        if len(torrent_files) != 1:
            raise ReleaseServiceError("upload kit has no unique torrent")
        path = Path(record.kit_path) / torrent_files[0].path
        if sha256_file(path) != torrent_files[0].sha256:
            raise ReleaseServiceError("torrent changed after kit verification")
        return path, torrent_files[0].path

    def torrent_bytes(self, preparation_id: str) -> tuple[bytes, str]:
        """Return exact manifest-pinned torrent bytes for an HTTP response."""

        record = self.store.get(preparation_id)
        if not record.kit_path or not record.manifest_sha256:
            raise StateConflictError("release upload kit is not ready")
        try:
            kit = Path(record.kit_path)
            manifest = verify_upload_kit(
                kit,
                expected_manifest_sha256=record.manifest_sha256,
            )
            torrent_files = [
                item for item in manifest.files if item.role.value == "TORRENT"
            ]
            if len(torrent_files) != 1:
                raise ReleaseServiceError("upload kit has no unique torrent")
            item = torrent_files[0]
            data = _read_stable_bounded_file(
                kit / item.path,
                root=kit,
                maximum_bytes=16 * 1024 * 1024,
            )
            if (
                len(data) != item.size
                or hashlib.sha256(data).hexdigest() != item.sha256
            ):
                raise ReleaseServiceError(
                    "release torrent changed after manifest verification"
                )
            verification = verify_torrent(
                data,
                expected_release_name=record.metadata.release_name,
                expected_infohash=record.torrent_infohash,
            )
            if (
                manifest.profile_id != record.profile_id
                or manifest.metadata_sha256 != record.metadata.canonical_digest()
                or manifest.payload_size != record.payload_size
                or manifest.payload_sha256 != record.payload_sha256
                or manifest.torrent_infohash != record.torrent_infohash
                or verification.file_size != record.payload_size
                or verification.payload_path
                != f"{record.metadata.release_name}/{record.payload_name}"
            ):
                raise ReleaseServiceError(
                    "release torrent no longer matches its preparation"
                )
            return data, item.path
        except StateConflictError:
            raise
        except ReleaseServiceError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise ReleaseServiceError(
                "release torrent failed stable verification"
            ) from None

    def dupe_check(
        self, preparation_id: str, *, expected_version: int
    ) -> ReleasePreparationView:
        record = self.store.get(preparation_id)
        if record.state is not ReleasePreparationState.READY:
            raise StateConflictError("dupe check requires a READY release kit")
        profile = self._bound_profile(record)
        self._bound_payload(record)
        network = profile.network
        if not network or not network.dupe_check_endpoint or not record.manifest_sha256:
            raise ConfigurationError("tracker profile has no duplicate-check endpoint")
        checker = HttpDupeChecker(
            network.dupe_check_endpoint,
            allowed_hosts=network.allowed_hosts,
            credential_name=profile.tracker.credential_name,
        )
        checking = self.store.transition(
            preparation_id,
            ReleasePreparationState.SEEDING_CHECK,
            expected_version=expected_version,
        )
        try:
            receipt = checker.check(
                checking.metadata,
                profile_id=checking.profile_id,
                manifest_sha256=checking.manifest_sha256 or "",
            )
        except Exception as exc:
            self.store.transition(
                preparation_id,
                ReleasePreparationState.UNKNOWN,
                expected_version=checking.version,
                values={"error": _safe_message(exc)},
            )
            raise ReleaseServiceError("duplicate check did not complete") from None
        if receipt.outcome is DupeCheckOutcome.CLEAR:
            target = ReleasePreparationState.READY_TO_PUBLISH
        elif receipt.outcome is DupeCheckOutcome.DUPLICATE:
            target = ReleasePreparationState.NEEDS_REVIEW
        else:
            target = ReleasePreparationState.UNKNOWN
        updated = self.store.transition(
            preparation_id,
            target,
            expected_version=checking.version,
            values={"dupe_receipt_json": receipt.model_dump(mode="json")},
        )
        return self.view(updated)

    def seed(
        self, preparation_id: str, *, expected_version: int
    ) -> ReleasePreparationView:
        record = self.store.get(preparation_id)
        if record.state not in {
            ReleasePreparationState.READY,
            ReleasePreparationState.READY_TO_PUBLISH,
        }:
            raise StateConflictError("seeding requires a verified release kit")
        if record.qbittorrent_receipt is not None:
            try:
                previous_receipt = QBitTorrentReceipt.model_validate(
                    record.qbittorrent_receipt
                )
            except ValueError as exc:
                raise StateConflictError(
                    "stored qBittorrent receipt is invalid; seeding is blocked"
                ) from exc
            if previous_receipt.outcome is QBitTorrentOutcome.ADDED_AND_RECHECKING:
                raise StateConflictError(
                    "this release was already added to qBittorrent"
                )
        original_state = record.state
        profile = self._bound_profile(record)
        self._bound_payload(record)
        config = profile.qbittorrent
        if config is None:
            raise ConfigurationError("tracker profile has no qBittorrent integration")
        if record.torrent_infohash is None:
            raise StateConflictError("release kit has no torrent identity")
        torrent, torrent_name = self.torrent_bytes(preparation_id)
        client = QBitTorrentClient(
            config.base_url,
            allowed_hosts=config.allowed_hosts,
            username_credential=config.username_credential,
            password_credential=config.password_credential,
        )
        seeding = self.store.claim_seeding(
            preparation_id,
            expected_version=expected_version,
            expected_profile_digest=record.profile_digest,
            expected_payload_sha256=record.payload_sha256,
            expected_infohash=record.torrent_infohash,
        )
        try:
            receipt = client.add_paused_and_recheck(
                torrent,
                torrent_name=torrent_name,
                expected_infohash=record.torrent_infohash,
                save_path=self.settings.completed_root,
                category="bdencode",
            )
        except Exception as exc:
            unknown = QBitTorrentReceipt(
                outcome=QBitTorrentOutcome.UNKNOWN,
                infohash=record.torrent_infohash,
                added_paused=None,
                full_recheck_requested=False,
                recorded_at=self._now(),
            )
            self.store.transition(
                preparation_id,
                ReleasePreparationState.UNKNOWN,
                expected_version=seeding.version,
                values={
                    "qbittorrent_receipt_json": unknown.model_dump(mode="json"),
                    "error": _safe_message(exc),
                },
                message=(
                    "qBittorrent outcome is unknown; automatic retry is forbidden"
                ),
            )
            raise ReleaseServiceError(
                "qBittorrent operation did not complete safely"
            ) from None
        if receipt.infohash != record.torrent_infohash:
            unknown = QBitTorrentReceipt(
                outcome=QBitTorrentOutcome.UNKNOWN,
                infohash=receipt.infohash,
                added_paused=None,
                full_recheck_requested=False,
                recorded_at=self._now(),
            )
            self.store.transition(
                preparation_id,
                ReleasePreparationState.UNKNOWN,
                expected_version=seeding.version,
                values={
                    "qbittorrent_receipt_json": unknown.model_dump(mode="json"),
                    "error": (
                        "qBittorrent returned a different torrent identity; "
                        "automatic retry is forbidden"
                    ),
                },
                message=(
                    "qBittorrent receipt did not match the prepared torrent; "
                    "the remote outcome is unknown"
                ),
            )
            raise ReleaseServiceError(
                "qBittorrent returned an unbound torrent receipt"
            ) from None
        messages = {
            QBitTorrentOutcome.ADDED_AND_RECHECKING: (
                "torrent added paused and full recheck requested"
            ),
            QBitTorrentOutcome.REJECTED: "qBittorrent rejected the torrent",
            QBitTorrentOutcome.UNKNOWN: (
                "qBittorrent outcome is unknown; automatic retry is forbidden"
            ),
        }
        target = (
            ReleasePreparationState.UNKNOWN
            if receipt.outcome is QBitTorrentOutcome.UNKNOWN
            else original_state
        )
        return self.view(
            self.store.transition(
                preparation_id,
                target,
                expected_version=seeding.version,
                message=messages[receipt.outcome],
                values={
                    "qbittorrent_receipt_json": receipt.model_dump(mode="json"),
                    "error": (
                        "qBittorrent outcome is unknown; automatic retry is forbidden"
                        if target is ReleasePreparationState.UNKNOWN
                        else None
                    ),
                },
            )
        )

    def publish(
        self,
        preparation_id: str,
        *,
        expected_version: int,
        manifest_sha256: str,
        approved_by: str,
    ) -> ReleasePreparationView:
        record = self.store.get(preparation_id)
        if (
            record.state is not ReleasePreparationState.READY_TO_PUBLISH
            or record.manifest_sha256 != manifest_sha256
            or record.dupe_receipt is None
            or record.kit_path is None
        ):
            raise StateConflictError(
                "publication requires this manifest's current CLEAR dupe receipt"
            )
        if record.version != expected_version:
            raise StateConflictError(
                f"release preparation version is {record.version}, "
                f"expected {expected_version}"
            )
        profile = self._bound_profile(record)
        self._bound_payload(record)
        network = profile.network
        if (
            not network
            or not network.dupe_check_endpoint
            or not network.publish_endpoint
        ):
            raise ConfigurationError(
                "tracker publication requires duplicate-check and publish endpoints"
            )
        # Acquire the durable publication lease before any network request.
        # Destructive maintenance observes PUBLISHING and cannot commit an
        # intent that would detach the kit/payload during the fresh dupe check.
        publishing = self.store.claim_publication(
            preparation_id,
            expected_version=expected_version,
            expected_profile_digest=record.profile_digest,
            expected_manifest_sha256=manifest_sha256,
            expected_payload_sha256=record.payload_sha256,
            dupe_receipt=record.dupe_receipt,
        )
        checker = HttpDupeChecker(
            network.dupe_check_endpoint,
            allowed_hosts=network.allowed_hosts,
            credential_name=profile.tracker.credential_name,
        )
        try:
            dupe = checker.check(
                publishing.metadata,
                profile_id=publishing.profile_id,
                manifest_sha256=manifest_sha256,
            )
        except Exception as exc:
            self.store.transition(
                preparation_id,
                ReleasePreparationState.UNKNOWN,
                expected_version=publishing.version,
                values={"error": _safe_message(exc)},
                message=(
                    "publication-time duplicate check did not complete; "
                    "publication was not attempted"
                ),
            )
            raise ReleaseServiceError(
                "publication-time duplicate check did not complete"
            ) from None
        now = self._now()
        receipt_is_bound = (
            dupe.profile_id == record.profile_id
            and dupe.manifest_sha256 == manifest_sha256
            and dupe.metadata_sha256 == record.metadata.canonical_digest()
            and dupe.checked_at <= now
            and now - dupe.checked_at <= _DUPE_RECEIPT_MAX_AGE
        )
        if dupe.outcome is not DupeCheckOutcome.CLEAR or not receipt_is_bound:
            target = (
                ReleasePreparationState.NEEDS_REVIEW
                if dupe.outcome is DupeCheckOutcome.DUPLICATE and receipt_is_bound
                else ReleasePreparationState.UNKNOWN
            )
            self.store.transition(
                preparation_id,
                target,
                expected_version=publishing.version,
                values={
                    "dupe_receipt_json": dupe.model_dump(mode="json"),
                    "error": (
                        "publication-time duplicate check found an existing release"
                        if target is ReleasePreparationState.NEEDS_REVIEW
                        else "publication-time duplicate check was not safely bound"
                    ),
                },
                message=("publication was stopped by its immediate duplicate check"),
            )
            raise StateConflictError(
                "publication-time duplicate check did not return a bound CLEAR result"
            )
        publishing = self.store.transition(
            preparation_id,
            ReleasePreparationState.PUBLISHING,
            expected_version=publishing.version,
            values={"dupe_receipt_json": dupe.model_dump(mode="json")},
            message="fresh publication-time duplicate check recorded",
        )
        approval = PublicationApproval(
            profile_id=record.profile_id,
            manifest_sha256=manifest_sha256,
            approved_by=approved_by,
            approved_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        try:
            current_profile = self._bound_profile(publishing)
            self._bound_payload(publishing)
            if (
                publishing.manifest_sha256 != manifest_sha256
                or publishing.profile_digest != record.profile_digest
                or publishing.payload_sha256 != record.payload_sha256
            ):
                raise StateConflictError(
                    "release bindings changed after publication was claimed"
                )
            prepublish_now = self._now()
            if (
                dupe.checked_at > prepublish_now
                or prepublish_now - dupe.checked_at > _DUPE_RECEIPT_MAX_AGE
                or prepublish_now >= approval.expires_at
            ):
                raise StateConflictError(
                    "publication evidence expired during final revalidation"
                )
            current_network = current_profile.network
            if not current_network or not current_network.publish_endpoint:
                raise ConfigurationError("tracker profile has no publish endpoint")
            publisher = HttpTrackerPublisher(
                current_network.publish_endpoint,
                profile_id=current_profile.tracker.profile_id,
                allowed_hosts=current_network.allowed_hosts,
                credential_name=current_profile.tracker.credential_name,
            )
        except Exception as exc:
            self.store.transition(
                preparation_id,
                ReleasePreparationState.FAILED,
                expected_version=publishing.version,
                values={"error": _safe_message(exc)},
                message=(
                    "publication bindings changed after the exclusive lease; "
                    "tracker upload was not attempted"
                ),
            )
            raise ReleaseServiceError(
                "release changed before tracker publication"
            ) from None
        try:
            receipt = publisher.publish(
                Path(publishing.kit_path or ""),
                approval=approval,
                dupe_receipt=dupe,
            )
        except Exception as exc:
            unknown_receipt = PublicationReceipt(
                profile_id=record.profile_id,
                manifest_sha256=manifest_sha256,
                outcome=PublicationOutcome.UNKNOWN,
                published_at=now,
            ).model_dump(mode="json")
            unknown_receipt.update(
                {
                    "approved_by": approval.approved_by,
                    "approved_at": approval.approved_at.isoformat(),
                }
            )
            self.store.transition(
                preparation_id,
                ReleasePreparationState.UNKNOWN,
                expected_version=publishing.version,
                values={
                    "publication_receipt_json": unknown_receipt,
                    "error": _safe_message(exc),
                },
            )
            raise ReleaseServiceError(
                "tracker publication outcome is unknown"
            ) from None
        if receipt.outcome is PublicationOutcome.PUBLISHED:
            target = ReleasePreparationState.PUBLISHED
        elif receipt.outcome is PublicationOutcome.REJECTED:
            target = ReleasePreparationState.FAILED
        else:
            target = ReleasePreparationState.UNKNOWN
        publication_receipt = receipt.model_dump(mode="json")
        publication_receipt.update(
            {
                "approved_by": approval.approved_by,
                "approved_at": approval.approved_at.isoformat(),
            }
        )
        return self.view(
            self.store.transition(
                preparation_id,
                target,
                expected_version=publishing.version,
                values={"publication_receipt_json": publication_receipt},
            )
        )

    def _verified_maintenance_kit(self, record: ReleasePreparation) -> Path | None:
        if record.kit_path is None:
            return None
        root = self.settings.release_kits_root
        try:
            resolved_root = root.resolve(strict=True)
            kit = Path(record.kit_path)
            resolved = kit.resolve(strict=True)
            if (
                _is_link_or_reparse(kit)
                or not kit.is_dir()
                or resolved != resolved_root / record.id
                or resolved.parent != resolved_root
                or kit.name != record.id
            ):
                raise ReleaseServiceError("release kit cannot be safely deleted")
            if not record.manifest_sha256:
                raise ReleaseServiceError("release kit has no manifest binding")
            verify_upload_kit(
                resolved,
                expected_manifest_sha256=record.manifest_sha256,
            )
            return resolved
        except ReleaseServiceError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise ReleaseServiceError(
                "release kit failed maintenance verification"
            ) from None

    def delete(self, preparation_id: str, *, expected_version: int) -> None:
        record = self.store.get(preparation_id)
        if record.version != expected_version:
            raise StateConflictError(
                f"release preparation version is {record.version}, "
                f"expected {expected_version}"
            )
        deletable_states = {
            ReleasePreparationState.NOT_PREPARED,
            ReleasePreparationState.READY,
            ReleasePreparationState.READY_TO_PUBLISH,
            ReleasePreparationState.NEEDS_REVIEW,
            ReleasePreparationState.FAILED,
        }
        if (
            record.state
            in {
                ReleasePreparationState.UNKNOWN,
                ReleasePreparationState.PUBLISHED,
            }
            or (
                record.qbittorrent_receipt is not None
                and record.qbittorrent_receipt.get("outcome") != "REJECTED"
            )
            or (
                record.publication_receipt is not None
                and record.publication_receipt.get("outcome") != "REJECTED"
            )
        ):
            raise StateConflictError(
                "release preparation has an external outcome and must remain "
                "available for reconciliation or completed-release deletion"
            )
        if record.state not in deletable_states:
            raise StateConflictError("active release preparation cannot be deleted")
        root = self.settings.release_kits_root
        kit = self._verified_maintenance_kit(record)
        operation = (
            self.maintenance.begin(
                "release-preparation-delete",
                preparation_id,
                [
                    MaintenanceTargetSpec(
                        kit,
                        root,
                        "private release kit",
                    )
                ],
                guard=MaintenanceDomainGuard(
                    job_id=record.job_id,
                    preparation_id=record.id,
                    expected_preparation_version=expected_version,
                    allowed_preparation_states=tuple(
                        state.value for state in deletable_states
                    ),
                ),
            )
            if kit is not None
            else None
        )

        try:
            if operation is not None:
                self.maintenance.stage(operation.id)
            self.store.delete(
                preparation_id,
                expected_version=expected_version,
                maintenance_operation_id=(
                    operation.id if operation is not None else None
                ),
            )
        except BaseException:
            if operation is not None:
                self.maintenance.rollback(operation.id)
            raise
        if operation is not None:
            try:
                self.maintenance.finalize(operation.id)
            except (MaintenanceSafetyError, OSError):
                pass


__all__ = [
    "ReleasePreparationView",
    "ReleaseService",
    "ReleaseServiceError",
]
