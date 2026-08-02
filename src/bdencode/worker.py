"""Durable, strictly serial pipeline worker.

The database owns scheduling and the job state is the recovery cursor.  Every
expensive filesystem stage also has a content-addressed marker, so a service
restart never treats the mere presence of a partial output as success.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import shutil
import signal
import socket
import time
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .config import Settings
from .capabilities import capability_snapshot
from .db import Database
from .encode import (
    ReferenceRemuxPlan,
    audio_track_command,
    encode_pipeline_commands,
    reference_remux_command,
    subtitle_track_command,
)
from .media.bluray import (
    BluRayScanner,
    ContentKind,
    DiscKind,
    DiscScan,
    HdrStaticMetadata,
    MediaStream,
    PlaylistCandidate,
    PlaylistSegment,
    StreamKind,
    ToolCapabilities,
    TrackRole,
    VideoCodec,
    VideoProperties,
)
from .media.language import (
    LanguageDecision,
    LanguageEvidence,
    LanguageResolver,
    LanguageSource,
    LanguageStatus,
)
from .media.language_runtime import AudioLanguageRuntime, LanguageInferenceUnavailable
from .media.planner import (
    Crop as PlannerCrop,
    EncodePlanner,
    EncodeRequest,
    FieldHandling,
    TrackAction,
    TrackSelection,
)
from .media.profiles import (
    ColorMetadata,
    DetailLevel,
    EncoderSettings,
    Hdr10Metadata,
    VbvSettings,
    VideoEncoder,
    recommended_profile,
)
from .logs import sanitize_text
from .models import (
    ArtifactCreate,
    ArtifactKind as DatabaseArtifactKind,
    EventCreate,
    Job,
    JobState,
    Scan,
    ScanCreate,
    ScanState,
    ScanUpdate,
    TERMINAL_STATES,
)
from .mux import (
    FinalTrackPolicy,
    FinalVideoPolicy,
    MuxTrack,
    inspection_commands,
    mkvmerge_command,
    validate_ffprobe_stream_policy,
    validate_hdr10_side_data,
    validate_mkvmerge_identification,
)
from .process import CommandRunner, ProcessFailure
from .qc.artifacts import inspect_png
from .qc.audio import (
    analysis_command,
    audio_probe_command,
    compare_audio_probes,
    parse_audio_probe,
    pcm_hash_command,
    spectrum_command,
)
from .qc.imgbb import ImageUploadError, ImgBBClient
from .qc.video import (
    FrameRecord,
    FrameSelectionError,
    comparison_manifest,
    extract_png_command,
    ffprobe_frame_command,
    parse_ffprobe_frames,
    parse_vspipe_info,
    png_filter_chain,
    select_frame_pairs,
    vspipe_info_command,
)
from .queue import JobQueue
from .utils import atomic_write_json, sha256_file as _uncached_sha256_file
from .vmaf_runner import streamed_vmaf_command
from .vapoursynth import (
    Crop as VapourSynthCrop,
    ReferenceScriptPlan,
    TemporalFilter,
    render_reference_script,
    script_record,
)


LOG = logging.getLogger(__name__)

_FFPROBE_PROFILE_NAMES = {
    "high": "High",
    "high10": "High 10",
    "main": "Main",
    "main10": "Main 10",
    # FFmpeg 5.x reports libx265's 12-bit Main profile under the HEVC
    # Range-Extensions profile family.
    "main12": "Rext",
}
_SAFE_OUTPUT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ ()'&+\-]{0,199}$")
_SELECTION_KEYS = {
    "playlist_id",
    "angle",
    "video",
    "crop",
    "temporal_filter",
    "tracks",
    "output_name",
    "upload_images",
    "dual_type_match",
}
_HASH_CACHE: dict[tuple[str, int, int, int, int, int], str] = {}
_MAINTENANCE_MARKERS = (
    Path("/var/lib/bdencode/install-transactions/active"),
    Path("/var/lib/bdencode/update-runtime/active.json"),
    Path("/var/lib/bdencode/apt-transactions/active"),
)


def _maintenance_active() -> bool:
    """Return true while an installer or toolchain transaction is published."""

    return any(os.path.lexists(marker) for marker in _MAINTENANCE_MARKERS)


def _sd_notify(message: str) -> bool:
    """Send one systemd notification without requiring libsystemd bindings.

    ``NOTIFY_SOCKET`` is absent during ordinary CLI and Windows execution, in
    which case notification is intentionally a no-op.  Removing it before the
    send also prevents media-tool child processes from inheriting the service
    manager's notification endpoint.
    """

    address = os.environ.pop("NOTIFY_SOCKET", None)
    if not address:
        return False
    if address.startswith("@"):
        # systemd exposes Linux abstract namespace sockets with a leading '@',
        # while Python's AF_UNIX API represents the leading NUL explicitly.
        address = "\0" + address[1:]
    elif not address.startswith("/"):
        raise RuntimeError("NOTIFY_SOCKET must be an absolute or abstract path")

    payload = message.encode("utf-8")
    unix_family = getattr(socket, "AF_UNIX", None)
    if unix_family is None:
        raise RuntimeError("systemd notification requires Unix-domain sockets")
    notifier = socket.socket(unix_family, socket.SOCK_DGRAM)
    try:
        notifier.connect(address)
        sent = notifier.send(payload)
        if sent != len(payload):
            raise OSError("short write to systemd notification socket")
    finally:
        notifier.close()
    return True


def sha256_file(path: Path) -> str:
    """Hash immutable stage inputs once per observed filesystem generation.

    A BD reference can be tens of gigabytes.  Re-reading it for every audio-QC
    sub-command would turn marker validation into more I/O than the analysis.
    Size, inode, modification and change times invalidate the process cache.
    """
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    key = (
        str(resolved),
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )
    value = _HASH_CACHE.get(key)
    if value is None:
        value = _uncached_sha256_file(resolved)
        _HASH_CACHE[key] = value
    return value


def _quick_source_fingerprint(path: Path) -> dict[str, Any]:
    """Detect source clip replacement without a second full-disc read.

    The durable reference remux itself receives a full SHA-256.  For marker
    invalidation we combine file identity/timestamps with hashes of the first
    and last MiB, which catches ordinary replacement/corruption while avoiding
    another complete read of every multi-gigabyte M2TS before remuxing.
    """

    resolved = path.resolve(strict=True)
    details = resolved.stat()
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if details.st_size > 1024 * 1024:
            handle.seek(max(0, details.st_size - 1024 * 1024))
            digest.update(handle.read(1024 * 1024))
    return {
        "path": str(resolved),
        "size_bytes": details.st_size,
        "mtime_ns": details.st_mtime_ns,
        "edge_sha256": digest.hexdigest(),
    }


def _playlist_source_snapshot(scan: DiscScan, playlist_id: str) -> list[dict[str, Any]]:
    playlist = scan.playlist(playlist_id)
    clip_ids = tuple(dict.fromkeys(segment.clip_id for segment in playlist.segments))
    result: list[dict[str, Any]] = []
    for clip_id in clip_ids:
        candidate = scan.source / "BDMV" / "STREAM" / f"{clip_id}.m2ts"
        if not candidate.is_file():
            candidate = scan.source / "BDMV" / "STREAM" / f"{clip_id}.M2TS"
        if not candidate.is_file():
            raise ReviewRequired(
                f"selected playlist clip is missing: {clip_id}",
                details={"playlist_id": playlist.playlist_id, "clip_id": clip_id},
            )
        result.append(_quick_source_fingerprint(candidate))
    return result


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        check: bool = True,
        ok_returncodes: Sequence[int] = (0,),
        timeout: float | None = None,
    ) -> Any: ...

    def run_pipeline(
        self,
        commands: Sequence[Sequence[str | os.PathLike[str]]],
        *,
        cwd: Path | None = None,
        stderr_paths: Sequence[Path] | None = None,
        timeout: float | None = None,
        check: bool = True,
    ) -> Any: ...


class ReviewRequired(RuntimeError):
    """The worker cannot safely infer a material operator decision."""

    def __init__(
        self, message: str, *, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class ParsedSelection:
    playlist_id: str
    angle: int
    settings: EncoderSettings
    crop: VapourSynthCrop
    temporal_filter: TemporalFilter
    tracks: tuple[TrackSelection, ...]
    output_name: str
    upload_images: bool
    dual_type_match: bool = False


@dataclass(frozen=True, slots=True)
class JobPaths:
    root: Path
    work: Path
    logs: Path
    analysis: Path
    comparison: Path
    stages: Path
    scan_json: Path
    plan_json: Path
    manifest_json: Path
    language_json: Path
    reference: Path
    script: Path
    encoded_video: Path
    muxed_output: Path

    @classmethod
    def create(cls, settings: Settings, job_id: str) -> "JobPaths":
        root = settings.job_root(job_id)
        value = cls(
            root=root,
            work=root / "work",
            logs=root / "logs",
            analysis=root / "analysis",
            comparison=root / "comparison",
            stages=root / ".stages",
            scan_json=root / "scan.json",
            plan_json=root / "encode-plan.json",
            manifest_json=root / "manifest.json",
            language_json=root / "analysis" / "language-inference.json",
            reference=root / "work" / "reference.mkv",
            script=root / "work" / "reference.vpy",
            encoded_video=root / "work" / "video-encoded.mkv",
            muxed_output=root / "work" / "output.mkv",
        )
        for path in (
            value.root,
            value.work,
            value.logs,
            value.analysis,
            value.comparison,
            value.stages,
            value.work / "tracks",
            value.work / "cache",
        ):
            path.mkdir(mode=0o750, parents=True, exist_ok=True)
        return value


def _current_comparison_pngs(paths: JobPaths, *, prune: bool = False) -> list[Path]:
    video_manifest = paths.comparison / "video-comparison.json"
    audio_manifest = paths.analysis / "audio-comparison.json"
    try:
        video_document = json.loads(video_manifest.read_text(encoding="utf-8"))
        audio_document = json.loads(audio_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("comparison manifests are missing or invalid") from exc

    selected: dict[Path, None] = {}

    def include(root: Path, name: object, expected_sha256: object) -> None:
        if not isinstance(name, str) or Path(name).name != name:
            raise RuntimeError("comparison manifest contains an unsafe image name")
        path = root / name
        if path.suffix.casefold() != ".png" or not path.is_file():
            raise RuntimeError(f"comparison image is missing or invalid: {name}")
        if (
            not isinstance(expected_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or sha256_file(path) != expected_sha256
        ):
            raise RuntimeError(f"comparison image hash differs from manifest: {name}")
        selected[path] = None

    pairs = video_document.get("pairs", [])
    if not isinstance(pairs, list):
        raise RuntimeError("video comparison manifest has no pairs array")
    for pair in pairs:
        if not isinstance(pair, Mapping):
            raise RuntimeError("video comparison pair is invalid")
        for key, hash_key in (
            ("reference_png", "reference_sha256"),
            ("encode_png", "encode_sha256"),
            ("reference_sdr_png", "reference_sdr_sha256"),
            ("encode_sdr_png", "encode_sdr_sha256"),
        ):
            if key in pair:
                include(paths.comparison, pair[key], pair.get(hash_key))

    tracks = audio_document.get("tracks", [])
    if not isinstance(tracks, list):
        raise RuntimeError("audio comparison manifest has no tracks array")
    for track in tracks:
        if not isinstance(track, Mapping):
            raise RuntimeError("audio comparison track is invalid")
        include(
            paths.analysis,
            track.get("source_spectrum"),
            track.get("source_spectrum_sha256"),
        )
        include(
            paths.analysis,
            track.get("encode_spectrum"),
            track.get("encode_spectrum_sha256"),
        )

    if prune:
        for candidate in (
            *paths.comparison.glob("*.png"),
            *paths.analysis.glob("*-spectrum.png"),
        ):
            if candidate not in selected:
                candidate.unlink()
    return sorted(selected, key=lambda item: (str(item.parent), item.name))


def _json_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _atomic_write_text(path: Path, text: str, *, mode: int = 0o640) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _output_records(outputs: Iterable[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for output in outputs:
        resolved = output.resolve(strict=True)
        if not resolved.is_file():
            raise RuntimeError(f"stage output is not a regular file: {output}")
        records.append(
            {
                "path": str(resolved),
                "size_bytes": resolved.stat().st_size,
                "sha256": sha256_file(resolved),
            }
        )
    return records


def _valid_stage(
    marker: Path, inputs: Mapping[str, Any], outputs: Sequence[Path]
) -> bool:
    if not marker.is_file():
        return False
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
        if document.get("schema_version") != 1:
            return False
        if document.get("input_sha256") != _json_hash(inputs):
            return False
        expected = {str(path.resolve(strict=False)): path for path in outputs}
        recorded = {item["path"]: item for item in document.get("outputs", [])}
        if set(expected) != set(recorded):
            return False
        for name, path in expected.items():
            item = recorded[name]
            if not path.is_file() or path.stat().st_size != item["size_bytes"]:
                return False
            if sha256_file(path) != item["sha256"]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _write_stage(
    marker: Path, inputs: Mapping[str, Any], outputs: Sequence[Path]
) -> None:
    atomic_write_json(
        marker,
        {
            "schema_version": 1,
            "input_sha256": _json_hash(inputs),
            "outputs": _output_records(outputs),
            "completed_at_epoch": time.time(),
        },
    )


def _content_kind(job: Job) -> ContentKind:
    return ContentKind(job.content_type.value.lower())


def _language_from_dict(value: Mapping[str, Any] | None) -> LanguageDecision | None:
    if not value:
        return None
    evidence = tuple(
        LanguageEvidence(
            source=LanguageSource(item["source"]),
            raw_code=item.get("raw_code"),
            normalized_code=item.get("normalized_code"),
            confidence=float(item.get("confidence", 1.0)),
            detail=item.get("detail"),
        )
        for item in value.get("evidence", [])
    )
    return LanguageDecision(
        iso639_2t=value.get("iso639_2t"),
        bcp47=value.get("bcp47"),
        status=LanguageStatus(value.get("status", "unknown")),
        confidence=float(value.get("confidence", 0.0)),
        evidence=evidence,
        needs_review=bool(value.get("needs_review", False)),
        overridden_by=value.get("overridden_by"),
    )


def _scan_from_dict(value: Mapping[str, Any]) -> DiscScan:
    playlists: list[PlaylistCandidate] = []
    for raw_playlist in value.get("playlists", []):
        streams: list[MediaStream] = []
        for raw_stream in raw_playlist.get("streams", []):
            raw_video = raw_stream.get("video")
            video = None
            if raw_video:
                raw_static = raw_video.get("hdr10_static") or {}
                video = VideoProperties(
                    codec=VideoCodec(raw_video.get("codec", "unknown")),
                    width=raw_video.get("width"),
                    height=raw_video.get("height"),
                    frame_rate=raw_video.get("frame_rate"),
                    field_order=raw_video.get("field_order"),
                    bit_depth=raw_video.get("bit_depth"),
                    pixel_format=raw_video.get("pixel_format"),
                    color_primaries=raw_video.get("color_primaries"),
                    color_transfer=raw_video.get("color_transfer"),
                    color_matrix=raw_video.get("color_matrix"),
                    color_range=raw_video.get("color_range"),
                    chroma_location=raw_video.get("chroma_location"),
                    hdr10=bool(raw_video.get("hdr10", False)),
                    hdr10_static=HdrStaticMetadata(
                        mastering_display=raw_static.get("mastering_display"),
                        max_cll=raw_static.get("max_cll"),
                        max_fall=raw_static.get("max_fall"),
                    ),
                    dolby_vision=bool(raw_video.get("dolby_vision", False)),
                    dolby_vision_profile=raw_video.get("dolby_vision_profile"),
                    hdr10_base_layer=bool(raw_video.get("hdr10_base_layer", False)),
                    hdr10_plus=bool(raw_video.get("hdr10_plus", False)),
                    three_d=bool(raw_video.get("three_d", False)),
                )
            streams.append(
                MediaStream(
                    id=str(raw_stream["id"]),
                    index=int(raw_stream["index"]),
                    pid=raw_stream.get("pid"),
                    kind=StreamKind(raw_stream["kind"]),
                    codec=str(raw_stream.get("codec", "unknown")),
                    codec_profile=raw_stream.get("codec_profile"),
                    language=_language_from_dict(raw_stream.get("language")),
                    title=raw_stream.get("title"),
                    channels=raw_stream.get("channels"),
                    channel_layout=raw_stream.get("channel_layout"),
                    sample_rate=raw_stream.get("sample_rate"),
                    bit_depth=raw_stream.get("bit_depth"),
                    default=bool(raw_stream.get("default", False)),
                    forced=bool(raw_stream.get("forced", False)),
                    roles=tuple(
                        TrackRole(item) for item in raw_stream.get("roles", [])
                    ),
                    object_audio=bool(raw_stream.get("object_audio", False)),
                    video=video,
                )
            )
        segments = tuple(
            PlaylistSegment(
                clip_id=str(item["clip_id"]),
                in_time_seconds=float(item["in_time_seconds"]),
                out_time_seconds=float(item["out_time_seconds"]),
                relative_start_seconds=float(item.get("relative_start_seconds", 0.0)),
                seamless=bool(item.get("seamless", True)),
                angle=int(item.get("angle", 1)),
            )
            for item in raw_playlist.get("segments", [])
        )
        playlists.append(
            PlaylistCandidate(
                playlist_id=str(raw_playlist["playlist_id"]).zfill(5),
                duration_seconds=float(raw_playlist.get("duration_seconds", 0.0)),
                chapters=tuple(
                    float(item) for item in raw_playlist.get("chapters", [])
                ),
                segments=segments,
                streams=tuple(streams),
                angle_count=int(raw_playlist.get("angle_count", 1)),
                seamless_branching=bool(raw_playlist.get("seamless_branching", False)),
                edition_group=raw_playlist.get("edition_group"),
                edition_label=raw_playlist.get("edition_label"),
                episode_number=raw_playlist.get("episode_number"),
                recommended=bool(raw_playlist.get("recommended", False)),
            )
        )
    raw_capabilities = value.get("capabilities", {})
    return DiscScan(
        source=Path(str(value["source"])),
        disc_kind=DiscKind(value["disc_kind"]),
        content_kind=ContentKind(value["content_kind"]),
        playlists=tuple(playlists),
        capabilities=ToolCapabilities(
            ffprobe=raw_capabilities.get("ffprobe"),
            mediainfo=raw_capabilities.get("mediainfo"),
            mkvmerge=raw_capabilities.get("mkvmerge"),
            libbluray_json=raw_capabilities.get("libbluray_json"),
            ffprobe_bluray=bool(raw_capabilities.get("ffprobe_bluray", False)),
        ),
        fingerprint=str(value["fingerprint"]),
        warnings=tuple(str(item) for item in value.get("warnings", [])),
    )


def _nested_encoder_overrides(raw: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(raw)
    if "color" in result and isinstance(result["color"], Mapping):
        result["color"] = ColorMetadata(**result["color"])
    if "vbv" in result and isinstance(result["vbv"], Mapping):
        result["vbv"] = VbvSettings(**result["vbv"])
    if "hdr10" in result and isinstance(result["hdr10"], Mapping):
        result["hdr10"] = Hdr10Metadata(**result["hdr10"])
    return result


def _derive_hdr10(scan: DiscScan, playlist_id: str) -> Hdr10Metadata:
    playlist = scan.playlist(playlist_id)
    if not playlist.video_streams:
        raise ReviewRequired("the selected playlist has no video stream")
    video = playlist.video_streams[0].video
    assert video is not None
    if not video.hdr10:
        return Hdr10Metadata()
    static = video.hdr10_static
    if not static.complete:
        raise ReviewRequired(
            "HDR10 static mastering metadata is incomplete; enter verified values",
            details={"playlist_id": playlist.playlist_id},
        )
    return Hdr10Metadata(
        enabled=True,
        mastering_display=static.mastering_display,
        max_cll=static.max_cll,
        max_fall=static.max_fall,
    )


def _derive_color(
    scan: DiscScan,
    playlist_id: str,
    confirmation: Mapping[str, Any] | None = None,
) -> ColorMetadata:
    playlist = scan.playlist(playlist_id)
    if not playlist.video_streams:
        raise ReviewRequired("the selected playlist has no video stream")
    video = playlist.video_streams[0].video
    assert video is not None
    scanned = {
        "primaries": video.color_primaries,
        "transfer": video.color_transfer,
        "matrix": video.color_matrix,
        "range": video.color_range,
        "chroma_location": video.chroma_location,
    }
    range_aliases = {
        "tv": "limited",
        "mpeg": "limited",
        "limited": "limited",
        "pc": "full",
        "jpeg": "full",
        "full": "full",
    }
    matrix_aliases = {"bt2020ncl": "bt2020nc", "bt2020cl": "bt2020c"}
    normalized_scanned = {
        "primaries": (
            str(video.color_primaries).casefold() if video.color_primaries else None
        ),
        "transfer": (
            str(video.color_transfer).casefold() if video.color_transfer else None
        ),
        "matrix": (
            matrix_aliases.get(
                str(video.color_matrix).casefold(),
                str(video.color_matrix).casefold(),
            )
            if video.color_matrix
            else None
        ),
        "range": (
            range_aliases.get(str(video.color_range).casefold())
            if video.color_range
            else None
        ),
        "chroma_location": (
            str(video.chroma_location).casefold() if video.chroma_location else None
        ),
    }
    blocking_fields = [
        name for name in ("primaries", "transfer", "matrix") if not scanned[name]
    ]
    if video.color_range and normalized_scanned["range"] is None:
        raise ReviewRequired(
            "source color range is unsupported",
            details={
                "code": "unsupported_source_color_metadata",
                "playlist_id": playlist.playlist_id,
                "field": "range",
                "value": video.color_range,
            },
        )
    if blocking_fields and confirmation is None:
        missing_source_fields = [name for name, value in scanned.items() if not value]
        suggested = None
        suggestion_reason = None
        if (
            scan.disc_kind is DiscKind.BD
            and video.codec in {VideoCodec.AVC, VideoCodec.VC1, VideoCodec.MPEG2}
            and (video.width or 0) >= 1280
            and (video.height or 0) >= 720
            and video.bit_depth == 8
            and not video.hdr10
        ):
            suggested = {
                "primaries": normalized_scanned["primaries"] or "bt709",
                "transfer": normalized_scanned["transfer"] or "bt709",
                "matrix": normalized_scanned["matrix"] or "bt709",
                "range": normalized_scanned["range"] or "limited",
                "chroma_location": normalized_scanned["chroma_location"] or "left",
            }
            suggestion_reason = "HD SDR Blu-ray source profile"
        raise ReviewRequired(
            "A forr\u00e1s sz\u00ednmetaadata hi\u00e1nyos. Ellen\u0151rizd \u00e9s er\u0151s\u00edtsd meg "
            "a sz\u00ednteret a vide\u00f3be\u00e1ll\u00edt\u00e1sokn\u00e1l.",
            details={
                "code": "source_color_confirmation_required",
                "playlist_id": playlist.playlist_id,
                "missing_fields": missing_source_fields,
                "blocking_fields": blocking_fields,
                "detected": normalized_scanned,
                "safe_defaults": {
                    name: value
                    for name, value in {
                        "range": "limited",
                        "chroma_location": "left",
                    }.items()
                    if not scanned[name]
                },
                "confirmation_field": "selection.video.settings.color",
                "suggested": suggested,
                "suggestion_reason": suggestion_reason,
            },
        )

    if confirmation is not None:
        expected_fields = {
            "primaries",
            "transfer",
            "matrix",
            "range",
            "chroma_location",
        }
        if set(confirmation) != expected_fields:
            missing_confirmation = sorted(expected_fields - set(confirmation))
            unknown_confirmation = sorted(set(confirmation) - expected_fields)
            raise ReviewRequired(
                "A sz\u00ednmetaadat meger\u0151s\u00edt\u00e9se nem teljes.",
                details={
                    "code": "invalid_source_color_confirmation",
                    "playlist_id": playlist.playlist_id,
                    "confirmation_field": "selection.video.settings.color",
                    "missing_fields": missing_confirmation,
                    "unknown_fields": unknown_confirmation,
                },
            )
        try:
            confirmed = ColorMetadata(
                primaries=str(confirmation["primaries"]).casefold(),
                transfer=str(confirmation["transfer"]).casefold(),
                matrix=matrix_aliases.get(
                    str(confirmation["matrix"]).casefold(),
                    str(confirmation["matrix"]).casefold(),
                ),
                range=range_aliases.get(
                    str(confirmation["range"]).casefold(),
                    str(confirmation["range"]).casefold(),
                ),
                chroma_location=str(confirmation["chroma_location"]).casefold(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewRequired(
                f"A sz\u00ednmetaadat meger\u0151s\u00edt\u00e9se \u00e9rv\u00e9nytelen: {exc}",
                details={
                    "code": "invalid_source_color_confirmation",
                    "playlist_id": playlist.playlist_id,
                    "confirmation_field": "selection.video.settings.color",
                },
            ) from exc
        conflicts = {
            name: {"scanned": value, "confirmed": getattr(confirmed, name)}
            for name, value in normalized_scanned.items()
            if value is not None and value != getattr(confirmed, name)
        }
        if conflicts:
            raise ReviewRequired(
                "changing color metadata without an explicit color conversion is not allowed",
                details={
                    "code": "source_color_confirmation_conflict",
                    "playlist_id": playlist.playlist_id,
                    "confirmation_field": "selection.video.settings.color",
                    "conflicts": conflicts,
                },
            )
        return confirmed

    # Blu-ray 4:2:0 video is studio/limited range with left chroma siting when
    # those VUI fields are omitted. Primaries/transfer/matrix are never guessed.
    color_range = range_aliases.get(str(video.color_range or "limited").casefold())
    if color_range is None:
        raise ReviewRequired(
            "source color range is unsupported",
            details={"value": video.color_range},
        )
    try:
        return ColorMetadata(
            primaries=str(video.color_primaries).casefold(),
            transfer=str(video.color_transfer).casefold(),
            matrix=matrix_aliases.get(
                str(video.color_matrix).casefold(),
                str(video.color_matrix).casefold(),
            ),
            range=color_range,
            chroma_location=str(video.chroma_location or "left").casefold(),
        )
    except ValueError as exc:
        raise ReviewRequired(f"source color metadata is unsupported: {exc}") from exc


def parse_selection(job: Job, scan: DiscScan) -> ParsedSelection:
    raw = job.selection
    if not isinstance(raw, Mapping):
        raise ReviewRequired("selection JSON is missing")
    unknown = set(raw) - _SELECTION_KEYS
    if unknown:
        raise ReviewRequired(
            "unknown selection field(s): " + ", ".join(sorted(unknown))
        )
    # ``video`` is the canonical home of crop/filter/settings in docs/API.md.
    # Top-level crop/filter and ``overrides`` remain accepted for queued jobs
    # created by the first backend prototype.
    required = {
        "playlist_id",
        "angle",
        "video",
        "tracks",
        "output_name",
        "upload_images",
    }
    missing = required - set(raw)
    if missing:
        raise ReviewRequired(
            "missing selection field(s): " + ", ".join(sorted(missing))
        )

    playlist_text = str(raw["playlist_id"]).lower().removesuffix(".mpls")
    if not playlist_text.isdigit():
        raise ReviewRequired("playlist_id must be numeric")
    playlist_id = playlist_text.zfill(5)
    try:
        playlist = scan.playlist(playlist_id)
    except KeyError as exc:
        raise ReviewRequired(str(exc)) from exc
    angle = raw["angle"]
    if (
        isinstance(angle, bool)
        or not isinstance(angle, int)
        or not 1 <= angle <= playlist.angle_count
    ):
        raise ReviewRequired(f"angle must be between 1 and {playlist.angle_count}")

    video_raw = raw["video"]
    allowed_video = {"detail_level", "settings", "overrides", "crop", "temporal_filter"}
    if not isinstance(video_raw, Mapping) or set(video_raw) - allowed_video:
        raise ReviewRequired(
            "video may contain detail_level, settings, crop and temporal_filter"
        )
    try:
        detail_level = DetailLevel(video_raw.get("detail_level", "beginner"))
    except ValueError as exc:
        raise ReviewRequired("invalid video detail_level") from exc
    if "settings" in video_raw and "overrides" in video_raw:
        if video_raw["settings"] != video_raw["overrides"]:
            raise ReviewRequired("video.settings and legacy video.overrides conflict")
    overrides_raw = video_raw.get("settings", video_raw.get("overrides", {}))
    if not isinstance(overrides_raw, Mapping):
        raise ReviewRequired("video.settings must be an object")
    encoder = VideoEncoder.X265 if scan.disc_kind is DiscKind.UHD else VideoEncoder.X264
    if "encoder" in overrides_raw:
        try:
            selected_encoder = VideoEncoder(overrides_raw["encoder"])
        except (TypeError, ValueError) as exc:
            raise ReviewRequired("invalid video encoder") from exc
        if selected_encoder is not encoder:
            raise ReviewRequired(
                f"{scan.disc_kind.value.upper()} output must use {encoder.value}"
            )
    manual_color = overrides_raw.get("color")
    if manual_color is not None and not isinstance(manual_color, Mapping):
        raise ReviewRequired(
            "video.settings.color must be an object",
            details={
                "code": "invalid_source_color_confirmation",
                "playlist_id": playlist_id,
                "confirmation_field": "selection.video.settings.color",
            },
        )
    source_color = _derive_color(scan, playlist_id, manual_color)
    hdr10 = None
    if encoder is VideoEncoder.X265:
        manual_hdr = overrides_raw.get("hdr10")
        if manual_hdr is not None:
            if not isinstance(manual_hdr, Mapping):
                raise ReviewRequired("video.settings.hdr10 must be an object")
            try:
                hdr10 = Hdr10Metadata(**manual_hdr)
            except (TypeError, ValueError) as exc:
                raise ReviewRequired(f"invalid manual HDR10 metadata: {exc}") from exc
        else:
            hdr10 = _derive_hdr10(scan, playlist_id)
    try:
        encoder_overrides = _nested_encoder_overrides(overrides_raw)
        selected_color = encoder_overrides.get("color")
        if selected_color is not None and selected_color != source_color:
            raise ReviewRequired(
                "changing color metadata without an explicit color conversion is not allowed"
            )
        settings = recommended_profile(
            encoder,
            detail_level=detail_level,
            content_type=job.content_type.value.lower(),
            hdr10=hdr10,
            color=source_color,
            overrides=encoder_overrides,
        )
    except ReviewRequired:
        raise
    except (TypeError, ValueError) as exc:
        raise ReviewRequired(f"invalid video settings: {exc}") from exc
    if settings.bframes < 1:
        raise ReviewRequired(
            "B-frames must remain enabled for mandatory I/P/B comparison"
        )

    if "crop" in raw and "crop" in video_raw and raw["crop"] != video_raw["crop"]:
        raise ReviewRequired("top-level crop and video.crop conflict")
    crop_raw = video_raw.get("crop", raw.get("crop"))
    if not isinstance(crop_raw, Mapping) or set(crop_raw) != {
        "left",
        "top",
        "right",
        "bottom",
    }:
        raise ReviewRequired("video.crop must contain left, top, right and bottom")
    try:
        crop = VapourSynthCrop(**{key: int(value) for key, value in crop_raw.items()})
    except (TypeError, ValueError) as exc:
        raise ReviewRequired(f"invalid crop: {exc}") from exc
    if (
        "temporal_filter" in raw
        and "temporal_filter" in video_raw
        and raw["temporal_filter"] != video_raw["temporal_filter"]
    ):
        raise ReviewRequired(
            "top-level temporal_filter and video.temporal_filter conflict"
        )
    temporal_raw = video_raw.get("temporal_filter", raw.get("temporal_filter"))
    try:
        temporal_filter = TemporalFilter(temporal_raw)
    except (TypeError, ValueError) as exc:
        raise ReviewRequired("invalid temporal_filter") from exc

    tracks_raw = raw["tracks"]
    if not isinstance(tracks_raw, list):
        raise ReviewRequired("tracks must be an array")
    tracks: list[TrackSelection] = []
    allowed_track = {
        "stream_id",
        "action",
        "language",
        "name",
        "default",
        "forced",
        "order",
    }
    for number, item in enumerate(tracks_raw):
        if not isinstance(item, Mapping) or set(item) - allowed_track:
            raise ReviewRequired(f"tracks[{number}] contains unknown fields")
        if "stream_id" not in item or "action" not in item:
            raise ReviewRequired(f"tracks[{number}] needs stream_id and action")
        action_value = str(item["action"]).lower()
        try:
            tracks.append(
                TrackSelection(
                    stream_id=str(item["stream_id"]),
                    action=TrackAction(action_value),
                    language=item.get("language"),
                    name=item.get("name"),
                    default=item.get("default"),
                    forced=item.get("forced"),
                    order=int(item.get("order", number)),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ReviewRequired(f"invalid tracks[{number}]: {exc}") from exc

    output_name = str(raw["output_name"]).removesuffix(".mkv")
    if not _SAFE_OUTPUT_RE.fullmatch(output_name) or output_name in {".", ".."}:
        raise ReviewRequired("output_name contains unsafe characters")
    if not isinstance(raw["upload_images"], bool):
        raise ReviewRequired("upload_images must be boolean")
    dual_type = raw.get("dual_type_match", True)
    if not isinstance(dual_type, bool):
        raise ReviewRequired("dual_type_match must be boolean")
    return ParsedSelection(
        playlist_id=playlist_id,
        angle=angle,
        settings=settings,
        crop=crop,
        temporal_filter=temporal_filter,
        tracks=tuple(tracks),
        output_name=output_name,
        upload_images=raw["upload_images"],
        dual_type_match=dual_type,
    )


def _field_handling(value: TemporalFilter) -> FieldHandling:
    if value is TemporalFilter.PROGRESSIVE:
        return FieldHandling.PROGRESSIVE
    if value in {TemporalFilter.IVTC_TFF, TemporalFilter.IVTC_BFF}:
        return FieldHandling.IVTC
    if value in {TemporalFilter.BWDIF_TFF, TemporalFilter.BWDIF_BFF}:
        return FieldHandling.DEINTERLACE
    return FieldHandling.HYBRID


def _planner_crop(value: VapourSynthCrop) -> PlannerCrop:
    return PlannerCrop(value.left, value.top, value.right, value.bottom)


class PipelineWorker:
    """One durable worker.  Running a second instance remains database-safe."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        scanner_factory: Callable[[Settings], BluRayScanner] | None = None,
        runner_factory: Callable[[JobPaths], Runner] | None = None,
        upload_client_factory: Callable[[], ImgBBClient] | None = None,
        language_runtime: AudioLanguageRuntime | None = None,
    ) -> None:
        self.database = database
        self.settings = settings.validate()
        self.queue = JobQueue(database)
        self.scanner_factory = scanner_factory
        self.runner_factory = runner_factory or (
            lambda paths: CommandRunner(paths.logs / "commands.jsonl")
        )
        self.upload_client_factory = upload_client_factory or ImgBBClient
        self.language_runtime = language_runtime or AudioLanguageRuntime(
            settings.data_root
        )
        self._runners: dict[str, Runner] = {}

    def _runner(self, paths: JobPaths) -> Runner:
        return self._runners.setdefault(str(paths.root), self.runner_factory(paths))

    def process_one_stage(self, job: Job) -> Job:
        """Run exactly the current durable stage and return refreshed state."""
        paths = JobPaths.create(self.settings, job.id)
        if job.state is JobState.SCANNING:
            self._scan(job, paths)
        elif job.state is JobState.READY:
            self._prepare(job, paths)
        elif job.state is JobState.ENCODING:
            self._encode(job, paths)
        elif job.state is JobState.MUXING:
            self._mux(job, paths)
        elif job.state is JobState.QC:
            self._qc(job, paths)
        elif job.state is JobState.COMPARISON:
            self._comparison(job, paths)
        elif job.state is JobState.UPLOADING:
            self._upload_and_finalize(job, paths)
        return self.database.get_job(job.id)

    def process_job(self, job: Job) -> Job:
        """Continue one job until completion or an operator-controlled pause."""
        while job.state not in TERMINAL_STATES:
            if job.state in {
                JobState.AWAITING_SELECTION,
                JobState.NEEDS_REVIEW,
                JobState.UPLOAD_FAILED,
            }:
                return job
            try:
                before = job.state
                job = self.process_one_stage(job)
                if job.state is before:
                    raise RuntimeError(
                        f"worker stage {before.value} made no durable progress"
                    )
            except ReviewRequired as exc:
                current = self.database.get_job(job.id)
                if (
                    current.state not in TERMINAL_STATES
                    and current.state is not JobState.NEEDS_REVIEW
                ):
                    job = self.queue.needs_review(
                        job.id, message=str(exc), details=exc.details
                    )
                else:
                    job = current
                return job
            except (ImageUploadError, OSError) as exc:
                current = self.database.get_job(job.id)
                if current.state is JobState.UPLOADING:
                    return self.queue.advance(
                        job.id,
                        JobState.UPLOAD_FAILED,
                        message="image upload failed; retry is safe",
                        details={"error_type": type(exc).__name__},
                    )
                self._fail(current, exc)
                return self.database.get_job(job.id)
            except Exception as exc:
                self._fail(self.database.get_job(job.id), exc)
                return self.database.get_job(job.id)
        return job

    def _fail(self, job: Job, exc: Exception) -> None:
        LOG.exception("job %s failed in %s", job.id, job.state.value)
        if job.state in TERMINAL_STATES:
            return
        if job.state is JobState.SCANNING:
            scans = self.database.list_scans(job_id=job.id, limit=1)
            if scans and scans[0].status in {ScanState.PENDING, ScanState.RUNNING}:
                self.database.update_scan(
                    scans[0].id,
                    ScanUpdate(
                        status=ScanState.FAILED,
                        error=f"{type(exc).__name__}: {exc}",
                        message="disc scan failed",
                    ),
                )
                # update_scan atomically transitions the owning SCANNING job
                # to FAILED in the same transaction.
                return
        self.queue.fail(
            job.id,
            message=f"{type(exc).__name__}: {exc}",
            details={"stage": job.state.value, "error_type": type(exc).__name__},
        )

    def _scan(self, job: Job, paths: JobPaths) -> None:
        scans = self.database.list_scans(job_id=job.id, limit=1)
        scan_row: Scan
        if scans and scans[0].status in {ScanState.PENDING, ScanState.RUNNING}:
            scan_row = scans[0]
        else:
            scan_row = self.database.create_scan(ScanCreate(job_id=job.id))
        inputs = {
            "source": job.source_path,
            "content_kind": _content_kind(job).value,
            "source_roots": [str(item) for item in self.settings.source_roots],
        }
        marker = paths.stages / "scan.json"
        if not _valid_stage(marker, inputs, [paths.scan_json]):
            source = self.settings.authorize_source(job.source_path)
            if self.scanner_factory is not None:
                scanner = self.scanner_factory(self.settings)
            else:
                source_root = next(
                    root
                    for root in self.settings.source_roots
                    if source == root or source.is_relative_to(root)
                )
                scanner = BluRayScanner(source_root=source_root)
            result = scanner.scan(source, content_kind=_content_kind(job))
            atomic_write_json(paths.scan_json, result.to_dict())
            _write_stage(marker, inputs, [paths.scan_json])
        result_json = json.loads(paths.scan_json.read_text(encoding="utf-8"))
        self._register_artifact(
            job.id,
            paths.scan_json,
            DatabaseArtifactKind.MANIFEST,
            "disc-scan.json",
            scan_id=scan_row.id,
            mime_type="application/json",
        )
        self.database.update_scan(
            scan_row.id,
            ScanUpdate(
                status=ScanState.AWAITING_SELECTION,
                result=result_json,
                message="scan complete; playlist, processing and tracks require confirmation",
            ),
        )

    def _load_scan_and_selection(
        self, job: Job, paths: JobPaths
    ) -> tuple[DiscScan, ParsedSelection]:
        if not paths.scan_json.is_file():
            raise RuntimeError("durable scan.json is missing")
        scan = _scan_from_dict(json.loads(paths.scan_json.read_text(encoding="utf-8")))
        selection = parse_selection(job, scan)
        if paths.language_json.is_file():
            try:
                report = json.loads(paths.language_json.read_text(encoding="utf-8"))
                if (
                    report.get("selection_sha256") == _json_hash(job.selection)
                    and report.get("scan_fingerprint") == scan.fingerprint
                    and report.get("status") == "completed"
                ):
                    resolved = report.get("resolved_languages", {})
                    selection = replace(
                        selection,
                        tracks=tuple(
                            replace(item, language=resolved[item.stream_id])
                            if item.language is None and item.stream_id in resolved
                            else item
                            for item in selection.tracks
                        ),
                    )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        return scan, selection

    @staticmethod
    def _declared_language_evidence(
        decision: LanguageDecision | None,
    ) -> dict[str, str | None]:
        result: dict[str, str | None] = {"mpls": None, "clpi": None, "pmt": None}
        if decision is None:
            return result
        for item in decision.evidence:
            if item.source.value in result:
                result[item.source.value] = item.raw_code
        return result

    def _resolve_selected_languages(
        self,
        job: Job,
        scan: DiscScan,
        selection: ParsedSelection,
        paths: JobPaths,
    ) -> ParsedSelection:
        playlist = scan.playlist(selection.playlist_id)
        streams = {item.id: item for item in playlist.streams}
        selected_ids = {item.stream_id for item in selection.tracks}
        resolved: dict[str, str] = {}
        evidence_records: list[dict[str, Any]] = []
        # A READY-stage restart may load previously inferred languages into the
        # in-memory selection. Preserve that durable evidence instead of
        # replacing the completed report with an empty map when inference is
        # consequently skipped on the replay.
        if paths.language_json.is_file():
            try:
                prior_report = json.loads(
                    paths.language_json.read_text(encoding="utf-8")
                )
                if (
                    prior_report.get("selection_sha256") == _json_hash(job.selection)
                    and prior_report.get("scan_fingerprint") == scan.fingerprint
                    and prior_report.get("status") == "completed"
                ):
                    prior_resolved = prior_report.get("resolved_languages", {})
                    if isinstance(prior_resolved, dict):
                        resolved.update(
                            {
                                str(stream_id): str(language)
                                for stream_id, language in prior_resolved.items()
                                if stream_id in selected_ids and language
                            }
                        )
                    prior_evidence = prior_report.get("evidence", [])
                    if isinstance(prior_evidence, list):
                        evidence_records.extend(
                            item for item in prior_evidence if isinstance(item, dict)
                        )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        unresolved: list[dict[str, Any]] = []
        reference_digest = sha256_file(paths.reference)
        resolver = LanguageResolver()

        for track in selection.tracks:
            if track.action is TrackAction.OMIT or track.language is not None:
                continue
            stream = streams.get(track.stream_id)
            if stream is None:
                continue
            declared = stream.language
            if (
                declared is not None
                and declared.iso639_2t is not None
                and not declared.needs_review
            ):
                continue
            if stream.kind is StreamKind.SUBTITLE:
                unresolved.append(
                    {
                        "stream_id": stream.id,
                        "kind": stream.kind.value,
                        "reason": "subtitle_ocr_or_manual_override_required",
                    }
                )
                continue
            if stream.kind is not StreamKind.AUDIO:
                continue
            try:
                inference = self.language_runtime.infer(
                    paths.reference,
                    stream.index,
                    playlist.duration_seconds,
                    paths.work,
                    self._runner(paths),
                    source_sha256=reference_digest,
                )
            except LanguageInferenceUnavailable as exc:
                unresolved.append(
                    {
                        "stream_id": stream.id,
                        "kind": stream.kind.value,
                        "reason": exc.reason_code,
                    }
                )
                continue
            consensus = inference.get("consensus", {})
            raw = self._declared_language_evidence(declared)
            decision = resolver.resolve(
                mpls=raw["mpls"],
                clpi=raw["clpi"],
                pmt=raw["pmt"],
                audio_lid=consensus.get("iso639_2t"),
                audio_confidence=float(consensus.get("confidence", 0.0)),
            )
            evidence_records.append(
                {
                    "stream_id": stream.id,
                    "decision": decision.to_dict(),
                    "inference": inference,
                }
            )
            if decision.iso639_2t and not decision.needs_review:
                resolved[stream.id] = decision.iso639_2t
            else:
                unresolved.append(
                    {
                        "stream_id": stream.id,
                        "kind": stream.kind.value,
                        "reason": "language_conflict_or_low_confidence",
                        "decision": decision.to_dict(),
                    }
                )

        report = {
            "schema_version": 1,
            "selection_sha256": _json_hash(job.selection),
            "scan_fingerprint": scan.fingerprint,
            "playlist_id": selection.playlist_id,
            "status": "needs_review" if unresolved else "completed",
            "resolved_languages": resolved,
            "evidence": evidence_records,
            "unresolved": unresolved,
        }
        atomic_write_json(paths.language_json, report)
        self._register_artifact(
            job.id,
            paths.language_json,
            DatabaseArtifactKind.REPORT,
            paths.language_json.name,
            mime_type="application/json",
        )
        if unresolved:
            raise ReviewRequired(
                "one or more retained tracks need a confirmed language before encoding",
                details={"tracks": unresolved},
            )
        if not resolved:
            return selection
        return replace(
            selection,
            tracks=tuple(
                replace(item, language=resolved[item.stream_id])
                if item.language is None and item.stream_id in resolved
                else item
                for item in selection.tracks
            ),
        )

    def _prepare(self, job: Job, paths: JobPaths) -> None:
        scan, selection = self._load_scan_and_selection(job, paths)
        remux_inputs = {
            "scan_fingerprint": scan.fingerprint,
            "playlist_id": selection.playlist_id,
            "angle": selection.angle,
            "source_clips": _playlist_source_snapshot(scan, selection.playlist_id),
        }
        remux_marker = paths.stages / "reference-remux.json"
        if not _valid_stage(remux_marker, remux_inputs, [paths.reference]):
            command = reference_remux_command(
                ReferenceRemuxPlan(
                    disc_root=scan.source,
                    playlist_id=selection.playlist_id,
                    output_path=paths.reference,
                    angle=selection.angle,
                )
            )
            self._runner(paths).run(
                command,
                cwd=paths.work,
                stderr_path=paths.logs / "reference-remux.log",
            )
            _write_stage(remux_marker, remux_inputs, [paths.reference])

        # Resolve uncertain retained audio tracks now: this avoids discovering
        # a language problem only after a multi-hour video encode. Manual track
        # language choices remain authoritative and skip content inference.
        selection = self._resolve_selected_languages(job, scan, selection, paths)
        plan_request = EncodeRequest(
            scan=scan,
            playlist_id=selection.playlist_id,
            settings=selection.settings,
            work_dir=paths.work,
            output_path=paths.muxed_output,
            track_selections=selection.tracks,
            field_handling=_field_handling(selection.temporal_filter),
            crop=_planner_crop(selection.crop),
            angle=selection.angle,
            overwrite=True,
        )
        try:
            encode_plan = EncodePlanner(work_root=self.settings.data_root).build(
                plan_request
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewRequired(f"selection cannot be planned safely: {exc}") from exc
        # This plan is produced only after an explicit operator selection.  Its
        # advisory warnings remain durable evidence, but do not create an
        # unresumable review loop (for example an intentional Atmos -> FLAC
        # choice). Missing or contradictory data has already raised above.
        if encode_plan.warnings:
            self.database.add_event(
                EventCreate(
                    job_id=job.id,
                    kind="worker.plan-warning",
                    message="encode plan contains advisory warnings",
                    payload={"warnings": list(encode_plan.warnings)},
                )
            )
        atomic_write_json(paths.plan_json, encode_plan.to_dict())

        script_plan = ReferenceScriptPlan(
            source=paths.reference,
            cache_path=paths.work / "cache" / "bestsource",
            script_path=paths.script,
            temporal_filter=selection.temporal_filter,
            crop=selection.crop,
        )
        content = render_reference_script(script_plan)
        script_inputs = {
            "reference_sha256": sha256_file(paths.reference),
            "plan": script_record(script_plan, content),
        }
        script_marker = paths.stages / "reference-script.json"
        script_record_path = paths.work / "reference-script.json"
        if not _valid_stage(
            script_marker, script_inputs, [paths.script, script_record_path]
        ):
            _atomic_write_text(paths.script, content)
            atomic_write_json(script_record_path, script_record(script_plan, content))
            _write_stage(
                script_marker, script_inputs, [paths.script, script_record_path]
            )
        self._register_artifact(
            job.id,
            paths.plan_json,
            DatabaseArtifactKind.MANIFEST,
            "encode-plan.json",
            mime_type="application/json",
        )
        self.queue.advance(
            job.id, JobState.ENCODING, message="reference timeline prepared"
        )

    def _encode(self, job: Job, paths: JobPaths) -> None:
        scan, selection = self._load_scan_and_selection(job, paths)
        if not paths.script.is_file() or not paths.reference.is_file():
            raise RuntimeError("prepared reference or VapourSynth script is missing")
        inputs = {
            "scan_fingerprint": scan.fingerprint,
            "script_sha256": sha256_file(paths.script),
            "settings": selection.settings.to_dict(),
        }
        marker = paths.stages / "video-encode.json"
        if not _valid_stage(marker, inputs, [paths.encoded_video]):
            commands = encode_pipeline_commands(
                paths.script,
                paths.encoded_video,
                selection.settings,
                metadata={
                    "bdencode_job": job.id,
                    "bdencode_scan": scan.fingerprint,
                },
            )
            self._runner(paths).run_pipeline(
                commands,
                cwd=paths.work,
                stderr_paths=[
                    paths.logs / "vapoursynth.log",
                    paths.logs / "video-encode.log",
                ],
            )
            _write_stage(marker, inputs, [paths.encoded_video])
        self.queue.advance(job.id, JobState.MUXING, message="video encode complete")

    @staticmethod
    def _selected_streams(
        scan: DiscScan, selection: ParsedSelection
    ) -> list[tuple[int, TrackSelection, MediaStream]]:
        playlist = scan.playlist(selection.playlist_id)
        streams = {item.id: item for item in playlist.streams}
        result: list[tuple[int, TrackSelection, MediaStream]] = []
        ordered = sorted(
            selection.tracks, key=lambda item: (item.order, item.stream_id)
        )
        for number, item in enumerate(ordered, start=1):
            if item.stream_id not in streams:
                raise ReviewRequired(
                    f"selected stream disappeared from scan: {item.stream_id}"
                )
            result.append((number, item, streams[item.stream_id]))
        return result

    @staticmethod
    def _track_path(
        paths: JobPaths, number: int, item: TrackSelection, stream: MediaStream
    ) -> Path:
        if stream.kind is StreamKind.SUBTITLE:
            suffix = ".mks"
        elif item.action is TrackAction.FLAC:
            # Matroska retains the source PTS/delay while carrying FLAC audio;
            # a raw .flac intermediate cannot represent that timeline.
            suffix = ".mka"
        else:
            suffix = ".mka"
        return paths.work / "tracks" / f"track-{number:02d}-{stream.kind.value}{suffix}"

    def _mux(self, job: Job, paths: JobPaths) -> None:
        scan, selection = self._load_scan_and_selection(job, paths)
        if not paths.encoded_video.is_file() or not paths.reference.is_file():
            raise RuntimeError("encoded video or reference remux is missing")
        audio: list[MuxTrack] = []
        subtitles: list[MuxTrack] = []
        for number, item, stream in self._selected_streams(scan, selection):
            if item.action is TrackAction.OMIT:
                continue
            if item.bcp47(stream) == "und":
                raise ReviewRequired(
                    f"retained track {stream.id} has no confirmed language; provide an override"
                )
            output = self._track_path(paths, number, item, stream)
            inputs = {
                "reference_sha256": sha256_file(paths.reference),
                "stream_id": stream.id,
                "stream_index": stream.index,
                "action": item.action.value,
            }
            marker = paths.stages / f"track-{number:02d}.json"
            if not _valid_stage(marker, inputs, [output]):
                if stream.kind is StreamKind.AUDIO:
                    command = audio_track_command(
                        paths.reference,
                        stream.index,
                        output,
                        action=item.action.value,
                    )
                elif stream.kind is StreamKind.SUBTITLE:
                    command = subtitle_track_command(
                        paths.reference, stream.index, output
                    )
                else:
                    raise ReviewRequired(
                        f"unsupported selected track kind: {stream.kind.value}"
                    )
                self._runner(paths).run(
                    command,
                    cwd=paths.work,
                    stderr_path=paths.logs / f"track-{number:02d}.log",
                )
                _write_stage(marker, inputs, [output])
            track = MuxTrack(
                path=output,
                language=item.bcp47(stream),
                name=item.name or stream.title,
                default=stream.default if item.default is None else item.default,
                forced=stream.forced if item.forced is None else item.forced,
            )
            (audio if stream.kind is StreamKind.AUDIO else subtitles).append(track)

        chapters = paths.work / "chapters.xml"
        chapter_inputs = {"reference_sha256": sha256_file(paths.reference)}
        chapter_marker = paths.stages / "chapters.json"
        if not _valid_stage(chapter_marker, chapter_inputs, [chapters]):
            chapter_result = self._runner(paths).run(
                ["mkvextract", str(paths.reference), "chapters", str(chapters)],
                cwd=paths.work,
                stderr_path=paths.logs / "chapters.log",
                ok_returncodes=(0, 1),
            )
            _write_stage(chapter_marker, chapter_inputs, [chapters])
            if getattr(chapter_result, "returncode", 0) == 1:
                raise ReviewRequired(
                    "mkvextract completed with warnings; inspect chapters.log before resuming"
                )

        sanitized_log = paths.logs / "encode.log"
        self._write_sanitized_log(job, selection, paths, sanitized_log)
        tags = paths.work / "tags.xml"
        settings_json = json.dumps(selection.settings.to_dict(), sort_keys=True)
        tags_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<Tags><Tag><Targets/>'
            f"<Simple><Name>BDENCODE_JOB</Name><String>{html.escape(job.id)}</String></Simple>"
            f"<Simple><Name>BDENCODE_SETTINGS</Name><String>{html.escape(settings_json)}</String></Simple>"
            "</Tag></Tags>\n"
        )
        _atomic_write_text(tags, tags_xml)
        mux_inputs = {
            "video_sha256": sha256_file(paths.encoded_video),
            "tracks": [
                {"path": str(item.path), "sha256": sha256_file(item.path)}
                for item in (*audio, *subtitles)
            ],
            "chapters_sha256": sha256_file(chapters),
            "log_sha256": sha256_file(sanitized_log),
            "tags_sha256": sha256_file(tags),
        }
        command = mkvmerge_command(
            paths.muxed_output,
            paths.encoded_video,
            audio_tracks=audio,
            subtitle_tracks=subtitles,
            chapters_path=chapters,
            tags_path=tags,
            sanitized_log_path=sanitized_log,
            title=selection.output_name,
        )
        # Track names/languages/default/forced flags and title are material mux
        # inputs even when the elementary stream hashes are unchanged.
        mux_inputs["argv"] = command
        marker = paths.stages / "mux.json"
        if not _valid_stage(marker, mux_inputs, [paths.muxed_output]):
            mux_result = self._runner(paths).run(
                command,
                cwd=paths.work,
                stderr_path=paths.logs / "mkvmerge.log",
                ok_returncodes=(0, 1),
            )
            _write_stage(marker, mux_inputs, [paths.muxed_output])
            if getattr(mux_result, "returncode", 0) == 1:
                raise ReviewRequired(
                    "mkvmerge completed with warnings; inspect mkvmerge.log before resuming"
                )
        self._register_artifact(
            job.id,
            sanitized_log,
            DatabaseArtifactKind.LOG,
            "encode.log",
            mime_type="text/plain",
        )
        self.queue.advance(job.id, JobState.QC, message="final Matroska mux complete")

    def _write_sanitized_log(
        self, job: Job, selection: ParsedSelection, paths: JobPaths, output: Path
    ) -> None:
        sections = [
            "BDEncode sanitized encode log",
            f"job_id={job.id}",
            "settings=" + json.dumps(selection.settings.to_dict(), sort_keys=True),
        ]
        # Keep the attachment deterministic. commands.jsonl grows when later
        # stages run, which would otherwise invalidate a completed mux marker.
        for name in ("vapoursynth.log", "video-encode.log"):
            path = paths.logs / name
            if path.is_file():
                sections.extend(
                    (
                        f"\n--- {name} ---",
                        path.read_text(encoding="utf-8", errors="replace"),
                    )
                )
        text = sanitize_text("\n".join(sections))
        _atomic_write_text(output, text.rstrip() + "\n")

    def _qc(self, job: Job, paths: JobPaths) -> None:
        scan, selection = self._load_scan_and_selection(job, paths)
        output = paths.muxed_output
        if not output.is_file():
            raise RuntimeError("mux output is missing")
        report_root = paths.analysis / "container"
        report_root.mkdir(mode=0o750, parents=True, exist_ok=True)
        reports: list[Path] = []
        for command, report in inspection_commands(output, report_root):
            effective_command = list(command)
            inputs = {"output_sha256": sha256_file(output), "argv": effective_command}
            marker = paths.stages / f"qc-{report.stem}.json"
            if not _valid_stage(marker, inputs, [report]):
                inspection_result = None
                if report.name == "full-decode.log":
                    inspection_result = self._runner(paths).run(
                        effective_command, cwd=paths.work, stderr_path=report
                    )
                else:
                    inspection_result = self._runner(paths).run(
                        effective_command,
                        cwd=paths.work,
                        stdout_path=report,
                        stderr_path=report.with_suffix(report.suffix + ".stderr"),
                        ok_returncodes=(0, 1) if command[0] == "mkvmerge" else (0,),
                    )
                _write_stage(marker, inputs, [report])
                if (
                    report.name == "mkvmerge-identify.json"
                    and getattr(inspection_result, "returncode", 0) == 1
                ):
                    raise ReviewRequired(
                        "mkvmerge identify completed with warnings; inspect its stderr before resuming"
                    )
            if (
                report.name == "full-decode.log"
                and report.read_text(encoding="utf-8", errors="replace").strip()
            ):
                raise ReviewRequired(
                    "full decode emitted an error-level diagnostic; final media is not accepted"
                )
            reports.append(report)

        playlist = scan.playlist(selection.playlist_id)
        retained_streams = [
            entry
            for entry in self._selected_streams(scan, selection)
            if entry[1].action is not TrackAction.OMIT
        ]
        expected_audio: list[MuxTrack] = []
        expected_subtitles: list[MuxTrack] = []
        for _number, item, stream in retained_streams:
            expected = MuxTrack(
                path=Path("unused"),
                language=item.bcp47(stream),
                name=item.name or stream.title,
                default=stream.default if item.default is None else item.default,
                forced=stream.forced if item.forced is None else item.forced,
            )
            (
                expected_audio
                if stream.kind is StreamKind.AUDIO
                else expected_subtitles
            ).append(expected)
        identify_document = json.loads(
            (report_root / "mkvmerge-identify.json").read_text(encoding="utf-8")
        )
        topology_errors = validate_mkvmerge_identification(
            identify_document,
            audio_tracks=expected_audio,
            subtitle_tracks=expected_subtitles,
            title=selection.output_name,
        )
        if topology_errors:
            raise ReviewRequired(
                "final Matroska topology differs from the reviewed mux plan",
                details={"errors": list(topology_errors)},
            )

        source_video = playlist.video_streams[0].video
        assert source_video is not None
        if source_video.width is None or source_video.height is None:
            raise ReviewRequired("source dimensions are missing from the reviewed scan")
        video_policy = FinalVideoPolicy(
            codec_name=(
                "h264" if selection.settings.encoder is VideoEncoder.X264 else "hevc"
            ),
            profile=_FFPROBE_PROFILE_NAMES[selection.settings.profile],
            width=source_video.width - selection.crop.left - selection.crop.right,
            height=source_video.height - selection.crop.top - selection.crop.bottom,
            pixel_format=selection.settings.pixel_format,
            color_range="pc" if selection.settings.color.range == "full" else "tv",
            color_space=selection.settings.color.matrix,
            color_transfer=selection.settings.color.transfer,
            color_primaries=selection.settings.color.primaries,
            chroma_location=selection.settings.color.chroma_location,
        )
        audio_policies = [
            FinalTrackPolicy(
                "audio", "flac" if item.action is TrackAction.FLAC else stream.codec
            )
            for _number, item, stream in retained_streams
            if stream.kind is StreamKind.AUDIO
        ]
        subtitle_policies = [
            FinalTrackPolicy("subtitle", stream.codec)
            for _number, _item, stream in retained_streams
            if stream.kind is StreamKind.SUBTITLE
        ]
        ffprobe_document = json.loads(
            (report_root / "ffprobe-streams.json").read_text(encoding="utf-8")
        )
        stream_errors = validate_ffprobe_stream_policy(
            ffprobe_document,
            video=video_policy,
            media_tracks=[*audio_policies, *subtitle_policies],
        )
        hdr = selection.settings.hdr10
        side_data_document = json.loads(
            (report_root / "ffprobe-video-side-data.json").read_text(encoding="utf-8")
        )
        hdr_errors = validate_hdr10_side_data(
            ffprobe_document,
            side_data_document,
            enabled=hdr.enabled,
            mastering_display=hdr.mastering_display,
            max_cll=hdr.max_cll,
            max_fall=hdr.max_fall,
        )
        policy_errors = (*stream_errors, *hdr_errors)
        if policy_errors:
            raise ReviewRequired(
                "final media streams differ from the reviewed codec/color/HDR policy",
                details={"errors": list(policy_errors)},
            )

        audio_manifest_path = paths.analysis / "audio-comparison.json"
        audio_outputs: list[Path] = [audio_manifest_path]
        audio_results: list[dict[str, Any]] = []
        spectrograms: list[Path] = []
        audio_ordinals = {
            item.id: index for index, item in enumerate(playlist.audio_streams)
        }
        audio_inputs: dict[str, Any] = {
            "reference_sha256": sha256_file(paths.reference),
            "output_sha256": sha256_file(output),
            "tracks": [],
        }
        retained = [
            entry for entry in retained_streams if entry[2].kind is StreamKind.AUDIO
        ]
        for final_audio_ordinal, (number, item, stream) in enumerate(retained):
            intermediate = self._track_path(paths, number, item, stream)
            source_audio_ordinal = audio_ordinals[stream.id]
            prefix = paths.analysis / f"audio-{number:02d}"
            source_probe = prefix.with_name(prefix.name + "-source-probe.json")
            encode_probe = prefix.with_name(prefix.name + "-encode-probe.json")
            source_pcm = prefix.with_name(prefix.name + "-source-pcm.sha256")
            encode_pcm = prefix.with_name(prefix.name + "-encode-pcm.sha256")
            source_analysis = prefix.with_name(prefix.name + "-source-analysis.log")
            encode_analysis = prefix.with_name(prefix.name + "-encode-analysis.log")
            source_spectrum = prefix.with_name(prefix.name + "-source-spectrum.png")
            encode_spectrum = prefix.with_name(prefix.name + "-encode-spectrum.png")
            spectrograms.extend((source_spectrum, encode_spectrum))
            commands: list[tuple[list[str], Path, bool]] = [
                (
                    audio_probe_command(paths.reference, source_audio_ordinal),
                    source_probe,
                    True,
                ),
                (audio_probe_command(output, final_audio_ordinal), encode_probe, True),
                (
                    pcm_hash_command(paths.reference, source_audio_ordinal),
                    source_pcm,
                    True,
                ),
                (pcm_hash_command(output, final_audio_ordinal), encode_pcm, True),
                (
                    analysis_command(
                        paths.reference, source_audio_ordinal, source_analysis
                    ),
                    source_analysis,
                    False,
                ),
                (
                    analysis_command(output, final_audio_ordinal, encode_analysis),
                    encode_analysis,
                    False,
                ),
                (
                    spectrum_command(
                        paths.reference, source_audio_ordinal, source_spectrum
                    ),
                    source_spectrum,
                    False,
                ),
                (
                    spectrum_command(output, final_audio_ordinal, encode_spectrum),
                    encode_spectrum,
                    False,
                ),
            ]
            for command, result_path, stdout_result in commands:
                command_inputs = {
                    "argv": command,
                    "reference": sha256_file(paths.reference),
                    "final_output": sha256_file(output),
                    "intermediate": sha256_file(intermediate),
                }
                marker = paths.stages / f"qc-{result_path.stem}.json"
                if not _valid_stage(marker, command_inputs, [result_path]):
                    if stdout_result:
                        self._runner(paths).run(
                            command,
                            cwd=paths.work,
                            stdout_path=result_path,
                            stderr_path=paths.logs / f"{result_path.stem}.stderr",
                        )
                    else:
                        self._runner(paths).run(
                            command,
                            cwd=paths.work,
                            stderr_path=result_path,
                        )
                    _write_stage(marker, command_inputs, [result_path])
            inspect_png(source_spectrum)
            inspect_png(encode_spectrum)
            source_value = parse_audio_probe(source_probe.read_text(encoding="utf-8"))
            encode_value = parse_audio_probe(encode_probe.read_text(encoding="utf-8"))
            comparison = compare_audio_probes(source_value, encode_value)
            pcm_match = sha256_file(source_pcm) == sha256_file(encode_pcm)
            delay_tolerance = Decimal(1) / Decimal(source_value.sample_rate)
            delay_match = abs(comparison.delay_seconds) <= delay_tolerance
            structural_match = (
                comparison.sample_rate_match
                and comparison.channels_match
                and comparison.channel_layout_match
            )
            if not structural_match or not pcm_match or not delay_match:
                raise ReviewRequired(
                    f"final audio verification failed for {stream.id}",
                    details={
                        "action": item.action.value,
                        "comparison": comparison.to_dict(),
                        "pcm_match": pcm_match,
                        "delay_tolerance_seconds": str(delay_tolerance),
                    },
                )
            source_probe_value = asdict(source_value)
            encode_probe_value = asdict(encode_value)
            for value in (source_probe_value, encode_probe_value):
                value["start_time"] = str(value["start_time"])
                if value["duration"] is not None:
                    value["duration"] = str(value["duration"])
            audio_results.append(
                {
                    "stream_id": stream.id,
                    "action": item.action.value,
                    "source_probe": source_probe_value,
                    "encode_probe": encode_probe_value,
                    "comparison": comparison.to_dict(),
                    "decoded_pcm_sha256_match": pcm_match,
                    "delay_within_one_sample": delay_match,
                    "delay_tolerance_seconds": str(delay_tolerance),
                    "source_pcm_sha256": sha256_file(source_pcm),
                    "encode_pcm_sha256": sha256_file(encode_pcm),
                    "source_spectrum": source_spectrum.name,
                    "encode_spectrum": encode_spectrum.name,
                    "source_spectrum_sha256": sha256_file(source_spectrum),
                    "encode_spectrum_sha256": sha256_file(encode_spectrum),
                }
            )
            audio_outputs.extend(
                (
                    source_probe,
                    encode_probe,
                    source_pcm,
                    encode_pcm,
                    source_analysis,
                    encode_analysis,
                    source_spectrum,
                    encode_spectrum,
                )
            )
            audio_inputs["tracks"].append(
                {
                    "stream": stream.id,
                    "action": item.action.value,
                    "sha256": sha256_file(intermediate),
                }
            )
        audio_marker = paths.stages / "qc-audio.json"
        if not _valid_stage(audio_marker, audio_inputs, audio_outputs):
            atomic_write_json(
                audio_manifest_path, {"schema_version": 1, "tracks": audio_results}
            )
            _write_stage(audio_marker, audio_inputs, audio_outputs)

        for report in reports:
            kind = (
                DatabaseArtifactKind.MEDIAINFO
                if report.name == "mediainfo.txt"
                else DatabaseArtifactKind.MKVINFO
                if report.name in {"mkvinfo.txt", "mkvmerge-identify.json"}
                else DatabaseArtifactKind.REPORT
            )
            self._register_artifact(
                job.id, report, kind, report.name, mime_type="text/plain"
            )
        self._register_artifact(
            job.id,
            audio_manifest_path,
            DatabaseArtifactKind.AUDIO_COMPARISON,
            audio_manifest_path.name,
            mime_type="application/json",
        )
        for spectrum in spectrograms:
            self._register_artifact(
                job.id,
                spectrum,
                DatabaseArtifactKind.SPECTROGRAM,
                spectrum.name,
                mime_type="image/png",
            )
        self.queue.advance(
            job.id, JobState.COMPARISON, message="container and audio QC passed"
        )

    @staticmethod
    def _reference_png_pipeline(
        script: Path,
        frame: int,
        output: Path,
        *,
        hdr_native: bool,
        source_hdr10: bool,
        color: ColorMetadata,
    ) -> list[list[str]]:
        filters = png_filter_chain(
            hdr_native=hdr_native,
            source_hdr10=source_hdr10,
            color_primaries=color.primaries,
            color_transfer=color.transfer,
            color_matrix=color.matrix,
            color_range=color.range,
        )
        return [
            [
                "vspipe",
                "--container",
                "y4m",
                "--start",
                str(frame),
                "--end",
                str(frame),
                str(script),
                "-",
            ],
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-v",
                "warning",
                "-f",
                "yuv4mpegpipe",
                "-i",
                "pipe:0",
                "-vf",
                ",".join(filters),
                "-frames:v",
                "1",
                "-compression_level",
                "6",
                "-y",
                str(output),
            ],
        ]

    @staticmethod
    def _metric_pipeline(script: Path, encoded: Path, result: Path) -> list[list[str]]:
        ssim_stats = (
            str(result.with_suffix(".ssim.log")).replace("\\", "/").replace(":", "\\:")
        )
        psnr_stats = (
            str(result.with_suffix(".psnr.log")).replace("\\", "/").replace(":", "\\:")
        )
        filter_value = (
            "[0:v]settb=AVTB,setpts=PTS-STARTPTS,split=2[r1][r2];"
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,split=2[e1][e2];"
            f"[e1][r1]ssim=stats_file={ssim_stats}[ssimout];"
            "[ssimout]nullsink;"
            f"[e2][r2]psnr=stats_file={psnr_stats}[metrics]"
        )
        return [
            ["vspipe", "--container", "y4m", str(script), "-"],
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-v",
                "info",
                "-f",
                "yuv4mpegpipe",
                "-i",
                "pipe:0",
                "-i",
                str(encoded),
                "-filter_complex",
                filter_value,
                "-map",
                "[metrics]",
                "-f",
                "null",
                "-",
            ],
        ]

    def _comparison(self, job: Job, paths: JobPaths) -> None:
        scan, selection = self._load_scan_and_selection(job, paths)
        encoded_input = paths.muxed_output
        if not encoded_input.is_file():
            raise RuntimeError("final Matroska output is missing before comparison")
        encoded_probe = paths.comparison / "encoded-frames.json"
        probe_inputs = {"final_mkv_sha256": sha256_file(encoded_input)}
        probe_marker = paths.stages / "comparison-frame-probe.json"
        if not _valid_stage(probe_marker, probe_inputs, [encoded_probe]):
            self._runner(paths).run(
                ffprobe_frame_command(encoded_input),
                cwd=paths.work,
                stdout_path=encoded_probe,
                stderr_path=paths.logs / "comparison-frame-probe.stderr",
            )
            _write_stage(probe_marker, probe_inputs, [encoded_probe])
        encoded = parse_ffprobe_frames(encoded_probe.read_text(encoding="utf-8"))
        reference_info_path = paths.comparison / "reference-vapoursynth-info.txt"
        reference_info_inputs = {"script_sha256": sha256_file(paths.script)}
        reference_info_marker = paths.stages / "comparison-reference-info.json"
        if not _valid_stage(
            reference_info_marker, reference_info_inputs, [reference_info_path]
        ):
            self._runner(paths).run(
                vspipe_info_command(paths.script),
                cwd=paths.work,
                stdout_path=reference_info_path,
                stderr_path=paths.logs / "comparison-reference-info.stderr",
            )
            _write_stage(
                reference_info_marker, reference_info_inputs, [reference_info_path]
            )
        reference_info = parse_vspipe_info(
            reference_info_path.read_text(encoding="utf-8", errors="replace")
        )
        if len(encoded) != reference_info.frames:
            raise ReviewRequired(
                "encoded frame count differs from the VapourSynth reference",
                details={
                    "reference_frames": reference_info.frames,
                    "encoded_frames": len(encoded),
                },
            )
        shifted = [
            item.presentation_index
            for item in encoded
            if abs(
                item.pts_seconds - reference_info.pts_for_frame(item.presentation_index)
            )
            > Decimal("0.001")
        ]
        if shifted:
            raise ReviewRequired(
                "encoded presentation timestamps diverge from the reference timeline",
                details={
                    "first_mismatched_frame": shifted[0],
                    "mismatch_count": len(shifted),
                },
            )
        # The reference is the exact VapourSynth output frame used to create each
        # encoded presentation frame. A decoded reference has no independent GOP
        # category; optional source bitstream types are recorded only when the
        # source timeline is unchanged.
        source_types: dict[int, str | None] = {}
        if selection.temporal_filter is TemporalFilter.PROGRESSIVE:
            source_probe = paths.comparison / "source-frames.json"
            source_inputs = {"reference_sha256": sha256_file(paths.reference)}
            source_marker = paths.stages / "comparison-source-frame-probe.json"
            if not _valid_stage(source_marker, source_inputs, [source_probe]):
                self._runner(paths).run(
                    ffprobe_frame_command(paths.reference),
                    cwd=paths.work,
                    stdout_path=source_probe,
                    stderr_path=paths.logs / "comparison-source-frame-probe.stderr",
                )
                _write_stage(source_marker, source_inputs, [source_probe])
            source_types = {
                item.presentation_index: item.pict_type
                for item in parse_ffprobe_frames(
                    source_probe.read_text(encoding="utf-8")
                )
            }
        reference = [
            FrameRecord(
                presentation_index=item.presentation_index,
                pts_seconds=reference_info.pts_for_frame(item.presentation_index),
                pict_type=source_types.get(item.presentation_index),
            )
            for item in encoded
            if item.presentation_index < reference_info.frames
        ]
        require_source_type_match = (
            selection.dual_type_match
            and selection.temporal_filter is TemporalFilter.PROGRESSIVE
        )
        try:
            pairs = select_frame_pairs(
                encoded,
                reference,
                per_type=self.settings.comparison_frames_per_type,
                dual_type_match=require_source_type_match,
            )
        except FrameSelectionError as exc:
            raise ReviewRequired(str(exc)) from exc

        playlist = scan.playlist(selection.playlist_id)
        video = playlist.video_streams[0].video
        assert video is not None
        hdr = selection.settings.hdr10.enabled
        comparison_color = selection.settings.color
        pngs: list[Path] = []
        manifest = comparison_manifest(pairs)
        manifest["reference_alignment"] = "same_vapoursynth_output_frame_index"
        manifest["distorted_input"] = {
            "role": "final_matroska_video_track_0",
            "path": encoded_input.name,
            "sha256": sha256_file(encoded_input),
        }
        manifest["source_bitstream_type_available"] = (
            selection.temporal_filter is TemporalFilter.PROGRESSIVE
        )
        manifest["source_bitstream_type_match_required"] = require_source_type_match
        manifest["reference_clip"] = {
            "frames": reference_info.frames,
            "fps_numerator": reference_info.fps_numerator,
            "fps_denominator": reference_info.fps_denominator,
        }
        for number, pair in enumerate(pairs, start=1):
            label = f"{number:02d}-{pair.category}-f{pair.presentation_index:09d}"
            reference_png = paths.comparison / f"{label}-reference.png"
            encoded_png = paths.comparison / f"{label}-encode.png"
            image_inputs = {
                "script_sha256": sha256_file(paths.script),
                "final_mkv_sha256": sha256_file(encoded_input),
                "frame": pair.presentation_index,
                "hdr_native": True,
            }
            image_marker = paths.stages / f"comparison-{label}-native.json"
            if not _valid_stage(
                image_marker, image_inputs, [reference_png, encoded_png]
            ):
                self._runner(paths).run_pipeline(
                    self._reference_png_pipeline(
                        paths.script,
                        pair.presentation_index,
                        reference_png,
                        hdr_native=True,
                        source_hdr10=hdr,
                        color=comparison_color,
                    ),
                    cwd=paths.work,
                    stderr_paths=[
                        paths.logs / f"{label}-reference-vs.log",
                        paths.logs / f"{label}-reference-png.log",
                    ],
                )
                self._runner(paths).run(
                    extract_png_command(
                        encoded_input,
                        pair.presentation_index,
                        encoded_png,
                        hdr_native=True,
                        source_hdr10=hdr,
                        color_primaries=comparison_color.primaries,
                        color_transfer=comparison_color.transfer,
                        color_matrix=comparison_color.matrix,
                        color_range=comparison_color.range,
                    ),
                    cwd=paths.work,
                    stderr_path=paths.logs / f"{label}-encode-png.log",
                )
                _write_stage(image_marker, image_inputs, [reference_png, encoded_png])
            inspect_png(reference_png, require_high_bit_depth=hdr)
            inspect_png(encoded_png, require_high_bit_depth=hdr)
            pngs.extend((reference_png, encoded_png))
            pair_value = manifest["pairs"][number - 1]
            pair_value["reference_png"] = reference_png.name
            pair_value["encode_png"] = encoded_png.name
            pair_value["reference_sha256"] = sha256_file(reference_png)
            pair_value["encode_sha256"] = sha256_file(encoded_png)
            if hdr:
                reference_sdr = paths.comparison / f"{label}-reference-sdr.png"
                encoded_sdr = paths.comparison / f"{label}-encode-sdr.png"
                sdr_inputs = dict(image_inputs, hdr_native=False)
                sdr_marker = paths.stages / f"comparison-{label}-sdr.json"
                if not _valid_stage(
                    sdr_marker, sdr_inputs, [reference_sdr, encoded_sdr]
                ):
                    self._runner(paths).run_pipeline(
                        self._reference_png_pipeline(
                            paths.script,
                            pair.presentation_index,
                            reference_sdr,
                            hdr_native=False,
                            source_hdr10=True,
                            color=comparison_color,
                        ),
                        cwd=paths.work,
                        stderr_paths=[
                            paths.logs / f"{label}-reference-sdr-vs.log",
                            paths.logs / f"{label}-reference-sdr-png.log",
                        ],
                    )
                    self._runner(paths).run(
                        extract_png_command(
                            encoded_input,
                            pair.presentation_index,
                            encoded_sdr,
                            hdr_native=False,
                            source_hdr10=True,
                            color_primaries=comparison_color.primaries,
                            color_transfer=comparison_color.transfer,
                            color_matrix=comparison_color.matrix,
                            color_range=comparison_color.range,
                        ),
                        cwd=paths.work,
                        stderr_path=paths.logs / f"{label}-encode-sdr-png.log",
                    )
                    _write_stage(sdr_marker, sdr_inputs, [reference_sdr, encoded_sdr])
                inspect_png(reference_sdr)
                inspect_png(encoded_sdr)
                pngs.extend((reference_sdr, encoded_sdr))
                pair_value["reference_sdr_png"] = reference_sdr.name
                pair_value["encode_sdr_png"] = encoded_sdr.name
                pair_value["reference_sdr_sha256"] = sha256_file(reference_sdr)
                pair_value["encode_sdr_sha256"] = sha256_file(encoded_sdr)

        metrics = paths.comparison / "video-metrics.json"
        metrics_inputs = {
            "script_sha256": sha256_file(paths.script),
            "final_mkv_sha256": sha256_file(encoded_input),
        }
        metrics_marker = paths.stages / "comparison-metrics.json"
        metric_sidecars: list[Path] = []
        if metrics.is_file():
            try:
                metric_document = json.loads(metrics.read_text(encoding="utf-8"))
                if metric_document.get("backend") == "ffmpeg-ssim-psnr":
                    metric_sidecars = [
                        paths.comparison / metric_document["ssim_stats"]["path"],
                        paths.comparison / metric_document["psnr_stats"]["path"],
                    ]
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                metric_sidecars = []
        if not _valid_stage(
            metrics_marker, metrics_inputs, [metrics, *metric_sidecars]
        ):
            metric_sidecars = []
            vmaf_available = bool(
                shutil.which("bdencode-vmaf") and shutil.which("vmaf")
            )
            if vmaf_available:
                try:
                    self._runner(paths).run(
                        streamed_vmaf_command(
                            paths.script,
                            encoded_input,
                            metrics,
                            hdr10=hdr,
                            model_4k=bool((video.width or 0) >= 3000),
                        ),
                        cwd=paths.work,
                        stderr_path=paths.logs / "metrics-vmaf.log",
                    )
                except (OSError, ProcessFailure) as exc:
                    raise ReviewRequired(
                        "official VMAF analysis failed; decode/alignment errors cannot be hidden by fallback",
                        details={"error_type": type(exc).__name__},
                    ) from exc
            else:
                fallback = paths.comparison / "video-metrics.ssim.log"
                psnr_fallback = paths.comparison / "video-metrics.psnr.log"
                self._runner(paths).run_pipeline(
                    self._metric_pipeline(paths.script, encoded_input, metrics),
                    cwd=paths.work,
                    stderr_paths=[
                        paths.logs / "metrics-vapoursynth-fallback.log",
                        paths.logs / "metrics-ssim-psnr.log",
                    ],
                )
                if not fallback.is_file() or not psnr_fallback.is_file():
                    raise RuntimeError(
                        "SSIM/PSNR fallback produced incomplete metric output"
                    )
                atomic_write_json(
                    metrics,
                    {
                        "schema_version": 1,
                        "backend": "ffmpeg-ssim-psnr",
                        "ssim_stats": {
                            "path": fallback.name,
                            "sha256": sha256_file(fallback),
                        },
                        "psnr_stats": {
                            "path": psnr_fallback.name,
                            "sha256": sha256_file(psnr_fallback),
                        },
                    },
                )
                metric_sidecars = [fallback, psnr_fallback]
            _write_stage(metrics_marker, metrics_inputs, [metrics, *metric_sidecars])
        manifest["metrics"] = {"path": metrics.name, "sha256": sha256_file(metrics)}
        manifest_path = paths.comparison / "video-comparison.json"
        atomic_write_json(manifest_path, manifest)
        _current_comparison_pngs(paths, prune=True)
        comparison_outputs = [manifest_path, metrics, *metric_sidecars, *pngs]
        _write_stage(
            paths.stages / "comparison.json",
            {
                "pairs": [item.to_dict() for item in pairs],
                "per_type": self.settings.comparison_frames_per_type,
                "hdr": hdr,
            },
            comparison_outputs,
        )
        self._register_artifact(
            job.id,
            manifest_path,
            DatabaseArtifactKind.VIDEO_COMPARISON,
            manifest_path.name,
            mime_type="application/json",
        )
        for png in pngs:
            self._register_artifact(
                job.id,
                png,
                DatabaseArtifactKind.VIDEO_COMPARISON,
                png.name,
                mime_type="image/png",
            )
        self.queue.advance(
            job.id, JobState.UPLOADING, message="I/P/B comparison complete"
        )

    def _upload_and_finalize(self, job: Job, paths: JobPaths) -> None:
        _scan, selection = self._load_scan_and_selection(job, paths)
        pngs = _current_comparison_pngs(paths, prune=True)
        allowed_names = {path.name for path in pngs}
        if len(allowed_names) != len(pngs):
            raise RuntimeError("comparison image basenames are not unique")
        checkpoint = paths.comparison / "uploads.json"
        uploaded: dict[str, Any] = {}
        if checkpoint.is_file():
            uploaded = json.loads(checkpoint.read_text(encoding="utf-8")).get(
                "images", {}
            )
            if not isinstance(uploaded, dict):
                uploaded = {}
            uploaded = {
                name: value
                for name, value in uploaded.items()
                if name in allowed_names and isinstance(value, dict)
            }
        if selection.upload_images:
            try:
                client = self.upload_client_factory()
                for png in pngs:
                    digest = sha256_file(png)
                    prior = uploaded.get(png.name)
                    if (
                        prior
                        and prior.get("local_sha256") == digest
                        and prior.get("image_url")
                    ):
                        continue
                    result = client.upload_png(png)
                    uploaded[png.name] = asdict(result)
                    atomic_write_json(
                        checkpoint, {"schema_version": 1, "images": uploaded}
                    )
            except ImageUploadError:
                raise
            except Exception as exc:
                # Credential loading happens before ImgBBClient's HTTP error
                # wrapper; normalize it so the durable state is UPLOAD_FAILED
                # and the already-uploaded checkpoint remains retryable.
                raise ImageUploadError("ImgBB credential or upload failed") from exc
        else:
            atomic_write_json(
                checkpoint,
                {
                    "schema_version": 1,
                    "skipped": True,
                    "reason": "upload_images=false",
                    "images": {},
                },
            )

        bbcode = paths.comparison / "comparison.bbcode"
        lines = [f"[b]{selection.output_name} — comparison[/b]"]
        for name in sorted(uploaded):
            item = uploaded[name]
            lines.extend((f"[b]{name}[/b]", str(item["bbcode"])))
        if not uploaded:
            lines.append(
                "[i]Image upload was disabled; see the lossless PNG sidecars.[/i]"
            )
        _atomic_write_text(bbcode, "\n".join(lines) + "\n")
        _write_stage(
            paths.stages / "upload.json",
            {
                "upload_images": selection.upload_images,
                "images": {path.name: sha256_file(path) for path in pngs},
            },
            [checkpoint, bbcode],
        )

        completed = self.settings.completed_root / selection.output_name
        owner = completed / ".bdencode-owner.json"
        if completed.exists() and not owner.is_file() and any(completed.iterdir()):
            raise ReviewRequired(
                f"completed directory is non-empty and has no BDEncode owner record: {completed}"
            )
        completed.mkdir(mode=0o750, parents=True, exist_ok=True)
        if owner.is_file():
            ownership = json.loads(owner.read_text(encoding="utf-8"))
            if ownership.get("job_id") != job.id:
                raise ReviewRequired(
                    f"completed directory belongs to another job: {completed}"
                )
        else:
            atomic_write_json(owner, {"schema_version": 1, "job_id": job.id})
        final_output = completed / f"{selection.output_name}.mkv"
        finalize_inputs = {
            "mux_sha256": sha256_file(paths.muxed_output),
            "comparison_sha256": sha256_file(
                paths.comparison / "video-comparison.json"
            ),
            "audio_sha256": sha256_file(paths.analysis / "audio-comparison.json"),
            "upload_sha256": sha256_file(checkpoint),
        }
        finalize_marker = paths.stages / "finalize.json"
        if not _valid_stage(finalize_marker, finalize_inputs, [final_output]):
            if (
                final_output.exists()
                and sha256_file(final_output) != finalize_inputs["mux_sha256"]
            ):
                raise ReviewRequired(
                    f"completed output already exists with different content: {final_output}"
                )
            temporary_output = completed / f".{selection.output_name}.mkv.partial"
            shutil.copy2(paths.muxed_output, temporary_output)
            if sha256_file(temporary_output) != finalize_inputs["mux_sha256"]:
                temporary_output.unlink(missing_ok=True)
                raise RuntimeError("completed output copy failed hash verification")
            os.replace(temporary_output, final_output)
            for directory in (paths.logs, paths.analysis, paths.comparison):
                destination = completed / directory.name
                shutil.copytree(directory, destination, dirs_exist_ok=True)
            _write_stage(finalize_marker, finalize_inputs, [final_output])
        self._register_artifact(
            job.id,
            bbcode,
            DatabaseArtifactKind.BBCODE,
            bbcode.name,
            mime_type="text/plain",
        )
        self._register_artifact(
            job.id,
            final_output,
            DatabaseArtifactKind.OUTPUT,
            final_output.name,
            mime_type="video/x-matroska",
        )
        self._write_manifest(job, selection, paths, final_output)
        shutil.copy2(paths.manifest_json, completed / "manifest.json")
        self.queue.advance(
            job.id, JobState.COMPLETED, message="encode, QC and comparison completed"
        )

    def _write_manifest(
        self,
        job: Job,
        selection: ParsedSelection,
        paths: JobPaths,
        output: Path,
    ) -> None:
        stage_records: dict[str, Any] = {}
        for marker in sorted(paths.stages.glob("*.json")):
            stage_records[marker.stem] = json.loads(marker.read_text(encoding="utf-8"))
        artifacts = [
            item.model_dump(mode="json")
            for item in self.database.list_artifacts(job_id=job.id, limit=1000)
        ]
        command_audit = paths.logs / "commands.jsonl"
        command_records: list[dict[str, Any]] = []
        if command_audit.is_file():
            for line in command_audit.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    command_records.append(record)
        toolchain = capability_snapshot(
            (
                "ffmpeg",
                "ffprobe",
                "x264",
                "x265",
                "vspipe",
                "mkvmerge",
                "mkvinfo",
                "mediainfo",
                "vmaf",
                "bdencode-libbluray-scan",
            )
        )
        atomic_write_json(
            paths.manifest_json,
            {
                "schema_version": 1,
                "job_id": job.id,
                "source_path": job.source_path,
                "selection": job.selection,
                "encoder_settings": selection.settings.to_dict(),
                "output": {
                    "path": str(output),
                    "sha256": sha256_file(output),
                    "size_bytes": output.stat().st_size,
                },
                "stages": stage_records,
                "artifacts": artifacts,
                "runtime_provenance": toolchain,
                "command_audit": {
                    "path": str(command_audit),
                    "sha256": sha256_file(command_audit)
                    if command_audit.is_file()
                    else None,
                    "records": command_records,
                },
                "comparison_attached_to_mkv": False,
            },
        )

    def _register_artifact(
        self,
        job_id: str,
        path: Path,
        kind: DatabaseArtifactKind,
        name: str,
        *,
        scan_id: str | None = None,
        mime_type: str | None = None,
    ) -> None:
        resolved = path.resolve(strict=True)
        digest = sha256_file(resolved)
        for current in self.database.list_artifacts(job_id=job_id, limit=1000):
            if current.path == str(resolved) and current.sha256 == digest:
                return
        self.database.create_artifact(
            ArtifactCreate(
                job_id=job_id,
                scan_id=scan_id,
                kind=kind,
                name=name,
                path=str(resolved),
                mime_type=mime_type,
                sha256=digest,
                size_bytes=resolved.stat().st_size,
                metadata={"sidecar": kind is not DatabaseArtifactKind.OUTPUT},
            )
        )


def run_worker(
    database: Database,
    settings: Settings,
    once: bool = False,
    poll_interval: float | None = None,
) -> int:
    """Run the serial worker; ``once`` processes at most one job end-to-end."""
    settings = settings.validate()
    settings.create_directories()
    database.initialize()
    worker = PipelineWorker(database, settings)
    interval = settings.worker_poll_seconds if poll_interval is None else poll_interval
    if interval < 0:
        raise ValueError("poll_interval cannot be negative")
    stopping = False
    instance_lock = None
    fcntl_module = None

    if os.name == "posix":
        import fcntl as fcntl_module  # type: ignore[no-redef]

        instance_path = settings.state_root / "worker.lock"
        instance_lock = instance_path.open("a+b")
        os.chmod(instance_path, 0o640)
        try:
            fcntl_module.flock(
                instance_lock.fileno(), fcntl_module.LOCK_EX | fcntl_module.LOCK_NB
            )
        except BlockingIOError:
            instance_lock.close()
            LOG.error("another BDEncode worker already owns %s", instance_path)
            return 75

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    previous: dict[int, Any] = {}
    for number in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[number] = signal.signal(number, stop)
        except (ValueError, OSError):
            pass
    try:
        _sd_notify("READY=1\nSTATUS=Ready; waiting for an encode job")
        while not stopping:
            if _maintenance_active():
                time.sleep(min(interval or 0.2, 0.2))
                continue
            if fcntl_module is not None:
                # The updater/installer takes this lock exclusively before it
                # checks queue-idle. Holding a shared lock across the atomic DB
                # claim closes the otherwise tiny maintenance-vs-claim race.
                deployment_path = settings.state_root / "deployment.lock"
                with deployment_path.open("a+b") as deployment_lock:
                    os.chmod(deployment_path, 0o640)
                    while not stopping:
                        try:
                            fcntl_module.flock(
                                deployment_lock.fileno(),
                                fcntl_module.LOCK_SH | fcntl_module.LOCK_NB,
                            )
                            break
                        except BlockingIOError:
                            time.sleep(min(interval or 0.2, 0.2))
                    if stopping:
                        return 0
                    try:
                        if _maintenance_active():
                            job = None
                        else:
                            job = database.active_job()
                            if job is None:
                                job = worker.queue.claim_next()
                    finally:
                        fcntl_module.flock(
                            deployment_lock.fileno(), fcntl_module.LOCK_UN
                        )
            else:
                job = database.active_job()
                if job is None:
                    job = worker.queue.claim_next()
            if job is None:
                if once:
                    return 0
                time.sleep(interval)
                continue
            worker.process_job(job)
            if once:
                return 0
            # A review/upload pause intentionally blocks the queue.  Polling the
            # database lets an API action resume it without restarting service.
            time.sleep(interval)
        return 0
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if instance_lock is not None:
            if fcntl_module is not None:
                fcntl_module.flock(instance_lock.fileno(), fcntl_module.LOCK_UN)
            instance_lock.close()


__all__ = [
    "ParsedSelection",
    "PipelineWorker",
    "ReviewRequired",
    "parse_selection",
    "run_worker",
]
