from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

import pytest
from fastapi.testclient import TestClient

from bdencode.api import create_app
from bdencode.config import Settings
from bdencode.db import Database
from bdencode.job_statistics import GIB, compute_job_statistics, encode_seconds, summarize
from bdencode.models import ArtifactCreate, ArtifactKind, JobCreate, JobState
from bdencode.previews import (
    MAX_PREVIEWS_PER_JOB,
    PreviewError,
    PreviewRequest,
    PreviewService,
    parse_media_info,
    preview_command,
    preview_filter,
)
from bdencode.queue import JobQueue


@pytest.fixture
def environment(tmp_path: Path):
    source = tmp_path / "storage"
    source.mkdir()
    settings = Settings(data_root=tmp_path / "encode", source_roots=(source,)).validate()
    settings.create_directories()
    database = Database(settings.resolved_database_path)
    database.initialize()
    return settings, database


def make_completed_job(
    settings: Settings,
    database: Database,
    *,
    name: str = "Movie",
    finished: datetime | None = None,
    source_bytes: int | None = 40 * GIB,
    output_bytes: int | None = 10 * GIB,
    with_evidence: bool = True,
    output_file: Path | None = None,
):
    queue = JobQueue(database)
    job = queue.enqueue(JobCreate(source_path=f"/storage/{name}", name=name))
    finished = finished or datetime(2026, 5, 2, 12, tzinfo=UTC)
    with database._write() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'COMPLETED', started_at = ?, finished_at = ?, "
            "output_path = ? WHERE id = ?",
            (
                (finished - timedelta(hours=5)).isoformat(),
                finished.isoformat(),
                str(output_file) if output_file else None,
                job.id,
            ),
        )
    if output_bytes is not None:
        database.create_artifact(
            ArtifactCreate(
                job_id=job.id,
                kind=ArtifactKind.OUTPUT,
                name=f"{name}.mkv",
                path=str(output_file or settings.completed_root / f"{name}.mkv"),
                mime_type="video/x-matroska",
                size_bytes=output_bytes,
            )
        )
    root = settings.job_root(job.id)
    if with_evidence:
        (root / "analysis").mkdir(parents=True)
        (root / "comparison").mkdir(parents=True)
        if source_bytes is not None:
            (root / "analysis" / "source-size.json").write_text(
                json.dumps({"total_bytes": source_bytes}), encoding="utf-8"
            )
        (root / "manifest.json").write_text(
            json.dumps({"encoder_settings": {"encoder": "x265", "crf": 17.75, "preset": "slow"}}),
            encoding="utf-8",
        )
        (root / "comparison" / "video-metrics.json").write_text(
            json.dumps(
                {
                    "sample_count": 24,
                    "aggregate": {"ssim_all_mean": 0.9871234, "psnr_average_db_mean": 44.567},
                }
            ),
            encoding="utf-8",
        )
        (root / "comparison" / "reference-vapoursynth-info.txt").write_text(
            "Frames: 172627\nFPS: 24000/1001 (23.976 fps)\n", encoding="utf-8"
        )
        (root / "analysis" / "crf-search.json").write_text(
            json.dumps(
                {
                    "status": "converged",
                    "chosen_crf": 17.75,
                    "chosen_score": 95.213,
                    "target_vmaf": 95.0,
                    "probes": [{"crf": 18, "score": 94.9}, {"crf": 17.75, "score": 95.213}],
                }
            ),
            encoding="utf-8",
        )
    base = finished - timedelta(hours=4)
    for kind, moment, state_from, state_to in (
        ("job.state", base, "READY", "ENCODING"),
        ("job.control.paused", base + timedelta(minutes=10), None, None),
        ("job.control.resumed", base + timedelta(minutes=15), None, None),
        ("job.state", base + timedelta(hours=1), "ENCODING", "MUXING"),
    ):
        with database._write() as connection:
            connection.execute(
                "INSERT INTO events (job_id, kind, state_from, state_to, message, "
                "payload_json, created_at) VALUES (?, ?, ?, ?, NULL, '{}', ?)",
                (job.id, kind, state_from, state_to, moment.isoformat()),
            )
    return database.get_job(job.id)


# -- statistics ----------------------------------------------------------------------


def test_job_statistics_combine_size_speed_and_quality(environment) -> None:
    settings, database = environment
    job = make_completed_job(settings, database)

    stats = compute_job_statistics(
        job,
        database.list_events(job_id=job.id),
        database.list_artifacts(job_id=job.id),
        settings.job_root(job.id),
    )

    assert stats["source_bytes"] == 40 * GIB and stats["output_bytes"] == 10 * GIB
    assert stats["saved_bytes"] == 30 * GIB and stats["saved_percent"] == 75.0
    assert stats["encoder"] == "x265" and stats["crf"] == 17.75 and stats["preset"] == "slow"
    assert stats["frames"] == 172627 and stats["media_seconds"] == pytest.approx(7200.0, abs=0.1)
    # One hour in ENCODING minus a five minute pause.
    assert stats["encode_seconds"] == 3300.0
    assert stats["encode_fps"] == pytest.approx(172627 / 3300, abs=0.01)
    assert stats["realtime_factor"] == pytest.approx(7200 / 3300, abs=0.01)
    assert stats["bitrate_kbps"] == pytest.approx(10 * GIB * 8 / 7199.98 / 1000, rel=0.001)
    assert stats["total_seconds"] == 5 * 3600.0
    assert stats["quality"] == {
        "vmaf_sample": 95.21,
        "vmaf_target": 95.0,
        "vmaf_scope": "auto-crf sample encodes",
        "ssim_mean": 0.98712,
        "psnr_mean_db": 44.57,
        "comparison_samples": 24,
    }
    assert stats["auto_crf"] == {"status": "converged", "chosen_crf": 17.75, "probes": 2}


def test_missing_evidence_yields_nulls_not_guesses(environment) -> None:
    settings, database = environment
    job = make_completed_job(
        settings, database, with_evidence=False, source_bytes=None, output_bytes=None
    )
    stats = compute_job_statistics(job, [], [], settings.job_root(job.id))
    for key in (
        "source_bytes",
        "output_bytes",
        "saved_bytes",
        "saved_percent",
        "frames",
        "encode_seconds",
        "encode_fps",
        "bitrate_kbps",
        "auto_crf",
    ):
        assert stats[key] is None, key
    assert stats["quality"]["vmaf_sample"] is None and stats["quality"]["ssim_mean"] is None


def test_encode_seconds_counts_retries_and_ignores_unfinished_stages() -> None:
    class E:
        def __init__(self, id, kind, when, to=None):
            self.id, self.kind, self.created_at, self.state_to = id, kind, when, to

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    minute = timedelta(minutes=1)
    events = [
        E(1, "job.state", t0, JobState.ENCODING),
        E(2, "job.state", t0 + 30 * minute, JobState.FAILED),
        E(3, "job.state", t0 + 40 * minute, JobState.ENCODING),
        E(4, "job.state", t0 + 100 * minute, JobState.MUXING),
    ]
    assert encode_seconds(events) == (30 + 60) * 60
    assert encode_seconds(events[:1]) is None
    assert encode_seconds([]) is None


def test_summary_averages_only_over_available_values() -> None:
    rows = [
        {
            "source_bytes": 40 * GIB,
            "output_bytes": 10 * GIB,
            "crf": 18.0,
            "encoder": "x265",
            "frames": 1000,
            "encode_seconds": 100.0,
            "bitrate_kbps": 8000.0,
            "realtime_factor": 2.0,
            "quality": {"vmaf_sample": 95.0, "ssim_mean": 0.98, "psnr_mean_db": 44.0},
        },
        {
            "source_bytes": 20 * GIB,
            "output_bytes": 15 * GIB,
            "crf": 20.0,
            "encoder": "x264",
            "frames": None,
            "encode_seconds": None,
            "bitrate_kbps": None,
            "realtime_factor": None,
            "quality": {"vmaf_sample": None, "ssim_mean": 0.96, "psnr_mean_db": None},
        },
        {"source_bytes": None, "output_bytes": 5 * GIB, "quality": {}},
    ]
    summary = summarize(rows)
    assert summary["jobs"] == 3 and summary["jobs_with_size_evidence"] == 2
    assert summary["saved_gib"] == 35.0 and summary["saved_percent"] == pytest.approx(58.33, abs=0.01)
    assert summary["total_output_gib"] == 30.0
    assert summary["average_vmaf_sample"] == 95.0
    assert summary["average_ssim"] == pytest.approx(0.97)
    assert summary["average_encode_fps"] == 10.0
    assert summary["encoders"] == {"x265": 1, "x264": 1}
    empty = summarize([])
    assert empty["jobs"] == 0 and empty["saved_percent"] is None and empty["average_vmaf_sample"] is None


def test_statistics_api_lists_completed_jobs_newest_first(environment) -> None:
    settings, database = environment
    older = make_completed_job(settings, database, name="Older", finished=datetime(2026, 1, 1, tzinfo=UTC))
    newer = make_completed_job(settings, database, name="Newer", finished=datetime(2026, 6, 1, tzinfo=UTC))
    JobQueue(database).enqueue(JobCreate(source_path="/storage/Queued", name="Queued"))

    with TestClient(create_app(database, settings=settings)) as client:
        body = client.get("/api/v1/statistics").json()
        assert [row["name"] for row in body["jobs"]] == ["Newer", "Older"]
        assert body["summary"]["jobs"] == 2 and body["summary"]["saved_gib"] == 60.0
        single = client.get(f"/api/v1/jobs/{older.id}/statistics").json()
        assert single["job_id"] == older.id and single["saved_percent"] == 75.0
        assert client.get("/api/v1/jobs/nope/statistics").status_code == 404
        assert client.get("/api/v1/statistics?limit=0").status_code == 422
    assert newer.id != older.id


def test_statistics_limit_keeps_the_most_recently_finished_jobs(environment) -> None:
    settings, database = environment
    for index in range(5):
        make_completed_job(
            settings,
            database,
            name=f"Film{index}",
            finished=datetime(2026, 1, 1 + index, tzinfo=UTC),
        )
    with TestClient(create_app(database, settings=settings)) as client:
        body = client.get("/api/v1/statistics?limit=2").json()
    assert [row["name"] for row in body["jobs"]] == ["Film4", "Film3"]
    assert body["summary"]["jobs"] == 2


# -- previews --------------------------------------------------------------------------------


FFPROBE_DOCUMENT = {
    "format": {"duration": "7200.5"},
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "hevc",
            "width": 3840,
            "height": 2160,
            "pix_fmt": "yuv420p10le",
            "color_transfer": "smpte2084",
        },
        {"codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"language": "eng", "title": "Main"}},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
    ],
    "chapters": [
        {"start_time": "0.000000", "tags": {"title": "Opening"}},
        {"start_time": "1200.250000", "tags": {}},
        {"start_time": "bad"},
    ],
}


class FakeMedia:
    """Plays ffprobe and ffmpeg for the preview service."""

    def __init__(self, document: dict[str, Any] | None = None) -> None:
        self.document = document or FFPROBE_DOCUMENT
        self.calls: list[list[str]] = []
        self.fail_transcode = False

    def __call__(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.calls.append(command)
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, json.dumps(self.document), "")
        if self.fail_transcode:
            return subprocess.CompletedProcess(command, 1, "", "Conversion failed!")
        Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 5000)
        return subprocess.CompletedProcess(command, 0, "", "")


def test_preview_filters_tone_map_only_hdr() -> None:
    assert preview_filter(720, hdr=False) == "scale=-2:720:flags=bicubic,format=yuv420p"
    hdr = preview_filter(480, hdr=True)
    assert hdr.startswith("zscale=t=linear") and "tonemap=tonemap=hable" in hdr
    assert hdr.endswith("scale=-2:480:flags=bicubic,format=yuv420p")


def test_preview_command_is_shell_free_and_browser_safe(tmp_path: Path) -> None:
    request = PreviewRequest(90, 20, 720)
    command = preview_command(
        tmp_path / "film.mkv", tmp_path / "out.mp4", request, hdr=True, has_audio=True
    )
    assert command[0] == "ffmpeg" and command[command.index("-ss") + 1] == "90"
    assert command.index("-ss") < command.index("-i"), "fast input seek"
    assert command[command.index("-t") + 1] == "20"
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-c:a") + 1] == "aac" and "-sn" in command
    assert "+faststart" in command and command[-1].endswith("out.mp4")
    assert command[command.index("-threads") + 1] == "2" and command[command.index("-filter_threads") + 1] == "2"
    silent = preview_command(
        tmp_path / "film.mkv", tmp_path / "out.mp4", request, hdr=False, has_audio=False
    )
    assert "-an" in silent and "-c:a" not in silent and "0:a:0" not in silent


@pytest.mark.parametrize(
    "arguments",
    [(-1, 20, 720), (0, 4, 720), (0, 31, 720), (0, 20, 1080), (0, 20.5, 720), (True, 20, 720)],
)
def test_preview_requests_are_bounded(arguments) -> None:
    with pytest.raises(PreviewError) as error:
        PreviewRequest(*arguments)
    assert error.value.code == "invalid"


def test_media_info_summarizes_tracks_and_chapters() -> None:
    info = parse_media_info(FFPROBE_DOCUMENT)
    assert info["duration_seconds"] == 7200.5
    assert info["video"]["hdr"] is True and info["video"]["width"] == 3840
    assert info["audio"] == [{"codec": "eac3", "channels": 6, "language": "eng", "title": "Main"}]
    assert info["subtitles"] == 1
    assert info["chapters"] == [
        {"start_seconds": 0.0, "title": "Opening"},
        {"start_seconds": 1200.25, "title": "Chapter 2"},
    ]
    assert parse_media_info({})["video"] is None


def test_service_transcodes_once_then_serves_from_cache(tmp_path: Path) -> None:
    media = FakeMedia()
    service = PreviewService(tmp_path / "cache", runner=media)
    source = tmp_path / "film.mkv"
    source.write_bytes(b"mkv")

    record, created = service.ensure("job1", source, PreviewRequest(60, 20, 480))
    assert created and record["start_seconds"] == 60 and record["height"] == 480
    transcodes = [c for c in media.calls if c[0] == "ffmpeg"]
    assert len(transcodes) == 1
    assert "zscale=t=linear" in transcodes[0][transcodes[0].index("-vf") + 1], "HDR source is tone-mapped"
    assert "0:a:0" in transcodes[0]

    again, created_again = service.ensure("job1", source, PreviewRequest(60, 20, 480))
    assert not created_again and again["name"] == record["name"]
    assert len([c for c in media.calls if c[0] == "ffmpeg"]) == 1
    assert [item["name"] for item in service.list("job1")] == [record["name"]]
    assert service.path_for("job1", record["name"]).read_bytes().startswith(b"\x00\x00\x00\x18ftyp")
    assert not list((tmp_path / "cache" / "previews" / "job1").glob(".*partial"))

    service.delete("job1", record["name"])
    assert service.list("job1") == []


def test_service_reports_failures_with_stable_codes(tmp_path: Path) -> None:
    media = FakeMedia()
    service = PreviewService(tmp_path / "cache", runner=media)
    source = tmp_path / "film.mkv"
    source.write_bytes(b"mkv")

    with pytest.raises(PreviewError) as error:
        service.ensure("job1", tmp_path / "missing.mkv", PreviewRequest(0))
    assert error.value.code == "no_output"
    with pytest.raises(PreviewError) as error:
        service.ensure("job1", source, PreviewRequest(9000))
    assert error.value.code == "invalid"

    media.fail_transcode = True
    with pytest.raises(PreviewError) as error:
        service.ensure("job1", source, PreviewRequest(10))
    assert error.value.code == "transcode_failed" and "Conversion failed" in str(error.value)
    assert not list((tmp_path / "cache" / "previews" / "job1").iterdir())

    def missing_tool(*_a: object) -> None:
        raise FileNotFoundError("ffmpeg")

    with pytest.raises(PreviewError) as error:
        PreviewService(tmp_path / "cache", runner=missing_tool).media_info(source)
    assert error.value.code == "unavailable"

    def slow(*_a: object) -> None:
        raise subprocess.TimeoutExpired("ffmpeg", 1)

    with pytest.raises(PreviewError) as error:
        PreviewService(tmp_path / "cache", runner=slow).media_info(source)
    assert error.value.code == "timeout"

    for bad in ("../evil.mp4", "preview-1s-20s-720p-zzzzzzzzzz.mp4", "x"):
        with pytest.raises(PreviewError):
            service.path_for("job1", bad)
    with pytest.raises(PreviewError):
        service.directory("../job")


def test_simultaneous_identical_requests_transcode_only_once(tmp_path: Path) -> None:
    import threading
    import time

    class SlowMedia(FakeMedia):
        def __call__(self, argv, timeout):
            if argv[0] == "ffmpeg":
                time.sleep(0.15)
            return super().__call__(argv, timeout)

    media = SlowMedia()
    service = PreviewService(tmp_path / "cache", runner=media)
    source = tmp_path / "film.mkv"
    source.write_bytes(b"mkv")
    results: list[tuple[dict, bool]] = []

    def request() -> None:
        results.append(service.ensure("job1", source, PreviewRequest(30, 20, 480)))

    threads = [threading.Thread(target=request) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 4
    assert sorted(created for _record, created in results) == [False, False, False, True]
    assert len({record["name"] for record, _created in results}) == 1
    assert len([c for c in media.calls if c[0] == "ffmpeg"]) == 1


def test_previews_per_job_are_bounded(tmp_path: Path) -> None:
    service = PreviewService(tmp_path / "cache", runner=FakeMedia())
    source = tmp_path / "film.mkv"
    source.write_bytes(b"mkv")
    for start in range(MAX_PREVIEWS_PER_JOB + 4):
        service.ensure("job1", source, PreviewRequest(start * 10))
    assert len(service.list("job1")) == MAX_PREVIEWS_PER_JOB


def test_player_api_creates_lists_serves_ranges_and_deletes(environment) -> None:
    settings, database = environment
    film = settings.completed_root / "Movie.mkv"
    film.write_bytes(b"mkv")
    job = make_completed_job(settings, database, output_file=film)

    with TestClient(create_app(database, settings=settings)) as client:
        client.app.state.preview_service = PreviewService(settings.cache_root, runner=FakeMedia())

        info = client.get(f"/api/v1/jobs/{job.id}/player").json()
        assert info["duration_seconds"] == 7200.5 and info["previews"] == []
        assert info["limits"]["max_duration_seconds"] == 30 and len(info["chapters"]) == 2

        created = client.post(
            f"/api/v1/jobs/{job.id}/previews", json={"start_seconds": 120, "duration_seconds": 15, "height": 360}
        )
        assert created.status_code == 201 and created.json()["created"] is True
        name = created.json()["name"]
        again = client.post(f"/api/v1/jobs/{job.id}/previews", json={"start_seconds": 120, "duration_seconds": 15, "height": 360})
        assert again.status_code == 200 and again.json()["created"] is False

        assert [item["name"] for item in client.get(f"/api/v1/jobs/{job.id}/previews").json()["items"]] == [name]
        full = client.get(f"/api/v1/jobs/{job.id}/previews/{name}")
        assert full.status_code == 200 and full.headers["content-type"] == "video/mp4"
        assert full.headers["x-content-type-options"] == "nosniff"
        partial = client.get(f"/api/v1/jobs/{job.id}/previews/{name}", headers={"Range": "bytes=0-9"})
        assert partial.status_code == 206 and len(partial.content) == 10

        assert client.post(f"/api/v1/jobs/{job.id}/previews", json={"height": 1080}).status_code == 422
        assert client.post(f"/api/v1/jobs/{job.id}/previews", json={"start_seconds": 99999}).status_code == 422
        assert client.get(f"/api/v1/jobs/{job.id}/previews/preview-1s-20s-720p-0000000000.mp4").status_code == 404
        assert client.delete(f"/api/v1/jobs/{job.id}/previews/{name}").status_code == 204
        assert client.get(f"/api/v1/jobs/{job.id}/previews/{name}").status_code == 404


def test_player_requires_a_finished_output_inside_the_release_roots(environment, tmp_path: Path) -> None:
    settings, database = environment
    queued = JobQueue(database).enqueue(JobCreate(source_path="/storage/Q", name="Q"))
    outside = tmp_path / "elsewhere.mkv"
    outside.write_bytes(b"mkv")
    escaped = make_completed_job(settings, database, name="Escaped", output_file=outside)

    with TestClient(create_app(database, settings=settings)) as client:
        client.app.state.preview_service = PreviewService(settings.cache_root, runner=FakeMedia())
        assert client.get(f"/api/v1/jobs/{queued.id}/player").status_code == 409
        assert client.get(f"/api/v1/jobs/{escaped.id}/player").status_code == 422
        assert client.get("/api/v1/jobs/none/player").status_code == 404


# -- database status and backups over HTTP ---------------------------------------------------------------


def test_backup_endpoints_create_and_list_verified_backups(environment) -> None:
    settings, database = environment
    with TestClient(create_app(database, settings=settings)) as client:
        status = client.get("/api/v1/system/database").json()
        assert status["schema_version"] == 2 and status["integrity"] == ["ok"]
        assert status["migrations"][0]["kind"] == "create" and status["backup_count"] == 0

        created = client.post("/api/v1/system/backups")
        assert created.status_code == 201
        body = created.json()
        assert body["label"] == "manual" and body["verified"] and len(body["sha256"]) == 64

        listing = client.get("/api/v1/system/backups").json()
        assert [item["name"] for item in listing["items"]] == [body["name"]]
        assert client.get("/api/v1/system/database").json()["latest_backup"]["name"] == body["name"]
        assert (settings.state_root / "backups" / body["name"]).is_file()

    with TestClient(create_app(Database(":memory:"))) as client:
        assert client.post("/api/v1/system/backups").status_code == 422
