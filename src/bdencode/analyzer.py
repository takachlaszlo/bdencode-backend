"""Read encoder settings and attachment inventory from a finished MKV."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .process import CommandRunner


class CaptureProtocol(Protocol):
    def capture(
        self, argv: Sequence[str | Path], *, timeout: float = 30, check: bool = True
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class MkvAnalysis:
    path: str
    container: dict[str, Any]
    video_tracks: tuple[dict[str, Any], ...]
    audio_tracks: tuple[dict[str, Any], ...]
    subtitle_tracks: tuple[dict[str, Any], ...]
    attachments: tuple[dict[str, Any], ...]
    encoder_settings: tuple[dict[str, Any], ...]
    comparison_attachment_violation: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "container": self.container,
            "video_tracks": list(self.video_tracks),
            "audio_tracks": list(self.audio_tracks),
            "subtitle_tracks": list(self.subtitle_tracks),
            "attachments": list(self.attachments),
            "encoder_settings": list(self.encoder_settings),
            "comparison_attachment_violation": self.comparison_attachment_violation,
        }


class MkvAnalyzer:
    def __init__(self, runner: CaptureProtocol | None = None) -> None:
        self.runner = runner or CommandRunner()

    def analyze(self, path: Path) -> MkvAnalysis:
        target = path.expanduser().resolve(strict=True)
        if not target.is_file() or target.suffix.lower() != ".mkv":
            raise ValueError("analysis target must be an existing MKV file")
        mkv = self._json(
            ["mkvmerge", "--identify", "--identification-format", "json", target]
        )
        media = self._json(["mediainfo", "--Output=JSON", target])
        probe = self._json(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-show_entries",
                "stream=index,codec_type,codec_name,profile,width,height,pix_fmt,bits_per_raw_sample,color_range,color_space,color_transfer,color_primaries:stream_tags:format=format_name,duration,size,bit_rate:format_tags",
                "-of",
                "json",
                target,
            ]
        )
        tracks = tuple(mkv.get("tracks", []))
        by_type = {
            name: tuple(item for item in tracks if item.get("type") == name)
            for name in ("video", "audio", "subtitles")
        }
        attachments = tuple(
            {
                "id": item.get("id"),
                "name": item.get("file_name"),
                "description": item.get("description"),
                "mime_type": item.get("content_type"),
                "size": item.get("size"),
            }
            for item in mkv.get("attachments", [])
        )
        settings = tuple(self._encoder_settings(media, probe))
        comparison_violation = any(
            any(
                marker in str(value or "").casefold()
                for marker in ("comparison", "compare", "spectrogram", "vmaf")
            )
            for attachment in attachments
            for value in (attachment.get("name"), attachment.get("description"))
        )
        return MkvAnalysis(
            path=str(target),
            container={
                "mkvmerge": mkv.get("container", {}),
                "ffprobe": probe.get("format", {}),
            },
            video_tracks=by_type["video"],
            audio_tracks=by_type["audio"],
            subtitle_tracks=by_type["subtitles"],
            attachments=attachments,
            encoder_settings=settings,
            comparison_attachment_violation=comparison_violation,
        )

    def _json(self, argv: Sequence[str | Path]) -> Mapping[str, Any]:
        completed = self.runner.capture(argv, timeout=120, check=True)
        document = json.loads(completed.stdout)
        if not isinstance(document, Mapping):
            raise ValueError("media tool returned a non-object JSON document")
        return document

    @staticmethod
    def _encoder_settings(
        media: Mapping[str, Any], probe: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        media_tracks = (
            media.get("media", {}).get("track", [])
            if isinstance(media.get("media"), Mapping)
            else []
        )
        for item in media_tracks:
            if not isinstance(item, Mapping) or item.get("@type") != "Video":
                continue
            fields = {
                key: item.get(key)
                for key in (
                    "Format",
                    "Format_Profile",
                    "CodecID",
                    "Encoded_Library",
                    "Encoded_Library_Name",
                    "Encoded_Library_Version",
                    "Encoded_Library_Settings",
                    "Writing_library",
                    "Encoding_settings",
                )
                if item.get(key) not in (None, "")
            }
            if fields:
                result.append({"source": "mediainfo", "fields": fields})
        for stream in probe.get("streams", []):
            if not isinstance(stream, Mapping) or stream.get("codec_type") != "video":
                continue
            tags = (
                stream.get("tags", {})
                if isinstance(stream.get("tags"), Mapping)
                else {}
            )
            fields = {
                key: value
                for key, value in tags.items()
                if key.casefold()
                in {
                    "encoder",
                    "encoder_settings",
                    "encoding_settings",
                    "writing_library",
                }
            }
            if fields:
                result.append(
                    {
                        "source": "ffprobe",
                        "stream": stream.get("index"),
                        "fields": fields,
                    }
                )
        return result
