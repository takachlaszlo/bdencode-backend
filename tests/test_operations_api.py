from __future__ import annotations

import json
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient

from bdencode.api import create_app
from bdencode.config import Settings
from bdencode.db import Database
from bdencode.maintenance import MaintenanceSafetyError
from bdencode.models import ArtifactCreate, ArtifactKind, JobCreate
from bdencode.utils import sha256_file


_CROSS_SITE_HEADERS = {
    "Origin": "https://evil.example",
    "Sec-Fetch-Site": "cross-site",
}


def _settings(tmp_path: Path) -> Settings:
    source = tmp_path / "source"
    source.mkdir()
    settings = Settings(
        data_root=tmp_path / "data",
        source_roots=(source,),
        release_profiles_path=tmp_path / "profiles.json",
    ).validate()
    settings.create_directories()
    return settings


def _completed_job(
    tmp_path: Path,
) -> tuple[Settings, Database, str, Path, str]:
    settings = _settings(tmp_path)
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
                            "piece_size_min": 16384,
                            "piece_size_max": 65536,
                            "piece_size_default": 16384,
                            "target_piece_count_min": 1,
                            "target_piece_count_max": 100,
                            "screenshot_minimum": 1,
                            "screenshot_maximum": 2,
                            "credential_name": "tracker-example-token",
                        }
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    database = Database(settings.resolved_database_path)
    job = database.create_job(JobCreate(source_path=str(settings.source_roots[0])))
    with database._write() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'COMPLETED' WHERE id = ?", (job.id,)
        )
    workspace = settings.job_root(job.id)
    (workspace / "work").mkdir(parents=True)
    (workspace / "work" / "large.partial").write_bytes(b"w" * 37)
    for name in ("logs", "analysis", "comparison", "stages"):
        (workspace / name).mkdir()

    release_name = "Example.Movie.2026.1080p.BluRay.x264-GROUP"
    completed = settings.completed_root / release_name
    comparison = completed / "comparison"
    comparison.mkdir(parents=True)
    payload = completed / f"{release_name}.mkv"
    payload.write_bytes(b"payload" * 5000)
    digest = sha256_file(payload)
    (completed / ".bdencode-owner.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "output_name": release_name,
                "mux_sha256": digest,
            }
        ),
        encoding="utf-8",
    )
    screenshot = comparison / "pair-01-encode.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\napi-fixture")
    (comparison / "video-comparison.json").write_text(
        json.dumps(
            {
                "pairs": [
                    {
                        "encode_png": screenshot.name,
                        "encode_sha256": sha256_file(screenshot),
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
    return settings, database, job.id, payload, digest


def _metadata(release_name: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "release_name": release_name,
        "title": "Example Movie",
        "year": 2026,
        "edition": None,
        "imdb_id": "tt1234567",
        "tmdb_id": 123,
        "category": "Movie",
        "source_media": "BluRay",
        "resolution": "1080p",
        "video_codec": "x264",
        "audio_codecs": ["FLAC"],
        "languages": ["en"],
    }


def test_control_routes_pause_continue_and_cancel_idle_job(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.resolved_database_path)
    with TestClient(create_app(database, settings=settings)) as client:
        created = client.post(
            "/api/v1/jobs",
            json={"source_path": str(settings.source_roots[0])},
        ).json()
        paused = client.post(
            f"/api/v1/jobs/{created['id']}/pause",
            json={"expected_control_revision": created["control_revision"]},
        )
        assert paused.status_code == 202
        assert paused.json()["control_state"] == "PAUSED"
        continued = client.post(
            f"/api/v1/jobs/{created['id']}/continue",
            json={"expected_control_revision": paused.json()["control_revision"]},
        )
        assert continued.status_code == 202
        assert continued.json()["control_state"] == "RUNNING"
        cancelled = client.post(
            f"/api/v1/jobs/{created['id']}/cancel",
            json={"expected_control_revision": continued.json()["control_revision"]},
        )
        assert cancelled.status_code == 202
        assert cancelled.json()["state"] == "CANCELLED"


def test_mutation_guard_accepts_matching_origin_and_rejects_scheme_mismatch(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.resolved_database_path)
    with TestClient(
        create_app(database, settings=settings),
        base_url="https://encoder.example",
    ) as client:
        accepted = client.post(
            "/api/v1/jobs",
            json={"source_path": str(settings.source_roots[0])},
            headers={
                "Origin": "https://encoder.example",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        assert accepted.status_code == 201

        rejected = client.post(
            "/api/v1/jobs",
            json={"source_path": str(settings.source_roots[0])},
            headers={"Origin": "http://encoder.example"},
        )
        assert rejected.status_code == 409
        assert client.get("/api/v1/jobs").json()["meta"]["count"] == 1


def test_cross_site_operations_have_no_control_storage_or_release_side_effects(
    tmp_path: Path,
) -> None:
    settings, database, job_id, payload, digest = _completed_job(tmp_path)
    queued = database.create_job(JobCreate(source_path=str(settings.source_roots[0])))
    initial_control = database.get_job(queued.id)

    with TestClient(create_app(database, settings=settings)) as client:
        for action in ("pause", "cancel"):
            response = client.post(
                f"/api/v1/jobs/{queued.id}/{action}",
                json={"expected_control_revision": initial_control.control_revision},
                headers=_CROSS_SITE_HEADERS,
            )
            assert response.status_code == 409
        unchanged_control = database.get_job(queued.id)
        assert unchanged_control.state == initial_control.state
        assert unchanged_control.control_state == initial_control.control_state
        assert unchanged_control.control_revision == initial_control.control_revision

        completed = database.get_job(job_id)
        partial = settings.job_root(job_id) / "work" / "large.partial"
        cleanup = client.post(
            f"/api/v1/jobs/{job_id}/cleanup",
            json={"scope": "temporary", "expected_version": completed.version},
            headers=_CROSS_SITE_HEADERS,
        )
        assert cleanup.status_code == 409
        assert partial.is_file()

        purge = client.delete(
            f"/api/v1/jobs/{job_id}/purge",
            params={"expected_version": completed.version},
            headers=_CROSS_SITE_HEADERS,
        )
        assert purge.status_code == 409
        assert database.get_job(job_id).version == completed.version
        assert settings.job_root(job_id).is_dir()

        create_blocked = client.post(
            f"/api/v1/jobs/{job_id}/release-preparations",
            json={
                "profile_id": "example",
                "metadata": _metadata(payload.stem),
            },
            headers=_CROSS_SITE_HEADERS,
        )
        assert create_blocked.status_code == 409
        assert client.get(f"/api/v1/jobs/{job_id}/release-preparations").json() == []

        created = client.post(
            f"/api/v1/jobs/{job_id}/release-preparations",
            json={
                "profile_id": "example",
                "metadata": _metadata(payload.stem),
            },
        ).json()
        baseline = client.get(f"/api/v1/release-preparations/{created['id']}").json()
        for action in ("validate", "build", "export", "dupe-check", "seed"):
            blocked = client.post(
                f"/api/v1/release-preparations/{created['id']}/{action}",
                json={"expected_version": baseline["version"]},
                headers=_CROSS_SITE_HEADERS,
            )
            assert blocked.status_code == 409
        upload = client.post(
            f"/api/v1/release-preparations/{created['id']}/upload",
            json={
                "expected_version": baseline["version"],
                "manifest_sha256": "0" * 64,
                "approved_by": "operator",
            },
            headers={
                **_CROSS_SITE_HEADERS,
                "X-BDEncode-Manifest": "0" * 64,
            },
        )
        assert upload.status_code == 409
        assert (
            client.get(f"/api/v1/release-preparations/{created['id']}").json()
            == baseline
        )

        preparation_delete = client.delete(
            f"/api/v1/release-preparations/{created['id']}",
            params={"expected_version": baseline["version"]},
            headers=_CROSS_SITE_HEADERS,
        )
        assert preparation_delete.status_code == 409
        assert (
            client.get(f"/api/v1/release-preparations/{created['id']}").status_code
            == 200
        )

        release_delete = client.request(
            "DELETE",
            f"/api/v1/jobs/{job_id}/release",
            json={
                "confirmation": payload.stem,
                "expected_sha256": digest,
                "force_if_seeded": False,
            },
            headers=_CROSS_SITE_HEADERS,
        )
        assert release_delete.status_code == 409
        assert payload.is_file()


def test_invalid_release_profile_api_response_never_echoes_rejected_url(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    rejected_url = "http://tracker.example/sekrit"
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
                            "announce_urls": [rejected_url],
                            "credential_name": "tracker-example-token",
                        }
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    database = Database(settings.resolved_database_path)

    with TestClient(create_app(database, settings=settings)) as client:
        response = client.get("/api/v1/release-profiles")

    assert response.status_code == 422
    assert response.json() == {
        "detail": "release profile configuration is invalid",
        "code": "release_profile_configuration_invalid",
    }
    assert "sekrit" not in response.text
    assert rejected_url not in response.text


def test_storage_cleanup_and_completed_job_delete_preserve_public_release(
    tmp_path: Path,
) -> None:
    settings, database, job_id, payload, _digest = _completed_job(tmp_path)
    with TestClient(create_app(database, settings=settings)) as client:
        before = client.get(f"/api/v1/jobs/{job_id}/storage")
        assert before.status_code == 200
        assert before.json()["reclaimable_bytes"] == 37
        version = client.get(f"/api/v1/jobs/{job_id}").json()["version"]
        cleaned = client.post(
            f"/api/v1/jobs/{job_id}/cleanup",
            json={"scope": "temporary", "expected_version": version},
        )
        assert cleaned.status_code == 200
        assert cleaned.json()["bytes_removed"] == 37
        deleted = client.delete(
            f"/api/v1/jobs/{job_id}/purge",
            params={"expected_version": version, "preserve_release": True},
        )
        assert deleted.status_code == 204

    assert payload.is_file()
    assert not settings.job_root(job_id).exists()


def test_cleanup_does_not_report_failure_after_committed_finalize_safety_error(
    tmp_path: Path, monkeypatch
) -> None:
    settings, database, job_id, _payload, _digest = _completed_job(tmp_path)
    application = create_app(database, settings=settings)
    with TestClient(application) as client:
        version = client.get(f"/api/v1/jobs/{job_id}").json()["version"]

        def fail_finalize(_operation_id: str):
            raise MaintenanceSafetyError("quarantine changed after commit")

        monkeypatch.setattr(
            application.state.maintenance_journal, "finalize", fail_finalize
        )
        response = client.post(
            f"/api/v1/jobs/{job_id}/cleanup",
            json={"scope": "temporary", "expected_version": version},
        )

    assert response.status_code == 200
    assert not (settings.job_root(job_id) / "work").exists()
    with database._read() as connection:
        operation = connection.execute(
            "SELECT phase FROM maintenance_operations "
            "WHERE kind = 'completed-workspace-cleanup' AND subject_id = ?",
            (job_id,),
        ).fetchone()
    assert operation["phase"] == "COMMITTED"


def test_purge_does_not_report_failure_after_committed_finalize_safety_error(
    tmp_path: Path, monkeypatch
) -> None:
    settings, database, job_id, payload, _digest = _completed_job(tmp_path)
    application = create_app(database, settings=settings)
    with TestClient(application) as client:
        version = client.get(f"/api/v1/jobs/{job_id}").json()["version"]

        def fail_finalize(_operation_id: str):
            raise MaintenanceSafetyError("quarantine changed after commit")

        monkeypatch.setattr(
            application.state.maintenance_journal, "finalize", fail_finalize
        )
        response = client.delete(
            f"/api/v1/jobs/{job_id}/purge",
            params={"expected_version": version, "preserve_release": True},
        )

    assert response.status_code == 204
    assert not settings.job_root(job_id).exists()
    assert payload.is_file()
    with database._read() as connection:
        operation = connection.execute(
            "SELECT phase FROM maintenance_operations "
            "WHERE kind = 'terminal-job-purge' AND subject_id = ?",
            (job_id,),
        ).fetchone()
    assert operation["phase"] == "COMMITTED"


def test_release_prepare_build_export_and_explicit_release_delete(
    tmp_path: Path, monkeypatch
) -> None:
    settings, database, job_id, payload, digest = _completed_job(tmp_path)

    def capture(_runner, argv, *, timeout=30, check=True):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"Complete name : {argv[-1]}\nFormat : Matroska\n",
            stderr="",
        )

    monkeypatch.setattr("bdencode.release_service.CommandRunner.capture", capture)
    with TestClient(create_app(database, settings=settings)) as client:
        profiles = client.get("/api/v1/release-profiles")
        assert profiles.status_code == 200
        assert profiles.json()["items"][0]["profile_id"] == "example"

        created = client.post(
            f"/api/v1/jobs/{job_id}/release-preparations",
            json={
                "profile_id": "example",
                "metadata": _metadata(payload.stem),
            },
        )
        assert created.status_code == 201
        prepared = created.json()
        validated = client.post(
            f"/api/v1/release-preparations/{prepared['id']}/validate",
            json={"expected_version": prepared["version"]},
        )
        assert validated.json()["valid"] is True
        built = client.post(
            f"/api/v1/release-preparations/{prepared['id']}/build",
            json={"expected_version": prepared["version"]},
        )
        assert built.status_code == 200
        ready = built.json()
        assert ready["state"] == "READY"
        exported = client.post(
            f"/api/v1/release-preparations/{prepared['id']}/export",
            json={"expected_version": ready["version"]},
        )
        assert exported.status_code == 200
        assert exported.headers["cache-control"].startswith("private, no-store")
        assert exported.content.startswith(b"d8:announce")

        cross_site = client.post(
            f"/api/v1/release-preparations/{prepared['id']}/upload",
            json={
                "expected_version": ready["version"],
                "manifest_sha256": ready["manifest_sha256"],
                "approved_by": "operator",
            },
            headers={
                "X-BDEncode-Manifest": ready["manifest_sha256"],
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert cross_site.status_code == 409

        missing_operator = client.post(
            f"/api/v1/release-preparations/{prepared['id']}/upload",
            json={
                "expected_version": ready["version"],
                "manifest_sha256": ready["manifest_sha256"],
            },
            headers={"X-BDEncode-Manifest": ready["manifest_sha256"]},
        )
        assert missing_operator.status_code == 409
        assert "authenticated operator" in missing_operator.json()["detail"]

        spoofed_operator = client.post(
            f"/api/v1/release-preparations/{prepared['id']}/upload",
            json={
                "expected_version": ready["version"],
                "manifest_sha256": ready["manifest_sha256"],
                "approved_by": "spoofed-operator",
            },
            headers={
                "X-BDEncode-Manifest": ready["manifest_sha256"],
                "X-Remote-User": "trusted-operator",
            },
        )
        assert spoofed_operator.status_code == 422

        concurrently_created = client.post(
            f"/api/v1/jobs/{job_id}/release-preparations",
            json={
                "profile_id": "example",
                "metadata": _metadata(payload.stem),
            },
        ).json()
        stale_delete = client.request(
            "DELETE",
            f"/api/v1/jobs/{job_id}/release",
            json={
                "confirmation": payload.stem,
                "expected_sha256": digest,
                "force_if_seeded": False,
                "preparation_versions": {ready["id"]: ready["version"]},
            },
        )
        assert stale_delete.status_code == 409
        assert payload.is_file()
        assert (settings.release_kits_root / ready["id"]).is_dir()
        assert (
            client.delete(
                f"/api/v1/release-preparations/{concurrently_created['id']}",
                params={"expected_version": concurrently_created["version"]},
            ).status_code
            == 204
        )

        def fail_finalize(_operation_id: str):
            raise MaintenanceSafetyError("quarantine changed after commit")

        monkeypatch.setattr(
            client.app.state.maintenance_journal, "finalize", fail_finalize
        )

        removed = client.request(
            "DELETE",
            f"/api/v1/jobs/{job_id}/release",
            json={
                "confirmation": payload.stem,
                "expected_sha256": digest,
                "force_if_seeded": False,
                "preparation_versions": {ready["id"]: ready["version"]},
            },
        )
        assert removed.status_code == 204

        with database._read() as connection:
            operation = connection.execute(
                "SELECT phase FROM maintenance_operations "
                "WHERE kind = 'completed-release-delete' AND subject_id = ?",
                (job_id,),
            ).fetchone()
        assert operation["phase"] == "COMMITTED"

    assert not payload.parent.exists()
