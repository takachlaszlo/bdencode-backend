from __future__ import annotations

import json
import os
import re
import shutil
import struct
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Sequence

import pytest
from fastapi.testclient import TestClient

import bdencode.worker as worker_module
from bdencode.api import create_app
from bdencode.config import Settings
from bdencode.db import Database
from bdencode.media.bluray import (
    ContentKind,
    DiscKind,
    DiscScan,
    HdrStaticMetadata,
    MediaStream,
    PlaylistCandidate,
    StreamKind,
    ToolCapabilities,
    VideoCodec,
    VideoProperties,
)
from bdencode.media.profiles import ColorMetadata
from bdencode.models import (
    ArtifactKind,
    ContentType,
    DiscType,
    JobCreate,
    JobState,
    ScanCreate,
    ScanState,
    ScanUpdate,
)
from bdencode.process import ProcessInterrupted
from bdencode.queue import JobQueue
from bdencode.worker import (
    JobPaths,
    PipelineWorker,
    ReviewRequired,
    _FFPROBE_PROFILE_NAMES,
    _current_comparison_pngs,
    parse_selection,
    run_worker,
)


def test_sd_notify_is_noop_outside_systemd(monkeypatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

    def unexpected_socket(*_args: Any, **_kwargs: Any):
        pytest.fail("a notification socket was opened outside systemd")

    monkeypatch.setattr(worker_module.socket, "socket", unexpected_socket)
    assert worker_module._sd_notify("READY=1") is False


def test_sd_notify_sends_one_abstract_unix_datagram(monkeypatch) -> None:
    class FakeNotifier:
        def __init__(self) -> None:
            self.address: str | None = None
            self.payload: bytes | None = None
            self.closed = False

        def connect(self, address: str) -> None:
            self.address = address

        def send(self, payload: bytes) -> int:
            self.payload = payload
            return len(payload)

        def close(self) -> None:
            self.closed = True

    notifier = FakeNotifier()
    created_with: list[tuple[int, int]] = []

    def make_socket(family: int, kind: int) -> FakeNotifier:
        created_with.append((family, kind))
        return notifier

    monkeypatch.setenv("NOTIFY_SOCKET", "@bdencode-worker-test")
    monkeypatch.setattr(worker_module.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(worker_module.socket, "socket", make_socket)

    assert worker_module._sd_notify("READY=1\nSTATUS=Ready") is True
    assert created_with == [(1, worker_module.socket.SOCK_DGRAM)]
    assert notifier.address == "\0bdencode-worker-test"
    assert notifier.payload == b"READY=1\nSTATUS=Ready"
    assert notifier.closed is True
    assert "NOTIFY_SOCKET" not in os.environ


def test_worker_readiness_follows_database_initialization_and_instance_lock(
    tmp_path: Path, monkeypatch
) -> None:
    # A production install intentionally publishes its maintenance marker before
    # running the candidate test suite.  Keep this unit test isolated from that
    # host-level state so its one-shot worker can make progress during install.
    monkeypatch.setattr(
        worker_module,
        "_MAINTENANCE_MARKERS",
        (tmp_path / "no-active-maintenance",),
    )
    source_root = tmp_path / "storage"
    source_root.mkdir()
    settings = Settings(
        data_root=tmp_path / "encode",
        source_roots=(source_root,),
        worker_poll_seconds=0,
    )
    database = Database(tmp_path / "state.sqlite3")
    notifications: list[str] = []

    def notify(message: str) -> bool:
        # This query fails if Database.initialize() has not completed.
        assert database.active_job() is None
        assert settings.state_root.is_dir()
        if os.name == "posix":
            import fcntl

            with (settings.state_root / "worker.lock").open("a+b") as contender:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(
                        contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
        notifications.append(message)
        return True

    monkeypatch.setattr(worker_module, "_sd_notify", notify)

    assert run_worker(database, settings, once=True, poll_interval=0) == 0
    assert notifications == ["READY=1\nSTATUS=Ready; waiting for an encode job"]


@pytest.mark.skipif(os.name != "posix", reason="fcntl worker lock is POSIX-only")
def test_second_worker_never_reports_ready(tmp_path: Path, monkeypatch) -> None:
    import fcntl

    source_root = tmp_path / "storage"
    source_root.mkdir()
    settings = Settings(
        data_root=tmp_path / "encode",
        source_roots=(source_root,),
        worker_poll_seconds=0,
    ).validate()
    settings.create_directories()
    database = Database(tmp_path / "state.sqlite3")
    lock_path = settings.state_root / "worker.lock"

    def unexpected_notify(_message: str) -> bool:
        pytest.fail("a worker without the instance lock reported READY=1")

    monkeypatch.setattr(worker_module, "_sd_notify", unexpected_notify)
    with lock_path.open("a+b") as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_worker(database, settings, once=True, poll_interval=0) == 75


def test_worker_systemd_unit_waits_for_notify_readiness() -> None:
    unit = (
        Path(__file__).parents[1]
        / "deploy"
        / "systemd"
        / "bdencode-worker.service.in"
    ).read_text(encoding="utf-8")

    assert "Type=notify\n" in unit
    assert "NotifyAccess=main\n" in unit
    assert "TimeoutStartSec=120s\n" in unit
    assert "Type=simple\n" not in unit


def test_worker_maintenance_markers_are_fail_closed(
    tmp_path: Path, monkeypatch
) -> None:
    markers = (tmp_path / "install-active", tmp_path / "runtime-active")
    monkeypatch.setattr(worker_module, "_MAINTENANCE_MARKERS", markers)
    assert worker_module._maintenance_active() is False
    markers[0].write_text("active\n", encoding="ascii")
    assert worker_module._maintenance_active() is True


def test_ffprobe_profile_names_match_debian_encoder_outputs() -> None:
    assert _FFPROBE_PROFILE_NAMES["high"] == "High"
    assert _FFPROBE_PROFILE_NAMES["main10"] == "Main 10"
    assert _FFPROBE_PROFILE_NAMES["main12"] == "Rext"


def _png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">IIBBBBB", 1920, 1080, 16, 2, 0, 0, 0)
        + b"\x00\x00\x00\x00"
    )


class FakeScanner:
    def __init__(self, scan: DiscScan) -> None:
        self.result = scan
        self.calls = 0

    def scan(self, source: Path, *, content_kind: ContentKind) -> DiscScan:
        self.calls += 1
        assert source == self.result.source
        assert content_kind is self.result.content_kind
        return self.result


class FakeRunner:
    """Materializes deterministic tiny outputs without invoking media tools."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    @staticmethod
    def _write(path: Path, data: bytes | str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_bytes(data)

    @staticmethod
    def _frames() -> str:
        return json.dumps(
            {
                "frames": [
                    {
                        "media_type": "video",
                        "best_effort_timestamp_time": "0.000",
                        "pict_type": "I",
                        "key_frame": 1,
                    },
                    {
                        "media_type": "video",
                        "best_effort_timestamp_time": "0.040",
                        "pict_type": "P",
                        "key_frame": 0,
                    },
                    {
                        "media_type": "video",
                        "best_effort_timestamp_time": "0.080",
                        "pict_type": "B",
                        "key_frame": 0,
                    },
                ]
            }
        )

    def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: Path | None = None,
        env: Any = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        check: bool = True,
        ok_returncodes: Sequence[int] = (0,),
        timeout: float | None = None,
    ) -> None:
        command = tuple(os.fspath(item) for item in argv)
        self.commands.append(command)
        if stderr_path is not None:
            self._write(stderr_path, "")
        if stdout_path is not None:
            if command[0] == "vspipe" and "--info" in command:
                self._write(stdout_path, "Frames: 3\nFPS: 25/1 (25.000 fps)\n")
            elif (
                command[0] == "ffprobe"
                and "-show_frames" in command
                and stdout_path.name != "ffprobe-video-side-data.json"
            ):
                self._write(stdout_path, self._frames())
            elif command[0] == "mkvmerge" and "--identify" in command:
                self._write(
                    stdout_path,
                    json.dumps(
                        {
                            "container": {"properties": {"title": "Movie.Encode"}},
                            "tracks": [{"id": 0, "type": "video", "properties": {}}],
                            "attachments": [
                                {
                                    "file_name": "encode.log",
                                    "content_type": "text/plain; charset=utf-8",
                                }
                            ],
                        }
                    ),
                )
            elif stdout_path.name == "ffprobe-streams.json":
                self._write(
                    stdout_path,
                    json.dumps(
                        {
                            "streams": [
                                {
                                    "index": 0,
                                    "codec_name": "h264",
                                    "profile": "High",
                                    "codec_type": "video",
                                    "width": 1920,
                                    "height": 1080,
                                    "pix_fmt": "yuv420p",
                                    "color_range": "tv",
                                    "color_space": "bt709",
                                    "color_transfer": "bt709",
                                    "color_primaries": "bt709",
                                    "chroma_location": "left",
                                }
                            ]
                        }
                    ),
                )
            elif stdout_path.name == "ffprobe-video-side-data.json":
                self._write(stdout_path, '{"frames": []}\n')
            elif stdout_path.suffix == ".json":
                self._write(stdout_path, "{}\n")
            else:
                self._write(stdout_path, "mock report\n")

        if command[0] == "mkvextract":
            self._write(Path(command[-1]), "<?xml version='1.0'?><Chapters/>\n")
        elif command[0] == "mkvmerge" and "--output" in command:
            self._write(Path(command[command.index("--output") + 1]), b"mock-mux")
        elif command[0] == "bdencode-vmaf":
            self._write(
                Path(command[command.index("--output") + 1]),
                '{"version":"mock","frames":[{"frameNum":0}]}\n',
            )
        elif command[0] == "ffmpeg" and command[-1] != "-" and stdout_path is None:
            output = Path(command[-1])
            self._write(output, _png() if output.suffix == ".png" else b"mock-media")

    def run_pipeline(
        self,
        commands: Sequence[Sequence[str | os.PathLike[str]]],
        *,
        cwd: Path | None = None,
        stderr_paths: Sequence[Path] | None = None,
        timeout: float | None = None,
        check: bool = True,
        stderr_line_callback: Callable[[str], None] | None = None,
        interrupt_requested: Callable[[], bool] | None = None,
        poll_interval: float = 0.2,
    ) -> None:
        normalized = [
            tuple(os.fspath(item) for item in command) for command in commands
        ]
        self.commands.extend(normalized)
        for path in stderr_paths or ():
            self._write(path, "")
        final = normalized[-1]
        if "libvmaf" in next((item for item in final if "libvmaf" in item), ""):
            filter_value = next(item for item in final if "libvmaf" in item)
            raw = filter_value.split("log_path=", 1)[1].replace("\\:", ":")
            self._write(Path(raw), '{"version":"mock","frames":[]}\n')
        elif any("ssim=stats_file=" in item for item in final):
            graph = next(item for item in final if "ssim=stats_file=" in item)
            for raw in re.findall(r"(?:ssim|psnr)=stats_file=([^\[]+)", graph):
                self._write(Path(raw.replace("\\:", ":")), "n:1 mock-metric\n")
        elif final[-1] != "-":
            output = Path(final[-1])
            self._write(output, _png() if output.suffix == ".png" else b"mock-video")


@pytest.fixture
def context(tmp_path: Path):
    source_root = tmp_path / "storage"
    source = source_root / "Movie"
    source.mkdir(parents=True)
    settings = Settings(
        data_root=tmp_path / "encode",
        source_roots=(source_root,),
        comparison_frames_per_type=1,
        worker_poll_seconds=0,
    ).validate()
    settings.create_directories()
    scan = DiscScan(
        source=source.resolve(),
        disc_kind=DiscKind.BD,
        content_kind=ContentKind.FILM,
        playlists=(
            PlaylistCandidate(
                playlist_id="00001",
                duration_seconds=7200,
                streams=(
                    MediaStream(
                        id="video:4113",
                        index=0,
                        pid=4113,
                        kind=StreamKind.VIDEO,
                        codec="h264",
                        video=VideoProperties(
                            codec=VideoCodec.AVC,
                            width=1920,
                            height=1080,
                            frame_rate="24000/1001",
                            field_order="progressive",
                            bit_depth=8,
                            pixel_format="yuv420p",
                            color_primaries="bt709",
                            color_transfer="bt709",
                            color_matrix="bt709",
                        ),
                    ),
                ),
                recommended=True,
            ),
        ),
        capabilities=ToolCapabilities(),
        fingerprint="a" * 64,
    )
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    scanner = FakeScanner(scan)
    runner = FakeRunner()
    worker = PipelineWorker(
        database,
        settings,
        scanner_factory=lambda _settings: scanner,
        runner_factory=lambda _paths: runner,
    )
    return database, settings, scan, scanner, runner, worker


def _enqueue(database: Database, source: Path):
    return JobQueue(database).enqueue(
        JobCreate(
            source_path=str(source),
            name="Movie",
            disc_type=DiscType.BD,
            content_type=ContentType.FILM,
        )
    )


def _selection(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "playlist_id": "00001",
        "angle": 1,
        "video": {
            "detail_level": "beginner",
            "settings": {},
            "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
            "temporal_filter": "progressive",
        },
        "tracks": [],
        "output_name": "Movie.Encode",
        "upload_images": False,
    }
    value.update(updates)
    return value


def _prepare_encoding(context):
    database, _settings, scan, _scanner, _runner, worker = context
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    awaiting_selection = worker.process_one_stage(claimed)
    ready = database.set_selection(awaiting_selection.id, _selection())
    encoding = worker.process_one_stage(ready)
    assert encoding.state is JobState.ENCODING
    return job, encoding


def test_scan_checkpoint_survives_crash_before_database_transition(
    context, monkeypatch
):
    database, _settings, scan, scanner, runner, worker = context
    job = _enqueue(database, scan.source)
    job = JobQueue(database).claim_next()
    assert job is not None and job.state is JobState.SCANNING
    real_update = database.update_scan

    def crash(*args: Any, **kwargs: Any):
        raise RuntimeError("simulated power loss")

    monkeypatch.setattr(database, "update_scan", crash)
    with pytest.raises(RuntimeError, match="power loss"):
        worker.process_one_stage(job)
    assert scanner.calls == 1
    monkeypatch.setattr(database, "update_scan", real_update)

    restarted = PipelineWorker(
        database,
        worker.settings,
        scanner_factory=lambda _settings: scanner,
        runner_factory=lambda _paths: runner,
    )
    result = restarted.process_one_stage(database.get_job(job.id))
    assert result.state is JobState.AWAITING_SELECTION
    assert scanner.calls == 1


def test_scan_failure_atomically_fails_job_instead_of_looping(context):
    database, settings, scan, scanner, _runner, worker = context

    def fail_scan(*_args, **_kwargs):
        scanner.calls += 1
        raise RuntimeError("unreadable disc")

    scanner.scan = fail_scan
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    result = worker.process_job(claimed)
    assert result.state is JobState.FAILED
    assert scanner.calls == 1
    assert database.list_scans(job_id=job.id)[0].status is ScanState.FAILED
    assert (settings.job_root(job.id) / "work").is_dir()


def test_completed_job_stays_completed_when_work_cleanup_fails(
    context, monkeypatch
):
    database, settings, scan, _scanner, _runner, worker = context

    def fail_cleanup(_path):
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(shutil, "rmtree", fail_cleanup)
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection())

    result = worker.process_job(ready)

    assert result.state is JobState.COMPLETED
    assert (settings.completed_root / "Movie.Encode" / "Movie.Encode.mkv").is_file()
    assert (settings.job_root(job.id) / "work").is_dir()
    warnings = [
        item
        for item in database.list_events(job_id=job.id, limit=1000)
        if item.kind == "job.workspace-cleanup-warning"
    ]
    assert len(warnings) == 1


def test_prepare_checkpoint_skips_reference_remux_after_transition_crash(
    context, monkeypatch
):
    database, _settings, scan, _scanner, runner, worker = context
    job = _enqueue(database, scan.source)
    job = JobQueue(database).claim_next()
    assert job is not None
    worker.process_one_stage(job)
    ready = database.set_selection(job.id, _selection())
    real_advance = worker.queue.advance

    def crash(*args: Any, **kwargs: Any):
        raise RuntimeError("simulated transition crash")

    monkeypatch.setattr(worker.queue, "advance", crash)
    with pytest.raises(RuntimeError, match="transition crash"):
        worker.process_one_stage(ready)
    remux_count = sum(
        command[0] == "ffmpeg" and "-playlist" in command for command in runner.commands
    )
    assert remux_count == 1
    monkeypatch.setattr(worker.queue, "advance", real_advance)

    result = worker.process_one_stage(database.get_job(job.id))
    assert result.state is JobState.ENCODING
    assert (
        sum(
            command[0] == "ffmpeg" and "-playlist" in command
            for command in runner.commands
        )
        == 1
    )


def test_video_encode_promotes_only_successful_temporary_output(context):
    database, settings, _scan, _scanner, _runner, worker = context
    job, encoding = _prepare_encoding(context)
    paths = JobPaths.create(settings, job.id)

    result = worker.process_one_stage(encoding)

    assert result.state is JobState.MUXING
    assert paths.encoded_video.read_bytes() == b"mock-video"
    assert not (paths.work / "video-encoded.partial.mkv").exists()
    assert (paths.stages / "video-encode.json").is_file()
    progress_records = [
        json.loads(line)
        for line in (paths.logs / "video-progress.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert progress_records[-1]["stage_fraction"] == 1.0
    assert database.get_job(job.id).progress == 0.78
    progress_events = [
        event
        for event in database.list_events(job_id=job.id)
        if event.kind == "job.progress"
    ]
    assert [event.payload["milestone_percent"] for event in progress_events] == [
        0,
        100,
    ]


def test_invalid_duration_disables_only_progress_observation(context):
    database, settings, scan, scanner, _runner, worker = context
    scanner.result = replace(
        scan,
        playlists=(replace(scan.playlists[0], duration_seconds=0),),
    )
    job, encoding = _prepare_encoding(context)

    result = worker.process_one_stage(encoding)
    paths = JobPaths.create(settings, job.id)

    assert result.state is JobState.MUXING
    assert paths.encoded_video.is_file()
    assert not (paths.logs / "video-progress.jsonl").exists()


def test_api_cancellation_interrupts_encode_without_failed_transition(context):
    database, settings, _scan, _scanner, _runner, worker = context
    job, encoding = _prepare_encoding(context)

    class CancellingRunner(FakeRunner):
        def run_pipeline(self, commands, **kwargs):
            normalized = [
                tuple(os.fspath(item) for item in command) for command in commands
            ]
            self.commands.extend(normalized)
            self._write(Path(normalized[-1][-1]), b"partial-video")
            worker.queue.cancel(job.id, message="operator cancelled")
            assert kwargs["interrupt_requested"]()
            raise ProcessInterrupted()

    cancelling_runner = CancellingRunner()
    interrupted_worker = PipelineWorker(
        database,
        settings,
        runner_factory=lambda _paths: cancelling_runner,
    )

    result = interrupted_worker.process_job(encoding)
    paths = JobPaths.create(settings, job.id)

    assert result.state is JobState.CANCELLED
    assert result.error is None
    assert not paths.encoded_video.exists()
    assert not (paths.work / "video-encoded.partial.mkv").exists()
    assert not (paths.stages / "video-encode.json").exists()
    assert all(
        event.state_to is not JobState.FAILED
        for event in database.list_events(job_id=job.id)
    )


def test_service_stop_interrupts_encode_and_leaves_stage_resumable(context):
    database, settings, _scan, _scanner, _runner, _worker = context
    job, encoding = _prepare_encoding(context)
    stopping = [False]

    class StoppingRunner(FakeRunner):
        def run_pipeline(self, commands, **kwargs):
            normalized = [
                tuple(os.fspath(item) for item in command) for command in commands
            ]
            self.commands.extend(normalized)
            self._write(Path(normalized[-1][-1]), b"partial-video")
            stopping[0] = True
            assert kwargs["interrupt_requested"]()
            raise ProcessInterrupted()

    stopping_worker = PipelineWorker(
        database,
        settings,
        runner_factory=lambda _paths: StoppingRunner(),
        stop_requested=lambda: stopping[0],
    )

    result = stopping_worker.process_job(encoding)
    paths = JobPaths.create(settings, job.id)

    assert result.state is JobState.ENCODING
    assert result.error is None
    assert not paths.encoded_video.exists()
    assert not (paths.work / "video-encoded.partial.mkv").exists()
    assert not (paths.stages / "video-encode.json").exists()


def test_service_stop_after_video_checkpoint_pauses_before_mux(
    context, monkeypatch
):
    database, settings, _scan, _scanner, runner, worker = context
    job, encoding = _prepare_encoding(context)
    stopping = [False]
    original_write_stage = worker_module._write_stage

    def stop_after_video_marker(marker, inputs, outputs):
        original_write_stage(marker, inputs, outputs)
        if marker.name == "video-encode.json":
            stopping[0] = True

    monkeypatch.setattr(worker_module, "_write_stage", stop_after_video_marker)
    worker.stop_requested = lambda: stopping[0]

    paused = worker.process_job(encoding)
    paths = JobPaths.create(settings, job.id)

    assert paused.state is JobState.ENCODING
    assert paths.encoded_video.is_file()
    assert (paths.stages / "video-encode.json").is_file()
    encode_calls = sum(command[0] == "vspipe" for command in runner.commands)

    stopping[0] = False
    resumed = worker.process_one_stage(paused)

    assert resumed.state is JobState.MUXING
    assert sum(command[0] == "vspipe" for command in runner.commands) == encode_calls


def test_api_cancel_after_video_checkpoint_never_enters_mux(context, monkeypatch):
    database, settings, _scan, _scanner, runner, worker = context
    job, encoding = _prepare_encoding(context)
    original_write_stage = worker_module._write_stage

    def cancel_after_video_marker(marker, inputs, outputs):
        original_write_stage(marker, inputs, outputs)
        if marker.name == "video-encode.json":
            worker.queue.cancel(job.id, message="operator cancelled")

    monkeypatch.setattr(worker_module, "_write_stage", cancel_after_video_marker)

    cancelled = worker.process_job(encoding)
    paths = JobPaths.create(settings, job.id)

    assert cancelled.state is JobState.CANCELLED
    assert paths.encoded_video.is_file()
    assert (paths.stages / "video-encode.json").is_file()
    assert not any(command[0] == "mkvmerge" for command in runner.commands)


def test_language_inference_report_survives_ready_stage_replay(context, monkeypatch):
    database, settings, scan, scanner, runner, worker = context
    audio = MediaStream(
        id="audio:4352",
        index=1,
        pid=4352,
        kind=StreamKind.AUDIO,
        codec="ac3",
        channels=2,
        channel_layout="stereo",
        sample_rate=48000,
    )
    scanner.result = replace(
        scan,
        playlists=(
            replace(
                scan.playlists[0],
                streams=(*scan.playlists[0].streams, audio),
            ),
        ),
    )

    class FakeLanguageRuntime:
        calls = 0

        def infer(self, *_args, **_kwargs):
            self.calls += 1
            return {"consensus": {"iso639_2t": "eng", "confidence": 0.99}}

    language_runtime = FakeLanguageRuntime()
    worker.language_runtime = language_runtime
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    selection = _selection(
        tracks=[{"stream_id": "audio:4352", "action": "copy", "language": None}]
    )
    ready = database.set_selection(job.id, selection)
    real_advance = worker.queue.advance

    def crash(*_args, **_kwargs):
        raise RuntimeError("simulated transition crash")

    monkeypatch.setattr(worker.queue, "advance", crash)
    with pytest.raises(RuntimeError, match="transition crash"):
        worker.process_one_stage(ready)
    monkeypatch.setattr(worker.queue, "advance", real_advance)

    restarted = PipelineWorker(
        database,
        settings,
        scanner_factory=lambda _settings: scanner,
        runner_factory=lambda _paths: runner,
        language_runtime=language_runtime,
    )
    result = restarted.process_one_stage(database.get_job(job.id))
    report = json.loads(
        settings.job_root(job.id)
        .joinpath("analysis", "language-inference.json")
        .read_text(encoding="utf-8")
    )
    assert result.state is JobState.ENCODING
    assert report["resolved_languages"] == {"audio:4352": "eng"}
    assert language_runtime.calls == 1


def test_invalid_selection_pauses_in_needs_review(context):
    database, _settings, scan, _scanner, _runner, worker = context
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, {"playlist_id": "00001"})
    result = worker.process_job(ready)
    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.READY
    assert "missing selection" in (result.status_message or "")


def test_mocked_pipeline_reaches_completed_with_sidecar_comparisons(context):
    database, settings, scan, _scanner, _runner, worker = context
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection())
    stale = settings.job_root(job.id) / "comparison" / "stale-old-encode.png"
    stale.write_bytes(_png())

    result = worker.process_job(ready)

    assert result.state is JobState.COMPLETED
    completed = settings.completed_root / "Movie.Encode"
    assert (completed / "Movie.Encode.mkv").is_file()
    assert (completed / "comparison" / "video-comparison.json").is_file()
    assert (completed / "analysis" / "audio-comparison.json").is_file()
    manifest = json.loads((completed / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["comparison_attached_to_mkv"] is False
    comparison = json.loads(
        (completed / "comparison" / "video-comparison.json").read_text(encoding="utf-8")
    )
    assert comparison["counts"] == {"B": 1, "I": 1, "P": 1}
    assert all("reference_sha256" in pair for pair in comparison["pairs"])
    audio_comparison = json.loads(
        (completed / "analysis" / "audio-comparison.json").read_text(encoding="utf-8")
    )
    assert all(
        "source_spectrum_sha256" in track and "encode_spectrum_sha256" in track
        for track in audio_comparison["tracks"]
    )
    assert len(list((completed / "comparison").glob("*-reference.png"))) == 3
    assert len(list((completed / "comparison").glob("*-encode.png"))) == 3
    assert not (completed / "comparison" / stale.name).exists()
    assert not (settings.job_root(job.id) / "work").exists()
    cleanup = [
        item
        for item in database.list_events(job_id=job.id, limit=1000)
        if item.kind == "job.workspace-cleaned"
    ]
    assert len(cleanup) == 1
    assert cleanup[0].payload["bytes_removed"] > 0
    assert all(
        Path(artifact.path).is_file()
        for artifact in database.list_artifacts(job_id=job.id, limit=1000)
    )

    job_paths = JobPaths.create(settings, job.id)
    tampered = next(job_paths.comparison.glob("*-reference.png"))
    tampered.write_bytes(tampered.read_bytes() + b"tampered")
    with pytest.raises(RuntimeError, match="hash differs"):
        _current_comparison_pngs(job_paths)


def test_mux_chapters_come_from_reviewed_playlist(context):
    database, settings, scan, scanner, runner, worker = context
    scanner.result = replace(
        scan,
        playlists=(replace(scan.playlists[0], chapters=(0.0, 440.08, 1007.6)),),
    )
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection())

    result = worker.process_job(ready)

    assert result.state is JobState.COMPLETED
    assert not any(command[0] == "mkvextract" for command in runner.commands)
    mux_command = next(
        command
        for command in runner.commands
        if command[0] == "mkvmerge" and "--output" in command
    )
    assert "--chapters" in mux_command
    assert not (settings.job_root(job.id) / "work").exists()


def test_mux_omits_chapter_option_when_playlist_has_none(context):
    database, _settings, scan, _scanner, runner, worker = context
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection())

    result = worker.process_job(ready)

    assert result.state is JobState.COMPLETED
    assert not any(command[0] == "mkvextract" for command in runner.commands)
    mux_command = next(
        command
        for command in runner.commands
        if command[0] == "mkvmerge" and "--output" in command
    )
    assert "--chapters" not in mux_command


def test_audio_spectrum_pngs_are_registered_as_spectrogram_artifacts(context):
    database, _settings, scan, scanner, runner, worker = context
    audio = MediaStream(
        id="audio:4352",
        index=1,
        pid=4352,
        kind=StreamKind.AUDIO,
        codec="ac3",
        channels=2,
        channel_layout="stereo",
        sample_rate=48000,
    )
    scanner.result = replace(
        scan,
        playlists=(
            replace(
                scan.playlists[0],
                streams=(*scan.playlists[0].streams, audio),
            ),
        ),
    )

    real_run = runner.run

    def run_with_audio_reports(argv, **kwargs):
        real_run(argv, **kwargs)
        command = tuple(os.fspath(item) for item in argv)
        stdout_path = kwargs.get("stdout_path")
        if stdout_path is None:
            return
        if command[0] == "mkvmerge" and "--identify" in command:
            runner._write(
                stdout_path,
                json.dumps(
                    {
                        "container": {"properties": {"title": "Movie.Encode"}},
                        "tracks": [
                            {"id": 0, "type": "video", "properties": {}},
                            {
                                "id": 1,
                                "type": "audio",
                                "properties": {
                                    "language": "en",
                                    "default_track": False,
                                    "forced_track": False,
                                },
                            },
                        ],
                        "attachments": [
                            {
                                "file_name": "encode.log",
                                "content_type": "text/plain; charset=utf-8",
                            }
                        ],
                    }
                ),
            )
        elif stdout_path.name == "ffprobe-streams.json":
            runner._write(
                stdout_path,
                json.dumps(
                    {
                        "streams": [
                            {
                                "index": 0,
                                "codec_name": "h264",
                                "profile": "High",
                                "codec_type": "video",
                                "width": 1920,
                                "height": 1080,
                                "pix_fmt": "yuv420p",
                                "color_range": "tv",
                                "color_space": "bt709",
                                "color_transfer": "bt709",
                                "color_primaries": "bt709",
                                "chroma_location": "left",
                            },
                            {"index": 1, "codec_name": "ac3", "codec_type": "audio"},
                        ]
                    }
                ),
            )
        elif stdout_path.name.endswith("-probe.json"):
            runner._write(
                stdout_path,
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "audio",
                                "codec_name": "ac3",
                                "sample_rate": "48000",
                                "channels": 2,
                                "channel_layout": "stereo",
                                "nb_samples": "480000",
                                "start_time": "0",
                                "duration": "10",
                            }
                        ]
                    }
                ),
            )

    runner.run = run_with_audio_reports
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(
        job.id,
        _selection(
            tracks=[
                {"stream_id": "audio:4352", "action": "copy", "language": "eng"}
            ]
        ),
    )

    result = worker.process_job(ready)

    assert result.state is JobState.COMPLETED, result.status_message
    spectra = [
        artifact
        for artifact in database.list_artifacts(job_id=job.id, limit=1000)
        if artifact.kind is ArtifactKind.SPECTROGRAM
    ]
    assert {artifact.name for artifact in spectra} == {
        "audio-01-source-spectrum.png",
        "audio-01-encode-spectrum.png",
    }
    assert all(artifact.mime_type == "image/png" for artifact in spectra)
    assert all(artifact.sha256 and len(artifact.sha256) == 64 for artifact in spectra)
    assert all(Path(artifact.path).is_file() for artifact in spectra)
    with TestClient(create_app(database, settings=worker.settings)) as client:
        listed = client.get("/api/v1/artifacts", params={"job_id": job.id})
        assert listed.status_code == 200
        api_spectra = [
            artifact
            for artifact in listed.json()["items"]
            if artifact["kind"] == "SPECTROGRAM"
        ]
        assert {artifact["name"] for artifact in api_spectra} == {
            artifact.name for artifact in spectra
        }
        for artifact in api_spectra:
            content = client.get(f"/api/v1/artifacts/{artifact['id']}/content")
            assert content.status_code == 200
            assert content.headers["content-type"] == "image/png"
            assert content.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_missing_imgbb_credential_becomes_retryable_upload_failure(context):
    database, _settings, scan, _scanner, runner, worker = context
    worker.upload_client_factory = lambda: (_ for _ in ()).throw(
        RuntimeError("credential intentionally unavailable")
    )
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection(upload_images=True))

    result = worker.process_job(ready)

    assert result.state is JobState.UPLOAD_FAILED
    assert result.error is None
    assert any(
        path.name.endswith("-reference.png")
        for path in worker.settings.job_root(job.id)
        .joinpath("comparison")
        .glob("*.png")
    )


def test_parse_selection_rejects_path_traversal(context):
    database, _settings, scan, _scanner, _runner, _worker = context
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    # A Job object with a selection is easiest to obtain through the documented
    # scan boundary; no worker subprocess is needed for this validation test.
    database.create_scan(ScanCreate(job_id=job.id))
    latest = database.list_scans(job_id=job.id, limit=1)[0]
    database.update_scan(
        latest.id,
        ScanUpdate(
            status=ScanState.AWAITING_SELECTION,
            result=scan.to_dict(),
        ),
    )
    ready = database.set_selection(job.id, _selection(output_name="../escape"))
    with pytest.raises(ReviewRequired, match="output_name"):
        parse_selection(ready, scan)


def test_parser_accepts_legacy_top_level_video_fields_and_rejects_conflict(context):
    database, _settings, scan, _scanner, _runner, _worker = context
    job = _enqueue(database, scan.source)
    legacy = _selection()
    video = legacy["video"]
    legacy["crop"] = video.pop("crop")
    legacy["temporal_filter"] = video.pop("temporal_filter")
    video["overrides"] = video.pop("settings")
    parsed = parse_selection(job.model_copy(update={"selection": legacy}), scan)
    assert parsed.temporal_filter.value == "progressive"

    legacy["video"]["crop"] = {"left": 2, "top": 0, "right": 0, "bottom": 0}
    with pytest.raises(ReviewRequired, match="conflict"):
        parse_selection(job.model_copy(update={"selection": legacy}), scan)


def test_selection_propagates_scanned_sdr_color_without_retagging(context):
    database, _settings, scan, _scanner, _runner, _worker = context
    source_stream = scan.playlists[0].video_streams[0]
    sd_stream = replace(
        source_stream,
        video=replace(
            source_stream.video,
            width=720,
            height=576,
            color_primaries="bt470bg",
            color_transfer="bt470bg",
            color_matrix="bt470bg",
            color_range="tv",
            chroma_location="left",
        ),
    )
    sd_scan = replace(
        scan,
        playlists=(replace(scan.playlists[0], streams=(sd_stream,)),),
    )
    job = _enqueue(database, scan.source).model_copy(update={"selection": _selection()})
    parsed = parse_selection(job, sd_scan)
    assert parsed.settings.color.primaries == "bt470bg"
    assert parsed.settings.color.transfer == "bt470bg"
    assert parsed.settings.color.matrix == "bt470bg"

    selection = _selection()
    selection["video"]["settings"] = {
        "color": {
            "primaries": "bt709",
            "transfer": "bt709",
            "matrix": "bt709",
            "range": "limited",
            "chroma_location": "left",
        }
    }
    with pytest.raises(ReviewRequired, match="explicit color conversion"):
        parse_selection(job.model_copy(update={"selection": selection}), sd_scan)


def test_incomplete_hd_sdr_color_requires_explicit_matching_confirmation(context):
    database, _settings, scan, _scanner, _runner, _worker = context
    source_stream = scan.playlists[0].video_streams[0]
    incomplete_stream = replace(
        source_stream,
        video=replace(
            source_stream.video,
            color_primaries=None,
            color_transfer=None,
            color_matrix=None,
            color_range=None,
            chroma_location="left",
        ),
    )
    incomplete_scan = replace(
        scan,
        playlists=(replace(scan.playlists[0], streams=(incomplete_stream,)),),
    )
    job = _enqueue(database, scan.source)
    selection = _selection()

    with pytest.raises(ReviewRequired) as error:
        parse_selection(
            job.model_copy(update={"selection": selection}), incomplete_scan
        )

    assert error.value.details["code"] == "source_color_confirmation_required"
    assert error.value.details["missing_fields"] == [
        "primaries",
        "transfer",
        "matrix",
        "range",
    ]
    assert error.value.details["suggested"] == {
        "primaries": "bt709",
        "transfer": "bt709",
        "matrix": "bt709",
        "range": "limited",
        "chroma_location": "left",
    }

    selection["video"]["settings"]["color"] = error.value.details["suggested"]
    parsed = parse_selection(
        job.model_copy(update={"selection": selection}), incomplete_scan
    )
    assert parsed.settings.color == ColorMetadata()


def test_color_confirmation_cannot_override_known_scan_fields(context):
    database, _settings, scan, _scanner, _runner, _worker = context
    source_stream = scan.playlists[0].video_streams[0]
    partial_stream = replace(
        source_stream,
        video=replace(source_stream.video, color_transfer=None),
    )
    partial_scan = replace(
        scan,
        playlists=(replace(scan.playlists[0], streams=(partial_stream,)),),
    )
    selection = _selection()
    selection["video"]["settings"]["color"] = {
        "primaries": "bt470bg",
        "transfer": "bt709",
        "matrix": "bt709",
        "range": "limited",
        "chroma_location": "left",
    }
    job = _enqueue(database, scan.source)

    with pytest.raises(ReviewRequired) as error:
        parse_selection(job.model_copy(update={"selection": selection}), partial_scan)

    assert error.value.details["code"] == "source_color_confirmation_conflict"
    assert error.value.details["conflicts"] == {
        "primaries": {"scanned": "bt709", "confirmed": "bt470bg"}
    }


def test_manual_hdr10_settings_can_complete_incomplete_uhd_scan(context):
    database, _settings, scan, _scanner, _runner, _worker = context
    source_stream = scan.playlists[0].video_streams[0]
    uhd_stream = replace(
        source_stream,
        codec="hevc",
        video=VideoProperties(
            codec=VideoCodec.HEVC,
            width=3840,
            height=2160,
            frame_rate="24000/1001",
            field_order="progressive",
            bit_depth=10,
            pixel_format="yuv420p10le",
            color_primaries="bt2020",
            color_transfer="smpte2084",
            color_matrix="bt2020nc",
            hdr10=True,
            hdr10_static=HdrStaticMetadata(),
            hdr10_base_layer=True,
        ),
    )
    uhd_scan = replace(
        scan,
        disc_kind=DiscKind.UHD,
        playlists=(replace(scan.playlists[0], streams=(uhd_stream,)),),
    )
    selection = _selection()
    selection["video"]["settings"] = {
        "hdr10": {
            "enabled": True,
            "mastering_display": "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)",
            "max_cll": 1000,
            "max_fall": 400,
        }
    }
    job = _enqueue(database, scan.source).model_copy(update={"selection": selection})

    parsed = parse_selection(job, uhd_scan)

    assert parsed.settings.encoder.value == "x265"
    assert parsed.settings.hdr10.enabled is True
    assert parsed.settings.hdr10.max_cll == 1000
