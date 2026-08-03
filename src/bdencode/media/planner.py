"""Turn a reviewed disc scan into an auditable, shell-free encode plan."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import os
from pathlib import Path
import re
from typing import Any

from .bluray import (
    DiscKind,
    DiscScan,
    MediaStream,
    PlaylistCandidate,
    StreamKind,
    VideoCodec,
)
from .language import iso639_2_to_bcp47, normalize_iso639_2
from .profiles import EncoderSettings, VideoEncoder


DEFAULT_WORK_ROOT = Path("/home/accofil/encode")
_BCP47_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class TrackAction(StrEnum):
    COPY = "copy"
    FLAC = "flac"
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
        if any(value < 0 for value in values):
            raise ValueError("crop values cannot be negative")
        if any(value % 2 for value in values):
            raise ValueError("4:2:0 crop values must be even")

    @property
    def enabled(self) -> bool:
        return any((self.left, self.top, self.right, self.bottom))

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
    order: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.action, str):
            object.__setattr__(self, "action", TrackAction(self.action))
        if not self.stream_id:
            raise ValueError("track selection requires a stream_id")
        if self.order < 0:
            raise ValueError("track order cannot be negative")
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

        selected = self._validate_tracks(playlist, request)
        if request.settings.bframes == 0:
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
        video_argv.extend(request.settings.ffmpeg_video_args())
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
            extension = (
                ".flac"
                if selection.action is TrackAction.FLAC
                else (".mks" if stream.kind is StreamKind.SUBTITLE else ".mka")
            )
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
            elif selection.action is TrackAction.FLAC:
                argv.extend(("-sn", "-c:a", "flac", "-compression_level", "8"))
                if stream.object_audio:
                    warnings.append(
                        f"{stream.id} contains object-audio metadata; FLAC preserves channels/PCM but loses Atmos/DTS:X objects."
                    )
                    needs_review = True
            else:
                argv.extend(("-sn", "-c:a", "copy"))
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
        for intermediate, selection, stream in mux_inputs:
            track_id = "0"
            mux_argv.extend(("--language", f"{track_id}:{selection.bcp47(stream)}"))
            default = (
                selection.default if selection.default is not None else stream.default
            )
            forced = selection.forced if selection.forced is not None else stream.forced
            mux_argv.extend(
                ("--default-track-flag", f"{track_id}:{'yes' if default else 'no'}")
            )
            mux_argv.extend(
                ("--forced-display-flag", f"{track_id}:{'yes' if forced else 'no'}")
            )
            if selection.name or stream.title:
                mux_argv.extend(
                    ("--track-name", f"{track_id}:{selection.name or stream.title}")
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
            "encoder": request.settings.to_dict(),
            "field_handling": request.field_handling.value,
            "crop": asdict(request.crop),
            "dynamic_hdr_retained": False,
            "dolby_vision_retained": False,
            "three_d_retained": False,
            "hdr10_static_retained": bool(request.settings.hdr10.enabled),
            "track_selections": [
                {
                    **asdict(selection),
                    "action": selection.action.value,
                    "resolved_language": selection.bcp47(stream),
                    "source_track": stream.to_dict(),
                }
                for selection, stream in selected
            ],
        }
        return EncodePlan(
            playlist_id=playlist.playlist_id,
            source_video_codec=video_stream.video.codec,
            output_encoder=request.settings.encoder,
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
        if (
            stream.video.width is not None
            and crop.left + crop.right >= stream.video.width
        ):
            raise ValueError("horizontal crop removes the complete picture")
        if (
            stream.video.height is not None
            and crop.top + crop.bottom >= stream.video.height
        ):
            raise ValueError("vertical crop removes the complete picture")

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
                "every audio/subtitle track needs copy, FLAC or omit: "
                + ", ".join(sorted(missing))
            )
        result: list[tuple[TrackSelection, MediaStream]] = []
        for selection in sorted(
            request.track_selections, key=lambda item: (item.order, item.stream_id)
        ):
            stream = eligible[selection.stream_id]
            if (
                stream.kind is StreamKind.SUBTITLE
                and selection.action is TrackAction.FLAC
            ):
                raise ValueError("FLAC is valid only for audio tracks")
            if (
                stream.kind is StreamKind.AUDIO
                and stream.codec.casefold() == "pcm_bluray"
                and selection.action is TrackAction.COPY
            ):
                raise ValueError(
                    "Blu-ray LPCM cannot be copied into Matroska; select FLAC or omit"
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
            result.extend(
                (
                    "fieldmatch=order=auto:combmatch=full",
                    "bwdif=mode=send_frame:parity=auto:deint=interlaced",
                    "decimate",
                )
            )
        return result
