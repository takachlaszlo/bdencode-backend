"""Immutable artifact metadata and strict PNG inspection."""

from __future__ import annotations

import struct
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from bdencode.utils import sha256_file


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ArtifactKind(StrEnum):
    RAW_LOG = "raw_log"
    SANITIZED_LOG = "sanitized_log"
    MANIFEST = "manifest"
    MEDIAINFO = "mediainfo"
    VIDEO_SOURCE_NATIVE = "video_source_native"
    VIDEO_REFERENCE_ALIGNED = "video_reference_aligned"
    VIDEO_ENCODE_DECODED = "video_encode_decoded"
    VIDEO_NATIVE_YUV = "video_native_yuv"
    VIDEO_SDR_PROOF = "video_sdr_proof"
    VIDEO_NATIVE_YUV_METRICS = "video_native_yuv_metrics"
    CROP_ANALYSIS = "crop_analysis"
    SOURCE_DIAGNOSTICS = "source_diagnostics"
    AUDIO_SPECTRUM_SOURCE = "audio_spectrum_source"
    AUDIO_SPECTRUM_ENCODE = "audio_spectrum_encode"
    METRICS = "metrics"
    BBCODE = "bbcode"


@dataclass(frozen=True, slots=True)
class PngInfo:
    width: int
    height: int
    bit_depth: int
    color_type: int
    interlace_method: int

    @property
    def high_bit_depth(self) -> bool:
        return self.bit_depth > 8


@dataclass(frozen=True, slots=True)
class Artifact:
    kind: ArtifactKind
    relative_path: str
    sha256: str
    size_bytes: int
    media_type: str
    metadata: dict[str, Any]

    @classmethod
    def from_path(
        cls,
        kind: ArtifactKind,
        path: Path,
        *,
        root: Path,
        media_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> "Artifact":
        absolute = path.resolve(strict=True)
        base = root.resolve(strict=True)
        if not absolute.is_relative_to(base):
            raise ValueError("artifact must be inside its job root")
        if not absolute.is_file():
            raise ValueError("artifact is not a regular file")
        return cls(
            kind=kind,
            relative_path=absolute.relative_to(base).as_posix(),
            sha256=sha256_file(absolute),
            size_bytes=absolute.stat().st_size,
            media_type=media_type,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["kind"] = self.kind.value
        return value


def inspect_png(path: Path, *, require_high_bit_depth: bool = False) -> PngInfo:
    with path.open("rb") as handle:
        if handle.read(8) != PNG_SIGNATURE:
            raise ValueError(f"not a PNG file: {path}")
        length_data = handle.read(4)
        chunk_type = handle.read(4)
        if len(length_data) != 4 or chunk_type != b"IHDR":
            raise ValueError("PNG does not start with IHDR")
        length = struct.unpack(">I", length_data)[0]
        if length != 13:
            raise ValueError("invalid PNG IHDR length")
        data = handle.read(13)
        if len(data) != 13:
            raise ValueError("truncated PNG IHDR")
        width, height, bit_depth, color_type, compression, filtering, interlace = (
            struct.unpack(">IIBBBBB", data)
        )
    if not width or not height:
        raise ValueError("PNG has zero dimensions")
    if compression != 0 or filtering != 0:
        raise ValueError("unsupported PNG compression/filter method")
    info = PngInfo(width, height, bit_depth, color_type, interlace)
    if require_high_bit_depth and not info.high_bit_depth:
        raise ValueError("HDR-native evidence must be higher than 8-bit")
    return info
