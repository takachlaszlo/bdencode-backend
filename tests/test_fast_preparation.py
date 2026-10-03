"""Faster disc preparation: parallel playlist probes, source staging, the GPU crop scan and the
source integrity decode beside the encode."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from bdencode import source_stage
from bdencode.config import ConfigurationError, Settings
from bdencode.media.bluray import BluRayScanner, ToolCapabilities
from bdencode.qc.crop import full_title_cropdetect_command
from bdencode.qc.integrity import source_video_integrity_command
from bdencode.worker import _BackgroundRun


# --- parallel playlist probes -------------------------------------------------------------------


class _SlowRunner:
    """Answers playlist probes after a delay that is longest for the first playlists."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def capture(self, argv, *, timeout: float = 30, check: bool = True):
        arguments = [str(item) for item in argv]
        if "-show_streams" not in arguments:
            return type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        playlist = int(arguments[arguments.index("-playlist") + 1])
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.01 * (self.count - playlist))
        with self.lock:
            self.active -= 1
        payload = {"format": {"duration": str(100 + playlist)}, "streams": []}
        return type("Result", (), {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""})()


def _disc(tmp_path: Path, count: int) -> tuple[Path, Path]:
    storage = tmp_path / "storage"
    disc = storage / "Disc"
    (disc / "BDMV" / "PLAYLIST").mkdir(parents=True)
    (disc / "BDMV" / "STREAM").mkdir(parents=True)
    for index in range(count):
        (disc / "BDMV" / "PLAYLIST" / f"{index:05d}.mpls").write_bytes(b"MPL0200")
    return storage, disc


def _scan(storage: Path, disc: Path, runner: _SlowRunner, workers: int, count: int):
    native = {"playlists": [{"id": f"{index:05d}", "duration": 100 + index} for index in range(count)]}
    return BluRayScanner(
        runner=runner,
        capabilities=ToolCapabilities(ffprobe="/usr/bin/ffprobe", ffprobe_bluray=True),
        libbluray_provider=lambda _: native,
        source_root=storage,
        probe_workers=workers,
    ).scan(disc)


def test_playlists_are_probed_concurrently_with_the_sequential_result(tmp_path: Path) -> None:
    storage, disc = _disc(tmp_path, 12)
    parallel_runner, sequential_runner = _SlowRunner(12), _SlowRunner(12)

    parallel = _scan(storage, disc, parallel_runner, 4, 12)
    sequential = _scan(storage, disc, sequential_runner, 1, 12)

    assert 1 < parallel_runner.peak <= 4
    assert sequential_runner.peak == 1
    assert [item.playlist_id for item in parallel.playlists] == [
        item.playlist_id for item in sequential.playlists
    ]
    assert sorted(item.playlist_id for item in parallel.playlists) == [
        f"{index:05d}" for index in range(12)
    ]
    assert parallel.fingerprint == sequential.fingerprint


# --- source staging -----------------------------------------------------------------------------


MOUNTS = (
    "/dev/sdd / ext4 rw,relatime 0 0\n"
    "C:\\ /mnt/c 9p rw,noatime,aname=drvfs 0 0\n"
    "//nas/share /mnt/nas cifs rw 0 0\n"
)


def test_slow_mounts_are_recognised_from_the_mount_table() -> None:
    assert source_stage.filesystem_type(Path("/mnt/c/Videos/Disc"), MOUNTS) == "9p"
    assert source_stage.filesystem_type(Path("/home/taki/discs"), MOUNTS) == "ext4"
    assert source_stage.needs_staging(Path("/mnt/c/Videos/Disc"), mounts=MOUNTS)
    assert source_stage.needs_staging(Path("/mnt/nas/Disc"), mounts=MOUNTS)
    assert not source_stage.needs_staging(Path("/home/taki/discs/Disc"), mounts=MOUNTS)
    assert source_stage.needs_staging(Path("/home/taki/discs/Disc"), mode="always", mounts=MOUNTS)
    assert not source_stage.needs_staging(Path("/mnt/c/Videos/Disc"), mode="never", mounts=MOUNTS)
    # "/mnt/cdrom" is not inside the "/mnt/c" mount.
    assert source_stage.filesystem_type(Path("/mnt/cdrom"), MOUNTS) == "ext4"


def _bluray(root: Path) -> Path:
    disc = root / "Disc"
    for name, size in (
        ("BDMV/index.bdmv", 100),
        ("BDMV/PLAYLIST/00001.mpls", 300),
        ("BDMV/PLAYLIST/00002.mpls", 300),
        ("BDMV/CLIPINF/00010.clpi", 200),
        ("BDMV/CLIPINF/00020.clpi", 200),
        ("BDMV/STREAM/00010.m2ts", 70_000),
        ("BDMV/STREAM/00011.m2ts", 5_000),
        ("BDMV/STREAM/00020.m2ts", 50_000),
        ("CERTIFICATE/id.bdmv", 50),
    ):
        path = disc / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(os.urandom(size))
    return disc


def test_only_the_selected_clips_are_planned(tmp_path: Path) -> None:
    disc = _bluray(tmp_path)
    everything = [item.relative for item in source_stage.plan_files(disc)]
    selected = [item.relative for item in source_stage.plan_files(disc, ["00010", "00011"])]
    assert "BDMV/STREAM/00020.m2ts" in everything
    assert "BDMV/STREAM/00020.m2ts" not in selected
    assert {"BDMV/STREAM/00010.m2ts", "BDMV/STREAM/00011.m2ts", "CERTIFICATE/id.bdmv",
            "BDMV/CLIPINF/00020.clpi", "BDMV/PLAYLIST/00002.mpls"} <= set(selected)


def test_a_disc_is_copied_byte_for_byte_and_the_copy_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the ranged multi-reader copy on small files.
    monkeypatch.setattr(source_stage, "PARALLEL_THRESHOLD", 1)
    disc = _bluray(tmp_path)
    cache = tmp_path / "cache"
    reports: list[tuple[int, int]] = []

    staged = source_stage.stage_disc(
        disc, cache, clips=["00010"], readers=3, block=4096, report=lambda done, total: reports.append((done, total))
    )

    assert staged == source_stage.stage_root_for(cache, disc)
    for item in source_stage.plan_files(disc, ["00010"]):
        original, copy = disc / item.relative, staged / item.relative
        assert copy.read_bytes() == original.read_bytes()
        assert copy.stat().st_mtime_ns == original.stat().st_mtime_ns
    assert not (staged / "BDMV/STREAM/00020.m2ts").exists()
    assert reports[-1][0] == reports[-1][1]
    assert source_stage.current_stage(cache, disc, ["00010"]) == staged

    marker = staged / "BDMV/STREAM/00010.m2ts"
    marker_mtime = marker.stat().st_mtime_ns
    assert source_stage.stage_disc(disc, cache, clips=["00010"]) == staged
    assert marker.stat().st_mtime_ns == marker_mtime  # reused, not copied again

    # A changed source file invalidates the copy.
    (disc / "BDMV/STREAM/00010.m2ts").write_bytes(os.urandom(70_001))
    assert source_stage.current_stage(cache, disc, ["00010"]) is None
    restaged = source_stage.stage_disc(disc, cache, clips=["00010"])
    assert (restaged / "BDMV/STREAM/00010.m2ts").stat().st_size == 70_001

    source_stage.remove_stage(cache, disc)
    assert not staged.exists()


def test_staging_refuses_when_the_disc_and_the_reserve_do_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disc = _bluray(tmp_path)
    usage = type("Usage", (), {"total": 10**6, "used": 0, "free": 100_000})()
    monkeypatch.setattr(source_stage.shutil, "disk_usage", lambda _path: usage)
    with pytest.raises(source_stage.StagingUnavailable, match="not enough local space"):
        source_stage.stage_disc(disc, tmp_path / "cache", reserve_bytes=60_000)


def test_a_stop_request_ends_the_copy_and_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_stage, "PARALLEL_THRESHOLD", 1)
    monkeypatch.setattr(source_stage, "STOP_CHECK_SECONDS", 0.0)
    disc = _bluray(tmp_path)
    cache = tmp_path / "cache"
    with pytest.raises(source_stage.StagingInterrupted):
        source_stage.stage_disc(disc, cache, readers=2, block=1024, should_stop=lambda: True)
    assert list((cache / "disc-stage").iterdir()) == []


def test_old_stages_and_abandoned_partial_copies_are_removed(tmp_path: Path) -> None:
    root = tmp_path / "cache" / "disc-stage"
    old, fresh, partial, kept = (root / name for name in ("old", "fresh", "x.partial", "kept"))
    for path in (old, fresh, partial, kept):
        path.mkdir(parents=True)
    now = time.time()
    os.utime(old, (now - 2 * 86400, now - 2 * 86400))
    os.utime(partial, (now - 7200, now - 7200))
    os.utime(kept, (now - 2 * 86400, now - 2 * 86400))

    removed = source_stage.remove_stale_stages(tmp_path / "cache", keep=kept)

    assert set(removed) == {old, partial}
    assert fresh.exists() and kept.exists()


def test_staging_and_crop_acceleration_settings_are_validated() -> None:
    settings = Settings(source_roots=(Path("/srv/discs"),), data_root=Path("/srv/encode"))
    assert settings.source_staging == "auto"
    assert settings.crop_hwaccel == "auto"
    with pytest.raises(ConfigurationError, match="source_staging"):
        Settings(source_roots=(Path("/srv/discs"),), data_root=Path("/srv/encode"), source_staging="sometimes").validate()
    with pytest.raises(ConfigurationError, match="crop_hwaccel"):
        Settings(source_roots=(Path("/srv/discs"),), data_root=Path("/srv/encode"), crop_hwaccel="vaapi").validate()


# --- crop scan and the source decode beside the encode ------------------------------------------


def test_the_gpu_crop_scan_only_adds_the_decoder_selection() -> None:
    cpu = full_title_cropdetect_command(Path("/work/reference.mkv"))
    gpu = full_title_cropdetect_command(Path("/work/reference.mkv"), hwaccel="cuda")
    position = gpu.index("-hwaccel")
    assert gpu[position:position + 2] == ["-hwaccel", "cuda"]
    assert gpu[:position] + gpu[position + 2:] == cpu
    assert position < gpu.index("-i")
    with pytest.raises(ValueError, match="unsupported"):
        full_title_cropdetect_command(Path("/work/reference.mkv"), hwaccel="vaapi")


def test_the_integrity_decode_threads_are_set_before_the_input() -> None:
    plain = source_video_integrity_command(Path("/work/reference.mkv"))
    threaded = source_video_integrity_command(Path("/work/reference.mkv"), threads=16)
    position = threaded.index("-threads")
    assert threaded[position:position + 2] == ["-threads", "16"]
    assert position < threaded.index("-i")
    assert threaded[:position] + threaded[position + 2:] == plain
    with pytest.raises(ValueError, match="thread"):
        source_video_integrity_command(Path("/work/reference.mkv"), threads=0)


def test_background_run_hands_its_error_to_the_waiting_thread() -> None:
    def fail(_cancelled) -> None:
        raise RuntimeError("integrity failed")

    run = _BackgroundRun(fail, name="test-fail")
    with pytest.raises(RuntimeError, match="integrity failed"):
        run.wait()
    assert run.failed()


def test_background_run_cancel_stops_the_work_and_returns_its_error() -> None:
    started = threading.Event()

    def work(cancelled) -> None:
        started.set()
        while not cancelled():
            time.sleep(0.01)
        raise RuntimeError("interrupted")

    run = _BackgroundRun(work, name="test-cancel")
    assert started.wait(5)
    assert not run.failed()
    error = run.cancel()
    assert isinstance(error, RuntimeError)
