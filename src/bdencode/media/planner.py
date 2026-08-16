"""Turn a reviewed disc scan into an auditable, shell-free encode plan."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from fractions import Fraction
import os
from pathlib import Path
import re
from typing import Any, Sequence

from ..audio import (
    AUDIO_TRANSCODE_PRESETS,
    audio_encode_args,
    effective_audio_policy,
)
from .bluray import (
    DiscKind,
    DiscScan,
    MediaStream,
    PlaylistCandidate,
    StreamKind,
    TrackRole,
    VideoCodec,
)
from .language import iso639_2_to_bcp47, normalize_iso639_2
from .profiles import (
    EncoderSettings,
    VideoEncoder,
    format_frame_rate,
    parse_frame_rate,
    source_adapted_settings,
)


DEFAULT_WORK_ROOT = Path("/home/accofil/encode")
_BCP47_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class TrackAction(StrEnum):
    COPY = "copy"
    FLAC = "flac"
    AC3 = "ac3"
    EAC3 = "eac3"
    DTS = "dts"
    OMIT = "omit"


class FieldHandling(StrEnum):
    PROGRESSIVE = "progressive"
    DEINTERLACE = "deinterlace"
    IVTC = "ivtc"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class Crop:
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    def __post_init__(self) -> None:
        values = (self.left, self.top, self.right, self.bottom)
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise ValueError("crop values must be integers")
        if any(value < 0 for value in values):
            raise ValueError("crop values cannot be negative")
        if any(value % 2 for value in values):
            raise ValueError("4:2:0 crop values must be even")

    @classmethod
    def from_detected_borders(
        cls,
        *,
        left: int = 0,
        top: int = 0,
        right: int = 0,
        bottom: int = 0,
        safety: int = 0,
    ) -> Crop:
        """Safely convert measured black borders into a 4:2:0 crop.

        Each measurement is reduced by the optional safety margin and rounded
        down to an even value.  The helper never crops an unmeasured pixel and
        is therefore suitable for full-title border-union recommendations.
        """

        values = (left, top, right, bottom, safety)
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise ValueError("detected borders and safety must be integers")
        if any(value < 0 for value in values):
            raise ValueError("detected borders and safety cannot be negative")

        def safe(value: int) -> int:
            return (max(0, value - safety) // 2) * 2

        return cls(safe(left), safe(top), safe(right), safe(bottom))

    @property
    def enabled(self) -> bool:
        return any((self.left, self.top, self.right, self.bottom))

    def output_dimensions(self, width: int, height: int) -> tuple[int, int]:
        """Validate and return cropped 4:2:0 dimensions."""

        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            raise ValueError("source dimensions must be positive integers")
        if width % 2 or height % 2:
            raise ValueError("4:2:0 source dimensions must be even")
        output_width = width - self.left - self.right
        output_height = height - self.top - self.bottom
        if output_width < 16:
            raise ValueError("horizontal crop must leave at least 16 pixels")
        if output_height < 16:
            raise ValueError("vertical crop must leave at least 16 pixels")
        if output_width % 2 or output_height % 2:
            raise ValueError("4:2:0 cropped dimensions must be even")
        return output_width, output_height

    def ffmpeg_filter(self) -> str:
        return f"crop=iw-{self.left + self.right}:ih-{self.top + self.bottom}:{self.left}:{self.top}"


@dataclass(frozen=True, slots=True)
class TrackSelection:
    stream_id: str
    action: TrackAction
    language: str | None = None
    name: str | None = None
    default: bool | None = None
    forced: bool | None = None
    subtitle_kind: str | None = None
    order: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.action, str):
            object.__setattr__(self, "action", TrackAction(self.action))
        if not self.stream_id:
            raise ValueError("track selection requires a stream_id")
        for field_name in ("default", "forced"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"track {field_name} must be boolean or omitted")
        if isinstance(self.order, bool) or not isinstance(self.order, int):
            raise ValueError("track order must be an integer")
        if self.order < 0:
            raise ValueError("track order cannot be negative")
        if self.subtitle_kind not in {None, "unknown", "full", "forced"}:
            raise ValueError(
                "subtitle_kind must be unknown, full, forced or omitted"
            )
        if self.language:
            normalized = normalize_iso639_2(self.language)
            if normalized is None and not _BCP47_RE.fullmatch(self.language):
                raise ValueError("track language must be ISO 639-2 or BCP 47")

    def bcp47(self, stream: MediaStream) -> str:
        if self.language:
            return iso639_2_to_bcp47(self.language) or self.language
        if stream.language and stream.language.bcp47:
            return stream.language.bcp47
        return "und"


_LANGUAGE_NAMES = {
    "cmn": "Mandarin",
    "de": "German",
    "deu": "German",
    "en": "English",
    "eng": "English",
    "fr": "French",
    "fra": "French",
    "hu": "Hungarian",
    "hun": "Hungarian",
    "nl": "Dutch",
    "nld": "Dutch",
    "yue": "Cantonese",
    "zh": "Chinese",
    "zho": "Chinese",
}


def resolved_track_name(selection: TrackSelection, stream: MediaStream) -> str:
    """Return the same meaningful public track name for plan and final mux."""

    if selection.name:
        return selection.name
    if stream.title:
        return stream.title
    language = _LANGUAGE_NAMES.get(
        selection.bcp47(stream).casefold(), selection.bcp47(stream)
    )
    if stream.kind is StreamKind.SUBTITLE:
        if selection.subtitle_kind == "forced":
            suffix = "Forced"
        elif TrackRole.SDH in stream.roles:
            suffix = "SDH"
        else:
            suffix = "Full"
        return f"{language} {suffix}"
    if TrackRole.COMMENTARY in stream.roles:
        return f"{language} Commentary"
    if TrackRole.AUDIO_DESCRIPTION in stream.roles:
        return f"{language} Audio Description"
    if TrackRole.DUB in stream.roles:
        return f"{language} Dub"
    return f"{language} Original Mix"


def resolve_audio_defaults(
    retained: Sequence[tuple[TrackSelection, MediaStream]],
) -> dict[str, bool]:
    """Resolve exactly one reviewed audio default for plan, mux and QC."""

    audio = [entry for entry in retained if entry[1].kind is StreamKind.AUDIO]
    if not audio:
        return {}
    selected = [
        entry
        for entry in audio
        if entry[0].default is True
        or (entry[0].default is None and entry[1].default)
    ]
    if len(selected) > 1:
        raise ValueError("exactly one retained audio track may be default")
    if not selected:
        selected = [
            next(
                (
                    entry
                    for entry in audio
                    if TrackRole.DUB not in entry[1].roles
                    and TrackRole.COMMENTARY not in entry[1].roles
                    and TrackRole.AUDIO_DESCRIPTION not in entry[1].roles
                ),
                audio[0],
            )
        ]
    default_stream = selected[0][1]
    if TrackRole.DUB in default_stream.roles and any(
        TrackRole.DUB not in stream.roles
        and TrackRole.COMMENTARY not in stream.roles
        and TrackRole.AUDIO_DESCRIPTION not in stream.roles
        for _selection, stream in audio
    ):
        raise ValueError(
            "a dubbed audio track cannot be default while an original/main mix "
            "is retained"
        )
    return {stream.id: stream.id == default_stream.id for _, stream in audio}


@dataclass(frozen=True, slots=True)
class EncodeRequest:
    scan: DiscScan
    playlist_id: str
    settings: EncoderSettings
    work_dir: Path
    output_path: Path
    track_selections: tuple[TrackSelection, ...]
    field_handling: FieldHandling = FieldHandling.PROGRESSIVE
    crop: Crop = field(default_factory=Crop)
    angle: int = 1
    overwrite: bool = False
    require_explicit_track_selection: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.field_handling, str):
            object.__setattr__(
                self, "field_handling", FieldHandling(self.field_handling)
            )
        if self.angle < 1:
            raise ValueError("angle must be at least one")
        ids = [selection.stream_id for selection in self.track_selections]
        if len(ids) != len(set(ids)):
            raise ValueError("each track may be selected only once")


@dataclass(frozen=True, slots=True)
class PlannedCommand:
    stage: str
    argv: tuple[str, ...]
    expected_outputs: tuple[Path, ...]
    purpose: str

    def __post_init__(self) -> None:
        if not self.argv or not self.argv[0]:
            raise ValueError("planned command argv cannot be empty")
        if any("\x00" in item for item in self.argv):
            raise ValueError("planned command contains a NUL byte")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "argv": list(self.argv),
            "expected_outputs": [str(item) for item in self.expected_outputs],
            "purpose": self.purpose,
        }


@dataclass(frozen=True, slots=True)
class EncodePlan:
    playlist_id: str
    source_video_codec: VideoCodec
    output_encoder: VideoEncoder
    commands: tuple[PlannedCommand, ...]
    warnings: tuple[str, ...]
    decisions: dict[str, Any]
    comparison_categories: tuple[str, ...] = ("I", "P", "B")
    needs_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "playlist_id": self.playlist_id,
            "source_video_codec": self.source_video_codec.value,
            "output_encoder": self.output_encoder.value,
            "commands": [command.to_dict() for command in self.commands],
            "warnings": list(self.warnings),
            "decisions": self.decisions,
            "comparison_categories": list(self.comparison_categories),
            "needs_review": self.needs_review,
        }


class EncodePlanner:
    def __init__(
        self,
        *,
        work_root: Path = DEFAULT_WORK_ROOT,
        ffmpeg: str = "ffmpeg",
        mkvmerge: str = "mkvmerge",
    ) -> None:
        self.work_root = work_root.expanduser()
        self.ffmpeg = ffmpeg
        self.mkvmerge = mkvmerge

    def build(self, request: EncodeRequest) -> EncodePlan:
        playlist = request.scan.playlist(request.playlist_id)
        work_dir = self._within_work_root(request.work_dir, "work_dir")
        output = self._within_work_root(request.output_path, "output_path")
        if output.suffix.lower() != ".mkv":
            raise ValueError("output_path must use the .mkv extension")
        if request.angle > playlist.angle_count:
            raise ValueError(
                f"angle {request.angle} is unavailable; playlist has {playlist.angle_count} angle(s)"
            )

        video_stream = self._select_video(playlist)
        assert video_stream.video is not None
        warnings: list[str] = list(request.scan.warnings)
        needs_review = False

        if video_stream.video.three_d or request.scan.has_three_d:
            raise ValueError(
                "MVC/3D sources are unsupported; select a 2D playlist or base view"
            )
        self._validate_codec_policy(
            request.scan.disc_kind, video_stream.video.codec, request.settings.encoder
        )
        if video_stream.video.dolby_vision:
            if not video_stream.video.hdr10_base_layer:
                raise ValueError(
                    "Dolby Vision source has no confirmed HDR10 base layer"
                )
            warnings.append(
                "Dolby Vision enhancement layer and RPU metadata will be discarded; HDR10 base layer only."
            )
        if video_stream.video.hdr10_plus:
            warnings.append(
                "Dynamic HDR10+ metadata will be discarded; output retains static HDR10 only."
            )

        self._validate_hdr_and_color(video_stream, request.settings)
        if (
            video_stream.video.interlaced
            and request.field_handling is FieldHandling.PROGRESSIVE
        ):
            raise ValueError(
                "scan reports interlaced video; select deinterlace, IVTC or hybrid handling"
            )
        if (
            not video_stream.video.interlaced
            and request.field_handling is not FieldHandling.PROGRESSIVE
        ):
            warnings.append(
                "A field-processing filter was selected for a stream reported as progressive; review detection samples."
            )
            needs_review = True
        self._validate_crop(video_stream, request.crop)
        output_dimensions = self._output_dimensions(video_stream, request.crop)
        output_frame_rate = self._output_frame_rate(
            video_stream.video.frame_rate,
            request.field_handling,
        )
        effective_settings, video_policy = source_adapted_settings(
            request.settings,
            width=output_dimensions[0] if output_dimensions else None,
            height=output_dimensions[1] if output_dimensions else None,
            frame_rate=output_frame_rate,
        )
        level_policy = video_policy["h264_level_4_1"]
        if level_policy.get("applied") and level_policy.get(
            "reference_frames_adjusted"
        ):
            warnings.append(
                "H.264 level 4.1 reduced reference frames from "
                f"{level_policy['requested_reference_frames']} to "
                f"{level_policy['effective_reference_frames']} for the cropped "
                "macroblock geometry."
            )

        selected = self._validate_tracks(playlist, request)
        if effective_settings.bframes == 0:
            warnings.append(
                "B-frames are disabled, so the mandatory B-frame comparison category cannot be populated."
            )
            needs_review = True

        video_path = work_dir / "video-encoded.mkv"
        input_args = self._input_args(request.scan, playlist, request.angle)
        video_filters = self._video_filters(request.field_handling, request.crop)
        video_argv = [
            self.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y" if request.overwrite else "-n",
            *input_args,
            "-map",
            f"0:{video_stream.index}",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "0",
            "-an",
            "-sn",
            "-dn",
        ]
        if video_filters:
            video_argv.extend(("-vf", ",".join(video_filters)))
        video_argv.extend(effective_settings.ffmpeg_video_args())
        video_argv.extend(("-fps_mode", "passthrough", os.fspath(video_path)))
        commands: list[PlannedCommand] = [
            PlannedCommand(
                "encode_video",
                tuple(video_argv),
                (video_path,),
                "Decode the selected presentation timeline and encode its 2D AVC/x265 base picture.",
            )
        ]

        mux_inputs: list[tuple[Path, TrackSelection, MediaStream]] = []
        for number, (selection, stream) in enumerate(selected, start=1):
            if selection.action is TrackAction.OMIT:
                continue
            extension = ".mks" if stream.kind is StreamKind.SUBTITLE else ".mka"
            intermediate = (
                work_dir / f"track-{number:02d}-{stream.kind.value}{extension}"
            )
            argv = [
                self.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-y" if request.overwrite else "-n",
                *input_args,
                "-map",
                f"0:{stream.index}",
                "-map_metadata",
                "-1",
                "-vn",
                "-dn",
            ]
            if stream.kind is StreamKind.SUBTITLE:
                argv.extend(("-an", "-c:s", "copy", "-f", "matroska"))
            else:
                audio_policy = effective_audio_policy(
                    selection.action.value,
                    source_codec=stream.codec,
                    source_profile=stream.codec_profile,
                    source_channels=stream.channels,
                    source_sample_rate=stream.sample_rate,
                    source_bit_depth=stream.bit_depth,
                )
                argv.append("-sn")
                argv.extend(
                    audio_encode_args(
                        selection.action.value,
                        source_codec=stream.codec,
                        source_profile=stream.codec_profile,
                        source_channels=stream.channels,
                        source_sample_rate=stream.sample_rate,
                        source_bit_depth=stream.bit_depth,
                    )
                )
                argv.extend(("-f", "matroska"))
                if stream.object_audio and selection.action is not TrackAction.COPY:
                    handling = (
                        "FLAC preserves channels/PCM but omits"
                        if selection.action is TrackAction.FLAC
                        else "DTS core extraction omits"
                        if audio_policy.strategy == "dts_core_extract"
                        else "transcoding omits"
                    )
                    warnings.append(
                        f"{stream.id} contains object-audio metadata; {handling} "
                        "Atmos/DTS:X objects."
                    )
                    needs_review = True
                if audio_policy.verification_mode == "lossy_transcode":
                    preset = AUDIO_TRANSCODE_PRESETS[selection.action.value]
                    target_channels = audio_policy.channels
                    warnings.append(
                        f"{stream.id} is intentionally transcoded to lossy "
                        f"{preset.label} at {preset.bitrate_kbps} kb/s and 48 kHz."
                    )
                    if target_channels != stream.channels:
                        warnings.append(
                            f"{stream.id} is {stream.channels}-channel audio; "
                            f"{preset.label} is encoded as {target_channels} channels "
                            "(maximum 5.1)."
                        )
                elif audio_policy.strategy == "dts_core_extract":
                    warnings.append(
                        f"{stream.id}: the embedded DTS core is extracted without "
                        "re-encoding; DTS-HD/DTS:X extension data is omitted."
                    )
            argv.append(os.fspath(intermediate))
            commands.append(
                PlannedCommand(
                    f"extract_{stream.kind.value}_{number:02d}",
                    tuple(argv),
                    (intermediate,),
                    "Create one independently verifiable track intermediate.",
                )
            )
            mux_inputs.append((intermediate, selection, stream))

        mux_argv = [self.mkvmerge, "--output", os.fspath(output), os.fspath(video_path)]
        mux_track_pairs = [
            (selection, stream) for _path, selection, stream in mux_inputs
        ]
        audio_defaults = resolve_audio_defaults(mux_track_pairs)
        for intermediate, selection, stream in mux_inputs:
            track_id = "0"
            mux_argv.extend(("--language", f"{track_id}:{selection.bcp47(stream)}"))
            default = (
                audio_defaults[stream.id]
                if stream.kind is StreamKind.AUDIO
                else (
                    selection.default
                    if selection.default is not None
                    else stream.default
                )
            )
            forced = (
                stream.kind is StreamKind.SUBTITLE
                and selection.subtitle_kind == "forced"
            )
            mux_argv.extend(
                ("--default-track-flag", f"{track_id}:{'yes' if default else 'no'}")
            )
            mux_argv.extend(
                ("--forced-display-flag", f"{track_id}:{'yes' if forced else 'no'}")
            )
            mux_argv.extend(
                ("--track-name", f"{track_id}:{resolved_track_name(selection, stream)}")
            )
            mux_argv.append(os.fspath(intermediate))
        commands.append(
            PlannedCommand(
                "mux",
                tuple(mux_argv),
                (output,),
                "Mux video and explicitly selected audio/subtitle tracks; comparison artifacts remain sidecars.",
            )
        )

        decisions = {
            "disc_kind": request.scan.disc_kind.value,
            "content_kind": request.scan.content_kind.value,
            "playlist": playlist.playlist_id,
            "angle": request.angle,
            "seamless_branching": playlist.seamless_branching,
            "edition_group": playlist.edition_group,
            "edition_label": playlist.edition_label,
            "episode_number": playlist.episode_number,
            "source_video": video_stream.to_dict(),
            "requested_encoder": request.settings.to_dict(),
            "encoder": effective_settings.to_dict(),
            "video_policy": video_policy,
            "field_handling": request.field_handling.value,
            "crop": asdict(request.crop),
            "dynamic_hdr_retained": False,
            "dolby_vision_retained": False,
            "three_d_retained": False,
            "hdr10_static_retained": bool(effective_settings.hdr10.enabled),
            "track_selections": [
                {
                    **asdict(selection),
                    "action": selection.action.value,
                    "resolved_language": selection.bcp47(stream),
                    "resolved_default": (
                        audio_defaults[stream.id]
                        if stream.kind is StreamKind.AUDIO
                        and selection.action is not TrackAction.OMIT
                        else (
                            selection.default
                            if selection.default is not None
                            else stream.default
                        )
                    ),
                    "resolved_forced": (
                        stream.kind is StreamKind.SUBTITLE
                        and selection.subtitle_kind == "forced"
                    ),
                    "resolved_name": resolved_track_name(selection, stream),
                    "source_track": stream.to_dict(),
                    "effective_audio_target": (
                        effective_audio_policy(
                            selection.action.value,
                            source_codec=stream.codec,
                            source_profile=stream.codec_profile,
                            source_channels=stream.channels,
                            source_sample_rate=stream.sample_rate,
                            source_bit_depth=stream.bit_depth,
                        ).to_dict()
                        if stream.kind is StreamKind.AUDIO
                        and selection.action is not TrackAction.OMIT
                        else None
                    ),
                }
                for selection, stream in selected
            ],
        }
        return EncodePlan(
            playlist_id=playlist.playlist_id,
            source_video_codec=video_stream.video.codec,
            output_encoder=effective_settings.encoder,
            commands=tuple(commands),
            warnings=tuple(dict.fromkeys(warnings)),
            decisions=decisions,
            needs_review=needs_review,
        )

    def _within_work_root(self, path: Path, label: str) -> Path:
        root = self.work_root.resolve(strict=False)
        resolved = path.expanduser().resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{label} must be within {root}") from exc
        return resolved

    @staticmethod
    def _select_video(playlist: PlaylistCandidate) -> MediaStream:
        if not playlist.video_streams:
            raise ValueError("selected playlist contains no video stream")
        # Prefer the base/first view. Any MVC stream is rejected later.
        return playlist.video_streams[0]

    @staticmethod
    def _validate_codec_policy(
        disc_kind: DiscKind, source_codec: VideoCodec, encoder: VideoEncoder
    ) -> None:
        if disc_kind is DiscKind.BD:
            if source_codec not in {VideoCodec.AVC, VideoCodec.VC1, VideoCodec.MPEG2}:
                raise ValueError("normal BD source must be AVC, VC-1 or MPEG-2")
            if encoder is not VideoEncoder.X264:
                raise ValueError("normal BD output must use x264")
            return
        if source_codec is not VideoCodec.HEVC:
            raise ValueError("UHD source must contain a 2D HEVC base video stream")
        if encoder is not VideoEncoder.X265:
            raise ValueError("UHD output must use x265")

    @staticmethod
    def _validate_hdr_and_color(stream: MediaStream, settings: EncoderSettings) -> None:
        assert stream.video
        video = stream.video
        if video.hdr10 and not settings.hdr10.enabled:
            raise ValueError(
                "HDR10 source requires static HDR10 x265 metadata in the output settings"
            )
        if settings.hdr10.enabled and not video.hdr10:
            raise ValueError(
                "HDR10 output cannot be enabled for a source not identified as HDR10"
            )
        comparisons = (
            ("primaries", video.color_primaries, settings.color.primaries),
            ("transfer", video.color_transfer, settings.color.transfer),
            ("matrix", video.color_matrix, settings.color.matrix),
        )
        mismatches = [
            name for name, source, output in comparisons if source and source != output
        ]
        if mismatches:
            raise ValueError(
                "output color tags differ from the unconverted source: "
                + ", ".join(mismatches)
            )

    @staticmethod
    def _validate_crop(stream: MediaStream, crop: Crop) -> None:
        assert stream.video
        width = stream.video.width
        height = stream.video.height
        if crop.enabled and (width is None or height is None):
            raise ValueError("crop requires known source width and height")
        if width is not None and height is not None:
            crop.output_dimensions(width, height)

    @staticmethod
    def _output_dimensions(stream: MediaStream, crop: Crop) -> tuple[int, int] | None:
        assert stream.video
        width = stream.video.width
        height = stream.video.height
        if width is None or height is None:
            return None
        return crop.output_dimensions(width, height)

    @staticmethod
    def _output_frame_rate(
        frame_rate: str | None, field_handling: FieldHandling
    ) -> str | None:
        if frame_rate is None:
            return None
        rate: Fraction = parse_frame_rate(frame_rate)
        if field_handling is FieldHandling.IVTC:
            rate *= Fraction(4, 5)
        elif field_handling is FieldHandling.HYBRID:
            # Keep this advisory FFmpeg plan equivalent to the worker's
            # HYBRID_SAFE_BOB VapourSynth graph: one output for every field.
            rate *= 2
        return format_frame_rate(rate)

    @staticmethod
    def _validate_tracks(
        playlist: PlaylistCandidate, request: EncodeRequest
    ) -> list[tuple[TrackSelection, MediaStream]]:
        eligible = {
            stream.id: stream
            for stream in playlist.streams
            if stream.kind in {StreamKind.AUDIO, StreamKind.SUBTITLE}
        }
        selections = {item.stream_id: item for item in request.track_selections}
        unknown = set(selections) - set(eligible)
        if unknown:
            raise ValueError(
                f"unknown selected stream(s): {', '.join(sorted(unknown))}"
            )
        missing = set(eligible) - set(selections)
        if request.require_explicit_track_selection and missing:
            raise ValueError(
                "every audio/subtitle track needs an explicit output action or omit: "
                + ", ".join(sorted(missing))
            )
        result: list[tuple[TrackSelection, MediaStream]] = []
        for selection in sorted(
            request.track_selections, key=lambda item: (item.order, item.stream_id)
        ):
            stream = eligible[selection.stream_id]
            if stream.kind is StreamKind.SUBTITLE and selection.action not in {
                TrackAction.COPY,
                TrackAction.OMIT,
            }:
                raise ValueError(
                    f"{selection.action.value} is valid only for audio tracks"
                )
            if stream.kind is StreamKind.AUDIO and (
                selection.forced is not None or selection.subtitle_kind is not None
            ):
                raise ValueError(
                    "audio tracks cannot define forced or subtitle_kind fields"
                )
            if (
                stream.kind is StreamKind.SUBTITLE
                and selection.action is not TrackAction.OMIT
            ):
                if selection.subtitle_kind not in {"full", "forced"}:
                    raise ValueError(
                        "retained subtitles require reviewed full/forced classification"
                    )
                if (
                    selection.subtitle_kind == "forced"
                    and selection.forced is False
                ) or (
                    selection.subtitle_kind == "full" and selection.forced is True
                ):
                    raise ValueError(
                        "subtitle forced flag conflicts with subtitle_kind"
                    )
            if (
                stream.kind is StreamKind.AUDIO
                and stream.codec.casefold() == "pcm_bluray"
                and selection.action is TrackAction.COPY
            ):
                raise ValueError(
                    "Blu-ray LPCM cannot be copied into Matroska; select FLAC, "
                    "AC-3, E-AC-3, DTS or omit"
                )
            result.append((selection, stream))
        return result

    @staticmethod
    def _input_args(
        scan: DiscScan, playlist: PlaylistCandidate, angle: int
    ) -> tuple[str, ...]:
        return (
            "-playlist",
            str(int(playlist.playlist_id)),
            "-angle",
            str(angle),
            "-i",
            f"bluray:{scan.source}",
        )

    @staticmethod
    def _video_filters(field_handling: FieldHandling, crop: Crop) -> list[str]:
        result: list[str] = []
        if crop.enabled:
            result.append(crop.ffmpeg_filter())
        if field_handling is FieldHandling.DEINTERLACE:
            result.append("bwdif=mode=send_frame:parity=auto:deint=all")
        elif field_handling is FieldHandling.IVTC:
            result.extend(("fieldmatch=order=auto:combmatch=full", "decimate"))
        elif field_handling is FieldHandling.HYBRID:
            result.append("bwdif=mode=send_field:parity=auto:deint=all")
        return result
