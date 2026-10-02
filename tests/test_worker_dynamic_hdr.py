from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import bdencode.hdr_dynamic as hdr_dynamic
import bdencode.worker as worker_module
from bdencode.media.bluray import HdrStaticMetadata
from bdencode.models import JobState
from bdencode.queue import JobQueue
from bdencode.worker import JobPaths, ReviewRequired, parse_selection

from test_worker import (  # noqa: F401
    FAKE_REFERENCE_FRAMES,
    FakeRunner,
    _HDR10_MASTERING,
    _enqueue,
    _uhd_hdr10_scan,
    _uhd_hdr10_selection,
    context,
)

AVAILABLE = {
    "hdr10plus": {
        "tool": "hdr10plus_tool",
        "tool_available": True,
        "tool_version": "hdr10plus_tool 1.6.1",
        "x265_supported": True,
        "available": True,
    },
    "dolby_vision": {
        "tool": "dovi_tool",
        "tool_available": True,
        "tool_version": "dovi_tool 2.1.2",
        "x265_supported": True,
        "available": True,
    },
}


def hdr10plus_document(frames: int) -> str:
    entry = '{"LuminanceParameters":{"AverageRGB":1},"NumberOfWindows":1}'
    return (
        '{"JSONInfo":{"Version":"1.0"},"SceneInfo":['
        + ",".join([entry] * frames)
        + '],"SceneInfoSummary":{"SceneFirstFrameIndex":[0],"SceneFrameNumbers":['
        + str(frames)
        + "]}}"
    )


class HdrRunner(FakeRunner):
    """FakeRunner that plays the roles of hdr10plus_tool and dovi_tool."""

    def __init__(
        self,
        *,
        frames: int = FAKE_REFERENCE_FRAMES,
        profile: int = 8,
        rebuilt_frames: int | None = None,
    ) -> None:
        super().__init__()
        self.frames = frames
        self.profile = profile
        # RPUs that a read-back of the rebuilt Dolby Vision stream reports.
        self.rebuilt_frames = frames if rebuilt_frames is None else rebuilt_frames

    def run(self, argv: Any, **kwargs: Any) -> None:
        command = tuple(os.fspath(item) for item in argv)
        stderr_path = kwargs.get("stderr_path")
        stdout_path = kwargs.get("stdout_path")
        if command[0] == "dovi_tool" and "info" in command:
            self.commands.append(command)
            readback = any(item.endswith("encoded-rpu.bin") for item in command)
            frames = self.rebuilt_frames if readback else self.frames
            self._write(
                stdout_path,
                f"Summary:\n  Frames: {frames}\n  Profile: {self.profile}\n",
            )
            if stderr_path is not None:
                self._write(stderr_path, "")
            return
        # The post-encode RPU injection: each tool leaves its (tiny) output where the worker expects it.
        if command[0] == "mkvmerge" and "--identify" in command and command[-1].endswith(
            "video-encoded.partial.mkv"
        ):
            self.commands.append(command)
            track = {"type": "video", "properties": {
                "default_duration": 41708333, "color_range": 1, "color_matrix_coefficients": 9,
                "color_transfer_characteristics": 16, "color_primaries": 9}}
            self._write(stdout_path, json.dumps({"tracks": [track]}))
            if stderr_path is not None:
                self._write(stderr_path, "")
            return
        if command[0] == "mkvpropedit":
            self.commands.append(command)
            if stderr_path is not None:
                self._write(stderr_path, "")
            return
        produced: Path | None = None
        if command[0] == "dovi_tool" and "inject-rpu" in command:
            produced = Path(command[command.index("-o") + 1])
        elif command[0] == "hdr10plus_tool" and "inject" in command:
            produced = Path(command[command.index("-o") + 1])
        elif command[0] == "mkvextract" and command[-1].startswith("0:"):
            produced = Path(command[-1].split(":", 1)[1])
        elif command[0] == "mkvmerge" and "--timestamps" in command:
            produced = Path(command[command.index("--output") + 1])
        elif command[0] == "ffmpeg" and command[-1].endswith("encoded.hevc"):
            produced = Path(command[-1])
        if produced is not None:
            self.commands.append(command)
            self._write(produced, b"stream")
            if stderr_path is not None:
                self._write(stderr_path, "")
            return
        super().run(argv, **kwargs)
        if stderr_path is not None and any("cropdetect=" in item for item in command):
            self._write(
                stderr_path,
                "\n".join("[Parsed_cropdetect_0] crop=3840:2160:0:0" for _ in range(30))
                + "\n",
            )

    def run_pipeline(self, commands: Any, **kwargs: Any) -> None:
        super().run_pipeline(commands, **kwargs)
        final = tuple(os.fspath(item) for item in commands[-1])
        if final[0] == "hdr10plus_tool":
            target = Path(final[final.index("-o") + 1])
            readback = target.name == "encoded-hdr10plus.json"
            self._write(
                target,
                hdr10plus_document(self.rebuilt_frames if readback else self.frames),
            )
        elif final[0] == "dovi_tool":
            self._write(Path(final[final.index("-o") + 1]), b"rpu")


def uhd_job(context, runner: HdrRunner, *, plus: bool = False, dolby: int | None = None):
    database, settings, scan, scanner, _runner, worker = context
    static = HdrStaticMetadata(_HDR10_MASTERING, 1000, 400)
    uhd = _uhd_hdr10_scan(scan, static)
    stream = uhd.playlists[0].video_streams[0]
    video = replace(
        stream.video,
        hdr10_plus=plus,
        dolby_vision=dolby is not None,
        dolby_vision_profile=dolby,
    )
    uhd = replace(
        uhd,
        playlists=(
            replace(uhd.playlists[0], streams=(replace(stream, video=video),)),
        ),
    )
    scanner.result = uhd
    worker.runner_factory = lambda _paths: runner
    worker._runners.clear()
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    return database, settings, worker, uhd, job


def uhd_job_with_enhancement_layer(context, runner: HdrRunner, *, el_type: str = "MEL"):
    """A profile 7 disc: the base layer plus the secondary 1080p stream that holds the RPUs."""

    from bdencode.media.bluray import MediaStream, StreamKind, VideoCodec, VideoProperties

    database, settings, scan, scanner, _runner, worker = context
    static = HdrStaticMetadata(_HDR10_MASTERING, 1000, 400)
    uhd = _uhd_hdr10_scan(scan, static)
    base = uhd.playlists[0].video_streams[0]
    base = replace(
        base,
        video=replace(
            base.video, dolby_vision=True, dolby_vision_profile=7,
            dolby_vision_el_stream_id="video:4117", dolby_vision_el_type=el_type,
        ),
    )
    enhancement = MediaStream(
        id="video:4117", index=1, pid=4117, kind=StreamKind.VIDEO, codec="hevc",
        video=VideoProperties(
            codec=VideoCodec.HEVC, width=1920, height=1080, frame_rate=base.video.frame_rate,
            hdr10=True, hdr10_base_layer=True,
        ),
    )
    uhd = replace(uhd, playlists=(replace(uhd.playlists[0], streams=(base, enhancement)),))
    scanner.result = uhd
    worker.runner_factory = lambda _paths: runner
    worker._runners.clear()
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    return database, settings, worker, uhd, job


def selection_with(mode: str, **updates: Any) -> dict[str, Any]:
    selection = _uhd_hdr10_selection()
    selection["video"]["dynamic_hdr"] = mode
    selection.update(updates)
    return selection


def encode_command(runner: HdrRunner) -> tuple[str, ...]:
    return next(
        command
        for command in runner.commands
        if command[0] == "ffmpeg" and command[-1].endswith("video-encoded.partial.mkv")
    )


def x265_params(command: tuple[str, ...]) -> str:
    return command[command.index("-x265-params") + 1]


def location(paths: JobPaths, name: str) -> str:
    """Where x265 is told to read metadata (POSIX in production)."""

    if os.name == "nt":
        return (Path("/data/jobs") / name).as_posix()
    return (paths.work / "dynamic-hdr" / name).as_posix()


@pytest.fixture(autouse=True)
def tools_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "dynamic_hdr_support", lambda: AVAILABLE)
    if os.name == "nt":
        # x265 parameter strings cannot carry a drive-letter colon (the guard
        # rightly refuses it); production paths are POSIX, so present the test
        # metadata files under a POSIX location.
        real = hdr_dynamic.x265_dynamic_params
        monkeypatch.setattr(
            worker_module,
            "x265_dynamic_params",
            lambda plan, metadata: real(plan, Path("/data/jobs") / metadata.name),
        )


def test_hdr10plus_metadata_is_extracted_verified_and_handed_to_x265(context) -> None:
    runner = HdrRunner()
    database, settings, worker, _uhd, job = uhd_job(context, runner, plus=True)
    ready = database.set_selection(job.id, selection_with("hdr10plus"))

    prepared = worker.process_one_stage(ready)

    assert prepared.state is JobState.ENCODING
    paths = JobPaths.create(settings, job.id)
    report = json.loads((paths.analysis / "dynamic-hdr.json").read_text("utf-8"))
    assert report["status"] == "retained" and report["plan"]["mode"] == "hdr10plus"
    assert report["summary"]["frames"] == report["output_frames"] == FAKE_REFERENCE_FRAMES
    assert report["tool"] == "hdr10plus_tool" and len(report["metadata"]["sha256"]) == 64
    extraction = [c for c in runner.commands if c[0] == "hdr10plus_tool"]
    assert len(extraction) == 1 and extraction[0][:2] == ("hdr10plus_tool", "extract")
    plan = json.loads(paths.plan_json.read_text("utf-8"))
    assert plan["decisions"]["dynamic_hdr_retained"] is True
    assert plan["decisions"]["dolby_vision_retained"] is False
    assert any("HDR10+ metadata will be retained" in item for item in plan["warnings"])

    encoded = worker.process_one_stage(prepared)
    assert encoded.state is JobState.MUXING
    params = x265_params(encode_command(runner)).split(":")
    # Debian's libx265 has no HDR10+ support and would drop these; the metadata is injected instead.
    assert not [item for item in params if item.startswith("dhdr10")]
    assert "hdr10=1" in params  # the static layer is still written
    order = [
        next(i for i, c in enumerate(runner.commands) if match(c))
        for match in (
            lambda c: c[0] == "ffmpeg" and c[-1].endswith("encoded.hevc"),
            lambda c: c[0] == "hdr10plus_tool" and "inject" in c,
            lambda c: c[0] == "mkvextract",
            lambda c: c[0] == "mkvmerge" and "--timestamps" in c,
            lambda c: c[0] == "mkvpropedit",
            lambda c: c[0] == "hdr10plus_tool" and "extract" in c and c[-2].endswith("encoded-hdr10plus.json"),
        )
    ]
    assert order == sorted(order)
    injection = next(c for c in runner.commands if c[0] == "hdr10plus_tool" and "inject" in c)
    assert injection[injection.index("-j") + 1].endswith("hdr10plus.json")
    assert paths.encoded_video.is_file()
    assert not list((paths.work / "dynamic-hdr").glob("encoded*"))
    events = [e.kind for e in database.list_events(job_id=job.id)]
    assert events.count("worker.dynamic-hdr") == 1


def test_hdr10plus_that_misses_frames_after_injection_needs_review(context) -> None:
    runner = HdrRunner(rebuilt_frames=FAKE_REFERENCE_FRAMES - 1)
    database, settings, worker, _uhd, job = uhd_job(context, runner, plus=True)
    ready = database.set_selection(job.id, selection_with("hdr10plus"))
    prepared = worker.process_one_stage(ready)

    with pytest.raises(ReviewRequired, match="HDR10\\+ metadata could not be attached"):
        worker.process_one_stage(prepared)

    paths = JobPaths.create(settings, job.id)
    assert not paths.encoded_video.exists()
    assert not list((paths.work / "dynamic-hdr").glob("encoded*"))


def test_metadata_frame_count_mismatch_sends_the_job_to_review(context) -> None:
    runner = HdrRunner(frames=FAKE_REFERENCE_FRAMES - 3)
    database, settings, worker, _uhd, job = uhd_job(context, runner, plus=True)
    ready = database.set_selection(job.id, selection_with("hdr10plus"))

    result = worker.process_job(ready)

    assert result.state is JobState.NEEDS_REVIEW
    assert not (JobPaths.create(settings, job.id).analysis / "dynamic-hdr.json").exists()
    assert not any(
        c[0] == "ffmpeg" and c[-1].endswith("video-encoded.partial.mkv")
        for c in runner.commands
    )


def test_dolby_vision_profile_7_is_converted_bounded_and_verified(context) -> None:
    runner = HdrRunner()
    database, settings, worker, _uhd, job = uhd_job(context, runner, dolby=7)
    ready = database.set_selection(job.id, selection_with("dolby_vision"))

    prepared = worker.process_one_stage(ready)

    paths = JobPaths.create(settings, job.id)
    report = json.loads((paths.analysis / "dynamic-hdr.json").read_text("utf-8"))
    assert report["plan"]["convert_mode"] == 2 and report["summary"]["profile"] == 8
    extraction = next(
        c for c in runner.commands if c[0] == "dovi_tool" and "extract-rpu" in c
    )
    assert extraction[:3] == ("dovi_tool", "-m", "2")
    assert "-c" not in extraction  # no crop, the active area stays as authored

    worker.process_one_stage(prepared)
    params = x265_params(encode_command(runner)).split(":")
    assert "dolby-vision-profile=8.1" in params
    assert f"dolby-vision-rpu={location(paths, 'rpu.bin')}" in params
    assert {"aud=1", "repeat-headers=1", "hrd=1"} <= set(params)
    assert "vbv-maxrate=160000" in params and "vbv-bufsize=160000" in params
    _scan, effective = worker._load_prepared_scan_and_selection(prepared, paths)
    assert effective.settings.vbv is not None

    # libx265 under FFmpeg cannot read the RPU file, so it is injected afterwards, in this order,
    # and read back from the rebuilt stream before the encode checkpoint is accepted.
    order = [
        next(i for i, c in enumerate(runner.commands) if match(c))
        for match in (
            lambda c: c[0] == "ffmpeg" and c[-1].endswith("encoded.hevc"),
            lambda c: c[0] == "dovi_tool" and "inject-rpu" in c,
            lambda c: c[0] == "mkvextract",
            lambda c: c[0] == "mkvmerge" and "--timestamps" in c,
            lambda c: c[0] == "mkvpropedit",
            lambda c: c[0] == "dovi_tool" and "extract-rpu" in c and c[-2].endswith("encoded-rpu.bin"),
        )
    ]
    assert order == sorted(order)
    injection = next(c for c in runner.commands if c[0] == "dovi_tool" and "inject-rpu" in c)
    assert injection[injection.index("--rpu-in") + 1].endswith("rpu.bin")
    rebuild = next(c for c in runner.commands if c[0] == "mkvmerge" and "--timestamps" in c)
    assert rebuild[rebuild.index("--colour-primaries") + 1] == "0:9"  # the container keeps its colour description
    assert next(c for c in runner.commands if c[0] == "mkvpropedit")[-1] == "default-duration=41708333"
    assert paths.encoded_video.is_file()
    assert not list((paths.work / "dynamic-hdr").glob("encoded*"))  # scratch files are removed


def test_an_rpu_stream_that_misses_frames_after_injection_needs_review(context) -> None:
    runner = HdrRunner(rebuilt_frames=FAKE_REFERENCE_FRAMES - 2)
    database, settings, worker, _uhd, job = uhd_job(context, runner, dolby=8)
    ready = database.set_selection(job.id, selection_with("dolby_vision"))
    prepared = worker.process_one_stage(ready)

    with pytest.raises(ReviewRequired, match="could not be attached"):
        worker.process_one_stage(prepared)

    paths = JobPaths.create(settings, job.id)
    assert not paths.encoded_video.exists()  # no checkpoint for an encode without its RPUs
    assert not list((paths.work / "dynamic-hdr").glob("encoded*"))


def test_explicit_retention_without_the_tool_needs_review_but_auto_falls_back(
    context, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = {
        key: {**value, "tool_available": False, "available": False}
        for key, value in AVAILABLE.items()
    }
    monkeypatch.setattr(worker_module, "dynamic_hdr_support", lambda: missing)
    runner = HdrRunner()
    database, settings, worker, _uhd, job = uhd_job(context, runner, plus=True)

    explicit = database.set_selection(job.id, selection_with("hdr10plus"))
    result = worker.process_job(explicit)
    assert result.state is JobState.NEEDS_REVIEW
    assert not [c for c in runner.commands if c[0] == "hdr10plus_tool"]

    automatic = database.set_selection(job.id, selection_with("auto"))
    prepared = worker.process_one_stage(automatic)
    assert prepared.state is JobState.ENCODING
    report = json.loads(
        (JobPaths.create(settings, job.id).analysis / "dynamic-hdr.json").read_text("utf-8")
    )
    assert report["status"] == "discarded" and "hdr10plus_tool" in report["plan"]["reason"]
    worker.process_one_stage(prepared)
    assert "dhdr10" not in x265_params(encode_command(runner))
    assert not [c for c in runner.commands if c[0] == "hdr10plus_tool"]


def test_default_jobs_never_touch_the_dynamic_hdr_stage(context) -> None:
    runner = HdrRunner()
    database, settings, worker, _uhd, job = uhd_job(context, runner, plus=True)
    ready = database.set_selection(job.id, _uhd_hdr10_selection())

    prepared = worker.process_one_stage(ready)
    worker.process_one_stage(prepared)

    paths = JobPaths.create(settings, job.id)
    assert not (paths.analysis / "dynamic-hdr.json").exists()
    assert not (paths.stages / "dynamic-hdr.json").exists()
    assert "dhdr10" not in x265_params(encode_command(runner))
    assert not [c for c in runner.commands if c[0] in {"hdr10plus_tool", "dovi_tool"}]


def test_selection_is_rejected_early_when_retention_is_impossible(context) -> None:
    runner = HdrRunner()
    database, _settings, worker, uhd, job = uhd_job(context, runner, plus=False)
    base = database.get_job(job.id)

    def parsed(selection: dict[str, Any], scan=uhd):
        return parse_selection(base.model_copy(update={"selection": selection}), scan)

    assert parsed(selection_with("discard")).dynamic_hdr.value == "discard"
    assert parsed(selection_with("auto")).dynamic_hdr.value == "auto"
    for mode, code in (
        ("hdr10plus", "dynamic_hdr_source_missing"),
        ("dolby_vision", "dynamic_hdr_source_missing"),
        ("sometimes", "dynamic_hdr_invalid_mode"),
    ):
        with pytest.raises(ReviewRequired) as error:
            parsed(selection_with(mode))
        assert error.value.details["code"] == code

    ivtc = selection_with("hdr10plus")
    ivtc["video"]["temporal_filter"] = "ivtc_tff"
    plus_scan = context[3].result
    with pytest.raises(ReviewRequired):
        parsed(ivtc, plus_scan)


# -- the finished MKV must prove the retained layer ---------------------------------


class QcRunner(HdrRunner):
    dynamic_side_data: list = []
    stream_side_data: list = []

    def run(self, argv, **kwargs):
        command = tuple(os.fspath(item) for item in argv)
        stdout_path = kwargs.get("stdout_path")
        stderr_path = kwargs.get("stderr_path")
        if (
            stdout_path is not None and command[0] == "mkvmerge" and "--identify" in command
            and not command[-1].endswith("video-encoded.partial.mkv")
        ):
            self.commands.append(command)
            self._write(stdout_path, json.dumps({
                "container": {"properties": {"title": "Movie.2026.2160p.UHD.BluRay.x265"}},
                "tracks": [{"id": 0, "type": "video", "properties": {"default_track": True, "forced_track": False}}],
                "attachments": [],
            }))
            if stderr_path is not None: self._write(stderr_path, "")
            return
        if stdout_path is not None and stdout_path.name == "ffprobe-streams.json":
            self.commands.append(command)
            self._write(stdout_path, json.dumps({"streams": [{
                "index": 0, "codec_name": "hevc", "profile": "Main 10", "codec_type": "video",
                "start_time": "0.000000", "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le",
                "color_range": "tv", "color_space": "bt2020nc", "color_transfer": "smpte2084",
                "color_primaries": "bt2020", "chroma_location": "left",
                "side_data_list": list(self.stream_side_data)}]}))
            if stderr_path is not None: self._write(stderr_path, "")
            return
        if stdout_path is not None and stdout_path.name == "ffprobe-video-side-data.json":
            self.commands.append(command)
            static = [
                {"side_data_type": "Mastering display metadata", "green_x": "13250/50000", "green_y": "34500/50000",
                 "blue_x": "7500/50000", "blue_y": "3000/50000", "red_x": "34000/50000", "red_y": "16000/50000",
                 "white_point_x": "15635/50000", "white_point_y": "16450/50000",
                 "max_luminance": "10000000/10000", "min_luminance": "1/10000"},
                {"side_data_type": "Content light level metadata", "max_content": 1000, "max_average": 400},
            ]
            self._write(stdout_path, json.dumps({"frames": [{"side_data_list": static + list(self.dynamic_side_data)}]}))
            if stderr_path is not None: self._write(stderr_path, "")
            return
        super().run(argv, **kwargs)


def run_to_qc(context, *, mode: str, dynamic, stream_extra=(), plus=False, dolby=None):
    runner = QcRunner()
    runner.dynamic_side_data = list(dynamic)
    runner.stream_side_data = list(stream_extra)
    database, settings, worker, _uhd, job = uhd_job(
        context, runner, plus=plus, dolby=dolby
    )
    ready = database.set_selection(job.id, selection_with(mode))
    state = worker.process_one_stage(ready)
    for _ in range(3):
        state = worker.process_one_stage(state)
    return state


HDR10PLUS_SIDE_DATA = {"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}


def test_qc_accepts_a_stream_that_carries_the_retained_hdr10plus(context) -> None:
    state = run_to_qc(context, mode="hdr10plus", dynamic=[HDR10PLUS_SIDE_DATA], plus=True)
    assert state.state is JobState.COMPARISON


def test_qc_rejects_a_stream_x265_silently_encoded_without_the_metadata(context) -> None:
    with pytest.raises(ReviewRequired) as error:
        run_to_qc(context, mode="hdr10plus", dynamic=[], plus=True)
    assert any("smpte2094-40" in item for item in error.value.details["errors"])


def test_qc_still_forbids_dynamic_hdr_the_job_did_not_ask_for(context) -> None:
    with pytest.raises(ReviewRequired) as error:
        run_to_qc(context, mode="discard", dynamic=[HDR10PLUS_SIDE_DATA], plus=True)
    assert any("hdr10+" in item for item in error.value.details["errors"])


DOVI_RECORD = {
    "side_data_type": "DOVI configuration record",
    "dv_profile": 8,
    "dv_bl_signal_compatibility_id": 1,
    "rpu_present_flag": 1,
}
DOVI_FRAME = {"side_data_type": "Dolby Vision Metadata"}


def test_qc_accepts_a_correct_dolby_vision_configuration_record(context) -> None:
    state = run_to_qc(
        context,
        mode="dolby_vision",
        dynamic=[DOVI_FRAME],
        stream_extra=[DOVI_RECORD],
        dolby=8,
    )
    assert state.state is JobState.COMPARISON


def test_qc_rejects_dolby_vision_without_its_configuration_record(context) -> None:
    with pytest.raises(ReviewRequired) as error:
        run_to_qc(context, mode="dolby_vision", dynamic=[DOVI_FRAME], dolby=8)
    assert any(
        "dovi configuration record" in item for item in error.value.details["errors"]
    )


def test_profile_7_reads_the_rpu_from_the_enhancement_layer_and_reports_the_plan(context) -> None:
    runner = HdrRunner()
    database, settings, worker, _uhd, job = uhd_job_with_enhancement_layer(context, runner)
    ready = database.set_selection(job.id, selection_with("auto"))

    prepared = worker.process_one_stage(ready)

    paths = JobPaths.create(settings, job.id)
    report = json.loads((paths.analysis / "dynamic-hdr.json").read_text("utf-8"))
    assert report["plan"]["mode"] == "dolby_vision" and report["plan"]["source_profile"] == 7
    assert report["plan"]["el_video_ordinal"] == 1 and report["plan"]["convert_mode"] == 2
    # (the scan stage also probed the secondary stream; the retained RPU is the one converted with -m 2)
    position = next(
        i for i, c in enumerate(runner.commands)
        if c[0] == "dovi_tool" and "extract-rpu" in c and "-m" in c
    )
    source, extract = runner.commands[position - 1], runner.commands[position]
    assert source[source.index("-map") + 1] == "0:v:1"  # the enhancement layer, not the base layer
    assert extract[:3] == ("dovi_tool", "-m", "2")
    assert prepared.state is JobState.ENCODING
