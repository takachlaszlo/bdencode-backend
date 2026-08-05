"""Headless Blu-ray/UHD discovery with injectable external-tool adapters.

The scanner never writes to the source.  Native libbluray playlist metadata is
preferred when an adapter is supplied; ffprobe linked against libbluray provides
stream properties, while mkvmerge/MediaInfo can inspect a clip as a degraded
fallback.  Every subprocess receives an argv sequence and ``shell`` is never used.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable, Mapping, Protocol, Sequence

from .language import LanguageDecision, LanguageResolver


DEFAULT_SOURCE_ROOT = Path("/home/accofil/storage")


class DiscKind(StrEnum):
    BD = "bd"
    UHD = "uhd"


class ContentKind(StrEnum):
    FILM = "film"
    CONCERT = "concert"
    ANIME = "anime"
    SERIES = "series"


class StreamKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"


class VideoCodec(StrEnum):
    AVC = "avc"
    VC1 = "vc1"
    MPEG2 = "mpeg2"
    HEVC = "hevc"
    MVC = "mvc"
    UNKNOWN = "unknown"


class TrackRole(StrEnum):
    MAIN = "main"
    COMMENTARY = "commentary"
    AUDIO_DESCRIPTION = "audio_description"
    SDH = "sdh"
    FORCED = "forced"
    SIGNS_SONGS = "signs_songs"
    DUB = "dub"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class HdrStaticMetadata:
    mastering_display: str | None = None
    max_cll: int | None = None
    max_fall: int | None = None

    @property
    def complete(self) -> bool:
        return bool(
            self.mastering_display is not None
            and self.max_cll is not None
            and self.max_fall is not None
        )


@dataclass(frozen=True, slots=True)
class VideoProperties:
    codec: VideoCodec
    width: int | None = None
    height: int | None = None
    frame_rate: str | None = None
    field_order: str | None = None
    bit_depth: int | None = None
    pixel_format: str | None = None
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_matrix: str | None = None
    color_range: str | None = None
    chroma_location: str | None = None
    hdr10: bool = False
    hdr10_static: HdrStaticMetadata = field(default_factory=HdrStaticMetadata)
    dolby_vision: bool = False
    dolby_vision_profile: int | None = None
    hdr10_base_layer: bool = False
    hdr10_plus: bool = False
    three_d: bool = False

    @property
    def interlaced(self) -> bool:
        return self.field_order not in {None, "unknown", "progressive"}


@dataclass(frozen=True, slots=True)
class MediaStream:
    id: str
    index: int
    pid: int | None
    kind: StreamKind
    codec: str
    codec_profile: str | None = None
    language: LanguageDecision | None = None
    title: str | None = None
    channels: int | None = None
    channel_layout: str | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    default: bool = False
    forced: bool = False
    roles: tuple[TrackRole, ...] = ()
    object_audio: bool = False
    video: VideoProperties | None = None

    def __post_init__(self) -> None:
        if self.kind is StreamKind.VIDEO and self.video is None:
            raise ValueError("video streams require VideoProperties")
        if self.kind is not StreamKind.VIDEO and self.video is not None:
            raise ValueError("only video streams may contain VideoProperties")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["kind"] = self.kind.value
        result["roles"] = [role.value for role in self.roles]
        result["language"] = self.language.to_dict() if self.language else None
        if self.video:
            result["video"]["codec"] = self.video.codec.value
        return result


@dataclass(frozen=True, slots=True)
class PlaylistSegment:
    clip_id: str
    in_time_seconds: float
    out_time_seconds: float
    relative_start_seconds: float = 0.0
    seamless: bool = True
    angle: int = 1

    def __post_init__(self) -> None:
        if self.in_time_seconds < 0 or self.out_time_seconds <= self.in_time_seconds:
            raise ValueError("playlist segment timestamps are invalid")
        if self.relative_start_seconds < 0:
            raise ValueError("relative segment start cannot be negative")
        if self.angle < 1:
            raise ValueError("angle must be at least one")

    @property
    def duration_seconds(self) -> float:
        return self.out_time_seconds - self.in_time_seconds


@dataclass(frozen=True, slots=True)
class PlaylistCandidate:
    playlist_id: str
    duration_seconds: float
    chapters: tuple[float, ...] = ()
    segments: tuple[PlaylistSegment, ...] = ()
    streams: tuple[MediaStream, ...] = ()
    angle_count: int = 1
    seamless_branching: bool = False
    edition_group: str | None = None
    edition_label: str | None = None
    episode_number: int | None = None
    recommended: bool = False

    def __post_init__(self) -> None:
        if not self.playlist_id.isdigit():
            raise ValueError("playlist_id must contain decimal digits")
        if self.duration_seconds < 0:
            raise ValueError("playlist duration cannot be negative")
        if self.angle_count < 1:
            raise ValueError("angle_count must be at least one")

    @property
    def video_streams(self) -> tuple[MediaStream, ...]:
        return tuple(item for item in self.streams if item.kind is StreamKind.VIDEO)

    @property
    def audio_streams(self) -> tuple[MediaStream, ...]:
        return tuple(item for item in self.streams if item.kind is StreamKind.AUDIO)

    @property
    def subtitle_streams(self) -> tuple[MediaStream, ...]:
        return tuple(item for item in self.streams if item.kind is StreamKind.SUBTITLE)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["segments"] = [asdict(item) for item in self.segments]
        result["streams"] = [item.to_dict() for item in self.streams]
        return result


@dataclass(frozen=True, slots=True)
class ToolCapabilities:
    ffprobe: str | None = None
    mediainfo: str | None = None
    mkvmerge: str | None = None
    libbluray_json: str | None = None
    ffprobe_bluray: bool = False

    @property
    def can_scan_playlists(self) -> bool:
        return bool(self.libbluray_json or (self.ffprobe and self.ffprobe_bluray))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"can_scan_playlists": self.can_scan_playlists}


@dataclass(frozen=True, slots=True)
class DiscScan:
    source: Path
    disc_kind: DiscKind
    content_kind: ContentKind
    playlists: tuple[PlaylistCandidate, ...]
    capabilities: ToolCapabilities
    fingerprint: str
    warnings: tuple[str, ...] = ()

    @property
    def has_multiple_editions(self) -> bool:
        groups: dict[str, int] = {}
        for item in self.playlists:
            if item.edition_group:
                groups[item.edition_group] = groups.get(item.edition_group, 0) + 1
        return any(count > 1 for count in groups.values())

    @property
    def has_seamless_branching(self) -> bool:
        return any(item.seamless_branching for item in self.playlists)

    @property
    def has_three_d(self) -> bool:
        return any(
            stream.video and stream.video.three_d
            for playlist in self.playlists
            for stream in playlist.video_streams
        )

    def playlist(self, playlist_id: str) -> PlaylistCandidate:
        normalized = playlist_id.zfill(5)
        try:
            return next(
                item for item in self.playlists if item.playlist_id == normalized
            )
        except StopIteration as exc:
            raise KeyError(f"playlist {normalized} is not present in the scan") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "disc_kind": self.disc_kind.value,
            "content_kind": self.content_kind.value,
            "playlists": [item.to_dict() for item in self.playlists],
            "capabilities": self.capabilities.to_dict(),
            "fingerprint": self.fingerprint,
            "has_multiple_editions": self.has_multiple_editions,
            "has_seamless_branching": self.has_seamless_branching,
            "has_three_d": self.has_three_d,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class CaptureResult:
    returncode: int
    stdout: str
    stderr: str = ""


class CaptureRunner(Protocol):
    def capture(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        timeout: float = 30,
        check: bool = True,
    ) -> Any: ...


class SubprocessCaptureRunner:
    """Default adapter. Tests normally inject a deterministic fake."""

    def capture(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        timeout: float = 30,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(os.fspath(item) for item in argv)
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=check,
            shell=False,
        )


LibblurayProvider = Callable[[Path], Mapping[str, Any]]


def validate_source_path(
    source: str | os.PathLike[str], source_root: Path = DEFAULT_SOURCE_ROOT
) -> Path:
    """Resolve a disc path and reject traversal/symlink escapes from storage."""

    if "\x00" in os.fspath(source):
        raise ValueError("source path contains a NUL byte")
    root = source_root.expanduser().resolve(strict=True)
    resolved = Path(source).expanduser().resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"source must be within {root}") from exc
    disc_root = resolved.parent if resolved.name.upper() == "BDMV" else resolved
    bdmv = disc_root / "BDMV"
    if not (bdmv / "PLAYLIST").is_dir() or not (bdmv / "STREAM").is_dir():
        raise ValueError(
            "source is not a Blu-ray directory with BDMV/PLAYLIST and BDMV/STREAM"
        )
    return disc_root


def discover_capabilities(runner: CaptureRunner | None = None) -> ToolCapabilities:
    runner = runner or SubprocessCaptureRunner()
    ffprobe = shutil.which("ffprobe")
    ffprobe_bluray = False
    if ffprobe:
        try:
            completed = runner.capture(
                [ffprobe, "-v", "quiet", "-protocols"], check=False
            )
            text = f"{getattr(completed, 'stdout', '')}\n{getattr(completed, 'stderr', '')}"
            ffprobe_bluray = any(line.strip() == "bluray" for line in text.splitlines())
        except (OSError, subprocess.SubprocessError):
            pass
    return ToolCapabilities(
        ffprobe=ffprobe,
        mediainfo=shutil.which("mediainfo"),
        mkvmerge=shutil.which("mkvmerge"),
        libbluray_json=shutil.which("bdencode-libbluray-scan"),
        ffprobe_bluray=ffprobe_bluray,
    )


class BluRayScanner:
    def __init__(
        self,
        *,
        runner: CaptureRunner | None = None,
        capabilities: ToolCapabilities | None = None,
        libbluray_provider: LibblurayProvider | None = None,
        source_root: Path = DEFAULT_SOURCE_ROOT,
        max_playlists: int = 128,
    ) -> None:
        self.runner = runner or SubprocessCaptureRunner()
        self.capabilities = capabilities or discover_capabilities(self.runner)
        self.libbluray_provider = libbluray_provider
        self.source_root = source_root
        self.max_playlists = max_playlists
        self.languages = LanguageResolver()
        self._hdr_static_cache: dict[Path, HdrStaticMetadata] = {}

    def scan(
        self,
        source: str | os.PathLike[str],
        *,
        content_kind: ContentKind | str = ContentKind.FILM,
    ) -> DiscScan:
        root = validate_source_path(source, self.source_root)
        content = ContentKind(content_kind)
        warnings: list[str] = []
        native = self._native_metadata(root)
        mpls_files = sorted((root / "BDMV" / "PLAYLIST").glob("*.mpls"))
        playlist_ids = [item.stem.zfill(5) for item in mpls_files[: self.max_playlists]]
        native_by_id = {
            str(item.get("id", item.get("playlist_id", ""))).zfill(5): item
            for item in native.get("playlists", [])
            if isinstance(item, Mapping)
        }
        playlist_ids = sorted(set(playlist_ids) | set(native_by_id))

        playlists: list[PlaylistCandidate] = []
        if self.capabilities.ffprobe and self.capabilities.ffprobe_bluray:
            for playlist_id in playlist_ids:
                probe = self._ffprobe_playlist(root, playlist_id)
                metadata = native_by_id.get(playlist_id, {})
                if probe is None and not metadata:
                    continue
                if probe is not None:
                    probe = self._with_hdr_static_metadata(
                        root,
                        probe,
                        metadata,
                    )
                playlists.append(
                    self._playlist_from_payload(playlist_id, probe or {}, metadata)
                )
        elif native_by_id:
            playlists.extend(
                self._playlist_from_payload(playlist_id, {}, payload)
                for playlist_id, payload in native_by_id.items()
            )
        else:
            fallback = self._fallback_largest_clip(root)
            if fallback:
                playlists.append(fallback)
                warnings.append(
                    "No libbluray playlist backend is available; only the largest M2TS clip was inspected."
                )

        if len(mpls_files) > self.max_playlists:
            warnings.append(
                f"Playlist safety limit reached: scanned {self.max_playlists} of {len(mpls_files)} MPLS files."
            )
        if not playlists:
            raise RuntimeError(
                "no readable Blu-ray playlists or transport-stream clips were found"
            )

        playlists = self._annotate_structure(playlists, content)
        disc_kind = self._disc_kind(playlists)
        if (
            any(
                stream.video and stream.video.three_d
                for playlist in playlists
                for stream in playlist.video_streams
            )
            or (root / "BDMV" / "STREAM" / "SSIF").exists()
        ):
            warnings.append(
                "MVC/3D content detected; 3D output is intentionally unsupported."
            )

        return DiscScan(
            source=root,
            disc_kind=disc_kind,
            content_kind=content,
            playlists=tuple(playlists),
            capabilities=self.capabilities,
            fingerprint=_disc_fingerprint(root),
            warnings=tuple(warnings),
        )

    def _native_metadata(self, root: Path) -> Mapping[str, Any]:
        if self.libbluray_provider:
            return self.libbluray_provider(root)
        executable = self.capabilities.libbluray_json
        if not executable:
            return {}
        completed = self.runner.capture(
            [executable, "--json", os.fspath(root)], timeout=120, check=False
        )
        if getattr(completed, "returncode", 1) != 0:
            return {}
        return _json_object(getattr(completed, "stdout", ""))

    def _ffprobe_playlist(
        self, root: Path, playlist_id: str
    ) -> Mapping[str, Any] | None:
        assert self.capabilities.ffprobe
        completed = self.runner.capture(
            [
                self.capabilities.ffprobe,
                "-v",
                "error",
                "-playlist",
                str(int(playlist_id)),
                "-show_streams",
                "-show_format",
                "-show_chapters",
                "-of",
                "json",
                f"bluray:{root}",
            ],
            timeout=180,
            check=False,
        )
        if getattr(completed, "returncode", 1) != 0:
            return None
        payload = _json_object(getattr(completed, "stdout", ""))
        return payload if payload else None

    def _with_hdr_static_metadata(
        self,
        root: Path,
        probe: Mapping[str, Any],
        native: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Enrich an HDR10 video stream from a directly referenced M2TS clip.

        Normal ``ffprobe -show_streams`` output often contains the BT.2020/PQ
        colour description but not mastering-display or content-light metadata.
        Those values are commonly exposed as decoded-frame side data.

        Frame probing the complete ``bluray:`` input is deliberately avoided:
        some libbluray/FFmpeg combinations crash on malformed or obfuscated
        playlists.  A representative M2TS clip is probed directly instead.
        """

        raw_streams = probe.get("streams", ())
        if not isinstance(raw_streams, (list, tuple)):
            return probe

        video_index: int | None = None
        video_stream: Mapping[str, Any] | None = None

        for index, candidate in enumerate(raw_streams):
            if not isinstance(candidate, Mapping):
                continue
            if str(
                candidate.get("codec_type", candidate.get("type", ""))
            ).lower() == "video":
                video_index = index
                video_stream = candidate
                break

        if video_index is None or video_stream is None:
            return probe

        native_video: Mapping[str, Any] = {}
        first_native_video: Mapping[str, Any] | None = None
        probe_key = _stream_match_key(video_stream)
        native_streams = native.get("streams", ())

        if isinstance(native_streams, (list, tuple)):
            for candidate in native_streams:
                if not isinstance(candidate, Mapping):
                    continue
                if str(
                    candidate.get("codec_type", candidate.get("type", ""))
                ).lower() != "video":
                    continue

                if first_native_video is None:
                    first_native_video = candidate

                if _stream_match_key(candidate) == probe_key:
                    native_video = candidate
                    break

        if not native_video and first_native_video is not None:
            native_video = first_native_video

        transfer = _optional_str(video_stream.get("color_transfer"))
        if transfer is None:
            transfer = _optional_str(native_video.get("color_transfer"))

        primaries = _optional_str(video_stream.get("color_primaries"))
        if primaries is None:
            primaries = _optional_str(native_video.get("color_primaries"))

        hdr10 = bool(
            native_video.get(
                "hdr10",
                transfer == "smpte2084" and primaries == "bt2020",
            )
        )

        if not hdr10:
            return probe

        existing_mastering = _optional_str(
            video_stream.get("mastering_display")
        )
        if existing_mastering is None:
            existing_mastering = _optional_str(
                native_video.get("mastering_display")
            )

        existing_max_cll = _int(video_stream.get("max_cll"), None)
        if existing_max_cll is None:
            existing_max_cll = _int(native_video.get("max_cll"), None)

        existing_max_fall = _int(video_stream.get("max_fall"), None)
        if existing_max_fall is None:
            existing_max_fall = _int(native_video.get("max_fall"), None)

        existing = HdrStaticMetadata(
            mastering_display=existing_mastering,
            max_cll=existing_max_cll,
            max_fall=existing_max_fall,
        )

        if existing.complete:
            return probe

        clip = _representative_clip_path(root, native)
        if clip is None:
            return probe

        detected = self._probe_clip_hdr_static(clip)

        # Ha a már meglévő és az újonnan detektált értékek ellentmondanak
        # egymásnak, fail-closed maradunk: nem gyártunk vegyes metaadatot.
        if (
            existing.mastering_display is not None
            and detected.mastering_display is not None
            and existing.mastering_display != detected.mastering_display
        ):
            return probe

        if (
            existing.max_cll is not None
            and detected.max_cll is not None
            and existing.max_cll != detected.max_cll
        ):
            return probe

        if (
            existing.max_fall is not None
            and detected.max_fall is not None
            and existing.max_fall != detected.max_fall
        ):
            return probe

        merged = HdrStaticMetadata(
            mastering_display=(
                existing.mastering_display
                if existing.mastering_display is not None
                else detected.mastering_display
            ),
            max_cll=(
                existing.max_cll
                if existing.max_cll is not None
                else detected.max_cll
            ),
            max_fall=(
                existing.max_fall
                if existing.max_fall is not None
                else detected.max_fall
            ),
        )

        if (
            merged.mastering_display is None
            and merged.max_cll is None
            and merged.max_fall is None
        ):
            return probe

        enriched_video = dict(video_stream)

        if merged.mastering_display is not None:
            enriched_video["mastering_display"] = merged.mastering_display
        if merged.max_cll is not None:
            enriched_video["max_cll"] = merged.max_cll
        if merged.max_fall is not None:
            enriched_video["max_fall"] = merged.max_fall

        enriched_streams = list(raw_streams)
        enriched_streams[video_index] = enriched_video

        enriched_probe = dict(probe)
        enriched_probe["streams"] = enriched_streams
        return enriched_probe

    def _probe_clip_hdr_static(
        self,
        clip: Path,
    ) -> HdrStaticMetadata:
        cached = self._hdr_static_cache.get(clip)
        if cached is not None:
            return cached

        ffprobe = self.capabilities.ffprobe
        if not ffprobe:
            result = HdrStaticMetadata()
            self._hdr_static_cache[clip] = result
            return result

        completed = self.runner.capture(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-read_intervals",
                "%+3",
                "-show_frames",
                "-show_entries",
                "frame=side_data_list",
                "-of",
                "json",
                os.fspath(clip),
            ],
            timeout=90,
            check=False,
        )

        if getattr(completed, "returncode", 1) != 0:
            result = HdrStaticMetadata()
            self._hdr_static_cache[clip] = result
            return result

        payload = _json_object(getattr(completed, "stdout", ""))
        result = _hdr_static_from_frame_payload(payload)
        self._hdr_static_cache[clip] = result
        return result

    def _fallback_largest_clip(self, root: Path) -> PlaylistCandidate | None:
        clips = sorted(
            (root / "BDMV" / "STREAM").glob("*.m2ts"),
            key=lambda path: path.stat().st_size,
            reverse=True,
        )
        if not clips:
            return None
        clip = clips[0]
        payload: Mapping[str, Any] = {}
        if self.capabilities.ffprobe:
            completed = self.runner.capture(
                [
                    self.capabilities.ffprobe,
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-show_chapters",
                    "-of",
                    "json",
                    os.fspath(clip),
                ],
                timeout=120,
                check=False,
            )
            if getattr(completed, "returncode", 1) == 0:
                payload = _json_object(getattr(completed, "stdout", ""))
        elif self.capabilities.mkvmerge:
            completed = self.runner.capture(
                [self.capabilities.mkvmerge, "-J", os.fspath(clip)],
                timeout=120,
                check=False,
            )
            if getattr(completed, "returncode", 1) == 0:
                payload = _mkvmerge_to_probe(
                    _json_object(getattr(completed, "stdout", ""))
                )
        elif self.capabilities.mediainfo:
            completed = self.runner.capture(
                [self.capabilities.mediainfo, "--Output=JSON", os.fspath(clip)],
                timeout=120,
                check=False,
            )
            if getattr(completed, "returncode", 1) == 0:
                payload = _mediainfo_to_probe(
                    _json_object(getattr(completed, "stdout", ""))
                )
        else:
            raise RuntimeError(
                "ffprobe, mkvmerge or mediainfo is required for clip inspection"
            )
        metadata = {
            "duration": _float(payload.get("format", {}).get("duration"), 0.0),
            "segments": [
                {
                    "clip_id": clip.stem,
                    "in_time": 0,
                    "out_time": max(
                        _float(payload.get("format", {}).get("duration"), 0.0), 0.001
                    ),
                }
            ],
        }
        if payload:
            payload = self._with_hdr_static_metadata(root, payload, metadata)
        return self._playlist_from_payload("00000", payload, metadata)

    def _playlist_from_payload(
        self,
        playlist_id: str,
        probe: Mapping[str, Any],
        native: Mapping[str, Any],
    ) -> PlaylistCandidate:
        native_streams = {
            _stream_match_key(item): item
            for item in native.get("streams", [])
            if isinstance(item, Mapping)
        }
        streams: list[MediaStream] = []
        for index, item in enumerate(probe.get("streams", [])):
            if not isinstance(item, Mapping):
                continue
            if str(item.get("codec_type", item.get("type", ""))).lower() not in {
                "video",
                "audio",
                "subtitle",
            }:
                continue
            native_item = native_streams.get(_stream_match_key(item), {})
            streams.append(self._stream_from_probe(index, item, native_item))
        if not streams:
            for index, item in enumerate(native.get("streams", [])):
                if isinstance(item, Mapping) and str(
                    item.get("codec_type", item.get("type", ""))
                ).lower() in {"video", "audio", "subtitle"}:
                    streams.append(self._stream_from_probe(index, item, item))

        duration = _float(native.get("duration"), None)
        if duration is None:
            duration = _float(probe.get("format", {}).get("duration"), 0.0)
        segments = tuple(
            self._segment(item)
            for item in native.get("segments", [])
            if isinstance(item, Mapping)
        )
        chapter_payload = probe.get("chapters") or native.get("chapters", [])
        chapters = tuple(
            _float(
                item.get(
                    "start_time", item.get("start_time_seconds", item.get("time", 0.0))
                ),
                0.0,
            )
            for item in chapter_payload
            if isinstance(item, Mapping)
        )
        return PlaylistCandidate(
            playlist_id=playlist_id.zfill(5),
            duration_seconds=duration
            or sum(segment.duration_seconds for segment in segments),
            chapters=chapters,
            segments=segments,
            streams=tuple(streams),
            angle_count=int(native.get("angle_count", 1)),
            seamless_branching=bool(
                native.get("seamless_branching", len(segments) > 1)
            ),
            edition_group=_optional_str(native.get("edition_group")),
            edition_label=_optional_str(native.get("edition_label")),
            episode_number=_int(native.get("episode_number"), None),
            recommended=bool(native.get("recommended", False)),
        )

    def _stream_from_probe(
        self, index: int, item: Mapping[str, Any], native: Mapping[str, Any]
    ) -> MediaStream:
        codec_type = str(
            item.get(
                "codec_type",
                item.get("type", native.get("codec_type", native.get("type", ""))),
            )
        ).lower()
        kind = {
            "video": StreamKind.VIDEO,
            "audio": StreamKind.AUDIO,
            "subtitle": StreamKind.SUBTITLE,
        }.get(codec_type)
        if kind is None:
            raise ValueError(f"unsupported stream type from probe: {codec_type}")
        tags = item.get("tags", {}) if isinstance(item.get("tags"), Mapping) else {}
        disposition = (
            item.get("disposition", {})
            if isinstance(item.get("disposition"), Mapping)
            else {}
        )
        pid = _parse_pid(item.get("id", native.get("pid")))
        raw_language = {
            "mpls": native.get("mpls_language"),
            "clpi": native.get("clpi_language"),
            "pmt": tags.get("language", native.get("pmt_language")),
        }
        language = self.languages.resolve(**raw_language)
        codec_name = str(item.get("codec_name", native.get("codec", "unknown"))).lower()
        title = _optional_str(tags.get("title", native.get("title")))
        forced = bool(disposition.get("forced", native.get("forced", False)))
        roles = _roles(title, forced, native.get("roles", ()))
        video = _video_properties(item, native) if kind is StreamKind.VIDEO else None
        object_audio = any(
            token in f"{codec_name} {item.get('profile', '')} {title or ''}".lower()
            for token in ("atmos", "dts:x", "dtsx")
        )
        return MediaStream(
            id=f"{kind.value}:{pid if pid is not None else index}",
            index=_int(item.get("index"), index) or index,
            pid=pid,
            kind=kind,
            codec=codec_name,
            codec_profile=_optional_str(item.get("profile", native.get("profile"))),
            language=language,
            title=title,
            channels=_int(item.get("channels", native.get("channels")), None),
            channel_layout=_optional_str(
                item.get("channel_layout", native.get("channel_layout"))
            ),
            sample_rate=_int(item.get("sample_rate", native.get("sample_rate")), None),
            bit_depth=_int(
                item.get(
                    "bits_per_raw_sample",
                    item.get("bits_per_sample", native.get("bit_depth")),
                ),
                None,
            ),
            default=bool(disposition.get("default", native.get("default", False))),
            forced=forced,
            roles=roles,
            object_audio=object_audio,
            video=video,
        )

    @staticmethod
    def _segment(item: Mapping[str, Any]) -> PlaylistSegment:
        in_time = _float(item.get("in_time", item.get("in_time_seconds")), 0.0) or 0.0
        out_time = _float(item.get("out_time", item.get("out_time_seconds")), None)
        duration = _float(item.get("duration"), None)
        if out_time is None:
            out_time = in_time + (duration or 0.001)
        return PlaylistSegment(
            clip_id=str(item.get("clip_id", item.get("clip", "unknown"))),
            in_time_seconds=in_time,
            out_time_seconds=out_time,
            relative_start_seconds=_float(
                item.get("relative_start", item.get("relative_start_seconds")), 0.0
            )
            or 0.0,
            seamless=bool(item.get("seamless", True)),
            angle=_int(item.get("angle"), 1) or 1,
        )

    @staticmethod
    def _disc_kind(playlists: Sequence[PlaylistCandidate]) -> DiscKind:
        videos = [
            stream.video
            for item in playlists
            for stream in item.video_streams
            if stream.video
        ]
        return (
            DiscKind.UHD
            if any(
                video.codec is VideoCodec.HEVC
                or (video.width or 0) > 1920
                or (video.height or 0) > 1080
                for video in videos
            )
            else DiscKind.BD
        )

    @staticmethod
    def _annotate_structure(
        playlists: list[PlaylistCandidate], content: ContentKind
    ) -> list[PlaylistCandidate]:
        """Mark likely main/episode playlists without hiding any edition choice."""

        if not playlists:
            return playlists
        from dataclasses import replace

        longest = max(playlists, key=lambda item: item.duration_seconds)
        if content is ContentKind.SERIES:
            episodes = sorted(
                (item for item in playlists if item.duration_seconds >= 10 * 60),
                key=lambda item: (item.duration_seconds, item.playlist_id),
            )
            episode_map = {
                item.playlist_id: number + 1 for number, item in enumerate(episodes)
            }
            return [
                replace(
                    item,
                    episode_number=item.episode_number
                    or episode_map.get(item.playlist_id),
                    recommended=item.recommended or item.playlist_id in episode_map,
                )
                for item in playlists
            ]
        return [
            replace(
                item,
                recommended=item.recommended or item.playlist_id == longest.playlist_id,
            )
            for item in playlists
        ]



def _representative_clip_path(
    root: Path,
    native: Mapping[str, Any],
) -> Path | None:
    """Choose the clip contributing the largest duration to the playlist."""

    segments = native.get("segments", ())
    if not isinstance(segments, (list, tuple)):
        return None

    durations: dict[str, float] = {}
    packets: dict[str, int] = {}

    for segment in segments:
        if not isinstance(segment, Mapping):
            continue

        raw_clip_id = segment.get("clip_id", segment.get("clip"))
        if raw_clip_id in (None, ""):
            continue

        clip_id = Path(str(raw_clip_id)).stem

        if not clip_id.isdigit() or len(clip_id) > 5:
            continue

        clip_id = clip_id.zfill(5)

        duration = _float(segment.get("duration"), None)
        if duration is None:
            in_time = (
                _float(
                    segment.get(
                        "in_time",
                        segment.get("in_time_seconds"),
                    ),
                    0.0,
                )
                or 0.0
            )
            out_time = _float(
                segment.get(
                    "out_time",
                    segment.get("out_time_seconds"),
                ),
                None,
            )
            duration = (
                max(out_time - in_time, 0.0)
                if out_time is not None
                else 0.0
            )

        durations[clip_id] = durations.get(clip_id, 0.0) + max(
            duration or 0.0,
            0.0,
        )
        packets[clip_id] = packets.get(clip_id, 0) + (
            _int(segment.get("packet_count"), 0) or 0
        )

    stream_directory = root / "BDMV" / "STREAM"
    ranked: list[tuple[float, int, int, str, Path]] = []

    for clip_id, duration in durations.items():
        lower = stream_directory / f"{clip_id}.m2ts"
        upper = stream_directory / f"{clip_id}.M2TS"

        if lower.is_file():
            candidate = lower
        elif upper.is_file():
            candidate = upper
        else:
            continue

        try:
            size = candidate.stat().st_size
        except OSError:
            continue

        ranked.append(
            (
                duration,
                packets.get(clip_id, 0),
                size,
                clip_id,
                candidate,
            )
        )

    if not ranked:
        return None

    return max(ranked, key=lambda item: item[:4])[4]


def _scaled_hdr_value(value: Any, scale: int) -> int | None:
    try:
        scaled = Fraction(str(value)) * scale
    except (TypeError, ValueError, ZeroDivisionError):
        return None

    if scaled.denominator != 1 or scaled.numerator < 0:
        return None

    return scaled.numerator


def _mastering_display_from_side_data(
    side_data: Mapping[str, Any],
) -> str | None:
    red_x = _scaled_hdr_value(side_data.get("red_x"), 50000)
    red_y = _scaled_hdr_value(side_data.get("red_y"), 50000)
    green_x = _scaled_hdr_value(side_data.get("green_x"), 50000)
    green_y = _scaled_hdr_value(side_data.get("green_y"), 50000)
    blue_x = _scaled_hdr_value(side_data.get("blue_x"), 50000)
    blue_y = _scaled_hdr_value(side_data.get("blue_y"), 50000)
    white_x = _scaled_hdr_value(
        side_data.get("white_point_x"),
        50000,
    )
    white_y = _scaled_hdr_value(
        side_data.get("white_point_y"),
        50000,
    )
    max_luminance = _scaled_hdr_value(
        side_data.get("max_luminance"),
        10000,
    )
    min_luminance = _scaled_hdr_value(
        side_data.get("min_luminance"),
        10000,
    )

    values = (
        red_x,
        red_y,
        green_x,
        green_y,
        blue_x,
        blue_y,
        white_x,
        white_y,
        max_luminance,
        min_luminance,
    )

    if any(value is None for value in values):
        return None

    return (
        f"G({green_x},{green_y})"
        f"B({blue_x},{blue_y})"
        f"R({red_x},{red_y})"
        f"WP({white_x},{white_y})"
        f"L({max_luminance},{min_luminance})"
    )


def _hdr_static_from_frame_payload(
    payload: Mapping[str, Any],
) -> HdrStaticMetadata:
    """Extract only unambiguous, complete HDR10 frame-side metadata."""

    raw_frames = payload.get("frames", ())
    if not isinstance(raw_frames, (list, tuple)):
        return HdrStaticMetadata()

    mastering_values: set[str] = set()
    cll_pairs: set[tuple[int, int]] = set()

    for frame in raw_frames:
        if not isinstance(frame, Mapping):
            continue

        raw_side_data = frame.get("side_data_list", ())
        if not isinstance(raw_side_data, (list, tuple)):
            continue

        for side_data in raw_side_data:
            if not isinstance(side_data, Mapping):
                continue

            side_type = str(
                side_data.get("side_data_type", "")
            ).lower()

            if "mastering display metadata" in side_type:
                mastering = _mastering_display_from_side_data(side_data)
                if mastering is not None:
                    mastering_values.add(mastering)

            elif "content light level metadata" in side_type:
                max_cll = _int(side_data.get("max_content"), None)
                max_fall = _int(side_data.get("max_average"), None)

                if (
                    max_cll is not None
                    and max_fall is not None
                    and max_cll >= 0
                    and max_fall >= 0
                ):
                    cll_pairs.add((max_cll, max_fall))

    # Több különböző érték esetén nem választunk önkényesen:
    # a planner továbbra is blokkol, amíg nincs ellenőrzött adat.
    mastering = (
        next(iter(mastering_values))
        if len(mastering_values) == 1
        else None
    )
    cll_pair = (
        next(iter(cll_pairs))
        if len(cll_pairs) == 1
        else None
    )

    return HdrStaticMetadata(
        mastering_display=mastering,
        max_cll=cll_pair[0] if cll_pair is not None else None,
        max_fall=cll_pair[1] if cll_pair is not None else None,
    )


def _video_properties(
    item: Mapping[str, Any], native: Mapping[str, Any]
) -> VideoProperties:
    codec_name = str(item.get("codec_name", native.get("codec", "unknown"))).lower()
    profile = str(item.get("profile", native.get("profile", ""))).lower()
    codec = {
        "h264": VideoCodec.AVC,
        "avc": VideoCodec.AVC,
        "vc1": VideoCodec.VC1,
        "vc-1": VideoCodec.VC1,
        "mpeg2video": VideoCodec.MPEG2,
        "mpeg-2": VideoCodec.MPEG2,
        "hevc": VideoCodec.HEVC,
        "h265": VideoCodec.HEVC,
        "mvc": VideoCodec.MVC,
    }.get(codec_name, VideoCodec.UNKNOWN)
    three_d = (
        codec is VideoCodec.MVC
        or "multiview" in profile
        or bool(native.get("three_d", False))
    )
    side_data = item.get("side_data_list", ())
    side_text = json.dumps(side_data, sort_keys=True).lower()
    dv_profile = _int(native.get("dolby_vision_profile"), None)
    if dv_profile is None:
        for side in side_data if isinstance(side_data, list) else ():
            if (
                isinstance(side, Mapping)
                and "dovi" in str(side.get("side_data_type", "")).lower()
            ):
                dv_profile = _int(side.get("dv_profile"), None)
                break
    transfer = _optional_str(item.get("color_transfer", native.get("color_transfer")))
    primaries = _optional_str(
        item.get("color_primaries", native.get("color_primaries"))
    )
    matrix = _optional_str(item.get("color_space", native.get("color_matrix")))
    hdr10 = bool(native.get("hdr10", transfer == "smpte2084" and primaries == "bt2020"))
    mastering = _optional_str(item.get("mastering_display"))
    if mastering is None:
        mastering = _optional_str(native.get("mastering_display"))

    max_cll = _int(item.get("max_cll"), None)
    if max_cll is None:
        max_cll = _int(native.get("max_cll"), None)

    max_fall = _int(item.get("max_fall"), None)
    if max_fall is None:
        max_fall = _int(native.get("max_fall"), None)
    return VideoProperties(
        codec=codec,
        width=_int(item.get("width", native.get("width")), None),
        height=_int(item.get("height", native.get("height")), None),
        frame_rate=_frame_rate(
            item.get(
                "avg_frame_rate", item.get("r_frame_rate", native.get("frame_rate"))
            )
        ),
        field_order=_optional_str(
            item.get("field_order", native.get("field_order", "unknown"))
        ),
        bit_depth=_int(item.get("bits_per_raw_sample", native.get("bit_depth")), None),
        pixel_format=_optional_str(item.get("pix_fmt", native.get("pixel_format"))),
        color_primaries=primaries,
        color_transfer=transfer,
        color_matrix=matrix,
        color_range=_optional_str(item.get("color_range", native.get("color_range"))),
        chroma_location=_optional_str(
            item.get("chroma_location", native.get("chroma_location"))
        ),
        hdr10=hdr10,
        hdr10_static=HdrStaticMetadata(mastering, max_cll, max_fall),
        dolby_vision=bool(
            native.get("dolby_vision", dv_profile is not None or "dovi" in side_text)
        ),
        dolby_vision_profile=dv_profile,
        hdr10_base_layer=bool(native.get("hdr10_base_layer", hdr10)),
        hdr10_plus=bool(
            native.get(
                "hdr10_plus", "smpte 2094-40" in side_text or "hdr10+" in side_text
            )
        ),
        three_d=three_d,
    )


def _roles(title: str | None, forced: bool, raw_roles: Any) -> tuple[TrackRole, ...]:
    roles: list[TrackRole] = []
    for raw in raw_roles if isinstance(raw_roles, (list, tuple)) else ():
        try:
            roles.append(TrackRole(str(raw).lower()))
        except ValueError:
            pass
    lowered = (title or "").lower()
    if "commentary" in lowered or "kommentar" in lowered:
        roles.append(TrackRole.COMMENTARY)
    if "audio description" in lowered or "descriptive" in lowered:
        roles.append(TrackRole.AUDIO_DESCRIPTION)
    if "sdh" in lowered or "hearing impaired" in lowered:
        roles.append(TrackRole.SDH)
    if forced:
        roles.append(TrackRole.FORCED)
    return tuple(dict.fromkeys(roles)) or (TrackRole.UNKNOWN,)


def _stream_match_key(item: Mapping[str, Any]) -> str:
    pid = _parse_pid(item.get("pid", item.get("id")))
    return f"pid:{pid}" if pid is not None else f"index:{item.get('index', -1)}"


def _parse_pid(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None


def _frame_rate(value: Any) -> str | None:
    if value in (None, "", "0/0"):
        return None
    try:
        fraction = Fraction(str(value))
        return f"{fraction.numerator}/{fraction.denominator}"
    except (ValueError, ZeroDivisionError):
        return str(value)


def _float(value: Any, default: float | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _json_object(text: str) -> Mapping[str, Any]:
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _disc_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    bdmv = root / "BDMV"
    candidates = sorted((bdmv / "PLAYLIST").glob("*.mpls")) + sorted(
        (bdmv / "CLIPINF").glob("*.clpi")
    )
    for path in candidates:
        stat = path.stat()
        digest.update(
            path.relative_to(root).as_posix().encode("utf-8", "surrogateescape")
        )
        digest.update(str(stat.st_size).encode("ascii"))
        # Playlist/clip metadata is small and materially identifies an edition.
        with path.open("rb") as handle:
            digest.update(handle.read())
    return digest.hexdigest()


def _mkvmerge_to_probe(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    streams: list[dict[str, Any]] = []
    for track in payload.get("tracks", []):
        if not isinstance(track, Mapping):
            continue
        properties = (
            track.get("properties", {})
            if isinstance(track.get("properties"), Mapping)
            else {}
        )
        dimensions = str(properties.get("pixel_dimensions") or "x")
        streams.append(
            {
                "index": track.get("id"),
                "codec_type": track.get("type"),
                "codec_name": str(track.get("codec", "unknown")).lower(),
                "tags": {
                    "language": properties.get("language"),
                    "title": properties.get("track_name"),
                },
                "channels": properties.get("audio_channels"),
                "sample_rate": properties.get("audio_sampling_frequency"),
                "width": dimensions.split("x")[0],
                "height": dimensions.split("x")[-1],
            }
        )
    return {"streams": streams, "format": {"duration": 0}}


def _mediainfo_to_probe(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    tracks = (
        payload.get("media", {}).get("track", [])
        if isinstance(payload.get("media"), Mapping)
        else []
    )
    streams: list[dict[str, Any]] = []
    duration = 0.0
    for index, track in enumerate(tracks):
        if not isinstance(track, Mapping):
            continue
        kind = str(track.get("@type", "")).lower()
        if kind == "general":
            duration = _float(track.get("Duration"), 0.0) or 0.0
            continue
        codec_type = "subtitle" if kind == "text" else kind
        streams.append(
            {
                "index": index,
                "codec_type": codec_type,
                "codec_name": str(track.get("Format", "unknown")).lower(),
                "profile": track.get("Format_Profile"),
                "width": track.get("Width"),
                "height": track.get("Height"),
                "channels": track.get("Channels"),
                "sample_rate": track.get("SamplingRate"),
                "tags": {
                    "language": track.get("Language"),
                    "title": track.get("Title"),
                },
            }
        )
    return {"streams": streams, "format": {"duration": duration}}
