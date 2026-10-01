from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bdencode.api import create_app
from bdencode.config import ConfigurationError, Settings
from bdencode.db import Database
from bdencode.models import ArtifactCreate, ArtifactKind, JobCreate


def _client(tmp_path: Path) -> tuple[TestClient, Settings, Database]:
    source = tmp_path / "source"
    source.mkdir()
    settings = Settings(data_root=tmp_path / "data", source_roots=(source,)).validate()
    settings.create_directories()
    database = Database(tmp_path / "state.sqlite3")
    app = create_app(database, settings=settings)
    # An unhandled exception must surface as a 500 response, not a test crash.
    return TestClient(app, raise_server_exceptions=False), settings, database


def test_browsing_a_missing_source_directory_is_a_client_error(tmp_path: Path) -> None:
    client, settings, _ = _client(tmp_path)
    missing = settings.source_roots[0] / "gone"
    response = client.get("/api/v1/sources", params={"path": str(missing)})
    assert response.status_code == 422
    assert "does not exist" in response.json()["detail"]


def test_browsing_a_path_with_a_nul_byte_is_a_client_error(tmp_path: Path) -> None:
    client, settings, _ = _client(tmp_path)
    response = client.get(
        "/api/v1/sources", params={"path": f"{settings.source_roots[0]}/a\x00b"}
    )
    assert response.status_code == 422


def test_browsing_a_symlink_loop_is_a_client_error(tmp_path: Path) -> None:
    client, settings, _ = _client(tmp_path)
    root = settings.source_roots[0]
    (root / "a").symlink_to(root / "b")
    (root / "b").symlink_to(root / "a")
    response = client.get("/api/v1/sources", params={"path": str(root / "a")})
    assert response.status_code == 422


def test_creating_a_job_for_a_missing_source_is_a_client_error(tmp_path: Path) -> None:
    client, settings, _ = _client(tmp_path)
    response = client.post(
        "/api/v1/jobs",
        json={"source_path": str(settings.source_roots[0] / "typo")},
    )
    assert response.status_code == 422
    assert "does not exist" in response.json()["detail"]
    assert client.get("/api/v1/jobs").json()["meta"]["count"] == 0


def test_authorize_source_still_rejects_paths_outside_the_roots(
    tmp_path: Path,
) -> None:
    _, settings, _ = _client(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ConfigurationError, match="outside configured roots"):
        settings.authorize_source(outside)


def test_authorize_source_can_return_a_not_yet_existing_path(tmp_path: Path) -> None:
    _, settings, _ = _client(tmp_path)
    candidate = settings.source_roots[0] / "later"
    assert settings.authorize_source(candidate, must_exist=False) == candidate


def test_analyzing_a_missing_mkv_is_not_found(tmp_path: Path) -> None:
    client, settings, _ = _client(tmp_path)
    response = client.get(
        "/api/v1/analyze-mkv",
        params={"path": str(settings.completed_root / "missing.mkv")},
    )
    assert response.status_code == 404


def test_artifact_content_for_a_vanished_file_is_not_found(tmp_path: Path) -> None:
    client, settings, database = _client(tmp_path)
    job = database.create_job(JobCreate(source_path=str(settings.source_roots[0])))
    vanished = settings.job_root(job.id) / "logs" / "gone.png"
    artifact = database.create_artifact(
        ArtifactCreate(
            job_id=job.id,
            kind=ArtifactKind.MANIFEST,
            name="gone.png",
            path=str(vanished),
            mime_type="image/png",
        )
    )
    response = client.get(f"/api/v1/artifacts/{artifact.id}/content")
    assert response.status_code == 404
    assert artifact.id in response.json()["detail"]


def test_unreadable_source_directory_is_a_client_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings, _ = _client(tmp_path)
    locked = settings.source_roots[0] / "locked"
    locked.mkdir()
    real_iterdir = Path.iterdir

    def deny(self: Path):
        if self == locked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", deny)
    response = client.get("/api/v1/sources", params={"path": str(locked)})
    assert response.status_code == 422


def test_analyzing_a_non_mkv_file_is_a_client_error(tmp_path: Path) -> None:
    client, settings, _ = _client(tmp_path)
    target = settings.completed_root / "notes.txt"
    target.write_text("not media", encoding="utf-8")
    response = client.get("/api/v1/analyze-mkv", params={"path": str(target)})
    assert response.status_code == 422
    assert "existing MKV" in response.json()["detail"]


def test_a_failing_media_tool_is_reported_without_leaking_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from bdencode import api as api_module

    class Failing:
        def analyze(self, _path: Path) -> object:
            raise subprocess.CalledProcessError(
                2, ["mkvmerge"], output="", stderr="secret /home/user/path"
            )

    monkeypatch.setattr(api_module, "MkvAnalyzer", Failing)
    client, settings, _ = _client(tmp_path)
    target = settings.completed_root / "movie.mkv"
    target.write_bytes(b"\x1a\x45\xdf\xa3")
    response = client.get("/api/v1/analyze-mkv", params={"path": str(target)})
    assert response.status_code == 422
    assert "secret" not in response.text and "/home/user" not in response.text
