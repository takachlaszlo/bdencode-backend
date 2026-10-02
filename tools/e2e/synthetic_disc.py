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
``--size 3840x2160 --scenes 12`` exercises real UHD resolution, ``--dolby-vision`` (needs ``dovi_tool``)
a generated Dolby Vision profile 8.1 source and ``--hdr10plus`` (needs ``hdr10plus_tool``) a generated
HDR10+ source whose dynamic metadata must come out frame-exactly.
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


def run(argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, capture_output=True, text=True, **options)  # type: ignore[call-overload]


def make_dolby_vision_master(master: Path, work: Path) -> Path:
    """A profile 8.1 source: the HDR10 master with a generated per-frame RPU injected into its HEVC stream."""

    frames = int(run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                      "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(master)]).stdout.strip())
    config = work / "dovi-generate.json"
    config.write_text(json.dumps({
        "cm_version": "V29", "length": frames,
        "level6": {"max_display_mastering_luminance": 1000, "min_display_mastering_luminance": 1,
                   "max_content_light_level": 1000, "max_frame_average_light_level": 400},
        "default_metadata_blocks": [{"Level1": {"min_pq": 0, "max_pq": 3079, "avg_pq": 819}}],
    }), encoding="utf-8")
    rpu, base, injected = work / "generated-rpu.bin", work / "base.hevc", work / "dv.hevc"
    run(["dovi_tool", "generate", "-j", str(config), "-o", str(rpu)])
    run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(master), "-map", "0:v:0",
         "-c", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(base)])
    run(["dovi_tool", "inject-rpu", "-i", str(base), "--rpu-in", str(rpu), "-o", str(injected)])
    video = work / "dv-video.mkv"
    run(["mkvmerge", "-q", "-o", str(video), "--default-duration", "0:24000/1001p", str(injected)])
    result = work / "synthetic-dolby-vision-master.mkv"
    run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(video), "-i", str(master),
         "-map", "0:v", "-map", "1:a", "-c", "copy", str(result)])
    return result


def make_hdr10plus_master(master: Path, work: Path) -> Path:
    """The HDR10+ source: the HDR10 master with generated per-frame dynamic metadata injected."""

    frames = int(run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                      "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(master)]).stdout.strip())
    scene = 48
    entries = [{
        "BezierCurveData": {"Anchors": [102, 205, 307, 410, 512, 614, 717, 819, 921], "KneePointX": 0, "KneePointY": 0},
        "LuminanceParameters": {
            "AverageRGB": 400 + index % scene,
            "LuminanceDistributions": {
                "DistributionIndex": [1, 5, 10, 25, 50, 75, 90, 95, 99],
                "DistributionValues": [100, 120, 150, 300, 600, 900, 1500, 2500, 4000],
            },
            "MaxScl": [8000, 8000, 8000],
        },
        "NumberOfWindows": 1, "TargetedSystemDisplayMaximumLuminance": 400,
        "SceneFrameIndex": index % scene, "SequenceFrameIndex": index, "SceneId": index // scene,
    } for index in range(frames)]
    starts = list(range(0, frames, scene))
    metadata = work / "hdr10plus-generated.json"
    metadata.write_text(json.dumps({
        "JSONInfo": {"HDR10plusProfile": "B", "Version": "1.0"},
        "SceneInfo": entries,
        "SceneInfoSummary": {"SceneFirstFrameIndex": starts,
                             "SceneFrameNumbers": [min(scene, frames - start) for start in starts]},
        "ToolInfo": {"Tool": "synthetic", "Version": "1"},
    }), encoding="utf-8")
    base, injected = work / "hdr10plus-base.hevc", work / "hdr10plus.hevc"
    run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(master), "-map", "0:v:0",
         "-c", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(base)])
    run(["hdr10plus_tool", "inject", "-i", str(base), "-j", str(metadata), "-o", str(injected)])
    video = work / "hdr10plus-video.mkv"
    run(["mkvmerge", "-q", "-o", str(video), "--default-duration", "0:24000/1001p", str(injected)])
    result = work / "synthetic-hdr10plus-master.mkv"
    run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(video), "-i", str(master),
         "-map", "0:v", "-map", "1:a", "-c", "copy", str(result)])
    return result


def hdr10plus_frames(path: Path) -> int:
    """Number of frames with HDR10+ metadata inside an MKV, read the way the worker reads it."""

    target = path.with_suffix(".hdr10plus.json")
    extract = subprocess.run(
        ["bash", "-c", 'ffmpeg -hide_banner -nostdin -v error -i "$1" -map 0:v:0 -c copy '
         '-bsf:v hevc_mp4toannexb -f hevc - | hdr10plus_tool extract -o "$2" -', "_", str(path), str(target)],
        capture_output=True, text=True, check=False,
    )
    if extract.returncode != 0 or not target.is_file():
        return 0
    return len(json.loads(target.read_text(encoding="utf-8")).get("SceneInfo", []))


def dolby_vision_summary(path: Path) -> dict[str, int]:
    """Frame count and profile of the RPU stream inside an MKV, read the way the worker reads it."""

    extract = subprocess.run(
        ["bash", "-c", 'ffmpeg -hide_banner -nostdin -v error -i "$1" -map 0:v:0 -c copy '
         '-bsf:v hevc_mp4toannexb -f hevc - | dovi_tool extract-rpu -o "$2" -', "_", str(path),
         str(path.with_suffix(".rpu.bin"))],
        capture_output=True, text=True, check=False,
    )
    if extract.returncode != 0:
        return {"frames": 0, "profile": 0}
    text = run(["dovi_tool", "info", "-i", str(path.with_suffix(".rpu.bin")), "--summary"]).stdout
    frames = re.search(r"Frames:\s*(\d+)", text)
    profile = re.search(r"Profile:\s*(\d+)", text)
    return {"frames": int(frames[1]) if frames else 0, "profile": int(profile[1]) if profile else 0}


def probe_master(path: Path) -> dict:
    """Size, duration, base-layer frame count and number of video streams of a real master."""

    document = json.loads(run(["ffprobe", "-v", "error", "-show_entries",
                               "stream=index,codec_type,codec_name,width,height", "-show_entries", "format=duration",
                               "-of", "json", str(path)]).stdout)
    videos = [stream for stream in document["streams"] if stream.get("codec_type") == "video"]
    frames = int(run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0", "-show_entries",
                      "stream=nb_read_packets", "-of", "csv=p=0", str(path)]).stdout.strip().rstrip(","))
    return {"width": int(videos[0]["width"]), "height": int(videos[0]["height"]), "videos": len(videos),
            "frames": frames, "duration": float(document["format"]["duration"])}


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
    parser.add_argument("--dolby-vision", action="store_true",
                        help="make the source a Dolby Vision profile 8.1 title and require it to be retained "
                        "(needs dovi_tool in PATH)")
    parser.add_argument("--hdr10plus", action="store_true",
                        help="make the source an HDR10+ title and require the dynamic metadata to be retained "
                        "(needs hdr10plus_tool in PATH)")
    parser.add_argument("--real-master", type=Path,
                        help="use this MKV instead of a synthetic master: a UHD base layer and, optionally, a Dolby "
                        "Vision enhancement layer as its second video stream (for example a short excerpt of a real "
                        "disc). The worker's own scan probe has to find the layer; the run is expected to produce "
                        "Dolby Vision profile 8 with one RPU per frame")
    parser.add_argument("--crop-bars", type=int, default=0,
                        help="rows of letterbox to crop at the top and the bottom (the RPU active area is zeroed)")
    parser.add_argument("--scenes", type=int, default=30, help="number of two-second scenes (UHD: 12 is plenty)")
    parser.add_argument("--auto-crf", type=float, default=90.0, help="VMAF target; 0 uses a fixed CRF 18")
    args = parser.parse_args()
    if not re.fullmatch(r"\d{3,5}x\d{3,5}", args.size) or args.scenes < 4:
        parser.error("--size must look like 3840x2160 and --scenes must be at least 4")
    width, height = (int(part) for part in args.size.split("x"))
    duration = round(args.scenes * SCENE_SECONDS, 3)

    if args.dolby_vision and args.hdr10plus:
        parser.error("--dolby-vision and --hdr10plus exclude each other")
    missing = missing_tools()
    for flag, tool in ((args.dolby_vision, "dovi_tool"), (args.hdr10plus, "hdr10plus_tool")):
        if flag and shutil.which(tool) is None:
            missing.append(tool)
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
    real = None
    if args.real_master:
        master = args.real_master.resolve()
        real = probe_master(master)
        # The playlist length is the frame-derived length, as on a real disc (the container's own duration
        # also counts audio and can differ by a few frames, which the completeness gate rightly questions).
        width, height = real["width"], real["height"]
        duration = round(real["frames"] * 1001 / 24000, 3)
        print(f"[{time.time() - started:6.1f}s] real master: {real}", flush=True)
    else:
        print(f"[{time.time() - started:6.1f}s] making the synthetic master", flush=True)
        make_master(master, size=args.size, scenes=args.scenes)
    if args.dolby_vision:
        print(f"[{time.time() - started:6.1f}s] injecting a generated Dolby Vision RPU", flush=True)
        master = make_dolby_vision_master(master, work)
        print("source RPU:", dolby_vision_summary(master), flush=True)
    if args.hdr10plus:
        print(f"[{time.time() - started:6.1f}s] injecting generated HDR10+ metadata", flush=True)
        master = make_hdr10plus_master(master, work)
        print("source HDR10+ frames:", hdr10plus_frames(master), flush=True)
    clip = disc / "BDMV" / "STREAM" / "00000.m2ts"
    if real is None:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(master), "-map", "0:v", "-map", "0:a",
             "-c", "copy", "-f", "mpegts", str(clip)],
            check=True,
        )
    else:
        # The remux is served from the real master; the disc only has to contain the clip.
        with master.open("rb") as stream:
            clip.write_bytes(stream.read(1 << 20))
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
            dolby_vision=args.dolby_vision, dolby_vision_profile=8 if args.dolby_vision else None,
            hdr10_plus=args.hdr10plus,
        ),
    )
    audio = MediaStream(
        id="audio:4352", index=1, pid=4352, kind=StreamKind.AUDIO, codec="ac3", channels=2,
        channel_layout="stereo", sample_rate=48000, default=True,
    )
    streams = (video, audio)
    if real is not None and real["videos"] > 1:
        enhancement = MediaStream(
            id="video:4117", index=2, pid=4117, kind=StreamKind.VIDEO, codec="hevc",
            video=VideoProperties(
                codec=VideoCodec.HEVC, width=1920, height=1080, frame_rate="24000/1001", field_order="unknown",
                bit_depth=10, pixel_format="yuv420p10le", color_primaries="bt2020", color_transfer="smpte2084",
                color_matrix="bt2020nc", hdr10=True, hdr10_base_layer=True,
            ),
        )
        streams = (video, enhancement, audio)
    playlist = PlaylistCandidate(
        playlist_id="00001", duration_seconds=duration,
        segments=(PlaylistSegment(clip_id="00000", in_time_seconds=0.0, out_time_seconds=duration),),
        streams=streams, recommended=True,
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

        def run_pipeline(self, commands, **kwargs):  # type: ignore[override]
            commands = [[os.fspath(item) for item in command] for command in commands]
            first = commands[0]
            if any(item.startswith("bluray:") for item in first):
                # The layer probe reads a stream of the playlist: serve it from the master instead.
                cleaned: list[str] = []
                skip = False
                for item in first:
                    if skip:
                        skip = False
                    elif item == "-playlist":
                        skip = True
                    else:
                        cleaned.append(str(master) if item.startswith("bluray:") else item)
                commands[0] = cleaned
            return super().run_pipeline(commands, **kwargs)

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
        "crop": {"left": 0, "top": args.crop_bars, "right": 0, "bottom": args.crop_bars},
        "temporal_filter": "progressive",
        "dynamic_hdr": "dolby_vision" if args.dolby_vision else "hdr10plus" if args.hdr10plus else "auto",
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
        "output_name": "Synthetic.Finish.2026.2160p.UHD.BluRay.x265-TEST", "upload_images": False,
    }
    job = database.set_selection(job.id, selection)
    final = worker.process_job(job)
    elapsed = time.time() - started
    print(f"[{elapsed:6.1f}s] FINAL {final.state.value}", flush=True)
    report = settings.job_root(final.id) / "analysis" / "crf-search.json"
    if report.is_file():
        print(json.dumps(json.loads(report.read_text(encoding="utf-8")).get("probes"), indent=None))
    status = 0 if final.state is JobState.COMPLETED else 1
    if args.dolby_vision and final.state is JobState.COMPLETED:
        output = next((settings.completed_root).rglob("*.mkv"), None)
        summary = dolby_vision_summary(output) if output else {"frames": 0, "profile": 0}
        source = dolby_vision_summary(master)
        print("output RPU:", summary, "source RPU:", source, flush=True)
        if summary["profile"] != 8 or summary["frames"] != source["frames"] or summary["frames"] == 0:
            print("the Dolby Vision RPU was not retained frame-exactly", file=sys.stderr)
            status = 1
    if real is not None and real["videos"] > 1 and final.state is JobState.COMPLETED:
        output = next((settings.completed_root).rglob("*.mkv"), None)
        summary = dolby_vision_summary(output) if output else {"frames": 0, "profile": 0}
        print("output RPU:", summary, "base-layer frames:", real["frames"], flush=True)
        if summary["profile"] != 8 or summary["frames"] != real["frames"]:
            print("the Dolby Vision RPU of the dual-layer source was not retained frame-exactly", file=sys.stderr)
            status = 1
    if args.hdr10plus and final.state is JobState.COMPLETED:
        output = next((settings.completed_root).rglob("*.mkv"), None)
        retained = hdr10plus_frames(output) if output else 0
        expected = hdr10plus_frames(master)
        print("output HDR10+ frames:", retained, "source HDR10+ frames:", expected, flush=True)
        if retained == 0 or retained != expected:
            print("the HDR10+ metadata was not retained frame-exactly", file=sys.stderr)
            status = 1
    for event in database.list_events(job_id=final.id, limit=5000)[-6:]:
        print("  ", event.kind, (event.message or "")[:140])
    if not args.keep and args.work is None:
        shutil.rmtree(work, ignore_errors=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
