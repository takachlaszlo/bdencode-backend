from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
from dataclasses import replace
from decimal import Decimal
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
from bdencode.process import CommandRunner, ProcessInterrupted
from bdencode.qc.integrity import require_video_cadence as real_require_video_cadence
from bdencode.qc.image_upload import ImageUploadError, UploadedImage
from bdencode.queue import JobQueue
from bdencode.utils import sha256_file
from bdencode.worker import (
    JobPaths,
    PipelineWorker,
    ReviewRequired,
    _FFPROBE_PROFILE_NAMES,
    _activate_source_log_generation,
    _assert_public_sidecar_safe,
    _current_comparison_pngs,
    _ensure_contained_directory,
    _public_diagnostic_summary,
    _sticky_source_diagnostics,
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
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        notifications.append(message)
        return True

    monkeypatch.setattr(worker_module, "_sd_notify", notify)

    assert run_worker(database, settings, once=True, poll_interval=0) == 0
    assert notifications == ["READY=1\nSTATUS=Ready; waiting for an encode job"]


def test_worker_runs_scan_lane_while_encode_lane_is_occupied(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = tmp_path / "storage"
    source_root.mkdir()
    settings = Settings(
        data_root=tmp_path / "encode",
        source_roots=(source_root,),
        worker_poll_seconds=0.01,
    )
    database = Database(tmp_path / "state.sqlite3")
    queue = JobQueue(database)
    encode_job = queue.enqueue(JobCreate(source_path="/storage/first", name="first"))
    queue.claim_next()
    queue.advance(encode_job.id, JobState.READY)
    scan_job = queue.enqueue(JobCreate(source_path="/storage/second", name="second"))

    encode_started = threading.Event()
    scan_started = threading.Event()
    stop = threading.Event()

    class CoordinatedWorker:
        def __init__(
            self,
            current_database: Database,
            _settings: Settings,
            *,
            stop_requested: Callable[[], bool],
            **_kwargs: Any,
        ) -> None:
            self.database = current_database
            self.queue = JobQueue(current_database)
            self.stop_requested = stop_requested

        def process_job(self, job):
            if job.state is JobState.ENCODING:
                encode_started.set()
                if not scan_started.wait(2):
                    raise RuntimeError("scan lane did not start beside encode lane")
                stop.set()
            elif job.state is JobState.SCANNING:
                if not encode_started.wait(2):
                    raise RuntimeError("encode lane did not start")
                scan_started.set()
                stop.wait(2)
            return self.database.get_job(job.id)

    monkeypatch.setattr(worker_module, "PipelineWorker", CoordinatedWorker)
    monkeypatch.setattr(worker_module, "_maintenance_active", lambda: False)
    monkeypatch.setattr(worker_module, "_sd_notify", lambda _message: True)
    results: list[int] = []
    errors: list[BaseException] = []

    def execute() -> None:
        try:
            results.append(
                run_worker(
                    database,
                    settings,
                    poll_interval=0.01,
                    stop_requested=stop.is_set,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=execute)
    thread.start()
    thread.join(timeout=5)
    stop.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert results == [0]
    assert database.encoding_job().id == encode_job.id
    assert database.preparing_job().id == scan_job.id


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
        Path(__file__).parents[1] / "deploy" / "systemd" / "bdencode-worker.service.in"
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


def test_recorded_output_digest_rejects_same_size_in_place_change(
    tmp_path: Path,
) -> None:
    output = tmp_path / "large-output.mkv"
    marker = tmp_path / "stage.json"
    output.write_bytes(b"first")
    worker_module._write_stage(marker, {}, [output])
    recorded = worker_module._recorded_output_sha256(marker, output)
    assert recorded == worker_module.sha256_file(output)

    output.write_bytes(b"other")
    completed_at = json.loads(marker.read_text(encoding="utf-8"))["completed_at_epoch"]
    changed_ns = int((completed_at + 1) * 1_000_000_000)
    os.utime(output, ns=(changed_ns, changed_ns))
    assert output.stat().st_size == 5
    assert worker_module._recorded_output_sha256(marker, output) is None


def test_comparison_png_allowlist_rejects_external_symlink(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "storage"
    source_root.mkdir()
    settings = Settings(
        data_root=tmp_path / "encode",
        source_roots=(source_root,),
    ).validate()
    settings.create_directories()
    paths = JobPaths.create(settings, "symlink-evidence-test")
    outside = tmp_path / "private.png"
    outside.write_bytes(_png())
    linked = paths.comparison / "proof.png"
    try:
        linked.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")
    (paths.comparison / "video-comparison.json").write_text(
        json.dumps(
            {
                "pairs": [
                    {
                        "reference_png": linked.name,
                        "reference_sha256": sha256_file(outside),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (paths.analysis / "audio-comparison.json").write_text(
        json.dumps({"tracks": []}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="cannot be a link"):
        _current_comparison_pngs(paths)


def test_comparison_evidence_root_rejects_external_directory_link(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "storage"
    source_root.mkdir()
    settings = Settings(
        data_root=tmp_path / "encode",
        source_roots=(source_root,),
    ).validate()
    settings.create_directories()
    paths = JobPaths.create(settings, "directory-link-test")
    external = tmp_path / "external-comparison"
    external.mkdir()
    paths.comparison.rmdir()
    try:
        paths.comparison.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(RuntimeError, match="evidence root cannot be a link"):
        _current_comparison_pngs(paths)


def test_job_paths_reject_precreated_external_job_root_link(tmp_path: Path) -> None:
    source_root = tmp_path / "storage"
    source_root.mkdir()
    settings = Settings(
        data_root=tmp_path / "encode",
        source_roots=(source_root,),
    ).validate()
    settings.create_directories()
    external = tmp_path / "external-job-root"
    external.mkdir()
    linked_root = settings.job_root("linked-job")
    try:
        linked_root.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(ReviewRequired, match="job root cannot be"):
        JobPaths.create(settings, "linked-job")
    assert list(external.iterdir()) == []


@pytest.mark.parametrize(
    "relative_parts",
    [
        ("logs", "source-history", "a" * 64),
        ("work", "video-mux-integrity"),
        ("work", "track-mux-integrity"),
        ("analysis", "container"),
        ("work", "spectrum", "audio-01"),
        ("work", "comparison-metric-frames"),
        ("work", "comparison-native-yuv"),
    ],
)
def test_contained_workspace_directory_rejects_external_component_link(
    tmp_path: Path,
    relative_parts: tuple[str, ...],
) -> None:
    source_root = tmp_path / "storage"
    source_root.mkdir()
    settings = Settings(
        data_root=tmp_path / "encode",
        source_roots=(source_root,),
    ).validate()
    settings.create_directories()
    paths = JobPaths.create(settings, "nested-link-test")
    target = paths.root.joinpath(*relative_parts)
    target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    external = tmp_path / ("external-" + "-".join(relative_parts))
    external.mkdir()
    try:
        target.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(ReviewRequired, match="cannot be created safely"):
        _ensure_contained_directory(
            target,
            root=paths.root,
            description="test workspace directory",
        )
    assert list(external.iterdir()) == []


def test_qc_rejects_external_container_directory_link(context, tmp_path: Path) -> None:
    database, settings, scan, _scanner, _runner, worker = context
    job = _enqueue(database, scan.source)
    current = JobQueue(database).claim_next()
    assert current is not None
    current = worker.process_one_stage(current)
    current = database.set_selection(current.id, _selection())
    while current.state is not JobState.QC:
        current = worker.process_one_stage(current)

    paths = JobPaths.create(settings, job.id)
    external = tmp_path / "external-container-qc"
    external.mkdir()
    linked_container = paths.analysis / "container"
    try:
        linked_container.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    result = worker.process_job(current)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.QC
    assert "container QC directory cannot be created safely" in (
        result.status_message or ""
    )
    assert list(external.iterdir()) == []


@pytest.mark.parametrize("sink", ["container-report", "command-audit"])
def test_qc_rejects_external_command_file_link(
    context,
    tmp_path: Path,
    sink: str,
) -> None:
    database, settings, scan, _scanner, _runner, worker = context
    job = _enqueue(database, scan.source)
    current = JobQueue(database).claim_next()
    assert current is not None
    current = worker.process_one_stage(current)
    current = database.set_selection(current.id, _selection())
    while current.state is not JobState.QC:
        current = worker.process_one_stage(current)

    paths = JobPaths.create(settings, job.id)
    external = tmp_path / f"external-{sink}.txt"
    external.write_text("SENTINEL\n", encoding="utf-8")
    if sink == "container-report":
        container = paths.analysis / "container"
        container.mkdir(mode=0o750, exist_ok=True)
        linked_path = container / "ffprobe-streams.json"
    else:
        linked_path = paths.logs / "commands.jsonl"
    try:
        linked_path.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    if sink == "container-report":

        class LinkCheckingRunner(FakeRunner):
            def run(
                self,
                argv: Sequence[str | os.PathLike[str]],
                **kwargs: Any,
            ) -> Any:
                if kwargs.get("stdout_path") == linked_path:
                    return CommandRunner().run(
                        [sys.executable, "-c", "print('{}')"],
                        stdout_path=linked_path,
                    )
                return super().run(argv, **kwargs)

        protected_runner: Any = LinkCheckingRunner()
    else:
        protected_runner = CommandRunner(linked_path)
    worker._runners[str(paths.root)] = protected_runner

    result = worker.process_job(current)

    assert result.state is JobState.FAILED
    assert external.read_text(encoding="utf-8") == "SENTINEL\n"
    assert linked_path.is_symlink()


def test_source_diagnostics_are_sticky_only_within_one_remux_generation(
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    analysis = tmp_path / "analysis"
    stages = tmp_path / "stages"
    logs.mkdir()
    analysis.mkdir()
    stages.mkdir()
    generation_record = analysis / "source-integrity-generation.json"
    remux_marker = stages / "reference-remux.json"
    generation_a = "a" * 64
    generation_b = "b" * 64

    _activate_source_log_generation(
        logs_root=logs,
        generation_record=generation_record,
        remux_marker=remux_marker,
        generation=generation_a,
    )
    corrupt = logs / "reference-remux.attempt-01.log"
    corrupt.write_text("Packet corrupt in stream 0\n", encoding="utf-8")

    _activate_source_log_generation(
        logs_root=logs,
        generation_record=generation_record,
        remux_marker=remux_marker,
        generation=generation_a,
    )
    assert corrupt.is_file()

    _activate_source_log_generation(
        logs_root=logs,
        generation_record=generation_record,
        remux_marker=remux_marker,
        generation=generation_b,
    )
    assert not corrupt.exists()
    assert (
        logs / "source-history" / generation_a / "reference-remux.attempt-01.log"
    ).is_file()
    assert not list(logs.glob("reference-remux*.log"))
    assert json.loads(generation_record.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "generation": generation_b,
    }


def test_sticky_source_history_blocks_corruption_but_not_transient_environment() -> (
    None
):
    environment = worker_module.classify_media_diagnostics(
        "No space left on device while writing output",
        context="source",
    )
    corruption = worker_module.classify_media_diagnostics(
        "Packet corrupt (stream=0, dts=123)",
        context="source",
    )

    assert _sticky_source_diagnostics(environment) == ()
    assert len(_sticky_source_diagnostics(corruption)) == 1


def test_public_diagnostic_summary_never_contains_raw_source_paths() -> None:
    windows_path = r"C:\Users\private-user\Videos\Secret Title\00001.m2ts"
    posix_path = "/home/private-user/Videos/Secret Title/00001.m2ts"
    diagnostics = worker_module.classify_media_diagnostics(
        f"Error opening input file {windows_path}\nPermission denied: {posix_path}",
        context="source",
    )

    public_text = json.dumps(_public_diagnostic_summary(diagnostics))
    assert windows_path not in public_text
    assert posix_path not in public_text
    assert "private-user" not in public_text
    assert {item["code"] for item in _public_diagnostic_summary(diagnostics)} == {
        "storage_or_io_failure",
        "unclassified_error",
    }


@pytest.mark.parametrize(
    "private_field",
    ["source_path", "job_id", "api_key", "delete_url", "host", "hostname", "cpu"],
)
def test_public_json_private_field_denylist_is_fail_closed(
    tmp_path: Path, private_field: str
) -> None:
    sidecar = tmp_path / "public.json"
    sidecar.write_text(
        json.dumps({"schema_version": 2, private_field: "private-value"}),
        encoding="utf-8",
    )

    with pytest.raises(ReviewRequired, match="private field"):
        _assert_public_sidecar_safe(sidecar)


def _png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">IIBBBBB", 1920, 1080, 16, 2, 0, 0, 0)
        + b"\x00\x00\x00\x00"
    )


FAKE_REFERENCE_FRAMES = 172_627
FAKE_REFERENCE_FPS_NUMERATOR = 24_000
FAKE_REFERENCE_FPS_DENOMINATOR = 1_001


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
        sample_count = 720
        return json.dumps(
            {
                "frames": [
                    {
                        "media_type": "video",
                        "best_effort_timestamp_time": format(
                            presentation_index
                            * FAKE_REFERENCE_FPS_DENOMINATOR
                            / FAKE_REFERENCE_FPS_NUMERATOR,
                            ".6f",
                        ),
                        "pict_type": "IPB"[sample_index % 3],
                        "key_frame": int(sample_index % 3 == 0),
                    }
                    for sample_index in range(sample_count)
                    for presentation_index in (
                        round(
                            sample_index
                            * (FAKE_REFERENCE_FRAMES - 1)
                            / (sample_count - 1)
                        ),
                    )
                ]
            }
        )

    @staticmethod
    def _audio_frames(*, internal_gap: bool = False) -> str:
        middle_pts = "200.032000" if internal_gap else "200.000000"
        return json.dumps(
            {
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "audio",
                        "codec_name": "eac3",
                        "sample_rate": "48000",
                        "time_base": "1/1000",
                        "nb_read_frames": "3",
                    }
                ],
                "frames": [
                    {
                        "media_type": "audio",
                        "stream_index": 0,
                        "pts_time": "0.000000",
                        "nb_samples": 9_600_000,
                        "sample_rate": "48000",
                    },
                    {
                        "media_type": "audio",
                        "stream_index": 0,
                        "pts_time": middle_pts,
                        "nb_samples": 9_600_000,
                        "sample_rate": "48000",
                    },
                    {
                        "media_type": "audio",
                        "stream_index": 0,
                        "pts_time": "400.000000",
                        "nb_samples": 9_648_000,
                        "sample_rate": "48000",
                    },
                ],
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
            if stderr_path.name.endswith("-analysis.log"):
                self._write(
                    stderr_path,
                    """
[Parsed_astats_1] Overall
[Parsed_astats_1] Peak level dB: -1.2
[Parsed_astats_1] Peak count: 2
[Parsed_astats_1] Number of NaNs: 0
[Parsed_astats_1] Number of Infs: 0
[Parsed_astats_1] Number of denormals: 0
[Parsed_ebur128_0] Summary:
[Parsed_ebur128_0]   Integrated loudness:
[Parsed_ebur128_0]     I: -18.0 LUFS
[Parsed_ebur128_0]   Loudness range:
[Parsed_ebur128_0]     LRA: 7.0 LU
[Parsed_ebur128_0]   True peak:
[Parsed_ebur128_0]     Peak: -1.0 dBFS
""",
                )
            if any("cropdetect=" in item for item in command):
                self._write(
                    stderr_path,
                    "\n".join(
                        "[Parsed_cropdetect_0] crop=1920:1080:0:0" for _ in range(30)
                    )
                    + "\n",
                )
        if stdout_path is not None:
            if command[0] == "ffmpeg" and "streamhash" in command:
                stream_type = command[command.index("-map") + 1].split(":")[1]
                self._write(
                    stdout_path,
                    f"0,{stream_type},SHA256=" + ("ab" * 32) + "\n",
                )
            elif command[0] == "vspipe" and "--info" in command:
                self._write(
                    stdout_path,
                    "Frames: 172627\nFPS: 24000/1001 (23.976 fps)\n",
                )
            elif (
                command[0] == "ffprobe"
                and "stream=index,codec_type,start_time" in command
            ):
                media_path = Path(command[-1])
                indexes = range(16) if media_path.name == "reference.mkv" else range(1)
                self._write(
                    stdout_path,
                    json.dumps(
                        {
                            "streams": [
                                {
                                    "index": index,
                                    "codec_type": "video" if index == 0 else "audio",
                                    "start_time": "0.000000",
                                }
                                for index in indexes
                            ]
                        }
                    ),
                )
            elif command[0] == "ffprobe" and "packet=size" in command:
                media_path = Path(command[-1])
                packet_size = 900 if media_path.name == "video-encoded.mkv" else 1000
                self._write(
                    stdout_path,
                    f"{packet_size}\n" * FAKE_REFERENCE_FRAMES,
                )
            elif (
                command[0] == "ffprobe"
                and "packet=pts_time,dts_time,duration_time,size" in command
            ):
                frame_duration = Decimal(1001) / Decimal(24000)
                title_duration = Decimal(FAKE_REFERENCE_FRAMES) * frame_duration
                middle = title_duration / Decimal(2)
                last = title_duration - frame_duration
                self._write(
                    stdout_path,
                    json.dumps(
                        {
                            "packets": [
                                {
                                    "pts_time": "0.000000000",
                                    "dts_time": "0.000000000",
                                    "duration_time": f"{frame_duration:.9f}",
                                    "size": "100",
                                },
                                {
                                    "pts_time": f"{middle:.9f}",
                                    "dts_time": f"{middle:.9f}",
                                    "duration_time": f"{frame_duration:.9f}",
                                    "size": "100",
                                },
                                {
                                    "pts_time": f"{last:.9f}",
                                    "dts_time": f"{last:.9f}",
                                    "duration_time": f"{frame_duration:.9f}",
                                    "size": "100",
                                },
                            ]
                        }
                    ),
                )
            elif (
                command[0] == "ffprobe"
                and "-show_frames" in command
                and "-select_streams" in command
                and command[command.index("-select_streams") + 1].startswith("a:")
            ):
                self._write(stdout_path, self._audio_frames())
            elif (
                command[0] == "ffprobe"
                and "-show_frames" in command
                and "-select_streams" in command
                and command[command.index("-select_streams") + 1].startswith("s:")
            ):
                subtitle_ordinal = int(
                    command[command.index("-select_streams") + 1].split(":", 1)[1]
                )
                self._write(
                    stdout_path,
                    json.dumps(
                        {
                            "streams": [
                                {
                                    "index": subtitle_ordinal + 1,
                                    "codec_type": "subtitle",
                                    "codec_name": "hdmv_pgs_subtitle",
                                }
                            ],
                            "frames": [
                                {
                                    "media_type": "subtitle",
                                    "pts_time": "12.000000",
                                    "start_display_time": 0,
                                    "end_display_time": 2000,
                                    "num_rects": 1,
                                }
                            ],
                        }
                    ),
                )
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
                            "container": {
                                "properties": {"title": "Movie.2026.1080p.BluRay.x264"}
                            },
                            "tracks": [
                                {
                                    "id": 0,
                                    "type": "video",
                                    "properties": {
                                        "default_track": True,
                                        "forced_track": False,
                                    },
                                }
                            ],
                            "attachments": [],
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
                                    "level": 41,
                                    "codec_type": "video",
                                    "start_time": "0.000000",
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
        elif command[0] == "ffmpeg" and any(
            "ssim=stats_file=" in item for item in command
        ):
            graph = next(item for item in command if "ssim=stats_file=" in item)
            matches = re.findall(r"(?:ssim|psnr)=stats_file=([^\[]+)", graph)
            self._write(
                Path(matches[0].replace("\\:", ":")),
                "n:1 Y:0.990 U:0.995 V:0.994 All:0.991 (20.0)\n",
            )
            self._write(
                Path(matches[1].replace("\\:", ":")),
                "n:1 mse_avg:1.0 psnr_avg:44.0 psnr_y:43.0 psnr_u:46.0 psnr_v:45.0\n",
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
def context(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        worker_module,
        "capability_snapshot",
        lambda _names: {"host": {"hostname": "test"}, "tools": {}},
    )
    # FakeRunner intentionally emits a three-packet compact timeline instead
    # of 172,627 records.  Cadence math has exhaustive unit coverage; worker
    # tests exercise the checkpoint/wiring without materializing gigabytes of
    # repetitive JSON across the suite.
    monkeypatch.setattr(
        worker_module,
        "require_video_cadence",
        lambda timeline, *_args, **_kwargs: worker_module.compare_packet_timelines(
            timeline, timeline
        ),
    )
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
        "output_name": "Movie.2026.1080p.BluRay.x264",
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


def test_completed_job_stays_completed_when_work_cleanup_fails(context, monkeypatch):
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
    assert (
        settings.completed_root
        / "Movie.2026.1080p.BluRay.x264"
        / "Movie.2026.1080p.BluRay.x264.mkv"
    ).is_file()
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


def test_prepare_converts_only_bluray_pcm_in_reference_remux(context):
    database, _settings, scan, scanner, runner, worker = context
    audio_streams = (
        MediaStream(
            id="audio:4352",
            index=1,
            pid=4352,
            kind=StreamKind.AUDIO,
            codec="ac3",
        ),
        MediaStream(
            id="audio:4353",
            index=2,
            pid=4353,
            kind=StreamKind.AUDIO,
            codec="pcm_bluray",
            bit_depth=24,
        ),
    )
    scanner.result = replace(
        scan,
        playlists=(
            replace(
                scan.playlists[0],
                streams=(*scan.playlists[0].streams, *audio_streams),
            ),
        ),
    )
    _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    awaiting_selection = worker.process_one_stage(claimed)
    ready = database.set_selection(awaiting_selection.id, _selection())

    result = worker.process_job(ready)

    assert result.state is JobState.NEEDS_REVIEW
    command = next(
        command
        for command in runner.commands
        if command[0] == "ffmpeg" and "-playlist" in command
    )
    assert command[command.index("-c:a:1") + 1] == "pcm_s24le"
    assert "-c:a:0" not in command


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


def test_invalid_duration_requires_review_before_distributed_qc(context):
    database, _settings, scan, scanner, _runner, worker = context
    scanner.result = replace(
        scan,
        playlists=(replace(scan.playlists[0], duration_seconds=0),),
    )
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection())

    result = worker.process_job(ready)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.READY


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


def test_service_stop_after_video_checkpoint_pauses_before_mux(context, monkeypatch):
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


def test_dense_subtitle_declared_forced_fails_closed_before_mux(context):
    database, settings, scan, scanner, runner, worker = context
    subtitle = MediaStream(
        id="subtitle:4608",
        index=1,
        pid=4608,
        kind=StreamKind.SUBTITLE,
        codec="hdmv_pgs_subtitle",
    )
    scanner.result = replace(
        scan,
        playlists=(
            replace(
                scan.playlists[0],
                streams=(*scan.playlists[0].streams, subtitle),
            ),
        ),
    )
    real_run = runner.run

    def run_with_dense_subtitle_probe(argv, **kwargs):
        real_run(argv, **kwargs)
        stdout_path = kwargs.get("stdout_path")
        if stdout_path is None or stdout_path.name != "subtitle-01-probe.json":
            return
        stdout_path.write_text(
            json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "subtitle",
                            "codec_name": "hdmv_pgs_subtitle",
                            "nb_read_packets": "1200",
                            "start_time": "0.000",
                            "duration": "7000.000",
                        }
                    ],
                    "format": {"start_time": "0.000", "duration": "7200.000"},
                }
            ),
            encoding="utf-8",
        )

    runner.run = run_with_dense_subtitle_probe
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(
        job.id,
        _selection(
            schema_version=2,
            tracks=[
                {
                    "stream_id": subtitle.id,
                    "action": "copy",
                    "language": "eng",
                    "subtitle_kind": "forced",
                    "order": 0,
                }
            ],
        ),
    )

    result = worker.process_job(ready)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.MUXING
    assert "classification requires review" in (result.status_message or "")
    assert not any(
        command[0] == "mkvmerge" and "--output" in command
        for command in runner.commands
    )
    probe = json.loads(
        (settings.job_root(job.id) / "analysis" / "subtitle-01-probe.json").read_text(
            encoding="utf-8"
        )
    )
    assert probe["streams"][0]["nb_read_packets"] == "1200"


@pytest.mark.parametrize(
    "failure_mode",
    [
        "decode",
        "stderr",
        "mux_payload",
        "mux_timeline",
        "extract_payload",
        "extract_timeline",
    ],
)
def test_final_subtitle_payload_integrity_is_fail_closed(context, failure_mode):
    database, settings, scan, scanner, _runner, _worker = context
    subtitle = MediaStream(
        id="subtitle:4608",
        index=1,
        pid=4608,
        kind=StreamKind.SUBTITLE,
        codec="hdmv_pgs_subtitle",
    )
    scanner.result = replace(
        scan,
        playlists=(
            replace(
                scan.playlists[0],
                streams=(*scan.playlists[0].streams, subtitle),
            ),
        ),
    )

    class EmptyDecodedSubtitleRunner(FakeRunner):
        def run(self, argv, **kwargs):
            result = super().run(argv, **kwargs)
            command = tuple(os.fspath(item) for item in argv)
            stdout_path = kwargs.get("stdout_path")
            if not isinstance(stdout_path, Path):
                return result
            if (
                command[0] == "ffprobe"
                and "stream=index,codec_type,start_time" in command
                and Path(command[-1]).name == "reference.mkv"
            ):
                self._write(
                    stdout_path,
                    json.dumps(
                        {
                            "streams": [
                                {
                                    "index": 0,
                                    "codec_type": "video",
                                    "start_time": "0.000000",
                                },
                                {
                                    "index": 1,
                                    "codec_type": "subtitle",
                                    "start_time": "0.000000",
                                },
                            ]
                        }
                    ),
                )
            elif stdout_path.name == "subtitle-01-probe.json":
                self._write(
                    stdout_path,
                    json.dumps(
                        {
                            "streams": [
                                {
                                    "codec_type": "subtitle",
                                    "codec_name": "hdmv_pgs_subtitle",
                                    "nb_read_packets": "20",
                                    "start_time": "10.000",
                                    "duration": "6000.000",
                                }
                            ],
                            "format": {
                                "start_time": "10.000",
                                "duration": "6000.000",
                            },
                        }
                    ),
                )
            elif command[0] == "mkvmerge" and "--identify" in command:
                self._write(
                    stdout_path,
                    json.dumps(
                        {
                            "container": {
                                "properties": {"title": "Movie.2026.1080p.BluRay.x264"}
                            },
                            "tracks": [
                                {
                                    "id": 0,
                                    "type": "video",
                                    "properties": {
                                        "default_track": True,
                                        "forced_track": False,
                                    },
                                },
                                {
                                    "id": 1,
                                    "type": "subtitles",
                                    "properties": {
                                        "language": "en",
                                        "track_name": "English Full",
                                        "default_track": False,
                                        "forced_track": False,
                                    },
                                },
                            ],
                            "attachments": [],
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
                                    "level": 41,
                                    "codec_type": "video",
                                    "start_time": "0.000000",
                                    "width": 1920,
                                    "height": 1080,
                                    "pix_fmt": "yuv420p",
                                    "color_range": "tv",
                                    "color_space": "bt709",
                                    "color_transfer": "bt709",
                                    "color_primaries": "bt709",
                                    "chroma_location": "left",
                                },
                                {
                                    "index": 1,
                                    "codec_name": "hdmv_pgs_subtitle",
                                    "codec_type": "subtitle",
                                    "start_time": "0.000000",
                                },
                            ]
                        }
                    ),
                )
            elif (
                stdout_path.name == "track-01-s-final.streamhash"
                and failure_mode == "mux_payload"
            ):
                self._write(
                    stdout_path,
                    "0,s,SHA256=" + ("cd" * 32) + "\n",
                )
            elif (
                stdout_path.name == "track-01-s-final-timeline.json"
                and failure_mode == "mux_timeline"
            ):
                document = json.loads(stdout_path.read_text(encoding="utf-8"))
                document["packets"][1]["pts_time"] = "3601.000000000"
                document["packets"][1]["dts_time"] = "3601.000000000"
                self._write(stdout_path, json.dumps(document))
            elif (
                stdout_path.name == "track-01-s-reference.streamhash"
                and failure_mode == "extract_payload"
            ):
                self._write(
                    stdout_path,
                    "0,s,SHA256=" + ("cd" * 32) + "\n",
                )
            elif (
                stdout_path.name == "track-01-s-reference-timeline.json"
                and failure_mode == "extract_timeline"
            ):
                document = json.loads(stdout_path.read_text(encoding="utf-8"))
                document["packets"][1]["pts_time"] = "3601.000000000"
                document["packets"][1]["dts_time"] = "3601.000000000"
                self._write(stdout_path, json.dumps(document))
            elif (
                stdout_path.name == "subtitle-01-decode.json"
                and failure_mode == "decode"
            ):
                self._write(
                    stdout_path,
                    json.dumps(
                        {
                            "streams": [
                                {
                                    "index": 1,
                                    "codec_type": "subtitle",
                                    "codec_name": "hdmv_pgs_subtitle",
                                }
                            ],
                            "frames": [],
                        }
                    ),
                )
            if (
                stdout_path.name == "subtitle-01-decode.json"
                and failure_mode == "stderr"
            ):
                stderr_path = kwargs.get("stderr_path")
                assert isinstance(stderr_path, Path)
                self._write(stderr_path, "decoder error despite rc=0\n")
            return result

    runner = EmptyDecodedSubtitleRunner()
    worker = PipelineWorker(
        database,
        settings,
        scanner_factory=lambda _settings: scanner,
        runner_factory=lambda _paths: runner,
    )
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(
        job.id,
        _selection(
            schema_version=2,
            tracks=[
                {
                    "stream_id": subtitle.id,
                    "action": "copy",
                    "language": "eng",
                    "subtitle_kind": "full",
                    "order": 0,
                }
            ],
        ),
    )

    result = worker.process_job(ready)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.QC
    if failure_mode in {
        "mux_payload",
        "mux_timeline",
        "extract_payload",
        "extract_timeline",
    }:
        report = json.loads(
            (
                settings.job_root(job.id) / "analysis" / "track-mux-integrity.json"
            ).read_text(encoding="utf-8")
        )
        assert report["status"] == "needs_review"
        if failure_mode == "mux_payload":
            assert "changed a retained audio/subtitle" in (result.status_message or "")
            assert report["tracks"][0]["payloads_match"] is False
        elif failure_mode == "mux_timeline":
            assert "changed a retained audio/subtitle" in (result.status_message or "")
            assert report["tracks"][0]["payloads_match"] is True
            assert report["tracks"][0]["timeline_verdict"]["passed"] is False
        elif failure_mode == "extract_payload":
            assert "copy extraction changed" in (result.status_message or "")
            assert report["tracks"][0]["extraction_payloads_match"] is False
        else:
            assert "copy extraction changed" in (result.status_message or "")
            assert report["tracks"][0]["extraction_payloads_match"] is True
            assert report["tracks"][0]["extraction_timeline_verdict"]["passed"] is False
        assert not any(
            command[0] == "ffprobe"
            and "-show_frames" in command
            and "-select_streams" in command
            and command[command.index("-select_streams") + 1] == "s:0"
            for command in runner.commands
        )
    else:
        assert "subtitle payload decode" in (result.status_message or "")
        decode_command = next(
            command
            for command in runner.commands
            if command[0] == "ffprobe"
            and "-select_streams" in command
            and command[command.index("-select_streams") + 1] == "s:0"
            and "-show_frames" in command
        )
        assert "-err_detect" in decode_command
        assert "-c:s" not in decode_command
        assert "copy" not in decode_command
        report = json.loads(
            (
                settings.job_root(job.id)
                / "analysis"
                / "container"
                / "subtitle-integrity.json"
            ).read_text(encoding="utf-8")
        )
        assert report["status"] == "needs_review"
        expected_error = (
            "error-level diagnostics"
            if failure_mode == "stderr"
            else "zero frames/events"
        )
        assert expected_error in report["tracks"][0]["error"]


@pytest.mark.parametrize(
    "output_name",
    (
        "Movie.Encode",
        "After.We.Fell.2021.COMPLETE.BLURAY-iNTEGRUM.BluRay.x264",
        "ARMOUR_OF_GOD_HONG_KONG_CUT_BD.BluRay.x264",
        "Movie.2026.DTS-HD.MA.1080p.BluRay.x264",
    ),
)
def test_release_name_policy_rejects_source_tags_and_false_codec_claims(
    context, output_name
):
    _database, _settings, scan, _scanner, _runner, _worker = context
    job = _enqueue(_database, scan.source).model_copy(
        update={"selection": _selection(output_name=output_name)}
    )

    with pytest.raises(ReviewRequired, match="output_name"):
        parse_selection(job, scan)


@pytest.mark.parametrize("value", (False, 2.9, "2"))
def test_parse_selection_rejects_coerced_crop_values(context, value: object) -> None:
    database, _settings, scan, _scanner, _runner, _worker = context
    selection = _selection()
    selection["video"]["crop"]["top"] = value
    job = _enqueue(database, scan.source).model_copy(update={"selection": selection})

    with pytest.raises(ReviewRequired, match="non-boolean integers"):
        parse_selection(job, scan)


@pytest.mark.parametrize("value", (True, 1.5, "1", -1))
def test_parse_selection_rejects_coerced_track_order(context, value: object) -> None:
    database, _settings, scan, _scanner, _runner, _worker = context
    selection = _selection(
        tracks=[
            {
                "stream_id": "audio:unknown",
                "action": "omit",
                "order": value,
            }
        ]
    )
    job = _enqueue(database, scan.source).model_copy(update={"selection": selection})

    with pytest.raises(ReviewRequired, match="non-negative integer"):
        parse_selection(job, scan)


def test_mocked_pipeline_reaches_completed_with_sidecar_comparisons(context):
    database, settings, scan, _scanner, runner, worker = context
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection())
    job_paths = JobPaths.create(settings, job.id)
    stale = job_paths.comparison / "stale-old-encode.png"
    stale.write_bytes(_png())
    stale_metric = job_paths.comparison / "video-metrics.ssim.log"
    stale_metric.write_text("legacy full-title metric\n", encoding="utf-8")
    legacy_markers = (
        job_paths.stages / "comparison-frame-probe.json",
        job_paths.stages / "comparison-source-frame-probe.json",
        job_paths.stages / "comparison-metrics.json",
        job_paths.stages / "comparison-old-I-native.json",
        job_paths.stages / "comparison-old-I-sdr.json",
    )
    for marker in legacy_markers:
        marker.write_text("{}\n", encoding="utf-8")

    result = worker.process_job(ready)

    assert result.state is JobState.COMPLETED
    completed = settings.completed_root / "Movie.2026.1080p.BluRay.x264"
    final_output = completed / "Movie.2026.1080p.BluRay.x264.mkv"
    assert final_output.is_file()
    assert (completed / "comparison" / "video-comparison.json").is_file()
    assert (completed / "analysis" / "audio-comparison.json").is_file()
    assert not (completed / "manifest.json").exists()
    assert not (completed / "logs").exists()
    assert {path.name for path in (completed / "analysis").iterdir()} == {
        "audio-comparison.json"
    }
    owner = json.loads((completed / ".bdencode-owner.json").read_text(encoding="utf-8"))
    assert owner == {
        "schema_version": 2,
        "output_name": "Movie.2026.1080p.BluRay.x264",
        "mux_sha256": sha256_file(final_output),
    }
    public_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in completed.rglob("*")
        if path.is_file()
        and path.suffix.casefold() in {".json", ".log", ".bbcode", ".txt"}
    )
    assert job.id not in public_text
    assert '"job_id"' not in public_text
    assert '"source_path"' not in public_text
    assert scan.fingerprint not in public_text
    assert not any(path.name == "encode.log" for path in completed.rglob("*"))
    manifest = json.loads(job_paths.manifest_json.read_text(encoding="utf-8"))
    assert manifest["comparison_attached_to_mkv"] is False
    assert manifest["manifest_registered_separately"] is True
    assert not any(
        artifact["path"] == str(job_paths.manifest_json.resolve())
        for artifact in manifest["artifacts"]
    )
    comparison = json.loads(
        (completed / "comparison" / "video-comparison.json").read_text(encoding="utf-8")
    )
    assert comparison["counts"] == {"B": 8, "I": 8, "P": 8}
    assert comparison["sampling"]["full_title_scan"] is False
    assert comparison["sampling"]["selected_pair_count"] == 24
    assert comparison["frame_completeness"]["status"] == "passed"
    completeness = comparison["frame_completeness"]["verdict"]
    assert completeness["reference_frame_count"] == FAKE_REFERENCE_FRAMES
    assert completeness["encoded_packet_count"] == FAKE_REFERENCE_FRAMES
    assert completeness["packet_count_matches"] is True
    assert completeness["duration_matches"] is True
    assert comparison["metrics"]["backend"] == "ffmpeg-native-yuv-sampled-ssim-psnr"
    assert comparison["metrics"]["sample_count"] == 24
    assert set(comparison["metrics"]["aggregate"]) == {
        "psnr_average_db_mean",
        "ssim_all_mean",
    }
    assert all("reference_sha256" in pair for pair in comparison["pairs"])
    assert comparison["visual_annotation"] == {
        "schema_version": 1,
        "layout": "lossless_png_header_outside_picture_area",
        "fields": [
            "image_role",
            "zero_based_presentation_frame_index",
            "comparison_frame_type",
        ],
        "metrics_use_unannotated_pixels": True,
        "metrics_use_native_yuv_planes": True,
    }
    assert all(
        pair["visual_label"]
        == {
            "reference_role": "SOURCE",
            "encode_role": "ENCODE",
            "frame_index": pair["presentation_index"],
            "frame_index_base": 0,
            "frame_type": pair["category"],
            "source_frame_type_mode": "native_bitstream_type",
        }
        for pair in comparison["pairs"]
    )
    metrics = json.loads(
        (completed / "comparison" / "video-metrics.json").read_text(encoding="utf-8")
    )
    assert metrics["backend"] == "ffmpeg-native-yuv-sampled-ssim-psnr"
    assert metrics["full_title_measurement"] is False
    assert metrics["sample_count"] == 24
    assert metrics["quality_gate"]["status"] == "passed"
    assert set(metrics["aggregate"]) == {
        "psnr_average_db_mean",
        "ssim_all_mean",
    }
    audio_comparison = json.loads(
        (completed / "analysis" / "audio-comparison.json").read_text(encoding="utf-8")
    )
    assert all(
        "source_spectrum_sha256" in track and "encode_spectrum_sha256" in track
        for track in audio_comparison["tracks"]
    )
    assert len(list((completed / "comparison").glob("*-reference.png"))) == 24
    assert len(list((completed / "comparison").glob("*-encode.png"))) == 24
    assert not (completed / "comparison" / "encoded-frames.json").exists()
    assert not (completed / "comparison" / "source-frames.json").exists()
    assert not (completed / "comparison" / "sampled-encoded-frames.json").exists()
    assert not (completed / "comparison" / "sampled-source-frames.json").exists()
    assert not (completed / "comparison" / "sampled-encoded-origin.json").exists()
    assert not (completed / "comparison" / "sampled-source-origin.json").exists()
    assert not (completed / "comparison" / stale_metric.name).exists()
    assert all(not marker.exists() for marker in legacy_markers)
    assert not any(command[0] == "bdencode-vmaf" for command in runner.commands)
    sampled_probes = [
        command
        for command in runner.commands
        if command[0] == "ffprobe"
        and "-show_frames" in command
        and str(settings.job_root(job.id) / "work") in " ".join(command)
    ]
    assert sampled_probes
    assert all("-read_intervals" in command for command in sampled_probes)
    encoded_png_commands = [
        command
        for command in runner.commands
        if command[0] == "ffmpeg"
        and "comparison-metric-frames" in command[-1]
        and command[-1].endswith("-encode-native.png")
    ]
    assert len(encoded_png_commands) == 24
    assert all(
        command.index("-ss") < command.index("-i") for command in encoded_png_commands
    )
    assert all(
        "select=eq(n" not in " ".join(command) for command in encoded_png_commands
    )
    annotation_commands = [
        command
        for command in runner.commands
        if command[0] == "ffmpeg"
        and command[-1].endswith(("-reference.png", "-encode.png"))
        and "drawtext=" in command[command.index("-vf") + 1]
    ]
    assert len(annotation_commands) == 48
    assert all(
        "pad=iw:ih+max(40\\,ih/16)" in command[command.index("-vf") + 1]
        and "format=rgb48be" in command[command.index("-vf") + 1]
        for command in annotation_commands
    )
    for pair in comparison["pairs"]:
        frame_text = f"0-BASED INDEX {pair['presentation_index']:09d}"
        reference_command = next(
            command
            for command in annotation_commands
            if command[-1].endswith(pair["reference_png"])
        )
        encode_command = next(
            command
            for command in annotation_commands
            if command[-1].endswith(pair["encode_png"])
        )
        assert (
            f"SOURCE | {frame_text} | {pair['category']}-FRAME"
            in reference_command[reference_command.index("-vf") + 1]
        )
        assert (
            f"ENCODE | {frame_text} | {pair['category']}-FRAME"
            in encode_command[encode_command.index("-vf") + 1]
        )
    metric_commands = [
        command
        for command in runner.commands
        if command[0] == "ffmpeg"
        and any("ssim=stats_file=" in value for value in command)
    ]
    assert len(metric_commands) == 24
    assert all(
        "comparison-native-yuv" in command[command.index("-i") + 1]
        for command in metric_commands
    )
    mux_command = next(
        command
        for command in runner.commands
        if command[0] == "mkvmerge" and "--output" in command
    )
    assert "--attach-file" not in mux_command
    assert "--global-tags" not in mux_command
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
    artifacts = database.list_artifacts(job_id=job.id, limit=1000)
    assert any(
        artifact.kind is ArtifactKind.MANIFEST
        and Path(artifact.path) == job_paths.manifest_json
        for artifact in artifacts
    )
    assert not any(Path(artifact.path).suffix == ".mks" for artifact in artifacts)

    tampered = next(job_paths.comparison.glob("*-reference.png"))
    tampered.write_bytes(tampered.read_bytes() + b"tampered")
    with pytest.raises(RuntimeError, match="hash differs"):
        _current_comparison_pngs(job_paths)


def test_comparison_rejects_truncated_encoded_video_before_sampling(context):
    database, settings, scan, scanner, _runner, _worker = context

    class TruncatedVideoRunner(FakeRunner):
        def run(self, argv, **kwargs):
            result = super().run(argv, **kwargs)
            stdout_path = kwargs.get("stdout_path")
            if (
                isinstance(stdout_path, Path)
                and stdout_path.name == "final-video-packet-sizes.csv"
            ):
                self._write(stdout_path, "900\n" * (FAKE_REFERENCE_FRAMES - 1))
            return result

    runner = TruncatedVideoRunner()
    worker = PipelineWorker(
        database,
        settings,
        scanner_factory=lambda _settings: scanner,
        runner_factory=lambda _paths: runner,
    )
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection())

    result = worker.process_job(ready)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.COMPARISON
    assert "completeness" in (result.status_message or "")
    report = json.loads(
        (
            settings.job_root(job.id) / "analysis" / "video-frame-completeness.json"
        ).read_text(encoding="utf-8")
    )
    assert report["status"] == "needs_review"
    assert "packet count does not match" in report["error"]
    assert not any(
        command[0] == "ffprobe"
        and "-show_frames" in command
        and "sampled-encoded" in " ".join(command)
        for command in runner.commands
    )


def test_comparison_rejects_wrong_final_video_timeline_duration(context):
    database, settings, scan, scanner, _runner, _worker = context

    class RetimedVideoRunner(FakeRunner):
        def run(self, argv, **kwargs):
            result = super().run(argv, **kwargs)
            stdout_path = kwargs.get("stdout_path")
            if isinstance(stdout_path, Path) and stdout_path.name in {
                "encoded-video-timeline.json",
                "final-video-timeline.json",
            }:
                document = json.loads(stdout_path.read_text(encoding="utf-8"))
                document["packets"][1]["pts_time"] = "1800.000000000"
                document["packets"][1]["dts_time"] = "1800.000000000"
                document["packets"][2]["pts_time"] = "3600.000000000"
                document["packets"][2]["dts_time"] = "3600.000000000"
                self._write(stdout_path, json.dumps(document))
            return result

    runner = RetimedVideoRunner()
    worker = PipelineWorker(
        database,
        settings,
        scanner_factory=lambda _settings: scanner,
        runner_factory=lambda _paths: runner,
    )
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection())

    result = worker.process_job(ready)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.COMPARISON
    assert "completeness" in (result.status_message or "")
    report = json.loads(
        (
            settings.job_root(job.id) / "analysis" / "video-frame-completeness.json"
        ).read_text(encoding="utf-8")
    )
    assert report["status"] == "needs_review"
    assert "packet-timeline duration" in report["error"]


def test_comparison_rejects_irregular_final_video_cadence(context, monkeypatch):
    database, settings, scan, scanner, _runner, _worker = context
    scanner.result = replace(
        scan,
        playlists=(replace(scan.playlists[0], duration_seconds=3),),
    )
    monkeypatch.setattr(
        worker_module, "require_video_cadence", real_require_video_cadence
    )

    class IrregularCadenceRunner(FakeRunner):
        def run(self, argv, **kwargs):
            result = super().run(argv, **kwargs)
            command = tuple(os.fspath(item) for item in argv)
            stdout_path = kwargs.get("stdout_path")
            if not isinstance(stdout_path, Path):
                return result
            if command[0] == "vspipe" and "--info" in command:
                self._write(stdout_path, "Frames: 3\nFPS: 1/1 (1.000 fps)\n")
            elif command[0] == "ffprobe" and "packet=size" in command:
                packet_size = (
                    "900" if Path(command[-1]).name == "video-encoded.mkv" else "1000"
                )
                self._write(stdout_path, f"{packet_size}\n" * 3)
            elif (
                command[0] == "ffprobe"
                and "packet=pts_time,dts_time,duration_time,size" in command
                and "video" in stdout_path.name
            ):
                self._write(
                    stdout_path,
                    json.dumps(
                        {
                            "packets": [
                                {
                                    "pts_time": "0.000",
                                    "dts_time": "0.000",
                                    "duration_time": "1.000",
                                    "size": "100",
                                },
                                {
                                    "pts_time": "1.500",
                                    "dts_time": "1.500",
                                    "duration_time": "1.000",
                                    "size": "100",
                                },
                                {
                                    "pts_time": "2.000",
                                    "dts_time": "2.000",
                                    "duration_time": "1.000",
                                    "size": "100",
                                },
                            ]
                        }
                    ),
                )
            return result

    runner = IrregularCadenceRunner()
    worker = PipelineWorker(
        database,
        settings,
        scanner_factory=lambda _settings: scanner,
        runner_factory=lambda _paths: runner,
    )
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection())

    result = worker.process_job(ready)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.COMPARISON
    report = json.loads(
        (
            settings.job_root(job.id) / "analysis" / "video-frame-completeness.json"
        ).read_text(encoding="utf-8")
    )
    assert report["status"] == "needs_review"
    assert "CFR grid" in report["error"]


def test_finalize_publishes_only_manifest_pinned_metric_and_spectrum_files(
    context,
):
    database, settings, scan, _scanner, _runner, worker = context
    job = _enqueue(database, scan.source)
    current = JobQueue(database).claim_next()
    assert current is not None
    current = worker.process_one_stage(current)
    current = database.set_selection(current.id, _selection())
    for _stage in range(8):
        if current.state is JobState.UPLOADING:
            break
        current = worker.process_one_stage(current)
    assert current.state is JobState.UPLOADING

    paths = JobPaths.create(settings, job.id)
    private_metric = paths.comparison / "private.ssim.log"
    private_spectrum = paths.analysis / "audio-secret-spectrum.png"
    private_metric.write_text(
        r"private source C:\Users\operator\Videos\Title" + "\n",
        encoding="utf-8",
    )
    private_spectrum.write_bytes(_png())

    result = worker.process_job(current)

    assert result.state is JobState.COMPLETED
    completed = settings.completed_root / "Movie.2026.1080p.BluRay.x264"
    assert not (completed / "comparison" / private_metric.name).exists()
    assert not (completed / "analysis" / private_spectrum.name).exists()


def test_finalize_rejects_precreated_output_symlink(context, tmp_path: Path):
    database, settings, scan, _scanner, _runner, worker = context
    job = _enqueue(database, scan.source)
    current = JobQueue(database).claim_next()
    assert current is not None
    current = worker.process_one_stage(current)
    current = database.set_selection(current.id, _selection())
    for _stage in range(8):
        if current.state is JobState.UPLOADING:
            break
        current = worker.process_one_stage(current)
    assert current.state is JobState.UPLOADING

    paths = JobPaths.create(settings, job.id)
    completed = settings.completed_root / "Movie.2026.1080p.BluRay.x264"
    completed.mkdir()
    (completed / ".bdencode-owner.json").write_text(
        json.dumps({"schema_version": 1, "job_id": job.id}),
        encoding="utf-8",
    )
    external = tmp_path / "external-final.mkv"
    external.write_bytes(paths.muxed_output.read_bytes())
    external_before = external.read_bytes()
    linked_output = completed / "Movie.2026.1080p.BluRay.x264.mkv"
    try:
        linked_output.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    result = worker.process_job(current)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.UPLOADING
    assert "cannot be a link" in (result.status_message or "")
    assert linked_output.is_symlink()
    assert external.read_bytes() == external_before


def test_finalize_rejects_external_completed_root_link(context, tmp_path: Path):
    database, settings, scan, _scanner, _runner, worker = context
    _enqueue(database, scan.source)
    current = JobQueue(database).claim_next()
    assert current is not None
    current = worker.process_one_stage(current)
    current = database.set_selection(current.id, _selection())
    for _stage in range(8):
        if current.state is JobState.UPLOADING:
            break
        current = worker.process_one_stage(current)
    assert current.state is JobState.UPLOADING

    settings.completed_root.rmdir()
    external = tmp_path / "external-completed-root"
    external.mkdir()
    try:
        settings.completed_root.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    result = worker.process_job(current)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.UPLOADING
    assert "completed root cannot be" in (result.status_message or "")
    assert list(external.iterdir()) == []


@pytest.mark.parametrize("failure_mode", ["payload", "timeline"])
def test_qc_rejects_video_changed_by_final_mux(context, failure_mode):
    database, settings, scan, scanner, _runner, _worker = context

    class MutatedMuxRunner(FakeRunner):
        def run(self, argv, **kwargs):
            result = super().run(argv, **kwargs)
            stdout_path = kwargs.get("stdout_path")
            if (
                isinstance(stdout_path, Path)
                and stdout_path.name == "final-video.streamhash"
                and failure_mode == "payload"
            ):
                self._write(
                    stdout_path,
                    "0,v,SHA256=" + ("cd" * 32) + "\n",
                )
            elif (
                isinstance(stdout_path, Path)
                and stdout_path.name == "final-video-timeline.json"
                and failure_mode == "timeline"
            ):
                document = json.loads(stdout_path.read_text(encoding="utf-8"))
                document["packets"][1]["pts_time"] = "3601.000000000"
                document["packets"][1]["dts_time"] = "3601.000000000"
                self._write(stdout_path, json.dumps(document))
            return result

    runner = MutatedMuxRunner()
    worker = PipelineWorker(
        database,
        settings,
        scanner_factory=lambda _settings: scanner,
        runner_factory=lambda _paths: runner,
    )
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection())

    result = worker.process_job(ready)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.QC
    assert "changed the compressed video" in (result.status_message or "")
    report = json.loads(
        (settings.job_root(job.id) / "analysis" / "video-mux-integrity.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["status"] == "needs_review"
    if failure_mode == "payload":
        assert report["payloads_match"] is False
    else:
        assert report["payloads_match"] is True
        assert report["timeline_verdict"]["passed"] is False
    assert not (
        settings.job_root(job.id) / "stages" / "video-mux-integrity.json"
    ).exists()


def test_fast_comparison_timeout_requests_review_without_losing_resume_stage(context):
    database, _settings, scan, _scanner, runner, worker = context
    real_run = runner.run

    def timeout_sample_probe(argv, **kwargs):
        command = tuple(os.fspath(item) for item in argv)
        if (
            command[0] == "ffprobe"
            and kwargs.get("stdout_path") is not None
            and kwargs["stdout_path"].name == "sampled-encoded-frames.json"
        ):
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 1))
        return real_run(argv, **kwargs)

    runner.run = timeout_sample_probe
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection())

    result = worker.process_job(ready)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.COMPARISON
    assert "bounded" in (result.status_message or "")


def test_upload_fails_closed_for_legacy_unannotated_comparison(context):
    database, settings, scan, _scanner, _runner, worker = context
    job = _enqueue(database, scan.source)
    current = JobQueue(database).claim_next()
    assert current is not None
    current = worker.process_one_stage(current)
    current = database.set_selection(current.id, _selection())
    for _stage in range(8):
        if current.state is JobState.UPLOADING:
            break
        current = worker.process_one_stage(current)
    assert current.state is JobState.UPLOADING

    paths = JobPaths.create(settings, job.id)
    manifest_path = paths.comparison / "video-comparison.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    manifest.pop("visual_annotation", None)
    for pair in manifest["pairs"]:
        pair.pop("visual_label", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = worker.process_job(current)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.UPLOADING
    assert "required visible SOURCE/ENCODE" in (result.status_message or "")
    assert result.error is None
    assert paths.work.is_dir()
    assert not (settings.completed_root / "Movie.2026.1080p.BluRay.x264").exists()


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


def test_mkvmerge_warning_never_creates_a_resumable_success_marker(context):
    database, settings, scan, scanner, _runner, _worker = context

    class WarningMuxRunner(FakeRunner):
        def run(self, argv, **kwargs):
            super().run(argv, **kwargs)
            command = tuple(os.fspath(item) for item in argv)
            return subprocess.CompletedProcess(
                command,
                1 if command[0] == "mkvmerge" and "--output" in command else 0,
            )

    runner = WarningMuxRunner()
    worker = PipelineWorker(
        database,
        settings,
        scanner_factory=lambda _settings: scanner,
        runner_factory=lambda _paths: runner,
    )
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection())

    first = worker.process_job(ready)
    paths = JobPaths.create(settings, job.id)
    assert first.state is JobState.NEEDS_REVIEW
    assert first.resume_state is JobState.MUXING
    assert not (paths.stages / "mux.json").exists()

    resumed = worker.queue.resume_review(job.id)
    second = worker.process_job(resumed)

    assert second.state is JobState.NEEDS_REVIEW
    assert second.resume_state is JobState.MUXING
    assert not (paths.stages / "mux.json").exists()
    assert (
        sum(
            command[0] == "mkvmerge" and "--output" in command
            for command in runner.commands
        )
        == 2
    )


def test_mkvmerge_identify_warning_blocks_every_qc_resume(context):
    database, settings, scan, scanner, _runner, _worker = context

    class WarningIdentifyRunner(FakeRunner):
        def run(self, argv, **kwargs):
            super().run(argv, **kwargs)
            command = tuple(os.fspath(item) for item in argv)
            return subprocess.CompletedProcess(
                command,
                1 if command[0] == "mkvmerge" and "--identify" in command else 0,
            )

    runner = WarningIdentifyRunner()
    worker = PipelineWorker(
        database,
        settings,
        scanner_factory=lambda _settings: scanner,
        runner_factory=lambda _paths: runner,
    )
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection())

    first = worker.process_job(ready)
    paths = JobPaths.create(settings, job.id)
    identify_marker = paths.stages / "qc-mkvmerge-identify.json"
    assert first.state is JobState.NEEDS_REVIEW
    assert first.resume_state is JobState.QC
    assert not identify_marker.exists()

    resumed = worker.queue.resume_review(job.id)
    second = worker.process_job(resumed)

    assert second.state is JobState.NEEDS_REVIEW
    assert second.resume_state is JobState.QC
    assert not identify_marker.exists()
    assert (
        sum(
            command[0] == "mkvmerge" and "--identify" in command
            for command in runner.commands
        )
        == 2
    )


def test_audio_spectrum_pngs_are_registered_as_spectrogram_artifacts(context):
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

    real_run = runner.run
    spectrum_invocations: list[tuple[tuple[str, ...], Path | None]] = []

    def run_with_audio_reports(argv, **kwargs):
        command = tuple(os.fspath(item) for item in argv)
        if command[0] == "ffmpeg" and any(
            "showspectrumpic" in item for item in command
        ):
            spectrum_invocations.append((command, kwargs.get("stderr_path")))
        real_run(argv, **kwargs)
        stdout_path = kwargs.get("stdout_path")
        if stdout_path is None:
            return
        if command[0] == "ffprobe" and "stream=index,codec_type,start_time" in command:
            media_name = Path(command[-1]).name
            if media_name == "reference.mkv":
                streams = [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "start_time": "0.040000",
                    },
                    {
                        "index": 1,
                        "codec_type": "audio",
                        "start_time": "0.040000",
                    },
                ]
            else:
                streams = [
                    {
                        "index": 0,
                        "codec_type": (
                            "video" if media_name == "video-encoded.mkv" else "audio"
                        ),
                        "start_time": (
                            "0.000000"
                            if media_name == "video-encoded.mkv"
                            else "0.040000"
                        ),
                    }
                ]
            runner._write(stdout_path, json.dumps({"streams": streams}))
        elif command[0] == "mkvmerge" and "--identify" in command:
            runner._write(
                stdout_path,
                json.dumps(
                    {
                        "container": {
                            "properties": {"title": "Movie.2026.1080p.BluRay.x264"}
                        },
                        "tracks": [
                            {
                                "id": 0,
                                "type": "video",
                                "properties": {
                                    "default_track": True,
                                    "forced_track": False,
                                },
                            },
                            {
                                "id": 1,
                                "type": "audio",
                                "properties": {
                                    "language": "en",
                                    "track_name": "English Original Mix",
                                    "default_track": True,
                                    "forced_track": False,
                                },
                            },
                        ],
                        "attachments": [],
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
                                "level": 41,
                                "codec_type": "video",
                                "start_time": "0.000000",
                                "width": 1920,
                                "height": 1080,
                                "pix_fmt": "yuv420p",
                                "color_range": "tv",
                                "color_space": "bt709",
                                "color_transfer": "bt709",
                                "color_primaries": "bt709",
                                "chroma_location": "left",
                            },
                            {
                                "index": 1,
                                "codec_name": "ac3",
                                "codec_type": "audio",
                                "start_time": "0.000000",
                            },
                        ]
                    }
                ),
            )
        elif stdout_path.name.endswith("-probe.json"):
            start_time = (
                "0.040000" if "source-probe" in stdout_path.name else "0.000000"
            )
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
                                "nb_samples": "28848000",
                                "start_time": start_time,
                                "duration": "601",
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
            tracks=[{"stream_id": "audio:4352", "action": "copy", "language": "eng"}]
        ),
    )
    paths = JobPaths.create(settings, job.id)

    result = worker.process_job(ready)

    assert result.state is JobState.COMPLETED, result.status_message
    timeline = json.loads(paths.timeline_json.read_text(encoding="utf-8"))
    assert timeline["origin_seconds"] == "0.040000"
    assert timeline["track_sync_offsets_ms"] == ["-40.000000"]
    mux_command = next(
        command
        for command in runner.commands
        if command[0] == "mkvmerge" and "--output" in command
    )
    assert "0:-40" in mux_command
    audio_report = json.loads(
        (paths.analysis / "audio-comparison.json").read_text(encoding="utf-8")
    )["tracks"][0]
    assert audio_report["source_probe"]["start_time"] == "0.040000"
    assert audio_report["source_timeline_probe"]["start_time"] == "0.000000"
    assert audio_report["encode_probe"]["start_time"] == "0.000000"
    assert audio_report["comparison"]["delay_seconds"] == "0.000000"
    pcm_decode_commands = [
        command
        for command in runner.commands
        if command[0] == "ffmpeg"
        and "-c:a" in command
        and command[command.index("-c:a") + 1] == "pcm_s32le"
    ]
    analysis_commands = [
        command
        for command in runner.commands
        if command[0] == "ffmpeg" and "-af" in command
    ]
    assert len(pcm_decode_commands) == 2
    assert len(analysis_commands) == 2
    assert all("-drc_scale" in command for command in pcm_decode_commands)
    assert all("-drc_scale" in command for command in analysis_commands)
    track_mux = json.loads(
        (paths.analysis / "track-mux-integrity.json").read_text(encoding="utf-8")
    )
    assert track_mux["status"] == "passed"
    assert track_mux["tracks"][0]["stream_type"] == "a"
    assert track_mux["tracks"][0]["payloads_match"] is True
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
    assert len(spectrum_invocations) == 6
    for command, stderr_path in spectrum_invocations:
        output_path = Path(command[-1])
        assert "-drc_scale" in command
        assert Decimal(command[command.index("-t") + 1]) <= Decimal("300")
        assert stderr_path is not None
        assert stderr_path != output_path
        assert stderr_path.parent.name == "logs"
        assert stderr_path.name == f"{output_path.stem}.stderr"
    stitch_commands = [
        command
        for command in runner.commands
        if command[0] == "ffmpeg" and any("vstack=inputs=3" in item for item in command)
    ]
    assert len(stitch_commands) == 2
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


@pytest.mark.parametrize("internal_final_audio_gap", [False, True])
def test_lossy_audio_transcode_uses_target_qc_without_pcm_hash_gate(
    context, internal_final_audio_gap: bool
):
    database, settings, scan, scanner, runner, worker = context
    audio = MediaStream(
        id="audio:4352",
        index=1,
        pid=4352,
        kind=StreamKind.AUDIO,
        codec="truehd",
        codec_profile="TrueHD",
        channels=8,
        channel_layout="7.1",
        sample_rate=48_000,
        object_audio=True,
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

    def run_with_eac3_reports(argv, **kwargs):
        command = tuple(os.fspath(item) for item in argv)
        if command[0] == "ffmpeg" and "-f" in command:
            muxer = command[command.index("-f") + 1]
            if muxer == "hash":
                pytest.fail("lossy audio QC must not run a meaningless PCM hash gate")
        real_run(argv, **kwargs)
        stdout_path = kwargs.get("stdout_path")
        if stdout_path is None:
            return
        if internal_final_audio_gap and stdout_path.name == "final-frames.json":
            runner._write(stdout_path, runner._audio_frames(internal_gap=True))
        if command[0] == "mkvmerge" and "--identify" in command:
            runner._write(
                stdout_path,
                json.dumps(
                    {
                        "container": {
                            "properties": {"title": "Movie.2026.1080p.BluRay.x264"}
                        },
                        "tracks": [
                            {
                                "id": 0,
                                "type": "video",
                                "properties": {
                                    "default_track": True,
                                    "forced_track": False,
                                },
                            },
                            {
                                "id": 1,
                                "type": "audio",
                                "properties": {
                                    "language": "en",
                                    "track_name": "English Original Mix",
                                    "default_track": True,
                                    "forced_track": False,
                                },
                            },
                        ],
                        "attachments": [],
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
                                "level": 41,
                                "codec_type": "video",
                                "start_time": "0.000000",
                                "width": 1920,
                                "height": 1080,
                                "pix_fmt": "yuv420p",
                                "color_range": "tv",
                                "color_space": "bt709",
                                "color_transfer": "bt709",
                                "color_primaries": "bt709",
                                "chroma_location": "left",
                            },
                            {
                                "index": 1,
                                "codec_name": "eac3",
                                "codec_type": "audio",
                                "start_time": "0.000000",
                            },
                        ]
                    }
                ),
            )
        elif stdout_path.name.endswith("-probe.json"):
            source_probe = "-source-probe" in stdout_path.name
            runner._write(
                stdout_path,
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "audio",
                                "codec_name": "truehd" if source_probe else "eac3",
                                "sample_rate": "48000",
                                "channels": 8 if source_probe else 6,
                                "channel_layout": "7.1"
                                if source_probe
                                else "5.1(side)",
                                "bit_rate": None if source_probe else "1024000",
                                "start_time": "0",
                                "duration": "601" if source_probe else "601.016",
                            }
                        ]
                    }
                ),
            )

    runner.run = run_with_eac3_reports
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(
        job.id,
        _selection(
            tracks=[{"stream_id": "audio:4352", "action": "eac3", "language": "eng"}]
        ),
    )
    paths = JobPaths.create(settings, job.id)

    result = worker.process_job(ready)

    if internal_final_audio_gap:
        assert result.state is JobState.NEEDS_REVIEW
        assert "sample-cursor continuity failed" in result.status_message
        assert not list(paths.analysis.glob("*frames.json"))
        assert not any(
            artifact.name.endswith("frames.json")
            for artifact in database.list_artifacts(job_id=job.id, limit=1000)
        )
        return

    assert result.state is JobState.COMPLETED, result.status_message
    manifest = json.loads(
        (
            settings.completed_root
            / "Movie.2026.1080p.BluRay.x264"
            / "analysis"
            / "audio-comparison.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == 3
    track = manifest["tracks"][0]
    assert track["verification_mode"] == "lossy_transcode"
    assert track["decoded_pcm_sha256_required"] is False
    assert track["decoded_pcm_sha256_match"] is None
    assert track["verification"]["passed"] is True
    assert track["effective_target"]["codec_name"] == "eac3"
    assert track["decoded_frame_continuity_required"] is True
    assert track["source_to_sidecar_continuity"]["passed"] is True
    assert track["source_to_final_continuity"]["passed"] is True
    assert track["source_frame_continuity"]["continuous"] is True
    assert track["sidecar_frame_continuity"]["continuous"] is True
    assert track["final_frame_continuity"]["continuous"] is True
    assert not list(
        (settings.completed_root / "Movie.2026.1080p.BluRay.x264" / "analysis").glob(
            "*frames.json"
        )
    )
    assert not any(
        artifact.name.endswith("frames.json")
        for artifact in database.list_artifacts(job_id=job.id, limit=1000)
    )
    analysis_commands = [
        command
        for command in runner.commands
        if command[0] == "ffmpeg" and "-af" in command
    ]
    spectrum_commands = [
        command
        for command in runner.commands
        if command[0] == "ffmpeg" and any("showspectrumpic" in item for item in command)
    ]
    assert len(analysis_commands) == 2
    assert len(spectrum_commands) == 6
    for command in (*analysis_commands, *spectrum_commands):
        input_name = Path(command[command.index("-i") + 1]).name
        if input_name == "reference.mkv":
            assert "-drc_scale" not in command
        else:
            assert "-drc_scale" in command
    frame_commands = [
        command
        for command in runner.commands
        if command[0] == "ffprobe"
        and "-show_frames" in command
        and "-select_streams" in command
        and command[command.index("-select_streams") + 1].startswith("a:")
    ]
    assert len(frame_commands) == 3
    assert "-drc_scale" not in frame_commands[0]
    assert all("-drc_scale" in command for command in frame_commands[1:])
    audio_command = next(
        command
        for command in runner.commands
        if command[0] == "ffmpeg" and "-c:a" in command and "eac3" in command
    )
    assert audio_command[audio_command.index("-b:a") + 1] == "1024k"
    assert audio_command[audio_command.index("-ac") + 1] == "6"


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
    failure = next(
        event
        for event in database.list_events(job_id=job.id, limit=1000)
        if event.state_to is JobState.UPLOAD_FAILED
    )
    assert failure.payload["provider"] is None
    assert failure.payload["detail"] == "image upload provider initialization failed"
    assert any(
        path.name.endswith("-reference.png")
        for path in worker.settings.job_root(job.id)
        .joinpath("comparison")
        .glob("*.png")
    )


class _FakeUploadClient:
    def __init__(
        self,
        provider_name: str,
        *,
        fail_on_call: int | None = None,
        allow_fallback: bool = False,
        provider_may_have_committed: bool = False,
        failure_message: str | None = None,
        on_call: Callable[[Path], None] | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.fail_on_call = fail_on_call
        self.allow_fallback = allow_fallback
        self.provider_may_have_committed = provider_may_have_committed
        self.failure_message = failure_message
        self.on_call = on_call
        self.calls: list[str] = []

    def upload_png(self, path: Path) -> UploadedImage:
        self.calls.append(path.name)
        if self.on_call is not None:
            self.on_call(path)
        if self.fail_on_call == len(self.calls):
            raise ImageUploadError(
                self.failure_message or f"{self.provider_name} test failure",
                provider=self.provider_name,
                allow_fallback=self.allow_fallback,
                provider_may_have_committed=self.provider_may_have_committed,
            )
        digest = sha256_file(path)
        if self.provider_name == "imgbb":
            image_url = f"https://i.ibb.co/test/{path.name}"
            viewer_url = f"https://ibb.co/{path.stem}"
        elif self.provider_name == "catbox":
            image_url = f"https://files.catbox.moe/{path.name}"
            viewer_url = image_url
        elif self.provider_name == "freeimage":
            image_url = f"https://iili.io/{path.name}"
            viewer_url = f"https://freeimage.host/i/{path.stem}"
        else:
            raise AssertionError(
                f"unsupported fake upload provider: {self.provider_name}"
            )
        return UploadedImage(
            provider=self.provider_name,
            image_url=image_url,
            viewer_url=viewer_url,
            local_sha256=digest,
            remote_sha256=digest,
            bbcode=f"[img]{image_url}[/img]",
        )


def _advance_to_uploading(
    context, provider_factories, *, image_upload_provider: str = "auto"
):
    database, settings, scan, scanner, runner, _worker = context
    worker = PipelineWorker(
        database,
        settings,
        scanner_factory=lambda _settings: scanner,
        runner_factory=lambda _paths: runner,
        upload_client_factories=provider_factories,
    )
    job = _enqueue(database, scan.source)
    current = JobQueue(database).claim_next()
    assert current is not None
    current = worker.process_one_stage(current)
    assert current.state is JobState.AWAITING_SELECTION
    current = database.set_selection(
        job.id,
        _selection(
            upload_images=True,
            image_upload_provider=image_upload_provider,
        ),
    )
    while current.state is not JobState.UPLOADING:
        current = worker.process_one_stage(current)
    return worker, current, JobPaths.create(settings, job.id)


def _read_upload_checkpoint(paths: JobPaths) -> dict[str, Any]:
    return json.loads((paths.comparison / "uploads.json").read_text(encoding="utf-8"))


def test_public_json_private_fields_block_before_any_image_upload(context):
    client = _FakeUploadClient("imgbb")
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: client,),
    )
    video_manifest_path = paths.comparison / "video-comparison.json"
    video_metrics_path = paths.comparison / "video-metrics.json"
    audio_manifest_path = paths.analysis / "audio-comparison.json"

    metrics = json.loads(video_metrics_path.read_text(encoding="utf-8"))
    metrics["api_key"] = "do-not-publish"
    video_metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
    video_manifest = json.loads(video_manifest_path.read_text(encoding="utf-8"))
    video_manifest["job_id"] = uploading.id
    video_manifest["metrics"]["sha256"] = sha256_file(video_metrics_path)
    video_manifest_path.write_text(json.dumps(video_manifest), encoding="utf-8")
    audio_manifest = json.loads(audio_manifest_path.read_text(encoding="utf-8"))
    audio_manifest["source_path"] = r"C:\Users\secret\Videos\private-title.m2ts"
    audio_manifest_path.write_text(json.dumps(audio_manifest), encoding="utf-8")

    result = worker.process_job(uploading)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.UPLOADING
    assert "evidence changed" in (result.status_message or "")
    assert client.calls == []
    assert not (paths.comparison / "uploads.json").exists()


@pytest.mark.parametrize("tampered", [b"evil-mux", b"tampered-after-qc"])
def test_post_qc_final_mkv_tamper_blocks_before_any_upload(context, tampered: bytes):
    client = _FakeUploadClient("imgbb")
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: client,),
    )
    paths.muxed_output.write_bytes(tampered)

    result = worker.process_job(uploading)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.UPLOADING
    assert "changed after its validated mux checkpoint" in (result.status_message or "")
    assert client.calls == []
    assert not (
        worker.settings.completed_root
        / "Movie.2026.1080p.BluRay.x264"
        / "Movie.2026.1080p.BluRay.x264.mkv"
    ).exists()


@pytest.mark.parametrize(
    ("relative_path", "field", "value", "expected_stage"),
    [
        (
            ("analysis", "audio-comparison.json"),
            "summary",
            "forged-audio-metric",
            "audio QC",
        ),
        (
            ("comparison", "video-comparison.json"),
            "summary",
            "forged-video-aggregate",
            "video comparison",
        ),
    ],
)
def test_post_qc_public_evidence_tamper_blocks_before_upload(
    context,
    relative_path: tuple[str, str],
    field: str,
    value: str,
    expected_stage: str,
):
    client = _FakeUploadClient("imgbb")
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: client,),
    )
    target = paths.root.joinpath(*relative_path)
    document = json.loads(target.read_text(encoding="utf-8"))
    document[field] = value
    target.write_text(json.dumps(document), encoding="utf-8")

    result = worker.process_job(uploading)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.UPLOADING
    assert f"{expected_stage} evidence changed" in (result.status_message or "")
    assert client.calls == []


def test_public_evidence_mutated_during_upload_never_reaches_completed(context):
    paths_holder: list[JobPaths] = []
    mutated = False

    def mutate_audio_manifest(_path: Path) -> None:
        nonlocal mutated
        if mutated:
            return
        mutated = True
        paths = paths_holder[0]
        manifest_path = paths.analysis / "audio-comparison.json"
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        document["host"] = {
            "hostname": "private-encoder-host",
            "os": "Windows 11",
            "cpu": "private-cpu-model",
        }
        manifest_path.write_text(json.dumps(document), encoding="utf-8")

    client = _FakeUploadClient("imgbb", on_call=mutate_audio_manifest)
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: client,),
    )
    paths_holder.append(paths)

    result = worker.process_job(uploading)

    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.UPLOADING
    assert "audio QC evidence changed" in (result.status_message or "")
    assert client.calls
    assert not (
        worker.settings.completed_root / "Movie.2026.1080p.BluRay.x264"
    ).exists()


def test_public_evidence_mutated_during_final_mkv_copy_is_not_published(
    context,
    monkeypatch,
):
    client = _FakeUploadClient("imgbb")
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: client,),
    )
    real_copy2 = shutil.copy2
    mutated = False

    def mutate_after_revalidation(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *args: Any,
        **kwargs: Any,
    ):
        nonlocal mutated
        destination_path = Path(destination)
        if not mutated and destination_path.name.endswith(".mkv.partial"):
            mutated = True
            manifest_path = paths.analysis / "audio-comparison.json"
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["host"] = {
                "hostname": "post-revalidation-private-host",
                "cpu": "private-cpu-model",
            }
            manifest_path.write_text(json.dumps(document), encoding="utf-8")
        return real_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(worker_module.shutil, "copy2", mutate_after_revalidation)

    result = worker.process_job(uploading)

    completed = worker.settings.completed_root / "Movie.2026.1080p.BluRay.x264"
    assert mutated is True
    assert client.calls
    assert result.state is JobState.NEEDS_REVIEW
    assert result.resume_state is JobState.UPLOADING
    assert "public analysis sidecar changed after validation" in (
        result.status_message or ""
    )
    assert not (completed / "analysis" / "audio-comparison.json").exists()
    assert not (completed / ".analysis.partial").exists()
    assert not (completed / ".comparison.partial").exists()


def test_upload_falls_back_only_before_the_first_success(context):
    primary = _FakeUploadClient("imgbb", fail_on_call=1, allow_fallback=True)
    fallback = _FakeUploadClient("catbox")
    unused = _FakeUploadClient("freeimage")
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: primary, lambda: fallback, lambda: unused),
    )
    expected_names = {path.name for path in _current_comparison_pngs(paths)}

    result = worker.process_job(uploading)

    assert result.state is JobState.COMPLETED
    assert len(primary.calls) == 1
    assert set(fallback.calls) == expected_names
    assert unused.calls == []
    checkpoint = _read_upload_checkpoint(paths)
    assert checkpoint["schema_version"] == 2
    assert checkpoint["provider"] == "catbox"
    assert set(checkpoint["images"]) == expected_names
    assert {item["provider"] for item in checkpoint["images"].values()} == {"catbox"}


def test_safe_fallback_atomically_advances_the_provisional_provider(context):
    observed: list[str] = []

    def observe_first_call(provider_name: str) -> Callable[[Path], None]:
        def observe(_path: Path) -> None:
            if provider_name in observed:
                return
            checkpoint = _read_upload_checkpoint(paths)
            assert checkpoint == {
                "schema_version": 2,
                "provider": provider_name,
                "provider_provisional": True,
                "images": {},
            }
            observed.append(provider_name)

        return observe

    primary = _FakeUploadClient(
        "imgbb",
        fail_on_call=1,
        allow_fallback=True,
        on_call=observe_first_call("imgbb"),
    )
    fallback = _FakeUploadClient("catbox", on_call=observe_first_call("catbox"))
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: primary, lambda: fallback),
    )

    result = worker.process_job(uploading)

    assert result.state is JobState.COMPLETED
    assert observed == ["imgbb", "catbox"]
    checkpoint = _read_upload_checkpoint(paths)
    assert checkpoint["provider"] == "catbox"
    assert "provider_provisional" not in checkpoint


def test_all_safe_provider_failures_clear_the_provisional_pin(context):
    primary = _FakeUploadClient("imgbb", fail_on_call=1, allow_fallback=True)
    fallback = _FakeUploadClient("catbox", fail_on_call=1, allow_fallback=True)
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: primary, lambda: fallback),
    )

    result = worker.process_job(uploading)

    assert result.state is JobState.UPLOAD_FAILED
    assert primary.calls
    assert fallback.calls
    assert _read_upload_checkpoint(paths) == {
        "schema_version": 2,
        "images": {},
    }


def test_process_interruption_preserves_pre_dispatch_provider_pin(context):
    class SimulatedProcessExit(BaseException):
        pass

    observed_checkpoint: dict[str, Any] = {}

    def interrupt_during_upload(_path: Path) -> None:
        observed_checkpoint.update(_read_upload_checkpoint(paths))
        raise SimulatedProcessExit

    primary = _FakeUploadClient("imgbb", on_call=interrupt_during_upload)
    fallback = _FakeUploadClient("catbox")
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: primary, lambda: fallback),
    )

    with pytest.raises(SimulatedProcessExit):
        worker.process_job(uploading)

    assert observed_checkpoint == {
        "schema_version": 2,
        "provider": "imgbb",
        "provider_provisional": True,
        "images": {},
    }
    assert worker.database.get_job(uploading.id).state is JobState.UPLOADING

    retry_primary = _FakeUploadClient("imgbb", fail_on_call=1, allow_fallback=True)
    retry_fallback = _FakeUploadClient("catbox")
    restarted = PipelineWorker(
        worker.database,
        worker.settings,
        upload_client_factories=(
            lambda: retry_primary,
            lambda: retry_fallback,
        ),
    )

    retried = restarted.process_job(worker.database.get_job(uploading.id))

    assert retried.state is JobState.UPLOAD_FAILED
    assert len(retry_primary.calls) == 1
    assert retry_fallback.calls == []
    assert _read_upload_checkpoint(paths) == observed_checkpoint


def test_manual_image_provider_disables_fallback_and_skips_other_hosts(context):
    primary = _FakeUploadClient("imgbb")
    selected = _FakeUploadClient("catbox")
    unused = _FakeUploadClient("freeimage")
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: primary, lambda: selected, lambda: unused),
        image_upload_provider="catbox",
    )

    result = worker.process_job(uploading)

    assert result.state is JobState.COMPLETED
    assert primary.calls == []
    assert selected.calls
    assert unused.calls == []
    checkpoint = _read_upload_checkpoint(paths)
    assert checkpoint["provider"] == "catbox"


def test_first_success_locks_provider_across_failure_and_retry(context):
    primary = _FakeUploadClient("imgbb", fail_on_call=2, allow_fallback=True)
    fallback = _FakeUploadClient("catbox")
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: primary, lambda: fallback),
    )

    failed = worker.process_job(uploading)

    assert failed.state is JobState.UPLOAD_FAILED
    assert len(primary.calls) == 2
    assert fallback.calls == []
    checkpoint = _read_upload_checkpoint(paths)
    assert checkpoint["schema_version"] == 2
    assert checkpoint["provider"] == "imgbb"
    assert len(checkpoint["images"]) == 1
    assert {item["provider"] for item in checkpoint["images"].values()} == {"imgbb"}

    retry_primary = _FakeUploadClient("imgbb", fail_on_call=1, allow_fallback=True)
    retry_fallback = _FakeUploadClient("catbox")
    restarted = PipelineWorker(
        worker.database,
        worker.settings,
        upload_client_factories=(
            lambda: retry_primary,
            lambda: retry_fallback,
        ),
    )
    retrying = JobQueue(worker.database).retry_upload(failed.id)

    retried = restarted.process_job(retrying)

    assert retried.state is JobState.UPLOAD_FAILED
    assert len(retry_primary.calls) == 1
    assert retry_fallback.calls == []
    assert _read_upload_checkpoint(paths) == checkpoint


@pytest.mark.parametrize(
    "failure_message",
    (
        "imgbb upload outcome is unknown",
        "imgbb verification failed after upload",
    ),
)
def test_unverified_commit_durably_pins_provider_across_retry(
    context, failure_message: str
):
    primary = _FakeUploadClient(
        "imgbb",
        fail_on_call=1,
        provider_may_have_committed=True,
        failure_message=failure_message,
    )
    fallback = _FakeUploadClient("catbox")
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: primary, lambda: fallback),
    )

    failed = worker.process_job(uploading)

    assert failed.state is JobState.UPLOAD_FAILED
    assert len(primary.calls) == 1
    assert fallback.calls == []
    checkpoint = _read_upload_checkpoint(paths)
    assert checkpoint == {
        "schema_version": 2,
        "provider": "imgbb",
        "images": {},
    }

    retry_primary = _FakeUploadClient("imgbb", fail_on_call=1, allow_fallback=True)
    retry_fallback = _FakeUploadClient("catbox")
    restarted = PipelineWorker(
        worker.database,
        worker.settings,
        upload_client_factories=(
            lambda: retry_primary,
            lambda: retry_fallback,
        ),
    )
    retrying = JobQueue(worker.database).retry_upload(failed.id)

    retried = restarted.process_job(retrying)

    assert retried.state is JobState.UPLOAD_FAILED
    assert len(retry_primary.calls) == 1
    assert retry_fallback.calls == []
    assert _read_upload_checkpoint(paths) == checkpoint


def test_schema_v1_upload_checkpoint_resumes_as_imgbb_without_mixing(context):
    primary = _FakeUploadClient("imgbb")
    fallback = _FakeUploadClient("catbox")
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: primary, lambda: fallback),
    )
    pngs = _current_comparison_pngs(paths)
    first = pngs[0]
    digest = sha256_file(first)
    legacy_url = f"https://i.ibb.co/legacy/{first.name}"
    legacy_viewer = f"https://ibb.co/{first.stem}"
    legacy_item = {
        "image_url": legacy_url,
        "viewer_url": legacy_viewer,
        "local_sha256": digest,
        "remote_sha256": digest,
        "bbcode": f"[url={legacy_viewer}][img]{legacy_url}[/img][/url]",
    }
    (paths.comparison / "uploads.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "images": {first.name: legacy_item},
            }
        ),
        encoding="utf-8",
    )

    result = worker.process_job(uploading)

    assert result.state is JobState.COMPLETED
    assert first.name not in primary.calls
    assert set(primary.calls) == {path.name for path in pngs[1:]}
    assert fallback.calls == []
    checkpoint = _read_upload_checkpoint(paths)
    assert checkpoint["schema_version"] == 2
    assert checkpoint["provider"] == "imgbb"
    migrated_legacy = checkpoint["images"][first.name]
    assert migrated_legacy["provider"] == "imgbb"
    assert {
        key: value for key, value in migrated_legacy.items() if key != "provider"
    } == legacy_item
    assert set(checkpoint["images"]) == {path.name for path in pngs}


def test_mixed_provider_checkpoint_requires_review_before_any_upload(context):
    primary = _FakeUploadClient("imgbb")
    fallback = _FakeUploadClient("catbox")
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: primary, lambda: fallback),
    )
    pngs = _current_comparison_pngs(paths)
    mixed: dict[str, dict[str, str]] = {}
    for path, provider in zip(pngs[:2], ("imgbb", "catbox"), strict=True):
        digest = sha256_file(path)
        image_url = (
            f"https://i.ibb.co/test/{path.name}"
            if provider == "imgbb"
            else f"https://files.catbox.moe/{path.name}"
        )
        viewer_url = f"https://ibb.co/{path.stem}" if provider == "imgbb" else image_url
        mixed[path.name] = {
            "provider": provider,
            "image_url": image_url,
            "viewer_url": viewer_url,
            "local_sha256": digest,
            "remote_sha256": digest,
            "bbcode": f"[img]{image_url}[/img]",
        }
    (paths.comparison / "uploads.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "provider": "imgbb",
                "images": mixed,
            }
        ),
        encoding="utf-8",
    )

    result = worker.process_job(uploading)

    assert result.state is JobState.NEEDS_REVIEW
    assert "provider conflicts with checkpoint" in (result.status_message or "")
    assert primary.calls == []
    assert fallback.calls == []


def test_tampered_upload_checkpoint_never_reaches_public_package(context):
    client = _FakeUploadClient("imgbb")
    worker, uploading, paths = _advance_to_uploading(
        context,
        (lambda: client,),
    )
    first = _current_comparison_pngs(paths)[0]
    digest = sha256_file(first)
    image_url = f"https://i.ibb.co/test/{first.name}"
    viewer_url = f"https://ibb.co/{first.stem}"
    tampered = {
        "schema_version": 2,
        "provider": "imgbb",
        "images": {
            first.name: {
                "provider": "imgbb",
                "image_url": image_url,
                "viewer_url": viewer_url,
                "local_sha256": digest,
                "remote_sha256": digest,
                "bbcode": "[img]https://attacker.invalid/injected.png[/img]",
                "delete_url": "https://ibb.co/delete/private-token",
            }
        },
    }
    checkpoint = paths.comparison / "uploads.json"
    checkpoint.write_text(json.dumps(tampered), encoding="utf-8")

    result = worker.process_job(uploading)

    assert result.state is JobState.NEEDS_REVIEW
    assert "unsafe" in (result.status_message or "")
    assert client.calls == []
    assert json.loads(checkpoint.read_text(encoding="utf-8")) == tampered
    assert not (
        worker.settings.completed_root / "Movie.2026.1080p.BluRay.x264"
    ).exists()


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


def test_selection_schema_v1_chroma_offset_migrates_to_v2_effective_value(context):
    database, _settings, scan, _scanner, _runner, _worker = context
    job = _enqueue(database, scan.source)
    legacy = _selection()
    legacy["video"]["settings"] = {"chroma_qp_offset": 0}

    parsed_legacy = parse_selection(job.model_copy(update={"selection": legacy}), scan)

    assert parsed_legacy.schema_version == 2
    assert parsed_legacy.settings.chroma_qp_offset == -2
    assert parsed_legacy.settings.private_params()["chroma-qp-offset"] == 0

    current = _selection(schema_version=2)
    current["video"]["settings"] = {"chroma_qp_offset": 0}
    parsed_current = parse_selection(
        job.model_copy(update={"selection": current}), scan
    )

    assert parsed_current.schema_version == 2
    assert parsed_current.settings.chroma_qp_offset == 0
    assert parsed_current.settings.private_params()["chroma-qp-offset"] == 2


def test_parse_selection_applies_automatic_level_4_1_vbv_ref_and_gop(context):
    database, _settings, scan, _scanner, _runner, _worker = context
    job = _enqueue(database, scan.source).model_copy(
        update={"selection": _selection(schema_version=2)}
    )

    parsed = parse_selection(job, scan)

    assert parsed.schema_version == 2
    assert parsed.settings.level == "4.1"
    assert parsed.settings.ref == 4
    assert (parsed.settings.keyint, parsed.settings.min_keyint) == (240, 24)
    assert parsed.settings.vbv is not None
    assert parsed.settings.vbv.maxrate_kbps == 62_500
    assert parsed.settings.vbv.bufsize_kbps == 78_125
    args = parsed.settings.ffmpeg_video_args()
    assert args[args.index("-level:v") + 1] == "4.1"
    private = args[args.index("-x264-params") + 1]
    assert "ref=4" in private
    assert "keyint=240" in private
    assert "min-keyint=24" in private
    assert "vbv-maxrate=62500" in private
    assert "vbv-bufsize=78125" in private


def test_native_yuv_metric_gate_accepts_all_exact_policy_boundaries() -> None:
    samples = [
        {"category": "I", "ssim_all": 0.97, "psnr_average_db": 35.0},
        {"category": "P", "ssim_all": 0.95, "psnr_average_db": 39.0},
        {"category": "B", "ssim_all": 0.93, "psnr_average_db": 40.0},
    ]

    assert worker_module._sampled_video_metric_errors(samples) == ()
    assert (
        worker_module._sampled_video_metric_errors(
            [{"category": "I", "ssim_all": 1.0, "psnr_average_db": "inf"}]
        )
        == ()
    )


@pytest.mark.parametrize(
    ("samples", "message"),
    (
        (
            [{"category": "I", "ssim_all": 0.929, "psnr_average_db": 40.0}],
            "SSIM is below 0.93",
        ),
        (
            [
                {"category": kind, "ssim_all": 0.949, "psnr_average_db": 40.0}
                for kind in "IPB"
            ],
            "mean sampled SSIM is below 0.95",
        ),
        (
            [{"category": "I", "ssim_all": 0.96, "psnr_average_db": 34.9}],
            "PSNR is below 35 dB",
        ),
        (
            [
                {"category": kind, "ssim_all": 0.96, "psnr_average_db": 37.9}
                for kind in "IPB"
            ],
            "mean sampled PSNR is below 38 dB",
        ),
        (
            [
                {"category": "I", "ssim_all": 0.98, "psnr_average_db": 40.0},
                {"category": "P", "ssim_all": 0.98, "psnr_average_db": 40.0},
                {"category": "B", "ssim_all": 0.949, "psnr_average_db": 40.0},
            ],
            "B-frame sampled SSIM trails P-frames by more than 0.03",
        ),
        (
            [{"category": "I", "ssim_all": "nan", "psnr_average_db": "nan"}],
            "no finite SSIM value",
        ),
    ),
)
def test_native_yuv_metric_gate_rejects_each_quality_failure(
    samples: list[dict[str, object]], message: str
) -> None:
    errors = worker_module._sampled_video_metric_errors(samples)

    assert any(message in error for error in errors)


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


_HDR10_MASTERING = (
    "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)"
)


def _uhd_hdr10_scan(scan: DiscScan, static: HdrStaticMetadata) -> DiscScan:
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
            hdr10_static=static,
            hdr10_base_layer=True,
        ),
    )
    return replace(
        scan,
        disc_kind=DiscKind.UHD,
        playlists=(replace(scan.playlists[0], streams=(uhd_stream,)),),
    )


def _uhd_hdr10_selection(
    **metadata_updates: object,
) -> dict[str, Any]:
    selection = _selection()
    selection["output_name"] = "Movie.2026.2160p.UHD.BluRay.x265"
    metadata: dict[str, object] = {
        "enabled": True,
        "mastering_display": _HDR10_MASTERING,
        "max_cll": 1000,
        "max_fall": 400,
    }
    metadata.update(metadata_updates)
    selection["video"]["settings"] = {
        "hdr10": metadata,
    }
    return selection


def test_manual_hdr10_settings_can_complete_incomplete_uhd_scan(context):
    database, _settings, scan, _scanner, _runner, _worker = context
    uhd_scan = _uhd_hdr10_scan(scan, HdrStaticMetadata())
    selection = _uhd_hdr10_selection()
    job = _enqueue(database, scan.source).model_copy(update={"selection": selection})

    parsed = parse_selection(job, uhd_scan)

    assert parsed.settings.encoder.value == "x265"
    assert parsed.settings.hdr10.enabled is True
    assert parsed.settings.hdr10.max_cll == 1000


def test_manual_hdr10_must_exactly_match_complete_uhd_scan(context) -> None:
    database, _settings, scan, _scanner, _runner, _worker = context
    static = HdrStaticMetadata(_HDR10_MASTERING, 1000, 400)
    uhd_scan = _uhd_hdr10_scan(scan, static)
    selection = _uhd_hdr10_selection()
    job = _enqueue(database, scan.source).model_copy(update={"selection": selection})

    parsed = parse_selection(job, uhd_scan)

    assert parsed.settings.hdr10.mastering_display == static.mastering_display
    assert parsed.settings.hdr10.max_cll == static.max_cll
    assert parsed.settings.hdr10.max_fall == static.max_fall
    assert parsed.settings.hdr10.hdr10_opt is True


@pytest.mark.parametrize(
    ("field_name", "selected_value"),
    [
        (
            "mastering_display",
            "G(13251,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)",
        ),
        ("max_cll", 1001),
        ("max_fall", 401),
    ],
)
def test_manual_hdr10_conflict_with_complete_scan_requires_review(
    context,
    field_name: str,
    selected_value: object,
) -> None:
    database, _settings, scan, _scanner, _runner, _worker = context
    static = HdrStaticMetadata(_HDR10_MASTERING, 1000, 400)
    uhd_scan = _uhd_hdr10_scan(scan, static)
    selection = _uhd_hdr10_selection(**{field_name: selected_value})
    job = _enqueue(database, scan.source).model_copy(update={"selection": selection})

    with pytest.raises(ReviewRequired) as error:
        parse_selection(job, uhd_scan)

    assert error.value.details["code"] == "source_hdr10_metadata_conflict"
    assert error.value.details["scan_complete"] is True
    assert error.value.details["conflicts"][field_name] == {
        "scanned": getattr(static, field_name),
        "selected": selected_value,
    }


@pytest.mark.parametrize("field_name", ["enabled", "hdr10_opt"])
def test_source_hdr10_cannot_disable_required_hdr_policy(
    context,
    field_name: str,
) -> None:
    database, _settings, scan, _scanner, _runner, _worker = context
    static = HdrStaticMetadata(_HDR10_MASTERING, 1000, 400)
    uhd_scan = _uhd_hdr10_scan(scan, static)
    if field_name == "enabled":
        selection = _uhd_hdr10_selection()
        selection["video"]["settings"]["hdr10"] = {"enabled": False}
    else:
        selection = _uhd_hdr10_selection(hdr10_opt=False)
    job = _enqueue(database, scan.source).model_copy(update={"selection": selection})

    with pytest.raises(ReviewRequired) as error:
        parse_selection(job, uhd_scan)

    assert error.value.details["code"] == "source_hdr10_metadata_conflict"
    assert error.value.details["conflicts"][field_name] == {
        "required": True,
        "selected": False,
    }


def test_manual_hdr10_can_only_fill_missing_scan_fields(context) -> None:
    database, _settings, scan, _scanner, _runner, _worker = context
    partial = HdrStaticMetadata(_HDR10_MASTERING, None, None)
    uhd_scan = _uhd_hdr10_scan(scan, partial)
    changed_mastering = (
        "G(13251,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)"
    )
    selection = _uhd_hdr10_selection(mastering_display=changed_mastering)
    job = _enqueue(database, scan.source).model_copy(update={"selection": selection})

    with pytest.raises(ReviewRequired) as error:
        parse_selection(job, uhd_scan)

    assert error.value.details["scan_complete"] is False
    assert "mastering_display" in error.value.details["conflicts"]
