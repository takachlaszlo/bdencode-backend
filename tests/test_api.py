from __future__ import annotations

from fastapi.testclient import TestClient

from bdencode import doctor
from bdencode.api import create_app
from bdencode.config import Settings
from bdencode.db import Database


def make_client(tmp_path) -> TestClient:
    return TestClient(create_app(Database(tmp_path / "api.sqlite3")))


def test_runtime_capabilities_is_fresh_and_does_not_prepare_paths(
    tmp_path, monkeypatch
):
    data_root = tmp_path / "unprepared-encode"
    source_root = tmp_path / "unprepared-storage"
    settings = Settings(
        data_root=data_root,
        source_roots=(source_root,),
    ).validate()
    database_root = tmp_path / "unprepared-state"
    database = Database(database_root / "api.sqlite3")
    calls = 0
    expected_tools = (*doctor.MANDATORY_TOOLS, *doctor.RECOMMENDED_TOOLS)

    def capability_report(names):
        nonlocal calls
        calls += 1
        assert tuple(names) == expected_tools
        return {
            "host": {
                "hostname": "test-host",
                "platform": "test-platform",
                "machine": "test-machine",
                "python": "3.test",
                "logical_cpus": 8,
            },
            "tools": {
                name: {
                    "name": name,
                    "path": f"/tools/{name}",
                    "version": f"test-call-{calls}",
                    "sha256": "0" * 64,
                    "available": True,
                }
                for name in names
            },
            "ffmpeg": {
                "encoders": sorted(doctor.MANDATORY_FFMPEG_ENCODERS),
                "filters": sorted(doctor.MANDATORY_FFMPEG_FILTERS),
                "protocols": sorted(doctor.MANDATORY_FFMPEG_PROTOCOLS),
            },
        }

    monkeypatch.setattr(doctor, "capability_snapshot", capability_report)
    monkeypatch.setattr(
        doctor,
        "_vapoursynth_plugins",
        lambda: {
            "ok": True,
            "plugins": {name: True for name in ("bs", "bwdif", "vivtc", "resize")},
            "error": None,
        },
    )
    monkeypatch.setattr(
        doctor,
        "_credential_status",
        lambda: {
            "configured": True,
            "encrypted_at_rest": True,
            "permissions_ok": True,
        },
    )

    with TestClient(create_app(database, settings=settings)) as client:
        first = client.get("/api/v1/runtime-capabilities")
        second = client.get("/api/v1/runtime-capabilities")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == 2
    first_report = first.json()
    second_report = second.json()
    data_status = first_report["paths"]["data"]
    assert data_status["path"] == str(data_root)
    assert data_status["exists"] is False
    assert data_status["readable"] is False
    assert data_status["root_writable"] is False
    assert data_status["writable"] is False
    assert data_status["ok"] is False
    assert set(data_status["required_writable_paths"]) == {
        "state",
        "jobs",
        "completed",
        "cache",
        "updates",
    }
    assert first_report["vapoursynth"]["ok"] is True
    assert isinstance(first_report["warnings"], list)
    assert first_report["database"]["schema_version"] is None
    assert "database is not initialized" in first_report["warnings"]
    assert set(first_report["tools"]) == set(expected_tools)
    assert "tsMuxeR" not in first_report["tools"]
    assert "whisper-cli" not in first_report["tools"]
    assert first_report["tools"]["ffmpeg"]["version"] == "test-call-1"
    assert second_report["tools"]["ffmpeg"]["version"] == "test-call-2"
    assert not data_root.exists()
    assert not source_root.exists()
    assert not database_root.exists()


def scan_result(
    *,
    warnings: list[str] | None = None,
    incomplete_color: bool = False,
) -> dict[str, object]:
    return {
        "source": "/storage/Film",
        "disc_kind": "bd",
        "content_kind": "film",
        "playlists": [
            {
                "playlist_id": "00001",
                "duration_seconds": 7200.0,
                "chapters": [],
                "segments": [],
                "streams": [
                    {
                        "id": "video:4113",
                        "index": 0,
                        "pid": 4113,
                        "kind": "video",
                        "codec": "h264",
                        "roles": [],
                        "video": {
                            "codec": "avc",
                            "width": 1920,
                            "height": 1080,
                            "frame_rate": "24000/1001",
                            "field_order": "progressive",
                            "bit_depth": 8,
                            "pixel_format": "yuv420p",
                            "color_primaries": None if incomplete_color else "bt709",
                            "color_transfer": None if incomplete_color else "bt709",
                            "color_matrix": None if incomplete_color else "bt709",
                            "color_range": None if incomplete_color else "limited",
                            "chroma_location": "left",
                        },
                    }
                ],
                "angle_count": 1,
            }
        ],
        "capabilities": {},
        "fingerprint": "scan-fingerprint",
        "warnings": warnings or [],
    }


def awaiting_selection_job(
    client: TestClient,
    *,
    warnings: list[str] | None = None,
    incomplete_color: bool = False,
):
    job = client.post(
        "/api/v1/jobs", json={"source_path": "/storage/Film", "name": "Film"}
    ).json()
    client.post("/api/v1/jobs/claim-next")
    scan = client.post("/api/v1/scans", json={"job_id": job["id"]}).json()
    response = client.patch(
        f"/api/v1/scans/{scan['id']}",
        json={
            "status": "AWAITING_SELECTION",
            "result": scan_result(warnings=warnings, incomplete_color=incomplete_color),
        },
    )
    assert response.status_code == 200
    return client.get(f"/api/v1/jobs/{job['id']}").json()


def valid_selection() -> dict[str, object]:
    return {
        "playlist_id": "1.mpls",
        "angle": 1,
        "output_name": "Film.1080p.BluRay.x264.mkv",
        "video": {
            "detail_level": "advanced",
            "temporal_filter": "progressive",
            "crop": {"left": "0", "top": "138", "right": "0", "bottom": "138"},
            "settings": {"crf": 17.5, "preset": "slow"},
        },
        "tracks": [],
        "upload_images": False,
    }


def test_health_capabilities_and_job_flow(tmp_path):
    with make_client(tmp_path) as client:
        capabilities = client.get("/api/v1/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["constraints"]["max_active_jobs"] == 1
        assert capabilities.json()["output_video_codecs"] == ["x264", "x265"]

        first = client.post(
            "/api/v1/jobs",
            json={"source_path": "/storage/Film/BDMV", "name": "Film"},
        )
        second = client.post(
            "/api/v1/jobs",
            json={"source_path": "/storage/Concert/BDMV", "name": "Concert"},
        )
        assert first.status_code == second.status_code == 201

        health = client.get("/api/v1/health").json()
        assert health["queued_jobs"] == 2
        assert health["active_job_id"] is None

        claimed = client.post("/api/v1/jobs/claim-next").json()
        job = claimed["job"]
        assert job["state"] == "SCANNING"
        blocked = client.post("/api/v1/jobs/claim-next").json()
        assert blocked["job"] is None
        assert blocked["blocked_by"]["id"] == job["id"]

        invalid = client.post(
            f"/api/v1/jobs/{job['id']}/transition", json={"state": "ENCODING"}
        )
        assert invalid.status_code == 409
        assert invalid.json()["current_state"] == "SCANNING"


def test_scan_selection_artifact_and_event_endpoints(tmp_path):
    with make_client(tmp_path) as client:
        job = client.post(
            "/api/v1/jobs", json={"source_path": "/storage/Series/BDMV"}
        ).json()
        client.post("/api/v1/jobs/claim-next")

        scan = client.post("/api/v1/scans", json={"job_id": job["id"]})
        assert scan.status_code == 201
        scan = scan.json()
        updated = client.patch(
            f"/api/v1/scans/{scan['id']}",
            json={
                "status": "AWAITING_SELECTION",
                "result": {"episodes": ["00001.mpls", "00002.mpls"]},
            },
        )
        assert updated.status_code == 200
        assert (
            client.get(f"/api/v1/jobs/{job['id']}").json()["state"]
            == "AWAITING_SELECTION"
        )

        selected = client.post(
            f"/api/v1/jobs/{job['id']}/selection",
            json={"selection": {"playlists": ["00001.mpls", "00002.mpls"]}},
        )
        assert selected.status_code == 200
        assert selected.json()["state"] == "READY"

        artifact = client.post(
            "/api/v1/artifacts",
            json={
                "job_id": job["id"],
                "scan_id": scan["id"],
                "kind": "SPECTROGRAM",
                "name": "audio-source.png",
                "path": "/home/accofil/encode/jobs/id/comparison/audio-source.png",
                "mime_type": "image/png",
                "sha256": "b" * 64,
            },
        )
        assert artifact.status_code == 201
        listed = client.get("/api/v1/artifacts", params={"job_id": job["id"]}).json()
        assert listed["items"][0]["kind"] == "SPECTROGRAM"

        custom_event = client.post(
            "/api/v1/events",
            json={"job_id": job["id"], "kind": "worker.note", "payload": {"pid": 42}},
        )
        assert custom_event.status_code == 201
        events = client.get("/api/v1/events", params={"job_id": job["id"]}).json()
        assert events["after_id"] == events["items"][-1]["id"]
        assert any(item["kind"] == "worker.note" for item in events["items"])


def test_unknown_resources_return_404(tmp_path):
    with make_client(tmp_path) as client:
        response = client.get("/api/v1/jobs/does-not-exist")
        assert response.status_code == 404


def test_png_artifact_content_is_inline_for_comparison_viewer(tmp_path):
    png = tmp_path / "comparison.png"
    png.write_bytes(b"not-a-real-png-but-the-api-does-not-decode-artifacts")

    with make_client(tmp_path) as client:
        job = client.post(
            "/api/v1/jobs", json={"source_path": "/storage/Film", "name": "Film"}
        ).json()
        artifact = client.post(
            "/api/v1/artifacts",
            json={
                "job_id": job["id"],
                "kind": "VIDEO_COMPARISON",
                "name": png.name,
                "path": str(png),
                "mime_type": "image/png",
            },
        ).json()

        response = client.get(f"/api/v1/artifacts/{artifact['id']}/content")

        assert response.status_code == 200
        assert response.content == png.read_bytes()
        assert response.headers["content-disposition"].startswith("inline;")
        assert response.headers["x-content-type-options"] == "nosniff"


def test_selection_validation_returns_effective_plan_without_mutating_job(tmp_path):
    with make_client(tmp_path) as client:
        job = awaiting_selection_job(client, warnings=["scanner advisory"])
        events_before = client.get(
            "/api/v1/events", params={"job_id": job["id"]}
        ).json()

        response = client.post(
            f"/api/v1/jobs/{job['id']}/selection/validate",
            json={
                "selection": valid_selection(),
                "expected_version": job["version"],
            },
        )

        assert response.status_code == 200
        preview = response.json()
        assert preview["valid"] is True
        assert preview["playlist_id"] == "00001"
        assert preview["encoder"] == "x264"
        assert preview["settings"]["crf"] == 17.5
        assert preview["settings"]["color"] == {
            "primaries": "bt709",
            "transfer": "bt709",
            "matrix": "bt709",
            "range": "limited",
            "chroma_location": "left",
        }
        assert preview["crop"] == {"left": 0, "top": 138, "right": 0, "bottom": 138}
        assert preview["temporal_filter"] == "progressive"
        assert preview["ffmpeg_video_args"][:2] == ["-c:v", "libx264"]
        assert preview["advisory_warnings"] == ["scanner advisory"]

        unchanged = client.get(f"/api/v1/jobs/{job['id']}").json()
        assert unchanged["state"] == "AWAITING_SELECTION"
        assert unchanged["selection"] is None
        assert unchanged["version"] == job["version"]
        events_after = client.get("/api/v1/events", params={"job_id": job["id"]}).json()
        assert events_after == events_before


def test_selection_validation_rejects_invalid_selection_without_mutation(tmp_path):
    with make_client(tmp_path) as client:
        job = awaiting_selection_job(client)
        selection = valid_selection()
        selection["video"]["crop"]["top"] = 137

        response = client.post(
            f"/api/v1/jobs/{job['id']}/selection/validate",
            json={"selection": selection},
        )

        assert response.status_code == 422
        assert "crop" in response.json()["detail"]
        unchanged = client.get(f"/api/v1/jobs/{job['id']}").json()
        assert unchanged["state"] == "AWAITING_SELECTION"
        assert unchanged["selection"] is None
        assert unchanged["version"] == job["version"]


def test_selection_validation_explains_and_accepts_color_confirmation(tmp_path):
    with make_client(tmp_path) as client:
        job = awaiting_selection_job(client, incomplete_color=True)
        selection = valid_selection()

        response = client.post(
            f"/api/v1/jobs/{job['id']}/selection/validate",
            json={"selection": selection},
        )

        assert response.status_code == 422
        problem = response.json()
        assert problem["code"] == "source_color_confirmation_required"
        assert problem["context"]["playlist_id"] == "00001"
        assert problem["context"]["missing_fields"] == [
            "primaries",
            "transfer",
            "matrix",
            "range",
        ]
        assert problem["context"]["blocking_fields"] == [
            "primaries",
            "transfer",
            "matrix",
        ]
        assert problem["context"]["confirmation_field"] == (
            "selection.video.settings.color"
        )
        assert problem["context"]["safe_defaults"] == {"range": "limited"}
        assert problem["context"]["suggested"] == {
            "primaries": "bt709",
            "transfer": "bt709",
            "matrix": "bt709",
            "range": "limited",
            "chroma_location": "left",
        }

        selection["video"]["settings"]["color"] = problem["context"]["suggested"]
        confirmed = client.post(
            f"/api/v1/jobs/{job['id']}/selection/validate",
            json={"selection": selection},
        )

        assert confirmed.status_code == 200
        assert confirmed.json()["settings"]["color"] == problem["context"]["suggested"]


def test_selection_validation_requires_successful_scan(tmp_path):
    with make_client(tmp_path) as client:
        job = client.post("/api/v1/jobs", json={"source_path": "/storage/Film"}).json()
        claimed = client.post("/api/v1/jobs/claim-next").json()["job"]
        client.post("/api/v1/scans", json={"job_id": job["id"]})
        awaiting = client.post(
            f"/api/v1/jobs/{job['id']}/transition",
            json={"state": "AWAITING_SELECTION"},
        )
        assert claimed["state"] == "SCANNING"
        assert awaiting.status_code == 200

        response = client.post(
            f"/api/v1/jobs/{job['id']}/selection/validate",
            json={"selection": valid_selection()},
        )

        assert response.status_code == 409
        assert response.json()["current_state"] == "AWAITING_SELECTION"
        assert "no successful scan" in response.json()["detail"]
