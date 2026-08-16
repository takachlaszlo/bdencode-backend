from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import threading
from types import SimpleNamespace

import pytest

from bdencode.config import Settings
from bdencode.db import Database, StateConflictError
from bdencode.maintenance import MaintenanceSafetyError
from bdencode.models import ArtifactCreate, ArtifactKind, JobCreate
from bdencode.release import (
    DupeCheckOutcome,
    DupeCheckReceipt,
    PublicationOutcome,
    PublicationReceipt,
    QBitTorrentOutcome,
    QBitTorrentReceipt,
    ReleaseMetadata,
    ReleasePreparationState,
    verify_torrent,
)
from bdencode.release_service import (
    ReleaseService,
    ReleaseServiceError,
    _read_stable_bounded_file,
)
import bdencode.release_service as release_service_module
from bdencode.utils import sha256_file


class _Runner:
    def capture(
        self,
        argv: list[str],
        *,
        timeout: float = 30,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "mediainfo"
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                "General\n"
                f"Complete name : {argv[-1]}\n"
                "Format : Matroska\n"
                "Video\nFormat : AVC\n"
            ),
            stderr="",
        )


def _fixture(tmp_path: Path) -> tuple[ReleaseService, str, Path]:
    source = tmp_path / "source"
    source.mkdir()
    settings = Settings(
        data_root=tmp_path / "data",
        source_roots=(source,),
        release_profiles_path=tmp_path / "profiles.json",
    ).validate()
    settings.create_directories()
    settings.resolved_release_profiles_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profiles": [
                    {
                        "tracker": {
                            "schema_version": 1,
                            "profile_id": "example",
                            "display_name": "Example",
                            "torrent_source": "EXAMPLE",
                            "announce_urls": ["https://tracker.example/announce"],
                            "piece_size_min": 16_384,
                            "piece_size_max": 65_536,
                            "piece_size_default": 16_384,
                            "target_piece_count_min": 1,
                            "target_piece_count_max": 100,
                            "screenshot_minimum": 1,
                            "screenshot_maximum": 2,
                            "credential_name": "tracker-example-token",
                        },
                        "network": {
                            "allowed_hosts": ["tracker.example"],
                            "dupe_check_endpoint": "https://tracker.example/dupe",
                            "publish_endpoint": "https://tracker.example/publish",
                        },
                        "qbittorrent": {
                            "base_url": "http://127.0.0.1:8080",
                            "allowed_hosts": ["127.0.0.1"],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    database = Database(settings.resolved_database_path)
    job = database.create_job(JobCreate(source_path=str(source)))
    with database._write() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'COMPLETED' WHERE id = ?", (job.id,)
        )

    name = "Example.Movie.2026.1080p.BluRay.x264-GROUP"
    completed = settings.completed_root / name
    comparison = completed / "comparison"
    comparison.mkdir(parents=True)
    payload = completed / f"{name}.mkv"
    payload.write_bytes((b"matroska-test-payload\x00" * 2000) + b"end")
    digest = sha256_file(payload)
    (completed / ".bdencode-owner.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "output_name": name,
                "mux_sha256": digest,
            }
        ),
        encoding="utf-8",
    )
    png = comparison / "pair-01-encode.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    (comparison / "video-comparison.json").write_text(
        json.dumps(
            {
                "pairs": [
                    {
                        "encode_png": png.name,
                        "encode_sha256": sha256_file(png),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    database.create_artifact(
        ArtifactCreate(
            job_id=job.id,
            kind=ArtifactKind.OUTPUT,
            name=payload.name,
            path=str(payload.resolve()),
            mime_type="video/x-matroska",
            sha256=digest,
            size_bytes=payload.stat().st_size,
        )
    )
    return ReleaseService(database, settings, runner=_Runner()), job.id, payload


def _metadata(payload: Path) -> ReleaseMetadata:
    return ReleaseMetadata(
        release_name=payload.stem,
        title="Example Movie",
        year=2026,
        category="Movie",
        source_media="BluRay",
        resolution="1080p",
        video_codec="x264",
        audio_codecs=("FLAC",),
        languages=("en",),
    )


def test_release_build_is_private_single_mkv_payload_and_safe_public_view(
    tmp_path: Path,
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(
        job_id,
        profile_id="example",
        metadata=_metadata(payload),
    )

    ready = service.build(created.id, expected_version=created.version)
    torrent_path, _name = service.torrent_path(ready.id)
    torrent_data, torrent_name = service.torrent_bytes(ready.id)
    verification = verify_torrent(
        torrent_data,
        expected_release_name=payload.stem,
        expected_file_sha256=sha256_file(payload),
        payload_file=payload,
    )

    assert ready.state.value == "READY"
    assert ready.payload_path == f"{payload.stem}/{payload.name}"
    assert str(tmp_path) not in ready.model_dump_json()
    assert verification.payload_path == f"{payload.stem}/{payload.name}"
    assert torrent_name == torrent_path.name
    assert ready.manifest_sha256


def test_release_preflight_detects_completed_payload_change(tmp_path: Path) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(
        job_id,
        profile_id="example",
        metadata=_metadata(payload),
    )
    payload.write_bytes(b"changed")

    result = service.validate(created.id)

    assert result["valid"] is False
    assert "completed MKV changed" in " ".join(result["failures"])


def test_ready_release_cannot_be_rebuilt_or_damage_its_existing_kit(
    tmp_path: Path,
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    ready = service.build(created.id, expected_version=created.version)
    manifest = service.settings.release_kits_root / ready.id / "package-manifest.json"
    manifest_digest = sha256_file(manifest)
    original = service.store.get(ready.id)

    with pytest.raises(StateConflictError, match="cannot transition"):
        service.build(ready.id, expected_version=ready.version)

    assert service.store.get(ready.id) == original
    assert sha256_file(manifest) == manifest_digest


def test_profile_list_does_not_return_announce_or_credential(tmp_path: Path) -> None:
    service, _job_id, _payload = _fixture(tmp_path)

    serialized = json.dumps(service.profiles())

    assert "announce" not in serialized
    assert "credential" not in serialized
    assert "tracker.example" not in serialized


def test_release_delete_stale_version_never_touches_kit(tmp_path: Path) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    ready = service.build(created.id, expected_version=created.version)
    kit = service.settings.release_kits_root / ready.id

    with pytest.raises(StateConflictError, match="version is"):
        service.delete(ready.id, expected_version=created.version)

    assert kit.is_dir()
    assert service.store.get(ready.id).version == ready.version


def test_release_delete_restores_quarantine_when_database_delete_fails(
    tmp_path: Path,
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    ready = service.build(created.id, expected_version=created.version)
    kit = service.settings.release_kits_root / ready.id
    with service.database._write() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_release_delete BEFORE DELETE ON release_preparations
            BEGIN SELECT RAISE(ABORT, 'injected delete failure'); END
            """
        )

    with pytest.raises(sqlite3.DatabaseError, match="injected delete failure"):
        service.delete(ready.id, expected_version=ready.version)

    assert kit.is_dir()


def test_active_preparation_delete_creates_no_intent_or_filesystem_disruption(
    tmp_path: Path,
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    preparing = service.store.transition(
        created.id,
        ReleasePreparationState.PREPARING,
        expected_version=created.version,
    )
    orphan = service.settings.release_kits_root / preparing.id
    orphan.mkdir()
    sentinel = orphan / "partial"
    sentinel.write_bytes(b"active-build")

    with pytest.raises(StateConflictError, match="active release preparation"):
        service.delete(preparing.id, expected_version=preparing.version)

    with service.database._read() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM maintenance_operations WHERE subject_id = ?",
            (preparing.id,),
        ).fetchone()["count"]
    assert count == 0
    assert sentinel.read_bytes() == b"active-build"


def test_cross_linked_kit_record_blocks_multitarget_maintenance(
    tmp_path: Path,
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = [
        service.create(job_id, profile_id="example", metadata=_metadata(payload))
        for _ in range(2)
    ]
    ready = [service.build(item.id, expected_version=item.version) for item in created]
    kit_paths = [service.settings.release_kits_root / item.id for item in ready]
    with service.database._write() as connection:
        connection.execute(
            "UPDATE release_preparations SET kit_path = ? WHERE id = ?",
            (str(kit_paths[1]), ready[0].id),
        )
    corrupted = service.store.get(ready[0].id)

    with pytest.raises(ReleaseServiceError, match="safely deleted"):
        service._verified_maintenance_kit(corrupted)

    assert all(path.is_dir() for path in kit_paths)
    with service.database._read() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) AS count FROM maintenance_operations"
            ).fetchone()["count"]
            == 0
        )


def test_ready_result_survives_scratch_cleanup_failure_and_startup_reaps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    real_rmtree = release_service_module.shutil.rmtree

    def fail_scratch(path):
        if Path(path).name.startswith(".release-build-"):
            raise OSError("simulated scratch cleanup failure")
        return real_rmtree(path)

    monkeypatch.setattr(release_service_module.shutil, "rmtree", fail_scratch)

    ready = service.build(created.id, expected_version=created.version)

    assert ready.state is ReleasePreparationState.READY
    orphans = list(service.settings.release_kits_root.glob(".release-build-*"))
    assert len(orphans) == 1
    monkeypatch.setattr(release_service_module.shutil, "rmtree", real_rmtree)
    service._recover_interrupted_operations()
    assert not list(service.settings.release_kits_root.glob(".release-build-*"))
    assert service.store.get(ready.id).version == ready.version


def test_unsafe_release_scratch_is_retained_without_recursive_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    real_usage = release_service_module.safe_tree_usage
    real_rmtree = release_service_module.shutil.rmtree
    rmtree_calls: list[Path] = []

    def reject_scratch(path: Path) -> tuple[int, int]:
        if Path(path).name.startswith(".release-build-"):
            raise MaintenanceSafetyError("unsafe mounted release scratch")
        return real_usage(path)

    def record_rmtree(path: Path) -> None:
        rmtree_calls.append(Path(path))

    monkeypatch.setattr(release_service_module, "safe_tree_usage", reject_scratch)
    monkeypatch.setattr(release_service_module.shutil, "rmtree", record_rmtree)

    ready = service.build(created.id, expected_version=created.version)

    assert ready.state is ReleasePreparationState.READY
    assert len(list(service.settings.release_kits_root.glob(".release-build-*"))) == 1
    assert rmtree_calls == []
    monkeypatch.setattr(release_service_module, "safe_tree_usage", real_usage)
    monkeypatch.setattr(release_service_module.shutil, "rmtree", real_rmtree)
    service._recover_interrupted_operations()
    assert not list(service.settings.release_kits_root.glob(".release-build-*"))


def test_ready_persistence_failure_removes_the_unbound_final_kit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    real_transition = service.store.transition
    failed_ready_write = False

    def fail_first_ready_transition(preparation_id, target, **kwargs):
        nonlocal failed_ready_write
        if target is ReleasePreparationState.READY and not failed_ready_write:
            failed_ready_write = True
            raise sqlite3.OperationalError("injected READY persistence failure")
        return real_transition(preparation_id, target, **kwargs)

    monkeypatch.setattr(
        service.store,
        "transition",
        fail_first_ready_transition,
    )

    with pytest.raises(ReleaseServiceError, match="failed safely"):
        service.build(created.id, expected_version=created.version)

    failed = service.store.get(created.id)
    kit = service.settings.release_kits_root / created.id
    assert failed.state is ReleasePreparationState.FAILED
    assert failed.kit_path is None
    assert not kit.exists()
    trash = service.settings.release_kits_root / ".trash"
    assert not trash.exists() or tuple(trash.iterdir()) == ()


def test_seed_lease_allows_only_one_qbittorrent_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    ready = service.build(created.id, expected_version=created.version)
    calls = 0
    lock = threading.Lock()

    class FakeQBitTorrentClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def add_paused_and_recheck(self, *_args, **_kwargs) -> QBitTorrentReceipt:
            nonlocal calls
            with lock:
                calls += 1
            return QBitTorrentReceipt(
                outcome=QBitTorrentOutcome.ADDED_AND_RECHECKING,
                infohash=ready.torrent_infohash or "0" * 40,
                added_paused=True,
                full_recheck_requested=True,
                recorded_at=datetime.now(UTC),
            )

    monkeypatch.setattr(
        release_service_module, "QBitTorrentClient", FakeQBitTorrentClient
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(service.seed, ready.id, expected_version=ready.version)
            for _ in range(2)
        ]
    outcomes: list[object] = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except StateConflictError as exc:
            outcomes.append(exc)

    assert calls == 1
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    final = service.store.get(ready.id)
    assert final.state is ReleasePreparationState.READY
    assert final.qbittorrent_receipt is not None
    with pytest.raises(StateConflictError, match="already added"):
        service.seed(final.id, expected_version=final.version)
    assert calls == 1


def test_equivalent_preparations_share_one_qbittorrent_add_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    drafts = [
        service.create(job_id, profile_id="example", metadata=_metadata(payload))
        for _ in range(2)
    ]
    ready = [service.build(item.id, expected_version=item.version) for item in drafts]
    assert ready[0].torrent_infohash == ready[1].torrent_infohash
    entered = threading.Event()
    allow_completion = threading.Event()
    add_calls = 0

    class BlockingQBitTorrentClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def add_paused_and_recheck(self, torrent, **kwargs) -> QBitTorrentReceipt:
            nonlocal add_calls
            assert isinstance(torrent, bytes)
            add_calls += 1
            entered.set()
            assert allow_completion.wait(timeout=10)
            return QBitTorrentReceipt(
                outcome=QBitTorrentOutcome.ADDED_AND_RECHECKING,
                infohash=kwargs["expected_infohash"],
                added_paused=True,
                full_recheck_requested=True,
                recorded_at=datetime.now(UTC),
            )

    monkeypatch.setattr(
        release_service_module,
        "QBitTorrentClient",
        BlockingQBitTorrentClient,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            service.seed,
            ready[0].id,
            expected_version=ready[0].version,
        )
        assert entered.wait(timeout=10)
        try:
            with pytest.raises(StateConflictError, match="equivalent torrent"):
                service.seed(
                    ready[1].id,
                    expected_version=ready[1].version,
                )
        finally:
            allow_completion.set()
        seeded = first.result(timeout=10)

    assert add_calls == 1
    assert seeded.qbittorrent_receipt is not None
    assert seeded.qbittorrent_receipt["outcome"] == "ADDED_AND_RECHECKING"
    assert service.store.get(ready[1].id).state is ReleasePreparationState.READY


def test_seed_exception_persists_unknown_receipt_and_forbids_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    ready = service.build(created.id, expected_version=created.version)
    calls = 0

    class FailingQBitTorrentClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def add_paused_and_recheck(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("private upstream detail")

    monkeypatch.setattr(
        release_service_module, "QBitTorrentClient", FailingQBitTorrentClient
    )
    with pytest.raises(ReleaseServiceError, match="did not complete safely"):
        service.seed(ready.id, expected_version=ready.version)

    unknown = service.store.get(ready.id)
    assert unknown.state is ReleasePreparationState.UNKNOWN
    assert unknown.qbittorrent_receipt is not None
    assert unknown.qbittorrent_receipt["outcome"] == "UNKNOWN"
    assert "private upstream detail" not in (unknown.error or "")
    with pytest.raises(StateConflictError, match="verified release kit"):
        service.seed(unknown.id, expected_version=unknown.version)
    with pytest.raises(StateConflictError, match="external outcome"):
        service.delete(unknown.id, expected_version=unknown.version)
    assert (service.settings.release_kits_root / unknown.id).is_dir()
    assert service.store.get(unknown.id).state is ReleasePreparationState.UNKNOWN
    assert calls == 1


def test_seed_unknown_outcome_is_persisted_and_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    ready = service.build(created.id, expected_version=created.version)

    class UnknownQBitTorrentClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def add_paused_and_recheck(self, *_args, **_kwargs) -> QBitTorrentReceipt:
            return QBitTorrentReceipt(
                outcome=QBitTorrentOutcome.UNKNOWN,
                infohash=ready.torrent_infohash or "0" * 40,
                added_paused=None,
                full_recheck_requested=False,
                recorded_at=datetime.now(UTC),
            )

    monkeypatch.setattr(
        release_service_module, "QBitTorrentClient", UnknownQBitTorrentClient
    )
    unknown = service.seed(ready.id, expected_version=ready.version)

    assert unknown.state is ReleasePreparationState.UNKNOWN
    assert unknown.qbittorrent_receipt is not None
    assert unknown.qbittorrent_receipt["outcome"] == "UNKNOWN"
    with pytest.raises(StateConflictError, match="verified release kit"):
        service.seed(unknown.id, expected_version=unknown.version)


def test_seed_rejects_and_tombstones_a_mismatched_torrent_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    ready = service.build(created.id, expected_version=created.version)
    expected_infohash = ready.torrent_infohash or "0" * 40
    mismatched_infohash = (
        "f" if expected_infohash[0] != "f" else "e"
    ) + expected_infohash[1:]
    observed: dict[str, object] = {}

    class MismatchedQBitTorrentClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def add_paused_and_recheck(self, torrent, **kwargs) -> QBitTorrentReceipt:
            observed["torrent_is_bytes"] = isinstance(torrent, bytes)
            observed["expected_infohash"] = kwargs["expected_infohash"]
            return QBitTorrentReceipt(
                outcome=QBitTorrentOutcome.ADDED_AND_RECHECKING,
                infohash=mismatched_infohash,
                added_paused=True,
                full_recheck_requested=True,
                recorded_at=datetime.now(UTC),
            )

    monkeypatch.setattr(
        release_service_module,
        "QBitTorrentClient",
        MismatchedQBitTorrentClient,
    )

    with pytest.raises(ReleaseServiceError, match="unbound torrent receipt"):
        service.seed(ready.id, expected_version=ready.version)

    unknown = service.store.get(ready.id)
    assert observed == {
        "torrent_is_bytes": True,
        "expected_infohash": expected_infohash,
    }
    assert unknown.state is ReleasePreparationState.UNKNOWN
    assert unknown.qbittorrent_receipt is not None
    assert unknown.qbittorrent_receipt["outcome"] == "UNKNOWN"
    assert unknown.qbittorrent_receipt["infohash"] == mismatched_infohash


def _clear_dupe_receipt(
    ready, *, checked_at: datetime | None = None
) -> dict[str, object]:
    return DupeCheckReceipt(
        profile_id=ready.profile_id,
        manifest_sha256=ready.manifest_sha256,
        metadata_sha256=ready.metadata.canonical_digest(),
        outcome=DupeCheckOutcome.CLEAR,
        checked_at=checked_at or datetime.now(UTC),
    ).model_dump(mode="json")


def _install_fresh_clear_checker(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str] | None = None,
) -> None:
    class FreshClearChecker:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def check(self, metadata, *, profile_id, manifest_sha256):
            if calls is not None:
                calls.append(manifest_sha256)
            return DupeCheckReceipt(
                profile_id=profile_id,
                manifest_sha256=manifest_sha256,
                metadata_sha256=metadata.canonical_digest(),
                outcome=DupeCheckOutcome.CLEAR,
                checked_at=datetime.now(UTC),
            )

    monkeypatch.setattr(
        release_service_module,
        "HttpDupeChecker",
        FreshClearChecker,
    )


@pytest.mark.parametrize("operation", ["dupe", "seed", "publish"])
def test_network_side_effects_require_the_canonical_profile_digest(
    tmp_path: Path, operation: str
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    ready = service.build(created.id, expected_version=created.version)
    current = ready
    if operation == "publish":
        current = service.view(
            service.store.transition(
                ready.id,
                ReleasePreparationState.READY_TO_PUBLISH,
                expected_version=ready.version,
                values={"dupe_receipt_json": _clear_dupe_receipt(ready)},
            )
        )
    document = json.loads(
        service.settings.resolved_release_profiles_path.read_text(encoding="utf-8")
    )
    document["profiles"][0]["tracker"]["display_name"] = "Changed profile"
    service.settings.resolved_release_profiles_path.write_text(
        json.dumps(document), encoding="utf-8"
    )

    with pytest.raises(StateConflictError, match="profile changed"):
        if operation == "dupe":
            service.dupe_check(current.id, expected_version=current.version)
        elif operation == "seed":
            service.seed(current.id, expected_version=current.version)
        else:
            service.publish(
                current.id,
                expected_version=current.version,
                manifest_sha256=current.manifest_sha256 or "",
                approved_by="operator",
            )


@pytest.mark.parametrize("operation", ["seed", "publish"])
def test_seed_and_publish_revalidate_the_completed_payload_binding(
    tmp_path: Path, operation: str
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    ready = service.build(created.id, expected_version=created.version)
    current = ready
    if operation == "publish":
        current = service.view(
            service.store.transition(
                ready.id,
                ReleasePreparationState.READY_TO_PUBLISH,
                expected_version=ready.version,
                values={"dupe_receipt_json": _clear_dupe_receipt(ready)},
            )
        )
    payload.write_bytes(b"tampered after release preparation")

    with pytest.raises(ReleaseServiceError, match="owner/artifact"):
        if operation == "seed":
            service.seed(current.id, expected_version=current.version)
        else:
            service.publish(
                current.id,
                expected_version=current.version,
                manifest_sha256=current.manifest_sha256 or "",
                approved_by="operator",
            )
    assert service.store.get(current.id).state is current.state


def test_publication_persists_the_trusted_approval_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    ready = service.build(created.id, expected_version=created.version)
    publishable = service.view(
        service.store.transition(
            ready.id,
            ReleasePreparationState.READY_TO_PUBLISH,
            expected_version=ready.version,
            values={"dupe_receipt_json": _clear_dupe_receipt(ready)},
        )
    )
    approvals = []
    _install_fresh_clear_checker(monkeypatch)

    class Publisher:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def publish(self, _kit, *, approval, dupe_receipt):
            approvals.append(approval)
            return PublicationReceipt(
                profile_id=publishable.profile_id,
                manifest_sha256=publishable.manifest_sha256 or "0" * 64,
                outcome=PublicationOutcome.PUBLISHED,
                published_at=datetime.now(UTC),
                remote_id="fixture-release",
            )

    monkeypatch.setattr(release_service_module, "HttpTrackerPublisher", Publisher)
    published = service.publish(
        publishable.id,
        expected_version=publishable.version,
        manifest_sha256=publishable.manifest_sha256 or "",
        approved_by="trusted-operator",
    )

    assert published.state is ReleasePreparationState.PUBLISHED
    assert approvals[0].approved_by == "trusted-operator"
    assert published.publication_receipt is not None
    assert published.publication_receipt["approved_by"] == "trusted-operator"
    assert published.publication_receipt["approved_at"]


def test_publication_rechecks_instead_of_trusting_stored_clear_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    ready = service.build(created.id, expected_version=created.version)
    publishable = service.view(
        service.store.transition(
            ready.id,
            ReleasePreparationState.READY_TO_PUBLISH,
            expected_version=ready.version,
            values={
                "dupe_receipt_json": _clear_dupe_receipt(
                    ready,
                    checked_at=datetime.now(UTC) - timedelta(minutes=11),
                )
            },
        )
    )

    class DuplicateChecker:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def check(self, metadata, *, profile_id, manifest_sha256):
            return DupeCheckReceipt(
                profile_id=profile_id,
                manifest_sha256=manifest_sha256,
                metadata_sha256=metadata.canonical_digest(),
                outcome=DupeCheckOutcome.DUPLICATE,
                matches=("existing-release",),
                checked_at=datetime.now(UTC),
            )

    monkeypatch.setattr(
        release_service_module,
        "HttpDupeChecker",
        DuplicateChecker,
    )
    monkeypatch.setattr(
        release_service_module,
        "HttpTrackerPublisher",
        lambda *_args, **_kwargs: pytest.fail("publication must not be attempted"),
    )

    with pytest.raises(StateConflictError, match="did not return a bound CLEAR"):
        service.publish(
            publishable.id,
            expected_version=publishable.version,
            manifest_sha256=publishable.manifest_sha256 or "",
            approved_by="trusted-operator",
        )

    reviewed = service.store.get(publishable.id)
    assert reviewed.state is ReleasePreparationState.NEEDS_REVIEW
    assert reviewed.dupe_receipt is not None
    assert reviewed.dupe_receipt["outcome"] == "DUPLICATE"


def test_equivalent_preparations_share_one_publication_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    drafts = [
        service.create(job_id, profile_id="example", metadata=_metadata(payload))
        for _ in range(2)
    ]
    ready = [service.build(item.id, expected_version=item.version) for item in drafts]
    publishable = [
        service.view(
            service.store.transition(
                item.id,
                ReleasePreparationState.READY_TO_PUBLISH,
                expected_version=item.version,
                values={"dupe_receipt_json": _clear_dupe_receipt(item)},
            )
        )
        for item in ready
    ]
    entered = threading.Event()
    allow_completion = threading.Event()
    publication_calls = 0
    dupe_calls: list[str] = []
    _install_fresh_clear_checker(monkeypatch, dupe_calls)

    class BlockingPublisher:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def publish(self, _kit, *, approval, dupe_receipt):
            nonlocal publication_calls
            publication_calls += 1
            entered.set()
            assert allow_completion.wait(timeout=10)
            return PublicationReceipt(
                profile_id=approval.profile_id,
                manifest_sha256=approval.manifest_sha256,
                outcome=PublicationOutcome.PUBLISHED,
                published_at=datetime.now(UTC),
                remote_id="exclusive-publication",
            )

    monkeypatch.setattr(
        release_service_module,
        "HttpTrackerPublisher",
        BlockingPublisher,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            service.publish,
            publishable[0].id,
            expected_version=publishable[0].version,
            manifest_sha256=publishable[0].manifest_sha256 or "",
            approved_by="trusted-operator",
        )
        assert entered.wait(timeout=10)
        try:
            with pytest.raises(StateConflictError, match="equivalent release"):
                service.publish(
                    publishable[1].id,
                    expected_version=publishable[1].version,
                    manifest_sha256=publishable[1].manifest_sha256 or "",
                    approved_by="trusted-operator",
                )
        finally:
            allow_completion.set()
        published = first.result(timeout=10)

    assert publication_calls == 1
    # The sibling is rejected by the durable publication claim before it can
    # perform even a duplicate-check network request.
    assert len(dupe_calls) == 1
    assert published.state is ReleasePreparationState.PUBLISHED
    assert service.store.get(publishable[1].id).state is (
        ReleasePreparationState.READY_TO_PUBLISH
    )


def test_crash_recovery_fails_build_and_quarantines_orphan_kit(
    tmp_path: Path,
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    preparing = service.store.transition(
        created.id,
        ReleasePreparationState.PREPARING,
        expected_version=created.version,
    )
    orphan = service.settings.release_kits_root / preparing.id
    orphan.mkdir()
    (orphan / "partial").write_bytes(b"partial")
    generic = service.settings.release_kits_root / ".release-build-crashed"
    generic.mkdir()
    (generic / "partial").write_bytes(b"partial")

    service._recover_interrupted_operations()

    assert service.store.get(preparing.id).state is ReleasePreparationState.FAILED
    assert not orphan.exists()
    assert not generic.exists()


def test_second_service_in_same_singleton_lifecycle_does_not_recover_live_work(
    tmp_path: Path,
) -> None:
    service, job_id, payload = _fixture(tmp_path)
    created = service.create(job_id, profile_id="example", metadata=_metadata(payload))
    preparing = service.store.transition(
        created.id,
        ReleasePreparationState.PREPARING,
        expected_version=created.version,
    )

    ReleaseService(service.database, service.settings, runner=_Runner())

    assert service.store.get(preparing.id).state is ReleasePreparationState.PREPARING


def test_stable_torrent_read_rejects_an_in_read_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "kit"
    root.mkdir()
    torrent = root / "release.torrent"
    torrent.write_bytes(b"torrent bytes")
    real_fstat = release_service_module.os.fstat
    calls = 0

    def changing_fstat(descriptor: int):
        nonlocal calls
        details = real_fstat(descriptor)
        calls += 1
        if calls != 2:
            return details
        return SimpleNamespace(
            st_dev=details.st_dev,
            st_ino=details.st_ino,
            st_size=details.st_size,
            st_mtime_ns=details.st_mtime_ns + 1,
            st_mode=details.st_mode,
        )

    monkeypatch.setattr(release_service_module.os, "fstat", changing_fstat)
    with pytest.raises(ReleaseServiceError, match="changed while it was read"):
        _read_stable_bounded_file(torrent, root=root, maximum_bytes=1024)


def test_stable_torrent_read_rejects_a_link(tmp_path: Path) -> None:
    root = tmp_path / "kit"
    root.mkdir()
    outside = tmp_path / "outside.torrent"
    outside.write_bytes(b"outside")
    link = root / "release.torrent"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable on this Windows host")

    with pytest.raises(ReleaseServiceError, match="not a regular file"):
        _read_stable_bounded_file(link, root=root, maximum_bytes=1024)


def test_stable_torrent_read_rejects_a_delete_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "kit"
    root.mkdir()
    torrent = root / "release.torrent"
    torrent.write_bytes(b"torrent bytes")
    real_lstat = Path.lstat
    target_calls = 0

    def disappearing_lstat(path: Path, *args, **kwargs):
        nonlocal target_calls
        if path == torrent:
            target_calls += 1
            if target_calls == 2:
                raise FileNotFoundError(torrent)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", disappearing_lstat)
    with pytest.raises(ReleaseServiceError, match="disappeared"):
        _read_stable_bounded_file(torrent, root=root, maximum_bytes=1024)
