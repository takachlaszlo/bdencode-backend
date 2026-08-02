from __future__ import annotations

from fastapi.testclient import TestClient

from bdencode.api import create_app
from bdencode.db import Database


def make_client(tmp_path) -> TestClient:
    return TestClient(create_app(Database(tmp_path / "api.sqlite3")))


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
