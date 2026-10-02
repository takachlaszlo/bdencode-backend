#!/usr/bin/env python3
"""End-to-end run of the worker pipeline on a synthetic HDR10 "disc" with the real media tools.

Everything runs for real (ffmpeg, x265, vspipe/VapourSynth, libvmaf through bdencode-vmaf,
mkvmerge, ffprobe, the comparison stage) except the one step that needs a physical Blu-ray:
the disc scan and the libbluray remux. Those are replaced by a fake ``DiscScan`` that describes a
HDR10 title (1280x720 unless --size says otherwise) and by a runner that turns the remux into a stream copy of the synthetic master.

This is the check that found four real defects the unit tests could not see (colour conversion
in the encoder pipe, 10-bit Y4M, slow CRF probes, FIFOs in the job tree). Run it after changing
the encode, QC or comparison code and after updating ffmpeg, x265, VapourSynth or libvmaf:

    python tools/e2e/synthetic_disc.py --work /tmp/bdencode-e2e

It needs ``ffmpeg`` (with libx265), ``ffprobe``, ``mkvmerge``, ``vspipe`` with BestSource and the
standalone ``vmaf`` in PATH (the installed tool runtime provides them) and takes about 8 minutes;
``--size 3840x2160 --scenes 12`` exercises real UHD resolution.
Exit status: 0 completed, 1 pipeline did not complete, 77 required tools missing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REQUIRED_TOOLS = ("ffmpeg", "ffprobe", "mkvmerge", "vspipe", "vmaf")
MASTERING = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)"
SOURCES = ("testsrc2", "smptehdbars", "rgbtestsrc", "gradients", "testsrc", "yuvtestsrc")
SCENE_SECONDS = 2.000633  # two seconds of 24000/1001 video: 48 frames


def missing_tools() -> list[str]:
    return [name for name in REQUIRED_TOOLS if shutil.which(name) is None]


def encoders_ok() -> bool:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, check=False)
    return "libx265" in result.stdout


def make_master(path: Path, *, size: str = "1280x720", scenes: int = 30, noise: int = 1) -> None:
    """Alternating bright/black two-second scenes (hard cuts give shared I frames)."""

    args: list[str] = []
    chain, labels = "", ""
    for index in range(scenes):
        if index % 2 == 0:
            source = SOURCES[(index // 2) % len(SOURCES)]
            args += ["-f", "lavfi", "-i", f"{source}=size={size}:rate=24000/1001:duration=2"]
        else:
            args += ["-f", "lavfi", "-i", f"color=c=black:size={size}:rate=24000/1001:duration=2"]
        chain += f"[{index}:v]format=yuv420p10le,setsar=1[v{index}];"
        labels += f"[v{index}]"
    args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={scenes * 2}"]
    graph = f"{chain}{labels}concat=n={scenes}:v=1:a=0[cat];[cat]noise=alls={noise}:allf=t,format=yuv420p10le[vid]"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, "-filter_complex", graph,
         "-map", "[vid]", "-map", f"{scenes}:a", "-c:v", "libx265", "-preset", "veryfast", "-crf", "0",
         "-pix_fmt", "yuv420p10le", "-x265-params",
         f"colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:master-display={MASTERING}"
         ":max-cll=1000,400:hdr10=1:repeat-headers=1:log-level=error",
         "-c:a", "ac3", "-b:a", "192k", "-ac", "2", str(path)],
        check=True,
    )


def write_config(work: Path, source_root: Path) -> Path:
    config = work / "config.toml"
    config.write_text(
        "[bdencode]\n"
        f'data_root = "{(work / "data").as_posix()}"\n'
        f'source_roots = ["{source_root.as_posix()}"]\n'
        'bind_host = "127.0.0.1"\nbind_port = 8796\napi_root_path = "/encoder"\n'
        "worker_poll_seconds = 2.0\ncpu_limit_percent = 80\ncomparison_pair_count = 24\n"
        'log_level = "INFO"\n',
        encoding="utf-8",
    )
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", type=Path, help="scratch directory (default: a temporary one)")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    parser.add_argument("--preset", default="fast")
    parser.add_argument("--size", default="1280x720", help="picture size, for example 3840x2160 for UHD")
    parser.add_argument("--scenes", type=int, default=30, help="number of two-second scenes (UHD: 12 is plenty)")
    parser.add_argument("--auto-crf", type=float, default=90.0, help="VMAF target; 0 uses a fixed CRF 18")
    args = parser.parse_args()
    if not re.fullmatch(r"\d{3,5}x\d{3,5}", args.size) or args.scenes < 4:
        parser.error("--size must look like 3840x2160 and --scenes must be at least 4")
    width, height = (int(part) for part in args.size.split("x"))
    duration = round(args.scenes * SCENE_SECONDS, 3)

    missing = missing_tools()
    if missing or not encoders_ok():
        print("missing tools:", ", ".join(missing + ([] if encoders_ok() else ["ffmpeg libx265"])), file=sys.stderr)
        return 77

    work = (args.work or Path(tempfile.mkdtemp(prefix="bdencode-e2e-"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    source_root = work / "source"
    disc = source_root / "SyntheticUHD"
    (disc / "BDMV" / "STREAM").mkdir(parents=True, exist_ok=True)
    (disc / "BDMV" / "PLAYLIST").mkdir(exist_ok=True)
    master = work / "synthetic-hdr10-master.mkv"
    started = time.time()
    print(f"[{time.time() - started:6.1f}s] making the synthetic master", flush=True)
    make_master(master, size=args.size, scenes=args.scenes)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(master), "-map", "0:v", "-map", "0:a",
         "-c", "copy", "-f", "mpegts", str(disc / "BDMV" / "STREAM" / "00000.m2ts")],
        check=True,
    )
    os.environ["BDENCODE_CONFIG"] = str(write_config(work, source_root))

    from bdencode.config import load_settings
    from bdencode.db import Database
    from bdencode.media.bluray import (
        ContentKind, DiscKind, DiscScan, HdrStaticMetadata, MediaStream, PlaylistCandidate,
        PlaylistSegment, StreamKind, ToolCapabilities, VideoCodec, VideoProperties,
    )
    from bdencode.models import ContentType, DiscType, JobCreate, JobState
    from bdencode.process import CommandRunner
    from bdencode.queue import JobQueue
    from bdencode.worker import PipelineWorker

    video = MediaStream(
        id="video:4113", index=0, pid=4113, kind=StreamKind.VIDEO, codec="hevc",
        video=VideoProperties(
            codec=VideoCodec.HEVC, width=width, height=height, frame_rate="24000/1001",
            field_order="progressive", bit_depth=10, pixel_format="yuv420p10le",
            color_primaries="bt2020", color_transfer="smpte2084", color_matrix="bt2020nc",
            hdr10=True, hdr10_static=HdrStaticMetadata(MASTERING, 1000, 400), hdr10_base_layer=True,
        ),
    )
    audio = MediaStream(
        id="audio:4352", index=1, pid=4352, kind=StreamKind.AUDIO, codec="ac3", channels=2,
        channel_layout="stereo", sample_rate=48000, default=True,
    )
    playlist = PlaylistCandidate(
        playlist_id="00001", duration_seconds=duration,
        segments=(PlaylistSegment(clip_id="00000", in_time_seconds=0.0, out_time_seconds=duration),),
        streams=(video, audio), recommended=True,
    )
    scan = DiscScan(
        source=disc, disc_kind=DiscKind.UHD, content_kind=ContentKind.FILM, playlists=(playlist,),
        capabilities=ToolCapabilities(), fingerprint="c" * 64,
    )

    class FakeScanner:
        def scan(self, source: Path, *, content_kind: ContentKind) -> DiscScan:
            return scan

    class DiscRunner(CommandRunner):
        """Real runner; only the libbluray remux is redirected to the synthetic master."""

        def run(self, argv, **kwargs):  # type: ignore[override]
            text = [os.fspath(item) for item in argv]
            if any(item.startswith("bluray:") for item in text):
                argv = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-i", str(master), "-map", "0",
                        "-map_metadata", "-1", "-map_chapters", "0", "-c", "copy", "-avoid_negative_ts",
                        "make_zero", "-max_interleave_delta", "0", "-y", text[-1]]
            return super().run(argv, **kwargs)

    settings = load_settings()
    settings.create_directories()
    database = Database(settings.resolved_database_path)
    database.initialize()
    worker = PipelineWorker(
        database, settings, scanner_factory=lambda _s: FakeScanner(),
        runner_factory=lambda paths: DiscRunner(paths.logs / "commands.jsonl"),
    )
    queue = JobQueue(database)
    job = queue.enqueue(JobCreate(source_path=str(disc.resolve()), name="Synthetic", disc_type=DiscType.UHD,
                                  content_type=ContentType.FILM))
    claimed = queue.claim_next()
    assert claimed is not None and claimed.id == job.id
    job = worker.process_one_stage(claimed)
    print(f"[{time.time() - started:6.1f}s] after scan: {job.state.value}", flush=True)

    video_selection: dict = {
        "detail_level": "advanced", "settings": {"preset": args.preset},
        "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0}, "temporal_filter": "progressive",
        "dynamic_hdr": "auto",
    }
    if args.auto_crf:
        video_selection["auto_crf"] = {
            "enabled": True, "target_vmaf": args.auto_crf, "samples": 6, "sample_seconds": 3.0,
            "probe_preset": "veryfast", "min_crf": 12.0, "max_crf": 18.0,
        }
    else:
        video_selection["settings"]["crf"] = 18
    selection = {
        "playlist_id": "00001", "angle": 1, "video": video_selection,
        "tracks": [{"stream_id": "audio:4352", "action": "omit"}],
        "output_name": f"Synthetic.Finish.2026.{height}p.UHD.BluRay.x265-TEST", "upload_images": False,
    }
    job = database.set_selection(job.id, selection)
    final = worker.process_job(job)
    elapsed = time.time() - started
    print(f"[{elapsed:6.1f}s] FINAL {final.state.value}", flush=True)
    report = settings.job_root(final.id) / "analysis" / "crf-search.json"
    if report.is_file():
        print(json.dumps(json.loads(report.read_text(encoding="utf-8")).get("probes"), indent=None))
    for event in database.list_events(job_id=final.id, limit=5000)[-6:]:
        print("  ", event.kind, (event.message or "")[:140])
    if not args.keep and args.work is None:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if final.state is JobState.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
