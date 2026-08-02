"""Audio integrity, objective analysis, and spectral evidence plans."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class AudioProbe:
    codec: str
    sample_rate: int
    channels: int
    channel_layout: str | None
    sample_count: int | None
    start_time: Decimal
    duration: Decimal | None
    bits_per_raw_sample: int | None


@dataclass(frozen=True, slots=True)
class AudioComparison:
    sample_rate_match: bool
    channels_match: bool
    channel_layout_match: bool
    sample_count_delta: int | None
    delay_seconds: Decimal
    duration_delta_seconds: Decimal | None

    @property
    def structurally_lossless(self) -> bool:
        return (
            self.sample_rate_match
            and self.channels_match
            and self.channel_layout_match
            and self.sample_count_delta == 0
            and self.delay_seconds == 0
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["delay_seconds"] = str(self.delay_seconds)
        if self.duration_delta_seconds is not None:
            result["duration_delta_seconds"] = str(self.duration_delta_seconds)
        return result


def parse_audio_probe(document: str | bytes | Mapping[str, Any]) -> AudioProbe:
    raw = json.loads(document) if isinstance(document, (str, bytes)) else document
    streams = [
        item
        for item in raw.get("streams", [])
        if item.get("codec_type") in (None, "audio")
    ]
    if len(streams) != 1:
        raise ValueError("audio probe must contain exactly one selected stream")
    item = streams[0]
    sample_rate = int(item["sample_rate"])
    duration = _decimal_or_none(item.get("duration"))
    sample_count = _optional_int(item.get("nb_samples"))
    if sample_count is None and duration is not None:
        sample_count = int((duration * sample_rate).to_integral_value())
    return AudioProbe(
        codec=str(item.get("codec_name", "unknown")),
        sample_rate=sample_rate,
        channels=int(item["channels"]),
        channel_layout=item.get("channel_layout"),
        sample_count=sample_count,
        start_time=Decimal(str(item.get("start_time", "0"))),
        duration=duration,
        bits_per_raw_sample=_optional_int(item.get("bits_per_raw_sample")),
    )


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value in (None, "N/A") else Decimal(str(value))


def _optional_int(value: Any) -> int | None:
    try:
        return None if value in (None, "N/A", "") else int(value)
    except (TypeError, ValueError):
        return None


def compare_audio_probes(source: AudioProbe, encoded: AudioProbe) -> AudioComparison:
    count_delta = None
    if source.sample_count is not None and encoded.sample_count is not None:
        count_delta = encoded.sample_count - source.sample_count
    duration_delta = None
    if source.duration is not None and encoded.duration is not None:
        duration_delta = encoded.duration - source.duration
    return AudioComparison(
        sample_rate_match=source.sample_rate == encoded.sample_rate,
        channels_match=source.channels == encoded.channels,
        channel_layout_match=source.channel_layout == encoded.channel_layout,
        sample_count_delta=count_delta,
        delay_seconds=encoded.start_time - source.start_time,
        duration_delta_seconds=duration_delta,
    )


def audio_probe_command(
    path: Path, stream: int = 0, *, ffprobe: str = "ffprobe"
) -> list[str]:
    return [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        f"a:{stream}",
        "-show_entries",
        "stream=codec_type,codec_name,sample_rate,channels,channel_layout,nb_samples,start_time,duration,bits_per_raw_sample",
        "-of",
        "json",
        str(path),
    ]


def pcm_hash_command(
    path: Path, stream: int = 0, *, ffmpeg: str = "ffmpeg"
) -> list[str]:
    """Hash canonical decoded PCM without depending on decoder frame boundaries.

    FFmpeg's ``framemd5`` output also records packet sizes, timestamps and frame
    boundaries.  Those can legitimately differ after a lossless FLAC transcode
    even when every decoded sample is identical.  The hash muxer instead hashes
    the canonical signed 32-bit PCM payload directly and emits only a tiny
    SHA-256 record, so a feature-length multichannel track never has to be
    materialized as raw PCM on disk.
    """
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        f"0:a:{stream}",
        "-c:a",
        "pcm_s32le",
        "-f",
        "hash",
        "-hash",
        "sha256",
        "-",
    ]


def analysis_command(
    path: Path, stream: int, log_path: Path, *, ffmpeg: str = "ffmpeg"
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-i",
        str(path),
        "-map",
        f"0:a:{stream}",
        "-af",
        "ebur128=peak=true,astats=metadata=1:reset=0,aphasemeter=video=0:phasing=1",
        "-f",
        "null",
        "-",
    ]


def spectrum_command(
    path: Path,
    stream: int,
    output_path: Path,
    *,
    width: int = 3840,
    height: int = 2160,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    if width < 320 or height < 240:
        raise ValueError("spectrum image is too small")
    # Fixed scale/legend settings are shared by source and encode for visual parity.
    filter_value = (
        f"showspectrumpic=s={width}x{height}:legend=1:color=intensity:scale=log:"
        "fscale=log:win_func=blackman:orientation=horizontal,format=rgb48be"
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "warning",
        "-i",
        str(path),
        "-map",
        f"0:a:{stream}",
        "-lavfi",
        filter_value,
        "-frames:v",
        "1",
        "-compression_level",
        "6",
        "-y",
        str(output_path),
    ]


def flac_encode_args(*, compression_level: int = 8) -> list[str]:
    if not 0 <= compression_level <= 12:
        raise ValueError("FLAC compression level must be between 0 and 12")
    return ["-c:a", "flac", "-compression_level", str(compression_level)]
