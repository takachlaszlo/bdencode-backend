"""Durable worker with concurrent scan and strictly serial encode lanes.

The database owns scheduling and the job state is the recovery cursor.  Every
expensive filesystem stage also has a content-addressed marker, so a service
restart never treats the mere presence of a partial output as success.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol, Sequence

from .audio import (
    AUDIO_DECODE_POLICY_SCHEMA_VERSION,
    audio_decode_input_args,
    effective_audio_policy,
    expected_audio_codec,
    normalize_audio_codec_name,
)
from .chapters import render_matroska_chapters
from .config import Settings
from .capabilities import capability_snapshot
from .db import Database, StateConflictError
from .encode import (
    PcmBlurayAudio,
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
    resolve_audio_defaults,
    resolved_track_name,
)
from .media.profiles import (
    ColorMetadata,
    DetailLevel,
    EncoderSettings,
    Hdr10Metadata,
    VbvSettings,
    VideoEncoder,
    format_frame_rate,
    parse_frame_rate,
    recommended_profile,
    source_adapted_settings,
)
from .maintenance import (
    MaintenanceDomainGuard,
    MaintenanceJournal,
    MaintenancePhase,
    MaintenanceTargetSpec,
    safe_tree_usage,
)
from .logs import assert_public_metadata_absent, sanitize_text
from .models import (
    ArtifactCreate,
    ArtifactKind as DatabaseArtifactKind,
    EventCreate,
    Job,
    JobControlState,
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
    parse_stream_start_times,
    parse_stream_start_times_by_type,
    plan_common_zero_timeline,
    stream_start_probe_command,
    validate_ffprobe_stream_policy,
    validate_hdr10_side_data,
    validate_mkvmerge_identification,
    validate_stream_start_times,
)
from .process import (
    CommandRunner,
    DiagnosticCategory,
    MediaDiagnostic,
    ProcessFailure,
    ProcessInterrupted,
    classify_media_diagnostics,
    redact_argv,
)
from .progress import EncodeProgressReporter
from .qc.artifacts import inspect_png
from .qc.audio import (
    AUDIO_FRAME_CONTINUITY_SCHEMA_VERSION,
    analysis_command,
    audio_frame_continuity_probe_command,
    audio_probe_command,
    compare_audio_frame_continuity,
    compare_audio_probes,
    parse_audio_frame_continuity,
    parse_audio_probe,
    parse_audio_analysis,
    pcm_hash_command,
    plan_spectrum_windows,
    spectrum_command,
    spectrum_stitch_command,
    verify_audio_output,
    verify_audio_signal,
)
from .qc.catbox import CatboxClient
from .qc.crop import (
    CropPolicyError,
    full_title_cropdetect_command,
    parse_stable_cropdetect,
    validate_operator_crop,
)
from .qc.freeimage import FreeimageClient
from .qc.image_upload import (
    IMAGE_UPLOAD_PROVIDERS,
    ImageUploadClient,
    ImageUploadError,
    parse_uploaded_image_checkpoint,
)
from .qc.imgbb import ImgBBClient
from .qc.video import (
    CropMargins as ActiveCropMargins,
    FrameRecord,
    FrameSelectionError,
    annotate_comparison_png_command,
    comparison_manifest,
    extract_png_at_timestamp_command,
    extract_y4m_at_timestamp_command,
    ffprobe_frame_origin_command,
    ffprobe_sampled_frame_command,
    parse_ffprobe_frame_origin,
    parse_ffmpeg_metric_stats,
    parse_sampled_ffprobe_frames,
    parse_vspipe_info,
    plan_sample_intervals,
    png_filter_chain,
    native_yuv_metric_command,
    select_frame_pairs,
    vspipe_info_command,
)
from .qc.subtitle import (
    SubtitleDecodeError,
    parse_subtitle_probe,
    require_subtitle_decode,
    subtitle_decode_probe_command,
    subtitle_probe_command,
    validate_subtitle_classification,
)
from .qc.integrity import (
    VideoEfficiencyError,
    compare_packet_timelines,
    packet_timeline_probe_command,
    parse_packet_timeline,
    parse_video_packet_sizes,
    parse_stream_payload_hash,
    parse_video_stream_hash,
    require_video_cadence,
    require_video_completeness,
    require_video_efficiency,
    source_video_integrity_command,
    stream_payload_hash_command,
    video_packet_size_command,
    video_stream_hash_command,
)
from .queue import JobQueue
from .utils import (
    atomic_write_json,
    atomic_write_text,
    sha256_file as _uncached_sha256_file,
)
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


def _ffprobe_level_idc(encoder: VideoEncoder, level: str | None) -> int | None:
    """Translate the configured codec level to FFprobe's integer notation."""

    if level is None:
        return None
    multiplier = 10 if encoder is VideoEncoder.X264 else 30
    return int(Decimal(level) * multiplier)


_SAFE_OUTPUT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ ()'&+\-]{0,199}$")
_X264_RELEASE_RE = re.compile(
    r"^.+\.1080p\.BluRay\.x264(?:-[A-Za-z0-9][A-Za-z0-9._-]{0,31})?$",
    re.IGNORECASE,
)
_X265_RELEASE_RE = re.compile(
    r"^.+\.2160p\.UHD\.BluRay\.x265(?:-[A-Za-z0-9][A-Za-z0-9._-]{0,31})?$",
    re.IGNORECASE,
)
_SELECTION_KEYS = {
    "schema_version",
    "playlist_id",
    "angle",
    "video",
    "crop",
    "temporal_filter",
    "tracks",
    "output_name",
    "upload_images",
    "image_upload_provider",
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
        stderr_line_callback: Callable[[str], None] | None = None,
        interrupt_requested: Callable[[], bool] | None = None,
        poll_interval: float = 0.2,
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
    image_upload_provider: str
    dual_type_match: bool = False
    schema_version: int = 2


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
    timeline_json: Path
    reference: Path
    script: Path
    encoded_video: Path
    muxed_output: Path

    @classmethod
    def create(cls, settings: Settings, job_id: str) -> "JobPaths":
        jobs_root = settings.jobs_root
        if os.path.lexists(jobs_root) and _unsafe_directory_link(jobs_root):
            raise ReviewRequired("jobs root cannot be a symbolic link or junction")
        jobs_root.mkdir(mode=0o750, parents=True, exist_ok=True)
        resolved_jobs_root = jobs_root.resolve(strict=True)
        resolved_data_root = settings.data_root.resolve(strict=True)
        if resolved_jobs_root.parent != resolved_data_root:
            raise ReviewRequired("jobs root escaped the configured data root")

        root = settings.job_root(job_id)
        if os.path.lexists(root) and _unsafe_directory_link(root):
            raise ReviewRequired("job root cannot be a symbolic link or junction")
        root.mkdir(mode=0o750, parents=False, exist_ok=True)
        resolved_root = root.resolve(strict=True)
        if resolved_root.parent != resolved_jobs_root or resolved_root.name != job_id:
            raise ReviewRequired("job root escaped the configured jobs root")
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
            timeline_json=root / "analysis" / "timeline-plan.json",
            reference=root / "work" / "reference.mkv",
            script=root / "work" / "reference.vpy",
            encoded_video=root / "work" / "video-encoded.mkv",
            muxed_output=root / "work" / "output.mkv",
        )
        for path in (
            value.work,
            value.logs,
            value.analysis,
            value.comparison,
            value.stages,
        ):
            if os.path.lexists(path) and _unsafe_directory_link(path):
                raise ReviewRequired(
                    f"job workspace member cannot be a link: {path.name}"
                )
            path.mkdir(mode=0o750, parents=True, exist_ok=True)
            if path.resolve(strict=True).parent != resolved_root:
                raise ReviewRequired(
                    f"job workspace member escaped its job root: {path.name}"
                )
        resolved_work = value.work.resolve(strict=True)
        for path in (value.work / "tracks", value.work / "cache"):
            if os.path.lexists(path) and _unsafe_directory_link(path):
                raise ReviewRequired(f"job work member cannot be a link: {path.name}")
            path.mkdir(mode=0o750, parents=False, exist_ok=True)
            if path.resolve(strict=True).parent != resolved_work:
                raise ReviewRequired(
                    f"job work member escaped its work root: {path.name}"
                )
        return value


def _current_comparison_pngs(paths: JobPaths, *, prune: bool = False) -> list[Path]:
    try:
        resolved_job_root = paths.root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("comparison job root cannot be resolved safely") from exc
    for evidence_root in (paths.comparison, paths.analysis):
        try:
            resolved_evidence_root = evidence_root.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("comparison evidence root cannot be resolved") from exc
        if (
            _unsafe_directory_link(evidence_root)
            or resolved_evidence_root.parent != resolved_job_root
        ):
            raise RuntimeError(
                "comparison evidence root cannot be a link or escape its job root"
            )
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
        try:
            resolved_root = root.resolve(strict=True)
            resolved_path = path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(
                f"comparison image cannot be resolved safely: {name}"
            ) from exc
        if _unsafe_directory_link(path) or resolved_path.parent != resolved_root:
            raise RuntimeError(
                f"comparison image cannot be a link or escape its evidence root: {name}"
            )
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


def _current_video_metric_sidecars(paths: JobPaths) -> list[Path]:
    """Resolve only hash-pinned metric evidence named by the v2 manifests."""

    manifest_path = paths.comparison / "video-comparison.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("video comparison manifest is missing or invalid") from exc
    if not isinstance(manifest, Mapping):
        raise RuntimeError("video comparison manifest must be a JSON object")

    root = paths.comparison.resolve(strict=True)
    selected: dict[Path, None] = {}

    def include(
        record: object,
        *,
        description: str,
        allowed_suffix: str,
    ) -> Path:
        if not isinstance(record, Mapping):
            raise RuntimeError(f"{description} record is missing or invalid")
        name = record.get("path")
        expected_sha256 = record.get("sha256")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.casefold().endswith(allowed_suffix)
        ):
            raise RuntimeError(f"{description} has an unsafe path")
        if not isinstance(expected_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_sha256
        ):
            raise RuntimeError(f"{description} has an invalid SHA-256")
        path = paths.comparison / name
        if not path.is_file() or _unsafe_directory_link(path):
            raise RuntimeError(f"{description} is missing or is a link")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(f"{description} cannot be resolved safely") from exc
        if resolved.parent != root or sha256_file(resolved) != expected_sha256:
            raise RuntimeError(f"{description} differs from its manifest")
        selected[resolved] = None
        return resolved

    metrics_path = include(
        manifest.get("metrics"),
        description="video metrics manifest",
        allowed_suffix=".json",
    )
    try:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("video metrics document is invalid") from exc
    if not isinstance(metrics, Mapping) or not isinstance(metrics.get("samples"), list):
        raise RuntimeError("video metrics document has no samples array")
    for index, sample in enumerate(metrics["samples"], start=1):
        if not isinstance(sample, Mapping):
            raise RuntimeError(f"video metric sample {index} is invalid")
        include(
            sample.get("ssim_stats"),
            description=f"video metric sample {index} SSIM stats",
            allowed_suffix=".ssim.log",
        )
        include(
            sample.get("psnr_stats"),
            description=f"video metric sample {index} PSNR stats",
            allowed_suffix=".psnr.log",
        )
    return sorted(selected, key=lambda item: item.name)


_FORBIDDEN_PUBLIC_JSON_KEYS = {
    "api_key",
    "argv",
    "authorization",
    "command",
    "cpu",
    "credential",
    "delete_url",
    "encoder_settings",
    "job_id",
    "job_uuid",
    "host",
    "hostname",
    "local_path",
    "password",
    "platform",
    "machine",
    "secret",
    "settings",
    "settings_json",
    "source_path",
    "source_fingerprint",
    "scan_fingerprint",
    "stderr_path",
    "stdout_path",
    "token",
    "tools",
    "capabilities",
    "binary_versions",
    "os",
    "user_name",
    "userhash",
    "username",
}


def _assert_public_sidecar_safe(path: Path) -> None:
    """Reject private fields/values before any public upload or package copy."""

    try:
        assert_public_metadata_absent(path)
        if path.suffix.casefold() != ".json":
            return
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReviewRequired(
            f"public sidecar contains unsafe metadata: {path.name}",
            details={"sidecar": path.name},
        ) from exc

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized = str(key).strip().casefold().replace("-", "_")
                if normalized in _FORBIDDEN_PUBLIC_JSON_KEYS:
                    raise ReviewRequired(
                        f"public sidecar contains a private field: {path.name}",
                        details={"sidecar": path.name, "field": normalized},
                    )
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
        elif isinstance(value, str) and sanitize_text(value, public=True) != value:
            raise ReviewRequired(
                f"public sidecar contains private host metadata: {path.name}",
                details={"sidecar": path.name},
            )

    visit(document)


def _has_current_visual_annotations(document: Mapping[str, Any]) -> bool:
    annotation = document.get("visual_annotation")
    pairs = document.get("pairs")
    schema_version = document.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or schema_version < 4
        or not isinstance(annotation, Mapping)
        or annotation.get("schema_version") != 1
        or annotation.get("metrics_use_unannotated_pixels") is not True
        or annotation.get("metrics_use_native_yuv_planes") is not True
        or not isinstance(pairs, list)
        or not pairs
    ):
        return False
    for pair in pairs:
        if not isinstance(pair, Mapping):
            return False
        visual_label = pair.get("visual_label")
        if (
            not isinstance(visual_label, Mapping)
            or visual_label.get("reference_role") != "SOURCE"
            or visual_label.get("encode_role") != "ENCODE"
            or visual_label.get("frame_index") != pair.get("presentation_index")
            or visual_label.get("frame_index_base") != 0
            or visual_label.get("frame_type") != pair.get("category")
        ):
            return False
    return True


def _json_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _remove_owned_completed_child(completed: Path, child: Path) -> None:
    """Remove one exact BDEncode-owned package child without following links."""

    if child.parent != completed:
        raise RuntimeError("completed-package cleanup target is not a direct child")
    if not os.path.lexists(child):
        return
    if child.is_symlink():
        raise ReviewRequired(
            f"completed package contains an unsafe symbolic link: {child.name}"
        )
    completed_root = completed.resolve(strict=True)
    resolved = child.resolve(strict=True)
    if resolved.parent != completed_root:
        raise ReviewRequired(
            f"completed-package cleanup target escaped its release directory: {child.name}"
        )
    if resolved.is_dir():
        safe_tree_usage(resolved)
        shutil.rmtree(resolved)
    else:
        resolved.unlink()


def _replace_public_sidecars(
    *,
    completed: Path,
    comparison_source: Path,
    analysis_source: Path,
    comparison_files: Sequence[Path],
    analysis_files: Sequence[Path],
    expected_files: Mapping[Path, tuple[int, str]],
) -> None:
    """Atomically refresh the v2 public evidence trees from explicit allowlists."""

    comparison_stage = completed / ".comparison.partial"
    analysis_stage = completed / ".analysis.partial"
    for stage in (comparison_stage, analysis_stage):
        _remove_owned_completed_child(completed, stage)
        _ensure_contained_directory(
            stage,
            root=completed,
            description="public evidence staging directory",
        )
    comparison_root = comparison_source.resolve(strict=True)
    analysis_root = analysis_source.resolve(strict=True)
    copied: set[Path] = set()

    def copy_pinned(
        source: Path,
        *,
        source_root: Path,
        stage_root: Path,
        evidence_kind: str,
    ) -> None:
        if _unsafe_directory_link(source):
            raise ReviewRequired(
                f"public {evidence_kind} sidecar cannot be a symbolic link"
            )
        resolved = source.resolve(strict=True)
        if not resolved.is_file() or resolved.parent != source_root:
            raise ReviewRequired(
                f"public {evidence_kind} sidecar escaped its source directory"
            )
        if resolved in copied:
            raise ReviewRequired(f"public {evidence_kind} sidecar is duplicated")
        pin = expected_files.get(resolved)
        if pin is None:
            raise ReviewRequired(f"public {evidence_kind} sidecar is not hash-pinned")
        expected_size, expected_sha256 = pin
        if (
            resolved.stat().st_size != expected_size
            or sha256_file(resolved) != expected_sha256
        ):
            raise ReviewRequired(
                f"public {evidence_kind} sidecar changed after validation"
            )
        staged = stage_root / resolved.name
        shutil.copy2(resolved, staged)
        if (
            _unsafe_directory_link(staged)
            or not staged.is_file()
            or staged.stat().st_size != expected_size
            or sha256_file(staged) != expected_sha256
        ):
            raise ReviewRequired(
                f"public {evidence_kind} sidecar changed during package staging"
            )
        copied.add(resolved)

    try:
        for source in comparison_files:
            copy_pinned(
                source,
                source_root=comparison_root,
                stage_root=comparison_stage,
                evidence_kind="comparison",
            )
        for source in analysis_files:
            copy_pinned(
                source,
                source_root=analysis_root,
                stage_root=analysis_stage,
                evidence_kind="analysis",
            )
        if copied != set(expected_files):
            raise ReviewRequired(
                "public evidence allowlist omitted or added a hash-pinned sidecar"
            )
    except Exception:
        for stage in (comparison_stage, analysis_stage):
            _remove_owned_completed_child(completed, stage)
        raise

    for legacy in (
        completed / "logs",
        completed / "manifest.json",
        completed / "comparison",
        completed / "analysis",
    ):
        _remove_owned_completed_child(completed, legacy)
    os.replace(comparison_stage, completed / "comparison")
    os.replace(analysis_stage, completed / "analysis")


def _unsafe_directory_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _ensure_contained_directory(
    path: Path,
    *,
    root: Path,
    description: str,
) -> Path:
    """Create one workspace directory without following links outside ``root``."""

    try:
        if _unsafe_directory_link(root):
            raise ValueError("containment root is a link")
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError("containment root is not a directory")
        relative = path.relative_to(root)
        if not relative.parts or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ValueError("unsafe relative directory path")

        current = root
        resolved_parent = resolved_root
        for part in relative.parts:
            current = current / part
            if os.path.lexists(current) and _unsafe_directory_link(current):
                raise ValueError("directory component is a link")
            current.mkdir(mode=0o750, parents=False, exist_ok=True)
            if _unsafe_directory_link(current) or not current.is_dir():
                raise ValueError("directory component is not a real directory")
            resolved_current = current.resolve(strict=True)
            if resolved_current.parent != resolved_parent:
                raise ValueError("directory component escaped its parent")
            resolved_parent = resolved_current
    except (OSError, ValueError) as exc:
        raise ReviewRequired(
            f"{description} cannot be created safely inside its workspace"
        ) from exc
    return path


def _validate_completed_members(
    completed: Path, output_name: str, *, allow_legacy: bool
) -> None:
    allowed = {
        ".bdencode-owner.json",
        f"{output_name}.mkv",
        "comparison",
        "analysis",
    }
    if allow_legacy:
        allowed.update(
            {
                "logs",
                "manifest.json",
                f".{output_name}.mkv.partial",
                ".comparison.partial",
                ".analysis.partial",
            }
        )
    unexpected = sorted(
        item.name for item in completed.iterdir() if item.name not in allowed
    )
    if unexpected:
        raise ReviewRequired(
            "completed directory contains files outside the v2 public allowlist",
            details={"unexpected": unexpected},
        )
    for item in completed.iterdir():
        if _unsafe_directory_link(item):
            raise ReviewRequired(
                f"completed package member cannot be a link: {item.name}"
            )
        if item.name in {"comparison", "analysis", "logs"}:
            if not item.is_dir():
                raise ReviewRequired(
                    f"completed package member must be a directory: {item.name}"
                )
        elif not item.is_file():
            raise ReviewRequired(
                f"completed package member must be a regular file: {item.name}"
            )


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


def _recorded_output_sha256(marker: Path, output: Path) -> str | None:
    """Reuse a durable upstream stage digest without re-reading a huge MKV.

    The producer stage wrote this digest after creating the immutable output.
    A size/path check prevents accidentally borrowing a digest for another
    file; callers fall back to a fresh SHA-256 when the record is unavailable.
    """

    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
        if document.get("schema_version") != 1 or not output.is_file():
            return None
        expected_path = str(output.resolve(strict=True))
        output_stat = output.stat()
        expected_size = output_stat.st_size
        completed_at = float(document["completed_at_epoch"])
        # A normal producer writes its checkpoint only after the output digest
        # has been calculated.  A later mtime therefore means the file changed
        # in place and the recorded SHA-256 can no longer describe it, even if
        # its path and byte size remained identical.
        if output_stat.st_mtime > completed_at:
            return None
        for item in document.get("outputs", []):
            if (
                item.get("path") == expected_path
                and item.get("size_bytes") == expected_size
                and isinstance(item.get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            ):
                return item["sha256"]
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def _assert_stage_outputs_current(
    marker: Path,
    outputs: Sequence[Path],
    *,
    stage_name: str,
) -> dict[Path, tuple[int, str]]:
    """Freshly hash outputs and return immutable size/SHA pins from the marker."""

    verified: dict[Path, tuple[int, str]] = {}
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
        if document.get("schema_version") != 1:
            raise ValueError("unsupported marker schema")
        raw_records = document.get("outputs")
        if not isinstance(raw_records, list):
            raise ValueError("marker has no output records")
        records = {
            str(record.get("path")): record
            for record in raw_records
            if isinstance(record, Mapping)
        }
        for output in outputs:
            resolved = output.resolve(strict=True)
            if _unsafe_directory_link(output) or not resolved.is_file():
                raise ValueError(f"unsafe output: {output.name}")
            record = records.get(str(resolved))
            if not isinstance(record, Mapping):
                raise ValueError(f"unrecorded output: {output.name}")
            expected_sha256 = record.get("sha256")
            expected_size = record.get("size_bytes")
            if (
                not isinstance(expected_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
                or isinstance(expected_size, bool)
                or not isinstance(expected_size, int)
                or expected_size < 0
                or resolved.stat().st_size != expected_size
                or sha256_file(resolved) != expected_sha256
            ):
                raise ValueError(f"changed output: {output.name}")
            verified[resolved] = (expected_size, expected_sha256)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReviewRequired(
            f"{stage_name} evidence changed after its validated checkpoint",
            details={"stage": stage_name, "action": "rerun_qc"},
        ) from exc
    return verified


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


def _activate_source_log_generation(
    *,
    logs_root: Path,
    generation_record: Path,
    remux_marker: Path,
    generation: str,
) -> None:
    """Keep sticky source diagnostics scoped to one remux input generation."""

    if not re.fullmatch(r"[0-9a-f]{64}", generation):
        raise ValueError("source log generation must be a SHA-256 digest")
    previous: str | None = None
    if generation_record.is_file():
        document = json.loads(generation_record.read_text(encoding="utf-8"))
        candidate = document.get("generation")
        if not isinstance(candidate, str) or not re.fullmatch(
            r"[0-9a-f]{64}", candidate
        ):
            raise ValueError("source log generation record is invalid")
        previous = candidate
    elif remux_marker.is_file():
        try:
            marker_document = json.loads(remux_marker.read_text(encoding="utf-8"))
            candidate = marker_document.get("input_sha256")
            if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{64}", candidate):
                previous = candidate
        except json.JSONDecodeError:
            # The normal stage validator will reject a malformed marker. With
            # no trustworthy generation, retaining current logs is fail-closed.
            previous = None

    if previous is not None and previous != generation:
        history_root = _ensure_contained_directory(
            logs_root / "source-history" / previous,
            root=logs_root,
            description="source diagnostic history directory",
        )
        current_logs = sorted(
            {
                *logs_root.glob("reference-remux*.log"),
                *logs_root.glob("source-video-integrity*.log"),
            },
            key=lambda item: item.name,
        )
        for source in current_logs:
            destination = history_root / source.name
            suffix = 1
            while destination.exists():
                destination = history_root / (
                    f"{source.stem}.generation-{suffix:02d}{source.suffix}"
                )
                suffix += 1
            source.replace(destination)

    atomic_write_json(
        generation_record,
        {"schema_version": 1, "generation": generation},
    )


def _public_diagnostic_summary(
    diagnostics: Iterable[MediaDiagnostic],
) -> list[dict[str, object]]:
    """Return event-safe diagnostic counts without raw paths or log lines."""

    return [
        {
            "code": item.code,
            "category": item.category.value,
            "severity": item.severity.value,
            "count": item.count,
            "requires_review": item.requires_review,
        }
        for item in diagnostics
    ]


def _sticky_source_diagnostics(
    diagnostics: Iterable[MediaDiagnostic],
) -> tuple[MediaDiagnostic, ...]:
    """Retain only corruption/decode failures across same-input retries."""

    return tuple(
        item
        for item in diagnostics
        if item.requires_review
        and item.category
        in {
            DiagnosticCategory.SOURCE_CORRUPTION,
            DiagnosticCategory.DECODE_INTEGRITY,
        }
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


def _resolve_hdr10(
    scan: DiscScan,
    playlist_id: str,
    manual: Hdr10Metadata | None,
) -> Hdr10Metadata:
    playlist = scan.playlist(playlist_id)
    if not playlist.video_streams:
        raise ReviewRequired("the selected playlist has no video stream")
    video = playlist.video_streams[0].video
    assert video is not None
    source_hdr10 = video.hdr10 or str(video.color_transfer or "").casefold() == (
        "smpte2084"
    )
    if not source_hdr10:
        return manual or Hdr10Metadata()
    static = video.hdr10_static
    if manual is None:
        if not static.complete:
            raise ReviewRequired(
                "HDR10 static mastering metadata is incomplete; enter verified values",
                details={
                    "code": "source_hdr10_metadata_incomplete",
                    "playlist_id": playlist.playlist_id,
                    "confirmation_field": "selection.video.settings.hdr10",
                },
            )
        try:
            return Hdr10Metadata(
                enabled=True,
                mastering_display=static.mastering_display,
                max_cll=static.max_cll,
                max_fall=static.max_fall,
            )
        except (TypeError, ValueError) as exc:
            raise ReviewRequired(
                f"scanned HDR10 static metadata is invalid: {exc}",
                details={
                    "code": "invalid_source_hdr10_metadata",
                    "playlist_id": playlist.playlist_id,
                },
            ) from exc

    conflicts: dict[str, dict[str, object]] = {}
    if not manual.enabled:
        conflicts["enabled"] = {"required": True, "selected": manual.enabled}
    if not manual.hdr10_opt:
        conflicts["hdr10_opt"] = {
            "required": True,
            "selected": manual.hdr10_opt,
        }
    for field_name in ("mastering_display", "max_cll", "max_fall"):
        scanned_value = getattr(static, field_name)
        selected_value = getattr(manual, field_name)
        if scanned_value is not None and selected_value != scanned_value:
            conflicts[field_name] = {
                "scanned": scanned_value,
                "selected": selected_value,
            }
    if conflicts:
        raise ReviewRequired(
            "manual HDR10 metadata conflicts with the selected source",
            details={
                "code": "source_hdr10_metadata_conflict",
                "playlist_id": playlist.playlist_id,
                "confirmation_field": "selection.video.settings.hdr10",
                "scan_complete": static.complete,
                "conflicts": conflicts,
            },
        )
    return manual


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
    schema_version = raw.get("schema_version", 1)
    if isinstance(schema_version, bool) or schema_version not in {1, 2}:
        raise ReviewRequired("selection schema_version must be 1 or 2")
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
    # V1 stored the value sent to x264.  x264's Psy-RD path then applied -2
    # internally, so an authored 0 described an effective -2.  V2 stores the
    # effective value and compensates only when building the encoder command.
    overrides_raw = dict(overrides_raw)
    if (
        schema_version == 1
        and overrides_raw.get("chroma_qp_offset") == 0
        and scan.disc_kind is DiscKind.BD
    ):
        overrides_raw["chroma_qp_offset"] = -2
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
        hdr10 = _resolve_hdr10(scan, playlist_id, hdr10)
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
    if any(type(value) is not int for value in crop_raw.values()):
        raise ReviewRequired("video.crop values must be non-boolean integers")
    try:
        crop = VapourSynthCrop(**dict(crop_raw))
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

    source_video_streams = playlist.video_streams
    if not source_video_streams or source_video_streams[0].video is None:
        raise ReviewRequired("the selected playlist has no video stream")
    source_video = source_video_streams[0].video
    missing_geometry = [
        name
        for name, value in {
            "width": source_video.width,
            "height": source_video.height,
            "frame_rate": source_video.frame_rate,
        }.items()
        if value is None
    ]
    if missing_geometry:
        raise ReviewRequired(
            "source video geometry/timing is incomplete: " + ", ".join(missing_geometry)
        )
    output_width = (
        source_video.width - crop.left - crop.right
        if source_video.width is not None
        else None
    )
    output_height = (
        source_video.height - crop.top - crop.bottom
        if source_video.height is not None
        else None
    )
    try:
        settings, _video_policy = source_adapted_settings(
            settings,
            width=output_width,
            height=output_height,
            frame_rate=_vapoursynth_output_frame_rate(
                source_video.frame_rate, temporal_filter
            ),
        )
    except ValueError as exc:
        raise ReviewRequired(
            f"video compatibility policy rejected the selection: {exc}"
        ) from exc

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
        "subtitle_kind",
        "order",
    }
    for number, item in enumerate(tracks_raw):
        if not isinstance(item, Mapping) or set(item) - allowed_track:
            raise ReviewRequired(f"tracks[{number}] contains unknown fields")
        if "stream_id" not in item or "action" not in item:
            raise ReviewRequired(f"tracks[{number}] needs stream_id and action")
        action_value = str(item["action"]).lower()
        order_value = item.get("order", number)
        if type(order_value) is not int or order_value < 0:
            raise ReviewRequired(
                f"invalid tracks[{number}]: order must be a non-negative integer"
            )
        try:
            tracks.append(
                TrackSelection(
                    stream_id=str(item["stream_id"]),
                    action=TrackAction(action_value),
                    language=item.get("language"),
                    name=item.get("name"),
                    default=item.get("default"),
                    forced=item.get("forced"),
                    subtitle_kind=item.get("subtitle_kind"),
                    order=order_value,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ReviewRequired(f"invalid tracks[{number}]: {exc}") from exc

    stream_by_id = {stream.id: stream for stream in playlist.streams}
    if len({item.stream_id for item in tracks}) != len(tracks):
        raise ReviewRequired("each playlist track may be selected only once")
    for number, item in enumerate(tracks):
        stream = stream_by_id.get(item.stream_id)
        if stream is None:
            raise ReviewRequired(f"tracks[{number}] references an unknown stream")
        if stream.kind is StreamKind.AUDIO:
            if item.forced:
                raise ReviewRequired(f"tracks[{number}] audio cannot be forced")
            if item.subtitle_kind is not None:
                raise ReviewRequired(
                    f"tracks[{number}] audio cannot declare subtitle_kind"
                )
        elif stream.kind is StreamKind.SUBTITLE and item.action is not TrackAction.OMIT:
            if item.subtitle_kind not in {"full", "forced"}:
                raise ReviewRequired(
                    f"tracks[{number}] retained subtitle needs an explicit full/forced classification"
                )
            expected_forced = item.subtitle_kind == "forced"
            if item.forced is not None and item.forced is not expected_forced:
                raise ReviewRequired(
                    f"tracks[{number}] subtitle forced flag conflicts with subtitle_kind"
                )

    output_name = str(raw["output_name"]).removesuffix(".mkv")
    if not _SAFE_OUTPUT_RE.fullmatch(output_name) or output_name in {".", ".."}:
        raise ReviewRequired("output_name contains unsafe characters")
    _validate_release_name(
        output_name,
        encoder=encoder,
        playlist=playlist,
        tracks=tracks,
    )
    if not isinstance(raw["upload_images"], bool):
        raise ReviewRequired("upload_images must be boolean")
    image_upload_provider = str(raw.get("image_upload_provider", "auto")).lower()
    if image_upload_provider not in {"auto", "imgbb", "catbox", "freeimage"}:
        raise ReviewRequired(
            "image_upload_provider must be auto, imgbb, catbox or freeimage"
        )
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
        image_upload_provider=image_upload_provider,
        dual_type_match=dual_type,
        schema_version=2,
    )


def _field_handling(value: TemporalFilter) -> FieldHandling:
    if value is TemporalFilter.PROGRESSIVE:
        return FieldHandling.PROGRESSIVE
    if value in {TemporalFilter.IVTC_TFF, TemporalFilter.IVTC_BFF}:
        return FieldHandling.IVTC
    if value in {TemporalFilter.BWDIF_TFF, TemporalFilter.BWDIF_BFF}:
        return FieldHandling.DEINTERLACE
    return FieldHandling.HYBRID


def _vapoursynth_output_frame_rate(
    frame_rate: str | None, temporal_filter: TemporalFilter
) -> str | None:
    """Return the actual frame rate emitted by the reviewed VapourSynth graph."""

    if frame_rate is None:
        return None
    rate = parse_frame_rate(frame_rate)
    if temporal_filter in {TemporalFilter.IVTC_TFF, TemporalFilter.IVTC_BFF}:
        rate *= Fraction(4, 5)
    elif temporal_filter in {
        TemporalFilter.HYBRID_SAFE_BOB_TFF,
        TemporalFilter.HYBRID_SAFE_BOB_BFF,
    }:
        rate *= 2
    return format_frame_rate(rate)


def _planner_crop(value: VapourSynthCrop) -> PlannerCrop:
    return PlannerCrop(value.left, value.top, value.right, value.bottom)


def _validate_release_name(
    output_name: str,
    *,
    encoder: VideoEncoder,
    playlist: PlaylistCandidate,
    tracks: Sequence[TrackSelection],
) -> None:
    """Reject source-release metadata masquerading as an encode name."""

    expected = _X264_RELEASE_RE if encoder is VideoEncoder.X264 else _X265_RELEASE_RE
    if "_" in output_name or expected.fullmatch(output_name) is None:
        suffix = (
            "Title.Year.1080p.BluRay.x264-GROUP"
            if encoder is VideoEncoder.X264
            else "Title.Year.2160p.UHD.BluRay.x265-GROUP"
        )
        raise ReviewRequired(
            f"output_name must be a clean encode name such as {suffix}"
        )
    compact = re.sub(r"[^A-Z0-9]+", "", output_name.upper())
    if any(
        token in compact
        for token in ("COMPLETEBLURAY", "BDMV", "BLURAYAVC", "BLURAYHEVC")
    ):
        raise ReviewRequired(
            "output_name contains a source-disc/source-codec tag; remove inherited release metadata"
        )

    streams = {item.id: item for item in playlist.streams}
    retained_audio = [
        (item, streams[item.stream_id])
        for item in tracks
        if item.action is not TrackAction.OMIT
        and item.stream_id in streams
        and streams[item.stream_id].kind is StreamKind.AUDIO
    ]
    if re.search(r"(?:^|\.)MULTI(?:\.|$)", output_name, re.IGNORECASE):
        languages = {
            item.bcp47(stream).casefold()
            for item, stream in retained_audio
            if item.bcp47(stream) != "und"
        }
        if len(languages) < 2:
            raise ReviewRequired(
                "output_name says MULTi but the retained audio plan has fewer than two languages"
            )

    actual_audio_tokens: set[str] = set()
    for item, stream in retained_audio:
        source = re.sub(
            r"[^A-Z0-9]+", "", f"{stream.codec} {stream.codec_profile or ''}".upper()
        )
        if item.action is TrackAction.COPY and "DTS" in source and "HD" in source:
            actual_audio_tokens.add("DTSHDMA")
        actual_audio_tokens.add(
            {
                TrackAction.FLAC: "FLAC",
                TrackAction.AC3: "AC3",
                TrackAction.EAC3: "EAC3",
                TrackAction.DTS: "DTS",
            }.get(item.action, "")
        )
    advertised = {token for token in ("DTSHDMA", "FLAC", "EAC3") if token in compact}
    if advertised - actual_audio_tokens:
        raise ReviewRequired(
            "output_name advertises an audio codec that is not present in the retained output plan"
        )


def _track_name(item: TrackSelection, stream: MediaStream) -> str:
    return resolved_track_name(item, stream)


def _effective_audio_defaults(
    retained: Sequence[tuple[int, TrackSelection, MediaStream]],
) -> dict[str, bool]:
    try:
        return resolve_audio_defaults([(item, stream) for _, item, stream in retained])
    except ValueError as exc:
        raise ReviewRequired(str(exc)) from exc


def _sampled_video_metric_errors(
    samples: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Conservative hard gates for catastrophic sampled video degradation."""

    errors: list[str] = []
    ssim_by_type: dict[str, list[float]] = {"I": [], "P": [], "B": []}
    finite_ssim: list[float] = []
    finite_psnr: list[float] = []
    for index, sample in enumerate(samples, start=1):
        category = str(sample.get("category", ""))
        ssim = sample.get("ssim_all")
        psnr = sample.get("psnr_average_db")
        if not isinstance(ssim, float) or not 0 <= ssim <= 1:
            errors.append(f"sample {index} has no finite SSIM value")
        else:
            finite_ssim.append(ssim)
            if category in ssim_by_type:
                ssim_by_type[category].append(ssim)
            if ssim < 0.93:
                errors.append(f"sample {index} SSIM is below 0.93 ({ssim:.6f})")
        if isinstance(psnr, float):
            finite_psnr.append(psnr)
            if psnr < 35:
                errors.append(f"sample {index} PSNR is below 35 dB ({psnr:.3f} dB)")
        elif str(psnr).casefold() not in {"inf", "+inf"}:
            errors.append(f"sample {index} has no valid PSNR value")
    if finite_ssim and sum(finite_ssim) / len(finite_ssim) < 0.95:
        errors.append("mean sampled SSIM is below 0.95")
    if finite_psnr and sum(finite_psnr) / len(finite_psnr) < 38:
        errors.append("mean sampled PSNR is below 38 dB")
    if ssim_by_type["B"] and ssim_by_type["P"]:
        b_mean = sum(ssim_by_type["B"]) / len(ssim_by_type["B"])
        p_mean = sum(ssim_by_type["P"]) / len(ssim_by_type["P"])
        if p_mean - b_mean > 0.03:
            errors.append("B-frame sampled SSIM trails P-frames by more than 0.03")
    return tuple(errors)


class PipelineWorker:
    """One durable worker.  Running a second instance remains database-safe."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        scanner_factory: Callable[[Settings], BluRayScanner] | None = None,
        runner_factory: Callable[[JobPaths], Runner] | None = None,
        upload_client_factory: Callable[[], ImageUploadClient] | None = None,
        upload_client_factories: Sequence[Callable[[], ImageUploadClient]]
        | None = None,
        language_runtime: AudioLanguageRuntime | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.database = database
        self.settings = settings.validate()
        self.maintenance = MaintenanceJournal(database, self.settings)
        self.maintenance.recover()
        self.queue = JobQueue(database)
        self.scanner_factory = scanner_factory
        self.runner_factory = runner_factory or (
            lambda paths: CommandRunner(paths.logs / "commands.jsonl")
        )
        if upload_client_factory is not None and upload_client_factories is not None:
            raise ValueError(
                "use either upload_client_factory or upload_client_factories"
            )
        # Keep the historical single-factory attribute mutable for tests and
        # integrations.  Production uses the ordered provider chain.
        self.upload_client_factory = upload_client_factory
        self.upload_client_factories = tuple(
            upload_client_factories or (ImgBBClient, CatboxClient, FreeimageClient)
        )
        self.language_runtime = language_runtime or AudioLanguageRuntime(
            settings.data_root
        )
        self.stop_requested = stop_requested or (lambda: False)
        self._runners: dict[str, Runner] = {}

    def _runner(self, paths: JobPaths) -> Runner:
        key = str(paths.root)
        runner = self._runners.get(key)
        if runner is None:
            runner = self.runner_factory(paths)
            if isinstance(runner, CommandRunner):
                runner.set_interrupt_requested(
                    lambda: self._process_interrupt_requested(paths.root.name)
                )
            self._runners[key] = runner
        return runner

    def _process_interrupt_requested(self, job_id: str) -> bool:
        if self.stop_requested():
            return True
        try:
            control, _revision = self.database.get_control(job_id)
        except Exception:
            LOG.exception("job %s control polling failed; process continues", job_id)
            return False
        return control in {
            JobControlState.PAUSE_REQUESTED,
            JobControlState.CANCEL_REQUESTED,
        }

    def _acknowledge_operator_control(self, job_id: str) -> Job | None:
        """Acknowledge the latest request at a worker-safe boundary."""

        while True:
            control, revision = self.database.get_control(job_id)
            try:
                if control is JobControlState.PAUSE_REQUESTED:
                    return self.queue.acknowledge_pause(
                        job_id, expected_control_revision=revision
                    )
                if control is JobControlState.CANCEL_REQUESTED:
                    return self.queue.acknowledge_cancel(
                        job_id, expected_control_revision=revision
                    )
                if control is JobControlState.PAUSED:
                    return self.database.get_job(job_id)
                return None
            except StateConflictError:
                # Pause may be escalated to cancel between observation and ack.
                # Re-read instead of acknowledging the superseded revision.
                continue

    def _stop_at_operator_boundary(self, job_id: str) -> None:
        if self._acknowledge_operator_control(job_id) is not None:
            raise ProcessInterrupted("operator control acknowledged at safe boundary")

    @staticmethod
    def _remove_interrupted_partials(paths: JobPaths) -> None:
        """Remove exact owned temporary outputs without traversing workspace links."""

        # A recursive suffix search is unsafe here: an operator-controlled local
        # workspace could acquire a directory symlink or Windows junction while a
        # command is running, making cleanup walk outside the job root. Keep this
        # list aligned with the explicit temporary paths created by the pipeline.
        candidates = (paths.work / "video-encoded.partial.mkv",)
        for candidate in candidates:
            if not os.path.lexists(candidate) or candidate.is_symlink():
                continue
            try:
                resolved_parent = candidate.parent.resolve(strict=True)
                resolved_work = paths.work.resolve(strict=True)
                if (
                    resolved_parent != resolved_work
                    or _unsafe_directory_link(paths.work)
                    or _unsafe_directory_link(candidate)
                    or not candidate.is_file()
                ):
                    LOG.warning(
                        "refusing to remove unsafe interrupted partial %s", candidate
                    )
                    continue
                candidate.unlink()
            except OSError:
                LOG.warning("could not remove interrupted partial %s", candidate)

    def _image_upload_clients(self) -> list[ImageUploadClient]:
        factories = (
            (self.upload_client_factory,)
            if self.upload_client_factory is not None
            else self.upload_client_factories
        )
        clients: list[ImageUploadClient] = []
        names: set[str] = set()
        for factory in factories:
            client = factory()
            name = str(getattr(client, "provider_name", "imgbb")).strip().lower()
            if not name or name in names:
                raise ImageUploadError("image upload provider names must be unique")
            names.add(name)
            clients.append(client)
        if not clients:
            raise ImageUploadError("no image upload provider is configured")
        return clients

    def process_one_stage(self, job: Job) -> Job:
        """Run exactly the current durable stage and return refreshed state."""
        paths = JobPaths.create(self.settings, job.id)
        controlled = self._acknowledge_operator_control(job.id)
        if controlled is not None:
            return controlled
        if job.state is JobState.SCANNING:
            self._scan(job, paths)
        elif job.state is JobState.READY:
            self._prepare(job, paths)
        elif job.state is JobState.ENCODING:
            if not self._preparation_is_current(job, paths):
                self._prepare(job, paths, advance=False)
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
            if self.stop_requested():
                LOG.info(
                    "job %s paused at %s for worker shutdown",
                    job.id,
                    job.state.value,
                )
                return self.database.get_job(job.id)
            controlled = self._acknowledge_operator_control(job.id)
            if controlled is not None:
                return controlled
            if job.state in {
                JobState.AWAITING_SELECTION,
                JobState.NEEDS_REVIEW,
                JobState.UPLOAD_FAILED,
            }:
                return job
            try:
                before = job.state
                job = self.process_one_stage(job)
                if job.control_state is JobControlState.PAUSED:
                    return job
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
            except ProcessInterrupted:
                paths = JobPaths.create(self.settings, job.id)
                self._remove_interrupted_partials(paths)
                controlled = self._acknowledge_operator_control(job.id)
                current = controlled or self.database.get_job(job.id)
                if current.state is JobState.CANCELLED:
                    LOG.info(
                        "job %s encode stopped after operator cancellation", job.id
                    )
                elif current.control_state is JobControlState.PAUSED:
                    LOG.info(
                        "job %s paused at durable %s boundary",
                        job.id,
                        current.state.value,
                    )
                elif self.stop_requested():
                    LOG.info("job %s encode stopped for worker shutdown", job.id)
                else:
                    LOG.warning(
                        "job %s process was interrupted; durable state remains %s",
                        job.id,
                        current.state.value,
                    )
                # Keeping the pipeline stage lets a resumed invocation reuse
                # completed checkpoints and replay only the interrupted unit.
                return current
            except subprocess.TimeoutExpired as exc:
                current = self.database.get_job(job.id)
                controlled = self._acknowledge_operator_control(job.id)
                if controlled is not None:
                    self._remove_interrupted_partials(
                        JobPaths.create(self.settings, job.id)
                    )
                    return controlled
                if self.stop_requested():
                    LOG.info(
                        "job %s stopped during %s without a failure transition",
                        job.id,
                        current.state.value,
                    )
                    return current
                if current.state is JobState.COMPARISON:
                    return self.queue.needs_review(
                        job.id,
                        message=(
                            "fast comparison exceeded its bounded command/time budget"
                        ),
                        details={
                            "timeout_seconds": exc.timeout,
                            "command": redact_argv(exc.cmd or ()),
                        },
                    )
                self._fail(current, exc)
                return self.database.get_job(job.id)
            except (ImageUploadError, OSError) as exc:
                current = self.database.get_job(job.id)
                controlled = self._acknowledge_operator_control(job.id)
                if controlled is not None:
                    return controlled
                if self.stop_requested():
                    LOG.info(
                        "job %s stopped during %s without a failure transition",
                        job.id,
                        current.state.value,
                    )
                    return current
                if current.state is JobState.UPLOADING:
                    detail = sanitize_text(str(exc)).strip()[:800]
                    if not detail:
                        detail = type(exc).__name__
                    provider = (
                        exc.provider
                        if isinstance(exc, ImageUploadError) and exc.provider
                        else None
                    )
                    LOG.warning(
                        "job %s image upload failed via %s: %s",
                        job.id,
                        provider or "unselected provider",
                        detail,
                    )
                    return self.queue.advance(
                        job.id,
                        JobState.UPLOAD_FAILED,
                        message="image upload failed; retry is safe",
                        details={
                            "error_type": type(exc).__name__,
                            "provider": provider,
                            "detail": detail,
                        },
                    )
                self._fail(current, exc)
                return self.database.get_job(job.id)
            except Exception as exc:
                current = self.database.get_job(job.id)
                controlled = self._acknowledge_operator_control(job.id)
                if controlled is not None:
                    self._remove_interrupted_partials(
                        JobPaths.create(self.settings, job.id)
                    )
                    return controlled
                if self.stop_requested():
                    LOG.info(
                        "job %s stopped during %s without a failure transition",
                        job.id,
                        current.state.value,
                    )
                    return current
                self._fail(current, exc)
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
                scanner = BluRayScanner(
                    source_root=source_root,
                    runner=self._runner(paths),
                )
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
        audio_ordinals = {
            stream.id: ordinal for ordinal, stream in enumerate(playlist.audio_streams)
        }

        for track in selection.tracks:
            if track.action is TrackAction.OMIT or track.language is not None:
                continue
            stream = streams.get(track.stream_id)
            if stream is None:
                continue
            declared = stream.language
            if stream.kind is StreamKind.SUBTITLE:
                # Subtitle declarations remain usable when their independent
                # authored metadata agrees.  Audio is different: repeated
                # MPLS/CLPI tags have proven capable of carrying the same wrong
                # value, so every retained non-overridden audio track is sampled.
                if (
                    declared is not None
                    and declared.iso639_2t is not None
                    and not declared.needs_review
                ):
                    continue
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
                    audio_ordinals[stream.id],
                    playlist.duration_seconds,
                    paths.work,
                    self._runner(paths),
                    source_sha256=reference_digest,
                )
            except LanguageInferenceUnavailable as exc:
                evidence_records.append(
                    {
                        "stream_id": stream.id,
                        "decision": declared.to_dict() if declared else None,
                        "inference": {
                            "status": "unavailable",
                            "reason": exc.reason_code,
                        },
                    }
                )
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

    def _preparation_inputs(self, job: Job, scan: DiscScan) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "scan_fingerprint": scan.fingerprint,
            "selection": job.selection,
        }

    def _preparation_is_current(self, job: Job, paths: JobPaths) -> bool:
        try:
            scan, _selection = self._load_scan_and_selection(job, paths)
        except (OSError, TypeError, ValueError, KeyError):
            return False
        return _valid_stage(
            paths.stages / "pipeline-prepare.json",
            self._preparation_inputs(job, scan),
            [paths.reference, paths.script, paths.plan_json],
        )

    def _prepare(self, job: Job, paths: JobPaths, *, advance: bool = True) -> None:
        scan, selection = self._load_scan_and_selection(job, paths)
        playlist = scan.playlist(selection.playlist_id)
        pcm_bluray_audio: list[PcmBlurayAudio] = []
        for ordinal, stream in enumerate(playlist.audio_streams):
            if stream.codec.casefold() != "pcm_bluray":
                continue
            if stream.bit_depth is None:
                raise ReviewRequired(
                    f"Blu-ray PCM stream {stream.id} has no verified bit depth"
                )
            try:
                pcm_bluray_audio.append(PcmBlurayAudio(ordinal, stream.bit_depth))
            except ValueError as exc:
                raise ReviewRequired(
                    f"Blu-ray PCM stream {stream.id} has an unsupported bit depth: "
                    f"{stream.bit_depth}"
                ) from exc
        remux_inputs = {
            "scan_fingerprint": scan.fingerprint,
            "playlist_id": selection.playlist_id,
            "angle": selection.angle,
            "pcm_bluray_audio": [asdict(item) for item in pcm_bluray_audio],
            "source_clips": _playlist_source_snapshot(scan, selection.playlist_id),
        }
        remux_marker = paths.stages / "reference-remux.json"
        try:
            _activate_source_log_generation(
                logs_root=paths.logs,
                generation_record=(paths.analysis / "source-integrity-generation.json"),
                remux_marker=remux_marker,
                generation=_json_hash(remux_inputs),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReviewRequired(
                f"source integrity generation record is invalid: {exc}"
            ) from exc
        if not _valid_stage(remux_marker, remux_inputs, [paths.reference]):
            command = reference_remux_command(
                ReferenceRemuxPlan(
                    disc_root=scan.source,
                    playlist_id=selection.playlist_id,
                    output_path=paths.reference,
                    angle=selection.angle,
                    pcm_bluray_audio=tuple(pcm_bluray_audio),
                )
            )
            self._runner(paths).run(
                command,
                cwd=paths.work,
                stderr_path=paths.logs / "reference-remux.log",
            )
            _write_stage(remux_marker, remux_inputs, [paths.reference])

        integrity_report = paths.analysis / "source-video-integrity.json"
        integrity_log = paths.logs / "source-video-integrity.log"
        integrity_progress = paths.analysis / "source-video-integrity-progress.txt"
        integrity_inputs = {
            "policy_schema_version": 2,
            "reference_sha256": sha256_file(paths.reference),
            "context": "source",
            "decode_mode": "full-pixel-decode",
        }
        integrity_marker = paths.stages / "source-video-integrity.json"
        if not _valid_stage(
            integrity_marker,
            integrity_inputs,
            [integrity_report, integrity_progress],
        ):
            failure: ProcessFailure | None = None
            try:
                self._runner(paths).run(
                    source_video_integrity_command(paths.reference),
                    cwd=paths.work,
                    stdout_path=integrity_progress,
                    stderr_path=integrity_log,
                )
            except ProcessFailure as exc:
                failure = exc
            source_log_paths = sorted(
                {
                    *paths.logs.glob("reference-remux*.log"),
                    *paths.logs.glob("source-video-integrity*.log"),
                },
                key=lambda item: item.name,
            )
            source_text = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in source_log_paths
                if path.is_file()
            )
            diagnostics = classify_media_diagnostics(source_text, context="source")
            current_log_paths = (
                paths.logs / "reference-remux.log",
                integrity_log,
            )
            current_diagnostics = classify_media_diagnostics(
                "\n".join(
                    path.read_text(encoding="utf-8", errors="replace")
                    for path in current_log_paths
                    if path.is_file()
                ),
                context="source",
            )
            sticky_diagnostics = _sticky_source_diagnostics(diagnostics)
            current_requires_review = any(
                item.requires_review for item in current_diagnostics
            )
            needs_review = (
                failure is not None
                or current_requires_review
                or bool(sticky_diagnostics)
            )
            atomic_write_json(
                integrity_report,
                {
                    "schema_version": 2,
                    "status": "needs_review" if needs_review else "passed",
                    "command_failed": failure is not None,
                    "sticky_history": [path.name for path in source_log_paths],
                    "diagnostics": [item.to_dict() for item in diagnostics],
                },
            )
            if needs_review:
                public_diagnostics = (
                    *current_diagnostics,
                    *sticky_diagnostics,
                )
                raise ReviewRequired(
                    "source video integrity diagnostics require review before encoding",
                    details={
                        "report": integrity_report.name,
                        "diagnostics": _public_diagnostic_summary(public_diagnostics),
                    },
                )
            _write_stage(
                integrity_marker,
                integrity_inputs,
                [integrity_report, integrity_progress],
            )

        # Corruption reported by an earlier remux/decode attempt remains
        # material even after a later retry succeeds.  CommandRunner preserves
        # those stderr files as attempt-NN logs, so enforce the history on
        # every resume instead of trusting only the latest base log/marker.
        historical_source_logs = sorted(
            {
                *paths.logs.glob("reference-remux*.log"),
                *paths.logs.glob("source-video-integrity*.log"),
            },
            key=lambda item: item.name,
        )
        historical_diagnostics = classify_media_diagnostics(
            "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in historical_source_logs
                if path.is_file()
            ),
            context="source",
        )
        sticky_failures = _sticky_source_diagnostics(historical_diagnostics)
        if sticky_failures:
            atomic_write_json(
                integrity_report,
                {
                    "schema_version": 2,
                    "status": "needs_review",
                    "command_failed": False,
                    "sticky_history": [path.name for path in historical_source_logs],
                    "diagnostics": [item.to_dict() for item in historical_diagnostics],
                },
            )
            raise ReviewRequired(
                "source corruption appeared in the retained command history",
                details={
                    "report": integrity_report.name,
                    "diagnostics": _public_diagnostic_summary(sticky_failures),
                },
            )

        source_video = playlist.video_streams[0].video
        assert source_video is not None
        if playlist.duration_seconds <= 0:
            raise ReviewRequired(
                "title duration must be positive for distributed crop and QC sampling"
            )
        if source_video.width is None or source_video.height is None:
            raise ReviewRequired(
                "source dimensions are required for distributed crop validation"
            )
        crop_report = paths.analysis / "crop-policy.json"
        crop_inputs = {
            "policy_schema_version": 2,
            "strategy": "full-title-sequential-1fps",
            "reference_sha256": sha256_file(paths.reference),
            "duration_seconds": playlist.duration_seconds,
            "source_width": source_video.width,
            "source_height": source_video.height,
            "requested": asdict(selection.crop),
        }
        crop_marker = paths.stages / "crop-policy.json"
        if not _valid_stage(crop_marker, crop_inputs, [crop_report]):
            crop_log_path = paths.logs / "crop-detect-full-title.log"
            self._runner(paths).run(
                full_title_cropdetect_command(paths.reference),
                cwd=paths.work,
                stderr_path=crop_log_path,
            )
            crop_log = crop_log_path.read_text(encoding="utf-8", errors="replace")
            try:
                crop_evidence = parse_stable_cropdetect(
                    crop_log,
                    source_width=source_video.width,
                    source_height=source_video.height,
                )
                crop_decision = validate_operator_crop(
                    ActiveCropMargins(
                        left=selection.crop.left,
                        top=selection.crop.top,
                        right=selection.crop.right,
                        bottom=selection.crop.bottom,
                    ),
                    crop_evidence,
                )
            except CropPolicyError as exc:
                atomic_write_json(
                    crop_report,
                    {
                        "schema_version": 1,
                        "status": "needs_review",
                        "code": exc.code,
                        "message": str(exc),
                    },
                )
                raise ReviewRequired(
                    f"crop policy requires review: {exc}",
                    details={"code": exc.code, "report": crop_report.name},
                ) from exc
            atomic_write_json(
                crop_report,
                {
                    "schema_version": 1,
                    "status": "passed",
                    "evidence": crop_evidence.to_dict(),
                    "decision": crop_decision.to_dict(),
                },
            )
            _write_stage(crop_marker, crop_inputs, [crop_report])

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
            atomic_write_text(paths.script, content)
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
        _write_stage(
            paths.stages / "pipeline-prepare.json",
            self._preparation_inputs(job, scan),
            [paths.reference, paths.script, paths.plan_json],
        )
        if advance:
            self.queue.advance(
                job.id, JobState.ENCODING, message="reference timeline prepared"
            )

    def _encode(self, job: Job, paths: JobPaths) -> None:
        scan, selection = self._load_scan_and_selection(job, paths)
        if not paths.script.is_file() or not paths.reference.is_file():
            raise RuntimeError("prepared reference or VapourSynth script is missing")
        reference_sha256 = _recorded_output_sha256(
            paths.stages / "reference-remux.json", paths.reference
        )
        if reference_sha256 is None:
            raise RuntimeError("reference remux checkpoint digest is missing")
        inputs = {
            "policy_schema_version": 2,
            "scan_fingerprint": scan.fingerprint,
            "playlist_id": selection.playlist_id,
            "angle": selection.angle,
            "reference_sha256": reference_sha256,
            "script_sha256": sha256_file(paths.script),
            "settings": selection.settings.to_dict(),
        }

        def interrupted() -> bool:
            return self._process_interrupt_requested(job.id)

        marker = paths.stages / "video-encode.json"
        if not _valid_stage(marker, inputs, [paths.encoded_video]):
            temporary_video = paths.work / "video-encoded.partial.mkv"
            temporary_video.unlink(missing_ok=True)
            commands = encode_pipeline_commands(
                paths.script,
                temporary_video,
                selection.settings,
            )
            playlist = scan.playlist(selection.playlist_id)

            def persist_progress(
                progress: float, message: str, details: dict[str, object]
            ) -> None:
                self.database.record_progress(
                    job.id,
                    progress,
                    message=message,
                    details=details,
                    expected_state=JobState.ENCODING,
                    emit_event="milestone_percent" in details,
                )

            reporter: EncodeProgressReporter | None = None
            try:
                reporter = EncodeProgressReporter(
                    playlist.duration_seconds,
                    paths.logs / "video-progress.jsonl",
                    persist_progress,
                )
                reporter.start()
            except Exception:
                # Invalid legacy duration metadata must not turn optional
                # progress observation into an encode failure.
                LOG.exception(
                    "job %s video progress reporter is unavailable; encode continues",
                    job.id,
                )

            try:
                self._runner(paths).run_pipeline(
                    commands,
                    cwd=paths.work,
                    stderr_paths=[
                        paths.logs / "vapoursynth.log",
                        paths.logs / "video-encode.log",
                    ],
                    stderr_line_callback=reporter.handle_line if reporter else None,
                    interrupt_requested=interrupted,
                )
                # Close the tiny race in which cancellation commits after the
                # final poll but before a successful temporary output is
                # promoted to the durable checkpoint path.
                if interrupted():
                    raise ProcessInterrupted()
                os.replace(temporary_video, paths.encoded_video)
                if reporter is not None:
                    reporter.complete()
            except BaseException:
                temporary_video.unlink(missing_ok=True)
                raise
            _write_stage(marker, inputs, [paths.encoded_video])
        # Hashing and writing a multi-gigabyte checkpoint creates a real race
        # window after FFmpeg exits. Stop cleanly at the durable boundary rather
        # than entering mux/QC after shutdown or operator cancellation.
        if interrupted():
            raise ProcessInterrupted()
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

    def _probe_stream_starts(
        self,
        paths: JobPaths,
        media_path: Path,
        *,
        label: str,
    ) -> dict[int, Decimal]:
        if not re.fullmatch(r"[a-z0-9-]+", label):
            raise ValueError("unsafe stream-start probe label")
        report = paths.analysis / f"{label}-stream-starts.json"
        command = stream_start_probe_command(media_path)
        inputs = {"media_sha256": sha256_file(media_path), "argv": command}
        marker = paths.stages / f"stream-start-{label}.json"
        if not _valid_stage(marker, inputs, [report]):
            self._runner(paths).run(
                command,
                cwd=paths.work,
                stdout_path=report,
                stderr_path=paths.logs / f"{label}-stream-starts.log",
            )
            try:
                parse_stream_start_times(json.loads(report.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise ReviewRequired(
                    f"{label} has no trustworthy presentation start time"
                ) from exc
            _write_stage(marker, inputs, [report])
        try:
            return parse_stream_start_times(
                json.loads(report.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ReviewRequired(
                f"{label} stream-start report is missing or invalid"
            ) from exc

    def _mux(self, job: Job, paths: JobPaths) -> None:
        scan, selection = self._load_scan_and_selection(job, paths)
        if not paths.encoded_video.is_file() or not paths.reference.is_file():
            raise RuntimeError("encoded video or reference remux is missing")
        playlist = scan.playlist(selection.playlist_id)
        retained = [
            entry
            for entry in self._selected_streams(scan, selection)
            if entry[1].action is not TrackAction.OMIT
        ]
        default_audio = _effective_audio_defaults(retained)
        audio_ordinals = {
            stream.id: ordinal for ordinal, stream in enumerate(playlist.audio_streams)
        }
        subtitle_ordinals = {
            stream.id: ordinal
            for ordinal, stream in enumerate(playlist.subtitle_streams)
        }
        extracted_audio: list[tuple[int, TrackSelection, MediaStream, Path]] = []
        extracted_subtitles: list[tuple[int, TrackSelection, MediaStream, Path]] = []
        for number, item, stream in retained:
            if item.bcp47(stream) == "und":
                raise ReviewRequired(
                    f"retained track {stream.id} has no confirmed language; provide an override"
                )
            output = self._track_path(paths, number, item, stream)
            inputs = {
                "reference_sha256": sha256_file(paths.reference),
                "stream_id": stream.id,
                "source_stream_index": stream.index,
                "reference_type_ordinal": (
                    audio_ordinals[stream.id]
                    if stream.kind is StreamKind.AUDIO
                    else subtitle_ordinals[stream.id]
                ),
                "action": item.action.value,
            }
            if stream.kind is StreamKind.AUDIO:
                audio_policy = effective_audio_policy(
                    item.action.value,
                    source_codec=stream.codec,
                    source_profile=stream.codec_profile,
                    source_channels=stream.channels,
                    source_sample_rate=stream.sample_rate,
                    source_bit_depth=stream.bit_depth,
                )
                inputs["effective_audio_policy"] = audio_policy.to_dict()
            marker = paths.stages / f"track-{number:02d}.json"
            if not _valid_stage(marker, inputs, [output]):
                if stream.kind is StreamKind.AUDIO:
                    command = audio_track_command(
                        paths.reference,
                        audio_ordinals[stream.id],
                        output,
                        action=item.action.value,
                        source_codec=stream.codec,
                        source_profile=stream.codec_profile,
                        source_channels=stream.channels,
                        source_sample_rate=stream.sample_rate,
                        source_bit_depth=stream.bit_depth,
                    )
                elif stream.kind is StreamKind.SUBTITLE:
                    command = subtitle_track_command(
                        paths.reference, subtitle_ordinals[stream.id], output
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
            if stream.kind is StreamKind.SUBTITLE:
                subtitle_report = paths.analysis / f"subtitle-{number:02d}-probe.json"
                subtitle_inputs = {
                    "sidecar_sha256": sha256_file(output),
                    "subtitle_kind": item.subtitle_kind,
                    "title_duration_seconds": playlist.duration_seconds,
                }
                subtitle_marker = paths.stages / f"subtitle-{number:02d}-qc.json"
                if not _valid_stage(
                    subtitle_marker, subtitle_inputs, [subtitle_report]
                ):
                    self._runner(paths).run(
                        subtitle_probe_command(output),
                        cwd=paths.work,
                        stdout_path=subtitle_report,
                        stderr_path=paths.logs / f"subtitle-{number:02d}-probe.log",
                    )
                    try:
                        subtitle_probe = parse_subtitle_probe(
                            subtitle_report.read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError) as exc:
                        raise ReviewRequired(
                            f"subtitle {stream.id} could not be classified safely"
                        ) from exc
                    subtitle_errors = validate_subtitle_classification(
                        subtitle_probe,
                        subtitle_kind=item.subtitle_kind or "unknown",
                        title_duration_seconds=playlist.duration_seconds,
                    )
                    if subtitle_errors:
                        raise ReviewRequired(
                            f"subtitle {stream.id} classification requires review",
                            details={"errors": list(subtitle_errors)},
                        )
                    _write_stage(subtitle_marker, subtitle_inputs, [subtitle_report])
                extracted_subtitles.append((number, item, stream, output))
            elif stream.kind is StreamKind.AUDIO:
                if item.forced:
                    raise ReviewRequired("audio tracks cannot be forced")
                extracted_audio.append((number, item, stream, output))
            else:
                raise ReviewRequired(
                    f"unsupported selected track kind: {stream.kind.value}"
                )

        ordered_sidecars = [*extracted_audio, *extracted_subtitles]
        self._probe_stream_starts(paths, paths.reference, label="reference")
        try:
            reference_start_document = json.loads(
                (paths.analysis / "reference-stream-starts.json").read_text(
                    encoding="utf-8"
                )
            )
            reference_starts_by_type = parse_stream_start_times_by_type(
                reference_start_document
            )
            reference_video_start = reference_starts_by_type[("video", 0)]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReviewRequired(
                "reference video start time is missing or ambiguous"
            ) from exc
        retained_reference_starts: list[Decimal] = []
        for _number, _item, stream, _output in ordered_sidecars:
            key = (
                (
                    "audio",
                    audio_ordinals[stream.id],
                )
                if stream.kind is StreamKind.AUDIO
                else (
                    "subtitle",
                    subtitle_ordinals[stream.id],
                )
            )
            if key not in reference_starts_by_type:
                raise ReviewRequired(f"reference start time is missing for {stream.id}")
            retained_reference_starts.append(reference_starts_by_type[key])
        encoded_starts = self._probe_stream_starts(
            paths, paths.encoded_video, label="encoded-video"
        )
        if len(encoded_starts) != 1:
            raise ReviewRequired(
                "encoded-video sidecar must contain exactly one stream"
            )
        sidecar_starts: list[Decimal] = []
        for number, _item, _stream, output in ordered_sidecars:
            starts = self._probe_stream_starts(
                paths, output, label=f"track-{number:02d}"
            )
            if len(starts) != 1:
                raise ReviewRequired(
                    f"track-{number:02d} sidecar must contain exactly one stream"
                )
            sidecar_starts.append(next(iter(starts.values())))
        timeline = plan_common_zero_timeline(
            reference_video_start,
            retained_reference_starts,
            encoded_video_start=next(iter(encoded_starts.values())),
            sidecar_start_times=sidecar_starts,
        )
        atomic_write_json(
            paths.timeline_json,
            {
                "schema_version": 1,
                "origin_seconds": str(timeline.origin_seconds),
                "expected_start_seconds": [
                    str(value) for value in timeline.expected_start_seconds
                ],
                "video_sync_offset_ms": str(timeline.video_sync_offset_ms),
                "track_sync_offsets_ms": [
                    str(value) for value in timeline.track_sync_offsets_ms
                ],
            },
        )
        audio: list[MuxTrack] = []
        subtitles: list[MuxTrack] = []
        for (number, item, stream, output), sync_offset in zip(
            ordered_sidecars, timeline.track_sync_offsets_ms, strict=True
        ):
            is_subtitle = stream.kind is StreamKind.SUBTITLE
            track = MuxTrack(
                path=output,
                language=item.bcp47(stream),
                name=_track_name(item, stream),
                default=(
                    (stream.default if item.default is None else item.default)
                    if is_subtitle
                    else default_audio[stream.id]
                ),
                forced=is_subtitle and item.subtitle_kind == "forced",
                sync_offset_ms=sync_offset,
                subtitle_kind=(item.subtitle_kind or "unknown")
                if is_subtitle
                else None,
            )
            (subtitles if is_subtitle else audio).append(track)

        chapters = paths.work / "chapters.xml"
        chapters_path: Path | None = None
        if playlist.chapters:
            chapter_inputs = {
                "format": "matroska-chapters-v1",
                "scan_fingerprint": scan.fingerprint,
                "playlist_id": playlist.playlist_id,
                "chapter_starts": list(playlist.chapters),
            }
            chapter_marker = paths.stages / "chapters.json"
            if not _valid_stage(chapter_marker, chapter_inputs, [chapters]):
                atomic_write_text(chapters, render_matroska_chapters(playlist.chapters))
                _write_stage(chapter_marker, chapter_inputs, [chapters])
            chapters_path = chapters

        mux_inputs = {
            "video_sha256": sha256_file(paths.encoded_video),
            "tracks": [
                {"path": str(item.path), "sha256": sha256_file(item.path)}
                for item in (*audio, *subtitles)
            ],
            "chapters_sha256": (
                sha256_file(chapters_path) if chapters_path is not None else None
            ),
            "timeline_sha256": sha256_file(paths.timeline_json),
        }
        try:
            command = mkvmerge_command(
                paths.muxed_output,
                paths.encoded_video,
                audio_tracks=audio,
                subtitle_tracks=subtitles,
                chapters_path=chapters_path,
                title=selection.output_name,
                video_sync_offset_ms=timeline.video_sync_offset_ms,
            )
        except ValueError as exc:
            raise ReviewRequired(
                f"mux track policy rejected the release: {exc}"
            ) from exc
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
            if getattr(mux_result, "returncode", 0) == 1:
                raise ReviewRequired(
                    "mkvmerge completed with warnings; inspect mkvmerge.log before resuming"
                )
            _write_stage(marker, mux_inputs, [paths.muxed_output])
        self.queue.advance(job.id, JobState.QC, message="final Matroska mux complete")

    def _qc(self, job: Job, paths: JobPaths) -> None:
        scan, selection = self._load_scan_and_selection(job, paths)
        output = paths.muxed_output
        if not output.is_file():
            raise RuntimeError("mux output is missing")
        efficiency_report = paths.analysis / "video-efficiency.json"
        source_packets = paths.analysis / "source-video-packet-sizes.csv"
        encoded_packets = paths.analysis / "encoded-video-packet-sizes.csv"
        final_packets = paths.analysis / "final-video-packet-sizes.csv"
        efficiency_inputs = {
            "policy_schema_version": 2,
            "reference_sha256": sha256_file(paths.reference),
            "encoded_video_sha256": sha256_file(paths.encoded_video),
            "final_mkv_sha256": sha256_file(output),
            "minimum_savings_ratio": "0.001",
            "encoded_is_lossy": True,
        }
        efficiency_marker = paths.stages / "video-efficiency.json"
        if not _valid_stage(
            efficiency_marker,
            efficiency_inputs,
            [efficiency_report, source_packets, encoded_packets, final_packets],
        ):
            for media_path, packet_report, label in (
                (paths.reference, source_packets, "source"),
                (paths.encoded_video, encoded_packets, "encoded"),
                (output, final_packets, "final"),
            ):
                self._runner(paths).run(
                    video_packet_size_command(media_path),
                    cwd=paths.work,
                    stdout_path=packet_report,
                    stderr_path=paths.logs / f"{label}-video-packet-sizes.log",
                )
            try:
                source_summary = parse_video_packet_sizes(
                    source_packets.read_text(encoding="utf-8")
                )
                encoded_summary = parse_video_packet_sizes(
                    encoded_packets.read_text(encoding="utf-8")
                )
                verdict = require_video_efficiency(
                    source_summary.total_bytes,
                    encoded_summary.total_bytes,
                    encoded_is_lossy=True,
                )
            except (OSError, ValueError) as exc:
                failure = (
                    str(exc)
                    if isinstance(exc, VideoEfficiencyError)
                    else f"video packet-size evidence is invalid: {exc}"
                )
                atomic_write_json(
                    efficiency_report,
                    {
                        "schema_version": 1,
                        "status": "needs_review",
                        "error": failure,
                    },
                )
                raise ReviewRequired(
                    "lossy video efficiency policy requires review",
                    details={"error": failure, "report": efficiency_report.name},
                ) from exc
            atomic_write_json(
                efficiency_report,
                {
                    "schema_version": 1,
                    "status": "passed",
                    "source": source_summary.to_dict(),
                    "encoded": encoded_summary.to_dict(),
                    "verdict": verdict.to_dict(),
                },
            )
            _write_stage(
                efficiency_marker,
                efficiency_inputs,
                [efficiency_report, source_packets, encoded_packets, final_packets],
            )

        # Muxing must be a bit-for-bit packet-payload copy, not merely produce
        # the same number of decodable access units.  Hash the compressed video
        # stream on both sides of mkvmerge and fail closed on any mutation.
        mux_integrity_report = paths.analysis / "video-mux-integrity.json"
        mux_integrity_root = _ensure_contained_directory(
            paths.work / "video-mux-integrity",
            root=paths.work,
            description="video mux-integrity directory",
        )
        encoded_streamhash = mux_integrity_root / "encoded-video.streamhash"
        final_streamhash = mux_integrity_root / "final-video.streamhash"
        encoded_timeline_path = mux_integrity_root / "encoded-video-timeline.json"
        final_timeline_path = mux_integrity_root / "final-video-timeline.json"
        encoded_hash_command = video_stream_hash_command(paths.encoded_video)
        final_hash_command = video_stream_hash_command(output)
        encoded_timeline_command = packet_timeline_probe_command(
            paths.encoded_video, stream_type="v"
        )
        final_timeline_command = packet_timeline_probe_command(output, stream_type="v")
        mux_integrity_inputs = {
            "policy_schema_version": 3,
            "encoded_video_sha256": sha256_file(paths.encoded_video),
            "final_mkv_sha256": sha256_file(output),
            "commands": [
                encoded_hash_command,
                final_hash_command,
                encoded_timeline_command,
                final_timeline_command,
            ],
        }
        mux_integrity_marker = paths.stages / "video-mux-integrity.json"
        if not _valid_stage(
            mux_integrity_marker,
            mux_integrity_inputs,
            [
                mux_integrity_report,
                encoded_streamhash,
                final_streamhash,
                encoded_timeline_path,
                final_timeline_path,
            ],
        ):
            for command, streamhash, label in (
                (encoded_hash_command, encoded_streamhash, "encoded"),
                (final_hash_command, final_streamhash, "final"),
            ):
                self._runner(paths).run(
                    command,
                    cwd=paths.work,
                    stdout_path=streamhash,
                    stderr_path=paths.logs / f"{label}-video-streamhash.log",
                )
            for command, timeline_path, label in (
                (encoded_timeline_command, encoded_timeline_path, "encoded"),
                (final_timeline_command, final_timeline_path, "final"),
            ):
                self._runner(paths).run(
                    command,
                    cwd=paths.work,
                    stdout_path=timeline_path,
                    stderr_path=paths.logs / f"{label}-video-timeline.log",
                )
            try:
                encoded_payload_sha256 = parse_video_stream_hash(
                    encoded_streamhash.read_text(encoding="utf-8")
                )
                final_payload_sha256 = parse_video_stream_hash(
                    final_streamhash.read_text(encoding="utf-8")
                )
                encoded_timeline = parse_packet_timeline(
                    encoded_timeline_path.read_text(encoding="utf-8")
                )
                final_timeline = parse_packet_timeline(
                    final_timeline_path.read_text(encoding="utf-8")
                )
                timeline_verdict = compare_packet_timelines(
                    encoded_timeline, final_timeline
                )
            except (OSError, UnicodeError, ValueError) as exc:
                atomic_write_json(
                    mux_integrity_report,
                    {
                        "schema_version": 1,
                        "status": "needs_review",
                        "error": f"compressed video hash evidence is invalid: {exc}",
                    },
                )
                raise ReviewRequired(
                    "final video mux integrity evidence is invalid",
                    details={"report": mux_integrity_report.name},
                ) from exc
            payloads_match = encoded_payload_sha256 == final_payload_sha256
            passed = payloads_match and timeline_verdict.passed
            atomic_write_json(
                mux_integrity_report,
                {
                    "schema_version": 1,
                    "status": "passed" if passed else "needs_review",
                    "algorithm": "sha256",
                    "encoded_video_payload_sha256": encoded_payload_sha256,
                    "final_video_payload_sha256": final_payload_sha256,
                    "payloads_match": payloads_match,
                    "encoded_timeline": encoded_timeline.to_dict(),
                    "final_timeline": final_timeline.to_dict(),
                    "timeline_verdict": timeline_verdict.to_dict(),
                },
            )
            if not payloads_match:
                raise ReviewRequired(
                    "final mux changed the compressed video packet payload",
                    details={"report": mux_integrity_report.name},
                )
            if not timeline_verdict.passed:
                raise ReviewRequired(
                    "final mux changed the compressed video packet timeline",
                    details={"report": mux_integrity_report.name},
                )
            _write_stage(
                mux_integrity_marker,
                mux_integrity_inputs,
                [
                    mux_integrity_report,
                    encoded_streamhash,
                    final_streamhash,
                    encoded_timeline_path,
                    final_timeline_path,
                ],
            )
        report_root = _ensure_contained_directory(
            paths.analysis / "container",
            root=paths.analysis,
            description="container QC directory",
        )
        reports: list[Path] = [mux_integrity_report]
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
                if (
                    report.name == "mkvmerge-identify.json"
                    and getattr(inspection_result, "returncode", 0) == 1
                ):
                    raise ReviewRequired(
                        "mkvmerge identify completed with warnings; inspect its stderr before resuming"
                    )
                _write_stage(marker, inputs, [report])
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
        default_audio = _effective_audio_defaults(retained_streams)

        track_mux_integrity_report = paths.analysis / "track-mux-integrity.json"
        track_mux_integrity_root = _ensure_contained_directory(
            paths.work / "track-mux-integrity",
            root=paths.work,
            description="track mux-integrity directory",
        )
        track_mux_records: list[dict[str, Any]] = []
        track_mux_raw_outputs: list[Path] = []
        reference_audio_ordinals = {
            stream.id: ordinal for ordinal, stream in enumerate(playlist.audio_streams)
        }
        reference_subtitle_ordinals = {
            stream.id: ordinal
            for ordinal, stream in enumerate(playlist.subtitle_streams)
        }
        audio_ordinal = 0
        subtitle_ordinal = 0
        for number, item, stream in retained_streams:
            if stream.kind is StreamKind.AUDIO:
                stream_type: Literal["a", "s"] = "a"
                final_ordinal = audio_ordinal
                audio_ordinal += 1
            elif stream.kind is StreamKind.SUBTITLE:
                stream_type = "s"
                final_ordinal = subtitle_ordinal
                subtitle_ordinal += 1
            else:
                continue
            reference_ordinal = (
                reference_audio_ordinals[stream.id]
                if stream_type == "a"
                else reference_subtitle_ordinals[stream.id]
            )
            intermediate = self._track_path(paths, number, item, stream)
            sidecar_hash_path = (
                track_mux_integrity_root
                / f"track-{number:02d}-{stream_type}-sidecar.streamhash"
            )
            final_hash_path = (
                track_mux_integrity_root
                / f"track-{number:02d}-{stream_type}-final.streamhash"
            )
            sidecar_timeline_path = (
                track_mux_integrity_root
                / f"track-{number:02d}-{stream_type}-sidecar-timeline.json"
            )
            final_timeline_path = (
                track_mux_integrity_root
                / f"track-{number:02d}-{stream_type}-final-timeline.json"
            )
            sidecar_command = stream_payload_hash_command(
                intermediate,
                stream_type=stream_type,
            )
            final_command = stream_payload_hash_command(
                output,
                stream_type=stream_type,
                stream=final_ordinal,
            )
            sidecar_timeline_command = packet_timeline_probe_command(
                intermediate,
                stream_type=stream_type,
            )
            final_timeline_command = packet_timeline_probe_command(
                output,
                stream_type=stream_type,
                stream=final_ordinal,
            )
            track_mux_raw_outputs.extend(
                (
                    sidecar_hash_path,
                    final_hash_path,
                    sidecar_timeline_path,
                    final_timeline_path,
                )
            )
            track_mux_records.append(
                {
                    "track_number": number,
                    "stream_id": stream.id,
                    "stream_type": stream_type,
                    "final_type_ordinal": final_ordinal,
                    "reference_type_ordinal": reference_ordinal,
                    "copy_from_reference": item.action is TrackAction.COPY,
                    "intermediate": intermediate,
                    "intermediate_sha256": sha256_file(intermediate),
                    "sidecar_hash_path": sidecar_hash_path,
                    "final_hash_path": final_hash_path,
                    "sidecar_timeline_path": sidecar_timeline_path,
                    "final_timeline_path": final_timeline_path,
                    "sidecar_command": sidecar_command,
                    "final_command": final_command,
                    "sidecar_timeline_command": sidecar_timeline_command,
                    "final_timeline_command": final_timeline_command,
                }
            )
            if item.action is TrackAction.COPY:
                record = track_mux_records[-1]
                reference_hash_path = (
                    track_mux_integrity_root
                    / f"track-{number:02d}-{stream_type}-reference.streamhash"
                )
                reference_timeline_path = (
                    track_mux_integrity_root
                    / f"track-{number:02d}-{stream_type}-reference-timeline.json"
                )
                record.update(
                    {
                        "reference_hash_path": reference_hash_path,
                        "reference_timeline_path": reference_timeline_path,
                        "reference_command": stream_payload_hash_command(
                            paths.reference,
                            stream_type=stream_type,
                            stream=reference_ordinal,
                        ),
                        "reference_timeline_command": packet_timeline_probe_command(
                            paths.reference,
                            stream_type=stream_type,
                            stream=reference_ordinal,
                        ),
                    }
                )
                track_mux_raw_outputs.extend(
                    (reference_hash_path, reference_timeline_path)
                )
        track_mux_inputs = {
            "policy_schema_version": 2,
            "reference_sha256": sha256_file(paths.reference),
            "final_mkv_sha256": sha256_file(output),
            "tracks": [
                {
                    key: str(value) if isinstance(value, Path) else value
                    for key, value in record.items()
                    if key
                    not in {
                        "sidecar_hash_path",
                        "final_hash_path",
                        "sidecar_timeline_path",
                        "final_timeline_path",
                        "reference_hash_path",
                        "reference_timeline_path",
                    }
                }
                for record in track_mux_records
            ],
        }
        track_mux_marker = paths.stages / "track-mux-integrity.json"
        track_mux_outputs = [track_mux_integrity_report, *track_mux_raw_outputs]
        if not _valid_stage(
            track_mux_marker,
            track_mux_inputs,
            track_mux_outputs,
        ):
            track_results: list[dict[str, Any]] = []
            for record in track_mux_records:
                for command_key, output_key, label in (
                    ("sidecar_command", "sidecar_hash_path", "sidecar"),
                    ("final_command", "final_hash_path", "final"),
                ):
                    self._runner(paths).run(
                        record[command_key],
                        cwd=paths.work,
                        stdout_path=record[output_key],
                        stderr_path=(
                            paths.logs
                            / f"track-{record['track_number']:02d}-{label}-streamhash.log"
                        ),
                    )
                for command_key, output_key, label in (
                    (
                        "sidecar_timeline_command",
                        "sidecar_timeline_path",
                        "sidecar",
                    ),
                    (
                        "final_timeline_command",
                        "final_timeline_path",
                        "final",
                    ),
                ):
                    self._runner(paths).run(
                        record[command_key],
                        cwd=paths.work,
                        stdout_path=record[output_key],
                        stderr_path=(
                            paths.logs
                            / f"track-{record['track_number']:02d}-{label}-timeline.log"
                        ),
                    )
                if record["copy_from_reference"]:
                    for command_key, output_key, suffix in (
                        (
                            "reference_command",
                            "reference_hash_path",
                            "streamhash",
                        ),
                        (
                            "reference_timeline_command",
                            "reference_timeline_path",
                            "timeline",
                        ),
                    ):
                        self._runner(paths).run(
                            record[command_key],
                            cwd=paths.work,
                            stdout_path=record[output_key],
                            stderr_path=(
                                paths.logs
                                / f"track-{record['track_number']:02d}-reference-{suffix}.log"
                            ),
                        )
                try:
                    sidecar_payload_sha256 = parse_stream_payload_hash(
                        record["sidecar_hash_path"].read_text(encoding="utf-8"),
                        expected_stream_type=record["stream_type"],
                    )
                    final_payload_sha256 = parse_stream_payload_hash(
                        record["final_hash_path"].read_text(encoding="utf-8"),
                        expected_stream_type=record["stream_type"],
                    )
                    sidecar_timeline = parse_packet_timeline(
                        record["sidecar_timeline_path"].read_text(encoding="utf-8")
                    )
                    final_timeline = parse_packet_timeline(
                        record["final_timeline_path"].read_text(encoding="utf-8")
                    )
                    timeline_verdict = compare_packet_timelines(
                        sidecar_timeline, final_timeline
                    )
                    reference_payload_sha256 = None
                    reference_timeline = None
                    extraction_payloads_match = None
                    extraction_timeline_verdict = None
                    if record["copy_from_reference"]:
                        reference_payload_sha256 = parse_stream_payload_hash(
                            record["reference_hash_path"].read_text(encoding="utf-8"),
                            expected_stream_type=record["stream_type"],
                        )
                        reference_timeline = parse_packet_timeline(
                            record["reference_timeline_path"].read_text(
                                encoding="utf-8"
                            )
                        )
                        extraction_payloads_match = (
                            reference_payload_sha256 == sidecar_payload_sha256
                        )
                        extraction_timeline_verdict = compare_packet_timelines(
                            reference_timeline, sidecar_timeline
                        )
                except (OSError, UnicodeError, ValueError) as exc:
                    atomic_write_json(
                        track_mux_integrity_report,
                        {
                            "schema_version": 1,
                            "status": "needs_review",
                            "tracks": track_results,
                            "error": (
                                "track payload hash evidence is invalid for "
                                f"{record['stream_id']}: {exc}"
                            ),
                        },
                    )
                    raise ReviewRequired(
                        "final track mux integrity evidence is invalid",
                        details={
                            "stream_id": record["stream_id"],
                            "report": track_mux_integrity_report.name,
                        },
                    ) from exc
                payloads_match = sidecar_payload_sha256 == final_payload_sha256
                track_results.append(
                    {
                        "track_number": record["track_number"],
                        "stream_id": record["stream_id"],
                        "stream_type": record["stream_type"],
                        "final_type_ordinal": record["final_type_ordinal"],
                        "sidecar_payload_sha256": sidecar_payload_sha256,
                        "final_payload_sha256": final_payload_sha256,
                        "payloads_match": payloads_match,
                        "sidecar_timeline": sidecar_timeline.to_dict(),
                        "final_timeline": final_timeline.to_dict(),
                        "timeline_verdict": timeline_verdict.to_dict(),
                        "reference_payload_sha256": reference_payload_sha256,
                        "reference_timeline": (
                            reference_timeline.to_dict()
                            if reference_timeline is not None
                            else None
                        ),
                        "extraction_payloads_match": extraction_payloads_match,
                        "extraction_timeline_verdict": (
                            extraction_timeline_verdict.to_dict()
                            if extraction_timeline_verdict is not None
                            else None
                        ),
                    }
                )
                if extraction_payloads_match is False:
                    atomic_write_json(
                        track_mux_integrity_report,
                        {
                            "schema_version": 1,
                            "status": "needs_review",
                            "tracks": track_results,
                        },
                    )
                    raise ReviewRequired(
                        "copy extraction changed an audio/subtitle packet payload",
                        details={
                            "stream_id": record["stream_id"],
                            "report": track_mux_integrity_report.name,
                        },
                    )
                if (
                    extraction_timeline_verdict is not None
                    and not extraction_timeline_verdict.passed
                ):
                    atomic_write_json(
                        track_mux_integrity_report,
                        {
                            "schema_version": 1,
                            "status": "needs_review",
                            "tracks": track_results,
                        },
                    )
                    raise ReviewRequired(
                        "copy extraction changed an audio/subtitle packet timeline",
                        details={
                            "stream_id": record["stream_id"],
                            "report": track_mux_integrity_report.name,
                        },
                    )
                if not payloads_match:
                    atomic_write_json(
                        track_mux_integrity_report,
                        {
                            "schema_version": 1,
                            "status": "needs_review",
                            "tracks": track_results,
                        },
                    )
                    raise ReviewRequired(
                        "final mux changed a retained audio/subtitle packet payload",
                        details={
                            "stream_id": record["stream_id"],
                            "report": track_mux_integrity_report.name,
                        },
                    )
                if not timeline_verdict.passed:
                    atomic_write_json(
                        track_mux_integrity_report,
                        {
                            "schema_version": 1,
                            "status": "needs_review",
                            "tracks": track_results,
                        },
                    )
                    raise ReviewRequired(
                        "final mux changed a retained audio/subtitle packet timeline",
                        details={
                            "stream_id": record["stream_id"],
                            "report": track_mux_integrity_report.name,
                        },
                    )
            atomic_write_json(
                track_mux_integrity_report,
                {
                    "schema_version": 1,
                    "status": "passed",
                    "tracks": track_results,
                },
            )
            _write_stage(
                track_mux_marker,
                track_mux_inputs,
                track_mux_outputs,
            )
        reports.append(track_mux_integrity_report)
        expected_audio: list[MuxTrack] = []
        expected_subtitles: list[MuxTrack] = []
        for _number, item, stream in retained_streams:
            is_subtitle = stream.kind is StreamKind.SUBTITLE
            expected = MuxTrack(
                path=Path("unused"),
                language=item.bcp47(stream),
                name=_track_name(item, stream),
                default=(
                    (stream.default if item.default is None else item.default)
                    if is_subtitle
                    else default_audio[stream.id]
                ),
                forced=is_subtitle and item.subtitle_kind == "forced",
                subtitle_kind=(item.subtitle_kind or "unknown")
                if is_subtitle
                else None,
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
            level=_ffprobe_level_idc(
                selection.settings.encoder, selection.settings.level
            ),
        )
        audio_policies = [
            FinalTrackPolicy(
                "audio", expected_audio_codec(item.action.value, stream.codec)
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
        try:
            timeline_document = json.loads(
                paths.timeline_json.read_text(encoding="utf-8")
            )
            expected_start_times = timeline_document["expected_start_seconds"]
            if not isinstance(expected_start_times, list):
                raise TypeError("expected_start_seconds is not an array")
            timeline_expected_starts = tuple(
                Decimal(str(value)) for value in expected_start_times
            )
            start_errors = validate_stream_start_times(
                ffprobe_document,
                expected_start_times=timeline_expected_starts,
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReviewRequired("timeline plan is missing or invalid") from exc
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
        policy_errors = (*stream_errors, *start_errors, *hdr_errors)
        if policy_errors:
            raise ReviewRequired(
                "final media streams differ from the reviewed codec/color/HDR policy",
                details={"errors": list(policy_errors)},
            )

        # The general full-decode pass maps video/audio only.  Send every final
        # subtitle event through FFmpeg's actual decoder; packet-copy/remux
        # evidence cannot prove that a PGS/text payload is parseable.
        subtitle_integrity_results: list[dict[str, Any]] = []
        subtitle_decode_reports: list[Path] = []
        retained_subtitle_entries = [
            entry for entry in retained_streams if entry[2].kind is StreamKind.SUBTITLE
        ]
        for subtitle_ordinal, (number, _item, stream) in enumerate(
            retained_subtitle_entries
        ):
            subtitle_decode_report = (
                report_root / f"subtitle-{subtitle_ordinal + 1:02d}-decode.json"
            )
            subtitle_decode_log = (
                paths.logs / f"subtitle-{subtitle_ordinal + 1:02d}-decode.log"
            )
            integrity_command = subtitle_decode_probe_command(output, subtitle_ordinal)
            sidecar_probe_path = paths.analysis / f"subtitle-{number:02d}-probe.json"
            integrity_inputs = {
                "policy_schema_version": 5,
                "output_sha256": sha256_file(output),
                "subtitle_ordinal": subtitle_ordinal,
                "sidecar_probe_sha256": sha256_file(sidecar_probe_path),
                "title_duration_seconds": playlist.duration_seconds,
                "argv": integrity_command,
            }
            integrity_marker = (
                paths.stages / f"qc-subtitle-{subtitle_ordinal + 1:02d}-integrity.json"
            )
            try:
                if not _valid_stage(
                    integrity_marker,
                    integrity_inputs,
                    [subtitle_decode_report, subtitle_decode_log],
                ):
                    self._runner(paths).run(
                        integrity_command,
                        cwd=paths.work,
                        stdout_path=subtitle_decode_report,
                        stderr_path=subtitle_decode_log,
                    )
                if subtitle_decode_log.read_text(
                    encoding="utf-8", errors="replace"
                ).strip():
                    raise SubtitleDecodeError(
                        "subtitle decoder emitted error-level diagnostics"
                    )
                verdict = require_subtitle_decode(
                    subtitle_decode_report.read_text(encoding="utf-8")
                )
                sidecar_probe = parse_subtitle_probe(
                    sidecar_probe_path.read_text(encoding="utf-8")
                )
                if (
                    stream.codec.casefold() == "hdmv_pgs_subtitle"
                    and verdict.decoded_event_count != sidecar_probe.packet_count
                ):
                    raise SubtitleDecodeError(
                        "decoded PGS event count differs from the sidecar packet count"
                    )
                title_duration = Decimal(str(playlist.duration_seconds))
                timestamp_tolerance = Decimal("0.100")
                if (
                    verdict.first_timestamp is None
                    or verdict.last_timestamp is None
                    or verdict.first_timestamp < -timestamp_tolerance
                    or verdict.last_timestamp > title_duration + timestamp_tolerance
                ):
                    raise SubtitleDecodeError(
                        "decoded subtitle timestamps fall outside the reviewed title"
                    )
            except (OSError, UnicodeError, ProcessFailure, SubtitleDecodeError) as exc:
                subtitle_integrity_results.append(
                    {
                        "subtitle_ordinal": subtitle_ordinal,
                        "status": "needs_review",
                        "error": str(exc),
                    }
                )
                subtitle_integrity_report = report_root / "subtitle-integrity.json"
                atomic_write_json(
                    subtitle_integrity_report,
                    {
                        "schema_version": 2,
                        "status": "needs_review",
                        "tracks": subtitle_integrity_results,
                    },
                )
                raise ReviewRequired(
                    "final subtitle payload decode requires review",
                    details={
                        "subtitle_ordinal": subtitle_ordinal,
                        "report": subtitle_integrity_report.name,
                    },
                ) from exc
            if not _valid_stage(
                integrity_marker,
                integrity_inputs,
                [subtitle_decode_report, subtitle_decode_log],
            ):
                _write_stage(
                    integrity_marker,
                    integrity_inputs,
                    [subtitle_decode_report, subtitle_decode_log],
                )
            subtitle_integrity_results.append(
                {
                    "subtitle_ordinal": subtitle_ordinal,
                    "status": "passed",
                    "decode": verdict.to_dict(),
                    "sidecar_packet_count": sidecar_probe.packet_count,
                    "evidence_sha256": sha256_file(subtitle_decode_report),
                }
            )
            subtitle_decode_reports.append(subtitle_decode_report)
        subtitle_integrity_report = report_root / "subtitle-integrity.json"
        atomic_write_json(
            subtitle_integrity_report,
            {
                "schema_version": 2,
                "status": "passed",
                "tracks": subtitle_integrity_results,
            },
        )
        reports.extend(subtitle_decode_reports)
        reports.append(subtitle_integrity_report)

        audio_manifest_path = paths.analysis / "audio-comparison.json"
        audio_outputs: list[Path] = [audio_manifest_path]
        audio_results: list[dict[str, Any]] = []
        spectrograms: list[Path] = []
        audio_ordinals = {
            item.id: index for index, item in enumerate(playlist.audio_streams)
        }
        audio_inputs: dict[str, Any] = {
            "manifest_schema_version": 3,
            "audio_decode_policy_schema_version": AUDIO_DECODE_POLICY_SCHEMA_VERSION,
            "audio_frame_continuity_schema_version": (
                AUDIO_FRAME_CONTINUITY_SCHEMA_VERSION
            ),
            "reference_sha256": sha256_file(paths.reference),
            "output_sha256": sha256_file(output),
            "tracks": [],
        }
        retained = [
            entry for entry in retained_streams if entry[2].kind is StreamKind.AUDIO
        ]
        for final_audio_ordinal, (number, item, stream) in enumerate(retained):
            audio_policy = effective_audio_policy(
                item.action.value,
                source_codec=stream.codec,
                source_profile=stream.codec_profile,
                source_channels=stream.channels,
                source_sample_rate=stream.sample_rate,
                source_bit_depth=stream.bit_depth,
            )
            intermediate = self._track_path(paths, number, item, stream)
            source_audio_ordinal = audio_ordinals[stream.id]
            source_input_codec = normalize_audio_codec_name(stream.codec)
            final_input_codec = normalize_audio_codec_name(
                expected_audio_codec(item.action.value, stream.codec)
            )
            decode_policy = {
                "schema_version": AUDIO_DECODE_POLICY_SCHEMA_VERSION,
                "source_input_codec": source_input_codec,
                "source_input_args": audio_decode_input_args(source_input_codec),
                "final_input_codec": final_input_codec,
                "final_input_args": audio_decode_input_args(final_input_codec),
            }
            continuity_required = item.action is not TrackAction.COPY
            prefix = paths.analysis / f"audio-{number:02d}"
            source_probe = prefix.with_name(prefix.name + "-source-probe.json")
            encode_probe = prefix.with_name(prefix.name + "-encode-probe.json")
            frame_evidence_root = (
                paths.work / "audio-frame-continuity" / (f"audio-{number:02d}")
            )
            if continuity_required:
                frame_evidence_root = _ensure_contained_directory(
                    frame_evidence_root,
                    root=paths.work,
                    description="audio frame-continuity evidence directory",
                )
            source_frames = frame_evidence_root / "source-frames.json"
            sidecar_frames = frame_evidence_root / "sidecar-frames.json"
            final_frames = frame_evidence_root / "final-frames.json"
            source_pcm = prefix.with_name(prefix.name + "-source-pcm.sha256")
            encode_pcm = prefix.with_name(prefix.name + "-encode-pcm.sha256")
            source_analysis = prefix.with_name(prefix.name + "-source-analysis.log")
            encode_analysis = prefix.with_name(prefix.name + "-encode-analysis.log")
            source_spectrum = prefix.with_name(prefix.name + "-source-spectrum.png")
            encode_spectrum = prefix.with_name(prefix.name + "-encode-spectrum.png")
            spectrograms.extend((source_spectrum, encode_spectrum))
            intermediate_sha256 = sha256_file(intermediate)
            track_command_inputs = {
                "reference": audio_inputs["reference_sha256"],
                "final_output": audio_inputs["output_sha256"],
                "intermediate": intermediate_sha256,
                "effective_audio_policy": audio_policy.to_dict(),
                "audio_decode_policy": decode_policy,
                "audio_frame_continuity_schema_version": (
                    AUDIO_FRAME_CONTINUITY_SCHEMA_VERSION
                ),
            }
            commands: list[tuple[list[str], Path, Literal["stdout", "stderr"]]] = [
                (
                    audio_probe_command(paths.reference, source_audio_ordinal),
                    source_probe,
                    "stdout",
                ),
                (
                    audio_probe_command(output, final_audio_ordinal),
                    encode_probe,
                    "stdout",
                ),
                (
                    analysis_command(
                        paths.reference,
                        source_audio_ordinal,
                        source_analysis,
                        input_codec=source_input_codec,
                    ),
                    source_analysis,
                    "stderr",
                ),
                (
                    analysis_command(
                        output,
                        final_audio_ordinal,
                        encode_analysis,
                        input_codec=final_input_codec,
                    ),
                    encode_analysis,
                    "stderr",
                ),
            ]
            if continuity_required:
                commands[2:2] = [
                    (
                        audio_frame_continuity_probe_command(
                            paths.reference,
                            source_audio_ordinal,
                            input_codec=source_input_codec,
                        ),
                        source_frames,
                        "stdout",
                    ),
                    (
                        audio_frame_continuity_probe_command(
                            intermediate,
                            input_codec=final_input_codec,
                        ),
                        sidecar_frames,
                        "stdout",
                    ),
                    (
                        audio_frame_continuity_probe_command(
                            output,
                            final_audio_ordinal,
                            input_codec=final_input_codec,
                        ),
                        final_frames,
                        "stdout",
                    ),
                ]
            if audio_policy.pcm_match_required:
                commands[2:2] = [
                    (
                        pcm_hash_command(
                            paths.reference,
                            source_audio_ordinal,
                            input_codec=source_input_codec,
                        ),
                        source_pcm,
                        "stdout",
                    ),
                    (
                        pcm_hash_command(
                            output,
                            final_audio_ordinal,
                            input_codec=final_input_codec,
                        ),
                        encode_pcm,
                        "stdout",
                    ),
                ]
            for command, result_path, result_mode in commands:
                command_inputs = {
                    "argv": command,
                    **track_command_inputs,
                }
                marker = paths.stages / f"qc-{result_path.stem}.json"
                if not _valid_stage(marker, command_inputs, [result_path]):
                    if result_mode == "stdout":
                        self._runner(paths).run(
                            command,
                            cwd=paths.work,
                            stdout_path=result_path,
                            stderr_path=paths.logs / f"{result_path.stem}.stderr",
                        )
                    elif result_mode == "stderr":
                        self._runner(paths).run(
                            command,
                            cwd=paths.work,
                            stderr_path=result_path,
                        )
                    else:
                        raise AssertionError(
                            f"unsupported QC result mode: {result_mode}"
                        )
                    _write_stage(marker, command_inputs, [result_path])
            try:
                source_value = parse_audio_probe(
                    source_probe.read_text(encoding="utf-8")
                )
                encode_value = parse_audio_probe(
                    encode_probe.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise ReviewRequired(
                    f"audio duration evidence is incomplete for {stream.id}"
                ) from exc
            source_frame_value = None
            sidecar_frame_value = None
            final_frame_value = None
            sidecar_continuity = None
            final_continuity = None
            if continuity_required:
                try:
                    source_frame_value = parse_audio_frame_continuity(
                        source_frames.read_text(encoding="utf-8")
                    )
                    sidecar_frame_value = parse_audio_frame_continuity(
                        sidecar_frames.read_text(encoding="utf-8")
                    )
                    final_frame_value = parse_audio_frame_continuity(
                        final_frames.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as exc:
                    raise ReviewRequired(
                        f"decoded audio frame evidence is incomplete for {stream.id}"
                    ) from exc
                sidecar_continuity = compare_audio_frame_continuity(
                    source_frame_value, sidecar_frame_value, audio_policy
                )
                final_continuity = compare_audio_frame_continuity(
                    source_frame_value, final_frame_value, audio_policy
                )
                if not sidecar_continuity.passed or not final_continuity.passed:
                    raise ReviewRequired(
                        "decoded audio sample-cursor continuity failed for "
                        f"{stream.id}",
                        details={
                            "action": item.action.value,
                            "source": source_frame_value.to_dict(),
                            "sidecar": sidecar_frame_value.to_dict(),
                            "final": final_frame_value.to_dict(),
                            "source_to_sidecar": sidecar_continuity.to_dict(),
                            "source_to_final": final_continuity.to_dict(),
                        },
                    )
            # Compare timing on the same rebased timeline used by the mux.  A
            # source track that legitimately started at +40 ms and was moved to
            # zero must not be mistaken for a -40 ms encode delay.
            source_timeline_value = replace(
                source_value,
                start_time=timeline_expected_starts[1 + final_audio_ordinal],
            )
            if source_value.duration is None or encode_value.duration is None:
                raise ReviewRequired(
                    f"audio duration evidence is incomplete for {stream.id}"
                )
            measured_durations = (source_value.duration, encode_value.duration)
            spectrum_duration = max(measured_durations)
            spectrum_windows = plan_spectrum_windows(spectrum_duration)
            spectrum_work = _ensure_contained_directory(
                paths.work / "spectrum" / f"audio-{number:02d}",
                root=paths.work,
                description="audio spectrum workspace directory",
            )

            for media_path, ordinal, input_codec, final_spectrum in (
                (
                    paths.reference,
                    source_audio_ordinal,
                    source_input_codec,
                    source_spectrum,
                ),
                (output, final_audio_ordinal, final_input_codec, encode_spectrum),
            ):
                window_outputs: list[Path] = []
                for window in spectrum_windows:
                    window_output = spectrum_work / (
                        f"{final_spectrum.stem}-window-{window.index + 1:03d}.png"
                    )
                    window_outputs.append(window_output)
                    command = spectrum_command(
                        media_path,
                        ordinal,
                        window_output,
                        start_seconds=window.start_seconds,
                        duration_seconds=window.duration_seconds,
                        height=window.height,
                        input_codec=input_codec,
                    )
                    command_inputs = {"argv": command, **track_command_inputs}
                    marker = paths.stages / f"qc-{window_output.stem}.json"
                    if not _valid_stage(marker, command_inputs, [window_output]):
                        self._runner(paths).run(
                            command,
                            cwd=paths.work,
                            stderr_path=paths.logs / f"{window_output.stem}.stderr",
                        )
                        inspect_png(window_output, require_high_bit_depth=True)
                        _write_stage(marker, command_inputs, [window_output])
                    else:
                        inspect_png(window_output, require_high_bit_depth=True)

                stitch_command = spectrum_stitch_command(
                    tuple(window_outputs), final_spectrum
                )
                stitch_inputs = {
                    "argv": stitch_command,
                    **track_command_inputs,
                    "window_sha256": [sha256_file(path) for path in window_outputs],
                }
                marker = paths.stages / f"qc-{final_spectrum.stem}.json"
                if not _valid_stage(marker, stitch_inputs, [final_spectrum]):
                    self._runner(paths).run(
                        stitch_command,
                        cwd=paths.work,
                        stderr_path=paths.logs / f"{final_spectrum.stem}.stderr",
                    )
                    inspect_png(final_spectrum, require_high_bit_depth=True)
                    _write_stage(marker, stitch_inputs, [final_spectrum])
                else:
                    inspect_png(final_spectrum, require_high_bit_depth=True)

            comparison = compare_audio_probes(source_timeline_value, encode_value)
            pcm_match = (
                sha256_file(source_pcm) == sha256_file(encode_pcm)
                if audio_policy.pcm_match_required
                else None
            )
            verification = verify_audio_output(
                source_timeline_value,
                encode_value,
                audio_policy,
                decoded_pcm_sha256_match=pcm_match,
            )
            try:
                source_signal = parse_audio_analysis(
                    source_analysis.read_text(encoding="utf-8", errors="replace")
                )
                encode_signal = parse_audio_analysis(
                    encode_analysis.read_text(encoding="utf-8", errors="replace")
                )
            except (OSError, ValueError) as exc:
                raise ReviewRequired(
                    f"audio signal evidence is incomplete for {stream.id}"
                ) from exc
            signal_verification = verify_audio_signal(
                source_signal, encode_signal, audio_policy
            )
            one_sample_tolerance = Decimal(1) / Decimal(source_value.sample_rate)
            delay_within_one_sample = (
                abs(comparison.delay_seconds) <= one_sample_tolerance
            )
            if not verification.passed:
                raise ReviewRequired(
                    f"final audio verification failed for {stream.id}",
                    details={
                        "action": item.action.value,
                        "comparison": comparison.to_dict(),
                        "effective_target": audio_policy.to_dict(),
                        "verification": verification.to_dict(),
                    },
                )
            if not signal_verification.passed:
                raise ReviewRequired(
                    f"audio signal safety verification failed for {stream.id}",
                    details={
                        "action": item.action.value,
                        "source_signal": source_signal.to_dict(),
                        "encode_signal": encode_signal.to_dict(),
                        "signal_verification": signal_verification.to_dict(),
                    },
                )
            source_probe_value = asdict(source_value)
            source_timeline_probe_value = asdict(source_timeline_value)
            encode_probe_value = asdict(encode_value)
            for value in (
                source_probe_value,
                source_timeline_probe_value,
                encode_probe_value,
            ):
                value["start_time"] = str(value["start_time"])
                if value["duration"] is not None:
                    value["duration"] = str(value["duration"])
            audio_results.append(
                {
                    "stream_id": stream.id,
                    "action": item.action.value,
                    "source_probe": source_probe_value,
                    "source_timeline_probe": source_timeline_probe_value,
                    "encode_probe": encode_probe_value,
                    "comparison": comparison.to_dict(),
                    "verification_mode": audio_policy.verification_mode,
                    "effective_target": audio_policy.to_dict(),
                    "verification": verification.to_dict(),
                    "source_signal": source_signal.to_dict(),
                    "encode_signal": encode_signal.to_dict(),
                    "signal_verification": signal_verification.to_dict(),
                    "decoded_pcm_sha256_match": pcm_match,
                    "decoded_pcm_sha256_required": audio_policy.pcm_match_required,
                    "decoded_frame_continuity_required": continuity_required,
                    "source_frame_continuity": (
                        source_frame_value.to_dict()
                        if source_frame_value is not None
                        else None
                    ),
                    "sidecar_frame_continuity": (
                        sidecar_frame_value.to_dict()
                        if sidecar_frame_value is not None
                        else None
                    ),
                    "final_frame_continuity": (
                        final_frame_value.to_dict()
                        if final_frame_value is not None
                        else None
                    ),
                    "source_to_sidecar_continuity": (
                        sidecar_continuity.to_dict()
                        if sidecar_continuity is not None
                        else None
                    ),
                    "source_to_final_continuity": (
                        final_continuity.to_dict()
                        if final_continuity is not None
                        else None
                    ),
                    "delay_within_one_sample": delay_within_one_sample,
                    "timing_within_tolerance": verification.timing_within_tolerance,
                    "duration_within_tolerance": verification.duration_within_tolerance,
                    "delay_tolerance_seconds": str(
                        verification.timing_tolerance_seconds
                    ),
                    "source_pcm_sha256": (
                        sha256_file(source_pcm)
                        if audio_policy.pcm_match_required
                        else None
                    ),
                    "encode_pcm_sha256": (
                        sha256_file(encode_pcm)
                        if audio_policy.pcm_match_required
                        else None
                    ),
                    "source_spectrum": source_spectrum.name,
                    "encode_spectrum": encode_spectrum.name,
                    "source_spectrum_sha256": sha256_file(source_spectrum),
                    "encode_spectrum_sha256": sha256_file(encode_spectrum),
                    "spectrum_coverage_seconds": str(spectrum_duration),
                    "spectrum_window_count": len(spectrum_windows),
                    "spectrum_max_window_seconds": "300",
                }
            )
            track_outputs = [
                source_probe,
                encode_probe,
                source_analysis,
                encode_analysis,
                source_spectrum,
                encode_spectrum,
            ]
            if audio_policy.pcm_match_required:
                track_outputs.extend((source_pcm, encode_pcm))
            if continuity_required:
                track_outputs.extend((source_frames, sidecar_frames, final_frames))
            audio_outputs.extend(track_outputs)
            audio_inputs["tracks"].append(
                {
                    "stream": stream.id,
                    "action": item.action.value,
                    "sha256": intermediate_sha256,
                    "effective_audio_policy": audio_policy.to_dict(),
                    "audio_decode_policy": decode_policy,
                }
            )
        audio_marker = paths.stages / "qc-audio.json"
        if not _valid_stage(audio_marker, audio_inputs, audio_outputs):
            atomic_write_json(
                audio_manifest_path, {"schema_version": 3, "tracks": audio_results}
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
                job.id,
                report,
                kind,
                report.name,
                mime_type=(
                    "application/json" if report.suffix == ".json" else "text/plain"
                ),
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
                "-fps_mode",
                "passthrough",
                "-update",
                "1",
                "-c:v",
                "png",
                "-compression_level",
                "6",
                "-y",
                str(output),
            ],
        ]

    @staticmethod
    def _sample_metric_pipeline(
        reference_png: Path,
        encoded_png: Path,
        ssim_output: Path,
        psnr_output: Path,
    ) -> list[list[str]]:
        """Measure only one selected proof pair, never the complete title."""

        ssim_stats = str(ssim_output).replace("\\", "/").replace(":", "\\:")
        psnr_stats = str(psnr_output).replace("\\", "/").replace(":", "\\:")
        filter_value = (
            "[0:v]format=gbrp16le,split=2[r1][r2];"
            "[1:v]format=gbrp16le,split=2[e1][e2];"
            f"[e1][r1]ssim=stats_file={ssim_stats}[ssimout];"
            f"[e2][r2]psnr=stats_file={psnr_stats}[psnrout]"
        )
        return [
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-v",
                "info",
                "-i",
                str(reference_png),
                "-i",
                str(encoded_png),
                "-filter_complex",
                filter_value,
                "-map",
                "[ssimout]",
                "-map",
                "[psnrout]",
                "-f",
                "null",
                "-",
            ],
        ]

    @staticmethod
    def _reference_y4m_pipeline(
        script: Path, frame: int, output: Path
    ) -> list[list[str]]:
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
                "-frames:v",
                "1",
                "-f",
                "yuv4mpegpipe",
                "-y",
                str(output),
            ],
        ]

    @staticmethod
    def _metric_stat(path: Path, name: str) -> float | str | None:
        """Read one FFmpeg key without making metrics-log wording an invariant."""

        try:
            value = re.search(
                rf"(?:^|\s){re.escape(name)}:([^\s]+)",
                path.read_text(encoding="utf-8", errors="replace"),
            )
        except OSError:
            return None
        if value is None:
            return None
        raw = value.group(1)
        if raw.casefold() in {"inf", "+inf", "-inf", "nan"}:
            return raw
        try:
            return float(raw)
        except ValueError:
            return None

    def _comparison(self, job: Job, paths: JobPaths) -> None:
        scan, selection = self._load_scan_and_selection(job, paths)
        encoded_input = paths.muxed_output
        if not encoded_input.is_file():
            raise RuntimeError("final Matroska output is missing before comparison")

        # Twenty-four distributed native-YUV pairs are still bounded, but their
        # open-GOP-safe extraction is materially stronger than the former five
        # RGB screenshots and needs a realistic worker budget.
        comparison_deadline = time.monotonic() + 1800

        def remaining_timeout(per_command_limit: float) -> float:
            remaining = comparison_deadline - time.monotonic()
            if remaining <= 1:
                raise ReviewRequired(
                    "comparison exceeded its thirty-minute time budget"
                )
            return max(1.0, min(per_command_limit, remaining))

        script_sha256 = sha256_file(paths.script)
        reference_sha256 = _recorded_output_sha256(
            paths.stages / "reference-remux.json", paths.reference
        )
        if reference_sha256 is None:
            raise RuntimeError("reference remux checkpoint digest is missing")
        encoded_sha256 = _recorded_output_sha256(
            paths.stages / "mux.json", encoded_input
        )
        if encoded_sha256 is None:
            raise RuntimeError("final MKV mux checkpoint digest is missing")
        self.database.record_progress(
            job.id,
            0.925,
            message="fast comparison: preparing bounded samples",
            expected_state=JobState.COMPARISON,
            emit_event=False,
        )

        reference_info_path = paths.comparison / "reference-vapoursynth-info.txt"
        reference_info_inputs = {
            "policy_schema_version": 2,
            "scan_fingerprint": scan.fingerprint,
            "playlist_id": selection.playlist_id,
            "angle": selection.angle,
            "reference_sha256": reference_sha256,
            "script_sha256": script_sha256,
        }
        reference_info_marker = paths.stages / "comparison-reference-info.json"
        if not _valid_stage(
            reference_info_marker, reference_info_inputs, [reference_info_path]
        ):
            self._runner(paths).run(
                vspipe_info_command(paths.script),
                cwd=paths.work,
                stdout_path=reference_info_path,
                stderr_path=paths.logs / "comparison-reference-info.stderr",
                timeout=remaining_timeout(60),
            )
            _write_stage(
                reference_info_marker, reference_info_inputs, [reference_info_path]
            )
        reference_info = parse_vspipe_info(
            reference_info_path.read_text(encoding="utf-8", errors="replace")
        )

        playlist = scan.playlist(selection.playlist_id)
        encoded_packet_report = paths.analysis / "final-video-packet-sizes.csv"
        mux_integrity_report = paths.analysis / "video-mux-integrity.json"
        final_video_timeline_path = (
            paths.work / "video-mux-integrity" / "final-video-timeline.json"
        )
        completeness_report = paths.analysis / "video-frame-completeness.json"
        if not encoded_packet_report.is_file():
            raise ReviewRequired(
                "encoded video packet-count evidence is missing before comparison"
            )
        _assert_stage_outputs_current(
            paths.stages / "video-mux-integrity.json",
            [mux_integrity_report, final_video_timeline_path],
            stage_name="video mux integrity",
        )
        try:
            mux_integrity_document = json.loads(
                mux_integrity_report.read_text(encoding="utf-8")
            )
            final_timeline = parse_packet_timeline(
                final_video_timeline_path.read_text(encoding="utf-8")
            )
            if (
                mux_integrity_document.get("status") != "passed"
                or final_timeline.presentation_span_ms is None
                or final_timeline.presentation_span_ms <= 0
                or final_timeline.missing_pts_count != 0
                or final_timeline.missing_duration_count != 0
            ):
                raise ValueError("final video packet timeline is incomplete")
            final_video_duration = Decimal(
                final_timeline.presentation_span_ms
            ) / Decimal(1000)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReviewRequired(
                "final video packet-timeline duration evidence is invalid",
                details={"report": mux_integrity_report.name},
            ) from exc
        completeness_inputs = {
            "policy_schema_version": 3,
            "final_mkv_sha256": encoded_sha256,
            "reference_info_sha256": sha256_file(reference_info_path),
            "encoded_packet_report_sha256": sha256_file(encoded_packet_report),
            "mux_integrity_report_sha256": sha256_file(mux_integrity_report),
            "final_video_timeline_sha256": sha256_file(final_video_timeline_path),
            "title_duration_seconds": str(playlist.duration_seconds),
            "final_video_duration_seconds": str(final_video_duration),
            "tolerance_frames": 2,
        }
        completeness_marker = paths.stages / "video-frame-completeness.json"
        if not _valid_stage(
            completeness_marker,
            completeness_inputs,
            [completeness_report],
        ):
            try:
                encoded_packet_summary = parse_video_packet_sizes(
                    encoded_packet_report.read_text(encoding="utf-8")
                )
                cadence_verdict = require_video_cadence(
                    final_timeline,
                    reference_info.frames,
                    fps_numerator=reference_info.fps_numerator,
                    fps_denominator=reference_info.fps_denominator,
                )
                completeness_verdict = require_video_completeness(
                    reference_info.frames,
                    encoded_packet_summary.packet_count,
                    fps_numerator=reference_info.fps_numerator,
                    fps_denominator=reference_info.fps_denominator,
                    title_duration_seconds=playlist.duration_seconds,
                    final_video_duration_seconds=final_video_duration,
                    tolerance_frames=2,
                )
            except (OSError, ValueError) as exc:
                error = str(exc)
                atomic_write_json(
                    completeness_report,
                    {
                        "schema_version": 1,
                        "status": "needs_review",
                        "error": error,
                    },
                )
                raise ReviewRequired(
                    "video frame-count or duration completeness requires review",
                    details={
                        "error": error,
                        "report": completeness_report.name,
                    },
                ) from exc
            atomic_write_json(
                completeness_report,
                {
                    "schema_version": 1,
                    "status": "passed",
                    "encoded_packet_summary": encoded_packet_summary.to_dict(),
                    "cadence_verdict": cadence_verdict.to_dict(),
                    "verdict": completeness_verdict.to_dict(),
                },
            )
            _write_stage(
                completeness_marker,
                completeness_inputs,
                [completeness_report],
            )
        try:
            completeness_document = json.loads(
                completeness_report.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ReviewRequired(
                "video completeness report is missing or invalid"
            ) from exc

        intervals = plan_sample_intervals(reference_info)
        interval_manifest = [
            {
                "start_seconds": str(item.start_seconds),
                "duration_seconds": str(item.duration_seconds),
            }
            for item in intervals
        ]
        sample_plan = {
            "schema_version": 2,
            "strategy": "bounded_distributed_read_intervals",
            "intervals": interval_manifest,
            "planned_interval_seconds_per_input": str(
                sum((item.duration_seconds for item in intervals), Decimal(0))
            ),
            "full_title_scan": False,
        }

        encoded_origin_path = paths.comparison / "sampled-encoded-origin.json"
        encoded_origin_inputs = {"final_mkv_sha256": encoded_sha256}
        encoded_origin_marker = (
            paths.stages / "comparison-sampled-encoded-origin-v2.json"
        )
        if not _valid_stage(
            encoded_origin_marker, encoded_origin_inputs, [encoded_origin_path]
        ):
            self._runner(paths).run(
                ffprobe_frame_origin_command(encoded_input),
                cwd=paths.work,
                stdout_path=encoded_origin_path,
                stderr_path=paths.logs / "comparison-sampled-encoded-origin.stderr",
                timeout=remaining_timeout(30),
            )
            _write_stage(
                encoded_origin_marker, encoded_origin_inputs, [encoded_origin_path]
            )
        try:
            encoded_pts_origin = parse_ffprobe_frame_origin(
                encoded_origin_path.read_text(encoding="utf-8")
            )
        except FrameSelectionError as exc:
            raise ReviewRequired(
                f"encoded comparison PTS origin is invalid: {exc}"
            ) from exc

        encoded_probe = paths.comparison / "sampled-encoded-frames.json"
        encoded_probe_inputs = {
            "final_mkv_sha256": encoded_sha256,
            "sample_plan": sample_plan,
            "pts_origin": str(encoded_pts_origin),
        }
        encoded_probe_marker = (
            paths.stages / "comparison-sampled-encoded-frame-probe-v2.json"
        )
        if not _valid_stage(
            encoded_probe_marker, encoded_probe_inputs, [encoded_probe]
        ):
            self._runner(paths).run(
                ffprobe_sampled_frame_command(
                    encoded_input, intervals, pts_origin=encoded_pts_origin
                ),
                cwd=paths.work,
                stdout_path=encoded_probe,
                stderr_path=paths.logs / "comparison-sampled-encoded-probe.stderr",
                timeout=remaining_timeout(90),
            )
            _write_stage(encoded_probe_marker, encoded_probe_inputs, [encoded_probe])
        try:
            encoded = parse_sampled_ffprobe_frames(
                encoded_probe.read_text(encoding="utf-8"),
                reference_info,
                pts_origin=encoded_pts_origin,
            )
        except FrameSelectionError as exc:
            raise ReviewRequired(
                f"encoded comparison sample is invalid: {exc}"
            ) from exc

        source_type_available = selection.temporal_filter is TemporalFilter.PROGRESSIVE
        # On an unchanged progressive timeline, the user's strict source/encode
        # I/P/B requirement is mandatory. A temporal transform has no meaningful
        # one-to-one source bitstream frame type, so it remains index/PTS aligned.
        require_source_type_match = source_type_available
        source_by_index: dict[int, FrameRecord] = {}
        source_pts_origin: Decimal | None = None
        if source_type_available:
            source_origin_path = paths.comparison / "sampled-source-origin.json"
            source_origin_inputs = {"reference_sha256": reference_sha256}
            source_origin_marker = (
                paths.stages / "comparison-sampled-source-origin-v2.json"
            )
            if not _valid_stage(
                source_origin_marker, source_origin_inputs, [source_origin_path]
            ):
                self._runner(paths).run(
                    ffprobe_frame_origin_command(paths.reference),
                    cwd=paths.work,
                    stdout_path=source_origin_path,
                    stderr_path=paths.logs / "comparison-sampled-source-origin.stderr",
                    timeout=remaining_timeout(30),
                )
                _write_stage(
                    source_origin_marker,
                    source_origin_inputs,
                    [source_origin_path],
                )
            try:
                source_pts_origin = parse_ffprobe_frame_origin(
                    source_origin_path.read_text(encoding="utf-8")
                )
            except FrameSelectionError as exc:
                raise ReviewRequired(
                    f"source comparison PTS origin is invalid: {exc}"
                ) from exc
            source_probe = paths.comparison / "sampled-source-frames.json"
            source_probe_inputs = {
                "reference_sha256": reference_sha256,
                "sample_plan": sample_plan,
                "pts_origin": str(source_pts_origin),
            }
            source_probe_marker = (
                paths.stages / "comparison-sampled-source-frame-probe-v2.json"
            )
            if not _valid_stage(
                source_probe_marker, source_probe_inputs, [source_probe]
            ):
                self._runner(paths).run(
                    ffprobe_sampled_frame_command(
                        paths.reference, intervals, pts_origin=source_pts_origin
                    ),
                    cwd=paths.work,
                    stdout_path=source_probe,
                    stderr_path=paths.logs / "comparison-sampled-source-probe.stderr",
                    timeout=remaining_timeout(90),
                )
                _write_stage(source_probe_marker, source_probe_inputs, [source_probe])
            try:
                source_frames = parse_sampled_ffprobe_frames(
                    source_probe.read_text(encoding="utf-8"),
                    reference_info,
                    pts_origin=source_pts_origin,
                )
            except FrameSelectionError as exc:
                raise ReviewRequired(
                    f"source comparison sample is invalid: {exc}"
                ) from exc
            source_by_index = {item.presentation_index: item for item in source_frames}
            reference = source_frames
        else:
            reference = [
                FrameRecord(
                    presentation_index=item.presentation_index,
                    pts_seconds=item.pts_seconds,
                    pict_type=None,
                )
                for item in encoded
            ]

        try:
            pairs = select_frame_pairs(
                encoded,
                reference,
                total_pairs=self.settings.comparison_pair_count,
                timeline_frames=reference_info.frames,
                dual_type_match=require_source_type_match,
            )
        except FrameSelectionError as exc:
            raise ReviewRequired(
                "bounded comparison sampling could not find the required "
                f"same-frame I/P/B pairs: {exc}",
                details={
                    "sample_plan": sample_plan,
                    "requested_pairs": self.settings.comparison_pair_count,
                },
            ) from exc

        self.database.record_progress(
            job.id,
            0.94,
            message=f"fast comparison: {len(pairs)} I/P/B pairs selected",
            expected_state=JobState.COMPARISON,
            emit_event=True,
        )

        video = playlist.video_streams[0].video
        assert video is not None
        hdr = selection.settings.hdr10.enabled
        comparison_color = selection.settings.color
        clean_png_root = _ensure_contained_directory(
            paths.work / "comparison-metric-frames",
            root=paths.work,
            description="comparison metric-frame directory",
        )
        native_yuv_root = _ensure_contained_directory(
            paths.work / "comparison-native-yuv",
            root=paths.work,
            description="comparison native-YUV directory",
        )
        pngs: list[Path] = []
        metric_sidecars: list[Path] = []
        metric_samples: list[dict[str, Any]] = []
        manifest = comparison_manifest(pairs)
        manifest["schema_version"] = 4
        manifest["reference_alignment"] = "same_vapoursynth_output_frame_index"
        manifest["visual_annotation"] = {
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
        manifest["sampling"] = {
            **sample_plan,
            "requested_pair_count": self.settings.comparison_pair_count,
            "selected_pair_count": len(pairs),
            "encoded_pts_origin": str(encoded_pts_origin),
            "source_pts_origin": (
                str(source_pts_origin) if source_pts_origin is not None else None
            ),
        }
        manifest["frame_completeness"] = completeness_document
        manifest["distorted_input"] = {
            "role": "final_matroska_video_track_0",
            "path": encoded_input.name,
            "sha256": encoded_sha256,
        }
        manifest["source_bitstream_type_available"] = source_type_available
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
            clean_reference_png = clean_png_root / f"{label}-reference-native.png"
            clean_encoded_png = clean_png_root / f"{label}-encode-native.png"
            encoded_record = next(
                item
                for item in encoded
                if item.presentation_index == pair.presentation_index
            )
            image_inputs = {
                "schema_version": 3,
                "script_sha256": script_sha256,
                "final_mkv_sha256": encoded_sha256,
                "frame": pair.presentation_index,
                "encoded_seek_pts_seconds": str(encoded_record.seek_pts_seconds),
                "extraction": "accurate_timestamp_seek",
                "hdr_native": True,
                "visual_annotation": {
                    "schema_version": 1,
                    "source_label": "SOURCE",
                    "encode_label": "ENCODE",
                    "frame_type": pair.category,
                    "source_frame_type_mode": (
                        "native_bitstream_type"
                        if pair.source_pict_type is not None
                        else "matched_to_encode_frame_type"
                    ),
                },
            }
            image_marker = paths.stages / f"comparison-{label}-native-v3.json"
            if not _valid_stage(
                image_marker,
                image_inputs,
                [
                    clean_reference_png,
                    clean_encoded_png,
                    reference_png,
                    encoded_png,
                ],
            ):
                self._runner(paths).run_pipeline(
                    self._reference_png_pipeline(
                        paths.script,
                        pair.presentation_index,
                        clean_reference_png,
                        hdr_native=True,
                        source_hdr10=hdr,
                        color=comparison_color,
                    ),
                    cwd=paths.work,
                    stderr_paths=[
                        paths.logs / f"{label}-reference-vs.log",
                        paths.logs / f"{label}-reference-png.log",
                    ],
                    timeout=remaining_timeout(90),
                )
                self._runner(paths).run(
                    extract_png_at_timestamp_command(
                        encoded_input,
                        encoded_record.seek_pts_seconds,
                        clean_encoded_png,
                        hdr_native=True,
                        source_hdr10=hdr,
                        color_primaries=comparison_color.primaries,
                        color_transfer=comparison_color.transfer,
                        color_matrix=comparison_color.matrix,
                        color_range=comparison_color.range,
                    ),
                    cwd=paths.work,
                    stderr_path=paths.logs / f"{label}-encode-png.log",
                    timeout=remaining_timeout(60),
                )
                self._runner(paths).run(
                    annotate_comparison_png_command(
                        clean_reference_png,
                        reference_png,
                        image_role="SOURCE",
                        presentation_index=pair.presentation_index,
                        pict_type=pair.source_pict_type or pair.category,
                        matched_to_type=pair.source_pict_type is None,
                    ),
                    cwd=paths.work,
                    stderr_path=paths.logs / f"{label}-reference-label.log",
                    timeout=remaining_timeout(30),
                )
                self._runner(paths).run(
                    annotate_comparison_png_command(
                        clean_encoded_png,
                        encoded_png,
                        image_role="ENCODE",
                        presentation_index=pair.presentation_index,
                        pict_type=pair.category,
                    ),
                    cwd=paths.work,
                    stderr_path=paths.logs / f"{label}-encode-label.log",
                    timeout=remaining_timeout(30),
                )
                _write_stage(
                    image_marker,
                    image_inputs,
                    [
                        clean_reference_png,
                        clean_encoded_png,
                        reference_png,
                        encoded_png,
                    ],
                )
            inspect_png(clean_reference_png, require_high_bit_depth=hdr)
            inspect_png(clean_encoded_png, require_high_bit_depth=hdr)
            inspect_png(reference_png, require_high_bit_depth=hdr)
            inspect_png(encoded_png, require_high_bit_depth=hdr)
            pngs.extend((reference_png, encoded_png))
            pair_value = manifest["pairs"][number - 1]
            pair_value["reference_png"] = reference_png.name
            pair_value["encode_png"] = encoded_png.name
            pair_value["visual_label"] = {
                "reference_role": "SOURCE",
                "encode_role": "ENCODE",
                "frame_index": pair.presentation_index,
                "frame_index_base": 0,
                "frame_type": pair.category,
                "source_frame_type_mode": (
                    "native_bitstream_type"
                    if pair.source_pict_type is not None
                    else "matched_to_encode_frame_type"
                ),
            }
            pair_value["encoded_seek_pts_seconds"] = str(
                encoded_record.seek_pts_seconds
            )
            if pair.presentation_index in source_by_index:
                pair_value["source_container_pts_seconds"] = str(
                    source_by_index[pair.presentation_index].seek_pts_seconds
                )
            pair_value["reference_sha256"] = sha256_file(reference_png)
            pair_value["encode_sha256"] = sha256_file(encoded_png)
            if hdr:
                reference_sdr = paths.comparison / f"{label}-reference-sdr.png"
                encoded_sdr = paths.comparison / f"{label}-encode-sdr.png"
                clean_reference_sdr = clean_png_root / f"{label}-reference-sdr.png"
                clean_encoded_sdr = clean_png_root / f"{label}-encode-sdr.png"
                sdr_inputs = dict(image_inputs, hdr_native=False)
                sdr_marker = paths.stages / f"comparison-{label}-sdr-v3.json"
                if not _valid_stage(
                    sdr_marker,
                    sdr_inputs,
                    [
                        clean_reference_sdr,
                        clean_encoded_sdr,
                        reference_sdr,
                        encoded_sdr,
                    ],
                ):
                    self._runner(paths).run_pipeline(
                        self._reference_png_pipeline(
                            paths.script,
                            pair.presentation_index,
                            clean_reference_sdr,
                            hdr_native=False,
                            source_hdr10=True,
                            color=comparison_color,
                        ),
                        cwd=paths.work,
                        stderr_paths=[
                            paths.logs / f"{label}-reference-sdr-vs.log",
                            paths.logs / f"{label}-reference-sdr-png.log",
                        ],
                        timeout=remaining_timeout(90),
                    )
                    self._runner(paths).run(
                        extract_png_at_timestamp_command(
                            encoded_input,
                            encoded_record.seek_pts_seconds,
                            clean_encoded_sdr,
                            hdr_native=False,
                            source_hdr10=True,
                            color_primaries=comparison_color.primaries,
                            color_transfer=comparison_color.transfer,
                            color_matrix=comparison_color.matrix,
                            color_range=comparison_color.range,
                        ),
                        cwd=paths.work,
                        stderr_path=paths.logs / f"{label}-encode-sdr-png.log",
                        timeout=remaining_timeout(60),
                    )
                    self._runner(paths).run(
                        annotate_comparison_png_command(
                            clean_reference_sdr,
                            reference_sdr,
                            image_role="SOURCE",
                            presentation_index=pair.presentation_index,
                            pict_type=pair.source_pict_type or pair.category,
                            matched_to_type=pair.source_pict_type is None,
                        ),
                        cwd=paths.work,
                        stderr_path=paths.logs / f"{label}-reference-sdr-label.log",
                        timeout=remaining_timeout(30),
                    )
                    self._runner(paths).run(
                        annotate_comparison_png_command(
                            clean_encoded_sdr,
                            encoded_sdr,
                            image_role="ENCODE",
                            presentation_index=pair.presentation_index,
                            pict_type=pair.category,
                        ),
                        cwd=paths.work,
                        stderr_path=paths.logs / f"{label}-encode-sdr-label.log",
                        timeout=remaining_timeout(30),
                    )
                    _write_stage(
                        sdr_marker,
                        sdr_inputs,
                        [
                            clean_reference_sdr,
                            clean_encoded_sdr,
                            reference_sdr,
                            encoded_sdr,
                        ],
                    )
                inspect_png(clean_reference_sdr)
                inspect_png(clean_encoded_sdr)
                inspect_png(reference_sdr)
                inspect_png(encoded_sdr)
                pngs.extend((reference_sdr, encoded_sdr))
                pair_value["reference_sdr_png"] = reference_sdr.name
                pair_value["encode_sdr_png"] = encoded_sdr.name
                pair_value["reference_sdr_sha256"] = sha256_file(reference_sdr)
                pair_value["encode_sdr_sha256"] = sha256_file(encoded_sdr)

            reference_y4m = native_yuv_root / f"{label}-reference.y4m"
            encoded_y4m = native_yuv_root / f"{label}-encode.y4m"
            yuv_inputs = {
                "schema_version": 1,
                "script_sha256": script_sha256,
                "final_mkv_sha256": encoded_sha256,
                "presentation_index": pair.presentation_index,
                "encoded_seek_pts_seconds": str(encoded_record.seek_pts_seconds),
                "pixel_format": selection.settings.pixel_format,
            }
            yuv_marker = paths.stages / f"comparison-{label}-native-yuv.json"
            if not _valid_stage(yuv_marker, yuv_inputs, [reference_y4m, encoded_y4m]):
                self._runner(paths).run_pipeline(
                    self._reference_y4m_pipeline(
                        paths.script, pair.presentation_index, reference_y4m
                    ),
                    cwd=paths.work,
                    stderr_paths=[
                        paths.logs / f"{label}-reference-yuv-vs.log",
                        paths.logs / f"{label}-reference-yuv.log",
                    ],
                    timeout=remaining_timeout(120),
                )
                self._runner(paths).run(
                    extract_y4m_at_timestamp_command(
                        encoded_input,
                        encoded_record.seek_pts_seconds,
                        encoded_y4m,
                    ),
                    cwd=paths.work,
                    stderr_path=paths.logs / f"{label}-encode-yuv.log",
                    timeout=remaining_timeout(90),
                )
                _write_stage(yuv_marker, yuv_inputs, [reference_y4m, encoded_y4m])
            ssim_stats = paths.comparison / f"{label}.ssim.log"
            psnr_stats = paths.comparison / f"{label}.psnr.log"
            sample_metric_inputs = {
                "schema_version": 4,
                "reference_y4m_sha256": sha256_file(reference_y4m),
                "encode_y4m_sha256": sha256_file(encoded_y4m),
                "scope": "single_selected_native_yuv_plane_pair",
                "pixel_format": selection.settings.pixel_format,
            }
            sample_metric_marker = paths.stages / f"comparison-{label}-metrics-v4.json"
            if not _valid_stage(
                sample_metric_marker,
                sample_metric_inputs,
                [ssim_stats, psnr_stats],
            ):
                self._runner(paths).run(
                    native_yuv_metric_command(
                        reference_y4m,
                        encoded_y4m,
                        ssim_stats,
                        psnr_stats,
                        pixel_format=selection.settings.pixel_format,
                    ),
                    cwd=paths.work,
                    stderr_path=paths.logs / f"{label}-metrics.log",
                    timeout=remaining_timeout(60),
                )
                if not ssim_stats.is_file() or not psnr_stats.is_file():
                    raise RuntimeError(
                        f"native-YUV SSIM/PSNR output is incomplete for {label}"
                    )
                _write_stage(
                    sample_metric_marker,
                    sample_metric_inputs,
                    [ssim_stats, psnr_stats],
                )
            metric_sidecars.extend((ssim_stats, psnr_stats))
            try:
                ssim_values = parse_ffmpeg_metric_stats(
                    ssim_stats.read_text(encoding="utf-8", errors="replace")
                )
                psnr_values = parse_ffmpeg_metric_stats(
                    psnr_stats.read_text(encoding="utf-8", errors="replace")
                )
            except (OSError, ValueError) as exc:
                raise ReviewRequired(
                    f"native-YUV metrics are invalid for {label}: {exc}"
                ) from exc
            metric_samples.append(
                {
                    "category": pair.category,
                    "presentation_index": pair.presentation_index,
                    "reference_png": reference_png.name,
                    "encode_png": encoded_png.name,
                    "measurement_input": "native_yuv_planes_before_rgb_conversion",
                    "reference_measurement_sha256": sha256_file(reference_y4m),
                    "encode_measurement_sha256": sha256_file(encoded_y4m),
                    "ssim_all": ssim_values.get("All"),
                    "ssim_planes": {
                        "y": ssim_values.get("Y"),
                        "u": ssim_values.get("U"),
                        "v": ssim_values.get("V"),
                    },
                    "psnr_average_db": psnr_values.get("psnr_avg"),
                    "psnr_planes_db": {
                        "y": psnr_values.get("psnr_y"),
                        "u": psnr_values.get("psnr_u"),
                        "v": psnr_values.get("psnr_v"),
                    },
                    "ssim_stats": {
                        "path": ssim_stats.name,
                        "sha256": sha256_file(ssim_stats),
                    },
                    "psnr_stats": {
                        "path": psnr_stats.name,
                        "sha256": sha256_file(psnr_stats),
                    },
                }
            )
            self.database.record_progress(
                job.id,
                0.94 + (0.035 * number / len(pairs)),
                message=f"fast comparison: pair {number}/{len(pairs)} complete",
                expected_state=JobState.COMPARISON,
                emit_event=number == len(pairs),
            )

        # Include Python-side validation, hashing and manifest finalization in
        # the same operator-visible wall-clock budget as the subprocesses.
        remaining_timeout(300)
        finite_ssim = [
            item["ssim_all"]
            for item in metric_samples
            if isinstance(item["ssim_all"], float)
        ]
        finite_psnr = [
            item["psnr_average_db"]
            for item in metric_samples
            if isinstance(item["psnr_average_db"], float)
        ]
        metrics = paths.comparison / "video-metrics.json"
        metric_document = {
            "schema_version": 4,
            "backend": "ffmpeg-native-yuv-sampled-ssim-psnr",
            "scope": "selected_ipb_native_yuv_plane_pairs",
            "full_title_measurement": False,
            "sample_count": len(metric_samples),
            "aggregate": {
                "ssim_all_mean": (
                    sum(finite_ssim) / len(finite_ssim) if finite_ssim else None
                ),
                "psnr_average_db_mean": (
                    sum(finite_psnr) / len(finite_psnr) if finite_psnr else None
                ),
            },
            "samples": metric_samples,
        }
        metric_errors = _sampled_video_metric_errors(metric_samples)
        metric_document["quality_gate"] = {
            "status": "needs_review" if metric_errors else "passed",
            "errors": list(metric_errors),
            "thresholds": {
                "minimum_sample_ssim": 0.93,
                "minimum_mean_ssim": 0.95,
                "minimum_sample_psnr_db": 35,
                "minimum_mean_psnr_db": 38,
                "maximum_b_minus_p_ssim_deficit": 0.03,
            },
        }
        atomic_write_json(metrics, metric_document)
        if metric_errors:
            raise ReviewRequired(
                "sampled native-YUV video metrics require review",
                details={"errors": list(metric_errors), "report": metrics.name},
            )
        _write_stage(
            paths.stages / "comparison-sampled-metrics-v4.json",
            {
                "pairs": [item.to_dict() for item in pairs],
                "backend": metric_document["backend"],
            },
            [metrics, *metric_sidecars],
        )
        manifest["metrics"] = {
            "path": metrics.name,
            "sha256": sha256_file(metrics),
            "backend": metric_document["backend"],
            "scope": metric_document["scope"],
            "sample_count": metric_document["sample_count"],
            "full_title_measurement": metric_document["full_title_measurement"],
            "aggregate": metric_document["aggregate"],
        }
        manifest_path = paths.comparison / "video-comparison.json"
        atomic_write_json(manifest_path, manifest)
        _current_comparison_pngs(paths, prune=True)
        allowed_metric_names = {path.name for path in metric_sidecars}
        for stale_metric in (
            *paths.comparison.glob("*.ssim.log"),
            *paths.comparison.glob("*.psnr.log"),
        ):
            if stale_metric.name not in allowed_metric_names:
                stale_metric.unlink(missing_ok=True)
        comparison_outputs = [manifest_path, metrics, *metric_sidecars, *pngs]
        _write_stage(
            paths.stages / "comparison.json",
            {
                "pairs": [item.to_dict() for item in pairs],
                "pair_count": self.settings.comparison_pair_count,
                "sampling_schema_version": 2,
                "visual_annotation_schema_version": 1,
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
        self._register_artifact(
            job.id,
            completeness_report,
            DatabaseArtifactKind.REPORT,
            completeness_report.name,
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

        # Scratch from the former full-title implementation must never be
        # copied into the completed comparison sidecars. New bounded probe JSON
        # is also disposable once the durable manifest/checkpoint exists.
        for scratch in (
            paths.comparison / "encoded-frames.json",
            paths.comparison / "source-frames.json",
            encoded_origin_path,
            encoded_probe,
            paths.comparison / "sampled-source-origin.json",
            paths.comparison / "sampled-source-frames.json",
        ):
            scratch.unlink(missing_ok=True)
        legacy_stage_markers = [
            paths.stages / "comparison-frame-probe.json",
            paths.stages / "comparison-source-frame-probe.json",
            paths.stages / "comparison-metrics.json",
            *paths.stages.glob("comparison-*-native.json"),
            *paths.stages.glob("comparison-*-sdr.json"),
            *paths.stages.glob("comparison-*-native-v2.json"),
            *paths.stages.glob("comparison-*-sdr-v2.json"),
            *paths.stages.glob("comparison-*-metrics-v2.json"),
        ]
        for legacy_marker in legacy_stage_markers:
            legacy_marker.unlink(missing_ok=True)
        remaining_timeout(300)
        self.queue.advance(
            job.id,
            JobState.UPLOADING,
            message=f"{len(pairs)} sampled I/P/B comparison pairs complete",
        )

    def _upload_and_finalize(self, job: Job, paths: JobPaths) -> None:
        _scan, selection = self._load_scan_and_selection(job, paths)
        recorded_mux_digest = _recorded_output_sha256(
            paths.stages / "mux.json", paths.muxed_output
        )
        try:
            current_mux_digest = sha256_file(paths.muxed_output)
        except OSError as exc:
            raise ReviewRequired(
                "final Matroska is missing after its validated mux checkpoint"
            ) from exc
        if recorded_mux_digest is None or current_mux_digest != recorded_mux_digest:
            raise ReviewRequired(
                "final Matroska changed after its validated mux checkpoint",
                details={"action": "rerun_mux_and_qc"},
            )
        mux_digest = current_mux_digest
        video_manifest_path = paths.comparison / "video-comparison.json"
        try:
            video_manifest = json.loads(video_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "video comparison manifest is missing or invalid"
            ) from exc
        if not isinstance(
            video_manifest, Mapping
        ) or not _has_current_visual_annotations(video_manifest):
            raise ReviewRequired(
                "comparison images predate the required visible SOURCE/ENCODE, "
                "0-based frame index and I/P/B labels; reapprove the selection "
                "to rebuild the comparison before upload",
                details={
                    "action": "reapprove_selection",
                    "required_video_comparison_schema": 4,
                    "required_visual_annotation_schema": 1,
                },
            )
        pngs = _current_comparison_pngs(paths, prune=False)
        metric_sidecars = _current_video_metric_sidecars(paths)
        video_evidence_pins = _assert_stage_outputs_current(
            paths.stages / "comparison.json",
            [
                video_manifest_path,
                *metric_sidecars,
                *(path for path in pngs if path.parent == paths.comparison),
            ],
            stage_name="video comparison",
        )
        audio_evidence_pins = _assert_stage_outputs_current(
            paths.stages / "qc-audio.json",
            [
                paths.analysis / "audio-comparison.json",
                *(path for path in pngs if path.parent == paths.analysis),
            ],
            stage_name="audio QC",
        )
        validated_public_evidence = {
            **video_evidence_pins,
            **audio_evidence_pins,
        }
        if len(validated_public_evidence) != len(video_evidence_pins) + len(
            audio_evidence_pins
        ):
            raise ReviewRequired("public evidence stage outputs overlap unexpectedly")
        for public_metadata in (
            video_manifest_path,
            paths.analysis / "audio-comparison.json",
            *metric_sidecars,
        ):
            _assert_public_sidecar_safe(public_metadata)
        allowed_names = {path.name for path in pngs}
        if len(allowed_names) != len(pngs):
            raise RuntimeError("comparison image basenames are not unique")
        checkpoint = paths.comparison / "uploads.json"
        checkpoint_document: dict[str, Any] = {}
        uploaded: dict[str, Any] = {}
        top_provider: str | None = None
        if checkpoint.is_file():
            try:
                loaded_checkpoint = json.loads(checkpoint.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ReviewRequired("upload checkpoint is invalid") from exc
            if not isinstance(loaded_checkpoint, dict):
                raise ReviewRequired("upload checkpoint must be a JSON object")
            checkpoint_document = loaded_checkpoint
            checkpoint_schema = checkpoint_document.get("schema_version")
            if checkpoint_schema == 2:
                required_checkpoint_fields = {"schema_version", "images"}
                allowed_checkpoint_fields = {
                    "schema_version",
                    "provider",
                    "provider_provisional",
                    "images",
                }
            elif checkpoint_schema == 1:
                skipped_checkpoint = checkpoint_document.get("skipped") is True
                allowed_checkpoint_fields = (
                    {"schema_version", "skipped", "reason", "images"}
                    if skipped_checkpoint
                    else {"schema_version", "images"}
                )
                required_checkpoint_fields = allowed_checkpoint_fields
                if skipped_checkpoint and selection.upload_images:
                    raise ReviewRequired(
                        "skipped upload checkpoint conflicts with current selection"
                    )
            else:
                raise ReviewRequired("upload checkpoint schema is unsupported")
            checkpoint_fields = set(checkpoint_document)
            if not required_checkpoint_fields.issubset(
                checkpoint_fields
            ) or not checkpoint_fields.issubset(allowed_checkpoint_fields):
                raise ReviewRequired(
                    "upload checkpoint contains unknown or missing fields"
                )
            raw_top_provider = checkpoint_document.get("provider")
            if raw_top_provider is not None:
                if not isinstance(raw_top_provider, str):
                    raise ReviewRequired("upload checkpoint provider is invalid")
                top_provider = raw_top_provider.strip().lower()
                if top_provider not in IMAGE_UPLOAD_PROVIDERS:
                    raise ReviewRequired("upload checkpoint provider is invalid")
            if (
                "provider_provisional" in checkpoint_document
                and checkpoint_document["provider_provisional"] is not True
            ):
                raise ReviewRequired(
                    "upload checkpoint provisional-provider flag is invalid"
                )
            if (
                checkpoint_document.get("provider_provisional") is True
                and top_provider is None
            ):
                raise ReviewRequired(
                    "upload checkpoint provisional provider is missing"
                )
            raw_uploaded = checkpoint_document.get("images", {})
            if not isinstance(raw_uploaded, dict) or not set(raw_uploaded).issubset(
                allowed_names
            ):
                raise ReviewRequired(
                    "upload checkpoint image names do not match current evidence"
                )
            for png in pngs:
                if png.name not in raw_uploaded:
                    continue
                try:
                    parsed_upload = parse_uploaded_image_checkpoint(
                        raw_uploaded[png.name],
                        expected_local_sha256=sha256_file(png),
                        legacy_provider=(
                            top_provider if checkpoint_schema == 2 else None
                        ),
                        infer_legacy_provider=checkpoint_schema == 1,
                    )
                except ValueError as exc:
                    raise ReviewRequired(
                        f"upload checkpoint entry {png.name} is unsafe: {exc}"
                    ) from exc
                uploaded[png.name] = asdict(parsed_upload)
        # Any schema-v2 provider loaded at stage entry is a durable lock.  This
        # includes a provisional pin left by a process interruption after the
        # network call began.  The current invocation may advance its own
        # provisional candidate after a classified-safe failure, but a later
        # invocation must conservatively stay on the persisted provider.
        provider_lock: str | None = (
            top_provider
            if checkpoint_document.get("schema_version") == 2 and top_provider
            else None
        )
        if uploaded:
            item_providers = {
                str(item.get("provider", "")).strip().lower()
                for item in uploaded.values()
                if str(item.get("provider", "")).strip()
            }
            if len(item_providers) > 1 or (
                top_provider and item_providers and top_provider not in item_providers
            ):
                raise ReviewRequired("upload checkpoint mixes image providers")
            # Schema v1 checkpoints predate provider metadata and can only
            # contain ImgBB results.
            provider_lock = (
                provider_lock or top_provider or next(iter(item_providers), "imgbb")
            )
            for item in uploaded.values():
                item.setdefault("provider", provider_lock)

        if selection.upload_images:
            try:
                pending = [png for png in pngs if png.name not in uploaded]
                if pending:
                    clients = self._image_upload_clients()
                    by_name = {
                        str(client.provider_name).strip().lower(): client
                        for client in clients
                    }
                    if provider_lock:
                        if (
                            selection.image_upload_provider != "auto"
                            and selection.image_upload_provider != provider_lock
                        ):
                            raise ReviewRequired(
                                "selected image provider conflicts with upload checkpoint"
                            )
                        client = by_name.get(provider_lock)
                        if client is None:
                            raise ImageUploadError(
                                f"checkpoint provider is unavailable: {provider_lock}"
                            )
                        candidates = [client]
                    elif selection.image_upload_provider != "auto":
                        client = by_name.get(selection.image_upload_provider)
                        if client is None:
                            raise ImageUploadError(
                                "selected image upload provider is unavailable",
                                provider=selection.image_upload_provider,
                            )
                        candidates = [client]
                    else:
                        candidates = clients

                    fallback_failures: list[str] = []
                    finished = False
                    for client in candidates:
                        provider_name = str(client.provider_name).strip().lower()
                        if provider_lock is None:
                            # Persist the candidate before entering adapter code:
                            # the process may die after POST dispatch without an
                            # exception ever reaching this worker.
                            atomic_write_json(
                                checkpoint,
                                {
                                    "schema_version": 2,
                                    "provider": provider_name,
                                    "provider_provisional": True,
                                    "images": uploaded,
                                },
                            )
                        try:
                            for png in pending:
                                # Never begin a new remote effect after a
                                # durable operator request.  A request arriving
                                # inside upload_png is acknowledged only after
                                # its result/checkpoint is known.
                                self._stop_at_operator_boundary(job.id)
                                result = client.upload_png(png)
                                if result.provider != provider_name:
                                    raise ImageUploadError(
                                        "image uploader returned the wrong provider",
                                        provider=provider_name,
                                        provider_may_have_committed=True,
                                    )
                                provider_lock = provider_name
                                try:
                                    validated_result = parse_uploaded_image_checkpoint(
                                        asdict(result),
                                        expected_local_sha256=sha256_file(png),
                                        legacy_provider=provider_name,
                                    )
                                except ValueError as exc:
                                    raise ImageUploadError(
                                        "image uploader returned unsafe checkpoint data",
                                        provider=provider_name,
                                        provider_may_have_committed=True,
                                    ) from exc
                                uploaded[png.name] = asdict(validated_result)
                                atomic_write_json(
                                    checkpoint,
                                    {
                                        "schema_version": 2,
                                        "provider": provider_lock,
                                        "images": uploaded,
                                    },
                                )
                                self._stop_at_operator_boundary(job.id)
                            finished = True
                            break
                        except ImageUploadError as exc:
                            if exc.provider_may_have_committed:
                                provider_lock = provider_name
                                atomic_write_json(
                                    checkpoint,
                                    {
                                        "schema_version": 2,
                                        "provider": provider_lock,
                                        "images": uploaded,
                                    },
                                )
                            # A provider is immutable after its first success.
                            # This preserves retry checkpoints and guarantees a
                            # single host for the complete BBCode package.
                            if provider_lock == provider_name or not exc.allow_fallback:
                                raise
                            fallback_failures.append(
                                f"{provider_name}: {sanitize_text(str(exc))[:400]}"
                            )
                            if selection.image_upload_provider != "auto":
                                # A manual provider has no next candidate.  Its
                                # explicitly safe failure must not leave a stale
                                # provisional lock behind.
                                atomic_write_json(
                                    checkpoint,
                                    {"schema_version": 2, "images": uploaded},
                                )
                                raise
                    if not finished:
                        # Every candidate failed before committing an image and
                        # explicitly allowed fallback.  Clear the current-run
                        # provisional pin so a later automatic retry may begin
                        # at the start of the configured chain.
                        atomic_write_json(
                            checkpoint,
                            {"schema_version": 2, "images": uploaded},
                        )
                        detail = "; ".join(fallback_failures)
                        raise ImageUploadError(
                            "all image upload providers are temporarily unavailable"
                            + (f": {detail}" if detail else "")
                        )

                if provider_lock is None:
                    raise ImageUploadError("image upload provider was not selected")
                # Persist a schema-v1 migration even when every image was
                # already present and no network request was necessary.
                atomic_write_json(
                    checkpoint,
                    {
                        "schema_version": 2,
                        "provider": provider_lock,
                        "images": uploaded,
                    },
                )
            except ProcessInterrupted:
                raise
            except ImageUploadError:
                raise
            except Exception as exc:
                raise ImageUploadError(
                    "image upload provider initialization failed"
                ) from exc
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
        if uploaded and provider_lock:
            lines.append(f"[i]Image host: {provider_lock}[/i]")
        for name in sorted(uploaded):
            item = uploaded[name]
            lines.extend((f"[b]{name}[/b]", str(item["bbcode"])))
        if not uploaded:
            lines.append(
                "[i]Image upload was disabled; see the lossless PNG sidecars.[/i]"
            )
        atomic_write_text(bbcode, "\n".join(lines) + "\n")
        _assert_public_sidecar_safe(checkpoint)
        _assert_public_sidecar_safe(bbcode)
        upload_marker = paths.stages / "upload.json"
        _write_stage(
            upload_marker,
            {
                "upload_images": selection.upload_images,
                "provider": provider_lock,
                "images": {path.name: sha256_file(path) for path in pngs},
            },
            [checkpoint, bbcode],
        )
        self._stop_at_operator_boundary(job.id)

        # Upload adapters are external code and may run for minutes.  Re-open
        # the trust boundary afterwards so a concurrent/local mutation cannot
        # turn previously validated evidence (or the final MKV) into the
        # public release between upload and staging.
        if (
            _recorded_output_sha256(paths.stages / "mux.json", paths.muxed_output)
            != mux_digest
            or sha256_file(paths.muxed_output) != mux_digest
        ):
            raise ReviewRequired(
                "final Matroska changed during public evidence upload",
                details={"action": "rerun_mux_and_qc"},
            )
        pngs = _current_comparison_pngs(paths, prune=False)
        metric_sidecars = _current_video_metric_sidecars(paths)
        revalidated_video_pins = _assert_stage_outputs_current(
            paths.stages / "comparison.json",
            [
                video_manifest_path,
                *metric_sidecars,
                *(path for path in pngs if path.parent == paths.comparison),
            ],
            stage_name="video comparison",
        )
        revalidated_audio_pins = _assert_stage_outputs_current(
            paths.stages / "qc-audio.json",
            [
                paths.analysis / "audio-comparison.json",
                *(path for path in pngs if path.parent == paths.analysis),
            ],
            stage_name="audio QC",
        )
        revalidated_public_evidence = {
            **revalidated_video_pins,
            **revalidated_audio_pins,
        }
        if revalidated_public_evidence != validated_public_evidence:
            raise ReviewRequired(
                "public evidence checkpoint set changed during image upload",
                details={"action": "rerun_qc"},
            )
        for public_metadata in (
            video_manifest_path,
            paths.analysis / "audio-comparison.json",
            *metric_sidecars,
            checkpoint,
            bbcode,
        ):
            _assert_public_sidecar_safe(public_metadata)
        upload_evidence_pins = _assert_stage_outputs_current(
            upload_marker,
            [checkpoint, bbcode],
            stage_name="upload checkpoint",
        )
        pinned_public_files = {
            **validated_public_evidence,
            **upload_evidence_pins,
        }
        if len(pinned_public_files) != len(validated_public_evidence) + len(
            upload_evidence_pins
        ):
            raise ReviewRequired("public upload evidence overlaps producer outputs")

        configured_completed_root = self.settings.completed_root
        if os.path.lexists(configured_completed_root) and _unsafe_directory_link(
            configured_completed_root
        ):
            raise ReviewRequired("completed root cannot be a symbolic link or junction")
        configured_completed_root.mkdir(mode=0o750, parents=True, exist_ok=True)
        completed_root = configured_completed_root.resolve(strict=True)
        resolved_data_root = self.settings.data_root.resolve(strict=True)
        if (
            completed_root.parent != resolved_data_root
            or completed_root.name != "completed"
        ):
            raise ReviewRequired("completed root escaped the configured data root")
        completed = completed_root / selection.output_name
        if os.path.lexists(completed) and _unsafe_directory_link(completed):
            raise ReviewRequired(
                "completed release directory cannot be a symbolic link or junction"
            )
        owner = completed / ".bdencode-owner.json"
        if os.path.lexists(owner) and _unsafe_directory_link(owner):
            raise ReviewRequired(
                "completed release owner record cannot be a symbolic link or junction"
            )
        if completed.exists() and not owner.is_file() and any(completed.iterdir()):
            raise ReviewRequired(
                f"completed directory is non-empty and has no BDEncode owner record: {completed}"
            )
        completed.mkdir(mode=0o750, parents=True, exist_ok=True)
        if completed.resolve(strict=True).parent != completed_root:
            raise ReviewRequired("completed release directory escaped completed_root")
        if owner.is_file():
            ownership = json.loads(owner.read_text(encoding="utf-8"))
            legacy_owner = (
                ownership.get("schema_version") == 1
                and ownership.get("job_id") == job.id
            )
            current_owner = (
                ownership.get("schema_version") == 2
                and ownership.get("output_name") == selection.output_name
                and ownership.get("mux_sha256") == mux_digest
            )
            if not legacy_owner and not current_owner:
                raise ReviewRequired(
                    f"completed directory contains a different release: {completed}"
                )
        _validate_completed_members(completed, selection.output_name, allow_legacy=True)
        atomic_write_json(
            owner,
            {
                "schema_version": 2,
                "output_name": selection.output_name,
                "mux_sha256": mux_digest,
            },
        )
        final_output = completed / f"{selection.output_name}.mkv"
        if os.path.lexists(final_output) and _unsafe_directory_link(final_output):
            raise ReviewRequired(
                "completed release output cannot be a symbolic link or junction"
            )
        finalize_inputs = {
            "mux_sha256": mux_digest,
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
            _write_stage(finalize_marker, finalize_inputs, [final_output])
        # Public release sidecars are rebuilt from an explicit allowlist.  This
        # also removes v1 logs/manifests and stale analysis files when an owned
        # completed directory is migrated in place.
        public_analysis_files = tuple(
            sidecar
            for sidecar in (
                paths.analysis / "audio-comparison.json",
                *(path for path in pngs if path.parent == paths.analysis),
            )
        )
        public_comparison_files = tuple(
            sidecar
            for sidecar in (
                video_manifest_path,
                checkpoint,
                bbcode,
                *metric_sidecars,
                *(path for path in pngs if path.parent == paths.comparison),
            )
        )
        _replace_public_sidecars(
            completed=completed,
            comparison_source=paths.comparison,
            analysis_source=paths.analysis,
            comparison_files=public_comparison_files,
            analysis_files=public_analysis_files,
            expected_files=pinned_public_files,
        )
        _validate_completed_members(
            completed, selection.output_name, allow_legacy=False
        )
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
        completed_job = self.queue.advance(
            job.id, JobState.COMPLETED, message="encode, QC and comparison completed"
        )
        self._cleanup_completed_work(completed_job, paths)

    def _cleanup_completed_work(self, job: Job, paths: JobPaths) -> None:
        """Remove only bulky retry data after the durable COMPLETE transition.

        FAILED and operator-paused jobs deliberately retain ``work`` so their
        validated checkpoints can be resumed.  Logs, analysis, comparisons,
        stage records and manifests remain under the job root even after a
        successful encode because database artifacts point to those files.

        Cleanup is best-effort and can never turn an already completed encode
        back into a failure.  Every target is resolved and checked against the
        configured job root before recursive removal.
        """

        if job.state is not JobState.COMPLETED or not paths.work.exists():
            return

        operation = None
        try:
            operation = self.maintenance.begin(
                "completed-workspace-cleanup",
                job.id,
                [
                    MaintenanceTargetSpec(
                        paths.work,
                        paths.root,
                        "completed temporary work directory",
                    )
                ],
                guard=MaintenanceDomainGuard(
                    job_id=job.id,
                    expected_job_version=job.version,
                    allowed_job_states=(JobState.COMPLETED.value,),
                ),
            )
            removed_bytes = int(operation.targets[0]["bytes_moved"])
            self.maintenance.stage(operation.id)
            self.database.record_completed_cleanup(
                job.id,
                expected_version=job.version,
                cleanup=None,
                payload={"bytes_removed": removed_bytes},
                maintenance_operation_id=operation.id,
            )
            self.maintenance.finalize(operation.id)
        except (OSError, RuntimeError) as exc:
            if operation is not None:
                try:
                    current = self.maintenance.operation(operation.id)
                    if current.phase in {
                        MaintenancePhase.INTENT,
                        MaintenancePhase.DETACHED,
                    }:
                        self.maintenance.rollback(operation.id)
                except Exception:
                    LOG.exception("completed job %s workspace rollback failed", job.id)
            LOG.warning("completed job %s workspace cleanup failed: %s", job.id, exc)
            try:
                self.database.add_event(
                    EventCreate(
                        job_id=job.id,
                        kind="job.workspace-cleanup-warning",
                        message="completed encode retained temporary work files",
                        payload={"error_type": type(exc).__name__},
                    )
                )
            except Exception:
                LOG.exception("job %s cleanup warning could not be recorded", job.id)
            return

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
        manifest_path = str(paths.manifest_json.resolve(strict=False))
        artifacts = [
            item.model_dump(mode="json")
            for item in self.database.list_artifacts(job_id=job.id, limit=1000)
            if item.path != manifest_path
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
                "manifest_registered_separately": True,
            },
        )
        self._register_artifact(
            job.id,
            paths.manifest_json,
            DatabaseArtifactKind.MANIFEST,
            paths.manifest_json.name,
            mime_type="application/json",
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
    stop_requested: Callable[[], bool] | None = None,
) -> int:
    """Run one serial encode lane plus one lightweight concurrent scan lane.

    ``once`` remains deterministic for maintenance/tests and processes at most
    one claimed job without starting the background preparation lane.
    """
    settings = settings.validate()
    settings.create_directories()
    database.initialize()
    interval = settings.worker_poll_seconds if poll_interval is None else poll_interval
    if interval < 0:
        raise ValueError("poll_interval cannot be negative")
    stopping = False

    def should_stop() -> bool:
        return stopping or bool(stop_requested and stop_requested())

    worker = PipelineWorker(database, settings, stop_requested=should_stop)
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

    idle_sleep = max(interval, 0.05)

    def claim_lane(
        current: Callable[[], Job | None],
        claim: Callable[[], Job | None],
    ) -> Job | None:
        """Observe or claim one lane while excluding deployment transactions."""

        if _maintenance_active():
            return None
        if fcntl_module is None:
            return current() or claim()
        deployment_path = settings.state_root / "deployment.lock"
        with deployment_path.open("a+b") as deployment_lock:
            os.chmod(deployment_path, 0o640)
            while not should_stop():
                try:
                    fcntl_module.flock(
                        deployment_lock.fileno(),
                        fcntl_module.LOCK_SH | fcntl_module.LOCK_NB,
                    )
                    break
                except BlockingIOError:
                    time.sleep(min(idle_sleep, 0.2))
            if should_stop():
                return None
            try:
                if _maintenance_active():
                    return None
                return current() or claim()
            finally:
                fcntl_module.flock(deployment_lock.fileno(), fcntl_module.LOCK_UN)

    preparation_thread: threading.Thread | None = None
    try:
        _sd_notify("READY=1\nSTATUS=Ready; waiting for an encode job")
        if once:
            job = claim_lane(database.encoding_job, worker.queue.claim_next_ready)
            if job is None:
                job = claim_lane(database.preparing_job, worker.queue.claim_next)
            if job is not None:
                worker.process_job(job)
            return 0

        preparation_worker = PipelineWorker(
            database,
            settings,
            stop_requested=should_stop,
        )

        def prepare_jobs() -> None:
            while not should_stop():
                try:
                    job = claim_lane(
                        database.preparing_job,
                        preparation_worker.queue.claim_next,
                    )
                    if job is None:
                        time.sleep(idle_sleep)
                        continue
                    preparation_worker.process_job(job)
                except Exception:
                    LOG.exception(
                        "preparation lane failed; retrying after poll interval"
                    )
                    time.sleep(idle_sleep)

        preparation_thread = threading.Thread(
            target=prepare_jobs,
            name="bdencode-preparation",
            daemon=True,
        )
        preparation_thread.start()

        while not should_stop():
            job = claim_lane(database.encoding_job, worker.queue.claim_next_ready)
            if job is None:
                time.sleep(idle_sleep)
                continue
            worker.process_job(job)
            # A processing review/upload pause intentionally keeps the encode
            # lane. Polling lets an API action resume it without a restart.
            time.sleep(idle_sleep)
        return 0
    finally:
        stopping = True
        if preparation_thread is not None:
            preparation_thread.join(timeout=min(max(idle_sleep * 2, 0.2), 5.0))
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
